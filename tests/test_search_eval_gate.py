"""#403's harness side: `make_arm_alt`'s `hybrid_alt_any_offset` (the
incumbent-fusion baseline through the task-local override) and the
`python -m scripts.search_eval gate` step, on synthetic reports for every
exit path."""
from __future__ import annotations

import asyncio
import json

import pytest

from scripts.search_eval import EXIT_FAILURE, EXIT_OK, EXIT_USAGE, UsageError
from scripts.search_eval.__main__ import main as cli_main
from scripts.search_eval.arms import ArmContext
from scripts.search_eval.corpus import Corpus
from scripts.search_eval_arms import hybrid as hybrid_arm
from src.services import hybrid as hybrid_service
from tests.test_hybrid_search_eval import _query, _Session
from tests.test_search_eval import ALL, run, write_data

CAND = "scripts.search_eval_arms.hybrid:make_arm"
BASE = "scripts.search_eval_arms.hybrid:make_arm_alt"
METRICS = "recall@10,recall@20,recall@50,ndcg@10"
SLICES = "overall,class:identifier"


# ── make_arm_alt(hybrid_alt_any_offset) ─────────────────────────────────────


def _recording_service(monkeypatch):
    seen = []

    async def fake(session, query, **kw):
        seen.append(hybrid_service.any_fill_offset(True))
        return hybrid_service.HybridResult(
            rows=[hybrid_service.HybridRow(path="A.md", title="A", tags=[], score=0.1)])

    monkeypatch.setattr(hybrid_service, "hybrid_search", fake)
    return seen


def test_the_alt_arm_replays_the_incumbent_fusion_through_the_override(monkeypatch):
    seen = _recording_service(monkeypatch)
    alt = hybrid_arm.make_arm_alt(ArmContext(_Session, Corpus({}), {
        "hybrid_alt_k": "60", "hybrid_alt_any_offset": "0"}))
    live = hybrid_arm.make_arm(ArmContext(_Session, Corpus({}), {}))
    asyncio.run(alt(_query(), 10))
    asyncio.run(live(_query(), 10))
    assert seen == [0, 50]
    assert hybrid_service._any_fill_offset_override.get() is None, "reset after the call"


def test_the_alt_arm_defaults_to_the_live_constant(monkeypatch):
    seen = _recording_service(monkeypatch)
    alt = hybrid_arm.make_arm_alt(ArmContext(_Session, Corpus({}), {"hybrid_alt_k": "60"}))
    asyncio.run(alt(_query(), 10))
    assert seen == [50]


def test_concurrent_alt_and_live_queries_do_not_share_the_override(monkeypatch):
    seen = {}

    async def fake(session, query, **kw):
        await asyncio.sleep(0.01 if query == "alt" else 0.005)
        seen[query] = hybrid_service.any_fill_offset(True)
        return hybrid_service.HybridResult(rows=[])

    monkeypatch.setattr(hybrid_service, "hybrid_search", fake)
    alt = hybrid_arm.make_arm_alt(ArmContext(_Session, Corpus({}), {
        "hybrid_alt_k": "60", "hybrid_alt_any_offset": "0"}))
    live = hybrid_arm.make_arm(ArmContext(_Session, Corpus({}), {}))

    async def both():
        await asyncio.gather(alt(_query("alt"), 10), live(_query("live"), 10))

    asyncio.run(both())
    assert seen == {"alt": 0, "live": 50}


@pytest.mark.parametrize("bad", ["10", "-1", "fifty", "100"])
def test_an_invalid_offset_option_exits_64_before_the_generation(tmp_path, monkeypatch, bad):
    with pytest.raises(UsageError, match="hybrid_alt_any_offset"):
        hybrid_arm.make_arm_alt(ArmContext(None, Corpus({}), {
            "hybrid_alt_k": "60", "hybrid_alt_any_offset": bad}))
    monkeypatch.setenv("SEARCH_EVAL_DATABASE_URL", "postgresql+asyncpg://u:p@127.0.0.1:1/x")
    d = tmp_path / "d"
    write_data(d, [{"text": "q", "relevant": ["A.md"]}], {"1": {"A.md": dict(ALL)}})
    with pytest.raises(UsageError, match="hybrid_alt_any_offset"):
        run(d, [BASE], arm_options={"hybrid_alt_k": "60", "hybrid_alt_any_offset": bad})
    assert not (d / "runs").exists(), "refused before the generation is reserved"


# ── the gate ────────────────────────────────────────────────────────────────


def _cell(lower, upper=0.05, mean=0.0, n=60):
    return {"n": n, "mean": mean, "ci95": None if lower is None else [lower, upper],
            "deltas": []}


def _report(*, lower=-0.005, verdict="OK", attachable=True, users=1, baseline=BASE,
            arms=(CAND, BASE), cells=None):
    by_slice = {sl: {m: _cell(lower) for m in METRICS.split(",")}
                for sl in ("overall", "class:identifier", "class:prose")}
    if cells:
        for (sl, m), c in cells.items():
            by_slice[sl][m] = c
    return {
        "verdict": verdict, "attachable": attachable,
        "provenance": {"users": users, "basis": "user_filter"},
        "arms": [{"arm": a} for a in arms], "baseline": baseline,
        "deltas_baseline": baseline, "schema_version": 3,
        "deltas": {a: by_slice for a in arms if a != baseline},
    }


def _write(tmp_path, name, report):
    p = tmp_path / name
    p.write_text(json.dumps(report))
    return p


def _gate(tmp_path, report_path, *extra, out=None):
    out = out or tmp_path / "gate.md"
    argv = ["gate", "--report", str(report_path), "--arm", CAND, "--metrics", METRICS,
            "--slices", SLICES, "--min-ci-lower", "-0.01", "--out", str(out), *extra]
    return cli_main(argv), out


def test_every_cell_passes(tmp_path, capsys):
    code, out = _gate(tmp_path, _write(tmp_path, "r.json", _report()))
    assert code == EXIT_OK
    printed = capsys.readouterr().out
    cells = [ln for ln in printed.splitlines() if ln.startswith("  ") and "ci95" not in ln]
    assert len(cells) == 8 and all(ln.endswith(" PASS") for ln in cells)
    assert "gate: PASS" in printed
    md = out.read_text()
    assert md.count("| PASS |") == 8 and "**PASS**" in md


def test_one_cell_below_the_bound_fails(tmp_path, capsys):
    report = _report(cells={("class:identifier", "recall@10"): _cell(-0.0101)})
    code, out = _gate(tmp_path, _write(tmp_path, "r.json", report))
    assert code == EXIT_FAILURE
    printed = capsys.readouterr().out
    assert "gate: FAIL (1 of 8 cells)" in printed
    assert out.read_text().count("| FAIL |") == 1


def test_the_bound_is_inclusive(tmp_path):
    report = _report(cells={("overall", "ndcg@10"): _cell(-0.01)})
    assert _gate(tmp_path, _write(tmp_path, "r.json", report))[0] == EXIT_OK


def test_a_null_ci_fails_the_cell(tmp_path):
    report = _report(cells={("overall", "recall@50"): _cell(None)})
    assert _gate(tmp_path, _write(tmp_path, "r.json", report))[0] == EXIT_FAILURE


def _refused(tmp_path, capsys, report_path, *extra, existing=None):
    out = tmp_path / "gate.md"
    if existing is not None:
        out.write_text(existing)
    code, _ = _gate(tmp_path, report_path, *extra, out=out)
    printed = capsys.readouterr()
    assert code == EXIT_USAGE
    assert "PASS" not in printed.out and "FAIL" not in printed.out
    assert "| " not in printed.out, "no cell printed"
    if existing is None:
        assert not out.exists(), "--out is not created"
    else:
        assert out.read_text() == existing, "--out is not replaced"
    return printed.err


def test_a_multi_user_report_is_refused_with_no_output(tmp_path, capsys):
    """Codex round 2: verdict OK, a baseline, all eight cells, attachable
    false (provenance.users "multi") → 64, nothing printed, no file."""
    report = _report(attachable=False, users="multi")
    err = _refused(tmp_path, capsys, _write(tmp_path, "r.json", report))
    assert "attachable" in err
    _refused(tmp_path, capsys, _write(tmp_path, "r.json", report), existing="old table\n")


@pytest.mark.parametrize("attachable, users", [(True, "multi"), (False, 1), (True, 2),
                                                (None, 1), (True, None)])
def test_attachable_and_single_user_are_both_required(tmp_path, capsys, attachable, users):
    report = _report(attachable=attachable, users=users)
    _refused(tmp_path, capsys, _write(tmp_path, "r.json", report))


@pytest.mark.parametrize("verdict", ["CORPUS_LOSS", "INSUFFICIENT_DATA"])
def test_a_report_that_is_not_evidence_is_refused(tmp_path, capsys, verdict):
    _refused(tmp_path, capsys, _write(tmp_path, "r.json", _report(verdict=verdict)))


RF_GEN = "20261031T000000Z"
RF_LABEL = "scripts.search_eval_arms.hybrid:make_arm"
RF = f"runfile:{RF_LABEL}@{RF_GEN}"
HASHES = {"qrels_sha256": "q" * 64, "snapshot_sha256": "s" * 64, "selected_qids_hash": "h" * 64}


def _sha(path):
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rf_candidate(tmp_path, source=None, *, digest=..., source_generation=RF_GEN,
                  name="r.json", **over):
    """A candidate report whose baseline is the runfile arm RF, carrying the
    source generation and the digest the harness records when it stages RF
    (by default the digest of `source`)."""
    report = {**_report(baseline=RF, arms=(CAND, RF)), "generation": "20261101T000000Z",
              **HASHES}
    entry = report["arms"][1]
    entry["source_generation"] = source_generation
    if digest is ...:
        digest = _sha(source) if source is not None else None
    if digest is not None:
        entry["source_report_sha256"] = digest
    report.update(over)
    return _write(tmp_path, name, report)


def _rf_source(tmp_path, name="src.json", **over):
    src = {**_report(arms=(RF_LABEL, BASE)), "generation": RF_GEN, **HASHES}
    src.update(over)
    return _write(tmp_path, name, src)


def test_a_runfile_baseline_needs_its_own_attachable_report(tmp_path, capsys):
    good = _rf_source(tmp_path)
    report = _rf_candidate(tmp_path, good)
    _refused(tmp_path, capsys, report)                              # missing
    bad = _rf_source(tmp_path, "b.json", attachable=False, provenance={"users": "multi"})
    _refused(tmp_path, capsys, _rf_candidate(tmp_path, bad, name="rb.json"),
             "--baseline-report", str(bad))
    code, out = _gate(tmp_path, report, "--baseline-report", str(good))
    assert code == EXIT_OK and out.exists()


# ── Codex implementation round 2: the baseline label and the source digest ──


def test_a_relabeled_baseline_not_among_the_arms_is_refused(tmp_path, capsys):
    """Codex round 2's failing input: deltas computed against a multi-user
    runfile arm, `baseline` relabeled to a safe one that is not an arm."""
    safe_label = "runfile:hybrid-safe@20261030T000000Z"
    safe = _rf_source(tmp_path, generation="20261030T000000Z", arms=[{"arm": "hybrid-safe"}])
    report = {**_report(baseline=RF, arms=(CAND, RF)), **HASHES, "baseline": safe_label,
              "deltas_baseline": safe_label}
    report["arms"][1]["source_report_sha256"] = _sha(safe)
    err = _refused(tmp_path, capsys, _write(tmp_path, "r.json", report),
                   "--baseline-report", str(safe), existing="old table\n")
    assert "occurs 0 times" in err


def test_a_baseline_listed_twice_is_refused(tmp_path, capsys):
    good = _rf_source(tmp_path)
    report = _rf_candidate(tmp_path, good, arms=[{"arm": CAND}, {"arm": RF,
                           "source_generation": RF_GEN, "source_report_sha256": _sha(good)},
                           {"arm": RF}])
    err = _refused(tmp_path, capsys, report, "--baseline-report", str(good))
    assert "occurs 2 times" in err


@pytest.mark.parametrize("deltas_baseline", [BASE, None, "missing"])
def test_deltas_baseline_must_equal_the_baseline(tmp_path, capsys, deltas_baseline):
    good = _rf_source(tmp_path)
    over = {} if deltas_baseline == "missing" else {"deltas_baseline": deltas_baseline}
    path = _rf_candidate(tmp_path, good, **over)
    if deltas_baseline == "missing":
        body = json.loads(path.read_text())
        body.pop("deltas_baseline")
        path.write_text(json.dumps(body))
    err = _refused(tmp_path, capsys, path, "--baseline-report", str(good))
    assert "deltas_baseline" in err


def test_a_missing_source_digest_is_refused(tmp_path, capsys):
    good = _rf_source(tmp_path)
    err = _refused(tmp_path, capsys, _rf_candidate(tmp_path, good, digest=None),
                   "--baseline-report", str(good))
    assert "digest" in err


def test_a_source_digest_mismatch_is_refused(tmp_path, capsys):
    """A forged-but-consistent source report (same generation, hashes, arm)
    whose bytes are not the report the harness recorded."""
    good = _rf_source(tmp_path)
    report = _rf_candidate(tmp_path, good)
    forged = _rf_source(tmp_path, "forged.json", mine={"forged": True})
    err = _refused(tmp_path, capsys, report, "--baseline-report", str(forged),
                   existing="old table\n")
    assert "sha256" in err


def test_a_source_generation_mismatch_on_the_arm_is_refused(tmp_path, capsys):
    good = _rf_source(tmp_path)
    err = _refused(tmp_path, capsys,
                   _rf_candidate(tmp_path, good, source_generation="20261029T000000Z"),
                   "--baseline-report", str(good))
    assert "source_generation" in err


def test_the_harness_records_the_source_report_digest(tmp_path):
    """`run` stages a `runfile:` arm with the digest of its source data
    directory's `report.json` when that report scores the source generation,
    and null otherwise; `score` carries both fields into the arm entry."""
    from scripts.search_eval.runner import _source_report_sha256

    data = tmp_path / "src"
    runfile = data / "runs" / RF_GEN / "arm.json"
    runfile.parent.mkdir(parents=True)
    runfile.write_text("{}")
    assert _source_report_sha256(runfile, RF_GEN) is None          # no report
    (data / "report.json").write_text(json.dumps({"generation": "other"}))
    assert _source_report_sha256(runfile, RF_GEN) is None          # another generation
    (data / "report.json").write_text(json.dumps({"generation": RF_GEN}))
    assert _source_report_sha256(runfile, RF_GEN) == _sha(data / "report.json")
    assert _source_report_sha256(runfile, None) is None


@pytest.mark.parametrize("over", [
    {"generation": "20261031T999999Z"},                 # an unrelated safe report
    {"qrels_sha256": "x" * 64},
    {"snapshot_sha256": "x" * 64},
    {"selected_qids_hash": "x" * 64},
    {"qrels_sha256": None},
    {"arms": [{"arm": BASE}]},                          # the source arm is absent
])
def test_a_baseline_report_that_is_not_the_runfiles_source_is_refused(tmp_path, capsys, over):
    """Codex implementation round 1: an attachable single-user report that
    is not the runfile's source generation cannot vouch for it."""
    source = _rf_source(tmp_path, **over)
    # The digest matches the supplied file, so the round-1 binding is what
    # refuses it.
    report = _rf_candidate(tmp_path, source)
    err = _refused(tmp_path, capsys, report, "--baseline-report", str(source))
    assert "--baseline-report" in err
    _refused(tmp_path, capsys, report, "--baseline-report", str(source),
             existing="old table\n")


def test_a_runfile_label_without_a_generation_is_refused(tmp_path, capsys):
    rf = "runfile:no-generation"
    report = _write(tmp_path, "r.json", {**_report(baseline=rf, arms=(CAND, rf)), **HASHES})
    _refused(tmp_path, capsys, report, "--baseline-report", str(_rf_source(tmp_path)))


@pytest.mark.parametrize("mutate", [
    lambda r: r.update(baseline=None),
    lambda r: r.update(arms=[{"arm": BASE}]),
    lambda r: r.update(deltas={}),
])
def test_usage_errors(tmp_path, capsys, mutate):
    report = _report()
    mutate(report)
    _refused(tmp_path, capsys, _write(tmp_path, "r.json", report))


def test_unknown_metric_or_slice_and_a_missing_report(tmp_path, capsys):
    p = _write(tmp_path, "r.json", _report())
    out = tmp_path / "gate.md"
    assert cli_main(["gate", "--report", str(p), "--arm", CAND, "--metrics", "recall@7",
                     "--slices", SLICES, "--min-ci-lower", "-0.01", "--out", str(out)]) == 64
    assert cli_main(["gate", "--report", str(p), "--arm", CAND, "--metrics", METRICS,
                     "--slices", "class:nope", "--min-ci-lower", "-0.01",
                     "--out", str(out)]) == 64
    assert cli_main(["gate", "--report", str(tmp_path / "none.json"), "--arm", CAND,
                     "--metrics", METRICS, "--slices", SLICES, "--min-ci-lower", "-0.01",
                     "--out", str(out)]) == 64
    assert not out.exists()
    capsys.readouterr()
