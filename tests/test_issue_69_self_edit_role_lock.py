"""Regression test (#69): a self-edit can never demote or deactivate you.

`user_edit.html` promises, unconditionally, "This is your own account — you
can't remove your own admin role or deactivate yourself here." The handler
only ever refused when the target was the **last active admin**, so with any
second active admin present an admin who unchecked their own Admin (or Active)
box and saved was demoted or deactivated on the spot — locking themselves out
of a panel they may be the only operator of. Neither checkbox carried
`disabled`, so nothing in the UI contradicted the promise.

The fix makes both sides agree, the fail-closed way:

- both checkboxes render `disabled` when `is_self`;
- the handler refuses the change for a self-edit *regardless* of how many
  other admins exist — including a hand-crafted POST that bypasses the form;
- and, because a `disabled` checkbox is not POSTed at all, an absent field on
  a self-edit means "unchanged", never "unchecked". Reading it as unchecked
  would demote the operator on every single save, which is the trap the fix
  must not walk into.
"""
import asyncio
import os

import pydantic_settings

_orig_init = pydantic_settings.BaseSettings.__init__


def _no_env_file_init(self, *args, **kwargs):
    kwargs.setdefault("_env_file", None)
    _orig_init(self, *args, **kwargs)


pydantic_settings.BaseSettings.__init__ = _no_env_file_init
try:
    from starlette.templating import Jinja2Templates

    from src.control_panel import users as users_mod
    from src.control_panel.flash import FLASH_SESSION_KEY
    from src.models.db import User
finally:
    pydantic_settings.BaseSettings.__init__ = _orig_init


TEMPLATES_DIR = os.path.join(os.path.dirname(users_mod.__file__), "templates")


# --- Fakes ----------------------------------------------------------------


class _Result:
    """A canned result. `rowcount` is what the session revocation reads (#198).

    `delete_user` and `edit_user_submit` now issue an `UPDATE user_sessions`
    inside the same critical section, and `revoke_user_sessions` reads
    `result.rowcount` off it. Zero is the honest answer for a fake that holds
    no session rows: these tests are about the guard, not about the registry.
    """

    rowcount = 0

    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def one_or_none(self):
        return self._value

    def scalar(self):
        return self._value


class _ActorRow:
    """What `select(User.is_admin, User.is_active)` yields."""

    def __init__(self, is_admin, is_active):
        self.is_admin = is_admin
        self.is_active = is_active


class _FakeSession:
    """Answers the handler's queries: the advisory lock (which returns
    nothing anyone reads), the target row, then the remaining-active-admins
    count."""

    def __init__(self, target: User, remaining_admins: int = 1,
                 actor_after_lock=None):
        self._target = target
        self._remaining_admins = remaining_admins
        # What the actor's row looks like *once the lock has been taken* —
        # i.e. after any concurrent demotion has committed. `None` means
        # "unchanged: still an active admin".
        self._actor_after_lock = actor_after_lock
        self.committed = False
        self.rolled_back = False
        self.deleted = None
        self.queries = 0
        self.statements: list[str] = []
        self.lock_keys: list[object] = []

    async def execute(self, stmt, *_a, **_k):
        sql = str(stmt)
        self.statements.append(sql)
        if "pg_advisory_xact_lock" in sql:
            params = _a[0] if _a else _k.get("params", {})
            self.lock_keys.append(params.get("key"))
            return _Result(None)
        # Must be a *prefix* test: `select(User)` names every column, so
        # "users.is_admin, users.is_active" appears in the target query too.
        if sql.startswith("SELECT users.is_admin, users.is_active"):
            if self._actor_after_lock is False:  # the actor's row is gone
                return _Result(None)
            return _Result(
                self._actor_after_lock
                if self._actor_after_lock is not None
                else _ActorRow(True, True)
            )
        if "count(" in sql.lower():
            return _Result(self._remaining_admins)
        return _Result(self._target)

    async def delete(self, obj):
        self.deleted = obj

    async def rollback(self):
        self.rolled_back = True

    async def commit(self):
        self.committed = True


def _user(**overrides) -> User:
    u = User(
        username="max",
        password_hash="x",
        is_admin=True,
        is_active=True,
        vault_path=None,
    )
    u.id = 1
    for k, v in overrides.items():
        setattr(u, k, v)
    return u


class _Req:
    """The `Request` the handlers now take (#138).

    Only two attributes are ever touched: `session`, where the flash lands,
    and `query_params`, which `delete_user` reads for `?permanent=true`.
    """

    def __init__(self, query_params: dict | None = None):
        self.session: dict = {}
        self.query_params = query_params if query_params is not None else {}


def _flash_of(request: _Req) -> tuple[str | None, str]:
    """`(message, kind)` the handler parked in the session, or `(None, "ok")`."""
    entry = request.session.get(FLASH_SESSION_KEY)
    if not entry:
        return None, "ok"
    return entry["message"], entry["kind"]


def _submit(actor: User, target: User, *, is_admin: str | None,
            is_active: str | None, remaining_admins: int = 1,
            actor_after_lock=None):
    session = _FakeSession(
        target, remaining_admins=remaining_admins,
        actor_after_lock=actor_after_lock,
    )
    request = _Req()
    response = asyncio.run(
        users_mod.edit_user_submit(
            user_id=target.id,
            request=request,
            vault_path="",
            vault_path_custom="",
            is_admin=is_admin,
            is_active=is_active,
            session=session,
            user=actor,
        )
    )
    # The refusal is no longer visible in the redirect target (#138) — the
    # message rides the session — so the request that carried it has to reach
    # the assertions. Every call site already unpacks two values.
    response.flash_request = request
    return response, session


def _is_error_redirect(response) -> bool:
    """A refusal: an `err`-kind flash was set for the next panel render."""
    message, kind = _flash_of(response.flash_request)
    return message is not None and kind == "err"


# --- The bug: self-demotion with a second admin present -------------------


def test_self_demotion_is_refused_even_when_other_admins_exist():
    """The exact #69 scenario: two active admins, one unchecks their own
    Admin box and saves. Before the fix the demotion went through.

    An unchecked *browser* checkbox submits nothing at all, which is
    indistinguishable from the `disabled` field the fixed template renders —
    so the handler reads it as "unchanged" and the role survives. That is the
    property under test; the save itself is allowed to succeed for the fields
    a self-edit may legitimately change (the vault path)."""
    me = _user()
    _submit(me, me, is_admin=None, is_active="on", remaining_admins=3)
    assert me.is_admin is True, "self-demotion went through — the #69 bug"


def test_self_deactivation_is_refused_even_when_other_admins_exist():
    me = _user()
    _submit(me, me, is_admin="on", is_active=None, remaining_admins=3)
    assert me.is_active is True, "self-deactivation went through — the #69 bug"


def test_self_deactivation_via_an_explicit_false_is_an_error():
    """A submission that *states* the intent (rather than merely omitting the
    field) gets a reason back instead of a silent no-op."""
    me = _user()
    response, session = _submit(
        me, me, is_admin="on", is_active="false", remaining_admins=3
    )
    assert me.is_active is True
    assert _is_error_redirect(response)
    assert session.committed is False


def test_self_demotion_is_refused_for_a_scripted_post_too():
    """`disabled` only removes the field from the browser's submission; a
    hand-crafted POST can still send `is_admin=off`. The handler is the
    guard, not the template."""
    me = _user()
    response, session = _submit(
        me, me, is_admin="off", is_active="on", remaining_admins=3
    )
    assert me.is_admin is True
    assert _is_error_redirect(response)
    assert session.committed is False


def test_present_but_empty_field_is_refused_not_read_as_unchanged():
    """`is_admin=` — present, empty — is a submission, not an omission. It
    must not slip through the "absent means unchanged" door; only a truly
    absent field (`None`) may."""
    me = _user()
    response, session = _submit(
        me, me, is_admin="", is_active="on", remaining_admins=3
    )
    assert me.is_admin is True
    assert _is_error_redirect(response)
    assert session.committed is False


def test_present_but_empty_active_field_is_refused_too():
    me = _user()
    response, session = _submit(
        me, me, is_admin="on", is_active="", remaining_admins=3
    )
    assert me.is_active is True
    assert _is_error_redirect(response)
    assert session.committed is False


# --- Concurrency: the guard and the write must be one critical section ----


def _lock_index(statements: list[str]) -> int:
    for i, s in enumerate(statements):
        if "pg_advisory_xact_lock" in s:
            return i
    return -1


def _count_index(statements: list[str]) -> int:
    for i, s in enumerate(statements):
        if "count(" in s.lower():
            return i
    return -1


def test_edit_takes_the_admin_lock_before_counting_admins():
    """Without it the count and the write are two statements with nothing
    between them: two admins demoting each other concurrently both read "one
    other admin remains", both pass, and the panel is left with zero."""
    me = _user()
    bob = _user(username="bob")
    bob.id = 2
    _, session = _submit(me, bob, is_admin=None, is_active="on", remaining_admins=1)

    lock_at = _lock_index(session.statements)
    count_at = _count_index(session.statements)
    assert lock_at != -1, "no advisory lock taken"
    assert count_at != -1, "the last-admin count did not run"
    assert lock_at < count_at, "the lock must precede the count it protects"


def test_edit_takes_the_lock_before_reading_the_target_row():
    """The target's own flags are part of what the guard decides on, so the
    read belongs inside the critical section too."""
    me = _user()
    _, session = _submit(me, me, is_admin=None, is_active=None)
    assert "pg_advisory_xact_lock" in session.statements[0]


def _delete(actor: User, target: User, *, permanent=False, remaining_admins=1,
            actor_after_lock=None):
    session = _FakeSession(
        target, remaining_admins=remaining_admins,
        actor_after_lock=actor_after_lock,
    )
    request = _Req({"permanent": "true"} if permanent else {})
    response = asyncio.run(
        users_mod.delete_user(
            user_id=target.id,
            request=request,
            session=session,
            user=actor,
        )
    )
    response.flash_request = request
    return response, session


def test_delete_takes_the_admin_lock_before_counting_admins():
    """A delete and an edit can each remove the other's "remaining admin", so
    both must take the *same* lock — excluding only their own kind would not
    close the race."""
    me = _user()
    bob = _user(username="bob")
    bob.id = 2
    _, session = _delete(me, bob, remaining_admins=1)

    lock_at = _lock_index(session.statements)
    count_at = _count_index(session.statements)
    assert lock_at != -1, "no advisory lock taken"
    assert count_at != -1
    assert lock_at < count_at


def test_delete_still_refuses_the_last_active_admin():
    me = _user()
    bob = _user(username="bob")
    bob.id = 2
    response, session = _delete(me, bob, remaining_admins=0)
    assert bob.is_active is True
    assert _is_error_redirect(response)
    assert session.committed is False


def test_edit_refuses_when_the_actor_was_demoted_while_waiting():
    """`require_admin_panel` authorised this request before the lock was even
    requested, and the wait for that lock is exactly when another admin's
    demotion of *this* actor commits. Serializing the writes is not enough if
    the loser then performs the mutation anyway."""
    me = _user()
    bob = _user(username="bob")
    bob.id = 2
    response, session = _submit(
        me, bob, is_admin=None, is_active="on", remaining_admins=1,
        actor_after_lock=_ActorRow(is_admin=False, is_active=True),
    )
    assert bob.is_admin is True, "a demoted actor still performed the mutation"
    assert session.committed is False
    assert session.rolled_back is True, "the advisory lock must be released"
    assert _is_error_redirect(response)


def test_edit_refuses_when_the_actor_was_deactivated_while_waiting():
    me = _user()
    bob = _user(username="bob")
    bob.id = 2
    response, session = _submit(
        me, bob, is_admin=None, is_active="on", remaining_admins=1,
        actor_after_lock=_ActorRow(is_admin=True, is_active=False),
    )
    assert bob.is_admin is True
    assert session.committed is False
    assert _is_error_redirect(response)


def test_edit_refuses_when_the_actor_row_is_gone():
    """A permanently deleted actor has no row at all — not privileged
    either."""
    me = _user()
    bob = _user(username="bob")
    bob.id = 2
    response, session = _submit(
        me, bob, is_admin=None, is_active="on", remaining_admins=1,
        actor_after_lock=False,  # sentinel: `one_or_none()` finds nothing
    )
    assert bob.is_admin is True
    assert session.committed is False
    assert _is_error_redirect(response)


def test_edit_actor_recheck_happens_after_the_lock():
    """Re-reading before the lock would read the same stale row the
    dependency did."""
    me = _user()
    bob = _user(username="bob")
    bob.id = 2
    _, session = _submit(me, bob, is_admin=None, is_active="on")
    lock_at = _lock_index(session.statements)
    actor_at = next(
        i for i, sql in enumerate(session.statements)
        if sql.startswith("SELECT users.is_admin, users.is_active")
    )
    assert lock_at < actor_at


def test_delete_refuses_when_the_actor_was_demoted_while_waiting():
    me = _user()
    bob = _user(username="bob")
    bob.id = 2
    response, session = _delete(
        me, bob, remaining_admins=1,
        actor_after_lock=_ActorRow(is_admin=False, is_active=True),
    )
    assert bob.is_active is True, "a demoted actor still deleted a user"
    assert session.committed is False
    assert session.rolled_back is True
    assert _is_error_redirect(response)


def test_delete_actor_recheck_happens_after_the_lock():
    me = _user()
    bob = _user(username="bob")
    bob.id = 2
    _, session = _delete(me, bob)
    lock_at = _lock_index(session.statements)
    actor_at = next(
        i for i, sql in enumerate(session.statements)
        if sql.startswith("SELECT users.is_admin, users.is_active")
    )
    assert lock_at < actor_at


def test_single_user_sentinel_is_not_re_read():
    """The sentinel has no `users` row (its `id` is None), and there is no
    second admin who could have demoted it."""
    class _Sentinel:
        id = None
        username = "admin"
        is_admin = True
        is_active = True

    bob = _user(username="bob")
    bob.id = 2
    response, session = _submit(
        _Sentinel(), bob, is_admin=None, is_active="on", remaining_admins=1
    )
    assert session.committed is True
    assert not _is_error_redirect(response)
    assert not any(
        sql.startswith("SELECT users.is_admin, users.is_active")
        for sql in session.statements
    )


def test_delete_and_edit_share_one_lock_key():
    """Two different constants would not exclude each other at all."""
    me = _user()
    bob = _user(username="bob")
    bob.id = 2
    _, edit_session = _submit(me, bob, is_admin=None, is_active="on")
    _, delete_session = _delete(me, bob)
    key = users_mod._ADMIN_GUARD_LOCK_KEY
    assert edit_session.lock_keys == [key]
    assert delete_session.lock_keys == [key]
    # `pg_advisory_xact_lock(bigint)` — a key outside int64 is a runtime error
    # on the first concurrent edit, i.e. exactly when it is needed.
    assert -(2 ** 63) <= key < 2 ** 63


# --- The trap the fix must not walk into ----------------------------------


def test_absent_checkboxes_on_a_self_edit_mean_unchanged_not_unchecked():
    """Both fields are `disabled` in the rendered form, so a *normal* save of
    your own account submits neither. That must save the vault path without
    demoting or deactivating you."""
    me = _user(vault_path=None)
    response, session = _submit(me, me, is_admin=None, is_active=None)
    assert me.is_admin is True
    assert me.is_active is True
    assert session.committed is True
    assert not _is_error_redirect(response)
    # The success message is a session flash now, not a query parameter (#138).
    assert _flash_of(response.flash_request) == (
        f"Updated user '{me.username}'.", "ok"
    )
    assert response.headers["location"] == "/admin/users/"


# --- Editing somebody else is unchanged -----------------------------------


def test_demoting_another_admin_still_works_when_admins_remain():
    me = _user()
    bob = _user(username="bob")
    bob.id = 2
    response, session = _submit(
        me, bob, is_admin=None, is_active="on", remaining_admins=1
    )
    assert bob.is_admin is False
    assert session.committed is True
    assert not _is_error_redirect(response)


def test_demoting_the_last_active_admin_is_still_refused():
    me = _user()
    bob = _user(username="bob")
    bob.id = 2
    response, session = _submit(
        me, bob, is_admin=None, is_active="on", remaining_admins=0
    )
    assert bob.is_admin is True
    assert _is_error_redirect(response)
    assert session.committed is False


# --- The template side ----------------------------------------------------


def _render_user_edit(is_self: bool) -> str:
    templates = Jinja2Templates(directory=TEMPLATES_DIR)
    response = templates.TemplateResponse(
        request=None,  # user_edit.html never touches `request` directly
        name="user_edit.html",
        context={
            "active": "users",
            "is_admin": True,
            "multi_user_mode": True,
            "username": "max",
            "csrf_token": "test-csrf-token",
            "target": {
                "id": 1,
                "username": "max",
                "is_admin": True,
                "is_active": True,
                "vault_path": "",
            },
            "available_vaults": ["/vaults/max"],
            "is_self": is_self,
            "error": None,
            "flash": None,
        },
    )
    return response.body.decode()


def _input_tag(html: str, name: str) -> str:
    idx = html.find(f'name="{name}"')
    assert idx != -1, f"no input named {name}"
    start = html.rfind("<input", 0, idx)
    return html[start : html.find(">", idx) + 1]


def test_self_edit_renders_both_checkboxes_disabled():
    html = _render_user_edit(is_self=True)
    assert "disabled" in _input_tag(html, "is_admin")
    assert "disabled" in _input_tag(html, "is_active")


def test_editing_another_user_leaves_the_checkboxes_live():
    html = _render_user_edit(is_self=False)
    assert "disabled" not in _input_tag(html, "is_admin")
    assert "disabled" not in _input_tag(html, "is_active")
