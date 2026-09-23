## 1. Implementation

- [x] 1.1 Add `mcp_reject_unknown_arguments: bool = True` (`MCP_REJECT_UNKNOWN_ARGUMENTS`) to `src/config.py`; document it wherever sibling `MCP_*` settings are documented (`.env.example`, README/DEPLOYMENT settings tables)
- [x] 1.2 In `src/mcp_server/server.py`, after the last `@mcp.tool()`, apply one function over `mcp._tool_manager.list_tools()` that swaps `fn_metadata.arg_model` for an `extra="forbid"` subclass and sets `parameters["additionalProperties"] = False`, only when the setting is true
- [x] 1.3 Log once at startup when the setting is false (warning), following how other rollback switches (`PANEL_CSP`) are logged
- [x] 1.4 Add a short "Unknown arguments are refused" paragraph to `docs/architecture/vault-tools.md` (the why: #295, silent unfiltered results; the SDK-internals dependency and its pinning test)

## 2. Tests

- [x] 2.1 For every registered tool: an extra argument raises a tool error naming it via `Tool.run`, before the tool function is invoked (patch the fn or assert via a sentinel)
- [x] 2.2 For every registered tool: the listed schema (`await mcp.list_tools()`) has `additionalProperties: false`
- [x] 2.3 Declared arguments (all optional ones as `null` where allowed) still validate on the swapped models
- [x] 2.4 With the setting false, the models and schemas are left untouched
- [ ] 2.5 Existing suite green (`pytest tests`), plus `make test-integration` — offline suite green (5678 passed, 688 skipped, with `OMCP_ALLOW_SKIP_TRANSFER_INTEGRATION=1`); `make test-integration` not yet run (skipped during the DB move)

## 3. Verify and ship

- [ ] 3.1 `openspec validate reject-unknown-tool-arguments --strict`
- [ ] 3.2 openspec-verifier pass; one adversarial Codex round (tool-argument surface for every write tool)
- [ ] 3.3 Deploy; live end-to-end: `keyword_search` with `foo` refused, `keyword_search`/`semantic_search`/`read_note` with valid args succeed, `tools/list` shows `additionalProperties: false`
- [ ] 3.4 Archive, commit, push (`Closes #295`)
