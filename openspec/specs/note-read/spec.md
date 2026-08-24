# note-read Specification

## Purpose
Bounded reading of markdown note content via `read_note`: the response-size cap that keeps a single read from exhausting the caller's context window, section addressing (exact heading text, `Parent/Child` path-style, and `#N` ordinal), offset/limit windowing, and the heading outline returned when a note is too large to return whole. Distinct from `file-access`, which covers raw byte transport for arbitrary vault files; the two share the `MAX_READ_RESPONSE_CHARS` cap because both feed tool output straight back into a model's context.
## Requirements
### Requirement: read_note tool

The MCP server SHALL expose a tool named `read_note` that returns the contents of a markdown note in the vault. The tool SHALL accept `path` (string, required), `section` (string, optional), `offset` (integer, optional, default 0), and `limit` (integer, optional).

The response SHALL include the note's title, vault-relative path, and — when present — its tags and frontmatter, followed by the selected note content.

#### Scenario: Tool is registered

- **WHEN** an MCP client lists available tools on the server
- **THEN** the listing SHALL contain `read_note`
- **AND** its input schema SHALL accept `path`, `section`, `offset`, and `limit`

#### Scenario: Missing note

- **WHEN** `read_note` is invoked on a path that does not exist
- **THEN** the tool SHALL return an error message identifying the path
- **AND** the tool SHALL NOT raise an unhandled exception

### Requirement: read_note response size cap

The `read_note` tool SHALL NOT return more than `MAX_READ_RESPONSE_CHARS` characters of note content in a single response. This cap governs what is returned to the caller and is independent of any cap governing how much the server reads from disk.

When the selected content exceeds the cap, the tool SHALL return the first window of content together with a truncation notice that states the character range shown, the total size of the selected content, and the exact `offset` value that continues the read. When a window reaches the end of the content, the notice SHALL NOT offer a continuation offset.

#### Scenario: Note within the cap

- **WHEN** `read_note` is invoked on a note whose content is at or below `MAX_READ_RESPONSE_CHARS`
- **THEN** the full content SHALL be returned
- **AND** the response SHALL NOT contain a truncation notice

#### Scenario: Note exceeds the cap

- **WHEN** `read_note` is invoked on a note whose content exceeds `MAX_READ_RESPONSE_CHARS`
- **THEN** the response SHALL contain at most `MAX_READ_RESPONSE_CHARS` characters of content
- **AND** the response SHALL state the range shown and the total size
- **AND** the response SHALL state the `offset` that continues the read

#### Scenario: Continuing a truncated read

- **WHEN** `read_note` is reissued with the `offset` reported by a previous truncation notice
- **THEN** the returned window SHALL begin exactly where the previous window ended, with no gap and no overlap

#### Scenario: Final window

- **WHEN** a windowed read reaches the end of the selected content
- **THEN** the response SHALL NOT offer a further continuation offset

#### Scenario: Offset beyond the content

- **WHEN** `read_note` is invoked with an `offset` strictly greater than the length of the selected content
- **THEN** the tool SHALL return a message reporting the offset and the total size
- **AND** the tool SHALL NOT return an empty response that could be mistaken for an empty note

#### Scenario: Offset exactly at the end of the content

- **WHEN** `read_note` is invoked with an `offset` exactly equal to the length of the selected content
- **THEN** the tool SHALL report that the end has been reached and nothing further remains
- **AND** SHALL distinguish this from an offset that is past the end, which is a caller error

#### Scenario: Invalid offset or limit

- **WHEN** `read_note` is invoked with a negative `offset` or a `limit` below 1
- **THEN** the tool SHALL return an error naming the offending value
- **AND** SHALL NOT return note content

### Requirement: limit may lower the response cap but not raise it

The `limit` parameter SHALL reduce the number of characters returned below `MAX_READ_RESPONSE_CHARS`. A `limit` greater than `MAX_READ_RESPONSE_CHARS` SHALL NOT increase the amount returned; the configured cap SHALL still apply.

#### Scenario: Limit below the cap

- **WHEN** `read_note` is invoked with a `limit` lower than `MAX_READ_RESPONSE_CHARS`
- **THEN** at most `limit` characters of content SHALL be returned

#### Scenario: Limit above the cap

- **WHEN** `read_note` is invoked with a `limit` greater than `MAX_READ_RESPONSE_CHARS`
- **THEN** at most `MAX_READ_RESPONSE_CHARS` characters of content SHALL be returned

### Requirement: Section selection in read_note

When `section` is supplied, `read_note` SHALL return only the named section — the matched ATX heading line together with the content up to the next heading of equal-or-shallower depth, or end of note. The selector SHALL accept the same forms as the write tools: exact heading text, the path-style `Parent/Child` form, and the `#N` ordinal form.

A section response SHALL be subject to the same response-size cap, and `offset` SHALL window within the selected section rather than within the whole note.

#### Scenario: Reading one section of a large note

- **WHEN** `read_note` is invoked with a `section` that is within the cap, on a note that is not
- **THEN** the response SHALL contain that section's heading and body
- **AND** SHALL NOT contain content from other sections
- **AND** SHALL NOT be truncated

#### Scenario: Section larger than the cap

- **WHEN** the selected section itself exceeds `MAX_READ_RESPONSE_CHARS`
- **THEN** the response SHALL be truncated with a notice identifying the section and the continuing `offset`
- **AND** the continuation SHALL preserve the `section` selection

#### Scenario: Unknown section

- **WHEN** `read_note` is invoked with a `section` that matches no heading
- **THEN** the response SHALL list the headings that are present
- **AND** SHALL NOT return note content

### Requirement: Heading outline on a truncated whole-note read

When a whole-note read is truncated and the note contains ATX headings, the response SHALL include an outline of the note's sections. Each entry SHALL carry the section's `#N` ordinal, heading depth, heading text, and size in characters, and SHALL indicate when a section itself exceeds the cap. The response SHALL tell the caller how to read a listed section directly.

The outline SHALL NOT be included when a `section` was explicitly selected, since the caller has already chosen.

The outline SHALL itself be bounded by `MAX_READ_RESPONSE_CHARS`. It is appended to a response that exists because the content was too large, so an unbounded outline would reintroduce the failure this capability prevents: a note with very many headings can otherwise produce an outline far larger than the content window it accompanies. Overlong headings SHALL be elided, and when the listing does not fit, it SHALL stop and report how many sections were omitted along with the full ordinal range. When the complete listing fits within the cap it SHALL be emitted in full, with no omission summary — no room SHALL be reserved for a summary that is not needed, since that would drop entries the budget could afford. At least one entry SHALL be emitted whenever one fits; when the cap is too small for even a single entry or the summary itself, the outline SHALL degrade to a truncated marker rather than exceed the cap. The cap is the binding constraint: there is no output the outline may exceed it to produce.

#### Scenario: Truncated note with headings

- **WHEN** a whole-note read is truncated on a note containing ATX headings
- **THEN** the response SHALL list each section with its ordinal, title, and size
- **AND** SHALL indicate which sections exceed the cap
- **AND** SHALL show how to request a section directly

#### Scenario: Truncated note without headings

- **WHEN** a whole-note read is truncated on a note containing no ATX headings
- **THEN** the response SHALL still be truncated with a continuation offset
- **AND** SHALL suggest narrowing the request by search rather than offering an outline

#### Scenario: Outline of a heading-heavy note stays bounded

- **WHEN** a truncated note contains so many headings that a full listing would exceed `MAX_READ_RESPONSE_CHARS`
- **THEN** the outline SHALL be truncated to stay within the cap
- **AND** SHALL report the number of sections omitted and the full ordinal range
- **AND** the total response SHALL remain proportionate to the cap

#### Scenario: A listing that fits is emitted in full

- **WHEN** the complete section listing fits within `MAX_READ_RESPONSE_CHARS`
- **THEN** every section SHALL be listed
- **AND** the response SHALL NOT contain an omission summary

#### Scenario: Cap too small for any entry

- **WHEN** the effective cap cannot accommodate even one outline entry
- **THEN** the outline SHALL be truncated to the cap
- **AND** SHALL NOT exceed it in order to satisfy the at-least-one-entry preference

#### Scenario: Overlong heading text is elided

- **WHEN** a section's heading text is longer than the outline's per-title limit
- **THEN** the entry SHALL show an elided title rather than the full text

#### Scenario: Truncated section read omits the outline

- **WHEN** a read with an explicit `section` is truncated
- **THEN** the response SHALL NOT list the note's other sections

### Requirement: Duplicate headings are flagged in the outline

When two or more sections in an outline share the same heading text, the outline SHALL mark them as duplicates and direct the caller to the ordinal form, which is the only selector that separates them.

#### Scenario: Note with repeated section titles

- **WHEN** a truncated note contains more than one section with identical heading text
- **THEN** each such entry SHALL be marked as a duplicate title
- **AND** the response SHALL direct the caller to use the ordinal

### Requirement: Configurable read response limit

The server configuration SHALL expose `MAX_READ_RESPONSE_CHARS` (default 40,000) as a setting loadable from the environment, governing the response size of `read_note` and the text results of `read_file`. It SHALL be validated as at least 1,000.

#### Scenario: Default applies when unset

- **WHEN** `MAX_READ_RESPONSE_CHARS` is not set in the environment
- **THEN** the cap SHALL default to 40,000 characters

#### Scenario: Override is honored

- **WHEN** `MAX_READ_RESPONSE_CHARS` is set in the environment
- **THEN** `read_note` and `read_file` SHALL enforce the configured value

### Requirement: The truncation guidance SHALL name only registered tools
Every tool name that `read_note`'s truncation responses offer to the caller as a next step SHALL be a name a tool is registered under on the MCP server. This covers both producers of that guidance — the heading outline's omitted-sections summary and the `read_note` truncation notice — and it MUST be checked against the server's own tool registry rather than against a hand-maintained list, so a name that stops being registered is caught on the day it stops. The names written to `usage_logs` are governed separately and are not affected; a historical spelling retained for reading rows written before it was corrected is not agent-facing guidance.

**The check runs over the two producers' rendered output, and its extraction rule is fixed.** In a rendered guidance string, a *tool reference* is a backtick-delimited span whose content is either exactly an identifier matching `[A-Za-z_][A-Za-z0-9_]*`, or such an identifier immediately followed by `(`; the referenced name is that identifier. Any other span is not a tool reference — an ordinal (`` `#7` ``), an outline entry (`` `## Tasks` ``), a quoted argument (`` `section="#7"` ``). Each producer's rendered text SHALL yield a non-empty set of tool references, and every name in it SHALL appear in the registry.

**Non-empty and registered is not enough: the guidance SHALL name the search tool.** For each producer, the extracted set of the clause carrying that producer's narrowing guidance SHALL contain `keyword_search`. This is membership, not equality — a further registered tool reference added beside it is permitted, so the requirement does not freeze the copy.

That last assertion is an altitude correction, and is recorded as one. The two assertions above it encode a general property — no agent-facing string names an unregistered tool — which is too weak to express what is actually wanted here: that *this* guidance names *the search tool*. Under the general property alone, rewriting the summary to narrow with `delete_note` passes every check while pointing the agent at a destructive tool, and adding a second registered reference beside `keyword_search` lets its backticks be dropped with the set still non-empty and fully registered. Three review rounds of the check passed vacuously on that gap before it was closed. The registry comparison stays as the broad backstop; the membership assertion is what pins the specific claim.

**Both halves of the registry check** — a non-empty set, and every name in it registered — are load-bearing, and the alternative shapes fail in opposite directions. A source-wide scan for backticked identifiers across the tool module cannot work: `list_files`'s own truncation line already emits a bare `` `pattern` ``, which is lexically identical to a bare `` `keyword_search` `` and is not a tool — and filtering the candidate set against the registry to suppress it would remove exactly the unregistered names the check exists to catch, leaving a check that passes over an empty set. Hence a fixed scope of two producers, plus the non-empty assertion: without it, a reformatting that drops the backticks turns the check into a no-op that still reports green.

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

#### Scenario: The narrowing guidance names the search tool

- **WHEN** the clause carrying each producer's narrowing guidance is rendered and its tool references extracted
- **THEN** that clause's extracted set SHALL contain `keyword_search`
- **AND** SHALL NOT be required to contain it alone, so a further registered tool reference beside it is permitted

#### Scenario: A registered but wrong replacement fails the check

- **WHEN** either producer's guidance is changed to narrow with a different registered tool, such as `delete_note`, instead of `keyword_search`
- **THEN** that clause's extracted set SHALL still be non-empty and every name in it SHALL still appear in the registry
- **AND** the check SHALL nevertheless fail, because `keyword_search` is absent from it

#### Scenario: A second reference does not mask a dropped name

- **WHEN** a second registered tool reference is added to a producer's guidance clause and `keyword_search`'s backticks are then removed
- **THEN** that clause's extracted set SHALL still be non-empty and fully registered
- **AND** the check SHALL fail, because `keyword_search` is no longer a member of it

#### Scenario: A reinstated `search_notes` fails the check

- **WHEN** either producer is changed to name `search_notes` again
- **THEN** `search_notes` SHALL be extracted as a tool reference and SHALL NOT appear in the registry
- **AND** the check SHALL fail, rather than the defect reaching a caller

