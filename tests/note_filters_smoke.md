# note-filters smoke tests (client-side)

You are an MCP client connected to `<your-instance-host>`. Run each test below
**verbatim**, capture the response, and report PASS/FAIL with a one-line reason.
Do not improvise around failures — record them and move on.

The vault has 2,577 notes. Tag and frontmatter values used below were sampled from
the live index on 2026-04-25 and are stable enough for a smoke run; if a test
returns "no results" double-check that the index has been re-populated since deploy.

## Tools under test
`keyword_search`, `semantic_search`, `list_notes`, `get_recent`.

---

## 1. Baseline (regression: existing calls unchanged)

### 1.1 keyword_search without filters
- Call: `keyword_search(query="docker", limit=5)`
- Expect: returns up to 5 results with `rank` floats, sorted descending.
- PASS if: response starts with "Found N results for 'docker':" and lists notes.

### 1.2 semantic_search without filters
- Call: `semantic_search(query="ideas about productivity", limit=5)`
- Expect: returns up to 5 results with similarity scores between -1 and 1.
- PASS if: response starts with "Found N semantic matches".

### 1.3 list_notes without filters
- Call: `list_notes(limit=5)`
- Expect: 5 most recently modified notes from the vault root with size and date.
- PASS if: response shows 5 lines like `` - `path/to/note.md` (NNN B, modified YYYY-MM-DD) ``.

### 1.4 get_recent without filters
- Call: `get_recent(limit=5)`
- Expect: 5 most recently modified notes with title and tags.
- PASS if: response starts with "Last 5 modified notes:".

---

## 2. Tag filter (new)

### 2.1 keyword_search + tag
- Call: `keyword_search(query="today", tags=["journal"], limit=10)`
- Expect: every returned note's tag list includes `journal` (look at the `[tag1, tag2]` chunk in each line).
- PASS if: every result row shows `journal` in its bracketed tag list.
- Reference: 215 notes match this combination in the live DB; getting 0 means the filter is too strict (regression).

### 2.2 list_notes + folder + tag
- Call: `list_notes(folder="Daily Notes/", tags=["journal"], limit=10)`
- Expect: only notes under `Daily Notes/` (path starts with `Daily Notes/`).
- PASS if: every `path` starts with `Daily Notes/` and the count is ≤ 10.
  (The tag filter is not visible in the list_notes response, so verify by spot-reading one note.)

### 2.3 get_recent + tag
- Call: `get_recent(limit=10, tags=["journal"])`
- Expect: each result line shows `[journal, ...]` in the bracketed tag list.
- PASS if: every result has `journal` in tags.

### 2.4 Tag filter with multiple tags (AND semantics)
- Call: `get_recent(limit=10, tags=["journal", "concept"])`
- Expect: every result has BOTH `journal` AND `concept` in its tag list. May return 0 if no overlap exists (acceptable, but flag).
- PASS if: every returned row has both tags. If 0 results, also acceptable (AND can be empty).

### 2.5 Empty tag list is a no-op
- Call: `keyword_search(query="docker", tags=[], limit=5)`
- Expect: identical results to test 1.1 (`tags=[]` short-circuits the filter).
- PASS if: same set of paths as 1.1.

---

## 3. Frontmatter filter (new)

### 3.1 semantic_search + frontmatter
- Call: `semantic_search(query="application status", frontmatter={"status": "applied"}, limit=10)`
- Expect: at most 10 results, all from notes whose YAML frontmatter has `status: applied`.
- PASS if: response is non-empty (10 such notes exist in the live DB).
  Spot-check one result with `read_note(path)` and confirm its `frontmatter` field shows `status: applied`.

### 3.2 keyword_search + frontmatter
- Call: `keyword_search(query="role", frontmatter={"status": "active"}, limit=5)`
- Expect: results restricted to notes with `status: active` in frontmatter (16 such notes total in live DB).
- PASS if: 0 ≤ N ≤ 5 results, all of which (when read with `read_note`) have `frontmatter` showing `status: active`.

### 3.3 Strict type matching (negative test)
- Call: `keyword_search(query="role", frontmatter={"status": 0}, limit=5)`
- Expect: 0 results (strict type — string `"0"` ≠ integer `0`, and no note has `status: 0` numeric).
- PASS if: response says "No results for 'role'" or returns an empty/zero-result message.

### 3.4 Empty frontmatter dict is a no-op
- Call: `keyword_search(query="docker", frontmatter={}, limit=5)`
- Expect: identical to 1.1.
- PASS if: same set of paths as 1.1.

---

## 4. Combined filters

### 4.1 Folder + tag + frontmatter
- Call: `list_notes(folder="Projects/", tags=["log/p/KAI"], limit=20)`
- Expect: returns notes under `Projects/` tagged with `log/p/KAI`. Live DB has 151 such notes; 20 is enough to verify intersection.
- PASS if: every `path` starts with `Projects/` and (spot-check one) has the tag.

### 4.2 Filter that should yield nothing
- Call: `keyword_search(query="zzznonsensequery", tags=["journal"])`
- Expect: 0 results.
- PASS if: response is "No results for 'zzznonsensequery'".

---

## 5. Symmetric, non-ranking docstrings

### 5.1 Tool listing
- Call: list MCP tools (whatever your client uses for `tools/list`).
- Inspect the descriptions for `keyword_search` and `semantic_search`.
- PASS if:
  - Neither description contains the word "primary".
  - `keyword_search` mentions exact identifiers / code symbols / proper nouns / known phrases AND mentions `semantic_search` as the alternative.
  - `semantic_search` mentions conceptual / paraphrased queries AND mentions `keyword_search` as the alternative.

---

## 6. Sanity (existing tools should still work)

Quick yes/no calls — each should return a non-error response:

- `read_note(path="<one of the paths from test 2.2>")` → `content` carries the note body.
- `get_tags(limit=10)` → returns top 10 tags.
- `get_vault_guide()` → returns the Obsidian primer plus CLAUDE.md contents (or onboarding text if absent).
- `create_note` and `edit_note` — only run if you have a readwrite key; otherwise skip.

PASS if all four (or three, if no rw key) succeed.

---

## Reporting

When done, return a markdown table:

| ID  | PASS/FAIL | Notes |
|-----|-----------|-------|
| 1.1 | PASS      |       |
| 1.2 | PASS      |       |
| ... | ...       |       |

Then a one-paragraph summary covering:
- Regressions (any baseline test that broke).
- New filter behavior (tag, frontmatter, combined).
- Anything surprising (response shape changes, error messages).
