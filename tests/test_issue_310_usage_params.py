"""#310 — `usage_logs.params` rendering (design D1–D4).

Unit tests for `render_usage_params`, the reserved marker name, and the
transfer-row pin (D3). The write-outcome and coalescer tests live in
`test_issue_310_usage_write_outcome.py`; the real-Postgres proof in
`tests/integration/test_issue_310_usage_params_pg.py`.
"""

from __future__ import annotations

import inspect
import json

import pytest

from src.services import usage_params
from src.services.usage_params import (
    CYCLE,
    RENDERED_PARAMS_KEY,
    UNRENDERABLE,
    render_usage_params,
)

NAN = float("nan")
INF = float("inf")


def _storable(doc) -> bool:
    """What PostgreSQL would accept: strict JSON, no NUL, no lone surrogate."""
    text = json.dumps(doc, allow_nan=False, ensure_ascii=False)
    text.encode("utf-8")  # raises on a lone surrogate
    return "\\u0000" not in json.dumps(doc, allow_nan=False)


# ── D1 table ──────────────────────────────────────────────────────────────


def test_nul_is_rendered_as_backslash_x00():
    out = render_usage_params({"query": "a\x00b"})
    assert out["query"] == "a\\x00b"
    assert out[RENDERED_PARAMS_KEY] == ["query"]
    assert _storable(out)


def test_lone_surrogate_is_rendered_like_backslashreplace():
    out = render_usage_params({"path": "x\ud800y\udfff"})
    assert out["path"] == "x\\ud800y\\udfff"
    assert out["path"] == "x\ud800y\udfff".encode("utf-8", "backslashreplace").decode()
    assert _storable(out)


@pytest.mark.parametrize(
    "value, token", [(NAN, ".nan"), (INF, ".inf"), (-INF, "-.inf")]
)
def test_non_finite_floats_take_the_154_tokens(value, token):
    out = render_usage_params({"frontmatter": {"x": value}})
    assert out["frontmatter"] == {"x": token}
    assert out[RENDERED_PARAMS_KEY] == ["frontmatter"]
    assert _storable(out)


def test_top_level_non_finite_float_is_rendered():
    out = render_usage_params({"x": NAN})
    assert out == {"x": ".nan", RENDERED_PARAMS_KEY: ["x"]}


def test_backslash_is_doubled_only_inside_a_rendered_value():
    out = render_usage_params({"a": "C:\\dir\x00", "b": "C:\\dir"})
    assert out["a"] == "C:\\\\dir\\x00"
    assert out["b"] == "C:\\dir"  # not rendered: stored byte for byte
    assert out[RENDERED_PARAMS_KEY] == ["a"]


def test_a_rendered_nul_and_a_literal_escape_are_distinguishable():
    out = render_usage_params({"find": ["\x00", "\\x00"]})
    assert out["find"] == ["\\x00", "\\\\x00"]
    assert out["find"][0] != out["find"][1]


def test_every_string_in_a_rendered_value_follows_the_grammar():
    out = render_usage_params({"frontmatter": {"k\\1": "v\\2", "bad": "\x00"}})
    assert out["frontmatter"] == {"k\\\\1": "v\\\\2", "bad": "\\x00"}


def test_nested_key_and_value_positions():
    out = render_usage_params(
        {"frontmatter": {"k\x00": [NAN, {"deep\ud800": "v\x00"}, INF, -INF]}}
    )
    assert out["frontmatter"] == {
        "k\\x00": [".nan", {"deep\\ud800": "v\\x00"}, ".inf", "-.inf"]
    }
    assert out[RENDERED_PARAMS_KEY] == ["frontmatter"]
    assert _storable(out)


def test_non_json_values_are_listed_or_stringified():
    class Thing:
        def __str__(self):
            return "thing\x00"

    out = render_usage_params({"t": (1, "a"), "s": frozenset({3}), "o": Thing()})
    assert out["t"] == [1, "a"]
    assert out["s"] == [3]
    assert out["o"] == "thing\\x00"
    assert out[RENDERED_PARAMS_KEY] == ["o", "s", "t"]


def test_non_string_keys_are_stringified_and_first_key_wins():
    out = render_usage_params({"z": {NAN: 1, ".nan": 2, 3: "x"}})
    assert out["z"] == {".nan": 1, "3": "x"}


def test_bool_int_none_and_finite_float_are_unchanged_in_a_rendered_value():
    out = render_usage_params({"v": [True, 0, None, 1.5, "\x00"]})
    assert out["v"] == [True, 0, None, 1.5, "\\x00"]
    assert out["v"][0] is True


# ── clean input, markers, ordering ────────────────────────────────────────


def test_clean_params_are_unchanged_and_carry_no_marker():
    params = {
        "query": "hello \\ world",
        "limit": 5,
        "tags": ["a", "b"],
        "frontmatter": {"k": 1.25},
        "error": "rate_limited",
        "suppressed": 0,
    }
    out = render_usage_params(params)
    assert out == params
    assert RENDERED_PARAMS_KEY not in out
    assert out is not params  # a copy; the coalescer template stays raw


def test_rendered_params_is_sorted_and_names_only_affected_keys():
    out = render_usage_params(
        {"z": "\x00", "a": "\ud800", "m": "fine", "error": "argument_not_encodable"}
    )
    assert out[RENDERED_PARAMS_KEY] == ["a", "z"]
    assert out["m"] == "fine" and out["error"] == "argument_not_encodable"


def test_an_input_marker_is_recomputed_not_trusted():
    out = render_usage_params({RENDERED_PARAMS_KEY: ["forged"], "a": "ok"})
    assert out == {"a": "ok"}


def test_server_markers_survive_beside_a_rendered_argument():
    out = render_usage_params(
        {"path": "\x00", "error": "rate_limited", "suppressed": 3,
         "concurrency": {"v": 2, "mode": "shadow", "epoch": "abc"}}
    )
    assert out["error"] == "rate_limited"
    assert out["suppressed"] == 3
    assert out["concurrency"] == {"v": 2, "mode": "shadow", "epoch": "abc"}


def test_top_level_key_with_an_unstorable_character_is_escaped():
    out = render_usage_params({"k\x00": "v"})
    assert out == {"k\\x00": "v", RENDERED_PARAMS_KEY: ["k\\x00"]}


# ── termination and totality ─────────────────────────────────────────────


def test_ten_thousand_deep_nesting_does_not_recurse():
    deep: list = []
    cur = deep
    for _ in range(10_000):
        nxt: list = []
        cur.append(nxt)
        cur = nxt
    cur.append("\x00")
    out = render_usage_params({"deep": deep})
    assert out[RENDERED_PARAMS_KEY] == ["deep"]
    node = out["deep"]
    for _ in range(10_000):
        node = node[0]
    assert node == ["\\x00"]


def test_clean_deep_nesting_is_not_rendered():
    deep: list = []
    cur = deep
    for _ in range(10_000):
        nxt: list = []
        cur.append(nxt)
        cur = nxt
    out = render_usage_params({"deep": deep})
    assert RENDERED_PARAMS_KEY not in out


def test_self_referential_dict_renders_cycle():
    d: dict = {"a": 1}
    d["self"] = d
    out = render_usage_params({"x": d})
    assert out["x"] == {"a": 1, "self": CYCLE}
    assert out[RENDERED_PARAMS_KEY] == ["x"]
    assert _storable(out)


def test_shared_reference_is_not_a_cycle():
    shared = ["\x00"]
    out = render_usage_params({"x": [shared, shared]})
    assert out["x"] == [["\\x00"], ["\\x00"]]


def test_hostile_str_becomes_unrenderable_and_does_not_raise():
    class Hostile:
        def __str__(self):
            raise RuntimeError("no")

    out = render_usage_params({"h": Hostile(), "error": "tool_exception"})
    assert out == {
        "h": UNRENDERABLE,
        "error": "tool_exception",
        RENDERED_PARAMS_KEY: ["h"],
    }


def test_non_dict_input_is_total():
    assert render_usage_params(["\x00"]) == ["\\x00"]
    assert render_usage_params("plain") == "plain"


def test_d4_length_bound_after_truncation():
    from src.mcp_server.tools import _MAX_PARAM_LEN, _truncate_params

    truncated = _truncate_params({"q": "\ud800" * 5000})
    out = render_usage_params(truncated)
    assert len(out["q"]) <= 6 * _MAX_PARAM_LEN + 1


# ── 1.4 the reserved name ────────────────────────────────────────────────


def test_rendered_params_is_not_a_tool_parameter_or_marker():
    from src.mcp_server import server, tools
    from src.services import refusals, timing

    for name, fn in vars(server).items():
        if callable(fn) and inspect.iscoroutinefunction(fn):
            assert RENDERED_PARAMS_KEY not in inspect.signature(fn).parameters, name
    for name, fn in vars(tools).items():
        if name.endswith("_impl") and callable(fn):
            target = inspect.unwrap(fn)
            assert RENDERED_PARAMS_KEY not in inspect.signature(target).parameters, name
    for module in (tools, refusals, rate_limits_module(), timing):
        for name, value in vars(module).items():
            if isinstance(value, str) and (name.isupper() or name.endswith("_MARKER")):
                assert value != RENDERED_PARAMS_KEY, (module.__name__, name)
    timing_source = inspect.getsource(timing)
    assert f'"{RENDERED_PARAMS_KEY}"' not in timing_source


def rate_limits_module():
    from src.services import rate_limits

    return rate_limits


# ── 1.5 transfer pin (D3) ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "params",
    [
        {"path": "Projects/a file.pdf", "size": 12345},
        {"path": "notes/ünïcode/日本.md", "size": 0},
    ],
)
def test_transfer_row_params_are_unchanged(params):
    """`_log_row`'s params (src/transfer/routes.py) are a path read back from a
    PostgreSQL `text` column plus an integer: nothing the renderer changes. A
    field added there that could carry an unstorable value should trip this
    test's sibling below."""
    assert render_usage_params(params) == params


def test_transfer_log_row_params_shape_is_path_and_size():
    from src.transfer import routes

    source = inspect.getsource(routes)
    assert source.count("_log_row(") == 3  # definition + upload + download
    assert '{"path": locked.token.path, "size": result["size"]}' in source
    assert '{"path": row.path, "size": st.st_size}' in source


def test_module_exports():
    assert usage_params.RENDERED_PARAMS_KEY == "rendered_params"
