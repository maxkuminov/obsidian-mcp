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
    from src.models.db import User
finally:
    pydantic_settings.BaseSettings.__init__ = _orig_init


TEMPLATES_DIR = os.path.join(os.path.dirname(users_mod.__file__), "templates")


# --- Fakes ----------------------------------------------------------------


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalar(self):
        return self._value


class _FakeSession:
    """Answers the handler's two queries: the target row, then the
    remaining-active-admins count."""

    def __init__(self, target: User, remaining_admins: int = 1):
        self._target = target
        self._remaining_admins = remaining_admins
        self.committed = False
        self.queries = 0

    async def execute(self, _stmt):
        self.queries += 1
        if self.queries == 1:
            return _Result(self._target)
        return _Result(self._remaining_admins)

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


def _submit(actor: User, target: User, *, is_admin: str, is_active: str,
            remaining_admins: int = 1):
    session = _FakeSession(target, remaining_admins=remaining_admins)
    response = asyncio.run(
        users_mod.edit_user_submit(
            user_id=target.id,
            vault_path="",
            vault_path_custom="",
            is_admin=is_admin,
            is_active=is_active,
            session=session,
            user=actor,
        )
    )
    return response, session


def _is_error_redirect(response) -> bool:
    return "error=" in response.headers["location"]


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
    _submit(me, me, is_admin="", is_active="on", remaining_admins=3)
    assert me.is_admin is True, "self-demotion went through — the #69 bug"


def test_self_deactivation_is_refused_even_when_other_admins_exist():
    me = _user()
    _submit(me, me, is_admin="on", is_active="", remaining_admins=3)
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


# --- The trap the fix must not walk into ----------------------------------


def test_absent_checkboxes_on_a_self_edit_mean_unchanged_not_unchecked():
    """Both fields are `disabled` in the rendered form, so a *normal* save of
    your own account submits neither. That must save the vault path without
    demoting or deactivating you."""
    me = _user(vault_path=None)
    response, session = _submit(me, me, is_admin="", is_active="")
    assert me.is_admin is True
    assert me.is_active is True
    assert session.committed is True
    assert "error=" not in response.headers["location"]
    assert "flash=" in response.headers["location"]


# --- Editing somebody else is unchanged -----------------------------------


def test_demoting_another_admin_still_works_when_admins_remain():
    me = _user()
    bob = _user(username="bob")
    bob.id = 2
    response, session = _submit(
        me, bob, is_admin="", is_active="on", remaining_admins=1
    )
    assert bob.is_admin is False
    assert session.committed is True
    assert "error=" not in response.headers["location"]


def test_demoting_the_last_active_admin_is_still_refused():
    me = _user()
    bob = _user(username="bob")
    bob.id = 2
    response, session = _submit(
        me, bob, is_admin="", is_active="on", remaining_admins=0
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
