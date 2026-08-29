"""Regression test (#76): the panel's key surfaces must apply the same
liveness predicate `APIKeyMiddleware` does.

Two defects, one cause — the panel used a weaker predicate than auth:

1. `keys.html` badged Status from `api_keys.is_active` alone. `auth.py`
   additionally selects `User.is_active` for the key's `user_id` and 401s
   (reason=inactive_user) unless it is exactly True, and `delete_user` sets
   `is_active=False` *without touching that user's keys* — so after
   deactivating a user the panel showed their dead keys green/"Active".
   There was also no owner column at all, so the finding would have been
   unattributable even once badged.

2. The users list counted `count(APIKey.id)` with no `is_active` filter, so
   revoking every one of a user's keys left the number unchanged — and no
   panel surface anywhere would have shown otherwise.

Both bite only in multi_user_mode; single-user keys carry `user_id=NULL`,
which the middleware exempts from the owner check (and the outer join here
keeps).
"""
import asyncio
import os
from datetime import datetime, timedelta, timezone

import pydantic_settings

_orig_init = pydantic_settings.BaseSettings.__init__


def _no_env_file_init(self, *args, **kwargs):
    kwargs.setdefault("_env_file", None)
    _orig_init(self, *args, **kwargs)


pydantic_settings.BaseSettings.__init__ = _no_env_file_init
try:
    from starlette.templating import Jinja2Templates

    from src.control_panel import routes
    from src.control_panel import users as users_mod
finally:
    pydantic_settings.BaseSettings.__init__ = _orig_init


TEMPLATES_DIR = os.path.join(os.path.dirname(routes.__file__), "templates")


# --- keys_page: effective status is a join, not a column ------------------


class _KeyRow:
    """Stand-in for an APIKey ORM row."""

    def __init__(self, id, user_id, is_active=True, name="k", permission="read",
                 expires_at=None, daily_request_limit=None):
        self.id = id
        self.name = name
        self.key_prefix = "omcp_abcdef"
        self.permission = permission
        self.is_active = is_active
        self.user_id = user_id
        self.created_at = _FixedTime()
        self.last_used_at = None
        self.expires_at = expires_at
        # NULL is the default and means unlimited (#162). Liveness and quota
        # are independent facts about a key — a key at its ceiling is still
        # live, it is just refusing today — so every case here leaves it unset.
        self.daily_request_limit = daily_request_limit


class _FixedTime:
    def isoformat(self):
        return "2026-08-20T00:00:00+00:00"


class _KeysResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def fetchall(self):
        return self._rows


class _KeysSession:
    """The keys page issues two statements: the key/owner join, then the quota
    counters for today (#162). The second is parameterised, which is how they
    are told apart here — the counter read returns nothing, so every key on
    this page renders 0 consumed, which is what a key with no counter row
    means."""

    def __init__(self, rows):
        self._rows = rows
        self.statements = []

    async def execute(self, stmt, params=None):
        self.statements.append(stmt)
        if params is not None:
            return _KeysResult([])
        return _KeysResult(self._rows)


class _Request:
    """Minimal stand-in: keys_page only pops a flash off `request.session`,
    and `_panel_context` needs it for the CSRF token."""

    def __init__(self):
        self.session = {}
        self.scope = {}


class _AdminUser:
    id = 1
    username = "max"
    is_admin = True
    is_active = True


def _keys_context(rows, monkeypatch):
    captured = {}

    def _fake_response(request, name, context):
        captured["name"] = name
        captured["context"] = context
        return None

    monkeypatch.setattr(routes.templates, "TemplateResponse", _fake_response)
    monkeypatch.setattr(routes, "generate_csrf_token", lambda _r: "csrf")
    session = _KeysSession(rows)
    asyncio.run(routes.keys_page(request=_Request(), session=session, user=_AdminUser()))
    return captured["context"], session


def test_key_of_a_deactivated_owner_is_not_effectively_active(monkeypatch):
    rows = [(_KeyRow(id=1, user_id=2), "bob", False)]
    ctx, _ = _keys_context(rows, monkeypatch)
    key = ctx["keys"][0]
    assert key["is_active"] is True, "the key's own column is untouched by deactivation"
    assert key["owner_is_active"] is False
    assert key["effective_active"] is False
    assert key["owner_username"] == "bob"


def test_key_of_an_active_owner_is_effectively_active(monkeypatch):
    rows = [(_KeyRow(id=1, user_id=2), "bob", True)]
    ctx, _ = _keys_context(rows, monkeypatch)
    key = ctx["keys"][0]
    assert key["effective_active"] is True
    assert key["owner_is_active"] is True


def test_single_user_key_with_null_owner_stays_active(monkeypatch):
    """`user_id IS NULL` is single-user mode; auth.py skips the owner check
    entirely there, so the panel must not invent an owner problem."""
    rows = [(_KeyRow(id=1, user_id=None), None, None)]
    ctx, _ = _keys_context(rows, monkeypatch)
    key = ctx["keys"][0]
    assert key["owner_is_active"] is True
    assert key["effective_active"] is True
    assert key["owner_username"] is None


def test_key_whose_owner_row_is_missing_is_not_active(monkeypatch):
    """An outer join yields NULLs for a key pointing at a row that isn't
    there. The middleware's `is True` test fails for it, so this must not
    read as live."""
    rows = [(_KeyRow(id=1, user_id=99), None, None)]
    ctx, _ = _keys_context(rows, monkeypatch)
    key = ctx["keys"][0]
    assert key["owner_is_active"] is False
    assert key["effective_active"] is False


def test_revoked_key_is_never_effectively_active(monkeypatch):
    rows = [(_KeyRow(id=1, user_id=2, is_active=False), "bob", True)]
    ctx, _ = _keys_context(rows, monkeypatch)
    assert ctx["keys"][0]["effective_active"] is False


# --- expiry: the third half of the middleware's predicate ------------------


def test_expired_key_is_not_effectively_active(monkeypatch):
    """`auth.py` 401s (reason=key_expired) on `expires_at < now`. The panel
    must apply that too, or it badges a dead key green the day anything
    starts setting the column."""
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    rows = [(_KeyRow(id=1, user_id=2, expires_at=past), "bob", True)]
    ctx, _ = _keys_context(rows, monkeypatch)
    key = ctx["keys"][0]
    assert key["is_active"] is True
    assert key["owner_is_active"] is True
    assert key["is_expired"] is True
    assert key["effective_active"] is False


def test_unexpired_key_stays_active(monkeypatch):
    future = datetime.now(timezone.utc) + timedelta(days=30)
    rows = [(_KeyRow(id=1, user_id=2, expires_at=future), "bob", True)]
    ctx, _ = _keys_context(rows, monkeypatch)
    assert ctx["keys"][0]["is_expired"] is False
    assert ctx["keys"][0]["effective_active"] is True


def test_null_expires_at_never_expires(monkeypatch):
    rows = [(_KeyRow(id=1, user_id=2, expires_at=None), "bob", True)]
    ctx, _ = _keys_context(rows, monkeypatch)
    assert ctx["keys"][0]["is_expired"] is False
    assert ctx["keys"][0]["effective_active"] is True


def test_the_boundary_instant_matches_the_middleware(monkeypatch):
    """The middleware rejects on `expires_at < now`, so a key whose
    `expires_at` is exactly now is still accepted. The panel must draw the
    boundary on the same side rather than inventing a `>=` of its own."""
    fixed = datetime(2026, 8, 20, 12, 0, 0, tzinfo=timezone.utc)

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed

    monkeypatch.setattr(routes, "datetime", _FrozenDatetime)
    rows = [(_KeyRow(id=1, user_id=2, expires_at=fixed), "bob", True)]
    ctx, _ = _keys_context(rows, monkeypatch)
    assert ctx["keys"][0]["is_expired"] is False, (
        "at exactly expires_at the middleware still authenticates the key"
    )
    assert ctx["keys"][0]["effective_active"] is True

    # One microsecond later it is expired on both sides.
    rows = [(
        _KeyRow(id=1, user_id=2, expires_at=fixed - timedelta(microseconds=1)),
        "bob",
        True,
    )]
    ctx, _ = _keys_context(rows, monkeypatch)
    assert ctx["keys"][0]["is_expired"] is True


def test_middleware_still_uses_the_comparison_this_mirrors():
    """If auth.py's boundary ever moves, this pairing must be revisited —
    fail here rather than let the two surfaces drift apart silently."""
    import inspect

    from src.mcp_server import auth

    src = inspect.getsource(auth)
    assert "api_key.expires_at < datetime.now(timezone.utc)" in src


def test_expired_key_renders_its_own_badge():
    html = _render_keys_page(
        [_key_ctx(is_expired=True, effective_active=False)]
    )
    assert _status_badge(html) == "Expired"
    assert 'class="badge badge-green"' not in html


def test_owner_inactive_beats_expired_in_the_badge():
    """A key that is both expired and owner-inactive: `auth.py` checks the
    owner's `is_active` first and 401s there, so the badge must name the
    reason the middleware would actually give."""
    html = _render_keys_page([
        _key_ctx(owner_is_active=False, is_expired=True, effective_active=False)
    ])
    assert _status_badge(html) == "Owner inactive"
    assert 'class="badge badge-green"' not in html


def test_middleware_checks_the_owner_before_expiry():
    """The ordering this badge mirrors. If auth.py ever checks expiry first,
    this fails and the template should follow it."""
    import inspect

    from src.mcp_server import auth

    src = inspect.getsource(auth)
    owner_at = src.index('"reason": "inactive_user"')
    expiry_at = src.index("api_key.expires_at < datetime.now(timezone.utc)")
    assert owner_at < expiry_at


def test_revoked_beats_expired_in_the_badge():
    html = _render_keys_page(
        [_key_ctx(is_active=False, is_expired=True, effective_active=False)]
    )
    assert _status_badge(html) == "Revoked"


def test_keys_query_outer_joins_the_owner(monkeypatch):
    """The join must be an OUTER join — an inner one would drop every
    single-user key (`user_id IS NULL`) off the page entirely."""
    rows = [(_KeyRow(id=1, user_id=2), "bob", True)]
    _, session = _keys_context(rows, monkeypatch)
    sql = str(session.statements[0].compile()).upper()
    assert "LEFT OUTER JOIN USERS" in sql


# --- keys.html: the badge and the owner column ----------------------------


def _render_keys_page(keys, multi_user_mode=True) -> str:
    templates = Jinja2Templates(directory=TEMPLATES_DIR)
    response = templates.TemplateResponse(
        request=None,  # keys.html never touches `request` directly
        name="keys.html",
        context={
            "active": "keys",
            "is_admin": True,
            "multi_user_mode": multi_user_mode,
            "username": "max",
            "csrf_token": "csrf",
            "keys": keys,
            "new_key": None,
        },
    )
    return response.body.decode()


def _key_ctx(**overrides) -> dict:
    key = {
        "id": 1,
        "name": "laptop",
        "key_prefix": "omcp_abcdef",
        "permission": "read",
        "is_active": True,
        "owner_is_active": True,
        "is_expired": False,
        "expires_at": None,
        "effective_active": True,
        "owner_username": "bob",
        "created_at": "2026-08-20T00:00:00+00:00",
        "last_used_at": None,
    }
    key.update(overrides)
    return key


def _status_badge(html: str) -> str:
    for label in ("Active", "Revoked", "Expired", "Owner inactive"):
        if f">{label}<" in html:
            return label
    return "NONE"


def test_deactivated_owner_does_not_render_a_green_active_badge():
    html = _render_keys_page(
        [_key_ctx(owner_is_active=False, effective_active=False)]
    )
    assert 'class="badge badge-green"' not in html  # the stylesheet defines it
    assert _status_badge(html) == "Owner inactive"


def test_live_key_still_renders_active():
    html = _render_keys_page([_key_ctx()])
    assert _status_badge(html) == "Active"
    assert "badge-green" in html


def test_revoked_key_still_renders_revoked():
    html = _render_keys_page(
        [_key_ctx(is_active=False, effective_active=False)]
    )
    assert _status_badge(html) == "Revoked"


def test_owner_column_is_rendered_in_multi_user_mode():
    html = _render_keys_page([_key_ctx(owner_username="bob")])
    assert "<th>Owner</th>" in html
    assert "bob" in html


def test_owner_column_is_hidden_in_single_user_mode():
    html = _render_keys_page(
        [_key_ctx(owner_username=None)], multi_user_mode=False
    )
    assert "<th>Owner</th>" not in html


def test_revoke_action_still_offered_for_an_owner_inactive_key():
    """The key's own `is_active` is what revoke/delete key off — an
    owner-inactive key is still revocable, and must not be offered the
    permanent-delete action reserved for already-revoked keys."""
    html = _render_keys_page(
        [_key_ctx(owner_is_active=False, effective_active=False)]
    )
    assert "/revoke" in html
    assert "/delete" not in html


# --- users list: "N active / M total" -------------------------------------


class _CountRow:
    def __init__(self, user_id, total, active):
        self.user_id = user_id
        self.total = total
        self.active = active


class _ListResult:
    def __init__(self, rows=None, scalars=None):
        self._rows = rows or []
        self._scalars = scalars or []

    def all(self):
        return self._rows

    def scalars(self):
        outer = self

        class _S:
            def all(self_inner):
                return outer._scalars

        return _S()


class _ListSession:
    """Answers list_users' three queries: key counts, note counts, users."""

    def __init__(self, key_rows, users):
        self._key_rows = key_rows
        self._users = users
        self.statements = []

    async def execute(self, stmt):
        self.statements.append(stmt)
        n = len(self.statements)
        if n == 1:
            return _ListResult(rows=self._key_rows)
        if n == 2:
            return _ListResult(rows=[])
        return _ListResult(scalars=self._users)


class _UserRow:
    def __init__(self, id, username):
        self.id = id
        self.username = username
        self.is_admin = False
        self.is_active = True
        self.vault_path = "/vaults/bob"
        self.last_login_at = None
        self.created_at = _FixedTime()


def _list_users_context(key_rows, users, monkeypatch):
    captured = {}

    def _fake_response(request, name, context):
        captured["context"] = context
        return None

    monkeypatch.setattr(users_mod.templates, "TemplateResponse", _fake_response)
    monkeypatch.setattr(routes, "generate_csrf_token", lambda _r: "csrf")
    session = _ListSession(key_rows, users)

    class _Req:
        query_params: dict = {}
        scope: dict = {}

    asyncio.run(
        users_mod.list_users(request=_Req(), session=session, user=_AdminUser())
    )
    return captured["context"], session


def test_users_list_reports_active_and_total_separately(monkeypatch):
    """bob holds four keys, all revoked. The old column said "4"."""
    ctx, _ = _list_users_context(
        [_CountRow(user_id=2, total=4, active=0)],
        [_UserRow(2, "bob")],
        monkeypatch,
    )
    row = ctx["users"][0]
    assert row["api_keys_total"] == 4
    assert row["api_keys_active"] == 0


def test_users_list_counts_default_to_zero_for_a_user_with_no_keys(monkeypatch):
    ctx, _ = _list_users_context([], [_UserRow(2, "bob")], monkeypatch)
    row = ctx["users"][0]
    assert row["api_keys_total"] == 0
    assert row["api_keys_active"] == 0


def test_users_list_key_query_filters_the_active_count(monkeypatch):
    """Pin the SQL: the active count must be a filtered aggregate, not the
    same bare count under a different label."""
    _, session = _list_users_context([], [_UserRow(2, "bob")], monkeypatch)
    sql = str(session.statements[0].compile()).upper()
    assert "FILTER (WHERE" in sql
    assert "IS_ACTIVE IS TRUE" in sql


def _render_users_page(users) -> str:
    templates = Jinja2Templates(directory=TEMPLATES_DIR)
    response = templates.TemplateResponse(
        request=None,  # users.html never touches `request` directly
        name="users.html",
        context={
            "active": "users",
            "is_admin": True,
            "multi_user_mode": True,
            "username": "max",
            "csrf_token": "csrf",
            "users": users,
            "flash": None,
            "flash_kind": "ok",
            "error": None,
        },
    )
    return response.body.decode()


def test_users_html_renders_active_over_total():
    html = _render_users_page([
        {
            "id": 2,
            "username": "bob",
            "is_admin": False,
            "is_active": True,
            "vault_path": "/vaults/bob",
            "last_login_at": None,
            "created_at": "2026-08-20T00:00:00+00:00",
            "api_keys_active": 1,
            "api_keys_total": 4,
            "notes": 12,
        }
    ])
    assert "1 active / 4 total" in html
