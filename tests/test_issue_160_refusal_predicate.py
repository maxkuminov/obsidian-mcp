"""The one shared executed/refused predicate, and the performance page's copy.

Hermetic. What is pinned here is the *enumeration* — that the predicate names
exactly the pre-body markers `_tracked` writes, that its mirror of those two
string constants has not drifted from `src/mcp_server/tools.py`, and that it is
not a broad "params carries an error" match. That it actually partitions rows
that way on PostgreSQL is
`tests/integration/test_issue_160_performance_pg.py`.
"""
import re
from pathlib import Path

from src.services import usage_stats

_TEMPLATES = Path(__file__).resolve().parent.parent / "src" / "control_panel" / "templates"


# --------------------------------------------------------------------------
# 1. The mirror cannot drift from `_tracked`.
# --------------------------------------------------------------------------


def test_the_marker_values_match_the_writer():
    """`usage_stats` mirrors these two constants rather than importing them,
    because #162's quota gate will import `usage_stats` from `tools.py` and an
    import the other way closes the cycle. A mirror needs a pin."""
    from src.mcp_server import tools

    assert usage_stats.NO_VAULT_MARKER == tools._NO_VAULT_MARKER
    assert usage_stats.UNENCODABLE_ARG_MARKER == tools._UNENCODABLE_ARG_MARKER


def test_exactly_the_pre_body_markers_are_enumerated():
    assert usage_stats.PRE_BODY_REFUSAL_ERROR_MARKERS == (
        "no_vault_assigned",
        "argument_not_encodable",
    )
    assert usage_stats.OVER_QUOTA_PARAM == "over_quota"


def test_the_post_body_markers_are_not_in_the_predicate():
    """`vault_assignment_changed` and `vault_confirmation_unavailable` are
    written by tools whose bodies *ran* — they resolved a vault, did the work,
    and then refused to publish. Excluding them would hide the slowest write
    path in the server from the one view built to find slow paths."""
    from src.mcp_server import tools

    fragment = usage_stats.pre_body_refusal_sql()
    binds = usage_stats.PRE_BODY_REFUSAL_BINDS
    for marker in (
        tools._VAULT_REASSIGNED_MARKER,
        tools._CONFIRMATION_UNAVAILABLE_MARKER,
    ):
        assert marker not in fragment
        assert marker not in binds.values()


def test_the_predicate_is_not_a_broad_error_match():
    """A `params ? 'error'` / `error IS NOT NULL` predicate is wrong in a way
    that is invisible on the page: it would drop every row whose body ran and
    then failed out of the latency aggregates."""
    fragment = usage_stats.pre_body_refusal_sql()
    assert "IS NOT NULL" not in fragment
    assert "?" not in fragment
    # `error` may appear only as the key of an equality against the enumerated
    # bind parameters.
    assert re.search(r"->>'error' IN \(:[^)]+\)", fragment)


# --------------------------------------------------------------------------
# 2. Shape: NULL-safe, negatable, parameterised.
# --------------------------------------------------------------------------


def test_the_predicate_is_null_safe_on_both_halves():
    """`params` is nullable and `->>` on a missing key yields NULL. Without the
    COALESCEs the negation used by the executed filter would be NULL and
    PostgreSQL would drop every ordinary row on the floor."""
    fragment = usage_stats.pre_body_refusal_sql()
    assert "COALESCE((ul.params->>'over_quota')::boolean, false)" in fragment
    assert fragment.count("COALESCE(") == 2


def test_executed_is_exactly_the_negation():
    assert usage_stats.executed_sql() == f"(NOT {usage_stats.pre_body_refusal_sql()})"


def test_the_markers_travel_as_bind_parameters():
    fragment = usage_stats.pre_body_refusal_sql()
    for name, value in usage_stats.PRE_BODY_REFUSAL_BINDS.items():
        assert f":{name}" in fragment
        assert value not in fragment, "a marker value must not be interpolated"
    assert set(usage_stats.PRE_BODY_REFUSAL_BINDS.values()) == set(
        usage_stats.PRE_BODY_REFUSAL_ERROR_MARKERS
    )


def test_the_alias_is_honoured():
    assert "logs.params" in usage_stats.pre_body_refusal_sql(alias="logs")


# --------------------------------------------------------------------------
# 3. One helper, both consumers.
# --------------------------------------------------------------------------


def test_the_aggregate_and_the_refusal_count_use_the_same_expression():
    """Two hand-written predicates agree until somebody adds a third marker to
    one of them. The count and the percentiles must partition the window."""
    import inspect

    source = inspect.getsource(usage_stats.tool_aggregates)
    assert "executed = executed_sql()" in source
    assert "refused = pre_body_refusal_sql()" in source
    # Every filtered aggregate is expressed through those two names, never by
    # re-writing the predicate inline.
    assert "no_vault_assigned" not in source
    assert "over_quota" not in source


# --------------------------------------------------------------------------
# 4. Windows.
# --------------------------------------------------------------------------


def test_the_three_windows_are_offered_and_capped_at_thirty_days():
    assert list(usage_stats.WINDOWS) == ["24h", "7d", "30d"]
    assert usage_stats.WINDOWS["30d"] == 30 * 24 * 60 * 60


def test_an_unknown_window_falls_back_rather_than_erroring():
    """The selector is a set of links; a hand-edited query string should render
    the page, not a 422."""
    assert usage_stats.normalize_window(None) == "24h"
    assert usage_stats.normalize_window("90d") == "24h"
    assert usage_stats.normalize_window("' OR 1=1--") == "24h"
    assert usage_stats.normalize_window("7d") == "7d"


# --------------------------------------------------------------------------
# 5. The page's empty states, and its theming.
# --------------------------------------------------------------------------


def _render(template: str, **context) -> str:
    from jinja2 import ChainableUndefined, ChoiceLoader, DictLoader, Environment, FileSystemLoader

    env = Environment(
        loader=ChoiceLoader([
            DictLoader({
                "base.html": "{% block title %}{% endblock %}{% block content %}{% endblock %}"
            }),
            FileSystemLoader(str(_TEMPLATES)),
        ]),
        undefined=ChainableUndefined,
        autoescape=True,
    )
    return env.get_template(template).render(**context)


def _empty_context():
    window = usage_stats.DEFAULT_WINDOW
    return {
        "active": "performance",
        "window": window,
        "window_label": usage_stats.WINDOW_LABELS[window],
        "windows": [
            {"key": key, "label": key, "selected": key == window}
            for key in usage_stats.WINDOWS
        ],
        "tools": [],
        "phases": [
            {"phase": "embed_ms", "label": "Embedding", "count": 0, "mean": None, "p95": None},
            {"phase": "db_ms", "label": "Database", "count": 0, "mean": None, "p95": None},
        ],
        "slowest": [],
        "has_data": False,
    }


def test_an_empty_window_renders_an_explicit_empty_state():
    rendered = _render("performance.html", **_empty_context())
    assert "No MCP requests were logged in this window" in rendered
    assert "No calls logged in this window." in rendered
    assert "No executed requests in this window." in rendered
    assert "no calls recorded this phase in this window" in rendered


def test_a_tool_with_no_executed_call_shows_a_dash_not_a_zero():
    """A NULL percentile means the filtered set was empty — every row for that
    tool in the window was a refusal. An em dash says "not measured"; a 0 would
    be a measurement, and a false one."""
    ctx = _empty_context()
    ctx["has_data"] = True
    ctx["tools"] = [{
        "tool": "semantic_search", "executed": 0, "refusals": 5000,
        "p50": None, "p95": None, "p99": None,
        "mean_size": None, "max_size": None,
    }]
    rendered = _render("performance.html", **ctx)
    assert "5000" in rendered
    assert "—" in rendered
    assert "0 ms" not in rendered


def test_the_page_extends_the_shared_base_and_defines_no_palette():
    source = (_TEMPLATES / "performance.html").read_text()
    assert source.lstrip().startswith('{% extends "base.html" %}')
    # A page template may add a scoped token; it may not define a palette.
    # Nothing here defines a custom property at all — every color is a `var()`
    # reference to `_theme.html`.
    stripped = re.sub(r"var\(--[a-z0-9-]+\)", "", source)
    assert not re.search(r"--[a-z0-9-]+\s*:", stripped), (
        "performance.html defines a custom property; the palette lives in "
        "_theme.html and nowhere else"
    )
