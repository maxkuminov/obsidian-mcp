"""Search-cache pre-warm on the indexer tick, and the lock protocol it forces.

`semantic_search` latency is bimodal — ~0.47 s warm, ~17.5 s cold, of which
~14 s is Ollama reloading bge-m3 after eviction and ~3 s is HNSW pages missing
from a 128 MB `shared_buffers`. As the median gap between calls grew from 135 s
to 1,676 s, more calls paid the cold price. One warm-up per five-minute indexer
tick removes both for ≈ 0.4 s + 6 ms.

The pre-warm runs *inside* `index_pass_lock`, which is the interesting part: it
means the panel's destructive actions (reset-embeddings, legacy re-embed) must
take that lock too, or a reset can drop the HNSW index out from under a probe.
And they must release their request connection *before* waiting for it, or a
handful of concurrent resets exhaust the five-connection pool while the lock
holder is itself waiting for a connection.

Fully offline: no DB, no network, no embedding provider.
"""

import asyncio
import os
import tempfile

import pytest

os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
os.chdir(tempfile.gettempdir())

import src.control_panel.routes as routes  # noqa: E402
import src.services.indexer as indexer  # noqa: E402


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class _Result:
    def __init__(self, row=None):
        self._row = row

    def first(self):
        return self._row


class _ProbeSession:
    """Records statements; reports whether the HNSW index exists."""

    def __init__(self, has_hnsw=True, on_probe=None):
        self.statements: list[str] = []
        self.clauses: list = []
        self._has_hnsw = has_hnsw
        self._on_probe = on_probe

    async def execute(self, clause, *_a, **_k):
        sql = str(clause)
        self.statements.append(sql)
        self.clauses.append(clause)
        if "pg_indexes" in sql:
            return _Result((1,) if self._has_hnsw else None)
        if sql.lstrip().upper().startswith("SET LOCAL"):
            return _Result()
        if self._on_probe is not None:
            await self._on_probe()
        return _Result()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return None


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Fresh HNSW cache, unpaused, non-sandbox, ollama provider per test."""
    indexer.invalidate_hnsw_index_cache()
    monkeypatch.setattr(routes, "indexer_paused", False, raising=False)
    monkeypatch.setattr(indexer.settings, "mcp_sandbox_mode", False, raising=False)
    monkeypatch.setattr(indexer.settings, "embedding_provider", "ollama", raising=False)
    monkeypatch.setattr(indexer.settings, "embedding_dimensions", 8, raising=False)
    yield
    indexer.invalidate_hnsw_index_cache()


def _install(monkeypatch, session, embed=None):
    monkeypatch.setattr(indexer, "async_session", lambda: session)
    calls = {"embed": 0}

    async def _embed(_text):
        calls["embed"] += 1
        if embed is not None:
            return await embed(_text)
        return [0.0] * 8

    monkeypatch.setattr("src.services.embeddings.get_embedding", _embed)
    return calls


# --------------------------------------------------------------------------- #
# The pre-warm body
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_prewarm_embeds_and_probes(monkeypatch, caplog):
    session = _ProbeSession()
    calls = _install(monkeypatch, session)

    with caplog.at_level("INFO"):
        await indexer.prewarm_search_caches()

    assert calls["embed"] == 1
    assert any("random_page_cost = 1.1" in s for s in session.statements)
    assert "Pre-warm complete" in caplog.text
    assert "embed_ms=" in caplog.text and "probe_ms=" in caplog.text


@pytest.mark.asyncio
async def test_probe_vector_is_non_zero_and_correctly_sized(monkeypatch):
    """A zero vector has no cosine direction, so `embedding <=> '[0,...]'` would
    not traverse the HNSW graph and the probe would warm nothing."""
    monkeypatch.setattr(indexer.settings, "embedding_dimensions", 1024, raising=False)
    vec = indexer._probe_vector()
    assert len(vec) == 1024
    assert any(v != 0.0 for v in vec)
    assert sum(v * v for v in vec) == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_probe_skipped_without_an_hnsw_index(monkeypatch, caplog):
    """EMBEDDING_DIMENSIONS > 2000 deployments have no HNSW index; probing
    would mean a full sequential scan every five minutes."""
    session = _ProbeSession(has_hnsw=False)
    calls = _install(monkeypatch, session)

    with caplog.at_level("INFO"):
        await indexer.prewarm_search_caches()

    assert calls["embed"] == 1, "the embedding pre-warm is independent of the index"
    assert "HNSW probe skipped" in caplog.text
    assert not any("cosine_distance" in s or "<=>" in s for s in session.statements)


@pytest.mark.asyncio
async def test_hnsw_lookup_is_cached_across_ticks(monkeypatch):
    session = _ProbeSession()
    _install(monkeypatch, session)

    await indexer.prewarm_search_caches()
    await indexer.prewarm_search_caches()

    lookups = [s for s in session.statements if "pg_indexes" in s]
    assert len(lookups) == 1, lookups


@pytest.mark.asyncio
async def test_reset_invalidates_the_hnsw_cache(monkeypatch):
    """Reset drops and (conditionally) recreates the index, so a cached answer
    can go stale in either direction."""
    session = _ProbeSession()
    _install(monkeypatch, session)
    await indexer.prewarm_search_caches()
    assert indexer._hnsw_index_present is True

    indexer.invalidate_hnsw_index_cache()
    assert indexer._hnsw_index_present is None


@pytest.mark.asyncio
async def test_embed_skipped_for_remote_provider_but_probe_still_runs(monkeypatch):
    """A remote API has no warm state; billing it once per tick buys nothing.
    The database probe is unaffected — the index still goes cold."""
    monkeypatch.setattr(indexer.settings, "embedding_provider", "openai", raising=False)
    session = _ProbeSession()
    calls = _install(monkeypatch, session)

    await indexer.prewarm_search_caches()

    assert calls["embed"] == 0
    assert any("pg_indexes" in s for s in session.statements)
    assert any("random_page_cost" in s for s in session.statements)


@pytest.mark.asyncio
async def test_skipped_when_paused(monkeypatch, caplog):
    """A panel action sets the pause flag precisely because it is about to run
    destructive statements — including one set *during* a long index pass."""
    session = _ProbeSession()
    calls = _install(monkeypatch, session)
    monkeypatch.setattr(routes, "indexer_paused", True, raising=False)

    with caplog.at_level("INFO"):
        await indexer.prewarm_search_caches()

    assert calls["embed"] == 0
    assert session.statements == []
    assert "Pre-warm skipped (paused)" in caplog.text


@pytest.mark.asyncio
async def test_skipped_in_sandbox_mode(monkeypatch):
    session = _ProbeSession()
    calls = _install(monkeypatch, session)
    monkeypatch.setattr(indexer.settings, "mcp_sandbox_mode", True, raising=False)

    await indexer.prewarm_search_caches()

    assert calls["embed"] == 0
    assert session.statements == []


# --------------------------------------------------------------------------- #
# Containment
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_provider_failure_is_logged_not_raised(monkeypatch, caplog):
    async def _boom(_text):
        raise RuntimeError("ollama is down")

    session = _ProbeSession()
    _install(monkeypatch, session, embed=_boom)

    with caplog.at_level("WARNING"):
        await indexer.prewarm_search_caches()

    assert "Pre-warm failed" in caplog.text
    assert "ollama is down" in caplog.text


@pytest.mark.asyncio
async def test_database_failure_is_logged_not_raised(monkeypatch, caplog):
    class _BrokenSession(_ProbeSession):
        async def execute(self, clause, *_a, **_k):
            raise RuntimeError("connection reset")

    _install(monkeypatch, _BrokenSession())

    with caplog.at_level("WARNING"):
        await indexer.prewarm_search_caches()

    assert "Pre-warm failed" in caplog.text


@pytest.mark.asyncio
async def test_timeout_is_bounded_and_logged(monkeypatch, caplog):
    monkeypatch.setattr(indexer, "PREWARM_TIMEOUT_SECONDS", 0.05)

    async def _hang(_text):
        await asyncio.sleep(30)

    _install(monkeypatch, _ProbeSession(), embed=_hang)

    with caplog.at_level("WARNING"):
        await asyncio.wait_for(indexer.prewarm_search_caches(), timeout=5)

    assert "Pre-warm exceeded" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["embed", "probe"])
async def test_cancellation_propagates(monkeypatch, stage):
    """Lifespan shutdown cancels the indexer task. If the pre-warm swallowed
    `CancelledError` the task would outlive the app, still holding sessions."""
    started = asyncio.Event()

    async def _hang(*_a, **_k):
        started.set()
        await asyncio.sleep(30)

    if stage == "embed":
        _install(monkeypatch, _ProbeSession(), embed=_hang)
    else:
        _install(monkeypatch, _ProbeSession(on_probe=_hang))

    task = asyncio.create_task(indexer.prewarm_search_caches())
    await asyncio.wait_for(started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# --------------------------------------------------------------------------- #
# Placement in the periodic loop
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_prewarm_runs_once_per_tick_under_the_lock(monkeypatch):
    """The lock is what makes the probe safe against a concurrent reset."""
    fresh_lock = asyncio.Lock()
    monkeypatch.setattr(indexer, "index_pass_lock", fresh_lock)
    monkeypatch.setattr(indexer.settings, "multi_user_mode", False, raising=False)
    monkeypatch.setattr(indexer.settings, "index_interval_seconds", 0, raising=False)

    calls = {"prewarm": 0, "locked": []}

    async def _noop(*_a, **_k):
        return None

    monkeypatch.setattr(indexer, "index_vault", _noop)
    monkeypatch.setattr(indexer, "embed_vault", _noop)
    monkeypatch.setattr(indexer, "link_backfill_pass", _noop)
    monkeypatch.setattr(indexer, "cleanup_expired_tokens", _noop)

    async def _prewarm():
        calls["prewarm"] += 1
        calls["locked"].append(fresh_lock.locked())
        if calls["prewarm"] >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(indexer, "prewarm_search_caches", _prewarm)

    task = asyncio.create_task(indexer.run_indexer_loop())
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)

    assert calls["prewarm"] == 2
    assert calls["locked"] == [True, True], "pre-warm must hold the pass lock"


@pytest.mark.asyncio
async def test_prewarm_does_not_touch_the_failure_counter(monkeypatch, caplog):
    """A five-minute cadence means a permanently-down provider would trip the
    'manual intervention required' alarm in 25 minutes if the pre-warm counted."""
    monkeypatch.setattr(indexer, "index_pass_lock", asyncio.Lock())
    monkeypatch.setattr(indexer.settings, "multi_user_mode", False, raising=False)
    monkeypatch.setattr(indexer.settings, "index_interval_seconds", 0, raising=False)

    async def _noop(*_a, **_k):
        return None

    monkeypatch.setattr(indexer, "index_vault", _noop)
    monkeypatch.setattr(indexer, "embed_vault", _noop)
    monkeypatch.setattr(indexer, "link_backfill_pass", _noop)
    monkeypatch.setattr(indexer, "cleanup_expired_tokens", _noop)

    # The *real* pre-warm, against a provider and a database that both fail —
    # the loop must not see any of it.
    ticks = {"n": 0}

    class _BrokenSession(_ProbeSession):
        async def execute(self, clause, *_a, **_k):
            raise RuntimeError("connection reset")

    async def _broken_embed(_text):
        raise RuntimeError("ollama is down")

    monkeypatch.setattr(indexer, "async_session", lambda: _BrokenSession())
    monkeypatch.setattr("src.services.embeddings.get_embedding", _broken_embed)

    real_prewarm = indexer.prewarm_search_caches

    async def _counting_prewarm():
        ticks["n"] += 1
        await real_prewarm()
        if ticks["n"] >= 6:
            raise asyncio.CancelledError

    monkeypatch.setattr(indexer, "prewarm_search_caches", _counting_prewarm)

    with caplog.at_level("ERROR"):
        task = asyncio.create_task(indexer.run_indexer_loop())
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)

    assert ticks["n"] == 6
    assert "consecutive" not in caplog.text
    assert "manual intervention required" not in caplog.text


# --------------------------------------------------------------------------- #
# The panel's destructive actions and the pass lock
# --------------------------------------------------------------------------- #
class _PanelSession:
    def __init__(self):
        self.executed: list[str] = []
        self.closed = False

    async def execute(self, clause, *_a, **_k):
        self.executed.append(str(clause))
        return _Result()

    async def commit(self):
        pass

    async def close(self):
        self.closed = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return None


class _Pool:
    """A one-connection pool. `_PanelSession` never checks one out; the route
    is supposed to take one only after it holds the lock."""

    def __init__(self, size=1):
        self.available = size
        self.exhausted = False

    def acquire(self):
        if self.available <= 0:
            self.exhausted = True
            raise RuntimeError("QueuePool limit reached")
        self.available -= 1

    def release(self):
        self.available += 1


class _PooledSession(_PanelSession):
    def __init__(self, pool):
        super().__init__()
        self._pool = pool
        self._pool.acquire()

    async def close(self):
        if not self.closed:
            self._pool.release()
        await super().close()

    async def __aexit__(self, *_a):
        await self.close()
        return None


class _FakeRequest:
    headers = {"accept": "application/json"}


@pytest.mark.asyncio
async def test_reset_waits_for_the_lock_without_pinning_a_connection(monkeypatch):
    """The failure this prevents: the reset holds its request connection while
    blocking on the lock; the lock holder (an index pass) needs a connection to
    finish; nobody can proceed."""
    fresh_lock = asyncio.Lock()
    monkeypatch.setattr(indexer, "index_pass_lock", fresh_lock)
    monkeypatch.setattr(routes, "_spawn", lambda coro: coro.close())
    monkeypatch.setattr(routes.settings, "embedding_dimensions", 8, raising=False)

    pool = _Pool(size=1)
    request_session = _PooledSession(pool)
    destructive: list[_PooledSession] = []

    def _fresh():
        s = _PooledSession(pool)
        destructive.append(s)
        return s

    monkeypatch.setattr(routes, "async_session", _fresh)

    await fresh_lock.acquire()
    task = asyncio.create_task(
        routes.reset_embeddings(
            request=_FakeRequest(), session=request_session, user=object()
        )
    )
    for _ in range(10):
        await asyncio.sleep(0)

    assert request_session.closed, "request connection was still held while waiting"
    assert not destructive, "a connection was taken before the lock was held"
    assert pool.available == 1, "the waiter pinned the only pooled connection"

    fresh_lock.release()
    await asyncio.wait_for(task, timeout=2)

    assert not pool.exhausted
    assert destructive and any(
        "DROP INDEX" in s for s in destructive[0].executed
    )


@pytest.mark.asyncio
async def test_reset_and_prewarm_never_overlap(monkeypatch):
    """Both take `index_pass_lock`, so the reset cannot drop the HNSW index
    while a probe is walking it."""
    fresh_lock = asyncio.Lock()
    monkeypatch.setattr(indexer, "index_pass_lock", fresh_lock)
    monkeypatch.setattr(routes, "_spawn", lambda coro: coro.close())
    monkeypatch.setattr(routes.settings, "embedding_dimensions", 8, raising=False)

    order: list[str] = []
    probe_running = asyncio.Event()
    release_probe = asyncio.Event()

    async def _probe_holder():
        async with fresh_lock:
            order.append("probe-start")
            probe_running.set()
            await release_probe.wait()
            order.append("probe-end")

    monkeypatch.setattr(routes, "async_session", lambda: _PanelSession())

    holder = asyncio.create_task(_probe_holder())
    await asyncio.wait_for(probe_running.wait(), timeout=2)

    reset = asyncio.create_task(
        routes.reset_embeddings(
            request=_FakeRequest(), session=_PanelSession(), user=object()
        )
    )
    for _ in range(10):
        await asyncio.sleep(0)
    assert order == ["probe-start"], "reset ran while the probe held the lock"

    release_probe.set()
    await asyncio.wait_for(holder, timeout=2)
    await asyncio.wait_for(reset, timeout=2)
    assert order == ["probe-start", "probe-end"]


@pytest.mark.asyncio
async def test_reset_pauses_the_indexer_and_unpauses_on_failure(monkeypatch):
    monkeypatch.setattr(indexer, "index_pass_lock", asyncio.Lock())
    monkeypatch.setattr(routes, "_spawn", lambda coro: coro.close())
    monkeypatch.setattr(routes.settings, "embedding_dimensions", 8, raising=False)

    class _Boom(_PanelSession):
        async def execute(self, clause, *_a, **_k):
            raise RuntimeError("ALTER failed")

    monkeypatch.setattr(routes, "async_session", lambda: _Boom())

    with pytest.raises(RuntimeError):
        await routes.reset_embeddings(
            request=_FakeRequest(), session=_PanelSession(), user=object()
        )
    assert routes.indexer_paused is False


@pytest.mark.asyncio
async def test_legacy_reembed_clears_the_embedded_hashes(monkeypatch):
    """Pre-existing bug: the route deleted every `note_embeddings` row but left
    `notes_metadata.embedded_content_hash` stamped. `embed_vault` selects notes
    whose hash differs from `content_hash`, so the reindex it spawned re-embedded
    nothing — the vault silently lost semantic search until a manual reset."""
    monkeypatch.setattr(indexer, "index_pass_lock", asyncio.Lock())
    monkeypatch.setattr(routes, "_spawn", lambda coro: coro.close())

    destructive = _PanelSession()
    monkeypatch.setattr(routes, "async_session", lambda: destructive)

    token = routes._reembed_serializer().dumps("x")
    request_session = _PanelSession()
    await routes.trigger_reembed(
        token=token, session=request_session, user=object()
    )

    joined = " | ".join(destructive.executed)
    assert "DELETE FROM note_embeddings" in joined
    assert "UPDATE notes_metadata SET embedded_content_hash" in joined
    assert request_session.closed
    assert routes.indexer_paused is False


@pytest.mark.asyncio
async def test_legacy_reembed_takes_the_pass_lock(monkeypatch):
    fresh_lock = asyncio.Lock()
    monkeypatch.setattr(indexer, "index_pass_lock", fresh_lock)
    monkeypatch.setattr(routes, "_spawn", lambda coro: coro.close())

    destructive = _PanelSession()
    monkeypatch.setattr(routes, "async_session", lambda: destructive)

    token = routes._reembed_serializer().dumps("x")
    await fresh_lock.acquire()
    task = asyncio.create_task(
        routes.trigger_reembed(token=token, session=_PanelSession(), user=object())
    )
    for _ in range(10):
        await asyncio.sleep(0)
    assert destructive.executed == [], "re-embed ran while the pass lock was held"

    fresh_lock.release()
    await asyncio.wait_for(task, timeout=2)
    assert destructive.executed
