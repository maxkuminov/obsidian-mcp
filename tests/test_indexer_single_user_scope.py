"""Mixed-mode regressions: ``user_id=None`` means NULL-owned rows only."""

from types import SimpleNamespace

import pytest
from sqlalchemy.sql.dml import Delete
from sqlalchemy.sql.elements import TextClause

from src.services import indexer


def _sql(stmt) -> str:
    return str(stmt.compile(compile_kwargs={"literal_binds": True}))


class _Result:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def fetchall(self):
        return self.rows

    def all(self):
        return self.rows


class _Session:
    def __init__(self, results=()):
        self.results = list(results)
        self.statements = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def execute(self, stmt, _params=None):
        self.statements.append(stmt)
        return self.results.pop(0) if self.results else _Result()

    async def commit(self):
        pass


@pytest.mark.asyncio
async def test_index_vault_reads_and_deletes_only_null_owned_rows(monkeypatch, tmp_path):
    # `extraction_version` joined the scan's select with #150.
    old = SimpleNamespace(
        file_path="gone.md",
        content_hash="hash",
        extraction_version=indexer.CURRENT_EXTRACTION_VERSION,
    )
    session = _Session([_Result([old])])
    monkeypatch.setattr(indexer, "async_session", lambda: session)
    monkeypatch.setattr(indexer, "_vault_root", lambda _uid: tmp_path)

    await indexer.index_vault(user_id=None)

    assert "notes_metadata.user_id IS NULL" in _sql(session.statements[0])
    deletion = next(s for s in session.statements if isinstance(s, Delete))
    assert "notes_metadata.user_id IS NULL" in _sql(deletion)


@pytest.mark.asyncio
async def test_link_index_vault_query_is_null_owned(monkeypatch, tmp_path):
    session = _Session()
    await indexer._update_links_for_changed(session, tmp_path, [], user_id=None)
    assert "notes_metadata.user_id IS NULL" in _sql(session.statements[0])


@pytest.mark.asyncio
async def test_link_reresolution_update_is_null_owned(tmp_path):
    note = tmp_path / "note.md"
    note.write_text("no links", encoding="utf-8")
    row = SimpleNamespace(file_path="note.md", id=7)
    session = _Session([_Result([row])])

    await indexer._update_links_for_changed(
        session, tmp_path, ["note.md"], user_id=None
    )

    updates = [
        s for s in session.statements
        if isinstance(s, TextClause) and "UPDATE note_links" in s.text
    ]
    assert updates
    assert "SELECT id FROM notes_metadata WHERE user_id IS NULL" in updates[0].text


@pytest.mark.asyncio
async def test_embed_vault_selection_is_null_owned(monkeypatch, tmp_path):
    session = _Session()
    monkeypatch.setattr(indexer, "async_session", lambda: session)
    monkeypatch.setattr(indexer, "_vault_root", lambda _uid: tmp_path)
    await indexer.embed_vault(user_id=None)
    selection = session.statements[0]
    assert isinstance(selection, TextClause)
    assert "nm.user_id IS NULL" in selection.text


@pytest.mark.asyncio
async def test_rebuild_tsvectors_selection_is_null_owned(monkeypatch, tmp_path):
    session = _Session()
    monkeypatch.setattr(indexer, "_vault_root", lambda _uid: tmp_path)
    assert await indexer.rebuild_tsvectors(session, user_id=None) == 0
    assert "notes_metadata.user_id IS NULL" in _sql(session.statements[0])
