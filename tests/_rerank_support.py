"""Shared fixtures for the reranker tests (#367): the fake server, settings
pointed at it, and a clean rerank module state per test."""
from __future__ import annotations

import os
import tempfile

import pytest

os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
os.chdir(tempfile.gettempdir())

from src.config import settings  # noqa: E402
from src.services import rerank  # noqa: E402
from tests.fixtures.fake_reranker import FakeReranker  # noqa: E402


@pytest.fixture
def fake_server():
    server = FakeReranker().start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
async def fake(fake_server, monkeypatch):
    """The fake reranker, `RERANK_URL` pointed at it, the state `enabled`,
    a fresh client bound to this test's loop, closed afterwards."""
    monkeypatch.setattr(settings, "rerank_url", fake_server.url, raising=False)
    monkeypatch.setattr(settings, "rerank_timeout_ms", 1500, raising=False)
    monkeypatch.setattr(settings, "rerank_model", "fake-model", raising=False)
    monkeypatch.setattr(rerank, "_client", None)
    rerank._reset_state_for_tests()
    rerank._set_enabled()
    try:
        yield fake_server
    finally:
        await rerank.close_rerank_client()
        rerank._reset_state_for_tests()


@pytest.fixture
def no_reranker(monkeypatch):
    monkeypatch.setattr(settings, "rerank_url", "", raising=False)
    monkeypatch.setattr(rerank, "_client", None)
    rerank._reset_state_for_tests()
    yield
    rerank._reset_state_for_tests()
