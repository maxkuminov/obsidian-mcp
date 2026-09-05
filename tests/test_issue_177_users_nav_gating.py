"""Regression test (#177): the Users nav item rendered in single-user mode.

`base.html` gated the ADMIN section on `is_admin` alone. The single-user
sentinel reports `is_admin=True` (`_SingleUserSentinel`), so an operator who
never turned multi-user mode on still saw an "Admin → Users" link to a list
that can only ever hold the sentinel itself.

The gate is now `is_admin and multi_user_mode`, and it covers the section
heading too because Users is that section's only entry. Settings keeps its
own `is_admin`-only gate — it belongs to Content and is meaningful either way.

`multi_user_mode` reaches the template from `_panel_context`, which every
handler that renders a `base.html` descendant merges in, so the flag is never
missing (a missing flag would be falsy under Jinja's default `Undefined` and
would hide the link, not leak it).
"""
import os

import pydantic_settings

_orig_init = pydantic_settings.BaseSettings.__init__


def _no_env_file_init(self, *args, **kwargs):
    kwargs.setdefault("_env_file", None)
    _orig_init(self, *args, **kwargs)


pydantic_settings.BaseSettings.__init__ = _no_env_file_init
try:
    from starlette.templating import Jinja2Templates

    from src.control_panel import routes
finally:
    pydantic_settings.BaseSettings.__init__ = _orig_init


TEMPLATES_DIR = os.path.join(os.path.dirname(routes.__file__), "templates")


def _render_base(**overrides) -> str:
    """Render `base.html` itself — the nav lives there and nowhere else."""
    context = {
        "active": "dashboard",
        "is_admin": True,
        "multi_user_mode": True,
        "username": "max",
        "csrf_token": "test-csrf-token",
    }
    context.update(overrides)
    templates = Jinja2Templates(directory=TEMPLATES_DIR)
    response = templates.TemplateResponse(
        request=None,  # base.html never dereferences `request`
        name="base.html",
        context=context,
    )
    return response.body.decode()


def test_single_user_mode_hides_the_users_nav_item():
    html = _render_base(is_admin=True, multi_user_mode=False)
    assert "/admin/users/" not in html
    assert ">Admin</div>" not in html, "the section heading goes with its only entry"


def test_multi_user_admin_still_gets_the_users_nav_item():
    html = _render_base(is_admin=True, multi_user_mode=True)
    assert "/admin/users/" in html
    assert ">Admin</div>" in html


def test_a_non_admin_never_sees_users_even_in_multi_user_mode():
    html = _render_base(is_admin=False, multi_user_mode=True)
    assert "/admin/users/" not in html


def test_settings_stays_admin_gated_in_both_modes():
    """Settings is not part of the #177 change: admin-only, mode-agnostic."""
    assert "/admin/settings" in _render_base(is_admin=True, multi_user_mode=False)
    assert "/admin/settings" in _render_base(is_admin=True, multi_user_mode=True)
    assert "/admin/settings" not in _render_base(is_admin=False, multi_user_mode=True)


def test_panel_context_always_carries_the_flag():
    """The template gate is only as good as the context builder feeding it."""
    import inspect

    source = inspect.getsource(routes._panel_context)
    assert '"multi_user_mode"' in source
