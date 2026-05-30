"""Regression test for GitHub issue #12.

The periodic indexer loop (`run_indexer_loop`) and the panel-triggered
on-demand reindex (`_reindex_background` in `src/control_panel/routes.py`)
both call `index_vault`/`embed_vault`. Originally there was no mutual
exclusion: a user clicking "reindex" in the panel could launch a second
`index_vault`/`embed_vault` pass concurrently with the periodic loop over the
same scope, racing on move-detection, deleted-path removal, and per-note
embedding delete+insert.

The fix introduces a module-level `asyncio.Lock`
(`src.services.indexer.index_pass_lock`) that both the periodic loop's pass
body and `_reindex_background` acquire, so only one index/embed pass runs at a
time.

This test drives `_reindex_background` while the shared lock is already held
(simulating an in-flight periodic pass) and asserts the on-demand reindex
blocks until the lock is released — i.e. it does NOT run a concurrent
`index_vault`. It also asserts two concurrent `_reindex_background` calls never
overlap.

Fully offline: no DB, no network, no embedding provider. `index_vault` and
`embed_vault` are replaced with fakes before anything runs.
"""

import asyncio
import os
import tempfile

import pytest

# Importing the production modules pulls in `src.config`, whose module-level
# `Settings()` singleton reads `./.env`. The real `.env` on this host carries
# host-only keys the model forbids, so we must NOT let it load: provide the
# same minimal defaults conftest uses and chdir to a dir without a `.env`
# (env_file is resolved relative to CWD) BEFORE importing anything.
os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
os.chdir(tempfile.gettempdir())

import src.services.indexer as indexer  # noqa: E402
import src.control_panel.routes as routes  # noqa: E402


def test_index_pass_lock_exists():
    """The shared lock must exist at module level (the anchor of the fix)."""
    assert isinstance(indexer.index_pass_lock, asyncio.Lock)


@pytest.mark.asyncio
async def test_reindex_background_blocks_on_held_lock(monkeypatch):
    """`_reindex_background` must acquire `index_pass_lock`. While the lock is
    held (simulating an in-flight periodic pass), the on-demand reindex must
    NOT call `index_vault` — proving the two passes are serialized."""
    # Single-user mode keeps the legacy one-pass path.
    monkeypatch.setattr(routes.settings, "multi_user_mode", False, raising=False)

    index_started = asyncio.Event()

    async def _fake_index_vault(*args, **kwargs):
        index_started.set()

    async def _fake_embed_vault(*args, **kwargs):
        return None

    # `_reindex_background` imports these names from `src.services.indexer`
    # at call time, so patching the indexer module is what counts.
    monkeypatch.setattr(indexer, "index_vault", _fake_index_vault)
    monkeypatch.setattr(indexer, "embed_vault", _fake_embed_vault)

    # Use a fresh lock to avoid cross-test interference, and bind it where
    # `_reindex_background` reads it from.
    fresh_lock = asyncio.Lock()
    monkeypatch.setattr(indexer, "index_pass_lock", fresh_lock)

    # Simulate the periodic loop currently holding the pass lock.
    await fresh_lock.acquire()
    try:
        task = asyncio.create_task(routes._reindex_background())
        # Give the task ample scheduling opportunities; it must block on the
        # lock and never reach index_vault while we hold it.
        for _ in range(5):
            await asyncio.sleep(0)
        assert not index_started.is_set(), (
            "_reindex_background ran index_vault while the index pass lock was "
            "held — passes are not mutually exclusive"
        )
    finally:
        fresh_lock.release()

    # Once released, the on-demand reindex proceeds.
    await asyncio.wait_for(task, timeout=2)
    assert index_started.is_set()


@pytest.mark.asyncio
async def test_two_reindex_passes_do_not_overlap(monkeypatch):
    """Two concurrent `_reindex_background` calls must serialize via the lock:
    `index_vault` is never running for both at the same time."""
    monkeypatch.setattr(routes.settings, "multi_user_mode", False, raising=False)

    concurrency = {"current": 0, "max": 0}

    async def _fake_index_vault(*args, **kwargs):
        concurrency["current"] += 1
        concurrency["max"] = max(concurrency["max"], concurrency["current"])
        # Yield so an overlapping pass (if any) would be observed.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        concurrency["current"] -= 1

    async def _fake_embed_vault(*args, **kwargs):
        await asyncio.sleep(0)

    monkeypatch.setattr(indexer, "index_vault", _fake_index_vault)
    monkeypatch.setattr(indexer, "embed_vault", _fake_embed_vault)
    monkeypatch.setattr(indexer, "index_pass_lock", asyncio.Lock())

    await asyncio.gather(
        routes._reindex_background(),
        routes._reindex_background(),
    )

    assert concurrency["max"] == 1, (
        f"index_vault ran concurrently (max overlap={concurrency['max']}); "
        "the index pass lock did not serialize the two reindex passes"
    )
