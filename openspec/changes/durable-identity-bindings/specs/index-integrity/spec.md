## ADDED Requirements

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

Nothing else about the pass changes: it still selects only rows whose `embedded_content_hash` differs from their `content_hash`, still reads beneath the root it pinned, and still writes nothing for a note it skipped. The verification governs the path that *embeds* content; the exclude-pattern branch, which reads no file, writes no vector and marks the row from its own recorded hash, is unaffected by it and by the un-gating alike.

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
Any per-file skip during a re-deriving pass SHALL make that re-derive **incomplete**, and an incomplete re-derive SHALL NOT record provenance for that user. A skip is any discovered file the pass did not fully process — a directory it could not open, a file it could not read, stat, decode or parse, or a changed note whose links it could not extract. An incomplete pass SHALL still perform every repair it can, SHALL log the paths that kept it unrecorded, and the next pass SHALL re-derive again.

Without this rule the pass's structural claim is false. The scan continues past a file it cannot decode or read, and ordinary pruning keeps a row whose relative path exists under the assigned root — which is exactly the row a re-derive exists to replace. A vault that supplies a note at the same relative path as the previous vault, but whose bytes cannot be decoded, therefore leaves the previous vault's metadata row and its link rows untouched while the pass completes and records the new directory over them. One skipped file is enough to certify a foreign row.

The rule fails toward re-work rather than toward wrongness, and that is the trade the system SHALL take. The alternative — transactionally deleting the stale rows for each skipped path, as a fresh index would — is a second deletion path for index contents, and it destroys a row that may be the correct row for a file that was merely unreadable at that moment.

The system SHALL accept, and document, that a file which is permanently unreadable keeps that user in re-derive mode indefinitely. That is preferable to the alternative, which is recording a claim the pass could not establish: the record would then license the keep branch over rows the pass never visited. The cost is bounded — a re-derive parses and upserts a vault the pass already reads in full and makes no embedding call for unchanged content — and it SHALL be operator-visible: the pass SHALL name the offending paths in its log on every pass, so the file to fix is identified rather than left as an unexplained recurring cost.

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
