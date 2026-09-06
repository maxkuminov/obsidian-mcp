"""Mixed-mode regressions: ``user_id=None`` means NULL-owned rows only."""

from types import SimpleNamespace

import pytest
from sqlalchemy.sql.dml import Delete
from sqlalchemy.sql.elements import TextClause
from sqlalchemy.sql.selectable import Select

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

    def scalar(self):
        # The index pass's `to_regclass('indexer_state')` probe: `None` means
        # migration 023 has not run, so the fingerprint re-validation defers to
        # alembic and this offline fixture stays about owner scoping.
        return None


class _Session:
    def __init__(self, results=()):
        self.results = list(results)
        self.statements = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    #: Statements the index pass now issues at the head of its transaction —
    #: the generation lock and the `indexer_state` existence probe (D7c3).
    #: They are recorded but must not consume the queued results, which are
    #: positional and belong to the owner-scoped queries this module is about.
    _PREAMBLE = ("pg_advisory_xact_lock", "to_regclass")

    async def execute(self, stmt, _params=None):
        self.statements.append(stmt)
        text = getattr(stmt, "text", "")
        if any(marker in text for marker in self._PREAMBLE):
            return _Result()
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

    # Searched rather than indexed: the pass now opens its transaction with
    # the generation lock and the `indexer_state` probe (D7c3), so the scoped
    # scan query is no longer the first statement — and this module is about
    # owner scoping, not statement order.
    scan = next(s for s in session.statements if isinstance(s, Select))
    assert "notes_metadata.user_id IS NULL" in _sql(scan)
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
    # The stage-head fingerprint probe runs before the backlog query, so the
    # selection is found by what it says rather than by where it sits.
    selection = next(
        s for s in session.statements
        if isinstance(s, TextClause) and "notes_metadata nm" in s.text
    )
    assert "nm.user_id IS NULL" in selection.text


@pytest.mark.asyncio
async def test_rebuild_tsvectors_selection_is_null_owned(monkeypatch, tmp_path):
    session = _Session()
    monkeypatch.setattr(indexer, "_vault_root", lambda _uid: tmp_path)
    assert await indexer.rebuild_tsvectors(session, user_id=None) == 0
    assert "notes_metadata.user_id IS NULL" in _sql(session.statements[0])
