"""Migration 034 mirrors application constants as literals, as a migration
must: the index generation lock key, the five column markers and the two CHECK
predicates. Pin them, and pin that the ORM declares the same shape.
"""
import importlib.util
import pathlib

from src.models import db
from src.services import index_state

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load_034():
    path = ROOT / "alembic" / "versions" / "034_heading_chunks.py"
    spec = importlib.util.spec_from_file_location("migration_034", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_034_mirrors_the_generation_lock_key():
    assert _load_034().INDEX_GENERATION_LOCK_KEY == index_state.INDEX_GENERATION_LOCK_KEY


def test_034_mirrors_every_column_marker():
    m = _load_034()
    tables = {
        "note_embeddings": db.NoteEmbedding.__table__,
        "notes_metadata": db.NoteMetadata.__table__,
    }
    for table, column, _ddl, _type, marker in m.COLUMNS:
        col = tables[table].c[column]
        assert col.comment == marker, (table, column)
        assert col.nullable is True
        assert col.server_default is None


def test_034_mirrors_both_check_predicates():
    m = _load_034()
    assert m.HEADING_META_PREDICATE == db._HEADING_META_PREDICATE
    assert m.CHUNKING_VERSION_PREDICATE == db._CHUNKING_VERSION_PREDICATE
    names = {name for name, *_ in m.CHECKS}
    assert names == {db._HEADING_META_CHECK_NAME, db._CHUNKING_VERSION_CHECK_NAME}


def test_034_chains_from_033():
    m = _load_034()
    assert (m.revision, m.down_revision) == ("034", "033")


def test_034_bounds_both_directions():
    """`lock_timeout` / `statement_timeout` are set and reset in upgrade()
    and in downgrade(), whose generation-lock wait they bound."""
    import inspect

    m = _load_034()
    for fn in (m.upgrade, m.downgrade):
        src = inspect.getsource(fn)
        for setting in ("lock_timeout", "statement_timeout"):
            assert f"SET LOCAL {setting}" in src, (fn.__name__, setting)
            assert f"RESET {setting}" in src, (fn.__name__, setting)
    down = inspect.getsource(m.downgrade)
    assert down.index("SET LOCAL lock_timeout") < down.index("pg_advisory_xact_lock")
