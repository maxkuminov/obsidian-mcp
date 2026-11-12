## Context

**What the code does today** (verified on `main` at aafb9f0):

- **The task write runner.** `_run_task_write` (`src/mcp_server/tools.py`) is the #428 D2 sequence for one note: `_precondition_syntax_error`, `open_mutable`, `_leaf_state_error`, then per attempt `read_bytes_at` under `MAX_NOTE_BYTES`, the `_precondition_error` ladder (`enforceable=False`: the `(path, id)` pair satisfies required mode), UTF-8 decode, a pure planner off the loop, `_note_size_error`, `dry_run`, `_confirmed_publication(write_file_at(…, expected=existing_bytes))`, the write-time refresh (`task_index.refresh_note_tasks`), and one `modified` event. An in-call conflict without `expected_hash` repeats from the read, at most `TASK_WRITE_MAX_ATTEMPTS = 3`.
- **The planners** (`src/services/task_write.py`) return a `Plan` of `Span`s over the original text, never a regenerated note, and every plan passes `verify` (#428 D5). `verify` and `_line_map` accept only two span kinds: a line-confined edit of one declared target line, and a whole-line insertion at a line boundary. **There is no line deletion anywhere in the task tools today.**
- **The continuation block** (`continuation_block`, `_owners`, `_shape`) is uncapped: every following line with a greater blockquote depth, or the same depth and a greater indentation width (tab = 4), crossing blank lines; trailing blank lines are not part of it.
- **`add_task` placement** (`plan_add`, `_region`): region = the `section` body or the whole post-frontmatter body; anchor = the last task line in the region whose indentation width after its blockquote prefix is 0; the new line goes after the anchor's continuation block, copying the anchor's blockquote prefix and bullet; with no anchor, after the region's last non-blank line; in an empty region, after the heading or at body start. The target-note duplicate check refuses an open task with the same normalised description.
- **`move_note`** is the only tool that publishes more than once. A failure after its first publication (the rename) is a **partial success**, rendered with `disposition="partial"` and never `nothing_written: true` (`BodyOutcome` refuses that combination at construction). Each publication takes its own `_confirmed_publication`.
- **The refresh** takes exactly one `notes_metadata` row lock, with a 2-second `lock_timeout`, in its own transaction, and never the generation lock. The change-feed append takes the scope's advisory lock last, in its own short transaction.
- **The `MCP-REFUSAL` payload** (`refusals.Refusal.as_payload`) renders `code` and the optional `path`, `current_hash`, `cap_name`, `cap_bytes`, `nothing_written` (absent, never null).
- **Counts on `main`:** 32 tools; 19 read-only; write bucket 11; `WRITE_REQUIRED_TOOLS` 12; a read-only grant lists 20; `CLASS_MAPPING_VERSION` 6; `SERVER_INSTRUCTIONS` 1,812 characters with a 505-character lead.

## Goals / Non-Goals

**Goals:**

1. A move never loses a byte of the task or of either note. A duplicate is acceptable; a loss is not.
2. Every intermediate state is named to the caller, and a retry of the same call is safe and converges.
3. The moved lines arrive byte for byte, apart from one defined re-framing; every other line of both notes is untouched; ambiguity is refused.
4. The same envelope as the other task writes: resolution from the current bytes, verification before every publication, read-your-writes, attributed events, typed outcomes.

**Non-goals:** see `proposal.md`.

## Decisions

### D1. Arguments, and what the id is after the move

```
move_task(path, id, to_path, to_section=None, allow_duplicate=False,
          dry_run=False, expected_hash=None, to_expected_hash=None)
```

| Argument | Meaning |
|---|---|
| `path`, `id` | The source note and the task, exactly as `complete_task` / `update_task` take them (#428 D1, D2). |
| `to_path` | The destination note. It must exist; `move_task` never creates a note (`not_found`, as `add_task`). It may equal `path`. |
| `to_section` | A heading in the destination, addressed as `add_task(section=…)` addresses one, with the same refusals (`selector_unresolved`, the frontmatter-defect and unmatched-fence `content_unsafe`). Omitted: the whole body. |
| `allow_duplicate` | Insert even when the destination already holds a duplicate (D9). It never disables convergence. |
| `dry_run` | Plan, verify and return both diffs; publish nothing. |
| `expected_hash` | Binds the **source** note's whole bytes, through the existing ladder. |
| `to_expected_hash` | Binds the **destination** note's whole bytes, through the same ladder. |

Argument checks (`invalid_argument` unless stated), all before any read: the id grammar; a suffixed id without `expected_hash` → `precondition_required` (#428 D2, every mode); `path` and `to_path` capped at `MAX_PATH_CHARS`, `id` at `TASK_ID_ARG_MAX_CHARS`; the syntax of both hashes (`malformed_precondition`, naming which argument).

**The id after the move.** The key is `sha256("omcp-task-v1\0" + uncut text)[:12]`, and the text excludes the status, indentation, list marker and blockquote prefix (#428 D1). Re-framing (D4) changes only the excluded prefix, so **every moved task keeps its key**: the moved task and every sub-task in its block.

- The rendered id is bare when the destination holds exactly one task with that key after the move, and `<key>.<i>of<n>` otherwise (only possible with `allow_duplicate=true`, or for a sub-task whose text repeats in the destination).
- Ordinal suffixes are renumbered by document order **on both sides**: a source task that shared a key with a moved task, and a destination task that shares a key with one, may change its `.<i>of<n>`. The response names the moved tasks' new ids and says, when it happened, that suffixed ids sharing a moved key were renumbered in either note (D18). An id from an earlier listing of either note that carried a suffix must be listed again; a bare id stays valid wherever it still resolves.

### D2. Resolution in the source

Exactly #428 D2 steps 1–9, over the source note's bytes read through its anchored descriptor: `task_not_found`, `task_ambiguous`, `task_changed`, `size_limit` over the 5,000-task cap, `content_unsafe` on a defective frontmatter block, `task_malformed` on NUL. No line number from the index or a listing is used. The id names any checkbox, whatever its status (open, done, cancelled, custom) and whatever the caller's task rule (#434 D7: the rule never blocks a write).

### D3. The moved block, and what is refused

The **moved block** is the task line `t` plus its continuation block (#428 D8), uncapped: lines `t … e−1` where `e = continuation_block(lines, t)`. Sub-tasks, context lines, blank lines crossed inside the block, and deeper-indented fences all belong to it. The 8-line / 600-character context cap of the index never applies to a write.

**A nested task moves with its subtree.** Moving a task that has sub-tasks moves them all. Moving a sub-task (a task inside another task's continuation block) is allowed: it takes its own subtree, leaves its parent and siblings in place, and lands at the destination as a top-level task (D4, D5). The response says `was nested under line N` (open question Q1). Moving the line alone is never done: its children would silently change owner.

Refused with `task_malformed`, nothing written, naming the line:

- **a heading inside the block.** An ATX heading line (by the masked-text heading scan `verify` already uses) moving between notes changes both notes' section structure and every `#N` section ordinal after it;
- **a line that cannot be re-framed** (D4);
- **the block's code status is not self-contained**: a fence opened inside the block and closed outside it, or the reverse, so that removing or inserting the block would change the code status of a line that is not moved (D8 catches it; the refusal names it);
- a NUL in either note (as #428).

### D4. Re-framing: the one transformation the moved bytes undergo

Let the **source frame** `F_s` be the task line's text before its list marker: its blockquote prefix `Q_s` (every `>` and the spaces between them, plus at most one space after the last `>`, as `_shape` measures it) followed by its indentation `I_s`. Let the **destination frame** `F_d` be the destination anchor's text before its list marker (D5). By D5's anchor rule the anchor has indentation width 0, so `F_d = Q_d`, a blockquote prefix or the empty string.

Every line `L` of the moved block becomes:

| `L` | Becomes |
|---|---|
| starts with `F_s` (byte-exact) | `F_d + L[len(F_s):]` |
| blank (whitespace only after its blockquote prefix) and does not start with `F_s` | `Q_d` with trailing spaces and tabs removed |
| anything else | the move is refused `task_malformed` ("line N under the task does not start with the task's own indentation and quote prefix, so it cannot be re-indented without a guess") |

- **When `F_s = F_d` the block arrives byte for byte.** That is the common case: a top-level task moved to a top-level position outside any quote, where both frames are empty.
- Relative structure is preserved: a child indented four spaces deeper than the task is four spaces deeper than the task at the destination, a deeper quote level stays deeper, and the list marker of every line, the moved task's own included, is kept as written (an ordered `3.` stays `3.`; nothing is renumbered, L9).
- The re-framing never touches content after the frame: no field, tag, status, rule text, `^block` link or trailing whitespace changes.
- Rejected: **no re-framing at all** (open question Q2). A sub-task moved verbatim keeps its four-space indentation, so at the destination it becomes a child of whatever task precedes it, or, after a blank line or a paragraph, an indented code block in which the extractor sees no task. Either is a silent wrong result. Re-framing to the destination's top level is the minimal change that keeps the task a task and an owner-less top-level item.
- Rejected: **re-framing by indentation width** (tab = 4 arithmetic). Converting between tabs and spaces rewrites bytes the caller never asked to change, and CommonMark list continuation depends on columns that width arithmetic does not preserve. The prefix rule is byte-exact or it refuses.

### D5. Placement at the destination

Exactly `add_task`'s rule (#428 D8), applied to the destination note after any same-note removal (D13):

1. **Region** `R`: the `to_section` body, or the whole post-frontmatter body.
2. **Anchor**: the last task line in `R` whose indentation width after its blockquote prefix is 0. A task that is part of the moved block is never the anchor.
3. **Insertion point**: directly after the anchor's full continuation block; with no anchor, after `R`'s last non-blank line; with `R` empty, directly after the section's heading line, or at body start.
4. **Frame**: `F_d` = the anchor's blockquote prefix (empty without an anchor). Unlike `add_task`, the moved task keeps **its own** list marker; only the frame comes from the anchor.
5. **Planned owner** of the moved task line: the anchor's owner, as `add_task`'s new line (None at top level). Every other moved line's planned owner is the image of its source owner inside the block.

### D6. Terminators

The moved lines' **content** is byte-exact under D4. Their **terminators** are the destination's: every moved line gets `T` = the terminator of the line it is inserted after, else the note's first terminator, else LF (the #428 D7 rule), so a CRLF note never receives LF lines or the reverse. When the insertion is at the end of a file that has no final terminator, `T` is prepended instead, and the file still ends without one (#428 `_insertion`). Rejected: keeping the source terminators, which would make a CRLF destination mixed (open question Q8).

### D7. The removal span at the source

The removal deletes lines `t … e−1` with their terminators, as one span `[start of line t, start of line e)`. One exception keeps the file's final-terminator state: when line `e−1` is the last line and has no terminator, the span starts at the end of line `t−1` instead (it takes the preceding terminator), so the file still ends without one. When `t` is the first body line and the block runs to the end of the file, the body becomes empty and the frontmatter block is untouched. Blank lines before `t` and after `e−1` are untouched (two blank lines may then be adjacent; nothing is collapsed).

### D8. Verification before each publication

Each plan is verified by a pure function that re-derives everything from the old and new texts, in the #428 D5 style, before any dry-run answer and before publication. The existing `verify` is not changed; `verify_removal` and `verify_move_insert` (or one generalised function that leaves `verify`'s existing behaviour byte-identical) are added. A failure is `task_malformed`, with nothing written.

**Destination (`verify_move_insert(old_d, new_d, plan)`):**

1. `new_d` is `old_d` with exactly one whole-line insertion at a line boundary; every pre-existing line keeps its content and terminator (the end-of-file terminator exception of #428 included).
2. The inserted lines equal the re-framed block line for line, content and terminator (D4, D6).
3. Every pre-existing task re-extracts identically (`_COMPARED` fields), shifted by the inserted count.
4. The new task set is the old set plus exactly the moved tasks, and **each moved task re-extracts with the same full text, key, status character, state, every date field, priority, tags, recurrence and `text_cut` as at the source**. Tags are therefore conserved by construction.
5. No pre-existing line changes its owning task; each moved line is owned as planned (D5).
6. The code mask of every pre-existing line is unchanged; each moved line's mask, re-framed, equals its source mask re-framed (its code status is the same); the headings and the frontmatter block are unchanged.

**Source (`verify_removal(old_s, new_s, plan)`):**

1. `new_s` is `old_s` with exactly the D7 span removed; every other line keeps its content and terminator.
2. Every remaining task re-extracts identically, shifted by the removed count.
3. The new task set is the old set minus exactly the moved tasks.
4. No remaining line changes its owning task.
5. The code mask of every remaining line, the headings and the frontmatter block are unchanged.

A plan whose result exceeds `MAX_NOTE_BYTES` is `size_limit` (`_note_size_error`), and one whose extraction would exceed the 5,000-task cap is `size_limit`, in either note, before anything is published.

### D9. Duplicates at the destination, and convergence

Both are decided from the **destination's current bytes**, never from the index.

**Convergence (checked first).** If the destination already holds a task with the moved task's key whose block, compared after re-framing both to the empty frame, is byte-identical (content, line for line) to the moved block, nothing is inserted. The plan is the source removal alone, and the response says `destination already held this task at line N; nothing was inserted, and the source copy was removed`. This is what makes a retry after a partial outcome converge (D10), and it can never lose a byte: the bytes removed from the source exist at the destination. `allow_duplicate` does not disable it (open question Q4; L3 states the cost).

**Duplicates (refused `duplicate_task` unless `allow_duplicate=true`, nothing written), naming up to 5 matches as `path:line id`:**

- (a) a task in the destination with the moved task's key whose block differs (the same line, other context or sub-tasks);
- (b) when the moved task is open: an open destination task with the same normalised description (#428 D9, `add_task`'s target-note rule, rule-independent as #434 Q1).

A done or cancelled task is checked by (a) only: an archive note legitimately holds many done tasks with one description. The index-side scan of other notes and the similar-task list of `add_task` are not run: the task already exists in the caller's scope, so a move creates no new task to compare.

**Same note** (D13): the moved task is excluded from both checks against its own note.

### D10. Publication order and the state after every failure

**Order:** read and plan both notes → publish the destination insert → refresh the destination's task rows → record its event → publish the source removal → refresh the source → record its event. The destination is published first because a failure after it leaves a duplicate, and a failure after a source-first removal would leave a loss.

**The source removal is planned against bytes the destination publication did not touch,** and is published with `expected=` those bytes. It is never published unless the destination publication returned success (or convergence found the copy already there).

| # | Step | What happens | Destination | Source | Caller receives |
|---|---|---|---|---|---|
| 1 | arguments, hash syntax | refused | unchanged | unchanged | the refusal, `nothing_written: true` |
| 2 | open, read, ladder, resolve, plan and verify **either** note | refused | unchanged | unchanged | that refusal; a precondition refusal names which note (`path` in the sentinel is that note) |
| 3 | `dry_run` | | unchanged | unchanged | both diffs (destination first), the moved lines, the new ids |
| 4 | destination publish: in-call conflict | without `to_expected_hash`: repeat from row 2, re-reading **both** notes, up to 3 attempts in total; then, or with it, refuse | unchanged | unchanged | `concurrent_write`, `nothing_written: true` |
| 5 | destination publish: vault reassigned, anchor lost | refused by `_confirmed_publication` | unchanged | unchanged | the existing marker refusal |
| 6 | destination publish: OS error | the rename may or may not have happened | **uncertain** | unchanged | `publication_uncertain`, partial: "the destination may hold the task; the source is unchanged; a retry with the same arguments is safe" |
| 7 | destination refresh fails | the write stands | holds the task | unchanged | (continues) `index (destination): may lag (<code>)` |
| 8 | source publish: in-call conflict | without `expected_hash`: re-read the source and re-locate the moved block (below), up to 3 attempts in total; then, or with it, stop | holds the task | changed by another writer, task still there | `task_move_incomplete` (`concurrent_write`) |
| 9 | source re-locate fails | the key is absent, held by more than one task, or its block is not byte-identical to the moved block | holds the task | holds something else | `task_move_incomplete` (`source_changed`) |
| 10 | source publish: vault reassigned, anchor lost, confirmation outage | stop | holds the task | unchanged | `task_move_incomplete` (`vault_assignment_changed`, `vault_anchor_lost_at_publish` or `vault_confirmation_unavailable`) |
| 11 | source publish: OS error | the removal may or may not have happened | holds the task | **uncertain** | `task_move_incomplete` (`io_failure`), saying the source state is unknown |
| 12 | source refresh fails | the move stands | holds the task | task removed | success, `index (source): may lag (<code>)` |
| 13 | process killed between the two publications | no response | holds the task | unchanged | nothing; the next pass indexes both copies (L2) |
| 14 | everything succeeds | | holds the task | task removed | success (D18) |

**Re-locating the block for a source retry (rows 8–9).** Nothing from the first read is trusted. The source is re-read through the same anchored descriptor; the tasks are re-derived; the moved key must resolve to exactly one task (the original id was bare, since a suffixed id carries `expected_hash` and never retries), and that task's block must be byte-identical to the moved block as read at step 2. Only then is a new removal planned, verified (D8) and published with `expected=` the new bytes. If the task was edited (completed, re-dated, given a context line) after the destination copy was written, the source copy is **not** removed: removing it would lose the edit.

**A retry converges.** After any of rows 6, 8–11 and 13, calling `move_task` again with the same arguments:

- finds the task in the source unchanged and an identical copy at the destination → D9 convergence → only the source copy is removed;
- finds the source task changed (row 9) → its key or block differs from the destination copy → D9 (a) or (b) refuses `duplicate_task`, naming the destination copy, so a third copy is never written;
- finds the source task gone → `task_not_found`, nothing written; the destination copy stands.

No sequence of retries can grow the number of copies past two, and none removes a byte that does not also exist at the destination.

### D11. The partial outcome, `task_move_incomplete`

A new code in `refusals.BODY_CODES` (not `TASK_CODES`, which carry `nothing_written: true`). It is rendered with `disposition="partial"`, never `nothing_written`, and its prose begins with one fixed sentence an agent can rely on:

```
move_task: the task was added to `<to_path>` (line N, id <dest_id>) but was not removed from `<path>`: <reason sentence>. The task now exists in both notes. Calling move_task again with the same arguments removes the source copy once it is unchanged; nothing else will remove it.
destination content_hash: sha256:…
index (destination): refreshed
MCP-REFUSAL {"code":"task_move_incomplete","path":"<path>","destination_path":"<to_path>","source_id":"<id>","destination_id":"<dest_id>","reason":"<reason>"}
```

- `reason` is one of `concurrent_write`, `source_changed`, `vault_assignment_changed`, `vault_anchor_lost_at_publish`, `vault_confirmation_unavailable`, `io_failure`. For `io_failure` the sentence says the source's state is unknown, not that it still holds the task.
- **Four optional `Refusal` fields** (`destination_path`, `source_id`, `destination_id`, `reason`), rendered absent-not-null like the precondition fields, and set only by this code (a `Refusal` construction check enforces that). Paths are bounded as every path in the sentinel is; ids are server-derived hex. Rejected: carrying them in the prose only (agents are told to act on the sentinel, not the prose), and reusing `partial_completion` (it cannot be told apart from `move_note`'s). Open question Q3.
- The convergence case (D9) and a same-note move never produce it.

### D12. Preconditions, required mode and retries, per note

| | Source (`path`) | Destination (`to_path`) |
|---|---|---|
| hash argument | `expected_hash` | `to_expected_hash` |
| compared | immediately after each read, by the existing ladder | the same |
| required mode (`WRITE_PRECONDITION_REQUIRED=true`) | a bare id satisfies it (#428 D3: `(path, id)` binds the exact bytes removed); a suffixed id always needs `expected_hash` | exempt: an insertion removes no byte (#428 D3, as `add_task`) |
| in-call conflict, hash absent | the phase retries (rows 4, 8), at most 3 attempts per phase | the same |
| in-call conflict, hash present | no retry: row 4 refuses, row 8 stops partial | the same |
| `stale_precondition` | names `path` and its `current_hash` | names `to_path` and its `current_hash` |

**Same note:** `to_expected_hash` must be absent or equal to `expected_hash` (`invalid_argument` otherwise); one ladder runs.

A supplied `expected_hash` stays bound across both phases: the removal is published with `expected=` the bytes that matched it, so any change to the source after the check is a conflict, and with a hash supplied a conflict is never retried.

### D13. The same-note move

`to_path` that resolves to the source note is detected after `open_mutable`, by `rel` equality **and** by leaf identity (`st_dev`, `st_ino` of both leaves through their anchored parents). Two different `rel` paths to one inode (a hard link, or a symlinked folder the resolver does not collapse) are refused `invalid_argument` ("`path` and `to_path` name the same file"), because the two-phase flow would read and publish one file as if it were two.

For the same note:

- one read, one ladder, one plan: the removal span (D7) and the insertion (D5, computed on the note **with the block removed**, so the moved task is never its own anchor and `to_section` is resolved without it) as one span list over the original text, which must not overlap;
- one verification over the old and new text combining D8's checks: every line outside the block keeps content and terminator, every non-moved task identical, the task set unchanged, each moved task re-extracts identically, no non-moved line changes owner, masks and headings unchanged;
- one publication, one refresh, one `modified` event;
- a plan whose new text equals the old text (the task is already where it would go) answers `No changes for <path>` with nothing written, no event and no refresh;
- no duplicate check against itself, and no partial outcome is possible.

### D14. Concurrency and locks

- **No file locks.** Each publication is the existing optimistic `write_file_at(…, expected=)`: a conflict is detected, never waited on.
- **No two database locks are ever held together.** Each refresh is its own transaction holding at most one `notes_metadata` row lock (2-second `lock_timeout`), committed or rolled back before the next refresh begins; each change-feed append is its own transaction taking the scope's advisory lock last. Two opposite moves (`A → B` and `B → A`, different tasks) therefore cannot deadlock: neither ever waits while holding a lock the other needs. A test pins "one row lock per transaction" by asserting the refresh is called once per note, sequentially, and never inside another transaction.
- **Two opposite moves interleaved** both succeed or, after their bounded retries, end in `concurrent_write` (phase 1, nothing written) or `task_move_incomplete` (phase 2, a duplicate). Never a loss.
- **Two moves of the same task** to different destinations: both publish their destination; one removal wins; the other re-locates, finds the key absent, and returns `task_move_incomplete` (`source_changed`). The task is then in two destinations, a duplicate (L12). To the same destination: the second's phase-1 retry finds the first's copy and converges.
- **The residual of the shared publish path (#431) is inherited, not solved.** `expected=` compares the current bytes immediately before the rename, so a non-cooperating writer landing inside that window is overwritten, as with `edit_note` and the other task tools (#428 L26). A move has the window twice, once per note (L1).
- **Admission.** One `_tracked` call: one general-bucket token, **one write-bucket token**, one concurrency slot of class `write` held through both publications, one quota slot. All retries run inside it.

### D15. Refresh, change feed, usage, rate limit, class

- **Refresh, per note**, after that note's confirmed publication (#428 D10, unchanged): destination after the insert, source after the removal. Each reports `index (destination): refreshed | may lag (<code>)` and `index (source): …`. A same-note move has one `index:` line. Convergence publishes no destination bytes, so the destination is not refreshed and its line reads `index (destination): unchanged`.
- **Change feed:** one `modified` event per published note, `tool = 'move_task'`, attributed like every task write, each recorded after its own note's refresh: the destination's before the source removal is attempted, so a partial outcome still records it. No new event kind (L14). A refused call, a dry run and a no-change same-note move record none.
- **Usage row:** one per call. Logged params: `path`, `id`, `to_path`, `to_section`, `dry_run`, `allow_duplicate`, and the presence (never the value) of `expected_hash` and `to_expected_hash`. Result fields: `move_outcome` (`moved`, `moved_within`, `converged`, `no_change`, `incomplete:<reason>`, `uncertain`), `task_refresh` (source), `task_refresh_to` (destination), `task_write_attempts` (when > 1, per phase as `<p1>/<p2>`). No task text is logged. The outcome is classified from the typed `BodyOutcome` only.
- **Write bucket: one charge per call** (open question Q7). The bucket counts mutating calls, as `move_note` (which publishes up to 1 + N times) is charged once; one move is one intent.
- **Concurrency class `write`**; `CLASS_MAPPING_VERSION` 6 → 7, which starts a new readiness epoch for `make concurrency-report` (documented in `DEPLOYMENT.md` and `docs/deployment-kubernetes.md`).

### D16. The task rule and the default scope

**The rule is not consulted for the write.** A moved task is moved as it is: no global filter text is prepended (#434 D7 applies to `add_task` only), nothing is refused, and no rule statement is added, because the moved line's text is unchanged and the rule (`all`, `fields`, `filter`) reads only the line's text and fields, so its verdict cannot change with the move.

**The location part of the default scope can change.** When `to_path` lies under one of the caller's excluded folders (#428 D14, the `folder_prefix_clause` semantics) and the source did not, the response carries `note: the destination is in a folder your default task scope excludes, so default listings will not show this task; list_tasks(scope="all") will.` Excluded note tags are not evaluated (they live in the index's note tags, L8).

### D17. The contract surface, enumerated

Every literal below was found by grep on `main` at aafb9f0. The implementer re-greps before finishing.

| Fact | Before | After | Where it is pinned (files that change) |
|---|---|---|---|
| registered tools | 32 | **33** | `tests/test_issue_343_tool_metadata.py` (`len(listed()) == 32`), `tests/test_issue_358_scoped_tools_list.py` (`== 32` in several places, the parametrised counts, module docstring), `tests/test_issue_295_unknown_arguments.py` (two), `tests/test_issue_263_coverage_registry.py` (`len(registered) == 32`, `COVERAGE`, `ADDED_AFTER_INVENTORY`), `tests/test_transfer_overlap_gate.py` (docstring "all 32 tools") |
| `TOOL_CLASSES` entries | 32 | **33** | `tests/test_issue_261_controller.py`, `tests/test_issue_261_tool_admission.py` |
| `CLASS_MAPPING_VERSION` | 6 | **7** | `src/services/concurrency.py` (constant and its history comment), any test pinning the value |
| write bucket (`_tracked(write_class=True)`) | 11 | **12** | `tests/test_issue_343_tool_metadata.py` (`len(w) == 11`; `TASK_WRITE_TOOLS`), `README.md` ("the eleven write tools" ×3), `CLAUDE.md` ("the eleven vault-mutating tools", "eleven-tool write bucket"), `DEPLOYMENT.md`, `docs/architecture/rate-limits.md` (the `write` class row, line 51's list), `docs/architecture/vault-tools.md` ("eleven tools since #428") |
| `WRITE_REQUIRED_TOOLS` | 12 | **13** | `src/mcp_server/tool_permissions.py`, `tests/test_issue_358_scoped_tools_list.py` (`TWELVE`), `CLAUDE.md` ("twelve names" ×2), `README.md` ("the twelve write tools"), `docs/architecture/vault-tools.md` ("a frozenset literal of twelve names") |
| destructive set | write bucket − {`create_note`, `add_task`} ∪ {`request_upload`} | **unchanged rule**; `move_task` is in it | `tests/test_issue_343_tool_metadata.py` (the rule is read from code; the enumerated scenario list), `server.py` annotation comment, `README.md` destructive list (line ~289) |
| read-only annotations | 19 | 19 | unchanged |
| read-only grant's `tools/list` | 20 | **20** | `tests/fixtures/tools_list_readonly_oauth.json` **unchanged**; its entries compared against the v3 oracle |
| registration order | `add_task` last | `move_task` after `add_task` | the first 32 entries keep their order |
| full-list oracle | `tools_list_full_v2.json` (32) | **new `tests/fixtures/tools_list_full_v3.json` (33)**; v2 kept unedited as the 1.3 record, and a test asserts v3 minus `move_task` equals v2 byte for byte (open question Q9) | `tests/test_issue_358_scoped_tools_list.py`, `tests/test_issue_343_tool_metadata.py` (whichever reads the oracle), `CLAUDE.md`, `docs/architecture/vault-tools.md`, `README.md` (line ~1378) |
| additions fixture | 7 entries | **8** (`move_task` appended) | `tests/fixtures/tools_list_schema_additions.json` |
| `SERVER_INSTRUCTIONS` | 1,812 characters, lead 505 | **1,897**, lead byte-identical | `src/mcp_server/server.py`: "…prints, `add_task` inserts one, and `move_task` moves one, with the lines indented under it, to another note or section." |
| description | | `move_task` ≤ 4,000 characters, contract first | `tests/test_issue_343_tool_metadata.py` (the cap test runs unchanged) |
| vault guide | five task tools | six | `src/mcp_server/vault_guide_primer.md`, its test |
| publishing-tool registries | 12 publishing tools | **13** | `tests/test_issue_88_root_confirmed_before_publish.py` (the publishing set, the per-tool call map), `tests/test_symlink_mutation_guard.py`, `tests/test_anchored_note_writes.py`, `tests/test_write_preconditions_notes.py`, `tests/test_issue_192_write_refusal_marker.py`, `tests/test_issue_263_tool_outcomes.py`, `tests/integration/test_concurrency_tool_checkout_peaks_pg.py` |
| refusal codes | | `task_move_incomplete` | `src/services/refusals.py` (`BODY_CODES`), any test pinning the code set, `docs/architecture/vault-tools.md` "Refusal codes" |
| docs | | | `README.md` (tool count 32 → 33 at lines ~16, ~281, the task tool list, the upgrade note), `CLAUDE.md` (counts, the MCP tools section, the task-write key decision), `DEPLOYMENT.md` and `docs/deployment-kubernetes.md` (upgrade note: 33 tools, write bucket 12, `CLASS_MAPPING_VERSION` 7), `docs/architecture/vault-tools.md` (Tool metadata table, Who sees which tools, a new "Moving a task (#430)" section), `docs/architecture/usage-attribution.md` (the params table), `docs/architecture/rate-limits.md`, `docs/architecture/indexing-and-embeddings.md` (two refreshes per move) |

### D18. Responses

**Success (two notes):**

```
Moved task from `Inbox.md` to `Projects/Alpha.md`
line 14: - [ ] Call the bank 📅 2026-11-20
id: 3f9a0c1b2d4e
moved: 3 lines, 2 tasks (1 sub-task)
sub-task line 15: id 9b1c0a7e5f21
source: removed lines 5–7
destination content_hash: sha256:…
source content_hash: sha256:…
index (destination): refreshed
index (source): refreshed
```

- `line N:` is the moved task's line at the destination, rendered as the other task tools render a line (`_one_line`, paths escaped by `_escape_task_path`).
- Sub-task lines: up to 10, then `+N more sub-tasks`.
- Optional lines, each when it applies: `was nested under line N` (D3), `re-indented: quote/indent prefix replaced` (D4 with `F_s ≠ F_d`), `ids renumbered: tasks sharing a moved key now carry new .<i>of<n> suffixes in <which note(s)>` (D1), `block ids moved: ^a, ^b (links to <path>#^a no longer resolve)` (Q6), the D16 scope note.
- **Convergence:** `destination already held this task at line N; nothing was inserted, and the source copy was removed`, then the source lines.
- **Same note:** `Moved task within `X``, `line N:`, `id:`, one `content_hash:`, one `index:` line.
- **Dry run:** `Dry run — nothing was written.`, the same lines with the line numbers the call would produce, then `destination diff:` and `source diff:` (one `diff:` for a same note), each a unified diff whose header names the escaped path.

### D19. The planner interface (for independent slices)

```python
@dataclass(frozen=True)
class MoveArgs:
    id: str
    hash_supplied: bool
    to_section: str | None = None
    allow_duplicate: bool = False

@dataclass(frozen=True)
class MovedBlock:          # what was read at the source, for the phase-2 re-locate
    key: str
    lines: tuple[str, ...]  # content of lines t … e−1, without terminators, as read
    tasks: tuple            # ExtractedTask of each moved task, source coordinates
    nested_under: int | None
    block_ids: tuple[str, ...]

@dataclass(frozen=True)
class MovePlan:
    insert: Plan | None     # None: converged (D9)
    converged_line: int | None
    removal: Plan
    moved: MovedBlock

def plan_move(source_text: str, dest_text: str, args: MoveArgs, settings) -> MovePlan | TaskWriteRefusal
def plan_move_within(note_text: str, args: MoveArgs, settings) -> Plan | TaskWriteRefusal
def plan_relocated_removal(source_text: str, moved: MovedBlock) -> Plan | TaskWriteRefusal   # D10 rows 8–9
def verify_move_insert(old: str, new: str, plan: Plan, moved: MovedBlock) -> TaskWriteRefusal | None
def verify_removal(old: str, new: str, plan: Plan, moved: MovedBlock) -> TaskWriteRefusal | None
```

All pure, all run off the event loop; each returned plan has passed its verifier. `Plan` gains no field that changes the existing planners' behaviour; whatever the move needs (`removed_range`, `moved_lines`) is added with defaults that leave `plan_complete`, `plan_update`, `plan_add` and `verify` byte-identical in behaviour, pinned by the existing tests running unmodified.

## Alternatives rejected

- **"The agent does it with `add_task` then an edit."** Worse on every axis the issue cares about. `add_task` re-serialises the line from arguments, so the status, field order, rule text, `🆔`/`⛔`/`🏁`, block links and the continuation block are lost unless the agent rebuilds them; `add_task` cannot add a done task at all. The removal is an agent-composed `edit_note`, the very write that has clobbered notes before, with no verification that only the task's lines go. There is no ordering guarantee, no named intermediate state and no convergence, and each agent reinvents all three. A server-side move is one verified, ordered, typed operation.
- **Source removal first, then the insert.** A failure between them loses the task. Refused by the issue's bar.
- **One atomic publication of both notes.** No filesystem primitive renames two files atomically, and a journal or a staging protocol would be a new durable mechanism in the publish path, out of all proportion to triage.
- **A server-side journal of in-flight moves** to resume or roll back after a crash. It adds durable state whose own failure modes need design, for a window (D10 row 13) whose worst case is a visible duplicate that a retry converges.
- **Rolling back the destination insert when the source removal fails.** The rollback is itself a publication that can fail or race, and it removes bytes; a duplicate is the safer stopping point.
- **Moving the line only.** Its children would be re-parented to the previous task in the source (D3).
- **A dedicated `moved_task` change-feed kind.** `note_events` kinds are note-level; a task move is two note modifications, attributed to one tool. A consumer that wants the task-level view reads the tool name (L14).
- **Re-using `move_note`'s `partial_completion` code.** Indistinguishable from a note move's partial outcome, and its sentinel carries no destination (D11).

## Accepted limitations

- **L1** The shared publish path's residual window (#431) is inherited, once per note: a non-cooperating writer landing between a publication's byte comparison and its rename is overwritten. Not solved here.
- **L2** A process killed between the two publications returns nothing and leaves the task in both notes. The next index pass lists both; a retry of the same call converges (D10).
- **L3** Convergence removes the source copy whenever the destination already holds a byte-identical block, including a legitimate second record of identical done work. No byte is lost, but two identical lines become one.
- **L4** Links and block references to the moved lines (`[[Inbox#^abc]]`) are not rewritten and stop resolving; the response names the block ids moved.
- **L5** Moved lines take the destination's terminator (D6).
- **L6** A block whose non-blank line does not start with the task's exact frame bytes (mixed tabs and spaces, a deeper quote written without the space) is refused rather than re-indented.
- **L7** Blank lines inside a moved block that do not start with the source frame are rewritten as the destination's bare quote prefix (or empty): their whitespace is not preserved when the frames differ.
- **L8** The default-scope note (D16) covers excluded folders only, not excluded note tags.
- **L9** Ordered list markers are kept as written and nothing is renumbered, in either note.
- **L10** Suffixed ids sharing a key with a moved task are renumbered in both notes; an earlier listing's suffixed ids must be listed again.
- **L11** A block containing a heading is refused; move such content with `edit_note`.
- **L12** A partial outcome, or two concurrent moves of one task to different destinations, leaves the task in two notes. There is no delete-task tool; the agent retries (which converges when the source is unchanged) or removes the copy with `edit_note`.
- **L13** Either refresh may lag (#428 L9). Until the next pass a listing may show the task in both notes, or in neither's new state.
- **L14** The change feed shows two `modified` events with `tool = 'move_task'`, not a task-level move.
- **L15** Two paths to one inode are refused, not treated as one note.
- **L16** Verification proves task identity, ownership, code status and headings. It does not prove that the rendered Markdown is identical: a moved ordered item may start a new list, and a moved block after a paragraph may render as part of a loose list.

## Open questions for the owner

Each has a recommendation; the design above is written to the recommendation.

- **Q1. Nested tasks.** (a) Move the subtree, and allow moving a sub-task out of its parent; (b) move the subtree, but refuse a task that is itself nested; (c) refuse any task with sub-tasks. **Recommend (a):** triage moves whole items, and an agent filing a sub-task into another project is a real case; the response names the former parent.
- **Q2. Re-indentation.** (a) Re-frame to the destination's top level by prefix substitution (D4); (b) never re-indent: refuse when the source frame differs from the destination frame. **Recommend (a):** (b) refuses every sub-task move and every move into or out of a callout, and re-framing is byte-exact or it refuses.
- **Q3. The partial outcome's machine-readable facts.** (a) Four optional fields on the `MCP-REFUSAL` line, set only by `task_move_incomplete`; (b) the code alone, with the facts in a fixed prose line. **Recommend (a):** agents are told to act on the sentinel, and the retry needs both paths and both ids.
- **Q4. Convergence.** (a) Automatic, whenever the destination already holds a byte-identical block; (b) only with an explicit `resume=true`. **Recommend (a):** it is what makes "call again with the same arguments" safe without the agent knowing which failure happened, and it never removes a byte that is not also at the destination (L3 is the cost).
- **Q5. Duplicates at the destination.** (a) Refuse `duplicate_task` for the same key with a different block, and for an open task with the same normalised description when the moved task is open, unless `allow_duplicate`; (b) never check. **Recommend (a):** it is `add_task`'s target-note rule, and it is what stops a retry after a changed source from writing a third copy.
- **Q6. Block references.** (a) Move and name the moved `^block` ids in the response; (b) refuse a block that carries one unless `allow_block_ref=true`. **Recommend (a):** a block id on an inbox task is rare, and a refusal would need a new argument; the agent sees the names and can fix the links.
- **Q7. Write-bucket charge.** One per call (recommended, the `move_note` precedent) or two (one per publication).
- **Q8. Terminators.** Destination's terminator (recommended) or the source line's own.
- **Q9. The oracle.** A new `tools_list_full_v3.json` with v2 derived from it (recommended, the #428 precedent, which also proves no other tool changed) or regenerate v2 in place.

## Adversarial scenarios for the implementation review

Each is a scenario the reviewer traces with file-and-line evidence to SAFE or a finding.

1. The source removal fails with an in-call conflict after the destination published, with and without `expected_hash`: the task is in both notes, the response is `task_move_incomplete` with both ids, the destination event is recorded, and a retry converges.
2. Between the two publications another writer completes the task in the source (its text gains `✅`): phase 2 must not remove the completed line; the outcome is `task_move_incomplete` (`source_changed`); a retry refuses `duplicate_task` and never writes a third copy.
3. Between the two publications another writer adds a context line under the task in the source: the block differs, so the source is not removed (the context line would be lost).
4. Between the two publications another writer adds an identical task line elsewhere in the source: the key now resolves to two tasks; phase 2 stops; no guess.
5. A destination publication that raises `OSError` after its rename: the source must not be touched; the outcome is `publication_uncertain`; a retry converges.
6. A vault reassignment between the two publications: the source publication is refused by `_confirmed_publication`, and the outcome is partial, never `nothing_written`.
7. Two opposite moves `A → B` and `B → A` released together, 50 rounds: no deadlock (no wait while holding a lock), no loss; every task ends in exactly one note or in two with a `task_move_incomplete`.
8. Two moves of the same task to two different destinations at once: no loss; one success and one `task_move_incomplete (source_changed)`.
9. Two moves of the same task to the same destination at once: the second converges or is refused; the destination never holds three copies.
10. A sub-task four spaces deep, moved to a destination with no tasks after a paragraph: it lands at indentation 0 and re-extracts as a task (not an indented code block).
11. A task inside a callout (`> - [ ] x` with `>     context`) moved to a plain note, and a plain task moved into a callout's task list: frames substituted, blank lines inside the block, ownership preserved.
12. A block whose child line uses a tab where the task used spaces: refused, nothing written.
13. A block containing a fenced code block that is closed inside the block, and one whose fence is closed after the block: the first moves; the second is refused.
14. A block containing an indented `  # heading`: refused.
15. The moved task is the last line of a source with no final newline; the destination also has no final newline: both files still end without one, and no other terminator changes.
16. A CRLF source moving into an LF destination, and the reverse: the moved lines take the destination's terminator; every other byte of both notes is unchanged.
17. A suffixed id without `expected_hash`: refused before any read. With a matching hash: the right occurrence moves; the remaining identical sibling is renumbered from `.2of2` to bare.
18. A moved task whose text repeats in the destination (`allow_duplicate=true`): both get suffixed ids, and the response names the moved one's.
19. `path` and `to_path` naming the same file through two different paths (a hard link): refused before any publication.
20. Same-note move to a section above the task, to a section below it, and to where it already is: one publication each; the last answers `No changes`.
21. Same-note move where `to_section`'s heading is inside the moved block: refused (headings in a block are refused) before any publication.
22. A recurring, a completed, a cancelled and a custom-status task: moved byte for byte; no rule text is parsed or refused; no done date or next occurrence is written.
23. A task whose text is over 1,000 characters (stored text cut): the key is computed over the uncut text and the move resolves.
24. A source over the 5,000-task cap, and a destination whose result would exceed it: `size_limit`, nothing written in either.
25. A destination whose result would exceed `MAX_NOTE_BYTES`: `size_limit` before the destination publication.
26. A destination with a malformed frontmatter block, or an unmatched indented fence with `to_section`: `content_unsafe`, nothing written in either.
27. A path containing a backtick or a newline: every path printed through `_escape_task_path` or the sentinel's bounded field; no forged response line.
28. A read-only OAuth grant: `move_task` is not listed and a call by name is refused `permission_denied`, spending one write token and no quota beyond the existing rule.
29. Required mode, no hashes, a bare id: proceeds. A wrong `to_expected_hash`: `stale_precondition` naming `to_path`, nothing written anywhere.
30. The refresh of the destination lags (its row locked for 3 s): the move still completes, the source refresh runs, and the response says which lagged.
31. The process is stopped between the two publications (simulated): the next pass indexes both copies, and a retry converges.
32. A move into an excluded folder of the default scope: the scope note appears; the write is unchanged.
33. The usage row never holds task text, for a success, a partial and every refusal.

## Test plan

**Unit (pure), `tests/test_task_move_plan.py`:**

- re-framing (D4) over every frame pair: none ↔ none, none ↔ `> `, indented ↔ none, tabbed, nested quotes; blank-line rules; the refusals;
- placement (D5) as `add_task`'s cases, with the marker kept;
- terminators (D6) and the end-of-file rules for both the removal (D7) and the insertion;
- `verify_move_insert` and `verify_removal`: each check fails on a hand-built bad plan;
- D9: convergence, (a), (b), a done task, `allow_duplicate`;
- `plan_relocated_removal`: unchanged block, changed block, absent key, duplicated key;
- same note (D13): above, below, no change, heading in block;
- ids (D1): keys unchanged, ordinals renumbered on both sides;
- a property test over a corpus of notes: for every task, the two-note move's two new texts together contain every original line of both notes exactly once (moved lines re-framed), and the same-note move is a permutation of lines;
- the existing `tests/test_task_write.py` runs unmodified.

**Wiring (offline), `tests/test_task_move_e2e.py`:** every row of the D10 table, by monkeypatching `write_file_at` / `_confirmed_publication` to conflict, raise or refuse at each publication; the response and sentinel of each outcome; the event and refresh order (destination before source); usage params and result fields; dry run publishes nothing; the D12 precondition matrix.

**Integration (`tests/integration/test_task_move_pg.py`, real Postgres):** list → move → list shows the task in the destination with its id and not in the source, with no pass; both refreshes; two `modified` events attributed to the caller; the opposite-moves race (scenario 7); a held row lock on one note; tenancy (user 8's identically named notes untouched); the next pass after a move leaves `tasks_source_hash = content_hash` on both notes.

**Surface:** every count in D17; the v3 oracle and the v2 derivation; the additions fixture; the read-only fixture unchanged; instructions ≤ 2,000 with the lead unchanged; description ≤ 4,000; `#295` unknown arguments refused on `move_task`; `TOOL_CLASSES` complete; `CLASS_MAPPING_VERSION` 7; the coverage and publishing-tool registries.

## Slice plan (independent agents, one git worktree each)

Pre-flight: this change committed on the base and `openspec validate task-move --strict` green. Every brief opens with the base check: `openspec/changes/task-move/` exists and `src/services/task_rule.py` exists. No migration in any slice.

| Slice | Wave | Owns (only these files) | Depends / stubs |
|---|---|---|---|
| **M1 planner** | 1 | `src/services/task_write.py` (additions only: D3–D9, D13, D19), `tests/test_task_move_plan.py` (new) | none; must leave every existing function's behaviour unchanged |
| **M2 wiring** | 1 | `src/services/refusals.py` (the code and the four fields), `src/mcp_server/tools.py` (`move_task_impl`, the two-phase runner; no change to `_run_task_write` behaviour), `tests/test_task_move_e2e.py` (new), `tests/integration/test_task_move_pg.py` (new), `tests/test_refusals*.py` if the code set is pinned there | stubs `task_write.plan_move*` with the D19 signatures and a `TODO(#430)` until M1 merges; throwaway database on its own port |
| **M3 surface** | 2 (after M1, M2) | `src/mcp_server/server.py` (registration after `add_task`, description, instructions), `src/mcp_server/tool_permissions.py`, `src/services/concurrency.py`, `src/mcp_server/vault_guide_primer.md`, `tests/fixtures/tools_list_full_v3.json` (new), `tests/fixtures/tools_list_schema_additions.json`, every count-pinning and registry test in D17 | none |
| **M4 docs** | 2 | `README.md`, `CLAUDE.md`, `DEPLOYMENT.md`, `docs/deployment-kubernetes.md`, `docs/architecture/vault-tools.md`, `rate-limits.md`, `usage-attribution.md`, `indexing-and-embeddings.md` | this change's text only |

**Seams the supervisor greps after merging** (a production caller of each): `task_write.plan_move`, `plan_move_within`, `plan_relocated_removal` called by `move_task_impl`; `verify_move_insert` and `verify_removal` called by the planners; `task_index.refresh_note_tasks` called twice per two-note move; `refusals` code `task_move_incomplete` produced by `move_task_impl`; `move_task` in `WRITE_REQUIRED_TOOLS`, `TOOL_CLASSES` and the write bucket.

Then the full offline suite, the task and move integration modules, and `openspec validate --strict` once on the merged result.
