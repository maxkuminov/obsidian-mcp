"""Unit tests for the configurable full-text-search layer.

Covers the `FTS_CONFIGS` config parser/validator and the SQL-building helpers
in `src/services/fts.py`. Integration behavior (real Postgres stemming) lives
in `test_fts_integration.py`, which is skipped without a database.
"""
import pytest
from pydantic import ValidationError

from src.config import Settings


# ── Config parsing / validation ──────────────────────────────────────────


def test_fts_configs_default_is_english():
    s = Settings(_env_file=None)
    assert s.fts_configs == ["english"]


def test_fts_configs_parses_json_list():
    s = Settings(fts_configs='["simple","norwegian"]', _env_file=None)
    assert s.fts_configs == ["simple", "norwegian"]


def test_fts_configs_parses_csv():
    s = Settings(fts_configs="simple, norwegian", _env_file=None)
    assert s.fts_configs == ["simple", "norwegian"]


def test_fts_configs_lowercases_and_strips():
    s = Settings(fts_configs="  English , NORWEGIAN ", _env_file=None)
    assert s.fts_configs == ["english", "norwegian"]


def test_fts_configs_dedupes_preserving_order():
    s = Settings(fts_configs="english,english,simple,english", _env_file=None)
    assert s.fts_configs == ["english", "simple"]


def test_fts_configs_accepts_python_list():
    s = Settings(fts_configs=["English", " simple "], _env_file=None)
    assert s.fts_configs == ["english", "simple"]


def test_fts_configs_drops_empty_entries():
    s = Settings(fts_configs="english,,  ,simple", _env_file=None)
    assert s.fts_configs == ["english", "simple"]


def test_fts_configs_rejects_empty_string():
    with pytest.raises(ValidationError):
        Settings(fts_configs="", _env_file=None)


def test_fts_configs_rejects_empty_list():
    with pytest.raises(ValidationError):
        Settings(fts_configs=[], _env_file=None)


def test_fts_configs_rejects_whitespace_only():
    with pytest.raises(ValidationError):
        Settings(fts_configs="   ,  ", _env_file=None)


def test_fts_configs_rejects_invalid_bracketed_json():
    # Leading "[" signals JSON intent; malformed JSON must fail loudly rather
    # than silently CSV-splitting into junk config names.
    with pytest.raises(ValidationError):
        Settings(fts_configs="[english, norwegian", _env_file=None)


def test_fts_configs_rejects_non_list_json():
    with pytest.raises(ValidationError):
        Settings(fts_configs='["english"', _env_file=None)


# ── index_tsvector_sql ───────────────────────────────────────────────────


def _set_configs(monkeypatch, cfgs):
    """Point the fts module's `settings.fts_configs` at `cfgs` for one test."""
    from src.services import fts

    monkeypatch.setattr(fts.settings, "fts_configs", cfgs, raising=False)
    return fts


def test_index_tsvector_sql_single_config(monkeypatch):
    fts = _set_configs(monkeypatch, ["english"])
    frag, params = fts.index_tsvector_sql("content")
    assert frag == "to_tsvector(CAST(:fts_cfg_0 AS regconfig), :content)"
    assert params == {"fts_cfg_0": "english"}


def test_index_tsvector_sql_two_configs(monkeypatch):
    fts = _set_configs(monkeypatch, ["english", "norwegian"])
    frag, params = fts.index_tsvector_sql("content")
    assert frag == (
        "to_tsvector(CAST(:fts_cfg_0 AS regconfig), :content)"
        " || to_tsvector(CAST(:fts_cfg_1 AS regconfig), :content)"
    )
    assert params == {"fts_cfg_0": "english", "fts_cfg_1": "norwegian"}


def test_index_tsvector_sql_three_configs(monkeypatch):
    fts = _set_configs(monkeypatch, ["simple", "english", "norwegian"])
    frag, params = fts.index_tsvector_sql("content")
    assert frag.count("to_tsvector(") == 3
    assert frag.count(" || ") == 2
    assert params == {
        "fts_cfg_0": "simple",
        "fts_cfg_1": "english",
        "fts_cfg_2": "norwegian",
    }


def test_index_tsvector_sql_honors_content_bind(monkeypatch):
    fts = _set_configs(monkeypatch, ["english"])
    frag, _ = fts.index_tsvector_sql("body")
    assert frag == "to_tsvector(CAST(:fts_cfg_0 AS regconfig), :body)"


def test_index_tsvector_sql_never_interpolates_config_name(monkeypatch):
    # A hostile config name must appear only as a bound value, never inline.
    fts = _set_configs(monkeypatch, ["english'; DROP TABLE notes_metadata; --"])
    frag, params = fts.index_tsvector_sql("content")
    assert "DROP TABLE" not in frag
    assert params["fts_cfg_0"] == "english'; DROP TABLE notes_metadata; --"


# ── combined_tsquery ─────────────────────────────────────────────────────


def _compile(expr):
    from sqlalchemy.dialects import postgresql

    return str(expr.compile(dialect=postgresql.dialect()))


def test_combined_tsquery_single_config(monkeypatch):
    fts = _set_configs(monkeypatch, ["english"])
    sql = _compile(fts.combined_tsquery("hello"))
    assert sql.count("websearch_to_tsquery(") == 1
    assert "||" not in sql


def test_combined_tsquery_ors_two_configs(monkeypatch):
    fts = _set_configs(monkeypatch, ["english", "norwegian"])
    sql = _compile(fts.combined_tsquery("hello"))
    assert sql.count("websearch_to_tsquery(") == 2
    assert "||" in sql


def test_combined_tsquery_ors_three_configs(monkeypatch):
    fts = _set_configs(monkeypatch, ["simple", "english", "norwegian"])
    sql = _compile(fts.combined_tsquery("hello"))
    assert sql.count("websearch_to_tsquery(") == 3
    assert sql.count("||") == 2


# ── Backward-compat regression: default == historical single-english SQL ──


def test_default_config_reproduces_single_english_behavior(monkeypatch):
    """The default `["english"]` must build exactly one english-config
    tsvector/tsquery — i.e. byte-for-byte the pre-feature behavior (a
    single-element concat/OR is identical to a lone call)."""
    s = Settings(_env_file=None)
    assert s.fts_configs == ["english"]

    fts = _set_configs(monkeypatch, s.fts_configs)

    frag, params = fts.index_tsvector_sql("content")
    assert frag == "to_tsvector(CAST(:fts_cfg_0 AS regconfig), :content)"
    assert params == {"fts_cfg_0": "english"}

    sql = _compile(fts.combined_tsquery("hello"))
    assert sql.count("websearch_to_tsquery(") == 1
    assert "||" not in sql


# ── rebuild_tsvectors (offline: fake session, real file IO) ───────────────


@pytest.mark.asyncio
async def test_rebuild_tsvectors_updates_every_note(monkeypatch, tmp_path):
    """`rebuild_tsvectors` re-reads each indexed note and issues an UPDATE that
    sets `content_tsvector` under the configured config(s), carrying the FTS
    config bind params. Fully offline — no DB, no network, no embeddings."""
    from collections import namedtuple

    import src.services.indexer as indexer
    from sqlalchemy.sql.elements import TextClause

    Row = namedtuple("Row", ["id", "file_path"])

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "a.md").write_text("---\ntitle: A\n---\nalpha body\n", encoding="utf-8")
    (vault / "b.md").write_text("---\ntitle: B\n---\nbeta body\n", encoding="utf-8")

    monkeypatch.setattr(indexer.settings, "vault_path", str(vault), raising=False)
    monkeypatch.setattr(indexer.settings, "fts_configs", ["simple", "norwegian"], raising=False)

    rows = [Row(1, "a.md"), Row(2, "b.md")]

    class _Result:
        def __init__(self, data=None):
            self._data = data or []

        def all(self):
            return self._data

    class _FakeSession:
        def __init__(self):
            self.updates = []

        def begin_nested(self):
            """The savepoint each keyword-vector attempt runs in (#127, D4).

            A no-op here: this stub records statements rather than executing them,
            so there is nothing to roll back. What a stub can never show is that
            the driver's aborted-transaction state clears between attempts — that
            is why the retreat has a real-PostgreSQL test
            (`tests/integration/test_tsvector_bounded_pg.py`).
            """
            class _Savepoint:
                async def __aenter__(self_inner):
                    return self_inner

                async def __aexit__(self_inner, *exc):
                    return False

            return _Savepoint()

        async def execute(self, stmt, params=None):
            if isinstance(stmt, TextClause) and "content_tsvector" in stmt.text:
                self.updates.append((stmt.text, params))
                return _Result()
            # The note-listing SELECT (a SQLAlchemy Select) returns our rows.
            return _Result(rows)

        async def commit(self):
            return None

    session = _FakeSession()
    n = await indexer.rebuild_tsvectors(session, user_id=None)

    assert n == 2
    assert len(session.updates) == 2
    ids = {p["id"] for _, p in session.updates}
    assert ids == {1, 2}
    for text_sql, p in session.updates:
        # Both configs present as bound params; names never interpolated.
        assert p["fts_cfg_0"] == "simple"
        assert p["fts_cfg_1"] == "norwegian"
        assert "CAST(:fts_cfg_0 AS regconfig)" in text_sql
        assert "CAST(:fts_cfg_1 AS regconfig)" in text_sql
        assert "body" in p["content"]


# ── validate_fts_configs (offline: fake session over pg_ts_config) ────────


class _ConfigRowsSession:
    """Minimal async session whose execute() returns a fixed set of
    `pg_ts_config` rows, mimicking the (cfgname,) tuples the real query yields."""

    def __init__(self, available):
        self._rows = [(name,) for name in available]

    async def execute(self, *args, **kwargs):
        return self._rows  # iterating yields (cfgname,) tuples


@pytest.mark.asyncio
async def test_validate_fts_configs_passes_when_all_present(monkeypatch):
    fts = _set_configs(monkeypatch, ["english", "norwegian"])
    session = _ConfigRowsSession(["english", "norwegian", "simple"])
    # Must not raise.
    await fts.validate_fts_configs(session)


@pytest.mark.asyncio
async def test_validate_fts_configs_raises_on_unknown(monkeypatch):
    fts = _set_configs(monkeypatch, ["english", "klingon"])
    session = _ConfigRowsSession(["english", "norwegian", "simple"])
    with pytest.raises(SystemExit) as exc:
        await fts.validate_fts_configs(session)
    msg = str(exc.value)
    assert "klingon" in msg  # the offending config is named
    assert "english" not in msg.split("Available:")[0]  # not flagged as missing
