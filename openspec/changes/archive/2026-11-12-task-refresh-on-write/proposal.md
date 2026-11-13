Refs #432 (follow-up to #428, v1.3.0, and #434)

## Why

`list_tasks` and `task_agenda` read `note_tasks`, never a file. Since #428 the three task write tools refresh the written note's task rows in the same call, so the next listing reflects the write. Every other writer leaves the rows as they were until the next index pass, which runs every 300 s by default. The archived `2026-11-10-task-tools` change records this as its L5.

The gap is not rare. The owner uses the task tools daily, and agents mix `edit_note` with `list_tasks`: they rewrite a checklist section, tick a box with a `find`/replace, or delete a finished note, then list. For up to five minutes the listing then shows the old text and the old id, or the tasks of a note that no longer exists, and nothing says so. `complete_task` on that old id answers `task_not_found`, because it re-derives the task from the current bytes, and listing again returns the same stale row. A silently wrong listing that an agent acts on is the expensive failure for this product.

`task_index.refresh_note_tasks` already composes with the index pass:

- it derives from the published bytes;
- it takes only the note's row lock, with a bounded wait;
- it re-checks the bytes on disk under that lock;
- it never takes the generation lock and never fails the write.

This change calls it from the generic note writers too, as a best-effort tail behind a cheap gate.

## What Changes

- **`edit_note`, `set_frontmatter`, and `write_file` when it replaces an existing `.md` note** attempt a refresh after their confirmed publication and their change-feed event. They do this only when the bytes the call published, or the bytes it replaced, may hold a task. `set_frontmatter` is included because a block that changes size moves every task's line number.
- **`delete_note` clears.** After every confirmed delete it removes the deleted note's task rows. Before doing so it confirms, under the note's row lock, that nothing is at the path, so a note recreated in the meantime is never cleared. There is no byte gate: the delete removes whatever stands at the leaf when it runs, and those bytes are never read.
- **Excluded, each for a stated reason** (design D1):
  - `create_note`, and `write_file` when it creates a note: there is no index row yet.
  - `move_note`, plain or with link rewrites. A plain move re-keys the row, and the tasks follow it. Link rewrites are left to the pass (L4).
  - `import_from_url` and `PUT /transfer/upload`: they never hold the bytes.
  - `delete_file`: it refuses `.md`.
  - Non-markdown paths.
- **The gate** is `tasks.may_hold_tasks`: the extractor's own checkbox prefix, matched at line starts over the canonical text. That makes it a provable superset of the extractor. It never reads the file a second time and reads no database row.
- **Best effort, silent, non-interfering** (D4, D5).
  - When the refresh runs, the next listing reflects the write.
  - When it cannot run (lock timeout, superseded, error), the rows converge at the next index pass, exactly as before this change. The generic writers say nothing about it: their responses stay byte-identical (L3).
  - Every tool's name, schema, description and annotations stay byte-identical.
  - The existing publish → event order is kept, and the refresh runs after the event.
  - When a refresh is attempted, the usage row gains the result-side key `task_refresh`.
- **At most one refresh per call**, bounded by one note's size cap and the existing 2 s lock wait.
- **No migration, no new setting, no new constant, no schema or description change.**

## Non-goals

- A guarantee that the next listing reflects a generic write.
- Indexing a note at write time. #428 D10 rejected this.
- Making a new note's tasks, or `move_note`'s link rewrites, visible before the pass.
- Refreshing `notes_metadata.tags` at write time.
- Printing `index: …` lines on the generic writers.
- Reordering the three task tools, which keep publish → refresh → event.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `task-write`: which generic writers refresh, the gate, the delete clear, the silent best-effort contract, the per-call bound, and convergence for the new call sites.
- `note-tasks`: the "only other writer" of task rows now includes the generic writers' refresh and the delete clear.

## Impact

- **Code:**
  - `src/services/tasks.py`: the gate.
  - `src/services/task_index.py`: the absence form, and the log wording.
  - `src/mcp_server/tools.py`: one helper, plus call sites in `edit_note_impl`, `set_frontmatter_impl`, `write_file_impl` and `delete_note_impl`.
- **Schema:** none.
- **Behaviour:**
  - Most generic writes to a note that holds or held a checkbox now show in the next listing.
  - Deleted notes leave listings at once.
  - Such writes take longer by one derivation plus one short transaction, and by up to 2 s more when the row lock is contended. Every delete costs one short transaction.
  - Responses are unchanged.
- **Docs:**
  - `CLAUDE.md`;
  - `README.md`;
  - `docs/architecture/indexing-and-embeddings.md`;
  - `docs/architecture/vault-tools.md`;
  - `docs/architecture/usage-attribution.md`.
