"""Real-Postgres gate for #325 (code replay) and #326 (absolute grant lifetime).

What these assert *is* database behaviour: the lineage written in the same
transaction that spends the code, the family UPDATE under the grant lock, two
exchanges of one code serialized by the bootstrap lock and a `FOR UPDATE`
re-read, rotation inheriting `grant_issued_at` from the row re-read under the
lock, the retention predicate deleting real rows, and migration 029's backfill
meeting a live refresh. The branch logic and the response constancy under
injected failures are pinned offline in `tests/test_oauth_code_replay.py` and
`tests/test_oauth_grant_lifetime.py`.

Skipped unless `PGVECTOR_TEST_ADMIN_URL` names a throwaway Postgres *server*
(the module creates and drops its own databases); `make test-integration`
stands one up.
"""
import asyncio
import json
import logging
import os
import secrets
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit

import pytest
import pytest_asyncio
from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import src.mcp_server.auth as mcp_auth
from src.models.db import OAuthClient, OAuthCode, OAuthToken, UsageLog, User
from src.oauth import grants
from src.oauth import routes as oauth
from src.services import indexer, security_events

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
CODE = "code-" + "c" * 40
UTC = timezone.utc


# ── throwaway database harness (test_oauth_grants_pg.py's shape) ───────────


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


def _alembic(url: str, *args: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env={
            **os.environ,
            "DATABASE_URL": url,
            "SECRET_KEY": os.environ.get("SECRET_KEY") or "test-migration-key",
            "EMBEDDING_ALLOW_PLAINTEXT": "true",
        },
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, (
        f"alembic {' '.join(args)} failed\n{result.stdout}\n{result.stderr}"
    )


class _Database:
    def __init__(self, prefix: str):
        admin_db = unquote(urlsplit(PGVECTOR_TEST_ADMIN_URL).path).lstrip("/").casefold()
        if admin_db in FORBIDDEN_DB_NAMES:
            pytest.fail("PGVECTOR_TEST_ADMIN_URL points at the production database name")
        self.name = f"{prefix}_{uuid.uuid4().hex}"
        self.url = _with_database(PGVECTOR_TEST_ADMIN_URL, self.name)

    def create(self):
        asyncio.run(
            _run_maintenance(PGVECTOR_TEST_ADMIN_URL, f'CREATE DATABASE "{self.name}"')
        )

    def drop(self):
        try:
            asyncio.run(
                _run_maintenance(
                    PGVECTOR_TEST_ADMIN_URL,
                    f'DROP DATABASE IF EXISTS "{self.name}" (FORCE)',
                )
            )
        except Exception as e:  # pragma: no cover - cleanup best effort
            print(f"warning: could not drop throwaway database {self.name}: {e}")


@pytest.fixture(scope="module")
def migrated_database():
    db = _Database("test_grant_life")
    try:
        db.create()
        _alembic(db.url, "upgrade", "head")
        yield db.url
    finally:
        db.drop()


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def engine(migrated_database):
    eng = create_async_engine(migrated_database)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def sessionmaker(engine):
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture(scope="module")
def monkeypatch_module():
    mp = pytest.MonkeyPatch()
    yield mp
    mp.undo()


@pytest_asyncio.fixture(loop_scope="module")
async def clean(sessionmaker, monkeypatch_module):
    async with sessionmaker() as session:
        await session.execute(sa_delete(UsageLog))
        await session.execute(sa_delete(OAuthToken))
        await session.execute(sa_delete(OAuthCode))
        await session.execute(sa_delete(OAuthClient))
        await session.execute(sa_delete(User))
        await session.commit()
    monkeypatch_module.setattr(oauth, "async_session", sessionmaker)
    monkeypatch_module.setattr(indexer, "async_session", sessionmaker)
    monkeypatch_module.setattr(mcp_auth, "async_session", sessionmaker)
    monkeypatch_module.setattr(oauth.settings, "multi_user_mode", False, raising=False)
    monkeypatch_module.setattr(
        grants.settings, "oauth_grant_absolute_lifetime_days", 90, raising=False
    )
    yield sessionmaker
    # Undo any clock a case injected.
    monkeypatch_module.setattr(oauth, "_now", _real_now)
    monkeypatch_module.setattr(
        grants.settings, "oauth_grant_absolute_lifetime_days", 90, raising=False
    )


_real_now = oauth._now


@pytest.fixture
def clock(monkeypatch_module):
    """Pin the token endpoint's clock; returns a setter."""

    def _set(value: datetime):
        monkeypatch_module.setattr(oauth, "_now", lambda: value)

    return _set


@pytest.fixture
def events():
    class _Capture(logging.Handler):
        def __init__(self):
            super().__init__(level=logging.DEBUG)
            self.records = []

        def emit(self, record):
            self.records.append(record)

    handler = _Capture()
    logger = security_events.logger
    logger.addHandler(handler)
    propagate, level = logger.propagate, logger.level
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    security_events.reset_state()
    try:
        with security_events.suppression_disabled():
            yield handler.records
    finally:
        logger.removeHandler(handler)
        logger.propagate = propagate
        logger.setLevel(level)
        security_events.reset_state()


def named(records, event):
    return [r for r in records if r.getMessage() == event]


# ── seeding and driving ────────────────────────────────────────────────────


async def seed_client(sessionmaker, *, confidential=False, client_id="c1"):
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
            )
        )
        await session.commit()


async def seed_code(sessionmaker, *, code=CODE, client_id="c1", used=False,
                    grant_id=None, expires_at=None):
    async with sessionmaker() as session:
        session.add(
            OAuthCode(
                code_hash=oauth._hash(code),
                client_id=client_id,
                redirect_uri=REDIRECT_URI,
                scope="readwrite",
                code_challenge=oauth._base64url_sha256(VERIFIER),
                code_challenge_method="S256",
                expires_at=expires_at or datetime.now(UTC) + timedelta(minutes=10),
                used=used,
                grant_id=grant_id,
            )
        )
        await session.commit()


async def seed_family(sessionmaker, *, grant_id="g1", issued_at, client_id="c1",
                      access_expires=None, refresh_expires=None):
    """One live access/refresh pair with a chosen issuance time; returns the
    raw access and refresh values."""
    access_value = secrets.token_hex(32)
    refresh_value = secrets.token_hex(32)
    now = datetime.now(UTC)
    async with sessionmaker() as session:
        session.add(
            OAuthToken(
                token_hash=oauth._hash(access_value),
                token_type="access",
                client_id=client_id,
                scope="readwrite",
                grant_id=grant_id,
                grant_issued_at=issued_at,
                expires_at=access_expires or now + timedelta(hours=1),
            )
        )
        session.add(
            OAuthToken(
                token_hash=oauth._hash(refresh_value),
                token_type="refresh",
                client_id=client_id,
                scope="readwrite",
                grant_id=grant_id,
                grant_issued_at=issued_at,
                expires_at=refresh_expires or now + timedelta(days=30),
            )
        )
        await session.commit()
    return access_value, refresh_value


async def exchange(*, code=CODE, verifier=VERIFIER, client_id=None,
                   redirect_uri=REDIRECT_URI, client_secret=None):
    form = {"code": code, "code_verifier": verifier, "redirect_uri": redirect_uri}
    if client_id:
        form["client_id"] = client_id
    if client_secret:
        form["client_secret"] = client_secret
    return await oauth._handle_auth_code(form)


async def refresh(token, client_id=None):
    form = {"refresh_token": token}
    if client_id:
        form["client_id"] = client_id
    return await oauth._handle_refresh(form)


async def family(sessionmaker, grant_id) -> list[OAuthToken]:
    async with sessionmaker() as session:
        return list(
            (
                await session.execute(
                    select(OAuthToken)
                    .where(OAuthToken.grant_id == grant_id)
                    .order_by(OAuthToken.id)
                )
            ).scalars().all()
        )


async def live(sessionmaker, grant_id) -> int:
    return sum(1 for t in await family(sessionmaker, grant_id) if not t.revoked)


async def code_row(sessionmaker, code=CODE) -> OAuthCode | None:
    async with sessionmaker() as session:
        return (
            await session.execute(
                select(OAuthCode).where(OAuthCode.code_hash == oauth._hash(code))
            )
        ).scalar_one_or_none()


async def by_value(sessionmaker, value) -> OAuthToken:
    async with sessionmaker() as session:
        return (
            await session.execute(
                select(OAuthToken).where(OAuthToken.token_hash == oauth._hash(value))
            )
        ).scalar_one()


def body(response) -> dict:
    return json.loads(response.body)


async def unknown_code_response():
    return await exchange(code="never-issued-" + secrets.token_hex(8))


def assert_identical(a, b):
    assert a.status_code == b.status_code
    assert a.body == b.body
    assert dict(a.headers) == dict(b.headers)


async def first_exchange(sessionmaker, **seed):
    await seed_client(sessionmaker, **{k: v for k, v in seed.items() if k == "confidential"})
    await seed_code(sessionmaker)
    kwargs = {"client_secret": SECRET} if seed.get("confidential") else {}
    response = await exchange(**kwargs)
    assert response.status_code == 200, response.body
    grant_id = (await code_row(sessionmaker)).grant_id
    return response, grant_id


# ── #325: first exchange and replay ────────────────────────────────────────


async def test_the_first_exchange_records_lineage_and_one_issuance_time(clean, clock):
    sessionmaker = clean
    now = datetime.now(UTC).replace(microsecond=0)
    clock(now)
    response, grant_id = await first_exchange(sessionmaker)

    payload = body(response)
    assert payload["expires_in"] == 3600
    code = await code_row(sessionmaker)
    assert code.used is True and code.grant_id == grant_id
    tokens = await family(sessionmaker, grant_id)
    assert {t.token_type for t in tokens} == {"access", "refresh"}
    assert {t.grant_issued_at for t in tokens} == {now}
    by_type = {t.token_type: t for t in tokens}
    assert by_type["access"].expires_at == now + timedelta(hours=1)
    assert by_type["refresh"].expires_at == now + timedelta(days=30)


async def test_a_valid_replay_revokes_the_family_and_answers_like_an_unknown_code(
    clean, events
):
    sessionmaker = clean
    _response, grant_id = await first_exchange(sessionmaker)
    assert await live(sessionmaker, grant_id) == 2

    replay = await exchange()
    assert_identical(replay, await unknown_code_response())
    assert await live(sessionmaker, grant_id) == 0, "the live access token must die too"
    (alarm,) = named(events, "oauth_code_replay_detected")
    assert alarm.levelno == logging.WARNING
    assert alarm.grant_id == grant_id and alarm.revoked_tokens == 2

    # A second replay has nothing live left to kill: no second WARNING.
    second = await exchange()
    assert_identical(second, await unknown_code_response())
    assert len(named(events, "oauth_code_replay_detected")) == 1


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(verifier="w" * 64),
        dict(verifier="malformed"),
        dict(redirect_uri="https://elsewhere.example.com/callback"),
        dict(client_id="someone-else"),
        dict(client_secret="wrong" * 10),
    ],
    ids=["wrong_verifier", "malformed_verifier", "wrong_redirect", "wrong_client_id",
         "wrong_secret"],
)
async def test_a_replay_failing_revalidation_revokes_nothing(clean, events, kwargs):
    sessionmaker = clean
    _response, grant_id = await first_exchange(sessionmaker, confidential=True)
    if "client_secret" not in kwargs:
        kwargs = {**kwargs, "client_secret": SECRET}

    response = await exchange(**kwargs)
    assert response.status_code in (400, 401)
    assert await live(sessionmaker, grant_id) == 2
    assert named(events, "oauth_code_replay_detected") == []


async def test_a_replay_after_the_codes_expiry_still_revokes(clean, clock):
    sessionmaker = clean
    _response, grant_id = await first_exchange(sessionmaker)
    clock(datetime.now(UTC) + timedelta(days=1))
    replay = await exchange()
    assert replay.status_code == 400
    assert await live(sessionmaker, grant_id) == 0


@pytest.mark.parametrize("attempt", range(3))
async def test_a_concurrent_double_exchange_leaves_no_live_family(clean, attempt):
    sessionmaker = clean
    await seed_client(sessionmaker)
    await seed_code(sessionmaker)

    results = await asyncio.gather(exchange(), exchange(), return_exceptions=True)
    for r in results:
        assert not isinstance(r, BaseException), r
    assert sorted(r.status_code for r in results) == [200, 400]

    async with sessionmaker() as session:
        live_rows = (
            await session.execute(
                select(OAuthToken).where(OAuthToken.revoked == False)  # noqa: E712
            )
        ).scalars().all()
    assert live_rows == [], "the family the winner minted must not survive the loser"


async def test_a_replay_racing_a_refresh_leaves_no_live_token(clean):
    sessionmaker = clean
    response, grant_id = await first_exchange(sessionmaker)
    refresh_value = body(response)["refresh_token"]

    results = await asyncio.gather(
        exchange(), refresh(refresh_value), return_exceptions=True
    )
    for r in results:
        assert not isinstance(r, BaseException), r
    assert await live(sessionmaker, grant_id) == 0, (
        "a pair rotated in while the replay waited must be revoked with the rest"
    )


async def test_a_spent_code_without_lineage_revokes_nothing(clean, events):
    sessionmaker = clean
    await seed_client(sessionmaker)
    await seed_family(sessionmaker, grant_id="unrelated", issued_at=datetime.now(UTC))
    await seed_code(sessionmaker, used=True, grant_id=None)

    response = await exchange()
    assert_identical(response, await unknown_code_response())
    assert await live(sessionmaker, "unrelated") == 2
    (refused,) = [
        r for r in named(events, "oauth_token_refused")
        if r.reason == "invalid_grant.code_reused"
    ]
    assert refused.client_id == "c1"


async def test_spent_codes_are_retained_seven_days_past_expiry(clean):
    sessionmaker = clean
    await seed_client(sessionmaker)
    now = datetime.now(UTC)
    await seed_code(sessionmaker, code="spent-recent", used=True, grant_id="g-a",
                    expires_at=now - timedelta(days=6))
    await seed_code(sessionmaker, code="spent-old", used=True, grant_id="g-b",
                    expires_at=now - timedelta(days=8))
    await seed_code(sessionmaker, code="unspent-recent", expires_at=now - timedelta(days=6))
    await seed_code(sessionmaker, code="unspent-old", expires_at=now - timedelta(days=8))
    await seed_code(sessionmaker, code="just-spent", used=True, grant_id="g-c")

    await indexer.cleanup_expired_tokens()

    assert await code_row(sessionmaker, "spent-recent") is not None
    assert await code_row(sessionmaker, "just-spent") is not None
    assert await code_row(sessionmaker, "unspent-recent") is not None
    assert await code_row(sessionmaker, "spent-old") is None
    assert await code_row(sessionmaker, "unspent-old") is None


async def test_replay_detection_ends_with_the_retention_window(clean, clock, events):
    sessionmaker = clean
    _response, grant_id = await first_exchange(sessionmaker)
    # Age the spent code past the window and run the real cleanup.
    async with sessionmaker() as session:
        code = (await session.execute(select(OAuthCode))).scalar_one()
        code.expires_at = datetime.now(UTC) - timedelta(days=8)
        await session.commit()
    await indexer.cleanup_expired_tokens()
    assert await code_row(sessionmaker) is None

    response = await exchange()
    assert_identical(response, await unknown_code_response())
    assert await live(sessionmaker, grant_id) == 2
    assert named(events, "oauth_code_replay_detected") == []


# ── #326: inheritance, clamp, deadline ─────────────────────────────────────


async def test_rotation_inherits_the_issuance_time_across_rotations(clean, clock):
    sessionmaker = clean
    issued = datetime.now(UTC).replace(microsecond=0)
    clock(issued)
    response, grant_id = await first_exchange(sessionmaker)
    token = body(response)["refresh_token"]
    for hours in (1, 25, 24 * 10):
        clock(issued + timedelta(hours=hours))
        response = await refresh(token)
        assert response.status_code == 200, response.body
        token = body(response)["refresh_token"]
    assert {t.grant_issued_at for t in await family(sessionmaker, grant_id)} == {issued}


async def test_near_the_deadline_the_refresh_token_is_clamped(clean, clock):
    sessionmaker = clean
    await seed_client(sessionmaker)
    now = datetime.now(UTC).replace(microsecond=0)
    issued = now - timedelta(days=88)
    deadline = issued + timedelta(days=90)
    _a, refresh_value = await seed_family(sessionmaker, issued_at=issued)
    clock(now)

    response = await refresh(refresh_value)
    assert response.status_code == 200, response.body
    assert body(response)["expires_in"] == 3600
    access = await by_value(sessionmaker, body(response)["access_token"])
    new_refresh = await by_value(sessionmaker, body(response)["refresh_token"])
    assert new_refresh.expires_at == deadline
    assert access.expires_at == now + timedelta(hours=1)
    assert access.grant_issued_at == new_refresh.grant_issued_at == issued


async def test_inside_the_last_hour_both_tokens_are_clamped(clean, clock):
    sessionmaker = clean
    await seed_client(sessionmaker)
    now = datetime.now(UTC).replace(microsecond=0)
    issued = now - timedelta(days=90) + timedelta(minutes=30)
    deadline = issued + timedelta(days=90)
    _a, refresh_value = await seed_family(sessionmaker, issued_at=issued)
    clock(now)

    response = await refresh(refresh_value)
    assert response.status_code == 200, response.body
    assert body(response)["expires_in"] == 1800
    access = await by_value(sessionmaker, body(response)["access_token"])
    new_refresh = await by_value(sessionmaker, body(response)["refresh_token"])
    assert access.expires_at == new_refresh.expires_at == deadline


@pytest.mark.parametrize(
    "offset",
    [timedelta(0), timedelta(days=3), -timedelta(milliseconds=500)],
    ids=["at_deadline", "beyond", "under_one_second_left"],
)
async def test_a_live_refresh_at_the_deadline_is_refused_and_revokes_nothing(
    clean, clock, events, offset
):
    sessionmaker = clean
    await seed_client(sessionmaker)
    now = datetime.now(UTC)
    issued = now - timedelta(days=89)
    deadline = issued + timedelta(days=90)
    _a, refresh_value = await seed_family(
        sessionmaker, issued_at=issued, access_expires=deadline + timedelta(days=5),
        refresh_expires=deadline + timedelta(days=5),
    )
    clock(deadline + offset)

    response = await refresh(refresh_value)
    assert response.status_code == 400
    assert body(response) == {
        "error": "invalid_grant",
        "error_description": "grant lifetime exceeded; re-authorize",
    }
    tokens = await family(sessionmaker, "g1")
    assert len(tokens) == 2, "nothing minted"
    assert all(not t.revoked for t in tokens), "nothing revoked"
    reasons = [r.reason for r in named(events, "oauth_token_refused")]
    assert "invalid_grant.grant_lifetime_exceeded" in reasons


async def test_concurrent_refreshes_near_the_deadline(clean, clock):
    sessionmaker = clean
    await seed_client(sessionmaker)
    now = datetime.now(UTC).replace(microsecond=0)
    issued = now - timedelta(days=89)
    deadline = issued + timedelta(days=90)
    _a, refresh_value = await seed_family(sessionmaker, issued_at=issued)
    clock(now)

    results = await asyncio.gather(
        refresh(refresh_value), refresh(refresh_value), return_exceptions=True
    )
    for r in results:
        assert not isinstance(r, BaseException), r
    assert sorted(r.status_code for r in results) == [200, 400]
    (winner,) = [r for r in results if r.status_code == 200]
    for key in ("access_token", "refresh_token"):
        minted = await by_value(sessionmaker, body(winner)[key])
        assert minted.expires_at <= deadline
        assert minted.grant_issued_at == issued
    assert await live(sessionmaker, "g1") == 0, "the loser is reuse (#182)"


async def test_a_rotated_away_token_after_the_deadline_is_still_reuse(
    clean, clock, events
):
    """Codex spec review: reuse is decided first and keeps the generic body."""
    sessionmaker = clean
    await seed_client(sessionmaker)
    now = datetime.now(UTC)
    issued = now - timedelta(days=80)
    deadline = issued + timedelta(days=90)
    _a, old_refresh = await seed_family(sessionmaker, issued_at=issued)
    clock(now)
    rotated = await refresh(old_refresh)
    assert rotated.status_code == 200

    clock(deadline + timedelta(days=1))
    replay = await refresh(old_refresh)
    assert replay.status_code == 400
    assert body(replay) == {"error": "invalid_grant"}, (
        "the reuse response must not carry the re-authorize description"
    )
    unknown = await refresh("never-issued-" + secrets.token_hex(8))
    assert_identical(replay, unknown)
    assert await live(sessionmaker, "g1") == 0
    assert len(named(events, "oauth_refresh_reuse_detected")) == 1


async def _drive_middleware(bearer):
    sent = []

    async def downstream(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive():  # pragma: no cover - never awaited
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await mcp_auth.APIKeyMiddleware(downstream)(
        {
            "type": "http", "method": "POST", "path": "/mcp/",
            "headers": [(b"authorization", f"Bearer {bearer}".encode())],
            "client": (f"198.51.100.{secrets.randbelow(200) + 1}", 4242),
        },
        receive,
        send,
    )
    return next(m for m in sent if m["type"] == "http.response.start")["status"]


async def test_a_shortened_policy_takes_effect_at_the_next_request(
    clean, events, monkeypatch_module
):
    sessionmaker = clean
    await seed_client(sessionmaker)
    issued = datetime.now(UTC) - timedelta(days=10)
    access_value, refresh_value = await seed_family(sessionmaker, issued_at=issued)
    assert await _drive_middleware(access_value) == 200

    monkeypatch_module.setattr(
        grants.settings, "oauth_grant_absolute_lifetime_days", 7, raising=False
    )
    assert await _drive_middleware(access_value) == 401
    failures = [r.reason for r in named(events, "auth_failure")]
    assert "grant_lifetime_exceeded" in failures

    response = await refresh(refresh_value)
    assert response.status_code == 400
    assert body(response)["error_description"] == "grant lifetime exceeded; re-authorize"
    assert await live(sessionmaker, "g1") == 2


async def test_the_absolute_period_is_anchored_to_the_exchange_not_the_approval(
    clean, clock
):
    """Approve through the real `/authorize` POST, then exchange seven minutes
    later: the family's clock starts at the exchange."""
    sessionmaker = clean
    await seed_client(sessionmaker)
    server_state = "csrfstatetoken1234567890"
    signed = oauth._state_serializer().dumps(server_state)

    class _Req:
        cookies = {"oauth_state": signed}
        session = {}

    approved_at = datetime.now(UTC)
    redirect = await oauth.authorize_post(
        _Req(),
        action="approve",
        client_id="c1",
        redirect_uri=REDIRECT_URI,
        code_challenge=oauth._base64url_sha256(VERIFIER),
        code_challenge_method="S256",
        scope="read",
        state=server_state,
        client_state="echo",
    )
    assert redirect.status_code == 302
    code = parse_qs(urlsplit(redirect.headers["location"]).query)["code"][0]

    exchanged_at = approved_at + timedelta(minutes=7)
    clock(exchanged_at)
    response = await exchange(code=code)
    assert response.status_code == 200, response.body
    grant_id = (await code_row(sessionmaker, code)).grant_id
    issued = {t.grant_issued_at for t in await family(sessionmaker, grant_id)}
    assert issued == {exchanged_at}
    assert grants.grant_deadline(exchanged_at) == exchanged_at + timedelta(days=90)


# ── migration 029 meets a live refresh ─────────────────────────────────────


async def test_a_pre_029_family_refreshes_immediately_after_the_upgrade(monkeypatch):
    """The owner decision, end to end: no connector is logged out by the
    deploy, and the clock starts at the migration."""
    import asyncpg

    db = _Database("test_grant_life_029")
    await _run_maintenance(PGVECTOR_TEST_ADMIN_URL, f'CREATE DATABASE "{db.name}"')
    try:
        await asyncio.to_thread(_alembic, db.url, "upgrade", "028")
        refresh_value = secrets.token_hex(32)
        old = datetime(2025, 1, 1, tzinfo=UTC)

        conn = await asyncpg.connect(_asyncpg_dsn(db.url))
        try:
            await conn.execute(
                "INSERT INTO oauth_clients (client_id, token_endpoint_auth_method, "
                " client_name, redirect_uris, scope, last_used_at) "
                "VALUES ('c1', 'none', 'Old', $1::jsonb, "
                " 'read readwrite offline_access', now())",
                json.dumps([REDIRECT_URI]),
            )
            await conn.execute(
                "INSERT INTO oauth_tokens (token_hash, token_type, client_id, "
                " scope, grant_id, expires_at, revoked, created_at) VALUES "
                "($1, 'refresh', 'c1', 'readwrite', 'g-old', $2, false, $3)",
                oauth._hash(refresh_value),
                datetime.now(UTC) + timedelta(days=20),
                old,
            )
        finally:
            await conn.close()

        await asyncio.to_thread(_alembic, db.url, "upgrade", "head")

        eng = create_async_engine(db.url)
        maker = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
        monkeypatch.setattr(oauth, "async_session", maker)
        monkeypatch.setattr(oauth, "_now", _real_now)
        monkeypatch.setattr(oauth.settings, "multi_user_mode", False, raising=False)
        monkeypatch.setattr(
            grants.settings, "oauth_grant_absolute_lifetime_days", 90, raising=False
        )
        try:
            async with maker() as session:
                (backfilled,) = (
                    await session.execute(select(OAuthToken.grant_issued_at))
                ).scalars().all()
            response = await refresh(refresh_value)
            async with maker() as session:
                rows = (await session.execute(select(OAuthToken))).scalars().all()
        finally:
            await eng.dispose()

        assert backfilled > old, "the clock starts at migration, not at creation"
        assert response.status_code == 200, response.body
        assert len(rows) == 3
        assert {r.grant_issued_at for r in rows} == {backfilled}
    finally:
        await _run_maintenance(
            PGVECTOR_TEST_ADMIN_URL, f'DROP DATABASE IF EXISTS "{db.name}" (FORCE)'
        )
