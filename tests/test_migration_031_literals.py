"""Migration 031 mirrors three application constants as literals, as a
migration must (it cannot drift with the app): the index generation lock key,
the keyword fingerprint key and version, and the column marker. Pin them.
"""
import importlib.util
import pathlib

from src.models import db
from src.services import index_state

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load_031():
    path = ROOT / "alembic" / "versions" / "031_keyword_head_and_excerpt.py"
    spec = importlib.util.spec_from_file_location("migration_031", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_031_mirrors_the_generation_lock_key():
    m = _load_031()
    assert m.INDEX_GENERATION_LOCK_KEY == index_state.INDEX_GENERATION_LOCK_KEY


def test_031_mirrors_the_fingerprint_key_and_version():
    m = _load_031()
    assert m.KEY_FTS_FINGERPRINT == index_state.KEY_FTS_FINGERPRINT
    assert m.FINGERPRINT_VERSION == index_state.FINGERPRINT_VERSION


def test_031_mirrors_the_column_marker():
    m = _load_031()
    assert m.MARKER == db._BODY_EXCERPT_COLUMN_MARKER
    assert db.NoteMetadata.__table__.c.body_excerpt.comment == m.MARKER


def test_031_chains_from_030():
    m = _load_031()
    assert (m.revision, m.down_revision) == ("031", "030")
