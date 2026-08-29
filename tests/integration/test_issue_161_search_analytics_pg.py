"""Real-Postgres gate for the search analytics aggregates (#161).

Everything asserted here *is* database behaviour and has no meaningful
fake-session equivalent:

* **Tenancy.** Two users with a note at the same path is the case the identity
  rule exists for. Whether `(user_id, path)` with `IS NOT DISTINCT FROM` keeps
  their analytics apart — and keeps the ownerless slice visible instead of
  dropping it, which `=` would do silently — is a fact about NULL semantics,
  not about a string in the SQL.
* **The exclusions.** Whether a pre-body refusal, and whether a row carrying
  `find_related`'s operational markers, stay out of the zero-result view is a
  fact about JSONB operators. Getting it wrong is invisible: the page just
  reports the operator's typos as gaps in the vault's memory.
* **The guards.** A row with `result_count: "lots"` or `result_paths: "a.md"`
  must cost one row, not the whole window's page.
* **`jsonb_array_elements_text` over a window**, which is the coverage lists.

Skipped unless `PGVECTOR_TEST_ADMIN_URL` names a throwaway Postgres *server*
(the harness creates and drops its own database):

    docker run --rm -d --name omcp-161 -e POSTGRES_PASSWORD=test \\
        -p 55495:5432 pgvector/pgvector:pg16
    PGVECTOR_TEST_ADMIN_URL=postgresql+asyncpg://postgres:test@localhost:55495/postgres \\
        pytest -q tests/integration/test_issue_161_search_analytics_pg.py
    docker rm -f omcp-161
"""
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import _harness
from src.models.db import NoteMetadata, UsageLog, User
from src.services.search_analytics import (
    LOGGED_RESULTS_PER_CALL,
    QUERY_TOOLS,
    RELATED_TOOL,
    never_retrieved,
    top_logged_retrievals,
    top_queries,
    zero_result_queries,
)

DIM = 64

pytestmark = [
    pytest.mark.asyncio(loop_scope="module"),
    _harness.requires_pgvector,
]


@pytest.fixture(scope="module")
def migrated_url():
    yield from _harness.throwaway_database("search_161", DIM)


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def sessionmaker(migrated_url):
    engine = create_async_engine(migrated_url, poolclass=None)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest_asyncio.fixture(loop_scope="module")
async def clean(sessionmaker):
    async def _wipe():
        async with sessionmaker() as session:
            await session.execute(delete(UsageLog))
            await session.execute(delete(NoteMetadata))
            await session.execute(delete(User))
            await session.commit()

    await _wipe()
    yield sessionmaker
    await _wipe()


def _search(tool, *, query=None, source=None, count=None, paths=None,
            error=None, over_quota=None, ago_minutes=1, user_id=None):
    """One `usage_logs` row shaped like the telemetry writes them."""
    params: dict = {}
    if query is not None:
        params["query"] = query
    if source is not None:
        params["source_path"] = source
    if count is not None:
        params["result_count"] = count
    if paths is not None:
        params["result_paths"] = paths
    if error is not None:
        params["error"] = error
    if over_quota is not None:
        params["over_quota"] = over_quota
    return UsageLog(
        tool=tool,
        params=params,
        duration_ms=10,
        response_size=100,
        user_id=user_id,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=ago_minutes),
    )


def _note(path, *, user_id=None, title=None, ago_days=1):
    return NoteMetadata(
        user_id=user_id,
        file_path=path,
        title=title or path,
        content_hash="x" * 8,
        modified_at=datetime.now(timezone.utc) - timedelta(days=ago_days),
    )


# --------------------------------------------------------------------------
# 1. Query tables.
# --------------------------------------------------------------------------


async def test_top_and_zero_result_queries_per_tool(clean):
    """The spec's scenario: top queries with call counts and mean result count,
    and the zero-result queries, listed per query tool."""
    async with clean() as session:
        session.add_all([
            _search("keyword_search", query="alpha", count=4,
                    paths=["a.md", "b.md", "c.md", "d.md"]),
            _search("keyword_search", query="alpha", count=2, paths=["a.md", "b.md"]),
            _search("keyword_search", query="alpha", count=0, paths=[]),
            _search("keyword_search", query="beta", count=1, paths=["b.md"]),
            # A different tool's identical query must not join this one's rows.
            _search("semantic_search", query="alpha", count=9, paths=["z.md"]),
            _search("semantic_search", query="gamma", count=0, paths=[]),
        ])
        await session.commit()

        keyword = await top_queries(session, "24h", None, "keyword_search")
        semantic = await top_queries(session, "24h", None, "semantic_search")
        keyword_zero = await zero_result_queries(
            session, "24h", None, "keyword_search"
        )

    alpha = next(r for r in keyword if r["value"] == "alpha")
    assert alpha["calls"] == 3
    assert alpha["mean_results"] == pytest.approx(2.0)  # (4 + 2 + 0) / 3
    assert alpha["zero_calls"] == 1
    assert [r["value"] for r in keyword] == ["alpha", "beta"], "most frequent first"

    assert {r["value"]: r["calls"] for r in semantic} == {"alpha": 1, "gamma": 1}
    assert [(r["value"], r["zero_calls"]) for r in keyword_zero] == [("alpha", 1)]


async def test_a_refused_call_is_not_a_zero_result(clean):
    """A pre-body refusal has no result set at all. Counting it as a search
    that found nothing would report a credential problem as a gap in the
    vault."""
    async with clean() as session:
        session.add_all([
            _search("keyword_search", query="alpha", count=0, paths=[]),
            _search("keyword_search", query="alpha", error="no_vault_assigned"),
            _search("keyword_search", query="alpha", error="argument_not_encodable"),
            _search("keyword_search", query="alpha", over_quota=True, count=0),
        ])
        await session.commit()

        rows = await top_queries(session, "24h", None, "keyword_search")
        zero = await zero_result_queries(session, "24h", None, "keyword_search")

    assert len(rows) == 1
    assert rows[0]["calls"] == 1, f"three refusals leaked in: {rows[0]}"
    assert zero[0]["zero_calls"] == 1


async def test_an_over_quota_false_row_is_an_ordinary_search(clean):
    """The #160 predicate reads the boolean, not the key's presence."""
    async with clean() as session:
        session.add(
            _search("keyword_search", query="alpha", count=3,
                    paths=["a.md"], over_quota=False)
        )
        await session.commit()
        rows = await top_queries(session, "24h", None, "keyword_search")

    assert rows[0]["calls"] == 1 and rows[0]["mean_results"] == 3.0


async def test_windows_bound_the_aggregation(clean):
    async with clean() as session:
        session.add_all([
            _search("keyword_search", query="alpha", count=1, paths=["a.md"],
                    ago_minutes=30),
            _search("keyword_search", query="alpha", count=1, paths=["a.md"],
                    ago_minutes=60 * 72),
            _search("keyword_search", query="alpha", count=1, paths=["a.md"],
                    ago_minutes=60 * 24 * 20),
        ])
        await session.commit()

        seen = {
            window: (await top_queries(session, window, None, "keyword_search"))
            for window in ("24h", "7d", "30d")
        }

    assert [seen[w][0]["calls"] for w in ("24h", "7d", "30d")] == [1, 2, 3]


async def test_an_empty_window_is_an_empty_result_not_an_error(clean):
    async with clean() as session:
        for tool in (*QUERY_TOOLS, RELATED_TOOL):
            assert await top_queries(session, "7d", None, tool) == []
            assert await zero_result_queries(session, "7d", None, tool) == []
        assert await top_logged_retrievals(session, "7d", None) == []
        assert await never_retrieved(session, "7d", None) == ([], 0)


async def test_a_row_predating_the_telemetry_counts_but_does_not_average(clean):
    """Rows written before #161 carry a query and no `result_count`. They are
    real calls and must appear; a missing count read as 0 would report a query
    that works as one that finds nothing."""
    async with clean() as session:
        session.add_all([
            _search("keyword_search", query="alpha"),
            _search("keyword_search", query="alpha", count=6, paths=["a.md"]),
            _search("keyword_search", query="legacy-only"),
        ])
        await session.commit()

        rows = await top_queries(session, "24h", None, "keyword_search")
        zero = await zero_result_queries(session, "24h", None, "keyword_search")

    by_query = {r["value"]: r for r in rows}
    assert by_query["alpha"]["calls"] == 2
    assert by_query["alpha"]["measured"] == 1
    assert by_query["alpha"]["mean_results"] == pytest.approx(6.0)
    assert by_query["legacy-only"]["mean_results"] is None
    assert zero == [], "an absent count is not a zero result"


async def test_a_non_numeric_result_count_costs_one_row_not_the_page(clean):
    """The guarded cast. Unguarded, this row is a 500 for the whole window —
    for every user — until it ages out."""
    async with clean() as session:
        session.add_all([
            _search("keyword_search", query="alpha", count="lots", paths=["a.md"]),
            _search("keyword_search", query="alpha", count=2, paths=["a.md"]),
        ])
        await session.commit()

        rows = await top_queries(session, "24h", None, "keyword_search")

    assert rows[0]["calls"] == 2
    assert rows[0]["measured"] == 1
    assert rows[0]["mean_results"] == pytest.approx(2.0)


# --------------------------------------------------------------------------
# 2. find_related, grouped by source.
# --------------------------------------------------------------------------


async def test_find_related_groups_by_source_path(clean):
    """The spec's scenario: five calls on one source, one of them empty."""
    async with clean() as session:
        session.add_all(
            [
                _search(RELATED_TOOL, source="Projects/Alpha.md", count=3,
                        paths=["a.md", "b.md", "c.md"])
                for _ in range(4)
            ] + [
                _search(RELATED_TOOL, source="Projects/Alpha.md", count=0, paths=[]),
            ]
        )
        await session.commit()

        rows = await top_queries(session, "24h", None, RELATED_TOOL)
        zero = await zero_result_queries(session, "24h", None, RELATED_TOOL)

    assert len(rows) == 1
    assert rows[0]["value"] == "Projects/Alpha.md"
    assert rows[0]["calls"] == 5
    assert rows[0]["zero_calls"] == 1
    assert [(r["value"], r["zero_calls"]) for r in zero] == [
        ("Projects/Alpha.md", 1)
    ]


async def test_find_related_operational_failures_are_not_zero_results(clean):
    """The distinction the change turns on, at the reading end.

    A source note that is missing or not yet embedded produced no result set;
    a source that exists, is embedded and has no neighbours did. Only the
    second is a fact about what the vault holds.
    """
    from src.mcp_server import tools

    async with clean() as session:
        session.add_all([
            _search(RELATED_TOOL, source="Gone.md", count=0,
                    error=tools._RELATED_SOURCE_NOT_FOUND_MARKER),
            _search(RELATED_TOOL, source="Fresh.md", count=0,
                    error=tools._RELATED_SOURCE_NOT_EMBEDDED_MARKER),
            _search(RELATED_TOOL, source="Lonely.md", count=0, paths=[]),
        ])
        await session.commit()

        rows = await top_queries(session, "24h", None, RELATED_TOOL)
        zero = await zero_result_queries(session, "24h", None, RELATED_TOOL)

    assert [r["value"] for r in rows] == ["Lonely.md"]
    assert [r["value"] for r in zero] == ["Lonely.md"]


async def test_two_over_long_sources_stay_apart_as_digests(clean):
    """Why the grouping key is `source_path` and not the named `path` param:
    that one is truncated at 200 characters, so two distinct long paths would
    collapse onto a single row and the page would attribute one note's calls to
    another."""
    from src.services.timing import source_path_value

    long_a = "d" * 1100 + "/a.md"
    long_b = "d" * 1100 + "/b.md"
    digest_a, digest_b = source_path_value(long_a), source_path_value(long_b)
    assert digest_a != digest_b and len(digest_a) == 64

    async with clean() as session:
        session.add_all([
            _search(RELATED_TOOL, source=digest_a, count=1, paths=["x.md"]),
            _search(RELATED_TOOL, source=digest_a, count=1, paths=["x.md"]),
            _search(RELATED_TOOL, source=digest_b, count=0, paths=[]),
        ])
        await session.commit()

        rows = await top_queries(session, "24h", None, RELATED_TOOL)

    assert {r["value"]: r["calls"] for r in rows} == {digest_a: 2, digest_b: 1}


# --------------------------------------------------------------------------
# 3. Coverage.
# --------------------------------------------------------------------------


async def test_top_logged_retrievals_counts_appearances(clean):
    async with clean() as session:
        session.add_all([
            _search("semantic_search", query="a", count=3,
                    paths=["hub.md", "b.md", "c.md"]),
            _search("semantic_search", query="b", count=2, paths=["hub.md", "b.md"]),
            _search("keyword_search", query="c", count=1, paths=["hub.md"]),
            # Excluded: no result set.
            _search("semantic_search", query="d", error="no_vault_assigned",
                    paths=["never.md"]),
        ])
        session.add_all([_note("hub.md"), _note("b.md")])
        await session.commit()

        rows = await top_logged_retrievals(session, "24h", None)

    ranked = [(r["path"], r["appearances"]) for r in rows]
    assert ranked[0] == ("hub.md", 3)
    assert ("b.md", 2) in ranked
    assert all(p != "never.md" for p, _ in ranked), (
        "a refused call's params must not feed the coverage ranking"
    )
    # `c.md` was retrieved but is not in the index: renamed or deleted since.
    by_path = {r["path"]: r for r in rows}
    assert by_path["hub.md"]["indexed"] is True
    assert by_path["c.md"]["indexed"] is False


async def test_never_retrieved_lists_only_indexed_notes_absent_from_the_window(clean):
    async with clean() as session:
        session.add_all([
            _note("seen.md", ago_days=5),
            _note("unseen-new.md", ago_days=1),
            _note("unseen-old.md", ago_days=30),
        ])
        session.add(_search("keyword_search", query="a", count=1, paths=["seen.md"]))
        await session.commit()

        rows, total = await never_retrieved(session, "24h", None)

    assert [r["path"] for r in rows] == ["unseen-new.md", "unseen-old.md"], (
        "newest-modified first — the interesting end of the list"
    )
    assert total == 2


async def test_a_row_with_a_non_array_result_paths_costs_one_row(clean):
    """`jsonb_array_elements_text` raises on a scalar. Unguarded that is a 500
    for both coverage lists."""
    async with clean() as session:
        session.add_all([
            _search("keyword_search", query="a", count=1, paths="a.md"),  # scalar
            _search("keyword_search", query="b", count=1, paths=["a.md"]),
        ])
        session.add_all([_note("a.md"), _note("b.md")])
        await session.commit()

        rows = await top_logged_retrievals(session, "24h", None)
        missing, total = await never_retrieved(session, "24h", None)

    assert [(r["path"], r["appearances"]) for r in rows] == [("a.md", 1)]
    assert [r["path"] for r in missing] == ["b.md"] and total == 1


async def test_the_logging_cap_is_what_the_page_claims(clean):
    """The ranking counts appearances *within the logged prefix*. A call that
    returned forty notes contributes ten paths and a `result_count` of 40 —
    which is precisely why the page labels the ranking and calls the
    never-retrieved list an upper bound."""
    from src.services.timing import fit_result_paths

    returned = [f"n{i}.md" for i in range(40)]
    logged = fit_result_paths(returned)
    assert len(logged) == LOGGED_RESULTS_PER_CALL

    async with clean() as session:
        session.add(
            _search("semantic_search", query="wide", count=len(returned), paths=logged)
        )
        session.add_all([_note(p) for p in returned])
        await session.commit()

        rows = await top_logged_retrievals(session, "24h", None)
        missing, total = await never_retrieved(session, "24h", None)
        top = await top_queries(session, "24h", None, "semantic_search")

    assert len(rows) == LOGGED_RESULTS_PER_CALL
    assert total == 30, "the notes below the logging cap read as never-retrieved"
    assert {r["path"] for r in missing} == set(returned[LOGGED_RESULTS_PER_CALL:])
    assert top[0]["mean_results"] == pytest.approx(40.0), (
        "result_count is the full count, not the logged one"
    )


# --------------------------------------------------------------------------
# 4. Tenancy: the identity rule, against two users sharing a path.
# --------------------------------------------------------------------------


async def test_two_users_sharing_a_path_are_never_merged(clean):
    """The case `(user_id, path)` exists for. Both users have
    `Daily/2026-08-29.md`; they are different notes, and a coverage list is a
    list of note paths, so merging them would be one tenant reading the
    other's vault."""
    shared = "Daily/2026-08-29.md"

    async with clean() as session:
        alice = User(username="alice-161", password_hash="x")
        bob = User(username="bob-161", password_hash="x")
        session.add_all([alice, bob])
        await session.flush()
        a, b = alice.id, bob.id

        session.add_all([
            _note(shared, user_id=a),
            _note("alice-only.md", user_id=a),
            _note(shared, user_id=b),
            _note("bob-only.md", user_id=b),
        ])
        session.add_all([
            _search("keyword_search", query="daily", count=1, paths=[shared],
                    user_id=a),
            _search("keyword_search", query="daily", count=1, paths=[shared],
                    user_id=a),
            _search("keyword_search", query="daily", count=1, paths=[shared],
                    user_id=b),
        ])
        await session.commit()

        mine = await top_logged_retrievals(session, "24h", a)
        mine_missing, mine_total = await never_retrieved(session, "24h", a)
        mine_queries = await top_queries(session, "24h", a, "keyword_search")
        everyone = await top_logged_retrievals(session, "24h", None)
        all_queries = await top_queries(session, "24h", None, "keyword_search")

    # Alice sees her own two appearances, not the three in the table.
    assert [(r["path"], r["appearances"]) for r in mine] == [(shared, 2)]
    assert [r["path"] for r in mine_missing] == ["alice-only.md"], (
        "bob's notes must not appear in alice's never-retrieved list"
    )
    assert mine_total == 1
    assert [(r["value"], r["calls"]) for r in mine_queries] == [("daily", 2)]

    # The admin view keeps them as separate rows rather than one row of 3.
    assert sorted((r["appearances"], r["owner"]) for r in everyone) == [
        (1, "bob-161"), (2, "alice-161")
    ]
    assert sorted((r["calls"], r["owner"]) for r in all_queries) == [
        (1, "bob-161"), (2, "alice-161")
    ]


async def test_the_ownerless_slice_is_not_dropped(clean):
    """`IS NOT DISTINCT FROM`, not `=`. Single-user mode writes NULL on every
    log row and every note; `usage_logs.user_id` is also ON DELETE SET NULL, so
    a deleted user's history lands here. `=` would silently make every one of
    those notes look never-retrieved."""
    async with clean() as session:
        session.add_all([_note("solo.md"), _note("unread.md")])
        session.add(_search("keyword_search", query="q", count=1, paths=["solo.md"]))
        await session.commit()

        rows = await top_logged_retrievals(session, "24h", None)
        missing, total = await never_retrieved(session, "24h", None)

    assert [(r["path"], r["appearances"], r["indexed"]) for r in rows] == [
        ("solo.md", 1, True)
    ]
    assert [r["path"] for r in missing] == ["unread.md"] and total == 1


async def test_an_owned_search_does_not_match_an_ownerless_note(clean):
    """The other half of NULL-safety: NULL is *its own* owner, not a wildcard.
    A named user's retrieval must not mark an ownerless note as retrieved."""
    async with clean() as session:
        user = User(username="named-161", password_hash="x")
        session.add(user)
        await session.flush()
        session.add(_note("shared-name.md", user_id=None))
        session.add(
            _search("keyword_search", query="q", count=1, paths=["shared-name.md"],
                    user_id=user.id)
        )
        await session.commit()

        rows = await top_logged_retrievals(session, "24h", None)
        missing, _ = await never_retrieved(session, "24h", None)

    assert [(r["path"], r["indexed"]) for r in rows] == [("shared-name.md", False)], (
        "the retrieved path belongs to the named user, and no note of theirs "
        "has it — the ownerless note with the same path is a different note"
    )
    assert [r["path"] for r in missing] == ["shared-name.md"]


# --------------------------------------------------------------------------
# 5. The page, end to end over a real database.
# --------------------------------------------------------------------------


async def test_all_three_windows_render_against_seeded_data(clean):
    from jinja2 import (
        ChainableUndefined,
        ChoiceLoader,
        DictLoader,
        Environment,
        FileSystemLoader,
    )

    from src.control_panel.routes import _label_group_value
    from src.services.usage_stats import WINDOW_LABELS, WINDOWS

    templates_dir = _harness.ROOT / "src" / "control_panel" / "templates"
    env = Environment(
        loader=ChoiceLoader([
            DictLoader({
                "base.html":
                    "{% block title %}{% endblock %}{% block content %}{% endblock %}"
            }),
            FileSystemLoader(str(templates_dir)),
        ]),
        undefined=ChainableUndefined,
        autoescape=True,
    )

    digest = "f" * 64
    async with clean() as session:
        session.add_all([
            _search("keyword_search", query="alpha", count=2, paths=["a.md", "b.md"]),
            _search("semantic_search", query="beta", count=0, paths=[],
                    ago_minutes=60 * 40),
            _search(RELATED_TOOL, source="Projects/Alpha.md", count=1, paths=["a.md"]),
            _search(RELATED_TOOL, source=digest, count=0, paths=[],
                    ago_minutes=60 * 24 * 12),
        ])
        session.add_all([_note("a.md"), _note("b.md"), _note("lonely.md")])
        await session.commit()

        for window in WINDOWS:
            tables = []
            for tool in QUERY_TOOLS:
                tables.append({
                    "tool": tool,
                    "label": tool,
                    "top": [
                        dict(r, label=_label_group_value(tool, r["value"]))
                        for r in await top_queries(session, window, None, tool)
                    ],
                    "zero": [
                        dict(r, label=_label_group_value(tool, r["value"]))
                        for r in await zero_result_queries(session, window, None, tool)
                    ],
                })
            related = {
                "tool": RELATED_TOOL,
                "label": "Related notes",
                "top": [
                    dict(r, label=_label_group_value(RELATED_TOOL, r["value"]))
                    for r in await top_queries(session, window, None, RELATED_TOOL)
                ],
                "zero": [
                    dict(r, label=_label_group_value(RELATED_TOOL, r["value"]))
                    for r in await zero_result_queries(
                        session, window, None, RELATED_TOOL
                    )
                ],
            }
            retrievals = await top_logged_retrievals(session, window, None)
            missing, missing_total = await never_retrieved(session, window, None)

            html = env.get_template("search_analytics.html").render(
                active="search-analytics",
                window=window,
                window_label=WINDOW_LABELS[window],
                windows=[
                    {"key": k, "label": k, "selected": k == window} for k in WINDOWS
                ],
                query_tables=tables,
                related=related,
                retrievals=retrievals,
                never_retrieved=missing,
                never_retrieved_total=missing_total,
                logged_per_call=LOGGED_RESULTS_PER_CALL,
                show_owner=False,
                has_data=bool(retrievals),
            )

            assert "Top-logged retrievals" in html
            assert WINDOW_LABELS[window] in html
            assert html.count(
                f"Only the first {LOGGED_RESULTS_PER_CALL} results of each call"
            ) == 2
            if window == "24h":
                assert "alpha" in html and "a.md" in html
                assert "lonely.md" in html, "never-retrieved renders"
                assert "beta" not in html, "a 7d-only row is outside this window"
            if window == "30d":
                # The over-long source is shown as a digest, not as a note path.
                assert "digest ffffffffffffffff" in html
                assert digest not in html
