Refs #358

## Why

The `read` and `readwrite` OAuth scopes are enforced at call time. `_require_write` (`src/mcp_server/tools.py:4060`) refuses a read-only credential inside every write body, and `_mint_preflight(need_write=True)` does the same for `request_upload` and `import_from_url`. But `tools/list` returns all 28 tools to every principal. A read-only grant in Claude therefore sees `edit_note`, `delete_note`, `write_file` and the rest: it spends tool-search and context budget on nine tools that can never succeed for it, it tries them, and each try produces a `permission_denied` refusal, a `tool_write_refused` security event and a usage row. Read-only is the **preselected** consent choice (`oauth-authorization-integrity`, "Consent preselects the least privilege"), so this is the common case for an OAuth connector, not an edge case.

The MCP 2026-07-28 tools page sanctions the fix explicitly (https://modelcontextprotocol.io/specification/2026-07-28/server/tools, "Capabilities"):

> Servers that declare the `tools` capability **MUST** respond to `tools/list` requests with the set of tools currently available to the requesting client. This set **MAY** be empty and **MAY** change over time (see List Changed Notification), but **MUST NOT** vary per-connection or as a side effect of other requests on the connection. The set **MAY** vary by the authorization presented on the request — for example, returning only the tools the caller's granted scopes permit — since credentials are per-request input, not connection state.
>
> Servers **SHOULD** return tools in a deterministic order (i.e., the same ordering across requests when the underlying set of tools has not changed).

Now, because 1.0 is a tool-contract promise: shrinking what a read-only grant lists is a contract change, and making it before the freeze costs a release note; after it, a major version.

## What Changes

- **`tools/list` is filtered for OAuth grants without `readwrite`.** When the request's principal is `("oauth", grant_id)` and its permission — the same `current_permission` `_require_write` reads — is not `readwrite`, the list omits every tool that refuses every call from a read-only credential: the write bucket (`_tracked(write_class=True)`: `create_note`, `edit_note`, `move_note`, `delete_note`, `set_frontmatter`, `write_file`, `delete_file`, `import_from_url`) plus `request_upload`. Nine hidden, **19 listed**. The nine names live in **one** registry, `WRITE_REQUIRED_TOOLS` (a `frozenset` in a new SDK-free `tool_permissions.py`), which drives both a new body-boundary permission gate in `_tracked` and the list filter, so the two cannot drift; the existing in-body checks stay as defence in depth under an AST guard, and a test calls all 28 tools under a read-only grant (design D2).
- **Everyone else gets today's list, unchanged**: API-key principals whatever the key's permission (owner decision, design D3), `readwrite` OAuth grants, `MCP_SANDBOX_MODE`, the stdio entry point, and any in-process caller with no principal. The full list is asserted byte-identical to a committed oracle of the complete pre-change `tools/list` result (`tools_list_full_v1.json`, captured over HTTP for an API key on c491326; also the 1.0 freeze fixture).
- **Order is deterministic**: the filtered list is the full list in its existing order with the hidden entries removed, and each kept entry is identical to its oracle entry. A second fixture pins the 19 names in order. Pagination is unchanged: one complete result, no `nextCursor`, a cursor ignored (design D12).
- **Enforcement is unchanged in outcome.** The filter is a UX and context-budget measure. A read-only grant that calls a hidden tool by name is refused as today — same refusal text and code, usage row, security event, token and quota spend — pinned with the SDK's tool cache warm and cleared, and under barrier-synchronised concurrent requests (design D6). The one deliberate difference: for a read-only caller, any refusal `write_file` / `delete_file` made before their permission check (malformed `expected_hash`; `no_incumbent`) is now `permission_denied` (L5).
- **The override seam** is a `FastMCP` subclass overriding the public `list_tools()` method, which both SDK 1.30 (`FastMCP._setup_handlers`) and SDK 2.x (`MCPServer._handle_list_tools`) call to answer `tools/list`. The visibility rule and the registry import nothing from the SDK, directly or transitively, even when invoked (design D4).
- **Not changed**: the server `instructions` (design D7), descriptions, schemas, annotations; `notifications/tools/list_changed` is never sent and `listChanged` stays `false` (design D8); `tools/list` writes no usage row, before or after (design D9); **no new setting** (design D10).
- **BREAKING for read-only OAuth grants only**: their visible list shrinks from 28 to 19. Nothing changes for API keys or `readwrite` grants.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `mcp-request-routing` (governs the published tool surface since #343):
  - MODIFIED "Every registered tool SHALL publish a title and a complete set of behaviour annotations" — the metadata and classification are statements about the registry and the full list; the scenarios say which principal lists.
  - MODIFIED "Tool metadata SHALL leave the published tool surface otherwise unchanged" — the fixture comparison is the full list as served to an API-key principal.
  - ADDED "`tools/list` SHALL omit the write-requiring tools for an OAuth grant without the `readwrite` scope, and only for it".
  - ADDED "One write-required registry SHALL drive both the pre-body permission check and the tool-list filter".
  - ADDED "Hiding a tool from `tools/list` SHALL NOT change whether a call to it is admitted or refused".

No OAuth spec names what a scope grants in terms of tools, so `oauth-authorization-integrity` gets no delta; the consent copy is unchanged (see design, "Open questions"). There is no `vault-tools` spec capability; the "every registered tool" statements live in `mcp-request-routing` and are modified there.

## Impact

- `src/mcp_server/server.py`: `mcp` is constructed from a `FastMCP` subclass whose `list_tools()` applies the visibility rule; no registration changes.
- `src/mcp_server/tool_permissions.py` (new, SDK-free): `WRITE_REQUIRED_TOOLS` and the rule (`is_filtered`, `hidden_names`, `visible`) — one module (implementation shift, design "Implementation notes").
- `src/mcp_server/tools.py`: `_tracked` runs `_require_write()` at the body boundary for registry members (after every existing gate); the in-body checks are untouched.
- Tests: new `tests/test_issue_358_scoped_tools_list.py`; new fixtures `tests/fixtures/tools_list_full_v1.json` (complete pre-change oracle, committed first) and `tests/fixtures/tools_list_readonly_oauth.json` (19 names, in order); `tests/test_issue_343_tool_metadata.py` unchanged in what it compares.
- Docs: `docs/architecture/vault-tools.md` ("Tool metadata" gains "Who sees which tools"), `docs/architecture/oauth-and-grants.md` (what a `read` grant lists), `CLAUDE.md` (MCP tools; one key-decisions bullet), `README.md` (OAuth: a read-only connection lists 19 tools; reconnect after changing a grant's scope).
- No migration, dependency, setting or template change.
- Clients: a read-only Claude connector lists 19 tools; a `readwrite` connector and every API key list 28.
