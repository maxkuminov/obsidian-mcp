"""#202 — the chunker is capped per note, and a capped note is still certified.

`chunk_text` had no bound. `MAX_NOTE_BYTES` is 10 MiB and `CHUNK_SIZE` is 512
tokens (~4 characters each), so one *legal* note is ~5,120 chunks, each of them
one sequential 30 s-bounded provider call, under the single `index_pass_lock`,
with no `LIMIT` on the backlog behind it. Re-editing one such note kept every
later tenant's new and edited notes out of `notes_metadata`, the tsvector index
and the embeddings indefinitely — visible only as missing `indexer_runs` rows.

The cap is a **declared degradation**, not a skip and not a refusal: the first
N chunks in document order are embedded and the note **is** certified. An
uncertified capped note would be re-selected by the backlog on every tick for
ever and would re-perform every provider call it already made — #127's
permanent burn arriving by a new route.

Fully offline.
"""
from __future__ import annotations

import os
import tempfile

import pytest
from sqlalchemy import Delete, Update
from sqlalchemy.sql.elements import TextClause

os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
os.chdir(tempfile.gettempdir())

from src.config import MAX_CHUNKS_PER_NOTE  # noqa: E402
from src.services import embeddings  # noqa: E402
from src.services.embeddings import (  # noqa: E402
    NoteEmbedOutcome,
    chunk_text,
    chunk_text_bounded,
)
from src.services.index_state import embedding_fingerprint  # noqa: E402


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class _StateResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _RowcountResult:
    def __init__(self, rowcount):
        self.rowcount = rowcount


class _Session:
    def __init__(self):
        self.fingerprint = embedding_fingerprint()
        self.certified: list[str] = []
        self.vector_deletes = 0
        self.added: list = []

    async def execute(self, clause, params=None):
        if isinstance(clause, TextClause):
            if "indexer_state" in clause.text:
                return _StateResult(self.fingerprint)
            return None
        if isinstance(clause, Update):
            values = dict(clause._values or {})
            self.certified.append(str(next(iter(values.values())).effective_value))
            return _RowcountResult(1)
        if isinstance(clause, Delete):
            self.vector_deletes += 1
            return None
        return None

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass

    def expire(self, _obj, _attrs=None):
        pass


class _Note:
    def __init__(self):
        self.id = 3
        self.file_path = "Archive/Enormous.md"
        self.content_hash = "h-new"
        self.embedded_content_hash = "h-old"


@pytest.fixture
def tiny_chunks(monkeypatch):
    """`chunk_size=1` → four characters a chunk, so a cap is reachable cheaply
    without building the ~2 MB of prose the real cap needs."""
    monkeypatch.setattr(embeddings.settings, "chunk_size", 1)
    monkeypatch.setattr(embeddings.settings, "chunk_overlap", 0)


def _prose(chunks: int) -> str:
    """Exactly `chunks` non-empty four-character windows."""
    return "x" * (4 * chunks)


# --------------------------------------------------------------------------- #
# The bounded chunker itself, against the real cap
# --------------------------------------------------------------------------- #
def test_the_real_cap_keeps_the_first_n_chunks_in_document_order():
    # Distinct four-character windows, so "document order" is checkable rather
    # than merely counted.
    body = "".join(f"{i:04d}" for i in range(MAX_CHUNKS_PER_NOTE + 25))
    chunks, truncated = chunk_text_bounded(
        body, chunk_size=1, overlap=0, max_chunks=MAX_CHUNKS_PER_NOTE
    )

    assert truncated is True
    assert len(chunks) == MAX_CHUNKS_PER_NOTE
    assert chunks[0] == "0000"
    assert chunks[-1] == f"{MAX_CHUNKS_PER_NOTE - 1:04d}"
    # The head, never an arbitrary window: the dropped chunks are the tail.
    assert chunks == [f"{i:04d}" for i in range(MAX_CHUNKS_PER_NOTE)]


def test_a_note_exactly_at_the_cap_is_not_marked():
    """`truncated` means a chunk was *dropped*. A note that lands on the cap is
    complete, and marking it would put a permanent ERROR and an
    `embedding_truncated: true` on a note whose tail is fully searchable."""
    chunks, truncated = chunk_text_bounded(
        _prose(MAX_CHUNKS_PER_NOTE), chunk_size=1, overlap=0,
        max_chunks=MAX_CHUNKS_PER_NOTE,
    )
    assert len(chunks) == MAX_CHUNKS_PER_NOTE
    assert truncated is False


def test_one_chunk_over_the_cap_is_marked():
    chunks, truncated = chunk_text_bounded(
        _prose(MAX_CHUNKS_PER_NOTE + 1), chunk_size=1, overlap=0,
        max_chunks=MAX_CHUNKS_PER_NOTE,
    )
    assert len(chunks) == MAX_CHUNKS_PER_NOTE
    assert truncated is True


def test_a_note_under_the_cap_is_untouched():
    chunks, truncated = chunk_text_bounded(
        _prose(5), chunk_size=1, overlap=0, max_chunks=MAX_CHUNKS_PER_NOTE
    )
    assert len(chunks) == 5
    assert truncated is False


def test_the_short_circuit_keeps_its_unstripped_single_chunk():
    """The `len(content) <= char_size` branch returns the content *unstripped*
    — long-standing behaviour that the emptiness test (`strip()`) and the
    stored chunk deliberately disagree about. One chunk cannot exceed a cap."""
    chunks, truncated = chunk_text_bounded(
        "  hi  ", chunk_size=512, overlap=0, max_chunks=MAX_CHUNKS_PER_NOTE
    )
    assert chunks == ["  hi  "]
    assert truncated is False

    empty, empty_truncated = chunk_text_bounded(
        "   \n ", chunk_size=512, overlap=0, max_chunks=MAX_CHUNKS_PER_NOTE
    )
    assert empty == [] and empty_truncated is False


def test_chunk_text_delegates_and_is_bounded():
    """"This note produces no chunks" and "this note's chunks" must mean the
    same thing everywhere they are asked — `embed_note` and the exclusion
    sweep's zero-chunk probe alike."""
    body = _prose(MAX_CHUNKS_PER_NOTE + 40)
    assert len(chunk_text(body, chunk_size=1, overlap=0)) == MAX_CHUNKS_PER_NOTE
    assert chunk_text(body, chunk_size=1, overlap=0) == chunk_text_bounded(
        body, chunk_size=1, overlap=0, max_chunks=MAX_CHUNKS_PER_NOTE
    )[0]


def test_the_overlap_step_floor_survives():
    """#10's guard: `step = max(char_size - char_overlap, 1)`. `Settings`
    refuses this configuration at startup (D3b), but the floor stays — without
    it the window never advances and the loop never terminates."""
    chunks, truncated = chunk_text_bounded(
        "abcdefgh", chunk_size=1, overlap=1, max_chunks=4
    )
    assert len(chunks) == 4
    assert truncated is True


# --------------------------------------------------------------------------- #
# `embed_note` over the cap
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_a_capped_note_is_embedded_to_the_cap_and_certified(
    monkeypatch, tiny_chunks
):
    monkeypatch.setattr(embeddings, "MAX_CHUNKS_PER_NOTE", 5)
    session, note = _Session(), _Note()
    body = "".join(f"{i:04d}" for i in range(9))

    async def ok(chunks):
        return [[float(i), 1.0] for i in range(len(chunks))]

    monkeypatch.setattr(embeddings, "get_embeddings_batch", ok)

    result = await embeddings.embed_note(
        session, note, body,
        certified_hash="h-new", certified_path="Archive/Enormous.md",
    )

    assert result.outcome is NoteEmbedOutcome.EMBEDDED
    assert result.truncated is True
    assert result.chunks_submitted == result.chunks_embedded == 5
    # Exactly the cap, holding the first N chunks in document order…
    assert [row.chunk_text for row in session.added] == [
        f"{i:04d}" for i in range(5)
    ]
    assert [row.chunk_index for row in session.added] == list(range(5))
    # …and **certified**. Withholding the stamp would re-select this note on
    # every tick for ever.
    assert session.certified == ["h-new"]
    assert session.vector_deletes == 1


@pytest.mark.asyncio
async def test_a_note_at_the_cap_exactly_is_not_marked(monkeypatch, tiny_chunks):
    monkeypatch.setattr(embeddings, "MAX_CHUNKS_PER_NOTE", 5)
    session, note = _Session(), _Note()

    async def ok(chunks):
        return [[0.0, 1.0] for _ in chunks]

    monkeypatch.setattr(embeddings, "get_embeddings_batch", ok)

    result = await embeddings.embed_note(
        session, note, _prose(5),
        certified_hash="h-new", certified_path="Archive/Enormous.md",
    )

    assert result.outcome is NoteEmbedOutcome.EMBEDDED
    assert result.truncated is False
    assert result.chunks_embedded == 5
    assert session.certified == ["h-new"]


@pytest.mark.asyncio
async def test_a_note_edited_back_under_the_cap_reports_untruncated(
    monkeypatch, tiny_chunks
):
    """The marker's lifecycle is `links_truncated`'s: the caller clears it when
    a later embed of that note fits."""
    monkeypatch.setattr(embeddings, "MAX_CHUNKS_PER_NOTE", 5)

    async def ok(chunks):
        return [[0.0, 1.0] for _ in chunks]

    monkeypatch.setattr(embeddings, "get_embeddings_batch", ok)

    over = await embeddings.embed_note(
        _Session(), _Note(), _prose(12),
        certified_hash="h-new", certified_path="Archive/Enormous.md",
    )
    assert over.truncated is True

    under = await embeddings.embed_note(
        _Session(), _Note(), _prose(3),
        certified_hash="h-new", certified_path="Archive/Enormous.md",
    )
    assert under.truncated is False
    assert under.chunks_embedded == 3


@pytest.mark.asyncio
async def test_cardinality_is_exact_over_the_capped_list(monkeypatch, tiny_chunks):
    """The requested chunks *are* the capped list, so one vector short of the
    cap is still a refusal — and the note's previous vectors survive it."""
    monkeypatch.setattr(embeddings, "MAX_CHUNKS_PER_NOTE", 5)
    session, note = _Session(), _Note()

    async def one_short(chunks):
        return [[0.0, 1.0]] * (len(chunks) - 1)

    monkeypatch.setattr(embeddings, "get_embeddings_batch", one_short)

    result = await embeddings.embed_note(
        session, note, _prose(20),
        certified_hash="h-new", certified_path="Archive/Enormous.md",
    )

    assert result.outcome is NoteEmbedOutcome.PROVIDER_CARDINALITY_MISMATCH
    assert result.failure.requested == 5
    assert result.failure.received == 4
    assert result.truncated is True
    assert session.certified == []
    assert session.vector_deletes == 0
    assert session.added == []
    assert note.embedded_content_hash == "h-old"


@pytest.mark.asyncio
async def test_a_full_capped_batch_is_accepted(monkeypatch, tiny_chunks):
    monkeypatch.setattr(embeddings, "MAX_CHUNKS_PER_NOTE", 5)
    session, note = _Session(), _Note()

    async def exact(chunks):
        return [[0.0, 1.0] for _ in chunks]

    monkeypatch.setattr(embeddings, "get_embeddings_batch", exact)

    result = await embeddings.embed_note(
        session, note, _prose(50),
        certified_hash="h-new", certified_path="Archive/Enormous.md",
    )

    assert result.outcome is NoteEmbedOutcome.EMBEDDED
    assert len(session.added) == 5
    assert session.certified == ["h-new"]


@pytest.mark.asyncio
async def test_the_cap_logs_nothing_here(monkeypatch, tiny_chunks, caplog):
    """The truncation ERROR belongs to the caller, **after** the certifying
    transaction commits. Logging it here would leave a permanent ERROR in the
    bounded, process-lifetime ops buffer for a write that then rolled back on a
    `StaleCertification` — an operator chasing a note that was never stored
    that way."""
    monkeypatch.setattr(embeddings, "MAX_CHUNKS_PER_NOTE", 3)

    async def ok(chunks):
        return [[0.0, 1.0] for _ in chunks]

    monkeypatch.setattr(embeddings, "get_embeddings_batch", ok)

    with caplog.at_level("ERROR", logger="src.services.embeddings"):
        result = await embeddings.embed_note(
            _Session(), _Note(), _prose(30),
            certified_hash="h-new", certified_path="Archive/Enormous.md",
        )

    assert result.truncated is True
    assert caplog.records == []
