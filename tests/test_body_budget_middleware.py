"""The /mcp body-memory budget wired into `APIKeyMiddleware` (#322, D3–D7).

Every spec scenario of `mcp-request-routing` that is reachable in-process: a
probe downstream app and a scripted ASGI `receive`/`send`. Disconnects are
real `http.disconnect` messages through `receive`, never `task.cancel()`
(uvicorn reports a client that left through `receive`), except where the
scenario is cancellation itself.
"""
import asyncio
from types import SimpleNamespace

import pytest
from starlette.responses import JSONResponse

from src.config import Settings
from src.mcp_server import auth, tools
from src.services import body_budget, concurrency, rate_limits
from src.services.body_budget import BodyBudget

MIB = 1024 * 1024
KIB = 1024
SECRET = "issue-322-test-secret-only-0123456789abcdef"
DISCONNECT = {"type": "http.disconnect"}


def scope(*, length=None, method="POST", token="omcp_body_budget_fixture"):
    headers = [(b"authorization", ("Bearer " + token).encode())]
    if length is not None:
        headers.append((b"content-length", str(length).encode()))
    return {"type": "http", "method": method, "path": "/mcp", "raw_path": b"/mcp",
            "query_string": b"", "scheme": "http", "server": ("test", 80),
            "client": ("127.0.0.9", 3000), "headers": headers}


def body(data, more=False):
    return {"type": "http.request", "body": data, "more_body": more}


class Wire:
    """A scripted `receive`: messages in order, then blocks (as uvicorn does)."""

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


class Probe:
    """A downstream app that records every `receive` and can be held open."""

    def __init__(self, *, hold=False, read=True, raise_after_read=False, respond=200):
        self.calls = 0
        self.received = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        if not hold:
            self.release.set()
        self.read = read
        self.raise_after_read = raise_after_read
        self.respond = respond

    async def __call__(self, scope_, receive, send):
        self.calls += 1
        self.entered.set()
        if self.read:
            while True:
                message = await receive()
                self.received.append(message)
                if message["type"] == "http.disconnect" or not message.get("more_body"):
                    break
        await self.release.wait()
        if self.raise_after_read:
            raise RuntimeError("downstream failed")
        await send({"type": "http.response.start", "status": self.respond, "headers": []})
        await send({"type": "http.response.body", "body": b""})


def received_body(probe):
    return b"".join(m.get("body", b"") for m in probe.received
                    if m["type"] == "http.request")


@pytest.fixture
def harness(monkeypatch):
    state = SimpleNamespace(usage_rows=0, events=[])

    def install_controller(mode="shadow", **kw):
        c = concurrency.Controller(Settings(_env_file=None, secret_key=SECRET,
                                            mcp_concurrency_mode=mode, **kw))
        monkeypatch.setattr(concurrency, "_controller", c)
        monkeypatch.setattr(concurrency, "_replay_budget",
                            concurrency.ReplayBudget(c.limits["replay_budget_bytes"]))
        concurrency.reset_counters()
        return c

    def install_budget(*, small=16 * MIB, large=112 * MIB, wait=1.0, waiters=8):
        b = BodyBudget(small_lane=small, large_lane=large, wait_seconds=wait,
                       waiters=waiters)
        body_budget.reset_body_budget(b)
        return b

    async def authenticated(self, request, scope_, token):
        return None

    async def no_usage(values):
        state.usage_rows += 1

    def emit(event, **fields):
        state.events.append((event, fields))

    monkeypatch.setattr(auth.APIKeyMiddleware, "_authenticate", authenticated)
    monkeypatch.setattr(tools, "_insert_usage", no_usage)
    monkeypatch.setattr(auth.settings, "mcp_sandbox_mode", False)
    monkeypatch.setattr(auth.rate_limits, "check_auth_failures", lambda *_: None)
    monkeypatch.setattr(auth.rate_limits, "record_auth_failure", lambda *_: None)
    monkeypatch.setattr(auth.security_events, "emit", emit)
    state.install_controller = install_controller
    state.install_budget = install_budget
    install_controller("shadow")
    yield state
    concurrency.reset_counters()


async def until(predicate, limit=500):
    for _ in range(limit):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never held")


def run(app, scope_, wire, sent):
    async def send(message):
        sent.append(message)
    return asyncio.create_task(auth.APIKeyMiddleware(app)(scope_, wire.receive, send))


def status(sent):
    starts = [m for m in sent if m["type"] == "http.response.start"]
    return starts[0]["status"] if starts else None


def json_body(sent):
    import json
    return json.loads(b"".join(m.get("body", b"") for m in sent
                               if m["type"] == "http.response.body"))


def headers(sent):
    start = next(m for m in sent if m["type"] == "http.response.start")
    return {k.decode().lower(): v.decode() for k, v in start["headers"]}


def body_events(state):
    return [f for e, f in state.events
            if e == "mcp_concurrency_pressure" and f.get("reason") == "body:memory"]


def assert_clean(b):
    assert b.reserved == 0
    assert b.waiting == 0
    assert concurrency.replay_budget().used == 0


# ── admission point ─────────────────────────────────────────────────────────

async def test_the_application_reads_nothing_before_the_grant(harness):
    b = harness.install_budget(wait=5)
    holder = await b.reserve(112 * MIB, small=False)
    payload = bytes(range(256)) * (40 * KIB)  # 10 MiB
    wire = Wire(body(payload[:4 * MIB], True), body(payload[4 * MIB:], False))
    probe, sent = Probe(), []
    task = run(probe, scope(length=len(payload)), wire, sent)
    await until(lambda: b.waiting == 1)
    for _ in range(20):
        await asyncio.sleep(0)
    assert probe.calls == 0 and probe.received == []
    holder.lease.release()
    await asyncio.wait_for(task, 2)
    assert received_body(probe) == payload
    assert status(sent) == 200
    assert_clean(b)


async def test_an_admitted_body_is_byte_identical(harness):
    b = harness.install_budget()
    payload = b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
    wire = Wire(body(payload[:10], True), body(payload[10:], False))
    probe, sent = Probe(), []
    await run(probe, scope(length=len(payload)), wire, sent)
    assert received_body(probe) == payload
    assert status(sent) == 200
    assert_clean(b)


async def test_an_oversized_declaration_is_refused_at_once(harness):
    b = harness.install_budget()
    limit = auth.settings.mcp_max_request_body_bytes
    probe, sent = Probe(), []
    await run(probe, scope(length=limit + 1), Wire(), sent)
    assert status(sent) == 413
    assert b"".join(m.get("body", b"") for m in sent) == b"Request body too large"
    assert probe.calls == 0
    assert_clean(b)


async def test_a_chunked_post_reserves_the_per_request_limit_from_the_large_lane(harness):
    b = harness.install_budget()
    limit = auth.settings.mcp_max_request_body_bytes
    probe, sent = Probe(hold=True), []
    task = run(probe, scope(length=None), Wire(body(b"{}")), sent)
    await asyncio.wait_for(probe.entered.wait(), 1)
    assert b.large_used == limit and b.small_used == 0
    probe.release.set()
    await asyncio.wait_for(task, 1)
    assert_clean(b)


async def test_an_unparseable_length_is_treated_as_unknown(harness):
    b = harness.install_budget()
    limit = auth.settings.mcp_max_request_body_bytes
    s = scope()
    s["headers"].append((b"content-length", b"-12"))
    probe, sent = Probe(hold=True), []
    task = run(probe, s, Wire(body(b"{}")), sent)
    await asyncio.wait_for(probe.entered.wait(), 1)
    assert b.large_used == limit
    probe.release.set()
    await asyncio.wait_for(task, 1)
    assert_clean(b)


async def test_a_small_declaration_uses_the_small_lane_and_zero_reserves_nothing(harness):
    b = harness.install_budget()
    probe, sent = Probe(hold=True), []
    task = run(probe, scope(length=4 * KIB), Wire(body(b"x" * 4 * KIB)), sent)
    await asyncio.wait_for(probe.entered.wait(), 1)
    assert b.small_used == 4 * KIB and b.large_used == 0
    probe.release.set()
    await asyncio.wait_for(task, 1)
    probe2, sent2 = Probe(), []
    await run(probe2, scope(length=0), Wire(body(b"")), sent2)
    assert status(sent2) == 200
    assert_clean(b)


async def test_more_bytes_than_reserved_are_never_delivered(harness):
    b = harness.install_budget()
    wire = Wire(body(b"a" * 600, True), body(b"b" * 600, True), body(b"c" * 800, False))
    probe, sent = Probe(), []
    await run(probe, scope(length=1000), wire, sent)
    assert [m["type"] for m in probe.received] == ["http.request", "http.disconnect"]
    assert received_body(probe) == b"a" * 600
    assert_clean(b)


async def test_after_the_cut_every_receive_is_a_disconnect(harness):
    b = harness.install_budget()
    seen = []

    async def app(scope_, receive, send):
        for _ in range(4):
            seen.append((await receive())["type"])

    await run(app, scope(length=10), Wire(body(b"x" * 20, True), body(b"y")), [])
    assert seen == ["http.disconnect"] * 4
    assert_clean(b)



async def test_a_declared_empty_body_is_counted_too(harness):
    """`Content-Length: 0` takes no reservation, but over-delivery is still cut."""
    b = harness.install_budget()
    probe, sent = Probe(), []
    await run(probe, scope(length=0), Wire(body(b"surprise", True), body(b"!")), sent)
    assert [m["type"] for m in probe.received] == ["http.disconnect"]
    assert received_body(probe) == b""
    assert_clean(b)


async def test_a_declared_empty_body_passes_an_empty_message(harness):
    b = harness.install_budget()
    probe, sent = Probe(), []
    await run(probe, scope(length=0), Wire(body(b"")), sent)
    assert [m["type"] for m in probe.received] == ["http.request"]
    assert status(sent) == 200
    assert_clean(b)

@pytest.mark.parametrize("mode", ["off", "shadow", "queue", "enforce"])
async def test_the_budget_enforces_in_every_concurrency_mode(harness, mode):
    harness.install_controller(mode)
    b = harness.install_budget(wait=0.05)
    holder = await b.reserve(112 * MIB, small=False)
    probe, sent = Probe(), []
    await run(probe, scope(length=20 * MIB), Wire(body(b"x")), sent)
    assert status(sent) == 429
    assert json_body(sent)["code"] == "body_memory"
    assert probe.calls == 0
    holder.lease.release()
    assert_clean(b)


async def test_an_unauthenticated_body_never_holds_budget(harness, monkeypatch):
    b = harness.install_budget()
    holder = await b.reserve(100 * MIB, small=False)

    async def refuse(self, request, scope_, token):
        return JSONResponse({"error": "Invalid or revoked key"}, status_code=401)

    monkeypatch.setattr(auth.APIKeyMiddleware, "_authenticate", refuse)
    probe, sent = Probe(), []
    await asyncio.wait_for(run(probe, scope(length=60 * MIB), Wire(), sent), 0.5)
    assert status(sent) == 401
    assert b.reserved == 100 * MIB and b.waiting == 0
    assert probe.calls == 0
    holder.lease.release()
    assert_clean(b)


async def test_a_missing_bearer_never_holds_budget(harness):
    b = harness.install_budget()
    s = scope(length=60 * MIB)
    s["headers"] = [(b"content-length", str(60 * MIB).encode())]
    sent = []
    await run(Probe(), s, Wire(), sent)
    assert status(sent) == 401
    assert_clean(b)


# ── methods other than POST bypass (Codex spec review finding 2) ────────────

async def test_a_long_lived_get_stream_holds_no_budget(harness):
    b = harness.install_budget(wait=0)
    stream, get_sent = Probe(hold=True, read=False), []
    get_task = run(stream, scope(length=None, method="GET"), Wire(), get_sent)
    await asyncio.wait_for(stream.entered.wait(), 1)
    assert b.reserved == 0 and b.waiting == 0
    limit = auth.settings.mcp_max_request_body_bytes
    post, post_sent = Probe(hold=True), []
    post_task = run(post, scope(length=limit), Wire(body(b"{}")), post_sent)
    await asyncio.wait_for(post.entered.wait(), 1)
    assert b.large_used == limit, "the maximum POST was not admitted at once"
    post.release.set()
    await asyncio.wait_for(post_task, 1)
    stream.release.set()
    await asyncio.wait_for(get_task, 1)
    assert_clean(b)


@pytest.mark.parametrize("method", ["GET", "DELETE", "PUT"])
async def test_non_post_methods_bypass_even_with_a_declared_length(harness, method):
    b = harness.install_budget(wait=0)
    holder = await b.reserve(112 * MIB, small=False)
    probe, sent = Probe(read=False), []
    await run(probe, scope(length=60 * MIB, method=method), Wire(), sent)
    assert status(sent) == 200 and probe.calls == 1
    assert b.reserved == 112 * MIB
    holder.lease.release()
    assert_clean(b)


# ── waiting, refusal, telemetry ─────────────────────────────────────────────

async def test_five_near_limit_requests_queue_and_refuse_within_the_budget(harness):
    b = harness.install_budget(large=40 * MIB, wait=1.0, waiters=2)
    peak = 0
    probes = [Probe(hold=True) for _ in range(5)]
    sents = [[] for _ in range(5)]
    tasks = [run(p, scope(length=20 * MIB), Wire(body(b"x" * 16)), s)
             for p, s in zip(probes, sents)]
    await until(lambda: sum(p.calls for p in probes) == 2 and b.waiting == 2
                and any(status(s) == 429 for s in sents))
    peak = max(peak, b.reserved)
    refused_now = [i for i, s in enumerate(sents) if status(s) == 429]
    assert len(refused_now) == 1
    admitted = [i for i, p in enumerate(probes) if p.calls]
    await asyncio.sleep(0.15)  # long enough for a `waited` event
    probes[admitted[0]].release.set()
    await until(lambda: sum(p.calls for p in probes) == 3)
    peak = max(peak, b.reserved)
    assert b.waiting == 1
    # The last waiter's 1 s deadline expires: a body_memory 429.
    await asyncio.wait_for(asyncio.gather(*(t for i, t in enumerate(tasks)
                                            if not probes[i].calls and i not in refused_now)), 3)
    refused = [i for i, s in enumerate(sents) if status(s) == 429]
    assert len(refused) == 2
    for i in refused:
        payload = json_body(sents[i])
        assert payload["code"] == "body_memory" and payload["scope"] == "large"
        assert payload["limit"] == 40 * MIB
        assert headers(sents[i])["retry-after"] == "2"
    for p in probes:
        p.release.set()
    await asyncio.wait_for(asyncio.gather(*tasks), 2)
    assert peak <= 40 * MIB
    assert_clean(b)
    outcomes = [e["outcome"] for e in body_events(harness)]
    assert outcomes.count("refused") == 2
    assert outcomes.count("waited") == 1


async def test_a_refusal_consumes_nothing_durable(harness):
    rate_limits.reset_state_for_tests()
    b = harness.install_budget(wait=0)
    holder = await b.reserve(112 * MIB, small=False)
    concurrency.counters().drain()
    probe, sent = Probe(), []
    await run(probe, scope(length=20 * MIB), Wire(body(b"x")), sent)
    assert status(sent) == 429
    assert probe.calls == 0, "_tracked never ran: no token, no quota, no row"
    assert rate_limits.tracked_principals() == 0
    assert harness.usage_rows == 0
    event = body_events(harness)[0]
    assert event["outcome"] == "refused" and event["limit_count"] == 112 * MIB
    holder.lease.release()
    assert_clean(b)
    # Not fed to the concurrency durable counters as a transport refusal.
    drained = concurrency.counters().drain()
    metrics = {metric for (_, metric) in drained}
    assert "transport_refused" not in metrics


async def test_the_429_has_the_concurrency_429_keys(harness):
    b = harness.install_budget(wait=0)
    holder = await b.reserve(112 * MIB, small=False)
    sent = []
    await run(Probe(), scope(length=20 * MIB), Wire(body(b"x")), sent)
    body_keys = set(json_body(sent))
    pressure = concurrency.Pressure("request", "global", 64)
    concurrency_429 = auth._concurrency_response(concurrency.Admission(None, pressure))
    import json
    assert body_keys == set(json.loads(concurrency_429.body))
    assert json_body(sent)["error"] == "MCP request body memory budget is unavailable"
    holder.lease.release()
    assert_clean(b)


async def test_a_disconnect_while_waiting_releases_the_waiter(harness):
    b = harness.install_budget(wait=5)
    holder = await b.reserve(112 * MIB, small=False)
    wire = Wire(body(b"x" * 1000, True))
    probe, sent = Probe(), []
    task = run(probe, scope(length=20 * MIB), wire, sent)
    await until(lambda: b.waiting == 1 and concurrency.replay_budget().used == 1000)
    wire.push(DISCONNECT)
    await asyncio.wait_for(task, 1)
    assert sent == [] and probe.calls == 0
    assert b.waiting == 0
    holder.lease.release()
    assert_clean(b)


# ── release on every exit ───────────────────────────────────────────────────

async def test_sdk_validation_failure_releases(harness):
    b = harness.install_budget()
    probe, sent = Probe(respond=400), []
    await run(probe, scope(length=7), Wire(body(b"{not js")), sent)
    assert status(sent) == 400
    assert_clean(b)


async def test_a_raising_application_releases_and_admits_the_next(harness):
    b = harness.install_budget(large=40 * MIB, wait=5)
    failing = Probe(hold=True, raise_after_read=True)
    t1 = run(failing, scope(length=30 * MIB), Wire(body(b"x")), [])
    await asyncio.wait_for(failing.entered.wait(), 1)
    nxt, sent = Probe(), []
    t2 = run(nxt, scope(length=30 * MIB), Wire(body(b"y")), sent)
    await until(lambda: b.waiting == 1)
    failing.release.set()
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(t1, 1)
    await asyncio.wait_for(t2, 1)
    assert status(sent) == 200 and nxt.calls == 1
    assert_clean(b)


async def test_cancellation_while_the_application_runs_releases(harness):
    b = harness.install_budget()
    probe = Probe(hold=True)
    task = run(probe, scope(length=30 * MIB), Wire(body(b"x")), [])
    await asyncio.wait_for(probe.entered.wait(), 1)
    assert b.reserved == 30 * MIB
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert_clean(b)


async def test_cancellation_while_waiting_releases(harness):
    b = harness.install_budget(wait=5)
    holder = await b.reserve(112 * MIB, small=False)
    wire = Wire(body(b"x" * 2048, True))
    task = run(Probe(), scope(length=30 * MIB), wire, [])
    await until(lambda: b.waiting == 1 and wire.delivered == 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)
    assert b.waiting == 0
    holder.lease.release()
    assert_clean(b)


# ── watcher teardown (Codex spec review finding 3) ──────────────────────────

def _pending_receives():
    return [t for t in asyncio.all_tasks()
            if not t.done() and "Wire.receive" in repr(t.get_coro())]


@pytest.mark.parametrize("exit_", ["disconnect", "cancel"])
async def test_watchers_leave_nothing_behind_before_the_handoff(harness, exit_):
    harness.install_controller("enforce", mcp_concurrency_transport_wait_seconds=5)
    b = harness.install_budget(wait=5)
    holder = await b.reserve(112 * MIB, small=False)
    replay_before = concurrency.replay_budget().used
    wire = Wire(body(b"a" * 1500, True), body(b"b" * 500, True))
    task = run(Probe(), scope(length=20 * MIB), wire, [])
    await until(lambda: b.waiting == 1
                and concurrency.replay_budget().used == replay_before + 2000)
    assert _pending_receives(), "the body watcher should be blocked in receive"
    if exit_ == "disconnect":
        wire.push(DISCONNECT)
        await asyncio.wait_for(task, 1)
    else:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    for _ in range(5):
        await asyncio.sleep(0)
    assert concurrency.replay_budget().used == replay_before
    assert _pending_receives() == []
    holder.lease.release()
    assert_clean(b)



def _pending_reads():
    """Pending reads through either layer: `Wire.receive` or a watcher's replay."""
    return [t for t in asyncio.all_tasks()
            if not t.done() and any(name in repr(t.get_coro())
                                    for name in ("Wire.receive", "downstream.<locals>.replay"))]


@pytest.mark.parametrize("exit_", ["disconnect", "cancel"])
async def test_both_watchers_leave_nothing_behind_before_the_handoff(harness, exit_):
    """The transport watcher (a held auth slot) and the body watcher both read
    bytes; the request then ends before the handoff. Every replay byte comes
    back and no `receive` is left pending."""
    c = harness.install_controller("enforce", mcp_concurrency_transport_wait_seconds=5)
    b = harness.install_budget(wait=5)
    auth_holders = [await c.auth() for _ in range(c.limits["auth"])]
    body_holder = await b.reserve(112 * MIB, small=False)
    replay_before = concurrency.replay_budget().used
    wire = Wire(body(b"a" * 1500, True))
    task = run(Probe(), scope(length=20 * MIB), wire, [])
    await until(lambda: c.pending and wire.delivered == 1
                and concurrency.replay_budget().used >= replay_before + 1500)
    auth_holders.pop().lease.release()
    await until(lambda: b.waiting == 1)
    wire.push(body(b"b" * 500, True))
    await until(lambda: wire.delivered == 2
                and concurrency.replay_budget().used >= replay_before + 2000)
    # The body watcher's in-flight read is the transport watcher's replay,
    # now past its list and calling `Wire.receive` directly.
    assert _pending_reads(), "the body watcher should be blocked in receive"
    if exit_ == "disconnect":
        wire.push(DISCONNECT)
        await asyncio.wait_for(task, 1)
    else:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    for _ in range(5):
        await asyncio.sleep(0)
    assert concurrency.replay_budget().used == replay_before
    assert _pending_reads() == [] and _pending_receives() == []
    body_holder.lease.release()
    for h in auth_holders:
        h.lease.release()
    assert_clean(b)

async def test_waiting_behind_a_transport_watcher_replays_in_order(harness):
    """A request that waited at the auth stage *and* for body budget: both
    watchers read some of the body, and the app sees it intact, once."""
    c = harness.install_controller("enforce", mcp_concurrency_transport_wait_seconds=5)
    b = harness.install_budget(wait=5)
    auth_holders = [await c.auth() for _ in range(c.limits["auth"])]
    body_holder = await b.reserve(112 * MIB, small=False)
    small_holder = await b.reserve(16 * MIB, small=True)
    payload = [b"one-", b"two-", b"three"]
    wire = Wire(body(payload[0], True))
    probe, sent = Probe(), []
    task = run(probe, scope(length=13), wire, sent)
    await until(lambda: c.pending and wire.delivered == 1)
    auth_holders.pop().lease.release()
    await until(lambda: b.waiting == 1)
    wire.push(body(payload[1], True))
    await until(lambda: wire.delivered == 2)
    small_holder.lease.release()
    body_holder.lease.release()
    wire.push(body(payload[2], False))
    await asyncio.wait_for(task, 2)
    assert received_body(probe) == b"".join(payload)
    assert status(sent) == 200
    for h in auth_holders:
        h.lease.release()
    assert_clean(b)
