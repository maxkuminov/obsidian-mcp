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
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.models.db import APIKey, OAuthClient, OAuthToken, TransferToken, User
from src.services import transfer

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


# ── 3.1 token lifecycle against a real database ─────────────────────────────


async def _mint(session, user, key, **kwargs):
    kwargs.setdefault("direction", "upload")
    kwargs.setdefault("overwrite", False)
    kwargs.setdefault("expected_fingerprint", None)
    kwargs.setdefault("expires_in", None)
    return await transfer.mint_token(
        session,
        kwargs.pop("direction"),
        kwargs.pop("path", "Attachments/a.png"),
        identity=transfer.Identity(key_id=key.id, user_id=user.id),
        vault_root="/obsidian",
        **kwargs,
    )


async def test_mint_stores_only_a_hash(clean):
    async with clean() as session:
        user, key, _ = await _seed_identity(session)
        token, row = await _mint(session, user, key)

    assert row.token_hash == transfer.hash_token(token)
    assert token not in row.token_hash
    # Nothing anywhere in the row's own columns carries the secret.
    stored = {c.name: getattr(row, c.name) for c in TransferToken.__table__.columns}
    assert not any(token in str(v) for v in stored.values())
    assert row.state == "pending"


@pytest.mark.parametrize("requested,expected", [(5, 60), (99999, 3600), (None, 600)])
async def test_mint_clamps_the_ttl(clean, requested, expected):
    async with clean() as session:
        user, key, _ = await _seed_identity(session)
        _, row = await _mint(session, user, key, expires_in=requested)
        lifetime = (row.expires_at - row.created_at).total_seconds()
    # `created_at` is a server default and `expires_at` an app clock, so allow
    # a couple of seconds of skew rather than asserting an exact equality that
    # would flake on a slow box.
    assert abs(lifetime - expected) < 5


async def test_mint_prunes_long_dead_rows(clean):
    async with clean() as session:
        user, key, _ = await _seed_identity(session)
        stale = TransferToken(
            token_hash="0" * 64,
            direction="upload",
            state="completed",
            path="old.png",
            vault_root="/obsidian",
            overwrite=False,
            key_id=key.id,
            user_id=user.id,
            expires_at=_now() - datetime.timedelta(days=3),
        )
        recent = TransferToken(
            token_hash="1" * 64,
            direction="upload",
            state="completed",
            path="recent.png",
            vault_root="/obsidian",
            overwrite=False,
            key_id=key.id,
            user_id=user.id,
            expires_at=_now() - datetime.timedelta(hours=1),
        )
        session.add_all([stale, recent])
        await session.commit()

        await _mint(session, user, key)
        paths = set(
            (await session.execute(select(TransferToken.path))).scalars().all()
        )
    assert "old.png" not in paths, "a row three days past expiry should be pruned"
    assert "recent.png" in paths, "an hour past expiry is inside the grace window"


async def test_claim_moves_pending_to_claimed(clean):
    async with clean() as session:
        user, key, _ = await _seed_identity(session)
        token, row = await _mint(session, user, key)

        claimed = await transfer.claim_upload(session, token)
        assert claimed is not None
        assert claimed.state == "claimed"
        assert claimed.claimed_at is not None

        # Replay of the same token finds nothing.
        assert await transfer.claim_upload(session, token) is None


async def test_claim_rejects_the_wrong_direction(clean):
    async with clean() as session:
        user, key, _ = await _seed_identity(session)
        token, _ = await _mint(session, user, key, direction="download")
        assert await transfer.claim_upload(session, token) is None


async def test_claim_rejects_an_expired_token(clean):
    async with clean() as session:
        user, key, _ = await _seed_identity(session)
        token, row = await _mint(session, user, key)
        await session.execute(
            update(TransferToken)
            .where(TransferToken.id == row.id)
            .values(expires_at=_now() - datetime.timedelta(seconds=1))
        )
        await session.commit()
        assert await transfer.claim_upload(session, token) is None


async def test_claim_rejects_an_unknown_token(clean):
    async with clean() as session:
        assert await transfer.claim_upload(session, "not-a-real-token") is None


async def test_concurrent_claims_produce_exactly_one_winner(clean, sessionmaker):
    """The single-use property, proved where it actually lives.

    Two tasks release from a barrier into the same conditional UPDATE. Postgres
    serialises them on the row, so the second sees `state='claimed'` and matches
    nothing. A fake session has no such serialisation to test against — which is
    why this case cannot live in the unit suite.
    """
    async with clean() as session:
        user, key, _ = await _seed_identity(session)
        token, _ = await _mint(session, user, key)

    racers = 3
    barrier = asyncio.Barrier(racers)

    async def attempt():
        async with sessionmaker() as s:
            # Open the session (and its connection) first, then release
            # together: the barrier has to gate the UPDATE, not the connect.
            await s.execute(select(1))
            await barrier.wait()
            return await transfer.claim_upload(s, token)

    tasks = [asyncio.create_task(attempt()) for _ in range(racers)]
    results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=30)

    winners = [r for r in results if r is not None]
    assert len(winners) == 1, f"{len(winners)} claims succeeded; single-use is broken"


async def test_release_returns_the_claim_and_allows_a_retry(clean):
    async with clean() as session:
        user, key, _ = await _seed_identity(session)
        token, _ = await _mint(session, user, key)
        claimed = await transfer.claim_upload(session, token)

        assert await transfer.release_claim(session, claimed) is True
        again = await transfer.claim_upload(session, token)
        assert again is not None, "a released claim must be redeemable again"
        assert again.id == claimed.id


async def test_consume_burns_the_claim_permanently(clean):
    async with clean() as session:
        user, key, _ = await _seed_identity(session)
        token, _ = await _mint(session, user, key)
        claimed = await transfer.claim_upload(session, token)

        assert await transfer.consume(session, claimed) is True
        assert await transfer.claim_upload(session, token) is None
        assert await transfer.lookup_upload(session, token) is None
        # Releasing afterwards must not resurrect it.
        assert await transfer.release_claim(session, claimed) is False


async def test_complete_records_the_result_and_is_not_replayable(clean):
    async with clean() as session:
        user, key, _ = await _seed_identity(session)
        token, _ = await _mint(session, user, key)
        claimed = await transfer.claim_upload(session, token)

        assert await transfer.complete_upload(
            session, claimed, 42, "a" * 64, "image/png"
        ) is True
        fresh = (
            await session.execute(
                select(TransferToken).where(TransferToken.id == claimed.id)
            )
        ).scalar_one()
        assert fresh.state == "completed"
        assert (fresh.size, fresh.sha256, fresh.mime) == (42, "a" * 64, "image/png")
        assert fresh.completed_at is not None
        assert await transfer.claim_upload(session, token) is None


async def test_lookup_only_returns_usable_tokens(clean):
    async with clean() as session:
        user, key, _ = await _seed_identity(session)
        dl_token, dl_row = await _mint(session, user, key, direction="download")
        assert await transfer.lookup_download(session, dl_token) is not None
        # Downloads are multi-use within the TTL.
        assert await transfer.lookup_download(session, dl_token) is not None
        # ...but not visible on the upload side.
        assert await transfer.lookup_upload(session, dl_token) is None

        up_token, _ = await _mint(session, user, key)
        claimed = await transfer.claim_upload(session, up_token)
        assert await transfer.lookup_upload(session, up_token) is None
        await transfer.complete_upload(session, claimed, 1, "b" * 64, "text/plain")
        assert await transfer.lookup_upload(session, up_token) is None


async def test_fingerprint_round_trips_through_jsonb(clean):
    """`mtime_ns` is ~1.7e18 — bigger than a float64 can hold exactly."""
    fp = {
        "dev": 2049,
        "inode": 123456789,
        "size": 1024,
        "mtime_ns": 1755302400123456789,
        "ctime_ns": 1755302400987654321,
        "sha256": "c" * 64,
    }
    async with clean() as session:
        user, key, _ = await _seed_identity(session)
        _, row = await _mint(session, user, key, expected_fingerprint=fp)
        session.expunge_all()
        fresh = (
            await session.execute(
                select(TransferToken).where(TransferToken.id == row.id)
            )
        ).scalar_one()
    assert fresh.expected_fingerprint == fp


# ── 3.1 identity and root predicates ────────────────────────────────────────


async def test_identity_ok_for_a_healthy_readwrite_key(clean):
    async with clean() as session:
        user, key, _ = await _seed_identity(session)
        _, row = await _mint(session, user, key)
        assert await transfer.resolve_identity_ok(session, row, need_write=True) is True


async def test_identity_not_ok_for_a_revoked_key(clean):
    async with clean() as session:
        user, key, _ = await _seed_identity(session)
        _, row = await _mint(session, user, key)
        key.is_active = False
        await session.commit()
        assert await transfer.resolve_identity_ok(session, row, need_write=True) is False


async def test_identity_not_ok_for_an_expired_key(clean):
    async with clean() as session:
        user, key, _ = await _seed_identity(session)
        _, row = await _mint(session, user, key)
        key.expires_at = _now() - datetime.timedelta(seconds=1)
        await session.commit()
        assert await transfer.resolve_identity_ok(session, row, need_write=True) is False


async def test_downgraded_permission_blocks_write_but_not_read(clean):
    async with clean() as session:
        user, key, _ = await _seed_identity(session)
        _, row = await _mint(session, user, key)
        key.permission = "read"
        await session.commit()
        assert await transfer.resolve_identity_ok(session, row, need_write=True) is False
        assert await transfer.resolve_identity_ok(session, row, need_write=False) is True


async def test_identity_not_ok_for_an_inactive_user(clean):
    async with clean() as session:
        user, key, _ = await _seed_identity(session)
        _, row = await _mint(session, user, key)
        user.is_active = False
        await session.commit()
        assert await transfer.resolve_identity_ok(session, row, need_write=True) is False


async def test_oauth_predicates(clean):
    async with clean() as session:
        user, _, oauth = await _seed_identity(session)
        _, row = await transfer.mint_token(
            session,
            "upload",
            "Attachments/a.png",
            overwrite=False,
            identity=transfer.Identity(oauth_token_id=oauth.id, user_id=user.id),
            vault_root="/obsidian",
            expected_fingerprint=None,
        )
        assert await transfer.resolve_identity_ok(session, row, need_write=True) is True

        oauth.scope = "read"
        await session.commit()
        assert await transfer.resolve_identity_ok(session, row, need_write=True) is False
        assert await transfer.resolve_identity_ok(session, row, need_write=False) is True

        oauth.scope = "readwrite"
        oauth.revoked = True
        await session.commit()
        assert await transfer.resolve_identity_ok(session, row, need_write=False) is False

        oauth.revoked = False
        oauth.expires_at = _now() - datetime.timedelta(seconds=1)
        await session.commit()
        assert await transfer.resolve_identity_ok(session, row, need_write=False) is False


async def test_a_key_reassigned_to_another_user_loses_the_capability(clean):
    """The credential must still belong to the user the token was minted for."""
    async with clean() as session:
        user, key, _ = await _seed_identity(session)
        other = User(username=f"o{uuid.uuid4().hex[:8]}", password_hash="x",
                     is_active=True, vault_path="/obsidian")
        session.add(other)
        await session.flush()
        _, row = await _mint(session, user, key)
        key.user_id = other.id
        await session.commit()
        assert await transfer.resolve_identity_ok(session, row, need_write=True) is False


async def test_root_ok_reads_the_database_not_the_process_cache(clean, monkeypatch):
    from src.services import vault

    async with clean() as session:
        user, key, _ = await _seed_identity(session, vault_path="/obsidian")
        _, row = await _mint(session, user, key)
        assert await transfer.resolve_root_ok(session, row) is True

        # Reassign in the database and leave a *stale* process cache behind,
        # exactly as another worker would have. The check must not believe it.
        vault._user_vault_cache[user.id] = Path("/obsidian")
        user.vault_path = "/vaults/elsewhere"
        await session.commit()
        try:
            assert await transfer.resolve_root_ok(session, row) is False
        finally:
            vault.clear_user_vault_cache()


async def test_root_ok_is_false_for_an_inactive_or_unassigned_user(clean):
    async with clean() as session:
        user, key, _ = await _seed_identity(session, vault_path="/obsidian")
        _, row = await _mint(session, user, key)
        user.is_active = False
        await session.commit()
        assert await transfer.resolve_root_ok(session, row) is False

        user.is_active = True
        user.vault_path = None
        await session.commit()
        assert await transfer.resolve_root_ok(session, row) is False


async def test_single_user_root_compares_against_settings(clean, monkeypatch):
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
        monkeypatch.setattr(transfer.settings, "vault_path", "/obsidian")
        _, row = await transfer.mint_token(
            session,
            "upload",
            "a.png",
            overwrite=False,
            identity=transfer.Identity(key_id=key.id),
            vault_root="/obsidian",
            expected_fingerprint=None,
        )
        assert await transfer.resolve_root_ok(session, row) is True
        monkeypatch.setattr(transfer.settings, "vault_path", "/vaults/other")
        assert await transfer.resolve_root_ok(session, row) is False


# ── 3.1 the pre-publication lock ────────────────────────────────────────────


async def test_lock_for_publish_returns_the_three_rows(clean):
    async with clean() as session:
        user, key, _ = await _seed_identity(session)
        token, _ = await _mint(session, user, key)
        claimed = await transfer.claim_upload(session, token)

        async with session.begin():
            locked = await transfer.lock_for_publish(session, claimed.id)
            assert locked is not None
            assert locked.token.id == claimed.id
            assert locked.credential.id == key.id
            assert locked.user.id == user.id
            assert transfer.locked_rows_ok(locked, need_write=True) is True


async def test_lock_for_publish_declines_an_unclaimed_or_missing_token(clean):
    async with clean() as session:
        user, key, _ = await _seed_identity(session)
        _, row = await _mint(session, user, key)
        row_id = row.id

    # Fresh sessions: `lock_for_publish` is meant to be called at the start of
    # its own transaction, which is also how the route uses it.
    async with clean() as session, session.begin():
        assert await transfer.lock_for_publish(session, row_id) is None
    async with clean() as session, session.begin():
        assert await transfer.lock_for_publish(session, 10**9) is None


async def test_lock_for_publish_blocks_a_racing_revocation(clean, sessionmaker):
    """A revocation committed after the lock is taken must wait for the publisher.

    This is the property the design buys with `SELECT … FOR UPDATE` held across
    the filesystem publish: there is no interleaving in which the publisher sees
    a valid key, the revocation commits, and the bytes then land.
    """
    async with clean() as session:
        user, key, _ = await _seed_identity(session)
        token, _ = await _mint(session, user, key)
        claimed = await transfer.claim_upload(session, token)

    revocation_committed = asyncio.Event()

    async def revoke():
        async with sessionmaker() as s:
            await s.execute(
                update(APIKey).where(APIKey.id == key.id).values(is_active=False)
            )
            await s.commit()
            revocation_committed.set()

    async with sessionmaker() as publisher:
        async with publisher.begin():
            locked = await transfer.lock_for_publish(publisher, claimed.id)
            assert locked is not None
            assert transfer.locked_rows_ok(locked, need_write=True) is True

            task = asyncio.create_task(revoke())
            # The revocation needs the same row lock, so it cannot commit while
            # we hold it. If this ever stops being true the sleep is generous
            # enough that the event would be set.
            await asyncio.sleep(0.5)
            assert not revocation_committed.is_set(), (
                "a revocation committed while the publisher held the row lock"
            )
        await asyncio.wait_for(task, timeout=10)

    # And after the publisher's transaction ends, the revocation lands.
    async with sessionmaker() as s:
        fresh = (
            await s.execute(select(APIKey).where(APIKey.id == key.id))
        ).scalar_one()
        assert fresh.is_active is False


async def test_claim_and_publish_lock_do_not_deadlock_on_a_single_connection(
    migrated_database,
):
    """Pool-size-1 sanity: the two transactions must not need each other's connection."""
    engine = create_async_engine(migrated_database, pool_size=1, max_overflow=0)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with maker() as session:
            await session.execute(sa_delete(TransferToken))
            await session.commit()
            user, key, _ = await _seed_identity(session)
            token, _ = await _mint(session, user, key)
            claimed = await transfer.claim_upload(session, token)
            async with session.begin():
                locked = await asyncio.wait_for(
                    transfer.lock_for_publish(session, claimed.id), timeout=10
                )
                assert locked is not None
                await transfer.complete_upload(
                    session, locked.token, 3, "d" * 64, "text/plain", commit=False
                )
            fresh = (
                await session.execute(
                    select(TransferToken).where(TransferToken.id == claimed.id)
                )
            ).scalar_one()
            assert fresh.state == "completed"
    finally:
        await engine.dispose()
