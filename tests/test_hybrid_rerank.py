"""`hybrid_search(rerank=…)` (#367; design D6, D9, D10; tasks 5.1, 5.2).

Offline, on hybrid's scripted session: `rerank=false` adds no statement and
renders as before; `rerank=true` takes the first 50 fused rows, reads the
excerpt for candidates without a usable semantic chunk in the same snapshot
(after the snippets), reranks after the session closes, puts the token after
`arms:` and `rerank_score` after hybrid's server fields, and stays inside the
1,000-character cap with the 20–25 cut floor."""
from __future__ import annotations

import json
import re
from types import SimpleNamespace

import pytest

from tests._rerank_support import fake, fake_server, no_reranker  # noqa: F401
from tests.test_hybrid_search_offline import (  # noqa: F401
    CONTROLS, _Result, _Session, _offline, _split_row, kw_row, sem_row, snip,
)
import src.mcp_server.tools as tools
from src.config import settings
from src.services import embeddings, hybrid, refusals, rerank
from src.services.hybrid import HybridResult, HybridRow


class _ExcerptSession(_Session):
    """Answers the D6 excerpt statement from `excerpts` ({path: text})."""

    def __init__(self, excerpts=None, **answers):
        super().__init__(**answers)
        self.excerpts = excerpts or {}
        self.closed_before_rerank: bool | None = None
        self.open = False

    async def execute(self, clause, *a, **k):
        sql = str(clause)
        if "left(notes_metadata.body_excerpt" in sql.lower():
            self.log.append(("excerpt", sql))
            return _Result([SimpleNamespace(file_path=p, excerpt=t)
                            for p, t in self.excerpts.items()])
        return await super().execute(clause, *a, **k)

    async def __aenter__(self):
        self.open = True
        return self

    async def __aexit__(self, *_a):
        self.open = False
        return None


class _Captured:
    def __init__(self):
        self.rows = []

    async def __call__(self, tool, params, duration_ms, response_size):
        self.rows.append((tool, params, duration_ms))


@pytest.fixture
def captured(monkeypatch):
    cap = _Captured()
    monkeypatch.setattr(tools, "_log_usage", cap)
    return cap


def _serve(monkeypatch, session):
    monkeypatch.setattr(tools, "async_session", lambda: session)


def _corpus(n_kw=30, n_sem=30):
    keyword = [kw_row(i, f"k{i:02d}.md") for i in range(n_kw)]
    semantic = [sem_row(100 + i, f"s{i:02d}.md", distance=0.01 * i, chunk=f"semantic {i}")
                for i in range(n_sem)]
    return keyword, semantic


# ── D6: the service ─────────────────────────────────────────────────────────


async def test_rerank_false_adds_no_statement_and_keeps_the_page(monkeypatch):
    keyword, semantic = _corpus()
    a = _ExcerptSession(ranked=[keyword], semantic=[semantic], snippet=[[snip(0)]])
    plain = await hybrid.hybrid_search(a, "q", limit=10)
    b = _ExcerptSession(ranked=[keyword], semantic=[semantic], snippet=[[snip(0)]])
    flagged = await hybrid.hybrid_search(b, "q", limit=10, rerank_candidates=True)
    assert len(plain.rows) == 10 and len(flagged.rows) == 50
    assert [r.path for r in flagged.rows[:10]] == [r.path for r in plain.rows]
    assert not any(k == "excerpt" for k, _ in a.log)
    assert [entry for entry in b.log if entry[0] != "excerpt"] == a.log
    # The excerpt statement comes after the snippet statements, once.
    kinds = [k for k, _ in b.log]
    assert kinds.count("excerpt") == 1
    assert kinds.index("excerpt") > max(i for i, k in enumerate(kinds) if k == "snippet")
    assert all(r.rerank_text is None for r in plain.rows)


async def test_candidate_text_by_arm(monkeypatch):
    keyword = [kw_row(1, "both.md", title="Both"), kw_row(2, "kw.md", title="Kw"),
               kw_row(3, "null.md", title="Null")]
    long_chunk = "x" * 700 + "PAST-500" + "y" * 1000
    semantic = [sem_row(1, "both.md", title="Both", chunk=long_chunk),
                sem_row(4, "stale.md", title="Stale", chunk="SUPERSEDED", stale=True)]
    session = _ExcerptSession(
        excerpts={"kw.md": "keyword excerpt", "stale.md": "current excerpt", "null.md": None},
        ranked=[keyword], semantic=[semantic], snippet=[[]])
    result = await hybrid.hybrid_search(session, "q", limit=10, rerank_candidates=True)
    by = {r.path: r for r in result.rows}
    assert "PAST-500" in by["both.md"].rerank_text and by["both.md"].chunk == long_chunk
    assert by["kw.md"].rerank_text == "keyword excerpt"
    assert by["stale.md"].rerank_text == "current excerpt"
    assert by["null.md"].rerank_text is None
    sql = next(s for k, s in session.log if k == "excerpt")
    assert "notes_metadata.user_id IS NULL" in sql, "the owner predicate"


async def test_no_excerpt_statement_when_every_candidate_has_a_chunk():
    semantic = [sem_row(i, f"s{i}.md") for i in range(3)]
    session = _ExcerptSession(ranked=[[]], semantic=[semantic])
    await hybrid.hybrid_search(session, "q", limit=10, rerank_candidates=True)
    assert not any(k == "excerpt" for k, _ in session.log)


# ── D9/D10: the tool ────────────────────────────────────────────────────────


async def test_reranked_rows_carry_the_score_after_hybrids_fields(monkeypatch, captured, fake):
    keyword, semantic = _corpus()
    session = _ExcerptSession(excerpts={f"k{i:02d}.md": f"kw {i}" for i in range(30)},
                              ranked=[keyword], semantic=[semantic], snippet=[[]])
    _serve(monkeypatch, session)

    def scorer(q, docs):
        assert not session.open, "the session is closed before the exchange"
        return [100.0 if d.startswith("s20") else 1.0 for d in docs]

    fake.scorer = scorer
    out = await tools.hybrid_search_impl("q", limit=10, rerank=True)
    assert len(fake.requests[-1]["documents"]) == 50
    header = out.splitlines()[0]
    assert re.search(r" — arms: both — reranked: true \(candidates: 50\) — rrf_k: ", header)
    rows = [_split_row(line) for line in out.splitlines() if line.startswith("- {")]
    assert rows[0][0]["path"] == "s20.md"
    for obj, tail in rows:
        assert list(obj) == ["title", "path", "tags", "preview_kind", "preview"]
        fields = tail.split(" — ")[1:]
        assert fields[0].startswith("score: ")
        explain = next(i for i, f in enumerate(fields) if f.startswith("explain: "))
        score_at = next(i for i, f in enumerate(fields) if f.startswith("rerank_score: "))
        assert score_at > explain and tail.count("rerank_score:") == 1
    _, params, _ = captured.rows[-1]
    assert params["rerank"] is True and params["reranked"] is True
    assert params["rerank_candidates"] == 50 and isinstance(params["rerank_ms"], int)
    assert params["result_paths"][0] == "s20.md"
    assert params["result_paths"] == [o["path"] for o, _ in rows][:10]


async def test_a_fallback_equals_the_unreranked_rerank_true_baseline(monkeypatch, captured, fake):
    keyword, semantic = _corpus()
    answers = dict(ranked=[keyword], semantic=[semantic], snippet=[[]])
    fake.mode = "status:503"
    _serve(monkeypatch, _ExcerptSession(**{k: list(v) for k, v in answers.items()}))
    fb = await tools.hybrid_search_impl("q", limit=10, rerank=True)
    _, params, _ = captured.rows[-1]
    assert params["reranked"] is False and params["rerank_reason"] == "http_error"
    assert "error" not in params
    monkeypatch.setattr(settings, "rerank_url", "", raising=False)
    _serve(monkeypatch, _ExcerptSession(**{k: list(v) for k, v in answers.items()}))
    nc = await tools.hybrid_search_impl("q", limit=10, rerank=True)
    _serve(monkeypatch, _ExcerptSession(**{k: list(v) for k, v in answers.items()}))
    legacy = await tools.hybrid_search_impl("q", limit=10)
    assert "reranked: false (http_error)" in fb.splitlines()[0]
    assert "reranked: false (not_configured)" in nc.splitlines()[0]
    assert fb.splitlines()[1:] == nc.splitlines()[1:]
    assert "rerank_score" not in fb
    # In order, the same rows as the legacy page (which uses the same encoding).
    assert fb.splitlines()[1:] == legacy.splitlines()[1:]
    assert "reranked" not in legacy


async def test_rerank_false_is_unchanged(monkeypatch, captured):
    keyword, semantic = _corpus(5, 5)
    session = _ExcerptSession(ranked=[keyword], semantic=[semantic], snippet=[[]])
    _serve(monkeypatch, session)
    out = await tools.hybrid_search_impl("q", limit=10)
    direct = tools.render_hybrid_results(
        "q", await hybrid.hybrid_search(
            _ExcerptSession(ranked=[keyword], semantic=[semantic], snippet=[[]]), "q", limit=10))
    assert out == direct[0]
    _, params, _ = captured.rows[-1]
    assert params["rerank"] is False
    assert not {"reranked", "rerank_reason", "rerank_ms", "rerank_candidates"} & set(params)


async def test_a_keyword_only_call_still_reranks_with_both_markers(monkeypatch, captured, fake):
    async def _down(_t, **_kw):
        raise RuntimeError("provider down")

    monkeypatch.setattr(embeddings, "get_embedding", _down)
    keyword = [kw_row(i, f"k{i}.md") for i in range(5)]
    _serve(monkeypatch, _ExcerptSession(excerpts={f"k{i}.md": f"text {i}" for i in range(5)},
                                        ranked=[keyword], snippet=[[]]))
    out = await tools.hybrid_search_impl("q", limit=5, rerank=True)
    header = out.splitlines()[0]
    assert "arms: keyword-only (semantic unavailable: provider_error)" in header
    assert "reranked: true (candidates: 5)" in header
    assert fake.requests[-1]["documents"][0] == "k0\n\ntext 0"


async def test_the_zero_result_line_carries_the_token(monkeypatch, captured, fake):
    _serve(monkeypatch, _ExcerptSession(ranked=[[]], semantic=[[]]))
    out = await tools.hybrid_search_impl("q", rerank=True)
    assert out.startswith('No hybrid results for "q" — arms: both — '
                          "reranked: false (no_candidates)")
    assert fake.requests == []


# ── the bound (D9) ──────────────────────────────────────────────────────────


def test_the_fixed_bounds_with_rerank():
    worst_rows = [
        HybridRow(path=f"{i}.md", title="", tags=[], score=2 / 1050, keyword_rank=50,
                  semantic_rank=50, match="any", similarity=-0.123, stale=True,
                  embedding_truncated=True, no_excerpt=True, matched_in=["title+tags+body"])
        for i in range(50)
    ]
    result = HybridResult(
        rows=worst_rows, arms="keyword_only", semantic_unavailable="provider_input_rejected",
        rrf_k=1000, keyword_rows=50, keyword_all=50, keyword_any=50, semantic_rows=50,
        semantic_chunks=50, overlap=50, snippet_cap=49)
    q_lit, query_cut = tools._hybrid_query_echo("\x01" * 8192)
    token = "reranked: false (malformed_response)"
    header = tools._hybrid_header(q_lit, query_cut, result, worst_rows, 50, token)
    assert len(header) <= tools.HYBRID_RERANK_HEADER_MAX == 431, len(header)
    labels = ["tags omitted", "preview omitted", "title_cut", "path_cut", "abbreviated"]
    structure = tools._hybrid_row_line(
        worst_rows[0], 1000, title="", path="", tags=[], preview=("none", ""), labels=labels,
        extra=(tools._rerank_score_field(-1.234e-100),))
    assert len(structure) <= tools.HYBRID_RERANK_ROW_STRUCTURE_MAX == 367, len(structure)
    print(f"\n[hybrid bound] rerank=true abbreviated structure: {len(structure)}")
    assert max(len(tools._rerank_cap_footer(n)) for n in range(1, 51)) <= 75
    # Since #391 the cut form's floor sum exceeds the cap in this one worst
    # case, so the row continues to the labelled omitted form (≤ 984).
    assert 431 + 367 + 25 + 25 + 75 + 75 + 5 == 1003 > 1000
    assert 431 + 367 + 31 + 75 + 75 + 5 == 984 <= 1000


@pytest.mark.parametrize("split", [False, True])
@pytest.mark.parametrize("mode", ["correct", "not_json"])
async def test_the_hybrid_worst_case_with_rerank(monkeypatch, captured, fake, mode, split):
    monkeypatch.setattr(settings, "max_read_response_chars", 1000, raising=False)
    monkeypatch.setattr(settings, "hybrid_rrf_k", 1000, raising=False)
    monkeypatch.setattr(settings, "keyword_snippet_rows", 20, raising=False)
    query = (CONTROLS * 1000)[:8192]
    title = (CONTROLS * 100)[:512]
    path = ("\x01\"\\" * 400)[:1020] + ".md"
    tags = [f"t{i}" for i in range(1000)]

    async def _fail(_text, **_kw):
        raise refusals.ProviderInputTooLarge("too large")

    monkeypatch.setattr(embeddings, "get_embedding", _fail)
    keyword = [kw_row(1, path, title=title, tags=tags)] + [
        kw_row(10 + i, f"n{i}.md") for i in range(49)]
    session = _ExcerptSession(
        excerpts={path: "e" * 3000},
        ranked=[keyword],
        snippet=[[snip(1, hl="a \x02b\x03", title=False, body=not split)]
                 + [snip(10 + i) for i in range(19)]]
        + ([[SimpleNamespace(_mapping={"id": 1, "head": "opening words"})]] if split else []))
    _serve(monkeypatch, session)
    fake.mode = mode
    fake.scorer = (lambda q, d: [-1.234e-05 if i == 0 else -9.0 for i in range(len(d))]) \
        if mode == "correct" else None
    text = await tools.hybrid_search_impl(query, limit=50, rerank=True)
    assert len(text) <= 1000, len(text)
    lines = text.splitlines()
    assert "arms: keyword-only (semantic unavailable: provider_input_rejected)" in lines[0]
    assert ("reranked: true (candidates: 50)" if mode == "correct"
            else "reranked: false (malformed_response)") in lines[0]
    rows = [line for line in lines if line.startswith("- {")]
    assert len(rows) == 1
    if split:
        # `matched_in: title+tags+body` (#391). These inputs' real header is
        # shorter than the 431 worst case, so the 20–25 cut form fits here
        # (pinned); the arithmetic worst case (1,003 > 1,000) takes the
        # omitted form, pinned by the direct-render test below.
        assert "matched_in: title+tags+body" in rows[0]
        assert '"title":""' not in rows[0] and '"path":""' not in rows[0]
    for key in ("title", "path"):
        lit = re.search(r'"%s":("(?:[^"\\]|\\.)*")' % key, rows[0]).group(1)
        assert 20 <= len(lit) - 2 <= 25, (key, lit)
        assert {"title": title, "path": path}[key].startswith(json.loads(lit))
    assert lines[-1].startswith("Response cap: 49 omitted — more: true")
    assert tools.HYBRID_KEYWORD_ONLY_FOOTER in lines


# ── #355 merge: the transition line and the locator on rerank=true pages ───


def _outcome(reranked, n):
    if reranked:
        return rerank.RerankOutcome(True, list(range(n)), n, scores=[1.0] * n, ms=1)
    return rerank.RerankOutcome(False, list(range(n)), n, reason="malformed_response")


@pytest.mark.parametrize("reranked", [True, False])
def test_rerank_true_carries_the_transition_line_and_locators(reranked):
    from tests.test_hybrid_search_offline import _result, _row

    rows = [
        _row("a.md", keyword_rank=None, semantic_rank=1, similarity=0.9, chunk="c",
             section={"path": ["Plan", "Risks"], "ordinal": 7}),
        _row("b.md", keyword_rank=2, semantic_rank=2, similarity=0.8, chunk="c",
             section={"path": [], "ordinal": 0}),
        _row("c.md"),
    ]
    plain, _ = tools.render_hybrid_results("q", _result(rows, rechunk_pending=3))
    scores = {r.path: 1.0 for r in rows} if reranked else None
    text, _ = tools.render_hybrid_results("q", _result(rows, rechunk_pending=3),
                                          rerank=_outcome(reranked, 3), rerank_scores=scores)
    lines = text.splitlines()
    assert lines[1] == plain.splitlines()[1] == embeddings.transition_line(3)
    tails = {_split_row(line)[0]["path"]: _split_row(line)[1]
             for line in lines if line.startswith("- {")}
    assert tails["a.md"].endswith(' — § "Plan/Risks" (#7)')
    assert tails["b.md"].endswith(" — § (top of note)")
    assert "§" not in tails["c.md"]
    if reranked:
        fields = tails["a.md"].split(" — ")
        assert fields.index("rerank_score: 1") < fields.index('§ "Plan/Risks" (#7)')


@pytest.mark.parametrize("reranked", [True, False])
def test_the_hybrid_worst_case_with_a_transition_line_and_a_locator(monkeypatch, reranked):
    """Both arms (so the transition line, no keyword-only footer), the
    longest transition line and locator: still ≤ 1,000 with rerank=true."""
    from tests.test_hybrid_search_offline import _worst_case

    query, title, path, result = _worst_case(monkeypatch, keyword_only=False)
    result.rechunk_pending = 9_999_999_999
    result.rows[0].section = {"path": ["H" * 200] * 6, "ordinal": 99999}
    n = len(result.rows)
    scores = {r.path: -1.234e-05 for r in result.rows} if reranked else None
    text, paths = tools.render_hybrid_results(query, result, rerank=_outcome(reranked, n),
                                              rerank_scores=scores)
    assert len(text) <= 1000, len(text)
    lines = text.splitlines()
    assert lines[1].startswith("index_transition: 9999999999 notes not yet re-chunked")
    row = next(line for line in lines if line.startswith("- {"))
    assert "section omitted" in row and "abbreviated" in row
    assert paths == [path]


def test_the_floor_arithmetic_with_the_transition_line():
    """After #355 a both-arms page reserves the `index_transition:` line
    (≤ 127 characters at a ten-digit count). With it the cut form sums to
    1,004 at the worst case, so that case falls through to the labelled
    omitted form, which fits: the 1,000 bound holds, the 20–25 cut does not
    always survive (recorded in the archived design's implementation notes)."""
    from src.services.embeddings import transition_line

    rows = [HybridRow(path=f"{i}.md", title="", tags=[], score=2 / 1050, keyword_rank=50,
                      semantic_rank=50, match="any", similarity=-0.123, stale=True,
                      embedding_truncated=True, no_excerpt=True, matched_in=["title", "body"])
            for i in range(50)]
    result = HybridResult(rows=rows, arms="both", rrf_k=1000, keyword_rows=50, keyword_all=50,
                          keyword_any=50, semantic_rows=50, semantic_chunks=50, overlap=50,
                          snippet_cap=49)
    q_lit, query_cut = tools._hybrid_query_echo("\x01" * 8192)
    header = len(tools._hybrid_header(q_lit, query_cut, result, rows, 50,
                                      "reranked: false (malformed_response)"))
    line = len(transition_line(9_999_999_999))
    score = (tools._rerank_score_field(-1.234e-100),)
    base = ["section omitted", "tags omitted", "preview omitted", "title_cut", "path_cut"]
    cut_form = len(tools._hybrid_row_line(rows[0], 1000, title="", path="", tags=[],
                                          preview=("none", ""), labels=[*base, "abbreviated"],
                                          extra=score))
    omitted = len(tools._hybrid_row_line(
        rows[0], 1000, title="", path="", tags=[], preview=("none", ""),
        labels=[*base, "title omitted", "path omitted", "abbreviated"], extra=score))
    assert (header, line, cut_form) == (371, 127, 378)
    assert header + 1 + line + 1 + cut_form + 25 + 25 + 1 + 75 == 1004
    assert header + 1 + line + 1 + omitted + 1 + 75 <= 1000


def test_the_rerank_worst_case_takes_the_omitted_form_when_the_cut_cannot_fit(monkeypatch):
    """#391 V6: with `matched_in: title+tags+body` the cut-form floor sum is
    431 + 367 + 25 + 25 + 75 + 75 + 5 = 1,003 > 1,000, so at the arithmetic
    worst case the first row must take the labelled omitted form. Pinned by
    rendering at the budget the omitted form needs, which the cut form
    exceeds."""
    from tests.test_hybrid_search_offline import _worst_case

    query, _title, _path, result = _worst_case(monkeypatch, keyword_only=True, split=True)
    n = len(result.rows)
    outcome = rerank.RerankOutcome(False, list(range(n)), n, reason="malformed_response")
    omitted, _ = tools.render_hybrid_results(query, result, rerank=outcome, budget=1)
    cut, _ = tools.render_hybrid_results(query, result, rerank=outcome, budget=1000)
    assert '"title":""' in omitted and '"title":""' not in cut
    L_omit = len(omitted)
    assert len(cut) > L_omit
    text, _ = tools.render_hybrid_results(query, result, rerank=outcome, budget=L_omit)
    row = next(line for line in text.splitlines() if line.startswith("- {"))
    assert "matched_in: title+tags+body" in row
    assert '"title":""' in row and '"path":""' in row
    for label in ("title_cut", "title omitted", "path_cut", "path omitted", "abbreviated"):
        assert label in row, label
    assert len(text) <= L_omit
