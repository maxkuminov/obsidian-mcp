"""The embedding HTTP client factory (#185; design D7, task 2.6).

Every client aimed at `OLLAMA_URL` / `OPENAI_BASE_URL` comes from
`embedding_http_client`, which switches the environment off (no proxy, no
`SSL_CERT_*`, no netrc), refuses redirects and pins the trust anchor. The
proxy regression is proven with two real loopback listeners rather than a
mock: the request must reach the target and never the proxy.
"""

from __future__ import annotations

import ast
import asyncio
import ssl
from pathlib import Path

import certifi
import httpx
import pytest
import respx

import src.config
from src.config import Settings
from src.services import embeddings
from src.services import transport_security as ts

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "transport_tls"
CA = str(FIXTURES / "ca.pem")
OTHER_CA = str(FIXTURES / "other_ca.pem")
SECRET = "a-real-test-secret-not-a-placeholder"


def _use_settings(monkeypatch, **kw) -> Settings:
    kw.setdefault("secret_key", SECRET)
    s = Settings(_env_file=None, **kw)
    monkeypatch.setattr(src.config, "settings", s)
    # The providers read the name they imported.
    monkeypatch.setattr(embeddings, "settings", s)
    return s


@pytest.fixture(autouse=True)
def _fresh_certifi_context(monkeypatch):
    monkeypatch.setattr(ts, "_certifi_context", None)


def _ca_count(ctx: ssl.SSLContext) -> int:
    return ctx.cert_store_stats()["x509_ca"]


def _client_context(client: httpx.AsyncClient) -> ssl.SSLContext:
    return client._transport._pool._ssl_context


# ── Construction ────────────────────────────────────────────────────────────


def test_the_factory_switches_the_environment_off_and_refuses_redirects():
    client = ts.embedding_http_client(5.0)
    assert client.follow_redirects is False
    assert client.trust_env is False
    assert client._mounts == {}
    assert client.timeout == httpx.Timeout(5.0)


def test_default_trust_is_certifis_bundle(monkeypatch):
    _use_settings(monkeypatch, ollama_url="https://ollama.internal.example")
    ctx = _client_context(ts.embedding_http_client(5.0))
    expected = ssl.create_default_context(cafile=certifi.where())
    assert _ca_count(ctx) == _ca_count(expected) > 0
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname


def test_ca_file_pins_trust_to_that_file_only(monkeypatch):
    s = _use_settings(
        monkeypatch,
        ollama_url="https://ollama.internal.example",
        embedding_ca_file=CA,
    )
    ctx = _client_context(ts.embedding_http_client(5.0))
    assert ctx is s.embedding_ssl_context
    assert _ca_count(ctx) == 1


def test_ambient_ca_variables_are_ignored(monkeypatch):
    _use_settings(monkeypatch, ollama_url="https://ollama.internal.example")
    baseline = _ca_count(_client_context(ts.embedding_http_client(5.0)))
    monkeypatch.setattr(ts, "_certifi_context", None)
    monkeypatch.setenv("SSL_CERT_FILE", OTHER_CA)
    monkeypatch.setenv("SSL_CERT_DIR", str(FIXTURES))
    ctx = _client_context(ts.embedding_http_client(5.0))
    assert _ca_count(ctx) == baseline
    assert ctx.cert_store_stats() == ssl.create_default_context(
        cafile=certifi.where()
    ).cert_store_stats()


# ── Ambient proxy regression (Codex r1 #1) ─────────────────────────────────


class _Listener:
    """A one-line HTTP server on loopback that counts the requests it gets."""

    def __init__(self):
        self.hits = 0
        self.server = None
        self.port = None

    async def _handle(self, reader, writer):
        self.hits += 1
        try:
            await reader.readuntil(b"\r\n\r\n")
        except Exception:  # noqa: BLE001
            pass
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"
        )
        await writer.drain()
        writer.close()

    async def __aenter__(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self.server.close()
        await self.server.wait_closed()


async def test_an_ambient_proxy_does_not_carry_the_loopback_hop(monkeypatch):
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    async with _Listener() as target, _Listener() as proxy:
        proxy_url = f"http://127.0.0.1:{proxy.port}"
        for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            monkeypatch.setenv(var, proxy_url)
        url = f"http://127.0.0.1:{target.port}/api/tags"

        # Control: an environment-trusting client *is* diverted, so the
        # assertion below is about the factory, not about httpx's defaults.
        async with httpx.AsyncClient(timeout=5.0) as naive:
            await naive.get(url)
        assert proxy.hits == 1 and target.hits == 0

        async with ts.embedding_http_client(5.0) as client:
            assert client._mounts == {}
            response = await client.get(url)
        assert response.status_code == 200
        assert target.hits == 1
        assert proxy.hits == 1  # unchanged: the factory's request never reached it


# ── Redirects are not followed, by either provider ─────────────────────────


@respx.mock
async def test_ollama_does_not_follow_a_redirect(monkeypatch):
    _use_settings(monkeypatch, ollama_url="http://127.0.0.1:11434")
    respx.post("http://127.0.0.1:11434/api/embed").mock(
        return_value=httpx.Response(
            307, headers={"Location": "http://elsewhere.example/api/embed"}
        )
    )
    target = respx.post("http://elsewhere.example/api/embed").mock(
        return_value=httpx.Response(200, json={"embeddings": [[0.1]]})
    )
    with pytest.raises(httpx.HTTPStatusError):
        await embeddings.OllamaProvider().embed_one("text")
    assert target.call_count == 0


@respx.mock
async def test_openai_does_not_follow_a_redirect(monkeypatch):
    _use_settings(
        monkeypatch,
        embedding_provider="openai",
        openai_api_key="sk-test",
        openai_base_url="https://api.example.test/v1",
    )
    respx.post("https://api.example.test/v1/embeddings").mock(
        return_value=httpx.Response(
            307, headers={"Location": "http://elsewhere.example/v1/embeddings"}
        )
    )
    target = respx.post("http://elsewhere.example/v1/embeddings").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    with pytest.raises(Exception):
        await embeddings.OpenAIProvider()._post(["text"])
    assert target.call_count == 0


# ── No bypassing client ─────────────────────────────────────────────────────

_SWEPT = (
    ROOT / "src" / "services" / "embeddings.py",
    ROOT / "src" / "control_panel" / "routes.py",
)


def _client_constructions(path: Path) -> list[int]:
    tree = ast.parse(path.read_text())
    imported = {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "httpx"
        for alias in node.names
        if alias.name in ("AsyncClient", "Client")
    }
    lines = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr in ("AsyncClient", "Client")
            and isinstance(func.value, ast.Name)
            and func.value.id == "httpx"
        ) or (isinstance(func, ast.Name) and func.id in imported):
            lines.append(node.lineno)
    return lines


@pytest.mark.parametrize("path", _SWEPT, ids=lambda p: p.name)
def test_no_http_client_is_built_outside_the_factory(path):
    assert _client_constructions(path) == [], (
        f"{path.name} builds an httpx client directly; use "
        "transport_security.embedding_http_client"
    )


def test_the_sweep_would_catch_a_direct_construction(tmp_path):
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import httpx\nfrom httpx import AsyncClient as AC\n"
        "httpx.AsyncClient(timeout=1)\nAC()\n"
    )
    assert _client_constructions(probe) == [3, 4]


def test_every_embedding_call_site_uses_the_factory():
    sources = {p.name: p.read_text() for p in _SWEPT}
    assert sources["embeddings.py"].count("embedding_http_client(") == 2
    assert sources["routes.py"].count("embedding_http_client(") == 1
