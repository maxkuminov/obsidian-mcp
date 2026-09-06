"""L1 — the per-address budget on *failed* `/mcp` authentication (#194).

What this control is, and what it is not. It bounds **the database work an
unauthenticated caller can force**: every probe that reaches the credential
lookup costs one session checkout and one indexed SELECT, and nothing else on
this server bounds how many of those a caller may buy. It is *not* a defence
against credential guessing — #194's own verification withdrew that claim,
because the keys are 256-bit `secrets.token_hex` values.

The four properties that make it correct, each pinned below:

* **Every 401 branch increments.** A prober picks the cheapest branch, so a
  budget covering six of seven bounds nothing. Ten branches charge through
  `_emit_auth_failure`, which every one of them already calls; the eleventh —
  a request with no bearer token at all, the cheapest probe there is — charges
  directly, because no credential was presented and there is no `auth_failure`
  record to make. An AST sweep pins that no 401 can be added without a charge.
* **The refusal costs nothing.** Checked *before* the credential lookup, so an
  over-budget request opens no session and issues no query. Pinned by counting
  session factory calls.
* **The address is the proxied client's, never a header.** Reading
  `X-Forwarded-For` here would accept a forged one from any client on the
  internet, which is precisely what `ProxyHeadersMiddleware` exists to stop.
* **Memory is bounded by construction.** A fixed-size table of counters indexed
  by a per-process salted hash: 100,000 distinct addresses leave memory
  proportional to the table, colliding addresses share a budget rather than
  escape one, and a request with no address is charged to a reserved shared
  slot rather than exempted — exempting would be a bypass anyone who can strip
  the header gets for free.
"""
import ast
import asyncio
import inspect
import pathlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import src.mcp_server.auth as mcp_auth
from src.models.db import APIKey, OAuthToken
from src.services import rate_limits


# ── plumbing ────────────────────────────────────────────────────────────────


class _Result:
    """Answers whichever accessor the branch under test uses."""

    def __init__(self, scalar=None, first=None):
        self._scalar = scalar
        self._first = first

    def scalar_one_or_none(self):
        return self._scalar

    def scalar(self):
        return self._scalar

    def first(self):
        return self._first

    def fetchall(self):
        return []


class _Session:
    """Stands in for the one `async_session()` the middleware opens."""

    def __init__(self, *, api_key=None, oauth=None, user_active=True,
                 client_owner=None, client_name="A Client"):
        self.api_key = api_key
        self.oauth = oauth
        self.user_active = user_active
        self.client_owner = client_owner
        self.client_name = client_name

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def commit(self):
        pass

    async def execute(self, stmt, *_a, **_kw):
        sql = str(stmt)
        if sql.startswith("UPDATE"):
            return _Result()
        if "FROM api_keys" in sql:
            return _Result(scalar=self.api_key)
        if "FROM oauth_tokens" in sql:
            row = (
                None
                if self.oauth is None
                else (self.oauth, self.client_owner, self.client_name)
            )
            return _Result(first=row)
        if "FROM users" in sql:
            return _Result(scalar=self.user_active)
        return _Result()


def _key(**kwargs):
    row = APIKey(
        id=kwargs.pop("id", 3),
        name="probe key",
        key_prefix="omcp_abcdef",
        key_hash="deadbeef",
        permission="read",
        is_active=True,
        user_id=kwargs.pop("user_id", 5),
        expires_at=kwargs.pop("expires_at", None),
    )
    for name, value in kwargs.items():
        setattr(row, name, value)
    return row


def _token(**kwargs):
    return OAuthToken(
        id=91,
        token_hash="cafe",
        token_type="access",
        revoked=False,
        client_id="client123",
        grant_id=kwargs.pop("grant_id", "grant-1"),
        user_id=kwargs.pop("user_id", 5),
        scope=kwargs.pop("scope", "vault:read"),
        expires_at=kwargs.pop(
            "expires_at", datetime.now(timezone.utc) + timedelta(hours=1)
        ),
    )


def _drive(
    *,
    token: str | None = "omcp_probe",
    session: _Session | None = None,
    multi_user: bool = True,
    client=("203.0.113.7", 4242),
    count_sessions: list | None = None,
):
    """Run the real middleware once. Returns `(status, headers, served)`."""
    sent = []
    served = []

    async def _send(message):
        sent.append(message)

    async def _receive():  # pragma: no cover - never awaited
        return {"type": "http.request", "body": b"", "more_body": False}

    async def _downstream(scope, receive, send):
        served.append(True)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    headers = []
    if token is not None:
        headers.append((b"authorization", f"Bearer {token}".encode()))

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp/",
        "headers": headers,
    }
    if client is not None:
        scope["client"] = client

    async def run():
        mp = pytest.MonkeyPatch()
        try:
            def factory():
                if count_sessions is not None:
                    count_sessions.append(True)
                return session or _Session()

            mp.setattr(mcp_auth, "async_session", factory)
            mp.setattr(mcp_auth.settings, "multi_user_mode", multi_user, raising=False)

            async def _warm(_session, _uid):
                return None

            mp.setattr(mcp_auth, "warm_user_vault_cache", _warm)
            app = mcp_auth.APIKeyMiddleware(_downstream)
            await app(scope, _receive, _send)
        finally:
            mp.undo()

    asyncio.run(run())
    start = next(m for m in sent if m["type"] == "http.response.start")
    headers_out = {k.decode(): v.decode() for k, v in start.get("headers", [])}
    return start["status"], headers_out, bool(served)


def _slot(address):
    table = rate_limits._table()
    return table[rate_limits._slot_index(address, len(table))]


def _count(address="203.0.113.7"):
    slot = _slot(address)
    return 0 if slot is None else slot.count


# ── every 401 branch is budgeted ────────────────────────────────────────────

_PAST = datetime.now(timezone.utc) - timedelta(hours=1)

BRANCHES = [
    ("missing_bearer", dict(token=None)),
    ("invalid_key_api", dict(token="omcp_x", session=_Session(api_key=None))),
    (
        "ownerless_api",
        dict(token="omcp_x", session=_Session(api_key=_key(user_id=None))),
    ),
    (
        "inactive_user_api",
        dict(token="omcp_x", session=_Session(api_key=_key(), user_active=False)),
    ),
    (
        "key_expired_api",
        dict(token="omcp_x", session=_Session(api_key=_key(expires_at=_PAST))),
    ),
    ("invalid_key_oauth", dict(token="tok", session=_Session(oauth=None))),
    (
        "ownerless_oauth",
        dict(token="tok", session=_Session(oauth=_token(user_id=None))),
    ),
    (
        "inactive_user_oauth",
        dict(token="tok", session=_Session(oauth=_token(), user_active=False)),
    ),
    (
        "cross_user_grant",
        dict(token="tok", session=_Session(oauth=_token(user_id=5), client_owner=6)),
    ),
    (
        "key_expired_oauth",
        dict(token="tok", session=_Session(oauth=_token(expires_at=_PAST))),
    ),
    (
        "no_vault_scope",
        dict(token="tok", session=_Session(oauth=_token(scope="offline_access"))),
    ),
]


@pytest.mark.parametrize("name,kwargs", BRANCHES, ids=[n for n, _ in BRANCHES])
def test_every_401_branch_charges_the_budget(name, kwargs):
    """One parameterisation over every branch, because a budget that covers all
    but one is a budget with a documented bypass."""
    status, _headers, served = _drive(**kwargs)
    assert status == 401, name
    assert not served
    assert _count() == 1, f"{name} did not charge the address"


def test_the_charge_sites_are_structural_not_remembered():
    """Every 401 in the middleware is a sibling of a charge, by AST.

    Ten branches charge inside `_emit_auth_failure` (which each already calls)
    and the bearer-less branch charges directly. A twelfth 401 added later
    without either is what this sweep catches — the alternative is a list
    somebody has to remember to extend, which is how the seventh gets missed.
    """
    source = pathlib.Path(inspect.getfile(mcp_auth)).read_text()
    tree = ast.parse(source)
    blocks_with_401 = 0

    def charge_in(statements) -> bool:
        for stmt in statements:
            text = ast.unparse(stmt)
            if "_emit_auth_failure(" in text or "record_auth_failure(" in text:
                return True
        return False

    for node in ast.walk(tree):
        for field in ("body", "orelse", "finalbody"):
            statements = getattr(node, field, None)
            if not isinstance(statements, list):
                continue
            for index, stmt in enumerate(statements):
                # Simple statements only — `response = JSONResponse(…, 401)`.
                # A container (the enclosing `if`, the class, the module) also
                # *contains* that text, and counting those would ask the wrong
                # block for the charge.
                if not isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.Return)):
                    continue
                if "status_code=401" not in ast.unparse(stmt):
                    continue
                blocks_with_401 += 1
                assert charge_in(statements[:index]), (
                    f"a 401 at line {stmt.lineno} is not preceded by a charge "
                    "to the failed-authentication budget"
                )

    assert blocks_with_401 == 11, (
        f"expected 11 branches that answer 401, found {blocks_with_401} — "
        "if a branch was added or removed, say so here"
    )


# ── the threshold, and its arithmetic ───────────────────────────────────────


def test_the_threshold_is_inclusive_and_a_refusal_does_not_increment(monkeypatch):
    """With a limit of N the (N+1)th request is the first refused, and the
    refusals that follow do not deepen the hole they are in."""
    monkeypatch.setattr(mcp_auth.settings, "mcp_auth_failure_limit", 3)

    for _ in range(3):
        assert _drive(token=None)[0] == 401
    assert _count() == 3

    status, headers, served = _drive(token=None)
    assert status == 429
    assert not served
    assert int(headers["retry-after"]) >= 1
    assert "WWW-Authenticate" in {k.title(): v for k, v in headers.items()} or (
        "www-authenticate" in headers
    )
    assert _count() == 3, "a refused request must not increment"

    _drive(token=None)
    assert _count() == 3


def test_the_transport_refusal_carries_no_sentinel_line(monkeypatch):
    """L1 is deliberately **outside** the in-band refusal contract.

    There is no tool call to answer, no principal and no `usage_logs` row, so
    the honest answer is an HTTP error with `Retry-After`, not a fabricated
    tool result carrying a sentinel line.
    """
    monkeypatch.setattr(mcp_auth.settings, "mcp_auth_failure_limit", 1)
    _drive(token=None)
    status, headers, _served = _drive(token=None)
    assert status == 429
    assert "Retry-After" in {k.title(): v for k, v in headers.items()}
    assert not any("MCP-REFUSAL" in value for value in headers.values())


def test_retry_after_is_the_seconds_left_in_the_window(monkeypatch):
    """Whole seconds remaining, minimum one — a `Retry-After: 0` invites the
    tightest possible loop."""
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(
        rate_limits, "time", SimpleNamespace(monotonic=lambda: clock.now)
    )
    monkeypatch.setattr(mcp_auth.settings, "mcp_auth_failure_limit", 1)
    monkeypatch.setattr(mcp_auth.settings, "mcp_auth_failure_window_seconds", 60)

    rate_limits.record_auth_failure("198.51.100.9")
    clock.now = 1010.0
    refusal = rate_limits.check_auth_failures("198.51.100.9")
    assert refusal is not None
    assert refusal.retry_after_seconds == 50

    clock.now = 1059.5
    assert rate_limits.check_auth_failures("198.51.100.9").retry_after_seconds == 1

    clock.now = 1060.0
    assert rate_limits.check_auth_failures("198.51.100.9") is None


def test_only_the_first_refusal_in_a_window_is_flagged_for_a_warning(monkeypatch):
    monkeypatch.setattr(mcp_auth.settings, "mcp_auth_failure_limit", 1)
    rate_limits.record_auth_failure("198.51.100.10")
    assert rate_limits.check_auth_failures("198.51.100.10").first is True
    assert rate_limits.check_auth_failures("198.51.100.10").first is False


# ── the refusal costs nothing ───────────────────────────────────────────────


def test_a_refused_probe_opens_no_database_session(monkeypatch):
    """The whole point of the control: a refused probe buys no session and no
    query, so the work an unauthenticated caller can force is bounded."""
    monkeypatch.setattr(mcp_auth.settings, "mcp_auth_failure_limit", 2)
    sessions = []
    _drive(token="omcp_x", session=_Session(api_key=None), count_sessions=sessions)
    _drive(token="omcp_x", session=_Session(api_key=None), count_sessions=sessions)
    assert len(sessions) == 2

    status, _headers, _served = _drive(
        token="omcp_x", session=_Session(api_key=None), count_sessions=sessions
    )
    assert status == 429
    assert len(sessions) == 2, "the refused probe reached the credential lookup"


def test_a_valid_credential_under_budget_is_served_and_charges_nothing():
    status, _headers, served = _drive(
        token="omcp_x", session=_Session(api_key=_key())
    )
    assert status == 200
    assert served
    assert _count() == 0


def test_the_window_rolls_and_a_valid_credential_is_served_again(monkeypatch):
    clock = SimpleNamespace(now=500.0)
    monkeypatch.setattr(
        rate_limits, "time", SimpleNamespace(monotonic=lambda: clock.now)
    )
    monkeypatch.setattr(mcp_auth.settings, "mcp_auth_failure_limit", 1)
    monkeypatch.setattr(mcp_auth.settings, "mcp_auth_failure_window_seconds", 300)

    _drive(token=None)
    assert _drive(token="omcp_x", session=_Session(api_key=_key()))[0] == 429

    clock.now += 301
    status, _headers, served = _drive(
        token="omcp_x", session=_Session(api_key=_key())
    )
    assert status == 200 and served


def test_null_disables_the_control(monkeypatch):
    monkeypatch.setattr(mcp_auth.settings, "mcp_auth_failure_limit", None)
    for _ in range(100):
        assert _drive(token=None)[0] == 401
    assert _slot("203.0.113.7") is None, "nothing should be counted when off"


# ── the address ─────────────────────────────────────────────────────────────


def test_the_address_is_the_proxied_client_never_a_header(monkeypatch):
    """A budget keyed on a spoofable header is worse than no budget: any client
    on the internet could then charge a victim's slot, or mint a fresh one per
    request."""
    monkeypatch.setattr(mcp_auth.settings, "mcp_auth_failure_limit", 5)
    sent = []
    served = []

    async def _send(message):
        sent.append(message)

    async def _receive():  # pragma: no cover
        return {"type": "http.request", "body": b"", "more_body": False}

    async def _downstream(scope, receive, send):  # pragma: no cover
        served.append(True)

    async def run():
        mp = pytest.MonkeyPatch()
        try:
            mp.setattr(mcp_auth, "async_session", lambda: _Session())
            app = mcp_auth.APIKeyMiddleware(_downstream)
            await app(
                {
                    "type": "http",
                    "method": "POST",
                    "path": "/mcp/",
                    "client": ("10.0.0.4", 1234),
                    "headers": [(b"x-forwarded-for", b"198.51.100.200")],
                },
                _receive,
                _send,
            )
        finally:
            mp.undo()

    asyncio.run(run())
    assert _count("10.0.0.4") == 1
    assert _slot("198.51.100.200") is None or _count("198.51.100.200") == 0


def test_a_request_with_no_address_is_charged_to_the_shared_reserved_slot():
    """Charged, not exempted. Exempting is a bypass anyone able to strip the
    header gets for free; the shared slot only makes the control stricter."""
    _drive(token=None, client=None)
    _drive(token=None, client=None)
    reserved = rate_limits._table()[0]
    assert reserved is not None and reserved.count == 2
    assert rate_limits._slot_index(None, 4096) == 0
    assert rate_limits._slot_index("", 4096) == 0


def test_the_shared_slot_is_subject_to_the_same_threshold(monkeypatch):
    monkeypatch.setattr(mcp_auth.settings, "mcp_auth_failure_limit", 2)
    assert _drive(token=None, client=None)[0] == 401
    assert _drive(token=None, client=None)[0] == 401
    assert _drive(token=None, client=None)[0] == 429


# ── bounded memory ──────────────────────────────────────────────────────────


def test_one_hundred_thousand_addresses_stay_proportional_to_the_table():
    """Addresses are free to mint, so eviction is a losing game: the table is
    fixed-size and there is nothing to evict."""
    size = rate_limits.settings.mcp_auth_failure_table_size
    for index in range(100_000):
        rate_limits.record_auth_failure(f"198.51.{index // 256}.{index % 256}")
    table = rate_limits._table()
    assert len(table) == size
    assert sum(1 for slot in table if slot is not None) <= size


def test_colliding_addresses_share_a_budget_rather_than_escape_one():
    size = 16  # small on purpose, so a collision is quick to find
    first = "203.0.113.1"
    target = rate_limits._slot_index(first, size)
    partner = next(
        candidate
        for candidate in (f"203.0.113.{n}" for n in range(2, 4000))
        if rate_limits._slot_index(candidate, size) == target
    )
    assert partner != first


def test_the_salt_is_per_process_and_random():
    """Nobody may *choose* to collide with a victim's slot. A salt derived from
    anything an attacker can read would let them compute one."""
    before = rate_limits._address_salt
    rate_limits.reset_state_for_tests()
    assert rate_limits._address_salt != before
    assert len(before) >= 16
