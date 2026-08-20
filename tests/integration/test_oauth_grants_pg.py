"""Real-Postgres gate for the OAuth grant-family concurrency properties.

Three of these have no meaningful fake-session equivalent, because what they
assert *is* the database's behaviour:

* **Revocation vs rotation.** The failure is a snapshot artefact — an
  `UPDATE ... WHERE grant_id = :g` cannot see rows a concurrent refresh
  inserts after its statement began, so the panel reports "revoked" while the
  client keeps the pair it just rotated into. A fake has no snapshot to be
  wrong about; only two real transactions racing on a real lock manager show
  whether the advisory lock actually orders them.
* **Concurrent first consent.** `UPDATE oauth_clients SET user_id = :u WHERE
  user_id IS NULL RETURNING client_id` is only an arbitrator because Postgres
  blocks the loser on the row lock and re-evaluates the predicate. In a fake
  the two writes simply happen in program order.
* **Client-authenticated revocation** is included because it is cheap here and
  exercises the same rows end to end.

Skipped unless `PGVECTOR_TEST_ADMIN_URL` names a throwaway Postgres *server*
(the module creates and drops its own database):

    docker run --rm -d --name pgvector-test -e POSTGRES_PASSWORD=test \\
        -p 55432:5432 pgvector/pgvector:pg16
    PGVECTOR_TEST_ADMIN_URL=postgresql+asyncpg://postgres:test@localhost:55432/postgres \\
        pytest -q tests/integration/test_oauth_grants_pg.py
    docker rm -f pgvector-test
"""
import asyncio
import os
import secrets
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit

import pytest
import pytest_asyncio
from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.models.db import OAuthClient, OAuthCode, OAuthToken, UsageLog, User
from src.oauth import routes as oauth

PGVECTOR_TEST_ADMIN_URL = os.environ.get("PGVECTOR_TEST_ADMIN_URL")
FORBIDDEN_DB_NAMES = {"obsidian_mcp"}
ROOT = Path(__file__).resolve().parent.parent.parent

pytestmark = [
    pytest.mark.asyncio(loop_scope="module"),
    pytest.mark.skipif(
        not PGVECTOR_TEST_ADMIN_URL,
        reason="PGVECTOR_TEST_ADMIN_URL not set",
    ),
]

REDIRECT_URI = "https://client.example.com/callback"
VERIFIER = "v" * 64
SECRET = "s" * 64
FUTURE = datetime(2099, 1, 1, tzinfo=timezone.utc)


# ── throwaway database harness (same shape as test_transfer_pg.py) ──────────


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


@pytest.fixture(scope="module")
def migrated_database():
    admin_db = unquote(urlsplit(PGVECTOR_TEST_ADMIN_URL).path).lstrip("/").casefold()
    if admin_db in FORBIDDEN_DB_NAMES:
        pytest.fail(
            "PGVECTOR_TEST_ADMIN_URL points at the production database name; "
            "this fixture creates and drops databases."
        )
    dbname = f"test_grants_{uuid.uuid4().hex}"
    try:
        asyncio.run(
            _run_maintenance(PGVECTOR_TEST_ADMIN_URL, f'CREATE DATABASE "{dbname}"')
        )
        url = _with_database(PGVECTOR_TEST_ADMIN_URL, dbname)
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=ROOT,
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
async def clean(sessionmaker, monkeypatch_module):
    """Empty the tables this module writes to, and point the handlers here."""
    async with sessionmaker() as session:
        await session.execute(sa_delete(UsageLog))
        await session.execute(sa_delete(OAuthToken))
        await session.execute(sa_delete(OAuthCode))
        await session.execute(sa_delete(OAuthClient))
        await session.execute(sa_delete(User))
        await session.commit()
    # The token endpoint opens its own sessions through this module global.
    monkeypatch_module.setattr(oauth, "async_session", sessionmaker)
    yield sessionmaker


@pytest.fixture(scope="module")
def monkeypatch_module():
    mp = pytest.MonkeyPatch()
    yield mp
    mp.undo()


# ── seeding ────────────────────────────────────────────────────────────────


async def seed_client(sessionmaker, *, confidential=False, user_id=None, client_id="c1"):
    async with sessionmaker() as session:
        session.add(
            OAuthClient(
                client_id=client_id,
                client_secret_hash=oauth._hash(SECRET) if confidential else None,
                token_endpoint_auth_method=(
                    "client_secret_post" if confidential else "none"
                ),
                client_name="Test Client",
                redirect_uris=[REDIRECT_URI],
                scope="read readwrite offline_access",
                user_id=user_id,
            )
        )
        await session.commit()


async def seed_grant(sessionmaker, *, client_id="c1", grant_id="g1", scope="readwrite"):
    """One consent's worth of rows; returns the raw refresh-token value."""
    refresh_value = secrets.token_hex(32)
    async with sessionmaker() as session:
        session.add(
            OAuthToken(
                token_hash=oauth._hash(secrets.token_hex(32)),
                token_type="access",
                client_id=client_id,
                scope=scope,
                grant_id=grant_id,
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            )
        )
        session.add(
            OAuthToken(
                token_hash=oauth._hash(refresh_value),
                token_type="refresh",
                client_id=client_id,
                scope=scope,
                grant_id=grant_id,
                expires_at=datetime.now(timezone.utc) + timedelta(days=30),
            )
        )
        await session.commit()
    return refresh_value


async def live_count(sessionmaker, grant_id="g1") -> int:
    async with sessionmaker() as session:
        rows = (
            await session.execute(
                select(OAuthToken).where(
                    OAuthToken.grant_id == grant_id,
                    OAuthToken.revoked == False,  # noqa: E712
                )
            )
        ).scalars().all()
        return len(rows)


# ── revocation vs rotation ────────────────────────────────────────────────


async def _panel_revoke(sessionmaker, grant_id="g1"):
    from src.oauth.grants import revoke_grant_family

    async with sessionmaker() as session:
        await revoke_grant_family(session, grant_id)
        await session.commit()


class _GatedSession:
    """A real `AsyncSession` whose COMMIT waits for the test to say go.

    Racing the two handlers with `asyncio.gather` and hoping for the bad
    interleaving is not a test — measured, it reproduced the failure on the
    first (cold-pool) attempt and not on the next seven, so a regression would
    have been caught about one run in eight. This pins the interleaving
    instead: the refresh reads its rows and inserts the new pair, then parks in
    `commit()` while the revocation runs. That is exactly the window an
    unlocked `UPDATE ... WHERE grant_id = :g` cannot see into.

    The wait has a deadline because the *correct* implementation deliberately
    deadlocks this construction: the refresh holds the advisory lock for its
    whole transaction, so the revocation blocks in `lock_grant` and can never
    open the gate. Timing out and letting the refresh commit is what lets the
    same test assert the fixed behaviour rather than merely hanging.
    """

    def __init__(self, inner, gate, deadline):
        self._inner = inner
        self._gate = gate
        self._deadline = deadline

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def __aenter__(self):
        await self._inner.__aenter__()
        return self

    async def __aexit__(self, *exc):
        return await self._inner.__aexit__(*exc)

    async def commit(self):
        try:
            await asyncio.wait_for(self._gate.wait(), timeout=self._deadline)
        except asyncio.TimeoutError:
            pass
        return await self._inner.commit()


async def test_a_revocation_reaches_a_pair_a_refresh_inserted_but_had_not_committed(
    clean,
):
    """The failure the grant lock exists to prevent, pinned deterministically.

    Under READ COMMITTED an `UPDATE ... WHERE grant_id = :g` takes its snapshot
    when the statement starts, so rows a concurrent refresh has inserted but
    not yet committed are invisible to it — and rows it inserted *after* that
    snapshot stay invisible even once committed. The panel therefore revokes
    every row it can see, reports success, and the client keeps the brand-new
    pair it rotated into. Holding an advisory lock on the family across both
    transactions is what removes the window: the revocation waits, then issues
    its UPDATE on a fresh snapshot that includes the new rows.
    """
    sessionmaker = clean
    await seed_client(sessionmaker)
    refresh_value = await seed_grant(sessionmaker)

    gate = asyncio.Event()

    def _gated():
        return _GatedSession(sessionmaker(), gate, deadline=1.0)

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(oauth, "async_session", _gated)
        refresh_task = asyncio.create_task(
            oauth._handle_refresh({"refresh_token": refresh_value, "client_id": "c1"})
        )
        # Let the refresh get as far as its (gated) commit.
        await asyncio.sleep(0.15)

        revoke_task = asyncio.create_task(_panel_revoke(sessionmaker))
        # Give the revocation its chance to run to completion first. With the
        # lock it cannot; without it, it finishes here and misses the new pair.
        await asyncio.sleep(0.15)
        gate.set()

        response = await refresh_task
        await revoke_task
    finally:
        monkeypatch.undo()

    assert response.status_code == 200, "the refresh itself must still succeed"
    assert await live_count(sessionmaker) == 0, (
        "a token the refresh inserted survived a completed revocation"
    )


@pytest.mark.parametrize("attempt", range(4))
async def test_a_revocation_leaves_no_live_token_racing_a_refresh(clean, attempt):
    """The same invariant under ordinary scheduling, as a smoke check.

    Whichever wins, the family must end with zero usable tokens: revoke first
    and the refresh finds a revoked row and mints nothing; refresh first and
    the revocation's UPDATE runs on a snapshot that includes its rows.
    """
    sessionmaker = clean
    await seed_client(sessionmaker)
    refresh_value = await seed_grant(sessionmaker)

    results = await asyncio.gather(
        oauth._handle_refresh({"refresh_token": refresh_value, "client_id": "c1"}),
        _panel_revoke(sessionmaker),
        return_exceptions=True,
    )
    for r in results:
        assert not isinstance(r, BaseException), r

    assert await live_count(sessionmaker) == 0, (
        "a token survived a completed revocation"
    )


async def test_a_refresh_after_a_revocation_is_rejected(clean):
    sessionmaker = clean
    await seed_client(sessionmaker)
    refresh_value = await seed_grant(sessionmaker)

    await _panel_revoke(sessionmaker)
    response = await oauth._handle_refresh(
        {"refresh_token": refresh_value, "client_id": "c1"}
    )

    assert response.status_code == 400
    assert await live_count(sessionmaker) == 0


async def test_an_uncontended_refresh_still_rotates(clean):
    """The lock must not make the ordinary path fail or hang."""
    sessionmaker = clean
    await seed_client(sessionmaker)
    refresh_value = await seed_grant(sessionmaker)

    response = await oauth._handle_refresh(
        {"refresh_token": refresh_value, "client_id": "c1"}
    )

    assert response.status_code == 200
    # Old access token + the new pair; the old refresh token is revoked.
    assert await live_count(sessionmaker) == 3


# ── concurrent first consent ──────────────────────────────────────────────


class _Req:
    def __init__(self, signed):
        self.cookies = {"oauth_state": signed}
        self.session = {}


async def _consent(user_id, monkeypatch):
    server_state = "csrfstatetoken1234567890"
    signed = oauth._state_serializer().dumps(server_state)

    class _U:
        id = user_id

    async def _resolve(_request, _session):
        return _U()

    monkeypatch.setattr(oauth, "get_active_session_user", _resolve)
    return await oauth.authorize_post(
        _Req(signed),
        action="approve",
        client_id="c1",
        redirect_uri=REDIRECT_URI,
        code_challenge="A" * 43,
        code_challenge_method="S256",
        scope="read",
        state=server_state,
        client_state="echo",
    )


async def test_two_users_consenting_at_once_produce_exactly_one_owner(
    clean, monkeypatch_module
):
    """The conditional claim makes Postgres the arbitrator.

    The unconditional ORM assignment it replaces let the loser overwrite the
    winner's `user_id`, silently re-binding a client another user had just
    claimed and minting a code under it.
    """
    sessionmaker = clean
    monkeypatch_module.setattr(oauth.settings, "multi_user_mode", True, raising=False)
    async with sessionmaker() as session:
        for uid, name in ((1, "alice"), (2, "bob")):
            session.add(
                User(
                    id=uid,
                    username=name,
                    password_hash="x",
                    is_admin=False,
                    is_active=True,
                    session_version=1,
                )
            )
        await session.commit()
    await seed_client(sessionmaker, user_id=None)

    # Two consents for the same unbound client, from two different users.
    results = await asyncio.gather(
        _consent(1, monkeypatch_module),
        _consent(2, monkeypatch_module),
        return_exceptions=True,
    )
    for r in results:
        assert not isinstance(r, BaseException), r

    statuses = sorted(getattr(r, "status_code", 0) for r in results)
    assert statuses == [302, 403], statuses

    async with sessionmaker() as session:
        owner = (
            await session.execute(
                select(OAuthClient.user_id).where(OAuthClient.client_id == "c1")
            )
        ).scalar_one()
        codes = (await session.execute(select(OAuthCode))).scalars().all()

    assert owner in (1, 2)
    assert len(codes) == 1, "only the winner may mint a code"
    assert codes[0].user_id == owner

    monkeypatch_module.setattr(
        oauth.settings, "multi_user_mode", False, raising=False
    )


# ── client-authenticated revocation ───────────────────────────────────────


class _FormReq:
    def __init__(self, form):
        self._form = form

    async def form(self):
        return self._form


async def test_confidential_client_needs_its_secret_to_revoke(clean):
    sessionmaker = clean
    await seed_client(sessionmaker, confidential=True)
    refresh_value = await seed_grant(sessionmaker)

    denied = await oauth.revoke_token.__wrapped__(
        _FormReq({"token": refresh_value, "client_id": "c1"})
    )
    assert denied.status_code == 401
    assert await live_count(sessionmaker) == 2

    allowed = await oauth.revoke_token.__wrapped__(
        _FormReq({"token": refresh_value, "client_id": "c1", "client_secret": SECRET})
    )
    assert allowed.status_code == 200
    assert await live_count(sessionmaker) == 0


async def test_naming_another_client_is_a_200_that_does_nothing(clean):
    sessionmaker = clean
    await seed_client(sessionmaker)
    refresh_value = await seed_grant(sessionmaker)

    response = await oauth.revoke_token.__wrapped__(
        _FormReq({"token": refresh_value, "client_id": "not-this-one"})
    )

    assert response.status_code == 200
    assert await live_count(sessionmaker) == 2


async def test_omitting_client_id_revokes_nothing_even_for_a_public_client(clean):
    """Absence is not a match, over real rows.

    A public client carries no secret, so `_client_authenticated` passes
    trivially. Had a missing `client_id` also counted as naming the right
    client, posting somebody else's token value with no client field at all
    would have ended their 30-day grant while proving nothing — and unlike
    `/token`, no PKCE verifier binds this request to anyone.
    """
    sessionmaker = clean
    await seed_client(sessionmaker)  # public: token_endpoint_auth_method='none'
    refresh_value = await seed_grant(sessionmaker)

    response = await oauth.revoke_token.__wrapped__(_FormReq({"token": refresh_value}))

    assert response.status_code == 200, "still a uniform 200 (RFC 7009 §2.2)"
    assert await live_count(sessionmaker) == 2, "nothing may be revoked"

    # Naming the client is all it takes for the same public client to succeed,
    # so the refusal above is about identification, not about public clients.
    allowed = await oauth.revoke_token.__wrapped__(
        _FormReq({"token": refresh_value, "client_id": "c1"})
    )
    assert allowed.status_code == 200
    assert await live_count(sessionmaker) == 0
