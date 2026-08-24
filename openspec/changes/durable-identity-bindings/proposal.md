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

**Decision: `users.indexed_vault_path`, nullable `String(1024)`, written only
by the index pass.** Compared at the head of `index_vault(user_id)`, before
`discover_markdown_files` reads a single file under the new root, in one
committed transaction. Three outcomes: equal → nothing; different and non-NULL
→ delete that user's `notes_metadata` (embeddings and links cascade) and stamp
the new root, in that one transaction; NULL → stamp, delete nothing, because
the root those rows came from was never recorded and MUST NOT be guessed.
Skipped entirely for `user_id is None` — single-user mode has no `users` row.

Two properties this buys that a panel-side comparison could not. It **survives
the unassignment**, which is the whole reason it is a column. And **the indexer
stays the only writer of index contents**: a second place that deletes
`notes_metadata` is how two paths drift apart, which is the argument #64 made
for resolving a grant family exactly one way. Every caller of `index_vault` —
the startup pass (`indexer.py:1014`), the periodic tick (`indexer.py:1080`) and
the panel's `_reindex_background` (`control_panel/routes.py:1536`) — inherits
the reconciliation by calling it, in the same way every tool inherits the
admission gate by being registered. **Do not let this be talked into the POST
handler for latency**; that is the trade below and it is taken deliberately.

A useful consequence: a pass that commits the discard and then fails while
scanning the new root retries cleanly. The next pass finds the recorded root
already equal to the assigned root and simply indexes — it does not repeat the
delete, and it cannot re-serve the old rows, because they are gone.

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
request" shape as an OAuth revocation. Declared, not discovered.

**The one-time backfill hole.** 016 backfills `indexed_vault_path =
vault_path` for every row whose `vault_path` is non-NULL — a fact the indexer's
own scoping rule guarantees, since only an assigned user is ever indexed — and
leaves it NULL for the rest. It therefore asserts nothing about an account that
is **already unassigned when the migration runs**. That account's previous root
was never recorded anywhere, so it gets exactly one reassignment without
reconciliation, after which it is stamped and behaves like every other account.
That is a one-time consequence of introducing the column, not a rule, and it
MUST NOT be closed by guessing: the only guesses available are "the root it is
being assigned to now" (which silently keeps a foreign index) and "some other
user's root" (which is nonsense).

**Rejected: age-based pruning.** Dropping index rows untouched for N days
invents a retention policy nobody asked for, deletes exactly the rows #66
preserves on purpose, and costs the full re-embed #66 exists to avoid when it
is wrong. Reassignment to a different root is a real event with a real trigger;
"this index is old" is not an event, and an unassigned account later restored
to its own directory is the *normal* case.

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
admission gate. The stamp is taken once per call, immediately before the call's
first publishing syscall, so `move_note(rewrite_links=True)` confirms before
the `renameat2` that commits the move and its rewrites are covered by the same
confirmation.

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

## What Changes

- **`users.indexed_vault_path`** (migration 016, nullable `varchar(1024)`, no
  server default, marked): the root a user's index was built from, written only
  by `index_vault` and never by an operator-facing handler.
- **`index_vault` reconciles before it scans.** Equal → no-op; different and
  non-NULL → delete the user's `notes_metadata` (embeddings and links cascade)
  and stamp, in one committed transaction, before any file under the new root
  is read; NULL → stamp only. Never for `user_id is None`.
- **`transfer_tokens.actor_kind` / `actor_label` / `actor_ref`** (migration 017,
  nullable, marked): the denormalised actor, read from `current_actor` inside
  `mint_token` through the same single reader `_log_usage` uses, and copied onto
  the `UsageLog` by `_log_row` at redemption.
- **017 backfills `transfer_tokens` only**, from its own surviving FKs, guarded
  on `actor_kind IS NULL`. It writes nothing to `usage_logs`.
- **Every vault mutation confirms the assignment before it publishes.** A fresh
  read stamps the targets the call is about to publish through; the publish
  helpers refuse an unstamped target; a changed, cleared or deactivated
  assignment refuses the call with nothing written and
  `usage_logs.params.error = "vault_reassigned"`.
- **`delete_file` confirms the same way** before its soft delete or unlink,
  since it does not publish through a `MutableTarget`.
- **The schema gate covers both migrations.** `tests/integration/
  test_schema_check.py` gains the 016 and 017 cases at the 013/014/015 bar —
  fresh shape and marker, backfill grouping, stamp-back idempotence, foreign
  and partial-column refusals, downgrade — and `HEAD_REVISION` becomes `017`.

## Capabilities

### Modified Capabilities
- `index-integrity`: the index records the root it was built from, and a pass
  discards an index whose root has moved before it scans the new one.
- `schema-integrity`: migrations 016 and 017, each owning its columns as a
  marked unit, with `alembic check` clean at head.
- `file-transfer`: a transfer capability records the actor that minted it, and
  the redemption's usage row carries that actor; `delete_file` confirms the
  vault assignment before it deletes.
- `vault-write`: a mutation confirms the caller's vault assignment immediately
  before it publishes, and refuses when it has moved.

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

- `alembic/versions/016_indexed_vault_path.py` — new
- `alembic/versions/017_transfer_token_actor.py` — new (`down_revision = "016"`)
- `src/models/db.py` — `User.indexed_vault_path` with 016's marker;
  `TransferToken.actor_kind` / `actor_label` / `actor_ref` with 017's marker
- `src/services/indexer.py` — the reconciliation at the head of `index_vault`
- `src/auth/session.py` — the one shared reader of `current_actor`, extracted
  from `tools.py::_actor_columns` so mint and log cannot drift
- `src/services/transfer.py` — `mint_token` records the actor; the pre-publish
  root confirmation helper
- `src/transfer/routes.py` — `_log_row` copies the three columns
- `src/services/vault.py` — the confirmation stamp on `MutableTarget` and the
  publish helpers' refusal of an unstamped target
- `src/mcp_server/tools.py` — the confirmation call on each mutating tool,
  `delete_file`'s own confirmation, and the `vault_reassigned` marker
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
