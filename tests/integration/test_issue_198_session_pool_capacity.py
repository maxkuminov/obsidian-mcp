"""#198 — the session touch must not take a second connection. Real pool.

`tests/test_issue_198_session_registry.py` has the unit expression of this
property: a fake session, a semaphore standing in for the pool, and a
monkeypatched `async_session` that raises if anything asks for a second lease.
That test is precise about *intent* and blind about *reality* — it proves the
code does not call one particular factory, not that a real
`AsyncEngine` survives the load.

This one uses the real thing. An engine with production's exact pool geometry
(`pool_size=5, max_overflow=10`), a deliberately short `pool_timeout` so a
regression fails in seconds rather than blocking the suite for thirty, and
**more concurrent stale-session validations than the pool can hold leases
for**. Every validation is stale, so every one of them issues the `last_seen_at`
touch; if the touch ever opens a session of its own, the requests need two
leases apiece, the pool cannot supply them, and the callers past the ceiling
raise `TimeoutError` instead of returning a user.

That is not a hypothetical failure mode. `get_session` yields one session per
request and the pool tops out at fifteen concurrent checkouts; a per-request
path that quietly takes a second halves the server's real capacity, and
everything else in the process — MCP tool calls, `/token`, the indexer — waits
`pool_timeout` and then 500s. A telemetry field must not be able to do that.

Skipped unless `PGVECTOR_TEST_ADMIN_URL` is set, like every other module here.
`make test-schema` runs it.
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime
import os

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import _harness

REQUIRED = os.environ.get("OMCP_REQUIRE_SCHEMA_INTEGRATION") == "1"
pytestmark = [] if REQUIRED else [_harness.requires_pgvector]

DIM = 1024

#: Production's geometry, from `src/database.py`. Not imported, so that a
#: change to those numbers shows up here as a deliberate edit rather than
#: silently re-tuning what this test is asserting.
POOL_SIZE = 5
MAX_OVERFLOW = 10
CAPACITY = POOL_SIZE + MAX_OVERFLOW

#: Comfortably past the ceiling. With one lease each these all complete; with
#: two they need fifty against a pool of fifteen.
CONCURRENCY = 25

#: Short on purpose. Production waits thirty seconds before giving up, which is
#: the right answer for a live request and the wrong one for a test — a
#: regression here should fail in seconds, not stall the gate.
POOL_TIMEOUT = 5


@contextlib.contextmanager
def _migrated_database():
    generator = _harness.throwaway_database("session_pool", DIM)
    url = next(generator)
    try:
        yield url
    finally:
        generator.close()


async def _capacity_run(url: str) -> list:
    from src.auth.session import SESSION_ID_KEY, hash_session_id

    import session_helpers as sh

    engine = create_async_engine(
        url,
        pool_size=POOL_SIZE,
        max_overflow=MAX_OVERFLOW,
        pool_timeout=POOL_TIMEOUT,
    )
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        # One user, and one **stale** session row per concurrent request, so
        # every request takes the touch branch rather than the throttled
        # short-circuit. Distinct rows, because a single row would serialize
        # them on a row lock and hide the pool behaviour entirely.
        stale = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
            hours=1
        )
        expires = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
            days=7
        )
        sids = [f"pool-capacity-session-{index}" for index in range(CONCURRENCY)]

        async with maker() as setup:
            await setup.execute(
                sa.text(
                    "INSERT INTO users (username, password_hash, is_admin, "
                    "is_active, session_version) VALUES "
                    "('poolcap', 'x', false, true, 1)"
                )
            )
            user_id = (
                await setup.execute(
                    sa.text("SELECT id FROM users WHERE username = 'poolcap'")
                )
            ).scalar_one()
            for sid in sids:
                await setup.execute(
                    sa.text(
                        "INSERT INTO user_sessions "
                        "(id, user_id, created_at, last_seen_at, expires_at) "
                        "VALUES (:id, :uid, :stale, :stale, :expires)"
                    ),
                    {
                        "id": hash_session_id(sid),
                        "uid": user_id,
                        "stale": stale,
                        "expires": expires,
                    },
                )
            await setup.commit()

        from src.auth.session import get_active_session_user

        async def one_request(sid: str):
            # One session per request, exactly as `get_session` yields one.
            async with maker() as session:
                request = sh.browser_request(
                    session={
                        "user_id": user_id,
                        "session_version": 1,
                        "is_admin": False,
                        "username": "poolcap",
                        SESSION_ID_KEY: sid,
                    }
                )
                return await get_active_session_user(request, session)

        return await asyncio.gather(*(one_request(sid) for sid in sids))
    finally:
        await engine.dispose()


def test_more_concurrent_stale_sessions_than_the_pool_holds_all_complete():
    """`CONCURRENCY` validations, each touching, against `CAPACITY` leases.

    A `TimeoutError` out of `asyncio.gather` here is the regression: something
    on the validation path took a second connection while the request's own was
    still checked out.
    """
    assert CONCURRENCY > CAPACITY, "the test must actually exceed the pool"

    with _migrated_database() as url:
        users = asyncio.run(_capacity_run(url))

    assert len(users) == CONCURRENCY
    assert all(user is not None for user in users), (
        "every request must have resolved its user; a None here means the "
        "validator refused rather than that the pool ran out"
    )
    assert {user.username for user in users} == {"poolcap"}


def test_the_touch_actually_wrote(tmp_path):
    """The companion assertion: the run above was not a no-op.

    If `last_seen_at` were left alone the capacity result would be vacuous —
    a validation that never issues the touch obviously needs one lease. So the
    same run is checked for the write it is supposed to have made.
    """
    from src.auth.session import hash_session_id

    with _migrated_database() as url:
        asyncio.run(_capacity_run(url))

        async def read_back():
            engine = create_async_engine(url, poolclass=None)
            try:
                async with engine.connect() as conn:
                    return (
                        await conn.execute(
                            sa.text(
                                "SELECT count(*) FROM user_sessions "
                                "WHERE last_seen_at > created_at"
                            )
                        )
                    ).scalar_one()
            finally:
                await engine.dispose()

        touched = asyncio.run(read_back())

    assert touched == CONCURRENCY, (
        f"only {touched} of {CONCURRENCY} rows had their last-seen advanced, so "
        "the capacity assertion above did not exercise the touch"
    )
    assert hash_session_id("x")  # the helper is the one used to seed the rows
