"""Production decorator/writer seams, beyond the pure controller's lattice."""
import asyncio
import json

import pytest

from src.auth.session import current_principal, current_user_id
from src.config import Settings
from src.mcp_server import tools
from src.services import concurrency, rate_limits, refusals
from src.services.tool_outcomes import body_refusal


@pytest.fixture
def configured(monkeypatch):
    rows, events, quota_calls = [], [], []
    def install(mode='enforce', **kw):
        controller = concurrency.Controller(Settings(
            _env_file=None, secret_key='issue-261-test-secret-only-0123456789abcdef',
            mcp_concurrency_mode=mode, **kw))
        monkeypatch.setattr(concurrency, '_controller', controller)
        return controller
    async def insert(values): rows.append(values)
    async def quota(): quota_calls.append(True)
    monkeypatch.setattr(tools, '_insert_usage', insert)
    monkeypatch.setattr(tools, '_bucket_admission', lambda write: None)
    monkeypatch.setattr(tools, '_vault_admission_error', lambda: None)
    monkeypatch.setattr(tools, '_quota_admission_error', quota)
    monkeypatch.setattr(tools.security_events, 'emit', lambda event, **kw: events.append((event, kw)))
    principal = current_principal.set(('oauth', 'stable-grant'))
    tenant = current_user_id.set(7)
    transport = concurrency.request_observations.set(())
    rate_limits.reset_state_for_tests()
    yield install, rows, events, quota_calls
    rate_limits.reset_state_for_tests()
    concurrency.request_observations.reset(transport)
    current_user_id.reset(tenant)
    current_principal.reset(principal)


@tools._tracked('occupancy_probe', [], resource_class='other')
async def probe(started, finish, *, refused=False, explode=False):
    started.set()
    await finish.wait()
    if explode: raise RuntimeError('body failure')
    return body_refusal('body refused', 'not_found') if refused else 'success'


def decision(result):
    message = result.error if hasattr(result, 'error') else result
    return json.loads(message.splitlines()[-1].removeprefix('MCP-REFUSAL '))


@pytest.mark.asyncio
async def test_enforced_structured_refusal_spends_no_quota_and_is_coalesced(configured):
    install, rows, _, quotas = configured
    c = install()
    entered, finish = asyncio.Event(), asyncio.Event()
    held = asyncio.create_task(probe(entered, finish))
    await entered.wait()
    assert c.tools.active == 1
    for _ in range(2):
        result = await tools.read_note_impl('absent.md')
        assert decision(result)['code'] == 'slot_timeout'
        assert decision(result)['limit_unit'] == 'concurrent_calls'
        assert 'retry_after_seconds' not in decision(result)
        assert result._body_outcome is None
    assert len(quotas) == 1
    assert len(rows) == 1 and rows[0]['params']['error'] == 'slot_timeout'
    assert rows[0]['params']['queue_ms'] == 0
    finish.set()
    assert await held == 'success'
    await rate_limits.flush_all()
    refused = [r for r in rows if r['params'].get('error') == 'slot_timeout']
    assert sum(1+r['params'].get('suppressed', 0) for r in refused) == 2
    assert c.tools.active == c.writers.active == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('explode', [False, True])
async def test_shadow_preserves_actual_body_error_and_transport_observation(configured, explode):
    install, rows, _, quotas = configured
    c = install('shadow')
    entered, finish = asyncio.Event(), asyncio.Event()
    held = asyncio.create_task(probe(entered, finish))
    await entered.wait()
    token = concurrency.request_observations.set((concurrency.Pressure('auth', 'global', 2),))
    ready, immediate = asyncio.Event(), asyncio.Event()
    immediate.set()
    try:
        if explode:
            with pytest.raises(RuntimeError): await probe(ready, immediate, explode=True)
        else:
            result = await probe(ready, immediate, refused=True)
            assert result.marker == 'not_found'
    finally: concurrency.request_observations.reset(token)
    assert len(quotas) == 2
    assert len(rows) == 1
    params = rows[0]['params']
    assert params['error'] == ('tool_exception' if explode else 'not_found')
    assert params.get('body_outcome') == (None if explode else 'refused')
    shadow = params['concurrency_shadow']
    assert shadow['shadow'] and shadow['basis'] == 'observed_occupancy_zero_wait'
    assert {o['stage'] for o in shadow['observations']} == {'auth', 'tool'}
    assert params['queue_ms'] == 0
    finish.set()
    await held
    assert c.tools.active == 0 and not c.pending


@pytest.mark.asyncio
async def test_quota_refusal_releases_admitted_lease(configured, monkeypatch):
    install, rows, _, _ = configured
    c = install()
    async def deny(): return 'over quota'
    monkeypatch.setattr(tools, '_quota_admission_error', deny)
    entered = asyncio.Event()
    assert await probe(entered, asyncio.Event()) == 'over quota'
    assert not entered.is_set()
    assert rows[0]['params']['over_quota'] is True
    assert c.tools.active == 0


@pytest.mark.asyncio
async def test_wait_cancellation_releases_registry_without_spending_quota(configured):
    install, rows, _, quotas = configured
    c = install(mcp_concurrency_wait_seconds=1)
    entered, finish = asyncio.Event(), asyncio.Event()
    held = asyncio.create_task(probe(entered, finish))
    await entered.wait()
    queued = asyncio.create_task(probe(asyncio.Event(), asyncio.Event()))
    await asyncio.sleep(0)
    assert len(c.pending) == 1
    queued.cancel()
    with pytest.raises(asyncio.CancelledError): await queued
    assert not c.pending and c.tools.active == 1 and len(quotas) == 1
    assert rows == []
    finish.set(); await held
    assert c.tools.active == 0 and not c.tenants.entries and not c.principals.entries


@pytest.mark.asyncio
async def test_wait_timeout_records_actual_wait_and_no_quota(configured):
    install, rows, _, quotas = configured
    c = install(mcp_concurrency_wait_seconds=.02)
    entered, finish = asyncio.Event(), asyncio.Event()
    held = asyncio.create_task(probe(entered, finish)); await entered.wait()
    result = await probe(asyncio.Event(), asyncio.Event())
    assert decision(result)['code'] == 'slot_timeout'
    assert rows[0]['params']['queue_ms'] >= 10
    assert len(quotas) == 1
    finish.set(); await held
    assert c.tools.active == 0


@pytest.mark.asyncio
async def test_lease_remains_held_during_usage_tail_and_releases_on_cancel(configured, monkeypatch):
    install, _, _, _ = configured
    c = install()
    logging, finish_log = asyncio.Event(), asyncio.Event()
    async def blocked(values):
        assert c.tools.active == c.writers.active == 1
        logging.set()
        await finish_log.wait()
    monkeypatch.setattr(tools, '_insert_usage', blocked)
    done = asyncio.Event(); done.set()
    task = asyncio.create_task(probe(asyncio.Event(), done))
    await logging.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert c.tools.active == c.writers.active == 0


@pytest.mark.asyncio
async def test_writer_limit_covers_whole_fk_retry_and_precedes_insert(configured, monkeypatch):
    install, _, events, _ = configured
    c = install(mcp_concurrency_writer_wait_seconds=.02)
    retry_entered, release = asyncio.Event(), asyncio.Event()
    inserts=[]
    async def insert(values):
        assert c.writers.active == 1
        inserts.append(values)
        if len(inserts)==1: raise RuntimeError('simulated deleted credential')
        retry_entered.set(); await release.wait()
    monkeypatch.setattr(tools, '_insert_usage', insert)
    monkeypatch.setattr(tools, '_is_fk_violation', lambda exc: True)
    monkeypatch.setattr(tools, '_violated_user_fk', lambda exc: False)
    first = asyncio.create_task(tools.write_usage_row(dict(tool='probe', params={}, user_id=7, key_id=4)))
    await retry_entered.wait()
    assert await tools.write_usage_row(dict(tool='other', params={}, user_id=7)) is False
    assert len(inserts)==2 and inserts[1]['key_id'] is None
    assert any(e=='usage_log_failed' and p['reason']=='concurrency_capacity' for e,p in events)
    release.set(); assert await first is True
    assert c.writers.active==0


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel', [False, True])
async def test_slot_coalescer_preserves_weight_on_writer_refusal_or_cancel(configured, cancel):
    install, rows, _, _ = configured
    c = install(mcp_concurrency_writer_wait_seconds=.02)
    holder = await c.writer()
    values = dict(tool='probe', params={'error':'slot_timeout'}, user_id=None, key_id=None, oauth_token_id=None, duration_ms=0, response_size=0)
    planned = rate_limits.record_rate_refusal(current_principal.get(), 'probe', 'slot_timeout', 'other', lambda: values)
    task = asyncio.create_task(rate_limits.write_planned_row(planned))
    if cancel:
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
    else:
        assert await task is False
    assert not rows
    holder.lease.release()
    await rate_limits.flush_all()
    assert sum(1+r['params'].get('suppressed',0) for r in rows)==1
    assert c.writers.active==0 and not c.pending


@pytest.mark.asyncio
async def test_shadow_writer_pressure_appends_to_same_real_row(configured):
    install, rows, _, _ = configured
    c = install('shadow')
    holder = await c.writer()
    params = {'error':'not_found', 'body_outcome':'refused',
              'concurrency_shadow': concurrency.shadow_metadata((concurrency.Pressure('tool','other',1),))}
    assert await tools.write_usage_row(dict(tool='probe', params=params, user_id=7))
    assert len(rows)==1 and rows[0]['params']['error']=='not_found'
    assert {x['stage'] for x in rows[0]['params']['concurrency_shadow']['observations']}=={'tool','writer'}
    assert params['concurrency_shadow']['observations']==[{'stage':'tool','scope':'other','limit':1}]
    holder.lease.release()


def test_every_registered_tool_class_matches_wrapper_and_write_class():
    import ast
    import inspect
    tree=ast.parse(inspect.getsource(tools))
    seen={}
    for node in tree.body:
        if not isinstance(node,ast.AsyncFunctionDef):continue
        for d in node.decorator_list:
            if isinstance(d,ast.Call) and isinstance(d.func,ast.Name) and d.func.id=='_tracked':
                name=d.args[0].value
                write=any(k.arg=='write_class' and isinstance(k.value,ast.Constant) and k.value.value for k in d.keywords)
                cls=concurrency.TOOL_CLASSES[name]
                assert (cls=='write') is write
                assert getattr(tools,node.name).__concurrency_class__==cls
                seen[name]=cls
    assert seen==concurrency.TOOL_CLASSES
