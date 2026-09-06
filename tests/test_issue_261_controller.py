"""Deterministic lease ownership, atomicity, shadow and overflow regressions."""
import asyncio

import pytest

from src.config import Settings
from src.services.concurrency import Controller, TOOL_CLASSES, shadow_metadata


def make(**kw):
    return Controller(Settings(_env_file=None, secret_key="issue-261-test-secret-only-0123456789abcdef", mcp_concurrency_mode="enforce", **kw))


@pytest.mark.asyncio
async def test_atomic_class_admission_and_shadow_neutrality():
    c = make()
    a = await c.tool("semantic_search", 1, ("oauth", "grant"))
    b = await c.tool("semantic_search", 2, ("api_key", 2))
    assert not b.admitted and b.pressure.scope == "embedding"
    assert c.tools.active == 1
    v = await c.tool("find_related", 2, ("api_key", 2))
    assert v.admitted
    a.lease.release(); a.lease.release(); v.lease.release()
    assert c.tools.active == 0 and not c.tenants.entries and not c.principals.entries
    c = Controller(Settings(_env_file=None, secret_key="issue-261-test-secret-only-0123456789abcdef"))
    calls = [await c.tool("semantic_search", 1, ("oauth", "g")) for _ in range(8)]
    assert all(x.admitted and x.queue_ms == 0 for x in calls)
    assert calls[-1].shadow["basis"] == "observed_occupancy_zero_wait"
    assert not c.pending and c.tools.active == 8
    for a in calls: a.lease.release()


@pytest.mark.asyncio
async def test_shared_tenant_and_principal_across_classes():
    c = make()
    a = await c.tool("semantic_search", 1, ("oauth", "g"))
    b = await c.tool("find_related", 1, ("oauth", "g"))
    denied = await c.tool("read_note", 1, ("oauth", "g"))
    assert denied.pressure.scope == "principal"
    third = await c.tool("read_note", 1, ("api_key", 2))
    fourth = await c.tool("edit_note", 1, ("api_key", 3))
    assert not fourth.admitted and fourth.pressure.scope == "tenant"
    other = await c.tool("edit_note", 2, ("api_key", 4))
    assert other.admitted and c.tools.active == 4
    for item in (a,b,third,other):item.lease.release()


@pytest.mark.asyncio
async def test_eligible_fifo_and_cancellation_after_grant():
    c = make(mcp_concurrency_wait_seconds=1)
    first = await c.tool("semantic_search", 1, ("api_key", 1))
    wait = asyncio.create_task(c.tool("semantic_search", 2, ("api_key", 2)))
    await asyncio.sleep(0)
    assert len(c.pending) == 1 and c.tools.active == 1
    vector = await c.tool("find_related", 3, ("api_key", 3))
    assert vector.admitted
    first.lease.release()  # ownership transfers before task wakes
    assert c.tools.active == 2
    wait.cancel()
    with pytest.raises(asyncio.CancelledError):await wait
    assert c.tools.active == 1 and not c.pending
    vector.lease.release()
    assert not c.tenants.entries


@pytest.mark.asyncio
async def test_one_deadline_queue_bound_and_cancel_cleanup():
    c = make(mcp_concurrency_wait_seconds=.03,
             mcp_concurrency_waiters=1, mcp_concurrency_tenant_waiters=1,
             mcp_concurrency_principal_waiters=1)
    first = await c.tool("semantic_search", 1, ("api_key", 1))
    wait = asyncio.create_task(c.tool("semantic_search", 2, ("api_key", 2)))
    await asyncio.sleep(0)
    refused = await c.tool("semantic_search", 3, ("api_key", 3))
    assert refused.pressure.scope == "global_waiters" and refused.queue_ms == 0
    expired = await wait
    assert not expired.admitted and expired.queue_ms >= 15
    assert not c.pending and c.tools.waiting == 0
    first.lease.release()
    assert not c.tenants.entries


@pytest.mark.asyncio
async def test_sticky_overflow_for_active_and_queued_tool_owners():
    c = make(mcp_concurrency_registry_size=1, mcp_concurrency_wait_seconds=1)
    dedicated = await c.tool("semantic_search", 1, ("api_key", 1))
    overflow = await c.tool("find_related", 2, ("api_key", 2))
    waiter = asyncio.create_task(c.tool("find_related", 2, ("api_key", 2)))
    await asyncio.sleep(0)
    dedicated.lease.release()  # frees dedicated capacity but epoch remains
    same = await c.tool("read_note", 2, ("api_key", 2))
    assert same.admitted and not c.tenants.entries
    assert c.tenants.overflow.active == 2 and c.tenants.overflow.waiting == 1
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):await waiter
    same.lease.release(); overflow.lease.release()
    assert c.tenants.overflow.refs == 0
    next_epoch = await c.tool("read_note", 2, ("api_key", 2))
    assert 2 in c.tenants.entries
    next_epoch.lease.release()


def test_request_overflow_is_sticky_and_auth_has_separate_lifetime():
    c = make(mcp_concurrency_registry_size=1, mcp_concurrency_fingerprint=1)
    first = c.request("dedicated")
    second = c.request("overflow")
    first.lease.release()
    denied = c.request("overflow")
    assert not denied.admitted and denied.pressure.scope == "fingerprint"
    assert not c.fingerprints.entries
    a, b, excess = c.auth(), c.auth(), c.auth()
    assert a.admitted and b.admitted and not excess.admitted
    a.lease.release(); b.lease.release()
    assert c.requests.active == 1
    second.lease.release()
    fresh = c.request("overflow")
    assert "overflow" in c.fingerprints.entries
    fresh.lease.release()


@pytest.mark.asyncio
async def test_writer_bound_shutdown_and_shadow_no_drop():
    c = make(mcp_concurrency_writer_waiters=1, mcp_concurrency_writer_wait_seconds=.03)
    writer = await c.writer()
    pending = asyncio.create_task(c.writer())
    await asyncio.sleep(0)
    denied = await c.writer()
    assert not denied.admitted and denied.pressure.scope == "writer_waiters"
    assert not (await pending).admitted
    c.shutdown()
    writer.lease.release()
    final_flush = await c.writer()
    assert final_flush.admitted
    final_flush.lease.release()
    c.shutdown(close_writers=True)
    assert not (await c.writer()).admitted
    c = Controller(Settings(_env_file=None, secret_key="issue-261-test-secret-only-0123456789abcdef"))
    a,b = await c.writer(),await c.writer()
    assert b.admitted and b.shadow and b.queue_ms == 0
    a.lease.release();b.lease.release()


@pytest.mark.asyncio
async def test_shutdown_wakes_queued_without_grants_and_off_exemption():
    c = make(mcp_concurrency_wait_seconds=1)
    first = await c.tool("semantic_search",1,1)
    waiter = asyncio.create_task(c.tool("semantic_search",2,2))
    await asyncio.sleep(0)
    c.shutdown()
    result = await waiter
    assert not result.admitted and result.pressure.scope == "shutdown"
    first.lease.release()
    assert not c.pending and not c.tenants.entries
    c = Controller(Settings(_env_file=None, secret_key="issue-261-test-secret-only-0123456789abcdef", mcp_concurrency_mode="off"))
    for _ in range(50):
        assert (await c.tool("semantic_search",1,1)).admitted
    assert c.tools.active == 0
    assert (await make().tool("internal",None,None,resource_class="other")).admitted
    with pytest.raises(ValueError):await make().tool("unregistered",1,1)
    assert len(TOOL_CLASSES) == 25
    assert shadow_metadata(()) is None
