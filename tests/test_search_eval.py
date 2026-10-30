"""Offline tests for the search-eval harness (#356).

No skip condition and no database: the miner, the classifier, the metrics, the
bootstrap, arm resolution, the generation manifest and the whole fixture
pipeline are pure Python over files in `tmp_path`.

Hand-checked golden numbers, `fixture-title` arm over the 12 queries that
survive the drift drop (`Old/Renamed.md` is absent from `corpus.json`, so
"old plan" is dropped). Per query, recall@10 / RR / nDCG@10:

    snake_case_name        run []                 0    0    0
    rollback procedure     run []                 0    0    0
    taxes 2026             hit at rank 1          1    1    1
    search_eval            hit at rank 1          1    1    1
    receipts               hit at rank 1          1    1    1
    weekly review (u1)     hit at rank 1          1    1    1
    #356                   1 of 2 relevant, r1    .5   1    1/(1 + 1/log2 3) = 0.613147
    deploy runbook         1 of 2 relevant, r1    .5   1    0.613147
    journal today (u2)     hit at rank 1          1    1    1
    weekly review (u2)     hit at rank 1          1    1    1
    taxes                  hit at rank 1          1    1    1
    rotate backup keys     hit at rank 1          1    1    1

    overall:     recall@10 = 9/12 = 0.75, MRR = 10/12 = 0.833333,
                 nDCG@10 = (8 + 2 * 0.613147) / 12 = 0.768858
    identifier (snake_case_name, taxes 2026, search_eval, #356, journal today):
                 recall = 3.5/5 = 0.7, MRR = 4/5 = 0.8, nDCG = 3.613147/5 = 0.722629
    prose (the other seven):
                 recall = 5.5/7 = 0.785714, MRR = 6/7 = 0.857143,
                 nDCG = 5.613147/7 = 0.801878
    mined (all but journal today): recall = 8/11 = 0.727273, MRR = 9/11,
                 nDCG = 8.226294/11 = 0.747845
    manual (deploy runbook, journal today): recall = 0.75, MRR = 1,
                 nDCG = 1.613147/2 = 0.806574
    novel@10 = 0 everywhere: every fixture-title answer is inside its pool.
    recall@5 equals recall@10 (no run is longer than two).
"""
from __future__ import annotations

import asyncio
import json
import math
import re
import subprocess
import textwrap
from pathlib import Path

import pytest

from scripts.search_eval import EXIT_FAILURE, EXIT_INSUFFICIENT, EXIT_OK, EXIT_USAGE, UsageError
from scripts.search_eval.__main__ import main as cli_main
from scripts.search_eval._io import canonical_json, pretty_json, sha256_file
from scripts.search_eval.arms import resolve_arm, resolve_arms
from scripts.search_eval.classify import classify
from scripts.search_eval.commands import write_mine
from scripts.search_eval.corpus import Corpus
from scripts.search_eval.metrics import (
    bootstrap_ci,
    dedup,
    mean,
    ndcg_at,
    recall_at,
    reciprocal_rank,
)
from scripts.search_eval.mining import (
    ManualError,
    load_export,
    derive_provenance,
    is_truncated,
    mine,
    parse_export_lines,
    parse_manual_lines,
    principal_of,
    query_id,
    usable_read,
    usable_search,
)
from scripts.search_eval.runner import run_generation
from scripts.search_eval.scoring import (
    NOT_ATTACHABLE_LINE,
    UNSTABLE_KEYS,
    ReportLeak,
    assert_no_leak,
    score,
    stable_projection,
)

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "search_eval"
EXPORT_SQL = ROOT / "scripts" / "search_eval_export.sql"
S = 1_000_000


# --------------------------------------------------------------------------
# Row builders.
# --------------------------------------------------------------------------

_ids = iter(range(1, 10**9))


def _who(who):
    return {
        "K1": dict(user_id=1, key_id=11, oauth_token_id=None, grant_id=None, actor_ref="omcp_k1"),
        "K2": dict(user_id=2, key_id=12, oauth_token_id=None, grant_id=None, actor_ref="omcp_k2"),
        "G1": dict(user_id=7, key_id=None, oauth_token_id=21, grant_id="g1", actor_ref="client"),
        "G2": dict(user_id=7, key_id=None, oauth_token_id=22, grant_id="g2", actor_ref="client"),
        "UN": dict(user_id=7, key_id=None, oauth_token_id=None, grant_id=None, actor_ref="client"),
        "U1": dict(user_id=1, key_id=13, oauth_token_id=None, grant_id=None, actor_ref="omcp_u1"),
        "U2": dict(user_id=2, key_id=14, oauth_token_id=None, grant_id=None, actor_ref="omcp_u2"),
    }[who]


def srch(t, q, paths, who="K1", tool="keyword_search", **extra):
    params = {"query": q, "folder": None, "tags": None, "frontmatter": None}
    if paths is not None:
        params["result_paths"] = paths
    params.update(extra)
    return {"id": next(_ids), "created_at": int(t * S), "tool": tool, "actor_kind": None,
            "user_filter": None, "deployment_users": 2, **_who(who), "params": params}


def rd(t, path, who="K1", **extra):
    return {"id": next(_ids), "created_at": int(t * S), "tool": "read_note", "actor_kind": None,
            "user_filter": None, "deployment_users": 2, **_who(who),
            "params": {"path": path, **extra}}


def judgments(result):
    return {(q["text"], q["user_id"]): sorted(result.qrels[q["qid"]]) for q in result.queries}


# --------------------------------------------------------------------------
# Mining scenarios (spec: "Relevance judgments SHALL be mined only ...").
# --------------------------------------------------------------------------


def test_a_read_of_a_shown_path_within_the_window_is_credited():
    res = mine([srch(0, "deploy runbook", ["Ops/Runbook.md", "X.md"]), rd(40, "Ops/Runbook.md")])
    assert judgments(res) == {("deploy runbook", 1): ["Ops/Runbook.md"]}
    assert res.qrels[res.queries[0]["qid"]] == {"Ops/Runbook.md": 1}


def test_a_read_outside_the_window_or_of_an_unshown_path_is_not_credited():
    res = mine([srch(0, "q", ["A.md"]), rd(300, "A.md"), rd(10, "B.md")])
    assert res.queries == []
    assert res.report["unattributed_reads"] == 2
    assert res.report["queries_without_judgment"] == 1


def test_the_window_is_strict_on_both_sides():
    same_instant = mine([srch(10, "q", ["A.md"]), rd(10, "A.md")])
    at_edge = mine([srch(0, "q", ["A.md"]), rd(120, "A.md")])
    inside = mine([srch(0, "q", ["A.md"]), rd(119.999999, "A.md")])
    assert same_instant.queries == [] and at_edge.queries == []
    assert len(inside.queries) == 1


def test_failed_reads_and_refused_searches_contribute_nothing():
    res = mine([
        srch(0, "a", ["A.md"]), rd(5, "A.md", error="not_found"),
        srch(100, "b", ["B.md"], over_quota=True), rd(105, "B.md"),
        srch(200, "c", ["C.md"], error="rate_limited"), rd(205, "C.md"),
    ])
    assert res.queries == []
    assert res.report["excluded"] == {"error": 2, "over_quota": 1}
    assert res.report["excluded_by_kind"]["read"] == {"error": 1}
    assert res.report["unattributed_reads"] == 2


def test_truncated_and_pre_telemetry_rows_are_excluded():
    long_q = "x" * 200 + "…"
    res = mine([srch(0, long_q, ["A.md"]), srch(1, "old", None), rd(5, "A.md"),
                srch(10, "p", ["P" * 197 + ".md"]), rd(12, "P" * 200 + "…")])
    assert res.report["excluded"] == {"no_telemetry": 1, "path_truncated": 1, "query_truncated": 1}
    assert res.queries == []
    assert is_truncated(long_q) and not is_truncated("x" * 201) and not is_truncated("x…")


def test_only_the_latest_qualifying_search_is_credited():
    res = mine([srch(0, "A", ["X.md"]), srch(30, "B", ["X.md"]), rd(60, "X.md")])
    assert judgments(res) == {("B", 1): ["X.md"]}
    assert res.report["queries_without_judgment"] == 1


def test_the_latest_search_that_contains_the_path_wins_over_a_later_one_that_does_not():
    res = mine([srch(0, "A", ["X.md"]), srch(30, "B", ["Y.md"]), rd(60, "X.md")])
    assert judgments(res) == {("A", 1): ["X.md"]}


def test_two_grants_of_the_same_oauth_client_are_two_principals():
    res = mine([srch(0, "taxes", ["X.md"], who="G1"), srch(10, "receipts", ["X.md"], who="G2"),
                rd(20, "X.md", who="G1")])
    assert judgments(res) == {("taxes", 7): ["X.md"]}


def test_an_unresolvable_principal_is_excluded_not_merged():
    res = mine([srch(0, "q", ["X.md"], who="UN"), rd(5, "X.md", who="G1"),
                srch(10, "r", ["Y.md"], who="G1"), rd(15, "Y.md", who="UN")])
    assert res.queries == []
    assert res.report["excluded"] == {"unresolved_principal": 2}
    assert res.report["unattributed_reads"] == 1
    assert principal_of(_who("UN")) is None
    assert principal_of(_who("G1")) == ("oauth", "g1")
    assert principal_of({**_who("G1"), "key_id": 5}) == ("api_key", 5)
    # A grant without the token it was resolved from is not a principal.
    assert principal_of({**_who("G1"), "oauth_token_id": None}) is None
    assert principal_of({**_who("G1"), "grant_id": None}) is None
    orphan = mine([{**srch(0, "q", ["X.md"], who="G1"), "oauth_token_id": None},
                   rd(5, "X.md", who="G1")])
    assert orphan.report["excluded"] == {"unresolved_principal": 1}


def test_principals_interleave_without_cross_crediting():
    res = mine([srch(0, "#356", ["I.md", "D.md"], who="K1"), srch(5, "search_eval", ["D.md"], who="U1"),
                rd(10, "I.md", who="K1"), rd(15, "D.md", who="U1"), rd(20, "D.md", who="K1")])
    assert judgments(res) == {("#356", 1): ["D.md", "I.md"], ("search_eval", 1): ["D.md"]}


def test_repeated_reads_collapse_into_one_judgment():
    res = mine([srch(0, "q", ["A.md"]), rd(5, "A.md"), rd(6, "A.md")])
    assert res.report["credited_reads"] == 2 and res.report["judgments"] == 1


def test_the_same_query_from_two_users_stays_two_queries():
    res = mine([srch(0, "weekly review", ["Weekly.md"], who="U1"), rd(5, "Weekly.md", who="U1"),
                srch(0, "weekly review", ["Weekly.md"], who="U2"), rd(6, "Weekly.md", who="U2")])
    assert len({q["qid"] for q in res.queries}) == 2
    assert judgments(res) == {("weekly review", 1): ["Weekly.md"], ("weekly review", 2): ["Weekly.md"]}


def test_qid_is_stable_and_folds_whitespace_and_tools():
    f = {"folder": None, "tags": None, "frontmatter": None}
    assert query_id(1, "  a   b ", f) == query_id(1, "a b", f)
    assert query_id(1, "a b", f) != query_id(1, "A b", f)
    assert query_id(1, "a b", f) != query_id(2, "a b", f)
    assert query_id(1, "a b", f) != query_id(1, "a b", {**f, "folder": "Ops"})
    assert query_id(None, "a", f) == query_id(None, "a", dict(f))
    assert re.fullmatch(r"[0-9a-f]{16}", query_id(1, "a", f))
    res = mine([srch(0, "a  b", ["A.md"]), srch(50, "a b", ["B.md"], tool="semantic_search"),
                rd(55, "B.md"), rd(56, "A.md")])
    (q,) = res.queries
    assert q["n_searches"] == 2 and q["pool"] == ["A.md", "B.md"]
    assert q["source_tools"] == ["keyword_search", "semantic_search"]


def test_malformed_export_lines_are_counted_not_raised():
    good = srch(0, "q", ["A.md"])
    lines = [json.dumps(good), "not json", "[]", json.dumps({**good, "id": "x"}),
             json.dumps({**good, "created_at": True}), json.dumps({**good, "params": "s"}), ""]
    loaded = parse_export_lines(lines)
    assert len(loaded.rows) == 1 and loaded.malformed == 5


def test_an_undecodable_line_is_malformed_not_repaired(tmp_path):
    good = json.dumps(srch(0, "q", ["A.md"])).encode()
    bad = json.dumps(srch(1, "PLACEHOLDER", ["B.md"])).encode().replace(b"PLACEHOLDER", b"caf\xe9")
    f = tmp_path / "export.jsonl"
    f.write_bytes(good + b"\n" + bad + b"\n")
    loaded = load_export(f)
    assert loaded.malformed == 1
    assert [r["params"]["query"] for r in loaded.rows] == ["q"]
    assert "\ufffd" not in json.dumps(loaded.rows)


def test_bad_shapes_are_excluded_with_a_reason():
    assert usable_search(srch(0, "q", "A.md")) == (False, "bad_result_paths")
    assert usable_search(srch(0, "q", [1])) == (False, "bad_result_paths")
    assert usable_search(srch(0, "  ", ["A.md"])) == (False, "no_query")
    assert usable_search(srch(0, "q", ["A.md"], tags="x")) == (False, "bad_filters")
    assert usable_search(srch(0, "q", ["A.md"], error=None)) == (True, None)
    assert usable_search(srch(0, "q", ["A.md"], over_quota=False)) == (True, None)
    assert usable_search(srch(0, "q", ["A.md"], folder="F" * 200 + "…")) == (False, "folder_truncated")
    assert usable_read(rd(0, "")) == (False, "no_path")
    assert usable_read(rd(0, "A.md", over_quota=True)) == (False, "over_quota")


# --------------------------------------------------------------------------
# Classifier (D5).
# --------------------------------------------------------------------------


@pytest.mark.parametrize("text,expected", [
    ("snake_case_name", "identifier"),
    ("#356", "identifier"),
    ("src/main.py", "identifier"),
    ("how do I rotate the backup keys", "prose"),
    ("v0.9.3", "identifier"),
    ("semanticSearch", "identifier"),
    ("ReadNoteResult", "identifier"),
    ("HNSW recall", "identifier"),
    ("sha256 digest", "identifier"),
    ('"exact phrase here"', "identifier"),
    ("weekly review", "prose"),
    ("deploy runbook", "prose"),
    ("I", "prose"),
    ("", "prose"),
    ("the API keys of the whole team", "prose"),  # more than four tokens
    ("what is 42", "prose"),
])
def test_classifier_table(text, expected):
    assert classify(text) == expected


# --------------------------------------------------------------------------
# Manual judgments and sources.
# --------------------------------------------------------------------------


def test_a_manual_judgment_colliding_with_a_mined_query():
    manual = parse_manual_lines([json.dumps({"user_id": 1, "text": "q  one", "relevant": ["B.md"]}),
                                 json.dumps({"user_id": 1, "text": "other", "relevant": ["C.md"],
                                             "class": "identifier"})])
    res = mine([srch(0, "q one", ["A.md", "Z.md"]), rd(5, "A.md")], manual=manual)
    by_text = {q["text"]: q for q in res.queries}
    q = by_text["q one"]
    assert res.qrels[q["qid"]] == {"A.md": 1, "B.md": 1}
    assert q["sources"] == ["manual", "mined"]
    assert q["pool"] == ["A.md", "B.md", "Z.md"]
    assert res.report["n_both_sources"] == 1
    assert res.report["per_source"] == {"mined": 1, "manual": 2}
    assert by_text["other"]["class"] == "identifier" and by_text["other"]["sources"] == ["manual"]
    assert by_text["other"]["pool"] == ["C.md"]


@pytest.mark.parametrize("line", [
    "not json", "[]", '{"text": "a", "relevant": ["A.md"]}',
    '{"user_id": 1, "text": "", "relevant": ["A.md"]}',
    '{"user_id": 1, "text": "a", "relevant": []}',
    '{"user_id": 1, "text": "a", "relevant": ["A.md"], "class": "other"}',
    '{"user_id": 1, "text": "a", "relevant": ["A.md"], "extra": 1}',
])
def test_invalid_manual_lines_are_usage_errors(line):
    with pytest.raises(ManualError):
        parse_manual_lines([line])
    assert issubclass(ManualError, UsageError)


# --------------------------------------------------------------------------
# Provenance (D10).
# --------------------------------------------------------------------------


def test_provenance_rules():
    two = [srch(0, "a", ["A.md"], who="U1"), srch(1, "b", ["B.md"], who="U2")]
    assert derive_provenance(two)["users"] == "multi"
    assert derive_provenance([])["users"] == "multi"
    one_dep = [{**r, "deployment_users": 1, "user_id": None} for r in two]
    assert derive_provenance(one_dep)["users"] == 1
    single = [{**two[0], "deployment_users": 1}]
    assert derive_provenance(single)["users"] == 1
    filtered = [{**two[0], "user_filter": "1"}]
    assert derive_provenance(filtered) == {"users": 1, "basis": "user_filter"}
    lying = [{**two[1], "user_filter": "1"}]
    assert derive_provenance(lying)["users"] == "multi"
    assert derive_provenance(two, user_filter=1)["users"] == "multi"  # unfiltered rows remain
    assert mine(two, user_filter=1).report["provenance"] == {"users": 1, "basis": "user_filter"}


# --------------------------------------------------------------------------
# Export SQL shape (D1, D3).
# --------------------------------------------------------------------------


def _statements(sql: str) -> tuple[list[str], list[str]]:
    """Split on `;` outside string literals and comments. Returns (statements, literals)."""
    stmts, literals, buf = [], [], []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j < 0 else j
            continue
        if sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            assert j >= 0, "unterminated block comment"
            i = j + 2
            continue
        if c == "'":
            j = i + 1
            while True:
                j = sql.find("'", j)
                assert j >= 0, "unterminated literal"
                if sql.startswith("''", j):
                    j += 2
                    continue
                break
            literals.append(sql[i + 1:j])
            buf.append(sql[i:j + 1])
            i = j + 1
            continue
        if c == ";":
            stmts.append(" ".join("".join(buf).split()))
            buf = []
            i += 1
            continue
        buf.append(c)
        i += 1
    tail = " ".join("".join(buf).split())
    if tail:
        stmts.append(tail)
    return stmts, literals


def test_the_export_sql_is_one_read_only_transaction_holding_one_select():
    stmts, literals = _statements(EXPORT_SQL.read_text(encoding="utf-8"))
    assert len(stmts) == 3, stmts
    assert stmts[0].upper() == "BEGIN TRANSACTION READ ONLY"
    assert stmts[2].upper() == "COMMIT"
    assert stmts[1].upper().startswith("SELECT JSON_BUILD_OBJECT(")
    assert all(";" not in lit for lit in literals)
    body = stmts[1]
    for lit in literals:
        body = body.replace(f"'{lit}'", "''")
    words = set(re.findall(r"[a-z_]+", body.lower()))
    assert not words & {"insert", "update", "delete", "merge", "copy", "create", "drop", "alter",
                        "truncate", "grant", "set", "call", "do", "lock", "into"}
    assert "from usage_logs ul left join oauth_tokens ot on ot.id = ul.oauth_token_id" in body.lower()
    for field in ("grant_id", "user_filter", "deployment_users", "created_at", "params"):
        assert f"'{field}'" in stmts[1]


def test_the_statement_splitter_catches_an_added_statement():
    sql = EXPORT_SQL.read_text(encoding="utf-8").replace("COMMIT;", "DELETE FROM usage_logs;\nCOMMIT;")
    assert len(_statements(sql)[0]) == 4


# --------------------------------------------------------------------------
# Metrics against hand-computed values (D8).
# --------------------------------------------------------------------------

L3 = 1 / math.log2(3)


def test_metric_edge_cases_from_the_spec():
    rel = {"a", "b"}
    for k in (5, 10):
        assert recall_at(rel, [], k) == 0 and ndcg_at(rel, [], k) == 0
    assert reciprocal_rank(rel, []) == 0
    run = ["x", "a", "a"]
    assert recall_at(rel, run, 10) == 0.5
    assert reciprocal_rank(rel, run) == 0.5
    assert ndcg_at(rel, run, 10) == pytest.approx(L3 / (1 + L3))
    assert recall_at(rel, ["a"], 5) == 0.5


def test_more_metric_cases():
    assert dedup(["a", "b", "a", "c", "b"]) == ["a", "b", "c"]
    assert recall_at({"a"}, ["x", "y"], 10) == 0  # relevant missing
    assert recall_at({"a", "b"}, ["a", "b"], 50) == 1  # k larger than run
    assert ndcg_at({"a", "b"}, ["a", "a", "b"], 10) == pytest.approx(1.0)  # duplicate gains once
    # more relevant than k: IDCG over k items
    rel = {f"r{i}" for i in range(5)}
    assert ndcg_at(rel, ["r0", "r1"], 2) == pytest.approx(1.0)
    assert recall_at(rel, ["r0", "r1"], 2) == pytest.approx(0.4)
    assert ndcg_at({"a"}, ["x", "a"], 1) == 0
    assert ndcg_at({"a"}, ["x", "a"], 10) == pytest.approx(L3)
    assert reciprocal_rank({"c"}, ["a", "a", "b", "c"]) == pytest.approx(1 / 3)
    assert mean([]) is None and mean([1, 2]) == 1.5


def test_bootstrap_ci():
    deltas = [0.1, -0.2, 0.3, 0.0, 0.5, -0.1, 0.2]
    assert bootstrap_ci(deltas, seed=3) == bootstrap_ci(deltas, seed=3)
    assert bootstrap_ci(deltas, seed=3) != bootstrap_ci(deltas, seed=4)
    assert bootstrap_ci([]) is None
    assert bootstrap_ci([0.25] * 9) == (0.25, 0.25)
    lo, hi = bootstrap_ci(deltas)
    assert lo <= sum(deltas) / len(deltas) <= hi and lo < hi


# --------------------------------------------------------------------------
# Data-directory helpers.
# --------------------------------------------------------------------------

F0 = {"folder": None, "tags": None, "frontmatter": None}
ALL = {"metadata": True, "keyword": True, "semantic": True}


def write_data(d: Path, queries: list[dict], snapshot: dict, *, provenance=None) -> dict:
    """queries: dicts with text, relevant, and optional user_id/pool/class/sources."""
    d.mkdir(parents=True, exist_ok=True)
    rows, qrels = [], {}
    for q in queries:
        uid = q.get("user_id", 1)
        qid = query_id(uid, q["text"], F0)
        rows.append({"qid": qid, "user_id": uid, "text": q["text"], **F0,
                     "class": q.get("class", classify(q["text"])),
                     "sources": q.get("sources", ["mined"]), "source_tools": ["keyword_search"],
                     "pool": sorted(q.get("pool", q["relevant"])), "n_searches": 1})
        qrels[qid] = {p: 1 for p in q["relevant"]}
    (d / "queries.jsonl").write_text("".join(canonical_json(r) + "\n" for r in rows))
    (d / "qrels.json").write_text(canonical_json(qrels))
    (d / "mine-report.json").write_text(pretty_json({
        "window_seconds": 120, "provenance": provenance or {"users": 1, "basis": "user_filter"}}))
    (d / "corpus-snapshot.json").write_text(canonical_json(snapshot))
    return {r["text"]: r["qid"] for r in rows}


ARM_MODULE = textwrap.dedent('''
    CALLS = []

    def _arm(fn, membership="metadata", requires_db=False):
        def factory(ctx):
            async def arm(q, depth):
                return fn(q, depth, ctx)
            return arm
        factory.membership = membership
        factory.requires_db = requires_db
        return factory

    def _raise_at(n):
        def fn(q, depth, ctx):
            CALLS.append(q.qid)
            if len(CALLS) == n:
                raise RuntimeError("boom on " + q.text)
            return ["A.md"]
        return fn

    raise_at_61 = _arm(_raise_at(61))
    fixed_a = _arm(lambda q, d, c: ["A.md"])
    empty = _arm(lambda q, d, c: [])
    non_list = _arm(lambda q, d, c: ("A.md",))
    non_str = _arm(lambda q, d, c: ["A.md", 3])
    noisy = _arm(lambda q, d, c: ["A.md", "A.md", "B.md"] + ["N%d.md" % i for i in range(30)])
    keyword_m = _arm(lambda q, d, c: ["A.md"], "keyword")
    semantic_m = _arm(lambda q, d, c: ["A.md"], "semantic")
    hybrid_m = _arm(lambda q, d, c: ["A.md"], "keyword_and_semantic")
    def no_membership(ctx):
        raise AssertionError("never built")
    def bad_membership(ctx):
        raise AssertionError("never built")
    bad_membership.membership = "everything"
''')


@pytest.fixture
def armmod(tmp_path, monkeypatch):
    name = f"se_test_arms_{abs(hash(str(tmp_path)))}"
    (tmp_path / f"{name}.py").write_text(ARM_MODULE)
    monkeypatch.syspath_prepend(str(tmp_path))
    return name


def run(d, arms, **kw):
    kw.setdefault("generation", "G1")
    return asyncio.run(run_generation(d, arms, **kw))


def manifest_of(d):
    return json.loads((d / "runs" / "manifest.json").read_text())


# --------------------------------------------------------------------------
# Drift versus corpus loss (D6).
# --------------------------------------------------------------------------

SNAP = {"1": {"A.md": dict(ALL), "B.md": dict(ALL),
              "C.md": {"metadata": True, "keyword": True, "semantic": False}}}


def test_drift_drops_paths_absent_from_the_snapshot(tmp_path, armmod):
    d = tmp_path / "d"
    write_data(d, [{"text": "q", "relevant": ["A.md", "Gone.md"]},
                   {"text": "renamed", "relevant": ["Gone.md"]}], SNAP)
    assert run(d, [f"{armmod}:fixed_a"]).exit_code == EXIT_OK
    res = score(d, min_queries=1)
    c = res.report["counts"]
    assert (c["n_queries"], c["dropped_paths"], c["dropped_queries"]) == (1, 2, 1)
    assert res.report["arms"][0]["metrics"]["overall"]["recall@10"] == 1.0
    assert res.exit_code == EXIT_OK


def test_a_candidate_that_loses_an_indexed_note_is_not_rewarded(tmp_path, armmod):
    d = tmp_path / "d"
    write_data(d, [{"text": "q", "relevant": ["A.md", "B.md"]}], SNAP)
    replay = Corpus({"1": {"A.md": dict(ALL), "C.md": SNAP["1"]["C.md"]}})
    assert run(d, [f"{armmod}:fixed_a"], replay_corpus=replay).exit_code == EXIT_OK
    res = score(d, min_queries=1)
    arm = res.report["arms"][0]
    assert arm["metrics"]["overall"]["recall@10"] == 0.5
    assert arm["corpus_loss"] == 1
    assert res.report["verdict"] == "CORPUS_LOSS" and res.exit_code == EXIT_INSUFFICIENT == 2
    assert res.report["counts"]["dropped_paths"] == 0, "the replay corpus never shrinks the qrels"


def test_losing_only_the_embeddings_is_loss_for_semantic_memberships_only(tmp_path, armmod):
    d = tmp_path / "d"
    write_data(d, [{"text": "q", "relevant": ["A.md", "B.md"]}, {"text": "c", "relevant": ["C.md"]}],
               SNAP)
    replay = Corpus({"1": {"A.md": dict(ALL), "B.md": {"metadata": True, "keyword": True,
                                                        "semantic": False},
                           "C.md": SNAP["1"]["C.md"]}})
    arms = [f"{armmod}:keyword_m", f"{armmod}:semantic_m", f"{armmod}:hybrid_m"]
    assert run(d, arms, replay_corpus=replay).exit_code == EXIT_OK
    res = score(d, min_queries=1)
    by = {a["arm"].split(":")[1]: a for a in res.report["arms"]}
    assert by["keyword_m"]["corpus_loss"] == 0 and by["keyword_m"]["unsearchable_before"] == 0
    assert by["semantic_m"]["corpus_loss"] == 1
    assert by["hybrid_m"]["corpus_loss"] == 1
    # C.md had a tsvector but no embeddings in the snapshot: unreachable before,
    # for the semantic and hybrid arms, and never their loss.
    assert by["semantic_m"]["unsearchable_before"] == 1
    assert by["hybrid_m"]["unsearchable_before"] == 1
    assert res.exit_code == 2 and res.report["verdict"] == "CORPUS_LOSS"


def test_a_db_backed_factory_without_membership_exits_64(tmp_path, armmod):
    d = tmp_path / "d"
    write_data(d, [{"text": "q", "relevant": ["A.md"]}], SNAP)
    for name in ("no_membership", "bad_membership"):
        with pytest.raises(UsageError, match="membership"):
            run(d, [f"{armmod}:{name}"])
    assert not (d / "runs").exists(), "nothing executes before resolution"
    assert cli_main(["run", "--data", str(d), "--arm", f"{armmod}:no_membership"]) == EXIT_USAGE


# --------------------------------------------------------------------------
# Arm resolution (D4).
# --------------------------------------------------------------------------

H = dict(qrels_sha256="q", snapshot_sha256="s", selected_qids_hash="h")


def test_arm_resolution(armmod):
    kw = resolve_arm("keyword", **H)
    assert (kw.kind, kw.membership, kw.requires_db) == ("builtin", "keyword", True)
    assert resolve_arm("semantic", **H).membership == "semantic"
    ft = resolve_arm("fixture-title", **H)
    assert (ft.membership, ft.requires_db) == ("metadata", False)
    imp = resolve_arm(f"{armmod}:hybrid_m", **H)
    assert (imp.kind, imp.membership) == ("import", "keyword_and_semantic")
    for bad in ("nope", "no.such.module:f", f"{armmod}:missing", "runfile:/does/not/exist.json"):
        with pytest.raises(UsageError):
            resolve_arm(bad, **H)
    with pytest.raises(UsageError):
        resolve_arms(["keyword", "keyword"], **H)


def test_no_url_no_replay(tmp_path, monkeypatch):
    monkeypatch.delenv("SEARCH_EVAL_DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://never:used@127.0.0.1:1/x")
    d = tmp_path / "d"
    write_data(d, [{"text": "q", "relevant": ["A.md"]}], SNAP)
    with pytest.raises(UsageError, match="SEARCH_EVAL_DATABASE_URL"):
        run(d, ["keyword"])
    assert not (d / "runs").exists()
    assert cli_main(["run", "--data", str(d), "--arm", "semantic"]) == EXIT_USAGE


def _copy_inputs(src: Path, dst: Path):
    dst.mkdir(parents=True)
    for name in ("queries.jsonl", "qrels.json", "mine-report.json", "corpus-snapshot.json"):
        (dst / name).write_bytes((src / name).read_bytes())


def test_runfile_arms(tmp_path, armmod):
    other = tmp_path / "other"
    write_data(other, [{"text": "q1", "relevant": ["A.md"]}, {"text": "q2", "relevant": ["B.md"]}], SNAP)
    assert run(other, [f"{armmod}:fixed_a"], generation="R1").exit_code == EXIT_OK
    staged = other / "runs" / manifest_of(other)["files"][f"{armmod}:fixed_a"]["path"]
    here = tmp_path / "here"
    _copy_inputs(other, here)
    spec = f"runfile:{staged}"
    assert run(here, ["fixture-lexical", spec], generation="H1").exit_code == EXIT_OK
    res = score(here, ["fixture-lexical", spec], baseline="fixture-lexical", min_queries=1)
    labels = [a["arm"] for a in res.report["arms"]]
    assert labels[1] == f"runfile:{armmod}:fixed_a@R1"
    assert str(tmp_path) not in json.dumps(res.report), "no filesystem path in the report"
    assert res.report["arms"][1]["missing_queries"] == 0

    # A run file missing a query: reported and scored as empty.
    content = json.loads(staged.read_text())
    dropped = sorted(content["runs"])[0]
    del content["runs"][dropped]
    staged.write_text(pretty_json(content))
    m = manifest_of(other)
    m["files"][f"{armmod}:fixed_a"]["sha256"] = sha256_file(staged)
    (other / "runs" / "manifest.json").write_text(pretty_json(m))
    assert run(here, [spec], generation="H2").exit_code == EXIT_OK
    res = score(here, [spec], min_queries=1)
    assert res.report["arms"][0]["missing_queries"] == 1

    # Not complete: refused before anything runs.
    (other / "runs" / "manifest.json").write_text(pretty_json({**m, "status": "failed"}))
    with pytest.raises(UsageError, match="not complete"):
        run(here, [spec], generation="H3")
    (other / "runs" / "manifest.json").write_text(pretty_json(m))

    # Mismatched hashes: a different qrels in the scoring directory.
    qrels = json.loads((here / "qrels.json").read_text())
    qrels[next(iter(qrels))]["Extra.md"] = 1
    (here / "qrels.json").write_text(canonical_json(qrels))
    with pytest.raises(UsageError, match="qrels_sha256"):
        run(here, [spec], generation="H4")

    # Bytes changed after completion: the manifest's sha256 no longer matches.
    staged.write_text(staged.read_text() + " ")
    with pytest.raises(UsageError, match="sha256"):
        resolve_arm(spec, qrels_sha256=m["qrels_sha256"], snapshot_sha256=m["snapshot_sha256"],
                    selected_qids_hash=m["selected_qids_hash"])


# --------------------------------------------------------------------------
# Atomicity, with the review's exact input (D4).
# --------------------------------------------------------------------------


def _seventy(d: Path):
    write_data(d, [{"text": f"query number {i:02d}", "relevant": ["A.md"]} for i in range(70)], SNAP)


def test_a_short_query_inside_a_default_generation_stamp_still_scores(tmp_path, armmod):
    """A query `2026` and a UTC-stamp generation that contains its year (#356 review)."""
    from scripts.search_eval.__main__ import _default_generation

    generation = _default_generation()
    year = generation[:4]
    d = tmp_path / "d"
    manual = tmp_path / "manual.jsonl"
    manual.write_text("".join(json.dumps({"user_id": 1, "text": t, "relevant": ["A.md"]}) + "\n"
                              for t in sorted({"2026", year})))
    (tmp_path / "export.jsonl").write_text("")
    write_mine(export=tmp_path / "export.jsonl", out=d, manual=manual)
    (d / "corpus-snapshot.json").write_text(canonical_json(SNAP))
    assert run(d, [f"{armmod}:fixed_a"], generation=generation).exit_code == EXIT_OK
    res = score(d, min_queries=1)
    assert res.exit_code == EXIT_OK
    assert year in res.report["generation"]


def test_an_arm_label_equal_to_a_query_text_trips_the_check(tmp_path, monkeypatch):
    mod = tmp_path / "hybrid_mod.py"
    mod.write_text(ARM_MODULE)
    monkeypatch.syspath_prepend(str(tmp_path))
    d = tmp_path / "d"
    write_data(d, [{"text": "hybrid_mod:fixed_a", "relevant": ["A.md"]}], SNAP)
    assert run(d, ["hybrid_mod:fixed_a"]).exit_code == EXIT_OK
    with pytest.raises(ReportLeak):
        score(d, min_queries=1)
    assert cli_main(["score", "--data", str(d), "--min-queries", "1"]) == EXIT_FAILURE


def test_a_failed_generation_cannot_be_scored_from_an_earlier_one(tmp_path, armmod):
    d = tmp_path / "d"
    _seventy(d)
    assert run(d, [f"{armmod}:fixed_a"], generation="G1").exit_code == EXIT_OK
    g1 = {p.name: p.read_bytes() for p in (d / "runs" / "G1").iterdir()}
    assert score(d, min_queries=1).exit_code == EXIT_OK

    out = run(d, [f"{armmod}:raise_at_61"], generation="G2")
    assert out.exit_code == EXIT_FAILURE
    m = manifest_of(d)
    assert (m["generation"], m["status"]) == ("G2", "failed")
    assert m["failed_arm"] == f"{armmod}:raise_at_61"
    assert m["error"] == f"RuntimeError in arm {armmod}:raise_at_61", "type and arm only (D4)"
    assert "query number" not in json.dumps(m) and "boom" not in json.dumps(m)
    assert {p.name: p.read_bytes() for p in (d / "runs" / "G1").iterdir()} == g1
    assert "G1" not in json.dumps(m.get("files", {}))
    with pytest.raises(UsageError, match="G2"):
        score(d, min_queries=1)
    assert cli_main(["score", "--data", str(d), "--min-queries", "1"]) == EXIT_USAGE


def test_an_early_failure_still_reserves_the_generation(tmp_path, monkeypatch):
    monkeypatch.setenv("SEARCH_EVAL_DATABASE_URL", "postgresql+asyncpg://x:y@127.0.0.1:1/none")
    d = tmp_path / "d"
    write_data(d, [{"text": "q", "relevant": ["A.md"]}], SNAP)
    out = run(d, ["keyword"], generation="EARLY")
    assert out.exit_code == EXIT_FAILURE
    reserved = d / "runs" / "EARLY"
    assert reserved.is_dir() and (reserved.stat().st_mode & 0o777) == 0o700
    kept = json.loads((reserved / "_manifest.json").read_text())
    assert (kept["generation"], kept["status"]) == ("EARLY", "failed")
    assert kept == manifest_of(d)
    with pytest.raises(UsageError, match="already exists"):
        run(d, ["fixture-title"], generation="EARLY")
    assert cli_main(["run", "--data", str(d), "--arm", "fixture-title",
                     "--generation", "EARLY"]) == EXIT_USAGE
    assert manifest_of(d)["status"] == "failed", "the refused re-run published nothing"


def test_a_running_manifest_is_refused(tmp_path, armmod):
    d = tmp_path / "d"
    _seventy(d)
    assert run(d, [f"{armmod}:fixed_a"], generation="G1").exit_code == EXIT_OK
    m = manifest_of(d)
    (d / "runs" / "manifest.json").write_text(pretty_json({**m, "generation": "G9",
                                                           "status": "running"}))
    with pytest.raises(UsageError, match="running"):
        score(d, min_queries=1)


@pytest.mark.parametrize("arm", ["non_list", "non_str"])
def test_wrong_output_types_abort_the_run(tmp_path, armmod, arm):
    d = tmp_path / "d"
    write_data(d, [{"text": "q", "relevant": ["A.md"]}], SNAP)
    out = run(d, [f"{armmod}:{arm}"])
    assert out.exit_code == EXIT_FAILURE
    assert manifest_of(d)["status"] == "failed"
    assert manifest_of(d)["error"] == f"ArmOutputError in arm {armmod}:{arm}"
    with pytest.raises(UsageError):
        score(d, min_queries=1)


def test_a_changed_selection_is_refused(tmp_path, armmod):
    d = tmp_path / "d"
    _seventy(d)
    assert run(d, [f"{armmod}:fixed_a"], limit_queries=40).exit_code == EXIT_OK
    assert score(d, min_queries=1).report["counts"]["n_queries"] == 40
    m = manifest_of(d)
    (d / "runs" / "manifest.json").write_text(pretty_json({**m, "selection": {"limit_queries": 50}}))
    with pytest.raises(UsageError, match="selection"):
        score(d, min_queries=1)
    (d / "runs" / "manifest.json").write_text(pretty_json(m))
    # A changed queries.jsonl: one query now belongs to an owner the snapshot lacks.
    rows = sorted((json.loads(x) for x in (d / "queries.jsonl").read_text().splitlines()),
                  key=lambda r: r["qid"])
    rows[0]["user_id"] = 99  # the first selected qid
    (d / "queries.jsonl").write_text("".join(canonical_json(r) + "\n" for r in rows))
    with pytest.raises(UsageError, match="selection"):
        score(d, min_queries=1)


def test_a_changed_run_file_or_a_reused_generation_is_refused(tmp_path, armmod):
    d = tmp_path / "d"
    write_data(d, [{"text": "q", "relevant": ["A.md"]}], SNAP)
    assert run(d, [f"{armmod}:fixed_a"]).exit_code == EXIT_OK
    with pytest.raises(UsageError, match="already exists"):
        run(d, [f"{armmod}:fixed_a"], generation="G1")
    staged = d / "runs" / next(iter(manifest_of(d)["files"].values()))["path"]
    staged.write_text(staged.read_text().replace('"A.md"', '"B.md"'))
    with pytest.raises(UsageError, match="changed"):
        score(d, min_queries=1)


def test_a_deleted_file_of_an_unrequested_arm_still_refuses_the_generation(tmp_path):
    d = _fixture_pipeline(tmp_path / "f")
    m = manifest_of(d)
    (d / "runs" / m["files"]["fixture-lexical"]["path"]).unlink()
    with pytest.raises(UsageError, match="fixture-lexical"):
        score(d, ["fixture-title"], min_queries=1)
    assert cli_main(["score", "--data", str(d), "--arm", "fixture-title",
                     "--min-queries", "1"]) == EXIT_USAGE


def test_output_is_deduplicated_and_truncated_in_order(tmp_path, armmod):
    d = tmp_path / "d"
    write_data(d, [{"text": "q", "relevant": ["B.md"]}], SNAP)
    assert run(d, [f"{armmod}:noisy"], depth=5).exit_code == EXIT_OK
    staged = json.loads((d / "runs" / next(iter(manifest_of(d)["files"].values()))["path"]).read_text())
    (ranked,) = staged["runs"].values()
    assert ranked == ["A.md", "B.md", "N0.md", "N1.md", "N2.md"]
    assert score(d, min_queries=1).report["arms"][0]["metrics"]["overall"]["mrr"] == 0.5


def test_insufficient_data_exits_2(tmp_path, armmod):
    d = tmp_path / "d"
    write_data(d, [{"text": "q", "relevant": ["A.md"]}], SNAP)
    assert run(d, [f"{armmod}:fixed_a"]).exit_code == EXIT_OK
    res = score(d, min_queries=50)
    assert res.report["verdict"] == "INSUFFICIENT_DATA" and res.exit_code == 2
    assert "recall@10" in res.table or "R@10" in res.table


def test_a_baseline_comparison_is_reproducible_and_never_gates(tmp_path, armmod):
    d = tmp_path / "d"
    write_data(d, [{"text": f"q{i}", "relevant": ["A.md"]} for i in range(6)], SNAP)
    arms = [f"{armmod}:fixed_a", f"{armmod}:empty"]
    assert run(d, arms).exit_code == EXIT_OK
    one = score(d, arms, baseline=arms[0], min_queries=1, seed=5)
    two = score(d, arms, baseline=arms[0], min_queries=1, seed=5)
    assert one.exit_code == EXIT_OK
    deltas = one.report["deltas"][arms[1]]["overall"]
    assert deltas["recall@10"]["deltas"] == [-1.0] * 6
    assert deltas["recall@10"]["mean"] == -1.0 and deltas["recall@10"]["ci95"] == [-1.0, -1.0]
    assert one.report["deltas"] == two.report["deltas"]


def test_the_report_holds_no_query_text_or_path(tmp_path):
    d = _fixture_pipeline(tmp_path / "f")
    score(d, baseline="fixture-lexical", min_queries=1)
    blob = (d / "report.json").read_text()
    texts = [json.loads(line)["text"] for line in (d / "queries.jsonl").read_text().splitlines()]
    corpus = json.loads((FIXTURE / "corpus.json").read_text())
    paths = {p for owner in corpus.values() for p in owner} | {"Old/Renamed.md"}
    for t in texts:
        assert f'"{t}"' not in blob
        if len(t) >= 4:
            assert t not in blob, t
    for p in paths:
        assert p not in blob, p
    with pytest.raises(ReportLeak):
        assert_no_leak(json.dumps({"options": {"x": "Ops/Runbook.md"}}), set(), {"Ops/Runbook.md"})
    # Arm labels, the generation and options are operator words: exact match only.
    assert_no_leak(json.dumps({"arms": [{"arm": "my deploy runbook arm"}]}),
                   {"deploy runbook"}, set())
    with pytest.raises(ReportLeak):
        assert_no_leak(json.dumps({"arms": [{"arm": "deploy runbook"}]}), {"deploy runbook"}, set())
    with pytest.raises(ReportLeak):
        assert_no_leak(json.dumps({"deltas": {"hybrid": {}}}), {"hybrid"}, set())
    assert_no_leak(json.dumps({"verdict": "OK", "arms": [{"membership": "keyword"}]}),
                   {"keyword"}, set())


# --------------------------------------------------------------------------
# The fixture pipeline and its golden report (D7).
# --------------------------------------------------------------------------


def _fixture_pipeline(d: Path, *, user_id=None) -> Path:
    write_mine(export=FIXTURE / "usage-export.jsonl", out=d, manual=FIXTURE / "manual.jsonl",
               user_id=user_id)
    out = run(d, ["fixture-lexical", "fixture-title"], generation="GOLDEN",
              fixture_corpus=FIXTURE / "corpus.json")
    assert out.exit_code == EXIT_OK, out.manifest
    return d


def _rounded(v):
    if isinstance(v, float):
        return round(v, 6)
    if isinstance(v, dict):
        return {k: _rounded(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_rounded(x) for x in v]
    return v


def test_fixture_pipeline_matches_expected_report(tmp_path):
    d = _fixture_pipeline(tmp_path / "f")
    res = score(d, ["fixture-lexical", "fixture-title"], baseline="fixture-lexical", min_queries=1)
    assert res.exit_code == EXIT_OK
    report = res.report
    expected = json.loads((FIXTURE / "expected-report.json").read_text())
    assert _rounded(stable_projection(report)) == _rounded(expected)
    assert not UNSTABLE_KEYS & set(expected)
    assert report["provenance"]["users"] == "multi" and report["attachable"] is False
    assert res.table.splitlines()[0] == NOT_ATTACHABLE_LINE
    assert json.loads((d / "report.json").read_text()) == report
    assert re.fullmatch(r"[0-9a-f]{40}", report["commit_sha"])
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True)
    assert report["commit_sha"] == head.stdout.strip()


def test_fixture_golden_numbers_by_hand(tmp_path):
    d = _fixture_pipeline(tmp_path / "f")
    report = score(d, min_queries=1).report
    title = next(a for a in report["arms"] if a["arm"] == "fixture-title")["metrics"]
    approx = lambda v: pytest.approx(v, abs=1e-6)  # noqa: E731
    nd = 1 / (1 + L3)
    assert title["overall"] == {"n_queries": 12, "recall@5": approx(0.75), "recall@10": approx(0.75),
                                "mrr": approx(10 / 12), "ndcg@10": approx((8 + 2 * nd) / 12),
                                "novel@10": 0.0}
    assert title["class:identifier"]["recall@10"] == approx(0.7)
    assert title["class:identifier"]["ndcg@10"] == approx((3 + nd) / 5)
    assert title["class:prose"]["recall@10"] == approx(5.5 / 7)
    assert title["class:prose"]["mrr"] == approx(6 / 7)
    assert title["source:mined"]["recall@10"] == approx(8 / 11)
    assert title["source:manual"] == {"n_queries": 2, "recall@5": approx(0.75),
                                      "recall@10": approx(0.75), "mrr": approx(1.0),
                                      "ndcg@10": approx((1 + nd) / 2), "novel@10": 0.0}
    c = report["counts"]
    assert (c["n_queries"], c["n_both_sources"], c["dropped_paths"], c["dropped_queries"]) == (12, 1, 1, 1)
    assert c["per_class"] == {"identifier": 5, "prose": 7}
    assert c["per_source"] == {"mined": 11, "manual": 2}
    mine_counts = report["mine"]
    assert mine_counts["malformed_lines"] == 2
    assert mine_counts["excluded"]["unresolved_principal"] == 2
    assert mine_counts["unattributed_reads"] == 5


def test_the_fixture_filtered_to_one_user_is_attachable(tmp_path):
    d = _fixture_pipeline(tmp_path / "f", user_id=1)
    res = score(d, min_queries=1)
    assert res.report["provenance"] == {"users": 1, "basis": "user_filter"}
    assert res.report["attachable"] is True
    assert res.table.splitlines()[0] != NOT_ATTACHABLE_LINE
    # The fixture's user-2 manual line ("journal today") stays out of a user-1 report.
    owners = {json.loads(x)["user_id"] for x in (d / "queries.jsonl").read_text().splitlines()}
    assert owners == {1}
    assert res.report["counts"]["per_source"]["manual"] == 1
    assert res.report["mine"]["manual_excluded_other_owner"] == 1


def test_manual_judgments_of_another_owner_never_ride_an_attachable_provenance():
    manual = parse_manual_lines([json.dumps({"user_id": 2, "text": "journal today",
                                             "relevant": ["Journal/Today.md"]})])
    rows = [srch(0, "q", ["A.md"], who="U1"), rd(5, "A.md", who="U1")]
    filtered = mine(rows, manual=manual, user_filter=1)
    assert {q["user_id"] for q in filtered.queries} == {1}
    assert filtered.report["manual_excluded_other_owner"] == 1
    assert filtered.report["provenance"]["users"] == 1
    # From the export's own USER_ID= filter, too.
    exported = mine([{**r, "user_filter": "1"} for r in rows], manual=manual)
    assert exported.report["manual_excluded_other_owner"] == 1
    assert exported.report["provenance"] == {"users": 1, "basis": "user_filter"}
    # A single-user deployment with another owner's manual line is not single-user.
    single = mine([{**r, "deployment_users": 1} for r in rows], manual=manual)
    assert single.report["provenance"] == {"users": "multi", "basis": "manual_other_owner"}


def test_the_cli_pipeline_writes_owner_only_files(tmp_path):
    d = tmp_path / "cli"
    assert cli_main(["mine", "--export", str(FIXTURE / "usage-export.jsonl"), "--manual",
                     str(FIXTURE / "manual.jsonl"), "--out", str(d)]) == EXIT_OK
    assert cli_main(["run", "--data", str(d), "--fixture-corpus", str(FIXTURE / "corpus.json"),
                     "--arm", "fixture-lexical", "fixture-title"]) == EXIT_OK
    assert cli_main(["score", "--data", str(d), "--baseline", "fixture-lexical",
                     "--min-queries", "1"]) == EXIT_OK
    assert (d.stat().st_mode & 0o777) == 0o700
    for name in ("queries.jsonl", "qrels.json", "mine-report.json", "report.json",
                 "corpus-snapshot.json"):
        assert ((d / name).stat().st_mode & 0o777) == 0o600, name
    assert cli_main(["score", "--data", str(tmp_path / "nowhere")]) == EXIT_USAGE
    assert cli_main(["mine", "--export", str(tmp_path / "none.jsonl"), "--out", str(d)]) == EXIT_USAGE
    with pytest.raises(SystemExit) as exc:
        cli_main(["bogus"])
    assert exc.value.code == EXIT_USAGE


def test_atomic_write_never_follows_a_planted_temp_symlink(tmp_path):
    from scripts.search_eval._io import atomic_write

    victim = tmp_path / "victim"
    victim.write_text("untouched")
    target = tmp_path / "qrels.json"
    (tmp_path / "qrels.json.tmp").symlink_to(victim)
    atomic_write(target, "{}")
    assert victim.read_text() == "untouched"
    assert target.read_text() == "{}" and not target.is_symlink()
    assert (target.stat().st_mode & 0o777) == 0o600
    assert sorted(p.name for p in tmp_path.iterdir()) == ["qrels.json", "qrels.json.tmp", "victim"]


def test_the_fixture_needs_no_database_settings():
    import sys

    code = ("import sys; import scripts.search_eval.__main__, scripts.search_eval.runner, "
            "scripts.search_eval.scoring, scripts.search_eval.arms; "
            "assert not [m for m in sys.modules if m == 'src' or m.startswith('src.')], "
            "sorted(m for m in sys.modules if m.startswith('src'))")
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True,
                         env={"PATH": "/usr/bin:/bin"})
    assert out.returncode == 0, out.stderr


def test_a_semantic_runfile_from_an_older_harness_scores_with_nulls(tmp_path, armmod):
    """#355 (D8): a `runfile:` semantic arm carries its meta's retrieval
    fingerprint and legacy count, or null from a harness that recorded none;
    fixture arms gain neither key."""
    other = tmp_path / "other"
    write_data(other, [{"text": "q1", "relevant": ["A.md"]}], SNAP)
    assert run(other, [f"{armmod}:semantic_m"], generation="S1").exit_code == EXIT_OK
    staged = other / "runs" / manifest_of(other)["files"][f"{armmod}:semantic_m"]["path"]
    meta = json.loads(staged.read_text())["meta"]
    # Not DB-backed, so this producing run recorded neither field.
    assert "retrieval_fingerprint" not in meta and "legacy_chunked_notes" not in meta
    here = tmp_path / "here"
    _copy_inputs(other, here)
    spec = f"runfile:{staged}"
    assert run(here, ["fixture-lexical", spec], generation="S2").exit_code == EXIT_OK
    res = score(here, ["fixture-lexical", spec], min_queries=1)
    lexical, semantic = res.report["arms"]
    assert "retrieval_fingerprint" not in lexical and "legacy_chunked_notes" not in lexical
    assert semantic["retrieval_fingerprint"] is None
    assert semantic["legacy_chunked_notes"] is None
