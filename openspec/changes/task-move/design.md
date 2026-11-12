
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
2. Every intermediate state is named to the caller with only the facts the tool verified, and every partial outcome names the exact call that completes the move. Nothing is ever inferred from content alone: an identical block at the destination is not taken as proof of an earlier move unless the caller says so (`resume=true`).
3. The moved lines arrive byte for byte, apart from one defined re-framing; every other line of both notes is untouched; ambiguity is refused.
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

The moved lines' **content** is byte-exact under D4. Their **terminators** are the destination's: every moved line gets `T` = the terminator of the line it is inserted after, else the note's first terminator, else LF (the #428 D7 rule), so a CRLF note never receives LF lines or the reverse. Two boundary cases, exactly as #428's `_insertion` behaves:

- **Insertion at the end of a file with no final terminator.** `T` is prepended to the inserted lines instead of appended, so the pre-existing last line **gains** the terminator `T` and the last moved line has none: `Intro` (no newline) receiving `- [ ] x` becomes `Intro\n- [ ] x` (no newline). This is the one pre-existing line whose terminator changes at the destination.
- **Insertion into an empty body** (an empty file, or frontmatter ending the file): the moved lines are joined by `T` with no final terminator: an empty file becomes `- [ ] x` (no newline). Rejected: keeping the source terminators, which would make a CRLF destination mixed (open question Q8).

### D7. The removal span at the source

The removal deletes lines `t … e−1` with their terminators, as one span `[start of line t, start of line e)`. One exception keeps the file's final-terminator state: when line `e−1` is the last line and has no terminator, the span starts at the end of line `t−1`'s content instead (it takes the preceding terminator), so the file still ends without one. That line `t−1` is then the one pre-existing source line whose terminator changes: it **loses** it. `Intro\n- [ ] x` (no final newline) becomes `Intro` (no newline); `Intro\n- [ ] x\n` becomes `Intro\n` (no exception: every remaining line keeps its terminator); `- [ ] x` alone becomes the empty file. When `t` is the first body line and the block runs to the end of the file, the body becomes empty and the frontmatter block is untouched. Blank lines before `t` and after `e−1` are untouched (two blank lines may then be adjacent; nothing is collapsed).

### D8. Verification before each publication

Each plan is verified by a pure function that re-derives everything from the old and new texts, in the #428 D5 style, before any dry-run answer and before publication. The existing `verify` is not changed; `verify_removal` and `verify_move_insert` (or one generalised function that leaves `verify`'s existing behaviour byte-identical) are added. A failure is `task_malformed`, with nothing written.

**Destination (`verify_move_insert(old_d, new_d, plan)`):**

1. `new_d` is `old_d` with exactly one whole-line insertion at a line boundary; every pre-existing line keeps its content, and every pre-existing line keeps its terminator **except** in the D6 end-of-file case, where the old last line, unterminated, gains exactly `T` and nothing else changes.
2. The inserted lines equal the re-framed block line for line, content and terminator (D4, D6).
3. Every pre-existing task re-extracts identically (`_COMPARED` fields), shifted by the inserted count.
4. The new task set is the old set plus exactly the moved tasks, and **each moved task re-extracts with the same full text, key, status character, state, every date field, priority, tags, recurrence and `text_cut` as at the source**. Tags are therefore conserved by construction.
5. No pre-existing line changes its owning task; each moved line is owned as planned (D5).
6. The code mask of every pre-existing line is unchanged; each moved line's mask, re-framed, equals its source mask re-framed (its code status is the same); the headings and the frontmatter block are unchanged.

**Source (`verify_removal(old_s, new_s, plan)`):**

1. `new_s` is `old_s` with exactly the D7 span removed; every remaining line keeps its content, and every remaining line keeps its terminator **except** in the D7 end-of-file case, where line `t−1` loses exactly its terminator and becomes the unterminated last line.
2. Every remaining task re-extracts identically, shifted by the removed count.
3. The new task set is the old set minus exactly the moved tasks.
4. No remaining line changes its owning task.
5. The code mask of every remaining line, the headings and the frontmatter block are unchanged.

**Block ids (Codex spec r1 #7).** Before publication, every `^block-id` the moved lines carry (a ` ^id` suffix at the end of a line, read as the plugin reads a task's block link, on the unmasked text) is compared, case-insensitively, with every block id already present in the destination's body. A collision is refused `block_id_conflict`, naming the id and the destination line, with nothing written in either note: two blocks with one id make `[[note#^id]]` ambiguous. A same-note move cannot create a collision and does not run the check.

A plan whose result exceeds `MAX_NOTE_BYTES` is `size_limit` (`_note_size_error`), and one whose extraction would exceed the 5,000-task cap is `size_limit`, in either note, before anything is published.

### D9. Duplicates at the destination, and the explicit `resume`

Both are decided from the **destination's current bytes**, never from the index. A task id names text within one note, not a task across notes, so **an identical block at the destination is never taken as proof of an earlier partial move** (Codex spec r1 #2): two notes may legitimately hold independent identical tasks.

**Duplicates (refused `duplicate_task` unless `allow_duplicate=true`, nothing written in either note)**, naming up to 5 matches as `path:line id`:

- (a) a task in the destination with the moved task's key, **whatever its block**, a byte-identical block included;
- (b) when the moved task is open: an open destination task with the same normalised description (#428 D9, `add_task`'s target-note rule, rule-independent as #434 Q1).

When a match in (a) is byte-identical to the moved block (both compared with their frames removed), the refusal says so and names both ways on: `allow_duplicate=true` to keep both copies, or, if this is the copy an earlier `move_task` wrote, `resume=true` to remove the source copy. A done or cancelled moved task is checked by (a) only: an archive note legitimately holds many done tasks with one description. The index-side scan of other notes and `add_task`'s similar-task list are not run: a move creates no new task.

`allow_duplicate=true` inserts a further copy. It only ever adds lines, so it can never cause a loss.

**`resume=true`** has exactly one meaning: "the destination already holds this exact block; remove the source copy and insert nothing".

- It requires **exactly one** task in the destination with the moved key whose block, with frames removed, is byte-identical to the moved block. Zero or more than one is refused `resume_unmatched` (the prose says which, and names the candidates), writing nothing.
- It then runs phase 2 only (D10 steps 6–8), with the destination presence check aimed at that copy's position.
- It cannot be combined with `allow_duplicate` or `to_section` (D1), and a same-note `resume` is refused (D12).
- Its only removal is of source lines whose bytes, re-framed, are present at the destination at the moment of the check. If the destination copy was in fact an independent task, the caller asserted otherwise, and the cost is that one of two identical records is gone while its bytes remain (L3).

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
   - (b) **the destination presence check (Codex spec r1 #1):** re-read the destination through its anchored target and require that the lines at the inserted position (lines `k` … `k+n−1`, where step 3 put them, or where `resume` found the copy) have exactly the contents of the inserted lines, byte for byte. Anything else fails the check (an outside editor saved an older destination, a line was inserted above, the file is gone or over the cap), and the source is not written;
   - (c) the source write, `expected=` the source bytes the removal was planned against.
7. Refresh the source's task rows.
8. Record the source's `modified` event.

The destination is published first because a failure after it leaves a duplicate, and a failure after a source-first removal would leave a loss. **The source is never written unless step 3 returned success (or `resume` found the copy) and the step 6(b) check passed in the same synchronous step as the write.** A destination refresh that reports `superseded` (the destination changed after step 3) does not by itself stop the move, but it can never be followed by a source removal unless the check in 6(b), run later, finds the moved lines still in place.

In the table, `P` and `I` are the source path and id, `D` and `J` the destination path and the moved task's id there. Every partial outcome carries a `complete with:` line naming the exact call that completes the move (D11).

| # | Step | What happens | Destination | Source | Caller receives | `complete with:` |
|---|---|---|---|---|---|---|
| 1 | arguments, hash syntax | refused | unchanged | unchanged | the refusal, `nothing_written: true` | (not partial) |
| 2 | step 1, **either** note | refused (incl. `duplicate_task`, `resume_unmatched`, `block_id_conflict`) | unchanged | unchanged | that refusal; a precondition refusal names its note | (not partial) |
| 3 | `dry_run` | | unchanged | unchanged | both diffs, the moved lines, the new ids | |
| 4 | step 3: in-call conflict | without `to_expected_hash`: repeat from step 1, re-reading **both** notes, up to 3 attempts; then, or with it, refuse | unchanged | unchanged | `concurrent_write`, `nothing_written: true` | (not partial; the same call again) |
| 5 | step 3: vault reassigned, anchor lost | refused by `_confirmed_publication` | unchanged | unchanged | the existing marker refusal | (not partial) |
| 6 | step 3: OS error | the rename may or may not have happened | **uncertain** | unchanged | `publication_uncertain`, partial | `read_note(D)`; if it holds the lines, `move_task(path=P, id=I, to_path=D, resume=true)`, otherwise `move_task(path=P, id=I, to_path=D)` |
| 7 | step 4: refresh not `refreshed` | **Q10.** (a), recommended: continue, the response says `index (destination): may lag (<code>)`; (b): stop before step 6 | holds the task | (a) removed at step 6; (b) unchanged | (a) success with the lag line; (b) `task_move_incomplete` (`destination_index_lagged`) | (b) `move_task(path=P, id=I, to_path=D, resume=true)` |
| 8 | step 6(b) fails | source not written | **verified**: no longer holds the moved lines at line `k` | not changed by this call | `task_move_incomplete` (`destination_changed`) | `move_task(path=P, id=I, to_path=D)`; if that is refused `duplicate_task` because the copy is still elsewhere in `D`, `move_task(path=P, id=I, to_path=D, resume=true)` |
| 9 | step 6(c): in-call conflict, retries exhausted or `expected_hash` given | stop | holds the task (verified in 6(b)) | not removed by this call | `task_move_incomplete` (`concurrent_write`) | `move_task(path=P, id=I, to_path=D, resume=true)`, with `expected_hash` from a fresh `read_note(P)` if one was given |
| 10 | step 6 retry: re-read finds no task with the key of `I` | stop | holds the task | **verified**: no task with id `I` | `task_move_incomplete` (`source_missing`) | nothing, if it was removed or moved elsewhere; if its text was edited, `list_tasks(path=P)` gives its new id and `move_task(path=P, id=<new id>, to_path=D, allow_duplicate=true)` moves the edited copy beside the earlier one |
| 11 | step 6 retry: the key resolves once, its block differs | stop (removing it would lose the edit) | holds the task | **verified**: holds an edited version, id `I'` | `task_move_incomplete` (`source_changed`) | `move_task(path=P, id=I', to_path=D, allow_duplicate=true)`: both versions then sit in `D`, nothing is lost |
| 12 | step 6 retry: the key resolves to several tasks | stop (no guess) | holds the task | **verified**: several identical tasks | `task_move_incomplete` (`source_ambiguous`) | `read_note(P)`, then `move_task(path=P, id=<key>.<i>of<n>, to_path=D, resume=true, expected_hash=<content_hash>)` naming the copy to remove |
| 13 | step 6(a): reassigned, anchor lost, confirmation outage | stop | holds the task | not removed | `task_move_incomplete` (that marker as `reason`) | `move_task(path=P, id=I, to_path=D, resume=true)` once the assignment is restored |
| 14 | step 6(c): OS error | the removal may or may not have happened | holds the task | **unknown** | `task_move_incomplete` (`io_failure`) | `read_note(P)`; if it still holds the task, `move_task(path=P, id=I, to_path=D, resume=true)`; otherwise the move is complete |
| 15 | step 7: refresh not `refreshed` | the move stands | holds the task | removed | success, `index (source): may lag (<code>)` | |
| 16 | process killed between steps 3 and 6 | no response | holds the task | unchanged | nothing (L2) | the agent lists both notes; if both hold it, `move_task(path=P, id=I, to_path=D, resume=true)` |
| 17 | an outside writer replaces the destination with a version lacking the moved lines **after 6(b) and before 6(c)'s rename completes**, or at any time after the move | the source copy is removed | **lacks the task** | removed | success | **loss** (L17): recoverable only from the destination's outside history or from this call's response |
| 18 | everything succeeds | | holds the task | removed | success (D18) | |

**Re-locating the block for a source retry (rows 9–12).** Nothing from the first read is trusted. The source is re-read through the same anchored descriptor; the tasks are re-derived; the moved key must resolve to exactly one task (the original id was bare, since a suffixed id carries `expected_hash` and never retries), and that task's block must be byte-identical to the moved block as read in step 1. Only then is a new removal planned, verified (D8) and published, again through step 6 with its presence check. If the task was edited after the destination copy was written, the source copy is not removed: removing it would lose the edit.

**Nothing grows past two copies without the caller asking.** A plain retry of a partial move is refused `duplicate_task` (D9 (a)) and writes nothing. Only `allow_duplicate=true` adds a copy. Only `resume=true` removes the source copy, and only when exactly one identical copy is at the destination at the moment of the check.

**Q10 (b), if chosen,** is row 7's second branch: when step 4 does not report `refreshed`, the move stops before step 6 with `task_move_incomplete` (`destination_index_lagged`). The list → move → list guarantee then holds unconditionally, at the cost of a duplicate the caller completes with `resume=true`.
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
| `concurrent_write` | `holds the task at line N, id J (checked before the source write)` | `another writer changed it during this call; this call did not remove the task` |
| `source_missing` | `holds the task at line N, id J` | `no longer holds a task with id I (it was removed, moved, or its text was edited by another writer)` |
| `source_changed` | `holds the task at line N, id J` | `holds an edited version of the task, id I'; it was not removed, so the edit is kept` |
| `source_ambiguous` | `holds the task at line N, id J` | `now holds several identical tasks with this text; none was removed` |
| `destination_changed` | `no longer holds the moved lines at line N (another writer changed the note after this call wrote it)` | `not changed by this call` |
| `destination_index_lagged` (Q10 (b) only) | `holds the task at line N, id J; its task index could not be updated (<code>)` | `not changed by this call` |
| `vault_assignment_changed`, `vault_anchor_lost_at_publish`, `vault_confirmation_unavailable` | `holds the task at line N, id J` | `not changed by this call` |
| `io_failure` | `holds the task at line N, id J` | `unknown: the removal failed part-way; read the note` |

- A partial outcome **never** says the source holds the task unless a re-read in this call found it there.
- **Four optional `Refusal` fields** (`destination_path`, `source_id`, `destination_id`, `reason`), rendered absent-not-null like the precondition fields, and set only by this code (a `Refusal` construction check enforces that). `destination_id` is absent for `destination_changed`. Paths are bounded as every path in the sentinel is; ids are server-derived hex. Rejected: carrying them in the prose only (agents are told to act on the sentinel), and reusing `partial_completion` (indistinguishable from `move_note`'s). Open question Q3.
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
- one verification over the old and new text combining D8's checks: every line outside the block keeps content and terminator, every non-moved task identical, the task set unchanged, each moved task re-extracts identically, no non-moved line changes owner, masks and headings unchanged;
- one publication, one refresh, one `modified` event;
- a plan whose new text equals the old text (the task is already where it would go) answers `No changes for <path>` with nothing written, no event and no refresh;
- no duplicate check against itself, and no partial outcome is possible.

### D14. Concurrency and locks

- **No file locks.** Each publication is the existing optimistic `write_file_at(…, expected=)`: a conflict is detected, never waited on.
- **No two database locks are ever held together.** Each refresh is its own transaction holding at most one `notes_metadata` row lock (2-second `lock_timeout`), committed or rolled back before the next refresh begins; each change-feed append is its own transaction taking the scope's advisory lock last. Two opposite moves (`A → B` and `B → A`, different tasks) therefore cannot deadlock: neither ever waits while holding a lock the other needs. A test pins "one row lock per transaction" by asserting the refresh is called once per note, sequentially, and never inside another transaction.
- **Two opposite moves interleaved** both succeed or, after their bounded retries, end in `concurrent_write` (phase 1, nothing written) or `task_move_incomplete` (phase 2, a duplicate). Never a loss.
- **Two moves of the same task** to different destinations: both publish their destination; one removal wins; the other re-locates, finds the key absent, and returns `task_move_incomplete` with `reason` `source_missing`, which says the source no longer holds a task with that id and does not claim it does. The task is then in two destinations, a duplicate (L12). To the same destination: the second's phase-1 retry re-reads the destination, finds the first's identical copy, and is refused `duplicate_task`, writing nothing.
- **The destination presence check (D10 step 6)** runs inside the source's publication callback, after the root confirmation and with no `await` before the source write. It shrinks, but cannot close, the window in which an outside writer can remove the destination copy before the source copy is gone (L17).
- **The residual of the shared publish path (#431) is inherited, not solved.** `expected=` compares the current bytes immediately before the rename, so a non-cooperating writer landing inside that window is overwritten, as with `edit_note` and the other task tools (#428 L26). A move has the window twice, once per note (L1).
- **Admission.** One `_tracked` call: one general-bucket token, **one write-bucket token**, one concurrency slot of class `write` held through both publications, one quota slot. All retries run inside it.

### D15. Refresh, change feed, usage, rate limit, class

- **Refresh, per note**, after that note's confirmed publication (#428 D10, unchanged): destination after the insert, source after the removal. Each reports `index (destination): refreshed | may lag (<code>)` and `index (source): …`. A same-note move has one `index:` line. A `resume=true` call publishes no destination bytes, so the destination is not refreshed and its line reads `index (destination): unchanged`. **Whether a destination refresh that does not report `refreshed` still lets the source removal go ahead is open question Q10**; the design is written to recommendation (a), and option (b) is one added row in the D10 table.
- **Change feed:** one `modified` event per published note, `tool = 'move_task'`, attributed like every task write, each recorded after its own note's refresh: the destination's before the source removal is attempted, so a partial outcome still records it. No new event kind (L14). A refused call, a dry run and a no-change same-note move record none.
- **Usage row:** one per call. Logged params: `path`, `id`, `to_path`, `to_section`, `dry_run`, `allow_duplicate`, and the presence (never the value) of `expected_hash` and `to_expected_hash`. Logged params also include `resume`. Result fields: `move_outcome` (`moved`, `moved_within`, `resumed`, `no_change`, `incomplete:<reason>`, `uncertain`), `task_refresh` (source), `task_refresh_to` (destination), `task_write_attempts` (when > 1, per phase as `<p1>/<p2>`). No task text is logged. The outcome is classified from the typed `BodyOutcome` only.
- **Write bucket: one charge per call** (open question Q7). The bucket counts mutating calls, as `move_note` (which publishes up to 1 + N times) is charged once; one move is one intent.
- **Concurrency class `write`**; `CLASS_MAPPING_VERSION` 6 → 7, which starts a new readiness epoch for `make concurrency-report` (documented in `DEPLOYMENT.md` and `docs/deployment-kubernetes.md`).

### D16. The task rule and the default scope

**The rule is not consulted for the write.** A moved task is moved as it is: no global filter text is prepended (#434 D7 applies to `add_task` only), nothing is refused, and no rule statement is added, because the moved line's text is unchanged and the rule (`all`, `fields`, `filter`) reads only the line's text and fields, so its verdict cannot change with the move.

**The location part of the default scope can change.** When `to_path` lies under one of the caller's excluded folders (#428 D14, the `folder_prefix_clause` semantics) and the source did not, the response carries `note: the destination is in a folder your default task scope excludes, so default listings will not show this task; list_tasks(scope="all") will.` Excluded note tags are not evaluated (they live in the index's note tags, L8).

### D17. The contract surface, enumerated

Every literal below was found by grep on `main` at aafb9f0. **How (revised after Codex spec r1 #6, which found three misses):** over `tests/`, `src/`, `scripts/` (Python) and over `README.md`, `CLAUDE.md`, `DEPLOYMENT.md`, `docs/**/*.md`, `src/mcp_server/*.md`:

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
| refusal codes | | `task_move_incomplete` (body, partial), `resume_unmatched` and `block_id_conflict` (task codes) | `src/services/refusals.py` (`BODY_CODES`), any test pinning the code set, `docs/architecture/vault-tools.md` "Refusal codes" |
| docs | | | `README.md` (tool count 32 → 33 at lines ~16, ~281, the architecture diagram's `(32 tools)` at ~2647, "all 32" at ~783, the task tool list, the upgrade note), `CLAUDE.md` (counts, the MCP tools section, the task-write key decision), `DEPLOYMENT.md` and `docs/deployment-kubernetes.md` (upgrade note: 33 tools, write bucket 12, `CLASS_MAPPING_VERSION` 7), `docs/architecture/oauth-and-grants.md` (line ~167, "lists 20 tools, not 32"), `docs/architecture/vault-tools.md` (Tool metadata table, Who sees which tools, a new "Moving a task (#430)" section), `docs/architecture/usage-attribution.md` (the params table), `docs/architecture/rate-limits.md`, `docs/architecture/indexing-and-embeddings.md` (two refreshes per move) |

Checked and unchanged: `tests/integration/test_task_tools_pg.py` (`len(listed) == 20`, a read-only grant), `tests/fixtures/tools_list_readonly_oauth.json`, the 19 read-only annotations.

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
- Optional lines, each when it applies: `was nested under line N` (D3), `re-indented: quote/indent prefix replaced` (D4 with `F_s ≠ F_d`), `ids renumbered: tasks sharing a moved key now carry new .<i>of<n> suffixes in <which note(s)>` (D1), `block ids moved: ^a, ^b (links to <path>#^a no longer resolve)` (Q6; a collision with a block id already in the destination is refused before publication, D8), the D16 scope note, and `index (destination): may lag (<code>)` when the destination refresh lagged (Q10 (a)).
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
    nested_under: int | None
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
- **L6** A block whose non-blank line does not start with the task's exact frame bytes (mixed tabs and spaces, a deeper quote written without the space) is refused rather than re-indented.
- **L7** Blank lines inside a moved block that do not start with the source frame are rewritten as the destination's bare quote prefix (or empty): their whitespace is not preserved when the frames differ.
- **L8** The default-scope note (D16) covers excluded folders only, not excluded note tags.
- **L9** Ordered list markers are kept as written and nothing is renumbered, in either note.
- **L10** Suffixed ids sharing a key with a moved task are renumbered in both notes; an earlier listing's suffixed ids must be listed again.
- **L11** A block containing a heading is refused; move such content with `edit_note`.
- **L12** A partial outcome, or two concurrent moves of one task to different destinations, leaves the task in two notes. There is no delete-task tool; the agent completes with the call the partial outcome names, or removes a copy with `edit_note`.
- **L13** Either refresh may lag (#428 L9). Under Q10 (a), when the destination's refresh lags and the source's succeeds, `list_tasks` shows the task in **neither** note until the next pass; the response names the lagging note. Under Q10 (b) this case becomes a partial outcome instead.
- **L14** The change feed shows two `modified` events with `tool = 'move_task'`, not a task-level move.
- **L15** Two paths to one inode are refused, not treated as one note.
- **L16** Verification proves task identity, ownership, code status and headings. It does not prove that the rendered Markdown is identical: a moved ordered item may start a new list, and a moved block after a paragraph may render as part of a loose list.
- **L17** **The one window that can still lose the task.** The destination presence check (D10 6(b)) and the source write (6(c)) run in one synchronous step, but the source write itself (staging file, fsync, rename) takes time, typically milliseconds. An outside writer that replaces the destination with a version lacking the moved lines inside that window, for example an editor saving a stale buffer, leaves the task in neither note while `move_task` reports success. The same outside save made **after** a successful move has the same consequence, as it would after any manual cut-and-paste. Consequence: the task is recoverable only from the destination's outside history (the editor's file recovery, sync history, a backup) or from the moved lines this call's response printed. This is the #431 residual applied across two files; closing it would need a cross-file transaction the filesystem does not offer.
- **L18** The presence check compares the moved lines at their inserted line number. A harmless edit above them in the destination (a line added at the top) fails the check, and the move stops with `destination_changed` instead of completing. Conservative by design: a stop produces a duplicate, never a loss.
## Open questions for the owner

Each has a recommendation; the design above is written to the recommendation.

- **Q0. Scope.** The full design, or the "Smaller first version" below. **Recommend the smaller first version** for the first release, with the full design kept as the documented path. It covers ordinary inbox triage, removes about a third of the refusal and framing surface, and leaves the publication, presence-check and resume machinery (which both versions need) as the only hard part.
- **Q1. Nested tasks** (full design only). (a) Move the subtree, and allow moving a sub-task out of its parent; (b) move the subtree, but refuse a task that is itself nested; (c) refuse any task with sub-tasks. **Recommend (a):** triage moves whole items, and filing a sub-task into another project is a real case; the response names the former parent. The reviewer judged (a) reasonable only with the specified verifier and boundary tests, which are in D8 and the scenarios.
- **Q2. Re-indentation** (full design only). (a) Re-frame to the destination's top level by prefix substitution (D4); (b) never re-indent: refuse when the source frame differs from the destination frame. **Recommend (a):** (b) refuses every sub-task move and every move into or out of a callout, and re-framing is byte-exact or it refuses.
- **Q3. The partial outcome's machine-readable facts.** (a) Four optional fields on the `MCP-REFUSAL` line, set only by `task_move_incomplete`; (b) the code alone, with the facts in fixed prose lines. **Recommend (a)** (the reviewer agreed): agents act on the sentinel, and the completing call needs both paths and both ids.
- **Q4. How a partial move is completed (revised after Codex spec r1 #2).** Automatic convergence is removed: content alone never proves an earlier move. (a) An explicit `resume=true` that removes the source copy only when exactly one byte-identical block is at the destination, refused otherwise, not combinable with `allow_duplicate` or `to_section`; (b) the same, but also requiring the caller to pass the destination id it was given (`to_id`), so the copy is named rather than searched for. **Recommend (a):** the partial outcome already prints the exact call; (b) adds an argument for a case (two identical copies at the destination) that (a) already refuses.
- **Q5. Duplicates at the destination (revised).** (a) Refuse `duplicate_task`, unless `allow_duplicate=true`, for any destination task with the moved key, a byte-identical block included, and, when the moved task is open, for an open task with the same normalised description; (b) refuse only the same key. **Recommend (a):** it is `add_task`'s target-note rule, and it makes a plain retry after a partial outcome write nothing. `allow_duplicate` only ever adds a copy; `resume` only ever removes the source copy; the two cannot be combined.
- **Q6. Block references.** (a) Move, name the moved `^block` ids in the response (links to them in the source stop resolving), and refuse a collision with an id already in the destination (`block_id_conflict`, decided after Codex spec r1 #7); (b) also refuse any block that carries an id unless `allow_block_ref=true`. **Recommend (a):** a block id on an inbox task is rare, and the agent sees the names; the collision, which would make links ambiguous rather than broken, is refused either way.
- **Q7. Write-bucket charge.** One per call (recommended, the `move_note` precedent) or two (one per publication).
- **Q8. Terminators.** The destination's terminator (recommended) or the source line's own. Either way the two end-of-file exceptions of D6 and D7 apply and are stated.
- **Q9. The oracle.** A new `tools_list_full_v3.json` with v2 derived from it (recommended, the #428 precedent, which also proves no other tool changed) or regenerate v2 in place.
- **Q10. A destination refresh that does not report `refreshed` (Codex spec r1 #3).**
  - (a) Proceed with the source removal and print `index (destination): may lag (<code>)`. This is the contract `complete_task`, `update_task` and `add_task` already have. The list → move → list scenario is then conditional on both refreshes reporting `refreshed`, and L13 states that `list_tasks` can show the task in neither note until the next pass.
  - (b) Keep the source copy and return `task_move_incomplete` (`destination_index_lagged`) whenever the destination's rows could not be established, completed with `resume=true`. The list → move → list scenario is then unconditional.
  - **Recommend (a).** A lock timeout is transient and the next pass converges; the files are correct either way; and (b) turns an index hiccup into a duplicate in the vault, the owner's source of truth, which the caller must then clean up. The spec carries (a) and marks the one sentence and the one scenario that change under (b), so either answer is a small edit.
## Adversarial scenarios for the implementation review

Each is a scenario the reviewer traces with file-and-line evidence to SAFE or a finding.

1. The source removal fails with an in-call conflict after the destination published, with and without `expected_hash`: the task is in both notes, the response is `task_move_incomplete` with both ids, the destination event is recorded, the `complete with:` line names `resume=true`, a plain retry is refused `duplicate_task` writing nothing, and the named `resume=true` call completes the move.
2. Between the two publications another writer completes the task in the source (its text gains `✅`, so its key changes): phase 2 must not remove the completed line; the outcome is `task_move_incomplete` (`source_missing`), whose `source:` line does not claim the source holds the task; no call writes a third copy without `allow_duplicate`.
3. Between the two publications another writer adds a context line under the task in the source: the block differs, so the source is not removed (the context line would be lost); the outcome is `source_changed` with the current id and the `allow_duplicate=true` completing call.
4. Between the two publications another writer adds an identical task line elsewhere in the source: the key now resolves to two tasks; phase 2 stops; no guess.
5. A destination publication that raises `OSError` after its rename: the source must not be touched; the outcome is `publication_uncertain`, naming both completing calls.
6. A vault reassignment between the two publications: the source publication is refused by `_confirmed_publication`, and the outcome is partial, never `nothing_written`.
7. Two opposite moves `A → B` and `B → A` released together, 50 rounds: no deadlock (no wait while holding a lock), no loss; every task ends in exactly one note or in two with a `task_move_incomplete`.
8. Two moves of the same task to two different destinations at once: no loss; one success and one `task_move_incomplete (source_missing)` that never says the source still holds the task.
9. Two moves of the same task to the same destination at once: the second is refused (`duplicate_task` on its re-read, or `source_missing`); the destination never holds three copies.
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
31. The process is stopped between the two publications (simulated): the next pass indexes both copies, a plain retry writes nothing, and `resume=true` completes.
32. A move into an excluded folder of the default scope: the scope note appears; the write is unchanged.
33. The usage row never holds task text, for a success, a partial and every refusal.
34. **The superseded destination (Codex spec r1 #1).** After the destination publication, an outside editor saves an older destination without the moved lines; the destination refresh reports `superseded`. The source must not be written: the 6(b) check fails and the outcome is `task_move_incomplete` (`destination_changed`), with the source unchanged and the `complete with:` call re-inserting the task.
35. The same, but the older save lands between the 6(b) check and the source rename (an injected hook inside the publication callback): L17's window, reproduced and documented, not claimed safe.
36. A line inserted at the top of the destination between the two publications: the check fails (L18) and the move stops with a duplicate, never a loss.
37. **Independent identical tasks (Codex spec r1 #2).** `Inbox.md` and `Project.md` each hold an independent `- [ ] Call Alex` with identical context, before any move. `move_task` from `Inbox.md` to `Project.md` is refused `duplicate_task` and writes nothing; `allow_duplicate=true` leaves two copies in `Project.md`; only `resume=true` removes the inbox copy.
38. `resume=true` when the destination holds zero, or two, identical copies: `resume_unmatched`, nothing written. `resume=true` with `allow_duplicate=true`, with `to_section`, or with `to_path == path`: `invalid_argument`.
39. **The index lag (Codex spec r1 #3).** The destination's row lock is held for 3 s: under Q10 (a) the move completes, the response names `index (destination): may lag (lock_timeout)`, and `list_tasks` shows the task in neither note until the next pass, after which it shows it in the destination; under Q10 (b) the move stops with `destination_index_lagged` and the source unchanged.
40. **Block-id collision (Codex spec r1 #7).** The moved task ends in `^followup` and the destination already holds another block ending in `^FollowUp`: `block_id_conflict` naming the id, nothing written in either note.
41. **End of file (Codex spec r1 #5).** Destination `Intro` (no newline) receives `- [ ] x`: exactly `Intro\n- [ ] x`. Source `Intro\n- [ ] x` (no final newline) loses the task: exactly `Intro`. Source `- [ ] x` alone: the empty file. The verifiers accept exactly these terminator changes and refuse any other.
## Test plan

**Unit (pure), `tests/test_task_move_plan.py`:**

- re-framing (D4) over every frame pair: none ↔ none, none ↔ `> `, indented ↔ none, tabbed, nested quotes; blank-line rules; the refusals;
- placement (D5) as `add_task`'s cases, with the marker kept;
- terminators (D6) and the end-of-file rules for both the removal (D7) and the insertion;
- `verify_move_insert` and `verify_removal`: each check fails on a hand-built bad plan;
- D9: (a) with an identical and a differing block, (b), a done task, `allow_duplicate`, `resume` with zero, one and two identical copies, the forbidden combinations;
- the block-id collision check;
- `destination_still_holds` over the D10 6(b) cases (unchanged, superseded by an older save, a line inserted above);
- `plan_relocated_removal`: unchanged block, changed block, absent key, duplicated key;
- same note (D13): above, below, no change, heading in block;
- ids (D1): keys unchanged, ordinals renumbered on both sides;
- a property test over a corpus of notes: for every task, the two-note move's two new texts together contain every original line of both notes exactly once (moved lines re-framed), and the same-note move is a permutation of lines;
- the existing `tests/test_task_write.py` runs unmodified.

**Wiring (offline), `tests/test_task_move_e2e.py`:** every row of the D10 table, by monkeypatching `write_file_at` / `_confirmed_publication` to conflict, raise or refuse at each publication; the response and sentinel of each outcome; the event and refresh order (destination before source); usage params and result fields; dry run publishes nothing; the D12 precondition matrix; for every partial outcome, the `destination:` / `source:` lines say only what was verified and the `complete with:` call, when then made, completes the move; the presence check runs inside the publication callback with no `await` before the source write (asserted by a hook that mutates the destination before the callback, which must stop the move).

**Integration (`tests/integration/test_task_move_pg.py`, real Postgres):** list → move → list shows the task in the destination with its id and not in the source, with no pass; both refreshes; two `modified` events attributed to the caller; the opposite-moves race (scenario 7); a held row lock on one note; tenancy (user 8's identically named notes untouched); the next pass after a move leaves `tasks_source_hash = content_hash` on both notes.

**Surface:** every count in D17; the v3 oracle and the v2 derivation; the additions fixture; the read-only fixture unchanged; instructions ≤ 2,000 with the lead unchanged; description ≤ 4,000; `#295` unknown arguments refused on `move_task`; `TOOL_CLASSES` complete; `CLASS_MAPPING_VERSION` 7; the coverage and publishing-tool registries.

## Smaller first version

A version that covers ordinary inbox triage with materially less to get wrong (open question Q0). Everything not listed here is as in the full design.

**What it allows:**

- the moved task is a **top-level, unquoted** task: indentation width 0 and no blockquote prefix (no callout);
- its continuation block may hold plain indented lines and indented sub-tasks, but **no blockquote line, no fence line and no heading**;
- the destination region's anchor, when there is one, is also unquoted (with no anchor the task goes after the region's last non-blank line, as in D5);
- placement, `to_section`, same-note moves, `allow_duplicate`, `resume`, both hashes and `dry_run` are unchanged.

**What it refuses** (`task_malformed`, naming the reason, nothing written): a nested task, a quoted or callout task, a block with a quote or fence line, a quoted destination anchor. Each refusal says that `edit_note` can make the move by hand.

**What it removes from the design:**

- **D4 entirely.** Both frames are always empty, so the moved lines arrive byte for byte with no re-framing rule: the "starts with the frame" refusal, the blank-line rewrite (L6, L7) and Q1 / Q2 disappear;
- **the code-status refusal of D3** reduces to "no fence line in the block", checked by a line scan rather than by comparing masks across two notes;
- adversarial scenarios 10–13 and the frame-pair unit matrix.

**What it does not remove.** The D10 failure table is unchanged, row for row: the publication order, the presence check, the partial outcomes, `resume` and the L17 window are the same for a one-line task as for a nested one. The reviewer said so too: a smaller version "would not resolve the publication and retry findings". The saving is in the framing and refusal surface, not in the two-note machinery, which is where the real risk is.

A later change widens it to the full design by lifting the refusals and adding D4, with no change to the tool's arguments.

## Spec review record

**Codex spec review, round 1 (2026-11-12): FAIL**, 1 BLOCKER, 3 MAJOR, 3 MINOR. All seven resolved:

1. **BLOCKER, fixed.** A `superseded` destination refresh did not stop the source removal, so an outside save of an older destination could leave the task in neither note. The source write now runs only after the D10 6(b) presence check, inside the same synchronous publication callback; a failure stops with `destination_changed`. The window that remains (during the source write itself, and any outside save after the move) is L17; rows 8 and 17 and scenarios 34–36 cover it.
2. **MAJOR, fixed.** Automatic convergence deleted an independent identical task on the first call. Removed: an identical block at the destination is refused `duplicate_task` like any duplicate; completing a partial move is the explicit `resume=true` (exactly one identical copy, else `resume_unmatched`; not combinable with `allow_duplicate` or `to_section`). Every partial outcome names its completing call (D10, D11). Q4 and Q5 rewritten.
3. **MAJOR, made an owner question (Q10).** A lagging destination refresh followed by a successful source refresh hides the task from `list_tasks` until the next pass. (a) proceed and say so (recommended, the existing task-tool contract; the scenario is now conditional and L13 states the gap) or (b) stop with `destination_index_lagged`. The spec carries (a) with the (b) text marked.
4. **MAJOR, fixed.** The partial-outcome prose claimed the task was in both notes after an absent-key re-read. `source_missing` and `source_ambiguous` are now separate from `source_changed`, every outcome states only what was verified, and the completing call depends on the reason (D11 table).
5. **MINOR, fixed.** The two end-of-file terminator exceptions are stated in D6, D7, the verifiers (D8) and the spec scenarios, with exact bytes.
6. **MINOR, fixed.** D17 adds `tests/integration/test_scoped_tools_list_pg.py`, `tests/test_hybrid_search_offline.py`, `tests/test_semantic_rerank.py`, and on a re-run of the greps (method stated in D17) the `tool_permissions.py` comment, the README diagram and line 783, and `docs/architecture/oauth-and-grants.md`.
7. **MINOR, fixed.** A moved `^block` id that already exists in the destination is refused `block_id_conflict` before publication (D8).

The reviewer also judged Q3, Q5, Q7 and Q9 safe as recommended, Q1 and Q2 reasonable given the verifier and boundary tests, and Q8 reasonable once the end-of-file wording was truthful. It suggested the smaller first version above.

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
