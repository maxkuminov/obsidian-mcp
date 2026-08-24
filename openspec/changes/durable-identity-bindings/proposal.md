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

### 1. The index does not record which vault root it was built from (#91, deferred half)

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

**A different directory is a different question, and nothing answers it.**
`notes_metadata.file_path` is vault-relative; no row, and no column anywhere,
records which root the index was built from. After an admin repoints a user at
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
is not a comparison but a **record**: a value describing the root the rows were
built from, independent of the current assignment and therefore surviving the
unassignment.

**Decision: 016 adds two columns, `users.indexed_vault_path` and
`users.indexed_vault_fsid`, nullable, one marked unit, written only by the
index pass — and 016 backfills neither.** The pair is the *identity of the
directory the rows were actually scanned from*, read at the head of
`index_vault(user_id)` before `discover_markdown_files` opens a single file, and
compared against the same two facts observed for the assigned root now. Skipped
entirely for `user_id is None` — single-user mode has no `users` row.

**Identity is not a normalised pathname, and this is the part the first draft
got wrong.** `transfer.canonical_vault_root` is `str(Path(path))` and nothing
more — its docstring says so, deliberately, because the question *it* answers is
"is this still the string the operator saved?". Reused here it is wrong in both
directions: a symlink retargeted from `/data/A` to `/data/B` under an unchanged
`/vaults/current` yields the *same* string for a *different* directory, so a
foreign index is kept; and `/vaults/current` versus `/vaults/real-a` naming one
directory yields *different* strings for the *same* one, so a good index is
destroyed and re-embedded. So the record is two facts, not one:

- `indexed_vault_path` — `os.path.realpath` of the root as it was scanned. This
  resolves symlinks and normalises separators, `.` and `..`, so a trailing
  separator or an aliasing symlink is never read as a reassignment.
- `indexed_vault_fsid` — an **opaque** `"<st_dev>:<st_ino>"` token for that
  directory's inode, observed at the same moment. Text, not integers: nobody
  should do arithmetic on it, and it sidesteps the unsigned-64 question that
  `bigint` columns would raise.

Neither alone is identity. A realpath comparison cannot see a directory deleted
and re-created at the same path; an inode tuple cannot be trusted on its own
because `st_dev` is not guaranteed stable across a reboot for every device type
and `st_ino` is reusable. So the pass reaches one of four verdicts, and **a keep
requires both signals to agree**:

| recorded vs. observed | verdict | what the pass does |
| --- | --- | --- |
| realpath equal **and** fsid equal | same directory | nothing |
| realpath differs **and** fsid differs | different directory | **discard**: delete the user's `notes_metadata` (embeddings and links cascade) and stamp, in one committed transaction, before any file under the new root is read |
| anything else — no record at all, or exactly one of the two disagreeing | **provenance unresolved** | **re-derive** (below), then stamp at the end of the pass |
| assigned root absent, not a directory, or not stattable | indeterminate | nothing at all: no delete, no stamp; the pass fails as it does today |

**Which error the design prefers, said plainly.** CLAUDE.md ranks silently wrong
search results above expensive ones, so the design never resolves an ambiguity
in favour of keeping. It also never resolves one in favour of the *destructive*
branch: a discard costs a full re-embed, and firing it on an `st_dev` that
shifted across a reboot would charge that on every restart. Ambiguity therefore
goes to a third branch that asserts nothing and destroys nothing. The
indeterminate row is the one place the design does nothing at all, and for a
reason: you cannot re-derive from a directory you cannot read, and destroying an
index because a bind mount was briefly unavailable buys nothing and costs the
full re-embed.

**The re-derive, precisely — and why it is not a compromise.** For an unresolved
root the pass runs over the assigned root with **content-hash change detection
disabled**: every discovered file is parsed and upserted regardless of its hash,
the ordinary prune removes every row whose relative path is not present under
that root, and because every note counts as changed,
`_update_links_for_changed` deletes and re-extracts **every** one of that user's
link rows and re-resolves them against an index built from those notes alone. So
after the pass, every surviving row and every link row was written by that pass
from a file under the assigned root. That is a structural claim about who wrote
the rows, not an enumeration of columns that has to be re-audited whenever a
column is added.

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

**Why 016 backfills nothing.** `indexed_vault_path = vault_path` looks free and
is not: "assigned now" is not "indexed from what is assigned now", and the
reassignment lag it ignores is the exact defect this change exists to close. An
admin who reassigns and deploys before the next pass gets rows from vault A
stamped as B; the next pass then sees both signals equal, takes the no-op
branch, and the identical-path/identical-hash case — the one that never heals —
is *guaranteed* suppressed rather than merely possible. NULL is the only value
that asserts nothing, and under the classification above NULL is not "stamp and
move on": it is the unresolved branch, so every legacy user is repaired once,
cheaply, on the first pass after the upgrade, and stamped only then.

This **deletes a hole rather than costing one**. The earlier draft carried a
"one-time backfill hole" for accounts already unassigned at migration time,
which got exactly one reassignment without reconciliation. Those accounts are
now NULL like everybody else and get the same repair. There is no special case
left to document.

**The stamp is written where the state it describes is established, which means
two different places.** On the discard branch the state is "this user has no
index rows", which is true the instant that transaction commits, so the stamp
goes with it at the head of the pass — and a pass that then fails while scanning
retries cleanly, because the next one finds both signals equal and simply
indexes. On the re-derive branch the state is "every row was derived from this
root", which is not true until the pass finishes, so the stamp is committed
**after** the pass's last write and only if the pass raised nothing. A crash
mid-repair leaves no stamp and the next pass repairs again — bounded,
idempotent, and never a stamp over a half-repaired index. Head-stamping a
re-derive would be exactly the false provenance this section is about, written
by our own code instead of by the migration.

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
cannot write either column — they are not on its models and no code path sets
them — so every row is NULL when the new container starts, whatever the old pass
wrote, and the new container's first pass per user takes the unresolved branch
and re-derives from the assigned root. Overlap between the two indexer loops is
prevented by `docker compose up -d --force-recreate` being stop-then-start for
one service, not by anything in the code. The residual is therefore a property
of the deploy command: **a deploy that runs two indexing containers of this
service concurrently — a second replica, a rolling deploy, a manually started
container — can let an old pass commit rows from the old root after a new pass
has tail-stamped the new one.** `make deploy` does not do that; an operator who
changes it must quiesce the old container before migrating.

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
one-time legacy population and by genuinely ambiguous identity.

**Rejected: age-based pruning.** Dropping index rows untouched for N days
invents a retention policy nobody asked for, deletes exactly the rows #66
preserves on purpose, and costs the full re-embed #66 exists to avoid when it
is wrong. Reassignment to a different root is a real event with a real trigger;
"this index is old" is not an event, and an unassigned account later restored
to its own directory is the *normal* case.

**Rejected: backfill `indexed_vault_path = vault_path`.** The failing input is
decisive and is carried into the spec as a scenario: vault A holds `Same.md`
linking to `OnlyA.md`; vault B holds a byte-identical `Same.md` and no
`OnlyA.md`; the user is indexed on A, reassigned to B, and the deploy runs
before the next pass. The backfill stamps B over rows built from A, both signals
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

**Two questions, two normalisers, and they must not be merged.** Item 1 above
replaces a lexical comparison with a realpath-plus-inode identity precisely
because *its* question is "is this the directory those rows were scanned from?".
This one's question is "is this still the assignment the operator saved?", and
for it the lexical form is not a weakness but the definition: #88's harm is a
write landing in a vault the operator has moved the caller out of, and that is a
change to the record, not to the disk. A symlink retarget under an unchanged
assignment is deliberately outside #88 — #59 pins the parent descriptor exactly
so a pathname relinked mid-call cannot redirect a write, and re-resolving here
would reintroduce the check-then-act #59 removed. So `canonical_vault_root`
stays exactly as it is and gains no `resolve()`; item 1's identity helper is a
*second, separate* function, and neither may be refactored into the other.
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

- **`users.indexed_vault_path` and `users.indexed_vault_fsid`** (migration 016,
  nullable `varchar(1024)` and `varchar(64)`, no server defaults, one marked
  unit): the realpath and the opaque `"<st_dev>:<st_ino>"` identity of the
  directory a user's index was actually scanned from, written only by
  `index_vault` and never by an operator-facing handler. **016 backfills
  neither.**
- **`index_vault` classifies the root before it scans.** Both signals equal →
  no-op; both differ → delete the user's `notes_metadata` (embeddings and links
  cascade) and stamp, in one committed transaction, before any file under the
  new root is read; anything else, including no record at all → re-derive the
  index from the assigned root (change detection off, prune as usual, every link
  row re-extracted and re-resolved, embeddings kept where the content hash still
  matches) and stamp after the pass completes; assigned root unreadable →
  nothing at all. Never for `user_id is None`.
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
- `index-integrity`: the index records the identity of the directory it was
  scanned from; a pass reconciles against that identity before it scans,
  discarding an index whose directory demonstrably moved and re-deriving one
  whose provenance it cannot resolve.
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

- `alembic/versions/016_indexed_vault_identity.py` — new
- `alembic/versions/017_transfer_token_actor.py` — new (`down_revision = "016"`)
- `src/models/db.py` — `User.indexed_vault_path` / `User.indexed_vault_fsid`
  with 016's marker; `TransferToken.actor_kind` / `actor_label` / `actor_ref`
  with 017's marker
- `src/services/indexer.py` — the root-identity helper (realpath + fsid, its own
  function, **not** a change to `transfer.canonical_vault_root`), the
  classification at the head of `index_vault`, the re-derive mode, and the
  tail stamp
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
