## MODIFIED Requirements

### Requirement: Rate refusals are recorded through a self-contained coalescer

A call refused by either token bucket SHALL be recorded in `usage_logs` — unless its coalesced row is terminally `unstorable` and dropped as specified below — with the `rate_limited` marker and a `rate_limit_scope` string naming which bucket fired; a call refused by the argument length cap SHALL be recorded with the `argument_too_long` marker. Both markers SHALL be classified **pre-body** and SHALL be enumerated by the shared pre-body-refusal predicate, so refused calls are counted as refusals rather than folded into latency percentiles.

Because a rate refusal occurs at the caller's arrival rate — the very rate nothing else bounds — `rate_limited` rows SHALL be **coalesced** on the key `(principal, tool, marker, scope)`, the scope included so a write-bucket refusal is never attributed to the general one. At most one row SHALL be written per key per `MCP_REFUSAL_LOG_INTERVAL_SECONDS`; refusals arriving inside an open window SHALL increment an in-memory pending count and SHALL issue no statement of any kind, neither an INSERT nor an UPDATE.

**The arithmetic SHALL be exact, and the two flush paths differ.** `pending` SHALL count the refusals observed since the last row was written that no row yet represents, and every reader SHALL take a row to represent `1 + suppressed` refusals.

- **Window opening.** The first refusal for a key SHALL write its own row with `suppressed = 0` and set `pending` to zero — that row represents exactly itself.
- **Rollover, triggered by a new refusal.** A refusal arriving after the window has closed SHALL write a row with `suppressed = pending`, **the new refusal being that row's base**, and SHALL then reset the window and set `pending` to zero.
- **Standalone flush, triggered by the periodic indexer tick or by shutdown.** A closed window with `pending` greater than zero SHALL write a row with `suppressed = pending − 1`, because there is no new refusal to serve as the row's base and the row itself must stand for one of the pending refusals. A closed window with `pending` of zero SHALL write **no** row, because the refusal that opened it already has one.

While every planned row for a key is storable, the sum of `1 + suppressed` across every row written for that key SHALL therefore equal the number of refusals observed for it, with no double counting at a rollover and no double counting at a flush. A row dropped as `unstorable` is accounted for on a **best-effort** basis only: its weight is carried by one `usage_refusal_row_dropped` emission attempt, which the log suppressor may withhold, and which names the tool, marker and user but not the principal or scope. No exact per-key reconciliation of dropped weight from the log sink is claimed.

**The coalescer entry SHALL hold the complete, immutable attribution of the row it will write** — the owning user, the credential identifiers, the denormalised actor triple, the tool, the marker, the scope and the bounded params — captured when the window opened. A deferred flush SHALL therefore read no request-scoped context variable and SHALL NOT depend on the credential still existing; a flush whose foreign key no longer resolves SHALL land through the existing usage-log recovery that clears the credential identifiers and keeps the denormalised actor columns.

**A planned row SHALL be acknowledged when it lands, requeued when its write fails for any reason other than the data, and dropped when the database rejects its data.** `write_planned_row` SHALL write through the usage writer's three-valued outcome. On `landed` it SHALL release the row. On `failed`, or when the writer raises an `Exception`, it SHALL requeue the row's whole weight exactly as before: into the open window, or into a window re-created with the row's original start when the flush path had retired it. Cancellation SHALL requeue and propagate as before. On `unstorable` (the terminal insert failed with SQLSTATE class 22, with 54000, or with a bare `UnicodeEncodeError`) it SHALL release the row **without** requeueing: it SHALL NOT re-create a retired window, SHALL NOT add the weight to an open window's `pending`, and SHALL make exactly one `usage_refusal_row_dropped` emission attempt carrying the row's weight as `count`. A row so dropped SHALL NOT be attempted again by any later tick, rollover or shutdown flush.

`argument_too_long` SHALL NOT be coalesced: it is refused below the general bucket, so its rate is already bounded by that bucket, and a second mechanism would buy nothing. Coalescer cardinality SHALL be bounded by the same registry cap as the buckets, with keys beyond the cap folded into shared overflow entries keyed on `(tool, marker, scope)` — dropping only the principal from the key, so an overflowed row still names the tool, the marker and the control that fired and loses per-principal attribution alone. `rate_limit_scope` SHALL be a JSON string that no reader casts; `suppressed` SHALL be a JSON integer read with a guarded cast.

#### Scenario: Cancellation preserves rows for the shutdown flush

- **WHEN** an immediate refusal write or periodic flush is cancelled before its write is confirmed
- **THEN** its unconfirmed weight and every retired but unattempted row SHALL return to pending state before cancellation propagates, allowing the shutdown flush to record them

#### Scenario: Planned rows retain registered ownership

- **WHEN** a planned row awaits persistence and its entry becomes full and idle
- **THEN** the sweep SHALL retain the entry until acknowledgement or requeue, and new principals SHALL use the existing overflow behavior when the registry is at its cap

#### Scenario: A refused call leaves a marked usage row

- **WHEN** a principal's first refusal for a key occurs and its row is storable
- **THEN** exactly one `usage_logs` row SHALL be written for it, naming the same tool and actor an executed call would, carrying the marker and a `rate_limit_scope`, and the tool body SHALL NOT have run

#### Scenario: A refusal loop writes no statement per refusal

- **WHEN** one principal is refused many times for one tool and scope inside one coalescing interval
- **THEN** no INSERT and no UPDATE SHALL be issued for the refusals after the first, proven by a statement-counting test

#### Scenario: A deferred flush needs neither request context nor a live credential

- **WHEN** a pending count is flushed by the periodic tick or at shutdown, after every request-scoped context variable has been cleared and after the credential that produced the refusals has been deleted
- **THEN** the row SHALL still be written with the attribution captured when the window opened, SHALL carry the denormalised actor columns, and SHALL NOT raise

#### Scenario: A single refusal followed by a flush writes exactly one row

- **WHEN** exactly one refusal occurs for a key and the window later closes with no further refusal, and the tick or shutdown flush runs
- **THEN** the flush SHALL write **no** row, only the opening row SHALL exist, its `suppressed` SHALL be 0, and the sum of `1 + suppressed` SHALL be 1

#### Scenario: A standalone flush does not double-count the opening refusal

- **WHEN** five refusals occur for a key inside one window and the tick or shutdown flush then runs
- **THEN** two rows SHALL exist — the opening row with `suppressed` 0 and the flush row with `suppressed` 3 — and the sum of `1 + suppressed` SHALL be exactly 5

#### Scenario: A rollover counts the triggering refusal as the row's base

- **WHEN** five refusals occur for a key inside one window, the window closes, and a sixth refusal then arrives
- **THEN** the rollover row SHALL carry `suppressed` 4 with the sixth refusal as its base, the sum of `1 + suppressed` across both rows SHALL be exactly 6, and the pending count SHALL be reset to zero

#### Scenario: Pending counts are flushed and the totals are exact

- **WHEN** many refusals occur for one key across several windows, mixing rollovers with a final tick or shutdown flush, and every planned row is storable
- **THEN** every refusal SHALL be represented exactly once, and the sum of `1 + suppressed` across every written row SHALL equal the number of refusals

#### Scenario: Scopes are not merged

- **WHEN** one principal is refused by the general bucket and by the write bucket for the same tool inside one interval
- **THEN** the two SHALL be coalesced separately and each scope's count SHALL be attributable on its own row

#### Scenario: An over-long argument writes its own row

- **WHEN** a principal is refused repeatedly by the argument length cap
- **THEN** each refusal SHALL write its own `usage_logs` row rather than being coalesced, bounded by the general bucket that runs above it

#### Scenario: The markers are distinct and separable

- **WHEN** an operator reads the marker register
- **THEN** `rate_limited` and `argument_too_long` SHALL each be used by exactly one refusal branch, SHALL be distinguishable without parsing a scope string, and SHALL differ from every existing marker

#### Scenario: An unstorable flushed row is dropped once, not retried every tick

- **WHEN** a pending count is flushed by the periodic tick and the database rejects the row with a data-class SQLSTATE
- **THEN** exactly one INSERT SHALL have been attempted for it, no window SHALL be re-created for its key, exactly one `usage_refusal_row_dropped` emission SHALL be attempted with `count` equal to the row's `1 + suppressed`, and the next tick SHALL issue no INSERT for it, proven by a statement-counting test

#### Scenario: An unstorable immediate row is dropped without inflating the open window

- **WHEN** the opening or rollover row for a key is rejected with a data-class SQLSTATE while its window stays open
- **THEN** the window's `pending` SHALL NOT be increased by the dropped weight, the entry's in-flight count SHALL return to its prior value, and one `usage_refusal_row_dropped` emission attempt SHALL carry the dropped weight

#### Scenario: A non-data failure still requeues

- **WHEN** a planned row's write fails because the writer permit was refused, the connection was lost, or the writer raised an exception carrying no data-class SQLSTATE
- **THEN** the row's whole weight SHALL be requeued exactly as before and no `usage_refusal_row_dropped` record SHALL be emitted

#### Scenario: A refusal for an argument carrying an unstorable value lands its row

- **WHEN** a principal is refused by a token bucket on a call whose **logged** argument contains U+0000, an unpaired surrogate or a non-finite float
- **THEN** the coalesced `rate_limited` row SHALL land on its first attempt, against a real PostgreSQL database, with the argument rendered and named in `rendered_params`
