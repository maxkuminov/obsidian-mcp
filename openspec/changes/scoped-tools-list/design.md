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

### D2. What is hidden: one registry that enforcement and the filter both read (revised, Codex round 1)

Hidden set = `WRITE_REQUIRED_TOOLS`, **one `frozenset` literal** in a new SDK-free module `src/mcp_server/tool_permissions.py`: `create_note`, `edit_note`, `move_note`, `delete_note`, `set_frontmatter`, `write_file`, `delete_file`, `import_from_url`, `request_upload`. Nine tools.

It is the single source for **both** consumers:

1. **Enforcement.** `_tracked` looks up its own `tool_name` in the registry on every call (a frozenset membership test), so the registry is the live source. For a member, at the **body boundary** — after every existing gate (argument caps, vault root, rate buckets, concurrency admission, quota), exactly where the body would start — it calls `_require_write()` and, on refusal, returns that refusal without running the body, classified exactly as the body's own refusal is today (a body refusal, executed work, the same `BodyOutcome`, usage marker and `tool_write_refused` event; `_require_write` already records both at its single definition). Rate tokens and the quota slot are therefore spent exactly as today, because today the body ran and refused as its first act.
2. **Discovery.** `tool_permissions.hidden_names()` returns the same object.

No decorator argument and no wrapper stamp defines membership (the round-0 `requires_write` stamp is dropped: Codex showed it was metadata that enforcement never consumed). `write_class` stays what it is — the rate-bucket and concurrency class — and a test pins `W == WRITE_REQUIRED_TOOLS − {request_upload}`; `request_upload` is outside the bucket because its write charge is taken at `PUT /transfer/upload` redemption (`rate-limits.md`).

The seven in-body `_require_write()` calls and `_mint_preflight(need_write=True)` **stay** as defence in depth (removing them is a separate, riskier edit to nine write bodies). For a read-only caller they are now unreachable for registry members; an AST guard requires every such call site to sit in a registry member, so a new write body that calls `_require_write` without being registered fails the suite.

**The one enforcement-visible difference.** Seven bodies call `_require_write()` as their first statement and two call `_mint_preflight` first, whose first act is the same check — so moving the check to the body boundary changes nothing for them. `write_file` and `delete_file` run `_precondition_syntax_error(expected_hash)` (and `write_file`, for `overwrite=false` with a well-formed hash, its `no_incumbent` refusal) *before* their in-body permission check; a read-only caller who got either of those refusals now gets `permission_denied` (widened after the verifier and adversarial Codex round 1 both found the `no_incumbent` case). This is the safer order (no argument validation is disclosed to a caller who may not write), and it is pinned by its own scenario. Nothing else changes.

Drift guards (all in `tests/test_issue_358_scoped_tools_list.py`):

1. **Behavioural reconciliation**: all 28 registered tools are called under a read-only OAuth principal (the harness of `tests/test_issue_192_write_refusal_marker.py`, minimal valid arguments); the `permission_denied` set equals `WRITE_REQUIRED_TOOLS`.
2. **The gate reads the registry**: with a member monkeypatched out, the body-boundary gate does not refuse (the in-body check still does); with a read tool monkeypatched in, the gate refuses and the list hides it.
3. **AST**: every `_require_write()` / `_mint_preflight(…, need_write=True)` call in `tools.py` lies in an implementation whose `_tracked` name is a member.
4. **Structural**: `WRITE_REQUIRED_TOOLS == W ∪ {request_upload} == destructive ∪ {create_note}`; `request_download`, `check_upload` ∉; ⊆ registered names; 9 members.

A literal set is immune to the #343 probe-pollution problem by construction (nothing appends to it).

`check_upload` stays visible: it never refuses a read-only caller (a read grant has minted nothing, so it answers `not_found`, which is true). `request_download` stays visible: it is a read (`need_write=False`).

### D3. API keys keep the full list (owner decision, default)

Per the issue: an API key's permission is set by its owner in the panel and shown there; a read-only key is a deliberate operator artefact, and its full list keeps the #343 fixture test, every registry and every scripted client byte-identical. Recorded as L1; extending the rule to read-only keys later is a one-line change to D1 and a second contract change (question for Max).

### D4. The seam: override the public `list_tools()`; the rule and the registry import no SDK

```python
# src/mcp_server/tool_permissions.py  (no `mcp` import; holds the registry too)
def is_filtered(principal, permission) -> bool: ...
def hidden_names() -> frozenset[str]:  # returns tool_permissions.WRITE_REQUIRED_TOOLS
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
- The rule is an ordinary function of `(tools, principal, permission)` over objects with a `.name`. **The promise is precise (Codex round 1):** `tool_permissions.py` imports nothing from `mcp`, directly or transitively, *including when `hidden_names()` and `visible()` are invoked* — which holds because the registry is a literal in an SDK-free module rather than an enumeration of `tools.py` (which imports `mcp.server.fastmcp.Image`). The test runs both calls in a clean subprocess and inspects `sys.modules`.
- **2.x obligation, recorded for #365**: on the 2026-07-28 path a `tools/list` result carries `cacheScope`. A list that varies by authorization MUST be `"private"` (caching utility: "`private` is appropriate … for filtered list results that vary per user"; a `"public"` list "may be shared outside of the initial request's authorization context"). 2.x's default `CacheHint` is `"private"`; the migration SHALL NOT configure `cache_hints` for `tools/list` as `"public"`, and SHALL carry a test that a filtered list is `"private"`. Under 1.30 there is no `cacheScope` field; HTTP responses to the `/mcp` POSTs are not cacheable by shared caches.

### D5. Determinism, the oracle and the fixtures (revised, Codex round 1)

The filtered list is the full list, in registration order, minus the registry. Each kept `MCPTool` is the very object the full list built, so its serialised entry is identical.

- **The oracle**: `tests/fixtures/tools_list_full_v1.json` — the complete `tools/list` `result` object (all 28 tools, every serialised field: `name`, `title`, `description`, `inputSchema`, `outputSchema`, `annotations`, anything else the SDK emits), captured **over HTTP for an API-key principal on the pre-change base c491326**, canonically serialised (UTF-8, `sort_keys`, `separators=(",", ":")`). It is generated and committed as the **first commit of the implementation branch, before any code change**, with the generating script in the test module's docstring. It doubles as the 1.0 contract-freeze fixture: the existing 5a391ca / additions / amendments fixtures pin only names and schemas, and Codex showed that comparing post-change principals to each other proves nothing about the pre-change body.
- **Byte identity** is then literal: for a `readwrite` key, a `read` key and a `readwrite` grant, the canonical serialisation of the HTTP `result` equals the oracle's bytes.
- `tests/fixtures/tools_list_readonly_oauth.json`: the 19 names in order — `keyword_search`, `read_note`, `list_notes`, `get_tags`, `get_recent`, `semantic_search`, `get_vault_guide`, `get_backlinks`, `get_links`, `get_neighborhood`, `find_related`, `find_orphans`, `read_file`, `list_files`, `check_upload`, `request_download`, `list_tasks`, `get_changes`, `hybrid_search`; each filtered entry is asserted byte-identical to the oracle's entry of the same name.
- The #343 fixture test additionally runs under an API-key principal, unchanged otherwise.
- Sequential determinism: N repeated lists under one read-only grant are identical; a second read-only grant gets the same list; interleaving a readwrite list changes neither.
- **Concurrent determinism** (Codex round 1): see D6.

### D6. Enforcement unchanged, including the SDK tool cache and concurrency

The filter changes what is *listed*, never what is *callable*. A read-only grant's `tools/call edit_note` reaches `FastMCP.call_tool` → `_tool_manager` → `_tracked` → `_require_write` and is refused `permission_denied`, with the `tool_write_refused` event and the usage marker, exactly as today.

The one SDK interaction: on 1.30 a filtered list clears and refills `_tool_cache` with 19 entries. Consequences, all checked:

- A `readwrite` or API-key `tools/call` of a hidden tool after that misses the cache, refreshes **under its own authorization**, gets the full list, and validates as today. Correctness never depends on cache state.
- A read-only call of a hidden tool misses (or hits, if a full list was the last refresh); on a miss the SDK logs one WARNING and skips output validation. The refusal is a valid `{"result": str}` structure that validation would have passed, so the response is identical either way (L3).
- No await separates the refresh from the lookup (`list_tools` is synchronous in effect), so one request cannot observe another's refill mid-lookup.

The implementation SHALL NOT touch `_tool_cache` (an internal). Tests pin the refusal response for a read-only grant with the cache (a) warm from a full list and (b) just cleared by a read-only list, both equal to the response before this change, and a readwrite call after (b) still output-validated.

**Concurrency test (Codex round 1).** Sequenced tests cannot catch an implementation that keeps the principal in shared state or that adds a suspension point between a cache refill and a lookup. The suite therefore drives the real ASGI app (`src.main:app`, one worker, one event loop) with five requests released together through an `anyio` barrier: a read-only OAuth `tools/list`, an API-key `tools/list`, a `readwrite` OAuth `tools/list`, a read-only `tools/call` of a hidden tool, and a `readwrite` `tools/call` of a hidden tool (an output-schema-carrying one, e.g. `edit_note` with `dry_run=true`). Each response is asserted against **its own** credential (19 / oracle / oracle / `permission_denied` / ran and output-validated). The round is repeated ≥ 50 times in each of two cache orderings (cache last filled by a full list; by a filtered list), and once more with a test-only `await asyncio.sleep(0)` injected inside the `list_tools()` override, so a future suspension point is exercised rather than assumed absent.

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

### D12. Pagination is unchanged (Codex round 1)

SDK 1.30's `FastMCP.list_tools()` takes no request and returns the complete list; the low-level wrapper ignores `params.cursor` and emits no `nextCursor`. The filter runs on that complete list, so the behaviour is preserved exactly: one result, no `nextCursor`, and a request carrying any `cursor` gets the same result as one without, for both principals. If pagination is ever introduced (2.x or later), it SHALL page the already-filtered list, and cursors are then per-authorization — recorded for #365. The transport test sends one cursor-bearing request per principal.

## Risks

- **SDK 1.30 cache churn** (D6): bounded by the general rate bucket; no correctness effect; one WARNING per read-only call of a hidden tool on a cache miss — those calls already emit a `tool_write_refused` event.
- **SDK 2.x** (#365): the seam is the public `list_tools()`, present in 2.x; the base-class rename and the `cacheScope: "private"` requirement are recorded for that change. If 2.x's `_tool_input_schema` (called before middleware, "also finds a tool that middleware hides") matters, it only affects `Mcp-Param-*` header validation, which this server does not use.
- **A client that treats an unlisted tool as unknown** and refuses to call it client-side: that is the intended effect for a read-only grant.
- **Drift**: a new write tool not added to the registry — caught by guards 1 and 3 in D2 (it refuses in its body but is listed; its `_require_write` call site is outside the registry).
- **Wrong principal kind**: an OAuth request whose `current_principal` were left unset would get the full list (today's behaviour), never a wrong filtered one; the middleware sets both together (`auth.py:1178–1189`).

## Accepted limitations

- **L1** Read-only API keys still list all 28 tools (D3).
- **L2** A read-only grant's `instructions` and some read-tool descriptions name tools it does not list (D7).
- **L3** On SDK 1.30 a read-only call of a hidden tool after a filtered list logs the SDK's "not listed, no validation" WARNING and skips output validation of a refusal that would pass it (D6).
- **L4** No `list_changed`: after a panel scope change, a client sees the new list only when it re-lists (D8).
- **L5** `write_file` / `delete_file`: for a read-only caller, every refusal those bodies made before their in-body permission check — `malformed_precondition` (both tools) and `no_incumbent` (`write_file`, `overwrite=false` with a well-formed `expected_hash`) — is now `permission_denied` (D2). Deliberate, owner decision 2026-10-31: the safer order, disclosing nothing about the arguments or the path's state to a caller who may not write. A `readwrite` caller's refusals are unchanged. Pinned by `test_l5_a_pre_permission_refusal_is_now_permission_denied_for_a_readonly_caller` (including Codex's exact case).
- **L6** The in-body permission checks remain beside the registry gate (D2); they are guarded by the AST test rather than removed.

## Open questions (Max)

1. Should read-only **API keys** also get the filtered list (L1)? Default: no, per the issue.
2. Should the consent page's read-only option say that write tools will not be offered to the agent? That is a template change (browser pass) and touches the consent surface the `oauth-cimd` drafter is near; default: not in this change.
3. The brief's live check said a read-only grant sees 17 tools; the arithmetic is 28 − 9 = **19** (`check_upload` and `request_download` stay listed). Confirm 19 is intended, or name the two further tools to hide.

## Verification plan

Offline + integration suites; `openspec-verifier`; one mandatory adversarial Codex pass (auth/permission surface) framed on: a filter that hides a tool a grant can use, a filter that varies by anything other than the presented credential, a list that leaks across requests, and any change to call-time refusal. Live: a read-only Claude connector lists 19 and is refused on a named `edit_note`; a `readwrite` connector and an API key list 28.

## Implementation notes (design shifts made while implementing)

- **One module, not two.** The rule (`is_filtered`, `hidden_names`, `visible`) lives in `src/mcp_server/tool_permissions.py` beside `WRITE_REQUIRED_TOOLS` rather than in a separate `tool_visibility.py`: the registry and the rule have the same import constraint (no SDK, no `tools.py`), and one file makes "the list reads the object the gate reads" visible at a glance. The spec's "visibility module" is this module.
- **The gate looks the registry up per call**, not once at decoration: the cost is one frozenset membership test, and it is what lets the "the gate reads the registry" test monkeypatch the live object (spec scenario "The registry is the enforcement source").
- **The #343 fixture test is not edited.** Task 4.14's "also under an API-key principal" is met in `tests/test_issue_358_scoped_tools_list.py` (`test_in_process_listing_per_principal`, which compares names and schemas against the oracle for both API-key permissions, and the HTTP byte-identity tests), leaving the #343 module untouched.
- **The HTTP harness stubs authentication only** (`tests/_scoped_tools_harness.py`): the real SDK transport and low-level server run, and a shim sets exactly the contextvars `APIKeyMiddleware._authenticate` sets. The real middleware is exercised where it is the subject (401 before the handler; sandbox pass-through).
- **The concurrency test resets the in-process rate buckets between rounds** (never inside one): the read-only `delete_note` is in the write bucket (burst 30) and would otherwise be refused `rate_limited` from round 31. The injected suspension is placed in `FastMCP.list_tools` (reached through `super()`), because the SDK registered the bound override at construction.

- **Two existing guards learned about the gate.** `tests/test_mcp_rate_limits.py::test_the_documented_gate_order_is_the_implemented_one` now pins the write gate between the quota and the body; `tests/test_issue_88_root_confirmed_before_publish.py`'s write-tool detector skips `_tracked` and its closures (the gate calls `_require_write` for every wrapped tool, which made every read tool look like a writer). Neither guard was weakened: both still classify by what the tool's own body does.

## Codex spec review — round 1

Verdict as written by Codex (`gpt-daybreak-blue-latest`, read-only), verbatim:

FAIL. The SDK 1.30 cache does not itself appear to leak filtered lists: list construction and cache refill contain no suspension point, and `tools/list` does not read from that cache ([SDK source](https://github.com/modelcontextprotocol/python-sdk/blob/v1.30.0/src/mcp/server/lowlevel/server.py)). However, three MAJOR specification-control gaps remain: the hidden set is duplicate metadata rather than the enforcement source, byte identity has no pre-change oracle, and concurrency requirements receive only sequential tests.

```json
{"verdict":"FAIL","findings":[{"severity":"MAJOR","file":"openspec/changes/scoped-tools-list/specs/mcp-request-routing/spec.md","line":123,"claim":"The hidden set is derived from the code that refuses read-only callers.","failing_input":"Following tasks 2.1-2.3 leaves authorization unchanged: __requires_write__ is metadata defaulted from write_class, while the seven direct _require_write calls and two _mint_preflight need_write flags remain independent. request_upload, for example, is hidden because of one explicit stamp and refused because of a separate need_write=True argument. The sets agree today only because a test reconciles them; enforcement never consumes the stamp.","fix":"Either make one declarative write-required source drive both enforcement and discovery, or state honestly that the stamp is duplicate metadata guarded by reconciliation rather than claiming derivation from enforcement."},{"severity":"MAJOR","file":"openspec/changes/scoped-tools-list/specs/mcp-request-routing/spec.md","line":66,"claim":"The full tools/list result remains byte-identical to its pre-change result for every unfiltered principal.","failing_input":"A tool title, description, annotation, or other serialized field changes while names and schemas remain valid. The 5a391ca, additions, and amendments fixtures contain only name/inputSchema/outputSchema, and tasks 4.3-4.4 provide no stored pre-change HTTP result. Comparing three post-change principals to each other cannot prove equality to the unavailable pre-change body.","fix":"Add a committed oracle for the complete normalized pre-change result, or narrow the requirement from byte identity to the exact fields the existing fixtures actually pin."},{"severity":"MAJOR","file":"openspec/changes/scoped-tools-list/tasks.md","line":25,"claim":"The tests prove results cannot vary with concurrent request timing or process-wide cache mutation.","failing_input":"A read-only OAuth tools/list overlaps an API-key or readwrite tools/list and a hidden tools/call in the same worker. Tasks 4.5 and 4.7 only sequence requests, so an implementation using shared principal state or introducing a suspension inside cache refresh can pass while returning 19 tools to a legitimate caller or skipping its output validation.","fix":"Require synchronized concurrent ASGI requests covering read-only list versus API-key/readwrite list and hidden call, asserting each response against its own credential and repeating both cache orderings."},{"severity":"MINOR","file":"openspec/changes/scoped-tools-list/tasks.md","line":31,"claim":"The SDK-free test exercises the visibility rule's SDK independence.","failing_input":"tool_visibility can lazily import production_write_required_tools from src.mcp_server.tools. The subprocess import test passes because the lazy function is not called, but invoking hidden_names then imports tools.py, which imports mcp.server.fastmcp.Image.","fix":"Test invocation of hidden_names in the clean subprocess, or redefine the promise as no direct SDK import and stop describing the whole rule as SDK-free."},{"severity":"MINOR","file":"openspec/changes/scoped-tools-list/specs/mcp-request-routing/spec.md","line":72,"claim":"The authorization-dependent result is fully specified for tools/list requests.","failing_input":"A tools/list request includes a cursor. Neither the delta nor tasks pin whether the existing SDK behavior of returning the complete unpaginated list is preserved, while filtering before versus after future pagination produces different page sizes and cursors.","fix":"State that cursor and pagination behavior remain unchanged and add one cursor-bearing request to the transport test."}]}
```

What changed, per finding:

1. **MAJOR, duplicate metadata.** The `requires_write` stamp is gone. One literal `WRITE_REQUIRED_TOOLS` in SDK-free `tool_permissions.py` drives both a body-boundary permission gate in `_tracked` and the list filter; the in-body checks stay as defence in depth under an AST guard; one deliberate precedence change (L5). D2; spec "One write-required registry SHALL drive both…"; tasks §2.
2. **MAJOR, no pre-change oracle.** A complete canonical `tools/list` result for an API-key principal, captured over HTTP on c491326, is committed first as `tests/fixtures/tools_list_full_v1.json` (also the 1.0 freeze fixture); byte identity is asserted against it. D5; spec "The full list is byte-identical to the committed pre-change oracle"; tasks 4.1.
3. **MAJOR, sequential-only tests.** A barrier-synchronised concurrent ASGI test (three lists and two hidden calls, each checked against its own credential, both cache orderings, ≥ 50 rounds, plus an injected suspension). D6; spec "Concurrent requests each get their own credential's answer"; tasks 4.6.
4. **MINOR, SDK-free claim.** Promise restated as "no SDK import, directly or transitively, including on invocation", made true by the literal registry, and tested by invoking `hidden_names()` and `visible()` in a clean subprocess. D4; tasks 4.12.
5. **MINOR, pagination.** Cursor behaviour stated unchanged (complete list, no `nextCursor`, cursor ignored) and pinned with one cursor-bearing request per principal. D12; spec "A cursor does not change the result"; tasks 4.5.

## Codex spec review — round 2

Verdict as written by Codex, verbatim:

1. Hidden-set enforcement source — VERIFIED. `WRITE_REQUIRED_TOOLS` now drives both `_tracked` enforcement and discovery, with reconciliation and AST guards.
2. Pre-change byte-identity oracle — VERIFIED. The complete canonical HTTP result must be captured and committed before implementation changes.
3. Concurrent request isolation — VERIFIED. Barrier-synchronized ASGI tests cover three principals, hidden calls, both cache orderings, and an injected suspension point.
4. SDK-free visibility rule — VERIFIED. The subprocess invokes both `hidden_names()` and `visible()` before inspecting loaded modules.
5. Cursor and pagination behavior — VERIFIED. The specification preserves the complete unpaginated response and tests cursor-bearing requests for filtered and full-list principals.

No new BLOCKER or MAJOR defect was found within the requested failure classes. The revised proposal closes the round-1 control gaps and passes strict OpenSpec validation.

```json
{"verdict":"PASS","round1":[{"claim":"The hidden set is duplicate metadata rather than the enforcement source.","status":"VERIFIED","note":"One literal WRITE_REQUIRED_TOOLS registry now drives both the _tracked body-boundary permission gate and the list filter; behavioral, AST, and structural tests guard drift."},{"claim":"Byte identity has no pre-change oracle.","status":"VERIFIED","note":"Tasks require the complete canonical HTTP tools/list result to be captured from the unchanged base and committed alone before source changes, then compared for all unfiltered principal types."},{"claim":"Concurrency requirements receive only sequential tests.","status":"VERIFIED","note":"The revised scenario requires barrier-synchronized requests for three list principals and two hidden calls, repeated under both cache orderings and with an injected suspension."},{"claim":"The SDK-free test does not invoke the visibility rule.","status":"VERIFIED","note":"The clean subprocess must invoke hidden_names and visible on stand-in objects before asserting that no mcp module was loaded."},{"claim":"Cursor and pagination behavior are unspecified.","status":"VERIFIED","note":"The result is explicitly complete and unpaginated, cursor is ignored, nextCursor is absent, and cursor-bearing transport requests are required for both filtered and full-list principals."}],"findings":[]}
```

Owner answers to the open questions (Max, 2026-10-31): read-only API keys are **not** filtered (L1 stands); no consent-page wording; 19 is the count.

## Verifier and adversarial Codex — implementation round 1 (cfbf2b8)

The `openspec-verifier` passed cfbf2b8 with 0 blocking, 1 MAJOR, 3 MINOR; adversarial Codex failed it with exactly one finding, the verifier's MAJOR.

**Decision (owner, 2026-10-31):** keep the new, safer order and widen L5 explicitly — in the spec requirement and its scenario, D2, L5 and `docs/architecture/vault-tools.md` — to "any pre-permission refusal of `write_file` / `delete_file` (malformed `expected_hash`, `no_incumbent`) is now `permission_denied` for a read-only caller", with Codex's regression case pinned as a test.

Verifier minors, folded:

1. Task 4.9's test now asserts, per refused call, the `tool_write_refused` record, the usage row and its `permission_denied` marker, the rate tokens taken (one general, plus one write for a write-bucket tool — what the pre-change path took before its body refused) and exactly one quota admission; and both cache orderings run inside one test, so the comparison no longer depends on two parameters sharing a process.
2. The real scope-change path: `tests/integration/test_scoped_tools_list_pg.py` seeds a `read` grant in Postgres, lists through the real `APIKeyMiddleware` (19), upgrades the family through the panel handler `update_oauth_token_scope`, lists again (28), and asserts exactly one JSON-RPC message per response and that `ServerSession.send_tool_list_changed` was never called.
3. Task 6.1 names what ran and where (below the tasks list), unchanged otherwise.

Adversarial Codex verdict, verbatim:

FAIL. The filtering/cache implementation held up under targeted review, and 129 relevant tests passed, but there is one plausible, unapproved call-time behavior change beyond L5.

```json
{"verdict":"FAIL","findings":[{"severity":"MAJOR","file":"src/mcp_server/tools.py","line":1946,"claim":"The new body-boundary gate changes write_file refusal precedence beyond the sole malformed-hash exception approved in L5.","failing_input":"A read-only OAuth grant calls write_file(path=\"a.bin\", content=\"AAAA\", overwrite=false, expected_hash=\"sha256:\" followed by 64 zeroes). On c491326, write_file returned no_incumbent before reaching _require_write; HEAD returns permission_denied from _tracked. The new tests cover only malformed expected_hash, so this valid-hash case passes unnoticed.","fix":"Preserve the existing valid-hash/no-overwrite no_incumbent precheck ahead of the permission gate and add an exact regression assertion for this read-only case. If the broader precedence change is intended, obtain owner approval and amend L5 and the documentation explicitly."}]}
```
