## Context

#428 (archived as `2026-11-10-task-tools`, design D10) added the write-time task refresh, `task_index.refresh_note_tasks`. `complete_task`, `update_task` and `add_task` call it after a confirmed publication. Its contract (R1):

- the tasks are derived from the published bytes, off the event loop, before any transaction;
- then, in its own transaction with `SET LOCAL lock_timeout = 2s` (`TASK_REFRESH_LOCK_WAIT_MS`):
  1. the root binding is re-checked;
  2. the owner-scoped `notes_metadata` row is taken `FOR UPDATE`;
  3. under that lock, the leaf is re-read through the call's anchored target and compared byte for byte with what was published (`superseded` if it differs);
  4. the note's `note_tasks` rows are replaced;
  5. `tasks_source_hash`, `tasks_refreshed_at`, `tasks_failed` and `tasks_truncated` are stamped.

It never takes the generation lock and never raises.

The pass composes with it through three rules:

- **R2**: `scan_started_at` is read once per pass.
- **R3**: a row whose `tasks_source_hash IS DISTINCT FROM content_hash` is never stat-shortcut.
- **R4**: a pass keeps a refresh's rows when `tasks_refreshed_at >= scan_started_at` and the hashes differ.

#428 L5 left every other writer out: "`edit_note`, `set_frontmatter`, `move_note`, `write_file` and transfer uploads do not refresh task rows. Their changes appear after the next pass." D10 called the hook "a one-line follow-up". It is not quite one line. The writers differ in what they hold after publishing, and one of them, `delete_note`, publishes no bytes at all.

### What each writer holds after a confirmed publication (verified at aafb9f0)

| Writer | Anchored target open after publish | Bytes replaced | Bytes published | Index row at the path |
|---|---|---|---|---|
| `create_note` | yes | none (no-clobber `link`) | `content` | normally none |
| `edit_note` | yes | `existing_bytes` (always read) | `new_content` | yes, unless never indexed or quarantined |
| `set_frontmatter` | yes | `raw_bytes` (always read) | `new_raw` | as above |
| `write_file`, `.md`, `overwrite=False` | yes | none | `data` | normally none |
| `write_file`, `.md`, `overwrite=True` | yes | `incumbent` only when a hash was supplied or required mode is on; otherwise unknown | `data` | yes if an incumbent stood |
| `delete_note` | yes (the parent fd; the leaf is gone) | an incumbent read **before** publication, when one is read at all; the delete removes whatever stands at the leaf when it runs | nothing | yes, until the pass prunes it |
| `move_note`, plain | yes | n/a (same bytes) | n/a | re-keyed to the destination in the metadata transaction |
| `move_note`, each link rewrite | until `drop(write_target)` in the loop | `original_bytes` | `new_content` | yes |
| `import_from_url`, `PUT /transfer/upload` | no: `stream_to_vault` publishes from a staged stream | a fingerprint, not bytes | never held whole | either |
| `delete_file` | n/a: refuses every `.md` | | | |

## Goals / Non-Goals

**Goals:**

- After every qualifying generic write to a note that holds or held a task, the server **attempts** to refresh that note's task rows in the same call. When the refresh cannot run, the rows converge at the next index pass, exactly as before this change.
- No new cost on notes without a checkbox, the large majority of writes.
- No change to any generic writer's response, outcome, change-feed event or event order.
- No migration.
- Every R1 invariant, and R3/R4 convergence, hold unchanged for the new call sites.

**Non-goals:**

- A guarantee that the next listing reflects a generic write. The refresh is best effort, and a lag is silent (L3).
- A write-time index of the note.
- A new note's tasks before the pass.
- Write-time `notes_metadata.tags`.
- Refreshing after `move_note`'s link rewrites (L4).

## Decisions

### D1. Which writers refresh, and why each excluded writer is excluded

**Refresh** (each after a confirmed publication and its change-feed event, gated by D2):

1. **`edit_note`.** All four modes (full replacement, append, `find`, `section`) publish `new_content` over `existing_bytes`. This is the main case of #432.
2. **`set_frontmatter`.** It cannot change a task line: it rewrites only the block. It can still change the task rows:
   - every task's `line` is a file line number, and `tasks_from_note_text` adds the frontmatter's line count. A block that gains or loses lines shifts every task, so `list_tasks` prints a `path:line` that no longer points at the task;
   - removing the last key drops the block entirely;
   - task text, ids, tags, dates, context and heading path come from the body and do not change.

   The task rule (#434) reads only `note_tasks` columns, so its verdict does not change either. `excluded_note_tags` reads `notes_metadata.tags`, which the refresh does not write (L2). `set_frontmatter` refreshes so that line numbers stay true, and the gate keeps the cost off notes without tasks.
3. **`write_file`, after every publication to a note path.** The path predicate is `change_feed.is_note_path(target.rel)`, the one the event uses. The decision does **not** depend on `had_incumbent`: that is an `lstat` sampled before the publication, for the event's kind only, and a note moved onto the path between the sample and the publication would be overwritten with no refresh (implementation review r1, MAJOR). The replaced bytes are known only for a guarded overwrite, whose publication is bound to them (`expected=incumbent`); otherwise they are unknown (D2). A genuine creation finds no index row, so the refresh is the existing `not_indexed` no-op: one query, nothing written.

**Clear** (D3):

4. **`delete_note`**, soft and permanent, after **every** confirmed delete, with no byte gate. The row survives the delete until the pass prunes it, and so do the note's task rows: without a clear, a listing shows tasks of a note that is gone.

   The byte gate cannot be used here, because the bytes the delete removed are not known. The incumbent, when read at all, is read before publication. Both delete forms then remove whatever stands at the leaf when they run: `unlink_at` unlinks the current leaf, and `soft_delete_target` renames it (`src/services/vault.py`). A second writer can add a task in between, so a gate on the earlier bytes would skip the clear for a note that did hold a task (review finding 1). The clear is cheap: one short transaction with no derivation.

**Excluded:**

- **`create_note`.** There is no index row for a path this call just created. The one exception is a stale row the pass has not pruned yet: the file was deleted outside the server, or deleted by `delete_note` and already cleared. Writing a new note's tasks under a row whose other columns describe a different note would anticipate a decision that belongs to the pass: upsert, or move pairing by hash. Such a refresh would almost always report `not_indexed` anyway. The new note's tasks appear after the next pass (L1). (`write_file` is not excluded the same way: it may land on a path a move filled after its existence sample, D1.3.)
- **Plain `move_note`.** The bytes do not change, and the task rows are keyed by `note_id`. The metadata transaction re-keys `file_path` on that row, so the listing shows the tasks under the new path at once (the `note-tasks` rule "an id-preserving move SHALL leave the rows attached"). There is nothing to refresh. When that transaction fails (`DB update failed; reindex will reconcile`), the rows stay under the old path until the pass (L6).
- **`move_note`'s link rewrites**, of the moved note and of each backlink source. A rewritten link inside a task line changes that task's text and id. Until the pass, a task write that uses the listed old id refuses with `task_not_found` and writes nothing. That is a refusal, not a wrong write. Refreshing every rewritten note would add a per-call budget, stop rules and time under the process-wide `_MOVE_REWRITE_LOCK` that serialises every rewrite move. Cut by review (finding 6), as L4.
- **`import_from_url` and `PUT /transfer/upload`.** They stream to a staged file and publish without ever holding the whole content, through `transfer`'s own path rather than a `MutableTarget`. R1 derives only from the bytes the call published and never from a re-read, so that a refresh cannot "launder" a concurrent writer's bytes into refreshed state. A refresh here would need exactly such a re-read, or a buffered copy of up to `MAX_NOTE_BYTES` per upload. The upload route is also not an MCP tool call (L1).
- **`delete_file`.** It refuses every `.md` path.
- **Non-markdown paths.** They have no index row and no tasks.

### D2. The gate: `tasks.may_hold_tasks(data) -> bool`

A refresh by `edit_note`, `set_frontmatter` or `write_file` is attempted only when **either side may hold a task**:

- `may_hold_tasks(published)` is true; **or**
- the replaced bytes are known and `may_hold_tasks(replaced)` is true; **or**
- the call does not hold the bytes it replaced: every `write_file` except a guarded overwrite (including a creation, and an unguarded or over-cap overwrite). Such bytes are assumed to have held a task.

**Equal bytes.** A call that **knows** the bytes it replaced and published exactly those bytes attempts nothing. `edit_note` and `set_frontmatter` know them. So does `write_file` when it read the incumbent. `edit_note` already skips its event in that case. An unguarded `write_file` overwrite does not know them, so the third gate rule applies to it even when the bytes happen to be equal. The refresh then rewrites the same rows (review finding 3).

`delete_note` is not gated (D1.4).

**Definition.** `may_hold_tasks(data: bytes) -> bool`:

1. Take `note_hash.canonical_note_text(data)`. If it is `None` (not UTF-8), return **true**. The refresh then reports `not_utf8`, which costs one call and writes nothing.
2. Otherwise return whether `_CHECKBOX_GATE_RE.search(text)` matches.

`_CHECKBOX_RE` is split into `_CHECKBOX_PREFIX` (everything through the closing `\]`) and its lookahead `(?=[ \t]|$)`, with no change to what `_CHECKBOX_RE` matches. `_CHECKBOX_GATE_RE` is `^` + `_CHECKBOX_PREFIX`, compiled with `re.MULTILINE`. It is built in `tasks.py` from the same string, never retyped.

**Why it is a superset.** The extractor:

- splits the canonical text into lines at `\n`, the only terminator left after canonicalisation;
- calls `_CHECKBOX_RE.match` at each line start of the masked body, with `endpos` at the line end;
- keeps a match only when the original and masked text are equal through the closing `]` (the #218 rule).

So the **original** text of every task line matches `_CHECKBOX_PREFIX` at a line start, because original and masked are equal through `]`.

The lookahead is deliberately left out of the gate. The extractor evaluates it on the **masked** text, where the character after `]` may be part of a masked code span. A `- [ ]` followed directly by a code span reads as a space there, and as U+0060 (the backtick) in the original. Including the lookahead would break the superset, and a scratch fuzz run confirmed the miss.

With `re.MULTILINE`, `^` matches at every line start of the canonical text. The body is a suffix of that text and starts at a line start. The gate runs over the whole text, frontmatter included, so it can only match more.

It over-matches checkboxes in code fences, in frontmatter values and in non-task contexts. Each over-match costs one refresh, never a wrong answer. A property test pins the superset, with deterministic generated notes built from task-shaped fragments so that thousands of them hold tasks.

**The same pre-publication question for `edit_note` and `set_frontmatter`** (checked for review r1). Both read the incumbent through the anchored target and publish with `expected=` those bytes, so the bytes the gate examines as "replaced" are exactly the bytes the publication replaced; a different file at the leaf refuses the publication (`concurrent_write`) and no refresh runs. The only residual is #428 L26's declared window between `expected=`'s comparison and the rename: a non-cooperating writer landing inside it is overwritten, and if its bytes alone held a task (neither the read nor the published bytes did), no refresh runs; its own refresh, if any, left the row task-dirty, so the next pass converges (L10).

**Failure.** The caller wraps the gate: any exception from it counts as "may hold". A gate fault can only cost a refresh. It can never skip one, and it can never fail the write.

**Cost.** Per side: one strict decode and one linear regex scan, with possessive quantifiers as in the extractor. It reads no file and no row. Both sides are scanned in **one** `asyncio.to_thread` call, so a 10 MiB note is never scanned on the event loop.

**Rejected: gating on the row's task count** (`EXISTS (SELECT 1 FROM note_tasks WHERE note_id = …)`). It is exact for the "contained" half, and it catches rows that an outside edit left stale. But it costs a database round trip on every note write, including the large majority with no checkbox, which is the cost #432 asks to avoid. The byte gate covers the bytes this call replaced, so the only staleness it leaves is staleness this write did not cause (L5).

### D3. The clear: `refresh_note_tasks`' absence form, for `delete_note`

`refresh_note_tasks(uid=…, root=…, target=…, rel=…, published=ABSENT)`, where `ABSENT` is a module sentinel (not `None`), runs R1 with three changes:

- **Step 3a, still current.** Under the row lock, `target.lstat()` must be `None`: nothing at the leaf, through the anchored parent descriptor. Anything there means another writer has published at the path since the delete, so the clear writes nothing (`superseded`). A note recreated at the path is therefore never cleared.
- **Steps 4–5.** Delete the note's `note_tasks` rows. There is nothing to derive.
- **Step 6.** Stamp `tasks_source_hash = canonical_note_hash(b"")` (the empty note, whose tasks are none), `tasks_refreshed_at = clock_timestamp()`, `tasks_failed = false` and `tasks_truncated = false`. No other column is written. The row itself is the pass's to prune.

**Why the empty-note hash.** It is a real canonical hash, so no new sentinel value enters the column, and it says what the rows now describe: no tasks.

- It differs from the row's `content_hash` unless the deleted note was itself empty, in which case nothing changes: no tasks before, none after, and the pass prunes the row.
- The row is therefore task-dirty (R3), and R4 keeps the cleared state against a pass that read the note before the delete.
- If a file reappears at the path before the pass (a sync, or a human restoring from `.trash`), the pass walks it, R3 forces a read, and it derives from disk.
- If no file reappears, the walk does not find it and the prune deletes the row, cascading to any task rows.
- The #309 rule is unchanged: a row beneath an unlistable directory is not pruned, and it keeps its cleared, empty task set, which is true of a deleted note.

### D4. Placement, ordering, locks

For each refreshing writer, in order:

1. Publication, confirmed through `_confirmed_publication`, exactly as today.
2. The change-feed event, exactly as today: the same call, the same position, the same fields.
3. The gate (D2), off the loop (none for `delete_note`).
4. The refresh or clear, through one helper `_refresh_after_write(uid, target, *, replaced, published)` built on the existing `_refresh_task_rows`. It runs **inside the tool's `with target:` block**, so the anchored descriptor R1's re-check needs is still open.
5. The response, byte-identical (D5).

Then `_tracked` writes the usage row (after the body returns) and releases the concurrency lease (in its `finally`). The refresh therefore runs **under the call's lease and before its usage row**, as the task tools' refresh does, and changes neither.

**Why after the event (review finding 4).** The refresh is a best-effort tail, and placing it last leaves every generic writer's existing publish → event sequence untouched. It also means a call cancelled during the refresh has already recorded its attributed event.

The cost is that a feed consumer reacting to a generic writer's `modified` event could list tasks before the refresh commits, and see the old rows for the refresh's duration (at most one derivation plus a 2 s lock wait). The consumer converges as soon as the refresh commits, or at the next pass.

No code forces the other order. The feed append and the refresh each run in their own transaction, take disjoint locks (the scope's feed advisory lock; the note's row lock) and never nest, and neither reads the other's writes.

**The three task tools keep publish → refresh → event.** They are not reordered here: their event is specified as "after the write-time task refresh" in the `change-feed` capability. A task tool cancelled during its refresh has published its bytes, records no attributed event (the pass later records the change as `human/sync`), returns no response and writes no usage row, and its lease is released by `_tracked`'s `finally`.

**Cancellation of a generic writer during its refresh.** The cancellation (a client disconnect or a shutdown) surfaces as `CancelledError` inside the refresh:

- **File:** the published bytes stand. For a delete, the leaf stays deleted.
- **Event:** the attributed event is already recorded.
- **Task rows:** either unchanged, or fully replaced, never a partial set. If the cancellation arrives before the refresh's `COMMIT` is sent, its transaction rolls back, the rows are unchanged, and they converge at the next pass (the publication changed the stat; for a delete, the walk no longer finds the file). If it arrives during or after the `COMMIT`, PostgreSQL may already have committed, and the rows are then exactly those derived from the published bytes (or, for a delete, cleared). That is harmless, because they describe what is on disk. Every task-row write of a refresh happens in one transaction with one commit (`src/services/task_index.py`, `_run_locked`, the single `session.commit()`), so there is no partial state; a structural test pins the single commit. (Implementation review r2, MINOR.)
- **Response:** none. The call is cancelled.
- **Usage row:** none. `_tracked` deliberately does not catch `BaseException`, and a cancelled call writes no row, exactly as a call cancelled anywhere else in its body today.
- **Lease:** released by `_tracked`'s `finally` (pinned by an offline test in `queue` mode: a lease is held during the refresh, and every controller counter is back to zero after the cancellation).

**Locks are R1's, unchanged.**

- The only lock is the note's `notes_metadata` row lock, under `SET LOCAL lock_timeout = TASK_REFRESH_LOCK_WAIT_MS` (2 s).
- The generation lock is **never** taken. The existing AST guard in `tests/test_note_tasks_extractor.py` covers `task_index.py`, and it is extended to the new helper.
- None of the refreshing writers holds a session or a row lock across its publication or its event, so there is no self-wait.
- The refresh's session is sequential with the feed append's, never concurrent, so a call's peak number of pooled connections is unchanged.

**What never refreshes:** a refusal, a dry run, a "No changes" answer, a publication that raised (including an in-call conflict), and an uncertain publication. None of these records an event either.

### D5. Output and usage: silent, with one usage key

The generic writers' responses stay **byte-identical**, whatever the refresh did. Their texts are pinned by the existing response tests and by the 1.0 tool-contract promise. An agent parsing `Created note:`, `Replaced …`, `Updated frontmatter in …`, `Wrote N bytes …` or `Soft-deleted: …` must see no new line.

**A lag is therefore silent to the agent (L3).** If the row lock is held for more than 2 s, or the file was superseded, or the refresh errors, the agent gets its usual success text, and a `list_tasks` right after it shows the old rows until the next pass. This is the behaviour of every generic write before this change, now in a minority of cases rather than all of them. Best-effort freshness with no new response text was decided by the implementing session on 2026-11-12 (review finding 2), pending the owner's confirmation.

**Rejected:** printing `index: may lag (<code>)` on a lagged generic refresh. It would change the success text of four tools and every test pinning it, and it would put index state into tools whose callers mostly never list tasks.

**Usage.** When a refresh or clear is attempted, `timing.record("task_refresh", …)` adds the result-side key with the existing values, `refreshed` or `lagged:<code>`. When the gate does not pass, **the key is absent**, so a write to a note without a checkbox logs exactly what it logs today. A lag is not an error marker and never changes the outcome classification (`BodyOutcome`, disposition, marker).

`refresh_note_tasks`' WARNING is reworded from "after a task write" to "after a write". It already names only the code and the exception class.

### D6. Bounds per call, and large notes

Each refreshing writer makes **at most one** refresh or clear per call. Its costs are R1's:

- one gate scan of at most two byte strings, each at most `MAX_NOTE_BYTES`, off the loop;
- one derivation of at most `MAX_NOTE_BYTES`, off the loop and outside the transaction. The extractor stops at `MAX_TASKS_PER_NOTE` (5,000) tasks and sets `tasks_truncated`, exactly as the pass does;
- one transaction: the root check, a row lock with a wait of at most 2 s, one re-read of at most `MAX_NOTE_BYTES` through the anchored target (for the clear, one `lstat`), one delete, inserts in batches of 1,000 (at most 5), and one update.

Every generic writer's published note is at most `MAX_NOTE_BYTES`. `edit_note` and `set_frontmatter` refuse larger results. `write_file` caps a `.md` path at `min(MAX_NOTE_BYTES, MAX_FILE_WRITE_BYTES)` (`_write_cap_for`). So the re-read cap equals the publication cap, and no published note is "beyond" the bound. A note with more than 5,000 tasks is stored truncated and counted `over the task cap`, as from the pass.

The worst-case added time for one call is one maximal derivation plus one short transaction, plus a 2 s lock wait. No new constant is needed.

### D7. Convergence: R3 and R4 hold for the new call sites, unchanged

The pass is not modified. The generic refresh writes exactly what a task tool's refresh writes, through the same function, under the same lock and the same re-check, so the D10 argument holds verbatim with "a task tool" read as "any refreshing writer". The only new state is the clear (D3), which is a refresh whose bytes are the empty note's.

The interleavings and their outcomes:

1. **The pass scans before the write.** The pass buffers B0. `edit_note` publishes B1 and refreshes, so the rows describe B1 with `tasks_refreshed_at > scan_started_at`. When the pass reaches the note, R4 sees `tasks_source_hash = H(B1) ≠ H(B0)` with a refresh after its scan, and keeps the B1 rows. The next pass reads the note (changed stat, and R3) and derives B1: clean.
2. **The write lands while the pass holds the note's row lock.** The refresh waits at most 2 s.
   - If the pass commits within the wait, the refresh then writes B1 over whatever the pass wrote.
   - If the wait expires, the outcome is `lagged:lock_timeout` and nothing is written. If the pass buffered B0, the changed stat makes the next pass read B1.
3. **The pass scans after the publication.** It buffers B1, and its rows and the refresh's agree whichever commits first. If the refresh commits last, `tasks_source_hash = content_hash` with a non-NULL `tasks_refreshed_at`. That is #428 L23, harmless.
4. **Two writers on one note, refreshes in order.** The rows describe the later bytes.
5. **Two writers on one note, refreshes out of order.** A publishes B1, B publishes B2, B refreshes (B2), then A refreshes. A's re-check reads B2 ≠ B1, so A is `superseded` and writes nothing. The rows describe B2. This holds across writer kinds (a task tool against `edit_note`, `edit_note` against `set_frontmatter`).
6. **An outside writer (Obsidian sync, a human) lands before the generic write's refresh.** The refresh is `superseded`. The outside write changed the stat, so the next pass reads the file.
7. **The outside write lands after the refresh commits.** The row is task-dirty (`H(B1) ≠ content_hash`), so R3 reads it on the next pass. R4 keeps B1 only for a pass that began before the refresh. The first pass to begin after it derives B2.
8. **A gated-out write after a refreshed one.** If neither the replaced nor the published bytes hold a checkbox, the rows already describe zero tasks for B1 (or older rows from a pass), and the stat change brings the pass.
9. **`delete_note` while a pass is mid-scan.** The pass buffered B0. The clear stamps the empty-note hash after `scan_started_at`, so R4 keeps the cleared rows, and the next pass prunes the row.
10. **A second writer adds a task between `delete_note`'s incumbent read and its unlink/rename** (review finding 1). The delete removes the newer file. The clear runs because it is not gated. The newer file's refresh, if it committed, is overwritten by the clear, and the listing shows no task of the deleted note.
11. **`delete_note`, then a recreation at the path before the clear takes the lock.** `target.lstat()` finds a file, so the clear is `superseded` and nothing is cleared. The recreating writer's own refresh, or the pass, covers the new file.
12. **`delete_note`, the clear, then a recreation before the pass.** The row is task-dirty, so R3 reads the new file and the pass derives it.
13. **A root reassignment between the publication and the refresh.** The outcome is `root_changed` and nothing is written. #428 L21 (an unlocked re-check) applies unchanged.
14. **A note moved onto `write_file`'s path between its existence sample and its publication** (implementation review r1). `write_file(overwrite=True)` replaces it; the refresh runs regardless of the sample (D1.3), with the replaced bytes unknown, so the rows describe the published bytes. Pinned by an offline test that lands the move inside the publication step.

**No row is ever stuck.** Every refreshing write leaves the row in one of two states:

- untouched (lag, superseded, cancelled), after a publication that changed the stat or removed the file, so the next pass reads or prunes it;
- or task-dirty until a pass that began after the refresh, which R3 guarantees will read it.

### D8. Statements this change reverses or narrows, and their replacement text

| Where | Current | Replacement |
|---|---|---|
| Archived `2026-11-10-task-tools` L5 | "`edit_note`, `set_frontmatter`, `move_note`, `write_file` and transfer uploads do not refresh task rows. Their changes appear after the next pass." | Not edited: archives are history. Narrowed by this change's D1, L1 and L4. |
| Archived L14 (the duplicate scan misses generic writes) | as written | Narrowed, not edited: the index side now reflects `edit_note`, `set_frontmatter` and `write_file` writes whose refresh ran. |
| `CLAUDE.md`, "Task writes address a line…" | "Generic writers do not refresh (L5)." | "Since #432 `edit_note`, `set_frontmatter` and every `write_file` to a note path also attempt the refresh, after their change-feed event, only when the replaced or published bytes may hold a checkbox (`tasks.may_hold_tasks`), and silently: their output is unchanged, and a lag converges at the next pass. `delete_note` always clears the deleted note's task rows (absence form, never over a recreated file). `create_note`, imports, uploads and `move_note`'s link rewrites wait for the pass." |
| `docs/architecture/indexing-and-embeddings.md`, the "What generic writers do not do (L5)." paragraph | the whole paragraph | Retitled "**Generic writers (#432).**" The new text gives D1's writer list, the D2 gate and its superset argument, the D3 clear, the D4 order with its cancellation outcome, and "the pass is unchanged; R3/R4 cover every refreshing writer; a refresh that cannot run leaves the rows to the next pass, as before". The L21 and L23 residuals are kept verbatim. |
| Same file, "What `list_tasks` can and cannot see" | "except an edit made by a task tool, which refreshes that note's task rows at once (#428, the next section). `edit_note` and the other generic writers do not." | "except a write whose refresh ran: the task tools (#428), and since #432 `edit_note`, `set_frontmatter`, a `write_file` to a note path, and `delete_note` (a clear). A refresh that cannot run, a new note (`create_note`, a `write_file` creation whose refresh finds no row, an import or an upload) and `move_note`'s link rewrites wait for the next pass." |
| `docs/architecture/vault-tools.md`, duplicate-scan paragraph | "a duplicate written by a generic tool since the last pass can be missed (L14)" | "a duplicate written since the last pass by `create_note`, an import, an upload, a `move_note` link rewrite, or a generic write whose refresh did not run can be missed (L14, narrowed by #432)" |
| `README.md`, task section | "`edit_note` and the other generic write tools do not refresh task rows, so their changes show up after the next pass (every 300 s by default)." | "`edit_note`, `set_frontmatter` and `write_file` also try to refresh the task rows of a note they change, and `delete_note` removes a deleted note's tasks; their output does not say whether that worked. A new note created by `create_note`, `write_file`, an import or an upload, links rewritten by `move_note`, and any refresh that could not run show up after the next pass (every 300 s by default)." |
| `docs/architecture/usage-attribution.md`, task section | no generic writers listed | One added paragraph: `edit_note`, `set_frontmatter`, `write_file` and `delete_note` gain the result-side key `task_refresh`, present only when a refresh or clear was attempted. |
| `src/services/tasks.py` docstring, "The write-time refresh exception (#428, design D10)" | "a task tool's write-time refresh … from the bytes its own call published and from nothing else" | "a write-time refresh, by a task tool or by a generic note writer (#432) … from the bytes its own call published, or, after `delete_note`, from the absence it caused, and from nothing else" |
| `src/services/task_index.py` docstring | "A task tool … publishes a note and then calls…" | Adds the generic writers, their order (after the event) and the absence form. |
| `list_tasks` description ("task writes refresh it at once") | unchanged | Still true, so **not** edited: editing it would change the tool-contract oracle (D9). |

### D9. Tool surface: unchanged

No tool name, argument, schema, title, annotation or description changes. `tests/fixtures/tools_list_full_v2.json` and `tools_list_readonly_oauth.json` must pass untouched. There is no migration, no new setting and no new constant. If the implementation finds either a migration or a description change unavoidable, it stops and reports.

## Rejected alternatives

- **No gate: refresh after every note write.** It adds a transaction to every write of a note without tasks, which is #432's stated concern.
- **Gate on the row's task count** (D2). It costs a round trip on every write.
- **Gate `delete_note` on the incumbent bytes.** Those bytes are read before the delete, and the delete removes whatever is there by then (D1.4).
- **Gate on the diff: refresh only when a changed line is a task line.** It misses heading-path changes, context-line changes and line-number shifts, all of which the rows store, and it is more code on the destructive path for less correctness.
- **Refresh after `move_note` link rewrites.** Cut (D1, L4).
- **Index the whole note at write time.** Rejected in #428 D10: it would be a second writer of the hottest code in the server.
- **Re-read the file after a streaming write.** It violates R1, and could launder a concurrent writer's bytes into refreshed state.
- **Buffer the upload or import body to refresh from it.** Up to `MAX_NOTE_BYTES` of extra memory per transfer, against the #322 body-memory design.
- **Refresh a new note by creating its index row.** That is indexing at write time.
- **Wake the index pass right after a write.** It would cover creations, transfers and link rewrites, but it is a scheduler change with its own fairness and tenancy questions. A possible follow-up.
- **Refresh before the event, as the task tools do.** A feed consumer would never see an event before the rows. But it would change every generic writer's event sequence and widen the window in which a cancelled call loses its attributed event (D4).
- **An `index:` line on the generic writers** (D5).

## What existing behaviour changes

- After `edit_note`, `set_frontmatter` or `write_file` (to a note path) on a note whose old or new bytes may hold a checkbox, the server refreshes that note's task rows in the same call when it can. When the refresh runs, `list_tasks`, `task_agenda` and `add_task`'s index-side duplicate scan reflect the write in the next call. When it cannot run, they reflect it after the next pass, as before.
- After every `delete_note`, its tasks leave listings when the clear runs.
- Writes to such notes take longer by one derivation plus one short transaction, and by at most a 2 s lock wait when a pass holds the row. Every delete takes one short transaction longer.
- Usage rows of those writes gain `task_refresh` (D5).
- Responses, schemas, descriptions, annotations, change-feed events and their order are unchanged.

## Accepted limitations

- **L1**: A new note's tasks appear only after the next pass. This covers `create_note`, `write_file` creating a note (its refresh finds no row), `import_from_url`, and an upload (creating or overwriting). An import or upload that overwrites a note with tasks also leaves its old rows until the pass.
- **L2**: `excluded_note_tags` reads `notes_metadata.tags`, which only the pass writes, so a tag change by `set_frontmatter` or `edit_note` changes that note's exclusion only after the next pass.
- **L3**: The refresh is best effort and its lag is silent. When it cannot run (`lock_timeout`, `superseded`, `error`, `root_changed`, `not_indexed`), the generic writer returns its usual success text, the next listing may still show the old rows, and they converge at the next pass. The usage row records the lag. The task tools still say `index: may lag`. Decided by the implementing session on 2026-11-12, pending the owner's confirmation.
- **L4**: `move_note`'s link rewrites (the moved note's own and every backlink source's) do not refresh. Until the next pass, a task whose text contained a rewritten link is listed with its old text and id, and a task write that uses that id refuses with `task_not_found` and writes nothing.
- **L5**: The gate considers only the bytes this call replaced and published. Rows left stale by an earlier outside edit are not corrected by a later gated-out write. That is the pre-existing pass lag, unchanged.
- **L6**: When `move_note`'s metadata transaction fails, the tasks stay listed under the old path until the pass, like every other row of that note.
- **L7**: A feed consumer that reacts to a generic writer's event may list tasks before that call's refresh commits (D4), for at most the refresh's duration.
- **L8**: The gate over-matches: a checkbox in a code fence or a frontmatter value triggers a refresh that writes the same rows. It costs time, never correctness.
- **L9**: #428 L21 (the unlocked root re-check) and L23 (a clean row keeping a non-NULL `tasks_refreshed_at`) apply to the new call sites unchanged.
- **L10**: Inside #428 L26's declared window (between `expected=`'s comparison and the rename), a non-cooperating writer's bytes that `edit_note` or `set_frontmatter` overwrite are not seen by the gate; when only those bytes held a task, no refresh runs and the next pass converges.
- **L11**: The clear's absence check and its `DELETE` are separate steps under the row lock, which does not serialise publication at the path. A note recreated between them is present when the clear commits: the rows cleared were the deleted note's, and the new note's rows wait for the pass, as any created note's do (L1). Implementation review r1, NOTE, accepted.

## Adversarial scenarios a reviewer should trace

Trace each through the code as implemented, against the expected outcome given.

1. **Every interleaving in D7 (1–14)**, with `edit_note`, `set_frontmatter`, a `write_file` overwrite and the `delete_note` clear in the writer role. After at most two passes, `tasks_source_hash = content_hash` and the rows equal `tasks_from_note_text(disk)`. No pass writes rows older than a refresh that began after its scan.
2. **Failure injection after a confirmed publication.** In each case the response text and outcome classification are byte-identical to the no-refresh run, and the change-feed event is recorded, because it precedes the refresh:
   - the gate raises: counted as "may hold", the refresh runs, nothing propagates;
   - the derivation raises: `derivation_failed`, rows removed, `tasks_failed` set;
   - the root check fails: `root_changed`;
   - there is no row: `not_indexed`;
   - the lock wait expires: `lock_timeout`;
   - the re-read fails or differs: `superseded`;
   - the insert fails: `error`, rolled back, rows unchanged;
   - the commit fails: `error`.

   **If the publication itself raises**, the refusal is unchanged and there is no event and no refresh.
3. **Cancellation during the refresh** (D4): the file stands, the event is recorded, the task rows are either unchanged (cancelled before the refresh's commit began) or fully replaced by the rows derived from the published bytes (cancelled during or after the commit), never partial, there is no response and no usage row, and the lease is released (integration test with a real held row lock; offline test of the lease in `queue` mode).
4. **A note at the size caps.** `edit_note` publishes exactly `MAX_NOTE_BYTES` with 5,001 checkboxes: 5,000 rows, `tasks_truncated = true`; the gate and the derivation run in a worker thread, never on the event loop's thread (a structural test records the thread each runs on; no wall-clock bound, issue #401). The same for `write_file` at its `.md` cap. A file grown past the cap by an outside writer before the re-check: the re-read fails and the outcome is `superseded`.
5. **A 10 MiB adversarial gate input** (`- - - …`, `> > > …`, digits with no delimiter): `may_hold_tasks` returns within 2 s.
6. **The gate's superset property.** No input on which the extractor finds a task and the gate says no. Inputs to cover: CRLF and lone CR, U+0000 inside the brackets, a BOM, blockquote and callout prefixes, ordered markers, a code span directly after `]`, and a last line without a terminator.
7. **Multi-user ownership.** Users 7 and 8 each have `Notes/a.md` with tasks. User 7 edits, then deletes. User 8's task rows and `tasks_*` columns stay byte-identical. Single-user (NULL owner) also works, and a user-7 call never touches a NULL-owned row.
8. **`delete_note` specifics.**
   - Soft and permanent.
   - Guarded and unguarded: the clear runs in every case.
   - The finding-1 interleaving: a task is added between the incumbent read and the delete, and the clear still removes the rows.
   - A recreation before the clear: `superseded`.
   - A `.trash` restore before the pass: an R3 read, and the rows are derived.
9. **No generation lock.** The AST guard covers the helper and every call site. A refresh contending with a pass that holds the generation lock and a row lock completes or lags within 2 s, and never deadlocks.
10. **Responses.** For each refreshing writer, the success text for a note with tasks equals the text with the refresh disabled. `tools_list_full_v2.json` is unchanged.
11. **Usage.** A write to a note without a checkbox logs params byte-identical to aafb9f0's. A write to a note with tasks adds only `task_refresh`.

## Test plan

- **Unit, `tests/test_note_tasks_extractor.py`:**
  - the deterministic property test of the gate's superset, built from task-shaped fragments so thousands of generated notes hold tasks;
  - linear time;
  - pattern identity;
  - the masked-lookahead case;
  - the AST guard extended to the new helper.
- **Unit, a new `tests/test_task_refresh_generic.py`, with the refresh stubbed:**
  - the writer and exclusion matrix;
  - the gate decisions, including unknown incumbents and the equal-bytes rule;
  - no call on a refusal, a dry run, a no-change answer or a raised publication;
  - the publish → event → refresh order;
  - byte-identical responses for every refresh code;
  - the usage key present or absent;
  - the cancellation outcome.
- **Integration, `tests/integration/test_task_refresh_generic_pg.py` (new, through the registered tools; `test_task_refresh_pg.py` keeps the R1 cases):**
  - list → write → list with no pass between, for `edit_note`, `set_frontmatter` and `write_file`;
  - a `write_file` creation (`not_indexed`, no row written);
  - a call cancelled while its refresh waits for a held row lock;
  - an edit that removes the last checkbox removes the rows;
  - `delete_note` → list, soft and permanent;
  - the finding-1 interleaving;
  - the recreated path;
  - D7's interleavings with `edit_note` and the clear;
  - out-of-order refreshes across writer kinds;
  - multi-user;
  - `lock_timeout` with a held row lock, with the response unchanged and the event recorded.
- **The offline suite** and the tool-contract tests, unchanged and green.

## Review record

**Who decided.** Every product decision in this change was decided by the implementing session on 2026-11-12, pending the owner's confirmation; none is owner-accepted. The pull request lists them for the owner to confirm or overrule:

- the refresh is best effort and its lag is silent to the agent (L3, D5);
- `delete_note` clears the deleted note's task rows after every confirmed delete (D1.4, D3);
- `set_frontmatter` refreshes, for line numbers (D1.2);
- `create_note` is excluded (D1, L1);
- `move_note` link-rewrite refreshes are cut (D1, L4);
- the generic writers run publish → event → refresh, while the task tools keep publish → refresh → event (D4).

Codex spec review, round 1 (verdict `FAIL`: 2 MAJOR, 3 MINOR, 1 NOTE), and the dispositions the implementing session took:

1. **The delete gate could skip the clear** (MAJOR). Fixed: the clear now runs after every confirmed delete, with the under-lock absence check kept (D1.4, D3, D7.10).
2. **The unconditional "next listing reflects the write" claim** (MAJOR). Resolved as best-effort freshness with a silent lag, recorded as L3, decided by the implementing session on 2026-11-12, pending the owner's confirmation. The claim is removed throughout.
3. **The equal-bytes skip for an unguarded `write_file`** (MINOR). Fixed: the skip applies only when the replaced bytes are known (D2).
4. **The failure matrix and cancellation** (MINOR). Fixed, and the order changed to publish → event → refresh for the generic writers (D4).
5. **The 40-source scenario** (MINOR). Moot after 6.
6. **Link-rewrite refreshes in `move_note`** (NOTE). Cut (D1, L4), with the 32-note constant.

Implementation review, round 1, at `1c53083` (Codex `FAIL`, 1 MAJOR, 1 NOTE; the independent verifier: 2 blocking gaps), and the dispositions the implementing session took:

1. **`write_file` keyed the refresh on a pre-publication existence sample** (MAJOR, found by both). Fixed: the refresh is attempted after every publication to a note path (D1.3, D7.14); `edit_note` and `set_frontmatter` were checked for the same shape and are bound to the bytes they read (D2, L10).
2. **The clear's absence check is not atomic with its `DELETE`** (NOTE). Accepted as L11.
3. **The cancellation clauses (lease, unchanged rows) were untested** (verifier). Integration test with a real held row lock, and an offline lease test in `queue` mode.
4. **The 100 ms event-loop bound was unmeasurable** (verifier). Replaced by the structural requirement that the gate and the derivation run in a worker thread, tested by recording their threads.

Implementation review, round 2, at `b55bb5a` (Codex `PASS`, 1 MINOR): the cancellation clause claimed the task rows stay unchanged, which is false for a cancellation arriving during or after the refresh's `COMMIT`. Narrowed to "unchanged or fully replaced, never partial" (D4, the spec's cancellation clause); no code change.
