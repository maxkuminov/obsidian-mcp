"""Deterministic lease ownership, atomicity, shadow and overflow regressions.

Extended for #188 (`concurrency-enforce-ready`): async transport stages with
one shared deadline, bounded transport waiters, and a `disconnected` event
that releases a waiter.
"""
import asyncio
import time

import pytest

from src.config import Settings
from src.services.concurrency import (
    CLASSES, Controller, TOOL_CLASSES, shadow_metadata,
)

SECRET = "issue-261-test-secret-only-0123456789abcdef"


def settings(**kw):
    return Settings(_env_file=None, secret_key=SECRET, **kw)


def make(**kw):
    """Enforce with zero waits unless a test opts into waiting."""
    kw.setdefault("mcp_concurrency_mode", "enforce")
    kw.setdefault("mcp_concurrency_wait_seconds", 0)
    kw.setdefault("mcp_concurrency_transport_wait_seconds", 0)
    return Controller(settings(**kw))


def assert_drained(c):
    assert not c.pending
    for counter in (c.requests, c.authentication, c.tools, c.writers, *c.classes.values()):
        assert counter.active == 0 and counter.waiting == 0
    for registry in (c.fingerprints, c.tenants, c.principals):
        assert not registry.entries and registry.overflow.refs == 0


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
    # Shadow never waits even with the default positive waits configured.
    c = Controller(settings())
    assert c.limits["wait_seconds"] == 5 and c.limits["transport_wait_seconds"] == 2
    calls = [await c.tool("semantic_search", 1, ("oauth", "g")) for _ in range(8)]
    assert all(x.admitted and x.queue_ms == 0 for x in calls)
    assert calls[-1].shadow["basis"] == "observed_occupancy_zero_wait"
    assert calls[-1].shadow["configured_wait_ms"] == {"transport": 2000, "tool": 5000}
    assert not c.pending and c.tools.active == 8
    for a in calls: a.lease.release()


@pytest.mark.asyncio
async def test_shared_tenant_and_principal_across_classes():
    c = make(mcp_concurrency_tools=4, mcp_concurrency_tenant=3, mcp_concurrency_principal=2)
    a = await c.tool("semantic_search", 1, ("oauth", "g"))
    b = await c.tool("find_related", 1, ("oauth", "g"))
    denied = await c.tool("read_note", 1, ("oauth", "g"))
    assert denied.pressure.scope == "principal"
    third = await c.tool("read_note", 1, ("api_key", 2))
    fourth = await c.tool("edit_note", 1, ("api_key", 3))
    assert not fourth.admitted and fourth.pressure.scope == "tenant"
    other = await c.tool("edit_note", 2, ("api_key", 4))
    assert other.admitted and c.tools.active == 4
    for item in (a, b, third, other): item.lease.release()
    assert_drained(c)


@pytest.mark.asyncio
async def test_class_ceilings_are_independent_and_global_bounds_the_mix():
    # tools 6 with light 4, scan 2, write 1, embedding 1, vector 1 (sum 9).
    c = make()
    leases = []
    for i, name in enumerate(("read_note", "read_note", "keyword_search", "keyword_search",
                              "semantic_search", "find_related", "edit_note", "read_file")):
        leases.append(await c.tool(name, i, ("api_key", i)))
    admitted = [x for x in leases if x.admitted]
    assert len(admitted) == 6 and c.tools.active == 6
    assert leases[-1].pressure.scope in {"global", "write", "light"}
    for x in admitted: x.lease.release()
    assert_drained(c)


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
    with pytest.raises(asyncio.CancelledError): await wait
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
    with pytest.raises(asyncio.CancelledError): await waiter
    same.lease.release(); overflow.lease.release()
    assert c.tenants.overflow.refs == 0
    next_epoch = await c.tool("read_note", 2, ("api_key", 2))
    assert 2 in c.tenants.entries
    next_epoch.lease.release()


@pytest.mark.asyncio
async def test_request_overflow_is_sticky_and_auth_has_separate_lifetime():
    # FINGERPRINT >= PRINCIPAL + PRINCIPAL_WAITERS, so the smallest coherent
    # fingerprint ceiling is 2.
    c = make(mcp_concurrency_registry_size=1, mcp_concurrency_fingerprint=2,
             mcp_concurrency_principal=1, mcp_concurrency_principal_waiters=1)
    first = await c.request("dedicated")
    second = await c.request("overflow")
    third = await c.request("other-overflow")  # sticky: shares the overflow entry
    first.lease.release()
    denied = await c.request("overflow")
    assert not denied.admitted and denied.pressure.scope == "fingerprint"
    assert not c.fingerprints.entries
    a, b, excess = await c.auth(), await c.auth(), await c.auth()
    assert a.admitted and b.admitted and not excess.admitted
    assert excess.pressure.scope == "global"
    a.lease.release(); b.lease.release()
    assert c.requests.active == 2
    second.lease.release(); third.lease.release()
    fresh = await c.request("overflow")
    assert "overflow" in c.fingerprints.entries
    fresh.lease.release()
    assert_drained(c)


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
    c = Controller(settings())
    a, b = await c.writer(), await c.writer()
    assert b.admitted and b.shadow and b.queue_ms == 0
    a.lease.release(); b.lease.release()


@pytest.mark.asyncio
async def test_shutdown_wakes_queued_without_grants_and_off_exemption():
    c = make(mcp_concurrency_wait_seconds=1)
    first = await c.tool("semantic_search", 1, 1)
    waiter = asyncio.create_task(c.tool("semantic_search", 2, 2))
    await asyncio.sleep(0)
    c.shutdown()
    result = await waiter
    assert not result.admitted and result.pressure.scope == "shutdown"
    first.lease.release()
    assert not c.pending and not c.tenants.entries
    c = Controller(settings(mcp_concurrency_mode="off"))
    for _ in range(50):
        assert (await c.tool("semantic_search", 1, 1)).admitted
        assert (await c.request("fp")).admitted and (await c.auth()).admitted
    assert c.tools.active == 0 and c.requests.active == 0
    assert (await make().tool("internal", None, None, resource_class="light")).admitted
    with pytest.raises(ValueError): await make().tool("unregistered", 1, 1)
    with pytest.raises(ValueError): await make().tool("internal", 1, 1, resource_class="other")
    assert len(TOOL_CLASSES) == 25
    assert set(TOOL_CLASSES.values()) == CLASSES == {"embedding", "vector", "write", "scan", "light"}
    assert shadow_metadata(()) is None


def test_class_mapping_is_the_design_table():
    by_class = {}
    for tool, cls in TOOL_CLASSES.items():
        by_class.setdefault(cls, set()).add(tool)
    assert by_class == {
        "embedding": {"semantic_search"},
        "vector": {"find_related"},
        "write": {"create_note", "edit_note", "move_note", "delete_note", "set_frontmatter",
                  "write_file", "delete_file", "import_from_url"},
        "scan": {"keyword_search", "list_notes", "get_tags", "get_neighborhood",
                 "find_orphans", "list_files"},
        "light": {"read_note", "read_file", "get_recent", "get_vault_guide", "get_backlinks",
                  "get_links", "request_upload", "check_upload", "request_download"},
    }


# ── transport stages: shared deadline, bounded waiters, disconnects ─────

def transport(mode="enforce", **kw):
    kw.setdefault("mcp_concurrency_fingerprint", 2)
    kw.setdefault("mcp_concurrency_principal", 1)
    kw.setdefault("mcp_concurrency_principal_waiters", 1)
    kw.setdefault("mcp_concurrency_transport_wait_seconds", .2)
    return make(mcp_concurrency_mode=mode, **kw)


@pytest.mark.asyncio
async def test_transport_stages_share_one_deadline():
    c = transport()
    holders = [await c.request("fp") for _ in range(2)]
    auth_holders = [await c.auth(), await c.auth()]
    loop = asyncio.get_running_loop()
    deadline = c.transport_deadline()
    started = loop.time()
    loop.call_later(.15, holders[0].lease.release)
    request = await c.request("fp", deadline, asyncio.Event())
    assert request.admitted and 100 <= request.queue_ms <= 190
    auth = await c.auth(deadline, asyncio.Event())
    elapsed = loop.time() - started
    assert not auth.admitted and auth.pressure.scope == "global"
    # Only the remaining ~50 ms of the one deadline, not a fresh 200 ms.
    assert auth.queue_ms < 100 and elapsed < .26
    request.lease.release(); holders[1].lease.release()
    for x in auth_holders: x.lease.release()
    assert_drained(c)


@pytest.mark.asyncio
async def test_expired_transport_deadline_is_a_zero_wait_miss():
    c = transport()
    holders = [await c.request("fp") for _ in range(2)]
    deadline = asyncio.get_running_loop().time() - 1
    refused = await c.request("fp", deadline, asyncio.Event())
    assert not refused.admitted and refused.queue_ms == 0 and not c.pending
    for x in holders: x.lease.release()
    assert_drained(c)


@pytest.mark.asyncio
async def test_auth_ceiling_two_absorbs_six_arrivals():
    c = make(mcp_concurrency_transport_wait_seconds=2)
    open_now = peak = 0

    async def one():
        nonlocal open_now, peak
        adm = await c.auth(c.transport_deadline(), asyncio.Event())
        assert adm.admitted
        open_now += 1
        peak = max(peak, open_now)
        try:
            await asyncio.sleep(.01)
        finally:
            open_now -= 1
            adm.lease.release()
        return adm

    results = await asyncio.gather(*(one() for _ in range(6)))
    assert all(r.admitted for r in results) and peak == 2
    assert sum(1 for r in results if r.queue_ms > 0) == 4
    assert_drained(c)


@pytest.mark.asyncio
async def test_transport_waiter_bounds():
    c = transport(mcp_concurrency_fingerprint_waiters=1)
    holders = [await c.request("fp") for _ in range(2)]
    waiting = asyncio.create_task(c.request("fp", c.transport_deadline(), asyncio.Event()))
    await asyncio.sleep(0)
    assert c.fingerprints.entries["fp"].waiting == 1 and c.requests.waiting == 1
    # The waiter keeps its fingerprint entry retained.
    assert c.fingerprints.entries["fp"].refs == 3
    overflow = await c.request("fp", c.transport_deadline(), asyncio.Event())
    assert not overflow.admitted and overflow.pressure.scope == "fingerprint_waiters"
    holders[0].lease.release()
    granted = await waiting
    assert granted.admitted
    granted.lease.release(); holders[1].lease.release()
    assert_drained(c)
    c = make(mcp_concurrency_auth_waiters=1, mcp_concurrency_transport_wait_seconds=1)
    held = [await c.auth(), await c.auth()]
    queued = asyncio.create_task(c.auth(c.transport_deadline(), asyncio.Event()))
    await asyncio.sleep(0)
    refused = await c.auth(c.transport_deadline(), asyncio.Event())
    assert not refused.admitted and refused.pressure.scope == "auth_waiters"
    queued.cancel()
    with pytest.raises(asyncio.CancelledError): await queued
    for x in held: x.lease.release()
    assert_drained(c)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["enforce", "queue"])
async def test_disconnect_releases_a_request_waiter(mode):
    c = transport(mode, mcp_concurrency_transport_wait_seconds=5)
    holders = [await c.request("fp") for _ in range(2)]
    gone = asyncio.Event()
    started = time.monotonic()
    task = asyncio.create_task(c.request("fp", c.transport_deadline(), gone))
    await asyncio.sleep(0)
    assert c.requests.waiting == 1 and c.fingerprints.entries["fp"].refs == 3
    gone.set()
    result = await task
    assert result.disconnected and not result.admitted
    assert time.monotonic() - started < 1
    assert c.requests.waiting == 0 and not c.pending
    assert c.fingerprints.entries["fp"].refs == 2 and c.requests.active == 2
    for x in holders: x.lease.release()
    assert_drained(c)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["enforce", "queue"])
async def test_disconnect_releases_an_auth_waiter(mode):
    c = make(mcp_concurrency_mode=mode, mcp_concurrency_transport_wait_seconds=5)
    request = await c.request("fp")
    held = [await c.auth(), await c.auth()]
    gone = asyncio.Event()
    task = asyncio.create_task(c.auth(c.transport_deadline(), gone))
    await asyncio.sleep(0)
    assert c.authentication.waiting == 1
    gone.set()
    result = await task
    assert result.disconnected and not result.admitted and result.lease is None
    assert c.authentication.waiting == 0 and c.authentication.active == 2 and not c.pending
    for x in held: x.lease.release()
    request.lease.release()
    assert_drained(c)


@pytest.mark.asyncio
async def test_disconnect_racing_a_grant_releases_the_grant():
    c = make(mcp_concurrency_transport_wait_seconds=5)
    held = [await c.auth(), await c.auth()]
    gone = asyncio.Event()
    task = asyncio.create_task(c.auth(c.transport_deadline(), gone))
    await asyncio.sleep(0)
    held[0].lease.release()  # grant transferred to the waiter...
    gone.set()               # ...and the client left in the same step
    result = await task
    assert result.disconnected and not result.admitted
    assert c.authentication.active == 1
    held[1].lease.release()
    assert_drained(c)


@pytest.mark.asyncio
async def test_already_disconnected_request_does_not_wait():
    c = transport(mcp_concurrency_transport_wait_seconds=5)
    holders = [await c.request("fp") for _ in range(2)]
    gone = asyncio.Event()
    gone.set()
    result = await c.request("fp", c.transport_deadline(), gone)
    assert result.disconnected and not c.pending
    for x in holders: x.lease.release()
    assert_drained(c)


@pytest.mark.asyncio
async def test_cancelled_transport_waiter_leaks_nothing():
    c = transport(mcp_concurrency_transport_wait_seconds=5)
    holders = [await c.request("fp") for _ in range(2)]
    task = asyncio.create_task(c.request("fp", c.transport_deadline(), asyncio.Event()))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert c.fingerprints.entries["fp"].refs == 2 and c.requests.waiting == 0
    for x in holders: x.lease.release()
    assert_drained(c)


@pytest.mark.asyncio
async def test_shadow_transport_never_waits():
    c = Controller(settings(mcp_concurrency_fingerprint=2, mcp_concurrency_principal=1,
                            mcp_concurrency_principal_waiters=1))
    leases = [await c.request("fp", c.transport_deadline(), asyncio.Event()) for _ in range(4)]
    assert all(x.admitted and x.queue_ms == 0 for x in leases)
    assert leases[-1].shadow["code"] == "request_concurrency_limited"
    assert not c.pending
    for x in leases: x.lease.release()
    assert_drained(c)
