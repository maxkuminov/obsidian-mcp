"""Mandatory real-Postgres gate for the transfer token machinery.

Unlike `test_pgvector_search.py`, which is an optional performance/shape check,
this module is a **security** gate and therefore **fails** rather than skips
when it cannot run. Three of the properties it covers have no meaningful
fake-session equivalent:

* single-use is a *transaction boundary* (`UPDATE … WHERE state='pending'
  RETURNING`), so only a real concurrent claim proves it;
* the pre-publication `SELECT … FOR UPDATE` either blocks a racing revocation
  or loses to it — a fake session has no lock manager to lose to;
* `ON DELETE CASCADE` is enforced by the database, not by SQLAlchemy.

Run it with a throwaway server (see `requirements-dev.txt`)::

    docker run --rm -d --name pgvector-test -e POSTGRES_PASSWORD=test \\
        -p 55432:5432 pgvector/pgvector:pg16
    PGVECTOR_TEST_ADMIN_URL=postgresql+asyncpg://postgres:test@localhost:55432/postgres \\
        pytest -q tests/integration/test_transfer_pg.py
    docker rm -f pgvector-test

Set `OMCP_ALLOW_SKIP_TRANSFER_INTEGRATION=1` to downgrade the failure to a skip
— for a machine with no Docker. That is a deliberate, visible opt-out; the
deploy gate does not set it.
"""
import asyncio
import datetime
import os
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit

import pytest
import pytest_asyncio
from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.models.db import APIKey, OAuthClient, OAuthToken, TransferToken, User

PGVECTOR_TEST_ADMIN_URL = os.environ.get("PGVECTOR_TEST_ADMIN_URL")
ALLOW_SKIP = os.environ.get("OMCP_ALLOW_SKIP_TRANSFER_INTEGRATION") == "1"

FORBIDDEN_DB_NAMES = {"obsidian_mcp"}

ROOT = Path(__file__).resolve().parent.parent.parent

pytestmark = [pytest.mark.asyncio(loop_scope="module")]

_MISSING_URL_MESSAGE = (
    "PGVECTOR_TEST_ADMIN_URL is not set. This is the mandatory Postgres gate "
    "for binary-file-transfer (claim linearizability, FOR UPDATE publish "
    "barrier, ON DELETE CASCADE) and it fails rather than skips on purpose. "
    "Run:\n"
    "  docker run --rm -d --name pgvector-test -e POSTGRES_PASSWORD=test "
    "-p 55432:5432 pgvector/pgvector:pg16\n"
    "  PGVECTOR_TEST_ADMIN_URL=postgresql+asyncpg://postgres:test@localhost:"
    "55432/postgres pytest -q tests/integration/test_transfer_pg.py\n"
    "Set OMCP_ALLOW_SKIP_TRANSFER_INTEGRATION=1 to opt out explicitly."
)


@pytest.fixture(autouse=True, scope="module")
def _require_postgres():
    if PGVECTOR_TEST_ADMIN_URL:
        return
    if ALLOW_SKIP:
        pytest.skip("OMCP_ALLOW_SKIP_TRANSFER_INTEGRATION=1")
    pytest.fail(_MISSING_URL_MESSAGE, pytrace=False)


# ── throwaway database harness (same shape as test_pgvector_search.py) ───────


def _with_database(url: str, dbname: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path=f"/{dbname}"))


def _asyncpg_dsn(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(parts._replace(scheme=parts.scheme.split("+", 1)[0]))


async def _run_maintenance(admin_url: str, statement: str) -> None:
    import asyncpg

    conn = await asyncpg.connect(_asyncpg_dsn(admin_url))
    try:
        await conn.execute(statement)
    finally:
        await conn.close()


def _admin_database_name(url: str) -> str:
    return unquote(urlsplit(url).path).lstrip("/").casefold()


@pytest.fixture(scope="module")
def migrated_database(_require_postgres):
    """Create a throwaway database on the admin server, migrate it, drop it."""
    admin_db = _admin_database_name(PGVECTOR_TEST_ADMIN_URL)
    if admin_db in FORBIDDEN_DB_NAMES:
        pytest.fail(
            "PGVECTOR_TEST_ADMIN_URL points at the production database name "
            f"{admin_db!r}. Point it at a throwaway server; this fixture "
            "creates and drops databases."
        )

    dbname = f"test_transfer_{uuid.uuid4().hex}"
    try:
        asyncio.run(
            _run_maintenance(PGVECTOR_TEST_ADMIN_URL, f'CREATE DATABASE "{dbname}"')
        )
        url = _with_database(PGVECTOR_TEST_ADMIN_URL, dbname)
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=ROOT,
            # SECRET_KEY has no usable default (the placeholder guard rejects
            # `changeme`), and a checkout under test need not have a `.env`, so
            # `alembic env.py`'s `import src.config` would abort before the
            # first migration ran.
            env={
                **os.environ,
                "DATABASE_URL": url,
                "SECRET_KEY": os.environ.get("SECRET_KEY") or "test-migration-key",
            },
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert result.returncode == 0, (
            f"alembic upgrade head failed\n{result.stdout}\n{result.stderr}"
        )
        yield url
    finally:
        try:
            asyncio.run(
                _run_maintenance(
                    PGVECTOR_TEST_ADMIN_URL,
                    f'DROP DATABASE IF EXISTS "{dbname}" (FORCE)',
                )
            )
        except Exception as e:  # pragma: no cover - cleanup best effort
            print(f"warning: could not drop throwaway database {dbname}: {e}")


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def engine(migrated_database):
    eng = create_async_engine(migrated_database)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def sessionmaker(engine):
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture(loop_scope="module")
async def clean(sessionmaker):
    """Empty every table this module writes to, before each test."""
    async with sessionmaker() as session:
        await session.execute(sa_delete(TransferToken))
        await session.execute(sa_delete(OAuthToken))
        await session.execute(sa_delete(OAuthClient))
        await session.execute(sa_delete(APIKey))
        await session.execute(sa_delete(User))
        await session.commit()
    return sessionmaker


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


async def _seed_identity(session, *, permission="readwrite", vault_path="/obsidian"):
    """A user with one API key and one OAuth access token."""
    user = User(
        username=f"u{uuid.uuid4().hex[:8]}",
        password_hash="x",
        is_active=True,
        vault_path=vault_path,
    )
    session.add(user)
    await session.flush()

    key = APIKey(
        user_id=user.id,
        name="k",
        key_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        key_prefix="omcp_test",
        permission=permission,
        is_active=True,
    )
    session.add(key)

    client = OAuthClient(
        user_id=user.id,
        client_id=uuid.uuid4().hex,
        client_secret_hash="x" * 64,
        client_name="c",
        redirect_uris=["https://example.com/cb"],
        scope="readwrite",
    )
    session.add(client)
    await session.flush()

    oauth = OAuthToken(
        user_id=user.id,
        token_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        token_type="access",
        client_id=client.client_id,
        scope="readwrite",
        expires_at=_now() + datetime.timedelta(hours=1),
        revoked=False,
    )
    session.add(oauth)
    await session.flush()
    await session.commit()
    return user, key, oauth


async def _add_token(session, *, key=None, oauth=None, user=None, direction="upload",
                     state="pending", path="Attachments/a.png", root="/obsidian",
                     ttl=600):
    row = TransferToken(
        token_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        direction=direction,
        state=state,
        path=path,
        vault_root=root,
        overwrite=False,
        key_id=key.id if key else None,
        oauth_token_id=oauth.id if oauth else None,
        user_id=user.id if user else None,
        expires_at=_now() + datetime.timedelta(seconds=ttl),
    )
    session.add(row)
    await session.commit()
    return row


# ── 1.1 ON DELETE CASCADE ───────────────────────────────────────────────────


async def test_deleting_an_api_key_removes_its_transfer_rows(clean):
    """The panel's per-key delete (`session.delete(api_key)`) must keep working."""
    async with clean() as session:
        user, key, oauth = await _seed_identity(session)
        await _add_token(session, key=key, user=user)
        await _add_token(session, key=key, user=user, state="completed")
        await _add_token(session, oauth=oauth, user=user, direction="download")

        await session.delete(key)
        await session.commit()

        remaining = (await session.execute(select(TransferToken))).scalars().all()
    assert len(remaining) == 1
    assert remaining[0].oauth_token_id == oauth.id


async def test_bulk_key_delete_removes_transfer_rows(clean):
    """The panel's expiry sweep uses a bulk `DELETE … WHERE id IN (…)`."""
    async with clean() as session:
        user, key, _ = await _seed_identity(session)
        await _add_token(session, key=key, user=user)

        await session.execute(sa_delete(APIKey).where(APIKey.id.in_([key.id])))
        await session.commit()

        assert (await session.execute(select(func.count()).select_from(TransferToken))).scalar_one() == 0


async def test_deleting_an_oauth_token_removes_its_transfer_rows(clean):
    async with clean() as session:
        user, _, oauth = await _seed_identity(session)
        await _add_token(session, oauth=oauth, user=user)

        await session.delete(oauth)
        await session.commit()

        assert (await session.execute(select(func.count()).select_from(TransferToken))).scalar_one() == 0


async def test_deleting_a_user_removes_every_transfer_row(clean):
    async with clean() as session:
        user, key, oauth = await _seed_identity(session)
        await _add_token(session, key=key, user=user)
        await _add_token(session, oauth=oauth, user=user, direction="download")

        await session.delete(user)
        await session.commit()

        assert (await session.execute(select(func.count()).select_from(TransferToken))).scalar_one() == 0


async def test_single_user_rows_have_no_identity_user(clean):
    """Single-user mode mints with `user_id IS NULL`; nothing cascades it away."""
    async with clean() as session:
        key = APIKey(
            name="k",
            key_hash=uuid.uuid4().hex + uuid.uuid4().hex,
            key_prefix="omcp_test",
            permission="readwrite",
            is_active=True,
        )
        session.add(key)
        await session.commit()
        row = await _add_token(session, key=key)
        assert row.user_id is None

        await session.delete(key)
        await session.commit()
        assert (await session.execute(select(func.count()).select_from(TransferToken))).scalar_one() == 0
