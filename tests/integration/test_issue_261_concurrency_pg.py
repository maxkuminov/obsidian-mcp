"""Real checkouts and persisted outcomes for the MCP concurrency boundary.

Only the HTTP fetch body is synthetic; auth, quota, publication confirmation,
move metadata, import publish locks and usage INSERT/FK retry use PostgreSQL.
A session subclass adds deterministic pauses *after* real statements/flushes.
It does not replace SQL results or the production admission/usage functions.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import _harness
import src.database as database
from src.auth.session import current_principal, current_user_id, current_vault_root
from src.config import Settings, settings
from src.mcp_server import auth, tools
from src.models.db import APIKey, UsageLog, User
from src.services import concurrency, quotas, rate_limits, transfer, vault_overlap
from src.services.pool_budget import POOL_SIZE, POOL_OVERFLOW, TOOL_CONNECTION_MULTIPLIER
from src.services.tool_outcomes import body_refusal
from src.services.usage_stats import PRE_BODY_REFUSAL_BINDS, executed_sql, pre_body_refusal_sql

pytestmark = [_harness.requires_pgvector, pytest.mark.asyncio(loop_scope='module')]


@dataclass
class Meter:
    active: int = 0
    peak: int = 0
    checkouts: int = 0
    by_task: Counter = field(default_factory=Counter)
    task_peaks: Counter = field(default_factory=Counter)
    statements: Counter = field(default_factory=Counter)

    def checkout(self, connection, record, proxy):
        owner = asyncio.current_task()
        record.info['_261_owner'] = owner
        self.active += 1
        self.checkouts += 1
        self.peak = max(self.peak, self.active)
        self.by_task[owner] += 1
        self.task_peaks[owner] = max(self.task_peaks[owner], self.by_task[owner])

    def checkin(self, connection, record):
        self.active -= 1
        self.by_task[record.info.pop('_261_owner')] -= 1

    def statement(self, connection, cursor, statement, parameters, context, executemany):
        lowered = statement.lower()
        for name in ('api_keys', 'users', 'quota_counters', 'usage_logs', 'notes_metadata'):
            if name in lowered:
                self.statements[name] += 1

    def reset(self):
        assert self.active == 0
        self.peak = self.checkouts = 0
        self.by_task.clear()
        self.task_peaks.clear()
        self.statements.clear()


@pytest.fixture(scope='module')
def migrated_url():
    yield from _harness.throwaway_database('concurrency_261', 64)


@pytest_asyncio.fixture(scope='module', loop_scope='module')
async def postgres(migrated_url):
    meter = Meter()
    gate = SimpleNamespace(auth=False, writer=False, reached=0, expected=1,
                           entered=None, release=None)

    class ObservedSession(AsyncSession):
        async def execute(self, statement, *args, **kwargs):
            result = await super().execute(statement, *args, **kwargs)
            if gate.auth and 'from api_keys' in str(statement).lower():
                gate.reached += 1
                if gate.reached >= gate.expected:
                    gate.entered.set()
                await gate.release.wait()
            return result

        async def commit(self):
            is_usage = any(isinstance(row, UsageLog) for row in self.new)
            if gate.writer and is_usage:
                await self.flush()  # a real INSERT and a checked-out connection
                gate.reached += 1
                if gate.reached >= gate.expected:
                    gate.entered.set()
                await gate.release.wait()
            return await super().commit()

    engine = create_async_engine(migrated_url, pool_size=POOL_SIZE,
                                 max_overflow=POOL_OVERFLOW, pool_timeout=3)
    event.listen(engine.sync_engine, 'checkout', meter.checkout)
    event.listen(engine.sync_engine, 'checkin', meter.checkin)
    event.listen(engine.sync_engine, 'before_cursor_execute', meter.statement)
    maker = async_sessionmaker(engine, class_=ObservedSession, expire_on_commit=False)
    yield SimpleNamespace(maker=maker, engine=engine, meter=meter, gate=gate)
    await engine.dispose()


@pytest_asyncio.fixture(loop_scope='module')
async def env(postgres, monkeypatch, tmp_path):
    maker, meter, gate = postgres.maker, postgres.meter, postgres.gate
    gate.auth = gate.writer = False
    gate.entered, gate.release = asyncio.Event(), asyncio.Event()
    gate.reached, gate.expected = 0, 1
    async with maker() as session:
        await session.execute(text('TRUNCATE users, api_keys, quota_counters, usage_logs, notes_metadata CASCADE'))
        user = User(username='concurrency-fixture', password_hash='unused',
                    is_active=True, vault_path=str(tmp_path))
        session.add(user)
        await session.flush()
        key = APIKey(user_id=user.id, name='fixture', key_hash=auth.hash_key('omcp_fixture_261'),
                     key_prefix='omcp_fixture', permission='readwrite', is_active=True,
                     daily_request_limit=100)
        session.add(key)
        await session.commit()
        uid, kid = user.id, key.id
    for module in (database, auth, tools, quotas):
        monkeypatch.setattr(module, 'async_session', maker)
    monkeypatch.setattr(settings, 'multi_user_mode', True)
    monkeypatch.setattr(settings, 'mcp_sandbox_mode', False)
    monkeypatch.setattr(settings, 'vault_path', str(tmp_path))
    monkeypatch.setattr(settings, 'vault_allow_named_staging_fallback', True)
    monkeypatch.setattr(tools, '_bucket_admission', lambda write: None)
    monkeypatch.setattr(auth.rate_limits, 'check_auth_failures', lambda *_: None)
    vault_overlap.publish_synthetic_snapshot()
    rate_limits.reset_state_for_tests()

    def install(mode='enforce', **kw):
        controller = concurrency.Controller(Settings(
            _env_file=None, secret_key='issue-261-test-secret-only-0123456789abcdef',
            mcp_concurrency_mode=mode, **kw))
        monkeypatch.setattr(concurrency, '_controller', controller)
        return controller

    controller = install()
    meter.reset()
    tasks = []
    def spawn(coro):
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task
    result = SimpleNamespace(**vars(postgres), uid=uid, kid=kid, root=tmp_path,
                             install=install, controller=controller, spawn=spawn)
    yield result
    gate.auth = gate.writer = False
    gate.release.set()
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    concurrency.get_controller().shutdown(close_writers=True)
    rate_limits.reset_state_for_tests()
    assert meter.active == 0, 'test left a real database connection checked out'


@contextmanager
def identity(env, *, limit=100):
    values = ((current_principal, ('api_key', env.kid)), (current_user_id, env.uid),
              (current_vault_root, (env.uid, env.root)),
              (auth.current_permission, 'readwrite'), (auth.current_api_key_id, env.kid),
              (auth.current_oauth_token_id, None), (auth.current_daily_request_limit, limit))
    tokens = [(var, var.set(value)) for var, value in values]
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


def assert_released(env):
    c = concurrency.get_controller()
    assert env.meter.active == c.tools.active == c.writers.active == c.authentication.active == 0
    assert max(env.meter.task_peaks.values(), default=0) <= TOOL_CONNECTION_MULTIPLIER


@tools._tracked('checkout_hold', [], resource_class='other')
async def held_tool(maker, entered, release):
    async with maker() as session:
        await session.execute(text('SELECT 1'))
        entered.set()
        await release.wait()
    return 'complete'


async def test_real_auth_checkouts_are_bounded_and_sends_happen_after_close(env):
    env.gate.auth = True
    env.gate.expected = 2
    sent = []

    async def one(index):
        async def send(message):
            # A different request may still be authenticating. This request
            # must already have returned ITS connection before sending bytes.
            assert env.meter.by_task[asyncio.current_task()] == 0
            if message['type'] == 'http.response.start':
                sent.append(message['status'])
        async def receive():
            return {'type':'http.request','body':b'','more_body':False}
        async def app(*args):
            raise AssertionError('invalid key reached app')
        scope = {'type':'http','method':'GET','path':'/mcp','raw_path':b'/mcp',
                 'query_string':b'', 'scheme':'http','server':('test',80),
                 'client':('127.0.0.2',1000+index),
                 'headers':[(b'authorization',f'Bearer omcp_invalid_{index}'.encode())]}
        await auth.APIKeyMiddleware(app)(scope, receive, send)

    first = [env.spawn(one(i)) for i in range(2)]
    await asyncio.wait_for(env.gate.entered.wait(), 3)
    assert env.meter.active == env.meter.peak == 2
    await asyncio.gather(*(one(i) for i in range(2,8)))
    assert sent == [429]*6
    assert env.gate.reached == 2, 'refused probes queried credentials'
    env.gate.auth = False
    env.gate.release.set()
    await asyncio.gather(*first)
    assert sent.count(401) == 2
    assert env.controller.requests.active == 0
    assert_released(env)


async def test_named_publication_move_import_quota_and_logging_real_sessions(env, monkeypatch):
    # Only external fetch is replaced; staging, publish locks and confirmation
    # all execute against the actual filesystem/database and real identity.
    @asynccontextmanager
    async def fetched(*args, **kwargs):
        async def chunks():
            yield b'imported bytes'
        yield SimpleNamespace(chunks=chunks(), final_url='https://fixture.example/source')
    monkeypatch.setattr(transfer, 'fetch_url_guarded', fetched)
    monkeypatch.setattr(settings, '_public_origin_explicit', True)
    monkeypatch.setattr(settings, 'base_url', 'https://fixture.example')
    with identity(env):
        created = await tools.create_note_impl('before.md', 'original\n')
        assert created.startswith('Created note')
        edited = await tools.edit_note_impl('before.md', 'changed\n')
        assert 'content_hash:' in edited and 'MCP-REFUSAL' not in edited
        moved = await tools.move_note_impl('before.md', 'after.md')
        assert moved.startswith('Moved') and 'warning:' not in moved
        imported = await tools.import_from_url_impl('https://fixture.example/source', 'import.bin')
        assert imported.startswith('Imported'), imported
    assert (env.root/'after.md').read_text() == 'changed\n'
    assert (env.root/'import.bin').read_bytes() == b'imported bytes'
    assert env.meter.statements['users'] >= 4  # named confirmations + import lock
    assert env.meter.statements['api_keys'] >= 1  # import locked credential
    assert env.meter.statements['notes_metadata'] >= 1  # actual move metadata
    assert env.meter.statements['quota_counters'] >= 4
    assert env.meter.statements['usage_logs'] >= 4
    assert_released(env)
    async with env.maker() as session:
        assert (await session.execute(text('SELECT count FROM quota_counters WHERE key_id=:kid'), {'kid':env.kid})).scalar_one() == 4
        assert (await session.execute(text('SELECT count(*) FROM usage_logs'))).scalar_one() == 4


async def test_actual_slot_refusals_are_coalesced_without_spending_quota(env):
    entered, release = asyncio.Event(), asyncio.Event()
    with identity(env):
        running = env.spawn(held_tool(env.maker, entered, release))
        await entered.wait()
        for _ in range(3):
            result = await tools.read_note_impl('not-created.md')
            assert '"code":"slot_timeout"' in result.error
        # Actual holder connection + the refusal writer, never an auth query.
        assert env.meter.peak <= 2
        release.set()
        assert await running == 'complete'
        await rate_limits.flush_all()
    assert_released(env)
    async with env.maker() as session:
        assert (await session.execute(text('SELECT count FROM quota_counters WHERE key_id=:kid'), {'kid':env.kid})).scalar_one() == 1
        rows = (await session.execute(text('SELECT params, '+executed_sql()+' AS executed, '+pre_body_refusal_sql()+' AS refused FROM usage_logs ul'), dict(PRE_BODY_REFUSAL_BINDS))).all()
    refused = [r for r in rows if r.params.get('error') == 'slot_timeout']
    assert sum(1+r.params.get('suppressed',0) for r in refused) == 3
    assert all(r.refused and not r.executed for r in refused)
    assert sum(bool(r.executed) for r in rows) == 1


async def test_shadow_pressure_and_actual_body_refusal_persist_on_one_real_row(env):
    env.install('shadow')
    entered, release = asyncio.Event(), asyncio.Event()
    with identity(env):
        running = env.spawn(held_tool(env.maker, entered, release))
        await entered.wait()
        result = await tools.read_note_impl('missing.md')
        assert '"code":"not_found"' in result.error
        release.set()
        await running
    assert_released(env)
    async with env.maker() as session:
        row = (await session.execute(text('SELECT params, '+executed_sql()+' AS executed, '+pre_body_refusal_sql()+" AS refused FROM usage_logs ul WHERE tool='read_note'"), dict(PRE_BODY_REFUSAL_BINDS))).one()
        count = (await session.execute(text('SELECT count(*) FROM usage_logs'))).scalar_one()
    assert count == 2
    assert row.params['error'] == 'not_found' and row.params['body_outcome'] == 'refused'
    assert row.params['concurrency_shadow']['shadow'] is True
    assert row.params['concurrency_shadow']['code'] == 'slot_timeout'
    assert row.executed and not row.refused


async def test_writer_ceiling_bounds_actual_concurrent_inserts_and_fk_retry(env):
    env.gate.writer = True
    values = dict(tool='writer_fixture', params={}, duration_ms=1, response_size=1,
                  key_id=env.kid, user_id=env.uid)
    first = env.spawn(tools.write_usage_row(values))
    await env.gate.entered.wait()
    pending = [env.spawn(tools.write_usage_row(values)) for _ in range(5)]
    await asyncio.sleep(0)
    assert env.meter.active == env.meter.peak == env.controller.writers.active == 1
    env.gate.writer = False
    env.gate.release.set()
    assert all(await asyncio.gather(first,*pending))
    assert env.meter.peak == 1, 'writer waiters checked out their own connections'
    checkouts = env.meter.checkouts
    # Deleted credential: first INSERT really fails its FK, retry uses a fresh
    # session, preserves attribution and still owns exactly one writer slot.
    assert await tools.write_usage_row(dict(values, key_id=2147483647,
                actor_kind='api_key', actor_label='gone fixture', actor_ref='omcp_gone'))
    assert env.meter.checkouts == checkouts+2
    assert env.meter.peak == 1
    assert_released(env)
    async with env.maker() as session:
        row = (await session.execute(text("SELECT key_id, actor_label FROM usage_logs WHERE actor_ref='omcp_gone'"))).one()
        assert row.key_id is None and row.actor_label == 'gone fixture'


async def test_cancelled_tool_returns_real_checkouts(env):
    entered, release = asyncio.Event(), asyncio.Event()
    with identity(env):
        running = env.spawn(held_tool(env.maker, entered, release))
        await entered.wait()
        assert env.meter.active == env.controller.tools.active == 1
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
    assert_released(env)
