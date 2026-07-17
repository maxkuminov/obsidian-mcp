"""Regression coverage for per-user link-backfill query scoping."""

from __future__ import annotations

import pytest

from src.services import indexer


class _Result:
    def __init__(self, *, count: int | None = None):
        self.count = count

    def scalar(self):
        return self.count

    def all(self):
        return []


class _Session:
    def __init__(self):
        self.statements = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def execute(self, stmt, _params=None):
        self.statements.append(stmt)
        return _Result(count=0 if len(self.statements) == 1 else None)


@pytest.mark.asyncio
async def test_single_user_backfill_scan_excludes_multi_user_rows(monkeypatch, tmp_path):
    """Both completion detection and note scanning use the NULL-user scope.

    This prevents a legacy/single-user pass from reading mixed-scope metadata
    rows and resolving them against the wrong vault.
    """
    session = _Session()
    monkeypatch.setattr(indexer, "async_session", lambda: session)
    monkeypatch.setattr(indexer, "_vault_root", lambda _uid: tmp_path)

    await indexer.link_backfill_pass(user_id=None)

    assert len(session.statements) == 2
    completion_sql = str(session.statements[0].compile(compile_kwargs={"literal_binds": True}))
    scan_sql = str(session.statements[1].compile(compile_kwargs={"literal_binds": True}))
    assert "notes_metadata.user_id IS NULL" in completion_sql
    assert "notes_metadata.user_id IS NULL" in scan_sql
