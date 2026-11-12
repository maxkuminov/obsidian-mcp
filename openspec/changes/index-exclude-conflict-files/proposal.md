Refs #392

## Why

File-sync tools resolve a conflicting edit by keeping both versions: one stays at the note's path and the other is written beside it under a marked name, for example `Note (conflict 2026-05-29-19-45-39).md` (Syncthing writes `Note.sync-conflict-20260529-194539-ABCDEFG.md`, Nextcloud `Note (conflicted copy 2026-05-29 194539).md`, Obsidian Sync `Note (Conflicted copy MyLaptop 202605291945).md`). The indexer treats each copy as an ordinary note. The copy is nearly identical to the original, so it comes back as a duplicate row in `keyword_search`, `semantic_search`, `hybrid_search`, `list_notes`, `get_recent`, `find_related`, the task listings and the link graph. An agent acting on search results cannot tell which of the two rows is the live note, and may edit the stale copy.

This was observed live (2026-10-30, after #354): the reference vault holds two conflict copies outside `.trash/` today, and four more sit in `.trash/`, all in the form shown first above.

The vault is the owner's source of truth, and a conflict copy may hold the only good version of an edit (the sync tool picks the winner by modification time or device id, not by content). So the server must not hide these files by default, must never delete or rename them, and must keep them reachable by exact path so the owner or an agent can recover one.

## What Changes

- **One opt-in setting, `INDEX_EXCLUDE_CONFLICT_FILES`** (boolean, default `false`). When it is true, a Markdown file whose **basename** matches one of a fixed, documented set of conflict-copy name patterns is not indexed. Six patterns are covered: Syncthing, Nextcloud (current and legacy), Dropbox, Obsidian Sync, and the `(conflict YYYY-MM-DD-HH-MM-SS)` form from the issue. Each pattern comes from the tool's primary documentation or source code, except the issue's form, which comes from the reference vault. Matching is case-sensitive, anchored at the end of the name, and needs the tool's literal marker plus a complete numeric timestamp, so an ordinary title cannot match by accident.
- **Index only.** The exclusion is decided at the scan by name, before any stat or read, using the existing present-but-not-indexable mechanism (#308 D7). An excluded file is not upserted, never takes part in move pairing, and any row already at its path is deleted in the pass's transaction. Its embeddings, keyword vector, outgoing links and task rows go with the row. The file is never touched. `read_note`, `edit_note`, `set_frontmatter`, `move_note`, `delete_note`, `list_files`, `read_file` and the task write tools keep working on it by exact path. Every index-backed listing and search leaves it out.
- **No new failure mode.** An exclusion is not a skip. It never fails or restarts a pass, never marks a walk incomplete, never withholds a re-derive's stamp, and never keeps a scope due for a full-hash pass. `/health` does not change.
- **Change feed.** When a pass removes the row of a now-excluded path, it records `deleted` with the existing reason `not_indexable`. `get_changes` already documents that reason as "still on disk but cannot be indexed", and the reason set is fixed by a CHECK constraint, so this needs no migration and no description change. An excluded path with no row records nothing. Turning the setting off makes the files ordinary new notes on the next pass, recorded as `created`.
- **Reporting.** Each pass that excluded files logs one INFO count line. A pass that removed rows because of the exclusion also logs one INFO line naming them (the existing offender-report format: up to 20 paths, then `, and N more`). The panel dashboard shows a single template-only line with the viewer's scope count while the setting is on. Nothing goes into `indexer_runs.error`, because a non-quarantine line there marks a run failed.
- **Documentation:** `README.md` settings table, `.env.example`, `DEPLOYMENT.md` (what turning it on and off does, how to recover a conflict copy, the multi-user caveat), and the indexing architecture note.

## Non-goals

- A general exclusion-glob list. Rejected in design D1.
- Resolving, merging, renaming or deleting conflict copies, or flagging them to agents in search output.
- A per-user setting. The setting is deployment-wide; see limitation L4.
- Changing any tool's name, schema, title, annotations or description. `tests/fixtures/tools_list_full_v2.json` stays byte-identical.
- A migration, or a new change-feed reason.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `index-integrity`: the conflict-copy exclusion, its matching rule, its place in the scan and prune, and its non-effect on pass outcome, the backstop, the re-derive, the quarantine and `/health`.
- `change-feed`: the event an exclusion removal records, and the toggle-off creations.
- `panel-ops-health`: the dashboard's excluded-count line, for the viewer's own vault scope.
- `note-tasks`: every task scope, `scope="all"` included, is defined over indexed notes.

After the spec review (r1), `create_note` and a fully successful `move_note` that publish a conflict-shaped name while the setting is on end with one fixed notice line. Their output is otherwise unchanged, and byte-identical when the setting is off.

## Impact

- Code: a new pure module `src/services/conflict_copies.py`, `src/config.py` (one boolean), `src/services/indexer.py` (`_scan_vault` classification, the removal log line, `IndexPassResult.excluded`, an in-process per-scope count), `src/control_panel/routes.py` and `templates/dashboard.html` (one line).
- No migration, no tool-contract change, no `/health` change.
- The panel line needs the owner's manual browser pass under the nonce-based CSP. The template change adds no script, no inline style and no new class.
- Operator-visible: rows for conflict copies disappear from every index-backed tool on the first pass after a restart with the setting on. Each removal is recorded in the change feed.
