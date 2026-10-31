## Context

`hybrid_search` (#354) fuses the keyword arm and the semantic arm by
reciprocal rank fusion over note paths:
`score = Σ 1/(k + rank)`, `k = HYBRID_RRF_K` (60), over each arm's top 50
(`HYBRID_ARM_DEPTH`). The keyword arm is `search.keyword_arm_rows`. It
returns the conjunctive block (`match: all`, at most 50 rows). When that
block under-fills, it appends the disjunctive fill (`match: any`, built from
PostgreSQL's own parse, #344) up to 50 rows in total. `fuse` currently gets
only the arm's path list, so it cannot tell a fill row from a conjunctive
one.

The row encoding already shows which rows are fill rows: each keyword row
renders `keyword_rank: N (match: any|all, matched_in: …)`. The header
carries `keyword: n notes (all a, any b)` and
`arm_depth: keyword n/50, …`. The service records `any_rows` and (#389)
`keyword_all` on every call. Nothing in the *score* reflects the label.
That is the bug.

## Goals / Non-goals

Goals: a `match: any` row can never outrank a `match: all` row through the
keyword arm alone. Fill rows stay in the candidate set and still add to a
semantic hit. Pages without fill rows and keyword-only pages stay
byte-identical. The A/B against the incumbent runs in one harness
generation.

Non-goals: changing either arm's retrieval, the fill's construction, `k`,
the arm depth, the confidence rule, the header grammar, or
`keyword_search` / `semantic_search` output. Option A (fuse only
`match: all`) and option C (offset only title-only fill rows) are not
implemented. See D8.

## D1 — The offset: `R_fusion = rank + 50` for `match: any`

`fuse(keyword_paths, semantic_paths, k, *, any_paths=frozenset(),
any_offset=0)`. For a keyword path in `any_paths`, the keyword term is
`1/(k + rank + any_offset)`. Every other term is unchanged. The service
passes `any_paths = {rows with match == "any"}` and
`any_offset = HYBRID_ANY_FILL_OFFSET` (= `HYBRID_ARM_DEPTH` = 50) **only
when the semantic arm ran**. The value is derived from the arm depth, not
picked as a separate number. It is the smallest offset that gives the
guarantee for every `k`:

- a conjunctive row has keyword rank `r_all ≤ 50`, so its term is
  `≥ 1/(k+50)`;
- a fill row has keyword rank `r ≥ 1`, so its fusion rank is `≥ 51` and
  its term is `≤ 1/(k+51) < 1/(k+50)`.

At `k = 60`, the best possible fill term is `1/111 ≈ 0.009009` and the worst
conjunctive term is `1/110 ≈ 0.009091`. The same bound places every
fill-only note below every semantic-only note, because semantic ranks are
≤ 50 (50-chunk overfetch, deduped). Before this change, a fill row at
keyword rank 1 tied a semantic rank-1 note.

`rank` is the position in the arm, not the position within the fill. A
fill row after `a` conjunctive rows has rank `a + j`, so its fusion rank is
`a + j + 50`. This is still `≥ 51` and still monotone in the arm's own
order. The rendered `keyword_rank` stays this arm position.

A fill row's term still **adds** to its semantic term. That is deliberate.
A note that matched some query words *and* is semantically close should
beat a note that is only semantically close at a similar rank. The offset
removes the keyword arm's ability to *lead* with a fill row. It does not
take away the fill row's vote. Worst case at `k = 60`, fill rank 100 plus
semantic rank 50: `1/160 + 1/110 ≈ 0.01534`, below a semantic-only rank-1
note (`1/61 ≈ 0.01639`), as intended.

## D2 — Tie-break and preview use the unoffset rank

The order is fused score DESC, then `best_rank` ASC, then path ASC.
`best_rank` stays the minimum of the **unoffset** arm ranks. Ties need
equal scores. A fill-only note can never tie a semantic-only or
conjunctive-only note (D1's strict inequality), so the tie-break only
decides between notes whose term multisets give equal sums, as before.
Keeping it unoffset keeps the #354 "favour no arm" rule word for word.
`_hybrid_preview` keeps comparing the unoffset `keyword_rank` with
`semantic_rank`, so previews on pages with fill rows are unchanged. The
offset is a fusion weight, not a claim that the keyword arm ranked the
note lower.

## D3 — Keyword-only pages: no offset

When the embedding fails (`arms: keyword-only …`), `fuse` is called with
an empty semantic list. Applying the offset there would leave the order
unchanged, because the fill already follows the conjunction and the offset
is monotone. It would still change every fill row's `score` and `explain`.
The service therefore passes `any_offset=0` on that path. The degraded page
stays byte-identical to today: the keyword arm, in keyword order, scored by
its own term (#354 D4).

## D4 — Rendering and the header: no new token

`HybridRow` gains `keyword_fusion_rank: int | None`. It is `keyword_rank +
offset` for a fused fill row and `keyword_rank` otherwise.
`_hybrid_server_fields` builds the keyword `explain` term from it:
`explain: 1/(60+12) + 1/(60+55)` next to `keyword_rank: 5 (match: any,
matched_in: title,body)`. The score therefore always equals the sum of the
rendered terms.

The header gets no new token, because the disclosure already exists:

- per row, `keyword_rank: N (match: any, …)` marks the fill row, and the
  `explain` term now shows the offset rank;
- in the header, `keyword: n notes (all a, any b)` gives the split and
  `arm_depth` the depth;
- the description gains one sentence: "A `match: any` keyword row (only
  some words matched) enters at `rank + 50`, below every all-terms row."

A header token (for example `any_offset: 50`) would cost about 16
characters of base budget on every page, like `confidence` did (#389
L14). It would only repeat what the rows and the description already say.

**Bounds re-measured.** The header is unchanged: 405 / 444. The
worst-case row structure used ranks of 50. A fused fill row now renders a
fusion rank up to 100, which adds one digit (`1/(1000+100)`). The current
`rerank=false` structure measures **337** (test print, 2026-10-31),
so it becomes 338, inside the pinned 345. `HYBRID_RERANK_ROW_STRUCTURE_MAX`
367 is re-measured the same way. The keyword-only measured figures do not
move (D3). Projected both-arms figures: `rerank=false` cut form 999 →
1,000 (still ≤ 1,000, and the 40–45 cut still holds); `rerank=true`
omitted form 1,007 → 1,008, falling through to `explain omitted` at 988
(unchanged, because that rung drops the term). Task 5 measures and pins
these figures. If any figure differs, the spec delta is corrected before
verification. The fallback form `1/(k+rank+50)` was rejected: it costs 3
characters and would push the both-arms `rerank=false` cut form to 1,002,
losing the 40–45 cut guarantee at the floor.

The description is 3,827 raw characters today. The added sentence fits
under the 4,000 cap (#343) with roughly 20 characters to spare. If it does
not, the sentence is shortened. Nothing else is removed.

## D5 — Rerank and confidence

`hybrid_search(rerank=true)` still reranks the first
`max(limit, RERANK_MAX_CANDIDATES)` = 50 fused rows (#367 D6). The union of
the arms can hold up to 100 notes. Under the new order, fill-only notes
sort last, so they are the first to fall outside the rerank window when
the union exceeds 50. This is intended, and it is the same effect on the
plain page at any `limit`. `confidence` (#389) reads `keyword_all` and
`top_similarity`. Neither changes: `match: any` was never evidence. The
telemetry keys are unchanged; `any_rows` already records how many fill
rows were fused.

## D6 — The harness A/B seam

`--arm-opt` is one dict for every arm in a run. `make_arm_alt` already
requires `hybrid_alt_k`, and it gains the optional option
`hybrid_alt_any_offset ∈ {0, 50}` (default 50, any other value is a usage
error, exit 64). The live path has no argument for it. Instead,
`src/services/hybrid.py` declares a module-level
`_any_fill_offset_override: ContextVar[int | None]` (default `None` = use
the constant). The alt arm sets it with `token = var.set(0)` / `var.reset(token)`
around its own service call. A `ContextVar` is task-local, so a concurrent
`make_arm` query in the same process cannot see it. A plain module global
could leak between arms if replay ever ran arms concurrently. A test pins
that nothing under `src/` assigns the override (AST guard). The release
run:

```
make search-eval-snapshot
make search-eval SEARCH_EVAL_DATA=.search-eval SEARCH_EVAL_DATABASE_URL=<restored BACKUP_FULL=1 copy> \
  ARMS="keyword semantic scripts.search_eval_arms.hybrid:make_arm scripts.search_eval_arms.hybrid:make_arm_alt" \
  ARM_OPTS="hybrid_alt_k=60 hybrid_alt_any_offset=0" \
  BASELINE=scripts.search_eval_arms.hybrid:make_arm_alt
```

No migration is involved, so the A/B needs one generation at the candidate
commit, not keyword-tags' two-commit runfile workflow. The candidate arm
and the incumbent arm then share qrels, snapshot and selection by
construction.

## D7 — Worked numbers, and what the live page says

**The arithmetic, at `k = 60`, on the issue's shape** (illustrative ranks:
job notes as fill rows at keyword ranks 1–3 with mid semantic ranks, the
opinion note E at semantic 2 and fill rank 20):

| note | ranks (sem, kw any) | incumbent | with offset |
| --- | --- | --- | --- |
| E (opinion) | 2, 20 | 1/62 + 1/80 = 0.028629 (5th) | 1/62 + 1/130 = 0.023821 (1st) |
| J1 | 9, 1 | 1/69 + 1/61 = 0.030886 | 1/69 + 1/111 = 0.023502 |
| J2 | 12, 2 | 0.030018 | 1/72 + 1/112 = 0.022817 |
| J3 | 15, 3 | 0.029206 | 1/75 + 1/113 = 0.022183 |
| J4 | 14, 4 | 0.029139 | 1/74 + 1/114 = 0.022285 |
| a semantic-only rank 1 | 1, – | 0.016393 | 0.016393 |

**The live page (2026-10-31, one read-only `hybrid_search` call against the
deployed server, `limit=12`)** does not have that shape. Vault titles are
replaced by role labels here (public repo); E is the note named in #403.
The header reads `keyword: 50 notes (all 2, any 48)`. The two conjunctive
rows match all words in the body and have no semantic rank. The job notes
(J1–J3) reach the top through the **semantic** arm (ranks 3, 4, 5) plus
fill ranks 8–15. E is semantic 7, fill 25. Recomputed:

| rank | incumbent | with offset |
| --- | --- | --- |
| 1 | an AI-practice reference note R (s1, k5) 0.031778 | R 0.025089 |
| 2 | J1 (s5, k8) 0.030090 | J2 0.023873 |
| 3 | J2 (s3, k15) 0.029206 | J1 0.023859 |
| 4 | J3 (s4, k14) 0.029139 | J3 0.023690 |
| 5 | **E (s7, k25) 0.026690** | **E 0.022333** |
| 6–7 | two more semantic+fill notes (s8/k34, s13/k29) | same |
| 8–10 | conjunctive C1 (k1) 0.016393, semantic-only (s2) 0.016129, conjunctive C2 (k2) 0.016129 | same |
| 11–12 | fill-only (k3) 0.015873, fill-only (k4) 0.015625 | drop to 0.008850 / 0.008772, below every semantic-only note |

So on the real query, option B **does not move E into the top 3**. The
semantic arm itself ranks the job notes above E, and all five leading notes
carry fill terms of similar size. What B does change on this page: two
fill-only notes (11–12) stop outranking semantic-only notes, and the
fill's ±0.002 swing no longer reorders the semantic leaders (J1 and J2
swap). The release bar as briefed ("the opinion note
in the top 3") is therefore not reachable by this change on current data.
See Open questions. Task 7.3 records E's rank in both arms of the report.

## D8 — Alternatives

- **A (fuse only `match: all`)** loses recall at depth (the fill's notes
  leave the candidate set entirely) and empties the keyword arm on the
  common case `all 0`. Owner chose B.
- **C (offset only title-only fill rows)** keeps body-cover fill rows at
  full weight. On the live page every leading fill row is
  `matched_in: title,body`, so C would change nothing there.
- **A smaller offset (e.g. 10)** keeps the fill competitive with
  conjunctive rows ranked 11–50, which breaks the "never outrank" guarantee.
- **A setting or per-call argument**: no. One more ranking knob an agent
  cannot see is worse than a documented constant. `HYBRID_RRF_K` already
  covers tuning.

## Result identity (the oracle)

Against the incumbent, on the same corpus, filters and arguments:

1. The **fused candidate set** (the union of both arms, before `limit`) is
   identical. Neither arm's retrieval changes.
2. A call where **no `match: any` row was fused** (`keyword_any = 0`), or
   where the semantic arm was unavailable, renders a **byte-identical**
   response. This is pinned with the existing fixtures in
   `tests/test_hybrid_search_offline.py` and `test_hybrid_search_pg.py`.
3. On a call with fused fill rows, the **relative order of the rows
   without a `match: any` keyword rank** is unchanged (their scores and
   tie-break keys are unchanged). Only the positions of fill-bearing rows,
   their `score`, and their keyword `explain` term may differ. The page's
   membership under `limit`, and the rerank window, may change only by
   exchanging fill-bearing rows for others.

## Risks / Trade-offs

- [The issue's query is not fixed by B on live data] → stated in D7; owner
  question below. The change is still correct on its own terms: it removes
  the fill's equal vote.
- [Weaker keyword arm when `all = 0`] → every keyword term is then offset,
  and the fused order is mostly semantic, with the fill as a bonus.
  `keyword_search` keeps the labelled fill for identifier-style queries. The
  eval report's `class:identifier` slice is watched for a regression.
- [Explain readability: `keyword_rank: 5` next to `1/(60+55)`] → the
  description sentence explains the offset, and `match: any` sits on the
  same row.
- [Harness override leaking into production] → `ContextVar`, set only by
  the harness, AST guard on `src/`.
- [Bound drift] → task 5 re-measures every pinned figure.

## Accepted limitations

- L1: The offset is uniform across fill rows. A fill row that matched four
  of five words gets the same penalty as one that matched one word.
  Option C/D-style graded weights are out of scope.
- L2: On `all = 0` queries the keyword arm can no longer lift a
  semantically weak note above a semantically strong one, even when its
  title is the best lexical match. `keyword_search` remains the tool for
  that.
- L3: Scores of fill-bearing rows are not comparable with pre-#403 scores.
  Scores were only ever comparable within one page.

## Migration Plan

No migration, no setting. Ships through the normal merge → CI → image →
Flux path. Rollback is the previous image.

## Open questions (Max)

1. **Release bar.** On the live page B leaves the opinion note 5th (D7).
   Options: keep B and drop the "top 3" bar (gate on recall@20/@50 CI95
   only, report E's rank); also reconsider the semantic arm on this query
   (a reranker run with `rerank=true` is the existing remedy); or move to
   option A for the `all > 0` case.
2. Should `explain` use `1/(k+55)` (chosen, +1 character) or the
   self-describing `1/(k+5+50)` (+3 characters, which loses the 40–45 cut at
   the both-arms floor)?
