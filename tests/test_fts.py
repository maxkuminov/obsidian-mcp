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


def _head(i: int, bind: str) -> str:
    return f"setweight(to_tsvector(CAST(:fts_cfg_{i} AS regconfig), :{bind}), 'A')"


def _tags(i: int, bind: str = "tags_text") -> str:
    return f"setweight(to_tsvector(CAST(:fts_cfg_{i} AS regconfig), :{bind}), 'B')"


def _body(i: int, bind: str = "content") -> str:
    return f"to_tsvector(CAST(:fts_cfg_{i} AS regconfig), :{bind})"


def test_index_tsvector_sql_single_config(monkeypatch):
    """`A(title) || A(aliases) || B(tags) || D(body)` (#344 D1, #391 D1): the
    body part is the pre-031 fragment byte for byte, so a pre-031 vector is
    exactly the body part of a post-031 one."""
    fts = _set_configs(monkeypatch, ["english"])
    frag, params = fts.index_tsvector_sql("content", tags_bind="tags_text")
    assert frag == (
        f"{_head(0, 'title')} || {_head(0, 'aliases')} || {_tags(0)} || {_body(0)}"
    )
    assert params == {"fts_cfg_0": "english"}
    assert fts.body_tsvector_sql("content") == (_body(0), params)


def test_index_tsvector_sql_single_config_without_tags(monkeypatch):
    """`tags_bind=None` (031's call only) omits the tags part: the pre-#391
    fragment."""
    fts = _set_configs(monkeypatch, ["english"])
    frag, _ = fts.index_tsvector_sql("content", tags_bind=None)
    assert frag == f"{_head(0, 'title')} || {_head(0, 'aliases')} || {_body(0)}"


def test_index_tsvector_sql_two_configs(monkeypatch):
    """Title parts for every config, then aliases parts, then tags parts,
    then content parts — the order the design fixes, so every writer stores
    one shape."""
    fts = _set_configs(monkeypatch, ["english", "norwegian"])
    frag, params = fts.index_tsvector_sql("content", tags_bind="tags_text")
    assert frag == " || ".join([
        _head(0, "title"), _head(1, "title"),
        _head(0, "aliases"), _head(1, "aliases"),
        _tags(0), _tags(1),
        _body(0), _body(1),
    ])
    assert params == {"fts_cfg_0": "english", "fts_cfg_1": "norwegian"}
    frag, _ = fts.index_tsvector_sql("content", tags_bind=None)
    assert frag == " || ".join([
        _head(0, "title"), _head(1, "title"),
        _head(0, "aliases"), _head(1, "aliases"),
        _body(0), _body(1),
    ])


def test_index_tsvector_sql_three_configs(monkeypatch):
    fts = _set_configs(monkeypatch, ["simple", "english", "norwegian"])
    frag, params = fts.index_tsvector_sql("content", tags_bind="tags_text")
    assert frag.count("to_tsvector(") == 12
    assert frag.count("setweight(") == 9
    assert frag.count("'B')") == 3
    assert frag.count(" || ") == 11
    untagged, _ = fts.index_tsvector_sql("content", tags_bind=None)
    assert untagged.count("to_tsvector(") == 9
    assert untagged.count("setweight(") == 6
    assert untagged.count(" || ") == 8
    # The builders sort the configs (#344, V-N4): 031 builds under the
    # fingerprint's sorted list, so the writers must too, whatever order
    # `FTS_CONFIGS` names them in.
    assert params == {
        "fts_cfg_0": "english",
        "fts_cfg_1": "norwegian",
        "fts_cfg_2": "simple",
    }


def test_index_tsvector_sql_honors_the_bind_names(monkeypatch):
    fts = _set_configs(monkeypatch, ["english"])
    frag, _ = fts.index_tsvector_sql("body", "t", "al", tags_bind="tg")
    assert frag == (
        f"{_head(0, 't')} || {_head(0, 'al')} || {_tags(0, 'tg')} || {_body(0, 'body')}"
    )


def test_tags_tsvector_sql_is_the_tags_parts_alone(monkeypatch):
    fts = _set_configs(monkeypatch, ["english"])
    assert fts.tags_tsvector_sql("head_tags") == (
        _tags(0, "head_tags"), {"fts_cfg_0": "english"}
    )
    frag, params = fts.tags_tsvector_sql("t", configs=["simple", "english"])
    assert frag == f"{_tags(0, 't')} || {_tags(1, 't')}"
    assert params == {"fts_cfg_0": "english", "fts_cfg_1": "simple"}


def test_rehead_tsvector_sql_keeps_the_d_part_only(monkeypatch):
    """`head || ts_filter(content_tsvector, '{d}')`: the head (title, aliases,
    tags) from stored columns, the body kept, any previous head dropped."""
    fts = _set_configs(monkeypatch, ["english"])
    frag, params = fts.rehead_tsvector_sql(
        "head_title", "head_aliases", tags_bind="head_tags"
    )
    assert frag == (
        f"{_head(0, 'head_title')} || {_head(0, 'head_aliases')} "
        f"|| {_tags(0, 'head_tags')} || ts_filter(content_tsvector, '{{d}}')"
    )
    assert params == {"fts_cfg_0": "english"}


#: Migration 031's re-head fragment as it was built before #391, for the
#: configs `["english", "simple"]`, written out by hand. 031 now passes
#: `tags_bind=None`; its bytes must not move (#391, D1).
PRE_391_031_FRAGMENT = (
    "setweight(to_tsvector(CAST(:fts_cfg_0 AS regconfig), :head_title), 'A') || "
    "setweight(to_tsvector(CAST(:fts_cfg_1 AS regconfig), :head_title), 'A') || "
    "setweight(to_tsvector(CAST(:fts_cfg_0 AS regconfig), :head_aliases), 'A') || "
    "setweight(to_tsvector(CAST(:fts_cfg_1 AS regconfig), :head_aliases), 'A') || "
    "ts_filter(content_tsvector, '{d}')"
)


def test_031s_fragment_is_byte_identical_to_its_pre_391_text(monkeypatch):
    fts = _set_configs(monkeypatch, ["english"])
    frag, params = fts.rehead_tsvector_sql(
        "head_title", "head_aliases", ["simple", "english"], tags_bind=None
    )
    assert frag == PRE_391_031_FRAGMENT
    assert params == {"fts_cfg_0": "english", "fts_cfg_1": "simple"}
    # And the call 031 actually makes is that call.
    import pathlib

    src = next(
        (pathlib.Path(__file__).resolve().parent.parent / "alembic/versions")
        .glob("031_*.py")
    ).read_text(encoding="utf-8")
    assert (
        'rehead_tsvector_sql(\n        "head_title", "head_aliases", configs, '
        "tags_bind=None\n    )"
    ) in src


def test_the_builders_require_the_tags_bind(monkeypatch):
    fts = _set_configs(monkeypatch, ["english"])
    for builder in (fts.head_tsvector_sql, fts.index_tsvector_sql, fts.rehead_tsvector_sql):
        with pytest.raises(TypeError):
            builder()


def test_builders_take_explicit_configs_for_the_migration(monkeypatch):
    """031/035 build under the configs the stored fingerprint names, not the
    environment's — the builders accept them explicitly."""
    fts = _set_configs(monkeypatch, ["english"])
    frag, params = fts.rehead_tsvector_sql(configs=["simple", "norwegian"], tags_bind=None)
    assert params == {"fts_cfg_0": "norwegian", "fts_cfg_1": "simple"}  # sorted
    assert frag.count("setweight(") == 4
    frag, _ = fts.rehead_tsvector_sql(configs=["simple", "norwegian"], tags_bind="t")
    assert frag.count("setweight(") == 6


def test_builders_produce_one_sql_whatever_the_config_order(monkeypatch):
    """V-N4: the writers (settings order) and the migrations (the
    fingerprint's sorted list) must build byte-equal vectors, so the builders
    sort."""
    fts = _set_configs(monkeypatch, ["simple", "english"])
    from_settings = (
        fts.index_tsvector_sql(tags_bind="t"), fts.rehead_tsvector_sql(tags_bind="t")
    )
    _set_configs(monkeypatch, ["english", "simple"])
    assert (
        fts.index_tsvector_sql(tags_bind="t"), fts.rehead_tsvector_sql(tags_bind="t")
    ) == from_settings
    assert fts.rehead_tsvector_sql(configs=["simple", "english"], tags_bind="t") == from_settings[1]


def test_index_tsvector_sql_never_interpolates_config_name(monkeypatch):
    # A hostile config name must appear only as a bound value, never inline.
    fts = _set_configs(monkeypatch, ["english'; DROP TABLE notes_metadata; --"])
    frag, params = fts.index_tsvector_sql("content", tags_bind="tags_text")
    assert "DROP TABLE" not in frag
    assert params["fts_cfg_0"] == "english'; DROP TABLE notes_metadata; --"


# ── tags_text (#391, design D2) ──────────────────────────────────────────


@pytest.mark.parametrize(
    "tags,expected",
    [
        (None, ""),
        ([], ""),
        (["hoa"], "hoa"),
        (["ctx/phone", "hoa"], "ctx/phone ctx phone\nhoa"),
        (["a/b/c"], "a/b/c a b c"),
        (["#hoa"], "#hoa"),  # the parser reads `#` as a blank
        (["hoa", "hoa"], "hoa\nhoa"),  # the extractor de-duplicates; this rule does not
        (["a//b/"], "a//b/ a b"),
        (["x\x00y", "\x02z\x03"], "xy\nz"),
        (["\x00"], ""),
        (["project-x"], "project-x"),
    ],
)
def test_tags_text(tags, expected):
    from src.services.fts import tags_text

    assert tags_text(tags) == expected


def test_tags_text_is_bounded_at_whole_tags():
    from src.services.fts import FTS_TAGS_MAX_CHARS, tags_text

    tags = [f"{i:04d}" + "t" * 26 for i in range(1000)]
    out = tags_text(tags)
    assert FTS_TAGS_MAX_CHARS == 2048
    assert len(out) <= 2048
    lines = out.split("\n")
    assert lines == tags[: len(lines)]  # whole lines, in order, none cut
    assert len(out) + 1 + 30 > 2048  # the next whole line would not fit


def test_tags_text_drops_a_nested_line_that_would_cross_the_bound():
    from src.services.fts import tags_text

    first = "a" * 2010
    out = tags_text([first, "b" * 10 + "/" + "c" * 10, "d"])
    # `first` + `\n` + the 43-character nested line crosses 2,048: that line
    # and every later one are dropped, even though `d` alone would fit.
    assert out == first


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

    frag, params = fts.body_tsvector_sql("content")
    assert frag == "to_tsvector(CAST(:fts_cfg_0 AS regconfig), :content)"
    assert params == {"fts_cfg_0": "english"}
    full, _ = fts.index_tsvector_sql("content", tags_bind="tags_text")
    assert full.endswith(" || " + frag)

    sql = _compile(fts.combined_tsquery("hello"))
    assert sql.count("websearch_to_tsquery(") == 1
    assert "||" not in sql


# ── _rebuild_tsvectors_single_scope_for_tests (offline: fake session, real file IO) ───────────────


@pytest.mark.asyncio
async def test_rebuild_tsvectors_updates_every_note(monkeypatch, tmp_path):
    """`_rebuild_tsvectors_single_scope_for_tests` re-reads each indexed note and issues an UPDATE that
    sets `content_tsvector` under the configured config(s), carrying the FTS
    config bind params. Fully offline — no DB, no network, no embeddings."""
    import hashlib
    from collections import namedtuple

    import src.services.indexer as indexer
    from sqlalchemy.sql.elements import TextClause

    # The rebuild certifies against owner + path + hash (#127), so the stub's
    # rows have to carry what it retains, and the hashes have to be the real
    # ones for the files below — the rebuild verifies the bytes it reads.
    Row = namedtuple("Row", ["id", "user_id", "file_path", "content_hash"])

    vault = tmp_path / "vault"
    vault.mkdir()
    bodies = {
        "a.md": "---\ntitle: A\n---\nalpha body\n",
        "b.md": "---\ntitle: B\n---\nbeta body\n",
    }
    for name, body in bodies.items():
        (vault / name).write_text(body, encoding="utf-8")

    monkeypatch.setattr(indexer.settings, "vault_path", str(vault), raising=False)
    monkeypatch.setattr(indexer.settings, "fts_configs", ["simple", "norwegian"], raising=False)

    rows = [
        Row(i, None, name, hashlib.sha256(body.encode("utf-8")).hexdigest())
        for i, (name, body) in enumerate(bodies.items(), start=1)
    ]

    class _Result:
        def __init__(self, data=None, rowcount=1):
            self._data = data or []
            self.rowcount = rowcount

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
    n = await indexer._rebuild_tsvectors_single_scope_for_tests(session, user_id=None)

    assert n == 2
    assert len(session.updates) == 2
    ids = {p["id"] for _, p in session.updates}
    assert ids == {1, 2}
    for text_sql, p in session.updates:
        # Both configs present as bound params; names never interpolated.
        # Sorted by the builders (V-N4), not in `FTS_CONFIGS` order.
        assert p["fts_cfg_0"] == "norwegian"
        assert p["fts_cfg_1"] == "simple"
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
