"""Regression coverage for ChatGPT's public-client MCP OAuth flow."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pydantic_settings

_orig_init = pydantic_settings.BaseSettings.__init__


def _no_env_file_init(self, *args, **kwargs):
    kwargs.setdefault("_env_file", None)
    _orig_init(self, *args, **kwargs)


pydantic_settings.BaseSettings.__init__ = _no_env_file_init
try:
    from src.models.db import OAuthClient, OAuthCode
    from src.oauth import routes
finally:
    pydantic_settings.BaseSettings.__init__ = _orig_init


class _Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _Session:
    def __init__(self, results=()):
        self.results = iter(results)
        self.added = []
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def execute(self, query, params=None):
        # The token handlers now take a transaction-scoped advisory lock before
        # they read anything (see src/oauth/grants.py). It consumes no canned
        # result, so skip it rather than letting it shift the sequence.
        if "pg_advisory_xact_lock" in str(query):
            return _Result(None)
        return _Result(next(self.results))

    def add(self, value):
        self.added.append(value)

    async def commit(self):
        self.committed = True


class _RegistrationRequest:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


def _json(response):
    return json.loads(response.body)


def test_chatgpt_public_registration_has_no_secret_and_allows_write(monkeypatch):
    session = _Session()
    monkeypatch.setattr(routes, "async_session", lambda: session)

    response = asyncio.run(
        routes.register_client.__wrapped__(
            _RegistrationRequest(
                {
                    "client_name": "ChatGPT",
                    "redirect_uris": ["https://chatgpt.com/connector/oauth/callback"],
                    "token_endpoint_auth_method": "none",
                }
            )
        )
    )

    body = _json(response)
    assert response.status_code == 201
    assert body["token_endpoint_auth_method"] == "none"
    assert "client_secret" not in body
    assert set(body["scope"].split()) == {"read", "readwrite", "offline_access"}
    assert len(session.added) == 1
    assert session.added[0].client_secret_hash is None
    assert session.added[0].token_endpoint_auth_method == "none"


def test_confidential_registration_remains_backward_compatible(monkeypatch):
    session = _Session()
    monkeypatch.setattr(routes, "async_session", lambda: session)

    response = asyncio.run(
        routes.register_client.__wrapped__(
            _RegistrationRequest(
                {
                    "client_name": "Claude",
                    "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
                    "scope": "read",
                }
            )
        )
    )

    body = _json(response)
    assert response.status_code == 201
    assert body["token_endpoint_auth_method"] == "client_secret_post"
    assert body["client_secret"]
    assert session.added[0].client_secret_hash


def test_public_client_token_exchange_accepts_pkce_without_client_secret(monkeypatch):
    verifier = "v" * 64
    client = OAuthClient(
        client_id="chatgpt-client",
        client_secret_hash=None,
        token_endpoint_auth_method="none",
        client_name="ChatGPT",
        redirect_uris=["https://chatgpt.com/connector/oauth/callback"],
        scope="read readwrite",
    )
    code = OAuthCode(
        code_hash=routes._hash("authorization-code"),
        client_id=client.client_id,
        redirect_uri=client.redirect_uris[0],
        scope="read",
        code_challenge=routes._base64url_sha256(verifier),
        code_challenge_method="S256",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        used=False,
    )
    session = _Session((code, client))
    monkeypatch.setattr(routes, "async_session", lambda: session)

    # ChatGPT has shipped token requests without either confidential-client
    # credentials or a repeated client_id. The one-time code + PKCE verifier
    # still identifies and authenticates the registered public client.
    response = asyncio.run(
        routes._handle_auth_code(
            {
                "code": "authorization-code",
                "code_verifier": verifier,
                "redirect_uri": client.redirect_uris[0],
            }
        )
    )

    body = _json(response)
    assert response.status_code == 200
    assert body["access_token"]
    assert body["refresh_token"]
    assert code.used is True
    assert session.committed is True
    assert len(session.added) == 2
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"


def test_oauth_redirect_state_is_encoded_not_injected():
    url = routes._append_query(
        "https://client.example/callback?existing=1",
        code="safe-code",
        state="opaque&code=attacker-code",
    )

    assert "existing=1" in url
    assert "state=opaque%26code%3Dattacker-code" in url
    assert url.count("code=") == 1


def test_invalid_pkce_verifier_returns_invalid_grant(monkeypatch):
    client = OAuthClient(
        client_id="chatgpt-client",
        client_secret_hash=None,
        token_endpoint_auth_method="none",
        client_name="ChatGPT",
        redirect_uris=["https://chatgpt.com/connector/oauth/callback"],
        scope="read",
    )
    code = OAuthCode(
        code_hash=routes._hash("authorization-code"),
        client_id=client.client_id,
        redirect_uri=client.redirect_uris[0],
        scope="read",
        code_challenge="A" * 43,
        code_challenge_method="S256",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        used=False,
    )
    session = _Session((code, client))
    monkeypatch.setattr(routes, "async_session", lambda: session)

    response = asyncio.run(
        routes._handle_auth_code(
            {
                "code": "authorization-code",
                "code_verifier": "not-ascii-é",
                "redirect_uri": client.redirect_uris[0],
            }
        )
    )

    assert response.status_code == 400
    assert _json(response)["error"] == "invalid_grant"
    assert code.used is False


def test_confidential_token_exchange_still_requires_secret(monkeypatch):
    verifier = "v" * 64
    client = OAuthClient(
        client_id="confidential-client",
        client_secret_hash=routes._hash("secret"),
        token_endpoint_auth_method="client_secret_post",
        client_name="Claude",
        redirect_uris=["https://claude.ai/api/mcp/auth_callback"],
        scope="read",
    )
    code = OAuthCode(
        code_hash=routes._hash("authorization-code"),
        client_id=client.client_id,
        redirect_uri=client.redirect_uris[0],
        scope="read",
        code_challenge=routes._base64url_sha256(verifier),
        code_challenge_method="S256",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        used=False,
    )
    session = _Session((code, client))
    monkeypatch.setattr(routes, "async_session", lambda: session)

    response = asyncio.run(
        routes._handle_auth_code(
            {
                "code": "authorization-code",
                "client_id": client.client_id,
                "code_verifier": verifier,
                "redirect_uri": client.redirect_uris[0],
            }
        )
    )

    assert response.status_code == 401
    assert _json(response)["error"] == "invalid_client"
    assert code.used is False
