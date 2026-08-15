"""Per-phase search timing in `usage_logs.params`.

`semantic_search` latency is bimodal and the two halves fail for unrelated
reasons — the embedding provider evicting bge-m3 (14 s) and HNSW index pages
falling out of a shared 128 MB `shared_buffers` (3 s). A single whole-call
`duration_ms` cannot tell those apart, so the last regression had to be
diagnosed by hand-running probes against the live database. Recording
`embed_ms` and `db_ms` makes the next one answerable from `usage_logs` alone.

The holder is a `ContextVar` owned by `_tracked`: fresh per call, cleared in
`finally`. That is the part worth pinning — a leaked value would attribute one
tool's latency to another and send the next investigation the wrong way.

Fully offline.
"""

import asyncio
import os
import tempfile

import pytest

os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
os.chdir(tempfile.gettempdir())

import src.mcp_server.tools as tools  # noqa: E402
from src.services import timing  # noqa: E402
from src.services.embeddings import semantic_search  # noqa: E402


class _Result:
    def __init__(self, rows=()):
        self._rows = list(rows)

    def fetchall(self):
        return self._rows

    def all(self):
        return self._rows

    def scalars(self):
        return self

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class _Session:
    def __init__(self, batches=(), delay=0.0):
        self._batches = list(batches)
        self._delay = delay

    async def execute(self, clause, *_a, **_k):
        sql = str(clause)
        if sql.lstrip().upper().startswith("SET LOCAL"):
            return _Result()
        if self._delay:
            await asyncio.sleep(self._delay)
        return _Result(self._batches.pop(0) if self._batches else [])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return None


class _Note:
    def __init__(self, note_id=1, path="a.md"):
        self.id = note_id
        self.file_path = path
        self.title = "a"
        self.tags = []


class _Captured:
    """Collects the params each `_tracked` call would log."""

    def __init__(self):
        self.rows: list[tuple[str, dict, int]] = []

    async def __call__(self, tool, params, duration_ms, response_size):
        self.rows.append((tool, params, duration_ms))

    def params_for(self, tool):
        return next(p for t, p, _ in self.rows if t == tool)

    def duration_for(self, tool):
        return next(d for t, _, d in self.rows if t == tool)


@pytest.fixture
def captured(monkeypatch):
    cap = _Captured()
    monkeypatch.setattr(tools, "_log_usage", cap)
    return cap


@pytest.fixture(autouse=True)
def _fake_embedding(monkeypatch):
    async def _fake(_text):
        await asyncio.sleep(0.01)
        return [1.0, 0.0, 0.0]

    monkeypatch.setattr("src.services.embeddings.get_embedding", _fake)


# --------------------------------------------------------------------------- #
# semantic_search
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_semantic_search_logs_embed_db_and_fallback(monkeypatch, captured):
    monkeypatch.setattr(tools, "async_session", lambda: _Session([[]], delay=0.01))

    await tools.semantic_search_impl("needle", limit=5)

    params = captured.params_for("semantic_search")
    assert isinstance(params["embed_ms"], int) and params["embed_ms"] >= 0
    assert isinstance(params["db_ms"], int) and params["db_ms"] >= 0
    assert params["exact_fallback"] is False
    # The phases are components of the call, never larger than the whole.
    assert params["embed_ms"] + params["db_ms"] <= captured.duration_for("semantic_search")
    # The pre-existing params are still there.
    assert params["query"] == "needle"
    assert params["limit"] == 5


@pytest.mark.asyncio
async def test_exact_fallback_is_recorded_when_it_fires(monkeypatch, captured):
    rows = [(
        type("C", (), {"note_id": 1, "chunk_index": 0, "embedding": [1.0, 0.0, 0.0],
                       "chunk_text": "x"})(),
        _Note(1, "B/a.md"),
        0.1,
    )]
    monkeypatch.setattr(tools, "async_session", lambda: _Session([[], rows]))

    await tools.semantic_search_impl("needle", limit=5, folder="B/")

    assert captured.params_for("semantic_search")["exact_fallback"] is True


# --------------------------------------------------------------------------- #
# find_related
# --------------------------------------------------------------------------- #
class _FindRelatedSession(_Session):
    def __init__(self, source, chunks, batches=()):
        super().__init__(batches)
        self._preamble = [[source] if source else [], chunks]

    async def execute(self, clause, *_a, **_k):
        sql = str(clause)
        if not sql.lstrip().upper().startswith("SET LOCAL") and self._preamble:
            await asyncio.sleep(0.005)
            return _Result(self._preamble.pop(0))
        return await super().execute(clause, *_a, **_k)


@pytest.mark.asyncio
async def test_find_related_logs_db_ms_only(monkeypatch, captured):
    row = type("R", (), {
        "note_id": 2, "file_path": "b.md", "title": "b", "tags": [],
        "chunk_text": "x", "embedding": [1.0, 0.0, 0.0], "distance": 0.1,
    })()
    monkeypatch.setattr(
        tools,
        "async_session",
        lambda: _FindRelatedSession(_Note(), [[1.0, 0.0, 0.0]], [[row]]),
    )

    await tools.find_related_impl("a.md", limit=5)

    params = captured.params_for("find_related")
    assert isinstance(params["db_ms"], int) and params["db_ms"] > 0
    assert "embed_ms" not in params, "find_related makes no embedding call"
    assert params["db_ms"] <= captured.duration_for("find_related")


@pytest.mark.asyncio
async def test_find_related_not_embedded_yet_still_reports_db_ms(monkeypatch, captured):
    """The early return did real database work; the usage row must say so, or
    the 'fast call' looks like it did nothing."""
    monkeypatch.setattr(
        tools, "async_session", lambda: _FindRelatedSession(_Note(), [])
    )

    out = await tools.find_related_impl("a.md", limit=5)

    assert "has not been embedded yet" in out
    params = captured.params_for("find_related")
    assert isinstance(params["db_ms"], int) and params["db_ms"] > 0
    assert "embed_ms" not in params


# --------------------------------------------------------------------------- #
# Scoping
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_no_leakage_into_a_later_call_in_the_same_task(monkeypatch, captured):
    """Same task, sequential calls: the second tool must not inherit the
    first's phases, or a cheap call gets blamed for an expensive one."""
    monkeypatch.setattr(tools, "async_session", lambda: _Session([[]], delay=0.01))
    await tools.semantic_search_impl("needle", limit=5)

    class _FTSSession(_Session):
        async def execute(self, clause, *_a, **_k):
            return _Result([])

    monkeypatch.setattr(tools, "async_session", lambda: _FTSSession())
    monkeypatch.setattr("src.services.search.combined_tsquery", lambda _q: "tsq")
    await tools.search_notes_impl("needle")

    first = captured.params_for("semantic_search")
    second = captured.params_for("search_notes")
    assert first["embed_ms"] >= 0
    assert "embed_ms" not in second
    assert "db_ms" not in second
    assert "exact_fallback" not in second


@pytest.mark.asyncio
async def test_holder_is_cleared_even_when_the_tool_raises(monkeypatch, captured):
    @tools._tracked("boom", [])
    async def _boom():
        timing.record("db_ms", 5)
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await _boom()

    assert timing.current() is None, "the holder outlived the call"

    # And a subsequent call starts clean.
    monkeypatch.setattr(tools, "async_session", lambda: _Session([[]]))

    class _FTSSession(_Session):
        async def execute(self, clause, *_a, **_k):
            return _Result([])

    monkeypatch.setattr(tools, "async_session", lambda: _FTSSession())
    monkeypatch.setattr("src.services.search.combined_tsquery", lambda _q: "tsq")
    await tools.search_notes_impl("needle")
    assert "db_ms" not in captured.params_for("search_notes")


@pytest.mark.asyncio
async def test_direct_service_call_outside_a_tracked_tool_does_not_raise():
    """The panel and the integration tests call `semantic_search` directly.
    With no holder installed, the timing writes must be silent no-ops."""
    assert timing.current() is None
    results = await semantic_search(_Session([[]]), "needle", limit=5)
    assert results == []
    assert timing.current() is None


@pytest.mark.asyncio
async def test_concurrent_calls_do_not_share_a_holder(monkeypatch, captured):
    """Each MCP tool call runs in its own task, and a ContextVar is per-task —
    but only if the holder is set inside the call, not at import."""
    monkeypatch.setattr(tools, "async_session", lambda: _Session([[]], delay=0.02))

    await asyncio.gather(
        tools.semantic_search_impl("one", limit=5),
        tools.semantic_search_impl("two", limit=5),
    )

    rows = [p for t, p, _ in captured.rows if t == "semantic_search"]
    assert len(rows) == 2
    for params in rows:
        assert params["embed_ms"] + params["db_ms"] <= 10_000
        assert params["exact_fallback"] is False


def test_timing_helpers_are_no_ops_without_a_holder():
    assert timing.current() is None
    timing.record("db_ms", 1)
    timing.add_ms("db_ms", 1.0)
    assert timing.current() is None


def test_add_ms_accumulates():
    token = timing.begin()
    try:
        timing.add_ms("db_ms", 0.010)
        timing.add_ms("db_ms", 0.020)
        assert timing.current()["db_ms"] == 30
    finally:
        timing.clear(token)
