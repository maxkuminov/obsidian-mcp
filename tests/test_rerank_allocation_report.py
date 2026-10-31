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
