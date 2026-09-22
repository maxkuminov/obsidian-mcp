"""The database transport policy against a real PostgreSQL (#184; task 1.11).

The throwaway `pgvector/pgvector:pg16` server runs with `ssl=off`, which is
exactly production's server today. So this module proves the #184 downgrade
closed against a real server: under `prefer` the session is plaintext and the
startup assertion says so; under `require` and `verify-full` the first connect
**fails** instead of falling back to plaintext; and the D3b per-connection
listener, run through SQLAlchemy's real greenlet cursor, discards a plaintext
connection.

Skips without `PGVECTOR_TEST_ADMIN_URL`. Connects to the admin URL's own
maintenance database and runs only `SELECT`s; it creates nothing.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import src.config
import src.database
from src.config import Settings
from src.services import security_events
from src.services import transport_security as ts

from _harness import PGVECTOR_TEST_ADMIN_URL, requires_pgvector

pytestmark = requires_pgvector

CA = str(Path(__file__).resolve().parent.parent / "fixtures" / "transport_tls" / "ca.pem")
SECRET = "a-real-test-secret-not-a-placeholder"


def _settings(mode: str, **kw) -> Settings:
    if mode.startswith("verify"):
        kw.setdefault("database_ssl_ca_file", CA)
    return Settings(
        _env_file=None,
        secret_key=SECRET,
        database_url=PGVECTOR_TEST_ADMIN_URL,
        database_ssl_mode=mode,
        **kw,
    )


def _engine(settings: Settings):
    return create_async_engine(
        settings.database_url,
        connect_args=ts.database_ssl_connect_args(settings),
    )


@pytest.fixture
def events():
    records: list[logging.LogRecord] = []

    class _Handler(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _Handler(level=logging.DEBUG)
    logger = logging.getLogger("security_events")
    logger.addHandler(handler)
    security_events.reset_state()
    try:
        with security_events.strict_fields(), security_events.suppression_disabled():
            yield records
    finally:
        logger.removeHandler(handler)
        security_events.reset_state()


def _install(monkeypatch, settings: Settings, engine) -> None:
    monkeypatch.setattr(src.config, "settings", settings)
    monkeypatch.setattr(
        src.database,
        "async_session",
        async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False),
    )


async def test_prefer_connects_in_plaintext_and_is_reported(monkeypatch, events, caplog):
    settings = _settings("prefer")
    engine = _engine(settings)
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text("SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()")
                )
            ).first()
        assert row is not None and row[0] is False
        _install(monkeypatch, settings, engine)
        with caplog.at_level(logging.INFO):
            await ts.check_database_transport()
    finally:
        await engine.dispose()
    plaintext = [r for r in events if r.getMessage() == "internal_transport_plaintext"]
    assert len(plaintext) == 1
    assert (plaintext[0].reason, plaintext[0].outcome) == ("database", "prefer")
    lines = [
        r.getMessage()
        for r in caplog.records
        if r.getMessage().startswith("Database transport:")
    ]
    assert lines and "encrypted=False" in lines[0] and "mode=prefer" in lines[0]


@pytest.mark.parametrize("mode", ["require", "verify-full"])
async def test_a_strict_mode_refuses_a_server_without_tls(mode):
    """The #184 downgrade, closed: no plaintext retry, no session."""
    settings = _settings(mode)
    engine = _engine(settings)
    try:
        with pytest.raises(Exception) as info:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        assert not isinstance(info.value, AssertionError)
    finally:
        await engine.dispose()


@pytest.mark.parametrize("mode", ["require", "verify-full"])
async def test_the_startup_assertion_exits_under_a_strict_mode(monkeypatch, caplog, mode):
    settings = _settings(mode)
    engine = _engine(settings)
    try:
        _install(monkeypatch, settings, engine)
        with caplog.at_level(logging.INFO), pytest.raises(SystemExit):
            await ts.check_database_transport()
    finally:
        await engine.dispose()
    critical = [r for r in caplog.records if r.levelno == logging.CRITICAL]
    assert critical and mode in critical[0].getMessage()


async def test_the_per_connection_listener_discards_a_plaintext_connection():
    """D3b through SQLAlchemy's real async-adapted cursor: a plaintext session
    (here admitted by `prefer`, since the server offers nothing else) is
    refused by the listener before any application statement runs on it
    (SQLAlchemy's own dialect setup queries may precede it — design.md, N1)."""
    settings = _settings("prefer")
    engine = _engine(settings)
    ts.install_strict_transport_listener(engine)
    try:
        with pytest.raises(ts.DatabaseTransportError):
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
    finally:
        await engine.dispose()
