"""#198 — an administrative action that ends an account's access ends its sessions.

`users.session_version` was the only server-side invalidator in the tree and
exactly one handler incremented it. Everything else an administrator could do
to an account — deactivating it from the edit form, soft-deleting it — left
every live browser session of that account signed in until its cookie's own
seven-day signature aged out. The registry (#198) makes a session a row that
can be revoked; this module is about the four places that must now revoke it,
the two that must not, and the purge that decides how long the evidence lives.

Four properties are asserted here rather than assumed, because each one is a
way the fix could be present and not work:

* **The revocation rides the handler's transaction.** These handlers hold the
  account-guard advisory lock, and the documented rule for that critical
  section is that nothing commits between the lock and the protected write. The
  fake below therefore models a rollback honestly — it restores the revocation
  state it snapshotted — so "a rolled-back edit revokes nothing" is observed
  rather than asserted about a mock.
* **Every refusal returns before the revocation.** The last-administrator
  guard, the self-target refusal and the `_actor_still_privileged` re-check all
  precede any write, and a revocation that leaked past one of them would sign a
  user out of an account the panel had just declined to touch.
* **The permanent delete adds nothing.** `user_sessions` carries
  `ON DELETE CASCADE` and `User.sessions` declares `passive_deletes=True`, so
  the *database* removes those rows. Exercised through the real handler, with
  the assertion that the ORM issued no statement against `user_sessions` at
  all — a per-row delete here would mean the schema's cascade is dead code and
  nobody would notice until the ORM path was removed.
* **The purge measures from the later of expiry and revocation.** An
  administrative reset revokes every unrevoked row of a user *including
  already-expired ones*, so the OAuth half's expiry-only predicate would delete
  the record of a revocation minutes after an operator performed it.
"""
from __future__ import annotations

import asyncio
import datetime
import logging

import pydantic_settings
import pytest

_orig_init = pydantic_settings.BaseSettings.__init__


def _no_env_file_init(self, *args, **kwargs):
    kwargs.setdefault("_env_file", None)
    _orig_init(self, *args, **kwargs)


pydantic_settings.BaseSettings.__init__ = _no_env_file_init
try:
    from sqlalchemy import and_, or_
    from sqlalchemy.sql import operators
    from sqlalchemy.sql.dml import Delete, Update
    from sqlalchemy.sql.elements import (
        BinaryExpression,
        BindParameter,
        BooleanClauseList,
        Grouping,
        Null,
    )

    from src.config import settings
    from src.control_panel import users as users_mod
    from src.control_panel.flash import FLASH_SESSION_KEY
    from src.models.db import User, UserSession
    from src.services import indexer, security_events
finally:
    pydantic_settings.BaseSettings.__init__ = _orig_init

from tests.session_helpers import session_row, utcnow

UTC = datetime.timezone.utc


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
    """Records emitted through the catalogue, with the suppressor out of the
    way and **strict mode on**: a field this change's events do not declare is
    a raise here rather than a silently dropped attribute."""
    handler = _Capture()
    logger = security_events.logger
    logger.addHandler(handler)
    propagate, level = logger.propagate, logger.level
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    security_events.reset_state()
    try:
        with security_events.suppression_disabled():
            with security_events.strict_fields():
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


# ── the fake session ────────────────────────────────────────────────────────


class _Result:
    def __init__(self, value=None, rows=None, rowcount: int = 0):
        self._value = value
        self._rows = list(rows or [])
        self.rowcount = rowcount

    def scalar_one_or_none(self):
        return self._value

    def one_or_none(self):
        return self._value

    def scalar(self):
        return self._value

    def all(self):
        return self._rows


class _ActorRow:
    def __init__(self, is_admin=True, is_active=True):
        self.is_admin = is_admin
        self.is_active = is_active


class _AdminSession:
    """An `AsyncSession` double that *interprets* the revocation.

    The registry half is real: session rows live in a list, the `UPDATE` the
    handler issues is applied to them with its `revoked_at IS NULL` predicate
    honoured, and `rollback()` restores the snapshot taken when this session
    was opened. A fake that canned a rowcount could not tell "revoked" from
    "issued a statement that matched nothing", and could not observe that a
    second revocation leaves the first timestamp alone.

    The `users` half stays canned — the target row, the actor re-read and the
    remaining-admin count — because those are what the surrounding guards are
    made of and the modules that own them (#69, #90) already interpret them.
    """

    def __init__(
        self,
        target,
        *,
        sessions=(),
        remaining_admins: int = 1,
        actor_after_lock=None,
        peers=(),
        commit_error: Exception | None = None,
    ):
        self._target = target
        self.sessions = list(sessions)
        self._remaining_admins = remaining_admins
        self._actor_after_lock = actor_after_lock
        self._peers = list(peers)
        self._commit_error = commit_error
        self._snapshot = {row.id: row.revoked_at for row in self.sessions}
        #: A monotonic stand-in for `now()`, so two revocations in the same
        #: microsecond are still distinguishable — which is the whole point of
        #: "a re-revocation keeps the first time".
        self._clock = utcnow()
        self.committed = 0
        self.rolled_back = 0
        self.deleted = None
        self.added: list = []
        self.statements: list[str] = []
        self.lock_keys: list[object] = []

    # -- clock ------------------------------------------------------------

    def _now(self) -> datetime.datetime:
        self._clock += datetime.timedelta(seconds=1)
        return self._clock

    # -- session surface --------------------------------------------------

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        self.statements.append(sql)

        if "pg_advisory_xact_lock" in sql:
            assert params, "the advisory lock was issued without a key"
            self.lock_keys.append(params.get("key"))
            return _Result()

        if isinstance(stmt, Update):
            if stmt.table.name == "user_sessions":
                return self._revoke(stmt, sql)
            # `usage_logs.key_id = NULL` on the permanent-delete path.
            return _Result(rowcount=0)

        if sql.startswith("SELECT users.is_admin, users.is_active"):
            if self._actor_after_lock is False:  # the actor's row is gone
                return _Result(None)
            return _Result(
                self._actor_after_lock
                if self._actor_after_lock is not None
                else _ActorRow()
            )
        if sql.startswith("SELECT users.id, users.username, users.vault_path"):
            return _Result(rows=self._peers)
        if "count(" in sql.lower():
            return _Result(self._remaining_admins)
        return _Result(self._target)

    def _revoke(self, stmt, sql: str) -> _Result:
        bound = dict(stmt.compile().params)
        rows = [r for r in self.sessions if r.user_id == bound.get("user_id_1")]
        # `revoked_at IS NULL` compiles to a SQL literal rather than a bind
        # parameter, so it is read off the rendered statement. Dropping it
        # would let a second revocation rewrite a historical revocation time.
        if "user_sessions.revoked_at IS NULL" in sql:
            rows = [r for r in rows if r.revoked_at is None]
        stamp = self._now()
        for row in rows:
            row.revoked_at = stamp
        return _Result(rowcount=len(rows))

    def add(self, obj):
        self.added.append(obj)

    async def delete(self, obj):
        """`session.delete(user)` — and the database's `ON DELETE CASCADE`.

        The cascade is modelled here, in the *database's* half of the fake,
        precisely because the handler must contribute nothing to it.
        """
        self.deleted = obj
        self.sessions = [r for r in self.sessions if r.user_id != obj.id]

    async def commit(self):
        if self._commit_error is not None:
            raise self._commit_error
        self.committed += 1
        self._snapshot = {row.id: row.revoked_at for row in self.sessions}

    async def rollback(self):
        self.rolled_back += 1
        for row in self.sessions:
            if row.id in self._snapshot:
                row.revoked_at = self._snapshot[row.id]

    # -- assertions -------------------------------------------------------

    @property
    def live(self) -> list[UserSession]:
        return [r for r in self.sessions if r.revoked_at is None]

    @property
    def revoked(self) -> list[UserSession]:
        return [r for r in self.sessions if r.revoked_at is not None]

    def touched_the_registry(self) -> bool:
        return any("user_sessions" in sql for sql in self.statements)


class _Req:
    def __init__(self, query_params: dict | None = None, path="/admin/users/2/edit"):
        self.session: dict = {}
        self.query_params = query_params if query_params is not None else {}
        self.url = type("_URL", (), {"path": path})()
        self.client = type("_Client", (), {"host": "198.51.100.9"})()
        self.method = "POST"
        self.headers: dict = {}


def _user(user_id=1, username="max", **overrides) -> User:
    u = User(
        username=username,
        password_hash="x",
        is_admin=True,
        is_active=True,
        vault_path=None,
    )
    u.id = user_id
    u.session_version = 1
    for key, value in overrides.items():
        setattr(u, key, value)
    return u


def _rows_for(user_id: int, count: int = 2) -> list[UserSession]:
    return [
        session_row(user_id, sid=f"sid-{user_id}-{n}") for n in range(count)
    ]


def _flash(request: _Req) -> tuple[str | None, str]:
    entry = request.session.get(FLASH_SESSION_KEY)
    if not entry:
        return None, "ok"
    return entry["message"], entry["kind"]


def _is_refusal(request: _Req) -> bool:
    message, kind = _flash(request)
    return message is not None and kind == "err"


# ── drivers ─────────────────────────────────────────────────────────────────


def _reset(actor, target, session, *, new_password="a-long-enough-password"):
    request = _Req(path="/admin/users/2/reset-password")
    response = asyncio.run(
        users_mod.reset_password(
            user_id=target.id,
            request=request,
            new_password=new_password,
            session=session,
            user=actor,
        )
    )
    return response, request


def _edit(actor, target, session, *, is_admin="on", is_active="on", vault_path=""):
    request = _Req()
    response = asyncio.run(
        users_mod.edit_user_submit(
            user_id=target.id,
            request=request,
            vault_path=vault_path,
            vault_path_custom="",
            is_admin=is_admin,
            is_active=is_active,
            session=session,
            user=actor,
        )
    )
    return response, request


def _delete(actor, target, session, *, permanent=False):
    request = _Req(
        {"permanent": "true"} if permanent else {}, path="/admin/users/2/delete"
    )
    response = asyncio.run(
        users_mod.delete_user(
            user_id=target.id,
            request=request,
            session=session,
            user=actor,
        )
    )
    return response, request


def _create(actor, session, *, username="bob", initial_password="a-long-password"):
    request = _Req(path="/admin/users/create")
    response = asyncio.run(
        users_mod.create_user(
            request=request,
            username=username,
            initial_password=initial_password,
            session=session,
            user=actor,
        )
    )
    return response, request


# ── the administrator password reset ────────────────────────────────────────


def test_a_password_reset_revokes_the_targets_sessions():
    actor, target = _user(), _user(2, "bob", is_admin=False)
    session = _AdminSession(target, sessions=_rows_for(2))
    _reset(actor, target, session)

    assert session.committed == 1
    assert session.live == [], "a reset left a live session behind"
    assert target.session_version == 2, "the account-wide switch still fires"


def test_a_password_reset_leaves_the_actors_own_sessions_alone():
    """The administrator stays signed in on the page they did it from."""
    actor, target = _user(), _user(2, "bob", is_admin=False)
    mine, theirs = _rows_for(1, 1), _rows_for(2, 1)
    session = _AdminSession(target, sessions=mine + theirs)
    _reset(actor, target, session)

    assert mine[0].revoked_at is None
    assert theirs[0].revoked_at is not None


def test_the_reset_revocation_rides_the_transaction_and_commits_once():
    """The advisory lock is transaction-scoped: a commit between it and the
    protected write would release it and un-do the guard's atomicity."""
    actor, target = _user(), _user(2, "bob", is_admin=False)
    session = _AdminSession(target, sessions=_rows_for(2))
    _reset(actor, target, session)

    lock_at = next(
        i for i, s in enumerate(session.statements) if "pg_advisory_xact_lock" in s
    )
    revoke_at = next(
        i for i, s in enumerate(session.statements) if s.startswith("UPDATE user_sessions")
    )
    assert lock_at < revoke_at
    assert session.committed == 1, "the write and the revocation are one commit"


def test_re_revoking_keeps_the_first_revocation_time():
    """`WHERE revoked_at IS NULL` — a second reset must not rewrite the record
    of the first one."""
    actor, target = _user(), _user(2, "bob", is_admin=False)
    rows = _rows_for(2, 1)
    session = _AdminSession(target, sessions=rows)
    _reset(actor, target, session)
    first = rows[0].revoked_at
    assert first is not None
    _reset(actor, target, session)
    assert rows[0].revoked_at == first


def test_the_reset_records_the_revocation_after_the_commit(captured):
    actor, target = _user(), _user(2, "bob", is_admin=False)
    session = _AdminSession(target, sessions=_rows_for(2, 3))
    _reset(actor, target, session)

    record = only(captured, "panel_sessions_revoked")
    assert record["reason"] == "admin_password_reset"
    assert record["user_id"] == 2
    assert record["count"] == 3
    # Neither the new password nor any session identifier has a field to ride
    # in, and none appears anywhere in the record.
    assert "a-long-enough-password" not in repr(record)
    assert all("sid-" not in str(value) for value in record.values())


def test_a_reset_whose_commit_fails_records_nothing(captured):
    actor, target = _user(), _user(2, "bob", is_admin=False)
    session = _AdminSession(
        target, sessions=_rows_for(2), commit_error=RuntimeError("commit failed")
    )
    with pytest.raises(RuntimeError):
        _reset(actor, target, session)
    assert captured.named("panel_sessions_revoked") == []


def test_the_reset_applies_the_shared_password_policy():
    """Eleven characters was accepted here yesterday; the owner of the account
    could not have set it themselves (#197)."""
    actor, target = _user(), _user(2, "bob", is_admin=False)
    session = _AdminSession(target, sessions=_rows_for(2))
    _, request = _reset(actor, target, session, new_password="short-one!")

    assert _is_refusal(request)
    assert "12 characters" in _flash(request)[0]
    assert session.committed == 0
    assert session.live, "a refused reset revoked a session"


def test_the_reset_refuses_a_nul_byte_instead_of_raising():
    """`hash_password` raises `ValueError` on an embedded NUL — deliberately,
    passlib's rule — so without the validator this was a 500."""
    actor, target = _user(), _user(2, "bob", is_admin=False)
    session = _AdminSession(target, sessions=_rows_for(2))
    _, request = _reset(actor, target, session, new_password="abcdefghijkl\x00mn")

    assert _is_refusal(request)
    assert "NUL" in _flash(request)[0]
    assert session.committed == 0


def test_a_password_refusal_never_echoes_the_password():
    actor, target = _user(), _user(2, "bob", is_admin=False)
    session = _AdminSession(target, sessions=_rows_for(2))
    _, request = _reset(actor, target, session, new_password="hunter2")
    assert "hunter2" not in _flash(request)[0]


# ── the edit form ───────────────────────────────────────────────────────────


def test_deactivating_a_user_revokes_their_sessions():
    actor, target = _user(), _user(2, "bob", is_admin=False)
    rows = _rows_for(2)
    session = _AdminSession(target, sessions=rows)
    _edit(actor, target, session, is_admin=None, is_active=None)

    assert target.is_active is False
    assert session.committed == 1
    assert session.live == []


def test_a_vault_path_edit_revokes_nothing():
    """A live session of an account that is still active is a session nobody
    ended. The registry must not be touched at all."""
    actor, target = _user(), _user(2, "bob", is_admin=False)
    rows = _rows_for(2)
    session = _AdminSession(target, sessions=rows)
    _edit(actor, target, session, is_admin=None, is_active="on")

    assert target.is_active is True
    assert session.committed == 1
    assert session.touched_the_registry() is False
    assert session.live == rows


def test_editing_an_already_inactive_user_revokes_nothing():
    """Only the *transition* revokes. A save that leaves an inactive account
    inactive has ended nothing that was not already ended."""
    actor = _user()
    target = _user(2, "bob", is_admin=False, is_active=False)
    session = _AdminSession(target, sessions=_rows_for(2))
    _edit(actor, target, session, is_admin=None, is_active=None)

    assert session.touched_the_registry() is False


def test_promoting_a_user_revokes_nothing():
    actor, target = _user(), _user(2, "bob", is_admin=False)
    session = _AdminSession(target, sessions=_rows_for(2))
    _edit(actor, target, session, is_admin="on", is_active="on")

    assert target.is_admin is True
    assert session.touched_the_registry() is False


def test_the_deactivation_records_its_reason(captured):
    actor, target = _user(), _user(2, "bob", is_admin=False)
    session = _AdminSession(target, sessions=_rows_for(2))
    _edit(actor, target, session, is_admin=None, is_active=None)

    record = only(captured, "panel_sessions_revoked")
    assert record["reason"] == "user_deactivated"
    assert record["user_id"] == 2
    assert record["count"] == 2


def test_a_rolled_back_deactivation_revokes_nothing(captured):
    """The flag write and the revocation land or roll back together — an
    `IntegrityError` on the commit must not leave a user signed out of an
    account that is still active."""
    from sqlalchemy.exc import IntegrityError

    actor, target = _user(), _user(2, "bob", is_admin=False)
    rows = _rows_for(2)
    session = _AdminSession(
        target,
        sessions=rows,
        commit_error=IntegrityError("stmt", {}, Exception("boom")),
    )
    _, request = _edit(actor, target, session, is_admin=None, is_active=None)

    assert session.rolled_back == 1
    assert session.live == rows, "a rolled-back transaction revoked a session"
    assert _is_refusal(request)
    assert captured.named("panel_sessions_revoked") == []


# ── the deletes ─────────────────────────────────────────────────────────────


def test_a_soft_delete_revokes_the_targets_sessions():
    actor, target = _user(), _user(2, "bob", is_admin=False)
    session = _AdminSession(target, sessions=_rows_for(2))
    _delete(actor, target, session)

    assert target.is_active is False
    assert session.committed == 1
    assert session.live == []


def test_the_soft_delete_records_its_reason(captured):
    actor, target = _user(), _user(2, "bob", is_admin=False)
    session = _AdminSession(target, sessions=_rows_for(2))
    _delete(actor, target, session)

    record = only(captured, "panel_sessions_revoked")
    assert record["reason"] == "user_deleted"
    assert record["user_id"] == 2


def test_a_permanent_delete_leaves_no_rows_and_issues_no_registry_statement():
    """Through the **real handler**. The rows go because the database's
    `ON DELETE CASCADE` takes them; the handler contributes nothing, and
    `passive_deletes=True` is what keeps the ORM from loading and deleting
    them one at a time (which would make the schema's cascade dead code)."""
    actor, target = _user(), _user(2, "bob", is_admin=False)
    session = _AdminSession(target, sessions=_rows_for(2, 3))
    _delete(actor, target, session, permanent=True)

    assert session.deleted is target
    assert session.sessions == [], "session rows survived a permanent delete"
    assert session.touched_the_registry() is False, (
        "the handler issued its own statement against user_sessions — the "
        "cascade is then not the mechanism that fires"
    )


def test_a_permanent_delete_records_no_revocation(captured):
    actor, target = _user(), _user(2, "bob", is_admin=False)
    session = _AdminSession(target, sessions=_rows_for(2))
    _delete(actor, target, session, permanent=True)
    assert captured.named("panel_sessions_revoked") == []


def test_the_relationship_declares_passive_deletes_and_a_delete_cascade():
    """The configuration the test above depends on, pinned where a change to
    it is a failure rather than a silently different mechanism."""
    relationship = User.__mapper__.relationships["sessions"]
    assert relationship.passive_deletes is True
    assert "delete" in relationship.cascade


# ── every refusal returns before the revocation ─────────────────────────────


def test_the_last_admin_guard_revokes_nothing():
    actor = _user()
    target = _user(2, "bob")
    rows = _rows_for(2)
    session = _AdminSession(target, sessions=rows, remaining_admins=0)
    _, request = _edit(actor, target, session, is_admin=None, is_active=None)

    assert _is_refusal(request)
    assert target.is_active is True
    assert session.touched_the_registry() is False
    assert session.live == rows


def test_the_self_target_delete_refusal_revokes_nothing():
    me = _user()
    rows = _rows_for(1)
    session = _AdminSession(me, sessions=rows, remaining_admins=3)
    _, request = _delete(me, me, session)

    assert _is_refusal(request)
    assert me.is_active is True
    assert session.touched_the_registry() is False
    assert session.live == rows


def test_a_demoted_actor_revokes_nothing_through_the_edit_form():
    actor, target = _user(), _user(2, "bob", is_admin=False)
    rows = _rows_for(2)
    session = _AdminSession(
        target, sessions=rows, actor_after_lock=_ActorRow(is_admin=False)
    )
    _, request = _edit(actor, target, session, is_admin=None, is_active=None)

    assert _is_refusal(request)
    assert session.rolled_back == 1
    assert session.touched_the_registry() is False
    assert session.live == rows


def test_a_demoted_actor_revokes_nothing_through_the_delete():
    actor, target = _user(), _user(2, "bob", is_admin=False)
    rows = _rows_for(2)
    session = _AdminSession(
        target, sessions=rows, actor_after_lock=_ActorRow(is_admin=False)
    )
    _, request = _delete(actor, target, session)

    assert _is_refusal(request)
    assert session.touched_the_registry() is False
    assert session.live == rows


def test_a_demoted_actor_revokes_nothing_through_the_reset():
    actor, target = _user(), _user(2, "bob", is_admin=False)
    rows = _rows_for(2)
    session = _AdminSession(
        target, sessions=rows, actor_after_lock=_ActorRow(is_admin=False)
    )
    _, request = _reset(actor, target, session)

    assert _is_refusal(request)
    assert session.touched_the_registry() is False
    assert session.live == rows


def test_a_deleted_actor_revokes_nothing():
    actor, target = _user(), _user(2, "bob", is_admin=False)
    rows = _rows_for(2)
    session = _AdminSession(target, sessions=rows, actor_after_lock=False)
    _, request = _reset(actor, target, session)

    assert _is_refusal(request)
    assert session.touched_the_registry() is False


# ── create_user's share of the policy ───────────────────────────────────────


def test_create_user_refuses_a_short_initial_password():
    actor = _user()
    session = _AdminSession(None)
    _, request = _create(actor, session, initial_password="short-one!")

    assert _is_refusal(request)
    assert "12 characters" in _flash(request)[0]
    assert session.committed == 0


def test_create_user_refuses_a_nul_byte_without_raising():
    """`hash_password` raises on a NUL, and `create_user` handed it form input
    directly — an unhandled `ValueError` and a 500 on an admin route."""
    actor = _user()
    session = _AdminSession(None)
    _, request = _create(actor, session, initial_password="abcdefghijkl\x00mn")

    assert _is_refusal(request)
    assert "NUL" in _flash(request)[0]
    assert session.committed == 0


def test_create_user_still_accepts_a_conforming_password():
    actor = _user()
    session = _AdminSession(None)
    _, request = _create(actor, session, initial_password="a-conforming-password")

    assert not _is_refusal(request)
    assert session.committed == 1


# ── the guard is one key ────────────────────────────────────────────────────


def test_the_admin_guard_delegates_to_the_shared_account_key():
    """Two keys do not exclude each other: the self-service password change
    and every session mint take `lock_account_guard`'s key, so these handlers
    must take exactly that one."""
    from src.oauth.grants import ACCOUNT_GUARD_LOCK_KEY

    actor, target = _user(), _user(2, "bob", is_admin=False)
    session = _AdminSession(target, sessions=_rows_for(2))
    _reset(actor, target, session)

    assert session.lock_keys == [ACCOUNT_GUARD_LOCK_KEY]
    assert users_mod._ADMIN_GUARD_LOCK_KEY == ACCOUNT_GUARD_LOCK_KEY


# ── the purge ───────────────────────────────────────────────────────────────


FROZEN_NOW = datetime.datetime(2026, 9, 5, 12, 0, tzinfo=UTC)


class _FrozenDatetime(datetime.datetime):
    @classmethod
    def now(cls, tz=None):
        return FROZEN_NOW if tz is None else FROZEN_NOW.astimezone(tz)


class _RecordingSession:
    """Captures the DELETEs the cleanup emits, executing nothing."""

    def __init__(self):
        self.statements = []
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def execute(self, stmt, *_a, **_kw):
        self.statements.append(stmt)
        return _Result(rowcount=0)

    async def commit(self):
        self.committed = True


def _run_cleanup(monkeypatch) -> _RecordingSession:
    session = _RecordingSession()
    monkeypatch.setattr(indexer, "async_session", lambda: session)
    monkeypatch.setattr(indexer, "datetime", _FrozenDatetime)
    asyncio.run(indexer.cleanup_expired_tokens())
    assert session.committed is True
    return session


def _session_delete(session) -> Delete:
    for stmt in session.statements:
        assert isinstance(stmt, Delete), stmt
        if stmt.table.name == "user_sessions":
            return stmt
    raise AssertionError("no DELETE emitted for user_sessions")


def _literal(node):
    if isinstance(node, BindParameter):
        return node.value
    if isinstance(node, Null):
        return None
    raise AssertionError(f"unsupported literal in the purge predicate: {node!r}")


def _evaluate(clause, row: dict) -> bool:
    """Evaluate the emitted WHERE clause against a row, in Python.

    A real (if tiny) evaluator rather than a peek at one bind parameter:
    reading the cutoff alone would keep passing if the revocation disjunct were
    dropped, since the cutoff itself would not change. Only the node types this
    predicate can emit are supported — anything else fails loudly rather than
    being silently treated as false.
    """
    if isinstance(clause, Grouping):
        # `and_(a, or_(b, c))` wraps the disjunction in a `Grouping` so the SQL
        # carries its parentheses. Unwrap it rather than treating a
        # parenthesised clause as an unsupported node.
        return _evaluate(clause.element, row)
    if isinstance(clause, BooleanClauseList):
        parts = [_evaluate(part, row) for part in clause.clauses]
        if clause.operator is operators.or_:
            return any(parts)
        if clause.operator is operators.and_:
            return all(parts)
        raise AssertionError(f"unsupported boolean operator: {clause.operator}")
    if isinstance(clause, BinaryExpression):
        value = row[clause.left.name]
        other = _literal(clause.right)
        if clause.operator is operators.lt:
            return value is not None and value < other
        if clause.operator is operators.is_:
            return value is None
        if clause.operator is operators.is_not:
            return value is not None
        raise AssertionError(f"unsupported comparison: {clause.operator}")
    raise AssertionError(f"unsupported clause: {clause!r}")


def _purged(session, *, expires_at, revoked_at=None) -> bool:
    where = _session_delete(session).whereclause
    return _evaluate(where, {"expires_at": expires_at, "revoked_at": revoked_at})


def test_the_purge_predicate_takes_the_later_of_expiry_and_revocation(monkeypatch):
    """Structural, compared as a tree. The OAuth half's single `expires_at`
    comparison is the shape this must never collapse back into."""
    session = _run_cleanup(monkeypatch)
    cutoff = FROZEN_NOW - datetime.timedelta(days=settings.session_purge_retain_days)
    where = _session_delete(session).whereclause

    assert where.compare(
        and_(
            UserSession.expires_at < cutoff,
            or_(
                UserSession.revoked_at.is_(None),
                UserSession.revoked_at < cutoff,
            ),
        )
    )
    assert not where.compare(UserSession.expires_at < cutoff)


def test_a_long_expired_never_revoked_row_is_deleted(monkeypatch):
    session = _run_cleanup(monkeypatch)
    assert _purged(session, expires_at=FROZEN_NOW - datetime.timedelta(days=8)) is True


def test_an_already_expired_row_revoked_moments_ago_survives(monkeypatch):
    """The #64 blank space, in a new table. An administrative reset revokes
    every unrevoked row of a user *including already-expired ones*; purging on
    expiry alone would delete the record of that revocation on the next tick,
    minutes after the operator performed it."""
    session = _run_cleanup(monkeypatch)
    assert (
        _purged(
            session,
            expires_at=FROZEN_NOW - datetime.timedelta(days=400),
            revoked_at=FROZEN_NOW - datetime.timedelta(minutes=5),
        )
        is False
    )


def test_a_recently_revoked_unexpired_row_survives(monkeypatch):
    session = _run_cleanup(monkeypatch)
    assert (
        _purged(
            session,
            expires_at=FROZEN_NOW + datetime.timedelta(days=6),
            revoked_at=FROZEN_NOW - datetime.timedelta(minutes=1),
        )
        is False
    )


def test_a_live_session_is_never_purged(monkeypatch):
    session = _run_cleanup(monkeypatch)
    assert _purged(session, expires_at=FROZEN_NOW + datetime.timedelta(days=7)) is False


def test_a_row_revoked_and_expired_beyond_the_window_is_deleted(monkeypatch):
    """Retention is a window, not an amnesty: the evidence goes once both
    timestamps are past it."""
    session = _run_cleanup(monkeypatch)
    assert (
        _purged(
            session,
            expires_at=FROZEN_NOW - datetime.timedelta(days=30),
            revoked_at=FROZEN_NOW - datetime.timedelta(days=29),
        )
        is True
    )


def test_the_retention_window_comes_from_configuration(monkeypatch):
    monkeypatch.setattr(settings, "session_purge_retain_days", 30)
    session = _run_cleanup(monkeypatch)
    cutoff = FROZEN_NOW - datetime.timedelta(days=30)
    assert _session_delete(session).whereclause.compare(
        and_(
            UserSession.expires_at < cutoff,
            or_(
                UserSession.revoked_at.is_(None),
                UserSession.revoked_at < cutoff,
            ),
        )
    )


def test_the_purge_runs_in_single_user_mode(monkeypatch):
    """Single-user mode never creates or validates a session row, but a
    deployment that flipped modes still has rows, and maintenance that skipped
    them would strand them forever."""
    monkeypatch.setattr(settings, "multi_user_mode", False)
    session = _run_cleanup(monkeypatch)
    assert _session_delete(session) is not None


def test_the_token_purge_is_unchanged(monkeypatch):
    """Scoped: the session predicate is an addition, not a rewrite of the
    token retention rule #64 settled."""
    from src.models.db import OAuthToken

    session = _run_cleanup(monkeypatch)
    token_delete = next(
        s for s in session.statements if s.table.name == OAuthToken.__tablename__
    )
    assert token_delete.whereclause.compare(
        OAuthToken.expires_at < FROZEN_NOW - datetime.timedelta(days=7)
    )


def test_the_cleanup_keeps_its_name():
    """"Tokens" there already means "dead credential rows on one schedule";
    renaming churns a 3,400-line module and its call site for a word — and the
    indexer loop's one call site would have to move with it."""
    assert callable(indexer.cleanup_expired_tokens)
