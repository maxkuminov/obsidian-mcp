"""#191 — the panel's authentication decisions leave a record.

`src/auth/routes.py` contained no logger at all. A successful sign-in, a wrong
password, an unknown username, a deactivated account, a logout and the
first-administrator bootstrap were indistinguishable in the log, because none of
them was in the log. An operator watching a credential-stuffing burst saw
nothing.

Three properties are asserted here and they pull against each other, which is
why they are asserted together:

* **The response must not change.** The three login failures are one
  `_render_login(..., 401)`, byte-identical, so splitting the condition for a
  reason code leaks nothing to the caller.
* **Exactly one emission attempt per outcome**, with the reason that
  distinguishes it — counted at `acquire`, with the suppressor forced open, so
  "one attempt" means one and not "one that happened to survive the flood
  bound".
* **A success record follows its commit** (D17). A commit that raises must
  leave no line claiming a sign-in, or an administrator, that does not exist.
"""
import itertools
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.requests import Request

import session_helpers as sh
from src.auth import routes as auth_routes
from src.auth.passwords import hash_password
from src.limiter import limiter
from src.services import security_events
from src.services import vault as vault_service

PASSWORD = "correct horse battery staple"
USERNAME = "routeadmin"

# `login_submit` is wrapped in slowapi's `@limiter.limit("5/minute")`, which
# insists on a real `Request` and buckets by client address in a process-wide
# store. A fresh address per request keeps these tests independent of each other
# and of collection order.
_client_ips = itertools.count(1)


@pytest.fixture(autouse=True)
def _clean_rate_limiter():
    limiter.reset()
    yield
    limiter.reset()


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


class _Events:
    """Captured records plus the `acquire` count that produced them.

    The count is the assertion that matters for "exactly one emission attempt":
    with the suppressor forced open every attempt becomes a record, so a
    duplicate would show up as two — but a call site that acquired a permit and
    then dropped it would not, and that is a spent allowance either way.
    """

    def __init__(self, records, acquires):
        self.records = records
        self.acquires = acquires

    def named(self, event: str) -> list[logging.LogRecord]:
        return [r for r in self.records if r.getMessage() == event]

    def one(self, event: str) -> logging.LogRecord:
        (record,) = self.named(event)
        return record


@pytest.fixture
def events(monkeypatch):
    handler = _Capture()
    logger = security_events.logger
    logger.addHandler(handler)
    propagate, level = logger.propagate, logger.level
    logger.propagate = False
    # INFO is half the catalogue; the root's default WARNING would filter it.
    logger.setLevel(logging.DEBUG)

    acquires: list[tuple] = []
    real_acquire = security_events.acquire

    def counting_acquire(event, subject=None, **kwargs):
        acquires.append((event, subject))
        return real_acquire(event, subject, **kwargs)

    monkeypatch.setattr(security_events, "acquire", counting_acquire)
    security_events.reset_state()
    try:
        with security_events.suppression_disabled():
            yield _Events(handler.records, acquires)
    finally:
        logger.removeHandler(handler)
        logger.propagate = propagate
        logger.setLevel(level)
        security_events.reset_state()


@pytest.fixture(autouse=True)
def _constant_csrf(monkeypatch):
    """One constant CSRF token, so "byte-identical" is a claim about the page.

    The real token is a *timestamped* signature over a per-session nonce, so two
    renderings differ even for one unchanged page. Pinning it isolates the thing
    under test: whether the reason code changed what the caller sees.
    """
    monkeypatch.setattr(auth_routes, "generate_csrf_token", lambda request: "csrf")


def _make_request(path: str, session: dict | None = None) -> Request:
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
            "session": {} if session is None else session,
            "state": {},
        }
    )


def _session_returning(user, commit_error: Exception | None = None):
    result = MagicMock()
    result.scalar_one_or_none.return_value = user
    session = AsyncMock()
    session.execute.return_value = result
    # `add` is synchronous on a real `AsyncSession`, and `start_session` calls
    # it to insert the session row. Left as an `AsyncMock` attribute it returns
    # a coroutine nobody awaits, which is a `RuntimeWarning` the suite runs
    # under `-W error` to keep out.
    session.add = MagicMock()
    if commit_error is not None:
        session.commit.side_effect = commit_error
    return session


def _empty_users_session(commit_error: Exception | None = None):
    result = MagicMock()
    result.scalar.return_value = 0
    result.scalar_one_or_none.return_value = None
    session = AsyncMock()
    session.execute.return_value = result
    session.add = MagicMock()
    if commit_error is not None:
        session.commit.side_effect = commit_error
    return session


def _stored_user(**overrides):
    fields = {
        "id": 1,
        "username": USERNAME,
        "password_hash": hash_password(PASSWORD),
        "is_active": True,
        "is_admin": True,
        "session_version": 1,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


async def _login(user, password=PASSWORD, username=USERNAME, commit_error=None):
    request = _make_request("/admin/auth/login")
    session = _session_returning(user, commit_error=commit_error)
    response = await auth_routes.login_submit(
        request=request,
        username=username,
        password=password,
        next="/admin/",
        session=session,
    )
    return request, response


# --- login failures: three reasons, one response -------------------------


async def test_the_three_login_failures_are_byte_identical(events):
    """The reason exists in the log and nowhere else.

    Splitting the merged condition is what makes the reason code possible; it is
    also what would leak which usernames exist if any of the three returned a
    different page. So the three bodies are compared directly.
    """
    _, unknown = await _login(None, username="nobody")
    _, inactive = await _login(_stored_user(is_active=False))
    _, wrong = await _login(_stored_user(), password=PASSWORD + "!")

    for response in (unknown, inactive, wrong):
        assert response.status_code == 401

    # Same username submitted, so the rendered form is the same form.
    _, unknown_same = await _login(None)
    assert unknown_same.body == inactive.body == wrong.body
    assert (
        unknown_same.headers["content-type"]
        == inactive.headers["content-type"]
        == wrong.headers["content-type"]
    )

    reasons = [r.reason for r in events.named("panel_login_failed")]
    assert reasons == ["unknown_user", "inactive_user", "bad_password", "unknown_user"]


@pytest.mark.parametrize(
    "user_factory, password, reason, resolves",
    [
        (lambda: None, PASSWORD, "unknown_user", False),
        (lambda: _stored_user(is_active=False), PASSWORD, "inactive_user", True),
        (lambda: _stored_user(), PASSWORD + "!", "bad_password", True),
    ],
)
async def test_one_attempt_per_failure_with_its_reason(
    events, user_factory, password, reason, resolves
):
    await _login(user_factory(), password=password)

    assert events.acquires == [
        ("panel_login_failed", events.acquires[0][1])
    ], "exactly one emission attempt for one decision"
    record = events.one("panel_login_failed")
    assert record.levelno == logging.WARNING
    assert record.reason == reason
    # The submitted username is caller-supplied and may appear only under the
    # `_submitted` name, whatever the outcome (D15).
    assert record.username_submitted == USERNAME
    assert not hasattr(record, "username")
    # `user_id` is unsuffixed, so it may hold only a value read from a row.
    assert hasattr(record, "user_id") is resolves
    assert record.client_ip.startswith("10.0.0.")
    assert record.route == "/admin/auth/login"


async def test_the_failure_subject_is_the_address_not_the_guessed_account(events):
    """A guessed *valid* username must not mint its own flood allowance.

    Keying suppression on the resolved row would give an attacker one fresh
    bucket per real account they can name, which is precisely the "caller can
    mint subjects" hole the suppressor exists to close.
    """
    await _login(_stored_user(), password=PASSWORD + "!")
    (_event, subject) = events.acquires[0]
    assert subject.startswith("ip:")


# --- login success: after the commit, never before -----------------------


async def test_a_successful_login_is_recorded_once_after_its_commit(events):
    user = _stored_user()
    request, response = await _login(user)

    assert response.status_code == 302
    assert request.session["user_id"] == 1
    assert events.acquires == [("panel_login_succeeded", "user:1")]
    record = events.one("panel_login_succeeded")
    assert record.levelno == logging.INFO
    assert record.user_id == 1
    assert record.username == USERNAME
    assert record.route == "/admin/auth/login"
    assert not hasattr(record, "username_submitted")


async def test_a_failed_last_login_commit_leaves_no_success_record(events):
    """D17. A record asserting a sign-in that the transaction did not keep is
    worse than no record: an operator would read it as fact."""
    with pytest.raises(RuntimeError):
        await _login(_stored_user(), commit_error=RuntimeError("commit failed"))

    assert events.named("panel_login_succeeded") == []
    assert events.acquires == []


async def test_a_refused_mint_leaves_no_success_record(events, monkeypatch):
    """D17 again, one step later: the mint is the durable half of a sign-in.

    The `last_login_at` commit is not what makes somebody signed in — the
    session row is. An administrator's reset committing between the password
    check and the mint's guard makes `start_session` refuse, and a
    `panel_login_succeeded` written before it would assert a sign-in that never
    happened. The attempt is still recorded, as a failure with its own reason,
    so a correct credential that did not sign in is never silently absent.
    """
    monkeypatch.setattr(auth_routes, "verify_password", lambda *_a, **_k: True)
    user = sh.fake_user(1, username=USERNAME, session_version=3)
    registry = sh.FakeRegistry(users=[user])
    registry.on_lock = lambda _key: setattr(user, "session_version", 4)
    request = sh.browser_request(
        method="POST", path="/admin/auth/login", client=(f"10.0.0.{next(_client_ips) % 250 + 1}", 5555)
    )

    response = await auth_routes.login_submit(
        request=request,
        username=USERNAME,
        password=PASSWORD,
        next="/admin/",
        session=registry,
    )

    assert response.status_code == 401
    assert events.named("panel_login_succeeded") == []
    record = events.one("panel_login_failed")
    assert record.reason == "session_mint_refused"
    assert record.user_id == 1
    assert record.levelno == logging.WARNING
    # The subject is the address, as on every other login failure: keying on
    # the resolved row would hand an attacker one fresh allowance per account.
    assert events.acquires == [("panel_login_failed", events.acquires[0][1])]
    assert events.acquires[0][1] != "user:1", "the subject is the address, not the row"


async def test_the_password_is_never_in_any_login_record(events):
    await _login(_stored_user(), password=PASSWORD + "!")
    await _login(_stored_user())

    for record in events.records:
        rendered = record.getMessage() + " ".join(
            str(value) for value in record.__dict__.values()
        )
        assert PASSWORD not in rendered
        assert (PASSWORD + "!") not in rendered


# --- logout ---------------------------------------------------------------


async def test_logout_records_the_session_values_before_clearing_them(events):
    session = {"user_id": 9, "username": "someone", "is_admin": True}
    request = _make_request("/admin/auth/logout", session=session)

    response = await auth_routes.logout(request)

    assert response.status_code == 302
    assert request.session == {}
    assert events.acquires == [("panel_logout", "user:9")]
    record = events.one("panel_logout")
    assert record.levelno == logging.INFO
    # `_session` is the provenance: copied from the cookie, never re-read, so it
    # may name an account that has since been renamed or deleted.
    assert record.user_id_session == 9
    assert record.username_session == "someone"
    assert not hasattr(record, "user_id")
    assert not hasattr(record, "username")


async def test_logout_without_a_session_still_records_one_event(events):
    request = _make_request("/admin/auth/logout", session={})

    await auth_routes.logout(request)

    record = events.one("panel_logout")
    assert not hasattr(record, "user_id_session")
    assert not hasattr(record, "username_session")


# --- bootstrap ------------------------------------------------------------


async def _register(
    tmp_path,
    monkeypatch,
    *,
    username=USERNAME,
    password=PASSWORD,
    password_confirm=None,
    vault_path=None,
    session=None,
):
    monkeypatch.setattr(vault_service.settings, "vault_path", str(tmp_path))
    request = _make_request("/admin/register")
    session = _empty_users_session() if session is None else session
    response = await auth_routes.register_submit(
        request=request,
        username=username,
        password=password,
        password_confirm=password if password_confirm is None else password_confirm,
        vault_path=str(tmp_path) if vault_path is None else vault_path,
        session=session,
    )
    return response, session


@pytest.mark.parametrize(
    "kwargs, reason",
    [
        ({"username": "Not Valid!"}, "invalid_username"),
        ({"password": "short", "password_confirm": "short"}, "weak_password"),
        ({"password_confirm": "something else"}, "password_mismatch"),
        ({"vault_path": "   "}, "vault_path_missing"),
        ({"vault_path": "relative/path"}, "vault_path_invalid"),
    ],
)
async def test_each_bootstrap_refusal_has_its_own_reason(
    tmp_path, monkeypatch, events, kwargs, reason
):
    response, _ = await _register(tmp_path, monkeypatch, **kwargs)

    assert response.status_code == 400
    assert events.acquires == [("panel_bootstrap_refused", events.acquires[0][1])]
    record = events.one("panel_bootstrap_refused")
    assert record.levelno == logging.WARNING
    assert record.reason == reason
    assert record.client_ip.startswith("10.0.0.")


async def test_losing_the_bootstrap_race_is_recorded(tmp_path, monkeypatch, events):
    """The form is told nothing (it is redirected to login), so the log is the
    only place the race is visible."""
    result = MagicMock()
    result.scalar.return_value = 1  # users table is no longer empty
    result.scalar_one_or_none.return_value = None
    session = AsyncMock()
    session.execute.return_value = result
    session.add = MagicMock()

    response, _ = await _register(tmp_path, monkeypatch, session=session)

    assert response.status_code == 302
    assert events.one("panel_bootstrap_refused").reason == "already_bootstrapped"


async def test_the_first_administrator_is_recorded_after_the_commit(
    tmp_path, monkeypatch, events
):
    response, session = await _register(tmp_path, monkeypatch)

    assert response.status_code == 302
    assert session.commit.await_count == 1
    record = events.one("panel_bootstrap_admin_created")
    assert record.levelno == logging.INFO
    assert record.username == USERNAME
    assert events.acquires == [("panel_bootstrap_admin_created", events.acquires[0][1])]


async def test_a_failed_bootstrap_commit_leaves_no_admin_record(
    tmp_path, monkeypatch, events
):
    """The defect round 2 of the design review caught, pinned.

    The insert happens, the commit raises, the transaction rolls back — and the
    record must not exist, because an administrator who does not exist is the
    single worst thing this log could claim.
    """
    session = _empty_users_session(commit_error=RuntimeError("commit failed"))

    with pytest.raises(RuntimeError):
        await _register(tmp_path, monkeypatch, session=session)

    assert events.named("panel_bootstrap_admin_created") == []


# --- the malformed stored hash -------------------------------------------


async def test_a_corrupt_password_hash_is_recorded_through_the_suppressor(events):
    """A caller drives this branch through the login form, so it is bounded.

    One `password_hash_malformed` for the corrupt column, then one
    `panel_login_failed` for the refusal it produced — the hash never verifies,
    so the login is a `bad_password` outcome and both records are true.
    """
    await _login(_stored_user(password_hash="not-a-bcrypt-hash"))

    assert [event for event, _ in events.acquires] == [
        "password_hash_malformed",
        "panel_login_failed",
    ]
    record = events.one("password_hash_malformed")
    assert record.user_id == 1
    assert events.one("panel_login_failed").reason == "bad_password"
