## ADDED Requirements

### Requirement: read_note responses are structurally framed

`read_note` SHALL return a structured result — discrete fields for metadata, note content, truncation state, and errors — declared via the tool's MCP output schema and delivered as structured content alongside a JSON-serialized text rendering. No note-controlled value (title, path, tags, frontmatter keys or values, or note text) SHALL be able to alter which field any other value appears in: there SHALL be no delimiter-based envelope whose frame note content could forge. The unstructured text rendering SHALL be exactly the JSON serialization of the structured result, so recovery of any field from either form is reversible.

#### Scenario: A multiline YAML title cannot forge the frame

- **WHEN** a note's frontmatter title is a block scalar containing a line that is exactly `---` (issue #149 reproduction 1) and the note is read with `section="#1"`
- **THEN** the forged line SHALL appear only inside the `title` field's JSON-escaped value, and the `content` field SHALL carry exactly the section body

#### Scenario: A quoted frontmatter key cannot forge the frame

- **WHEN** a note's frontmatter contains a key whose decoded value embeds `\n---\n` (issue #149 reproduction 2)
- **THEN** the key SHALL appear only as a JSON-escaped string inside the `frontmatter` field, and the `content` field SHALL carry exactly the selected content

#### Scenario: Distinct paths stay distinguishable

- **WHEN** two note paths differ only by a character that a lossy rendering would collapse (e.g. a newline versus a space)
- **THEN** the `path` field SHALL distinguish them exactly

#### Scenario: Any valid frontmatter serializes

- **WHEN** a note's frontmatter contains YAML values with no native JSON form (dates, nested maps, anchors)
- **THEN** the response SHALL serialize successfully, coercing such leaves to strings, and SHALL NOT raise

#### Scenario: Errors are in-band fields

- **WHEN** `read_note` is invoked on a missing path, with an invalid `offset` or `limit`, or with a selector matching no heading
- **THEN** the result's `error` field SHALL carry the message today's contract requires (identifying the path, the offending value, or the available headings), content-bearing fields SHALL be absent, and the tool SHALL NOT raise

## MODIFIED Requirements

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

### Requirement: Section selection in read_note

When `section` is supplied, `read_note` SHALL return only the named section: the matched ATX heading line in the `heading` field (without its line terminator), and in `content` the section's **body only** — exactly the span `edit_note(section=…)` replaces, nothing more and nothing less, so the section read's `content` is byte-exact input for the section write. The selector SHALL accept the same forms as the write tools: exact heading text, the path-style `Parent/Child` form, and the `#N` ordinal form.

A section response SHALL be subject to the same response-size cap, and `offset` SHALL window within the selected body rather than within the whole note.

The parity claim is scoped to notes for which section-mode writing is **admitted**. A note whose line-1 frontmatter is defective (unclosed fence, YAML error, or non-mapping) stays readable by section — the read scans its raw bytes — while every section write to it is refused by name, per the vault-write requirement this change does not relax. On such a note the guarantee is the refusal, not the round trip.

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

When a whole-note read is truncated and the note contains ATX headings, the response SHALL include an outline of the note's sections as a list of structured entries. Each entry SHALL carry the section's `#N` ordinal, heading depth, heading text, and size in characters, and SHALL indicate when a section itself exceeds the cap. The response's `notice` field SHALL tell the caller how to read a listed section directly.

The outline SHALL NOT be included when a `section` was explicitly selected, since the caller has already chosen.

The outline SHALL itself be bounded by `MAX_READ_RESPONSE_CHARS`, measured over its serialized entries. It accompanies a response that exists because the content was too large, so an unbounded outline would reintroduce the failure this capability prevents. Overlong headings SHALL be elided, and when the listing does not fit, it SHALL stop and report how many sections were omitted along with the full ordinal range. When the complete listing fits within the cap it SHALL be emitted in full, with no omission summary. At least one entry SHALL be emitted whenever one fits; when the cap is too small for even a single entry or the summary itself, the outline SHALL degrade to an explicit truncation marker rather than exceed the cap. The cap is the binding constraint: there is no output the outline may exceed it to produce.

#### Scenario: Truncated note with headings

- **WHEN** a whole-note read is truncated on a note containing ATX headings
- **THEN** the outline SHALL list each section with its ordinal, title, and size
- **AND** SHALL indicate which sections exceed the cap
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
