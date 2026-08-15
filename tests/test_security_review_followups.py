from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.auth.session import get_current_user
from src.control_panel import users as panel_users
from src.mcp_server import tools
from src.oauth import routes as oauth_routes
from src.services import embeddings, search


@pytest.mark.parametrize(
    ("value", "maximum", "expected"),
    [(0, 500, 1), (-10, 500, 1), (10**9, 500, 500), (10**9, 50, 50)],
)
def test_query_limit_clamp(value, maximum, expected):
    assert tools._clamp_limit(value, maximum) == expected


@pytest.mark.asyncio
async def test_full_text_search_clamps_limit(monkeypatch):
    session = AsyncMock()
    result = MagicMock()
    result.all.return_value = []
    session.execute.return_value = result
    monkeypatch.setattr(search, "combined_tsquery", lambda _query: "query")

    await search.full_text_search(session, "needle", limit=10**9)

    statement = session.execute.await_args.args[0]
    assert statement._limit_clause.value == 500


@pytest.mark.asyncio
async def test_semantic_search_clamps_before_overfetch(monkeypatch):
    session = AsyncMock()
    result = MagicMock()
    result.fetchall.return_value = []
    # Three `SET LOCAL`s (ef_search, random_page_cost, iterative_scan) then
    # the vector select.
    session.execute.side_effect = [None, None, None, result]
    monkeypatch.setattr(
        embeddings, "get_embedding", AsyncMock(return_value=[0.0] * 1024)
    )

    await embeddings.semantic_search(session, "needle", limit=10**9)

    statement = session.execute.await_args_list[-1].args[0]
    assert statement._limit_clause.value == 250


class _Request:
    def __init__(self, session):
        self.session = session


@pytest.mark.asyncio
async def test_current_user_accepts_matching_session_version(monkeypatch):
    monkeypatch.setattr("src.auth.session.settings.multi_user_mode", True)
    user = SimpleNamespace(id=7, session_version=3, is_active=True)
    result = MagicMock()
    result.scalar_one_or_none.return_value = user
    db = AsyncMock()
    db.execute.return_value = result
    request = _Request({"user_id": 7, "session_version": 3})

    assert await get_current_user(request, db) is user
    assert request.session["user_id"] == 7


@pytest.mark.asyncio
@pytest.mark.parametrize("cookie_version", [None, 2])
async def test_current_user_rejects_and_clears_stale_session(monkeypatch, cookie_version):
    monkeypatch.setattr("src.auth.session.settings.multi_user_mode", True)
    user = SimpleNamespace(id=7, session_version=3, is_active=True)
    result = MagicMock()
    result.scalar_one_or_none.return_value = user
    db = AsyncMock()
    db.execute.return_value = result
    session = {"user_id": 7}
    if cookie_version is not None:
        session["session_version"] = cookie_version
    request = _Request(session)

    assert await get_current_user(request, db) is None
    assert request.session == {}


@pytest.mark.asyncio
async def test_password_reset_bumps_session_version(monkeypatch):
    target = SimpleNamespace(
        id=7, password_hash="old", session_version=3, username="alice"
    )
    result = MagicMock()
    result.scalar_one_or_none.return_value = target
    db = AsyncMock()
    db.execute.return_value = result
    monkeypatch.setattr(panel_users, "hash_password", lambda _password: "new-hash")

    response = await panel_users.reset_password(
        user_id=7,
        new_password="new-password",
        session=db,
        user=SimpleNamespace(is_admin=True),
    )

    assert response.status_code == 303
    assert target.password_hash == "new-hash"
    assert target.session_version == 4
    db.commit.assert_awaited_once()


@pytest.mark.parametrize(
    ("base_url", "secure_fragment"),
    [
        ("http://localhost:8000", "Secure"),
        ("https://notes.example.com", "Secure"),
    ],
)
@pytest.mark.asyncio
async def test_oauth_state_cookie_matches_base_url_transport(
    monkeypatch, base_url, secure_fragment
):
    client = SimpleNamespace(
        client_name="Test", redirect_uris=["https://client.example/callback"], scope="read"
    )
    result = MagicMock()
    result.scalar_one_or_none.return_value = client
    db = AsyncMock()
    db.execute.return_value = result
    context = AsyncMock()
    context.__aenter__.return_value = db
    monkeypatch.setattr(oauth_routes, "async_session", lambda: context)
    monkeypatch.setattr(oauth_routes.settings, "base_url", base_url)

    request = MagicMock()
    response = await oauth_routes.authorize_get(
        request=request,
        response_type="code",
        client_id="client",
        redirect_uri="https://client.example/callback",
        code_challenge="a" * 43,
        code_challenge_method="S256",
        scope="read",
        state="state",
    )

    cookie = response.headers["set-cookie"]
    if base_url.startswith("https://"):
        assert secure_fragment in cookie
    else:
        assert secure_fragment not in cookie
