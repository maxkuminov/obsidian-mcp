"""DB session and ASGI response lifetimes cannot share an auth permit."""
import asyncio
from types import SimpleNamespace

import pytest

from src.config import Settings
from src.mcp_server import auth
from src.services import concurrency


def scope(token='omcp_missing',method='POST'):
    return {'type':'http','method':method,'path':'/mcp','raw_path':b'/mcp',
            'query_string':b'', 'scheme':'http','server':('test',80),'client':('127.0.0.2',2000),
            'headers':[(b'authorization',('Bearer '+token).encode())]}


async def receive():
    return {'type':'http.request','body':b'','more_body':False}


@pytest.fixture
def controller(monkeypatch):
    c=concurrency.Controller(Settings(_env_file=None, secret_key="issue-261-test-secret-only-0123456789abcdef",mcp_concurrency_mode='enforce'))
    monkeypatch.setattr(concurrency,'_controller',c)
    monkeypatch.setattr(auth.settings,'mcp_sandbox_mode',False)
    monkeypatch.setattr(auth.settings,'multi_user_mode',False)
    monkeypatch.setattr(auth.rate_limits,'check_auth_failures',lambda *_:None)
    monkeypatch.setattr(auth.rate_limits,'record_auth_failure',lambda *_:None)
    monkeypatch.setattr(auth.security_events,'emit',lambda *a,**kw:None)
    return c


@pytest.mark.asyncio
@pytest.mark.parametrize('token',['omcp_invalid','oauth_invalid'])
async def test_invalid_response_send_is_outside_session_and_auth_lease(controller,monkeypatch,token):
    active=0
    class Session:
        async def __aenter__(self):
            nonlocal active
            active+=1
            assert controller.authentication.active==1
            return self
        async def __aexit__(self,*a):
            nonlocal active
            active-=1
        async def execute(self,*a,**kw):
            return SimpleNamespace(scalar_one_or_none=lambda:None,first=lambda:None)
    monkeypatch.setattr(auth,'async_session',Session)
    sending=asyncio.Event()
    async def send(message):
        assert active==0 and controller.authentication.active==0
        assert controller.requests.active==1
        sending.set()
        await asyncio.Future()
    async def app(*args):raise AssertionError('invalid credential reached app')
    task=asyncio.create_task(auth.APIKeyMiddleware(app)(scope(token),receive,send))
    await sending.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert controller.requests.active==0 and not controller.fingerprints.entries


@pytest.mark.asyncio
async def test_sse_retains_request_but_releases_auth(controller,monkeypatch):
    entered=asyncio.Event()
    async def authenticate(self,*a):
        assert controller.authentication.active==1
        return None
    monkeypatch.setattr(auth.APIKeyMiddleware,'_authenticate',authenticate)
    async def app(*args):
        assert controller.authentication.active==0
        assert controller.requests.active==1
        entered.set()
        await asyncio.Future()
    task=asyncio.create_task(auth.APIKeyMiddleware(app)(scope(method='GET'),receive,lambda x:None))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert controller.requests.active==0


@pytest.mark.asyncio
async def test_auth_pressure_opens_no_session(controller,monkeypatch):
    held=[controller.auth(),controller.auth()]
    monkeypatch.setattr(auth,'async_session',lambda:(_ for _ in ()).throw(AssertionError('DB lookup')))
    sent=[]
    async def send(message):sent.append(message)
    async def app(*args):raise AssertionError('app called')
    await auth.APIKeyMiddleware(app)(scope(),receive,send)
    assert sent[0]['status']==429
    assert controller.requests.active==0
    for a in held:a.lease.release()


@pytest.mark.asyncio
async def test_auth_cancellation_releases_both_leases(controller,monkeypatch):
    opened=asyncio.Event()
    class Session:
        async def __aenter__(self):return self
        async def __aexit__(self,*a):pass
        async def execute(self,*a,**kw):
            opened.set()
            await asyncio.Future()
    monkeypatch.setattr(auth,'async_session',Session)
    async def app(*a):raise AssertionError()
    task=asyncio.create_task(auth.APIKeyMiddleware(app)(scope(),receive,lambda x:None))
    await opened.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert controller.authentication.active==controller.requests.active==0


@pytest.mark.asyncio
async def test_pressure_telemetry_failure_cannot_leak_request(controller,monkeypatch):
    held=[controller.auth(),controller.auth()]
    def failed_emit(*a,**kw):raise ValueError('sink unavailable')
    monkeypatch.setattr(auth.security_events,'emit',failed_emit)
    sent=[]
    async def send(message):sent.append(message)
    await auth.APIKeyMiddleware(None)(scope(),receive,send)
    assert sent[0]['status']==429 and controller.requests.active==0
    for a in held:a.lease.release()
