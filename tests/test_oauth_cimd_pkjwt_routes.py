"""`private_key_jwt` CIMD clients at the routes (#420, Slice R): the offline half.

The production handlers run — `_handle_auth_code`, `_handle_refresh`,
`revoke_token`, the `/authorize` CIMD consent pair and the AS metadata — over
in-memory sessions. The pre-phase's two database touch points
(`client_assertion._load`, `_cas_write`) read and write the same in-memory
rows, and the JWKS origin is `cimd._fetch_jwks`, so the **real**
`cimd.fetch_jwks_guarded` (its join, its global cap, its cache fill) and the
real pre-phase logic are what the assertions observe.

What is pinned here (design D7, D8, D9, D10):

* the D8 order per endpoint path, with the identical-response properties: a
  spent code's failed assertion is the unknown-code 400 byte for byte; a live
  credential's is the existing `invalid_client` 401; an expired live code with
  a bad assertion answers 401, not "code expired"; nothing is revoked by a
  failed, missing or replayed assertion; the #182 reuse branch still revokes
  without an assertion;
* `/revoke` without `client_id`: a `private_key_jwt` row authenticates against
  the credential-resolved row; any other row is the constant 200;
* a `none` CIMD row presenting an assertion is `assertion_unexpected`;
* the pre-phase fetches only for an eligible credential, at most once per
  minute per `jwks_uri`, on a kid miss or a snapshot older than 24 h; a failed
  stale refetch falls back to the stored snapshot; a successful one that
  dropped the `kid` refuses;
* the seam fixes: one refetch takes one global in-flight slot, and the cache
  entry keeps the origin's own TTL.

The real-Postgres half (row shape, the no-lock proof, CAS against a re-pin)
is `tests/integration/test_oauth_cimd_pkjwt_pg.py`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from joserfc import jwk, jwt
from sqlalchemy.sql.dml import Update
from starlette.requests import Request

from src.config import settings
from src.models.db import OAuthClient, OAuthCode
from src.oauth import cimd
from src.oauth import client_assertion as ca
from src.oauth import routes as oauth
from src.services import security_events, transfer

from _oauth_grant_fakes import FakeSession, FakeToken, in_hours

UTC = timezone.utc
ISSUER = "https://mcp.example"
URL = "https://chatgpt.com/oauth/client.json"
JWKS_URI = "https://chatgpt.com/oauth/jwks.json"
ROW_ID = "cimd_" + "c" * 59
NONE_ROW_ID = "cimd_" + "n" * 59
NONE_URL = "https://claude.ai/oauth/claude-code-client-metadata"
REDIRECT = "https://chatgpt.com/connector/oauth/cb"
VERIFIER = "v" * 64
CODE = "the-code"
REFRESH = "r" * 64
TYPE = ca.ASSERTION_TYPE

RSA_A = jwk.RSAKey.generate_key(2048, parameters={"kid": "rsa-a"})
RSA_B = jwk.RSAKey.generate_key(2048, parameters={"kid": "rsa-b"})


def utcnow() -> datetime:
    return datetime.now(UTC)


def pub(key) -> dict:
    return dict(key.as_dict(private=False))


def sign(key=RSA_A, *, aud=None, iss=URL, sub=URL, jti=None, exp_in=60) -> str:
    now = int(utcnow().timestamp())
    payload = {
        "iss": iss, "sub": sub, "aud": aud or f"{ISSUER}/token",
        "iat": now, "exp": now + exp_in, "jti": jti or uuid.uuid4().hex,
    }
    return jwt.encode({"alg": "RS256", "kid": key.kid}, payload, key, algorithms=["RS256"])


def assertion_fields(assertion) -> dict:
    if assertion is None:
        return {}
    return {"client_assertion_type": TYPE, "client_assertion": assertion}


# ── in-memory rows ──────────────────────────────────────────────────────────


class PkClient:
    """An `oauth_clients` row with the #420 columns."""

    def __init__(self, **overrides):
        self.client_id = ROW_ID
        self.client_name = "ChatGPT"
        self.scope = "read readwrite offline_access"
        self.user_id = None
        self.redirect_uris = [REDIRECT]
        self.created_at = utcnow()
        self.client_secret_hash = None
        self.token_endpoint_auth_method = "private_key_jwt"
        self.client_id_metadata_url = URL
        self.jwks_uri = JWKS_URI
        self.token_endpoint_auth_signing_alg = "RS256"
        self.jwks_snapshot = {"keys": [pub(RSA_A)]}
        self.jwks_fetched_at = utcnow() - timedelta(hours=1)
        self.last_used_at = None
        for name, value in overrides.items():
            setattr(self, name, value)


def none_client() -> PkClient:
    return PkClient(
        client_id=NONE_ROW_ID, client_id_metadata_url=NONE_URL,
        token_endpoint_auth_method="none", jwks_uri=None,
        token_endpoint_auth_signing_alg=None, jwks_snapshot=None,
        jwks_fetched_at=None,
    )


class Code:
    def __init__(self, *, used=False, expires_at=None, grant_id="g-code", client_id=ROW_ID):
        self.code_hash = oauth._hash(CODE)
        self.client_id = client_id
        self.redirect_uri = REDIRECT
        self.code_challenge = oauth._base64url_sha256(VERIFIER)
        self.code_challenge_method = "S256"
        self.scope = "read offline_access"
        self.user_id = 7
        self.used = used
        self.grant_id = grant_id
        self.expires_at = expires_at or utcnow() + timedelta(minutes=10)


class _R:
    def __init__(self, obj=None, rowcount=0):
        self._obj = obj
        self.rowcount = rowcount

    def scalar_one_or_none(self):
        return self._obj


class CodeSession:
    """Answers the code exchange's lookups and counts every UPDATE (a revoke
    or the code consumption): the assertions below are about which paths
    reach one."""

    def __init__(self, world):
        self.world = world
        self.updates = 0
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, stmt, params=None):
        if isinstance(stmt, Update):
            self.updates += 1
            return _R(rowcount=2)
        descs = getattr(stmt, "column_descriptions", None) or []
        if not descs:
            return _R()  # advisory locks
        first = descs[0]
        if first.get("entity") is OAuthCode:
            code = self.world.code
            bound = stmt.compile().params
            return _R(code if code and bound.get("code_hash_1") == code.code_hash else None)
        if first.get("entity") is OAuthClient:
            client = self.world.client_by_id(self.world.code.client_id)
            if first.get("name") == "client_id_metadata_url":
                return _R(client.client_id_metadata_url if client else None)
            return _R(client)
        return _R()

    def add(self, obj):
        pass

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        pass


class World:
    def __init__(self):
        self.client = PkClient()
        self.clients = [self.client]
        self.code: Code | None = None
        self.tokens: list = []
        self.served = [RSA_A]
        self.directive = None
        self.fail: BaseException | None = None
        self.gate: asyncio.Event | None = None
        self.fetches: list[str] = []
        self.cas: list = []
        self.events: list[tuple[str, dict]] = []
        self.session = None

    def client_by_id(self, client_id):
        return next((c for c in self.clients if c.client_id == client_id), None)

    def family(self, *, client_id=ROW_ID, grant_id="g1", revoked=False, **refresh_kw):
        rows = [
            FakeToken(grant_id=grant_id, token_type="access", scope="read offline_access",
                      client_id=client_id, user_id=7, expires_at=in_hours(1)),
            FakeToken(grant_id=grant_id, token_type="refresh", scope="read offline_access",
                      client_id=client_id, user_id=7, revoked=revoked,
                      token_hash=oauth._hash(REFRESH),
                      expires_at=refresh_kw.pop("expires_at", in_hours(720)), **refresh_kw),
        ]
        self.tokens.extend(rows)
        self.session = FakeSession(clients=self.clients, tokens=self.tokens)
        return rows

    def named(self, event):
        return [fields for name, fields in self.events if name == event]


@pytest.fixture
def world(monkeypatch):
    w = World()
    monkeypatch.setattr(settings, "base_url", ISSUER)
    monkeypatch.setattr(settings, "multi_user_mode", False)
    monkeypatch.setattr(settings, "oauth_cimd_enabled", True)
    monkeypatch.setattr(settings, "oauth_cimd_cache_max_seconds", 86400)

    def session_factory():
        return w.session if w.session is not None else CodeSession(w)

    monkeypatch.setattr(oauth, "async_session", session_factory)

    async def load(kind, credential):
        digest = oauth._hash(credential)
        if kind == "code":
            cred = w.code if w.code is not None and w.code.code_hash == digest else None
        else:
            cred = next(
                (t for t in w.tokens if t.token_hash == digest
                 and (kind == "token" or t.token_type == "refresh")),
                None,
            )
        if cred is None:
            return None, None
        return cred, w.client_by_id(cred.client_id)

    async def cas(row_id, uri, snapshot):
        for client in w.clients:
            if (client.client_id == row_id and client.jwks_uri == uri
                    and client.jwks_fetched_at < snapshot.fetched_at):
                client.jwks_snapshot = snapshot.as_json()
                client.jwks_fetched_at = snapshot.fetched_at
                w.cas.append((row_id, snapshot.fetched_at))
                return 1
        return 0

    async def fetch_jwks(uri, host):
        w.fetches.append(uri)
        if w.gate is not None:
            await w.gate.wait()
        if w.fail is not None:
            raise w.fail
        snapshot = cimd.validate_jwks({"keys": [pub(k) for k in w.served]}, host=host)
        return snapshot, w.directive

    monkeypatch.setattr(ca, "_load", load)
    monkeypatch.setattr(ca, "_cas_write", cas)
    monkeypatch.setattr(cimd, "_fetch_jwks", fetch_jwks)

    real_emit = security_events.emit

    def capture(event, **fields):
        w.events.append((event, fields))
        return real_emit(event, **fields)

    monkeypatch.setattr(security_events, "emit", capture)
    cimd.reset_state_for_tests()
    ca.reset_state_for_tests()
    security_events.reset_state()
    with security_events.suppression_disabled():
        yield w
    cimd.reset_state_for_tests()
    ca.reset_state_for_tests()
    security_events.reset_state()


def run(coro):
    return asyncio.run(coro)


def req(path="/token") -> Request:
    return Request({
        "type": "http", "http_version": "1.1", "method": "POST", "scheme": "https",
        "path": path, "raw_path": path.encode(), "root_path": "",
        "query_string": b"", "headers": [(b"host", b"testserver")],
        "client": ("203.0.113.9", 51234), "server": ("testserver", 443), "state": {},
    })


def exchange(world, assertion=None, **extra):
    form = {"code": CODE, "code_verifier": VERIFIER, "redirect_uri": REDIRECT}
    form.update(assertion_fields(assertion))
    form.update(extra)
    form = {k: v for k, v in form.items() if v is not None}
    world.session = None
    return run(oauth._handle_auth_code(form, req()))


def refresh(world, assertion=None, **extra):
    form = {"refresh_token": REFRESH}
    form.update(assertion_fields(assertion))
    form.update(extra)
    form = {k: v for k, v in form.items() if v is not None}
    return run(oauth._handle_refresh(form, req()))


class _FormRequest:
    """Enough of a request for `revoke_token`: the form, the address, the path."""

    def __init__(self, form):
        self._form = form
        self.client = type("C", (), {"host": "203.0.113.9"})()
        self.url = type("U", (), {"path": "/revoke"})()
        self.state = type("S", (), {})()

    async def form(self):
        return self._form


def revoke(world, assertion=None, *, token=REFRESH, **extra):
    form = {"token": token}
    form.update(assertion_fields(assertion))
    form.update(extra)
    form = {k: v for k, v in form.items() if v is not None}
    return run(oauth.revoke_token.__wrapped__(_FormRequest(form)))


def body(response):
    return json.loads(response.body)


def wire(response):
    """Status, headers and body: what "byte-identical" compares."""
    return response.status_code, sorted(response.headers.items()), response.body


def refused_reasons(world, event="oauth_token_refused"):
    return [f.get("reason") for f in world.named(event)]


def revoked(world):
    return [t for t in world.tokens if t.revoked]


# ── D9: the AS metadata ─────────────────────────────────────────────────────


def test_the_metadata_advertises_private_key_jwt_only_while_enabled(world, monkeypatch):
    enabled = json.loads(run(oauth.oauth_metadata()).body)
    assert enabled["token_endpoint_auth_methods_supported"] == [
        "none", "client_secret_post", "private_key_jwt",
    ]
    assert enabled["token_endpoint_auth_signing_alg_values_supported"] == [
        "RS256", "PS256", "ES256",
    ]
    monkeypatch.setattr(settings, "oauth_cimd_enabled", False)
    disabled = run(oauth.oauth_metadata()).body
    assert json.loads(disabled)["token_endpoint_auth_methods_supported"] == [
        "none", "client_secret_post",
    ]
    assert b"token_endpoint_auth_signing_alg_values_supported" not in disabled
    assert b"private_key_jwt" not in disabled


# ── D6: the dispatcher ──────────────────────────────────────────────────────


def test_the_dispatcher_leaves_dcr_clients_byte_for_byte(world):
    class Dcr:
        client_id = "client123"
        token_endpoint_auth_method = "client_secret_post"
        client_secret_hash = oauth._hash("s" * 64)
        client_id_metadata_url = None

    good = {"client_secret": "s" * 64, **assertion_fields("garbage")}
    assert oauth._client_auth_failure(Dcr(), good, endpoint="token", now=utcnow()) is None
    assert oauth._client_auth_failure(
        Dcr(), {"client_secret": "w" * 64}, endpoint="token", now=utcnow()
    ) == "authentication_failed"
    Dcr.token_endpoint_auth_method, Dcr.client_secret_hash = "none", None
    assert oauth._client_auth_failure(
        Dcr(), assertion_fields("garbage"), endpoint="revoke", now=utcnow()
    ) is None


def test_a_none_cimd_row_presenting_an_assertion_is_unexpected(world):
    row = none_client()
    for form in ({"client_assertion": "x"}, {"client_assertion_type": TYPE},
                 assertion_fields(sign())):
        assert oauth._client_auth_failure(row, form, endpoint="token", now=utcnow()) == (
            "assertion_unexpected"
        )
    assert oauth._client_auth_failure(row, {}, endpoint="token", now=utcnow()) is None


def test_a_none_cimd_refresh_with_an_assertion_is_refused_and_revokes_nothing(world):
    world.clients[:] = [none_client()]
    world.client = world.clients[0]
    world.family(client_id=NONE_ROW_ID)
    response = refresh(world, sign())
    assert response.status_code == 401 and body(response) == {"error": "invalid_client"}
    assert refused_reasons(world) == ["invalid_client.assertion_unexpected"]
    assert revoked(world) == []
    assert world.fetches == []


# ── /token refresh ──────────────────────────────────────────────────────────


def test_a_live_refresh_with_a_valid_assertion_rotates(world):
    world.family()
    response = refresh(world, sign())
    assert response.status_code == 200, body(response)
    assert "refresh_token" in body(response)
    assert world.fetches == []


def test_a_live_refresh_without_an_assertion_is_the_existing_401(world):
    world.family()
    response = refresh(world)
    assert wire(response)[0] == 401 and body(response) == {"error": "invalid_client"}
    assert refused_reasons(world) == ["invalid_client.assertion_missing"]
    assert revoked(world) == []


def test_a_secret_beside_an_assertion_is_refused(world):
    world.family()
    response = refresh(world, sign(), client_secret="s" * 64)
    assert response.status_code == 401
    assert refused_reasons(world) == ["invalid_client.secret_presented"]


def test_a_live_refresh_bad_assertion_is_byte_identical_to_a_dcr_auth_failure(world):
    world.family()
    bad = refresh(world, sign(RSA_A, aud="https://evil.example/token"))
    assert refused_reasons(world) == ["invalid_client.assertion_audience_invalid"]
    # client_id was omitted (RFC 7523 allows it): the host is the row's URL's.
    [record] = world.named("oauth_token_refused")
    assert record["client_id"] == ROW_ID and record["cimd_host"] == "chatgpt.com"
    # Today's `authentication_failed` 401, rendered by the same line.
    from fastapi.responses import JSONResponse

    reference = JSONResponse({"error": "invalid_client"}, status_code=401)
    assert wire(bad) == wire(reference)
    assert revoked(world) == []


def test_a_replayed_assertion_on_a_live_refresh_is_401_and_revokes_nothing(world):
    world.family()
    assertion = sign()
    assert refresh(world, assertion).status_code == 200
    # Rotation added rows to `session.added`; the presented token is now
    # revoked, so mint a second live family to replay against.
    world.tokens.clear()
    world.family(grant_id="g2")
    again = refresh(world, assertion)
    assert again.status_code == 401
    assert refused_reasons(world)[-1] == "invalid_client.assertion_replayed"
    assert revoked(world) == []


def test_a_rotated_away_refresh_without_an_assertion_still_revokes_its_family(world):
    """#182 is unchanged (L7): reuse is decided before client authentication,
    and the assertion is never verified, recorded or consumed."""
    rows = world.family(revoked=True)
    rows[0].revoked = False  # the family still has a live access token
    response = refresh(world)
    assert response.status_code == 400 and body(response) == {"error": "invalid_grant"}
    assert all(t.revoked for t in world.tokens)
    assert world.named("oauth_refresh_reuse_detected")
    assert ca.replay_cache_size() == 0
    assert world.fetches == []


def test_a_refresh_works_while_cimd_is_disabled_and_refetches_still_run(world, monkeypatch):
    monkeypatch.setattr(settings, "oauth_cimd_enabled", False)
    world.family()
    world.served = [RSA_A, RSA_B]
    response = refresh(world, sign(RSA_B))  # kid miss
    assert response.status_code == 200, body(response)
    assert world.fetches == [JWKS_URI]
    world.client.jwks_fetched_at = utcnow() - timedelta(hours=25)
    ca.reset_state_for_tests()  # past the cooldown
    cimd.reset_state_for_tests()  # and no qualifying cache entry
    world.tokens.clear()
    world.family(grant_id="g3")
    assert refresh(world, sign(RSA_A)).status_code == 200
    assert world.fetches == [JWKS_URI, JWKS_URI]


# ── the pre-phase at /token ─────────────────────────────────────────────────


def test_a_kid_miss_refetches_once_and_the_rotated_key_authenticates(world):
    world.family()
    world.served = [RSA_A, RSA_B]
    response = refresh(world, sign(RSA_B))
    assert response.status_code == 200, body(response)
    assert world.fetches == [JWKS_URI]
    [event] = world.named("oauth_client_jwks_refetched")
    assert event["trigger"] == "kid_miss" and event["outcome"] == "updated"
    assert event["client_id"] == ROW_ID and event["cimd_host"] == "chatgpt.com"
    assert event["route"] == "/token"
    rendered = json.dumps(world.events, default=str)
    assert "rsa-b" not in rendered and JWKS_URI not in rendered and URL not in rendered


def test_twenty_kid_miss_requests_in_a_minute_fetch_at_most_once(world):
    world.family()
    world.served = [RSA_A]  # the kid never appears
    for _ in range(20):
        response = refresh(world, sign(RSA_B))
        assert response.status_code == 401
    assert len(world.fetches) <= 1
    assert set(refused_reasons(world)) == {"invalid_client.assertion_kid_unknown"}
    assert revoked(world) == []


def test_a_client_id_only_post_with_a_random_kid_fetches_nothing(world):
    world.family()
    form = {"client_id": URL, **assertion_fields(sign(RSA_B))}
    run(oauth._handle_refresh(form, req()))  # no refresh_token at all
    run(oauth._handle_auth_code(dict(form, code_verifier=VERIFIER), req()))
    run(oauth._handle_refresh(dict(form, refresh_token="unknown"), req()))
    assert world.fetches == []


def test_a_stale_snapshot_with_the_kid_still_served_refetches_then_succeeds(world):
    world.family()
    world.client.jwks_fetched_at = utcnow() - timedelta(hours=25)
    before = world.client.jwks_fetched_at
    response = refresh(world, sign(RSA_A))
    assert response.status_code == 200, body(response)
    assert world.fetches == [JWKS_URI]
    assert world.client.jwks_fetched_at > before
    [event] = world.named("oauth_client_jwks_refetched")
    assert event["trigger"] == "stale" and event["outcome"] == "updated"


def test_a_stale_refetch_that_dropped_the_kid_refuses_and_mints_nothing(world):
    world.family()
    world.client.jwks_fetched_at = utcnow() - timedelta(hours=25)
    world.served = [RSA_B]  # RSA_A was removed at the origin
    response = refresh(world, sign(RSA_A))
    assert response.status_code == 401
    assert refused_reasons(world) == ["invalid_client.assertion_kid_unknown"]
    assert world.client.jwks_snapshot == {"keys": [pub(RSA_B)]}
    assert revoked(world) == [] and world.session.added == []
    [event] = world.named("oauth_client_jwks_refetched")
    assert event["outcome"] == "kid_still_absent"


@pytest.mark.parametrize("failure", [
    cimd.CimdRefusal("jwks_fetch_timeout"),
    cimd.CimdRefusal("jwks_http_status"),
    cimd.CimdRefusal("jwks_invalid"),
    cimd.CimdRefusal("jwks_private_key"),
])
def test_a_failed_stale_refetch_falls_back_to_the_stored_snapshot(world, failure):
    world.family()
    stored_at = utcnow() - timedelta(hours=25)
    world.client.jwks_fetched_at = stored_at
    stored = dict(world.client.jwks_snapshot)
    world.fail = failure
    response = refresh(world, sign(RSA_A))
    assert response.status_code == 200, body(response)
    assert world.client.jwks_snapshot == stored and world.client.jwks_fetched_at == stored_at
    [event] = world.named("oauth_client_jwks_refetched")
    assert event["trigger"] == "stale" and event["outcome"] == "failed"
    assert event["reason"] == failure.reason


def test_a_failed_stale_refetch_retries_only_after_the_cooldown(world, monkeypatch):
    world.family()
    world.client.jwks_fetched_at = utcnow() - timedelta(hours=25)
    world.fail = cimd.CimdRefusal("jwks_http_status")
    clock = [1000.0]
    monkeypatch.setattr(ca, "_mono", lambda: clock[0])
    for _ in range(20):  # 20 stale requests within the minute
        world.tokens.clear()
        world.family()
        assert refresh(world, sign(RSA_A)).status_code == 200
        clock[0] += 2
    assert len(world.fetches) == 1
    clock[0] += 61
    world.tokens.clear()
    world.family()
    refresh(world, sign(RSA_A))
    assert len(world.fetches) == 2


def test_stale_and_kid_miss_together_fetch_once_recorded_as_kid_miss(world):
    world.family()
    world.client.jwks_fetched_at = utcnow() - timedelta(hours=25)
    world.served = [RSA_A, RSA_B]
    assert refresh(world, sign(RSA_B)).status_code == 200
    assert world.fetches == [JWKS_URI]
    [event] = world.named("oauth_client_jwks_refetched")
    assert event["trigger"] == "kid_miss"


def test_a_stale_cache_hit_older_than_a_day_is_not_used(world):
    world.family()
    world.client.jwks_fetched_at = utcnow() - timedelta(hours=30)
    old = cimd.validate_jwks({"keys": [pub(RSA_A)]}, fetched_at=utcnow() - timedelta(hours=25))
    cimd.jwks_cache_put(JWKS_URI, old, transfer.parse_cache_control("max-age=86400"))
    assert refresh(world, sign(RSA_A)).status_code == 200
    assert world.fetches == [JWKS_URI]


def test_a_fresh_stale_cache_hit_is_used_without_a_fetch(world):
    world.family()
    world.client.jwks_fetched_at = utcnow() - timedelta(hours=25)
    fresh_at = utcnow() - timedelta(hours=2)
    hit = cimd.validate_jwks({"keys": [pub(RSA_A)]}, fetched_at=fresh_at)
    cimd.jwks_cache_put(JWKS_URI, hit, transfer.parse_cache_control("max-age=86400"))
    assert refresh(world, sign(RSA_A)).status_code == 200
    assert world.fetches == []
    assert world.client.jwks_fetched_at == fresh_at  # the entry's time, not now
    assert world.named("oauth_client_jwks_refetched") == []


def test_the_documents_are_never_fetched_at_token_or_revoke(world, monkeypatch):
    async def boom(*_a, **_kw):
        raise AssertionError("the document was fetched")

    monkeypatch.setattr(cimd, "resolve", boom)
    monkeypatch.setattr(cimd, "_fetch", boom)
    world.family()
    world.client.jwks_fetched_at = utcnow() - timedelta(hours=25)
    assert refresh(world, sign(RSA_A)).status_code == 200
    world.code = Code(used=True)
    exchange(world, sign(RSA_A))
    world.tokens.clear()
    world.family(grant_id="g9")
    assert revoke(world, sign(RSA_A, aud=f"{ISSUER}/revoke")).status_code == 200


# ── zero-fetch eligibility (D7 step 2) ──────────────────────────────────────


def _zero_fetch(world, call):
    """The response with the pre-phase, and with it patched out: identical,
    and no fetch either way."""
    with_prephase = call()
    fetched = list(world.fetches)
    original = ca.prephase

    async def nothing(*_a, **_kw):
        return None

    ca.prephase = nothing
    try:
        ca.reset_state_for_tests()
        without = call()
    finally:
        ca.prephase = original
    assert fetched == [] and world.fetches == []
    assert wire(with_prephase) == wire(without)
    return with_prephase


@pytest.mark.parametrize("case", [
    "spent_bad_pkce", "expired_bad_pkce", "wrong_redirect", "malformed_verifier",
    "client_id_mismatch",
])
def test_an_ineligible_code_never_fetches(world, case):
    kwargs = {}
    if case == "spent_bad_pkce":
        world.code = Code(used=True)
        kwargs["code_verifier"] = "w" * 64
    elif case == "expired_bad_pkce":
        world.code = Code(expires_at=utcnow() - timedelta(minutes=1))
        kwargs["code_verifier"] = "w" * 64
    elif case == "wrong_redirect":
        world.code = Code()
        kwargs["redirect_uri"] = "https://chatgpt.com/elsewhere"
    elif case == "malformed_verifier":
        world.code = Code()
        kwargs["code_verifier"] = "short"
    else:
        world.code = Code()
        kwargs["client_id"] = "https://other.example/oauth/client.json"
    _zero_fetch(world, lambda: exchange(world, sign(RSA_B, jti=uuid.uuid4().hex), **kwargs))


@pytest.mark.parametrize("case", ["revoked", "rotated_away", "past_lifetime"])
def test_an_ineligible_refresh_token_never_fetches(world, case, monkeypatch):
    if case == "past_lifetime":
        world.family(grant_issued_at=utcnow() - timedelta(days=400))
    else:
        rows = world.family(revoked=True)
        if case == "rotated_away":
            rows[0].revoked = False  # a live sibling: the reuse branch revokes it

    def call():
        for t in world.tokens:  # each call starts from the same rows
            t.revoked = t.token_type == "refresh" and case != "past_lifetime"
        if case == "rotated_away":
            world.tokens[0].revoked = False
        return refresh(world, sign(RSA_B))

    _zero_fetch(world, call)
    if case == "rotated_away":
        assert world.tokens[0].revoked  # the family was still revoked


def test_an_ineligible_revoke_token_never_fetches(world):
    world.family(revoked=True)
    _zero_fetch(world, lambda: revoke(world, sign(RSA_B, aud=f"{ISSUER}/revoke")))
    world.tokens.clear()
    world.family(expires_at=utcnow() - timedelta(hours=1))
    world.tokens[0].expires_at = utcnow() - timedelta(hours=1)
    _zero_fetch(world, lambda: revoke(world, sign(RSA_B, aud=f"{ISSUER}/revoke")))


# ── /token authorization_code ───────────────────────────────────────────────


def test_a_spent_code_with_a_bad_assertion_is_byte_identical_to_an_unknown_code(world):
    world.code = Code(used=True)
    spent = exchange(world, sign(RSA_A, aud="https://evil.example"))
    world.code.code_hash = "nothing"
    unknown = exchange(world, sign(RSA_A))
    assert wire(spent) == wire(unknown)
    assert refused_reasons(world) == [
        "invalid_grant.spent_code_assertion_audience_invalid",
        "invalid_grant.unknown_code",
    ]


@pytest.mark.parametrize("assertion", [None, "garbage"])
def test_a_spent_code_with_a_missing_or_malformed_assertion_revokes_nothing(world, assertion):
    world.code = Code(used=True)
    session = CodeSession(world)
    world.session = session
    form = {"code": CODE, "code_verifier": VERIFIER, "redirect_uri": REDIRECT}
    form.update(assertion_fields(assertion))
    response = run(oauth._handle_auth_code(form, req()))
    assert wire(response)[0] == 400 and body(response) == {"error": "invalid_grant"}
    assert session.updates == 0
    expected = "assertion_missing" if assertion is None else "assertion_malformed"
    assert refused_reasons(world) == [f"invalid_grant.spent_code_{expected}"]


def test_a_spent_code_with_a_valid_fresh_assertion_reaches_the_replay_branch(world):
    world.code = Code(used=True)
    session = CodeSession(world)
    world.session = session
    form = {"code": CODE, "code_verifier": VERIFIER, "redirect_uri": REDIRECT,
            **assertion_fields(sign(RSA_A))}
    response = run(oauth._handle_auth_code(form, req()))
    assert body(response) == {"error": "invalid_grant"}
    assert session.updates == 1  # the family revocation
    assert world.named("oauth_code_replay_detected")


def test_a_live_code_with_a_bad_assertion_is_the_existing_401(world):
    world.code = Code()
    response = exchange(world, sign(RSA_A, iss="https://other.example/oauth/client.json"))
    assert response.status_code == 401 and body(response) == {"error": "invalid_client"}
    assert refused_reasons(world) == ["invalid_client.assertion_claims_invalid"]
    # client_id was omitted (RFC 7523 allows it): the host is the row's URL's.
    [record] = world.named("oauth_token_refused")
    assert record["client_id"] == ROW_ID and record["cimd_host"] == "chatgpt.com"


def test_an_expired_live_code_with_a_bad_assertion_is_401_not_code_expired(world):
    world.code = Code(expires_at=utcnow() - timedelta(minutes=1))
    response = exchange(world, sign(RSA_A, aud="https://evil.example"))
    assert response.status_code == 401
    assert refused_reasons(world) == ["invalid_client.assertion_audience_invalid"]


def test_a_live_code_with_a_kid_miss_refetches_before_the_lock(world):
    world.code = Code()
    world.served = [RSA_B]
    session = CodeSession(world)
    opened = []

    def factory():
        opened.append(list(world.fetches))
        return session

    import src.oauth.routes as routes_module

    original = routes_module.async_session
    routes_module.async_session = factory
    try:
        form = {"code": CODE, "code_verifier": VERIFIER, "redirect_uri": REDIRECT,
                **assertion_fields(sign(RSA_B))}
        run(oauth._handle_auth_code(form, req()))
    finally:
        routes_module.async_session = original
    # The fetch had finished before the main session was opened.
    assert opened and opened[0] == [JWKS_URI]


# ── /revoke (D8) ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("assertion, status, reason", [
    (None, 401, "assertion_missing"),
    ("not.a.jws.at.all", 401, "assertion_malformed"),
    ("wrong_iss", 401, "assertion_claims_invalid"),
    ("valid", 200, None),
])
def test_revoke_without_client_id_on_a_private_key_jwt_row(world, assertion, status, reason):
    world.family()
    if assertion == "wrong_iss":
        assertion = sign(RSA_A, aud=f"{ISSUER}/revoke", iss="https://evil.example/c.json")
    elif assertion == "valid":
        assertion = sign(RSA_A, aud=f"{ISSUER}/revoke")
    response = revoke(world, assertion)
    assert response.status_code == status
    if reason is None:
        assert body(response) == {}
        assert all(t.revoked for t in world.tokens)
        [granted] = world.named("oauth_grant_revoked")
        assert granted["cimd_host"] == "chatgpt.com"
    else:
        assert body(response) == {"error": "invalid_client"}
        assert refused_reasons(world, "oauth_revoke_refused") == [reason]
        assert revoked(world) == []


def test_revoke_without_client_id_on_a_none_row_is_the_constant_200(world):
    world.clients[:] = [none_client()]
    world.family(client_id=NONE_ROW_ID)
    response = revoke(world)
    assert response.status_code == 200 and body(response) == {}
    assert revoked(world) == []
    assert refused_reasons(world, "oauth_revoke_noop") == ["client_mismatch"]


def test_revoke_with_client_id_authenticates_and_a_mismatch_stays_concealed(world):
    world.family()
    mismatch = revoke(world, sign(RSA_A, aud=f"{ISSUER}/revoke"),
                      client_id="https://other.example/oauth/client.json")
    assert mismatch.status_code == 200 and revoked(world) == []
    bad = revoke(world, sign(RSA_A, aud="https://evil.example"), client_id=URL)
    assert bad.status_code == 401 and revoked(world) == []
    assert refused_reasons(world, "oauth_revoke_refused") == ["assertion_audience_invalid"]
    good = revoke(world, sign(RSA_A), client_id=URL)  # the token URL is accepted
    assert good.status_code == 200 and all(t.revoked for t in world.tokens)


def test_a_stale_revoke_whose_refetch_dropped_the_kid_is_401_and_revokes_nothing(world):
    world.family()
    world.client.jwks_fetched_at = utcnow() - timedelta(hours=25)
    world.served = [RSA_B]
    response = revoke(world, sign(RSA_A, aud=f"{ISSUER}/revoke"))
    assert response.status_code == 401 and revoked(world) == []
    assert refused_reasons(world, "oauth_revoke_refused") == ["assertion_kid_unknown"]
    [event] = world.named("oauth_client_jwks_refetched")
    assert event["route"] == "/revoke"


def test_a_dcr_secret_failure_at_revoke_keeps_client_auth_failed(world):
    class Dcr(PkClient):
        pass

    dcr = Dcr(client_id="client123", client_id_metadata_url=None,
              token_endpoint_auth_method="client_secret_post",
              client_secret_hash=oauth._hash("s" * 64), jwks_uri=None,
              token_endpoint_auth_signing_alg=None, jwks_snapshot=None, jwks_fetched_at=None)
    world.clients[:] = [dcr]
    world.family(client_id="client123")
    response = revoke(world, sign(RSA_A), client_id="client123", client_secret="w" * 64)
    assert response.status_code == 401
    assert refused_reasons(world, "oauth_revoke_refused") == ["client_auth_failed"]


# ── the seam fixes (Wave 1 J × A) ───────────────────────────────────────────


def test_one_refetch_takes_one_global_slot_and_keeps_the_origin_ttl(world, monkeypatch):
    """Seam (a): `fetch_jwks_guarded` alone enforces the cap of eight, so one
    pre-phase refetch is one slot. Seam (b): it alone fills the cache, with the
    origin's `Cache-Control`, so the entry's TTL is the origin's, not the
    default 300 s a second put would have written."""
    world.family()
    world.served = [RSA_A, RSA_B]
    world.directive = transfer.parse_cache_control("max-age=3600")
    world.gate = asyncio.Event()
    clock = [5000.0]
    monkeypatch.setattr(cimd, "_now", lambda: clock[0])
    seen = []

    async def scenario():
        task = asyncio.create_task(oauth._handle_refresh(
            {"refresh_token": REFRESH, **assertion_fields(sign(RSA_B))}, req()
        ))
        for _ in range(20):
            await asyncio.sleep(0)
            if world.fetches:
                break
        seen.append(cimd._global_inflight)
        world.gate.set()
        return await task

    response = run(scenario())
    assert response.status_code == 200, body(response)
    assert seen == [1]
    assert cimd._global_inflight == 0
    _snapshot, expires = cimd._jwks_cache[JWKS_URI]
    assert expires == pytest.approx(clock[0] + 3600)


def test_a_busy_global_cap_records_nothing_and_arms_no_cooldown(world, monkeypatch):
    world.family()
    world.served = [RSA_A, RSA_B]
    monkeypatch.setattr(cimd, "_global_inflight", cimd.GLOBAL_IN_FLIGHT)
    response = refresh(world, sign(RSA_B))
    assert response.status_code == 401  # the kid is still unknown
    assert world.fetches == [] and world.named("oauth_client_jwks_refetched") == []
    monkeypatch.setattr(cimd, "_global_inflight", 0)
    world.tokens.clear()
    world.family(grant_id="g4")
    assert refresh(world, sign(RSA_B)).status_code == 200
    assert world.fetches == [JWKS_URI]


# ── /authorize (D2, D4, D5) ─────────────────────────────────────────────────


CHATGPT_DOC = {
    "client_id": URL,
    "client_name": "ChatGPT",
    "redirect_uris": [REDIRECT],
    "grant_types": ["authorization_code", "refresh_token"],
    "response_types": ["code"],
    "token_endpoint_auth_method": "private_key_jwt",
    "token_endpoint_auth_signing_alg": "RS256",
    "jwks_uri": JWKS_URI,
}


class _UpsertSession:
    """Captures the approve transaction: the `FOR UPDATE` read and the upsert."""

    def __init__(self, existing=None, upsert_lost=False):
        self.existing = existing
        # True: the guarded conflict update affected no row (a concurrent
        # approval committed a private_key_jwt row first).
        self.upsert_lost = upsert_lost
        self.statements = []
        self.added = []
        self.commits = 0
        self.rollbacks = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, stmt, params=None):
        from sqlalchemy.dialects.postgresql import Insert

        self.statements.append(stmt)
        if isinstance(stmt, Insert):
            return _R(None if self.upsert_lost else "cimd_row")
        return _R(self.existing)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


def _upsert_values(session) -> dict:
    from sqlalchemy.dialects.postgresql import Insert

    [insert] = [s for s in session.statements if isinstance(s, Insert)]
    return insert.compile().params


def _approve(projection, monkeypatch, session):
    monkeypatch.setattr(oauth, "async_session", lambda: session)
    cimd._cache_put(URL, projection, None)
    return run(oauth._authorize_post_cimd(
        req("/authorize"),
        action="approve", client_id=URL, redirect_uri=REDIRECT,
        code_challenge=oauth._base64url_sha256(VERIFIER), code_challenge_method="S256",
        scope="read offline_access", client_state="st",
        bound_hash=projection.authorization_hash,
    ))


def test_an_approval_pins_the_method_key_source_and_the_origin_fetch_time(world, monkeypatch):
    projection = cimd.validate_document(URL, CHATGPT_DOC)
    origin_at = utcnow() - timedelta(hours=23, minutes=50)
    cached = cimd.validate_jwks({"keys": [pub(RSA_A)]}, fetched_at=origin_at)
    cimd.jwks_cache_put(JWKS_URI, cached, transfer.parse_cache_control("max-age=86400"))
    session = _UpsertSession()
    response = _approve(projection, monkeypatch, session)
    assert response.status_code == 302, response.body
    values = _upsert_values(session)
    assert values["token_endpoint_auth_method"] == "private_key_jwt"
    assert values["jwks_uri"] == JWKS_URI
    assert values["token_endpoint_auth_signing_alg"] == "RS256"
    assert values["jwks_snapshot"] == {"keys": [pub(RSA_A)]}
    assert values["jwks_fetched_at"] == origin_at  # never now()
    assert world.fetches == []  # served from the cache
    # Eleven minutes later the row's snapshot is past 24 h: the stale trigger.
    assert not ca._fresh(origin_at, utcnow() + timedelta(minutes=11))


def test_a_none_approval_writes_no_key_source(world, monkeypatch):
    doc = {k: v for k, v in CHATGPT_DOC.items()
           if k not in ("jwks_uri", "token_endpoint_auth_signing_alg")}
    doc["token_endpoint_auth_method"] = "none"
    projection = cimd.validate_document(URL, doc)
    session = _UpsertSession()
    assert _approve(projection, monkeypatch, session).status_code == 302
    values = _upsert_values(session)
    assert values["token_endpoint_auth_method"] == "none"
    assert values["jwks_uri"] is None
    assert "jwks_snapshot" not in values  # an SQL NULL literal, never a JSON null bind
    assert values["jwks_fetched_at"] is None
    assert world.fetches == []


def test_a_private_key_jwt_row_is_never_downgraded_to_none(world, monkeypatch):
    doc = {k: v for k, v in CHATGPT_DOC.items()
           if k not in ("jwks_uri", "token_endpoint_auth_signing_alg")}
    doc["token_endpoint_auth_method"] = "none"
    projection = cimd.validate_document(URL, doc)
    session = _UpsertSession(existing=PkClient())
    response = _approve(projection, monkeypatch, session)
    assert response.status_code == 400 and response.body == b'{"error":"invalid_client"}'
    assert refused_reasons(world, "oauth_authorize_refused") == [
        "client_metadata.auth_method_downgrade"
    ]
    assert session.commits == 0 and session.added == []
    from sqlalchemy.dialects.postgresql import Insert

    assert not any(isinstance(s, Insert) for s in session.statements)


def test_a_none_approval_losing_the_race_to_a_key_row_is_the_downgrade_refusal(
    world, monkeypatch
):
    """#420 review F1: both approvals saw no row; the key-holding one committed
    first, so the guarded conflict update affects nothing and the `none` one
    gets the same refusal the locked read gives."""
    doc = {k: v for k, v in CHATGPT_DOC.items()
           if k not in ("jwks_uri", "token_endpoint_auth_signing_alg")}
    doc["token_endpoint_auth_method"] = "none"
    projection = cimd.validate_document(URL, doc)
    session = _UpsertSession(upsert_lost=True)
    response = _approve(projection, monkeypatch, session)
    assert response.status_code == 400 and response.body == b'{"error":"invalid_client"}'
    assert refused_reasons(world, "oauth_authorize_refused") == [
        "client_metadata.auth_method_downgrade"
    ]
    assert session.commits == 0 and session.rollbacks == 1
    assert not world.named("oauth_consent_granted")


def test_the_conflict_update_is_guarded_against_a_downgrade(world, monkeypatch):
    from sqlalchemy.dialects import postgresql as pg
    from sqlalchemy.dialects.postgresql import Insert

    session = _UpsertSession()
    response = _approve(cimd.validate_document(URL, CHATGPT_DOC), monkeypatch, session)
    assert response.status_code == 302
    [insert] = [s for s in session.statements if isinstance(s, Insert)]
    sql = str(insert.compile(dialect=pg.dialect()))
    assert "ON CONFLICT (client_id) DO UPDATE" in sql
    where = sql.split("DO UPDATE", 1)[1].rsplit(" WHERE ", 1)[1]
    assert "oauth_clients.token_endpoint_auth_method" in where
    assert "excluded.token_endpoint_auth_method" in where


def test_a_none_row_may_upgrade_to_private_key_jwt(world, monkeypatch):
    projection = cimd.validate_document(URL, CHATGPT_DOC)
    world.served = [RSA_A]
    session = _UpsertSession(existing=none_client())
    assert _approve(projection, monkeypatch, session).status_code == 302
    assert _upsert_values(session)["token_endpoint_auth_method"] == "private_key_jwt"
    assert world.fetches == [JWKS_URI]


def test_a_jwks_failure_at_approve_is_the_constant_refusal_and_writes_nothing(world, monkeypatch):
    projection = cimd.validate_document(URL, CHATGPT_DOC)
    world.fail = cimd.CimdRefusal("jwks_private_key", "chatgpt.com")
    session = _UpsertSession()
    response = _approve(projection, monkeypatch, session)
    assert response.status_code == 400 and response.body == b'{"error":"invalid_client"}'
    assert refused_reasons(world, "oauth_authorize_refused") == [
        "client_metadata.jwks_private_key"
    ]
    assert session.statements == []


def _consent_page(monkeypatch):
    monkeypatch.setattr(oauth, "async_session", lambda: _UpsertSession())
    cimd._cache_put(URL, cimd.validate_document(URL, CHATGPT_DOC), None)
    return run(oauth._authorize_get_cimd(
        req("/authorize"),
        client_id=URL, redirect_uri=REDIRECT,
        code_challenge=oauth._base64url_sha256(VERIFIER), code_challenge_method="S256",
        scope="read offline_access", state="st", session_user_id=None,
    ))


def test_the_consent_page_resolves_the_jwks_first(world, monkeypatch):
    response = _consent_page(monkeypatch)
    assert response.status_code == 200
    assert world.fetches == [JWKS_URI]
    assert "chatgpt.com" in response.body.decode()


def test_a_jwks_failure_at_the_consent_page_is_the_constant_refusal(world, monkeypatch):
    world.served = []  # an empty set: no usable key
    response = _consent_page(monkeypatch)
    assert response.status_code == 400 and response.body == b'{"error":"invalid_client"}'
    assert refused_reasons(world, "oauth_authorize_refused") == [
        "client_metadata.jwks_invalid"
    ]
