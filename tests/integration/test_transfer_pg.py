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

from src.models.db import APIKey, OAuthClient, OAuthToken, TransferToken, UsageLog, User
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
        # Before `api_keys`: `usage_logs.key_id` has no `ON DELETE` (a
        # pre-existing shape, unchanged by this work), so a usage-log row
        # written by an upload route blocks its key's delete.
        await session.execute(sa_delete(UsageLog))
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
        public_id=transfer.new_public_id(),
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
    # `mint_token` also returns the `MintWindow` it computed (the credential
    # clamp); the lifecycle tests care about the token and the row.
    token, row, _window = await transfer.mint_token(
        session,
        kwargs.pop("direction"),
        kwargs.pop("path", "Attachments/a.png"),
        identity=transfer.Identity(key_id=key.id, user_id=user.id),
        vault_root="/obsidian",
        **kwargs,
    )
    return token, row


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
            public_id=transfer.new_public_id(),
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
            public_id=transfer.new_public_id(),
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
        _, row, _window = await transfer.mint_token(
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
        _, row, _window = await transfer.mint_token(
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


# ════════════════════════════════════════════════════════════════════════════
# 4.7 — the same properties, driven through the real HTTP route
#
# The service-level tests above prove the primitives. These prove the *route*
# uses them correctly, which is a different claim: a handler that takes the
# right locks and then releases a claim it should not have released is green on
# every test above and still wrong. Only the claim's transaction boundary and
# the `FOR UPDATE` barrier make these meaningful, so they live here rather than
# in `tests/test_transfer_routes.py`.
# ════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def vault_root(tmp_path):
    from src.services import vault_fs

    (tmp_path / "Attachments").mkdir()
    vault_fs.reset_filesystem_probe_cache()
    yield tmp_path
    vault_fs.reset_filesystem_probe_cache()


@pytest.fixture
def wired(sessionmaker, monkeypatch):
    """Point the transfer routes at the throwaway database."""
    from src.limiter import limiter
    from src.transfer import routes as transfer_routes

    monkeypatch.setattr(transfer_routes, "async_session", sessionmaker)
    limiter.reset()
    yield sessionmaker
    limiter.reset()


def _client(ip: str):
    import httpx

    from src.main import app

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(ip, 1)),
        base_url="http://localhost:8000",
    )


async def _mint_upload(session, vault_root, *, path="Attachments/shot.png", permission="readwrite"):
    user, key, _ = await _seed_identity(
        session, permission=permission, vault_path=str(vault_root)
    )
    token, row, _window = await transfer.mint_token(
        session,
        "upload",
        path,
        overwrite=False,
        identity=transfer.Identity(key_id=key.id, user_id=user.id),
        vault_root=str(vault_root),
        expected_fingerprint=None,
    )
    return user, key, token, row


PAYLOAD = b"\x89PNG\r\n\x1a\n" + b"payload-bytes " * 64


async def test_info_reports_the_credential_clamped_expiry(clean, vault_root, wired):
    """The deadline the human is shown is the one redemption will honour.

    The whole point of clamping at mint rather than reporting two deadlines:
    `/transfer/upload/info` reads `transfer_tokens.expires_at` and knows
    nothing about credentials, so it is honest for free — but only if the
    column really holds `min(requested TTL, credential expiry)`. This mints
    with a 3600 s request against an OAuth access token with ~90 s left, the
    shape that used to be *always* divergent, and reads the deadline back off
    the wire.
    """
    async with clean() as session:
        user, _key, oauth = await _seed_identity(session, vault_path=str(vault_root))
        cutoff = _now() + datetime.timedelta(seconds=90)
        oauth.expires_at = cutoff
        await session.commit()

        token, row, window = await transfer.mint_token(
            session,
            "upload",
            "Attachments/shot.png",
            overwrite=False,
            identity=transfer.Identity(oauth_token_id=oauth.id, user_id=user.id),
            vault_root=str(vault_root),
            expected_fingerprint=None,
            expires_in=3600,
        )
    assert window.clamped is True
    assert row.expires_at == cutoff

    async with _client("203.0.113.31") as client:
        response = await client.get(
            "/transfer/upload/info", headers={"Authorization": f"Bearer {token}"}
        )
    assert response.status_code == 200, response.text
    assert datetime.datetime.fromisoformat(response.json()["expires_at"]) == cutoff


async def test_a_nearly_expired_credential_mints_nothing(clean, vault_root):
    """Under 30 s of runway: no row, so nothing exists to be shown or redeemed."""
    async with clean() as session:
        user, _key, oauth = await _seed_identity(session, vault_path=str(vault_root))
        oauth.expires_at = _now() + datetime.timedelta(seconds=5)
        await session.commit()

        with pytest.raises(transfer.CredentialTooShortLived):
            await transfer.mint_token(
                session,
                "upload",
                "Attachments/shot.png",
                overwrite=False,
                identity=transfer.Identity(oauth_token_id=oauth.id, user_id=user.id),
                vault_root=str(vault_root),
                expected_fingerprint=None,
            )
        await session.rollback()

    async with clean() as session:
        count = (
            await session.execute(select(func.count()).select_from(TransferToken))
        ).scalar_one()
    assert count == 0


async def test_a_scope_downgrade_before_the_insert_mints_nothing(clean, vault_root):
    """`mint_token` re-validates with the redemption predicate, in its own txn.

    A capability minted against a credential that has already lost `readwrite`
    could only ever 404, so the mint refuses rather than writing the row.
    """
    async with clean() as session:
        user, _key, oauth = await _seed_identity(session, vault_path=str(vault_root))
        oauth.scope = "read offline_access"
        await session.commit()
        # Plain ints: the ORM objects expire on the rollback below, and reading
        # an expired attribute would emit lazy IO outside the greenlet.
        identity = transfer.Identity(oauth_token_id=oauth.id, user_id=user.id)

        with pytest.raises(transfer.CredentialNotUsable):
            await transfer.mint_token(
                session,
                "upload",
                "Attachments/shot.png",
                overwrite=False,
                identity=identity,
                vault_root=str(vault_root),
                expected_fingerprint=None,
            )
        await session.rollback()

        # A *download* needs no write, so the same credential still mints one.
        _t, row, _w = await transfer.mint_token(
            session,
            "download",
            "Attachments/shot.png",
            overwrite=False,
            identity=identity,
            vault_root=str(vault_root),
            expected_fingerprint=None,
        )
        assert row.direction == "download"

    async with clean() as session:
        rows = (await session.execute(select(TransferToken))).scalars().all()
    assert [r.direction for r in rows] == ["download"]


# ── ownerless capabilities are nobody once multi-user mode is on ───────────


async def _ownerless_credentials(session):
    """An API key and an OAuth access token with `user_id IS NULL`.

    The shape a vault gets after running single-user for a while: rows minted
    when there were no users at all.
    """
    key = APIKey(
        user_id=None,
        name="k",
        key_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        key_prefix="omcp_test",
        permission="readwrite",
        is_active=True,
    )
    client = OAuthClient(
        user_id=None,
        client_id=uuid.uuid4().hex,
        client_secret_hash="x" * 64,
        client_name="c",
        redirect_uris=["https://example.com/cb"],
        scope="readwrite",
    )
    session.add_all([key, client])
    await session.flush()
    oauth = OAuthToken(
        user_id=None,
        token_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        token_type="access",
        client_id=client.client_id,
        scope="readwrite",
        expires_at=_now() + datetime.timedelta(hours=1),
        revoked=False,
    )
    session.add(oauth)
    await session.commit()
    return key, oauth


@pytest.mark.parametrize("direction", ["upload", "download"])
@pytest.mark.parametrize("credential", ["api_key", "oauth"])
async def test_an_ownerless_capability_dies_when_multi_user_is_enabled(
    clean, vault_root, monkeypatch, direction, credential
):
    """`None == None` used to pass the ownership check, authorising a global root.

    An ownerless key mints a capability in single-user mode; the operator then
    enables multi-user. The MCP middleware starts rejecting the key, but the
    already-minted capability was still redeemable — it compared
    `settings.vault_path` against its stored root and published. Fail closed:
    in multi-user mode an ownerless identity is nobody, and every predicate the
    routes consult says so.
    """
    monkeypatch.setattr(transfer.settings, "vault_path", str(vault_root))
    async with clean() as session:
        key, oauth = await _ownerless_credentials(session)
        identity = (
            transfer.Identity(key_id=key.id)
            if credential == "api_key"
            else transfer.Identity(oauth_token_id=oauth.id)
        )
        _token, row, _window = await transfer.mint_token(
            session,
            direction,
            "Attachments/shot.png",
            overwrite=False,
            identity=identity,
            vault_root=str(vault_root),
            expected_fingerprint=None,
        )
        assert row.user_id is None
        need_write = direction == "upload"

        # Single-user mode: still exactly as usable as it was.
        assert await transfer.resolve_identity_ok(session, row, need_write=need_write)
        assert await transfer.resolve_root_ok(session, row)

        monkeypatch.setattr(transfer.settings, "multi_user_mode", True)

        assert not await transfer.resolve_identity_ok(
            session, row, need_write=need_write
        )
        # Both halves of the defensive pair, not just the credential one.
        assert not await transfer.resolve_root_ok(session, row)
        cred = key if credential == "api_key" else oauth
        assert not transfer.locked_rows_ok(
            transfer.LockedRows(token=row, credential=cred, user=None),
            need_write=need_write,
        )

        # And nothing new can be minted from it either.
        with pytest.raises(transfer.CredentialNotUsable):
            await transfer.mint_token(
                session,
                direction,
                "Attachments/other.png",
                overwrite=False,
                identity=identity,
                vault_root=str(vault_root),
                expected_fingerprint=None,
            )
        await session.rollback()


async def test_an_ownerless_upload_token_gets_the_uniform_404(
    clean, vault_root, wired, monkeypatch
):
    """End to end: the PUT refuses and the target is never written."""
    monkeypatch.setattr(transfer.settings, "vault_path", str(vault_root))
    async with clean() as session:
        key, _oauth = await _ownerless_credentials(session)
        token, row, _window = await transfer.mint_token(
            session,
            "upload",
            "Attachments/shot.png",
            overwrite=False,
            identity=transfer.Identity(key_id=key.id),
            vault_root=str(vault_root),
            expected_fingerprint=None,
        )
        token_id = row.id

    monkeypatch.setattr(transfer.settings, "multi_user_mode", True)
    async with _client("203.0.113.40") as client:
        response = await client.put(
            "/transfer/upload",
            headers={"Authorization": f"Bearer {token}"},
            content=PAYLOAD,
        )
    assert response.status_code == 404
    assert response.json() == {"error": "not found"}
    assert not (vault_root / "Attachments" / "shot.png").exists()

    async with wired() as session:
        fresh = (
            await session.execute(
                select(TransferToken).where(TransferToken.id == token_id)
            )
        ).scalar_one()
        # Refused before publication, so the claim goes back rather than
        # stranding — the token is simply unusable while multi-user is on.
        assert fresh.state == "pending"


async def test_an_ownerless_download_token_gets_the_uniform_404(
    clean, vault_root, wired, monkeypatch
):
    from src.services import vault_fs as _vault_fs

    target = vault_root / "Attachments" / "spec.pdf"
    target.write_bytes(b"%PDF-1.4\n" + b"body " * 100)
    root_fd = _vault_fs.open_root(vault_root)
    try:
        dir_fd, name = _vault_fs.open_parent(root_fd, "Attachments/spec.pdf")
        try:
            fingerprint = _vault_fs.fingerprint(dir_fd, name, hash_up_to=10**9)
        finally:
            os.close(dir_fd)
    finally:
        os.close(root_fd)

    monkeypatch.setattr(transfer.settings, "vault_path", str(vault_root))
    async with clean() as session:
        _key, oauth = await _ownerless_credentials(session)
        token, _row, _window = await transfer.mint_token(
            session,
            "download",
            "Attachments/spec.pdf",
            overwrite=False,
            identity=transfer.Identity(oauth_token_id=oauth.id),
            vault_root=str(vault_root),
            expected_fingerprint=fingerprint,
        )

    headers = {"Authorization": f"Bearer {token}"}
    monkeypatch.setattr(transfer.settings, "multi_user_mode", True)
    async with _client("203.0.113.41") as client:
        info = await client.get("/transfer/download/info", headers=headers)
        got = await client.get("/transfer/download/file", headers=headers)
    assert info.status_code == got.status_code == 404
    assert got.json() == {"error": "not found"}


async def test_a_gate_delayed_past_the_deadline_consumes_the_token(
    clean, vault_root, wired, monkeypatch
):
    """The real gate, real locks: past the deadline nothing is published.

    `_drain` bounds the body; the gate runs after it and can wait arbitrarily
    long on `SELECT … FOR UPDATE`. The clock is stepped from inside
    `lock_for_publish` — between drain completion and the publish — and the
    request must end as a deadline overrun, which the state machine answers by
    consuming the token.
    """
    async with clean() as session:
        _user, _key, token, row = await _mint_upload(session, vault_root)
        token_id = row.id

    real_lock = transfer.lock_for_publish
    stepped = _now() + datetime.timedelta(hours=1)

    async def slow_lock(session, tid):
        monkeypatch.setattr(transfer, "now_utc", lambda: stepped)
        return await real_lock(session, tid)

    monkeypatch.setattr(transfer, "lock_for_publish", slow_lock)

    async with _client("203.0.113.42") as client:
        response = await client.put(
            "/transfer/upload",
            headers={"Authorization": f"Bearer {token}"},
            content=PAYLOAD,
        )
    assert response.status_code == 408
    assert not (vault_root / "Attachments" / "shot.png").exists()
    async with wired() as session:
        fresh = (
            await session.execute(
                select(TransferToken).where(TransferToken.id == token_id)
            )
        ).scalar_one()
    assert fresh.state == "consumed"


async def test_route_upload_round_trip(clean, vault_root, wired):
    async with clean() as session:
        _user, key, token, row = await _mint_upload(session, vault_root)

    async with _client("203.0.113.20") as client:
        response = await client.put(
            "/transfer/upload",
            headers={"Authorization": f"Bearer {token}"},
            content=PAYLOAD,
        )
    assert response.status_code == 200, response.text
    assert (vault_root / "Attachments" / "shot.png").read_bytes() == PAYLOAD

    async with wired() as session:
        fresh = (
            await session.execute(
                select(TransferToken)
                .where(TransferToken.id == row.id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        assert fresh.state == "completed"
        assert fresh.sha256 == response.json()["sha256"]
        logs = (await session.execute(select(UsageLog))).scalars().all()
    assert [log.tool for log in logs] == ["upload_file"]
    assert logs[0].key_id == key.id
    assert token not in str(logs[0].params)


async def test_two_concurrent_puts_yield_one_200_one_404_and_one_file(
    clean, vault_root, wired
):
    """The claim is a transaction boundary; only a real race can prove it."""
    async with clean() as session:
        _user, _key, token, _row = await _mint_upload(session, vault_root)

    headers = {"Authorization": f"Bearer {token}"}

    async def put(ip: str):
        async with _client(ip) as client:
            return await client.put("/transfer/upload", headers=headers, content=PAYLOAD)

    first, second = await asyncio.gather(put("203.0.113.21"), put("203.0.113.22"))
    statuses = sorted([first.status_code, second.status_code])
    assert statuses == [200, 404], (first.text, second.text)

    written = sorted(p.name for p in (vault_root / "Attachments").iterdir())
    assert written == ["shot.png"]
    assert (vault_root / "Attachments" / "shot.png").read_bytes() == PAYLOAD


async def test_a_failure_recording_the_completion_leaves_the_token_claimed(
    clean, vault_root, wired, monkeypatch
):
    """The bytes are published; the row must stay `claimed`, never `pending`.

    Releasing here would hand back a token that replays over a path already
    holding the upload — the one outcome worse than a stuck capability.
    """
    async with clean() as session:
        _user, _key, token, row = await _mint_upload(session, vault_root)

    async def exploding(*args, **kwargs):
        raise RuntimeError("connection reset while committing")

    monkeypatch.setattr(transfer, "complete_upload", exploding)

    async with _client("203.0.113.23") as client:
        with pytest.raises(transfer.PostPublishFailure):
            await client.put(
                "/transfer/upload",
                headers={"Authorization": f"Bearer {token}"},
                content=PAYLOAD,
            )

    assert (vault_root / "Attachments" / "shot.png").read_bytes() == PAYLOAD
    async with wired() as session:
        fresh = (
            await session.execute(
                select(TransferToken)
                .where(TransferToken.id == row.id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
    assert fresh.state == "claimed"
    assert fresh.completed_at is None


async def _gated_body(gate: asyncio.Event, released: asyncio.Event):
    """A body that hands control back mid-stream so a test can mutate the DB."""
    yield PAYLOAD[:16]
    released.set()
    await asyncio.wait_for(gate.wait(), timeout=10)
    yield PAYLOAD[16:]


async def test_cascade_deleting_the_key_mid_upload_publishes_nothing(
    clean, vault_root, wired
):
    async with clean() as session:
        _user, key, token, row = await _mint_upload(session, vault_root)

    gate, released = asyncio.Event(), asyncio.Event()

    async def upload():
        async with _client("203.0.113.24") as client:
            return await client.put(
                "/transfer/upload",
                headers={"Authorization": f"Bearer {token}"},
                content=_gated_body(gate, released),
            )

    task = asyncio.create_task(upload())
    await asyncio.wait_for(released.wait(), timeout=10)
    async with wired() as session:
        # Cascades the transfer row away with the key.
        await session.execute(sa_delete(APIKey).where(APIKey.id == key.id))
        await session.commit()
    gate.set()

    response = await asyncio.wait_for(task, timeout=20)
    assert response.status_code == 404
    assert response.json() == {"error": "not found"}
    assert not (vault_root / "Attachments" / "shot.png").exists()
    async with wired() as session:
        assert (
            await session.execute(select(func.count()).select_from(TransferToken))
        ).scalar_one() == 0


async def test_revocation_mid_upload_never_publishes(clean, vault_root, wired):
    """The locked re-check runs against the committed revocation, not a cache."""
    async with clean() as session:
        _user, key, token, row = await _mint_upload(session, vault_root)

    gate, released = asyncio.Event(), asyncio.Event()

    async def upload():
        async with _client("203.0.113.25") as client:
            return await client.put(
                "/transfer/upload",
                headers={"Authorization": f"Bearer {token}"},
                content=_gated_body(gate, released),
            )

    task = asyncio.create_task(upload())
    await asyncio.wait_for(released.wait(), timeout=10)
    async with wired() as session:
        await session.execute(
            update(APIKey).where(APIKey.id == key.id).values(is_active=False)
        )
        await session.commit()
    gate.set()

    response = await asyncio.wait_for(task, timeout=20)
    assert response.status_code == 404
    assert not (vault_root / "Attachments" / "shot.png").exists()
    async with wired() as session:
        fresh = (
            await session.execute(
                select(TransferToken)
                .where(TransferToken.id == row.id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
    # Nothing was published, so the claim goes back: the link still works if
    # the key is reinstated.
    assert fresh.state == "pending"


async def test_a_permission_downgrade_mid_upload_never_publishes(
    clean, vault_root, wired
):
    async with clean() as session:
        _user, key, token, _row = await _mint_upload(session, vault_root)

    gate, released = asyncio.Event(), asyncio.Event()

    async def upload():
        async with _client("203.0.113.26") as client:
            return await client.put(
                "/transfer/upload",
                headers={"Authorization": f"Bearer {token}"},
                content=_gated_body(gate, released),
            )

    task = asyncio.create_task(upload())
    await asyncio.wait_for(released.wait(), timeout=10)
    async with wired() as session:
        await session.execute(
            update(APIKey).where(APIKey.id == key.id).values(permission="read")
        )
        await session.commit()
    gate.set()

    response = await asyncio.wait_for(task, timeout=20)
    assert response.status_code == 404
    assert not (vault_root / "Attachments" / "shot.png").exists()


async def test_root_reassignment_is_a_404_not_a_500(clean, vault_root, wired, tmp_path):
    """Read from the database, and never let a reassignment become a 500."""
    async with clean() as session:
        user, _key, token, _row = await _mint_upload(session, vault_root)

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    async with wired() as session:
        await session.execute(
            update(User).where(User.id == user.id).values(vault_path=str(elsewhere))
        )
        await session.commit()

    headers = {"Authorization": f"Bearer {token}"}
    async with _client("203.0.113.27") as client:
        info = await client.get("/transfer/upload/info", headers=headers)
        put = await client.put("/transfer/upload", headers=headers, content=PAYLOAD)

    assert info.status_code == 404
    assert put.status_code == 404
    assert info.json() == put.json() == {"error": "not found"}
    assert not (vault_root / "Attachments" / "shot.png").exists()
    assert list(elsewhere.iterdir()) == []


async def test_root_reassignment_mid_upload_publishes_nothing(
    clean, vault_root, wired, tmp_path
):
    async with clean() as session:
        user, _key, token, _row = await _mint_upload(session, vault_root)

    elsewhere = tmp_path / "elsewhere2"
    elsewhere.mkdir()
    gate, released = asyncio.Event(), asyncio.Event()

    async def upload():
        async with _client("203.0.113.28") as client:
            return await client.put(
                "/transfer/upload",
                headers={"Authorization": f"Bearer {token}"},
                content=_gated_body(gate, released),
            )

    task = asyncio.create_task(upload())
    await asyncio.wait_for(released.wait(), timeout=10)
    async with wired() as session:
        await session.execute(
            update(User).where(User.id == user.id).values(vault_path=str(elsewhere))
        )
        await session.commit()
    gate.set()

    response = await asyncio.wait_for(task, timeout=20)
    assert response.status_code == 404
    assert not (vault_root / "Attachments" / "shot.png").exists()
    assert list(elsewhere.iterdir()) == []


async def test_a_replayed_token_is_a_404(clean, vault_root, wired):
    async with clean() as session:
        _user, _key, token, _row = await _mint_upload(session, vault_root)

    headers = {"Authorization": f"Bearer {token}"}
    async with _client("203.0.113.29") as client:
        first = await client.put("/transfer/upload", headers=headers, content=PAYLOAD)
        second = await client.put("/transfer/upload", headers=headers, content=PAYLOAD)
        info = await client.get("/transfer/upload/info", headers=headers)

    assert first.status_code == 200
    assert second.status_code == 404
    assert info.status_code == 404
    assert (vault_root / "Attachments" / "shot.png").read_bytes() == PAYLOAD


async def test_download_round_trip_through_the_route(clean, vault_root, wired):
    from src.services import vault_fs

    target = vault_root / "Attachments" / "spec.pdf"
    target.write_bytes(b"%PDF-1.4\n" + b"body " * 100)

    root_fd = vault_fs.open_root(vault_root)
    try:
        dir_fd, name = vault_fs.open_parent(root_fd, "Attachments/spec.pdf")
        try:
            fingerprint = vault_fs.fingerprint(dir_fd, name, hash_up_to=10**9)
        finally:
            os.close(dir_fd)
    finally:
        os.close(root_fd)

    async with clean() as session:
        user, key, _ = await _seed_identity(session, vault_path=str(vault_root))
        token, _row, _window = await transfer.mint_token(
            session,
            "download",
            "Attachments/spec.pdf",
            overwrite=False,
            identity=transfer.Identity(key_id=key.id, user_id=user.id),
            vault_root=str(vault_root),
            expected_fingerprint=fingerprint,
        )

    headers = {"Authorization": f"Bearer {token}"}
    async with _client("203.0.113.30") as client:
        got = await client.get("/transfer/download/file", headers=headers)
        # Multi-use within the TTL, unlike an upload token.
        again = await client.get("/transfer/download/file", headers=headers)

    assert got.status_code == again.status_code == 200
    assert got.content == target.read_bytes()
    async with wired() as session:
        logs = (await session.execute(select(UsageLog))).scalars().all()
    assert [log.tool for log in logs] == ["download_file", "download_file"]
    assert all(log.user_id == user.id for log in logs)


# ════════════════════════════════════════════════════════════════════════════
# 5.4 — `import_from_url` publishes through a locked identity gate
#
# The import tool has no capability to re-validate: it is authorised by the
# caller's own MCP session. But the fetch holds a network stream open for up to
# 30 s, which is ample time for the key to be revoked, downgraded, or pointed at
# another vault — and without a gate the bytes would land under whatever the
# identity looked like when the tool started. Only real row locks can show that
# the revocation either waits for the publisher or wins outright.
# ════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def import_caller(vault_root, sessionmaker, monkeypatch):
    """Run `import_from_url` as a seeded identity against the throwaway database."""
    import src.mcp_server.tools as tools
    from src.auth.session import current_user_id
    from src.mcp_server.auth import current_api_key_id, current_permission
    from src.services import vault as vault_service

    monkeypatch.setattr(tools, "async_session", sessionmaker)
    monkeypatch.setattr(tools.settings, "mcp_hostname", "vault.example.com")
    monkeypatch.setattr(tools.settings, "base_url", "https://vault.example.com")
    monkeypatch.setattr(tools.settings, "_public_origin_explicit", True)

    def become(user, key):
        # The tools read identity from contextvars, exactly as the MCP auth
        # middleware sets them, and the vault root from the warmed cache. No
        # reset on teardown: pytest-asyncio runs each test coroutine in its own
        # task, which copies the context, so a `set` in here cannot escape into
        # the next test — and a `reset` from the fixture's (different) context
        # is an error, not a cleanup.
        vault_service._user_vault_cache[user.id] = Path(vault_root)
        current_permission.set(key.permission)
        current_api_key_id.set(key.id)
        current_user_id.set(user.id)

    yield become

    vault_service.clear_user_vault_cache()


def _paused_fetch(gate: asyncio.Event, released: asyncio.Event):
    """A canned origin whose body stalls once, so the test can mutate the DB."""
    from contextlib import asynccontextmanager

    async def body():
        yield PAYLOAD[:16]
        released.set()
        await asyncio.wait_for(gate.wait(), timeout=10)
        yield PAYLOAD[16:]

    @asynccontextmanager
    async def fake_fetch(url, **kwargs):
        yield transfer.FetchResult(
            chunks=body(), final_url=url, content_type="image/png"
        )

    return fake_fetch


async def _start_import(clean, vault_root, import_caller, monkeypatch):
    """Seed an identity, start an import, and pause it mid-body.

    Returns `(task, gate, user, key)`. The caller mutates the identity in the
    database, sets `gate`, and awaits the task — so every mutation lands while
    the tool is between its first and last chunk, i.e. before the publish.
    """
    import src.mcp_server.tools as tools

    async with clean() as session:
        user, key, _ = await _seed_identity(session, vault_path=str(vault_root))
    import_caller(user, key)

    gate, released = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(transfer, "fetch_url_guarded", _paused_fetch(gate, released))

    task = asyncio.create_task(
        tools.import_from_url_impl("https://example.com/a.png", "Attachments/a.png")
    )
    await asyncio.wait_for(released.wait(), timeout=10)
    return task, gate, user, key


async def test_import_publishes_nothing_when_the_key_is_revoked_mid_fetch(
    clean, vault_root, wired, import_caller, monkeypatch
):
    task, gate, _user, key = await _start_import(
        clean, vault_root, import_caller, monkeypatch
    )
    async with wired() as session:
        await session.execute(
            update(APIKey).where(APIKey.id == key.id).values(is_active=False)
        )
        await session.commit()
    gate.set()

    result = await asyncio.wait_for(task, timeout=20)
    assert "no longer valid" in result
    assert not (vault_root / "Attachments" / "a.png").exists()
    staging = vault_root / ".transfer-tmp"
    assert not staging.exists() or list(staging.iterdir()) == []


async def test_import_publishes_nothing_when_the_key_is_downgraded_mid_fetch(
    clean, vault_root, wired, import_caller, monkeypatch
):
    task, gate, _user, key = await _start_import(
        clean, vault_root, import_caller, monkeypatch
    )
    async with wired() as session:
        await session.execute(
            update(APIKey).where(APIKey.id == key.id).values(permission="read")
        )
        await session.commit()
    gate.set()

    result = await asyncio.wait_for(task, timeout=20)
    assert "no longer valid" in result
    assert not (vault_root / "Attachments" / "a.png").exists()


async def test_import_publishes_nothing_when_the_root_is_reassigned_mid_fetch(
    clean, vault_root, wired, import_caller, monkeypatch, tmp_path
):
    """The gate compares the *database's* root with the one the tool started on."""
    task, gate, user, _key = await _start_import(
        clean, vault_root, import_caller, monkeypatch
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    async with wired() as session:
        await session.execute(
            update(User).where(User.id == user.id).values(vault_path=str(elsewhere))
        )
        await session.commit()
    gate.set()

    result = await asyncio.wait_for(task, timeout=20)
    assert "no longer valid" in result
    assert not (vault_root / "Attachments" / "a.png").exists()
    assert list(elsewhere.iterdir()) == []


async def test_import_completes_under_an_untouched_identity(
    clean, vault_root, wired, import_caller, monkeypatch
):
    """The gate is a guard, not a wall: an unchanged identity still publishes."""
    task, gate, _user, _key = await _start_import(
        clean, vault_root, import_caller, monkeypatch
    )
    gate.set()

    result = await asyncio.wait_for(task, timeout=20)
    assert "Imported" in result
    assert (vault_root / "Attachments" / "a.png").read_bytes() == PAYLOAD
