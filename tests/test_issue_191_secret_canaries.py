"""#191 / design D16 — no credential reaches the log, in any field or message.

The allow-list has no field a password or a token could ride in, and every call
site is written not to pass one. That is an argument, and an argument is exactly
what a canary test exists to replace. So every credential position is filled
with a value that appears nowhere else in the process, the real handlers are
driven, and every captured record is searched — **rendered through the real
formatter**, because the formatter is what actually ships.

Two halves, because half the secrets are not submitted:

* **Submitted** — a distinct 32-character canary in each caller-controlled
  credential position: the panel password, the session cookie, the CSRF token,
  the OAuth `client_secret` at `/token` and at `/revoke`, the authorization
  `code`, the `refresh_token`, the PKCE `code_verifier`, the MCP bearer and the
  transfer redemption bearer.
* **Generated** — a server-generated secret cannot be planted, so the value the
  server *produced* is captured and its absence asserted: the DCR
  `client_secret` (**not** the `client_id`, a public identifier the catalogue
  logs on purpose), the `/authorize` code, the access/refresh pair from an
  exchange and from a rotation, and an `omcp_` key.

The match is deliberately not equality. A record containing any **12-character
substring** of a canary fails, which catches a truncation ("the first 8
characters are harmless") as well as a whole value.

Two positions are exercised at the boundary rather than through their route.
`/transfer/*` and the panel's key administration belong to Slices D and C of
this change and emit nothing yet, so a route-level canary there would pass
vacuously today and say nothing. Instead their bearers go through
`redacted_token_tag` — the single function in the codebase that turns a
presented credential into something loggable, and therefore the only way one
could ever reach a record — and through `auth_failure`, which is what actually
carries a presented bearer into the log.
"""
import asyncio
import hashlib
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone

import pytest
from starlette.requests import Request

from src.auth import routes as auth_routes
from src.csrf import verify_csrf
from src.limiter import limiter
from src.logging_setup import StructuredFormatter
from src.mcp_server import auth as mcp_auth
from src.models.db import OAuthCode, OAuthToken
from src.oauth import routes as oauth
from src.services import security_events

from _oauth_grant_fakes import FakeClient, FakeSession, FakeToken, SeqSession, in_hours

REGISTERED_URI = "https://client.example.com/callback"
MIN_SUBSTRING = 12


def canary() -> str:
    """32 characters that appear nowhere else in this process."""
    return secrets.token_hex(16)


# --- capture: every record, rendered the way it ships ---------------------


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


class _Sink:
    def __init__(self, records):
        self.records = records
        self._formatter = StructuredFormatter()

    def rendered(self) -> str:
        """Everything a canary could hide in: the shipped line, plus the raw
        record attributes the formatter chose to drop."""
        parts = []
        for record in self.records:
            parts.append(self._formatter.format(record))
            parts.append(record.getMessage())
            parts.extend(str(value) for value in record.__dict__.values())
        return "\n".join(parts)

    def assert_absent(self, *values: str) -> None:
        haystack = self.rendered()
        for value in values:
            assert value, "an empty canary would assert nothing"
            assert value not in haystack, f"{value[:8]}… reached a record whole"
            for start in range(len(value) - MIN_SUBSTRING + 1):
                fragment = value[start : start + MIN_SUBSTRING]
                assert fragment not in haystack, (
                    f"a {MIN_SUBSTRING}-character fragment of {value[:8]}… "
                    "reached a record"
                )


@pytest.fixture
def sink():
    """Records from the security stream **and** from the root.

    Attached to both deliberately: a canary that escaped through a bare
    `logger.warning` somebody added would never touch `security_events`, and
    that is exactly the leak worth catching.
    """
    handler = _Capture()
    root = logging.getLogger()
    events = security_events.logger
    root_level, events_level = root.level, events.level
    root.addHandler(handler)
    events.addHandler(handler)
    root.setLevel(logging.DEBUG)
    events.setLevel(logging.DEBUG)
    # `security_events` propagates to the root, so without this every record
    # would be captured twice — harmless for a substring search, misleading in
    # a failure message.
    propagate = events.propagate
    events.propagate = False
    security_events.reset_state()
    limiter.reset()
    try:
        with security_events.suppression_disabled():
            yield _Sink(handler.records)
    finally:
        root.removeHandler(handler)
        events.removeHandler(handler)
        root.setLevel(root_level)
        events.setLevel(events_level)
        events.propagate = propagate
        security_events.reset_state()
        limiter.reset()


def _request(path, method="POST", session=None, headers=None) -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "https",
            "path": path,
            "raw_path": path.encode(),
            "root_path": "",
            "query_string": b"",
            "headers": [(b"host", b"testserver")] + list(headers or []),
            "client": ("203.0.113.7", 54321),
            "server": ("testserver", 443),
            "session": {} if session is None else session,
            "state": {},
        }
    )


# --- submitted: the panel ------------------------------------------------


async def test_a_submitted_password_never_reaches_a_record(sink):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    password = canary()
    user = SimpleNamespace(
        id=1,
        username="admin",
        password_hash="$2b$12$" + "a" * 53,
        is_active=True,
        is_admin=True,
        session_version=1,
    )
    result = MagicMock()
    result.scalar_one_or_none.return_value = user
    session = AsyncMock()
    session.execute.return_value = result

    response = await auth_routes.login_submit(
        request=_request("/admin/auth/login"),
        username="admin",
        password=password,
        next="/admin/",
        session=session,
    )

    assert response.status_code == 401
    assert sink.records, "the failure must have produced a record to search"
    sink.assert_absent(password)


async def test_a_submitted_session_cookie_never_reaches_a_record(sink):
    """The cookie is a signed bearer for the whole panel session."""
    cookie = canary()
    request = _request(
        "/admin/auth/logout",
        headers=[(b"cookie", f"session={cookie}".encode())],
        session={"user_id": 3, "username": "admin"},
    )

    await auth_routes.logout(request)

    assert sink.records
    sink.assert_absent(cookie)


async def test_a_submitted_csrf_token_never_reaches_a_record(sink):
    token = canary()
    request = _request(
        "/admin/keys", headers=[(b"x-csrf-token", token.encode())]
    )

    with pytest.raises(Exception):
        await verify_csrf(request)

    sink.assert_absent(token)


# --- submitted: OAuth ----------------------------------------------------


def _code_row(**overrides):
    fields = {
        "code_hash": oauth._hash("a-real-code"),
        "client_id": "client123",
        "redirect_uri": REGISTERED_URI,
        "scope": "read",
        "code_challenge": oauth._base64url_sha256("v" * 64),
        "code_challenge_method": "S256",
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
        "used": False,
        "user_id": 5,
    }
    fields.update(overrides)
    return OAuthCode(**fields)


def test_a_submitted_authorization_code_never_reaches_a_record(monkeypatch, sink):
    code = canary()
    session = SeqSession([None])
    monkeypatch.setattr(oauth, "async_session", lambda: session)

    response = asyncio.run(
        oauth._handle_auth_code(
            {
                "code": code,
                "code_verifier": "v" * 64,
                "redirect_uri": REGISTERED_URI,
            },
            _request("/token"),
        )
    )

    assert response.status_code == 400
    assert sink.records
    sink.assert_absent(code, oauth._hash(code))


def test_a_submitted_pkce_verifier_never_reaches_a_record(monkeypatch, sink):
    verifier_canary = canary()
    # The verifier must satisfy the 43–128 character PKCE grammar, so the canary
    # is embedded in one rather than used as one; the substring rule is what
    # makes that a real assertion.
    verifier = verifier_canary + "a" * 32
    session = SeqSession([_code_row(), FakeClient()])
    monkeypatch.setattr(oauth, "async_session", lambda: session)

    response = asyncio.run(
        oauth._handle_auth_code(
            {
                "code": "a-real-code",
                "code_verifier": verifier,
                "redirect_uri": REGISTERED_URI,
            },
            _request("/token"),
        )
    )

    assert response.status_code == 400
    assert sink.records
    sink.assert_absent(verifier_canary)


def test_a_submitted_client_secret_never_reaches_a_record(monkeypatch, sink):
    presented = canary()
    client = FakeClient(
        token_endpoint_auth_method="client_secret_post",
        client_secret_hash=oauth._hash("the-real-secret"),
    )
    session = SeqSession([_code_row(), client])
    monkeypatch.setattr(oauth, "async_session", lambda: session)

    response = asyncio.run(
        oauth._handle_auth_code(
            {
                "code": "a-real-code",
                "code_verifier": "v" * 64,
                "redirect_uri": REGISTERED_URI,
                "client_id": "client123",
                "client_secret": presented,
            },
            _request("/token"),
        )
    )

    assert response.status_code == 401
    assert sink.records
    sink.assert_absent(presented)


def test_a_submitted_refresh_token_never_reaches_a_record(monkeypatch, sink):
    presented = canary()
    session = FakeSession(clients=[FakeClient()], tokens=[])
    monkeypatch.setattr(oauth, "async_session", lambda: session)

    response = asyncio.run(
        oauth._handle_refresh({"refresh_token": presented}, _request("/token"))
    )

    assert response.status_code == 400
    assert sink.records
    sink.assert_absent(presented, oauth._hash(presented))


def test_a_submitted_revocation_client_secret_never_reaches_a_record(
    monkeypatch, sink
):
    presented = canary()
    token = FakeToken(
        grant_id="g1",
        token_type="refresh",
        user_id=5,
        expires_at=in_hours(720),
        token_hash=oauth._hash("a-real-token"),
    )
    client = FakeClient(
        token_endpoint_auth_method="client_secret_post",
        client_secret_hash=oauth._hash("the-real-secret"),
    )
    session = FakeSession(clients=[client], tokens=[token])
    monkeypatch.setattr(oauth, "async_session", lambda: session)
    request = _request("/revoke")
    request._form = {
        "token": "a-real-token",
        "client_id": "client123",
        "client_secret": presented,
    }

    response = asyncio.run(oauth.revoke_token.__wrapped__(request))

    assert response.status_code == 401
    assert sink.records
    sink.assert_absent(presented)


# --- submitted: bearers at the request boundary --------------------------


class _EmptyResult:
    def scalars(self):
        return self

    def all(self):
        return []

    def scalar_one_or_none(self):
        return None

    def first(self):
        return None


class _MiddlewareSession:
    """Resolves nothing, so every bearer is an `invalid_key` refusal."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def commit(self):
        pass

    async def execute(self, *_a, **_kw):
        return _EmptyResult()


def _drive_middleware(monkeypatch, bearer: str) -> int:
    sent: list = []

    async def downstream(_scope, _receive, _send):  # pragma: no cover - unreachable
        raise AssertionError("an unresolvable bearer must not reach the app")

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    monkeypatch.setattr(mcp_auth, "async_session", lambda: _MiddlewareSession())
    app = mcp_auth.APIKeyMiddleware(downstream)
    asyncio.run(
        app(
            {
                "type": "http",
                "method": "POST",
                "path": "/mcp/",
                "headers": [(b"authorization", f"Bearer {bearer}".encode())],
                "client": ("203.0.113.7", 54321),
            },
            receive,
            send,
        )
    )
    return next(m["status"] for m in sent if m["type"] == "http.response.start")


@pytest.mark.parametrize("shape", ["opaque", "omcp"])
def test_a_submitted_mcp_bearer_never_reaches_a_record(monkeypatch, sink, shape):
    value = canary()
    bearer = f"omcp_{value}" if shape == "omcp" else value

    status = _drive_middleware(monkeypatch, bearer)

    assert status == 401
    assert sink.records, "a refused bearer must have produced an auth_failure"
    sink.assert_absent(value)
    # And what *did* reach the record is the one permitted form.
    tags = [
        getattr(record, "token_tag", None)
        for record in sink.records
        if record.getMessage() == "auth_failure"
    ]
    assert tags and all(
        tag == "sha:" + hashlib.sha256(bearer.encode()).hexdigest()[:8] for tag in tags
    )


def test_a_transfer_redemption_bearer_survives_only_as_a_tag(sink):
    """The boundary check for the position Slice D's route will fill.

    `redacted_token_tag` is the only function that turns a presented credential
    into something loggable, so a bearer that cannot survive it cannot reach a
    record by any route.
    """
    value = canary()

    tag = security_events.redacted_token_tag(value)
    security_events.emit(
        "auth_failure",
        subject="ip:203.0.113.7",
        reason="invalid_key",
        token_tag=tag,
        client_ip="203.0.113.7",
        route="/transfer/upload/info",
    )

    assert tag.startswith("sha:") and len(tag) == 12
    assert sink.records
    sink.assert_absent(value)
    # An absent credential yields an absent field, never `sha:` of the empty
    # string — a constant that reads like a tag on every anonymous request.
    assert security_events.redacted_token_tag(None) is None
    assert security_events.redacted_token_tag("") is None


# --- generated: what the server produced ---------------------------------


class _RegisterRequest:
    def __init__(self, payload):
        self._payload = payload
        self.client = type("_C", (), {"host": "198.51.100.4"})()
        self.url = type("_U", (), {"path": "/register"})()

    async def json(self):
        return self._payload


class _CollectingSession:
    def __init__(self):
        self.added = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        pass

    async def rollback(self):
        pass


def test_the_generated_dcr_client_secret_never_reaches_a_record(monkeypatch, sink):
    """The `client_id` is a public identifier the catalogue logs on purpose;
    the secret returned beside it is the thing that must never appear."""
    monkeypatch.setattr(oauth, "async_session", lambda: _CollectingSession())

    response = asyncio.run(
        oauth.register_client.__wrapped__(
            _RegisterRequest(
                {
                    "client_name": "Some Connector",
                    "redirect_uris": [REGISTERED_URI],
                    "token_endpoint_auth_method": "client_secret_post",
                }
            )
        )
    )

    registration = json.loads(response.body)
    assert response.status_code == 201
    assert sink.records
    sink.assert_absent(registration["client_secret"])
    # The public half is present deliberately, so the canary check above is a
    # statement about the secret and not about the search being blind.
    assert registration["client_id"] in sink.rendered()


class _AuthorizeSession:
    def __init__(self, client):
        self._client = client
        self.added = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def execute(self, stmt, *_a, **_kw):
        from sqlalchemy.sql.dml import Update

        from _oauth_grant_fakes import _Result

        if isinstance(stmt, Update):
            return _Result([])
        return _Result([self._client])

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        pass


def test_the_generated_authorization_code_never_reaches_a_record(monkeypatch, sink):
    client = FakeClient(redirect_uris=[REGISTERED_URI], user_id=5)
    monkeypatch.setattr(oauth, "async_session", lambda: _AuthorizeSession(client))
    monkeypatch.setattr(oauth.settings, "multi_user_mode", True, raising=False)

    async def _session_user(_request, _session):
        return type("_U", (), {"id": 5})()

    monkeypatch.setattr(oauth, "get_active_session_user", _session_user)
    server_state = "csrfstatetoken1234567890"
    signed = oauth._state_serializer().dumps(server_state)
    request = _request(
        "/authorize", headers=[(b"cookie", f"oauth_state={signed}".encode())]
    )

    response = asyncio.run(
        oauth.authorize_post(
            request=request,
            action="approve",
            client_id="client123",
            redirect_uri=REGISTERED_URI,
            code_challenge=oauth._base64url_sha256("v" * 64),
            code_challenge_method="S256",
            scope="read",
            state=server_state,
            client_state="",
        )
    )

    assert response.status_code == 302
    code = response.headers["location"].split("code=")[1].split("&")[0]
    assert len(code) == 64
    assert sink.records
    sink.assert_absent(code, oauth._hash(code))


def test_the_generated_token_pair_never_reaches_a_record(monkeypatch, sink):
    session = SeqSession([_code_row(), FakeClient()])
    monkeypatch.setattr(oauth, "async_session", lambda: session)
    monkeypatch.setattr(oauth.settings, "multi_user_mode", True, raising=False)

    response = asyncio.run(
        oauth._handle_auth_code(
            {
                "code": "a-real-code",
                "code_verifier": "v" * 64,
                "redirect_uri": REGISTERED_URI,
            },
            _request("/token"),
        )
    )

    minted = json.loads(response.body)
    assert response.status_code == 200
    assert sink.records
    sink.assert_absent(minted["access_token"], minted["refresh_token"])
    # The grant id is logged on purpose, so the search is demonstrably live.
    rows = [obj for obj in session.added if isinstance(obj, OAuthToken)]
    assert rows[0].grant_id in sink.rendered()


def test_the_rotated_token_pair_never_reaches_a_record(monkeypatch, sink):
    presented = "r" * 64
    family = [
        FakeToken(grant_id="g1", token_type="access", user_id=5, expires_at=in_hours(1)),
        FakeToken(
            grant_id="g1",
            token_type="refresh",
            user_id=5,
            expires_at=in_hours(720),
            token_hash=oauth._hash(presented),
        ),
    ]
    session = FakeSession(clients=[FakeClient()], tokens=family)
    monkeypatch.setattr(oauth, "async_session", lambda: session)
    monkeypatch.setattr(oauth.settings, "multi_user_mode", True, raising=False)

    response = asyncio.run(
        oauth._handle_refresh({"refresh_token": presented}, _request("/token"))
    )

    rotated = json.loads(response.body)
    assert response.status_code == 200
    assert sink.records
    sink.assert_absent(rotated["access_token"], rotated["refresh_token"])


def test_a_generated_omcp_key_never_reaches_a_record(monkeypatch, sink):
    """The shape `src/api/routes.py` mints, driven through the one middleware
    that ever sees a presented key."""
    raw_key = f"omcp_{secrets.token_hex(32)}"

    status = _drive_middleware(monkeypatch, raw_key)

    assert status == 401
    assert sink.records
    sink.assert_absent(raw_key.removeprefix("omcp_"))
