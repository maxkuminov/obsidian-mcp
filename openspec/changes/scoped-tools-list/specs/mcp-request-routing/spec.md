## MODIFIED Requirements

### Requirement: Every registered tool SHALL publish a title and a complete set of behaviour annotations
Every registered tool — and therefore every entry of every `tools/list` result, full or filtered by the `scoped-tools-list` rule — SHALL carry a non-empty `title` and a `ToolAnnotations` whose `readOnlyHint`, `destructiveHint`, `idempotentHint` and `openWorldHint` are each an explicit boolean, never absent, so that no client falls back to the protocol's defaults (`readOnlyHint` false, `destructiveHint` true, `openWorldHint` true). The hints SHALL be set through the public registration API (`FastMCP.tool(title=…, annotations=…)`) on each registration. The classification SHALL be: every tool that neither changes vault bytes nor mints a capability is `readOnlyHint=true, destructiveHint=false, idempotentHint=true, openWorldHint=false`; the set of `destructiveHint=true` tools is exactly the write-bucket set (the production tools registered with `_tracked(write_class=True)`) minus `create_note` plus `request_upload` (whose `overwrite=True` capability overwrites on redemption — owner decision, 2026-10-28); the set of `readOnlyHint=false` tools is exactly the write-bucket set plus `request_upload` and `request_download`; `openWorldHint=true` is exactly `import_from_url`; and `idempotentHint=true` is exactly the read-only set. The write-bucket set used for these comparisons SHALL be derived from the production tools only, so that a `_tracked(write_class=True)` decoration performed by a test module cannot change it. An undo path through `.trash/` SHALL NOT make a deleting tool non-destructive. The classification is a statement about the registry and the full list; a filtered list SHALL carry each kept tool's annotations unchanged. Annotations are a client-side hint: no server-side gate SHALL consult them, and the tool-list filter SHALL NOT consult them either (it reads the write-required registry `WRITE_REQUIRED_TOOLS`); read-only enforcement SHALL remain `_tracked`'s admission and `_require_write`'s permission check, unchanged.

#### Scenario: Every listed tool carries complete metadata
- **WHEN** a client authenticated with an API key lists tools
- **THEN** each of the 28 tools SHALL carry a non-empty `title`, distinct from every other tool's
- **AND** each SHALL carry `annotations` with `readOnlyHint`, `destructiveHint`, `idempotentHint` and `openWorldHint` each `true` or `false`, none absent

#### Scenario: The read tools are marked read-only
- **WHEN** a client authenticated with an API key lists tools
- **THEN** `keyword_search`, `semantic_search`, `read_note`, `list_notes`, `get_tags`, `get_recent`, `get_vault_guide`, `read_file`, `list_files`, `get_backlinks`, `get_links`, `get_neighborhood`, `find_related`, `find_orphans`, `check_upload`, `list_tasks`, `get_changes` and `hybrid_search` (#354) SHALL each carry `readOnlyHint=true`, `destructiveHint=false`, `idempotentHint=true`, `openWorldHint=false`
- **AND** no other tool SHALL carry `readOnlyHint=true`

#### Scenario: The destructive set is the write bucket minus create_note plus request_upload
- **WHEN** the set of tools carrying `destructiveHint=true` is compared with the production write-bucket set, read from the code rather than from a list kept in the test
- **THEN** the destructive set SHALL equal the write-bucket set with `create_note` removed and `request_upload` added — `edit_note`, `move_note`, `delete_note`, `set_frontmatter`, `write_file`, `delete_file`, `import_from_url`, `request_upload`
- **AND** the write-bucket set SHALL equal the `write` class of `concurrency.TOOL_CLASSES`
- **AND** `create_note` and `request_download` SHALL carry `readOnlyHint=false` and `destructiveHint=false`

#### Scenario: A test probe cannot pollute the write-bucket set
- **WHEN** a test module decorates a function with `_tracked("probe_write", [], write_class=True)` and another with `_tracked("edit_note", [], write_class=False)` without registering either on the production server
- **THEN** the production write-bucket set the metadata test derives SHALL be unchanged — still exactly the eight production tools

#### Scenario: Only import_from_url is open-world
- **WHEN** a client authenticated with an API key lists tools
- **THEN** `import_from_url` SHALL carry `openWorldHint=true` and `destructiveHint=true`
- **AND** every other tool SHALL carry `openWorldHint=false`

#### Scenario: A hint gates nothing
- **WHEN** a credential with read-only permission calls `edit_note`, `create_note` or `request_upload`
- **THEN** the call SHALL be refused with `permission_denied` by `_require_write`, exactly as before this change
- **AND** no code path that admits or refuses a tool call SHALL read the tool's `annotations`

#### Scenario: A filtered list carries the same metadata
- **WHEN** an OAuth grant without `readwrite` lists tools
- **THEN** each of the 19 listed tools SHALL carry the same `title` and the same four hints as its entry in the full list

### Requirement: Tool metadata SHALL leave the published tool surface otherwise unchanged
Adding titles, annotations and instructions, and shortening descriptions, SHALL NOT change the set of tool names, their listing order, any tool's `inputSchema`, or any tool's `outputSchema`. These statements describe the **full list** — the list served to every principal the `scoped-tools-list` rule does not filter (an API key of either permission, an OAuth grant with `readwrite`, sandbox mode, and an in-process caller with no principal); the list a read-only OAuth grant receives is defined by that rule as this list minus its hidden entries. The first 25 entries of the full listing SHALL be compared against the frozen fixture generated from the registry on `main` at 5a391ca, and every entry after them SHALL be compared, in order, against `tests/fixtures/tools_list_schema_additions.json`, which a tool-adding change regenerates deliberately; `list_tasks` (#350) is its first entry, `get_changes` (#351) its second and `hybrid_search` (#354) its third, so the listing holds 28 tools. A deliberate schema change to one of the first 25 tools SHALL be recorded as a full replacement entry in `tests/fixtures/tools_list_schema_amendments.json`, and that entry SHALL be compared instead of the frozen one; the frozen fixture itself SHALL NOT be edited. The change-feed change (#351) records two amendments, `list_notes` and `get_recent`, each differing from its frozen entry only by the optional `since`, `until`, `order_by` and `cursor` input properties. The `hybrid-search` change (#354) adds no amendment: the three search tools' descriptions change and their schemas do not. The reranker change (#367) records a third amendment, `semantic_search`, differing from its frozen entry only by the optional boolean `rerank` input property (default false), and regenerates the `hybrid_search` additions entry, which then differs from its #354 form only by the same optional `rerank` property. A schema change to an existing tool SHALL be a deliberate fixture regeneration and never a side effect.

#### Scenario: The same 25 tools in the same order, then the additions
- **WHEN** a client authenticated with an API key lists tools, and again when the listing is taken in-process with no principal
- **THEN** the first 25 names SHALL equal the sequence recorded in `tests/fixtures/tools_list_schema_5a391ca.json`
- **AND** the remaining names SHALL equal the sequence recorded in `tests/fixtures/tools_list_schema_additions.json`, which after this change is exactly `list_tasks`, `get_changes`, `hybrid_search`, for 28 tools in all

#### Scenario: Every schema is identical to the fixture
- **WHEN** each tool's `inputSchema` and `outputSchema` are compared with the entry of the same name in the amendments fixture if it holds one, and otherwise in whichever of the two other fixtures holds it
- **THEN** each SHALL be equal as a JSON value
- **AND** a test SHALL assert that `get_recent` and `list_notes` each differ from their frozen entries only by the four optional properties, that `semantic_search` differs from its frozen entry only by the optional `rerank` property, that none of these is listed in `required`, and that the amendments fixture names exactly `get_recent`, `list_notes` and `semantic_search`
- **AND** the set of tools carrying an `outputSchema` SHALL be unchanged apart from the additions (`list_tasks`, `get_changes` and `hybrid_search` return `str` and carry the SDK's `{"result": string}` wrapper) — on 5a391ca every tool but `read_file` carries one (the SDK wraps a `str` return as `{"result": string}`; `read_note` publishes its model's schema; `read_file` returns content blocks and has none)

#### Scenario: Unknown-argument refusal is unaffected
- **WHEN** `tests/test_issue_295_unknown_arguments.py` runs with its tool count at 28
- **THEN** it SHALL pass, and every `inputSchema`, `get_changes`'s, `hybrid_search`'s and the amended `semantic_search`'s included, SHALL carry `additionalProperties: false`

#### Scenario: The semantic_search amendment carries only rerank
- **WHEN** the `semantic_search` amendment entry is compared with its frozen entry
- **THEN** the only difference SHALL be a `rerank` property of type boolean with default false, absent from `required`

#### Scenario: The hybrid_search additions entry carries rerank
- **WHEN** the `hybrid_search` entry of the additions fixture is read
- **THEN** its `inputSchema` SHALL carry a `rerank` property of type boolean with default false, absent from `required`, and no other property SHALL differ from the #354 entry

#### Scenario: The full list is byte-identical to the committed pre-change oracle
- **WHEN** `tools/list` is requested over HTTP with a `readwrite` API key, a `read` API key, and a `readwrite` OAuth grant
- **THEN** each `result` object, serialised canonically (UTF-8 JSON, keys sorted, no insignificant whitespace), SHALL be byte-identical to `tests/fixtures/tools_list_full_v1.json` — the complete `tools/list` result (all 28 tools, every field: `name`, `title`, `description`, `inputSchema`, `outputSchema`, `annotations`, and any other field the SDK serialises), generated over HTTP for an API-key principal on the pre-change base (c491326) and committed before any implementation change
- **AND** that oracle SHALL be edited only by a change that deliberately alters the published tool surface, which then states the edit in its own spec delta

## ADDED Requirements

### Requirement: `tools/list` SHALL omit the write-requiring tools for an OAuth grant without the `readwrite` scope, and only for it
When the request's principal is an OAuth grant (`current_principal` of kind `"oauth"`) and the request's permission (`current_permission`, the value `_require_write` reads, derived from the token's scope by `token_has_write`) is not `readwrite`, the `tools/list` result SHALL be the full list with every tool named in the write-required registry (`WRITE_REQUIRED_TOOLS`) removed, in the full list's order, each kept entry identical to its full-list entry. The registry SHALL name exactly `create_note`, `edit_note`, `move_note`, `delete_note`, `set_frontmatter`, `write_file`, `delete_file`, `import_from_url` and `request_upload`, so such a grant lists 19. For every other request — an API-key principal of either permission, an OAuth grant with `readwrite`, `MCP_SANDBOX_MODE`, the stdio entry point, or any in-process call with no principal — the result SHALL be the full list. The result SHALL depend only on the authorization presented on that request: it MUST NOT depend on any earlier or concurrent request, connection, session, cache state or time. Cursor and pagination behaviour SHALL be unchanged: the server returns the complete (full or filtered) list in one result with no `nextCursor`, and a request carrying a `cursor` receives the same result as one without. The server SHALL NOT send `notifications/tools/list_changed` and SHALL keep advertising `listChanged: false`. The server `instructions` and every tool description SHALL be the same for every principal. A `tools/list` request writes no `usage_logs` row, as before. No setting SHALL enable or disable this behaviour. The filter SHALL be applied by overriding the server object's public `list_tools()` method; the visibility rule and the registry SHALL live in modules that import nothing from the MCP SDK, directly or transitively, including when the rule is invoked.

#### Scenario: A read-only grant lists 19 tools in a fixed order
- **WHEN** an OAuth grant whose scope lacks `readwrite` sends `tools/list`
- **THEN** the result SHALL list exactly the names in `tests/fixtures/tools_list_readonly_oauth.json`, in that order: `keyword_search`, `read_note`, `list_notes`, `get_tags`, `get_recent`, `semantic_search`, `get_vault_guide`, `get_backlinks`, `get_links`, `get_neighborhood`, `find_related`, `find_orphans`, `read_file`, `list_files`, `check_upload`, `request_download`, `list_tasks`, `get_changes`, `hybrid_search`
- **AND** each entry SHALL be byte-identical, under the canonical serialisation, to the entry of the same name in `tests/fixtures/tools_list_full_v1.json`

#### Scenario: Full list for everyone else
- **WHEN** `tools/list` is sent with a `readwrite` OAuth grant, a `readwrite` API key or a `read` API key
- **THEN** the result SHALL list all 28 tools in the full list's order

#### Scenario: Sandbox and in-process callers get the full list
- **WHEN** `tools/list` is served with `MCP_SANDBOX_MODE=true`, or `await mcp.list_tools()` is called with no principal set
- **THEN** the result SHALL list all 28 tools

#### Scenario: An unauthenticated list never reaches the handler
- **WHEN** `tools/list` is sent to `/mcp` with no credential and sandbox mode is off
- **THEN** the middleware SHALL answer 401 and the list handler SHALL NOT run

#### Scenario: Deterministic and free of side effects
- **WHEN** one read-only grant lists tools repeatedly, a second read-only grant lists tools, and a `readwrite` principal lists tools between them in the same process
- **THEN** every read-only result SHALL be identical, and every `readwrite` result SHALL be the full list

#### Scenario: Concurrent requests each get their own credential's answer
- **WHEN** a read-only OAuth `tools/list`, an API-key `tools/list`, a `readwrite` OAuth `tools/list`, a read-only OAuth `tools/call` of a hidden tool and a `readwrite` `tools/call` of a hidden tool are released together through one synchronisation barrier into the same ASGI app and worker, repeated at least 50 times, once with the SDK tool cache last filled by a full list and once with it last filled by a filtered list, and again with a test-only suspension point (`await asyncio.sleep(0)`) injected inside the `list_tools()` override
- **THEN** every read-only list SHALL be the 19-entry list, every API-key and `readwrite` list SHALL be byte-identical to the oracle, every read-only hidden call SHALL be refused `permission_denied`, and every `readwrite` hidden call SHALL run with its result validated against that tool's `outputSchema`

#### Scenario: A cursor does not change the result
- **WHEN** a read-only grant and an API key each send `tools/list` with `params.cursor` set to an arbitrary string
- **THEN** each result SHALL equal that principal's result without a cursor, and neither SHALL carry `nextCursor`

#### Scenario: The list follows a scope change on the next request
- **WHEN** a grant's scope is changed from `read` to `readwrite` in the control panel and the grant then sends `tools/list`
- **THEN** that result SHALL be the full list, and no notification SHALL have been sent

#### Scenario: Instructions are not filtered
- **WHEN** a read-only OAuth grant and an API key each send `initialize`
- **THEN** both results SHALL carry the same `instructions`, equal to the module-level constant

#### Scenario: The rule and the registry are SDK-free when invoked
- **WHEN** a clean subprocess imports the visibility module, calls `hidden_names()` and `visible(…)` on stand-in tool objects, and then inspects `sys.modules`
- **THEN** no module named `mcp` or `mcp.*` SHALL be loaded, and the server's `list_tools()` override SHALL be the only production caller of `visible`

### Requirement: One write-required registry SHALL drive both the pre-body permission check and the tool-list filter
`WRITE_REQUIRED_TOOLS`, one `frozenset` of tool names in an SDK-free module, SHALL be the single source of which tools require the `readwrite` permission. `_tracked` SHALL consult it for its own tool name and, for a member, SHALL run the read-only check (`_require_write`) at the body boundary — after every existing gate (argument caps, vault root, rate buckets, concurrency, quota), immediately before the body would run — returning that refusal without running the body; the tool-list filter SHALL read the same object. No decorator argument, stamp or second list SHALL define write-required membership. The in-body `_require_write()` calls and `_mint_preflight(need_write=True)` MAY remain as defence in depth, and every production call site of `_require_write` or `_mint_preflight(need_write=True)` SHALL belong to a tool in the registry. The write bucket (`write_class=True`) SHALL equal the registry minus `request_upload`.

#### Scenario: The registry is the enforcement source
- **WHEN** a tool name is removed from `WRITE_REQUIRED_TOOLS` in a test (monkeypatched) and a read-only grant calls that tool
- **THEN** the pre-body gate SHALL NOT refuse it (the in-body check still does), proving the gate reads the registry; and when a read-only tool's name is added, the pre-body gate SHALL refuse it and `tools/list` SHALL hide it

#### Scenario: Reconciliation with the call sites
- **WHEN** each of the 28 registered tools is called with minimal valid arguments under a read-only OAuth principal
- **THEN** the tools refused with `permission_denied` SHALL be exactly `WRITE_REQUIRED_TOOLS`
- **AND** an AST scan of `src/mcp_server/tools.py` SHALL find every `_require_write()` and `_mint_preflight(…, need_write=True)` call inside a tool implementation whose `_tracked` name is in the registry

#### Scenario: Reconciliation with the other derived sets
- **WHEN** the registry is compared with the production write-bucket set and the `destructiveHint=true` set
- **THEN** it SHALL equal the write-bucket set plus `request_upload`, and the destructive set plus `create_note`
- **AND** it SHALL NOT contain `request_download` or `check_upload`, and SHALL be a subset of the registered tool names

### Requirement: Hiding a tool from `tools/list` SHALL NOT change whether a call to it is admitted or refused
The tool-list filter is a presentation of what a credential can use, not a control. A `tools/call` of a hidden tool by a read-only OAuth grant SHALL pass through the same gates as before this change, in the same order, and SHALL be refused with the same refusal text, the same `MCP-REFUSAL` code `permission_denied`, the same `tool_write_refused` security event, the same usage row and marker, and the same rate-token and quota consumption as before. The one permitted difference is precedence inside two bodies: `write_file` and `delete_file` validate `expected_hash` syntax before their in-body permission check today, so a read-only caller sending a malformed hash to them SHALL now receive `permission_denied` (from the body-boundary gate) instead of the syntax refusal; for every other tool and argument the response SHALL be identical. No code in this server that admits or refuses a tool call SHALL consult the filtered list. The implementation SHALL NOT read or write the SDK's internal tool cache.

#### Scenario: A named hidden tool is still refused, not unknown
- **WHEN** a read-only OAuth grant calls `edit_note`, `request_upload` or `import_from_url` by name without having listed it
- **THEN** the call SHALL be refused with `permission_denied` exactly as before this change, as a tool result and not as an unknown-tool protocol error

#### Scenario: Cache state does not change the refusal
- **WHEN** a read-only grant calls `delete_note` once after a full list has been served and once immediately after a filtered list has been served
- **THEN** both responses SHALL be identical to each other and to the response before this change

#### Scenario: The precedence change is confined
- **WHEN** a read-only grant calls `write_file` or `delete_file` with a malformed `expected_hash`
- **THEN** the call SHALL be refused `permission_denied`, and the same call under a `readwrite` credential SHALL still receive the syntax refusal

#### Scenario: A filtered list does not degrade a write caller
- **WHEN** a filtered list is served and a `readwrite` principal then calls `edit_note`
- **THEN** the call SHALL run and its result SHALL be validated against `edit_note`'s `outputSchema` as before
