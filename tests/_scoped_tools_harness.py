"""Streamable-HTTP harness for the scoped `tools/list` tests (#358).

Not collected (leading underscore). It drives the **real** SDK transport and
the **real** low-level server (`mcp._mcp_server`) over ASGI, with one stand-in:
authentication. `APIKeyMiddleware._authenticate` needs a database; what the
code under test reads from it is only the per-request contextvars it sets, so
`credential_shim` sets exactly those — the same names, the same values and the
same reset-in-`finally` lifecycle as `src/mcp_server/auth.py` (API-key branch
around line 950, OAuth branch around line 1171). That the real middleware
refuses a request with no credential before the app runs is tested separately,
against the real middleware.

A fresh `StreamableHTTPSessionManager` is built per harness (rather than
`mcp.session_manager`, whose `run()` may be entered once per process), with the
arguments `FastMCP.streamable_http_app()` uses, except DNS-rebinding
protection, which is not under test here.
"""
from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass

import httpx

from src.auth.session import (
    current_actor,
    current_principal,
    current_user_id,
    current_vault_root,
)
from src.mcp_server.auth import (
    current_api_key_id,
    current_daily_request_limit,
    current_oauth_token_id,
    current_permission,
)

JSON_HEADERS = {
    "accept": "application/json, text/event-stream",
    "content-type": "application/json",
}


@dataclass(frozen=True)
class Credential:
    kind: str  # "api_key" | "oauth"
    permission: str  # "read" | "readwrite"
    ident: object  # key id, or grant id
    user_id: int = 7

    @property
    def bearer(self) -> str:
        return f"{self.kind}:{self.permission}:{self.ident}"


READ_KEY = Credential("api_key", "read", 11)
READWRITE_KEY = Credential("api_key", "readwrite", 12)
READ_GRANT = Credential("oauth", "read", "grant-read-1")
READ_GRANT_2 = Credential("oauth", "read", "grant-read-2")
READWRITE_GRANT = Credential("oauth", "readwrite", "grant-rw-1")
ALL = {c.bearer: c for c in (READ_KEY, READWRITE_KEY, READ_GRANT, READ_GRANT_2, READWRITE_GRANT)}


def credential_shim(app, vault_root):
    """ASGI wrapper: Bearer `<kind>:<permission>:<id>` → the middleware's contextvars."""

    async def shim(scope, receive, send):
        if scope["type"] != "http":
            return await app(scope, receive, send)
        headers = dict(scope.get("headers") or [])
        raw = headers.get(b"authorization", b"").decode()
        cred = ALL.get(raw[7:]) if raw.startswith("Bearer ") else None
        if cred is None:
            body = b'{"error":"Missing Bearer token"}'
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": body})
            return
        tokens = [
            (current_permission, current_permission.set(cred.permission)),
            (current_user_id, current_user_id.set(cred.user_id)),
            (current_vault_root, current_vault_root.set((cred.user_id, vault_root))),
            (current_daily_request_limit, current_daily_request_limit.set(None)),
            (current_principal, current_principal.set((cred.kind, cred.ident))),
        ]
        if cred.kind == "api_key":
            tokens += [
                (current_api_key_id, current_api_key_id.set(cred.ident)),
                (current_oauth_token_id, current_oauth_token_id.set(None)),
                (current_actor, current_actor.set(("api_key", "test key", "omcp_test"))),
            ]
        else:
            tokens += [
                (current_api_key_id, current_api_key_id.set(None)),
                (current_oauth_token_id, current_oauth_token_id.set(99)),
                (current_actor, current_actor.set(("oauth", "test client", "client-1"))),
            ]
        try:
            await app(scope, receive, send)
        finally:
            for var, tok in reversed(tokens):
                var.reset(tok)

    return shim


@contextlib.asynccontextmanager
async def running_client(server, vault_root):
    """An httpx client against a freshly run session manager for `server`."""
    from mcp.server.fastmcp.server import StreamableHTTPASGIApp
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

    manager = StreamableHTTPSessionManager(
        app=server._mcp_server,
        json_response=server.settings.json_response,
        stateless=True,
        security_settings=None,
        max_request_body_size=server.settings.max_request_body_size,
    )
    app = credential_shim(StreamableHTTPASGIApp(manager), vault_root)
    async with manager.run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client


def rpc(method: str, params: dict | None = None, rid: int = 1) -> dict:
    msg = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


def payload(response: httpx.Response) -> dict:
    """The JSON-RPC message from a JSON or SSE-framed response."""
    ctype = response.headers.get("content-type", "")
    if ctype.startswith("application/json"):
        return response.json()
    for line in response.text.splitlines():
        if line.startswith("data: "):
            return json.loads(line[len("data: "):])
    raise AssertionError(f"no JSON-RPC message in response: {response.status_code} {response.text[:300]!r}")


async def post(client, cred: Credential | None, message: dict) -> httpx.Response:
    headers = dict(JSON_HEADERS)
    if cred is not None:
        headers["authorization"] = f"Bearer {cred.bearer}"
    return await client.post("/", json=message, headers=headers)


async def list_result(client, cred, params: dict | None = None) -> dict:
    response = await post(client, cred, rpc("tools/list", params))
    assert response.status_code == 200, response.text[:300]
    msg = payload(response)
    assert "result" in msg, msg
    return msg["result"]


async def call_tool(client, cred, name: str, arguments: dict) -> dict:
    response = await post(client, cred, rpc("tools/call", {"name": name, "arguments": arguments}))
    assert response.status_code == 200, response.text[:300]
    return payload(response)


def canonical(value) -> bytes:
    """UTF-8 JSON, keys sorted, no insignificant whitespace."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
