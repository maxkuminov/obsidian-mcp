"""#127 / D5 — the Ollama batch has no aggregate deadline.

The old `OllamaProvider.embed_batch` carried a fixed 300 s budget over the
whole batch. A hung provider trips the 30 s per-call `wait_for` long before
that, so the only thing the aggregate ever caught was a note with more chunks
than 300 s of *healthy* latency covers: it raised, `embed_note` returned 0, the
row was never certified, and the next pass selected it again — a permanent
300 s burn per tick under `index_pass_lock` that could never complete.

Fully offline: the provider's HTTP call is replaced.
"""

import asyncio
import os
import tempfile

import pytest

os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
os.chdir(tempfile.gettempdir())

from src.services import embeddings  # noqa: E402


def test_embed_batch_takes_no_timeout_argument():
    """The retired knob is gone from the signature, not merely defaulted.

    A caller could otherwise re-impose the deadline this change removed, and
    `get_embeddings_batch` — the only production caller — has no way to pass
    one, so a surviving parameter would be a trap with no user.
    """
    import inspect

    params = inspect.signature(embeddings.OllamaProvider.embed_batch).parameters
    assert list(params) == ["self", "texts"], params
    assert list(
        inspect.signature(embeddings.get_embeddings_batch).parameters
    ) == ["texts"]


@pytest.mark.asyncio
async def test_a_batch_past_the_old_aggregate_budget_completes(monkeypatch):
    """Many chunks, each individually healthy, at a simulated latency whose
    sum exceeds the retired 300 s budget."""
    calls = {"n": 0}
    # 400 chunks × a nominal 1 s each = 400 simulated seconds, well past 300.
    # The clock is faked rather than slept through: this must stay a unit test.
    fake_now = {"t": 0.0}
    monkeypatch.setattr(embeddings.time, "monotonic", lambda: fake_now["t"])

    async def _one(_text):
        calls["n"] += 1
        fake_now["t"] += 1.0
        return [0.1, 0.2, 0.3]

    provider = embeddings.OllamaProvider()
    monkeypatch.setattr(provider, "embed_one", _one)

    out = await provider.embed_batch([f"chunk {i}" for i in range(400)])

    assert len(out) == 400
    assert calls["n"] == 400


@pytest.mark.asyncio
async def test_a_hung_chunk_still_fails_at_the_per_call_timeout(monkeypatch):
    """The per-call bound is the liveness guarantee that replaces the
    aggregate — and it is the *only* one, so it must still fire."""
    async def _hangs(_text):
        await asyncio.sleep(3600)

    provider = embeddings.OllamaProvider()
    monkeypatch.setattr(provider, "embed_one", _hangs)

    real_wait_for = asyncio.wait_for
    seen: list[float] = []

    async def _spy(coro, timeout):
        seen.append(timeout)
        # Run the real thing at a timeout short enough for a test, having
        # recorded the one production actually asks for.
        return await real_wait_for(coro, 0.05)

    monkeypatch.setattr(embeddings.asyncio, "wait_for", _spy)

    with pytest.raises((asyncio.TimeoutError, TimeoutError)):
        await provider.embed_batch(["a"])

    assert seen == [30.0], "the per-chunk timeout must stay at 30 s"


@pytest.mark.asyncio
async def test_partial_coverage_is_not_certified_on_the_certified_path(monkeypatch):
    """`embed_note`'s `len(embeddings) != len(chunks)` refusal, exercised on
    the path `embed_vault` actually uses — with `certified_hash`/
    `certified_path` supplied. Nothing may be stamped and nothing deleted."""
    class _Session:
        def __init__(self):
            self.executed = []
            self.added = []

        async def execute(self, stmt, *_a, **_k):
            self.executed.append(stmt)
            raise AssertionError("no statement may run for a partial batch")

        def add(self, obj):
            self.added.append(obj)

        async def flush(self):
            raise AssertionError("nothing may be flushed for a partial batch")

    class _Note:
        id = 1
        file_path = "A.md"
        content_hash = "hash-1"
        embedded_content_hash = "old"

    monkeypatch.setattr(embeddings.settings, "chunk_size", 1)
    monkeypatch.setattr(embeddings.settings, "chunk_overlap", 0)

    async def _short(chunks):
        return [[0.0, 1.0]] * (len(chunks) - 1)

    monkeypatch.setattr(embeddings, "get_embeddings_batch", _short)

    session, note = _Session(), _Note()
    result = await embeddings.embed_note(
        session, note, "first chunk second chunk third chunk",
        certified_hash="hash-1", certified_path="A.md",
    )

    assert result == 0
    assert session.executed == [] and session.added == []
    assert note.embedded_content_hash == "old"
