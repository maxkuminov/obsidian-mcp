"""Opt-in integration: a raising tool's audit row, against real PostgreSQL.

The hermetic suite (`tests/test_issue_193_tool_exception.py`) pins the handler's
*shape* — what it guards, what it catches, what it may not claim — against a
captured `_log_usage`. What a fake cannot see is the half this module covers:

* the row `_tracked` builds for a raising body is **accepted by the schema** and
  readable back. `params` is JSONB and the row carries two keys no writer wrote
  before this change (`error = 'tool_exception'` and `error_type`), so "the
  audit row exists" is a claim about PostgreSQL, not about a dict;
* it is counted as **executed** by `usage_stats.executed_sql()`, not as a
  refusal. A tool that raises after seconds of I/O is the slowest path there is,
  and filing it under the pre-body predicate would drop it out of every
  percentile — the exact defect `vault_anchor_lost_at_publish` was split out to
  fix;
* `/admin/performance`'s aggregates read the window **without a 500**. That page
  casts `params->>'over_quota'` and the phase timings unguardedly, so a new
  reserved key is a live hazard until a real query has read a real row carrying
  it: one bad row takes the page down for every user until it ages out.

Skipped unless `PGVECTOR_TEST_ADMIN_URL` names a throwaway Postgres *server*
(the harness creates and drops its own database):

    docker run --rm -d --name pgvector-test -e POSTGRES_PASSWORD=test \\
        -p 55432:5432 pgvector/pgvector:pg16
    PGVECTOR_TEST_ADMIN_URL=postgresql+asyncpg://postgres:test@localhost:55432/postgres \\
        pytest -q tests/integration/test_issue_193_tool_exception_pg.py
    docker rm -f pgvector-test
"""
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import _harness
import src.mcp_server.tools as tools
from src.services.usage_stats import (
    executed_sql,
    pre_body_refusal_sql,
    PRE_BODY_REFUSAL_BINDS,
    slowest_requests,
    tool_aggregates,
)

DIM = 64

pytestmark = [
    _harness.requires_pgvector,
    pytest.mark.asyncio(loop_scope="module"),
]


class Boom(RuntimeError):
    """The tool body's own failure."""


@tools._tracked("probe_raises_pg", ["path"], resource_class="other")
async def probe_raises_pg(path: str = "a.md") -> str:
    raise Boom("the body failed after doing real work")


@tools._tracked("probe_ok_pg", ["path"], resource_class="other")
async def probe_ok_pg(path: str = "a.md") -> str:
    return "ran"


@pytest.fixture(scope="module")
def migrated_url():
    yield from _harness.throwaway_database("tool_exception_193", DIM)


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def sessionmaker(migrated_url):
    engine = create_async_engine(migrated_url, poolclass=None)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest_asyncio.fixture(loop_scope="module")
async def logging_to_pg(sessionmaker, monkeypatch):
    """`_log_usage` writes to the throwaway database; the vault gate is stubbed.

    Stubbed rather than satisfied: this module is about the row, and a real root
    would only add a filesystem to the surface.
    """
    monkeypatch.setattr(tools, "async_session", sessionmaker)
    monkeypatch.setattr(tools, "_vault_admission_error", lambda: None)
    async with sessionmaker() as session:
        await session.execute(text("DELETE FROM usage_logs"))
        await session.commit()
    yield sessionmaker
    async with sessionmaker() as session:
        await session.execute(text("DELETE FROM usage_logs"))
        await session.commit()


async def test_the_raising_bodys_row_lands_and_reads_back(logging_to_pg):
    with pytest.raises(Boom):
        await probe_raises_pg("Projects/plan.md")

    async with logging_to_pg() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT tool, duration_ms, response_size, "
                    "       params->>'error' AS error, "
                    "       params->>'error_type' AS error_type, "
                    "       params->>'path' AS path "
                    "FROM usage_logs ORDER BY id"
                )
            )
        ).fetchall()

    assert len(rows) == 1, "the audit row was discarded"
    row = rows[0]
    assert row.tool == "probe_raises_pg"
    assert row.error == tools._TOOL_EXCEPTION_MARKER
    assert row.error_type == "Boom"
    # The named arguments ride along exactly as they do on a successful call.
    assert row.path == "Projects/plan.md"
    assert row.duration_ms >= 0
    assert row.response_size == 0


async def test_the_row_counts_as_executed_and_not_as_a_refusal(logging_to_pg):
    with pytest.raises(Boom):
        await probe_raises_pg()

    async with logging_to_pg() as session:
        counts = (
            await session.execute(
                text(
                    "SELECT count(*) FILTER (WHERE "
                    f"{executed_sql()}) AS executed, "
                    "       count(*) FILTER (WHERE "
                    f"{pre_body_refusal_sql()}) AS refused "
                    "FROM usage_logs ul"
                ),
                dict(PRE_BODY_REFUSAL_BINDS),
            )
        ).one()

    assert counts.executed == 1, (
        "a body that raised did the work; filing it as a pre-body refusal drops "
        "the slowest calls in the server out of every percentile"
    )
    assert counts.refused == 0


async def test_a_permission_denied_row_is_executed_too(logging_to_pg):
    """The other post-body marker, seeded rather than driven.

    `permission_denied` is written by `_require_write` from inside a body that
    has already passed every pre-body gate and **consumed its quota slot**, so
    the predicate must read it as executed. That is residual R5 in exchange:
    a read-only credential probing a write tool dilutes its percentiles, and the
    refusal is made visible on `/admin/usage` instead of being filed as a
    pre-body refusal it is not.
    """
    async with logging_to_pg() as session:
        await session.execute(
            text(
                "INSERT INTO usage_logs (tool, params, duration_ms, response_size, "
                "created_at) VALUES ('create_note', "
                "jsonb_build_object('path', 'a.md', 'error', CAST(:marker AS text)), "
                "1, 120, now())"
            ),
            {"marker": tools._PERMISSION_DENIED_MARKER},
        )
        await session.commit()

        counts = (
            await session.execute(
                text(
                    "SELECT count(*) FILTER (WHERE "
                    f"{executed_sql()}) AS executed, "
                    "       count(*) FILTER (WHERE "
                    f"{pre_body_refusal_sql()}) AS refused "
                    "FROM usage_logs ul"
                ),
                dict(PRE_BODY_REFUSAL_BINDS),
            )
        ).one()
        rows = await tool_aggregates(session, "24h", None)

    assert (counts.executed, counts.refused) == (1, 0)
    row = next(r for r in rows if r["tool"] == "create_note")
    assert row["executed"] == 1
    assert row["refusals"] == 0


async def test_the_aggregates_read_the_window_without_a_cast_failure(logging_to_pg):
    """`/admin/performance` casts `params->>'over_quota'` and the phase timings
    with no guard, so a single row it cannot cast takes the page down with a 500
    for every user until it ages out. `error_type` is a new reserved key on that
    same JSONB column: this is the query actually reading a row that carries it.
    """
    for _ in range(3):
        with pytest.raises(Boom):
            await probe_raises_pg("slow.md")
    await probe_ok_pg("fast.md")

    async with logging_to_pg() as session:
        rows = await tool_aggregates(session, "24h", None)
        slowest = await slowest_requests(session, "24h", None)

    by_tool = {row["tool"]: row for row in rows}
    assert by_tool["probe_raises_pg"]["executed"] == 3
    assert by_tool["probe_raises_pg"]["refusals"] == 0
    assert by_tool["probe_ok_pg"]["executed"] == 1
    assert {r.tool for r in slowest} >= {"probe_raises_pg", "probe_ok_pg"}
