## ADDED Requirements

### Requirement: The index records the identity of the directory it was scanned from
The system SHALL record, per user, the identity of the directory that user's `notes_metadata` rows were actually scanned from, in a record that is independent of the user's current vault assignment and therefore survives an unassignment. That record SHALL be written only by the index pass that establishes the state it describes, and MUST NOT be written by any operator-facing handler that changes the assignment.

The record SHALL comprise two facts observed at the same moment, both derived from the directory descriptor the pass pins: the **canonical real path** of the directory scanned, with symbolic links resolved and separators, `.` and `..` normalised; and an **opaque kernel file handle** for that directory, obtained from the pinned descriptor and stored as text that is compared by byte equality and never parsed. A normalised pathname alone is not directory identity, in either direction: a symbolic link retargeted from one directory to another under an unchanged assignment yields the same pathname for a different directory, and two aliases naming one directory yield different pathnames for the same one.

The second fact SHALL be a durable identity that distinguishes a directory from a *replacement* of that directory, and a device-and-inode pair SHALL NOT be used for it. Inode numbers are reusable: a directory deleted and re-created at the same path can be handed the same inode number, at which point a real-path comparison and an inode comparison agree unanimously about two different directories and a foreign index would be kept on what the system calls proof. The kernel file handle carries the inode's generation counter for exactly this purpose, so it distinguishes the replacement from the original.

Device and inode numbers SHALL NOT be recorded or compared across passes at all. They SHALL be used only within a single observation, to establish that the canonical real path being recorded still names the descriptor that was pinned; a disagreement there SHALL be treated as indeterminate rather than as any kind of match.

Neither fact is identity on its own. A real-path comparison cannot observe a directory deleted and re-created at the same path, and a file handle is unique only within its filesystem. The system SHALL therefore treat the two as independent signals, SHALL NOT collapse them into one, and SHALL require both to agree before it keeps an index.

Where the filesystem holding the assigned root cannot produce a file handle, the system SHALL treat the identity as unobtainable: it SHALL NOT record a partial identity, SHALL NOT keep an index on the remaining signal alone, and SHALL re-derive on every pass. That condition SHALL be reported to the operator through the log — once per process per root, and named as the reason in every re-derive it causes — rather than degraded silently.

The record's normalisation is a **separate** question from the normalisation used to decide whether a caller's vault *assignment* has changed, and the two SHALL NOT be served by one function. This record answers "is this the directory those rows came from?", which is a fact about a directory and requires reading the filesystem. The assignment check answers "is this still the path the operator saved?", which is a fact about a stored value and deliberately does not read the filesystem.

#### Scenario: A completed pass records the directory it scanned

- **WHEN** an index pass reconciles a user's index against the assigned root and completes
- **THEN** it SHALL record both the canonical real path and the file handle of the directory it scanned

#### Scenario: A directory re-created at the same path with a reused inode is not the same directory

- **WHEN** the directory a user's index was scanned from is removed, a different directory is created at the same path, and the filesystem hands the new directory the same device and inode numbers as the old one
- **THEN** the recorded identity SHALL NOT compare equal to the observed identity
- **AND** the pass SHALL NOT keep that user's index on the strength of the real path and the inode numbers agreeing

#### Scenario: A filesystem with no file handles cannot license a keep

- **WHEN** the assigned root is on a filesystem that does not support file handles
- **THEN** the pass SHALL re-derive that user's index
- **AND** SHALL record no identity, so every subsequent pass SHALL re-derive again
- **AND** the condition SHALL be reported in the log once per process for that root

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
- **THEN** the recorded real path and the recorded file handle SHALL both disagree with the directory now scanned
- **AND** the pass SHALL treat it as a different directory

#### Scenario: The assignment check keeps its own normalisation

- **WHEN** the pre-publish confirmation compares a caller's assignment against the root bound at admission
- **THEN** it SHALL continue to compare canonical pathnames without resolving symbolic links, and SHALL NOT be changed to use the index's directory-identity record

### Requirement: A pass classifies the recorded identity before it scans, keeps an index only on proof, and never resolves an ambiguity by keeping
Before any file under the assigned root is read, the index pass SHALL compare the recorded identity with the identity observed for the assigned root now, and SHALL reach exactly one verdict, from a classification that is total over every combination of inputs.

**Keeping requires proof, and the proof is the file handle.** The system SHALL classify as **same directory** — and therefore do nothing — only when a file handle was obtained for the assigned root, an identity is recorded for that user, the recorded real path equals the observed real path, and the recorded handle equals the observed handle byte for byte. No other combination of inputs SHALL be classified as the same directory. In particular, agreement of the real path together with agreement of device and inode numbers SHALL NOT be sufficient, because that combination is produced by a directory replaced at the same path with a reused inode.

The remaining verdicts are:

- **Indeterminate** — the assigned root cannot be opened as a directory, or its canonical real path no longer names the directory the pass pinned. The pass SHALL do nothing at all: no delete, no identity recorded, with the pass failing as it does today.
- **Provenance unresolved, no proof obtainable** — the root was pinned but the filesystem produced no file handle. The pass SHALL re-derive and SHALL record no identity, so every subsequent pass re-derives.
- **Provenance unresolved, no record** — a handle was obtained and no identity is recorded for that user. The pass SHALL re-derive and record the observed identity at the end, subject to the completeness rule below.
- **Different directory** — a handle was obtained, an identity is recorded, and the recorded real path and the recorded handle **both** disagree with the observed ones. The pass SHALL discard.
- **Provenance unresolved, partial disagreement** — a handle was obtained, an identity is recorded, and exactly one of the two signals disagrees. The pass SHALL re-derive and record the observed identity at the end, subject to the completeness rule below.

The system prefers, in order: never keeping a foreign index without proof, because silently wrong search results are the expensive failure this product names; and never destroying a valid index on ambiguous evidence, because a discard costs a full re-embed. Ambiguity therefore resolves to a branch that asserts nothing and destroys nothing, and only unanimous disagreement destroys.

The indeterminate verdict does nothing because an index cannot be re-derived from a directory that cannot be read, and destroying one because a mount was briefly unavailable buys nothing and costs the full re-embed.

#### Scenario: Both signals agree and a handle was obtained, so the directory is proven unchanged

- **WHEN** a file handle is obtained for the assigned root and both the recorded real path and the recorded handle match it
- **THEN** no reconciliation SHALL be performed and the pass SHALL proceed exactly as before

#### Scenario: A restart or a remount does not disturb the record

- **WHEN** the host reboots, the container is recreated, or the vault filesystem is remounted, and the assigned root is otherwise untouched
- **THEN** the pass SHALL classify the directory as unchanged and SHALL neither delete a row nor re-embed a note

#### Scenario: A vault restored in place is re-derived rather than discarded

- **WHEN** the assigned root's contents are restored from a backup at the same pathname, so the real path is unchanged and the directory's file handle is not
- **THEN** the pass SHALL treat the provenance as unresolved and SHALL re-derive
- **AND** SHALL NOT discard, so no note is re-embedded on account of a restore

#### Scenario: Both signals disagree, so the directory changed

- **WHEN** a file handle is obtained for the assigned root and both the recorded real path and the recorded handle disagree with it
- **THEN** the pass SHALL discard that user's index as specified below

#### Scenario: The path matches but the directory was replaced

- **WHEN** the recorded real path equals the assigned root's real path but the file handle differs, as when a directory is deleted and a new one created at the same path
- **THEN** the pass SHALL treat the provenance as unresolved and SHALL re-derive rather than keep
- **AND** SHALL NOT discard, because a replacement at the same pathname is as likely to be a restore as a reassignment

#### Scenario: The handle matches but the path differs

- **WHEN** the file handle equals the recorded one but the real path differs, as for a bind-mounted alias of the same directory
- **THEN** the pass SHALL treat the provenance as unresolved and SHALL re-derive
- **AND** SHALL NOT discard, so no vault is re-embedded on account of an alias

#### Scenario: An unopenable root changes nothing

- **WHEN** the assigned root does not exist, is not a directory, or cannot be opened
- **THEN** the pass SHALL delete no row and SHALL write no identity record

#### Scenario: A root whose pathname is moving under the pass changes nothing

- **WHEN** the assigned root is pinned but its canonical real path no longer names the directory that was pinned
- **THEN** the pass SHALL treat the verdict as indeterminate, SHALL delete no row and SHALL write no identity record

### Requirement: The pass pins the assigned root and scans beneath that descriptor
The index pass SHALL open the assigned root once, as a directory descriptor, before it observes the root's identity, and SHALL derive both recorded facts from that descriptor rather than from the pathname. Discovery of the files to index, and every read of a vault file the pass performs, SHALL be anchored to that same descriptor, so that the verdict, the scan and the recorded identity all describe one directory.

Observing an identity through a pathname and then scanning that pathname is check-then-act, and the interval between them is exploitable in both directions. An assignment naming a symbolic link can be retargeted after the identity is observed and before the scan, so the pass indexes one directory and records another; retargeting it back before the following pass then leaves that record permanently accepted by the keep branch. A directory descriptor keeps naming the same directory however its pathname is later renamed or relinked, which is why the system already anchors its mutation path this way.

Anchoring SHALL NOT change what the index contains. Directory symbolic links SHALL still not be descended, and a symbolic link at a discovered file SHALL still be read as it is today; the requirement is about which directory is scanned, and it makes no containment claim about the leaves that the system did not already make. A file's size and modification time SHALL be taken from the same open file the pass read, rather than from a second resolution of its pathname.

Every pass in the indexer that reads vault files for a user — the scan that records the identity, the embedding pass, the one-shot link backfill and the keyword-vector rebuild — SHALL read beneath a root pinned and identity-checked the same way. A recorded identity is a claim about the rows, and a later pass that writes rows for that user through an unanchored pathname would falsify it: a user whose notes contain no links leaves the link backfill eligible to run on every startup.

#### Scenario: The identity, the discovery and the reads describe one directory

- **WHEN** an index pass runs
- **THEN** the identity it observes, the files it discovers and the file contents it reads SHALL all come from the single directory descriptor it pinned at the head of the pass

#### Scenario: A symlinked assignment retargeted mid-pass cannot mislabel the scan

- **WHEN** the assigned root is a symbolic link pointing at one directory when the pass pins it, and the link is retargeted to a second directory before the pass discovers or reads any file
- **THEN** the pass SHALL scan the directory it pinned, not the directory the link now names
- **AND** any identity it records SHALL be the identity of the directory it actually scanned

#### Scenario: The retarget-and-restore interleaving cannot certify a foreign index

- **WHEN** the assigned root is a symbolic link that points at directory A while one pass runs, is retargeted to directory B, and is retargeted back to A before the following pass runs
- **THEN** no pass SHALL leave rows derived from B recorded as having been scanned from A
- **AND** a subsequent pass SHALL NOT take the keep branch on the strength of such a record

#### Scenario: Discovery keeps today's symbolic-link behaviour

- **WHEN** the vault contains a symbolic link to a directory and a symbolic link to a markdown file
- **THEN** the anchored discovery SHALL find the same set of relative paths the pathname-based discovery finds, descending neither directory symbolic link
- **AND** a markdown file reached through a symbolic link SHALL be read as it is today

#### Scenario: Every file-reading pass is anchored

- **WHEN** the embedding pass, the one-shot link backfill or the keyword-vector rebuild reads a user's vault files
- **THEN** it SHALL read them beneath a root pinned and identity-checked as the scan pins and checks it

### Requirement: A directory that demonstrably changed discards the previous directory's index
When a file handle was obtained for the assigned root, an identity is recorded for that user, and both the recorded real path and the recorded handle disagree with the observed ones, the index pass SHALL delete that user's `notes_metadata` rows — and, by cascade, their `note_embeddings` and `note_links` rows — before any file under the new root is read, and SHALL then record the new directory's identity. The discard and the record SHALL commit as one transaction, so no pass can leave rows describing one directory beside a record naming another.

Serving the previous directory's rows is the failure this prevents. The tools served purely from the database — `semantic_search`, `keyword_search`, `list_notes`, `get_recent` and the graph tools — would otherwise return paths, titles, tags, frontmatter and chunk excerpts from a vault the caller no longer has, and a subsequent read of one of those paths can silently return a different note that occupies the same relative path in the new root.

The pass's existing prune by relative path does not make this redundant. A note whose relative path **and** content hash are identical in both directories is classified as unchanged and skipped, so its links are never re-extracted; the notes it pointed at are pruned, and because `note_links.target_note_id` is `ON DELETE SET NULL` the link row survives with its target resolution lost. That link never heals, because the note is never re-parsed again.

Because this branch is destructive and costs a full re-embed of the newly assigned vault, it SHALL fire only on unanimous evidence, and never on a missing or partial record, and never where no file handle was obtainable.

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
- **THEN** the next pass SHALL find both signals in agreement, with a file handle obtained, and SHALL simply index, rather than repeating a delete or re-serving the old rows

#### Scenario: Every caller of the index pass inherits the reconciliation

- **WHEN** the index pass is invoked from the startup pass, from the periodic tick, or from an operator-triggered reindex
- **THEN** the reconciliation SHALL run in all three cases, because it lives in the pass rather than in any one caller

### Requirement: Unresolved provenance is repaired by re-deriving the index, not by asserting a root
When the pass cannot resolve the provenance of a user's index, it SHALL re-derive that index from the assigned root rather than assume the record it lacks. The re-derived pass SHALL disable content-hash change detection, so every file discovered under the assigned root is parsed and upserted regardless of its hash; SHALL prune every `notes_metadata` row whose relative path is not present under that root; and SHALL delete and re-extract **every** one of that user's `note_links` rows, resolving each against an index built from those notes alone. After it, every surviving metadata row and every link row SHALL have been written by that pass from a file under the assigned root.

`note_embeddings` SHALL NOT be deleted by this branch. An embedding is a function of chunk text and `notes_metadata.content_hash` establishes content equality, so a vector attached to a row whose hash still matches the file under the assigned root is the correct vector for that file; the embedding pass's existing selection on a differing embedded hash then re-embeds exactly the notes whose content differs. The re-derive therefore costs no embedding call for unchanged content, while the discard branch costs a full re-embed.

This branch SHALL be reached by a legacy row that carries no record at all, so introducing the record SHALL NOT require a vault-wide re-embed on upgrade, and SHALL NOT leave any account with a reassignment that goes unreconciled.

The re-derived pass SHALL extract each changed note's links from the body it already buffered during the scan, and SHALL NOT re-read that note from the filesystem for the link rebuild. Re-reading is a second window in which the file can change or disappear between the scan and the rebuild, which silently drops that note's links while the row the scan wrote stands.

The embedding pass SHALL verify that the content it read hashes to the content hash of the row it selected, and SHALL skip the note when it does not, leaving the note to a later pass. Retaining `note_embeddings` across a re-derive rests on a matching content hash proving that the stored vector is the right vector for that file, and that inference holds only if every vector was in fact produced from content hashing to what was recorded alongside it.

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

#### Scenario: The link rebuild reads no file

- **WHEN** a note is scanned successfully and is then deleted from the vault before the pass rebuilds its links
- **THEN** the pass SHALL extract that note's links from the body it buffered during the scan
- **AND** the deletion SHALL NOT cause the note's links to be silently omitted

#### Scenario: The embedding pass refuses to certify content it did not read

- **WHEN** the embedding pass reads a file whose content does not hash to the content hash of the row it selected
- **THEN** it SHALL NOT embed that content and SHALL NOT mark the row as embedded

#### Scenario: A completed re-derive is recorded and not repeated

- **WHEN** a re-deriving pass completes without error and without skipping any discovered file
- **THEN** the identity of the directory it scanned SHALL be recorded after its last write
- **AND** the next pass SHALL find both signals in agreement, with a file handle obtained, and SHALL take the no-op branch

### Requirement: A re-derive that skipped any file is incomplete, and an incomplete re-derive is not recorded
Any per-file skip during a re-deriving pass SHALL make that re-derive **incomplete**, and an incomplete re-derive SHALL NOT record an identity for that user. A skip is any discovered file the pass did not fully process — a directory it could not open, a file it could not read, stat, decode or parse, or a changed note whose links it could not extract. An incomplete pass SHALL still perform every repair it can, SHALL log the paths that kept it unrecorded, and the next pass SHALL re-derive again.

Without this rule the pass's structural claim is false. The scan continues past a file it cannot decode or read, and ordinary pruning keeps a row whose relative path exists under the assigned root — which is exactly the row a re-derive exists to replace. A vault that supplies a note at the same relative path as the previous vault, but whose bytes cannot be decoded, therefore leaves the previous vault's metadata row and its link rows untouched while the pass completes and records the new directory over them. One skipped file is enough to certify a foreign row.

The rule fails toward re-work rather than toward wrongness, and that is the trade the system SHALL take. The alternative — transactionally deleting the stale rows for each skipped path, as a fresh index would — is a second deletion path for index contents, and it destroys a row that may be the correct row for a file that was merely unreadable at that moment.

The system SHALL accept, and document, that a file which is permanently unreadable keeps that user in re-derive mode indefinitely. That is preferable to the alternative, which is recording a claim the pass could not establish: the identity would then license the keep branch over rows the pass never visited. The cost is bounded — a re-derive parses and upserts a vault the pass already reads in full and makes no embedding call for unchanged content — and it SHALL be operator-visible: the pass SHALL name the offending paths in its log on every pass, so the file to fix is identified rather than left as an unexplained recurring cost.

#### Scenario: An undecodable file withholds the record

- **WHEN** a re-deriving pass discovers a file it cannot decode and completes the rest of its work
- **THEN** the pass SHALL record no identity for that user
- **AND** the next pass SHALL re-derive again

#### Scenario: A foreign row behind a skipped path is never certified

- **WHEN** a user was indexed from one vault, is assigned another, and the newly assigned vault holds a file at the same relative path whose bytes cannot be decoded
- **THEN** the pass SHALL NOT record the newly assigned directory's identity
- **AND** a later pass SHALL NOT take the keep branch over the row that path still carries

#### Scenario: A file that disappears during the scan withholds the record

- **WHEN** a file is discovered by a re-deriving pass and can no longer be read when the pass reaches it
- **THEN** the pass SHALL treat that path as a skip and SHALL record no identity for that user

#### Scenario: The skipped paths are named

- **WHEN** a re-deriving pass is incomplete
- **THEN** it SHALL log the paths responsible, bounded to a stated number with a count of the remainder

#### Scenario: A complete re-derive is recorded

- **WHEN** a re-deriving pass processes every discovered file without a skip and raises nothing
- **THEN** it SHALL record the identity of the directory it scanned, after its last write

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
