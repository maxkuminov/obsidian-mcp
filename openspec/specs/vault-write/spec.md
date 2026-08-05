# vault-write Specification

## Purpose
TBD - created by archiving change vault-write-completion. Update Purpose after archive.
## Requirements
### Requirement: Atomic write invariant

The system SHALL perform all file writes from MCP write tools via a temporary file in the same directory as the destination followed by `os.replace()` (or equivalent atomic rename on the same filesystem). The applicable tools are `create_note`, `edit_note`, `move_note`, `delete_note`, and `set_frontmatter`. Direct writes that could leave the destination truncated on crash SHALL NOT be used.

#### Scenario: Crash mid-write does not truncate the destination

- **WHEN** the server process is killed between the tmp-file write and the
  rename
- **THEN** the destination file SHALL retain its prior content unchanged
- **AND** the orphaned `.tmp-*` file SHALL be discoverable for cleanup by
  the next reindex (it lives in a dot-prefixed name, so the indexer
  ignores it)

#### Scenario: Successful write atomically replaces existing content

- **WHEN** `edit_note` is called with new content and succeeds
- **THEN** any reader observing the destination path SHALL see either the
  full prior content or the full new content, never a partial mix

### Requirement: Write tools require a `readwrite` API key

Each write tool SHALL call the existing `_require_write()` helper before performing any filesystem mutation. The applicable tools are `create_note`, `edit_note`, `move_note`, `delete_note`, and `set_frontmatter`. Calls authenticated with a read-only key SHALL receive the existing "Permission denied" error message and SHALL NOT mutate the vault.

#### Scenario: Read-only key cannot move a note

- **WHEN** a client authenticated with a read-only key invokes
  `move_note(from_path=..., to_path=...)`
- **THEN** the server SHALL return the standard permission-denied message
- **AND** SHALL NOT move the file or update the database

#### Scenario: Read-only key cannot soft-delete

- **WHEN** a client authenticated with a read-only key invokes
  `delete_note(path=...)`
- **THEN** the server SHALL return the permission-denied message
- **AND** the note SHALL remain at its original path

### Requirement: `edit_note` supports four mutually exclusive modes

The `edit_note` tool SHALL expose exactly four edit modes, selected by the
combination of parameters supplied: full-replace (default), append,
find/replace, and section. The four modes SHALL be mutually exclusive —
supplying parameters that select more than one mode SHALL return an
actionable error and SHALL NOT mutate the file.

#### Scenario: Full-replace mode (default)

- **WHEN** the client calls `edit_note(path, content)` with neither
  `append`, `find`, nor `section` set
- **THEN** the entire note SHALL be overwritten with `content`

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

### Requirement: Find/replace mode supports single-match and replace-all

When `find` is supplied, `edit_note` SHALL by default require `find` to
appear exactly once in the prior content (preserving the existing
behavior). When `replace_all=True` is also supplied, the tool SHALL
replace every occurrence of `find` with `content`. Setting `replace_all`
without `find` SHALL be ignored (no error).

#### Scenario: Single match (default)

- **WHEN** `find` matches exactly one location and `replace_all` is not
  set or is False
- **THEN** that single occurrence SHALL be replaced with `content`

#### Scenario: Zero matches returns actionable error

- **WHEN** `find` does not appear in the prior content
- **THEN** the response SHALL state that the find text was not found and
  SHALL include a preview of the first 500 characters of the note
- **AND** SHALL NOT modify the file

#### Scenario: Multiple matches without replace_all returns actionable error

- **WHEN** `find` matches more than once and `replace_all` is False or
  unset
- **THEN** the response SHALL state the match count and instruct the
  caller to add surrounding context or set `replace_all=True`
- **AND** SHALL NOT modify the file

#### Scenario: Multiple matches with replace_all replaces all

- **WHEN** `find` matches N>=1 times and `replace_all=True`
- **THEN** all N occurrences SHALL be replaced with `content`
- **AND** the response SHALL report the number of replacements made

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

### Requirement: `dry_run` returns a unified diff without mutating

`edit_note` SHALL accept a `dry_run: bool = False` parameter applicable
to all four edit modes. When `dry_run=True`, the tool SHALL compute the
would-be new content, return a unified diff (via `difflib.unified_diff`)
between the prior content and the would-be new content, and SHALL NOT
write to the filesystem.

#### Scenario: Dry-run returns the diff text

- **WHEN** the client calls `edit_note(path, content, find=<text>, dry_run=True)`
- **THEN** the response SHALL be a string containing a unified diff with
  `---` / `+++` headers and `@@` hunk markers
- **AND** the file at `path` SHALL be byte-identical before and after
  the call

#### Scenario: Dry-run on a no-op edit

- **WHEN** the requested edit would produce the same content as exists
  on disk
- **THEN** the response SHALL indicate no changes (empty diff or "no
  changes")
- **AND** SHALL NOT write to the filesystem

### Requirement: `move_note` renames or relocates a note and updates the link graph

The MCP server SHALL expose a tool `move_note(from_path: str, to_path:
str, rewrite_links: bool = False) -> str` that moves the note at
`from_path` to `to_path`, updates `notes_metadata.file_path` for the
moved note, and updates `note_links.target_path` for every row whose
prior `target_path` was `from_path`. Rename and move SHALL be the same
operation (a rename is a move whose `to_path` differs only in basename).

#### Scenario: Move within the vault

- **WHEN** the client calls `move_note(from_path="Cards/A.md",
  to_path="Cards/B.md")` and `Cards/A.md` exists and `Cards/B.md` does
  not
- **THEN** the file SHALL be moved on disk via atomic rename
- **AND** `notes_metadata.file_path` for that note SHALL be updated to
  `Cards/B.md`
- **AND** every `note_links` row whose `target_path` was `Cards/A.md`
  SHALL have `target_path` updated to `Cards/B.md`
- **AND** outgoing-link rows authored by the moved note SHALL continue to
  resolve from it without further DB mutation (the moved note's primary
  key is unchanged, so `note_links.source_note_id` foreign keys remain
  valid)

#### Scenario: Move creates missing destination directory

- **WHEN** `to_path` is `New/Folder/X.md` and `New/Folder/` does not
  exist
- **THEN** the system SHALL create the parent directories before moving
  the file

#### Scenario: Destination exists

- **WHEN** the file at `to_path` already exists
- **THEN** the response SHALL state that the destination exists and
  refuse the move
- **AND** the file at `from_path` SHALL remain in place
- **AND** the link graph SHALL NOT be modified

#### Scenario: Source missing

- **WHEN** the file at `from_path` does not exist
- **THEN** the response SHALL state that the source is missing
- **AND** SHALL NOT modify the link graph

#### Scenario: Path traversal rejected

- **WHEN** either `from_path` or `to_path` resolves outside the vault
  root via the existing `validate_path` helper
- **THEN** the response SHALL return the standard validation error
- **AND** SHALL NOT touch the filesystem or database

### Requirement: `move_note` rewrites incoming wikilinks only when opted in

When `rewrite_links=True`, `move_note` SHALL additionally rewrite
incoming `[[wikilinks]]` and `![[embeds]]` in source notes to point at
the new title/path. The set of source notes SHALL be the same set
returned by `get_backlinks(from_path)` prior to the move. When
`rewrite_links=False` (default), source-note bodies SHALL NOT be
modified.

#### Scenario: Default leaves source-note bodies untouched

- **WHEN** the client calls `move_note(from_path, to_path)` without
  setting `rewrite_links`
- **THEN** no source-note files SHALL be opened or rewritten
- **AND** any `[[OldTitle]]` references in the vault SHALL remain as
  written and become dangling references

#### Scenario: Opt-in rewrite updates incoming wikilinks

- **WHEN** the client calls `move_note(from_path="Cards/Foo.md",
  to_path="Cards/Bar.md", rewrite_links=True)` and a source note
  contains `[[Foo]]` or `![[Foo]]`
- **THEN** the source note SHALL be updated so that `[[Foo]]` becomes
  `[[Bar]]` and `![[Foo]]` becomes `![[Bar]]`
- **AND** any block/heading suffix following `Foo` (e.g. `[[Foo#H1]]`,
  `[[Foo#^abc]]`) SHALL be preserved and only the title portion
  SHALL be rewritten

#### Scenario: Aliased wikilinks have alias preserved

- **WHEN** a source note contains `[[Foo|Display Text]]` and
  `rewrite_links=True`
- **THEN** the link SHALL be rewritten to `[[Bar|Display Text]]`

#### Scenario: Path-style wikilinks updated when used

- **WHEN** a source note contains `[[folder/Foo]]` referencing the moved
  note and `rewrite_links=True`
- **THEN** the link SHALL be rewritten to use the new path

### Requirement: `delete_note` soft-deletes to `.trash/` by default

The MCP server SHALL expose a tool `delete_note(path: str, permanent:
bool = False) -> str`. With `permanent=False` (default), the tool SHALL
move the note to `.trash/<YYYYMMDD-HHMMSS>-<original-basename>` inside
the vault root, creating `.trash/` if needed. With `permanent=True`, the
tool SHALL `os.unlink()` the file directly. In both cases the response
SHALL identify what happened and where the file went (or that it was
permanently deleted).

#### Scenario: Soft-delete moves the file under `.trash/`

- **WHEN** the client calls `delete_note(path="Cards/Old.md")`
- **THEN** the file SHALL be moved to a path of the form
  `.trash/<timestamp>-Old.md` inside the vault root
- **AND** the response SHALL include the trash path

#### Scenario: Soft-delete is invisible to search

- **WHEN** a soft-deleted note has been moved into `.trash/` and the
  next reindex pass completes
- **THEN** the row in `notes_metadata` for that note SHALL be removed
- **AND** the dependent `note_embeddings` and `note_links` rows SHALL be
  cleaned up via existing FK cascades

#### Scenario: Permanent delete removes the file outright

- **WHEN** the client calls `delete_note(path="Cards/Old.md", permanent=True)`
- **THEN** the file SHALL be removed via `os.unlink()`
- **AND** the response SHALL state that the file was permanently deleted

#### Scenario: Trash collisions are disambiguated by timestamp

- **WHEN** the same note path is soft-deleted twice (e.g. user restores,
  then re-deletes)
- **THEN** each delete SHALL produce a distinct `.trash/` entry
  distinguished by its timestamp prefix

#### Scenario: Missing note returns an actionable error

- **WHEN** the client calls `delete_note` on a non-existent path
- **THEN** the response SHALL state that the note does not exist
- **AND** SHALL NOT create a `.trash/` directory

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
- **THEN** the response SHALL indicate no changes
- **AND** the file SHALL be byte-identical before and after the call

### Requirement: Write-tool docstrings use neutral framing

Write-tool docstrings SHALL NOT instruct the agent to call `get_vault_guide` first using compelling language such as "MUST", "IMPORTANT: Call …first", or equivalent. The applicable tools are `create_note`, `edit_note`, `move_note`, `delete_note`, and `set_frontmatter`. References to `get_vault_guide` SHALL use neutral framing such as "see `get_vault_guide` for vault conventions". The docstrings SHALL NOT describe any tool as the "primary" or "default" write tool.

#### Scenario: Move/delete/set_frontmatter docstrings

- **WHEN** an MCP client lists tools
- **THEN** the docstrings of `move_note`, `delete_note`, and
  `set_frontmatter` SHALL each describe their use case and parameters
- **AND** SHALL NOT contain "MUST call" or "IMPORTANT: Call …first" in
  reference to `get_vault_guide`
- **AND** if any docstring mentions `get_vault_guide`, the reference
  SHALL be informational ("see", "for context", "describes")

### Requirement: Usage logs capture the new tools and parameters

Calls to `move_note`, `delete_note`, and `set_frontmatter` SHALL be
recorded via the existing `_tracked` decorator with `tool` set to the
respective tool name. Calls to `edit_note` that include `dry_run`,
`replace_all`, or `section` SHALL include those parameters in
`usage_logs.params` (subject to the existing string-truncation behavior
of `_tracked`).

#### Scenario: `move_note` invocation is logged

- **WHEN** an agent calls `move_note(from_path="A.md", to_path="B.md")`
- **THEN** a row SHALL be appended to `usage_logs` with
  `tool='move_note'` and `params` containing `from_path` and `to_path`

#### Scenario: `dry_run` flag is logged on `edit_note`

- **WHEN** an agent calls `edit_note(path, content, dry_run=True)`
- **THEN** the `usage_logs` row for that call SHALL have `tool='edit_note'`
- **AND** `params` SHALL include `dry_run`

### Requirement: No-clobber mutations are race-safe
`create_note` and `move_note` SHALL atomically fail if another actor creates the destination at any time before the operation commits. They MUST NOT implement no-clobber solely as an existence check followed by a replacing rename.

#### Scenario: Destination appears during create
- **WHEN** another actor creates the destination after validation but before `create_note` commits
- **THEN** `create_note` SHALL report that the destination exists
- **AND** SHALL leave the other actor's content unchanged

#### Scenario: Destination appears during move
- **WHEN** another actor creates the destination after validation but before `move_note` commits
- **THEN** `move_note` SHALL fail without replacing the destination
- **AND** the source note SHALL remain available

### Requirement: Note read-modify-write operations detect conflicts
`edit_note`, `set_frontmatter`, and backlink body rewrites SHALL compare the current on-disk content with the content on which the new result was computed immediately before atomic publication. They SHALL reject a mutation when that comparison observes a difference. This is optimistic conflict detection and does not claim coordination with a non-cooperating writer in the interval after comparison.

#### Scenario: External edit occurs concurrently
- **WHEN** Obsidian changes a note after the server reads it and the pre-publication comparison observes the new content
- **THEN** the server SHALL return a conflict
- **AND** SHALL NOT overwrite the newer external content

#### Scenario: Backlink rewrite conflicts after a move
- **WHEN** a backlink source changes before its post-move rewrite is published
- **THEN** the source's newer content SHALL remain unchanged
- **AND** `move_note` SHALL report partial success and identify that one or more link rewrites failed

### Requirement: Note tools reject hidden vault paths
All note mutation tools SHALL reject a source or destination containing a dot-prefixed path component, including `.obsidian` and `.trash`, except for the server's internal soft-delete destination handling.

#### Scenario: Hidden configuration mutation attempted
- **WHEN** a note mutation targets `.obsidian/plugins/example.md`
- **THEN** the operation SHALL return a validation error
- **AND** SHALL NOT modify the hidden file

### Requirement: Link rewrites preserve source-relative meaning
When `move_note(rewrite_links=true)` rewrites Markdown links, each resulting href SHALL resolve from the source note to the moved target. A moved note that links to itself SHALL be rewritten at its new path.

#### Scenario: Markdown source and target are in different folders
- **WHEN** `Folder/source.md` links to a target moved to `Archive/target.md`
- **THEN** the rewritten Markdown href SHALL resolve from `Folder/source.md` to `Archive/target.md`

#### Scenario: Moved note contains a self-link
- **WHEN** the moved note itself links to its old path and rewriting is enabled
- **THEN** its body at the destination SHALL link to the new path

### Requirement: Soft delete preserves concurrent trash entries
Soft delete SHALL publish the deleted note into `.trash` without replacing an existing or concurrently created trash entry. On a name collision it SHALL select another destination name.

#### Scenario: Trash destination appears concurrently
- **WHEN** another actor creates the chosen trash destination before soft delete publishes the note
- **THEN** soft delete SHALL preserve the existing trash entry
- **AND** SHALL move the deleted note to a distinct trash name

