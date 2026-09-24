"""Concurrency configuration: bounds, hierarchy, coherence and pool budget.

Extended for #188: four modes, independent class ceilings, per-class pool
demand, the legacy MCP_CONCURRENCY_OTHER, and the .env.example block.
"""
import logging
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.config import Settings
from src.services.pool_budget import POOL_CAPACITY, POOL_OVERFLOW, POOL_SIZE

SECRET = "issue-261-test-secret-only-0123456789abcdef"
ROOT = Path(__file__).resolve().parent.parent


def settings(**kw):
    return Settings(_env_file=None, secret_key=SECRET, **kw)


@pytest.mark.parametrize('kw', [
    {'mcp_concurrency_mode': 'invalid'},
    {'mcp_concurrency_mode': 'other'},
    {'mcp_concurrency_wait_seconds': 10.01},
    {'mcp_concurrency_wait_seconds': -1},
    {'mcp_concurrency_wait_seconds': float('nan')},
    {'mcp_concurrency_transport_wait_seconds': 5.01},
    {'mcp_concurrency_transport_wait_seconds': float('inf')},
    {'mcp_concurrency_writer_wait_seconds': float('inf')},
    {'mcp_concurrency_writer_wait_seconds': .26},
    {'mcp_concurrency_tools': 3},            # tenant 4 > tools 3
    {'mcp_concurrency_fingerprint': 65},     # > requests 64
    {'mcp_concurrency_principal': 5},        # > tenant 4
    {'mcp_concurrency_tenant_waiters': 65},  # > waiters 64
    {'mcp_concurrency_registry_size': 0},
    {'mcp_concurrency_replay_budget_bytes': 2 ** 20 - 1},
    {'mcp_concurrency_replay_budget_bytes': 2 ** 28 + 1},
    {'mcp_concurrency_auth_waiters': 0},
])
def test_invalid_configuration_refused(kw):
    with pytest.raises(ValidationError):
        settings(**kw)


def test_defaults():
    s = settings()
    assert s.mcp_concurrency_mode == 'shadow'
    expected = dict(
        wait_seconds=5, transport_wait_seconds=2, requests=64, fingerprint=20,
        request_waiters=64, fingerprint_waiters=16, auth=2, auth_waiters=32,
        tools=6, tenant=4, principal=3, embedding=1, vector=1, write=1, scan=2,
        light=4, other=None, waiters=64, tenant_waiters=32, principal_waiters=16,
        registry_size=1024, writers=1, writer_waiters=64, writer_wait_seconds=.25,
        replay_budget_bytes=32 * 1024 * 1024,
    )
    for name, value in expected.items():
        assert getattr(s, 'mcp_concurrency_' + name) == value, name


def test_defaults_fit_one_shared_pool_budget_with_a_spare_connection():
    from src.services.pool_budget import budget_terms
    s = settings()
    caps = {c: getattr(s, 'mcp_concurrency_' + c)
            for c in ('embedding', 'vector', 'write', 'scan', 'light')}
    terms = budget_terms(auth=s.mcp_concurrency_auth, tools=s.mcp_concurrency_tools,
                         caps=caps, writers=s.mcp_concurrency_writers)
    assert (terms['auth'], terms['tool_demand'], terms['writers'], terms['headroom']) == (2, 7, 1, 4)
    assert terms['total'] == 14 < POOL_CAPACITY
    assert POOL_CAPACITY == POOL_SIZE + POOL_OVERFLOW == 15


@pytest.mark.parametrize('mode', ['off', 'shadow', 'queue', 'enforce'])
def test_every_mode_accepts_positive_waits(mode):
    s = settings(mcp_concurrency_mode=mode, mcp_concurrency_wait_seconds=10,
                 mcp_concurrency_transport_wait_seconds=5)
    assert s.mcp_concurrency_mode == mode


def test_class_ceilings_may_overlap_the_global_ceiling():
    s = settings(mcp_concurrency_tools=6, mcp_concurrency_light=4, mcp_concurrency_scan=2,
                 mcp_concurrency_write=1, mcp_concurrency_embedding=1, mcp_concurrency_vector=1)
    assert s.mcp_concurrency_light + s.mcp_concurrency_scan + 3 > s.mcp_concurrency_tools


@pytest.mark.parametrize('kw,names', [
    ({'mcp_concurrency_fingerprint': 65}, ['MCP_CONCURRENCY_FINGERPRINT', 'MCP_CONCURRENCY_REQUESTS']),
    ({'mcp_concurrency_principal': 5}, ['MCP_CONCURRENCY_PRINCIPAL', 'MCP_CONCURRENCY_TENANT']),
    ({'mcp_concurrency_tenant': 7}, ['MCP_CONCURRENCY_TENANT', 'MCP_CONCURRENCY_TOOLS']),
    ({'mcp_concurrency_light': 7}, ['MCP_CONCURRENCY_LIGHT', 'MCP_CONCURRENCY_TOOLS']),
    ({'mcp_concurrency_scan': 7}, ['MCP_CONCURRENCY_SCAN', 'MCP_CONCURRENCY_TOOLS']),
    ({'mcp_concurrency_embedding': 7}, ['MCP_CONCURRENCY_EMBEDDING', 'MCP_CONCURRENCY_TOOLS']),
    ({'mcp_concurrency_vector': 7}, ['MCP_CONCURRENCY_VECTOR', 'MCP_CONCURRENCY_TOOLS']),
    ({'mcp_concurrency_write': 7}, ['MCP_CONCURRENCY_WRITE', 'MCP_CONCURRENCY_TOOLS']),
    ({'mcp_concurrency_principal_waiters': 33},
     ['MCP_CONCURRENCY_PRINCIPAL_WAITERS', 'MCP_CONCURRENCY_TENANT_WAITERS']),
    ({'mcp_concurrency_tenant_waiters': 65},
     ['MCP_CONCURRENCY_TENANT_WAITERS', 'MCP_CONCURRENCY_WAITERS']),
    ({'mcp_concurrency_fingerprint_waiters': 65},
     ['MCP_CONCURRENCY_FINGERPRINT_WAITERS', 'MCP_CONCURRENCY_REQUEST_WAITERS']),
    # The transport envelope cannot refuse what the tool stage would queue.
    ({'mcp_concurrency_principal': 3, 'mcp_concurrency_principal_waiters': 16,
      'mcp_concurrency_fingerprint': 4},
     ['MCP_CONCURRENCY_FINGERPRINT', 'MCP_CONCURRENCY_PRINCIPAL',
      'MCP_CONCURRENCY_PRINCIPAL_WAITERS']),
    # auth 2 + (write 3x2 + 5x1) + writers 1 + 4 = 18 > 15.
    ({'mcp_concurrency_tools': 8, 'mcp_concurrency_write': 3},
     ['MCP_CONCURRENCY_AUTH', 'tool demand 11', 'MCP_CONCURRENCY_TOOLS 8',
      'MCP_CONCURRENCY_WRITE 3x2', 'MCP_CONCURRENCY_WRITERS', 'headroom 4', '= 18',
      'capacity 15']),
])
def test_each_coherence_rule_names_its_settings(kw, names):
    with pytest.raises(ValidationError) as err:
        settings(**kw)
    message = str(err.value)
    for name in names:
        assert name in message, (name, message)


def test_legacy_shadow_zero_wait_rule_and_class_sum_rule_are_gone():
    settings(mcp_concurrency_mode='shadow', mcp_concurrency_wait_seconds=.1)
    settings(mcp_concurrency_tools=4, mcp_concurrency_light=4, mcp_concurrency_scan=2,
             mcp_concurrency_tenant=4, mcp_concurrency_principal=3)


def _env_file(tmp_path, lines):
    path = tmp_path / 'concurrency.env'
    path.write_text('SECRET_KEY=' + SECRET + '\n' + '\n'.join(lines) + '\n')
    return path


def test_other_one_is_ignored_with_a_warning(tmp_path, caplog):
    path = _env_file(tmp_path, ['MCP_CONCURRENCY_OTHER=1'])
    with caplog.at_level(logging.WARNING, logger='src.config'):
        s = Settings(_env_file=path)
    assert s.mcp_concurrency_other == 1
    warnings = [r for r in caplog.records
                if r.levelno == logging.WARNING and 'MCP_CONCURRENCY_OTHER' in r.getMessage()]
    assert len(warnings) == 1
    assert 'MCP_CONCURRENCY_LIGHT' in warnings[0].getMessage()
    assert 'MCP_CONCURRENCY_SCAN' in warnings[0].getMessage()


def test_other_three_is_refused(tmp_path):
    path = _env_file(tmp_path, ['MCP_CONCURRENCY_OTHER=3'])
    with pytest.raises(ValidationError) as err:
        Settings(_env_file=path)
    assert 'MCP_CONCURRENCY_LIGHT' in str(err.value)
    assert 'MCP_CONCURRENCY_SCAN' in str(err.value)


def _example_block():
    text = (ROOT / '.env.example').read_text()
    lines = [line for line in text.splitlines()
             if re.match(r'^MCP_CONCURRENCY_[A-Z_]+=', line)]
    assert lines, 'no MCP_CONCURRENCY_ block in .env.example'
    return lines


@pytest.mark.parametrize('mode', ['off', 'shadow', 'queue', 'enforce'])
def test_env_example_block_validates_in_every_mode(tmp_path, mode):
    lines = [f'MCP_CONCURRENCY_MODE={mode}' if line.startswith('MCP_CONCURRENCY_MODE=')
             else line for line in _example_block()]
    s = Settings(_env_file=_env_file(tmp_path, lines))
    assert s.mcp_concurrency_mode == mode


def test_env_example_block_is_the_defaults_and_names_every_setting():
    block = dict(line.split('=', 1) for line in _example_block())
    defaults = settings()
    fields = {name for name in Settings.model_fields if name.startswith('mcp_concurrency_')}
    named = {'mcp_concurrency_' + key.removeprefix('MCP_CONCURRENCY_').lower() for key in block}
    assert 'MCP_CONCURRENCY_OTHER' not in block
    assert named == fields - {'mcp_concurrency_other'}
    for key, raw in block.items():
        value = getattr(defaults, 'mcp_concurrency_' + key.removeprefix('MCP_CONCURRENCY_').lower())
        assert str(value) == raw or float(raw) == value, key
