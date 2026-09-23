## Why

**#295** — every MCP tool silently drops arguments it does not declare. FastMCP 1.29 builds each tool's argument model on `ArgModelBase`, whose `model_config` sets no `extra`, so pydantic's default `ignore` applies; the advertised `inputSchema`s carry no `additionalProperties: false`, so a client cannot catch it up front either. Observed live: `semantic_search {"query": …, "user_id": 2}` and `keyword_search {"query": …, "foo": "bar"}` return normal results.

This is not an isolation bypass (tenancy is bound to the credential; `user_id`/`vault` had no effect). It is a **silently-wrong-results** failure, which CLAUDE.md names as one of the two expensive failures for this product: an agent that writes `folders=` for `folder=`, or passes `frontmatter` to a tool that has no such filter, gets unfiltered results, believes they are filtered, and acts on them with no human in the loop. A wrong *type* on a known argument is already refused; only extra names pass.

## What Changes

- **Unknown arguments are refused on all 25 tools.** After registration, `src/mcp_server/server.py` replaces each tool's `fn_metadata.arg_model` with a subclass carrying `extra="forbid"` (other config inherited), and sets `additionalProperties: false` on each tool's published `parameters` schema. One pass over the tool manager, not per-tool code, so a tool added later is covered without opting in.
- **The refusal is the SDK's existing validation error** — the same path that refuses `folder=123` today: a tool result with `isError: true` whose text names each offending argument ("Extra inputs are not permitted"). It happens before `_tracked`, so, like a type error today, it charges no rate bucket or quota slot and writes no `usage_logs` row. It does not carry an `MCP-REFUSAL` line: that contract covers refusals raised inside `_tracked`, and argument validation has never been one.
- **`MCP_REJECT_UNKNOWN_ARGUMENTS`** (bool, default `true`), read at import, logged once at startup when `false`. `false` restores today's ignore behaviour and schemas exactly — a rollback for a client found to send extras, one `.env` line and a container recreate.
- **A test pins the mechanism**, because it reaches into SDK internals (`mcp._tool_manager`, `Tool.fn_metadata.arg_model`, `Tool.parameters`): for every registered tool, an extra argument is refused through `Tool.run`, every declared argument still validates, and the listed schema carries `additionalProperties: false`. An SDK upgrade that moves these attributes fails the suite instead of silently reverting to ignore.

No migration, no dependency, no change to any tool's declared arguments or results.

## Capabilities

### Modified Capabilities

- `mcp-request-routing`: adds a requirement that tool calls with undeclared arguments are refused and that published input schemas forbid additional properties.

## Impact

- Code: `src/mcp_server/server.py`, `src/config.py`, a new test, `docs/architecture/vault-tools.md` (short note), `.env.example`/README settings table if settings are listed there.
- Clients: a client that today sends an undeclared argument starts getting a tool error. That is the intent; the flag is the escape hatch.

## Accepted limitations

- L1: the refusal text is pydantic's, not an `MCP-REFUSAL` line, and is not recorded in `usage_logs` — same as every existing argument-type error.
- L1a: pydantic's refusal text echoes the undeclared argument's value (`input_value=…`). It is the caller's own input, so nothing leaks; it is only extra text in the error.
- L3: the rollback WARNING is emitted by the HTTP lifespan only; `src/mcp_stdio.py` (registry introspection) honours the flag but does not log it (Codex round 1, declined).
- L2: the mechanism depends on FastMCP 1.29 private attributes; guarded by the pinning test, not by a public API.
