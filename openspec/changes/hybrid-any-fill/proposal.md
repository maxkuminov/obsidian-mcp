## Why

GitHub #403, from Max's ergonomics pass on 2026-10-31.
`hybrid_search("My opinion on AI replacing software engineers")` returned
mostly job-posting notes, and the note that holds the opinion (*Essential
Skills in the Age of AI Agents*) came 5th. The keyword arm
(`keyword_arm_rows`) returns the conjunctive `match: all` block and then
the disjunctive `match: any` fill (#344). `fuse` ranks the two as one
list, so a fill row at keyword rank `r` scores `1/(k + r)`, exactly what
an all-terms row at the same position would. The fill is there so a
`keyword_search` page is not short, and it is labelled `match: any`.
Fusion drops that label. A note that matched one common word (`AI`,
`engineer`) then votes in the fusion as strongly as a genuine all-terms
match. This is the "silently wrong search result" failure CLAUDE.md
ranks second: an agent acts on the fused order and no person sees the
query.

## What Changes

- **Fill rows enter RRF with their keyword rank offset by the arm depth
  (owner decision: option B of #403).** When both arms ran, a keyword
  row tagged `match: any` contributes `1/(k + rank + 50)` instead of
  `1/(k + rank)`, where `rank` is still its 1-based position in the
  keyword arm. `50` is `HYBRID_ANY_FILL_OFFSET`, a constant defined as
  `HYBRID_ARM_DEPTH` next to `HYBRID_RRF_K`'s use in
  `src/services/hybrid.py`. It is not a setting and there is no per-call
  argument. Every conjunctive row has keyword rank ≤ 50, so its keyword
  term is ≥ `1/(k+50)`. Every fill row's term is ≤ `1/(k+51)`. A fill row
  therefore cannot outrank a conjunctive row through the keyword arm
  alone, for any `k`. Fill rows stay in the candidate set and still add
  their (smaller) term to a semantic hit on the same note, so recall at
  depth is unchanged. Conjunctive rows and semantic terms are not
  changed.
- **The tie-break is unchanged.** It is still fused score, then the
  smaller *unoffset* arm rank (`best_rank`), then path. The preview
  choice still compares unoffset ranks.
- **`explain` stays truthful.** A fill row's keyword term renders with
  the rank fusion actually used: `1/(60+55)` for `keyword_rank: 5
  (match: any, …)`. The row's existing `keyword_rank` and `match` fields
  already show that it is a fill row, so the header gains **no token**
  (design D4). The `hybrid_search` description gains one sentence that
  states the offset, and stays ≤ 4,000 characters.
- **The keyword-only path is byte-identical.** When the semantic arm is
  unavailable, no offset is applied: the page is the keyword arm in
  keyword order with its own terms, as it is today.
- **Rerank (#367)** still takes the first 50 fused rows. Under the new
  order, fill-only notes are the first to leave that window.
  **Confidence (#389)** is unchanged: `keyword_all` already counts
  conjunctive rows only.
- **Harness.** `make_arm_alt` gains the optional arm option
  `hybrid_alt_any_offset` (`0` or `50`, default `50`). With `0` it
  replays the incumbent fusion through a `ContextVar` override that only
  the harness sets. The release A/B is then one generation:
  `make_arm` (candidate) against `make_arm_alt` with
  `hybrid_alt_k=60 hybrid_alt_any_offset=0` as `BASELINE`.
- Docs: `docs/architecture/search.md` (fusion, bounds, harness),
  CLAUDE.md's `hybrid_search` bullet, one README sentence, and
  DEPLOYMENT.md's eval recipe (`hybrid_alt_any_offset`).

**Not a tool-contract change for the 1.0 freeze.** The input schema,
header tokens, row fields and their order are all unchanged. Fused
scores and order change on pages that fuse at least one `match: any`
row, as ranks have changed before (#344, #391); ranks were never
comparable across versions. The `explain` grammar (`1/(k+R)` terms) is
unchanged, and only the value of `R` on a fill row differs.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `hybrid-search`: the fusion requirement (the fill offset, the
  unoffset tie-break, the keyword-only exemption, the identity oracle);
  the rendering requirement (`explain` uses the fusion rank); the
  response-bound requirement (a fusion rank up to 100 in the worst-case
  structure, re-measured figures); the description requirement (states
  the offset).
- `search-evaluation`: the hybrid arm requirement (`make_arm_alt`'s
  `hybrid_alt_any_offset` option and the override seam).

`search-quality` states nothing about fusion (its keyword requirements
govern `keyword_search` and the arm core, both unchanged), so it has no
delta. The `reranker` spec's "first 50 fused rows" wording still holds.

## Impact

- Code: `src/services/hybrid.py` (`fuse`, the constant, the override
  `ContextVar`, `HybridRow.keyword_fusion_rank`);
  `src/mcp_server/tools.py` (`_hybrid_server_fields`' keyword term);
  `src/mcp_server/server.py` (`hybrid_search` description);
  `scripts/search_eval_arms/hybrid.py`.
- Tests: `tests/test_hybrid_search_offline.py`, `tests/test_hybrid_rerank.py`,
  `tests/test_hybrid_search_eval.py`, `tests/integration/test_hybrid_search_pg.py`,
  `tests/test_issue_343_tool_metadata.py` (description cap).
- No migration, no setting, no new dependency, no write path touched.
  Search-path change: the CLAUDE.md eval-gate row applies, and an
  adversarial Codex pass is mandatory (ranking an agent acts on unseen).
