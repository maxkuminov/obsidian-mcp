"""rerank-documents D5 oracles 2 and 4, against frozen pre-change renders.

`tests/fixtures/rerank_documents_golden.json` was written by **this module run
on the c491326 tree** (`RERANK_GOLDEN_WRITE=<path>`), i.e. by the code before
#402, for one-chunk-per-note rows with NULL prefixes and a prefix-blind
scorer. On this tree every page must equal it byte for byte:

- oracle 4: the reranked `semantic_search` page, and a `hybrid_search` page
  whose rows are semantic-only or keyword-only (no keyword snippet for a
  chunk winner to replace);
- oracle 2: every fallback page, one per closed reason code reachable here.

Never regenerate the file from this tree: a difference is a rendering change.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests._rerank_support import fake, fake_server, no_reranker  # noqa: F401
from tests.test_hybrid_rerank import _ExcerptSession
from tests.test_hybrid_search_offline import _offline, kw_row  # noqa: F401
import src.mcp_server.tools as tools
from src.config import settings
from src.services import embeddings

GOLDEN = Path(__file__).parent / "fixtures" / "rerank_documents_golden.json"
_WRITE = os.environ.get("RERANK_GOLDEN_WRITE")
_pages: dict[str, str] = {}

FALLBACK_MODES = {
    "http_error": "status:500",
    "malformed_response": "not_json",
    "oversize_response": "oversize",
    "count_mismatch": "short",
    "bad_index": "dup_index",
    "non_finite_score": "nan",
}


def _row(i):
    return SimpleNamespace(
        note_id=i, chunk_index=0, chunk_text=f"golden chunk {i} text", file_path=f"Notes/g{i}.md",
        title=f"G{i}", tags=["t"], content_hash="h", embedded_content_hash="h",
        chunks_truncated=False, distance=0.1 * (i + 1),
        heading_path=["Sec"] if i % 2 else [], section_ordinal=(i + 1) if i % 2 else 0,
    )


class _Session:
    def __init__(self, rows):
        self.rows = rows

    async def execute(self, clause, *_a, **_k):
        sql = str(clause)

        class _R:
            def __init__(self, rows):
                self._rows = rows

            def fetchall(self):
                return self._rows

            def all(self):
                return self._rows

            def scalar(self):
                return 0

        if sql.lstrip().upper().startswith("SET") or "count(" in sql.lower():
            return _R([])
        return _R(self.rows)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return None


def _blind(_q, docs):
    # Prefix-blind and deterministic: scores by the trailing note number.
    return [float(d.split("golden chunk ")[1][0]) if "golden chunk " in d else 0.5
            for d in docs]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    async def _log(*_a, **_k):
        return None

    async def _embed(_t, **_k):
        return [1.0, 0.0, 0.0]

    monkeypatch.setattr(tools, "_log_usage", _log)
    monkeypatch.setattr(embeddings, "get_embedding", _embed)


def _check(name: str, page: str):
    if _WRITE:
        _pages[name] = page
        Path(_WRITE).write_text(json.dumps(_pages, indent=1, sort_keys=True))
        return
    golden = json.loads(GOLDEN.read_text())
    assert page == golden[name], name


async def _semantic(monkeypatch):
    monkeypatch.setattr(tools, "async_session", lambda: _Session([_row(i) for i in range(5)]))
    return await tools.semantic_search_impl("golden", limit=3, rerank=True)


async def _hybrid(monkeypatch):
    keyword = [kw_row(10 + i, f"Notes/k{i}.md", title=f"K{i}") for i in range(3)]
    semantic = [_row(i) for i in range(3)]
    session = _ExcerptSession(excerpts={f"Notes/k{i}.md": f"golden chunk {i + 5} kw"
                                        for i in range(3)},
                              ranked=[keyword], semantic=[semantic], snippet=[[]])
    monkeypatch.setattr(tools, "async_session", lambda: session)
    return await tools.hybrid_search_impl("golden", limit=4, rerank=True)


@pytest.mark.parametrize("tool", ["semantic", "hybrid"])
async def test_one_chunk_prefix_blind_pages_are_byte_identical(monkeypatch, fake, tool):
    fake.scorer = _blind
    page = await (_semantic if tool == "semantic" else _hybrid)(monkeypatch)
    assert "reranked: true" in page.splitlines()[0]
    _check(f"{tool}:reranked", page)


@pytest.mark.parametrize("code", sorted(FALLBACK_MODES))
@pytest.mark.parametrize("tool", ["semantic", "hybrid"])
async def test_every_fallback_page_is_byte_identical(monkeypatch, fake, tool, code):
    fake.mode = FALLBACK_MODES[code]
    page = await (_semantic if tool == "semantic" else _hybrid)(monkeypatch)
    assert f"reranked: false ({code})" in page.splitlines()[0]
    _check(f"{tool}:{code}", page)


@pytest.mark.parametrize("tool", ["semantic", "hybrid"])
async def test_the_not_configured_page_is_byte_identical(monkeypatch, no_reranker, tool):
    page = await (_semantic if tool == "semantic" else _hybrid)(monkeypatch)
    assert "reranked: false (not_configured)" in page.splitlines()[0]
    _check(f"{tool}:not_configured", page)


@pytest.mark.parametrize("tool", ["semantic", "hybrid"])
async def test_the_timeout_page_is_byte_identical(monkeypatch, fake, tool):
    monkeypatch.setattr(settings, "rerank_timeout_ms", 200, raising=False)
    fake.mode = "sleep:600"
    page = await (_semantic if tool == "semantic" else _hybrid)(monkeypatch)
    assert "reranked: false (timeout)" in page.splitlines()[0]
    _check(f"{tool}:timeout", page)
