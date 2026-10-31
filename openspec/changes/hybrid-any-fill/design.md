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
`semantic_rank` when it picks between one row's keyword and semantic
preview. The offset is a fusion weight, not a claim that the keyword arm
ranked the note lower. **Snippet assignment is a different matter.**
Snippets and `matched_in` go to the first `KEYWORD_SNIPPET_ROWS`
keyword-bearing rows *of the fused page*, and the page's order changes,
so a snippet can move across that boundary: a non-fill row can gain or
lose its snippet, `matched_in` and `preview_kind`. That is deliberate.
The page stays honest about the order it shows, and keeping the
incumbent's assignment would need a second, hidden ordering (Codex
round 1, F2).

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
- the description's fusion sentence drops "with equal votes" and states
  that semantic and all-terms keyword ranks use the same reciprocal-rank
  formula, while a fill (`match: any`) keyword rank enters offset by 50,
  below every all-terms row.

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
omitted form 1,007 → 1,008 (the spec's explain-omitted scenario cites
this D4), falling through to `explain omitted` at 988
(unchanged, because that rung drops the term). Task 5 measures and pins
these figures. If any figure differs, the spec delta is corrected before
verification. The fallback form `1/(k+rank+50)` was rejected: it costs 3
characters and would push the both-arms `rerank=false` cut form to 1,002,
losing the 40–45 cut guarantee at the floor.

The description is 3,827 raw characters today. The rewritten fusion
sentence (measured on a copy of the docstring, 2026-10-31) brings it to
**3,976**, under the 4,000 cap (#343) with 24 characters to spare.

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

So on the real query, option B **does not move E into the top 3, and
this change does not fix that query**. The query is semantic-led: the
semantic arm itself ranks the job notes (3–5) above E (7), and all five leading notes
carry fill terms of similar size. What B does change on this page: two
fill-only notes (11–12) stop outranking semantic-only notes, and the
fill's ±0.002 swing no longer reorders the semantic leaders (J1 and J2
swap). Owner decision (2026-10-31): B ships as a fusion-hygiene change. The
release bar is the eight-cell gate of task 7.3 (recall@10/@20/@50 and
ndcg@10 on `overall` and `class:identifier`), plus the fixture proof that a fill row can never outrank an
all-terms row through the keyword arm alone (task 4.1). The "top 3"
bar is dropped. The issue's query is addressed by the re-test after the
`CHUNKING_VERSION` 2 drain and by #402's reranker path. Task 7.3 still
records E's rank in both arms of the report, for information only.

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
3. **Exchange rule after slicing.** On a call with fused fill rows, the
   relative order of the rows without a `match: any` keyword rank is
   unchanged (their scores and tie-break keys are unchanged). The page
   (`limit`) and the rerank window (first 50) change membership only by
   fill-bearing rows being exchanged for other union members. The
   rerank window **can** change: with one `all` row, 49 `any` rows and
   50 disjoint semantic rows, the window goes from about 25 + 25 to the
   `all` row plus semantic 1–49, and the test pins that.
4. **Snippet boundary.** A non-fill row's line is byte-identical except
   where snippet/`matched_in` assignment moves across
   `KEYWORD_SNIPPET_ROWS` (D2). The fixture: `KEYWORD_SNIPPET_ROWS=1`,
   A all-rank-1 only, F any-rank-50 plus semantic-50.

## Risks / Trade-offs

- [The issue's query is not fixed by B on live data] → stated in D7;
  addressed by the post-drain re-test and #402's reranker path. The change is still correct on its own terms: it removes
  the fill's equal vote.
- [Weaker keyword arm when `all = 0`] → every keyword term is then offset,
  and the fused order is mostly semantic, with the fill as a bonus.
  `keyword_search` keeps the labelled fill for identifier-style queries. The
  gate (task 7.3) enforces the `class:identifier` slice, not just
  inspects it.
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

## Owner decisions (2026-10-31)

1. **Release bar.** Keep option B as fusion hygiene. The gate is the
   eight-cell `search_eval gate` (recall@10/@20/@50 and ndcg@10 ×
   `overall` and `class:identifier`, FAIL when the CI95 lower bound is
   below −0.01), plus the fixture proof (task 4.1). The "top 3" bar for
   the issue's query is dropped (D7).
2. **`explain` form.** `1/(k+55)`: the fusion rank, +1 character (D4).

## Codex spec review — round 1

Verdict, verbatim:

The proposal is internally coherent on the offset arithmetic, confidence inputs, telemetry counts, and process-local harness override. It fails control review because its identity oracle contradicts the permitted top-50 exchanges, overlooks snippet/provenance changes caused by reordering, and uses an evaluation bar blind to default-page regressions.

```json
{"verdict":"FAIL","findings":[{"severity":"MAJOR","file":"openspec/changes/hybrid-any-fill/specs/hybrid-search/spec.md","line":55,"claim":"The identity scenario requires the same fused path set at limit=50, contradicting line 4, design D5, and the actual behavior: the pre-limit union remains identical, but the first 50 page and reranker candidates may change.","failing_input":"At k=60, use one match:all keyword row at rank 1, 49 match:any rows at ranks 2-50, and 50 disjoint semantic rows. Before the offset, the top 50 contain keyword and semantic rows through roughly rank 25. Afterward, every semantic row outranks every match:any-only row, so the top 50 become the all-terms row plus semantic ranks 1-49. The union is unchanged but the asserted limit=50 set is not.","fix":"Test union identity directly before slicing. For limit=50, assert the documented exchange rule instead: unchanged non-fill ordering, with membership changes restricted to fill-bearing rows exchanged for other union members. Explicitly pin the changed reranker window."},{"severity":"MAJOR","file":"openspec/changes/hybrid-any-fill/design.md","line":78,"claim":"The claim that previews remain unchanged, and the oracle's byte-identical non-fill row requirement, are false against the current snippet-selection code. Snippets and matched_in are assigned to the first KEYWORD_SNIPPET_ROWS keyword-bearing rows after fusion, so reordered fill rows can change a non-fill row's rendered provenance and preview.","failing_input":"Set KEYWORD_SNIPPET_ROWS=1. Let A be match:all keyword rank 1 only, and F be match:any keyword rank 50 plus semantic rank 50. Before #403, F scores 2/110 and precedes A at 1/61, so F receives the sole snippet. Afterward F scores 1/160+1/110 and follows A, so A receives the snippet. A carries no match:any rank, yet its preview_kind, preview and matched_in line change.","fix":"Specify whether snippet selection follows incumbent unoffset fusion or the new fused order. Either preserve it explicitly or relax the preview and byte-identity oracle, then add a snippet-cap boundary fixture for a non-fill row."},{"severity":"MAJOR","file":"openspec/changes/hybrid-any-fill/tasks.md","line":51,"claim":"The release bar cannot detect an important regression this ranking change can cause. It checks only recall@20 and recall@50, while the default page has limit=10; the CI comparison also does not gate automatically, and the task does not define the inspected slice or exact rejection inequality. This also leaves design.md's promised class:identifier watch unenforced.","failing_input":"A relevant fill-bearing note moves from rank 10 to 11 while all relevant notes remain within ranks 20 and 50. Both gated recall metrics and their deltas are unchanged, but the default agent-visible page silently loses the note and emits no response-cap footer because slicing happened before rendering. Confidence is unchanged.","fix":"Define exact report fields, slices and rejection inequalities. Include an existing metric sensitive to the default page, such as recall@10, and explicitly enforce the promised class:identifier check rather than merely inspecting recall@20/@50."},{"severity":"MINOR","file":"openspec/changes/hybrid-any-fill/specs/hybrid-search/spec.md","line":150,"claim":"The explain-omitted scenario retains the pre-change 1,007 omitted-form figure, contradicting the same requirement at line 126, design D4, and task 5.2, which correctly project 1,008 after the three-digit fusion rank adds one character.","failing_input":"Both arms, k=1000, keyword rank 50 match:any with fusion rank 100, semantic rank 50, maximum transition line and rerank fallback: the omitted intermediate form gains one character and measures 1,008; only explain-omitted remains 988.","fix":"Change 1,007 to 1,008 and reference the correct design section."},{"severity":"MINOR","file":"openspec/changes/hybrid-any-fill/specs/hybrid-search/spec.md","line":166,"claim":"The required tool description still calls the arms' votes equal while also saying match:any keyword rows use rank+50. That is agent-facing shorthand inconsistent with the effective contribution.","failing_input":"At arm rank 1 and k=60, a semantic-only row contributes 1/61 while a match:any keyword-only row contributes 1/111, despite the description saying equal votes.","fix":"Replace equal votes with wording that states equal reciprocal formulas for semantic and all-terms ranks, with the documented fill-rank offset."}]}
```

What changed, per finding (owner decisions, 2026-10-31):

1. MAJOR, identity oracle: fixed. The union is compared before slicing.
   After slicing the exchange rule applies (non-fill relative order
   unchanged; membership changes only by fill-bearing rows exchanged for
   other union members). The rerank-window change is pinned by Codex's
   1-all + 49-any + 50-semantic construction (spec scenario "The exchange
   rule…", design oracle 3).
2. MAJOR, snippets: fixed. Snippet and `matched_in` assignment follows the
   new fused order. The byte-identity oracle allows a reassignment at the
   `KEYWORD_SNIPPET_ROWS` boundary and nothing else. Codex's boundary
   fixture is a spec scenario and task 4.2b. The "previews unchanged"
   claim is gone from D2.
3. MAJOR, release bar: fixed. Eight cells (recall@10, recall@20,
   recall@50, ndcg@10 × `overall`, `class:identifier`). FAIL when the
   candidate-minus-baseline CI95 lower bound is below −0.01. Enforced by
   a new `search_eval gate` step (ADDED search-evaluation requirement),
   which prints PASS/FAIL per cell; the PR body carries the table (task
   7.3).
4. MINOR, 1,007: fixed. The spec scenario reads 1,008 → 988 and cites
   design D4.
5. MINOR, "equal votes": fixed. The description requirement and task 2.2
   state the same reciprocal-rank formula for semantic and all-terms ranks,
   with fill ranks offset by 50. The description measures 3,976 characters.

## Codex spec review — round 2

Verdict, verbatim:

1. Identity oracle — VERIFIED. Union identity is checked before slicing; page and reranker-window exchanges are explicitly tested.
2. Snippet/provenance movement — VERIFIED. Assignment follows the new fused order, with the exact boundary fixture and permitted row-field differences specified.
3. Default-page release gate — REGRESSED. The recall@10/ndcg@10 gate closes the original failure, but its publishable output omits the existing single-tenant attachability control.
4. Explain-omitted arithmetic — VERIFIED. The scenario now states 1,008 → 988 and cites D4.
5. “Equal votes” description — VERIFIED. The text now distinguishes semantic/all-terms ranks from offset fill ranks.

The remaining scoped pass found one MAJOR: an unfiltered multi-user report can produce a PR-ready gate table containing cross-tenant aggregate evaluation evidence.

```json
{"verdict":"FAIL","round1":[{"claim":"Union identity must be tested before slicing, with page and reranker-window exchanges explicitly permitted.","status":"VERIFIED","note":"The modified requirement, scenarios, oracle and tasks now compare the full union before slicing and pin the 1-all/49-any/50-semantic window change."},{"claim":"Snippet and matched_in assignment can move when fusion reorders rows.","status":"VERIFIED","note":"Design D2 and the specification deliberately follow the new fused order and test the KEYWORD_SNIPPET_ROWS=1 boundary, limiting the non-fill row exception to preview_kind, preview and matched_in."},{"claim":"The release bar must detect default-page and class:identifier regressions.","status":"REGRESSED","note":"recall@10 and ndcg@10, both required slices, the exact inequality and an executable gate now close the original failing input; however, the new PR-ready gate output fails to enforce report attachability and can publish multi-tenant aggregate evidence."},{"claim":"The explain-omitted intermediate figure must be 1,008 rather than 1,007.","status":"VERIFIED","note":"The scenario now states 1,008 → 988 and references hybrid-any-fill design D4."},{"claim":"The tool description must not call the arms' votes equal.","status":"VERIFIED","note":"The requirement and task now state the shared formula only for semantic and all-terms keyword ranks and separately disclose the fill-rank offset."}],"findings":[{"severity":"MAJOR","file":"openspec/changes/hybrid-any-fill/specs/search-evaluation/spec.md","line":37,"claim":"The new gate writes a table expressly ready for a public PR while validating only verdict, baseline, arm, metric and slice. It does not require attachable:true or single-user provenance, contradicting the existing search-evaluation tenant boundary and task 7.3's unconditional instruction to put that table in the PR body.","failing_input":"Run the gate on an unfiltered two-tenant report with verdict OK, a baseline and all eight requested delta cells. The existing harness correctly marks report.json attachable:false, but this requirement still makes the gate print and write the cross-tenant delta means and confidence intervals with PASS/FAIL, after which task 7.3 requires that table in the public PR body.","fix":"Make attachable:true and provenance.users=1 mandatory gate inputs. Otherwise exit 64, print no cell or PASS result, and do not create or replace --out. Add a multi-user-report scenario and require task 7.3 to publish only a table produced by a successful attachable gate run."}]}
```

What changed (owner decision, 2026-10-31):

- Round-1 items 1, 2, 4 and 5 were VERIFIED. No change.
- Round-1 item 3 REGRESSED (MAJOR, tenant boundary): fixed. `gate`
  requires `attachable: true` and `provenance.users == 1` on the
  candidate's and the baseline's report (`--baseline-report` for a
  `runfile:` baseline). Otherwise it exits 64, prints no cell and no
  PASS/FAIL, and neither creates nor replaces `--out`. New scenarios cover
  the multi-user report (verdict OK, baseline, eight cells,
  `attachable: false` → 64, no file) and a non-attachable runfile
  baseline. Task 7.3 publishes only a table from a successful gate run on
  an attachable report (`USER_ID=` export on multi-user). Round 3 is
  verification-only for this item.

## Codex spec review — round 3

Verdict, verbatim:

PASS. The gate now enforces attachable single-user provenance for candidate and baseline evidence, including `runfile:` baselines, with the required fail-closed output behavior. Task 7.3 and the multi-user scenario are aligned, and the four previously verified fixes remain intact. Strict OpenSpec validation passes.

```json
{"verdict":"PASS","round1":[{"claim":"Union identity and permitted page/reranker-window exchanges remain correctly specified.","status":"VERIFIED"},{"claim":"Snippet and matched_in reassignment at the fused-order boundary remains correctly specified.","status":"VERIFIED"},{"claim":"The explain-omitted intermediate figure remains 1,008 rather than 1,007.","status":"VERIFIED"},{"claim":"The tool description distinguishes equal reciprocal formulas from the fill-rank offset.","status":"VERIFIED"}],"round2":[{"claim":"The release gate must reject non-attachable or non-single-user candidate and baseline evidence without publishing results.","status":"VERIFIED"}],"findings":[]}
```

No change: every item VERIFIED.

## Implementation notes (2026-10-31)

- `fuse(..., *, any_paths, any_offset)` as D1, with the defaults the #354
  fusion (a randomized test compares it with the verbatim incumbent).
  `hybrid.any_fill_offset(semantic_ran)` returns 0 on the keyword-only path,
  the override when set, and the constant otherwise.
  `HybridRow.keyword_fusion_rank` carries the rank the renderer prints in
  `explain`.
- Snippet assignment needed no code change: the service already assigned
  snippets to the page's keyword rows in fused order. The #403 change of
  order is what moves them (D2), and a test pins it.
- **Small shift: the runner gained a `check_options` factory hook** beside
  `required_options`, so `hybrid_alt_any_offset`'s value is a usage error
  (exit 64) *before* the generation is reserved, which D6 required.
- **Shift: the named judgment is not in the synthetic fixture.** The
  synthetic fixture (`tests/fixtures/search_eval/`) replays only the
  `fixture-*` arms, which have no database and no hybrid arm. The opinion
  note's vault path is tenant data and does not belong in a public repo.
  The judgment therefore goes into `.search-eval/manual.jsonl` (tenant data,
  never committed) in the operator pass (task 7.3), with its rank reported
  and no target.
- Re-measured bounds: headers 405 / 444 unchanged; structures 338 / 366
  inside the unchanged 345 / 367; D5 figures keyword-only 981 / 1,008 →
  989 unchanged, both arms `rerank=false` cut 1,000, `rerank=true` omitted
  1,008 → `explain omitted` 988; with the transition line `rerank=true` cut
  1,027 (was 1,026). The description measures 3,976 characters (pinned).

