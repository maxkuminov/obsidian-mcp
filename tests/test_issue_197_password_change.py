"""#197 — a signed-in user can change their own password, safely.

The finding: every route that wrote `password_hash` outside bootstrap lived on
the **admin** router, so a non-administrator who suspected their password was
compromised had to ask an administrator — who then knew the replacement. The
page this module covers closes that, and almost everything here is about the
two ways a naive version of it would be worse than the gap:

* **The stale-read overwrite.** A handler that verifies the submitted current
  password against the `User` a FastAPI dependency loaded is authorised by a
  hash an administrator may already have replaced. Committing on that evidence
  restores access the administrator had just removed. So the change takes the
  **same** account guard the administrative handlers take, re-reads the row
  `FOR UPDATE` with `populate_existing=True`, and re-checks `is_active` before
  it verifies anything (D12). Two tests here commit an administrator's reset
  and an administrator's deactivation *in the window the guard exists to
  close*, and both must end in a refusal that wrote nothing and minted nothing.
* **The session that outlives the password.** A change that leaves the old
  cookie alive has not changed the credential (#198's finding, on this route).
  So the transaction that writes the hash also bumps `session_version` and
  revokes **every** session of that user — this browser's included — and only
  after the commit does a second, separately guarded transaction mint a fresh
  one (D13). Other devices out, this one still in, under a new identifier.

The database is `session_helpers.FakeRegistry`, which interprets the real
statements against in-memory rows, and every session in this module is minted
by the **production** `start_session`. The handler under test is the real
`change_password`, called with its slowapi decorators in place — which is why
the two throttles can be exercised by calling it, and why the limiter is reset
around every test.
"""
from __future__ import annotations

import datetime
import logging
import os

import pytest
from slowapi.errors import RateLimitExceeded

import session_helpers as sh
from src.auth import session as auth_session
from src.auth.passwords import MIN_PASSWORD_LENGTH, hash_password, verify_password
from src.auth.session import (
    SESSION_ID_KEY,
    get_active_session_user,
    hash_session_id,
)
from src.control_panel import routes as panel_routes
from src.control_panel.flash import ERR, FLASH_SESSION_KEY, OK
from src.limiter import limiter
from src.oauth.grants import ACCOUNT_GUARD_LOCK_KEY

UTC = datetime.timezone.utc

#: Both are comfortably over `MIN_PASSWORD_LENGTH`, so the length rule is never
#: the reason a test here refuses unless it says so.
OLD = "old correct horse battery"
NEW = "new correct horse battery"

TEMPLATES_DIR = os.path.join(os.path.dirname(panel_routes.__file__), "templates")


@pytest.fixture(autouse=True)
def _multi_user(monkeypatch):
    """Multi-user mode, and a clean throttle allowance for every test.

    The limiter's storage is process-wide and its window is a minute, so a
    module that calls a limited handler dozens of times has to reset it or the
    tests start depending on their own order.
    """
    monkeypatch.setattr(panel_routes.settings, "multi_user_mode", True)
    monkeypatch.setattr(auth_session.settings, "multi_user_mode", True)
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


def _events(records, name: str) -> list[logging.LogRecord]:
    return [r for r in records if r.getMessage() == name]


def _rendered(records) -> str:
    parts = []
    for record in records:
        parts.append(record.getMessage())
        parts.extend(str(value) for value in record.__dict__.values())
    return "\n".join(parts)


def account_user(user_id: int = 7, *, password: str = OLD, **kwargs):
    """A `users` row whose hash is a **real** one, so `verify_password` decides.

    `session_helpers.fake_user`'s default hash is deliberately malformed (it
    exists for the session lifecycle, which never verifies a password); here the
    verification is the thing under test.
    """
    return sh.fake_user(user_id, password_hash=hash_password(password), **kwargs)


def post(
    *,
    session: dict | None = None,
    ip: str = "203.0.113.9",
) -> "sh.Request":  # type: ignore[name-defined]
    return sh.browser_request(
        method="POST",
        path="/admin/account/password",
        session={} if session is None else session,
        client=(ip, 44444),
    )


async def change(
    request,
    registry,
    user,
    *,
    current: str = OLD,
    new: str = NEW,
    confirm: str | None = None,
):
    """Drive the real handler, decorators and all."""
    return await panel_routes.change_password(
        request=request,
        current_password=current,
        new_password=new,
        new_password_confirm=new if confirm is None else confirm,
        session=registry,
        user=user,
    )


def flashed(request) -> tuple[str | None, str | None]:
    raw = request.session.get(FLASH_SESSION_KEY)
    if not isinstance(raw, dict):
        return None, None
    return raw.get("message"), raw.get("kind")


# --- the happy path -------------------------------------------------------


async def test_a_change_succeeds_and_only_the_new_password_authenticates():
    user = account_user()
    _sid, request, registry = await sh.sign_in(user)

    response = await change(request, registry, user)

    assert response.status_code == 303
    assert verify_password(NEW, user.password_hash)
    assert not verify_password(OLD, user.password_hash)
    message, kind = flashed(request)
    assert kind == OK and message == panel_routes.SUCCESS_MESSAGE


async def test_the_account_wide_version_moves_too():
    """A cookie that somehow escapes the registry check still fails the version
    check — the two switches are deliberately independent (D2/D13)."""
    user = account_user(session_version=4)
    _sid, request, registry = await sh.sign_in(user)

    await change(request, registry, user)

    assert user.session_version == 5


async def test_the_change_takes_the_account_guard_before_it_reads_or_writes():
    user = account_user()
    _sid, request, registry = await sh.sign_in(user)
    registry.events.clear()
    registry.statements.clear()

    await change(request, registry, user)

    # The guard first, then the locked re-read.
    assert "FOR UPDATE" in registry.statements[1] and "users" in registry.statements[1]
    # And the whole shape in order: **one** commit inside the guarded critical
    # section — nothing commits between the lock and the write it protects —
    # and then a **second** guarded transaction for the re-issue, because the
    # lock is released by the first commit and an administrator can deactivate
    # in that gap (D13).
    assert registry.events == [
        ("lock", ACCOUNT_GUARD_LOCK_KEY),
        ("commit",),
        ("lock", ACCOUNT_GUARD_LOCK_KEY),
        ("commit",),
    ]


def test_the_re_read_forces_the_locked_values_over_the_loaded_ones():
    """`populate_existing=True` is load-bearing and invisible in the SQL.

    A `SELECT … FOR UPDATE` whose row is already in the session's identity map
    hands back the *loaded* object with its pre-lock attribute values, and this
    handler reads `is_active` and `password_hash` in Python. Every caller
    arrives with that row already loaded, so dropping this option would make
    the re-read prove nothing — while every behavioural test still passed,
    because a fake and a real session agree about everything except the
    identity map. It is therefore pinned as source.
    """
    import inspect

    source = inspect.getsource(panel_routes.change_password)
    assert "with_for_update()" in source
    assert "populate_existing=True" in source


# --- the races the guard exists to close ----------------------------------


async def test_a_concurrent_admin_reset_is_refused_and_not_overwritten(records):
    """An administrator's reset commits before this handler takes the lock."""
    user = account_user()
    _sid, request, registry = await sh.sign_in(user)
    administrators_hash = hash_password("the administrator chose this")

    def administrator_resets(_key):
        user.password_hash = administrators_hash

    registry.on_lock = administrator_resets
    committed = registry.committed
    rows = len(registry.sessions)

    response = await change(request, registry, user)

    assert response.status_code == 303
    # The administrator's hash stands; the change was verified against it and
    # refused, rather than overwriting it from a stale verification.
    assert user.password_hash == administrators_hash
    assert not verify_password(NEW, user.password_hash)
    assert registry.committed == committed, "nothing committed on a refusal"
    assert len(registry.sessions) == rows, "no session was minted"
    assert [r.revoked_at for r in registry.sessions] == [None] * rows
    refusals = _events(records, "panel_password_change_refused")
    assert [r.reason for r in refusals] == ["wrong_current_password"]


async def test_a_concurrent_deactivation_is_refused_and_writes_nothing(records):
    user = account_user()
    _sid, request, registry = await sh.sign_in(user)
    original = user.password_hash

    def administrator_deactivates(_key):
        user.is_active = False

    registry.on_lock = administrator_deactivates
    committed = registry.committed
    rows = len(registry.sessions)

    response = await change(request, registry, user)

    assert response.status_code == 303
    assert user.password_hash == original and user.is_active is False
    assert registry.committed == committed
    assert len(registry.sessions) == rows, "no session was minted"
    # The acting browser is signed out: the row it holds is for an account that
    # is no longer active, and the cookie is emptied rather than left to fail
    # one request later.
    assert "user_id" not in request.session
    assert SESSION_ID_KEY not in request.session
    assert flashed(request) == (panel_routes.INACTIVE_REFUSAL, ERR)
    assert [r.reason for r in _events(records, "panel_password_change_refused")] == [
        "account_inactive"
    ]


async def test_a_deleted_account_cannot_change_its_password(records):
    user = account_user()
    _sid, request, registry = await sh.sign_in(user)

    def administrator_deletes(_key):
        registry.users.clear()

    registry.on_lock = administrator_deletes
    committed = registry.committed

    response = await change(request, registry, user)

    assert response.status_code == 303
    assert registry.committed == committed
    assert registry.rolled_back >= 1
    assert "user_id" not in request.session
    assert [r.reason for r in _events(records, "panel_password_change_refused")] == [
        "account_inactive"
    ]


# --- refusals -------------------------------------------------------------


async def test_a_wrong_current_password_leaves_everything_untouched(records):
    user = account_user()
    _sid, request, registry = await sh.sign_in(user)
    original_hash = user.password_hash
    original_version = user.session_version
    committed = registry.committed

    response = await change(request, registry, user, current="not the password")

    assert response.status_code == 303
    assert user.password_hash == original_hash
    assert user.session_version == original_version
    assert [r.revoked_at for r in registry.sessions] == [None]
    assert registry.committed == committed
    assert flashed(request) == (panel_routes.CREDENTIAL_REFUSAL, ERR)
    assert [r.reason for r in _events(records, "panel_password_change_refused")] == [
        "wrong_current_password"
    ]
    # The acting session is *not* ended by a wrong guess: that would hand an
    # attacker with a stolen cookie a way to log the real user out.
    assert request.session[SESSION_ID_KEY]


@pytest.mark.parametrize(
    ("new", "confirm", "reason"),
    [
        (NEW, NEW + "-typo", "mismatch"),
        ("short", "short", "too_short"),
        ("has a \x00 in it and is long", "has a \x00 in it and is long", "nul_byte"),
        (OLD, OLD, "same_as_current"),
    ],
    ids=["mismatch", "too_short", "nul_byte", "same_as_current"],
)
async def test_each_refusal_writes_nothing_and_flashes(records, new, confirm, reason):
    """The four refusals that are about the *new* password.

    `nul_byte` is the one that used to be a 500: `hash_password` raises on an
    embedded NUL (passlib's policy, preserved deliberately), and four handlers
    passed form input straight into it. It is a form error here.
    """
    user = account_user()
    _sid, request, registry = await sh.sign_in(user)
    original_hash = user.password_hash
    original_version = user.session_version
    committed = registry.committed

    response = await change(request, registry, user, new=new, confirm=confirm)

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/account"
    assert user.password_hash == original_hash
    assert user.session_version == original_version
    assert [r.revoked_at for r in registry.sessions] == [None]
    assert registry.committed == committed
    message, kind = flashed(request)
    assert kind == ERR and message
    assert [r.reason for r in _events(records, "panel_password_change_refused")] == [
        reason
    ]


async def test_the_two_credential_refusals_are_one_message():
    """A message that says *which* credential check failed says something about
    the stored password. Both branches answer with the same sentence; the
    `reason` that separates them exists only in the record."""
    wrong_user = account_user(1)
    _s1, wrong_request, wrong_registry = await sh.sign_in(wrong_user)
    await change(wrong_request, wrong_registry, wrong_user, current="nope, wrong")

    reuse_user = account_user(2)
    _s2, reuse_request, reuse_registry = await sh.sign_in(reuse_user)
    await change(reuse_request, reuse_registry, reuse_user, new=OLD)

    assert flashed(wrong_request)[0] == flashed(reuse_request)[0]
    assert flashed(wrong_request)[0] == panel_routes.CREDENTIAL_REFUSAL


async def test_no_outcome_puts_a_message_in_the_url():
    """#138: the message rides the session, and the redirect target is bare —
    a message a link can carry is a message an attacker chose."""
    user = account_user()
    _sid, request, registry = await sh.sign_in(user)

    refused = await change(request, registry, user, current="wrong one")
    assert refused.headers["location"] == "/admin/account"

    succeeded = await change(request, registry, user)
    assert succeeded.headers["location"] == "/admin/account"
    assert "?" not in succeeded.headers["location"]
    assert flashed(request)[0] == panel_routes.SUCCESS_MESSAGE


# --- the effect on sessions -----------------------------------------------


async def test_the_other_browser_is_signed_out_and_this_one_is_not():
    user = account_user()
    registry = sh.FakeRegistry(users=[user])
    _first_sid, first, _ = await sh.sign_in(user, registry=registry)
    _second_sid, second, _ = await sh.sign_in(user, registry=registry)
    second_cookie = dict(second.session)

    await change(first, registry, user)

    # The second browser's next request resolves nothing.
    replay = sh.browser_request(path="/admin/", session=dict(second_cookie))
    assert await get_active_session_user(replay, registry) is None
    assert replay.session == {}

    # The one that made the change is still signed in — under a **new**
    # identifier, because the cookie that was live while the old password was
    # live must not survive the change either.
    still_in = sh.browser_request(path="/admin/", session=dict(first.session))
    assert await get_active_session_user(still_in, registry) is user


async def test_the_pre_change_cookie_of_the_changing_browser_is_dead_too():
    user = account_user()
    sid, request, registry = await sh.sign_in(user)
    before = dict(request.session)

    await change(request, registry, user)

    assert request.session[SESSION_ID_KEY] != sid, "the identifier rotates"
    replay = sh.browser_request(path="/admin/", session=dict(before))
    assert await get_active_session_user(replay, registry) is None


async def test_every_row_of_that_user_is_revoked_including_this_one(records):
    user = account_user()
    registry = sh.FakeRegistry(users=[user])
    _a, first, _ = await sh.sign_in(user, registry=registry)
    await sh.sign_in(user, registry=registry)
    await sh.sign_in(user, registry=registry)

    await change(first, registry, user)

    minted_after = [row for row in registry.sessions if row.revoked_at is None]
    assert len(minted_after) == 1, "only the re-issued session survives"
    assert minted_after[0].id == hash_session_id(first.session[SESSION_ID_KEY])
    counted = _events(records, "panel_sessions_revoked")
    assert [(r.reason, r.count) for r in counted] == [("password_change", 3)]


async def test_a_failing_write_revokes_nothing(records):
    """The revocation and the hash ride one transaction: a failure in it leaves
    the stored hash alone and no row revoked."""
    user = account_user()
    _sid, request, registry = await sh.sign_in(user)
    registry.fail_on = "UPDATE user_sessions SET revoked_at"
    registry.fail_with = RuntimeError("the write failed")
    committed = registry.committed

    with pytest.raises(RuntimeError):
        await change(request, registry, user)

    assert registry.committed == committed, "nothing was committed"
    assert [r.revoked_at for r in registry.sessions] == [None]
    assert _events(records, "panel_password_changed") == []


async def test_a_deactivation_in_the_gap_signs_the_browser_out_without_undoing_it():
    """The guard is released between the change and the re-issue, and an
    administrator can deactivate in exactly that gap (D13). The password change
    is the durable half; the session is the recoverable one."""
    user = account_user()
    _sid, request, registry = await sh.sign_in(user)

    locks: list[int] = []

    def deactivate_between_the_transactions(key):
        locks.append(key)
        if len(locks) == 2:  # the re-issue's guard, not the change's
            user.is_active = False

    registry.on_lock = deactivate_between_the_transactions

    response = await change(request, registry, user)

    assert response.status_code == 303
    assert verify_password(NEW, user.password_hash), "the change is not rolled back"
    assert [row for row in registry.sessions if row.revoked_at is None] == []
    assert "user_id" not in request.session and SESSION_ID_KEY not in request.session


async def test_a_raising_re_issue_leaves_the_new_password_in_force(records, monkeypatch):
    user = account_user()
    _sid, request, registry = await sh.sign_in(user)

    async def exploding_mint(*_args, **_kwargs):
        raise RuntimeError(f"could not insert row for {hash_session_id('x')}")

    monkeypatch.setattr(panel_routes, "start_session", exploding_mint)

    response = await change(request, registry, user)

    assert response.status_code == 303
    assert verify_password(NEW, user.password_hash)
    assert "user_id" not in request.session
    assert _events(records, "panel_password_changed"), "the change did happen"
    # Recorded through the catalogue, not a bare `logger.error`: a caller drives
    # this path, so the record passes the same allowance check as every other
    # caller-triggerable one.
    failures = _events(records, "panel_session_reissue_failed")
    assert len(failures) == 1
    assert failures[0].levelno == logging.ERROR
    assert failures[0].error_type == "RuntimeError"
    assert failures[0].reason == "password_change"
    # The class name only: a SQLAlchemy error renders its bound parameters, one
    # of which on this path is a stored session hash.
    assert hash_session_id("x") not in _rendered(records)


# --- the two throttles ----------------------------------------------------


async def test_the_account_keyed_limit_bounds_guessing_across_addresses():
    """An address-only key hands an attacker a fresh allowance per address, so
    the account-keyed limit is what bounds guessing against **one** account."""
    user = account_user()

    for index in range(5):
        registry = sh.FakeRegistry(users=[user])
        request = post(session={"user_id": user.id}, ip=f"198.51.100.{index}")
        # A validation refusal: it never reaches bcrypt, and it counts against
        # the allowance exactly as a real attempt does.
        assert (await change(request, registry, user, new="short")).status_code == 303

    with pytest.raises(RateLimitExceeded):
        await change(
            post(session={"user_id": user.id}, ip="198.51.100.200"),
            sh.FakeRegistry(users=[user]),
            user,
            new="short",
        )


async def test_the_address_keyed_limit_bounds_walking_many_accounts():
    """An account-only key lets one address walk many accounts; neither key
    subsumes the other, which is why both decorators are there."""
    for user_id in range(1, 6):
        user = account_user(user_id)
        request = post(session={"user_id": user_id}, ip="198.51.100.77")
        assert (
            await change(request, sh.FakeRegistry(users=[user]), user, new="short")
        ).status_code == 303

    sixth = account_user(6)
    with pytest.raises(RateLimitExceeded):
        await change(
            post(session={"user_id": 6}, ip="198.51.100.77"),
            sh.FakeRegistry(users=[sixth]),
            sixth,
            new="short",
        )


async def test_a_success_counts_against_the_same_allowance():
    """Otherwise the allowance could be drained — or dodged — by mixing
    successful changes into a run of guesses.

    Each attempt is its own `Request`, as it is in production: slowapi marks a
    request it has already checked and skips it on a second pass, so reusing
    one object would count six attempts as one and the test would pass without
    the limit doing anything.
    """
    user = account_user()
    _sid, first, registry = await sh.sign_in(user)
    cookie = dict(first.session)

    # 1: a success.
    assert (await change(post(session=dict(cookie)), registry, user)).status_code == 303
    for _ in range(4):  # 2-5: refusals, which cost the same allowance.
        assert (
            await change(post(session=dict(cookie)), registry, user, new="short")
        ).status_code == 303

    with pytest.raises(RateLimitExceeded):
        await change(post(session=dict(cookie)), registry, user, new="short")


# --- the app-level surface ------------------------------------------------


def _panel_app(user, registry, *, csrf: bool = True):
    """The panel router on a real app: session middleware, the limiter, and the
    application-wide rate-limit handler — the pieces the 429 and the CSRF
    rejection are properties of."""
    from fastapi import FastAPI
    from starlette.middleware.sessions import SessionMiddleware

    from src.csrf import verify_csrf
    from src.database import get_session
    from src.main import _rate_limit_handler

    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="k" * 32)
    app.include_router(panel_routes.router)
    app.dependency_overrides[get_session] = lambda: registry
    app.dependency_overrides[panel_routes.require_user_panel] = lambda: user
    if not csrf:
        app.dependency_overrides[verify_csrf] = lambda: None
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_handler)
    return app


def test_the_sixth_attempt_answers_with_the_applications_rate_limit_response():
    """D25: a rate-limited request never reaches the handler, and slowapi's own
    JSON 429 stands — it is the one refusal deliberately exempt from the
    flash-and-303 rule, because the alternative is a second counter inside the
    handler that diverges from the limiter that already decided."""
    from fastapi.testclient import TestClient

    user = account_user()
    registry = sh.FakeRegistry(users=[user])
    client = TestClient(_panel_app(user, registry, csrf=False))
    form = {
        "current_password": OLD,
        "new_password": "short",
        "new_password_confirm": "short",
    }

    for _ in range(5):
        assert client.post(
            "/admin/account/password", data=form, follow_redirects=False
        ).status_code == 303

    limited = client.post("/admin/account/password", data=form, follow_redirects=False)

    assert limited.status_code == 429
    assert "location" not in {k.lower() for k in limited.headers}
    assert registry.committed == 0
    assert verify_password(OLD, user.password_hash), "nothing was written"


def test_a_post_without_a_csrf_token_is_rejected():
    from fastapi.testclient import TestClient

    user = account_user()
    registry = sh.FakeRegistry(users=[user])
    client = TestClient(_panel_app(user, registry, csrf=True))

    response = client.post(
        "/admin/account/password",
        data={
            "current_password": OLD,
            "new_password": NEW,
            "new_password_confirm": NEW,
        },
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert registry.committed == 0
    assert verify_password(OLD, user.password_hash)


# --- the page and its gating ----------------------------------------------


async def test_a_non_admin_reaches_the_account_page():
    """`require_user_panel`, not `require_admin_panel` — the whole point."""
    user = account_user(is_admin=False)
    _sid, request, _registry = await sh.sign_in(user)
    page = sh.browser_request(path="/admin/account", session=dict(request.session))

    response = await panel_routes.account_page(page, user)

    body = response.body.decode()
    assert response.status_code == 200
    assert 'action="/admin/account/password"' in body
    assert 'name="current_password"' in body
    assert 'name="new_password_confirm"' in body
    assert str(MIN_PASSWORD_LENGTH) in body
    # The page says what the change does to the user's other browsers.
    assert "signs you out of every other browser" in body


async def test_both_the_page_and_the_handler_are_absent_in_single_user_mode(
    monkeypatch,
):
    """D23: there is no account row and no local password there, so a page
    whose only content is a form that cannot exist is not a page — and one rule
    for both methods is one thing to verify."""
    from fastapi import HTTPException

    monkeypatch.setattr(panel_routes.settings, "multi_user_mode", False)
    user = account_user()
    registry = sh.FakeRegistry(users=[user])

    with pytest.raises(HTTPException) as page:
        await panel_routes.account_page(sh.browser_request(path="/admin/account"), user)
    assert page.value.status_code == 404

    with pytest.raises(HTTPException) as handler:
        await change(post(session={"user_id": user.id}), registry, user)
    assert handler.value.status_code == 404
    assert registry.committed == 0
    assert verify_password(OLD, user.password_hash)


def _render_base(**overrides) -> str:
    from starlette.templating import Jinja2Templates

    context = {
        "active": "dashboard",
        "is_admin": False,
        "multi_user_mode": True,
        "username": "alice",
        "csrf_token": "test-csrf-token",
    }
    context.update(overrides)
    templates = Jinja2Templates(directory=TEMPLATES_DIR)
    return templates.TemplateResponse(
        request=None, name="base.html", context=context
    ).body.decode()


def test_the_sidebar_entry_is_gated_on_multi_user_mode_like_the_users_link():
    assert '/admin/account' in _render_base()
    assert '/admin/account' not in _render_base(multi_user_mode=False)


def test_the_sidebar_entry_is_not_admin_only():
    """Every signed-in user has an account; only administrators have Users."""
    body = _render_base(is_admin=False)
    assert '/admin/account' in body
    assert '/admin/users/' not in body


def test_the_sidebar_entry_marks_itself_active():
    import re

    def account_classes(body: str) -> str:
        match = re.search(r'<a href="/admin/account" class="nav-item([^"]*)"', body)
        assert match, "the Account entry is not in the sidebar at all"
        return match.group(1)

    assert "active" in account_classes(_render_base(active="account"))
    assert "active" not in account_classes(_render_base(active="keys"))


# --- the catalogue and the secret-absence rule ----------------------------


async def test_every_password_event_passes_the_catalogue_under_strict_mode(
    records, monkeypatch
):
    """Strict mode turns "this event does not declare that field" into a raise,
    so driving every event this handler emits through it proves the emitters and
    `EVENT_FIELDS` agree — a mismatch in production only drops the field
    silently."""
    from src.services import security_events

    user = account_user()
    _sid, request, registry = await sh.sign_in(user)

    with security_events.strict_fields():
        await change(request, registry, user, current="wrong one")
        await change(request, registry, user)

        failing_user = account_user(8)
        _s, failing_request, failing = await sh.sign_in(failing_user)

        async def exploding_mint(*_args, **_kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(panel_routes, "start_session", exploding_mint)
        await change(failing_request, failing, failing_user)

    assert _events(records, "panel_password_change_refused")
    assert _events(records, "panel_password_changed")
    assert _events(records, "panel_sessions_revoked")
    assert _events(records, "panel_session_reissue_failed")


async def test_no_record_carries_a_password_a_hash_or_a_session_identifier(records):
    """D19's rule. The account these records name is the one whose credential
    just moved, so a record leaking any part of it would be the worst line in
    the file."""
    user = account_user()
    sid, request, registry = await sh.sign_in(user)
    old_hash = user.password_hash

    await change(request, registry, user, current="wrong one")
    await change(request, registry, user)
    new_sid = request.session[SESSION_ID_KEY]

    assert records, "the calls must have produced records to search"
    text = _rendered(records)
    for secret in (OLD, NEW, old_hash, user.password_hash, sid, new_sid):
        assert secret not in text
        assert secret[:12] not in text, "not even a 12-character fragment"
        assert hash_session_id(secret) not in text
