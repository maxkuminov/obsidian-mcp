"""Regression test (#90): an admin cannot delete or deactivate themselves.

PR #80 made the self-edit promise unconditional per #69 — on the edit form an
admin can neither demote nor deactivate their own account, the checkboxes
render `disabled`, and `edit_user_submit` refuses a hand-built POST that tries
anyway. `delete_user`, the *other* handler on the same page and reachable from
the two forms directly beneath those checkboxes, still allowed both as long as
one other active admin existed. The last-admin guard was the only thing in the
way, and it is about the panel keeping *an* admin, not about this admin keeping
their account.

So the promise held on one form and not on the form beneath it. An operator who
has been told the role toggle is inert reasonably reads "Soft delete: sets
`is_active=false`. Data preserved" as a different, safer control; it reaches the
same `users.is_active` flag by another route. The permanent form is strictly
worse and one click further down: the cascade on `users.id` also destroys the
actor's `api_keys`, `oauth_clients`, `oauth_tokens` and `notes_metadata`, so
nothing survives with which to sign back in and undo it.

The decision: #69's promise is about the **account**, not the form. Both delete
forms refuse a self-target unconditionally — including when other active admins
exist. What is deliberately *not* changed is the last-admin guard, which stays
exactly as narrow as it was: it refuses only when the target is itself an active
admin and no **other** active admin exists. Broadening it to "the state left
behind holds no active admin" would refuse ordinary account cleanup on a table
that has no active admin row at all, which is the shape single-user mode
presents, and would also forbid the very removal the new refusal directs the
operator to ask for.
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

    from src.auth.session import _SingleUserSentinel
    from src.control_panel import users as users_mod
    from src.models.db import User
finally:
    pydantic_settings.BaseSettings.__init__ = _orig_init


TEMPLATES_DIR = os.path.join(os.path.dirname(users_mod.__file__), "templates")


# --- Fakes (same shape as tests/test_issue_69_self_edit_role_lock.py) ------


class _Result:
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
    """Answers `delete_user`'s queries: the advisory lock, the actor re-read,
    the target row, then the remaining-active-admins count."""

    def __init__(self, target: User, remaining_admins: int = 1,
                 actor_after_lock=None):
        self._target = target
        self._remaining_admins = remaining_admins
        # The actor's row as it looks *once the lock has been taken* — i.e.
        # after any concurrent demotion has committed. `None` means
        # "unchanged: still an active admin"; `False` means the row is gone.
        self._actor_after_lock = actor_after_lock
        self.committed = False
        self.rolled_back = False
        self.deleted = None
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
            if self._actor_after_lock is False:
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


def _delete(actor, target: User, *, permanent=False, remaining_admins=1,
            actor_after_lock=None):
    session = _FakeSession(
        target, remaining_admins=remaining_admins,
        actor_after_lock=actor_after_lock,
    )

    class _Req:
        query_params = {"permanent": "true"} if permanent else {}

    response = asyncio.run(
        users_mod.delete_user(
            user_id=target.id,
            request=_Req(),
            session=session,
            user=actor,
        )
    )
    return response, session


def _location(response) -> str:
    return response.headers["location"]


def _is_error_redirect(response) -> bool:
    return "error=" in _location(response)


def _mentions_another_admin(response) -> bool:
    """The refusal has to name the way out. `_q` URL-encodes the message, so
    match on the encoded form the operator's browser actually receives."""
    from urllib.parse import unquote

    return "another admin" in unquote(_location(response)).lower()


# --- The bug: self-delete with other admins present -----------------------


def test_soft_self_delete_is_refused_when_other_admins_exist():
    """The exact #90 scenario. Before the fix, `remaining_admins=3` sailed
    past the last-admin guard and the actor deactivated themselves."""
    me = _user()
    response, session = _delete(me, me, remaining_admins=3)
    assert me.is_active is True, "self-deactivation went through — the #90 bug"
    assert session.committed is False
    assert session.deleted is None
    assert _is_error_redirect(response)
    assert _mentions_another_admin(response)


def test_permanent_self_delete_is_refused_when_other_admins_exist():
    """The worse half: the `users.id` cascade would take the actor's own
    api_keys, oauth_clients, oauth_tokens and notes_metadata with the row."""
    me = _user()
    response, session = _delete(me, me, permanent=True, remaining_admins=3)
    assert session.deleted is None, "the actor's own row was deleted — the #90 bug"
    assert session.committed is False
    assert me.is_active is True, "the flag must be untouched by the refusal too"
    assert _is_error_redirect(response)
    assert _mentions_another_admin(response)


def test_self_delete_is_refused_even_with_a_large_admin_roster():
    """Unconditional means unconditional: no number of colleagues makes
    removing your own account permissible through this handler."""
    for permanent in (False, True):
        me = _user()
        _, session = _delete(me, me, permanent=permanent, remaining_admins=99)
        assert me.is_active is True
        assert session.committed is False
        assert session.deleted is None


def test_the_refusal_writes_nothing_at_all():
    """`is_active` and the row itself both survive, on both paths — the
    refusal must not be a write that happens to be reported as an error."""
    for permanent in (False, True):
        me = _user()
        _, session = _delete(me, me, permanent=permanent, remaining_admins=2)
        assert me.is_active is True
        assert me.is_admin is True
        assert session.deleted is None
        assert session.committed is False


# --- Deleting somebody else is unaffected ---------------------------------


def test_deleting_another_user_still_works_soft():
    me = _user()
    bob = _user(username="bob", is_admin=False)
    bob.id = 2
    response, session = _delete(me, bob, remaining_admins=1)
    assert bob.is_active is False
    assert session.committed is True
    assert not _is_error_redirect(response)


def test_deleting_another_user_still_works_permanent():
    me = _user()
    bob = _user(username="bob", is_admin=False)
    bob.id = 2
    response, session = _delete(me, bob, permanent=True, remaining_admins=1)
    assert session.deleted is bob
    assert session.committed is True
    assert not _is_error_redirect(response)


def test_one_of_two_active_admins_may_delete_the_other():
    """This is the removal the new refusal tells the operator to ask for, so
    it must keep working. The unchanged guard permits it because the acting
    admin remains an active admin afterwards."""
    for permanent in (False, True):
        me = _user()
        other = _user(username="bob", is_admin=True, is_active=True)
        other.id = 2
        response, session = _delete(
            me, other, permanent=permanent, remaining_admins=1
        )
        assert session.committed is True
        assert not _is_error_redirect(response)
        if permanent:
            assert session.deleted is other
        else:
            assert other.is_active is False


# --- The last-admin guard is unchanged ------------------------------------


class _Sentinel(_SingleUserSentinel):
    """Single-user mode's acting admin: not a `User`, and `id` is None, so no
    target can ever be it and it is never counted among the admins."""


def test_last_admin_guard_still_fires_on_its_one_reachable_path():
    """With a self-target refused above and any other target leaving the
    acting `User` admin in place, the guard's only remaining path is the
    single-user sentinel deleting the sole active admin in the table. That
    would leave a database no multi-user deployment could be switched back
    on with."""
    for permanent in (False, True):
        sole = _user(username="max", is_admin=True, is_active=True)
        response, session = _delete(
            _Sentinel(), sole, permanent=permanent, remaining_admins=0
        )
        assert sole.is_active is True
        assert session.deleted is None
        assert session.committed is False
        assert _is_error_redirect(response)
        from urllib.parse import unquote

        assert "last active admin" in unquote(_location(response)).lower()


def test_deleting_a_non_admin_succeeds_on_a_table_with_no_active_admin():
    """The false positive a broader "zero admins would remain" reading would
    introduce. The target is not an active admin, so the delete cannot change
    how many active admins the table holds — and it holds none. Refusing here
    would block ordinary account cleanup in exactly the deployment shape
    single-user mode presents."""
    for permanent in (False, True):
        bob = _user(username="bob", is_admin=False, is_active=True)
        bob.id = 2
        response, session = _delete(
            _Sentinel(), bob, permanent=permanent, remaining_admins=0
        )
        assert session.committed is True, (
            "a non-admin delete was refused for want of an admin"
        )
        assert not _is_error_redirect(response)
        if permanent:
            assert session.deleted is bob
        else:
            assert bob.is_active is False


def test_the_sentinel_has_no_account_to_refuse():
    """`isinstance(user, User)` is what expresses "there is no account for the
    target to be". The sentinel's `id` is None; comparing it to a target id
    without the isinstance test would be a silent no-match today and a live
    bug the day anything gives the sentinel an id."""
    sentinel = _Sentinel()
    assert not isinstance(sentinel, User)
    assert sentinel.id is None
    bob = _user(username="bob", is_admin=False)
    bob.id = 2
    _, session = _delete(sentinel, bob, remaining_admins=1)
    assert session.committed is True
    # The sentinel is never re-read either — it has no row to re-read.
    assert not any(
        sql.startswith("SELECT users.is_admin, users.is_active")
        for sql in session.statements
    )


# --- Ordering inside the critical section ---------------------------------


def _index(statements, pred) -> int:
    for i, s in enumerate(statements):
        if pred(s):
            return i
    return -1


def test_a_demoted_actor_gets_the_actor_revoked_message_not_the_self_one():
    """The ordering scenario. The actor re-check runs first, so an admin
    demoted while queued for the lock is told *that* — not told they cannot
    delete themselves, which would be a true statement that misdiagnoses what
    just happened."""
    me = _user()
    response, session = _delete(
        me, me, remaining_admins=3,
        actor_after_lock=_ActorRow(is_admin=False, is_active=True),
    )
    from urllib.parse import unquote

    msg = unquote(_location(response))
    assert users_mod._ACTOR_REVOKED_MSG in msg
    assert "your own account" not in msg.lower()
    assert me.is_active is True
    assert session.committed is False
    assert session.rolled_back is True, "the advisory lock must be released"


def test_the_self_refusal_runs_inside_the_lock_and_before_the_count():
    """It must be under the *existing* advisory lock, after the actor
    re-read, and before the active-admin count — the count is what would
    otherwise answer a self-target with the last-admin message."""
    me = _user()
    _, session = _delete(me, me, remaining_admins=3)

    lock_at = _index(session.statements, lambda s: "pg_advisory_xact_lock" in s)
    actor_at = _index(
        session.statements,
        lambda s: s.startswith("SELECT users.is_admin, users.is_active"),
    )
    count_at = _index(session.statements, lambda s: "count(" in s.lower())

    assert lock_at == 0, "the lock must be the first statement"
    assert actor_at > lock_at, "the actor re-read belongs inside the lock"
    assert count_at == -1, (
        "the admin count ran — the self refusal must answer before it"
    )


def test_a_missing_target_is_still_a_404():
    """The target load and its 404 stay in place. Deliberately not pinned any
    harder than this: the refusal may compare `target.id` after the load (as
    it does) or the `user_id` route parameter before it — those are
    equivalent, and a test that forbade one of them would fail a refactor the
    contract explicitly allows. What must not happen is a missing target
    being swallowed by either guard."""
    import fastapi

    me = _user()
    session = _FakeSession(target=None, remaining_admins=3)

    class _Req:
        query_params: dict = {}

    try:
        asyncio.run(
            users_mod.delete_user(
                user_id=999, request=_Req(), session=session, user=me
            )
        )
    except fastapi.HTTPException as exc:
        assert exc.status_code == 404
    else:
        raise AssertionError("a delete of a nonexistent user did not 404")
    assert session.committed is False
    assert session.deleted is None


def test_no_second_lock_key_is_introduced():
    """Two keys do not exclude each other — CLAUDE.md is explicit. The
    self-delete refusal must ride the one key the edit handler also takes."""
    me = _user()
    _, session = _delete(me, me, remaining_admins=3)
    assert session.lock_keys == [users_mod._ADMIN_GUARD_LOCK_KEY]


def test_the_delete_handler_never_commits_before_writing_the_flags():
    """The advisory lock is transaction-scoped, so a commit between taking it
    and the write would release it and un-do the atomicity of the guard."""
    import inspect

    src = inspect.getsource(users_mod.delete_user)
    lock_at = src.index("_lock_admin_guard")
    first_commit = src.index("session.commit()")
    between = src[lock_at:first_commit]
    assert "session.commit()" not in between


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


def _delete_buttons(html: str) -> list[str]:
    """The two submits inside the forms posting to `/delete`."""
    out = []
    start = 0
    while True:
        idx = html.find("/delete", start)
        if idx == -1:
            return out
        btn = html.find("<button", idx)
        assert btn != -1, "a delete form with no submit button"
        out.append(html[btn : html.find(">", btn) + 1])
        start = idx + 1


def test_the_self_view_offers_no_enabled_delete_control():
    html = _render_user_edit(is_self=True)
    buttons = _delete_buttons(html)
    assert len(buttons) == 2, f"expected both delete submits, got {buttons}"
    for tag in buttons:
        assert "disabled" in tag, f"delete control still live on a self-view: {tag}"


def test_the_self_view_states_the_refusal():
    # Collapse whitespace first: the copy is wrapped across source lines, so
    # a phrase can straddle a newline in the rendered body.
    html = _render_user_edit(is_self=True)
    lowered = " ".join(html.split()).lower()
    assert "your own account" in lowered
    assert "another admin" in lowered
    # It says the server refuses too — the markup is the explanation, the
    # handler is the enforcement, and the copy must not imply otherwise.
    assert "refuses the request" in lowered


def test_deleting_another_user_leaves_both_controls_live():
    html = _render_user_edit(is_self=False)
    buttons = _delete_buttons(html)
    assert len(buttons) == 2
    for tag in buttons:
        assert "disabled" not in tag
