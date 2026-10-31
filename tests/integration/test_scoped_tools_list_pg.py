"""#358 on real Postgres: a panel scope change shows on the grant's next list.

The offline suite swaps a stubbed credential; this module takes the real
path end to end — the real `APIKeyMiddleware._authenticate` reading the token
row (`token_has_write` over its stored scope), the real panel handler
`update_oauth_token_scope` writing the whole family's scope, and the real SDK
transport answering `tools/list`. It reuses the throwaway-database harness of
`test_oauth_grant_lifetime_pg.py` (its fixtures are imported, so they run with
that module's own database name prefix and lifecycle).
"""
from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select

import src.mcp_server.auth as mcp_auth
from src.auth.session import _SingleUserSentinel
from src.control_panel import routes as panel
from src.mcp_server.server import mcp
from src.models.db import OAuthToken
from src.oauth import routes as oauth
from tests.integration.test_oauth_grant_lifetime_pg import (  # noqa: F401 - fixtures
    PGVECTOR_TEST_ADMIN_URL,
    clean,
    engine,
    migrated_database,
    monkeypatch_module,
    seed_client,
    sessionmaker,
)

pytestmark = [
    pytest.mark.asyncio(loop_scope="module"),
    pytest.mark.skipif(not PGVECTOR_TEST_ADMIN_URL, reason="PGVECTOR_TEST_ADMIN_URL not set"),
]

UTC = timezone.utc
HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


async def _seed_read_family(maker) -> tuple[str, int]:
    access = secrets.token_hex(32)
    now = datetime.now(UTC)
    async with maker() as session:
        row = OAuthToken(
            token_hash=oauth._hash(access), token_type="access", client_id="c1",
            scope="read offline_access", grant_id="g358", grant_issued_at=now,
            expires_at=now + timedelta(hours=1),
        )
        session.add(row)
        session.add(OAuthToken(
            token_hash=oauth._hash(secrets.token_hex(32)), token_type="refresh",
            client_id="c1", scope="read offline_access", grant_id="g358",
            grant_issued_at=now, expires_at=now + timedelta(days=30),
        ))
        await session.commit()
        return access, row.id


def _messages(response: httpx.Response) -> list[dict]:
    if response.headers.get("content-type", "").startswith("application/json"):
        return [response.json()]
    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]


async def test_a_panel_upgrade_lists_28_on_the_next_request_and_notifies_nothing(clean, monkeypatch_module):
    from mcp.server.fastmcp.server import StreamableHTTPASGIApp
    from mcp.server.session import ServerSession
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

    maker = clean
    await seed_client(maker)
    access, token_id = await _seed_read_family(maker)

    notified = []

    async def spy(self, *a, **kw):  # pragma: no cover - must not run
        notified.append(1)

    monkeypatch_module.setattr(ServerSession, "send_tool_list_changed", spy)

    manager = StreamableHTTPSessionManager(app=mcp._mcp_server, stateless=True)
    app = mcp_auth.APIKeyMiddleware(StreamableHTTPASGIApp(manager))

    async def tools_list(client) -> list[dict]:
        response = await client.post(
            "/", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={**HEADERS, "authorization": f"Bearer {access}"},
        )
        assert response.status_code == 200, response.text[:300]
        return _messages(response)

    async with manager.run():
        transport = httpx.ASGITransport(app=app, client=("198.51.100.58", 4242))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            before = await tools_list(client)

            async with maker() as session:
                await panel.update_oauth_token_scope(
                    token_id=token_id, request=None, scope="readwrite",
                    session=session, user=_SingleUserSentinel(),
                )
            async with maker() as session:
                scopes = (await session.execute(
                    select(OAuthToken.scope).where(OAuthToken.grant_id == "g358")
                )).scalars().all()
            assert scopes and all("readwrite" in s.split() for s in scopes)

            after = await tools_list(client)

    assert len(before) == 1 and len(after) == 1, "one result message, nothing else on the wire"
    assert len(before[0]["result"]["tools"]) == 19
    assert len(after[0]["result"]["tools"]) == 28
    for message in before + after:
        assert message.get("method") != "notifications/tools/list_changed"
    assert notified == []
