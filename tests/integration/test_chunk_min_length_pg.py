"""Chunking version 2 on a real PostgreSQL (#390, tasks 2.9, 3.1, 3.4).

* the SQL `clen` twin agrees with the chunker's Python `clen`;
* the harness's `chunk_lengths` and the recipe's `UNDER_MIN_SQL` on a seeded
  copy (a whole-short note, an over-window whitespace chunk, normal chunks);
* cross-version reuse end to end: a version-1 note re-chunked under version 2
  reuses exactly the chunks whose version-1 input equals the version-2 one,
  a NULL stamp reuses nothing, and certification stamps 2;
* the embed stage's `(S submitted, R reused)` completion line.

Skipped unless `PGVECTOR_TEST_ADMIN_URL` is set — see `_harness.py`.
"""
import hashlib
import logging

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import src.database
import src.mcp_server.tools as tools
from scripts.search_eval import db as eval_db
from src.config import settings
from src.models.db import NoteEmbedding, NoteMetadata
from src.services import embeddings as embeddings_service
from src.services import index_state, indexer, indexer_health
from src.services import vault as vault_service
from src.services.embeddings import CHUNKING_VERSION, CLEN_SQL, clen
from src.services.index_state import KEY_EMBEDDING_FINGERPRINT
import _harness

pytestmark = [
    _harness.requires_pgvector,
    pytest.mark.asyncio(loop_scope="module"),
]

DIM = int(settings.embedding_dimensions)
W = int(settings.chunk_size) * 4
M = embeddings_service.effective_min_chars(W)
PROSE = "the archivist records every entry in the ledger and files it away "


def prose(n: int, tag: str = "") -> str:
    out = ((tag + " ") if tag else "") + PROSE * (n // len(PROSE) + 2)
    return out[:n].rstrip() + "."


def vector_of(seed: str) -> list[float]:
    out: list[float] = []
    block = seed.encode("utf-8")
    while len(out) < DIM:
        block = hashlib.sha256(block).digest()
        out.extend(b / 255.0 - 0.5 for b in block)
    return out[:DIM]


class Provider:
    def __init__(self):
        self.documents: list[str] = []

    async def batch(self, texts, *, kind):
        assert kind == "document"
        self.documents.extend(texts)
        return [vector_of(t) for t in texts]


@pytest.fixture(scope="module")
def migrated_url():
    yield from _harness.throwaway_database("chunk_min_390", DIM)


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def sessionmaker(migrated_url):
    engine = create_async_engine(migrated_url, poolclass=None)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest_asyncio.fixture(loop_scope="module")
async def world(sessionmaker, monkeypatch, tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    for target in (settings, indexer.settings, tools.settings):
        monkeypatch.setattr(target, "vault_path", str(root), raising=False)
    monkeypatch.setattr(settings, "multi_user_mode", False, raising=False)
    monkeypatch.setattr(settings, "embedding_exclude_patterns", [], raising=False)
    monkeypatch.setattr(settings, "embed_chunk_budget_per_user", 0, raising=False)
    monkeypatch.setattr(settings, "embed_time_budget_seconds_per_user", 0, raising=False)
    monkeypatch.setattr(indexer, "async_session", sessionmaker)
    monkeypatch.setattr(tools, "async_session", sessionmaker)
    monkeypatch.setattr(src.database, "async_session", sessionmaker)
    monkeypatch.setattr(indexer, "_is_paused", lambda: False)
    provider = Provider()
    monkeypatch.setattr(embeddings_service, "get_embeddings_batch", provider.batch)
    indexer._last_full_hash.clear()
    indexer_health.reset()
    vault_service.clear_user_vault_cache()
    async with sessionmaker() as session:
        for table in ("note_links", "note_embeddings", "notes_metadata",
                      "indexer_runs", "indexer_state", "users"):
            await session.execute(text(f"DELETE FROM {table}"))
        await index_state.set_state(
            session, KEY_EMBEDDING_FINGERPRINT, index_state.embedding_fingerprint()
        )
        await session.commit()
    yield {"root": root, "provider": provider}
    indexer._last_full_hash.clear()
    indexer_health.reset()


def write(root, rel, body):
    (root / rel).write_text(body, encoding="utf-8")


async def stamp(sessionmaker, version):
    async with sessionmaker() as session:
        await session.execute(
            text("UPDATE notes_metadata SET embedded_chunking_version = :v"),
            {"v": version},
        )
        await session.commit()


async def versions(sessionmaker):
    async with sessionmaker() as session:
        return set((await session.execute(
            select(NoteMetadata.embedded_chunking_version)
        )).scalars())


# ── the SQL twin of `clen` ────────────────────────────────────────────────


CLEN_CASES = [
    "", " ", "plain", "  lead and trail  ", "a\t\tb", "a\r\nb", "a\rb",
    "a\x0bb\x0cc", "a  b", " ", "x" + " " * 3000 + "y",
    "\n\n# heading\n\nbody\t \r\n", "\t\n\x0b\x0c\r ",
]


async def test_sql_clen_equals_python_clen(sessionmaker):
    async with sessionmaker() as session:
        for case in CLEN_CASES:
            got = (await session.execute(
                text(f"SELECT {CLEN_SQL.format(col='CAST(:t AS text)')}"), {"t": case}
            )).scalar()
            assert got == clen(case), repr(case)


# ── chunk_lengths and UNDER_MIN_SQL on a seeded copy ──────────────────────


async def test_chunk_lengths_on_a_seeded_copy(sessionmaker, world):
    root = world["root"]
    write(root, "Short.md", "a stub\n")
    write(root, "Spaces.md", "A" * 100 + " " * 3000 + "B" * 100 + "\n")
    write(root, "Long.md", "# One\n" + prose(3000, "one") + "\n# Two\n" + prose(900, "two") + "\n")
    await indexer.index_vault(user_id=None)
    await indexer.embed_vault(user_id=None)
    assert await versions(sessionmaker) == {CHUNKING_VERSION}

    async with sessionmaker() as session:
        stats = await eval_db.chunk_lengths(session, (True, None), M, W)
        under = (await session.execute(text(eval_db.UNDER_MIN_SQL), {"m": M})).scalar()
        total = (await session.execute(text("SELECT count(*) FROM note_embeddings"))).scalar()
    assert under == 0
    assert stats["chunks"] == total and total >= 4
    assert stats["under_min"] == 0
    assert stats["whole_short"] == 1
    assert stats["raw_over_window"] == 1
    assert stats["p10"] <= stats["p50"] <= stats["p90"] <= W
    # Another owner's rows are outside a scoped count.
    async with sessionmaker() as session:
        assert await eval_db.chunk_lengths(session, (True, 4242), M, W) is None


# ── cross-version reuse, end to end ───────────────────────────────────────


async def test_a_version_1_note_reuses_only_what_its_v1_input_allows(
    sessionmaker, world, caplog
):
    root, provider = world["root"], world["provider"]
    plain = prose(600, "plain")
    spaced = prose(300, "spaced") + "\n" + " " * 12 + "- deep item\n" + prose(300, "after")
    write(root, "R.md", f"# Plain\n{plain}\n# Spaced\n{spaced}\n")
    await indexer.index_vault(user_id=None)
    await indexer.embed_vault(user_id=None)
    async with sessionmaker() as session:
        rows = (await session.execute(
            select(NoteEmbedding.chunk_text, NoteEmbedding.embed_prefix)
            .join(NoteMetadata, NoteMetadata.id == NoteEmbedding.note_id)
            .where(NoteMetadata.file_path == "R.md")
            .order_by(NoteEmbedding.chunk_index)
        )).all()
    assert [r.chunk_text for r in rows] == [plain + "\n", spaced + "\n"]

    # Pretend the rows were certified by the version-1 build: same texts,
    # same prefixes. The version-1 input of the spaced chunk kept its 12-space
    # run, so it is the only one that cannot be reused.
    await stamp(sessionmaker, 1)
    provider.documents.clear()
    caplog.set_level(logging.INFO, logger="src.services.indexer")
    caplog.clear()
    await indexer.embed_vault(user_id=None)
    assert provider.documents == [
        embeddings_service.embedding_input("R > Spaced", spaced + "\n")
    ]
    assert " " * 12 not in provider.documents[0]
    assert await versions(sessionmaker) == {CHUNKING_VERSION}
    (line,) = [r.getMessage() for r in caplog.records
               if r.getMessage().startswith("Embedding complete")]
    assert "1 notes, 2 chunks (1 submitted, 1 reused)" in line, line

    # A NULL stamp reuses nothing.
    await stamp(sessionmaker, None)
    provider.documents.clear()
    await indexer.embed_vault(user_id=None)
    assert len(provider.documents) == 2
    # A stamp outside the formatter table reuses nothing either.
    await stamp(sessionmaker, 7)
    provider.documents.clear()
    await indexer.embed_vault(user_id=None)
    assert len(provider.documents) == 2
    assert await versions(sessionmaker) == {CHUNKING_VERSION}


async def test_the_completion_line_counts_a_reused_note_and_a_fresh_one(
    sessionmaker, world, caplog
):
    root, provider = world["root"], world["provider"]
    write(root, "Old.md", "# Old\n" + prose(500, "old") + "\n")
    await indexer.index_vault(user_id=None)
    await indexer.embed_vault(user_id=None)
    # `Old.md` is re-selected (its certification cleared) and fully reusable;
    # `New.md` is fresh.
    async with sessionmaker() as session:
        await session.execute(text(
            "UPDATE notes_metadata SET embedded_content_hash = NULL WHERE file_path = 'Old.md'"
        ))
        await session.commit()
    write(root, "New.md", "# New\n" + prose(500, "new") + "\n# More\n" + prose(500, "more") + "\n")
    await indexer.index_vault(user_id=None)
    provider.documents.clear()
    caplog.set_level(logging.INFO, logger="src.services.indexer")
    caplog.clear()
    await indexer.embed_vault(user_id=None)
    assert len(provider.documents) == 2
    (line,) = [r.getMessage() for r in caplog.records
               if r.getMessage().startswith("Embedding complete")]
    assert ": 2 notes, 3 chunks (2 submitted, 1 reused)" in line, line
