"""The /mcp body-memory budget's lanes, waits and releases (#322, D5–D7).

Pure in-process tests of `BodyBudget`: no middleware, no ASGI. The middleware
wiring is `tests/test_body_budget_middleware.py`.
"""
import asyncio
import random

import pytest

from src.services.body_budget import BodyBudget

MIB = 1024 * 1024
KIB = 1024


def budget(*, small=16 * MIB, large=112 * MIB, wait=1.0, waiters=8):
    return BodyBudget(small_lane=small, large_lane=large, wait_seconds=wait,
                      waiters=waiters)


async def settle(n=3):
    for _ in range(n):
        await asyncio.sleep(0)


def assert_idle(b):
    assert b.reserved == 0
    assert b.waiting == 0
    assert not b.small_queue and not b.large_queue


async def test_immediate_grant_and_idempotent_release():
    b = budget()
    a = await b.reserve(10 * MIB, small=False)
    assert a.admitted and a.lane == "large" and a.scope == "large"
    assert b.large_used == 10 * MIB
    a.lease.release()
    a.lease.release()
    assert_idle(b)


async def test_zero_size_reserves_nothing():
    b = budget()
    a = await b.reserve(0, small=True)
    assert a.admitted
    assert b.reserved == 0
    a.lease.release()
    assert_idle(b)


async def test_small_requests_pass_while_the_large_lane_is_full():
    b = budget(wait=5)
    full = await b.reserve(112 * MIB, small=False)
    waiting = asyncio.create_task(b.reserve(60 * MIB, small=False))
    await settle()
    assert len(b.large_queue) == 1
    small = await b.reserve(4 * KIB, small=True)
    assert small.admitted and small.lane == "small"
    small.lease.release()
    full.lease.release()
    granted = await waiting
    assert granted.admitted
    granted.lease.release()
    assert_idle(b)


async def test_small_requests_cannot_starve_a_waiting_large_request():
    b = budget(small=2 * MIB, large=112 * MIB, wait=5)
    smalls = [await b.reserve(1 * MIB, small=True) for _ in range(2)]
    assert b.small_used == 2 * MIB
    holder = await b.reserve(100 * MIB, small=False)
    # 12 MiB of the large lane is free, but a large request waits for 60.
    large = asyncio.create_task(b.reserve(60 * MIB, small=False))
    await settle()
    assert len(b.large_queue) == 1
    blocked = asyncio.create_task(b.reserve(1 * MIB, small=True))
    await settle()
    assert len(b.small_queue) == 1, "a small request borrowed while a large one waited"
    assert b.large_used == 100 * MIB
    holder.lease.release()
    big = await large
    assert big.admitted
    # The large FIFO is empty again, so the small waiter may borrow now.
    small = await blocked
    assert small.admitted and small.lane == "large"
    for a in (*smalls, big, small):
        a.lease.release()
    assert_idle(b)


async def test_small_borrows_from_large_when_no_large_request_waits():
    b = budget(small=1 * MIB)
    first = await b.reserve(1 * MIB, small=True)
    second = await b.reserve(512 * KIB, small=True)
    assert second.admitted and second.lane == "large"
    second.lease.release()
    assert b.large_used == 0, "a borrowed reservation is credited to the large lane"
    first.lease.release()
    assert_idle(b)


async def test_no_barging_among_large_requests():
    b = budget(wait=5)
    first = await b.reserve(50 * MIB, small=False)
    second = await b.reserve(52 * MIB, small=False)  # 10 MiB free
    head = asyncio.create_task(b.reserve(60 * MIB, small=False))
    await settle()
    later = asyncio.create_task(b.reserve(2 * MIB, small=False))
    await settle()
    assert not later.done(), "a 2 MiB request overtook a waiting 60 MiB head"
    assert [w.size for w in b.large_queue] == [60 * MIB, 2 * MIB]
    # 60 MiB free: exactly the head's size. The head is admitted first and
    # takes all of it, so the 2 MiB request is still behind.
    first.lease.release()
    a = await head
    assert a.admitted
    await settle()
    assert not later.done()
    assert [w.size for w in b.large_queue] == [2 * MIB]
    second.lease.release()
    c = await later
    assert c.admitted
    a.lease.release()
    c.lease.release()
    assert_idle(b)


async def test_waiter_bound_overflow_refuses_at_once():
    b = budget(waiters=2, wait=5)
    holder = await b.reserve(112 * MIB, small=False)
    w1 = asyncio.create_task(b.reserve(20 * MIB, small=False))
    w2 = asyncio.create_task(b.reserve(20 * MIB, small=False))
    await settle()
    assert b.waiting == 2
    refused = await b.reserve(20 * MIB, small=False)
    assert not refused.admitted and refused.refused_reason == "waiters"
    assert refused.scope == "large" and refused.limit == 112 * MIB
    holder.lease.release()
    for t in (w1, w2):
        (await t).lease.release()
    assert_idle(b)


async def test_waiter_bound_is_shared_by_both_lanes():
    b = budget(small=1 * MIB, large=2 * MIB, waiters=1, wait=5)
    large = await b.reserve(2 * MIB, small=False)
    small = await b.reserve(1 * MIB, small=True)
    w = asyncio.create_task(b.reserve(1 * MIB, small=False))
    await settle()
    refused = await b.reserve(1 * MIB, small=True)
    assert refused.refused_reason == "waiters" and refused.scope == "small"
    assert refused.limit == 1 * MIB
    w.cancel()
    await asyncio.gather(w, return_exceptions=True)
    large.lease.release()
    small.lease.release()
    assert_idle(b)


async def test_zero_wait_refuses_without_suspending():
    b = budget(wait=0)
    holder = await b.reserve(112 * MIB, small=False)
    coro = b.reserve(20 * MIB, small=False)
    # An immediate refusal completes on its first step: it never suspends.
    with pytest.raises(StopIteration) as stop:
        coro.send(None)
    admission = stop.value.value
    assert not admission.admitted and admission.refused_reason == "deadline"
    assert b.waiting == 0
    holder.lease.release()
    assert_idle(b)


async def test_deadline_expiry_refuses_and_leaves_the_queue():
    b = budget(wait=0.05)
    holder = await b.reserve(112 * MIB, small=False)
    a = await b.reserve(20 * MIB, small=False)
    assert not a.admitted and a.refused_reason == "deadline"
    assert a.queue_ms >= 40
    assert b.waiting == 0
    holder.lease.release()
    assert_idle(b)


async def test_disconnect_removes_the_waiter_within_one_iteration():
    b = budget(wait=5)
    holder = await b.reserve(112 * MIB, small=False)
    gone = asyncio.Event()
    t = asyncio.create_task(b.reserve(20 * MIB, small=False, disconnected=gone))
    await settle()
    assert b.waiting == 1
    gone.set()
    await settle(3)
    assert t.done()
    a = t.result()
    assert a.disconnected and not a.admitted
    assert b.waiting == 0
    holder.lease.release()
    assert_idle(b)


async def test_cancellation_while_waiting_removes_the_waiter():
    b = budget(wait=5)
    holder = await b.reserve(112 * MIB, small=False)
    t = asyncio.create_task(b.reserve(20 * MIB, small=False))
    await settle()
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    assert b.waiting == 0
    holder.lease.release()
    assert_idle(b)


async def test_a_grant_that_races_cancellation_is_handed_back():
    b = budget(wait=5)
    holder = await b.reserve(112 * MIB, small=False)
    t = asyncio.create_task(b.reserve(20 * MIB, small=False))
    await settle()
    # Same tick: the release grants the waiter, then its task is cancelled
    # before it runs again.
    holder.lease.release()
    assert b.large_used == 20 * MIB
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    assert_idle(b)


async def test_release_wakes_the_next_eligible_waiter():
    b = budget(wait=5)
    a = await b.reserve(60 * MIB, small=False)
    c = await b.reserve(50 * MIB, small=False)
    w = asyncio.create_task(b.reserve(60 * MIB, small=False))
    await settle()
    c.lease.release()
    await settle()
    assert not w.done(), "50 MiB free is not enough for 60"
    a.lease.release()
    granted = await w
    assert granted.admitted and granted.queue_ms > 0
    granted.lease.release()
    assert_idle(b)


async def test_an_unsatisfiable_size_is_refused_at_once():
    b = budget(small=1 * MIB, large=8 * MIB)
    a = await b.reserve(9 * MIB, small=False)
    assert a.refused_reason == "too_large"
    assert_idle(b)


async def test_mixed_burst_of_fifty_leaves_nothing_held():
    rng = random.Random(322)
    b = budget(small=2 * MIB, large=40 * MIB, wait=0.2, waiters=8)
    outcomes = {"admitted": 0, "refused": 0, "disconnected": 0, "cancelled": 0}

    async def one(i):
        small = rng.random() < 0.5
        size = rng.randint(1, MIB) if small else rng.randint(2 * MIB, 30 * MIB)
        gone = asyncio.Event()
        fate = rng.choice(["complete", "fail", "disconnect", "cancel"])
        task = asyncio.create_task(b.reserve(size, small=small, disconnected=gone))
        if fate == "disconnect":
            asyncio.get_running_loop().call_later(rng.random() * 0.05, gone.set)
        if fate == "cancel":
            asyncio.get_running_loop().call_later(rng.random() * 0.05, task.cancel)
        try:
            a = await task
        except asyncio.CancelledError:
            outcomes["cancelled"] += 1
            return
        if a.disconnected:
            outcomes["disconnected"] += 1
            return
        if not a.admitted:
            outcomes["refused"] += 1
            return
        outcomes["admitted"] += 1
        try:
            await asyncio.sleep(rng.random() * 0.02)
            if fate == "fail":
                raise RuntimeError("downstream failed")
        except RuntimeError:
            pass
        finally:
            a.lease.release()

    await asyncio.gather(*(one(i) for i in range(50)))
    assert sum(outcomes.values()) == 50
    assert outcomes["admitted"] > 0
    assert_idle(b)
