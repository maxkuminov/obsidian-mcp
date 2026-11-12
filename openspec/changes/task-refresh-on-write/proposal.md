Refs #432 (follow-up to #428, v1.3.0, and #434)

## Why

`list_tasks` and `task_agenda` read `note_tasks`, never a file. Since #428 the three task write tools refresh the written note's task rows in the same call, so the next listing reflects the write. Every other writer leaves the rows as they were until the next index pass, 300 s by default. The archived `2026-11-10-task-tools` change records this as its L5.

In practice the gap is not rare. The owner uses the task tools daily, and agents mix `edit_note` with `list_tasks`: they rewrite a checklist section, tick a box with a `find`/replace, or delete a finished note, then list. For up to five minutes the listing then shows:

- the old text and the old id. `complete_task` on that id answers `task_not_found` because it re-derives from the current bytes, and listing again returns the same stale row;
- or tasks of a note that no longer exists.

Nothing in the response says so. A silently wrong listing that an agent acts on is the expensive failure for this product.

`task_index.refresh_note_tasks` already composes with the index pass. It derives from the published bytes, takes only the note's row lock with a bounded wait, re-checks the bytes on disk under that lock, never takes the generation lock and never fails the write. This change calls it from the generic note writers too, behind a cheap gate that keeps the cost off notes with no checkbox.

## What Changes

- **These writers refresh**, after a confirmed publication and before their change-feed event, when the gate passes:
  - `edit_note`;
  - `set_frontmatter`, which moves task line numbers when the block changes size;
  - `write_file` when it replaces an existing `.md` note;
  - `move_note(rewrite_links=True)`, for the moved note's own rewrite and each backlink source it rewrites. A rewritten link inside a task line changes the task's text, and with it the task's id.
- **`delete_note` clears.** In the same refresh transaction, after confirming that the path is now empty, it removes the deleted note's task rows. Listings stop showing tasks of a note that is gone.
- **These are excluded, each for a stated reason** (design D1):
  - `create_note`, and `write_file` when it creates a note: there is no index row yet, and creating one is the pass's job;
  - a plain `move_note`: task rows are keyed by note id and follow the row the move re-keys;
  - `import_from_url` and `PUT /transfer/upload`: they stream to disk and never hold the bytes, so a refresh would need a second read, which R1 forbids;
  - `delete_file`: it refuses every `.md` path;
  - every non-markdown path.
- **The gate** (D2). A refresh runs only if the bytes the call published, or the bytes it replaced, may hold a task. "May hold a task" means one new pure function, `tasks.may_hold_tasks`: a line-start checkbox match over the canonical text, with exactly the extractor's checkbox pattern, so the gate is a provable superset of the extractor. When the replaced bytes are unknown (an unguarded `write_file` overwrite, an unguarded `delete_note`), it assumes they may. It never reads the file a second time, and it reads no database row.
- **Silent and non-interfering** (D4, D5). The generic writers' responses stay byte-identical, and so does every tool's name, schema, description and annotations. The refresh can neither change a call's outcome nor raise. When a refresh is attempted, the usage row gains the result-side key `task_refresh`.
- **Bounded per call** (D6). Every refresh is bounded by one note's size cap and the existing 2 s lock wait. `move_note` refreshes at most 32 notes and at most `MAX_NOTE_BYTES` of published bytes per call, and stops after the first refresh that hits `lock_timeout` or `error`. Notes beyond that are left to the pass, which converges them.
- **No migration, no new setting, no schema or description change.**

## Non-goals

- Indexing a note at write time: its row, hash, keyword vector, links or embeddings. #428 D10 rejected this and the reason stands.
- Making a new note's tasks visible before the pass. A follow-up could wake the pass early.
- Refreshing `notes_metadata.tags` at write time. The `excluded_note_tags` exclusion still follows the pass (L2).
- Printing `index: refreshed` / `index: may lag` on the generic writers.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `task-write`: which generic writers refresh, the gate, the delete clear, the silent contract, the per-call bound, and convergence for the new call sites.
- `note-tasks`: the "only other writer" of task rows now covers the generic writers' refresh and the delete clear.
- `change-feed`: the event of a generic writer that refreshes is recorded after the refresh.

## Impact

- **Code:**
  - `src/services/tasks.py` (`may_hold_tasks`);
  - `src/services/task_index.py` (the absence form for `delete_note`, and log wording);
  - `src/mcp_server/tools.py` (one helper, plus call sites in `edit_note_impl`, `set_frontmatter_impl`, `write_file_impl`, `delete_note_impl` and `_move_note_locked`);
  - `src/config.py` (the two per-call constants, which are not settings).
- **Schema:** none. The refresh writes only the columns #428 introduced (`tasks_source_hash`, `tasks_refreshed_at`, `tasks_failed`, `tasks_truncated`) and `note_tasks`.
- **Behaviour:**
  - after one of the listed writes to a note that holds or held a checkbox, `list_tasks` / `task_agenda` reflect it in the next call, and listings stop showing a deleted note's tasks;
  - writes to such notes take longer by one derivation plus one short transaction, and by at most 2 s more when the row lock is contended;
  - responses are unchanged.
- **Docs:**
  - `CLAUDE.md` ("Task writes address a line…" entry);
  - `README.md` (the task section's "Listings see the write at once" bullet);
  - `docs/architecture/indexing-and-embeddings.md` (the L5 paragraph and "What `list_tasks` can and cannot see");
  - `docs/architecture/vault-tools.md` (task tools, the L14 sentence);
  - `docs/architecture/usage-attribution.md` (the result-side keys).
