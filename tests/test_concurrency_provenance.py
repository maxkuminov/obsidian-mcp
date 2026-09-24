"""Row provenance, queue-mode annotation and the refusal contract (#188).

Every row `_tracked` writes, on every path, carries
`params.concurrency = {v: 2, mode, epoch}` whenever the mode is not `off`, so
the readiness evaluator can exclude legacy rows and mixed windows.
"""
import asyncio
import json

import pytest

from src.auth.session import current_principal, current_user_id
from src.config import Settings
from src.mcp_server import auth, tools
from src.services import concurrency, rate_limits, refusals
from src.services.tool_outcomes import body_refusal


@pytest.fixture
def configured(monkeypatch):
    rows, quota_calls = [], []

    def install(mode='enforce', **kw):
        controller = concurrency.Controller(Settings(
            _env_file=None, secret_key='issue-188-test-secret-only-0123456789abcdef',
            mcp_concurrency_mode=mode, **kw))
        monkeypatch.setattr(concurrency, '_controller', controller)
        concurrency.reset_counters()
        return controller

    async def insert(values):
        rows.append(values)

    async def quota():
        quota_calls.append(True)

    monkeypatch.setattr(tools, '_insert_usage', insert)
    monkeypatch.setattr(tools, '_bucket_admission', lambda write: None)
    monkeypatch.setattr(tools, '_vault_admission_error', lambda: None)
    monkeypatch.setattr(tools, '_quota_admission_error', quota)
    monkeypatch.setattr(tools.security_events, 'emit', lambda *a, **kw: None)
    principal = current_principal.set(('oauth', 'provenance-grant'))
    tenant = current_user_id.set(9)
    transport = concurrency.request_observations.set(())
    rate_limits.reset_state_for_tests()
    yield install, rows, quota_calls
    rate_limits.reset_state_for_tests()
    concurrency.reset_counters()
    concurrency.request_observations.reset(transport)
    current_user_id.reset(tenant)
    current_principal.reset(principal)


@tools._tracked('provenance_probe', [], resource_class='light')
async def probe(started, finish, *, explode=False, refused=False):
    started.set()
    await finish.wait()
    if explode:
        raise RuntimeError('body failure')
    return body_refusal('body refused', 'not_found') if refused else 'done'


body_runs = []


@tools._tracked('write_probe', [], write_class=True, resource_class='write')
async def write_probe():
    body_runs.append(True)
    return 'written'


def now():
    e = asyncio.Event()
    e.set()
    return e


def expected(controller):
    return {'v': 2, 'mode': controller.mode, 'epoch': controller.epoch}


def drained():
    out = {}
    for (_, metric), (count, _) in concurrency.counters().drain().items():
        out[metric] = out.get(metric, 0) + count
    return out


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['shadow', 'queue', 'enforce'])
async def test_unpressured_row_carries_provenance_only(configured, mode):
    install, rows, _ = configured
    c = install(mode)
    assert await probe(asyncio.Event(), now()) == 'done'
    params = rows[0]['params']
    assert params['concurrency'] == expected(c)
    assert len(c.epoch) == 12 and int(c.epoch, 16) >= 0
    assert 'concurrency_queue' not in params and 'concurrency_shadow' not in params
    if mode in ('queue', 'enforce'):
        assert params['queue_ms'] == 0 and params['transport_queue_ms'] == 0
    else:
        assert 'transport_queue_ms' not in params


@pytest.mark.asyncio
async def test_off_mode_rows_carry_no_provenance(configured):
    install, rows, _ = configured
    install('off')
    await probe(asyncio.Event(), now())
    assert 'concurrency' not in rows[0]['params']


@pytest.mark.asyncio
async def test_rate_limited_row_carries_provenance(configured, monkeypatch):
    install, rows, quotas = configured
    c = install('queue')
    monkeypatch.setattr(tools, '_bucket_admission', lambda write: ('principal', 3))
    result = await probe(asyncio.Event(), now())
    assert '"code":"rate_limited"' in result
    assert rows[0]['params']['error'] == 'rate_limited'
    assert rows[0]['params']['concurrency'] == expected(c)
    assert quotas == []


@pytest.mark.asyncio
async def test_coalesced_slot_timeout_rows_carry_provenance(configured):
    install, rows, quotas = configured
    c = install(mcp_concurrency_light=1, mcp_concurrency_wait_seconds=0)
    entered, finish = asyncio.Event(), asyncio.Event()
    held = asyncio.create_task(probe(entered, finish))
    await entered.wait()
    for _ in range(3):
        result = await probe(asyncio.Event(), now())
        assert '"code":"slot_timeout"' in result
    finish.set()
    await held
    await rate_limits.flush_all()
    refused = [r for r in rows if r['params'].get('error') == 'slot_timeout']
    assert sum(1 + r['params'].get('suppressed', 0) for r in refused) == 3
    assert all(r['params']['concurrency'] == expected(c) for r in rows)
    assert len(quotas) == 1


@pytest.mark.asyncio
async def test_over_quota_and_tool_exception_rows_carry_provenance(configured, monkeypatch):
    install, rows, _ = configured
    c = install('shadow')
    with pytest.raises(RuntimeError):
        await probe(asyncio.Event(), now(), explode=True)
    assert rows[-1]['params']['error'] == 'tool_exception'
    assert rows[-1]['params']['concurrency'] == expected(c)

    async def deny():
        return 'over quota'
    monkeypatch.setattr(tools, '_quota_admission_error', deny)
    entered = asyncio.Event()
    assert await probe(entered, now()) == 'over quota'
    assert not entered.is_set()
    assert rows[-1]['params']['over_quota'] is True
    assert rows[-1]['params']['concurrency'] == expected(c)


@pytest.mark.asyncio
async def test_writer_merged_shadow_row_keeps_provenance(configured):
    install, rows, _ = configured
    c = install('shadow')
    holder = await c.writer()
    try:
        await probe(asyncio.Event(), now())
    finally:
        holder.lease.release()
    params = rows[0]['params']
    assert params['concurrency'] == expected(c)
    shadow = params['concurrency_shadow']
    assert shadow['code'] == 'writer_concurrency_limited'
    assert shadow['schema'] == 2
    assert shadow['configured_wait_ms'] == {'transport': 2000, 'tool': 5000}


@pytest.mark.asyncio
async def test_queue_overrun_then_quota_refusal_stays_over_quota(configured, monkeypatch):
    install, rows, quotas = configured
    c = install('queue', mcp_concurrency_light=1, mcp_concurrency_wait_seconds=0.02)
    entered, finish = asyncio.Event(), asyncio.Event()
    held = asyncio.create_task(probe(entered, finish))
    await entered.wait()

    async def deny():
        # The quota gate refuses: nothing is consumed.
        return quotas_refusal
    quotas_refusal = 'Error: over quota'
    monkeypatch.setattr(tools, '_quota_admission_error', deny)
    ran = asyncio.Event()
    result = await probe(ran, now())
    assert result == quotas_refusal and not ran.is_set()
    assert c.tools.active == 1, 'the overrun lease was released with the refusal'
    row = rows[-1]['params']
    assert row['over_quota'] is True and 'error' not in row
    queue = row['concurrency_queue']
    assert queue['overrun'] is True and queue['code'] == 'slot_timeout'
    assert queue['observations'][-1]['stage'] == 'tool'
    assert queue['observations'][-1]['waited_ms'] >= 10
    assert row['queue_ms'] >= 10
    assert not any(r['params'].get('error') == 'slot_timeout' for r in rows)
    finish.set()
    await held


@pytest.mark.asyncio
async def test_queue_overrun_executes_and_is_annotated(configured):
    install, rows, quotas = configured
    c = install('queue', mcp_concurrency_light=1, mcp_concurrency_wait_seconds=0.02)
    entered, finish = asyncio.Event(), asyncio.Event()
    held = asyncio.create_task(probe(entered, finish))
    await entered.wait()
    assert await probe(asyncio.Event(), now()) == 'done'
    assert len(quotas) == 2
    queue = rows[-1]['params']['concurrency_queue']
    assert queue['overrun'] is True and queue['code'] == 'slot_timeout'
    finish.set()
    await held
    assert c.tools.active == 0


@pytest.mark.asyncio
async def test_queue_ordinary_wait_is_measured_and_sets_no_code(configured):
    install, rows, _ = configured
    c = install('queue', mcp_concurrency_light=1, mcp_concurrency_wait_seconds=5)
    entered, finish = asyncio.Event(), asyncio.Event()
    held = asyncio.create_task(probe(entered, finish))
    await entered.wait()
    waiting = asyncio.create_task(probe(asyncio.Event(), now()))
    await asyncio.sleep(0.04)
    finish.set()
    await held
    assert await waiting == 'done'
    params = rows[-1]['params']
    assert params['queue_ms'] >= 30
    queue = params['concurrency_queue']
    assert queue['overrun'] is False and queue['code'] is None
    assert queue['observations'][0]['overrun'] is False
    assert params['concurrency'] == expected(c)


@pytest.mark.asyncio
async def test_queue_transport_wait_then_tool_overrun_codes_the_overrun(configured):
    install, rows, _ = configured
    install('queue', mcp_concurrency_light=1, mcp_concurrency_wait_seconds=0.02)
    auth_wait = concurrency.Pressure('auth', 'global', 2, waited_ms=40)
    token = concurrency.request_observations.set((auth_wait,))
    transport = auth.current_transport_queue_ms.set(40.0)
    entered, finish = asyncio.Event(), asyncio.Event()
    try:
        held = asyncio.create_task(probe(entered, finish))
        await entered.wait()
        await probe(asyncio.Event(), now())
    finally:
        auth.current_transport_queue_ms.reset(transport)
        concurrency.request_observations.reset(token)
    params = rows[-1]['params']
    queue = params['concurrency_queue']
    assert queue['code'] == 'slot_timeout'
    assert queue['observations'][0] == {'stage': 'auth', 'scope': 'global', 'limit': 2,
                                        'waited_ms': 40, 'overrun': False}
    assert params['transport_queue_ms'] == 40.0
    finish.set()
    await held


@pytest.mark.asyncio
async def test_queue_writer_overrun_keeps_the_row_and_counts(configured):
    install, rows, _ = configured
    c = install('queue', mcp_concurrency_writer_wait_seconds=0.02)
    holder = await c.writer()
    try:
        assert await tools.write_usage_row(dict(tool='probe', params={'x': 1}, user_id=9))
    finally:
        holder.lease.release()
    assert rows[0]['params']['concurrency_queue']['code'] == 'writer_concurrency_limited'
    assert drained().get('writer_overrun') == 1


@pytest.mark.asyncio
async def test_enforce_writer_refusal_is_counted(configured):
    install, rows, _ = configured
    c = install('enforce', mcp_concurrency_writer_wait_seconds=0.02)
    holder = await c.writer()
    try:
        assert await tools.write_usage_row(dict(tool='probe', params={}, user_id=9)) is False
    finally:
        holder.lease.release()
    assert rows == []
    assert drained().get('writer_refused') == 1


@pytest.mark.asyncio
async def test_enforce_slot_refusal_spends_both_tokens_and_no_quota(configured, monkeypatch):
    # D9: the non-consumption guarantee covers the durable daily quota only.
    install, rows, quotas = configured
    c = install(mcp_concurrency_write=1, mcp_concurrency_wait_seconds=0)
    monkeypatch.setattr(tools, '_bucket_admission', REAL_BUCKET_ADMISSION)
    principal = current_principal.get()
    entered, finish = asyncio.Event(), asyncio.Event()
    held = asyncio.create_task(blocking_write(entered, finish))
    await entered.wait()
    entry = rate_limits._entries[principal]
    general, write = entry.general.tokens, entry.write.tokens
    body_runs.clear()
    result = await write_probe()
    assert '"code":"slot_timeout"' in result and body_runs == []
    # Both tokens were spent by the buckets-first gates and are not refunded.
    assert entry.general.tokens < general - 0.9
    assert entry.write.tokens < write - 0.9
    assert len(quotas) == 1, 'the refused call reached the quota gate'
    finish.set()
    await held
    assert c.tools.active == 0


REAL_BUCKET_ADMISSION = tools._bucket_admission


@tools._tracked('blocking_write_probe', [], write_class=True, resource_class='write')
async def blocking_write(started, finish):
    started.set()
    await finish.wait()
    return 'written'


@pytest.mark.asyncio
async def test_mcp_refusal_line_is_unchanged(configured):
    install, _, _ = configured
    install(mcp_concurrency_light=1, mcp_concurrency_wait_seconds=0)
    c = concurrency.get_controller()
    entered, finish = asyncio.Event(), asyncio.Event()
    held = asyncio.create_task(probe(entered, finish))
    await entered.wait()
    result = await probe(asyncio.Event(), now())
    last = result.splitlines()[-1]
    assert last == ('MCP-REFUSAL {"code":"slot_timeout","scope":"light","limit":1,'
                    '"limit_unit":"concurrent_calls"}')
    assert json.loads(last.removeprefix('MCP-REFUSAL '))['code'] == refusals.SLOT_TIMEOUT
    assert result.count('MCP-REFUSAL') == 1
    finish.set()
    await held
    assert c.tools.active == 0
