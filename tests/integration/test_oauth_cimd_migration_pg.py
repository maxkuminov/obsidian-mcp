"""Migration 036 (#357): the CIMD column, its shape CHECK, and the refusing downgrade.

Skipped unless `PGVECTOR_TEST_ADMIN_URL` names a throwaway Postgres server
(`make test-integration` sets it).
"""
import asyncio

import asyncpg
import pytest

import _harness

pytestmark = _harness.requires_pgvector

CIMD_ID = "cimd_" + "a" * 59
URL = "https://claude.ai/oauth/claude-code-client-metadata"


def _dsn(url: str) -> str:
    return _harness.asyncpg_dsn(url)


async def _exec(url: str, sql: str, *args):
    conn = await asyncpg.connect(_dsn(url))
    try:
        return await conn.execute(sql, *args)
    finally:
        await conn.close()


async def _fetchval(url: str, sql: str, *args):
    conn = await asyncpg.connect(_dsn(url))
    try:
        return await conn.fetchval(sql, *args)
    finally:
        await conn.close()


def _insert(url, client_id, metadata_url, method="none", secret=None):
    return asyncio.run(
        _exec(
            url,
            "INSERT INTO oauth_clients (client_id, client_secret_hash, "
            "token_endpoint_auth_method, client_name, redirect_uris, scope, "
            "client_id_metadata_url, last_used_at) "
            "VALUES ($1, $2, $3, 'x', '[]'::jsonb, 'read', $4, now())",
            client_id,
            secret,
            method,
            metadata_url,
        )
    )


@pytest.fixture(scope="module")
def db_url():
    yield from _harness.throwaway_database("cimd_036", 64)


def test_shape_check_accepts_both_kinds(db_url):
    _insert(db_url, "0" * 32, None)
    _insert(db_url, CIMD_ID, URL)
    asyncio.run(_exec(db_url, "DELETE FROM oauth_clients"))


@pytest.mark.parametrize(
    "client_id, metadata_url, method, secret",
    [
        ("cimd_" + "b" * 59, None, "none", None),  # cimd_ id without a URL
        ("1" * 32, URL, "none", None),  # URL on a DCR-shaped id
        ("cimd_" + "c" * 59, URL, "client_secret_post", "f" * 64),  # confidential
    ],
)
def test_shape_check_refuses_malformed_rows(db_url, client_id, metadata_url, method, secret):
    with pytest.raises(asyncpg.CheckViolationError):
        _insert(db_url, client_id, metadata_url, method, secret)


def test_downgrade_refuses_while_cimd_rows_exist(db_url):
    _insert(db_url, CIMD_ID, URL)
    result = _harness.run_alembic(db_url, "downgrade", "035", check=False)
    assert result.returncode != 0
    assert "036 downgrade refused: 1 Client ID Metadata Document" in (result.stdout + result.stderr)
    # Nothing changed: the column is still there and the row survives.
    assert asyncio.run(
        _fetchval(db_url, "SELECT count(*) FROM oauth_clients WHERE client_id_metadata_url IS NOT NULL")
    ) == 1

    asyncio.run(_exec(db_url, "DELETE FROM oauth_clients"))
    _harness.run_alembic(db_url, "downgrade", "035")
    assert asyncio.run(
        _fetchval(
            db_url,
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_name = 'oauth_clients' AND column_name = 'client_id_metadata_url'",
        )
    ) == 0
    _harness.run_alembic(db_url, "upgrade", "head")
    assert asyncio.run(
        _fetchval(
            db_url,
            "SELECT count(*) FROM pg_constraint WHERE conname = 'ck_oauth_clients_cimd_shape'",
        )
    ) == 1
