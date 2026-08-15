"""Opt-in integration test: the vector query paths against a real pgvector.

pgvector 0.5.0 stopped requiring NumPy and now returns SQLAlchemy vector
columns as **plain Python lists** instead of `numpy.ndarray`. Both vector query
paths post-process those rows with NumPy (`semantic_search` computes a cosine
similarity per row, `find_related` averages a note's chunk vectors and then
scores candidates), so the shape change is exactly the kind of thing a unit
test with fake rows would miss.

Skipped unless `PGVECTOR_TEST_ADMIN_URL` is set. That variable names a
**server** to create a throwaway database on — the fixture never touches the
database in the URL itself beyond using it as a maintenance connection. It
`CREATE DATABASE test_pgvector_<uuid>`, migrates *that*, runs the tests there,
and drops it in teardown, so a mistyped URL cannot cost data. The URL's own
database name may not be `obsidian_mcp` (the production name) — that is a hard
failure, not a skip. It is deliberately a different variable from the
`TEST_DATABASE_URL` that `tests/test_fts_integration.py` uses in place.

    docker run --rm -d --name pgvector-test -e POSTGRES_PASSWORD=test \\
        -p 55432:5432 pgvector/pgvector:pg16
    PGVECTOR_TEST_ADMIN_URL=postgresql+asyncpg://postgres:test@localhost:55432/postgres \\
        pytest -q tests/integration/test_pgvector_search.py
    docker rm -f pgvector-test
"""
import asyncio
import math
import os
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest
import pytest_asyncio
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import src.mcp_server.tools as tools
from src.config import settings
from src.models.db import NoteEmbedding, NoteMetadata
from src.services.embeddings import semantic_search

PGVECTOR_TEST_ADMIN_URL = os.environ.get("PGVECTOR_TEST_ADMIN_URL")

# The production database name. Refusing it is a backstop against someone
# exporting the live URL here: the fixture would otherwise open a maintenance
# connection to it (and, on a bad edit, run migrations against it).
FORBIDDEN_DB_NAMES = {"obsidian_mcp"}

pytestmark = [
    pytest.mark.skipif(
        not PGVECTOR_TEST_ADMIN_URL,
        reason="set PGVECTOR_TEST_ADMIN_URL to run pgvector integration tests",
    ),
    pytest.mark.asyncio(loop_scope="module"),
]

ROOT = Path(__file__).resolve().parent.parent.parent
DIM = settings.embedding_dimensions


def _with_database(url: str, dbname: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path=f"/{dbname}"))


def _asyncpg_dsn(url: str) -> str:
    """SQLAlchemy URL → a DSN asyncpg.connect() accepts."""
    parts = urlsplit(url)
    return urlunsplit(parts._replace(scheme=parts.scheme.split("+", 1)[0]))


async def _run_maintenance(admin_url: str, statement: str) -> None:
    """Run a CREATE/DROP DATABASE on the admin URL's own database.

    asyncpg is autocommit outside an explicit transaction, which is what
    CREATE/DROP DATABASE requires.
    """
    import asyncpg

    conn = await asyncpg.connect(_asyncpg_dsn(admin_url))
    try:
        await conn.execute(statement)
    finally:
        await conn.close()


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
    """Create a throwaway database on the admin server, migrate it, drop it."""
    admin_db = urlsplit(PGVECTOR_TEST_ADMIN_URL).path.lstrip("/")
    if admin_db in FORBIDDEN_DB_NAMES:
        pytest.fail(
            "PGVECTOR_TEST_ADMIN_URL points at the production database name "
            f"{admin_db!r}. Point it at a throwaway server (see the module "
            "docstring); this fixture creates and drops databases."
        )

    dbname = f"test_pgvector_{uuid.uuid4().hex}"
    asyncio.run(
        _run_maintenance(PGVECTOR_TEST_ADMIN_URL, f'CREATE DATABASE "{dbname}"')
    )
    try:
        url = _with_database(PGVECTOR_TEST_ADMIN_URL, dbname)
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=ROOT,
            env={**os.environ, "DATABASE_URL": url},
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert result.returncode == 0, (
            f"alembic upgrade head failed\n{result.stdout}\n{result.stderr}"
        )
        yield url
    finally:
        # FORCE terminates any connection the test left behind, so a failing
        # test still cleans up its database.
        asyncio.run(
            _run_maintenance(
                PGVECTOR_TEST_ADMIN_URL, f'DROP DATABASE IF EXISTS "{dbname}" (FORCE)'
            )
        )


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
    # Pins the shape the NumPy post-processing has to accept: pgvector 0.5
    # returns a plain `list` where 0.4 returned a `numpy.ndarray`. Asserting
    # `list` is what makes a silent revert to the old shape visible.
    assert isinstance(row, list), type(row)
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
