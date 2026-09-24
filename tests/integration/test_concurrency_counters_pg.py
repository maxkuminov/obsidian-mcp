"""Real-Postgres gate for the durable concurrency counters (#188, design D8).

What only a real server shows: the multi-row upsert's arithmetic (count added,
`max_value` the greater, NULL kept for count metrics), that a flush is one
counter statement whatever the request volume, that the run row's watermark
advances with no traffic and never backwards, and that the coverage reader
turns hard kills, clean restarts and lossy runs into the intervals the
readiness evaluator relies on.

Times are injected (`now=` / `at=`), so every scenario is a fixed calendar
instant rather than a race against the wall clock.

Skipped unless `PGVECTOR_TEST_ADMIN_URL` names a throwaway Postgres server; run
it with `make test-integration`.
"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import _harness
from src.services import concurrency
from src.services import concurrency_counters as cc

pytestmark = [_harness.requires_pgvector, pytest.mark.asyncio(loop_scope="module")]

EPOCH = "0123456789ab"
UTC = dt.timezone.utc


def at(hh, mm, ss=0, day=23):
    return dt.datetime(2026, 9, day, hh, mm, ss, tzinfo=UTC)


@pytest.fixture(scope="module")
def migrated_url():
    yield from _harness.throwaway_database("concurrency_counters", 64)


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def db(migrated_url):
    engine = create_async_engine(migrated_url, pool_size=2, max_overflow=0)
    statements = []

    def spy(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", spy)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield SimpleNamespace(engine=engine, maker=maker, statements=statements)
    await engine.dispose()


@pytest_asyncio.fixture(loop_scope="module")
async def env(db):
    async with db.maker() as session:
        await session.execute(text("TRUNCATE concurrency_counters, concurrency_runs"))
        await session.commit()
    cc.reset_for_tests()
    concurrency.reset_counters()
    db.statements.clear()

    def controller(mode="queue", epoch=EPOCH):
        return SimpleNamespace(mode=mode, epoch=epoch)

    async def register(now, mode="queue", epoch=EPOCH):
        return await cc.register_run(controller=controller(mode, epoch), now=now,
                                     session_factory=db.maker)

    async def flush(now, clean=False, factory=None):
        return await cc.flush(clean, now=now, session_factory=factory or db.maker)

    async def buckets():
        async with db.maker() as session:
            rows = (await session.execute(text(
                "SELECT bucket_start, metric, count, max_value, mode, epoch "
                "FROM concurrency_counters ORDER BY bucket_start, metric"))).all()
        return {(r.bucket_start, r.metric): (r.count, r.max_value) for r in rows}

    async def run_row(run):
        async with db.maker() as session:
            return (await session.execute(text(
                "SELECT * FROM concurrency_runs WHERE run_id = :r"),
                {"r": run.run_id})).one()

    async def coverage(start, end, mode="queue", epoch=EPOCH):
        async with db.maker() as session:
            return await cc.coverage(session, start, end, epoch, mode)

    yield SimpleNamespace(db=db, register=register, flush=flush, buckets=buckets,
                          run_row=run_row, coverage=coverage)
    cc.reset_for_tests()
    concurrency.reset_counters()


def kill():
    """A hard kill: the process's unflushed accumulator and its run state are
    gone, and no shutdown flush ran."""
    cc.reset_for_tests()
    concurrency.reset_counters()


# ── registration and the watermark ──────────────────────────────────────


async def test_register_run_inserts_the_row_at_its_start(env):
    run = await env.register(at(12, 0, 7), mode="shadow")
    row = await env.run_row(run)
    assert (row.epoch, row.mode) == (EPOCH, "shadow")
    assert row.started_at == row.completed_through == at(12, 0, 7)
    assert (row.clean_shutdown, row.lossy) == (False, False)


async def test_the_watermark_advances_with_no_traffic(env):
    run = await env.register(at(12, 0, 7))
    assert await env.flush(at(12, 1, 7))
    assert (await env.run_row(run)).completed_through == at(12, 1)
    assert await env.flush(at(12, 2, 7))
    assert (await env.run_row(run)).completed_through == at(12, 2)
    assert await env.buckets() == {}, "an idle flush writes no counter row"


async def test_the_watermark_never_moves_backwards(env):
    run = await env.register(at(12, 0))
    await env.flush(at(12, 5, 1))
    await env.flush(at(12, 3, 1))  # a clock step backwards
    assert (await env.run_row(run)).completed_through == at(12, 5)


async def test_a_clean_shutdown_sets_the_exact_instant(env):
    run = await env.register(at(12, 0))
    await env.flush(at(12, 4, 20), clean=True)
    row = await env.run_row(run)
    assert row.completed_through == at(12, 4, 20)
    assert row.clean_shutdown is True


# ── counts, gauges and event-time attribution ──────────────────────────


async def test_two_flushes_add_counts_and_keep_the_greater_gauge(env):
    await env.register(at(12, 0))
    acc = concurrency.counters()
    acc.record_request("overrun", at=at(12, 0, 5))
    acc.record_request("none", at=at(12, 0, 6))
    acc.gauge("pool_high_water", 5, at=at(12, 0, 7))
    await env.flush(at(12, 1, 1))
    acc.record_request("overrun", at=at(12, 0, 59))  # late arrival, same minute
    acc.gauge("pool_high_water", 3, at=at(12, 0, 58))
    await env.flush(at(12, 2, 1))
    rows = await env.buckets()
    assert rows[(at(12, 0), "requests")] == (3, None)
    assert rows[(at(12, 0), "transport_overrun")] == (2, None)
    assert rows[(at(12, 0), "pool_high_water")][1] == 5
    # And a third, idle flush changes nothing.
    await env.flush(at(12, 3, 1))
    assert await env.buckets() == rows


async def test_an_incident_keeps_its_event_time_minute(env):
    run = await env.register(at(11, 59))
    concurrency.counters().record("pool_checkout_timeout", at=at(12, 0, 50))
    await env.flush(at(12, 1, 10))
    assert await env.buckets() == {(at(12, 0), "pool_checkout_timeout"): (1, None)}
    assert (await env.run_row(run)).completed_through == at(12, 1)


async def test_a_failed_flush_re_merges_and_keeps_attribution(env):
    run = await env.register(at(11, 59))
    acc = concurrency.counters()
    acc.record("pool_checkout_timeout", at=at(12, 0, 30))
    acc.record_request("waited", at=at(12, 0, 40))

    class Boom(Exception):
        pass

    def failing_factory():
        class _Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def execute(self, *args, **kwargs):
                raise Boom("database went away")

        return _Session()

    assert await env.flush(at(12, 1, 10), factory=failing_factory) is False
    # Re-merged under the original keys, and the watermark did not move.
    snapshot = acc.drain()
    assert snapshot == {
        (at(12, 0), "pool_checkout_timeout"): (1, None),
        (at(12, 0), "requests"): (1, None),
        (at(12, 0), "transport_waited"): (1, None),
    }
    acc.merge_back(snapshot)
    assert (await env.run_row(run)).completed_through == at(11, 59)
    assert await env.buckets() == {}

    assert await env.flush(at(12, 2, 5))
    rows = await env.buckets()
    assert rows[(at(12, 0), "pool_checkout_timeout")] == (1, None)
    assert set(k[0] for k in rows) == {at(12, 0)}, "nothing re-attributed to 12:02"
    assert (await env.run_row(run)).completed_through == at(12, 2)


async def test_a_database_level_failure_rolls_back_and_re_merges(env):
    """A real server-side failure inside the transaction: the counter upsert
    runs, then the run-row upsert is refused by the mode CHECK. The whole
    transaction rolls back — no counter row survives — and the drained entries
    are merged back under their keys."""
    await env.register(at(11, 59), mode="rehearse")  # logged, not raised
    acc = concurrency.counters()
    acc.record("writer_overrun", at=at(12, 0, 1))
    env.db.statements.clear()
    assert await env.flush(at(12, 1, 1)) is False
    assert any("INSERT INTO concurrency_counters" in s for s in env.db.statements)
    assert await env.buckets() == {}
    assert acc.drain() == {(at(12, 0), "writer_overrun"): (1, None)}


async def test_an_out_of_range_gauge_cannot_wedge_the_flush(env):
    await env.register(at(11, 59))
    concurrency.counters().gauge("transport_wait_max_ms", 3_000_000_000, at=at(12, 0, 1))
    assert await env.flush(at(12, 1, 1))
    assert (await env.buckets())[(at(12, 0), "transport_wait_max_ms")][1] == 2**31 - 1


async def test_ten_thousand_requests_flush_as_one_statement(env):
    await env.register(at(11, 59))
    acc = concurrency.counters()
    outcomes = ("none", "pressured", "waited", "overrun", "refused")
    for i in range(10_000):
        acc.record_request(outcomes[i % 5], at=at(12, i % 7, i % 60))
    env.db.statements.clear()
    assert await env.flush(at(12, 8, 0))
    counter_inserts = [s for s in env.db.statements
                       if "INSERT INTO concurrency_counters" in s]
    assert len(counter_inserts) == 1, env.db.statements
    assert len(env.db.statements) <= 2, env.db.statements  # counters + run row
    rows = await env.buckets()
    assert sum(c for (_, m), (c, _v) in rows.items() if m == "requests") == 10_000
    assert sum(c for (_, m), (c, _v) in rows.items() if m == "transport_refused") == 2_000


async def test_rows_carry_the_runs_mode_and_epoch(env):
    await env.register(at(11, 59), mode="shadow", epoch="fedcba987654")
    concurrency.counters().record_request("pressured", at=at(12, 0, 1))
    await env.flush(at(12, 1, 1))
    async with env.db.maker() as session:
        pairs = (await session.execute(text(
            "SELECT DISTINCT mode, epoch FROM concurrency_counters"))).all()
    assert [tuple(p) for p in pairs] == [("shadow", "fedcba987654")]


# ── prune ───────────────────────────────────────────────────────────────


async def test_prune_removes_counters_and_runs_older_than_35_days(env):
    old = at(12, 0, day=1) - dt.timedelta(days=30)  # 2026-08-02
    await env.register(old)
    concurrency.counters().record("writer_refused", at=old)
    await env.flush(old + dt.timedelta(minutes=1), clean=True)
    kill()
    current = await env.register(at(12, 0))
    concurrency.counters().record("writer_refused", at=at(12, 0, 1))
    await env.flush(at(12, 1, 1))

    await cc.prune(now=at(12, 5), session_factory=env.db.maker)

    rows = await env.buckets()
    assert list(rows) == [(at(12, 0), "writer_refused")]
    async with env.db.maker() as session:
        runs = (await session.execute(text("SELECT run_id FROM concurrency_runs"))).all()
    assert [r.run_id for r in runs] == [current.run_id]


async def test_prune_never_deletes_the_current_run(env):
    old = at(12, 0) - dt.timedelta(days=40)
    current = await env.register(old)
    await cc.prune(now=at(12, 0), session_factory=env.db.maker)
    assert (await env.run_row(current)).run_id == current.run_id


# ── coverage ────────────────────────────────────────────────────────────


async def test_a_hard_kill_with_a_quick_restart_is_uncovered(env):
    """The spec scenario: flush at 12:00, overrun at 12:00:20, killed at
    12:00:40 with no shutdown flush, a same-settings run starts at 12:01."""
    await env.register(at(11, 50))
    await env.flush(at(12, 0, 5))  # completed_through = 12:00
    concurrency.counters().record_request("overrun", at=at(12, 0, 20))
    kill()  # the 12:00:20 overrun is lost with the process
    await env.register(at(12, 1))
    await env.flush(at(12, 5, 5))

    cov = await env.coverage(at(11, 55), at(12, 5))
    assert cov.uncovered == [(at(12, 0), at(12, 1))]
    assert cov.watermark == at(12, 5)
    assert await env.buckets() == {}, "the lost overrun was never durable"


async def test_a_clean_restart_is_covered(env):
    await env.register(at(11, 50))
    await env.flush(at(12, 0, 20), clean=True)
    kill()  # the process is gone, but it flushed at shutdown
    await env.register(at(12, 1, 0))
    await env.flush(at(12, 5, 5))

    cov = await env.coverage(at(11, 50), at(12, 5))
    assert cov.uncovered == []
    assert cov.watermark == at(12, 5)


async def test_two_runs_in_one_minute_share_the_bucket(env):
    """Run A shuts down cleanly at 12:00:20; run B (same epoch and mode)
    starts at 12:00:40 and times out a checkout at 12:00:50. Coverage is
    continuous; B's incident is in bucket 12:00, which is why the evaluator
    rounds an end at A's shutdown down to 12:00 (S4)."""
    await env.register(at(11, 50))
    await env.flush(at(12, 0, 20), clean=True)
    kill()
    await env.register(at(12, 0, 40))
    concurrency.counters().record("pool_checkout_timeout", at=at(12, 0, 50))
    await env.flush(at(12, 2, 1))

    assert (await env.coverage(at(11, 50), at(12, 2))).uncovered == []
    assert await env.buckets() == {(at(12, 0), "pool_checkout_timeout"): (1, None)}


async def test_the_tail_after_the_watermark_is_uncovered(env):
    await env.register(at(12, 0))
    await env.flush(at(12, 3, 30))
    cov = await env.coverage(at(12, 0), at(12, 6))
    assert cov.watermark == at(12, 3)
    assert cov.uncovered == [(at(12, 3), at(12, 6))]


async def test_a_lossy_run_covers_nothing(env):
    await env.register(at(12, 0))
    acc = concurrency.counters()
    for minute in range(concurrency.MAX_UNFLUSHED_MINUTES + 1):
        acc.record_request("none", at=at(12, 0) + dt.timedelta(minutes=minute))
    assert acc.lossy
    await env.flush(at(13, 5, 1))
    cov = await env.coverage(at(12, 0), at(13, 5))
    assert cov.uncovered == [(at(12, 0), at(13, 5))]


async def test_a_run_in_another_mode_or_epoch_is_not_coverage(env):
    await env.register(at(12, 0), mode="queue")
    await env.flush(at(12, 10, 0), clean=True)
    kill()
    await env.register(at(12, 10, 30), mode="shadow")
    await env.flush(at(12, 20, 0), clean=True)
    kill()
    await env.register(at(12, 20, 30), mode="queue", epoch="ffffffffffff")
    await env.flush(at(12, 30, 0))

    cov = await env.coverage(at(12, 0), at(12, 30), mode="queue")
    assert cov.watermark == at(12, 10, 0)
    # The clean gaps are covered; the shadow run and the other-epoch run are not.
    assert cov.uncovered == [(at(12, 10, 30), at(12, 20)), (at(12, 20, 30), at(12, 30))]
