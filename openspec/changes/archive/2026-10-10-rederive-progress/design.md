# Design: re-derive progress (#311)

## Context

A multi-user scope enters **re-derive** when `classify_provenance` cannot
resolve the recorded provenance (`users.indexed_vault_assignment / _realpath /
_handle`): no record, a half-set record, a handle mismatch, or exactly one of
assignment and real path changed. (Both changed is a **discard**: every row is
deleted in one committed transaction and nothing is re-derived.) A re-derive
is today a stateless repair:

- `force_read = backstop or re_derive or not INDEX_STAT_SHORTCUT` — no stat
  shortcut for any file;
- `_needs_body` retains every body; the attempt's change-detection branch is
  skipped (`not re_derive and …`), so every file goes into `to_upsert`, gets a
  keyword-vector UPDATE and a link delete-and-re-extract;
- `clear_sweep_state(user_id)` forgets the clean exclusion sweep;
- the tail stamp is written only if `withholding` is empty (#308 D8), else
  `REDERIVE_INCOMPLETE`, which `indexer_health.record_rederive` counts toward
  `degraded`.

The stamp is a **scope-wide** claim — "every surviving row was derived from
this root" — and the pass has no way to remember that it already made that
true for all rows but one. So one row-backed unreadable file makes every tick
a full-scope rewrite (#311).

The expensive failure in this product is **silently wrong search results**: a
row from the previous root served as the current root's. Every choice below is
ranked by that first, write volume second.

## Goals / Non-goals

Goals: a repeated re-derive rewrites only rows not yet derived under the
current root; a foreign row is never treated as derived under the current
root; the stamp records only when every surviving row is; restart, A→B→A,
moves, a file becoming readable and a file being deleted all behave correctly.

Non-goals: changing the classification table, the discard, `/health`'s shape
or the full-hash backstop; detecting filesystem substitution behind unchanged
facts (declared out of scope by the existing requirement).

## D1 — Mechanism: a persisted, bound per-row marker (migration 030)

Options compared:

**(a) Per-row marker column** (chosen). `notes_metadata.derived_under`, written
in the same statement/transaction as the derived state it describes.

**(b) Per-scope in-process set of completed paths.** No migration. Correct if
it is keyed exactly like (a)'s digest and updated only after commit. Costs:
lost on every restart, so each deploy, OOM or pod reschedule of a scope that is
in re-derive costs one more full-scope rewrite — on k3s with deploy-on-merge
that is several per day in an active week, i.e. #308's amplification moved
from "per tick" to "per restart", not removed. It also needs post-commit
bookkeeping (an ambiguous commit, a cancellation between commit and the
update, quarantine restarts) to stay in step with rows it cannot see, and it
cannot be checked against the database by a test or an operator.

**(c) A per-user "re-derive epoch" on `users` plus a per-row epoch.** Mint an
epoch when a re-derive starts for a new set of facts. Equivalent to (a) for
the steady case, but A→B→A mints a fresh epoch for the second A and loses
progress that is provably valid, and it adds two columns and a minting rule
(when is a re-derive "new"?) for no correctness gain over binding the marker
to the facts themselves.

**(d) Store the three facts per row.** Same information as (a)'s digest, three
wide columns (the real path is hex of an arbitrary pathname; the handle up to
320 chars) per row, and still needs binding to path/hash/version (D3).

Why (a): it is atomic with the derived state by construction (the marker
commits or rolls back with the rows it describes — no post-commit step), it
survives restart, and it turns the stamp's gate into a database-checkable
invariant (D6). The migration is one nullable column, metadata-only.

## D2 — The digest

```
derived_under = sha256(json.dumps(
    ["derived-under-v1", facts.assignment, facts.realpath_hex, facts.handle,
     file_path, content_hash, extraction_version],
    ensure_ascii=True, separators=(",", ":"))).hexdigest()
```

- **The facts are the provenance stamp's own triple**, as `observe_root_facts`
  returns them for the pinned root. Equality of the triple is the strongest
  identity the system has (the stamp records nothing more); a marker for
  facts F means "derived by a re-derive pass whose pinned root presented F".
  The handle is included verbatim, NULL included: a handle-mismatch re-derive
  (same assignment and real path, directory replaced) must not find the old
  directory's rows current. The cost is that a handle that flickers between
  unobtainable and obtainable changes the digest and forces one more full
  re-derive — the refusing direction.
- **Exact equality, not `classify_provenance`'s handle-tolerant comparison.**
  The classification ignores a handle that is absent on one side; the marker
  must not, because tolerance there is a keep decision and a marker is only
  ever consulted inside a re-derive.
- **The row's `file_path`, `content_hash` and `extraction_version` are bound
  in** (D3). `ensure_ascii` makes the encoding total over surrogate-escaped
  strings (the real path is already hex; assignments come from the database).
- The version tag lets a later change redefine the digest: every existing
  marker then mismatches, which is "not derived" — the safe direction.
- Computed in Python only (the pass compares; SQL never recomputes it).

## D3 — Who writes the marker, and why other writers need not change

**Inside the index pass** (`_index_vault_attempt`):

- The batch upsert's `ON CONFLICT … SET` and the id-preserving move UPDATE set
  `derived_under = NULL`; a fresh insert carries NULL.
- After the link rebuild (the pass's last derived-state write) and before the
  stamp, **only on a re-derive pass**, one owner-scoped UPDATE (chunked) sets
  `derived_under` to each row's digest for exactly the *fully derived* set:
  `to_upsert ∪ moved_new_paths` minus every path with a keyword-vector or
  link-rebuild skip. The UPDATE is predicated on `file_path`,
  `content_hash` and `extraction_version` equal to the values the digest was
  computed from.
- A keep-mode or single-user pass never writes a non-NULL marker. Single-user
  rows (`user_id IS NULL`) therefore always carry NULL, which is what the
  setup-time adoption UPDATE in `src/auth/routes.py` carries into the adopting
  user's scope — and that user has no provenance record, so it re-derives.
- The stat refresh, the grammar invalidation, the embedding writes and the
  prune do not touch it.

**Rule for every other writer** (Codex spec review r1, MAJOR). A marker is
valid only for the `(file_path, content_hash, extraction_version)` it was
computed with, so a writer that changes one of those invalidates it without
knowing the column exists. But some writers change a row's **root-dependent
extracted link state without changing any bound field**, and the binding does
not see them. The failing input: an unresolved scope marks source S under
root A; the user is reassigned to B; `move_note(T.md, U.md)` rewrites every
`note_links.target_path = 'T.md'` row — S's included — to `U.md`; reassigned
back to A, S's marker still matches, S is not re-extracted, and D7 can only
re-resolve the already-mutated `U.md`, never recover `T.md` from A's bytes.
Provenance A would be stamped over B-era graph state. So:

> Any writer that changes a row's derived state, or the extracted state of
> its link rows (`target_path`, `link_text`, `kind`, `position`, the row set
> itself), outside that row's own full derivation by a re-deriving pass,
> SHALL set that row's `derived_under` to NULL in the same transaction.

Applied to every such writer (found by grepping every `notes_metadata` /
`note_links` write outside the attempt's upsert):

| Writer | Rows cleared |
| --- | --- |
| `move_note` metadata transaction | the moved row; every `source_note_id` whose `note_links` row the `target_path` UPDATE changes; every backlink source in the call's planned rewrites (their files are about to change — the hash check would catch them, the clear makes it unconditional) |
| the pass's id-preserving move `target_path` rewrite (`move_tp_sql`) | every `source_note_id` whose rows it changes (the moved row itself is NULLed by the move UPDATE; D3 above). Sources in the pass's fully-derived set are re-marked by the tail as usual; others are re-derived by the next re-deriving pass |
| link backfill (`link_backfill_pass`) | every note whose link rows it deletes and inserts. Clearing was chosen over hash-certifying its input: the backfill reads bodies without comparing them with `content_hash`, it runs only on a keep verdict, and a clear costs at most one forced re-derivation of the row if a re-derive ever follows |
| dangling re-resolution, D7 re-resolution | none: they write only `target_note_id`, which D7 recomputes at completion |
| tsvector rebuild, embed pass, panel resets, stat refresh, grammar invalidation | none: they write values that are functions of hash-verified bytes plus configuration (or no derived state at all) |
| setup-time adoption (`user_id NULL → uid`) | none needed: single-user rows always carry NULL |

The binding still covers what it covers: a previous build (deploy overlap, or
an image rollback without `alembic downgrade`) that rewrites a marked row with
different content or path invalidates its marker; one with the same path and
bytes wrote the same derived values. A previous build's `target_path` rewrite
on a move is **not** covered — that is inside L2.

## D4 — Per-row force and change detection

Let `D(row) = digest(facts, row.file_path, row.content_hash,
row.extraction_version)` for the pass's facts; a row is **current** iff
`row.derived_under == D(row)`. Only meaningful when `re_derive` is True.

- **Scan eligibility (stat shortcut).** A file may skip its read iff the
  shortcut is on, the pass is not a full-hash pass, the row's extraction
  marker is current, its stat is non-NULL and equal in all four fields, **and
  the pass is not a re-derive or the row is current**. `force_read` becomes a
  per-row decision (`_scan_vault` receives the facts digest input).
- **Body retention.** `_needs_body` retains the body iff the row is absent,
  the hash differs, the extraction marker is stale, or (re-derive and the row
  is not current).
- **Change detection under the lock.** The "no change" branch applies iff the
  extraction marker is current, the path is in the locked rows with an equal
  hash, **and the pass is not a re-derive or the locked row is current**. A
  current row with an equal hash under a re-derive therefore gets exactly the
  ordinary treatment, stat refresh included.
- **C4.** `SnapshotRow` gains the marker, so a row whose marker changed
  between the snapshot and the lock (another pass completed it; a writer moved
  it) is re-processed under the lock, with its body, against the locked row.
- **Decisions use the locked row** (C4's rule), never the snapshot's marker.

## D5 — Withholding, narrowed to "could leave a row not derived under this root"

D8 (#308) withholds on a skip that could hide *a row*. The precise rationale
is a row **not derived under the current root**; a current row cannot be
foreign. So, under a re-derive:

| Skip | Withholds iff |
| --- | --- |
| scan read, C4 re-read, raw-body fallback, parse failure | the locked row at the path exists **and is not current** |
| directory the walk could not list (non-root prefix) | some locked row at or beneath the prefix is **not current** |
| C5 deferral | the locked row is **not current** |
| keyword-vector skip, link-rebuild skip | always (unchanged; the path's marker is NULL after the upsert) |
| row-less read skip, D7 not-indexable, quarantine | never (unchanged) |

A skip on a current row is still logged and still blocks the backstop's clock
(D12 is unchanged). A parse failure on a current row leaves that row stale but
not foreign — the state a keep pass would leave it in.

## D6 — The stamp is gated on the row invariant

After the pass's last write **including** the completion re-resolution (D7),
as the last statement before the NOWAIT stamp, the pass re-reads the scope's
rows **in its own transaction** — when it is about to record, with `FOR SHARE
NOWAIT` in a savepoint (NOWAIT for the tail stamp's deadlock reason; a refused
lock withholds the stamp as `REDERIVE_UNRECORDED`) —
(`file_path, content_hash, extraction_version, derived_under`) and counts the
rows that are not current. The stamp is recorded iff `withholding` is empty
**and** that count is zero; otherwise the outcome is `REDERIVE_INCOMPLETE`.
`withholding` remains the named-offender list; if the count is non-zero while
`withholding` is empty (a writer this design did not foresee, a concurrent
insert by another container), the pass is still incomplete and logs the first
`SKIP_REPORT_LIMIT` such paths — the invariant, not the list, is
authoritative. The count travels on `IndexPassResult.rederive_pending` (0 when
recorded or not a re-derive).

A committed concurrent insert seen by the READ COMMITTED re-read carries NULL
and makes the pass incomplete: conservative.

## D7 — Completion re-resolution of link targets

Today a recording re-derive extracted **and resolved** every link row against
the final row set in one pass. With progress, a carried-forward note's links
were resolved in an earlier pass, against a row set that may have contained
rows since pruned (a protected row beneath a directory that became listable, a
C5-deferred row) — its link would now be dangling (`ON DELETE SET NULL`)
where a full re-derive would have resolved it to another candidate. So the
pass that is about to record re-resolves **every** link row of the scope:
`resolve_target(target_path, source_path, build_vault_index(final rows))`,
updating `target_note_id` only where it differs, in the same transaction,
reading no file. Link extraction (the row set itself) is a function of the
note's bytes and extraction version, which the marker binds; resolution is a
function of the row set, which only the final pass knows. A `target_path` at
the 1,024-character storage cap may have been truncated from the extracted
target and is left as resolved (L3).

## D8 — The exclusion sweep

`clear_sweep_state(user_id)` moves from "every re-deriving pass, before the
scan" to "after a re-deriving pass commits, iff it upserted, moved or deleted
at least one row". A carried-forward row was swept (or not) after it was
derived, and nothing about it changed. A pass that fails rolls back and
changed nothing.

## D9 — Reporting while unresolved rows remain

- `rederive_incomplete` increments on each committed incomplete re-derive, and
  `/health` reports `degraded` after `INDEXER_DEGRADED_AFTER_FAILURES` — by
  design: a possibly foreign row is still served. No `/health` field changes.
- The incomplete WARNING gains "N row(s) not yet derived under the current
  root" and keeps naming the withholding offenders.
- What a stuck scope now costs per tick: the unresolved rows' re-read and (if
  readable) rewrite, the invariant re-read, plus — **pre-existing and
  unchanged** — a full-hash read of the scope, because a persistently
  unreadable file keeps the scope due for the backstop (D12). Reads, not
  writes.

## D10 — Migration 030

- `notes_metadata.derived_under varchar(64) NULL`, no default, no index (only
  read in the owner-scoped snapshot and the tail re-read), comment marker
  `_DERIVED_UNDER_COLUMN_MARKER` declared in the ORM and the migration.
- **No backfill: every existing row reads NULL.** Backfilling the recorded
  provenance's digest would be pointless and would assert provenance a
  migration never observed (016's rule): a re-derive happens only when the
  observed facts differ from the recorded ones (no record; a handle mismatch;
  one fact changed), so a digest of the recorded facts can never equal a
  re-derive's expected digest. NULL is equivalent and asserts nothing. A scope
  that is in re-derive at deploy time pays one more full re-derive, then
  progresses.
- 026's shape: `search_path` pinned, `lock_timeout`/`statement_timeout` set
  and `RESET`; reconcile-or-refuse on the stamp-back re-run (a same-named
  column not `varchar(64)`, NOT NULL, with a default, or without the marker is
  refused by name); `downgrade()` drops only a marked column. Downgrade is
  safe: the previous build neither reads nor writes it.
- `alembic check` stays clean (ORM and migration declare the same column and
  comment); the schema gate's head literal becomes `030`.

## Scenarios

**File becomes readable.** The row is not current → forced read → upsert,
keyword vector, links → tail marks it → invariant holds → stamp recorded →
next pass is keep.

**File deleted.** Its row is not seen; snapshot equals locked → pruned →
nothing left that is not current → stamp. (Its inbound links are re-resolved
by D7.)

**Restart mid re-derive.** Markers are persisted. The first pass after start
is a full-hash pass: every file is read and hashed, current rows with an equal
hash are not rewritten, only unresolved and changed rows are. Quarantine
entries are re-learned as today.

**A→B→A.** Discard (both facts differ) at either step deletes every row; the
markers go with them. Re-derive at each step: under B, A-marked rows mismatch
B's digest and are forced; rows B rewrites carry B's digest; rows B could not
rewrite keep A's — and they are A-derived. Back under A, those A-marked rows
whose path, hash and extraction version are unchanged are current (they were
derived from A's bytes at that path) and get ordinary change detection; every
B-marked or NULL row is forced. Link targets are re-resolved at recording
(D7). If the middle step is a **keep** (A's re-derive interrupted by an
assignment back to the recorded root R), keep passes write NULL on every row
they rewrite, so only untouched A-derived rows stay A-marked.

**External move/rename of a current row.** The new path is new (not in the
locked rows), so it is upserted or move-paired from bytes read this pass; the
move UPDATE sets NULL and the tail marks the new path; backlink sources whose
`target_path` the move rewrote are NULLed and re-derived. **`move_note`**
NULLs the moved row and every source whose link rows it mutates or whose file
it plans to rewrite: each is not current and the next re-derive pass
re-derives it (one read each). **A→B→A with `move_note` under B** is the
review's failing input: S's A marker is cleared by the move, so A re-extracts
S's link from A's bytes before it can stamp. **Move of a not-current row**
(file moved, unreadable at neither end): same-hash pairing rewrites it from
the new path's bytes and marks it; unreadable at the new path: the old path is
pruned, the new path is a row-less read skip → no withholding.

**Quarantine.** A quarantined note's row is deleted (D7 mechanism); nothing to
mark, nothing counted. A quarantine restart rolls back the attempt's marker
writes with everything else.

**#309.** A protected row beneath an unlistable directory withholds iff it is
not current; it is counted by the invariant either way. `walk_incomplete` and
its degradation are unchanged.

**Full-hash pass under re-derive.** Reads every file; current rows with an
equal hash are not rewritten; a current row whose hash changed is upserted
and re-marked.

**Stat shortcut under re-derive.** Allowed for current rows only (D4).

**Extraction-version bump during a re-derive.** Every digest changes (the
version is bound in) and every row is marker-stale anyway: one full re-derive,
as today.

## Accepted limitations

- **L1 — filesystem substitution behind identical facts** (including two
  passes that both observed no handle) is undetected, exactly as the
  provenance record's own declared non-goal.
- **L2 — deploy overlap** with a previous build that writes rows from a
  *different* root while the new build re-derives is not prevented; the
  binding (D3) invalidates any marker on a row whose path or bytes it changed,
  and an unchanged row's derived values are identical. The existing
  "two indexing containers" limitation stands.
- **L3 — link targets of 1,024 characters** (possibly truncated at storage)
  are not re-resolved at completion.
- **L4 — a handle that flickers** between unobtainable and obtainable changes
  the digest and costs one more full re-derive.
- **L6 — a reassignment A→B→A completed inside one pass's final window**,
  with a `move_note` under B committing before the final locked re-read, or
  one confirmed under B whose metadata transaction commits after the stamp,
  is not detected: the stamp matches A, so no re-derive follows (Codex
  implementation review r1, triaged implausible; no cross-writer lock
  protocol). The final re-read takes `FOR SHARE NOWAIT` on the scope's rows
  and the stamp follows in the same transaction, so a writer that is mid-commit
  withholds the stamp instead.
- **L5 — a scope with a permanently unreadable row-backed file** stays
  incomplete and `degraded` indefinitely and, as before #311, keeps the scope
  due for the full-hash backstop (a full read per tick). Only the write
  amplification is removed.

## Out of scope

- Changing the classification, the discard or the stamp's columns.
- Any `/health` field (see open question 1).
- The keep-after-partial-re-derive case (re-derive interrupted by an
  assignment back to the recorded root): already a keep verdict, unchanged.
- Read amplification from the backstop on a persistently unreadable file.

## Owner decisions (2026-10-10)

1. Should `/health` expose a pending-row count per degraded re-derive? Decided
   **no** (counts only, but it is one more field for a rare state; the log and
   `IndexPassResult` carry it).
2. The walk-failure narrowing (D5) lets a re-derive **record** while a
   directory is unlistable, provided every row beneath it is current;
   `walk_incomplete` still reports `degraded`. Decided **yes** — the record
   is about foreign rows, and #309 already reports the unlisted directory.

## Noticed, not in scope

- **Backstop read amplification.** Any persistently unreadable file (any
  mode, not only re-derive) keeps its scope due for the full-hash pass, so
  every tick reads and hashes the whole scope (D12, by design). Worth its own
  issue if it matters on the reference host.
- **Keep after an interrupted re-derive.** A re-derive under B interrupted by
  an assignment back to the recorded root takes the keep branch; same-hash
  rows B rewrote keep links resolved against B's row set until they change.
  The marker could detect this, but it changes the keep branch.
- **Ordinary deletion does not re-resolve inbound bare-name links** to another
  candidate (they fall back to dangling) in keep mode either; only new notes
  attach previously-dangling rows.
