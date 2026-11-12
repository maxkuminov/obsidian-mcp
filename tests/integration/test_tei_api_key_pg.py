"""#426 — a refused `TEI_API_KEY` leaves its name, never its value, in the
persisted pass record and on `/admin/health`, against a real PostgreSQL.

What only a real database shows: that the text the embed stage swallows per
note reaches `indexer_runs.error` through the real pass record, and that the
page an operator reads it on (run history plus the in-process error ring)
renders it without the key.

Skipped unless `PGVECTOR_TEST_ADMIN_URL` is set (see `_harness`).
"""
import logging

import pytest
import pytest_asyncio
import respx
from fastapi.testclient import TestClient
from httpx import Response
from pydantic import SecretStr
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import _harness
import src.database
from src import main
from src.config import settings
from src.control_panel import routes as panel_routes
from src.database import get_session
from src.models.db import IndexerRun, NoteEmbedding, User
from src.services import embeddings as embeddings_service
from src.services import error_log, indexer
from src.services import vault as vault_service

pytestmark = [
    _harness.requires_pgvector,
    pytest.mark.asyncio(loop_scope="module"),
]

DIM = 8
SENTINEL = "omcp-sentinel-7f3a9c"
TEI = "http://tei:80"


@pytest.fixture(scope="module")
def migrated_url():
    yield from _harness.throwaway_database("tei_api_key_426", DIM)


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def sessionmaker(migrated_url):
    engine = create_async_engine(migrated_url, poolclass=NullPool)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest_asyncio.fixture(loop_scope="module")
async def vault(sessionmaker, monkeypatch, tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    for setting, value in {
        "vault_path": str(root),
        "multi_user_mode": False,
        "embedding_exclude_patterns": [],
        "embedding_provider": "tei",
        "tei_url": TEI,
        "tei_embedding_model": "BAAI/bge-m3",
        "tei_embed_batch_size": 16,
        "embedding_dimensions": DIM,
        "tei_api_key": SecretStr(SENTINEL),
    }.items():
        monkeypatch.setattr(settings, setting, value, raising=False)
    monkeypatch.setattr(indexer, "async_session", sessionmaker)
    monkeypatch.setattr(src.database, "async_session", sessionmaker)
    monkeypatch.setattr(embeddings_service, "async_session", sessionmaker, raising=False)
    monkeypatch.setattr(indexer, "_is_paused", lambda: False)
    monkeypatch.setattr(embeddings_service, "_TEI_CLIENT", embeddings_service._SharedClient())
    monkeypatch.setattr(embeddings_service, "_TEI_AUTH_LOG", embeddings_service._TeiAuthLog())
    verification = embeddings_service._TeiVerification()
    verification.verified = True
    monkeypatch.setattr(embeddings_service, "_TEI_VERIFICATION", verification)
    embeddings_service.get_provider.cache_clear()
    indexer._last_full_hash.clear()
    vault_service.clear_user_vault_cache()

    async with sessionmaker() as session:
        for table in ("note_links", "note_embeddings", "notes_metadata", "indexer_runs", "users"):
            await session.execute(text(f"DELETE FROM {table}"))
        await session.commit()

    yield root

    embeddings_service.get_provider.cache_clear()
    indexer._last_full_hash.clear()


async def test_a_refused_key_is_named_but_never_shown(sessionmaker, vault, migrated_url, caplog):
    for name in ("One", "Two", "Three"):
        (vault / f"{name}.md").write_text(f"# {name}\n\nSome body text.\n", encoding="utf-8")

    error_log.reset()
    error_log.attach()
    caplog.set_level(logging.DEBUG)
    try:
        with respx.mock(base_url=TEI, assert_all_called=False) as mock:
            embed = mock.post("/embed").mock(
                return_value=Response(401, text=f"bad token {SENTINEL}")
            )
            await indexer._index_pass_once(None)
        assert embed.call_count == 3, "one request per note, never retried"
        assert all(
            c.request.headers["authorization"] == f"Bearer {SENTINEL}" for c in embed.calls
        ), "the positive control"

        async with sessionmaker() as session:
            (error,) = (await session.execute(select(IndexerRun.error))).scalars().all()
            stored = (await session.execute(select(NoteEmbedding.id))).scalars().all()
            admin = User(username="admin", password_hash="x", is_admin=True, is_active=True)
            session.add(admin)
            await session.commit()
        assert stored == []
        assert error is not None
        assert "TEI_API_KEY" in error and SENTINEL not in error

        # One provider ERROR line, beside the indexer's pass summary and the
        # per-note WARNING lines, which this change leaves as they were.
        provider_errors = [
            r for r in caplog.records
            if r.levelno == logging.ERROR
            and r.getMessage() == "TEI /embed answered HTTP 401; the server rejected TEI_API_KEY"
        ]
        assert len(provider_errors) == 1
        assert any(r.getMessage().startswith("Embedding pass") for r in caplog.records)
        assert sum("Failed to embed" in r.getMessage() for r in caplog.records) == 3

        page = _render_health(migrated_url, admin)
        assert "TEI_API_KEY" in page, "the run history and error ring render the cause"
        assert SENTINEL not in page
        assert all(SENTINEL not in r.getMessage() for r in caplog.records)
    finally:
        error_log.detach()
        error_log.reset()


def _render_health(url: str, admin: User) -> str:
    overrides = dict(main.app.dependency_overrides)

    async def _get_session():
        engine = create_async_engine(url, poolclass=NullPool)
        try:
            async with async_sessionmaker(engine, class_=AsyncSession)() as session:
                yield session
        finally:
            await engine.dispose()

    async def _panel_user():
        return admin

    main.app.dependency_overrides[get_session] = _get_session
    main.app.dependency_overrides[panel_routes.require_user_panel] = _panel_user
    try:
        response = TestClient(main.app, base_url="https://localhost").get("/admin/health")
        assert response.status_code == 200
        return response.text
    finally:
        main.app.dependency_overrides.clear()
        main.app.dependency_overrides.update(overrides)
