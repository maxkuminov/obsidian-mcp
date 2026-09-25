"""Queue mode (#188 design D7): waits like enforce, never refuses for capacity."""
import asyncio
import time

import pytest

from src.config import Settings
from src.services.concurrency import Controller, queue_metadata

SECRET = "issue-261-test-secret-only-0123456789abcdef"


def make(mode="queue", **kw):
    kw.setdefault("mcp_concurrency_wait_seconds", .03)
    kw.setdefault("mcp_concurrency_transport_wait_seconds", .03)
    return Controller(Settings(_env_file=None, secret_key=SECRET,
                               mcp_concurrency_mode=mode, **kw))


def assert_drained(c):
    assert not c.pending
    for counter in (c.requests, c.authentication, c.tools, c.writers, *c.classes.values()):
        assert counter.active == 0 and counter.waiting == 0
    for registry in (c.fingerprints, c.tenants, c.principals):
        assert not registry.entries and registry.overflow.refs == 0


@pytest.mark.asyncio
async def test_deadline_expiry_grants_with_overrun():
    c = make()
    first = await c.tool("semantic_search", 1, ("api_key", 1))
    second = await c.tool("semantic_search", 2, ("api_key", 2))
    assert second.admitted and second.overrun is not None
    assert second.overrun.scope == "embedding" and second.overrun.code == "slot_timeout"
    assert second.queue_ms >= 20
    # L1: queue bounds latency, not occupancy.
    assert c.classes["embedding"].active == 2 and c.tools.active == 2
    obs = second.observation
    assert obs.overrun and obs.waited_ms >= 20
    meta = queue_metadata((obs,))
    assert meta["overrun"] is True and meta["code"] == "slot_timeout"
    first.lease.release(); second.lease.release()
    assert_drained(c)


@pytest.mark.asyncio
async def test_waiter_overflow_grants_immediately_with_overrun():
    c = make(mcp_concurrency_wait_seconds=1, mcp_concurrency_waiters=1,
             mcp_concurrency_tenant_waiters=1, mcp_concurrency_principal_waiters=1)
    first = await c.tool("semantic_search", 1, ("api_key", 1))
    waiting = asyncio.create_task(c.tool("semantic_search", 2, ("api_key", 2)))
    await asyncio.sleep(0)
    started = time.monotonic()
    overflow = await c.tool("semantic_search", 3, ("api_key", 3))
    assert time.monotonic() - started < .1
    assert overflow.admitted and overflow.overrun.scope == "global_waiters"
    assert overflow.queue_ms == 0
    # The overrun lease occupies the class like any other (L1): the waiter is
    # granted only once both holders are gone.
    first.lease.release()
    await asyncio.sleep(0)
    assert not waiting.done()
    overflow.lease.release()
    granted = await waiting
    assert granted.admitted and granted.overrun is None
    granted.lease.release()
    assert_drained(c)


@pytest.mark.asyncio
async def test_zero_wait_grants_with_overrun():
    c = make(mcp_concurrency_wait_seconds=0, mcp_concurrency_transport_wait_seconds=0,
             mcp_concurrency_fingerprint=2, mcp_concurrency_principal=1,
             mcp_concurrency_principal_waiters=1)
    first = await c.tool("semantic_search", 1, ("api_key", 1))
    second = await c.tool("semantic_search", 2, ("api_key", 2))
    assert second.admitted and second.overrun.scope == "embedding" and second.queue_ms == 0
    requests = [await c.request("fp", c.transport_deadline(), asyncio.Event()) for _ in range(3)]
    assert all(r.admitted for r in requests)
    assert requests[-1].overrun.scope == "fingerprint"
    assert requests[-1].overrun.code == "request_concurrency_limited"
    for x in (first, second, *requests): x.lease.release()
    assert_drained(c)


@pytest.mark.asyncio
async def test_transport_and_writer_overrun():
    c = make(mcp_concurrency_writer_wait_seconds=.02)
    auths = [await c.auth(c.transport_deadline(), asyncio.Event()) for _ in range(3)]
    assert all(a.admitted for a in auths)
    assert auths[-1].overrun.code == "auth_concurrency_limited" and auths[-1].queue_ms >= 15
    writers = [await c.writer(), await c.writer()]
    assert writers[-1].admitted and writers[-1].overrun.code == "writer_concurrency_limited"
    for x in (*auths, *writers): x.lease.release()
    assert_drained(c)


@pytest.mark.asyncio
async def test_ordinary_wait_is_measured_and_sets_no_code():
    c = make(mcp_concurrency_wait_seconds=1)
    first = await c.tool("read_note", 1, ("api_key", 1))
    others = [await c.tool("read_note", 1, ("api_key", 1)) for _ in range(2)]
    assert all(x.overrun is None for x in others)  # principal 3 is now full
    loop = asyncio.get_running_loop()
    loop.call_later(.04, first.lease.release)
    waited = await c.tool("read_note", 1, ("api_key", 1))
    assert waited.admitted and waited.overrun is None
    assert waited.pressure.scope == "principal"
    assert 30 <= waited.queue_ms < 500
    obs = waited.observation
    assert not obs.overrun and obs.waited_ms >= 30
    meta = queue_metadata((obs,))
    assert meta["overrun"] is False and meta["code"] is None
    for x in (*others, waited): x.lease.release()
    assert_drained(c)


@pytest.mark.asyncio
async def test_shutdown_still_refuses_in_queue_mode():
    c = make(mcp_concurrency_wait_seconds=1)
    first = await c.tool("semantic_search", 1, 1)
    waiter = asyncio.create_task(c.tool("semantic_search", 2, 2))
    await asyncio.sleep(0)
    c.shutdown()
    result = await waiter
    assert not result.admitted and result.pressure.scope == "shutdown"
    assert not (await c.tool("read_note", 3, 3)).admitted
    assert not (await c.request("fp")).admitted
    first.lease.release()
    c.shutdown(close_writers=True)
    assert not (await c.writer()).admitted
    assert_drained(c)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["queue", "enforce"])
@pytest.mark.parametrize("timeout_first", [False, True])
async def test_grant_racing_the_timeout_is_granted_exactly_once(mode, timeout_first):
    c = make(mode, mcp_concurrency_wait_seconds=.02)
    holder = await c.tool("semantic_search", 1, ("api_key", 1))
    task = asyncio.create_task(c.tool("semantic_search", 2, ("api_key", 2)))
    await asyncio.sleep(0)
    assert len(c.pending) == 1
    # Block the loop past the deadline so the timeout and the release are
    # ready together; then order them both ways.
    time.sleep(.05)
    if timeout_first:
        await asyncio.sleep(0)  # the timeout handle runs; the task has not resumed
    holder.lease.release()     # _pump(): capacity is free, so it grants
    result = await task
    assert result.admitted and result.overrun is None
    assert c.classes["embedding"].active == 1 and c.tools.active == 1
    result.lease.release()
    result.lease.release()
    assert_drained(c)


@pytest.mark.asyncio
async def test_cancellation_racing_an_overrun_leaks_nothing():
    c = make(mcp_concurrency_wait_seconds=.02)
    holder = await c.tool("semantic_search", 1, ("api_key", 1))
    task = asyncio.create_task(c.tool("semantic_search", 2, ("api_key", 2)))
    await asyncio.sleep(.04)
    task.cancel()
    try:
        result = await task
    except asyncio.CancelledError:
        pass
    else:
        result.lease.release()
    holder.lease.release()
    assert_drained(c)
