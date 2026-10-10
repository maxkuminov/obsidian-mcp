## MODIFIED Requirements

### Requirement: Unresolved provenance is repaired by re-deriving the index, not by asserting a root
When the pass cannot resolve the provenance of a user's index, it SHALL re-derive that index from the assigned root rather than assume the record it lacks. A re-deriving pass SHALL treat every row that is **not derived under the current root** (see "Re-derive progress is recorded per row, bound to the root facts and to the row's own content") as changed — reading, parsing and upserting its file regardless of its hash, and deleting and re-extracting its `note_links` rows — and SHALL apply the ordinary incremental rules (content-hash change detection, and the stat shortcut where the pass is not a full-hash pass) to a row that **is** derived under the current root. It SHALL prune every `notes_metadata` row whose relative path is not present under that root. When the re-derive records provenance, every surviving metadata row and the extraction of every link row SHALL have been written from a file under the assigned root by a re-deriving pass that observed the same root facts — this pass or an earlier one — and every link row's target SHALL have been resolved by the recording pass against the scope's final row set.

`note_embeddings` SHALL NOT be deleted by this branch. An embedding is a function of chunk text and `notes_metadata.content_hash` establishes content equality, so a vector attached to a row whose hash still matches the file under the assigned root is the correct vector for that file; the embedding pass's existing selection on a differing embedded hash then re-embeds exactly the notes whose content differs. The re-derive therefore costs no embedding call for unchanged content, while the discard branch costs a full re-embed.

This branch SHALL be reached by a legacy row that carries no record at all, so introducing the record SHALL NOT require a vault-wide re-embed on upgrade, and SHALL NOT leave any account with a reassignment that goes unreconciled.

The re-derived pass SHALL extract each changed note's links from the body it already buffered during the scan, and SHALL NOT re-read that note from the filesystem for the link rebuild. Re-reading is a second window in which the file can change or disappear between the scan and the rebuild, which silently drops that note's links while the row the scan wrote stands.

A re-derive that repeats because it is incomplete SHALL NOT rewrite rows already derived under the current root: before this rule, one row-backed unreadable file made every tick a full-scope upsert, keyword-vector rewrite and link rebuild (#311), the write amplification #308 measured. The clean-exclusion-sweep record SHALL be forgotten after a re-deriving pass commits only if that pass upserted, moved or deleted at least one row.

Retaining `note_embeddings` across a re-derive rests on a matching content hash proving that the stored vector is the right vector for that file, and that inference holds only if every vector was in fact produced from content hashing to what was recorded alongside it. That verification is required by "The embedding pass is not gated on provenance, because it verifies every hash it certifies" above, which is also why the embedding pass keeps running while this branch is repeating — a re-derive that never completes must not freeze a readable note's embeddings at content it no longer has.

#### Scenario: A legacy index with no record is re-derived, not trusted and not discarded

- **WHEN** the first pass after the record is introduced runs for a user whose index carries no recorded provenance
- **THEN** the pass SHALL re-derive that user's index from the assigned root
- **AND** SHALL NOT delete `note_embeddings` for a note whose content hash still matches the file under that root

#### Scenario: A legacy index built from a different vault is repaired

- **WHEN** a user was indexed from one vault, reassigned to another before any record existed, and the first pass after the upgrade runs — where a note has the same relative path and the same content in both vaults, and the notes it linked to exist only in the previous vault
- **THEN** after that pass the note's link rows SHALL have been re-extracted from the file under the assigned root and resolved against that root alone
- **AND** no row SHALL remain whose relative path is absent under the assigned root
- **AND** the graph tools SHALL report that note's neighbourhood from the assigned root alone

#### Scenario: A note identical in both roots does not keep a broken link

- **WHEN** a reconciliation of either kind runs and a note has the same relative path and the same content hash in the previous and the new root
- **THEN** that note SHALL NOT retain a link row whose resolution was silently dropped by the prune

#### Scenario: The re-derive is recorded only when it completes

- **WHEN** a re-deriving pass fails part way through
- **THEN** no provenance SHALL be recorded for that user, and no row SHALL be marked derived under the current root by that pass
- **AND** the next pass SHALL re-derive again, rather than treating a partially repaired index as established

#### Scenario: The link rebuild reads no file

- **WHEN** a note is scanned successfully and is then deleted from the vault before the pass rebuilds its links
- **THEN** the pass SHALL extract that note's links from the body it buffered during the scan
- **AND** the deletion SHALL NOT cause the note's links to be silently omitted

#### Scenario: A completed re-derive is recorded and not repeated

- **WHEN** a re-deriving pass completes without error and without a withholding skip, and every surviving row is derived under the current root
- **THEN** the provenance of the directory it scanned SHALL be recorded after its last write, as all three facts together
- **AND** the next pass SHALL find the assignment and the real path in agreement, with no observable handle mismatch, and SHALL take the no-op branch

#### Scenario: A repeated re-derive does not rewrite rows already derived under the current root

- **WHEN** a re-deriving pass commits incomplete because one row-backed file could not be read, and the next pass re-derives again with the same root facts while that file is still unreadable and no other file changed
- **THEN** the next pass SHALL NOT upsert, rewrite the keyword vector of, or delete and re-insert the link rows of any row derived under the current root by the earlier pass
- **AND** SHALL again attempt to read the unreadable file

#### Scenario: Progress survives a restart

- **WHEN** a re-deriving pass commits incomplete, the process restarts, and the first pass after the restart re-derives with the same root facts
- **THEN** that pass SHALL read and hash every discovered file (it is a full-hash pass)
- **AND** SHALL NOT upsert any row derived under the current root whose file's hash is unchanged

#### Scenario: A carried-forward note's link is re-resolved when its target is pruned

- **WHEN** an earlier incomplete re-derive resolved a bare-name link of note N to a row beneath a directory the walk could not list, and a later re-deriving pass can list that directory, prunes that row (its path is absent under the assigned root) and records provenance, while another note with the same stem exists under the assigned root
- **THEN** after the recording pass, N's link row SHALL resolve to the note `resolve_target` selects against the final row set, exactly as a single complete re-derive would have resolved it

#### Scenario: The exclusion sweep is not forgotten by a re-derive that changed nothing

- **WHEN** a re-deriving pass commits without upserting, moving or deleting any row
- **THEN** the scope's clean-exclusion-sweep record SHALL be kept

### Requirement: A re-derive that skipped any file is incomplete, and an incomplete re-derive is not recorded
A per-file skip during a re-deriving pass SHALL make that re-derive **incomplete** when the skip could leave a row **not derived under the current root**, and an incomplete re-derive SHALL NOT record provenance for that user. Such a skip is: a discovered file the pass could not open, stat, read or parse **whose path has a row** in the pass's locked rows that is not derived under the current root; a directory it could not open or list beneath the root with such a row at or beneath it; a row deferred because it changed after the pass's snapshot (C5) that is not derived under the current root; or a changed note already selected for upsert whose keyword vector or links it could not write. A skipped path with **no** row, or whose row is derived under the current root, cannot certify a foreign row and SHALL NOT withhold the record. A path that is **present but not indexable** — a quarantined note, a file whose content is not valid UTF-8, or a path that cannot be encoded or exceeds the stored path length (see "Paths and contents the index can never hold are present but not indexable") — SHALL NOT withhold the record either, because any row at that path is deleted in the pass's transaction. A note whose link extraction was **truncated at the declared cap** (`MAX_LINKS_PER_NOTE`) is NOT a skip: the cap is a bounded, deterministic, logged degradation, the rows the pass wrote are exactly the rows it derived, and the note is marked `links_truncated` so the truncation is durably visible. Independently of the skips, the pass SHALL re-read the scope's rows in its own transaction after its last write and SHALL NOT record provenance if any surviving row is not derived under the current root; that check, not the skip list, is authoritative. An incomplete pass SHALL still perform every repair it can, SHALL log the paths that kept it unrecorded and the number of rows not yet derived under the current root, and the next pass SHALL re-derive again.

Without this rule the pass's structural claim is false. The scan continues past a file it cannot read, and ordinary pruning keeps a row whose relative path exists under the assigned root — which is exactly the row a re-derive exists to replace. A vault that supplies a note at the same relative path as the previous vault, but which cannot be read, therefore leaves the previous vault's metadata row and its link rows untouched while the pass completes and records the new directory over them. One such skip is enough to certify a foreign row.

The rule fails toward re-work rather than toward wrongness for an unreadable file, because the alternative — deleting the row behind every unreadable path — destroys a row that may be the correct row for a file that was merely unreadable at that moment. A file whose bytes were read in full but are not valid UTF-8 is not in that position: its bytes are provably not those the row was derived from, so its row is deleted rather than kept.

An incomplete re-derive re-runs every tick until it completes. Each re-run SHALL rewrite only rows not derived under the current root (see "Unresolved provenance is repaired by re-deriving the index"), and a scope whose re-derive stays incomplete for `INDEXER_DEGRADED_AFTER_FAILURES` consecutive passes SHALL be counted as degraded (see the failure-accounting requirement), in addition to the pass naming the offending paths in its log.

#### Scenario: An unreadable file with a row behind it withholds the record

- **WHEN** a re-deriving pass discovers a file it cannot read, the locked rows have a row at that path that is not derived under the current root, and the pass completes the rest of its work
- **THEN** the pass SHALL record no provenance for that user
- **AND** the next pass SHALL re-derive again

#### Scenario: An unreadable file whose row is already derived under the current root does not withhold the record

- **WHEN** a re-deriving pass cannot read a file whose row was derived under the current root by an earlier pass, and no other row is unresolved
- **THEN** the pass SHALL record the provenance of the directory it scanned
- **AND** SHALL keep that row as it is and log the read failure

#### Scenario: An unreadable file with no row does not withhold the record

- **WHEN** a re-deriving pass discovers a file it cannot read, no row exists at that path, and nothing else is skipped
- **THEN** the pass SHALL record the provenance of the directory it scanned
- **AND** the next tick SHALL NOT re-derive or re-upsert the scope's unchanged notes

#### Scenario: The file becomes readable

- **WHEN** a scope's re-derive was incomplete because one row-backed file could not be read, and that file is readable at the next pass
- **THEN** that pass SHALL read, upsert and re-link that file, mark its row derived under the current root, and record provenance if nothing else withholds it
- **AND** `rederive_incomplete` for the scope SHALL reset

#### Scenario: The unreadable file is deleted

- **WHEN** a scope's re-derive was incomplete because one row-backed file could not be read, and that file is deleted before the next pass
- **THEN** that pass SHALL prune its row and record provenance if nothing else withholds it

#### Scenario: A foreign row behind an undecodable path is deleted, not certified

- **WHEN** a user was indexed from one vault, is assigned another, and the newly assigned vault holds a file at the same relative path whose bytes are not valid UTF-8
- **THEN** the pass SHALL delete the previous vault's row at that path, with its embeddings and outgoing links, in its transaction
- **AND** if nothing else withholds the record it SHALL record the newly assigned root's provenance, and no later pass SHALL take the keep branch over a row from the previous vault at that path

#### Scenario: A file that disappears during the scan withholds the record only if it has an unresolved row

- **WHEN** a file is discovered by a re-deriving pass and can no longer be read when the pass reaches it
- **THEN** the pass SHALL treat that path as a skip, and SHALL record no provenance for that user if the locked rows have a row at that path that is not derived under the current root

#### Scenario: A directory that cannot be listed withholds the record when it covers an unresolved row

- **WHEN** a re-deriving pass cannot open or list a directory beneath the root and a locked row at or beneath it is not derived under the current root
- **THEN** the pass SHALL record no provenance for that user

#### Scenario: A directory that cannot be listed over rows all derived under the current root does not withhold the record

- **WHEN** a re-deriving pass cannot list a directory beneath the root, every locked row at or beneath it is derived under the current root, and nothing else is unresolved
- **THEN** the pass SHALL record the provenance of the directory it scanned
- **AND** the scope's `walk_incomplete` counter SHALL still increment

#### Scenario: Every link-extraction skip is recorded, including the unreachable one

- **WHEN** a re-deriving pass reaches a changed note it cannot extract links for — because it holds no buffered body for that path, or because that path has no index row to attach the links to
- **THEN** both cases SHALL be recorded as skips, so the record is withheld
- **AND** neither SHALL be dropped silently, whatever its likelihood, because the record is a claim that every surviving link row was written by a re-deriving pass under the current root

#### Scenario: A row left unmarked without a named skip still withholds the record

- **WHEN** a re-deriving pass records no withholding skip, but its in-transaction re-read finds a surviving row that is not derived under the current root
- **THEN** the pass SHALL record no provenance, SHALL report the re-derive as incomplete, and SHALL log the paths of such rows, bounded to a stated number with a count of the remainder

#### Scenario: A capped note does not withhold the record

- **WHEN** a re-deriving pass reaches a changed note with more than `MAX_LINKS_PER_NOTE` links and processes every other discovered file without a skip
- **THEN** the first `MAX_LINKS_PER_NOTE` links SHALL be written, `links_truncated` SHALL be set on the note, an ERROR line SHALL be logged, and the pass SHALL record the provenance of the directory it scanned

#### Scenario: A quarantined note does not withhold the record, and leaves no foreign row

- **WHEN** a user was indexed from one vault, is assigned another, and a re-deriving pass quarantines the newly assigned vault's note at a relative path the previous vault also had
- **THEN** the row at that path from the previous vault SHALL be deleted in the pass's transaction, together with its embeddings and outgoing links
- **AND** if the pass has no other withholding skip it SHALL record the provenance of the directory it scanned, and the next tick SHALL NOT re-derive or re-upsert the scope's unchanged notes

#### Scenario: The skipped paths are named

- **WHEN** a re-deriving pass is incomplete
- **THEN** it SHALL log the paths responsible, bounded to a stated number with a count of the remainder, and the number of rows not yet derived under the current root

#### Scenario: A complete re-derive is recorded

- **WHEN** a re-deriving pass leaves no withholding skip, raises nothing, and every surviving row is derived under the current root
- **THEN** it SHALL record the provenance of the directory it scanned, after its last write

### Requirement: An unchanged file SHALL be skipped only when its recorded stat matches, and the stat SHALL describe the bytes that were hashed
`notes_metadata` SHALL record, per row, the `(size, mtime_ns, ctime_ns, inode)` of the file whose bytes produced `content_hash`. The tuple SHALL be taken from the file descriptor that was read, before the read. The four fields SHALL be all NULL or all non-NULL.

Racy recency SHALL be measured against the wall-clock instant taken immediately before that pre-read `fstat` (`t_start`), not against the end of the read or hash. A stat whose modification or change time is at or after `t_start − 2 s`, including any time later than `t_start`, SHALL be recorded as NULL.

An index pass SHALL skip reading and hashing a discovered file only if all of the following hold:
- `INDEX_STAT_SHORTCUT` is enabled;
- the pass is not a full-hash pass;
- the pass is not a re-derive, or the row is derived under the current root;
- the row's extraction marker is current;
- the row's recorded stat is non-NULL;
- the file's current stat, following a leaf symlink as the read does, equals the recorded stat in all four fields.

In every other case the file SHALL be read and hashed exactly as before.

When a file is re-read and its hash is unchanged but its stat differs from the row's, or the row's stat is NULL, the pass SHALL update the row's stat with a conditional write that names the row's id, path and content hash. `move_note` SHALL set the moved row's stat to NULL.

#### Scenario: An unchanged file is not read
- **WHEN** a file's current stat equals its row's recorded stat and no bypass condition applies
- **THEN** the pass SHALL not open the file and SHALL treat it as unchanged

#### Scenario: A touched file is re-read and its stat refreshed
- **WHEN** a file's modification time changes but its bytes do not
- **THEN** the pass SHALL read and hash it, find the hash unchanged, write nothing but the new stat, and skip it on the following pass

#### Scenario: Every bypass reads
- **WHEN** the row's stat is NULL, the extraction marker is stale, the pass is a full-hash pass, the pass is a re-derive and the row is not derived under the current root, or the shortcut is disabled
- **THEN** the file SHALL be read and hashed

#### Scenario: A re-derive uses the shortcut for a row already derived under the current root
- **WHEN** a re-deriving pass that is not a full-hash pass discovers a file whose row is derived under the current root, has a current extraction marker, and whose recorded stat equals the file's current stat
- **THEN** the pass SHALL not open the file and SHALL treat it as unchanged

#### Scenario: A racy stat is not trusted
- **WHEN** a file's modification or change time is within 2 seconds before the instant the pass took before its pre-read `fstat`, or later than that instant
- **THEN** its row's stat SHALL be recorded as NULL, so the next pass reads it

#### Scenario: A slow read does not launder a fresh timestamp
- **WHEN** a file's timestamp is fresh at read start, a writer rewrites already-read bytes in the same timestamp tick without changing the size, and the read then takes longer than 2 seconds to complete
- **THEN** the row's stat SHALL still be recorded as NULL, and the next pass SHALL read the file and index the rewritten bytes

#### Scenario: A retargeted symlink is re-read
- **WHEN** a discovered `.md` symlink is repointed at a different file
- **THEN** the stat comparison SHALL see the new target's inode and the pass SHALL read it

## ADDED Requirements

### Requirement: Re-derive progress is recorded per row, bound to the root facts and to the row's own content
`notes_metadata` SHALL carry a nullable `derived_under` marker (migration 030), and a row SHALL be **derived under the current root** exactly when its marker equals the digest of the pinned root's observed provenance facts (assignment, real path and handle, compared exactly, an absent handle included) together with the row's current `file_path`, `content_hash` and `extraction_version`, as defined in the design (`derived-under-v1`).

The marker SHALL be set to a non-NULL value only by a re-deriving pass, in the same transaction as the derived state it describes, and only for a row that pass fully derived from bytes it read under the pinned root: upserted or move-repaired, with its keyword vector written and its links extracted without a skip. Every upsert and id-preserving move written by the index pass SHALL set the marker NULL unless that same transaction then marks the row; a keep-mode or single-user pass SHALL NOT write a non-NULL marker. A row that the pass failed to fully derive SHALL be left NULL. The marker SHALL be compared only inside a re-deriving pass, and a mismatch, NULL included, SHALL mean "not derived under the current root".

Writers outside the index pass (`move_note`, the link backfill, the keyword-vector rebuild, the embedding pass, the panel's resets) SHALL NOT write a non-NULL marker. They are not required to clear it: a change to a row's path, content hash or extraction version invalidates its marker by the binding, and these writers' other outputs are functions of hash-verified bytes and configuration, except link resolution, which the recording pass recomputes.

Migration 030 SHALL add the column as `varchar(64) NULL` with no default, no index and a comment marker shared with the ORM; it SHALL backfill nothing, so every existing row reads "not derived under the current root"; it SHALL reconcile or refuse a pre-existing same-named column on a re-run, and its downgrade SHALL drop only a marked column. `alembic check` SHALL report no new upgrade operations after it.

#### Scenario: A row from the previous root is never current

- **WHEN** a user is re-derived under root facts F, and a row was last fully derived by a pass that observed different facts, or carries NULL
- **THEN** that row SHALL NOT be derived under the current root, and the pass SHALL read, parse and upsert its file if the file can be read

#### Scenario: A handle-mismatch re-derive does not trust the replaced directory's rows

- **WHEN** a scope's rows were marked by a re-derive whose pinned root presented handle H1, and a later re-derive under the same assignment and real path observes handle H2
- **THEN** none of those rows SHALL be derived under the current root

#### Scenario: A→B→A keeps only progress that is genuinely A's

- **WHEN** a user re-derives under root A and commits incomplete, is reassigned to root B (re-derive) where some rows are rewritten and some are not, and is reassigned back to A (re-derive)
- **THEN** under A, every row rewritten under B SHALL NOT be derived under the current root
- **AND** a row marked under A whose path, content hash and extraction version are unchanged SHALL be derived under the current root and SHALL receive only ordinary change detection

#### Scenario: A row rewritten by a keep pass loses its marker

- **WHEN** a row was marked under root facts F by an incomplete re-derive, a later keep-mode pass upserts that row, and a still later re-derive observes F again
- **THEN** that row SHALL NOT be derived under the current root

#### Scenario: move_note invalidates the marker without writing it

- **WHEN** `move_note` moves a note whose row is derived under the current root, and the next pass re-derives with the same facts
- **THEN** the moved row SHALL NOT be derived under the current root, and that pass SHALL read the file at its new path and mark the row

#### Scenario: An external rename is re-marked from the bytes read

- **WHEN** a note whose row is derived under the current root is renamed outside the server and the next re-deriving pass pairs it as a move
- **THEN** the id-preserving move SHALL be marked derived under the current root for its new path in that pass's transaction

#### Scenario: A partial derivation is not marked

- **WHEN** a re-deriving pass upserts a note but cannot extract its links or write its keyword vector
- **THEN** the note's marker SHALL be NULL after the pass commits

#### Scenario: A rolled-back attempt marks nothing

- **WHEN** a re-deriving attempt raises `PoisonNote` and is re-run without the quarantined note
- **THEN** no marker written by the rolled-back attempt SHALL survive, and the re-run SHALL mark the rows it fully derives

#### Scenario: The migration asserts nothing

- **WHEN** migration 030 runs on a database holding rows of users whose provenance is recorded and of users in re-derive
- **THEN** every row's `derived_under` SHALL be NULL afterwards, and `alembic check` SHALL report no new upgrade operations
