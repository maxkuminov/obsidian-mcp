# Tasks

Three slices, two of which carry a migration. **They are not all disjoint by
file, and the shape below says exactly where they collide and in what order
they must run.** A group that finds itself needing a file outside its scope
must stop and report rather than reach for it — that is a sign the split is
wrong, not a licence to widen it.

## Sequencing

    A (016 + indexed root)  ─┐
                             ├──►  B (017 + transfer actor)  ──►  D (gates)
    C (#88 pre-publish)     ─┘

- **A and C may run as parallel worktrees.** Their file scopes are disjoint:
  A owns `alembic/versions/016_*`, `src/services/indexer.py`,
  `src/models/db.py` and `tests/integration/test_schema_check.py`; C owns
  `src/services/vault.py`, `src/mcp_server/tools.py` and its own new test. No
  file appears in both.
- **B must run after both**, and cannot be parallelised with either:
  - it needs 016 to exist, because `017.down_revision = "016"`. Numbered
    migrations collide, and a worktree that guesses the predecessor produces a
    branched revision graph that only shows up at `upgrade head`;
  - it edits `src/models/db.py` and `tests/integration/test_schema_check.py`,
    both of which A edits — and `HEAD_REVISION` in that test module is a single
    constant that A moves to `016` and B moves to `017`, so two worktrees
    editing it independently is a guaranteed conflict on the one line that
    decides whether the gate is real;
  - it edits `src/mcp_server/tools.py`, which C edits, because the shared
    `current_actor` reader is extracted out of `_actor_columns` and that call
    site moves.
- **D runs once, on the merged result.** Per-worktree gate runs prove nothing
  about the merge, and both migrations must be exercised in one `make
  test-schema` run.
- **Base check, before anything.** Every worktree opens by confirming
  `openspec/changes/archive/2026-08-23-truthful-surfaces/DEFERRED-91a.md`
  exists and that `alembic/versions/` ends at `015_usage_log_actor.py`. Group C
  additionally confirms that the `atomic-beneath-root-writes` work has landed —
  `src/services/vault.py` must already resolve the mutation parent through a
  single kernel-enforced beneath-root lookup rather than a per-component walk.
  If it has not, stop: C's seam is defined against that shape.

---

## A. `users.indexed_vault_path` and the reconciling pass (#91, deferred half)

**Files owned:** `alembic/versions/016_indexed_vault_path.py` (new),
`src/models/db.py`, `src/services/indexer.py`,
`tests/integration/test_schema_check.py`,
`tests/test_issue_91_indexed_root.py` (new).

**Do not touch:** `src/control_panel/users.py` or any panel template. The
record is written **only** by the index pass. A panel handler that changes
`vault_path` leaves it alone, and that asymmetry is what makes the record mean
"what the rows are" rather than "what the assignment is". Do not add age-based
pruning; it is a rejected alternative, not a stretch goal. Do not delete
`notes_metadata` anywhere except inside the reconciliation.

- [ ] A.1 Add `User.indexed_vault_path` to `src/models/db.py`: nullable
      `String(1024)`, no server default, `comment=_INDEXED_ROOT_MARKER` where
      that constant is `"the vault root this user's index was built from
      (016_indexed_vault_path)"`. Document on the column why it exists (the
      `/old → unassigned → /new` transition erases the evidence a panel-side
      comparison would need) and that the indexer is its only writer. Keep the
      string byte-identical to `MARKER` in the migration or `alembic check`
      goes dirty.
- [ ] A.2 Write `alembic/versions/016_indexed_vault_path.py`
      (`down_revision = "015"`). `SET LOCAL lock_timeout` / `statement_timeout`
      and `RESET` both at the end of `upgrade()` — `SET LOCAL` lasts for the
      transaction and `alembic/env.py` runs every pending revision in one, so a
      later revision would otherwise inherit them (013 and 014 do the same, for
      the same reason). Add the column, stamp the COMMENT marker, backfill
      `indexed_vault_path = vault_path WHERE vault_path IS NOT NULL AND
      indexed_vault_path IS NULL`.
- [ ] A.3 Treat a pre-existing `users.indexed_vault_path` as an ownership
      question, not a convenience: absent → create and mark; present, nullable,
      exactly `varchar(1024)`, default-free **and** marked → accept as a
      re-run; anything else (wrong type or width, `NOT NULL`, server default,
      unmarked) → raise naming what was found, changing nothing. The marker is
      what makes this stronger than 015's case: the value is the sole input to
      a decision that deletes a user's whole index, so adopting a foreign
      column is a mass delete on a string nobody in this scheme wrote.
- [ ] A.4 `downgrade()` drops the column **only** if it carries the marker.
- [ ] A.5 In `src/services/indexer.py`, add the reconciliation at the head of
      `index_vault(user_id)`, after `vault = _vault_root(user_id)` and
      **before** `discover_markdown_files(vault)`. Skip entirely when
      `user_id is None`. Open a session, read `indexed_vault_path`, and in one
      committed transaction: equal (after canonicalising both sides the same
      way) → nothing; different and non-NULL → `DELETE FROM notes_metadata
      WHERE user_id = :uid` (embeddings cascade, links cascade on
      `source_note_id` and null out on `target_note_id`) and stamp; NULL →
      stamp only. Log both roots and the deleted row count on the discard path.
      Commit before the first file under the new root is opened.
- [ ] A.6 Use one canonicalisation on both sides of the comparison and for the
      value stamped, so a trailing separator or another normalisation-equivalent
      spelling is never read as a reassignment.
      `transfer.canonical_vault_root` is the existing normaliser and is the
      obvious candidate; if importing it from `src/services/indexer.py` creates
      a cycle, move it to a module both can import rather than writing a second
      one. **Do not write a second normaliser** — two of them is how the
      comparison starts disagreeing with the stamp.
- [ ] A.7 `tests/integration/test_schema_check.py`: set `HEAD_REVISION = "016"`
      (B moves it to `"017"`); a fresh database has the column nullable,
      `varchar(1024)`, default-free and marked; the backfill stamps every
      assigned user with **their own** `vault_path` and leaves every unassigned
      user NULL, with no cross-user stamping; `alembic stamp 015` then
      `upgrade head` after the indexer has recorded a root differing from
      `vault_path` changes nothing; a foreign same-named column is refused for
      each of wrong type, `NOT NULL`, server default and missing marker, with
      the schema unchanged; a complete marked column is accepted; downgrade to
      015 drops a marked column and leaves an unmarked one; `alembic check`
      clean at head.
- [ ] A.8 New `tests/test_issue_91_indexed_root.py`: the four outcomes of the
      head-of-pass comparison (equal, different, NULL, single-user skip); the
      discard removes `notes_metadata`, `note_embeddings` **and** `note_links`
      for that user and nothing for any other user; the delete and the stamp
      are one transaction (a failure between them leaves neither applied); the
      stamp lands before any file under the new root is read; a pass that fails
      after committing the discard is retried by the next pass without a second
      delete; **the link case** — a note with the same relative path and the
      same content hash in both roots does not survive the reconciliation
      carrying a link row whose target resolution was silently dropped; and all
      three callers of `index_vault` (startup, tick, panel reindex) inherit it.

---

## B. The actor on `transfer_tokens` (#92, item 2)

**Runs after A and after C.** See Sequencing.

**Files owned:** `alembic/versions/017_transfer_token_actor.py` (new),
`src/models/db.py`, `src/auth/session.py`, `src/services/transfer.py`,
`src/transfer/routes.py`, `src/mcp_server/tools.py` (the `_actor_columns` call
site only), `tests/integration/test_schema_check.py`,
`tests/test_issue_92_transfer_actor.py` (new).

**Do not touch:** `_credential_ok`, `resolve_root_ok`, `plan_mint_window`'s
clamp, or anything in the publish gate. The label is display and audit only and
is never read for authorization. Do not add a `transfer_token_id` to
`usage_logs`. Do not re-run 015's `usage_logs` backfill from 017.

- [ ] B.1 Extract the single reader of `current_actor` — the mapping from the
      ContextVar triple to `{actor_kind, actor_label, actor_ref}` **including
      the truncation to the stored widths** — out of
      `src/mcp_server/tools.py::_actor_columns` into `src/auth/session.py`,
      beside the ContextVar it reads. Leave `_actor_columns` as a thin
      delegation so the tool-call log path is unchanged. One reader is the
      point: the widths are identical on both tables and a second copy is how
      the mint and the log start truncating differently.
- [ ] B.2 Add `TransferToken.actor_kind` / `actor_label` / `actor_ref` to
      `src/models/db.py` — nullable `String(20)` / `String(255)` / `String(64)`,
      no server default, each `comment=TransferToken._ACTOR_COLUMN_MARKER`
      where that constant is `"denormalised actor, recorded at mint
      (017_transfer_token_actor)"`. Document that the redemption request
      carries a capability rather than a credential, which is why the label has
      to be recorded at mint.
- [ ] B.3 In `mint_token` (`src/services/transfer.py`), populate the three
      fields from the shared reader **inside the mint transaction**, before the
      INSERT. Read the ContextVar there rather than taking a parameter — the
      `plan_mint_window` discipline: a caller-supplied value is one the caller
      can get stale or wrong. An unset ContextVar leaves all three NULL and the
      row keeps its pre-017 shape.
- [ ] B.4 In `src/transfer/routes.py::_log_row`, copy `row.actor_kind`,
      `row.actor_label` and `row.actor_ref` onto the `UsageLog`. Nothing else
      about that function changes; `params` still never contains the token.
- [ ] B.5 Write `alembic/versions/017_transfer_token_actor.py`
      (`down_revision = "016"`), same `SET LOCAL` / `RESET` discipline as 016.
      Add the three columns, stamp each with the marker, and backfill guarded
      on `actor_kind IS NULL`: from `api_keys` for rows with `key_id`, and from
      `oauth_tokens` → `oauth_clients` for rows with `oauth_token_id`. Because
      both FKs are `ON DELETE CASCADE`, a row whose credential is gone does not
      exist to label — the rows left NULL are the ones carrying neither FK, and
      they stay NULL rather than being inferred from `user_id`.
- [ ] B.6 Treat the three as one owned unit, exactly as 015 does: all absent →
      create and mark; all present, exactly typed, nullable, default-free and
      marked → accept as a re-run; anything else (partial set, `NOT NULL`,
      server default, unmarked, foreign) → raise naming what was found.
      `downgrade()` drops only marked columns, all-or-nothing.
- [ ] B.7 017 writes **nothing** to `usage_logs`. Record why in the migration's
      docstring: there is no reference from a usage row back to the token that
      produced it, and the only alternative is re-running 015's credential join
      — a second writer on columns 015 owns and guards, which is the second
      resolution path #64 argued against. Rows in the 015→017 gap keep
      join-only attribution and render honestly.
- [ ] B.8 `tests/integration/test_schema_check.py`: move `HEAD_REVISION` to
      `"017"`; fresh-database shape and marker for all three columns; the
      backfill labels a key-minted row from its own key and an OAuth-minted row
      from its own client, never from another row; a row with both FKs NULL
      stays NULL; pre-existing transfer-route `usage_logs` rows are untouched
      by 017; `alembic stamp 016` then `upgrade head` does not rewrite an actor
      recorded by a mint; refusals for a partial set, a `NOT NULL` column, a
      server default, a wrong type and an unmarked set; a complete marked set
      accepted; downgrade drops the marked set and leaves a partly unmarked one;
      `alembic check` clean at head.
- [ ] B.9 New `tests/test_issue_92_transfer_actor.py`: a mint records the actor
      the middleware bound, on both the API-key and the OAuth branch, and the
      OAuth label is the `client_name`; the mint issues **no additional
      statement** for it (statement count against the pre-change baseline); a
      redemption's `usage_logs` row carries the three values; that row still
      renders the actor after the OAuth client is deleted (cascade) and after
      the panel's null-then-delete sequence for an API key; a rename between
      mint and redemption does not change the recorded label; a mint with no
      request-scoped actor records none and the redemption row keeps its old
      shape; and the recorded actor changes no redemption decision — a
      capability whose recorded credential has been deleted is refused by the
      same predicates as before.

---

## C. Confirm the vault root before publishing (#88)

**May run in parallel with A.** Must land before B.

**Files owned:** `src/services/vault.py`, `src/mcp_server/tools.py`,
`tests/test_issue_88_root_confirmed_before_publish.py` (new).

**Do not touch:** `src/transfer/routes.py`, `transfer.before_publish` or
`transfer.lock_identity_for_publish`. Those paths already hold the stronger
locked gate and are explicitly not weakened to this form. Do not change
`_vault_root` into anything that queries the database — the admission gate
stays a pure cache lookup, and the whole justification for the new query is
that it runs only on mutations. Do not add a second normaliser for the root;
use `transfer.canonical_vault_root` (Group A may have relocated it — check
before assuming its import path).

- [ ] C.1 Add an async confirmation in `src/services/vault.py`: one
      `SELECT users.vault_path, users.is_active WHERE id = :uid` on its own
      short-lived session, canonicalised and compared against the root this
      request bound in `current_vault_root`. Refuse on any of: differs, now
      NULL, row absent, `is_active` false. Return the confirmation rather than
      a bare boolean, so the caller has something to stamp with. No-op —
      issuing no query at all — when `user_id is None` outside multi-user mode.
- [ ] C.2 Stamp the confirmation onto the `MutableTarget`(s) the call is about
      to publish through, and make the shared publish helpers
      (`_atomic_write_at`, `move_file_no_clobber`, and `vault`'s
      `soft_delete_at` call) **refuse a target carrying no confirmation from
      the current call**. That refusal is a programming error and must raise
      distinguishably from the operational refusal in C.1 — a reviewer, and a
      log reader, must be able to tell "somebody added a tool and forgot" from
      "an admin reassigned mid-call".
- [ ] C.3 Take the confirmation **once per call**, immediately before the
      call's first publishing syscall, and stamp every target that call will
      publish through. For `move_note(rewrite_links=True)` that is before the
      `renameat2` that commits the move, after the rewrite preflight, so a
      refusal aborts before any mutation; the rewrites are covered by the same
      confirmation. Do not take one confirmation per rewrite source.
- [ ] C.4 Wire the six tools in `src/mcp_server/tools.py` — `create_note`,
      `edit_note` (every mode, `dry_run` included: a dry run publishes nothing
      and therefore needs no confirmation, but it must not be the reason a
      later mode skips one), `move_note`, `delete_note`, `set_frontmatter`,
      `write_file`. Confirm the placement against `_leaf_state_error` and the
      existing no-clobber refusal so the ordering of error messages stays
      sensible: a symlinked leaf is still named as one rather than reported as
      a reassignment.
- [ ] C.5 Give `delete_file_impl` its own confirmation before
      `vault_fs.soft_delete` / `vault_fs.remove`. It resolves through
      `_vault_context` and walks from its own `vault_fs.open_root(root)`, so
      the target stamp does not reach it. Refuse before `check_trash_support`
      creates anything.
- [ ] C.6 Record the refusal in `usage_logs.params` as an error marker naming a
      changed vault assignment, distinct from the admission gate's
      `no_vault_assigned`, and with no other new field. Use the request-scoped
      params holder `_tracked` already merges (`src/services/timing.py`);
      `_tracked` remains the only thing that calls `begin()`/`clear()`.
- [ ] C.7 Document the residual in the tool's own error text and in the
      docstrings: the confirmation narrows the window to staging, flush and one
      publishing call — it does not close it, and a reassignment committing
      inside the publish still lands in the former root. Say it at the same
      level as `edit_note(expected=…)`.
- [ ] C.8 New `tests/test_issue_88_root_confirmed_before_publish.py`:
      reassignment, unassignment, deactivation and a deleted user row each
      refuse, for each of the seven tools, with the former root's file
      byte-identical afterwards and nothing created in the new root; the
      confirmation is a **fresh read** (database changed, cache and snapshot
      still stale → still refused); an unchanged assignment publishes and
      issues exactly one re-read; `move_note(rewrite_links=True)` refused
      leaves the note, every source and both tables unchanged; `delete_note`
      refused creates no `.trash` entry; publishing a target with no
      confirmation raises, distinguishably; a read tool issues no such query
      and `_vault_root` still performs no database work; single-user mode
      issues none either; and the refusal writes the distinct error marker.
- [ ] C.9 A registry-shaped test in the same module, in the idiom of
      `tests/test_issue_66_vault_unassignment_revokes_tools.py`: enumerate the
      tools registered on the MCP server, and assert that every one that gates
      on `_require_write` either publishes through a confirmed target or is on
      an explicit, justified list of tools holding the stronger locked gate
      (`import_from_url`, `request_upload`). A tool added later must fail this
      test rather than silently publish unconfirmed.

---

## D. Gates (merged result, once)

- [ ] D.1 `pytest --ignore=tests/integration` green
- [ ] D.2 `make test-schema` green with `HEAD_REVISION = "017"` — required, this
      change carries two migrations
- [ ] D.3 `openspec validate durable-identity-bindings --strict`
- [ ] D.4 `make audit`
- [ ] D.5 Adversarial Codex pass, framed as a defensive control review. Both
      triggers fire: two migrations, and a refusal on the write path. Give it
      the product framing — the consumer is an agent, the expensive failures
      are destructive writes and silently wrong search results — and point it
      at the three things most likely to be wrong: whether the 016
      reconciliation can ever delete an index it should have kept, whether the
      017 marker/refusal logic can adopt a foreign column, and whether the #88
      confirmation can be bypassed by a publish path that does not go through a
      `MutableTarget`.
- [ ] D.6 `openspec-verifier` subagent against this proposal and the spec deltas
- [ ] D.7 Deploy: `make deploy`, then `make db-check` must report "No new
      upgrade operations detected"
- [ ] D.8 In place of the `user-representative` browser pass (there is no
      browser UI on the MCP side), exercise the affected tools against the live
      server and name in the report which were actually called: `create_note`
      and `edit_note` (the confirmed publish path, unchanged assignment),
      `request_upload` followed by a redemption and then `check_upload` (the
      minted actor and the attributed usage row), and `keyword_search` plus a
      graph tool after a reindex (the reconciliation left a coherent index).
      Do **not** exercise a live reassignment against the operator's own
      account; that path is covered by tests.
