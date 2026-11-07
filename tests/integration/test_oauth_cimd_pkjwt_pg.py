"""`private_key_jwt` CIMD clients against real Postgres (#420, tasks §6).

The database half of `tests/test_oauth_cimd_pkjwt_routes.py`:

* the full ChatGPT flow: consent approval → the row's shape (method, pinned
  `jwks_uri` and alg, the normalised snapshot, `jwks_fetched_at` = the origin
  fetch time) → a code exchange and a refresh, each with an assertion;
* key rotation through a kid-miss refetch, and a stale refetch on a row aged
  to 25 h by direct UPDATE: a key removed at the origin is refused, and an
  origin outage refreshes on the stored snapshot with `jwks_fetched_at`
  unchanged in the database;
* `/revoke` without `client_id`; a spent code replayed with a valid assertion
  revokes its family; two users get two rows and one user's `jti` is refused
  on the other's refresh;
* 6.2: while the JWKS fetch is suspended in the pre-phase (both triggers) no
  app connection is `idle in transaction` and none holds a lock;
* 6.3: an approval that re-pins `jwks_uri` during a suspended refetch wins —
  the refetch's compare-and-set UPDATE affects 0 rows.

The document is put in the CIMD cache directly, as `test_oauth_cimd_pg.py`
does; the JWKS origin is `cimd._fetch_jwks`, so the real guarded-fetch
admission, cache and pre-phase run. Skipped unless `PGVECTOR_TEST_ADMIN_URL`
names a throwaway Postgres server (`make test-integration` sets it).
"""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import pytest
import pytest_asyncio
from joserfc import jwk, jwt
from sqlalchemy import delete as sa_delete
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from starlette.requests import Request

import _harness
from src.models.db import OAuthClient, OAuthCode, OAuthToken, UsageLog, User
from src.oauth import cimd, grants
from src.oauth import client_assertion as ca
from src.oauth import routes as oauth
from src.services import security_events

pytestmark = [pytest.mark.asyncio(loop_scope="module"), _harness.requires_pgvector]

URL = "https://chatgpt.com/oauth/client.json"
JWKS_URI = "https://chatgpt.com/oauth/jwks.json"
JWKS_URI_2 = "https://chatgpt.com/oauth/jwks-2.json"
REDIRECT = "https://chatgpt.com/connector_platform_oauth_redirect"
VERIFIER = "v" * 64
TYPE = ca.ASSERTION_TYPE
DOC = {
    "client_id": URL,
    "client_name": "ChatGPT",
    "redirect_uris": [REDIRECT],
    "grant_types": ["authorization_code", "refresh_token"],
    "response_types": ["code"],
    "token_endpoint_auth_method": "private_key_jwt",
    "token_endpoint_auth_signing_alg": "RS256",
    "jwks_uri": JWKS_URI,
}

RSA_A = jwk.RSAKey.generate_key(2048, parameters={"kid": "rsa-a"})
RSA_B = jwk.RSAKey.generate_key(2048, parameters={"kid": "rsa-b"})


def pub(key) -> dict:
    return dict(key.as_dict(private=False))


class Origin:
    """The JWKS origin: what each URI serves, every fetch, and a gate."""

    def __init__(self):
        self.served: dict[str, list] = {JWKS_URI: [RSA_A], JWKS_URI_2: [RSA_B]}
        self.fail: BaseException | None = None
        self.fetches: list[str] = []
        self.gate: asyncio.Event | None = None
        self.started = asyncio.Event()

    async def fetch(self, uri, host):
        self.fetches.append(uri)
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.fail is not None:
            raise self.fail
        return cimd.validate_jwks({"keys": [pub(k) for k in self.served[uri]]}, host=host), None


@pytest.fixture(scope="module")
def migrated_url():
    yield from _harness.throwaway_database("oauth_pkjwt", 64)


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def sessionmaker(migrated_url):
    engine = create_async_engine(migrated_url)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


@pytest_asyncio.fixture(loop_scope="module")
async def db(sessionmaker, monkeypatch):
    async def wipe():
        async with sessionmaker() as session:
            await session.execute(sa_delete(UsageLog))
            await session.execute(sa_delete(OAuthToken))
            await session.execute(sa_delete(OAuthCode))
            await session.execute(sa_delete(OAuthClient))
            await session.execute(sa_delete(User))
            await session.commit()

    await wipe()
    monkeypatch.setattr(oauth, "async_session", sessionmaker)
    monkeypatch.setattr(ca, "async_session", sessionmaker)
    monkeypatch.setattr(oauth.settings, "multi_user_mode", False, raising=False)
    monkeypatch.setattr(oauth.settings, "oauth_cimd_enabled", True, raising=False)
    monkeypatch.setattr(oauth.settings, "oauth_cimd_cache_max_seconds", 86400, raising=False)
    monkeypatch.setattr(grants.settings, "oauth_grant_absolute_lifetime_days", 90, raising=False)
    cimd.reset_state_for_tests()
    ca.reset_state_for_tests()
    yield sessionmaker
    cimd.reset_state_for_tests()
    ca.reset_state_for_tests()
    await wipe()


@pytest.fixture
def origin(monkeypatch):
    server = Origin()
    monkeypatch.setattr(cimd, "_fetch_jwks", server.fetch)
    return server


@pytest.fixture
def emitted(monkeypatch):
    records: list[tuple[str, dict]] = []
    real = security_events.emit

    def capture(event, **kwargs):
        records.append((event, kwargs))
        return real(event, **kwargs)

    monkeypatch.setattr(security_events, "emit", capture)
    return records


def named(emitted, event):
    return [kw for name, kw in emitted if name == event]


def request(path="/authorize") -> Request:
    return Request({
        "type": "http", "http_version": "1.1", "method": "POST", "scheme": "https",
        "path": path, "raw_path": path.encode(), "root_path": "",
        "query_string": b"", "headers": [(b"host", b"testserver")],
        "client": ("198.51.100.7", 51234), "server": ("testserver", 443), "state": {},
    })


def sign(key=RSA_A, *, aud=None, jti=None, iss=URL) -> str:
    now = int(datetime.now(timezone.utc).timestamp())
    payload = {
        "iss": iss, "sub": iss, "aud": aud or f"{oauth.settings.base_url}/token",
        "iat": now, "exp": now + 60, "jti": jti or uuid.uuid4().hex,
    }
    return jwt.encode({"alg": "RS256", "kid": key.kid}, payload, key, algorithms=["RS256"])


def with_assertion(form, assertion):
    if assertion is not None:
        form.update({"client_assertion_type": TYPE, "client_assertion": assertion})
    return form


async def approve(document=None):
    projection = cimd.validate_document(URL, document or DOC)
    cimd._cache_put(URL, projection, None)
    response = await oauth._authorize_post_cimd(
        request(),
        action="approve", client_id=URL, redirect_uri=REDIRECT,
        code_challenge=oauth._base64url_sha256(VERIFIER), code_challenge_method="S256",
        scope="read offline_access", client_state="st",
        bound_hash=projection.authorization_hash,
    )
    return response


def code_of(response) -> str:
    assert response.status_code == 302, response.body
    return parse_qs(urlsplit(response.headers["location"]).query)["code"][0]


async def exchange(code, assertion=None, client_id=URL):
    form = {"code": code, "code_verifier": VERIFIER, "redirect_uri": REDIRECT}
    if client_id:
        form["client_id"] = client_id
    return await oauth._handle_auth_code(with_assertion(form, assertion), request("/token"))


async def refresh(token, assertion=None, client_id=URL):
    form = {"refresh_token": token}
    if client_id:
        form["client_id"] = client_id
    return await oauth._handle_refresh(with_assertion(form, assertion), request("/token"))


async def revoke(token, assertion=None, client_id=None):
    form = with_assertion({"token": token}, assertion)
    if client_id:
        form["client_id"] = client_id

    class _Req:
        url = type("U", (), {"path": "/revoke"})()
        client = None
        headers = {}
        state = type("S", (), {})()

        async def form(self):
            return form

    return await oauth.revoke_token.__wrapped__(_Req())


def body(response):
    return json.loads(response.body)


async def the_row(sessionmaker, client_id=None) -> OAuthClient:
    async with sessionmaker() as session:
        query = select(OAuthClient)
        if client_id:
            query = query.where(OAuthClient.client_id == client_id)
        (row,) = (await session.execute(query)).scalars().all()
        return row


async def age_row(sessionmaker, hours=25):
    async with sessionmaker() as session:
        await session.execute(
            update(OAuthClient).values(
                jwks_fetched_at=datetime.now(timezone.utc) - timedelta(hours=hours)
            )
        )
        await session.commit()


async def live_tokens(sessionmaker):
    async with sessionmaker() as session:
        return (
            await session.execute(select(OAuthToken).where(OAuthToken.revoked.is_(False)))
        ).scalars().all()


async def connect(origin) -> dict:
    """Approve, then exchange the code with an assertion: the live pair."""
    issued = await exchange(code_of(await approve()), sign())
    assert issued.status_code == 200, body(issued)
    return body(issued)


# ── 6.1: the ChatGPT flow ───────────────────────────────────────────────────


async def test_the_full_flow_pins_the_row_and_authenticates_both_grants(db, origin):
    response = await approve()
    row = await the_row(db)
    assert row.client_id == cimd.cimd_row_id(URL, None)
    assert row.token_endpoint_auth_method == "private_key_jwt"
    assert row.client_secret_hash is None
    assert row.jwks_uri == JWKS_URI and row.token_endpoint_auth_signing_alg == "RS256"
    assert row.jwks_snapshot == {"keys": [pub(RSA_A)]}
    cached = cimd.jwks_cache_get(JWKS_URI)
    assert row.jwks_fetched_at == cached.fetched_at  # the origin fetch time
    assert origin.fetches == [JWKS_URI]

    issued = await exchange(code_of(response), sign())
    assert issued.status_code == 200, body(issued)
    rotated = await refresh(body(issued)["refresh_token"], sign())
    assert rotated.status_code == 200, body(rotated)
    # No assertion: refused, and nothing revoked.
    before = len(await live_tokens(db))
    missing = await refresh(body(rotated)["refresh_token"])
    assert missing.status_code == 401 and body(missing) == {"error": "invalid_client"}
    assert len(await live_tokens(db)) == before


async def test_a_none_document_after_private_key_jwt_is_refused(db, origin, emitted):
    await approve()
    doc = {k: v for k, v in DOC.items() if k not in ("jwks_uri", "token_endpoint_auth_signing_alg")}
    doc["token_endpoint_auth_method"] = "none"
    refused = await approve(doc)
    assert refused.status_code == 400 and body(refused) == {"error": "invalid_client"}
    assert named(emitted, "oauth_authorize_refused")[-1]["reason"] == (
        "client_metadata.auth_method_downgrade"
    )
    assert (await the_row(db)).token_endpoint_auth_method == "private_key_jwt"


async def test_key_rotation_through_a_kid_miss_refetch(db, origin, emitted):
    tokens = await connect(origin)
    origin.served[JWKS_URI] = [RSA_A, RSA_B]
    rotated = await refresh(tokens["refresh_token"], sign(RSA_B))
    assert rotated.status_code == 200, body(rotated)
    row = await the_row(db)
    assert row.jwks_snapshot == {"keys": [pub(RSA_A), pub(RSA_B)]}
    [event] = named(emitted, "oauth_client_jwks_refetched")
    assert event["trigger"] == "kid_miss" and event["outcome"] == "updated"


async def test_a_stale_refetch_honours_removal_and_survives_an_outage(db, origin):
    tokens = await connect(origin)
    # An outage first: the stored snapshot still authenticates.
    await age_row(db)
    aged = (await the_row(db)).jwks_fetched_at
    cimd.reset_state_for_tests()  # no qualifying cache entry
    origin.fail = cimd.CimdRefusal("jwks_http_status", "chatgpt.com")
    rotated = await refresh(tokens["refresh_token"], sign(RSA_A))
    assert rotated.status_code == 200, body(rotated)
    row = await the_row(db)
    assert row.jwks_fetched_at == aged and row.jwks_snapshot == {"keys": [pub(RSA_A)]}

    # Past the cooldown, the origin is back and no longer serves RSA_A.
    ca.reset_state_for_tests()
    origin.fail = None
    origin.served[JWKS_URI] = [RSA_B]
    before = sorted(t.id for t in await live_tokens(db))
    refused = await refresh(body(rotated)["refresh_token"], sign(RSA_A))
    assert refused.status_code == 401 and body(refused) == {"error": "invalid_client"}
    row = await the_row(db)
    assert row.jwks_snapshot == {"keys": [pub(RSA_B)]} and row.jwks_fetched_at > aged
    # Nothing minted, nothing revoked.
    assert sorted(t.id for t in await live_tokens(db)) == before


async def test_revoke_without_client_id_authenticates_against_the_row(db, origin):
    tokens = await connect(origin)
    aud = f"{oauth.settings.base_url}/revoke"
    refused = await revoke(tokens["access_token"])
    assert refused.status_code == 401 and len(await live_tokens(db)) == 2
    done = await revoke(tokens["access_token"], sign(aud=aud))
    assert done.status_code == 200 and done.body == b"{}"
    assert await live_tokens(db) == []


async def test_a_replayed_spent_code_with_a_valid_assertion_revokes_the_family(db, origin):
    code = code_of(await approve())
    assert (await exchange(code, sign())).status_code == 200
    bad = await exchange(code, sign(aud="https://evil.example"))
    assert bad.status_code == 400 and body(bad) == {"error": "invalid_grant"}
    assert len(await live_tokens(db)) == 2  # a failed assertion revokes nothing
    replay = await exchange(code, sign())
    assert replay.status_code == 400 and body(replay) == {"error": "invalid_grant"}
    assert await live_tokens(db) == []


async def test_two_users_get_two_rows_and_a_jti_is_spent_across_them(db, origin, monkeypatch):
    async with db() as session:
        alice = User(username="alice", password_hash="x")
        bob = User(username="bob", password_hash="x")
        session.add_all([alice, bob])
        await session.commit()
    monkeypatch.setattr(oauth.settings, "multi_user_mode", True, raising=False)
    pairs = {}
    for user in (alice, bob):
        async def who(*_a, _user=user, **_kw):
            return _user

        monkeypatch.setattr(oauth, "get_active_session_user", who)
        issued = await exchange(code_of(await approve()), sign())
        assert issued.status_code == 200, body(issued)
        pairs[user.username] = body(issued)
    async with db() as session:
        found = (await session.execute(select(OAuthClient))).scalars().all()
    assert sorted(r.user_id for r in found) == sorted([alice.id, bob.id])
    shared = sign(jti="one-jti")
    assert (await refresh(pairs["alice"]["refresh_token"], shared)).status_code == 200
    again = await refresh(pairs["bob"]["refresh_token"], shared)
    assert again.status_code == 401 and body(again) == {"error": "invalid_client"}


# ── 6.2: no lock across the hop ─────────────────────────────────────────────


async def _app_backends(sessionmaker):
    async with sessionmaker() as probe:
        idle = (await probe.execute(text(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
            "AND pid <> pg_backend_pid() AND state LIKE 'idle in transaction%'"
        ))).scalar_one()
        locks = (await probe.execute(text(
            "SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid "
            "WHERE a.datname = current_database() AND l.pid <> pg_backend_pid()"
        ))).scalar_one()
        return idle, locks


@pytest.mark.parametrize("trigger", ["kid_miss", "stale"])
async def test_no_transaction_or_lock_is_held_while_the_refetch_is_suspended(db, origin, trigger):
    tokens = await connect(origin)
    if trigger == "stale":
        await age_row(db)
        cimd.reset_state_for_tests()
        key = RSA_A
    else:
        origin.served[JWKS_URI] = [RSA_A, RSA_B]
        key = RSA_B
    origin.gate = asyncio.Event()
    origin.started = asyncio.Event()
    task = asyncio.create_task(refresh(tokens["refresh_token"], sign(key)))
    await asyncio.wait_for(origin.started.wait(), timeout=10)
    assert await _app_backends(db) == (0, 0)
    origin.gate.set()
    response = await task
    assert response.status_code == 200, body(response)


# ── 6.3: the compare-and-set ────────────────────────────────────────────────


async def test_an_approval_that_re_pins_the_jwks_uri_wins_over_a_suspended_refetch(
    db, origin, monkeypatch
):
    tokens = await connect(origin)
    origin.served[JWKS_URI] = [RSA_A, RSA_B]
    results = []
    real_cas = ca._cas_write

    async def recording_cas(*args):
        written = await real_cas(*args)
        results.append(written)
        return written

    monkeypatch.setattr(ca, "_cas_write", recording_cas)
    gate = asyncio.Event()
    started = asyncio.Event()
    real_fetch = origin.fetch

    async def gated(uri, host):
        if uri == JWKS_URI:
            started.set()
            await gate.wait()
        return await real_fetch(uri, host)

    monkeypatch.setattr(cimd, "_fetch_jwks", gated)
    task = asyncio.create_task(refresh(tokens["refresh_token"], sign(RSA_B)))
    await asyncio.wait_for(started.wait(), timeout=10)
    # The user re-approves; the document now names another JWKS.
    repinned = await approve(dict(DOC, jwks_uri=JWKS_URI_2))
    assert repinned.status_code == 302, repinned.body
    gate.set()
    await task
    assert results == [0]
    row = await the_row(db)
    assert row.jwks_uri == JWKS_URI_2 and row.jwks_snapshot == {"keys": [pub(RSA_B)]}


# ── review F1: the downgrade rule under concurrent first approvals ──────────


async def test_concurrent_first_approvals_never_downgrade_a_key_row(
    db, origin, emitted, monkeypatch
):
    """Two approvals race for the same absent row: both run the `FOR UPDATE`
    read before either upserts (there is no row to lock). The
    `private_key_jwt` one commits first; the `none` one's conflict update must
    then affect nothing and answer the ordinary downgrade refusal."""
    import contextvars

    from sqlalchemy.dialects.postgresql import Insert

    role: contextvars.ContextVar = contextvars.ContextVar("role", default=None)
    a_selected, b_selected, a_committed = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Steered:
        """Forces A.select → B.select → A.insert+commit → B.insert."""

        def __init__(self, inner, who):
            self._inner, self._who = inner, who

        async def __aenter__(self):
            await self._inner.__aenter__()
            return self

        async def __aexit__(self, *exc):
            return await self._inner.__aexit__(*exc)

        async def execute(self, stmt, *args, **kwargs):
            insert = isinstance(stmt, Insert)
            if insert and self._who == "A":
                await asyncio.wait_for(b_selected.wait(), timeout=10)
            if insert and self._who == "B":
                await asyncio.wait_for(a_committed.wait(), timeout=10)
            result = await self._inner.execute(stmt, *args, **kwargs)
            if not insert:
                (a_selected if self._who == "A" else b_selected).set()
            return result

        async def commit(self):
            await self._inner.commit()
            if self._who == "A":
                a_committed.set()

        def __getattr__(self, name):
            return getattr(self._inner, name)

    def steered_session():
        return Steered(db(), role.get())

    monkeypatch.setattr(oauth, "async_session", steered_session)

    async def as_role(who, document):
        role.set(who)
        return await approve(document)

    none_doc = {k: v for k, v in DOC.items()
                if k not in ("jwks_uri", "token_endpoint_auth_signing_alg")}
    none_doc["token_endpoint_auth_method"] = "none"

    # A resolves its (private_key_jwt) projection and reads the absent row
    # before B puts its own projection in the document cache.
    task_a = asyncio.create_task(as_role("A", DOC))
    await asyncio.wait_for(a_selected.wait(), timeout=10)
    task_b = asyncio.create_task(as_role("B", none_doc))
    winner, loser = await asyncio.wait_for(asyncio.gather(task_a, task_b), timeout=20)

    assert winner.status_code == 302, winner.body
    assert loser.status_code == 400 and body(loser) == {"error": "invalid_client"}
    assert named(emitted, "oauth_authorize_refused")[-1]["reason"] == (
        "client_metadata.auth_method_downgrade"
    )
    assert len(named(emitted, "oauth_consent_granted")) == 1
    row = await the_row(db)
    assert row.token_endpoint_auth_method == "private_key_jwt"
    assert row.jwks_uri == JWKS_URI and row.jwks_snapshot == {"keys": [pub(RSA_A)]}
    async with db() as session:
        codes = (await session.execute(select(OAuthCode))).scalars().all()
    assert len(codes) == 1  # the loser minted no code
