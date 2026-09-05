## ADDED Requirements

### Requirement: The write-precondition digest is defined in exactly one place

A file's `content_hash` SHALL be the string `"sha256:"` followed by the lowercase hexadecimal SHA-256 digest of the **complete raw bytes of that file as stored on disk**, and every producer and consumer of the digest across this server SHALL use that one definition.

The bytes hashed are every byte of the file, in file order, with no universal-newline translation, no frontmatter stripping, no windowing, and no re-encoding — the same bytes the read-modify-write tools compare through `expected=` immediately before publication, and the same digest the transfer fingerprint already computes over a file. The digest is therefore **not** a hash of any body, section, or rendered response field: a note carrying frontmatter, or CRLF terminators, has a `content_hash` that a hash of the text returned to the caller cannot reproduce, and every docstring that mentions the field SHALL say so.

The digest SHALL NOT be conflated with `notes_metadata.content_hash`, which hashes the universal-newline-translated text of a note and consequently differs for every file whose terminators are not LF. The `sha256:` prefix exists so that a comparison of the two fails uniformly rather than intermittently, and the index column's definition SHALL NOT be changed by this capability.

A tool that accepts a caller-supplied hash SHALL accept both the canonical `sha256:<64 hex>` form and a bare `<64 hex>` string, hexadecimal case-insensitive, and SHALL reject any other input — wrong length, non-hexadecimal characters, or an unrecognised algorithm prefix — as **malformed**, with a message naming the accepted form, never as a mismatch.

#### Scenario: The digest describes the file, not the returned body

- **WHEN** a note begins with a valid YAML frontmatter block and its body uses CRLF terminators
- **THEN** its `content_hash` SHALL equal the SHA-256 of the file's raw bytes, prefixed `sha256:`
- **AND** it SHALL NOT equal the SHA-256 of the `content` field any read of that note returns

#### Scenario: A malformed hash is refused as malformed

- **WHEN** a write tool is invoked with `expected_hash` set to a 63-character string, a non-hex string, or a value carrying an unknown algorithm prefix
- **THEN** the tool SHALL return an error naming the accepted form, SHALL NOT write, and SHALL NOT report a precondition mismatch

#### Scenario: Both accepted input forms behave identically

- **WHEN** the same digest is supplied once as `sha256:<hex>` and once as the bare `<hex>`, hexadecimal in either case
- **THEN** both calls SHALL reach the same verdict

### Requirement: Every overwrite path accepts an optional caller-supplied precondition

`edit_note` (in all four modes, `dry_run` included, and with `replace_frontmatter` set or unset), `set_frontmatter`, `write_file` with `overwrite=true`, `move_note`, and `delete_note` SHALL each accept an optional `expected_hash` argument that binds the call to the file's `content_hash`, and SHALL behave exactly as they do without it when it is omitted.

The argument SHALL never be required. A call that omits it keeps today's behaviour in full, including the absence of any conflict detection on `write_file(overwrite=true)`, so no deployed client is broken by this capability.

When it is supplied, the tool SHALL compare it against the digest of the bytes it read inside the call, and:

- a **match** proceeds to the write;
- a **mismatch** is an in-band refusal that names the file's current `content_hash`, states that nothing was written, and leaves the file, every other file, and every database row untouched;
- a **precondition that cannot be enforced** — supplied to a no-clobber creation (`create_note`, `write_file` with `overwrite=false`), or naming a file the tool cannot read within its byte cap — is a refusal naming the reason. It SHALL NOT be ignored: a caller that supplied a precondition believes the write was guarded by it.

For `move_note` the precondition binds the **source note's own bytes** and nothing else; the backlink sources a `rewrite_links=true` move rewrites are not bound, because the caller never read them.

The refusal message SHALL carry no note content — no excerpt, no diff, no length — and SHALL be distinguishable from the in-call conflict message so that a caller can tell "the bytes moved before I called" from "the bytes moved during my call".

The value of `expected_hash` SHALL be recorded in the tool's usage-log parameters, so that an operator can tell a guarded write from an unguarded one after the fact.

#### Scenario: A stale hash refuses and names the current one

- **WHEN** an agent reads a note, the note changes on disk, and the agent then calls `edit_note` with the `expected_hash` from its read
- **THEN** the tool SHALL return a precondition refusal naming the note's current `content_hash`, the file SHALL be byte-identical to what the concurrent writer left, and no `notes_metadata` or `note_links` row SHALL change

#### Scenario: An omitted hash keeps today's behaviour

- **WHEN** the same stale `edit_note` call is made with no `expected_hash`
- **THEN** the write SHALL proceed exactly as it does today, overwriting the concurrent change

#### Scenario: A matching hash proceeds

- **WHEN** `expected_hash` equals the file's current `content_hash`
- **THEN** the tool SHALL perform its write and report success

#### Scenario: A precondition on a creation is refused, not ignored

- **WHEN** `create_note`, or `write_file` with `overwrite=false`, is invoked with `expected_hash`
- **THEN** the tool SHALL return an error stating that a no-clobber creation has no incumbent bytes to bind, and SHALL NOT create the file

#### Scenario: An unreadable incumbent refuses rather than proceeding unguarded

- **WHEN** `write_file(overwrite=true, expected_hash=…)` targets a file larger than the read cap, so the incumbent bytes cannot be hashed
- **THEN** the tool SHALL refuse naming the cap, and SHALL NOT write

#### Scenario: A guarded delete

- **WHEN** `delete_note` is invoked with an `expected_hash` that no longer matches, with `permanent` either true or false
- **THEN** the note SHALL remain at its path, nothing SHALL be moved to `.trash` or unlinked, and the refusal SHALL name the current hash

#### Scenario: A guarded move binds the source only

- **WHEN** `move_note(from_path, to_path, expected_hash=…)` is invoked and `from_path`'s bytes no longer match
- **THEN** the refusal SHALL happen before the rename and before any link rewrite, the note SHALL stay at `from_path`, and no source note SHALL be rewritten

### Requirement: The precondition is checked immediately after the in-call read and before any other decision

A tool that receives `expected_hash` SHALL evaluate it immediately after the read of the incumbent bytes inside that call, and SHALL do so before mode dispatch, before the result size cap, before `dry_run` diff generation, and before any no-op or defect determination.

Ordering is observable and therefore normative: a unified diff, a "no changes" answer, or a frontmatter-defect report computed against a base the caller does not hold is a wrong answer, not a cheap one. A tool that reads nothing today when unguarded (`write_file`, `move_note`, `delete_note`) SHALL perform the read **only** when `expected_hash` is supplied.

#### Scenario: A dry run against a stale base refuses instead of diffing

- **WHEN** `edit_note(dry_run=true, expected_hash=…)` is invoked with a hash that no longer matches
- **THEN** the tool SHALL return the precondition refusal and SHALL NOT return a diff

#### Scenario: A stale precondition outranks a no-op

- **WHEN** `set_frontmatter` is invoked with a stale `expected_hash` and with `updates` that would have been a byte-identical no-op
- **THEN** the tool SHALL return the precondition refusal rather than "no changes"

#### Scenario: An unguarded call reads no more than it does today

- **WHEN** `write_file(overwrite=true)` is invoked without `expected_hash`
- **THEN** the tool SHALL NOT read the incumbent file

### Requirement: A section write's precondition is the whole file's hash

`edit_note(section=…)` SHALL accept the file's whole-file `content_hash` as its precondition and SHALL NOT define or accept any section-scoped digest.

A hash over a section body is unsound as a precondition because section selectors are positional: an `#N` ordinal names the Nth heading of the *current* document, so a body-only digest could certify an unchanged body while an insertion above it changed which section the selector resolves to. The consequence is declared rather than hidden — the mode with the smallest blast radius becomes the most conflict-prone, since an unrelated edit elsewhere in the file invalidates the precondition. That is acceptable only because the argument is optional, and both docstring layers SHALL say so.

#### Scenario: An edit elsewhere in the file invalidates a section precondition

- **WHEN** a caller reads `read_note(path, section="Tasks")`, another writer edits an unrelated section of the same note, and the caller writes back with `expected_hash`
- **THEN** the tool SHALL refuse with the precondition error naming the current hash

#### Scenario: A section read carries the whole file's hash

- **WHEN** `read_note(path, section=…)` returns a section body
- **THEN** the `content_hash` in that response SHALL be the whole file's, and passing it straight to `edit_note(section=…, expected_hash=…)` on an unchanged file SHALL succeed

### Requirement: Write tools report the content hash of the bytes they published

`create_note`, `edit_note`, `set_frontmatter` and `write_file` SHALL report the resulting file's `content_hash` in their success result, computed over the bytes that call published.

The value describes what this call wrote, not what is on disk at the moment the caller reads the message; the docstrings SHALL state that distinction. Reporting it makes a write→write chain guardable without an intervening read.

#### Scenario: A write reports a hash the next write can bind to

- **WHEN** `edit_note` succeeds and its result is followed immediately by a second `edit_note` supplying the reported hash as `expected_hash`
- **THEN** the second call SHALL proceed

#### Scenario: A dry run reports no hash

- **WHEN** `edit_note(dry_run=true)` returns a diff
- **THEN** the result SHALL NOT report a `content_hash`, because nothing was published

## MODIFIED Requirements

### Requirement: Note read-modify-write operations detect conflicts
`edit_note`, `set_frontmatter`, and backlink body rewrites SHALL compare the current on-disk content with the content on which the new result was computed immediately before atomic publication. They SHALL reject a mutation when that comparison observes a difference. This is optimistic conflict detection and does not claim coordination with a non-cooperating writer in the interval after comparison.

This comparison covers **one window: this call's own read through to this call's publishing rename.** It structurally cannot see a change that landed between a caller's earlier read and this call, because the bytes it compares are the ones this call read. The caller-visible `expected_hash` precondition covers that other window — the caller's read through to this call's read — and the two SHALL be documented as a pair, with neither described as subsuming the other and neither removed in favour of the other. A supplied `expected_hash` that matched therefore does not exempt a call from this comparison: a writer landing between the precondition check and the publication SHALL still be detected here.

`write_file(overwrite=true)` SHALL perform this comparison **only** on calls that supply `expected_hash` — on those calls the bytes read for the precondition are the ones compared before the rename — and SHALL remain an unconditional replace on calls that do not, which is its documented behaviour.

#### Scenario: External edit occurs concurrently
- **WHEN** Obsidian changes a note after the server reads it and the pre-publication comparison observes the new content
- **THEN** the server SHALL return a conflict
- **AND** SHALL NOT overwrite the newer external content

#### Scenario: Backlink rewrite conflicts after a move
- **WHEN** a backlink source changes before its post-move rewrite is published
- **THEN** the source's newer content SHALL remain unchanged
- **AND** `move_note` SHALL report partial success and identify that one or more link rewrites failed

#### Scenario: A matching precondition does not disable the in-call comparison
- **WHEN** `edit_note` is called with a matching `expected_hash` and another writer changes the note after the precondition check but before publication
- **THEN** the in-call comparison SHALL observe the difference and refuse with its own distinct conflict message
- **AND** nothing SHALL be written

#### Scenario: The two refusals are distinguishable
- **WHEN** a caller compares a stale-precondition refusal with an in-call conflict refusal
- **THEN** the two messages SHALL differ, and only the precondition refusal SHALL name the file's current `content_hash`
