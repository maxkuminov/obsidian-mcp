"""#154 — non-finite frontmatter numbers, coerced at the boundaries.

`x: .nan` is *valid* YAML and a value both YAML and Python render, so
`_scrub_frontmatter` deliberately keeps it: its predicate is "nothing can
render this", and coercing there would put the string `".nan"` into the
mapping `set_frontmatter` re-serialises — rewriting a note's own bytes as a
side effect of setting an unrelated key, which is the destructive-write class.
The fix therefore lives at each *boundary*: the indexer's JSONB write, the
indexer's title, `vault.read_file`'s title (which `read_note` and the panel
inherit) and `read_note`'s frontmatter view.

**Today's behaviour, observed before anything changed** (task 4.1 — the design
reasoned this from the code; these are the values that actually came back, run
against the tree at the parent commit):

    _view_leaf(nan)                  -> 'nan'      (Python's spelling, silent)
    _view_leaf(inf) / (-inf)         -> 'inf' / '-inf'
    _view_key(nan)                   -> 'nan'
    frontmatter_view({'x': nan})     -> ({'x': 'nan'}, None)   # no coercion said
    indexer._note_title({'title': nan}) -> 'nan'
    indexer._sanitize_frontmatter({'x': nan}) -> {'x': nan}     # float, untouched
    json.dumps(that)                 -> '{"x": NaN}'            # not JSON
    _sanitize_frontmatter({nan: 1, 'nan': 2}) -> {'nan': 2}     # LAST key won

The last two are the bug: `NaN` is not JSON, PostgreSQL's `jsonb` parser
rejects it, and the batch upsert has no per-note retreat — see
`tests/integration/test_issue_154_non_finite_frontmatter_pg.py`, which pins
that half against a real column. The `{'nan': 2}` line is why first-key-wins
had to be *stated*: today's winner is an accident of iteration order.

Offline: a `tmp_path` vault, `_log_usage` stubbed, no DB.
"""

import json
import math
import os
import tempfile

os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
os.chdir(tempfile.gettempdir())

import datetime as _dt  # noqa: E402

import pytest  # noqa: E402

import src.mcp_server.tools as tools  # noqa: E402
from src.mcp_server.auth import current_permission  # noqa: E402
from src.mcp_server.read_result import (  # noqa: E402
    COERCED_NON_FINITE_FLOAT,
    OMITTED_COERCED_DUPLICATE_KEY,
    OMITTED_DUPLICATE_KEY,
    ReadNoteResult,
    _view_key,
    _view_leaf,
    frontmatter_view,
)
from src.mcp_server.server import mcp  # noqa: E402
from src.services import indexer  # noqa: E402
from src.services import vault as vault_service  # noqa: E402

NAN = float("nan")
INF = float("inf")
NEG_INF = float("-inf")


# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _no_usage_log(monkeypatch):
    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(tools, "_log_usage", _noop)


@pytest.fixture(autouse=True)
def _writable():
    token = current_permission.set("readwrite")
    yield
    current_permission.reset(token)


@pytest.fixture
def vault(monkeypatch, tmp_path):
    monkeypatch.setattr(tools.settings, "vault_path", str(tmp_path))
    vault_service.clear_user_vault_cache()
    return tmp_path


def write(vault, name, text):
    (vault / name).write_text(text, encoding="utf-8")
    return name


def view_budget():
    return {"nodes": 5_000, "chars": 100_000, "coerced": False}


# ══════════════════════════════════════════════════════════════════════════
# 1. today's failure mechanism, pinned by the primitive it runs on
# ══════════════════════════════════════════════════════════════════════════


def test_a_bare_non_finite_float_serializes_to_something_that_is_not_json():
    """The mechanism behind #154, pinned at the layer that cannot be fixed.

    SQLAlchemy sets no `json_serializer` on the engine, so a `float('nan')`
    reaching the `frontmatter` JSONB column is serialized by stock
    `json.dumps`, which with its default `allow_nan=True` emits the bare
    tokens below. None of the three is JSON — a conforming parser rejects
    them, and PostgreSQL's `jsonb` parser is one. This test does not depend on
    the sanitiser and therefore survives the fix: it is the reason the fix has
    to exist.
    """
    assert json.dumps({"x": NAN}) == '{"x": NaN}'
    assert json.dumps({"a": INF, "b": NEG_INF}) == '{"a": Infinity, "b": -Infinity}'

    def reject(token):  # what a strict parser does with the constants
        raise ValueError(f"{token} is not JSON")

    for payload in ('{"x": NaN}', '{"a": Infinity}', '{"a": -Infinity}'):
        with pytest.raises(ValueError):
            json.loads(payload, parse_constant=reject)


def test_the_python_spelling_is_gone_from_every_boundary():
    """The four boundaries no longer emit `nan` / `inf` / `-inf`.

    Each of these returned Python's spelling before this change (see the
    module docstring for the observed values); each returns YAML's now.
    """
    assert _view_leaf(NAN, view_budget()) == ".nan"
    assert _view_key(NAN) == ".nan"
    assert indexer._sanitize_frontmatter({"x": NAN}) == {"x": ".nan"}
    assert indexer._note_title({"title": NAN}, "n.md") == ".nan"
    assert vault_service.note_title({"title": NAN}, "n.md") == ".nan"


# ══════════════════════════════════════════════════════════════════════════
# 2. the read view
# ══════════════════════════════════════════════════════════════════════════


def test_the_view_renders_the_canonical_tokens():
    view, omission, coercions = frontmatter_view({"x": NAN, "a": INF, "b": NEG_INF})
    assert view == {"x": ".nan", "a": ".inf", "b": "-.inf"}
    assert omission is None
    assert [(c.field, c.reason) for c in coercions] == [
        ("frontmatter", COERCED_NON_FINITE_FLOAT)
    ]
    assert COERCED_NON_FINITE_FLOAT == "non_finite_float"


def test_positive_and_negative_infinity_stay_distinguishable():
    view, _, _ = frontmatter_view({"a": INF, "b": NEG_INF})
    assert view["a"] == ".inf"
    assert view["b"] == "-.inf"


def test_a_finite_float_is_neither_coerced_nor_reported():
    view, omission, coercions = frontmatter_view({"x": 1.5, "y": 0.0})
    assert view == {"x": 1.5, "y": 0.0}
    assert omission is None
    assert coercions == []


def test_a_boolean_is_not_a_non_finite_number():
    """`isinstance(True, int)` is true; the helper excludes `bool` on purpose."""
    view, _, coercions = frontmatter_view({"t": True, "f": False})
    assert view == {"t": True, "f": False}
    assert coercions == []


def test_a_non_finite_mapping_key_is_coerced_too():
    view, omission, coercions = frontmatter_view({NAN: 1, INF: 2})
    assert view == {".nan": 1, ".inf": 2}
    assert omission is None
    assert [c.reason for c in coercions] == [COERCED_NON_FINITE_FLOAT]


def test_a_coercion_induced_key_collision_omits_the_view_whole():
    """`.nan: 1` beside `".nan": 2` — #149's rule, with its own reason code.

    First-key-wins is deliberately NOT used in the view: keeping one of two
    keys silently emits a partial mapping, and a caller cannot tell a pruned
    view from a complete one (design D10, L14). The index, which has no channel
    to report a loss, takes first-key-wins instead — asserted in section 3.
    """
    view, omission, coercions = frontmatter_view({NAN: 1, ".nan": 2})
    assert view is None
    assert omission is not None
    assert omission.field == "frontmatter"
    assert omission.reason == OMITTED_COERCED_DUPLICATE_KEY
    assert omission.reason != OMITTED_DUPLICATE_KEY
    # Nothing is reported as retained-but-altered, because nothing was retained.
    assert coercions == []


def test_a_native_key_collision_keeps_its_own_reason_code():
    """`1:` beside `"1":` is a property of the note, not of this rendering."""
    _, omission, _ = frontmatter_view({1: "a", "1": "b"})
    assert omission is not None
    assert omission.reason == OMITTED_DUPLICATE_KEY


@pytest.mark.asyncio
async def test_read_note_reports_the_coercion_and_omits_nothing(vault):
    write(vault, "n.md", "---\nx: .nan\na: .inf\nb: -.inf\n---\nbody\n")
    out = await tools.read_note_impl("n.md")

    assert out.frontmatter == {"x": ".nan", "a": ".inf", "b": "-.inf"}
    assert out.metadata_coercions is not None
    assert [(c.field, c.reason) for c in out.metadata_coercions] == [
        ("frontmatter", COERCED_NON_FINITE_FLOAT)
    ]
    # A retained-but-altered value is not an omission and never appears as one.
    assert out.metadata_omissions is None
    # The block's own text is untouched and carries the note's spelling.
    assert "x: .nan" in out.frontmatter_yaml


@pytest.mark.asyncio
async def test_an_ordinary_note_carries_no_coercion_list(vault):
    write(vault, "n.md", "---\nx: 1\n---\nbody\n")
    out = await tools.read_note_impl("n.md")
    assert out.frontmatter == {"x": 1}
    # Absent, not an empty list: `metadata_coercions` means something happened.
    assert out.metadata_coercions is None
    assert "metadata_coercions" not in out.model_dump()


@pytest.mark.asyncio
async def test_alternate_spellings_render_canonically(vault):
    """YAML 1.1 accepts eight spellings; the parse preserves none of them."""
    write(vault, "n.md", "---\nx: .NaN\ny: .INF\nz: +.inf\nw: -.Inf\n---\nbody\n")
    out = await tools.read_note_impl("n.md")

    assert out.frontmatter == {"x": ".nan", "y": ".inf", "z": ".inf", "w": "-.inf"}
    # ...and `frontmatter_yaml` still shows what the note actually says.
    assert "x: .NaN" in out.frontmatter_yaml
    assert "z: +.inf" in out.frontmatter_yaml


@pytest.mark.asyncio
async def test_both_renderings_agree_and_neither_emits_a_non_json_token(vault):
    write(vault, "n.md", "---\nx: .nan\na: .inf\nb: -.inf\n---\nbody\n")
    blocks, structured = await mcp.call_tool("read_note", {"path": "n.md"})

    assert json.loads(blocks[0].text) == structured
    ReadNoteResult.model_validate(structured)
    for token in ("NaN", "Infinity", "-Infinity"):
        assert token not in blocks[0].text
    assert structured["frontmatter"] == {"x": ".nan", "a": ".inf", "b": "-.inf"}
    assert structured["metadata_coercions"][0]["reason"] == COERCED_NON_FINITE_FLOAT


@pytest.mark.asyncio
async def test_a_coercion_is_reported_apart_from_a_budget_omission(vault, monkeypatch):
    """One response, one drop and one coercion — each in its own list.

    The coercion names the field that is *still there*; the omission names the
    one that is gone. A dropped `frontmatter` view takes its own coercion entry
    with it, since an entry naming a field the caller did not receive would
    contradict the omission beside it.
    """
    monkeypatch.setattr(tools.settings, "max_read_response_chars", 400)
    # A long `frontmatter_yaml` forces the JSON view out first, then the block.
    padding = "p: " + "x" * 600
    write(vault, "n.md", f"---\nx: .nan\n{padding}\n---\nbody\n")
    out = await tools.read_note_impl("n.md")

    assert out.frontmatter is None  # dropped whole under the budget
    reasons = {o.reason for o in out.metadata_omissions}
    assert "metadata_budget" in reasons
    assert out.metadata_coercions is None

    # With room, the same note keeps both the view and its coercion entry.
    monkeypatch.setattr(tools.settings, "max_read_response_chars", 40_000)
    kept = await tools.read_note_impl("n.md")
    assert kept.frontmatter["x"] == ".nan"
    assert kept.metadata_coercions[0].reason == COERCED_NON_FINITE_FLOAT


# ══════════════════════════════════════════════════════════════════════════
# 3. the indexer's JSONB sanitiser
# ══════════════════════════════════════════════════════════════════════════


def test_the_sanitiser_output_is_json_and_carries_the_tokens():
    out = indexer._sanitize_frontmatter({"x": NAN, "a": INF, "b": NEG_INF})
    assert out == {"x": ".nan", "a": ".inf", "b": "-.inf"}
    # The whole point: this now survives the serializer the driver uses.
    assert json.dumps(out) == '{"x": ".nan", "a": ".inf", "b": "-.inf"}'


def test_the_sanitiser_reaches_into_containers():
    out = indexer._sanitize_frontmatter({"l": [NAN, 1], "d": {"k": INF}})
    assert out == {"l": [".nan", 1], "d": {"k": ".inf"}}
    json.dumps(out)


def test_the_sanitiser_coerces_keys_and_the_first_key_wins():
    """`.nan: 1` then `".nan": 2` — first in document order, stated.

    Before this change the dict comprehension kept the *last* (observed:
    `{'nan': 2}`), which was iteration order rather than a decision. The index
    has no channel through which to report the loss and must never fail the
    pass, so the remedy available is a documented winner.
    """
    assert indexer._sanitize_frontmatter({NAN: 1, ".nan": 2}) == {".nan": 1}
    assert indexer._sanitize_frontmatter({".nan": 2, NAN: 1}) == {".nan": 2}
    # A key that only *looks* alike is not a collision.
    assert indexer._sanitize_frontmatter({NAN: 1, "nan": 2}) == {".nan": 1, "nan": 2}


def test_the_sanitiser_leaves_everything_else_exactly_as_it_was():
    """The split must not change any value it was not written for."""
    fm = {
        "s": "text",
        "i": 42,
        "f": 1.5,
        "t": True,
        "n": None,
        "l": ["a", 1],
        "d": {"k": "v"},
        "date": _dt.date(2026, 8, 25),
        1: "int key",
        _dt.date(2026, 8, 25): "date key",
    }
    assert indexer._sanitize_frontmatter(fm) == {
        "s": "text",
        "i": 42,
        "f": 1.5,
        "t": True,
        "n": None,
        "l": ["a", 1],
        "d": {"k": "v"},
        "date": "2026-08-25",
        "1": "int key",
        "2026-08-25": "date key",
    }


# ══════════════════════════════════════════════════════════════════════════
# 4. one title rule (design D10b's table)
# ══════════════════════════════════════════════════════════════════════════
#
# The canonical rule is the indexer's, adopted by `read_note` and the panel.
# Both surfaces below go through `vault.note_title`: the indexer through
# `indexer._note_title`, and `read_note` / the panel through
# `vault.read_file()["title"]`.


TITLE_CASES = [
    ("a plain string", "Plain string", "Plain string"),
    ("a top-level date", _dt.date(2026, 8, 25), "2026-08-25"),
    ("a date inside a list", [_dt.date(2026, 8, 25)], "['2026-08-25']"),
    ("a non-string mapping key", {1: "a"}, "{'1': 'a'}"),
    ("a list of strings", ["a", "b"], "['a', 'b']"),
    ("a numeric title", 5, "5"),
    ("a 600-character string", "t" * 600, "t" * 512),
    ("nan", NAN, ".nan"),
    ("inf", INF, ".inf"),
    ("-inf", NEG_INF, "-.inf"),
]


@pytest.mark.parametrize(
    "label,value,expected", TITLE_CASES, ids=[c[0] for c in TITLE_CASES]
)
def test_the_indexer_title_rule(label, value, expected):
    assert indexer._note_title({"title": value}, "note.md") == expected


@pytest.mark.parametrize("label,value,expected", [c for c in TITLE_CASES], ids=[c[0] for c in TITLE_CASES])
def test_the_read_path_agrees_with_the_indexer(label, value, expected, vault, monkeypatch):
    """`read_note`'s title and the panel's are one call — `vault.read_file`."""
    import yaml

    block = yaml.safe_dump({"title": value}, sort_keys=False, default_flow_style=False)
    write(vault, "note.md", f"---\n{block}---\nbody\n")
    data = vault_service.read_file("note.md")
    assert data["title"] == expected
    assert data["title"] == indexer._note_title(data["frontmatter"], "note.md")


@pytest.mark.parametrize("falsy", [0, False, "", [], {}])
def test_every_falsy_title_falls_back_to_the_stem(falsy):
    assert indexer._note_title({"title": falsy}, "Daily Plan.md") == "Daily Plan"
    assert vault_service.note_title({"title": falsy}, "Daily Plan.md") == "Daily Plan"


def test_a_missing_title_falls_back_to_the_stem():
    assert indexer._note_title({}, "Daily Plan.md") == "Daily Plan"
    assert vault_service.note_title({}, "Daily Plan.md") == "Daily Plan"


@pytest.mark.asyncio
async def test_read_note_reports_the_token_as_the_title(vault):
    write(vault, "n.md", "---\ntitle: .nan\n---\nbody\n")
    out = await tools.read_note_impl("n.md")
    assert out.title == ".nan"
    assert out.title == indexer._note_title({"title": NAN}, "n.md")


# ══════════════════════════════════════════════════════════════════════════
# 5. nothing reaches the note's bytes
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_set_frontmatter_leaves_a_non_finite_value_byte_identical(vault):
    """The reason the coercion is at the boundaries and not at the parse.

    `set_frontmatter` re-serialises the *parsed mapping*, so a coerced
    `".nan"` string in that mapping would rewrite the note's own bytes as a
    side effect of setting an unrelated key — the destructive-write class.
    """
    original = "---\nx: .nan\na: .inf\nb: -.inf\n---\nbody\n"
    write(vault, "n.md", original)

    out = await tools.set_frontmatter_impl("n.md", updates={"status": "done"})
    assert "Error" not in out, out

    after = (vault / "n.md").read_bytes().decode("utf-8")
    assert "x: .nan" in after
    assert "a: .inf" in after
    assert "b: -.inf" in after
    assert "'.nan'" not in after and '".nan"' not in after
    assert "status: done" in after


@pytest.mark.asyncio
async def test_the_parsed_mapping_still_holds_the_float(vault):
    """The coercion is a property of the *view*, never of the mapping."""
    write(vault, "n.md", "---\nx: .nan\n---\nbody\n")
    data = vault_service.read_file("n.md")
    assert isinstance(data["frontmatter"]["x"], float)
    assert math.isnan(data["frontmatter"]["x"])
