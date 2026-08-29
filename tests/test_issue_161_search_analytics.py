"""The search-analytics page's contract, offline (#161).

What a real database has to answer — do the aggregates come out right — is
`tests/integration/test_issue_161_search_analytics_pg.py`. What is pinned here
is the shape of the queries and the page, because these are the properties that
break silently:

* **Every grouping and every coverage join keys on `(user_id, path)`, matched
  `IS NOT DISTINCT FROM`.** Dropping the owner would merge two tenants'
  analytics into one row — and a coverage list *is* a list of note paths, so
  that is one tenant reading another's vault. Using `=` would silently drop
  every ownerless row, which in single-user mode is all of them.
* **The refusal predicate is imported, never re-typed.** A second copy agrees
  with the first until somebody adds a marker to one of them.
* **The casts are guarded.** An unguarded `::bigint` or `jsonb_array_elements`
  on a bad row is a 500 for the whole window, for every user, until it ages out.
* **The logging-cap caveat renders beside *both* coverage sections.** A caveat
  on one of them makes the other look exact by contrast.
"""

import os
import re
import tempfile

import pytest

os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
os.chdir(tempfile.gettempdir())

from src.services import search_analytics as sa  # noqa: E402
from src.services.usage_stats import (  # noqa: E402
    WINDOWS,
    pre_body_refusal_sql,
)


class _Result:
    def fetchall(self):
        return []


class _Capturing:
    """Records the SQL and bind parameters each aggregate would issue."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def execute(self, clause, params=None):
        self.calls.append((str(clause), params or {}))
        return _Result()

    @property
    def sql(self) -> str:
        return self.calls[-1][0]

    @property
    def params(self) -> dict:
        return self.calls[-1][1]


async def _issue_every_query(user_id):
    """Run each public aggregate against a capturing session."""
    session = _Capturing()
    for tool in (*sa.QUERY_TOOLS, sa.RELATED_TOOL):
        await sa.top_queries(session, "24h", user_id, tool)
        await sa.zero_result_queries(session, "24h", user_id, tool)
    await sa.top_logged_retrievals(session, "24h", user_id)
    await sa.never_retrieved(session, "24h", user_id)
    return session


# --------------------------------------------------------------------------- #
# 1. The predicate is imported, and broadened deliberately.
# --------------------------------------------------------------------------- #
def test_the_refusal_predicate_is_the_shared_one():
    """One helper, two consumers, no drift — the #160 rule, kept."""
    assert pre_body_refusal_sql() in sa.analysable_sql()


def test_an_error_marked_row_is_not_a_result_set():
    """Broader than `/admin/performance`'s predicate, on purpose. There, a
    post-body refusal is still an expensive call and belongs in a percentile.
    Here the question is whether the call produced results an operator can
    reason about, and an error-marked row did not."""
    fragment = sa.analysable_sql()
    assert "->>'error' IS NULL" in fragment
    # Never NULL: `params` is nullable, so the missing-key case must read as
    # "no error", not as NULL propagating through the AND.
    assert "COALESCE" in fragment


def test_find_relateds_operational_markers_are_excluded_by_that_filter():
    """The scenario the change turns on: a missing or not-yet-embedded source
    must not reach the zero-result view. Both are `params.error` values, so the
    broad filter is what excludes them — asserted here so a future narrowing of
    that filter to an enumeration fails loudly."""
    from src.mcp_server import tools

    for marker in (
        tools._RELATED_SOURCE_NOT_FOUND_MARKER,
        tools._RELATED_SOURCE_NOT_EMBEDDED_MARKER,
    ):
        # The filter is value-blind: any error marker excludes the row.
        assert marker not in sa.analysable_sql()
    assert "->>'error' IS NULL" in sa.analysable_sql()


# --------------------------------------------------------------------------- #
# 2. Identity: (user_id, path), NULL-safe.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
@pytest.mark.parametrize("user_id", [None, 7])
async def test_every_grouping_carries_the_owner(user_id):
    session = await _issue_every_query(user_id)
    for sql, _ in session.calls:
        if "GROUP BY" in sql:
            group_by = re.search(r"GROUP BY ([^\n]+)", sql).group(1)
            assert "user_id" in group_by, (
                f"a grouping without the owner merges tenants: {group_by}"
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("user_id", [None, 7])
async def test_every_path_join_is_null_safe(user_id):
    """`=` would drop the ownerless slice — which is every row in single-user
    mode, and every row of a deleted user (`usage_logs.user_id` is
    ON DELETE SET NULL)."""
    session = await _issue_every_query(user_id)
    joins = [
        line.strip()
        for sql, _ in session.calls
        for line in sql.splitlines()
        if "user_id" in line and ("nm." in line or "l." in line) and "AND" in line
    ]
    assert joins, "the coverage queries must join on the owner at all"
    for line in joins:
        assert "IS NOT DISTINCT FROM" in line or ":uid" in line, line


@pytest.mark.asyncio
async def test_a_regular_user_is_scoped_on_both_tables():
    """The log rows *and* the notes: a coverage list built from a scoped log
    against every user's notes would report another tenant's notes as
    never-retrieved, listing their paths."""
    session = _Capturing()
    await sa.never_retrieved(session, "24h", 7)
    sql, params = session.calls[-1]

    assert sql.count("= :uid") == 2, "both usage_logs and notes_metadata scope"
    assert "ul.user_id = :uid" in sql and "nm.user_id = :uid" in sql
    assert params["uid"] == 7


@pytest.mark.asyncio
async def test_an_admin_scopes_neither_and_still_groups_per_owner():
    session = _Capturing()
    await sa.never_retrieved(session, "24h", None)
    sql, params = session.calls[-1]

    assert ":uid" not in sql
    assert "uid" not in params
    assert "IS NOT DISTINCT FROM" in sql, (
        "an admin sees every owner's rows, so the join is the only thing "
        "keeping two owners' identical paths apart"
    )


# --------------------------------------------------------------------------- #
# 3. Guards, binds, and windows.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_the_result_count_cast_is_guarded():
    session = _Capturing()
    await sa.top_queries(session, "24h", None, "keyword_search")
    sql = session.sql
    assert "~ '^-?[0-9]{1,9}$'" in sql, "an unguarded cast is a 500 per bad row"
    assert "::bigint" in sql


@pytest.mark.asyncio
async def test_the_unnest_is_guarded_by_jsonb_typeof():
    """`jsonb_array_elements_text` raises on a non-array value."""
    session = _Capturing()
    await sa.top_logged_retrievals(session, "24h", None)
    assert "jsonb_typeof(ul.params->'result_paths') = 'array'" in session.sql


@pytest.mark.asyncio
async def test_values_travel_as_binds_not_literals():
    session = _Capturing()
    await sa.top_queries(session, "7d", 3, "semantic_search")
    sql, params = session.calls[-1]

    assert params["tool"] == "semantic_search"
    assert "'semantic_search'" not in sql
    assert params["window_seconds"] == WINDOWS["7d"]
    assert ":window_seconds" in sql
    # The refusal markers ride along as binds too.
    assert any(k.startswith("omcp_pre_body_refusal_") for k in params)


def test_an_unknown_tool_has_no_grouping_key():
    """The group key is interpolated into SQL, so it is resolved from the map
    and never from a caller."""
    with pytest.raises(ValueError):
        sa._group_key("read_note")
    assert sa.GROUP_KEYS["find_related"] == "source_path", (
        "grouping find_related by its named `path` param would collapse "
        "distinct long paths — that param is truncated at 200 characters"
    )
    assert set(sa.GROUP_KEYS) == {*sa.QUERY_TOOLS, sa.RELATED_TOOL}


# --------------------------------------------------------------------------- #
# 4. The page.
# --------------------------------------------------------------------------- #
def _render(**overrides):
    from jinja2 import (
        ChainableUndefined,
        ChoiceLoader,
        DictLoader,
        Environment,
        FileSystemLoader,
    )

    here = os.path.dirname(os.path.abspath(__file__))
    templates = os.path.join(here, "..", "src", "control_panel", "templates")
    env = Environment(
        loader=ChoiceLoader([
            DictLoader({
                "base.html":
                    "{% block title %}{% endblock %}{% block content %}{% endblock %}"
            }),
            FileSystemLoader(templates),
        ]),
        undefined=ChainableUndefined,
        autoescape=True,
    )

    def group(value, digest=False, **kw):
        return dict({
            "user_id": None, "owner": None, "value": value, "calls": 3,
            "zero_calls": 1, "mean_results": 2.5, "measured": 3,
            "label": {"text": value, "is_digest": digest},
        }, **kw)

    ctx = dict(
        active="search-analytics", window="24h", window_label="Last 24 hours",
        windows=[{"key": "24h", "label": "24h", "selected": True}],
        query_tables=[
            {"tool": "keyword_search", "label": "Keyword search",
             "top": [group("alpha")], "zero": [group("alpha")]},
            {"tool": "semantic_search", "label": "Semantic search",
             "top": [], "zero": []},
        ],
        related={"tool": "find_related", "label": "Related notes",
                 "top": [group("d" * 64, digest=True)], "zero": []},
        retrievals=[{"user_id": None, "owner": None, "path": "A.md",
                     "appearances": 5, "indexed": True}],
        never_retrieved=[{"user_id": None, "owner": None, "path": "C.md",
                          "title": "C", "modified_at": None}],
        never_retrieved_total=1204, table_limit=20, coverage_limit=50,
        logged_per_call=sa.LOGGED_RESULTS_PER_CALL,
        show_owner=False, has_data=True,
    )
    ctx.update(overrides)
    return env.get_template("search_analytics.html").render(**ctx)


def test_the_cap_caveat_renders_beside_both_coverage_sections():
    html = _render()
    caveat = f"Only the first {sa.LOGGED_RESULTS_PER_CALL} results of each call"
    assert html.count(caveat) == 2, (
        "the caveat has to sit beside the ranking *and* the never-retrieved "
        "list; on one of them it makes the other look exact"
    )


def test_the_ranking_is_labelled_as_logged_appearances():
    html = _render()
    assert "Top-logged retrievals" in html
    assert "Logged appearances" in html
    assert "upper bound" in html, "never-retrieved must not read as exact"


def test_a_digest_source_is_labelled_as_one():
    """64 unmarked hex characters in a column of note paths reads as a note."""
    html = _render()
    assert "digest dddddddddddddddd" in html
    assert "d" * 64 not in html


def test_the_never_retrieved_cap_never_reads_as_the_total():
    assert "showing 1 of 1204" in _render()


def test_an_unmeasured_group_renders_an_em_dash_not_a_zero():
    """A group whose calls all predate the telemetry has no mean. Rendering 0
    would say "this query finds nothing", which is a different claim."""
    html = _render(query_tables=[{
        "tool": "keyword_search", "label": "Keyword search",
        "top": [{"user_id": None, "owner": None, "value": "q", "calls": 2,
                 "zero_calls": 0, "mean_results": None, "measured": 0,
                 "label": {"text": "q", "is_digest": False}}],
        "zero": [],
    }])
    assert "No call in this group recorded a result count" in html


def test_the_owner_column_appears_only_when_owners_differ():
    assert "bob" not in _render()
    html = _render(show_owner=True, retrievals=[{
        "user_id": 2, "owner": "bob", "path": "A.md",
        "appearances": 5, "indexed": True,
    }])
    assert "bob" in html


def test_the_page_is_reachable_and_in_the_nav():
    from src.control_panel.routes import router

    paths = {r.path for r in router.routes}
    assert "/admin/search-analytics" in paths

    here = os.path.dirname(os.path.abspath(__file__))
    base = os.path.join(here, "..", "src", "control_panel", "templates", "base.html")
    with open(base) as fh:
        nav = fh.read()
    assert "/admin/search-analytics" in nav
    assert "active == 'search-analytics'" in nav


def test_the_digest_label_recognises_a_digest_and_nothing_else():
    from src.control_panel.routes import _label_group_value

    assert _label_group_value("find_related", "a" * 64)["is_digest"] is True
    assert _label_group_value("find_related", "Projects/A.md")["is_digest"] is False
    # A query is never a digest, whatever it looks like.
    assert _label_group_value("keyword_search", "a" * 64)["is_digest"] is False
