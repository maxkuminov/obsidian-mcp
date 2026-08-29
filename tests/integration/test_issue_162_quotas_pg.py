"""Real-Postgres gate for the daily quota and the filtered usage views (#162).

These properties have no meaningful fake-session equivalent, because what they
assert *is* the database's behaviour:

* **The concurrent boundary.** With a limit of N and more than N genuinely
  concurrent calls, exactly N tool bodies run. That is a fact about how
  PostgreSQL evaluates `ON CONFLICT ... DO UPDATE ... WHERE` while holding the
  conflicting row's lock, and it is the entire reason the counter table exists
  rather than a COUNT over `usage_logs`. A test that serialises the calls
  proves nothing; this one releases every task from one barrier onto a pool
  wide enough to hold them all.
* **Refusals do not consume.** The guarded UPDATE declining is what makes an
  agent looping on a refusal unable to push the number past the ceiling — and
  what makes exactly `limit` new calls admissible after the day rolls over.
* **The UTC boundary and the enable-reset rule**, both of which are about which
  row exists and when it is deleted.
* **The usage page's filter composition**, including a row whose credential has
  been deleted: it must still appear under its denormalised label (#77).
* **Over-quota rows reaching #160's refusal counts** through the shared
  predicate, which is a JSONB cast the aggregate performs.

Skipped unless `PGVECTOR_TEST_ADMIN_URL` names a throwaway Postgres *server*
(the harness creates and drops its own database):

    docker run --rm -d --name pgvector-test -e POSTGRES_PASSWORD=test \\
        -p 55432:5432 pgvector/pgvector:pg16
    PGVECTOR_TEST_ADMIN_URL=postgresql+asyncpg://postgres:test@localhost:55432/postgres \\
        pytest -q tests/integration/test_issue_162_quotas_pg.py
    docker rm -f pgvector-test
"""
import asyncio
import datetime as dt

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import _harness
import src.mcp_server.auth as mcp_auth
import src.mcp_server.tools as tools
import src.services.quotas as quotas
from src.models.db import APIKey, QuotaCounter, UsageLog, User
from src.services.usage_filters import (
    Filters,
    actor_totals,
    chart_series,
    filter_options,
    recent_logs,
    resolve_filters,
)
from src.services.usage_stats import tool_aggregates

DIM = 64

pytestmark = [
    pytest.mark.asyncio(loop_scope="module"),
    _harness.requires_pgvector,
]


@pytest.fixture(scope="module")
def migrated_url():
    yield from _harness.throwaway_database("quotas_162", DIM)


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def sessionmaker(migrated_url):
    # A pool wide enough to hold every task of the concurrency case at once.
    # With the default five, twenty "concurrent" admissions would queue behind
    # each other in the pool and the test would pass without ever exercising
    # the property it exists to check.
    engine = create_async_engine(migrated_url, pool_size=40, max_overflow=10)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest_asyncio.fixture(loop_scope="module")
async def clean(sessionmaker):
    """Empty everything these cases write, before and after each."""

    async def wipe():
        async with sessionmaker() as session:
            await session.execute(delete(QuotaCounter))
            await session.execute(delete(UsageLog))
            await session.execute(delete(APIKey))
            await session.execute(delete(User))
            await session.commit()

    await wipe()
    yield sessionmaker
    await wipe()


@pytest_asyncio.fixture(loop_scope="module")
async def quota_db(clean, monkeypatch):
    """Point `quotas.admit` at the throwaway database for the duration.

    The admission opens its **own** session — that is the design, so the tool
    body never waits on it — which means redirecting it is redirecting
    `quotas.async_session` and nothing else.
    """
    monkeypatch.setattr(quotas, "async_session", clean)
    return clean


async def make_key(sessionmaker, name="agent", prefix="omcp_000001", limit=None,
                   user_id=None):
    async with sessionmaker() as session:
        key = APIKey(
            name=name,
            key_hash=f"hash-{name}",
            key_prefix=prefix,
            permission="read",
            is_active=True,
            user_id=user_id,
            daily_request_limit=limit,
        )
        session.add(key)
        await session.commit()
        return key.id


async def make_user(sessionmaker, username):
    async with sessionmaker() as session:
        user = User(
            username=username,
            password_hash="x",
            is_admin=False,
            is_active=True,
        )
        session.add(user)
        await session.commit()
        return user.id


async def counter_for(sessionmaker, key_id, day=None):
    async with sessionmaker() as session:
        return (
            await session.execute(
                text(
                    "SELECT count FROM quota_counters "
                    "WHERE key_id = :k AND day = :d"
                ),
                {"k": key_id, "d": day or quotas.utc_day()},
            )
        ).scalar()


# --------------------------------------------------------------------------
# 1. the boundary, sequentially
# --------------------------------------------------------------------------


async def test_exactly_the_limit_is_admitted_and_the_next_call_is_refused(quota_db):
    key_id = await make_key(quota_db, limit=3)

    admitted = [await quotas.admit(key_id, 3) for _ in range(5)]

    assert admitted == [1, 2, 3, None, None]
    assert await counter_for(quota_db, key_id) == 3


async def test_refusals_never_increment_the_counter(quota_db):
    """An agent looping on the refusal cannot push the number past the
    ceiling — the guarded UPDATE declines, so the row is left where it was."""
    key_id = await make_key(quota_db, limit=2)

    for _ in range(2):
        assert await quotas.admit(key_id, 2) is not None
    for _ in range(50):
        assert await quotas.admit(key_id, 2) is None

    assert await counter_for(quota_db, key_id) == 2


async def test_a_key_with_a_limit_does_not_affect_another_key(quota_db):
    exhausted = await make_key(quota_db, name="loud", prefix="omcp_000002", limit=1)
    other = await make_key(quota_db, name="quiet", prefix="omcp_000003", limit=1)

    assert await quotas.admit(exhausted, 1) == 1
    assert await quotas.admit(exhausted, 1) is None
    # The other key's ceiling is its own.
    assert await quotas.admit(other, 1) == 1
    assert await counter_for(quota_db, other) == 1


# --------------------------------------------------------------------------
# 2. the concurrent boundary — the reason the counter table exists
# --------------------------------------------------------------------------


async def test_exactly_n_tool_bodies_run_under_more_than_n_concurrent_calls(
    quota_db, monkeypatch
):
    """The normative scenario, at the tool layer and against real Postgres.

    Twenty calls, a limit of seven, every task released from one barrier onto a
    connection pool wide enough to hold them all. A COUNT-then-decide gate
    passes the sequential boundary test above and fails this one: several
    tasks read "six used" at once and all of them run.

    What is counted is **tool body executions**, not admissions — the thing a
    quota exists to bound is work performed, and an increment that admits calls
    the body then also runs is not a quota.
    """
    limit = 7
    callers = 20
    key_id = await make_key(quota_db, name="swarm", prefix="omcp_000004", limit=limit)

    bodies = 0
    released = asyncio.Event()
    logged = []

    async def fake_log_usage(tool, params, duration_ms, response_size):
        logged.append(params)

    monkeypatch.setattr(tools, "_log_usage", fake_log_usage)

    @tools._tracked("swarm_probe", [])
    async def probe() -> str:
        nonlocal bodies
        bodies += 1
        return "ran"

    async def one_call():
        limit_token = mcp_auth.current_daily_request_limit.set(limit)
        key_token = mcp_auth.current_api_key_id.set(key_id)
        try:
            await released.wait()
            return await probe()
        finally:
            mcp_auth.current_api_key_id.reset(key_token)
            mcp_auth.current_daily_request_limit.reset(limit_token)

    tasks = [asyncio.create_task(one_call()) for _ in range(callers)]
    # Let every task reach the barrier before any of them touches the database.
    await asyncio.sleep(0.05)
    released.set()
    results = await asyncio.gather(*tasks)

    assert bodies == limit, f"{bodies} tool bodies ran against a limit of {limit}"
    assert sum(1 for r in results if r == "ran") == limit
    assert sum(1 for r in results if "daily request limit" in r) == callers - limit
    assert await counter_for(quota_db, key_id) == limit
    # Every refusal is logged with the marker, and no admitted call carries it.
    assert sum(1 for p in logged if p.get("over_quota") is True) == callers - limit
    assert len(logged) == callers


# --------------------------------------------------------------------------
# 3. the UTC day boundary
# --------------------------------------------------------------------------


async def test_the_day_rolls_over_and_exactly_limit_many_are_admitted_again(quota_db):
    """After a day's worth of refusals, the next UTC day admits exactly the
    limit again — not fewer (the refusals did not carry over) and not more."""
    key_id = await make_key(quota_db, name="daily", prefix="omcp_000005", limit=4)
    today = dt.datetime.now(dt.timezone.utc)
    tomorrow = today + dt.timedelta(days=1)

    for _ in range(4):
        assert await quotas.admit(key_id, 4, today) is not None
    for _ in range(10):
        assert await quotas.admit(key_id, 4, today) is None

    admitted_tomorrow = [await quotas.admit(key_id, 4, tomorrow) for _ in range(6)]
    assert admitted_tomorrow == [1, 2, 3, 4, None, None]

    # Two separate rows, and yesterday's is untouched by today's traffic.
    assert await counter_for(quota_db, key_id, quotas.utc_day(today)) == 4
    assert await counter_for(quota_db, key_id, quotas.utc_day(tomorrow)) == 4


async def test_the_day_is_utc_not_the_servers_timezone(quota_db):
    """23:30 in a +02:00 zone is already the next UTC day, and the counter must
    agree — a limit that resets at an hour nobody administering it can name is
    not a limit anybody can reason about."""
    key_id = await make_key(quota_db, name="tz", prefix="omcp_000006", limit=9)
    plus_two = dt.timezone(dt.timedelta(hours=2))

    await quotas.admit(key_id, 9, dt.datetime(2026, 8, 30, 1, 30, tzinfo=plus_two))
    await quotas.admit(key_id, 9, dt.datetime(2026, 8, 29, 23, 30, tzinfo=plus_two))

    # Both instants are 2026-08-29 in UTC, so they share one row.
    assert await counter_for(quota_db, key_id, dt.date(2026, 8, 29)) == 2
    assert await counter_for(quota_db, key_id, dt.date(2026, 8, 30)) is None


async def test_rows_older_than_two_days_are_pruned_on_the_next_days_first_call(
    quota_db,
):
    key_id = await make_key(quota_db, name="prune", prefix="omcp_000007", limit=5)
    today = dt.datetime.now(dt.timezone.utc)

    async with quota_db() as session:
        for age in (10, 5, 3, 1):
            session.add(
                QuotaCounter(
                    key_id=key_id, day=(today - dt.timedelta(days=age)).date(), count=1
                )
            )
        await session.commit()

    # The first admission of *today* is the INSERT branch, which is what
    # triggers the prune.
    assert await quotas.admit(key_id, 5, today) == 1

    async with quota_db() as session:
        remaining = sorted(
            (await session.execute(select(QuotaCounter.day))).scalars().all()
        )
    assert remaining == [
        (today - dt.timedelta(days=1)).date(),
        today.date(),
    ], remaining


# --------------------------------------------------------------------------
# 4. pre-body ordering and body failure, end to end
# --------------------------------------------------------------------------


async def test_a_pre_body_refusal_consumes_nothing_and_an_admitted_failure_consumes_one(
    quota_db, monkeypatch
):
    """The normative scenario, against the real counter: the call refused by an
    earlier gate leaves the row absent, and the admitted call whose body raises
    leaves it at exactly one."""
    key_id = await make_key(quota_db, name="order", prefix="omcp_000008", limit=10)
    monkeypatch.setattr(tools, "_log_usage", _noop_log)

    @tools._tracked("order_probe", [])
    async def probe() -> str:
        raise RuntimeError("the body failed after being admitted")

    async def call():
        limit_token = mcp_auth.current_daily_request_limit.set(10)
        key_token = mcp_auth.current_api_key_id.set(key_id)
        try:
            return await probe()
        finally:
            mcp_auth.current_api_key_id.reset(key_token)
            mcp_auth.current_daily_request_limit.reset(limit_token)

    # (a) an earlier gate refuses: no row at all.
    monkeypatch.setattr(
        tools, "_vault_admission_error", lambda: tools._NO_VAULT_MESSAGE
    )
    assert await call() == tools._NO_VAULT_MESSAGE
    assert await counter_for(quota_db, key_id) is None

    # (b) admitted, then the body raises: exactly one consumed, never returned.
    monkeypatch.setattr(tools, "_vault_admission_error", lambda: None)
    with pytest.raises(RuntimeError):
        await call()
    assert await counter_for(quota_db, key_id) == 1


async def _noop_log(tool, params, duration_ms, response_size):
    return None


# --------------------------------------------------------------------------
# 5. enable resets, change keeps — through the panel's own route
# --------------------------------------------------------------------------


async def test_enabling_a_limit_resets_the_day_and_changing_one_keeps_it(quota_db):
    """The panel route is what implements this, so the route is what is
    exercised — including that the counter delete and the limit write land in
    one transaction."""
    import src.control_panel.routes as panel
    from types import SimpleNamespace

    key_id = await make_key(quota_db, name="lifecycle", prefix="omcp_000009", limit=None)

    # 40 admissions while unlimited would not exist — an unlimited key does no
    # accounting — so the honest fixture is a row from an *earlier* limit that
    # was since cleared, which is exactly the "clears it, re-enables an hour
    # later" scenario.
    async with quota_db() as session:
        session.add(QuotaCounter(key_id=key_id, day=quotas.utc_day(), count=40))
        await session.commit()

    admin = SimpleNamespace(id=1, is_admin=True, username="admin")
    request = SimpleNamespace(session={})

    async with quota_db() as session:
        await panel.set_key_limit_form(
            request=request,
            key_id=key_id,
            daily_request_limit="100",
            session=session,
            user=admin,
        )
    assert await counter_for(quota_db, key_id) is None, "enabling did not reset"
    async with quota_db() as session:
        key = (await session.execute(select(APIKey).where(APIKey.id == key_id))).scalar_one()
        assert key.daily_request_limit == 100

    # Consume some, then raise the limit: the count survives, because those
    # calls really were admitted under a quota.
    for _ in range(3):
        await quotas.admit(key_id, 100)
    assert await counter_for(quota_db, key_id) == 3

    async with quota_db() as session:
        await panel.set_key_limit_form(
            request=request,
            key_id=key_id,
            daily_request_limit="200",
            session=session,
            user=admin,
        )
    assert await counter_for(quota_db, key_id) == 3, "a change must not reset"
    async with quota_db() as session:
        key = (await session.execute(select(APIKey).where(APIKey.id == key_id))).scalar_one()
        assert key.daily_request_limit == 200

    # Clearing returns the key to unlimited and stops the accounting.
    async with quota_db() as session:
        await panel.set_key_limit_form(
            request=request,
            key_id=key_id,
            daily_request_limit="",
            session=session,
            user=admin,
        )
    async with quota_db() as session:
        key = (await session.execute(select(APIKey).where(APIKey.id == key_id))).scalar_one()
        assert key.daily_request_limit is None


async def test_an_invalid_limit_from_the_panel_changes_nothing(quota_db):
    """Rejected above the CHECK so the operator sees a sentence, and the stored
    value is untouched — not a 500 with a half-applied change."""
    import src.control_panel.routes as panel
    from types import SimpleNamespace

    key_id = await make_key(quota_db, name="invalid", prefix="omcp_000010", limit=50)
    request = SimpleNamespace(session={})
    admin = SimpleNamespace(id=1, is_admin=True, username="admin")

    for bad in ("0", "-5", "1000001", "1oo"):
        async with quota_db() as session:
            await panel.set_key_limit_form(
                request=request,
                key_id=key_id,
                daily_request_limit=bad,
                session=session,
                user=admin,
            )
        assert request.session.get("flash_key_error")
        request.session.clear()
        async with quota_db() as session:
            key = (
                await session.execute(select(APIKey).where(APIKey.id == key_id))
            ).scalar_one()
            assert key.daily_request_limit == 50, bad


async def test_deleting_a_key_takes_its_counters_with_it(quota_db):
    key_id = await make_key(quota_db, name="doomed", prefix="omcp_000011", limit=5)
    await quotas.admit(key_id, 5)
    assert await counter_for(quota_db, key_id) == 1

    async with quota_db() as session:
        # The panel's own sequence: NULL the log's FK, then delete the key.
        await session.execute(
            text("UPDATE usage_logs SET key_id = NULL WHERE key_id = :k"),
            {"k": key_id},
        )
        await session.execute(delete(APIKey).where(APIKey.id == key_id))
        await session.commit()

    async with quota_db() as session:
        left = (
            await session.execute(
                text("SELECT count(*) FROM quota_counters WHERE key_id = :k"),
                {"k": key_id},
            )
        ).scalar()
    assert left == 0


# --------------------------------------------------------------------------
# 6. over-quota rows reach #160's refusal counts
# --------------------------------------------------------------------------


async def test_over_quota_rows_are_counted_as_refusals_and_kept_out_of_the_percentiles(
    clean,
):
    """The two halves of the shared predicate must partition the window: an
    over-quota row is a refusal, an ordinary row is executed, and a row
    carrying some *other* `params.error` value stays in the aggregates."""
    async with clean() as session:
        session.add_all([
            UsageLog(tool="read_note", params={"over_quota": True},
                     duration_ms=0, response_size=180),
            UsageLog(tool="read_note", params={"over_quota": True},
                     duration_ms=1, response_size=180),
            UsageLog(tool="read_note", params={"path": "a.md"},
                     duration_ms=400, response_size=900),
            UsageLog(tool="read_note", params={"error": "something_else"},
                     duration_ms=600, response_size=40),
        ])
        await session.commit()

        rows = await tool_aggregates(session, "24h", None)

    by_tool = {r["tool"]: r for r in rows}
    assert by_tool["read_note"]["refusals"] == 2
    # The `something_else` row's body ran — a broad "params carries an error"
    # match would have hidden it, and it is the slow one.
    assert by_tool["read_note"]["executed"] == 2
    assert by_tool["read_note"]["p95"] is not None
    assert by_tool["read_note"]["max_size"] == 900


# --------------------------------------------------------------------------
# 7. the usage page's filters
# --------------------------------------------------------------------------


async def test_filters_compose_across_chart_log_and_totals(clean):
    alice = await make_user(clean, "alice")
    bob = await make_user(clean, "bob")
    alice_key = await make_key(clean, "alice key", "omcp_a00001", user_id=alice)
    bob_key = await make_key(clean, "bob key", "omcp_b00001", user_id=bob)

    async with clean() as session:
        session.add_all([
            UsageLog(tool="read_note", user_id=alice, key_id=alice_key,
                     actor_kind="api_key", actor_label="alice key",
                     actor_ref="omcp_a00001", duration_ms=10),
            UsageLog(tool="read_note", user_id=alice, key_id=alice_key,
                     actor_kind="api_key", actor_label="alice key",
                     actor_ref="omcp_a00001", duration_ms=11),
            UsageLog(tool="semantic_search", user_id=alice, key_id=alice_key,
                     actor_kind="api_key", actor_label="alice key",
                     actor_ref="omcp_a00001", duration_ms=12),
            UsageLog(tool="read_note", user_id=bob, key_id=bob_key,
                     actor_kind="api_key", actor_label="bob key",
                     actor_ref="omcp_b00001", duration_ms=13),
        ])
        await session.commit()

        options = await filter_options(session, "24h", None)
        assert {u["label"] for u in options["users"]} == {"alice", "bob"}
        assert {k["label"] for k in options["keys"]} == {"alice key", "bob key"}
        assert set(options["tools"]) == {"read_note", "semantic_search"}

        # Unfiltered: everything.
        every = resolve_filters("24h", None, None, None, None, options)
        assert len(await recent_logs(session, every)) == 4
        assert sum(r.requests for r in await actor_totals(session, every)) == 4
        assert sum((await chart_series(session, every))["values"]) == 4

        # One key.
        by_key = resolve_filters("24h", None, str(alice_key), None, None, options)
        assert len(await recent_logs(session, by_key)) == 3
        totals = await actor_totals(session, by_key)
        assert [r.actor_label for r in totals] == ["alice key"]
        assert totals[0].requests == 3
        assert sum((await chart_series(session, by_key))["values"]) == 3

        # Key *and* tool — the filters compose rather than replacing each other.
        both = resolve_filters("24h", None, str(alice_key), "read_note", None, options)
        assert len(await recent_logs(session, both)) == 2
        assert sum((await chart_series(session, both))["values"]) == 2

        # User, for an admin.
        by_user = resolve_filters("24h", str(bob), None, None, None, options)
        assert [r.actor_label for r in await actor_totals(session, by_user)] == [
            "bob key"
        ]


async def test_a_deleted_credentials_history_still_names_its_actor(clean):
    """The row an operator opens this page to read: the credential is gone, so
    the LEFT JOIN answers nothing, and the denormalised label written at call
    time is the whole content of the line (#77)."""
    key_id = await make_key(clean, "since deleted", "omcp_c00001")
    async with clean() as session:
        session.add(
            UsageLog(tool="read_note", key_id=key_id, actor_kind="api_key",
                     actor_label="since deleted", actor_ref="omcp_c00001",
                     duration_ms=5)
        )
        await session.commit()
        # The panel's own delete sequence.
        await session.execute(
            text("UPDATE usage_logs SET key_id = NULL WHERE key_id = :k"),
            {"k": key_id},
        )
        await session.execute(delete(APIKey).where(APIKey.id == key_id))
        await session.commit()

        options = await filter_options(session, "24h", None)
        assert options["keys"] == [], "the deleted key is not offered as a filter"

        unfiltered = resolve_filters("24h", None, None, None, None, options)
        totals = await actor_totals(session, unfiltered)
        assert [(r.actor_label, r.requests) for r in totals] == [("since deleted", 1)]
        logs = await recent_logs(session, unfiltered)
        assert logs[0].actor_label == "since deleted"
        assert logs[0].api_key_name is None


async def test_an_unknown_filter_value_falls_back_to_the_wider_view(clean):
    """A stale bookmark renders the page rather than 422ing, and the fallback
    is always *less* specific — never a value passed through to SQL."""
    async with clean() as session:
        options = await filter_options(session, "24h", None)

    resolved = resolve_filters("nonsense", "999", "999", "no_such_tool", None, options)
    assert resolved.window == "24h"
    assert (resolved.user_id, resolved.key_id, resolved.tool) == (None, None, None)


async def test_a_scoped_viewer_cannot_widen_with_a_user_filter(clean):
    alice = await make_user(clean, "alice2")
    bob = await make_user(clean, "bob2")
    alice_key = await make_key(clean, "alice2 key", "omcp_a00002", user_id=alice)
    bob_key = await make_key(clean, "bob2 key", "omcp_b00002", user_id=bob)

    async with clean() as session:
        session.add_all([
            UsageLog(tool="read_note", user_id=alice, key_id=alice_key, duration_ms=1),
            UsageLog(tool="read_note", user_id=bob, key_id=bob_key, duration_ms=2),
        ])
        await session.commit()

        options = await filter_options(session, "24h", alice)
        # No user selector is offered to a scoped viewer, and no other user's
        # keys appear in the one that is.
        assert options["users"] == []
        assert [k["label"] for k in options["keys"]] == ["alice2 key"]

        scoped = resolve_filters("24h", str(bob), None, None, alice, options)
        assert scoped.user_id is None
        logs = await recent_logs(session, scoped)
        assert len(logs) == 1
        assert sum(r.requests for r in await actor_totals(session, scoped)) == 1

        # Even naming the other user's key by id changes nothing: the scope
        # clause is applied on top of it.
        with_foreign_key = Filters("24h", None, bob_key, None, alice)
        assert await recent_logs(session, with_foreign_key) == []
