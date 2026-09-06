import pytest
from pydantic import ValidationError
from src.config import Settings
from src.services.pool_budget import POOL_CAPACITY, POOL_SIZE, POOL_OVERFLOW


@pytest.mark.parametrize('kw',[
    {'mcp_concurrency_mode':'invalid'},
    {'mcp_concurrency_wait_seconds':.1},
    {'mcp_concurrency_wait_seconds':float('nan')},
    {'mcp_concurrency_writer_wait_seconds':float('inf')},
    {'mcp_concurrency_auth':3},
    {'mcp_concurrency_tools':3},
    {'mcp_concurrency_fingerprint':33},
    {'mcp_concurrency_principal':4},
    {'mcp_concurrency_tenant_waiters':33},
    {'mcp_concurrency_registry_size':0},
])
def test_invalid_configuration_refused(kw):
    with pytest.raises(ValidationError):Settings(_env_file=None, secret_key="issue-261-test-secret-only-0123456789abcdef",**kw)


def test_defaults_fit_one_shared_pool_budget():
    s=Settings(_env_file=None, secret_key="issue-261-test-secret-only-0123456789abcdef")
    assert s.mcp_concurrency_mode=='shadow'
    assert s.mcp_concurrency_auth + 2*s.mcp_concurrency_tools + s.mcp_concurrency_writers + 4 == POOL_CAPACITY
    assert POOL_CAPACITY == POOL_SIZE+POOL_OVERFLOW == 15
