# vault-write Specification

## Purpose
TBD - created by archiving change vault-write-completion. Update Purpose after archive.
## Requirements
### Requirement: Atomic write invariant

The system SHALL perform all file writes from MCP write tools by staging the payload in the destination's own directory, flushing it to durable storage before publication, publishing it with an atomic same-directory rename (overwrite) or hard link (no-clobber) relative to the destination's directory descriptor, and flushing that directory once the publication has happened. The applicable tools are `create_note`, `edit_note`, `move_note`, `delete_note`, `set_frontmatter`, and `write_file`. Payload and directory durability are properties of the shared atomic-write helper, so **every** caller of it inherits them, including `write_file` in both its no-clobber and its `overwrite=True` mode; an implementation SHALL NOT satisfy this requirement for the note tools while omitting the flush for a raw-byte write that goes through the same helper. Direct writes that could leave the destination truncated on crash SHALL NOT be used. Where staging carries a name — the overwrite path, whose replacing rename has no by-descriptor form — that name SHALL be created with exclusive, non-symlink-following semantics so a pre-created name cannot be written through; the no-clobber path SHALL have no name at all, unless `vault_allow_named_staging_fallback` is set and the filesystem cannot stage an unnamed inode, which is the one opted-in case where it too stages under an exclusively created name.

The destination directory SHALL be flushed **after** the publishing rename or link, so that the directory entry the write created or replaced is durable and not only its contents. Flushing only the immediate parent leaves the entry that names *it* unflushed, so a crash can lose the whole new folder and with it a note the tool reported written. The directories to flush SHALL be the **complete ancestor chain** from the destination parent up to the vault root, innermost first — not only the directories the publishing call itself created. Per-call creation provenance is insufficient and SHALL NOT be relied on: a call that creates a directory and then aborts before publication flushes nothing, correctly, because it published nothing; the call that later succeeds finds that directory already present, records no creation of it, and would leave the entry naming it made durable by nobody. The obligation outlives the call that incurred it, and outlives the process, so no in-memory record of "who created what" can discharge it. The chain is bounded by path depth and a directory flush is metadata-only, so the conservative rule is also the cheap one. A failure of the destination-directory flush, or of any of those ancestor flushes, SHALL be logged and SHALL NOT turn a write that already landed into a reported failure: the payload was already durable, the previous content survives either way, and a note tool that reports a false failure is retried — `edit_note(append=True)` retried after a write that landed appends the same block twice. This is deliberately the opposite failure direction from the transfer path, where the source bytes are gone and the ambiguity must be surfaced instead.

**Durability is a property of every publication, not only of the staged-payload helper — and of every *caller* of it, not only the ones a tool name makes obvious.** A note tool publishes in three ways and all three write a directory entry that a crash can lose, so the requirement names them rather than scoping itself to the shared atomic-write helper: the staged-payload `rename`/`link` above; `move_note`'s `renameat2`, which writes **two** entries — the destination's new one and the source's removal — so **both** parent directories SHALL be flushed after it lands, and so SHALL both parents of a rollback rename that puts a source back; and the soft delete's `renameat2` into the trash, which SHALL flush the source's parent **and** the trash directory. A permanent delete's `unlink` SHALL flush the parent directory it removed the entry from. The soft delete's flushes belong to the shared primitive the note and file tools both reach it through, so `delete_file`'s soft delete SHALL get them too. In each case a crash that makes only one of the two entries durable leaves the vault holding the note twice or not at all, or holding an entry for a note the tool reported deleted.

**A link rewrite is one of those callers and SHALL NOT be exempt.** `move_note(rewrite_links=True)` publishes a rewritten body into every backlink source, through the same staged-payload helper, and each of those publications owes the same complete ancestor chain — a rewrite target may sit under directories an external writer created and never made durable, which is exactly the case the chain rule exists for. An implementation SHALL therefore keep a usable vault-root anchor available to every target it publishes through: without one the ancestor lookup is impossible and the flush silently degrades to the leaf's parent alone, which is a *quiet* exemption from this requirement rather than a visible one.

That anchor SHALL be reconciled with the descriptor budget rather than allowed to defeat it. The rewrite phase holds one target per planned rewrite and the number of backlink sources is unbounded, so giving each target its own root descriptor doubles the per-source cost and halves how large a move the process can afford. Since every rewrite target resolves the same vault root, a **single shared root descriptor** for the phase SHALL be permitted, and it SHALL be adopted by a target only after verifying that it names the same root inode that target's parent was proved beneath — a mismatch means the vault root pathname was repointed mid-call, which SHALL abort the whole move before any mutation rather than being reported as one source failing to rewrite. Whatever descriptors the phase retains SHALL be charged against the documented budget.

Every one of these flushes SHALL take the same failure direction as the write path's: **logged, and never turned into a reported failure**, for the same reason expressed for the operation at hand. The rename or the unlink has already happened; a tool that reports it as failed is retried, and a retried move or delete finds the source gone and either contradicts the vault or acts on whatever has since taken the name. Nothing is lost by absorbing the failure except a warning.

#### Scenario: Crash mid-write of an overwrite does not truncate the destination

- **WHEN** the server process is killed between the staging write and the
  publication of an **overwrite** write, whose staging carries a name
- **THEN** the destination file SHALL retain its prior content unchanged
- **AND** the orphaned `.tmp-*` file SHALL be discoverable for cleanup by
  the next reindex (it lives in a dot-prefixed name, so the indexer
  ignores it)

#### Scenario: Crash mid-write of a no-clobber write leaves nothing behind

- **WHEN** the server process is killed between the staging write and the
  publication of a **no-clobber** write, whose staging has no directory entry
- **THEN** nothing SHALL exist at the destination path
- **AND** no `.tmp-*` entry or any other directory entry SHALL be left in the
  destination directory for a sweep or a reindex to find, because the unnamed
  inode is reclaimed when the last descriptor closes

#### Scenario: Crash immediately after publication does not publish empty content

- **WHEN** the payload has been written to the temporary file and the system
  loses power immediately after the publishing rename
- **THEN** the destination SHALL hold either the full prior content or the full
  new content, because the payload was flushed to durable storage before the
  rename was issued

#### Scenario: The publishing rename is made durable

- **WHEN** a note write publishes its payload
- **THEN** the destination directory SHALL be flushed after the rename or link and before the tool returns

#### Scenario: A failed directory flush does not report a landed write as failed

- **WHEN** the destination directory's flush fails after the payload has been published
- **THEN** the tool SHALL report the write as successful
- **AND** the failure SHALL be logged
- **AND** the tool SHALL NOT return an error that would invite the caller to retry the write

#### Scenario: Successful write atomically replaces existing content

- **WHEN** `edit_note` is called with new content and succeeds
- **THEN** any reader observing the destination path SHALL see either the
  full prior content or the full new content, never a partial mix

#### Scenario: A no-clobber write exposes no staging name

- **WHEN** `create_note` or `write_file` (without `overwrite`) stages its payload
  on a filesystem that supports staging an unnamed inode, or on any filesystem
  when `vault_allow_named_staging_fallback` is not set
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
  and `vault_allow_named_staging_fallback` is not set (the default)
- **THEN** a no-clobber write SHALL be refused with an error naming the
  unsupported capability and the `VAULT_ALLOW_NAMED_STAGING_FALLBACK` setting
  that would opt into the fallback
- **AND** SHALL NOT fall back to staging under a name

#### Scenario: Named-staging fallback, opted in

- **WHEN** the vault filesystem does not support staging an unnamed file
  and `vault_allow_named_staging_fallback` is set
- **THEN** the no-clobber write SHALL stage a named temporary file, created
  `O_CREAT|O_EXCL|O_NOFOLLOW` through the same parent directory descriptor
  (`MutableTarget.dir_fd`, opened at validation) every other mutating write
  uses — no pathname SHALL be re-resolved to obtain it
- **AND** the write SHALL publish by hard-linking the staged file to the
  destination name, so an existing destination is still refused (`EEXIST`)
  rather than replaced
- **AND** this reopens the named-staging substitution window unnamed-inode
  staging exists to close: a directory entry for the staged content exists,
  observable and replaceable, between staging and publication
- **AND** a `WARNING` SHALL be logged exactly once per process, the first
  time the fallback is actually exercised — that is, once a staging name has
  been created, and not when the setting is enabled, not when a probe selects
  the mode, and not when the creation of that name failed every attempt
- **AND** that warning SHALL state where the exercising path stages, and
  SHALL NOT attribute one path's staging location to the other: a note write
  stages beside its destination in an ordinary vault directory, a transfer
  stages in its own hidden staging directory, and the note path's window is
  the wider of the two
- **AND** `/health` SHALL report `vault_named_staging_fallback_active: true`
  once the fallback has been exercised in that process, and SHALL NOT report
  it for a process whose only attempt to stage under a name failed

#### Scenario: A staging name is not removed unless it is provably ours

- **WHEN** a write that staged under a name reaches its cleanup without an
  identity for what it staged — the `fstat` of the staged descriptor failed
  after the exclusive creation, or the name was already gone when the
  publication looked for it
- **THEN** the cleanup SHALL NOT unlink the name
- **AND** the file at that name SHALL be left in place and the fact logged,
  because a no-clobber write that published nothing must not destroy a file
  that took the name over — the same destructive-write class as unlinking an
  identified substitute
- **AND** this SHALL hold for both write paths, since they clean up through
  the same primitive

#### Scenario: A staging name disappears before publication

- **WHEN** a write's staging name is absent at cleanup time and the write did
  **not** publish
- **THEN** the cleanup SHALL report the disappearance rather than treating it
  as the ordinary consumed-by-publication case
- **AND** an absent staging name SHALL be quiet only when the write published,
  because that is the `renameat` that consumed it

#### Scenario: Staging happens in the destination directory

- **WHEN** any note or file write stages its payload
- **THEN** the staged inode SHALL be allocated in the destination's own
  directory, so publication is a same-directory operation
- **AND** where that staging carries a name — the overwrite path, and the
  no-clobber path under the opted-in named-staging fallback — the name SHALL
  be removed whether the write succeeds or fails, and only while it still
  refers to the inode this call staged

#### Scenario: A move's rename is made durable at both ends

- **WHEN** `move_note` publishes its `renameat2` from one directory to another
- **THEN** the destination's parent directory SHALL be flushed after the rename, and so SHALL the source's
- **AND** every directory above each end, up to the vault root, SHALL be flushed as well — not only the ones this call created
- **AND** a failure of any of those flushes SHALL be logged and SHALL NOT turn a move that already landed into a reported failure

#### Scenario: A move that is rolled back is made durable too

- **WHEN** a move is refused after its rename landed — the destination held the caller's inode but is a directory or a symbolic link — and the tool renames it back
- **THEN** both parent directories SHALL be flushed after the rollback rename lands
- **AND** the refusal SHALL still be reported to the caller, unchanged by whether those flushes succeeded

#### Scenario: A soft delete's rename is made durable

- **WHEN** a note or a file is soft-deleted by renaming it into the trash directory
- **THEN** the source's parent directory SHALL be flushed after the rename, and so SHALL the trash directory
- **AND** a failure of either flush SHALL be logged and the delete SHALL still be reported as the success it is
- **AND** the same SHALL hold for the rollback rename that puts back a directory the soft delete refuses to take

#### Scenario: A permanent delete's unlink is made durable

- **WHEN** a note or a file is deleted permanently
- **THEN** the parent directory the entry was removed from SHALL be flushed after the unlink
- **AND** a failure of that flush SHALL be logged and SHALL NOT be reported as a failed delete, because the file is already unlinked and a retry would act on whatever now holds the name

#### Scenario: A backlink rewrite is made durable to the same depth as any other write

- **WHEN** `move_note(rewrite_links=True)` rewrites a backlink in a source note that lives several directories deep, and an outer directory on that source's path was created by another writer and never flushed
- **THEN** the rewrite's publication SHALL flush every directory above that source's parent, up to the vault root, innermost first — exactly as a `create_note` into the same path would
- **AND** the descriptor arrangement that makes the lookup possible SHALL NOT pin one root descriptor per rewritten source

#### Scenario: The vault root is repointed while rewrites are being planned

- **WHEN** a rewrite target cannot be shown to have been validated against the same vault root the move is anchored to
- **THEN** the move SHALL be aborted before its rename runs, so nothing is moved, rewritten or reindexed
- **AND** it SHALL NOT be reported as a single source failing to rewrite, because the root itself moved and every remaining target is equally suspect

#### Scenario: A newly created folder is durable too

- **WHEN** `create_note("New/Folder/x.md", …)` creates `New` and `Folder` and
  then publishes the note
- **THEN** every directory above the destination parent, up to the vault root,
  SHALL be flushed as well — not only the ones this call created, because a
  directory an aborted attempt created and never flushed is indistinguishable
  from one this call found
- **AND** a failure of any of those flushes SHALL be logged and SHALL NOT be
  reported to the caller as a failed write

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
  (`section=<sel>`), a paged window (`offset>0`, or `truncated` true in
  the structured result), or otherwise less than the whole body, and its
  `content` is passed to default full-replace
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
ATX heading (1–6 `#` characters) and SHALL replace that heading's **body** with
the supplied `content`. The matched heading line itself SHALL NOT be removed or
rewritten.

A section's body SHALL begin at the first byte of the line immediately
following the heading line, and SHALL end immediately before the next heading of
equal-or-shallower depth, or at end of file. At most one line terminator (LF,
CRLF as a unit, or a lone CR) separates the heading line from the body; a
heading at end of file with no trailing terminator has an empty body. No
whitespace, blank line, or fenced code block — as recognised by the shared code
masker — between the heading line and the next heading of equal-or-shallower
depth SHALL be excluded from the body: a section has no third region between
its heading line and its body.

A separator newline SHALL be inserted around the replacement only when the
replacement body is **non-empty**: one before it when the retained prefix does
not already end in a terminator (an end-of-file heading), and one after it when
a following heading would otherwise be glued to it. Replacing an empty body
with an empty body SHALL leave the note byte-identical — an unconditional
separator makes a section with no body grow a blank line on every round trip,
and makes an unterminated end-of-file heading grow a newline.

Because a section write replaces the whole body, content the caller does not
resend is **deleted**, fenced code blocks included. This is the contract, not
an accident; it SHALL be stated in the caller-visible documentation in those
terms.

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

Narrowing the body's start to the line after the heading line SHALL NOT change
which heading any selector resolves to. Heading depth, trimmed heading text,
document order, and therefore every `#N` ordinal SHALL be unchanged by this
requirement.

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

#### Scenario: A section round trip is byte-identical

- **WHEN** the `content` field of a **complete, unwindowed** section read for
  `<sel>` (`offset=0`, `truncated` false) is passed back unchanged as
  `edit_note(path, content=<content>, section=<sel>)`, on a note whose body
  newlines are LF, whose line-1 frontmatter is absent or valid, and which
  contains no unmatched indented fence opener — the notes for which a section
  write is admitted at all
- **THEN** the resulting file SHALL be byte-identical to the original
- **AND** this SHALL hold for every `#N` ordinal in the note, including
  sections whose body begins with a blank line, sections whose body begins
  with a fenced code block, sections with an empty body, and the final
  section of the note
- **AND** the guarantee SHALL be verified against the shared section helpers
  AND against the structured response itself: the section read's `content`
  field **is** the body, so no recovery procedure exists to get wrong
- **AND** it SHALL NOT extend to a windowed or truncated section response
  (`truncated` true) — writing such a window back replaces the whole body with
  the fragment and deletes the remainder, exactly as the note-read requirement
  already warns
- **AND** it SHALL NOT be read as weakening the refusal on a defective
  frontmatter block: such a note remains readable by section and refused for
  section writes, and the refusal takes precedence over the round trip
- **AND** the same precedence SHALL hold for the unmatched-indented-fence-opener
  refusal this change introduces: such a note remains readable by section, its
  selectors resolve for reads under the not-a-fence interpretation, and every
  section write to it is refused by name — selector parity between read and
  write is a claim about resolution on admitted writes, not a promise that
  every readable section is writable

#### Scenario: An empty section survives a round trip

- **WHEN** a note is `# A\n# B\nb\n` (the first section has no body) and
  the client calls `edit_note(path, "", section="#1")`
- **THEN** the resulting note SHALL be `# A\n# B\nb\n`, unchanged — no
  separator newline SHALL be inserted for an empty body
- **AND** on the note `# A` (a heading at end of file with no trailing
  terminator), the same call SHALL leave the note as `# A`

#### Scenario: A non-empty body is still separated from what surrounds it

- **WHEN** the client calls `edit_note(path, "- item", section="Notes")` on
  a note whose `# Notes` heading is the last line and carries no trailing
  newline
- **THEN** the heading SHALL NOT be glued to the content: the result SHALL
  be `# Notes\n- item`
- **AND** when a following heading exists, a terminator SHALL be ensured
  between the new body and that heading

#### Scenario: A fenced block under a heading is replaced, not duplicated

- **WHEN** a note is `# A\n` followed by a fenced code block recognised by
  the shared masker (whose content may itself contain `#`-prefixed lines)
  followed by `# B\nb\n`, and the client calls
  `edit_note(path, "new", section="#1")`
- **THEN** the fenced block SHALL be replaced by `new`
- **AND** the resulting note SHALL contain the fence exactly zero further
  times — it SHALL NOT be retained with `new` inserted after it
- **AND** the caller-visible documentation SHALL state that this is a
  deletion: a `content` that does not resend the block loses it

#### Scenario: Indented and longer-closed fences are covered by the masker

- **WHEN** a fence is indented by one to three spaces, or is closed by a
  run at least as long as its opener — shapes the `code-masking`
  capability's grammar recognises
- **THEN** this requirement's fenced-code guarantees SHALL cover it: a
  heading inside it is not selectable and does not bound a section
- **AND** the re-addressing this widening causes on notes containing such
  shapes is the declared break of the fence-grammar change (issue #150),
  superseding the residual this scenario previously declared out of scope

#### Scenario: A blank line after a heading belongs to the body

- **WHEN** a note is `# A\n\nold\n# B\nb\n` and the client calls
  `edit_note(path, "new", section="#1")`
- **THEN** the resulting note SHALL be `# A\nnew\n# B\nb\n` — the blank
  line is part of the replaced body, and a caller that wants it back
  SHALL include it in `content`
- **AND** repeating the read-strip-write round trip on the original note
  any number of times SHALL NOT change the file

#### Scenario: Trailing spaces on a heading line stay on the heading line

- **WHEN** a heading line carries trailing horizontal whitespace (spaces,
  tabs, or a non-ASCII space) before its terminator
- **THEN** that whitespace SHALL remain part of the heading line and SHALL
  NOT become part of the body
- **AND** the heading's trimmed text, and therefore its addressability by
  the exact-text and path-style selectors, SHALL be unchanged

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

The parent descriptor SHALL be obtained from the *resolved* parent path by a **single kernel-enforced beneath-root lookup** from an open vault-root descriptor, which refuses to follow a symbolic link at any component and refuses any resolution that would leave the root. It SHALL NOT be obtained by opening one component at a time: an ancestor renamed out of the vault between two such opens yields a parent descriptor outside the root, and every mutation anchored to it then lands outside the vault while the tool reports success for the path the caller named. Symbolic-link directory components that resolve inside the vault therefore remain permitted (they are resolved before the lookup); a component that is a symbolic link at lookup time SHALL be refused.

A missing parent directory SHALL NOT be created during validation. It SHALL be created on first use of the descriptor by a write, so a call refused for an unrelated reason leaves no directories behind, and reads SHALL NOT create it at all. Each missing directory SHALL be created through a directory descriptor obtained by a fresh beneath-root lookup of the prefix that already exists, and the directory descriptor the write then anchors to SHALL come from a fresh beneath-root lookup of the whole parent path performed after the creation, not from the creation itself.

Directory creation keeps a bounded residual that SHALL be stated rather than claimed closed: there is no beneath-root form of directory creation, so a prefix renamed out of the vault between its lookup and the single creation issued through it yields an empty directory outside the root. The bound is at most one such directory per component **per creation descent**, and it is an empty directory in a place the renaming process already controls — never a note, never note content, and never something the tool reports success about. No directory descriptor a creation produced SHALL be returned to a caller or used as a pathname anchor for a later operation.

What the lookup proves, and what it does not, SHALL be stated exactly, in the words every artifact of this change uses: **Every below-root directory descriptor a call uses as a pathname anchor comes from a lookup the kernel proved beneath the vault root at the moment it resolved, and no directory descriptor retained from a creation descent is ever returned to a caller or used as a pathname anchor — so no operation is ever redirected into a directory that was never beneath the root.** This is a claim about **directory** descriptors used as pathname anchors: a call's own staged payload descriptor is created by that call, is written, flushed and published through by descriptor, and never anchors a pathname lookup. A lookup does not, and cannot, promise where that directory will be a moment later: a directory descriptor keeps naming the same directory however its pathname is subsequently renamed, which is exactly the property that keeps a mutation on the directory the caller named rather than on a substitute left at its name. A process that renames that directory out of the vault after the lookup and before the publish therefore carries the whole call with it, and the note lands there while the tool reports success for the path the caller named. That is a retained residual of descriptor anchoring — unchanged by this change and inherent to it — and it SHALL be recorded as such rather than specified as prevented.

When the kernel or the container cannot perform a beneath-root lookup, the mutation SHALL be refused with an error naming the unsupported capability, and SHALL NOT fall back to a per-component walk.

#### Scenario: An ancestor is renamed out of the vault while the parent is being resolved

- **WHEN** a mutating note tool is resolving the parent of `A/B/note.md` and another process renames `<vault>/A` to a directory outside the vault root during that resolution
- **THEN** the tool SHALL either anchor to a directory the kernel resolved beneath the vault root or refuse
- **AND** SHALL NOT anchor to a descriptor produced by opening the path one component at a time, or to any directory whose containment the lookup did not establish
- **AND** SHALL NOT be redirected into a directory that was never beneath the root

#### Scenario: The anchored parent is renamed out of the vault after the lookup

- **WHEN** the lookup has returned a descriptor the kernel proved beneath the vault root, and another process then renames that directory — or an ancestor of it — to a location outside the root before the tool publishes
- **THEN** the mutation SHALL take effect in the directory that was resolved, wherever that directory has since been moved, and the tool MAY report success
- **AND** this SHALL be recorded as a retained residual of anchoring a call to a directory descriptor, not specified as prevented — the same property that makes the mutation land in the directory the caller named rather than in a substitute left at its name
- **AND** no other directory SHALL be written to: the call SHALL NOT be redirected into a directory the lookup did not resolve

#### Scenario: A parent created on first use is re-looked-up before it is written through

- **WHEN** `create_note("New/Folder/x.md", …)` creates the missing parent directories and then writes
- **THEN** the directory descriptor the write anchors to SHALL be the result of a beneath-root lookup performed after those directories were created
- **AND** each of those directories SHALL have been created through a directory descriptor obtained by a fresh beneath-root lookup of the prefix that already existed
- **AND** the note SHALL be created inside the vault root

#### Scenario: An ancestor is renamed out of the vault while missing parents are created

- **WHEN** `create_note("A/B/C/x.md", …)` is creating the missing directories and another process renames `<vault>/A` outside the root during that creation
- **THEN** no note and no note content SHALL be written through any directory descriptor that creation produced: the write SHALL anchor to a directory descriptor obtained by a fresh beneath-root lookup performed after the creation, or the call SHALL be refused
- **AND** what the race can leave outside the root SHALL be at most an empty directory per component, per creation descent — never a note and never note content
- **AND** the residual SHALL be documented rather than reported as prevented

#### Scenario: The beneath-root lookup is unavailable

- **WHEN** a mutating note tool runs where the kernel or the container cannot perform a beneath-root lookup
- **THEN** the tool SHALL return an error naming the unsupported capability
- **AND** SHALL NOT walk the path one component at a time instead
- **AND** nothing SHALL be written

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

### Requirement: A mutation confirms the caller's vault assignment immediately before each publishing operation
Every mutating note and file tool SHALL re-read the caller's vault assignment from the database immediately before **each** publishing operation it performs, and SHALL refuse when that assignment no longer equals the root the request bound at admission. The applicable tools are `create_note`, `edit_note`, `move_note`, `delete_note`, `set_frontmatter` and `write_file`. On refusal nothing further SHALL be written, published, renamed or unlinked, and no target directory SHALL be created.

The re-read SHALL be a fresh database read, not a lookup in the process vault cache or in the request's own bound snapshot. Both of those are the values being checked: the snapshot is bound once, at admission, and is deliberately immutable so the admission gate fails closed under a concurrent bulk cache warm, and the process cache is add-only from the indexer's side. Consulting either would compare a value with itself.

A confirmation SHALL cover exactly the publishing operation it precedes, and SHALL NOT be carried across an intervening `await`, database transaction, or subsequent publishing operation. Five of the six tools publish exactly once, so for them this is one confirmation per call. `move_note(rewrite_links=True)` publishes once for the move and once per planned link rewrite, with a metadata transaction of unbounded duration between the move and the first rewrite; a single confirmation reused across all of that would reintroduce, inside one call, the staleness this requirement exists to narrow. Each of those publications SHALL therefore carry its own confirmation.

The metadata transaction that follows the move SHALL NOT require a confirmation: it writes no vault bytes and it records a publication that has already occurred, so refusing it would leave the database describing a note that is no longer at that path.

The confirmation SHALL be enforced structurally rather than by convention, and the enforcement has three parts.

**One awaiting wrapper, and no retainable confirmation.** There SHALL be no public way to obtain a confirmation and hold it: the only entry point SHALL be an asynchronous confirmed-publication wrapper that awaits the assignment read and then invokes a **synchronous** publish callable before returning control to the event loop.

**The confirmation SHALL be leased to that callable's dynamic extent.** The wrapper SHALL activate it before the call and invalidate it in a `finally` on every exit path — normal return, exception, or a callable that retained the object — and `consume` SHALL refuse a confirmation that is not currently leased. Single consumption alone is insufficient and SHALL NOT be relied on: it bounds how many times a confirmation may be used and says nothing about *when*, so a callable that stores its confirmation and publishes with it after the wrapper has returned, and after a reassignment has committed, is otherwise obeyed.

**A successful publication SHALL have consumed exactly one confirmation.** A callable that returns normally without consuming the confirmation it was given SHALL be refused, because that is the shape of a publish path added outside the shared helpers.

Publish callables that would not have published by the time they return SHALL be refused rather than driven: coroutine functions, generator functions and async-generator functions, and likewise a *returned* coroutine, generator or async generator — a callable object whose `__call__` is a generator is none of the first three. The wrapper SHALL NOT invoke `close` or any other method on such a returned object: that is arbitrary code of a stranger's choosing, and the lease has already been revoked, so driving the object later cannot publish.

**The confirmation is intrinsically single-consumption and target-bound.** The consumed flag SHALL live on the confirmation itself rather than on the target it authorises, so one confirmation cannot be spent by two publications however it is attached, and the publish helper SHALL check the confirmation's user id and canonical assignment against the target's own before spending it. A confirmation taken for one user, or for one root, SHALL NOT authorise a publication into another's target.

**Every publish helper refuses an unauthorised publication.** The shared helpers SHALL refuse a mutation target for which no confirmation is presented, one already spent, or one taken for a different user or root, so a mutating tool added later cannot publish without confirming the assignment first, in the same way a tool added later cannot skip the admission gate. A refusal for a missing or unusable confirmation SHALL be distinguishable from a refusal for a changed assignment, because the first is a programming error and the second is an operational event.

**The rollback of a publication is covered by that publication's confirmation, through a narrowly scoped permit.** `move_note`'s inode verification may have to move the file straight back when what arrived at the destination is the source inode but is a directory or a symbolic link, and refusing that for want of a confirmation would strand the note somewhere nobody named. The forward move SHALL return a permit naming exactly the two targets it moved between, and that permit SHALL authorise exactly one reverse move between those same two targets — not a second forward move, not any other pair, and not itself twice. The permit SHALL NOT be a confirmation and SHALL NOT be usable as one. This is not a second confirmation and is not claimed to be: the rollback undoes the very publication the confirmation covered, synchronously, with no intervening `await`, so it lies inside that publication's window rather than opening a new one. Stamping the one confirmation onto both endpoints instead — so that either could spend it — SHALL NOT be done, because it makes a reusable token of a single-use fact.

**The permit SHALL be unforgeable and SHALL expire with the publication that issued it.** It SHALL be constructible only by a successful, confirmed forward move; a permit built by any other caller SHALL be refused rather than honoured, because it would otherwise authorise a rename for which no confirmation was ever taken. It SHALL be bound to the lease of the confirmation the forward move consumed and SHALL be refused once that lease has been revoked, and it SHALL additionally record the immutable `(user id, assignment, vault-relative path)` of each endpoint and refuse a rollback whose endpoints no longer carry them.

**Both ends of a move SHALL belong to one caller, one assignment and one pinned root directory.** A no-clobber move removes the source directory entry as surely as it creates the destination one, but only one confirmation is consumed for the pair. The publish helper SHALL therefore require, before consuming anything and on the rollback path too, that the two targets carry identical user ids and identical canonical assignments, and that their pinned vault-root descriptors name the same directory inode — a pathname comparison is insufficient, since two assignments may spell the same string while different directories were pinned. Requiring this is what makes confirming the destination sufficient for the source; without it a source validated for one user can be removed under another user's confirmation.

**Every destructive operation on a mutation target SHALL go through such a helper, the permanent unlink included.** A tool that reaches a bare unlink syscall on a target's parent descriptor is outside the enforcement, and while any such call site remains the structural claim is false rather than merely incomplete. `delete_note(permanent=True)` is that call site today and SHALL be routed through a permanent-unlink helper on the same seam as the atomic write, the no-clobber move and the soft delete.

`user_id is None` outside multi-user mode has no user row to re-read; those calls SHALL be unaffected and SHALL issue no such query.

#### Scenario: Reassignment between admission and publication

- **WHEN** a request is admitted with one vault root bound, an administrator commits a reassignment to a different root, and the request's `create_note`, `edit_note`, `set_frontmatter` or `write_file` then reaches its publish
- **THEN** the call SHALL be refused with a tool error naming that the vault assignment changed while the call was in flight
- **AND** no file SHALL be created or modified in the former root
- **AND** no file SHALL be created or modified in the newly assigned root

#### Scenario: Unassignment between admission and publication

- **WHEN** the caller's vault assignment is cleared in that window
- **THEN** the call SHALL be refused and nothing SHALL be written

#### Scenario: The caller is deactivated between admission and publication

- **WHEN** the caller's account is deactivated, or its user row removed, in that window
- **THEN** the call SHALL be refused and nothing SHALL be written

#### Scenario: The confirmation is a fresh read, not a cache hit

- **WHEN** the database's assignment has changed but the process vault cache and the request's bound snapshot both still hold the previous root
- **THEN** the call SHALL still be refused

#### Scenario: An unchanged assignment publishes as before

- **WHEN** the assignment is unchanged between admission and publication
- **THEN** the mutation SHALL complete exactly as it does today
- **AND** a tool that publishes once SHALL have issued exactly one assignment re-read
- **AND** a tool that publishes more than once SHALL have issued exactly one re-read per publishing operation

#### Scenario: A move that rewrites links confirms before it commits

- **WHEN** `move_note(rewrite_links=True)` runs and the assignment has already changed when it reaches the operation that commits the move
- **THEN** the note SHALL NOT be moved, no source SHALL be rewritten, and `notes_metadata` and `note_links` SHALL be unchanged

#### Scenario: A move that rewrites links confirms again before every rewrite

- **WHEN** `move_note(rewrite_links=True)` commits the move under a valid confirmation and the assignment changes during the metadata transaction that follows
- **THEN** the first link rewrite SHALL be refused by its own confirmation
- **AND** no further source SHALL be rewritten, because every remaining rewrite would write into a vault the caller no longer holds

#### Scenario: A refusal part way through a move reports the partial outcome

- **WHEN** a link rewrite is refused after the move has already committed
- **THEN** the tool SHALL report that the move completed in the previous root, that the vault assignment changed while the call was in flight, and which sources were left unrewritten
- **AND** SHALL NOT report the move as a clean success
- **AND** the move itself SHALL NOT be rolled back, and the metadata rows recording it SHALL remain consistent with where the note now is

#### Scenario: The confirming read fails before the first publication

- **WHEN** the assignment re-read fails outright — the database is unreachable — before a call has published anything
- **THEN** the call SHALL fail rather than publish
- **AND** the failure SHALL NOT be reported as a changed vault assignment, because no administrator changed anything and the server cannot say whether one did

#### Scenario: The confirming read fails after the move has committed

- **WHEN** the assignment re-read fails outright before one of `move_note`'s link rewrites, after the move has already committed
- **THEN** the tool SHALL stop the remaining rewrites and report the partial outcome through the same mechanism a refusal uses: the completed move, the root it completed in, and every source left unrewritten
- **AND** it SHALL name the cause as a confirmation outage and SHALL NOT state that the vault assignment changed
- **AND** the move SHALL NOT be rolled back and the metadata rows SHALL remain consistent with where the note now is
- **AND** the call SHALL be recorded in `usage_logs` with an error marker distinct from both the changed-assignment marker and the missing-assignment marker

#### Scenario: A permanent delete is refused by a helper, not by a convention

- **WHEN** `delete_note(permanent=True)` reaches its unlink and the assignment has changed
- **THEN** the unlink SHALL be refused and the note SHALL remain at its path
- **AND** the refusal SHALL come from the same shared enforcement that covers the atomic write, the no-clobber move and the soft delete, rather than from a check written into the tool

#### Scenario: A soft delete confirms before it moves the note to trash

- **WHEN** `delete_note` runs and the assignment has changed
- **THEN** the note SHALL remain at its path and no `.trash` entry SHALL be created for it

#### Scenario: Every mutating tool inherits the confirmation

- **WHEN** any mutation target reaches a shared publish helper — including the permanent-unlink helper — without a confirmation for the operation about to be performed
- **THEN** the helper SHALL raise rather than publish
- **AND** the resulting error SHALL be distinguishable from the refusal a changed assignment produces

#### Scenario: A confirmation cannot be spent twice, on one target or on two

- **WHEN** a confirmation that has already authorised a publication is presented to a second one — through the same target or through a different target
- **THEN** the second publication SHALL be refused and nothing SHALL be written

#### Scenario: A confirmation is bound to the user and the root it was taken for

- **WHEN** a confirmation taken for one user id, or for one canonical assignment, is presented for a target validated for another
- **THEN** the publication SHALL be refused and nothing SHALL be written

#### Scenario: A confirmation cannot be held across a scheduling point

- **WHEN** the public interface for a confirmed publication is examined
- **THEN** there SHALL be no exported way to obtain a confirmation without publishing with it in the same synchronous step
- **AND** a publish callable that is asynchronous, or that returns an awaitable, SHALL be refused rather than awaited

#### Scenario: A callable that retains its confirmation publishes nothing later

- **WHEN** a publish callable stores the confirmation it was given, returns without consuming it, and the caller later presents that object to a publish helper — after an administrator's reassignment has committed
- **THEN** the wrapper SHALL refuse the callable's return, because nothing was consumed
- **AND** the retained confirmation SHALL authorise no publication, because its lease was revoked when the callable returned

#### Scenario: The lease is revoked when the callable raises

- **WHEN** a publish callable raises
- **THEN** its exception SHALL propagate unchanged
- **AND** the confirmation SHALL nonetheless be left unable to authorise a later publication

#### Scenario: A deferred publish callable is refused, not driven

- **WHEN** the publish callable is a coroutine function, a generator function or an async-generator function, or returns a coroutine, a generator or an async generator
- **THEN** it SHALL be refused
- **AND** the wrapper SHALL NOT call `close` or any other method on the returned object

#### Scenario: A move permit cannot be constructed by a caller

- **WHEN** any caller other than a successful confirmed forward move attempts to build a move permit
- **THEN** the attempt SHALL be refused, and no rename SHALL be authorised by it

#### Scenario: A move permit is inert once its publication has returned

- **WHEN** a permit issued inside a confirmed publication is presented after that publication has returned
- **THEN** the rollback SHALL be refused

#### Scenario: A move refuses endpoints that do not share a caller, an assignment and a root

- **WHEN** a no-clobber move is asked to move between two targets validated for different users, under different canonical assignments, or anchored to different vault-root directories
- **THEN** it SHALL be refused before any confirmation is consumed, and nothing SHALL be renamed

#### Scenario: A rollback is authorised by a permit, not by a spare confirmation

- **WHEN** the inode verification after a move has to put the file back
- **THEN** the reverse move SHALL be authorised by the permit the forward move returned, for exactly those two targets and exactly once
- **AND** the permit SHALL NOT authorise a second forward move, a move between any other pair of targets, or a second use of itself
- **AND** no confirmation SHALL be left unspent on either endpoint once the move stands

#### Scenario: The refusal is auditable

- **WHEN** a mutation is refused because the assignment changed
- **THEN** the call SHALL be recorded in `usage_logs` with the same allow-listed parameters as a successful call, carrying an error marker that names a changed vault assignment, and no additional field
- **AND** that marker SHALL be distinct from the marker a missing vault assignment produces at admission

#### Scenario: Single-user mode is unaffected

- **WHEN** a mutating tool runs in single-user mode
- **THEN** it SHALL issue no assignment re-read and SHALL behave exactly as before

#### Scenario: The read path gains no query

- **WHEN** a tool that does not mutate the vault is called — a search, a read, a listing or a graph tool
- **THEN** it SHALL issue no assignment re-read, and the admission gate SHALL remain a pure cache lookup performing no database work

### Requirement: The pre-publish confirmation is optimistic, and its residual is declared
The confirmation SHALL be documented as narrowing the window rather than closing it. A reassignment that commits after the confirming read and before the publishing operation completes — including one that commits while that operation is running — SHALL still take effect in the former root, and the tool MAY report success. The system SHALL NOT claim that a vault reassignment is linearizable with an in-flight mutation.

What changes is the size of the window: from the whole tool body — a read, a diff, a section resolve, a payload of up to the note size cap — down to staging, the durability flush and one publishing call. This is the same guarantee level the system declares for `edit_note(expected=…)` and for the transfer fingerprint check, and it is stated rather than implied.

That bound holds for **every** publishing operation, including each of `move_note`'s link rewrites, because each carries its own confirmation. The consequence is that a multi-publication tool has several such windows rather than one, and can be refused part way through; the partial outcome is specified and reported rather than swallowed. A claim that the residual is bounded by "one publishing call per tool call" would be false for `move_note` and SHALL NOT be made.

Closing the window would mean holding the credential and user rows locked across arbitrary vault I/O, which is the shape the transfer routes use and which those routes can afford because their publish is a bounded byte stream against an already-open session. Adopting it for the note tools would put a note read, a link-rewrite plan and an unbounded number of file writes inside a lock every authenticated request contends for, and it is rejected for that reason rather than overlooked.

#### Scenario: A reassignment inside the publish window is not prevented

- **WHEN** the confirming read succeeds and the reassignment commits before the publishing operation completes
- **THEN** the mutation MAY take effect in the former root and the tool MAY report success
- **AND** this SHALL be recorded as a declared residual rather than specified as prevented

#### Scenario: The narrowed window is what is claimed

- **WHEN** the guarantee is stated to an operator or in the design record
- **THEN** it SHALL be stated as a re-read immediately before publication, bounded by the publish operation, and SHALL NOT be stated as a lock held across the publish

#### Scenario: No row locks are held across vault I/O

- **WHEN** a mutating note tool performs the confirmation
- **THEN** it SHALL NOT hold a `SELECT … FOR UPDATE` on the credential or user row across the note read, the link-rewrite plan, the staging write or the publish

### Requirement: A cross-mount rename names the mount boundary as its cause

The non-replacing rename primitive SHALL classify `EXDEV` on its failure path before naming a cause, because `EXDEV` is not exclusively a mount-boundary signal: Landlock returns it for a same-mount reparenting a policy denies, and overlayfs for renames its layering cannot perform — so an unconditional mount-layout diagnosis is causally false on real same-mount configurations, the same defect class this change removes. On `EXDEV` the primitive SHALL compare the two directory descriptors' mount identities, inside that one failure path and never persisted: a definite mismatch SHALL raise the mount-boundary error (`MountBoundary`, the existing subclass of `UnsupportedFilesystem`) naming the mount layout; a definite match SHALL raise an error that names a security policy (Landlock) or filesystem-internal boundary (overlayfs, btrfs subvolume) as the plausible causes and SHALL NOT claim a mount boundary; and where mount identity cannot be read the error SHALL present `EXDEV` as ambiguous between those causes rather than assert either. All three remain distinct from the `EINVAL`/`ENOSYS`/`EOPNOTSUPP` cases that genuinely mean the kernel or filesystem cannot perform a non-replacing rename. Every caller of the primitive — the soft delete, `move_note`'s publication and its rollbacks — SHALL surface the classified cause rather than re-wrapping it into filesystem-support prose; a caller that wraps rename failures in its own message SHALL handle the mount-boundary subclass before the generic class, or its wrapper is a lie and the subclass handler unreachable. The note path's named-fallback publish mappings SHALL classify the same way — their staging is same-directory, so an unconditional mount claim there is false in almost every case that can fire.

#### Scenario: A same-mount `EXDEV` does not blame the mount layout

- **WHEN** `renameat2(RENAME_NOREPLACE)` returns `EXDEV` for two names whose directories provably share a mount (e.g. a Landlock policy denying the reparent, or an overlayfs rename restriction)
- **THEN** the raised error SHALL name a security policy or filesystem-internal boundary as the plausible causes
- **AND** it SHALL NOT claim the two names are on different mounts or tell the caller to change the mount layout

#### Scenario: An `EXDEV` whose mount identity cannot be read is reported as ambiguous

- **WHEN** the same rename fails `EXDEV` where `STATX_MNT_ID` is unavailable
- **THEN** the error SHALL present the failure as `EXDEV` with both a mount boundary and a policy or filesystem-internal boundary as possible causes, asserting neither

#### Scenario: A rename across a nested mount is refused with the mount-boundary cause

- **WHEN** a `renameat2(RENAME_NOREPLACE)` issued by a vault primitive returns `EXDEV` and the two directories' mount identities provably differ
- **THEN** the raised error SHALL be the mount-boundary type and its text SHALL name the mount layout as the cause
- **AND** the text SHALL NOT state or imply that the filesystem lacks non-replacing-rename support

#### Scenario: The genuinely unsupported cases keep their message

- **WHEN** the same primitive fails with `EINVAL`, `ENOSYS` or `EOPNOTSUPP`
- **THEN** the error SHALL remain the generic unsupported-filesystem refusal stating that a non-replacing rename is unavailable and there is no safe fallback

### Requirement: A soft delete across a mount boundary is refused with an accurate cause and an actionable workaround

The soft delete SHALL refuse to move a file into `.trash/` across a mount boundary with a mount-boundary error naming the layout — the file's directory and the vault root's `.trash` are on different mounts, which the rename cannot cross — and naming `permanent=True` as the workaround; it SHALL NOT blame `.trash/`'s ability to receive a non-replacing rename for a cross-mount failure. Where the kernel can answer the mount question (`STATX_MNT_ID`), a best-effort preflight comparing the source parent with the opened `.trash` descriptor SHALL raise that refusal before the rename is attempted; where it cannot answer, the preflight SHALL be skipped — never failed closed, because a kernel between the 5.6 floor and 5.8 serves same-mount soft deletes correctly today — and the rename's own `EXDEV` classification is the backstop — which, with the identity unreadable, presents the refusal as ambiguous rather than asserting a mount boundary it cannot prove. Both mount ids SHALL be read inside a single comparison immediately before use and never persisted. The behavior SHALL live in the shared soft-delete primitive, so `delete_note` (specified here) and `delete_file` (whose own requirement in `file-transfer` states the same refusal) cannot drift apart; soft-deleting into a per-mount trash is out of scope and the operation still fails on such a layout — only the reported cause changes.

#### Scenario: Soft delete of a file on a nested mount

- **WHEN** `delete_note(path)` or `delete_file(path)` soft-deletes a file whose directory is on a different mount than the vault root's `.trash/` (e.g. a directory of the same filesystem bind-mounted beneath the root)
- **THEN** the tool SHALL refuse with a mount-boundary error naming the mount layout as the cause and `permanent=True` as the workaround
- **AND** the file SHALL be untouched and nothing SHALL be created in `.trash/`
- **AND** the error SHALL NOT claim that `.trash/` cannot receive a non-replacing rename from the vault

#### Scenario: A kernel that cannot answer the mount question keeps its soft delete

- **WHEN** the same soft delete runs where `STATX_MNT_ID` is unavailable and the source and `.trash/` share a mount
- **THEN** the preflight SHALL be skipped and the soft delete SHALL proceed and succeed
- **AND** a cross-mount attempt on such a kernel SHALL still be refused by the rename's `EXDEV` classification, presented as ambiguous between a mount boundary and a policy or filesystem-internal boundary — the identity that would prove the mount claim is exactly what such a kernel cannot read

### Requirement: `move_note` refuses a cross-mount move naming the mount boundary

`move_note` SHALL refuse a move whose source and destination parents sit on different mounts with a mount-boundary error naming the layout, and SHALL NOT attribute the failure to missing filesystem support for the non-replacing rename. Where the kernel can answer the mount question, a best-effort preflight SHALL refuse before the rename, before any mutating database statement and before any commit — the read-only planning SELECTs the tool issues before the move (backlink planning, assignment confirmation) are permitted and change nothing — and the preflight itself SHALL create nothing: when the destination parent does not exist yet, the comparison SHALL run against the destination's deepest **existing** ancestor — a directory created beneath it lands on that ancestor's mount, the same reasoning the transfer mint preflight already uses — never by materializing the destination parent to compare against it. Where the kernel cannot answer, the preflight SHALL be skipped and the rename's `EXDEV` mapping is the backstop; on that path missing destination parent directories may have been created before the rename refuses, and what such a refusal can leave behind SHALL be at most empty directories — the same bounded residual already declared for creation descents — never a moved note, a lost note, or a database change. The preflight SHALL run only on the forward move: a rollback SHALL always attempt its rename, because refusing a rollback on a preflight strands the note at the destination, and a forward rename that landed proves both parents share a mount. A refused move SHALL update no database row — `notes_metadata` and `note_links` SHALL be untouched, including under `rewrite_links=True` with planned rewrites, and the refusal SHALL come before any source note is rewritten. Moves that stay on one side of a boundary SHALL be unaffected, and a copying fallback SHALL NOT be introduced — it breaks the guarantee that whichever inode is at the source when the call runs is what moves.

#### Scenario: A move across a nested-mount boundary

- **WHEN** `move_note("M/a.md", "a.md")` runs where `M/` is a mount beneath the vault root
- **THEN** the tool SHALL refuse with a mount-boundary error naming the mount layout as the cause
- **AND** the source note SHALL be untouched, nothing SHALL exist at the destination, and no mutating database statement SHALL be executed and nothing SHALL be committed — verified against the session (absence of DML plus row snapshots of `notes_metadata` and `note_links`), not inferred from the refusal text; read-only planning SELECTs are permitted — including under `rewrite_links=True` with at least one planned backlink rewrite, whose source notes SHALL be byte-identical afterwards
- **AND** the error SHALL NOT claim that `renameat2(RENAME_NOREPLACE)` is unavailable

#### Scenario: A cross-mount move to a missing destination folder creates nothing

- **WHEN** `move_note("M/a.md", "New/Sub/a.md")` runs where `M/` is a mount beneath the vault root and `New/Sub/` does not exist, on a kernel that can answer the mount question
- **THEN** the preflight SHALL refuse against the deepest existing destination ancestor's mount
- **AND** neither `New/` nor `New/Sub/` SHALL exist after the refusal

#### Scenario: A move on one side of the boundary still works

- **WHEN** a note is moved between two directories on the same mount, in a vault that also contains a nested mount elsewhere
- **THEN** the move SHALL proceed exactly as before this change

#### Scenario: A degraded kernel keeps its moves

- **WHEN** `move_note` runs where `STATX_MNT_ID` is unavailable and both parents share a mount
- **THEN** the preflight SHALL be skipped and the move SHALL succeed

### Requirement: The note path's named-fallback publish classifies its `EXDEV`

The note path's named-staging fallback SHALL classify `EXDEV` from its publishing link, and from its overwrite publish (`os.replace`), through the same comparison the rename primitive uses rather than letting either escape as a bare `OSError` or asserting a cause it has not measured. Because that fallback stages **in the destination's own directory**, the two ends of both publications are one directory descriptor, so the comparison answers "same mount" in every ordinary configuration that can produce this errno — and a message naming a mount boundary there would be false in almost every case it can fire, which is the defect class this change removes rather than an instance of the thing it fixes. A definite mismatch SHALL still raise the mount-boundary error, so an exotic layout gets the accurate answer and the branch is not dead. Of the remaining link errnos, only `EOPNOTSUPP` SHALL be described as the filesystem not supporting hard links; `EPERM` SHALL be described as hard-link publication being denied — pointing at permissions or security policy (seccomp/LSM) as well as filesystem support — because a security policy returns `EPERM` for `link` on filesystems whose hard links work fine. The two write paths SHALL use the same vocabulary.

#### Scenario: The fallback's no-clobber link fails `EXDEV`

- **WHEN** the named-fallback publish's hard link fails with `EXDEV` and its staging file sits in the destination's own directory, so the two ends provably share a mount
- **THEN** the raised error SHALL name a security policy or filesystem-internal boundary and SHALL NOT claim a mount boundary
- **AND** the same failure where the two ends' mount identities provably differ SHALL raise the mount-boundary error naming the layout, and where they cannot be read SHALL present both causes without asserting either
- **AND** an `EOPNOTSUPP` failure of the same link SHALL keep the message stating the filesystem does not support hard links
- **AND** an `EPERM` failure SHALL be reported as hard-link publication denied, naming security policy alongside filesystem support as possible causes

#### Scenario: The fallback's overwrite rename fails `EXDEV`

- **WHEN** the named-fallback overwrite publish's replacing rename fails with `EXDEV`
- **THEN** the raised error SHALL be the classified refusal — not a bare `OSError`, and not a mount-boundary claim unless the mount comparison supports one

### Requirement: Section-mode docstrings state the round-trip contract

Both layers' `edit_note` docstrings SHALL state that in section mode `content` is the section's **body only** — the registered wrapper in `server.py` (what an MCP client sees) and the implementation in `tools.py` alike. The body is exactly what a `read_note(section=…)` response's `content` field carries, beginning on the line immediately after the heading line.

The docstrings SHALL state that any blank line the caller wants between the
heading and its content belongs in `content`, and SHALL name
`read_note(section=…)` as the matching read: a section response carries the
heading line in its `heading` field and the body in its `content` field, and
`edit_note(section=…)` takes exactly that `content`.

They SHALL NOT prescribe a textual procedure for recovering the body from a
rendered response — no "split on the separator", no "drop the first line".
The structured `content` field is the recovery; the reason such procedures
were banned (any rendered envelope interpolates note-controlled values and can
be forged into an instruction that writes `**Path:** …` into the note) SHALL
stay recorded where a future author will find it.

They SHALL further state (a) that a section write **replaces the whole body**,
so content omitted from `content` — a fenced code block included — is deleted,
and (b) that byte-identity holds only for notes whose body newlines are LF.
Every non-LF terminator inside the **selected body** comes back as LF, whether
the note uses one dialect throughout or mixes them, because the read path
normalises and the write path rewrites raw bytes; terminators outside the
selected body are untouched, so a round trip can leave a note with more mixed
endings than it started with.

#### Scenario: An MCP client can learn the contract from introspection

- **WHEN** an MCP client introspects `edit_note`
- **THEN** the documentation for `section` SHALL say that `content` replaces
  the body beginning on the line after the heading line
- **AND** SHALL say that the whole body is replaced, so omitted content — a
  fenced code block included — is deleted
- **AND** SHALL name `read_note(section=…)` as the matching read and its
  `content` field as the exact body to pass back
- **AND** the same statements SHALL appear in the `tools.py` implementation's
  docstring, so the two layers do not diverge

### Requirement: Section addressing hides code per the code-masking grammar

Section resolution for `edit_note(section=…)` and `read_note(section=…)` SHALL scan text masked by the shared masker under the `code-masking` capability's grammar, so no heading inside any recognised fenced block is selectable, occupies a `#N` ordinal, or bounds a neighbouring section. The re-addressing consequence is declared: on a note containing a fence shape the previous masker missed (indented opener or closer, longer closer, unterminated column-zero fence), `#N` ordinals emitted before this change MAY shift, because a line inside code stops counting as a heading.

#### Scenario: An indented fence no longer exposes a heading to section writes

- **WHEN** a note is `# A\n   ```\n# Hidden\ntext\n   ```\n# B\nb\n` and a client calls `edit_note(section="#1", content="new")`
- **THEN** the section `A` SHALL be the entire span through the masked block (its body ending before `# B`), the write SHALL replace that whole body, and `# Hidden` SHALL NOT be selectable by any selector nor occupy an ordinal — `#2` SHALL resolve to `# B`

#### Scenario: A longer-closed fence is one opaque span

- **WHEN** a note is `# A\n```\n# Hidden\n````\n# B\nb\n`
- **THEN** section resolution SHALL treat the fenced span as body of `A`, a write to `A` SHALL replace it whole rather than splitting at `# Hidden`, and `# B` SHALL remain the note's second section — the closing line's terminator survives masking

#### Scenario: No heading below an unterminated column-zero fence is selectable

- **WHEN** a note opens a column-zero fence that is never closed, with `#`-prefixed lines below it
- **THEN** none of those lines SHALL be selectable sections, and the truncation outline SHALL NOT list them

#### Scenario: The outline and the resolver agree after the grammar change

- **WHEN** `read_note` emits a truncation outline for a note containing any newly-recognised fence shape
- **THEN** every `#N` in that outline SHALL resolve, via `read_note(section=…)` and `edit_note(section=…)`, to exactly the heading the outline listed

### Requirement: Section writes are refused on a note with an unmatched indented fence opener

When a note contains an indented (1–3 space) fence opener with no closing fence below it, `edit_note(section=…)` SHALL refuse the write, naming the unmatched opener and where it sits, because the block's true extent depends on container structure the flat grammar does not parse — under CommonMark the block could end at an enclosing list item's end, so any flat interpretation risks either splitting a code block or extending a section over real content. Reads (`read_note(section=…)` and the outline) SHALL keep working under the not-a-fence interpretation; as with defective frontmatter, the guarantee on such a note is the refusal, not the round trip. All other section-mode refusal semantics (naming, no write performed) follow the existing refusal patterns.

#### Scenario: The list-item fence shape cannot extend a section to end of note

- **WHEN** a note is `# A\n- item\n  ```\n  code\n\n# B\nkeep\n` and a client calls `edit_note(section="A", content="new")`
- **THEN** the call SHALL be refused without writing, the refusal SHALL name the unmatched indented opener, and `# B` SHALL still resolve as a section for reads

#### Scenario: A matched indented fence does not trigger the refusal

- **WHEN** every indented opener in a note has a closing fence
- **THEN** section writes SHALL proceed normally under the masked interpretation

#### Scenario: A rewrite-enabled move preflights every rewrite source

- **WHEN** `move_note(from, to, rewrite_links=True)` selects sources for link
  rewriting (the moved note's own body included) and any of them reports an
  unmatched indented fence opener
- **THEN** the whole move SHALL be refused before the rename is published,
  naming each such source and the opener's position, because rewriting would
  mutate text whose code/prose status the flat grammar cannot decide — a link
  inside an actual list-contained unterminated fence must not be silently
  rewritten
- **AND** the same move with `rewrite_links=False` SHALL be unaffected

#### Scenario: The refusal is disclosed where callers read

- **WHEN** the `edit_note` and `move_note` docstrings (the MCP-facing
  registrations and the implementation docstrings alike) describe when a call
  is refused
- **THEN** they SHALL disclose the unmatched-indented-fence-opener refusal
  alongside the defective-frontmatter refusal, and SHALL NOT advertise
  unqualified read/write selector parity on such notes
