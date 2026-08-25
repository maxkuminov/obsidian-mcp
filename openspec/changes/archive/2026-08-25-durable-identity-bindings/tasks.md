# Tasks

Three slices, two of which carry a migration. **They are not all disjoint by
file, and the shape below says exactly where they collide and in what order
they must run.** A group that finds itself needing a file outside its scope
must stop and report rather than reach for it — that is a sign the split is
wrong, not a licence to widen it.

## Sequencing

    A (016 + index provenance)  ─┐
                                 ├──►  B (017 + transfer actor)  ──►  D
    C (#88 pre-publish)         ─┘

**The `(A ∥ C) → B → D` shape survives round 4's rescope, and it was re-checked
rather than assumed.** Round 4 only *removed* machinery from A (the handle is
demoted to hardening, the degraded no-handle branch and its warn-once logging
are gone) and *added* two things, both inside `src/services/indexer.py`: the
settled-provenance gate on the unverified ancillary passes (A.6c) and a third
provenance column (A.1–A.4). Neither reaches a file C owns. **Round 5 changed
only the shape of those two additions, not the sequencing:** the pathname
columns become `TEXT` (A.1, A.3, A.8) and `embed_vault` comes back out of the
gate (A.6c, A.7c). Both stay inside A's own files.

**The one new cross-group edge, and why it is safe.** After the rescope A's
assignment fact is the *same* normalisation C compares with, so A **imports and
calls `transfer.canonical_vault_root`** instead of writing a second
`str(Path(path))`. That is a read-only dependency on a symbol C also depends on:
A may call it and **may not modify, move or `resolve()` it**, which is the same
constraint A already carried. There is no import cycle — `src/services/
transfer.py` imports `config`, `models.db`, `oauth.scope`, `vault_fs` and
`vault`, and nothing in the repository imports `indexer` from any of them
(`src/main.py` and `src/control_panel/routes.py` are the only importers of
`indexer`). Verified against the tree, not assumed. If a future edit would make
that a cycle, the resolution is to move `canonical_vault_root` to a module both
can import — **as a separate change**, not inside A.

Round 2's fixes: finding 1's identity redesign and finding 2's re-derive branch
lived entirely in A's files; findings 3 and 4 in C's; finding 5 in B's.

Round 3's surviving fixes are the pinned root descriptor with anchored
discovery and reads (A.6a, A.6b) and the completeness accounting (A.7a); round
4 adds the ancillary-pass gate (A.6c), which round 5 narrows to two passes.
**All of them land in
`src/services/indexer.py`, which only A owns.** The one that had to be checked
carefully is the anchoring: it needs a descriptor-relative directory walk, and
this repository already has an anchored-filesystem module in
`src/services/vault_fs.py`.

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
disjointness claim below is re-stated with A's round-3 and round-4 additions
folded in.

- **A and C may run as parallel worktrees.** Their file scopes are disjoint:
  A owns `alembic/versions/016_*`, `src/services/indexer.py`,
  `src/models/db.py` and `tests/integration/test_schema_check.py`; C owns
  `src/services/vault.py`, `src/mcp_server/tools.py` and its own new test. No
  file appears in both; **neither edits `src/services/transfer.py`** — A only
  *imports* `canonical_vault_root` from it and C only calls it, both as
  unchanged callers; and **neither touches `src/services/vault_fs.py`** — C
  reaches `vault_fs.soft_delete` / `vault_fs.remove` as an unchanged caller,
  and A reaches nothing in it at all.
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
  `str(Path(path))` — A **calls** it and must leave it that way, and C depends
  on it. **Group A additionally confirms that
  `ctypes.CDLL(None).name_to_handle_at` resolves in the container image and
  whether a handle can be read for the vault root**; if it raises
  `EOPNOTSUPP` there, that is not a blocker and not a degraded mode — under the
  rescope an unavailable handle simply removes the hardening, and A must
  exercise that path as an ordinary one. Group C
  additionally confirms that the `atomic-beneath-root-writes` work has landed —
  `src/services/vault.py` must already resolve the mutation parent through a
  single kernel-enforced beneath-root lookup rather than a per-component walk.
  If it has not, stop: C's seam is defined against that shape.

---

## A. The index-provenance record and the reconciling pass (#91, deferred half)

**Files owned:** `alembic/versions/016_indexed_vault_provenance.py` (new),
`src/models/db.py`, `src/services/indexer.py`,
`tests/integration/test_schema_check.py`,
`tests/test_issue_91_indexed_root.py` (new).

**Do not touch:** `src/control_panel/users.py` or any panel template. The
record is written **only** by the index pass. A panel handler that changes
`vault_path` leaves it alone, and that asymmetry is what makes the record mean
"what the rows were scanned under" rather than "what the assignment is". Do not
add age-based pruning; it is a rejected alternative, not a stretch goal. Do not
delete `notes_metadata` anywhere except inside the reconciliation. **Do not
edit `src/services/transfer.py`** — A *imports and calls*
`canonical_vault_root` (see A.6) and may not change, move or `resolve()` it; C
depends on the symbol exactly as it stands. **Do not try to detect filesystem
substitution behind an unchanged assignment** — it is a declared non-goal, and
adding a proxy for it (content overlap, path overlap, a mount identifier, a
filesystem UUID) reintroduces the heuristic three review rounds rejected. **Do not touch `src/services/vault_fs.py`** —
the indexer's anchored walk is A's own and read-only; see Sequencing for why
that is a design decision rather than an ownership one. **Do not edit
`tests/test_symlink_mutation_guard.py`**: it must keep passing unchanged, which
is how you know the anchored discovery finds what the old one found.

- [x] A.1 Add three columns to `src/models/db.py` — `User.indexed_vault_assignment`
      (nullable `Text`), `User.indexed_vault_realpath` (nullable `Text`) and
      `User.indexed_vault_handle` (nullable `String(320)`) —
      no server default, each `comment=_INDEXED_PROVENANCE_MARKER` where that
      constant is `"provenance of this user's index, recorded by the index pass
      (016_indexed_vault_provenance)"`. Document on the columns what each one is
      for and, more importantly, what it is **not**: `indexed_vault_assignment`
      is the canonical assignment string (`transfer.canonical_vault_root`'s
      form) and is the fact the keep/discard decision turns on; it exists
      because the `/old → unassigned → /new` transition erases the evidence a
      panel-side comparison would need. `indexed_vault_realpath` is the
      `os.path.realpath` of the directory that assignment named when the pass
      ran, and its only job is to keep a cosmetic rename or an alias from
      costing a full re-embed — **it is not a proof of directory identity**.
      **It stores `os.fsencode(realpath).hex()`, not the pathname as text**, and
      is compared encode-then-compare: reduce the newly observed real path to
      the same hex and compare the two strings; never decode the stored value in
      order to compare it. Decode (`os.fsdecode(bytes.fromhex(...))`) only to
      render it in a log. Say why on the column, because it reads like
      gratuitous obfuscation otherwise: a POSIX pathname is arbitrary non-NUL
      bytes, Python decodes a non-UTF-8 component with `surrogateescape`, so
      `os.path.realpath` can return a lone surrogate like `'\udcff'` that
      asyncpg cannot UTF-8-encode — and the discard writes the record *and* the
      delete in one transaction, so that encode failure rolls the delete back on
      every later pass and serves the former vault forever. Hex has no
      unrepresentable input, so the column is total over the fact by
      construction rather than by a bound. Hex and not base64: the handle column
      already spells opaque bytes as hex, base64 has variant alphabets and
      optional padding so one value gets two spellings under a byte-equality
      comparison, and the 2x length is exactly what `Text` is here to absorb.
      **The two pathname columns are `Text`, not a width, and that is
      correctness rather than taste.** The rule is that a provenance column must
      be able to record any value the fact it mirrors can take: a value the pass
      observed and cannot store is a bug, never a truncation and never a NULL.
      A short assignment may be a symlink to a canonical path of any length, and
      the discard branch writes the record *and* the delete in one transaction —
      so an oversized value raises `string_data_right_truncation`, rolls the
      delete back with it, and leaves the former vault's index served on every
      later pass, which is #91's own symptom caused by a column width. Give
      `indexed_vault_assignment` `Text` too even though `String(1024)` matches
      `users.vault_path` today: that sufficiency is a property of another
      column's DDL and of the current normaliser, not of this record, and the two
      pathname facts are written and read as one unit. Do **not** add a
      length check, a truncation, or a NULL-on-oversize rule to either of them.
      **`indexed_vault_assignment` is stored as a plain pathname and must NOT be
      hex-encoded.** Its value is `str(Path(users.vault_path))` — lexical only,
      reading no directory, introducing no non-ASCII character its input lacked
      — over a value the database itself handed back, and a UTF-8 database
      cannot be holding bytes it would refuse to accept, so the totality rule is
      already satisfied there without an encoding. The environment-derived
      `settings.vault_path` never reaches this column, because A.5 skips the
      classification entirely for `user_id is None`. Encoding it too would only
      make the fact an operator reads in the discard log unreadable.
      `indexed_vault_handle` is an **opaque** `"<handle_type>:<hex of
      f_handle>"` token from `name_to_handle_at`, text, compared by byte
      equality, never parsed, never fed to `open_by_handle_at` — and it is
      **best-effort hardening in the refusing direction only**: a mismatch
      demotes a keep to a re-derive, a match grants nothing, and NULL means "no
      hardening signal", never "provenance unknown". Say on the columns that the
      indexer is their only writer and that every stamp writes **all three**,
      NULL for anything the pass could not observe. 320 characters because a
      handle is at most `MAX_HANDLE_SZ` (128) bytes → 256 hex characters, plus
      type and separator — sufficient for the declared ext4/xfs filesystems and
      for NFSv4's 128-byte maximum, and *not* claimed as an eternal bound; a
      handle that would not fit is recorded as NULL, never truncated — the one
      column the totality rule above does not govern, because a handle is a
      *comparison token* whose absence is a defined state (no hardening signal)
      while a missing pathname is not a state at all but a half-set record. Keep
      the marker string byte-identical to `MARKER` in the migration or
      `alembic check` goes dirty.
- [x] A.2 Write `alembic/versions/016_indexed_vault_provenance.py`
      (`down_revision = "015"`). `SET LOCAL lock_timeout` /
      `statement_timeout` and `RESET` both at the end of `upgrade()` — `SET
      LOCAL` lasts for the transaction and `alembic/env.py` runs every pending
      revision in one, so a later revision would otherwise inherit them (013
      and 014 do the same, for the same reason). Add the three columns and stamp
      the COMMENT marker on each. **Backfill nothing.** Record in the
      docstring why: `vault_path` says what is assigned now, not what the rows
      were built under, and stamping it would make the never-heals link case
      guaranteed rather than possible; a NULL record is the only true statement
      available, and the pass repairs a NULL user by re-deriving rather than
      discarding, so introducing the columns costs no vault-wide re-embed.
      Record the deploy-order argument too: 016 writes no provenance, so an old
      container's in-flight pass has nothing to contradict, and that — not any
      lock — is why the deploy is safe.
- [x] A.3 Treat pre-existing columns of any of the three names as an ownership
      question, not a convenience, and treat the three as **one unit**: all
      absent → create and mark; all present, nullable, exactly `text` / `text` /
      `varchar(320)`, default-free **and** marked → accept as
      a re-run; anything else (any one wrong-typed, `NOT NULL`, defaulted or
      unmarked, or a partial set of one or two) → raise naming what was found,
      changing nothing. The marker is what makes this stronger than 015's case:
      the record is the sole input to a decision that deletes a user's whole
      index, so adopting a foreign column is a mass delete on a value nobody in
      this scheme wrote.
- [x] A.4 `downgrade()` drops the columns **only** if they carry the marker,
      all-or-nothing.
- [x] A.5 In `src/services/indexer.py`, add the classification at the head of
      `index_vault(user_id)`, after `vault = _vault_root(user_id)` and
      **before** any discovery. Skip entirely when `user_id is None`. Pin the
      root (A.6a), observe the three facts (A.6), read the recorded three, and
      take exactly one of six branches — the table in the proposal is the
      authority and it is total:
      **root unopenable, or its realpath no longer names the pinned inode** →
      nothing at all: no delete, no record; let the pass fail as it does today;
      **no record at all** → re-derive (A.7) and stamp at the end if the pass
      raised nothing **and skipped nothing** (A.7a);
      **assignment equal and realpath equal, with no observable handle
      mismatch** → nothing;
      **assignment equal and realpath equal, but a handle was recorded, a
      handle was read now, and they differ** → re-derive (A.7), stamp at the end
      under the same condition;
      **assignment differs and realpath differs** → in one committed transaction
      `DELETE FROM notes_metadata WHERE user_id = :uid` (embeddings cascade,
      links cascade on `source_note_id` and null out on `target_note_id`) and
      stamp the new provenance, committed before the first file under the new
      root is opened;
      **exactly one of assignment and realpath differs** → re-derive (A.7),
      stamp at the end under the same condition.
      A "handle mismatch is observable" only when a handle is recorded **and**
      one was read now; either being absent means there is nothing to observe,
      **not** a degraded mode. Log both provenances and the deleted row count on
      the discard path, and log the re-derive path with its reason (no record,
      which fact disagreed, or a contradicting handle).
      **Write the stamp as one UPDATE of all three columns**, NULL for anything
      not observed, so no later observation can be compared against a root the
      stamp did not describe.
- [x] A.6 Write the provenance helper **in this module, as a new function**:
      the canonical assignment string, the `os.path.realpath` of the assigned
      root **encoded as `os.fsencode(...).hex()`** (A.1 — the raw string can
      carry a surrogate escape the driver cannot encode, and this is the one
      write that must never roll back), and — where available — the **kernel
      file handle** of the pinned descriptor as
      `f"{handle_type}:{f_handle.hex()}"`. Both realpath comparisons in A.5 run
      on the encoded form, on both sides. **Take the assignment
      string from `transfer.canonical_vault_root`**: import and call it, do not
      re-implement `str(Path(path))` and do not change, move or `resolve()` it.
      One normaliser for "the same assignment" is the point — C compares with
      the same function, and two copies is how the index's notion of it and the
      write path's notion of it drift apart. Reach `name_to_handle_at` through
      `ctypes` in the **wrapper-first** shape `vault_fs._resolve_renameat2`
      uses — `ctypes.CDLL(None).name_to_handle_at` resolves on glibc 2.39 and
      2.41 and has existed since 2.14, so unlike `openat2` there is **no**
      raw-syscall fallback and **no** architecture number table; a missing
      symbol is "no handle available", not a guess.
      Declare `struct file_handle` as `{c_uint handle_bytes; c_int handle_type;
      c_ubyte f_handle[MAX_HANDLE_SZ]}` with `MAX_HANDLE_SZ = 128`, call it as
      `(fd, b"", byref(fh), byref(mount_id), AT_EMPTY_PATH)` with
      `AT_EMPTY_PATH = 0x1000`, and read the errno through
      `ctypes.set_errno(0)` / `ctypes.get_errno()` exactly as `_renameat2_raw`
      does. **`EOPNOTSUPP` / `ENOSYS` / a missing symbol / a payload that will
      not fit the column are all simply "no handle": return None, record NULL,
      log nothing, change no verdict.** **Never call `open_by_handle_at`** — it
      needs `CAP_DAC_READ_SEARCH` and the container has none; the handle is a
      value to compare, never a door to open. Ignore `mount_id`: it is not
      stable across a remount, and the measurement in the proposal shows the
      handle bytes are identical on the host and inside a bind-mounting
      container whose `mount_id` differs.
- [x] A.6a Pin the assigned root **once per pass**, before the facts are
      observed: `os.open(vault, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)`,
      closed in a `finally`. Take `os.fstat(root_fd)` and the handle from that
      descriptor, and bind the recorded realpath to it the way #59's
      `_require_same_directory` does — `os.stat(os.path.realpath(vault))` must
      report the same `(st_dev, st_ino)` as `os.fstat(root_fd)`, otherwise the
      verdict is **indeterminate**. That is the *only* use of device and inode
      numbers in this design: a within-one-moment check that the realpath being
      recorded describes the inode being pinned. They are never stored and
      never compared across passes. Say in the docstring what the pin does and
      does not buy: within one pass the facts observed, the files discovered and
      the bytes read all come from one inode — it does **not** prove the pinned
      directory is the one earlier rows came from, and nothing does.
- [x] A.6b Anchor discovery and every vault-file read to that descriptor. Add
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
      `link_backfill_pass` and `rebuild_tsvectors`: each pins its own root, for
      the same within-pass-consistency reason. When you are done, no
      `Path.read_text()` on a vault-derived path remains in the module.
- [x] A.6c **Gate `link_backfill_pass` and `rebuild_tsvectors` on settled
      provenance, per user — and leave `embed_vault` out of the gate.** Both
      gated passes write rows the provenance is a claim about — `note_links`,
      `content_tsvector` — with no verification that the bytes they read belong
      to the row they write against, and neither may assume the scan settled
      that claim a moment ago: a user whose notes contain no links leaves
      `link_backfill_pass` eligible on *every* startup, and a reassignment can
      commit between the scan and either of them. So each of them, **for each
      user**, runs the same classification helper A.5 uses and proceeds only on
      the **same assignment** verdict; on anything else it skips **that user**,
      logs once, and writes nothing for them. The skip must be per user — one
      unsettled user must not stop the pass for everybody else — and the
      classification must come from the one function, not a re-implementation.
      Do **not** substitute a per-file content check to let them write anyway:
      `link_backfill_pass` cannot be fixed that way at all (resolution is a
      function of the whole note set, not one file's bytes), `rebuild_tsvectors`
      records nothing a later pass could use to notice a foreign vector, and the
      re-derive redoes both their jobs on every pass, so the delay costs nothing.
      **`embed_vault` must NOT be gated**, and this is a deliberate reversal of
      the round-4 draft: the gate composes with A.7a's completeness rule into
      indefinite staleness, because one permanently unreadable file withholds
      the stamp forever while a *readable* changed note keeps serving the chunk
      text of content it no longer has (`semantic_search` reads `chunk_text`
      with no `embedded_content_hash = content_hash` guard —
      `embeddings.py:353`). It is safe ungated for a reason that is specific to
      it: A.7c makes it hash-verify every note it certifies, so it can only ever
      write a vector against a row whose recorded content the bytes actually
      hash to. If you find yourself removing A.7c, you must re-gate this pass in
      the same change.
- [x] A.7 Add the re-derive mode to `index_vault`: content-hash change
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
- [x] A.7a **Account for skips, and withhold the record when there are any.**
      Thread a per-pass skip list through the scan. A skip is: a directory the
      walk could not open (other than a symlinked one, which is a deliberate
      non-descent and not a skip); a file whose open, read, `fstat`, decode or
      frontmatter parse raised — the `UnicodeDecodeError` at `indexer.py:150`
      and the bare `except` at `:153`; a changed path with no buffered body in
      the tsvector loop (`:311`); and any changed note whose links could not be
      extracted. **A non-empty skip list makes the re-derive incomplete: do not
      write the provenance stamp, log the first twenty paths and a count of the
      remainder (013's and 015's offender-report shape), and let the next pass
      re-derive again.** Do the repairs you can regardless — an unreadable file
      must not abort the pass. Do **not** "fix" this by transactionally
      deleting the skipped path's rows: that is a second deletion path for
      index contents, and it destroys a row that may be the right row for a
      file that was merely unreadable this second.
- [x] A.7b **The link rebuild reads no file.** Pass the scan's
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
- [x] A.7c **`embed_vault` verifies the hash it is about to certify.**
      `embed_note` sets `note.embedded_content_hash = note.content_hash` — the
      *row's* hash, not a hash of the bytes just embedded — so a file that
      differs from its row at embedding time is embedded and then permanently
      marked as embedded for a hash it does not have. Re-hash the raw text with
      `_content_hash` and skip the note when it does not equal the row's
      `content_hash`; the next pass, having refreshed the row, picks it up.
      This check does two jobs, and both are load-bearing. It is what makes the
      re-derive's retention of `note_embeddings` sound: that branch keeps a
      vector *because* a matching content hash proves it is the right vector.
      And it is the entire licence for A.6c leaving `embed_vault` ungated — an
      embedding is a pure function of content, so refusing bytes that do not
      hash to the selected row's `content_hash` means the vector and the row
      describe the same content whatever directory supplied the bytes; under a
      wrong root the hashes disagree and the pass skips. Say that in the
      docstring, next to the check, so the coupling is visible at the site of any
      future removal.
- [x] A.8 `tests/integration/test_schema_check.py`: set `HEAD_REVISION = "016"`
      (B moves it to `"017"`); a fresh database has all three columns nullable,
      exactly typed (`text` / `text` / `varchar(320)`), default-free and marked;
      a value longer than `users.vault_path`'s own width round-trips through
      `indexed_vault_realpath` unchanged (assert the stored value equals what was
      written — that column must not be re-bounded by a later edit); **an
      encoded real path whose bytes are not valid UTF-8 round-trips losslessly**
      — build it from a real directory whose name carries a non-UTF-8 byte
      (`os.mkdir` on a `bytes` name containing `\xff`), take `os.path.realpath`
      of it, write `os.fsencode(rp).hex()`, and assert the write succeeds and
      `os.fsdecode(bytes.fromhex(stored)) == rp`; assert too that writing the
      *raw* `rp` to that column raises, which is what keeps the encoding
      load-bearing rather than decorative;
      **016 backfills nothing** — after it, all three
      columns are NULL for every user including every assigned one, and no
      `notes_metadata` / `note_embeddings` / `note_links` row changed;
      `alembic stamp 015` then `upgrade head` after the indexer has recorded a
      provenance whose assignment differs from `vault_path` changes nothing; a
      foreign same-named column is refused for each of wrong type, `NOT NULL`,
      server default and missing marker, and a partial set of one and of two
      columns is refused, with the schema unchanged; a complete marked set of
      three is accepted; downgrade to 015 drops a marked set and leaves a set
      with any column unmarked; `alembic check` clean at head.
- [x] A.9 New `tests/test_issue_91_indexed_root.py`, covering all six verdicts,
      both repair shapes, the hardening's one direction, and the declared
      non-goal:
      **assignment-level classification** — reassignment to a different vault at
      a different real path discards; reassignment to an alias of the same
      directory (a symlink alias, and a bind-mounted alias where the harness can
      make one) re-derives and never discards; trailing and redundant separators
      and a `.` component compare equal through the shared normaliser and keep;
      an assigned root that is missing or unopenable deletes nothing and records
      nothing; a root whose realpath stops naming the pinned inode is
      indeterminate; a retargeted symlink under an unchanged assignment
      re-derives (assignment equal, realpath differs) and does **not** discard;
      **an over-long realpath still discards** — reassign a user to a *short*
      assignment that is a symlink to a directory whose canonical realpath
      exceeds `users.vault_path`'s width (build the depth in the tmpdir; do not
      mock `os.path.realpath`), and assert the discard commits, the rows are
      gone, and the recorded realpath equals the observed one in full. This is
      the round-4 blocker: with a bounded column the transaction raises
      `string_data_right_truncation`, rolls the delete back, and the former
      vault stays queryable forever;
      **a realpath with a non-UTF-8 component still discards** — the round-5
      blocker, and it must use a **real** directory rather than a mock: create
      the new vault beneath a directory whose name is a `bytes` path containing
      `\xff`, reassign the user to it, and assert that the pass deletes the
      user's `notes_metadata` rows **and** stamps all three provenance columns
      in one committed transaction; that no `UnicodeEncodeError` escapes; that
      the stored realpath decodes back to `os.path.realpath` of that directory
      exactly, surrogates included; and that the *next* pass over the same root
      classifies it **same assignment** rather than re-deriving, which is what
      proves the comparison runs on the encoded form on both sides. Skip only if
      the test filesystem refuses the byte name, and say so — do not substitute a
      mocked `os.path.realpath`, because the defect is in what the kernel can
      hand back;
      **the shared normaliser** — assert the assignment fact is produced by
      `transfer.canonical_vault_root` itself (patch it and observe both the
      index record and the pre-publish confirmation change together), so a
      second copy cannot be introduced without failing a test;
      **the handle, in its one direction, as a real filesystem operation rather
      than a mock** — index from a directory, `rmdir` it, `mkdir` another at the
      same path, and loop until `os.stat` reports the same `(st_dev, st_ino)` as
      before (on ext4 this took **one** iteration when measured for the
      proposal; skip rather than hang if it has not happened within a few
      thousand tries). Assert the recorded and observed handles differ while
      the assignment and realpath are equal, and that the pass therefore
      **re-derives instead of keeping**. Then assert the other direction
      explicitly: a matching handle with a differing assignment or realpath
      **never** produces a keep;
      **no handle available** — with the handle helper reporting that the
      filesystem cannot produce one, the pass reaches exactly the verdict it
      would reach with handles available on the other two facts, records NULL in
      the handle column, re-derives no more often than before, and **logs no
      degraded-mode warning**. Assert both the keep case and the discard case
      under that condition;
      **the stamp is whole** — after a stamp taken with no handle available, the
      handle column is NULL rather than retaining a previously recorded value;
      and a later handle-capable pass over that same root takes the keep branch
      (no recorded handle ⇒ no observable mismatch) rather than discarding;
      **the declared non-goal** — a substituted root behind an unchanged
      assignment whose handle also matches is **kept**, and the test asserts the
      documented consequence rather than a prevention: the ordinary scan prunes
      the paths the substitute lacks, and a note identical by path and content
      hash keeps a dangling link. Mark it as the non-goal it is, so a future
      reader does not "fix" the test;
      **anchoring, as the ABA interleaving within one pass** — the assignment is
      a symlink to A; the pass is instrumented to retarget it to B after the
      facts are observed and before discovery; assert the pass scans A, not B,
      and that any recorded provenance describes A. Also assert
      `discover_markdown_files` returns exactly what it returned before the
      change for a fixture containing a symlinked directory and a symlinked
      `.md`;
      **discard** — removes `notes_metadata`, `note_embeddings` **and**
      `note_links` for that user and nothing for any other user; the delete and
      the stamp are one transaction (a failure between them leaves neither
      applied); the stamp lands before any file under the new root is read; a
      pass that fails after committing the discard is retried by the next pass
      without a second delete;
      **re-derive** — a NULL record re-derives instead of discarding or
      trusting; the reviewer's case, verbatim: vault A holds `Same.md` linking
      to `OnlyA.md`, vault B holds a byte-identical `Same.md` and no
      `OnlyA.md`, the user is indexed on A, reassigned to B, and the first pass
      after the upgrade must leave `Same.md`'s link rows re-extracted from B
      and resolved against B alone, with `OnlyA.md`'s row gone; a note whose
      content hash still matches keeps its `note_embeddings` rows and triggers
      no embedding call; a re-derive that raises stamps nothing and the next
      pass re-derives again; a re-derive that completes **and skips nothing**
      stamps all three columns after its last write and the next pass takes the
      no-op branch;
      **completeness** — the reviewer's invalid-UTF-8 case: vault A supplied
      `Same.md`, vault B holds a `Same.md` whose bytes are not valid UTF-8
      (write real invalid bytes; do not mock the decoder). Assert the pass
      stamps **nothing**, that A's row for that path is still present (nothing
      was invented to replace it), that the log names the path, and that the
      *next* pass re-derives again rather than keeping. A file deleted between
      discovery and its read is a skip with the same consequences; a note
      deleted between the scan and the link rebuild has its links extracted
      from the scan's buffer and is **not** a skip; a pass with an empty skip
      list stamps;
      **the gated ancillary passes** — for a user with no recorded provenance,
      and for a user reassigned after the scan stamped, each of
      `link_backfill_pass` and `rebuild_tsvectors` writes **no** row for that
      user and logs once; in the same call a second user whose provenance is
      settled has their work done normally (assert the skip is per user, not
      global); and round 3's failing input specifically — a user reassigned from
      A to B between `index_vault` and `link_backfill_pass`, where both vaults
      hold `Same.md` with different link targets — leaves `note_links`
      unwritten for that user rather than committing a B-derived target against
      an A-derived row;
      **embedding, which is deliberately *not* gated** — `embed_vault` on a file
      whose bytes no longer hash to the row's `content_hash` embeds nothing and
      leaves `embedded_content_hash` alone, and the following pass, after the row
      is refreshed, embeds it; and round 4's failing input: a vault holding one
      permanently unreadable note (real invalid bytes) **and** one readable note
      whose content is then changed — assert that no provenance is ever recorded
      for that user *and* that the changed note's `note_embeddings` are updated
      on the next pass, so a single bad file cannot freeze a readable note's
      vectors at content it no longer has. Assert the same for a user under a
      **reassigned** and under a **provenance unknown** classification: the pass
      processes them rather than skipping, and every note it embeds hashed to
      the row it was written against;
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

- [x] B.1 Extract the single reader of `current_actor` — the mapping from the
      ContextVar triple to `{actor_kind, actor_label, actor_ref}` **including
      the truncation to the stored widths** — out of
      `src/mcp_server/tools.py::_actor_columns` into `src/auth/session.py`,
      beside the ContextVar it reads. Leave `_actor_columns` as a thin
      delegation so the tool-call log path is unchanged. One reader is the
      point: the widths are identical on both tables and a second copy is how
      the mint and the log start truncating differently.
- [x] B.2 Add `TransferToken.actor_kind` / `actor_label` / `actor_ref` to
      `src/models/db.py` — nullable `String(20)` / `String(255)` / `String(64)`,
      no server default, each `comment=TransferToken._ACTOR_COLUMN_MARKER`
      where that constant is `"denormalised actor, recorded at mint
      (017_transfer_token_actor)"`. Document that the redemption request
      carries a capability rather than a credential, which is why the label has
      to be recorded at mint.
- [x] B.3 In `mint_token` (`src/services/transfer.py`), populate the three
      fields from the shared reader **inside the mint transaction**, before the
      INSERT. Read the ContextVar there rather than taking a parameter — the
      `plan_mint_window` discipline: a caller-supplied value is one the caller
      can get stale or wrong. An unset ContextVar leaves all three NULL and the
      row keeps its pre-017 shape.
- [x] B.4 In `src/transfer/routes.py::_log_row`, copy `row.actor_kind`,
      `row.actor_label` and `row.actor_ref` onto the `UsageLog`. Nothing else
      about that function changes; `params` still never contains the token.
- [x] B.5 Write `alembic/versions/017_transfer_token_actor.py`
      (`down_revision = "016"`), same `SET LOCAL` / `RESET` discipline as 016.
      Add the three columns, stamp each with the marker, and backfill guarded
      on `actor_kind IS NULL`: from `api_keys` for rows with `key_id`, and from
      `oauth_tokens` → `oauth_clients` for rows with `oauth_token_id`. Because
      both FKs are `ON DELETE CASCADE`, a row whose credential is gone does not
      exist to label — the rows left NULL are the ones carrying neither FK, and
      they stay NULL rather than being inferred from `user_id`.
- [x] B.5a **Port 015's `_assert_no_orphan_labels` to `transfer_tokens` and run
      it before the backfill.** The first draft of this proposal claimed to
      follow 015 exactly and omitted this; it is the invariant that makes the
      `actor_kind IS NULL` guard safe on a stamp-back re-run. Same shape as
      015: select up to 20 offending ids where `actor_kind IS NULL AND
      (actor_label IS NOT NULL OR actor_ref IS NOT NULL)`, count the rest,
      raise naming them, and change nothing. Without it a re-run relabels such
      a row from whatever credential its FK points at *now*, rewriting a
      recorded attribution — the one thing these columns must never do.
- [x] B.6 Treat the three as one owned unit, exactly as 015 does: all absent →
      create and mark; all present, exactly typed, nullable, default-free and
      marked → accept as a re-run; anything else (partial set, `NOT NULL`,
      server default, unmarked, foreign) → raise naming what was found.
      `downgrade()` drops only marked columns, all-or-nothing.
- [x] B.7 017 writes **nothing** to `usage_logs`. Record why in the migration's
      docstring: there is no reference from a usage row back to the token that
      produced it, and the only alternative is re-running 015's credential join
      — a second writer on columns 015 owns and guards, which is the second
      resolution path #64 argued against. Rows in the 015→017 gap keep
      join-only attribution and render honestly.
- [x] B.8 `tests/integration/test_schema_check.py`: move `HEAD_REVISION` to
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
- [x] B.9 New `tests/test_issue_92_transfer_actor.py`: a mint records the actor
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
here would reintroduce the check-then-act #59 removed. **Group A now calls the
same function** — after round 4's rescope the index record's assignment fact is
this same normalisation — so `canonical_vault_root` has two read-only callers
and no editor: neither group may change, move or `resolve()` it, and C's import
path is unchanged.

- [x] C.1 Add an async confirmation in `src/services/vault.py`: one
      `SELECT users.vault_path, users.is_active WHERE id = :uid` on its own
      short-lived session, canonicalised and compared against the root this
      request bound in `current_vault_root`. Refuse on any of: differs, now
      NULL, row absent, `is_active` false. Return the confirmation rather than
      a bare boolean, so the caller has something to stamp with. No-op —
      issuing no query at all — when `user_id is None` outside multi-user mode.
- [x] C.2 Stamp the confirmation onto the `MutableTarget` the *next* publishing
      operation will go through, and make the shared publish helpers
      (`_atomic_write_at`, `move_file_no_clobber`, `vault`'s `soft_delete_at`
      call, **and the new permanent-unlink helper of C.2a**) **refuse a target
      carrying no confirmation for the operation about to be performed**. That
      refusal is a programming error and must raise distinguishably from the
      operational refusal in C.1 — a reviewer, and a log reader, must be able to
      tell "somebody added a tool and forgot" from "an admin reassigned
      mid-call".
- [x] C.2a **Add a `MutableTarget`-based permanent-unlink helper to
      `src/services/vault.py` and route `delete_note(permanent=True)` through
      it**, replacing the bare `os.unlink(target.name, dir_fd=target.dir_fd)` at
      `src/mcp_server/tools.py:1969`. It is the only bare mutating syscall left
      on the target path, and while it stands the "publish helpers refuse an
      unstamped target" claim is false rather than merely incomplete. The helper
      keeps the existing semantics — unlink through the parent descriptor, no
      symlink following — and adds only the stamp check.
- [x] C.3 Take the confirmation **immediately before each publishing
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
- [x] C.3a On a rewrite refusal, **stop the loop** — every remaining rewrite
      would write into a vault the caller no longer holds, through descriptors
      pinned before the reassignment — and report the partial outcome: the move
      completed in the previous root, the assignment changed mid-call, and these
      sources were left unrewritten. Carry it through the existing
      `failed_rewrite_sources` / `_rewrite_failure_warning` idiom with a
      distinct reason rather than inventing a second reporting mechanism. Do not
      roll the move back and do not undo the metadata update: the note really is
      at its new path and the rows must keep saying so.
- [x] C.4 Wire the six tools in `src/mcp_server/tools.py` — `create_note`,
      `edit_note` (every mode, `dry_run` included: a dry run publishes nothing
      and therefore needs no confirmation, but it must not be the reason a
      later mode skips one), `move_note`, `delete_note`, `set_frontmatter`,
      `write_file`. Confirm the placement against `_leaf_state_error` and the
      existing no-clobber refusal so the ordering of error messages stays
      sensible: a symlinked leaf is still named as one rather than reported as
      a reassignment.
- [x] C.5 Give `delete_file_impl` its own confirmation before
      `vault_fs.soft_delete` / `vault_fs.remove`. It resolves through
      `_vault_context` and walks from its own `vault_fs.open_root(root)`, so
      the target stamp does not reach it. Refuse before `check_trash_support`
      creates anything.
- [x] C.6 Record the refusal in `usage_logs.params` as an error marker naming a
      changed vault assignment, distinct from the admission gate's
      `no_vault_assigned`, and with no other new field. Use the request-scoped
      params holder `_tracked` already merges (`src/services/timing.py`);
      `_tracked` remains the only thing that calls `begin()`/`clear()`.
- [x] C.7 Document the residual in the tool's own error text and in the
      docstrings: the confirmation narrows the window to staging, flush and one
      publishing call — it does not close it, and a reassignment committing
      inside the publish still lands in the former root. Say it at the same
      level as `edit_note(expected=…)`.
- [x] C.8 New `tests/test_issue_88_root_confirmed_before_publish.py`:
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
- [x] C.9 A registry-shaped test in the same module, in the idiom of
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

- [x] D.1 `pytest --ignore=tests/integration` green
- [x] D.2 `make test-schema` green with `HEAD_REVISION = "017"` — required, this
      change carries two migrations
- [x] D.3 `openspec validate durable-identity-bindings --strict`
- [x] D.4 `make audit`
- [x] D.5 Adversarial Codex pass, framed as a defensive control review. Both
      triggers fire: two migrations, and a refusal on the write path. Give it
      the product framing — the consumer is an agent, the expensive failures
      are destructive writes and silently wrong search results.
      **Give it the rescope first, because round 4 narrowed the claim rather
      than hardening it, and a reviewer who does not know that will re-file
      round 3.** The claim is now *assignment-level*: the record answers "did
      the assignment change?", the file handle is best-effort hardening that
      may only downgrade a keep to a re-derive and may never establish
      anything, and **filesystem substitution behind an unchanged assignment is
      an explicit non-goal** with its reasoning in the proposal. A finding that
      the system fails to detect a cloned image, a remount, or a symlink
      retarget-and-restore is *in scope only if it argues the boundary itself is
      wrong* — that the harm is reachable through a supported path, or that the
      declared mitigation (the ordinary scan reconciling by path and hash) does
      not hold — not merely that the case exists.
      Point it at what the rescope must still get right: whether the
      reconciliation can ever delete an index it should have kept **or keep one
      it should have re-derived** *for an assignment-level reason*; whether the
      handle can, on any path, upgrade a verdict rather than only refuse one;
      whether a missing handle changes any verdict it should not; whether the
      three-columns-written-as-one stamp rule really removes every state in
      which a later observation compares against a root the stamp did not
      describe; whether the re-derive branch leaves every surviving row and link
      derived from the assigned root, and whether keeping `note_embeddings` on a
      matching `content_hash` is sound; **whether a re-derive can complete and
      stamp while having skipped a file** — any per-file skip must withhold the
      stamp, so attack the skip accounting rather than the intent; whether the
      ancillary-pass gate can be bypassed, or is global where it should be per
      user; whether the 016/017 marker and refusal logic can adopt a foreign
      column or rewrite a recorded actor on a stamp-back; and whether the #88
      confirmation can be bypassed by a publish path that does not go through a
      `MutableTarget` — naming the permanent unlink as the one that was missed
      the first time.
      **Tell it what each round already fixed, so it checks the replacements
      rather than rediscovering the originals.** Round 1: a provenance backfill
      that stamped `vault_path` onto rows built elsewhere, and a lexical
      comparison presented as directory identity. Round 2: `realpath +
      st_dev:st_ino` as identity, which a reused inode defeats; an identity read
      from a pathname that was then scanned; a re-derive that could complete
      while the scan `continue`d past unreadable files. Round 3: the handle as
      *proof*, which a cloned filesystem defeats; ancillary passes with no
      defined behaviour under unresolved provenance; a no-stamp branch that left
      a previous root's record standing. Round 4 keeps round 2's pinned root and
      round 3's skip accounting, replaces the identity claim with the
      assignment claim, demotes the handle, adds the per-user ancillary gate,
      and makes every stamp write all three columns. Round 4's own review
      accepted that rescope and attacked its mechanics instead, producing two
      fixes: the two pathname columns became `TEXT`, because a bounded one could
      make the discard-and-stamp transaction roll back forever; and `embed_vault`
      came back *out* of the ancillary gate, because gating it composed with the
      completeness rule into indefinite semantic staleness — it is safe ungated
      only because it hash-verifies what it certifies, and the spec binds those
      two halves into one requirement. Round 5 asked this brief's own third
      question and found the answer was still yes: `TEXT` bounds a value's
      *length* and not its *bytes*, and `os.path.realpath` can hand back a
      surrogate-escaped string for a non-UTF-8 pathname component that the
      driver cannot encode — the same indefinite rollback through a different
      channel. `indexed_vault_realpath` therefore stores
      `os.fsencode(realpath).hex()` and is compared encode-then-compare, which
      makes the column total over the fact by construction rather than by a
      bound; the assignment column is untouched, because its value comes from
      `users.vault_path` through a lexical normaliser and a UTF-8 database
      cannot hold bytes it would refuse to accept back. Ask whether *those*
      replacements are complete; whether any of them introduced a new way to
      **destroy** an index that should have been kept; whether any other write
      on the provenance path can still fail on a value the pass legitimately
      observed — by length, by encoding, or by any third property of the value;
      and whether the un-gated `embed_vault` can, on any path, write a vector
      against a row whose content it did not verify.
- [x] D.6 `openspec-verifier` subagent against this proposal and the spec deltas
- [x] D.7 Deploy: `make deploy`, then `make db-check` must report "No new
      upgrade operations detected"
- [x] D.8 In place of the `user-representative` browser pass (there is no
      browser UI on the MCP side), exercise the affected tools against the live
      server and name in the report which were actually called: `create_note`
      and `edit_note` (the confirmed publish path, unchanged assignment),
      `request_upload` followed by a redemption and then `check_upload` (the
      minted actor and the attributed usage row), and `keyword_search` plus a
      graph tool after a reindex (the reconciliation left a coherent index).
      Do **not** exercise a live reassignment against the operator's own
      account; that path is covered by tests.

## Adversarial round 1 — record

The first adversarial Codex pass over the implemented change (branch
`spec/durable-identity-bindings` at `0e23917`) returned FAIL with seven
findings. All seven were verified against the code before being acted on; none
was found wrong. Dispositions:

1. **BLOCKER — the discard was not bound to the assignment that produced it**
   (`indexer.py`). **Fixed.** The discard transaction now takes the `users` row
   `SELECT … FOR UPDATE`, re-reads it, and requires present/active/assigned and
   `vault_path == facts.assignment` before deleting anything; the three-column
   stamp beside it must affect exactly one row or the whole transaction rolls
   back. Any disagreement deletes nothing, records nothing and aborts the pass,
   so the next one reclassifies. The re-derive's tail stamp takes the same lock
   and the same re-read and is *withheld* on disagreement (it destroys nothing,
   so failing the pass would buy nothing). Spec: index-integrity, the discard
   requirement plus four scenarios.
2. **MAJOR — verify-then-embed database TOCTOU.** **Fixed.** `embed_note` takes
   `certified_hash` / `certified_path` — what the bytes were verified against —
   and stamps them with a conditional, row-locking `UPDATE … WHERE id AND
   file_path AND content_hash = H1`, issued **before** any vector is replaced
   and **after** the provider call. A row that moved matches nothing;
   `StaleCertification` rolls the note back and leaves it unmarked. The hash
   written is H1, never one re-read from the row. The reviewer's suggested
   defence-in-depth `embedded_content_hash = content_hash` predicate on
   `semantic_search` was **not** adopted: it would silently drop every edited
   note from semantic search until the next embedding pass, which is a
   behavioural change of its own and is not what the finding requires.
3. **MAJOR — a confirmation DB failure after the move stood.** **Fixed.**
   `VaultConfirmationUnavailable` (deliberately not a `RuntimeError`)
   distinguishes "the read failed" from "the assignment changed". Before a
   call's first publication it propagates, as decided. In `move_note`'s rewrite
   loop, after the move has committed, it is caught: the remaining rewrites
   stop and the partial outcome is reported through the existing
   `failed_rewrite_sources` idiom, naming it a **confirmation outage** rather
   than a reassignment, with the usage marker
   `vault_confirmation_unavailable` — distinct from both
   `vault_assignment_changed` and `no_vault_assigned`. Both timings tested.
4. **MAJOR — 017's deploy window.** **Declared, not quiesced** — the standing
   decision, and it stands. An old-code mint between the backfill and the
   container recreate produces a row with NULL actor columns: the tail of the
   already-declared 015→017 gap, rendering through the join fallback exactly
   like every pre-017 row and degrading only if the credential is later
   deleted. Recorded as a declared residual in 017's docstring, in the
   schema-integrity delta (a requirement symmetric with 016's treatment) and in
   CLAUDE.md. No Makefile change and no deployment barrier: the window is
   seconds long, the fallback works, and quiescing the service for a column
   addition and a small backfill trades a real ongoing availability cost
   against it.
5. **MAJOR — the confirmation was not structurally single-use, target-bound or
   await-safe.** **Fixed, by removing the retainable stamp entirely.**
   `MutableTarget.confirm` / `take_confirmation` / `clear_confirmation` /
   `is_confirmed` are gone, `confirm_vault_assignment` is now private, and the
   single public entry point is
   `vault.confirmed_publication(user_id, publish)` — it awaits the read and
   calls a **synchronous** `publish` before returning control, and refuses a
   coroutine function or a returned awaitable. `RootConfirmation` carries its
   own spent flag (so one object cannot be spent through two targets) and
   `consume` validates the acting user id and canonical assignment against
   `MutableTarget.user_id` / `.assignment`. The two-ended move uses a
   `MovePermit`: single-use, that target pair only, not a confirmation and not
   convertible into one. All six tools, `delete_file` and the rewrite loop were
   reworked; the test that awaited between stamp and publish is gone and is
   replaced by one asserting the API makes it unwriteable.
   `UnconfirmedPublication` vs `VaultAssignmentChanged` is unchanged. Spec:
   vault-write, the structural-enforcement requirement plus four scenarios,
   including the rollback permit — which also resolves the verifier's
   concern 2, the two-ended stamp whose reasoning lived only in a comment.
6. **MAJOR — 017 arbitrarily attributed a double-FK row.** **Fixed.** 017
   refuses rows where both credential FKs are non-NULL, naming the ids in
   013's and 015's offender shape, **before** either backfill statement; it
   then creates `ck_transfer_tokens_one_credential`
   (`key_id IS NULL OR oauth_token_id IS NULL` — both NULL stays legal),
   marker-owned, resolved through `pg_constraint` against the server's own
   rendering, requiring `convalidated`, and dropped on downgrade only if
   marked. `transfer.Identity.__post_init__` asserts the same invariant.
   Schema-gate cases added for the fresh shape, the rejected insert, the
   offender refusal, the impostor / unmarked / `NOT VALID` constraints,
   stamp-back idempotence and the downgrade.
7. **MINOR — vacuous parametrization.** **Fixed.**
   `test_embed_vault_runs_under_every_classification` is replaced by
   `test_embed_vault_applies_no_provenance_gate_at_all`, which asserts the
   property directly (the classifier and the provenance read are monkeypatched
   to fail the test if reached), plus the finding-2 race test and its positive
   control.

Two non-blocking items from the independent verifier were folded in with these:
the silent link-extraction skip in `_update_links_for_changed` (a changed path
with no index row) now records a skip like its sibling, with a test and a spec
scenario; and CLAUDE.md gained register sections for the #88 confirmation and
the #91 provenance record.

### Adversarial round 2

The second pass, over `dd59ea7`, confirmed round 1's fixes present as described
and ruled the `semantic_search` hash-predicate declination reasonable ("hiding
every edited note until embedding completes is a worse default than serving its
last certified vector"). It returned FAIL with five MAJORs, all against the new
API. All five were verified against the code before being acted on; none was
wrong.

1. **`confirmed_publication` leaked an unspent confirmation.** Single
   consumption bounded *how many times* a confirmation could be used and said
   nothing about *when*, so `lambda c: saved.append(c)` returned with it
   unspent and a later `write_file_at(..., confirmation=saved[0])` published
   into a root the administrator had already reassigned away from. Generator
   and async-generator callbacks evaded both checks — calling a generator
   function runs none of its body. **Fixed** with a wrapper-scoped lease:
   `_leased` activates the confirmation and a `finally` revokes it on every
   exit; `consume` refuses an unleased one; a callable that returns without
   consuming is refused; coroutine/generator/async-generator functions *and*
   returned coroutines/generators/awaitables are refused. Nothing is `close()`d
   — arbitrary code on an unknown awaitable, and the lease has already made the
   object inert.
2. **`MovePermit` was forgeable.** `MovePermit(destination, source)` built by
   hand authorised a rename with no confirmation at all. **Fixed:** issuance is
   internal to a successful confirmed forward move (module-private
   `_PERMIT_ISSUE` token), the permit is bound to that confirmation's lease so
   it is inert once the enclosing `confirmed_publication` returns, and it
   carries the immutable `(user_id, assignment, rel)` of each endpoint beside
   object identity.
3. **The forward move confirmed only the destination** while `rename_noreplace`
   removes the source entry too. **Fixed:** `_require_one_vault` demands
   identical `user_id`, identical canonical `assignment`, and the same root
   *inode* (`fstat` of each pinned `root_fd`, not a pathname compare) before
   anything is consumed, on the forward move and on the rollback. Unreachable
   from `move_note`, which opens both ends with one `uid`; checked at the
   primitive because the next caller may not.
4. **The embedding-exclusion branch bypassed certification**, stamping by `id`
   alone. A concurrent `move_note` out of an excluded folder (same content, so
   the same hash) left an *included* note marked embedded with zero vectors —
   hash-equal, never selected again, permanently absent from
   `semantic_search`. **Fixed:** the branch goes through the same
   `certify_embedded` predicate (id + path + hash, rowcount exactly one),
   stamping before the delete so the conditional UPDATE's row lock covers it,
   and `StaleCertification` rolls the note back.
5. **The re-derive tail deadlocked with user deletion.** It took the `users`
   lock *after* holding `notes_metadata` row locks, reversing the panel's
   parent→child cascade order. **Fixed** per the reviewer's first option:
   `FOR UPDATE NOWAIT` inside `session.begin_nested()`, `55P03` treated as a
   withheld stamp, only the savepoint rolled back, the repairs committed, the
   reason logged. The savepoint is load-bearing — a failed statement poisons
   its transaction, so without one the pass loses every repair along with the
   stamp. The **discard head was verified** to take the users lock in its own
   transaction before any child write (the safe parent→child direction) and is
   therefore left waiting; a test asserts that ordering so it cannot drift.

Both spec deltas and CLAUDE.md were updated to describe the lease, the permit's
unforgeability and expiry, the two-ended move rule, the exclusion-branch
certification, and the asymmetric lock discipline between the discard head and
the re-derive tail.

**Observed, not caused, and not fixed here:**
`tests/test_transfer_routes.py::test_pages_are_static_and_nonce_guarded[/transfer/upload]`
failed once in a full-suite run and passed in isolation and on every re-run.
`/transfer/*` pages are rate-limited 30/minute by a process-global,
IP-keyed limiter that no fixture resets, and several modules exercise those
pages, so a run whose timing packs 30+ requests into one window gets a 429.
Pre-existing and unrelated to this change, which touches neither the transfer
routes nor the limiter.

### Adversarial round 3

The third pass, over `a77bcca`, re-attacked round 2's five fixes at second
order and found four of them holding: lease revocation survives an exception
raised from inside `consume`; permits do not retain `_PERMIT_ISSUE`; a released
root descriptor fails before anything is consumed or mutated; and the tail
savepoint preserves the repairs only for 55P03, while an anomalous zero-row
stamp still aborts the outer transaction. One MAJOR remained.

1. **A move did not re-open the path-dependent exclusion decision.** Verified
   real at both sites before fixing. Round 2's conditional certification closes
   *move-before-certify*; the mirror ordering is invisible to a predicate,
   because when the move lands after a correct certification the stamp is
   already committed and already true of the content. It is no longer true of
   the *decision*: `embedded_content_hash` records that the row's current
   content has been dealt with and says nothing about how, and the exclusion
   branch decides how by matching `EMBEDDING_EXCLUDE_PATTERNS` against
   `file_path`. Carried across a move the old answer is frozen for ever,
   because the pass selects on `embedded_content_hash != content_hash`.
   Excluded → included leaves an included note with zero vectors that is never
   selected again — silently absent from `semantic_search`; included →
   excluded leaves an excluded note searchable.
   **Fixed** conservatively, as directed: every statement that changes
   `file_path` now clears `embedded_content_hash` in the same statement —
   `move_note`'s metadata UPDATE (`.values(file_path=…,
   embedded_content_hash=None)`) and the indexer's **id-preserving** move
   detection (`SET … embedded_content_hash = NULL`). The prune-and-insert path
   needs nothing: its replacement row starts null. NULL means "re-evaluate at
   the next pass", so an unmoved-in-spirit note re-embeds only if the hash
   check selects it, and the exclusion decision is re-taken against the new
   path either way. Consulting the exclusion configuration at move time was
   rejected in the spec text: the configuration can change before the next
   pass, which is the same frozen answer in a new place, and it would give the
   move path a dependency on embedding configuration it has no other reason to
   know about.

`tests/integration/test_move_reevaluates_embedding.py` drives the **real**
`embed_vault` around the **real** move paths on a throwaway PostgreSQL, because
the property under test is what the next pass's selection predicate does with
the row a move left behind. It covers all four cells — certify-then-move and
move-then-certify, each in both boundary directions — for **both** movers (the
write tool and the indexer's move detection), and pins that both preserve the
row id, so the null stamp is the repair rather than a replacement row.
`tests/test_issue_91_indexed_root.py` adds the fast structural guard for a
machine with no PostgreSQL: both statements that change `file_path` are parsed
out of the source and required to clear the column.

The index-integrity delta and CLAUDE.md's #91 section both state the
invalidation rule; neither did before.


## Verification record (2026-08-24/25)

**D.6** — independent `openspec-verifier` audit at `0e23917`: 0 blocking gaps,
109/109 scenarios verified, zero falsely-ticked tasks, all four flagged
judgement calls ruled sound. The three fix commits since (`dd59ea7`,
`a77bcca`, `1f579e2`) were each spec-synchronised in the same commit and
verified by the adversarial gate below, with `validate --strict`, the unit
and full suites, and `make test-schema` green after every one — recorded here
rather than re-running the full audit against a tree the other gate had just
verified line-by-line.

**D.5** — adversarial Codex, four rounds against the implementation:
1 BLOCKER + 5 MAJOR + 1 MINOR → 5 MAJOR → 1 MAJOR → **0, PASS**. Every fix
carries a regression test built from the reviewer's own failing input and a
negative control (fix reverted in place → test fails): 13/13 controls fire.
Two reviewer sub-recommendations declined with recorded reasons (the
semantic_search hash predicate — ruled "reasonable" by the reviewer itself in
round 2 — and the 017 deploy-window quiesce, declared instead, symmetric with
016). Final gates on `1f579e2`: unit 1722 / full 1940 / schema gate 86 /
validate clean.


## Deploy and live exercise (2026-08-25)

**D.7** — deployed: backup `backup_20260824_214746.sql.gz`, migrations run in
the one-off container, container recreated and healthy. `alembic current` →
`017 (head)`; `alembic check` → "No new upgrade operations detected"; zero
tracebacks in the startup log.

**D.8** — exercised against the live server, naming the tools actually
called: `create_note`, `edit_note(append=True)`, `move_note`, `delete_note
(permanent=True)` — each through `confirmed_publication`'s fresh
read-and-consume — and `request_upload`, whose freshly minted token was
verified (read-only SQL) to carry `actor_kind='api_key'` and the key's label
at mint time, distinguishing the mint path from 017's backfill. The first
post-deploy index pass re-derived and tail-stamped both assigned users: all
three provenance facts non-NULL, confirming a skip-free re-derive over the
real vault. The refusal paths (reassignment mid-flight, confirmation outage)
cannot be exercised against production without mutating a real user's
assignment; they are covered by the suite's interleaving tests (1940 passing,
13 negative controls) and stated here rather than claimed live.
