## MODIFIED Requirements

### Requirement: The embedding pass is not gated on provenance, because it verifies every hash it certifies
The embedding pass SHALL verify that the content it read hashes to the content hash of the `notes_metadata` row it selected, SHALL skip the note and leave the row unmarked when it does not, and SHALL otherwise run for a user **whatever that user's provenance classification is**. These two halves are one requirement: the verification is the entire licence for the un-gating, and neither may be removed or weakened without the other.

Gating the embedding pass on a settled provenance was specified first and was wrong, because the two rules it sits between compose into indefinite staleness. A permanently unreadable file withholds the provenance record forever — deliberately, so that nothing certifies a root the pass could not fully visit — and the embedding gate then turns that withheld record into a permanent refusal to embed **anything** for that user. Meanwhile the scan keeps working: a readable note that the user edits gets a fresh `content_hash` on every pass, while its `note_embeddings` rows still hold the chunk text of the content it used to have. Semantic search reads those chunks without requiring `embedded_content_hash` to equal `content_hash`, so it returns excerpts of superseded content, indefinitely, to a consumer that is an agent and will act on them without a human ever seeing the query. One unreadable file would have converted the whole user's semantic search into a silently wrong one — the failure this system ranks above every expensive one.

The un-gating is sound only because of the verification, and the argument is exact. The gate existed to stop a pass from writing a row derived from one root against a metadata row derived from another. An embedding is a pure function of the note's content, and the verification refuses to embed any bytes that do not hash to the content hash the selected row records. So a chunk vector is written against a row **only** when the bytes it was built from are the bytes that row describes — which directory supplied them is not a fact the vector depends on. The pass therefore cannot mix roots: under a wrong root the hashes disagree and it skips, and under bytes that match the row the embedding is correct by construction.

The verification also SHALL NOT be understood as an optimisation. `embed_note` marks a row embedded by copying the *row's* `content_hash`, not a hash of the bytes it embedded, so without the check a file that differs from its row at embedding time is embedded and then permanently marked as embedded for a hash it does not have — nothing re-embeds it again. That is what makes the re-derive branch's retention of `note_embeddings` sound, and it is now also what makes this requirement's un-gating sound. Anyone proposing to remove it must re-gate the embedding pass in the same change, and this sentence is here so that consequence is visible at the site of the removal.

**Verifying the bytes is not sufficient on its own, because the row can move between the verification and the certification.** The pass verifies against the content hash from its initial query and then re-reads the metadata row, and that second read — in a later transaction — can return a hash another pass has committed since. Copying *that* value onto vectors built from the verified content marks the row embedded for content it does not have, and because the pass selects rows whose embedded hash differs from their content hash, the resulting equality then blocks every later repair: permanently wrong semantic results for a consumer that acts on them without a human seeing the query.

So the certification SHALL be a conditional write, in the same transaction as the vector replacement, requiring the row still to have the same id, the same relative path and the same content hash the bytes were verified against; and the value it writes SHALL be that verified hash, never one re-read from the row. The conditional write SHALL be issued **before** any stored vector is deleted or inserted, so the row lock it takes holds for the remainder of the transaction, and **after** the embedding provider call, so no row lock is held across a network request. When it matches no row the generated vectors SHALL be discarded, no stored vector SHALL be deleted or inserted, the row SHALL be left unmarked, and a later pass SHALL embed it as it then stands.

**Every path that marks a row embedded SHALL use that same conditional write, the exclusion branch included.** That branch reads no file and computes no vector, but it deletes the note's stored vectors and marks the row embedded from the hash it selected, which is the same claim about the same row — and a move is precisely the change it cannot see, because relocating a note changes its relative path while leaving its content hash untouched. Marking by row id alone therefore lets a decision taken about an excluded path delete the vectors of a row that has since become an *included* one and record it as embedded with none; the row's content hash then equals its embedded hash, so no later pass ever selects it and the note is silently and permanently absent from semantic search. Including the relative path in the predicate makes the moved row match nothing, and the branch SHALL then discard the decision and roll back rather than delete anything.

**A change of `file_path` SHALL invalidate the embedding certification.** The conditional predicate above closes only one ordering — a move that commits *before* the certification. The mirror ordering is not something a predicate can see: when the move happens *after* a correct certification has committed, the stamp is already there and already true of the content. It is no longer true of the *decision*, because `embedded_content_hash` records that a row's current content has been dealt with and says nothing about **how**, while the exclusion branch decides how by matching the exclusion patterns against the path. A move therefore changes the answer without changing any content, and a stamp carried across it freezes the old answer permanently: the pass selects on `embedded_content_hash IS NULL OR embedded_content_hash != content_hash`, which a preserved stamp makes false for ever.

Both boundary directions are wrong and both are permanent. A note moved *out* of an excluded folder keeps a stamp the exclusion branch wrote after deleting its vectors: it is now included, has no vectors, is never selected again, and is silently absent from semantic search with nothing to indicate it. A note moved *into* an excluded folder keeps the vectors it was embedded with and stays searchable although it is now excluded.

**Every statement that changes `file_path` SHALL therefore set `embedded_content_hash` to null in the same statement.** That is `move_note`'s metadata update and the index pass's id-preserving move detection; the ordinary prune-and-insert path is unaffected, because a row it replaces starts from null anyway. Null SHALL be understood as "re-evaluate at the next pass" rather than as "not embedded": a note whose content did not really change is re-embedded only because the selection predicate picks it up, and the exclusion decision is re-taken against the path the row now has. Clearing unconditionally is deliberate and SHALL NOT be replaced by consulting the exclusion configuration at move time: the configuration can change between the move and the next pass, so a decision taken at move time is the same frozen answer in a new place, and the move path would gain a dependency on embedding configuration it otherwise has no reason to know about.

The pass SHALL work two explicit eligibility sets, and nothing else about it changes. The **backlog** is the set it has always selected — rows whose `embedded_content_hash` is null or differs from their `content_hash` — processed exactly as before. The **reconciliation sweep**, run after the backlog, is the set of rows whose certification is current (`embedded_content_hash IS NOT DISTINCT FROM content_hash`), consulted only to detect disagreement between the current exclusion configuration and the row's stored vectors (see the reconciliation requirement); every write it performs goes through the same conditional certification as the backlog. The pass still reads beneath the root it pinned, and still writes nothing for a note it skipped. The verification governs the path that *embeds* content; the exclude-pattern branch, which reads no file, writes no vector and marks the row from its own recorded hash, is unaffected by it and by the un-gating alike.

#### Scenario: The embedding pass refuses to certify content it did not read

- **WHEN** the embedding pass reads a file whose content does not hash to the content hash of the row it selected
- **THEN** it SHALL NOT embed that content and SHALL NOT mark the row as embedded
- **AND** a later pass, after the scan has refreshed the row, SHALL embed it

#### Scenario: A permanently unreadable note does not freeze another note's embeddings

- **WHEN** a user's vault holds one note that can never be read — so every re-derive is incomplete and no provenance is ever recorded — and a second, readable note is changed so that its `content_hash` no longer matches its `embedded_content_hash`
- **THEN** the next pass SHALL embed the changed note's new content and update its `note_embeddings` rows
- **AND** it SHALL do so even though no provenance has been recorded for that user and no provenance is recorded by that pass either

#### Scenario: The embedding pass runs under every classification

- **WHEN** a user's classification is provenance unknown, provenance unresolved, or reassigned, and the embedding pass runs
- **THEN** it SHALL process that user's eligible rows rather than skip the user
- **AND** each note it embeds SHALL have hashed to the row it was written against

#### Scenario: The row's hash changes between the verification and the certification

- **WHEN** the embedding pass verifies a file's bytes against the content hash from its initial query, and another transaction commits a different content hash for that row before the pass certifies it
- **THEN** the certification SHALL match no row, the generated vectors SHALL be discarded, and no `note_embeddings` row for that note SHALL be deleted or inserted
- **AND** `embedded_content_hash` SHALL be left unchanged, so a later pass embeds the note as it then stands

#### Scenario: An excluded note that moves out of the exclusion is not marked embedded

- **WHEN** the embedding pass selects a row whose path matches an exclusion pattern, and another transaction commits that row at a non-excluded path with an unchanged content hash before the exclusion branch acts
- **THEN** the branch SHALL delete no `note_embeddings` row and SHALL leave `embedded_content_hash` unchanged
- **AND** a later pass SHALL still select that row, so the note is not silently absent from semantic search

#### Scenario: A note certified as excluded and then moved out is re-embedded

- **WHEN** the exclusion branch marks a row embedded and deletes its vectors, and the note is afterwards moved to a path the exclusion patterns do not match
- **THEN** the move SHALL leave `embedded_content_hash` null
- **AND** the next embedding pass SHALL select that row and give it vectors, so the note is searchable at its new path

#### Scenario: A note certified as included and then moved in is dropped from search

- **WHEN** a note is embedded at an included path and afterwards moved to a path the exclusion patterns match
- **THEN** the move SHALL leave `embedded_content_hash` null
- **AND** the next embedding pass SHALL delete its vectors, so an excluded note does not stay searchable

#### Scenario: Both move paths invalidate the certification

- **WHEN** the move is performed by the write tool, and when it is performed by the index pass's id-preserving move detection
- **THEN** both SHALL clear `embedded_content_hash` in the statement that changes `file_path`
- **AND** the row identity SHALL be preserved by both, so the clearing is what re-opens the decision rather than a replacement row

#### Scenario: An unmoved excluded note is still marked and its vectors dropped

- **WHEN** the same branch runs for a row that has not moved
- **THEN** it SHALL delete that note's stored vectors and mark the row embedded from the hash it selected

#### Scenario: The certified hash is the one the bytes were verified against

- **WHEN** the embedding pass certifies a note whose row has not moved
- **THEN** the value written to `embedded_content_hash` SHALL be the hash the bytes were verified against, and SHALL NOT be a value re-read from the metadata row

#### Scenario: A foreign root cannot be embedded against a surviving row

- **WHEN** the embedding pass runs for a user whose metadata rows were derived from one directory while the assigned root is another, and the file at a row's relative path under the assigned root holds different bytes
- **THEN** the pass SHALL skip that note, SHALL write no `note_embeddings` row for it, and SHALL leave its `embedded_content_hash` unchanged

## ADDED Requirements

### Requirement: Exclusion-pattern changes reconcile on the next completed embed pass

After processing the hash-mismatch backlog, an embed pass SHALL run a reconciliation sweep over rows whose certification is current (`embedded_content_hash IS NOT DISTINCT FROM content_hash`, owner-scoped) against the *current* `EMBEDDING_EXCLUDE_PATTERNS`: a row whose path matches a pattern and still has vectors SHALL have them removed; a row whose path matches no pattern and has none SHALL be re-embedded from its verified bytes. Every reconciliation write SHALL go through the certified predicate (`id + content_hash + file_path`, stamp before delete) with a per-note commit; a row that fails certification SHALL be rolled back and left for a later pass, never patched by id.

Convergence is defined for a **completed** sweep — one that visited every selected row without pause or error. After it, every certification-current row satisfies "vectors exist iff the current configuration includes it", with three defined exceptions: a row whose cleaned content produces zero chunks is correct with zero vectors and SHALL NOT be rewritten; a row whose on-disk bytes no longer hash to its `content_hash` SHALL be skipped (the backlog owns it next pass); a row whose provider call fails SHALL be left unstamped where the stamp would have been new and retried on a later pass. A sweep interrupted by the pause flag SHALL stop between notes, and the next pass SHALL run a fresh sweep from the start — per-note commits make re-visiting already-repaired rows a no-op.

#### Scenario: Adding a pattern removes existing vectors

- **WHEN** a note was embedded, its certification is current, and the operator then adds a pattern matching its path
- **THEN** the next completed embed pass SHALL certify the row (`id + content_hash + file_path`) and delete its vectors
- **AND** the note SHALL stop appearing in semantic search after that pass

#### Scenario: Removing a pattern restores vectors

- **WHEN** a note was stamped by the exclusion branch (certified, zero vectors) and the operator then removes the pattern that excluded it
- **THEN** the next completed embed pass SHALL re-read the note's bytes beneath the pass's pinned root, verify they hash to the row's `content_hash`, and embed it through the certified path
- **AND** the note SHALL appear in semantic search after that pass

#### Scenario: A concurrent move defeats the reconciliation write, not the vault

- **WHEN** the reconciliation decides about a row and the row's `file_path` changes before the certifying UPDATE commits
- **THEN** the certification SHALL match no row, the note's reconciliation SHALL be rolled back, and no vector SHALL be deleted or written on the strength of the stale decision

#### Scenario: A genuinely empty note is not rewritten every pass

- **WHEN** an included note's cleaned content produces zero chunks and its certification is current
- **THEN** the reconciliation SHALL write nothing for it

#### Scenario: Bytes that no longer match the row are left to the backlog

- **WHEN** the reconciliation reads a row's bytes and they do not hash to the row's `content_hash`
- **THEN** the reconciliation SHALL write nothing for that row
- **AND** the ordinary backlog SHALL select the row on a later pass once the scan has upserted its new hash

#### Scenario: A pause stops the sweep between notes and the next pass converges

- **WHEN** the pause flag is set while a reconciliation sweep is mid-way
- **THEN** the sweep SHALL stop before the next note, already-repaired rows SHALL stay repaired (per-note commits), and the next unpaused pass SHALL run a fresh sweep that completes the remainder

### Requirement: A move recomputes the title through the shared derivation

Both move paths SHALL leave `notes_metadata.title` exactly what a fresh index of the file at its new path would produce, computed by the one shared title-derivation helper (sanitize the frontmatter `title`, stringify non-strings, fall back to the filename stem for falsy values, bound to the column width). The indexer's id-preserving move detection SHALL take the title from the entry it parsed for the new path. `move_note` SHALL read the moved note through the destination's validated descriptor after the move stands, parse its frontmatter with the same parser the indexer uses, and derive the title from that; only when that read or parse fails MAY it fall back to deriving from the row's stored sanitized frontmatter, which is best-effort and self-heals at the next content change.

#### Scenario: Renaming a note with no frontmatter title updates the title

- **WHEN** `Alpha.md` (no frontmatter `title`) is renamed to `Beta.md`, by either an external move the indexer detects or by `move_note`
- **THEN** index-backed tools SHALL report the note's title as `Beta` after the move is indexed

#### Scenario: An explicit frontmatter title survives a move

- **WHEN** a note whose frontmatter sets `title: Roadmap` is moved or renamed
- **THEN** its title SHALL remain `Roadmap`

#### Scenario: Falsy frontmatter titles agree with a fresh index

- **WHEN** a note whose frontmatter `title` is `false`, `0`, `[]`, `{}`, or the empty string is moved from `Alpha.md` to `Beta.md`
- **THEN** the stored title SHALL be `Beta` — the same value a fresh index of the file would derive — on both move paths

#### Scenario: Stale indexed frontmatter does not decide the title

- **WHEN** a note's frontmatter `title` was added or removed on disk after the last index pass, and `move_note` then moves the note
- **THEN** the stored title SHALL be derived from the file's current frontmatter, not from the stale indexed copy

### Requirement: Keyword indexing attempts full content and degrades per-note without aborting the pass

The tsvector build (incremental pass and full rebuild alike) SHALL attempt the note's full content. Each attempt SHALL run inside its own savepoint, entered such that a database error unwinds the savepoint through the context manager's rollback before any retry (the failure handling sits outside the savepoint context, so the outer transaction is never left in the aborted state). On failure the build SHALL retreat by halving the content, one fresh savepoint per attempt, down to a floor of exactly 100,000 characters — the pre-change statement. A failure at the floor SHALL propagate, and the two call sites SHALL provide different — but individually stated — guarantees: the **incremental pass** aborts with nothing committed, so no state is stranded and the note is retried next tick (the pre-change behavior); the **full rebuild** SHALL be atomic — no intermediate commits — so a floor failure rolls the entire rebuild back and the error surfaces to the operator who invoked it, never leaving a keyword index half-built under two FTS configurations that no periodic pass would repair.

Verification of the savepoint behavior SHALL include a real-PostgreSQL integration test (mocks cannot prove the driver's aborted-transaction state clears): induce a genuine statement failure, observe the bounded retry succeed within the same outer transaction, perform a further update, commit, and verify both rows.

**The full rebuild SHALL update only rows it certifies.** It snapshots the table once and then reads the vault note by note, so both the rows and the files move underneath it — and a keyword vector is only ever rewritten again when a note's `content_hash` changes, because both move paths preserve `content_tsvector` and the ordinary scan skips a row whose hash is unchanged. A row the rebuild steps over, or writes the wrong bytes into, therefore stays on the previous configuration with nothing that would ever revisit it.

The rebuild's snapshot SHALL therefore retain each row's owner, relative path and content hash; the bytes it reads SHALL be verified to hash to that retained content hash before anything is written; and its UPDATE SHALL be conditional on all four of id, owner, relative path and content hash, and SHALL require that exactly one row matched. A zero-row update, a read failure, or a hash mismatch SHALL NOT be committed around and SHALL NOT be routed through the size-halving retreat, which addresses a size failure and cannot fix a stale target. Each SHALL instead trigger a bounded re-read of the current owner-scoped row: a row that is gone is safely absent and SHALL be skipped; a row whose path or hash has changed SHALL be retried against those fresh values within a bounded number of attempts; and a row that still records the path and hash the rebuild acted on — an unreadable file, or bytes the scan has not caught up with — SHALL abort the whole rebuild, which being one transaction rolls every other note back with it.

#### Scenario: A note moved mid-rebuild is repaired, never stepped over

- **WHEN** a note's `file_path` changes (by either move path) after the rebuild's snapshot and before it reads that row, so the read at the snapshotted path fails
- **THEN** the rebuild SHALL re-read the row, retry against its current path, and write that row's tsvector under the current configuration
- **AND** it SHALL NOT commit the remaining notes while leaving that row on the previous configuration

#### Scenario: A stale write does not land when the content hash advances

- **WHEN** a concurrent index pass commits a new `content_hash` and a matching `content_tsvector` for a row between the rebuild's read of the earlier content and its UPDATE
- **THEN** the certified UPDATE SHALL match no row and the earlier content's tsvector SHALL NOT be written
- **AND** the rebuild SHALL re-read the row and rebuild it against the committed content, so the stored hash and the stored tsvector describe the same content

#### Scenario: A row it cannot certify aborts the whole rebuild

- **WHEN** the rebuild cannot read a note, or the bytes it reads do not hash to the row's `content_hash`, and a re-read shows the row still records that path and that hash
- **THEN** the rebuild SHALL abort and its transaction SHALL roll back, leaving every note's `content_tsvector` unchanged
- **AND** the error SHALL surface to the operator who invoked it, rather than the rebuild committing around that row

#### Scenario: A row deleted mid-rebuild is safely absent

- **WHEN** the row is deleted (or leaves the rebuild's owner scope) between the snapshot and the write
- **THEN** the rebuild SHALL skip it without aborting, because no row remains in scope to leave on the previous configuration

#### Scenario: Terms beyond the former 100K slice are searchable when the full build succeeds

- **WHEN** a valid note carries a distinctive term past 100,000 characters and its full-content tsvector build succeeds
- **THEN** after the next index of that note, `keyword_search` for that term SHALL return the note

#### Scenario: A pathological note degrades alone, and the degradation is bounded and logged

- **WHEN** a note's full-content tsvector exceeds PostgreSQL's size limit
- **THEN** the pass SHALL retreat to a bounded prefix for that note only, log the retreat with the prefix length, index the remaining notes normally, and commit
- **AND** terms present only beyond the successful prefix are accepted as unsearchable for that note — the declared degradation

#### Scenario: A floor failure in the incremental pass behaves exactly as before the change

- **WHEN** even the 100,000-character floor attempt fails for a note during an incremental index pass
- **THEN** the error SHALL propagate and the pass SHALL abort with nothing committed, exactly as the pre-change implementation aborted, so the note's metadata hash does not advance and the pass retries next tick

#### Scenario: A floor failure during a full rebuild rolls the whole rebuild back

- **WHEN** `rebuild_tsvectors` is processing a vault of more than 500 notes and a note's floor attempt fails after what would previously have been an intermediate commit boundary
- **THEN** the entire rebuild SHALL roll back — no note's tsvector changes — and the error SHALL surface to the invoking operator
- **AND** the keyword index SHALL never be left half-built under two FTS configurations

### Requirement: A many-chunk note completes, and certifies only on full coverage

The Ollama (local, sequential) embedding batch SHALL have no aggregate deadline: its per-chunk timeout (30 s per provider call) is the liveness bound, so a note cannot be structurally unable to finish while every chunk is individually healthy. A note SHALL be certified only when every one of its chunks produced a vector; no partial chunk coverage may ever be stamped complete.

#### Scenario: A giant note eventually embeds and stops being retried

- **WHEN** a note produces more chunks than the former fixed 300 s deadline allowed at normal provider latency
- **THEN** the embed pass SHALL process all of its chunks, certify it, and not select it again while its content is unchanged

#### Scenario: A hung provider still fails fast

- **WHEN** the Ollama embedding provider stops responding mid-batch (the OpenAI provider keeps its own pre-existing contract: per-request HTTP timeout with bounded retries)
- **THEN** the in-flight chunk call SHALL time out at the per-chunk timeout and the note SHALL remain uncertified

#### Scenario: Partial coverage is never certified

- **WHEN** the provider returns fewer vectors than chunks for a note
- **THEN** the note SHALL NOT be certified and its previous vectors SHALL remain in place
