"""Hermetic twin of `tests/integration/test_issue_332_rollback_expiry_pg.py`.

#332: the key-creation budget refusal rolled back and *then* read the acting
user for its security record. A real `AsyncSession.rollback()` expires the
`User` the request loaded, so that read was a lazy load — `MissingGreenlet`,
a 500 instead of the refusal. The #323 tests missed it because their fake
session's `rollback()` only counted and their user was a `SimpleNamespace`,
which nothing can expire.

The stand-in here models the one property that matters: after the session
rolls back, every attribute read on the user raises, exactly as the expired
instance does. The real-Postgres module proves the model is faithful.
"""
import asyncio

import pytest
from fastapi import HTTPException
from starlette.requests import Request

import src.api.routes as api
import src.control_panel.routes as panel
from src.config import settings
from src.services import rate_limits, security_events


class _Expired(Exception):
    pass


class _ExpiringUser:
    """Reads like a `User` until its session rolls back; then every read raises."""

    def __init__(self, uid, username):
        object.__setattr__(self, "_values", {"id": uid, "username": username,
                                             "is_admin": False, "is_active": True})
        object.__setattr__(self, "expired", False)

    def __getattr__(self, name):
        values = object.__getattribute__(self, "_values")
        if name not in values:
            raise AttributeError(name)
        if object.__getattribute__(self, "expired"):
            raise _Expired(f"lazy load of {name} after rollback")
        return values[name]


class _Session:
    def __init__(self, user):
        self.user = user

    async def rollback(self):
        object.__setattr__(self.user, "expired", True)

    async def execute(self, *a, **k):  # pragma: no cover - cap disabled below
        raise AssertionError("no statement expected")


def _request(path):
    return Request({
        "type": "http", "method": "POST", "path": path, "headers": [],
        "query_string": b"", "client": ("198.51.100.7", 1), "state": {},
        "app": None, "session": {},
    })


@pytest.fixture
def exhausted(monkeypatch):
    monkeypatch.setattr(settings, "key_creation_account_limit", 1)
    monkeypatch.setattr(settings, "key_creation_address_limit", None)
    monkeypatch.setattr(settings, "key_creation_window_seconds", 3600)
    monkeypatch.setattr(settings, "key_max_active_per_account", None)
    rate_limits.reset_key_creation_for_tests()
    assert rate_limits.try_charge_key_creation(("user", 5), None) is None
    seen = []
    monkeypatch.setattr(security_events, "_log", lambda e, lvl, exc, f: seen.append((e, f)))
    security_events.reset_state()
    with security_events.suppression_disabled():
        yield seen
    security_events.reset_state()
    rate_limits.reset_key_creation_for_tests()


def test_json_refusal_after_rollback_is_a_429(exhausted):
    user = _ExpiringUser(5, "eve")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(api.create_key(
            request=_request("/api/keys"),
            req=api.CreateKeyRequest(name="k", daily_request_limit=10),
            session=_Session(user), user=user,
        ))
    assert exc.value.status_code == 429
    assert exc.value.headers["Retry-After"]
    [(event, fields)] = exhausted
    assert event == "key_creation_throttled"
    assert (fields["actor_user_id"], fields["actor_username"]) == (5, "eve")


def test_form_refusal_after_rollback_flashes_and_redirects(exhausted):
    user = _ExpiringUser(5, "eve")
    request = _request("/admin/keys/create")
    response = asyncio.run(panel.create_key_form(
        request=request, name="k", permission="read", daily_request_limit="10",
        unlimited="", session=_Session(user), user=user,
    ))
    assert response.status_code == 303
    assert "Too many keys" in request.session["flash_key_error"]
    [(event, fields)] = exhausted
    assert event == "key_creation_throttled"
    assert (fields["actor_user_id"], fields["actor_username"]) == (5, "eve")
