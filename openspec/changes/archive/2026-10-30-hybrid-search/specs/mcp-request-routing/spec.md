## MODIFIED Requirements

### Requirement: Every registered tool SHALL publish a title and a complete set of behaviour annotations
Every tool the server lists SHALL carry a non-empty `title` and a `ToolAnnotations` whose `readOnlyHint`, `destructiveHint`, `idempotentHint` and `openWorldHint` are each an explicit boolean, never absent, so that no client falls back to the protocol's defaults (`readOnlyHint` false, `destructiveHint` true, `openWorldHint` true). The hints SHALL be set through the public registration API (`FastMCP.tool(title=…, annotations=…)`) on each registration. The classification SHALL be: every tool that neither changes vault bytes nor mints a capability is `readOnlyHint=true, destructiveHint=false, idempotentHint=true, openWorldHint=false`; the set of `destructiveHint=true` tools is exactly the write-bucket set (the production tools registered with `_tracked(write_class=True)`) minus `create_note` plus `request_upload` (whose `overwrite=True` capability overwrites on redemption — owner decision, 2026-10-28); the set of `readOnlyHint=false` tools is exactly the write-bucket set plus `request_upload` and `request_download`; `openWorldHint=true` is exactly `import_from_url`; and `idempotentHint=true` is exactly the read-only set. The write-bucket set used for these comparisons SHALL be derived from the production tools only, so that a `_tracked(write_class=True)` decoration performed by a test module cannot change it. An undo path through `.trash/` SHALL NOT make a deleting tool non-destructive. Annotations are a client-side hint: no server-side gate SHALL consult them, and read-only enforcement SHALL remain `_tracked`'s admission and `_require_write`'s permission check, unchanged.

#### Scenario: Every listed tool carries complete metadata
- **WHEN** a client lists tools
- **THEN** each of the 28 tools SHALL carry a non-empty `title`, distinct from every other tool's
- **AND** each SHALL carry `annotations` with `readOnlyHint`, `destructiveHint`, `idempotentHint` and `openWorldHint` each `true` or `false`, none absent

#### Scenario: The read tools are marked read-only
- **WHEN** a client lists tools
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
- **WHEN** a client lists tools
- **THEN** `import_from_url` SHALL carry `openWorldHint=true` and `destructiveHint=true`
- **AND** every other tool SHALL carry `openWorldHint=false`

#### Scenario: A hint gates nothing
- **WHEN** a credential with read-only permission calls `edit_note`, `create_note` or `request_upload`
- **THEN** the call SHALL be refused with `permission_denied` by `_require_write`, exactly as before this change
- **AND** no code path that admits or refuses a tool call SHALL read the tool's `annotations`

### Requirement: The server SHALL publish bounded instructions whose lead states the contract
The server SHALL be constructed with `instructions` taken from one module-level constant of at most 2,000 characters. Its first 512 characters SHALL state that the server is agent memory in a markdown vault; SHALL name `keyword_search`, `semantic_search` and `hybrid_search` for search as peers, each with its one-word use (identifiers, paraphrase, unsure), ranking none above another, `read_note` for reading, and `create_note` and `edit_note` (with `dry_run` first) for writing; SHALL describe `get_vault_guide` as the once-per-session orientation in neutral framing; SHALL state that `create_note` never overwrites; and SHALL describe `expected_hash` as optional, caller-supplied stale-read protection — a guarded edit is refused if the file changed, an unguarded edit overwrites — without implying that an unguarded edit refuses a stale overwrite. The instructions SHALL state the read-to-write round-trip rule with its conditions: only `content` from a complete, unwindowed whole-note read (`offset=0` and `truncated=false`) may be passed back as a full replacement; a section's `content` is written back only with the same section selector; a truncated read must be completed first. Every sentence of the instructions SHALL be true of the server as deployed, and every tool-shaped backticked identifier in it — a span in call form (`name(`), a dotted span (`name.field`) whose head is tool-shaped, or a bare identifier containing an underscore whose first `_`-delimited word is the first word of a registered tool's name — SHALL name a registered tool. Nothing in the server SHALL depend on a client having read the instructions.

#### Scenario: The instructions fit both limits
- **WHEN** the server's `instructions` are read from the constructed `FastMCP` instance
- **THEN** they SHALL be identical to the module-level constant
- **AND** their length SHALL be at most 2,000 characters

#### Scenario: The lead carries the contract
- **WHEN** the first 512 characters of the instructions are inspected
- **THEN** they SHALL mention memory and markdown, `keyword_search`, `semantic_search`, `hybrid_search`, `read_note`, `create_note`, `edit_note`, `dry_run`, `get_vault_guide`, `expected_hash`, that `expected_hash` is optional, and that `create_note` never overwrites

#### Scenario: The round-trip rule is qualified
- **WHEN** the instructions describe passing a `read_note` result back to `edit_note`
- **THEN** they SHALL state the whole-note condition (`offset=0`, `truncated=false`), the same-section condition for a section's `content`, and that a truncated read must be completed first
- **AND** they SHALL NOT contain an unqualified instruction to pass `content` back unchanged

#### Scenario: Only registered tools are named
- **WHEN** every tool-shaped backticked identifier in the instructions is extracted — call form, a dotted span whose head is tool-shaped, or a bare identifier containing an underscore whose first `_`-delimited word is the first word of a registered tool's name, the word set derived from the registry at test time
- **THEN** each one (the head, for a dotted span) SHALL be registered on the server
- **AND** a backticked argument or field name such as `content`, `expected_hash` or `find` SHALL NOT be counted as a tool reference

#### Scenario: The lead names the three search peers within the cap
- **WHEN** the first paragraph of the instructions is measured after the `hybrid-search` change (#354)
- **THEN** it SHALL be at most 512 characters, SHALL name `keyword_search`, `semantic_search` and `hybrid_search` in one clause, and SHALL NOT contain "primary" or "default"
- **AND** the whole instructions SHALL be at most 2,000 characters and SHALL still state `content_hash` and `precondition_required` outside the lead

### Requirement: Tool metadata SHALL leave the published tool surface otherwise unchanged
Adding titles, annotations and instructions, and shortening descriptions, SHALL NOT change the set of tool names, their listing order, any tool's `inputSchema`, or any tool's `outputSchema`. The first 25 entries of the listing SHALL be compared against the frozen fixture generated from the registry on `main` at 5a391ca, and every entry after them SHALL be compared, in order, against `tests/fixtures/tools_list_schema_additions.json`, which a tool-adding change regenerates deliberately; `list_tasks` (#350) is its first entry, `get_changes` (#351) its second and `hybrid_search` (#354) its third, so the listing holds 28 tools. A deliberate schema change to one of the first 25 tools SHALL be recorded as a full replacement entry in `tests/fixtures/tools_list_schema_amendments.json`, and that entry SHALL be compared instead of the frozen one; the frozen fixture itself SHALL NOT be edited. The change-feed change (#351) records exactly two amendments, `list_notes` and `get_recent`, each differing from its frozen entry only by the optional `since`, `until`, `order_by` and `cursor` input properties. The `hybrid-search` change (#354) adds no amendment: the three search tools' descriptions change and their schemas do not. A schema change to an existing tool SHALL be a deliberate fixture regeneration and never a side effect.

#### Scenario: The same 25 tools in the same order, then the additions
- **WHEN** a client lists tools
- **THEN** the first 25 names SHALL equal the sequence recorded in `tests/fixtures/tools_list_schema_5a391ca.json`
- **AND** the remaining names SHALL equal the sequence recorded in `tests/fixtures/tools_list_schema_additions.json`, which after this change is exactly `list_tasks`, `get_changes`, `hybrid_search`, for 28 tools in all

#### Scenario: Every schema is identical to the fixture
- **WHEN** each tool's `inputSchema` and `outputSchema` are compared with the entry of the same name in the amendments fixture if it holds one, and otherwise in whichever of the two other fixtures holds it
- **THEN** each SHALL be equal as a JSON value
- **AND** a test SHALL assert that each amendment differs from its frozen entry only by the four optional properties, none of them listed in `required`, and that the amendments fixture names exactly `get_recent` and `list_notes`
- **AND** the set of tools carrying an `outputSchema` SHALL be unchanged apart from the additions (`list_tasks`, `get_changes` and `hybrid_search` return `str` and carry the SDK's `{"result": string}` wrapper) — on 5a391ca every tool but `read_file` carries one (the SDK wraps a `str` return as `{"result": string}`; `read_note` publishes its model's schema; `read_file` returns content blocks and has none)

#### Scenario: Unknown-argument refusal is unaffected
- **WHEN** `tests/test_issue_295_unknown_arguments.py` runs with only its tool count moved from 27 to 28
- **THEN** it SHALL pass, and every `inputSchema`, `get_changes`'s and `hybrid_search`'s included, SHALL carry `additionalProperties: false`
