"""The rerank harness arms (#367, design D12; tasks 6.2, 7.8), offline: the
factories' memberships, the probe before the first answer (exit 64), a
deeper candidate promoted at depth 20 exactly as the tool promotes it, and
an unreranked answer failing the run. Replay against PostgreSQL is in
`tests/integration/test_rerank_pg.py`."""
from __future__ import annotations

import asyncio
import json

import pytest

from tests._rerank_support import fake_server  # noqa: F401
from scripts.search_eval import EXIT_FAILURE, EXIT_OK, EXIT_USAGE, UsageError
from scripts.search_eval.__main__ import main as cli_main
from scripts.search_eval.arms import ArmContext, EvalQuery, resolve_arm
from scripts.search_eval.corpus import Corpus
from scripts.search_eval_arms import rerank as rerank_arm
import src.mcp_server.tools as tools
from src.config import settings
from src.services import embeddings, hybrid as hybrid_service, rerank
from tests.test_search_eval import ALL, H, manifest_of, run, write_data
from tests.test_semantic_rerank import _Session, row

SEM = "scripts.search_eval_arms.rerank:make_semantic_arm"
HYB = "scripts.search_eval_arms.rerank:make_hybrid_arm"
KW_ONLY = {"metadata": True, "keyword": True, "semantic": False}


@pytest.fixture
def reranker(fake_server, monkeypatch):
    monkeypatch.setattr(settings, "rerank_url", fake_server.url, raising=False)
    monkeypatch.setattr(settings, "rerank_timeout_ms", 1500, raising=False)
    monkeypatch.setattr(rerank, "_client", None)
    rerank._reset_state_for_tests()

    async def _embed(_t, **_kw):
        return [1.0, 0.0, 0.0]

    monkeypatch.setattr(embeddings, "get_embedding", _embed)
    yield fake_server
    rerank._reset_state_for_tests()


def _stub_replay_db(monkeypatch, rows, membership):
    import scripts.search_eval.db as db

    class _Engine:
        async def dispose(self):
            pass

    async def fake_open(url):
        return _Engine(), lambda: _Session(rows)

    async def fake_membership(session):
        return membership

    async def fake_chunk_lengths(_session, _owner, _m, _w):
        return None  # #390: the stub copy has no chunk table to measure

    monkeypatch.setattr(db, "open_readonly", fake_open)
    monkeypatch.setattr(db, "fetch_membership", fake_membership)
    monkeypatch.setattr(db, "chunk_lengths", fake_chunk_lengths)
    monkeypatch.setenv("SEARCH_EVAL_DATABASE_URL", "postgresql+asyncpg://u:p@127.0.0.1:1/x")


def _query(text="q", user_id=None):
    return EvalQuery(qid="q1", user_id=user_id, text=text, folder=None, tags=None,
                     frontmatter=None, query_class="prose", sources=frozenset({"mined"}))


def test_the_factories_resolve_with_their_memberships():
    sem, hyb = resolve_arm(SEM, **H), resolve_arm(HYB, **H)
    assert (sem.membership, sem.requires_db) == ("semantic", True)
    assert (hyb.membership, hyb.requires_db) == ("keyword_or_semantic", True)
    assert rerank_arm.make_semantic_arm.preflight is rerank_arm.preflight


def test_without_a_reranker_the_arm_is_a_usage_error(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "rerank_url", "", raising=False)
    monkeypatch.setenv("SEARCH_EVAL_DATABASE_URL", "postgresql+asyncpg://u:p@127.0.0.1:1/x")
    d = tmp_path / "d"
    write_data(d, [{"text": "q", "relevant": ["A.md"]}], {"1": {"A.md": dict(ALL)}})
    with pytest.raises(UsageError, match="RERANK_URL"):
        run(d, [SEM])
    assert not (d / "runs").exists()


@pytest.mark.parametrize("mode", ["inverted", "position", "tie"])
def test_a_failing_probe_exits_64_before_any_answer(tmp_path, monkeypatch, reranker, mode):
    reranker.mode = mode
    d = tmp_path / "d"
    write_data(d, [{"text": "q", "relevant": ["A.md"]}], {"1": {"A.md": dict(ALL)}})
    monkeypatch.setenv("SEARCH_EVAL_DATABASE_URL", "postgresql+asyncpg://u:p@127.0.0.1:1/x")
    with pytest.raises(UsageError, match="probe"):
        run(d, [SEM])
    assert not (d / "runs").exists(), "refused before the generation is reserved"
    assert cli_main(["run", "--data", str(d), "--arm", SEM]) == EXIT_USAGE
    assert all(len(r["documents"]) == 2 for r in reranker.requests), "probe requests only"


def test_a_deeper_note_is_promoted_at_depth_20_as_the_tool_promotes_it(monkeypatch, reranker):
    rows = [row(i) for i in range(30)]
    reranker.scorer = lambda q, docs: [10.0 if d.startswith("n23") else 1.0 for d in docs]

    async def go():
        await rerank_arm.preflight({})
        arm = rerank_arm.make_semantic_arm(ArmContext(lambda: _Session(rows), Corpus({})))
        answer = await arm(_query(), 20)
        monkeypatch.setattr(tools, "async_session", lambda: _Session(rows))

        async def _noop(*_a, **_k):
            return True

        monkeypatch.setattr(tools, "_log_usage", _noop)
        text = await tools.semantic_search_impl("q", limit=20, rerank=True)
        await rerank.close_rerank_client()
        return answer, text

    answer, text = asyncio.run(go())
    assert answer[0] == "Notes/n23.md" and len(answer) == 20
    rendered = [json.JSONDecoder().raw_decode(line[2:])[0]["path"]
                for line in text.splitlines() if line.startswith("- {")]
    assert answer == rendered
    # The arm scored the same up-to-50 candidates as production (30 here).
    assert len(reranker.requests[-1]["documents"]) == 30


def test_an_ab_against_the_incumbent_and_a_timeout_fails_the_run(tmp_path, monkeypatch,
                                                                 reranker):
    rows = [row(1, "A.md"), row(2, "B.md")]
    _stub_replay_db(monkeypatch, rows, {"1": {"A.md": dict(ALL), "B.md": dict(ALL)}})
    d = tmp_path / "d"
    write_data(d, [{"text": "q", "relevant": ["A.md"]}],
               {"1": {"A.md": dict(ALL), "B.md": dict(ALL)}})
    out = run(d, ["semantic", SEM])
    assert out.exit_code == EXIT_OK, out.manifest
    # Now the reranker times out mid-run: the generation fails, nothing scored.
    monkeypatch.setattr(settings, "rerank_timeout_ms", 200, raising=False)

    real = rerank.score

    async def slow_after_probe(query, documents, *, deadline_s):
        if query != rerank.PROBE_QUERY:
            reranker.mode = "sleep:2000"
        return await real(query, documents, deadline_s=deadline_s)

    monkeypatch.setattr(rerank, "score", slow_after_probe)
    out = run(d, [SEM], generation="G2")
    assert out.exit_code == EXIT_FAILURE
    m = manifest_of(d)
    assert m["status"] == "failed" and m["error"].startswith("UnrerankedAnswer")
    assert "files" not in m


def test_the_hybrid_rerank_arm_keeps_union_membership(tmp_path, monkeypatch, reranker):
    async def fake_hybrid(session, query, **kw):
        assert kw["rerank_candidates"] is True and kw["limit"] == 10
        rows = [hybrid_service.HybridRow(path=p, title=p, tags=[], score=0.1, keyword_rank=1,
                                         rerank_text=f"text {p}") for p in ("A.md", "B.md")]
        return hybrid_service.HybridResult(rows=rows)

    monkeypatch.setattr(hybrid_service, "hybrid_search", fake_hybrid)
    # The judged note is keyword-only in the snapshot, and the replay copy
    # has lost its keyword constituent: corpus loss for the union arm.
    _stub_replay_db(monkeypatch, [], {"1": {"A.md": {"metadata": True, "keyword": False,
                                                     "semantic": False},
                                            "B.md": dict(ALL)}})
    d = tmp_path / "d"
    write_data(d, [{"text": "q", "relevant": ["A.md"]}],
               {"1": {"A.md": KW_ONLY, "B.md": dict(ALL)}})
    out = run(d, [HYB], depth=10)
    assert out.exit_code == EXIT_OK, out.manifest
    meta = json.loads((d / "runs" / out.manifest["files"][HYB]["path"]).read_text())["meta"]
    assert meta["membership"] == "keyword_or_semantic"
    assert list(meta["corpus_loss"].values()) == [["A.md"]]


def test_a_keyword_only_hybrid_answer_fails_the_run(tmp_path, monkeypatch, reranker):
    async def fake_hybrid(session, query, **kw):
        return hybrid_service.HybridResult(rows=[], arms="keyword_only",
                                           semantic_unavailable="provider_error")

    monkeypatch.setattr(hybrid_service, "hybrid_search", fake_hybrid)
    _stub_replay_db(monkeypatch, [], {"1": {"A.md": dict(ALL)}})
    d = tmp_path / "d"
    write_data(d, [{"text": "q", "relevant": ["A.md"]}],
               {"1": {"A.md": dict(ALL)}})
    out = run(d, [HYB])
    assert out.exit_code == EXIT_FAILURE
    assert manifest_of(d)["error"].startswith("UnrerankedAnswer")


def test_no_database_url_means_no_probe(tmp_path, monkeypatch, reranker):
    """Codex round 1 (C2): a reachable reranker and no replay URL exits 64
    before any connection is made, the probe's included."""
    monkeypatch.delenv("SEARCH_EVAL_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    d = tmp_path / "d"
    write_data(d, [{"text": "q", "relevant": ["A.md"]}], {"1": {"A.md": dict(ALL)}})
    with pytest.raises(UsageError):
        run(d, [SEM])
    assert cli_main(["run", "--data", str(d), "--arm", SEM]) == EXIT_USAGE
    assert reranker.requests == [], "no probe request without a replay URL"
    assert not (d / "runs").exists()


# ── no_candidates is an empty answer (Codex round 2; owner triage) ─────────


def _assert_one_empty(d, out, spec):
    from scripts.search_eval.scoring import score

    assert out.exit_code == EXIT_OK, out.manifest
    assert out.manifest["empty_candidates"] == {spec: 1}
    staged = json.loads((d / "runs" / out.manifest["files"][spec]["path"]).read_text())
    assert staged["meta"]["empty_candidates"] == 1
    assert list(staged["runs"].values()) == [[]]
    res = score(d, [spec], min_queries=1)
    assert res.report["arms"][0]["metrics"]["overall"]["recall@10"] == 0.0


def test_a_zero_candidate_query_is_an_empty_answer_for_the_semantic_arm(tmp_path, monkeypatch,
                                                                       reranker):
    _stub_replay_db(monkeypatch, [], {"1": {"A.md": dict(ALL)}})
    d = tmp_path / "d"
    write_data(d, [{"text": "q", "relevant": ["A.md"]}], {"1": {"A.md": dict(ALL)}})
    _assert_one_empty(d, run(d, [SEM]), SEM)
    assert all(r["query"] == rerank.PROBE_QUERY for r in reranker.requests), "no rerank request"


def test_a_zero_candidate_query_is_an_empty_answer_for_the_hybrid_arm(tmp_path, monkeypatch,
                                                                     reranker):
    async def fake_hybrid(session, query, **kw):
        return hybrid_service.HybridResult(rows=[])

    monkeypatch.setattr(hybrid_service, "hybrid_search", fake_hybrid)
    _stub_replay_db(monkeypatch, [], {"1": {"A.md": dict(ALL)}})
    d = tmp_path / "d"
    write_data(d, [{"text": "q", "relevant": ["A.md"]}], {"1": {"A.md": dict(ALL)}})
    _assert_one_empty(d, run(d, [HYB]), HYB)


@pytest.mark.parametrize("spec", [SEM, HYB])
def test_any_other_unreranked_reason_still_fails_the_run(tmp_path, monkeypatch, reranker, spec):
    async def fake_hybrid(session, query, **kw):
        rows = [hybrid_service.HybridRow(path="A.md", title="A", tags=[], score=0.1,
                                         keyword_rank=1, rerank_text="text")]
        return hybrid_service.HybridResult(rows=rows)

    monkeypatch.setattr(hybrid_service, "hybrid_search", fake_hybrid)
    _stub_replay_db(monkeypatch, [row(1, "A.md")], {"1": {"A.md": dict(ALL)}})
    real = rerank.score

    async def failing_after_probe(query, documents, *, deadline_s):
        if query != rerank.PROBE_QUERY:
            reranker.mode = "status:500"
        return await real(query, documents, deadline_s=deadline_s)

    monkeypatch.setattr(rerank, "score", failing_after_probe)
    d = tmp_path / "d"
    write_data(d, [{"text": "q", "relevant": ["A.md"]}], {"1": {"A.md": dict(ALL)}})
    out = run(d, [spec])
    assert out.exit_code == EXIT_FAILURE
    m = manifest_of(d)
    assert m["status"] == "failed" and m["error"].startswith("UnrerankedAnswer")
