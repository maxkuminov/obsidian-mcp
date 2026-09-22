"""#189 — a per-account failed-login budget that cannot reach another account.

The panel's 5/min slowapi limit is keyed on the client address, which hands an
attacker a fresh allowance for every address they can rotate through. The
budget added beside it is keyed **exactly** on `users.id`, so the two are
additive and neither subsumes the other.

Three properties are asserted here, and the second is the one the spec review
forced:

* **It bounds guessing.** The attempt past the budget is refused *before*
  `verify_password` runs — a budget consulted after the comparison bounds
  nothing, because the guess has already been answered. Asserted by counting
  calls, not by timing.
* **It cannot reach another account, and an unknown username creates nothing.**
  The salted slot table the `/mcp` budget uses merges colliding keys, which is
  a bound there and would be a cross-account denial here. So: a second account
  signs in normally while the first is exhausted, and any number of
  non-existent usernames consumes no account allowance.
* **It is not a username oracle by content.** The refusal is the same status,
  the same template and the same message as an ordinary failed login, and not
  a 429. Content only — timing differs (bcrypt is skipped) and is an accepted
  limitation, and the CSRF token is pinned here precisely because it is
  excluded from the comparison by definition.
"""
import itertools
import logging
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.requests import Request

from src.auth import routes as auth_routes
from src.auth.passwords import hash_password
from src.limiter import limiter
from src.services import rate_limits, security_events

PASSWORD = "correct horse battery staple"
USERNAME = "budgetadmin"
OTHER_USERNAME = "otheradmin"

# One fresh client address per attempt: the retained 5/min address limit would
# otherwise answer 429 on its own terms and prove nothing about the account
# budget — which is also the point of the "rotating addresses" scenario.
_client_ips = itertools.count(1)


@pytest.fixture(autouse=True)
def _clean_rate_limiter():
    limiter.reset()
    yield
    limiter.reset()


@pytest.fixture(autouse=True)
def _constant_csrf(monkeypatch):
    """Pin the token so "the same page" is a claim about the page.

    The real token is a timestamped signature, so two renderings of one
    unchanged page differ. Equivalence is defined over content and explicitly
    excludes this value (design D6a); pinning it is how the content comparison
    becomes possible at all.
    """
    monkeypatch.setattr(auth_routes, "generate_csrf_token", lambda request: "csrf")


@pytest.fixture(autouse=True)
def _small_budget(monkeypatch):
    """Three failures per window, so the tests read as arithmetic."""
    monkeypatch.setattr(rate_limits.settings, "panel_login_failure_limit", 3)
    monkeypatch.setattr(rate_limits.settings, "panel_login_failure_window_seconds", 900)


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture
def events():
    handler = _Capture()
    logger = security_events.logger
    logger.addHandler(handler)
    propagate, level = logger.propagate, logger.level
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    security_events.reset_state()
    try:
        with security_events.suppression_disabled():
            yield handler.records
    finally:
        logger.removeHandler(handler)
        logger.propagate = propagate
        logger.setLevel(level)
        security_events.reset_state()


@pytest.fixture
def verify_calls(monkeypatch):
    """Count `verify_password` calls without changing what it answers."""
    calls: list[int] = []
    real = auth_routes.verify_password

    def counting(password, password_hash, **kwargs):
        calls.append(kwargs.get("user_id"))
        return real(password, password_hash, **kwargs)

    monkeypatch.setattr(auth_routes, "verify_password", counting)
    return calls


def _make_request(client_ip: str | None = None) -> Request:
    """One attempt. `client_ip` pins the address instead of rotating it, which
    is how the retained 5/min address limit can be exercised deliberately."""
    path = "/admin/auth/login"
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
            "client": (
                client_ip or f"10.0.0.{next(_client_ips) % 250 + 1}",
                12345,
            ),
            "server": ("testserver", 80),
            "session": {},
            "state": {},
            # slowapi's own 429 delegate reads `request.app.state.limiter`, so
            # the address-limit case below can hand a real request to the
            # application's handler rather than a stand-in.
            "app": SimpleNamespace(state=SimpleNamespace(limiter=limiter)),
        }
    )


def _stored_user(user_id=1, username=USERNAME, **overrides):
    fields = {
        "id": user_id,
        "username": username,
        "password_hash": hash_password(PASSWORD),
        "is_active": True,
        "is_admin": True,
        "session_version": 1,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _session_returning(user):
    result = MagicMock()
    result.scalar_one_or_none.return_value = user
    session = AsyncMock()
    session.execute.return_value = result
    session.add = MagicMock()
    return session


async def _login(user, password=PASSWORD, username=USERNAME, client_ip=None):
    return await auth_routes.login_submit(
        request=_make_request(client_ip),
        username=username,
        password=password,
        next="/admin/",
        session=_session_returning(user),
    )


def _named(records, event):
    return [r for r in records if r.getMessage() == event]


# --- the bound, and where it sits ----------------------------------------


async def test_rotating_addresses_cannot_outrun_the_account_budget(verify_calls):
    """Every attempt from a different address; the budget still bites.

    And it bites *before* the comparison: three failures run `verify_password`,
    the fourth does not. A budget consulted afterwards would bound nothing.
    """
    user = _stored_user()
    for _ in range(3):
        response = await _login(user, password=PASSWORD + "!")
        assert response.status_code == 401

    assert verify_calls == [1, 1, 1]

    refused = await _login(user, password=PASSWORD + "!")

    assert refused.status_code == 401
    assert verify_calls == [1, 1, 1], "the throttled attempt compared no password"


def _mask_nonces(body: bytes) -> bytes:
    return re.sub(rb'nonce="[^"]*"', b'nonce="N"', body)


async def test_the_refusal_is_content_identical_to_an_ordinary_failure(verify_calls):
    """Same status, same template, same message — and not a 429.

    Rendered for the same submitted inputs, so a difference here would be a
    username-and-throttle-state oracle readable straight from the response.
    """
    user = _stored_user()
    ordinary = await _login(user, password=PASSWORD + "!")
    for _ in range(2):
        await _login(user, password=PASSWORD + "!")

    throttled = await _login(user, password=PASSWORD + "!")

    assert throttled.status_code == ordinary.status_code == 401
    # The panel CSP nonce is fresh per response (#195) and carries no state,
    # so it is masked before comparing; everything else must match byte-for-byte.
    assert _mask_nonces(throttled.body) == _mask_nonces(ordinary.body)
    assert throttled.headers["content-type"] == ordinary.headers["content-type"]


# --- the cross-account property -------------------------------------------


async def test_a_second_account_signs_in_while_the_first_is_exhausted():
    """The failure mode the salted slot table would have introduced.

    Merged keys would let three failures against one name refuse a *different*
    account's correct password. Asserted directly rather than argued from the
    data structure.
    """
    attacked = _stored_user(user_id=1)
    for _ in range(4):
        await _login(attacked, password=PASSWORD + "!")

    bystander = _stored_user(user_id=2, username=OTHER_USERNAME)
    response = await _login(bystander, username=OTHER_USERNAME)

    assert response.status_code == 302, "an unrelated account's password still works"


async def test_unknown_usernames_consume_no_account_allowance(verify_calls):
    """There is no credential behind a name that matches no account.

    Each attempt comes from its own address so the retained 5/min address limit
    is unexhausted; otherwise the 429 would answer first and this would prove
    nothing about the account budget.
    """
    for index in range(20):
        response = await _login(None, username=f"ghost{index}")
        assert response.status_code == 401

    assert rate_limits._login_failures == {}

    response = await _login(_stored_user())

    assert response.status_code == 302
    assert verify_calls == [1]


async def test_an_authenticated_session_is_unaffected_by_a_flood(verify_calls):
    """The second of the four properties that make this not a lockout.

    A flood can delay a *fresh password* sign-in for the name it attacks; it
    cannot evict anyone, because `login_form` resolves an existing session and
    short-circuits to the panel before any of this machinery is consulted.
    The session's own validation is `get_active_session_user`'s contract and is
    tested where it lives (#198); what is asserted here is that the exhausted
    budget is not in that path at all — no password is compared, the redirect
    is the ordinary one, and the account's counter is left exactly as the
    flood left it.
    """
    user = _stored_user()
    for _ in range(4):
        await _login(user, password=PASSWORD + "!")
    assert verify_calls == [1, 1, 1], "the budget is exhausted"

    async def _resolve(_request, _session):
        return user

    saved = auth_routes.get_active_session_user
    auth_routes.get_active_session_user = _resolve
    try:
        response = await auth_routes.login_form(
            request=_make_request(),
            next="/admin/",
            session=_session_returning(user),
        )
    finally:
        auth_routes.get_active_session_user = saved

    assert response.status_code == 302
    assert response.headers["location"] == "/admin/"
    assert verify_calls == [1, 1, 1], "the session path compares no password"
    assert list(rate_limits._login_failures) == [1], "and clears no allowance"


async def test_the_address_limit_still_answers_on_the_sixth_rapid_attempt():
    """The retained 5/min per-address limit, asserted rather than assumed.

    The account budget is *additive* to it and replaces neither: this one is
    what bounds an attacker walking many usernames from one address, where no
    single account counter ever fills. Every other case in this file rotates
    addresses precisely to stay under it, so without this the restated
    scenario would rest on nothing.

    slowapi raises; the application turns that into the 429 the scenario
    names, through the one handler every limited route shares.
    """
    from slowapi.errors import RateLimitExceeded

    from src import main

    for index in range(5):
        response = await _login(
            None, username=f"walk{index}", client_ip="198.51.100.9"
        )
        assert response.status_code == 401

    sixth = _make_request("198.51.100.9")
    with pytest.raises(RateLimitExceeded) as exceeded:
        await auth_routes.login_submit(
            request=sixth,
            username="walk5",
            password=PASSWORD,
            next="/admin/",
            session=_session_returning(None),
        )

    assert main._rate_limit_handler(sixth, exceeded.value).status_code == 429


# --- what is counted, and what is not -------------------------------------


async def test_a_correct_password_succeeds_and_records_no_failure():
    response = await _login(_stored_user())

    assert response.status_code == 302
    assert rate_limits._login_failures == {}


async def test_an_inactive_account_is_counted_but_an_unknown_name_is_not():
    """Keying is by row, not by the active flag.

    An inactive account cannot be signed into anyway, so the budget is moot for
    it — but branching on the flag would make budget behaviour a side channel
    for account state, and there is no reason to introduce one.
    """
    await _login(_stored_user(is_active=False))

    assert list(rate_limits._login_failures) == [1]


async def test_the_budget_expires_without_intervention(monkeypatch, verify_calls):
    clock = {"now": 1_000.0}
    monkeypatch.setattr(
        rate_limits, "time", SimpleNamespace(monotonic=lambda: clock["now"])
    )
    user = _stored_user()
    for _ in range(4):
        await _login(user, password=PASSWORD + "!")
    assert verify_calls == [1, 1, 1], "the fourth was refused"

    clock["now"] += 901.0
    await _login(user, password=PASSWORD + "!")

    assert verify_calls == [1, 1, 1, 1], "evaluated again, no administrative action"
    assert list(rate_limits._login_failures) == [1], "and the stale window was swept"


async def test_the_map_holds_one_entry_per_recently_failing_account(monkeypatch):
    """Bounded by the users table, not by the attacker's imagination."""
    clock = {"now": 1_000.0}
    monkeypatch.setattr(
        rate_limits, "time", SimpleNamespace(monotonic=lambda: clock["now"])
    )
    for _ in range(2):
        await _login(_stored_user(user_id=1), password=PASSWORD + "!")
        await _login(
            _stored_user(user_id=2, username=OTHER_USERNAME),
            password=PASSWORD + "!",
            username=OTHER_USERNAME,
        )
    for index in range(10):
        await _login(None, username=f"ghost{index}")

    assert sorted(rate_limits._login_failures) == [1, 2]

    clock["now"] += 901.0
    await _login(_stored_user(user_id=3, username="third"), username="third")

    assert rate_limits._login_failures == {}, "expired entries are swept on access"


# --- the record -----------------------------------------------------------


async def test_the_throttle_record_carries_exactly_its_allow_listed_fields(events):
    user = _stored_user()
    for _ in range(4):
        await _login(user, password=PASSWORD + "!")

    (record,) = _named(events, "panel_login_account_throttled")
    assert record.levelno == logging.WARNING
    assert record.username_submitted == USERNAME
    assert record.limit_count == 3
    assert record.window_seconds == 900
    assert record.client_ip.startswith("10.0.0.")
    assert record.route == "/admin/auth/login"
    # The response says nothing about the account; neither may the record.
    assert not hasattr(record, "user_id")
    assert not hasattr(record, "username")
    assert not hasattr(record, "reason")


async def test_the_subject_is_the_address_not_the_submitted_username(events):
    """A caller-supplied subject mints a fresh logging allowance per value.

    The doc's rule is categorical, and a submitted username is as
    caller-supplied as a value gets.
    """
    subjects = []
    real_acquire = security_events.acquire

    def counting(event, subject=None, **kwargs):
        subjects.append((event, subject))
        return real_acquire(event, subject, **kwargs)

    security_events.acquire, saved = counting, security_events.acquire
    try:
        user = _stored_user()
        for _ in range(4):
            await _login(user, password=PASSWORD + "!")
    finally:
        security_events.acquire = saved

    throttles = [s for event, s in subjects if event == "panel_login_account_throttled"]
    assert throttles and all(s.startswith("ip:") for s in throttles)
    assert not any(USERNAME in s for s in throttles)


async def test_an_over_long_submitted_username_is_truncated_by_the_formatter(events):
    """No new truncation code: `src/logging_setup.py` already bounds this field
    at 64 characters and `coerce_field` applies it, so the new event inherits
    the bound rather than reintroducing the problem."""
    from src.logging_setup import ALLOWED_FIELDS, coerce_field

    long_name = "u" * 300
    user = _stored_user(username=long_name)
    for _ in range(4):
        await _login(user, password=PASSWORD + "!", username=long_name)

    (record,) = _named(events, "panel_login_account_throttled")
    assert len(coerce_field("username_submitted", record.username_submitted)) == 64
    assert ALLOWED_FIELDS["username_submitted"].max_len == 64


async def test_the_submitted_password_is_in_no_record(events):
    """The canary, for the new event as for every other login record."""
    user = _stored_user()
    for _ in range(4):
        await _login(user, password=PASSWORD + "!")

    for record in events:
        rendered = record.getMessage() + " ".join(
            str(value) for value in record.__dict__.values()
        )
        assert PASSWORD not in rendered
        assert (PASSWORD + "!") not in rendered
