## 1. Pre-flight and the oracle (before any code change)

- [ ] 1.1 Base check: `git log --oneline -1` on the branch base shows `c491326` (or later `main`); `tests/fixtures/tools_list_schema_additions.json` lists `list_tasks`, `get_changes`, `hybrid_search`; `grep -c '^@mcp.tool' src/mcp_server/server.py` is 28
- [ ] 1.2 Re-read design D1–D6, D12 and `docs/architecture/vault-tools.md` "Tool metadata"; do not touch `src/oauth/` registration or consent (#357 is in flight there)
- [ ] 1.3 **Oracle first** (design D5): with the base tree unchanged, capture the complete `tools/list` `result` over HTTP (`src.main:app`, existing ASGI harness) for a `readwrite` API-key principal; write it canonically (UTF-8, `sort_keys=True`, `separators=(",", ":")`) to `tests/fixtures/tools_list_full_v1.json`; put the generating snippet in the new test module's docstring; commit this fixture alone as the branch's first commit, before any `src/` change

## 2. The single write-required registry (design D2)

- [ ] 2.1 New `src/mcp_server/tool_permissions.py`, importing nothing from `mcp` or `src.mcp_server.tools`: `WRITE_REQUIRED_TOOLS = frozenset({...nine names...})` with a docstring naming its two consumers
- [ ] 2.2 `_tracked`: compute `requires_write = tool_name in WRITE_REQUIRED_TOOLS` at decoration; at the body boundary (after argument caps, vault root, rate buckets, concurrency admission and quota — exactly where the body is invoked) call `_require_write()` for a member and return its refusal without running the body, classified as the body's own refusal is today (same `BodyOutcome`, usage marker, security event); no new decorator argument, no stamp
- [ ] 2.3 Leave the seven in-body `_require_write()` calls and `_mint_preflight(need_write=True)` untouched (defence in depth)
- [ ] 2.4 Confirm `write_class` is unchanged everywhere; it remains the rate/concurrency class only

## 3. The visibility rule and the seam (design D1, D4, D12)

- [ ] 3.1 New `src/mcp_server/tool_visibility.py`, importing nothing from `mcp` directly or transitively: `is_filtered(principal, permission)` (kind `"oauth"` and permission ≠ `"readwrite"`), `hidden_names()` (returns `tool_permissions.WRITE_REQUIRED_TOOLS`), `visible(tools, *, principal, permission)` (order-preserving filter by `.name`, returns the same objects)
- [ ] 3.2 In `server.py`, `class _ScopedFastMCP(FastMCP)` overriding `async def list_tools(self)` → `visible(await super().list_tools(), principal=current_principal.get(), permission=current_permission.get())`; construct `mcp` from it with the arguments unchanged; the comment names the 1.30 dispatch (`_setup_handlers`), the 2.x one (`MCPServer._handle_list_tools`), the `cacheScope: "private"` obligation and "paginate the filtered list" for #365
- [ ] 3.3 Do not read or write `mcp._mcp_server._tool_cache`; do not change `listChanged`; do not filter `instructions` or descriptions; do not add pagination; no new setting
- [ ] 3.4 Grep: `visible` has exactly one production caller (the override); `WRITE_REQUIRED_TOOLS` exactly two (`_tracked`, `hidden_names`)

## 4. Fixtures and tests (`tests/test_issue_358_scoped_tools_list.py`)

- [ ] 4.1 Fixture `tests/fixtures/tools_list_readonly_oauth.json`: the 19 names in order (design D5); assert each filtered entry is byte-identical (canonical serialisation) to the oracle entry of the same name
- [ ] 4.2 Unit (pure): `is_filtered` truth table over `None`, `("api_key", 1)` × {read, readwrite}, `("oauth", "g")` × {read, readwrite}; `visible` preserves order and identity
- [ ] 4.3 Per principal kind, via contextvars as the middleware sets them: read-only OAuth → 19; readwrite OAuth, readwrite key, read key, no principal → 28
- [ ] 4.4 Over HTTP: the canonical `result` for a `readwrite` key, a `read` key and a `readwrite` grant is byte-identical to `tools_list_full_v1.json`; a read-only grant's equals the oracle minus the nine; no credential → 401 and the override never ran; `MCP_SANDBOX_MODE` → 28
- [ ] 4.5 Pagination: one `tools/list` with `params.cursor` set, per principal (read-only grant, API key) — same result as without, no `nextCursor`
- [ ] 4.6 **Concurrency** (design D6): five requests released together through an `anyio` barrier into the real ASGI app — read-only OAuth list, API-key list, readwrite OAuth list, read-only `tools/call` of a hidden tool, readwrite `tools/call` of an output-schema-carrying hidden tool (`edit_note`, `dry_run=true`); each asserted against its own credential (19 / oracle / oracle / `permission_denied` / ran and output-validated); ≥ 50 rounds with the cache last filled by a full list, ≥ 50 with it last filled by a filtered list, and one more series with a test-only `await asyncio.sleep(0)` injected inside the override
- [ ] 4.7 Sequential determinism: 10 lists under one read-only grant identical; a second read-only grant gets the same list; interleaving a readwrite list changes neither
- [ ] 4.8 Scope change: a grant's live tokens switched `read` → `readwrite` (the panel path) list 28 on the next request; no `notifications/tools/list_changed` is ever emitted; `initialize` advertises `listChanged: false`
- [ ] 4.9 Enforcement unchanged: a read-only grant calls `edit_note`, `request_upload`, `import_from_url` and `delete_note` by name — refused `permission_denied`, same text, same `tool_write_refused` event, usage row/marker, token and quota spend as before (compare with `tests/test_issue_192_write_refusal_marker.py`), returned as a tool result; once with the cache warm from a full list and once right after a filtered list; then a `readwrite` `edit_note` still gets output-schema validation
- [ ] 4.10 Precedence (L5): a read-only grant sending `write_file` / `delete_file` a malformed `expected_hash` gets `permission_denied`; a `readwrite` caller still gets the syntax refusal
- [ ] 4.11 Registry guards (design D2): all 28 tools called under a read-only principal → the `permission_denied` set equals `WRITE_REQUIRED_TOOLS`; monkeypatching a member out stops the body-boundary gate (the in-body check still refuses) and a read tool in makes the gate refuse and the list hide it; AST: every `_require_write()` / `_mint_preflight(…, need_write=True)` call site lies in a registry member's implementation; structural: `== W ∪ {request_upload} == destructive ∪ {create_note}`, `request_download`/`check_upload` ∉, ⊆ registered, 9 members
- [ ] 4.12 Instructions: a read-only grant's and an API key's `initialize` carry the same `instructions`, equal to `SERVER_INSTRUCTIONS`
- [ ] 4.13 SDK-free on invocation: a clean subprocess imports `tool_visibility`, calls `hidden_names()` and `visible(…)` on stand-in objects, then asserts no `mcp` / `mcp.*` in `sys.modules`
- [ ] 4.14 `tests/test_issue_343_tool_metadata.py`: the fixture test additionally runs with an API-key principal set; nothing else changes; `tests/test_issue_295_unknown_arguments.py`, `tests/test_issue_66_*`, `tests/test_issue_192_*` stay green unmodified

## 5. Docs (same PR)

- [ ] 5.1 `docs/architecture/vault-tools.md`, "Tool metadata": new subsection "Who sees which tools (#358)" — D1 table, the single registry and its two consumers, the body-boundary gate and the L5 precedence change, the seam and why it survives 2.x, the 1.30 tool-cache interaction, the oracle, L1–L6
- [ ] 5.2 `docs/architecture/oauth-and-grants.md`: what a `read` grant lists (19) and that a panel scope change is seen on the client's next list (reconnect)
- [ ] 5.3 `docs/architecture/rate-limits.md`: the gate order in `_tracked` gains the body-boundary permission gate (after quota; no token or slot spent differently)
- [ ] 5.4 `CLAUDE.md`: "MCP tools" paragraph (a read-only OAuth grant lists 19; `WRITE_REQUIRED_TOOLS` drives the gate and the list) and one key-decisions bullet
- [ ] 5.5 `README.md` OAuth section: a read-only connection lists 19 tools; choose read + write at consent to get the write tools; reconnect after changing a grant's scope. Grep-check: no setting to add to `.env.example` or the settings tables

## 6. Gates

- [ ] 6.1 `pytest tests` and `make test-integration`; `openspec validate scoped-tools-list --strict`
- [ ] 6.2 `openspec-verifier` subagent (not the implementer) against these deltas; iterate to zero blocking gaps
- [ ] 6.3 Adversarial Codex (mandatory: auth/permission surface), `-m gpt-daybreak-blue-latest`, read-only sandbox, on the committed tree. Framing: the consumer is an agent; "wrong" means a list that hides a tool the credential can use, a list that varies by anything but the presented credential (prior or concurrent requests, cache, timing), a filtered list reaching another principal, any change in call-time refusal beyond L5, or a body-boundary gate that spends tokens/quota differently. Two rounds by default; triage per the workflow; record declined findings as accepted limitations
- [ ] 6.4 Archive (`openspec archive scoped-tools-list -y`) and docs as the last commits of the feature branch; PR `Closes #358`

## 7. Live end-to-end (after deploy)

- [ ] 7.1 A read-only OAuth connector in Claude: `tools/list` shows 19 tools; calling `edit_note` by name is refused `permission_denied`; `read_note`, `keyword_search`, `request_download` work
- [ ] 7.2 A `readwrite` OAuth connector and an API key: 28 tools; one guarded `edit_note` dry run succeeds
- [ ] 7.3 Report which tools were actually called on the live server
