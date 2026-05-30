"""Regression test for GitHub issue #15.

The control-panel `GET /api/usage` endpoint (`get_usage` in `src/api/routes.py`)
took an unbounded `limit` query param and passed it straight into
`select(UsageLog).limit(limit)`. Any authenticated panel user could request,
e.g., `?limit=10000000`, forcing the DB to materialize an arbitrarily large
result set — a trivial resource-exhaustion vector. Sibling MCP tools in
`src/mcp_server/tools.py` already clamp with `limit = max(1, min(limit, 500))`.

The fix adds the same clamp as the first line of `get_usage`. These tests call
`get_usage` directly with a fake AsyncSession that captures the executed
SQLAlchemy statement, then assert the LIMIT value reaching the query is clamped
to [1, 500]. No DB, no network, no embeddings — fully offline.
"""

import asyncio
import os
import sys
import tempfile

# `src.api.routes` -> `src.config`, whose module-level `Settings()` reads
# `./.env`. The real `.env` carries forbidden host-only keys, so provide minimal
# defaults and chdir to a dir without a `.env` (env_file resolves relative to
# CWD) BEFORE importing. Keeps the import fully offline.
os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(tempfile.gettempdir())

from src.api.routes import get_usage  # noqa: E402


class _FakeResult:
    def scalars(self):
        return self

    def all(self):
        return []


class _CapturingSession:
    """Minimal stand-in for AsyncSession that records the executed statement."""

    def __init__(self):
        self.executed = None

    async def execute(self, query):
        self.executed = query
        return _FakeResult()


class _AdminUser:
    is_admin = True
    id = 1


def _applied_limit(requested: int) -> int:
    """Run get_usage with a capturing session and return the LIMIT value that
    actually reached the SQLAlchemy statement."""
    session = _CapturingSession()
    asyncio.run(
        get_usage(limit=requested, key_id=None, session=session, user=_AdminUser())
    )
    return session.executed._limit_clause.value


def test_huge_limit_is_clamped_to_500():
    assert _applied_limit(10_000_000) == 500


def test_zero_limit_is_clamped_to_1():
    assert _applied_limit(0) == 1


def test_negative_limit_is_clamped_to_1():
    assert _applied_limit(-5) == 1


def test_in_range_limit_is_preserved():
    assert _applied_limit(100) == 100


def test_boundary_500_is_preserved():
    assert _applied_limit(500) == 500
