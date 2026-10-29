## ADDED Requirements

### Requirement: Every registered tool SHALL publish a title and a complete set of behaviour annotations
Every tool the server lists SHALL carry a non-empty `title` and a `ToolAnnotations` whose `readOnlyHint`, `destructiveHint`, `idempotentHint` and `openWorldHint` are each an explicit boolean, never absent, so that no client falls back to the protocol's defaults (`readOnlyHint` false, `destructiveHint` true, `openWorldHint` true). The hints SHALL be set through the public registration API (`FastMCP.tool(title=…, annotations=…)`) on each registration. The classification SHALL be: every tool that neither changes vault bytes nor mints a capability is `readOnlyHint=true, destructiveHint=false, idempotentHint=true, openWorldHint=false`; the set of `destructiveHint=true` tools is exactly the write-bucket set (the tools registered with `_tracked(write_class=True)`) minus `create_note`; the set of `readOnlyHint=false` tools is exactly the write-bucket set plus `request_upload` and `request_download`; `openWorldHint=true` is exactly `import_from_url`; and `idempotentHint=true` is exactly the read-only set. An undo path through `.trash/` SHALL NOT make a deleting tool non-destructive. Annotations are a client-side hint: no server-side gate SHALL consult them, and read-only enforcement SHALL remain `_tracked`'s admission and `_require_write`'s permission check, unchanged.

#### Scenario: Every listed tool carries complete metadata
- **WHEN** a client lists tools
- **THEN** each of the 25 tools SHALL carry a non-empty `title`, distinct from every other tool's
- **AND** each SHALL carry `annotations` with `readOnlyHint`, `destructiveHint`, `idempotentHint` and `openWorldHint` each `true` or `false`, none absent

#### Scenario: The read tools are marked read-only
- **WHEN** a client lists tools
- **THEN** `keyword_search`, `semantic_search`, `read_note`, `list_notes`, `get_tags`, `get_recent`, `get_vault_guide`, `read_file`, `list_files`, `get_backlinks`, `get_links`, `get_neighborhood`, `find_related`, `find_orphans` and `check_upload` SHALL each carry `readOnlyHint=true`, `destructiveHint=false`, `idempotentHint=true`, `openWorldHint=false`
- **AND** no other tool SHALL carry `readOnlyHint=true`

#### Scenario: The destructive set is the write bucket minus create_note
- **WHEN** the set of tools carrying `destructiveHint=true` is compared with the set of tools registered with `_tracked(write_class=True)`, read from the code rather than from a list kept in the test
- **THEN** the destructive set SHALL equal the write-bucket set with `create_note` removed — `edit_note`, `move_note`, `delete_note`, `set_frontmatter`, `write_file`, `delete_file`, `import_from_url`
- **AND** the write-bucket set SHALL equal the `write` class of `concurrency.TOOL_CLASSES`
- **AND** `create_note`, `request_upload` and `request_download` SHALL carry `readOnlyHint=false` and `destructiveHint=false`

#### Scenario: Only import_from_url is open-world
- **WHEN** a client lists tools
- **THEN** `import_from_url` SHALL carry `openWorldHint=true` and `destructiveHint=true`
- **AND** every other tool SHALL carry `openWorldHint=false`

#### Scenario: A hint gates nothing
- **WHEN** a credential with read-only permission calls `edit_note`, `create_note` or `request_upload`
- **THEN** the call SHALL be refused with `permission_denied` by `_require_write`, exactly as before this change
- **AND** no code path that admits or refuses a tool call SHALL read the tool's `annotations`

### Requirement: The server SHALL publish bounded instructions whose lead states the contract
The server SHALL be constructed with `instructions` taken from one module-level constant of at most 2,000 characters. Its first 512 characters SHALL state that the server is agent memory in a markdown vault; SHALL name `keyword_search` and `semantic_search` for search, `read_note` for reading, and `create_note` and `edit_note` (with `dry_run` first) for writing; SHALL describe `get_vault_guide` as the once-per-session orientation in neutral framing; and SHALL state that writes refuse to clobber and honour `expected_hash`. Every sentence of the instructions SHALL be true of the server as deployed, and every tool-shaped backticked identifier in it — a span in call form (`name(`), or a bare identifier containing an underscore whose first `_`-delimited word is the first word of a registered tool's name — SHALL be a registered tool. Nothing in the server SHALL depend on a client having read the instructions.

#### Scenario: The instructions fit both limits
- **WHEN** the server's `instructions` are read from the constructed `FastMCP` instance
- **THEN** they SHALL be identical to the module-level constant
- **AND** their length SHALL be at most 2,000 characters

#### Scenario: The lead carries the contract
- **WHEN** the first 512 characters of the instructions are inspected
- **THEN** they SHALL mention memory and markdown, `keyword_search`, `semantic_search`, `read_note`, `create_note`, `edit_note`, `dry_run`, `get_vault_guide`, `expected_hash`, and that writes refuse to clobber

#### Scenario: Only registered tools are named
- **WHEN** every tool-shaped backticked identifier in the instructions is extracted — call form, or a bare identifier containing an underscore whose first `_`-delimited word is the first word of a registered tool's name, the word set derived from the registry at test time
- **THEN** each one SHALL be registered on the server
- **AND** a backticked argument or field name such as `content`, `expected_hash` or `find` SHALL NOT be counted as a tool reference

### Requirement: No published tool description SHALL exceed 4,000 characters, and the restructured descriptions SHALL lead with their contract
Every tool's published description — the string the registry holds, which is the registered function's raw docstring — SHALL be at most 4,000 characters. For `edit_note`, `move_note` and `set_frontmatter`, the first 1,500 characters SHALL name every declared argument, the `readwrite` permission precondition, `expected_hash`, and the `MCP-REFUSAL` line; for `read_note`, the first 1,500 characters SHALL name every declared argument and the `content`, `heading`, `content_hash` and `error` fields. Shortening a description SHALL NOT change any argument name, default or described behaviour, and SHALL NOT remove any statement another requirement in `openspec/specs/` makes about that description; narrative removed from a description SHALL be present in `docs/architecture/vault-tools.md` or `README.md`. The `tools.py` implementation docstrings are not published and are not bounded by this requirement.

#### Scenario: Every description is under the cap
- **WHEN** a client lists tools
- **THEN** every tool's `description` SHALL be at most 4,000 characters long

#### Scenario: The write tools lead with their contract
- **WHEN** the first 1,500 characters of the `edit_note`, `move_note` or `set_frontmatter` description are inspected
- **THEN** they SHALL contain the name of every property in that tool's `inputSchema`
- **AND** SHALL contain `readwrite`, `expected_hash` and `MCP-REFUSAL`

#### Scenario: read_note leads with its fields
- **WHEN** the first 1,500 characters of the `read_note` description are inspected
- **THEN** they SHALL contain the name of every property in its `inputSchema`
- **AND** SHALL contain `` `content` ``, `` `heading` ``, `` `content_hash` `` and `` `error` ``

#### Scenario: Pinned statements survive the shortening
- **WHEN** the existing description tests run unmodified — `tests/test_issue_150_docstrings.py`, `tests/test_issue_149_read_note_framing.py`, `tests/test_issue_128_edit_note_frontmatter.py`, `tests/test_issue_89_tool_names_in_copy.py`
- **THEN** every one of them SHALL pass

#### Scenario: A moved sentence leaves no dangling name
- **WHEN** every tool-shaped backticked identifier in every published description is extracted under the same rule as the instructions scenario above
- **THEN** each one SHALL be registered on the server

### Requirement: Tool metadata SHALL leave the published tool surface otherwise unchanged
Adding titles, annotations and instructions, and shortening descriptions, SHALL NOT change the set of tool names, their listing order, any tool's `inputSchema`, or any tool's `outputSchema`. The listing SHALL be compared against a fixture generated from the registry on `main` at 5a391ca, so that a schema change is a deliberate regeneration of the fixture and never a side effect.

#### Scenario: The same 25 tools in the same order
- **WHEN** a client lists tools
- **THEN** the sequence of names SHALL equal the sequence recorded in `tests/fixtures/tools_list_schema_5a391ca.json`

#### Scenario: Every schema is identical to the fixture
- **WHEN** each tool's `inputSchema` and `outputSchema` are compared with the fixture entry of the same name
- **THEN** each SHALL be equal as a JSON value
- **AND** `read_note` SHALL remain the only tool with an `outputSchema`

#### Scenario: Unknown-argument refusal is unaffected
- **WHEN** `tests/test_issue_295_unknown_arguments.py` runs unmodified
- **THEN** it SHALL pass, and every `inputSchema` SHALL still carry `additionalProperties: false`
