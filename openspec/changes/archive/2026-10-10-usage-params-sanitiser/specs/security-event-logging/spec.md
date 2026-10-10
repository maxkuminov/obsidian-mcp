## ADDED Requirements

### Requirement: A dropped refusal row SHALL be recorded once, with the weight it carried
The server SHALL invoke the security-event emitter exactly once for `usage_refusal_row_dropped`, at warning level, each time the refusal coalescer drops a planned row whose write was `unstorable`. The record SHALL carry only `tool`, `reason` (the row's marker, `rate_limited` or `slot_timeout`), `count` (the row's `1 + suppressed`) and `user_id` when the row is attributed. Its suppression subject SHALL be the subject `usage_log_failed` uses for the same row. Whether the record reaches the sink SHALL remain subject to the global per-subject suppressor. These field names already carry bounds in the formatter's allow-list, and this requirement SHALL NOT add or widen a field bound. The record SHALL NOT carry the row's parameters, the exception's message or a traceback, because the driver's message quotes the bound parameters. A failure to emit SHALL NOT raise into the request path or the housekeeping tick. The event SHALL be catalogued in `EVENT_FIELDS` and in the architecture note's event table. The emission is a best-effort account of the dropped weight: a suppressed record's `count` is not summed by the suppressor's summary, and the record does not name the principal or scope, so it SHALL NOT be relied on for exact reconciliation of coalescer counts.

#### Scenario: A drop is recorded with its weight
- **WHEN** the coalescer drops an unstorable planned row that represented four refusals
- **THEN** exactly one `usage_refusal_row_dropped` emission SHALL occur, with `count` 4 and the row's tool and marker

#### Scenario: Nothing outside the allow-list is written
- **WHEN** the dropped row's parameters contain note content and a vault path
- **THEN** the record SHALL contain neither, and no field outside its declared four

#### Scenario: A requeued row is not a drop
- **WHEN** a planned row's write fails for a non-data reason and is requeued
- **THEN** no `usage_refusal_row_dropped` emission SHALL occur

## MODIFIED Requirements

### Requirement: Every event SHALL pass exactly one allowance check, on a subject a caller cannot mint

The server SHALL bound the number of records that reach the log sink at **every level, informational records included**, per event and subject within a time window and per subject across all events, SHALL count what it withholds, and SHALL emit one summary record naming the event and the suppressed count. The allowance SHALL be checked **exactly once per emission attempt**: a caller SHALL acquire a permit for an event and subject, which charges the allowance, and the emitting call SHALL consume that permit **without performing a second check**, so that a caller that must do work to build its fields cannot be charged twice or escape the bound. The subject SHALL be computable before any work the permit gates, and SHALL be the authenticated user id when the request already resolved one and otherwise the trusted client address; a caller-supplied or caller-derived value — a token tag, a submitted username, a submitted client id — SHALL NOT be used as a subject, so that rotating credentials cannot mint fresh allowances. A summary SHALL be emitted when the next attempt for that key arrives after its window closed, **before any entry carrying a nonzero withheld count is evicted**, and for any outstanding count at shutdown; a summary SHALL carry the suppressed event's own level and SHALL NOT itself be suppressed or counted. Suppression state SHALL be bounded in size, SHALL fail open, SHALL never raise into a request path, and SHALL apply to log records only: a `usage_logs` row for a refused or failed tool call SHALL always be written, with one exception that is not suppression: a coalesced refusal row the database terminally rejects as data (`unstorable`, per `mcp-rate-limits`) is dropped and is accounted for, best-effort, by one `usage_refusal_row_dropped` emission attempt. **Every security, refusal or credential-outcome record that a caller can trigger repeatedly through a request SHALL be emitted through this mechanism**, including the events that exist today and are written directly to a logger; background and once-per-pass work — indexing, embedding, filesystem housekeeping and startup — SHALL remain outside it, so that suppression can never hide the operational errors the health page exists to show.

#### Scenario: An existing refusal event is bounded too

- **WHEN** a caller drives an existing caller-triggerable refusal — a tool call with no vault assignment, an over-quota tool call, an authentication failure, or a transfer publication refusal — far more times than the per-window limit
- **THEN** those records SHALL be bounded and accounted exactly as the new events are, and SHALL NOT reach the sink unbounded by way of a direct logger call

#### Scenario: A failing session-activity write is bounded

- **WHEN** the server's periodic session-activity write fails repeatedly for one signed-in browser, so that the interval which throttles that write never advances
- **THEN** the resulting records SHALL be bounded by the same allowance as every other event, keyed on the resolved user, SHALL carry the failing stage and the exception's class without its text, and the withheld count SHALL be stated

#### Scenario: Background work is not suppressed

- **WHEN** the indexer, the embed pass or filesystem housekeeping logs a warning or an error
- **THEN** that record SHALL reach the sink and the ring buffer without passing through suppression

#### Scenario: A refusal flood is bounded and accounted

- **WHEN** one subject triggers the same refusal event far more times than the per-window limit
- **THEN** at most the configured number of records SHALL reach the sink in that window, and one summary record SHALL name the event and the number withheld

#### Scenario: Rotating credentials do not mint allowances

- **WHEN** one client address presents a different unknown bearer token on every request, far more times than the per-window limit
- **THEN** the records SHALL be suppressed on that address's allowance, and the differing token tags SHALL NOT create a new allowance per token

#### Scenario: One subject cannot multiply its allowance across events

- **WHEN** one subject triggers many different refusal events in one window
- **THEN** the total number of its records reaching the sink SHALL be bounded by the per-subject cap

#### Scenario: Suppression does not hide other subjects

- **WHEN** one subject is being suppressed and a different subject triggers the same event
- **THEN** the second subject's record SHALL be emitted

#### Scenario: Outstanding counts are not lost at shutdown

- **WHEN** a window holds a nonzero suppressed count and the process shuts down before the next event for that key
- **THEN** a summary record SHALL be emitted during shutdown

#### Scenario: Outstanding counts are not lost to eviction

- **WHEN** the suppression state is at its size bound and an entry holding a nonzero withheld count is the one chosen for eviction
- **THEN** that entry's summary SHALL be emitted before it is evicted, so the withheld count is never lost

#### Scenario: An informational flood is bounded and accounted

- **WHEN** one subject drives an informational outcome — such as a replayed consent or a logout — far more times than the per-window limit on a route no rate limit covers
- **THEN** at most the configured number of informational records SHALL reach the sink in that window and one summary SHALL name the exact number withheld

#### Scenario: The allowance is charged once, not twice

- **WHEN** a call site acquires a permit and then emits with it
- **THEN** exactly one unit of that subject's allowance SHALL be consumed for that outcome, and the emitting call SHALL NOT re-evaluate the limit

#### Scenario: An acquired permit that is never spent is still charged

- **WHEN** a call site acquires a permit and then fails to emit
- **THEN** the allowance SHALL remain charged, so the failure direction is a quieter log rather than an unbounded one

#### Scenario: The audit row is never suppressed

- **WHEN** a read-only credential is refused a write tool more times than the per-window log limit
- **THEN** every one of those calls SHALL still write its `usage_logs` row carrying the `permission_denied` marker

#### Scenario: Suppression cannot break a request

- **WHEN** the suppressor's internal state raises for any reason
- **THEN** the record SHALL still be emitted and the request SHALL complete normally
