"""Regression test for issue #11: embed_note dropped good vectors on provider failure.

Before the fix, embed_note deleted the note's existing NoteEmbedding rows BEFORE
calling the provider. When the provider raised, the exception was swallowed and
embed_note returned 0 without re-raising — but the DELETE was already staged on
the session. embed_vault then committed that session (indexer.py:558), so a
transient embedding failure permanently dropped previously-good vectors.

The fix moves the DELETE to after get_embeddings_batch succeeds. On failure,
no DELETE is staged, so the existing vectors survive the surrounding commit.

`embed_note` no longer answers with a chunk count (#201): it returns an
`EmbedNoteResult` whose `outcome` distinguishes the provider failure from the
zero-chunk certification that used to share `0` with it. The invariant this
file exists for is unchanged and is asserted on the outcome and the two chunk
counts instead.

Fully offline: the AsyncSession and embedding provider are both faked, so no
DB / network / Ollama / OpenAI is touched.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete
from sqlalchemy.sql.elements import TextClause

import src.services.embeddings as embeddings
from src.models.db import NoteEmbedding
from src.services.embeddings import NoteEmbedOutcome


class _FakeNote:
    def __init__(self):
        self.id = 42
        self.file_path = "notes/example.md"
        self.content_hash = "newhash"
        self.embedded_content_hash = "oldhash"


class _StateResult:
    """What `get_state`'s SELECT hands back: an absent row."""

    def scalar_one_or_none(self):
        return None


class _RecordingSession:
    """Minimal AsyncSession stand-in that records what embed_note does.

    We don't simulate a real DB — we only need to know whether embed_note
    issued the DELETE of existing embeddings, and what rows it tried to add.

    It also answers the two textual statements of the generation interlock
    (#206): `pg_advisory_xact_lock`, whose result is never read, and the
    `indexer_state` fingerprint read, which answers **absent**. Absent is not a
    mismatch — nothing has been claimed about the stored rows — so these tests
    exercise the ordinary path, and `lock_taken` pins that the lock is taken
    only after the provider has answered.
    """

    def __init__(self):
        self.delete_executed = False
        self.added: list = []
        self.flushed = False
        self.lock_taken = False
        self.fingerprint_read = False

    async def execute(self, stmt, params=None):
        if isinstance(stmt, TextClause):
            if "pg_advisory_xact_lock" in stmt.text:
                self.lock_taken = True
            elif "indexer_state" in stmt.text:
                self.fingerprint_read = True
                return _StateResult()
            return None
        # The only other execute() embed_note issues is the DELETE of old
        # embeddings.
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

    # The failure is its own outcome, distinguishable from a note that cleaned
    # to zero chunks and *was* certified — the conflation #201 was.
    assert result.outcome is NoteEmbedOutcome.PROVIDER_FAILED
    assert result.chunks_submitted >= 1
    assert result.chunks_embedded == 0
    # Critical invariant: the old embeddings must NOT have been deleted, so the
    # surrounding embed_vault commit cannot drop good vectors.
    assert session.delete_executed is False
    assert session.added == []
    # And the note must not be marked as freshly embedded.
    assert note.embedded_content_hash == "oldhash"
    # Nothing that could write was reached: the generation lock is taken after
    # the provider answers, and the provider never did.
    assert session.lock_taken is False


@pytest.mark.asyncio
async def test_provider_success_replaces_embeddings(monkeypatch):
    session = _RecordingSession()
    note = _FakeNote()

    async def ok(chunks):
        return [[0.0, 1.0, 2.0] for _ in chunks]

    monkeypatch.setattr(embeddings, "get_embeddings_batch", ok)

    result = await embeddings.embed_note(session, note, "some real content here")

    # On success the old embeddings are deleted and new rows are staged.
    assert result.outcome is NoteEmbedOutcome.EMBEDDED
    assert result.chunks_embedded >= 1
    assert result.chunks_submitted == result.chunks_embedded
    assert result.failure is None
    assert session.delete_executed is True
    assert len(session.added) == result.chunks_embedded
    assert all(isinstance(o, NoteEmbedding) for o in session.added)
    assert session.flushed is True
    # The note is marked embedded against its current content hash.
    assert note.embedded_content_hash == note.content_hash
    # The interlock ran, in its window: lock then fingerprint, after the
    # provider call and before anything was written.
    assert session.lock_taken is True
    assert session.fingerprint_read is True


@pytest.mark.asyncio
async def test_empty_content_short_circuits(monkeypatch):
    session = _RecordingSession()
    note = _FakeNote()

    # Provider should never be called for empty/whitespace content.
    monkeypatch.setattr(
        embeddings, "get_embeddings_batch", AsyncMock(side_effect=AssertionError)
    )

    result = await embeddings.embed_note(session, note, "   \n  ")

    # Certified with zero vectors, which is the correct representation of an
    # empty note — and a *different* outcome from the provider failure above.
    assert result.outcome is NoteEmbedOutcome.CERTIFIED_EMPTY
    assert result.chunks_submitted == 0
    assert result.chunks_embedded == 0
    assert result.failure is None
    assert session.delete_executed is True
    assert session.added == []
    assert session.flushed is True
    assert note.embedded_content_hash == note.content_hash
    # No provider call, so nothing a generation change could invalidate: this
    # branch does not take the lock, for the exclusion branch's reason.
    assert session.lock_taken is False


@pytest.mark.asyncio
async def test_partial_provider_response_preserves_existing_embeddings(monkeypatch):
    session = _RecordingSession()
    note = _FakeNote()
    monkeypatch.setattr(embeddings.settings, "chunk_size", 1)
    monkeypatch.setattr(embeddings.settings, "chunk_overlap", 0)

    async def partial(_chunks):
        return [[0.0, 1.0, 2.0]]

    monkeypatch.setattr(embeddings, "get_embeddings_batch", partial)
    result = await embeddings.embed_note(session, note, "first chunk second chunk")

    assert result.outcome is NoteEmbedOutcome.PROVIDER_CARDINALITY_MISMATCH
    assert result.chunks_embedded == 0
    assert result.failure is not None
    assert result.failure.received == 1
    assert result.failure.requested == result.chunks_submitted > 1
    assert session.delete_executed is False
    assert session.added == []
    assert note.embedded_content_hash == "oldhash"
    assert session.lock_taken is False
