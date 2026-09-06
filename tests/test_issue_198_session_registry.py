"""#198 — the panel session is a server-side row with a lifecycle.

The regression the issue asks for is the first test here: sign in, capture the
cookie, log out, replay it, and be refused. Before the registry that replay
answered with `user_id 7` for seven more days, because `logout()` cleared the
cookie and nothing else — and a signed cookie cannot be un-signed. Everything
else in this module exists because the fix has more moving parts than the
regression it closes:

* the **mint** is one guarded critical section that commits (D7a), so the
  cookie it hands back works on the very next request, and a deactivation
  racing it leaves **no row at all** — not even one that comes to life when the
  account is re-enabled;
* the **validator** refuses six ways and clears the cookie every time (D4,
  D14), including for a pre-registry cookie that carries no `sid`;
* the **touch** runs on the request's own session, on safe methods only, and
  may never fail a page (D6);
* the **logout** revokes exactly one row and still signs the browser out when
  the write, or the rollback after it, fails (D8) — recording the exception's
  class name and nothing else.

The database is `session_helpers.FakeRegistry`, which interprets the real
statements against in-memory rows rather than returning canned results, so a
revocation that quietly narrowed itself or a validator that stopped filtering
on `revoked_at` is observed here. The **production** functions run: the mint in
`session_helpers.sign_in` is `start_session` itself.
"""
from __future__ import annotations

import asyncio
import datetime
import hashlib
import logging

import pytest

import session_helpers as sh
from src.auth import routes as auth_routes
from src.auth import session as auth_session
from src.auth.session import (
    SESSION_ID_KEY,
    get_active_session_user,
    hash_session_id,
    revoke_session,
    revoke_user_sessions,
    start_session,
    touch_session,
)
from src.control_panel import routes as panel_routes
from src.limiter import limiter
from src.oauth.grants import ACCOUNT_GUARD_LOCK_KEY, USER_BOOTSTRAP_LOCK_KEY

UTC = datetime.timezone.utc
MIN_SUBSTRING = 12


@pytest.fixture(autouse=True)
def _multi_user(monkeypatch):
    monkeypatch.setattr(auth_session.settings, "multi_user_mode", True)
    monkeypatch.setattr(panel_routes.settings, "multi_user_mode", True)
    limiter.reset()
    yield
    limiter.reset()


@pytest.fixture
def records():
    """Every record the security stream and the root logger emit, captured."""

    class _Capture(logging.Handler):
        def __init__(self):
            super().__init__(level=logging.DEBUG)
            self.records: list[logging.LogRecord] = []

        def emit(self, record):
            self.records.append(record)

    from src.services import security_events

    handler = _Capture()
    root = logging.getLogger()
    events = security_events.logger
    root.addHandler(handler)
    events.addHandler(handler)
    previous_root, previous_events = root.level, events.level
    root.setLevel(logging.DEBUG)
    events.setLevel(logging.DEBUG)
    propagate = events.propagate
    events.propagate = False
    security_events.reset_state()
    try:
        with security_events.suppression_disabled():
            yield handler.records
    finally:
        root.removeHandler(handler)
        events.removeHandler(handler)
        root.setLevel(previous_root)
        events.setLevel(previous_events)
        events.propagate = propagate
        security_events.reset_state()


def _rendered(records) -> str:
    """Every place a value could hide: the message and every raw attribute."""
    parts = []
    for record in records:
        parts.append(record.getMessage())
        parts.extend(str(value) for value in record.__dict__.values())
    return "\n".join(parts)


def _assert_absent(records, *values: str) -> None:
    haystack = _rendered(records)
    for value in values:
        assert value, "an empty value would assert nothing"
        assert value not in haystack
        for start in range(len(value) - MIN_SUBSTRING + 1):
            fragment = value[start : start + MIN_SUBSTRING]
            assert fragment not in haystack, (
                f"a {MIN_SUBSTRING}-character fragment of {value[:8]}… "
                "reached a record"
            )


def _events(records, name: str) -> list[logging.LogRecord]:
    return [r for r in records if r.getMessage() == name]


# --- the regression -------------------------------------------------------


async def test_a_cookie_replayed_after_logout_is_refused(records):
    """#198 itself: the cookie survives logout as a signed value, and dies as a
    credential the moment the row it names is revoked."""
    user = sh.fake_user(7)
    sid, request, registry = await sh.sign_in(user)
    cookie = dict(request.session)

    logout_request = sh.browser_request(
        method="POST", path="/admin/auth/logout", session=dict(cookie)
    )
    response = await auth_routes.logout(logout_request, registry)
    assert response.status_code == 302
    assert logout_request.session == {}

    # The captured cookie is still a perfectly valid signed value; replay it.
    replay = sh.browser_request(path="/admin/keys", session=dict(cookie))
    assert await get_active_session_user(replay, registry) is None
    assert replay.session == {}

    # And through the panel dependency the issue actually exercised: 302 to
    # the login form, not a page.
    from fastapi import HTTPException

    panel_request = sh.browser_request(path="/admin/keys", session=dict(cookie))
    resolved = await auth_session.get_current_user(panel_request, registry)
    assert resolved is None
    with pytest.raises(HTTPException) as raised:
        await panel_routes.require_user_panel(panel_request, resolved, registry)
    assert raised.value.status_code == 302
    assert raised.value.headers["Location"].startswith("/admin/auth/login")

    refusals = _events(records, "panel_session_replay_refused")
    assert [r.reason for r in refusals] == ["revoked_session"] * 2
    _assert_absent(records, sid, hash_session_id(sid))


# --- the mint -------------------------------------------------------------


async def test_the_minted_cookie_authenticates_the_very_next_request():
    """The helper commits, so the row is durable before the cookie leaves."""
    user = sh.fake_user(7)
    sid, request, registry = await sh.sign_in(user)

    assert registry.committed == 1, "the mint owns its commit"
    assert request.session[SESSION_ID_KEY] == sid
    assert request.session["user_id"] == 7

    # A *separately opened* session sees the row: it was committed, not left
    # pending on the minting session.
    elsewhere = sh.FakeRegistry(users=[user], sessions=registry.sessions)
    next_request = sh.browser_request(path="/admin/", session=dict(request.session))
    assert await get_active_session_user(next_request, elsewhere) is user


async def test_the_mint_takes_the_account_guard_before_it_reads():
    user = sh.fake_user(7)
    _sid, _request, registry = await sh.sign_in(user)

    assert registry.advisory_locks == [ACCOUNT_GUARD_LOCK_KEY]
    # The guard first, then the locked re-read, then the insert's commit.
    assert registry.events[0] == ("lock", ACCOUNT_GUARD_LOCK_KEY)
    assert registry.events[-1] == ("commit",)
    locked_read = registry.statements[1]
    assert "FOR UPDATE" in locked_read and "users" in locked_read


async def test_the_mint_stores_a_hash_and_never_the_identifier():
    user = sh.fake_user(7)
    sid, _request, registry = await sh.sign_in(user)

    row = registry.sessions[0]
    assert row.id == hashlib.sha256(sid.encode()).hexdigest()
    assert sid not in str(row.__dict__.values())
    assert row.revoked_at is None
    assert row.expires_at > sh.utcnow()


async def test_a_mint_for_an_inactive_account_writes_nothing():
    user = sh.fake_user(7, is_active=False)
    registry = sh.FakeRegistry(users=[user])
    request = sh.browser_request()

    assert (
        await start_session(
            request,
            registry,
            user.id,
            expected_session_version=user.session_version,
        )
        is None
    )
    assert registry.sessions == []
    assert registry.committed == 0
    assert registry.rolled_back == 1, "the guard must not be held on"
    assert request.session == {}


async def test_a_deactivation_racing_a_login_mints_nothing_even_after_reactivation(
    monkeypatch,
):
    """The window D7a closes. The administrator's deactivation commits while
    the login waits for the guard; the mint re-reads under it and refuses."""
    monkeypatch.setattr(auth_routes, "verify_password", lambda *_a, **_k: True)
    user = sh.fake_user(7)
    registry = sh.FakeRegistry(users=[user])

    def deactivate(_key):
        user.is_active = False

    registry.on_lock = deactivate
    request = sh.browser_request(
        method="POST", path="/admin/auth/login", client=("10.0.0.198", 5555)
    )

    response = await auth_routes.login_submit(
        request=request,
        username=user.username,
        password="whatever",
        next="/admin/",
        session=registry,
    )

    assert response.status_code == 401
    assert registry.sessions == [], "a live row for a just-disabled account"
    # The rendered form's CSRF nonce is not an identity; nothing else survives.
    assert "user_id" not in request.session
    assert SESSION_ID_KEY not in request.session

    # And reactivation does not bring a row from that window to life, because
    # there is no row: that is the whole point of refusing inside the guard.
    user.is_active = True
    assert registry.sessions == []
    replay = sh.browser_request(path="/admin/", session={"user_id": 7, SESSION_ID_KEY: "x"})
    assert await get_active_session_user(replay, registry) is None


async def test_a_deactivation_racing_a_re_issue_leaves_the_browser_signed_out():
    """The password-change re-issue (D13) runs in a *second* guarded
    transaction, so the guard is released between them and the account can be
    disabled in the gap."""
    user = sh.fake_user(7)
    sid, request, registry = await sh.sign_in(user)

    # The change's own transaction: revoke every session, commit.
    assert await revoke_user_sessions(registry, user.id) == 1
    await registry.commit()

    # The re-issue, overtaken by a deactivation.
    user.is_active = False
    assert (
        await start_session(
            request,
            registry,
            user.id,
            expected_session_version=user.session_version,
        )
        is None
    )
    assert [row for row in registry.sessions if row.revoked_at is None] == []

    signed_out = sh.browser_request(path="/admin/", session=dict(request.session))
    assert await get_active_session_user(signed_out, registry) is None
    assert hash_session_id(sid) == registry.sessions[0].id


async def test_a_reset_racing_a_login_mints_nothing_for_the_old_password(monkeypatch):
    """The mint is bound to the generation that authorized it.

    `is_active` alone does not close this window. An administrator's reset
    writes a new hash, **bumps `session_version`** and revokes every row it can
    see — all while a login that has already verified the *old* password waits
    for the account guard. The account stays active throughout, so a mint that
    checked only `is_active` would insert a live row *after* the reset's sweep
    and copy the new version into the cookie: the superseded password would
    come away with a fully valid session, and both invalidators — the registry
    and `session_version` — would have been defeated by the same race they
    exist to close.
    """
    monkeypatch.setattr(auth_routes, "verify_password", lambda *_a, **_k: True)
    user = sh.fake_user(7, session_version=3)
    registry = sh.FakeRegistry(users=[user])

    def reset(_key):
        # What `reset_password` does, in the window: new hash, new generation.
        user.password_hash = "$2b$12$" + "z" * 53
        user.session_version = 4

    registry.on_lock = reset
    request = sh.browser_request(
        method="POST", path="/admin/auth/login", client=("10.0.0.197", 5555)
    )

    response = await auth_routes.login_submit(
        request=request,
        username=user.username,
        password="the old password",
        next="/admin/",
        session=registry,
    )

    assert response.status_code == 401
    assert user.is_active is True, "the account was never disabled — only reset"
    assert registry.sessions == [], "no row for a superseded credential"
    # The one commit is `last_login_at`, which precedes the mint. The mint's
    # own transaction rolled back rather than committing a row, and released
    # the guard doing it.
    assert registry.rolled_back == 1, "the guard must not be held on"
    assert "user_id" not in request.session
    assert SESSION_ID_KEY not in request.session


async def test_a_reset_racing_a_re_issue_leaves_the_browser_signed_out():
    """The same window on the password-change re-issue (D13).

    The re-issue runs in a *second* guarded transaction, so an administrator's
    reset can commit in the gap. The user is still active and the browser still
    holds a cookie, but the generation the handler committed is no longer the
    one on the row — so the mint refuses and this browser is signed out rather
    than re-issued against a version it does not hold.
    """
    user = sh.fake_user(7, session_version=1)
    sid, request, registry = await sh.sign_in(user)

    # The change's own transaction: bump, revoke, commit.
    user.session_version = 2
    assert await revoke_user_sessions(registry, user.id) == 1
    await registry.commit()
    committed_version = user.session_version

    # ...and an administrator's reset lands before the re-issue takes the guard.
    user.session_version = 3

    assert (
        await start_session(
            request,
            registry,
            user.id,
            expected_session_version=committed_version,
        )
        is None
    )
    assert [row for row in registry.sessions if row.revoked_at is None] == []

    signed_out = sh.browser_request(path="/admin/", session=dict(request.session))
    assert await get_active_session_user(signed_out, registry) is None
    assert hash_session_id(sid) == registry.sessions[0].id


def test_the_locked_re_read_repopulates_the_identity_map():
    """`populate_existing=True` on the mint's `SELECT … FOR UPDATE`.

    Asserted against the source because the effect is invisible to a fake: a
    locking re-read whose row is already in the session's identity map hands
    back the **pre-lock** attribute values, and `is_active` and
    `session_version` are both read in Python here. Every caller arrives with
    that row already loaded, so without this option the two checks above prove
    nothing at all.
    """
    import inspect

    source = inspect.getsource(auth_session.start_session)
    assert "with_for_update()" in source
    assert "execution_options(populate_existing=True)" in source
    # And the two checks that depend on it.
    assert "user.is_active is not True" in source
    assert "user.session_version != expected_session_version" in source


async def test_bootstrap_mints_outside_the_bootstrap_transaction(tmp_path, monkeypatch):
    """The two advisory keys are taken **sequentially, never nested**: the
    bootstrap transaction commits before the mint takes the account guard, so
    no path holds one while asking for the other."""
    from src.services import vault as vault_service

    monkeypatch.setattr(vault_service.settings, "vault_path", str(tmp_path))
    registry = sh.FakeRegistry()
    request = sh.browser_request(method="POST", path="/admin/register")

    response = await auth_routes.register_submit(
        request=request,
        username="rootadmin",
        password="correct horse battery staple",
        password_confirm="correct horse battery staple",
        vault_path=str(tmp_path),
        session=registry,
    )

    assert response.status_code == 302
    assert response.headers["location"] == "/admin/"
    bootstrap = registry.events.index(("lock", USER_BOOTSTRAP_LOCK_KEY))
    guard = registry.events.index(("lock", ACCOUNT_GUARD_LOCK_KEY))
    commits = [i for i, event in enumerate(registry.events) if event == ("commit",)]
    assert bootstrap < guard, "the bootstrap key comes first"
    assert any(bootstrap < c < guard for c in commits), (
        "the bootstrap transaction must have committed — and released its lock "
        "— before the mint takes the account guard"
    )
    assert len(registry.sessions) == 1
    assert request.session[SESSION_ID_KEY]


# --- validation -----------------------------------------------------------


async def test_a_cookie_with_no_session_id_is_refused_not_grandfathered(records):
    """A correctly signed pre-deploy cookie. Accepting it would keep the #198
    replay window open for another seven days after the fix shipped."""
    user = sh.fake_user(7)
    registry = sh.FakeRegistry(users=[user])
    request = sh.browser_request(session={"user_id": 7, "session_version": 1})

    assert await get_active_session_user(request, registry) is None
    assert request.session == {}
    assert [r.reason for r in _events(records, "panel_session_replay_refused")] == [
        "no_session_id"
    ]


async def test_an_unknown_row_is_refused(records):
    user = sh.fake_user(7)
    registry = sh.FakeRegistry(users=[user])
    request = sh.browser_request(session=sh.cookie_for(user, "never-minted"))

    assert await get_active_session_user(request, registry) is None
    assert request.session == {}
    assert [r.reason for r in _events(records, "panel_session_replay_refused")] == [
        "unknown_session"
    ]


async def test_an_expired_row_is_refused_while_the_signature_is_still_valid(records):
    user = sh.fake_user(7)
    sid = "expired-session-id"
    row = sh.session_row(
        7, sid=sid, expires_at=sh.utcnow() - datetime.timedelta(seconds=1)
    )
    registry = sh.FakeRegistry(users=[user], sessions=[row])
    request = sh.browser_request(session=sh.cookie_for(user, sid))

    assert await get_active_session_user(request, registry) is None
    assert request.session == {}
    assert [r.reason for r in _events(records, "panel_session_replay_refused")] == [
        "expired_session"
    ]


async def test_a_revoked_row_is_refused(records):
    user = sh.fake_user(7)
    sid = "revoked-session-id"
    row = sh.session_row(7, sid=sid, revoked_at=sh.utcnow())
    registry = sh.FakeRegistry(users=[user], sessions=[row])
    request = sh.browser_request(session=sh.cookie_for(user, sid))

    assert await get_active_session_user(request, registry) is None
    assert request.session == {}
    assert [r.reason for r in _events(records, "panel_session_replay_refused")] == [
        "revoked_session"
    ]


async def test_a_row_owned_by_another_user_is_refused_and_cleared(records):
    """D14. The two values are written together at the mint and can only
    disagree through tampering or a bug, so neither is preferred: the request
    authenticates nobody."""
    victim = sh.fake_user(7, username="victim")
    attacker = sh.fake_user(9, username="attacker")
    sid = "borrowed-session-id"
    row = sh.session_row(victim.id, sid=sid)
    registry = sh.FakeRegistry(users=[victim, attacker], sessions=[row])
    request = sh.browser_request(session=sh.cookie_for(attacker, sid))

    assert await get_active_session_user(request, registry) is None
    assert request.session == {}
    assert [r.reason for r in _events(records, "panel_session_replay_refused")] == [
        "user_mismatch"
    ]


async def test_an_inactive_user_is_refused_even_with_a_live_row(records):
    user = sh.fake_user(7)
    sid, request, registry = await sh.sign_in(user)
    user.is_active = False

    replay = sh.browser_request(path="/admin/", session=dict(request.session))
    assert await get_active_session_user(replay, registry) is None
    assert replay.session == {}
    assert registry.sessions[0].revoked_at is None, "validation revokes nothing"
    assert [r.reason for r in _events(records, "panel_session_replay_refused")] == [
        "user_inactive"
    ]


async def test_a_deleted_user_is_refused_and_recorded(records):
    user = sh.fake_user(7)
    _sid, request, registry = await sh.sign_in(user)
    registry.users.clear()  # the account was hard-deleted under the cookie

    replay = sh.browser_request(path="/admin/", session=dict(request.session))
    assert await get_active_session_user(replay, registry) is None
    assert replay.session == {}
    assert [r.reason for r in _events(records, "panel_session_replay_refused")] == [
        "user_missing"
    ]


async def test_a_stale_session_version_is_refused_and_recorded(records):
    """The account-wide invalidator, and the branch that had no record at all.

    A cookie whose row is still live but whose `session_version` is behind is
    exactly what an administrator's password reset leaves behind on a browser
    the registry write somehow missed — the second gate doing its job. It signs
    that browser out, so it has to say so.
    """
    user = sh.fake_user(7, session_version=3)
    _sid, request, registry = await sh.sign_in(user)
    user.session_version = 4  # the reset bumps it

    replay = sh.browser_request(path="/admin/", session=dict(request.session))
    assert await get_active_session_user(replay, registry) is None
    assert replay.session == {}
    assert [r.reason for r in _events(records, "panel_session_replay_refused")] == [
        "version_mismatch"
    ]


def test_every_refusal_branch_has_a_reason_and_every_reason_is_declared():
    """The vocabulary is closed, and the closure is checked both ways.

    A branch that clears the cookie without a record is a user signed out with
    nothing in the log to explain it; a reason declared but never emitted is a
    catalogue that has drifted from the code.
    """
    import inspect
    import re

    source = inspect.getsource(auth_session.get_active_session_user)
    emitted = set(re.findall(r'_replay_refused\(\s*request,\s*"([a-z_]+)"', source))
    emitted |= set(re.findall(r'_replay_refused\(\n?\s*request,\n?\s*"([a-z_]+)"', source))
    # The four resolved by the `reason = "..."` ladder above the shared call.
    emitted |= set(re.findall(r'reason = "([a-z_]+)"', source))
    # `user_missing` / `user_inactive` arrive through a conditional expression.
    emitted |= set(re.findall(r'"([a-z_]+)" if user is None else "([a-z_]+)"', source)[0]
                   if re.findall(r'"([a-z_]+)" if user is None else "([a-z_]+)"', source)
                   else [])
    assert emitted == auth_session.REPLAY_REFUSAL_REASONS, (
        f"declared but never emitted: "
        f"{sorted(auth_session.REPLAY_REFUSAL_REASONS - emitted)}; "
        f"emitted but not declared: {sorted(emitted - auth_session.REPLAY_REFUSAL_REASONS)}"
    )
    # And every `cookie.clear()` in the validator is preceded by a record.
    assert source.count("cookie.clear()") == source.count("_replay_refused(")


async def test_a_live_session_authenticates():
    user = sh.fake_user(7)
    _sid, request, registry = await sh.sign_in(user)
    replay = sh.browser_request(path="/admin/", session=dict(request.session))

    assert await get_active_session_user(replay, registry) is user
    assert replay.session["user_id"] == 7


async def test_a_second_session_survives_the_first_ones_logout():
    user = sh.fake_user(7)
    registry = sh.FakeRegistry(users=[user])
    _first_sid, first_request, _ = await sh.sign_in(user, registry=registry)
    first_cookie = dict(first_request.session)
    _second_sid, second_request, _ = await sh.sign_in(user, registry=registry)
    second_cookie = dict(second_request.session)

    await auth_routes.logout(
        sh.browser_request(method="POST", session=dict(first_cookie)), registry
    )

    assert (
        await get_active_session_user(
            sh.browser_request(session=dict(first_cookie)), registry
        )
        is None
    )
    assert (
        await get_active_session_user(
            sh.browser_request(session=dict(second_cookie)), registry
        )
        is user
    )


async def test_login_form_renders_for_a_revoked_cookie():
    """Without this the visitor bounces forever between the login page (which
    saw a user id) and the panel (which refused the session)."""
    user = sh.fake_user(7)
    sid, request, registry = await sh.sign_in(user)
    await revoke_session(registry, hash_session_id(sid))
    await registry.commit()

    form_request = sh.browser_request(
        path="/admin/auth/login", session=dict(request.session)
    )
    response = await auth_routes.login_form(form_request, "/admin/", registry)

    assert response.status_code == 200, "a redirect here is the login loop"
    # The identity is gone — a fresh CSRF nonce for the rendered form is not an
    # identity and is written after the refusal cleared the cookie.
    assert "user_id" not in form_request.session
    assert SESSION_ID_KEY not in form_request.session


# --- the touch ------------------------------------------------------------


async def test_the_touch_is_skipped_inside_the_interval():
    user = sh.fake_user(7)
    _sid, request, registry = await sh.sign_in(user)
    before = registry.sessions[0].last_seen_at
    commits = registry.committed

    replay = sh.browser_request(session=dict(request.session))
    assert await get_active_session_user(replay, registry) is user
    assert registry.sessions[0].last_seen_at == before
    assert registry.committed == commits, "no write, no commit"


async def test_a_stale_session_is_touched_and_committed_on_a_get():
    user = sh.fake_user(7)
    _sid, request, registry = await sh.sign_in(user)
    row = registry.sessions[0]
    row.last_seen_at = sh.utcnow() - datetime.timedelta(hours=1)
    commits = registry.committed

    replay = sh.browser_request(method="GET", session=dict(request.session))
    assert await get_active_session_user(replay, registry) is user

    assert row.last_seen_at > sh.utcnow() - datetime.timedelta(seconds=30)
    assert registry.committed == commits + 1


async def test_an_unsafe_method_does_not_touch():
    user = sh.fake_user(7)
    _sid, request, registry = await sh.sign_in(user)
    row = registry.sessions[0]
    stale = sh.utcnow() - datetime.timedelta(hours=1)
    row.last_seen_at = stale
    commits = registry.committed

    replay = sh.browser_request(method="POST", session=dict(request.session))
    assert await get_active_session_user(replay, registry) is user
    assert row.last_seen_at == stale
    assert registry.committed == commits


async def test_the_touch_takes_no_second_connection(monkeypatch):
    """A second `AsyncSession` would hold two pool leases for the request."""
    import src.database as database

    def _forbidden(*_args, **_kwargs):  # pragma: no cover - the assertion
        raise AssertionError("the touch opened a second AsyncSession")

    monkeypatch.setattr(database, "async_session", _forbidden)
    monkeypatch.setattr(auth_session, "get_session", _forbidden, raising=False)

    user = sh.fake_user(7)
    _sid, request, registry = await sh.sign_in(user)
    registry.sessions[0].last_seen_at = sh.utcnow() - datetime.timedelta(hours=1)

    replay = sh.browser_request(session=dict(request.session))
    assert await get_active_session_user(replay, registry) is user
    assert registry.statements, "the update went to the request's own session"


async def test_more_concurrent_stale_sessions_than_the_pool_holds_all_complete(
    monkeypatch,
):
    """The capacity property, expressed the only way a fake can express it.

    `pool_size=5, max_overflow=10` is fifteen leases. Each request here takes
    exactly one, and the test fails if any code path asks for a second — which
    is what a `touch` on its own session would have done. Twenty-five
    concurrent stale-session requests therefore complete against a pool of
    fifteen; with two leases each they would have needed fifty and the
    sixteenth caller in the process would have waited `pool_timeout` and 500ed.
    """
    import src.database as database

    def _forbidden(*_args, **_kwargs):  # pragma: no cover - the assertion
        raise AssertionError("a second pool lease was taken")

    monkeypatch.setattr(database, "async_session", _forbidden)

    capacity = 5 + 10
    leases = asyncio.Semaphore(capacity)
    user = sh.fake_user(7)
    minting = sh.FakeRegistry(users=[user])
    _sid, request, _ = await sh.sign_in(user, registry=minting)
    cookie = dict(request.session)

    async def one_request():
        async with leases:  # the request's single lease
            row = sh.session_row(
                user.id,
                sid=cookie[SESSION_ID_KEY],
                last_seen_at=sh.utcnow() - datetime.timedelta(hours=1),
            )
            registry = sh.FakeRegistry(users=[user], sessions=[row])
            return await get_active_session_user(
                sh.browser_request(session=dict(cookie)), registry
            )

    results = await asyncio.gather(*(one_request() for _ in range(25)))
    assert results == [user] * 25


def _touch_failures(records):
    return [r for r in records if r.getMessage() == "panel_session_touch_failed"]


async def test_a_raising_touch_still_serves_the_page(records):
    user = sh.fake_user(7)
    _sid, request, registry = await sh.sign_in(user)
    registry.sessions[0].last_seen_at = sh.utcnow() - datetime.timedelta(hours=1)
    registry.fail_on = "UPDATE user_sessions SET last_seen_at"
    registry.fail_with = RuntimeError("the last-seen write failed")

    replay = sh.browser_request(session=dict(request.session))
    assert await get_active_session_user(replay, registry) is user
    # **The savepoint absorbed it; the request's own transaction did not move.**
    # `Session.rollback()` expires every object loaded in the transaction, and
    # the object loaded in this one is the authenticated user just returned —
    # so rolling back to recover from a failed *telemetry* write handed the
    # panel an expired instance, and the next attribute read became lazy I/O on
    # an async session: `MissingGreenlet` instead of the page. A `GET` that
    # would have worked failed **because** the optional write failed.
    assert registry.savepoint_rollbacks == 1
    assert registry.rolled_back == 0, "the enclosing transaction must be untouched"

    # Through the emitter, not the bare logger (design D23): the touch
    # interval gates the *write*, and a failing write records no new
    # `last_seen_at` to throttle against, so a stale browser on `GET` drives
    # one of these per request forever.
    (failure,) = _touch_failures(records)
    assert failure.reason == "touch"
    assert failure.user_id == 7
    assert failure.error_type == "RuntimeError"
    # Class only. The statement binds `user_sessions.session_hash`, and
    # SQLAlchemy renders bound parameters into an error's text.
    assert "the last-seen write failed" not in _rendered(records)


async def test_a_failing_touch_leaves_the_user_usable_not_expired(records):
    """The property the savepoint exists for, stated as the caller sees it.

    The returned user must still answer for its columns after the touch has
    failed. Against a real `AsyncSession` an outer rollback would have expired
    it and the next read would raise `MissingGreenlet`; here the assertion is
    that the transaction was never rolled back at all, so nothing could have
    been expired.
    """
    user = sh.fake_user(7, username="alice", is_admin=True)
    _sid, request, registry = await sh.sign_in(user)
    registry.sessions[0].last_seen_at = sh.utcnow() - datetime.timedelta(hours=1)
    registry.fail_on = "UPDATE user_sessions SET last_seen_at"
    registry.fail_with = RuntimeError("the last-seen write failed")

    replay = sh.browser_request(session=dict(request.session))
    resolved = await get_active_session_user(replay, registry)

    assert resolved is user
    assert (resolved.id, resolved.username, resolved.is_admin) == (7, "alice", True)
    assert registry.rolled_back == 0
    assert registry.expunged == [], "nothing had to be detached; nothing expired"
    assert [r.reason for r in _touch_failures(records)] == ["touch"]


async def test_a_touch_whose_commit_fails_detaches_the_user_before_rolling_back(records):
    """The rarer half: the savepoint released and the *commit* then failed.

    A failed commit leaves the session unusable and its rollback expires what
    is loaded, savepoint or not — so the authenticated user is detached first.
    A detached instance keeps every column it has already loaded, which is what
    the panel reads; an expired one goes back to the database for them, and
    there is no database left to go to.
    """
    user = sh.fake_user(7, username="alice")
    _sid, request, registry = await sh.sign_in(user)
    registry.sessions[0].last_seen_at = sh.utcnow() - datetime.timedelta(hours=1)
    registry.fail_commit = RuntimeError("commit failed")
    registry.fail_rollback = RuntimeError("rollback failed")

    replay = sh.browser_request(session=dict(request.session))
    resolved = await get_active_session_user(replay, registry)

    assert resolved is user
    assert resolved.username == "alice"
    assert registry.savepoint_rollbacks == 0, "the write itself succeeded"
    assert registry.expunged == [user], "detached before the rollback, not after"

    # Both stages, named apart: a failing write with a working rollback is a
    # database refusing one statement; a failing rollback is a connection that
    # is gone. Same request, two different pages for the operator.
    stages = [r.reason for r in _touch_failures(records)]
    assert stages == ["touch", "rollback"]


class _ExpiresWhenTheConnectionDies:
    """A `user_sessions` row that behaves like an **expired** ORM instance.

    What SQLAlchemy does when a `commit()` fails and the `rollback()` after it
    also raises: it expires the instances it is holding *before* re-raising. An
    expired instance has no values — every mapped column read goes back to the
    database — and the database here is reachable only through the connection
    that just died. So the read raises.

    That is the whole shape of the round-3 finding: the failure *record*
    dereferenced this object, on the one path whose contract is that it cannot
    fail. Modelled rather than mocked, because a `MagicMock` answers `user_id`
    cheerfully and would prove the opposite of what this asserts.
    """

    #: The columns the lifecycle reads off a row.
    COLUMNS = ("id", "user_id", "created_at", "last_seen_at", "expires_at", "revoked_at")

    def __init__(self, **values):
        self.__dict__["_values"] = dict(values)
        self.__dict__["_expired"] = False
        self.__dict__["reads_after_expiry"] = 0

    def expire(self):
        self.__dict__["_expired"] = True

    def __getattr__(self, name):
        if name in self.COLUMNS:
            if self.__dict__["_expired"]:
                self.__dict__["reads_after_expiry"] += 1
                raise RuntimeError(
                    f"cannot refresh {name}: the connection this instance would "
                    "reload from is gone"
                )
            return self.__dict__["_values"].get(name)
        raise AttributeError(name)

    def __setattr__(self, name, value):
        if name in self.COLUMNS:
            self.__dict__["_values"][name] = value
        else:
            self.__dict__[name] = value


async def test_a_dead_connection_during_the_touch_still_serves_the_page(records):
    """Round 3. Both halves of the recovery fail, and the page is still served.

    `commit()` raises because the connection is gone; the `rollback()` that
    follows raises for the same reason, and — as a real session does — expires
    the row on its way out. Everything the two failure records need was read
    **before** the commit was attempted, so neither of them touches the expired
    instance and neither turns a served page into a 500.

    The row counts its own post-expiry reads, so this cannot pass by the
    expiry quietly never happening.
    """
    user = sh.fake_user(7, username="alice")
    sid, request, registry = await sh.sign_in(user)

    row = _ExpiresWhenTheConnectionDies(
        id=hash_session_id(sid),
        user_id=user.id,
        created_at=sh.utcnow() - datetime.timedelta(hours=2),
        last_seen_at=sh.utcnow() - datetime.timedelta(hours=1),
        expires_at=sh.utcnow() + datetime.timedelta(days=7),
        revoked_at=None,
    )
    registry.sessions = [row]
    registry.fail_commit = RuntimeError("the connection is gone")
    registry.fail_rollback = RuntimeError("and so is the recovery")
    registry.on_rollback = row.expire

    replay = sh.browser_request(
        method="GET", path="/admin/", session=dict(request.session)
    )

    # The panel GET, through the dependency chain a route actually sits on.
    # It must return the user, not raise.
    from src.control_panel.routes import require_user_panel

    resolved = await get_active_session_user(replay, registry)
    assert resolved is user
    assert replay.session["user_id"] == 7, "the cookie is intact; nothing was cleared"
    gated = await require_user_panel(request=replay, user=resolved, session=registry)
    assert gated is user, "the page still renders"

    # Both stages recorded, both carrying the id captured before the failure.
    failures = _touch_failures(records)
    assert [r.reason for r in failures] == ["touch", "rollback"]
    assert [r.user_id for r in failures] == [7, 7]
    assert [r.route for r in failures] == ["/admin/", "/admin/"]
    assert [r.error_type for r in failures] == ["RuntimeError", "RuntimeError"]

    # Non-vacuity, both directions: the instance really was expired, and
    # nothing read it afterwards.
    assert registry.rolled_back == 1
    assert row.reads_after_expiry == 0, (
        "the failure record dereferenced an expired instance — the one read "
        "that can turn this best-effort path into a 500"
    )
    with pytest.raises(RuntimeError):
        row.user_id  # noqa: B018 - proving the expiry is real


async def test_the_captured_ids_are_read_before_the_first_statement():
    """The narrower guarantee, stated where it cannot drift.

    A future edit that moves the capture below the `begin_nested` would pass
    every assertion above — the savepoint path does not expire anything — and
    reintroduce the defect on the path that does. So the order is asserted
    against the source.
    """
    import inspect

    source = inspect.getsource(auth_session.touch_session)
    capture = source.index("row_user_id = getattr(row, \"user_id\", None)")
    first_statement = source.index("session.begin_nested()")
    assert capture < first_statement, (
        "the primitives must be captured before anything that can fail"
    )
    # And no `_touch_failed` call reads off the row.
    assert "_touch_failed(request, row," not in source
    assert source.count("user_id=row_user_id") == 3


async def test_a_hammering_stale_browser_cannot_flood_the_sink():
    """The reason this had to leave the bare logger.

    Twenty-five `GET`s against a database that refuses the touch used to be
    twenty-five WARNING lines, bounded by nothing — the interval throttles the
    *write*, and a write that never lands never moves the timestamp the
    interval is measured against. Under the permit the same burst is capped
    and the withheld count is stated rather than silently dropped.

    Its own capture, with the **real** suppressor in the path: the `records`
    fixture forces the suppressor open, which is right for every test that
    asks *what* was recorded and wrong for the one that asks how many.
    """
    from src.services import security_events

    class _Capture(logging.Handler):
        def __init__(self):
            super().__init__(level=logging.DEBUG)
            self.records: list[logging.LogRecord] = []

        def emit(self, record):
            self.records.append(record)

    handler = _Capture()
    events = security_events.logger
    events.addHandler(handler)
    previous, propagate = events.level, events.propagate
    events.setLevel(logging.DEBUG)
    events.propagate = False
    security_events.reset_state()
    try:
        user = sh.fake_user(7)
        _sid, request, registry = await sh.sign_in(user)
        registry.sessions[0].last_seen_at = sh.utcnow() - datetime.timedelta(hours=1)
        registry.fail_on = "UPDATE user_sessions SET last_seen_at"
        registry.fail_with = RuntimeError("write failed")

        for _ in range(25):
            replay = sh.browser_request(session=dict(request.session))
            assert await get_active_session_user(replay, registry) is user

        emitted = _touch_failures(handler.records)
        assert len(emitted) == security_events.MAX_EVENTS_PER_WINDOW, (
            "the burst was not bounded"
        )

        security_events.flush_suppression_summaries()
        summaries = [
            r
            for r in handler.records
            if r.getMessage() == security_events.SUMMARY_EVENT
        ]
        assert summaries, "a withheld count must be stated, never silently dropped"
        assert summaries[-1].reason == "panel_session_touch_failed"
        assert summaries[-1].count == 25 - security_events.MAX_EVENTS_PER_WINDOW
    finally:
        events.removeHandler(handler)
        events.setLevel(previous)
        events.propagate = propagate
        security_events.reset_state()


async def test_touch_returns_false_on_an_unsafe_method_without_touching():
    user = sh.fake_user(7)
    _sid, _request, registry = await sh.sign_in(user)
    row = registry.sessions[0]
    row.last_seen_at = sh.utcnow() - datetime.timedelta(hours=1)

    assert (
        await touch_session(sh.browser_request(method="DELETE"), registry, row) is False
    )


# --- revocation -----------------------------------------------------------


async def test_the_revoke_helpers_never_commit():
    """They ride the caller's transaction: nothing may commit between the
    account guard being taken and the flags it protects being written."""
    user = sh.fake_user(7)
    registry = sh.FakeRegistry(users=[user])
    await sh.sign_in(user, registry=registry)
    await sh.sign_in(user, registry=registry)
    commits = registry.committed

    assert await revoke_user_sessions(registry, user.id) == 2
    assert await revoke_session(registry, registry.sessions[0].id) == 0
    assert registry.committed == commits, "a helper that commits breaks the guard"


async def test_a_second_revocation_keeps_the_original_time():
    user = sh.fake_user(7)
    registry = sh.FakeRegistry(users=[user])
    await sh.sign_in(user, registry=registry)

    assert await revoke_user_sessions(registry, user.id) == 1
    first = registry.sessions[0].revoked_at
    assert await revoke_user_sessions(registry, user.id) == 0
    assert registry.sessions[0].revoked_at == first


async def test_logout_revokes_only_the_presenting_session(records):
    user = sh.fake_user(7)
    registry = sh.FakeRegistry(users=[user])
    _one, first, _ = await sh.sign_in(user, registry=registry)
    _two, second, _ = await sh.sign_in(user, registry=registry)

    await auth_routes.logout(
        sh.browser_request(method="POST", session=dict(first.session)), registry
    )

    revoked = [row for row in registry.sessions if row.revoked_at is not None]
    assert len(revoked) == 1
    assert revoked[0].id == hash_session_id(first.session[SESSION_ID_KEY])
    assert second.session[SESSION_ID_KEY]
    counted = _events(records, "panel_sessions_revoked")
    assert [(r.reason, r.count) for r in counted] == [("logout", 1)]


async def test_a_failing_logout_revocation_still_clears_and_redirects(records):
    user = sh.fake_user(7)
    sid, request, registry = await sh.sign_in(user)
    registry.fail_on = "UPDATE user_sessions SET revoked_at"
    registry.fail_with = RuntimeError(
        f"could not write row id={hash_session_id(sid)}"
    )

    logout_request = sh.browser_request(method="POST", session=dict(request.session))
    response = await auth_routes.logout(logout_request, registry)

    assert response.status_code == 302
    assert response.headers["location"] == "/admin/auth/login"
    assert logout_request.session == {}
    failures = _events(records, "panel_session_revocation_failed")
    assert len(failures) == 1
    assert failures[0].error_type == "RuntimeError"
    assert failures[0].levelno == logging.ERROR
    # The class name and nothing else — the exception's *message* here carries
    # the stored session hash, which names a specific live session.
    _assert_absent(records, sid, hash_session_id(sid))


async def test_a_failing_logout_rollback_still_clears_and_redirects(records):
    user = sh.fake_user(7)
    _sid, request, registry = await sh.sign_in(user)
    registry.fail_on = "UPDATE user_sessions SET revoked_at"
    registry.fail_with = RuntimeError("write failed")
    registry.fail_rollback = RuntimeError("rollback failed")

    logout_request = sh.browser_request(method="POST", session=dict(request.session))
    response = await auth_routes.logout(logout_request, registry)

    assert response.status_code == 302
    assert logout_request.session == {}
    failures = _events(records, "panel_session_revocation_failed")
    assert failures and "RuntimeError" in failures[0].error_type


async def test_a_logout_with_no_session_id_still_redirects(records):
    """A pre-registry cookie reaching logout: nothing to revoke, no record
    asserting a revocation that did not happen."""
    registry = sh.FakeRegistry()
    request = sh.browser_request(method="POST", session={"user_id": 7})

    response = await auth_routes.logout(request, registry)

    assert response.status_code == 302
    assert request.session == {}
    assert _events(records, "panel_sessions_revoked") == []
    assert _events(records, "panel_logout")


# --- the secret-absence rule ---------------------------------------------


async def test_every_session_record_passes_the_catalogue_under_strict_mode(records):
    """The catalogue is checked at the call site, not only in the registry.

    Strict mode turns "this event does not declare that field" into a raise, so
    driving all three session events through it is what proves the emitters and
    `EVENT_FIELDS` agree — a mismatch in production only drops the field.
    """
    from src.services import security_events

    user = sh.fake_user(7)
    sid, request, registry = await sh.sign_in(user)
    cookie = dict(request.session)

    with security_events.strict_fields():
        await auth_routes.logout(
            sh.browser_request(method="POST", session=dict(cookie)), registry
        )
        await get_active_session_user(sh.browser_request(session=dict(cookie)), registry)

        failing_sid, failing_request, failing = await sh.sign_in(user)
        failing.fail_on = "UPDATE user_sessions SET revoked_at"
        failing.fail_with = RuntimeError("boom")
        await auth_routes.logout(
            sh.browser_request(method="POST", session=dict(failing_request.session)),
            failing,
        )

    assert _events(records, "panel_sessions_revoked")
    assert _events(records, "panel_session_replay_refused")
    assert _events(records, "panel_session_revocation_failed")
    _assert_absent(records, sid, failing_sid)


async def test_no_record_carries_the_identifier_or_its_stored_hash(records):
    """D19's rule, including the stored hash: it is `user_sessions.id`, so a
    record carrying it names one specific live session."""
    user = sh.fake_user(7)
    sid, request, registry = await sh.sign_in(user)
    cookie = dict(request.session)

    await auth_routes.logout(
        sh.browser_request(method="POST", session=dict(cookie)), registry
    )
    await get_active_session_user(sh.browser_request(session=dict(cookie)), registry)
    await get_active_session_user(
        sh.browser_request(session={"user_id": 7}), registry
    )

    assert records, "the refusals must have produced records to search"
    _assert_absent(records, sid, hash_session_id(sid))
    # What *is* permitted: the eight-hex `token_tag`, four characters short of
    # the fragment length the rule forbids.
    tags = {
        getattr(r, "token_tag", None)
        for r in _events(records, "panel_session_replay_refused")
    }
    assert "sha:" + hashlib.sha256(sid.encode()).hexdigest()[:8] in tags
