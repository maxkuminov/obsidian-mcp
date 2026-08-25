## Why

Three defects, one shape. In each of them the system holds a **binding** — this
index came from that root; this usage row was produced by that credential; this
request may write to that vault — and the binding is allowed to go stale
without anything noticing. Two of them are fixed by *recording the fact where
it happens*, which is what makes a column unavoidable in both. The third is
fixed by *re-reading the fact before acting on it*, which is what makes it a
write-path refusal rather than a column.

They ride together because two of them carry migrations, and the project's
standing rule is that migration numbers are assigned up front across the whole
wave or exactly one worktree owns schema changes. **016 and 017 are assigned
here.** One `make test-schema` run and one adversarial pass covers both.

### 1. The index does not record which vault assignment it was built under (#91, deferred half)

`truthful-surfaces` shipped the display half of #91 and deferred this one to
the next migration-carrying wave, preserving the analysis in
`DEFERRED-91a.md`. This is that wave. The argument below has been re-derived
against the tree at `2a430f9`; where the code has moved since `1ce4c9d` it is
noted.

`_active_user_ids()` filters `vault_path IS NOT NULL`, so an unassigned user's
index rows are frozen rather than removed. Since #66 that is deliberate: every
tool is refused meanwhile — the admission gate is in `_tracked` — so the rows
are not a leak, and keeping them is what lets a reassignment resume without
re-embedding ~16.7k chunks. `mcp-request-routing` pins it: reassignment to the
same directory SHALL still find the previously indexed rows present.

**A different assignment is a different question, and nothing answers it.**
`notes_metadata.file_path` is vault-relative; no row, and no column anywhere,
records which assignment the index was built under. After an admin repoints a user at
another vault, the metadata-only tools — `semantic_search`, `keyword_search`,
`list_notes`, `get_recent` and every graph tool, all served from the database
filtered by `user_id` alone — answer from the *previous* vault: its paths,
titles, tags, frontmatter and chunk excerpts. `read_note` on one of those paths
then either fails or, worse, returns a genuinely different note that happens to
occupy the same relative path in the new root. CLAUDE.md names "silently wrong
search results" as one of the two expensive failures in this product, because
an agent acts on them without a human ever seeing the query.

**Say the obvious objection out loud, because it is mostly right.**
`index_vault` already prunes by relative path — `deleted_paths = set(existing)
- set(files)` at `src/services/indexer.py:180` — so every stale row whose
relative path does not exist under the new root is removed on the first pass
over that root, with none of the machinery below. Two things genuinely do not
reconcile, and they are the whole justification:

1. **The window.** The prune happens at the next pass, so between the Save and
   that pass the database-backed tools answer from the previous root. The
   column does not remove this either — see the accepted residual — but it
   narrows what is served during it from *the previous vault* to *nothing*.

2. **A note identical by relative path *and* content hash in both roots.**
   `index_vault` classes it "no change" and `continue`s (`indexer.py:158-159`),
   so its links are never re-extracted. Meanwhile the notes it *pointed at*
   were pruned, and `note_links.target_note_id` is `ON DELETE SET NULL`
   (`source_note_id` is `ON DELETE CASCADE`, which is why the source's own rows
   survive). The result is a link row that keeps its `target_path` and loses
   its resolution: a resolved link silently becomes dangling, permanently,
   because nothing will ever re-extract it — the content hash matches, so the
   note is never re-parsed again. `get_backlinks`, `get_neighborhood` and
   `find_orphans` then report an under-resolved neighbourhood for that note for
   as long as it is unedited. **This is not a window; it does not heal.** It is
   the item that makes a schema change worth carrying, and it is the one a
   reviewer will not find on their own.

**Why a column rather than a comparison in the panel.** The tempting cheap fix
is to compare old and new in `edit_user_submit` and purge when they differ. It
does not work, because of the transition an operator actually performs:

    /old  →  unassigned  →  /new

Two Saves, because the panel's vault selector is how an admin takes an account
out of service before repointing it. On the second Save the handler sees
`old_vault = None`, and `None → /new` is *exactly* the shape #66 protects — a
user restored to the directory their index came from. The handler cannot tell
"reassigned back to where the index came from" (keep) from "reassigned
somewhere else" (discard), because the only thing that distinguishes them is a
value that no longer exists anywhere by the time it is needed. What is required
is not a comparison but a **record**: a value describing the assignment the rows
were scanned under, independent of the *current* assignment and therefore
surviving the unassignment.

**What round 4 changed, and why it is a rescope rather than another round of
hardening.** Rounds 1–3 attacked one claim — *this is the same directory the
rows were scanned from* — with an escalating series of substitutions: a
pathname, then a device-and-inode pair, then a kernel file handle, then a
bit-identical clone of the filesystem presenting the same handle at the same
pathname. Every round's fix was correct and every round's next attack was also
correct, which is precisely the signal CLAUDE.md names: **a heuristic
pretending to be a rule**, absorbing another round of tuning each time. Proving
directory identity across time from userspace is unwinnable by construction —
a clone is indistinguishable from the original by every fact the kernel will
hand a process that is not the storage layer.

So the claim is narrowed to one the system can actually make, and it is the one
the issue asked for. **#91 asks "did the assignment change?", not "is this the
same directory?"** The event it names — an operator repointing a user at
another vault through the panel — is a change to a value *this system stores
and writes*, so detecting it is a rule: exact, cheap, and with no input that
defeats it. Everything below is that rule, plus one best-effort refusal on top
of it, plus a declared boundary where the escalation used to live.

Round 3's headline failing input was checked by hand before this rescope was
taken, and it does not in fact produce a wrong outcome. Keeping an index over a
substituted (cloned) root is not "serving the old vault": the ordinary scan
prunes every path the clone lacks and the link rows pointing at them go to NULL
by `ON DELETE SET NULL` — which is the same end state a *fresh* index of the
clone reaches, because freshly extracting a link to a note that does not exist
yields a dangling link too. The variant that genuinely goes wrong — a note
whose relative path *and* content hash are identical in both, so it is never
re-parsed and its resolved link is never re-extracted — is a defect **today's
incremental indexer already has on a single vault, with no reassignment
anywhere in the story**. It is not this change's to fix. See the non-goal
below, which states it precisely and says where it should be fixed.

**Decision: 016 adds three columns — `users.indexed_vault_assignment`,
`users.indexed_vault_realpath` and `users.indexed_vault_handle`, nullable, one
marked unit, written only by the index pass — and 016 backfills none of them.**
Together they are the **provenance** of that user's rows: the assignment the
pass ran under, the directory that assignment named at that moment, and — where
the filesystem offers one — a kernel file handle for it. The pass reads them at
the head of `index_vault(user_id)` and compares them with the same three facts
observed for the assigned root now. Skipped entirely for `user_id is None`:
single-user mode has no `users` row.

The three facts do three different jobs, and conflating them is what the first
three rounds kept doing:

- **`indexed_vault_assignment` — the canonical assignment string, and the
  load-bearing fact.** `str(Path(vault_path))`: the same normalisation
  `transfer.canonical_vault_root` performs for #88's pre-publish confirmation,
  and the same form `vault._vault_root` yields. It changes when an operator
  reassigns and for no other reason, because it *is* the operator's saved
  value. Comparing it answers the question #91 asked, exactly, with no
  filesystem read and nothing to spoof from inside the vault.
- **`indexed_vault_realpath` — `os.path.realpath` of the root as the pass
  actually scanned it**, proven at that moment to name the descriptor the pass
  pinned. Its job is *not* to prove identity. It is to stop the assignment
  comparison from firing destructively on a cosmetic rename: `/vaults/current`
  (a symlink to `/data/A`) and `/data/A` differ as strings and agree as
  realpaths, so reassigning a user from one to the other re-derives instead of
  charging a full re-embed of a vault that did not move. The converse — one
  string naming a different directory — makes the two disagree as well, and
  that too routes to the cheap safe branch; but it is a *signal*, not a
  guarantee, and the non-goal below says so rather than letting a reader infer
  otherwise. **It is recorded and compared as the hexadecimal encoding of its
  filesystem bytes** — `os.fsencode(realpath).hex()` — because a kernel-returned
  pathname is an arbitrary byte sequence and only this fact is; see the column
  rationale below.
- **`indexed_vault_handle` — best-effort hardening, and only in the refusing
  direction.** Where a handle was recorded *and* one can be read for the
  assigned root now, and the two differ, a verdict that would otherwise have
  been **keep** is demoted to **re-derive**. That is the whole of its
  authority. **A matching handle proves nothing extra and never upgrades any
  verdict**, and where no handle is available on either side the hardening is
  simply *absent* — no degraded mode, no re-derive-on-every-pass, no
  warn-once machinery. A NULL in this column means "no hardening signal here",
  never "provenance unknown".

The handle is kept because it is free and it only ever refuses. On ext4 and xfs
a directory's handle is its inode number plus the inode's **generation
counter**, which the kernel bumps precisely so a reused inode is not mistaken
for the old one; measured on this host, `rmdir` + `mkdir` in a loop reused the
inode on the **first** recreation (`dev=66306 ino=19944872` before and after)
while the handle changed from `a85530010b6f671e` to `a8553001fbe0f6ef`. So a
directory replaced in place under an unchanged assignment is *usually* caught,
and being caught costs a cheap re-derive. It is not promised, because a handle
is unique only within its filesystem and filesystems can be cloned — which is
exactly why it is hardening here and proof nowhere.

Three further measurements, because they decide how the hardening is reached:

- **The wrapper exists.** `ctypes.CDLL(None).name_to_handle_at` resolves on
  glibc 2.39 (this host) and 2.41 (`python:3.12-slim`, the deployment base);
  glibc has exported it since 2.14. So unlike `openat2` — which the
  `atomic-beneath-root-writes` change must reach through a raw `syscall(2)`
  number table because glibc exports no wrapper at any version — this takes the
  wrapper-first shape `rename_noreplace` already uses and needs no architecture
  table. Verified by `getattr` on both, not assumed. A missing symbol is
  "no hardening available", not an error.
- **No capability is required, and a bind mount does not disturb it.** In a
  `--cap-drop ALL --user 1000:1000` container with the repository bind-mounted
  at another path, the handle for that directory was byte-identical to the
  handle read on the host — `2fb398003402e116` both times — while the kernel's
  mount id differed (6307 in the container, 31 on the host); the design
  therefore ignores the `mount_id` the call also returns. Only
  `open_by_handle_at` needs `CAP_DAC_READ_SEARCH`, and **this design never
  calls it**: a handle is a value to compare, never a door to open.
- **Some filesystems have no handles, and that is now uneventful.**
  `name_to_handle_at` returned `EOPNOTSUPP` for `/proc`, `/sys` and a
  container's own overlayfs root; some FUSE mounts do the same. Under this
  design that removes a *refusal*, not a proof, so the pass decides on the
  assignment and the realpath exactly as it does everywhere else, records NULL
  in the handle column, and says nothing to the operator.

**The two pathname columns are `TEXT`, and that is a correctness decision.**
The rule is that **a provenance column must be able to record any value the
fact it mirrors can take**; a value the pass observed and cannot store is a
bug, never a NULL and never a truncation. The realpath is what forces it: a
short assignment may be a symlink to a canonical path of any length, and this
system owns no bound on that. The cost of getting it wrong is not a clipped
string — the discard branch writes the record *and* the delete in one
transaction, so an oversized value raises `string_data_right_truncation`, the
delete rolls back with it, every later pass repeats the failure, and the
database-backed tools keep serving the former vault forever. A column width
would have reproduced #91's own symptom. `indexed_vault_assignment` is `TEXT`
too even though `varchar(1024)` is sufficient *today*: that sufficiency is a
property of `users.vault_path`'s DDL and of the current normaliser, not of this
record, and the two pathname facts are written and read as one unit.

**And `TEXT` alone does not satisfy that rule for the realpath, which is why
that fact is stored hexadecimal-encoded.** A column is total over a fact only
when neither the *length* nor the *byte content* of an observable value can be
rejected, and `TEXT` bounds only the first. A POSIX pathname is an arbitrary
sequence of non-NUL bytes with no obligation to be valid UTF-8; Python decodes
such a component with `surrogateescape`, so `os.path.realpath` can hand back a
string carrying a lone surrogate like `'\udcff'`, which the UTF-8 driver cannot
encode and the database will not accept. The consequence is identical to the
width bug and reached through a different channel: the discard writes the
record and the delete in one transaction, the parameter fails to encode, the
delete rolls back with it, and the former vault is served forever. So
`indexed_vault_realpath` holds `os.fsencode(realpath).hex()` and comparison is
encode-then-compare on both sides — every byte value has exactly one
two-character spelling, so there is no input the column cannot take, and
`os.fsdecode(bytes.fromhex(stored))` returns the observed string exactly.
Hexadecimal rather than base64 because the handle column already spells opaque
bytes that way, because base64's variant alphabets and optional padding give
one value two spellings and this record is decided by byte equality, and
because the doubled length is the exact cost `TEXT` is here to absorb.

**`indexed_vault_assignment` needs no such encoding, and that asymmetry is
about where each fact comes from.** The assignment is `str(Path(vault_path))`:
a purely lexical normalisation, reading no directory and introducing no
non-ASCII character its input lacked, over a value the database itself
supplied — and a UTF-8 database cannot be holding bytes it would refuse to
accept back, so that fact round-trips by construction. The realpath is
kernel-derived and constrained by nothing. The one environment-derived pathname
in the system, `settings.vault_path`, never reaches this column: classification
is skipped for `user_id is None`, and an assigned user's root is read from
`users.vault_path`. Encoding it too would buy no totality it already has, and
would make the fact an operator actually reads in a discard log unreadable —
the realpath is decoded for the log, never for the comparison.
`indexed_vault_handle` stays `varchar(320)` with its NULL-on-oversize rule
because it is a different kind of value — a comparison token with a documented
external maximum, whose absence is a *defined* state (no hardening signal),
where a missing pathname is not a state at all but a half-set record.

**Every stamp writes all three columns, and a fact the pass could not observe
is written NULL.** There is no partial stamp: no branch updates one column and
leaves another describing a root it does not describe. That single rule is what
makes a later observation safe to compare — it can never be measured against a
root the stamp did not cover — and it is the whole of round 3's transition
finding, which arose because that round had a branch that re-derived and
deliberately stamped nothing while a previous root's record stood.

So the pass reaches one of six verdicts, and the classification is total over
every combination of inputs:

| observed vs. recorded | verdict | what the pass does |
| --- | --- | --- |
| the assigned root cannot be opened as a directory, or its realpath no longer names the pinned inode | **indeterminate** | nothing at all: no delete, no stamp; the pass fails as it does today |
| no provenance present — all three columns null, or a half-set record in which the assignment or the realpath is null | **provenance unknown** | **re-derive**, then stamp at the end of the pass *if the pass was complete* |
| assignment equal **and** realpath equal, with no observable handle mismatch | **same assignment** | nothing |
| assignment equal **and** realpath equal, but a handle was recorded, a handle was read now, and they differ | **provenance unresolved** | **re-derive**, then stamp *if the pass was complete* |
| assignment differs **and** realpath differs | **reassigned** | **discard**: delete the user's `notes_metadata` (embeddings and links cascade) and stamp, in one committed transaction, before any file under the new root is read |
| exactly one of assignment and realpath differs | **provenance unresolved** | **re-derive**, then stamp *if the pass was complete* |

A record counts as **present** only when both the assignment string and the
realpath are non-null. Both are always observable for a root the pass could
pin, so a half-set record is drift rather than a state this code writes — and
the safe reading of drift is that nothing is known, not that the half that is
set may be trusted.

Every input lands somewhere, and the ones an operator can actually cause land
where they should:

- **The transition #91 is about** — `/old` → unassigned → `/new`, two Saves,
  the second of which the panel cannot distinguish from a restore. On the first
  pass after `/new` is saved the assignment differs and the realpath differs:
  **discard**, and the stale vault stops being served.
- **Reassignment to an alias of the same directory** — assignment differs,
  realpath equal: **re-derive**. No vault is re-embedded because an operator
  spelled the root differently.
- **A cosmetic re-spelling** — a trailing separator, a doubled separator, a `.`
  component. `str(Path(...))` normalises all of them away, so the assignment
  compares equal and the realpath compares equal: **keep**.
- **A reboot, a container recreate, a remount** — nothing moves, including the
  handle (on ext4/xfs both the inode number and its generation counter live on
  disk): **keep**, with no re-embed. This is the case that makes the whole
  design affordable, and it is the one round 2's `st_dev` scheme threatened.
- **A directory deleted and re-created at the same path** — assignment equal,
  realpath equal, and the handle differs where handles are available:
  **re-derive**, cheaply. Where handles are unavailable: **keep**, and the
  ordinary scan reconciles by path and hash. That second outcome is inside the
  declared non-goal below.
- **A vault restored in place from a backup** — same shape as above: with
  handles, a cheap re-derive; without, a keep whose ordinary scan re-hashes
  every file anyway. Never a discard, so a restore never costs a re-embed.
- **A retargeted symlink under an unchanged assignment** — assignment equal,
  realpath differs: **re-derive**. Detected when the retarget persists to the
  next pass; not guaranteed, because the retarget can be reverted before that
  pass runs. Declared, not claimed.
- **A cloned filesystem image mounted at the same pathname under the same
  assignment** — every recorded fact matches, including the handle: **keep**.
  This is round 3's blocker and it is now a declared non-goal rather than an
  absorbing hardening loop.

**Which error the design prefers, said plainly.** CLAUDE.md ranks silently
wrong search results above expensive ones, so ambiguity never resolves toward
*keeping*: only an exact match of both recorded facts keeps, and even that is
demoted by a handle that disagrees. Ambiguity also never resolves toward the
*destructive* branch: a discard costs a full re-embed, so it fires only when
both facts agree that the assignment moved. Everything between goes to a branch
that asserts nothing and destroys nothing. The indeterminate row does nothing at
all, and for a reason: an index cannot be re-derived from a directory that
cannot be read, and destroying one because a bind mount was briefly unavailable
buys nothing and costs the full re-embed.

#### Non-goal: filesystem substitution behind an unchanged assignment

**The system does not detect, and does not claim to detect, a change of
*storage* underneath an unchanged vault assignment.** Retargeting a symlink the
assignment names, remounting a different filesystem at the same pathname,
restoring a cloned image over the vault, replacing the directory with a copy —
all of these are operator actions on storage. Where the handle hardening
happens to catch one, the outcome is a cheap re-derive; where it does not, the
index is kept and reconciled by the ordinary scan. Neither is promised.

This is a boundary, stated so the next reviewer reads a decision rather than an
oversight, and there are three reasons it is the right one:

- **It is unwinnable by construction.** A bit-identical clone of an ext4 or xfs
  image presents the same inode numbers, the same generation counters and
  therefore the same file handles, at the same pathname, under the same
  assignment. No fact a userspace process can read separates it from the
  original. A design claiming otherwise is a heuristic with a confident name —
  and three rounds of review demonstrated exactly that, each substitution
  defeated by the next.
- **It is the same trust class as editing the database directly.** Anyone who
  can remount the vault or restore an image over it can also `UPDATE users SET
  indexed_vault_assignment = …`. No in-process check survives an adversary at
  that level, and the system does not pretend to hold one anywhere else either.
  **Today's system, which records nothing at all, is equally blind to every one
  of these**; this change does not regress that and does not close it.
- **Most of it heals anyway, and the part that does not is a pre-existing
  defect.** A keep is not a no-op: the ordinary scan reconciles every note by
  relative path and content hash, prunes rows whose path is absent under the
  root, and re-parses and re-embeds every note whose bytes differ. A substituted
  root therefore converges on a correct index for everything except one case,
  described next.

**One interleaving inside the boundary, named rather than left to be found.** A
re-derive triggered by a real-path disagreement can be *incomplete*, in which
case it stamps nothing; if the substitution is reverted before the next pass,
that pass sees both recorded facts agree and keeps — over rows a previous pass
partly re-derived from the substitute. Same non-goal, different route, same
bound: the ordinary scan reconciles those rows by relative path and content
hash, leaving only the case below. Closing it would mean stamping an
*incomplete* re-derive, which is the round-2 blocker this design refuses on
purpose.

**The one case that does not heal, precisely — and it needs no reassignment.**
A note whose relative path *and* content hash are unchanged is classified "no
change" and `continue`d (`indexer.py:158-159`), so its links are never
re-extracted. If a note it linked to is pruned, `note_links` keeps the row and
loses its resolution — `target_note_id` is `ON DELETE SET NULL` — and nothing
will ever re-resolve it, because the source's hash will keep matching. **This is
reachable today on a single vault with no reassignment at all**: delete
`OnlyA.md`, let one pass prune it (the link from an unedited `Same.md` goes to
NULL), then re-create `OnlyA.md` at the same path. The next pass inserts a
*new* `notes_metadata` row for it, `Same.md` is still unchanged, and its link
row keeps a NULL target for as long as `Same.md` is not edited —
`get_backlinks("OnlyA.md")` misses it permanently. The fix belongs to the
link-resolution path (re-resolving dangling links whenever the set of notes
changes), not to a provenance column, and it is **not filed** as an issue
today. It is named here so the next reviewer who reaches it through a
substitution scenario recognises where it actually lives.

#### Where every prior round's attack lands under this design

Five rounds of review produced fourteen findings. Each one has a home here, and
the point of this table is that the next reviewer can check the *dispositions*
rather than re-derive the attacks. "Out of scope" appears three times and each
time it names the non-goal above, which is a declared boundary with an argument,
not a shrug. Rounds 1–3 attacked the identity claim; rounds 4 and 5 accepted the
rescope and attacked the new design's own mechanics, which is why their findings
are about a column width, a gate and a value domain rather than about what the
record means. Round 5's single finding is the sharpest of that kind: it did not
dispute round 4's rule, it showed that round 4's *implementation* of the rule —
a wider type — enforced only half of it.

| # | Round | The attack | Where it lands now |
| --- | --- | --- | --- |
| 1 | R1 BLOCKER | 016 backfills `indexed_vault_path = vault_path`, so rows built under A are stamped as B and the never-heals link case becomes guaranteed | **Fixed and unchanged.** 016 backfills nothing; NULL is the unknown branch and re-derives |
| 2 | R1 BLOCKER | A lexical pathname comparison is not durable identity — a retargeted symlink keeps a foreign index, and two aliases destroy a good one | **Re-answered by the rescope.** The lexical string is deliberately *the* fact, because the question is the assignment; the recorded realpath is the second fact that stops the alias case from discarding (→ **re-derive**), and a persisting retarget makes exactly one fact disagree (→ **re-derive**). A retarget reverted before the next pass is **out of scope** |
| 3 | R1 MAJOR | `move_note` reuses one confirmation across a database await and every link rewrite | **Fixed in group C and untouched by the rescope.** One confirmation per publishing operation |
| 4 | R1 MAJOR | `delete_note(permanent=True)` reaches a bare `os.unlink`, outside any refusing helper | **Fixed in group C and untouched.** A `MutableTarget`-based permanent-unlink helper on the same seam |
| 5 | R1 MINOR | 017 omits 015's orphan-label invariant, so a stamp-back re-run rewrites a recorded attribution | **Fixed in group B and untouched.** B.5a ports `_assert_no_orphan_labels` |
| 6 | R2 BLOCKER | `realpath + st_dev:st_ino` is not identity — a reused inode makes both signals agree about two different directories | **Dissolved.** Device and inode numbers are recorded nowhere and compared across time nowhere. The case itself — a directory replaced at the same path under an unchanged assignment — is **keep**, demoted to **re-derive** by the handle where one is available, and **out of scope** where one is not |
| 7 | R2 BLOCKER | Identity read from a pathname, then the pathname scanned: an ABA retarget leaves B's rows recorded as A | **Fixed and kept.** The root is pinned once and the facts, the discovery and the reads all come from that descriptor. The rationale is reframed: it makes one pass internally consistent, which is achievable, rather than proving identity across time, which is not |
| 8 | R2 BLOCKER | The scan `continue`s past unreadable files, so a re-derive can complete over a foreign row and still stamp | **Fixed and kept verbatim.** Any per-file skip makes the re-derive incomplete and withholds the stamp; the link rebuild reads the scan's buffer |
| 9 | R3 BLOCKER | A cloned ext4/xfs image at the same pathname presents the same handle, so the keep branch keeps the wrong clone's index | **Out of scope, declared.** Also checked by hand: the stated failing input converges on the same end state a fresh index of the clone reaches, and the variant that does not is the pre-existing incremental-indexer defect named in the non-goal |
| 10 | R3 BLOCKER | `embed_vault` / `link_backfill_pass` / `rebuild_tsvectors` have no defined behaviour under unresolved provenance, so a link row from one root can land against a row from another | **Fixed, and narrowed in round 5.** `link_backfill_pass` and `rebuild_tsvectors` run for a user only on the *same assignment* verdict; otherwise each skips **that user**, logs once, and waits for the next scan. `embed_vault` is not gated — see finding 13 — because it hash-verifies every note it certifies and so cannot write a vector against a row it does not describe |
| 11 | R3 MAJOR | The no-handle branch stamps nothing, so a previous root's record stands and a later handle-capable pass charges a destructive discard against freshly correct rows | **Dissolved twice over.** There is no no-handle branch any more — an absent handle removes a refusal, not a proof — and every stamp writes all three columns, NULL for anything unobserved, so no record can outlive the root it describes |
| 12 | R4 BLOCKER | `varchar(1024)` cannot hold every realpath a valid assignment can name, so the discard-and-stamp transaction rolls back on `string_data_right_truncation` and the former vault's index is served forever | **Fixed, and completed in round 5.** Both pathname columns are `TEXT`, under the stated rule that a provenance column must be able to record any value the fact it mirrors can take; never truncated, never NULL'd. The handle keeps its width because an absent handle is a defined state and an absent pathname is not. `TEXT` turned out to bound only the *length* — row 14 is the same rule applied to the *byte content* |
| 13 | R4 MAJOR | Gating `embed_vault` on settled provenance makes one permanently unreadable file freeze a *readable* note's embeddings indefinitely, and `semantic_search` has no `embedded_content_hash = content_hash` guard | **Fixed.** `embed_vault` is un-gated and runs under every classification, protected by the verify-then-embed rule it already carried; the gate keeps `link_backfill_pass` and `rebuild_tsvectors`, whose work the re-derive redoes on every pass anyway. The verification and the un-gating are specified as one requirement so neither can be removed alone |
| 14 | R5 BLOCKER | `TEXT` removes the width bound but not the *encoding* bound: `os.path.realpath` can return a surrogate-escaped string for a non-UTF-8 pathname component, which the driver cannot encode, so the discard-and-stamp transaction still rolls back forever | **Fixed.** `indexed_vault_realpath` stores `os.fsencode(realpath).hex()` and is compared encode-then-compare, so the column is total over the fact by construction rather than by a bound. The assignment column is untouched: its value comes from `users.vault_path` through a lexical normaliser, and a UTF-8 database cannot hold bytes it would refuse to accept back |

Two of the fourteen — rows 2 (in part) and 9 — are answered by declaring a boundary rather than by code.
That is the whole substance of round 4, and it is worth being explicit about the
trade: **the system detects every operator reassignment, which is what #91
asked for, and detects storage substitution only by luck.** The previous three
rounds each bought a little more luck at the cost of a claim that the next round
falsified.

**The root is pinned once per pass, and everything that pass does runs beneath
that descriptor — for internal consistency within the pass, not as a proof of
identity across time.** Round 2 shipped the check-then-act version: derive the
identity from a pathname, then scan that pathname. The reviewer's interleaving
is `/vault/current` pointing at A while the identity is read, retargeted to B
before `discover_markdown_files`, and retargeted back to A before the next
pass — leaving B's rows recorded as A's. The fix is #59's, taken wholesale
rather than reinvented: **open the assigned root once, derive the observed
facts from that descriptor, and perform discovery and every file read beneath
it.** `os.open(vault, O_RDONLY | O_DIRECTORY)` pins an inode; `os.fstat` and
`name_to_handle_at(fd, "", AT_EMPTY_PATH)` describe the thing pinned rather
than the thing named; `os.scandir(fd)` and `os.open(name, dir_fd=parent)` walk
downward from it.

Be precise about what the pin buys, because round 3 over-claimed it. It does
**not** prove that the pinned directory is the one the rows came from; nothing
proves that. It proves something narrower and entirely achievable: **within one
pass, the facts observed, the files discovered and the bytes read all come from
one inode**, so the pass cannot record a provenance describing a directory it
did not scan. That is why it survives the rescope unchanged.

Two properties of that walk are deliberate, because anchoring must not quietly
change *what the index contains*:

- **The symlink policy is unchanged.** `Path.rglob` does not descend directory
  symlinks today, and the descriptor walk descends with
  `O_DIRECTORY | O_NOFOLLOW` — the same rule, now enforced by the kernel per
  descent rather than by a library's traversal habit. A symlinked *file* at a
  discovered path is read as it is today. Anchoring is about *which directory
  is scanned*, not about containment, and this change makes no containment
  claim it did not already make: a symlinked leaf can still point outside the
  root, exactly as before, and `open_mutable` remains the guard that matters
  for writes.
- **Stat and read describe one inode.** The scan currently calls
  `full_path.read_text()` and then `full_path.stat()` — two independent
  pathname resolutions, so `file_size` and `modified_at` can describe a
  different file from the one whose bytes were hashed. Under the anchored form
  the file is opened once through the parent descriptor and `os.fstat`ed on
  that descriptor. Not the reason for the change; a free consequence of it,
  and worth having.

The walk holds one descriptor per level of depth, not per file — depth-first,
each parent closed once its children are done — so it costs the process a
handful of descriptors, not thousands. It is a *read* walk and it stays inside
`src/services/indexer.py`; see the sequencing note in `tasks.md` for why it is
deliberately not a `vault_fs` helper.

**`link_backfill_pass` and `rebuild_tsvectors` do nothing for a user whose
provenance is not settled.** `index_vault` is the pass that classifies and
stamps, but both of these also read `vault / file_path` and write rows the
provenance is a claim about — `note_links` and `content_tsvector` — with **no
verification of any kind** that the bytes they read belong to the row they
write against. A user whose notes contain no links leaves `link_backfill_pass`
eligible on *every* startup, so "the scan settled this a moment ago" is not
something either may assume. **Each therefore runs for a user only when that
user's provenance is recorded and the classification for the assigned root
right now is *same assignment* — the keep verdict, computed by the one function
that computes it. Otherwise it skips that user, logs once, and leaves the work
to a later pass**, after `index_vault` has settled the provenance. The skip is
**per user**: an unsettled user does not stop the pass for everybody else. That
is round 3's second blocker — a link row extracted from one root landing
against a metadata row from another — and this is its fix.

Verification is not merely unimplemented in those two. A link row's
*resolution* is a function of the whole set of notes under a root (`vault_index`
is built from the metadata rows, not from one file), so no per-file check could
license `link_backfill_pass`. `rebuild_tsvectors` could in principle be verified
the way `embed_vault` is, and is still gated, because nothing records what a
tsvector was built from — there is no keyword analogue of
`embedded_content_hash`, so a vector built from foreign bytes leaves no evidence
a later pass could act on.

**And skipping costs those two nothing, even for a user whose provenance never
settles.** The re-derive branch does both of their jobs itself, on every pass:
it deletes and re-extracts *every* one of that user's link rows, and — because
it treats every note as changed — it rewrites *every* note's `content_tsvector`
from the scan's own buffer. So the gated work is a backfill of a table the
re-derive is filling anyway and a rebuild of vectors the re-derive is rewriting
anyway. Maintenance that does nothing is safe, which is what makes skipping the
right fix here rather than a second verification path in two more places.

**`embed_vault` is deliberately not gated, and that is a correction of the
round-4 draft, which gated all three.** The gate composed with the completeness
rule into indefinite staleness. A permanently unreadable file withholds the
stamp forever — by design — and the gate turned that withheld stamp into a
permanent refusal to embed anything for that user, while the scan kept working:
a *readable* note the user edits gets a fresh `content_hash` every pass, its
`note_embeddings` still hold the chunk text of the content it used to have, and
`semantic_search` reads `chunk_text` with **no** `embedded_content_hash =
content_hash` guard (`embeddings.py:353`). One bad file would have made that
user's semantic search silently wrong, indefinitely, for an agent consumer that
acts on the result without a human seeing the query — the failure CLAUDE.md
ranks above every expensive one. The re-derive branch does *not* do
`embed_vault`'s job the way it does the other two, precisely because it keeps
`note_embeddings` and makes no embedding call; so this was the one gate whose
cost was unbounded.

**And the un-gating is sound only because the embedding pass verifies the hash
it is about to certify — the two are one decision.** `embed_note` sets
`note.embedded_content_hash = note.content_hash` — the *metadata* row's hash,
not a hash of the bytes it just embedded. So a file that differs from its row at
embedding time is embedded and then marked as embedded *for the row's hash*, and
nothing will ever re-embed it. `embed_vault` therefore re-hashes what it read
and skips the note when it does not match the row it selected; the next pass,
which will have refreshed the row, picks it up. One sha256 over bytes already in
memory. That check does two jobs. It is what makes the re-derive's retention of
`note_embeddings` load-bearing rather than merely plausible — the branch keeps a
vector *because* a matching `content_hash` proves it is that file's vector. And
it is what makes running ungated safe: the gate existed to stop a row derived
from one root being written against a row derived from another, and an embedding
is a pure function of content, so refusing to embed any bytes that do not hash to
the selected row's `content_hash` means the vector and the row describe the same
content *whatever directory supplied the bytes*. Under a wrong root the hashes
disagree and the pass skips; under bytes that match the row the vector is correct
by construction. Removing the verification would therefore require re-gating
`embed_vault` in the same change, and the spec says so at both sites so the
consequence is visible wherever someone touches one of them.

**The re-derive, precisely — and why it is not a compromise.** For an unresolved
provenance the pass runs over the assigned root with **content-hash change
detection disabled**: every discovered file is parsed and upserted regardless of
its hash, the ordinary prune removes every row whose relative path is not
present under that root, and because every note counts as changed,
`_update_links_for_changed` deletes and re-extracts **every** one of that user's
link rows and re-resolves them against an index built from those notes alone. So
after the pass, every surviving row and every link row was written by that pass
from a file under the assigned root. That is a structural claim about who wrote
the rows, not an enumeration of columns that has to be re-audited whenever a
column is added.

**A skip falsifies that claim, so a skip withholds the stamp — round 2's third
blocker, and it survives the rescope unchanged.** The claim above is only true
if the pass actually visited every file. The scan does not: it `continue`s past
a `UnicodeDecodeError` (`indexer.py:150`) and past any other read failure
(`:153`), the tsvector loop `continue`s for a path it has no buffered body for
(`:311`), and `_update_links_for_changed` re-reads each changed note from disk
and `continue`s on `UnicodeDecodeError | FileNotFoundError | OSError` (`:403`).
Each of those leaves the *ordinary prune* as the only thing acting on that path
— and the prune keeps a row whose relative path exists under the new root, which
is exactly the case a re-derive exists to repair. Vault A supplied `Same.md`;
vault B holds a different `Same.md` with invalid UTF-8; the path is discovered,
the read raises, the row and its links survive untouched, and round 2's design
then tail-stamps B over them. A pass can complete "successfully" and leave a
foreign row certified.

**The rule is the simplest one that keeps the claim true: any per-file
discovery, read, stat, parse or link-extraction skip makes the re-derive
incomplete, and an incomplete re-derive is not stamped.** The pass still does
all the work it can — an unreadable file does not abort the pass, and every
readable file is still repaired — but it logs which paths kept it unstamped
(the first twenty and a count, the same offender-report shape 013 and 015 use)
and the next pass re-derives again.

Three reasons this is the right rule rather than the conservative one.
**It fails toward re-work, never toward wrongness.** The alternative shape the
reviewer offered — transactionally delete the stale rows for a skipped path, as
a fresh index would — is also correct, and it is strictly more machinery: a
second deletion path for index contents, which is the thing #64 and this
document's own panel-purge argument both refuse, and it destroys a row that may
well be the *right* row (the file was merely unreadable this second). **It keeps
provenance honest.** A permanently undecodable file leaves that user in
re-derive mode indefinitely, and that is the cost, stated plainly: the pass
never asserts a provenance it could not establish. The alternative is stamping a
claim the pass cannot prove, which is the whole defect being fixed — a foreign
row silently laundered into a certified one. **And the cost is bounded and
visible.** Re-derive mode is parse-and-upsert plus a link rebuild with zero
embedding calls, on a vault an ordinary pass already reads in full; the log
names the offending paths on every pass, so an operator who is paying for it is
told which file to fix, by name, rather than left to notice a CPU bill.

**And the link rebuild reads no file at all.** `_update_links_for_changed`
currently re-reads each changed note from disk, which is both a second read of
bytes the scan already parsed and a second window in which the file can vanish —
a disappearance between the scan and the rebuild silently drops that note's
links while the scan's row stands. The scan already buffers each changed note's
parsed body in `path_to_content` for the tsvector loop, for exactly this reason
(issue #18); the link rebuild takes the same buffer. That removes the window
rather than classifying it, and a changed path absent from the buffer becomes a
skip like any other — incomplete, unstamped, retried.

`note_embeddings` are **not** deleted, and that is where the cost goes. An
embedding is a pure function of chunk text and `notes_metadata.content_hash`
proves content equality, so a vector attached to a row whose hash still matches
the file under the assigned root is *provably* the right vector for that file.
`embed_vault` already selects on `embedded_content_hash != content_hash`, so it
re-embeds exactly the notes whose content differs and nothing else.

The marginal cost over an ordinary pass is consequently small: an ordinary pass
already reads and hashes **100%** of the vault's files, so the re-derive adds
only the parse-and-upsert of the notes an ordinary pass would have `continue`d
past, plus one link re-extraction — and **zero embedding calls** for unchanged
content. Against that, a discard on this vault means re-embedding ~16.7k chunks
serially through Ollama bge-m3; CLAUDE.md's own figures (a 14 s cold provider
reload, a ≈0.47 s warm `semantic_search` that includes one embed) put a single
chunk in the hundreds of milliseconds, so 16.7k of them is **tens of minutes per
assigned user**, during which `semantic_search` answers from a shrinking
fraction of the vault. Charging that on every upgrade, to every assigned user,
is a real availability event; charging it on a deliberate reassignment the
operator just performed is not.

**Why 016 backfills nothing.** `indexed_vault_assignment = vault_path` looks
free and is not: "assigned now" is not "indexed under what is assigned now",
and the reassignment lag it ignores is the exact defect this change exists to
close. An admin who reassigns and deploys before the next pass gets rows from
vault A stamped as B; the next pass then sees both facts equal, takes the keep
branch, and the identical-path/identical-hash link case — the one that never
heals — is *guaranteed* suppressed rather than merely possible. NULL is the only
value that asserts nothing, and under the classification above NULL is not
"stamp and move on": it is the unknown branch, so every legacy user is repaired
once, cheaply, on the first pass after the upgrade, and stamped only then.

This **deletes a hole rather than costing one**. The earlier draft carried a
"one-time backfill hole" for accounts already unassigned at migration time,
which got exactly one reassignment without reconciliation. Those accounts are
now NULL like everybody else and get the same repair. There is no special case
left to document.

**The stamp is written where the state it describes is established, which means
two different places.** On the discard branch the state is "this user has no
index rows", which is true the instant that transaction commits, so the stamp
goes with it at the head of the pass — and a pass that then fails while scanning
retries cleanly, because the next one finds both facts equal and simply indexes.
On the re-derive branch the state is "every row was derived from this root",
which is not true until the pass finishes, so the stamp is committed **after**
the pass's last write and only if the pass raised nothing **and skipped
nothing**. A crash mid-repair leaves the previous record untouched and the next
pass repairs again — bounded, idempotent, and never a stamp over a
half-repaired index. Head-stamping a re-derive would be exactly the false
provenance this section is about, written by our own code instead of by the
migration.

**Deploy ordering, and what actually holds.** `make deploy` runs
`docker compose run --rm obsidian-mcp alembic upgrade head` in a one-off
container **while the old container is still running**, and only then
`docker compose up -d --force-recreate`. So an old-code index pass can be
mid-flight when 016 commits and can go on committing `notes_metadata` rows from
the old root afterwards. **Nothing serialises the two.** `index_pass_lock` is an
in-process `asyncio.Lock`; there is no advisory lock, no row lock and no
cross-container coordination, and claiming otherwise is exactly the kind of
thing this document exists to not do.

What makes it safe is not a lock but the absence of a backfill: **016 writes no
provenance, so an old pass's writes have nothing to contradict.** Old code
cannot write any of the three columns — they are not on its models and no code
path sets them — so every row is NULL when the new container starts, whatever
the old pass wrote, and the new container's first pass per user takes the
unknown branch and re-derives from the assigned root. Overlap between the two
indexer loops is prevented by `docker compose up -d --force-recreate` being
stop-then-start for one service, not by anything in the code. The residual is
therefore a property of the deploy command: **a deploy that runs two indexing
containers of this service concurrently — a second replica, a rolling deploy, a
manually started container — can let an old pass commit rows from the old root
after a new pass has tail-stamped the new one.** `make deploy` does not do that;
an operator who changes it must quiesce the old container before migrating.

**Accepted residual.** The reconciliation runs in the indexer, so a
reassignment is honoured at the *next* pass, not at the Save. The window is
bounded by `INDEX_INTERVAL_SECONDS` (default 5 minutes) plus the duration of a
pass already in flight, and during it the metadata-only tools still answer from
the previous root. Closing it entirely means either purging inside the panel's
POST transaction — a second writer of index contents, for a five-minute
improvement — or refusing every tool for the whole interval, which breaks the
disk-backed tools that are already correct against the new root. Neither is
worth it. Same optimistic level as `edit_note(expected=…)` and the transfer
fingerprint check, and the same "takes effect at the next authenticated
request" shape as an OAuth revocation. Declared, not discovered. And note that
the *re-derive* branch does not narrow that window even to "nothing served": its
rows are replaced as the pass proceeds rather than deleted up front. That is the
price of not asserting a provenance nobody recorded, and it is paid only by the
one-time legacy population and by genuinely ambiguous provenance.

**Rejected: the file handle as *proof* of directory identity — round 3's
design.** Round 3 made a keep require a kernel file handle equal byte for byte
to the recorded one, on the strength of the inode-reuse measurement quoted
above. The measurement is real and the handle is still used here — as
hardening. It was rejected as *proof* because **a handle is unique only within
its filesystem, and filesystems can be made identical**: clone an ext4 or xfs
image and mount it at the same pathname, and the clone's realpath, inode,
generation counter and handle bytes all match the original's, so the keep
branch would keep an index of the wrong image on what the design called proof.
Scoping the handle to a filesystem does not rescue it — a mount id is not
stable across a remount (measured: 6307 versus 31 for one directory), a
filesystem UUID is a property of the image and is cloned with it, and a
non-reused mount-instance epoch degrades to re-derive after every reboot, which
is the cost the whole design exists to avoid. Worse, the escalation had no
natural end: each round's substitution was defeated by the next, which is the
"heuristic pretending to be a rule" signal rather than a reviewer being
thorough. The handle keeps the job it can actually do — it can *disprove*
sameness, so it refuses a keep — and is never asked to establish one.

**Rejected: re-derive on every pass where no handle is available — round 3's
degraded branch.** With the handle demoted to hardening there is nothing left to
degrade: an absent handle removes a *refusal*, not a proof, so the pass decides
on the assignment and the realpath exactly as it does everywhere else. Round 3
had to re-derive forever on such a root, warn once per process per root, and
name that condition in every re-derive line. All of that machinery goes — and
with it the transition defect it created, where a pass that deliberately
stamped nothing left a previous root's record standing and a later
handle-capable observation compared freshly re-derived rows against it and
charged a destructive discard.

**Rejected: record device and inode numbers.** They are recorded nowhere and
compared across time nowhere. Inode numbers are reusable — the measurement
above reused one on the first try — so a pair that agrees proves nothing that
the assignment string does not already prove better. `st_dev`/`st_ino` survive
in exactly one role, and it is not a match signal: at observation time the pass
checks that `os.path.realpath(assigned)` still names the inode it pinned, which
is #59's own `_require_same_directory` idiom (the pathname and the descriptor
must describe one directory *right now*, because the recorded realpath is
computed from the name). A disagreement there means the name is moving under
the pass, and it routes to **indeterminate** — assert nothing, destroy nothing.
Never to a keep.

**Rejected: a UUID persisted inside the vault.** It is the obvious durable
identity and it is wrong here for a reason that has nothing to do with
correctness: it writes the server's bookkeeping into the user's own data. The
vault is Max's single source of truth, synced by Obsidian across machines; a
`.obsidian-mcp-root-id` file would be copied by every duplication of the vault
(so two copies claim one identity — the exact failure mode a UUID is supposed
to prevent), deleted by anyone tidying dotfiles, and would make a *read-only*
vault mount un-identifiable. It also inverts the trust direction: the identity
of a directory would be whatever a writer of that directory says it is.

**Rejected: age-based pruning.** Dropping index rows untouched for N days
invents a retention policy nobody asked for, deletes exactly the rows #66
preserves on purpose, and costs the full re-embed #66 exists to avoid when it
is wrong. Reassignment to a different root is a real event with a real trigger;
"this index is old" is not an event, and an unassigned account later restored
to its own directory is the *normal* case.

**Rejected: backfill `indexed_vault_assignment = vault_path`.** The failing
input is decisive and is carried into the spec as a scenario: vault A holds
`Same.md` linking to `OnlyA.md`; vault B holds a byte-identical `Same.md` and no
`OnlyA.md`; the user is indexed on A, reassigned to B, and the deploy runs
before the next pass. The backfill stamps B over rows built from A, both facts
then agree, and `Same.md`'s link is dangling forever — the never-heals case
turned from *possible* into *guaranteed* by the migration meant to prevent it.

**Rejected: force a full discard for every assigned legacy row.** Correct, and
the most expensive correct thing available. It charges tens of minutes of serial
embedding per assigned user on the upgrade, for a population in which the
genuinely drifted accounts — reassigned but not yet reindexed at migration time
— are a small minority, and it destroys vectors whose validity a content hash
already proves. The re-derive is correct by the same structural argument and
costs no embedding call for unchanged content.

**Rejected: quiesce and discard inside the migration.** It makes 016 a second
deleter of index contents, which is the rule this section already invokes
against a panel-side purge and which #64 argued for grant families; it cannot in
fact quiesce a container it does not control, since `make deploy` migrates from
a *separate* one-off container while the old app is still up; and it pays the
same full re-embed as the option above.

**Rejected: delete the skipped path's rows instead of withholding the record.**
The reviewer offered this as the other way to keep the structural claim true,
and it is correct — a fresh index would remove those rows. It is rejected for
two reasons and neither is effort. It makes the reconciliation a *second*
deletion path for index contents, keyed on a transient read failure, which is
the shape this document already refuses for a panel-side purge and which #64
argued against for grant families. And its failure direction is wrong: a file
that could not be decoded *this second* is very often the right file for the row
being deleted, so the rule would destroy valid rows — and, with them,
embeddings — in exchange for a stamp nobody is waiting for. Withholding the
stamp costs a repeat of a pass that makes no embedding calls.

**Rejected: treat an unreadable file as a fatal pass error.** Simpler to state,
and it converts one bad file into a total outage of index maintenance for that
user: no new note is indexed, no deletion is pruned, and nothing is embedded
until a human intervenes. The chosen rule does every repair it can and declines
only to *certify*.

**Rejected: let `link_backfill_pass` and `rebuild_tsvectors` write under an
unresolved provenance, guarded by a per-file content check.** Round 3's reviewer
offered this as the alternative to skipping: have both verify each file's hash
against the metadata row before writing, the way `embed_vault` does. For
`link_backfill_pass` it does not even work — link *resolution* is a function of
the whole note set, not of one file's bytes. For `rebuild_tsvectors` it would
work and is still refused: it is a second verification path, and unlike the
embedding case it licenses work that can simply wait, because the re-derive
rewrites every tsvector on every pass anyway. Maintenance that does nothing is
safe; the simple rule is to do nothing until the scan has settled the
provenance. `embed_vault` is the exception for the opposite reason — its work
*cannot* wait, and it already carries the verification.

**Rejected: infer provenance by overlapping the recorded relative paths with the
files found under the assigned root.** A threshold on a heuristic — high overlap
"means" the same root — and CLAUDE.md is explicit that a heuristic pretending to
be a rule should ship behind a flag or in shadow mode rather than absorb another
round of threshold tuning. Its failure direction is also the wrong one: two
vaults that share a directory layout produce a high overlap and a silent keep.

### 2. Transfer-route usage rows lose attribution when the credential is deleted (#92, item 2)

#77 made every MCP tool call carry a denormalised actor — `actor_kind`,
`actor_label`, `actor_ref` on `usage_logs`, bound by `APIKeyMiddleware` into
`current_actor` and written by `_log_usage` — because both credential FKs are
allowed to lose their target while the log row stays, and both do so on the
operator's most urgent path.

**The transfer routes were not covered, and CLAUDE.md records the gap as
known.** `src/transfer/routes.py::_log_row` builds its own `UsageLog` from the
*minting* identity carried on the `transfer_tokens` row: `key_id`,
`oauth_token_id`, `user_id`, and nothing else. There is no request-scoped actor
to read — the redemption request is session-less and authenticates with a
capability, not a credential — so those rows are attributed by LEFT JOIN
exactly as every row was before #77. Delete the OAuth client and every
`upload_file` / `download_file` line it produced renders "unknown"; NULL a
key's `usage_logs.key_id` before deleting it and the same thing happens. These
are the rows an operator reviewing a suspect connector most wants: the ones
where bytes entered or left the vault.

**Decision: record the actor on `transfer_tokens` at mint, copy it into the
usage row at redemption.** Migration 017 adds `actor_kind` / `actor_label` /
`actor_ref` to `transfer_tokens`, mirroring 015's types exactly
(`String(20)` / `String(255)` / `String(64)`, nullable, no server default), and
`_log_row` copies the three fields onto the `UsageLog` it builds.

In the #77 register, and for the #77 reasons:

- **Bound at mint from the credential the request already loaded.** The minting
  call is an ordinary authenticated MCP tool call — `request_upload` /
  `request_download` run under `_tracked`, inside a request `APIKeyMiddleware`
  has already resolved — so `current_actor` is *already set* and already holds
  the OAuth `client_name` that `_load_credential` alone would not (the OAuth
  branch's token lookup `outerjoin`s `oauth_clients` for exactly this). `mint_token`
  reads that ContextVar itself rather than taking a parameter, through the
  **same single reader** `_log_usage` uses, so the two cannot drift in shape or
  truncation and **no path gains a query**. This is the `plan_mint_window`
  discipline applied to a second field: the mint reads what it needs in its own
  transaction rather than trusting a caller-supplied value.
- **A snapshot, never re-derived.** The label is what the credential was called
  when the capability was minted. Re-reading it at redemption would rewrite
  history on every rename, and would fail entirely in the case the scheme
  exists for — the credential deleted.
- **One owned unit with a COMMENT marker.** 017 stamps each column with
  `denormalised actor, recorded at mint (017_transfer_token_actor)`, completes
  only a set that is all present, exactly typed, nullable, default-free **and
  marked**, and refuses anything else — a partial set, a `NOT NULL` column, a
  foreign one — naming what it found. `downgrade()` drops only marked columns,
  all-or-nothing. The same string is declared on the model
  (`TransferToken._ACTOR_COLUMN_MARKER`) so `alembic check` compares it. Type
  and width are a coincidence anyone could reproduce; the marker is the only
  evidence that *this* scheme wrote the values, which is the entire basis for
  showing them to an operator as an audit trail.
- **The backfill labels what its own FK still points at, and nothing else.**
  017 backfills `transfer_tokens` from `api_keys` and from `oauth_tokens` →
  `oauth_clients`, guarded on `actor_kind IS NULL` so a re-run cannot rewrite a
  value minting has since recorded. Worth stating precisely, because it differs
  from 015: `transfer_tokens.key_id` and `.oauth_token_id` are **`ON DELETE
  CASCADE`**, so a row whose minting credential is gone does not exist to
  label. The rows the backfill leaves NULL are therefore the ones that carry no
  credential FK at all — a single-user or sandbox mint — and they render as
  unattributed rather than as a guess.
- **A label beside a NULL `actor_kind` is drift, and 017 refuses it — 015's
  `_assert_no_orphan_labels` rule, which the first draft of this proposal
  omitted while claiming to follow 015 exactly.** The backfill's only guard is
  `actor_kind IS NULL`, so a row that already carries an `actor_label` or an
  `actor_ref` under a NULL kind would be *relabelled* from whatever credential
  its FK points at now — rewriting a recorded attribution, which is the one
  thing these columns must never do. It is reachable by a stamp-back re-run
  over a database that drift or a faulty writer has put in that state, so 017
  runs the same offender query 015 runs, before the backfill, and raises naming
  the offending ids while changing nothing. Cheap, and it is the invariant that
  makes the marker pattern safe on re-run rather than merely well-typed.
- **017 writes nothing to `usage_logs`, and that is a decision.** A transfer
  usage row written before 017 carries no link back to the token that produced
  it — there is no `transfer_token_id` on `usage_logs` and adding one to label
  history would be inventing a join that never existed. The only other
  available backfill is a re-run of 015's own credential join, which 015 owns
  and guards; two migrations writing the same three columns of the same table
  by the same rule is precisely the second resolution path #64 argued against.
  So rows in the 015→017 gap keep join-only attribution, render through the
  panel's existing pre-015 fallback, and show "unknown (credential deleted)"
  when their credential is gone. That is a bounded, closed set that only
  shrinks — the same shape as 016's one-time hole, stated rather than left to
  be found.
- **Nothing about redemption's authorisation changes.** The label is display
  and audit only and is never read for authorization, exactly as on
  `usage_logs`. `_credential_ok`, `resolve_root_ok` and the publish gate are
  untouched.

### 3. A vault reassignment is not seen by a write already in flight (#88)

Surfaced by the adversarial audit of the panel slice (PR #80) and accepted as a
documented limitation in PR #81. In multi-user mode: an MCP request
authenticates, `APIKeyMiddleware` warms the cache and binds
`current_vault_root = (user_id, /vaults/old)`; before the tool body runs an
admin commits a reassignment to `/vaults/new` and the panel reports success;
the request's snapshot still says `/vaults/old`, so its `create_note` /
`edit_note` / `write_file` lands in the *former* vault after the reassignment
was reported complete. The snapshot is deliberately immutable — that is what
makes #66's admission gate fail closed under a concurrent bulk warm — so the
staleness is a property of the design, not a bug in it. The bound is one
request's lifetime, which for a write tool includes the whole tool body.

**Decision (already taken; not re-litigated here): re-read the current root
immediately before the publish, and refuse on change. Not a `SELECT … FOR
UPDATE` gate held across the filesystem publish.** The transfer path holds row
locks across its publish because it has a token row, a bounded byte stream and
an already-open session doing nothing else; a note mutation has none of those.
Holding the credential and user rows `FOR UPDATE` across `move_note`'s link
rewrites, or across an `edit_note` on a note near `MAX_NOTE_BYTES`, would put
arbitrary vault I/O inside a lock every authenticated request contends for. The
re-read narrows the window from *one request's lifetime* to *the publish phase
of one call*, at the same optimistic level as `edit_note(expected=…)` and the
transfer fingerprint check, and says so.

**The seam.** `atomic-beneath-root-writes` — which lands before this change —
leaves the mutation path as: `open_mutable(rel, user_id)` yields a
`MutableTarget` holding a beneath-root parent descriptor, and every publish
(`_atomic_write_at`'s `linkat`/`renameat`, `move_file_no_clobber`'s
`rename_noreplace`, `soft_delete_at`) runs through that target. This change
puts the confirmation on the target: a fresh read of the assignment **stamps
every target the call is about to publish through**, and the publish helpers
**refuse a target that carries no stamp from this call**. That is what makes
inheritance structural rather than conventional — a mutating tool added later
cannot publish without one, the way a tool added later cannot skip the
admission gate.

**One confirmation per publishing operation, not one per call — the first draft
had this wrong and the correction simplifies it.** "Once per call" is fine for
the five tools that publish exactly once, and false for `move_note`. That tool's
single confirmation would have been taken before the `renameat2` and then reused
across an `async with async_session()` metadata transaction — an await of
unbounded duration — and across an arbitrary number of separate
`write_file_at` publications, one per planned link rewrite. Reusing one stamp
there is the same staleness the change exists to narrow, just relocated: an
admin who reassigns during that await sees every remaining rewrite land in the
former vault under a confirmation taken before the reassignment committed. So
the rule is the simpler one — **the confirmation is taken immediately before
each publishing operation and covers exactly that operation** — which is what
makes the residual below ("staging, flush and one publishing call") *true for
every tool* rather than true for six of seven. The extra cost is one indexed
primary-key read per rewrite, against one file read and one file write per
rewrite that the tool already performs.

**And the permanent unlink goes behind a helper, because otherwise the
structural claim is false.** `delete_note(permanent=True)` currently reaches a
bare `os.unlink(target.name, dir_fd=target.dir_fd)` — a mutation through a
`MutableTarget` that no publish helper mediates, so nothing could refuse it for
a missing stamp. It is the only bare mutating syscall left on that path. A
`MutableTarget`-based permanent-unlink helper joins `_atomic_write_at`,
`move_file_no_clobber` and `soft_delete_at` on the seam, and the direct call is
replaced by it. Without that, "the publish helpers refuse an unstamped target"
would be an accurate description of five sixths of a destructive-write surface
and a false description of the whole.

**What a mid-sequence refusal leaves, and what the caller is told.** For
`move_note(rewrite_links=True)` a per-publication confirmation can refuse
*after* the move has committed. There is nothing to roll back: the `renameat2`
happened, and `notes_metadata` and `note_links` were updated to match it, which
is correct — refusing the metadata update would leave the database describing a
note that is no longer there. So the tool **stops at the first refusal** —
every remaining rewrite would write into a vault the caller no longer has,
through descriptors pinned before the reassignment — and reports the partial
outcome explicitly: the move completed in the previous root, the assignment
changed mid-call, and these sources were left unrewritten. That reuses the
existing `failed_rewrite_sources` idiom, which already carries per-source
rewrite failures into the result string as a named warning; the reassignment is
a new reason, not a new mechanism. Silence here would be the worst option: a
half-rewritten link graph reported as a clean move is precisely the "graph
asserting a link the vault bytes do not contain" the preflight exists to
prevent.

**What "changed" means.** The database's current `users.vault_path` for the
acting user, canonicalised the way `transfer.canonical_vault_root` canonicalises
a root, compared against the root this request bound at admission
(`current_vault_root`). Refuse when it differs, when it is now NULL, when the
`users` row is gone, or when `is_active` is false — the same four conditions
`APIKeyMiddleware` and `_credential_ok` already treat as loss of entitlement.
Comparison is on the canonical *pathname*, not on a `resolve()`d form: resolving
is itself a filesystem read that a concurrent rename can change, and the fact
being checked is what the operator saved, not what the disk currently looks
like.

**One question, one normaliser — and after round 4's rescope item 1 shares
it.** The earlier drafts had item 1 answering "is this the directory those rows
were scanned from?" and this one answering "is this still the assignment the
operator saved?", with two separate normalisations that must not be merged.
Item 1 now asks the *assignment* question too, so the two SHALL share one
definition: `transfer.canonical_vault_root`, unchanged and un-`resolve()`d, is
that definition, and item 1 **calls** it rather than re-implementing
`str(Path(path))`. Two copies is how the index's notion of "the same
assignment" and the write path's notion of it drift apart. For #88 the lexical
form is not a weakness but the definition: its harm is a write landing in a
vault the operator has moved the caller out of, which is a change to the
record, not to the disk. A symlink retarget under an unchanged assignment is
deliberately outside #88 — #59 pins the parent descriptor exactly so a pathname
relinked mid-call cannot redirect a write, and re-resolving here would
reintroduce the check-then-act #59 removed. So `canonical_vault_root` still
gains no `resolve()`. Item 1's *realpath* is a second **fact**, observed and
recorded alongside the assignment, not a different normalisation of it, and it
never enters this comparison. Item 1 may not modify `canonical_vault_root`;
this path depends on the symbol exactly as it stands.
**A fresh read, not a cache hit — and the honest cost.** Reading
`_user_vault_cache` or `current_vault_root` would be a tautology: those are the
values being checked. So this is one `SELECT users.vault_path, users.is_active
WHERE id = :uid`, and it reintroduces exactly the per-call query that #66
forbade in `_vault_root` ("Keep it a pure cache lookup … a DB query here would
be a query on every tool call"). The reconciliation is that #66's rule is about
*every tool call*, and this query runs only on **mutations**. Search, read,
list and the graph tools — which dominate the call mix by a wide margin — are
untouched, and a mutation already does far more expensive work than one indexed
primary-key lookup. Stated as a trade, not hidden: the admission gate stays a
pure cache lookup, and the mutation path gains one query.

**Scope, and what is deliberately outside it.** The six tools that publish
through a `MutableTarget` — `create_note`, `edit_note`, `move_note`,
`delete_note`, `set_frontmatter`, `write_file` — plus `delete_file`, which does
**not** go through `open_mutable` (it resolves via `_vault_context` and walks
from `vault_fs.open_root(root)`), and therefore needs its own confirmation
before `soft_delete` / `remove`. Naming that asymmetry rather than letting a
reviewer find it: the structural stamp covers six tools, and the seventh is
covered explicitly. `import_from_url` and `PUT /transfer/upload` already hold a
**stronger** gate — `lock_identity_for_publish` and `before_publish()` lock the
credential and user rows `FOR UPDATE` across the publish and re-check the root
against the one captured at mint — and SHALL NOT be weakened to this optimistic
form.

**The residual, precisely.** A reassignment that commits after the confirming
read and before the publish syscall — including one that commits while the
syscall is running — still lands in the former root, and the tool reports
success. Nothing short of a lock held across the publish closes that, and that
is the option this change rejected on purpose. What changes is the size of the
window: from the whole tool body (a read, a diff, a section resolve, an
embedding-sized payload) down to staging, `fsync` and one publishing call.

Because the confirmation is per publishing operation rather than per call, that
bound holds for `move_note(rewrite_links=True)` too — each rewrite carries its
own window rather than inheriting one taken before the move — at the cost that
the tool has *several* such windows and can therefore be refused part way
through. That partial outcome is specified above and reported, not swallowed.
The metadata transaction between the move and the first rewrite is not covered
by any confirmation and does not need to be: it writes no vault bytes, and it
describes a publication that has already happened.

## What Changes

- **`users.indexed_vault_assignment`, `users.indexed_vault_realpath` and
  `users.indexed_vault_handle`** (migration 016, nullable `text`, `text` and
  `varchar(320)`, no server defaults, one marked unit): the
  **provenance** of a user's index — the canonical assignment string the pass
  ran under, the realpath that assignment named at that moment — stored as
  `os.fsencode(realpath).hex()`, because a kernel-returned pathname need not be
  valid UTF-8 and the discard's transaction must never fail to encode — and,
  where the filesystem offers one, the opaque kernel file handle
  (`"<handle_type>:<hex>"`, from `name_to_handle_at`) of the pinned directory.
  Written only by `index_vault`, never by an operator-facing handler.
  **016 backfills none of them**, and every stamp writes all three, NULL for a
  fact the pass could not observe.
- **The comparison is assignment-level.** The recorded assignment string is
  `transfer.canonical_vault_root`'s form — the *same* single normaliser #88's
  pre-publish confirmation uses, called rather than re-implemented. The handle
  is **best-effort hardening in the refusing direction only**: a mismatch
  demotes a would-be keep to a re-derive, a match upgrades nothing, and where
  no handle is available the hardening is simply absent. **Filesystem
  substitution behind an unchanged assignment is a declared non-goal.**
- **The pass pins the assigned root as a descriptor**, derives the observed
  facts from that descriptor, and performs discovery and every vault-file read
  beneath it — in `index_vault`, `embed_vault`, `link_backfill_pass` and
  `rebuild_tsvectors` alike — so that within one pass the facts observed, the
  files discovered and the bytes read all come from one inode.
- **`index_vault` classifies the provenance before it scans.** Assignment and
  realpath both equal, with no observable handle mismatch → no-op; both differ →
  delete the user's `notes_metadata` (embeddings and links cascade) and stamp,
  in one committed transaction, before any file under the new root is read;
  anything else — no record at all, exactly one fact disagreeing, or a handle
  that contradicts an otherwise-matching pair → re-derive the index from the
  assigned root (change detection off, prune as usual, every link row
  re-extracted and re-resolved from the scan's own buffer, embeddings kept where
  the content hash still matches) and stamp after the pass completes; assigned
  root unopenable, or its realpath no longer naming the pinned inode → nothing
  at all. Never for `user_id is None`.
- **`link_backfill_pass` and `rebuild_tsvectors` skip a user whose provenance
  is not settled**, logging once, and leave the work to a later pass. They
  proceed only for a user whose recorded provenance matches the assigned root
  right now — the keep verdict, from the one function that computes it. The skip
  is per user, not global. Neither verifies what it reads, and the re-derive
  redoes both their jobs on every pass, so the delay costs nothing.
- **A re-derive that skipped any file is incomplete and is not stamped.** Any
  per-file discovery, read, stat, parse or link-extraction skip withholds the
  tail stamp; the pass logs the paths that kept it unstamped and the next pass
  re-derives again.
- **`embed_vault` verifies the hash it certifies and is therefore *not* gated**
  — one decision, specified as one requirement. It re-hashes what it read
  against the row it selected and skips on mismatch, so `embedded_content_hash`
  cannot certify a vector built from other bytes; that is what the re-derive's
  retention of `note_embeddings` rests on, and it is what makes running under
  any classification safe. Gating it instead would let one permanently
  unreadable file freeze a readable note's embeddings indefinitely, which
  `semantic_search` would serve without a staleness guard.
- **`transfer_tokens.actor_kind` / `actor_label` / `actor_ref`** (migration 017,
  nullable, marked): the denormalised actor, read from `current_actor` inside
  `mint_token` through the same single reader `_log_usage` uses, and copied onto
  the `UsageLog` by `_log_row` at redemption.
- **017 backfills `transfer_tokens` only**, from its own surviving FKs, guarded
  on `actor_kind IS NULL` and preceded by 015's orphan-label check (a label
  beside a NULL kind aborts the migration, changing nothing). It writes nothing
  to `usage_logs`.
- **Every vault mutation confirms the assignment before each publishing
  operation.** A fresh read stamps the target that operation publishes through;
  the publish helpers — including a new `MutableTarget`-based permanent-unlink
  helper that replaces `delete_note(permanent=True)`'s bare `os.unlink` — refuse
  an unstamped target; a changed, cleared or deactivated assignment refuses with
  nothing written and `usage_logs.params.error = "vault_reassigned"`.
- **`move_note(rewrite_links=True)` confirms before the move and before each
  link rewrite**, stops at the first refusal, and reports the partial outcome:
  which root the move landed in and which sources were left unrewritten.
- **`delete_file` confirms the same way** before its soft delete or unlink,
  since it does not publish through a `MutableTarget`.
- **The schema gate covers both migrations.** `tests/integration/
  test_schema_check.py` gains the 016 and 017 cases at the 013/014/015 bar —
  fresh shape and marker, the absence of a 016 backfill, 017's backfill
  grouping and orphan-label refusal, stamp-back idempotence, foreign and
  partial-column refusals, downgrade — and `HEAD_REVISION` becomes `017`.

## Capabilities

### Modified Capabilities
- `index-integrity`: the index records the **provenance** of its rows — the
  canonical vault assignment the pass ran under, the realpath that assignment
  named, and a best-effort file handle — all observed from a descriptor the
  pass pins and then scans beneath; a pass reconciles against that record
  before it scans, keeping an index only when the assignment is unchanged,
  discarding one whose assignment demonstrably moved, and re-deriving every
  ambiguous case — recording the result only when the re-derive visited every
  file, and skipping the two unverified ancillary passes for a user whose
  provenance is not settled while the hash-verifying embedding pass runs
  regardless. Filesystem substitution behind an unchanged assignment is a
  declared non-goal.
- `schema-integrity`: migrations 016 and 017, each owning its columns as a
  marked unit, with `alembic check` clean at head.
- `file-transfer`: a transfer capability records the actor that minted it, and
  the redemption's usage row carries that actor; `delete_file` confirms the
  vault assignment before it deletes.
- `vault-write`: a mutation confirms the caller's vault assignment immediately
  before each publishing operation, and refuses when it has moved; every
  destructive syscall on a mutation target, the permanent unlink included, goes
  through a helper that enforces it.

No new capability. The two migration requirements go to `schema-integrity`
rather than beside their behaviour (015 put its migration requirement in
`mcp-request-routing`) because this wave carries two migrations that
`make test-schema` gates as one unit, and splitting them across capabilities
would put one gate's contract in two places. The behaviour each migration
enables stays in `index-integrity` and `file-transfer` respectively.

The #88 requirement is **ADDED to `vault-write`, not a modification of "Note
mutations are anchored to the parent directory opened at validation"** — that
requirement is being rewritten by `atomic-beneath-root-writes`, which lands
first, and two changes modifying one requirement's text is a merge conflict
dressed up as a spec. The refusal's usage-log marker is a scenario on the new
requirement rather than a modification of `mcp-request-routing`'s "A refused
tool call is recorded in the usage log", which is about the admission gate and
stays about the admission gate.

## Impact

- `alembic/versions/016_indexed_vault_provenance.py` — new
- `alembic/versions/017_transfer_token_actor.py` — new (`down_revision = "016"`)
- `src/models/db.py` — `User.indexed_vault_assignment` /
  `User.indexed_vault_realpath` / `User.indexed_vault_handle` with 016's marker; `TransferToken.actor_kind` / `actor_label` / `actor_ref`
  with 017's marker
- `src/services/indexer.py` — the `name_to_handle_at` binding (wrapper-first
  `ctypes`, the `rename_noreplace` shape), the provenance helper (pin, observe
  the assignment through `transfer.canonical_vault_root`, the realpath and the
  optional handle — its own function, calling but **not** changing
  `canonical_vault_root`), the descriptor-anchored discovery and read helpers
  used by all four passes, the classification at the head of `index_vault`, the
  settled-provenance gate on `link_backfill_pass` and `rebuild_tsvectors` (and
  deliberately **not** on `embed_vault`), the re-derive mode with its
  completeness accounting, the link rebuild reading from the scan buffer,
  `embed_vault`'s hash verification, and the three-column stamp
- `src/auth/session.py` — the one shared reader of `current_actor`, extracted
  from `tools.py::_actor_columns` so mint and log cannot drift
- `src/services/transfer.py` — `mint_token` records the actor; the pre-publish
  root confirmation helper
- `src/transfer/routes.py` — `_log_row` copies the three columns
- `src/services/vault.py` — the confirmation stamp on `MutableTarget`, the
  publish helpers' refusal of an unstamped target, and a new permanent-unlink
  helper on the same seam
- `src/mcp_server/tools.py` — the confirmation before each publishing operation,
  `move_note`'s per-rewrite confirmation and partial-outcome reporting,
  `delete_note(permanent=True)` routed through the new helper, `delete_file`'s
  own confirmation, and the `vault_reassigned` marker
- `tests/integration/test_schema_check.py` — 016 and 017 cases;
  `HEAD_REVISION = "017"`
- `tests/test_issue_91_indexed_root.py`,
  `tests/test_issue_92_transfer_actor.py`,
  `tests/test_issue_88_root_confirmed_before_publish.py` — new

Carries two migrations, so `make test-schema` is a required gate and
`make db-check` must report "No new upgrade operations detected" after deploy.
Both a migration and a write-path refusal are in the mandatory
adversarial-review category; the Codex framing is destructive writes and
silently wrong search results, and this change touches both.
