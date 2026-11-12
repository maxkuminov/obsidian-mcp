"""The search-eval recipe's rough edges (#408).

Offline: the table keeps two arms of one module apart, the score summary
states the evidence the miner and the drift drop set aside, `make
search-eval` passes `USER_ID` to `mine --user-id`, and the DEPLOYMENT.md
recipe dumps without recording, restores with `ON_ERROR_STOP`, verifies row
counts and exports from the copy.
"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from scripts.search_eval import EXIT_OK
from scripts.search_eval._io import canonical_json, pretty_json
from scripts.search_eval.classify import classify
from scripts.search_eval.commands import write_mine
from scripts.search_eval.mining import query_id
from scripts.search_eval.runner import run_generation
from scripts.search_eval.scoring import ARM_COLUMN_MIN, render_table, score

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "search_eval"
F0 = {"folder": None, "tags": None, "frontmatter": None}
ALL = {"metadata": True, "keyword": True, "semantic": True}

# A module whose name is longer than the arm column's minimum, with two
# factories that differ only after that width: the shape of
# `scripts.search_eval_arms.hybrid:make_arm` / `make_arm_alt`.
LONG_MODULE = "se_recipe_arms_with_a_long_module_name"
ARMS_SOURCE = textwrap.dedent('''
    def _arm(paths):
        def factory(ctx):
            async def arm(q, depth):
                return list(paths)
            return arm
        factory.membership = "metadata"
        factory.requires_db = False
        return factory

    make_arm = _arm(["A.md"])
    make_arm_alt = _arm([])
''')


def _write_data(d: Path, n: int, *, mine_extra: dict | None = None) -> None:
    d.mkdir(parents=True, exist_ok=True)
    rows, qrels = [], {}
    for i in range(n):
        text = f"query {i}"
        qid = query_id(1, text, F0)
        rows.append({"qid": qid, "user_id": 1, "text": text, **F0, "class": classify(text),
                     "sources": ["mined"], "source_tools": ["keyword_search"],
                     "pool": ["A.md"], "n_searches": 1})
        qrels[qid] = {"A.md": 1}
    (d / "queries.jsonl").write_text("".join(canonical_json(r) + "\n" for r in rows))
    (d / "qrels.json").write_text(canonical_json(qrels))
    (d / "mine-report.json").write_text(pretty_json({
        "window_seconds": 120, "provenance": {"users": 1, "basis": "user_filter"},
        **(mine_extra or {})}))
    (d / "corpus-snapshot.json").write_text(canonical_json({"1": {"A.md": dict(ALL)}}))


@pytest.fixture
def longmod(tmp_path, monkeypatch):
    (tmp_path / f"{LONG_MODULE}.py").write_text(ARMS_SOURCE)
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop(LONG_MODULE, None)
    yield LONG_MODULE
    sys.modules.pop(LONG_MODULE, None)


def test_two_arms_of_one_module_are_distinct_in_the_table_and_the_report(tmp_path, longmod):
    d = tmp_path / "d"
    _write_data(d, 4)
    a, b = f"{longmod}:make_arm", f"{longmod}:make_arm_alt"
    assert len(a) > ARM_COLUMN_MIN and a[:ARM_COLUMN_MIN] == b[:ARM_COLUMN_MIN]
    out = asyncio.run(run_generation(d, [a, b], generation="G1"))
    assert out.exit_code == EXIT_OK, out.manifest
    res = score(d, baseline=a, min_queries=1)
    labels = [arm["arm"] for arm in res.report["arms"]]
    assert labels == [a, b]
    assert list(res.report["deltas"]) == [b]
    rows = [line.split()[0] for line in res.table.splitlines()
            if line.split()[1:2] == ["overall"]]
    assert rows == [a, b]
    delta_rows = {line.split()[0] for line in res.table.splitlines()
                  if line.startswith("  ") and line.split()[1:2] == ["recall@10"]}
    assert delta_rows == {b}
    # Columns still line up: the header's `slice` starts where each row's slice does.
    header = next(line for line in res.table.splitlines() if line.startswith("arm "))
    for line in res.table.splitlines():
        if line.split()[1:2] == ["overall"]:
            assert line.index(" overall") == header.index(" slice")


def test_short_labels_keep_the_historical_column_width(tmp_path):
    d = tmp_path / "f"
    write_mine(export=FIXTURE / "usage-export.jsonl", out=d, manual=FIXTURE / "manual.jsonl")
    out = asyncio.run(run_generation(d, ["fixture-lexical", "fixture-title"], generation="G",
                                     fixture_corpus=FIXTURE / "corpus.json"))
    assert out.exit_code == EXIT_OK
    table = score(d, baseline="fixture-lexical", min_queries=1).table
    header = next(line for line in table.splitlines() if line.startswith("arm "))
    assert header.index("slice") == ARM_COLUMN_MIN + 1


def test_the_summary_states_the_evidence_that_was_not_scored(tmp_path, longmod):
    d = tmp_path / "d"
    _write_data(d, 3, mine_extra={
        "excluded": {"unresolved_principal": 1112, "error": 4},
        "unattributed_reads": 9, "queries_without_judgment": 21})
    # Two judged paths the snapshot no longer has: one query keeps A.md, one
    # is left with nothing and is dropped.
    qrels = json.loads((d / "qrels.json").read_text())
    first, second = sorted(qrels)[:2]
    qrels[first]["Gone.md"] = 1
    qrels[second] = {"Gone.md": 1}
    (d / "qrels.json").write_text(canonical_json(qrels))
    assert asyncio.run(run_generation(d, [f"{longmod}:make_arm"], generation="G1")).exit_code == EXIT_OK
    res = score(d, min_queries=1)
    lines = res.table.splitlines()
    assert "drift: 2 judged path(s) and 1 query(ies) absent from the snapshot; 0 ownerless" in lines
    assert ("not scored: 1112 usage row(s) unresolved_principal, 9 read(s) unattributed, "
            "21 search query(ies) without a judgment") in lines
    # The counts the lines print are the report's own, unchanged in meaning.
    assert res.report["mine"]["excluded"]["unresolved_principal"] == 1112
    assert res.report["counts"]["dropped_paths"] == 2
    assert res.report["counts"]["dropped_queries"] == 1


def test_a_report_without_mine_counts_prints_zeros(tmp_path, longmod):
    d = tmp_path / "d"
    _write_data(d, 2)
    assert asyncio.run(run_generation(d, [f"{longmod}:make_arm"], generation="G1")).exit_code == EXIT_OK
    res = score(d, min_queries=1)
    assert ("not scored: 0 usage row(s) unresolved_principal, 0 read(s) unattributed, "
            "0 search query(ies) without a judgment") in res.table.splitlines()
    # render_table is pure over the report: re-rendering gives the same text.
    assert render_table(res.report) == res.table


# --------------------------------------------------------------------------
# make search-eval passes USER_ID to `mine --user-id`.
# --------------------------------------------------------------------------


def _make_n(*args: str) -> str:
    out = subprocess.run(["make", "-n", "--no-print-directory", "search-eval", *args],
                         cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return out.stdout


@pytest.mark.skipif(shutil.which("make") is None, reason="GNU make is not installed")
def test_make_search_eval_passes_user_id_to_mine():
    text = _make_n("SEARCH_EVAL_DATA=.search-eval-test", "USER_ID=7")
    mine = re.search(r'scripts\.search_eval mine --export "\.search-eval-test/[^;]*', text)
    assert mine and "--user-id 7" in mine.group(0)
    assert "USER_ID must be empty or an integer" in text
    fixture = re.search(r"scripts\.search_eval mine --export tests/fixtures/[^;]*", text)
    assert fixture and "--user-id" not in fixture.group(0)


@pytest.mark.skipif(shutil.which("make") is None, reason="GNU make is not installed")
def test_make_search_eval_without_user_id_passes_none():
    assert "--user-id" not in _make_n("SEARCH_EVAL_DATA=.search-eval-test", "USER_ID=")


# --------------------------------------------------------------------------
# The DEPLOYMENT.md recipe.
# --------------------------------------------------------------------------


def _recipe() -> str:
    text = (ROOT / "DEPLOYMENT.md").read_text()
    start = text.index("### Measuring search quality")
    return text[start:text.index("#### Before and after a re-chunk", start)]


def test_the_recipe_dumps_without_recording():
    recipe = _recipe()
    commands = "\n".join(re.findall(r"```sh\n(.*?)```", recipe, re.S))
    assert "make db-backup" not in commands
    assert "pg_dump -U postgres --no-owner --no-privileges obsidian_mcp" in commands
    assert "--exclude-table-data" not in commands
    assert "pipefail" in commands


def test_the_recipe_restores_with_on_error_stop_and_verifies_counts():
    recipe = _recipe()
    restore = next(line for line in recipe.splitlines() if "psql -q" in line)
    assert "-v ON_ERROR_STOP=1" in restore and "--single-transaction" in restore
    counts = ("SELECT (SELECT count(*) FROM notes_metadata), (SELECT count(*) FROM "
              "note_embeddings), (SELECT count(*) FROM usage_logs)")
    assert recipe.count(counts) == 2  # once on the source, once on the copy


def test_the_recipe_exports_from_the_copy_after_the_restore():
    recipe = _recipe()
    export = recipe.index("make search-eval-export DB_BACKEND=docker DB_CONTAINER=omcp-eval "
                          "SEARCH_EVAL_DB_NAME=eval_copy")
    assert recipe.index("**Restore it into a scratch database**") < export
    assert export < recipe.index("**Snapshot, before any candidate.**")


def test_the_recipe_lists_the_harness_environment_with_placeholders_only():
    recipe = _recipe()
    for var in ("SEARCH_EVAL_DATABASE_URL", "SECRET_KEY", "DATABASE_URL", "DATABASE_SSL_MODE=disable",
                "MULTI_USER_MODE", "OLLAMA_URL", "EMBEDDING_QUERY_INSTRUCTION=$'",
                "unset RERANK_URL"):
        assert var in recipe, var
    for url in re.findall(r"(?:https?|postgresql\+asyncpg)://[^'\"`]+", recipe):
        host = re.sub(r"^[a-z+]+://(?:[^@/]*@)?", "", url).split("/")[0].split(":")[0]
        assert host == "127.0.0.1", url
    assert "unresolved_principal" in recipe and "not scored:" in recipe
