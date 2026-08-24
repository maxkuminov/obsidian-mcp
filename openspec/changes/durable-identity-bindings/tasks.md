# Tasks

Three slices, two of which carry a migration. **They are not all disjoint by
file, and the shape below says exactly where they collide and in what order
they must run.** A group that finds itself needing a file outside its scope
must stop and report rather than reach for it — that is a sign the split is
wrong, not a licence to widen it.

## Sequencing

    A (016 + indexed-root identity)  ─┐
                                      ├──►  B (017 + transfer actor)  ──►  D
    C (#88 pre-publish)              ─┘

**The `(A ∥ C) → B → D` shape is unchanged by the review fixes**, and that was
checked rather than assumed: finding 1's identity redesign and finding 2's
re-derive branch live entirely in A's files; findings 3 and 4 live entirely in
C's; finding 5 lives entirely in B's. The one cross-group hazard the earlier
draft carried has been *removed* — A no longer has permission to relocate or
alter `transfer.canonical_vault_root`, because A now needs a different function
for a different question (A.6), so C's dependency on that symbol is no longer
something A can break.

- **A and C may run as parallel worktrees.** Their file scopes are disjoint:
  A owns `alembic/versions/016_*`, `src/services/indexer.py`,
  `src/models/db.py` and `tests/integration/test_schema_check.py`; C owns
  `src/services/vault.py`, `src/mcp_server/tools.py` and its own new test. No
  file appears in both, and neither touches `src/services/transfer.py`.
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
  exists and that `alembic/versions/` ends at `015_usage_log_actor.py`, and
  that `src/services/transfer.py::canonical_vault_root` is still
  `str(Path(path))` — A must leave it that way and C depends on it. Group C
  additionally confirms that the `atomic-beneath-root-writes` work has landed —
  `src/services/vault.py` must already resolve the mutation parent through a
  single kernel-enforced beneath-root lookup rather than a per-component walk.
  If it has not, stop: C's seam is defined against that shape.

---

## A. The indexed-root identity and the reconciling pass (#91, deferred half)

**Files owned:** `alembic/versions/016_indexed_vault_identity.py` (new),
`src/models/db.py`, `src/services/indexer.py`,
`tests/integration/test_schema_check.py`,
`tests/test_issue_91_indexed_root.py` (new).

**Do not touch:** `src/control_panel/users.py` or any panel template. The
record is written **only** by the index pass. A panel handler that changes
`vault_path` leaves it alone, and that asymmetry is what makes the record mean
"what the rows are" rather than "what the assignment is". Do not add age-based
pruning; it is a rejected alternative, not a stretch goal. Do not delete
`notes_metadata` anywhere except inside the reconciliation. **Do not touch
`src/services/transfer.py`** — in particular do not change, move or `resolve()`
`canonical_vault_root`; see A.6.

- [ ] A.1 Add `User.indexed_vault_path` (nullable `String(1024)`) and
      `User.indexed_vault_fsid` (nullable `String(64)`) to `src/models/db.py`,
      no server default, each `comment=_INDEXED_ROOT_MARKER` where that
      constant is `"identity of the directory this user's index was scanned
      from (016_indexed_vault_identity)"`. Document on the columns why they
      exist (the `/old → unassigned → /new` transition erases the evidence a
      panel-side comparison would need), that the indexer is their only writer,
      and that `indexed_vault_fsid` is an **opaque** `"<st_dev>:<st_ino>"` token
      — text, never parsed as numbers. Keep the string byte-identical to
      `MARKER` in the migration or `alembic check` goes dirty.
- [ ] A.2 Write `alembic/versions/016_indexed_vault_identity.py`
      (`down_revision = "015"`). `SET LOCAL lock_timeout` /
      `statement_timeout` and `RESET` both at the end of `upgrade()` — `SET
      LOCAL` lasts for the transaction and `alembic/env.py` runs every pending
      revision in one, so a later revision would otherwise inherit them (013
      and 014 do the same, for the same reason). Add the two columns and stamp
      the COMMENT marker on each. **Backfill nothing.** Record in the
      docstring why: `vault_path` says what is assigned now, not what the rows
      were built from, and stamping it would make the never-heals link case
      guaranteed rather than possible; a NULL record is the only true statement
      available, and the pass repairs a NULL user by re-deriving rather than
      discarding, so introducing the columns costs no vault-wide re-embed.
      Record the deploy-order argument too: 016 writes no provenance, so an old
      container's in-flight pass has nothing to contradict, and that — not any
      lock — is why the deploy is safe.
- [ ] A.3 Treat pre-existing columns of either name as an ownership question,
      not a convenience, and treat the two as **one unit**: both absent →
      create and mark; both present, nullable, exactly `varchar(1024)` /
      `varchar(64)`, default-free **and** marked → accept as a re-run; anything
      else (either one wrong-typed, `NOT NULL`, defaulted or unmarked, or
      exactly one of the two present) → raise naming what was found, changing
      nothing. The marker is what makes this stronger than 015's case: the
      value is the sole input to a decision that deletes a user's whole index,
      so adopting a foreign column is a mass delete on a value nobody in this
      scheme wrote.
- [ ] A.4 `downgrade()` drops the columns **only** if they carry the marker,
      all-or-nothing.
- [ ] A.5 In `src/services/indexer.py`, add the classification at the head of
      `index_vault(user_id)`, after `vault = _vault_root(user_id)` and
      **before** `discover_markdown_files(vault)`. Skip entirely when
      `user_id is None`. Observe the assigned root's identity, read the
      recorded pair, and take exactly one of four branches:
      **both agree** → nothing;
      **both disagree** → in one committed transaction `DELETE FROM
      notes_metadata WHERE user_id = :uid` (embeddings cascade, links cascade
      on `source_note_id` and null out on `target_note_id`) and record the new
      identity, committed before the first file under the new root is opened;
      **anything else, NULL included** → run the pass in re-derive mode (A.7)
      and record the identity **after** the pass's last write, only if it
      raised nothing;
      **assigned root missing / not a directory / not stattable** → do nothing
      at all: no delete, no record; let the pass fail as it does today.
      Log both identities and the deleted row count on the discard path, and
      log the re-derive path with its reason (no record, or which signal
      disagreed).
- [ ] A.6 Write the identity helper **in this module, as a new function** —
      `os.path.realpath` plus an opaque `f"{st.st_dev}:{st.st_ino}"` from one
      `os.stat` of that realpath. **Do not reuse or modify
      `transfer.canonical_vault_root`, and do not add `resolve()` to it.** The
      two answer different questions and the first draft conflated them: this
      one asks "is this the directory those rows came from?" and must read the
      filesystem; `canonical_vault_root` asks "is this still the string the
      operator saved?" and deliberately must not. Group C depends on the
      unchanged behaviour of `canonical_vault_root`, so changing it is also a
      cross-group break. Use the new helper on both sides of every comparison
      and for the value recorded, so a trailing separator, a redundant
      separator or a `.` component is never read as a reassignment.
- [ ] A.7 Add the re-derive mode to `index_vault`: content-hash change
      detection disabled, so every discovered file is parsed and upserted
      regardless of its hash; the ordinary prune unchanged; and every note
      therefore counted as changed, so `_update_links_for_changed` deletes and
      re-extracts **every** one of that user's link rows and resolves them
      against an index built from those notes alone. **Do not delete
      `note_embeddings`** — a vector on a row whose `content_hash` still
      matches the file under the assigned root is provably that file's vector,
      and `embed_vault`'s existing `embedded_content_hash != content_hash`
      selection re-embeds exactly the rest. Leave move detection enabled; under
      a full re-upsert plus a full link rebuild it can only preserve a valid
      row id and its valid embeddings.
- [ ] A.8 `tests/integration/test_schema_check.py`: set `HEAD_REVISION = "016"`
      (B moves it to `"017"`); a fresh database has both columns nullable,
      exactly typed, default-free and marked; **016 backfills nothing** — after
      it, both columns are NULL for every user including every assigned one,
      and no `notes_metadata` / `note_embeddings` / `note_links` row changed;
      `alembic stamp 015` then `upgrade head` after the indexer has recorded an
      identity differing from `vault_path` changes nothing; a foreign
      same-named column is refused for each of wrong type, `NOT NULL`, server
      default and missing marker, and a half-present pair is refused, with the
      schema unchanged; a complete marked pair is accepted; downgrade to 015
      drops a marked pair and leaves a pair with either column unmarked;
      `alembic check` clean at head.
- [ ] A.9 New `tests/test_issue_91_indexed_root.py`, covering the four verdicts
      and both repair shapes:
      **identity** — a retargeted symlink under an unchanged assignment is a
      different directory; two aliases of one directory (a symlink alias and a
      bind-mounted alias, the latter skipped where the harness cannot bind
      mount) never discard; a directory replaced at the same real path is
      re-derived, not kept and not discarded; trailing and redundant
      separators and a `.` component are not a reassignment; an assigned root
      that is missing or unstattable deletes nothing and records nothing;
      **discard** — removes `notes_metadata`, `note_embeddings` **and**
      `note_links` for that user and nothing for any other user; the delete and
      the record are one transaction (a failure between them leaves neither
      applied); the record lands before any file under the new root is read; a
      pass that fails after committing the discard is retried by the next pass
      without a second delete;
      **re-derive** — a NULL record re-derives instead of discarding or
      trusting; the reviewer's case, verbatim: vault A holds `Same.md` linking
      to `OnlyA.md`, vault B holds a byte-identical `Same.md` and no
      `OnlyA.md`, the user is indexed on A, reassigned to B, and the first pass
      after the upgrade must leave `Same.md`'s link rows re-extracted from B
      and resolved against B alone, with `OnlyA.md`'s row gone; a note whose
      content hash still matches keeps its `note_embeddings` rows and triggers
      no embedding call; a re-derive that raises records nothing and the next
      pass re-derives again; a re-derive that completes records the identity
      after its last write and the next pass takes the no-op branch;
      **single-user** — `user_id is None` neither reads nor writes the record;
      **inheritance** — all three callers of `index_vault` (startup, tick,
      panel reindex) get the classification.

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
- [ ] B.5a **Port 015's `_assert_no_orphan_labels` to `transfer_tokens` and run
      it before the backfill.** The first draft of this proposal claimed to
      follow 015 exactly and omitted this; it is the invariant that makes the
      `actor_kind IS NULL` guard safe on a stamp-back re-run. Same shape as
      015: select up to 20 offending ids where `actor_kind IS NULL AND
      (actor_label IS NOT NULL OR actor_ref IS NOT NULL)`, count the rest,
      raise naming them, and change nothing. Without it a re-run relabels such
      a row from whatever credential its FK points at *now*, rewriting a
      recorded attribution — the one thing these columns must never do.
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
      recorded by a mint; **a stamp-back re-run over a row carrying a label
      beside a NULL `actor_kind` fails naming the offender and leaves every
      `transfer_tokens` row byte-for-byte as it was**; refusals for a partial
      set, a `NOT NULL` column, a server default, a wrong type and an unmarked
      set; a complete marked set accepted; downgrade drops the marked set and
      leaves a partly unmarked one; `alembic check` clean at head.
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
that it runs only on mutations. Use `transfer.canonical_vault_root` **exactly
as it is** and do not add `resolve()` to it: this comparison is about the value
the operator saved, not about what the disk currently looks like, and resolving
here would reintroduce the check-then-act #59 removed. Group A owns a
*separate* directory-identity helper for a different question and does not
touch this one, so its import path is unchanged.

- [ ] C.1 Add an async confirmation in `src/services/vault.py`: one
      `SELECT users.vault_path, users.is_active WHERE id = :uid` on its own
      short-lived session, canonicalised and compared against the root this
      request bound in `current_vault_root`. Refuse on any of: differs, now
      NULL, row absent, `is_active` false. Return the confirmation rather than
      a bare boolean, so the caller has something to stamp with. No-op —
      issuing no query at all — when `user_id is None` outside multi-user mode.
- [ ] C.2 Stamp the confirmation onto the `MutableTarget` the *next* publishing
      operation will go through, and make the shared publish helpers
      (`_atomic_write_at`, `move_file_no_clobber`, `vault`'s `soft_delete_at`
      call, **and the new permanent-unlink helper of C.2a**) **refuse a target
      carrying no confirmation for the operation about to be performed**. That
      refusal is a programming error and must raise distinguishably from the
      operational refusal in C.1 — a reviewer, and a log reader, must be able to
      tell "somebody added a tool and forgot" from "an admin reassigned
      mid-call".
- [ ] C.2a **Add a `MutableTarget`-based permanent-unlink helper to
      `src/services/vault.py` and route `delete_note(permanent=True)` through
      it**, replacing the bare `os.unlink(target.name, dir_fd=target.dir_fd)` at
      `src/mcp_server/tools.py:1969`. It is the only bare mutating syscall left
      on the target path, and while it stands the "publish helpers refuse an
      unstamped target" claim is false rather than merely incomplete. The helper
      keeps the existing semantics — unlink through the parent descriptor, no
      symlink following — and adds only the stamp check.
- [ ] C.3 Take the confirmation **immediately before each publishing
      operation**, covering exactly that operation, and never carry one across
      an `await`, a database transaction or a later publication. Five of the six
      tools publish once, so for them this is one confirmation per call. For
      `move_note(rewrite_links=True)` take one before the `renameat2` that
      commits the move (after the rewrite preflight, so a refusal there aborts
      before any mutation) **and one before each `write_file_at` in the rewrite
      loop** — the loop sits behind an `async with async_session()` metadata
      transaction of unbounded duration, and reusing the pre-move stamp across
      it is the same staleness this change exists to narrow. The metadata
      transaction itself needs no confirmation: it writes no vault bytes and
      records a publication that already happened.
- [ ] C.3a On a rewrite refusal, **stop the loop** — every remaining rewrite
      would write into a vault the caller no longer holds, through descriptors
      pinned before the reassignment — and report the partial outcome: the move
      completed in the previous root, the assignment changed mid-call, and these
      sources were left unrewritten. Carry it through the existing
      `failed_rewrite_sources` / `_rewrite_failure_warning` idiom with a
      distinct reason rather than inventing a second reporting mechanism. Do not
      roll the move back and do not undo the metadata update: the note really is
      at its new path and the rows must keep saying so.
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
      issues exactly one re-read per publishing operation; a read tool issues no
      such query and `_vault_root` still performs no database work; single-user
      mode issues none either; and the refusal writes the distinct error marker.
      Plus, specifically:
      **both delete forms** — `delete_note(permanent=True)` and
      `delete_note()` are each refused for a missing stamp and for a changed
      stamp, independently, with the note still at its path and no `.trash`
      entry; the permanent form's refusal comes from the helper, not from the
      tool body (assert it by calling the helper directly with an unstamped
      target);
      **the move interleavings** — refused before the move leaves the note,
      every source and both tables unchanged; a reassignment committing *after*
      the move and *during* the metadata transaction refuses the first rewrite,
      rewrites no further source, leaves every unrewritten source
      byte-identical, and produces a result naming the previous root, the
      reassignment and the unrewritten sources; a reassignment committing
      between rewrite *k* and *k+1* stops at *k+1* with the first *k* written;
      **the unstamped path** — publishing through each helper with a target
      carrying no confirmation raises, distinguishably from the operational
      refusal.
- [ ] C.9 A registry-shaped test in the same module, in the idiom of
      `tests/test_issue_66_vault_unassignment_revokes_tools.py`: enumerate the
      tools registered on the MCP server, and assert that every one that gates
      on `_require_write` either publishes through a confirmed target or is on
      an explicit, justified list of tools holding the stronger locked gate
      (`import_from_url`, `request_upload`). A tool added later must fail this
      test rather than silently publish unconfirmed. Add a second, structural
      assertion in the same module: **no module under `src/mcp_server/` or
      `src/services/vault.py` calls a mutating syscall on a `MutableTarget`'s
      `dir_fd` outside the publish helpers** — a source scan for `os.unlink`,
      `os.rename`, `os.link` and `os.mkdir` with a `dir_fd=` argument, allowed
      only inside the helpers themselves. That is what would have caught the
      permanent unlink of C.2a.

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
      at the five things most likely to be wrong: whether the reconciliation
      can ever delete an index it should have kept **or keep one it should have
      re-derived**; whether the re-derive branch really leaves every surviving
      row and link derived from the assigned root, and whether keeping
      `note_embeddings` on a matching `content_hash` is sound; whether the
      identity classification can be defeated by a symlink, a bind mount, a
      replaced directory or an unstable device number; whether the 016/017
      marker and refusal logic can adopt a foreign column or rewrite a recorded
      actor on a stamp-back; and whether the #88 confirmation can be bypassed by
      a publish path that does not go through a `MutableTarget` — naming the
      permanent unlink as the one that was missed the first time. Tell it that
      the previous round's BLOCKERs were a provenance backfill and a lexical
      root comparison, so it can check the replacements rather than rediscover
      the originals.
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
