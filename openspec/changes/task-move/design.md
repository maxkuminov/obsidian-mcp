
## Context

**Scope (owner decision, Max, 2026-11-16: Q0).** This change builds the **smaller first version**: the moved task is a top-level, unquoted task, and its block holds no blockquote, fence or heading line (D3). The moved lines therefore arrive byte for byte, with no re-framing. The wider design (nested tasks, callouts, prefix re-framing) is kept at the end of this file as the documented next step ("Full design: the documented next step"); none of it is a requirement or a task of this change.

**What the code does today** (verified on `main` at fa27234, after #443, #444 and #448):

- **The task write runner.** `_run_task_write` (`src/mcp_server/tools.py`) is the #428 D2 sequence for one note: `_precondition_syntax_error`, `open_mutable`, `_leaf_state_error`, then per attempt `read_bytes_at` under `MAX_NOTE_BYTES`, the `_precondition_error` ladder (`enforceable=False`: the `(path, id)` pair satisfies required mode), UTF-8 decode, a pure planner off the loop, `_note_size_error`, `dry_run`, `_confirmed_publication(write_file_at(…, expected=existing_bytes))`, the write-time refresh (`task_index.refresh_note_tasks`), and one `modified` event. An in-call conflict without `expected_hash` repeats from the read, at most `TASK_WRITE_MAX_ATTEMPTS = 3`. An `OSError` from the publication is answered `io_failure`; a `VaultFSError`, `ValueError` or a non-conflict `RuntimeError` is `validation_failed`.
- **Published file permissions (#447, merged in #448).** Every publication through `vault._atomic_write_at` applies one permission plan to the staged inode before its single `fsync`. An **edit** (the publication replaces an existing regular file; every `write_file_at(…, expected=)` of the task tools) keeps that file's permission bits, POSIX access ACL and group; a **create** follows the folder. On the overwrite branch the leaf is captured twice, **P1** before the apply and **P2** immediately before the `expected=` byte comparison; any P1/P2 difference (identity, gid, mode bits, ACL bytes, a regular file appearing or vanishing), and any difference found by the new staged-bytes check of the named `.tmp-*` copy, publishes nothing and raises the existing in-call conflict `File changed while editing: <name>`. A failure to capture or apply the plan raises `vault_fs.PermissionsNotApplied` **before anything is published**: an `OSError`, not a `VaultFSError`, so the tools answer it `io_failure` with text saying nothing was written. The compare-to-rename window is unchanged (`openspec/specs/file-permissions/spec.md`; `docs/architecture/vault-tools.md`, "Published file permissions (#447)").
- **Generic writers refresh too (#432, merged in #444).** `edit_note`, `set_frontmatter` and `write_file` to a note path now call the same `refresh_note_tasks` after publication **and after their event**, gated by `tasks.may_hold_tasks`, silently; `delete_note` clears the deleted note's rows. The three task write tools keep their order, publication → refresh → event, and their visible `index:` line (`task-write` spec, "A generic writer's task refresh…").
- **Sync-conflict copies (#392, merged in #443).** With `INDEX_EXCLUDE_CONFLICT_FILES` on, a note whose basename matches a conflict pattern is never indexed; the path-addressed tools, the task writes included, still work on it, and a task write's refresh there reports `index: may lag (not_indexed)`. Only `create_note` and `move_note` print the exclusion notice line.
- **The planners** (`src/services/task_write.py`) return a `Plan` of `Span`s over the original text, never a regenerated note, and every plan passes `verify` (#428 D5). `verify` and `_line_map` accept only two span kinds: a line-confined edit of one declared target line, and a whole-line insertion at a line boundary. **There is no line deletion anywhere in the task tools today.**
- **The continuation block** (`continuation_block`, `_owners`, `_shape`) is uncapped: every following line with a greater blockquote depth, or the same depth and a greater indentation width (tab = 4), crossing blank lines; trailing blank lines are not part of it.
- **`add_task` placement** (`plan_add`, `_region`): region = the `section` body or the whole post-frontmatter body; anchor = the last task line in the region whose indentation width after its blockquote prefix is 0; the new line goes after the anchor's continuation block, copying the anchor's blockquote prefix and bullet; with no anchor, after the region's last non-blank line; in an empty region, after the heading or at body start. The target-note duplicate check refuses an open task with the same normalised description.
- **`move_note`** is the only tool that publishes more than once. A failure after its first publication (the rename) is a **partial success**, rendered with `disposition="partial"` and never `nothing_written: true` (`BodyOutcome` refuses that combination at construction). Each publication takes its own `_confirmed_publication`.
- **The refresh** takes exactly one `notes_metadata` row lock, with a 2-second `lock_timeout`, in its own transaction, and never the generation lock. The change-feed append takes the scope's advisory lock last, in its own short transaction.
- **The `MCP-REFUSAL` payload** (`refusals.Refusal.as_payload`) renders `code` and the optional `path`, `current_hash`, `cap_name`, `cap_bytes`, `nothing_written` (absent, never null).
- **Counts on `main` at fa27234** (unchanged by #443, #444, #448): 32 tools; 19 read-only; write bucket 11; `WRITE_REQUIRED_TOOLS` 12; a read-only grant lists 20; `CLASS_MAPPING_VERSION` 6; `SERVER_INSTRUCTIONS` 1,812 characters with a 505-character lead.

## Goals / Non-Goals

**Goals:**

1. A move never loses a byte of the task or of either note. A duplicate is acceptable; a loss is not.
2. Every intermediate state is named to the caller with only the facts the tool verified, and every partial outcome names the exact call that completes the move. Nothing is ever inferred from content alone: an identical block at the destination is not taken as proof of an earlier move unless the caller says so (`resume=true`).
3. The moved lines arrive byte for byte (only their terminators follow the destination, D6); every other line of both notes is untouched; a task or block outside this version's scope is refused, never guessed at.
4. The same envelope as the other task writes: resolution from the current bytes, verification before every publication, read-your-writes, attributed events, typed outcomes.

**Non-goals:** see `proposal.md`.

## Decisions

### D1. Arguments, and what the id is after the move

```
move_task(path, id, to_path, to_section=None, allow_duplicate=False,
          resume=False, dry_run=False, expected_hash=None, to_expected_hash=None)
```

| Argument | Meaning |
|---|---|
| `path`, `id` | The source note and the task, exactly as `complete_task` / `update_task` take them (#428 D1, D2). |
| `to_path` | The destination note. It must exist; `move_task` never creates a note (`not_found`, as `add_task`). It may equal `path`. |
| `to_section` | A heading in the destination, addressed as `add_task(section=…)` addresses one, with the same refusals (`selector_unresolved`, the frontmatter-defect and unmatched-fence `content_unsafe`). Omitted: the whole body. |
| `allow_duplicate` | Insert even when the destination already holds a duplicate (D9), including a byte-identical copy. It only ever adds lines. |
| `resume` | Complete an earlier partial move: the destination already holds this exact block, so insert nothing and remove the source copy. Refused unless exactly one byte-identical block is found at the destination (D9). |
| `dry_run` | Plan, verify and return both diffs; publish nothing. |
| `expected_hash` | Binds the **source** note's whole bytes, through the existing ladder. |
| `to_expected_hash` | Binds the **destination** note's whole bytes, through the same ladder. |

Argument checks (`invalid_argument` unless stated), all before any read: the id grammar; a suffixed id without `expected_hash` → `precondition_required` (#428 D2, every mode); `path` and `to_path` capped at `MAX_PATH_CHARS`, `id` at `TASK_ID_ARG_MAX_CHARS`; the syntax of both hashes (`malformed_precondition`, naming which argument); `resume=true` together with `allow_duplicate=true` (the two would mean "insert a copy" and "insert nothing" at once); `resume=true` together with `to_section` (a resume inserts nothing, so a placement is meaningless and probably a mistaken call).

**The id after the move.** The key is `sha256("omcp-task-v1\0" + uncut text)[:12]`, and the text excludes the status, indentation, list marker and blockquote prefix (#428 D1). The moved lines' content is unchanged (D4), so **every moved task keeps its key**: the moved task and every sub-task in its block.

- The rendered id is bare when the destination holds exactly one task with that key after the move, and `<key>.<i>of<n>` otherwise (only possible with `allow_duplicate=true`, or for a sub-task whose text repeats in the destination).
- Ordinal suffixes are renumbered by document order **on both sides**: a source task that shared a key with a moved task, and a destination task that shares a key with one, may change its `.<i>of<n>`. The response names the moved tasks' new ids and says, when it happened, that suffixed ids sharing a moved key were renumbered in either note (D18). An id from an earlier listing of either note that carried a suffix must be listed again; a bare id stays valid wherever it still resolves.

### D2. Resolution in the source

Exactly #428 D2 steps 1–9, over the source note's bytes read through its anchored descriptor: `task_not_found`, `task_ambiguous`, `task_changed`, `size_limit` over the 5,000-task cap, `content_unsafe` on a defective frontmatter block, `task_malformed` on NUL. No line number from the index or a listing is used. The id names any checkbox, whatever its status (open, done, cancelled, custom) and whatever the caller's task rule (#434 D7: the rule never blocks a write).

### D3. The moved block, and what is refused

The **moved block** is the task line `t` plus its continuation block (#428 D8), uncapped: lines `t … e−1` where `e = continuation_block(lines, t)`. Sub-tasks, context lines, blank lines crossed inside the block, and deeper-indented fences all belong to it. The 8-line / 600-character context cap of the index never applies to a write.

**A task moves with its subtree.** Moving a task that has sub-tasks moves them all. Moving the line alone is never done: its children would silently change owner.

**This version's scope (owner decision, Max, 2026-11-16: Q0).** The moved task must be a **top-level, unquoted** task: indentation width 0 and no blockquote prefix (so not inside a callout). Its continuation block may hold plain indented lines, blank lines and indented sub-tasks, and nothing else. Refused with `task_malformed`, nothing written in either note, naming the line and the reason, each refusal adding that `edit_note` can make the move by hand:

- **a nested task**: the named task lies inside another task's continuation block (its indentation width or its blockquote depth is greater than 0);
- **a quoted task**: the task line carries a blockquote prefix (a callout task);
- **a blockquote line inside the block**: any line of the block that starts, after its indentation, with `>`;
- **a fence line inside the block**: any line of the block that opens or closes a fenced code block (after its indentation, three or more backticks or tildes), whether the fence closes inside the block or not. This line scan replaces the full design's cross-note mask comparison for the refusal; the D8 verifiers still compare the masks;
- **a heading inside the block**: an ATX heading line, matched on the line's code-masked text after its indentation (`#{1,6}` then whitespace or the line's end; the vault's heading scan recognises only an unindented `#`, and every block line is indented, so that scan alone could never fire here — implementation deviation, recorded), which moving between notes would change both notes' section structure and every `#N` section ordinal after it;
- a NUL in either note (as #428).

A **quoted destination anchor** is refused the same way, at placement (D5).

### D4. The moved lines arrive byte for byte

In this version the moved task and the destination anchor are both unquoted and at indentation 0 (D3, D5), so the task's frame (its text before the list marker) and the destination's are both empty, and **no re-framing exists**: the content of every moved line, without its terminator, is inserted exactly as it was read. Only the terminators follow the destination (D6).

- Relative structure is preserved trivially: a child indented four spaces under the task is four spaces under it at the destination, and the list marker of every line, the moved task's own included, is kept as written (an ordered `3.` stays `3.`; nothing is renumbered, L9).
- No field, tag, status, rule text, `^block` link or trailing whitespace changes.
- The prefix re-framing that would let a nested or quoted task move (the full design's D4) is the documented next step, not this change.

### D5. Placement at the destination

Exactly `add_task`'s rule (#428 D8), applied to the destination note after any same-note removal (D13):

1. **Region** `R`: the `to_section` body, or the whole post-frontmatter body.
2. **Anchor**: the last task line in `R` whose indentation width after its blockquote prefix is 0. A task that is part of the moved block is never the anchor. **An anchor carrying a blockquote prefix (a callout's task list) is refused** `task_malformed`, naming the anchor line, nothing written in either note (Q0: placing an unquoted block after a quoted anchor would need the full design's re-framing).
3. **Insertion point**: directly after the anchor's full continuation block; with no anchor, after `R`'s last non-blank line; with `R` empty, directly after the section's heading line, or at body start.
4. **Marker**: unlike `add_task`, which copies the anchor's blockquote prefix and bullet, the moved task keeps **its own** list marker and gets no prefix.
5. **Planned owner** of the moved task line: none (a top-level task, as the anchor is). Every other moved line's planned owner is the image of its source owner inside the block.

### D6. Terminators

The moved lines' **content** is byte-exact under D4. Their **terminators** are the destination's: every moved line gets `T` = the terminator of the line it is inserted after, else the note's first terminator, else LF (the #428 D7 rule), so a CRLF note never receives LF lines or the reverse. Two boundary cases, exactly as #428's `_insertion` behaves:

- **Insertion at the end of a file with no final terminator.** `T` is prepended to the inserted lines instead of appended, so the pre-existing last line **gains** the terminator `T` and the last moved line has none: `Intro` (no newline) receiving `- [ ] x` becomes `Intro\n- [ ] x` (no newline). This is the one pre-existing line whose terminator changes at the destination.
- **Insertion into an empty body** (an empty file, or a frontmatter block that ends the file with no terminator; after frontmatter that ends with a terminator, the lines are inserted normally, each with `T`): the moved lines are joined by `T` with no final terminator: an empty file becomes `- [ ] x` (no newline). Rejected: keeping the source terminators, which would make a CRLF destination mixed (owner decision, Max, 2026-11-16: Q8).

### D7. The removal span at the source

The removal deletes lines `t … e−1` with their terminators, as one span `[start of line t, start of line e)`. One exception keeps the file's final-terminator state: when line `e−1` is the last line and has no terminator, the span starts at the end of line `t−1`'s content instead (it takes the preceding terminator), so the file still ends without one. That line `t−1` is then the one pre-existing source line whose terminator changes: it **loses** it. `Intro\n- [ ] x` (no final newline) becomes `Intro` (no newline); `Intro\n- [ ] x\n` becomes `Intro\n` (no exception: every remaining line keeps its terminator); `- [ ] x` alone becomes the empty file. When `t` is the first body line and the block runs to the end of the file, the body becomes empty and the frontmatter block's content is untouched; if the file had no final terminator, the frontmatter's closing line loses its terminator under the end-of-file rule above (implementation deviation, recorded). Blank lines before `t` and after `e−1` are untouched (two blank lines may then be adjacent; nothing is collapsed).

### D8. Verification before each publication

Each plan is verified by a pure function that re-derives everything from the old and new texts, in the #428 D5 style, before any dry-run answer and before publication. The existing `verify` is not changed; `verify_removal` and `verify_move_insert` (or one generalised function that leaves `verify`'s existing behaviour byte-identical) are added. A failure is `task_malformed`, with nothing written.

**Destination (`verify_move_insert(old_d, new_d, plan)`):**

1. `new_d` is `old_d` with exactly one whole-line insertion at a line boundary; every pre-existing line keeps its content, and every pre-existing line keeps its terminator **except** in the D6 end-of-file case, where the old last line, unterminated, gains exactly `T` and nothing else changes.
2. The inserted lines equal the moved block line for line: content byte-identical (D4), terminator `T` (D6).
3. Every pre-existing task re-extracts identically (`_COMPARED` fields), shifted by the inserted count.
4. The new task set is the old set plus exactly the moved tasks, and **each moved task re-extracts with the same full text, key, status character, state, every date field, priority, tags, recurrence and `text_cut` as at the source**. Tags are therefore conserved by construction.
5. No pre-existing line changes its owning task; each moved line is owned as planned (D5).
6. The code mask of every pre-existing line is unchanged; each moved line's mask equals its source mask (its code status is the same); the headings and the frontmatter block are unchanged.

**Source (`verify_removal(old_s, new_s, plan)`):**

1. `new_s` is `old_s` with exactly the D7 span removed; every remaining line keeps its content, and every remaining line keeps its terminator **except** in the D7 end-of-file case, where line `t−1` loses exactly its terminator and becomes the unterminated last line.
2. Every remaining task re-extracts identically, shifted by the removed count.
3. The new task set is the old set minus exactly the moved tasks.
4. No remaining line changes its owning task.
5. The code mask of every remaining line, the headings and the frontmatter block are unchanged.

**Block ids (Codex spec r1 #7; owner decision, Max, 2026-11-16: Q6).** Before publication, every `^block-id` the moved lines carry (a ` ^id` suffix at the end of a line, read as the plugin reads a task's block link, on the unmasked text) is compared, case-insensitively, with every block id already present in the destination's body. A collision is refused `block_id_conflict`, naming the id and the destination line, with nothing written in either note: two blocks with one id make `[[note#^id]]` ambiguous. A same-note move cannot create a collision and does not run the check.

A plan whose result exceeds `MAX_NOTE_BYTES` is `size_limit` (`_note_size_error`), and one whose extraction would exceed the 5,000-task cap is `size_limit`, in either note, before anything is published.

### D9. Duplicates at the destination, and the explicit `resume`

(Owner decision, Max, 2026-11-16: Q4 explicit `resume=true`; Q5 `duplicate_task` unless `allow_duplicate=true`.) Both are decided from the **destination's current bytes**, never from the index. A task id names text within one note, not a task across notes, so **an identical block at the destination is never taken as proof of an earlier partial move** (Codex spec r1 #2): two notes may legitimately hold independent identical tasks.

**Duplicates (refused `duplicate_task` unless `allow_duplicate=true`, nothing written in either note)**, naming up to 5 matches as `path:line id`:

- (a) a task in the destination with the moved task's key, **whatever its block**, a byte-identical block included;
- (b) when the moved task is open: an open destination task with the same normalised description (#428 D9, `add_task`'s target-note rule, rule-independent as #434 Q1).

When a match in (a) is byte-identical to the moved block (every line's content equal, terminators excluded, since the moved lines take the destination's terminators), the refusal says so and names both ways on: `allow_duplicate=true` to keep both copies, or, if this is the copy an earlier `move_task` wrote, `resume=true` to remove the source copy. A done or cancelled moved task is checked by (a) only: an archive note legitimately holds many done tasks with one description. The index-side scan of other notes and `add_task`'s similar-task list are not run: a move creates no new task.

`allow_duplicate=true` inserts a further copy. It only ever adds lines, so it can never cause a loss.

**`resume=true`** has exactly one meaning: "the destination already holds this exact block; remove the source copy and insert nothing".

- It requires **exactly one** task in the destination with the moved key whose block is byte-identical to the moved block (line contents, terminators excluded). Zero or more than one is refused `resume_unmatched` (the prose says which, and names the candidates), writing nothing.
- It then runs phase 2 only (D10 steps 6–8), with the destination presence check aimed at that copy's position.
- It cannot be combined with `allow_duplicate` or `to_section` (D1), and a same-note `resume` is refused (D12).
- Its only removal is of source lines whose content is present at the destination at the moment of the check. If the destination copy was in fact an independent task, the caller asserted otherwise, and the cost is that one of two identical records is gone while its bytes remain (L3).

**Same note** (D13): the moved task is excluded from both checks against its own note.
### D10. Publication order and the state after every failure

**Order:**

1. Read both notes; resolve; run the D9 and block-id checks; plan and verify both notes (D8).
2. `dry_run`: answer, publish nothing.
3. Publish the destination insert (`expected=` the destination bytes read in step 1).
4. Refresh the destination's task rows.
5. Record the destination's `modified` event.
6. **Publish the source removal**, inside one confirmed publication whose synchronous callback does, with no `await` between them:
   - (a) the root confirmation (`_confirmed_publication`);
   - (b) **the destination presence check (Codex spec r1 #1):** re-read the destination through its anchored target and require that the lines at the inserted position (lines `k` … `k+n−1`, where step 3 put them, or where `resume` found the copy) have exactly the contents of the inserted lines, byte for byte. Since implementation review r2 the check also re-extracts the destination with the pass's extractor (`tasks.tasks_from_note_text`, code masking included) and requires the moved task and every moved sub-task at their offsets with their keys, the block still the task's continuation block and each line's code mask unchanged (`task_write.destination_still_holds_task`): an unclosed fence opened above the lines keeps the bytes but hides the task. That state is its own result, **not a task** (row 8b, implementation review r3): the lines are there but do not read as the task, so the source is not written. The check has three results, and only the first lets the source be written: **holds** (the read succeeded and the lines match); **changed** (the read succeeded, or found no file at the name, and the lines are absent or different at that position: an outside editor saved an older destination, a line was inserted above, the note was deleted), row 8; **unverified** (the read itself failed: a permission or I/O error, a non-regular file, a file over the read cap; or the bytes read are not valid UTF-8, implementation review r1), row 8a, where the tool knows nothing about the destination's contents and says so (Codex spec r2 #1);
   - (c) the source write, `expected=` the source bytes the removal was planned against.
7. Refresh the source's task rows.
8. Record the source's `modified` event.

**File permissions (#447).** Both publications are **edits** in #447's sense: `to_path` must exist (D1) and the source is the note being read, and both go through `write_file_at(…, expected=<the bytes read>)`, the overwrite branch. So each publication applies the `file-permissions` edit rule to its own note (Codex spec r2 #3): the replaced file's permission bits, POSIX access ACL and group, with that rule's group translation (a named group entry, `group::---`) when the server cannot set the note's group, and its mode-only behaviour on a filesystem without POSIX ACLs. The moved lines take the destination file's permissions, and the source keeps the source's. Neither publication ever takes the create rule: a destination that vanished, or stopped being a regular file, after step 1 fails P2 or the `expected=` comparison as the in-call conflict, so `move_task` cannot create a note through a race either. The #447 failure modes map onto this table as follows:

- **A P1/P2 difference, or a staged-bytes difference, on either write** raises `File changed while editing: <name>`, the in-call conflict, and publishes nothing. It is handled exactly as a byte conflict on that note: at the destination, row 4 (repeat from step 1 without `to_expected_hash`, else `concurrent_write`, nothing written); at the source, rows 9–12 (re-locate and retry without `expected_hash`, else `task_move_incomplete` `concurrent_write`). A permission-only change (a `chmod`, `chgrp` or ACL edit with the bytes unchanged) therefore costs a retry, or, with a hash supplied, a refusal or a partial outcome whose completing call, made with the same hash, succeeds because the bytes still match.
- **`vault_fs.PermissionsNotApplied` on the destination write** (step 3) is raised before anything is published, so it is **not** row 6's uncertainty: nothing is written in either note, and the answer is the existing `io_failure` refusal whose text names the permission step and says nothing was written (row 6a). It is told apart from other `OSError`s by type, never by text.
- **`vault_fs.PermissionsNotApplied` on the source write** (step 6(c)) leaves the source verifiably unpublished while the destination holds the task: `task_move_incomplete` with `reason` `permissions_not_applied` (row 14a), completed by `resume=true` once the permission problem is fixed. It is not retried: retrying cannot fix the cause, and `resume=true` later repeats the source write in full.

The destination is published first because a failure after it leaves a duplicate, and a failure after a source-first removal would leave a loss. **The source is never written unless step 3 returned success (or `resume` found the copy) and the step 6(b) check passed in the same synchronous step as the write.** A destination refresh that reports `superseded` (the destination changed after step 3) does not by itself stop the move, but it can never be followed by a source removal unless the check in 6(b), run later, finds the moved lines still in place.

In the table, `P` and `I` are the source path and id, `D` and `J` the destination path and the moved task's id there. Every partial outcome carries a `complete with:` line naming the exact call that completes the move (D11). **Every completing call omits `to_expected_hash` or passes a fresh destination `content_hash`** (Codex spec r3 #3): the destination publication changed the destination, so the original call's hash is stale, and the `complete with:` line says so whenever one was supplied. **No completing call repeats the original call's `expected_hash`** (implementation review r1): a concurrent source edit makes it stale, so a call naming it would be refused `stale_precondition`. With a bare id the completing call omits `expected_hash` (a bare id satisfies required mode); with a suffixed id the `complete with:` line starts `read_note(path=P) for the source's content_hash, then` and names `expected_hash=<the content_hash that read_note returns>`. Every completing call is rendered by one helper (`tools._completing_call`, implementation review r2), which decides the hash from the id it names: none for a concrete bare id; `expected_hash=<the content_hash that read_note(path=P) returns>` for a concrete suffixed id or the `<key>.<i>of<n>` placeholder; and, for an id the caller takes from a current listing (`<its new id>`, `<its current id>`), the instruction to add that hash if the id carries a suffix. The calls in the table below are written for a bare id.

| # | Step | What happens | Destination | Source | Caller receives | `complete with:` |
|---|---|---|---|---|---|---|
| 1 | arguments, hash syntax | refused | unchanged | unchanged | the refusal (`nothing_written: true` where its code carries it, as for the other task tools) | (not partial) |
| 2 | step 1, **either** note | refused (incl. `duplicate_task`, `resume_unmatched`, `block_id_conflict`) | unchanged | unchanged | that refusal; a precondition refusal names its note | (not partial) |
| 3 | `dry_run` | | unchanged | unchanged | both diffs, the moved lines, the new ids | |
| 4 | step 3: in-call conflict | without `to_expected_hash`: repeat from step 1, re-reading **both** notes, up to 3 attempts; then, or with it, refuse | unchanged | unchanged | `concurrent_write`, `nothing_written: true` | (not partial; the same call again) |
| 5 | step 3: vault reassigned, anchor lost | refused by `_confirmed_publication` | unchanged | unchanged | the existing marker refusal | (not partial) |
| 6 | step 3: OS error other than `PermissionsNotApplied` | the rename may or may not have happened | **uncertain** | unchanged | `publication_uncertain`, partial | `read_note(D)`; if it holds the lines, `move_task(path=P, id=I, to_path=D, resume=true)`, otherwise `move_task(path=P, id=I, to_path=D)` |
| 6a | step 3: `PermissionsNotApplied` (#447) | refused before anything is published | unchanged | unchanged | `io_failure` ("…permissions could not be applied…; nothing was written") | (not partial; the same call once the permission problem is fixed) |
| 7 | step 4: refresh not `refreshed` | continue (owner decision, Max, 2026-11-16: Q10); the response says `index (destination): may lag (<code>)` | holds the task | removed at step 6 | success with the lag line | |
| 8 | step 6(b) reads the destination and finds it changed | source not written | **verified**: no longer holds the moved lines at line `k` | not changed by this call | `task_move_incomplete` (`destination_changed`) | `move_task(path=P, id=I, to_path=D)`; if that is refused `duplicate_task` because the copy is still elsewhere in `D`, `move_task(path=P, id=I, to_path=D, resume=true)` |
| 8b | step 6(b) finds the moved bytes at line `k`, but they no longer extract as the moved task (an unclosed fence above them, for example) | source not written | **verified** at the check: held the moved lines at line `k`, not readable as a task | not changed by this call (its current state is not verified) | `task_move_incomplete` (`destination_not_a_task`) | repair `D` so the lines read as a task again (close the fence), then `move_task(path=P, id=I, to_path=D, resume=true)`; or, after `read_note(P)` confirms the source still holds the task, remove the copy in `D` by hand with `edit_note` |
| 8a | step 6(b) cannot read the destination, or reads bytes that are not valid UTF-8 (`destination_unverified`, never `destination_changed`) | source not written | **unverified**: the tool could not read it, and says nothing about its contents | not changed by this call | `task_move_incomplete` (`destination_unverified`) | once `D` is readable, `read_note(D)`: if it holds exactly one byte-identical copy, `move_task(path=P, id=I, to_path=D, resume=true)`; otherwise `move_task(path=P, id=I, to_path=D)` (a `duplicate_task` refusal then names any copy elsewhere in `D`) |
| 9 | step 6(c): in-call conflict (a byte change, or since #447 a P1/P2 permission difference or a staged-bytes difference), retries exhausted or `expected_hash` given | stop | holds the task (verified in 6(b)) | not removed by this call; the other writer's change is unknown to the tool | `task_move_incomplete` (`concurrent_write`) | `read_note(P)`, then branch (Codex spec r2 #2): if `I` still resolves to a block byte-identical to the moved one, `move_task(path=P, id=I, to_path=D, resume=true)`, with that read's `content_hash` as `expected_hash` when the id is suffixed; if the key resolves once with a different block, row 11's call (`allow_duplicate=true` with the current id); if it is absent, row 10's |
| 10 | step 6 retry: re-read finds no task with the key of `I`, or finds no source file at all | stop | holds the task | **verified**: no task with id `I` | `task_move_incomplete` (`source_missing`) | nothing, if it was removed or moved elsewhere; if its text was edited, `list_tasks(path=P)` gives its new id and `move_task(path=P, id=<new id>, to_path=D, allow_duplicate=true)` moves the edited copy beside the earlier one |
| 11 | step 6 retry: the key resolves once, its block differs | stop (removing it would lose the edit) | holds the task | **verified**: holds an edited version, id `I'` | `task_move_incomplete` (`source_changed`) | `move_task(path=P, id=I', to_path=D, allow_duplicate=true)`: both versions then sit in `D`, nothing is lost |
| 12 | step 6 retry: the key resolves to several tasks | stop (no guess) | holds the task | **verified**: several identical tasks | `task_move_incomplete` (`source_ambiguous`) | `read_note(P)`, then `move_task(path=P, id=<key>.<i>of<n>, to_path=D, resume=true, expected_hash=<content_hash>)` naming the copy to remove |
| 13 | step 6(a): reassigned, anchor lost, confirmation outage | stop | holds the task | not removed | `task_move_incomplete` (that marker as `reason`) | `move_task(path=P, id=I, to_path=D, resume=true)` once the assignment is restored |
| 14 | step 6(c): OS error other than `PermissionsNotApplied` | the removal may or may not have happened | holds the task | **unknown** | `task_move_incomplete` (`io_failure`) | `read_note(P)`; if it still holds the task, `move_task(path=P, id=I, to_path=D, resume=true)`; otherwise the move is complete |
| 14a | step 6(c): `PermissionsNotApplied` (#447) | refused before anything is published; not retried | holds the task (verified in 6(b)) | **verified**: not changed by this call | `task_move_incomplete` (`permissions_not_applied`) | `move_task(path=P, id=I, to_path=D, resume=true)` once the permission problem is fixed |
| 15 | step 7: refresh not `refreshed` | the move stands | holds the task | removed | success, `index (source): may lag (<code>)` | |
| 16 | process killed between steps 3 and 6 | no response | holds the task | unchanged | nothing (L2) | the agent lists both notes; if both hold it, `move_task(path=P, id=I, to_path=D, resume=true)` |
| 17 | an outside writer replaces the destination with a version lacking the moved lines **after 6(b) and before 6(c)'s rename completes**, or at any time after the move | the source copy is removed | **lacks the task** | removed | success | **loss** (L17): recoverable only from the destination's outside history or from this call's response |
| 18 | everything succeeds | | holds the task | removed | success (D18) | |

**Re-locating the block for a source retry (rows 9–12).** Nothing from the first read is trusted. The source is re-read through the same anchored descriptor; the tasks are re-derived; the moved key must resolve to exactly one task (the original id was bare, since a suffixed id carries `expected_hash` and never retries), and that task's block must be byte-identical to the moved block as read in step 1. Only then is a new removal planned, verified (D8) and published, again through step 6 with its presence check. If the task was edited after the destination copy was written, the source copy is not removed: removing it would lose the edit.

**Nothing grows past two copies without the caller asking**, for one call and its completions, or for two concurrent calls once one of their source removals succeeds. A plain retry of a partial move is refused `duplicate_task` (D9 (a)) and writes nothing. Only `allow_duplicate=true` adds a copy. Only `resume=true` removes the source copy, and only when exactly one identical copy is at the destination at the moment of the check. **The bound is per call, not global** (Codex spec r2 #6): N concurrent moves of one task to N different destinations can each publish their destination before one source removal wins, leaving one copy in each destination that published (N copies), never zero; the losers each answer `source_missing` (L12). And when **every** source removal stops partial (Codex spec r3 #1; for example two calls whose source writes are both refused `permissions_not_applied`), the source copy remains as well: N + 1 copies, three for two calls.

**Rejected (owner decision, Max, 2026-11-16: Q10):** stopping before step 6 whenever the destination refresh does not report `refreshed` (a `destination_index_lagged` partial outcome). It would make the list → move → list guarantee unconditional, at the cost of turning an index hiccup into a duplicate in the vault that the caller must clean up.
### D11. The partial outcome, `task_move_incomplete`

A new code in `refusals.BODY_CODES` (not `TASK_CODES`, which carry `nothing_written: true`). It is rendered with `disposition="partial"`, never `nothing_written`. Its prose states **only what the tool verified** (Codex spec r1 #4), in a fixed shape:

```
move_task: the move of task <I> from `<P>` to `<D>` did not complete (<reason>).
destination: <what the tool verified about D>
source: <what the tool verified about P>
complete with: <the exact call, from the D10 table>
destination content_hash: sha256:…
index (destination): refreshed
MCP-REFUSAL {"code":"task_move_incomplete","path":"<P>","destination_path":"<D>","source_id":"<I>","destination_id":"<J>","reason":"<reason>"}
```

| `reason` | `destination:` line | `source:` line |
|---|---|---|
| `concurrent_write` | `holds the task at line N, id J (re-checked before the source write)` | `another writer changed it during this call; this call did not remove the task; re-read it to choose the completing call` |
| `source_missing` (also when the re-read finds no source file) | `holds the task at line N, id J (re-checked before the source write)` | `a re-read during this call found no task with id I; this call did not change it` |
| `source_changed` | `holds the task at line N, id J (re-checked before the source write)` | `a re-read during this call found the task's text once with a different block, id I'; this call did not remove it, so that version is kept` |
| `source_ambiguous` | `holds the task at line N, id J (re-checked before the source write)` | `a re-read during this call found several tasks with this text; this call removed none` |
| `destination_changed` | `did not hold the moved lines at line N when re-read before the source write (the note changed, or was gone, after this call wrote it)` | `this call did not change it` |
| `destination_not_a_task` | `held the moved lines at line N when re-read before the source write, but they no longer read as a task there (for example, they are inside an unclosed code fence)` | `this call did not change it` |
| `destination_unverified` (also bytes that are not valid UTF-8) | `could not be read to check that it still holds the moved lines (<error>); its contents are unverified` | `this call did not change it` |
| `vault_assignment_changed`, `vault_anchor_lost_at_publish`, `vault_confirmation_unavailable` | `this call wrote the task at line N, id J; it was not re-checked after that` (refused before the presence check) | `this call did not change it` |
| `permissions_not_applied` (#447) | `holds the task at line N, id J (re-checked before the source write)` | `this call did not change it: its file permissions could not be applied (<step>: <errno name>), so nothing was published` |
| `io_failure` | `holds the task at line N, id J (re-checked before the source write)` | `unknown: the removal failed part-way; read the note` |

For a `resume=true` call, which wrote nothing to the destination, `holds` reads `held … (re-checked before the source write)` and the confirmation rows read `held the task at line N, id J when this call read it`. **Every sentence states only what this call verified** (implementation review r4): no line says what a note holds now unless this call read it, and "the source still holds the task" is never stated; a completing call that depends on it says to read the source first.

- A partial outcome **never** says the source holds the task unless a re-read in this call found it there.
- **Four optional `Refusal` fields** (`destination_path`, `source_id`, `destination_id`, `reason`), rendered absent-not-null like the precondition fields, and set only by this code (a `Refusal` construction check enforces that). `destination_id` is absent for `destination_changed`, `destination_not_a_task` and `destination_unverified`. Paths are bounded as every path in the sentinel is; ids are server-derived hex. Rejected: carrying them in the prose only (agents are told to act on the sentinel), and reusing `partial_completion` (indistinguishable from `move_note`'s). Owner decision, Max, 2026-11-16: Q3, the four fields.
- `reason` takes exactly the values of the table above. `permissions_not_applied` was added when this change was reconciled with #448 (#447): before it, every `OSError` from the source write was the `io_failure` row, which says the source state is unknown; #447's refusal is raised before anything is published, so the source state is known and the completing call is `resume=true` without a read first.
- A `resume=true` call that stops in phase 2 uses the same shape. A same-note move never produces it.

**Two new task refusal codes** (`refusals.TASK_CODES`, nothing written): `resume_unmatched` (D9) and `block_id_conflict` (D8).
### D12. Preconditions, required mode and retries, per note

| | Source (`path`) | Destination (`to_path`) |
|---|---|---|
| hash argument | `expected_hash` | `to_expected_hash` |
| compared | immediately after each read, by the existing ladder | the same |
| required mode (`WRITE_PRECONDITION_REQUIRED=true`) | a bare id satisfies it (#428 D3: `(path, id)` binds the exact bytes removed); a suffixed id always needs `expected_hash` | exempt: an insertion removes no byte (#428 D3, as `add_task`) |
| in-call conflict, hash absent | phase 2 re-locates and retries (rows 9–11), at most 3 attempts | phase 1 repeats from the read (row 4), at most 3 attempts |
| in-call conflict, hash present | no retry: phase 2 stops partial | no retry: row 4 refuses |
| `stale_precondition` | names `path` and its `current_hash` | names `to_path` and its `current_hash` |

**Same note:** `to_expected_hash` must be absent or equal to `expected_hash` (`invalid_argument` otherwise); one ladder runs; `resume=true` is `invalid_argument` (a same-note move has no partial outcome to resume).

**`resume=true`:** both ladders run as for a move; the destination is read and checked but never published, so `to_expected_hash` only binds the check.

A supplied `expected_hash` stays bound across both phases: the removal is published with `expected=` the bytes that matched it, so any change to the source after the check is a conflict, and with a hash supplied a conflict is never retried.

### D13. The same-note move

`to_path` that resolves to the source note is detected after `open_mutable`, by `rel` equality **and** by leaf identity (`st_dev`, `st_ino` of both leaves through their anchored parents). Two different `rel` paths to one inode (a hard link, or a symlinked folder the resolver does not collapse) are refused `invalid_argument` ("`path` and `to_path` name the same file"), because the two-phase flow would read and publish one file as if it were two.

For the same note:

- one read, one ladder, one plan: the removal span (D7) and the insertion (D5, computed on the note **with the block removed**, so the moved task is never its own anchor and `to_section` is resolved without it) as one span list over the original text, which must not overlap;
- one verification over the old and new text combining D8's checks (implemented as the D7 removal's verification followed by the D5 insertion's on the intermediate text): every line outside the block keeps content and terminator (except in the two D6/D7 end-of-file cases, where a line outside the block can gain or lose its terminator so that the file keeps its final-terminator state — implementation deviation, recorded), every non-moved task identical, the task set unchanged, each moved task re-extracts identically, no non-moved line changes owner, masks and headings unchanged;
- one publication, one refresh, one `modified` event;
- a task that is already the last top-level task of the destination region (whatever blank or paragraph lines precede it) stays where it is, and that call, like any plan whose new text equals the old text, answers `No changes for <path>` with nothing written, no event and no refresh (spec verifier);
- no duplicate check against itself, and no partial outcome is possible.

### D14. Concurrency and locks

- **No file locks.** Each publication is the existing optimistic `write_file_at(…, expected=)`: a conflict is detected, never waited on.
- **No two database locks are ever held together.** Each refresh is its own transaction holding at most one `notes_metadata` row lock (2-second `lock_timeout`), committed or rolled back before the next refresh begins; each change-feed append is its own transaction taking the scope's advisory lock last. Two opposite moves (`A → B` and `B → A`, different tasks) therefore cannot deadlock: neither ever waits while holding a lock the other needs. A test pins "one row lock per transaction" by asserting the refresh is called once per note, sequentially, and never inside another transaction.
- **Two opposite moves interleaved** both succeed or, after their bounded retries, end in `concurrent_write` (phase 1, nothing written) or `task_move_incomplete` (phase 2, a duplicate). Never a loss.
- **Two moves of the same task** to different destinations: both publish their destination; one removal wins; the other re-locates, finds the key absent, and returns `task_move_incomplete` with `reason` `source_missing`, which says the source no longer holds a task with that id and does not claim it does. The task is then in two destinations, a duplicate (L12). To the same destination: the second's phase-1 retry re-reads the destination, finds the first's identical copy, and is refused `duplicate_task`, writing nothing.
- **The destination presence check (D10 step 6)** runs inside the source's publication callback, after the root confirmation and with no `await` before the source write. It shrinks, but cannot close, the window in which an outside writer can remove the destination copy before the source copy is gone (L17).
- **The residual of the shared publish path (#431) is inherited, not solved.** `expected=` compares the current bytes immediately before the rename, so a non-cooperating writer landing inside that window is overwritten, as with `edit_note` and the other task tools (#428 L26). A move has the window twice, once per note (L1). #447's own limitations of that path are inherited the same way, once per note: a permission change after the P2 capture and before the rename is lost with the replaced inode (#447 L7), and the named staged copy can be altered in the interval after its bytes are verified (#447 L14).
- **Admission.** One `_tracked` call: one general-bucket token, **one write-bucket token**, one concurrency slot of class `write` held through both publications, one quota slot. All retries run inside it.

### D15. Refresh, change feed, usage, rate limit, class

- **Refresh, per note**, after that note's confirmed publication (#428 D10, unchanged): destination after the insert, source after the removal, each in the task tools' order **publication → refresh → event** (not the generic writers' publication → event → refresh of #432/#444). Each reports `index (destination): refreshed | may lag (<code>)` and `index (source): …`. A same-note move has one `index:` line. A `resume=true` call publishes no destination bytes, so the destination is not refreshed and its line reads `index (destination): unchanged`. **A destination refresh that does not report `refreshed` does not stop the source removal** (owner decision, Max, 2026-11-16: Q10): the response carries `index (destination): may lag (<code>)` and L13 states the consequence.
- **No `may_hold_tasks` gate.** #444's gate (`tasks.may_hold_tasks`) decides whether a generic writer refreshes. `move_task` always refreshes each note it published: the destination's published bytes and the source's replaced bytes both hold the moved checkbox, so the gate would always be true.
- **An excluded sync-conflict copy (#443).** Either note may be an excluded conflict copy: resolution, placement and both publications read and write bytes by path, as every task write does. A refresh at an excluded path finds no index row and reports `may lag (not_indexed)`, so a move **into** an excluded copy completes with `index (destination): may lag (not_indexed)` and the task leaves the listings until the setting is turned off (L19). `move_task` prints no exclusion notice: it creates no note, and the notice belongs to `create_note` and `move_note` only.
- **Change feed:** one `modified` event per published note, `tool = 'move_task'`, attributed like every task write, each recorded after its own note's refresh: the destination's before the source removal is attempted, so a partial outcome still records it. No new event kind (L14). A refused call, a dry run and a no-change same-note move record none.
- **Usage row:** one per call. Logged params: `path`, `id`, `to_path`, `to_section`, `dry_run`, `allow_duplicate`, and the presence (never the value) of `expected_hash` and `to_expected_hash`. Logged params also include `resume`. Result fields: `move_outcome` (`moved`, `moved_within`, `resumed`, `no_change`, `incomplete:<reason>`, `uncertain`), `task_refresh` (source), `task_refresh_to` (destination), `task_write_attempts` (when > 1, per phase as `<p1>/<p2>`). No task text is logged. The outcome is classified from the typed `BodyOutcome` only.
- **Write bucket: one charge per call** (owner decision, Max, 2026-11-16: Q7). The bucket counts mutating calls, as `move_note` (which publishes up to 1 + N times) is charged once; one move is one intent.
- **Concurrency class `write`**; `CLASS_MAPPING_VERSION` 6 → 7, which starts a new readiness epoch for `make concurrency-report` (documented in `DEPLOYMENT.md` and `docs/deployment-kubernetes.md`).

### D16. The task rule and the default scope

**The rule never blocks or alters the write.** A moved task is moved as it is: no global filter text is prepended (#434 D7 applies to `add_task` only) and nothing is refused. **But the response states the rule's verdict** (Codex spec r2 #5), exactly as `complete_task`, `update_task` and `add_task` do (#434 D7): after planning, `move_task` runs the same check, `_task_rule_note(plan, line, effective)` in `src/mcp_server/tools.py`, which calls `task_rule.extracted_is_task` over the planned destination text's own extraction of the moved task line under the caller's effective rule, and when it is not a task appends `task_rule.not_task_note(effective)` (`note: not a task under your task rule "fields" (no task field and no task tag), so default listings will not show it; list_tasks(scope="all") will.`, or the `filter` form) to the success and the dry-run response. The verdict is the same at the source, since the line's text is unchanged, but a caller moving a plain checkbox under `fields` or `filter` would otherwise not learn why the task is missing from default listings in both notes.

**The location part of the default scope can change.** When `to_path` lies under one of the caller's excluded folders (#428 D14, the `folder_prefix_clause` semantics) and the source did not, the response carries `note: the destination is in a folder your default task scope excludes, so default listings will not show this task; list_tasks(scope="all") will.` Excluded note tags are not evaluated (they live in the index's note tags, L8).

### D17. The contract surface, enumerated

Every literal below was found by grep on `main` at aafb9f0 and **re-checked on `main` at fa27234** after the rebase: #443, #444 and #448 add no tool and change none of the counts; they move README line numbers (updated below) and add no count-pinning test (their new tests, `test_preserve_file_permissions.py`, `test_task_refresh_generic.py`, `test_issue_392_conflict_copies.py` and their integration twins, call tool implementations directly and enumerate no registry). **How (revised after Codex spec r1 #6, which found three misses):** over `tests/`, `src/`, `scripts/` (Python) and over `README.md`, `CLAUDE.md`, `DEPLOYMENT.md`, `docs/**/*.md`, `src/mcp_server/*.md`:

- `\b32\b` filtered to lines mentioning a tool, registry, listing, `TOOL_CLASSES` or a `len(...)`;
- `(==|len\(.*\)) *(11|12|13|20)\b` and the words `eleven`, `twelve`, `thirteen`, `TWELVE`, each read and kept only where it counts tools;
- `CLASS_MAPPING_VERSION` and `class_mapping_version`;
- the fixture names `tools_list_full_v2`, `tools_list_readonly_oauth`, `tools_list_schema_additions`;
- `"add_task"` and `'add_task'` (every per-tool map and registry lists the newest write tool);
- in the docs, `32 tools`, `all 32`, `of the 32`, `lists 20`, `twelve`, `eleven`.

No pin of `33` or `13` exists today. The implementer re-runs the same searches before finishing; the list is not trusted.

| Fact | Before | After | Where it is pinned (files that change) |
|---|---|---|---|
| registered tools | 32 | **33** | `tests/test_issue_343_tool_metadata.py` (`len(listed()) == 32`), `tests/test_issue_358_scoped_tools_list.py` (`== 32` in several places, the parametrised counts, module docstring), `tests/test_issue_295_unknown_arguments.py` (two), `tests/test_issue_263_coverage_registry.py` (`len(registered) == 32`, `COVERAGE`, `ADDED_AFTER_INVENTORY`), `tests/test_transfer_overlap_gate.py` (docstring "all 32 tools"), **`tests/integration/test_scoped_tools_list_pg.py`** (line 118, `== 32`; line 117's `== 20` is unchanged), `src/mcp_server/tool_permissions.py` (comment "lists 20 of 32") |
| `TOOL_CLASSES` entries | 32 | **33** | `tests/test_issue_261_controller.py`, `tests/test_issue_261_tool_admission.py` |
| `CLASS_MAPPING_VERSION` | 6 | **7** | `src/services/concurrency.py` (constant and its history comment), **`tests/test_hybrid_search_offline.py`** (line 820, `== 6`), **`tests/test_semantic_rerank.py`** (line 633, `== 6`) |
| write bucket (`_tracked(write_class=True)`) | 11 | **12** | `tests/test_issue_343_tool_metadata.py` (`len(w) == 11`; `TASK_WRITE_TOOLS`), `README.md` ("the eleven write tools" ×3), `CLAUDE.md` ("the eleven vault-mutating tools", "eleven-tool write bucket"), `DEPLOYMENT.md`, `docs/architecture/rate-limits.md` (the `write` class row, line 51's list), `docs/architecture/vault-tools.md` ("eleven tools since #428") |
| `WRITE_REQUIRED_TOOLS` | 12 | **13** | `src/mcp_server/tool_permissions.py`, `tests/test_issue_358_scoped_tools_list.py` (`TWELVE`), `CLAUDE.md` ("twelve names" ×2), `README.md` ("the twelve write tools"), `docs/architecture/vault-tools.md` ("a frozenset literal of twelve names") |
| destructive set | write bucket − {`create_note`, `add_task`} ∪ {`request_upload`} | **unchanged rule**; `move_task` is in it | `tests/test_issue_343_tool_metadata.py` (the rule is read from code; the enumerated scenario list), `server.py` annotation comment, `README.md` destructive list (line ~286) |
| read-only annotations | 19 | 19 | unchanged |
| read-only grant's `tools/list` | 20 | **20** | `tests/fixtures/tools_list_readonly_oauth.json` **unchanged**; its entries compared against the v3 oracle |
| registration order | `add_task` last | `move_task` after `add_task` | the first 32 entries keep their order |
| full-list oracle | `tools_list_full_v2.json` (32) | **new `tests/fixtures/tools_list_full_v3.json` (33)**; v2 kept unedited as the 1.3 record, and a test asserts v3 minus `move_task` equals v2 byte for byte (owner decision, Max, 2026-11-16: Q9) | `tests/test_issue_358_scoped_tools_list.py` (`ORACLE`, line 52, and its module docstring), `CLAUDE.md`, `docs/architecture/vault-tools.md` (line ~216), `README.md` (lines ~1388 and ~1394) |
| additions fixture | 7 entries | **8** (`move_task` appended) | `tests/fixtures/tools_list_schema_additions.json` |
| `SERVER_INSTRUCTIONS` | 1,812 characters, lead 505 | **1,897**, lead byte-identical | `src/mcp_server/server.py`: "…prints, `add_task` inserts one, and `move_task` moves one, with the lines indented under it, to another note or section." |
| description | | `move_task` ≤ 4,000 characters, contract first | `tests/test_issue_343_tool_metadata.py` (the cap test runs unchanged) |
| vault guide | five task tools | six | `src/mcp_server/vault_guide_primer.md`, its test |
| publishing-tool registries | 12 publishing tools | **13** | `tests/test_issue_88_root_confirmed_before_publish.py` (the publishing set, the per-tool call map), `tests/test_symlink_mutation_guard.py`, `tests/test_anchored_note_writes.py`, `tests/test_write_preconditions_notes.py`, `tests/test_issue_192_write_refusal_marker.py`, `tests/test_issue_263_tool_outcomes.py`, `tests/integration/test_concurrency_tool_checkout_peaks_pg.py` |
| refusal codes | | `task_move_incomplete` (body, partial; `reason` values per D11, `permissions_not_applied` included), `resume_unmatched` and `block_id_conflict` (task codes) | `src/services/refusals.py` (`BODY_CODES`), any test pinning the code set, `docs/architecture/vault-tools.md` "Refusal codes" |
| docs | | | `README.md` (tool count 32 → 33 at lines ~16, ~281 and ~1388, the architecture diagram's `(32 tools)` at ~2717, "lists 20" / "twelve write tools" / "all 32" at ~785–787, "the eleven write tools" at ~2190, ~2596 and ~2688, the task tool list, the upgrade note), `docs/architecture/vault-tools.md` "Published file permissions (#447)" (the edit rule's list of covered tools names "the three task write tools": add `move_task`'s two publications), `CLAUDE.md` (counts, the MCP tools section, the task-write key decision), `DEPLOYMENT.md` and `docs/deployment-kubernetes.md` (upgrade note: 33 tools, write bucket 12, `CLASS_MAPPING_VERSION` 7), `docs/architecture/oauth-and-grants.md` (line ~167, "lists 20 tools, not 32"), `docs/architecture/vault-tools.md` (Tool metadata table, Who sees which tools, a new "Moving a task (#430)" section), `docs/architecture/usage-attribution.md` (the params table), `docs/architecture/rate-limits.md`, `docs/architecture/indexing-and-embeddings.md` (two refreshes per move) |

Checked and unchanged: `tests/integration/test_task_tools_pg.py` (`len(listed) == 20`, a read-only grant), `tests/fixtures/tools_list_readonly_oauth.json`, the 19 read-only annotations.

**Main specs that name v2 or enumerate tools, checked after the rebase and not edited by this change.** Two requirements merged since aafb9f0 pin v2 as a record of their own change: `task-write`, "A generic writer's task refresh SHALL NOT change the writer's response…" (#444: "`tests/fixtures/tools_list_full_v2.json` SHALL match unchanged"), and `index-integrity`, "Tools that address a file by exact path…" (#443: "…SHALL stay byte-identical"). v2 stays on disk unedited and the v3-minus-`move_task` test keeps it exactly derivable from the new oracle, so both stay satisfied; the oracle the live list is compared against becomes v3 through the `mcp-request-routing` delta. Two requirements list tools illustratively rather than exhaustively: `file-permissions`, "A publication that replaces an existing regular file…" ("This covers `edit_note`, …, `add_task`, …"), and the same `index-integrity` requirement's list of path-addressed tools that work on an excluded copy. `move_task` falls under both general rules; the `task-write` delta states it explicitly for both (the envelope requirement), so neither list needs a MODIFIED delta.

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
- Optional lines, each when it applies: `ids renumbered: tasks sharing a moved key now carry new .<i>of<n> suffixes in <which note(s)>` (D1), `block ids moved: ^a, ^b (links to <path>#^a no longer resolve)` (Q6; a collision with a block id already in the destination is refused before publication, D8), the D16 task-rule note and scope note, and `index (destination): may lag (<code>)` when the destination refresh lagged (Q10).
- **`resume=true`:** `Resumed move: the destination already held this task at line N, id J; nothing was inserted, and the source copy was removed`, then the source lines and `index (destination): unchanged`.
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
    resume: bool = False

@dataclass(frozen=True)
class MovedBlock:          # what was read at the source, for the phase-2 re-locate
    key: str
    lines: tuple[str, ...]  # content of lines t … e−1, without terminators, as read
    tasks: tuple            # ExtractedTask of each moved task, source coordinates
    block_ids: tuple[str, ...]

@dataclass(frozen=True)
class MovePlan:
    insert: Plan | None     # None only for resume=true (D9)
    dest_lines: tuple[int, int]   # (k, n): where the moved lines sit in the destination, for the D10 6(b) check
    removal: Plan
    moved: MovedBlock

def plan_move(source_text: str, dest_text: str, args: MoveArgs, settings) -> MovePlan | TaskWriteRefusal
def plan_move_within(note_text: str, args: MoveArgs, settings) -> Plan | TaskWriteRefusal
def plan_relocated_removal(source_text: str, moved: MovedBlock) -> Plan | TaskWriteRefusal   # D10 rows 9–12
def destination_still_holds(dest_text: str, k: int, inserted: tuple[str, ...]) -> bool        # D10 6(b), pure
def verify_move_insert(old: str, new: str, plan: Plan, moved: MovedBlock) -> TaskWriteRefusal | None
def verify_removal(old: str, new: str, plan: Plan, moved: MovedBlock) -> TaskWriteRefusal | None
```

All pure, all run off the event loop; each returned plan has passed its verifier. `Plan` gains no field that changes the existing planners' behaviour; whatever the move needs (`removed_range`, `moved_lines`) is added with defaults that leave `plan_complete`, `plan_update`, `plan_add` and `verify` byte-identical in behaviour, pinned by the existing tests running unmodified.

## Alternatives rejected

- **"The agent does it with `add_task` then an edit."** Worse on every axis the issue cares about. `add_task` re-serialises the line from arguments, so the status, field order, rule text, `🆔`/`⛔`/`🏁`, block links and the continuation block are lost unless the agent rebuilds them; `add_task` cannot add a done task at all. The removal is an agent-composed `edit_note`, the very write that has clobbered notes before, with no verification that only the task's lines go. There is no ordering guarantee, no named intermediate state and no defined way to complete an interrupted move, and each agent reinvents all three. A server-side move is one verified, ordered, typed operation.
- **Source removal first, then the insert.** A failure between them loses the task. Refused by the issue's bar.
- **One atomic publication of both notes.** No filesystem primitive renames two files atomically, and a journal or a staging protocol would be a new durable mechanism in the publish path, out of all proportion to triage.
- **A server-side journal of in-flight moves** to resume or roll back after a crash. It adds durable state whose own failure modes need design, for a window (D10 row 16) whose worst case is a visible duplicate the caller completes with `resume=true`.
- **Rolling back the destination insert when the source removal fails.** The rollback is itself a publication that can fail or race, and it removes bytes; a duplicate is the safer stopping point.
- **Moving the line only.** Its children would be re-parented to the previous task in the source (D3).
- **The full design in this change** (nested tasks, callouts, prefix re-framing). Not rejected but deferred (owner decision, Max, 2026-11-16: Q0); it is the documented next step at the end of this file.
- **A dedicated `moved_task` change-feed kind.** `note_events` kinds are note-level; a task move is two note modifications, attributed to one tool. A consumer that wants the task-level view reads the tool name (L14).
- **Re-using `move_note`'s `partial_completion` code.** Indistinguishable from a note move's partial outcome, and its sentinel carries no destination (D11).
- **Automatic convergence** (the first draft's D9: an identical block at the destination taken as an earlier partial move, so the source copy is removed on any call). A task id names text within one note, not a task across notes; two notes can hold independent identical tasks, and the first call would delete one (Codex spec r1 #2). Replaced by the explicit `resume=true`.
- **Proceeding with the source removal after a destination refresh reports `superseded`, unchecked** (the first draft). An outside save of an older destination then left the task in neither note (Codex spec r1 #1). Replaced by the D10 6(b) presence check inside the source publication.

## Accepted limitations

- **L1** The shared publish path's residual window (#431) is inherited, once per note: a non-cooperating writer landing between a publication's byte comparison and its rename is overwritten. Not solved here.
- **L2** A process killed between the two publications returns nothing and leaves the task in both notes. The next index pass lists both. The agent, having had no response, lists both notes and completes with `resume=true` (D10 row 16).
- **L3** `resume=true` trusts the caller's claim that the identical destination copy is the one an earlier move wrote. If it was an independent identical task, one of two identical records is gone; its bytes remain at the destination. Without `resume`, an identical copy is always refused as a duplicate.
- **L4** Links and block references to the moved lines (`[[Inbox#^abc]]`) are not rewritten and stop resolving; the response names the block ids moved. A moved block id that already exists in the destination is refused (`block_id_conflict`), not renamed.
- **L5** Moved lines take the destination's terminator (D6). At the end of an unterminated destination the old last line gains a terminator, and at the end of an unterminated source the line before the block loses its terminator (D6, D7): the only pre-existing terminators a move changes.
- **L6** In this version a nested task, a quoted (callout) task, a block holding a blockquote, fence or heading line, and a destination region whose last top-level task is quoted are all refused (`task_malformed`); the refusal says `edit_note` can make the move by hand. The full design lifts the first three kinds through re-framing (documented next step).
- **L7** (Withdrawn with Q0. It stated the full design's blank-line rewrite under re-framing, which this version does not have.)
- **L8** The default-scope note (D16) covers excluded folders only, not excluded note tags.
- **L9** Ordered list markers are kept as written and nothing is renumbered, in either note.
- **L10** Suffixed ids sharing a key with a moved task are renumbered in both notes; an earlier listing's suffixed ids must be listed again.
- **L11** A block containing a heading is refused; move such content with `edit_note`.
- **L12** A partial outcome, or two concurrent moves of one task to different destinations, leaves the task in two notes; N concurrent moves to N destinations can leave it in N, or in N + 1 notes (source included) when every source removal stops partial. There is no delete-task tool; the agent completes with the call the partial outcome names, or removes a copy with `edit_note`.
- **L13** Either refresh may lag (#428 L9). When the destination's refresh lags and the source's succeeds, `list_tasks` shows the task in **neither** note until the next pass; the response names the lagging note (owner decision, Max, 2026-11-16: Q10).
- **L14** The change feed shows two `modified` events with `tool = 'move_task'`, not a task-level move.
- **L15** Two paths to one inode are refused, not treated as one note.
- **L16** Verification proves task identity, ownership, code status and headings. It does not prove that the rendered Markdown is identical: a moved ordered item may start a new list, and a moved block after a paragraph may render as part of a loose list.
- **L17** **The one window that can still lose the task.** The destination presence check (D10 6(b)) and the source write (6(c)) run in one synchronous step, but the source write itself (staging file, fsync, rename) takes time, typically milliseconds. An outside writer that replaces the destination with a version lacking the moved lines inside that window, for example an editor saving a stale buffer, leaves the task in neither note while `move_task` reports success. The same outside save made **after** a successful move has the same consequence, as it would after any manual cut-and-paste. Consequence: the task is recoverable only from the destination's outside history (the editor's file recovery, sync history, a backup) or from the moved lines this call's response printed. This is the #431 residual applied across two files; closing it would need a cross-file transaction the filesystem does not offer. Since #447 the source write between the check and the rename also captures and applies the source's permissions (P1), verifies the staged copy's bytes and captures the leaf again (P2), all before the byte comparison: the compare-to-rename window is unchanged, but the time from the 6(b) check to the source rename, and so this window, is longer by those steps.
- **L18** The presence check compares the moved lines at their inserted line number. A harmless edit above them in the destination (a line added at the top) fails the check, and the move stops with `destination_changed` instead of completing. Conservative by design: a stop produces a duplicate, never a loss.
- **L19** A move into an excluded sync-conflict copy (#443, `INDEX_EXCLUDE_CONFLICT_FILES` on) completes with `index (destination): may lag (not_indexed)`, and the task is then in no listing until the setting is turned off, as for `add_task` into such a note. No exclusion notice is printed (D15).
- **L21** **The source write's own compare-to-rename window (#431).** A non-cooperating editor that saves the source between the shared publication's `expected=` byte comparison and its rename has that save overwritten, so a line it added is lost while the call reports success. It is the window every write tool has (`edit_note` identically, L1), inherited by the source removal, not introduced by `move_task`; hardening it is #431.
- **L20** A permission-only change to either note during the call (#447: mode, group or ACL, bytes unchanged) is an in-call conflict: retried without a hash; with `to_expected_hash` it refuses `concurrent_write`, and with `expected_hash` it ends `task_move_incomplete` (`concurrent_write`), a duplicate the named `resume=true` call completes.
## Owner decisions (Max, 2026-11-16)

The owner accepted every recommendation of the open questions as written. Each line below is an owner decision (Max, 2026-11-16); the design above is written to it. Nothing else in this design is an owner decision.

- **Q0. Scope: the smaller first version.** The moved task is a top-level, unquoted task whose block holds no blockquote, fence or heading line, and a quoted destination anchor is refused (D3, D5). The full design is kept, labelled, as the documented next step ("Full design: the documented next step"); it is not part of this change.
- **Q1. Nested tasks** and **Q2. Re-indentation**: do not apply. Both were full-design questions; in this version a nested task is refused and nothing is re-framed. They are re-asked, with their former recommendations, in the documented next step.
- **Q3. The partial outcome's machine-readable facts: (a).** `task_move_incomplete` carries four optional fields on the `MCP-REFUSAL` line (`destination_path`, `source_id`, `destination_id`, `reason`), set only by that code (D11).
- **Q4. Completing a partial move: (a).** An explicit `resume=true`, which removes the source copy only when exactly one byte-identical block is at the destination, is refused otherwise (`resume_unmatched`) and cannot be combined with `allow_duplicate`, `to_section` or a same-note move (D9). No `to_id` argument.
- **Q5. Duplicates at the destination: (a).** `duplicate_task` for any destination task with the moved key, a byte-identical block included, and, for an open moved task, an open task with the same normalised description, unless `allow_duplicate=true` (D9).
- **Q6. Block references: (a).** A `^block` id moves with its line and is named in the response; a collision with an id already in the destination is refused `block_id_conflict` (D8, D18). No `allow_block_ref` argument.
- **Q7. Write-bucket charge:** one token per call, however many publications (D15).
- **Q8. Terminators:** the destination's, with the two end-of-file exceptions of D6 and D7.
- **Q9. The oracle:** a new `tests/fixtures/tools_list_full_v3.json`; v2 kept unedited and derived from v3 by a test (D17).
- **Q10. A destination refresh that does not report `refreshed`: (a).** The source removal proceeds and the response prints `index (destination): may lag (<code>)`; L13 states that `list_tasks` can then show the task in neither note until the next pass. The `destination_index_lagged` stop is rejected (D10).

## Adversarial scenarios for the implementation review

Each is a scenario the reviewer traces with file-and-line evidence to SAFE or a finding.

1. The source removal fails with an in-call conflict after the destination published, with and without `expected_hash`: the task is in both notes, the response is `task_move_incomplete` with both ids, the destination event is recorded, the `complete with:` line says to re-read the source and names `resume=true` for an unchanged block and the `source_changed` call otherwise, a plain retry is refused `duplicate_task` writing nothing, and, the block being unchanged, `resume=true` completes the move. With `expected_hash` and a context line added under the source task by the other writer: `resume=true` is `resume_unmatched` and the `allow_duplicate=true` call with the current id loses no line.
2. Between the two publications another writer completes the task in the source (its text gains `✅`, so its key changes): phase 2 must not remove the completed line; the outcome is `task_move_incomplete` (`source_missing`), whose `source:` line does not claim the source holds the task; no call writes a third copy without `allow_duplicate`.
3. Between the two publications another writer adds a context line under the task in the source: the block differs, so the source is not removed (the context line would be lost); the outcome is `source_changed` with the current id and the `allow_duplicate=true` completing call.
4. Between the two publications another writer adds an identical task line elsewhere in the source: the key now resolves to two tasks; phase 2 stops; no guess.
5. A destination publication that raises `OSError` after its rename: the source must not be touched; the outcome is `publication_uncertain`, naming both completing calls.
6. A vault reassignment between the two publications: the source publication is refused by `_confirmed_publication`, and the outcome is partial, never `nothing_written`.
7. Two opposite moves `A → B` and `B → A` released together, 50 rounds: no deadlock (no wait while holding a lock), no loss; every task ends in exactly one note or in two with a `task_move_incomplete`.
8. Two moves of the same task to two different destinations at once: no loss; one success and one `task_move_incomplete (source_missing)` that never says the source still holds the task.
9. Two moves of the same task to the same destination at once: the second is refused (`duplicate_task` on its re-read, or `source_missing`); the destination never holds three copies.
10. **Scope (Q0).** A sub-task four spaces deep under a parent: refused `task_malformed` naming the nesting, nothing written in either note, the parent and its other children untouched.
11. **Scope (Q0).** A task inside a callout (`> - [ ] x`), and a plain task whose destination region's last top-level task is `> - [ ] a`: both refused `task_malformed`, nothing written in either note.
12. **Scope (Q0).** A block holding a line `    > quoted context` under an unquoted task: refused, nothing written.
13. **Scope (Q0).** A block holding a fenced code block closed inside the block, and one whose fence closes after the block: both refused by the fence-line scan, nothing written; a tab-indented child line under a top-level task, with no fence, moves byte for byte.
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
31. The process is stopped between the two publications (simulated): the next pass indexes both copies, a plain retry writes nothing, and `resume=true` completes.
32. A move into an excluded folder of the default scope: the scope note appears; the write is unchanged.
33. The usage row never holds task text, for a success, a partial and every refusal.
34. **The superseded destination (Codex spec r1 #1).** After the destination publication, an outside editor saves an older destination without the moved lines; the destination refresh reports `superseded`. The source must not be written: the 6(b) check fails and the outcome is `task_move_incomplete` (`destination_changed`), with the source unchanged and the `complete with:` call re-inserting the task.
35. The same, but the older save lands between the 6(b) check and the source rename (an injected hook inside the publication callback): L17's window, reproduced and documented, not claimed safe.
36. A line inserted at the top of the destination between the two publications: the check fails (L18) and the move stops with a duplicate, never a loss.
37. **Independent identical tasks (Codex spec r1 #2).** `Inbox.md` and `Project.md` each hold an independent `- [ ] Call Alex` with identical context, before any move. `move_task` from `Inbox.md` to `Project.md` is refused `duplicate_task` and writes nothing; `allow_duplicate=true` leaves two copies in `Project.md`; only `resume=true` removes the inbox copy.
38. `resume=true` when the destination holds zero, or two, identical copies: `resume_unmatched`, nothing written. `resume=true` with `allow_duplicate=true`, with `to_section`, or with `to_path == path`: `invalid_argument`.
39. **The index lag (Codex spec r1 #3; Q10).** The destination's row lock is held for 3 s: the move completes, the response names `index (destination): may lag (lock_timeout)`, and `list_tasks` shows the task in neither note until the next pass, after which it shows it in the destination.
40. **Block-id collision (Codex spec r1 #7).** The moved task ends in `^followup` and the destination already holds another block ending in `^FollowUp`: `block_id_conflict` naming the id, nothing written in either note.
41. **End of file (Codex spec r1 #5).** Destination `Intro` (no newline) receives `- [ ] x`: exactly `Intro\n- [ ] x`. Source `Intro\n- [ ] x` (no final newline) loses the task: exactly `Intro`. Source `- [ ] x` alone: the empty file. The verifiers accept exactly these terminator changes and refuse any other.
42. **Permissions applied, destination (#447).** `PermissionsNotApplied` injected at the destination write (an ACL write failing `EIO`): the answer is `io_failure` saying nothing was written, not `publication_uncertain` and not partial; both notes' bytes, modes and ACLs are unchanged; no event, no refresh.
43. **Permissions applied, source (#447).** `PermissionsNotApplied` injected at the source write after the destination published and the 6(b) check passed: `task_move_incomplete` with `reason` `permissions_not_applied`, a `source:` line saying the source was not changed, no retry, the destination event recorded and no source event; the named `resume=true` call then completes the move.
44. **Permission-only change between the phases (#447).** A `chmod` of the source between the source write's P1 and P2 captures (an injected hook; a change before P1 is simply captured and applied, so that write succeeds first time), bytes unchanged: without `expected_hash` the in-call conflict is retried, the block re-locates identical and the move completes; with `expected_hash` it ends `task_move_incomplete` (`concurrent_write`) and `resume=true` with the same hash completes. Each note ends under the edit rule (its own mode, group and ACL where the server can set the group; #447's group translation where it cannot), never the folder's create rule.
45. **A destination that vanishes before its publication.** `to_path` is deleted (or replaced by a directory) after step 1: the destination write fails as the in-call conflict and is never published under the create rule; the call ends in a refusal (`concurrent_write`, or the re-read's own refusal) with nothing written in either note and no file created.
46. **An excluded sync-conflict copy (#443).** With `INDEX_EXCLUDE_CONFLICT_FILES` on, a move into `N (conflict 2026-05-29-19-45-39).md`: the move completes, `index (destination): may lag (not_indexed)`, no exclusion notice line; a move out of such a copy into an indexed note: the task is listed in the destination after the move.
47. **An unreadable destination (Codex spec r2 #1).** After the destination publication the presence-check read fails with `EACCES` (or the file becomes a directory, or grows past the cap): the source is not written, and the outcome is `task_move_incomplete` (`destination_unverified`) whose `destination:` line claims nothing about the contents; once readable, the named branch completes the move with no third copy.
48. **The task rule's verdict (Codex spec r2 #5).** Under `fields`, `- [ ] buy stamps` (no field, no task tag) moved, and dry-run moved, into an indexed note outside every excluded folder: the move proceeds and both responses carry the `not_task_note` line `add_task` and `update_task` print; under `all` no such line appears.
## Test plan

**Unit (pure), `tests/test_task_move_plan.py`:**

- the scope refusals (D3, D5): a nested task, a quoted task, a blockquote line, a fence line (closed inside and outside the block), a heading line in the block, a quoted destination anchor; and that every accepted block arrives byte for byte (D4);
- placement (D5) as `add_task`'s cases, with the marker kept and no prefix copied;
- terminators (D6) and the end-of-file rules for both the removal (D7) and the insertion;
- `verify_move_insert` and `verify_removal`: each check fails on a hand-built bad plan;
- D9: (a) with an identical and a differing block, (b), a done task, `allow_duplicate`, `resume` with zero, one and two identical copies, the forbidden combinations;
- the block-id collision check;
- `destination_still_holds` over the D10 6(b) cases (unchanged, superseded by an older save, a line inserted above);
- `plan_relocated_removal`: unchanged block, changed block, absent key, duplicated key;
- same note (D13): above, below, no change, heading in block;
- ids (D1): keys unchanged, ordinals renumbered on both sides;
- a property test over a corpus of notes: for every task, the two-note move's two new texts together contain every original line's content of both notes exactly once (or the move is refused for scope), and the same-note move is a permutation of lines;
- the existing `tests/test_task_write.py` runs unmodified.

**Wiring (offline), `tests/test_task_move_e2e.py`:** every row of the D10 table, by monkeypatching `write_file_at` / `_confirmed_publication` to conflict, raise or refuse at each publication; the response and sentinel of each outcome; the event and refresh order (destination before source); usage params and result fields; dry run publishes nothing; the D12 precondition matrix; for every partial outcome, the `destination:` / `source:` lines say only what was verified and the `complete with:` call, when then made, completes the move; the presence check runs inside the publication callback with no `await` before the source write (asserted by a hook that mutates the destination before the callback, which must stop the move); rows 6a and 14a by raising `vault_fs.PermissionsNotApplied` from each write, row 8a by failing the presence-check read, the row 9 branch (unchanged block → `resume=true`; changed block → `resume_unmatched`, then `allow_duplicate=true`), the task-rule note under `fields` and `filter` on success and dry run, and a P1/P2 permission difference (`File changed while editing`) at each write handled as rows 4 and 9.

**Integration (`tests/integration/test_task_move_pg.py`, real Postgres):** list → move → list shows the task in the destination with its id and not in the source, with no pass; both refreshes; two `modified` events attributed to the caller; the opposite-moves race (scenario 7); a held row lock on one note; tenancy (user 8's identically named notes untouched); the next pass after a move leaves `tasks_source_hash = content_hash` on both notes.

**Surface:** every count in D17; the v3 oracle and the v2 derivation; the additions fixture; the read-only fixture unchanged; instructions ≤ 2,000 with the lead unchanged; description ≤ 4,000; `#295` unknown arguments refused on `move_task`; `TOOL_CLASSES` complete; `CLASS_MAPPING_VERSION` 7; the coverage and publishing-tool registries.

## Spec review record

**Codex spec review, round 1 (2026-11-12): FAIL**, 1 BLOCKER, 3 MAJOR, 3 MINOR. All seven resolved:

1. **BLOCKER, fixed.** A `superseded` destination refresh did not stop the source removal, so an outside save of an older destination could leave the task in neither note. The source write now runs only after the D10 6(b) presence check, inside the same synchronous publication callback; a failure stops with `destination_changed`. The window that remains (during the source write itself, and any outside save after the move) is L17; rows 8 and 17 and scenarios 34–36 cover it.
2. **MAJOR, fixed.** Automatic convergence deleted an independent identical task on the first call. Removed: an identical block at the destination is refused `duplicate_task` like any duplicate; completing a partial move is the explicit `resume=true` (exactly one identical copy, else `resume_unmatched`; not combinable with `allow_duplicate` or `to_section`). Every partial outcome names its completing call (D10, D11). Q4 and Q5 rewritten.
3. **MAJOR, made an owner question (Q10).** A lagging destination refresh followed by a successful source refresh hides the task from `list_tasks` until the next pass. (a) proceed and say so (recommended, the existing task-tool contract; the scenario is now conditional and L13 states the gap) or (b) stop with `destination_index_lagged`. The spec carried (a) with the (b) text marked; the owner chose (a) on 2026-11-16.
4. **MAJOR, fixed.** The partial-outcome prose claimed the task was in both notes after an absent-key re-read. `source_missing` and `source_ambiguous` are now separate from `source_changed`, every outcome states only what was verified, and the completing call depends on the reason (D11 table).
5. **MINOR, fixed.** The two end-of-file terminator exceptions are stated in D6, D7, the verifiers (D8) and the spec scenarios, with exact bytes.
6. **MINOR, fixed.** D17 adds `tests/integration/test_scoped_tools_list_pg.py`, `tests/test_hybrid_search_offline.py`, `tests/test_semantic_rerank.py`, and on a re-run of the greps (method stated in D17) the `tool_permissions.py` comment, the README diagram and line 783, and `docs/architecture/oauth-and-grants.md`.
7. **MINOR, fixed.** A moved `^block` id that already exists in the destination is refused `block_id_conflict` before publication (D8).

The reviewer also judged Q3, Q5, Q7 and Q9 safe as recommended, Q1 and Q2 reasonable given the verifier and boundary tests, and Q8 reasonable once the end-of-file wording was truthful. It suggested the smaller first version, which the owner then chose (Q0).

**Spec review round 1 (Codex): FAIL (1 BLOCKER, 3 MAJOR, 3 MINOR), all folded in 66c0b0d. Rebased onto main at fa27234 and reconciled with #444, #443, #448:**

- **#444 (task rows refresh after `edit_note`, `set_frontmatter`, `write_file`, `delete_note`; #432):** Context and D15 state that generic writers refresh after their event and silently, that `move_task` keeps the task tools' publication → refresh → event order and visible `index:` lines, and that it always refreshes (no `may_hold_tasks` gate); D17 records that #444's "v2 SHALL match unchanged" sentence stays satisfied because v2 is kept and derived from v3.
- **#443 (sync-conflict exclusion; #392):** D15, L19, scenario 46 and the `task-write` envelope requirement: either note may be an excluded copy, a move into one completes with `index (destination): may lag (not_indexed)`, and `move_task` prints no exclusion notice (that line is `create_note`'s and `move_note`'s only).
- **#448 (published file permissions; #447):** both publications are edits and keep each note's own mode, ACL and group, never the create rule (D10, `task-write` publish requirement); a P1/P2 or staged-bytes difference is the in-call conflict on that note (rows 4 and 9); `PermissionsNotApplied` at the destination is a nothing-written `io_failure` (new row 6a, no longer `publication_uncertain`), and at the source a new partial reason `permissions_not_applied` (row 14a, D11), completed by `resume=true`; L17 notes the longer check-to-rename time; L20 and scenarios 42–45 added.
- **Owner decisions (Max, 2026-11-16)** folded in: the smaller first version (Q0) replaces nested-task moves and re-framing in D1–D5, D8, D9, D18, D19, L6/L7, the scenarios and the test plan, with the full design moved to the labelled appendix; Q3–Q10 recorded as decided; the Q10 (b) branch removed from the design, the spec and the scenarios.
- **Also corrected while reconciling:** row 1 and the spec's pre-publication bullet claimed `nothing_written: true` on every refusal; body codes (`invalid_argument`, `content_unsafe`, `io_failure`, …) do not carry it for the other task tools either, so both now say "where its code carries it".
- **Counts re-checked at fa27234:** 32 tools, write bucket 11, `WRITE_REQUIRED_TOOLS` 12, read-only grant 20, `CLASS_MAPPING_VERSION` 6, instructions 1,812 / lead 505: unchanged, so D17's before/after columns stand; README line numbers in D17 updated.

**Codex spec review, round 2 (2026-11-16): FAIL**, 5 MAJOR, 2 MINOR, no BLOCKER, no loss path found. All seven accepted with the reviewer's fix:

1. **MAJOR, fixed.** An unreadable destination at the presence check was reported as `destination_changed`. New reason `destination_unverified` (D10 6(b), row 8a, D11; spec scenario; scenario 47), which claims nothing about the contents; `destination_changed` only for a successful read (or no file) showing a mismatch.
2. **MAJOR, fixed.** A source `concurrent_write` with `expected_hash` named `resume=true`, which a changed block refuses. The completing call now re-reads the source and branches: unchanged block → `resume=true`; changed → the `source_changed` call; absent → `source_missing` (row 9, D11, spec table and two scenarios).
3. **MAJOR, fixed.** "Keeps its bits, ACL and group" ignored #447's group translation and mode-only fallback. Each publication applies the `file-permissions` edit rule, those included; the exact-preservation scenario now requires groups the server can set (D10, spec).
4. **MAJOR, fixed.** The `mcp-request-routing` delta kept "SHALL NOT change the set of tool names". It now says existing names, order and schemas are unchanged and `move_task` is appended.
5. **MAJOR, fixed.** A plain checkbox moved under `fields` / `filter` vanished from default listings unexplained. The success and dry-run responses carry `task_rule.not_task_note` through the existing `_task_rule_note` (D16, D18, spec scenario, scenario 48).
6. **MINOR, fixed.** The "no more than two copies" bound is per call or two concurrent calls; N concurrent moves to N destinations can leave N copies (D10, L12, spec).
7. **MINOR, fixed.** The source-mode-change scenario now places the change between P1 and P2 and asserts the new mode is kept with no line lost (spec, scenario 44).

**Spec review round 3 (Codex, verification of round 2): FAIL on 2 MAJOR + 1 MINOR scenario-wording findings, no loss path; all accepted as wording. Review budget for the spec ends here; implementation review follows.**

1. **MAJOR, wording.** The two-copy bound held only where a source removal succeeds; two partial moves (both source writes refused) leave three copies. Bound restated in the spec, D10 and L12.
2. **MAJOR, wording.** "The source removal conflicts" assumed the task stays in the source; it is now an unrelated edit that keeps the task (the removed or changed task is the row 9 re-read branch).
3. **MINOR, wording.** Every completing call omits `to_expected_hash` or passes a fresh destination hash (spec reasons list, D10).

**Codex implementation review, round 1 (at 2c46656): FAIL**, 1 BLOCKER, 2 MAJOR, 1 MINOR.

1. **BLOCKER, declined by triage.** An outside editor saving the source between the shared writer's byte comparison and its rename loses that editor's line. It is the shared compare-to-rename window of every write tool (#431, `_atomic_write_at`'s documented window; `edit_note` has it identically), not something `move_task` introduces; stated as L21 next to L17.
2. **MAJOR, fixed.** A destination that reads but is not valid UTF-8 was reported `destination_changed`; it is now `destination_unverified` (D10 6(b), row 8a, spec scenario).
3. **MAJOR, fixed.** A `complete with:` call could repeat the original `expected_hash`, stale after a concurrent source edit. Completing calls now omit it for a bare id and name a fresh read's `content_hash` for a suffixed id, on every row (D10, spec scenario).
4. **MINOR, fixed.** The crash test seeded the two-copy state; a test now stops a real move at the step boundary after the destination publication and completes it with `resume=true`.

**Spec verifier (same round): 0 blocking gaps.** Non-blocking items applied: a task already last in its region is left in place (D13); the empty-body terminator rule names its two cases (D6, spec); the D3 / D7 / D13 implementation deviations recorded in place; tasks 1.9 wording; tests for row 13's anchor marker, the printed id completing at the destination, and scenario 33's partial outcome; rows 8a and 10 / D11 name the UTF-8 and missing-file cases.

**Codex implementation review, round 2 (at 536cd2d): FAIL**, 1 BLOCKER, 1 MAJOR.

1. **BLOCKER, downgraded by triage (no line is lost: the moved bytes stay in the destination) and fixed anyway.** The presence check compared bytes only, so an unclosed fence opened above the moved lines left them in place but no longer a task, and the source was removed. 6(b) now re-extracts with the pass's extractor and requires the task, its sub-tasks, its block and its code masks (D10, spec scenario).
2. **MAJOR, fixed at the root.** A `source_missing` completing call could name a newly suffixed id without a fresh hash, the second round with this class. Every completing call now goes through one helper that decides the hash from the id it names (D10, D11, spec); a test pins that the helper is the only renderer.

**Codex implementation review, round 3 (verification of round 2): one finding, fixed.** With the moved bytes still at their line but no longer extracted as a task, `destination_changed` said the lines were gone and both printed calls failed. That state is now its own reason, `destination_not_a_task` (row 8b, D11, spec), whose completing call is to repair the destination and `resume=true`.

**Codex implementation review, round 4 (wording): fixed.** The `destination_not_a_task` text said the source still held the task, which the call had not verified. Every partial-outcome text was audited and now states only what the call verified (D11 table): the presence-check rows say "re-checked before the source write", the confirmation rows say the destination was written but not re-checked, the source rows say what a re-read found or that this call did not change the note, and removing a destination copy by hand is conditioned on reading the source first.

## Slice plan (independent agents, one git worktree each)

Pre-flight: this change committed on the base and `openspec validate task-move --strict` green. Every brief opens with the base check: `openspec/changes/task-move/` exists and `openspec/specs/file-permissions/spec.md` exists (merged in #448, so present only on a base at or after fa27234). No migration in any slice.

| Slice | Wave | Owns (only these files) | Depends / stubs |
|---|---|---|---|
| **M1 planner** | 1 | `src/services/task_write.py` (additions only: D3–D9, D13, D19), `tests/test_task_move_plan.py` (new) | none; must leave every existing function's behaviour unchanged |
| **M2 wiring** | 1 | `src/services/refusals.py` (the code and the four fields), `src/mcp_server/tools.py` (`move_task_impl`, the two-phase runner; no change to `_run_task_write` behaviour), `tests/test_task_move_e2e.py` (new), `tests/integration/test_task_move_pg.py` (new), `tests/test_refusals*.py` if the code set is pinned there | stubs `task_write.plan_move*` with the D19 signatures and a `TODO(#430)` until M1 merges; throwaway database on its own port |
| **M3 surface** | 2 (after M1, M2) | `src/mcp_server/server.py` (registration after `add_task`, description, instructions), `src/mcp_server/tool_permissions.py`, `src/services/concurrency.py`, `src/mcp_server/vault_guide_primer.md`, `tests/fixtures/tools_list_full_v3.json` (new), `tests/fixtures/tools_list_schema_additions.json`, every count-pinning and registry test in D17 | none |
| **M4 docs** | 2 | `README.md`, `CLAUDE.md`, `DEPLOYMENT.md`, `docs/deployment-kubernetes.md`, `docs/architecture/vault-tools.md`, `rate-limits.md`, `usage-attribution.md`, `indexing-and-embeddings.md` | this change's text only |

**Seams the supervisor greps after merging** (a production caller of each): `task_write.plan_move`, `plan_move_within`, `plan_relocated_removal` called by `move_task_impl`; `verify_move_insert` and `verify_removal` called by the planners; `task_index.refresh_note_tasks` called twice per two-note move; `refusals` code `task_move_incomplete` produced by `move_task_impl`; `move_task` in `WRITE_REQUIRED_TOOLS`, `TOOL_CLASSES` and the write bucket.

Then the full offline suite, the task and move integration modules, and `openspec validate --strict` once on the merged result.

## Full design: the documented next step (not this change)

**Status.** Deferred by owner decision (Max, 2026-11-16: Q0). Nothing in this section is a requirement, a task or a test of this change; the spec deltas and `tasks.md` carry the smaller first version only. A later change widens `move_task` to this design by lifting the D3/D5 scope refusals and adding the re-framing below, with no change to the tool's arguments. Everything not listed here (the D10 failure table, the presence check, the partial outcomes, `resume`, the L17 window) is the same in both versions: the reviewer noted that a smaller version "would not resolve the publication and retry findings", so the saving is in the framing and refusal surface, not in the two-note machinery.

**What it would add.**

- **Nested tasks (former Q1, recommendation (a)).** Moving a sub-task (a task inside another task's continuation block) is allowed: it takes its own subtree, leaves its parent and siblings in place, and lands at the destination as a top-level task. The response says `was nested under line N`, and `MovedBlock` gains `nested_under: int | None`. The reviewer judged (a) reasonable only with the specified verifier and boundary tests.
- **Callout tasks and quoted anchors**, through re-framing (below).
- **The code-status refusal by masks** instead of the fence-line scan: a fence opened inside the block and closed outside it, or the reverse, so that removing or inserting the block would change the code status of a line that is not moved, is refused; a fence closed inside the block moves.

**Re-framing (the full design's D4; former Q2, recommendation (a)).** Let the **source frame** `F_s` be the task line's text before its list marker: its blockquote prefix `Q_s` (every `>` and the spaces between them, plus at most one space after the last `>`, as `_shape` measures it) followed by its indentation `I_s`. Let the **destination frame** `F_d` be the destination anchor's text before its list marker. By D5's anchor rule the anchor has indentation width 0, so `F_d = Q_d`, a blockquote prefix or the empty string. Every line `L` of the moved block becomes:

| `L` | Becomes |
|---|---|
| starts with `F_s` (byte-exact) | `F_d + L[len(F_s):]` |
| blank (whitespace only after its blockquote prefix) and does not start with `F_s` | `Q_d` with trailing spaces and tabs removed |
| anything else | the move is refused `task_malformed` ("line N under the task does not start with the task's own indentation and quote prefix, so it cannot be re-indented without a guess") |

- When `F_s = F_d` the block arrives byte for byte (this version's only case).
- Relative structure is preserved: a child four spaces deeper than the task stays four spaces deeper, a deeper quote level stays deeper, every list marker is kept.
- The re-framing never touches content after the frame.
- Rejected there: **no re-framing at all** (a sub-task moved verbatim becomes a child of whatever precedes it, or an indented code block), and **re-framing by indentation width** (tab/space conversion rewrites bytes the caller never asked to change).
- Placement would copy `F_d`, the anchor's blockquote prefix, onto every moved line, and the planned owner of the moved task line would be the anchor's owner.
- The response would add `re-indented: quote/indent prefix replaced` when `F_s ≠ F_d`; the D8 checks 2 and 6 and the D9 comparisons would compare re-framed lines and frame-removed blocks.

**Its limitations** (replacing this version's L6 and L7): a block whose non-blank line does not start with the task's exact frame bytes (mixed tabs and spaces, a deeper quote written without the space) is refused rather than re-indented; blank lines inside a moved block that do not start with the source frame are rewritten as the destination's bare quote prefix (or empty).

**Its extra adversarial scenarios and tests:** a sub-task four spaces deep moved to a destination with no tasks after a paragraph lands at indentation 0 and re-extracts as a task; a callout task (`> - [ ] x` with `>     context`) moved to a plain note and a plain task moved into a callout's task list, frames substituted, ownership preserved; a child line using a tab where the task used spaces, refused; a fence closed inside the block moves and one closed after it is refused; the frame-pair unit matrix (none ↔ none, none ↔ `> `, indented ↔ none, tabbed, nested quotes, blank-line rules).

Verification: `openspec-verifier` at 2c46656 found 0 blocking gaps (6 non-blocking items, applied in 536cd2d). Codex implementation review: round 1 FAIL (1 BLOCKER declined as #431's shared window, L21; 2 MAJOR + 1 MINOR fixed); round 2 FAIL (1 BLOCKER downgraded by triage, no line lost, fixed; 1 MAJOR fixed at the root through `_completing_call`); round 3 FAIL (the new fence case's text, fixed as `destination_not_a_task`); round 4 FAIL (a partial-outcome claim the call had not verified). The same class had recurred, so every partial-outcome text was audited to state only what the call verified (46e380d); those texts were then read against the code by the supervising session, and the review budget ended there.
