"""Migrations run to head without superuser, and the role checks hold (#324).

Design D5, D6, D8. Against the shared integration server
(`PGVECTOR_TEST_ADMIN_URL`, whose user is the superuser `postgres`):

* a fresh `NOSUPERUSER` role owns a fresh database in which `postgres` has
  installed `vector` — the shape both the compose bundles and the Kubernetes
  bundle (CloudNativePG, role `obsidian_mcp`) create. `alembic upgrade head`
  and `alembic check` run **as that role** and must succeed and be clean; any
  future migration that needs superuser fails here. The server's startup
  superuser check is then run on that role's session and must pass silently,
  which is the production path;
* `alembic upgrade head` with a wrong password, through SQLAlchemy's real
  exception wrapping, prints exactly one upgrade hint line; a failure that is
  not authentication prints none.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import uuid
from urllib.parse import urlsplit, urlunsplit

import pytest

from tests.integration._harness import (
    PGVECTOR_TEST_ADMIN_URL,
    _run_maintenance,
    _with_database,
    asyncpg_dsn,
    requires_pgvector,
    run_alembic,
)

pytestmark = requires_pgvector

HINT = "docker/upgrade-split-db-roles.sql"


def _as_user(url: str, user: str, password: str) -> str:
    parts = urlsplit(url)
    host = parts.hostname or "localhost"
    netloc = f"{user}:{password}@{host}" + (f":{parts.port}" if parts.port else "")
    return urlunsplit(parts._replace(netloc=netloc))


async def _admin_in(db: str, statement: str) -> None:
    import asyncpg

    conn = await asyncpg.connect(asyncpg_dsn(_with_database(PGVECTOR_TEST_ADMIN_URL, db)))
    try:
        await conn.execute(statement)
    finally:
        await conn.close()


@pytest.fixture(scope="module")
def owner_db():
    suffix = uuid.uuid4().hex[:12]
    role = f"omcp_nonsuper_{suffix}"
    db = f"test_nonsuper_{suffix}"
    password = secrets.token_hex(16)
    try:
        asyncio.run(_run_maintenance(
            PGVECTOR_TEST_ADMIN_URL,
            f"CREATE ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
            f"NOREPLICATION NOBYPASSRLS PASSWORD '{password}'",
        ))
        asyncio.run(_run_maintenance(PGVECTOR_TEST_ADMIN_URL, f'CREATE DATABASE "{db}" OWNER {role}'))
        # The privileged init phase: the superuser installs the extension.
        asyncio.run(_admin_in(db, "CREATE EXTENSION IF NOT EXISTS vector"))
        url = _as_user(_with_database(PGVECTOR_TEST_ADMIN_URL, db), role, password)
        yield {"url": url, "role": role, "db": db, "password": password}
    finally:
        try:
            asyncio.run(_run_maintenance(PGVECTOR_TEST_ADMIN_URL, f'DROP DATABASE IF EXISTS "{db}" (FORCE)'))
        finally:
            asyncio.run(_run_maintenance(PGVECTOR_TEST_ADMIN_URL, f"DROP ROLE IF EXISTS {role}"))


def test_non_superuser_owner_migrates_to_head_cleanly(owner_db):
    run_alembic(owner_db["url"], "upgrade", "head")
    result = run_alembic(owner_db["url"], "check")
    assert "No new upgrade operations detected" in result.stdout + result.stderr

    import asyncpg

    async def inspect():
        conn = await asyncpg.connect(asyncpg_dsn(owner_db["url"]))
        try:
            assert await conn.fetchval("SELECT current_user") == owner_db["role"]
            assert await conn.fetchval(
                "SELECT rolsuper FROM pg_roles WHERE rolname = current_user"
            ) is False
            tables = await conn.fetch(
                "SELECT tablename, tableowner FROM pg_tables WHERE schemaname = 'public'"
            )
            assert tables, "no tables were created"
            assert {t["tableowner"] for t in tables} == {owner_db["role"]}, tables
            assert "alembic_version" in {t["tablename"] for t in tables}
            assert await conn.fetchval(
                "SELECT pg_get_userbyid(extowner) FROM pg_extension WHERE extname = 'vector'"
            ) == "postgres"
        finally:
            await conn.close()

    asyncio.run(inspect())


async def test_startup_superuser_check_passes_for_a_non_superuser_owner(owner_db, monkeypatch, caplog):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import src.config
    import src.database
    from src.services import database_role

    engine = create_async_engine(owner_db["url"])
    try:
        monkeypatch.setattr(src.database, "async_session", async_sessionmaker(engine))
        monkeypatch.setattr(src.config.settings, "database_allow_superuser", False)
        with caplog.at_level(logging.DEBUG):
            await database_role.check_database_role()  # no SystemExit
        assert [r for r in caplog.records if r.name == "src.services.database_role"] == []
    finally:
        await engine.dispose()


async def test_startup_superuser_check_refuses_the_real_superuser(monkeypatch, caplog):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import src.config
    import src.database
    from src.services import database_role

    engine = create_async_engine(PGVECTOR_TEST_ADMIN_URL)
    try:
        monkeypatch.setattr(src.database, "async_session", async_sessionmaker(engine))
        monkeypatch.setattr(src.config.settings, "database_allow_superuser", False)
        with caplog.at_level(logging.DEBUG), pytest.raises(SystemExit):
            await database_role.check_database_role()
    finally:
        await engine.dispose()


def test_wrong_password_prints_the_upgrade_hint_once(owner_db):
    url = _as_user(_with_database(PGVECTOR_TEST_ADMIN_URL, owner_db["db"]), owner_db["role"], "wrong" + "0" * 27)
    result = run_alembic(url, "upgrade", "head", check=False)
    assert result.returncode != 0
    hint_lines = [line for line in result.stderr.splitlines() if HINT in line]
    assert len(hint_lines) == 1, result.stderr
    assert f"role {owner_db['role']}" in hint_lines[0]
    assert "wrong" not in hint_lines[0]


def test_non_authentication_failure_prints_no_hint(owner_db):
    # Authenticates fine, but the database does not exist (SQLSTATE 3D000).
    url = _as_user(
        _with_database(PGVECTOR_TEST_ADMIN_URL, f"no_such_db_{uuid.uuid4().hex[:8]}"),
        owner_db["role"], owner_db["password"],
    )
    result = run_alembic(url, "upgrade", "head", check=False)
    assert result.returncode != 0
    assert HINT not in result.stderr
