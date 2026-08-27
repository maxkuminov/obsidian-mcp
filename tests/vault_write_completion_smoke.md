# vault-write-completion smoke tests (client-side)

You are an MCP client connected to `<your-instance-host>` with a `readwrite`
API key. Run each test below **verbatim**, capture the response, and report
PASS/FAIL with a one-line reason. Do not improvise around failures — record
them and move on.

This script verifies the `vault-write-completion` change: atomic writes, the
expanded `edit_note` modes (`dry_run`, `replace_all`, `section`), and the three
new write tools (`move_note`, `delete_note`, `set_frontmatter`).

## Tools under test

`edit_note` (new modes/flags), `move_note`, `delete_note`, `set_frontmatter`.
Plus a regression check on `create_note` to confirm the atomic-write refactor
didn't break the existing path.

## Throwaway fixtures used in this script

All fixtures live under `Cards/_smoke/` so they're easy to clean up and
unlikely to clash with real vault notes. Each test creates the fixtures it
needs at the top and references them by name. Final cleanup at the bottom.

| Fixture                                    | Purpose                                     |
|--------------------------------------------|---------------------------------------------|
| `Cards/_smoke/atomic.md`                   | Atomic-write target.                        |
| `Cards/_smoke/edit_modes.md`               | `edit_note` mode tests.                     |
| `Cards/_smoke/section.md`                  | Section-mode tests with multiple headings.  |
| `Cards/_smoke/move_src.md` → `move_dst.md` | `move_note` source/destination.             |
| `Cards/_smoke/move_link_src.md`            | Source linking to the moved note.           |
| `Cards/_smoke/delete_soft.md`              | Soft-delete target.                         |
| `Cards/_smoke/delete_perm.md`              | Permanent-delete target.                    |
| `Cards/_smoke/fm_existing.md`              | Note already carrying frontmatter.          |
| `Cards/_smoke/fm_none.md`                  | Note with no frontmatter (line 1 ≠ `---`).  |

---

## 0. Pre-flight (deploy)

### 0.1 Health endpoint
- Call: `GET https://<your-instance-host>/health`.
- PASS if: `{"status":"ok"}`.

### 0.2 Tool listing exposes the three new tools and the expanded edit_note signature
- Call: `tools/list`.
- PASS if: response includes `move_note`, `delete_note`, `set_frontmatter`,
  and the `edit_note` tool's listed parameters include `section`,
  `replace_all`, and `dry_run`.

### 0.3 Read-only key cannot reach any of the new tools (skip if no read-only key handy)
- Authenticate with a `read` key.
- Call: `move_note(from_path="x.md", to_path="y.md")` — expect "Permission denied …".
- Call: `delete_note(path="x.md")` — expect "Permission denied …".
- Call: `set_frontmatter(path="x.md", updates={})` — expect "Permission denied …".
- PASS if: each call returns the standard permission-denied message and the
  filesystem is unchanged.

---

## 1. Atomic write (regression on `create_note` and `edit_note`)

### 1.1 `create_note` still works
- Call: `create_note(path="Cards/_smoke/atomic.md", content="hello")`.
- PASS if: response is "Created note: …" and `read_note(path="Cards/_smoke/atomic.md")`
  returns the content.

### 1.2 No `.tmp-*` orphans remain after a successful write
- After test 1.1, list the files in `Cards/_smoke/` (use the panel file
  browser, or shell into the container: `ls /obsidian/Cards/_smoke/`).
- PASS if: the only file is `atomic.md`. No `.tmp-*` files left over.

### 1.3 Crash-mid-write does not truncate the destination (manual)
- This requires shell access to the container. Skip if not feasible.
- In a Python REPL inside the container, monkey-patch `Path.write_text` to
  raise mid-call, then call `write_file("Cards/_smoke/atomic.md", "REPLACED")`.
- PASS if: the call raises, AND `cat Cards/_smoke/atomic.md` still prints
  `hello` (unchanged), AND any `.tmp-*atomic.md*` orphan in
  `Cards/_smoke/` has been cleaned up.
- If skipped, note "MANUAL/SKIP" in the report.

---

## 2. `edit_note` — mode mutex

### 2.1 Reject conflicting modes
- Setup: `create_note(path="Cards/_smoke/edit_modes.md", content="line one\nline two\n")`
  (re-run test 1.1's cleanup if needed).
- Call: `edit_note(path="Cards/_smoke/edit_modes.md", content="X", append=True, find="line one")`.
- PASS if: response says modes conflict (mentions both `append=True` and `find=...`)
  AND the file is unchanged.

### 2.2 Section + find combo also rejected
- Call: `edit_note(path="Cards/_smoke/edit_modes.md", content="X", find="line", section="H")`.
- PASS if: response mentions both `find=...` and `section=...` AND the file
  is unchanged.

---

## 3. `edit_note` — `dry_run`

### 3.1 Dry-run on full-replace returns a unified diff
- Call: `edit_note(path="Cards/_smoke/edit_modes.md", content="totally new\n", dry_run=True)`.
- PASS if: response includes `--- Cards/_smoke/edit_modes.md`, `+++ Cards/_smoke/edit_modes.md`,
  `@@`, AND a subsequent `read_note(path="Cards/_smoke/edit_modes.md")` has
  `content` = the prior content (file untouched).

### 3.2 Dry-run on append
- Call: `edit_note(path="Cards/_smoke/edit_modes.md", content="appended", append=True, dry_run=True)`.
- PASS if: diff shows `+appended` AND the file is unchanged on disk.

### 3.3 Dry-run on find/replace
- Call: `edit_note(path="Cards/_smoke/edit_modes.md", content="LINE ONE", find="line one", dry_run=True)`.
- PASS if: diff shows `-line one` and `+LINE ONE` AND the file is unchanged.

### 3.4 Dry-run no-op
- Call: `edit_note(path="Cards/_smoke/edit_modes.md", content=<the file's exact current content>, dry_run=True)`.
- PASS if: response is "No changes for Cards/_smoke/edit_modes.md".

---

## 4. `edit_note` — `replace_all`

### 4.1 Multiple matches without `replace_all` is an error
- Reset the file: `edit_note(path="Cards/_smoke/edit_modes.md", content="cat dog cat dog cat\n")`.
- Call: `edit_note(path="Cards/_smoke/edit_modes.md", content="bird", find="cat")`.
- PASS if: response says it matches 3 locations AND mentions `replace_all=True`
  in the actionable hint AND the file is unchanged.

### 4.2 `replace_all=True` replaces all occurrences and reports the count
- Call: `edit_note(path="Cards/_smoke/edit_modes.md", content="bird", find="cat", replace_all=True)`.
- PASS if: response is "Replaced 3 occurrence(s) in Cards/_smoke/edit_modes.md"
  AND `read_note`'s `content` is `bird dog bird dog bird`.

### 4.3 Single match with `replace_all=True` still works (N=1 edge case)
- Reset: `edit_note(path="Cards/_smoke/edit_modes.md", content="apple banana\n")`.
- Call: `edit_note(path="Cards/_smoke/edit_modes.md", content="cherry", find="banana", replace_all=True)`.
- PASS if: response is "Replaced 1 occurrence(s) …" AND content is
  `apple cherry`.

### 4.4 Zero matches still surfaces a preview (replace_all does not change this)
- Call: `edit_note(path="Cards/_smoke/edit_modes.md", content="X", find="nonexistent", replace_all=True)`.
- PASS if: response says find text not found AND includes a 500-char preview
  of the note.

---

## 5. `edit_note` — section mode

### 5.1 Setup with multiple sections at multiple depths
- Call:
  ```
  create_note(path="Cards/_smoke/section.md", content=
  "# Project A\n\n## Tasks\n- a1\n- a2\n\n## Notes\nA notes\n\n# Project B\n\n## Tasks\n- b1\n\n## Notes\nB notes\n")
  ```

### 5.2 Replace a uniquely-named section
- Call: `edit_note(path="Cards/_smoke/section.md", content="REPLACED A NOTES", section="Notes")`.
- Expect: error because "Notes" appears twice (under both Project A and Project B).
- PASS if: response says it matches multiple headings AND instructs path-style
  disambiguation (`Parent/Child`).

### 5.3 Path-style disambiguation
- Call: `edit_note(path="Cards/_smoke/section.md", content="REPLACED A NOTES\n", section="Project A/Notes")`.
- PASS if: `read_note(section="Project A/Notes")` has `content` =
  `REPLACED A NOTES\n` AND `read_note(section="Project B/Notes")` still has
  `content` = `B notes\n`.

### 5.4 Section bounded by next equal-or-shallower heading
- Reset section.md per 5.1 if needed.
- Call: `edit_note(path="Cards/_smoke/section.md", content="X\nY\n", section="Project A/Tasks")`.
- PASS if: the note now reads (in order): `# Project A`, blank, `## Tasks`,
  `X`, `Y`, blank, `## Notes`, `A notes`, …, `# Project B`, …
- The replacement does NOT bleed past `## Notes` into Project B.

### 5.5 Section heading not found
- Call: `edit_note(path="Cards/_smoke/section.md", content="X", section="DoesNotExist")`.
- PASS if: response says heading not found AND lists the headings present
  (with depth markers like `# Project A`, `## Tasks`, etc.) AND the file is
  unchanged.

### 5.6 Replace section at end of file
- Reset: `create_note` (overwrite via edit_note full-replace) so the file ends
  with `## Tail\nold tail body\n`.
- Call: `edit_note(path="Cards/_smoke/section.md", content="new tail\n", section="Tail")`.
- PASS if: the file now ends with `## Tail\nnew tail\n` (heading preserved,
  body replaced).

### 5.7 Headings inside fenced code are NOT matched
- Set the file to:
  ```
  # Real Heading
  body
  ```
  ` ```\n## Fake Heading\n```\n`
- Call: `edit_note(path="Cards/_smoke/section.md", content="X", section="Fake Heading")`.
- PASS if: response says "not found" (the heading is inside a fenced code
  block and is skipped).

---

## 6. `move_note`

### 6.1 Setup
- `create_note(path="Cards/_smoke/move_src.md", content="# src\nbody\n")`.
- `create_note(path="Cards/_smoke/move_link_src.md", content="see [[move_src]] and [[move_src|alias]] and ![[move_src]] and [[Cards/_smoke/move_src]]\n")`.
- Wait one indexing interval (≤ 5 minutes) so `note_links` populates.

### 6.2 Plain move (no link rewrite)
- Call: `move_note(from_path="Cards/_smoke/move_src.md", to_path="Cards/_smoke/move_dst.md")`.
- PASS if: response is "Moved Cards/_smoke/move_src.md → Cards/_smoke/move_dst.md"
  AND `read_note(path="Cards/_smoke/move_dst.md")` returns the content
  AND `read_note(path="Cards/_smoke/move_src.md")` returns `error` = "Note not found: …"
  AND `read_note(path="Cards/_smoke/move_link_src.md")` has `content`
  unchanged (still says `[[move_src]]` etc).

### 6.3 Destination already exists
- Call: `move_note(from_path="Cards/_smoke/move_dst.md", to_path="Cards/_smoke/move_link_src.md")`.
- PASS if: response says destination exists AND BOTH files remain in place.

### 6.4 Source missing
- Call: `move_note(from_path="Cards/_smoke/does_not_exist.md", to_path="Cards/_smoke/x.md")`.
- PASS if: response says source not found.

### 6.5 Path traversal rejected
- Call: `move_note(from_path="../escape.md", to_path="Cards/x.md")`.
- PASS if: response is the standard "Path traversal denied" error.

### 6.6 Move + rewrite_links round-trip
- Reset: undo 6.2 by `move_note(from_path="Cards/_smoke/move_dst.md", to_path="Cards/_smoke/move_src.md")`,
  then wait for an indexing interval so `note_links` re-populates.
- Call: `move_note(from_path="Cards/_smoke/move_src.md", to_path="Cards/_smoke/move_dst.md", rewrite_links=True)`.
- PASS if: response includes a "rewrote N link(s) across M note(s)" suffix,
  AND `read_note(path="Cards/_smoke/move_link_src.md")` shows
  `[[move_dst]]`, `[[move_dst|alias]]`, `![[move_dst]]`, and
  `[[Cards/_smoke/move_dst]]` (path-style updated to the new path).

---

## 7. `delete_note`

### 7.1 Soft-delete moves to `.trash/`
- Setup: `create_note(path="Cards/_smoke/delete_soft.md", content="goodbye\n")`.
- Call: `delete_note(path="Cards/_smoke/delete_soft.md")`.
- PASS if: response is "Soft-deleted: Cards/_smoke/delete_soft.md → .trash/<TS>-delete_soft.md"
  AND the file no longer exists at the original path
  AND a file matching `.trash/*delete_soft.md` does exist (verify via shell or
  panel file browser).

### 7.2 Soft-delete is invisible to search after reindex
- Wait one indexing interval.
- Call: `keyword_search(query="goodbye", limit=5)`.
- PASS if: the deleted note is not in the results.

### 7.3 Trash collisions disambiguated by timestamp/counter
- Setup again: `create_note(path="Cards/_smoke/delete_soft.md", content="goodbye 2\n")`.
- Immediately delete: `delete_note(path="Cards/_smoke/delete_soft.md")`.
- PASS if: response has a different `.trash/` filename than test 7.1's
  (either a different timestamp or a `-1-` / `-2-` counter suffix).

### 7.4 Permanent delete
- Setup: `create_note(path="Cards/_smoke/delete_perm.md", content="bye\n")`.
- Call: `delete_note(path="Cards/_smoke/delete_perm.md", permanent=True)`.
- PASS if: response is "Permanently deleted: Cards/_smoke/delete_perm.md"
  AND no `.trash/*delete_perm*` file exists.

### 7.5 Missing note returns actionable error
- Call: `delete_note(path="Cards/_smoke/does_not_exist.md")`.
- PASS if: response says note not found AND no `.trash/` directory has been
  created if it didn't already exist.

### 7.6 Path traversal rejected
- Call: `delete_note(path="../something.md")`.
- PASS if: response is the standard "Path traversal denied" error.

---

## 8. `set_frontmatter`

### 8.1 Update an existing key
- Setup: `create_note(path="Cards/_smoke/fm_existing.md", content="---\nstatus: draft\nproject: Foo\n---\n\n# Body\nbody text\n")`.
- Call: `set_frontmatter(path="Cards/_smoke/fm_existing.md", updates={"status": "done"})`.
- PASS if: response mentions "set: status"
  AND `read_note`'s `frontmatter` has `status: done` and `project: Foo` both present
  AND the body still reads `# Body\nbody text` (untouched).

### 8.2 Add a new key
- Call: `set_frontmatter(path="Cards/_smoke/fm_existing.md", updates={"tags": ["x", "y"]})`.
- PASS if: frontmatter now includes `tags: [x, y]` (or block-style equivalent)
  AND the existing keys are preserved AND the body is untouched.

### 8.3 Remove keys
- Call: `set_frontmatter(path="Cards/_smoke/fm_existing.md", updates={}, remove=["project", "wip"])`.
- PASS if: response mentions "removed: project" (NOT "wip" since it didn't
  exist) AND `read_note`'s `frontmatter` no longer has `project`.

### 8.4 No-op short-circuits
- Call: `set_frontmatter(path="Cards/_smoke/fm_existing.md", updates={}, remove=[])`.
- PASS if: response is "No changes for …" AND the file is byte-identical to
  before (compare via shell stat or by re-reading).

### 8.5 Note with no frontmatter gets a fresh block prepended
- Setup: `create_note(path="Cards/_smoke/fm_none.md", content="# Heading\n\nbody only, no frontmatter\n")`.
- Call: `set_frontmatter(path="Cards/_smoke/fm_none.md", updates={"tags": ["new"]})`.
- PASS if: `read_note`'s `frontmatter_yaml` is `tags:\n- new\n` and its
  `content` is the original `# Heading\n\nbody only, no frontmatter\n`.

### 8.6 Frontmatter not on line 1 is treated as no frontmatter
- Setup: `create_note(path="Cards/_smoke/fm_none.md", content="\n---\nfoo: 1\n---\nactually a body\n")`
  (note the leading blank line — frontmatter must start on line 1 per
  Obsidian's rule).
- Call: `set_frontmatter(path="Cards/_smoke/fm_none.md", updates={"tags": ["new"]})`.
- PASS if: `read_note`'s `frontmatter` is ONLY `{"tags": ["new"]}` — the
  original `---\nfoo: 1\n---\n` stays in `content` as body text and is NOT
  merged into the new frontmatter.

### 8.7 Body byte-identical when only frontmatter changes
- Setup with body containing trailing whitespace:
  `create_note(path="Cards/_smoke/fm_existing.md", content="---\nfoo: 1\n---\n\nline a\nline b   \n\n")`.
- Run `stat` (or capture file size) on the file.
- Call: `set_frontmatter(path="Cards/_smoke/fm_existing.md", updates={"foo": 2})`.
- PASS if: `read_note`'s `frontmatter` shows `foo: 2`, AND its `content` is
  byte-identical to the original body (trailing spaces and blank lines
  preserved).

---

## 9. Usage logs

### 9.1 `move_note`, `delete_note`, `set_frontmatter` are logged
- After running tests 6–8, open the panel "Usage" page (or query `usage_logs`).
- PASS if: there are rows with `tool='move_note'`, `tool='delete_note'`, and
  `tool='set_frontmatter'` matching today's calls, with `params` populated.

### 9.2 `edit_note` flag params are logged
- Pick a row from test 3.x.
- PASS if: that row's `params` JSON includes `dry_run: true`.

---

## 10. Cleanup

- Permanently delete the entire smoke fixture set:
  - `delete_note(path="Cards/_smoke/atomic.md", permanent=True)`
  - `delete_note(path="Cards/_smoke/edit_modes.md", permanent=True)`
  - `delete_note(path="Cards/_smoke/section.md", permanent=True)`
  - `delete_note(path="Cards/_smoke/move_dst.md", permanent=True)` (or `move_src.md` if 6.6 was skipped)
  - `delete_note(path="Cards/_smoke/move_link_src.md", permanent=True)`
  - `delete_note(path="Cards/_smoke/fm_existing.md", permanent=True)`
  - `delete_note(path="Cards/_smoke/fm_none.md", permanent=True)`
- Optionally empty `.trash/` of the soft-deleted entries created by tests 7.1
  and 7.3.

---

## Reporting

When done, return a markdown table:

| ID  | PASS/FAIL/SKIP | Notes |
|-----|----------------|-------|
| 0.1 | PASS           |       |
| 0.2 | PASS           |       |
| ... | ...            |       |

Then a one-paragraph summary covering:
- Atomic write — any orphan `.tmp-*` files observed.
- `edit_note` modes — anything surprising in error messages.
- `move_note` — link rewrite correctness across all four wikilink shapes.
- `delete_note` — soft vs permanent behavior + DB cleanup lag.
- `set_frontmatter` — comment-loss observed on round-trip? body-byte preservation?
- Anything else worth flagging.
