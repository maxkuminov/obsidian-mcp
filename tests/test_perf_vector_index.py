"""The half-precision vector index has one definition (#283, design D19).

Offline. What a fake session and the compiler can pin:

  * `alembic/env.py`'s `include_object` excludes exactly one object — the
    index `vector_index.INDEX_NAME` — and both `context.configure` calls pass
    it, so `alembic check` ignores that index and compares everything else;
  * `index_enabled` is false above 2000 dimensions, and then both vector
    queries use the plain full-precision expression, with no `halfvec` cast;
  * the ORDER BY the queries issue compiles to exactly the index expression
    the DDL builds, so the planner can match them;
  * both tools re-sort the fetched rows by the full-precision distance
    *before* the per-note dedupe, and report `similarity = 1 - distance`.

Whether the planner really uses the index, and whether recall holds, is the
integration suite's (`tests/integration/test_search_recall.py`,
`test_prewarm_probe.py`); the catalogue shape is the schema gate's.
"""

import ast
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")

ROOT = Path(__file__).resolve().parent.parent
os.chdir(tempfile.gettempdir())

from sqlalchemy.dialects import postgresql  # noqa: E402

import src.mcp_server.tools as tools  # noqa: E402
from src.models.db import Base  # noqa: E402
from src.services import vector_index  # noqa: E402
from src.services.embeddings import semantic_search  # noqa: E402
from src.services.indexer import _probe_vector, probe_statement  # noqa: E402

DIALECT = postgresql.asyncpg.dialect()


def _sql(clause) -> str:
    return str(clause.compile(dialect=DIALECT))


# --------------------------------------------------------------------------- #
# 1. alembic/env.py: include_object
# --------------------------------------------------------------------------- #
def _env_tree() -> ast.Module:
    return ast.parse((ROOT / "alembic" / "env.py").read_text())


def _include_object():
    """The hook itself, extracted from env.py — importing env.py would run the
    migrations it configures."""
    tree = _env_tree()
    fn = next(
        n for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "include_object"
    )
    namespace: dict = {}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "env.py", "exec"), namespace)
    return namespace["include_object"]


def _candidate_objects():
    """Every name the comparison can see, under every type it reports."""
    names = {vector_index.INDEX_NAME, vector_index.LEGACY_INDEX_NAME}
    for table in Base.metadata.tables.values():
        names.add(table.name)
        names.update(c.name for c in table.columns)
        names.update(i.name for i in table.indexes if i.name)
        names.update(c.name for c in table.constraints if c.name)
    types = (
        "table", "column", "index", "unique_constraint",
        "foreign_key_constraint", "check_constraint",
    )
    return [(name, type_) for name in sorted(names) for type_ in types]


def test_include_object_excludes_exactly_one_object():
    hook = _include_object()
    excluded = [
        (name, type_)
        for name, type_ in _candidate_objects()
        for reflected in (True, False)
        if not hook(None, name, type_, reflected, None)
    ]
    # Once reflected, once from the model side: the same single object.
    assert set(excluded) == {(vector_index.INDEX_NAME, "index")}


def test_include_object_keeps_the_legacy_index_and_every_model_index():
    hook = _include_object()
    assert hook(None, vector_index.LEGACY_INDEX_NAME, "index", True, None)
    for table in Base.metadata.tables.values():
        for index in table.indexes:
            assert hook(None, index.name, "index", False, None), index.name
    # Same name, another type: not excluded.
    assert hook(None, vector_index.INDEX_NAME, "table", True, None)


def test_both_configure_calls_pass_the_hook():
    calls = [
        n for n in ast.walk(_env_tree())
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "configure"
    ]
    assert len(calls) == 2
    for call in calls:
        kw = {k.arg: k.value for k in call.keywords}
        assert isinstance(kw.get("include_object"), ast.Name)
        assert kw["include_object"].id == "include_object"


def test_the_model_no_longer_declares_a_vector_index():
    names = {i.name for i in Base.metadata.tables["note_embeddings"].indexes}
    assert vector_index.LEGACY_INDEX_NAME not in names
    assert vector_index.INDEX_NAME not in names


# --------------------------------------------------------------------------- #
# 2. The definition
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("dim,enabled", [(8, True), (1024, True), (2000, True),
                                         (2001, False), (3072, False)])
def test_index_enabled_boundary(dim, enabled):
    assert vector_index.index_enabled(dim) is enabled


def test_create_index_sql_refuses_above_the_limit():
    with pytest.raises(ValueError):
        vector_index.create_index_sql(2001)


def test_create_index_sql_is_the_halfvec_expression_at_the_dimension():
    ddl = vector_index.create_index_sql(1536)
    assert ddl == (
        f"CREATE INDEX {vector_index.INDEX_NAME} ON note_embeddings "
        "USING hnsw ((CAST(embedding AS HALFVEC(1536))) halfvec_cosine_ops) "
        "WITH (m = 16, ef_construction = 64)"
    )


def test_drop_index_sql_drops_both_names_if_they_exist():
    assert vector_index.drop_index_sql() == (
        f"DROP INDEX IF EXISTS {vector_index.INDEX_NAME}, "
        f"{vector_index.LEGACY_INDEX_NAME}"
    )


@pytest.mark.parametrize("dim", [8, 1024, 2000])
def test_order_expression_is_the_index_expression_verbatim(dim):
    """The planner matches an expression index only on the identical
    expression. The ORDER BY's left operand, unqualified, must be the text the
    DDL indexes; the right operand casts the query the same way."""
    order = vector_index.order_expr([0.5] * dim)
    rendered = str(order.compile(dialect=postgresql.dialect()))
    left, right = rendered.split(" <=> ")
    assert left.replace("note_embeddings.", "") == vector_index.index_expression_sql(dim)
    assert right.endswith(f"AS HALFVEC({dim}))")
    assert f"({vector_index.index_expression_sql(dim)}) halfvec_cosine_ops" in (
        vector_index.create_index_sql(dim)
    )


def test_the_query_dimension_is_the_query_vectors():
    assert "HALFVEC(8)" in _sql(vector_index.order_expr([0.1] * 8))


def test_above_the_limit_order_and_full_are_one_plain_expression():
    order, full = vector_index.order_and_full_distance([0.1] * 2001)
    assert order is full
    assert "HALFVEC" not in _sql(order).upper()


# --------------------------------------------------------------------------- #
# 3. The queries
# --------------------------------------------------------------------------- #
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


class _Session:
    """Serves one batch per non-`SET LOCAL` statement, recording each."""

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


def _embedding(monkeypatch, dim):
    async def _fake(_text):
        return [1.0] + [0.0] * (dim - 1)

    monkeypatch.setattr("src.services.embeddings.get_embedding", _fake)


def _order_by_sql(stmt) -> str:
    return " ".join(_sql(c) for c in stmt._order_by_clauses)


@pytest.mark.asyncio
async def test_semantic_search_orders_by_the_index_and_selects_full_distance(monkeypatch):
    _embedding(monkeypatch, 1024)
    session = _Session([[]])
    await semantic_search(session, "q", limit=5)
    stmt = session.statements[0]
    order_sql = _order_by_sql(stmt)
    assert order_sql.startswith("CAST(note_embeddings.embedding AS HALFVEC(1024)) <=>")
    distance = next(c for c in stmt.selected_columns if c.name == "distance")
    assert "HALFVEC" not in _sql(distance).upper()
    assert "note_embeddings.embedding <=>" in _sql(distance)


@pytest.mark.asyncio
async def test_semantic_search_above_the_limit_has_no_cast(monkeypatch):
    _embedding(monkeypatch, 2001)
    session = _Session([[]])
    await semantic_search(session, "q", limit=5)
    assert "HALFVEC" not in _sql(session.statements[0]).upper()


def test_find_related_statement_orders_by_the_index():
    stmt = tools.find_related_stmt(1, [1.0] + [0.0] * 1023, None, 10)
    assert _order_by_sql(stmt).startswith(
        "CAST(note_embeddings.embedding AS HALFVEC(1024)) <=>"
    )
    distance = next(c for c in stmt.selected_columns if c.name == "distance")
    assert "HALFVEC" not in _sql(distance).upper()


def test_find_related_statement_above_the_limit_has_no_cast():
    stmt = tools.find_related_stmt(1, [1.0] + [0.0] * 2000, None, 10)
    assert "HALFVEC" not in _sql(stmt).upper()


def test_the_prewarm_probe_orders_by_the_index_expression(monkeypatch):
    monkeypatch.setattr("src.services.indexer.settings.embedding_dimensions", 1024)
    order = _order_by_sql(probe_statement())
    assert order.startswith("CAST(note_embeddings.embedding AS HALFVEC(1024)) <=>")
    assert len(_probe_vector()) == 1024


def _semantic_row(note_id, path, distance, chunk_index):
    return SimpleNamespace(
        note_id=note_id, chunk_index=chunk_index, chunk_text=f"{path}#{chunk_index}",
        file_path=path, title=path, tags=[], content_hash="h",
        embedded_content_hash="h", chunks_truncated=False, distance=distance,
    )


@pytest.mark.asyncio
async def test_semantic_search_resorts_by_full_distance_before_the_dedupe(monkeypatch):
    """Rows arrive in the half-precision scan's order. The first `a.md` row is
    *not* its nearest chunk at full precision; the dedupe must keep chunk 1,
    and `b.md` must rank between."""
    _embedding(monkeypatch, 1024)
    rows = [
        _semantic_row(1, "a.md", 0.30, 0),
        _semantic_row(2, "b.md", 0.20, 0),
        _semantic_row(1, "a.md", 0.10, 1),
    ]
    results = await semantic_search(_Session([rows]), "q", limit=5)
    assert [(r["path"], r["chunk_index"]) for r in results] == [("a.md", 1), ("b.md", 0)]
    assert [r["similarity"] for r in results] == [pytest.approx(0.9), pytest.approx(0.8)]


def _related_row(note_id, path, distance, text):
    return SimpleNamespace(
        note_id=note_id, file_path=path, title=path, tags=[], chunk_text=text,
        distance=distance, content_hash="h", embedded_content_hash="h",
        chunks_truncated=False,
    )


class _FindRelatedSession(_Session):
    def __init__(self, batches):
        super().__init__(batches)
        source = SimpleNamespace(id=1, content_hash="h", embedded_content_hash="h")
        self._preamble = [[source], [[1.0] + [0.0] * 1023]]

    async def execute(self, clause, *_a, **_k):
        if not str(clause).lstrip().upper().startswith("SET LOCAL") and self._preamble:
            return _Result(self._preamble.pop(0))
        return await super().execute(clause, *_a, **_k)


@pytest.mark.asyncio
async def test_find_related_resorts_by_full_distance_before_the_dedupe(monkeypatch):
    rows = [
        _related_row(2, "b.md", 0.30, "far chunk of b"),
        _related_row(3, "c.md", 0.20, "c"),
        _related_row(2, "b.md", 0.10, "near chunk of b"),
    ]
    monkeypatch.setattr(tools, "async_session", lambda: _FindRelatedSession([rows]))
    out = await tools.find_related_impl("a.md", limit=5)
    lines = [line for line in out.splitlines() if line.startswith("- **")]
    assert "`b.md`" in lines[0] and "sim: 0.900" in lines[0], out
    assert "`c.md`" in lines[1] and "sim: 0.800" in lines[1], out
    assert "near chunk of b" in out and "far chunk of b" not in out
