"""Opt-in integration test: the vector query paths against a real pgvector.

pgvector 0.5.0 stopped requiring NumPy and now returns SQLAlchemy vector
columns as **plain Python lists** instead of `numpy.ndarray`. Both vector query
paths post-process those rows with NumPy (`semantic_search` computes a cosine
similarity per row, `find_related` averages a note's chunk vectors and then
scores candidates), so the shape change is exactly the kind of thing a unit
test with fake rows would miss.

Skipped unless `TEST_DATABASE_URL` is set. Never point it at a real database:
the test runs migrations and truncates the note tables.

    docker run --rm -d --name pgvector-test -e POSTGRES_PASSWORD=test \\
        -p 55432:5432 pgvector/pgvector:pg16
    TEST_DATABASE_URL=postgresql+asyncpg://postgres:test@localhost:55432/postgres \\
        pytest -q tests/integration/test_pgvector_search.py
    docker rm -f pgvector-test
"""
import math
import os
import subprocess
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import src.mcp_server.tools as tools
from src.config import settings
from src.models.db import NoteEmbedding, NoteMetadata
from src.services.embeddings import semantic_search

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.skipif(
        not TEST_DATABASE_URL, reason="set TEST_DATABASE_URL to run pgvector integration tests"
    ),
    pytest.mark.asyncio(loop_scope="module"),
]

ROOT = Path(__file__).resolve().parent.parent.parent
DIM = settings.embedding_dimensions


def _unit(*leading: float) -> list[float]:
    """A DIM-wide vector with `leading` in the first components, normalized."""
    vec = list(leading) + [0.0] * (DIM - len(leading))
    norm = math.sqrt(sum(v * v for v in vec))
    return [v / norm for v in vec]


# Three notes at known angles from the query vector (1, 0, 0, ...):
#   near   → cos 1.0
#   middle → cos ~0.8
#   far    → cos 0.0
QUERY = _unit(1.0)
NEAR = _unit(1.0)
MIDDLE = _unit(0.8, 0.6)
FAR = _unit(0.0, 1.0)


@pytest.fixture(scope="module")
def migrated_database():
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env={**os.environ, "DATABASE_URL": TEST_DATABASE_URL},
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, (
        f"alembic upgrade head failed\n{result.stdout}\n{result.stderr}"
    )
    return TEST_DATABASE_URL


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def sessionmaker(migrated_database):
    engine = create_async_engine(migrated_database, poolclass=None)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest_asyncio.fixture(loop_scope="module")
async def seeded(sessionmaker, monkeypatch):
    """Three notes; the 'near' note carries two chunks so dedupe is exercised."""
    async with sessionmaker() as session:
        await session.execute(delete(NoteEmbedding))
        await session.execute(delete(NoteMetadata))
        await session.commit()

        rows = {}
        for name, vectors in (
            ("near.md", [NEAR, MIDDLE]),
            ("middle.md", [MIDDLE]),
            ("far.md", [FAR]),
        ):
            note = NoteMetadata(
                file_path=name,
                title=name.removesuffix(".md"),
                tags=["fixture"],
                frontmatter={},
                content_hash=name,
            )
            session.add(note)
            await session.flush()
            for i, vec in enumerate(vectors):
                session.add(
                    NoteEmbedding(
                        note_id=note.id,
                        chunk_index=i,
                        chunk_text=f"{name} chunk {i}",
                        embedding=vec,
                    )
                )
            rows[name] = note.id
        await session.commit()

    # find_related_impl uses the module-global sessionmaker and logs usage.
    monkeypatch.setattr(tools, "async_session", sessionmaker)

    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(tools, "_log_usage", noop)
    return rows


# ── semantic_search ─────────────────────────────────────────────────────────


async def test_semantic_search_orders_dedupes_and_scores(sessionmaker, seeded, monkeypatch):
    async def fake_get_embedding(text_: str):
        return QUERY

    monkeypatch.setattr(
        "src.services.embeddings.get_embedding", fake_get_embedding
    )

    async with sessionmaker() as session:
        results = await semantic_search(session, "anything", limit=10)

    paths = [r["path"] for r in results]
    assert paths == ["near.md", "middle.md", "far.md"], paths
    # One row per note even though near.md contributed two chunks.
    assert len(paths) == len(set(paths))
    # The similarity is computed in Python from the returned vector, which is a
    # plain list on pgvector 0.5 — the value must still be right.
    by_path = {r["path"]: r["similarity"] for r in results}
    assert by_path["near.md"] == pytest.approx(1.0, abs=1e-6)
    assert by_path["middle.md"] == pytest.approx(0.8, abs=1e-6)
    assert by_path["far.md"] == pytest.approx(0.0, abs=1e-6)


async def test_embedding_rows_are_plain_lists_on_pgvector_05(sessionmaker, seeded):
    from sqlalchemy import select

    async with sessionmaker() as session:
        row = (
            await session.execute(
                select(NoteEmbedding.embedding).limit(1)
            )
        ).scalar_one()
    # Documents the shape the NumPy post-processing has to accept. `list` on
    # pgvector 0.5; `numpy.ndarray` on 0.4. Either must work — this asserts the
    # code is exercised against whatever the installed version returns.
    assert len(row) == DIM
    assert float(row[0]) == pytest.approx(NEAR[0], abs=1e-6)


# ── find_related ────────────────────────────────────────────────────────────


async def test_find_related_ranks_by_averaged_chunk_similarity(seeded):
    output = await tools.find_related_impl("near.md", limit=10)

    # The source note appears only in the header line, never as a result.
    assert output.count("`near.md`") == 1, output
    middle_at = output.index("middle.md")
    far_at = output.index("far.md")
    assert middle_at < far_at, output
    # near.md averages NEAR and MIDDLE, so middle.md is the closer neighbour.
    assert "Top 2 related notes" in output


async def test_find_related_dedupes_per_note(sessionmaker, seeded):
    """A note with several chunks appears once, at its best-matching chunk."""
    async with sessionmaker() as session:
        note_id = seeded["middle.md"]
        for i, vec in enumerate((FAR, NEAR), start=1):
            session.add(
                NoteEmbedding(
                    note_id=note_id,
                    chunk_index=i,
                    chunk_text=f"middle.md chunk {i}",
                    embedding=vec,
                )
            )
        await session.commit()

    output = await tools.find_related_impl("near.md", limit=10)
    assert output.count("`middle.md`") == 1, output


# ── the HNSW index is actually usable on this build ─────────────────────────


async def test_hnsw_index_exists_and_query_settings_apply(sessionmaker, seeded):
    async with sessionmaker() as session:
        await session.execute(text("SET LOCAL hnsw.ef_search = 80"))
        indexes = (
            await session.execute(
                text(
                    "SELECT indexname FROM pg_indexes "
                    "WHERE tablename = 'note_embeddings'"
                )
            )
        ).scalars().all()
    assert "ix_note_embeddings_embedding_hnsw" in indexes
