"""#192 — every panel and CSRF refusal leaves a record, and nothing else moves.

Before this, `/admin`'s 403s were silent. `require_admin_panel`, the three
ownership guards, the duplicate ownership check in the JSON API's
`revoke_key`, the actor re-check inside the admin critical section, and
`verify_csrf` all refused a request and told nobody: an operator could not
answer "who tried to touch whose key", and the actor re-check — a real
authorization race being lost — left no trace at all.

Two invariants run through every test here.

**The response never changes.** Each guard raises the same `HTTPException`
with the same status and the same detail it always did; the record is added
beside it, never in place of it.

**`actor_user_id` is who acted, `user_id` is who the record is about** (design
D19). They are emitted as a pair on every surface where one account can act on
another's resource — including when they are equal, so a query for one
administrator's actions is complete rather than silently missing self-actions.
`admin_required` and `actor_revoked` carry no `user_id`, because neither names
a resource with an owner; absence is the honest rendering.
"""
from __future__ import annotations

import asyncio
import logging
import os

import pydantic_settings
import pytest

_orig_init = pydantic_settings.BaseSettings.__init__


def _no_env_file_init(self, *args, **kwargs):
    kwargs.setdefault("_env_file", None)
    _orig_init(self, *args, **kwargs)


pydantic_settings.BaseSettings.__init__ = _no_env_file_init
try:
    from fastapi import HTTPException
    from starlette.datastructures import Headers

    from src.api import routes as api_routes
    from src.auth.session import _SingleUserSentinel
    from src.control_panel import routes as panel
    from src.control_panel import users as users_mod
    from src.csrf import verify_csrf
    from src.models.db import APIKey, OAuthClient, OAuthToken, User
    from src.services import security_events
finally:
    pydantic_settings.BaseSettings.__init__ = _orig_init


# ── capture ─────────────────────────────────────────────────────────────────


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)

    def named(self, event: str) -> list[logging.LogRecord]:
        return [r for r in self.records if r.getMessage() == event]


@pytest.fixture
def captured():
    handler = _Capture()
    logger = security_events.logger
    logger.addHandler(handler)
    propagate, level = logger.propagate, logger.level
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    security_events.reset_state()
    try:
        with security_events.suppression_disabled():
            yield handler
    finally:
        logger.removeHandler(handler)
        logger.propagate = propagate
        logger.setLevel(level)
        security_events.reset_state()


def fields(record) -> dict:
    standard = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__)
    standard |= {"message", "asctime", "taskName"}
    return {k: v for k, v in record.__dict__.items() if k not in standard}


def only(handler, event: str) -> dict:
    records = handler.named(event)
    assert len(records) == 1, [r.getMessage() for r in handler.records]
    return fields(records[0])


# ── fakes ───────────────────────────────────────────────────────────────────


class _URL:
    def __init__(self, path):
        self.path = path


class _Client:
    def __init__(self, host):
        self.host = host


class _Request:
    """Enough `Request` for a guard: a URL, a method, a peer and a session."""

    def __init__(self, path="/admin/keys/4/revoke", method="POST", session=None):
        self.url = _URL(path)
        self.method = method
        self.client = _Client("198.51.100.9")
        self.session = {} if session is None else session
        self.query_params: dict = {}
        self.headers = Headers({})


def _user(uid: int, username: str, *, is_admin=False, is_active=True) -> User:
    u = User(
        username=username,
        password_hash="x",
        is_admin=is_admin,
        is_active=is_active,
        vault_path=None,
    )
    u.id = uid
    return u


def _key(owner_id: int, key_id: int = 4) -> APIKey:
    k = APIKey(
        name="k",
        key_hash="h",
        key_prefix="omcp_aaaaaaa",
        permission="read",
        user_id=owner_id,
    )
    k.id = key_id
    return k


# ── require_admin_panel ─────────────────────────────────────────────────────


def test_a_non_admin_is_refused_and_recorded_with_no_resource_owner(captured):
    actor = _user(5, "bea")
    request = _Request("/admin/settings", "GET")
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(panel.require_admin_panel(request, actor))

    # The response is exactly what it was.
    assert excinfo.value.status_code == 403
    assert excinfo.value.detail == "Admin required"

    record = only(captured, "panel_forbidden")
    assert record["reason"] == "admin_required"
    assert record["actor_user_id"] == 5
    assert record["actor_username"] == "bea"
    assert record["route"] == "/admin/settings"
    assert record["method"] == "GET"
    # No resource with an owner was named, so inventing the actor's own id
    # here would make "whose resource was this about" unanswerable.
    assert "user_id" not in record


def test_an_admin_passes_and_records_nothing(captured):
    admin = _user(1, "max", is_admin=True)
    assert asyncio.run(panel.require_admin_panel(_Request(), admin)) is admin
    assert captured.records == []


def test_the_single_user_sentinel_is_an_admin(captured):
    sentinel = _SingleUserSentinel()
    assert asyncio.run(panel.require_admin_panel(_Request(), sentinel)) is sentinel
    assert captured.records == []


# ── the three ownership guards ──────────────────────────────────────────────


def test_a_key_belonging_to_someone_else_is_refused_and_recorded(captured):
    actor = _user(5, "bea")
    request = _Request("/admin/keys/4/revoke", "POST")
    with pytest.raises(HTTPException) as excinfo:
        panel._assert_key_owner(_key(owner_id=9), actor, request=request)

    assert excinfo.value.status_code == 403
    assert excinfo.value.detail == "Not your key"

    record = only(captured, "panel_forbidden")
    assert record["reason"] == "not_your_key"
    assert record["actor_user_id"] == 5
    assert record["actor_username"] == "bea"
    # D19's pair: who acted, and whose key it was.
    assert record["user_id"] == 9
    assert record["route"] == "/admin/keys/4/revoke"
    assert record["method"] == "POST"


def test_a_missing_key_is_a_404_and_not_a_forbidden_record(captured):
    """The 404/403 split is unchanged: absence is not a permission answer."""
    with pytest.raises(HTTPException) as excinfo:
        panel._assert_key_owner(None, _user(5, "bea"), request=_Request())
    assert excinfo.value.status_code == 404
    assert captured.records == []


def test_the_owner_and_an_admin_both_pass_without_a_record(captured):
    owner = _user(9, "ada")
    admin = _user(1, "max", is_admin=True)
    key = _key(owner_id=9)
    assert panel._assert_key_owner(key, owner, request=_Request()) is key
    assert panel._assert_key_owner(key, admin, request=_Request()) is key
    assert captured.records == []


class _OwnerSession:
    """Returns one row for the guard's single SELECT."""

    def __init__(self, row):
        self.row = row

    async def execute(self, *_a, **_k):
        return _OwnerResult(self.row)


class _OwnerResult:
    def __init__(self, row):
        self.row = row

    def scalar_one_or_none(self):
        return self.row


def test_an_oauth_client_belonging_to_someone_else_is_recorded(captured):
    client = OAuthClient(
        client_id="c-1",
        client_secret_hash="h",
        client_name="Some app",
        redirect_uris=["https://x/cb"],
        scope="read",
        user_id=9,
    )
    request = _Request("/admin/oauth/c-1/delete", "POST")
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(
            panel._assert_oauth_client_owner(
                _OwnerSession(client), "c-1", _user(5, "bea"), request=request
            )
        )
    assert excinfo.value.status_code == 403
    assert excinfo.value.detail == "Not your client"

    record = only(captured, "panel_forbidden")
    assert record["reason"] == "not_your_client"
    assert record["actor_user_id"] == 5
    assert record["user_id"] == 9
    assert record["route"] == "/admin/oauth/c-1/delete"


def test_an_oauth_token_belonging_to_someone_else_is_recorded(captured):
    token = OAuthToken(
        token_hash="h",
        token_type="access",
        client_id="c-1",
        user_id=9,
        scope="read",
        grant_id="g-1",
    )
    token.id = 3
    request = _Request("/admin/oauth/token/3/revoke", "POST")
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(
            panel._assert_oauth_token_owner(
                _OwnerSession(token), 3, _user(5, "bea"), request=request
            )
        )
    assert excinfo.value.status_code == 403
    assert excinfo.value.detail == "Not your token"

    record = only(captured, "panel_forbidden")
    assert record["reason"] == "not_your_token"
    assert record["actor_user_id"] == 5
    assert record["user_id"] == 9


def test_a_guard_without_a_request_still_refuses_and_still_records(captured):
    """A logging call may not change a decision, and may not raise either.

    `request` is keyword-only and defaulted precisely so that a caller which
    has none cannot accidentally change who is allowed through. The record is
    poorer — no route, no method — and that is the right trade.
    """
    with pytest.raises(HTTPException) as excinfo:
        panel._assert_key_owner(_key(owner_id=9), _user(5, "bea"))
    assert excinfo.value.status_code == 403
    record = only(captured, "panel_forbidden")
    assert record["reason"] == "not_your_key"
    assert "route" not in record and "method" not in record


# ── the REST API's inline duplicate (D12) ───────────────────────────────────


class _ApiSession:
    def __init__(self, row):
        self.row = row
        self.committed = False

    async def execute(self, *_a, **_k):
        return _OwnerResult(self.row)

    async def commit(self):
        self.committed = True


def test_the_json_api_revoke_records_its_own_ownership_refusal(captured):
    """`src/api/routes.py` carries an inline copy of the predicate.

    It was missed by the first draft of this change and is exactly the kind of
    second copy that silently stops being covered.
    """
    session = _ApiSession(_key(owner_id=9))
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(
            api_routes.revoke_key(
                key_id=4,
                request=_Request("/api/keys/4", "DELETE"),
                session=session,
                user=_user(5, "bea"),
            )
        )
    assert excinfo.value.status_code == 403
    assert excinfo.value.detail == "Not your key"
    assert session.committed is False

    record = only(captured, "panel_forbidden")
    assert record["reason"] == "not_your_key"
    assert record["actor_user_id"] == 5
    assert record["user_id"] == 9
    assert record["route"] == "/api/keys/4"
    assert record["method"] == "DELETE"


def test_the_json_api_revoke_still_revokes_for_the_owner(captured):
    key = _key(owner_id=5)
    session = _ApiSession(key)
    result = asyncio.run(
        api_routes.revoke_key(
            key_id=4,
            request=_Request("/api/keys/4", "DELETE"),
            session=session,
            user=_user(5, "bea"),
        )
    )
    assert result == {"status": "revoked"}
    assert key.is_active is False
    assert captured.records == []


# ── the actor re-check inside the admin critical section ────────────────────


class _ActorRow:
    def __init__(self, is_admin, is_active):
        self.is_admin = is_admin
        self.is_active = is_active


class _Result:
    """`rowcount` is read by the session revocation these handlers now issue
    (#198): `revoke_user_sessions` takes it off the `UPDATE user_sessions`
    result. Zero — this fake holds no session rows, and these tests are about
    what gets *recorded*, not about the registry."""

    rowcount = 0

    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def one_or_none(self):
        return self._value

    def scalar(self):
        return self._value


class _UsersSession:
    def __init__(self, target, *, actor_after_lock, remaining_admins=1):
        self._target = target
        self._actor_after_lock = actor_after_lock
        self._remaining_admins = remaining_admins
        self.committed = False
        self.rolled_back = False

    async def execute(self, stmt, *_a, **_k):
        sql = str(stmt)
        if "pg_advisory_xact_lock" in sql:
            return _Result(None)
        if sql.startswith("SELECT users.is_admin, users.is_active"):
            return _Result(self._actor_after_lock)
        if "count(" in sql.lower():
            return _Result(self._remaining_admins)
        return _Result(self._target)

    async def delete(self, obj):
        pass

    async def rollback(self):
        self.rolled_back = True

    async def commit(self):
        self.committed = True


DEMOTED = _ActorRow(is_admin=False, is_active=True)


def _target() -> User:
    target = _user(2, "bob")
    # `session_version` has a server-side default, so a row built in memory
    # carries None until a flush. The reset increments it.
    target.session_version = 0
    return target


def test_edit_records_the_lost_race_inside_the_critical_section(captured):
    """A demotion that committed while this request queued for the lock.

    Nothing else in the server says this happened — the operator sees a flash
    message and the log used to say nothing at all — so the refusal *is* the
    record.
    """
    session = _UsersSession(_target(), actor_after_lock=DEMOTED)
    asyncio.run(
        users_mod.edit_user_submit(
            user_id=2,
            request=_Request("/admin/users/2/edit", "POST"),
            vault_path="",
            vault_path_custom="",
            is_admin=None,
            is_active="on",
            session=session,
            user=_user(1, "max", is_admin=True),
        )
    )
    assert session.committed is False
    record = only(captured, "panel_forbidden")
    assert record["reason"] == "actor_revoked"
    assert record["actor_user_id"] == 1
    assert record["actor_username"] == "max"
    # The actor's own standing was refused, not a named resource's ownership.
    assert "user_id" not in record


def test_delete_records_the_lost_race(captured):
    session = _UsersSession(_target(), actor_after_lock=DEMOTED)
    asyncio.run(
        users_mod.delete_user(
            user_id=2,
            request=_Request("/admin/users/2/delete", "POST"),
            session=session,
            user=_user(1, "max", is_admin=True),
        )
    )
    assert session.committed is False
    assert only(captured, "panel_forbidden")["reason"] == "actor_revoked"


def test_password_reset_records_the_lost_race(captured):
    session = _UsersSession(_target(), actor_after_lock=DEMOTED)
    asyncio.run(
        users_mod.reset_password(
            user_id=2,
            request=_Request("/admin/users/2/reset-password", "POST"),
            new_password="a-long-enough-password",
            session=session,
            user=_user(1, "max", is_admin=True),
        )
    )
    assert session.committed is False
    assert only(captured, "panel_forbidden")["reason"] == "actor_revoked"
    assert captured.named("panel_password_reset") == []


def test_create_user_has_no_such_refusal_to_record(captured):
    """Residual R7, pinned so nobody "fixes" it here by accident.

    `create_user` takes neither the advisory lock nor the actor re-check, so
    there is no refusal at that site to log. That is a pre-existing
    authorization race and belongs to a change that owns this critical
    section; adding a record for a check that does not exist would be a claim
    the code cannot back.
    """
    import inspect

    source = inspect.getsource(users_mod.create_user)
    assert "_actor_still_privileged" not in source
    assert "_lock_admin_guard" not in source


# ── the password reset's own success record (D17) ───────────────────────────


def test_a_completed_password_reset_is_recorded_after_its_commit(captured):
    target = _target()
    session = _UsersSession(target, actor_after_lock=_ActorRow(True, True))
    asyncio.run(
        users_mod.reset_password(
            user_id=2,
            request=_Request("/admin/users/2/reset-password", "POST"),
            new_password="a-long-enough-password",
            session=session,
            user=_user(1, "max", is_admin=True),
        )
    )
    assert session.committed is True
    record = only(captured, "panel_password_reset")
    assert record["actor_user_id"] == 1
    assert record["user_id"] == 2
    assert record["username"] == "bob"
    assert record["client_ip"] == "198.51.100.9"
    assert record["route"] == "/admin/users/2/reset-password"
    # The new password is not a field and has no field to ride in.
    assert "a-long-enough-password" not in repr(record)


def test_a_failed_commit_leaves_no_password_reset_record(captured):
    """D17: a success record may not outlive the transaction that made it true."""

    class _Failing(_UsersSession):
        async def commit(self):
            raise RuntimeError("commit failed")

    session = _Failing(_target(), actor_after_lock=_ActorRow(True, True))
    with pytest.raises(RuntimeError):
        asyncio.run(
            users_mod.reset_password(
                user_id=2,
                request=_Request(),
                new_password="a-long-enough-password",
                session=session,
                user=_user(1, "max", is_admin=True),
            )
        )
    assert captured.named("panel_password_reset") == []


# ── the panel's grant revocation (D10 / D19) ────────────────────────────────


class _RevokeSession:
    def __init__(self, token, revoked=2):
        self.token = token
        self.revoked = revoked
        self.committed = False

    async def execute(self, stmt, *_a, **_k):
        sql = str(stmt)
        if "pg_advisory_xact_lock" in sql:
            return _Result(None)
        if sql.strip().upper().startswith("UPDATE"):
            return _RowcountResult(self.revoked)
        return _OwnerResult(self.token)

    async def commit(self):
        self.committed = True


class _RowcountResult:
    def __init__(self, rowcount):
        self.rowcount = rowcount


def _grant_token() -> OAuthToken:
    token = OAuthToken(
        token_hash="h",
        token_type="access",
        client_id="c-1",
        user_id=9,
        scope="read",
        grant_id="g-1",
    )
    token.id = 3
    return token


def test_an_admin_revoking_another_users_grant_records_both_identities(captured):
    """Round 3's ambiguity: one `user_id` could not say whose it was.

    Emitted by the HTTP caller after *its own* commit, never inside
    `revoke_grant_family` — that helper has no request, no address, no session
    user, and does not commit, so a record from there would be a claim about a
    transaction that may still roll back (D10).
    """
    session = _RevokeSession(_grant_token(), revoked=2)
    asyncio.run(
        panel.revoke_oauth_token(
            token_id=3,
            request=_Request("/admin/oauth/token/3/revoke", "POST"),
            session=session,
            user=_user(1, "max", is_admin=True),
        )
    )
    assert session.committed is True
    record = only(captured, "oauth_grant_revoked")
    assert record["actor_user_id"] == 1, "the administrator who clicked"
    assert record["user_id"] == 9, "the grant's owner"
    assert record["grant_id"] == "g-1"
    assert record["client_id"] == "c-1"
    # The rowcount `revoke_grant_family` returns, which every caller used to
    # discard — "already fully revoked" and "revoked two tokens" are different
    # answers to the same click.
    assert record["count"] == 2
    assert record["route"] == "/admin/oauth/token/3/revoke"


def test_the_grant_revocation_is_recorded_at_info(captured):
    session = _RevokeSession(_grant_token())
    asyncio.run(
        panel.revoke_oauth_token(
            token_id=3, request=_Request(), session=session,
            user=_user(1, "max", is_admin=True),
        )
    )
    assert captured.named("oauth_grant_revoked")[0].levelno == logging.INFO


def test_a_failed_commit_leaves_no_revocation_record(captured):
    class _Failing(_RevokeSession):
        async def commit(self):
            raise RuntimeError("commit failed")

    with pytest.raises(RuntimeError):
        asyncio.run(
            panel.revoke_oauth_token(
                token_id=3, request=_Request(),
                session=_Failing(_grant_token()),
                user=_user(1, "max", is_admin=True),
            )
        )
    assert captured.named("oauth_grant_revoked") == []


# ── CSRF ────────────────────────────────────────────────────────────────────


class _CsrfRequest:
    def __init__(self, path="/admin/keys", method="POST", session=None):
        self.url = _URL(path)
        self.method = method
        self.client = _Client("198.51.100.9")
        self.headers = Headers({})
        self.session = {} if session is None else session

    async def form(self):
        return {}


def test_a_csrf_failure_keeps_its_403_and_gains_a_record(captured):
    request = _CsrfRequest(session={"user_id": 7})
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(verify_csrf(request))

    # Unchanged, both of them.
    assert excinfo.value.status_code == 403
    assert excinfo.value.detail == "CSRF validation failed"

    record = only(captured, "csrf_refused")
    assert record["route"] == "/admin/keys"
    assert record["method"] == "POST"
    assert record["user_id"] == 7
    assert record["client_ip"] == "198.51.100.9"


def test_a_csrf_failure_with_no_session_user_carries_none(captured):
    with pytest.raises(HTTPException):
        asyncio.run(verify_csrf(_CsrfRequest()))
    record = only(captured, "csrf_refused")
    assert "user_id" not in record
    assert record["route"] == "/admin/keys"


def test_a_safe_method_is_neither_refused_nor_recorded(captured):
    assert asyncio.run(verify_csrf(_CsrfRequest(method="GET"))) is None
    assert captured.records == []


def test_the_csrf_record_never_carries_the_submitted_token(captured):
    """A CSRF token is credential material and has no field to ride in."""
    canary = "Z9x" + "q" * 29

    class _WithToken(_CsrfRequest):
        def __init__(self):
            super().__init__()
            self.headers = Headers({"x-csrf-token": canary})

    with pytest.raises(HTTPException):
        asyncio.run(verify_csrf(_WithToken()))
    assert canary not in repr([fields(r) for r in captured.records])


def test_the_csrf_refusal_is_bounded_by_the_suppressor():
    """`verify_csrf` is a router-wide dependency on a route no limit covers."""
    handler = _Capture()
    logger = security_events.logger
    logger.addHandler(handler)
    propagate, level = logger.propagate, logger.level
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    security_events.reset_state()
    try:
        for _ in range(security_events.MAX_EVENTS_PER_WINDOW + 5):
            with pytest.raises(HTTPException):
                asyncio.run(verify_csrf(_CsrfRequest()))
        assert (
            len(handler.named("csrf_refused"))
            == security_events.MAX_EVENTS_PER_WINDOW
        )
    finally:
        logger.removeHandler(handler)
        logger.propagate = propagate
        logger.setLevel(level)
        security_events.reset_state()


# ── the panel's migrated operational failures ───────────────────────────────


def test_the_on_demand_index_and_embed_failures_go_through_the_emitter():
    """Both are caller-triggerable — an operator can press Reindex Now.

    A bare `logger.error` reaches the sink whatever the suppressor says, which
    makes it an unbounded channel beside the bounded one. The assertion is on
    the source rather than by driving a full reindex: the handler is inside a
    background task that needs the indexer, a lock and a vault.
    """
    import inspect

    source = inspect.getsource(panel._reindex_background)
    assert "panel_ondemand_index_failed" in source
    assert "panel_ondemand_embed_failed" in source
    assert "logger.error" not in source


def test_the_health_strip_failure_goes_through_the_emitter():
    import inspect

    source = inspect.getsource(panel._health_strip_or_degraded)
    assert "panel_health_strip_failed" in source
    # Both failures — the read and the rollback that follows it — are named by
    # D18's table, so neither is left on the bare logger.
    assert "logger." not in source
