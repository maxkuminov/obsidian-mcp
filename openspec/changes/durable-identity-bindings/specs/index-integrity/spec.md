## ADDED Requirements

### Requirement: The index records the identity of the directory it was scanned from
The system SHALL record, per user, the identity of the directory that user's `notes_metadata` rows were actually scanned from, in a record that is independent of the user's current vault assignment and therefore survives an unassignment. That record SHALL be written only by the index pass that establishes the state it describes, and MUST NOT be written by any operator-facing handler that changes the assignment.

The record SHALL comprise two facts observed at the same moment: the **canonical real path** of the directory scanned, with symbolic links resolved and separators, `.` and `..` normalised; and an **opaque filesystem identity token** for that directory's inode, derived from its device and inode numbers. A normalised pathname alone is not directory identity, in either direction: a symbolic link retargeted from one directory to another under an unchanged assignment yields the same pathname for a different directory, and two aliases naming one directory yield different pathnames for the same one.

Neither fact is identity on its own. A real-path comparison cannot observe a directory deleted and re-created at the same path, and an inode identity token cannot be relied on alone because device numbers are not guaranteed stable across a reboot for every device type and inode numbers may be reused. The system SHALL therefore treat the two as independent signals and SHALL NOT collapse them into one.

The record's normalisation is a **separate** question from the normalisation used to decide whether a caller's vault *assignment* has changed, and the two SHALL NOT be served by one function. This record answers "is this the directory those rows came from?", which is a fact about a directory and requires reading the filesystem. The assignment check answers "is this still the path the operator saved?", which is a fact about a stored value and deliberately does not read the filesystem.

#### Scenario: A completed pass records the directory it scanned

- **WHEN** an index pass reconciles a user's index against the assigned root
- **THEN** it SHALL record both the canonical real path and the filesystem identity token of the directory it scanned

#### Scenario: The assignment handler does not write the record

- **WHEN** an administrator changes, clears or restores a user's vault assignment through the control panel
- **THEN** the recorded identity SHALL be left unchanged by that request

#### Scenario: Single-user mode does not use the record

- **WHEN** an index pass runs with no user identifier
- **THEN** it SHALL neither read nor write the recorded identity, because single-user mode has no user row
- **AND** the pass SHALL behave exactly as it does today

#### Scenario: A cosmetic difference in spelling is not a reassignment

- **WHEN** the assigned root and the recorded root denote the same directory but differ only in a trailing separator, a redundant separator, or a `.` component
- **THEN** the pass SHALL treat the directory as unchanged and SHALL delete nothing

#### Scenario: Two aliases of one directory are not a reassignment

- **WHEN** a user's index was built through one pathname and the assignment later names a different pathname that resolves to the same directory
- **THEN** the pass SHALL NOT discard that user's index, and SHALL NOT re-embed the vault

#### Scenario: A retargeted symlink is a different directory

- **WHEN** the assignment is unchanged but the pathname it names is a symbolic link that has been retargeted to a different directory since the index was built
- **THEN** the recorded real path and the recorded identity token SHALL both disagree with the directory now scanned
- **AND** the pass SHALL treat it as a different directory

#### Scenario: The assignment check keeps its own normalisation

- **WHEN** the pre-publish confirmation compares a caller's assignment against the root bound at admission
- **THEN** it SHALL continue to compare canonical pathnames without resolving symbolic links, and SHALL NOT be changed to use the index's directory-identity record

### Requirement: A pass classifies the recorded identity before it scans, and never resolves an ambiguity by keeping
Before any file under the assigned root is read, the index pass SHALL compare the recorded identity with the identity observed for the assigned root now, and SHALL reach exactly one of four verdicts. **Same directory** — both the real path and the identity token agree — SHALL do nothing. **Different directory** — both disagree — SHALL discard. **Provenance unresolved** — no record at all, or exactly one of the two signals disagreeing — SHALL re-derive. **Indeterminate** — the assigned root is absent, is not a directory, or cannot be stat'ed — SHALL do nothing at all: no delete, no record written, with the pass failing as it does today.

A keep therefore requires both signals to agree. The system prefers, in order: never keeping a foreign index on ambiguous evidence, because silently wrong search results are the expensive failure this product names; and never destroying a valid one on ambiguous evidence, because a discard costs a full re-embed and an unstable device number would otherwise charge it on every restart. Ambiguity resolves to a third branch that asserts nothing and destroys nothing.

The indeterminate verdict does nothing because an index cannot be re-derived from a directory that cannot be read, and destroying one because a mount was briefly unavailable buys nothing and costs the full re-embed.

#### Scenario: Both signals agree that the directory is unchanged

- **WHEN** the recorded real path and identity token both match the assigned root
- **THEN** no reconciliation SHALL be performed and the pass SHALL proceed exactly as before

#### Scenario: Both signals agree that the directory changed

- **WHEN** the recorded real path and identity token both disagree with the assigned root
- **THEN** the pass SHALL discard that user's index as specified below

#### Scenario: The path matches but the directory was replaced

- **WHEN** the recorded real path equals the assigned root's real path but the identity token differs, as when a directory is deleted and a new one created at the same path
- **THEN** the pass SHALL treat the provenance as unresolved and SHALL re-derive rather than keep
- **AND** SHALL NOT discard, because the same observation is produced by a device number that shifted across a reboot

#### Scenario: The identity token matches but the path differs

- **WHEN** the identity token equals the recorded one but the real path differs, as for a bind-mounted alias of the same directory
- **THEN** the pass SHALL treat the provenance as unresolved and SHALL re-derive
- **AND** SHALL NOT discard, so no vault is re-embedded on account of an alias

#### Scenario: An unreadable root changes nothing

- **WHEN** the assigned root does not exist, is not a directory, or cannot be stat'ed
- **THEN** the pass SHALL delete no row and SHALL write no identity record

### Requirement: A directory that demonstrably changed discards the previous directory's index
When both signals agree that the scanned directory has changed, the index pass SHALL delete that user's `notes_metadata` rows — and, by cascade, their `note_embeddings` and `note_links` rows — before any file under the new root is read, and SHALL then record the new directory's identity. The discard and the record SHALL commit as one transaction, so no pass can leave rows describing one directory beside a record naming another.

Serving the previous directory's rows is the failure this prevents. The tools served purely from the database — `semantic_search`, `keyword_search`, `list_notes`, `get_recent` and the graph tools — would otherwise return paths, titles, tags, frontmatter and chunk excerpts from a vault the caller no longer has, and a subsequent read of one of those paths can silently return a different note that occupies the same relative path in the new root.

The pass's existing prune by relative path does not make this redundant. A note whose relative path **and** content hash are identical in both directories is classified as unchanged and skipped, so its links are never re-extracted; the notes it pointed at are pruned, and because `note_links.target_note_id` is `ON DELETE SET NULL` the link row survives with its target resolution lost. That link never heals, because the note is never re-parsed again.

Because this branch is destructive and costs a full re-embed of the newly assigned vault, it SHALL fire only on unanimous evidence, and never on a missing or partial record.

#### Scenario: Reassignment to a different directory

- **WHEN** a user whose index was built from one directory is assigned a different one and the next index pass runs
- **THEN** the rows from the previous directory SHALL be deleted before the new root is scanned
- **AND** the user's `note_embeddings` and `note_links` rows SHALL be removed with them

#### Scenario: Reassignment to the recorded directory keeps the index

- **WHEN** a user's assignment is cleared and later restored to the same directory the index was built from
- **THEN** no row SHALL be deleted and no note SHALL be re-embedded, preserving the behaviour that makes an unassignment reversible without a full re-index

#### Scenario: The discard precedes the first read of the new root

- **WHEN** a discarding pass runs
- **THEN** the delete and the identity record SHALL be committed before any file under the newly assigned root is opened, so a failure while scanning cannot leave the previous directory's rows queryable

#### Scenario: A failed pass after a discard retries cleanly

- **WHEN** the discard commits and the subsequent scan of the new root fails
- **THEN** the next pass SHALL find both signals in agreement and SHALL simply index, rather than repeating a delete or re-serving the old rows

#### Scenario: Every caller of the index pass inherits the reconciliation

- **WHEN** the index pass is invoked from the startup pass, from the periodic tick, or from an operator-triggered reindex
- **THEN** the reconciliation SHALL run in all three cases, because it lives in the pass rather than in any one caller

### Requirement: Unresolved provenance is repaired by re-deriving the index, not by asserting a root
When the pass cannot resolve the provenance of a user's index, it SHALL re-derive that index from the assigned root rather than assume the record it lacks. The re-derived pass SHALL disable content-hash change detection, so every file discovered under the assigned root is parsed and upserted regardless of its hash; SHALL prune every `notes_metadata` row whose relative path is not present under that root; and SHALL delete and re-extract **every** one of that user's `note_links` rows, resolving each against an index built from those notes alone. After it, every surviving metadata row and every link row SHALL have been written by that pass from a file under the assigned root.

`note_embeddings` SHALL NOT be deleted by this branch. An embedding is a function of chunk text and `notes_metadata.content_hash` establishes content equality, so a vector attached to a row whose hash still matches the file under the assigned root is the correct vector for that file; the embedding pass's existing selection on a differing embedded hash then re-embeds exactly the notes whose content differs. The re-derive therefore costs no embedding call for unchanged content, while the discard branch costs a full re-embed.

This branch SHALL be reached by a legacy row that carries no record at all, so introducing the record SHALL NOT require a vault-wide re-embed on upgrade, and SHALL NOT leave any account with a reassignment that goes unreconciled.

#### Scenario: A legacy index with no record is re-derived, not trusted and not discarded

- **WHEN** the first pass after the record is introduced runs for a user whose index carries no recorded identity
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
- **THEN** no identity SHALL be recorded for that user
- **AND** the next pass SHALL re-derive again, rather than treating a partially repaired index as established

#### Scenario: A completed re-derive is recorded and not repeated

- **WHEN** a re-deriving pass completes without error
- **THEN** the identity of the directory it scanned SHALL be recorded after its last write
- **AND** the next pass SHALL find both signals in agreement and SHALL take the no-op branch

### Requirement: The migration introducing the record asserts no provenance, and the deploy order is stated
The migration that introduces the identity record SHALL leave it unset for every existing row and SHALL NOT derive it from the current vault assignment. "Assigned now" does not establish "indexed from what is assigned now" — reassignment lag is the defect the record exists to close — so a backfill from the assignment would stamp rows built from one vault as belonging to another, after which both signals agree, the no-op branch is taken, and the link case that never heals is guaranteed rather than merely possible.

Because the migration writes no provenance, an index pass running under the previous code during or after the migration SHALL have no record to contradict: the previous code cannot write these columns, so every row is unset when the new code starts, and the first pass per user takes the unresolved branch and re-derives. This SHALL be documented as the reason the deploy is safe, and it SHALL NOT be described as serialisation: the index pass lock is process-local, and no advisory lock, row lock or other cross-container coordination exists between a migration container and a running application container.

The system SHALL document that overlap between two indexing containers of this service is prevented by the deploy replacing the container rather than by any code-level guarantee, and that a deploy which runs two such containers concurrently can let a pass under the previous code write rows from the previous root after a new pass has recorded the new one.

#### Scenario: The migration stamps nothing

- **WHEN** the migration introducing the record runs on a database holding both assigned and unassigned users
- **THEN** the recorded identity SHALL be unset for every row, including every assigned user's

#### Scenario: A reassignment made before the upgrade is still reconciled

- **WHEN** a user is reassigned to a different vault and the upgrade runs before the next index pass
- **THEN** the first pass after the upgrade SHALL NOT treat that user's index as built from the newly assigned root
- **AND** SHALL re-derive it from the newly assigned root

#### Scenario: A pass under the previous code cannot forge a record

- **WHEN** an index pass under the previous code commits `notes_metadata` rows after the migration has committed
- **THEN** the recorded identity SHALL remain unset for that user, because the previous code has no code path that writes it
- **AND** the first pass under the new code SHALL re-derive that user's index

### Requirement: A reassignment is honoured at the next index pass, not at the moment of assignment
The reconciliation SHALL be performed by the index pass, and the system SHALL NOT claim that a reassignment takes effect immediately. Between the assignment being saved and the next pass completing its reconciliation, the database-backed tools may still answer from the previous root; that window is bounded by the configured index interval plus the duration of a pass already in flight, and it SHALL be documented as a limitation rather than left to be discovered.

Closing the window would require either a second writer of index contents inside the panel's request transaction — which is how two deletion paths drift apart — or refusing every tool for the whole interval, including the disk-backed tools that are already correct against the new root. This is the same optimistic level the system declares for `edit_note(expected=…)` and the transfer fingerprint check.

The re-derive branch SHALL be documented as not narrowing that window even to "nothing served": it replaces rows as the pass proceeds rather than deleting them up front, which is the price of not asserting a provenance nobody recorded.

#### Scenario: The bound is the index interval

- **WHEN** an administrator reassigns a user to a different root
- **THEN** the previous root's rows SHALL be gone once the first index pass started after that change has completed its reconciliation

#### Scenario: Disk-backed tools are not refused during the window

- **WHEN** a tool that reads the vault from disk is called during that window
- **THEN** it SHALL operate against the newly assigned root, and SHALL NOT be refused on account of the pending reconciliation

#### Scenario: The panel does not delete index rows

- **WHEN** an administrator saves a change to a user's vault assignment
- **THEN** that request SHALL NOT delete any `notes_metadata`, `note_embeddings` or `note_links` row
