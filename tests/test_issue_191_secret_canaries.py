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

Two of the positions are covered twice over. `redacted_token_tag` — the
single function in the codebase that turns a presented credential into
something loggable, and therefore the only way one could reach a record — is
pinned at the boundary; and now that Slices C and D have landed and those
surfaces actually emit, the same secrets are also driven through their **real**
paths: a transfer token is minted by `request_upload` and redeemed against
`/transfer/*` until it is refused, an `omcp_` key is created through
`POST /api/keys` and presented to the middleware, and a read-only key drives
`create_note` to a `tool_write_refused`. A canary that is only planted proves
the plumbing; one that is captured from the server and driven through the route
proves the record.
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
from src.api import routes as api_routes
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


class _KeyUser:
    id = 1
    is_admin = True
    is_active = True
    username = "max"


class _KeyCreateSession:
    """Enough session for `create_key`: it adds one row and refreshes its id."""

    def __init__(self):
        self.added = []

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        return None

    async def refresh(self, obj):
        if getattr(obj, "id", None) is None:
            obj.id = 99


def _create_real_key(permission: str = "readwrite"):
    """Create a key through the real handler and return `(raw_key, row)`.

    Captured, never synthesised. A test that builds `omcp_` + 64 hex characters
    itself proves that *that shape* does not leak; it proves nothing about the
    value `create_key` actually hands a caller, which is the one an operator
    would have to rotate. `key_prefix` — the first twelve characters, and the
    thing that used to ride into `tool_write_refused` as `actor_ref` — is
    derived by the handler, so it comes back with the row.
    """
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/keys",
            "headers": [],
            "query_string": b"",
            "client": ("203.0.113.7", 1234),
            "state": {},
            "app": None,
        }
    )
    session = _KeyCreateSession()
    response = asyncio.run(
        api_routes.create_key(
            request=request,
            req=api_routes.CreateKeyRequest(name="canary", permission=permission),
            session=session,
            user=_KeyUser(),
        )
    )
    return response.key, session.added[0]


def test_a_generated_omcp_key_never_reaches_a_record(monkeypatch, sink):
    """The key the server actually returned, driven through the one middleware
    that ever sees a presented key."""
    raw_key, row = _create_real_key()
    assert raw_key.startswith("omcp_") and len(raw_key) == 69
    assert row.key_prefix == raw_key[:12], "the row's prefix is part of the key"

    status = _drive_middleware(monkeypatch, raw_key)

    assert status == 401
    assert sink.records
    # The whole value, and the prefix the row carries — which is a twelve
    # character substring of it, i.e. exactly the length this file refuses.
    sink.assert_absent(raw_key.removeprefix("omcp_"))
    assert row.key_prefix not in sink.rendered()


# --- generated: the credential-attributed tool events ---------------------


def test_a_read_only_key_refused_at_create_note_leaks_no_part_of_itself(
    monkeypatch, sink, tmp_path
):
    """`tool_write_refused`, with a *live* credential bound the way the
    middleware binds one.

    This is the record that shipped carrying `actor_ref` — which for an
    API-key caller is `api_keys.key_prefix`, the first twelve characters of the
    key the caller is still using. The credential is now named by row id
    (`key_id`), and the assertion is the general rule: no security record
    carries a substring of a credential.
    """
    from src.auth.session import current_actor
    from src.mcp_server import tools
    from src.mcp_server.auth import current_api_key_id, current_permission

    raw_key, row = _create_real_key(permission="read")
    row.id = 4242

    async def _no_usage_row(*_args, **_kwargs):
        return True

    monkeypatch.setattr(tools, "_log_usage", _no_usage_row)
    monkeypatch.setattr(tools.settings, "vault_path", str(tmp_path))

    permission = current_permission.set("read")
    key_id = current_api_key_id.set(row.id)
    # Exactly what `APIKeyMiddleware` binds out of the key row it just loaded.
    actor = current_actor.set(("api_key", row.name, row.key_prefix))
    try:
        refusal = asyncio.run(tools.create_note_impl("Notes/canary.md", "body"))
    finally:
        current_actor.reset(actor)
        current_api_key_id.reset(key_id)
        current_permission.reset(permission)

    assert refusal.startswith("Permission denied")
    refused = [r for r in sink.records if r.getMessage() == "tool_write_refused"]
    assert len(refused) == 1, "the refusal must have produced the record to search"
    assert getattr(refused[0], "key_id") == 4242, (
        "the credential is named by row id — that is what replaced the prefix"
    )
    assert not hasattr(refused[0], "actor_ref")
    sink.assert_absent(raw_key.removeprefix("omcp_"))
    assert row.key_prefix not in sink.rendered()


# --- generated: a minted transfer capability -----------------------------


class _MintResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return self

    def first(self):
        return self._value


class _MintSession:
    """The session `mint_token` writes through: one credential read, one add."""

    def __init__(self, credential):
        self.credential = credential
        self.added = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def add(self, obj):
        self.added.append(obj)

    async def execute(self, *args, **kwargs):
        return _MintResult(self.credential)

    async def commit(self):
        return None

    async def refresh(self, obj):
        return None


class _RefusalSession:
    """The redemption side, which must reach no database in this test."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def add(self, obj):
        pass

    async def commit(self):
        return None

    async def rollback(self):
        return None


def test_a_minted_transfer_token_never_reaches_a_record(monkeypatch, sink, tmp_path):
    """D16's captured-secret case for the transfer capability.

    The token is **minted by the real `request_upload` path** and read out of
    the URL fragment the tool hands the agent — the exact string a human is told
    to treat as a secret — then presented to `/transfer/*` until the routes
    refuse it. Both refusals emit `transfer_refused`, which is the one event
    that carries a *presented* transfer bearer into the log.

    The hash is swept as well as the token. `transfer_tokens` stores only
    `hash_token(token)`, so a record that leaked the hash would leak the thing
    the admission query compares against — and `token_tag`, which is `sha:` plus
    **eight** hex characters, is deliberately shorter than the twelve this file
    refuses, so the tag survives the sweep and a longer prefix would not.
    """
    from src.mcp_server import tools
    from src.mcp_server.auth import current_api_key_id, current_permission
    from src.models.db import APIKey
    from src.services import transfer, vault_fs
    from src.transfer import routes as transfer_routes

    credential = APIKey(
        id=11,
        name="k",
        key_hash="k" * 64,
        key_prefix="omcp_test",
        permission="readwrite",
        is_active=True,
        expires_at=None,
    )

    async def _no_usage_row(*_args, **_kwargs):
        return True

    (tmp_path / "Attachments").mkdir()
    vault_fs.reset_filesystem_probe_cache()
    monkeypatch.setattr(tools, "_log_usage", _no_usage_row)
    monkeypatch.setattr(tools, "async_session", lambda: _MintSession(credential))
    monkeypatch.setattr(tools.settings, "vault_path", str(tmp_path))
    monkeypatch.setattr(tools.settings, "mcp_hostname", "vault.example.com")
    monkeypatch.setattr(tools.settings, "base_url", "https://vault.example.com")
    monkeypatch.setattr(tools.settings, "_public_origin_explicit", True)

    permission = current_permission.set("readwrite")
    key_id = current_api_key_id.set(11)
    try:
        minted = asyncio.run(tools.request_upload_impl("Attachments/shot.png"))
    finally:
        current_api_key_id.reset(key_id)
        current_permission.reset(permission)
        vault_fs.reset_filesystem_probe_cache()

    line = next(
        l for l in minted.splitlines() if "https://" in l and "/transfer/upload#" in l
    )
    token = line.split("#", 1)[1].strip()
    assert len(token) >= 32, "the fragment did not carry a capability"
    digest = hashlib.sha256(token.encode()).hexdigest()

    # ── redeem it until it is refused ──────────────────────────────────────
    # The row is not in any database this test can reach, so both admission
    # queries answer `None` — which is the *most* interesting case for a
    # canary: it is the branch that runs the diagnosis, builds a `token_tag`
    # out of the presented value, and emits the record.
    async def _no_row(*_args, **_kwargs):
        return None

    async def _unknown(_session, _token, *, direction):
        return transfer.TransferRefusal("unknown_token")

    monkeypatch.setattr(transfer, "lookup_token", _no_row)
    monkeypatch.setattr(transfer, "claim_upload", _no_row)
    monkeypatch.setattr(transfer, "classify_token_refusal", _unknown)
    monkeypatch.setattr(transfer_routes, "async_session", _RefusalSession)

    async def _drive() -> list[int]:
        import httpx

        from src.main import app

        headers = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("203.0.113.7", 4242)),
            base_url="http://localhost:8000",
        ) as client:
            info = await client.get("/transfer/upload/info", headers=headers)
            put = await client.put("/transfer/upload", headers=headers, content=b"x")
        return [info.status_code, put.status_code]

    assert asyncio.run(_drive()) == [404, 404], "the uniform 404 moved"

    refusals = [r for r in sink.records if r.getMessage() == "transfer_refused"]
    assert len(refusals) == 2, "both redemptions must have produced a record"
    tags = {getattr(r, "token_tag", None) for r in refusals}
    assert tags == {"sha:" + digest[:8]}, "the tag is the only permitted form"

    sink.assert_absent(token, digest)
