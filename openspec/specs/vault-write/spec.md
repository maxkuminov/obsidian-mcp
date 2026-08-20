# vault-write Specification

## Purpose
TBD - created by archiving change vault-write-completion. Update Purpose after archive.
## Requirements
### Requirement: Atomic write invariant

The system SHALL perform all file writes from MCP write tools via a temporary file created in the same directory as the destination, whose contents are flushed to durable storage before publication, followed by an atomic same-directory rename (overwrite) or hard link (no-clobber) relative to the destination's directory descriptor. The applicable tools are `create_note`, `edit_note`, `move_note`, `delete_note`, and `set_frontmatter`. Direct writes that could leave the destination truncated on crash SHALL NOT be used, and the temporary file SHALL be created with exclusive, non-symlink-following semantics so a pre-created name cannot be written through.

#### Scenario: Crash mid-write does not truncate the destination

- **WHEN** the server process is killed between the tmp-file write and the
  publication
- **THEN** the destination file SHALL retain its prior content unchanged
- **AND** the orphaned `.tmp-*` file SHALL be discoverable for cleanup by
  the next reindex (it lives in a dot-prefixed name, so the indexer
  ignores it)

#### Scenario: Crash immediately after publication does not publish empty content

- **WHEN** the payload has been written to the temporary file and the system
  loses power immediately after the publishing rename
- **THEN** the destination SHALL hold either the full prior content or the full
  new content, because the payload was flushed to durable storage before the
  rename was issued

#### Scenario: Successful write atomically replaces existing content

- **WHEN** `edit_note` is called with new content and succeeds
- **THEN** any reader observing the destination path SHALL see either the
  full prior content or the full new content, never a partial mix

#### Scenario: A no-clobber write exposes no staging name

- **WHEN** `create_note` or `write_file` (without `overwrite`) stages its payload
- **THEN** no directory entry for the staged content SHALL exist at any point before publication
- **AND** the staged content SHALL be published by descriptor, so that no name a third party could take over is consulted
- **AND** no cleanup of a staging name SHALL be required or performed

#### Scenario: The staging file of an overwrite is replaced before publication

- **WHEN** another process detaches an overwrite's staged temporary file from its name — by unlinking it or renaming a different file over it — after the payload has been flushed and before publication
- **THEN** the substituted file's contents SHALL NOT be published at the destination
- **AND** the destination SHALL hold either its prior content or the content this call staged, never a third party's
- **AND** the substituted file SHALL be left in place rather than unlinked by the cleanup

#### Scenario: The filesystem cannot stage without a name

- **WHEN** the vault filesystem does not support staging an unnamed file
- **THEN** a no-clobber write SHALL be refused with an error naming the unsupported capability
- **AND** SHALL NOT fall back to staging under a name

#### Scenario: Staging happens in the destination directory

- **WHEN** any note or file write stages its payload
- **THEN** the temporary file SHALL be created in the destination's own
  directory, so publication is a same-directory operation
- **AND** the temporary file SHALL be removed whether the write succeeds or
  fails

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

The MCP server SHALL expose a tool `delete_note(path: str, permanent: bool = False) -> str`. With `permanent=False` (default), the tool SHALL move the note into `.trash/` inside the vault root under a name of the form `<YYYYMMDD-HHMMSS>-<original-basename>-<random suffix>`, creating `.trash/` if needed, using a single non-replacing rename so that an existing or concurrently created trash entry is never overwritten. With `permanent=True`, the tool SHALL unlink the file directly. In both cases the operation SHALL run relative to the parent directory descriptor opened at validation and the trash directory SHALL be resolved from the same vault-root descriptor. In both cases the response SHALL identify what happened and where the file went (or that it was permanently deleted). When the vault filesystem cannot perform a non-replacing rename into `.trash/`, the soft delete SHALL be refused with an error that names the limitation and points at `permanent=True`.

#### Scenario: Soft-delete moves the file under `.trash/`

- **WHEN** the client calls `delete_note(path="Cards/Old.md")`
- **THEN** the file SHALL be moved to a path under `.trash/` whose name begins
  with a timestamp and contains the original basename
- **AND** the response SHALL include the trash path

#### Scenario: Soft-delete is invisible to search

- **WHEN** a soft-deleted note has been moved into `.trash/` and the
  next reindex pass completes
- **THEN** the row in `notes_metadata` for that note SHALL be removed
- **AND** the dependent `note_embeddings` and `note_links` rows SHALL be
  cleaned up via existing FK cascades

#### Scenario: Permanent delete removes the file outright

- **WHEN** the client calls `delete_note(path="Cards/Old.md", permanent=True)`
- **THEN** the file SHALL be unlinked relative to the validated parent
  directory
- **AND** the response SHALL state that the file was permanently deleted

#### Scenario: Trash collisions are disambiguated

- **WHEN** the same note path is soft-deleted twice within the same second
- **THEN** each delete SHALL produce a distinct `.trash/` entry
- **AND** neither entry SHALL have overwritten the other

#### Scenario: Missing note returns an actionable error

- **WHEN** the client calls `delete_note` on a non-existent path
- **THEN** the response SHALL state that the note does not exist
- **AND** SHALL NOT create a `.trash/` directory

#### Scenario: Non-replacing rename unavailable

- **WHEN** the vault filesystem or kernel cannot perform a non-replacing rename into `.trash/`
- **THEN** `delete_note(permanent=False)` SHALL return an error naming the limitation
- **AND** the note SHALL remain at its original path

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

`create_note`, `write_file` (without `overwrite`) and `move_note` SHALL atomically fail if another actor creates the destination at any time before the operation commits. They MUST NOT implement no-clobber as an existence check followed by a replacing rename. `create_note` and `write_file` SHALL publish by hard-linking the staged temporary file, and `move_note` SHALL publish with a non-replacing rename, so that the destination is either created by the operation or the operation fails. When the vault filesystem cannot provide the required primitive, the tool SHALL refuse with an error naming the unsupported capability and SHALL NOT fall back to an operation that can replace an existing file.

#### Scenario: Destination appears during create
- **WHEN** another actor creates the destination after validation but before `create_note` commits
- **THEN** `create_note` SHALL report that the destination exists
- **AND** SHALL leave the other actor's content unchanged

#### Scenario: Destination appears during move
- **WHEN** another actor creates the destination after validation but before `move_note` commits
- **THEN** `move_note` SHALL fail without replacing the destination
- **AND** the source note SHALL remain available

#### Scenario: The source is replaced during a move

- **WHEN** another actor replaces the file at `from_path` with a different *regular file* after validation but before `move_note` commits
- **THEN** whichever file is at `from_path` when the move executes SHALL be relocated intact
- **AND** no file SHALL be unlinked that was not the one moved

#### Scenario: The source is replaced by a directory or a link during a move

- **WHEN** another actor replaces the file at `from_path` with a directory or a symbolic link after validation but before `move_note` commits
- **THEN** `move_note` SHALL detect that what arrived at the destination is the object it moved and is not a regular file, SHALL move it back with a non-replacing rename, and SHALL report the move as refused
- **AND** SHALL NOT update `notes_metadata` or `note_links`
- **AND** if the rollback cannot be performed, the error SHALL name the location the object now occupies so it can be recovered

#### Scenario: Something else takes the destination immediately after the move

- **WHEN** the file at the destination after the rename is not the object `move_note` moved — because a third party replaced it, or because the moved object could not be identified beforehand
- **THEN** `move_note` SHALL report the outcome, naming the destination, rather than raising
- **AND** SHALL NOT move anything back, because relocating that object would act on a name rather than on an identified file
- **AND** SHALL NOT update `notes_metadata` or `note_links`

#### Scenario: A move exhausts the process descriptor table

- **WHEN** `move_note(rewrite_links=True)` runs out of file descriptors while planning its rewrites
- **THEN** the whole move SHALL be aborted before any mutation, rather than recorded as a failure of the individual source
- **AND** the error SHALL say that descriptors ran out and suggest moving without `rewrite_links`

#### Scenario: Two link-rewriting moves run concurrently

- **WHEN** two `move_note(rewrite_links=True)` calls are in flight at the same time
- **THEN** their preflight-and-rewrite spans SHALL NOT overlap, so that the descriptor bound holds for the process and not merely for each call

#### Scenario: Hard links unavailable

- **WHEN** the vault filesystem refuses a hard link inside the vault root and a no-clobber note or file write is attempted
- **THEN** the tool SHALL return an error naming hard links as the unsupported capability
- **AND** any existing file at the destination SHALL be unchanged

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

### Requirement: Note write tools bound the resulting note size

`create_note`, `edit_note`, `set_frontmatter`, and the link-rewriting path of `move_note(rewrite_links=True)` SHALL refuse to produce a note whose UTF-8 encoded content exceeds `MAX_NOTE_BYTES` (10 MiB). The check SHALL be applied to the content that would be written (after the edit, frontmatter mutation, or link rewrite is computed), SHALL return a tool-level error that names the limit, and SHALL NOT write that file. Under `edit_note(dry_run=True)` the same error SHALL be reported instead of a diff. For `move_note(rewrite_links=True)`, all rewrites SHALL be computed before any mutation; if any rewritten source would exceed the cap the tool SHALL abort the entire move before touching the filesystem or the database, returning an error that names the offending source and the limit, so vault bytes and `note_links` never disagree, and SHALL abort before any mutation when the aggregate bytes held for the rewrite (originals plus rewritten content) would exceed `MAX_MOVE_REWRITE_BYTES` (256 MiB), naming the count and the limit. Every note-writing path therefore has a tool-level size cap strictly below the MCP transport body limit, so a supported write is never rejected only by the transport.

#### Scenario: edit_note result over the cap

- **WHEN** `edit_note` is invoked in any mode such that the resulting note would exceed `MAX_NOTE_BYTES`
- **THEN** the tool SHALL return an error naming `MAX_NOTE_BYTES`
- **AND** the note on disk SHALL be unchanged

#### Scenario: set_frontmatter result over the cap

- **WHEN** `set_frontmatter` is invoked with updates whose serialized result would push the note over `MAX_NOTE_BYTES`
- **THEN** the tool SHALL return an error naming `MAX_NOTE_BYTES`
- **AND** the note on disk SHALL be unchanged

#### Scenario: move_note link rewrite over the cap

- **WHEN** `move_note(rewrite_links=True)` would expand a source note's links such that the rewritten note exceeds `MAX_NOTE_BYTES`
- **THEN** the move SHALL NOT happen: the note stays at its original path, no source is rewritten, `notes_metadata`/`note_links` are unchanged, and the tool returns an error naming the offending source and the limit
- **AND** when no source is over the cap, rewrites proceed with the `expected=` conflict guard as before

#### Scenario: move_note aggregate rewrite bound

- **WHEN** `move_note(rewrite_links=True)` has backlink sources whose originals plus rewritten content sum to more than `MAX_MOVE_REWRITE_BYTES`, even though every individual source is under `MAX_NOTE_BYTES`
- **THEN** the move SHALL NOT happen: the note stays at its original path, no source is rewritten, `notes_metadata`/`note_links` are unchanged, and the tool returns an error naming the number of notes involved and the limit

#### Scenario: Result at the cap is accepted

- **WHEN** an `edit_note` or `set_frontmatter` call produces a note of exactly `MAX_NOTE_BYTES` bytes
- **THEN** the write SHALL succeed

### Requirement: Mutating note tools act on the named path and refuse symlinked final components

`create_note`, `edit_note`, `set_frontmatter`, `move_note` (source and destination), `delete_note` and `write_file` SHALL operate on the directory entry named by the path — the resolved parent directory (which MUST be inside the vault) joined with the final component as named — and SHALL refuse with an error naming the link's canonical vault-relative target when that final component is a symbolic link (including a dangling one). Symbolic-link directory components that resolve inside the vault SHALL remain permitted; the tools' database updates (`notes_metadata.file_path`, `note_links`, backlink discovery for `rewrite_links`) SHALL use the resolved vault-relative path, matching what the indexer stores for files under such directories. Read tools are unchanged and MAY follow links.

#### Scenario: Alias note is not retargeted

- **WHEN** `alias.md` is a symlink to `important.md` and any mutating note tool is invoked on `alias.md`
- **THEN** the tool SHALL return an error naming `important.md`
- **AND** `important.md` and the link SHALL be byte-identical afterwards

#### Scenario: Symlinked folder inside the vault still works

- **WHEN** `Shared/` is a symlink to `Real/` inside the vault and `create_note("Shared/new.md", …)` is invoked
- **THEN** the note SHALL be created in `Real/`

#### Scenario: Move through a symlinked folder keeps the index consistent

- **WHEN** `Real/A.md` is indexed, `Shared -> Real`, and `move_note("Shared/A.md", "Shared/B.md", rewrite_links=True)` is invoked
- **THEN** the file SHALL move to `Real/B.md`, `notes_metadata.file_path` and `note_links` SHALL be updated for `Real/A.md` → `Real/B.md`, and backlinks to `A` SHALL be rewritten

#### Scenario: An ancestor repointed mid-mutation cannot redirect the write

- **WHEN** a tool reads a note through a symlinked ancestor directory and that ancestor is repointed at a different directory before the write is published
- **THEN** the write SHALL land in the directory that was validated at the start of the call
- **AND** the directory the link now points at SHALL be unchanged, even when it holds a byte-identical copy of the note

#### Scenario: Soft delete through a symlinked folder

- **WHEN** `Shared -> Real` and `delete_note("Shared/note.md")` is invoked
- **THEN** `Real/note.md` SHALL be moved into `.trash/` and the response SHALL include the trash path

#### Scenario: Multi-user vault root

- **WHEN** the same alias case occurs under a per-user vault root
- **THEN** the refusal and the canonical target SHALL be computed relative to that user's root

#### Scenario: Dangling link at a destination

- **WHEN** `create_note` or `move_note` targets a path whose final component is a dangling symlink
- **THEN** the tool SHALL return an error and SHALL NOT write

#### Scenario: Escaping link still rejected

- **WHEN** a path component links outside the vault root
- **THEN** the existing traversal error SHALL be returned

### Requirement: Note mutations are anchored to the parent directory opened at validation

Every mutating note tool SHALL open the validated parent directory as a descriptor before it acts, and SHALL perform every subsequent filesystem operation of that call — temporary-file creation, the pre-publication read, publication, permanent deletion, and the soft-delete rename — relative to that descriptor. The applicable tools are `create_note`, `edit_note`, `move_note`, `delete_note`, `set_frontmatter`, and `write_file`. After validation, no pathname SHALL be resolved again by the kernel for that call.

The parent descriptor SHALL be obtained by walking the *resolved* parent path from an open vault-root descriptor, one component at a time, refusing to follow a symbolic link at any component. Symbolic-link directory components that resolve inside the vault therefore remain permitted (they are resolved before the walk); a component that is a symbolic link at walk time SHALL be refused.

A missing parent directory SHALL NOT be created during validation. It SHALL be created on first use of the descriptor by a write, so a call refused for an unrelated reason leaves no directories behind, and reads SHALL NOT create it at all.

#### Scenario: The validated parent is renamed and a symlink left at its name

- **WHEN** a mutating note tool has validated `Folder/note.md`, and before the tool publishes, another process renames `Folder` away and creates a symbolic link named `Folder` pointing at a different in-vault directory that holds a byte-identical `note.md`
- **THEN** the mutation SHALL take effect in the directory that was validated, under its new name
- **AND** the note in the directory the link now points at SHALL be unchanged
- **AND** the tool SHALL report success for the path the caller named

#### Scenario: The directory behind a symlinked vault root is renamed mid-call

- **WHEN** the configured vault root is a symbolic link, a mutating note tool has validated a path under it, and the directory the link points at is renamed away and replaced by a link to a different directory before the tool publishes
- **THEN** the mutation SHALL take effect in the directory that was validated
- **AND** the substituted directory SHALL be unchanged

#### Scenario: A soft delete's trash destination is anchored to the same root

- **WHEN** the vault root's target is substituted between validation and the soft delete of a note
- **THEN** the note SHALL be moved into the `.trash` directory of the root that was validated
- **AND** no `.trash` directory SHALL be created in the substituted directory

#### Scenario: A leaf that becomes a symbolic link after validation is named as one

- **WHEN** the final component of a validated path is replaced by a symbolic link before the tool reads or writes it
- **THEN** the tool SHALL return an error identifying the path as a symbolic link
- **AND** SHALL NOT report the note as missing
- **AND** SHALL NOT follow the link or modify anything

#### Scenario: A creating tool refused by the swapped leaf says why

- **WHEN** `create_note`, `write_file`, or the destination of `move_note` names a path that is absent at validation and holds a symbolic link by the time the tool publishes
- **THEN** the tool SHALL return an error identifying the path as a symbolic link, rather than a bare "already exists" message
- **AND** `write_file(overwrite=True)` SHALL NOT replace the link and report a successful write
- **AND** the link and the file it points at SHALL both be unchanged

#### Scenario: A move that would pin more descriptors than the process can spare

- **WHEN** `move_note(rewrite_links=True)` plans more link rewrites than the running process can hold open parent descriptors for
- **THEN** the move SHALL be aborted before any mutation: the note stays at its original path, no source is rewritten, and `notes_metadata`/`note_links` are unchanged
- **AND** the error SHALL name the limit and suggest moving without `rewrite_links`

#### Scenario: The vault root is substituted between resolution and anchoring

- **WHEN** the directory the configured vault root resolves to is renamed away and replaced by a link to another directory while a path is being validated
- **THEN** the operation SHALL be refused
- **AND** neither the original directory's copy of the note nor the substituted directory's copy SHALL be modified

#### Scenario: A backlink source needing no rewrite holds nothing open

- **WHEN** `move_note(rewrite_links=True)` considers a backlink source that is missing, unreadable, or contains no link this move rewrites
- **THEN** that source's descriptor SHALL be released immediately
- **AND** the descriptors held at any point SHALL be bounded by the number of rewrites actually planned, not by the number of sources considered

#### Scenario: A refused write creates no directories

- **WHEN** a note write names a path whose parent directory does not exist and the call is then refused before writing (for example because the resulting note would exceed the size cap)
- **THEN** no directory SHALL have been created

