## ADDED Requirements

### Requirement: `move_task` SHALL move one task, with its whole continuation block, resolved from the source note's current bytes

`move_task(path, id, to_path, to_section=None, allow_duplicate=False, resume=False, dry_run=False, expected_hash=None, to_expected_hash=None)` SHALL move the task that `id` names in the note `path`, together with its uncapped continuation block (every following line with a greater blockquote depth, or the same blockquote prefix and a greater indentation width, crossing blank lines), to the note `to_path`, which may equal `path`.

- The task SHALL be resolved exactly as `complete_task` resolves one: from the source's bytes read through the anchored descriptor that will be published to, with `task_not_found`, `task_ambiguous`, `task_changed`, the suffixed-id `precondition_required` before any read, and `size_limit` over the per-note task cap. No line number from the index or a listing SHALL be used.
- Any checkbox SHALL be movable whatever its status (open, done, cancelled or a custom marker) and whatever the caller's task rule.
- A task that has sub-tasks SHALL move with its whole subtree. A task nested inside another task's continuation block SHALL move with its own subtree, leaving its parent and siblings in place. A task line SHALL never be moved without its continuation block.
- `to_path` SHALL name an existing note; `move_task` SHALL never create one.
- The moved lines SHALL NOT be edited: no field, tag, status, rule text, block link or trailing whitespace SHALL change, and no global filter text SHALL be added.

#### Scenario: A task moves with its context and sub-task
- **WHEN** `Inbox.md` holds `- [ ] Call the bank 📅 2026-11-20`, then `    - ask about the fee`, then `    - [ ] find the account number`, and `move_task(path="Inbox.md", id=<id>, to_path="Projects/Money.md")` runs
- **THEN** all three lines SHALL be in `Projects/Money.md`, byte for byte, and none of them SHALL remain in `Inbox.md`

#### Scenario: A done task is moved as it is
- **WHEN** `move_task` names `- [x] Paid rent 🔁 every month 📅 2026-11-01 ✅ 2026-11-02`
- **THEN** the line SHALL arrive unchanged, no next occurrence SHALL be written, and the rule text SHALL NOT be parsed

#### Scenario: A nested task leaves its parent in place
- **WHEN** `- [ ] Project` is followed by `    - [ ] step one` and `    - [ ] step two`, and `move_task` names `step one`
- **THEN** only `    - [ ] step one` SHALL leave the source, `- [ ] Project` and `    - [ ] step two` SHALL stay, and `step two` SHALL still belong to `Project`

#### Scenario: A missing destination is never created
- **WHEN** `to_path` names no note
- **THEN** the call SHALL be refused with `not_found`, no file SHALL be created and the source SHALL be unchanged

### Requirement: The moved lines SHALL be re-framed to the destination's top level by one byte-exact prefix rule, or the move SHALL be refused

Every moved line SHALL be re-framed by one prefix substitution, or the move SHALL be refused.

Let the source frame be the task line's text before its list marker (its blockquote prefix and indentation), and the destination frame be the blockquote prefix of the destination anchor (empty without one). Every line of the moved block that starts with the source frame SHALL become the destination frame followed by the rest of the line. A blank line that does not start with the source frame SHALL become the destination's blockquote prefix with trailing spaces and tabs removed. Any other line SHALL make the move refused with `task_malformed`, writing nothing in either note.

When the two frames are equal, the moved lines SHALL arrive byte for byte. Each line's list marker SHALL be kept as written, and nothing SHALL be renumbered.

**Placement** SHALL be `add_task`'s: in the `to_section` body (resolved with `add_task`'s selectors and refusals) or the whole body, after the full continuation block of the last task whose indentation after its blockquote prefix is zero (never a task of the moved block); with no such task, after the region's last non-blank line; in an empty region, after the heading or at the body's start.

**Terminators.** Every moved line SHALL end with the destination's terminator: that of the line it is inserted after, else the note's first terminator, else LF. A file without a final terminator SHALL keep none, at the destination and at the source. Exactly two pre-existing terminators SHALL change, and only in these cases:

- an insertion at the end of a destination with no final terminator SHALL add the terminator to the old last line and leave the last moved line unterminated;
- a removal of a block that ends a source with no final terminator SHALL remove the terminator of the line before the block, which becomes the unterminated last line.

An insertion into an empty body SHALL join the moved lines with the terminator and add none at the end.

#### Scenario: A plain task arrives byte for byte
- **WHEN** a top-level task with a two-line context block moves to a note whose tasks are top-level and unquoted
- **THEN** the three inserted lines SHALL be byte-identical to the three removed ones

#### Scenario: A sub-task becomes a top-level task
- **WHEN** a task indented four spaces under a parent, with a child indented eight spaces, moves to a note with no tasks after a paragraph
- **THEN** it SHALL arrive at indentation 0 with its child at indentation 4, and both SHALL re-extract as tasks

#### Scenario: A callout task moves to a plain note and back
- **WHEN** `> - [ ] Renew passport` followed by `>     - photo booked` moves to a plain note, and a plain task moves into a note whose last top-level task is `> - [ ] a`
- **THEN** the first SHALL arrive as `- [ ] Renew passport` and `    - photo booked`, and the second SHALL arrive with the prefix `> ` on every line

#### Scenario: A mixed-indentation block is refused
- **WHEN** the task line is indented with two spaces and a line under it is indented with a tab
- **THEN** the move SHALL be refused with `task_malformed` and neither note SHALL change

#### Scenario: Terminators follow the destination
- **WHEN** a task in a CRLF note moves into an LF note
- **THEN** the moved lines SHALL end in LF, and every other byte of both notes SHALL be unchanged

#### Scenario: End of file without a newline, at both ends
- **WHEN** the source is exactly `Intro` LF `- [ ] x` (no final newline), the destination is exactly `Notes` (no final newline), and `x` is moved
- **THEN** the source SHALL be exactly `Intro`, and the destination SHALL be exactly `Notes` LF `- [ ] x`, neither with a final newline

#### Scenario: The only task of a note, into an empty note
- **WHEN** the source is exactly `- [ ] x` and the destination is the empty file
- **THEN** the source SHALL be the empty file and the destination SHALL be exactly `- [ ] x`

#### Scenario: Terminated files keep every terminator
- **WHEN** the source is `Intro` LF `- [ ] x` LF and the destination is `Notes` LF
- **THEN** the source SHALL be `Intro` LF and the destination `Notes` LF `- [ ] x` LF

### Requirement: Every `move_task` plan SHALL be verified before it is shown or published, and a block that cannot move cleanly SHALL be refused

The move SHALL be refused with `task_malformed`, writing nothing in either note, when the moved block contains an ATX heading line, or when removing or inserting it would change the code status of any line that is not moved.

The move SHALL be refused with `block_id_conflict`, writing nothing in either note and naming the id and the destination line, when a moved line carries a `^block` id that, compared case-insensitively, already ends a line in the destination's body. A move within one note SHALL NOT run this check.

Before a dry-run answer and before each publication, the plan for that note SHALL be verified from the old and new texts:

**Destination:**
1. the new text SHALL be the old text with exactly one whole-line insertion; every pre-existing line SHALL keep its content and its terminator, except that, for an insertion at the end of a destination without a final terminator, the old last line SHALL gain exactly the inserted terminator;
2. the inserted lines SHALL equal the re-framed block;
3. every pre-existing task SHALL re-extract identically, shifted by the inserted count;
4. the task set SHALL gain exactly the moved tasks, each re-extracting with the same text, key, status, dates, priority, tags and recurrence as at the source;
5. no pre-existing line SHALL change its owning task, and each moved line SHALL be owned as planned;
6. code status, headings and the frontmatter block SHALL be unchanged for every pre-existing line, and each moved line's code status SHALL equal its source code status.

**Source:**
1. the new text SHALL be the old text with exactly the moved lines removed; every remaining line SHALL keep its content and its terminator, except that, when the block ended a source without a final terminator, the line before the block SHALL lose exactly its terminator;
2. every remaining task SHALL re-extract identically, shifted by the removed count;
3. the task set SHALL lose exactly the moved tasks;
4. no remaining line SHALL change its owning task;
5. code status, headings and the frontmatter block SHALL be unchanged.

Any other terminator change SHALL fail verification. A result over `MAX_NOTE_BYTES` or over the per-note task cap, in either note, SHALL be refused with `size_limit` before anything is published. A defective frontmatter block in either note, or an unmatched indented fence with `to_section`, SHALL be refused with `content_unsafe`.

#### Scenario: A heading under the task is refused
- **WHEN** a task's continuation block contains the line `  # Notes`
- **THEN** the move SHALL be refused with `task_malformed` and neither note SHALL change

#### Scenario: A fence that closes outside the block is refused
- **WHEN** a fence opens on an indented line under the task and its closer is a line after the block
- **THEN** the move SHALL be refused, and neither note SHALL change

#### Scenario: A block id that already exists at the destination is refused
- **WHEN** the moved task ends in `^followup` and `Project.md` already holds another line ending in `^FollowUp`
- **THEN** the move SHALL be refused with `block_id_conflict` naming `followup` and that line, and neither note SHALL change

#### Scenario: A failed verification writes nothing
- **WHEN** a plan is altered in a test so that one destination line other than the inserted ones changes, or so that a pre-existing terminator outside the two end-of-file cases changes
- **THEN** the verifier SHALL refuse it with `task_malformed` and nothing SHALL be published in either note

### Requirement: `move_task` SHALL publish the destination first and SHALL write the source only after re-checking that the destination still holds the moved lines

The destination insert SHALL be published first, through the confirmed, descriptor-anchored publication path with the in-call conflict check. The source removal SHALL be attempted only after the destination publication returned success, or, with `resume=true`, after the destination was found to hold exactly one byte-identical copy.

The source removal SHALL be one confirmed publication whose synchronous step performs, with no suspension point between them: the root confirmation; **the destination presence check**, which re-reads the destination through its anchored target and requires the lines at the position where the moved lines were inserted (or found) to have exactly their contents; and the source write, with `expected=` the source bytes the removal was planned against. When the presence check fails, the source SHALL NOT be written. A destination task refresh that reports `superseded` SHALL never be followed by a source write unless that check, run afterwards, passes.

#### Scenario: An older destination saved between the steps stops the move
- **WHEN** after the destination publication an outside editor saves an older `Project.md` without the moved lines, so the destination refresh reports `superseded`
- **THEN** `Inbox.md` SHALL be byte-identical, and the answer SHALL be `task_move_incomplete` with `reason` `destination_changed`, whose `destination:` line says the destination no longer holds the moved lines

#### Scenario: A line added above the inserted lines stops the move
- **WHEN** after the destination publication another writer inserts a line at the top of `Project.md`
- **THEN** `Inbox.md` SHALL be byte-identical, and the answer SHALL be `task_move_incomplete` with `reason` `destination_changed`

#### Scenario: Nothing is removed when the destination did not publish
- **WHEN** the destination publication is refused, or raises an OS error
- **THEN** `Inbox.md` SHALL be byte-identical, and the answer SHALL be the refusal, or `publication_uncertain` without `nothing_written`

### Requirement: Every failure after the destination publication SHALL be a named partial outcome that states only what the tool verified and names the call that completes the move

- **Before the destination publication**, every refusal SHALL leave both notes byte-identical and carry `nothing_written: true`. A destination in-call conflict without `to_expected_hash` SHALL repeat the whole sequence from reading both notes, at most 3 attempts, then refuse with `concurrent_write`, nothing written.
- **An OS error from the destination publication** SHALL leave the source untouched and SHALL be answered `publication_uncertain` as a partial outcome, saying the destination may hold the task and naming both completing calls (`resume=true` if `read_note` shows the lines there, the plain call otherwise).
- **After the destination publication**, a stop SHALL be answered with the code `task_move_incomplete`, a partial outcome that SHALL NOT carry `nothing_written`. Its prose SHALL carry a `destination:` line and a `source:` line, each stating only what this call verified, and a `complete with:` line naming the exact call that completes the move. It SHALL NOT say the source holds the task unless a re-read in this call found it there. Its `MCP-REFUSAL` line SHALL carry `path` (the source), `destination_path`, `source_id`, `destination_id` (absent for `destination_changed`) and `reason`.
- **Reasons and their completing calls:**

| `reason` | when | `complete with:` |
|---|---|---|
| `destination_changed` | the presence check failed | the same call without `resume`; if that is refused `duplicate_task`, the same call with `resume=true` |
| `concurrent_write` | the source changed during the call and no retry was allowed or the retries ran out | the same call with `resume=true` (and a fresh `expected_hash` if one was given) |
| `source_missing` | a re-read found no task with the id's key | none if it was removed or moved; if its text was edited, `list_tasks(path=…)` for its new id, then the call with that id and `allow_duplicate=true` |
| `source_changed` | a re-read found the key once with a different block | the call with the current id and `allow_duplicate=true` |
| `source_ambiguous` | a re-read found the key on several tasks | `read_note` of the source, then the call with a suffixed id, `resume=true` and `expected_hash` |
| `vault_assignment_changed`, `vault_anchor_lost_at_publish`, `vault_confirmation_unavailable` | the source publication was refused by the confirmation | the same call with `resume=true` once the assignment is restored |
| `io_failure` | the source write raised an OS error | `read_note` of the source; if it still holds the task, the same call with `resume=true` |

- **Source retry.** When the source write meets an in-call conflict and no `expected_hash` was supplied, the tool SHALL re-read the source, re-derive its tasks, and write the removal again (through the same presence check) only if the moved task's key resolves to exactly one task whose block is byte-identical to the block that was moved; at most 3 attempts. With `expected_hash` supplied it SHALL NOT retry.

#### Scenario: The source removal conflicts
- **WHEN** another writer changes `Inbox.md` between the destination publication and the source removal of a move called with `expected_hash`
- **THEN** the task SHALL be in both notes, the other writer's bytes SHALL be on disk unchanged, and the answer SHALL be `task_move_incomplete` with `reason` `concurrent_write`, both paths and both ids, a `complete with:` line naming `resume=true`, and no `nothing_written`

#### Scenario: A context line added in the source is kept
- **WHEN** another writer adds a context line under the task in `Inbox.md` after the destination publication of a move called without `expected_hash`
- **THEN** the task and its new context line SHALL remain in `Inbox.md`, and the answer SHALL be `task_move_incomplete` with `reason` `source_changed`, naming the current id and the `allow_duplicate=true` call

#### Scenario: A task completed in the source between the steps is reported missing, not present
- **WHEN** another writer completes the task in `Inbox.md` (its text gains a done date) after the destination publication of a move called without `expected_hash`
- **THEN** the completed line SHALL remain in `Inbox.md`, and the answer SHALL be `task_move_incomplete` with `reason` `source_missing`, whose `source:` line does not say that `Inbox.md` holds the task

#### Scenario: Two concurrent moves of one task to different notes
- **WHEN** moves of one task from `A.md` to `B.md` and to `C.md` run at once and both publish their destination
- **THEN** one SHALL succeed, the other SHALL answer `task_move_incomplete` with `reason` `source_missing` without claiming `A.md` holds the task, and no line SHALL be lost

#### Scenario: A reassignment between the steps is partial
- **WHEN** the caller's vault assignment changes after the destination publication
- **THEN** the source publication SHALL be refused by the confirmation, and the answer SHALL be `task_move_incomplete` with `reason` `vault_assignment_changed` and a `complete with:` line naming `resume=true`

### Requirement: An identical copy at the destination SHALL be refused as a duplicate, and a partial move SHALL be completed only by an explicit `resume`

Before planning an insertion, the destination's current bytes SHALL be checked, never the index. The move SHALL be refused with `duplicate_task`, writing nothing in either note and naming up to 5 matches, when the destination holds a task with the moved task's key, whatever its block, a byte-identical block included, or, when the moved task is open, an open task with the same normalised description (`add_task`'s rule), unless `allow_duplicate=true`. When a match is byte-identical to the moved block, the refusal SHALL say so and name both `allow_duplicate=true` and `resume=true`. `allow_duplicate=true` SHALL only ever add lines.

`resume=true` SHALL mean only: the destination already holds this exact block; insert nothing and remove the source copy. It SHALL require exactly one destination task with the moved key whose block, with frames removed, is byte-identical to the moved block, and SHALL otherwise be refused with `resume_unmatched`, writing nothing. It SHALL then write the source removal through the destination presence check aimed at that copy. It SHALL NOT be combinable with `allow_duplicate`, with `to_section`, or with a move within one note.

No sequence of `move_task` calls without `allow_duplicate` SHALL leave more than two copies of a moved block, and no call SHALL remove a source line whose bytes, re-framed, are not present at the destination at the moment of the presence check.

#### Scenario: An independent identical task at the destination is not deleted
- **WHEN** `Inbox.md` and `Project.md` each hold an independent `- [ ] Call Alex` with identical context, before any move, and `move_task` moves the inbox task to `Project.md`
- **THEN** it SHALL be refused with `duplicate_task`, saying the copy is byte-identical, and neither note SHALL change

#### Scenario: A plain retry after a partial outcome writes nothing
- **WHEN** a move ended in `task_move_incomplete` with `reason` `concurrent_write`, and the same call is made again without `resume`
- **THEN** it SHALL be refused with `duplicate_task`, and neither note SHALL change

#### Scenario: Resume completes a partial move
- **WHEN** a move ended in `task_move_incomplete` with `reason` `concurrent_write`, the source task is unchanged, and the same call is made with `resume=true`
- **THEN** nothing SHALL be inserted in the destination, the source copy SHALL be removed, and the response SHALL say the move was resumed, naming the destination line

#### Scenario: Resume without exactly one copy is refused
- **WHEN** `resume=true` is passed and the destination holds no byte-identical copy, or two
- **THEN** it SHALL be refused with `resume_unmatched`, and neither note SHALL change

#### Scenario: An open duplicate at the destination is refused
- **WHEN** `Projects/Money.md` holds `- [ ] call the  Bank` and `move_task` moves `- [ ] Call the bank` there
- **THEN** it SHALL be refused with `duplicate_task` naming that line, and with `allow_duplicate=true` the task SHALL move

#### Scenario: A done task into an archive is not refused for its description
- **WHEN** `Archive.md` holds `- [x] Water plants ✅ 2026-10-01` and `move_task` moves `- [x] Water plants ✅ 2026-11-01` there
- **THEN** the task SHALL move

### Requirement: `move_task` SHALL bind each note by its own precondition, with the task tools' required-mode rules

`expected_hash` SHALL bind the source note's whole bytes and `to_expected_hash` the destination's, each compared by the existing precondition ladder immediately after that note's in-call read, with the existing codes. A `stale_precondition` refusal SHALL name the note whose hash differed.

When the deployment requires preconditions, a bare id SHALL satisfy the requirement for the source, a suffixed id SHALL always require `expected_hash`, and the destination SHALL be exempt, as for `add_task`. A supplied hash SHALL still be compared.

When `path` and `to_path` resolve to the same note, `to_expected_hash` SHALL be absent or equal to `expected_hash`, and `resume` SHALL be false, else the call SHALL be refused with `invalid_argument`. `resume=true` combined with `allow_duplicate=true` or with `to_section` SHALL be refused with `invalid_argument` before any read. When they name the same file through two different vault paths (equal device and inode), the call SHALL be refused with `invalid_argument` before any publication.

#### Scenario: A stale destination hash writes nothing
- **WHEN** `to_expected_hash` does not match the destination
- **THEN** the call SHALL be refused with `stale_precondition` naming `to_path`, and neither note SHALL change

#### Scenario: Required mode with a bare id
- **WHEN** `WRITE_PRECONDITION_REQUIRED=true` and `move_task` is called with a bare id and no hashes
- **THEN** it SHALL proceed

#### Scenario: Two paths to one file are refused
- **WHEN** `path` and `to_path` are two hard links to one file
- **THEN** the call SHALL be refused with `invalid_argument`, and the file SHALL be unchanged

### Requirement: A move within one note SHALL be one plan, one verification and one publication

When `to_path` resolves to the source note, the removal and the insertion SHALL be planned over the original text as one set of non-overlapping spans, with the insertion point computed on the note without the moved block. The plan SHALL be verified once (every line outside the block unchanged, every non-moved task identical, the task set unchanged, each moved task re-extracting identically, no non-moved line changing owner, code status and headings unchanged) and published once, with one refresh and one `modified` event. A plan that leaves the note unchanged SHALL answer that there were no changes, with nothing written, no event and no refresh. No partial outcome SHALL be possible.

#### Scenario: Moving a task to another section of its note
- **WHEN** `Inbox.md` has sections `Today` and `Later`, and `move_task(path="Inbox.md", id=<id of a Today task>, to_path="Inbox.md", to_section="Later")` runs
- **THEN** the task and its block SHALL be after `Later`'s last top-level task, one publication SHALL have occurred, and one `modified` event SHALL be recorded

#### Scenario: A task already in place
- **WHEN** the task is the last top-level task of the section named by `to_section` in its own note
- **THEN** the response SHALL say there were no changes, and nothing SHALL be written

### Requirement: `move_task` SHALL share the task write envelope, refresh and record each note it published, and take one write charge

`move_task` SHALL:

- be wrapped by `_tracked` with the write class, be named in `WRITE_REQUIRED_TOOLS`, require write permission, and be charged **one** write-bucket token and one quota slot per call, however many publications it makes;
- hold one `write` concurrency slot for the whole call;
- after each confirmed publication, refresh that note's task rows with the write-time refresh, then record one `modified` change-feed event for that note with `tool = 'move_task'`, the destination's before the source removal is attempted;
- never hold two database locks at once: each refresh SHALL run in its own transaction holding at most one `notes_metadata` row lock, ended before the next begins;
- report each refresh as `index (destination): …` and `index (source): …` (one `index:` line for a same-note move); a source refresh that does not report `refreshed` SHALL never fail the move, and a destination refresh that does not report `refreshed` SHALL NOT stop the source removal (open question Q10 (a); under Q10 (b), it SHALL stop the move before the source is written, with `task_move_incomplete` and `reason` `destination_index_lagged`);
- log `path`, `id`, `to_path`, `to_section`, `dry_run`, `allow_duplicate`, `resume` and the presence of each hash, and never task text;
- with `dry_run=true`, return both diffs and the moved lines, publishing nothing, refreshing nothing and recording no event.

The caller's task rule SHALL NOT be consulted for the move. When `to_path` lies under a folder the caller's default task scope excludes and the source does not, the response SHALL say that default listings will not show the task and that `list_tasks(scope="all")` will.

#### Scenario: List, move, list
- **WHEN** `list_tasks` shows a task in `Inbox.md`, `move_task` moves it to `Projects/Money.md` and reports `index (destination): refreshed` and `index (source): refreshed`, and `list_tasks` is called before any index pass
- **THEN** the task SHALL be listed in `Projects/Money.md` with its id, and not in `Inbox.md`

#### Scenario: A lagging destination refresh is reported, not hidden
- **WHEN** another transaction holds the destination's row lock for longer than 2 seconds during a move
- **THEN** the move SHALL complete, the response SHALL say `index (destination): may lag (lock_timeout)`, and the next index pass SHALL list the task in the destination (open question Q10 (a); under Q10 (b) this scenario becomes: the source SHALL be unchanged and the answer SHALL be `task_move_incomplete` with `reason` `destination_index_lagged`)

#### Scenario: Two events, attributed
- **WHEN** an API key moves a task between two notes
- **THEN** two `modified` events SHALL be recorded with `tool = 'move_task'`, the destination's first, each carrying the canonical hash of the bytes that note published

#### Scenario: Opposite moves do not deadlock
- **WHEN** a move from `A.md` to `B.md` and a move of another task from `B.md` to `A.md` are released together, repeatedly
- **THEN** every call SHALL finish, and every task SHALL be in exactly one note, or in two with a `task_move_incomplete` answer

#### Scenario: One write token per move
- **WHEN** a principal with a write burst of 2 makes two two-note moves
- **THEN** both SHALL be admitted by the write bucket

### Requirement: The `move_task` response SHALL name the moved task's new line and id, and say when other ids changed

A success SHALL report the destination line of the moved task and its id, the number of lines and tasks moved, the lines removed from the source, each published note's `content_hash`, and each refresh result. Every moved task SHALL keep its key. The id SHALL be bare when the destination holds one task with that key after the move, and suffixed otherwise. When a task in either note that shares a key with a moved task has its ordinal suffix renumbered by the move, the response SHALL say so. When the moved task was nested, the response SHALL say under which source line. When moved lines carry `^block` ids, the response SHALL name them and say that links to them in the source note no longer resolve (a collision with an id already in the destination is refused before publication). Every printed path SHALL be escaped as every task tool escapes one.

#### Scenario: The id survives the move
- **WHEN** a task with the bare id `3f9a0c1b2d4e` moves to a note holding no task with that key
- **THEN** the response SHALL give its id as `3f9a0c1b2d4e`, and `complete_task(path=<destination>, id="3f9a0c1b2d4e")` SHALL resolve it

#### Scenario: A remaining sibling loses its suffix
- **WHEN** `Inbox.md` holds two identical `- [ ] Call` lines, and `move_task` moves `<key>.1of2` with a matching `expected_hash`
- **THEN** the remaining line's id SHALL be the bare key, and the response SHALL say that suffixed ids sharing the moved key were renumbered in `Inbox.md`
