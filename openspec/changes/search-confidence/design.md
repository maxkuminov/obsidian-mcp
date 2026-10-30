# Design: search-confidence (#389, recall@20/50)

## Context

`hybrid_search` (#354) and `semantic_search` always return a full page. The semantic arm returns its nearest neighbours however distant they are. When the keyword arm is empty too, nothing on the page says the list is noise. The owner's framing (2026-10-30): the search tools are investigation entry points, so the product needs recall at depth and honest signals about the candidate set. Precision at rank one matters less. This change adds the honesty signal and two depth metrics. It drops nothing and reorders nothing.

### Live numbers (bge-m3, 2026-10-30, production index mid-rechunk: 1,783 notes not yet re-chunked)

| Query | Tool | Keyword arm | Best similarity |
| --- | --- | --- | --- |
| `xqzvplorth` (#389 report) | hybrid | 0 | 0.36–0.49 across five rows |
| `xqzvplorth` (re-run while drafting) | hybrid | 0 | 0.467 (then 0.464, 0.460) |
| "how do I keep an agent from overwriting a note it did not read first" | semantic | — | 0.618 (then 0.618, 0.611) |
| real paraphrases (operator probes) | semantic | — | 0.62–0.67 |

## Decisions

### D1. One rule, two tools

`src/services/search_confidence.py` holds a pure function, `confidence(keyword_notes: int, top_similarity: float | None, threshold: float) -> "ok" | "low"`:

```
low  ⇔  threshold > 0  ∧  keyword_notes == 0  ∧  (top_similarity is None ∨ top_similarity < threshold)
```

- **`hybrid_search`** passes `result.keyword_rows` (the arm's note count, `all` + `any`) and `result.top_similarity`.
- **`semantic_search`** passes `0` (it has no keyword arm) and the best similarity of its result set.
- **The degraded path.** `hybrid_search`'s keyword-only path passes `top_similarity=None`, so a page with keyword rows reads `ok` and an empty one reads `low`.
- **The boundary.** It is strict: equal to the threshold reads `ok`.
- **A zero threshold is off.** `threshold == 0` is a hard off, so a negative similarity (possible under cosine) cannot fire it.

**`top_similarity` is a property of the arm, not of the page.** It is the full-precision similarity of the semantic arm's best note, under the call's owner and filters. It is read before rerank, before truncation to `limit` and before the response cap. Reranking, `limit=1` and a 1,000-character cap therefore give the same value as the default call. In `hybrid.py` it is `max(sr["similarity"] for sr in s_rows)` (the arm is already re-sorted, so this is rank 1). In `embeddings.semantic_search` / `semantic_candidates` it is the first row's similarity, exposed on the result rather than recomputed in the tool.

Filters are part of the question. `folder="Projects/X"` with only weak matches reads `low`, which is the honest answer for that scope.

`keyword_search` gets no token, because it has no similarity and a keyword hit is lexical evidence. `find_related` gets none, because its similarity is note-to-note, not a query match.

### D2. The threshold: `SEARCH_LOW_CONFIDENCE_SIMILARITY`, default 0.50

The setting is a float in [0, 1], non-finite refused, read at startup. It is not a call argument.

**Why 0.50 for bge-m3.** Gibberish tops out at 0.49 (0.467 today). Real paraphrases start at 0.61. 0.50 sits just above the noise ceiling and about 0.11 below the paraphrase floor. That placement is deliberate. The two errors are not symmetric:

- A **false `low`** on a good page tells an agent to leave the right notes. That is a new harm.
- A **missing `low`** on noise is today's behaviour. That is the bug this change fixes, not a regression it could cause.

So the threshold errs toward silence: as low as it can sit while still catching the observed noise.

**Model-dependent.** Similarity scales differ by model (OpenAI `text-embedding-3-*` cosines run much lower than bge-m3's). The number therefore belongs beside the retrieval fingerprint in `search.md`, which says the model is part of what makes a similarity comparable, and the docs say to re-choose it whenever the fingerprint's model changes. The threshold changes no ranking, so it is **not** part of `retrieval_fingerprint()`. **The harness tunes it:** D7's usage keys let an operator plot `top_similarity` by query class from the export, without a replay.

**The token is always present.** There is no off switch for the token, only for `low`. A deployment on another model that has not tuned the threshold can set `0` and get `confidence: ok` everywhere, which is today's information content.

### D3. Token placement

- `hybrid_search`: directly after `arms: …`, and after `reranked: …` on `rerank=true`. So `Found N hybrid results for "q" — arms: both — confidence: low — rrf_k: 60 — …`. It also appears on the zero-result line and the keyword-only path.
- `semantic_search(rerank=false)`: `Found N semantic matches for 'q' — S stale, T truncated — confidence: ok:`. The zero-result line: `No semantic results for 'q' (embeddings may still be building) — confidence: low`.
- `semantic_search(rerank=true)`: after the stale/truncated counts, before `reranked:`.

The token is server text in the header line. No note text precedes the header, so no note can forge it (the hybrid and rerank encodings quote every note field; the legacy semantic render's raw titles are L9 of #367 and live on row lines, not the header).

### D4. The hint line

There is one fixed constant per tool, at most 140 characters, pinned by a test as `CONFIDENCE_HINT_MAX`:

- hybrid (134): ``low confidence: no keyword match and weak similarity; try `keyword_search` with an identifier, or `get_neighborhood` from a known note``
- semantic (132): ``low confidence: weak similarity and no keyword arm; try `keyword_search` with an identifier, or `get_neighborhood` from a known note``

**Placement.** The hint goes after the header, after the `index_transition:` line when present, and before the first row. On a zero-result response it follows the zero-result line and the transition line, and precedes hybrid's keyword-only footer. The transition line keeps its "directly after the header" contract (#355), so nothing in `search-quality`'s transition requirement moves.

**Why it is safe to add.** The hint never begins with `- `, so row parsers skip it, and a forged copy inside a note's text is always inside a quoted literal or on a row line.

**What it suggests.** It says "try", never "ignore these". The rows are still the semantic arm's nearest notes and stay on the page. `get_neighborhood` is named because the owner's investigation model is to go to the graph from a known note.

### D5. The bound, re-measured at 425bc22

The measurements below were taken by rendering the existing worst-case fixtures, then adding the token's 17 characters (`" — confidence: ok"`) or 18 (`low`). The implementation re-measures every number and the tests pin them.

| Case (floor 1,000, `k=1000`, 8,192-control query) | Before | After |
| --- | --- | --- |
| hybrid header worst (keyword-only, longest code) | 388 ≤ 400 | 405 ≤ **405** |
| hybrid `rerank=true` header worst | 427 ≤ 431 | 444 ≤ **444** |
| semantic `rerank=true` header worst | 181 ≤ 185 | 199 ≤ **199** |
| hybrid floor arithmetic | 985 | 405+340+45+45+75+75+5 = **990** |
| keyword-only, `rerank=true`, cut form (actual footer 71) | 986 | 1,003 → omitted form 984 |
| both arms + ten-digit transition line, cut form | 977 | 994 |
| both arms + transition, `rerank=true`, omitted form | 985 | **1,002** → `explain omitted` ≈ 983 |
| `low` + transition + hint (134), cut form / omitted form | — | 1,066 / **1,007** → `explain omitted` ≈ 988, hint kept |
| `low` + transition + hint, `rerank=true`, omitted form | — | **1,074** → `explain omitted` ≈ 1,054 → hint withdrawn ≈ 919 |
| semantic `rerank=true` low: 199 + 133 + 128 + 235 + 45 + 45 + 76 | — | 861 |

**`confidence: ok` alone breaks the 1,000 bound.** On the both-arms `rerank=true` page with the transition line, the token pushes the last existing rung, the omitted form, to 1,002. A `low` page with the hint is worse. Two rungs are appended to the abbreviation ladder, **after** the existing omitted forms and only for the first row:

1. **`explain omitted`.** The row's `explain` field is replaced by that label. This saves 19–20 characters, and nothing is lost: `explain` is `1/(k+rank)` terms computed from the row's `semantic_rank`, `keyword_rank` and the header's `rrf_k`.
2. **Hint withdrawn.** On a `low` page only, if the row still does not fit, the reserved hint line is released and the row is rendered against the freed room. The header's `confidence: low` stays, so the signal survives and only the advice goes.

The 20–25 cut floor on `rerank=true` hybrid pages was already not guaranteed on the both-arms + transition case (#367/#355 note). Now it is also not guaranteed on the keyword-only worst case (cut form 1,003). Both cases fall through to the labelled omitted form, and the bound holds. These cases need a 1,000-character cap (the default is 40,000), an 8,192-control-character query and a ten-digit pending count. They are pinned because the bound is a guarantee, not because they occur.

**Reservation.** The renderer reserves the header computed with the call's actual value (`ok` or `low`), plus the hint when `low`. `low` on a page with rows implies `keyword_rows == 0`, so the `low` header is always shorter than the `ok` worst case (measured 362 vs 444 on `rerank=true`).

### D6. Rerank

The token is computed from the arm (D1), so `rerank=true` and `rerank=false` carry the same value for the same call. A reranker that promotes a different note to rank 1 does not change it. A cross-encoder score could later inform confidence, but it is out of scope: rerank is opt-in and unmeasured (#367).

### D7. Usage rows

Both tools record two keys through the timing holder, on every call that ran the body, the zero-result and keyword-only calls included:

- **`confidence`**: `"ok"` or `"low"`.
- **`top_similarity`**: rounded to three decimals, and only when a semantic note existed.

Neither key is an `error` marker. A refused call (quota, rate, argument) has no confidence. With both keys, the miner's existing `identifier`/`prose` class can later give the `low` rate per class and the similarity distribution per class. Counting that in the report is **not** in this change (L9): the report's schema change is limited to D8.

### D8. Harness: recall@20 and recall@50

- **The metric.** `METRICS` becomes `("recall@5", "recall@10", "recall@20", "recall@50", "mrr", "ndcg@10")`. They use the same `recall_at`, are reported per arm, class and source, and get baseline deltas with the seeded 1,000-resample bootstrap CI. The table gains `R@20` and `R@50` columns.
- **Depth 50.** `DEFAULT_DEPTH` 20 → 50, because a run truncated at 20 cannot measure recall@50. Fifty is the tools' maximum `limit` and `hybrid_search`'s arm depth, so recall@50 is the ceiling of one page. Replay cost rises modestly: the `keyword` and `semantic` arms pass `limit=depth`, and the hybrid and rerank arms already use `min(depth, 50)`.
- **Null beyond depth.** A recall@k with k greater than the arm's staged depth is **null**, as are its delta and CI. A `runfile:` baseline staged at depth 20 would otherwise score recall@50 equal to recall@20 and fake a delta.
- **Schema and golden.** `schema_version` 1 → 2. The synthetic golden `tests/fixtures/search_eval/expected-report.json` is regenerated **once**, with the reason in the commit message: the metric set and default depth changed. The diff must show only the added keys, the depth and the schema version. Any other change in it is a regression.
- **Unchanged.** The verdict text and the no-threshold rule stay as they are, and `novel@10` stays at ten.

### D9. 1.0 contract

The change is additive:

- one header token on two tools;
- one optional line, which never begins with `- `;
- two usage keys;
- for `hybrid_search` pages at the 1,000-character floor, two new labelled abbreviation rungs.

No row, field, ordering, argument, annotation or tool changes. The #367 `rerank=false` byte-identity fixtures are re-based. Their pinned strings gain exactly ` — confidence: ok` (or `low` plus the hint), and the test diff shows exactly that. The two tool descriptions gain one sentence each on the token and stay ≤ 4,000 characters.

## Owner decisions (taken by default, 2026-10-30)

- O1. The token is `confidence: ok | low`, always present, on `semantic_search` and `hybrid_search` only.
- O2. `low` means: no keyword note (hybrid) or no keyword arm (semantic), and best similarity below `SEARCH_LOW_CONFIDENCE_SIMILARITY` (default 0.50). `0` never fires.
- O3. The hint text is the owner's for hybrid; semantic gets a sibling. The hint is bounded and counted in the budget.
- O4. No row is dropped. There is no similarity floor and no filtering mode.
- O5. The usage rows record `confidence` (and `top_similarity`, D7).
- O6. The harness adds recall@20/50 with default depth 50, regenerates the golden once, and keeps the gate a report.
- O7. No migration.

## Accepted limitations

- L1. **The threshold rests on a handful of live probes**, on an index mid-rechunk (#355: 1,783 notes not yet re-chunked, query-kind embeddings new). Similarity scales may shift once the backlog drains. The live check (tasks §9) re-probes. The harness is the instrument for anything better.
- L2. **The disjunctive fill counts as a keyword match.** A gibberish token plus one common word (`xqzvplorth notes`) gets `any` rows and reads `ok`. Using `keyword_all == 0` would catch it but widens `low`. Deferred as Q1.
- L3. **`semantic_search` judges on similarity alone.** A genuine query about a topic the vault covers only in short or foreign-language notes can read `low`. The rows are all still there, and the hint suggests a different entry, not abandonment.
- L4. **One global threshold.** There is none per folder or per tag, and a filter changes the best similarity by design.
- L5. **A stale best row counts.** Its similarity is against superseded content (#200), and it is counted anyway.
- L6. **At the 1,000-character floor, the 20–25 (`rerank=true`) cut is not guaranteed** on two worst cases (D5). The labelled omitted form and `explain omitted` take over, and the bound holds.
- L7. **At the floor, a `low` `rerank=true` page with the transition line drops the hint.** The token stays.
- L8. **The keyword-only path's empty page cites similarity it never measured.** It reads `low` with the hybrid hint, whose "weak similarity" clause is technically about a semantic arm that did not run. The existing keyword-only footer says why.
- L9. **The report does not count `low` yet.** It neither counts `low` per class nor calibrates the threshold. The data is recorded (D7), and the analysis is a follow-up.
- L10. **recall@50 is null on a baseline staged shallower than 50.** Old baselines must be re-run at depth 50 to get a recall@50 delta.
- L11. **`confidence` is not part of the miner's query identity**, which it should not be: it is an output, not an input.

## Risks

- **A false `low` on a good page.** This is the expensive error, because an agent may skip the right notes. It is mitigated three ways: the threshold sits at the noise ceiling, not the midpoint; the rows are never dropped; and the hint says "try". The adversarial pass is asked to attack exactly this (a realistic query with an empty keyword arm and a correct top note under 0.50).
- **A silent `ok` on noise.** This is today's behaviour, so the residual risk is no worse than the status quo. L2 is the known hole.
- **A bound regression.** The 1,000-character guarantee is the one hard invariant this change touches. Every case in D5 is a pinned test, and the ladder's new rungs are ordered after every existing rung, so no page that fits today renders differently except for the token.
- **The model changes, the threshold does not.** A deployment switching to OpenAI embeddings with 0.50 would read `low` on nearly everything, because those cosines run lower. The README and `.env.example` say so next to the setting, and `0` is the safe value for an untuned model.

## Open questions (only Max can answer)

- Q1. Should `low` also fire when the keyword arm has only `match: any` rows (`keyword_all == 0`)? It catches L2 and widens false-`low` exposure.
- Q2. Should the 0.50 default be re-validated after the #355 re-chunk backlog drains, before or after merge?
- Q3. Raising the default `--depth` to 50 makes fresh runs comparable at depth but leaves old `runfile:` baselines with null recall@50. Is that acceptable, or should depth stay at 20 with recall@50 opt-in?
