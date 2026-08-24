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

**The `(A ∥ C) → B → D` shape survives round 3, and it was re-checked rather
than assumed** — round 3 moved work only inside A, and the question worth
answering is whether it moved work into a file C owns. It did not, and the one
place it nearly did is settled below.

Round 2's fixes: finding 1's identity redesign and finding 2's re-derive branch
lived entirely in A's files; findings 3 and 4 in C's; finding 5 in B's. The
cross-group hazard the first draft carried was removed then — A no longer has
permission to relocate or alter `transfer.canonical_vault_root`, because A needs
a different function for a different question (A.6), so C's dependency on that
symbol is not something A can break.

Round 3's three fixes are the kernel file handle (A.6), the pinned root
descriptor with anchored discovery and reads (A.6a, A.6b), and the completeness
accounting (A.7a). **All three land in `src/services/indexer.py`, which only A
owns.** The one that had to be checked carefully is the second: anchored
discovery needs a descriptor-relative directory walk, and this repository
already has an anchored-filesystem module in `src/services/vault_fs.py`.

**Decision: A does not touch `src/services/vault_fs.py`, and the indexer's walk
is A's own, built on stdlib `dir_fd=` primitives.** This is a design call, not
an ownership dodge, and it would stand even if A and C ran sequentially:

- `vault_fs` is the **mutation** primitive module. Every helper in it writes or
  refuses — publish, `rename_noreplace`, `soft_delete`, `create_temp`, the
  write probes — and after `atomic-beneath-root-writes` its containment
  contract is `openat2(RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS |
  RESOLVE_NO_MAGICLINKS)`: **no** symbolic link anywhere in the path, ever.
  CLAUDE.md is explicit that relaxing that walk to follow in-vault links "would
  weaken the primitive `/transfer/*` depends on".
- The indexer needs the opposite leaf policy and must keep it: a markdown file
  reached through a symbolic link is indexed today, and `Path.rglob` already
  declines to descend directory symlinks. A shared helper would have to fork
  its symlink policy per caller — and a future editor unifying the two forks
  would silently change either what the index contains or what a transfer may
  write. The right answer is two walks with two policies, in the two modules
  that own those policies, not one walk with a flag.
- The indexer's walk is read-only, needs no `openat2`, and is four stdlib calls
  (`os.open(O_DIRECTORY|O_NOFOLLOW, dir_fd=…)`, `os.scandir(fd)`,
  `os.open(name, dir_fd=…)`, `os.fstat`). Putting it in `vault_fs` would add a
  read primitive to a module whose entire contract is "this is how we mutate".

So no shared primitive is created, no resequencing is needed, and the `(A ∥ C)`
disjointness claim below is re-stated with A's round-3 additions folded in.

- **A and C may run as parallel worktrees.** Their file scopes are disjoint:
  A owns `alembic/versions/016_*`, `src/services/indexer.py`,
  `src/models/db.py` and `tests/integration/test_schema_check.py`; C owns
  `src/services/vault.py`, `src/mcp_server/tools.py` and its own new test. No
  file appears in both; neither touches `src/services/transfer.py`; and
  **neither touches `src/services/vault_fs.py`** — C reaches
  `vault_fs.soft_delete` / `vault_fs.remove` as an unchanged caller, and A
  reaches nothing in it at all.
- **A must leave `discover_markdown_files(vault)` callable by pathname.**
  `tests/test_symlink_mutation_guard.py` imports it and asserts what it
  discovers under a symlinked folder. A adds the descriptor-taking form and
  keeps the pathname form as a thin wrapper over it, so that test passes
  **unchanged** — which makes it the regression check that anchoring did not
  change what the index contains. A therefore does not need that file in its
  scope, and must not edit it; if it stops passing, the walk is wrong, not the
  test.
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
  `str(Path(path))` — A must leave it that way and C depends on it. **Group A
  additionally confirms that `ctypes.CDLL(None).name_to_handle_at` resolves in
  the container image and that a handle can be read for the vault root**; if it
  raises `EOPNOTSUPP` there, that is not a blocker — it is the degraded branch
  of A.6 and must be exercised as such. Group C
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
`canonical_vault_root`; see A.6. **Do not touch `src/services/vault_fs.py`** —
the indexer's anchored walk is A's own and read-only; see Sequencing for why
that is a design decision rather than an ownership one. **Do not edit
`tests/test_symlink_mutation_guard.py`**: it must keep passing unchanged, which
is how you know the anchored discovery finds what the old one found.

- [ ] A.1 Add `User.indexed_vault_path` (nullable `String(1024)`) and
      `User.indexed_vault_handle` (nullable `String(320)`) to
      `src/models/db.py`, no server default, each `comment=_INDEXED_ROOT_MARKER`
      where that constant is `"identity of the directory this user's index was
      scanned from (016_indexed_vault_identity)"`. Document on the columns why
      they exist (the `/old → unassigned → /new` transition erases the evidence
      a panel-side comparison would need), that the indexer is their only
      writer, and that `indexed_vault_handle` is an **opaque**
      `"<handle_type>:<hex of f_handle>"` token from `name_to_handle_at` —
      text, compared by byte equality, never parsed, and never fed to
      `open_by_handle_at`. Say on the column why it is not `st_dev:st_ino`:
      inode numbers are reusable, so a directory replaced at the same path
      would compare equal on both signals. 320 characters because a handle is
      at most `MAX_HANDLE_SZ` (128) bytes → 256 hex characters, plus type and
      separator. Keep the string byte-identical to `MARKER` in the migration or
      `alembic check` goes dirty.
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
      `varchar(320)`, default-free **and** marked → accept as a re-run; anything
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
      **before** any discovery. Skip entirely when `user_id is None`. Pin the
      root (A.6a), observe its identity (A.6), read the recorded pair, and take
      exactly one of six branches — the table in the proposal is the
      authority and it is total:
      **root unopenable, or its realpath no longer names the pinned inode** →
      nothing at all: no delete, no record; let the pass fail as it does today;
      **pinned but no handle available** → re-derive (A.7) and record nothing,
      ever, for this root; log it once per process per root at warning level
      and name it as the reason in every re-derive it causes;
      **handle obtained, both signals agree** → nothing;
      **handle obtained, both signals disagree** → in one committed transaction
      `DELETE FROM notes_metadata WHERE user_id = :uid` (embeddings cascade,
      links cascade on `source_note_id` and null out on `target_note_id`) and
      record the new identity, committed before the first file under the new
      root is opened;
      **handle obtained, no record at all, or exactly one signal disagreeing**
      → re-derive (A.7) and record the identity **after** the pass's last
      write, only if it raised nothing **and skipped nothing** (A.7a).
      Log both identities and the deleted row count on the discard path, and
      log the re-derive path with its reason (no handle, no record, or which
      signal disagreed).
- [ ] A.6 Write the identity helper **in this module, as a new function**:
      `os.path.realpath` of the assigned root, plus the **kernel file handle**
      of the pinned descriptor, stored as `f"{handle_type}:{f_handle.hex()}"`.
      Reach `name_to_handle_at` through `ctypes` in the **wrapper-first** shape
      `vault_fs._resolve_renameat2` uses — `ctypes.CDLL(None).name_to_handle_at`
      resolves on glibc 2.39 and 2.41 and has existed since 2.14, so unlike
      `openat2` there is **no** raw-syscall fallback and **no** architecture
      number table; a missing symbol is "handles unavailable", not a guess.
      Declare `struct file_handle` as `{c_uint handle_bytes; c_int handle_type;
      c_ubyte f_handle[MAX_HANDLE_SZ]}` with `MAX_HANDLE_SZ = 128`, call it as
      `(fd, b"", byref(fh), byref(mount_id), AT_EMPTY_PATH)` with
      `AT_EMPTY_PATH = 0x1000`, and read the errno through
      `ctypes.set_errno(0)` / `ctypes.get_errno()` exactly as `_renameat2_raw`
      does. **`EOPNOTSUPP` / `ENOSYS` / a missing symbol are the degraded
      branch, not an error to raise.** **Never call `open_by_handle_at`** — it
      needs `CAP_DAC_READ_SEARCH` and the container has none; the handle is an
      identity to compare, never a door to open. Ignore `mount_id`: it is not
      stable across a remount, and the measurement in the proposal shows the
      handle bytes are identical on the host and inside a bind-mounting
      container whose `mount_id` differs. **Do not reuse or
      modify `transfer.canonical_vault_root`, and do not add `resolve()` to
      it** — the two answer different questions and the first draft conflated
      them; C depends on the unchanged symbol. Use the helper on both sides of
      every comparison and for the value recorded, so a trailing separator, a
      redundant separator or a `.` component is never read as a reassignment.
- [ ] A.6a Pin the assigned root **once per pass**, before the identity is
      observed: `os.open(vault, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)`,
      closed in a `finally`. Take `os.fstat(root_fd)` and the handle from that
      descriptor, and bind the recorded pathname to it the way #59's
      `_require_same_directory` does — `os.stat(os.path.realpath(vault))` must
      report the same `(st_dev, st_ino)` as `os.fstat(root_fd)`, otherwise the
      verdict is **indeterminate**. That is the *only* use of device and inode
      numbers in this design: a within-one-moment check that the name being
      recorded describes the inode being pinned. They are never stored and
      never compared across passes.
- [ ] A.6b Anchor discovery and every vault-file read to that descriptor. Add
      `discover_markdown_files_at(root_fd)` — depth-first, `os.scandir(fd)` per
      directory, descending with `os.open(name, os.O_RDONLY | os.O_DIRECTORY |
      os.O_NOFOLLOW, dir_fd=parent_fd)` (an `ELOOP`/`ENOTDIR` is a symlinked or
      vanished directory: skip it, which is exactly what `rglob` does today),
      skipping any component starting with `.`, closing each parent once its
      children are done so the walk costs one descriptor per level of depth and
      not one per file. Keep `discover_markdown_files(vault)` as a thin wrapper
      that opens, walks and closes, so `tests/test_symlink_mutation_guard.py`
      passes unchanged. Read a note by `os.open(name, os.O_RDONLY,
      dir_fd=parent_fd)` — **no** `O_NOFOLLOW` on the leaf, because a symlinked
      `.md` is read today and this change must not alter what the index
      contains — and take `file_size` / `modified_at` from `os.fstat` on that
      same descriptor, replacing the second pathname resolution
      `full_path.stat()` performs. Apply the same anchoring in `embed_vault`,
      `link_backfill_pass` and `rebuild_tsvectors`: each pins its own root and
      performs the same identity check, because each writes rows the stamp is a
      claim about, and a user whose notes contain no links leaves
      `link_backfill_pass` eligible to run on every startup. When you are done,
      no `Path.read_text()` on a vault-derived path remains in the module.
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
- [ ] A.7a **Account for skips, and withhold the record when there are any.**
      Thread a per-pass skip list through the scan. A skip is: a directory the
      walk could not open (other than a symlinked one, which is a deliberate
      non-descent and not a skip); a file whose open, read, `fstat`, decode or
      frontmatter parse raised — the `UnicodeDecodeError` at `indexer.py:150`
      and the bare `except` at `:153`; a changed path with no buffered body in
      the tsvector loop (`:311`); and any changed note whose links could not be
      extracted. **A non-empty skip list makes the re-derive incomplete: do not
      write the identity, log the first twenty paths and a count of the
      remainder (013's and 015's offender-report shape), and let the next pass
      re-derive again.** Do the repairs you can regardless — an unreadable file
      must not abort the pass. Do **not** "fix" this by transactionally
      deleting the skipped path's rows: that is a second deletion path for
      index contents, and it destroys a row that may be the right row for a
      file that was merely unreadable this second.
- [ ] A.7b **The link rebuild reads no file.** Pass the scan's
      `path_to_content` buffer into `_update_links_for_changed` and extract
      from it instead of re-reading `vault / path` (`indexer.py:399-404`).
      That removes the disappear-between-scan-and-rebuild window rather than
      classifying it, and it drops one full re-read of every changed note. A
      changed path missing from the buffer is an A.7a skip, not a `continue`.
      The buffer already holds the post-frontmatter body, which is exactly what
      `extract_links` consumes. Note the memory shape and say it in the
      docstring: in re-derive mode the buffer holds the whole vault's parsed
      bodies for the duration of the pass, where an ordinary pass holds only
      the changed ones.
- [ ] A.7c **`embed_vault` verifies the hash it is about to certify.**
      `embed_note` sets `note.embedded_content_hash = note.content_hash` — the
      *row's* hash, not a hash of the bytes just embedded — so a file that
      differs from its row at embedding time is embedded and then permanently
      marked as embedded for a hash it does not have. Re-hash the raw text with
      `_content_hash` and skip the note when it does not equal the row's
      `content_hash`; the next pass, having refreshed the row, picks it up.
      This is what makes the re-derive's retention of `note_embeddings`
      load-bearing rather than merely plausible: that branch keeps a vector
      *because* a matching content hash proves it is the right vector.
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
- [ ] A.9 New `tests/test_issue_91_indexed_root.py`, covering all six verdicts
      and both repair shapes:
      **identity** — a retargeted symlink under an unchanged assignment is a
      different directory; two aliases of one directory (a symlink alias and a
      bind-mounted alias, the latter skipped where the harness cannot bind
      mount) never discard; trailing and redundant separators and a `.`
      component are not a reassignment; an assigned root that is missing or
      unopenable deletes nothing and records nothing; a root whose realpath
      stops naming the pinned inode is indeterminate;
      **the reviewer's inode-reuse case, verbatim and as a real filesystem
      operation rather than a mock** — index from a directory, `rmdir` it,
      `mkdir` another at the same path, and loop until `os.stat` reports the
      same `(st_dev, st_ino)` as before (on ext4 this took **one** iteration
      when measured for the proposal; skip rather than hang if it has not
      happened within a few thousand tries). Assert the pass does **not** keep
      the index and does re-derive, and — as the direct unit check on the
      helper — that the recorded and observed handles differ even though
      realpath and `(st_dev, st_ino)` are equal;
      **no handles** — with the handle helper reporting that the filesystem
      cannot produce one, the pass re-derives, records nothing, re-derives again
      on the next pass, never takes the keep branch, and logs the condition once
      per process for that root;
      **anchoring, as the ABA interleaving** — the assignment is a symlink to A;
      the pass is instrumented to retarget it to B after the identity is
      observed and before discovery; assert the pass scans A, not B, and that
      any recorded identity is A's. Then retarget back to A before a second pass
      and assert that no pass ever left B-derived rows recorded as A. Also
      assert `discover_markdown_files` returns exactly what it returned before
      the change for a fixture containing a symlinked directory and a symlinked
      `.md`;
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
      pass re-derives again; a re-derive that completes **and skips nothing**
      records the identity after its last write and the next pass takes the
      no-op branch;
      **completeness** — the reviewer's invalid-UTF-8 case: vault A supplied
      `Same.md`, vault B holds a `Same.md` whose bytes are not valid UTF-8
      (write real invalid bytes; do not mock the decoder). Assert the pass
      records **no** identity, that A's row for that path is still present
      (nothing was invented to replace it), that the log names the path, and
      that the *next* pass re-derives again rather than keeping. A file deleted
      between discovery and its read is a skip with the same consequences; a
      note deleted between the scan and the link rebuild has its links extracted
      from the scan's buffer and is **not** a skip; a pass with an empty skip
      list records the identity;
      **embedding** — `embed_vault` on a file whose bytes no longer hash to the
      row's `content_hash` embeds nothing and leaves `embedded_content_hash`
      alone, and the following pass, after the row is refreshed, embeds it;
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
      replaced directory with a reused inode, a filesystem that returns no
      handle, or a pathname retargeted between the verdict and the scan;
      **whether a re-derive can complete and record an identity while having
      skipped a file** — the round-3 rule is that any per-file skip withholds
      the record, so attack the skip accounting rather than the intent; whether
      the 016/017 marker and refusal logic can adopt a foreign column or
      rewrite a recorded actor on a stamp-back; and whether the #88
      confirmation can be bypassed by a publish path that does not go through a
      `MutableTarget` — naming the permanent unlink as the one that was missed
      the first time.
      **Tell it what each round already fixed, so it checks the replacements
      rather than rediscovering the originals.** Round 1: a provenance backfill
      that stamped `vault_path` onto rows built elsewhere, and a lexical root
      comparison that could not see a retargeted symlink. Round 2's
      replacements were accepted in direction and sharpened three ways: the
      identity was `realpath + st_dev:st_ino`, which a reused inode defeats —
      it is now a kernel file handle, and **a keep requires one**; the identity
      was read from a pathname that was then scanned — the root is now pinned
      as a descriptor and everything runs beneath it; a re-derive could complete
      while the scan `continue`d past unreadable files — any skip now withholds
      the record. Ask whether those three are complete, and whether any of them
      introduced a new way to *destroy* an index that should have been kept.
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
