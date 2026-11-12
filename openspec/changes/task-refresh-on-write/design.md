## Context

#428 (archived as `2026-11-10-task-tools`, design D10) added the write-time task refresh: `task_index.refresh_note_tasks`, called by `complete_task`, `update_task` and `add_task` after a confirmed publication. Its contract (R1) is:

- the tasks are derived from the published bytes, off the loop, before the transaction;
- then, in its own transaction with `SET LOCAL lock_timeout = 2s` (`TASK_REFRESH_LOCK_WAIT_MS`):
  1. the root binding is re-checked;
  2. the owner-scoped `notes_metadata` row is taken `FOR UPDATE`;
  3. under that lock, the leaf is re-read through the call's anchored target and compared byte for byte with what was published (`superseded`);
  4. the note's `note_tasks` rows are replaced;
  5. `tasks_source_hash`, `tasks_refreshed_at`, `tasks_failed` and `tasks_truncated` are stamped.

It never takes the generation lock and never raises. The pass composes with it through R2 (`scan_started_at`, read once per pass), R3 (a row with `tasks_source_hash IS DISTINCT FROM content_hash` is never stat-shortcut) and R4 (a pass keeps a refresh's rows when `tasks_refreshed_at >= scan_started_at` and the hashes differ).

#428 L5 left every other writer out: "`edit_note`, `set_frontmatter`, `move_note`, `write_file` and transfer uploads do not refresh task rows. Their changes appear after the next pass." D10 called the hook "a one-line follow-up". It is not quite one line. The writers differ in what they hold after publishing, and one of them, `delete_note`, has no published bytes at all.

### What each writer holds after a confirmed publication (verified at aafb9f0)

| Writer | Anchored target open after publish | Bytes replaced | Bytes published | Index row at the path |
|---|---|---|---|---|
| `create_note` | yes | none (no-clobber `link`) | `content` | normally none |
| `edit_note` | yes | `existing_bytes` (always read) | `new_content` | yes, unless never indexed or quarantined |
| `set_frontmatter` | yes | `raw_bytes` (always read) | `new_raw` | as above |
| `write_file`, `.md`, `overwrite=False` | yes | none | `data` | normally none |
| `write_file`, `.md`, `overwrite=True` | yes | `incumbent` only when a hash was supplied or required mode is on; otherwise unknown | `data` | yes if an incumbent stood |
| `delete_note` | yes (the parent fd; the leaf is gone) | `incumbent` only when a hash was supplied or required mode is on | nothing | yes, until the pass prunes it |
| `move_note`, plain | yes | n/a (same bytes) | n/a | re-keyed to the destination in the metadata transaction |
| `move_note`, each rewrite | yes until `drop(write_target)` inside the loop | `original_bytes` | `new_content` | yes (the moved note's row is already re-keyed to `to_rel`) |
| `import_from_url`, `PUT /transfer/upload` | no: `stream_to_vault` publishes from a staged stream | a fingerprint, not bytes | never held whole | either |
| `delete_file` | n/a: refuses every `.md` | | | |

## Goals / Non-Goals

**Goals:**

- After any listed generic write to a note that holds or held a task, the next `list_tasks` / `task_agenda` reflects that write.
- No new cost on notes without a checkbox, the large majority of writes.
- No change to any generic writer's response, outcome or usage classification.
- No migration.
- Every R1 invariant, and R3/R4 convergence, hold unchanged for the new call sites.

**Non-goals:**

- A write-time index of the note.
- A new note's tasks before the pass.
- Write-time `notes_metadata.tags`.

## Decisions

### D1. Which writers refresh, and why each excluded writer is excluded

**Refresh** (each after a confirmed publication, gated by D2):

1. **`edit_note`.** All four modes (full replacement, append, `find`, `section`) publish `new_content` over `existing_bytes`. This is the main case of #432.
2. **`set_frontmatter`.** It cannot change a task line: it rewrites only the block. It can still change the task rows:
   - every task's `line` is a file line number, `tasks_from_note_text` adds the frontmatter's line count, and a block that gains or loses lines shifts every task, so `list_tasks` prints `path:line` that no longer points at the task;
   - removing the last key drops the block entirely;
   - task text, ids, tags, dates, context and heading path come from the body and do not change.

   The task rule (#434) reads only `note_tasks` columns, so the rule's verdict does not change either. `excluded_note_tags` reads `notes_metadata.tags`, which the refresh does not write: L2. `set_frontmatter` refreshes so that line numbers stay true. The gate keeps the cost off notes without tasks.
3. **`write_file` on a `.md` path that had an incumbent** (`had_incumbent`, which the tool already computes for its event kind). The path predicate is `change_feed.is_note_path(target.rel)`, the one the event uses, so a path that gets an event is exactly a path that may refresh.
4. **`move_note(rewrite_links=True)`, per published rewrite.** That is the moved note's own rewrite, at `to_rel`, whose row the metadata transaction has already re-keyed, and each backlink source. A wikilink in a task line (`- [ ] review [[Old]]`) is part of the task's text and so of its id. Without a refresh, the listing keeps the old id, and every task write on it answers `task_not_found` until the pass. The refresh runs inside the rewrite loop, after the publication and before `drop(write_target)`, because R1's re-check needs the open target. It is budgeted per call (D6).

**Clear** (D3):

5. **`delete_note`**, soft and permanent. The row survives the delete until the pass prunes it, and with it the note's task rows, so a listing shows tasks of a note that is gone. The clear removes them.

**Excluded:**

- **`create_note`, and `write_file` creating a note.** There is no index row for a path this call just created. The one exception is a stale row the pass has not pruned yet: the file was deleted outside the server, or deleted by `delete_note` and already cleared. Writing a new note's tasks under a row whose other columns describe a different note would anticipate a decision that belongs to the pass: upsert, or move pairing by hash. A refresh would almost always report `not_indexed` anyway. The new note's tasks appear after the next pass (L1).
- **Plain `move_note`.** The bytes do not change, and the task rows are keyed by `note_id`. The metadata transaction re-keys `file_path` on that row, so the listing shows the tasks under the new path at once (the `note-tasks` lifecycle rule "an id-preserving move SHALL leave the rows attached"). There is nothing to refresh. When that transaction fails (`DB update failed; reindex will reconcile`), the rows stay under the old path until the pass (L7).
- **`import_from_url` and `PUT /transfer/upload`.** They stream to a staged file and publish without ever holding the whole content, and they publish through `transfer`'s own path, not a `MutableTarget`. R1 derives only from the bytes the call published, and never from a re-read, so a re-read cannot "launder" a concurrent writer's bytes into refreshed state. A refresh here would need exactly such a re-read, or a buffered copy of up to `MAX_NOTE_BYTES` per upload. The upload route is also not an MCP tool call. L1 records the lag.
- **`delete_file`.** It refuses every `.md` path, so it can never touch a note.
- **Non-markdown paths.** No index row, no tasks.

### D2. The gate: `tasks.may_hold_tasks(data) -> bool`

A refresh (or clear) is attempted only when **either side may hold a task**:

- `may_hold_tasks(published)` (absent for a clear), **or**
- the replaced bytes are known and `may_hold_tasks(replaced)`, **or**
- the call replaced or removed an incumbent whose bytes it did not read. That is an unguarded `write_file(overwrite=True)`, an over-cap incumbent, or an unguarded `delete_note`. They are assumed to have held a task.

A call that published bytes equal to the bytes it replaced attempts nothing. `edit_note` already skips its event in that case.

**Definition.** `may_hold_tasks(data: bytes) -> bool`:

1. Take `note_hash.canonical_note_text(data)`. If it is `None` (not UTF-8), return **true**. The refresh then reports `not_utf8`, which costs one call and writes nothing.
2. Otherwise return whether `_CHECKBOX_GATE_RE.search(text)` matches. `_CHECKBOX_RE` is split into `_CHECKBOX_PREFIX` (everything through the closing `\]`) and its lookahead `(?=[ \t]|$)`, with no change to what `_CHECKBOX_RE` matches. `_CHECKBOX_GATE_RE` is `^` + `_CHECKBOX_PREFIX`, compiled with `re.MULTILINE`. It is defined in `tasks.py` from the same string, never retyped.

**Why it is a superset.** The extractor:

- splits the canonical text into lines at `\n`, which is the only terminator left after canonicalisation;
- calls `_CHECKBOX_RE.match` at each line start on the masked body, with `endpos` at the line end;
- keeps a match only when the original and masked text are equal through the closing `]` (the #218 rule).

So every task line's **original** text matches `_CHECKBOX_PREFIX` at a line start, because the original equals the masked text through `]`. The lookahead is deliberately not in the gate. The extractor evaluates it on the **masked** text, where the character after `]` may be a masked code span (`- [ ]`+backtick-code+backtick reads as a space there and as U+0060, the backtick, in the original), so including it would break the superset. With `re.MULTILINE`, `^` matches at every line start of the canonical text, whose only terminator is `\n`. The body is a suffix of the canonical text that starts at a line start, and the gate runs over the whole text, frontmatter included, so it can only match more.

It over-matches checkboxes in code fences, in frontmatter values and in non-task contexts. Each over-match costs one refresh, never a wrong answer. A test pins the superset property: for every input in the extractor corpus and a generated set, if `tasks_from_note_text(x)` returns any task, then `may_hold_tasks(x)` is true.

**Failure.** The gate evaluation is wrapped by its caller: any exception from it counts as "may hold", so a gate fault can only cost a refresh, never skip one or fail the write.

**Cost.** One strict decode and one linear regex scan (possessive quantifiers, as the extractor's) per side. It reads no file and no row. Both sides are scanned in **one** `asyncio.to_thread` call, so a 10 MiB note never scans on the event loop.

**Rejected: gating on the row's task count** (`EXISTS (SELECT 1 FROM note_tasks WHERE note_id = …)`). It is exact for the "contained" half, and it catches rows made stale by an outside edit. But it costs a database round trip on every note write, including the large majority with no checkbox, which is the cost #432 asks to avoid. The byte gate covers the bytes this call replaced, so the only staleness it leaves is one this write did not cause (L5).

### D3. The clear: `refresh_note_tasks`' absence form, for `delete_note`

`refresh_note_tasks(uid=…, root=…, target=…, rel=…, published=ABSENT)`, where `ABSENT` is a module sentinel and not `None`, runs R1 with three changes:

- **Step 3a, still current:** under the row lock, `target.lstat()` must be `None`: nothing at the leaf, through the anchored parent descriptor. Anything there means another writer published at the path since the delete, so it writes nothing (`superseded`).
- **Steps 4–5:** delete the note's `note_tasks` rows. There is nothing to derive.
- **Step 6:** stamp:
  - `tasks_source_hash = canonical_note_hash(b"")`, the hash of the empty note, whose tasks are none;
  - `tasks_refreshed_at = clock_timestamp()`;
  - `tasks_failed = false`, `tasks_truncated = false`.

  No other column is written. The row itself is the pass's to prune.

**Why the empty-note hash.** It is a real canonical hash, so no new sentinel value enters the column. It says what the rows now describe: no tasks.

- It differs from the row's `content_hash` unless the deleted note was itself empty. In that case nothing changes: the note had no tasks, and the pass prunes the row.
- So the row is task-dirty (R3), and R4 keeps the cleared state against a pass that read the note before the delete.
- If a file reappears at the path before the pass (a sync, or a human restoring it from `.trash`), the pass walks it. R3 forces a read and the pass derives from disk.
- If it does not reappear, the walk does not find it and the prune deletes the row, cascading to any task rows.
- The #309 rule is unchanged: a row beneath an unlistable directory is not pruned and keeps its cleared, empty task set, which is true of a deleted note.

The gate for the clear is D2 with no published side: the incumbent, when read, may hold a task, or the incumbent was not read.

### D4. Placement, ordering, locks

For each refreshing writer, in order:

1. Publication, confirmed through `_confirmed_publication`, exactly as today.
2. The gate (D2), off the loop.
3. The refresh or clear, through one helper `_refresh_after_write(uid, target, *, replaced, published, budget)` built on the existing `_refresh_task_rows`. It runs **inside the tool's `with target:` block**, so the anchored descriptor R1's re-check needs is still open. For `move_note` it runs inside the rewrite loop before `drop(write_target)`.
4. The change-feed event, as today. It is recorded whatever the refresh did: the same publish → refresh → event order as the task tools, so a consumer that reacts to the event and then lists sees the new state.
5. The response, byte-identical (D5).

Then `_tracked` writes the usage row, after the body returns, and releases the concurrency lease. The refresh therefore runs **under the call's lease and before its usage row**, as the task tools' refresh does. It changes neither.

**Locks are R1's, unchanged.**

- The only lock is the note's `notes_metadata` row lock, taken with `SET LOCAL lock_timeout = TASK_REFRESH_LOCK_WAIT_MS` (2 s).
- The generation lock is **never** taken. The existing AST guard in `tests/test_note_tasks_extractor.py` already covers `task_index.py`, and it is extended to the new helper.
- The refresh opens its own session after the publication. None of the generic writers holds a session or a row lock across its publication, so there is no self-wait.
  - `move_note`'s metadata transaction (generation lock, row update, `moved` event) commits before its rewrite loop starts.
  - The rewrite loop runs under the process-wide `_MOVE_REWRITE_LOCK`, an `asyncio.Lock`, not a database lock. Holding it during a refresh lengthens a rewrite move by at most D6's bound.
- The refresh's session is sequential with the change-feed append's, never concurrent, so a call's peak pooled connections are unchanged.

**What never refreshes:** a refusal, a dry run, a "No changes" answer, a publication that raised (including an in-call conflict), an uncertain publication, and a `move_note` rewrite that failed or was stopped.

### D5. Output and usage: silent, with one usage key

The generic writers' responses stay **byte-identical**, whatever the refresh did. Their texts are pinned by the existing response tests, and by the 1.0 tool-contract promise: an agent parsing `Created note:`, `Replaced …`, `Updated frontmatter in …`, `Wrote N bytes …`, `Soft-deleted: …` or `Moved … — …` must see no new line. An `index:` line would also be a fourth shape of the response for a tool whose contract is about the file, not the index.

**Rejected:** printing `index: may lag (<code>)` on a lagged generic refresh. It would make a lag visible to the agent, but:

- it changes the success text of five tools, and every test pinning it;
- it puts index state into tools whose callers mostly never list tasks;
- a lag already converges within one pass (L3).

The task tools keep their `index:` line.

**Usage.** When a refresh or clear is attempted, `timing.record("task_refresh", …)` adds a result-side key:

- single-note writers record `refreshed` or `lagged:<code>`, with the existing codes;
- `move_note` records one value for the call: `lagged:<code>` of the first refresh that lagged; otherwise `deferred` if the per-call budget (D6) left a gated-in rewrite unrefreshed; otherwise `refreshed`.

When the gate does not pass, **the key is absent**, so a write to a note without a checkbox logs exactly what it logs today. A lag is not an error marker and never changes the outcome classification (`BodyOutcome`, disposition, marker).

`refresh_note_tasks`' WARNING is reworded from "after a task write" to "after a write", and it already names only the code and the exception class. Nothing else is logged.

### D6. Bounds: per refresh, per call, and large notes

**Per refresh** (unchanged R1 costs, now on generic writers):

- one gate scan of at most two byte strings of at most `MAX_NOTE_BYTES` each;
- one derivation of at most `MAX_NOTE_BYTES`, off the loop and outside the transaction. The extractor stops at `MAX_TASKS_PER_NOTE` (5,000) tasks and sets `tasks_truncated`, exactly as the pass does;
- one transaction: the root check, a row lock with a wait of at most 2 s, one re-read of at most `MAX_NOTE_BYTES` through the anchored target, one delete, inserts in batches of 1,000 rows (at most 5), and one update.

Every generic writer's published note is at most `MAX_NOTE_BYTES`:

- `edit_note`, `set_frontmatter` and the move rewrites refuse larger results;
- `write_file` caps a `.md` path at `min(MAX_NOTE_BYTES, MAX_FILE_WRITE_BYTES)` (`_write_cap_for`).

So the re-read cap equals the publication cap, and no published note is "beyond" the bound. A note with more than 5,000 tasks is stored truncated and counted `over the task cap`, as from the pass.

**Per call.** Every single-note writer makes at most one refresh. `move_note(rewrite_links=True)` can publish hundreds of rewrites (bounded today only by descriptors and `MAX_MOVE_REWRITE_BYTES`, 256 MiB). It refreshes gated-in rewrites in publication order while all of these hold:

- fewer than `TASK_REFRESH_MAX_NOTES_PER_CALL` (32, a constant in `src/config.py`, not a setting) refreshes have been attempted in this call;
- the published bytes of the notes already refreshed, plus this one, are at most `MAX_NOTE_BYTES` in total;
- no earlier refresh in this call returned `lock_timeout` or `error`. A contended row lock means a pass holds row locks, and waiting 2 s per note would multiply the wait.

The first rewrite that fails any of these, and every gated-in rewrite after it, is **deferred**: nothing is refreshed for it. Its publication changed the file's stat, so the next pass reads and derives it. The worst-case added time for one `move_note` call is therefore one maximal derivation's worth of bytes, plus 32 short transactions, plus one 2 s lock wait.

### D7. Convergence: R3 and R4 hold for the new call sites, unchanged

The pass is not modified. The generic refresh writes exactly what a task tool's refresh writes, through the same function, under the same lock and the same re-check. The D10 argument therefore holds verbatim with "a task tool" read as "any refreshing writer". The only new state is the clear (D3), which is a refresh whose bytes are the empty note's. The interleavings and their outcomes:

1. **The pass scans before the write.** The pass buffers B0. `edit_note` publishes B1 and refreshes, so the rows describe B1 with `tasks_refreshed_at > scan_started_at`. The pass reaches the note, and R4 sees `tasks_source_hash = H(B1) ≠ H(B0)` with a refresh after its scan, so it keeps the B1 rows. The next pass reads the note (changed stat, and R3) and derives B1: clean.
2. **The write lands while the pass holds the note's row lock.** The refresh waits at most 2 s.
   - If the pass commits within that wait, the refresh then writes B1. If the pass had buffered B0, R4 was evaluated before the refresh existed and the pass wrote B0 rows; the refresh then overwrites them with B1. Correct.
   - If the wait expires: `lagged:lock_timeout`, nothing written. The pass's rows describe whatever it buffered. If that was B0, the changed stat makes the next pass read B1.
3. **The pass scans after the publication.** It buffers B1, and its rows and the refresh's agree whichever commits first. When the refresh commits after the pass, `tasks_source_hash = H(B1) = content_hash` with a non-NULL `tasks_refreshed_at`: #428 L23, harmless.
4. **Two writers on one note, refreshes in order.** A publishes B1 and refreshes; B publishes B2 and refreshes. The rows describe B2.
5. **Two writers on one note, refreshes out of order.** A publishes B1, B publishes B2, then B refreshes (B2) and A refreshes. A's re-check reads B2 ≠ B1, so `superseded`, nothing written. The rows describe B2. This holds across mixed writer kinds: a task tool and `edit_note`, `set_frontmatter` and a move rewrite.
6. **A generic write followed by an outside writer** (Obsidian sync, a human) before its refresh. `superseded`. The outside writer changed the stat, so the next pass reads it.
7. **The same, but the outside write lands after the refresh commits.** The rows describe B1 while disk holds B2. The row is task-dirty (`H(B1) ≠ content_hash`), so R3 reads it on the next pass. R4 keeps B1 only for a pass that began before the refresh. The first pass to begin after it derives B2.
8. **A gated-out write after a refreshed one.** `edit_note` refreshes B1 (tasks); a later `edit_note` publishes B2 with no checkbox over B1, which had one, so the gate passes and it refreshes. If both B1 and B2 lack checkboxes, the gate fails; the rows already describe zero tasks, and the stat change brings the pass.
9. **`delete_note` while a pass is mid-scan.** The pass buffered B0. The clear stamps the empty hash after `scan_started_at`, R4 keeps the cleared (empty) rows, and the next pass prunes the row.
10. **`delete_note`, then a recreation at the path before the clear runs.** `target.lstat()` finds a file, so `superseded`, nothing cleared, and the pass converges.
11. **`delete_note`, the clear, then a recreation before the pass.** The row is task-dirty, so R3 reads the new file and derives it.
12. **A move rewrite racing an edit of a backlink source.** `write_file_at(expected=original_bytes)` refuses the rewrite (an in-call conflict), so there is no publication and no refresh. The edit's own refresh, or the pass, covers the source.
13. **A root reassignment between publication and refresh.** `root_changed`, nothing written. #428 L21 (an unlocked re-check) applies unchanged.

**No row is ever stuck.** Every refreshing write leaves the row either:

- untouched: a lag or a superseded refresh, after a publication that changed the stat, so the next pass reads it;
- or task-dirty until a pass that began after the refresh, which R3 guarantees will read it.

### D8. Statements this change reverses or narrows, and their replacement text

| Where | Current | Replacement |
|---|---|---|
| Archived `2026-11-10-task-tools` L5 | "`edit_note`, `set_frontmatter`, `move_note`, `write_file` and transfer uploads do not refresh task rows. Their changes appear after the next pass." | Not edited: archives are history. Superseded by this change's D1 and L1, and recorded in its "What existing behaviour changes". |
| Archived L14 (duplicate scan misses generic writes) | as written | Narrowed (not edited). The index side now reflects `edit_note`, `set_frontmatter`, `write_file` overwrites and move rewrites; it still misses `create_note`, imports and uploads until the pass. |
| `CLAUDE.md`, "Task writes address a line…" | "Generic writers do not refresh (L5)." | "Since #432 `edit_note`, `set_frontmatter`, a `write_file` that replaces a note and `move_note`'s link rewrites refresh too, silently and only when the replaced or published bytes may hold a checkbox (`tasks.may_hold_tasks`); `delete_note` clears the note's task rows; `create_note`, imports and uploads wait for the pass." |
| `docs/architecture/indexing-and-embeddings.md`, "What generic writers do not do (L5)" paragraph | the whole paragraph | Retitled "**Generic writers (#432).**" The new text gives D1's table, the D2 gate and its superset proof, the D3 clear, D6's per-call budget, and the statement "the pass is unchanged; R3/R4 cover every refreshing writer". The two residuals (L21, L23) are kept verbatim. |
| Same file, "What `list_tasks` can and cannot see" | "except an edit made by a task tool, which refreshes that note's task rows at once (#428, the next section). `edit_note` and the other generic writers do not." | "except a write that refreshes that note's task rows at once: the task tools (#428), and since #432 `edit_note`, `set_frontmatter`, a `write_file` replacing a note, `move_note`'s link rewrites and `delete_note` (a clear). A new note (`create_note`, `write_file` creating, import, upload) appears after the next pass." |
| `docs/architecture/vault-tools.md`, duplicate-scan paragraph | "a duplicate written by a generic tool since the last pass can be missed (L14)" | "a duplicate written since the last pass by `create_note`, an import or an upload can be missed (L14, narrowed by #432)" |
| `README.md`, task section | "`edit_note` and the other generic write tools do not refresh task rows, so their changes show up after the next pass (every 300 s by default)." | "`edit_note`, `set_frontmatter`, `write_file` and `move_note`'s link rewrites refresh the task rows of a note they change, and `delete_note` removes a deleted note's tasks. A new note created by `create_note`, `write_file`, an import or an upload shows up after the next pass (every 300 s by default)." |
| `docs/architecture/usage-attribution.md`, task tools section | the table has no generic writers | One added row group: `edit_note`, `set_frontmatter`, `write_file`, `delete_note`, `move_note` gain the result-side key `task_refresh`, present only when a refresh was attempted, with `deferred` for `move_note` (D5). |
| `src/services/tasks.py` docstring, "The write-time refresh exception (#428, design D10)" | "a task tool's write-time refresh … from the bytes its own call published and from nothing else" | "a write-time refresh, by a task tool or by a generic note writer (#432) … from the bytes its own call published, or, for `delete_note`, from the absence it caused, and from nothing else" |
| `src/services/task_index.py` docstring | "A task tool (`complete_task`, `update_task`, `add_task`) publishes a note and then calls…" | Adds the generic writers, the absence form (D3) and D6's caller-side budget. |
| `list_tasks` description ("task writes refresh it at once") | unchanged | Still true, so it is **not** edited: editing it would change the tool-contract oracle (D9). |

### D9. Tool surface: unchanged

No tool name, argument, schema, title, annotation or description changes. `tests/fixtures/tools_list_full_v2.json` and `tools_list_readonly_oauth.json` must pass untouched. There is no migration and no new setting: `TASK_REFRESH_MAX_NOTES_PER_CALL` is a constant, as `TASK_REFRESH_LOCK_WAIT_MS` is. If the implementation finds either a migration or a description change unavoidable, it stops and reports.

## Rejected alternatives

- **No gate: refresh after every note write.** It adds a transaction to every write of a note without tasks, which is #432's stated concern.
- **Gate on the row's task count** (D2). It costs a round trip on every write.
- **Gate on the diff: refresh only when a changed line is a task line.** It misses heading-path changes, context-line changes and line-number shifts, all of which the rows store. It is also more code on the destructive path for less correctness.
- **Index the whole note at write time.** Rejected in #428 D10: a second writer of the hottest code in the server.
- **Re-read the file after a streaming write** (import, upload). It violates R1's "only the bytes this call published", and a concurrent writer's bytes could be laundered into refreshed state.
- **Buffer the upload or import body to refresh from it.** Up to `MAX_NOTE_BYTES` of extra memory per transfer, against the #322 body-memory budget's design. Not worth it for a path a person drives.
- **Refresh a newly created note by creating its index row.** Same as indexing at write time.
- **Wake the index pass right after a write.** It would cover creations and transfers, but it is a scheduler change with its own fairness and tenancy questions. A possible follow-up, not part of this change.
- **Event before refresh for the generic writers** (to keep today's narrower cancellation window). It breaks the property that a consumer reacting to a `modified` event and then listing sees the new tasks, and it diverges from the task tools.
- **An `index:` line on generic writers** (D5).
- **A configurable per-call budget.** A setting would need README, `.env.example` and validation for a bound nobody has asked to tune.

## What existing behaviour changes

- After `edit_note`, `set_frontmatter`, `write_file` (replacing a note) or a `move_note` link rewrite of a note whose old or new bytes may hold a checkbox, `list_tasks`, `task_agenda` and `add_task`'s index-side duplicate scan reflect the write in the next call. Before, they did so after the next pass.
- After `delete_note` of such a note (or of any note whose bytes the call did not read), its tasks disappear from listings at once.
- Those writes take longer: one derivation, one short transaction, and at most a 2 s lock wait when a pass holds the row (D6).
- Usage rows of those writes gain `task_refresh` (D5).
- Responses, schemas, descriptions, annotations and the change-feed events themselves are unchanged. The events' order relative to the refresh is now the task tools' order.

## Accepted limitations

- **L1**: A new note's tasks appear only after the next pass. That covers `create_note`, `write_file` creating a note, `import_from_url`, and an upload, which creates or overwrites. An import or upload that overwrites a note with tasks also leaves its old rows until the pass.
- **L2**: `excluded_note_tags` reads `notes_metadata.tags`, which only the pass writes. A `set_frontmatter` or `edit_note` that changes a note's tags changes that note's exclusion only after the next pass.
- **L3**: A generic writer's refresh that lags is silent in the response. It shows only in the usage row, and the pass converges it within one interval. The task tools still say `index: may lag`.
- **L4**: `move_note` refreshes at most 32 rewritten notes, and at most `MAX_NOTE_BYTES` of them, per call, and stops after the first `lock_timeout` or `error`. The rest wait for the pass.
- **L5**: The gate considers only the bytes this call replaced and published. Rows made stale by an earlier outside edit are not corrected by a later gated-out write: an outside edit added tasks, then `edit_note` removed them with neither side holding a checkbox in the bytes the call saw. That staleness is the pre-existing pass lag, unchanged.
- **L6**: The refresh sits between the publication and the change-feed event, so a call cancelled during the refresh loses its attributed event, and the pass records the change as `human/sync`. This is the same window the task tools have, widened for the generic writers by at most one derivation plus one 2 s lock wait.
- **L7**: When `move_note`'s metadata transaction fails, the tasks stay listed under the old path until the pass, as every other row of that note does.
- **L8**: The gate over-matches: a checkbox inside a code fence or a frontmatter value triggers a refresh that writes the same rows. It costs time, never correctness.
- **L9**: `#428 L21` (the unlocked root re-check) and `L23` (a clean row keeping a non-NULL `tasks_refreshed_at`) apply to the new call sites unchanged.

## Adversarial scenarios a reviewer should trace

Each should be traced through the code as implemented. Each states the expected outcome.

1. **Every interleaving in D7 (1–13)**, run with each refreshing writer in the writer role: `edit_note`, `set_frontmatter`, `write_file` overwrite, a move rewrite, and the `delete_note` clear. After at most two passes, `tasks_source_hash = content_hash` and the rows equal `tasks_from_note_text(disk)`. No pass writes rows older than a refresh that began after its scan.
2. **Failure injection at each step of the generic path.** In every case the response text and outcome classification are byte-identical to the no-refresh run, and the change-feed event is recorded:
   - the publication raises (no refresh, no event, unchanged refusal);
   - the gate raises (treated as "may hold", so the refresh runs; the exception never propagates);
   - the derivation raises (`derivation_failed`, rows removed, `tasks_failed` set);
   - the root check fails (`root_changed`);
   - no row (`not_indexed`);
   - the lock wait expires (`lock_timeout`);
   - the re-read fails or differs (`superseded`);
   - the insert fails (`error`, rolled back, rows unchanged);
   - the commit fails (`error`).
3. **A note at the size caps.** `edit_note` publishing exactly `MAX_NOTE_BYTES` with 5,001 checkboxes: the refresh stores 5,000 rows, `tasks_truncated = true`, and no event-loop stall over 100 ms (the gate and the derivation are off the loop). The same for `write_file` at `min(MAX_NOTE_BYTES, MAX_FILE_WRITE_BYTES)`. A file at the path grown past the cap by an outside writer between publication and re-check: the re-read fails, `superseded`.
4. **A 10 MiB adversarial gate input**: one line of `- - - - …`, one of `> > > …`, a line of digits without `.`: `may_hold_tasks` returns within 2 s.
5. **The gate's superset property.** No input for which the extractor finds a task and the gate returns false. Check CRLF and lone-CR notes, NUL inside the brackets (`[` U+0000 `x]`, which canonicalises to `[x]`), a BOM before line 1, blockquote and callout prefixes, ordered markers, and a task as the last line without a terminator.
6. **Multi-user ownership.** Users 7 and 8 each have `Notes/a.md` with tasks, and user 7 runs `edit_note` and `delete_note`. Only user 7's row is locked, refreshed or cleared, and user 8's rows and `tasks_*` columns are byte-identical before and after. Single-user mode (NULL owner) also works, and a NULL-owned row is never touched by a user-7 call.
7. **`delete_note` clear specifics.** Soft and permanent. Guarded (bytes known, no checkbox: no clear) and unguarded (clear). A recreation at the path before the clear: `superseded`. A `.trash` restore before the pass: R3 read, rows derived.
8. **`move_note` specifics.**
   - A plain move makes no refresh call, and the tasks are listed under `to_rel` at once.
   - A rewrite move with 40 gated-in sources: exactly 32 refreshed, the rest deferred, `task_refresh = deferred`.
   - The first source's refresh hits `lock_timeout`: no further refresh in the call.
   - The moved note's rewrite refreshes the row at `to_rel`.
   - A failed or conflicted rewrite makes no refresh.
   - `db_failed` stops the rewrites before any refresh.
   - The refresh happens before `drop(write_target)`.
9. **No generation lock**: the AST guard covers the helper and every call site. A refresh contending with a pass holding the generation lock and a row lock completes or lags within 2 s; it never deadlocks.
10. **The response golden**: for each generic writer, the success text for a note with tasks equals the success text for the same write with refresh disabled. `tools_list_full_v2.json` is unchanged.
11. **Usage**: a write to a note without a checkbox logs params byte-identical to aafb9f0's. A write to a note with tasks adds only `task_refresh`.

## Test plan

- **Unit, `tests/test_note_tasks_extractor.py`:**
  - `may_hold_tasks` superset property over the existing extractor corpus plus generated inputs (scenario 5);
  - linear time (scenario 4);
  - `_CHECKBOX_RE.pattern == _CHECKBOX_PREFIX + lookahead` and the gate is built from `_CHECKBOX_PREFIX` (identity assertions), plus the masked-lookahead case (`- [ ]` followed directly by a code span) yields a task and passes the gate;
  - the AST guard extended to the new helper.
- **Unit, a new `tests/test_task_refresh_generic.py`, with a stubbed refresh:**
  - which writers call it, with which `replaced` / `published` / `ABSENT`;
  - gate decisions per writer, including unknown incumbents;
  - no call on refusal, dry run, no-change or a raised publication;
  - the ordering publish → refresh → event;
  - byte-identical responses with refresh results of every code (scenario 10);
  - the usage key present or absent (scenario 11);
  - the move budget (scenario 8).
- **Integration, `tests/integration/test_task_refresh_pg.py`, extended:**
  - list → `edit_note` → list with no pass between, for each of the four refreshing writers;
  - `delete_note` → list;
  - D7's interleavings with `edit_note` and the delete clear in the writer role;
  - the out-of-order refresh across writer kinds (task tool against `edit_note`);
  - multi-user (scenario 6);
  - `lock_timeout` with a held row lock: the `edit_note` response is unchanged and the usage row reads `lagged:lock_timeout`;
  - the pass-derivation = refresh-derivation equality is reused.
- **Offline suite and the tool-contract tests**, unchanged and green.

## Owner decisions requested

1. **`delete_note` clears task rows** (D3). It is new behaviour beyond "refresh", and the most visible fix. Confirm.
2. **`set_frontmatter` refreshes** (D1.2), for line numbers only. Confirm, or exclude it and accept stale `:line` until the pass.
3. **The `move_note` budget** of 32 notes and `MAX_NOTE_BYTES`, stopping at the first lock timeout (D6). Confirm the numbers.
4. **Silence on the generic writers** (D5): no `index:` line. Confirm.
5. **`create_note` excluded** (D1, L1). An agent that creates a note with tasks and lists at once sees nothing until the pass. The alternative is a follow-up that wakes the pass early.
