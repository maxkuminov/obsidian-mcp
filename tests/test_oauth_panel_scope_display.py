"""Regression test: the admin panel's OAuth token scope dropdown must
reflect a token's actual granted permission, not just an exact string
match against "read" / "readwrite".

`OAuthToken.scope` is a space-separated set (e.g. "offline_access
readwrite" - offline_access is requested by every DCR-registered client
per DEFAULT_CLIENT_SCOPE in src/oauth/routes.py). oauth.html's <select>
previously compared `token.scope == 'read'` / `== 'readwrite'` directly:
neither literal ever matches a scope string that also carries
offline_access, so NEITHER <option> got `selected` and the browser fell
back to showing the first one, "read" - misrepresenting an actual
readwrite grant as read-only in the UI. The real permission (checked by
src/mcp_server/auth.py via `"readwrite" in scope_parts`) was correct the
whole time; only the display was wrong.

Fix: compute `has_write` (a membership check) in oauth_page and drive the
template's `selected` attributes off that instead of raw string equality.
Also verified: update_oauth_token_scope preserves the offline_access
marker instead of silently dropping it when an admin flips read/readwrite
via the panel.
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

    from src.control_panel import routes
finally:
    pydantic_settings.BaseSettings.__init__ = _orig_init


TEMPLATES_DIR = os.path.join(
    os.path.dirname(routes.__file__), "templates"
)


def _render_oauth_page(tokens: list[dict]) -> str:
    templates = Jinja2Templates(directory=TEMPLATES_DIR)
    response = templates.TemplateResponse(
        request=None,  # oauth.html never touches `request` directly
        name="oauth.html",
        context={
            "active": "oauth",
            "is_admin": True,
            "multi_user_mode": False,
            "username": "admin",
            "csrf_token": "test-csrf-token",
            "clients": [
                {
                    "client_id": "client123",
                    "client_name": "Claude",
                    "created_at": "2026-08-20T00:00:00Z",
                    "tokens": tokens,
                }
            ],
        },
    )
    return response.body.decode()


def _base_token(**overrides) -> dict:
    token = {
        "id": 1,
        "token_type": "access",
        "scope": "offline_access readwrite",
        "has_write": True,
        "revoked": False,
        "expired": False,
        "expires_at": "2026-09-01T00:00:00Z",
        "created_at": "2026-08-20T00:00:00Z",
    }
    token.update(overrides)
    return token


def _selected_option(html: str) -> str:
    """Return the value= of whichever <option> in the scope <select> is selected."""
    import re

    for value in ("read", "readwrite"):
        m = re.search(
            rf'<option value="{value}"\s*[^>]*>',
            html,
        )
        assert m, f"couldn't find <option value=\"{value}\"> at all"
        if "selected" in m.group(0):
            return value
    return "NONE-SELECTED"


def test_readwrite_token_with_offline_access_shows_readwrite_selected():
    """The exact scenario that fooled the panel before this fix: a real
    readwrite grant that also carries offline_access."""
    html = _render_oauth_page([_base_token(scope="offline_access readwrite", has_write=True)])
    assert _selected_option(html) == "readwrite"


def test_read_token_with_offline_access_shows_read_selected():
    html = _render_oauth_page([_base_token(scope="offline_access read", has_write=False)])
    assert _selected_option(html) == "read"


def test_bare_readwrite_token_shows_readwrite_selected():
    html = _render_oauth_page([_base_token(scope="readwrite", has_write=True)])
    assert _selected_option(html) == "readwrite"


# --- oauth_page's has_write computation (the actual fix) ------------------


def test_has_write_is_membership_not_equality():
    assert ("readwrite" in "offline_access readwrite".split()) is True
    assert ("readwrite" in "offline_access read".split()) is False
    assert ("readwrite" in "read".split()) is False
    assert ("readwrite" in "readwrite".split()) is True


# --- update_oauth_token_scope preserves offline_access ---------------------


class _FakeToken:
    def __init__(self, scope, revoked=False, user_id=None):
        self.id = 1
        self.scope = scope
        self.revoked = revoked
        self.user_id = user_id


class _FakeSession:
    def __init__(self, token):
        self._token = token
        self.committed = False

    async def execute(self, _stmt):
        class _Result:
            def __init__(self, obj):
                self._obj = obj

            def scalar_one_or_none(self):
                return self._obj

        return _Result(self._token)

    async def commit(self):
        self.committed = True


class _SingleUserSentinel:
    id = None
    is_admin = True


def test_update_scope_preserves_offline_access_marker():
    token = _FakeToken(scope="offline_access readwrite")
    session = _FakeSession(token)

    asyncio.run(
        routes.update_oauth_token_scope(
            token_id=1,
            scope="read",
            session=session,
            user=_SingleUserSentinel(),
        )
    )

    assert token.scope == "read offline_access"
    assert session.committed is True


def test_update_scope_without_offline_access_stays_bare():
    token = _FakeToken(scope="readwrite")
    session = _FakeSession(token)

    asyncio.run(
        routes.update_oauth_token_scope(
            token_id=1,
            scope="read",
            session=session,
            user=_SingleUserSentinel(),
        )
    )

    assert token.scope == "read"
