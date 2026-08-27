# wikilink-graph-navigation smoke tests (client-side)

You are an MCP client connected to `<your-instance-host>`. Run each test below
**verbatim**, capture the response, and report PASS/FAIL with a one-line reason.
Do not improvise around failures — record them and move on.

This script verifies the five new graph-navigation tools added by the
`wikilink-graph-navigation` change: `get_backlinks`, `get_links`,
`get_neighborhood`, `find_related`, `find_orphans`. Reference fixtures were
sampled from the live vault on 2026-04-25; if a fixture path or target name
has changed, substitute a comparable one and note it in the report.

## Tools under test
`get_backlinks`, `get_links`, `get_neighborhood`, `find_related`, `find_orphans`.
Plus a brief regression check on existing tools to confirm the schema migration
and indexer changes did not break anything.

## Vault fixtures used in this script

| Fixture                                               | Why it's useful                                         |
|-------------------------------------------------------|---------------------------------------------------------|
| `Cards/Weekend Outings Menu.md`                       | Hub: ~125 outgoing wikilinks. Stress test for `get_links`. |
| `Cards/The AGI Transition - Hub Note.md`              | Concept hub: ~36 outgoing links. Good for `get_neighborhood`. |
| `[[All Projects - Activity]]`                         | Most-referenced target (131 incoming). Use for `get_backlinks`. |
| `[[Edward Kuminov]]`                                  | Person node, 90 incoming. Good second backlinks test.    |
| `[[Alice Kuminov]]`                                   | Person node, 56 incoming. Symmetric sanity check.        |

> If any of these aren't reachable in the index, fall back to whatever
> `find_orphans(limit=1)` returns (any path) for null-result baseline tests.

---

## 0. Pre-flight (deploy + migration)

### 0.1 Health endpoint
- Call: `GET https://<your-instance-host>/health` (use the public probe; no auth).
- PASS if: `{"status":"ok"}`.

### 0.2 Tool listing exposes the five new tools
- Call: `tools/list` via your MCP client.
- PASS if: the listed tool names include `get_backlinks`, `get_links`, `get_neighborhood`, `find_related`, `find_orphans`.

### 0.3 Link backfill has completed
- Call: load `https://<your-instance-host>/admin/` (admin key required).
- Inspect the Graph widget for `total_links` and `dangling_links`.
- PASS if: `total_links` is non-zero (the backfill ran). If a "Link extraction in progress" indicator is showing, wait until it disappears and retry.
- Record the four numbers (`total_links`, `dangling_links`, `orphan_count`, top hub list) — they're referenced again in test 4.1.

---

## 1. `get_links` — outgoing edges

### 1.1 Hub note returns its outgoing links
- Call: `get_links(path="Cards/Weekend Outings Menu.md")`
- Expect: a long list (around 125 entries based on raw markdown).
- PASS if:
  - Response contains ≥ 50 links (some may dedup if the note links the same target multiple times — that's fine).
  - Each row has at least `target_path` (or `target_title`) and a `resolved` flag (true/false).
  - At least one row has `resolved=true`.

### 1.2 Resolved vs dangling distinguished
- From the 1.1 response, count rows where `resolved=false`.
- PASS if: response includes the distinction (a `resolved` boolean or equivalent dangling marker). It is acceptable for the hub note to have zero dangling links — but the field must exist.

### 1.3 Leaf note with no outgoing links
- First, find a leaf: call `find_orphans(limit=1)` and take the path it returns. If `find_orphans` returns 0 rows (vault has no orphans), pick any note from `list_notes(folder="Daily Notes/", limit=1)` instead.
- Call: `get_links(path="<leaf-path>")`
- PASS if: returns 0 rows OR a "no outgoing links" message. Either response shape is acceptable as long as the call doesn't error.

### 1.4 Non-existent path
- Call: `get_links(path="DoesNotExist.md")`
- PASS if: response is a clear "note not found" error message, NOT an unhandled exception.

---

## 2. `get_backlinks` — incoming edges

### 2.1 Highly-referenced target
- Call: `get_backlinks(path="All Projects - Activity.md", limit=20)` — note the `.md` suffix; the path resolves whatever the indexer stored as the canonical file path. If the path is wrong, try `get_backlinks(path="Atlas/All Projects - Activity.md", ...)` or whatever `keyword_search(query="\"All Projects - Activity\"", limit=1)` returns.
- Expect: 20 rows back (capped by `limit`).
- PASS if:
  - Response has 20 rows.
  - Each row carries source `path`, `title`, and the `link_text` or surrounding context.
  - At least 3 distinct source paths (the target is referenced 131 times across many sources).

### 2.2 Person-node backlinks
- Call: `get_backlinks(path="<resolved Edward Kuminov path>", limit=10)`
  Use `keyword_search(query="\"Edward Kuminov\"", limit=1)` to discover the canonical path if needed.
- PASS if: ≥ 5 backlinks returned, each from a distinct source path.

### 2.3 Note with zero backlinks
- Pick any orphan path (`find_orphans(limit=1)` from test 1.3) or a note clearly not referenced.
- Call: `get_backlinks(path="<orphan-path>")`
- PASS if: returns 0 rows OR a "no backlinks" message — not an error.

### 2.4 limit honored
- Call: `get_backlinks(path="<same path as 2.1>", limit=5)`
- PASS if: exactly 5 rows returned.

---

## 3. `get_neighborhood` — bounded BFS

### 3.1 Default depth (=1)
- Call: `get_neighborhood(path="Cards/The AGI Transition - Hub Note.md")`
- Expect: returns notes one hop away (links + backlinks, undirected).
- PASS if:
  - Response includes ≥ 5 distinct paths (a 36-link hub note will have a sizable 1-hop neighborhood).
  - Each row carries `distance` (should all be `1`) and either `via` (predecessor path) or equivalent.
  - The source note itself is NOT in the result list.

### 3.2 depth=2 expands further
- Call: `get_neighborhood(path="Cards/The AGI Transition - Hub Note.md", depth=2, limit=100)`
- PASS if:
  - More distinct paths than 3.1 (or equal, if 3.1 already saturated `limit`).
  - At least one row has `distance=2`.
  - Total result count ≤ 100.

### 3.3 limit truncation
- Call: `get_neighborhood(path="Cards/Weekend Outings Menu.md", depth=2, limit=10)`
- PASS if: exactly 10 rows returned and the response notes that the result was truncated (per the design's "truncation flag" decision).

### 3.4 limit clamped at 200
- Call: `get_neighborhood(path="Cards/Weekend Outings Menu.md", depth=3, limit=999)`
- PASS if: response has ≤ 200 rows (the tool clamps internally) — even if the neighborhood is larger.

### 3.5 Isolated note
- Call: `get_neighborhood(path="<orphan path from 1.3>")`
- PASS if: returns 0 rows or "no neighbors" message — not an error.

---

## 4. `find_orphans` — vault hygiene

### 4.1 Counts match dashboard
- Call: `find_orphans(limit=500)` (or whatever max is allowed).
- Compare row count to the `orphan_count` reported by the dashboard widget (test 0.3).
- PASS if: counts match exactly. If the count exceeds the limit, response should indicate truncation.

### 4.2 Folder filter
- Call: `find_orphans(folder="Daily Notes/")`
- PASS if: every returned `path` starts with `Daily Notes/`.

### 4.3 Returned notes really are orphan
- Pick one path from 4.1's response.
- Call `get_links(path="<orphan-path>")` and `get_backlinks(path="<orphan-path>")`.
- PASS if: BOTH return 0 rows. (An "orphan" must have neither incoming nor outgoing resolved links.)

### 4.4 Empty list is OK
- If the vault has zero orphans, the response should clearly say so (e.g. "No orphan notes found"), not return an error.

---

## 5. `find_related` — embedding-based neighbors

### 5.1 Well-embedded note
- Pick a note that's clearly indexed and embedded — e.g. `Cards/The AGI Transition - Hub Note.md`.
- Call: `find_related(path="Cards/The AGI Transition - Hub Note.md", limit=10)`
- PASS if:
  - 10 results returned.
  - Each row has `path`, `title`, `similarity` (float, typically 0.4–0.9).
  - The source note is NOT in the result list.
  - At least one result is conceptually related (spot-check by name / topic).

### 5.2 limit honored
- Call: `find_related(path="Cards/The AGI Transition - Hub Note.md", limit=3)`
- PASS if: exactly 3 results.

### 5.3 Unembedded note (negative path)
- Find a note that hasn't been embedded yet — easiest way is to pick the most-recently-indexed note via `get_recent(limit=1)`. If embeddings catch up fast, this test is opportunistic; if it returns results, that's fine, just record the case.
- Call: `find_related(path="<recent-path>")`
- PASS if: returns results OR a clear "not yet embedded" message — not an unhandled exception.

### 5.4 Source excluded from results
- Run 5.1 again and verify the source path is absent from every result.
- PASS if: no row has `path == source_path`.

---

## 6. Resolution / dangling-link behavior

### 6.1 Create a note that was previously a dangling target
- Pick a target name that currently appears in dangling links (use `get_links` on a hub note to find a `resolved=false` row, e.g. `target_path = "Foo Bar"`).
- Call: `create_note(path="Foo Bar.md", content="# Foo Bar\n\nplaceholder")` (requires readwrite key).
- Wait one indexing interval (≤ 5 minutes) — or trigger reindex via the panel if available.
- Call: `get_links(path="<the hub note that referenced Foo Bar>")` again.
- PASS if: the row that pointed at "Foo Bar" now shows `resolved=true` and `target_path` resolved to `Foo Bar.md`.
- After the test, optionally clean up: `edit_note` or remove the placeholder.
- If you don't have a readwrite key, SKIP this test and note it.

### 6.2 Delete cascade (skip if rw key not available)
- Create a throwaway note: `create_note(path="Cards/_smoke_link_target.md", content="# smoke")`.
- Wait for indexing.
- Create another note linking to it: `create_note(path="Cards/_smoke_link_source.md", content="# smoke source\n\nSee [[_smoke_link_target]].")`.
- Wait for indexing.
- Call `get_backlinks(path="Cards/_smoke_link_target.md")` → expect 1 row.
- Now delete `_smoke_link_target.md` from the vault (filesystem) and wait for indexing.
- Call `get_backlinks(path="Cards/_smoke_link_target.md")` → expect "note not found" error.
- Call `get_links(path="Cards/_smoke_link_source.md")` → expect the row that referenced it now shows `resolved=false` (NULLed `target_note_id` per ON DELETE SET NULL).
- Clean up: delete `_smoke_link_source.md` from the vault.

---

## 7. Existing-tool regression check

The migration adds a new table and the indexer gains a link extraction pass —
neither should affect existing behavior. Quick sanity:

- `keyword_search(query="docker", limit=3)` → returns results.
- `semantic_search(query="ideas", limit=3)` → returns results.
- `list_notes(limit=3)` → returns 3 recent notes.
- `get_recent(limit=3)` → returns 3 recent notes.
- `read_note(path="HOME.md")` → `content` carries the note body.
- `get_tags(limit=5)` → returns 5 tags.

PASS if all six return non-error responses.

---

## Reporting

When done, return a markdown table:

| ID  | PASS/FAIL/SKIP | Notes |
|-----|----------------|-------|
| 0.1 | PASS           |       |
| 0.2 | PASS           |       |
| ... | ...            |       |

Then a one-paragraph summary covering:
- Backfill state (did the link table populate?).
- Each tool's behavior (one short bullet each).
- Resolution + dangling lifecycle (test 6.x) — does deletion correctly NULL the target?
- Anything surprising (response shape, limit clamping, error messages).
