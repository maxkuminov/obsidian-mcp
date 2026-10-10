"""The shared key-creation budget, the active-key cap and admin-only unlimited (#323).

Before #323 the panel's create form had no limiter at all, and a blank limit
field created an unlimited key. Every new key is a fresh `/mcp` principal with
full bursts, so a scripted form could reset the per-principal buckets and skip
the daily quota at will. The fix has three parts, each pinned here:

* **One budget across both representations.** `POST /api/keys` and
  `POST /admin/keys/create` charge the same account counter and the same
  address counter (`rate_limits.try_charge_key_creation`); alternating JSON and
  form requests gains nothing.
* **Blank is the default, unlimited is an admin's explicit request**, on create
  and on edit, on both surfaces (`src/services/api_keys.py`).
* **A non-admin's stock of active keys is capped.** The exact-under-concurrency
  half of that needs a real row lock and lives in
  `tests/integration/test_issue_323_key_cap_pg.py`.

Hermetic: the handlers run directly against fake sessions with real starlette
requests (slowapi on the JSON route insists on the real type).
"""
import asyncio
import logging
import os
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

import src.api.routes as api
import src.control_panel.routes as panel
from src.auth.session import _SingleUserSentinel
from src.config import Settings, settings
from src.models.db import APIKey
from src.services import api_keys as key_issuance
from src.services import rate_limits, security_events

DEFAULT = 5000


# --------------------------------------------------------------------------
# plumbing
# --------------------------------------------------------------------------


def _user(uid, *, admin=False, name=None):
    return SimpleNamespace(
        id=uid, is_admin=admin, is_active=True, username=name or f"user{uid}"
    )


class _Session:
    """Records inserts; answers the cap's count with this account's keys.

    `execute` yields to the loop, so concurrent creates genuinely interleave
    at every statement — the budget must still be atomic across them.
    """

    def __init__(self, store, key=None):
        self.store = store
        self.added = []
        self.rolled_back = 0
        self._key = key

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        for obj in self.added:
            if obj not in self.store:
                self.store.append(obj)

    async def rollback(self):
        self.rolled_back += 1

    async def refresh(self, obj):
        if getattr(obj, "id", None) is None:
            obj.id = len(self.store)

    async def execute(self, stmt, params=None):
        await asyncio.sleep(0)
        key = self._key
        store = self.store
        return SimpleNamespace(
            scalar=lambda: sum(1 for k in store if k.is_active is not False),
            scalar_one_or_none=lambda: key,
        )


_counter = iter(range(1, 1_000_000))


def _request(address=None, path="/api/keys"):
    if address is None:
        n = next(_counter)
        address = f"10.{n // 65536 % 256}.{n // 256 % 256}.{n % 256}"
    return Request({
        "type": "http",
        "method": "POST",
        "path": path,
        "headers": [],
        "query_string": b"",
        "client": (address, 1234),
        "state": {},
        "app": None,
        "session": {},
    })


def _json(user, store, *, address=None, **payload):
    """`POST /api/keys`. Returns the response or the raised HTTPException."""
    payload.setdefault("name", "k")
    session = _Session(store)
    try:
        return asyncio.run(_ajson(user, session, address, payload))
    except HTTPException as exc:
        return exc


async def _ajson(user, session, address, payload):
    return await api.create_key(
        request=_request(address),
        req=api.CreateKeyRequest(**payload),
        session=session,
        user=user,
    )


async def _aform(user, store, address=None, *, name="k", limit="", unlimited=""):
    request = _request(address, "/admin/keys/create")
    response = await panel.create_key_form(
        request=request,
        name=name,
        permission="read",
        daily_request_limit=limit,
        unlimited=unlimited,
        session=_Session(store),
        user=user,
    )
    assert response.status_code == 303
    return request.session.get("flash_key_error"), request.session.get("flash_new_key")


def _form(user, store, address=None, **kw):
    """`POST /admin/keys/create`. Returns `(flash_error, new_key)`."""
    return asyncio.run(_aform(user, store, address, **kw))


@pytest.fixture
def budget(monkeypatch):
    """Small limits so the tests reach them; the cap out of the way."""
    monkeypatch.setattr(settings, "key_creation_account_limit", 3)
    monkeypatch.setattr(settings, "key_creation_address_limit", 5)
    monkeypatch.setattr(settings, "key_creation_window_seconds", 3600)
    monkeypatch.setattr(settings, "key_max_active_per_account", None)
    rate_limits.reset_key_creation_for_tests()
    yield
    rate_limits.reset_key_creation_for_tests()


@pytest.fixture
def events(monkeypatch):
    """Every `key_creation_throttled` / `panel_forbidden` record emitted."""
    seen = []

    def _log(event, level, exc_info, fields):
        seen.append((event, level, fields))

    monkeypatch.setattr(security_events, "_log", _log)
    security_events.reset_state()
    with security_events.suppression_disabled():
        yield seen
    security_events.reset_state()


def _throttled(events):
    return [f for e, _, f in events if e == "key_creation_throttled"]


# --------------------------------------------------------------------------
# 1–4. one budget, keyed by account and by address
# --------------------------------------------------------------------------


def test_alternating_json_and_form_requests_share_one_budget(budget):
    user = _user(7)
    store = []
    outcomes = []
    for i in range(6):
        if i % 2 == 0:
            r = _json(user, store)
            outcomes.append(("json", not isinstance(r, HTTPException)))
        else:
            err, new = _form(user, store)
            outcomes.append(("form", new is not None and err is None))
    assert [ok for _, ok in outcomes] == [True, True, True, False, False, False]
    assert len(store) == 3


def test_the_fourth_request_is_refused_on_either_route(budget):
    user = _user(8)
    store = []
    for _ in range(3):
        _form(user, store)
    refused = _json(user, store)
    assert isinstance(refused, HTTPException) and refused.status_code == 429
    err, new = _form(user, store)
    assert new is None and "Too many keys" in err
    assert len(store) == 3


def test_rotating_addresses_cannot_outrun_the_account_counter(budget):
    user = _user(9)
    store = []
    results = [_json(user, store) for _ in range(6)]  # fresh address each
    assert sum(not isinstance(r, HTTPException) for r in results) == 3
    assert all(r.status_code == 429 for r in results[3:])
    assert len(store) == 3


def test_rotating_accounts_cannot_outrun_the_address_counter(budget):
    store = []
    created = 0
    for uid in range(100, 108):  # each account well under its own limit
        err, new = _form(_user(uid), store, address="203.0.113.9")
        created += new is not None
    assert created == 5
    assert len(store) == 5


def test_an_unrelated_account_and_address_are_unaffected(budget):
    store = []
    for _ in range(3):
        _form(_user(1), store, address="203.0.113.1")
    for uid in range(2, 4):
        _form(_user(uid), store, address="203.0.113.1")
    assert _form(_user(1), store, address="203.0.113.1")[1] is None
    assert _form(_user(50), store, address="203.0.113.1")[1] is None
    # A different account at a different address.
    assert _form(_user(99), store, address="198.51.100.4")[1] is not None


def test_a_request_with_no_client_address_shares_one_bucket(budget, monkeypatch):
    monkeypatch.setattr(settings, "key_creation_account_limit", None)
    for uid in range(5):
        assert rate_limits.try_charge_key_creation(("user", uid), None) is None
    assert rate_limits.try_charge_key_creation(("user", 99), None) is not None
    assert rate_limits.try_charge_key_creation(("user", 99), "") is not None


# --------------------------------------------------------------------------
# 5. refused checks charge nothing
# --------------------------------------------------------------------------


def test_validation_failures_and_unlimited_attempts_charge_nothing(budget, events):
    user = _user(11)
    store = []
    for _ in range(10):
        assert "Key name" in _form(user, store, name="bad/name!")[0]
        # A bad JSON body is a 422 from the model, before the handler runs.
        with pytest.raises(ValueError):
            api.CreateKeyRequest(name="bad/name!", permission="admin")
        err, _ = _form(user, store, unlimited="1")
        assert "administrator" in err
        r = _json(user, store, daily_request_limit=None)
        assert r.status_code == 403
        assert "whole number" in _form(user, store, limit="1oo")[0]
    assert store == []
    for _ in range(3):
        assert _form(user, store)[1] is not None
    assert len(store) == 3


def test_a_cap_refusal_charges_nothing_and_is_not_a_security_event(budget, events, monkeypatch):
    monkeypatch.setattr(settings, "key_max_active_per_account", 2)
    user = _user(12)
    store = [APIKey(name="a", is_active=True), APIKey(name="b", is_active=True)]
    r = _json(user, store)
    assert r.status_code == 409 and "Revoke" in r.detail
    err, new = _form(user, store)
    assert new is None and "Revoke" in err
    assert _throttled(events) == []
    # Revoke one: room again, and the full account allowance is still there.
    store[0].is_active = False
    assert _form(user, store)[1] is not None
    assert rate_limits._key_creation_accounts[("user", 12)].count == 1


def test_admins_and_the_single_user_operator_are_not_capped(budget, monkeypatch):
    monkeypatch.setattr(settings, "key_max_active_per_account", 1)
    store = [APIKey(name="a", is_active=True), APIKey(name="b", is_active=True)]
    assert _form(_user(13, admin=True), store)[1] is not None
    assert _form(_SingleUserSentinel(), store)[1] is not None


# --------------------------------------------------------------------------
# 6–7. atomicity and expiry
# --------------------------------------------------------------------------


def test_concurrent_creates_cannot_overshoot(budget):
    user = _user(14)
    store = []

    async def many():
        return await asyncio.gather(
            *[_aform(user, store) for _ in range(12)]
        )

    results = asyncio.run(many())
    assert sum(new is not None for _, new in results) == 3
    assert len(store) == 3


def test_the_budget_expires_without_intervention(budget, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(rate_limits, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    user = _user(15)
    store = []
    for _ in range(3):
        assert _form(user, store)[1] is not None
    assert _form(user, store)[1] is None
    refusal = rate_limits.try_charge_key_creation(("user", 15), "x")
    assert refusal.retry_after_seconds == 3600
    clock[0] += 1800.4
    refusal = rate_limits.try_charge_key_creation(("user", 15), "x")
    assert 0 < refusal.retry_after_seconds <= 3600 - 1800
    clock[0] = 1000.0 + 3600
    assert _form(user, store)[1] is not None


def test_both_counters_are_charged_together_or_not_at_all(budget):
    # Account exhausted: the address is not charged by the refused attempt.
    for _ in range(3):
        assert rate_limits.try_charge_key_creation(("user", 1), "a") is None
    assert rate_limits.try_charge_key_creation(("user", 1), "b").reason == "account_budget"
    assert "b" not in rate_limits._key_creation_addresses
    # Address exhausted: the account is not charged by the refused attempt.
    for uid in range(2, 4):
        assert rate_limits.try_charge_key_creation(("user", uid), "a") is None
    assert rate_limits.try_charge_key_creation(("user", 9), "a").reason == "address_budget"
    assert ("user", 9) not in rate_limits._key_creation_accounts


def test_each_counter_can_be_disabled_independently(budget, monkeypatch):
    monkeypatch.setattr(settings, "key_creation_account_limit", None)
    for uid in range(5):
        assert rate_limits.try_charge_key_creation(("user", 1), f"x{uid}") is None
    assert rate_limits._key_creation_accounts == {}
    rate_limits.reset_key_creation_for_tests()
    monkeypatch.setattr(settings, "key_creation_account_limit", 3)
    monkeypatch.setattr(settings, "key_creation_address_limit", None)
    for _ in range(3):
        assert rate_limits.try_charge_key_creation(("user", 1), "same") is None
    assert rate_limits.try_charge_key_creation(("user", 1), "same").reason == "account_budget"
    assert rate_limits._key_creation_addresses == {}
    monkeypatch.setattr(settings, "key_creation_account_limit", None)
    for _ in range(50):
        assert rate_limits.try_charge_key_creation(("user", 1), "same") is None


def test_the_budget_is_bounded_by_construction(budget, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(rate_limits, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    # Refused attempts from many addresses create no address entries.
    for _ in range(3):
        rate_limits.try_charge_key_creation(("user", 1), "a")
    for n in range(500):
        rate_limits.try_charge_key_creation(("user", 1), f"rot-{n}")
    assert len(rate_limits._key_creation_addresses) == 1
    assert len(rate_limits._key_creation_accounts) == 1
    # And a passed window sweeps both on the next access.
    clock[0] = 3600.0
    rate_limits.try_charge_key_creation(("user", 2), "b")
    assert set(rate_limits._key_creation_accounts) == {("user", 2)}
    assert set(rate_limits._key_creation_addresses) == {"b"}


def test_accounts_are_keyed_exactly(budget):
    """No hashing: one account's exhaustion never refuses another."""
    for _ in range(3):
        rate_limits.try_charge_key_creation(("user", 1), f"a{_}")
    assert rate_limits.try_charge_key_creation(("user", 1), "z") is not None
    for uid in range(2, 200):
        assert rate_limits.try_charge_key_creation(("user", uid), f"q{uid}") is None
    assert len(rate_limits._key_creation_accounts) == 199


# --------------------------------------------------------------------------
# 8. refusal shapes and the event
# --------------------------------------------------------------------------


def test_refusal_shapes_and_the_event(budget, events):
    user = _user(16, name="eve")
    store = []
    for _ in range(3):
        _json(user, store, address="198.51.100.7")
    r = _json(user, store, address="198.51.100.7")
    assert r.status_code == 429
    retry = r.headers["Retry-After"]
    assert retry.isdigit() and 0 < int(retry) <= 3600
    assert "Too many keys" in r.detail and "account" not in r.detail

    err, new = _form(user, store, address="198.51.100.7", name="secret-name")
    assert new is None and "Too many keys" in err
    assert len(store) == 3

    records = _throttled(events)
    assert len(records) == 2, "exactly one emitter invocation per refusal"
    allowed = security_events.EVENT_FIELDS["key_creation_throttled"]
    for fields in records:
        assert set(fields) <= allowed
        assert fields["reason"] == "account_budget"
        assert fields["limit_count"] == 3
        assert fields["window_seconds"] == 3600
        assert fields["actor_user_id"] == 16
        assert fields["actor_username"] == "eve"
        assert fields["client_ip"] == "198.51.100.7"
        assert fields["method"] == "POST"
        assert "secret-name" not in repr(fields)
    assert records[0]["route"] == "/api/keys"
    assert records[1]["route"] == "/admin/keys/create"
    assert all(level == logging.WARNING for e, level, _ in events if e == "key_creation_throttled")


def test_the_address_counter_names_itself_in_the_event(budget, events):
    store = []
    for uid in range(20, 25):
        _form(_user(uid), store, address="192.0.2.50")
    _form(_user(30), store, address="192.0.2.50")
    (record,) = _throttled(events)
    assert record["reason"] == "address_budget"
    assert record["limit_count"] == 5


def test_the_suppression_subject_is_the_account_never_the_address(budget, monkeypatch):
    subjects = []
    real_acquire = security_events.acquire

    def spy(event, subject=None, **kw):
        if event == "key_creation_throttled":
            subjects.append(subject)
        return real_acquire(event, subject, **kw)

    monkeypatch.setattr(security_events, "acquire", spy)
    store = []
    user = _user(31)
    for _ in range(6):
        _json(user, store)  # rotating addresses
    assert subjects == ["user:31"] * 3


def test_single_user_refusals_from_rotating_addresses_share_one_subject(budget, monkeypatch):
    """Codex spec review MAJOR: the sentinel has `id=None`, so a subject from
    `subject_for(user_id=None, request=...)` would fall back to the address and
    every rotated address would mint a fresh log allowance. With the suppressor
    *live*, the records stop at the per-subject cap however many addresses."""
    monkeypatch.setattr(settings, "key_creation_address_limit", None)
    emitted = []
    subjects = []
    real_acquire = security_events.acquire

    def spy(event, subject=None, **kw):
        if event == "key_creation_throttled":
            subjects.append(subject)
        return real_acquire(event, subject, **kw)

    monkeypatch.setattr(security_events, "acquire", spy)
    monkeypatch.setattr(
        security_events, "_log",
        lambda event, level, exc_info, fields: emitted.append(event)
        if event == "key_creation_throttled" else None,
    )
    security_events.reset_state()
    sentinel = _SingleUserSentinel()
    store = []
    for n in range(3 + 25):
        _form(sentinel, store, address=f"10.77.0.{n + 1}")
    assert len(store) == 3
    assert len(subjects) == 25, "one emitter invocation per refusal"
    assert set(subjects) == {"account:single-user"}
    assert len(emitted) == security_events.MAX_EVENTS_PER_WINDOW
    security_events.reset_state()


# --------------------------------------------------------------------------
# 9–10. the unlimited matrix and the null default
# --------------------------------------------------------------------------


def test_create_matrix(budget, events):
    admin, member = _user(40, admin=True), _user(41)
    store = []
    # A value: that value, for anyone.
    assert _json(admin, store, daily_request_limit=250).daily_request_limit == 250
    assert _json(member, store, daily_request_limit=250).daily_request_limit == 250
    # Omitted (JSON) / blank (form): the default, for anyone.
    assert _json(member, store).daily_request_limit == DEFAULT
    _form(member, store, limit="")
    assert store[-1].daily_request_limit == DEFAULT
    _form(admin, store, limit="  ")
    assert store[-1].daily_request_limit == DEFAULT
    # The explicit request: NULL for an admin.
    assert _json(admin, store, daily_request_limit=None).daily_request_limit is None
    rate_limits.reset_key_creation_for_tests()
    _form(admin, store, unlimited="1", limit="77")  # the box wins over a stray number
    assert store[-1].daily_request_limit is None
    # ...and refused for a non-admin, recorded as panel_forbidden.
    before = len(store)
    r = _json(member, store, daily_request_limit=None)
    assert r.status_code == 403
    err, new = _form(member, store, unlimited="1")
    assert new is None and "administrator" in err
    assert len(store) == before
    forbidden = [f for e, _, f in events if e == "panel_forbidden"]
    assert [f["reason"] for f in forbidden] == ["unlimited_requires_admin"] * 2
    assert all(f["actor_user_id"] == 41 for f in forbidden)
    # Only exactly "1" counts as the request.
    _form(admin, store, unlimited="on", limit="")
    assert store[-1].daily_request_limit == DEFAULT


def test_a_null_default_makes_the_limit_required(budget, monkeypatch):
    monkeypatch.setattr(settings, "default_daily_request_limit", None)
    admin, member = _user(42, admin=True), _user(43)
    store = []
    r = _json(member, store)
    assert r.status_code == 400 and "required" in r.detail
    err, new = _form(member, store, limit="")
    assert new is None and "required" in err
    err, new = _form(admin, store, limit="")
    assert new is None and "required" in err
    assert store == []
    assert _form(admin, store, unlimited="1")[1] is not None
    assert store[-1].daily_request_limit is None
    assert _json(admin, store, daily_request_limit=None).daily_request_limit is None


def _existing(limit, owner=41):
    return APIKey(
        id=4, name="k", key_hash="x", key_prefix="omcp_a1b2c3", permission="read",
        is_active=True, user_id=owner, expires_at=None, daily_request_limit=limit,
    )


def _edit_form(user, key, *, limit="", unlimited=""):
    request = _request(path="/admin/keys/4/limit")
    response = asyncio.run(panel.set_key_limit_form(
        request=request, key_id=4, daily_request_limit=limit, unlimited=unlimited,
        session=_Session([], key=key), user=user,
    ))
    assert response.status_code == 303
    return request.session.get("flash_key_error")


def _edit_json(user, key, value):
    try:
        return asyncio.run(api.set_key_limit(
            key_id=4, req=api.SetKeyLimitRequest(daily_request_limit=value),
            request=_request(path="/api/keys/4/limit"),
            session=_Session([], key=key), user=user,
        ))
    except HTTPException as exc:
        return exc


def test_edit_matrix(events):
    admin, member = _user(40, admin=True), _user(41)
    # A value: for anyone allowed to edit the key.
    key = _existing(100)
    assert _edit_form(member, key, limit="250") is None and key.daily_request_limit == 250
    assert _edit_json(member, key, 300).daily_request_limit == 300
    # Blank: an error for everyone, the key unchanged.
    assert _edit_form(member, key, limit="") == "Enter a daily request limit."
    assert "tick Unlimited" in _edit_form(admin, key, limit="")
    assert key.daily_request_limit == 300
    # A non-admin cannot clear a limit, on either surface.
    assert "administrator" in _edit_form(member, key, unlimited="1")
    r = _edit_json(member, key, None)
    assert r.status_code == 403
    assert key.daily_request_limit == 300
    forbidden = [f for e, _, f in events if e == "panel_forbidden"]
    assert [f["reason"] for f in forbidden] == ["unlimited_requires_admin"] * 2
    assert all(f["user_id"] == 41 for f in forbidden)
    # An admin clears it explicitly, and the box wins over a stray number.
    assert _edit_form(admin, key, unlimited="1", limit="9") is None
    assert key.daily_request_limit is None
    key.daily_request_limit = 5
    assert _edit_json(admin, key, None).daily_request_limit is None


def test_ownership_is_checked_before_the_limit_rule(events):
    other = _existing(100, owner=99)
    with pytest.raises(HTTPException) as exc:
        _edit_form(_user(41), other, unlimited="1")
    assert exc.value.status_code == 403
    reasons = [f["reason"] for e, _, f in events if e == "panel_forbidden"]
    assert reasons == ["not_your_key"]


def test_a_non_admin_can_limit_their_grandfathered_unlimited_key():
    key = _existing(None)
    assert _edit_form(_user(41), key, limit="1000") is None
    assert key.daily_request_limit == 1000


# --------------------------------------------------------------------------
# api-auth-hardening delta: owner-scoped creation
# --------------------------------------------------------------------------


def test_a_non_admin_creates_their_own_key_through_the_api(budget):
    store = []
    r = _json(_user(44), store)
    assert not isinstance(r, HTTPException)
    assert store[0].user_id == 44


def test_a_non_admin_cannot_revoke_another_accounts_key():
    key = _existing(100, owner=99)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(api.revoke_key(
            key_id=4, request=None, session=_Session([], key=key), user=_user(41),
        ))
    assert exc.value.status_code == 403
    assert key.is_active is True


# --------------------------------------------------------------------------
# 11. the template and panel.js
# --------------------------------------------------------------------------


class _KeysPageSession:
    def __init__(self, limit):
        self.limit = limit

    async def execute(self, stmt, params=None):
        class _Result:
            def __init__(self, rows):
                self._rows = rows

            def all(self):
                return self._rows

            def fetchall(self):
                return self._rows

        if params is not None:
            return _Result([])
        key = _existing(self.limit)
        key.created_at = SimpleNamespace(isoformat=lambda: "2026-10-01T10:00:00+00:00")
        key.last_used_at = None
        return _Result([(key, "max", True)])


def _render(is_admin, limit=None):
    from jinja2 import (
        ChainableUndefined, ChoiceLoader, DictLoader, Environment, FileSystemLoader,
    )

    captured = {}
    mp = pytest.MonkeyPatch()
    mp.setattr(panel.templates, "TemplateResponse",
               lambda request, name, context: captured.update(context=context))
    mp.setattr(panel, "generate_csrf_token", lambda _r: "csrf-token")
    try:
        asyncio.run(panel.keys_page(
            request=SimpleNamespace(session={}, scope={}),
            session=_KeysPageSession(limit),
            user=SimpleNamespace(id=41, is_admin=is_admin, username="u"),
        ))
    finally:
        mp.undo()
    here = os.path.dirname(os.path.abspath(__file__))
    env = Environment(
        loader=ChoiceLoader([
            DictLoader({"base.html": "{% block page_style %}{% endblock %}{% block content %}{% endblock %}"}),
            FileSystemLoader(os.path.join(here, "..", "src", "control_panel", "templates")),
        ]),
        undefined=ChainableUndefined,
        autoescape=True,
    )
    context = dict(captured["context"])
    context.pop("request", None)
    return env.get_template("keys.html").render(**context)


def _create_modal(html):
    return html.split('id="create-modal"', 1)[1].split('id="limit-modal"', 1)[0]


def _limit_modal(html):
    return html.split('id="limit-modal"', 1)[1]


def test_the_unlimited_control_is_rendered_for_admins_only():
    admin_html = _render(True)
    member_html = _render(False)
    for modal in (_create_modal(admin_html), _limit_modal(admin_html)):
        assert 'name="unlimited"' in modal
        assert 'value="1"' in modal
        assert "data-unlimited-toggle" in modal
    assert 'data-unlimited-target="create-limit-input"' in admin_html
    assert 'data-unlimited-target="limit-input"' in admin_html
    assert 'name="unlimited"' not in member_html
    assert "data-unlimited-toggle" not in member_html


def test_the_help_copy_states_the_new_rules():
    member_create = _create_modal(_render(False))
    assert "empty box also gets that" in member_create
    assert "Only an administrator can make a key unlimited" in member_create
    assert "Empty means unlimited" not in member_create
    member_edit = _limit_modal(_render(False))
    assert "Only an administrator can make a key unlimited" in member_edit
    assert "returns the key to unlimited" not in member_edit


def test_the_new_markup_has_no_inline_handlers_or_styles():
    import re

    for html in (_render(True), _render(False)):
        assert not re.search(r"\son[a-z]+\s*=", html)
        assert not re.search(r"\sstyle\s*=", html)
        assert "!important" not in html


def test_a_grandfathered_unlimited_key_renders_an_editable_row_for_a_non_admin():
    html = _render(False, limit=None)
    assert 'data-limit-edit data-key-id="4" data-limit=""' in html
    assert 'id="limit-input"' in _limit_modal(html)
    assert "disabled" not in _limit_modal(html)


def _panel_js():
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "..", "src", "control_panel", "static", "panel.js")
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def test_panel_js_handles_the_toggle_and_tolerates_its_absence():
    js = _panel_js()
    assert "closest(event, '[data-unlimited-toggle]')" in js
    assert "data-unlimited-target" in js
    # editLimit: tick only when a box exists; with none, the input is enabled.
    assert "if (toggle) { toggle.checked = current === ''; }" in js
    assert "input.disabled = !!(toggle && toggle.checked);" in js


# --------------------------------------------------------------------------
# 12. settings through a real environment file
# --------------------------------------------------------------------------

BASE_ENV = {
    "SECRET_KEY": "an-actual-secret-not-a-placeholder",
    "EMBEDDING_ALLOW_PLAINTEXT": "true",
    "DATABASE_URL": "postgresql+asyncpg://test:test@localhost/test",
    "VAULT_PATH": "/tmp/test-vault",
}

NULLABLE = [
    "KEY_CREATION_ACCOUNT_LIMIT",
    "KEY_CREATION_ADDRESS_LIMIT",
    "KEY_MAX_ACTIVE_PER_ACCOUNT",
]


def _load(tmp_path, values):
    path = tmp_path / "kcb.env"
    path.write_text(
        "\n".join(f"{k}={v}" for k, v in {**BASE_ENV, **values}.items()) + "\n",
        encoding="utf-8",
    )
    return Settings(_env_file=str(path))


def test_the_shipped_defaults(tmp_path):
    loaded = _load(tmp_path, {})
    assert loaded.key_creation_account_limit == 10
    assert loaded.key_creation_address_limit == 20
    assert loaded.key_creation_window_seconds == 3600
    assert loaded.key_max_active_per_account == 25


@pytest.mark.parametrize("setting", NULLABLE)
@pytest.mark.parametrize("spelling", ["", "null", "none", "NULL", " None "])
def test_off_spellings_disable(tmp_path, setting, spelling):
    assert getattr(_load(tmp_path, {setting: spelling}), setting.lower()) is None


@pytest.mark.parametrize("setting", NULLABLE + ["KEY_CREATION_WINDOW_SECONDS"])
def test_zero_is_refused(tmp_path, setting):
    with pytest.raises(Exception):
        _load(tmp_path, {setting: "0"})


@pytest.mark.parametrize("setting,value", [
    ("KEY_CREATION_ACCOUNT_LIMIT", "1000001"),
    ("KEY_CREATION_ADDRESS_LIMIT", "1000001"),
    ("KEY_MAX_ACTIVE_PER_ACCOUNT", "1000001"),
    ("KEY_CREATION_WINDOW_SECONDS", "86401"),
])
def test_above_the_ceiling_is_refused(tmp_path, setting, value):
    with pytest.raises(Exception):
        _load(tmp_path, {setting: value})


def test_the_window_is_not_nullable(tmp_path):
    with pytest.raises(Exception):
        _load(tmp_path, {"KEY_CREATION_WINDOW_SECONDS": "null"})


# --------------------------------------------------------------------------
# The resolvers, directly: the full D5 matrix
# --------------------------------------------------------------------------

_ADMIN, _MEMBER = _user(1, admin=True), _user(2)


@pytest.mark.parametrize("default", [DEFAULT, None])
@pytest.mark.parametrize("who", ["admin", "member"])
@pytest.mark.parametrize("ask", ["value", "blank", "unlimited"])
def test_resolve_create_limit_matrix(monkeypatch, default, who, ask):
    monkeypatch.setattr(settings, "default_daily_request_limit", default)
    user = _ADMIN if who == "admin" else _MEMBER
    limit, refusal = key_issuance.resolve_create_limit(
        user,
        provided=ask == "value",
        value=250 if ask == "value" else None,
        unlimited=ask == "unlimited",
    )
    if ask == "value":
        assert (limit, refusal) == (250, None)
    elif ask == "blank":
        if default is None:
            assert refusal.code == key_issuance.REFUSAL_REQUIRED
        else:
            assert (limit, refusal) == (DEFAULT, None)
    elif who == "admin":
        assert (limit, refusal) == (None, None)
    else:
        assert refusal.code == key_issuance.REFUSAL_FORBIDDEN_UNLIMITED


@pytest.mark.parametrize("default", [DEFAULT, None])
@pytest.mark.parametrize("who", ["admin", "member"])
@pytest.mark.parametrize("ask", ["value", "blank", "unlimited"])
def test_resolve_edit_limit_matrix(monkeypatch, default, who, ask):
    monkeypatch.setattr(settings, "default_daily_request_limit", default)
    user = _ADMIN if who == "admin" else _MEMBER
    limit, refusal = key_issuance.resolve_edit_limit(
        user, value=250 if ask == "value" else None, unlimited=ask == "unlimited"
    )
    if ask == "value":
        assert (limit, refusal) == (250, None)
    elif ask == "blank":
        # Never the default, whatever it is.
        assert limit is None and refusal.code == key_issuance.REFUSAL_BLANK_EDIT
    elif who == "admin":
        assert (limit, refusal) == (None, None)
    else:
        assert refusal.code == key_issuance.REFUSAL_FORBIDDEN_UNLIMITED


def test_the_single_user_operator_is_one_fixed_account():
    a, b = _SingleUserSentinel(), _SingleUserSentinel()
    assert key_issuance.account_key(a) == key_issuance.account_key(b) == ("single-user",)
    assert key_issuance.account_subject(a) == "account:single-user"
    assert key_issuance.account_key(_user(5)) == ("user", 5)
    assert key_issuance.account_subject(_user(5)) == "user:5"
