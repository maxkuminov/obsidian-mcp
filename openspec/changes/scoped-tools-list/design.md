# Design: scoped `tools/list` (#358)

## Context

- **Where the principal lives.** `APIKeyMiddleware._authenticate` (`src/mcp_server/auth.py`) sets, per request, `current_permission` (`"readwrite"` or `"read"`; for OAuth `token_has_write(oauth_token.scope)`, `auth.py:1171`, the same helper the panel badge uses), and `current_principal` (`src/auth/session.py:120`): `("api_key", id)` or `("oauth", grant_id)` — the grant, so a refresh does not change it. Every value is reset in the middleware's `finally`. The server runs `stateless_http=True`, so each POST builds its own transport inside the request task and the SDK handlers inherit these contextvars; that is already how `_tracked` and `_require_write` read them.
- **Unauthenticated `/mcp`** never reaches the SDK: the middleware answers 401 first. Only `MCP_SANDBOX_MODE` bypasses auth, and then `current_principal` is `None`.
- **How `tools/list` is served on SDK 1.30.** `FastMCP._setup_handlers` (`mcp/server/fastmcp/server.py:320`) registers `self.list_tools` — a public, overridable coroutine returning `list[MCPTool]` built from `_tool_manager.list_tools()` in registration order — with the low-level `Server.list_tools()` decorator. That wrapper (`mcp/server/lowlevel/server.py:440–470`), on a plain list result, **clears and refills the process-wide `_tool_cache`**. `call_tool` consults that cache (`_get_cached_tool_definition`, line 482): on a miss it re-runs the `tools/list` handler with `None`, *in the calling request's context*, and if the tool is still absent logs `Tool '…' not listed, no validation will be performed` and runs the call without output-schema validation. Input validation is already off (`call_tool(validate_input=False)`).
- **How it is served on SDK 2.x** (inspected on the `mcp==2.3.0` wheel): `MCPServer.__init__` registers `on_list_tools=self._handle_list_tools`, which returns `ListToolsResult(tools=await self.list_tools())` (`mcp/server/mcpserver/server.py:427–430`); `list_tools()` is the same public method (`:516`). Cacheable results get `ttlMs`/`cacheScope` from a `CacheHint` whose default scope is `"private"` (`mcp/server/caching.py`).
- **Enforcement today.** Seven tool bodies call `_require_write()` and two call `_mint_preflight(need_write=True)` (`request_upload`, `import_from_url`), whose first act is `_require_write()`. `request_download` passes `need_write=False`. `check_upload` never refuses for permission.

## Goals / Non-goals

Goals: a read-only OAuth grant lists only the tools it can use; everyone else's list is byte-identical to today's; the hidden set cannot drift from enforcement; the seam survives the SDK 2.x rename.

Non-goals: changing enforcement; filtering read-only API keys; per-tool scopes; filtering descriptions or instructions; `list_changed`; any change to consent or registration (#357 owns OAuth client registration).

## Decisions

### D1. Who is filtered

The list is filtered **iff** `current_principal` is a tuple whose kind is `"oauth"` **and** `current_permission` is not `"readwrite"`. Reading `current_permission` — not re-parsing the token scope — means the list and `_require_write` read one value and cannot disagree. Everything else gets the full list:

| Request | List |
| --- | --- |
| OAuth grant, scope lacks `readwrite` | 19 (filtered) |
| OAuth grant with `readwrite` | 28 |
| API key, `readwrite` or `read` | 28 (D3) |
| `MCP_SANDBOX_MODE` (no principal) | 28 — registry introspection must see every tool |
| stdio entry point (`src/mcp_stdio.py`), in-process `await mcp.list_tools()` (tests), no principal | 28 |
| No credential on `/mcp` | never reaches the handler — 401 from the middleware |

"No principal → full list" is the fail direction of today's behaviour, not a hole: the filter is not a control, and every path that has no principal is either unauthenticated-by-design (sandbox, stdio) or in-process.

### D2. What is hidden: derived from the code that refuses

Hidden set `H` = the production tools that refuse **every** call from a read-only credential, before any vault effect: the write bucket `W` (`_tracked(write_class=True)`, eight tools) ∪ `{request_upload}`. Nine tools: `create_note`, `edit_note`, `move_note`, `delete_note`, `set_frontmatter`, `write_file`, `delete_file`, `import_from_url`, `request_upload`.

It is **not** a list in `tool_visibility.py`. `_tracked` gains `requires_write: bool | None = None`; the wrapper is stamped `__requires_write__ = write_class if requires_write is None else requires_write`, and `request_upload_impl` is decorated `requires_write=True` (it is not in the write bucket: its write charge is taken at `PUT /transfer/upload` redemption, `rate-limits.md`). `production_write_required_tools()` enumerates the stamp exactly as `production_write_bucket_tools()` does (module attributes of `src.mcp_server.tools` whose `__module__` is that module), so test probes decorated in test modules cannot pollute it — the #343 lesson (Codex spec round 1). The filter maps registered tool names against that set; `tests/test_issue_66_*` already proves every registered tool delegates to a `_tracked` wrapper of the same name.

Drift guards (all in `tests/test_issue_358_scoped_tools_list.py`):

1. **Behavioural reconciliation**: every one of the 28 registered tools is called under a read-only OAuth principal (the harness of `tests/test_issue_192_write_refusal_marker.py`, with minimal valid arguments); the set refused with `permission_denied` SHALL equal `production_write_required_tools()`. A new write tool that forgets the stamp, or a stamp on a tool that does not refuse, fails here.
2. **Structural**: `H == W ∪ {request_upload}` and `H == destructive ∪ {create_note}` (the #343 destructive set), and `request_download`, `check_upload` ∉ `H`.
3. `H ⊆` registered names, and `len(H) == 9`.

`check_upload` stays visible: it never refuses a read-only caller (a read grant has minted nothing, so it answers `not_found`, which is true). `request_download` stays visible: it is a read (it mints a download link, `need_write=False`).

### D3. API keys keep the full list (owner decision, default)

Per the issue: an API key's permission is set by its owner in the panel and shown there; a read-only key is a deliberate operator artefact, and its full list keeps the #343 fixture test, every registry and every scripted client byte-identical. Recorded as L1; extending the rule to read-only keys later is a one-line change to D1 and a second contract change (question for Max).

### D4. The seam: override the public `list_tools()`, keep the rule SDK-free

```python
# src/mcp_server/tool_visibility.py  (no `mcp` import)
def is_filtered(principal, permission) -> bool: ...
def hidden_names() -> frozenset[str]:  # production_write_required_tools()
def visible(tools: Sequence[T], *, principal, permission) -> list[T]:  # order-preserving, by `.name`

# src/mcp_server/server.py
class _ScopedFastMCP(FastMCP):
    async def list_tools(self):
        tools = await super().list_tools()
        return visible(tools, principal=current_principal.get(), permission=current_permission.get())

mcp = _ScopedFastMCP("obsidian-vault", instructions=…, …)  # arguments unchanged
```

Why this seam:

- It overrides a **public** method the SDK itself dispatches to — `self.list_tools` in 1.30's `_setup_handlers`, `await self.list_tools()` in 2.x's `_handle_list_tools`. The 2.x migration (#365) changes the base class name (`FastMCP` → `mcp.server.mcpserver.MCPServer`) and nothing else here. Re-registering the low-level handler (`mcp._mcp_server.list_tools()(…)`) was rejected: it reaches a private attribute that 2.x replaces with constructor-passed `on_*` handlers.
- The return type stays `list[MCPTool]`, so every test that calls `await mcp.list_tools()` is unaffected — they run with no principal and get the full list.
- The rule is an ordinary function of `(tools, principal, permission)`, testable without the SDK and reusable when 2.x arrives.
- **2.x obligation, recorded for #365**: on the 2026-07-28 path a `tools/list` result carries `cacheScope`. A list that varies by authorization MUST be `"private"` (caching utility: "`private` is appropriate … for filtered list results that vary per user"; a `"public"` list "may be shared outside of the initial request's authorization context"). 2.x's default `CacheHint` is `"private"`; the migration SHALL NOT configure `cache_hints` for `tools/list` as `"public"`, and SHALL carry a test that a filtered list is `"private"`. Under 1.30 there is no `cacheScope` field; HTTP responses to the `/mcp` POSTs are not cacheable by shared caches.

### D5. Determinism and the fixtures

The filtered list is the full list, in registration order, minus `H`. Each kept `MCPTool` is the very object the full list built (same `name`, `title`, `description`, `inputSchema`, `outputSchema`, `annotations`), so its serialised entry is identical. Tests:

- `tests/fixtures/tools_list_readonly_oauth.json`: the 19 names in order — `keyword_search`, `read_note`, `list_notes`, `get_tags`, `get_recent`, `semantic_search`, `get_vault_guide`, `get_backlinks`, `get_links`, `get_neighborhood`, `find_related`, `find_orphans`, `read_file`, `list_files`, `check_upload`, `request_download`, `list_tasks`, `get_changes`, `hybrid_search`. Names only: the schemas are already pinned by the #343 fixtures, and each filtered entry is asserted equal to its full-list entry.
- The #343 fixture test runs **under an API-key principal** (contextvars set as the middleware sets them) in addition to the no-principal call it makes today, so "full list, byte-identical" is proven for the principal it is promised to.
- Determinism: N repeated lists under one read-only grant are identical; two different read-only grants get the identical list; a read-only list interleaved with a readwrite list in the same process does not change either (the list does not depend on prior requests — the spec's "MUST NOT vary … as a side effect of other requests").
- Over HTTP (`httpx.ASGITransport` against `src.main:app`, existing harness): the `tools/list` result body for an API key equals the body before this change; for a read-only grant it equals that body with the nine entries removed.

### D6. Enforcement unchanged, including the SDK tool cache

The filter changes what is *listed*, never what is *callable*. A read-only grant's `tools/call edit_note` reaches `FastMCP.call_tool` → `_tool_manager` → `_tracked` → `_require_write` and is refused `permission_denied`, with the `tool_write_refused` event and the usage marker, exactly as today.

The one SDK interaction: on 1.30 a filtered list clears and refills `_tool_cache` with 19 entries. Consequences, all checked:

- A `readwrite` or API-key `tools/call` of a hidden tool after that misses the cache, refreshes **under its own authorization**, gets the full list, and validates as today. Correctness never depends on cache state.
- A read-only call of a hidden tool misses (or hits, if a full list was the last refresh); on a miss the SDK logs one WARNING and skips output validation. The refusal is a valid `{"result": str}` structure that validation would have passed, so the response is identical either way (L3).
- No await separates the refresh from the lookup (`list_tools` is synchronous in effect), so one request cannot observe another's refill mid-lookup.

The implementation SHALL NOT touch `_tool_cache` (an internal). Tests pin the refusal response for a read-only grant with the cache (a) warm from a full list and (b) just cleared by a read-only list, both equal to the response before this change, and a readwrite call after (b) still output-validated.

### D7. `instructions` are not filtered

`SERVER_INSTRUCTIONS` is one constant (#343); its ≤ 512-character lead names `create_note` / `edit_note` and the hash guard, and its rest names the file and transfer tools. A read-only grant receives the same text. Reasons: the lead is a pinned contract (one constant, one test set); some clients drop instructions anyway, so nothing may depend on them; a per-authorization `initialize` result adds a second varying surface for little gain. Recommendation: **no change**, no extra sentence; recorded as L2. Tool descriptions that cross-reference write tools (e.g. `read_note` naming `edit_note`) are likewise not rewritten.

### D8. No `list_changed`

The list depends on the authorization presented, not on time or server state. `listChanged` stays `false` in the capabilities and no notification is sent. A grant whose scope is changed in the panel (`oauth-authorization-integrity`, "A scope change applies to every live token in the grant family") sees the new list on its next `tools/list`; a client that cached its list keeps it until it re-lists — after a downgrade the extra tools are refused at call time, after an upgrade the write tools appear only after a reconnect (L4, README note).

### D9. Usage logging

`tools/list` has never written a `usage_logs` row (`_log_usage` runs only inside `_tracked`), and this change adds none: a row per list would be a database write on a call that does no work, in a table whose rows are tool calls. Nothing records whether a list was filtered; it is a pure function of the credential, which the next `tools/call` row records.

### D10. No setting

No `MCP_SCOPED_TOOLS_LIST`. The behaviour follows the spec, the full list is still served to every principal that can use it, and enforcement never depended on the list, so there is no failure a switch would contain. Rollback is a revert. Grep-check: nothing for `.env.example` or the README settings table.

### D11. The one contract change, and why before 1.0

For read-only OAuth grants the listed set shrinks 28 → 19. Every removed tool could only ever return `permission_denied` to that grant, so no working call disappears; what disappears is a dead end the agent was invited into. 1.0 promises the tool contract; doing this after 1.0 would make it a breaking change to the promise, doing it now makes the read-only list part of the promise. API keys and `readwrite` grants: no change at all.

## Risks

- **SDK 1.30 cache churn** (D6): bounded by the general rate bucket; no correctness effect; one WARNING per read-only call of a hidden tool on a cache miss — those calls already emit a `tool_write_refused` event.
- **SDK 2.x** (#365): the seam is the public `list_tools()`, present in 2.x; the base-class rename and the `cacheScope: "private"` requirement are recorded for that change. If 2.x's `_tool_input_schema` (called before middleware, "also finds a tool that middleware hides") matters, it only affects `Mcp-Param-*` header validation, which this server does not use.
- **A client that treats an unlisted tool as unknown** and refuses to call it client-side: that is the intended effect for a read-only grant.
- **Drift**: a new write tool without the stamp — caught by guard 1 in D2.
- **Wrong principal kind**: an OAuth request whose `current_principal` were left unset would get the full list (today's behaviour), never a wrong filtered one; the middleware sets both together (`auth.py:1178–1189`).

## Accepted limitations

- **L1** Read-only API keys still list all 28 tools (D3).
- **L2** A read-only grant's `instructions` and some read-tool descriptions name tools it does not list (D7).
- **L3** On SDK 1.30 a read-only call of a hidden tool after a filtered list logs the SDK's "not listed, no validation" WARNING and skips output validation of a refusal that would pass it (D6).
- **L4** No `list_changed`: after a panel scope change, a client sees the new list only when it re-lists (D8).

## Open questions (Max)

1. Should read-only **API keys** also get the filtered list (L1)? Default: no, per the issue.
2. Should the consent page's read-only option say that write tools will not be offered to the agent? That is a template change (browser pass) and touches the consent surface the `oauth-cimd` drafter is near; default: not in this change.
3. The brief's live check said a read-only grant sees 17 tools; the arithmetic is 28 − 9 = **19** (`check_upload` and `request_download` stay listed). Confirm 19 is intended, or name the two further tools to hide.

## Verification plan

Offline + integration suites; `openspec-verifier`; one mandatory adversarial Codex pass (auth/permission surface) framed on: a filter that hides a tool a grant can use, a filter that varies by anything other than the presented credential, a list that leaks across requests, and any change to call-time refusal. Live: a read-only Claude connector lists 19 and is refused on a named `edit_note`; a `readwrite` connector and an API key list 28.
