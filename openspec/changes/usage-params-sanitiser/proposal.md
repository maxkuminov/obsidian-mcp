Refs #310

## Why

`usage_logs.params` is JSONB (`src/models/db.py`, `UsageLog.params`). The values that reach it are the caller's own tool arguments: `_tracked`'s `named_params()` binds them by name and `_truncate_params` shortens long top-level strings, but nothing checks whether the result can be stored as `jsonb`. SQLAlchemy serialises the dict with a plain `json.dumps` (no `json_serializer` is set on the engine), and PostgreSQL then rejects three kinds of value that Python's JSON layer accepts:

- **U+0000** in any string. `json.dumps` writes `"\u0000"`, and `jsonb` refuses it (`22P05`, "unsupported Unicode escape sequence").
- **A lone surrogate** (`"\ud800"`) in any string. `json.dumps` writes the escape, and `jsonb` refuses an unpaired surrogate escape (class 22).
- **A non-finite float** (`NaN`, `Infinity`, `-Infinity`). `json.dumps` writes the bare tokens, which are not JSON (class 22, `22P02`).

All three can arrive over the wire. The Streamable HTTP transport parses the body with `json.loads`, which accepts lone surrogate escapes, NUL escapes and the bare `NaN` / `Infinity` tokens. `set_frontmatter(updates={"x": NaN})`, `edit_note(find="a\u0000b")` and `read_note(path="\ud800")` all reach `_tracked` with the value intact (checked against the pinned SDK while writing this proposal).

`write_usage_row` treats any non-FK insert failure as final, so the audit row is lost and `usage_log_failed` (`reason=initial`, `error_type=DataError`) is emitted. The tool call itself is unaffected. Two consequences:

1. **An authenticated caller can keep its own calls out of `usage_logs`.** Adding a NUL to any argument of any tool drops the row. Two cases make this worse:
   - The `argument_not_encodable` refusal (#149) exists to record a call with a lone surrogate, and its own row is the one that cannot be stored, because the row quotes the argument it refused.
   - `usage_logs` is the per-credential record an operator reads after suspecting a credential (#77, #92).
2. **The rate-refusal coalescer retries an unstorable row forever.** A `rate_limited` or `slot_timeout` template captures the same params. `write_planned_row` requeues on any failure, and a flush-path requeue restores the window with its *original* start, so the same unstorable template is due again on every tick. Each tick produces one failing INSERT and one `usage_log_failed` event, until the process restarts or the principal's entry is reclaimed. The "exact Σ (1 + suppressed)" arithmetic was meant to make refusal counts trustworthy, but here it holds them in memory and never writes them.

The indexer had the same class of failure (#154 non-finite frontmatter, #308 NUL) and fixed it at its own JSONB boundary (`_jsonb_value`, the `.nan` / `.inf` tokens in `src/services/vault.py`, and the class-22 / 54000 "poison" classification in `poison_sqlstate`). The usage-log boundary never got the same treatment.

## What Changes

- **One parameter renderer at the usage-row insert boundary.** A new pure function, `render_usage_params(params) -> dict`, runs once per write at the top of `_write_usage_row_admitted`. Every `usage_logs` row the MCP side writes passes through that function: the success tail, the body-exception row, every pre-body refusal row, and every coalesced `rate_limited` / `slot_timeout` row, whether immediate or deferred. The FK-cleared retry reuses the rendered values. The renderer:
  - escapes NUL and lone surrogates visibly, with an unambiguous escape grammar;
  - replaces non-finite floats with the canonical #154 tokens `.nan` / `.inf` / `-.inf`;
  - stringifies any non-JSON value;
  - lists the top-level `params` keys whose value it changed in a new marker key, `rendered_params`.

  It is total (it never raises) and iterative (no depth limit). It changes nothing in a value that has no unstorable content.
- **A planned refusal row the database rejects as data is dropped, not requeued.** The insert path classifies its failure with the indexer's existing `poison_sqlstate`: SQLSTATE class 22, 54000, or a bare `UnicodeEncodeError`. `write_planned_row` acknowledges an *unstorable* failure instead of requeueing it, and emits one new security event, `usage_refusal_row_dropped`, which carries the `1 + suppressed` weight that will not be recorded. Every other failure is requeued exactly as today: writer-capacity refusal, connection loss, FK recovery failing for a non-data reason, or an exception. The coalescer's arithmetic becomes: Σ (1 + suppressed) over written rows + Σ `count` over `usage_refusal_row_dropped` events = refusals observed.
- `write_usage_row` keeps its `bool` contract for every existing caller. A sibling, `write_usage_row_outcome`, returns `landed | failed | unstorable` for the coalescer.
- **Docs**:
  - `docs/architecture/usage-attribution.md`: a new "What reaches `params` is rendered, not trusted" section.
  - `docs/architecture/rate-limits.md`: "A planned row is acknowledged, and a failed one is requeued" gains the unstorable exception.
  - `docs/architecture/security-event-logging.md`: a catalogue row for the new event, and the `usage_log_failed` row says what a data-class failure now means.

No new setting and no migration. `README.md` / `.env.example` are unaffected, because nothing operator-tunable is added.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `mcp-request-routing`:
  - ADDED "Logged usage parameters SHALL be rendered into a storable form before the row is inserted".
  - MODIFIED "The usage row SHALL be committed asynchronously, and its write contract SHALL be otherwise unchanged". The requeue clause now excepts an unstorable planned row, and the sibling outcome function is named.
- `mcp-rate-limits`: MODIFIED "Rate refusals are recorded through a self-contained coalescer". It adds the drop-not-requeue rule for an unstorable planned row and the arithmetic that includes dropped weight.
- `security-event-logging`: ADDED "A dropped refusal row SHALL be recorded once, with the weight it carried".

## Impact

- `src/mcp_server/tools.py`:
  - `render_usage_params` (new; or a small new module `src/services/usage_params.py` that `tools.py` imports, which is the implementer's choice, see design D2);
  - `_write_usage_row_admitted` renders first and classifies its terminal failure;
  - `write_usage_row_outcome` (new) and `write_usage_row` (unchanged signature, now a wrapper).
- `src/services/rate_limits.py`: `write_planned_row` drops on `unstorable`, emits the event and releases `in_flight`.
- `src/services/security_events.py`: `usage_refusal_row_dropped` in `EVENT_FIELDS`.
- `src/services/indexer.py`: **not modified**. `poison_sqlstate` is imported from it with a deferred import, the same way `tools.py` already imports `_note_title`.
- Tests: unit tests for the renderer and the coalescer drop, plus an integration test against real Postgres that inserts each bad value through `write_usage_row` and asserts the row lands.
- Not touched: the transfer routes (`src/transfer/routes.py` `_log_row`), the indexer, OAuth, the panel. Design D3 explains why the transfer writer needs nothing.

## Out of scope

- Transfer `usage_logs` rows (`upload_file`, `download_file`). Their params come from a `transfer_tokens.path` that PostgreSQL already stored as `text`, plus an integer, so they cannot carry any of the three values (D3).
- Bounding nested strings in `params`. `_truncate_params` truncates only top-level strings, and a nested `set_frontmatter(updates=…)` value is logged at full length. That is pre-existing and bounded by the argument caps and the request-body budget. This change neither widens nor fixes it.
- Changing what `argument_not_encodable` refuses, or adding NUL to that screen. A NUL is a valid Unicode scalar and is a vault-tool question, not a logging one.
- Sanitising any other JSONB column.
- Persisting coalescer state.
