Refs #432. Read `proposal.md`, `design.md` (D1–D9, the accepted limitations, the adversarial scenarios) and `specs/` before starting. The archived `openspec/changes/archive/2026-11-10-task-tools/` (D10, R1–R4) is the source of every invariant this builds on.

**Base check:** `src/services/task_rule.py` exists, `openspec/changes/task-refresh-on-write/` exists, and no `alembic/versions/041_*` is needed or created.

One slice: the files overlap (`tools.py`), so no fan-out.

## 1. Gate and absence form

- [ ] 1.1 `src/services/tasks.py`:
  - split `_CHECKBOX_RE` into `_CHECKBOX_PREFIX` (through `\]`) plus its lookahead, with no change to what it matches, and build `_CHECKBOX_GATE_RE` from `_CHECKBOX_PREFIX` with a `^` prefix and `re.MULTILINE`;
  - `may_hold_tasks(data: bytes) -> bool` (D2);
  - the docstring's "write-time refresh exception" paragraph replaced (D8).
- [ ] 1.2 `src/services/task_index.py`:
  - an `ABSENT` sentinel and the absence form of `refresh_note_tasks` (D3): the `target.lstat() is None` check under the row lock, the row deletion, and the empty-note hash stamp with both flags false;
  - the WARNING reworded ("after a write");
  - the module docstring updated (D8).
- [ ] 1.3 `src/config.py`: `TASK_REFRESH_MAX_NOTES_PER_CALL = 32`, a constant next to `TASK_REFRESH_LOCK_WAIT_MS`, with a comment naming D6.

## 2. Call sites (`src/mcp_server/tools.py`)

- [ ] 2.1 One helper, `_refresh_after_write(uid, target, *, replaced, published, budget=None)`:
  - the gate (both sides in one `asyncio.to_thread`; an exception counts as true);
  - the equal-bytes skip;
  - the call through `_refresh_task_rows` (or the absence form);
  - `timing.record("task_refresh", …)` only when attempted;
  - the per-call budget object used by `move_note` (D6).

  It never raises.
- [ ] 2.2 `edit_note_impl`: after the publication, inside `with target:`, before `_record_note_events`, only on the branch that published changed bytes. The response is unchanged.
- [ ] 2.3 `set_frontmatter_impl`: the same, with `raw_bytes` → `new_raw`.
- [ ] 2.4 `write_file_impl`: only when `change_feed.is_note_path(target.rel)` and `had_incumbent`. `replaced` is `incumbent` when it was read, else unknown.
- [ ] 2.5 `delete_note_impl`: both the soft and the permanent branch, through the absence form. `replaced` is the incumbent when it was read, else unknown.
- [ ] 2.6 `_move_note_locked`: in the rewrite loop, after a successful rewrite publication and before `drop(write_target)`, with one budget per call. Nothing for a failed or stopped rewrite or under `db_failed`. Record the call's single `task_refresh` value (D5).
- [ ] 2.7 Confirm that `create_note_impl`, `import_from_url_impl`, the upload route and `delete_file_impl` are untouched.

## 3. Tests

- [ ] 3.1 `tests/test_note_tasks_extractor.py`:
  - the gate superset property (corpus plus generated inputs, scenario 5);
  - the linear-time cases (scenario 4);
  - the pattern-identity assertion;
  - the no-generation-lock AST guard extended to the helper.
- [ ] 3.2 `tests/test_task_refresh_generic.py` (new, offline, stubbed refresh):
  - the writer and exclusion matrix (D1);
  - gate decisions, including unknown incumbents and equal bytes;
  - no call on a refusal, dry run, no-change answer or raised publication;
  - publish → refresh → event ordering;
  - byte-identical responses for every refresh code;
  - usage key present or absent;
  - the move budget (32 notes, the bytes total, stop at `lock_timeout`/`error`, `deferred`).
- [ ] 3.3 `tests/integration/test_task_refresh_pg.py` (extend):
  - list → write → list with no pass, for `edit_note`, `set_frontmatter`, `write_file` and a move rewrite;
  - `delete_note` → list (soft and permanent);
  - the recreated-path `superseded` case;
  - D7's interleavings with `edit_note` and the clear;
  - out-of-order refreshes across writer kinds;
  - multi-user isolation;
  - `lock_timeout` with a held row lock (the response unchanged, the usage row `lagged:lock_timeout`);
  - a note at `MAX_NOTE_BYTES` with 5,001 checkboxes.

  Port 55444, container `omcp-it-task-refresh-on-write`.
- [ ] 3.4 Run the offline suite once, and the touched integration modules plus `test_note_tasks_pg.py` and `test_task_tools_pg.py`. `tools_list_full_v2.json` passes untouched.

## 4. Docs (same PR)

- [ ] 4.1 `CLAUDE.md`: the "Task writes address a line…" sentence (D8).
- [ ] 4.2 `docs/architecture/indexing-and-embeddings.md`: the L5 paragraph and "What `list_tasks` can and cannot see" (D8).
- [ ] 4.3 `docs/architecture/vault-tools.md`: the L14 sentence (D8).
- [ ] 4.4 `docs/architecture/usage-attribution.md`: the `task_refresh` key on the generic writers, with `deferred` (D5).
- [ ] 4.5 `README.md`: the task section bullet (D8). No new setting, so `.env.example` and the settings table are unchanged.

## 5. Gates

- [ ] 5.1 `openspec validate task-refresh-on-write --strict`.
- [ ] 5.2 `openspec-verifier`, then the adversarial Codex pass. This is a mandatory trigger: the change touches the write tools. Give the reviewer the design's adversarial-scenario list.
- [ ] 5.3 After deploy, exercise end to end against the live server, with no `user-representative` pass (MCP-side change):
  - `list_tasks` → `edit_note` → `list_tasks`;
  - `set_frontmatter` → `list_tasks`;
  - `delete_note` → `list_tasks`.

  Name the tools called in the report.
