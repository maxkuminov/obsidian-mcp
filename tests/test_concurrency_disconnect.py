"""Disconnect-aware transport waits and byte-exact replay (#188 D1).

Every disconnect here is a real `http.disconnect` delivered through ASGI
`receive`, never `task.cancel()`: uvicorn reports a client that left through
`receive` and does not cancel the middleware, so cancellation alone proves
nothing about a waiter whose client went away.
"""
import asyncio
import json
from types import SimpleNamespace

import pytest

from src.config import Settings
from src.mcp_server import auth, tools
from src.services import concurrency

MIB = 1024 * 1024


def scope(token='omcp_disconnect_fixture', method='POST'):
    return {'type': 'http', 'method': method, 'path': '/mcp', 'raw_path': b'/mcp',
            'query_string': b'', 'scheme': 'http', 'server': ('test', 80),
            'client': ('127.0.0.3', 3000),
            'headers': [(b'authorization', ('Bearer ' + token).encode())]}


def body(data, more):
    return {'type': 'http.request', 'body': data, 'more_body': more}


DISCONNECT = {'type': 'http.disconnect'}


def record(harness):
    async def send(message):
        harness.sent.append(message)
    return send


class Wire:
    """A scripted ASGI `receive` that counts calls and never duplicates."""

    def __init__(self, *messages):
        self.queue = asyncio.Queue()
        for message in messages:
            self.queue.put_nowait(message)
        self.delivered = 0

    def push(self, message):
        self.queue.put_nowait(message)

    async def receive(self):
        message = await self.queue.get()
        self.delivered += 1
        return message


@pytest.fixture
def harness(monkeypatch):
    state = SimpleNamespace(sessions=0, usage_rows=0, app_calls=0, sent=[])

    def install(mode='enforce', **kw):
        c = concurrency.Controller(Settings(
            _env_file=None, secret_key='issue-188-test-secret-only-0123456789abcdef',
            mcp_concurrency_mode=mode, **kw))
        monkeypatch.setattr(concurrency, '_controller', c)
        monkeypatch.setattr(concurrency, '_replay_budget',
                            concurrency.ReplayBudget(c.limits['replay_budget_bytes']))
        concurrency.reset_counters()
        return c

    def no_session():
        state.sessions += 1
        raise AssertionError('credential query for a request whose client left')

    async def no_usage(values):
        state.usage_rows += 1

    monkeypatch.setattr(auth, 'async_session', no_session)
    monkeypatch.setattr(tools, '_insert_usage', no_usage)
    monkeypatch.setattr(auth.settings, 'mcp_sandbox_mode', False)
    monkeypatch.setattr(auth.settings, 'multi_user_mode', False)
    monkeypatch.setattr(auth.rate_limits, 'check_auth_failures', lambda *_: None)
    monkeypatch.setattr(auth.rate_limits, 'record_auth_failure', lambda *_: None)
    monkeypatch.setattr(auth.security_events, 'emit', lambda *a, **kw: None)
    state.install = install
    yield state
    concurrency.reset_counters()


async def hold(c, stage):
    """Fill one transport stage with leases owned by other requests."""
    if stage == 'request':
        deadline = c.transport_deadline()
        return [await c.request(f'other-{i}', deadline) for i in range(c.limits['requests'])]
    return [await c.auth() for _ in range(c.limits['auth'])]


# `requests` 2 needs `fingerprint` 2, and coherence then needs principal +
# principal_waiters ≤ 2.
SMALL = dict(mcp_concurrency_requests=2, mcp_concurrency_fingerprint=2,
             mcp_concurrency_principal=1, mcp_concurrency_principal_waiters=1)


async def until(predicate, limit=200):
    for _ in range(limit):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError('condition never held')


def assert_clean(c, holders):
    assert not c.pending
    assert c.requests.waiting == c.authentication.waiting == 0
    assert all(e.waiting == 0 for e in c.fingerprints.entries.values())
    assert concurrency.replay_budget().used == 0
    for lease in holders:
        lease.lease.release()
    assert c.requests.active == c.authentication.active == 0
    assert not c.fingerprints.entries


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['enforce', 'queue'])
@pytest.mark.parametrize('stage', ['request', 'auth'])
@pytest.mark.parametrize('case', ['no_body', 'complete_body', 'fragmented_body'])
async def test_real_disconnect_frees_a_transport_waiter(harness, mode, stage, case):
    c = harness.install(mode, mcp_concurrency_transport_wait_seconds=5, **SMALL)
    holders = await hold(c, stage)
    before = {'no_body': [],
              'complete_body': [body(b'{"jsonrpc":"2.0"}', False)],
              'fragmented_body': [body(b'{"json', True), body(b'rpc":', True)]}[case]
    wire = Wire(*before)

    async def app(*args):
        harness.app_calls += 1

    async def send(message):
        harness.sent.append(message)

    loop = asyncio.get_running_loop()
    task = asyncio.create_task(auth.APIKeyMiddleware(app)(scope(), wire.receive, send))
    await until(lambda: c.pending and wire.delivered == len(before))
    started = loop.time()
    wire.push(DISCONNECT)
    await asyncio.wait_for(task, 1)
    assert loop.time() - started < 0.5, 'released by the deadline, not the disconnect'
    assert harness.sent == [] and harness.app_calls == 0
    assert harness.sessions == 0, 'a credential query ran for a departed client'
    assert harness.usage_rows == 0
    assert_clean(c, holders)
    drained = concurrency.counters().drain()
    assert sum(n for (_, metric), (n, _) in drained.items() if metric == 'requests') == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['request', 'auth'])
async def test_cancelled_transport_waiter_leaks_nothing(harness, stage):
    c = harness.install('enforce', mcp_concurrency_transport_wait_seconds=5, **SMALL)
    holders = await hold(c, stage)
    wire = Wire(body(b'x', False))
    task = asyncio.create_task(auth.APIKeyMiddleware(None)(scope(), wire.receive, lambda m: None))
    await until(lambda: c.pending)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)
    assert harness.sessions == 0
    assert_clean(c, holders)


async def admitted_after_wait(harness, c, wire, *, release_when):
    """Run one request through an auth wait; return what the app received."""
    received = []

    async def authenticate(self, request, scope_, token):
        return None

    async def app(scope_, receive, send):
        while True:
            message = await receive()
            received.append(message)
            if message['type'] == 'http.disconnect' or not message.get('more_body'):
                return

    holders = await hold(c, 'auth')
    original = auth.APIKeyMiddleware._authenticate
    auth.APIKeyMiddleware._authenticate = authenticate
    try:
        task = asyncio.create_task(auth.APIKeyMiddleware(app)(scope(), wire.receive,
                                                               record(harness)))
        await until(lambda: c.pending)
        await release_when()
        holders.pop().lease.release()
        await asyncio.wait_for(task, 2)
    finally:
        auth.APIKeyMiddleware._authenticate = original
    for h in holders:
        h.lease.release()
    return received


@pytest.mark.asyncio
@pytest.mark.parametrize('messages', [
    [body(b'{"jsonrpc":"2.0","id":1}', False)],
    [body(b'{"json', True), body(b'rpc":"2.0",', True), body(b'"id":1}', False)],
    [body(b'', True), body(b'abc', True), body(b'', False)],
])
async def test_body_read_while_waiting_is_replayed_byte_exact(harness, messages):
    c = harness.install('enforce', mcp_concurrency_transport_wait_seconds=5)
    wire = Wire(*messages)

    async def consumed():
        await until(lambda: wire.delivered == len(messages))

    received = await admitted_after_wait(harness, c, wire, release_when=consumed)
    assert received == messages
    assert wire.delivered == len(messages)
    assert concurrency.replay_budget().used == 0


@pytest.mark.asyncio
async def test_single_message_larger_than_the_budget_is_kept(harness):
    c = harness.install('enforce', mcp_concurrency_transport_wait_seconds=5,
                        mcp_concurrency_replay_budget_bytes=MIB)
    big = bytes(range(256)) * (6 * 1024)  # 1.5 MiB, larger than the whole budget
    assert len(big) > MIB
    wire = Wire(body(big, False))

    async def consumed():
        await until(lambda: wire.delivered == 1)
        # The watcher appends one loop step after the receive task finishes.
        await until(lambda: concurrency.replay_budget().exhausted)

    received = await admitted_after_wait(harness, c, wire, release_when=consumed)
    assert received == [body(big, False)]
    assert concurrency.replay_budget().used == 0


@pytest.mark.asyncio
async def test_fragments_crossing_the_budget_stop_consumption_and_arrive_whole(harness):
    c = harness.install('enforce', mcp_concurrency_transport_wait_seconds=5,
                        mcp_concurrency_replay_budget_bytes=MIB)
    chunk = 400 * 1024
    parts = [bytes([i]) * chunk for i in range(4)]
    messages = [body(p, i < 3) for i, p in enumerate(parts)]
    wire = Wire(*messages)

    async def consumed():
        await until(lambda: wire.delivered == 3)
        for _ in range(20):
            await asyncio.sleep(0)
        # The third fragment crossed the budget: the watcher stopped, and the
        # fourth is still in the transport, not in memory here.
        assert wire.delivered == 3 and wire.queue.qsize() == 1

    received = await admitted_after_wait(harness, c, wire, release_when=consumed)
    assert b''.join(m['body'] for m in received) == b''.join(parts)
    assert received == messages
    assert concurrency.replay_budget().used == 0


@pytest.mark.asyncio
async def test_budget_exhausted_waiter_is_deadline_bounded(harness):
    c = harness.install('enforce', mcp_concurrency_transport_wait_seconds=0.3,
                        mcp_concurrency_replay_budget_bytes=MIB)
    holders = await hold(c, 'auth')
    wire = Wire(body(b'z' * (MIB + 1), False))
    loop = asyncio.get_running_loop()
    started = loop.time()
    task = asyncio.create_task(auth.APIKeyMiddleware(None)(scope(), wire.receive,
                                                            record(harness)))
    await until(lambda: wire.delivered == 1)
    wire.push(DISCONNECT)  # not read: the budget is exhausted (L8)
    await asyncio.wait_for(task, 2)
    elapsed = loop.time() - started
    assert 0.25 <= elapsed < 1.5
    assert harness.sent[0]['status'] == 429
    assert wire.queue.qsize() == 1, 'the watcher kept reading past an exhausted budget'
    assert harness.sessions == 0
    assert_clean(c, holders)


@pytest.mark.asyncio
async def test_handoff_race_replays_a_message_exactly_once(harness):
    c = harness.install('enforce', mcp_concurrency_transport_wait_seconds=5)
    wire = Wire()
    payload = body(b'{"id":7}', False)

    async def arrive_with_grant():
        # The message arrives in the same loop step as the grant below, so the
        # watcher's receive completes while admission is ending.
        wire.push(payload)

    received = await admitted_after_wait(harness, c, wire, release_when=arrive_with_grant)
    assert received == [payload]
    assert wire.delivered == 1
    assert wire.queue.qsize() == 0


@pytest.mark.asyncio
async def test_immediate_admission_starts_no_watcher(harness, monkeypatch):
    c = harness.install('enforce')
    started = []
    monkeypatch.setattr(auth.ReceiveWatch, 'start', lambda self: started.append(True))
    wire = Wire(body(b'{}', False))
    seen = []

    async def authenticate(self, *a):
        return None

    async def app(scope_, receive, send):
        seen.append(receive)

    monkeypatch.setattr(auth.APIKeyMiddleware, '_authenticate', authenticate)
    await auth.APIKeyMiddleware(app)(scope(), wire.receive, record(harness))
    assert started == [] and wire.delivered == 0
    assert seen == [wire.receive], 'an unwatched request must get the real receive'
    assert c.requests.active == c.authentication.active == 0


def test_uvicorn_flow_control_constant_bounding_l11():
    # L11's ~62 MiB bound is budget + 96 waiters x (HIGH_WATER_LIMIT + one
    # 256 KiB read). A uvicorn upgrade that changes this must re-derive it.
    from uvicorn.protocols.http import flow_control
    assert flow_control.HIGH_WATER_LIMIT == 65536


@pytest.mark.asyncio
async def test_non_blocking_receive_cannot_spin_the_watcher(harness):
    # Only a disconnect may follow a complete body; a receive that answers
    # without ever blocking must not turn the watcher into a busy loop.
    c = harness.install('enforce', mcp_concurrency_transport_wait_seconds=0.2)
    holders = await hold(c, 'auth')
    calls = 0

    async def eager_receive():
        nonlocal calls
        calls += 1
        return body(b'{}', False)

    await asyncio.wait_for(
        auth.APIKeyMiddleware(None)(scope(), eager_receive, record(harness)), 2)
    assert calls == 2
    assert harness.sent[0]['status'] == 429
    assert_clean(c, holders)


# ── per-request outcomes: events and the durable counters (D1, D8) ─────────

def totals():
    out = {}
    for (_, metric), (count, value) in concurrency.counters().drain().items():
        if metric in concurrency.GAUGE_METRICS:
            out[metric] = max(out.get(metric, 0), value)
        else:
            out[metric] = out.get(metric, 0) + count
    return out


def authenticated(monkeypatch):
    async def authenticate(self, *a):
        return None
    monkeypatch.setattr(auth.APIKeyMiddleware, '_authenticate', authenticate)


async def ok_app(scope_, receive, send):
    await send({'type': 'http.response.start', 'status': 200, 'headers': []})
    await send({'type': 'http.response.body', 'body': b'ok'})


@pytest.mark.asyncio
async def test_shadow_pressure_at_two_stages_counts_once(harness, monkeypatch):
    c = harness.install('shadow', **SMALL)
    events = []
    monkeypatch.setattr(auth.security_events, 'emit', lambda e, **kw: events.append(kw))
    authenticated(monkeypatch)
    held = await hold(c, 'request') + await hold(c, 'auth')
    await auth.APIKeyMiddleware(ok_app)(scope(), Wire(body(b'{}', False)).receive,
                                       record(harness))
    assert harness.sent[0]['status'] == 200
    assert [e['outcome'] for e in events] == ['shadow', 'shadow']
    assert {e['reason'] for e in events} == {'request:global', 'auth:global'}
    counted = totals()
    assert counted['requests'] == 1 and counted['transport_pressured'] == 1
    for lease in held:
        lease.lease.release()


@pytest.mark.asyncio
async def test_queue_waiter_overflow_admits_and_counts_one_overrun(harness, monkeypatch):
    c = harness.install('queue', mcp_concurrency_fingerprint_waiters=1, **SMALL)
    events = []
    monkeypatch.setattr(auth.security_events, 'emit', lambda e, **kw: events.append(kw))
    authenticated(monkeypatch)
    # Fill the fingerprint (2) and its single waiter slot with the same bearer.
    fp = auth.hash_key('omcp_disconnect_fixture')
    held = [await c.request(fp, c.transport_deadline()) for _ in range(2)]
    parked = asyncio.create_task(c.request(fp, c.transport_deadline()))
    await until(lambda: c.pending)
    loop = asyncio.get_running_loop()
    started = loop.time()
    await auth.APIKeyMiddleware(ok_app)(scope(), Wire(body(b'{}', False)).receive,
                                       record(harness))
    assert loop.time() - started < 0.1, 'an overflow in queue mode must not wait'
    assert harness.sent[0]['status'] == 200
    assert [e['outcome'] for e in events] == ['overrun']
    counted = totals()
    assert counted['requests'] == 1 and counted['transport_overrun'] == 1
    for lease in held:
        lease.lease.release()
    (await parked).lease.release()


@pytest.mark.asyncio
async def test_queue_deadline_overrun_carries_observations_to_the_tool(harness, monkeypatch):
    c = harness.install('queue', mcp_concurrency_transport_wait_seconds=0.05)
    authenticated(monkeypatch)
    seen = {}

    async def app(scope_, receive, send):
        seen['observations'] = concurrency.request_observations.get()
        seen['transport_ms'] = auth.current_transport_queue_ms.get()
        await ok_app(scope_, receive, send)

    held = await hold(c, 'auth')
    await auth.APIKeyMiddleware(app)(scope(), Wire(body(b'{}', False)).receive,
                                    record(harness))
    assert harness.sent[0]['status'] == 200
    (obs,) = seen['observations']
    assert obs.stage == 'auth' and obs.overrun and obs.waited_ms >= 40
    assert seen['transport_ms'] >= 40
    counted = totals()
    assert counted['transport_overrun'] == 1
    assert counted['transport_wait_max_ms'] >= 40
    for lease in held:
        lease.lease.release()


@pytest.mark.asyncio
async def test_long_granted_wait_emits_waited_and_never_refused(harness, monkeypatch):
    c = harness.install('enforce', mcp_concurrency_transport_wait_seconds=2)
    events = []
    monkeypatch.setattr(auth.security_events, 'emit', lambda e, **kw: events.append(kw))
    authenticated(monkeypatch)
    held = await hold(c, 'auth')
    wire = Wire(body(b'{}', False))
    task = asyncio.create_task(auth.APIKeyMiddleware(ok_app)(scope(), wire.receive,
                                                            record(harness)))
    await until(lambda: c.pending)
    await asyncio.sleep(0.15)
    held.pop().lease.release()
    await asyncio.wait_for(task, 2)
    assert harness.sent[0]['status'] == 200
    assert [e['outcome'] for e in events] == ['waited']
    counted = totals()
    assert counted['transport_waited'] == 1 and 'transport_refused' not in counted
    for lease in held:
        lease.lease.release()


@pytest.mark.asyncio
async def test_enforce_deadline_refusal_keeps_the_429_shape_and_counts(harness):
    c = harness.install('enforce', mcp_concurrency_transport_wait_seconds=0.05)
    held = await hold(c, 'auth')
    await auth.APIKeyMiddleware(None)(scope(), Wire(body(b'{}', False)).receive,
                                     record(harness))
    start = harness.sent[0]
    assert start['status'] == 429
    assert (b'retry-after', b'1') in start['headers']
    assert json.loads(harness.sent[1]['body']) == {
        'error': 'MCP concurrency capacity is unavailable',
        'code': 'auth_concurrency_limited', 'scope': 'global', 'limit': 2}
    counted = totals()
    assert counted['requests'] == 1 and counted['transport_refused'] == 1
    assert harness.sessions == 0
    assert_clean(c, held)


# ── handoff through the production middleware stack (impl review R1-1) ────

def production_stack(inner):
    """`inner` behind `APIKeyMiddleware`, behind the security-header wrapper.

    The same `BaseHTTPMiddleware` wrapping `src/main.py` installs with
    `@app.middleware("http")`, with the same dispatch function. Its `receive`
    runs in an anyio task group and suspends in the group's cleanup *after*
    it has taken a message from the server and *before* it returns it, which
    is the window a cancelling handoff lost messages in.
    """
    from starlette.middleware.base import BaseHTTPMiddleware
    from src import main
    return BaseHTTPMiddleware(auth.APIKeyMiddleware(inner),
                              dispatch=main.add_security_headers)


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['enforce', 'queue'])
@pytest.mark.parametrize('spins', [0, 1, 2, 3, 5])
async def test_body_arriving_at_handoff_survives_the_security_header_wrapper(
        harness, monkeypatch, mode, spins):
    c = harness.install(mode, mcp_concurrency_transport_wait_seconds=5)
    authenticated(monkeypatch)
    payload = body(b'{"jsonrpc":"2.0","id":9,"method":"ping"}', False)
    received = []

    async def app(scope_, receive, send):
        while True:
            message = await receive()
            received.append(message)
            if message['type'] == 'http.disconnect' or not message.get('more_body'):
                break
        await send({'type': 'http.response.start', 'status': 200,
                    'headers': [(b'content-type', b'application/json')]})
        await send({'type': 'http.response.body', 'body': b'{}'})

    wire = Wire()
    holders = await hold(c, 'auth')
    task = asyncio.create_task(production_stack(app)(scope(), wire.receive,
                                                     record(harness)))
    await until(lambda: c.pending)
    # The body arrives while the watcher's receive is inside the wrapper, and
    # the grant lands `spins` loop steps later: the wrapper has taken the
    # message from the server but not yet returned it to the watcher.
    wire.push(payload)
    for _ in range(spins):
        await asyncio.sleep(0)
    holders.pop().lease.release()
    await asyncio.wait_for(task, 2)

    assert received == [payload], 'the body taken at handoff was lost or altered'
    assert wire.delivered == 1 and wire.queue.qsize() == 0
    assert harness.sent[0]['type'] == 'http.response.start'
    assert harness.sent[0]['status'] == 200
    assert b''.join(m.get('body', b'') for m in harness.sent
                    if m['type'] == 'http.response.body') == b'{}'
    assert harness.sent[-1].get('more_body', False) is False
    assert c.requests.active == 0, 'the request lease was not released'
    assert_clean(c, holders)


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['enforce', 'queue'])
async def test_in_flight_receive_is_settled_after_a_bodyless_response(harness, monkeypatch,
                                                                      mode):
    # A GET: the watcher took the empty body and is parked in the next
    # receive (it waits for a disconnect). The app never reads; after the
    # response the in-flight receive is cancelled, not leaked.
    c = harness.install(mode, mcp_concurrency_transport_wait_seconds=5)
    authenticated(monkeypatch)
    wire = Wire(body(b'', False))
    holders = await hold(c, 'auth')
    watches = []
    original = auth.ReceiveWatch.__init__

    def spy(self, receive):
        original(self, receive)
        watches.append(self)

    monkeypatch.setattr(auth.ReceiveWatch, '__init__', spy)
    task = asyncio.create_task(auth.APIKeyMiddleware(ok_app)(
        scope(method='GET'), wire.receive, record(harness)))
    await until(lambda: c.pending and wire.delivered == 1)
    # The second receive: parked in the transport, waiting for a disconnect.
    await until(lambda: watches[0].messages and watches[0]._pending is not None)
    pending = watches[0]._pending
    assert not pending.done()
    holders.pop().lease.release()
    await asyncio.wait_for(task, 2)
    await asyncio.sleep(0)
    assert harness.sent[0]['status'] == 200
    assert pending.done() and pending.cancelled()
    assert watches[0]._pending is None
    assert_clean(c, holders)


@pytest.mark.asyncio
async def test_in_flight_disconnect_is_delivered_to_the_app(harness, monkeypatch):
    c = harness.install('enforce', mcp_concurrency_transport_wait_seconds=5)
    authenticated(monkeypatch)
    wire = Wire(body(b'{}', False))
    holders = await hold(c, 'auth')
    received = []

    async def app(scope_, receive, send):
        received.append(await receive())
        # The watcher's receive past the complete body is still in flight;
        # the next call awaits that same call, which yields the disconnect.
        wire.push(DISCONNECT)
        received.append(await receive())

    task = asyncio.create_task(auth.APIKeyMiddleware(app)(scope(), wire.receive,
                                                           record(harness)))
    await until(lambda: c.pending and wire.delivered == 1)
    for _ in range(5):
        await asyncio.sleep(0)
    holders.pop().lease.release()
    await asyncio.wait_for(task, 2)
    assert received == [body(b'{}', False), DISCONNECT]
    assert wire.delivered == 2
    assert_clean(c, holders)


@pytest.mark.asyncio
async def test_cancelled_app_receive_keeps_the_in_flight_message(harness, monkeypatch):
    # The app's own wait on the handed-over receive is cancelled; the message
    # that arrives afterwards still reaches its next call, exactly once.
    c = harness.install('enforce', mcp_concurrency_transport_wait_seconds=5)
    authenticated(monkeypatch)
    wire = Wire()
    holders = await hold(c, 'auth')
    payload = body(b'{"id":3}', False)
    received = []

    async def app(scope_, receive, send):
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(receive(), 0.01)
        wire.push(payload)
        received.append(await receive())

    task = asyncio.create_task(auth.APIKeyMiddleware(app)(scope(), wire.receive,
                                                           record(harness)))
    await until(lambda: c.pending)
    for _ in range(3):
        await asyncio.sleep(0)
    holders.pop().lease.release()
    await asyncio.wait_for(task, 2)
    assert received == [payload]
    assert wire.delivered == 1 and wire.queue.qsize() == 0
    assert_clean(c, holders)
