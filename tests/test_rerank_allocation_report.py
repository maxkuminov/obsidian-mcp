"""The task-7.4 measurement script (#402) prints numbers only."""
from __future__ import annotations

import re

from scripts.rerank_allocation_report import _parse, report_lines
from src.services import rerank


def _chunk(text, *, index, distance, path):
    return {"chunk_text": text, "chunk_index": index, "section": None,
            "distance": distance, "file_path": path, "embed_prefix": "Secret Title > H"}


def test_report_lines_are_numbers_only():
    rows = [
        {"path": "Private/b.md", "title": "B", "rerank_stamp": 2,
         "rerank_chunks": [_chunk("b text", index=0, distance=0.1, path="Private/b.md")]},
        {"path": "Private/target.md", "title": "Secret Title", "rerank_stamp": 2,
         "rerank_chunks": [_chunk(f"secret {j}", index=j, distance=0.2 + 0.01 * pos,
                                  path="Private/target.md") for pos, j in enumerate((6, 7, 10, 2, 3))]},
    ]
    groups = rerank.semantic_document_groups(rows)
    lines = report_lines("q1", "semantic", 10, rows, "Private/target.md",
                         path_of=lambda r: r["path"],
                         chunks_of=lambda r: r["rerank_chunks"], groups=groups,
                         scores={6: -8.87, 10: 0.33})
    text = "\n".join(lines)
    assert "secret" not in text.lower() and "Private" not in text and "target" not in text
    assert lines[0] == "q1 semantic limit=10: candidates=2 documents=5 note_rank=2"
    # Ranks are global over the fetched rows; the cap of 4 leaves the fifth out.
    assert lines[1] == "  chunk 6: fetched_rank=2 allocated=yes score=-8.87"
    assert lines[3] == "  chunk 10: fetched_rank=4 allocated=yes score=0.33"
    assert lines[5] == "  chunk 3: fetched_rank=6 allocated=no"
    for line in lines[1:]:
        assert re.fullmatch(r"  chunk \d+: fetched_rank=\d+ allocated=(yes|no)( score=-?\d+\.\d\d)?",
                            line)


def test_an_absent_note_is_reported_as_absent():
    lines = report_lines("q2", "hybrid", 20, [], "x.md", path_of=lambda r: r,
                         chunks_of=lambda r: [], groups=[])
    assert lines == ["q2 hybrid limit=20: candidates=0 documents=0 note_rank=absent"]


def test_the_arguments_come_in_pairs():
    args = _parse(["--database-url", "postgresql://u@h/d", "--query", "a", "--note", "b.md",
                   "--limits", "10,20"])
    assert args.pairs == [("a", "b.md")] and args.limit_list == [10, 20]


# ── Codex impl round 1: buffered output, sanitised failure ─────────────────

import pytest  # noqa: E402

import scripts.rerank_allocation_report as report  # noqa: E402

SECRET = "SECRET TENANT QUERY about the private note"


class _Engine:
    async def dispose(self):
        pass


class _Maker:
    def __call__(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return None


def _patch(monkeypatch, *, fail_on_call: int, exc: BaseException):
    import scripts.search_eval.db as db
    import src.services.embeddings as emb
    import src.services.hybrid as hyb
    from src.services.embeddings import SemanticCandidates
    from src.services.hybrid import HybridResult

    async def fake_open(_url):
        return _Engine(), _Maker()

    calls = {"n": 0}

    async def fake_candidates(_session, _query, **_kw):
        calls["n"] += 1
        if calls["n"] == fail_on_call:
            raise exc
        return SemanticCandidates(rows=[], limit=10)

    async def fake_hybrid(_session, _query, **_kw):
        return HybridResult(rows=[])

    monkeypatch.setattr(db, "open_readonly", fake_open)
    monkeypatch.setattr(emb, "semantic_candidates", fake_candidates)
    monkeypatch.setattr(hyb, "hybrid_search", fake_hybrid)


def _argv():
    return ["--database-url", "postgresql://u@h/d", "--query", "first query", "--note", "a.md",
            "--query", SECRET, "--note", "b.md", "--limits", "10"]


def test_a_provider_failure_mid_run_prints_no_partial_report_and_no_secret(monkeypatch, capsys):
    from src.services.refusals import ProviderInputTooLarge

    _patch(monkeypatch, fail_on_call=2, exc=ProviderInputTooLarge(f"input too large: {SECRET}"))
    assert report.main(_argv()) == 1
    out, err = capsys.readouterr()
    assert out == ""  # the first query's lines were never emitted
    assert "SECRET" not in out + err and "private" not in out + err
    assert "ProviderInputTooLarge" in err


def test_a_sql_failure_prints_only_the_class(monkeypatch, capsys):
    from sqlalchemy.exc import ProgrammingError

    exc = ProgrammingError(f"SELECT … WHERE q = '{SECRET}'", {"q": SECRET}, Exception(SECRET))
    _patch(monkeypatch, fail_on_call=1, exc=exc)
    assert report.main(_argv()) == 1
    out, err = capsys.readouterr()
    assert out == "" and "SECRET" not in out + err and "ProgrammingError" in err


def test_a_bad_argument_is_not_echoed(capsys):
    assert report.main(["--database-url", "x", f"--{SECRET.replace(' ', '-')}"]) == 64
    out, err = capsys.readouterr()
    assert "SECRET" not in out + err and "bad_arguments" in err


def test_a_successful_run_emits_every_line_at_once(monkeypatch, capsys):
    _patch(monkeypatch, fail_on_call=0, exc=RuntimeError())
    assert report.main(_argv()) == 0
    out, err = capsys.readouterr()
    assert out.splitlines() == [
        "q1 semantic limit=10: candidates=0 documents=0 note_rank=absent",
        "q1 hybrid limit=10: candidates=0 documents=0 note_rank=absent",
        "q2 semantic limit=10: candidates=0 documents=0 note_rank=absent",
        "q2 hybrid limit=10: candidates=0 documents=0 note_rank=absent",
    ]
    assert "SECRET" not in out + err
