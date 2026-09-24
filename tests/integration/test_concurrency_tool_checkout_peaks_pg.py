"""Every registered tool stays within its class's connection multiplier (#188 D2).

`pool_budget.CLASS_CONNECTIONS` lets the pool budget count one connection per
admitted non-write tool instead of two. That is only sound if no tool path
overlaps sessions. This test invokes **every** registered tool through
`_tracked` against real PostgreSQL, one at a time, and asserts its measured
checkout peak is at most `CLASS_CONNECTIONS[class]`.

The meter counts every checkout the call causes: the quota gate's, the body's
and the usage writer's. The writer runs after the body under its own permit,
so sequential checkouts never raise the peak; a tool whose sessions *overlap*
does. Two figures are checked: the per-task peak the design names, and the
engine-wide peak while the tool runs alone, which also catches a session a
tool opens from a sub-task.

A tool measuring above its multiplier is a stop-and-report, never a test edit:
raise the class multiplier in `pool_budget.py` in the same change.
"""
from __future__ import annotations

import asyncio
import base64
import re
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
from src.models.db import APIKey, NoteEmbedding, NoteLink, NoteMetadata, User
from src.services import concurrency, quotas, rate_limits, transfer, vault_overlap
from src.services import embeddings as embeddings_service
from src.services.pool_budget import CLASS_CONNECTIONS, POOL_OVERFLOW, POOL_SIZE
from src.services.usage_stats import PRE_BODY_REFUSAL_BINDS, pre_body_refusal_sql

pytestmark = [_harness.requires_pgvector, pytest.mark.asyncio(loop_scope='module')]

DIM = 8
UNIT = [1.0] + [0.0] * (DIM - 1)


@dataclass
class Meter:
    active: int = 0
    peak: int = 0
    by_task: Counter = field(default_factory=Counter)
    task_peaks: Counter = field(default_factory=Counter)

    def checkout(self, connection, record, proxy):
        owner = asyncio.current_task()
        record.info['_188_owner'] = owner
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.by_task[owner] += 1
        self.task_peaks[owner] = max(self.task_peaks[owner], self.by_task[owner])

    def checkin(self, connection, record):
        self.active -= 1
        self.by_task[record.info.pop('_188_owner')] -= 1

    def reset(self):
        assert self.active == 0
        self.peak = 0
        self.by_task.clear()
        self.task_peaks.clear()


@pytest.fixture(scope='module')
def migrated_url():
    yield from _harness.throwaway_database('checkout_peaks_188', DIM)


@pytest_asyncio.fixture(scope='module', loop_scope='module')
async def engine(migrated_url):
    meter = Meter()
    eng = create_async_engine(migrated_url, pool_size=POOL_SIZE,
                              max_overflow=POOL_OVERFLOW, pool_timeout=3)
    event.listen(eng.sync_engine, 'checkout', meter.checkout)
    event.listen(eng.sync_engine, 'checkin', meter.checkin)
    maker = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    yield SimpleNamespace(maker=maker, meter=meter)
    await eng.dispose()


@pytest_asyncio.fixture(loop_scope='module')
async def env(engine, monkeypatch, tmp_path):
    maker, meter = engine.maker, engine.meter
    root = tmp_path / 'vault'
    root.mkdir()
    (root / 'a.md').write_text('# A\n\nquartzite links to [[b]]\n')
    (root / 'b.md').write_text('---\nstatus: open\n---\n# B\n\nquartzite body\n')
    (root / 'lonely.md').write_text('# Lonely\n')
    (root / 'f.bin').write_bytes(b'\x00\x01binary')
    async with maker() as session:
        await session.execute(text(
            'TRUNCATE users, api_keys, quota_counters, usage_logs, notes_metadata, '
            'note_embeddings, note_links, transfer_tokens CASCADE'))
        user = User(username='peaks-fixture', password_hash='unused', is_active=True,
                    vault_path=str(root))
        session.add(user)
        await session.flush()
        key = APIKey(user_id=user.id, name='peaks', key_hash=auth.hash_key('omcp_peaks_188'),
                     key_prefix='omcp_peaks', permission='readwrite', is_active=True,
                     daily_request_limit=1000)
        session.add(key)
        await session.flush()
        ids = {}
        for path, tags in (('a.md', ['alpha']), ('b.md', ['beta']), ('lonely.md', [])):
            note = NoteMetadata(user_id=user.id, file_path=path,
                                title=path.removesuffix('.md'), tags=tags,
                                frontmatter={'status': 'open'}, content_hash=path)
            session.add(note)
            await session.flush()
            ids[path] = note.id
            session.add(NoteEmbedding(note_id=note.id, chunk_index=0,
                                      chunk_text=f'quartzite chunk of {path}',
                                      embedding=list(UNIT)))
        session.add(NoteLink(source_note_id=ids['a.md'], target_path='b',
                             target_note_id=ids['b.md'], link_text='b', kind='wikilink',
                             position=0))
        await session.execute(text(
            "UPDATE notes_metadata SET content_tsvector = to_tsvector('english', 'quartzite')"))
        await session.commit()
        uid, kid = user.id, key.id

    for module in (database, auth, tools, quotas):
        monkeypatch.setattr(module, 'async_session', maker)
    monkeypatch.setattr(embeddings_service, 'async_session', maker, raising=False)

    async def fake_embedding(_text):
        return list(UNIT)
    monkeypatch.setattr(embeddings_service, 'get_embedding', fake_embedding)

    @asynccontextmanager
    async def fetched(*args, **kwargs):
        async def chunks():
            yield b'imported bytes'
        yield SimpleNamespace(chunks=chunks(), final_url='https://fixture.example/source')
    monkeypatch.setattr(transfer, 'fetch_url_guarded', fetched)
    monkeypatch.setattr(settings, '_public_origin_explicit', True)
    monkeypatch.setattr(settings, 'base_url', 'https://fixture.example')
    monkeypatch.setattr(settings, 'multi_user_mode', True)
    monkeypatch.setattr(settings, 'mcp_sandbox_mode', False)
    monkeypatch.setattr(settings, 'vault_path', str(root))
    monkeypatch.setattr(settings, 'vault_allow_named_staging_fallback', True)
    monkeypatch.setattr(tools, '_bucket_admission', lambda write: None)
    vault_overlap.publish_synthetic_snapshot()
    rate_limits.reset_state_for_tests()
    controller = concurrency.Controller(Settings(
        _env_file=None, secret_key='issue-188-test-secret-only-0123456789abcdef',
        mcp_concurrency_mode='enforce'))
    monkeypatch.setattr(concurrency, '_controller', controller)
    meter.reset()
    yield SimpleNamespace(maker=maker, meter=meter, uid=uid, kid=kid, root=root,
                          controller=controller)
    controller.shutdown(close_writers=True)
    rate_limits.reset_state_for_tests()
    assert meter.active == 0, 'a tool left a real database connection checked out'


@contextmanager
def identity(env):
    values = ((current_principal, ('api_key', env.kid)), (current_user_id, env.uid),
              (current_vault_root, (env.uid, env.root)),
              (auth.current_permission, 'readwrite'), (auth.current_api_key_id, env.kid),
              (auth.current_oauth_token_id, None), (auth.current_daily_request_limit, 1000))
    tokens = [(var, var.set(value)) for var, value in values]
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


def _upload_id(text_):
    match = re.search(r'upload_id\W+([A-Za-z0-9_-]{22})', text_)
    return match.group(1) if match else 'A' * 22


# (registered tool name, impl, args) in an order where each call's fixture
# exists: create → edit → frontmatter → move → delete, and so on.
CALLS = [
    ('keyword_search', 'search_notes_impl', lambda s: (('quartzite',), {'limit': 5})),
    ('semantic_search', 'semantic_search_impl', lambda s: (('anything',), {'limit': 5})),
    ('read_note', 'read_note_impl', lambda s: (('a.md',), {})),
    ('list_notes', 'list_notes_impl', lambda s: ((), {})),
    ('get_tags', 'get_tags_impl', lambda s: ((), {})),
    ('get_recent', 'get_recent_impl', lambda s: ((), {})),
    ('get_vault_guide', 'get_vault_guide_impl', lambda s: ((), {})),
    ('get_backlinks', 'get_backlinks_impl', lambda s: (('b.md',), {})),
    ('get_links', 'get_links_impl', lambda s: (('a.md',), {})),
    ('get_neighborhood', 'get_neighborhood_impl', lambda s: (('a.md',), {})),
    ('find_related', 'find_related_impl', lambda s: (('a.md',), {})),
    ('find_orphans', 'find_orphans_impl', lambda s: ((), {})),
    ('create_note', 'create_note_impl', lambda s: (('new.md', 'first\n'), {})),
    ('edit_note', 'edit_note_impl', lambda s: (('new.md', 'second\n'), {})),
    ('set_frontmatter', 'set_frontmatter_impl',
     lambda s: (('new.md',), {'updates': {'status': 'done'}})),
    ('move_note', 'move_note_impl', lambda s: (('new.md', 'moved.md'), {})),
    ('delete_note', 'delete_note_impl', lambda s: (('moved.md',), {'permanent': True})),
    ('read_file', 'read_file_impl', lambda s: (('f.bin',), {})),
    ('write_file', 'write_file_impl',
     lambda s: (('g.bin', base64.b64encode(b'raw bytes').decode()), {})),
    ('list_files', 'list_files_impl', lambda s: ((), {})),
    ('delete_file', 'delete_file_impl', lambda s: (('g.bin',), {'permanent': True})),
    ('request_upload', 'request_upload_impl', lambda s: (('up.bin',), {})),
    ('check_upload', 'check_upload_impl', lambda s: ((_upload_id(s['request_upload']),), {})),
    ('request_download', 'request_download_impl', lambda s: (('f.bin',), {})),
    ('import_from_url', 'import_from_url_impl',
     lambda s: (('https://fixture.example/source', 'import.bin'), {})),
]


def test_the_call_table_covers_every_registered_tool():
    assert {name for name, _, _ in CALLS} == set(concurrency.TOOL_CLASSES)
    for name, impl, _ in CALLS:
        assert getattr(tools, impl).__tracked_tool__ == name


async def test_every_registered_tool_stays_within_its_class_multiplier(env):
    outputs, measured, over = {}, {}, []
    with identity(env):
        for name, impl, args in CALLS:
            cls = concurrency.TOOL_CLASSES[name]
            positional, keywords = args(outputs)
            env.meter.reset()
            result = await getattr(tools, impl)(*positional, **keywords)
            outputs[name] = result if isinstance(result, str) else str(getattr(result, 'error', result))
            assert env.controller.tools.active == 0
            task_peak = max(env.meter.task_peaks.values(), default=0)
            measured[name] = (cls, task_peak, env.meter.peak)
            if max(task_peak, env.meter.peak) > CLASS_CONNECTIONS[cls]:
                over.append(f'{name} (class {cls}): per-task peak {task_peak}, '
                            f'engine peak {env.meter.peak} > {CLASS_CONNECTIONS[cls]}')
    assert not over, 'stop and report (#188 D2): ' + '; '.join(over)

    # Every call ran its body: none of these rows is a pre-body refusal, so the
    # peaks above measured real tool work, not an early gate.
    async with env.maker() as session:
        rows = (await session.execute(text(
            'SELECT tool, ' + pre_body_refusal_sql() + ' AS refused FROM usage_logs ul'),
            dict(PRE_BODY_REFUSAL_BINDS))).all()
    by_tool = {}
    for row in rows:
        by_tool.setdefault(row.tool, []).append(row.refused)
    missing = set(concurrency.TOOL_CLASSES) - set(by_tool)
    assert not missing, f'no usage row for {sorted(missing)}'
    refused = sorted(t for t, flags in by_tool.items() if any(flags))
    assert not refused, f'pre-body refusals instead of real work: {refused}; {outputs}'
    # Every class was actually exercised with at least one real checkout.
    assert all(peak >= 1 for _, peak, _ in measured.values()), measured
