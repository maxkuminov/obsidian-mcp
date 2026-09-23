"""#281 / D16 — chunk-vector reuse, on a real PostgreSQL.

A stored vector is reused for a chunk whose text is byte-identical to a stored
chunk of the note, only while the stored embedding fingerprint is present and
equal to this process's, and only if every reused row still exists under the
generation lock. These tests drive `embed_note` against two connections so a
reset on the second can commit while the first is mid-attempt — the
interleaving the under-lock check exists for, and the one a mocked session
cannot show.

Skipped unless `PGVECTOR_TEST_ADMIN_URL` is set — see `_harness.py`.
"""
import asyncio
import hashlib
import json

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.config import settings
from src.models.db import NoteMetadata
from src.services import embeddings as embeddings_service
from src.services import index_state, indexer
from src.services.embeddings import NoteEmbedOutcome
from src.services.index_state import KEY_EMBEDDING_FINGERPRINT
import _harness

pytestmark = [
    _harness.requires_pgvector,
    pytest.mark.asyncio(loop_scope="module"),
]

#: The model's width. Multi-row ORM inserts cast to the model's
#: `Vector(settings.embedding_dimensions)`, so the column must match it.
DIM = int(settings.embedding_dimensions)
#: 4 "tokens" → 16-character windows, so a short body yields several chunks.
CHUNK_SIZE = 4

BODY = (
    "alpha alpha alph"
    "bravo bravo brav"
    "charlie charlie "
    "delta delta delt"
)
TAIL = "echo echo echo e" "foxtrot foxtrot "
APPENDED = BODY + TAIL


def content_hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def vector_of(chunk: str) -> list[float]:
    """A deterministic 'model': the same text always gets the same vector."""
    out: list[float] = []
    block = chunk.encode("utf-8")
    while len(out) < DIM:
        block = hashlib.sha256(block).digest()
        out.extend(b / 255.0 for b in block)
    return out[:DIM]


def chunks_of(body: str) -> list[str]:
    cleaned = embeddings_service.clean_for_embedding(body)
    chunks, _ = embeddings_service.chunk_text_bounded(
        cleaned, chunk_size=CHUNK_SIZE, overlap=0
    )
    return chunks


def _other_fingerprint() -> str:
    payload = json.loads(index_state.embedding_fingerprint())
    payload["model"] = payload["model"] + "-other"
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


@pytest.fixture(scope="module")
def migrated_url():
    yield from _harness.throwaway_database("chunk_reuse_281", DIM)


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def engines(migrated_url):
    """Two independent engines: the embedding pass, and the one-off reset."""
    a = create_async_engine(migrated_url, poolclass=None)
    b = create_async_engine(migrated_url, poolclass=None)
    yield (
        async_sessionmaker(a, class_=AsyncSession, expire_on_commit=False),
        async_sessionmaker(b, class_=AsyncSession, expire_on_commit=False),
    )
    await a.dispose()
    await b.dispose()


class _Provider:
    """Counts what is sent; answers with the deterministic vectors."""

    def __init__(self, during=None):
        self.sent: list[list[str]] = []
        self._during = during

    async def __call__(self, chunks):
        self.sent.append(list(chunks))
        if self._during is not None:
            await self._during()
        return [vector_of(c) for c in chunks]


@pytest_asyncio.fixture(loop_scope="module")
async def world(engines, monkeypatch):
    maker_a, _ = engines
    monkeypatch.setattr(settings, "chunk_size", CHUNK_SIZE, raising=False)
    monkeypatch.setattr(settings, "chunk_overlap", 0, raising=False)
    monkeypatch.setattr(settings, "embedding_provider", "ollama", raising=False)

    async with maker_a() as session:
        await session.execute(text("DELETE FROM note_embeddings"))
        await session.execute(text("DELETE FROM notes_metadata"))
        await session.execute(text("DELETE FROM indexer_state"))
        note_id = (await session.execute(text(
            "INSERT INTO notes_metadata (user_id, file_path, title, "
            "content_hash, file_size, modified_at, indexed_at) VALUES "
            "(NULL, 'Note.md', 'Note', :h, 10, now(), now()) RETURNING id"
        ), {"h": content_hash(BODY)})).scalar()
        # Rendered after the chunk-size patch: chunk size is in it.
        await index_state.set_state(
            session, KEY_EMBEDDING_FINGERPRINT, index_state.embedding_fingerprint()
        )
        await session.commit()

    # The note's first generation of vectors, embedded from scratch.
    first = _Provider()
    monkeypatch.setattr(embeddings_service, "get_embeddings_batch", first)
    result = await _embed(maker_a, note_id, BODY)
    assert result.outcome is NoteEmbedOutcome.EMBEDDED
    assert first.sent == [chunks_of(BODY)]
    return {"note_id": note_id}


async def _set_hash(maker, note_id, body):
    async with maker() as session:
        await session.execute(
            text("UPDATE notes_metadata SET content_hash = :h WHERE id = :i"),
            {"h": content_hash(body), "i": note_id},
        )
        await session.commit()


async def _embed(maker, note_id, body, accounting=None):
    async with maker() as session:
        note = (await session.execute(
            select(NoteMetadata).where(NoteMetadata.id == note_id)
        )).scalar_one()
        # The production callers end their read transaction before the call.
        await session.commit()
        result = await embeddings_service.embed_note(
            session, note, body,
            certified_hash=content_hash(body), certified_path="Note.md",
            on_provider_call=accounting.issued if accounting else None,
        )
        if accounting is not None:
            accounting.reconcile(result)
        if result.outcome is NoteEmbedOutcome.EMBEDDED:
            await session.commit()
        else:
            await session.rollback()
    return result


async def _rows(maker, note_id):
    async with maker() as session:
        rows = (await session.execute(text(
            "SELECT id, chunk_index, chunk_text, embedding::text FROM "
            "note_embeddings WHERE note_id = :i ORDER BY chunk_index"
        ), {"i": note_id})).all()
    return [
        (r[0], r[1], r[2], [float(x) for x in r[3].strip("[]").split(",")])
        for r in rows
    ]


async def _stamp(maker, note_id):
    async with maker() as session:
        return (await session.execute(text(
            "SELECT embedded_content_hash FROM notes_metadata WHERE id = :i"
        ), {"i": note_id})).scalar()


def _accounting():
    outcome = indexer.EmbedPassResult()
    budget = indexer.EmbedBudget()
    acc = indexer._ProviderCallAccounting(outcome, budget)
    acc.begin()
    return acc, outcome, budget


def _assert_vectors(rows, expected_chunks):
    assert [r[1] for r in rows] == list(range(len(expected_chunks)))
    assert [r[2] for r in rows] == expected_chunks
    for _id, _idx, chunk, vec in rows:
        assert vec == pytest.approx(vector_of(chunk), abs=1e-6)


async def _reset(maker_b, *, fingerprint=None):
    """The reset driver: `scripts/reset_embeddings.py`'s statements, in its
    order, on its own connection. The `ALTER TABLE … TYPE` needs
    `note_embeddings` exclusively, so it cannot complete while any other
    transaction still holds even an `AccessShareLock` on the table."""
    async with maker_b() as sb:
        await index_state.acquire_generation_lock_unbounded(sb)
        await sb.execute(text("DELETE FROM note_embeddings"))
        await sb.execute(text(
            f"ALTER TABLE note_embeddings ALTER COLUMN embedding TYPE vector({DIM})"
        ))
        await sb.execute(text("UPDATE notes_metadata SET embedded_content_hash = NULL"))
        await index_state.set_state(
            sb, KEY_EMBEDDING_FINGERPRINT,
            fingerprint or index_state.embedding_fingerprint(),
        )
        await sb.commit()


# ══════════════════════════════════════════════════════════════════════════


async def test_an_append_re_embeds_only_the_new_tail(engines, world, monkeypatch):
    maker_a, _ = engines
    note_id = world["note_id"]
    old_chunks = chunks_of(BODY)
    new_chunks = chunks_of(APPENDED)
    tail = [c for c in new_chunks if c not in set(old_chunks)]
    assert tail and len(tail) < len(new_chunks)

    provider = _Provider()
    monkeypatch.setattr(embeddings_service, "get_embeddings_batch", provider)
    await _set_hash(maker_a, note_id, APPENDED)
    acc, outcome, budget = _accounting()

    result = await _embed(maker_a, note_id, APPENDED, acc)

    assert result.outcome is NoteEmbedOutcome.EMBEDDED
    assert provider.sent == [tail], "only the new tail chunks go to the provider"
    assert result.chunks_submitted == len(tail)
    assert result.chunks_embedded == len(new_chunks)
    # The budget is debited by the subset only, and it was one attempt.
    assert budget.chunks_submitted == len(tail)
    assert outcome.attempted == 1
    # Equal to a from-scratch embed: every chunk, in order, with its vector.
    _assert_vectors(await _rows(maker_a, note_id), new_chunks)
    assert await _stamp(maker_a, note_id) == content_hash(APPENDED)


async def test_a_metadata_only_edit_makes_no_provider_call(
    engines, world, monkeypatch
):
    """Same body chunks under a new content hash (a frontmatter edit): no
    call, certified, `attempted` unchanged, nothing debited."""
    maker_a, _ = engines
    note_id = world["note_id"]
    before = await _rows(maker_a, note_id)
    provider = _Provider()
    monkeypatch.setattr(embeddings_service, "get_embeddings_batch", provider)
    # A different hash for the same body text: `embed_note` sees the body.
    async with maker_a() as session:
        await session.execute(
            text("UPDATE notes_metadata SET content_hash = 'meta-edit' WHERE id = :i"),
            {"i": note_id},
        )
        await session.commit()
    acc, outcome, budget = _accounting()

    async with maker_a() as session:
        note = (await session.execute(
            select(NoteMetadata).where(NoteMetadata.id == note_id)
        )).scalar_one()
        await session.commit()
        result = await embeddings_service.embed_note(
            session, note, BODY,
            certified_hash="meta-edit", certified_path="Note.md",
            on_provider_call=acc.issued,
        )
        acc.reconcile(result)
        await session.commit()

    assert result.outcome is NoteEmbedOutcome.EMBEDDED
    assert provider.sent == []
    assert result.chunks_submitted == 0
    assert outcome.attempted == 0 and budget.chunks_submitted == 0
    after = await _rows(maker_a, note_id)
    _assert_vectors(after, chunks_of(BODY))
    assert [r[2:] for r in after] == [r[2:] for r in before]
    assert await _stamp(maker_a, note_id) == "meta-edit"


async def test_an_absent_fingerprint_disables_reuse(engines, world, monkeypatch):
    maker_a, _ = engines
    note_id = world["note_id"]
    async with maker_a() as session:
        await session.execute(text("DELETE FROM indexer_state"))
        await session.commit()
    provider = _Provider()
    monkeypatch.setattr(embeddings_service, "get_embeddings_batch", provider)
    await _set_hash(maker_a, note_id, APPENDED)

    result = await _embed(maker_a, note_id, APPENDED)

    # Absent is not a mismatch, so the note certifies — with every chunk sent.
    assert result.outcome is NoteEmbedOutcome.EMBEDDED
    assert provider.sent == [chunks_of(APPENDED)]
    _assert_vectors(await _rows(maker_a, note_id), chunks_of(APPENDED))


async def test_a_differing_fingerprint_disables_reuse_and_certifies_nothing(
    engines, world, monkeypatch
):
    maker_a, _ = engines
    note_id = world["note_id"]
    async with maker_a() as session:
        await index_state.set_state(
            session, KEY_EMBEDDING_FINGERPRINT, _other_fingerprint()
        )
        await session.commit()
    before = await _rows(maker_a, note_id)
    provider = _Provider()
    monkeypatch.setattr(embeddings_service, "get_embeddings_batch", provider)
    await _set_hash(maker_a, note_id, APPENDED)

    result = await _embed(maker_a, note_id, APPENDED)

    assert provider.sent == [chunks_of(APPENDED)], "a stored vector was reused"
    assert result.outcome is NoteEmbedOutcome.GENERATION_MISMATCH
    assert await _rows(maker_a, note_id) == before
    assert await _stamp(maker_a, note_id) == content_hash(BODY)


async def test_a_reset_during_the_provider_call_defeats_reuse(
    engines, world, monkeypatch
):
    """Partial reuse; a reset (same fingerprint — the L1 case the fingerprint
    cannot see) commits while the tail's provider call is in flight. The
    lookup's transaction has ended, so the reset's `ALTER TABLE` is not
    blocked (no deadlock); the reused rows are gone under the lock, so the
    attempt writes nothing and reports the mismatch."""
    maker_a, maker_b = engines
    note_id = world["note_id"]

    async def _reset_mid_call():
        await asyncio.wait_for(_reset(maker_b), timeout=15)

    provider = _Provider(during=_reset_mid_call)
    monkeypatch.setattr(embeddings_service, "get_embeddings_batch", provider)
    await _set_hash(maker_a, note_id, APPENDED)
    acc, outcome, budget = _accounting()

    result = await asyncio.wait_for(
        _embed(maker_a, note_id, APPENDED, acc), timeout=30
    )

    assert len(provider.sent) == 1 and len(provider.sent[0]) < len(
        chunks_of(APPENDED)
    ), "the attempt was expected to reuse part of the note"
    assert result.outcome is NoteEmbedOutcome.GENERATION_MISMATCH
    assert result.chunks_embedded == 0
    # A provider call was issued, so it is an attempt.
    assert outcome.attempted == 1
    assert budget.chunks_submitted == len(provider.sent[0])
    # Nothing written: the reset's empty table and NULL stamp stand.
    assert await _rows(maker_a, note_id) == []
    assert await _stamp(maker_a, note_id) is None


async def test_an_all_reuse_note_loses_a_reset_race_without_an_attempt(
    engines, world, monkeypatch
):
    """Every chunk reusable; the reset commits between the lookup and the
    under-lock check. No provider call, so no attempt and no debit; the
    outcome is the mismatch and nothing is written."""
    maker_a, maker_b = engines
    note_id = world["note_id"]
    provider = _Provider()
    monkeypatch.setattr(embeddings_service, "get_embeddings_batch", provider)
    real = embeddings_service._generation_matches
    seen = {}

    async def _reset_then_check(session, note, reused_rows=None):
        seen["reused"] = dict(reused_rows or {})
        await asyncio.wait_for(_reset(maker_b), timeout=15)
        return await real(session, note, reused_rows)

    monkeypatch.setattr(
        embeddings_service, "_generation_matches", _reset_then_check
    )
    await _set_hash(maker_a, note_id, BODY + "\n")  # same chunks, new hash
    acc, outcome, budget = _accounting()

    result = await asyncio.wait_for(
        _embed(maker_a, note_id, BODY + "\n", acc), timeout=30
    )

    assert provider.sent == []
    assert len(seen["reused"]) == len(chunks_of(BODY))
    assert result.outcome is NoteEmbedOutcome.GENERATION_MISMATCH
    assert result.chunks_submitted == 0
    assert outcome.attempted == 0 and budget.chunks_submitted == 0
    assert await _rows(maker_a, note_id) == []
    assert await _stamp(maker_a, note_id) is None


async def test_reuse_is_the_ordinary_path_after_a_reset_re_embeds(
    engines, world, monkeypatch
):
    """After a reset nothing is reusable: the next embed sends every chunk,
    and the one after that reuses them again."""
    maker_a, maker_b = engines
    note_id = world["note_id"]
    await _reset(maker_b)
    provider = _Provider()
    monkeypatch.setattr(embeddings_service, "get_embeddings_batch", provider)

    first = await _embed(maker_a, note_id, BODY)
    assert first.outcome is NoteEmbedOutcome.EMBEDDED
    assert provider.sent == [chunks_of(BODY)]

    await _set_hash(maker_a, note_id, APPENDED)
    second = await _embed(maker_a, note_id, APPENDED)
    assert second.outcome is NoteEmbedOutcome.EMBEDDED
    assert provider.sent[1] == [
        c for c in chunks_of(APPENDED) if c not in set(chunks_of(BODY))
    ]
    _assert_vectors(await _rows(maker_a, note_id), chunks_of(APPENDED))
