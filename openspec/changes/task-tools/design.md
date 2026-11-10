## Context

### What the code does today (verified on `origin/task-tools` at 16e4a52)

- **Extraction.** `src/services/tasks.py` `extract_tasks_bounded(body, line_offset)` masks the body (`mask_code`, `BODY`) and splits lines by the universal-newline rule. A line is a task when the masked line matches `_CHECKBOX_RE` at its start and the original equals the masked text through `]` (#218).
  - Text is the original after `]` and its following `[ \t]*`, `rstrip()`ed, cut at 1,000.
  - Fields come from the masked span, and the **last occurrence anywhere** wins (#350 L2).
  - At most 5,000 tasks are kept per note (`tasks_truncated`).
  - Any grammar or cap change is a `CURRENT_EXTRACTION_VERSION` bump (now 4, `indexer.py:789`).
- **Derivation.** `_update_tasks_for_changed` (`indexer.py:4932`) runs after the link rebuild for `changed_for_derivation` (the upserted paths plus the moved paths), inside the pass transaction. That transaction took the global generation lock (`acquire_generation_lock_unbounded`, `indexer.py:3231`) before any row lock.
  - It works per note, in a savepoint: delete, insert, then the flags. A failure gets one retry, and then `tasks_failed` is set. The note is never poisoned.
  - Bodies come only from the scan buffer (`path_to_content`).
  - A note in the changed set has already had its `notes_metadata` row upserted or move-updated in this transaction, so the pass **holds that row's lock** when it rewrites the note's tasks.
- **Stat shortcut.** The scan skips the read when `(size, mtime_ns, ctime_ns, inode)` matches the stored stat. A NULL stat (racy) forces a read (`STAT_RACY_WINDOW_NS`, `indexer.py:2082`). The changed set is hash-based: an unchanged hash is not upserted.
- **#350 D9.** It declined re-deriving inside `edit_note`, "because the write tools do not index today and adding a derivation writer outside the pass would break D1's 'written only with the row'". D1's invariant is that task rows always describe the row's committed `content_hash`, because they are written in the same transaction as the upsert that wrote it.
- **Write path** (`edit_note_impl`, `tools.py:5303`). It runs `_require_write`, then the `expected_hash` syntax check, then `open_mutable` (a descriptor-anchored target). Then come `read_bytes_at` under the cap, the `_precondition_error` ladder (immediately after the read; required mode is `settings.write_precondition_required`), the mode logic, `_note_size_error`, `dry_run` (a unified diff), and `_confirmed_publication(uid, lambda c: write_file_at(target, new, expected=existing_bytes, confirmation=c))`.
  - An in-call conflict is reported as `concurrent_write`.
  - After publication it calls `_record_note_events(uid, [_content_event("modified", …)])`, which swallows every failure, then `_published_hash_clause`.
- **Section addressing.** `replace_section`, the heading selectors and `_scan_headings` are in `vault.py`. A section write is refused on a defective frontmatter block or on an unmatched indented fence (`_frontmatter_defect_error`, `_unmatched_fence_error`).
- **Registration.** `server.py` has the four annotation shapes (`_READ_ONLY`, `_ADDITIVE`, `_DESTRUCTIVE`, `_DESTRUCTIVE_OPEN_WORLD`), 28 registrations with `list_tasks`, `get_changes` and `hybrid_search` last, and `SERVER_INSTRUCTIONS` (≤ 2,000 characters, 512-character lead).
  - `tool_permissions.WRITE_REQUIRED_TOOLS` names nine tools.
  - `concurrency.TOOL_CLASSES` has eight `write` tools, `list_tasks` in `scan`, and `CLASS_MAPPING_VERSION = 5`.
- **Fixtures.**
  - `tools_list_schema_5a391ca.json` (frozen, the first 25 tools);
  - `tools_list_schema_amendments.json` (`list_notes`, `get_recent`, `semantic_search`);
  - `tools_list_schema_additions.json` (`list_tasks`, `get_changes`, `hybrid_search`);
  - `tools_list_full_v1.json` (the byte-identity 1.0 oracle, 28 tools);
  - `tools_list_readonly_oauth.json` (19 tools).
  
  Counts 28 and 19 are pinned in `test_issue_343_tool_metadata.py`, `test_issue_358_scoped_tools_list.py`, `test_issue_295_unknown_arguments.py`, `test_issue_263_coverage_registry.py`, `test_issue_261_controller.py`, `test_issue_261_tool_admission.py`, `test_body_budget_config.py`, `test_tei_identity.py`, `test_search_confidence.py` and `test_transfer_overlap_gate.py`. Re-grep before finishing.
- **Panel.**
  - Single-user mode has no `users` row: the panel identity is a sentinel, and `/account` 404s (`_require_account_route`). MCP callers there have `current_user_id = None` (the NULL owner scope).
  - The Settings page (`settings.html`, admin) exists in both modes.
  - The `page_account_*` goldens live in `tests/golden/oidc_off/`.
  - Every template uses a nonce CSP with no inline handlers or styles.
- **Dependencies.** No `python-dateutil` and no `tzdata` in `requirements.txt`.

### Tasks-plugin behaviour

The source is `references/tasks-plugin-reference.md` in this change: Tasks 8.6.0 at a pinned commit, cited below as **R§n**. It is authoritative for field symbols (R§1.2), serialisation order (R§1.3), the backward parse (R§1.4), recurrence (R§2) and the settings defaults (R§3).

## Goals / Non-Goals

**Goals:**

1. One verb per task operation, symmetric descriptions, nothing ranked.
2. A write can change only the line its id names, refuses rather than guesses, and never reformats.
3. A recurring task's series never ends silently and never gets a wrong date. Anything outside the verified grammar is refused.
4. No listing silently omits or wrongly includes a task.
5. Read-your-writes, without weakening the index's convergence.

**Non-goals:** see `proposal.md`.

## Decisions

### D1. The task id: a stored key over the uncut text, plus an ordinal-of-count suffix

- **Key.** `task_key = sha256(b"omcp-task-v1\x00" + full_text.encode("utf-8")).hexdigest()[:12]`. `full_text` is exactly the extractor's uncut text: the original line after `]` and the following `[ \t]*`, with `str.rstrip()` applied. The status marker, indentation, list marker and blockquote prefix are excluded.
- **Ordinal.** Within one note's extracted task list (document order, the same 5,000 cap), `key_count` is the number of tasks with that key and `key_ordinal` is this task's 1-based rank among them.
- **Rendered id.**
  - `<key>` when `key_count = 1`, otherwise `<key>.<ordinal>of<count>`.
  - Grammar `^[0-9a-f]{12}(\.[1-9][0-9]{0,3}of[1-9][0-9]{0,3})?$`, at most 23 characters (`<12 hex>.9999of9999`). Any rendering bound that reserves room for an id uses 23.
  - The write tools accept both forms. `<key>.1of1` is equivalent to `<key>`.
- **Stored.** `note_tasks.task_key char(12)`, `key_ordinal int` and `key_count int` are written by the extractor (D11), so an id works for text beyond the 1,000-character cut and the listing needs no window query.
- **A suffixed id names an occurrence, not a line** (Codex r1 #2). Two identical lines can swap places, or one can be replaced, with the count unchanged, and the ordinal then silently names the other occurrence. A write with a suffixed id is therefore accepted only together with a whole-note `expected_hash` that matches the current bytes (D2/D3), in every deployment mode. The ordinal is then relative to exactly the bytes the caller read.

**Why the status is excluded.** A retry of `complete_task` on a task whose text did not change (the done-date setting is off) resolves and answers `already_completed` instead of `task_not_found`. With the done date on (the default), the text changes, so a retry with the old id is `task_not_found`. Either answer is safe, and a double completion is impossible.

**Why `path` + id** rather than an index lookup or an opaque token:

- The id is verifiable against the bytes, with no server state. The path scopes collisions to one note: 48 bits is ample there, and a collision only behaves like a duplicate (an ordinal is required, and resolution is still exact).
- An opaque token that encodes a note id would leak a database-global identifier (the #350 round-2 lesson). It would also need a MAC and state, and still could not prove the bytes are unchanged.
- An index lookup by line trusts a line number the index may hold stale, which the brief forbids.

**Rejected:**

- **An id over the line with the status included.** A retry would become `task_not_found` even with the done date off.
- **An id over the description only, with fields excluded.** Two lines differing only in their due date would share a key. Every date edit would then keep the same id while changing which line is "first", inviting confusion between siblings.

### D2. Resolution is bound to the bytes being replaced

Every write tool follows this order. Ordering is normative.

1. `_require_write`.
2. Argument validation (D16 `invalid_argument`), including the id grammar.
3. `_precondition_syntax_error` on `expected_hash`.
4. `open_mutable(path)` and `_leaf_state_error`.
5. `read_bytes_at` under `_note_precondition_cap()`.
6. The `_precondition_error` ladder, with the D3 enforceability.
7. UTF-8 decode.
8. `tasks_from_note_text(raw)`: the indexer's frontmatter split plus `frontmatter_line_offset` plus `extract_tasks_bounded`. It is the one shared function the pass and the refresh also use (D10). If the extraction is truncated, the call is refused with `size_limit`: the 5,000-task cap would make a count unverifiable.
9. Match the id: key equality, then the ordinal rules below.
10. Build the plan (D4–D8), then verify it (D5). Planning and verification run **off the event loop** (`asyncio.to_thread`), because a 5,000-task note takes about a second to plan. Every planner first refuses `content_unsafe` (detail `frontmatter_defect`) when the note's frontmatter diagnosis reports a defect. That diagnosis is the one `edit_note(section=…)` uses: line 1 is exactly `---`, and either no closing `---` follows or the YAML between them is invalid. `add_task(section=…)` also refuses `content_unsafe` (detail `unmatched_fence`) on an unmatched indented fence. Both reuse `edit_note`'s refusal messages. A plan whose `changed` is false means "No changes": nothing is published, and there is no event and no refresh.
11. `_note_size_error`.
12. `dry_run`: a diff of the old and new note plus the new line(s), with nothing published.
13. `_confirmed_publication(uid, lambda c: write_file_at(target, new, expected=existing_bytes, confirmation=c))`. An in-call conflict gives `concurrent_write`.
14. The task index refresh (D10).
15. `_record_note_events` (`modified`).
16. The response (D16).

The ordinal rules, given `n` = the number of tasks in the current bytes whose key equals the id's key:

| Id form | n | Outcome |
|---|---|---|
| `.<i>of<m>`, no `expected_hash` | any | `precondition_required`, checked at entry before any read. The prose says to `read_note` the note and pass its `content_hash` as `expected_hash` |
| any | `n = 0` | `task_not_found` |
| bare key | `n = 1` | resolves |
| bare key | `n > 1` | `task_ambiguous` (lists the `n` suffixed ids with their lines, and says a suffixed id needs `expected_hash`) |
| `.<i>of<m>` with a matching `expected_hash` | `m ≠ n` | `task_changed` (unreachable while the hash matches; kept as a belt) |
| `.<i>of<m>` with a matching `expected_hash` | `m = n` | the `i`-th |

A suffixed id with a non-matching hash is refused earlier, by the ladder (`stale_precondition`).

**No line number from the index or from an earlier read is trusted.** The line printed in a response is informational only.

**Listings do not print a precondition hash** (decided after checking the code). `notes_metadata.content_hash` is `canonical_note_hash`: SHA-256 of the newline-translated, NUL-stripped text (`src/services/note_hash.py`). The write ladder's `expected_hash` is `content_hash_for_bytes`: `sha256:` plus SHA-256 of the raw bytes (`vault.py:1863`). `is_canonical_content_hash` deliberately refuses the index digest. The two differ for every CRLF or NUL-bearing note, so the index cannot supply a valid precondition. A caller acting on a suffixed id calls `read_note` first. **Rejected:** storing a raw-byte hash in the index for this purpose. It would add a fourth hash to the pass for a case (identical sibling tasks) that one read already covers.

**Bounded internal retry on an in-call conflict** (supervisor addition B). An agent asked to complete three tasks commonly issues three calls on one note at once. When step 13 reports an in-call conflict **and the caller supplied no `expected_hash`**, the tool repeats steps 5–13 from the read, up to **3 attempts in total**: re-read, re-resolve the id from the new bytes, re-plan, re-verify, publish. Nothing (line number, offsets, plan or diff) is carried from one attempt to the next. After the third conflict it refuses `concurrent_write`.

With `expected_hash` supplied there is no retry: the caller bound a version, and a changed file is `stale_precondition` on the next read. Suffixed ids always carry a hash, so they never retry.

Retries run inside one `_tracked` call. The gates (buckets, admission, concurrency slot, quota) are passed once, and every attempt still takes its own `_confirmed_publication` (#88). Safety is unchanged: each attempt is a complete, independent resolution against bytes it read through the same anchored descriptor, so a retry can only do what a fresh call would do. The same task completed twice in parallel yields one success and one refusal: the second attempt re-resolves against the completed line and answers `already_completed`, or `task_not_found` once the text changed.

**Composition with the publish path.** Resolution, plan and diff are computed from `existing_bytes`, the bytes read through the anchored descriptor. Publication uses `write_file_at(…, expected=existing_bytes)`, which refuses when the file changed between this call's read and its rename (`concurrent_write`). So the bytes the id was re-derived from are exactly the bytes replaced. A concurrent change elsewhere in the note causes a refusal, never a lost edit. The caller simply retries.

### D3. `expected_hash` and deployments that require it

All three tools accept an optional `expected_hash`, which binds the whole file through the existing ladder, at the same position (immediately after the in-call read), with identical codes.

**Suffixed ids always need it.** In every mode, a `complete_task` or `update_task` whose id carries an ordinal suffix requires `expected_hash` (D1, D2). Without one, the call is refused `precondition_required` with nothing written.

**Required mode (`WRITE_PRECONDITION_REQUIRED=true`).** Owner decision, 2026-11-10.

- For `complete_task` and `update_task` with a **bare** id, **the `(path, id)` pair satisfies the requirement**. It is a content precondition on the exact line replaced, re-derived from the bytes the rename is bound to (D2). Every other byte is spliced from the current file, never from a caller-held copy. The hazard required mode exists to prevent, an unguarded write that overwrites newer content with a stale base, therefore cannot occur.
- `add_task` removes no byte. It is **exempt**, on the same reasoning that exempts a no-incumbent creation.
- The ladder still runs: a supplied hash is compared, and a mismatch is `stale_precondition`.

**Rejected:**

- **Requiring `expected_hash` for every task write under required mode.** A bare key names exactly one line in the bytes the rename is bound to. `list_tasks` rows carry no file hash, so every task write would need an extra `read_note`, reintroducing the read-then-write round trip these tools exist to remove, for no safety gain. The exception is suffixed ids, where the ordinal is positional and the hash is the binding.

### D4. The minimal one-line editor (`src/services/task_line.py`, pure)

**Supervisor ruling 1: minimal edit wins over the plugin's whole-line rebuild.** The plugin re-serialises the whole line on every edit (R§1.3): it puts fields in a fixed order, moves tags in front of signifiers, and canonicalises the rule text, so `every weekend` → `every week` loses meaning. The server does none of that. "As the plugin would write it" means **semantically**: the same dates, and the same fields kept or dropped.

**Rejected: rebuilding the line in the canonical order.** It reformats the user's line, changes rule text meaning, and makes every write a whole-line rewrite whose diff an agent cannot audit.

**Anatomy** of a task line `L` (the content between line start and terminator):

```
prefix  = the _CHECKBOX_RE match up to and including the marker's whitespace: bq/indent + marker + ws
box     = "[" status "]"
gap     = [ \t]*                       (after "]")
text    = the rest, through the last non-whitespace character
tail_ws = trailing whitespace          (a "hard break" of ≥ 2 spaces included)
```

Inside `text`, the **trailing run** is found exactly as the plugin parses it (R§1.4):

1. Strip a block link matching ` \^[a-zA-Z0-9-]+$`.
2. Repeat at most 21 times, removing end-anchored matches of: priority, done, cancelled, due, scheduled, start, created, recurrence (`🔁 *[a-zA-Z0-9, !]+`), onCompletion, a trailing `#tag`, id and dependsOn. Each pattern is `symbol + U+FE0F? + ' *' + value + '$'`, with values as in R§1.2.
3. What remains is the **description region**, and the removed suffix is the **run**.

The run is a sequence of tokens with their exact spans: fields, tags and the block link.

**Refusal preconditions** (`task_malformed`, nothing written):

- **(a) Masked and original text differ inside the run** (inline code there). Applies when the call adds, changes or removes any field, or adds the done date.
- **(b) Supervisor ruling 4, duplicated fields.** A family the call changes, removes or adds occurs more than once anywhere in `text`, counted on the masked text.
- **(c) A stranded signifier.** A family the call touches occurs once, but in the description region rather than the run. The plugin would not parse it and the extractor would, so the server will not pick.
- **(d) An invalid date** (not a calendar date) in a field the call changes, or in any date field when completing a recurring task.

**Rejected: picking one copy** (the extractor's last-wins or the plugin's leftmost-wins). Either choice silently diverges from one of the two readers.

**Operations.** All are byte-exact outside the touched token spans.

- **Change a field value.** Replace only the value characters: the 10-character date, the rule text, or the priority emoji for a priority change. The signifier as written (variant 📆 / 🗓 / ⌛, VS16) and its spacing are kept.
- **Remove a field.** Delete the token span plus exactly one adjacent separating space: the preceding one if present, otherwise the following one.
- **Add a field.** Insert `" " + symbol + " " + value`, using the plugin's write symbols: `📅 ⏳ 🛫 ➕ ✅ ❌ 🔁`, and priority `🔺⏫🔼🔽⏬`. Placement:
  - Let `C = [id, dependsOn, priority, recurrence, onCompletion, created, start, scheduled, due, cancelled, done]` (R§1.3).
  - If the run's field tokens, ignoring tags, appear in non-decreasing `C` rank, insert immediately before the first field token whose rank is greater than the new field's rank. If none is greater, insert after the last field token.
  - Otherwise, when the order is not determinable, insert at the end of the run.
  - In every case the insertion goes before a trailing block link and before `tail_ws`.
  - An empty run means the field goes at the end of `text`, before any block link.
- **Add a tag.** Insert `" #" + tag` at the end of the description region, so the plugin keeps it as description. A tag already present, compared case-insensitively on the whole tag, is a no-op.
- **Remove a tag.** Remove every unmasked token equal to `#tag`, compared case-insensitively on the whole tag and never hierarchically, with one adjacent space. An absent tag is a no-op.
- **Replace the description.** Replace the description region. The new text must:
  - be a single line, 1–1,000 characters, stripped;
  - contain no field signifier, no `^block` suffix and no line-break or format character (CR, LF, U+0085, U+2028, U+2029);
  - not make the line re-parse differently (D5);
  - not contain an odd number of `%%`, an unpaired `<!--` / `-->`, or a run of three or more backticks or tildes. A context line also may not start with a list marker, a checkbox, a `#` heading or `>`. Each of these is refused `invalid_argument`.

  **Tags survive a description replacement** (Codex r2 #1). `text` replaces the *words* of the description. Tags are a separately controlled field. Trailing tags are run tokens under the plugin's parse, so they are untouched anyway. Every tag token that was in the old description region and does not occur in the new text (whole tag, case-insensitive, unmasked) is re-appended at the end of the new description region, in its original order, each as `" #" + tag` spelled exactly as before. A tag the caller typed inside the new text stays where it was typed and is not duplicated. `[[links]]` and all other description text are ordinary words, and are replaced with the description.

  When one call combines `text` with tag changes, the order is: replace the description (with re-append), then `remove_tags`, then `add_tags`. Only `remove_tags` removes a tag.
- **Status.** Replace the one status character inside `box`.

**Preserved byte for byte:** `prefix` (indentation, list marker, blockquote prefix), `gap`, every untouched token, the order of the run, the rule text unless changed, `tail_ws` (so the hard break survives) and the line terminator.

**CRLF and lone CR.** Lines are split by the universal-newline rule. Only the content between line start and terminator is edited. A note's mixed terminators are untouched.

### D5. Every plan is verified before publication

A plan is a list of `(old line span → new text)` replacements and insertions, never a regenerated note. Before `dry_run` or publish, `task_write` asserts:

1. The new note equals the old note outside the declared spans, compared byte for byte.
2. `tasks_from_note_text(new)` yields the target (and any inserted line) as tasks, with exactly the intended `status`, dates, priority, tags and recurrence, read through both the extractor and `task_line.parse`.
3. Every other task of the note extracts identically: same `full_text`, status and line. Lines after an insertion shift by exactly the number of inserted lines.
4. No new task appears except the declared ones. This catches a context line or description that would create a checkbox or a heading.
5. **No pre-existing line changes its owning task** (Codex r1 #1). A line's owning task is the nearest preceding task line whose **continuation block** contains it, where the continuation block is defined in D8: uncapped, crossing blank lines. Every line of the old note keeps its owner in the new note, except the declared new lines, which are owned as planned.
6. **Tags are conserved** (Codex r2 #1). The target's tag set after the write (whole tags, case-insensitive) equals the set before, plus `add_tags`, plus any tag the caller typed inside a replacement `text` (the caller wrote it, so it is a named change), minus `remove_tags`. No tag is ever lost unless `remove_tags` names it.

**As implemented, `verify` is stricter than checks 1–6:**

- Check 1 is line-level. Every span is either a line-confined edit of the declared target line or a whole-line insertion at a line boundary. Every other line keeps its content and terminator.
- The target and the next-occurrence line are compared byte for byte with the original outside the intended tokens: prefix, gap, trailing whitespace, the description (or its exact replacement composition), and untouched run tokens in their place and order.
- For field families a call does not touch, the expectation is "reads exactly as on the original line". A priority emoji stranded in a description therefore does not block a recurring completion.
- The frontmatter block, every heading and every pre-existing line's code mask are unchanged, and no inserted line lies inside code.
- No task disappears.
- A note containing NUL is refused.

A result over the per-note task cap is `size_limit`.

A failed assertion is `task_malformed`, with nothing written. This is the last line of defence and the strongest safety property here. It is pinned by a property test over a corpus.

### D6. Status transitions

| Call | From | Result |
|---|---|---|
| `complete_task` | ` `, `/`, any symbol other than `x`/`X`/`-` | `[x]`, plus `✅ <done_on>` if `set_done_date` |
| `complete_task` | `x`/`X` | `already_completed` |
| `complete_task` | `-` | `task_cancelled` (reopen with `update_task(status="open")` first) |
| `update_task(status="open")` | any | `[ ]`; removes `✅` and `❌` if present (R§2d, "cleared on leaving") |
| `update_task(status="in_progress")` | any | `[/]`; removes `✅` and `❌` if present |
| `update_task(status="cancelled")` | any | `[-]`; removes `✅`; adds `❌ <today>` if `set_cancelled_date` and there is none; never a next occurrence (R§2b) |
| `update_task(status="done")` | | `invalid_argument` ("use complete_task") |

**Supervisor ruling 7.**

- Only the core symbols are modelled. A task with any other symbol is open, per the existing mapping, and `complete_task` writes `[x]`. The response states the replaced symbol, and custom cycles are not followed (L6).
- `🏁 delete` (case-insensitive) on a task being completed is refused with `on_completion_unsupported`. The server never deletes a line implicitly. `🏁 keep` is kept.

### D7. Recurrence: a closed grammar over rule text (`src/services/task_recurrence.py`, pure, stdlib only)

**Supervisor ruling 3.**

- The grammar is closed over **text shapes**. An unrecognised rule is refused (`recurrence_unsupported`, nothing written) and is never passed to a generic parser.
- **Implementation:** `datetime.date`, `datetime.timedelta`, `calendar.monthrange` and `calendar.weekday` only. `python-dateutil` is not a direct dependency (it is not in `requirements.txt`). The plugin uses rrule 2.8.1 (JavaScript), which no Python port reproduces exactly, so a port would be a heavy dependency whose divergences would be silent.

**Rule text.**

- The rule text is the value of the single `🔁` field in the run, trimmed.
- A trailing ` when done` (case-insensitive) sets `when_done`.
- Matching is case-insensitive on ASCII. Tokens are separated by one or more spaces, and a comma list allows optional spaces after commas.

**Tier 1, admitted.** Each shape is round-trip-verified (R§2a) and its dates are documented (R§2b/2c). `N` is an integer from 2 to 999 with no leading zero.

| Shape | Next date after anchor `A` |
|---|---|
| `every day` / `every N days` | `A + 1` / `A + N` days |
| `every weekday` | first Mon–Fri date `> A` |
| `every week` / `every N weeks` | `A + 7` / `A + 7N` days |
| `every week on D[, D…]` / `every D[, D…]` (D = a full English weekday name) | first date `> A` whose weekday is in the set |
| `every month` / `every N months` | `A` + N months; if the day does not exist, the **last day** of that month (R§2c: 2021-10-31 → 11-30 → 12-30 → 01-30 → 02-28 → 03-28; 2022-01-31 every 3 months → 04-30 → 07-30) |
| `every month on the K` (K = `1st`…`31st` with its correct English suffix) | first date `> A` with day K; months lacking K are **skipped forward** (R§2c: 01-31 → 03-31 → 05-31 → 07-31 → 08-31) |
| `every month on the last` | the last day of `A`'s month if `> A`, else the last day of the next month |
| `every year` / `every N years` | the same month and day N years later. **`A` = Feb 29 → `recurrence_unsupported`** (R§2c marks it UNVERIFIED) |
| `every M on the K` (M = a full English month name, K valid for M; Feb 29 refused) | first date `> A` that is M-K |

**Tier 2, refused by this change.** These shapes have no dates documented in R, or their text is lossy, and each would need a committed rrule-2.8.1 golden table before admission (owner decision 2026-11-10: stays refused; follow-up issue):

- `every N weeks on D…`;
- `every month on the last D`;
- `every month on the Kth D` and `the Kth last D`;
- `every N months on the …`;
- `every M and M on the K and K`;
- `every weekend` (lossy, R§2a);
- abbreviations;
- `every 1 day`;
- `for N times`;
- `until`;
- anything else.

**Reference date** (R§2b): the task's due date, else scheduled, else start, with "remove scheduled date on recurrence" off. Created, done and cancelled are never the reference.

- **The anchor `A`.** Without `when done`, `A` is the reference date (the first occurrence strictly after it, with `dtstart` = the reference). **Missed occurrences are not skipped.** A weekly task completed three weeks late gets reference + 7, which may still be in the past. This is mirrored on purpose. With `when done`, `A` is `done_on`, the completion date.
- **Other dates** among start, scheduled and due keep their whole-day offset from the reference: `new = old + (next_ref − ref)` (R§2b example: `⏳ 2021-10-28 📅 2021-10-30` every 2 weeks → `⏳ 2021-11-11 📅 2021-11-13`).
- **No reference date at all.** The next line is still written, with no dates (R§2b test).
- **An invalid highest-priority date** (a signifier with a non-calendar date). The plugin drops the rule and writes no next line, which silently ends the series. The server **refuses** (`task_malformed`, D4(d)).
- **A rule refused but the task otherwise completable** gives `recurrence_unsupported`. The agent may clear the rule with `update_task(clear=["recurrence"])` and then complete. Nothing is ever completed without its next occurrence.

**The next line** (supervisor ruling 2) is a copy of the completed task's **original** line, the pre-completion bytes, with:

- the status set to `[ ]`;
- start, scheduled and due values replaced in place by the shifted dates;
- these fields **removed** (D4 removal): `✅`, `❌`, `🆔`, `⛔` and the trailing `^block` link;
- `➕`: if `set_created_date`, set to today in the caller's timezone (replaced in place, or added per D4); otherwise removed if present (R§2d).

Everything else is identical: prefix, order, rule text, tags, priority, `🏁 keep` and `tail_ws`. The completed line is the original with D6's completion edit. **Rejected: canonicalising the next line.**

**Placement and terminators.**

- **`next_recurrence_below = false`** (the plugin default, R§2e): `next + T + completed`, replacing the original line's content. The completed task's continuation block stays directly under it, and no line changes owner.
- **`next_recurrence_below = true`:** the next line goes **after the completed task's full continuation block** (D8, uncapped), so the completed task keeps its own children and no line changes owner (D5 check 5). This deliberately differs from the plugin, which writes the two lines adjacently and so hands the children to the new occurrence (L17). With no continuation block the result is the plugin's `completed + T + next`.
- **Terminators, one rule for both placements** (Codex r2 #3). `T` is the **original task line's own terminator**. If that line has none (it is the last line), `T` is the note's first terminator, otherwise `\n`.
  - "Above" writes `next + T` before the line.
  - "Below" writes `T + next` when the insertion point is the end of a file without a final terminator, so the file still has none, and `next + T` otherwise.
  - In a mixed CRLF/LF note, a CRLF task line yields a CRLF next line wherever it is placed.

### D8. `add_task`: canonical serialisation, a precise placement rule, and context lines

**Line.** `"- [ ] " + text [+ " #" + tag …] [+ " " + priority] [+ " 🔁 " + rule] [+ " ➕ " + today if set_created_date] [+ " 🛫 " + start] [+ " ⏳ " + scheduled] [+ " 📅 " + due]`. This is R§1.3's canonical order. Tags go in the description, as the plugin writes them.

- `text` follows the D4 description rules.
- Tags must match the inline-tag grammar (`[A-Za-z][A-Za-z0-9_/-]*`, at most 128 characters, at most 32 tags).
- The rule must be tier 1 (D7). `add_task` itself never computes a date from it.

**Region `R`.**

- With `section`: the section body, resolved by `replace_section`'s selector over the frontmatter-stripped body. It inherits the same refusals (`selector_unresolved`, the defective-frontmatter refusal and the unmatched-indented-fence refusal).
- Without `section`: the whole post-frontmatter body.

**Anchor.** The last task line in `R` whose indentation width is 0 after its blockquote prefix, where tab counts as 4. Lines inside fenced code are never tasks, by the extractor.

**Continuation block (the write rule, uncapped; Codex r1 #1).** A task's continuation block is every line that belongs to it in Markdown. Scanning forward from the task line, a line belongs when either:

- it has a greater blockquote depth than the task; or
- it has the same blockquote prefix and an indentation width (tab = 4) greater than the task's.

Blank lines are crossed while a later non-blank line still belongs. The block ends at its last belonging non-blank line; trailing blank lines are not part of it. It stops at the first non-blank line that does not belong.

There is **no line or character cap**, and headings or fences that are themselves deeper-indented belong. The 8-line / 600-character cap of D12 applies only to the context that is **stored and rendered**. Placement (D7, D8 and `append_context`) always uses this uncapped block, and D5 check 5 proves no pre-existing line changes its owner.

**Insertion point.**

1. **With an anchor:** directly after the anchor's **continuation block**. The new line copies the anchor's blockquote prefix and its bullet character. An ordered anchor `N.` / `N)` gives `N+1` with the same delimiter and the anchor's following whitespace. The number is written without leading zeros (`007)` gives `8)`), and later items are not renumbered (L10, L25).
2. **No anchor, `R` has a non-blank line:** after `R`'s last non-blank line, as `- [ ] …` with no prefix.
3. **`R` empty:** directly after the section's heading line, or at body start when there is no section. An empty body becomes the line.

The new line is terminated by `T`, chosen as in D7 from the line it follows. When the insertion is at end of file and the file has no final terminator, `T` is prepended instead, so the file keeps having none.

**Never "blindly at end of file"**: case 2 is the end of the region, not of the file, unless the region is the file.

**Context line** (`add_task(context=…)`, `update_task(append_context=…)`):

- **Indentation.** If the task has direct child list items in its continuation block, the line copies the exact leading text (blockquote prefix and indentation) of the first shallowest one, so the new bullet is their sibling. Otherwise it is the task's own leading text plus one unit: a tab when the task's indentation or the note's nested list items use tabs, otherwise four spaces. It is refused `task_malformed` only when the D5 ownership check fails.
- It is inserted after the task's full continuation block (uncapped), so a context line is appended and never lands inside a nested child task's own block. A deeper child's lines belong to the task too, and the new line goes after all of them.
- The text follows the D4 description rules. D5 then refuses a line that would parse as a task, heading or fence.

### D9. Duplicate detection in `add_task`

**Normalised description:**

1. Take the D4 description region (fields and block link removed; tags kept, because the plugin's description includes them).
2. Apply NFC, then `casefold()`.
3. Collapse every Unicode whitespace run to one space and strip.

`description_key = sha256(b"omcp-desc-v1\x00" + norm).hexdigest()[:16]`, stored per task (D11).

**Exact duplicate** gives `duplicate_task`, nothing written, unless `allow_duplicate=true`. An open task (state `open`) with the same `description_key`:

- (a) among the current bytes of the target note, always, whatever the scope. `duplicate_scan(..., exclude_path=<target>)` leaves the target's index rows out, so a stale index can neither hide nor invent a duplicate there; or
- (b) in the index, among the caller's tasks under the default scope (D14). The query is `state='open' AND description_key = :k` joined under the owner predicate, with a partial index.

The refusal names up to 5 matches as `path:line id`. The index side reflects the caller's own earlier task writes through D10.

**Similar tasks** (never block):

- Terms are the normalised description split on whitespace and Unicode punctuation, keeping terms of 2 or more code points that are not all digits. Take the up to 8 longest.
- Candidates are open tasks in the default scope whose `text_fold` contains any term (`strpos`), ordered by `(file_path, line)`, at most `TASK_SIMILAR_SCAN_MAX = 2,000` rows.
- The score is the Jaccard index of the term sets, using each candidate's terms from its stored `text`. A candidate that is not an exact duplicate is **similar** when its score is ≥ 0.5, **or** when one description's term set contains the other's and the smaller set has at least 2 terms (so `Renew passport` lists `Renew passport for the trip`, Jaccard 0.4). Similar tasks are ranked by containment first, then by score, then by `(file_path, line)`; the top 5 are listed in the response and in the dry run.
- A scan stopped at the bound says so (`similar-task scan stopped at 2000 candidates`).
- It is deterministic and language-agnostic. **Rejected:** `pg_trgm`, which is not installed. Adding an extension needs the init scripts (database least-privilege), and trigram similarity misbehaves on short multilingual strings.

### D10. Read-your-writes: a narrow write-time task refresh that composes with the pass

**The #350 reasoning holds**, and this design keeps its invariant in a weaker, checkable form. A task tool **does not** index the note (row, hash, keyword vector, links). It refreshes **only that note's task rows**, and records which bytes they describe.

**Rejected: a full single-note index from the tool.** That would be a second writer of the keyword vector, links and the fingerprint-guarded state, the hottest and most-audited code in the server.

**Rejected: leaving freshness to the pass.** The brief requires read-your-writes.

**Columns (migration 039).**

- `notes_metadata.tasks_source_hash varchar(64) NULL` is the canonical note hash of the bytes the task rows were derived from.
- `notes_metadata.tasks_refreshed_at timestamptz NULL` is set only by the tool refresh.
- **Invariant:** the task rows describe the bytes whose hash is `tasks_source_hash`. Either that equals `content_hash` (pass-derived, the #350 D1 state), or a task tool published those bytes after the row's scan, and the row is **task-dirty**.

**R1. Refresh (`task_index.refresh_note_tasks`).** It runs after a confirmed publication. It first derives the tasks from the published bytes **off the loop, before opening its transaction**, so neither the row lock nor a pooled connection is held during the derivation. The result, or its failure, is applied only after step 3a passes. The refresh then runs in its own transaction:

1. `SET LOCAL lock_timeout = '2s'` (`TASK_REFRESH_LOCK_WAIT`, a constant).
2. Re-check that the caller is still bound to the root the tool ran under, the same check `change_feed.record_tool_events` performs.
3. `SELECT id FROM notes_metadata WHERE file_path = :rel AND <owner predicate> FOR UPDATE`.
3a. **Still current?** (Codex r1 #4) Under that row lock, re-read the leaf through the call's anchored target (the same parent descriptor, `read_bytes_at` under the cap) and compare it byte for byte with the bytes this call published. If they differ, or the read fails, another writer has published since. The refresh then writes nothing and reports `index: may lag (superseded)`.
4. Use the tasks already derived from **the bytes this call published** (`tasks_from_note_text`). Step 3a has just shown those bytes equal to disk, and rows are never derived from the re-read.
5. Delete and insert that note's `note_tasks` rows, as the pass does.
6. Set `tasks_source_hash = canonical_note_hash(published)`, `tasks_refreshed_at = clock_timestamp()`, and `tasks_truncated` / `tasks_failed` per the result.

It **never** writes `content_hash`, the stat columns, `derived_under`, `extraction_version` or any other column. It takes **no generation lock**: tasks do not depend on the index fingerprint, and the only lock is the note's row lock, so no cycle with the pass is possible. The pass takes the generation lock, then row locks; the refresh takes one row lock and never waits on the advisory lock.

The pass already holds the row lock of every note whose tasks it rewrites (D-context), so the two writers of a note's `note_tasks` are serialised by that row lock. No unique-key race on `(note_id, line)` is possible.

**R2.** The pass takes `scan_started_at` from the database before the scan's first file read: the snapshot statement's `statement_timestamp()`, read **once per pass invocation**, not once per attempt. An earlier instant is safe. It can only make R4 keep more refreshed rows, and a kept row stays task-dirty, so R3 re-reads it on the next pass.

**R3.** A row with `tasks_source_hash IS DISTINCT FROM content_hash` is **ineligible for the stat shortcut**: the scan reads it. Its task derivation joins the changed list for the task stage only. It is not upserted when its hash is unchanged.

**R4.** In `_update_tasks_for_changed`, per note, under the note's row lock (the pass re-reads `tasks_refreshed_at` and `tasks_source_hash` with one `SELECT … FOR UPDATE` per derived note; that row is already locked by the pass's upsert or move, so this costs a round trip, not a wait): if `tasks_refreshed_at >= scan_started_at` and `tasks_source_hash ≠` the pass's buffered hash, **keep the task rows**. The tool wrote them from bytes published after this pass began reading, so they may be newer. This is not a skip and not a failure, and is logged at DEBUG. Otherwise derive as today, and set `tasks_source_hash` to the buffered hash and `tasks_refreshed_at = NULL`.

**Convergence.**

- After the last write to a note at time W, the first pass whose `scan_started_at` is later than both W and the last refresh reads the note, through the stat change or R3.
- R4 then cannot keep the rows, because `tasks_refreshed_at < scan_started_at`. That pass re-derives from current disk, and `tasks_source_hash = content_hash` again.
- **Cost.** When only task-dirty notes changed, the task stage reads every id in the scope. R4 adds one round trip per derived note, which matters only during the one-time backfill.
- A row is never stuck: while it is task-dirty, R3 forces a read every pass. Pinned by an integration test that interleaves a pass's scan, a tool write, a refresh and a later edit in every order.

**Why a superseded refresh cannot leave uncorrected rows.**

- A superseded refresh writes nothing. The rows keep whatever state they had: pass-derived, or written by an earlier refresh. The newer bytes on disk came from a later publication P2, and both cases are covered:
  - **P2 by a task tool.** Its own refresh queues on the same row lock. Its 3a check passes unless yet another writer followed, in which case the same argument repeats with that writer.
  - **P2 by a generic writer.** It changed the file's stat. If the rows are pass-derived, the next pass reads the file and derives it. If they came from an earlier refresh, the row is task-dirty, so R3 reads it regardless of the stat.
- A refresh whose 3a check passes and is then followed, before its commit, by another publication P3 leaves rows describing its own bytes while disk holds P3's. That is the ordinary task-dirty state:
  - If P3 was a task tool, its refresh runs after this one releases the lock, and wins.
  - Otherwise R3 forces the next pass to read the note. R4 keeps the rows only for a pass whose scan began before this refresh. The first pass to begin after it derives from disk.
- So the convergence claim above holds unchanged. Pinned by an integration test with two task writes on one note whose refreshes finish out of order: the second write's refresh runs first, and the first write's refresh reports `superseded`. The rows then describe the second write.

**Failure mode.** A refresh that cannot take the row lock within 2 s, finds no row (a note never indexed, or a quarantined one), finds the root changed, finds the file superseded (3a), or raises **leaves the write standing**. The response carries `index: may lag (<reason code>)`, and the usage params get `task_refresh = lagged:<code>`. A successful refresh carries `index: refreshed`. The refresh never changes the outcome classification. The next pass converges regardless.

**Ordering: publish → refresh → change-feed event.** A consumer that reacts to the feed event and then calls `list_tasks` sees the new state. The event is recorded whatever the refresh did.

**Why the refresh uses the published bytes, not a re-read.** It never derives from bytes the call did not write, so a refresh cannot launder a concurrent writer's edit into "tool-refreshed" state. Any later edit changes the stat, and R3/R4 catch it.

`edit_note`, `set_frontmatter` and the other generic writers do **not** refresh (L5). Adding the hook there is a one-line follow-up, out of scope here.

### D11. Schema: migration 039 and extraction version 5

`039_task_tools.py`, `down_revision = "038"`, follows the 032/038 reconcile-or-refuse shape:

- the markers `"… (039_task_tools)"`;
- `lock_timeout` / `statement_timeout` set and then `RESET`;
- no data written;
- downgrade drops only marked objects.

| Object | Definition |
|---|---|
| `note_tasks.task_key` | `char(12) NULL` |
| `note_tasks.key_ordinal`, `key_count` | `integer NULL`; CHECK `ck_note_tasks_key_shape`: all three NULL, or all set with `1 ≤ key_ordinal ≤ key_count` |
| `note_tasks.description_key` | `char(16) NULL` |
| `note_tasks.text_fold` | `text NULL` (casefold of the stored, cut `text`) |
| `note_tasks.tag_prefixes` | `varchar(128)[] NULL`; GIN index `ix_note_tasks_tag_prefixes` |
| `note_tasks.recurrence` | `varchar(256) NULL` (the run's `🔁` value, cut at 256 with `recurrence_cut`) |
| `note_tasks.recurrence_cut` | `boolean NOT NULL DEFAULT false` |
| `note_tasks.created`, `cancelled` | `date NULL` (`➕`, `❌`, the #350 date rules) |
| `note_tasks.context` | `text NULL` (≤ 8 lines, each cut to 160 characters, joined by `\n`, ≤ 600 characters) |
| `note_tasks.context_cut` | `boolean NOT NULL DEFAULT false` |
| index `ix_note_tasks_open_desc` | `(description_key) WHERE state = 'open'` |
| `notes_metadata.tasks_source_hash` | `varchar(64) NULL` |
| `notes_metadata.tasks_refreshed_at` | `timestamptz NULL` |
| table `task_settings` | D14 |

**032's reconcile compares the complete `note_tasks` shape**, so a stamp-back that re-runs 032 over a head database would refuse 039's units as foreign. 032 therefore carries an **allow-list**: exactly 039's named units (columns, the CHECK and the two indexes), each carrying its exact 039 marker. Any other name, or the right name with any other comment, is still compared and refused. A future revision that adds a unit to `note_tasks` must extend that list on purpose.

**Stored context** keeps each line verbatim, including its blockquote prefix and any child-task lines. Rendering neutralises them (D12, the forged-row tests).

**Columns are nullable** because existing rows have no value until the version-5 pass re-derives them. Pass and refresh always write all of them. `ck_note_tasks_key_shape` keeps the triple coherent. The models mirror this byte for byte, so `alembic check` stays clean. Partial and GIN indexes are declared with `postgresql_where` / `postgresql_using`.

**`tag_prefixes`.** **Normalisation and prefixes** (Codex r3 NIT). The shared tag grammar allows empty `/` segments, so a tag is normalised before its prefixes are taken, in three steps: lower-case it (the grammar is ASCII), collapse every run of `/` to one `/`, and strip a trailing `/`. Its prefixes are then the normalised tag cut immediately before each `/`, plus the whole normalised tag. Every segment is non-empty, so no prefix is empty or ends with `/`. A `tags` / `exclude_tags` value is normalised the same way before matching (a trailing `/` is accepted and stripped), so `a//b` and `a/b` match each other. Matching is therefore lossless for every stored tag under this one rule, and agrees with the stored `tags` up to that normalisation. Example: `Waiting-For//Chase/` → `waiting-for/chase` → `waiting-for`, `waiting-for/chase`.

The array is deduplicated and **not capped** (Codex r2 #2). **The bound, derived.** A normalised tag with k segments needs at least 2k − 1 characters (k non-empty segments and k − 1 slashes). The 128-character limit applies to the tag as written, and normalisation only shortens it, so 2k − 1 ≤ 128, which gives k ≤ 64 prefixes per tag. With at most 64 stored tags, the array holds at most 64 × 64 = 4,096 entries. That bound is reachable (64 tags of a distinct 2-character first segment plus 63 one-character segments are 128 characters and 64 prefixes each); the scenario below builds a 4,032-entry case and the unit test also builds the 4,096 one. `tags` / `exclude_tags` can therefore never disagree with the stored `tags`. `tags_cut` keeps exactly its #350 meaning: a tag dropped from the stored list (over 128 characters, or past the 64th).

**Version 5.** The extraction gains the D1 key, ordinal and count, the D9 description key, `text_fold`, `tag_prefixes`, the run-based `recurrence` (plugin rule, R§1.4), `created`, `cancelled` and `context`.

- `clean_at_version` gains `5: clean_for_embedding`, so **nothing is re-embedded**.
- The first pass after deploy re-derives every note, at the same cost as the version 2–4 bumps.
- **Ids render from the row, not the note's version** (Codex r1 #6). A row renders its id whenever its `task_key` is populated, including rows a write-time refresh wrote into a note still stamped 4. A row with a NULL `task_key` (awaiting the version-5 backfill) renders `id pending`. The header counts such rows as `task ids pending: N tasks`: rows matching every other filter with `task_key IS NULL`, stated even at 0. Write tools work on any task regardless, because resolution re-derives from bytes. `TASK_IDS_SINCE_EXTRACTION_VERSION = 5` is kept only for the operator's backfill progress, and no listing decision reads it.
- `move_note(rewrite_links=True)` returns `index_not_ready` for that one pass, the existing behaviour.
- **Rollback is roll-forward**, the #350 L8 mechanism. A version-4 image re-extracts every note (its `marker_stale` sees `5 != 4`), leaves the new columns untouched, and never reads them.

### D12. `list_tasks`: the new arguments

```
list_tasks(folder=None, status="open", due_by=None, scheduled_by=None, tags=None,
           limit=100, cursor=None,
           due_from=None, scheduled_from=None, done_from=None, done_by=None,
           undated=None, has_due=None, text=None, priority=None, recurring=None,
           exclude_tags=None, path=None, order_by="due", detail="full",
           context=False, scope="default")
```

Every existing argument keeps its name, position and default. All new arguments are optional.

**Dates.** Every date argument accepts `YYYY-MM-DD`, `today`, `tomorrow`, `yesterday`, `+Nd` or `-Nd` (N from 0 to 3660), case-insensitive. Date arguments are validated against the **caller's effective settings** and the call's one clock snapshot, so a `from > by` check is evaluated in the caller's timezone. Relative dates are resolved by `task_dates.resolve(value, tz, now=…)` against the current date in the caller's effective timezone (D14). The clock is read **once per call** (`snapshot_now`), and every relative date, the echoed "today", and the agenda's date use that one instant. A malformed date is refused `invalid_argument` with the wording "must be a YYYY-MM-DD date or one of today, tomorrow, …". This replaces #350's YYYY-MM-DD-only message, because `due_by` and `scheduled_by` now also accept relative forms.

- `*_from` is inclusive (`>=`), and `*_by` is inclusive (`<=`).
- A task without that date is excluded when either bound is given.
- `from > by` is `invalid_argument`.
- `undated=true` means `due IS NULL AND scheduled IS NULL`, and `undated=false` its negation. It conflicts with any due or scheduled bound and with `has_due=true`, as `invalid_argument`.
- `has_due` is `due IS [NOT] NULL`. It is an addition to the brief's list (owner decision 2026-11-10: accepted), required so that every `task_agenda` section has an exact one-call continuation (D13).

**`text`.**

- 1–256 characters and at most 8 whitespace-separated terms, else `invalid_argument`.
- Each term goes through NFC and `casefold()`, and each must satisfy `strpos(note_tasks.text_fold, :term) > 0` (AND).
- The match is collation-independent and language-agnostic, because the folding is done in Python at extraction (`text_fold`) and on the argument.
- **Rejected:** `ILIKE` / `lower()`, whose case folding depends on the database ctype (ASCII-only under `C`).
- The stored text is cut at 1,000 characters, so with `text` the header carries `tasks with cut text: N`: rows matching every other filter whose `text_cut` is true. It is conservative and stated even at 0.

**`priority`.**

- A list of 1–6 names from `highest|high|medium|none|low|lowest`. `none` means `priority IS NULL`.
- Unknown values are `invalid_argument`.

**`recurring`.** `true` means `recurrence IS NOT NULL`. A rule this server cannot compute still counts as recurring (L11).

**`tags`, amended, and `exclude_tags`.**

- Each argument is stripped of one leading `#` and must be 1–128 characters. It is then normalised exactly like a stored tag (`task_line.normalise_tag`: lower-cased, `/` runs collapsed, a trailing `/` stripped), and the result must match `[a-z][a-z0-9_/-]*`. A trailing slash is therefore **accepted and normalised**: `work/` means `work`, as #350 also accepted it. Duplicates are dropped, and there are at most 32 entries.
- `tags` is `tag_prefixes @> ARRAY[…]` (AND). `exclude_tags` is `NOT (tag_prefixes && ARRAY[…])`. A row awaiting the version-5 backfill (NULL `tag_prefixes`) is matched by the same rule evaluated over its stored `tags` in SQL. That is exact, because stored tags are ASCII.
- Hierarchical by segment and case-insensitive: `waiting-for` matches `waiting-for` and `Waiting-For/x`, never `waiting-forever` (supervisor ruling 5).
- **Rejected: the plugin's per-tag substring match** (`#home` matches `#homework`, R§4).
- With `tags` or `exclude_tags`, the existing `tasks with cut tags` count covers both. A cut row might hide a matching or an excluded tag.
- **Behaviour change**, listed below: an exact-case or exact-depth match that worked before still matches, and a nested or differently-cased tag now matches too.

**`path`.** Exact equality with `notes_metadata.file_path` under the owner predicate, combinable with `folder`. Capped at `MAX_PATH_CHARS` by `arg_char_caps`.

**`scope`.**

- `default` applies the caller's settings: a task is excluded when its note's `file_path` starts with an excluded folder (the `folder_prefix_clause` semantics), or when the note's `notes_metadata.tags` hierarchically match an excluded note tag. This is the same segment rule applied to the note's tags, and a leading `#` on a stored note tag (as a frontmatter tag may be stored) is stripped before matching.
- `all` applies nothing.
- The header always states the scope and `excluded by scope: N tasks`: rows matching every other filter that the scope removed, stated even at 0.
- Scope applies to `path` and `folder` too. An explicitly named note inside an excluded folder lists 0 rows and reports its exclusion count.

**`order_by`** keys. `rank` maps highest 0, high 1, medium 2, none 3, low 4, lowest 5, per R§1.2.

| `order_by` | Sort key | Notes |
|---|---|---|
| `due` | `(coalesce(due,'infinity'), file_path, line)` | today's order |
| `priority` | `(rank, coalesce(due,'infinity'), file_path, line)` | |
| `scheduled` | `(coalesce(scheduled,'infinity'), file_path, line)` | |
| `done` | `(coalesce(done_on,'-infinity') DESC, file_path ASC, line ASC)` | the predicate is expanded for the mixed direction: `d < :d OR (d = :d AND (p, l) > (:p, :l))` |
| `path` | `(file_path, line)` | |

**Cursor version 2.**

- The payload is `{"v":2,"o","f","ob","k":[…],"p"|"h","l"}`. `k` holds the order's leading key values: a date or `±infinity`, plus a rank for `priority`.
- `f` is the digest of every normalised filter, using the **resolved** dates, plus the scope and the effective exclusion lists. A settings change between pages therefore refuses the cursor.
- The same owner discriminator, compact form, 2,048 cap and checks as #350 D8 apply.
- A version-1 cursor is refused with `invalid_argument`, a deliberate one-time break at deploy.
- A relative-date query continued after midnight resolves to different dates, so its cursor is refused, and the caller restarts (L4).

**`detail`.**

- `full` is today's row with ` — id <id>` inserted after `path:line`, or ` — id pending` for a row whose `task_key` is still NULL (D11).
- `brief` is `- [s] <id> \`<escaped path>\`:<line> — <dates> — <priority> — <tags> — <desc>`, where:
  - `<dates>` is `due …`, `sched …`, `start …`, `done …`, whichever are present;
  - `<priority>` is `p:<name>` when set;
  - `<tags>` is `#a #b`, cut to 48 characters with `…+N`;
  - `<desc>` is the D4 description region of the stored text, `_one_line`d and cut to 80 characters with `…`.
  
  **A brief row is at most 260 characters plus its escaped path**, pinned by a test. The path is never cut in a brief row, because write tools need it exactly; the abbreviated-row rule still applies to a first row that does not fit.

**Paths.** Every note-controlled path the task tools print goes through one escaping helper, `_escape_task_path`, in rows, echoes, refusals and responses. It escapes, injectively:

- whitespace other than U+0020 (`\n`, `\r`, `\t`, `\uXXXX`);
- the backslash (`\\`);
- the backtick (as `\u0060`, the same `\uXXXX` form the other escapes use), so a file name cannot close the backtick-delimited path field and forge a line or id (Codex impl r2 C-C6).

The write tools take the **original** path, not the escaped one.

**`context=true`.**

- Each row is followed by its stored context lines, each rendered as `    ┆ ` + `_one_line(line)`. No context line can begin with `- [`.
- `context_cut` adds `    ┆ (context cut)`.
- Context is the maximal run, at most 8 lines, of non-blank lines directly below the task that have either a greater blockquote depth than the task, or the same blockquote prefix with greater indentation width (tab = 4). The run stops at a blank line, a heading, a fence line or the cap.
- An abbreviated row omits context and notes `context omitted`.

**Header.**

- **Line 1** is today's header, unchanged in tokens and order, plus `task ids pending: N tasks` (rows with a NULL `task_key`, D11) after `over the task cap`.
- **Line 2, the query line**, is present whenever any new argument is given, any relative date was resolved, or the default scope excludes anything:
  `Query — timezone: <tz> — today: <date> — <new filter echoes, each value cut at 32 chars, relative dates as "+7d=2026-11-17"> — scope: default|all — excluded by scope: N tasks — excluded folders: N [names…] — excluded note tags: N [names…]`.
  - The line is at most 600 characters. Names are listed while they fit, then `+N more`. The counts are always present, so exclusion is never silent.
  - The timezone and every resolved date are mandatory and never cut.

**Backfill window.** On rows still awaiting the version-5 backfill (NULL `task_key`), only `text` and `recurring` read a version-5 column, and both fall back:

- `text` falls back to `lower(text)`, which can miss a non-ASCII case match;
- `recurring` falls back to the `🔁` signifier anywhere in the stored text.

While such rows are among the matches of every other filter, the response carries the incomplete-list notice `Incomplete list: [unscanned notes, unindexable tasks; ]<text|recurring|text and recurring> filter(s) approximate until the next index pass.`, at most 120 characters. Tags, dates, priority, `has_due`, `undated`, scope and every order read version-4 columns, and are exact. `task ids pending` ignores `text` and `recurring`: it is a conservative "might match" count.

**Bounds, as implemented and measured** (S4 audit). The synthetic worst case has every header count at 10 digits, a 10-digit line number, a 23-character id, a 1,024-character folder echo and a 32 × 128-character tags echo.

- **Never refused, never over the cap.** At B = 1,000 with no query line (every #350-only call under empty exclusion settings), a row always renders, abbreviated if necessary, and the response never exceeds B. `response_cap_too_small` is returned only when **no** abbreviated row fits at all. It is never returned merely because the shares fall below 40.
- **Text share.** The text keeps at least 40 characters.
- **Path share** (L22). The path share can fall below 40 in extreme header cases at B = 1,000. Rendered values are 140 characters with zero counts, 39 with 4-digit counts, 29 with 6-digit counts, and 9 with 10-digit counts. #350 kept 183 / 77 / 61. The difference is the 29-character id token. 40 + 40 needs B ≥ 1,031 in the extreme case.
- **With the query line**, a row always renders from B = 1,531. The "never refused at B ≥ 1,600" guarantee holds. 40 + 40 needs 1,608 (at 1,600 the path keeps 32). With the incomplete-list notice also present, a row renders from 1,564 and 40 + 40 needs 1,641.
- **Header line 1** is at most 480 characters. In the extreme case the folder echo is shortened so that 480 holds; 481 was measured before the fix.
- **The query line** is at most 600 characters, measured at 591, because the `text`, `priority`, `exclude_tags` and `path` echoes shrink from 32 to 24 characters when needed.
- **The abbreviated row's fixed structure** measures 86 characters, plus 29 for the ` — id <id>` token, plus 18 for `— context omitted` when context was requested. The design's earlier paper bound of 100 + 24 is superseded by these measurements.
- **An empty result is never refused.** It is the header with `shown: 0` and no cursor.

### D13. `task_agenda` (read-only)

```
task_agenda(date="today", days=7, folder=None, tags=None, exclude_tags=None, scope="default")
```

`D` is the resolved date. `days` runs from 1 to 31. Every section is `state='open'` and owner- and scope-scoped, and all sections are read from one `REPEATABLE READ, READ ONLY` snapshot. The predicates are disjoint by construction:

| Section | Predicate | Continuation (`list_tasks`, plus this call's folder/tags/exclude_tags/scope) |
|---|---|---|
| overdue | `due ≤ D−1` | `due_by=D−1, order_by="due"` |
| due today | `due = D` | `due_from=D, due_by=D` |
| scheduled and now due · no due date | `scheduled ≤ D ∧ due IS NULL` | `scheduled_by=D, has_due=false, order_by="scheduled"` |
| scheduled and now due · due later | `scheduled ≤ D ∧ due ≥ D+days+1` | `scheduled_by=D, due_from=D+days+1, order_by="scheduled"` |
| upcoming (grouped by due date) | `D+1 ≤ due ≤ D+days` | `due_from=D+1, due_by=D+days` |

- **Coverage.** Every open task with `due ≤ D+days`, or with `scheduled ≤ D`, is in exactly one section. A task scheduled on or before `D` whose due date falls within the window appears under its due section.
- **Rows.** Brief rows (D12). Caps: overdue 25, today 25, each scheduled sub-section 15, upcoming 50.
- **Section headers.** Each section header always states its total count. A capped section ends with `continue: list_tasks(<the added arguments, dates resolved>[, cursor="<compact v2 cursor>"]) with this call's folder, tags, exclude_tags and scope`. A cursor is minted (for exactly that `list_tasks` query: same `o`, same `f`) **only from a row actually shown**. A section that shows zero rows, because the response cap left no room, gives the same arguments **without** a cursor, which continue it from its first row (Codex r1 #5).
- **Header.** `Agenda — timezone — date — days — scope — excluded by scope: N` plus the #350 counts (not yet scanned, could not be indexed, over cap, ids pending, cut tags with `tags`/`exclude_tags`).
- **Budget.** Sections render in table order. A section that no longer fits renders only its header with `not shown (response cap)` and its continuation. B < 2,800 gives `response_cap_too_small`.
- **Registration.** `scan` class, `_READ_ONLY`.

### D14. Settings

**Table `task_settings`** (migration 039):

```
id                    integer PK
user_id               integer NULL FK users.id ON DELETE CASCADE, UNIQUE NULLS NOT DISTINCT
timezone              varchar(64) NULL            -- NULL: server default
excluded_folders      varchar(1024)[] NOT NULL DEFAULT '{}'   -- ≤ 32
excluded_note_tags    varchar(128)[]  NOT NULL DEFAULT '{}'   -- ≤ 32, stored lower-case without '#'
set_done_date         boolean NULL                -- NULL: env default
set_cancelled_date    boolean NULL
set_created_date      boolean NULL
next_recurrence_below boolean NULL
updated_at            timestamptz NOT NULL DEFAULT now()
```

- The NULL `user_id` row is the single-user scope, and the caller's owner scope maps 1:1 to a row.
- The table is durable for backups, not derived.
- No row means every value is the default.

**Environment defaults** (in `Settings`, documented in README and `.env.example`). Each plugin-mirroring default is the plugin's own (R§3).

| Variable | Default |
|---|---|
| `TASKS_DEFAULT_TIMEZONE` | `UTC`; validated with `zoneinfo.ZoneInfo`, an invalid name refuses startup |
| `TASKS_SET_DONE_DATE` | `true` |
| `TASKS_SET_CANCELLED_DATE` | `true` |
| `TASKS_SET_CREATED_DATE` | `false` |
| `TASKS_NEXT_RECURRENCE_BELOW` | `false` |

"Remove scheduled date on recurrence" is not exposed, and the server behaves as the plugin does with it off (supervisor ruling 6, L12).

**Service.** `task_settings.effective(session, uid) -> EffectiveTaskSettings` is a frozen dataclass merging the row over the defaults. It is read once per call. `task_settings.save(session, uid, form)` validates:

- the timezone against `zoneinfo.available_timezones()`, refusing host pseudo-zones (`localtime`, `Factory`, `posixrules`, and anything under `posix/` or `right/`);
- folders as vault-relative, without `..`, without a leading `/`, at most `MAX_PATH_CHARS`, normalised to end with `/`;
- tags against the tag grammar;
- at most 32 of each.

It upserts under `FOR UPDATE`. Each write-behaviour field takes `"on"`, `"off"` or `"default"`. `"default"` stores NULL, so the environment default applies. **A boolean key absent from the form leaves the stored value unchanged** (NULL for a new row).

**Panel.**

- **The card** is one shared template, `templates/_task_settings_card.html`. It has:
  - a timezone `<input list>` backed by a `<datalist>` of a fixed list of 76 common IANA names (`TASK_TIMEZONE_SUGGESTIONS`). Any IANA name the validator accepts may still be typed;
  - two textareas, one item per line;
  - four **three-option selects** (server default with its current value / on / off), each labelled with the plugin's own setting name.
- **Multi-user mode:** on the account page, editing the signed-in user's own row (`POST /admin/account/tasks`; 404 in single-user mode).
- **Single-user mode:** on the Settings page, editing the NULL-scope row (`POST /admin/settings/tasks`, admin; 404 in multi-user mode).
- Both use CSRF, the session flash, a 303 redirect, `data-*` attributes and no inline handler or style. Any client behaviour goes through a delegated listener in `panel.js`.
- **Rejected:** a card on `/account` in single-user mode, which 404s there by design (D23 of #197).
- The `page_account_*` goldens are regenerated deliberately. The settings page has no golden. The owner walks the card by hand with devtools open, and zero CSP violations is part of the bar.

`tzdata` is pinned in `requirements.txt` (pure data). Without it, `zoneinfo` depends on the image's OS packages. The `pip-audit` gate covers it.

### D15. Tool registration and the published surface

| Tool | Title | Annotations | `WRITE_REQUIRED_TOOLS` | Write bucket | Class |
|---|---|---|---|---|---|
| `task_agenda` | "Task agenda" | `_READ_ONLY` | no | no | `scan` |
| `complete_task` | "Complete task" | `_DESTRUCTIVE` | yes | yes | `write` |
| `update_task` | "Update task" | `_DESTRUCTIVE` | yes | yes | `write` |
| `add_task` | "Add task" | `_ADDITIVE` | yes | yes | `write` |

- **Registration order.** All four are registered after `hybrid_search`, in the order `task_agenda`, `complete_task`, `update_task`, `add_task`, so the first 28 entries keep their order.
- **Counts.**
  - 32 tools.
  - Read-only annotations: 19 (`task_agenda` added to the 18).
  - `WRITE_REQUIRED_TOOLS`: 12.
  - A read-only grant lists 20.
  - Write bucket: 11.
  - `CLASS_MAPPING_VERSION` 6.
- **The destructive rule becomes:** the write bucket **minus `create_note` and `add_task`**, plus `request_upload`. `add_task` removes no byte. `test_issue_343_tool_metadata.py` pins the new rule, read from the code.
- **Fixtures.**
  - `tools_list_schema_additions.json`: the `list_tasks` entry is regenerated. A test asserts it differs from its committed predecessor only by added optional properties (none in `required`) and its description. The four new tools are appended.
  - `tools_list_schema_5a391ca.json` and `tools_list_schema_amendments.json` are unchanged.
  - `tools_list_full_v1.json` is kept as the historical 1.0 oracle. A new `tools_list_full_v2.json` (32 tools) becomes the byte-identity oracle, and a test derives v1 from v2: v2 minus the four new entries equals v1, except for `list_tasks`'s description and its added optional properties.
  - `tools_list_readonly_oauth.json` is regenerated (20 entries).
- **Instructions.** `SERVER_INSTRUCTIONS` keeps its lead byte-identical. The `list_tasks` sentence becomes one sentence naming `list_tasks`, `task_agenda`, `add_task`, `update_task` and `complete_task`, saying that task writes take a `path` plus a task id from a listing, at most 2,000 characters in total.
- **Descriptions.** Each is at most 4,000 characters and leads with its contract: every argument, `readwrite` for writes, the id rule, `dry_run`, the `MCP-REFUSAL` codes, read-your-writes.

### D16. Refusal codes and responses

Every refusal ends with `MCP-REFUSAL {"code":…}`, and every refusal writes nothing.

- **Task-specific codes** (`refusals.TASK_CODES`, listed below) carry the explicit `nothing_written: true` marker (`disposition = refused`).
- **Generic codes** the write tools reuse from `edit_note` (`not_found`, `validation_failed`, `size_limit`, `io_failure`, and the `content_unsafe` payload) keep their existing payloads, exactly as in `edit_note`. Nothing is written in those cases either.

The existing partial-publication shapes are never produced, because each task tool publishes exactly once.

**New codes** (added to `src/services/refusals.py`):

| Code | When |
|---|---|
| `task_not_found` | no task in the current bytes derives the id's key |
| `task_ambiguous` | a bare key matches 2 or more tasks; the prose lists their suffixed ids and lines |
| `task_changed` | a suffixed id's count differs from the current count |
| `already_completed` | `complete_task` on `[x]`/`[X]` |
| `task_cancelled` | `complete_task` on `[-]` |
| `task_malformed` | D4(a)–(d), D5 verification failure, an invalid highest-priority date on a recurring completion |
| `recurrence_unsupported` | a rule outside tier 1 (completion, `update_task(recurrence=…)`, `add_task(recurrence=…)`), or yearly from Feb 29 |
| `on_completion_unsupported` | `🏁 delete` on completion |
| `duplicate_task` | `add_task` exact open duplicate without `allow_duplicate` |
| `response_cap_too_small` | read tools, D12/D13 (a read, so nothing written by definition) |

`precondition_required` (reused) also fires for a suffixed id without `expected_hash`, in every mode (D2).

`content_unsafe` (reused from `edit_note`'s section mode) is returned by every planner on a defective frontmatter block (detail `frontmatter_defect`), and by `add_task(section=…)` on an unmatched indented fence (detail `unmatched_fence`). The prose is `edit_note`'s.

**Reused codes:** `invalid_argument`, `validation_failed`, `selector_unresolved` and the section-write defect refusals, `size_limit` (note over the read cap, result over the note cap, note over the 5,000-task cap), `io_failure`, `malformed_precondition`, `stale_precondition`, `precondition_unavailable`, `concurrent_write` (after the D2 retry budget), `permission_denied`, the vault reassignment and anchor markers, and the rate and quota refusals.

A call that would change nothing (an absent tag removed, a value set to itself) answers `No changes for <path>` with nothing written, no event and no refresh.

**Success response** (one line each; note-controlled text `_one_line`d, paths escaped as in #350):

```
Completed task in `Projects/Alpha.md`
line 12: - [x] Pay rent 🔁 every month 📅 2026-11-01 ✅ 2026-11-10
id: 3f9a0c1b2d4e
next occurrence, line 12 (above): - [ ] Pay rent 🔁 every month 📅 2026-12-01
next id: 9b1c0a7e5f21
content_hash: sha256:…
index: refreshed
```

`add_task` adds `similar open tasks: …` (up to 5 `path:line id — desc`, or `none`). `update_task` names the fields changed. Every response states the resolved dates it wrote and the timezone, when a relative date or "today" was used.

### D17. Usage logging and attribution

`_tracked` params use the existing renderer (#310).

| Tool | Logged params |
|---|---|
| `complete_task` | `path`, `id`, `done_on`, `dry_run`, `expected_hash` presence |
| `update_task` | `path`, `id`, `status`, `clear`, `dry_run`, and the **names** of fields set |
| `add_task` | `path`, `section`, `dry_run`, `allow_duplicate` |
| `task_agenda` | `date`, `days`, `folder`, `tags`, `exclude_tags`, `scope` |
| `list_tasks` | adds every new argument |

- `text`, `append_context`, `context` and the description of a write tool are **never** logged.
- `list_tasks(text=…)` is logged like `keyword_search`'s `query`: the caller's search terms, not note content.
- Result-side fields: `task_refresh` (`refreshed`, `lagged:<code>` or `skipped`), and on `add_task` `similar_count`.
- Attribution, actor columns and change-feed principals are exactly those of `edit_note`, with `tool` set to the task tool's name.

### D18. Tenancy

- Every query joins `notes_metadata` under `_note_owner_predicate(uid)`, the total mapping, as part of the join: list, agenda, the counts, cursor resolution, the duplicate scans and the refresh's row lookup.
- Write resolution reads only the file `open_mutable` anchored under the caller's root.
- An id is meaningful only together with a path inside the caller's root. It never resolves outside it, because it is checked only against those bytes.
- `task_settings` is read by owner scope only. The panel edits only the signed-in user's own row, or, in single-user mode, the NULL row.

### D19. Time zones

`task_dates` resolves "today" as `datetime.now(ZoneInfo(tz)).date()` at call time, with the effective timezone from D14. It is used for:

- relative dates;
- the default `done_on`;
- cancelled and created dates;
- `task_agenda`'s default date.

The clock is injectable for tests. A setting naming a zone missing from this build's `tzdata` falls back to UTC, with the header saying `timezone: UTC (configured zone unavailable)`. This is never a refusal, and the panel cannot save such a zone.

## What existing behaviour changes

Each item is deliberate and additive or necessary under the 1.0 tool-contract promise: names, arguments and refusal lines are stable, and additions are allowed.

1. **`list_tasks` rows gain ` — id <id>`** after `path:line`. This is an additive token, inserted before the fields so the `text:`-last rule holds. It is required for the write tools.
2. **`list_tasks(tags=…)` matching becomes hierarchical and case-insensitive.** It fixes a silently empty answer. Every previously matching row still matches, and only more rows can match.
3. **The `list_tasks` header line 1 gains `task ids pending: N tasks`**, and a query line appears when new features are in use or exclusions are configured.

**Everything a #350-only call can observe** (no new arguments, empty settings):

- a `tags` value outside the tag grammar (`["2024"]`, `["a.b"]`, a non-ASCII tag) used to return an empty page and is now refused `invalid_argument`;
- a note path containing a backtick is now rendered with the backtick escaped as `\u0060` in every task row. It was printed raw, which let a file name forge a second path, line or id in one row;

- the ` — id <id>` token on every row;
- the `task ids pending` header token;
- the line-1 `tags:` echo is the normalised value, so `#Work` shows as `#work`;
- hierarchical, case-insensitive tag matching;
- a shorter abbreviated-row path share at very small caps (L22);
- cursor version 2, with version-1 cursors refused once;
- `due_by` / `scheduled_by` accepting relative dates, so the malformed-date refusal wording changed ("YYYY-MM-DD date or one of today, tomorrow, …");
- a trailing-slash tag value is normalised (`work/` = `work`).
4. **Default scope.** Once an owner configures exclusions, an argument-less `list_tasks` excludes them and says so. With no settings (the default), results are unchanged.
5. **Cursor version 2.** Version-1 cursors are refused once after deploy, with `invalid_argument` ("start without one").
6. **The `list_tasks` description** grows to cover the new arguments. The schema changes only by additive optional properties.
7. **The tool surface grows from 28 to 32**, the read-only grant list from 19 to 20, and the full-list oracle moves to v2.
8. **The annotation rule** "destructive = write bucket minus `create_note` plus `request_upload`" becomes "minus `create_note` and `add_task`".
9. **Extraction version 5:** one re-derive pass, during which `move_note(rewrite_links=True)` returns `index_not_ready`.
10. **A new boot refusal:** an invalid `TASKS_DEFAULT_TIMEZONE`.

## Accepted limitations

- **L1**: #350's L1–L4, L7 and L9 stand.
- **L2**: Date fields are still read last-wins anywhere on the line for listing, while write tools refuse duplicated or stranded fields (D4).
- **L3**: The id changes whenever a task's text changes. An id from an older listing then answers `task_not_found`, and the caller lists again.
- **L4**: A relative-date or settings-dependent cursor is refused after midnight or after a settings change.
- **L5**: `edit_note`, `set_frontmatter`, `move_note`, `write_file` and transfer uploads do not refresh task rows. Their changes appear after the next pass.
- **L6**: Custom status cycles are not followed. Completing a task with a custom marker writes `[x]`.
- **L7**: Tier-2 recurrence shapes are refused (D7).
- **L8**: A late recurring completion yields a next date that may already be past, mirroring the plugin.
- **L9**: A refresh that lags (row lock held by a long pass, or a note not yet indexed) leaves the listing stale until the next pass. The response says so.
- **L10**: `add_task` after an ordered-list anchor does not renumber later items.
- **L11**: `recurring=true` includes rules the plugin itself would discard as invalid.
- **L12**: "Remove scheduled date on recurrence" and the global task filter are not modelled.
- **L13**: `text` matches only the first 1,000 characters (reported by `tasks with cut text`).
- **L14**: Duplicate detection reads the index for notes other than the target, so a duplicate written by a generic tool within the last pass interval may be missed (L5).
- **L15**: Context lines are capped at 8 lines and 600 characters, in what is stored and rendered. Write placement always scans the uncapped continuation block (D8).
- **L16**: Publication is bound to (path, bytes), not to inode identity, the same as `edit_note`. A move plus a byte-identical recreation at the same path inside the read→rename window receives the planned edit (Codex r1 #3, declined).
- **L17**: With `next_recurrence_below`, the next occurrence goes after the completed task's continuation block, not on the adjacent line as the plugin writes it, so children stay with the completed task.
- **L18**: A write with a suffixed id needs a `read_note` first for its `content_hash`. The index digest cannot serve as the precondition (D2).
- **L19**: The internal retry gives up after 3 in-call conflicts with `concurrent_write`. More parallel writers to one note than that need the caller to retry.
- **L20**: 039's downgrade drops a foreign, unmarked index that sits on a 039 column, because PostgreSQL drops a column's indexes with it.
- **L21**: The refresh's root re-check is not under a lock. A root reassignment between the check and the write stores rows under the old scope's row, which the provenance discard then purges.
- **L22**: At the 1,000-character minimum cap, the abbreviated row's path share can fall below 40 characters in extreme header cases (as low as 9), because the id token costs 29 characters. The text keeps 40. A row always renders, and the cap is never exceeded.
- **L23**: A clean row (`tasks_source_hash = content_hash`) may keep a non-NULL `tasks_refreshed_at`, when a pass read B and then the refresh of B landed. It is harmless.
- **L24**: A note whose line 1 is a `---` thematic break with no closing `---` reads as a defective frontmatter block, so no task in it can be written (`content_unsafe`).
- **L25**: A lone-CR-terminated task with below-placement is refused by `verify`. An ordered anchor `007)` gives `8)`, losing the zero padding. `<key>.1of1` on a line that has since been duplicated answers `task_ambiguous`.
- **L26**: The three task write tools share the declared residual of the shared publish path. `expected=` compares the current bytes immediately before the rename, so a non-cooperating writer landing inside that window is overwritten, the same as with `edit_note`. Hardening the shared path is a follow-up. The conflict retry handles only a conflict the comparison saw.
- **L27**: Migration 039 validates its CHECK and builds two indexes inside its transaction. That is fine at the table sizes this server sees; a deployment with tens of millions of task rows would need a staged migration.
- **L28**: During the version-5 backfill, rows with a NULL `description_key` / `text_fold` are missed by `add_task`'s duplicate and similar scans.

## Test plan

**Unit, pure (no database):**

- **`task_line`**:
  - anatomy over every #350 checkbox shape (blockquote, callout, tab, ordered, CRLF, lone CR, hard break);
  - the run parse against every R§1.4 example;
  - D4 change, remove and add in canonical and non-canonical runs, before `^block`, with VS16 and variant signifiers;
  - tag add and remove;
  - description replace;
  - each refusal precondition (a)–(d);
  - a property test that bytes outside the declared spans are identical over a generated corpus.
- **`task_recurrence`**:
  - a table test with every R§2b/2c example (2021-10-31 monthly chain; 2022-01-31 every 3 months; 01-31 on the 31st; `⏳ 2021-10-28 📅 2021-10-30` every 2 weeks; the docs' late completion 2021-02-06 → 2021-02-13; no-date rule; `when done`);
  - weekday and weekday-list cases across week boundaries;
  - `on the last` in February of leap and non-leap years;
  - yearly from Feb 28 accepted, Feb 29 refused;
  - every tier-2 text refused;
  - case and spacing variants;
  - next-line construction (fields removed, created per setting) and placement above or below at end of file without a terminator.
- **`task_dates`**: every relative form at UTC, `Pacific/Kiritimati` and `America/Adak` around midnight, using the injectable clock; `+3661d` refused.
- **D1 id**: the key over a 1,500-character text; ordinals for 3 identical lines; `.1of1` equivalence; the grammar.
- **Rendering bounds**: a brief row ≤ 260 plus the path; query line ≤ 600; the minimum-cap test with ids at B = 1,000 (never refused, never over the cap, text ≥ 40, path share as measured in D12); 40 + 40 at 1,031 / 1,608 / 1,641; `response_cap_too_small` only when no abbreviated row fits; agenda at B = 2,800; context lines never start with `- [`; the forged-row cases through context.

**Integration (`tests/integration/`, real Postgres):**

- **Resolution**:
  - wrong line;
  - duplicate lines (bare key refused, suffixed works);
  - count changed between list and write;
  - a concurrent modification between read and publish (monkeypatched `write_file_at` race → `concurrent_write`, bytes unchanged);
  - a blockquote task, an indented task, a checkbox in a fence (`task_not_found`), a frontmatter look-alike (`- [ ]` inside YAML is not a task);
  - CRLF notes byte-preserved;
  - a note over the 5,000 cap → `size_limit`.
- **`complete_task`**: plain; done date off and on; recurring above and below; `when done`; `🏁 delete` refused; duplicated `📅` refused; tier-2 refused with bytes unchanged.
- **`update_task`**: each field set and clear; status transitions (D6 table); context append; no-op.
- **`add_task`**:
  - placement cases 1–3 in a section and in the whole note;
  - an ordered anchor;
  - a callout anchor;
  - at end of file without a final newline;
  - an exact duplicate refused, then allowed;
  - similar tasks listed;
  - the scan bound.
- **Parallel writes**: three parallel `complete_task` calls on different tasks of one note all succeed, and the note holds all three edits. Two parallel completions of the same task yield exactly one success, the other `already_completed` or `task_not_found`. With `expected_hash` supplied there is no retry (`stale_precondition`).
- **Suffixed ids**: refused `precondition_required` without `expected_hash`, in default mode too. Two identical lines whose blocks swapped are refused `stale_precondition` under the old hash. With the current hash, the named occurrence changes.
- **Placement**: an anchor with a 10-line continuation block (with a blank line inside and a deeper child task): `add_task`, `append_context` and below-placed recurrence all insert after the last line, and no line changes owner.
- **Read-your-writes**:
  - list → complete → list shows the new state with no pass;
  - refresh lag with the row locked by a held transaction → `index: may lag (lock_timeout)` and the write stands;
  - the D10 interleavings: a pass scan started before the tool publish commits after the refresh; an edit after the refresh; a refresh before a pass that read older bytes. In each, after at most two more passes, `tasks_source_hash = content_hash` and the rows match disk;
  - the pass-derivation = refresh-derivation equality over a corpus.
  - two task writes on one note whose refreshes finish out of order: the earlier write's refresh reports `superseded`, and the rows describe the later write.
  - a refreshed version-4 note renders its ids; rows with a NULL key count as `task ids pending`.
- **`list_tasks`**:
  - each new filter;
  - relative dates with the header echo;
  - hierarchical tags vs `waiting-forever`;
  - `exclude_tags`, and the cut-tags count with both;
  - text in Cyrillic, Greek with final sigma, and German `ß` vs `SS`;
  - every `order_by` paged to exactly-once over 1,234 tasks, including `done` with the mixed direction;
  - a v1 cursor refused;
  - scope exclusion counted;
  - `path`.
- **`task_agenda`**: section disjointness over a generated set (every open task within the horizon appears exactly once); caps with continuation cursors that `list_tasks` accepts and continues exactly.
- **Tenancy**: NULL-owned, user 7 and user 8 for list, agenda, duplicate scans, refresh (user 8's refresh never touches user 7's row with the same path) and settings.
- **Schema**: `make test-schema` with 039 (fresh, stamp-back to 038 and re-upgrade, impostor checks), `test_nonsuperuser_migrations_pg.py`, and `alembic check` clean.

**Tool surface:** counts 32/20/19/12/11; the destructive rule; fixtures (additions, the `list_tasks` amendment test, the v2 oracle and the v1 derivation, read-only 20); description caps; instructions cap and lead unchanged; every backticked tool name registered; `TOOL_CLASSES` complete; `CLASS_MAPPING_VERSION` 6; `#295` unknown arguments refused on the four new tools.

**Panel:** GET and POST in both modes; validation errors flashed with nothing saved; CSRF refused; `/account/tasks` 404 in single-user mode; `/admin/settings/tasks` admin-only; goldens regenerated; a CSP test (no `style=`, no `on*=`, nonce on every `<script>`/`<style>`).

## Slice plan (independent agents, one git worktree each)

Pre-flight: the base `origin/task-tools` with this change archived, committed, and typecheck green. Every brief opens with the base check (`alembic/versions/038_oauth_cimd_private_key_jwt.py` exists, no `039_*`).

| Slice | Wave | Owns (only these files) | Stubs / depends | Schema-test container |
|---|---|---|---|---|
| **S1 pure** | 1 | `src/services/task_line.py`, `src/services/task_recurrence.py`, `src/services/task_dates.py`, `tests/test_task_line.py`, `tests/test_task_recurrence.py`, `tests/test_task_dates.py` | none | none |
| **S2 schema + settings** | 1 | `alembic/versions/039_task_tools.py`, `src/models/db.py`, `src/config.py`, `src/services/task_settings.py`, `requirements.txt` (`tzdata`), `Makefile` / `deploy/kubernetes/components/db-backup/configmap.yaml` (classification), `tests/test_backup_derived_tables.py`, schema-gate cases, `tests/test_task_settings.py` | none | `omcp-tt-s2` / 55452 |
| **S3 indexer + refresh** | 2 (after S1, S2) | `src/services/tasks.py`, `src/services/indexer.py`, `src/services/embeddings.py` (alias 5), `src/services/task_index.py`, `tests/test_note_tasks_extractor.py`, `tests/integration/test_task_refresh_pg.py`, `tests/integration/test_note_tasks_pg.py` | uses `task_line.parse` and the key helpers | `omcp-tt-s3` / 55453 |
| **S4 query + render** | 2 | `src/services/task_query.py`, `src/mcp_server/task_render.py`, `tests/test_task_render.py`, `tests/integration/test_task_query_pg.py` | uses S2 columns, S1 `task_dates`; does **not** touch `tools.py` | `omcp-tt-s4` / 55454 |
| **S5 write planning** | 2 | `src/services/task_write.py`, `src/services/refusals.py` (new codes), `tests/test_task_write.py`, `tests/integration/test_task_duplicates_pg.py` | uses S1, S2; plans are pure; the duplicate scan is one async query; does **not** publish | `omcp-tt-s5` / 55455 |
| **S6 panel** | 2 | `src/control_panel/routes.py` (two routes), `templates/account.html`, `templates/settings.html`, `src/control_panel/static/panel.js` (only if needed), `tests/test_panel_task_settings.py`, `tests/golden/oidc_off/page_account_*` | uses S2 `task_settings` | none |
| **S7 wiring + surface** | 3 (after S3–S5) | `src/mcp_server/tools.py` (thin `*_impl` for the four tools; `list_tasks_impl` delegates to `task_query`), `src/mcp_server/server.py`, `src/mcp_server/tool_permissions.py`, `src/services/concurrency.py`, `src/mcp_server/vault_guide_primer.md`, fixtures, every count-pinning test, `tests/test_task_tools_e2e.py`, `tests/integration/test_task_tools_pg.py` | none | `omcp-tt-s7` / 55457 |
| **S8 docs** | 3 (after S7) | `README.md`, `.env.example`, `DEPLOYMENT.md`, `docs/deployment-kubernetes.md`, `docs/architecture/*.md`, `CLAUDE.md` | none | none |

- S1 and S2 run in parallel.
- S3, S4, S5 and S6 run in parallel after S1 and S2 merge. Their file sets are disjoint. S3 is the only one touching `indexer.py` and `tasks.py`, and S5 the only one touching `refusals.py`.
- **S2 alone owns migration 039.**
- Do not run the integration suite and the schema gate concurrently in one worktree. The ports are unique per slice.

**Seams the orchestrator wires and verifies after merging** (grep for a production caller of each):

- `task_index.refresh_note_tasks`: called by the three write impls (S7).
- `tasks.tasks_from_note_text`: called by the indexer (S3), `task_index` (S3) and `task_write` (S5).
- `task_settings.effective`: called by `task_query`, `task_write`, the write impls and the panel routes.
- `task_query.list_tasks_query` / `task_agenda_query`: called by `tools.py`.
- `task_write.plan_complete`, `plan_update`, `plan_add` and `duplicate_scan`: called by `tools.py`.
- `task_recurrence.next_occurrence`: called by `task_write`.
- The new refusal codes: produced by the impls.
- The R2/R3/R4 hooks: exercised by the S3 integration test **and** by the e2e test after S7.

Then run the full offline suite, `make test-integration`, `make test-schema` and `openspec validate` once on the merged result. That is the authoritative gate.

## Owner decisions (2026-11-10)

- **D3 (required mode).** For a bare id, `(path, id)` satisfies `WRITE_PRECONDITION_REQUIRED`, and `add_task` is exempt. **Exception:** a suffixed id always needs a matching `expected_hash`, in every mode (Codex r1 #2).
- **D7 (tier 2).** Tier-2 recurrence shapes stay refused in this change. Admitting them is a follow-up issue that commits a golden table generated with rrule 2.8.1 and admits each shape only when its rows match.
- **D12/D13 (`has_due`).** Accepted as an additive `list_tasks` filter.
- **D12 (response-cap guarantee).** Accepted: the guarantee holds at ≥ 1,600 when the query line is present, with `response_cap_too_small` below that. #350-only calls keep the 1,000 guarantee.

## Spec review record

**Codex spec review, round 1 (2026-11-10): FAIL**, with 3 BLOCKER, 2 MAJOR and 1 MINOR. Five were accepted and one declined.

1. **BLOCKER, accepted.** `add_task` placement used the 8-line context cap, so a 9th continuation line could be reparented. Placement and `append_context` now scan the uncapped continuation block (D8), and D5 check 5 proves no line changes owner.
2. **BLOCKER, accepted.** A suffixed id could name a swapped or replaced identical occurrence with the count unchanged. Suffixed ids now require a matching `expected_hash` in every mode (D1–D3). Listings do not print the hash, because the index digest is not the precondition digest (D2).
3. **BLOCKER as reported, declined (L16).** A move plus a byte-identical recreation at the same path inside the read→rename window. The caller addressed that path, and the bytes there equal those the plan was verified against, so the applied edit is exactly the planned one and nothing is lost. This is the existing `edit_note` publish envelope, and changing the vault publish path is out of scope.
4. **MAJOR, accepted.** A delayed refresh could overwrite newer rows. The refresh now re-reads through the anchored path under the row lock and reports `superseded` on a mismatch (D10 R1 3a), with a convergence argument and an out-of-order test.
5. **MAJOR, accepted.** A zero-row agenda section had no cursor source. It now gives its arguments without a cursor, and cursors come only from shown rows (D13).
6. **MINOR, accepted.** Ids render whenever `task_key` is populated, and the pending count is per row (D11).

**Codex spec review, round 2 (2026-11-10).** All six round-1 items were verified, and additions A and B were accepted. The verdict was FAIL on three new findings, all accepted:

1. **MAJOR.** `update_task(text=…)` could erase tags. Tags in the old description now survive by re-append, and D5 check 6 conserves the tag set.
2. **MAJOR, implausible input but cheaper to fix.** The 256-entry `tag_prefixes` cap could make tag filters disagree with the stored tags. The cap is removed, and the array is bounded by construction.
3. **MINOR.** The next-occurrence terminator rule differed between placements. One rule now applies: the original task line's terminator.

Round 3 is verification only, per the review budget.

**Implementation audits (2026-11-10).** Each slice was audited independently after it merged.

| Slice | Result |
|---|---|
| S1 (pure modules) | 1 MAJOR, 3 MINOR, 2 NIT, all fixed. This includes the `task_line` performance fix: the run parse is now linear, and a 10 MiB line parses in under 2 s, where the old rescan-per-pass took about 25 s |
| S2 (schema, settings) | no BLOCKER or MAJOR; 4 fixed (032 allow-list, `save` on absent booleans, pseudo-zones, docs), 1 accepted (L20) |
| S3 (indexer, refresh) | no BLOCKER or MAJOR; 1 fixed (derive before the row lock), 2 accepted (L21, L23) |
| S4 (query, render) | 1 MAJOR (transient), 4 fixed (incomplete notice during the backfill, one clock read per call, empty result never refused, `#`-prefixed note tags in scope), 2 spec-text corrections (bounds, trailing slash) |
| S5 (write planning) | 1 MAJOR and 4 MINOR fixed (frontmatter-defect refusal in every planner, a stronger `verify`, caller-text structure refusals, context-line indentation, the similar-task rule), 3 accepted (L25) |

**Implementation review (2026-11-10).**

- **Whole-change `openspec-verifier`:** 0 BLOCKER, 0 MAJOR, 3 MINOR and 3 NIT.
  - Fixed: the date pre-check timezone, the refusal codes in the descriptions, the guard-rail enumerations, and the shared owner predicate.
  - Recorded: the generic-refusal payloads (D16) and the tag-grammar change.
- **Adversarial Codex, round 1:** PASS in three minutes with no trace, so it is not relied on.
- **Adversarial Codex, round 2:** three focused passes (write path; index, refresh and migration; listing, gates and surface), 38 scenarios traced with file-and-line evidence, 35 SAFE.
  - **Accepted and fixed (MAJOR):** a backtick in a path could forge a row.
  - **Declined:** the publish-path window (L26). It is an owner-declared residual, needs an external writer at one exact instant, and is shared with `edit_note`.
  - **Declined:** migration 039 on a multi-million-row table (L27). The size is implausible for this server.
- **Full gates on the merged result at 3145320** (before the final fix round): the offline suite passed (10,093 tests) and the full integration suite passed (1,565 tests, none skipped, real PostgreSQL). The schema gate passed three times during the slice work (404 tests each: the schema slice, its auditor, the indexer slice).
- **After the final fix round and the docs (the tree this change is archived from):** the full offline suite passed (10,160 tests); the integration modules the round touched passed on a throwaway database (task tools, duplicates, refresh, query, `list_tasks`: 163, then the task-tool module again with the path round trip: 19); `make audit` is clean. CI on the pull request runs the complete suite, the schema gate and `openspec validate --specs --strict` on that tree and is the authoritative gate.

**Supervisor additions** (round 1).

- **A.** With `next_recurrence_below`, the next line goes after the completed task's full continuation block (D7, L17).
- **B.** A bounded internal retry, at most 3 attempts with full re-resolution, on an in-call conflict when no `expected_hash` was supplied (D2).

**Codex spec review, round 3 (2026-11-10, verification only): PASS.** All three round-2 fixes were verified. One NIT was fixed: the stated 4,096 bound ignored the empty `/` segments the tag grammar allows, and its scenario could not be built. The prefix rule now normalises slash runs first, and the bound is derived from it (≤ 64 per tag, ≤ 4,096 in total). The scenario uses a buildable 4,032-prefix case.
