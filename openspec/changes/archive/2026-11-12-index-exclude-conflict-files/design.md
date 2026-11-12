# Design: excluding sync-conflict copies from the index (#392)

## Context

The pass discovers notes with `discover_markdown_files_at` (dot-directories skipped, directory symlinks not descended, only names ending `.md` yielded) and classifies them in `_scan_vault` on a worker thread, ahead of the generation lock (C1–C8 in `docs/architecture/indexing-and-embeddings.md`). The locked attempt `_index_vault_attempt` then decides moves, upserts and the prune against the locked rows.

One mechanism already does almost exactly what this change needs: **present but not indexable** (#308 D7). A path in `ScanResult.not_indexable` is kept in `seen`, so it is never a vanished file and never a move source. It is never in `to_upsert`, so it is never a move destination. Its row, if any, is added to `deleted_paths` after move detection, so the ordinary prune deletes it, and embeddings, outgoing links and task rows cascade while inbound links fall back to dangling (`target_note_id` is `ON DELETE SET NULL`). It is not a skip, so it withholds no re-derive stamp and does not keep the scope due for a full-hash pass. The change feed records the removal as `deleted reason=not_indexable`, or `quarantined` for a quarantine. The quarantine (#308 D3) reuses the same mechanism through its own reason value. This change reuses it again.

## Decisions

### D1. A boolean with a fixed pattern set, not a general glob list

`INDEX_EXCLUDE_CONFLICT_FILES: bool = False`.

The alternative was `INDEX_EXCLUDE_PATTERNS: list[str]` (fnmatch globs, like `EMBEDDING_EXCLUDE_PATTERNS`). It was rejected for four reasons.

- **It is a larger contract.** A glob list is a general "hide part of my vault from agents" control. Each of its uses (a folder, a tag-like name, `*`) would need its own reasoning against the #309 empty-root guard, re-derive withholding and the feed. The issue asks only for conflict copies.
- **Globs cannot state the safety property.** The protection against hiding a real note is the literal marker plus a full fixed-width numeric timestamp anchored before `.md`. fnmatch can spell that only as long `[0-9][0-9]…` chains that every operator would have to get right. A typo like `* (conflict*` would hide every note whose title contains "(conflict".
- **The common case should not need research.** Operators should not have to look up each sync tool's naming. The server ships the patterns, cites their sources, and tests them against near-miss titles.
- **A miss fails safe.** A conflict copy that no pattern matches stays indexed, which is today's behaviour. A false match would hide a real note. Fixed, strict patterns put every error on the safe side.

If a later issue needs general exclusion, it can add a glob list beside the boolean. Nothing here blocks that.

### D2. The pattern set, with sources

Matching is applied to the **basename** only, the final path component exactly as the walk yields it. It uses `re.fullmatch` with explicit `[0-9]` digit classes rather than `\d`, because Python's `\d` matches non-ASCII digits. It is **case-sensitive**, including the `.md` suffix (the walk yields only names ending in lower-case `.md`). Directory names are never matched: a file inside a folder named like a conflict copy is not excluded. Each pattern needs at least one character before its marker.

| # | Tool | Basename pattern (regex, whole name) | Example | Source |
| --- | --- | --- | --- | --- |
| P1 | Syncthing | `.+\.sync-conflict-[0-9]{8}-[0-9]{6}-(?:[A-Z2-7]{7})?\.md` | `Note.sync-conflict-20260529-194539-ABC2DEF.md` | docs.syncthing.net/users/syncing.html, "Conflicting Changes": `<filename>.sync-conflict-<date>-<time>-<modifiedBy>.<ext>`. Source `lib/model/folder_sendrecv.go` `conflictName`: time format `20060102-150405`, then `ShortID.String()`, which is seven base32 characters (A–Z, 2–7) or empty for a zero ID (`lib/protocol/deviceid.go`). |
| P2 | Nextcloud desktop (current) | `.+ \(conflicted copy (?:[^()/]* )?[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{6}\)\.md` | `Note (conflicted copy alice 2026-05-29 194539).md` | nextcloud/desktop `src/common/utility.cpp` `makeConflictFileName`: inserts ` (conflicted copy ` + optional user name (parentheses replaced by `_`) + space + `yyyy-MM-dd hhmmss` + `)` before the extension. |
| P3 | Nextcloud / ownCloud desktop (legacy) | `.+_conflict-[0-9]{8}-[0-9]{6}\.md` | `Note_conflict-20260529-194539.md` | Nextcloud desktop manual, Appendix B "History and Architecture" (example `message_conflict-20160101-153110.txt`). `Utility::isConflictFile` still recognises `_conflict-`. |
| P4 | Dropbox | `.+ \([^()/]+(?:'\|’)s conflicted copy [0-9]{4}-[0-9]{2}-[0-9]{2}(?: \([0-9]+\))?\)\.md` | `Note (Alice's conflicted copy 2026-05-29).md` | help.dropbox.com "conflicted copy": "The editor's username, 'conflicted copy', and the save date will be added to the filename." The help page gives the rule but no literal example. The parenthesised `<name>'s conflicted copy YYYY-MM-DD` shape, with an optional ` (N)` counter, is the form Dropbox clients produce. The apostrophe is either U+0027 (apostrophe) or U+2019 (right single quotation mark). This is the least documented pattern; a variant it misses stays indexed. |
| P5 | Obsidian Sync (1.9.7+, "Create conflict file") | `.+ \(Conflicted copy .+ [0-9]{12}\)\.md` | `Note (Conflicted copy MyMacBook2 202411281430).md` | obsidian.md/help/sync/troubleshoot: "original-note-name (Conflicted copy device-name YYYYMMDDHHMM).md". |
| P6 | Form observed in the issue | `.+ \(conflict [0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{2}-[0-9]{2}-[0-9]{2}\)\.md` | `Note (conflict 2026-05-29-19-45-39).md` | Six files of this shape in the reference vault (2026-05 to 2026-09). **No primary documentation found.** Syncthing, Nextcloud, Dropbox and Obsidian Sync use other forms, and a GitHub code search for the format string found no producer. The producer is unidentified. The owner kept P6 because it is the form the issue reports (spec review, 2026-11-12). |

The P4 table cell writes the alternation with an escaped bar because of the table. The pattern is `(?:'|’)`.

Rejected: Nextcloud's **case-clash** rename (` (case clash from yyyy-MM-dd hhmmss)`). It is not a copy of another note. It is a distinct file that clashed with another name on a case-insensitive filesystem, so hiding it would hide a real note.

Also rejected: the loose recognisers the tools use themselves (Syncthing's `contains(".sync-conflict-")`, Nextcloud's `contains("(conflicted copy")`). They match a title that merely mentions the marker.

**What the patterns do and do not catch.** Every pattern needs a tool-specific literal marker (`.sync-conflict-`, ` (conflicted copy `, `_conflict-`, `'s conflicted copy `, ` (Conflicted copy `, ` (conflict `), **immediately followed** by a complete fixed-width numeric timestamp and the closing delimiter, **immediately before** `.md`. So a title that only *mentions* a conflict does not match: "Crazy weekend - …, conflict, and bubbling life" (in the reference vault) and `2026-05-29 conflict.md` both stay indexed. **A title that exactly matches a pattern is excluded, by design, whoever wrote it.** The server cannot tell a sync tool's copy from a note deliberately named `Incident (conflict 2026-05-29-19-45-39).md`. That is limitation L1. `DEPLOYMENT.md` lists the full pattern set so an operator can check their own naming before turning the setting on.

The module is `src/services/conflict_copies.py`: `CONFLICT_PATTERNS` (an ordered tuple of `(tool, compiled regex)`), `is_conflict_copy(name: str) -> bool`, and a comment above each pattern that cites its source. It is pure, takes no settings, and is imported by the indexer only.

### D3. Decided at the scan, by name, before any stat or read

In `_scan_vault`, immediately after the existing D7 name check (`_path_not_indexable_reason`), the scan checks the setting. When `settings.index_exclude_conflict_files` is true and `is_conflict_copy(found.name)` holds, the path goes into `result.not_indexable` with the reason constant `EXCLUDED_CONFLICT_COPY` (`"excluded as a sync-conflict copy (INDEX_EXCLUDE_CONFLICT_FILES)"`). Then it `continue`s. `_scan_vault` receives the setting as a keyword argument (`exclude_conflicts: bool`) read once per pass in `_index_vault_pinned`, so a single pass never sees two values.

Consequences, each by the existing D7 path. Nothing new is invented:

- **The file is never statted, opened or read by the indexer.** Exclusion costs one regex per discovered name.
- **It stays in `seen`.** So it is never a vanished file: never pruned as `removed` and never a move source.
- **It is never in `files` or `to_upsert`.** So it is never a move destination and never upserted.
- **A row at its path, if any, is deleted.** The deletion runs in the pass's one transaction through `not_indexable_rows → deleted_paths`, after move detection. Embeddings (`note_embeddings`), outgoing links and task rows (`note_tasks`) cascade. Inbound links keep their `target_path` and lose `target_note_id`, so they are dangling. `derived_under` goes with the row. The keyword vector and excerpt are columns of the row.
- **It is not a skip.** It is not in `skips`, `unverified` or `withholding`. It does not block the full-hash clock (D12), withhold a re-derive stamp (#308 D8, #311), or count toward `walk_incomplete` or any `indexer_health` counter.
- **Quarantine.** It is never in `files`, so it is never quarantined. An existing quarantine entry at that path is cleared by the existing `rel in scan.not_indexable` rule.
- **C4 (locked re-read).** It is not in `scan.files` and not `NOT_UTF8_CONTENT`, so it is never re-read. A row at it that moved between the snapshot and the lock is still deleted, because `existing` comes from the locked rows.
- **C5 (deferral).** It applies only to rows the walk did not see. An excluded path was seen, so it is never deferred. A conflict-shaped row the walk did not see (its file is gone) is an ordinary vanished row (see D6).

Three places must distinguish the new reason from the D7 reasons:

1. **No per-path WARNING.** `_warn_not_indexable` is not called for an excluded path, because the operator chose this and a WARNING per file per tick would be noise. Instead, see D7.
2. **`IndexPassResult.not_indexable`** keeps counting only the D7 reasons (it is computed as today minus quarantined, minus excluded). A new `IndexPassResult.excluded: int` counts excluded paths.
3. **Every comment** that says "a path not indexable by its *name* can have no row" (the C4 loop, `_collect_feed_events`) is amended. An excluded path is decided by name and *can* have a row from before the setting was turned on. The code paths are already correct for that case: C4 skips it, and the prune deletes its row.

### D4. What each tool does with an excluded path

The index is the only thing that changes. A tool that reads or writes the file by exact path behaves exactly as it does for any note. A tool that answers from the index does not see the file.

| Tool | Behaviour for an excluded conflict copy |
| --- | --- |
| `read_note`, `read_file` | Unchanged: reads the file, returns its `content_hash`. |
| `edit_note`, `set_frontmatter` | Unchanged: edits the file. No row is created. The next pass still excludes it. The tool's own `modified` change-feed event is recorded as today. |
| `create_note` at a conflict-shaped name | Creates the file and records its `created` event, as today. The pass never indexes it. When the setting is on, the success result ends with the one-line exclusion **notice** (spec, index-integrity). |
| `write_file`, `import_from_url`, transfer upload at a conflict-shaped `.md` name | Unchanged byte transport, with no notice (L11). |
| `move_note` from a conflict-shaped name to an ordinary name | Unchanged: the file moves. With no row at the source, no row is updated. The next pass indexes the destination as a new note (`created`). This is the **recovery path**, and `DEPLOYMENT.md` says so. |
| `move_note` from an ordinary name to a conflict-shaped name | The row follows the path in `move_note`'s metadata transaction, as today, and stays indexed (vectors, links and task rows too) **until the next pass** excludes the destination and deletes it (`deleted reason=not_indexable`; L10). `move_note` does not remove the row itself, because the metadata transaction is the destructive-write surface and the gap closes within one tick. A fully successful result ends with the exclusion notice. |
| `delete_note` | Unchanged: soft or permanent delete of the file. |
| `list_files` | Unchanged: it reads the directory, not the index, so the file is listed. |
| `list_notes`, `get_recent`, `get_tags`, `keyword_search`, `semantic_search`, `hybrid_search`, `find_related`, `find_orphans`, `get_backlinks`, `get_neighborhood` | Index-backed: the note is absent. |
| `get_links` on another note that links to the copy | The link is reported **dangling** (its target has no row). Links to conflict copies are rare, because sync tools create them and no one links to them. |
| `get_links` / `get_backlinks` on the copy itself | It has no row, so these answer as for any path the index does not hold. |
| `list_tasks`, `task_agenda`, `add_task`'s index-side duplicate check | Index-backed: the note's tasks are absent. |
| `complete_task`, `update_task` by `path` + id | They re-read and re-derive ids from the file's bytes, so a valid id still writes. The write-time refresh finds no row and reports `index: may lag (not_indexed)`, as for any unindexed note. Here "lag" never ends while the setting is on (L5). No listing prints an id for an excluded note, so an agent only has one from before the exclusion took effect. |
| `add_task` with `path` = the copy | Inserts the line. The refresh reports `index: may lag (not_indexed)`. |
| `get_changes` | See D5. |

No tool's name, schema, title, annotations or description changes. `tests/fixtures/tools_list_full_v2.json` stays byte-identical.

### D5. The change feed: `deleted reason=not_indexable`, and nothing for a row-less path

The feed spec says the pass records `deleted` for **every** row it removes, with `removed` for an ordinary prune, `not_indexable` for a present-but-not-indexable path and `quarantined` for a quarantine removal. There were three candidates for the exclusion removal:

- **`deleted reason=removed`.** Rejected: it is false. `get_changes` documents `removed` as "the file is gone", and this file still exists.
- **Nothing.** Rejected. It breaks the feed's own invariant ("every row it removes"), and an agent that keeps a mirror of the vault from the feed would then believe the note is still indexed and look for it in `list_notes` in vain.
- **A new reason `excluded`.** Rejected. `note_events.reason` is held by the CHECK `ck_note_events_reason` (migration 033), so a new value needs a migration, which is out of scope. It would also change `get_changes`'s description, which lists the reasons and is part of the tool contract.
- **Chosen: `deleted reason=not_indexable`, attributed `human/sync` (source `index`).** The `get_changes` description already defines `not_indexable` as "still on disk but cannot be indexed", which is exactly true here. The attribution is the pass's, as for every index-discovered event: the server cannot name who changed the setting, and the actor (`by human/sync`) means "discovered by the index", not "edited by a person".

The existing mapping in `_collect_feed_events` already produces this: any `not_indexable_rows` path whose reason is not `QUARANTINED` gets `REASON_NOT_INDEXABLE`. The implementation adds a test, not a branch.

**A row-less excluded path records nothing.** The `created reason=not_indexable` event is reserved for `NOT_UTF8_CONTENT`. That fault is in a note the owner meant to be indexed, and an agent should hear about it. An exclusion is the operator's configuration, and every new conflict copy announcing itself would flood the feed. The existing condition (`not_indexable[p] == NOT_UTF8_CONTENT`) already ensures this.

**Suppression** works as before. A second pass finds no row and records nothing. If a tool's `deleted reason=not_indexable` is the latest event at the path (it never is, since tools do not emit that), the existing same-reason suppression applies.

**Turning the setting off.** The first pass after the restart finds each conflict copy as a path absent from the locked rows, so it upserts it. The feed records `created` with the content hash, `by human/sync`, and `get_changes` already documents `created` as covering "a note that became indexable". Move pairing applies to the new paths as to any new file. A conflict copy whose bytes equal a row that vanished in the same pass is paired as that row's move. That is the existing same-hash heuristic, and it is correct about the bytes.

**Overflow.** The first pass after turning the setting on removes every conflict copy's row at once. If that exceeds `CHANGE_FEED_MAX_EVENTS_PER_PASS` (default 2,000), the rest is one `gap pass_overflow` line, as for any large pass.

### D6. Move pairing

- **A present excluded file is never a source or destination** (D3).
- **A vanished row at a conflict-shaped path is an ordinary vanished row.** Suppose the setting is on, the row was not yet removed, and the file was renamed or deleted in the same interval. Its path is not in `seen`, so the existing rules apply: it may be paired with a same-hash new path (the copy was renamed to an ordinary name, which is a real move), or it is pruned with `reason=removed` (the file is gone, which is true). It cannot be paired with a present excluded file.
- **An indexed note renamed to a conflict-shaped name between passes** (by a sync tool's rename-then-rewrite, or by hand) loses its pairing. The destination is excluded, so the source row is pruned as `deleted reason=removed`, which is true for that path. If the sync tool then rewrites the original path, the next pass records `created` there, and the note gets a new row id and is re-embedded (vector reuse is per note id). Today the same sequence would pair the row to the conflict copy and then create the original. The churn is the same; only the copy's row differs. Accepted as L3.

### D7. Reporting: log lines, an in-process count, one dashboard line; never `indexer_runs.error`

- **Startup.** When the setting is on, the lifespan logs one INFO line: `Index exclusion: sync-conflict copies are not indexed (INDEX_EXCLUDE_CONFLICT_FILES=true)`.
- **Per pass.** A pass with `excluded > 0` logs one INFO line `Excluded N sync-conflict cop{y|ies} from the index (INDEX_EXCLUDE_CONFLICT_FILES)<log_suffix>`. A pass with zero logs nothing.
- **Per removal.** A pass that deleted rows because of the exclusion logs one INFO line `Removed the index row(s) of N excluded sync-conflict cop{y|ies}<log_suffix>: <paths>`. The paths come from the existing `_format_skips`, which renders up to `SKIP_REPORT_LIMIT` (20) paths through `_loggable_path`, then `, and N more`. Those rows are kept out of the existing WARNING "Removing the index row(s) of N present-but-not-indexable path(s)", which then counts only D7 and quarantine rows.
- **Not in `indexer_runs.error`.** `run_outcome` labels a run `failed` when any line of `error` is not a quarantine line, so an exclusion line there would mark a healthy pass failed.
- **In-process count.** `indexer.excluded_conflict_copies: dict[scope, int]` is set from each **committed** pass's `excluded` (scope = `user_id` or `None`). It lives in memory only, like the rechunk counts, and is reset on restart. The first pass after a start is a full-hash pass at startup, so the count is back within one pass.
- **Dashboard.** While the setting is on and the viewer's scope has a count, `dashboard.html` renders one muted line beside "Last run": `{{ n }} sync-conflict cop{y|ies} not indexed (INDEX_EXCLUDE_CONFLICT_FILES)`. It shows `0` when the pass excluded nothing, and nothing at all when the setting is off or no pass has counted yet. The route passes `excluded_conflicts` (an int or `None`) in the context. The template's condition is `is integer`, and the conditional sits on the existing line after its `</span>`, so a `None` or a missing key renders the pre-change page byte for byte. (Implementation note: a pass with the setting off drops its scope's entry.) **The template change is Jinja text only**: no `<script>`, no `style=`, no `on*=`, no new class, only existing utility classes. Because it is a panel template change, the owner walks the dashboard with devtools open and zero CSP violations is part of the bar (CLAUDE.md). The count is for the viewer's **own vault scope**: `user.id` in multi-user mode, administrators included, and the single-user scope (`None`) otherwise. It is deliberately **not** `_scope_user_id`, which returns `None` ("no filter") for an administrator and would show an administrator no count for their own vault (spec review r1). One tenant never sees another's volume.
- **`/health` does not change.** No field, no status, no counter.

### D8. Interaction with the #309 guards

- **A directory the walk could not list.** Rows at or beneath it are protected (neither pruned nor paired), including rows at conflict-shaped names. They stay until the directory is listable, and the run is `walk incomplete` as it already is. The exclusion adds nothing to that report. A protected conflict-copy row is still served meanwhile (L6).
- **An empty root (D3 of #309).** The predicate is "the walk discovered no `.md` file", decided on `seen`, and an excluded file is discovered and `seen`. So a vault whose only notes are conflict copies is determinate, and the pass deletes every other row as `removed` plus the copies' rows as `not_indexable`. This is unchanged on purpose. The guard exists to catch a mount that did not mount, and a mount that holds conflict copies did mount. This falls under #309's own L1 (a wrong but non-empty mount is not detected).
- **The provenance discard** is untouched.

### D9. The re-derive, the full-hash pass, the quarantine and the embed pass

- **Re-derive (#311).** An excluded path is not a skip, and its row is deleted, so the invariant re-read never sees it as not current. A re-derive with exclusions can record.
- **Full-hash pass (D12).** Excluded files are not read on any pass, including the backstop, and that does not block the clock: the backstop's claim is about indexed rows, and no row can exist at an excluded path after the pass commits.
- **Quarantine (#308 D3).** See D3. A note quarantined before the setting was turned on is cleared, because its path is now in `not_indexable`.
- **Embed pass.** The backlog, the exclusion sweep (`EMBEDDING_EXCLUDE_PATTERNS`) and certification all read `notes_metadata`. An excluded path has no row, so the embed pass never sees it. The sweep gate (D13) is unaffected.
- **Task rows (#350/#428).** These cascade with the row. The write-time refresh finds no row (`not_indexed`), as in D4.

### D10. Deployment-wide, read at the start of each pass

The setting is a `Settings` field (env only), so changing it means a restart, and the first pass after a restart is a full-hash pass. It applies to every scope. In multi-user mode it excludes conflict copies in every tenant's vault. A per-user variant would need:

- a durable per-scope value (a column in a per-user settings table, so a migration);
- a panel control and its browser pass;
- the pass reading the value per scope;
- the in-process count keyed as now.

That is out of scope (L4).

Validation: pydantic's boolean parsing. No cross-field rule.

## Alternatives rejected

- **General exclusion globs** (D1).
- **Using `EMBEDDING_EXCLUDE_PATTERNS`.** It removes vectors only. The note stays in `keyword_search`, `list_notes`, the graph and the task listings, so the duplicate rows remain.
- **Excluding at the walk** (not yielding the file from `discover_markdown_files_at`). The file would then be "not seen", and an existing row would be pruned as `deleted reason=removed`, which is false. It could also be **paired as a move source** with a same-hash new file, rewriting an unrelated note in place, which is exactly #309's failure. Deciding at the scan keeps it `seen`.
- **Reading the file before deciding** (for example, comparing it with its original). That costs a read per copy per full-hash pass and adds a content heuristic, and the name already decides.
- **Marking copies in search output instead of excluding them.** That would change every search tool's output and the tool contract, and agents would still have to filter.
- **A new feed reason** (D5).
- **Counting into `indexer_runs.error` or `/health`** (D7).
- **Nextcloud case-clash files and the tools' loose recognisers** (D2).

## Accepted limitations

- **L1. Deliberate look-alike titles.** A note whose author named it to end with a sync tool's exact marker and timestamp (for example a note documenting a Syncthing conflict, named after the file) is excluded while the setting is on. Remedy: rename it. The setting is off by default.
- **L2. Unmatched variants stay indexed.** A sync tool or version whose naming differs from D2 (a localised Dropbox client, a future Obsidian Sync format, a tool not listed) is not excluded. That is the status quo. Adding a pattern is a code change with a source citation.
- **L3. Rename-then-rewrite churn.** See D6. When a sync tool renames the indexed original to the conflict name before rewriting the original path, the original's row id changes and the note is re-embedded.
- **L4. Deployment-wide.** One value for every tenant. See D10.
- **L5. Task writes on an excluded note report `index: may lag (not_indexed)`**, and the lag does not end while the setting is on. The write itself stands.
- **L6. A protected row is still served.** A conflict-copy row beneath a directory the walk cannot list stays indexed until the directory is listable (D8).
- **L7. The feed attributes the removal to `human/sync`.** The operator's configuration change is not attributable to a principal.
- **L8. The dashboard count is in process memory.** It reads nothing until the first pass after a restart (the startup pass), and it describes the last committed pass, not the live directory.
- **L9. A toggle-off `created` can be suppressed.** Suppose an agent edited or created an excluded copy by exact path while the setting was on (an MCP content event, the latest at that path), and the setting is then turned off. The pass upserts the copy but records no `created`, because the existing suppression treats the MCP event with the same hash as already saying it. The note is fully indexed; only the feed line is missing. Pinned by a test, not changed (spec review r1, owner disposition).
- **L10. A row `move_note` re-keyed onto an excluded name stays indexed until the next pass.** Index-backed tools can return it during that interval. The move's result carries the notice.
- **L11. Byte-transport writers give no notice.** `write_file`, `import_from_url` and transfer uploads that land a conflict-shaped `.md` name say nothing about the exclusion. They are byte transport and return no note-level output; `DEPLOYMENT.md` says so.
- **L12. `list_tasks(scope="all")` cannot see tasks in an excluded note**, and its output says nothing about them. Every task scope is defined over indexed notes (the note-tasks delta). The task-query output is pinned, and the setting is deployment-wide.
- **L14. A link-rewriting move out of an excluded name cannot find the links to it.** `move_note(rewrite_links=True)` discovers backlinks through the source's index row (`note_links.target_note_id`), and the exclusion removed that row. The move succeeds, links in other notes are not rewritten, and the result carries the one-line rewrite notice (implementation review r1). The notice says the source *may no longer* be in the index (review r2): between turning the setting on and the next pass, the row is still there and its backlinks are rewritten normally. This is **not new**: a move out of any note the index does not yet hold, such as one created seconds ago, inserts a synthetic id that matches no link and is just as silent. That pre-existing case is unchanged here.
- **L15. A path containing a line break can forge a notice-looking line in `move_note`'s result.** `move_note` interpolates paths unescaped into every line of its result, and a newline is allowed in a vault path. That is how `move_note` already renders every path today; this change does not introduce it, and escaping would alter `move_note`'s output outside this change's scope. A follow-up issue owns it (implementation review r2).
- **L13. The toggle-off baseline.** A scope whose only notes were excluded copies has no rows, so turning the setting off records one `reset`, not per-note `created` lines (the existing baseline rule).

## Adversarial scenarios a reviewer should trace

1. **Toggle on over an existing index.** Scope with rows `A.md` and `A (conflict 2026-05-29-19-45-39).md`, both embedded and with tasks. Restart with the setting on. Expect: the first (full-hash) pass deletes the copy's row, embeddings and task rows. Inbound links to it become dangling. The feed records exactly one `deleted reason=not_indexable by human/sync` for the copy and nothing for `A.md`. The INFO removal line names the copy. The run is `ok`, `/health` is unchanged, the backstop clock advances, and the second pass writes nothing and records nothing.
2. **Toggle off again.** Restart with the setting off. Expect: the copy is upserted as a new row and the feed records `created` with its hash. `A.md` is untouched. A dangling link that targets the copy's path resolves as it does for any new note.
3. **The copy is the only surviving version.** `A.md` was deleted, and only `A (conflict …).md` holds the content. With the setting on, the copy is absent from search and `list_notes`, but `read_note` returns it and `move_note` to `A.md` succeeds, after which the next pass indexes `A.md` (`created`, or `moved` if `A.md`'s old row vanished in the same pass with the same hash). Nothing in the server deletes or renames the copy.
4. **A conflict copy as a move target.** The indexed `B.md` is renamed by a sync tool to `B.sync-conflict-20260529-194539-ABC2DEF.md`, and `B.md` is not yet rewritten when the pass runs. Expect: no pairing. `B.md`'s row is pruned with `deleted reason=removed`, no row is created for the copy, and the next pass (after the tool writes `B.md`) records `created B.md`. Check that the copy can never become an id-preserving move destination, including on a quarantine restart (`_index_vault_attempt` is re-run with copies of the scan verdicts; the copy stays in `not_indexable`).
5. **A conflict-copy row is renamed away in the same pass the setting turns on.** Its path is not seen. It is paired with the new ordinary path if the hashes are equal, otherwise pruned as `removed`. Both are true.
6. **A directory that cannot be listed in the same pass.** `sub/` is unlistable and holds the row `sub/C (conflict …).md`, while `D (conflict …).md` is at the root. Expect: the `sub/` row is protected and kept, the root copy's row is deleted, the run reads `walk incomplete: 1 dir(s) not listed: sub`, and the exclusion adds nothing to that line.
7. **A legitimate title that looks like a conflict.** Names such as `Crazy weekend - birthday, conflict, and bubbling life.md`, `2026-05-29 conflict.md`, `Conflict resolution (draft).md`, `Notes (conflict 2026-05-29).md` (date only), `Note (conflicted copy).md` (no timestamp), `Note (CONFLICT 2026-05-29-19-45-39).md` (case), `Note (conflict 2026-05-29-19-45-39) v2.md` (text after the marker), `Note.sync-conflict-2026-05-29.md`, and names with non-ASCII digits in the timestamp. **None** is excluded. The test lists them.
8. **A folder named like a conflict copy.** `Proj (conflict 2026-05-29-19-45-39)/Plan.md` is indexed.
9. **Re-derive with exclusions.** A provenance re-derive over a scope holding conflict copies records its stamp (no withholding) and leaves no copy row behind.
10. **A quarantined note is renamed to a conflict-shaped name, or the setting turns on while a note is quarantined.** The quarantine entry is cleared, no row exists, and the feed records at most one `deleted`.
11. **Empty-root guard.** A root containing only conflict copies over an existing index is determinate (D8) and deletes every row. Confirm this is unchanged behaviour and declared.
12. **Concurrency with `move_note`.** A `move_note` from an ordinary path to a conflict-shaped path commits between the snapshot and the lock. The locked row is at the conflict path and the walk saw the file as excluded (or did not see it: C5 defers). Expect: the row is deleted with `not_indexable`, or deferred to the next pass, and is never upserted.
13. **Multi-user.** Two tenants, setting on: both tenants' copies are excluded, each tenant's feed records only its own removals, and each dashboard shows only the viewer's count.

## Decisions taken during implementation (spec review r1, 2026-11-12)

These were taken by the implementing session's supervisor, not by the owner,
and are listed in the pull request for the owner to confirm or overrule.

- P6 is kept; its producer is unidentified.
- The removal reuses `not_indexable` (no new reason, no migration).
- The dashboard line is kept (the owner does the browser pass).
- Excluded files count as discovered for the empty-root guard (D8).
- Task writes on an excluded note answer `index: may lag (not_indexed)` (L5).
- Review findings: the toggle-off suppression is pinned (L9), not changed. `move_note` adds a notice, not an in-transaction removal (L10). `create_note` adds a notice, and byte transport does not (L11). The task scope is defined over indexed notes (L12). The `reset` baseline is allowed (L13). The dashboard uses the viewer's own scope.
