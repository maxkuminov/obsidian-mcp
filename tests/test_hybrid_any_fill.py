"""#403: `match: any` (disjunctive fill) keyword rows enter `hybrid_search`'s
RRF at `rank + HYBRID_ANY_FILL_OFFSET` (50), so a fill row can never outrank
an all-terms row through the keyword arm alone.

Offline. `fuse` is exercised directly; the service runs with its two arm
cores stubbed (`search.keyword_arm_rows`, `embeddings.semantic_arm_rows`) so
a test can hand it exact `all`/`any` rows; the renderer is exercised on what
the service returns. The pre-#403 fusion is the harness override set to 0.
"""
from __future__ import annotations

import ast
import asyncio
import os
import random
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
os.chdir(tempfile.gettempdir())

import src.mcp_server.tools as tools  # noqa: E402
from src.config import settings  # noqa: E402
from src.services import embeddings, hybrid, search  # noqa: E402
from src.services.hybrid import HYBRID_ANY_FILL_OFFSET, fuse  # noqa: E402

REPO = Path(__file__).resolve().parent.parent


def _old_fuse(keyword_paths, semantic_paths, k):
    """The #354 fusion, verbatim, as the incumbent oracle."""
    kr = {p: i + 1 for i, p in enumerate(keyword_paths)}
    sr = {p: i + 1 for i, p in enumerate(semantic_paths)}
    out = []
    for p in dict.fromkeys([*keyword_paths, *semantic_paths]):
        k_rank, s_rank = kr.get(p), sr.get(p)
        score = sum(1.0 / (k + r) for r in (k_rank, s_rank) if r is not None)
        best = min(r for r in (k_rank, s_rank) if r is not None)
        out.append((p, score, k_rank, s_rank, best))
    out.sort(key=lambda t: (-t[1], t[4], t[0]))
    return [t[:4] for t in out]


def _order(out):
    return [p for p, *_ in out]


# ── the constant and D1's bound ─────────────────────────────────────────────


def test_the_offset_is_the_arm_depth():
    assert HYBRID_ANY_FILL_OFFSET == hybrid.HYBRID_ARM_DEPTH == 50


@pytest.mark.parametrize("k", [1, 60, 1000])
def test_a_fill_row_never_outranks_an_all_terms_row_through_the_keyword_arm(k):
    """Release-bar fixture: every all-terms rank a and every fill rank f in
    1–50 (any arm position, a ≠ f), keyword arm only: the all-terms row
    leads."""
    filler = [f"x{i:02d}.md" for i in range(50)]
    for a in range(1, 51):
        for f in range(1, 51):
            if a == f:
                continue
            paths = list(filler)
            paths[a - 1], paths[f - 1] = "A.md", "F.md"
            any_paths = frozenset(p for p in paths if p != "A.md")
            out = fuse(paths, [], k, any_paths=any_paths, any_offset=HYBRID_ANY_FILL_OFFSET)
            order = _order(out)
            assert order.index("A.md") < order.index("F.md"), (k, a, f)
    # The bound itself, for every k in the setting's range.
    for kk in range(1, 1001):
        assert 1 / (kk + 51) < 1 / (kk + 50)


def test_the_spec_scenarios():
    # 1/110 vs 1/111.
    kw = [f"c{i:02d}.md" for i in range(49)] + ["A.md"]
    out = fuse(["B.md"] + kw, [], 60, any_paths={"B.md"}, any_offset=50)
    scores = {p: s for p, s, *_ in out}
    assert scores["B.md"] == pytest.approx(1 / 111)
    # A is at arm position 51 in that list; take it from a list where it is 50.
    out = fuse(kw, [], 60)
    assert dict((p, s) for p, s, *_ in out)["A.md"] == pytest.approx(1 / 110)
    # E (sem 2, any 20) before J (sem 9, any 1).
    keyword = ["J.md"] + [f"k{i:02d}.md" for i in range(18)] + ["E.md"]
    semantic = ["s0.md", "E.md"] + [f"s{i}.md" for i in range(1, 7)] + ["J.md"]
    any_paths = frozenset(keyword)
    new = fuse(keyword, semantic, 60, any_paths=any_paths, any_offset=50)
    s = {p: sc for p, sc, *_ in new}
    assert s["E.md"] == pytest.approx(1 / 62 + 1 / 130)
    assert s["J.md"] == pytest.approx(1 / 69 + 1 / 111)
    assert _order(new).index("E.md") < _order(new).index("J.md")
    old = {p: sc for p, sc, *_ in _old_fuse(keyword, semantic, 60)}
    assert old["J.md"] > old["E.md"], "before #403 J led"
    # A fill-only note sorts below a semantic-only note at rank 50.
    semantic = [f"s{i:02d}.md" for i in range(49)] + ["S.md"]
    out = fuse(["F.md"], semantic, 60, any_paths={"F.md"}, any_offset=50)
    assert _order(out)[-1] == "F.md" and _order(out)[-2] == "S.md"


def test_the_tie_break_uses_unoffset_ranks():
    """Equal scores sort by the smaller **unoffset** arm rank, then path. A
    keyword-only `all` row and a semantic-only row at rank 1 tie at 1/61 and
    the path decides; a fill row's tie key is its unoffset rank, not 51."""
    assert _order(fuse(["Z.md"], ["A.md"], 60)) == ["A.md", "Z.md"]
    out = fuse(["F.md", "Z.md"], ["A.md"], 60, any_paths={"F.md"}, any_offset=50)
    assert out[-1][0] == "F.md" and out[-1][2] == 1
    # Two fill-bearing notes with swapped arm ranks: the order is by score,
    # and the sort key is (score, unoffset best rank, path) throughout.
    keyword = [f"p{i:02d}.md" for i in range(50)]
    keyword[9], keyword[19] = "G.md", "H.md"           # G any 10, H any 20
    semantic = [f"q{i:02d}.md" for i in range(50)]
    semantic[19], semantic[9] = "G.md", "H.md"          # G sem 20, H sem 10
    any_paths = frozenset(keyword)
    out = fuse(keyword, semantic, 60, any_paths=any_paths, any_offset=50)
    s = {p: sc for p, sc, *_ in out}
    # G = 1/120 + 1/80, H = 1/130 + 1/70: not a tie, H leads on score.
    assert s["H.md"] > s["G.md"]
    order = _order(out)
    assert order.index("H.md") < order.index("G.md")
    # The sort key: score, then unoffset best rank, then path.
    keys = [(-sc, min(r for r in (kr, sr) if r is not None), p) for p, sc, kr, sr in out]
    assert keys == sorted(keys)


def test_offset_zero_is_the_incumbent_fusion():
    rng = random.Random(403)
    pool = [f"n{i:03d}.md" for i in range(150)]
    for _ in range(300):
        keyword = rng.sample(pool, rng.randint(0, 50))
        semantic = rng.sample(pool, rng.randint(0, 50))
        if not keyword and not semantic:
            continue
        any_paths = frozenset(rng.sample(keyword, rng.randint(0, len(keyword))))
        k = rng.choice([1, 60, 1000])
        assert fuse(keyword, semantic, k) == _old_fuse(keyword, semantic, k)
        assert fuse(keyword, semantic, k, any_paths=any_paths, any_offset=0) == \
            _old_fuse(keyword, semantic, k)


def test_union_identity_and_the_exchange_rule_on_random_arms():
    """Union identity before the cut; at every cut the exchange rule:
    non-fill rows keep their relative order and every membership change is a
    fill-bearing row exchanged for another union member."""
    rng = random.Random(4030)
    pool = [f"n{i:03d}.md" for i in range(150)]
    for _ in range(300):
        n_all = rng.randint(0, 20)
        keyword = rng.sample(pool, rng.randint(n_all, 50))
        any_paths = frozenset(keyword[n_all:])
        semantic = rng.sample(pool, rng.randint(0, 50))
        if not keyword and not semantic:
            continue
        old = _order(_old_fuse(keyword, semantic, 60))
        new = _order(fuse(keyword, semantic, 60, any_paths=any_paths, any_offset=50))
        assert set(old) == set(new), "union identity before slicing"
        non_fill = [p for p in old if p not in any_paths]
        assert [p for p in new if p not in any_paths] == non_fill
        for cut in (1, 10, 50):
            o, n = set(old[:cut]), set(new[:cut])
            assert all(p in any_paths for p in o - n) or all(p in any_paths for p in n - o), \
                "every exchange involves a fill-bearing row"
            # A non-fill row can only enter a window by displacing a fill row.
            for p in n - o:
                if p not in any_paths:
                    assert any(q in any_paths for q in o - n)


# ── the service, with stubbed arm cores ─────────────────────────────────────


class _Session:
    async def execute(self, *_a, **_k):
        class _R:
            def scalar(self):
                return 0

            def fetchall(self):
                return []

            def all(self):
                return []
        return _R()


def _kw(path, match, i):
    return {"id": i, "path": path, "title": path[:-3], "tags": [], "rank": 0.5,
            "match": match, "no_excerpt": False}


def _sem(path, i):
    return {"path": path, "title": path[:-3], "tags": [], "similarity": 0.9 - i * 0.001,
            "chunk": f"chunk {path}", "stale": False, "embedding_truncated": False,
            "section": None, "rerank_text": f"text {path}"}


@pytest.fixture
def arms(monkeypatch):
    state = {"keyword": [], "semantic": [], "snippets": []}

    async def fake_keyword(session, query, *, limit, with_excerpt_flag=False, **_f):
        return search.KeywordArm(rows=[dict(r) for r in state["keyword"]][:limit])

    async def fake_semantic(session, emb, *, overfetch, with_rerank_text=False, **_f):
        rows = [dict(r) for r in state["semantic"]][:overfetch]
        return embeddings.SemanticArm(rows=rows, chunk_rows=len(rows), exact_fallback=False,
                                      fetched_at=None)

    async def fake_snippets(session, rows, arm):
        state["snippets"].append([r["path"] for r in rows])
        for r in rows:
            r.update({"snippet": f"«{r['path']}»", "head": None,
                      "matched_in": ["title"], "matched_terms": None, "has_excerpt": True})

    async def fake_embed(_text, **_kw):
        return [1.0, 0.0, 0.0]

    async def fake_pending(_session, _uid):
        return 0

    monkeypatch.setattr(search, "keyword_arm_rows", fake_keyword)
    monkeypatch.setattr(search, "keyword_snippets", fake_snippets)
    monkeypatch.setattr(embeddings, "semantic_arm_rows", fake_semantic)
    monkeypatch.setattr(embeddings, "get_embedding", fake_embed)
    monkeypatch.setattr(embeddings, "rechunk_pending_count", fake_pending)
    monkeypatch.setattr(settings, "hybrid_rrf_k", 60, raising=False)
    return state


def _run(limit=10, *, incumbent=False, **kw):
    async def go():
        token = hybrid._any_fill_offset_override.set(0) if incumbent else None
        try:
            return await hybrid.hybrid_search(_Session(), "q", limit=limit, **kw)
        finally:
            if token is not None:
                hybrid._any_fill_offset_override.reset(token)
    return asyncio.run(go())


def _codex_window(arms):
    """Codex round 1: one `all` row at keyword rank 1, 49 `any` rows at
    ranks 2–50, 50 semantic notes disjoint from them."""
    arms["keyword"] = [_kw("all.md", "all", 0)] + [
        _kw(f"any{i:02d}.md", "any", i) for i in range(1, 50)]
    arms["semantic"] = [_sem(f"sem{i:02d}.md", i) for i in range(50)]


def test_the_exchange_rule_at_the_page_and_the_rerank_window(arms, monkeypatch):
    monkeypatch.setattr(settings, "keyword_snippet_rows", 0, raising=False)
    _codex_window(arms)
    old = [r.path for r in _run(50, incumbent=True).rows]
    new = [r.path for r in _run(50).rows]
    # Before: keyword and semantic interleave, about 25 + 25.
    assert sum(p.startswith("sem") for p in old) == 25
    assert sum(p.startswith(("any", "all")) for p in old) == 25
    # After: the all-terms row, then semantic ranks 1–49.
    assert new == ["all.md"] + [f"sem{i:02d}.md" for i in range(49)]
    # Every membership change is a fill row exchanged for another union member.
    assert all(p.startswith("any") for p in set(old) - set(new))
    assert all(p.startswith("sem") for p in set(new) - set(old))
    # Non-fill relative order is unchanged in both windows.
    assert [p for p in new if not p.startswith("any") and p in old] == \
        [p for p in old if not p.startswith("any") and p in new]
    # The rerank window is the same first 50 fused rows, and it changed.
    win_old = [r.path for r in _run(10, incumbent=True, rerank_candidates=True).rows]
    win_new = [r.path for r in _run(10, rerank_candidates=True).rows]
    assert win_old == old and win_new == new and win_old != win_new


def test_the_rerank_tool_path_receives_the_new_window(arms, monkeypatch):
    """`hybrid_search(rerank=true)` hands the reranker the first 50 fused
    rows of the new order (#367 D6 after #403)."""
    from src.services import rerank as rerank_service

    monkeypatch.setattr(settings, "keyword_snippet_rows", 0, raising=False)
    _codex_window(arms)
    seen = []

    async def fake_rerank(query, candidates):
        seen.append([t for t, _ in candidates])
        n = len(candidates)
        return rerank_service.RerankOutcome(False, list(range(n)), n, reason="not_configured")

    monkeypatch.setattr(rerank_service, "rerank_candidates", fake_rerank)
    monkeypatch.setattr(tools, "async_session", lambda: _CtxSession())
    tools.current_user_id.set(None)
    asyncio.run(tools._hybrid_search_reranked("q", 10, None, None, None, None))
    assert seen == [["all"] + [f"sem{i:02d}" for i in range(49)]]


class _CtxSession(_Session):
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return None


def test_snippet_assignment_follows_the_new_fused_order(arms, monkeypatch):
    """Codex round 1: KEYWORD_SNIPPET_ROWS=1, A all-rank-1 only, F
    any-rank-50 plus semantic-50."""
    monkeypatch.setattr(settings, "keyword_snippet_rows", 1, raising=False)
    arms["keyword"] = [_kw("A.md", "all", 0)] + [
        _kw(f"pad{i:02d}.md", "any", i) for i in range(1, 49)] + [_kw("F.md", "any", 49)]
    arms["semantic"] = [_sem(f"s{i:02d}.md", i) for i in range(49)] + [_sem("F.md", 49)]
    before = _run(50, incumbent=True)
    after = _run(50)
    assert [r.path for r in before.rows].index("F.md") < [r.path for r in before.rows].index("A.md")
    assert [r.path for r in after.rows].index("A.md") < [r.path for r in after.rows].index("F.md")
    assert arms["snippets"] == [["F.md"], ["A.md"]], "the one snippet follows the shown order"
    a_before = next(r for r in before.rows if r.path == "A.md")
    a_after = next(r for r in after.rows if r.path == "A.md")
    assert (a_before.snippet, a_before.matched_in) == (None, None)
    assert (a_after.snippet, a_after.matched_in) == ("«A.md»", ["title"])
    f_after = next(r for r in after.rows if r.path == "F.md")
    assert f_after.snippet is None
    assert f_after.score == pytest.approx(1 / 160 + 1 / 110)
    # A's rendered line differs only in preview_kind / preview / matched_in.
    line_b = tools._hybrid_row_line(a_before, 60, title="A", path="A.md", tags=[],
                                    preview=tools._hybrid_preview(a_before), labels=[])
    line_a = tools._hybrid_row_line(a_after, 60, title="A", path="A.md", tags=[],
                                    preview=tools._hybrid_preview(a_after), labels=[])
    assert '"preview_kind":"none","preview":""' in line_b and "(match: all)" in line_b
    assert '"preview_kind":"snippet","preview":"«A.md»"' in line_a
    assert "(match: all, matched_in: title)" in line_a
    assert line_b.replace("(match: all)", "") == line_a.replace(
        '"snippet","preview":"«A.md»"', '"none","preview":""').replace(
        "(match: all, matched_in: title)", "")


def test_a_page_without_fill_rows_is_byte_identical(arms, monkeypatch):
    monkeypatch.setattr(settings, "keyword_snippet_rows", 20, raising=False)
    arms["keyword"] = [_kw(f"k{i:02d}.md", "all", i) for i in range(30)]
    arms["semantic"] = [_sem(f"k{i:02d}.md", i) for i in range(0, 30, 3)] + [
        _sem(f"s{i:02d}.md", i) for i in range(20)]
    for limit in (1, 10, 50):
        old, _ = tools.render_hybrid_results("q", _run(limit, incumbent=True))
        new, _ = tools.render_hybrid_results("q", _run(limit))
        assert old == new


def test_the_keyword_only_page_is_unchanged(arms, monkeypatch):
    monkeypatch.setattr(settings, "keyword_snippet_rows", 0, raising=False)

    async def down(_text, **_kw):
        raise RuntimeError("provider down")

    monkeypatch.setattr(embeddings, "get_embedding", down)
    arms["keyword"] = [_kw("a.md", "all", 0), _kw("b.md", "any", 1), _kw("c.md", "any", 2)]
    result = _run(10)
    assert result.arms == "keyword_only"
    assert [r.path for r in result.rows] == ["a.md", "b.md", "c.md"]
    assert [r.keyword_fusion_rank for r in result.rows] == [1, 2, 3]
    text, _ = tools.render_hybrid_results("q", result)
    assert "explain: 1/(60+2), keyword only" in text
    assert "explain: 1/(60+3), keyword only" in text
    old, _ = tools.render_hybrid_results("q", _run(10, incumbent=True))
    assert old == text


def test_a_fill_rows_explain_shows_the_fusion_rank(arms, monkeypatch):
    monkeypatch.setattr(settings, "keyword_snippet_rows", 0, raising=False)
    arms["keyword"] = [_kw(f"pad{i}.md", "any", i) for i in range(4)] + [_kw("N.md", "any", 4)]
    arms["semantic"] = [_sem(f"s{i:02d}.md", i) for i in range(11)] + [_sem("N.md", 11)]
    result = _run(50)
    row = next(r for r in result.rows if r.path == "N.md")
    assert (row.keyword_rank, row.keyword_fusion_rank, row.semantic_rank) == (5, 55, 12)
    text, _ = tools.render_hybrid_results("q", result)
    line = next(ln for ln in text.splitlines() if '"path":"N.md"' in ln)
    assert "keyword_rank: 5 (match: any)" in line
    assert "explain: 1/(60+12) + 1/(60+55)" in line
    assert f"score: {1 / 72 + 1 / 115:.4g}" in line
    assert row.best_rank == 5, "the tie-break key is unoffset"


# ── the harness seam ────────────────────────────────────────────────────────


def test_nothing_under_src_sets_the_override():
    offenders = []
    for path in (REPO / "src").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute) and node.attr in ("set", "reset")
                    and isinstance(node.value, (ast.Name, ast.Attribute))):
                target = node.value.attr if isinstance(node.value, ast.Attribute) else node.value.id
                if target == "_any_fill_offset_override":
                    offenders.append(str(path.relative_to(REPO)))
            if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    name = t.attr if isinstance(t, ast.Attribute) else getattr(t, "id", None)
                    if name == "_any_fill_offset_override" and not (
                            path.name == "hybrid.py" and path.parent.name == "services"):
                        offenders.append(str(path.relative_to(REPO)))
    assert offenders == []


def test_a_concurrent_live_call_cannot_see_the_override():
    seen = {}

    async def alt():
        token = hybrid._any_fill_offset_override.set(0)
        try:
            await asyncio.sleep(0.01)
            seen["alt"] = hybrid.any_fill_offset(True)
        finally:
            hybrid._any_fill_offset_override.reset(token)

    async def live():
        await asyncio.sleep(0.005)
        seen["live"] = hybrid.any_fill_offset(True)

    async def both():
        await asyncio.gather(alt(), live())

    asyncio.run(both())
    assert seen == {"alt": 0, "live": 50}
    assert hybrid.any_fill_offset(False) == 0, "never on the keyword-only path"
