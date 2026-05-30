"""Regression test for issue #11: embed_note dropped good vectors on provider failure.

Before the fix, embed_note deleted the note's existing NoteEmbedding rows BEFORE
calling the provider. When the provider raised, the exception was swallowed and
embed_note returned 0 without re-raising — but the DELETE was already staged on
the session. embed_vault then committed that session (indexer.py:558), so a
transient embedding failure permanently dropped previously-good vectors.

The fix moves the DELETE to after get_embeddings_batch succeeds. On failure,
no DELETE is staged, so the existing vectors survive the surrounding commit.

Fully offline: the AsyncSession and embedding provider are both faked, so no
DB / network / Ollama / OpenAI is touched.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete

import src.services.embeddings as embeddings
from src.models.db import NoteEmbedding


class _FakeNote:
    def __init__(self):
        self.id = 42
        self.file_path = "notes/example.md"
        self.content_hash = "newhash"
        self.embedded_content_hash = "oldhash"


class _RecordingSession:
    """Minimal AsyncSession stand-in that records what embed_note does.

    We don't simulate a real DB — we only need to know whether embed_note
    issued the DELETE of existing embeddings, and what rows it tried to add.
    """

    def __init__(self):
        self.delete_executed = False
        self.added: list = []
        self.flushed = False

    async def execute(self, stmt):
        # The only execute() embed_note issues is the DELETE of old embeddings.
        if isinstance(stmt, delete(NoteEmbedding).__class__):
            self.delete_executed = True
        return None

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        self.flushed = True


@pytest.mark.asyncio
async def test_provider_failure_does_not_delete_existing_embeddings(monkeypatch):
    session = _RecordingSession()
    note = _FakeNote()

    async def boom(_chunks):
        raise RuntimeError("ollama down")

    monkeypatch.setattr(embeddings, "get_embeddings_batch", boom)

    result = await embeddings.embed_note(session, note, "some real content here")

    # Failure is reported as 0 chunks (caller treats note as still un-embedded).
    assert result == 0
    # Critical invariant: the old embeddings must NOT have been deleted, so the
    # surrounding embed_vault commit cannot drop good vectors.
    assert session.delete_executed is False
    assert session.added == []
    # And the note must not be marked as freshly embedded.
    assert note.embedded_content_hash == "oldhash"


@pytest.mark.asyncio
async def test_provider_success_replaces_embeddings(monkeypatch):
    session = _RecordingSession()
    note = _FakeNote()

    async def ok(chunks):
        return [[0.0, 1.0, 2.0] for _ in chunks]

    monkeypatch.setattr(embeddings, "get_embeddings_batch", ok)

    result = await embeddings.embed_note(session, note, "some real content here")

    # On success the old embeddings are deleted and new rows are staged.
    assert result >= 1
    assert session.delete_executed is True
    assert len(session.added) == result
    assert all(isinstance(o, NoteEmbedding) for o in session.added)
    assert session.flushed is True
    # The note is marked embedded against its current content hash.
    assert note.embedded_content_hash == note.content_hash


@pytest.mark.asyncio
async def test_empty_content_short_circuits(monkeypatch):
    session = _RecordingSession()
    note = _FakeNote()

    # Provider should never be called for empty/whitespace content.
    monkeypatch.setattr(
        embeddings, "get_embeddings_batch", AsyncMock(side_effect=AssertionError)
    )

    result = await embeddings.embed_note(session, note, "   \n  ")

    assert result == 0
    assert session.delete_executed is False
    assert session.added == []
