## Context

Issue #310 (LOW). A tool argument carrying U+0000, a lone surrogate or a non-finite float makes the `usage_logs` INSERT fail with a data-class SQLSTATE. The row is lost, and for a coalesced refusal the unstorable template is retried on every tick. See `proposal.md` for the reachability evidence.

### Every writer of `usage_logs.params` (verified on 16b8220)

| # | Writer | Source of `params` | Reaches the DB through | Covered |
| --- | --- | --- | --- | --- |
| W1 | `_tracked` success / refusal tail (`tools.py`, the `logged = named_params()` block) | caller args (via `transforms`, `_truncate_params`), outcome markers (`extra`, `BodyOutcome`), `timing.current()` (server telemetry, including `result_paths` and `source_path`, which are vault paths) | `_log_usage` → `write_usage_row` → `_write_usage_row_admitted` → `_insert_usage` | yes |
| W2 | `_record_tool_failure` (body raised) | `named_params()` + `timing.current()` + `tool_exception` / `error_type` | `_log_usage` → … | yes |
| W3 | `_rate_refusal_template` (bucket refusal), immediate and deferred | `named_params()` captured into the coalescer template + markers + provenance | `rate_limits.write_planned_row` → `write_usage_row` → … | yes |
| W4 | `slot_template` (enforce `slot_timeout`), immediate and deferred | same as W3 plus `concurrency_scope` / `queue_ms` | same as W3 | yes |
| W5 | `_merge_observation` (writer shadow / queue observation, inside `write_usage_row`) | server data only | `_write_usage_row_admitted` | yes (renders after merge) |
| W6 | `_with_provenance` | server data only | part of W1–W4 | yes |
| W7 | Transfer `_log_row` (`upload_file`, `download_file`, `src/transfer/routes.py`) | `transfer_tokens.path` (a PostgreSQL `text` value) + integer `size` | its own session, `session.add` in the redemption transaction | **no**, see D3 |

No other `UsageLog(...)` constructor and no raw `INSERT INTO usage_logs` / `UPDATE usage_logs SET params` exists in `src/` (grep for `UsageLog(`, `usage_logs` with insert/update).

## Goals / Non-Goals

**Goals**
- Every row W1–W6 build lands, whatever the caller put in its arguments.
- What was rendered is visible on the row and cannot be confused with what the caller literally sent.
- An unstorable coalesced row stops being retried. Its weight is accounted for in a security event, not lost silently.
- Non-data failures keep today's requeue behaviour exactly.

**Non-Goals**
- Reversible encoding of arbitrary values. This is an audit trail, and nothing replays it.
- Changing what any refusal refuses.
- Touching the transfer, indexer, OAuth or panel code.

## Decisions

### D1. Rendering of each unstorable value

Rendering is applied **per top-level `params` key**. A top-level value is *rendered* when it contains, at any depth, a string (key or value) with U+0000 or an unpaired surrogate, a non-finite float, or a value of a non-JSON type. When a top-level value is rendered, **every string inside it** goes through the escape grammar below, not only the offending ones. A top-level value with none of these is stored byte-for-byte as today.

Escape grammar inside a rendered value:

| Input | Stored as | Why |
| --- | --- | --- |
| `\` (U+005C) | `\\` (two characters) | Makes the grammar unambiguous. Inside a rendered value every backslash begins exactly one of the three sequences in this table, so a literal `\x00` the caller typed (stored `\\x00`) can never be read as a rendered NUL (stored `\x00`). |
| U+0000 | `\x00` (four characters) | Python's own escape spelling, the same family `backslashreplace` uses for surrogates, so one grammar covers both. `\0` was considered and rejected: it is ambiguous against a following digit in C and Python octal escapes (`\012`). |
| an unpaired surrogate U+D800–U+DFFF | `\udXXX`: six characters, lowercase hex, exactly what `str.encode("utf-8", "backslashreplace")` emits | Matches the #149 vocabulary ("unpaired surrogate"), is visible, and is plain ASCII, so it is storable. A *paired* surrogate cannot occur, because `json.loads` combines a valid pair into one astral code point. |
| `float('nan')`, `inf`, `-inf` | the strings `.nan`, `.inf`, `-.inf`, from `src.services.vault.non_finite_token` | The #154 canonical tokens, so the usage log spells a NaN the same way `read_note` and the index do. Inside a rendered value they are strings. A caller-sent string `".nan"` in the same value is indistinguishable (L1). |
| a non-JSON value (tuple / set / frozenset → list in iteration order; anything else, such as a `datetime` from a `transforms` entry → `str(value)`) | the list, or the escaped string | Today such a value fails `json.dumps` with `TypeError`, which carries no SQLSTATE, so it is lost (and a planned one is requeued forever). After rendering it is storable. |
| a non-string dict key | `str(key)` (a non-finite float key → its token), then escaped | JSON keys are strings. On a post-render key collision **the first key wins**, which is the rule the indexer's `_jsonb_value` already uses. Collisions are impossible between two string keys, because the grammar is injective; they are possible only with a non-string key (L2). |
| `bool`, `int`, finite `float`, `None` | unchanged | Already storable. `bool` is checked before `int`, the same care `non_finite_token` takes. |

**The marker.** When any top-level value is rendered, the row carries `params["rendered_params"]`: a JSON array of those top-level key names, sorted. The key is absent when nothing was rendered, so an ordinary row's shape does not change. `rendered_params` is a reserved name. A unit test asserts that no registered tool has a parameter by that name and that no existing marker or telemetry key uses it. A top-level *key* that itself contains an unstorable character (defensive only, because top-level keys are parameter names and server markers) is escaped by the same grammar, and the escaped name is what appears in the list.

**Termination.** The walk is iterative with an explicit stack and **no depth limit**. This follows the house rule `_first_unencodable_argument` states: a depth limit is "a hole with a number on it". Total size is already bounded by the request-body budget. A container already on the current path renders as the string `<cycle>`. JSON cannot express a cycle, but `transforms` and in-process callers can produce one.

**Totality.** `render_usage_params` never raises. If it fails internally anyway (a `__str__` that raises, say), each top-level value it could not render is replaced by the string `<unrenderable>` and listed in `rendered_params`. The row still lands with its server markers (`error`, `suppressed`, `concurrency`, …) intact.

### D2. Where the renderer sits

At the top of `_write_usage_row_admitted`, before the first `_insert_usage`, so that:

- one call site covers W1–W6. That includes `timing.current()` telemetry, which `named_params()` never sees: `result_paths` and `source_path` carry vault paths, and a non-UTF-8 filename decoded with `surrogateescape` yields lone surrogates (the test uses `source_path`, because `timing.record_results` currently raises on such a path before it can be recorded, a separate defect);
- the FK-cleared retry (`retry = dict(values, …)`) is built from the **rendered** values, so the renderer runs exactly once per write and need not be idempotent;
- a coalescer template stays raw in memory and is rendered afresh on each attempt. That is deterministic, so a retry after a non-data failure stores the same bytes.

It does **not** sit in `named_params()` (the issue's suggestion). That would leave the telemetry merge and the observation merge unscreened, and it would put the escape grammar into the in-memory values the refusal paths also read.

The function itself may live in `tools.py` or in a new `src/services/usage_params.py`. The new module is preferred, because it is pure, has no dependency on the request context, and is easy to unit-test. The test and doc tasks name it as `render_usage_params` either way.

### D3. The transfer writer (W7) is not covered

`_log_row`'s params are `{"path": token.path, "size": <int>}`. `token.path` was read from `transfer_tokens.path`, a PostgreSQL `text` column, which cannot hold U+0000 and, being valid server-encoded text, cannot hold an unpaired surrogate. `size` comes from `os.fstat` or the stream byte count and is always an `int`. So W7 cannot carry any of the three values. Covering it would mean touching `src/transfer/`, which is out of scope for this slice. A unit test pins the claim: it constructs `_log_row` params for an upload and a download and asserts `render_usage_params(p) == p`. Passing is the expected result, and the test exists so that a future field added to W7 trips it.

### D4. Interaction with `_truncate_params`

`named_params()` truncates first (top-level strings to `_MAX_PARAM_LEN` = 200 code points plus `…`). Rendering happens later, at the insert. Truncation works on code points, so it cannot split a surrogate pair into a lone surrogate (a pair is already one code point), and it cannot cut an escape sequence, because escapes do not exist yet. The stored length of a top-level string is therefore at most 6 × 200 + 1 characters (every code point a lone surrogate). Nested strings are not truncated today and are not truncated by this change (proposal, Out of scope). Rendering grows them by at most 6×.

### D5. Write outcome and the data-class classification

`_write_usage_row_admitted` returns a three-valued `UsageWriteOutcome`:

- `landed`;
- `unstorable`: the terminal insert attempt (the initial one, or the FK-cleared retry) failed and `src.services.indexer.poison_sqlstate(exc)` is not `None`. That covers SQLSTATE class 22, 54000, or a bare `UnicodeEncodeError` with no SQLSTATE anywhere in the chain;
- `failed`: anything else. That includes the writer-capacity refusal in `write_usage_row`, connection and interface failures (which `poison_sqlstate` already classifies as not poison), and a `TypeError` / `ValueError` from serialisation.

`poison_sqlstate` is reused rather than reimplemented. It already walks `orig` and `__cause__` (never `__context__`) and handles asyncpg's client-side `DataError(22000)`. It is imported lazily inside the function, the way `tools.py` already imports `_note_title` from the indexer, to avoid an import cycle. The indexer is not modified. A first FK violation followed by a data-class failure on the retry is `unstorable`. A data-class failure on the first attempt is never retried with FKs cleared, because the FK path is entered only on 23503, as today.

`write_usage_row(values) -> bool` is unchanged for every caller: it is `outcome is landed`. The new `write_usage_row_outcome(values) -> UsageWriteOutcome` is the same function without the collapse. `usage_log_failed` keeps its fields and its `reason` vocabulary (`initial`, `after_clearing_fks`, `concurrency_capacity`). Its `error_type` already names the class (`DataError`). The catalogue row gains one sentence saying that after this change a data-class failure is a renderer bug, not a caller input.

### D6. A planned row that is `unstorable` is dropped, once, with an event

In `write_planned_row`:

| outcome | today | after |
| --- | --- | --- |
| `landed` | acknowledge (`in_flight -= 1`) | unchanged |
| `failed`, or an `Exception` raised out of the writer | `requeue(planned)` | unchanged |
| `BaseException` (cancellation) | `requeue` then re-raise | unchanged |
| `unstorable` | `requeue` (the infinite retry of #310) | **acknowledge without requeue** (`in_flight -= 1`, no window created or incremented), then emit `usage_refusal_row_dropped` once |

`usage_refusal_row_dropped` is a WARNING with fields `tool`, `reason` (the row's marker: `rate_limited` or `slot_timeout`), `count` (the planned row's `weight` = `1 + suppressed`, the refusals that will not appear in `usage_logs`) and `user_id` (from the template, absent on an overflow-entry row). Its subject is `subject_for(user_id=…)`, the same subject `usage_log_failed` uses for the same row, so the suppressor bounds it per user. It goes through `security_events.emit` and is subject to the ordinary suppressor. All four fields are already in the formatter allow-list (`count` and `limit_count` are typed `int`), so no new field bound is needed. The emit is wrapped so that a logging failure cannot turn a dropped row into an exception on the request path.

Arithmetic after the change: while every planned row for a coalescer key is storable, Σ (1 + `suppressed`) over its rows equals the refusals observed, exactly as before. A dropped row's weight is reported by one **best-effort** emission attempt. It is not an exact, observable accounting (L3), and the spec makes no such claim (Codex spec review, finding 2: the cheap option was chosen over weight-aware attributed accounting).

A window that stays open after an immediate-path drop keeps its template. That template is re-captured at the next rollover, and the standalone flush uses it as is. If it is unstorable again, the next flush drops again: one event per coalescing interval, never one per tick, because the dropped row is not requeued with its original start. With D1 in place this is expected to be unreachable. The drop rule is a fallback in case the renderer has a bug.

### D7. What does not change

- The `argument_not_encodable` screen still refuses a lone-surrogate argument. The refused call's own row now lands. When the offending argument is one the tool logs, it is rendered (`\ud800`) and listed in `rendered_params`; an unlogged one never enters `params`.
- `usage_stats`, `search_analytics` and `concurrency_readiness` cast only server-written keys (`embed_ms`, `db_ms`, `queue_ms`, `result_count` under a regex guard, `over_quota`, `suppressed` under a guarded cast). No caller-controlled value is ever written under those keys, so no reader query changes. A server-measured timing that is itself non-finite would render as `.nan` and break the unguarded `::double precision` cast in `phase_breakdown`. Those values are monotonic-clock deltas and cannot be non-finite (L4).

## Tests (contract for the implementer)

1. Renderer unit tests: each row of the D1 table, including the backslash case (`"a\\x00"` literal versus `"a\x00"` rendered gives two different stored strings); unchanged-when-clean (identity, and no `rendered_params` key); nested dict, list and key positions; `rendered_params` sorted, and listing only the affected top-level keys; a 10,000-deep nested list (no `RecursionError`); a self-referential dict (`<cycle>`); an object whose `__str__` raises (`<unrenderable>`, and the call does not raise); first-key-wins on a `{nan: 1, ".nan": 2}` collision.
2. The reserved name: no registered tool parameter, marker constant or `timing` key is `rendered_params`.
3. `write_usage_row_outcome` classification with fake exceptions: a class-22 chain is `unstorable`; 23503 then class 22 is `unstorable`; `InterfaceError` / `OSError` is `failed`; the writer-capacity refusal is `failed`; success is `landed`. `write_usage_row` still returns a bool.
4. Coalescer: an `unstorable` immediate row and an `unstorable` flushed row are each dropped. No window is re-created, `in_flight` returns to 0, and exactly one `usage_refusal_row_dropped` emission is attempted with `count == weight`. A second tick issues **no** INSERT for that row (statement-counting test). A `failed` row still requeues with its original start, as today.
5. W7 pin (D3).
6. Integration (`tests/integration/test_issue_310_usage_params_pg.py`, real Postgres via the harness). For each of: NUL in a top-level string; NUL in a nested dict value and in a nested key; a lone surrogate (top-level and nested); `NaN`, `Infinity` and `-Infinity` nested; a non-JSON value — call `write_usage_row` with a full row and assert `True`. Read the row back and assert the stored rendering and `rendered_params`. Also: one end-to-end `_tracked` call (a fake tool registered for the test) whose argument has a NUL lands a row; a `rate_limited` refusal for an argument with a lone surrogate lands its coalesced row on the first attempt; and, with the renderer patched to the identity, the planned row is dropped after exactly one failing INSERT and is absent from the next flush. Run with `SCHEMA_TEST_CONTAINER=omcp-schema-w2a SCHEMA_TEST_PORT=55444 make test-integration`.

## Risks / Trade-offs

- **Escaping every string inside a rendered top-level value** doubles any ordinary backslashes in it, for example a Windows-style path in the same `updates` dict. That is visible and documented, and it applies only to values that carried an unstorable character. It is the price of an unambiguous grammar without escaping every row.
- **Dropping instead of requeueing** gives up the "every refusal ends up in `usage_logs`" property for unstorable rows. The alternative, retrying forever, kept neither the row nor the tick budget, and the dropped weight is still accounted for, in the event.

## Accepted limitations

- **L1.** Inside a rendered value, a non-finite float's token (`.nan`) is indistinguishable from a caller-sent string `".nan"`. The JSON type would be the only disambiguator, and a string is the only storable form. This is audit data that nothing acts on.
- **L2.** A non-string dict key that renders onto an existing string key loses to the first one, the same rule the indexer's `_jsonb_value` uses. It is reachable only through `transforms` or in-process callers, because JSON keys are strings.
- **L3.** Dropped weight is not exactly reconcilable from the log sink. `usage_refusal_row_dropped` records are subject to the per-subject suppressor, whose summary counts withheld *records*, not their `count` weights. The record also omits the principal and the scope, so two coalescer keys for one user and tool cannot be told apart. The count invariant is therefore stated only for storable rows. Owner-accepted 2026-10-10. Reachable only if the renderer itself is wrong.
- **L4.** A non-finite *server-measured* timing would render as a token and break the unguarded cast in `usage_stats.phase_breakdown`. It is not reachable, because those values are monotonic-clock deltas.
- **L5.** A failure with no SQLSTATE that is nonetheless deterministic (a serialisation `TypeError` / `ValueError`) is still `failed` and still requeued. D1 removes every known source, and widening the drop rule to unclassified errors would risk dropping rows over transient faults.

## Migration plan

None. No schema change, no setting, no backfill. Rows lost before deploy stay lost. A process restart clears any template currently cycling.
