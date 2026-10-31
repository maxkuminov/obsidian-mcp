"""Migration 035 mirrors application constants as literals, as a migration
must (it cannot drift with the app): the index generation lock key and the
keyword fingerprint key and version. Pin them, and its place in the chain."""
import importlib.util
import pathlib

from src.services import index_state

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load(name: str):
    path = next((ROOT / "alembic" / "versions").glob(f"{name}_*.py"))
    spec = importlib.util.spec_from_file_location(f"migration_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_035_mirrors_the_generation_lock_key():
    assert _load("035").INDEX_GENERATION_LOCK_KEY == index_state.INDEX_GENERATION_LOCK_KEY


def test_035_mirrors_the_fingerprint_key_and_version():
    m = _load("035")
    assert m.KEY_FTS_FINGERPRINT == index_state.KEY_FTS_FINGERPRINT
    assert m.FINGERPRINT_VERSION == index_state.FINGERPRINT_VERSION


def test_035_chains_from_034():
    m = _load("035")
    assert (m.revision, m.down_revision) == ("035", "034")


def test_035_preflight_threshold():
    m = _load("035")
    assert m.TSVECTOR_LIMIT_BYTES == 1048575
    assert m.PREFLIGHT_MAX_BYTES == 1048511


def test_035_size_sql_is_the_documented_formula():
    m = _load("035")
    assert m.size_sql("v") == (
        "(SELECT coalesce(sum((octet_length(lexeme) + 1) / 2 * 2 "
        "+ CASE WHEN cardinality(positions) > 0 "
        "THEN 2 + 2 * cardinality(positions) ELSE 0 END), 0) "
        "FROM unnest(v))"
    )


def test_035_row_failure_names_id_and_sqlstate_only():
    m = _load("035")

    class _Orig(Exception):
        sqlstate = "54000"

    class _Wrapped(Exception):
        orig = _Orig("string is too long for tsvector; secret note text")

    err = m._row_failure("rewrite", 42, _Wrapped("UPDATE … [parameters: secret]"))
    text = str(err)
    assert "id=42" in text and "SQLSTATE 54000" in text and "034" in text
    assert "secret" not in text
