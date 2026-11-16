Refs #447. Read `proposal.md`, `design.md` (K1–K7, D1–D9, the scope inventory, L1–L12, the review record) and `specs/` first. This is a hotfix: one implementer, in order, with no fan-out. Slices 2 and 3 depend on slice 1, and the tests span all three.

**Base check:** `src/services/task_index.py` exists, `openspec/changes/preserve-file-permissions/` exists, and `git log --oneline -1` is at or after 85e8df5. No migration, no new `Settings` field, no new refusal code.

## 1. Permission helpers (`src/services/vault_fs.py`)

- [x] 1.1 ACL codec:
  - tag constants and `ACL_UNDEFINED_ID`;
  - `parse_acl(bytes)`, which refuses a wrong version, a bad length, an unknown tag, permission bits outside `rwx`, a missing base entry, named entries without a mask, and out-of-order or repeated entries;
  - `pack_acl(entries)`;
  - `is_minimal(entries)`;
  - `minimal_acl(mode)`.
- [x] 1.2 `PermissionsNotApplied(OSError)`, deliberately **not** a `VaultFSError` (D6). Its message names the step and errno and ends "nothing was written".
- [x] 1.3 Capture (D5):
  - `capture_leaf(dir_fd, name, *, fd=None) -> Leaf | None` runs `fstat`, then the ACL read through `fd` or `/proc/self/fd/<n>` of an `O_PATH | O_NOFOLLOW` open, then `fstat` again. A changed `(dev, ino, ctime_ns)` raises a `CaptureChanged` signal that each writer maps to its own conflict. `ENOENT` or a non-regular leaf gives `None`.
  - `capture_dir(dir_fd) -> Dir(dev, ino, setgid, gid, default_acl, acl_capable)`.
  - `ENODATA`, `EOPNOTSUPP` and `ENOTSUP` mean no ACL. Any other errno raises `PermissionsNotApplied`.
- [x] 1.4 `edit_plan(leaf)` and `create_plan(dir, staged_gid)` per D1. Never `os.getegid()`. `Plan` is a frozen dataclass.
- [x] 1.5 `translate(plan, staged_gid, acl_capable)` per D3: the ACL form (pre-existing named entry left alone, mask rule, sort order) and the mode-only form.
- [x] 1.6 `apply_plan(staged_fd, plan, acl_capable)` per D2. Order: gid, then (on `EPERM`/`EINVAL`) translate, then ACL, then mode. `EOPNOTSUPP`/`ENOTSUP` on writing a *translated* ACL takes the mode-only form only when the rule has no ACL. For a rule with an ACL, that and every other failure raises `PermissionsNotApplied`. Never `fchown` the uid.
- [x] 1.7 `_create_descent` (D8): read the parent's default ACL through the prefix descriptor. Use mode 0o777 when it exists, and 0o755 when there is none or any read error. `open_staging_dir` keeps 0o755 by passing `follow_default_acl=False`, so `.transfer-tmp` is never created group-writable before its `fchmod(0o700)`.
- [x] 1.8 Rewrite the docstrings that describe the #95 relax step: `default_file_mode`, `create_temp`, `open_staging_dir`.

## 2. Note and raw-file writes (`src/services/vault.py`)

- [x] 2.1 P1: replace the `fchmod` at `:2452` with capture (leaf on the overwrite branch only, and the directory), plan and `apply_plan`, before the existing `fsync`. `CaptureChanged` raises `RuntimeError("File changed while editing: <name>")`.
- [x] 2.2 P2, overwrite branch only. Replace the `_read_fd_bytes(…, max_bytes=len(expected))` comparison with a helper that keeps its descriptor, with the same bound and errors, and capture the leaf through it **before** reading and comparing the bytes, so nothing is added between the comparison and the rename. With no `expected`, capture through `O_PATH` immediately before `_require_staged_name`. Any difference from P1 raises `RuntimeError("File changed while editing: <name>")`: identity, gid, mode bits, ACL bytes, or a regular file appeared or vanished. There is no re-apply and no second `fsync`.
- [x] 2.3 Update the `_atomic_write_at` docstring's ordering list for P1 and P2, and state that the compare-to-rename window is exactly today's.

## 3. Uploads and imports (`src/services/transfer.py`)

- [x] 3.1 `_stream_locked`: replace the `fchmod` at `:1904` with one capture, before `_flush_staged_payload`.
  - Open the parent with `open_parent(…, create=False)`; a missing parent is `vault_fs.Conflict`.
  - Capture the leaf only for an overwrite token with a fingerprint, and require `(dev, ino, ctime_ns)` to equal the fingerprint's, else `Conflict("Target changed since the token was minted: …")`. Run the existing mount-boundary checks first, so a leaf bind-mounted after the mint is still refused as a mount boundary.
  - Capture the directory, plan, and `apply_plan(staged_fd, …)`.
  - Rewrite the comment block above.
- [x] 3.2 `_publish_into_current_parent`: for a create plan, after the mount checks, compare `fstat(dst_fd)`'s `(dev, ino)` with the captured directory's, else `Conflict`. Thread the captured directory identity from both call sites. There is no second capture, no reopen of the staging name and no second flush. `vault_fs.publish` is unchanged.
- [x] 3.3 Confirm that every new raise is pre-publication (`state["published"]` false): a `Conflict` gives 409 with the claim released, and `PermissionsNotApplied` gives the generic pre-publication branch with the claim released.

## 4. Tests and CI

- [x] 4.1 `tests/test_preserve_file_permissions.py` (new), per design "Test plan":
  - the probe fixture with the `OMCP_TEST_REQUIRE_POSIX_ACL` skip-to-failure switch;
  - control oracles with `st_gid` in the group-kept case, and the staged gid plus effective access in translation cases;
  - the effective-access helper (K7) for translation cases;
  - every case listed there.
- [x] 4.2 Invert `tests/test_transfer_service.py::test_overwrite_resets_the_incumbent_mode_to_server_policy`: the incumbent's 0600 is kept. Rename it and rewrite its docstring. Keep `test_written_note_keeps_the_umask_default_mode` and the umask-following upload test as they are.
- [x] 4.3 `.github/workflows/ci.yml`, `tests` job, "Run the test suite" env: `OMCP_TEST_REQUIRE_POSIX_ACL: "1"`, with a comment that a skip of the ACL tests there is a failure.
- [x] 4.4 Run `pytest -q tests` and `make test-integration` once. Confirm `tests/test_conftest_env_hermeticity.py` passes unchanged and that no `Settings` field was added.
- [x] 4.5 Implementation review round 2: verify a named staged copy's bytes after the flush and before P2 (in-call conflict on any difference, L14 for the rest); a symlink at the `expected=` comparison's open is the in-call conflict; tests for both.

## 5. Docs (last commits of the feature branch)

- [x] 5.1 `docs/architecture/vault-tools.md`: a "Published file permissions (#447)" section covering:
  - the two rules, group translation with the D3 property, and P1/P2 as a conflict;
  - the failure policy and directories under a default ACL;
  - L1–L12, one line each;
  - "do not reintroduce a fixed mode or a create-plan fallback for edits".
- [x] 5.2 `docs/architecture/file-transfer.md`: staging in `.transfer-tmp`, so the ACL is written explicitly; one capture bound to the fingerprint; the gate's directory-identity check; replace the "beside the `os.fchmod`" wording.
- [x] 5.3 `CLAUDE.md`: one key-decision bullet. Quote the owner's sentence, state both rules, translation, the never-broader refusal, and directories only under a default ACL, and point to the vault-tools note.
- [x] 5.4 `README.md`: 1.5.1 upgrade note.
  - The server does not repair files already narrowed or widened.
  - How an operator can find and fix them with `getfacl`/`setfacl` and `chmod`, using placeholders only.
  - Edits of files whose group the server is not in now carry a named group entry.
- [x] 5.5 Release-notes input for 1.5.1.
- [x] 5.6 Before the PR: grep for host paths, hostnames, usernames and vault note names; the repo is public.
