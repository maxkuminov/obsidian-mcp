# note-read Specification

## Purpose
Bounded reading of markdown note content via `read_note`: the response-size cap that keeps a single read from exhausting the caller's context window, section addressing (exact heading text, `Parent/Child` path-style, and `#N` ordinal), offset/limit windowing, and the heading outline returned when a note is too large to return whole. Distinct from `file-access`, which covers raw byte transport for arbitrary vault files; the two share the `MAX_READ_RESPONSE_CHARS` cap because both feed tool output straight back into a model's context.
## Requirements
### Requirement: read_note tool

The MCP server SHALL expose a tool named `read_note` that returns the contents of a markdown note in the vault. The tool SHALL accept `path` (string, required), `section` (string, optional), `offset` (integer, optional, default 0), and `limit` (integer, optional).

The response SHALL be the structured result defined by the "read_note responses are structurally framed" requirement, carrying the note's title, vault-relative path, and — when present — its tags and frontmatter as discrete fields, and the selected note content in the `content` field. A whole-note read's `content` SHALL be the note body with a valid frontmatter block stripped — exactly what `edit_note(path, content)` full replacement accepts — and only a complete, unwindowed whole-note `content` (offset 0, `truncated` false) round-trips that way. Truncation SHALL be reported as data: `truncated`, the window's `offset`, the `next_offset` that continues the read (absent at the end), and the total size of the selected content; the existing response-size-cap, `limit`, and outline requirements apply to these fields' values unchanged, with "the response's content" meaning the `content` field and "the truncation notice" meaning the truncation fields together with the server-authored `notice` field.

#### Scenario: Tool is registered with an output schema

- **WHEN** an MCP client lists available tools on the server
- **THEN** the listing SHALL contain `read_note`
- **AND** its input schema SHALL accept `path`, `section`, `offset`, and `limit`
- **AND** it SHALL declare an output schema matching the structured result

#### Scenario: Missing note

- **WHEN** `read_note` is invoked on a path that does not exist
- **THEN** the tool SHALL return an in-band error identifying the path
- **AND** the tool SHALL NOT raise an unhandled exception

#### Scenario: Admission refusals use the same structure

- **WHEN** the tool-level admission gate refuses the call before the tool body runs (no vault assignment, for example)
- **THEN** the refusal SHALL be delivered as the same structured result with `error` set — never as a bare string that fails output-schema validation and surfaces as a protocol error

#### Scenario: Empty selected content is a successful read

- **WHEN** the selected note or section body is empty and `offset` is 0
- **THEN** the response SHALL be a successful read with empty `content` — the offset-at-end error applies only to continuation offsets greater than zero

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

When `section` is supplied, `read_note` SHALL return only the named section: the matched ATX heading line in the `heading` field (without its line terminator), and in `content` the section's **body only** — exactly the span `edit_note(section=…)` replaces, nothing more and nothing less, so the section read's `content` is byte-exact input for the section write. The selector SHALL accept the same forms as the write tools: exact heading text, the path-style `Parent/Child` form, and the `#N` ordinal form.

A section response SHALL be subject to the same response-size cap, and `offset` SHALL window within the selected body rather than within the whole note.

The parity claim is scoped to notes for which section-mode writing is **admitted**. A note whose line-1 frontmatter is defective (unclosed fence, YAML error, or non-mapping) stays readable by section — the read scans its raw bytes — while every section write to it is refused by name, per the vault-write requirement this change does not relax. On such a note the guarantee is the refusal, not the round trip: it is the safe asymmetry, and widening parity to cover it would mean scanning a broken block for headings on the write side, which is the destructive behaviour #128 removed.

Whitespace, blank lines, and fenced code blocks (as recognised by the shared code masker) between the heading line and the next heading of equal-or-shallower depth are part of the body on both sides. The agreement SHALL remain verifiable directly against the shared section helpers, which operate on note text; with structural framing, it is additionally verifiable against the response, because `content` **is** the body.

No docstring SHALL instruct a caller to recover a section body by textual manipulation of a rendered response; the structured `content` field is the recovery. The historical reason (every textual procedure was forgeable by note content) SHALL remain recorded where a future author will find it.

#### Scenario: Reading one section of a large note

- **WHEN** `read_note` is invoked with a `section` that is within the cap, on a note that is not
- **THEN** the `heading` field SHALL carry that section's heading line and `content` its body
- **AND** `content` SHALL NOT contain content from other sections
- **AND** the response SHALL NOT be truncated

#### Scenario: A section read returns nothing a section write cannot replace

- **WHEN** a section is selected whose body begins with a blank line, or with a fenced code block, or both
- **THEN** every byte of `content` SHALL fall inside the span `edit_note(path, content, section=<sel>)` replaces
- **AND** passing `content` back unchanged SHALL reproduce an LF-bodied note exactly

#### Scenario: Docstrings teach the field round trip

- **WHEN** the `read_note` or `edit_note` documentation describes how a section read relates to a section write
- **THEN** it SHALL say that a section read's `content` field is the body `edit_note(section=…)` accepts, and SHALL NOT describe splitting any rendered text

#### Scenario: A CRLF note's section round trip is not byte-identical

- **WHEN** a note's raw bytes are `# A\r\nold\r\n# B\r\nkeep\r\n` and its first section's `content` is read and written back unchanged
- **THEN** the round trip SHALL preserve the section's *content*, and the resulting note SHALL be `# A\r\nold\n# B\r\nkeep\r\n` — the selected body's terminators become LF because the read path normalises and the write path works on raw bytes
- **AND** this residual SHALL be declared in both docstrings rather than claimed as byte-identity, which holds only for LF-bodied notes

#### Scenario: Section larger than the cap

- **WHEN** the selected section itself exceeds `MAX_READ_RESPONSE_CHARS`
- **THEN** the response SHALL be truncated, with the truncation fields identifying the continuing `offset`
- **AND** the continuation SHALL preserve the `section` selection
- **AND** the round-trip guarantee SHALL NOT apply to such a response, which is a window rather than a whole section

#### Scenario: Unknown section

- **WHEN** `read_note` is invoked with a `section` that matches no heading
- **THEN** the in-band error SHALL list the headings that are present
- **AND** no `content` SHALL be returned

### Requirement: Heading outline on a truncated whole-note read

When a whole-note read is truncated and the note contains ATX headings, the response SHALL include an outline object carrying: `entries` (one per listed section: `#N` ordinal, heading depth, heading text, size in characters, whether it exceeds the cap, and whether its title duplicates another's), and — when the listing is incomplete — the omission state as data: the count of sections omitted and the full ordinal range. When the budget cannot accommodate even one entry, the outline SHALL degrade to its explicit truncation marker as a field, not by exceeding the budget. `size` and the exceeds-cap flag keep their existing measure (heading line plus body, per the existing outline helper); this is conservative for body-only section reads — a section whose heading-plus-body fits the cap always has a body that fits. The response's `notice` field SHALL tell the caller how to read a listed section directly.

The outline SHALL NOT be included when a `section` was explicitly selected, since the caller has already chosen.

The outline SHALL itself be bounded by `MAX_READ_RESPONSE_CHARS`, measured over its serialized entries. It accompanies a response that exists because the content was too large, so an unbounded outline would reintroduce the failure this capability prevents. Overlong headings SHALL be elided, and when the listing does not fit, it SHALL stop and report how many sections were omitted along with the full ordinal range. When the complete listing fits within the cap it SHALL be emitted in full, with no omission summary — no room SHALL be reserved for a summary that is not needed, since that would drop entries the budget could afford. At least one entry SHALL be emitted whenever one fits, even when an earlier, longer entry does not; when the cap is too small for even a single entry or the summary itself, the outline SHALL degrade to an explicit truncation marker rather than exceed the cap, and when even that marker cannot fit, the outline SHALL be omitted entirely. The cap is the binding constraint: there is no output the outline may exceed it to produce.

#### Scenario: Truncated note with headings

- **WHEN** a whole-note read is truncated on a note containing ATX headings
- **THEN** every outline entry emitted SHALL carry its section's ordinal, title, and size, and SHALL indicate when the section exceeds the cap; when the complete listing fits its budget every section SHALL be listed, and otherwise the omission state (count and full ordinal range) SHALL report the sections not listed
- **AND** the `notice` SHALL show how to request a section directly

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
- **AND** the outline SHALL NOT contain an omission summary

#### Scenario: Cap too small for any entry

- **WHEN** the effective cap cannot accommodate even one outline entry
- **THEN** the outline SHALL degrade to its truncation marker within the cap
- **AND** SHALL NOT exceed the cap in order to satisfy the at-least-one-entry preference

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
Every tool name that `read_note`'s truncation responses offer to the caller as a next step SHALL be a name a tool is registered under on the MCP server. Since the outline became a data object with no prose (read-note-framing-149), the single producer of that guidance is the `notice` field of the structured result, and it MUST be checked against the server's own tool registry rather than against a hand-maintained list, so a name that stops being registered is caught on the day it stops. The names written to `usage_logs` are governed separately and are not affected; a historical spelling retained for reading rows written before it was corrected is not agent-facing guidance.

**The check runs over the producer's rendered output, and its extraction rule is fixed.** The outline object SHALL carry no prose and therefore SHALL yield zero tool references — the outline's former omitted-sections summary collapsed into `notice`, whose guidance covers both the truncation continuation and the narrowing suggestion. In a rendered guidance string, a *tool reference* is a backtick-delimited span whose content is either exactly an identifier matching `[A-Za-z_][A-Za-z0-9_]*`, or such an identifier immediately followed by `(`; the referenced name is that identifier. Any other span is not a tool reference — an ordinal (`` `#7` ``), an outline entry (`` `## Tasks` ``), a quoted argument (`` `section="#7"` ``). The `notice` field's rendered text SHALL yield a non-empty set of tool references, and every name in it SHALL appear in the registry.

**Non-empty and registered is not enough: the guidance SHALL name the search tool.** The extracted set of the clause carrying the narrowing guidance SHALL contain `keyword_search`. This is membership, not equality — a further registered tool reference added beside it is permitted, so the requirement does not freeze the copy.

That last assertion is an altitude correction, and is recorded as one. The two assertions above it encode a general property — no agent-facing string names an unregistered tool — which is too weak to express what is actually wanted here: that *this* guidance names *the search tool*. Under the general property alone, rewriting the summary to narrow with `delete_note` passes every check while pointing the agent at a destructive tool, and adding a second registered reference beside `keyword_search` lets its backticks be dropped with the set still non-empty and fully registered. Three review rounds of the check passed vacuously on that gap before it was closed. The registry comparison stays as the broad backstop; the membership assertion is what pins the specific claim.

**Both halves of the registry check** — a non-empty set, and every name in it registered — are load-bearing, and the alternative shapes fail in opposite directions. A source-wide scan for backticked identifiers across the tool module cannot work: `list_files`'s own truncation line already emits a bare `` `pattern` ``, which is lexically identical to a bare `` `keyword_search` `` and is not a tool — and filtering the candidate set against the registry to suppress it would remove exactly the unregistered names the check exists to catch, leaving a check that passes over an empty set. Hence a fixed scope of one producer (`notice`), plus the non-empty assertion and the outline's zero-reference assertion: without it, a reformatting that drops the backticks turns the check into a no-op that still reports green.

Requiring the guidance to be emitted through a registry-validating helper was the other candidate and is not what this requires. It moves a copy concern into the runtime, is bypassed by the next f-string exactly as a scan is, and its validation fires when a note is truncated in production rather than in the test run.

#### Scenario: Truncated whole-note read offers a callable tool

- **WHEN** a whole-note read is truncated and the response suggests narrowing the request by search instead of reading the whole note
- **THEN** the suggested tool name SHALL be `keyword_search`
- **AND** SHALL NOT be `search_notes`

#### Scenario: The outline object names no tools

- **WHEN** a truncated whole-note read carries the structured outline, complete or degraded
- **THEN** the outline object SHALL yield zero tool references under the fixed extraction rule, and the narrowing guidance (naming `keyword_search`) SHALL appear in `notice`


#### Scenario: The notice offers at least one name

- **WHEN** any truncated `read_note` response is rendered
- **THEN** the `notice` field SHALL yield a non-empty set of tool references


#### Scenario: Every extracted name is registered

- **WHEN** the tool references extracted from that rendered text are compared with the tool names the MCP server registry reports
- **THEN** every extracted name SHALL appear in that registry

#### Scenario: The narrowing guidance names the search tool

- **WHEN** the clause carrying the producer's narrowing guidance is rendered and its tool references extracted
- **THEN** that clause's extracted set SHALL contain `keyword_search`
- **AND** SHALL NOT be required to contain it alone, so a further registered tool reference beside it is permitted

#### Scenario: A registered but wrong replacement fails the check

- **WHEN** the producer's guidance is changed to narrow with a different registered tool, such as `delete_note`, instead of `keyword_search`
- **THEN** that clause's extracted set SHALL still be non-empty and every name in it SHALL still appear in the registry
- **AND** the check SHALL nevertheless fail, because `keyword_search` is absent from it

#### Scenario: A second reference does not mask a dropped name

- **WHEN** a second registered tool reference is added to a producer's guidance clause and `keyword_search`'s backticks are then removed
- **THEN** that clause's extracted set SHALL still be non-empty and fully registered
- **AND** the check SHALL fail, because `keyword_search` is no longer a member of it

#### Scenario: A reinstated `search_notes` fails the check

- **WHEN** the producer is changed to name `search_notes` again
- **THEN** `search_notes` SHALL be extracted as a tool reference and SHALL NOT appear in the registry
- **AND** the check SHALL fail, rather than the defect reaching a caller

### Requirement: read_note responses are structurally framed

`read_note` SHALL return a structured result — discrete fields for metadata, note content, truncation state, and errors — declared via the tool's MCP output schema and delivered as structured content alongside a JSON-serialized text rendering. No note-controlled value (title, path, tags, frontmatter keys or values, or note text) SHALL be able to alter which field any other value appears in: there SHALL be no delimiter-based envelope whose frame note content could forge. The unstructured text rendering and the structured content SHALL be built from the same already-JSON-safe values, so the two never diverge and recovery of any field from either form is reversible. Fields that are inapplicable to a given response SHALL be absent from both renderings, not `null`.

Every note-controlled field SHALL have an explicit budget: `content` is governed by the existing response-size cap; the outline by its existing independent budget; `path` is always exact — this change adds an explicit admission-time path-length limit (1,024 characters, matching the index's `file_path` column) so the exact value is a fixed allocation, never elided or marked, and path-bearing error messages inherit the same bound; and the remaining metadata fields (`title`, `tags`, `frontmatter_yaml` and its JSON view, `heading`) share a metadata budget also bounded by `MAX_READ_RESPONSE_CHARS`. When the aggregate exceeds that budget, fields SHALL be dropped in a deterministic priority order — the lossy `frontmatter` JSON view first, then `frontmatter_yaml`, then `tags`, then `heading`, then `title` — until the remainder fits; a dropped field is **omitted whole: never truncated in place, never cut short, and never replaced by an in-band textual marker** (a shortened or marked value inside a note-controlled field is indistinguishable from note content — the forgery class this change exists to end). Every omission SHALL be reported in a separate server-controlled `metadata_omissions` field naming the field, the reason, and how to read the full value (the raw note). Error and notice strings interpolate only bounded values. The worst-case serialized response — the sum of these budgets plus the fixed path allocation, doubled for the structured-plus-text duplication and multiplied by JSON string escaping's worst-case six-characters-per-character expansion of note-controlled text — SHALL be stated in the architecture documentation, replacing the previous rendered-string worst case.

When more than one failure applies to a call, precedence SHALL be: path resolution (missing note) first, then parameter validation (`offset`, `limit`), then section resolution; exactly one `error` is reported and content-bearing fields are absent.

#### Scenario: A multiline YAML title cannot forge the frame

- **WHEN** a note's frontmatter title is a block scalar containing a line that is exactly `---` (issue #149 reproduction 1) and the note is read with `section="#1"`
- **THEN** the forged line SHALL appear only inside the `title` field's JSON-escaped value, and the `content` field SHALL carry exactly the section body

#### Scenario: A quoted frontmatter key cannot forge the frame

- **WHEN** a note's frontmatter contains a key whose decoded value embeds `\n---\n` (issue #149 reproduction 2)
- **THEN** the key SHALL appear only as JSON-escaped text inside the frontmatter fields, and the `content` field SHALL carry exactly the selected content

#### Scenario: Distinct paths stay distinguishable

- **WHEN** two note paths differ only by a character that a lossy rendering would collapse (e.g. a newline versus a space)
- **THEN** the `path` field SHALL distinguish them exactly

#### Scenario: Any valid frontmatter serializes, and the raw block is authoritative

- **WHEN** a note carries a valid frontmatter block
- **THEN** the response SHALL carry `frontmatter_yaml` — the block's text as the read path sees it, universal-newline-normalized to LF (the same declared terminator residual as section bodies) with fence lines excluded — as the authoritative representation, and a best-effort JSON view in `frontmatter`; under metadata-budget pressure the block is omitted whole (reported in `metadata_omissions`), never truncated, so the field is content-lossless whenever present, with the LF residual declared in the docstrings
- **AND** the JSON view SHALL be constructed defensively: leaves with no native JSON form (dates, timestamps) become strings; construction is depth- and size-bounded so recursive aliases (`x: &X [*X]`) cannot raise or loop; a parser-accepted value the server cannot serialize — a lone-surrogate escape (`"\uD800"`) — SHALL cost at most the affected fields (view/title/tags omitted via `metadata_omissions`, `frontmatter_yaml` unaffected), and SHALL NOT raise, produce a protocol error, or desynchronize the structured and text renderings; a block the YAML parser cannot even construct (an integer beyond Python's digit limit, a composer-recursion overflow) SHALL be classified as the existing yaml-error defect — the note stays fully readable with the block's bytes in `content`, and structured frontmatter mutation refuses by name — because omitting only the view would hand `set_frontmatter` an empty mapping to merge over a block it never parsed, which is a destructive write; when construction fails, or two YAML keys would collide onto one JSON key (`1:` and `"1":`), the JSON view SHALL be omitted and reported in `metadata_omissions`, with `frontmatter_yaml` still present — unless budget pressure independently drops it, in which case `metadata_omissions` reports both omissions with their distinct reasons
- **AND** the documentation SHALL direct callers that mutate frontmatter to `set_frontmatter` (or the raw block), never to a round trip through the lossy JSON view

#### Scenario: Errors are in-band fields

- **WHEN** `read_note` is invoked on a missing path, with an invalid `offset` or `limit`, or with a selector matching no heading
- **THEN** the result's `error` field SHALL carry the message today's contract requires (identifying the path, the offending value, or the available headings), content-bearing fields SHALL be absent, and the tool SHALL NOT raise
