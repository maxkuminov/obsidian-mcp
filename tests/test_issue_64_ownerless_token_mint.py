"""No token may be minted without an owner while multi-user mode is on.

`register_submit` turns a single-user deployment into a multi-user one by
claiming every ownerless row for the first admin:
``UPDATE oauth_tokens SET user_id = <admin> WHERE user_id IS NULL``. Under READ
COMMITTED that statement's snapshot is taken when it starts, so a token
exchange or a refresh that commits *afterwards* inserts rows the claim can no
longer see. They survive as tokens belonging to nobody — outside
`_assert_oauth_token_owner`, outside the panel's per-user filters, and outside
the vault-root lookup.

Two things close it, and both are pinned here:

* both mint handlers take the **same** transaction-scoped advisory lock the
  bootstrap already holds for its claim, so a mint and the claim cannot
  interleave at all; and
* with `multi_user_mode` on, a mint whose `user_id` would be NULL is refused
  outright, so even a row that somehow escaped the claim cannot be rotated into
  a fresh ownerless pair.

The lock order (bootstrap key first, then the per-grant key) is asserted
because it is what keeps the panel's family operations — which take only the
grant lock — from closing a cycle with the token endpoint.

The last section covers the *other* end: a token that already exists with no
owner must not authenticate. That gate lives in `APIKeyMiddleware`
(`reason=ownerless_credential`) and is pinned here too, because the mint-side
refusals above are only half of the property — they stop new ones, not the
ones a configuration cycle left behind.
"""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from src.models.db import OAuthClient, OAuthCode, OAuthToken
from src.oauth import routes as oauth
from src.oauth.grants import USER_BOOTSTRAP_LOCK_KEY, grant_lock_key

from _oauth_grant_fakes import FakeClient, FakeSession, FakeToken, SeqSession, in_hours

REGISTERED_URI = "https://client.example.com/callback"
VERIFIER = "v" * 64
REFRESH_SECRET = "r" * 64


def body(response) -> dict:
    return json.loads(response.body)


def minted(session) -> list:
    return [obj for obj in session.added if isinstance(obj, OAuthToken)]


def _exchange(monkeypatch, *, code_user_id, client_user_id=None, multi_user=True):
    client = OAuthClient(
        client_id="client123",
        client_secret_hash=None,
        token_endpoint_auth_method="none",
        client_name="Claude",
        redirect_uris=[REGISTERED_URI],
        scope="read readwrite offline_access",
        user_id=client_user_id,
    )
    code = OAuthCode(
        code_hash=oauth._hash("the-code"),
        client_id=client.client_id,
        redirect_uri=REGISTERED_URI,
        scope="read",
        code_challenge=oauth._base64url_sha256(VERIFIER),
        code_challenge_method="S256",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        used=False,
        user_id=code_user_id,
    )
    session = SeqSession([code, client])
    monkeypatch.setattr(oauth, "async_session", lambda: session)
    monkeypatch.setattr(oauth.settings, "multi_user_mode", multi_user, raising=False)
    response = asyncio.run(
        oauth._handle_auth_code(
            {
                "code": "the-code",
                "code_verifier": VERIFIER,
                "redirect_uri": REGISTERED_URI,
            }
        )
    )
    return response, session, code


def _refresh(monkeypatch, *, token_user_id, client_user_id=None, multi_user=True):
    token = FakeToken(
        grant_id="g1",
        token_type="refresh",
        scope="read",
        user_id=token_user_id,
        expires_at=in_hours(720),
        token_hash=oauth._hash(REFRESH_SECRET),
    )
    session = FakeSession(
        clients=[FakeClient(user_id=client_user_id)], tokens=[token]
    )
    monkeypatch.setattr(oauth, "async_session", lambda: session)
    monkeypatch.setattr(oauth.settings, "multi_user_mode", multi_user, raising=False)
    response = asyncio.run(oauth._handle_refresh({"refresh_token": REFRESH_SECRET}))
    return response, session, token


# --- the refusal ----------------------------------------------------------


def test_code_with_no_owner_mints_nothing_in_multi_user_mode(monkeypatch):
    response, session, code = _exchange(monkeypatch, code_user_id=None)

    assert response.status_code == 400
    assert body(response)["error"] == "invalid_grant"
    assert minted(session) == []
    assert code.used is False


def test_refresh_of_an_ownerless_token_mints_nothing_in_multi_user_mode(monkeypatch):
    """Otherwise the rotation launders one ownerless row into a fresh pair."""
    response, session, token = _refresh(monkeypatch, token_user_id=None)

    assert response.status_code == 400
    assert body(response)["error"] == "invalid_grant"
    assert minted(session) == []
    assert token.revoked is False


def test_single_user_mode_still_mints_ownerless_tokens(monkeypatch):
    """`user_id IS NULL` *is* the single-user identity; it must keep working."""
    response, session, _ = _exchange(monkeypatch, code_user_id=None, multi_user=False)

    assert response.status_code == 200
    assert len(minted(session)) == 2
    assert all(t.user_id is None for t in minted(session))


def test_single_user_mode_still_refreshes_ownerless_tokens(monkeypatch):
    response, session, _ = _refresh(monkeypatch, token_user_id=None, multi_user=False)

    assert response.status_code == 200
    assert len(minted(session)) == 2


def test_an_owned_code_still_mints_in_multi_user_mode(monkeypatch):
    response, session, _ = _exchange(monkeypatch, code_user_id=4, client_user_id=4)

    assert response.status_code == 200
    assert [t.user_id for t in minted(session)] == [4, 4]


# --- the cross-user rotation the audit found -----------------------------


def test_refresh_refuses_a_grant_whose_owner_is_not_the_clients(monkeypatch):
    """A legacy or race-created cross-user grant must not rotate forever.

    `authorize_post` and `_handle_auth_code` both refuse to *create* this
    pairing now, but nothing stopped an existing one from re-minting itself
    indefinitely — live, and invisible in either user's panel.
    """
    response, session, token = _refresh(
        monkeypatch, token_user_id=2, client_user_id=1
    )

    assert response.status_code == 400
    assert body(response)["error"] == "invalid_grant"
    assert minted(session) == []
    assert token.revoked is False


def test_refresh_allows_a_grant_owned_by_the_clients_owner(monkeypatch):
    response, session, _ = _refresh(monkeypatch, token_user_id=1, client_user_id=1)

    assert response.status_code == 200
    assert len(minted(session)) == 2


def test_refresh_allows_an_unbound_client(monkeypatch):
    """An unclaimed client is not evidence of a conflict."""
    response, session, _ = _refresh(monkeypatch, token_user_id=1, client_user_id=None)

    assert response.status_code == 200
    assert len(minted(session)) == 2


# --- lock acquisition and its order --------------------------------------


def test_code_exchange_takes_the_bootstrap_lock(monkeypatch):
    _, session, _ = _exchange(monkeypatch, code_user_id=4, client_user_id=4)

    assert session.advisory_locks == [USER_BOOTSTRAP_LOCK_KEY]


def test_refresh_takes_the_bootstrap_lock_before_the_grant_lock(monkeypatch):
    """Order is the property, not mere presence.

    The panel's family operations take only the grant lock. If the token
    endpoint took the grant lock first and then asked for the bootstrap key,
    a bootstrap holding that key and waiting on the family would close a
    cycle. One fixed order on the only path that takes both removes it.
    """
    _, session, _ = _refresh(monkeypatch, token_user_id=1, client_user_id=1)

    assert session.advisory_locks == [USER_BOOTSTRAP_LOCK_KEY, grant_lock_key("g1")]


def test_the_bootstrap_key_is_the_one_the_claim_already_holds():
    """A different key would silently un-serialize the window it exists to close.

    `register_submit` has held this literal since before the token endpoint
    took it; during a rolling deploy an old process still uses the literal
    while a new one imports the constant.
    """
    from src.auth import routes as auth_routes

    assert auth_routes._BOOTSTRAP_LOCK_KEY == USER_BOOTSTRAP_LOCK_KEY
    assert USER_BOOTSTRAP_LOCK_KEY == 7283910429


def test_the_panel_never_takes_the_bootstrap_lock():
    """The other half of "no cycle": the family paths take one lock only."""
    from src.control_panel import routes as panel

    family = [
        FakeToken(grant_id="g1", token_type="access"),
        FakeToken(grant_id="g1", token_type="refresh"),
    ]
    session = FakeSession(clients=[FakeClient()], tokens=family)

    asyncio.run(
        panel.revoke_oauth_token(
            token_id=family[0].id,
            session=session,
            user=type("U", (), {"id": None, "is_admin": True, "username": "admin"})(),
        )
    )

    assert USER_BOOTSTRAP_LOCK_KEY not in session.advisory_locks
    assert session.advisory_locks == [grant_lock_key("g1")]


@pytest.mark.parametrize("handler", ["exchange", "refresh"])
def test_the_lock_is_taken_before_anything_is_written(monkeypatch, handler):
    if handler == "exchange":
        _, session, _ = _exchange(monkeypatch, code_user_id=4, client_user_id=4)
    else:
        _, session, _ = _refresh(monkeypatch, token_user_id=1, client_user_id=1)

    assert session.advisory_locks, "no lock taken at all"
    assert session.advisory_locks[0] == USER_BOOTSTRAP_LOCK_KEY


# --- the other end: an existing ownerless token must not authenticate -----


class _EmptyResult:
    rowcount = 0


class _RowsResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return self

    def all(self):
        return [self._value]


class _MiddlewareSession:
    """Answers the statements `APIKeyMiddleware` issues on the OAuth branch."""

    def __init__(self, token, *, client_owner=None, user_active=True):
        self.token = token
        self.client_owner = client_owner
        self.user_active = user_active

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def commit(self):
        pass

    async def execute(self, stmt, *_a, **_kw):
        sql = str(stmt)
        if sql.startswith("UPDATE"):
            return _EmptyResult()
        if "FROM oauth_tokens" in sql:
            return _RowsResult([self.token])
        if "FROM oauth_clients" in sql:
            return _ScalarResult(self.client_owner)
        if "vault_path" in sql:
            return _RowsResult([])
        return _ScalarResult(self.user_active)


def _drive_middleware(token, *, multi_user, client_owner=None):
    """Run the real middleware over one bearer token; return (sent, called)."""
    import src.mcp_server.auth as mcp_auth

    sent = []
    called = []

    async def _send(message):
        sent.append(message)

    async def _receive():  # pragma: no cover - never awaited
        return {"type": "http.request", "body": b"", "more_body": False}

    async def _downstream(scope, receive, send):
        called.append(True)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def run():
        mp = pytest.MonkeyPatch()
        try:
            mp.setattr(
                mcp_auth,
                "async_session",
                lambda: _MiddlewareSession(token, client_owner=client_owner),
            )
            mp.setattr(mcp_auth.settings, "multi_user_mode", multi_user, raising=False)
            app = mcp_auth.APIKeyMiddleware(_downstream)
            await app(
                {
                    "type": "http",
                    "method": "POST",
                    "path": "/mcp/",
                    "headers": [(b"authorization", b"Bearer live-token")],
                },
                _receive,
                _send,
            )
        finally:
            mp.undo()

    asyncio.run(run())
    return sent, called


def _live_token(user_id, *, client_id="client123", scope="readwrite"):
    return OAuthToken(
        id=91,
        token_hash=oauth._hash("live-token"),
        token_type="access",
        client_id=client_id,
        scope=scope,
        grant_id="g1",
        user_id=user_id,
        revoked=False,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )


def test_an_ownerless_token_is_refused_by_the_middleware_in_multi_user_mode():
    """The Codex BLOCKER, pinned end to end.

    A token minted while multi-user mode was off keeps `user_id IS NULL`, and
    the bootstrap backfill only claims NULL rows while `users` is empty — so
    flipping the flag after users exist never adopts it. Every layer then read
    that token as the single-user identity and handed it the global vault. Here
    the client is bound to user A, which is exactly the shape that makes the
    token's NULL owner indefensible.
    """
    sent, called = _drive_middleware(
        _live_token(None), multi_user=True, client_owner=1
    )

    assert sent, "the middleware sent nothing at all"
    assert sent[0]["status"] == 401
    assert called == [], "the downstream app must never run"
    assert b"Invalid or revoked token" in sent[1]["body"]


def test_the_same_token_is_accepted_in_single_user_mode():
    """`user_id IS NULL` *is* the identity there; the refusal must be scoped."""
    sent, called = _drive_middleware(
        _live_token(None), multi_user=False, client_owner=None
    )

    assert sent[0]["status"] == 200
    assert called == [True]


def test_an_owned_token_still_authenticates_in_multi_user_mode():
    sent, called = _drive_middleware(_live_token(1), multi_user=True, client_owner=1)

    assert sent[0]["status"] == 200
    assert called == [True]


def test_an_owned_token_under_another_users_client_is_refused():
    """The cross-user grant check, at the boundary rather than at mint."""
    sent, called = _drive_middleware(_live_token(2), multi_user=True, client_owner=1)

    assert sent[0]["status"] == 401
    assert called == []


def test_a_token_with_no_vault_scope_is_refused():
    """`offline_access` alone authenticates nowhere."""
    sent, called = _drive_middleware(
        _live_token(1, scope="offline_access"), multi_user=True, client_owner=1
    )

    assert sent[0]["status"] == 401
    assert called == []
