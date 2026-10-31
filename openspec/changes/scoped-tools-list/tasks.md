## 1. Pre-flight

- [ ] 1.1 Base check: `git log --oneline -1` on the branch base shows `c491326` (or later `main`); `tests/fixtures/tools_list_schema_additions.json` lists `list_tasks`, `get_changes`, `hybrid_search`; `grep -c '^@mcp.tool' src/mcp_server/server.py` is 28
- [ ] 1.2 Re-read design D1–D6 and `docs/architecture/vault-tools.md` "Tool metadata"; do not touch `src/oauth/` registration or consent (#357 is in flight there)

## 2. The derived hidden set (design D2)

- [ ] 2.1 `_tracked(..., requires_write: bool | None = None)`; stamp `wrapper.__requires_write__ = write_class if requires_write is None else requires_write` next to `__write_class__`
- [ ] 2.2 Decorate `request_upload_impl` with `requires_write=True`; no other decorator changes
- [ ] 2.3 `production_write_required_tools()` in `tools.py`, enumerating exactly like `production_write_bucket_tools()` (module attribute, `__module__ == __name__`); docstring cites #358 and the #343 probe-pollution lesson

## 3. The visibility rule and the seam (design D1, D4, D5)

- [ ] 3.1 New `src/mcp_server/tool_visibility.py`, importing nothing from `mcp`: `is_filtered(principal, permission)` (kind `"oauth"` and permission ≠ `"readwrite"`), `hidden_names()` (from 2.3, computed once and cached), `visible(tools, *, principal, permission)` (order-preserving filter by `.name`, returns the same objects)
- [ ] 3.2 In `server.py`, `class _ScopedFastMCP(FastMCP)` overriding `async def list_tools(self)` → `visible(await super().list_tools(), principal=current_principal.get(), permission=current_permission.get())`; construct `mcp` from it with the arguments unchanged; comment names the 1.30 dispatch (`_setup_handlers`) and the 2.x one (`MCPServer._handle_list_tools`) and the `cacheScope: "private"` obligation for #365
- [ ] 3.3 Do not read or write `mcp._mcp_server._tool_cache`; do not change `listChanged`; do not filter `instructions` or descriptions; no new setting
- [ ] 3.4 Grep: `tool_visibility` is used only by the override (no admission path imports it)

## 4. Fixtures and tests (`tests/test_issue_358_scoped_tools_list.py`)

- [ ] 4.1 New fixture `tests/fixtures/tools_list_readonly_oauth.json`: the 19 names in order (design D5), generated from the live filtered list and checked by hand against the full list minus the nine
- [ ] 4.2 Unit (pure): `is_filtered` truth table over `None`, `("api_key", 1)` × {read, readwrite}, `("oauth", "g")` × {read, readwrite}; `visible` preserves order and identity
- [ ] 4.3 Per principal kind, via contextvars as the middleware sets them: read-only OAuth → the 19-name fixture, each entry equal as JSON to its full-list entry; readwrite OAuth, readwrite key, read key, no principal → 28, equal to the #343 fixtures
- [ ] 4.4 Over HTTP (`src.main:app`, existing ASGI harness): `tools/list` bodies for a `readwrite` key, a `read` key and a `readwrite` grant are identical to each other and to the pre-change body; a read-only grant's body equals that body minus the nine entries; no credential → 401 and the override never ran; `MCP_SANDBOX_MODE` → 28
- [ ] 4.5 Determinism: 10 lists under one read-only grant are identical; a second read-only grant gets the same list; interleaving a readwrite list changes neither
- [ ] 4.6 Scope change: a grant's live tokens switched `read` → `readwrite` (the panel path) list 28 on the next request; no `notifications/tools/list_changed` is ever emitted; `initialize` advertises `listChanged: false`
- [ ] 4.7 Enforcement unchanged: a read-only grant calls `edit_note`, `request_upload`, `import_from_url` and `delete_note` by name — refused `permission_denied`, same text, same `tool_write_refused` event and usage marker as before (compare with `tests/test_issue_192_write_refusal_marker.py`), returned as a tool result, not an unknown-tool error; once with the SDK cache warm from a full list and once right after a filtered list; then a `readwrite` `edit_note` still gets output-schema validation
- [ ] 4.8 Reconciliation: every registered tool called under a read-only OAuth principal with minimal valid arguments; the `permission_denied` set equals `production_write_required_tools()`; plus `H == W ∪ {request_upload} == destructive ∪ {create_note}`, `request_download`/`check_upload` ∉ `H`, `len(H) == 9`
- [ ] 4.9 Probe pollution: probes decorated with `write_class=True` and `requires_write=True` in the test module leave `production_write_required_tools()` unchanged
- [ ] 4.10 Instructions: a read-only grant's and an API key's `initialize` carry the same `instructions`, equal to `SERVER_INSTRUCTIONS`
- [ ] 4.11 SDK-free: importing `tool_visibility` pulls in no `mcp.*` module (check `sys.modules` in a subprocess)
- [ ] 4.12 `tests/test_issue_343_tool_metadata.py`: the fixture test additionally runs with an API-key principal set; nothing else changes; `tests/test_issue_295_unknown_arguments.py`, `tests/test_issue_66_*` stay green unmodified

## 5. Docs (same PR)

- [ ] 5.1 `docs/architecture/vault-tools.md`, "Tool metadata": new subsection "Who sees which tools (#358)" — D1 table, the derived set, the seam and why it survives 2.x, the 1.30 tool-cache interaction, L1–L4
- [ ] 5.2 `docs/architecture/oauth-and-grants.md`: what a `read` grant lists (19) and that a panel scope change is seen on the client's next list (reconnect)
- [ ] 5.3 `CLAUDE.md`: "MCP tools" paragraph (a read-only OAuth grant lists 19; enforcement is `_require_write`, unchanged) and one key-decisions bullet
- [ ] 5.4 `README.md` OAuth section: a read-only connection lists 19 tools; choose read + write at consent to get the write tools; reconnect after changing a grant's scope. Grep-check: no setting to add to `.env.example` or the settings tables

## 6. Gates

- [ ] 6.1 `pytest tests` and `make test-integration`; `openspec validate scoped-tools-list --strict`
- [ ] 6.2 `openspec-verifier` subagent (not the implementer) against these deltas; iterate to zero blocking gaps
- [ ] 6.3 Adversarial Codex (mandatory: auth/permission surface), `-m gpt-daybreak-blue-latest`, read-only sandbox, on the committed tree. Framing: the consumer is an agent; "wrong" means a list that hides a tool the credential can use, a list that varies by anything but the presented credential (prior requests, cache, timing), a filtered list leaking to another principal, or any change in call-time refusal. Two rounds by default; triage per the workflow; record declined findings as accepted limitations
- [ ] 6.4 Archive (`openspec archive scoped-tools-list -y`) and docs as the last commits of the feature branch; PR `Closes #358`

## 7. Live end-to-end (after deploy)

- [ ] 7.1 A read-only OAuth connector in Claude: `tools/list` shows 19 tools; calling `edit_note` by name is refused `permission_denied`; `read_note`, `keyword_search`, `request_download` work
- [ ] 7.2 A `readwrite` OAuth connector and an API key: 28 tools; one guarded `edit_note` dry run succeeds
- [ ] 7.3 Report which tools were actually called on the live server
