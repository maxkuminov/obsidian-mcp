"""Regression test for GitHub issue #22.

In the non-sandbox lifespan path (`src/main.py`), the indexer background task
is created and scheduled BEFORE entering `async with
mcp.session_manager.run():`. The post-yield cleanup that cancels and awaits
that task originally sat *outside* any `try/finally`. If
`mcp.session_manager.run().__aenter__()` raised, the exception propagated out
of the lifespan generator and the cancel/await lines never ran, orphaning the
already-scheduled indexer task (which holds DB sessions).

The fix wraps the `async with` in `try/finally` so the indexer task is always
cancelled on every exit path. This test drives the lifespan generator with a
session manager whose `__aenter__` raises and asserts the indexer task ends up
cancelled (and that the original exception still propagates).

Fully offline: no DB, no network, no embedding provider. `run_indexer_loop`,
the startup database checks, and `session_manager.run()` are all replaced
with fakes/stubs before the generator runs.
"""

import asyncio
import os
import tempfile

import pytest

# Importing the production modules pulls in `src.config`, whose module-level
# `Settings()` singleton reads `./.env`. On this host the real `.env` carries
# host-only keys the model forbids, so we must NOT let that file load. Provide
# the same minimal defaults conftest uses and chdir to a dir without a `.env`
# (env_file is resolved relative to CWD) BEFORE importing anything.
os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
os.chdir(tempfile.gettempdir())

import src.main as main  # noqa: E402


class _RaisingSessionManager:
    """Stand-in for ``mcp.session_manager`` whose ``run()`` context manager
    fails on ``__aenter__`` — the exact failure mode issue #22 is about."""

    def run(self):
        return self

    async def __aenter__(self):
        raise RuntimeError("session manager boom")

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _OkSessionManager:
    """Stand-in whose ``run()`` enters/exits cleanly (normal shutdown path)."""

    def run(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeMcp:
    """Minimal stand-in for the ``mcp`` object the lifespan touches. The real
    ``FastMCP.session_manager`` is a read-only property, so we replace the whole
    object reference in ``src.main`` rather than its attribute."""

    def __init__(self, session_manager):
        self.session_manager = session_manager


async def _never_returns():
    """A stand-in indexer loop: runs forever until cancelled."""
    while True:
        await asyncio.sleep(3600)


def _install_fakes(monkeypatch, session_manager):
    # Force the non-sandbox branch.
    monkeypatch.setattr(main.settings, "mcp_sandbox_mode", False, raising=False)
    # Skip startup DB checks (both would otherwise open a DB session before
    # the lifecycle behavior under test is reached).
    async def _noop_check():
        return None

    monkeypatch.setattr(main, "_check_embedding_dim", _noop_check)
    monkeypatch.setattr(main, "_check_pgvector_version", _noop_check)
    monkeypatch.setattr(main, "_validate_fts_configs", _noop_check)
    # Replace the indexer loop with a forever-sleeping coroutine so it never
    # touches a DB/network and is observably cancellable.
    monkeypatch.setattr(main, "run_indexer_loop", _never_returns)
    # Swap the whole MCP object for our fake (session_manager is a read-only
    # property on the real FastMCP, so it can't be set directly).
    monkeypatch.setattr(main, "mcp", _FakeMcp(session_manager))


@pytest.mark.asyncio
async def test_indexer_cancelled_when_session_manager_aenter_raises(monkeypatch):
    """When ``session_manager.run().__aenter__()`` raises, the indexer task
    must still be cancelled (not orphaned), and the original error propagates.
    """
    _install_fakes(monkeypatch, _RaisingSessionManager())

    captured = {}
    real_create_task = asyncio.create_task

    def _spy_create_task(coro, *args, **kwargs):
        task = real_create_task(coro, *args, **kwargs)
        captured["task"] = task
        return task

    monkeypatch.setattr(main.asyncio, "create_task", _spy_create_task)

    gen = main.lifespan(object())
    with pytest.raises(RuntimeError, match="session manager boom"):
        await gen.__aenter__()

    task = captured["task"]
    # Give the event loop a tick so the cancellation requested in the
    # ``finally`` block is actually processed.
    await asyncio.sleep(0)
    assert task.cancelled(), "indexer task was orphaned instead of cancelled"


@pytest.mark.asyncio
async def test_indexer_cancelled_on_normal_shutdown(monkeypatch):
    """Sanity check: the normal startup/shutdown path still cancels the
    indexer task (behaviour unchanged by the fix)."""
    _install_fakes(monkeypatch, _OkSessionManager())

    captured = {}
    real_create_task = asyncio.create_task

    def _spy_create_task(coro, *args, **kwargs):
        task = real_create_task(coro, *args, **kwargs)
        captured["task"] = task
        return task

    monkeypatch.setattr(main.asyncio, "create_task", _spy_create_task)

    cm = main.lifespan(object())
    await cm.__aenter__()
    task = captured["task"]
    assert not task.done()
    # Trigger shutdown (runs the finally block).
    await cm.__aexit__(None, None, None)

    await asyncio.sleep(0)
    assert task.cancelled(), "indexer task was not cancelled on normal shutdown"
