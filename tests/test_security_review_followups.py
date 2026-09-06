from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import session_helpers
from src.auth.session import get_current_user
from src.control_panel import users as panel_users
from src.control_panel.flash import FLASH_SESSION_KEY
from src.mcp_server import tools
from src.models.db import User
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
    # Three `SET LOCAL`s (ef_search, random_page_cost, iterative_scan), the
    # vector select, then — because the owner predicate makes every query
    # filtered (#127, D1a) — the zero-row exact re-run: one more `SET LOCAL`
    # and the identical statement.
    session.execute.side_effect = [None, None, None, result, None, result]
    monkeypatch.setattr(
        embeddings, "get_embedding", AsyncMock(return_value=[0.0] * 1024)
    )

    await embeddings.semantic_search(session, "needle", limit=10**9)

    statement = session.execute.await_args_list[-1].args[0]
    assert statement._limit_clause.value == 250
    # The re-run is the identical statement, so the clamp holds on both.
    assert session.execute.await_args_list[3].args[0] is statement


class _Request:
    def __init__(self, session):
        self.session = session


# Since #198 a browser session is a server-side row, not a cookie: both tests
# below therefore mint one through `session_helpers.sign_in`, which drives the
# production `start_session`. Hand-building the cookie here is exactly the
# drift that helper exists to remove — and without a `sid` and a live row every
# assertion below would be answered by the pre-registry refusal rather than by
# the `session_version` check it was written for.


@pytest.mark.asyncio
async def test_current_user_accepts_matching_session_version(monkeypatch):
    monkeypatch.setattr("src.auth.session.settings.multi_user_mode", True)
    user = session_helpers.fake_user(7, session_version=3)
    _sid, request, registry = await session_helpers.sign_in(user)

    assert await get_current_user(request, registry) is user
    assert request.session["user_id"] == 7


@pytest.mark.asyncio
@pytest.mark.parametrize("cookie_version", [None, 2])
async def test_current_user_rejects_and_clears_stale_session(monkeypatch, cookie_version):
    monkeypatch.setattr("src.auth.session.settings.multi_user_mode", True)
    user = session_helpers.fake_user(7, session_version=3)
    _sid, request, registry = await session_helpers.sign_in(user)
    # A live row, so the refusal below is the version check and nothing else.
    if cookie_version is None:
        request.session.pop("session_version")
    else:
        request.session["session_version"] = cookie_version

    assert await get_current_user(request, registry) is None
    assert request.session == {}


class _PanelRequest:
    """The `Request` the panel user handlers take since #138.

    The flash message is parked in `session` on the way to the redirect, so
    a handler called directly still needs somewhere to put it.
    """

    def __init__(self):
        self.session: dict = {}
        self.query_params: dict = {}


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

    request = _PanelRequest()
    response = await panel_users.reset_password(
        user_id=7,
        request=request,
        new_password="new-password",
        session=db,
        user=SimpleNamespace(is_admin=True),
    )

    assert response.status_code == 303
    assert target.password_hash == "new-hash"
    assert target.session_version == 4
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_password_reset_takes_the_admin_guard_lock(monkeypatch):
    """#129: the reset enters the same critical section as edit/delete.

    A password reset rewrites the target's hash and bumps `session_version`,
    which is a full account takeover — it must not run for an actor whose own
    admin access was revoked while the request waited for the lock.
    """
    actor = User(id=1, username="admin", is_admin=True, is_active=True)
    lock = MagicMock()
    actor_row = MagicMock()
    actor_row.one_or_none.return_value = SimpleNamespace(
        is_admin=True, is_active=True
    )
    target = SimpleNamespace(
        id=7, password_hash="old", session_version=3, username="alice"
    )
    target_row = MagicMock()
    target_row.scalar_one_or_none.return_value = target
    # The fourth statement is the session revocation (#198): the reset now
    # revokes every live session of the target inside this same critical
    # section, and `revoke_user_sessions` reads `rowcount` off its result.
    revocation = MagicMock()
    revocation.rowcount = 2
    db = AsyncMock()
    db.execute.side_effect = [lock, actor_row, target_row, revocation]
    monkeypatch.setattr(panel_users, "hash_password", lambda _password: "new-hash")

    request = _PanelRequest()
    response = await panel_users.reset_password(
        user_id=7,
        request=request,
        new_password="new-password",
        session=db,
        user=actor,
    )

    assert response.status_code == 303
    # The advisory lock is the *first* statement, before the actor is re-read
    # and before the target row is loaded.
    assert "pg_advisory_xact_lock" in str(db.execute.await_args_list[0].args[0])
    # And the revocation rides the same transaction: it is issued after the
    # lock and before the single commit, with nothing committed in between.
    assert "UPDATE user_sessions" in str(db.execute.await_args_list[3].args[0])
    assert target.password_hash == "new-hash"
    db.commit.assert_awaited_once()
    db.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_password_reset_refuses_an_actor_demoted_while_queued(monkeypatch):
    """#129: `_actor_still_privileged` runs inside the lock, and refuses."""
    actor = User(id=1, username="admin", is_admin=True, is_active=True)
    lock = MagicMock()
    actor_row = MagicMock()
    actor_row.one_or_none.return_value = SimpleNamespace(
        is_admin=False, is_active=True
    )
    db = AsyncMock()
    db.execute.side_effect = [lock, actor_row]
    monkeypatch.setattr(panel_users, "hash_password", lambda _password: "new-hash")

    request = _PanelRequest()
    response = await panel_users.reset_password(
        user_id=7,
        request=request,
        new_password="new-password",
        session=db,
        user=actor,
    )

    assert response.status_code == 303
    # The refusal message rides the session now, not the query string (#138).
    assert response.headers["location"] == "/admin/users/"
    assert request.session[FLASH_SESSION_KEY] == {
        "message": panel_users._ACTOR_REVOKED_MSG,
        "kind": "err",
    }
    # Nothing was written and the target was never even loaded.
    assert db.execute.await_count == 2
    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()


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
