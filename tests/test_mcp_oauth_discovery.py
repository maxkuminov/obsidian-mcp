"""Regression test for PR #30: OAuth discovery for redirect-averse MCP clients.

Two cooperating fixes let clients like claude.ai (which don't follow redirects
on POST) complete the OAuth flow against /mcp:

1. `MCPSlashRewriteMiddleware` (src/main.py) rewrites `/mcp` -> `/mcp/` *in the
   ASGI scope* so Starlette's `Mount` matches without emitting a 307. It is
   deliberately scoped to HTTP only — the Streamable HTTP transport has no
   WebSocket handler at /mcp — so a `websocket` scope must pass through
   untouched.
2. `APIKeyMiddleware` (src/mcp_server/auth.py) returns a `WWW-Authenticate:
   Bearer ...` header on every 401 so a client can discover the RFC 9728
   protected-resource metadata. A bare 401 (no header) is what previously left
   the client with nothing to discover the auth server with.

This follow-up extends fix 2 from the original missing-token-only case to the
invalid/expired credential 401s as well (RFC 6750 `error="invalid_token"`),
which is what these tests pin down.

Runs fully offline: no DB, no network, no embedding provider. The 401 for a
missing Bearer token is returned before any DB access, and the rest is pure
header/scope assertion. Config loads hermetically via the env-file guard so the
dev host's `.env` can't leak in.
"""
import asyncio

import pydantic_settings
import pytest

# Load config purely from process env + conftest defaults, independent of the
# dev host's `.env` (which exists in this repo). Mirrors the guard used by the
# other offline tests.
_orig_init = pydantic_settings.BaseSettings.__init__


def _no_env_file_init(self, *args, **kwargs):
    kwargs.setdefault("_env_file", None)
    _orig_init(self, *args, **kwargs)


pydantic_settings.BaseSettings.__init__ = _no_env_file_init
try:
    from src.main import MCPSlashRewriteMiddleware
    from src.mcp_server.auth import APIKeyMiddleware, _www_authenticate
    from src.mcp_server import auth as auth_mod
finally:
    pydantic_settings.BaseSettings.__init__ = _orig_init


_METADATA_PATH = "/.well-known/oauth-protected-resource/mcp"


def _run(coro):
    return asyncio.run(coro)


async def _noop_receive():  # pragma: no cover - never awaited on these paths
    return {"type": "http.request", "body": b"", "more_body": False}


class _CaptureApp:
    """Downstream ASGI app that records the scope it was handed (or that it was
    never called at all)."""

    def __init__(self):
        self.called = False
        self.scope = None

    async def __call__(self, scope, receive, send):
        self.called = True
        self.scope = scope


async def _capture_response(app, scope):
    """Drive an ASGI app that writes its own response and return
    (status, headers_dict). Header names are lower-cased for lookup."""
    messages = []

    async def send(message):
        messages.append(message)

    await app(scope, _noop_receive, send)

    start = next(m for m in messages if m["type"] == "http.response.start")
    headers = {k.decode().lower(): v.decode() for k, v in start.get("headers", [])}
    return start["status"], headers


# --- Slash rewrite middleware -------------------------------------------------


def test_bare_mcp_path_is_rewritten_to_trailing_slash():
    capture = _CaptureApp()
    mw = MCPSlashRewriteMiddleware(capture)
    scope = {"type": "http", "path": "/mcp", "raw_path": b"/mcp"}
    _run(mw(scope, _noop_receive, lambda m: None))
    assert capture.called
    assert capture.scope["path"] == "/mcp/"
    assert capture.scope["raw_path"] == b"/mcp/"
    # The original scope must not be mutated in place.
    assert scope["path"] == "/mcp"


@pytest.mark.parametrize("path", ["/mcp/", "/mcp/messages", "/health", "/"])
def test_other_http_paths_pass_through_unchanged(path):
    capture = _CaptureApp()
    mw = MCPSlashRewriteMiddleware(capture)
    scope = {"type": "http", "path": path, "raw_path": path.encode()}
    _run(mw(scope, _noop_receive, lambda m: None))
    assert capture.scope["path"] == path


def test_websocket_mcp_scope_is_not_rewritten():
    # Deliberately HTTP-only (commit 6a09adf): there is no WS transport at /mcp.
    capture = _CaptureApp()
    mw = MCPSlashRewriteMiddleware(capture)
    scope = {"type": "websocket", "path": "/mcp", "raw_path": b"/mcp"}
    _run(mw(scope, _noop_receive, lambda m: None))
    assert capture.scope["path"] == "/mcp"


# --- WWW-Authenticate header builder -----------------------------------------


def test_www_authenticate_without_error_omits_error_field():
    value = _www_authenticate()
    assert value.startswith("Bearer ")
    assert "error=" not in value
    assert value.endswith(f'resource_metadata="{auth_mod.settings.base_url.rstrip("/")}{_METADATA_PATH}"')


def test_www_authenticate_with_error_includes_rfc6750_code():
    value = _www_authenticate("invalid_token")
    assert 'error="invalid_token"' in value
    assert _METADATA_PATH in value


def test_www_authenticate_uses_configured_base_url(monkeypatch):
    monkeypatch.setattr(auth_mod.settings, "base_url", "https://mcp.example.test/", raising=False)
    value = _www_authenticate("invalid_token")
    assert 'resource_metadata="https://mcp.example.test/.well-known/oauth-protected-resource/mcp"' in value


# --- 401 carries the discovery hint ------------------------------------------


def test_missing_bearer_returns_401_with_discovery_header():
    capture = _CaptureApp()
    mw = APIKeyMiddleware(capture)
    scope = {"type": "http", "method": "POST", "path": "/mcp/", "headers": []}
    status, headers = _run(_capture_response(mw, scope))
    assert status == 401
    assert not capture.called  # never reached the downstream app
    www = headers["www-authenticate"]
    assert www.startswith("Bearer ")
    assert "error=" not in www  # no credential was presented
    assert _METADATA_PATH in www
