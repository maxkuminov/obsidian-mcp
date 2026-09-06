"""Real-Postgres gate for the performance aggregates and the pass record (#160).

These properties have no meaningful fake-session equivalent, because what they
assert *is* the database's behaviour:

* **The refusal predicate partitions the window.** Whether
  `COALESCE((params->>'over_quota')::boolean, false)` reads a JSON boolean, and
  whether `params->>'error' IN (...)` leaves a row carrying some *other* error
  value in the aggregates, is a fact about PostgreSQL's JSONB operators. A
  string assertion on the fragment cannot see it, and getting it wrong is
  silent: the page would simply report the wrong numbers.
* **`percentile_cont` over a seeded window.** The reason the page exists is that
  nobody wants to write this SQL by hand; a test that does not run it has not
  checked it.
* **The refusal count sums `1 + suppressed`.** `rate_limited` rows are
  coalesced at the write site, so the row count stopped being the refusal
  count; whether `sum(...) FILTER (...)` over a guarded `::bigint` produces the
  right figure — and whether a malformed `suppressed` yields NULL rather than
  aborting the statement and taking the whole window down — is a fact about
  PostgreSQL, not about the fragment's text.
* **The prune leaves exactly 500 rows**, the `trigger` CHECK rejects a fifth
  value, and `ON DELETE SET NULL` keeps a pass's history past its user.

Skipped unless `PGVECTOR_TEST_ADMIN_URL` names a throwaway Postgres *server*
(the harness creates and drops its own database):

    docker run --rm -d --name pgvector-test -e POSTGRES_PASSWORD=test \\
        -p 55432:5432 pgvector/pgvector:pg16
    PGVECTOR_TEST_ADMIN_URL=postgresql+asyncpg://postgres:test@localhost:55432/postgres \\
        pytest -q tests/integration/test_issue_160_performance_pg.py
    docker rm -f pgvector-test
"""
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import _harness
from src.models.db import IndexerRun, UsageLog, User
from src.services import indexer
from src.services.usage_stats import (
    phase_breakdown,
    recent_indexer_runs,
    slowest_requests,
    tool_aggregates,
)

DIM = 64

pytestmark = [
    pytest.mark.asyncio(loop_scope="module"),
    _harness.requires_pgvector,
]


@pytest.fixture(scope="module")
def migrated_url():
    yield from _harness.throwaway_database("perf_160", DIM)


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def sessionmaker(migrated_url):
    engine = create_async_engine(migrated_url, poolclass=None)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest_asyncio.fixture(loop_scope="module")
async def clean(sessionmaker):
    """Empty both tables before and after each case."""
    async with sessionmaker() as session:
        await session.execute(delete(UsageLog))
        await session.execute(delete(IndexerRun))
        await session.execute(delete(User))
        await session.commit()
    yield sessionmaker
    async with sessionmaker() as session:
        await session.execute(delete(UsageLog))
        await session.execute(delete(IndexerRun))
        await session.execute(delete(User))
        await session.commit()


def _log(tool, *, duration=None, size=None, params=None, ago_minutes=1, user_id=None):
    return UsageLog(
        tool=tool,
        params=params,
        duration_ms=duration,
        response_size=size,
        user_id=user_id,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=ago_minutes),
    )


# --------------------------------------------------------------------------
# 1. The refusal predicate: exactly the enumerated markers, and nothing else.
# --------------------------------------------------------------------------


async def test_the_predicate_excludes_exactly_the_enumerated_markers(clean):
    """Three refusals, each carrying one enumerated marker, plus an executed
    row whose `params.error` is some *other* value. The other value must stay
    in the aggregates: its body ran, did the work of resolving a vault, and
    then refused to publish. Excluding it hides the slowest write path in the
    server from the one view built to find slow paths."""
    async with clean() as session:
        session.add_all([
            # Executed: the only rows the percentiles may see.
            _log("edit_note", duration=100, size=10),
            _log("edit_note", duration=200, size=20),
            # Executed, and carrying a post-body error marker. Still executed.
            _log(
                "edit_note",
                duration=900,
                size=30,
                params={"path": "a.md", "error": "vault_assignment_changed"},
            ),
            # Pre-body refusals: all three enumerated forms.
            _log("edit_note", duration=0, size=1, params={"error": "no_vault_assigned"}),
            _log("edit_note", duration=0, size=1, params={"error": "argument_not_encodable"}),
            _log("edit_note", duration=0, size=1, params={"over_quota": True}),
            # `over_quota: false` is not a refusal.
            _log("edit_note", duration=300, size=40, params={"over_quota": False}),
        ])
        await session.commit()

        rows = await tool_aggregates(session, "24h", None)

    assert len(rows) == 1
    row = rows[0]
    assert row["tool"] == "edit_note"
    assert row["executed"] == 4, (
        "the post-body-error row and the over_quota=false row must count as "
        f"executed; got {row['executed']}"
    )
    assert row["refusals"] == 3
    # 100, 200, 300, 900 -> p50 is the midpoint of 200 and 300.
    assert row["p50"] == pytest.approx(250.0)
    assert row["max_size"] == 40
    assert row["mean_size"] == 25  # (10 + 20 + 30 + 40) / 4


async def test_a_publish_time_anchor_loss_stays_in_the_percentiles(clean):
    """`vault_anchor_lost_at_publish` is a post-body marker and must aggregate.

    It used to be logged as `no_vault_assigned` — the admission gate's value,
    which this predicate enumerates as "the body never started". The branch that
    writes it sits at the *publication* step of a mutating tool: the root was
    resolved, the note read, the write computed, and only then did the anchor go
    missing (the #88 race). Filed under the pre-body value it was dropped from
    the percentiles, so the single most expensive class of refusal was invisible
    on the page built to find expensive calls.
    """
    from src.mcp_server import tools

    async with clean() as session:
        session.add_all([
            _log("edit_note", duration=50, size=10),
            _log(
                "edit_note",
                duration=4000,
                size=20,
                params={"error": tools._ANCHOR_LOST_AT_PUBLISH_MARKER},
            ),
        ])
        await session.commit()

        rows = await tool_aggregates(session, "24h", None)
        slowest = await slowest_requests(session, "24h", None)

    row = next(r for r in rows if r["tool"] == "edit_note")
    assert row["executed"] == 2, "the anchor-loss row's body ran"
    assert row["refusals"] == 0
    assert row["p99"] >= 3000, "the expensive row must reach the percentiles"
    # And it is visible where an operator would go looking for it.
    assert [r.duration_ms for r in slowest] == [4000, 50]


async def test_a_refusal_storm_does_not_move_the_percentiles(clean):
    """The spec's scenario, at a scale that would be obvious on the page: 100
    executed calls and 5,000 over-quota refusals."""
    async with clean() as session:
        session.add_all(
            [_log("semantic_search", duration=500 + i, size=100) for i in range(100)]
        )
        session.add_all([
            _log("semantic_search", duration=0, size=1, params={"over_quota": True})
            for _ in range(5000)
        ])
        await session.commit()

        rows = await tool_aggregates(session, "24h", None)

    row = next(r for r in rows if r["tool"] == "semantic_search")
    assert row["executed"] == 100
    assert row["refusals"] == 5000
    assert 500 <= row["p50"] <= 600, row["p50"]
    assert row["p99"] >= 590


async def test_a_tool_that_only_refused_reports_no_percentiles(clean):
    async with clean() as session:
        session.add_all([
            _log("read_note", duration=0, size=1, params={"error": "no_vault_assigned"})
            for _ in range(3)
        ])
        await session.commit()

        rows = await tool_aggregates(session, "24h", None)

    row = next(r for r in rows if r["tool"] == "read_note")
    assert row["executed"] == 0
    assert row["refusals"] == 3
    assert row["p50"] is None and row["p95"] is None and row["p99"] is None
    assert row["mean_size"] is None and row["max_size"] is None


async def test_coalesced_rate_refusals_are_counted_in_full(clean):
    """The coalescer's arithmetic, read back through the page's SQL.

    `rate_limited` rows are written at most once per
    `(principal, tool, marker, scope)` per interval, and each carries the
    `suppressed` count of the refusals folded into it that no other row
    represents. The row count is therefore no longer the refusal count —
    `1 + suppressed` is — and a reader that counted rows would report three
    refusals for a window holding three thousand. That is not a rounding error:
    the coalescer exists precisely so the row count stops tracking the arrival
    rate, so the undercount grows with exactly the traffic an operator opened
    the page to see.

    `argument_too_long` is in the same window and deliberately **not**
    coalesced — refused below the general bucket, so its rate is already
    bounded by that bucket. It carries no `suppressed` key, weighs 1, and goes
    through the same expression rather than a second code path.
    """
    async with clean() as session:
        session.add_all([
            # 40 executed calls: the only rows the percentiles may see.
            _log("semantic_search", duration=100 + i, size=50) for i in range(40)
        ])
        session.add_all([
            # Three coalesced rows standing for 2997 refusals between them.
            # Two scopes, because `principal` and `principal_write` are
            # different facts about the same tool and both are pre-body.
            _log("semantic_search", duration=0, size=1, params={
                "error": "rate_limited", "rate_limit_scope": "principal",
                "suppressed": 1499,
            }),
            _log("semantic_search", duration=0, size=1, params={
                "error": "rate_limited", "rate_limit_scope": "principal",
                "suppressed": 1000,
            }),
            _log("semantic_search", duration=0, size=1, params={
                "error": "rate_limited", "rate_limit_scope": "principal_write",
                "suppressed": 495,
            }),
            # A window-opening row: `suppressed = 0` stands for exactly itself.
            _log("semantic_search", duration=0, size=1, params={
                "error": "rate_limited", "rate_limit_scope": "principal",
                "suppressed": 0,
            }),
            # Two uncoalesced over-long-argument refusals, one row each.
            _log("semantic_search", duration=0, size=1,
                 params={"error": "argument_too_long"}),
            _log("semantic_search", duration=0, size=1,
                 params={"error": "argument_too_long"}),
        ])
        await session.commit()

        rows = await tool_aggregates(session, "24h", None)

    row = next(r for r in rows if r["tool"] == "semantic_search")
    assert row["executed"] == 40, "no refusal row may reach the executed count"
    # (1+1499) + (1+1000) + (1+495) + (1+0) + 1 + 1 = 3000.
    assert row["refusals"] == 3000, (
        f"the refusal count must sum 1 + suppressed; got {row['refusals']} "
        "(6 would be the row count)"
    )
    # The percentiles come from the 40 executed rows alone. The refusal rows
    # carry duration 0, so a leak would drag p50 toward zero.
    assert 115 <= row["p50"] <= 125, row["p50"]
    assert row["p99"] >= 130
    assert row["max_size"] == 50
    assert row["mean_size"] == 50


async def test_a_malformed_suppressed_value_renders_the_window(clean):
    """The guard on the cast, which is why there is a guard.

    `params` is JSONB and `->>` yields text. An unguarded `::bigint` on a value
    that is not a number does not return NULL — it **aborts the statement**, so
    one bad row takes down the whole window's page rather than reporting one
    wrong figure. A value that fails the guard still weighs 1: the row is a
    refusal either way, and only its suppressed tail is unreadable.

    Both shapes of malformed are here — a non-numeric string, and a numeric one
    far too large for `bigint`, which a length-unbounded guard would admit and
    then overflow on.
    """
    async with clean() as session:
        session.add_all([
            _log("keyword_search", duration=70, size=10),
            _log("keyword_search", duration=0, size=1, params={
                "error": "rate_limited", "suppressed": "lots",
            }),
            _log("keyword_search", duration=0, size=1, params={
                "error": "rate_limited", "suppressed": "9" * 40,
            }),
            _log("keyword_search", duration=0, size=1, params={
                "error": "rate_limited", "suppressed": -3,
            }),
            _log("keyword_search", duration=0, size=1, params={
                "error": "rate_limited", "suppressed": None,
            }),
            # And one readable row alongside them, so the aggregate is not
            # trivially right by every value being unreadable.
            _log("keyword_search", duration=0, size=1, params={
                "error": "rate_limited", "suppressed": 9,
            }),
        ])
        await session.commit()

        rows = await tool_aggregates(session, "24h", None)

    row = next(r for r in rows if r["tool"] == "keyword_search")
    assert row["executed"] == 1
    # Four unreadable rows weigh 1 each; the readable one weighs 10.
    assert row["refusals"] == 14, row["refusals"]
    assert row["p50"] == pytest.approx(70.0)


async def test_a_provider_input_rejection_is_an_executed_row(clean):
    """The marker whose caller-facing *code* is `argument_too_long` and whose
    *marker* is not.

    The character cap refuses before any provider call; this branch is reached
    only after the body resolved a vault and paid for a network round trip to
    the embedding provider, which is the slowest kind of call the server makes.
    Enumerating it as a pre-body refusal would drop that round trip out of the
    percentiles — the `vault_anchor_lost_at_publish` mistake exactly. An
    unrelated `params.error` value is here for the same reason: the predicate
    enumerates, it does not broadly match "this row carries an error".
    """
    from src.mcp_server import tools

    async with clean() as session:
        session.add_all([
            _log("semantic_search", duration=60, size=10),
            _log("semantic_search", duration=5000, size=20, params={
                "error": tools._PROVIDER_INPUT_REJECTED_MARKER,
            }),
            _log("semantic_search", duration=800, size=30, params={
                "error": "some_other_marker_nobody_enumerated",
            }),
            # The pre-body half of the same caller-facing code, for contrast.
            _log("semantic_search", duration=0, size=1, params={
                "error": tools._ARGUMENT_TOO_LONG_MARKER,
            }),
        ])
        await session.commit()

        rows = await tool_aggregates(session, "24h", None)
        slowest = await slowest_requests(session, "24h", None)

    row = next(r for r in rows if r["tool"] == "semantic_search")
    assert row["executed"] == 3, (
        "the provider rejection and the unenumerated error both ran a body"
    )
    assert row["refusals"] == 1, "only the pre-body cap refusal is a refusal"
    assert row["p99"] >= 4000, "the provider round trip must reach the percentiles"
    # And it is visible where an operator would go looking for it.
    assert [r.duration_ms for r in slowest] == [5000, 800, 60]


# --------------------------------------------------------------------------
# 2. Windows.
# --------------------------------------------------------------------------


async def test_a_window_sees_only_its_own_rows(clean):
    async with clean() as session:
        session.add_all([
            _log("list_notes", duration=10, ago_minutes=30),          # in 24h
            _log("list_notes", duration=1000, ago_minutes=60 * 72),   # in 7d only
            _log("list_notes", duration=9999, ago_minutes=60 * 24 * 20),  # 30d only
        ])
        await session.commit()

        day = await tool_aggregates(session, "24h", None)
        week = await tool_aggregates(session, "7d", None)
        month = await tool_aggregates(session, "30d", None)

    assert day[0]["executed"] == 1 and day[0]["p50"] == 10
    assert week[0]["executed"] == 2
    assert month[0]["executed"] == 3


async def test_an_empty_window_is_an_empty_result_not_an_error(clean):
    async with clean() as session:
        assert await tool_aggregates(session, "7d", None) == []
        assert await slowest_requests(session, "7d", None) == []
        phases = await phase_breakdown(session, "7d", None)
        assert [p["count"] for p in phases] == [0, 0]
        assert all(p["mean"] is None and p["p95"] is None for p in phases)


async def test_a_regular_user_sees_only_their_own_rows(clean):
    async with clean() as session:
        alice = User(username="alice-160", password_hash="x")
        bob = User(username="bob-160", password_hash="x")
        session.add_all([alice, bob])
        await session.flush()
        session.add_all([
            _log("get_tags", duration=5, user_id=alice.id),
            _log("get_tags", duration=5000, user_id=bob.id),
        ])
        await session.commit()

        mine = await tool_aggregates(session, "24h", alice.id)
        everyone = await tool_aggregates(session, "24h", None)

    assert mine[0]["executed"] == 1 and mine[0]["max_size"] is None
    assert mine[0]["p50"] == 5
    assert everyone[0]["executed"] == 2


# --------------------------------------------------------------------------
# 3. Phase breakdown: missing keys excluded, not zeroed.
# --------------------------------------------------------------------------


async def test_missing_phase_keys_are_excluded_rather_than_counted_as_zero(clean):
    async with clean() as session:
        session.add_all([
            _log("semantic_search", duration=300, params={"embed_ms": 100, "db_ms": 20}),
            _log("semantic_search", duration=500, params={"embed_ms": 300, "db_ms": 40}),
            # No phase keys at all: a note tool measures nothing.
            _log("read_note", duration=5, params={"path": "a.md"}),
            # A refusal that somehow carries a phase key must still be excluded
            # by the executed filter.
            _log(
                "semantic_search",
                duration=0,
                params={"error": "no_vault_assigned", "embed_ms": 99999},
            ),
        ])
        await session.commit()

        phases = await phase_breakdown(session, "24h", None)

    by_phase = {p["phase"]: p for p in phases}
    assert by_phase["embed_ms"]["count"] == 2
    assert by_phase["embed_ms"]["mean"] == pytest.approx(200.0), (
        "a tool that records no embed_ms must not drag the mean toward zero"
    )
    assert by_phase["db_ms"]["count"] == 2
    assert by_phase["db_ms"]["mean"] == pytest.approx(30.0)
    assert by_phase["embed_ms"]["p95"] is not None


# --------------------------------------------------------------------------
# 4. Slowest requests.
# --------------------------------------------------------------------------


async def test_slowest_is_ordered_capped_and_attributed(clean):
    async with clean() as session:
        session.add_all([
            UsageLog(
                tool="keyword_search",
                duration_ms=i,
                response_size=i,
                actor_kind="api_key",
                actor_label=f"key-{i}",
                actor_ref="omcp_abc",
                created_at=datetime.now(timezone.utc),
            )
            for i in range(1, 61)
        ])
        # A refusal slower than everything else must not appear.
        session.add(
            _log("keyword_search", duration=999999, params={"error": "no_vault_assigned"})
        )
        await session.commit()

        rows = await slowest_requests(session, "24h", None)

    assert len(rows) == 50, "the table is capped at 50"
    durations = [r.duration_ms for r in rows]
    assert durations == sorted(durations, reverse=True)
    assert durations[0] == 60, "a pre-body refusal must not head the table"
    assert rows[0].actor_label == "key-60"


# --------------------------------------------------------------------------
# 5. The pass record.
# --------------------------------------------------------------------------


async def test_a_pass_records_a_row_and_a_failing_one_records_its_error(
    clean, monkeypatch
):
    monkeypatch.setattr(indexer, "async_session", clean)

    async with indexer.record_indexer_run("scheduled", None) as stats:
        stats.record_index((42, 7))
        stats.record_embedded(3)

    with pytest.raises(RuntimeError):
        async with indexer.record_indexer_run("manual", None) as stats:
            stats.record_index((1, 1))
            raise RuntimeError("ollama refused the connection")

    async with clean() as session:
        rows = (
            await session.execute(select(IndexerRun).order_by(IndexerRun.id))
        ).scalars().all()

    assert [r.trigger for r in rows] == ["scheduled", "manual"]
    ok, failed = rows
    assert (ok.notes_scanned, ok.notes_indexed, ok.notes_embedded) == (42, 7, 3)
    assert ok.error is None
    assert ok.finished_at is not None and ok.finished_at >= ok.started_at
    assert "ollama refused the connection" in failed.error
    assert failed.finished_at is not None, (
        "a pass that raised must still have a finish time — that row is "
        "exactly the one an operator goes looking for"
    )


async def test_the_history_is_pruned_to_five_hundred(clean, monkeypatch):
    monkeypatch.setattr(indexer, "async_session", clean)
    base = datetime.now(timezone.utc) - timedelta(days=10)

    async with clean() as session:
        session.add_all([
            IndexerRun(
                trigger="scheduled",
                started_at=base + timedelta(seconds=i),
                finished_at=base + timedelta(seconds=i + 1),
                notes_scanned=i,
            )
            for i in range(505)
        ])
        await session.commit()

    # One more pass, which prunes in the same transaction as its own insert.
    async with indexer.record_indexer_run("scheduled", None) as stats:
        stats.record_index((1, 1))

    async with clean() as session:
        total = (await session.execute(select(func.count(IndexerRun.id)))).scalar()
        oldest_kept = (
            await session.execute(select(func.min(IndexerRun.started_at)))
        ).scalar()

    assert total == 500
    # 506 rows existed momentarily; the six oldest went, by *start* time —
    # passes are inserted at their finish, so id order is not start order.
    assert oldest_kept == base + timedelta(seconds=6)


async def test_a_per_user_pass_carries_its_user_and_survives_the_delete(
    clean, monkeypatch
):
    """The spec's two-user history, plus the reason `user_id` is SET NULL: the
    record that the server spent an hour indexing a vault is a fact about the
    server, and deleting the user must not erase it."""
    monkeypatch.setattr(indexer, "async_session", clean)

    async with clean() as session:
        one = User(username="one-160", password_hash="x")
        two = User(username="two-160", password_hash="x")
        session.add_all([one, two])
        await session.commit()
        one_id, two_id = one.id, two.id

    for uid in (one_id, two_id):
        async with indexer.record_indexer_run("startup", uid) as stats:
            stats.record_index((10, 1))

    async with clean() as session:
        rows = (
            await session.execute(select(IndexerRun).order_by(IndexerRun.id))
        ).scalars().all()
        assert sorted(r.user_id for r in rows) == sorted([one_id, two_id])

        await session.execute(delete(User).where(User.id == one_id))
        await session.commit()

    # A *fresh* session: `expire_on_commit=False` means the objects loaded
    # above still carry the user_id they were read with, so re-reading in the
    # same identity map would assert nothing about what the FK did.
    async with clean() as session:
        rows = (
            await session.execute(select(IndexerRun).order_by(IndexerRun.id))
        ).scalars().all()

    assert len(rows) == 2, "deleting a user must not delete their pass history"
    assert sorted(r.user_id if r.user_id is not None else -1 for r in rows) == [-1, two_id]


async def test_the_trigger_check_rejects_a_fifth_value(clean):
    """The panel groups and labels by this value; a typo'd trigger would render
    as a silent fifth category nobody notices."""
    from sqlalchemy.exc import IntegrityError

    async with clean() as session:
        session.add(IndexerRun(trigger="whenever"))
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

    for trigger in ("startup", "scheduled", "manual", "backfill"):
        async with clean() as session:
            session.add(IndexerRun(trigger=trigger))
            await session.commit()


async def test_all_three_windows_render_against_seeded_data(clean):
    """The page's own shape, end to end over a real database: the three
    aggregate queries the route issues, and the template rendering their
    results. What this cannot cover is the deployed server — that is the
    post-deploy exercise in the change's task 4.2."""
    from jinja2 import (
        ChainableUndefined,
        ChoiceLoader,
        DictLoader,
        Environment,
        FileSystemLoader,
    )

    from src.services.usage_stats import (
        RECENT_RUNS_LIMIT,
        WINDOW_LABELS,
        WINDOWS,
        recent_indexer_runs,
    )

    templates_dir = _harness.ROOT / "src" / "control_panel" / "templates"
    env = Environment(
        loader=ChoiceLoader([
            DictLoader({
                "base.html": "{% block title %}{% endblock %}{% block content %}{% endblock %}"
            }),
            FileSystemLoader(str(templates_dir)),
        ]),
        undefined=ChainableUndefined,
        autoescape=True,
    )

    async with clean() as session:
        session.add_all([
            _log("semantic_search", duration=400, size=900,
                 params={"query": "x", "embed_ms": 120, "db_ms": 30}, ago_minutes=10),
            _log("semantic_search", duration=1800, size=1200,
                 params={"query": "y", "embed_ms": 900, "db_ms": 60}, ago_minutes=60 * 40),
            _log("read_note", duration=8, size=4000, ago_minutes=60 * 24 * 12),
            _log("read_note", duration=0, size=1,
                 params={"error": "no_vault_assigned"}, ago_minutes=5),
        ])
        await session.commit()

        seen = {}
        for window in WINDOWS:
            tools = await tool_aggregates(session, window, None)
            phases = await phase_breakdown(session, window, None)
            slowest = await slowest_requests(session, window, None)
            seen[window] = sum(t["executed"] for t in tools)
            html = env.get_template("performance.html").render(
                active="performance",
                window=window,
                window_label=WINDOW_LABELS[window],
                windows=[
                    {"key": k, "label": k, "selected": k == window} for k in WINDOWS
                ],
                tools=tools,
                phases=phases,
                slowest=[{
                    "created_at": r.created_at.isoformat(),
                    "tool": r.tool,
                    "duration_ms": r.duration_ms,
                    "response_size": r.response_size,
                    "actor_name": r.actor_label,
                    "actor_detail": r.actor_ref,
                } for r in slowest],
                runs=await recent_indexer_runs(session, None),
                runs_limit=RECENT_RUNS_LIMIT,
                has_data=any(t["executed"] or t["refusals"] for t in tools),
            )
            assert "Per-tool latency" in html
            assert WINDOW_LABELS[window] in html
            # The refusal is counted and never priced into a percentile.
            if window == "24h":
                read = next(t for t in tools if t["tool"] == "read_note")
                assert read["executed"] == 0 and read["refusals"] == 1
                assert "400" in html

    # The windows are nested, so each one sees at least what the shorter saw.
    assert seen["24h"] <= seen["7d"] <= seen["30d"]
    assert seen["24h"] == 1 and seen["7d"] == 2 and seen["30d"] == 3


async def test_the_migration_round_trips(migrated_url):
    """`downgrade` drops the table 019 created, `upgrade` puts it back."""
    _harness.run_alembic(migrated_url, "downgrade", "018", dimensions=DIM)
    engine = create_async_engine(migrated_url, poolclass=None)
    try:
        async with engine.connect() as conn:
            present = (
                await conn.execute(text("SELECT to_regclass('indexer_runs')"))
            ).scalar()
            assert present is None
    finally:
        await engine.dispose()

    _harness.run_alembic(migrated_url, "upgrade", "head", dimensions=DIM)
    engine = create_async_engine(migrated_url, poolclass=None)
    try:
        async with engine.connect() as conn:
            present = (
                await conn.execute(text("SELECT to_regclass('indexer_runs')"))
            ).scalar()
            assert present is not None
    finally:
        await engine.dispose()


# --------------------------------------------------------------------------
# 6. The pass history the page renders.
# --------------------------------------------------------------------------


async def test_two_users_runs_come_back_with_distinct_owner_labels(clean, monkeypatch):
    """The spec's "Run displays SHALL label per-user runs by owner", against the
    join that produces the label. Two per-user passes an operator cannot
    attribute look like one user's vault being indexed twice."""
    monkeypatch.setattr(indexer, "async_session", clean)

    async with clean() as session:
        alice = User(username="alice-160", password_hash="x")
        bob = User(username="bob-160", password_hash="x")
        session.add_all([alice, bob])
        await session.commit()
        alice_id, bob_id = alice.id, bob.id

    async with indexer.record_indexer_run("startup", alice_id) as stats:
        stats.record_index((1200, 4))
    async with indexer.record_indexer_run("scheduled", bob_id) as stats:
        stats.record_index((340, 0))
    # An ownerless pass: single-user or global.
    async with indexer.record_indexer_run("backfill", None) as stats:
        stats.record_index((7, 7))

    async with clean() as session:
        runs = await recent_indexer_runs(session, None)

    assert [r["trigger"] for r in runs] == ["backfill", "scheduled", "startup"], (
        "newest first"
    )
    by_owner = {r["owner"]: r for r in runs}
    assert set(by_owner) == {"alice-160", "bob-160", None}
    assert by_owner["alice-160"]["notes_scanned"] == 1200
    assert by_owner["bob-160"]["notes_scanned"] == 340
    assert by_owner[None]["user_id"] is None
    assert all(not r["owner_missing"] for r in runs)
    # Durations are computed, not stored.
    assert all(r["duration"] is not None for r in runs)


async def test_a_deleted_owner_renders_as_ownerless_not_as_a_stale_name(
    clean, monkeypatch
):
    """Why the owner is joined live rather than denormalised. `usage_logs` keeps
    `actor_*` columns so a deleted credential still renders (#77) — an actor is
    a historical fact about who called. An owner is a live fact about who a row
    belongs to, and the FK is ON DELETE SET NULL: when the user goes, the row's
    claim about whose vault this pass indexed stops being true."""
    monkeypatch.setattr(indexer, "async_session", clean)

    async with clean() as session:
        user = User(username="doomed-160", password_hash="x")
        session.add(user)
        await session.commit()
        uid = user.id

    async with indexer.record_indexer_run("startup", uid) as stats:
        stats.record_index((99, 1))

    async with clean() as session:
        await session.execute(delete(User).where(User.id == uid))
        await session.commit()

    async with clean() as session:
        runs = await recent_indexer_runs(session, None)

    assert len(runs) == 1, "the pass history survives the user"
    assert runs[0]["owner"] is None and runs[0]["user_id"] is None
    assert runs[0]["owner_missing"] is False
    assert runs[0]["notes_scanned"] == 99


async def test_a_regular_user_sees_only_their_own_passes(clean, monkeypatch):
    monkeypatch.setattr(indexer, "async_session", clean)

    async with clean() as session:
        one = User(username="scoped-a-160", password_hash="x")
        two = User(username="scoped-b-160", password_hash="x")
        session.add_all([one, two])
        await session.commit()
        one_id, two_id = one.id, two.id

    for uid in (one_id, two_id):
        async with indexer.record_indexer_run("scheduled", uid) as stats:
            stats.record_index((5, 0))
    async with indexer.record_indexer_run("backfill", None):
        pass

    async with clean() as session:
        assert len(await recent_indexer_runs(session, None)) == 3, "admins see all"
        mine = await recent_indexer_runs(session, one_id)

    assert [r["user_id"] for r in mine] == [one_id]


async def test_the_history_is_capped_at_the_limit(clean, monkeypatch):
    monkeypatch.setattr(indexer, "async_session", clean)

    for _ in range(25):
        async with indexer.record_indexer_run("scheduled", None):
            pass

    async with clean() as session:
        assert len(await recent_indexer_runs(session, None)) == 20
        assert len(await recent_indexer_runs(session, None, limit=5)) == 5
        # Clamped, never trusted: this is a display cap, not a query knob.
        assert len(await recent_indexer_runs(session, None, limit=100000)) == 25


async def test_a_user_deleted_mid_pass_is_recorded_with_no_owner(clean, monkeypatch):
    """The FK race, against the real driver error.

    `user_id` is captured at pass start and inserted at pass end. Deleting the
    user in between makes the INSERT fail — `ON DELETE SET NULL` cannot help,
    because it fires on rows that already exist and this one never got in.
    Swallowed, the pass would vanish: the longest passes are the likeliest to
    lose the race, and "the operator just deleted a user" is exactly when they
    open the page.
    """
    monkeypatch.setattr(indexer, "async_session", clean)

    async with clean() as session:
        user = User(username="racing-160", password_hash="x")
        session.add(user)
        await session.commit()
        uid = user.id

    async with indexer.record_indexer_run("scheduled", uid) as stats:
        stats.record_index((4000, 12))
        # The administrator deletes the user while the pass is still running.
        async with clean() as session:
            await session.execute(delete(User).where(User.id == uid))
            await session.commit()

    async with clean() as session:
        runs = await recent_indexer_runs(session, None)

    assert len(runs) == 1, "the pass must survive its owner being deleted mid-flight"
    assert runs[0]["user_id"] is None
    assert runs[0]["notes_scanned"] == 4000, "the pass's own numbers survive"
    assert runs[0]["trigger"] == "scheduled"
