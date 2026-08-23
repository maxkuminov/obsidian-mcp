## ADDED Requirements

### Requirement: The truncation guidance SHALL name only registered tools
Every tool name that `read_note`'s truncation responses offer to the caller as a next step SHALL be a name a tool is registered under on the MCP server. This covers both producers of that guidance — the heading outline's omitted-sections summary and the `read_note` truncation notice — and it MUST be checked against the server's own tool registry rather than against a hand-maintained list, so a name that stops being registered is caught on the day it stops. The names written to `usage_logs` are governed separately and are not affected; a historical spelling retained for reading rows written before it was corrected is not agent-facing guidance.

**The check runs over the two producers' rendered output, and its extraction rule is fixed.** In a rendered guidance string, a *tool reference* is a backtick-delimited span whose content is either exactly an identifier matching `[A-Za-z_][A-Za-z0-9_]*`, or such an identifier immediately followed by `(`; the referenced name is that identifier. Any other span is not a tool reference — an ordinal (`` `#7` ``), an outline entry (`` `## Tasks` ``), a quoted argument (`` `section="#7"` ``). Each producer's rendered text SHALL yield a non-empty set of tool references, and every name in it SHALL appear in the registry.

Both halves are load-bearing, and the alternative shapes fail in opposite directions. A source-wide scan for backticked identifiers across the tool module cannot work: `list_files`'s own truncation line already emits a bare `` `pattern` ``, which is lexically identical to a bare `` `keyword_search` `` and is not a tool — and filtering the candidate set against the registry to suppress it would remove exactly the unregistered names the check exists to catch, leaving a check that passes over an empty set. Hence a fixed scope of two producers, plus the non-empty assertion: without it, a reformatting that drops the backticks turns the check into a no-op that still reports green.

Requiring the guidance to be emitted through a registry-validating helper was the other candidate and is not what this requires. It moves a copy concern into the runtime, is bypassed by the next f-string exactly as a scan is, and its validation fires when a note is truncated in production rather than in the test run.

#### Scenario: Truncated whole-note read offers a callable tool

- **WHEN** a whole-note read is truncated and the response suggests narrowing the request by search instead of reading the whole note
- **THEN** the suggested tool name SHALL be `keyword_search`
- **AND** SHALL NOT be `search_notes`

#### Scenario: Truncated outline offers a callable tool

- **WHEN** a heading outline is itself truncated and its omission summary suggests narrowing the request
- **THEN** the suggested tool name SHALL be `keyword_search`
- **AND** SHALL NOT be `search_notes`

#### Scenario: Each producer offers at least one name

- **WHEN** the outline's omitted-sections summary and the `read_note` truncation notice are rendered
- **THEN** each rendered text SHALL yield at least one tool reference under the extraction rule above
- **AND** a rendering that yields none SHALL fail the check, so it cannot pass over an empty candidate set

#### Scenario: Every extracted name is registered

- **WHEN** the tool references extracted from those two rendered texts are compared with the tool names the MCP server registry reports
- **THEN** every extracted name SHALL appear in that registry

#### Scenario: A reinstated `search_notes` fails the check

- **WHEN** either producer is changed to name `search_notes` again
- **THEN** `search_notes` SHALL be extracted as a tool reference and SHALL NOT appear in the registry
- **AND** the check SHALL fail, rather than the defect reaching a caller
