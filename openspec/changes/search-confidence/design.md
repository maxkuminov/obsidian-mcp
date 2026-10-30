# Design: search-confidence (#389, recall@20/50)

## Context

`hybrid_search` (#354) and `semantic_search` always return a full page. The semantic arm returns its nearest neighbours however distant they are, so when the keyword arm has nothing either, nothing on the page says the list is noise. The owner's framing (2026-10-30): the search tools are investigation entry points. What the product needs is recall at depth and honest signals about the candidate set, more than precision at rank one. This change adds the honesty signal and two depth metrics. The hint never costs a row; the token is a header token like `arm_depth` and can, at the cap boundary, cost one row, disclosed by the cap footer (L14). Nothing is filtered or reordered.

It ships **hard-off** (threshold 0, Codex round 1 M1). The token appears on every page from day one and reads `ok` until a deployment calibrates a threshold. From day one the usage rows record the evidence that calibration needs.

### Live numbers (bge-m3, 2026-10-30, production index mid-rechunk: 1,783 notes not yet re-chunked)

| Query | Tool | `match: all` | Best similarity |
| --- | --- | --- | --- |
| `xqzvplorth` (#389 report) | hybrid | 0 | 0.36–0.49 across five rows |
| `xqzvplorth` (re-run while drafting) | hybrid | 0 | 0.467 (then 0.464, 0.460) |
| "how do I keep an agent from overwriting a note it did not read first" | semantic | — | 0.618 (then 0.618, 0.611) |
| real paraphrases (operator probes) | semantic | — | 0.62–0.67 |

These are evidence for a *candidate* value of 0.50, not a calibration. They contain no short identifier with no all-terms match, which is exactly the false-`low` case that matters.

## Decisions

### D1. One rule, two tools

`src/services/search_confidence.py` holds a pure function, `confidence(keyword_all: int, top_similarity: float | None, threshold: float) -> "ok" | "low"`:

```
low  ⇔  threshold > 0  ∧  keyword_all == 0  ∧  (top_similarity is None ∨ top_similarity < threshold)
```

**Evidence is `keyword_all`, the `match: all` note count** (round 1 B2). A query whose terms all match a note has lexical evidence. A disjunctive `any` fill on one common word (`xqzvplorth notes` → rows for `notes`) is filler, not evidence. `semantic_search` has no keyword arm and passes 0.

**Edge cases.**
- The boundary is strict: a similarity exactly at the threshold reads `ok`.
- A threshold of 0 is a hard off, so a negative cosine cannot fire it.
- `hybrid_search`'s keyword-only path passes `top_similarity=None`. A keyword-only page reads `ok` with an all-terms match and `low` without one (when the threshold is above 0).

**`top_similarity` is defined over the call's actual candidate set** (round 1 B3, M6). No statement changes, and no service return type changes:

- **`semantic_search`** (both encodings): the similarity of the first row of the ordered, deduplicated list the call already fetched, through the existing `max(5 × limit, 50)`-chunk overfetch, before rerank and the cap. The `rerank=false` tool reads `results[0]["similarity"]` from the unchanged `list[dict]`. The `rerank=true` tool reads `candidates[0]` before calling the reranker.
  - **Limit can matter.** The overfetch grows with `limit`, so on the relaxed-order HNSW scan a larger `limit` can surface a better note and flip the token at the margin (L12).
- **`hybrid_search`**: the max similarity over its semantic arm's deduped notes (`s_rows`, the existing 50-chunk overfetch, independent of `limit`), computed in `src/services/hybrid.py` and carried as a new field `HybridResult.top_similarity`. `HybridResult` is the hybrid capability's own dataclass, already carrying `keyword_all`. The search-quality requirement that the *semantic/keyword* service return types stay unchanged is untouched.

**Filters are part of the question.** A folder scope with only weak matches reads `low`, which is the honest answer for that scope. `keyword_search` gets no token, because a keyword hit is lexical evidence and it has no similarity. `find_related` gets none, because its similarity is note-to-note.

### D2. The threshold ships at 0; calibration is a recipe, not a guess

`SEARCH_LOW_CONFIDENCE_SIMILARITY` is a float in [0, 1], default **0**, non-finite refused, read at startup, never a call argument. It changes no ranking, so it is not part of `retrieval_fingerprint()`, and the docs place it beside the fingerprint text because it is model-dependent: OpenAI `text-embedding-3-*` cosines run well below bge-m3's.

**Why not 0.50 at ship.** The asymmetry argument still holds: a false `low` pushes an agent away from the right notes, while a missing `low` is merely today's behaviour. That argument only makes a default *safe* if the false-`low` rate has been measured, and the live probes never tested a short identifier with no all-terms match. So the default is off. 0.50 is documented as bge-m3's candidate value.

**The calibration recipe** (in `DEPLOYMENT.md`, normative per the search-quality delta):

1. Run with the threshold at 0 for at least 14 days **and** until **each** tool has at least 200 token-bearing clicked-through pages. Usage rows accrue `confidence`, `top_similarity` and, on `hybrid_search`, `keyword_all`.
2. Run `make search-eval-export USER_ID=…`, `mine`, then `python -m scripts.search_eval confidence --data … --threshold 0.50`. This tabulates per tool and per query class (the miner's `identifier` / `prose`; the miner has no third class) and splits clicked-through pages (a shown note was read) from the rest. A row is post-change when its `confidence` key is recorded; a keyword-only page (null `top_similarity`) is included and, with `keyword_all = 0`, counts as `low`. The output carries the mine provenance; only `attachable: true` output may leave `.search-eval/`.
3. Pick the threshold at the noise ceiling of the non-clicked distribution, and **accept it only if both tools' lines read PASS**: false-low rate on clicked-through pages below 1 in 50, per tool, with no aggregate. Fewer than 14 days or 200 clicked-through pages (zero included) is `INSUFFICIENT_DATA`, never a pass. One setting enables both tools, so both lines must pass.
4. Set it, recreate, and re-run the tabulation after another period.

**The reference deployment sets 0.50 (or whatever the tabulation picks) only after both lines pass in step 3.**

### D3. Token placement

The token is on **every page** and is part of the base budget, like `arm_depth` (round 1 B1). There is no token-less page to compare against, so it is never a row-cost question.

- **`hybrid_search`**: directly after `arms: …`, and after `reranked: …` on `rerank=true`. For example: `Found N hybrid results for "q" — arms: both — confidence: low — rrf_k: 60 — …`. The zero-result line and the keyword-only path carry it too.
- **`semantic_search(rerank=false)`**: `Found N semantic matches for 'q' — S stale, T truncated — confidence: ok:`. The zero-result line is `No semantic results for 'q' (embeddings may still be building) — confidence: …`.
- **`semantic_search(rerank=true)`**: after the stale/truncated counts, before `reranked:`.

**A refusal is not a page** (round 1 M4). `semantic_search`'s provider-input rejection (`provider_input_rejected`) stays the existing refusal, with no header, no token and no hint, and its usage row has no `confidence` or `top_similarity`. The same holds for every pre-body refusal. `hybrid_search` has no such refusal, because a provider rejection there is its keyword-only *page*, which carries the token.

**Forgery.** The token is server text in the header line, and no note text precedes the header. The hybrid and rerank encodings quote every note field. The legacy semantic render's raw titles (#367 L9) sit on row lines, not the header.

### D4. The hint line is conditional and never costs a row

Each tool has one fixed constant, at most 140 characters (`CONFIDENCE_HINT_MAX`):

- hybrid: ``low confidence: no keyword match on all terms and weak similarity; try `keyword_search` with an identifier, or `get_neighborhood` from a known note``
- semantic: ``low confidence: weak similarity and no keyword arm; try `keyword_search` with an identifier, or `get_neighborhood` from a known note``

**Placement.** The hint goes after the header, after the `index_transition:` line when present, and before the first row. On a zero-result response it follows the zero-result line and the transition line, and precedes hybrid's keyword-only footer. #355's "transition line directly after the header" contract is untouched.

**Rule (round 1 B1).** The renderer first renders the page without the hint. It then renders it with the hint reserved, and keeps the hint only if that second rendering shows the same paths in the same order with byte-identical row lines. Otherwise the hint is withdrawn **before** any abbreviation rung or row drop. Precedence is therefore: withdraw the hint, then the existing abbreviation ladder, then drop rows.

**Cost and safety.** The cost is one extra render on a `low` page only. The `legacy` `semantic_search(rerank=false)` page has no character budget, so its hint is unconditional there. The hint says "try", never "ignore". It never begins with `- `, so row parsers skip it.

### D5. The bound, re-measured at 425bc22

The numbers below come from rendering the existing worst-case fixtures with the token appended (17 characters for `ok`, 18 for `low`). The low-plus-`any` case has 50 `match: any` rows, `all 0`, and the snippet-cap token. Its header measures **exactly equal** to the `ok` worst case, because `all 0` is one character shorter than `all 50` and `low` is one longer than `ok`. The hint never enters the bound (D4). The implementation re-measures every number and the tests pin each one, for `ok` and low-plus-`any` separately.

| Case (floor 1,000, `k=1000`, 8,192-control query) | Before | After (`ok` = low-plus-`any`) |
| --- | --- | --- |
| hybrid header worst, `rerank=false` | 388 ≤ 400 | 405 ≤ **405** |
| hybrid header worst, `rerank=true` | 427 ≤ 431 | 444 ≤ **444** |
| semantic `rerank=true` header worst | 181 ≤ 185 | 199 ≤ **199** |
| hybrid floor arithmetic (`rerank=false`) | 985 | 405+340+45+45+75+75+5 = **990** |
| keyword-only, `rerank=false`, cut form | 959 | 976 |
| keyword-only, `rerank=true`, cut / omitted form (actual footer 71) | 986 / 967 | **1,003** / 984 |
| both arms + ten-digit transition, `rerank=false`, cut form | 977 | 994 |
| both arms + transition, `rerank=true`, omitted form | 985 | **1,002** → `explain omitted` 983 |

**`confidence: ok` alone breaks the 1,000 bound** on the both-arms `rerank=true` page with the transition line. One rung is therefore appended to `hybrid_search`'s first-row ladder, after the existing omitted forms: **`explain omitted`**. The `explain` field is replaced by that label, saving 19 characters. Nothing is lost, because `explain` is the `1/(k+rank)` terms computable from the row's ranks and the header's `rrf_k`.

**Cut floor.** The 20–25 `rerank=true` cut was already not guaranteed on the both-arms + transition case (#367/#355). Now the keyword-only `rerank=true` worst case (cut form 1,003) falls through to the labelled omitted form as well. The bound holds in both. These cases require a 1,000-character cap (the default is 40,000), an 8,192-control-character query and a ten-digit pending count.

**Normative scope (round 1 M2).** The hybrid-search delta states 405 for `rerank=false` and 444 for `rerank=true`, for both `ok` and low-plus-`any`.

### D6. Rerank

The token is computed before rerank (D1), so `rerank=true` and `rerank=false` carry the same value for the same call. Using a cross-encoder score to inform confidence is out of scope: rerank is opt-in and unmeasured (#367).

The `reranker` capability's byte-identity requirement is **modified** (round 1 M3). The `rerank=false` baseline becomes the search-confidence contract's page: today's page plus the token, plus the conditional hint when `low`. Its scenario states the exact inserted bytes.

### D7. Usage rows

Both tools record, through the timing holder, on every **page** (zero-result and keyword-only included) and **whatever the threshold**:

- `confidence`: `"ok"` or `"low"`;
- `top_similarity`: three decimals, whenever a candidate existed;
- on `hybrid_search`, `keyword_all` (int).

None of these is an `error` marker. Refusals carry none of them (D3). To make D2's tabulation possible, the miner writes a per-search-row artifact, `confidence-rows.jsonl`, under the data directory. It holds `tool`, `class`, `confidence` (the recorded key; absent marks a pre-change row), `created_at` (UTC, from the usage row; the tabulator computes each tool's span from it), `top_similarity`, `keyword_all` and `clicked` (whether the miner credited a judgment to that search row), with no query text and no path. The `confidence` subcommand reads it, connects to no database, writes counts and rates only, and carries the mine provenance and `attachable` flag (non-attachable output begins with the `NOT ATTACHABLE` line).

### D8. Harness: recall@20 and recall@50

- **Metrics.** `METRICS` becomes `("recall@5", "recall@10", "recall@20", "recall@50", "mrr", "ndcg@10")`. They use the same `recall_at`, are reported per arm, class and source, and get baseline deltas with the seeded 1,000-resample bootstrap CI. The table gains `R@20` and `R@50` columns.
- **Depth.** `DEFAULT_DEPTH` goes from 20 to 50. Fifty is the tools' maximum `limit` and `hybrid_search`'s arm depth, so recall@50 is the ceiling of one page. Replay cost rises modestly: the `keyword` and `semantic` arms pass `limit=depth`, while the hybrid and rerank arms already use `min(depth, 50)`.
- **`staged_depth`** (round 1 M5). The runner records it per arm. For a database-backed arm it is the generation's depth. For a `runfile:` arm it is the minimum of the generation's depth and the source metadata's own `depth`, which the runner preserves and validates (missing, non-integer or non-positive → exit 64). Today the import overwrites the effective depth with the generation's. Scoring nulls every recall@k with k above the arm's `staged_depth`, together with its deltas and CIs. The import path gets an end-to-end test.
- **Report and golden.** `schema_version` goes from 1 to 2, and the report gains `staged_depth` per arm. The synthetic golden is regenerated **once**, with the reason in the commit message. Its diff must contain only the new metric keys, `staged_depth`, depth and schema version.
- **Unchanged.** The verdict text and the no-threshold rule stay as they are, and `novel@10` stays at ten.

### D9. 1.0 contract

The change is additive:

- one header token on two tools;
- one optional, conditional line that never begins with `- `;
- three usage keys and one harness subcommand;
- for `hybrid_search` at the 1,000-character floor, one labelled abbreviation rung.

No row content, field, ordering, argument, annotation or tool changes; at the cap boundary a page may show one row fewer, disclosed by the existing footer (L14). With the default threshold 0, the only byte change on any existing page is ` — confidence: ok`. The reranker's byte-identity baseline is re-based in its spec (D6). The two tool descriptions gain one sentence each and stay ≤ 4,000.

## Owner decisions (2026-10-30, round 1 included)

- O1. The token is `confidence: ok | low`, always present on every page of `semantic_search` and `hybrid_search`, part of the base budget.
- O2. `low` means: `keyword_all == 0` (hybrid) or no keyword arm (semantic), and best similarity below the threshold. `0` never fires.
- O3. The hint text is the owner's, amended to "no keyword match on all terms" (B2). It is conditional and withdrawn before it can cost a row (B1).
- O4. Nothing is filtered: there is no similarity floor. The hint never costs a row. The token is a header token with no compaction; a boundary row it displaces is disclosed by the cap footer (round 2).
- O5. The default is 0 at ship. Calibration follows the DEPLOYMENT.md recipe with a < 1-in-50 false-low acceptance line, and the reference deployment sets a value only after it passes (M1).
- O6. `top_similarity` is taken over the actual candidate set, and the limit-invariance claim is dropped (B3). Service return types are unchanged (M6).
- O7. A provider-input rejection on `semantic_search` is a refusal with no token (M4).
- O8. The harness adds recall@20/50 with default depth 50, a per-arm `staged_depth` with nulling, one golden regeneration, and a calibration tabulation. The gate stays a report.
- O9. No migration.

## Accepted limitations

- L1. **The candidate value 0.50 rests on a handful of probes** taken on an index mid-rechunk. Hence the hard-off default and the recipe.
- L2. **All-terms evidence can be lexical coincidence.** A gibberish query whose every term happens to match a note reads `ok`. The token claims no more than "an all-terms keyword match exists".
- L3. **`semantic_search` judges on similarity alone.** A genuine query about a topic covered only in short or foreign-language notes can read `low` once a threshold is set. The rows are all still there.
- L4. **One global threshold**, with none per folder or tag. A filter changes the best similarity by design.
- L5. **A stale best row counts**, although its similarity is against superseded content (#200).
- L6. **At the 1,000-character floor, the 20–25 `rerank=true` cut is not guaranteed** on two worst cases. The labelled omitted form and `explain omitted` take over, and the bound holds.
- L7. **On a tightly capped page the hint is often withdrawn.** The token stays.
- L8. **The keyword-only empty page cites similarity it never measured.** It reads `low` with the hybrid hint, whose "weak similarity" clause refers to a semantic arm that did not run. The keyword-only footer says why.
- L9. **The report does not include the false-low rate.** The calibration tabulation is a separate subcommand, not part of the search-eval report or its verdict.
- L10. **Old shallow runfiles get null recall@50.** A `runfile:` staged shallower than 50 has null recall@50, so it must be re-run for a delta.
- L11. **`confidence` is not part of the miner's query identity.** It is an output.
- L12. **On `semantic_search`, `limit` can flip the token at the margin**, because the overfetch is `max(5 × limit, 50)` chunks. `hybrid_search`'s arm is fixed at 50 and is limit-invariant.
- L13. **The miner has two query classes**, `identifier` and `prose`. The recipe tabulates by those two, not by a separate "mixed" class.
- L14. **The token can cost a boundary row.** It is reserved like `arm_depth`, with no compaction (owner decision, round 2). On a capped `hybrid_search` or `semantic_search(rerank=true)` page whose last row fitted before this change with fewer than 17–18 characters to spare, that row is now omitted, and the existing cap footer states the count (`more: true` on `rerank=true`). Under the default 40,000-character cap that requires a page within 18 characters of the cap.
- L15. **One setting enables both tools**, so a deployment that rarely uses one tool cannot pass that tool's accrual and cannot enable either until it does.

## Risks

- **A false `low` on a good page.** This is the expensive error. It is mitigated by the hard-off default, the per-tool measured acceptance lines before a value is set, the guarantee that no candidate is filtered or reordered (a token-displaced boundary row is reported by the cap footer, L14), and a hint that says "try". The adversarial pass is asked to attack this.
- **A silent `ok` on noise.** This is today's behaviour, and it is guaranteed until calibration.
- **A bound regression.** Every D5 case is a pinned test for both values of the token. The hint cannot enter the bound, and the new rung comes after every existing rung.
- **A model change without a threshold change.** The docs say to re-calibrate, and 0 is the shipped value.

## Open questions (only Max can answer)

- Q1. (Decided in round 2, no longer open: accrual is at least 14 days **and** at least 200 clicked-through pages per tool, as the search-quality setting requirement and the search-evaluation tabulation requirement state.)
- Q2. Is 1-in-50 the right false-low acceptance line, or should identifier queries have a stricter one?
- Q3. Should the depth default of 50 apply now? Old `runfile:` baselines get a null recall@50.

## Codex spec review, round 1 (2026-10-30)

Verdict FAIL (3 BLOCKER, 6 MAJOR), all accepted and folded in:

- **B1, token/hint versus row preservation.** The token is on every page and is base budget. The hint is conditional and withdrawn before it changes the shown path list or any row line (D4). A cap-boundary scenario was added.
- **B2, evidence.** Evidence is `keyword_all > 0`. The hint text is amended, the low-plus-`any` bounds were re-measured (equal to `ok`), and the rule table and the `xqzvplorth notes` case were added.
- **B3, limit-invariance.** `top_similarity` is taken over the call's actual candidate set, the invariance claim is dropped, and L12 records the consequence.
- **M1, uncalibrated default.** The default is 0. `top_similarity` and `keyword_all` are always recorded, and the DEPLOYMENT.md recipe adds the `confidence` tabulation and the 1-in-50 acceptance line.
- **M2, header scope.** 405 applies to `rerank=false`, and a normative 444 applies to `rerank=true`, for both `ok` and low-plus-`any`.
- **M3, reranker byte-identity.** A MODIFIED `reranker` delta re-bases the baseline.
- **M4, provider-input rejection.** It is a refusal with no token or keys. "Every response" became "every page".
- **M5, staged depth.** A per-arm `staged_depth` is preserved and validated from the runfile source, with an end-to-end import test.
- **M6, service return types.** They are unchanged. The tool reads the first row, and hybrid computes the value from its semantic arm's rows onto its own result.

## Codex spec review, round 2 (2026-10-30)

B2, B3, M1 (default), M2–M6 verified. B1 not fixed, plus two new MAJORs, all folded in by owner decision:

- **B1, the token can evict a boundary row.** No compaction is added. The "token drops no row" promise is removed: the token is a header token like `arm_depth`, and at the cap boundary a page may show one row fewer than before this change, disclosed by the existing cap footer (L14). A scenario now asserts the footer's count and `more: true`. The hint keeps its row-neutral rule.
- **M1, calibration gate.** Accrual is at least 14 days **and** at least 200 token-bearing clicked-through pages per tool. The tabulation prints one line per tool, with no aggregate, and zero or insufficient denominators are `INSUFFICIENT_DATA`. Activation requires PASS on both lines (one setting enables both tools, L15). Post-change rows are identified by the recorded `confidence` key, so keyword-only pages with null `top_similarity` are included, and a clicked-through keyword-only `low` counts as a false low.
- **M2, provenance.** The tabulation inherits the mine provenance and `attachable` flag and prints `NOT ATTACHABLE` for multi-user data. Only attachable output may leave `.search-eval/`. The tests cover the two-user fixture (not attachable) and the `USER_ID=` fixture (attachable).

## Codex spec review, round 3 (2026-10-30)

M2 verified. B1 and M1 were not fixed in text only, and this commit closes both. Verified by the supervisor; there is no round 4.

- **B1.** The design's risk line no longer says rows are never dropped. It states the actual guarantee: no candidate is filtered or reordered, and a token-displaced boundary row is reported by the cap footer (L14). I aligned the rest of the change's phrasing to match.
- **M1.** Each `confidence-rows.jsonl` line now carries `confidence` (absent on a pre-change row) and `created_at` (UTC) alongside `tool`, `class`, `top_similarity`, `keyword_all` and `clicked`. The 20-day-span and pre-change-row scenarios pin that both are preserved, and the tabulator computes each tool's span from `created_at`. Q1 now records the decided accrual rule.
