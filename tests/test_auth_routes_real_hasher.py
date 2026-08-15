"""Register → login through the auth routes with the *real* password hasher.

`tests/test_security_review_followups.py::test_password_reset_bumps_session_version`
monkeypatches `hash_password`, and it is the only login-adjacent coverage there
was. That is precisely why a totally broken hasher — `CryptContext` raising at
construction under bcrypt 4.1+ — passed the suite while taking down every login
in production. Nothing here is monkeypatched except the database and the
filesystem: `register_submit` computes a real bcrypt hash and `login_submit`
verifies it with the real `verify_password`, so an unusable hasher fails here.
"""
import itertools
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.requests import Request

from src.auth import routes as auth_routes
from src.limiter import limiter
from src.services import vault as vault_service

PASSWORD = "correct horse battery staple"
USERNAME = "routeadmin"

# `login_submit` is wrapped in slowapi's `@limiter.limit("5/minute")`, which
# insists on a real `starlette.requests.Request` and buckets by client IP in a
# process-wide store. Handing every request its own address keeps the tests
# independent of each other and of collection order.
_client_ips = itertools.count(1)


@pytest.fixture(autouse=True)
def _clean_rate_limiter():
    """Empty slowapi's process-wide bucket store around every test here.

    `limiter` is a module-level singleton shared by the whole test session, so
    hits recorded here would otherwise outlive the test that made them and
    could push an unrelated test over `5/minute`. Distinct client IPs already
    keep these tests apart; this closes the leak in the other direction, so
    nothing this module does can trip a limit somewhere else.
    """
    limiter.reset()
    yield
    limiter.reset()


def _make_request(path: str) -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "root_path": "",
            "query_string": b"",
            "headers": [(b"host", b"testserver")],
            "client": (f"10.0.0.{next(_client_ips) % 250 + 1}", 12345),
            "server": ("testserver", 80),
            "session": {},
            "state": {},
        }
    )


def _empty_users_session():
    """AsyncMock session whose `users` table reads as empty."""
    result = MagicMock()
    result.scalar.return_value = 0
    result.scalar_one_or_none.return_value = None
    session = AsyncMock()
    session.execute.return_value = result
    session.add = MagicMock()
    return session


def _session_returning(user):
    result = MagicMock()
    result.scalar_one_or_none.return_value = user
    session = AsyncMock()
    session.execute.return_value = result
    return session


async def _register(tmp_path, monkeypatch, password=PASSWORD):
    """Drive `POST /admin/register` and return the User the route built."""
    monkeypatch.setattr(vault_service.settings, "vault_path", str(tmp_path))
    request = _make_request("/admin/register")
    session = _empty_users_session()

    response = await auth_routes.register_submit(
        request=request,
        username=USERNAME,
        password=password,
        password_confirm=password,
        vault_path=str(tmp_path),
        session=session,
    )

    assert response.status_code == 302, "registration should have succeeded"
    session.add.assert_called_once()
    return session.add.call_args.args[0]


async def _login(username, password, user):
    request = _make_request("/admin/auth/login")
    session = _session_returning(user)
    response = await auth_routes.login_submit(
        request=request,
        username=username,
        password=password,
        next="/admin/",
        session=session,
    )
    return request, response


@pytest.mark.asyncio
async def test_register_then_login_round_trip_with_real_hasher(tmp_path, monkeypatch):
    new_user = await _register(tmp_path, monkeypatch)

    # A real bcrypt hash, not a monkeypatched sentinel.
    assert new_user.password_hash.startswith("$2b$12$")
    assert new_user.password_hash != PASSWORD

    # The route object never went through a DB flush, so give it the columns
    # `login_submit` reads.
    stored = SimpleNamespace(
        id=1,
        username=USERNAME,
        password_hash=new_user.password_hash,
        is_active=True,
        is_admin=True,
        session_version=1,
    )

    request, response = await _login(USERNAME, PASSWORD, stored)

    assert response.status_code == 302
    assert response.headers["location"] == "/admin/"
    assert request.session["user_id"] == 1
    assert request.session["username"] == USERNAME
    assert request.session["is_admin"] is True


@pytest.mark.asyncio
async def test_login_rejects_wrong_password_against_real_hash(tmp_path, monkeypatch):
    new_user = await _register(tmp_path, monkeypatch)
    stored = SimpleNamespace(
        id=1,
        username=USERNAME,
        password_hash=new_user.password_hash,
        is_active=True,
        is_admin=True,
        session_version=1,
    )

    request, response = await _login(USERNAME, PASSWORD + "!", stored)

    assert response.status_code == 401
    assert "user_id" not in request.session


@pytest.mark.asyncio
async def test_register_then_login_with_over_72_byte_password(tmp_path, monkeypatch):
    """The length that broke passlib's probe must survive the whole route pair."""
    long_password = "correct horse battery staple " * 4  # 116 bytes
    assert len(long_password.encode("utf-8")) > 72

    new_user = await _register(tmp_path, monkeypatch, password=long_password)
    stored = SimpleNamespace(
        id=1,
        username=USERNAME,
        password_hash=new_user.password_hash,
        is_active=True,
        is_admin=True,
        session_version=1,
    )

    _request, response = await _login(USERNAME, long_password, stored)
    assert response.status_code == 302
