## Why

Issue #263: refusals returned inside tool bodies look like successful calls in
usage history. Existing write-precondition sentinels identify some caller-facing
failures but carry no usage marker. An agent and an operator need the same
terminal outcome described honestly, especially when a write only partly lands.

## What Changes

- Give every existing in-body refusal a pure typed result containing its closed
  code, usage marker and refused/partial disposition. Keep existing prose and
  append the existing MCP-REFUSAL sentinel. Successful results stay unchanged.
- Classify only the terminal typed value in `_tracked`, never response prose or
  a sentinel found in note content. Structured reads retain private metadata
  outside their wire schema and reserve space for the complete sentinel.
- Persist a bounded post-body marker and disposition, and emit one bounded
  generic body-outcome event. Preserve existing specific authorization events.
- Cover all 25 tools, shared helpers and the partial move/import paths. The
  attached inventory names every current terminal branch and success exclusion.

## Capabilities

### Modified Capabilities
- `mcp-request-routing`: explicit typed body refusals, success exclusions and
  truthful partial outcomes, without changing admission or publication order.
- `security-event-logging`: bounded, redacted terminal body-outcome records.
- `panel-performance-views`: body failures remain in executed-work latency
  statistics; they do not become pre-body admission refusals.

## Impact

`src/services/refusals.py`, a new `src/services/tool_outcomes.py`,
`src/mcp_server/tools.py`, `src/mcp_server/read_result.py`, the security event
catalogue, tests, and relevant architecture notes. No migration, new dependency,
new permission, changed quota semantics or filesystem algorithm.

Existing issue #263 coordinates this work. The sibling #261 uses the same wrapper;
its shadow observations must not replace these actual outcome markers.
