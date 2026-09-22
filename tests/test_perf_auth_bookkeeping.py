"""Authentication bookkeeping on the request path (performance-2026-09, #279).
Hermetic.

What is pinned here, by counting the statements the real `APIKeyMiddleware`
issues against a recording session:

* An API-key request whose `last_used_at` is fresh issues exactly **one**
  statement: the credential, joined to its user's `is_active`/`vault_path`
  (D3). One whose stamp is stale (or NULL) issues that SELECT, then
  `SET LOCAL synchronous_commit = off`, then a *conditional* UPDATE, then
  commits (D1/D2). OAuth issues one statement.
* The folded read still revokes on the next request (#66), for each of:
  deactivating the user, clearing `vault_path`, revoking the key, deleting the
  `users` row, and revoking the OAuth token — with the same reason codes and
  bodies as before.
* `synchronous_commit` appears in exactly three places in `src/`: the
  `last_used_at` write in the middleware, `_insert_usage`, and `quotas.admit`.
  Every other write — OAuth code exchange, refresh rotation, revocation,
  transfer tokens, users, the indexer — is synchronous by construction. The
  runtime spy over the real OAuth and transfer handlers is in
  `tests/integration/test_perf_async_commit_pg.py`.

The real-database facts (the setting does not leak through the pool, the
quota boundary is exact under async commit) are proved there too.
"""
import ast
import asyncio
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
os.chdir(tempfile.gettempdir())

import pytest  # noqa: E402

import src.mcp_server.auth as mcp_auth  # noqa: E402
import src.services.vault as vault  # noqa: E402
from src.auth.session import current_vault_root  # noqa: E402
from src.models.db import APIKey, OAuthToken  # noqa: E402
from src.services import security_events  # noqa: E402

SRC = Path(__file__).resolve().parent.parent / "src"
UID = 5150
ROOT = "/vaults/alice"


# ── plumbing ────────────────────────────────────────────────────────────────


class _Result:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _RecordingSession:
    """The one `async_session()` the middleware opens, recording every
    statement and commit in order."""

    def __init__(self, *, key_row=None, oauth_row=None):
        self.key_row = key_row
        self.oauth_row = oauth_row
        self.log: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        self.log.append("EXIT")
        return False

    async def commit(self):
        self.log.append("COMMIT")

    async def execute(self, stmt, *_a, **_kw):
        sql = str(stmt)
        self.log.append(sql)
        if sql.startswith("SET LOCAL") or sql.startswith("UPDATE"):
            return _Result(None)
        if "FROM api_keys" in sql:
            return _Result(self.key_row)
        if "FROM oauth_tokens" in sql:
            return _Result(self.oauth_row)
        raise AssertionError(f"unexpected statement: {sql}")

    def statements(self):
        return [s for s in self.log if s not in ("COMMIT", "EXIT")]


def _key(last_used_at=None, user_id=UID, **kw):
    return APIKey(
        id=kw.pop("id", 3), name="probe", key_prefix="omcp_abcdef",
        key_hash="h", permission="read", is_active=True, user_id=user_id,
        expires_at=kw.pop("expires_at", None), last_used_at=last_used_at,
    )


def _token(user_id=UID):
    return OAuthToken(
        id=91, token_hash="t", token_type="access", revoked=False,
        client_id="c1", grant_id="g1", user_id=user_id, scope="read",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )


@pytest.fixture
def cache():
    saved = dict(vault._user_vault_cache)
    vault._user_vault_cache.clear()
    try:
        yield vault._user_vault_cache
    finally:
        vault._user_vault_cache.clear()
        vault._user_vault_cache.update(saved)


@pytest.fixture
def reasons(monkeypatch):
    seen = []
    real = security_events.emit

    def spy(event, *a, **kw):
        if event == "auth_failure":
            seen.append(kw.get("reason"))
        return real(event, *a, **kw)

    monkeypatch.setattr(security_events, "emit", spy)
    return seen


_n = iter(range(1, 100_000))


def drive(session, bearer="omcp_probe", multi_user=True):
    """Run the real middleware once; return `(status, body, bound root)`."""
    sent, captured = [], {}

    async def downstream(scope, receive, send):
        captured["root"] = current_vault_root.get()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive():  # pragma: no cover - never awaited
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    async def run():
        mp = pytest.MonkeyPatch()
        try:
            mp.setattr(mcp_auth, "async_session", lambda: session)
            mp.setattr(mcp_auth.settings, "multi_user_mode", multi_user, raising=False)
            n = next(_n)
            await mcp_auth.APIKeyMiddleware(downstream)(
                {
                    "type": "http", "method": "POST", "path": "/mcp/",
                    "headers": [(b"authorization", f"Bearer {bearer}".encode())],
                    "client": (f"203.0.{n // 250}.{n % 250 + 1}", 4242),
                },
                receive,
                send,
            )
        finally:
            mp.undo()

    asyncio.run(run())
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], body, captured.get("root")


# ── statement counts ────────────────────────────────────────────────────────


def test_a_fresh_stamp_issues_exactly_one_statement(cache):
    recent = datetime.now(timezone.utc) - timedelta(seconds=10)
    session = _RecordingSession(key_row=(_key(recent), True, ROOT))

    status, _, root = drive(session)

    assert status == 200
    stmts = session.statements()
    assert len(stmts) == 1, stmts
    assert "FROM api_keys LEFT OUTER JOIN users" in stmts[0]
    assert "users.is_active" in stmts[0] and "users.vault_path" in stmts[0]
    assert "COMMIT" not in session.log
    # The (read-only) transaction ended before the response.
    assert session.log[-1] == "EXIT"
    assert root == (UID, Path(ROOT))


@pytest.mark.parametrize(
    "last_used",
    [None, datetime.now(timezone.utc) - timedelta(minutes=2)],
    ids=["never-used", "stale"],
)
def test_a_due_stamp_is_select_set_local_update_commit(cache, last_used):
    session = _RecordingSession(key_row=(_key(last_used), True, ROOT))

    assert drive(session)[0] == 200

    log = [s for s in session.log if s != "EXIT"]
    assert len(log) == 4, log
    select_, set_local, update_, commit = log
    assert select_.startswith("SELECT") and "FROM api_keys" in select_
    assert set_local == "SET LOCAL synchronous_commit = off"
    assert update_.startswith("UPDATE api_keys")
    # Conditional: a concurrent request that already advanced it is a no-op.
    assert "api_keys.last_used_at IS NULL OR api_keys.last_used_at <" in update_
    assert commit == "COMMIT"


def test_the_resolution_is_sixty_seconds():
    assert mcp_auth.LAST_USED_AT_RESOLUTION_SECONDS == 60


def test_oauth_issues_exactly_one_statement(cache):
    session = _RecordingSession(
        oauth_row=(_token(), None, "Client", True, ROOT)
    )

    status, _, root = drive(session, bearer="oauth-access")

    assert status == 200
    stmts = session.statements()
    assert len(stmts) == 1, stmts
    assert "FROM oauth_tokens LEFT OUTER JOIN oauth_clients" in stmts[0]
    assert "LEFT OUTER JOIN users" in stmts[0]
    assert "COMMIT" not in session.log
    assert root == (UID, Path(ROOT))


def test_refusals_issue_no_write_and_no_set_local(cache):
    recent = None  # would be due, but the refusal comes first
    for row in [
        None,                                   # unknown / revoked key
        (_key(recent), False, ROOT),            # inactive user
        (_key(recent), None, None),             # users row gone
        (_key(recent, expires_at=datetime.now(timezone.utc) - timedelta(1)), True, ROOT),
    ]:
        session = _RecordingSession(key_row=row)
        assert drive(session)[0] == 401
        assert len(session.statements()) == 1, session.statements()


# ── the folded read still revokes on the next request (#66) ────────────────


def _two_requests(first, second, bearer="omcp_probe"):
    ok = drive(first, bearer=bearer)
    assert ok[0] == 200
    return drive(second, bearer=bearer)


def test_deactivating_the_user_refuses_the_next_request(cache, reasons):
    status, body, _ = _two_requests(
        _RecordingSession(key_row=(_key(), True, ROOT)),
        _RecordingSession(key_row=(_key(), False, ROOT)),
    )
    assert (status, body) == (401, b'{"error":"Invalid or revoked key"}')
    assert reasons[-1] == "inactive_user"
    assert UID not in cache


def test_clearing_vault_path_binds_none_and_evicts(cache):
    drive(_RecordingSession(key_row=(_key(), True, ROOT)))
    assert cache[UID] == Path(ROOT)

    status, _, root = drive(_RecordingSession(key_row=(_key(), True, None)))

    assert status == 200
    assert root == (UID, None)  # every tool call in the request refuses
    assert UID not in cache


def test_revoking_the_key_refuses_the_next_request(cache, reasons):
    status, body, _ = _two_requests(
        _RecordingSession(key_row=(_key(), True, ROOT)),
        _RecordingSession(key_row=None),  # `is_active = false` matches no row
    )
    assert (status, body) == (401, b'{"error":"Invalid or revoked key"}')
    assert reasons[-1] == "invalid_key"


def test_a_deleted_users_row_is_refused_as_inactive(cache, reasons):
    """The outer join yields NULLs; `None is not True` refuses, exactly as the
    former `scalar_one_or_none() is not True` did."""
    status, body, _ = _two_requests(
        _RecordingSession(key_row=(_key(), True, ROOT)),
        _RecordingSession(key_row=(_key(), None, None)),
    )
    assert (status, body) == (401, b'{"error":"Invalid or revoked key"}')
    assert reasons[-1] == "inactive_user"
    assert UID not in cache


def test_revoking_the_oauth_token_refuses_the_next_request(cache, reasons):
    status, body, _ = _two_requests(
        _RecordingSession(oauth_row=(_token(), None, "Client", True, ROOT)),
        _RecordingSession(oauth_row=None),  # `revoked = false` matches no row
        bearer="oauth-access",
    )
    assert (status, body) == (401, b'{"error":"Invalid or revoked token"}')
    assert reasons[-1] == "invalid_key"


@pytest.mark.parametrize("is_active", [False, None], ids=["inactive", "deleted"])
def test_an_oauth_tokens_inactive_or_deleted_user_is_refused(cache, reasons, is_active):
    status, body, _ = _two_requests(
        _RecordingSession(oauth_row=(_token(), None, "Client", True, ROOT)),
        _RecordingSession(oauth_row=(_token(), None, "Client", is_active, None)),
        bearer="oauth-access",
    )
    assert (status, body) == (401, b'{"error":"Invalid or revoked token"}')
    assert reasons[-1] == "inactive_user"
    assert UID not in cache


def test_single_user_keys_bind_nothing(cache):
    session = _RecordingSession(key_row=(_key(user_id=None), None, None))
    status, _, root = drive(session, multi_user=False)
    assert status == 200
    assert root is mcp_auth.UNSET_VAULT_ROOT
    assert cache == {}


# ── apply_user_vault_row == the warm's single-user form ─────────────────────


@pytest.mark.parametrize(
    "is_active,vault_path,expected",
    [
        (True, ROOT, Path(ROOT)),
        (True, None, None),
        (False, ROOT, None),
        (None, None, None),
        (None, ROOT, None),
    ],
)
def test_apply_user_vault_row_is_write_or_evict(cache, is_active, vault_path, expected):
    cache[UID] = Path("/stale")
    assert vault.apply_user_vault_row(UID, is_active, vault_path) == expected
    if expected is None:
        assert UID not in cache
    else:
        assert cache[UID] == expected


# ── synchronous_commit appears only where D2 allows it ─────────────────────


_ALLOWED_FUNCTIONS = {
    ("mcp_server/auth.py", "_authenticate"),
    ("mcp_server/tools.py", "_insert_usage"),
    ("services/quotas.py", "admit"),
}
_ALLOWED_CONSTANTS = {"mcp_server/auth.py", "services/quotas.py"}


def _sweep():
    """Every use of `synchronous_commit` in `src/`, by enclosing function.

    Returns `(literal_sites, constant_uses)`: string constants mentioning the
    setting, and loads of the `ASYNC_COMMIT_SQL` name, each as
    `(relative path, enclosing function or None, text)`.
    """
    literals, uses = [], []
    for path in SRC.rglob("*.py"):
        rel = path.relative_to(SRC).as_posix()
        tree = ast.parse(path.read_text(), filename=str(path))

        def visit(node, func):
            for child in ast.iter_child_nodes(node):
                inner = func
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    inner = child.name
                if (
                    isinstance(child, ast.Constant)
                    and isinstance(child.value, str)
                    and "synchronous_commit" in child.value
                ):
                    literals.append((rel, func, child.value))
                if (
                    isinstance(child, (ast.Name, ast.Attribute))
                    and (getattr(child, "id", None) or getattr(child, "attr", None))
                    == "ASYNC_COMMIT_SQL"
                    and isinstance(child.ctx, ast.Load)
                ):
                    uses.append((rel, func, "ASYNC_COMMIT_SQL"))
                visit(child, inner)

        visit(tree, None)
    return literals, uses


def test_synchronous_commit_is_only_turned_off_where_d2_allows():
    literals, uses = _sweep()
    assert literals, "the sweep found nothing — it is not looking in src/"

    for rel, func, value in literals:
        if func is None:
            # A module-level constant (or docstring) in one of the two
            # modules that define ASYNC_COMMIT_SQL.
            assert rel in _ALLOWED_CONSTANTS, (rel, value)
        else:
            assert (rel, func) in _ALLOWED_FUNCTIONS, (rel, func, value)
        # Never the session-wide form: it would ride the pooled connection
        # into the next checkout, including a revocation's commit.
        for line in value.splitlines():
            if "SET" in line and "synchronous_commit" in line:
                assert "SET LOCAL synchronous_commit" in line, (rel, line)

    for rel, func, _ in uses:
        assert (rel, func) in _ALLOWED_FUNCTIONS, (rel, func)

    # Each allowed site actually issues it.
    issued = {(r, f) for r, f, _ in uses} | {
        (r, f) for r, f, v in literals if f is not None and v.startswith("SET LOCAL")
    }
    assert issued == _ALLOWED_FUNCTIONS, issued
