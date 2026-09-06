"""#206 / D7c — the generation lock, on the interleaving it exists for.

A per-pass fingerprint re-read is **not sufficient**, and the shape that
defeats it is ordinary rather than exotic:

    old process: read fingerprint A == A, proceed
    old process: get_embeddings_batch(note)     <- seconds to minutes
    reset:       wipe column, write fingerprint B, commit
    old process: certify_embedded + insert      <- old-model vectors under B

`make reset-embeddings` runs as a one-off container on purpose (#142) so it can
read the edited `.env` while the service is up, which is exactly what makes the
window reachable. The check and the act are separated by a network call, so the
enforcement is a transaction-scoped advisory lock taken **after** the provider
call and **before** the certification, with the fingerprint re-read under it.

Two connections and a real PostgreSQL, because a single-connection or mocked
version of this test cannot show one transaction's commit becoming visible to
another mid-call — which is the entire mechanism.

Skipped unless `PGVECTOR_TEST_ADMIN_URL` is set — see `_harness.py`.
"""
import hashlib
import inspect
import json

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import src.database
from src.config import settings
from src.models.db import NoteEmbedding, NoteMetadata
from src.services import embeddings as embeddings_service
from src.services import index_state, indexer
from src.services.index_state import (
    INDEX_GENERATION_LOCK_KEY,
    KEY_EMBEDDING_FINGERPRINT,
    KEY_FTS_FINGERPRINT,
)
import _harness

pytestmark = [
    _harness.requires_pgvector,
    pytest.mark.asyncio(loop_scope="module"),
]

DIM = 8
BODY = "a body with enough words in it to produce exactly one chunk\n"
OTHER_MODEL = "another-model-of-the-same-width"


def content_hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _fingerprint_with_model(model: str) -> str:
    """The fingerprint this build would write under a different model name.

    Built by swapping the field rather than by mutating settings inside a
    running pass, so the "reset" half of the interleaving is a plain stored
    value — which is all the old container can see of it.
    """
    payload = json.loads(index_state.embedding_fingerprint())
    payload["model"] = model
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


@pytest.fixture(scope="module")
def migrated_url():
    yield from _harness.throwaway_database("generation_lock_206", DIM)


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def engines(migrated_url):
    """Two independent engines: the old container, and the one-off reset."""
    a = create_async_engine(migrated_url, poolclass=None)
    b = create_async_engine(migrated_url, poolclass=None)
    yield (
        async_sessionmaker(a, class_=AsyncSession, expire_on_commit=False),
        async_sessionmaker(b, class_=AsyncSession, expire_on_commit=False),
    )
    await a.dispose()
    await b.dispose()


@pytest_asyncio.fixture(loop_scope="module")
async def world(engines, monkeypatch, tmp_path):
    maker_a, _maker_b = engines
    root = tmp_path / "vault"
    root.mkdir(exist_ok=True)
    (root / "Note.md").write_text(BODY, encoding="utf-8")

    monkeypatch.setattr(settings, "vault_path", str(root), raising=False)
    monkeypatch.setattr(indexer.settings, "vault_path", str(root), raising=False)
    monkeypatch.setattr(indexer.settings, "multi_user_mode", False, raising=False)
    monkeypatch.setattr(indexer, "async_session", maker_a)
    monkeypatch.setattr(src.database, "async_session", maker_a)
    monkeypatch.setattr(indexer, "_is_paused", lambda: False)
    monkeypatch.setattr(
        indexer, "_refuse_quarantined_pass", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        settings, "embedding_provider", "ollama", raising=False
    )

    async with maker_a() as session:
        await session.execute(text("DELETE FROM note_embeddings"))
        await session.execute(text("DELETE FROM notes_metadata"))
        await session.execute(text("DELETE FROM indexer_state"))
        row = (await session.execute(text(
            "INSERT INTO notes_metadata (user_id, file_path, title, "
            "content_hash, embedded_content_hash, file_size, modified_at, "
            "indexed_at) VALUES (NULL, 'Note.md', 'Note', :h, :h, 10, now(), "
            "now()) RETURNING id"
        ), {"h": content_hash(BODY)})).scalar()
        await session.execute(text(
            "INSERT INTO note_embeddings (note_id, chunk_index, chunk_text, "
            "embedding) VALUES (:id, 0, 'the previous generation', :v)"
        ), {"id": row, "v": str([0.5] * DIM)})
        await index_state.set_state(
            session, KEY_EMBEDDING_FINGERPRINT,
            index_state.embedding_fingerprint(),
        )
        await session.commit()
    return {"root": root, "note_id": row}


async def _vectors(maker):
    async with maker() as session:
        return (await session.execute(text(
            "SELECT chunk_text FROM note_embeddings ORDER BY chunk_index"
        ))).scalars().all()


# ══════════════════════════════════════════════════════════════════════════
# The embedding path
# ══════════════════════════════════════════════════════════════════════════


async def test_a_reset_committing_during_the_provider_call_is_not_overwritten(
    engines, world, monkeypatch
):
    """The whole reason the lock exists.

    A reads a matching fingerprint, issues its provider call, the one-off reset
    commits a new fingerprint while that call is in flight, and A then reaches
    its certification. Under the lock A re-reads and refuses: it certifies
    nothing, inserts nothing and deletes nothing, which is the disposition a
    failed certification already has.
    """
    maker_a, maker_b = engines
    config_b = _fingerprint_with_model(OTHER_MODEL)
    observed = {}

    async def _provider_call(chunks):
        # The reset lands here, on its own connection, mid-call.
        async with maker_b() as sb:
            observed["lock_free"] = (await sb.execute(
                text("SELECT pg_try_advisory_xact_lock(:k)"),
                {"k": INDEX_GENERATION_LOCK_KEY},
            )).scalar()
            await index_state.set_state(sb, KEY_EMBEDDING_FINGERPRINT, config_b)
            await sb.commit()
        return [[0.1] * DIM for _ in chunks]

    monkeypatch.setattr(
        embeddings_service, "get_embeddings_batch", _provider_call
    )

    async with maker_a() as sa:
        note = (await sa.execute(
            select(NoteMetadata).where(NoteMetadata.id == world["note_id"])
        )).scalar_one()
        result = await embeddings_service.embed_note(
            sa, note, BODY,
            certified_hash=content_hash(BODY), certified_path="Note.md",
        )
        await sa.rollback()

    assert observed["lock_free"] is True, (
        "the generation lock was held across the provider call"
    )
    assert result.outcome is embeddings_service.NoteEmbedOutcome.GENERATION_MISMATCH
    assert result.chunks_embedded == 0

    # Nothing inserted, nothing deleted, the stamp untouched.
    assert await _vectors(maker_a) == ["the previous generation"]
    async with maker_a() as session:
        stamp = (await session.execute(text(
            "SELECT embedded_content_hash FROM notes_metadata WHERE id = :i"
        ), {"i": world["note_id"]})).scalar()
    assert stamp == content_hash(BODY)


async def test_a_later_pass_under_the_new_configuration_embeds_the_note(
    engines, world, monkeypatch
):
    """The refusal is a deferral, not a hole.

    The reset left the stored fingerprint naming the new model. A process
    actually running that model matches it and embeds normally, so the note the
    interlock refused is picked up by the next pass rather than stranded.
    """
    maker_a, _maker_b = engines
    async with maker_a() as session:
        await index_state.set_state(
            session, KEY_EMBEDDING_FINGERPRINT,
            _fingerprint_with_model(OTHER_MODEL),
        )
        await session.commit()
    monkeypatch.setattr(
        settings, "embedding_model", OTHER_MODEL, raising=False
    )
    monkeypatch.setattr(
        embeddings_service.settings, "embedding_model", OTHER_MODEL,
        raising=False,
    )

    async def _provider_call(chunks):
        return [[0.2] * DIM for _ in chunks]

    monkeypatch.setattr(
        embeddings_service, "get_embeddings_batch", _provider_call
    )

    async with maker_a() as sa:
        note = (await sa.execute(
            select(NoteMetadata).where(NoteMetadata.id == world["note_id"])
        )).scalar_one()
        result = await embeddings_service.embed_note(
            sa, note, BODY,
            certified_hash=content_hash(BODY), certified_path="Note.md",
        )
        await sa.commit()

    assert result.outcome is embeddings_service.NoteEmbedOutcome.EMBEDDED
    assert await _vectors(maker_a) != ["the previous generation"]


async def test_the_exclusion_branch_certifies_without_the_lock(
    engines, world, monkeypatch
):
    """Exempt by argument, not by omission.

    It issues no provider call, writes no vector, and stamps a row to record
    that an *excluded* note has been dealt with — a claim true under every
    configuration, because the correct vector set for an excluded note is the
    empty one. So it has nothing a generation change can invalidate, and it
    must keep working while the interlock is refusing everything else.
    """
    maker_a, _maker_b = engines

    async def _always_stale(*_a, **_k):
        return False

    monkeypatch.setattr(
        embeddings_service, "_generation_matches", _always_stale
    )
    monkeypatch.setattr(
        indexer.settings, "embedding_exclude_patterns", ["Note.md"],
        raising=False,
    )
    # A matching stored fingerprint, so the stage-head early exit lets the pass
    # reach the exclusion branch — the interlock is what is stubbed here.
    async with maker_a() as session:
        await index_state.set_state(
            session, KEY_EMBEDDING_FINGERPRINT,
            index_state.embedding_fingerprint(),
        )
        await session.execute(text(
            "UPDATE notes_metadata SET embedded_content_hash = NULL"
        ))
        await session.commit()

    await indexer.embed_vault(user_id=None)

    async with maker_a() as session:
        stamp = (await session.execute(text(
            "SELECT embedded_content_hash FROM notes_metadata WHERE id = :i"
        ), {"i": world["note_id"]})).scalar()
        left = (await session.execute(
            select(NoteEmbedding).where(
                NoteEmbedding.note_id == world["note_id"]
            )
        )).all()
    assert stamp == content_hash(BODY), (
        "the exclusion branch was blocked by an interlock it is exempt from"
    )
    assert left == [], "the excluded note kept its vectors"


# ══════════════════════════════════════════════════════════════════════════
# The keyword path
# ══════════════════════════════════════════════════════════════════════════


async def test_an_old_config_keyword_write_is_refused_after_a_rebuild(
    engines, world, monkeypatch
):
    """The mirror, one step removed.

    A keyword vector is rewritten only when a note's `content_hash` changes, so
    a row an old-configuration pass writes after the rebuild stays on the
    previous configuration **indefinitely**, behind a fingerprint claiming
    otherwise. The refusal aborts that pass with nothing committed, as a
    tsvector floor failure already does.
    """
    maker_a, maker_b = engines

    # The rebuild driver commits every retained scope and the fingerprint.
    async with maker_b() as sb:
        await sb.execute(text("DELETE FROM notes_metadata"))
        await sb.execute(text(
            "INSERT INTO notes_metadata (user_id, file_path, title, "
            "content_hash, file_size, modified_at, indexed_at) VALUES "
            "(NULL, 'Note.md', 'Note', :h, 10, now(), now())"
        ), {"h": content_hash(BODY)})
        await sb.commit()
    async with maker_b() as sb:
        outcomes = await indexer.rebuild_tsvectors_all_scopes(sb)
    assert outcomes[None].completed

    # A process still running the previous FTS_CONFIGS now runs its pass.
    monkeypatch.setattr(
        indexer.settings, "fts_configs", ["english", "simple"], raising=False
    )
    monkeypatch.setattr(
        settings, "fts_configs", ["english", "simple"], raising=False
    )
    (world["root"] / "New.md").write_text("a brand new note\n", encoding="utf-8")
    try:
        with pytest.raises(indexer.GenerationMismatch) as excinfo:
            await indexer.index_vault(user_id=None)
        assert "rebuild-tsvectors" in str(excinfo.value)

        async with maker_a() as session:
            paths = (await session.execute(text(
                "SELECT file_path FROM notes_metadata ORDER BY file_path"
            ))).scalars().all()
        assert paths == ["Note.md"], (
            "the aborted pass committed part of its transaction"
        )
    finally:
        (world["root"] / "New.md").unlink()


async def test_the_rebuild_takes_the_lock_before_it_reads(engines, world):
    """Asserted structurally, because the ordering is the property.

    The driver must hold the lock before the first row it intends to rebuild is
    read — not merely before its fingerprint write — so that no keyword-vector
    write by any process can commit between the snapshot and the record.
    """
    # The docstring names the same statements, so it is stripped before the
    # ordering is read off the body.
    body = inspect.getsource(indexer.rebuild_tsvectors_all_scopes).split(
        '"""'
    )[2]
    lock_at = body.index("acquire_generation_lock(session)")
    read_at = body.index("SELECT DISTINCT user_id")
    write_at = body.index("set_state(session, KEY_FTS_FINGERPRINT")
    assert lock_at < read_at < write_at


# ══════════════════════════════════════════════════════════════════════════
# The lock itself
# ══════════════════════════════════════════════════════════════════════════


async def test_a_rolled_back_holder_releases_the_lock(engines):
    """Transaction-scoped, never session-scoped.

    A session lock leaked into a pooled connection would be held by whatever
    ran next, and a crashed holder would strand it with no operator recourse.
    """
    maker_a, maker_b = engines
    async with maker_a() as sa:
        await index_state.acquire_generation_lock(sa)
        async with maker_b() as sb:
            held = (await sb.execute(
                text("SELECT pg_try_advisory_xact_lock(:k)"),
                {"k": INDEX_GENERATION_LOCK_KEY},
            )).scalar()
            await sb.rollback()
        assert held is False, "the lock did not exclude a second connection"
        await sa.rollback()

    async with maker_b() as sb:
        after = (await sb.execute(
            text("SELECT pg_try_advisory_xact_lock(:k)"),
            {"k": INDEX_GENERATION_LOCK_KEY},
        )).scalar()
        await sb.rollback()
    assert after is True, "a rolled-back holder stranded the lock"


async def test_the_key_is_one_declared_constant():
    """Not derived at runtime from anything that could differ between builds.

    Two processes holding different keys are two processes holding no lock at
    all, with a failure mode that is silent and permanent.
    """
    source = inspect.getsource(index_state)
    assert "INDEX_GENERATION_LOCK_KEY = 8029183045093649969" in source
    assert index_state.INDEX_GENERATION_LOCK_KEY == 8029183045093649969
    # And both fingerprints are guarded by the same key, so the ordering rule
    # is trivially total.
    assert KEY_EMBEDDING_FINGERPRINT != KEY_FTS_FINGERPRINT
