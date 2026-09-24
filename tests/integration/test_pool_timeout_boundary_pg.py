"""Real pool checkout timeouts, counted at the shared boundary (#188, D10).

The engine's pool class (`src.database.CountingQueuePool`) is the one place a
pool-exhaustion failure is visible for every consumer. Each case here builds a
real engine of that class on a throwaway database with **one** connection and
a 0.2 s `pool_timeout`, holds the connection, and drives a production path
into a real `QueuePool limit … reached` timeout:

- MCP authentication (`APIKeyMiddleware`, the credential lookup session);
- the daily-quota gate (`quotas.admit`);
- the usage writer (`tools._insert_usage`);
- a panel route (`GET /admin/`, the dashboard, through the ASGI app);
- OAuth `/token` (through the ASGI app).

Each must count exactly one `pool_checkout_timeout` and surface the same
exception class it always did. A provider's `TimeoutError` /
`asyncio.TimeoutError`, and a timeout while *creating* a connection, must not
count. And the checkout high-water gauge must see concurrent checkouts.

Skipped unless `PGVECTOR_TEST_ADMIN_URL` names a throwaway Postgres server; run
it with `make test-integration`.
"""
from __future__ import annotations

import asyncio
import datetime as dt
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import exc as sa_exc
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import _harness
import src.database as database
from src.config import settings
from src.mcp_server import auth, tools
from src.services import concurrency, quotas, rate_limits

pytestmark = [_harness.requires_pgvector, pytest.mark.asyncio(loop_scope="module")]


@pytest.fixture(scope="module")
def migrated_url():
    yield from _harness.throwaway_database("pool_timeout", 64)


def timeouts() -> int:
    """Pending `pool_checkout_timeout` increments, left in the accumulator."""
    drained = concurrency.counters().drain()
    concurrency.counters().merge_back(drained)
    return sum(c for (_m, metric), (c, _v) in drained.items()
               if metric == "pool_checkout_timeout")


def high_water() -> int | None:
    drained = concurrency.counters().drain()
    concurrency.counters().merge_back(drained)
    values = [v for (_m, metric), (_c, v) in drained.items() if metric == "pool_high_water"]
    return max(values) if values else None


@pytest_asyncio.fixture(loop_scope="module")
async def tiny(migrated_url, monkeypatch):
    """One-connection engine of the production pool class, 0.2 s timeout,
    wired into every module that opens its own session."""
    engine = create_async_engine(
        migrated_url, poolclass=database.CountingQueuePool,
        pool_size=1, max_overflow=0, pool_timeout=0.2,
    )
    maker = async_sessionmaker(engine, expire_on_commit=False)
    from src.oauth import routes as oauth_routes
    for module in (database, auth, tools, quotas, oauth_routes):
        monkeypatch.setattr(module, "async_session", maker)
    monkeypatch.setattr(settings, "mcp_sandbox_mode", False)
    monkeypatch.setattr(auth.rate_limits, "check_auth_failures", lambda *_: None)
    rate_limits.reset_state_for_tests()
    concurrency.reset_counters()

    @asynccontextmanager
    async def held():
        conn = await engine.connect()
        try:
            yield
        finally:
            await conn.close()

    yield SimpleNamespace(engine=engine, maker=maker, held=held)
    await engine.dispose()
    concurrency.reset_counters()


def assert_pool_timeout(error: BaseException) -> None:
    assert type(error) is sa_exc.TimeoutError, type(error)
    assert "QueuePool limit" in str(error)
    assert getattr(error, database.CountingQueuePool._COUNTED) is True


async def test_a_direct_checkout_timeout_counts_once_and_reraises_unchanged(tiny):
    async with tiny.held():
        with pytest.raises(sa_exc.TimeoutError) as info:
            async with tiny.engine.connect():
                pass
    assert_pool_timeout(info.value)
    assert timeouts() == 1


async def test_mcp_authentication(tiny):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    async def app(*_):
        raise AssertionError("an unauthenticated request reached the app")

    scope = {
        "type": "http", "method": "POST", "path": "/mcp", "raw_path": b"/mcp",
        "query_string": b"", "scheme": "http", "server": ("test", 80),
        "client": ("127.0.0.9", 4000),
        "headers": [(b"authorization", b"Bearer omcp_pool_timeout_probe")],
    }
    async with tiny.held():
        with pytest.raises(sa_exc.TimeoutError) as info:
            await auth.APIKeyMiddleware(app)(scope, receive, send)
    assert_pool_timeout(info.value)
    assert timeouts() == 1


async def test_the_quota_gate(tiny):
    async with tiny.held():
        with pytest.raises(sa_exc.TimeoutError) as info:
            await quotas.admit(1, 10)
    assert_pool_timeout(info.value)
    assert timeouts() == 1


async def test_the_usage_writer(tiny):
    values = dict(key_id=None, oauth_token_id=None, user_id=None, tool="read_note",
                  params={}, duration_ms=1, response_size=0)
    async with tiny.held():
        with pytest.raises(sa_exc.TimeoutError) as info:
            await tools._insert_usage(values)
    assert_pool_timeout(info.value)
    assert timeouts() == 1


async def _through_the_app(method: str, path: str, form: dict | None = None):
    from src.main import app

    transport = httpx.ASGITransport(app=app, client=("127.0.0.10", 5000))
    host = (settings.allowed_hosts or ["localhost"])[0]
    async with httpx.AsyncClient(transport=transport, base_url=f"https://{host}") as client:
        return await client.request(method, path, data=form)


async def test_a_panel_route(tiny, monkeypatch):
    # Single-user mode: `require_user_panel` resolves the sentinel, and the
    # dashboard then runs its own queries on the route's `get_session`.
    monkeypatch.setattr(settings, "multi_user_mode", False)
    async with tiny.held():
        with pytest.raises(sa_exc.TimeoutError) as info:
            await _through_the_app("GET", "/admin/")
    assert_pool_timeout(info.value)
    assert timeouts() == 1


async def test_oauth_token(tiny):
    async with tiny.held():
        with pytest.raises(sa_exc.TimeoutError) as info:
            await _through_the_app("POST", "/token", {
                "grant_type": "authorization_code", "code": "no-such-code",
                "client_id": "no-such-client", "redirect_uri": "https://example.test/cb",
                "code_verifier": "v" * 43,
            })
    assert_pool_timeout(info.value)
    assert timeouts() == 1


async def test_every_timeout_counts_once(tiny):
    async with tiny.held():
        for _ in range(3):
            with pytest.raises(sa_exc.TimeoutError):
                async with tiny.maker() as session:
                    await session.execute(text("SELECT 1"))
    assert timeouts() == 3


async def test_an_unrelated_timeout_error_does_not_count(tiny):
    async def provider_call(kind):
        await asyncio.sleep(0)
        raise kind("embedding provider timed out")

    for kind in (TimeoutError, asyncio.TimeoutError):
        with pytest.raises(kind):
            async with tiny.maker() as session:
                await session.execute(text("SELECT 1"))  # a real checkout
                await provider_call(kind)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(asyncio.sleep(5), timeout=0.01)
    assert timeouts() == 0


async def test_a_timeout_while_creating_a_connection_does_not_count(tiny, monkeypatch):
    def slow_connect(*_a, **_kw):
        raise TimeoutError("connect timed out")

    await tiny.engine.dispose()  # no idle connection: the next checkout creates
    monkeypatch.setattr(tiny.engine.pool, "_create_connection", slow_connect)
    with pytest.raises(TimeoutError) as info:
        async with tiny.engine.connect():
            pass
    assert type(info.value) is TimeoutError
    assert timeouts() == 0


async def test_the_checkout_high_water_gauge(migrated_url):
    concurrency.reset_counters()
    engine = create_async_engine(migrated_url, poolclass=database.CountingQueuePool,
                                 pool_size=3, max_overflow=2, pool_timeout=2)
    try:
        conns = [await engine.connect() for _ in range(4)]
        for conn in conns:
            await conn.close()
        async with engine.connect():
            pass
        assert high_water() == 4
    finally:
        await engine.dispose()
        concurrency.reset_counters()


async def test_the_production_engine_uses_the_counting_pool():
    assert isinstance(database.engine.pool, database.CountingQueuePool)
    # The pool's recreate (dispose, a failover) keeps the class.
    assert isinstance(database.engine.pool.recreate(), database.CountingQueuePool)


async def test_a_timeout_is_attributed_to_its_minute(tiny):
    before = dt.datetime.now(dt.timezone.utc).replace(second=0, microsecond=0)
    async with tiny.held():
        with pytest.raises(sa_exc.TimeoutError):
            async with tiny.engine.connect():
                pass
    after = dt.datetime.now(dt.timezone.utc).replace(second=0, microsecond=0)
    drained = concurrency.counters().drain()
    minutes = {m for (m, metric) in drained if metric == "pool_checkout_timeout"}
    assert len(minutes) == 1 and before <= minutes.pop() <= after
