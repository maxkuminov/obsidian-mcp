"""The harness half of #389 (design D7, D8; tasks 4.1–4.4), offline.

recall@20 / recall@50, the default depth 50, the per-arm `staged_depth` with
nulling of every recall@k beyond it (a shallow `runfile:` imported into a
deep generation, end to end), the invalid-depth refusals, and the
`confidence` calibration tabulation: `confidence-rows.jsonl`, the per-tool
PASS / FAIL / INSUFFICIENT_DATA lines, pre-change exclusion, keyword-only
false lows, provenance and leaks.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.search_eval import EXIT_OK, EXIT_USAGE, UsageError
from scripts.search_eval.__main__ import build_parser, main as cli_main
from scripts.search_eval._io import pretty_json, read_jsonl, sha256_bytes, sha256_file
from scripts.search_eval.classify import classify
from scripts.search_eval.commands import write_mine
from scripts.search_eval.confidence import (
    NOT_ATTACHABLE_LINE,
    acceptance_line,
    run_confidence,
    tabulate,
    would_be_low,
)
from scripts.search_eval.metrics import recall_at
from scripts.search_eval.mining import mine
from scripts.search_eval.runner import DEFAULT_DEPTH
from scripts.search_eval.scoring import METRICS, assert_no_leak, score
from tests.test_search_eval import (  # noqa: F401
    FIXTURE, SNAP, _copy_inputs, armmod, manifest_of, rd, run, srch, write_data,
)

DAY = 86_400


# ── 4.1 / 4.3 metrics ───────────────────────────────────────────────────────


def test_recall_at_depth_edge_cases():
    run_ = [f"x{i}.md" for i in range(29)] + ["a.md"]
    assert recall_at({"a.md", "b.md"}, run_, 20) == 0.0
    assert recall_at({"a.md", "b.md"}, run_, 50) == 0.5
    assert METRICS == ("recall@5", "recall@10", "recall@20", "recall@50", "mrr", "ndcg@10")


def test_the_default_depth_is_50():
    assert DEFAULT_DEPTH == 50
    args = build_parser().parse_args(["run", "--data", "d", "--arm", "keyword"])
    assert args.depth == 50


DEEP_ARM = '''

def _deep(q, d, c):
    return ["N%d.md" % i for i in range(29)] + ["A.md"]

deep = _arm(_deep)
'''


@pytest.fixture
def deepmod(armmod, tmp_path):
    path = tmp_path / f"{armmod}.py"
    path.write_text(path.read_text() + DEEP_ARM)
    return armmod


def test_a_shallow_runfile_is_not_scored_at_depth_end_to_end(tmp_path, deepmod):
    other = tmp_path / "other"
    write_data(other, [{"text": f"q{i}", "relevant": ["A.md"]} for i in range(4)], SNAP)
    assert run(other, [f"{deepmod}:fixed_a"], generation="R20", depth=20).exit_code == EXIT_OK
    staged = other / "runs" / manifest_of(other)["files"][f"{deepmod}:fixed_a"]["path"]
    assert json.loads(staged.read_text())["meta"]["staged_depth"] == 20
    here = tmp_path / "here"
    _copy_inputs(other, here)
    spec = f"runfile:{staged}"
    assert run(here, [spec, f"{deepmod}:deep"], generation="H50").exit_code == EXIT_OK
    metas = {}
    for arm, entry in manifest_of(here)["files"].items():
        metas[arm] = json.loads((here / "runs" / entry["path"]).read_text())["meta"]
    assert metas[spec]["staged_depth"] == 20 and metas[spec]["depth"] == 50
    assert metas[f"{deepmod}:deep"]["staged_depth"] == 50
    res = score(here, [spec, f"{deepmod}:deep"], baseline=spec, min_queries=1, seed=3)
    base, cand = res.report["arms"]
    assert (base["staged_depth"], cand["staged_depth"]) == (20, 50)
    assert base["metrics"]["overall"]["recall@50"] is None
    assert base["metrics"]["overall"]["recall@20"] == 1.0
    assert base["metrics"]["overall"]["recall@10"] == 1.0
    assert cand["metrics"]["overall"]["recall@20"] == 0.0
    assert cand["metrics"]["overall"]["recall@50"] == 1.0
    delta = res.report["deltas"][cand["arm"]]["overall"]
    assert delta["recall@50"] == {"n": 4, "mean": None, "ci95": None, "deltas": None}
    assert delta["recall@20"]["mean"] == -1.0 and delta["recall@20"]["ci95"] == [-1.0, -1.0]
    assert "R@50" in res.table and "staged_depth 20" in res.table
    again = score(here, [spec, f"{deepmod}:deep"], baseline=spec, min_queries=1, seed=3)
    assert again.report["deltas"] == res.report["deltas"], "seeded CI is reproducible"
    # #403: schema 3, the deltas baseline, and the runfile arm's source
    # (never scored here, so no source report digest).
    assert res.report["schema_version"] == 3
    assert res.report["deltas_baseline"] == res.report["baseline"] == base["arm"]
    assert base["source_generation"] == "R20" and base["source_report_sha256"] is None
    assert "source_report_sha256" not in cand


def test_a_scored_source_generation_s_report_digest_rides_the_runfile_arm(tmp_path, deepmod):
    """#403 (Codex implementation round 2): when the source data directory's
    `report.json` scores the runfile's generation, `run` records its sha256
    and `score` carries it into the arm entry `gate` checks."""
    import hashlib

    other = tmp_path / "other"
    write_data(other, [{"text": f"q{i}", "relevant": ["A.md"]} for i in range(4)], SNAP)
    assert run(other, [f"{deepmod}:fixed_a"], generation="R20", depth=20).exit_code == EXIT_OK
    score(other, [f"{deepmod}:fixed_a"], min_queries=1)
    digest = hashlib.sha256((other / "report.json").read_bytes()).hexdigest()
    staged = other / "runs" / manifest_of(other)["files"][f"{deepmod}:fixed_a"]["path"]
    here = tmp_path / "here"
    _copy_inputs(other, here)
    spec = f"runfile:{staged}"
    assert run(here, [spec, f"{deepmod}:deep"], generation="H50").exit_code == EXIT_OK
    res = score(here, [spec, f"{deepmod}:deep"], baseline=spec, min_queries=1)
    base = res.report["arms"][0]
    assert (base["source_generation"], base["source_report_sha256"]) == ("R20", digest)


def _forge_depth(staged: Path, other: Path, arm: str, value, *, drop=False):
    content = json.loads(staged.read_text())
    if drop:
        del content["meta"]["depth"]
    else:
        content["meta"]["depth"] = value
    staged.write_text(pretty_json(content))
    m = manifest_of(other)
    m["files"][arm]["sha256"] = sha256_file(staged)
    (other / "runs" / "manifest.json").write_text(pretty_json(m))


@pytest.mark.parametrize("value, drop", [(None, True), (0, False), ("20", False),
                                         (True, False), (-5, False), (20.0, False)])
def test_a_runfile_without_a_usable_depth_exits_64_and_publishes_nothing(
        tmp_path, armmod, value, drop):
    other = tmp_path / "other"
    write_data(other, [{"text": "q1", "relevant": ["A.md"]}], SNAP)
    assert run(other, [f"{armmod}:fixed_a"], generation="R1").exit_code == EXIT_OK
    staged = other / "runs" / manifest_of(other)["files"][f"{armmod}:fixed_a"]["path"]
    _forge_depth(staged, other, f"{armmod}:fixed_a", value, drop=drop)
    here = tmp_path / "here"
    _copy_inputs(other, here)
    with pytest.raises(UsageError, match="depth"):
        run(here, [f"runfile:{staged}"], generation="H1")
    assert not (here / "runs").exists(), "nothing reserved, nothing published"
    code = cli_main(["run", "--data", str(here), "--arm", f"runfile:{staged}",
                     "--generation", "H2"])
    assert code == EXIT_USAGE


def test_the_depth_report_holds_no_text_or_path(tmp_path, deepmod):
    d = tmp_path / "d"
    write_data(d, [{"text": "secret query text", "relevant": ["Secret/Path.md"]}],
               {"1": {"Secret/Path.md": SNAP["1"]["A.md"]}})
    assert run(d, [f"{deepmod}:deep"]).exit_code == EXIT_OK
    res = score(d, min_queries=1)
    serialized = pretty_json(res.report)
    assert_no_leak(serialized, {"secret query text"}, {"Secret/Path.md"})
    assert "Secret" not in serialized


# ── 4.4 calibration rows ────────────────────────────────────────────────────


def _hy(t, q, paths, who="K1", **params):
    return srch(t, q, paths, who=who, tool="hybrid_search", **params)


def _se(t, q, paths, who="K1", **params):
    return srch(t, q, paths, who=who, tool="semantic_search", **params)


def test_mine_writes_one_confidence_row_per_semantic_or_hybrid_search():
    rows = [
        _hy(0, "xqzvplorth", ["A.md"], confidence="ok", top_similarity=0.467, keyword_all=0),
        rd(5, "A.md"),
        _se(100, "how do I", ["B.md"], confidence="low", top_similarity=0.41),
        srch(200, "deploy", ["C.md"]),                       # keyword_search: no row
        _se(300, "old row", ["D.md"]),                       # pre-change: no key
        _hy(400, "refused", ["E.md"], error="rate_limited"),  # refusal: not usable
    ]
    result = mine(rows)
    out = result.confidence_rows
    assert [r["tool"] for r in out] == ["hybrid_search", "semantic_search", "semantic_search"]
    hy, se, old = out
    assert hy == {"tool": "hybrid_search", "class": classify("xqzvplorth"), "created_at": 0,
                  "top_similarity": 0.467, "keyword_all": 0, "clicked": True,
                  "confidence": "ok"}
    assert se["confidence"] == "low" and se["clicked"] is False and se["keyword_all"] is None
    assert se["created_at"] == 100 * 1_000_000
    assert "confidence" not in old and old["created_at"] == 300 * 1_000_000
    for r in out:
        assert set(r) <= {"tool", "class", "confidence", "created_at", "top_similarity",
                          "keyword_all", "clicked"}
    assert result.report["confidence_rows"] == 3


def test_the_export_sql_carries_the_confidence_keys():
    sql = (Path(__file__).resolve().parent.parent / "scripts" / "search_eval_export.sql").read_text()
    for key in ("'confidence'", "'top_similarity'", "'keyword_all'"):
        assert key in sql


# ── 4.4 the tabulation ──────────────────────────────────────────────────────


def _row(tool, day, *, clicked, top, kall=None, conf="ok", cls="prose"):
    r = {"tool": tool, "class": cls, "created_at": int(day * DAY * 1_000_000),
         "top_similarity": top, "keyword_all": kall if tool == "hybrid_search" else None,
         "clicked": clicked}
    if conf is not None:
        r["confidence"] = conf
    return r


SINGLE = {"provenance": {"users": 1, "basis": "user_filter"}}
MULTI = {"provenance": {"users": "multi", "basis": "multi_user"}}


def test_the_acceptance_lines_are_per_tool():
    rows = [_row("hybrid_search", 20 * i / 199, clicked=True, top=0.7, kall=0)
            for i in range(200)]
    rows += [_row("semantic_search", 20 * i / 19, clicked=True, top=0.7) for i in range(19)]
    rows += [_row("semantic_search", 3, clicked=True, top=0.3)]
    report = tabulate(rows, 0.50, SINGLE)
    hy, se = report["tools"]["hybrid_search"], report["tools"]["semantic_search"]
    assert hy["span_days"] == 20 and hy["verdict"] == "PASS"
    assert se["verdict"] == "INSUFFICIENT_DATA" and se["false_low"] == 1 and se["clicked"] == 20
    assert acceptance_line("hybrid_search", hy) == (
        "hybrid_search: false-low 0.0000 (0/200 clicked-through pages, 20 days) — PASS at < 1/50")
    assert "aggregate" not in json.dumps(report) and set(report["tools"]) == {
        "semantic_search", "hybrid_search"}


def test_fail_at_one_in_fifty():
    rows = [_row("semantic_search", 30 * i / 199, clicked=True, top=0.3 if i < 4 else 0.7)
            for i in range(200)]
    se = tabulate(rows, 0.5, SINGLE)["tools"]["semantic_search"]
    assert se["false_low_rate"] == 4 / 200 and se["verdict"] == "FAIL"
    rows[3]["top_similarity"] = 0.7
    assert tabulate(rows, 0.5, SINGLE)["tools"]["semantic_search"]["verdict"] == "PASS"


def test_a_keyword_only_clicked_through_low_counts_and_pre_change_rows_are_excluded():
    rows = [_row("hybrid_search", 0, clicked=True, top=None, kall=0),
            _row("hybrid_search", 1, clicked=True, top=None, kall=2),
            _row("hybrid_search", 40, clicked=True, top=0.1, kall=None, conf=None)]
    hy = tabulate(rows, 0.05, SINGLE)["tools"]["hybrid_search"]
    assert hy["false_low"] == 1 and hy["clicked"] == 2
    assert hy["excluded_pre_change"] == 1 and hy["rows"] == 2
    assert hy["span_days"] == 1, "the pre-change row contributes nothing to the span"
    assert would_be_low({"tool": "hybrid_search", "top_similarity": None, "keyword_all": 0},
                        1e-9)
    assert not would_be_low({"tool": "semantic_search", "top_similarity": 0.9}, 0.5)


@pytest.mark.parametrize("n, days", [(250, 10), (0, 30)])
def test_too_short_or_too_few_is_not_a_pass(n, days):
    rows = [_row("hybrid_search", days * i / max(n - 1, 1), clicked=True, top=0.9, kall=0)
            for i in range(n)]
    rows += [_row("hybrid_search", 0, clicked=False, top=0.9, kall=0),
             _row("hybrid_search", days, clicked=False, top=0.9, kall=0)]
    hy = tabulate(rows, 0.5, SINGLE)["tools"]["hybrid_search"]
    assert hy["verdict"] == "INSUFFICIENT_DATA"
    if n == 0:
        assert hy["false_low_rate"] is None
        assert "false-low n/a (0/0" in acceptance_line("hybrid_search", hy)


def test_per_class_blocks_and_deciles():
    rows = [_row("semantic_search", i, clicked=i % 2 == 0, top=i / 100,
                 cls="identifier" if i < 10 else "prose") for i in range(20)]
    se = tabulate(rows, 0.5, SINGLE)["tools"]["semantic_search"]
    assert se["per_class"]["identifier"]["rows"] == 10
    assert se["per_class"]["prose"]["rows"] == 10
    assert len(se["deciles_clicked"]) == 9 and len(se["deciles_not_clicked"]) == 9


def _fixture_mine(d, user_id=None):
    write_mine(export=FIXTURE / "usage-export.jsonl", out=d, manual=FIXTURE / "manual.jsonl",
               user_id=user_id)
    return d


def test_provenance_and_attachability_on_the_fixture(tmp_path, capsys):
    multi = run_confidence(_fixture_mine(tmp_path / "m"), 0.5)
    assert multi.report["attachable"] is False
    assert multi.table.splitlines()[0] == NOT_ATTACHABLE_LINE
    single = run_confidence(_fixture_mine(tmp_path / "s", user_id=1), 0.5)
    assert single.report["provenance"]["users"] == 1 and single.report["attachable"] is True
    assert single.table.splitlines()[0] != NOT_ATTACHABLE_LINE
    written = json.loads((tmp_path / "s" / "confidence-report.json").read_text())
    assert written == single.report
    assert ((tmp_path / "s" / "confidence-report.json").stat().st_mode & 0o777) == 0o600
    assert cli_main(["confidence", "--data", str(tmp_path / "s"), "--threshold", "0.5"]) == EXIT_OK
    out = capsys.readouterr().out
    assert out.count(" at < 1/50") == 2


def test_the_fixture_rows_file_is_written(tmp_path):
    d = _fixture_mine(tmp_path / "m")
    rows = read_jsonl(d / "confidence-rows.jsonl")
    assert [r["tool"] for r in rows] == ["semantic_search"]
    assert "confidence" not in rows[0], "the fixture predates the token"


def test_the_tabulation_does_not_leak(tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    rows = [_row("hybrid_search", 0, clicked=True, top=0.3, kall=0)]
    body = "".join(json.dumps(r) + "\n" for r in rows)
    (d / "confidence-rows.jsonl").write_text(body)
    (d / "mine-report.json").write_text(json.dumps(
        {**SINGLE, "confidence_rows": 1,
         "confidence_rows_sha256": sha256_bytes(body.encode())}))
    (d / "queries.jsonl").write_text(json.dumps(
        {"qid": "q", "text": "PASS", "pool": ["Secret.md"]}) + "\n")
    res = run_confidence(d, 0.5)
    serialized = (d / "confidence-report.json").read_text()
    assert_no_leak(serialized, {"PASS"}, {"Secret.md"}, vocabulary=frozenset(
        {"PASS", "FAIL", "INSUFFICIENT_DATA", "semantic_search", "hybrid_search"}))
    assert "Secret" not in serialized
    assert res.report["tools"]["hybrid_search"]["false_low"] == 1


@pytest.mark.parametrize("argv", [["--threshold", "0"], ["--threshold", "-1"],
                                  ["--threshold", "1.5"]])
def test_bad_thresholds_and_missing_inputs_are_usage_errors(tmp_path, argv):
    d = _fixture_mine(tmp_path / "m")
    try:
        code = cli_main(["confidence", "--data", str(d), *argv])
    except SystemExit as e:  # argparse's own refusal, exit 64 by the harness parser
        code = e.code
    assert code == EXIT_USAGE
    assert cli_main(["confidence", "--data", str(tmp_path / "none"), "--threshold", "0.5"]) == \
        EXIT_USAGE


def test_no_query_text_on_the_rows(tmp_path):
    rows = [_hy(0, "very secret words", ["Hidden/Note.md"], confidence="ok",
                top_similarity=0.9, keyword_all=1)]
    out = mine(rows).confidence_rows
    blob = json.dumps(out)
    assert "secret" not in blob and "Hidden" not in blob


# ── Codex implementation review, round 1 ────────────────────────────────────


def test_rows_from_another_mine_run_are_refused(tmp_path):
    """B1: multi-user rows left beside a single-user report (an interrupted
    or interleaved `mine`) never tabulate as attachable."""
    multi = _fixture_mine(tmp_path / "multi")
    single = _fixture_mine(tmp_path / "single", user_id=1)
    report = json.loads((single / "mine-report.json").read_text())
    assert report["confidence_rows_sha256"] == sha256_file(single / "confidence-rows.jsonl")
    assert report["confidence_rows"] == len(read_jsonl(single / "confidence-rows.jsonl"))
    # Plant the multi-user rows under the single-user report.
    forged = [*read_jsonl(multi / "confidence-rows.jsonl"),
              _row("hybrid_search", 0, clicked=True, top=0.9, kall=1)]
    (single / "confidence-rows.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in forged))
    with pytest.raises(UsageError, match="does not match"):
        run_confidence(single, 0.5)
    assert cli_main(["confidence", "--data", str(single), "--threshold", "0.5"]) == EXIT_USAGE
    assert not (single / "confidence-report.json").exists()
    # A report without the digest (an older mine) is refused too.
    del report["confidence_rows_sha256"]
    (single / "mine-report.json").write_text(json.dumps(report))
    with pytest.raises(UsageError):
        run_confidence(single, 0.5)


def test_the_rows_are_published_before_the_report(tmp_path, monkeypatch):
    from scripts.search_eval import commands

    order = []
    real = commands.atomic_write

    def spy(path, data):
        order.append(Path(path).name)
        return real(path, data)

    monkeypatch.setattr(commands, "atomic_write", spy)
    _fixture_mine(tmp_path / "m")
    assert order[-2:] == ["confidence-rows.jsonl", "mine-report.json"]


def test_truncated_queries_stay_confidence_evidence():
    """M1: 200 short clicked pages at 0.7 plus 100 clicked pages whose
    logged query hit the 200-character cap, at 0.3, over 20 days → 100/300
    FAIL at 0.5, never 0/200 PASS."""
    long_q = "x" * 200 + "…"
    rows = []
    for i in range(300):
        t = 20 * DAY * i / 299
        q, top = (f"short {i}", 0.7) if i < 200 else (long_q, 0.3)
        rows.append(_se(t, q, [f"N{i}.md"], confidence="ok", top_similarity=top))
        rows.append(rd(t + 5, f"N{i}.md"))
    out = mine(rows).confidence_rows
    assert len(out) == 300 and all(r["clicked"] for r in out)
    se = tabulate(out, 0.5, SINGLE)["tools"]["semantic_search"]
    assert (se["false_low"], se["clicked"], se["verdict"]) == (100, 300, "FAIL")
    # Refusals and unresolved principals are still not evidence.
    refused = mine([_se(0, "q", ["A.md"], confidence="ok", error="rate_limited"),
                    _se(1, "q", ["A.md"], who="UN", confidence="ok")]).confidence_rows
    assert refused == []


def test_the_judgments_ignore_the_truncated_query():
    long_q = "x" * 200 + "…"
    result = mine([_se(0, long_q, ["A.md"], confidence="ok", top_similarity=0.3),
                   rd(5, "A.md")])
    assert result.queries == [] and result.confidence_rows[0]["clicked"] is True


def test_the_fourteen_day_boundary_is_not_rounded():
    span = 13.9996
    rows = [_row("hybrid_search", span * i / 199, clicked=True, top=0.9, kall=1)
            for i in range(200)]
    hy = tabulate(rows, 0.5, SINGLE)["tools"]["hybrid_search"]
    assert hy["span_days"] == 14.0, "the display rounds"
    assert hy["verdict"] == "INSUFFICIENT_DATA"
    rows.append(_row("hybrid_search", 14.0001, clicked=True, top=0.9, kall=1))
    assert tabulate(rows, 0.5, SINGLE)["tools"]["hybrid_search"]["verdict"] == "PASS"


def test_a_null_keyword_all_counts_as_zero():
    assert would_be_low({"tool": "hybrid_search", "top_similarity": 0.3, "keyword_all": None},
                        0.5)
