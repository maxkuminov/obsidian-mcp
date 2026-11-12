Refs #432. Read `proposal.md`, `design.md` (D1–D9, the accepted limitations, the adversarial scenarios, the review record) and `specs/` before starting. The archived `openspec/changes/archive/2026-11-10-task-tools/` (D10, R1–R4) is the source of every invariant this builds on.

**Base check:** `src/services/task_rule.py` exists, `openspec/changes/task-refresh-on-write/` exists, and no migration is created.

This is one slice: the files overlap in `tools.py`, so there is no fan-out.

## 1. Gate and absence form

- [x] 1.1 `src/services/tasks.py`:
  - split `_CHECKBOX_RE` into `_CHECKBOX_PREFIX` (through `\]`) plus its lookahead, with no change to what it matches;
  - build `_CHECKBOX_GATE_RE` from `_CHECKBOX_PREFIX` with a `^` prefix and `re.MULTILINE`;
  - add `may_hold_tasks(data: bytes) -> bool` (D2);
  - replace the docstring's "write-time refresh exception" paragraph (D8).
- [x] 1.2 `src/services/task_index.py`:
  - add an `ABSENT` sentinel and the absence form of `refresh_note_tasks` (D3): check `target.lstat() is None` under the row lock, delete the rows, stamp the empty-note hash with both flags false;
  - reword the WARNING ("after a write");
  - update the module docstring (D8).

## 2. Call sites (`src/mcp_server/tools.py`)

- [x] 2.1 Add one helper, `_refresh_after_write(uid, target, *, replaced, published)`. It never raises. It covers:
  - the gate, both sides in one `asyncio.to_thread`, where an exception counts as true;
  - the equal-bytes skip, only when `replaced` is known;
  - the call through `_refresh_task_rows`, or the absence form;
  - `timing.record("task_refresh", …)`, only when a refresh was attempted.
- [x] 2.2 `edit_note_impl`: call the helper after `_record_note_events`, inside `with target:`, only on the branch that published changed bytes. The response is unchanged.
- [x] 2.3 `set_frontmatter_impl`: the same, with `raw_bytes` → `new_raw`.
- [x] 2.4 `write_file_impl`: only when `change_feed.is_note_path(target.rel)` and `had_incumbent`. `replaced` is `incumbent` when it was read, otherwise unknown.
- [x] 2.5 `delete_note_impl`: both branches, after the event, through the absence form, with no gate.
- [x] 2.6 Confirm that `create_note_impl`, `_move_note_locked`, `import_from_url_impl`, the upload route, `delete_file_impl` and the three task tools' order are untouched.

## 3. Tests

- [x] 3.1 `tests/test_note_tasks_extractor.py`:
  - the deterministic superset property, with thousands of generated notes that hold tasks;
  - linear time;
  - pattern identity;
  - the masked-lookahead case;
  - the no-generation-lock AST guard extended to the helper.
- [x] 3.2 `tests/test_task_refresh_generic.py` (new, offline, refresh stubbed):
  - the writer and exclusion matrix;
  - the gate decisions;
  - no call on a refusal, a dry run, a no-change answer or a raised publication;
  - the publish → event → refresh order;
  - byte-identical responses for every refresh code, with the event recorded each time;
  - the usage key present or absent;
  - the cancellation outcome.
- [x] 3.3 `tests/integration/test_task_refresh_generic_pg.py` (new; the R1 module `test_task_refresh_pg.py` is unchanged and still passes). Use port 55444, container `omcp-it-task-refresh-on-write`:
  - list → write → list with no pass, for `edit_note`, `set_frontmatter` and `write_file`;
  - removing the last checkbox;
  - `delete_note` → list;
  - the finding-1 interleaving;
  - the recreated path;
  - D7's interleavings;
  - out-of-order refreshes across writer kinds;
  - multi-user;
  - `lock_timeout`.
- [x] 3.5 Implementation review r1 follow-ups:
  - `write_file` refreshes after every note-path publication, with the move-between-sample-and-publish interleaving pinned offline;
  - a `write_file` creation is a `not_indexed` no-op (integration);
  - a call cancelled while its refresh waits on a held row lock (integration), and its lease released in `queue` mode (offline);
  - a worker-thread test for the gate and the derivation, replacing the 100 ms bound;
  - literal pre-change response strings;
  - an AST check that only the four writers call `_refresh_after_write`;
  - more no-refresh paths;
  - nesting and multi-line callouts in the gate generator.
- [x] 3.4 Run the offline suite once, and the integration modules covering the task index, the refresh, the pass's task derivation and the change feed. `tools_list_full_v2.json` must pass untouched.

## 4. Docs (same PR)

- [x] 4.1 `CLAUDE.md`: amend the "Task writes address a line…" bullet in place (D8).
- [x] 4.2 `docs/architecture/indexing-and-embeddings.md`: the L5 paragraph and "What `list_tasks` can and cannot see" (D8).
- [x] 4.3 `docs/architecture/vault-tools.md`: the L14 sentence (D8).
- [x] 4.4 `docs/architecture/usage-attribution.md`: the `task_refresh` key on the generic writers (D5).
- [x] 4.5 `README.md`: the task section bullet (D8).

## 5. Gates

- [x] 5.1 `openspec validate task-refresh-on-write --strict`.
- [x] 5.2 `openspec-verifier`, then the adversarial Codex pass. Both are mandatory: this change touches the write tools.
- [ ] 5.3 After deploy, exercise these against the live server (an MCP-side change, so no `user-representative` pass):
  - `list_tasks` → `edit_note` → `list_tasks`;
  - `set_frontmatter` → `list_tasks`;
  - `delete_note` → `list_tasks`.
