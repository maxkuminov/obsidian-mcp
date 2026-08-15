"""Opt-in: the indexer's HNSW pre-warm probe really walks the HNSW index.

The probe exists to keep the index's hot pages in `shared_buffers` between
sparse searches — ~3 s of a cold `semantic_search` is HNSW pages missing from a
128 MB buffer cache shared with another tenant. It only does that if its plan is
an *index* scan. A sequential scan over `note_embeddings` would warm the heap
instead, which is not what goes cold, and it would look identical in the logs:
same INFO line, same few milliseconds, no failure anywhere.

Nothing offline can catch that. `tests/test_search_prewarm.py` pins the
control flow against a fake session — the settings issued, the skip when no
index exists, the timeout, the cancellation — but a fake session has no
planner. So this module seeds a corpus big enough for the planner to prefer the
index at all, and `EXPLAIN`s the statement production issues
(`indexer.probe_statement()`) under the setting production issues
(`indexer.PROBE_PLANNER_SETTING`).

The skip path is asserted the same way, against the same database: drop the
index and the probe must return `None` rather than fall back to a sequential
scan every five minutes. That is the `EMBEDDING_DIMENSIONS > 2000` deployment,
where pgvector cannot build an HNSW index at all.

Skipped unless `PGVECTOR_TEST_ADMIN_URL` is set — see `_harness.py`.
"""
import math
import random

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import _harness
from src.config import settings
from src.models.db import NoteEmbedding, NoteMetadata
from src.services import indexer

pytestmark = [
    _harness.requires_pgvector,
    pytest.mark.asyncio(loop_scope="module"),
]

# Must match the ORM column width: `_probe_vector()` builds a vector of
# `settings.embedding_dimensions`, so a narrower column would make the probe
# itself fail rather than plan badly, and the test would pass for the wrong
# reason.
DIM = int(settings.embedding_dimensions)

SEED = 8765
N_NOTES = 1500
CHUNKS_PER_NOTE = 2  # 3,000 vectors — enough for the planner to prefer HNSW.
HNSW_INDEX = "ix_note_embeddings_embedding_hnsw"


def _random_unit(rng: random.Random) -> list[float]:
    vec = [rng.gauss(0.0, 1.0) for _ in range(DIM)]
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


@pytest.fixture(scope="module")
def migrated_url():
    yield from _harness.throwaway_database("prewarm_probe", DIM)


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def sessionmaker(migrated_url):
    engine = create_async_engine(migrated_url, poolclass=None)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def corpus(sessionmaker):
    """A few thousand vectors, then the index — built after the bulk load,
    single-threaded, with the same `m`/`ef_construction` production uses."""
    rng = random.Random(SEED)
    async with sessionmaker() as session:
        await session.execute(text(f"DROP INDEX IF EXISTS {HNSW_INDEX}"))
        notes = [
            NoteMetadata(
                file_path=f"P/note-{i:04d}.md",
                title=f"note-{i:04d}",
                tags=[],
                frontmatter={},
                content_hash=f"P/note-{i:04d}.md",
            )
            for i in range(N_NOTES)
        ]
        session.add_all(notes)
        await session.flush()
        for note in notes:
            for ci in range(CHUNKS_PER_NOTE):
                session.add(NoteEmbedding(
                    note_id=note.id, chunk_index=ci,
                    chunk_text=f"{note.file_path} chunk {ci}",
                    embedding=_random_unit(rng),
                ))
        await session.commit()

        await session.execute(text("SET LOCAL max_parallel_maintenance_workers = 0"))
        await session.execute(text(
            f"CREATE INDEX {HNSW_INDEX} ON note_embeddings "
            "USING hnsw (embedding vector_cosine_ops) "
            "WITH (m = 16, ef_construction = 64)"
        ))
        await session.execute(text("ANALYZE note_embeddings"))
        await session.commit()

    # `_prewarm_once` opens its own session from the module-global maker, and
    # caches the `pg_indexes` lookup across ticks.
    original = indexer.async_session
    indexer.async_session = sessionmaker
    indexer.invalidate_hnsw_index_cache()
    yield
    indexer.async_session = original
    indexer.invalidate_hnsw_index_cache()


async def _explain_probe(sessionmaker) -> str:
    async with sessionmaker() as session:
        await session.execute(text(indexer.PROBE_PLANNER_SETTING))
        return await _harness.explain(session, indexer.probe_statement())


async def test_probe_statement_uses_the_hnsw_index(sessionmaker, corpus):
    plan = await _explain_probe(sessionmaker)
    assert HNSW_INDEX in plan, plan


async def test_probe_runs_and_reports_a_duration(sessionmaker, corpus, monkeypatch):
    """The statement is not merely plannable — it executes against real data.

    The embedding half is skipped by pointing the provider at OpenAI: a local
    Ollama is not part of this harness, and the probe is what is under test.
    """
    monkeypatch.setattr(settings, "embedding_provider", "openai", raising=False)
    embed_ms, probe_ms = await indexer._prewarm_once()
    assert embed_ms is None
    assert probe_ms is not None and probe_ms >= 0


async def test_probe_is_skipped_when_no_hnsw_index_exists(
    sessionmaker, corpus, monkeypatch, caplog
):
    """Deployments above pgvector's 2000-dim HNSW limit have no index. The
    probe must skip, not silently seq-scan the table every five minutes."""
    monkeypatch.setattr(settings, "embedding_provider", "openai", raising=False)
    async with sessionmaker() as session:
        await session.execute(text(f"DROP INDEX {HNSW_INDEX}"))
        await session.commit()
    indexer.invalidate_hnsw_index_cache()
    try:
        with caplog.at_level("INFO"):
            embed_ms, probe_ms = await indexer._prewarm_once()
        assert probe_ms is None
        assert embed_ms is None
        assert "HNSW probe skipped" in caplog.text

        # And the planner agrees there is nothing to walk: without the index
        # the same statement can only seq-scan, which is precisely the work the
        # skip avoids.
        plan = await _explain_probe(sessionmaker)
        assert HNSW_INDEX not in plan, plan
    finally:
        async with sessionmaker() as session:
            await session.execute(
                text("SET LOCAL max_parallel_maintenance_workers = 0")
            )
            await session.execute(text(
                f"CREATE INDEX {HNSW_INDEX} ON note_embeddings "
                "USING hnsw (embedding vector_cosine_ops) "
                "WITH (m = 16, ef_construction = 64)"
            ))
            await session.commit()
        indexer.invalidate_hnsw_index_cache()
