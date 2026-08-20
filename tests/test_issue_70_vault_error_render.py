"""Regression test (#70): `vault.html` must render `vault_error`.

`vault_page` catches the "no vault_path assigned" RuntimeError from
`_vault_root` and deliberately hands the template a `vault_error` key so the
page can explain itself "as a friendly empty state rather than a 500". The
template never referenced that key, so the message was computed and thrown
away and an *unassigned* vault rendered as an ordinary, successful, **empty**
one — "Notes (0)", no error — while every note tool the same user called over
MCP failed.

The fix is an `{% if vault_error %}` alert at the top of the page body,
rendered for admins and non-admins alike (a non-admin has no other vault
surface in the panel, and the admin-only users list is where the
"(unassigned)" warning lives).
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


def _render_vault_page(**overrides) -> str:
    """Render vault.html with the exact context `vault_page` builds for the
    RuntimeError branch (src/control_panel/routes.py)."""
    context = {
        "active": "vault",
        "is_admin": True,
        "multi_user_mode": True,
        "username": "bob",
        "csrf_token": "test-csrf-token",
        "current_folder": "",
        "breadcrumbs": [],
        "folders": [],
        "notes": [],
        "selected_note": None,
        "note_content": None,
        "note_title": None,
        "note_tags": [],
    }
    context.update(overrides)
    templates = Jinja2Templates(directory=TEMPLATES_DIR)
    response = templates.TemplateResponse(
        request=None,  # vault.html never touches `request` directly
        name="vault.html",
        context=context,
    )
    return response.body.decode()


def _alert_block(html: str) -> str | None:
    """The rendered `alert alert-warning` block, if any."""
    marker = 'class="alert alert-warning'
    idx = html.find(marker)
    if idx == -1:
        return None
    end = html.find("</div>", idx)
    return html[idx:end]


def test_unassigned_vault_renders_a_warning_alert():
    html = _render_vault_page(vault_error="Vault path for user_id=7 is not in cache.")
    block = _alert_block(html)
    assert block is not None, "vault_error produced no alert — the bug in #70"
    assert "vault" in block.lower()
    # It must tell the reader what to do about it.
    assert "admin" in block.lower()


def test_alert_is_absent_on_a_healthy_vault():
    html = _render_vault_page(
        notes=[{"name": "Inbox", "path": "Inbox.md"}],
    )
    assert _alert_block(html) is None
    assert "Inbox" in html


def test_alert_is_absent_when_a_healthy_vault_is_genuinely_empty():
    """The distinction the page could not previously make: zero notes and no
    assignment problem must NOT show the alert."""
    html = _render_vault_page()
    assert _alert_block(html) is None
    assert "Notes" in html


def test_alert_renders_for_a_non_admin_too():
    """#70 explicitly requires this — pointing a non-admin at the admin-only
    users list would be useless, so the alert may not be admin-gated."""
    html = _render_vault_page(
        is_admin=False, vault_error="Vault path for user_id=7 is not in cache."
    )
    block = _alert_block(html)
    assert block is not None
    assert "admin" in block.lower()


def test_vault_page_still_passes_vault_error_to_the_template():
    """Guard the other half of the contract: the handler's RuntimeError branch
    is what supplies the key the template now reads."""
    import inspect

    source = inspect.getsource(routes.vault_page)
    assert '"vault_error"' in source

    with open(os.path.join(TEMPLATES_DIR, "vault.html")) as fh:
        assert "vault_error" in fh.read()
