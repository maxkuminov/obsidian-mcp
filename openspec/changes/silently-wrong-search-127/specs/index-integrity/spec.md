## ADDED Requirements

### Requirement: Exclusion-pattern changes reconcile on the next embed pass

An embed pass SHALL NOT rely on a content-hash mismatch to notice that the exclusion configuration changed. After processing the hash-mismatch backlog, the pass SHALL reconcile rows whose certification is current (`embedded_content_hash IS NOT DISTINCT FROM content_hash`, owner-scoped) against the *current* `EMBEDDING_EXCLUDE_PATTERNS`: a row whose path matches a pattern and still has vectors SHALL have them removed; a row whose path matches no pattern and has none SHALL be re-embedded. Every reconciliation write SHALL go through the certified predicate (`id + content_hash + file_path`, stamp before delete); a row that fails certification SHALL be rolled back and left for a later pass, never patched by id.

#### Scenario: Adding a pattern removes existing vectors

- **WHEN** a note was embedded, its `embedded_content_hash` equals its `content_hash`, and the operator then adds a pattern matching its path
- **THEN** the next embed pass SHALL certify the row (`id + content_hash + file_path`) and delete its vectors
- **AND** the note SHALL stop appearing in semantic search after that pass

#### Scenario: Removing a pattern restores vectors

- **WHEN** a note was stamped by the exclusion branch (certified, zero vectors) and the operator then removes the pattern that excluded it
- **THEN** the next embed pass SHALL re-read the note's bytes beneath the pass's pinned root, verify they hash to the row's `content_hash`, and embed it through the certified path
- **AND** the note SHALL appear in semantic search after that pass

#### Scenario: A concurrent move defeats the reconciliation write, not the vault

- **WHEN** the reconciliation decides about a row and the row's `file_path` changes before the certifying UPDATE commits
- **THEN** the certification SHALL match no row, the note's reconciliation SHALL be rolled back, and no vector SHALL be deleted or written on the strength of the stale decision

#### Scenario: A genuinely empty note is not rewritten every pass

- **WHEN** an included note's cleaned content produces zero chunks and its certification is current
- **THEN** the reconciliation SHALL write nothing for it

### Requirement: A move recomputes the stem-derived title

Both move paths — the indexer's id-preserving move detection and the `move_note` tool — SHALL leave `notes_metadata.title` equal to what a fresh index of the file at its new path would produce: the frontmatter `title` when one is present and non-empty, otherwise the new filename stem. The title SHALL be bounded to the column width.

#### Scenario: Renaming a note with no frontmatter title updates the title

- **WHEN** `Alpha.md` (no frontmatter `title`) is renamed to `Beta.md`, by either an external move the indexer detects or by `move_note`
- **THEN** index-backed tools SHALL report the note's title as `Beta` after the move is indexed

#### Scenario: An explicit frontmatter title survives a move

- **WHEN** a note whose frontmatter sets `title: Roadmap` is moved or renamed
- **THEN** its title SHALL remain `Roadmap`

### Requirement: Keyword indexing is bounded and can never abort a pass

The tsvector build (incremental pass and full rebuild alike) SHALL attempt the note's full content, and SHALL isolate each note's tsvector statement so that a per-note failure (including PostgreSQL's tsvector size limit) cannot abort the surrounding transaction. On failure the build SHALL retreat to a bounded prefix (halving, with a floor no lower than 100,000 characters) and, if every bounded attempt fails, skip that note's keyword vector, log it, and record the skip in the pass's skip list so an incomplete pass is not certified as complete.

#### Scenario: Terms beyond the former 100K slice are searchable

- **WHEN** a valid note carries a distinctive term at a position past 100,000 characters
- **THEN** after the next index of that note, `keyword_search` for that term SHALL return the note

#### Scenario: A pathological note degrades alone

- **WHEN** a note's full-content tsvector exceeds PostgreSQL's size limit
- **THEN** the pass SHALL retreat to a bounded prefix for that note only, the remaining notes SHALL be indexed normally, and the pass SHALL commit
- **AND** no index pass SHALL enter a state where the same statement re-aborts every subsequent tick

### Requirement: A many-chunk note completes, and certifies only on full coverage

The embedding batch deadline SHALL scale with the number of chunks (a per-chunk timeout still bounds a hung provider), so a note cannot be structurally unable to finish. A note SHALL be certified only when every one of its chunks produced a vector; no partial chunk coverage may ever be stamped complete.

#### Scenario: A giant note eventually embeds and stops being retried

- **WHEN** a note produces more chunks than the former fixed deadline allowed at normal provider latency
- **THEN** the embed pass SHALL process all of its chunks, certify it, and not select it again while its content is unchanged

#### Scenario: A hung provider still fails fast

- **WHEN** the embedding provider stops responding mid-batch
- **THEN** the in-flight chunk call SHALL time out at the per-chunk timeout and the note SHALL remain uncertified

#### Scenario: Partial coverage is never certified

- **WHEN** the provider returns fewer vectors than chunks for a note
- **THEN** the note SHALL NOT be certified and its previous vectors SHALL remain in place
