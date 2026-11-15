Refs #447. Read `proposal.md`, `design.md` (K1–K6, D1–D8, the scope inventory, L1–L13) and `specs/` first. This is a hotfix: one implementer, in order, with no fan-out (slices 2 and 3 both depend on slice 1, and the tests span all three).

**Base check:** `src/services/task_index.py` exists, `openspec/changes/preserve-file-permissions/` exists, and `git log --oneline -1` is at or after 85e8df5. No migration and no new `Settings` field.

## 1. Permission helpers (`src/services/vault_fs.py`)

- [ ] 1.1 ACL codec: constants for the six tags and `ACL_UNDEFINED_ID`.
  - `parse_acl(bytes) -> tuple[entry, ...]` refuses a wrong version, a length not `4 + 8n`, an unknown tag, a missing base entry, or entries out of kernel sort order.
  - `pack_acl(entries) -> bytes`.
  - `is_minimal(entries)`.
- [ ] 1.2 `PermissionsNotApplied(OSError)`, deliberately **not** a `VaultFSError` (D6). Its message names the step and errno and ends "nothing was written".
- [ ] 1.3 Capture:
  - `capture_leaf(dir_fd, name, *, fd=None)` returns `Leaf(dev, ino, ctime_ns, uid, gid, mode, acl)` or `None`. It uses `fd` when given (the comparison's descriptor), otherwise an `O_PATH | O_NOFOLLOW | O_CLOEXEC` open. It reads the ACL through `/proc/self/fd/<n>` (K4). `ENOENT` and a non-regular leaf give `None`.
  - `capture_dir(dir_fd)` returns `Dir(dev, ino, new_gid, default_acl)`.
  - `ENODATA` / `EOPNOTSUPP` / `ENOTSUP` mean no ACL. Any other errno raises `PermissionsNotApplied`.
- [ ] 1.4 `plan_for(leaf, dir) -> Plan(gid, mode, acl, fallback)` per D1. The create plan uses `default_file_mode()` when there is no default ACL. `Plan` is a frozen dataclass, so P1 and P2 plans compare with `==`.
- [ ] 1.5 `apply_plan(staged_fd, plan)` per D2: gid, then ACL, then mode. `EPERM`/`EINVAL` from `fchown` take the edit plan's fallback, or narrow a create plan's group class. Every other failure raises `PermissionsNotApplied`. Never `fchown` the uid.
- [ ] 1.6 `_create_descent`: `os.mkdir(…, 0o777)`. Add a `leaf_mode` keyword to `open_dir_beneath` / `_create_descent` (default 0o777). `open_staging_dir` passes 0o700, and its on-open `fchmod(0o700)` + verification are unchanged (D7).
- [ ] 1.7 Rewrite the docstrings of `default_file_mode`, `create_temp` and `open_staging_dir`, which describe the #95 relax step, to describe the two rules.

## 2. Note and raw-file writes (`src/services/vault.py`)

- [ ] 2.1 `_atomic_write_at`, P1: replace the `fchmod` at `:2452` with capture (leaf only on the overwrite branch, plus the directory through `target.dir_fd`), `plan_for` and `apply_plan`, before the existing `fsync`.
- [ ] 2.2 P2:
  - replace the `_read_fd_bytes(…, max_bytes=len(expected))` comparison with a helper that keeps the descriptor, compares the bytes, and returns `capture_leaf(…, fd=that_fd)`. Same bound, same `File changed while editing` errors;
  - with no `expected`, call `capture_leaf` via `O_PATH` immediately before `_require_staged_name` / the publish;
  - on the no-clobber branches, re-capture only the directory;
  - if the plan differs from P1's, `apply_plan` again and `os.fsync(fd)` before publishing.
- [ ] 2.3 Update the `_atomic_write_at` docstring's ordering list (steps 2–3) for P1/P2 and the second-flush rule.

## 3. Uploads and imports (`src/services/transfer.py`)

- [ ] 3.1 `_stream_locked`, P1: replace the `fchmod` at `:1904`.
  - Open the destination parent with `vault_fs.open_parent(root_fd, row.path, create=False)` and close it in a `finally`.
  - Capture the leaf only for `row.overwrite and row.expected_fingerprint is not None`, and the directory.
  - Plan, then `apply_plan(staged_fd, …)`, before `_flush_staged_payload`.
  - On `FileNotFoundError`, record "no P1 plan".
  - Rewrite the comment block above it.
- [ ] 3.2 `_publish_into_current_parent`, P2: after the two mount checks and before `vault_fs.publish`, capture through `dst_fd`.
  - For an overwrite token, require the leaf's `(dev, ino, ctime_ns)` to equal the fingerprint's, or raise `vault_fs.Conflict("Target changed since the token was minted: …")`.
  - If the plan differs from P1's, apply and `fsync` the staged inode. Unnamed mode uses `staged_fd`. Named mode reopens `tmp_name` `O_RDONLY | O_NOFOLLOW | O_NONBLOCK` in `staging_fd`, checks identity against `staged_st` (else `Conflict`), applies, `fsync`s and closes.
  - Thread the P1 plan through `_publish_into_current_parent`'s signature from both call sites.
- [ ] 3.3 Confirm that `PermissionsNotApplied` raised at P1 or P2 is pre-publication: `state["published"]` is false, so the route releases the claim. `vault_fs.publish` is unchanged.

## 4. Tests and CI

- [ ] 4.1 `tests/test_preserve_file_permissions.py` (new), per design "Test plan":
  - the ACL probe fixture with the `OMCP_TEST_REQUIRE_POSIX_ACL` skip-to-failure switch;
  - control-file oracles;
  - helper unit tests: codec round trip, refusal of malformed bytes, `plan_for` on group-`rwx` / private / no default ACL;
  - tool layer: `create_note_impl`, `edit_note_impl`, `set_frontmatter_impl`, `write_file_impl` (create, overwrite, overwrite to an absent name);
  - `stream_to_vault` (create, overwrite token, chmod-after-mint gives 409);
  - directories and `.transfer-tmp`;
  - gid fallback; mode-only `EOPNOTSUPP`; `EIO` gives `io_failure` with nothing changed; the P2 race with two `fsync`s; one `fsync` on the ordinary path;
  - the issue's reproduction table.
- [ ] 4.2 Invert `tests/test_transfer_service.py::test_overwrite_resets_the_incumbent_mode_to_server_policy`: the incumbent's 0600 is kept. Rename it and rewrite its docstring. Leave `test_written_note_keeps_the_umask_default_mode` and the umask-following upload test as they are; they still hold under the create rule with no default ACL.
- [ ] 4.3 `.github/workflows/ci.yml`, `tests` job, "Run the test suite" step env: `OMCP_TEST_REQUIRE_POSIX_ACL: "1"`, with a comment that a skip of the ACL tests there is a failure.
- [ ] 4.4 Run `pytest -q tests` (offline) and `make test-integration` once. Confirm that `tests/test_conftest_env_hermeticity.py` passes unchanged, and grep that no `Settings` field was added.

## 5. Docs (last commits of the feature branch)

- [ ] 5.1 `docs/architecture/vault-tools.md`: a "Published file permissions (#447)" section with the two rules, P1/P2 and the binding, the failure policy, the directory rule, L1–L13 in one line each, and "do not reintroduce a fixed mode".
- [ ] 5.2 `docs/architecture/file-transfer.md`: staging in `.transfer-tmp` means the ACL is written explicitly. Cover the gate re-capture, the ctime binding, the second flush only on a change, and replace the "beside the `os.fchmod`" wording in the durability paragraph.
- [ ] 5.3 `CLAUDE.md`: one key-decision bullet. It quotes the owner's sentence, states both rules and the never-broader refusal, says directories are made 0o777, and points to the vault-tools note.
- [ ] 5.4 `README.md`: 1.5.1 upgrade note.
  - The server does not repair files already narrowed or widened.
  - How an operator finds and fixes them with `getfacl`/`setfacl`, for example re-granting the mask on files under a folder with a group default ACL, and checking for 0644 files that should be private. Use placeholders only: no host paths or names.
  - Folders under umask 0 are now 0o777, as `mkdir` makes them.
- [ ] 5.5 Release-notes input for 1.5.1: the bug, both rules, the directory change, the refusal on non-"unsupported" ACL failures, and no setting or migration.
- [ ] 5.6 Before the PR: grep the change for host paths, hostnames, usernames and vault note names; the repo is public.
