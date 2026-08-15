"""Regression test for GitHub issue #6.

pgvector's default `vector` type allows an HNSW index only up to 2000
dimensions; above that, `CREATE INDEX ... USING hnsw (embedding
vector_cosine_ops)` hard-errors with "column cannot have more than 2000
dimensions for hnsw index". Two runtime sites issued that DDL with no
dimension guard:

  * alembic migration 008's ``upgrade()`` — aborts the deploy half-migrated.
  * the control-panel ``reset_embeddings`` route — same failure in-app.

The fix skips index creation (with a warning) when
``settings.embedding_dimensions > 2000`` so the schema operation completes and
the app starts; semantic search degrades to a sequential scan.

These tests exercise both sites with fakes — no DB, no network, no embedding
provider — so the whole module runs fully offline.
"""

import asyncio
import importlib.util
import os
import tempfile
from pathlib import Path

# Importing the production modules pulls in `src.config`, whose module-level
# `Settings()` singleton reads `./.env`. On this host the real `.env` carries
# host-only keys the model forbids, so we must NOT let that file load. Provide
# the same minimal defaults conftest uses and chdir to a dir without a `.env`
# (env_file is resolved relative to CWD) BEFORE importing anything.
os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
os.chdir(tempfile.gettempdir())

from src.config import settings  # noqa: E402


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class _FakeScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


class _FakeConn:
    """Stands in for op.get_bind(); reports a recent pgvector version."""

    def execute(self, *_a, **_k):
        # The migration only uses this to read pg_extension.extversion.
        return _FakeScalarResult("0.7.0")


class _RecordingOp:
    """Records every SQL string passed to op.execute()."""

    def __init__(self, conn):
        self._conn = conn
        self.executed: list[str] = []

    def get_bind(self):
        return self._conn

    def execute(self, sql):
        self.executed.append(str(sql))


def _load_migration_008():
    """Import migration 008 by path (the alembic/versions dir is not a package)."""
    path = (
        Path(__file__).resolve().parent.parent
        / "alembic"
        / "versions"
        / "008_hnsw_embedding_index.py"
    )
    spec = importlib.util.spec_from_file_location("_mig008", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_migration_upgrade(monkeypatch, dim):
    mig = _load_migration_008()
    rec = _RecordingOp(_FakeConn())
    # The migration calls `op.get_bind()`, `op.execute(...)` and `sa.text(...)`.
    monkeypatch.setattr(mig, "op", rec)
    monkeypatch.setattr(settings, "embedding_dimensions", dim)
    mig.upgrade()
    return rec.executed


def _has_hnsw_create(statements):
    return any(
        "CREATE INDEX" in s and "hnsw" in s for s in statements
    )


# --------------------------------------------------------------------------- #
# Migration 008
# --------------------------------------------------------------------------- #
def test_migration_skips_hnsw_index_when_dim_over_2000(monkeypatch):
    """dim=3072 (text-embedding-3-large) must NOT emit a CREATE INDEX hnsw."""
    statements = _run_migration_upgrade(monkeypatch, 3072)
    assert not _has_hnsw_create(statements), (
        "migration must skip HNSW index creation above 2000 dims"
    )


def test_migration_creates_hnsw_index_at_2000(monkeypatch):
    """dim=2000 is the documented hard limit and must still build the index."""
    statements = _run_migration_upgrade(monkeypatch, 2000)
    assert _has_hnsw_create(statements)


def test_migration_creates_hnsw_index_at_default_dim(monkeypatch):
    """The common 1024-dim case is unaffected by the fix."""
    statements = _run_migration_upgrade(monkeypatch, 1024)
    assert _has_hnsw_create(statements)


# --------------------------------------------------------------------------- #
# reset_embeddings control-panel route
# --------------------------------------------------------------------------- #
class _FakeSession:
    def __init__(self):
        self.executed: list[str] = []
        self.closed = False

    async def execute(self, clause, *_a, **_k):
        self.executed.append(str(clause))

    async def commit(self):
        pass

    async def close(self):
        self.closed = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return None


class _FakeRequest:
    headers = {"accept": "application/json"}


async def _call_reset(monkeypatch, dim):
    from src.control_panel import routes

    monkeypatch.setattr(settings, "embedding_dimensions", dim)
    # Don't spawn the background reindex (it would touch DB/embeddings).
    monkeypatch.setattr(routes, "_spawn", lambda coro: coro.close())

    # The route now runs its destructive statements on a *fresh* session taken
    # only after it holds the indexer pass lock — see the pre-warm change.
    # The request's own session is closed before the wait, so the statements
    # to assert on land on this one.
    destructive = _FakeSession()
    monkeypatch.setattr(routes, "async_session", lambda: destructive)

    request_session = _FakeSession()
    resp = await routes.reset_embeddings(
        request=_FakeRequest(), session=request_session, user=object()
    )
    assert request_session.closed, (
        "the request session must be released before waiting on the pass lock"
    )
    return destructive.executed, resp


def test_reset_skips_hnsw_index_when_dim_over_2000(monkeypatch):
    executed, resp = asyncio.run(_call_reset(monkeypatch, 3072))
    assert not _has_hnsw_create(executed), (
        "reset route must skip HNSW index creation above 2000 dims"
    )
    # The ALTER COLUMN to the wide vector must still have happened.
    assert any("ALTER COLUMN embedding TYPE vector(3072)" in s for s in executed)
    # JSON response surfaces the degraded state.
    assert resp.body and b'"hnsw":false' in resp.body.replace(b" ", b"")


def test_reset_creates_hnsw_index_at_default_dim(monkeypatch):
    executed, resp = asyncio.run(_call_reset(monkeypatch, 1024))
    assert _has_hnsw_create(executed)
    assert resp.body and b'"hnsw":true' in resp.body.replace(b" ", b"")
