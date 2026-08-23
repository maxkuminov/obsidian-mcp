# Deferred: the different-root half of #91

`truthful-surfaces` ships only the display half of #91 — `users.html` stops
printing a note count for an account whose tools are refused. The half
described here, **discarding an index whose vault assignment has moved to a
different root**, was drafted, reviewed and then pulled out: it carries a
migration and a slice that deletes a user's entire index on a string
comparison, so it belongs with the next wave's migration-carrying work, where
`make test-schema` and the adversarial pass run once over both. The two halves
were only ever adjacent because they came out of one issue.

This file exists so the later drafter can write the proposal without
re-deriving the analysis. It is not a spec and nothing here is committed to;
every claim about the code is a claim about the tree at `1ce4c9d` and must be
re-checked against the tree as it stands then.

## The defect

`_active_user_ids()` filters `vault_path IS NOT NULL`, so an unassigned user's
index rows are frozen rather than removed. Since #66 that is deliberate: every
tool is refused meanwhile — the admission gate is in `_tracked` — so the rows
are not a leak, and keeping them is what lets a reassignment resume without
re-embedding ~16.7k chunks. `mcp-request-routing` pins it: reassignment to the
same directory SHALL still find the previously indexed rows present.

**A different directory is a different question, and nothing answers it.**
`notes_metadata.file_path` is vault-*relative*; no row, and no column anywhere,
records which root the index was built from. So after an admin repoints a user
at another vault, the metadata-only tools — `semantic_search`, `keyword_search`,
`list_notes`, `get_recent` and every graph tool, all served from the database
filtered by `user_id` alone — answer from the *previous* vault: its paths, its
titles, its tags, its frontmatter, its chunk excerpts. `read_note` on one of
those paths then either fails or, worse, returns a genuinely different note that
happens to occupy the same relative path in the new root. CLAUDE.md names
"silently wrong search results" as one of the two expensive failures in this
product, because an agent acts on them without a human ever seeing the query.

## The sharpest form of the argument — keep this

The obvious objection is that the next pass fixes it anyway, and the objection
is *mostly right*. `index_vault` already prunes by relative path:
`deleted_paths = existing − files`, so every stale row whose relative path does
not exist under the new root is removed on the first pass over that root,
without any of the machinery below. A proposal that does not say this out loud
will be read as inventing work.

Two things genuinely do not reconcile, and they are the whole justification:

1. **The window.** The prune happens at the next pass, so between the Save and
   that pass the database-backed tools answer from the previous root. This one
   the column does not remove either — see the accepted residual — but it
   narrows what is served during it from "the previous vault" to "nothing".

2. **A note whose relative path *and* content hash are identical in both
   roots.** `index_vault` classes it "no change" and skips it, so its links are
   never re-extracted. Meanwhile the notes it *pointed at* were pruned, and
   `note_links.target_note_id` is `ON DELETE SET NULL` (`source_note_id` is
   `ON DELETE CASCADE`, which is why the source's own rows survive). The result
   is a link row that keeps its `target_path` and loses its resolution: a
   resolved link silently becomes dangling, permanently, because nothing will
   ever re-resolve it — the content hash matches, so the note is never
   re-extracted again. `get_backlinks`, `get_neighborhood` and `find_orphans`
   then report an under-resolved neighbourhood for that note for as long as it
   is unedited. This is not a window; it does not heal.

Item 2 is the one that makes a schema change worth carrying. It is also the one
a reviewer will not find on their own, so state it early.

## Why a schema column is unavoidable

The tempting cheap fix is to compare the old and new values in
`edit_user_submit` and purge when they differ. It does not work, and the reason
is the transition an operator actually performs:

    /old  →  unassigned  →  /new

Two Saves, because the panel's vault selector is how an admin takes an account
out of service before repointing it. On the second Save `edit_user_submit` sees
`old_vault = None`, and `None → /new` is *exactly* the shape of the case #66
protects — a user restored to the directory their index came from. The handler
cannot tell "reassigned back to where the index came from" (keep) from
"reassigned somewhere else" (discard), because the only thing that
distinguishes them is a value that no longer exists anywhere by the time it is
needed.

So what is required is not a comparison, it is a *record*: a value describing
the root the rows were built from, independent of the current assignment and
therefore surviving the unassignment. That is the entire reason it is a column
rather than two form values.

## The design as drafted

- **`users.indexed_vault_path`**, nullable `String(1024)`, no server default.
- **Compared at the head of `index_vault(user_id)`**, before
  `discover_markdown_files` — i.e. before any file under the new root is read —
  in one committed transaction. Three outcomes:
  - `indexed_vault_path == vault_path` → nothing happens. This is the #66 case
    and it stays free.
  - different, and `indexed_vault_path` is non-NULL → delete every
    `notes_metadata` row for that user (`note_embeddings` and `note_links`
    cascade), then stamp the new root. Log both roots and the row count.
  - `indexed_vault_path` IS NULL → stamp the new root, delete nothing. The root
    those rows came from was never recorded and MUST NOT be guessed.
- **Skipped entirely when `user_id is None`** — single-user mode has no `users`
  row, and neither reads nor writes the column.
- **Stamped only by the pass that establishes the state it describes**, never by
  an operator-facing handler. A panel handler that changes `vault_path` leaves
  the record alone; that asymmetry is what makes the record mean "what the rows
  are" rather than "what the assignment is".

Two properties this buys that a comparison inside the panel handler could not:

- **It survives the unassignment** — the argument above, and the reason the
  column exists at all.
- **The indexer stays the only writer of index contents.** Adding a second place
  that deletes `notes_metadata` is how two paths drift apart, which is the
  argument #64 made for resolving a grant family exactly one way. Every caller
  of `index_vault` — the startup pass, the periodic tick, and the panel's
  `_reindex_background` — inherits the reconciliation by calling it, in the same
  way every tool inherits the admission gate by being registered. Do not let a
  reviewer talk this into the POST handler for latency; that is the trade below,
  and it was taken deliberately.

A useful consequence to keep: a pass that commits the discard and then fails
while scanning the new root retries cleanly. The next pass finds the recorded
root already equal to the assigned root and simply indexes — it does not repeat
the delete, and it cannot re-serve the old rows, because they are gone.

## Accepted residual

The reconciliation runs in the indexer, so a reassignment is honoured at the
*next* pass, not at the Save. The window is bounded by
`INDEX_INTERVAL_SECONDS` (default 5 minutes) plus the duration of a pass
already in flight, and during it the metadata-only tools still answer from the
previous root.

Closing it entirely would mean either purging inside the panel's POST
transaction — a second writer of index contents, for a 5-minute improvement —
or refusing every tool for the whole interval, which breaks the disk-backed
tools that are already correct against the new root. Neither is worth it. This
is the same optimistic level declared for `edit_note(expected=…)` and the
transfer fingerprint check, and the same "takes effect at the next
authenticated request" shape as an OAuth revocation. Declare it; do not let it
be discovered.

## The one-time backfill hole

The migration backfills `indexed_vault_path = vault_path` for every row whose
`vault_path` is non-NULL — a fact the indexer's own scoping rule guarantees,
since only an assigned user is ever indexed — and leaves it NULL for the rest.

It therefore asserts nothing about an account that is **already unassigned when
the migration runs**. That account's previous root was never recorded anywhere,
so it gets exactly one reassignment without reconciliation, and after that pass
it is stamped and behaves like every other account. That is a one-time
consequence of introducing the column, not a rule, and it MUST NOT be closed by
guessing a root: the only guesses available are "the root it is being assigned
to now" (which silently keeps a foreign index) and "some other user's root"
(which is nonsense). Say this in the proposal rather than leaving a reviewer to
find it.

The backfill must also be guarded so a re-run — `alembic stamp` back, then
`upgrade head` — cannot overwrite a stamp the indexer has since written. That
is the same shape as 015's `actor_kind IS NULL` guard: the migration completes
what it created and never rewrites what the running system recorded afterwards.

## Rejected: age-based pruning

Dropping index rows that have not been touched for N days was considered and
rejected. It invents a retention policy nobody asked for; it deletes exactly the
rows #66 preserves on purpose; and the cost of being wrong is the full re-embed
that #66 exists to avoid. Reassignment to a different root is a real event with
a real trigger. "This index is old" is not an event, and an unassigned account
that is later restored to its own directory is the *normal* case, not a stale
one.

## Do not assign a migration number here

**The revision number must be assigned when the later change is drafted, not
now.** At `1ce4c9d` the head is `015_usage_log_actor`, and the draft this file
records used `016` — but the wave that will carry this also carries at least one
other migration (the actor label on transfer-route usage rows, #92 item 2, which
needs a column on `transfer_tokens`). Numbered migrations collide, and the
project's standing rule is that the numbers are assigned up front in the
contracts, or exactly one worktree owns schema changes. Pick the number when the
whole wave is on the table, and fix the head-revision assertions in
`tests/integration/test_schema_check.py` to match whatever it turns out to be.

The schema gate is `make test-schema`, and it is required for this work. The
cases the draft called for, beyond the column's presence, type and nullability:
the backfill's grouping (assigned users stamped with their own `vault_path`,
unassigned users left NULL), stamp-back idempotence, the downgrade, and
`alembic check` clean.

---

# Appendix: prior art

What follows is the spec delta text as it stood in `truthful-surfaces` before
this half was pulled out, preserved so the reasoning is not lost. **It is prior
art, not text to paste.** It was written against the tree at `1ce4c9d`, it names
migration `016`, and it was never implemented or verified — re-derive every
requirement against the code as it stands when the later change is drafted, and
re-run the Codex spec review on the result.

## `index-integrity` — as drafted

### ADDED Requirements

#### Requirement: The index records the vault root it was built from
The system SHALL record, per user, the vault root that the user's `notes_metadata` rows were built from, in a value that is independent of the user's current vault assignment and therefore survives an unassignment. That record SHALL be written only by the index pass that establishes the state it describes, and MUST NOT be written by any operator-facing handler that changes the assignment.

The record is required because `notes_metadata.file_path` is vault-relative: nothing in an index row says which root it came from, and comparing the previous and new values of the assignment cannot answer the question either, since the transition an operator performs is commonly `assigned` → `unassigned` → `assigned elsewhere` and the second step sees no previous root at all.

##### Scenario: A completed pass records the root it scanned

- **WHEN** an index pass runs for a user whose recorded root does not match the assigned root
- **THEN** the recorded root SHALL be updated to the assigned root in the same transaction as any reconciliation the mismatch requires

##### Scenario: The assignment handler does not write the record

- **WHEN** an administrator changes, clears or restores a user's vault assignment through the control panel
- **THEN** the recorded root SHALL be left unchanged by that request

##### Scenario: Single-user mode does not use the record

- **WHEN** an index pass runs with no user identifier
- **THEN** it SHALL neither read nor write the recorded root, because single-user mode has no user row

#### Requirement: A reassignment to a different root discards the previous root's index
When a user's assigned vault root differs from the root their index was built from, the index pass SHALL delete that user's `notes_metadata` rows — and, by cascade, their `note_embeddings` and `note_links` rows — before any file under the new root is read, and SHALL then record the new root. The discard MUST happen in one committed transaction, so no pass can leave rows describing one root beside rows describing another.

Serving the previous root's rows is the failure this prevents: the tools served purely from the database — `semantic_search`, `keyword_search`, `list_notes`, `get_recent` and the graph tools — would otherwise return paths, titles, tags, frontmatter and chunk excerpts from a vault the caller no longer has, and a subsequent read of one of those paths can silently return a different note that occupies the same relative path in the new root.

##### Scenario: Reassignment to a different directory

- **WHEN** a user whose index was built from one root is assigned a different root and the next index pass runs
- **THEN** the rows from the previous root SHALL be deleted before the new root is scanned
- **AND** the user's embeddings and link rows SHALL be removed with them

##### Scenario: Reassignment to the recorded root keeps the index

- **WHEN** a user's assignment is cleared and later restored to the same directory the index was built from
- **THEN** no row SHALL be deleted and no note SHALL be re-embedded, preserving the behaviour that makes an unassignment reversible without a full re-index

##### Scenario: An unchanged assignment is a no-op

- **WHEN** an index pass runs for a user whose assigned root equals the recorded root
- **THEN** no reconciliation SHALL be performed and the pass SHALL proceed exactly as before

##### Scenario: No recorded root discards nothing

- **WHEN** an index pass runs for a user whose recorded root is unset
- **THEN** the assigned root SHALL be recorded
- **AND** no row SHALL be deleted, because the root those rows came from was never recorded and MUST NOT be guessed

##### Scenario: Every caller of the index pass inherits the reconciliation

- **WHEN** the index pass is invoked from startup, from the periodic tick, or from an operator-triggered reindex
- **THEN** the reconciliation SHALL run in all three cases, because it lives in the pass rather than in any one caller

##### Scenario: A failed pass after a discard retries cleanly

- **WHEN** the discard commits and the subsequent scan of the new root fails
- **THEN** the next pass SHALL find the recorded root already equal to the assigned root and SHALL simply index, rather than repeating a delete or re-serving the old rows

#### Requirement: A reassignment is honoured at the next index pass, not at the moment of assignment
The reconciliation SHALL be performed by the index pass, and the system SHALL NOT claim that a reassignment takes effect immediately. Between the assignment being saved and the next pass completing its reconciliation, the database-backed tools may still answer from the previous root; that window is bounded by the configured index interval plus the duration of a pass already in flight.

This is a declared limitation, at the same level as the other optimistic guarantees in this system. Closing it would require a second writer of index contents inside the panel's request transaction, or refusing every tool — including the disk-backed ones that are already correct against the new root — for the whole interval.

##### Scenario: The bound is the index interval

- **WHEN** an administrator reassigns a user to a different root
- **THEN** the previous root's rows SHALL be gone once the first index pass started after that change has completed its reconciliation

##### Scenario: Disk-backed tools are not refused during the window

- **WHEN** a tool that reads the vault from disk is called during that window
- **THEN** it SHALL operate against the newly assigned root, and SHALL NOT be refused on account of the pending reconciliation

## `schema-integrity` — as drafted

### ADDED Requirements

#### Requirement: Migration 016 owns the indexed-root column and its backfill
Migration 016 SHALL add `users.indexed_vault_path` as a nullable `character varying(1024)` with no server default, SHALL backfill it from `users.vault_path` for every row whose `vault_path` is not null, SHALL leave it null for every row whose `vault_path` is null, and SHALL guard the backfill so that a re-run cannot overwrite a value the indexer has since written. `downgrade()` SHALL drop the column. After migrating to head, `alembic check` SHALL report no pending operations.

The backfill asserts a fact the indexer's own scoping rule guarantees — only a user with a non-null `vault_path` is ever indexed, so an assigned user's rows were built from the root currently assigned. It asserts nothing about an unassigned user, whose previous root was never recorded anywhere; such an account is left null and therefore gets one reassignment without reconciliation. That is a one-time consequence of introducing the column, not a rule, and it MUST NOT be closed by guessing a root.

##### Scenario: Fresh database

- **WHEN** an empty database is migrated to head
- **THEN** `users.indexed_vault_path` SHALL exist as nullable `character varying(1024)` with no server default
- **AND** `alembic check` SHALL report no new upgrade operations

##### Scenario: Backfill on a populated database

- **WHEN** 016 runs on a database holding assigned and unassigned users
- **THEN** every assigned user's `indexed_vault_path` SHALL equal that user's own `vault_path`
- **AND** every unassigned user's `indexed_vault_path` SHALL be null

##### Scenario: Re-running the migration does not overwrite a stamp

- **WHEN** the database is stamped back to 015 and upgraded again, after the indexer has recorded a root that differs from the current `vault_path`
- **THEN** the recorded root SHALL be left unchanged

##### Scenario: Downgrade

- **WHEN** a database at 016 is downgraded to 015
- **THEN** the column SHALL be dropped
- **AND** no other column SHALL be altered
