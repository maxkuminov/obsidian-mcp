"""The search-evaluation side of `hybrid_search` (#354): the
`keyword_or_semantic` membership and its per-constituent corpus loss, the
replayable hybrid arms, and the miner treating `hybrid_search` rows as search
rows. Offline: the DB-backed arm is driven against stub sessions and a stub
service; the integration suite replays it against PostgreSQL."""
from __future__ import annotations

import asyncio
import json

import pytest

from scripts.search_eval import EXIT_FAILURE, EXIT_OK, EXIT_USAGE, UsageError
from scripts.search_eval.__main__ import main as cli_main
from scripts.search_eval.arms import ArmContext, EvalQuery, resolve_arm
from scripts.search_eval.corpus import MEMBERSHIPS, Corpus, lost
from scripts.search_eval.mining import SEARCH_TOOLS, mine
from scripts.search_eval.scoring import score
from scripts.search_eval_arms import hybrid as hybrid_arm
from src.config import settings
from src.services import hybrid as hybrid_service
from tests.test_search_eval import (  # noqa: F401 — `armmod` is a fixture
    ALL,
    H,
    armmod,
    judgments,
    manifest_of,
    rd,
    run,
    srch,
    write_data,
)

KW_ONLY = {"metadata": True, "keyword": True, "semantic": False}
SEM_ONLY = {"metadata": True, "keyword": False, "semantic": True}
NEITHER = {"metadata": True, "keyword": False, "semantic": False}

UNION_ARMS = '''

union_m = _arm(lambda q, d, c: ["A.md"], "keyword_or_semantic")
'''


@pytest.fixture
def unionmod(armmod, tmp_path):
    path = tmp_path / f"{armmod}.py"
    path.write_text(path.read_text() + UNION_ARMS)
    return armmod


# ── the membership ──────────────────────────────────────────────────────────


def test_the_union_membership_is_declared_and_the_and_membership_is_unchanged():
    assert "keyword_or_semantic" in MEMBERSHIPS
    c = Corpus({"1": {"K.md": KW_ONLY, "S.md": SEM_ONLY, "B.md": ALL, "N.md": NEITHER}})
    assert [c.member(1, p, "keyword_or_semantic") for p in ("K.md", "S.md", "B.md", "N.md")] == [
        True, True, True, False]
    assert [c.member(1, p, "keyword_and_semantic") for p in ("K.md", "S.md", "B.md", "N.md")] == [
        False, False, True, False]


def test_loss_is_per_constituent_for_the_union_and_whole_for_the_others():
    snap = Corpus({"1": {"B.md": ALL}})
    only_kw = Corpus({"1": {"B.md": KW_ONLY}})
    assert lost(snap, only_kw, 1, "B.md", "keyword_or_semantic")
    assert not lost(snap, only_kw, 1, "B.md", "keyword")
    assert lost(snap, only_kw, 1, "B.md", "keyword_and_semantic")
    assert not lost(snap, snap, 1, "B.md", "keyword_or_semantic")
    # A constituent absent in the snapshot cannot be lost.
    assert not lost(Corpus({"1": {"K.md": KW_ONLY}}), Corpus({"1": {"K.md": KW_ONLY}}),
                    1, "K.md", "keyword_or_semantic")


def _union_run(tmp_path, armmod, snapshot, replay, relevant):
    d = tmp_path / "d"
    write_data(d, [{"text": "q", "relevant": relevant}], snapshot)
    arms = [f"{armmod}:union_m", f"{armmod}:keyword_m", f"{armmod}:hybrid_m"]
    assert run(d, arms, replay_corpus=Corpus(replay)).exit_code == EXIT_OK
    res = score(d, min_queries=1)
    return res, {a["arm"].split(":")[1]: a for a in res.report["arms"]}


def test_a_keyword_only_note_reachable_by_the_hybrid_is_the_hybrid_loss(tmp_path, unionmod):
    snap = {"1": {"A.md": dict(ALL), "C.md": dict(KW_ONLY)}}
    replay = {"1": {"A.md": dict(ALL), "C.md": dict(NEITHER)}}
    res, by = _union_run(tmp_path, unionmod, snap, replay, ["A.md", "C.md"])
    assert by["union_m"]["corpus_loss"] == 1 and by["union_m"]["unsearchable_before"] == 0
    assert by["keyword_m"]["corpus_loss"] == 1
    # The AND arm could not reach C.md before: unsearchable, never its loss.
    assert by["hybrid_m"]["unsearchable_before"] == 1 and by["hybrid_m"]["corpus_loss"] == 0
    assert res.report["verdict"] == "CORPUS_LOSS" and res.exit_code == 2


def test_a_semantic_only_note_reachable_by_the_hybrid_is_the_hybrid_loss(tmp_path, unionmod):
    snap = {"1": {"A.md": dict(ALL), "D.md": dict(SEM_ONLY)}}
    replay = {"1": {"A.md": dict(ALL), "D.md": dict(NEITHER)}}
    res, by = _union_run(tmp_path, unionmod, snap, replay, ["A.md", "D.md"])
    assert by["union_m"]["corpus_loss"] == 1
    assert by["keyword_m"]["corpus_loss"] == 0 and by["keyword_m"]["unsearchable_before"] == 1
    assert res.report["verdict"] == "CORPUS_LOSS" and res.exit_code == 2


def test_losing_one_constituent_of_a_two_arm_note_is_still_union_loss(tmp_path, unionmod):
    snap = {"1": {"A.md": dict(ALL), "B.md": dict(ALL)}}
    replay = {"1": {"A.md": dict(ALL), "B.md": dict(KW_ONLY)}}
    res, by = _union_run(tmp_path, unionmod, snap, replay, ["A.md", "B.md"])
    assert by["union_m"]["corpus_loss"] == 1, "the semantic constituent no longer sees B.md"
    assert by["keyword_m"]["corpus_loss"] == 0
    assert by["hybrid_m"]["corpus_loss"] == 1
    assert res.exit_code == 2


def test_a_path_neither_arm_could_reach_is_not_union_loss(tmp_path, unionmod):
    snap = {"1": {"A.md": dict(ALL), "E.md": dict(NEITHER)}}
    replay = {"1": {"A.md": dict(ALL)}}
    res, by = _union_run(tmp_path, unionmod, snap, replay, ["A.md", "E.md"])
    assert by["union_m"]["unsearchable_before"] == 1 and by["union_m"]["corpus_loss"] == 0
    assert res.exit_code == EXIT_OK


# ── the hybrid arms ─────────────────────────────────────────────────────────


def test_both_factories_resolve_as_union_db_arms():
    for name in ("make_arm", "make_arm_alt"):
        arm = resolve_arm(f"scripts.search_eval_arms.hybrid:{name}", **H)
        assert (arm.kind, arm.membership, arm.requires_db) == (
            "import", "keyword_or_semantic", True)


def test_make_arm_alt_without_its_option_exits_64(tmp_path, monkeypatch):
    monkeypatch.setenv("SEARCH_EVAL_DATABASE_URL", "postgresql+asyncpg://u:p@127.0.0.1:1/x")
    d = tmp_path / "d"
    write_data(d, [{"text": "q", "relevant": ["A.md"]}], {"1": {"A.md": dict(ALL)}})
    spec = "scripts.search_eval_arms.hybrid:make_arm_alt"
    with pytest.raises(UsageError, match="hybrid_alt_k"):
        run(d, [spec])
    assert not (d / "runs").exists(), "refused before the generation is reserved"
    assert cli_main(["run", "--data", str(d), "--arm", spec]) == EXIT_USAGE
    # The factory refuses on its own, too.
    with pytest.raises(UsageError):
        hybrid_arm.make_arm_alt(ArmContext(session_factory=None, corpus=Corpus({})))


@pytest.mark.parametrize("bad", ["0", "1001", "sixty"])
def test_an_invalid_k_option_is_a_usage_error(bad):
    with pytest.raises(UsageError):
        hybrid_arm.make_arm(ArmContext(session_factory=None, corpus=Corpus({}),
                                       options={"hybrid_k": bad}))


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _query(text="deploy runbook"):
    return EvalQuery(qid="q1", user_id=7, text=text, folder="Ops/", tags=["t"],
                     frontmatter={"a": 1}, query_class="prose", sources=frozenset({"mined"}))


def _stub_service(monkeypatch, arms="both"):
    calls = []

    async def fake(session, query, **kw):
        calls.append((query, kw))
        rows = [hybrid_service.HybridRow(path=p, title=p, tags=[], score=0.1)
                for p in ("A.md", "B.md")]
        return hybrid_service.HybridResult(
            rows=rows, arms=arms,
            semantic_unavailable=None if arms == "both" else "provider_error")

    monkeypatch.setattr(hybrid_service, "hybrid_search", fake)
    return calls


def test_the_arm_passes_text_owner_filters_depth_and_k(monkeypatch):
    calls = _stub_service(monkeypatch)
    ctx = ArmContext(session_factory=_Session, corpus=Corpus({}), options={"hybrid_k": "20"})
    out = asyncio.run(hybrid_arm.make_arm(ctx)(_query(), 80))
    assert out == ["A.md", "B.md"]
    (query, kw), = calls
    assert query == "deploy runbook"
    assert kw == dict(limit=50, folder="Ops/", tags=["t"], frontmatter={"a": 1},
                      user_id=7, rrf_k=20)
    # make_arm defaults to the setting; make_arm_alt reads its own key.
    calls.clear()
    asyncio.run(hybrid_arm.make_arm(ArmContext(_Session, Corpus({})))(_query(), 20))
    assert calls[0][1]["rrf_k"] == settings.hybrid_rrf_k and calls[0][1]["limit"] == 20
    calls.clear()
    alt = hybrid_arm.make_arm_alt(ArmContext(_Session, Corpus({}), {"hybrid_alt_k": "7"}))
    asyncio.run(alt(_query(), 20))
    assert calls[0][1]["rrf_k"] == 7


def _stub_replay_db(monkeypatch):
    import scripts.search_eval.db as db

    class _Engine:
        async def dispose(self):
            pass

    async def fake_open(url):
        return _Engine(), _Session

    async def fake_membership(session):
        return {"1": {"A.md": dict(ALL), "B.md": dict(ALL)}}

    monkeypatch.setattr(db, "open_readonly", fake_open)
    monkeypatch.setattr(db, "fetch_membership", fake_membership)
    monkeypatch.setenv("SEARCH_EVAL_DATABASE_URL", "postgresql+asyncpg://u:p@127.0.0.1:1/x")


def test_an_ab_of_k_in_one_report(tmp_path, monkeypatch):
    """Both factories in one generation, `hybrid_k=60` and `hybrid_alt_k=20`:
    one report scores both arms on the same qrels, snapshot and selection, and
    each arm ran with its own k."""
    calls = _stub_service(monkeypatch)
    _stub_replay_db(monkeypatch)
    d = tmp_path / "d"
    write_data(d, [{"text": "q", "relevant": ["A.md"]}],
               {"1": {"A.md": dict(ALL), "B.md": dict(ALL)}})
    arms = ["scripts.search_eval_arms.hybrid:make_arm",
            "scripts.search_eval_arms.hybrid:make_arm_alt"]
    out = run(d, arms, arm_options={"hybrid_k": "60", "hybrid_alt_k": "20"})
    assert out.exit_code == EXIT_OK, out.manifest
    assert sorted(kw["rrf_k"] for _, kw in calls) == [20, 60]
    metas = [json.loads((d / "runs" / out.manifest["files"][a]["path"]).read_text())["meta"]
             for a in arms]
    for key in ("qrels_sha256", "snapshot_sha256", "selected_qids_hash", "generation"):
        assert metas[0][key] == metas[1][key] == out.manifest.get(key, metas[0][key]), key
    assert all(m["membership"] == "keyword_or_semantic" for m in metas)
    res = score(d, arms, baseline=arms[0], min_queries=1)
    assert [a["arm"] for a in res.report["arms"]] == arms


def test_a_keyword_only_answer_fails_the_run(tmp_path, monkeypatch):
    """The provider failed during replay: the arm raises, the generation is
    `failed` (exit 1), and nothing is scored."""
    _stub_service(monkeypatch, arms="keyword_only")
    import scripts.search_eval.db as db

    class _Engine:
        async def dispose(self):
            pass

    async def fake_open(url):
        return _Engine(), _Session

    async def fake_membership(session):
        return {"1": {"A.md": dict(ALL)}}

    monkeypatch.setattr(db, "open_readonly", fake_open)
    monkeypatch.setattr(db, "fetch_membership", fake_membership)
    monkeypatch.setenv("SEARCH_EVAL_DATABASE_URL", "postgresql+asyncpg://u:p@127.0.0.1:1/x")
    d = tmp_path / "d"
    write_data(d, [{"text": "q", "relevant": ["A.md"]}], {"1": {"A.md": dict(ALL)}})
    out = run(d, ["scripts.search_eval_arms.hybrid:make_arm"])
    assert out.exit_code == EXIT_FAILURE
    m = manifest_of(d)
    assert m["status"] == "failed"
    assert m["error"].startswith("KeywordOnlyAnswer")
    assert "files" not in m


# ── the miner ───────────────────────────────────────────────────────────────


def test_hybrid_search_is_a_search_tool():
    assert SEARCH_TOOLS == ("keyword_search", "semantic_search", "hybrid_search")


def test_a_hybrid_row_credits_and_folds_with_a_keyword_row():
    res = mine([srch(0, "deploy runbook", ["Ops/Runbook.md"], tool="hybrid_search"),
                rd(30, "Ops/Runbook.md")])
    assert judgments(res) == {("deploy runbook", 1): ["Ops/Runbook.md"]}
    res = mine([srch(0, "deploy  runbook", ["X.md"]),
                srch(10, "deploy runbook", ["Ops/Runbook.md"], tool="hybrid_search"),
                rd(30, "Ops/Runbook.md"), rd(31, "X.md")])
    (q,) = res.queries
    assert q["source_tools"] == ["hybrid_search", "keyword_search"]
    assert sorted(res.qrels[q["qid"]]) == ["Ops/Runbook.md", "X.md"]


def test_a_keyword_only_hybrid_row_is_usable():
    res = mine([srch(0, "q", ["A.md"], tool="hybrid_search", arms="keyword_only",
                     semantic_unavailable="provider_error"),
                rd(5, "A.md")])
    assert judgments(res) == {("q", 1): ["A.md"]}


def test_the_export_sql_names_hybrid_search():
    from tests.test_search_eval import EXPORT_SQL

    assert "'hybrid_search'" in EXPORT_SQL.read_text()


def test_the_report_vocabulary_admits_the_new_membership():
    from scripts.search_eval import scoring

    assert "keyword_or_semantic" in scoring._VOCABULARY
