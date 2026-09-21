# index-integrity Specification

## Purpose
TBD - created by archiving change harden-cross-layer-integrity. Update Purpose after archive.
## Requirements
### Requirement: Derived index updates are retry-safe
The system SHALL commit note metadata, content hashes, FTS vectors, deletion cleanup, and outgoing link rows as one coherent transaction for an index pass. A failed or cancelled pass MUST NOT persist a metadata state that causes unfinished derived work to be skipped on retry.

#### Scenario: Failure after metadata update
- **WHEN** an index pass fails after preparing a new content hash but before FTS or links are complete
- **THEN** the transaction SHALL roll back
- **AND** the unchanged file SHALL be selected again by the next pass

### Requirement: Link backfill is per-user and restart-safe
Startup link backfill SHALL determine completion independently for each user scope and SHALL NOT treat links belonging to one user as proof that another user is complete. Partial backfill work MUST NOT be recorded as complete.

#### Scenario: Multiple users require migration backfill
- **WHEN** two users have indexed notes and neither user has link rows
- **THEN** startup SHALL backfill both users' notes

#### Scenario: Backfill is interrupted
- **WHEN** a user's link backfill fails before all notes are processed
- **THEN** its partial transaction SHALL roll back
- **AND** startup SHALL retry that user's complete backfill later

### Requirement: Single-user index work is NULL-owned scoped
When an index operation is invoked with no user identifier, the operation SHALL select, mutate, embed, rebuild, and resolve links only for metadata whose `user_id` is NULL. Rows owned by named users MUST remain unchanged, including after a deployment-mode transition leaves mixed scopes in the database.

#### Scenario: Mixed ownership survives single-user indexing
- **WHEN** NULL-owned and user-owned metadata coexist and a single-user index, embed, link, or FTS rebuild pass runs
- **THEN** only NULL-owned rows SHALL be selected or mutated
- **AND** user-owned metadata, vectors, and links SHALL remain unchanged

### Requirement: Embedding completion has exact cardinality
The system SHALL accept an embedding batch only when it contains exactly one vector for every requested chunk. It SHALL record an empty or fully-cleaned note as current with zero vectors.

#### Scenario: Provider returns too few vectors
- **WHEN** the provider returns fewer embeddings than requested chunks
- **THEN** the note SHALL NOT be marked current
- **AND** previously valid embeddings SHALL remain intact

#### Scenario: Note has no embeddable chunks
- **WHEN** cleaning and chunking produces zero chunks
- **THEN** the note's embedded content hash SHALL be marked current
- **AND** the note SHALL have zero embedding rows

### Requirement: The index records the vault assignment its rows were scanned under
The system SHALL record, per user, the **provenance** of that user's `notes_metadata` rows — the vault assignment the index pass ran under, and the directory that assignment named at the moment it ran — in a record that is independent of the user's current assignment and therefore survives an unassignment. That record SHALL be written only by the index pass that establishes the state it describes, and MUST NOT be written by any operator-facing handler that changes the assignment.

The question this record answers is **"did the assignment change?"**, not "is this the same directory?". The event it exists to detect is an operator repointing a user at another vault, which is a change to a value this system itself stores and writes; detecting that is exact, and no input defeats it. Proving directory identity across time is a different and unwinnable question, and the requirement below on filesystem substitution states where the boundary is.

The record SHALL comprise three facts, all observed at the same moment, from the directory descriptor the pass pins:

- the **canonical assignment string** — the user's assigned vault path normalised the way the pre-publish assignment check normalises it, without resolving symbolic links. This is the load-bearing fact. The system SHALL use **one** normaliser for both, called rather than re-implemented, so that the index's notion of "the same assignment" and the write path's notion of it cannot drift apart.
- the **canonical real path** of the directory the pass actually scanned, with symbolic links resolved and separators, `.` and `..` normalised. Its purpose is not to prove identity but to stop the assignment comparison from destroying a valid index over a cosmetic rename: two pathnames naming one directory differ as strings and agree as real paths, so reassignment to an alias re-derives instead of costing a full re-embed. This fact SHALL be recorded and compared as the **hexadecimal encoding of its filesystem bytes**, never as text. A pathname the kernel returns is an arbitrary byte sequence, so a component that is not valid UTF-8 decodes to a surrogate escape that a UTF-8 database cannot store; recording it as text would make the one write that must never roll back — the discard — fail on such a path and leave the former vault's index served indefinitely. Comparison is therefore encode-then-compare on both sides, never decode-then-compare, and the recorded value is decoded only to render it to an operator.
- an **opaque kernel file handle** for that directory, where the filesystem can produce one, stored as text that is compared by byte equality and never parsed.

The file handle SHALL be **best-effort hardening in the refusing direction only**. Where a handle is recorded for a user and a handle can be read for the assigned root now, and the two differ, a verdict that would otherwise keep the index SHALL be downgraded to a re-derive. A handle that **matches SHALL NOT upgrade any verdict** and SHALL NOT be treated as proof of anything. Where no handle is available on either side, the hardening SHALL simply be absent: the pass SHALL decide on the assignment and the real path exactly as it does elsewhere, SHALL NOT enter any degraded mode, SHALL NOT re-derive on every pass for that reason, and SHALL NOT warn. A null handle SHALL mean "no hardening signal", never "provenance unknown".

**Every write of the record SHALL write all three facts together, and a fact the pass could not observe SHALL be written as null.** No branch may update one part of the record and leave another part describing a root it does not describe. Without that rule a later observation can be compared against a root the stamp did not cover.

Device and inode numbers SHALL NOT be recorded or compared across passes at all. They SHALL be used only within a single observation, to establish that the canonical real path being recorded still names the descriptor that was pinned; a disagreement there SHALL be treated as indeterminate rather than as any kind of match.

#### Scenario: A completed pass records the assignment it ran under

- **WHEN** an index pass reconciles a user's index against the assigned root and completes
- **THEN** it SHALL record the canonical assignment string, the canonical real path of the directory it scanned, and the file handle of that directory where one is available

#### Scenario: The record is written as a whole, with unobservable facts null

- **WHEN** a pass records provenance for a user and the filesystem cannot produce a file handle
- **THEN** the recorded handle SHALL be null and the other two facts SHALL be those the pass observed
- **AND** no part of a previous record SHALL survive the write

#### Scenario: A missing file handle changes no verdict

- **WHEN** the assigned root is on a filesystem that cannot produce a file handle
- **THEN** the pass SHALL classify the provenance from the assignment string and the real path alone
- **AND** SHALL NOT re-derive on that account, SHALL NOT log a degraded-mode warning, and SHALL reach the same verdict it would reach if handles were unavailable everywhere

#### Scenario: A matching handle grants nothing

- **WHEN** the recorded handle equals the observed handle but the recorded assignment string or real path does not equal the observed one
- **THEN** the pass SHALL NOT keep the index on the strength of the handle agreeing

#### Scenario: The assignment handler does not write the record

- **WHEN** an administrator changes, clears or restores a user's vault assignment through the control panel
- **THEN** the recorded provenance SHALL be left unchanged by that request

#### Scenario: Single-user mode does not use the record

- **WHEN** an index pass runs with no user identifier
- **THEN** it SHALL neither read nor write the recorded provenance, because single-user mode has no user row
- **AND** the pass SHALL behave exactly as it does today

#### Scenario: A cosmetic difference in spelling is not a reassignment

- **WHEN** the assigned root and the recorded assignment denote the same path but differ only in a trailing separator, a redundant separator, or a `.` component
- **THEN** the shared normaliser SHALL render them equal, and the pass SHALL treat the assignment as unchanged and SHALL delete nothing

#### Scenario: Two aliases of one directory are not a reassignment

- **WHEN** a user's index was built under one assignment and the assignment later names a different pathname that resolves to the same directory
- **THEN** the recorded real path SHALL equal the observed one while the assignment strings differ
- **AND** the pass SHALL re-derive rather than discard, so the vault SHALL NOT be re-embedded

#### Scenario: One normaliser, shared with the write path

- **WHEN** the index record's assignment fact is produced and when the pre-publish confirmation compares a caller's assignment against the root bound at admission
- **THEN** both SHALL use the same normalisation function, which compares canonical pathnames without resolving symbolic links
- **AND** the index's real path SHALL be a separate recorded fact rather than a second normalisation of the assignment, and SHALL NOT enter the pre-publish comparison

### Requirement: A pass classifies the recorded provenance before it scans, and never resolves an ambiguity by keeping
Before any file under the assigned root is read, the index pass SHALL compare the recorded provenance with the same facts observed for the assigned root now, and SHALL reach exactly one verdict, from a classification that is total over every combination of inputs.

A recorded provenance SHALL count as **present** only when both the recorded assignment string and the recorded real path are non-null. Any other combination — both null, or one null and the other set — SHALL be treated as **no record at all**, never as a partial match and never as a keep. Both facts are always observable for a root the pass could pin, so a half-set record is drift rather than a state this system writes, and the safe reading of drift is that nothing is known.

The system SHALL classify as **same assignment** — and therefore do nothing — only when provenance is present for that user, the recorded assignment string equals the observed one, the recorded real path equals the observed one, and no handle mismatch is observable. A handle mismatch is observable only when a handle is recorded **and** a handle can be read for the assigned root now; where either is absent there is no mismatch to observe and the verdict stands on the other two facts.

The remaining verdicts are:

- **Indeterminate** — the assigned root cannot be opened as a directory, or its canonical real path no longer names the directory the pass pinned. The pass SHALL do nothing at all: no delete, no record written, with the pass failing as it does today.
- **Provenance unknown** — no provenance is present for that user, including a half-set record. The pass SHALL re-derive and record the observed facts at the end, subject to the completeness rule below.
- **Provenance unresolved, contradicted by the handle** — the assignment string and the real path both agree, and a handle is recorded, and a handle was read now, and the two differ. The pass SHALL re-derive and record at the end, subject to the completeness rule.
- **Reassigned** — provenance is recorded and the recorded assignment string and the recorded real path **both** disagree with the observed ones. The pass SHALL discard.
- **Provenance unresolved, partial disagreement** — provenance is recorded and exactly one of the assignment string and the real path disagrees. The pass SHALL re-derive and record at the end, subject to the completeness rule.

The system prefers, in order: never keeping an index across a reassignment, because silently wrong search results are the expensive failure this product names; and never destroying a valid index on ambiguous evidence, because a discard costs a full re-embed. Ambiguity therefore resolves to a branch that asserts nothing and destroys nothing, and only unanimous disagreement destroys.

The indeterminate verdict does nothing because an index cannot be re-derived from a directory that cannot be read, and destroying one because a mount was briefly unavailable buys nothing and costs the full re-embed.

#### Scenario: The assignment and the real path both agree, so nothing is done

- **WHEN** the recorded assignment string and the recorded real path both equal the observed ones and no handle mismatch is observable
- **THEN** no reconciliation SHALL be performed and the pass SHALL proceed exactly as before

#### Scenario: A restart, a recreate or a remount does not disturb the record

- **WHEN** the host reboots, the container is recreated, or the vault filesystem is remounted, and the assignment and the directory are otherwise untouched
- **THEN** the pass SHALL classify the assignment as unchanged and SHALL neither delete a row nor re-embed a note

#### Scenario: Reassignment to a different vault discards

- **WHEN** provenance is recorded and both the recorded assignment string and the recorded real path disagree with the observed ones
- **THEN** the pass SHALL discard that user's index as specified below

#### Scenario: A handle that contradicts an otherwise-matching pair downgrades a keep

- **WHEN** the assignment string and the real path both agree, a handle is recorded, a handle is read for the assigned root now, and the two handles differ — as when a directory is deleted and a new one created at the same path
- **THEN** the pass SHALL re-derive rather than keep
- **AND** SHALL NOT discard, because a replacement at the same pathname under an unchanged assignment is as likely to be a restore as anything else

#### Scenario: The real path differs under an unchanged assignment

- **WHEN** the recorded assignment string equals the observed one and the recorded real path does not, as when a symbolic link the assignment names has been retargeted
- **THEN** the pass SHALL re-derive
- **AND** SHALL NOT discard and SHALL NOT keep

#### Scenario: A half-set record is no record

- **WHEN** a user's recorded assignment string is set and the recorded real path is null, or the reverse
- **THEN** the pass SHALL treat the provenance as unknown and SHALL re-derive
- **AND** SHALL NOT keep and SHALL NOT discard on the strength of the fact that is set

#### Scenario: An unopenable root changes nothing

- **WHEN** the assigned root does not exist, is not a directory, or cannot be opened
- **THEN** the pass SHALL delete no row and SHALL write no provenance record

#### Scenario: A root whose pathname is moving under the pass changes nothing

- **WHEN** the assigned root is pinned but its canonical real path no longer names the directory that was pinned
- **THEN** the pass SHALL treat the verdict as indeterminate, SHALL delete no row and SHALL write no provenance record

### Requirement: Filesystem substitution behind an unchanged assignment is out of scope
The system SHALL NOT claim to detect a change of storage underneath an unchanged vault assignment, and SHALL NOT be extended with a heuristic that claims to. Retargeting a symbolic link the assignment names, remounting a different filesystem at the same pathname, restoring a cloned image over the vault, or replacing the directory with a copy are operator actions on storage. Where the file-handle hardening happens to catch one, the outcome is a cheap re-derive; where it does not, the index is kept and reconciled by the ordinary scan. Neither outcome is promised.

This is a declared boundary rather than an oversight, for three reasons. It is unwinnable by construction: a bit-identical clone of a filesystem presents the same inode numbers, generation counters and therefore the same file handles, at the same pathname, under the same assignment, and no fact a userspace process can read separates it from the original. It is the same trust class as editing the database directly: an actor who can remount the vault can also write the provenance record itself, and the system holds no in-process defence against that anywhere else either. And the system as it stands today, which records nothing at all, is equally blind to every one of these, so the record neither regresses nor closes this.

Most of a substitution heals without any of this machinery, and the system SHALL rely on that rather than on a stronger claim: a kept index is still reconciled by the ordinary scan, which matches every note by relative path and content hash, prunes rows whose path is absent under the root, and re-parses and re-embeds every note whose bytes differ.

One interleaving inside this boundary is worth naming rather than leaving to be found: a re-derive triggered by a real-path disagreement can be **incomplete**, in which case it writes no record, and if the substitution is reverted before the next pass that pass sees both recorded facts agree and keeps — over rows a previous pass partly re-derived from the substitute. That is the same non-goal, reached by a different route, and it is bounded the same way: the ordinary scan reconciles those rows by relative path and content hash, leaving only the case below.

The one case that does not heal SHALL be documented as a **pre-existing defect of the incremental indexer**, not as a gap in this record: a note whose relative path and content hash are both unchanged is classified "no change" and never re-parsed, so its link rows are never re-extracted, and a link whose target was pruned keeps a null resolution permanently. That is reachable today on a single vault with no reassignment anywhere in the sequence, and its fix belongs to link resolution rather than to provenance.

#### Scenario: A cloned filesystem at the same pathname is kept

- **WHEN** the vault filesystem is replaced by a bit-identical clone mounted at the same pathname, under an unchanged assignment, so that every recorded fact including the file handle matches
- **THEN** the pass SHALL keep the index
- **AND** the ordinary scan SHALL reconcile it by relative path and content hash, pruning rows whose paths the clone lacks
- **AND** this SHALL be recorded as a declared non-goal rather than specified as prevented

#### Scenario: A substitution reverted before the next pass is not detected

- **WHEN** the directory an assignment names is substituted, a pass re-derives incompletely and therefore records nothing, and the substitution is reverted before the following pass
- **THEN** the following pass SHALL find both recorded facts in agreement and SHALL keep
- **AND** this SHALL be recorded as the same declared non-goal rather than specified as prevented

#### Scenario: The dangling-link residual is attributed to the indexer, not to the record

- **WHEN** a note's relative path and content hash are unchanged and a note it linked to has been pruned
- **THEN** its link row SHALL keep a null resolution until that note is edited, in exactly the same way as when no reassignment and no substitution has occurred
- **AND** the system SHALL document this as a defect of incremental change detection rather than as a property of the provenance record

#### Scenario: No heuristic is added to close the boundary

- **WHEN** a design is considered that infers a substituted root from content overlap, path overlap, a mount identifier, or any other proxy
- **THEN** it SHALL be rejected, because its failure direction is a silent keep on two vaults that merely resemble each other

### Requirement: The pass pins the assigned root and scans beneath that descriptor
The index pass SHALL open the assigned root once, as a directory descriptor, before it observes the root's facts, and SHALL derive the observed real path and file handle from that descriptor rather than from the pathname. Discovery of the files to index, and every read of a vault file the pass performs, SHALL be anchored to that same descriptor.

What the pin establishes is deliberately narrow, and the system SHALL NOT claim more from it: **within one pass, the facts observed, the files discovered and the bytes read all come from one inode**, so a pass cannot record provenance describing a directory it did not scan. It does not prove that the pinned directory is the one earlier rows came from; nothing proves that, and the requirement above says so.

Observing facts through a pathname and then scanning that pathname is check-then-act, and the interval between them is exploitable in both directions. An assignment naming a symbolic link can be retargeted after the observation and before the scan, so the pass indexes one directory and records another; retargeting it back before the following pass then leaves that record standing over rows the pass never derived from it. A directory descriptor keeps naming the same directory however its pathname is later renamed or relinked, which is why the system already anchors its mutation path this way.

Anchoring SHALL NOT change what the index contains. Directory symbolic links SHALL still not be descended, and a symbolic link at a discovered file SHALL still be read as it is today; the requirement is about which directory is scanned, and it makes no containment claim about the leaves that the system did not already make. A file's size and modification time SHALL be taken from the same open file the pass read, rather than from a second resolution of its pathname.

Every pass in the indexer that reads vault files for a user — the scan, the embedding pass, the one-shot link backfill and the keyword-vector rebuild — SHALL read beneath a root it pinned this way.

#### Scenario: The observation, the discovery and the reads describe one directory

- **WHEN** an index pass runs
- **THEN** the facts it observes, the files it discovers and the file contents it reads SHALL all come from the single directory descriptor it pinned at the head of the pass

#### Scenario: A symlinked assignment retargeted mid-pass cannot mislabel the scan

- **WHEN** the assigned root is a symbolic link pointing at one directory when the pass pins it, and the link is retargeted to a second directory before the pass discovers or reads any file
- **THEN** the pass SHALL scan the directory it pinned, not the directory the link now names
- **AND** any provenance it records SHALL describe the directory it actually scanned

#### Scenario: Discovery keeps today's symbolic-link behaviour

- **WHEN** the vault contains a symbolic link to a directory and a symbolic link to a markdown file
- **THEN** the anchored discovery SHALL find the same set of relative paths the pathname-based discovery finds, descending neither directory symbolic link
- **AND** a markdown file reached through a symbolic link SHALL be read as it is today

#### Scenario: Every file-reading pass is anchored

- **WHEN** the embedding pass, the one-shot link backfill or the keyword-vector rebuild reads a user's vault files
- **THEN** it SHALL read them beneath a root it pinned as the scan pins it

### Requirement: The unverified ancillary passes do nothing for a user whose provenance is not settled
The one-shot link backfill and the keyword-vector rebuild SHALL each run, for a given user, only when that user's provenance is recorded and the classification for the assigned root at that moment is **same assignment**. For any other classification they SHALL skip that user, SHALL write no row for that user, and SHALL log the skip once, leaving the work to a later pass once the scan has settled the provenance.

The skip SHALL be **per user**, not global: a user whose provenance is unsettled SHALL NOT prevent these passes from running for every other user.

The classification SHALL be computed by the same function the scan uses, so that "settled" cannot come to mean two different things in two places.

**The embedding pass is deliberately not among them**, and the reason is stated in "The embedding pass is not gated on provenance, because it verifies every hash it certifies" below: it is the only one of the three that binds what it writes to the content the metadata row records, so it is safe by construction against the root mixing this gate exists to prevent, and gating it is the one gate whose cost is unbounded.

These two passes read vault files and write rows the provenance is a claim about — link rows and keyword vectors — with **no verification of any kind** that the bytes they read are the bytes the row they write against describes. They cannot assume the scan settled the provenance a moment ago: a user whose notes contain no links leaves the link backfill eligible on every startup, and a reassignment can commit between the scan and either of them. Allowing them to write under an unresolved provenance is what lets a link row extracted from one root be committed against a metadata row from another.

Verification is not merely unimplemented for the link backfill: a link row's *resolution* is a function of the whole set of notes under a root rather than of one file's bytes, so no per-file check could license it. The keyword-vector rebuild could in principle be verified the way the embedding pass is, and is still gated, because it records nothing that would let a later pass notice a vector built from foreign bytes — there is no keyword analogue of `embedded_content_hash`.

Skipping costs those two passes nothing even for a user whose provenance never settles, which is why it is the specified outcome rather than a per-file content check. The re-derive branch does both passes' work itself on every pass: it deletes and re-extracts every one of that user's link rows, and it rewrites every note's keyword vector, because it treats every note as changed. A delayed link backfill of a table the re-derive is filling anyway, and a delayed rebuild of vectors the re-derive is rewriting anyway, cost latency and write nothing wrong.

#### Scenario: An unsettled user is skipped by both gated passes

- **WHEN** a user has no recorded provenance, or the classification for their assigned root is anything other than same assignment, and the link backfill or the keyword-vector rebuild runs
- **THEN** that pass SHALL write no `note_links` or keyword-vector row for that user
- **AND** SHALL log the skip once

#### Scenario: The skip does not stop the pass for other users

- **WHEN** one user's provenance is unsettled and another user's is settled, and a gated pass runs
- **THEN** the settled user's work SHALL be performed in that same pass

#### Scenario: A reassignment between the scan and a later pass writes nothing

- **WHEN** the scan settles a user's provenance and the user is then reassigned to a different vault before the link backfill runs
- **THEN** the link backfill SHALL classify that user as reassigned rather than same assignment, and SHALL write no link row for them
- **AND** the next scan SHALL perform the reconciliation for that user

#### Scenario: A settled user proceeds unchanged

- **WHEN** a user's recorded provenance matches the assigned root and a gated pass runs
- **THEN** it SHALL do exactly the work it does today

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

### Requirement: An assignment that demonstrably changed discards the previous vault's index
When provenance is recorded for a user and both the recorded assignment string and the recorded real path disagree with the observed ones, the index pass SHALL delete that user's `notes_metadata` rows — and, by cascade, their `note_embeddings` and `note_links` rows — before any file under the new root is read, and SHALL then record the new provenance. The discard and the record SHALL commit as one transaction, so no pass can leave rows from one vault beside a record naming another.

Serving the previous directory's rows is the failure this prevents. The tools served purely from the database — `semantic_search`, `keyword_search`, `list_notes`, `get_recent` and the graph tools — would otherwise return paths, titles, tags, frontmatter and chunk excerpts from a vault the caller no longer has, and a subsequent read of one of those paths can silently return a different note that occupies the same relative path in the new root.

The pass's existing prune by relative path does not make this redundant. A note whose relative path **and** content hash are identical in both directories is classified as unchanged and skipped, so its links are never re-extracted; the notes it pointed at are pruned, and because `note_links.target_note_id` is `ON DELETE SET NULL` the link row survives with its target resolution lost. That link never heals, because the note is never re-parsed again.

Because this branch is destructive and costs a full re-embed of the newly assigned vault, it SHALL fire only when both recorded facts disagree, and never on a missing record, never on a partial disagreement, and never on the strength of a file handle — which can refuse a keep but can never establish a discard.

**The delete SHALL be bound to the assignment that produced the verdict, not merely to the user.** The classification is computed against a root taken from the process cache, in an earlier transaction than the one that acts on it, so an administrator can reassign — or correct a reassignment back to the root the index really was built from — in between. Filtering the delete by user id alone then destroys a complete, valid index for the assignment the row currently names, records provenance for a root nobody is assigned to, and forces a full re-embed that the next pass discards again. Inside the discard transaction the pass SHALL therefore take the user's row `SELECT … FOR UPDATE`, re-read it, and require that it is present, active, assigned, and assigned to exactly the assignment the classification was computed against. On any disagreement it SHALL delete nothing, record nothing, and abort, leaving the next pass to reclassify against the row as it then stands. The lock SHALL be held for the rest of that transaction, so the delete and the record beside it cannot straddle a change either.

**The provenance record SHALL be written to exactly the row that was locked.** The stamping update SHALL affect exactly one row; zero rows SHALL roll the transaction back, delete included, because a delete standing beside a provenance record that does not exist is precisely the "rows from one vault beside a record naming another" this branch exists to make impossible.

The same binding SHALL govern the re-derive branch's record, which is provenance too: before it is written the pass SHALL take the same lock and make the same re-read, and SHALL withhold the record on disagreement. Withheld rather than fatal, because that branch destroys nothing and its repairs remain correct for the root they were read from; an unrecorded provenance simply makes the next pass re-derive again.

**The two branches SHALL take that lock differently, and the difference is lock ordering rather than tuning.** The discard runs in its own transaction and takes the user's row *before* it touches any child row, which is the parent-then-child direction a permanent user deletion also takes, so the two queue behind one another; it MAY therefore wait for the lock. The re-derive's record is written at the end of the pass's own transaction, which by then holds `notes_metadata` row locks, while a permanent user deletion locks the user row first and then waits on exactly those children — so waiting there closes a cycle that the database resolves by aborting one side, possibly the operator's deletion. The re-derive's record SHALL therefore request the lock **without waiting**, inside a savepoint, and SHALL treat contention as a withheld record: only the savepoint rolls back, the pass's repairs are still committed, and the reason is logged. A savepoint is required rather than optional, because a failed statement aborts its transaction and the pass would otherwise lose every repair it had just made along with its record.

#### Scenario: Reassignment to a different vault

- **WHEN** a user whose index was built under one assignment is assigned a different vault at a different real path and the next index pass runs
- **THEN** the rows from the previous directory SHALL be deleted before the new root is scanned
- **AND** the user's `note_embeddings` and `note_links` rows SHALL be removed with them

#### Scenario: A real path too long for a bounded column still discards

- **WHEN** a user whose index was built under one assignment is reassigned to a short assignment that is a symbolic link to a directory whose canonical real path is longer than the width of `users.vault_path`, and the next index pass runs
- **THEN** the pass SHALL delete that user's rows and record the observed provenance in one committed transaction, storing the encoded real path in full
- **AND** the transaction SHALL NOT fail on the length of any recorded fact, because a failure there would roll the delete back and leave the former vault's index queryable on every subsequent pass

#### Scenario: A real path containing a non-UTF-8 component still discards

- **WHEN** a user whose index was built under one assignment is reassigned to a vault whose canonical real path contains a component that is not valid UTF-8, so that the observed real path carries a surrogate escape, and the next index pass runs
- **THEN** the pass SHALL delete that user's `notes_metadata` rows and record all three provenance facts in one committed transaction
- **AND** the transaction SHALL NOT fail to encode any recorded fact, because a failure there would roll the delete back and leave the former vault's index queryable on every subsequent pass
- **AND** the recorded real path SHALL decode back to the observed one exactly
- **AND** a later pass over that same root SHALL find the recorded and observed real paths equal, rather than re-deriving because the two were spelled differently

#### Scenario: Reassignment to the recorded assignment keeps the index

- **WHEN** a user's assignment is cleared and later restored to the same path the index was built under, naming the same directory
- **THEN** no row SHALL be deleted and no note SHALL be re-embedded, preserving the behaviour that makes an unassignment reversible without a full re-index

#### Scenario: The discard precedes the first read of the new root

- **WHEN** a discarding pass runs
- **THEN** the delete and the provenance record SHALL be committed before any file under the newly assigned root is opened, so a failure while scanning cannot leave the previous vault's rows queryable

#### Scenario: The assignment is corrected back before the discard transaction runs

- **WHEN** a pass classifies a user's index as a discard against a newly assigned root, and an administrator restores the previous assignment before the discard transaction begins
- **THEN** no `notes_metadata` row SHALL be deleted, no provenance SHALL be recorded, and no file under either root SHALL be read by that pass
- **AND** the pass SHALL abort so that the next one reclassifies against the assignment the row now carries

#### Scenario: The locked re-read finds a state the classification did not describe

- **WHEN** the discard transaction's locked re-read finds the user's row absent, inactive, or with a cleared vault assignment
- **THEN** nothing SHALL be deleted and nothing SHALL be recorded

#### Scenario: A provenance stamp that matches no row rolls the delete back

- **WHEN** the discard transaction's stamping update affects a number of rows other than exactly one
- **THEN** the transaction SHALL roll back, so no delete is committed without the record that must accompany it

#### Scenario: The discard locks the user row before any child write

- **WHEN** the discard transaction runs
- **THEN** it SHALL take the user row's lock before it deletes or updates any `notes_metadata` row
- **AND** it MAY wait for that lock, because at that point it holds no child row locks

#### Scenario: A contended user row withholds the record without losing the repairs

- **WHEN** the re-derive's record is due and another transaction already holds the user row
- **THEN** the pass SHALL NOT wait for that lock
- **AND** no provenance SHALL be recorded, the reason SHALL be logged, and every repair the pass made SHALL still be committed
- **AND** the next pass SHALL re-derive again and record then

#### Scenario: A re-derive record is withheld when the assignment moved under it

- **WHEN** a re-derive completes with nothing skipped but the locked re-read finds the assignment no longer equal to the one the pass ran under
- **THEN** no provenance SHALL be recorded
- **AND** the pass's repairs SHALL still commit, and the next pass SHALL re-derive again

#### Scenario: A failed pass after a discard retries cleanly

- **WHEN** the discard commits and the subsequent scan of the new root fails
- **THEN** the next pass SHALL find the assignment and the real path in agreement and SHALL simply index, rather than repeating a delete or re-serving the old rows

#### Scenario: Every caller of the index pass inherits the reconciliation

- **WHEN** the index pass is invoked from the startup pass, from the periodic tick, or from an operator-triggered reindex
- **THEN** the reconciliation SHALL run in all three cases, because it lives in the pass rather than in any one caller

### Requirement: Unresolved provenance is repaired by re-deriving the index, not by asserting a root
When the pass cannot resolve the provenance of a user's index, it SHALL re-derive that index from the assigned root rather than assume the record it lacks. The re-derived pass SHALL disable content-hash change detection, so every file discovered under the assigned root is parsed and upserted regardless of its hash; SHALL prune every `notes_metadata` row whose relative path is not present under that root; and SHALL delete and re-extract **every** one of that user's `note_links` rows, resolving each against an index built from those notes alone. After it, every surviving metadata row and every link row SHALL have been written by that pass from a file under the assigned root.

`note_embeddings` SHALL NOT be deleted by this branch. An embedding is a function of chunk text and `notes_metadata.content_hash` establishes content equality, so a vector attached to a row whose hash still matches the file under the assigned root is the correct vector for that file; the embedding pass's existing selection on a differing embedded hash then re-embeds exactly the notes whose content differs. The re-derive therefore costs no embedding call for unchanged content, while the discard branch costs a full re-embed.

This branch SHALL be reached by a legacy row that carries no record at all, so introducing the record SHALL NOT require a vault-wide re-embed on upgrade, and SHALL NOT leave any account with a reassignment that goes unreconciled.

The re-derived pass SHALL extract each changed note's links from the body it already buffered during the scan, and SHALL NOT re-read that note from the filesystem for the link rebuild. Re-reading is a second window in which the file can change or disappear between the scan and the rebuild, which silently drops that note's links while the row the scan wrote stands.

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
- **THEN** no provenance SHALL be recorded for that user
- **AND** the next pass SHALL re-derive again, rather than treating a partially repaired index as established

#### Scenario: The link rebuild reads no file

- **WHEN** a note is scanned successfully and is then deleted from the vault before the pass rebuilds its links
- **THEN** the pass SHALL extract that note's links from the body it buffered during the scan
- **AND** the deletion SHALL NOT cause the note's links to be silently omitted

#### Scenario: A completed re-derive is recorded and not repeated

- **WHEN** a re-deriving pass completes without error and without skipping any discovered file
- **THEN** the provenance of the directory it scanned SHALL be recorded after its last write, as all three facts together
- **AND** the next pass SHALL find the assignment and the real path in agreement, with no observable handle mismatch, and SHALL take the no-op branch

### Requirement: A re-derive that skipped any file is incomplete, and an incomplete re-derive is not recorded
Any per-file skip during a re-deriving pass SHALL make that re-derive **incomplete**, and an incomplete re-derive SHALL NOT record provenance for that user. A skip is any discovered file the pass did not fully process — a directory it could not open, a file it could not read, stat, decode or parse, or a changed note whose links it could not extract. A note whose link extraction was **truncated at the declared cap** (`MAX_LINKS_PER_NOTE`) is NOT a skip: the cap is a bounded, deterministic, logged degradation, the rows the pass wrote are exactly the rows it derived, and the note is marked `links_truncated` so the truncation is durably visible. An incomplete pass SHALL still perform every repair it can, SHALL log the paths that kept it unrecorded, and the next pass SHALL re-derive again.

Without this rule the pass's structural claim is false. The scan continues past a file it cannot decode or read, and ordinary pruning keeps a row whose relative path exists under the assigned root — which is exactly the row a re-derive exists to replace. A vault that supplies a note at the same relative path as the previous vault, but whose bytes cannot be decoded, therefore leaves the previous vault's metadata row and its link rows untouched while the pass completes and records the new directory over them. One skipped file is enough to certify a foreign row.

The rule fails toward re-work rather than toward wrongness, and that is the trade the system SHALL take. The alternative — transactionally deleting the stale rows for each skipped path, as a fresh index would — is a second deletion path for index contents, and it destroys a row that may be the correct row for a file that was merely unreadable at that moment.

The system SHALL accept, and document, that a file which is permanently unreadable keeps that user in re-derive mode indefinitely. That is preferable to the alternative, which is recording a claim the pass could not establish: the record would then license the keep branch over rows the pass never visited. The cost is bounded — a re-derive parses and upserts a vault the pass already reads in full and makes no embedding call for unchanged content — and it SHALL be operator-visible: the pass SHALL name the offending paths in its log on every pass, so the file to fix is identified rather than left as an unexplained recurring cost. A capped note, by contrast, does not keep the user in re-derive mode: it is complete by construction and its degradation is carried on the row, not in the pass's skip list.

#### Scenario: An undecodable file withholds the record

- **WHEN** a re-deriving pass discovers a file it cannot decode and completes the rest of its work
- **THEN** the pass SHALL record no provenance for that user
- **AND** the next pass SHALL re-derive again

#### Scenario: A foreign row behind a skipped path is never certified

- **WHEN** a user was indexed from one vault, is assigned another, and the newly assigned vault holds a file at the same relative path whose bytes cannot be decoded
- **THEN** the pass SHALL NOT record the newly assigned root's provenance
- **AND** a later pass SHALL NOT take the keep branch over the row that path still carries

#### Scenario: A file that disappears during the scan withholds the record

- **WHEN** a file is discovered by a re-deriving pass and can no longer be read when the pass reaches it
- **THEN** the pass SHALL treat that path as a skip and SHALL record no provenance for that user

#### Scenario: Every link-extraction skip is recorded, including the unreachable one

- **WHEN** a re-deriving pass reaches a changed note it cannot extract links for — because it holds no buffered body for that path, or because that path has no index row to attach the links to
- **THEN** both cases SHALL be recorded as skips, so the record is withheld
- **AND** neither SHALL be dropped silently, whatever its likelihood, because the record is a claim that every surviving link row was written by that pass

#### Scenario: A capped note does not withhold the record

- **WHEN** a re-deriving pass reaches a changed note with more than `MAX_LINKS_PER_NOTE` links and processes every other discovered file without a skip
- **THEN** the first `MAX_LINKS_PER_NOTE` links SHALL be written, `links_truncated` SHALL be set on the note, an ERROR line SHALL be logged, and the pass SHALL record the provenance of the directory it scanned

#### Scenario: The skipped paths are named

- **WHEN** a re-deriving pass is incomplete
- **THEN** it SHALL log the paths responsible, bounded to a stated number with a count of the remainder

#### Scenario: A complete re-derive is recorded

- **WHEN** a re-deriving pass processes every discovered file without a skip and raises nothing
- **THEN** it SHALL record the provenance of the directory it scanned, after its last write

### Requirement: The migration introducing the record asserts no provenance, and the deploy order is stated
The migration that introduces the provenance record SHALL leave every column of it unset for every existing row and SHALL NOT derive any of them from the current vault assignment. "Assigned now" does not establish "indexed under what is assigned now" — reassignment lag is the defect the record exists to close — so a backfill from the assignment would stamp rows built under one assignment as belonging to another, after which both recorded facts agree, the no-op branch is taken, and the link case that never heals is guaranteed rather than merely possible.

Because the migration writes no provenance, an index pass running under the previous code during or after the migration SHALL have no record to contradict: the previous code cannot write these columns, so every row is unset when the new code starts, and the first pass per user takes the unresolved branch and re-derives. This SHALL be documented as the reason the deploy is safe, and it SHALL NOT be described as serialisation: the index pass lock is process-local, and no advisory lock, row lock or other cross-container coordination exists between a migration container and a running application container.

The system SHALL document that overlap between two indexing containers of this service is prevented by the deploy replacing the container rather than by any code-level guarantee, and that a deploy which runs two such containers concurrently can let a pass under the previous code write rows from the previous root after a new pass has recorded the new one.

#### Scenario: The migration stamps nothing

- **WHEN** the migration introducing the record runs on a database holding both assigned and unassigned users
- **THEN** every column of the recorded provenance SHALL be unset for every row, including every assigned user's

#### Scenario: A reassignment made before the upgrade is still reconciled

- **WHEN** a user is reassigned to a different vault and the upgrade runs before the next index pass
- **THEN** the first pass after the upgrade SHALL NOT treat that user's index as built from the newly assigned root
- **AND** SHALL re-derive it from the newly assigned root

#### Scenario: A pass under the previous code cannot forge a record

- **WHEN** an index pass under the previous code commits `notes_metadata` rows after the migration has committed
- **THEN** the recorded provenance SHALL remain unset for that user, because the previous code has no code path that writes it
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

### Requirement: A masker grammar change forces re-derivation without corrupting note identity

Link rows, inline tags, and embedded vectors are derived through the shared fence recognizer, but re-derivation is normally gated on `content_hash`, which does not change when only the grammar changes. A grammar change SHALL therefore ship with a versioned re-derivation mechanism: a per-note extraction-version marker, distinct from `content_hash`, compared against a code-level current version. When a note's marker is stale, the next index pass SHALL re-extract its links and tags, and SHALL then stamp the marker. Grammar-attributable embedding invalidation SHALL occur exactly when the note's **embedded text — the cleaned-for-embedding output — differs between the grammar version that stamped the note's recorded version and the current grammar**; each version's cleaning function is retained frozen while any row remains stamped with it, so the comparison is direction-aware and works for rollbacks as well as upgrades. The comparison is over cleaned output, not recognised-span tuples, because span equality is neither necessary nor sufficient for embedded-text equality (the v0 cleaner applied its two regexes sequentially, so adjacent mixed fences produce identical spans but different cleaned text, and vice versa). Independent invalidators (content change, `file_path` change, provider or configuration change, exclusion reconciliation) are cumulative: the cleaned-output comparison SHALL NOT suppress an invalidation any other requirement mandates. `content_hash` SHALL always hold the true hash of the note's bytes — it SHALL NOT be nulled or overwritten with a sentinel — so hash-based move/rename matching keeps working throughout the remediation window.

#### Scenario: The fence-grammar deploy refreshes links and tags

- **WHEN** this change's migration has run and the indexer completes its next pass
- **THEN** every note's `note_links` rows and extracted tags SHALL reflect the new grammar

#### Scenario: Embedding invalidation is scoped to affected notes

- **WHEN** the re-derivation pass processes a note whose cleaned-for-embedding output is identical under the stamped version's frozen cleaner and the current one, and no independent invalidator applies (no content change, no `file_path` change, no provider or configuration change, no exclusion reconciliation)
- **THEN** the grammar migration SHALL NOT cause that note to be re-embedded — cleaned-output comparison governs only invalidation attributable to the grammar change and never suppresses an invalidation another requirement mandates
- **WHEN** it processes a note whose cleaned output differs (e.g. one containing an indented fence around text)
- **THEN** that note's embeddings SHALL be rebuilt from the newly cleaned text on a subsequent embed pass

#### Scenario: Move detection survives the remediation window

- **WHEN** a note is externally renamed after the migration runs but before the re-derivation pass reaches it
- **THEN** the indexer's content-hash move matching SHALL still identify it as the same note (its `content_hash` is real), preserving row identity rather than delete-and-reinserting — the path-change invalidation the existing requirements mandate for a move still applies
- **AND** the ID-preserving move branch SHALL re-derive that note's links and tags under the current grammar and stamp its marker in the same transaction, so the note does not need a second pass to satisfy the next-pass refresh promise

#### Scenario: Rewrite-enabled moves refuse while re-derivation is incomplete

- **WHEN** any note in the caller's owner scope still carries a stale extraction marker and a client calls `move_note(from, to, rewrite_links=True)`
- **THEN** the move SHALL be refused before the rename, naming the in-progress re-derivation, because rewrite-source discovery reads `note_links` rows that a stale-grammar extraction may have wrongly omitted (a link inside a span the old grammar masked but the new grammar reads as prose would be silently left broken)
- **AND** the same move with `rewrite_links=False` SHALL be unaffected, and the refusal SHALL clear on its own once the re-derivation pass completes

### Requirement: The link rebuild writes per note

During a pass, the link rows derived from a changed note SHALL be inserted before the next changed note's links are extracted, so that peak memory for link rows is bounded by one note's derived rows (at most `MAX_LINKS_PER_NOTE`) plus one insert batch, rather than by the number of changed notes in the pass. Body buffering for the pass is unchanged by this requirement.

#### Scenario: Many changed link-heavy notes in one pass

- **WHEN** a pass processes N changed notes each carrying the maximum number of links
- **THEN** the number of link rows held in memory at any instant SHALL NOT exceed one note's worth plus one insert batch (asserted by instrumenting the insert path), and every note's rows SHALL be present in `note_links` when the pass commits

### Requirement: A truncated extraction is recorded on the note

`notes_metadata` SHALL carry a `links_truncated` boolean (default false) that the pass sets when a note's link extraction was capped and clears when a later extraction of that note completes under the cap. The marker SHALL survive restarts (it is a column, not a log entry) and SHALL be what `get_links` reads.

#### Scenario: Marker set and cleared

- **WHEN** a note is indexed with more than `MAX_LINKS_PER_NOTE` links, then edited down to fewer, and indexed again
- **THEN** `links_truncated` SHALL be true after the first pass and false after the second

### Requirement: The link-grammar change is re-derived through the extraction version

Because the link grammar changed and `content_hash` cannot see a grammar change, `CURRENT_EXTRACTION_VERSION` SHALL be bumped with this change, with the new version's cleaning function identical to the previous one, so that the next pass re-extracts links and tags for every note under the new grammar and stamps the marker, while the cleaned-output comparison finds no difference and no note is re-embedded.

#### Scenario: One re-extraction pass, no re-embedding

- **WHEN** this change is deployed and the indexer completes its next pass
- **THEN** every note's `note_links` rows SHALL reflect the linear grammar, every note SHALL carry the new extraction version, and no note SHALL have been re-embedded on account of the version change

### Requirement: The frozen v0 cleaner is linear and byte-identical

The frozen v0 cleaning function retained for `extraction_version` comparison SHALL run in time linear in the input length, including inputs with many unclosed fence openers, and SHALL produce output byte-identical to the original regex-based implementation for every input. It SHALL split on `\n` only — never on `\r`, `\v`, `\f`, ` ` or any other separator `str.splitlines()` recognises — and SHALL treat a closer line's trailing run under the regex's Unicode `\s` semantics (so NBSP, `\x0b` and ` ` after the fence run close). Equivalence SHALL be established by a differential test that keeps the original regexes as an oracle and compares outputs over generated inputs covering unclosed openers, orphan closers, nested and adjacent mixed backtick/tilde fences, trailing ASCII and Unicode whitespace on closer lines, blank-line runs after closers, CRLF and lone `\r`, indented fences, empty blocks, and closer runs longer than the opener, plus every existing v0/v1 fixture.

#### Scenario: Many unclosed openers

- **WHEN** the v0 cleaner is invoked on n and 2n bytes of ```` ```x\n ```` repeated (n = 160 KB)
- **THEN** the 2n time divided by the n time SHALL be below 4, each run SHALL complete under a generous absolute ceiling, and both outputs SHALL equal the oracle's

#### Scenario: Differential equivalence

- **WHEN** the differential test generates inputs from the covered classes
- **THEN** for every input the scanner's output SHALL equal the oracle regex pair's output byte for byte

### Requirement: No pass over a vault root SHALL begin without a quarantine snapshot published by the shared detection
Every code path that can begin an index, link-backfill, embed or tsvector-rebuild pass over a vault root SHALL first call one shared detection routine, and that routine SHALL be the only thing in the process that computes and publishes a quarantine snapshot. The routine SHALL evaluate the identity and containment conditions across the roots of all active users holding an assignment, taking each root's device, inode and canonical real path from **one opened directory descriptor** rather than from the assignment string, and SHALL issue no database write.

Installing detection in the periodic loop alone leaves it installed in one of five places. The panel's on-demand reindex — reached by **Reindex Now**, by *re-embed* and by *reset embeddings* — mirrors the loop and shares only `index_pass_lock` with it, and the standalone tsvector rebuild (`make rebuild-tsvectors`) is a **separate process** with its own user enumeration, no loop and no lifespan. Both reach a pass, and neither would consult a check placed in the loop. The requirement is therefore stated over the property — no pass begins unchecked — and the per-user stage skip SHALL live in the shared pass helpers rather than in each caller's loop, so a sixth entry point added later inherits the guard by routing through the same helper instead of by remembering to add a call.

The startup entry point SHALL run the detection **synchronously before the application serves its first request**, and SHALL NOT rely on the asynchronous startup pass for it: between accepting connections and that pass completing, a tool call would otherwise be served against roots nothing had checked.

#### Scenario: The scheduled loop publishes before it indexes

- **WHEN** a periodic iteration begins
- **THEN** a snapshot SHALL be published before any note beneath any root is read

#### Scenario: The startup path publishes before the application serves

- **WHEN** the application starts
- **THEN** the first snapshot SHALL be published before the first request is served and before the indexer task is created

#### Scenario: The panel's on-demand reindex publishes

- **WHEN** an administrator triggers Reindex Now, re-embed, or reset embeddings
- **THEN** the resulting pass SHALL publish a snapshot before it takes the pass lock
- **AND** SHALL skip a quarantined user exactly as a scheduled pass does

#### Scenario: The standalone tsvector rebuild publishes

- **WHEN** the standalone rebuild process is run
- **THEN** it SHALL publish its own snapshot before rebuilding any keyword vector
- **AND** SHALL rebuild nothing for a quarantined user

#### Scenario: The skip is enforced in the shared pass helper

- **WHEN** the pass helpers are inspected
- **THEN** the per-user skip SHALL be enforced inside them, so that every caller inherits it
- **AND** no caller SHALL be relied upon to re-implement it

#### Scenario: Single-user mode has nothing to detect

- **WHEN** the server runs in single-user mode, where the root comes from settings and no `users` row carries an assignment
- **THEN** the published snapshot SHALL be empty and every pass SHALL behave exactly as it does today

#### Scenario: Single-user mode publishes without reading anything

- **WHEN** the server runs in single-user mode and the `users` table nevertheless holds active rows carrying overlapping assignments — a flag flipped back, or a deployment that was multi-user before
- **THEN** the detection SHALL publish an empty snapshot **without** enumerating those rows and **without** opening any root
- **AND** the snapshot SHALL still be published, because the never-published state is a refusal and single-user mode must never sit in it

### Requirement: Each root's observation SHALL be bounded by a finite deadline
Observing a root — opening it, stating the descriptor and resolving its canonical real path — SHALL be dispatched off the event loop and bounded by a finite, configurable deadline. Expiry SHALL be a per-user verdict of **root unexaminable with a timeout cause**, distinguishable from an error number, and the detection SHALL continue to the remaining roots and publish. Expiry MUST NOT be treated as a failure of the detection as a whole.

Vault roots are bind mounts, and a network- or FUSE-backed one blocks in the kernel for as long as it likes. Two things break together without a deadline. The startup detection is deliberately synchronous — it is what makes the process closed rather than permissive before it serves — so an unbounded observation would hold the application before its first request, taking the control panel down at exactly the moment an operator opens it to find out why. And the detection critical section would be held for the whole stall, queuing every other entry point behind one hung mount.

Treating expiry as a detection failure would be the other error: the previous snapshot would be retained on every iteration a slow mount was slow, so a genuine overlap appearing later would never be published. A timed-out root is one user's verdict, and the users beside it are still observable.

The cause is recorded as a timeout rather than folded into the error numbers because the two need different responses — a hung filesystem and a deleted directory are different incidents — and the operator surfaces word them apart.

#### Scenario: A hung root does not stall startup

- **WHEN** one active user's root blocks indefinitely on being opened and the application starts
- **THEN** the startup detection SHALL complete within the deadline for that root
- **AND** the application SHALL begin serving, with the control panel available

#### Scenario: A timed-out root is one user's verdict

- **WHEN** one root's observation exceeds the deadline and two other roots are observable
- **THEN** that user SHALL be quarantined with a timeout cause
- **AND** the other two SHALL be observed and the snapshot SHALL be published

#### Scenario: A timeout is not a detection failure

- **WHEN** an iteration times out observing a root
- **THEN** the resulting snapshot SHALL be published rather than the previous one retained

#### Scenario: The timeout cause is distinguishable

- **WHEN** one user is quarantined for a timeout and another for a root that could not be opened
- **THEN** the two reasons SHALL be distinguishable wherever they are surfaced

### Requirement: Detection SHALL be serialized, and a publication SHALL NOT replace a newer one
The whole detect-and-publish operation — observing the roots, evaluating the conditions and publishing the result — SHALL run inside one process-global critical section, so that a second detection cannot begin until the first has published. Each snapshot SHALL carry a sequence number assigned when its detection begins, taken inside that critical section, and publication SHALL discard a snapshot whose sequence is not greater than the sequence of the snapshot already published.

Every entry point calls the detection *before* taking the pass lock, which is correct — the check must not queue behind the pass it exists to gate — and it means two detections are trivially concurrent: a periodic iteration and a panel-triggered reindex overlap, and the panel path is reached from three separate controls. The resulting failure is not theoretical and it fails **open**. A detection that began before an overlap appeared, stalled on a slow `open` of a network or FUSE-backed root, and finished after a newer detection had published the quarantine would publish its own **empty** result over it and re-admit both tenants until some later entry point ran. Atomicity of the swap does not address this: both writes are individually atomic and the wrong one is last.

Holding the critical section across the publication alone is insufficient and MUST NOT be substituted, because it permits exactly that interleaving. The sequence number is not redundant with the critical section: the section is the mechanism and the sequence is the invariant, and the invariant is what remains true for a future caller — a test, a fixture, an entry point added later — that publishes without entering the section.

#### Scenario: A stalled older detection does not overwrite a newer quarantine

- **WHEN** one detection begins, is delayed while observing a root, and completes after a second detection has already published a snapshot naming two overlapping users
- **THEN** the published snapshot SHALL still name those two users
- **AND** the older detection's result SHALL NOT replace it

#### Scenario: Detections do not interleave

- **WHEN** two entry points call the detection concurrently
- **THEN** the second SHALL NOT begin observing roots until the first has published

#### Scenario: An out-of-order publication is discarded

- **WHEN** a snapshot is published whose sequence is not greater than that of the snapshot already published
- **THEN** it SHALL be discarded and the published snapshot SHALL be unchanged

### Requirement: The published snapshot SHALL be atomic, tri-state, and SHALL NOT regress on a failed detection
Publication SHALL replace the snapshot with one immutable value in a single assignment, so no reader observes a partially built snapshot, and SHALL be monotonic in the sequence number described above. The snapshot SHALL be tri-state — never published, published and empty, or published with quarantine reasons — and a detection that raises **after** a snapshot has been published SHALL retain the previous snapshot and log at ERROR. It MUST NOT clear the snapshot back to the never-published state.

The three states answer three different questions and collapsing any two is a defect. "Never published" means nothing has been checked and the correct response is to refuse; "published and empty" means everything was checked and nothing overlaps; and a failed re-detection means the last complete answer is the best available one. Clearing on failure would turn a transient database blip into a deployment-wide refusal, and treating a failure as an all-clear would serve overlapping roots on the strength of a query that never returned.

A detection failure is not a per-root failure: a root that cannot be opened is a per-user verdict, so the only way the routine itself fails is that the user enumeration failed — which means the database is unavailable and the tools are unusable regardless. The routine SHALL therefore log and let the process keep serving the panel rather than exiting, and SHALL retry at the next entry point.

#### Scenario: A failed re-detection keeps the last snapshot

- **WHEN** a snapshot has been published and a later detection raises
- **THEN** the previously published snapshot SHALL remain in force
- **AND** the failure SHALL be logged at ERROR

#### Scenario: A failed first detection does not become an all-clear

- **WHEN** the first detection of the process raises
- **THEN** the snapshot SHALL remain in the never-published state
- **AND** the process SHALL keep serving the panel rather than exiting

#### Scenario: No reader sees a partial snapshot

- **WHEN** a detection is publishing while a request reads the snapshot
- **THEN** the reader SHALL observe either the previous snapshot or the new one in full

#### Scenario: Sandbox mode is ready without touching the filesystem

- **WHEN** the server starts in sandbox mode, where there are no users and the indexer is skipped
- **THEN** an empty snapshot SHALL be published without opening any root

### Requirement: The snapshot SHALL record why each user is quarantined, and an unexaminable root SHALL NOT be reported as an overlap
The snapshot SHALL map each quarantined user to a structured reason: an **overlap** carrying the peer user and the relation found (identical, contains, or contained by), or a **root unexaminable** carrying its cause and naming no peer. The cause SHALL be one of three, kept distinguishable wherever it is surfaced: an **error number**, a **timeout** (the observation exceeded its deadline), or an **unstable pathname** — the root opened but its canonical real path did not name the inode that was opened, so nothing observed describes one directory. Each entry SHALL additionally carry, as immutable facts observed at detection time, the subject's username and canonical assignment, the peer's username and canonical assignment for an overlap, and the moment the detection ran. Each reason SHALL be worded separately wherever it is surfaced — the control panel, the log line, the `indexer_runs` row and the usage-log marker.

Three causes and not two, for the same reason there are two reasons and not one: they are different incidents an operator acts on differently. A missing directory is a mount that was not applied; a timeout is a mount that is not answering; an unstable pathname is a root being retargeted *while the check runs*, which is the only one of the three that says something is moving underneath the server rather than absent from it. Folding the third into an error number would name an `errno` no syscall returned.

A root that could not be opened is not an overlap, whichever of the three causes it carries. Reporting it as one sends an operator looking for a second account that does not exist, and reporting it under the overlap marker makes the two indistinguishable in the usage log. The user is quarantined because their status could not be established, which is a different fact requiring a different fix.

Recording the facts rather than the ids alone is what keeps the condition legible while the operator acts on it. The first response to "this root overlaps that account's" is to edit or delete one of the two accounts, and between that edit and the next detection a surface that resolved names at render time would show a changed path — or a blank, where a deleted peer was — beside a condition still in force. The recorded facts also make the staleness honest, because a surface can label them as of the last check rather than presenting them as the present state.

An unexaminable root SHALL quarantine **only that user**. The peers it could not be compared against SHALL keep being indexed and served — fail closed for the user whose status is unknown, fail open for users against whom nothing was observed, so that one broken mount does not take the deployment offline.

#### Scenario: An overlap names the peer and the relation

- **WHEN** two users' roots are detected as overlapping
- **THEN** each user's reason SHALL name the other user and the relation found

#### Scenario: An unexaminable root names no peer

- **WHEN** a user's assigned root cannot be opened
- **THEN** that user's reason SHALL record that the root could not be examined, with the error number
- **AND** SHALL NOT name any peer user or claim an overlap was observed

#### Scenario: A pathname moving under the check is its own cause

- **WHEN** a user's root opens but its canonical real path does not name the inode that was opened
- **THEN** that user SHALL be quarantined as root unexaminable with the unstable-pathname cause
- **AND** that cause SHALL be distinguishable from both an error number and a timeout wherever it is surfaced
- **AND** it SHALL NOT be reported as an overlap

#### Scenario: The pair stays nameable after the peer is changed

- **WHEN** an overlap is published and an administrator then corrects or deletes one of the two accounts, before a later detection publishes
- **THEN** the surfaces SHALL still name both accounts and both roots from the facts recorded in the snapshot
- **AND** SHALL present them as observed at the last check rather than as the current state

#### Scenario: An unexaminable root quarantines only its own user

- **WHEN** one active user's root cannot be opened and two other users hold unrelated, examinable roots
- **THEN** only the first user SHALL be quarantined
- **AND** the other two SHALL be indexed normally

### Requirement: The all-scopes keyword rebuild SHALL check every root it will open, before it takes the generation lock
The maintenance rebuild that enumerates scopes from the rows that exist — rather than from the active users the server serves — SHALL evaluate the identity and containment conditions across **every root it would open**, including the retained root of an inactive owner, together with the roots of all active users holding an assignment. Any relation involving a root it would open SHALL abort the whole operation, naming both sides and the relation; a root it would open that cannot be observed SHALL be a non-completed outcome and SHALL abort the operation in the same way. The observation SHALL be complete before the index generation lock is taken, and **no pathname SHALL be resolved while that lock is held**: the observation SHALL retain the directory descriptor it opened for each scope, the rebuild SHALL read through that descriptor, and the only filesystem call made on a root inside the locked section SHALL be a status check of the descriptor already held.

Retaining the descriptor answers two failures that reopening the pathname does not. The reopen is an unbounded synchronous call, so a network- or FUSE-backed mount that stops answering holds the index generation lock for as long as the kernel takes and every pass in every process queues behind it. And it is a second lookup, so it may resolve to a directory the observation never examined; a descriptor is the only reference that still names the same directory across the wait for the lock. The assignment SHALL nevertheless still be compared, because a scope reassigned in that interval holds a descriptor that is the wrong directory to rebuild — correct in identity, wrong in tenancy.

Every retained descriptor SHALL be closed on completion, on abort, and on an unexpected failure, and a descriptor opened by an observation that had already exceeded its deadline SHALL be closed when that observation completes. Descriptors for roots the command would not open SHALL be released as soon as the checks are done.

#### Scenario: No pathname is resolved after the lock

- **WHEN** the survey has succeeded and the rebuild runs
- **THEN** no vault-root pathname SHALL be opened after the generation lock is acquired
- **AND** each scope SHALL be rebuilt through the descriptor the survey retained for it

#### Scenario: A root renamed after the survey does not redirect the read

- **WHEN** a scope's assigned pathname is repointed to a different directory between the survey and the rebuild
- **THEN** the rebuild SHALL still read the directory the survey examined

#### Scenario: Every retained descriptor is released

- **WHEN** the rebuild completes, aborts, or raises
- **THEN** every descriptor the survey retained SHALL be closed

### Requirement: The all-scopes keyword rebuild SHALL hold the account-administration guard across its survey and its reads
The maintenance rebuild SHALL acquire the same cross-process advisory guard that the account-administration handlers take — the one held by the administrative user-management handlers, the self-service password change and every session mint — **before** it enumerates the roots it will open, and SHALL hold it until its transaction commits or rolls back. The lock order SHALL be **account guard, then index generation lock, then row locks**, in that direction on every path that takes more than one, and the ordering rule SHALL be recorded at each lock's definition.

Without it the survey is check-then-act across processes. The survey accepts a layout in which two roots nest whenever the conflicting user is inactive — which is correct, because nothing serves or indexes an inactive user — and an administrator may reactivate or reassign that user while the command is still running. The reads that follow are then the cross-tenant read the survey exists to prevent, and no re-check inside the command's own transaction can prevent it, because the edit is a separate connection committing between the check and the read. Serializing against the handlers that make those edits is the only mechanism that closes it, and that guard already exists for exactly this class of check-then-act.

The cost SHALL be accepted rather than mitigated: while the rebuild runs, account edits and session mints wait. It is an operator-initiated one-off command, the alternative is a cross-tenant read, and the guard is transaction-scoped so a crashed rebuild releases it with no operator action.

#### Scenario: An assignment edit cannot land between the survey and the reads

- **WHEN** the rebuild has completed its survey and has not yet read a row
- **THEN** another connection SHALL NOT be able to acquire the account-administration guard

#### Scenario: The guard precedes the enumeration

- **WHEN** the rebuild runs
- **THEN** the account guard SHALL be acquired before the roots it will open are enumerated

#### Scenario: The guard is released by the transaction

- **WHEN** the rebuild commits or rolls back
- **THEN** the account-administration guard SHALL be released without operator action

#### Scenario: The rebuild reads only scopes the surveyed population examined

- **WHEN** the rebuild reads any scope
- **THEN** that scope SHALL have been examined by the survey in force

The published quarantine snapshot cannot answer for this command, and this is a population difference rather than an oversight. The snapshot observes active users holding an assignment because that is exactly whom the server serves and indexes; this command opens the scope of an **inactive** owner too, because an inactive user's retained rows are as returnable by keyword search as anyone's and the coverage proof is about rows that exist. An inactive owner retaining a root that is an ancestor or an alias of an active tenant's is therefore named by nothing the snapshot publishes, and the rebuild would read that tenant's notes under the inactive owner's scope and record a fingerprint asserting that every retained row was rebuilt correctly.

This check SHALL be maintenance-only: it SHALL publish nothing and SHALL NOT change which users the serving snapshot names. The serving population MUST stay "active users holding an assignment" — quarantining an inactive account refuses nothing, because nothing serves it, while making an active peer appear implicated.

Completing the observation before the lock is a separate requirement from the check itself and neither substitutes for the other. Opening a root synchronously after the lock lets one hung mount hold the index generation lock for as long as the kernel takes to answer, and every pass in the process queues behind it — so the verdict is taken through the same bounded, off-loop observation the detection uses, and carried into the locked section.

A root that this command would **not** open and that cannot be observed SHALL NOT abort it. Nothing was observed to relate that root to anything, which is the same residual already recorded for an inaccessible peer, and failing a maintenance command because one unrelated tenant's mount is down is the false-positive direction this system treats as the expensive error.

#### Scenario: An inactive owner's retained root containing an active tenant's aborts the rebuild

- **WHEN** an inactive user retains rows under a root that contains an active user's assigned root, and the all-scopes rebuild is run
- **THEN** the rebuild SHALL abort, naming both roots and the relation
- **AND** no vault root SHALL have been opened
- **AND** no keyword vector and no fingerprint SHALL have been written

#### Scenario: The check happens before the generation lock

- **WHEN** the all-scopes rebuild runs
- **THEN** every root it will open SHALL have been observed before the generation lock is acquired
- **AND** no root that was not observed SHALL be opened while the lock is held

#### Scenario: A scope whose root cannot be observed aborts like any other skip

- **WHEN** a root the rebuild would open cannot be observed within the deadline, or cannot be opened at all
- **THEN** that scope's outcome SHALL be a non-completed one naming the cause
- **AND** the rebuild SHALL abort and record no fingerprint

#### Scenario: The maintenance check does not move the serving snapshot

- **WHEN** the all-scopes rebuild runs its check and aborts
- **THEN** the published quarantine snapshot SHALL be unchanged
- **AND** the users the admission gate refuses SHALL be exactly the users it refused before

#### Scenario: Sibling roots rebuild normally

- **WHEN** every scope's root is unrelated to every other
- **THEN** the rebuild SHALL proceed and record the fingerprint exactly as before

### Requirement: A quarantined user SHALL NOT be indexed, and unrelated users SHALL be
A pass SHALL skip the index, link-backfill, embed and tsvector-rebuild stages for every user the published snapshot names, and SHALL run all of them normally for every active user it does not name. The skip SHALL NOT delete, prune or otherwise mutate any `notes_metadata`, `note_embeddings` or `note_links` row belonging to a skipped user, and SHALL NOT write that user's provenance record.

Continuing to index an overlapping pair files one tenant's notes under the other tenant's `user_id`, which makes them answerable by `semantic_search`, `keyword_search` and every graph tool — a silently wrong search result delivered to an agent, which is the failure this server ranks highest. Refusing the named users is the narrowest control that stops it: the condition is a property of specific roots and says nothing about a third tenant's vault, so quarantining the deployment would convert a two-tenant misconfiguration into an outage for everyone.

Nothing is deleted for the same reason unassignment deletes nothing: preserving the rows is what makes a corrected assignment cheap, and the repair the operator's correction triggers — a discard or a re-derive from the provenance classification, plus the ordinary prune of rows whose files are no longer beneath the root — is machinery that already exists and is already reviewed. A blanket delete would be a second, unreviewed deletion path over index contents.

#### Scenario: Unrelated tenants keep indexing

- **WHEN** users A and B hold overlapping roots and user C holds an unrelated root
- **THEN** the pass SHALL index, backfill and embed C exactly as before
- **AND** SHALL perform none of those stages for A or B

#### Scenario: No rows are destroyed by the refusal

- **WHEN** a pass skips a user named by the snapshot
- **THEN** that user's `notes_metadata`, `note_embeddings` and `note_links` rows SHALL be unchanged
- **AND** the user's recorded vault provenance SHALL be unchanged

#### Scenario: A root that becomes aliased after assignment is detected at the next entry point

- **WHEN** two users hold non-overlapping assignments, both are indexing normally, and one user's assigned path is subsequently made a symbolic link to — or a bind mount of — the other user's root
- **THEN** the next detection SHALL name both users
- **AND** neither SHALL be indexed until the condition is corrected

#### Scenario: A root that becomes nested after assignment is detected

- **WHEN** one user's assigned path is subsequently replaced by a symbolic link resolving to a directory inside another active user's root
- **THEN** the next detection SHALL find it through the canonical real paths, not through the unchanged assignment strings

#### Scenario: A corrected condition resumes indexing

- **WHEN** an administrator changes one of the two roots so that no condition holds
- **THEN** the next detection SHALL publish a snapshot naming neither user
- **AND** both SHALL be indexed again, with the existing provenance classification deciding whether the previous rows are kept, re-derived or discarded

### Requirement: A quarantine SHALL be recorded durably for each affected user, and a pause SHALL NOT suppress the record
A pass that skips a user because the snapshot names them SHALL log the fact at ERROR with the reason-specific wording, and SHALL record it in that user's `indexer_runs` row so the record survives a container restart. An iteration that finds the indexer **paused** SHALL still publish the snapshot, still emit the ERROR log and still write those per-user run rows before returning; the pause suppresses index and embed work only.

Two records because they answer different questions over different lifetimes. The log line reaches the in-process error ring buffer, which is 100 entries and process-lifetime: the line naming a quarantine at deploy time is gone by the next restart while the misconfiguration persists. The run row is what an operator reads after a restart, and a pass that quietly did no work for a user would otherwise be indistinguishable from a pass that found nothing to do. A pause is entered precisely when an operator is doing something destructive and watching the panel, which is the worst moment for a quarantine to become invisible; and the row cadence is unchanged, because a running deployment already writes one row per user per iteration.

#### Scenario: The skip reaches the run row

- **WHEN** a pass skips a user for a quarantine
- **THEN** an `indexer_runs` row SHALL be written for that user
- **AND** its error text SHALL carry the reason-specific wording, naming the peer for an overlap and the error number for an unexaminable root

#### Scenario: The skip reaches the error buffer

- **WHEN** the same pass runs
- **THEN** it SHALL log at ERROR, so the health page's recent-errors section shows it while the process lives

#### Scenario: A paused iteration still records

- **WHEN** an iteration begins while the indexer is paused and the snapshot names at least one user
- **THEN** the snapshot SHALL still be published, the ERROR SHALL still be logged, and the per-user run rows SHALL still be written before the iteration returns
- **AND** no index or embed work SHALL be performed

#### Scenario: A skip is not reported as a healthy pass

- **WHEN** every active user in a deployment is skipped for a quarantine
- **THEN** the pass SHALL NOT be recorded as a clean run for those users

### Requirement: A non-finite frontmatter number never fails an index pass

The indexer SHALL store a non-finite YAML float from a note's frontmatter as the canonical YAML token — `.nan`, `.inf`, or `-.inf` — in the `notes_metadata.frontmatter` JSONB column, and a note carrying one SHALL NOT be able to abort, stall, or repeatedly retry an index pass.

`NaN`, `Infinity` and `-Infinity` are not valid JSON and PostgreSQL's `jsonb` parser rejects them, so a float that reaches the column unconverted raises inside the batch upsert. That batch has no per-note retreat: the pass's single transaction aborts, nothing commits, no note's `content_hash` advances, and every subsequent tick retries the same fatal batch — one note taking indexing down for the whole owner. The conversion SHALL therefore happen at the indexer's own JSON boundary — the sanitisation applied to a parsed frontmatter mapping before it is written — which is the same boundary that already stringifies dates and non-string keys.

The token SHALL be the canonical lowercase form whatever spelling the note used (`.NaN`, `.INF`, `+.inf` and the rest all load to the same float, and the parse preserves none of the spelling), and it SHALL be YAML's spelling rather than Python's `nan` / `inf`, so that the indexed value, the note's own frontmatter and every tool that displays it agree, and so `keyword_search(frontmatter=…)` matches the token a person would write.

The coercion SHALL apply to mapping **keys** as well as values, since the sanitiser stringifies non-string keys on the same walk. When two keys collide after coercion, **the first key in document order SHALL win**, stated as a rule rather than left as an accident of iteration order — today's dict comprehension silently keeps the *last*. The index has no channel through which to report the loss and SHALL NOT fail the pass for it; a deterministic, documented winner is the available remedy.

The shared frontmatter representability boundary SHALL NOT be changed to remove or coerce non-finite floats: it drops only what nothing can render, both Python and YAML render these, and the parsed mapping is what `set_frontmatter` re-serialises — a coerced string there would rewrite the note's own bytes as a side effect of an unrelated key.

#### Scenario: A note with a non-finite frontmatter number indexes

- **WHEN** a note whose frontmatter contains `x: .nan` is discovered by an index pass
- **THEN** the pass SHALL complete, the note SHALL be upserted with `frontmatter` carrying `x` as the string `.nan`, and every other note in the same batch SHALL be committed

#### Scenario: One such note cannot wedge the index

- **WHEN** a vault contains a note with `a: .inf` and `b: -.inf` in its frontmatter, that note's body is edited between two index passes, and both passes run
- **THEN** both passes SHALL complete, the note's stored `content_hash` SHALL advance to the hash of the edited note, and neither pass SHALL raise on the JSONB write

#### Scenario: An alternate spelling is stored canonically

- **WHEN** a note's frontmatter contains `x: .NaN` and `y: +.inf`
- **THEN** the stored JSONB values SHALL be `.nan` and `.inf`

#### Scenario: A non-finite mapping key is stored canonically, first key winning

- **WHEN** a note's frontmatter maps `.nan: 1` and, after it, `".nan": 2`
- **THEN** the stored JSONB object SHALL carry the key `.nan` with the value from the **first** of the two, and the pass SHALL complete

#### Scenario: The note's own bytes are never rewritten

- **WHEN** `set_frontmatter` sets an unrelated key on a note whose frontmatter contains `x: .nan`
- **THEN** the published block SHALL still contain `x: .nan` byte-identically, and no coerced string form SHALL appear in the note

### Requirement: One title normalization is shared by every consumer that shows a title

The coercion that turns a frontmatter `title` into a displayable string SHALL be a single shared rule applied identically by the indexer's `notes_metadata.title`, by the read path that serves `read_note`, and by the control panel's note viewer.

**That rule SHALL be the indexer's present `_note_title` behaviour** — the sanitised value, falling back to the filename stem when it is falsy, rendered with `str()` and bounded to 512 characters, where the sanitisation stringifies non-string mapping keys and non-JSON scalars *inside* a container before the outer rendering — **with exactly one exception: a non-finite number SHALL render as its canonical YAML token.** The indexer's is the rule to standardise on because it is already the value search results, listings and the panel's lists show, it is bounded to the column's width, and it is the one of the three that a titling incident has already hardened.

The indexer's JSONB sanitisation and its title coercion SHALL be separate functions over the shared token helper rather than one function whose return value silently answers both questions — "what may this value become in a JSONB document?" and "what is this note called?" — because today one function decides both, so a change made for the column silently re-keys titles.

Adopting it changes what the read path and the panel show in three cases besides the non-finite one, and those changes SHALL be stated with their expected outputs rather than discovered: a date inside a container renders as the stringified element (`['2026-08-25']`, not a Python `repr` of a date object); a non-string mapping key renders stringified (`{'1': 'a'}`); and a title longer than 512 characters is bounded to its first 512. A date at the top level, a list of strings, a numeric title, and every falsy title (`0`, `false`, an empty string, an empty list — all of which fall back to the filename stem) SHALL be unchanged.

#### Scenario: A non-finite title agrees across tools

- **WHEN** a note's frontmatter is `title: .nan` and the note is indexed
- **THEN** `notes_metadata.title`, the `title` field of `read_note`'s response, and the title the control panel shows SHALL all be `.nan`

#### Scenario: A date inside a container

- **WHEN** a note's frontmatter is `title: [2026-08-25]`
- **THEN** all three surfaces SHALL show `['2026-08-25']`

#### Scenario: A non-string mapping key in a title

- **WHEN** a note's frontmatter title is a mapping with the non-string key `1`
- **THEN** all three surfaces SHALL show the key stringified, as `{'1': 'a'}` for the value `a`

#### Scenario: A title longer than the column

- **WHEN** a note's frontmatter title is a 600-character string
- **THEN** all three surfaces SHALL show its first 512 characters

#### Scenario: Ordinary and falsy titles are unaffected

- **WHEN** notes carry a plain string title, a top-level date, a list of strings, a numeric title, and each falsy title (`0`, `false`, `""`, `[]`)
- **THEN** every one of those SHALL render exactly as the indexer renders it today, with each falsy title falling back to the filename stem

