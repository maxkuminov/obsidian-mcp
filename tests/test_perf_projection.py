"""Read paths select only what they render (#280, design D5–D7).

The six read statements — `semantic_search`, `keyword_search`, `list_notes`,
`get_recent`, `find_orphans` and `get_neighborhood`'s metadata hydration — each
project an explicit column list. None of them may carry
`notes_metadata.content_tsvector` (a 36 MB TOAST table the planner does not
cost), `note_embeddings.embedding` (a 1024-float vector per chunk) or
`notes_metadata.frontmatter` as an output column. `content_tsvector` is also
mapped deferred with raiseload, so an entity reader fails loudly instead of
shipping it or lazy-loading it under `AsyncSession`.

Fully offline: statements are captured from a recording session and inspected
as SQLAlchemy constructs. The behavioural identity against the pre-change
implementation is `tests/integration/test_perf_projection_identity_pg.py`.
"""

import datetime
import os
import tempfile
from types import SimpleNamespace

import pytest

os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
os.chdir(tempfile.gettempdir())

from sqlalchemy import Column, inspect  # noqa: E402
from sqlalchemy.dialects import postgresql  # noqa: E402
from sqlalchemy.exc import InvalidRequestError  # noqa: E402
from sqlalchemy.orm import Session, make_transient_to_detached  # noqa: E402
from sqlalchemy.sql import Select  # noqa: E402
from sqlalchemy.sql.elements import Label  # noqa: E402

import src.mcp_server.tools as tools  # noqa: E402
from src.models.db import NoteEmbedding, NoteMetadata  # noqa: E402
from src.services.embeddings import semantic_search  # noqa: E402
from src.services.search import full_text_search  # noqa: E402

FORBIDDEN = {
    ("notes_metadata", "content_tsvector"),
    ("notes_metadata", "frontmatter"),
    ("note_embeddings", "embedding"),
}


class _Result:
    def __init__(self, rows=()):
        self._rows = list(rows)

    def fetchall(self):
        return self._rows

    def all(self):
        return self._rows

    def scalars(self):
        return self

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class _RecordingSession:
    """Serves one batch per non-`SET LOCAL` statement and records each one."""

    def __init__(self, batches=()):
        self._batches = list(batches)
        self.statements: list = []

    async def execute(self, clause, *_a, **_k):
        if str(clause).lstrip().upper().startswith("SET LOCAL"):
            return _Result()
        self.statements.append(clause)
        return _Result(self._batches.pop(0) if self._batches else [])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return None


@pytest.fixture(autouse=True)
def _quiet_usage(monkeypatch):
    async def _noop(*_a, **_k):
        return None

    monkeypatch.setattr(tools, "_log_usage", _noop)


@pytest.fixture(autouse=True)
def _fake_embedding(monkeypatch):
    async def _fake(_text):
        return [1.0, 0.0, 0.0]

    monkeypatch.setattr("src.services.embeddings.get_embedding", _fake)


def _base_column(col):
    """The table column an output column *is*, or None for an expression.

    A `Label` over a bare column is still that column on the wire. A label over
    a function (`ts_rank_cd(content_tsvector, …)`, `embedding <=> :q`) is a
    scalar the server computes: the operand is read server-side and never
    shipped, which is exactly what D6 keeps.
    """
    while isinstance(col, Label):
        col = col.element
    if isinstance(col, Column):
        return col
    table = getattr(col, "table", None)
    if table is not None and getattr(col, "name", None):
        return col
    return None


def _output_columns(stmt: Select) -> list[tuple[str, str] | None]:
    out = []
    for c in stmt.selected_columns:
        base = _base_column(c)
        out.append(None if base is None else (base.table.name, base.name))
    return out


def _assert_projected(stmt: Select, expected_keys: list[str]) -> None:
    assert isinstance(stmt, Select)
    # No whole-entity select: every output is a column or a computed scalar.
    for desc in stmt.column_descriptions:
        assert desc["expr"] is not NoteMetadata, desc
        assert desc["expr"] is not NoteEmbedding, desc
    assert [c.key for c in stmt.selected_columns] == expected_keys
    for col in _output_columns(stmt):
        assert col not in FORBIDDEN, col
    # And the compiled SELECT list, as the database will see it.
    sql = str(stmt.compile(dialect=postgresql.dialect()))
    select_list = sql.split("\nFROM ", 1)[0]
    assert "notes_metadata.frontmatter" not in select_list
    assert "note_embeddings.embedding," not in select_list
    assert not select_list.rstrip().endswith("note_embeddings.embedding")


def _order_by_sql(stmt: Select) -> str:
    sql = str(stmt.compile(dialect=postgresql.dialect()))
    return sql.split("ORDER BY", 1)[1].split("\n LIMIT", 1)[0].split("LIMIT", 1)[0].strip()


# --------------------------------------------------------------------------- #
# The deferred mapping (D5)
# --------------------------------------------------------------------------- #
def test_content_tsvector_is_deferred_with_raiseload():
    prop = inspect(NoteMetadata).attrs.content_tsvector
    assert prop.deferred is True
    assert dict(prop.strategy_key).get("raiseload") is True


def test_frontmatter_stays_eager():
    """D5 assessed `frontmatter` and left it alone: deferring it would turn any
    future entity reader on a write path into a runtime error."""
    assert inspect(NoteMetadata).attrs.frontmatter.deferred is False


def test_touching_content_tsvector_on_a_loaded_entity_raises():
    note = NoteMetadata(id=1, file_path="a.md", title="a", content_hash="h")
    make_transient_to_detached(note)
    session = Session()  # unbound: any real load would fail differently
    session.add(note)
    assert note.file_path == "a.md"
    with pytest.raises(InvalidRequestError, match="raiseload"):
        _ = note.content_tsvector


def test_a_whole_entity_select_no_longer_carries_content_tsvector():
    from sqlalchemy import select

    sql = str(select(NoteMetadata).compile(dialect=postgresql.dialect()))
    assert "content_tsvector" not in sql
    assert "frontmatter" in sql  # still eager


# --------------------------------------------------------------------------- #
# The six statements (D6)
# --------------------------------------------------------------------------- #
def _semantic_row(note_id, path, distance, chunk_index=0):
    return SimpleNamespace(
        note_id=note_id, chunk_index=chunk_index, chunk_text=f"chunk {note_id}",
        file_path=path, title=path.removesuffix(".md"), tags=[],
        content_hash="h", embedded_content_hash="h", chunks_truncated=False,
        distance=distance,
    )


@pytest.mark.asyncio
async def test_semantic_search_projects_the_find_related_shape():
    session = _RecordingSession([[_semantic_row(1, "a.md", 0.1)]])
    await semantic_search(session, "q", limit=5)
    (stmt,) = session.statements
    _assert_projected(stmt, [
        "note_id", "chunk_index", "chunk_text", "file_path", "title", "tags",
        "content_hash", "embedded_content_hash", "chunks_truncated", "distance",
    ])


@pytest.mark.asyncio
async def test_semantic_search_similarity_is_one_minus_distance():
    rows = [_semantic_row(1, "a.md", 0.125), _semantic_row(2, "b.md", 0.5)]
    session = _RecordingSession([rows])
    results = await semantic_search(session, "q", limit=5)
    assert [r["similarity"] for r in results] == [0.875, 0.5]


@pytest.mark.asyncio
async def test_semantic_search_breaks_exact_distance_ties_deterministically():
    """(distance, file_path, chunk_index): equidistant notes sort by path, and
    the representative chunk among equidistant chunks is the lowest index."""
    rows = [
        _semantic_row(2, "b.md", 0.2, chunk_index=3),
        _semantic_row(2, "b.md", 0.2, chunk_index=1),
        _semantic_row(1, "a.md", 0.2, chunk_index=0),
    ]
    session = _RecordingSession([rows])
    results = await semantic_search(session, "q", limit=5)
    assert [(r["path"], r["chunk_index"]) for r in results] == [("a.md", 0), ("b.md", 1)]


@pytest.mark.asyncio
async def test_keyword_search_projects_four_columns():
    row = SimpleNamespace(file_path="a.md", title="a", tags=["t"], rank=0.5)
    session = _RecordingSession([[row]])
    results = await full_text_search(session, "needle")
    (stmt,) = session.statements
    _assert_projected(stmt, ["file_path", "title", "tags", "rank"])
    assert results == [{"path": "a.md", "title": "a", "tags": ["t"], "rank": 0.5}]
    assert _order_by_sql(stmt) == "rank DESC, notes_metadata.file_path ASC"


_T = datetime.datetime(2026, 9, 1, 12, 30, tzinfo=datetime.timezone.utc)


@pytest.mark.asyncio
async def test_list_notes_projects_and_renders_unchanged(monkeypatch):
    rows = [
        SimpleNamespace(file_path="a.md", file_size=1234, modified_at=_T),
        SimpleNamespace(file_path="b.md", file_size=None, modified_at=None),
    ]
    session = _RecordingSession([rows])
    monkeypatch.setattr(tools, "async_session", lambda: session)
    out = await tools.list_notes_impl(folder="", limit=10)
    (stmt,) = session.statements
    _assert_projected(stmt, ["file_path", "file_size", "modified_at"])
    assert _order_by_sql(stmt) == (
        "notes_metadata.modified_at DESC, notes_metadata.file_path ASC"
    )
    assert out == (
        "Found 2 notes in '/':\n\n"
        "- `a.md` (1,234B, modified 2026-09-01)\n"
        "- `b.md` (0B, modified unknown)"
    )


@pytest.mark.asyncio
async def test_get_recent_projects_and_renders_unchanged(monkeypatch):
    rows = [
        SimpleNamespace(file_path="a.md", title="A", tags=["x", "y"], modified_at=_T),
        SimpleNamespace(file_path="b.md", title="B", tags=[], modified_at=None),
    ]
    session = _RecordingSession([rows])
    monkeypatch.setattr(tools, "async_session", lambda: session)
    out = await tools.get_recent_impl(limit=10)
    (stmt,) = session.statements
    _assert_projected(stmt, ["file_path", "title", "tags", "modified_at"])
    assert _order_by_sql(stmt) == (
        "notes_metadata.modified_at DESC, notes_metadata.file_path ASC"
    )
    assert out == (
        "Last 2 modified notes:\n\n"
        "- `a.md` — A [x, y] (modified 2026-09-01 12:30)\n"
        "- `b.md` — B (modified unknown)"
    )


@pytest.mark.asyncio
async def test_find_orphans_projects_keeps_nulls_last_and_renders_unchanged(monkeypatch):
    rows = [
        SimpleNamespace(file_path="a.md", title="A", tags=["x"], modified_at=_T),
        SimpleNamespace(file_path="z.md", title="Z", tags=[], modified_at=None),
    ]
    session = _RecordingSession([rows])
    monkeypatch.setattr(tools, "async_session", lambda: session)
    out = await tools.find_orphans_impl(limit=10)
    (stmt,) = session.statements
    _assert_projected(stmt, ["file_path", "title", "tags", "modified_at"])
    assert _order_by_sql(stmt) == (
        "notes_metadata.modified_at DESC NULLS LAST, notes_metadata.file_path ASC"
    )
    assert out == (
        "Found 2 orphan notes:\n\n"
        "- `a.md` — A [x] (modified 2026-09-01)\n"
        "- `z.md` — Z (modified unknown)"
    )


@pytest.mark.asyncio
async def test_get_neighborhood_hydration_projects_and_renders_unchanged(monkeypatch):
    source = SimpleNamespace(id=1, file_path="src.md", title="Src", tags=[])
    edges = [(1, 2), (3, 1)]
    meta = [
        SimpleNamespace(id=2, file_path="b.md", title="B", tags=["t"]),
        SimpleNamespace(id=3, file_path="a.md", title="A", tags=[]),
    ]
    session = _RecordingSession([[source], edges, meta])
    monkeypatch.setattr(tools, "async_session", lambda: session)
    out = await tools.get_neighborhood_impl("src.md", depth=1, limit=10)
    meta_stmt = session.statements[2]
    _assert_projected(meta_stmt, ["id", "file_path", "title", "tags"])
    assert out == (
        "Neighborhood of `src.md` (depth ≤ 1, 2 notes):\n\n"
        "- d=1 **A** (`a.md`) via `src.md`\n"
        "- d=1 **B** (`b.md`) [t] via `src.md`"
    )
