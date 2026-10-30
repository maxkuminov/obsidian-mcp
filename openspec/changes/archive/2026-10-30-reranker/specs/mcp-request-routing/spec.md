## MODIFIED Requirements

### Requirement: Tool metadata SHALL leave the published tool surface otherwise unchanged
Adding titles, annotations and instructions, and shortening descriptions, SHALL NOT change the set of tool names, their listing order, any tool's `inputSchema`, or any tool's `outputSchema`. The first 25 entries of the listing SHALL be compared against the frozen fixture generated from the registry on `main` at 5a391ca, and every entry after them SHALL be compared, in order, against `tests/fixtures/tools_list_schema_additions.json`, which a tool-adding change regenerates deliberately; `list_tasks` (#350) is its first entry, `get_changes` (#351) its second and `hybrid_search` (#354) its third, so the listing holds 28 tools. A deliberate schema change to one of the first 25 tools SHALL be recorded as a full replacement entry in `tests/fixtures/tools_list_schema_amendments.json`, and that entry SHALL be compared instead of the frozen one; the frozen fixture itself SHALL NOT be edited. The change-feed change (#351) records two amendments, `list_notes` and `get_recent`, each differing from its frozen entry only by the optional `since`, `until`, `order_by` and `cursor` input properties. The `hybrid-search` change (#354) adds no amendment: the three search tools' descriptions change and their schemas do not. The reranker change (#367) records a third amendment, `semantic_search`, differing from its frozen entry only by the optional boolean `rerank` input property (default false), and regenerates the `hybrid_search` additions entry, which then differs from its #354 form only by the same optional `rerank` property. A schema change to an existing tool SHALL be a deliberate fixture regeneration and never a side effect.

#### Scenario: The same 25 tools in the same order, then the additions
- **WHEN** a client lists tools
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
