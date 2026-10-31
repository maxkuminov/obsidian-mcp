Refs #358

## Why

The `read` and `readwrite` OAuth scopes are enforced at call time. `_require_write` (`src/mcp_server/tools.py:4060`) refuses a read-only credential inside every write body, and `_mint_preflight(need_write=True)` does the same for `request_upload` and `import_from_url`. But `tools/list` returns all 28 tools to every principal. A read-only grant in Claude therefore sees `edit_note`, `delete_note`, `write_file` and the rest: it spends tool-search and context budget on nine tools that can never succeed for it, it tries them, and each try produces a `permission_denied` refusal, a `tool_write_refused` security event and a usage row. Read-only is the **preselected** consent choice (`oauth-authorization-integrity`, "Consent preselects the least privilege"), so this is the common case for an OAuth connector, not an edge case.

The MCP 2026-07-28 tools page sanctions the fix explicitly (https://modelcontextprotocol.io/specification/2026-07-28/server/tools, "Capabilities"):

> Servers that declare the `tools` capability **MUST** respond to `tools/list` requests with the set of tools currently available to the requesting client. This set **MAY** be empty and **MAY** change over time (see List Changed Notification), but **MUST NOT** vary per-connection or as a side effect of other requests on the connection. The set **MAY** vary by the authorization presented on the request — for example, returning only the tools the caller's granted scopes permit — since credentials are per-request input, not connection state.
>
> Servers **SHOULD** return tools in a deterministic order (i.e., the same ordering across requests when the underlying set of tools has not changed).

Now, because 1.0 is a tool-contract promise: shrinking what a read-only grant lists is a contract change, and making it before the freeze costs a release note; after it, a major version.

## What Changes

- **`tools/list` is filtered for OAuth grants without `readwrite`.** When the request's principal is `("oauth", grant_id)` and its permission — the same `current_permission` `_require_write` reads — is not `readwrite`, the list omits every tool that refuses every call from a read-only credential: the write bucket (`_tracked(write_class=True)`: `create_note`, `edit_note`, `move_note`, `delete_note`, `set_frontmatter`, `write_file`, `delete_file`, `import_from_url`) plus `request_upload`. Nine hidden, **19 listed**. The hidden set is derived from a stamp on the `_tracked` wrapper, not a hand-kept list, and a guard test reconciles it with the tools that actually refuse a read-only credential (design D2).
- **Everyone else gets today's list, unchanged**: API-key principals whatever the key's permission (owner decision, design D3), `readwrite` OAuth grants, `MCP_SANDBOX_MODE`, the stdio entry point, and any in-process caller with no principal. The existing fixture test keeps comparing the full 28-tool list.
- **Order is deterministic**: the filtered list is the full list in its existing order with the hidden entries removed, and each kept entry is identical to its full-list entry. A second fixture pins the 19 names in order.
- **Enforcement is unchanged.** The filter is a UX and context-budget measure. A read-only grant that calls a hidden tool by name is refused exactly as today — same refusal text and code, same usage row, same security event — and a test pins that with the SDK's tool cache both warm and cleared (design D6).
- **The override seam** is a `FastMCP` subclass overriding the public `list_tools()` method, which both SDK 1.30 (`FastMCP._setup_handlers`) and SDK 2.x (`MCPServer._handle_list_tools`) call to answer `tools/list`. The visibility rule itself is a pure function with no SDK import (design D4).
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
  - ADDED "Hiding a tool from `tools/list` SHALL NOT change how a call to it is admitted or refused".
  - ADDED "The hidden set SHALL be derived from the code that refuses read-only callers".

No OAuth spec names what a scope grants in terms of tools, so `oauth-authorization-integrity` gets no delta; the consent copy is unchanged (see design, "Open questions"). There is no `vault-tools` spec capability; the "every registered tool" statements live in `mcp-request-routing` and are modified there.

## Impact

- `src/mcp_server/server.py`: `mcp` is constructed from a `FastMCP` subclass whose `list_tools()` applies the visibility rule; no registration changes.
- `src/mcp_server/tool_visibility.py` (new, SDK-free): the rule — which principal is filtered, and which names are hidden.
- `src/mcp_server/tools.py`: `_tracked` gains a `requires_write` stamp (`__requires_write__`, defaulting to `write_class`; `request_upload` sets it) and `production_write_required_tools()`, enumerated exactly like `production_write_bucket_tools()`.
- Tests: new `tests/test_issue_358_scoped_tools_list.py`; new fixture `tests/fixtures/tools_list_readonly_oauth.json` (19 names, in order); `tests/test_issue_343_tool_metadata.py` unchanged in what it compares.
- Docs: `docs/architecture/vault-tools.md` ("Tool metadata" gains "Who sees which tools"), `docs/architecture/oauth-and-grants.md` (what a `read` grant lists), `CLAUDE.md` (MCP tools; one key-decisions bullet), `README.md` (OAuth: a read-only connection lists 19 tools; reconnect after changing a grant's scope).
- No migration, dependency, setting or template change.
- Clients: a read-only Claude connector lists 19 tools; a `readwrite` connector and every API key list 28.
