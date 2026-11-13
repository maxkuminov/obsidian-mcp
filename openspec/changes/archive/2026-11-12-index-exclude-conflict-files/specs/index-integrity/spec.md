## ADDED Requirements

### Requirement: `INDEX_EXCLUDE_CONFLICT_FILES` SHALL be an opt-in, deployment-wide boolean that excludes only basenames matching the fixed conflict-copy patterns

The system SHALL provide the setting `INDEX_EXCLUDE_CONFLICT_FILES`, a boolean with default `false`, read from the environment, applying to every scope. While it is `false`, the index SHALL contain exactly what it contains without this change.

While it is `true`, a discovered note SHALL be excluded if and only if its **basename** (the final path component, as discovered) fully matches at least one of these patterns. The patterns are case-sensitive and use ASCII digits only:

- P1 (Syncthing): `.+\.sync-conflict-[0-9]{8}-[0-9]{6}-(?:[A-Z2-7]{7})?\.md`
- P2 (Nextcloud desktop): `.+ \(conflicted copy (?:[^()/]* )?[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{6}\)\.md`
- P3 (Nextcloud/ownCloud legacy): `.+_conflict-[0-9]{8}-[0-9]{6}\.md`
- P4 (Dropbox): `.+ \([^()/]+(?:'|’)s conflicted copy [0-9]{4}-[0-9]{2}-[0-9]{2}(?: \([0-9]+\))?\)\.md`. The apostrophe is U+0027 (apostrophe) or U+2019 (right single quotation mark).
- P5 (Obsidian Sync): `.+ \(Conflicted copy .+ [0-9]{12}\)\.md`
- P6 (observed form): `.+ \(conflict [0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{2}-[0-9]{2}-[0-9]{2}\)\.md`

Directory components SHALL NOT be matched. The patterns SHALL be defined once, in `src/services/conflict_copies.py`, each with a comment citing its source.

#### Scenario: Each documented form is excluded

- **WHEN** the setting is true and the vault holds `N.sync-conflict-20260529-194539-ABC2DEF.md`, `N.sync-conflict-20260529-194539-.md`, `N (conflicted copy alice 2026-05-29 194539).md`, `N (conflicted copy 2026-05-29 194539).md`, `N_conflict-20260529-194539.md`, `N (Alice's conflicted copy 2026-05-29).md`, `N (Alice's conflicted copy 2026-05-29 (1)).md`, `N (Conflicted copy MyMacBook2 202411281430).md` and `N (conflict 2026-05-29-19-45-39).md`
- **THEN** none of them SHALL have an index row after the pass, and `N.md` SHALL be indexed

#### Scenario: Look-alike titles are not excluded

- **WHEN** the setting is true and the vault holds `Crazy weekend - birthday, conflict, and bubbling life.md`, `2026-05-29 conflict.md`, `Conflict resolution (draft).md`, `Notes (conflict 2026-05-29).md`, `Note (conflicted copy).md`, `Note (CONFLICT 2026-05-29-19-45-39).md`, `Note (conflict 2026-05-29-19-45-39) v2.md`, `Note.sync-conflict-2026-05-29.md`, `(conflict 2026-05-29-19-45-39).md` and a P6-shaped name whose timestamp uses non-ASCII digits
- **THEN** every one of them SHALL be indexed

#### Scenario: A folder named like a conflict copy

- **WHEN** the setting is true and the vault holds `Proj (conflict 2026-05-29-19-45-39)/Plan.md`
- **THEN** `Proj (conflict 2026-05-29-19-45-39)/Plan.md` SHALL be indexed

#### Scenario: Off by default

- **WHEN** the setting is unset and the vault holds `N (conflict 2026-05-29-19-45-39).md`
- **THEN** that note SHALL be indexed exactly as before this change

### Requirement: An excluded conflict copy SHALL be classified at the scan as present but not indexable, by name, without being statted or read

While the setting is true, `_scan_vault` SHALL record a matching path in `not_indexable` with the reason `EXCLUDED_CONFLICT_COPY`. It SHALL do so after the existing path-encodability check and before any stat or read of the file. The value of the setting SHALL be read once per pass and passed to the scan. An excluded path SHALL therefore be in `seen` and SHALL NOT be in `files`, so:

- it SHALL NOT be upserted;
- it SHALL NOT be a move source or a move destination;
- any row at its path in the locked rows SHALL be deleted in the pass's transaction through the ordinary prune, with its embeddings, outgoing links and task rows, and inbound links SHALL keep their `target_path` with a NULL `target_note_id`.

An exclusion SHALL NOT be a skip. It SHALL NOT:

- fail, abort or restart a pass;
- appear in `skips`, `unverified` or `withholding`;
- withhold a re-derive's record;
- keep the scope due for a full-hash pass;
- count toward `walk_incomplete`, any other `indexer_health` counter or the run's `error` text.

An excluded path SHALL NOT be logged by the per-path not-indexable WARNING. `IndexPassResult.not_indexable` SHALL NOT count it, and `IndexPassResult.excluded` SHALL count it. A quarantine entry at an excluded path SHALL be cleared.

#### Scenario: Turning the setting on removes existing rows and nothing else

- **WHEN** a scope holds indexed and embedded rows for `A.md` and `A (conflict 2026-05-29-19-45-39).md`, the copy carries a task, `B.md` links to `[[A (conflict 2026-05-29-19-45-39)]]`, and the server restarts with the setting true
- **THEN** after the first pass the copy SHALL have no `notes_metadata`, `note_embeddings` or `note_tasks` rows, `B.md`'s link SHALL be dangling, `A.md`'s row id, hash and vectors SHALL be unchanged, the file SHALL be byte-identical on disk, the run SHALL be recorded without an error, and the backstop clock SHALL advance

#### Scenario: The excluded file is never read

- **WHEN** the setting is true and a full-hash pass runs over a vault holding a conflict copy whose read would raise
- **THEN** the pass SHALL NOT attempt to read it, SHALL record no skip, and SHALL advance the backstop clock

#### Scenario: A second pass writes nothing

- **WHEN** the pass after the one that removed the copy's row runs with nothing changed
- **THEN** it SHALL write no row and SHALL log no removal

#### Scenario: Turning the setting off indexes the copies as new notes

- **WHEN** the server restarts with the setting false over a vault still holding `A (conflict 2026-05-29-19-45-39).md`
- **THEN** the next pass SHALL upsert it as a new row, and a dangling link whose `target_path` names it SHALL be resolved as for any new note

#### Scenario: A conflict copy is never a move destination

- **WHEN** the setting is true, the indexed `B.md` is renamed on disk to `B.sync-conflict-20260529-194539-ABC2DEF.md`, and the pass runs before `B.md` is rewritten
- **THEN** `B.md`'s row SHALL be pruned, no row SHALL exist at the conflict path, and no id-preserving move SHALL be recorded, including after a quarantine restart of the attempt

#### Scenario: A vanished conflict-shaped row is an ordinary vanished row

- **WHEN** the setting is turned on in the same interval in which `C (conflict 2026-05-29-19-45-39).md` (holding a row) is renamed on disk to `C2.md` with identical bytes
- **THEN** the pass SHALL pair the row to `C2.md` as an id-preserving move, exactly as without this change

#### Scenario: A directory that cannot be listed

- **WHEN** the setting is true, `sub/` cannot be listed and holds the row `sub/C (conflict 2026-05-29-19-45-39).md`, and `D (conflict 2026-05-29-19-45-39).md` at the root holds a row
- **THEN** the `sub/` row SHALL be kept, `D`'s row SHALL be deleted, and the run's `walk incomplete` text SHALL be the same as it would be without the setting

#### Scenario: The empty-root guard counts excluded files as discovered

- **WHEN** the setting is true and the root holds no `.md` file at all over an existing index
- **THEN** the pass SHALL be indeterminate and delete nothing, exactly as before this change
- **AND WHEN** instead the root's only `.md` files are conflict copies over an existing index
- **THEN** the pass SHALL be determinate, and SHALL delete every row: the copies' rows as excluded, the others as vanished

#### Scenario: A re-derive with exclusions records

- **WHEN** a re-deriving pass runs with the setting true over a scope holding conflict-copy rows and no other skip
- **THEN** the conflict-copy rows SHALL be deleted and the re-derive SHALL be recorded

#### Scenario: A quarantined note becomes excluded

- **WHEN** a note is quarantined and is then renamed on disk to a conflict-shaped name with the setting true
- **THEN** its quarantine entry SHALL be cleared and no row SHALL exist at either path

### Requirement: Tools that address a file by exact path SHALL work on an excluded conflict copy, and index-backed tools SHALL NOT show it

While the setting is true, the following tools SHALL behave on an excluded path exactly as on any note or file, except for the notice below: `read_note`, `read_file`, `edit_note`, `set_frontmatter`, `create_note`, `move_note`, `delete_note`, `list_files`, `write_file`, `import_from_url`, the transfer upload, `complete_task`, `update_task` and `add_task`. When a task write's write-time refresh finds no row at the path, it SHALL report `index: may lag (not_indexed)`.

**Once a pass has completed after the path became excluded**, the following SHALL NOT return an excluded note as a note: `list_notes`, `get_recent`, `get_tags`, `keyword_search`, `semantic_search`, `hybrid_search`, `find_related`, `find_orphans`, `get_backlinks`, `get_neighborhood`, `get_links`, `list_tasks` and `task_agenda`. `add_task`'s index-side duplicate check SHALL NOT consider it either. Before that pass, a row that `move_note` re-keyed onto an excluded name SHALL remain indexed, together with its vectors, links and task rows (limitation L10). `move_note` SHALL NOT remove the row itself.

**The notice.** When the setting is true and the basename of the note a call published matches a conflict pattern, `create_note`'s success result and `move_note`'s fully successful result (not its partial or refused outcomes) SHALL end with one extra line, after a single `\n`, with exactly this text:

`note: this name matches the sync-conflict exclusion (INDEX_EXCLUDE_CONFLICT_FILES); the note is stored but not indexed, so searches and listings will not return it once the next index pass has run. Read it by exact path.`

For `create_note` the note is the created path, and for `move_note` the destination. With the setting false, or a basename that matches no pattern, both results SHALL be byte-identical to their form before this change. `write_file`, `import_from_url` and the transfer upload are byte transport and SHALL carry no notice (limitation L11).

**The rewrite notice.** Backlink discovery is keyed on the source's index row, which the exclusion removes. So when the setting is true, `rewrite_links=True`, and the **source** basename matches a conflict pattern, `move_note`'s fully successful result SHALL carry one more line, after a single `\n`, with exactly this text:

`note: the source name matches the sync-conflict exclusion (INDEX_EXCLUDE_CONFLICT_FILES), so it may no longer be in the index and links to its old name may not have been found or rewritten; search for the old name and fix any by hand.`

When both notices apply (a move from one excluded name to another), the rewrite notice SHALL come first and the exclusion notice last, each exactly once. The move SHALL NOT be refused (limitation L14). With the setting false, `rewrite_links=False`, or a non-matching source name, the result SHALL carry no rewrite notice.

No tool's name, input schema, title, annotations or description SHALL change, and `tests/fixtures/tools_list_full_v2.json` SHALL stay byte-identical.

#### Scenario: Recovering the only surviving version

- **WHEN** the setting is true, `A.md` is gone, and only `A (conflict 2026-05-29-19-45-39).md` holds the content
- **THEN** `keyword_search` for a term unique to it SHALL return nothing, `read_note` on its path SHALL return its full content and `content_hash`, `move_note` from that path to `A.md` SHALL succeed, and after the next pass `A.md` SHALL be indexed

#### Scenario: Moving an indexed note to a conflict-shaped name

- **WHEN** the setting is true and `move_note` moves the indexed `E.md` to `E (conflict 2026-05-29-19-45-39).md`
- **THEN** the move SHALL succeed, its result SHALL end with the notice line, the row SHALL be at the destination path until the next pass, and after the next pass no row, vector, outgoing link or task row SHALL exist for either path

#### Scenario: A link-rewriting move out of an excluded name

- **WHEN** the setting is true, `B.md` links to `[[A (conflict 2026-05-29-19-45-39)]]`, a pass has removed the copy's row, and `move_note` moves the copy to `A.md` with `rewrite_links=True`
- **THEN** the move SHALL succeed, `B.md` SHALL be unchanged, and the result SHALL end with `\n` and the rewrite notice line
- **AND WHEN** the same move targets another excluded name
- **THEN** the result SHALL end with the rewrite notice line and then the exclusion notice line, each once

#### Scenario: Creating a note at an excluded name

- **WHEN** the setting is true and `create_note` creates `Review (conflict 2026-05-29-19-45-39).md`
- **THEN** the result SHALL be today's result followed by `\n` and the notice line, and `read_note` on that path SHALL return the content

#### Scenario: The notice is absent otherwise

- **WHEN** the setting is false and `create_note` / `move_note` publish a conflict-shaped name, or the setting is true and they publish `Review.md`
- **THEN** each result SHALL be byte-identical to its form before this change

### Requirement: The exclusion SHALL be reported in the log and SHALL NOT change `/health`

The system SHALL report the exclusion in the log as follows while the setting is true. The only other surface is the dashboard line in `panel-ops-health`.

- the lifespan SHALL log one INFO line at startup stating that sync-conflict copies are not indexed and naming `INDEX_EXCLUDE_CONFLICT_FILES`;
- a pass with a non-zero `excluded` count SHALL log one INFO line stating the count and naming the setting, and a pass with zero SHALL log no such line;
- a pass that deleted rows because of the exclusion SHALL log one INFO line stating the count and naming the paths through the existing `_format_skips` (at most `SKIP_REPORT_LIMIT`, 20, paths, then `, and N more`), and those rows SHALL NOT be counted or named by the present-but-not-indexable WARNING;
- after each committed pass the indexer SHALL hold, in process memory, that scope's `excluded` count.

The exclusion SHALL NOT write to `indexer_runs.error` and SHALL NOT change `/health`'s body, status or HTTP code.

#### Scenario: `/health` is unchanged

- **WHEN** a pass excludes three conflict copies and removes two rows
- **THEN** `/health` SHALL be byte-identical to its answer with the setting false over the same index state, and the run's `run_outcome` SHALL be `ok`

#### Scenario: The log names the removal once

- **WHEN** a pass removes the rows of 23 conflict copies
- **THEN** exactly one INFO line SHALL state 23 and name 20 paths followed by `, and 3 more`, and no WARNING SHALL name any of the 23
