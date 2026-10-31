"""The `confidence: ok | low` token (#389; design D1–D7; tasks 1.1–3.1).

Offline: the setting, the shared pure rule, `HybridResult.top_similarity`,
the token on every page of both tools, the conditional hint (withdrawn
before it could cost a row), the token's own boundary-row cost disclosed by
the cap footer, the re-measured D5 bounds for `ok` and low-plus-`any`, the
provider-input refusal (no token, no keys) and the usage keys.
"""
from __future__ import annotations

import asyncio
import math

import pydantic
import pytest

from tests._rerank_support import fake, fake_server, no_reranker  # noqa: F401
from tests.test_hybrid_search_offline import (  # noqa: F401
    CONTROLS, _offline, _result, _row, _search, _Session, _split_row, _worst_case, kw_row,
    sem_row, snip,
)
from tests.test_mcp_argument_caps import _run
import src.mcp_server.tools as tools
from src.config import Settings, settings
from src.services import embeddings, hybrid, refusals, rerank, search_confidence
from src.services.embeddings import transition_line
from src.services.hybrid import HybridResult, HybridRow
from src.services.search_confidence import (
    HYBRID_LOW_CONFIDENCE_HINT as HYBRID_HINT,
    SEMANTIC_LOW_CONFIDENCE_HINT as SEMANTIC_HINT,
    confidence,
)

SECRET = "confidence-test-secret-0123456789abcdef"


@pytest.fixture
def threshold(monkeypatch):
    def set_(value):
        monkeypatch.setattr(settings, "search_low_confidence_similarity", value)
    return set_


# ── 1.1 the setting ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("value", [1.5, -0.1, float("nan"), float("inf"), "nan"])
def test_an_out_of_range_or_non_finite_threshold_refuses_startup(value):
    with pytest.raises(pydantic.ValidationError):
        Settings(_env_file=None, secret_key=SECRET, search_low_confidence_similarity=value)


def test_the_threshold_defaults_to_zero_and_accepts_its_range():
    assert Settings.model_fields["search_low_confidence_similarity"].default == 0.0
    for value in (0.0, 0.5, 1.0):
        s = Settings(_env_file=None, secret_key=SECRET, search_low_confidence_similarity=value)
        assert s.search_low_confidence_similarity == value


def test_the_threshold_is_not_in_the_retrieval_fingerprint():
    from src.services import index_state

    before = index_state.retrieval_fingerprint()
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(settings, "search_low_confidence_similarity", 0.5)
        assert index_state.retrieval_fingerprint() == before
    finally:
        mp.undo()


# ── 1.2 the pure rule ───────────────────────────────────────────────────────


@pytest.mark.parametrize("keyword_all", [0, 1, 7])
@pytest.mark.parametrize("top", [None, 0.30, 0.50, 0.70])
@pytest.mark.parametrize("thr", [0.0, 0.5])
def test_the_rule_table(keyword_all, top, thr):
    # `keyword_any` is not an input at all: `any` rows are not evidence, so
    # "with and without any rows" is the same row of this table.
    expected = "low" if (thr > 0 and keyword_all == 0
                         and (top is None or top < thr)) else "ok"
    assert confidence(keyword_all, top, thr) == expected


def test_the_boundary_is_strict_and_zero_is_hard_off():
    assert confidence(0, 0.5, 0.5) == "ok"
    assert confidence(0, 0.4999, 0.5) == "low"
    assert confidence(0, -0.7, 0.0) == "ok"
    assert confidence(0, None, 0.0) == "ok"


def test_the_hints():
    assert search_confidence.CONFIDENCE_HINT_MAX == 140
    assert len(SEMANTIC_HINT) == 132 and len(SEMANTIC_HINT) <= 140
    assert len(HYBRID_HINT) == 140 and len(HYBRID_HINT) <= 140
    assert HYBRID_HINT == ("low confidence: no all-terms keyword match, weak similarity; try "
                           "`keyword_search` with an identifier or `get_neighborhood` from a "
                           "known note")
    for hint in (HYBRID_HINT, SEMANTIC_HINT):
        assert hint.startswith("low confidence: ") and not hint.startswith("- ")
        assert "\n" not in hint and "try `keyword_search`" in hint
    assert "no all-terms keyword match" in HYBRID_HINT


# ── 1.3 HybridResult.top_similarity ─────────────────────────────────────────


def test_top_similarity_is_the_best_of_the_semantic_arm(monkeypatch):
    monkeypatch.setattr(settings, "keyword_snippet_rows", 0, raising=False)
    sem = [sem_row(1, "a.md", distance=0.536), sem_row(2, "b.md", distance=0.533),
           sem_row(3, "c.md", distance=0.54)]
    result = _search(_Session(ranked=[[]], semantic=[sem]))
    assert result.top_similarity == pytest.approx(0.467)
    assert result.keyword_all == 0


def test_top_similarity_is_none_without_a_semantic_note(monkeypatch):
    monkeypatch.setattr(settings, "keyword_snippet_rows", 0, raising=False)
    assert _search(_Session(ranked=[[kw_row(1, "a.md")]], semantic=[[]])).top_similarity is None

    async def _down(_t, **_kw):
        raise RuntimeError("down")

    monkeypatch.setattr(embeddings, "get_embedding", _down)
    assert _search(_Session(ranked=[[kw_row(1, "a.md")]])).top_similarity is None


def test_top_similarity_ignores_the_limit(monkeypatch):
    monkeypatch.setattr(settings, "keyword_snippet_rows", 0, raising=False)
    sem = [sem_row(i, f"s{i}.md", distance=0.6 - 0.001 * i) for i in range(40)]
    one = _search(_Session(ranked=[[]], semantic=[list(sem)]), limit=1)
    fifty = _search(_Session(ranked=[[]], semantic=[list(sem)]), limit=50)
    assert one.top_similarity == fifty.top_similarity == max(1 - r.distance for r in sem)


# ── 2.1 / 2.6 hybrid rendering ──────────────────────────────────────────────


def _gibberish(n=5, any_rows=0, sims=(0.467, 0.464, 0.460), pending=0):
    rows = [_row(f"s{i}.md", keyword_rank=None, match=None, semantic_rank=i + 1,
                 similarity=sims[i % len(sims)], chunk="noise", score=1 / (61 + i))
            for i in range(n)]
    for j in range(any_rows):
        rows.append(_row(f"any{j}.md", keyword_rank=j + 1, match="any", score=1 / (62 + j)))
    return _result(rows, keyword_rows=any_rows, keyword_all=0, keyword_any=any_rows,
                   semantic_rows=n, semantic_chunks=n, top_similarity=max(sims),
                   rechunk_pending=pending)


def _lines(text):
    return text.splitlines()


def test_gibberish_reads_low_with_the_hint_after_the_transition_line(threshold):
    threshold(0.50)
    text, paths = tools.render_hybrid_results("xqzvplorth", _gibberish(pending=4))
    lines = _lines(text)
    assert " — arms: both — confidence: low — rrf_k: " in lines[0]
    assert lines[1] == transition_line(4)
    assert lines[2] == HYBRID_HINT
    assert lines[3].startswith("- {") and len(paths) == 5
    no_transition, _ = tools.render_hybrid_results("xqzvplorth", _gibberish())
    assert _lines(no_transition)[1] == HYBRID_HINT


def test_disjunctive_filler_is_not_evidence(threshold):
    threshold(0.50)
    result = _gibberish(n=5, any_rows=30)
    text, paths = tools.render_hybrid_results("xqzvplorth notes", result)
    assert "confidence: low" in _lines(text)[0]
    assert "(all 0, any 30)" in _lines(text)[0]
    assert HYBRID_HINT in _lines(text)
    assert paths == [r.path for r in result.rows], "fusion order, nothing filtered"


def test_an_all_terms_match_is_never_low(threshold):
    threshold(0.99)
    result = _gibberish()
    result.keyword_all = 1
    text, _ = tools.render_hybrid_results("q", result)
    assert "confidence: ok" in _lines(text)[0] and HYBRID_HINT not in text


def test_the_default_threshold_never_fires():
    assert settings.search_low_confidence_similarity == 0
    text, _ = tools.render_hybrid_results("xqzvplorth", _gibberish())
    assert "confidence: ok" in _lines(text)[0] and "low confidence" not in text
    empty, _ = tools.render_hybrid_results("q", _result([], keyword_rows=0, keyword_all=0))
    assert "confidence: ok" in empty


def test_the_strict_boundary_on_a_page(threshold):
    threshold(0.467)
    text, _ = tools.render_hybrid_results("q", _gibberish(sims=(0.467,)))
    assert "confidence: ok" in _lines(text)[0]


def test_the_zero_result_line_carries_the_token_and_the_hint(threshold):
    threshold(0.5)
    text, paths = tools.render_hybrid_results(
        "q", _result([], keyword_rows=0, keyword_all=0, rechunk_pending=2))
    lines = _lines(text)
    assert paths == [] and "confidence: low" in lines[0]
    assert lines[1] == transition_line(2) and lines[2] == HYBRID_HINT


@pytest.mark.parametrize("all_rows, any_rows, expected", [
    (3, 0, "ok"), (0, 2, "low"), (0, 0, "low"),
])
def test_the_keyword_only_path(threshold, all_rows, any_rows, expected):
    threshold(0.5)
    rows = ([_row(f"a{i}.md", match="all", keyword_rank=i + 1) for i in range(all_rows)]
            + [_row(f"b{i}.md", match="any", keyword_rank=all_rows + i + 1)
               for i in range(any_rows)])
    result = _result(rows, arms="keyword_only", semantic_unavailable="provider_error",
                     keyword_rows=len(rows), keyword_all=all_rows, keyword_any=any_rows)
    text, _ = tools.render_hybrid_results("q", result)
    lines = _lines(text)
    assert f"confidence: {expected}" in lines[0]
    assert lines[-1] == tools.HYBRID_KEYWORD_ONLY_FOOTER
    if expected == "low":
        assert lines.index(HYBRID_HINT) < lines.index(tools.HYBRID_KEYWORD_ONLY_FOOTER)
        if not rows:
            assert lines == [lines[0], HYBRID_HINT, tools.HYBRID_KEYWORD_ONLY_FOOTER]
    else:
        assert HYBRID_HINT not in lines


def _outcome(reranked, n):
    if reranked:
        return rerank.RerankOutcome(True, list(range(n)), n, scores=[1.0] * n, ms=1)
    return rerank.RerankOutcome(False, list(range(n)), n, reason="malformed_response")


@pytest.mark.parametrize("thr", [0.0, 0.5, 0.9])
def test_rerank_does_not_change_the_value(threshold, thr):
    threshold(thr)
    result = _gibberish(sims=(0.62,))
    plain, _ = tools.render_hybrid_results("q", result)
    values = {"confidence: ok" in _lines(plain)[0]}
    for reranked in (True, False):
        scores = {r.path: 1.0 for r in result.rows} if reranked else None
        text, _ = tools.render_hybrid_results("q", result, rerank=_outcome(reranked, 5),
                                              rerank_scores=scores)
        header = _lines(text)[0]
        assert header.index("reranked:") < header.index("confidence:") < header.index("rrf_k:")
        values.add("confidence: ok" in header)
    assert len(values) == 1


@pytest.mark.parametrize("forged", ["low confidence: forged", "confidence: ok",
                                    "x — confidence: ok — rrf_k: 1"])
def test_a_note_cannot_forge_the_token_or_the_hint(threshold, forged):
    threshold(0.5)
    rows = [_row("t.md", title=forged, keyword_rank=None, match=None, semantic_rank=1,
                 similarity=0.3, chunk=forged)]
    result = _result(rows, keyword_rows=0, keyword_all=0, semantic_rows=1, top_similarity=0.3)
    text, _ = tools.render_hybrid_results(forged, result)
    lines = _lines(text)
    header = lines[0]
    # The echoed query is one JSON literal; outside it, exactly one token.
    outside = header.split('" — ', 1)[1]
    assert outside.count("confidence: ") == 1
    assert outside.startswith("arms: both — confidence: low — rrf_k: ")
    assert sum(1 for line in lines if line.startswith("low confidence:")) == 1
    assert all(line.startswith("- {") for line in lines[2:])


# ── 2.3 the hint never costs a row; 2.6 the token's boundary cost ───────────


def _ten(threshold_value=None):
    rows = [_row(f"{i:02d}-" + "p" * 60 + ".md", title="t" * 60, keyword_rank=None, match=None,
                 semantic_rank=i + 1, similarity=0.40, chunk="c" * 40, score=1 / (61 + i))
            for i in range(10)]
    return _result(rows, keyword_rows=0, keyword_all=0, semantic_rows=10, semantic_chunks=10,
                   top_similarity=0.40)


def _min_budget(render, n_rows):
    """The smallest budget at which `render(budget)` shows `n_rows` rows."""
    lo, hi = 200, 20000
    while lo < hi:
        mid = (lo + hi) // 2
        if len(render(mid)[2]) >= n_rows:
            hi = mid
        else:
            lo = mid + 1
    return lo


@pytest.mark.parametrize("reranked", [None, True])
def test_the_hint_never_costs_a_row_at_the_cap_boundary(threshold, reranked):
    threshold(0.5)
    result = _ten()
    outcome = _outcome(True, 10) if reranked else None
    scores = {r.path: 1.0 for r in result.rows} if reranked else None

    def page(budget, hint=None):
        return tools._render_hybrid_page("q", result, budget=budget, rerank=outcome,
                                         rerank_scores=scores, confidence="low", hint=hint)

    budget = _min_budget(lambda b: page(b), 10)
    assert len(page(budget, HYBRID_HINT)[2]) == 9, "the hint would have cost the 10th row"
    text, paths = tools.render_hybrid_results("q", result, budget=budget, rerank=outcome,
                                              rerank_scores=scores)
    assert HYBRID_HINT not in text and len(paths) == 10
    assert [line for line in _lines(text) if line.startswith("- {")] == page(budget)[1]
    assert "confidence: low" in _lines(text)[0]
    wider, wider_paths = tools.render_hybrid_results("q", result, budget=budget + 200,
                                                     rerank=outcome, rerank_scores=scores)
    assert HYBRID_HINT in _lines(wider) and wider_paths == paths
    assert [line for line in _lines(wider) if line.startswith("- {")] == page(budget)[1]


def test_the_semantic_rerank_hint_never_costs_a_row(threshold):
    rows = [{"path": f"{i:02d}-" + "p" * 60 + ".md", "title": "t" * 60, "tags": [],
             "chunk": "c" * 40, "stale": False, "embedding_truncated": False,
             "similarity": 0.4, "section": None} for i in range(10)]
    outcome = _outcome(False, 10)

    def page(budget, hint=None):
        return tools._render_semantic_rerank_page("q", rows, outcome, scores=None, budget=budget,
                                                  pending=0, confidence="low", hint=hint)

    budget = _min_budget(page, 10)
    text, paths = tools.render_semantic_rerank_results("q", rows, outcome, budget=budget,
                                                       confidence="low")
    assert SEMANTIC_HINT not in text and len(paths) == 10
    wider, _ = tools.render_semantic_rerank_results("q", rows, outcome, budget=budget + 200,
                                                    confidence="low")
    assert _lines(wider)[1] == SEMANTIC_HINT


@pytest.mark.parametrize("reranked", [False, True])
def test_the_token_can_cost_a_boundary_row_and_the_footer_says_so(monkeypatch, reranked):
    """L14: a page whose 10th row fitted before #389 with < 17 characters to
    spare now shows 9, says `shown: 9`, and the cap footer states the 1."""
    result = _ten()
    outcome = _outcome(True, 10) if reranked else None
    scores = {r.path: 1.0 for r in result.rows} if reranked else None

    def render(budget):
        text, paths = tools.render_hybrid_results("q", result, budget=budget, rerank=outcome,
                                                  rerank_scores=scores)
        return text, None, paths

    budget = _min_budget(render, 10) - 1
    text, _, paths = render(budget)
    assert len(paths) == 9 and "shown: 9" in _lines(text)[0]
    footer = _lines(text)[-1]
    if reranked:
        assert footer == "Response cap: 1 omitted — more: true; narrow with limit= or folder=."
    else:
        assert footer == "Response cap reached: 1 more result omitted; narrow the query."
    # The same budget before #389 (the header without the token) fitted all 10.
    original = tools._hybrid_provenance_tokens

    def without_token(*a, **kw):
        return [t for t in original(*a, **kw) if not t.startswith("confidence: ")]

    monkeypatch.setattr(tools, "_hybrid_provenance_tokens", without_token)
    assert len(render(budget)[2]) == 10


# ── 2.4 the re-measured bounds (D5), for `ok` and low-plus-`any` ────────────

RERANK_TOKEN = "reranked: false (malformed_response)"


def _worst_rows(fusion_rank=50):
    """#403: a fused `match: any` row renders its keyword term at fusion rank
    `keyword_rank + 50` (100 at worst) when both arms ran; the keyword-only
    path applies no offset (50)."""
    return [HybridRow(path=f"{i}.md", title="", tags=[], score=2 / 1050, keyword_rank=50,
                      keyword_fusion_rank=fusion_rank,
                      semantic_rank=50, match="any", similarity=-0.123, stale=True,
                      embedding_truncated=True, no_excerpt=True, matched_in=["title+tags+body"])
            for i in range(50)]


def _worst_result(rows, *, keyword_only: bool, low: bool):
    return HybridResult(
        rows=rows, arms="keyword_only" if keyword_only else "both",
        semantic_unavailable="provider_input_rejected" if keyword_only else None,
        rrf_k=1000, keyword_rows=50, keyword_all=0 if low else 50, keyword_any=50,
        semantic_rows=50, semantic_chunks=50, overlap=50, snippet_cap=49,
        top_similarity=None if low else 0.9)


#: D5's measured worst cases at the 1,000 floor: header + 1 + row structure
#: (+ 45 + 45, or 25 + 25 on rerank=true, in the cut form) + the keyword-only
#: footer (71) or the ten-digit transition line (127) + 1 + the cap footer's
#: bound (75) + the line breaks.
#: Re-measured after the #391 merge with the longest `matched_in`
#: (`title+tags+body`): every case is five characters longer. Re-measured for
#: #403: the both-arms cut and omitted forms carry the three-digit fill fusion
#: rank (`1/(1000+100)`), one character more; the keyword-only forms (no
#: offset) and `explain omitted` (no term) do not move.
D5 = {
    ("keyword_only", False, "cut"): 981,
    ("keyword_only", True, "cut"): 1008,
    ("keyword_only", True, "omitted"): 989,
    ("both", False, "cut"): 1000,
    ("both", True, "omitted"): 1008,
    ("both", True, "explain_omitted"): 988,
}


@pytest.mark.parametrize("low", [False, True])
def test_the_d5_worst_cases(threshold, low):
    threshold(0.5 if low else 0.0)
    q_lit, query_cut = tools._hybrid_query_echo("\x01" * 8192)
    measured = {}
    for arms in ("keyword_only", "both"):
        rows = _worst_rows(50 if arms == "keyword_only" else 100)
        result = _worst_result(rows, keyword_only=arms == "keyword_only", low=low)
        assert tools.hybrid_confidence(result) == ("low" if low else "ok")
        for reranked in (False, True):
            token = RERANK_TOKEN if reranked else None
            header = len(tools._hybrid_header(q_lit, query_cut, result, rows, 50, token))
            if arms == "keyword_only":
                bound = tools.HYBRID_RERANK_HEADER_MAX if reranked else tools.HYBRID_HEADER_MAX
                assert header == bound, (arms, reranked, header)
            extra = (tools._rerank_score_field(-1.234e-100),) if reranked else ()
            base = ([] if arms == "keyword_only" else ["section omitted"]) + [
                "tags omitted", "preview omitted", "title_cut", "path_cut"]

            def line(labels, **kw):
                return len(tools._hybrid_row_line(rows[0], 1000, title="", path="", tags=[],
                                                  preview=("none", ""), labels=labels,
                                                  extra=extra, **kw))

            cut = line([*base, "abbreviated"])
            omitted = line([*base, "title omitted", "path omitted", "abbreviated"])
            explain = line([*base, "title omitted", "path omitted", "abbreviated"],
                           omit_explain=True)
            middle = (1 + len(tools.HYBRID_KEYWORD_ONLY_FOOTER) if arms == "keyword_only"
                      else 1 + len(transition_line(9_999_999_999)))
            fixed = header + middle + 1 + 1 + tools.HYBRID_FOOTER_MAX
            value = 25 if reranked else 45
            measured[(arms, reranked, "cut")] = fixed + cut + 2 * value
            measured[(arms, reranked, "omitted")] = fixed + omitted
            measured[(arms, reranked, "explain_omitted")] = fixed + explain
    for key, expected in D5.items():
        assert measured[key] == expected, (key, measured[key])
    # The cases the ladder resolves under the floor.
    assert measured[("keyword_only", True, "omitted")] <= 1000
    assert measured[("both", True, "explain_omitted")] <= 1000
    assert measured[("both", False, "cut")] <= 1000
    assert measured[("keyword_only", False, "cut")] <= 1000


def test_the_floor_arithmetic():
    assert tools.HYBRID_HEADER_MAX == 405 and tools.HYBRID_RERANK_HEADER_MAX == 444
    assert 405 + 345 + 45 + 45 + 75 + 75 + 5 == 995 <= 1000


@pytest.mark.parametrize("low", [False, True])
def test_the_semantic_rerank_header_bound(threshold, low):
    worst = {"path": "x", "title": "", "tags": [], "chunk": None, "stale": True,
             "embedding_truncated": True, "similarity": -0.123}
    q_lit, query_cut = tools._hybrid_query_echo("\x01" * 8192)
    header = tools._semantic_rerank_header(q_lit, query_cut, [worst] * 50, 50, RERANK_TOKEN,
                                           "low" if low else "ok")
    assert len(header) <= tools.SEMANTIC_RERANK_HEADER_MAX == 199, len(header)
    assert len(header) >= 198
    assert (tools.SEMANTIC_RERANK_HEADER_MAX + tools.SEMANTIC_RERANK_ROW_STRUCTURE_MAX
            + 45 + 45 + 75 + 3) <= 1000


@pytest.mark.parametrize("keyword_only", [False, True])
@pytest.mark.parametrize("low", [False, True])
def test_the_real_worst_case_pages_stay_under_the_floor(monkeypatch, threshold, keyword_only,
                                                        low):
    query, title, path, result = _worst_case(monkeypatch, keyword_only=keyword_only)
    threshold(0.99 if low else 0.0)
    if low:
        result.keyword_any, result.keyword_all = result.keyword_rows, 0
        for r in result.rows:
            if r.match:
                r.match = "any"
    if not keyword_only:
        result.rechunk_pending = 9_999_999_999
        result.rows[0].section = {"path": ["H" * 200] * 6, "ordinal": 99999}
    for reranked in (None, True, False):
        outcome = None if reranked is None else _outcome(reranked, len(result.rows))
        scores = ({r.path: -1.234e-05 for r in result.rows} if reranked else None)
        text, paths = tools.render_hybrid_results(query, result, rerank=outcome,
                                                  rerank_scores=scores)
        assert len(text) <= 1000, (keyword_only, low, reranked, len(text))
        assert ("confidence: low" if low else "confidence: ok") in _lines(text)[0]
        assert HYBRID_HINT not in text
        assert paths == [path]


def test_low_confidence_at_the_minimum_cap_reaches_explain_omitted(monkeypatch, threshold):
    """The hybrid-search scenario: 50 `any` rows and no `all` row, the best
    similarity below the threshold, a ten-digit transition count, rerank=true
    falling back with `malformed_response`, and a control-heavy first row
    with 1,000 tags, a section locator and both markers."""
    monkeypatch.setattr(settings, "max_read_response_chars", 1000, raising=False)
    threshold(0.5)
    title = (CONTROLS * 100)[:512]
    path = ("\x01\"\\" * 400)[:1020] + ".md"
    first = HybridRow(path=path, title=title, tags=[f"t{i}" for i in range(1000)],
                      score=2 / 1050, keyword_rank=50, semantic_rank=50, match="any",
                      matched_in=["title+tags+body"], snippet="a «b»", similarity=0.123,
                      chunk="x" * 500, stale=False, embedding_truncated=True,
                      section={"path": ["H" * 200] * 6, "ordinal": 99999})
    stale = dict(stale=True, embedding_truncated=True, chunk=None)
    rows = [first] + [HybridRow(path=f"n{i}.md", title="t", tags=[], score=1 / 1050,
                                keyword_rank=i + 1, semantic_rank=i + 1, match="any",
                                similarity=0.1, **stale) for i in range(49)]
    result = HybridResult(rows=rows, rrf_k=1000, keyword_rows=50, keyword_all=0, keyword_any=50,
                          semantic_rows=50, semantic_chunks=50, overlap=50, snippet_cap=20,
                          rechunk_pending=9_999_999_999, top_similarity=0.123)
    query = (CONTROLS * 1000)[:8192]
    text, paths = tools.render_hybrid_results(query, result, rerank=_outcome(False, 50))
    assert len(text) <= 1000, len(text)
    lines = _lines(text)
    assert "confidence: low" in lines[0] and "reranked: false (malformed_response)" in lines[0]
    assert HYBRID_HINT not in lines
    row = next(line for line in lines if line.startswith("- {"))
    assert "abbreviated" in row and paths == [path]
    # On real input the cut form already fits (the arithmetic worst case
    # mixes a fallback token with a `rerank_score` and full-page counts no
    # one-row header shows; see the design's implementation notes), so the
    # last rung is exercised the way the omitted form is: measured, then
    # forced by a room only the `explain omitted` form fits.
    floor_row = tools._hybrid_abbreviated_row(first, 1000, 1, cut=tools._hybrid_rerank_cut)
    assert "explain omitted" in floor_row and "explain: " not in floor_row
    assert "title omitted" in floor_row and "path omitted" in floor_row
    omitted_row = floor_row.replace(" — explain omitted",
                                    " — explain: 1/(1000+50) + 1/(1000+50)")
    assert len(omitted_row) - len(floor_row) == 19
    row = tools._hybrid_abbreviated_row(first, 1000, len(omitted_row) - 1,
                                        cut=tools._hybrid_rerank_cut)
    assert row == floor_row and row.endswith(" — abbreviated")
    fields = row.split('"} — ', 1)[1].split(" — ")
    assert fields.index("explain omitted") == 3, "in the place of `explain`"
    assert tools._hybrid_abbreviated_row(first, 1000, len(omitted_row),
                                         cut=tools._hybrid_rerank_cut) == omitted_row


# ── 2.2 semantic_search ─────────────────────────────────────────────────────


from tests.test_semantic_rerank import _Session as _SemSession, row as sem  # noqa: E402


def _serve_sem(monkeypatch, rows, pending=0):
    monkeypatch.setattr(_SemSession, "pending", pending)
    monkeypatch.setattr(tools, "async_session", lambda: _SemSession(rows))


@pytest.fixture
def _sem_offline(monkeypatch):
    async def _embed(_text, **_kw):
        return [1.0, 0.0, 0.0]

    monkeypatch.setattr(embeddings, "get_embedding", _embed)


def _call(fn, **kw):
    logged = []
    out = _run(fn, rows=logged, **kw)
    return str(out), logged[0]["params"]


def test_semantic_gibberish_reads_low_and_rows_are_unchanged(monkeypatch, threshold,
                                                             _sem_offline):
    rows = [sem(1, distance=0.533), sem(2, distance=0.536), sem(3, distance=0.54)]
    _serve_sem(monkeypatch, rows, pending=2)
    off, _ = _call(tools.semantic_search_impl, query="xqzvplorth", limit=3)
    threshold(0.50)
    low, params = _call(tools.semantic_search_impl, query="xqzvplorth", limit=3)
    lines = _lines(low)
    assert lines[0].endswith("— confidence: low:")
    assert lines[1] == transition_line(2) and lines[2] == SEMANTIC_HINT
    assert [x for x in lines if x.startswith("- ") or x.startswith("  >")] == [
        x for x in _lines(off) if x.startswith("- ") or x.startswith("  >")]
    assert params["confidence"] == "low" and params["top_similarity"] == 0.467
    assert "error" not in params


def test_semantic_paraphrase_reads_ok(monkeypatch, threshold, _sem_offline):
    threshold(0.50)
    _serve_sem(monkeypatch, [sem(1, distance=0.382), sem(2, distance=0.39)])
    out, params = _call(tools.semantic_search_impl, query="how do I keep an agent", limit=2)
    assert _lines(out)[0].endswith("— confidence: ok:") and "low confidence" not in out
    assert params["confidence"] == "ok" and params["top_similarity"] == 0.618


@pytest.mark.parametrize("thr, expected", [(0.0, "ok"), (0.5, "low")])
def test_semantic_zero_result_line(monkeypatch, threshold, _sem_offline, thr, expected):
    threshold(thr)
    _serve_sem(monkeypatch, [])
    out, params = _call(tools.semantic_search_impl, query="q")
    assert _lines(out)[0] == (
        f"No semantic results for 'q' (embeddings may still be building) — "
        f"confidence: {expected}")
    assert (SEMANTIC_HINT in out) is (expected == "low")
    assert params["confidence"] == expected and "top_similarity" not in params


@pytest.mark.parametrize("thr", [0.0, 0.5, 0.95])
def test_semantic_rerank_true_carries_the_same_value(monkeypatch, threshold, _sem_offline,
                                                     no_reranker, thr):  # noqa: F811
    threshold(thr)
    rows = [sem(i, distance=0.4 + 0.01 * i) for i in range(1, 6)]
    _serve_sem(monkeypatch, rows)
    plain, p_params = _call(tools.semantic_search_impl, query="q", limit=3)
    reranked, r_params = _call(tools.semantic_search_impl, query="q", limit=3, rerank=True)
    header = _lines(reranked)[0]
    assert " truncated — confidence: " in header
    assert header.index("confidence:") < header.index("reranked:")
    assert p_params["confidence"] == r_params["confidence"]
    assert p_params["top_similarity"] == r_params["top_similarity"] == 0.59
    assert (f"confidence: {p_params['confidence']}") in _lines(plain)[0]


def test_semantic_rerank_true_top_is_taken_before_rerank(monkeypatch, threshold, _sem_offline,
                                                         fake):  # noqa: F811
    threshold(0.5)
    rows = [sem(1, distance=0.45), sem(2, distance=0.8), sem(3, distance=0.9)]
    _serve_sem(monkeypatch, rows)
    fake.scorer = lambda q, docs: [0.0, 9.0, 8.0]
    out, params = _call(tools.semantic_search_impl, query="q", limit=3, rerank=True)
    assert "reranked: true" in _lines(out)[0]
    assert params["top_similarity"] == 0.55 and params["confidence"] == "ok"


def test_the_provider_input_refusal_carries_no_token_and_no_keys(monkeypatch, threshold):
    threshold(0.5)

    async def _too_large(_t, **_kw):
        raise refusals.ProviderInputTooLarge("too many tokens")

    monkeypatch.setattr(embeddings, "get_embedding", _too_large)
    _serve_sem(monkeypatch, [sem(1)])
    for rerank_arg in (False, True):
        out, params = _call(tools.semantic_search_impl, query="q", rerank=rerank_arg)
        assert "confidence" not in out and "low confidence" not in out
        assert "confidence" not in params and "top_similarity" not in params
        assert params["error"] == tools._PROVIDER_INPUT_REJECTED_MARKER


# ── 3.1 hybrid usage rows ───────────────────────────────────────────────────


def _hybrid_call(monkeypatch, session, **kw):
    monkeypatch.setattr(settings, "keyword_snippet_rows", 0, raising=False)
    monkeypatch.setattr(tools, "async_session", lambda: session)
    return _call(tools.hybrid_search_impl, **kw)


def test_hybrid_records_the_evidence_with_the_threshold_off(monkeypatch):
    sem_rows = [sem_row(1, "a.md", distance=0.5329), sem_row(2, "b.md", distance=0.54)]
    out, params = _hybrid_call(monkeypatch, _Session(ranked=[[]], semantic=[sem_rows]),
                               query="xqzvplorth", limit=5)
    assert "confidence: ok" in _lines(out)[0]
    assert params["confidence"] == "ok" and params["keyword_all"] == 0
    assert params["top_similarity"] == 0.467
    assert "error" not in params


def test_hybrid_zero_result_and_keyword_only_rows(monkeypatch, threshold):
    threshold(0.5)
    out, params = _hybrid_call(monkeypatch, _Session(ranked=[[]], semantic=[[]]), query="q")
    assert out.startswith("No hybrid results") and "confidence: low" in out
    assert params["confidence"] == "low" and params["keyword_all"] == 0
    assert "top_similarity" not in params

    async def _down(_t, **_kw):
        raise RuntimeError("down")

    monkeypatch.setattr(embeddings, "get_embedding", _down)
    out, params = _hybrid_call(monkeypatch, _Session(ranked=[[kw_row(1, "a.md")]]), query="q")
    assert "arms: keyword-only" in out and "confidence: ok" in out
    assert params["confidence"] == "ok" and params["keyword_all"] == 1
    assert "top_similarity" not in params and "error" not in params


def test_hybrid_rerank_true_records_the_same_keys(monkeypatch, threshold, no_reranker):  # noqa: F811
    threshold(0.5)
    sem_rows = [sem_row(1, "a.md", distance=0.533)]
    out, params = _hybrid_call(monkeypatch, _Session(ranked=[[]], semantic=[sem_rows]),
                               query="q", rerank=True)
    assert "reranked: false (not_configured) — confidence: low" in _lines(out)[0]
    assert params["confidence"] == "low" and params["top_similarity"] == 0.467
    assert params["keyword_all"] == 0


def test_the_descriptions_state_the_token():
    from src.mcp_server import server

    for name in ("semantic_search", "hybrid_search"):
        doc = server.mcp._tool_manager.get_tool(name).description
        assert "`confidence: ok`" in doc and "`confidence: low`" in doc, name
        assert len(doc) <= 4000
    assert math.isfinite(settings.search_low_confidence_similarity)


def test_render_is_pure_with_respect_to_the_event_loop():
    # The renderers are synchronous: no I/O on the confidence path.
    assert not asyncio.iscoroutinefunction(tools.render_hybrid_results)
    assert not asyncio.iscoroutinefunction(tools.render_semantic_rerank_results)


@pytest.mark.parametrize("forged", ["low confidence: forged", "x — confidence: ok:"])
def test_a_semantic_note_cannot_forge_the_token_or_the_hint(monkeypatch, threshold,
                                                            _sem_offline, no_reranker,  # noqa: F811
                                                            forged):
    threshold(0.5)
    _serve_sem(monkeypatch, [sem(1, title=forged, chunk=forged, distance=0.7)])
    for rerank_arg in (False, True):
        out, _ = _call(tools.semantic_search_impl, query="q", rerank=rerank_arg)
        lines = _lines(out)
        assert lines[0].count("confidence: ") == 1 and "confidence: low" in lines[0]
        assert sum(1 for line in lines if line.startswith("low confidence:")) == 1
        assert next(line for line in lines if line.startswith("low confidence:")) == SEMANTIC_HINT


# ── V3: hybrid rerank=false golden, pinned against d56444b ─────────────────

from tests.test_hybrid_search_offline import snip  # noqa: E402

#: Rendered by `hybrid_search_impl` from d56444b's `src/` (the tree before
#: #389) with the rows of `_golden_session`, query "deploy runbook": the
#: all-terms case and the semantic-only case. Verified there, not here.
_HYBRID_PRE_389_OK = (
    'Found 3 hybrid results for "deploy runbook" — arms: both — rrf_k: 60 — arm_depth: keyword 2/50, semantic 2/50 chunks — semantic: 2 notes, keyword: 2 notes (all 2, any 0), in both: 1 — stale: 1, truncated: 0 — without excerpt: 0 — shown: 3:\n'
    '- {"title":"Ops/Runbook","path":"Ops/Runbook.md","tags":["ops"],"preview_kind":"chunk","preview":"Deploy the\\\\nrunbook"} — score: 0.03279 — semantic_rank: 1 (similarity 0.800) — keyword_rank: 1 (match: all, matched_in: title,body) — explain: 1/(60+1) + 1/(60+1)\n'
    '- {"title":"Notes/Other","path":"Notes/Other.md","tags":[],"preview_kind":"stale_notice","preview":"(preview withheld — this note changed after it was embedded, so the stored excerpt is superseded text. Call `read_note` for the current content.)"} — score: 0.01613 — semantic_rank: 2 (similarity 0.600) — keyword_rank: - — explain: 1/(60+2), semantic only — stale: true\n'
    '- {"title":"Ops/Backups","path":"Ops/Backups.md","tags":[],"preview_kind":"none","preview":""} — score: 0.01613 — semantic_rank: - — keyword_rank: 2 (match: all, matched_in: body) — explain: 1/(60+2), keyword only'
)
_HYBRID_PRE_389_LOW = (
    'Found 2 hybrid results for "deploy runbook" — arms: both — rrf_k: 60 — arm_depth: keyword 0/50, semantic 2/50 chunks — semantic: 2 notes, keyword: 0 notes (all 0, any 0), in both: 0 — stale: 1, truncated: 0 — without excerpt: 0 — shown: 2:\n'
    '- {"title":"Ops/Runbook","path":"Ops/Runbook.md","tags":["ops"],"preview_kind":"chunk","preview":"Deploy the\\\\nrunbook"} — score: 0.01639 — semantic_rank: 1 (similarity 0.800) — keyword_rank: - — explain: 1/(60+1), semantic only\n'
    '- {"title":"Notes/Other","path":"Notes/Other.md","tags":[],"preview_kind":"stale_notice","preview":"(preview withheld — this note changed after it was embedded, so the stored excerpt is superseded text. Call `read_note` for the current content.)"} — score: 0.01613 — semantic_rank: 2 (similarity 0.600) — keyword_rank: - — explain: 1/(60+2), semantic only — stale: true'
)


def _golden_session(keyword_too=True):
    keyword = [kw_row(1, "Ops/Runbook.md", tags=["ops"]), kw_row(2, "Ops/Backups.md")]
    semantic = [sem_row(1, "Ops/Runbook.md", distance=0.2, chunk="Deploy the\\nrunbook", tags=["ops"]),
                sem_row(3, "Notes/Other.md", distance=0.4, chunk="other text", stale=True)]
    if not keyword_too:
        return _Session(ranked=[[]], semantic=[semantic])
    return _Session(ranked=[keyword], semantic=[semantic],
                    snippet=[[snip(1, hl="deploy «runbook»", title=True), snip(2)]])


@pytest.mark.parametrize("case", ["ok", "low"])
def test_hybrid_rerank_false_golden_differs_by_the_token_alone(monkeypatch, threshold, case):
    monkeypatch.setattr(settings, "keyword_snippet_rows", 20, raising=False)
    threshold(0.0 if case == "ok" else 0.95)
    session = _golden_session(case == "ok")
    monkeypatch.setattr(tools, "async_session", lambda: session)
    out = str(_run(tools.hybrid_search_impl, query="deploy runbook", limit=10))
    before = _HYBRID_PRE_389_OK if case == "ok" else _HYBRID_PRE_389_LOW
    token = f" — confidence: {case}"
    expected = before.replace(" — arms: both", " — arms: both" + token, 1)
    if case == "low":
        head, rest = expected.split("\n", 1)
        expected = head + "\n" + HYBRID_HINT + "\n" + rest
    assert out == expected
