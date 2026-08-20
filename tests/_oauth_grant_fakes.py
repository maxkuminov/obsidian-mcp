"""In-memory doubles for the OAuth grant-family tests (#64/#65/#67/#68/#76).

Not a test module -- the leading underscore keeps pytest from collecting it.

The point of these fakes is that the *production* handlers run: `oauth_page`
really groups rows into families and derives `has_write`, `revoke_oauth_token`
really issues the family UPDATE, `_handle_refresh` really re-clamps. Only the
database is replaced. A fixture that hand-builds the template context instead
is what left issue #65 gap 1 open -- it asserted the template consumes
`has_write` and never that the route derives it.

`FakeSession` therefore interprets real SQLAlchemy statements rather than
handing back canned results in call order: it dispatches on the entity the
statement selects, and applies `update()` statements to the in-memory rows so a
family write is observable exactly the way it would be in Postgres.
"""
from __future__ import annotations

import datetime

from sqlalchemy.sql.dml import Update

from src.models.db import OAuthClient, OAuthToken, User
from src.oauth.grants import USER_BOOTSTRAP_LOCK_KEY

UTC = datetime.timezone.utc


def utcnow() -> datetime.datetime:
    return datetime.datetime.now(UTC)


def in_hours(hours: float) -> datetime.datetime:
    return utcnow() + datetime.timedelta(hours=hours)


class FakeToken:
    """An `OAuthToken` row, with only the attributes the handlers read."""

    _next_id = 1

    def __init__(
        self,
        *,
        grant_id: str,
        token_type: str = "access",
        scope: str = "read",
        client_id: str = "client123",
        user_id: int | None = None,
        revoked: bool = False,
        expires_at: datetime.datetime | None = None,
        created_at: datetime.datetime | None = None,
        token_hash: str | None = None,
        id: int | None = None,
    ):
        if id is None:
            id = FakeToken._next_id
            FakeToken._next_id += 1
        self.id = id
        self.grant_id = grant_id
        self.token_type = token_type
        self.scope = scope
        self.client_id = client_id
        self.user_id = user_id
        self.revoked = revoked
        self.expires_at = expires_at or in_hours(1)
        self.created_at = created_at or utcnow()
        self.token_hash = token_hash

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"FakeToken(id={self.id}, type={self.token_type!r}, "
            f"grant={self.grant_id!r}, scope={self.scope!r}, revoked={self.revoked})"
        )


class FakeClient:
    def __init__(
        self,
        *,
        client_id: str = "client123",
        client_name: str = "Claude",
        scope: str = "read readwrite offline_access",
        user_id: int | None = None,
        redirect_uris: list[str] | None = None,
        created_at: datetime.datetime | None = None,
        client_secret_hash: str | None = None,
        token_endpoint_auth_method: str = "none",
    ):
        self.client_id = client_id
        self.client_name = client_name
        self.scope = scope
        self.user_id = user_id
        self.redirect_uris = redirect_uris or ["https://example.test/cb"]
        self.created_at = created_at or utcnow()
        self.client_secret_hash = client_secret_hash
        self.token_endpoint_auth_method = token_endpoint_auth_method


class FakeUser:
    def __init__(self, *, id: int, is_active: bool = True, is_admin: bool = False):
        self.id = id
        self.is_active = is_active
        self.is_admin = is_admin
        self.username = f"user{id}"


class SingleUserSentinel:
    """Mirrors `src.auth.session._SingleUserSentinel` for the panel handlers."""

    id = None
    is_admin = True
    username = "admin"


class _Scalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _Result:
    def __init__(self, rows, tuples=None, rowcount=0):
        self._rows = list(rows)
        self._tuples = tuples
        self.rowcount = rowcount

    def scalars(self):
        return _Scalars(self._rows)

    def all(self):
        return list(self._tuples if self._tuples is not None else self._rows)

    def scalar_one_or_none(self):
        if not self._rows:
            return None
        return self._rows[0]

    def scalar(self):
        return self.scalar_one_or_none()


def _entity(stmt):
    try:
        descriptions = stmt.column_descriptions
    except Exception:  # pragma: no cover - non-ORM statement (e.g. text())
        return None
    return descriptions[0]["entity"] if descriptions else None


def _params(stmt) -> dict:
    try:
        return dict(stmt.compile().params)
    except Exception:  # pragma: no cover
        return {}


def _is_advisory_lock(stmt) -> bool:
    """Is this the textual `pg_advisory_xact_lock` statement, specifically?"""
    return "pg_advisory_xact_lock" in str(stmt)


def _window_value(clause):
    return None if clause is None else clause.value


def _apply_window(stmt, rows: list) -> list:
    """Honour LIMIT/OFFSET, so a fake cannot hide a truncation bug.

    The panel's per-client scan is bounded, and a bound applied at the wrong
    point can push a live grant off the page entirely with no control to
    revoke it. A fake that ignored LIMIT would render that page complete.
    """
    offset = _window_value(getattr(stmt, "_offset_clause", None))
    limit = _window_value(getattr(stmt, "_limit_clause", None))
    if offset:
        rows = rows[offset:]
    if limit is not None:
        rows = rows[:limit]
    return rows


class FakeSession:
    """Interprets the handful of statement shapes the OAuth surfaces issue.

    Deliberately narrow: anything it does not recognise raises, so a handler
    that starts issuing a *different* query fails the test loudly instead of
    silently receiving an empty result and appearing to work.
    """

    def __init__(self, *, clients=(), tokens=(), users=()):
        self.clients = list(clients)
        self.tokens = list(tokens)
        self.users = list(users)
        self.committed = 0
        self.rolled_back = 0
        self.deleted: list = []
        self.added: list = []
        # Every advisory lock the handler took, in acquisition order. Order is
        # itself the property under test -- the bootstrap lock must precede the
        # grant lock, and both must precede any family read or write.
        self.advisory_locks: list[int] = []

    @property
    def locked_grants(self) -> list[int]:
        """Advisory locks other than the constant bootstrap key."""
        return [key for key in self.advisory_locks if key != USER_BOOTSTRAP_LOCK_KEY]

    # -- async session surface ---------------------------------------------

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed += 1

    async def rollback(self):
        self.rolled_back += 1

    async def close(self):
        pass

    async def delete(self, obj):
        self.deleted.append(obj)

    async def execute(self, stmt, params=None):
        # `lock_grant` and `lock_user_bootstrap` issue a textual
        # `pg_advisory_xact_lock`. Recording it is how the tests prove the
        # locks are actually taken, and in which order -- the ordering that
        # stops a concurrent refresh from surviving a revocation, and a
        # concurrent mint from escaping the bootstrap's claim.
        #
        # Matched on the *statement text*, not merely on the presence of a
        # `key` parameter: a handler that passed `{"key": ...}` to some other
        # query would otherwise be recorded as holding a lock it never took.
        if _is_advisory_lock(stmt):
            assert params and "key" in params, "advisory lock issued without a key"
            self.advisory_locks.append(params["key"])
            return _Result([])

        if isinstance(stmt, Update):
            return self._apply_update(stmt)

        entity = _entity(stmt)
        bound = _params(stmt)

        if entity is OAuthClient:
            rows = list(self.clients)
            if "client_id_1" in bound:
                rows = [c for c in rows if c.client_id == bound["client_id_1"]]
            if "user_id_1" in bound:
                rows = [c for c in rows if c.user_id == bound["user_id_1"]]
            return _Result(rows)

        if entity is OAuthToken:
            rows = self._filter_tokens(stmt, bound)
            rows.sort(key=lambda t: (t.created_at, t.id), reverse=True)
            rows = _apply_window(stmt, rows)
            # `select(OAuthToken.grant_id)` (the refresh handler's family
            # lookup) wants the column, not the row.
            if getattr(stmt.column_descriptions[0].get("expr", None), "key", None) == "grant_id":
                return _Result([t.grant_id for t in rows])
            return _Result(rows)

        if entity is User:
            wanted = bound.get("id_1")
            rows = self.users
            if isinstance(wanted, (list, tuple, set)):
                rows = [u for u in rows if u.id in wanted]
            return _Result(rows, tuples=[(u.id, u.is_active) for u in rows])

        raise AssertionError(f"FakeSession got an unexpected statement: {stmt}")

    # -- predicate interpretation ------------------------------------------

    # Every `oauth_tokens` predicate the OAuth surfaces use, and how to read it
    # off a compiled statement. Selects and updates go through the same code so
    # a family UPDATE that quietly narrows itself (say, to `token_type =
    # 'access'`) is *observed* by the fake instead of being flattened into the
    # correct behaviour -- which is the difference between a test that pins
    # "revocation covers the family" and one that cannot fail.
    _TOKEN_PREDICATES = (
        ("id_1", "id"),
        ("client_id_1", "client_id"),
        ("user_id_1", "user_id"),
        ("token_hash_1", "token_hash"),
        ("token_type_1", "token_type"),
        ("grant_id_1", "grant_id"),
        ("scope_1", "scope"),
    )

    def _filter_tokens(self, stmt, bound: dict) -> list:
        rows = list(self.tokens)
        rendered = str(stmt)
        # `revoked == False` / `== True` compile to SQL literals rather than
        # bind params, so they have to be read off the rendered statement.
        if "oauth_tokens.revoked = false" in rendered:
            rows = [t for t in rows if not t.revoked]
        if "oauth_tokens.revoked = true" in rendered:
            rows = [t for t in rows if t.revoked]
        for param, attribute in self._TOKEN_PREDICATES:
            if param in bound:
                rows = [t for t in rows if getattr(t, attribute) == bound[param]]
        return rows

    def _apply_update(self, stmt):
        if stmt.table.name != "oauth_tokens":
            raise AssertionError(f"unexpected UPDATE target: {stmt.table.name}")
        values = {col.key: bind.value for col, bind in stmt._values.items()}
        bound = _params(stmt)
        if "grant_id_1" not in bound:
            raise AssertionError("family UPDATE must be keyed on grant_id")
        # The SET clause's own bind params share the column names, so they must
        # not be mistaken for WHERE predicates.
        where_bound = {k: v for k, v in bound.items() if k.endswith("_1")}
        targets = self._filter_tokens(stmt, where_bound)
        for token in targets:
            for key, value in values.items():
                setattr(token, key, value)
        return _Result([], rowcount=len(targets))


class FakeRequest:
    """Enough of a Starlette request for `_panel_context` / TemplateResponse.

    A real `starlette.requests.Request` over a minimal scope: `request.session`
    raises without SessionMiddleware, which `generate_csrf_token` already
    handles by returning "" (single-user mode has no CSRF token).
    """

    def __new__(cls, path: str = "/admin/oauth"):
        from starlette.requests import Request

        return Request(
            {
                "type": "http",
                "http_version": "1.1",
                "method": "GET",
                "scheme": "https",
                "path": path,
                "raw_path": path.encode(),
                "query_string": b"",
                "root_path": "",
                "headers": [],
                "client": ("127.0.0.1", 12345),
                "server": ("testserver", 443),
            }
        )


class SeqSession:
    """Returns canned rows in call order -- enough for `_handle_auth_code`.

    Advisory locks are recorded rather than consumed from the result sequence,
    so a handler that starts taking one does not silently shift every
    subsequent canned row by one.
    """

    def __init__(self, results=()):
        self._results = iter(results)
        self.added: list = []
        self.committed = False
        self.advisory_locks: list[int] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def execute(self, stmt, params=None, *_a, **_kw):
        if _is_advisory_lock(stmt):
            assert params and "key" in params, "advisory lock issued without a key"
            self.advisory_locks.append(params["key"])
            return _Result([])
        value = next(self._results)
        return _Result([value] if value is not None else [])

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed = True

    async def rollback(self):
        pass
