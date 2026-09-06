"""One definition of "this test has a signed-in browser session" (#198).

Not a test module — the name is deliberate and pytest collects nothing here.

Before the session registry a test signed a browser in by writing
`{"user_id": 7}` into a dict, and fifteen modules did exactly that. Every one
of them was a hand-built cookie that could drift from what the validator
actually requires, and after #198 the validator requires a great deal more: a
`sid`, a row keyed on `sha256(sid)`, an unrevoked and unexpired row, and a row
whose `user_id` is the one the cookie names.

So the helper does not *describe* a session — it **mints** one, by driving the
production `start_session` against an in-memory registry. Guard, locked
re-read, insert, commit and cookie all run, which means a test session is
whatever the application would have produced, and a change to the mint that
the validator would reject fails here rather than in fifteen places at once.

Two pieces:

* `FakeRegistry` — an `AsyncSession` double that interprets the handful of
  statements the session lifecycle issues (the advisory lock, the locked user
  re-read, the row lookup, the two revocation updates, the last-seen touch).
  Anything it does not recognise raises, so a handler that starts issuing a
  *different* statement fails loudly instead of quietly receiving an empty
  result and appearing to work.
* `sign_in` / `sign_in_sync` — mint a session for a user against a registry
  and leave the cookie on the request.
"""
from __future__ import annotations

import asyncio
import datetime
from types import SimpleNamespace

from sqlalchemy.sql.dml import Update
from sqlalchemy.sql.elements import BindParameter

from src.auth.session import SESSION_ID_KEY, hash_session_id, start_session
from src.models.db import User, UserSession
from src.oauth.grants import ACCOUNT_GUARD_LOCK_KEY

UTC = datetime.timezone.utc

#: The advisory-lock statements this codebase issues, whole. Matched entire
#: rather than searched for, for `_oauth_grant_fakes`' reason: a substring test
#: counts a decoy — the name in a comment, or in a `text()` that also does
#: something else — as a lock that was never taken. Two spellings because
#: `register_submit` binds its key as `:k` and `src/oauth/grants.py` as `:key`.
ADVISORY_LOCK_SQL = "SELECT pg_advisory_xact_lock(:key)"
ADVISORY_LOCK_STATEMENTS = {
    ADVISORY_LOCK_SQL: "key",
    "SELECT pg_advisory_xact_lock(:k)": "k",
}


def utcnow() -> datetime.datetime:
    return datetime.datetime.now(UTC)


def fake_user(
    user_id: int = 7,
    *,
    username: str = "alice",
    is_admin: bool = False,
    is_active: bool = True,
    session_version: int = 1,
    password_hash: str = "$2b$12$" + "a" * 53,
):
    """The columns the mint and the validator read off a `users` row."""
    return SimpleNamespace(
        id=user_id,
        username=username,
        is_admin=is_admin,
        is_active=is_active,
        session_version=session_version,
        password_hash=password_hash,
    )


class _Row(tuple):
    """A multi-column result row: indexable like a tuple, attribute-readable
    like SQLAlchemy's `Row`, because callers use both."""

    def __new__(cls, names, values):
        row = super().__new__(cls, values)
        row._names = list(names)
        return row

    def __getattr__(self, name):
        try:
            return self[self._names.index(name)]
        except ValueError as exc:  # pragma: no cover - a column not selected
            raise AttributeError(name) from exc


class _Result:
    def __init__(self, rows, rowcount: int = 0):
        self._rows = list(rows)
        self.rowcount = rowcount

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None

    def scalar(self):
        return self.scalar_one_or_none()

    def scalars(self):
        return self

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return list(self._rows)


def _entity(stmt):
    try:
        descriptions = stmt.column_descriptions
    except Exception:  # pragma: no cover - non-ORM statement
        return None
    return descriptions[0]["entity"] if descriptions else None


def _params(stmt) -> dict:
    try:
        return dict(stmt.compile().params)
    except Exception:  # pragma: no cover
        return {}


def _normalized_sql(stmt) -> str:
    return " ".join(str(stmt).split())


def _advisory_lock_param(stmt) -> str | None:
    """The bind name this statement's key arrives under, or `None`."""
    return ADVISORY_LOCK_STATEMENTS.get(_normalized_sql(stmt))


class FakeRegistry:
    """An `AsyncSession` double for the session lifecycle. Interprets, not canned.

    Holds `users` and `sessions` in memory and applies real statements to them,
    so a revocation that quietly narrowed itself — or a validator that stopped
    filtering on `revoked_at` — is *observed* here rather than flattened into
    correct-looking behaviour.
    """

    def __init__(self, *, users=(), sessions=()):
        self.users = list(users)
        self.sessions = list(sessions)
        self.committed = 0
        self.rolled_back = 0
        self.added: list = []
        #: Every advisory lock taken, in order. The order is itself under test:
        #: the mint must take the guard *before* it re-reads the account.
        self.advisory_locks: list[int] = []
        #: Statements seen, so a test can assert what ran and in what order.
        self.statements: list[str] = []
        #: Locks and transaction boundaries in order, as `("lock", key)`,
        #: `("commit",)` and `("rollback",)`. This is what proves the bootstrap
        #: key and the account guard are taken **sequentially, never nested**.
        self.events: list[tuple] = []
        #: Called with the key each time an advisory lock is taken — the hook
        #: the race tests use to commit an administrator's deactivation in the
        #: window the guard exists to close.
        self.on_lock = None
        #: Set to raise from the next `execute` matching a substring.
        self.fail_on: str | None = None
        self.fail_with: Exception | None = None
        self.fail_rollback: Exception | None = None

    # -- async session surface --------------------------------------------

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    def add(self, obj):
        self.added.append(obj)
        if isinstance(obj, UserSession):
            self.sessions.append(obj)

    async def commit(self):
        self.committed += 1
        self.events.append(("commit",))

    async def rollback(self):
        self.rolled_back += 1
        self.events.append(("rollback",))
        if self.fail_rollback is not None:
            raise self.fail_rollback

    async def flush(self):
        """`register_submit` flushes to populate the new user's id, which a
        real session gets from the INSERT. Assign one the way a sequence
        would, so the bootstrap's backfill and its mint have a `uid`."""
        for obj in self.added:
            if isinstance(obj, User) and getattr(obj, "id", None) is None:
                obj.id = max((u.id for u in self.users), default=0) + 1
                self.users.append(obj)

    async def close(self):
        pass

    async def execute(self, stmt, params=None):
        rendered = _normalized_sql(stmt)
        self.statements.append(rendered)
        if self.fail_on is not None and self.fail_on in rendered:
            raise self.fail_with or RuntimeError("registry failure")

        lock_param = _advisory_lock_param(stmt)
        if lock_param is not None:
            assert params and lock_param in params, "advisory lock issued without a key"
            key = params[lock_param]
            self.advisory_locks.append(key)
            self.events.append(("lock", key))
            if self.on_lock is not None:
                self.on_lock(key)
            return _Result([])

        if isinstance(stmt, Update):
            return self._apply_update(stmt, rendered)

        entity = _entity(stmt)
        bound = _params(stmt)

        # `_users_table_empty`'s guard, and the panel dependency's own count.
        if rendered.startswith("SELECT count("):
            return _Result([len(self.users)])

        if entity is User:
            rows = list(self.users)
            if "id_1" in bound:
                rows = [u for u in rows if u.id == bound["id_1"]]
            # `warm_user_vault_cache` narrows on both of these, and they
            # compile to SQL literals rather than bind parameters.
            if "users.is_active IS true" in rendered:
                rows = [u for u in rows if getattr(u, "is_active", False)]
            if "users.vault_path IS NOT NULL" in rendered:
                rows = [u for u in rows if getattr(u, "vault_path", None) is not None]
            descriptions = stmt.column_descriptions
            if len(descriptions) > 1:
                names = [d.get("name") for d in descriptions]
                return _Result(
                    [
                        _Row(names, [getattr(u, name, None) for name in names])
                        for u in rows
                    ]
                )
            return _Result(rows)

        if entity is UserSession:
            wanted = bound.get("id_1")
            return _Result([s for s in self.sessions if s.id == wanted])

        raise AssertionError(f"FakeRegistry got an unexpected statement: {stmt}")

    # -- statement interpretation -----------------------------------------

    @staticmethod
    def _set_values(stmt) -> dict:
        """The SET clause. `func.now()` is not a bind parameter, so it is read
        as "the server's clock" rather than dropped."""
        values = {}
        for column, expression in stmt._values.items():
            if isinstance(expression, BindParameter):
                values[column.key] = expression.value
            else:
                values[column.key] = utcnow()
        return values

    #: Tables whose `UPDATE`s this fake accepts and ignores: the bootstrap
    #: backfill and `last_login_at`. It models the session registry, and
    #: pretending to model six other tables is how a fake starts lying.
    IGNORED_UPDATE_TABLES = frozenset(
        {
            "users",
            "api_keys",
            "oauth_clients",
            "oauth_tokens",
            "oauth_codes",
            "notes_metadata",
            "usage_logs",
        }
    )

    def _apply_update(self, stmt, rendered: str):
        if stmt.table.name in self.IGNORED_UPDATE_TABLES:
            return _Result([], rowcount=0)
        if stmt.table.name != "user_sessions":
            raise AssertionError(f"unexpected UPDATE target: {stmt.table.name}")
        bound = _params(stmt)
        rows = list(self.sessions)
        if "id_1" in bound:
            rows = [r for r in rows if r.id == bound["id_1"]]
        if "user_id_1" in bound:
            rows = [r for r in rows if r.user_id == bound["user_id_1"]]
        # `revoked_at IS NULL` compiles to a SQL literal, not a bind parameter,
        # so it has to be read off the rendered statement. Without it a second
        # revocation would rewrite a historical revocation time — which is the
        # thing the predicate exists to prevent.
        if "user_sessions.revoked_at IS NULL" in rendered:
            rows = [r for r in rows if r.revoked_at is None]
        values = self._set_values(stmt)
        for row in rows:
            for name, value in values.items():
                setattr(row, name, value)
        return _Result([], rowcount=len(rows))


def browser_request(
    *,
    method: str = "GET",
    path: str = "/admin/",
    session: dict | None = None,
    query: str = "",
    user_agent: str = "pytest",
    client: tuple[str, int] = ("203.0.113.9", 44444),
    cookies: dict[str, str] | None = None,
):
    """A **real** `starlette.requests.Request` carrying a session dict.

    Real rather than a stand-in, for three reasons the lifecycle depends on:
    `request.session` is the dict `SessionMiddleware` installs and behaves like
    one under `clear()`; `request.url.path` parses the way it does in
    production; and slowapi's decorator on `login_submit` refuses anything that
    is not a `Request` at all.

    `cookies` is rendered into a real `Cookie` header, so `request.cookies` is
    whatever Starlette parses rather than a dict a test asserted into place.
    """
    from starlette.requests import Request

    raw_path = path.encode()
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": raw_path,
            "root_path": "",
            "query_string": query.encode(),
            "headers": [
                (b"host", b"testserver"),
                (b"user-agent", user_agent.encode()),
            ]
            + (
                # Real `Cookie` header rather than a `cookies` attribute, so
                # Starlette's own parser produces `request.cookies` — the
                # OAuth consent POST reads its signed state from there.
                [
                    (
                        b"cookie",
                        "; ".join(f"{k}={v}" for k, v in cookies.items()).encode(),
                    )
                ]
                if cookies
                else []
            ),
            "client": client,
            "server": ("testserver", 80),
            "session": {} if session is None else session,
            "state": {},
            "app": None,
        }
    )


def session_row(
    user_id: int,
    *,
    sid: str,
    created_at: datetime.datetime | None = None,
    last_seen_at: datetime.datetime | None = None,
    expires_at: datetime.datetime | None = None,
    revoked_at: datetime.datetime | None = None,
) -> UserSession:
    """A registry row for `sid`, keyed the way the application keys it.

    For the negative cases — an expired row, a revoked row, a row belonging to
    somebody else. The positive case should go through `sign_in`, which runs
    the real mint.
    """
    now = created_at or utcnow()
    return UserSession(
        id=hash_session_id(sid),
        user_id=user_id,
        created_at=now,
        last_seen_at=last_seen_at or now,
        expires_at=expires_at or (now + datetime.timedelta(days=7)),
        revoked_at=revoked_at,
        user_agent_hash=None,
    )


async def sign_in(user, *, registry: FakeRegistry | None = None, request=None):
    """Mint a session for `user` through the **production** `start_session`.

    Returns `(sid, request, registry)`. The cookie is left on `request.session`
    exactly as a real login leaves it, so a test that wants only the cookie can
    take `request.session` and one that wants the row can read
    `registry.sessions`.
    """
    if registry is None:
        registry = FakeRegistry(users=[user])
    elif user not in registry.users:
        registry.users.append(user)
    if request is None:
        request = browser_request()
    sid = await start_session(request, registry, user.id)
    assert sid is not None, "the mint refused; is the fake user active?"
    return sid, request, registry


def sign_in_sync(user, *, registry: FakeRegistry | None = None, request=None):
    """`sign_in` for a synchronous test."""
    return asyncio.run(sign_in(user, registry=registry, request=request))


def cookie_for(user, sid: str) -> dict:
    """The five keys a mint writes, for a test that cannot run the mint.

    Prefer `sign_in`. This exists for the modules that hand a plain dict to a
    handler and never touch a database at all; it is written from
    `SESSION_ID_KEY` and the same field names, so it moves when the mint moves.
    """
    return {
        "user_id": user.id,
        "session_version": user.session_version,
        "is_admin": bool(user.is_admin),
        "username": user.username,
        SESSION_ID_KEY: sid,
    }


__all__ = [
    "ACCOUNT_GUARD_LOCK_KEY",
    "ADVISORY_LOCK_SQL",
    "FakeRegistry",
    "browser_request",
    "cookie_for",
    "fake_user",
    "hash_session_id",
    "session_row",
    "sign_in",
    "sign_in_sync",
    "utcnow",
]
