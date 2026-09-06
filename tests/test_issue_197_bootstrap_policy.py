"""#197 — bootstrap registration is the **fourth** password setter.

D10 says the minimum lives in one constant and every setter applies it. Three
of the four were routed through `validate_new_password` when the policy landed;
bootstrap was not. It kept an eight-character rule of its own, its own
confirmation compare, and no NUL check at all — which meant the most privileged
account on the server, the one created before any administrator exists to fix
it, sat under the weakest rule in the codebase, and a NUL byte typed into the
password field reached `hash_password`, which raises `ValueError`, and came
back as a 500.

The four cases below are the spec's two named scenarios ("The minimum is shared
by every setter" and "A NUL byte is a form error, not a server error") read at
this handler, plus the template check that keeps the browser's `minlength` and
the server's constant from drifting apart again — the drift is what produced
the eight in the first place.

The database is `session_helpers.FakeRegistry`, and the **production**
`register_submit` runs.
"""
from __future__ import annotations

import logging

import pytest

import session_helpers as sh
from src.auth import routes as auth_routes
from src.auth.passwords import MIN_PASSWORD_LENGTH
from src.models.db import User
from src.services import security_events
from src.services import vault as vault_service

#: One under and exactly at the minimum. Written from the constant so raising
#: it does not silently turn the "too short" case into an accepted one.
TOO_SHORT = "a" * (MIN_PASSWORD_LENGTH - 1)
JUST_LONG_ENOUGH = "b" * MIN_PASSWORD_LENGTH


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture
def events(monkeypatch):
    """The `security_events` records this handler emits, suppressor open."""
    handler = _Capture()
    logger = security_events.logger
    logger.addHandler(handler)
    propagate, level = logger.propagate, logger.level
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    try:
        yield handler.records
    finally:
        logger.removeHandler(handler)
        logger.propagate, logger.level = propagate, level


async def _register(tmp_path, monkeypatch, *, password, password_confirm=None):
    """Drive `POST /admin/register` against an empty registry."""
    monkeypatch.setattr(vault_service.settings, "vault_path", str(tmp_path))
    registry = sh.FakeRegistry()
    request = sh.browser_request(method="POST", path="/admin/register")
    response = await auth_routes.register_submit(
        request=request,
        username="rootadmin",
        password=password,
        password_confirm=password if password_confirm is None else password_confirm,
        vault_path=str(tmp_path),
        session=registry,
    )
    return response, registry


def _reason(records) -> str:
    (record,) = [r for r in records if r.getMessage() == "panel_bootstrap_refused"]
    return record.reason


def _created_users(registry) -> list[User]:
    return [obj for obj in registry.added if isinstance(obj, User)]


async def test_one_character_under_the_minimum_is_refused(tmp_path, monkeypatch, events):
    """The scenario "The minimum is shared by every setter", at bootstrap."""
    response, registry = await _register(tmp_path, monkeypatch, password=TOO_SHORT)

    assert response.status_code == 400
    assert str(MIN_PASSWORD_LENGTH) in response.body.decode()
    assert _reason(events) == "weak_password"
    # Nothing written, and — because the validator runs before the critical
    # section — the bootstrap lock was never taken by a request that could
    # not succeed.
    assert _created_users(registry) == []
    assert registry.advisory_locks == []
    assert registry.committed == 0


async def test_the_minimum_itself_is_accepted(tmp_path, monkeypatch):
    """Exactly `MIN_PASSWORD_LENGTH` characters bootstraps the admin."""
    response, registry = await _register(tmp_path, monkeypatch, password=JUST_LONG_ENOUGH)

    assert response.status_code == 302
    assert response.headers["location"] == "/admin/"
    (created,) = _created_users(registry)
    assert created.username == "rootadmin"
    assert created.is_admin is True


async def test_a_confirmation_mismatch_is_refused(tmp_path, monkeypatch, events):
    response, registry = await _register(
        tmp_path,
        monkeypatch,
        password=JUST_LONG_ENOUGH,
        password_confirm=JUST_LONG_ENOUGH + "!",
    )

    assert response.status_code == 400
    assert _reason(events) == "password_mismatch"
    assert _created_users(registry) == []
    assert registry.advisory_locks == []


async def test_a_nul_byte_is_a_form_error_not_a_server_error(tmp_path, monkeypatch, events):
    """The scenario "A NUL byte is a form error, not a server error".

    `hash_password` raises `ValueError` on an embedded NUL — passlib's policy,
    kept deliberately. Before the validator ran here that exception escaped the
    handler; the assertion that matters is that this call **returns** at all.
    """
    password = JUST_LONG_ENOUGH[:-1] + "\x00"
    response, registry = await _register(tmp_path, monkeypatch, password=password)

    assert response.status_code == 400
    body = response.body.decode()
    assert "NUL" in body
    # A refusal is rendered back into a page: it must never echo what was typed.
    assert JUST_LONG_ENOUGH[:-1] not in body
    assert _reason(events) == "password_nul_byte"
    assert _created_users(registry) == []
    assert registry.advisory_locks == []


async def test_the_form_reads_the_minimum_from_the_constant(tmp_path, monkeypatch):
    """`minlength` and the hint are rendered from `MIN_PASSWORD_LENGTH`.

    The eight the handler carried until now was also in this template, twice.
    A number written into the markup is a number that stops tracking the
    server, so the assertion is against the constant, not against `12`.
    """
    monkeypatch.setattr(vault_service.settings, "vault_path", str(tmp_path))
    request = sh.browser_request(method="GET", path="/admin/register")

    response = await auth_routes.register_form(request=request, session=sh.FakeRegistry())

    body = response.body.decode()
    assert body.count(f'minlength="{MIN_PASSWORD_LENGTH}"') == 2
    assert f"Minimum {MIN_PASSWORD_LENGTH} characters" in body
    assert 'minlength="8"' not in body
