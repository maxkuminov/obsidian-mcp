## MODIFIED Requirements

### Requirement: `edit_note` supports four mutually exclusive modes

The `edit_note` tool SHALL expose exactly four edit modes, selected by the
combination of parameters supplied: full-replace (default), append,
find/replace, and section. The four modes SHALL be mutually exclusive —
supplying parameters that select more than one mode SHALL return an
actionable error and SHALL NOT mutate the file.

#### Scenario: Full-replace mode (default) preserves a valid frontmatter block

- **WHEN** the client calls `edit_note(path, content)` with neither
  `append`, `find`, nor `section` set and without
  `replace_frontmatter=True`, on a note that carries a valid line-1
  frontmatter block
- **THEN** the written file SHALL be the existing raw block
  byte-identical, a separator (one `\n` inserted only when the block's
  bytes do not already end in a newline and `content` is non-empty),
  then `content` as the entire body
- **AND** no property of `content`'s shape — a leading `---`, an
  unclosed fence, or a complete mapping-shaped fenced block (exactly
  what `read_note` returns for a note whose body begins with one) —
  SHALL change this: `content` is always the body

#### Scenario: Any stripped body round-trips unchanged

- **WHEN** the note-content portion of a **complete, unwindowed
  whole-note** `read_note` response (`section=None`, `offset=0`, no
  truncation notice) is passed back via `edit_note(path, body)` —
  including a body whose first line is a thematic break `---`, a body
  that itself begins with a complete mapping-shaped fenced block, and
  the body of a note whose frontmatter is the whitespace-only empty
  block (which the shared parser SHALL strip on read exactly as it
  preserves it on write)
- **THEN** the resulting file SHALL be identical to the original for a
  note whose body newlines are LF
- **AND** for a CRLF-bodied note the frontmatter block SHALL still be
  preserved byte-identically (CRLF fences included); the *body* comes
  back through `read_note`'s pre-existing universal-newline translation
  as LF, and the round trip preserves content, not the body's original
  newline bytes — a pre-existing property of the read path, declared,
  not a regression introduced here

#### Scenario: The round-trip guarantee does not extend to windows or sections

- **WHEN** a `read_note` response was a selected section
  (`section=<sel>`), a paged window (`offset>0` or a `[TRUNCATED]`
  notice), or otherwise less than the whole body, and its content is
  passed to default full-replace
- **THEN** full-replace still preserves the frontmatter block but
  replaces the ENTIRE body with the partial text — so both layers'
  docstrings SHALL state that section responses belong to section mode
  and truncated reads must be completed before a full-replace write

#### Scenario: replace_frontmatter selects wholesale replacement

- **WHEN** the client calls
  `edit_note(path, content, replace_frontmatter=True)` in full-replace
  mode
- **THEN** the entire note, frontmatter included, SHALL be overwritten
  with exactly `content`
- **AND** `replace_frontmatter=True` combined with `append`, `find`, or
  `section` SHALL return the multi-mode error and SHALL NOT modify the
  file

#### Scenario: A note without a valid block is replaced wholesale

- **WHEN** the existing note has no line-1 fence, or its block is
  defective (unclosed fence — a bare unterminated `---` at EOF included
  — YAML error, or non-mapping), and the client calls
  `edit_note(path, content)` in full-replace mode
- **THEN** the entire note SHALL be overwritten with `content` (there is
  no valid block to preserve; this is the repair path and needs no flag)

#### Scenario: A metadata-only note gains a correct separator

- **WHEN** the existing note is exactly a valid frontmatter block whose
  closing fence ends at EOF without a trailing newline, and the client
  calls `edit_note(path, "Body\n")` in default full-replace mode
- **THEN** the written file SHALL contain the block, a single inserted
  newline, then `Body\n` — the closing fence SHALL remain a recognized
  fence

#### Scenario: A trailing-whitespace closing fence is one block everywhere

- **WHEN** a note's closing fence carries trailing spaces or tabs
  (which `parse_frontmatter` accepts today)
- **THEN** default full-replace SHALL treat the note as carrying a valid
  block and preserve it byte-identically, trailing whitespace included

#### Scenario: Fence lines end at LF, CRLF or a lone CR

- **WHEN** a note's fence lines are terminated by LF, by CRLF, or by a
  lone CR (classic-Mac line endings)
- **THEN** the shared parser SHALL recognize the block in every case,
  consistent with the universal-newline translation the read path applies
  before it parses the same file — so a note whose block `read_note`
  strips is never diagnosed as having no frontmatter by a tool that is
  about to write
- **AND** default full-replace SHALL preserve that block
  byte-identically, its own terminators included, and SHALL insert the
  separator newline only when the block's bytes end in no line terminator
  at all
- **AND** `set_frontmatter` SHALL update such a block rather than
  prepending a second one above it, and SHALL refuse it by name when it
  is defective
- **AND** the fenced/inline code masking that heading resolution runs on
  SHALL use the same terminator rule, so a heading inside a code block is
  hidden from `edit_note(section=…)` exactly as it is from
  `read_note(section=…)` — otherwise a selector resolves inside code on
  the write side only, and the replacement deletes the closing fence
- **AND** widening the terminator rule SHALL NOT narrow which characters
  separate a heading's `#` marker from its text: every whitespace
  character except the three terminators still separates them, so no
  heading present on an existing note loses its name or its `#N` ordinal

#### Scenario: The composed result meets the cap and conflict checks

- **WHEN** preservation composes a result whose byte size exceeds the
  note-size cap, or `expected=` was supplied and any part of the raw
  file — the frontmatter block included — changed since it was read
- **THEN** the call SHALL be refused without writing (the cap applies to
  the composed result; `expected=` compares the complete raw bytes, so a
  concurrent frontmatter-only change conflicts)
- **AND** `dry_run=True` SHALL diff the composed result

#### Scenario: Append mode

- **WHEN** the client calls `edit_note(path, content, append=True)`
  without `find` or `section`
- **THEN** the new file content SHALL be the prior content followed by a
  single `\n` separator and `content`

#### Scenario: Find/replace mode

- **WHEN** the client calls `edit_note(path, content, find=<text>)`
  without `append=True` or `section`
- **THEN** the system SHALL replace occurrences of `find` in the prior
  content with `content` per the `replace_all` rules below

#### Scenario: Section mode

- **WHEN** the client calls `edit_note(path, content, section=<heading>)`
  without `append=True` or `find`
- **THEN** the system SHALL replace the body under the named ATX heading
  per the section-mode rules below

#### Scenario: Multiple modes set is rejected

- **WHEN** the client supplies more than one of `append=True`,
  `find=...`, or `section=...` in the same call
- **THEN** the system SHALL return an error naming the conflicting
  parameters
- **AND** SHALL NOT modify the file

### Requirement: Section mode replaces the body under a named heading

When `section=<heading>` is supplied, `edit_note` SHALL locate the matching
ATX heading (1–6 `#` characters) and SHALL replace the lines between that
heading and the next heading of equal-or-shallower depth (or end of file)
with the supplied `content`. The matched heading line itself SHALL NOT be
removed or rewritten.

The selector SHALL accept three forms:

1. **Ordinal** (e.g. `#7`) — a selector consisting solely of `#` followed by
   digits SHALL select the Nth ATX heading in document order, 1-based. A bare
   ordinal SHALL always select by position, even when some heading's literal
   text is the same string. Ordinals are advertised to callers as the reliable
   selector, so note content MUST NOT be able to shadow one.
2. **Path-style chain** (e.g. `Parent/Child`), where the final part is the
   target heading and the preceding parts are ancestors in outermost-first
   order. A selector containing `/` SHALL NOT be interpreted as an ordinal.
3. **Exact heading text** (e.g. `Tasks`).

A heading whose literal text is `#N` SHALL remain addressable by the path-style
form (`Parent/#N`) and by its own ordinal.

The ordinal form exists because the path-style form cannot separate duplicate
**sibling** headings — headings with identical text under the same parent share
every ancestor, so no chain distinguishes them.

The same selector grammar SHALL be used by every tool that accepts a `section`
argument, so a selector that names a section for reading names the same section
for writing.

When the note carries a valid line-1 frontmatter block, heading resolution
(all three selector forms), body replacement, and the not-found/ambiguity
listings SHALL operate on the frontmatter-stripped body — the same text
`read_note` scans — and the write SHALL reattach the raw block
byte-identically. A line inside the block (a YAML `#` comment included)
SHALL never be selectable as a heading and SHALL never be counted by an
ordinal. When the note's block is **defective** (unclosed fence, YAML
error, or non-mapping — comment-only YAML included), section-mode writes
SHALL be refused with an error naming the defect and pointing at
`edit_note(replace_frontmatter=True)` as the repair — never resolved over
the raw bytes, where a `#` line inside the broken block could be selected
and a replacement could delete the closing fence. A note with no fence at
all resolves over its raw content, which is identical to what `read_note`
scans there.

#### Scenario: A YAML comment in frontmatter is not a heading

- **WHEN** a note is `---\n# Tasks\nstatus: draft\n---\n# Body\nkeep\n`
  and the client calls `edit_note(path, "x", section="Tasks")`
- **THEN** the selector SHALL NOT match the `# Tasks` line inside the
  frontmatter block; it resolves against the body's headings only (here
  reporting `Tasks` not found and listing `Body`)
- **AND** the frontmatter block SHALL be untouched by any section edit

#### Scenario: Ordinals agree between read_note and edit_note

- **WHEN** a note's frontmatter block contains `#`-prefixed comment lines
  and its body contains ATX headings
- **THEN** `edit_note(section="#N")` SHALL select the same heading that
  `read_note(section="#N")` extracts

#### Scenario: A defective block refuses section writes by name

- **WHEN** a note is `---\n# Tasks\n---\n# Body\nkeep\n` (comment-only
  YAML — a non-mapping) or carries an unclosed or YAML-erroring block,
  and the client calls `edit_note(path, "x", section=<anything>)`
- **THEN** the call SHALL be refused with an error naming the defect and
  the `replace_frontmatter=True` repair path
- **AND** SHALL NOT modify the file

#### Scenario: Replace section under a level-2 heading

- **WHEN** the note contains `## Tasks\nA\nB\n## Notes\nC` and the client
  calls `edit_note(path, content="X\nY", section="Tasks")`
- **THEN** the resulting note SHALL be `## Tasks\nX\nY\n## Notes\nC`

#### Scenario: Section heading not found

- **WHEN** no ATX heading in the note has trimmed text equal to
  `<heading>`, and the selector is not a valid ordinal
- **THEN** the response SHALL list the headings that ARE present in the
  note (with their depth) and instruct the caller to disambiguate
- **AND** SHALL NOT modify the file

#### Scenario: Multiple matching headings disambiguated by occurrence

- **WHEN** more than one heading in the note matches `<heading>` exactly
- **THEN** the response SHALL state the number of matches and instruct
  the caller to use the more-specific path-style form
  `Parent Heading/Child Heading` to disambiguate
- **AND** the response SHALL name the `#N` ordinals that identify each match
- **AND** SHALL NOT modify the file until the call is reissued
  unambiguously

#### Scenario: Path-style heading disambiguation

- **WHEN** the client calls `edit_note(path, content, section="Tasks/Today")`
  and the note contains `## Tasks` followed by `### Today`
- **THEN** the system SHALL replace the body under `### Today` (bounded
  by the next heading of depth ≤ 3) with `content`

#### Scenario: Duplicate sibling headings resolved by ordinal

- **WHEN** a note contains two headings with identical text under the same
  parent, and the client supplies the `#N` ordinal of one of them
- **THEN** that specific section SHALL be selected
- **AND** the other identically-titled section SHALL be unaffected

#### Scenario: Ordinal out of range

- **WHEN** the supplied ordinal is below 1 or exceeds the number of ATX
  headings in the note
- **THEN** the response SHALL report the valid ordinal range
- **AND** SHALL NOT modify the file

#### Scenario: A heading literally named like an ordinal

- **WHEN** a note contains a heading whose text is exactly `#2` and the
  client supplies `section="#2"`
- **THEN** the second heading in the note SHALL be selected, because a bare
  ordinal always selects by position
- **AND** the heading titled `#2` SHALL remain reachable via the path-style
  form and via its own ordinal


### Requirement: Usage logs capture the new tools and parameters

Calls to `move_note`, `delete_note`, and `set_frontmatter` SHALL be
recorded via the existing `_tracked` decorator with `tool` set to the
respective tool name. Calls to `edit_note` that include `dry_run`,
`replace_all`, `section`, or `replace_frontmatter` SHALL include those
parameters in `usage_logs.params` (subject to the existing
string-truncation behavior of `_tracked`).

#### Scenario: `move_note` invocation is logged

- **WHEN** an agent calls `move_note(from_path="A.md", to_path="B.md")`
- **THEN** a row SHALL be appended to `usage_logs` with
  `tool='move_note'` and `params` containing `from_path` and `to_path`

#### Scenario: `dry_run` flag is logged on `edit_note`

- **WHEN** an agent calls `edit_note(path, content, dry_run=True)`
- **THEN** the `usage_logs` row for that call SHALL have `tool='edit_note'`
- **AND** `params` SHALL include `dry_run`

#### Scenario: `replace_frontmatter` is logged on `edit_note`

- **WHEN** an agent calls
  `edit_note(path, content, replace_frontmatter=True)`
- **THEN** the `usage_logs` row for that call SHALL have
  `tool='edit_note'`
- **AND** `params` SHALL include `replace_frontmatter`, because it is the
  destructive-intent flag on this tool: it is the difference between a
  write that preserved the note's frontmatter and one that replaced it
  wholesale, which is what an operator reading the audit trail after a
  block went missing needs to see
- **AND** the note's `content` SHALL remain absent from `params`, as it
  is today


### Requirement: `set_frontmatter` performs structured frontmatter mutations

The MCP server SHALL expose a tool `set_frontmatter(path: str, updates:
dict, remove: list[str] = []) -> str` that parses the note's YAML
frontmatter, merges in `updates` (overwriting matching keys, adding new
ones), removes the keys listed in `remove`, and re-serializes the
frontmatter using `yaml.safe_dump(default_flow_style=False,
sort_keys=False, allow_unicode=True)`. The note body SHALL NOT be
modified.

#### Scenario: Update existing keys

- **WHEN** the client calls `set_frontmatter(path, updates={"status":
  "done"})` on a note whose frontmatter already has `status: draft`
- **THEN** the frontmatter SHALL contain `status: done` and all other
  keys SHALL be preserved with their existing values
- **AND** the body of the note SHALL be byte-identical to before the call

#### Scenario: Add a new key

- **WHEN** the client calls `set_frontmatter(path, updates={"project":
  "Cyberdeen"})` on a note whose frontmatter does not have a `project`
  key
- **THEN** the resulting frontmatter SHALL contain the existing keys
  plus `project: Cyberdeen`

#### Scenario: Remove keys

- **WHEN** the client calls `set_frontmatter(path, updates={},
  remove=["wip", "draft"])`
- **THEN** the resulting frontmatter SHALL not contain `wip` or `draft`
- **AND** any other existing keys SHALL be preserved

#### Scenario: Note has no existing frontmatter

- **WHEN** the note has no `---`-fenced frontmatter block at line 1 and
  the client calls `set_frontmatter(path, updates={"tags": ["x"]})`
- **THEN** a new frontmatter block SHALL be prepended to the note in the
  form `---\n<yaml>\n---\n` followed by the original body unchanged

#### Scenario: Note has frontmatter not on line 1

- **WHEN** the note begins with blank lines or other content before any
  `---` fence
- **THEN** the tool SHALL treat the note as having no frontmatter (per
  Obsidian's "frontmatter must be on line 1" rule) and SHALL prepend a
  new frontmatter block, leaving the original content unchanged after
  the new block

#### Scenario: Empty updates and empty removes is a no-op

- **WHEN** the client calls `set_frontmatter(path, updates={}, remove=[])`
  on a note whose frontmatter is absent or valid
- **THEN** the response SHALL indicate no changes
- **AND** the file SHALL be byte-identical before and after the call
- **AND** on a note whose frontmatter is malformed, the same call SHALL
  return the defect refusal (diagnosis precedes the no-op check), not a
  success report

#### Scenario: Unclosed frontmatter fence is refused

- **WHEN** the note's first line is exactly `---` with no closing fence
  on any later line — a file consisting solely of an unterminated `---`
  line included — and the client calls `set_frontmatter` with any
  `updates` or `remove`
- **THEN** the tool SHALL return an error naming the unclosed fence and
  the `edit_note(replace_frontmatter=True)` repair path
- **AND** SHALL NOT modify the file, and in particular SHALL NOT prepend
  a second frontmatter block

#### Scenario: Frontmatter that fails YAML parsing is refused

- **WHEN** the note has a line-1 fenced block whose contents fail YAML
  parsing
- **THEN** the tool SHALL return an error that includes the parser's
  message
- **AND** SHALL NOT modify the file

#### Scenario: Non-mapping frontmatter is refused

- **WHEN** the note has a line-1 fenced block whose YAML parses to a
  list, a scalar, or non-whitespace YAML loading to `None` (`null`, `~`,
  or only comments)
- **THEN** the tool SHALL return an error naming the non-mapping shape
- **AND** SHALL NOT modify the file

#### Scenario: remove on a malformed block refuses rather than no-ops

- **WHEN** the note's frontmatter is malformed in any of the above ways
  and the client calls `set_frontmatter(path, updates={}, remove=["x"])`
- **THEN** the tool SHALL return the same refusal, not a success report

#### Scenario: An empty fenced block is a valid empty mapping

- **WHEN** the note begins with `---\n---\n` (or the CRLF equivalent)
  and the client calls `set_frontmatter(path, updates={"a": 1})`
- **THEN** the block SHALL be treated as a valid empty mapping and
  updated to contain `a: 1`, with the body preserved exactly — including
  when the body is empty

#### Scenario: Removing the last key removes the block entirely

- **WHEN** the note's frontmatter contains exactly one key and the
  client calls `set_frontmatter(path, updates={}, remove=[<that key>])`
- **THEN** the written file SHALL contain no opening fence, no YAML
  region, no closing fence and no separator — exactly the prior body

#### Scenario: A call that changes no key writes nothing

- **WHEN** `set_frontmatter` is called with `updates` that set every
  named key to the value it already has, and/or `remove` naming only
  keys that are not present — including on a note whose frontmatter is
  the valid whitespace-only empty block
- **THEN** the response SHALL indicate no changes and the file SHALL be
  byte-identical — in particular, an existing empty block SHALL NOT be
  dropped by a remove that removed nothing (dropping it would promote a
  mapping-shaped body prefix into active frontmatter)
