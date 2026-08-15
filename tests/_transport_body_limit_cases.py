"""Transport body-limit cases — run ONLY as an import-isolated subprocess.

`tests/test_transport_body_limit.py` spawns this module with
`sys.executable -m pytest tests/_transport_body_limit_cases.py`, because the
things under test are decided at *import* time: `src/mcp_server/server.py`
builds the `FastMCP` instance (and with it the `RequestBodyLimitMiddleware`)
when it is imported, and `src/main.py` calls `mcp.streamable_http_app()` at
import. Monkeypatching `settings.max_file_write_bytes` afterwards would not
move the transport limit, so the whole process has to start with the settings
the cases need:

    MAX_FILE_WRITE_BYTES=65536   # far below MAX_NOTE_BYTES, so the note branch
                                 # of the formula is what sets the limit
    MCP_SANDBOX_MODE=true        # skips APIKeyMiddleware and the indexer
    VAULT_PATH=<tmpdir>

The leading underscore keeps it out of normal collection (`pytest.ini` only
collects `test_*.py`).

Sandbox mode leaves the auth contextvars untouched, so each test sets
`current_permission` to `"readwrite"` itself — a fake readwrite identity.
That propagates through the SDK's task group into the tool call (verified: the
write actually lands on disk), so no ASGI shim is needed.
"""
import asyncio
import base64
import json
import os
import sys
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import src.main as main_module  # noqa: E402
from src.auth.session import current_user_id  # noqa: E402
from src.config import MAX_NOTE_BYTES, settings  # noqa: E402
from src.mcp_server.auth import current_permission  # noqa: E402

WRITE_CAP = 65536
JSON_HEADERS = {
    "accept": "application/json, text/event-stream",
    "content-type": "application/json",
}

# `StreamableHTTPSessionManager.run()` may be entered only once per instance,
# and the instance is module-global — so the app lifespan is entered once for
# the whole module, which requires every test to share one event loop.
pytestmark = pytest.mark.asyncio(loop_scope="module")


def setup_module(module):
    """Fail loudly rather than silently testing the wrong configuration."""
    assert settings.mcp_sandbox_mode is True, "cases module needs MCP_SANDBOX_MODE=true"
    assert settings.max_file_write_bytes == WRITE_CAP, (
        f"cases module needs MAX_FILE_WRITE_BYTES={WRITE_CAP}"
    )
    assert settings.mcp_max_request_body_bytes == 6 * MAX_NOTE_BYTES + 1024 * 1024


@pytest_asyncio.fixture(scope="module", loop_scope="module", autouse=True)
async def _running_session_manager():
    """Start the MCP session manager (and the rest of the lifespan) once.

    The lifespan is held open by a dedicated task rather than by the fixture
    body: pytest-asyncio finalizes a module-scoped fixture in a different task
    than it set it up in, and anyio refuses to exit a cancel scope from a task
    other than the one that entered it.
    """
    started = asyncio.Event()
    stop = asyncio.Event()

    async def runner():
        async with main_module.app.router.lifespan_context(main_module.app):
            started.set()
            await stop.wait()

    task = asyncio.create_task(runner())
    await started.wait()
    yield
    stop.set()
    await task


@pytest.fixture
def vault() -> Path:
    return Path(settings.vault_path)


@pytest.fixture(autouse=True)
def _readwrite_identity():
    perm = current_permission.set("readwrite")
    uid = current_user_id.set(None)
    yield
    current_permission.reset(perm)
    current_user_id.reset(uid)


def _tool_call(name: str, arguments: dict) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }


def _result_text(response: httpx.Response) -> str:
    """Pull the tool's text content out of the SSE-framed JSON-RPC response."""
    for line in response.text.splitlines():
        if line.startswith("data: "):
            payload = json.loads(line[len("data: "):])
            return payload["result"]["content"][0]["text"]
    raise AssertionError(f"no SSE data frame in response: {response.text[:500]!r}")


async def _post(path: str, *, json_body=None, content=None, headers=None):
    transport = ASGITransport(app=main_module.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://localhost:8000", timeout=120
    ) as client:
        return await client.post(
            path,
            json=json_body,
            content=content,
            headers={**JSON_HEADERS, **(headers or {})},
        )


async def _post_chunked(path: str, chunks: list[bytes], headers=None):
    """Drive the ASGI app directly so the body arrives without Content-Length."""
    pending = list(chunks)
    sent: list[dict] = []

    async def receive():
        if pending:
            body = pending.pop(0)
            return {"type": "http.request", "body": body, "more_body": bool(pending)}
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    raw_headers = [
        (b"host", b"localhost"),
        (b"accept", b"application/json, text/event-stream"),
        (b"content-type", b"application/json"),
    ]
    for k, v in (headers or {}).items():
        raw_headers.append((k.lower().encode(), v.encode()))

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": raw_headers,
        "client": ("127.0.0.1", 12345),
        "server": ("localhost", 8000),
    }
    await main_module.app(scope, receive, send)
    starts = [m for m in sent if m["type"] == "http.response.start"]
    assert starts, "app produced no response"
    return starts[0]["status"]


# ── (a) a maximum-size base64 write succeeds end to end ─────────────────────


async def test_max_size_base64_write_succeeds(vault):
    payload = os.urandom(WRITE_CAP)
    target = vault / "at_cap.bin"
    response = await _post(
        "/mcp/",
        json_body=_tool_call(
            "write_file",
            {
                "path": "at_cap.bin",
                "content": base64.b64encode(payload).decode(),
                "encoding": "base64",
            },
        ),
    )
    assert response.status_code == 200
    text = _result_text(response)
    assert "Wrote" in text, text
    assert target.read_bytes() == payload


# ── (b) one byte over the cap is a *tool* error, not a transport rejection ───


async def test_one_byte_over_cap_is_a_tool_error(vault):
    payload = os.urandom(WRITE_CAP + 1)
    target = vault / "over_cap.bin"
    response = await _post(
        "/mcp/",
        json_body=_tool_call(
            "write_file",
            {
                "path": "over_cap.bin",
                "content": base64.b64encode(payload).decode(),
                "encoding": "base64",
            },
        ),
    )
    assert response.status_code == 200
    text = _result_text(response)
    assert "Content too large" in text
    assert str(WRITE_CAP) in text.replace(",", "")
    assert not target.exists()


# ── (c) the note branch of the formula: 6× JSON escaping still gets through ──


@pytest.mark.slow
async def test_worst_case_escaped_note_write_reaches_the_tool(vault):
    # MAX_NOTE_BYTES of U+0001: one UTF-8 byte each, six JSON bytes each
    # (backslash-u-0001), so the request body is ~60 MiB — under the 61 MiB limit only
    # because MAX_NOTE_BYTES is in the formula (MAX_FILE_WRITE_BYTES is 64 KiB
    # here). The note is exactly at MAX_NOTE_BYTES, so the tool accepts it.
    content = "\x01" * MAX_NOTE_BYTES
    response = await _post(
        "/mcp/",
        json_body=_tool_call("create_note", {"path": "escaped.md", "content": content}),
    )
    assert response.status_code == 200
    assert "Created note" in _result_text(response)
    assert (vault / "escaped.md").stat().st_size == MAX_NOTE_BYTES


# ── (d)–(f) oversized bodies are bounded ────────────────────────────────────


def _oversized_body() -> bytes:
    return b"x" * (settings.mcp_max_request_body_bytes + 1)


async def test_oversized_body_413_on_mcp_path():
    response = await _post("/mcp/", content=_oversized_body())
    assert response.status_code == 413


async def test_oversized_body_413_on_bearer_root_path():
    # RootMCPProxyMiddleware rewrites POST / with a Bearer token onto /mcp/;
    # sandbox mode bypasses the key check, so any token value works.
    response = await _post(
        "/",
        content=_oversized_body(),
        headers={"authorization": "Bearer omcp_sandbox"},
    )
    assert response.status_code == 413


async def test_oversized_chunked_body_413_without_content_length():
    chunk = b"x" * (1024 * 1024)
    count = settings.mcp_max_request_body_bytes // len(chunk) + 2
    status = await _post_chunked("/mcp/", [chunk] * count)
    assert status == 413


async def test_oversized_chunked_body_413_on_bearer_root_path():
    chunk = b"x" * (1024 * 1024)
    count = settings.mcp_max_request_body_bytes // len(chunk) + 2
    status = await _post_chunked(
        "/", [chunk] * count, headers={"authorization": "Bearer omcp_sandbox"}
    )
    assert status == 413


# ── a body just under the limit is not rejected by the transport ────────────


async def test_body_just_under_the_limit_is_not_rejected_by_the_transport():
    """The limit itself is off-by-one safe: `limit` bytes still reach the app.

    The payload is not valid JSON-RPC, so the app answers 400 — the point is
    that it is the *app* answering, not the transport's 413.
    """
    response = await _post("/mcp/", content=b"x" * settings.mcp_max_request_body_bytes)
    assert response.status_code != 413
