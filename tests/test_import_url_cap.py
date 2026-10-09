"""`import_from_url`'s URL-length cap and its non-parsing log transform (#322).

The cap goes through the shared decorator's declarative `arg_char_caps`
screen, so an over-long URL is the ordinary pre-body `argument_too_long`
refusal: no resolver, no HTTP client, no file. The `url` logging transform
returns `<over-long>` without converting or parsing such a value, because
`named_params()` runs it on the refusal's own usage row too.

This cap is not the memory bound; the body budget is
(`tests/test_body_budget_middleware.py`).
"""
import asyncio
import json
import urllib.parse
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

import src.mcp_server.auth as mcp_auth
import src.mcp_server.tools as tools
import src.services.quotas as quotas
from src.auth.session import current_principal, current_user_id
from src.config import MAX_IMPORT_URL_CHARS
from src.services import transfer

PREFIX = "https://example.com/"
OVER = PREFIX + "a" * (MAX_IMPORT_URL_CHARS + 1 - len(PREFIX))
AT = PREFIX + "a" * (MAX_IMPORT_URL_CHARS - len(PREFIX))


class _QuotaSpy:
    def __init__(self):
        self.statements = []

    def __call__(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def execute(self, stmt, params=None):
        self.statements.append(str(stmt))
        return SimpleNamespace(scalar=lambda: 1, fetchall=lambda: [])

    async def commit(self):
        pass


@pytest.fixture
def env(monkeypatch, tmp_path):
    state = SimpleNamespace(rows=[], fetched=[], preflights=0, parsed=[],
                            quota=_QuotaSpy())

    async def fake_log_usage(tool, params, duration_ms, response_size):
        state.rows.append({"tool": tool, "params": params})
        return True

    real_urlsplit = urllib.parse.urlsplit

    def recording_urlsplit(value, *a, **k):
        state.parsed.append(value)
        return real_urlsplit(value, *a, **k)

    def preflight(path, *, need_write, overwrite):
        state.preflights += 1
        return (None, str(tmp_path), path, None)

    @asynccontextmanager
    async def fetch(url, *, max_bytes):
        # The URL policy check: the call reached it, and is refused here so
        # that nothing is fetched.
        state.fetched.append(url)
        raise transfer.SSRFError("probe: policy check reached")
        yield  # pragma: no cover

    monkeypatch.setattr(tools, "_log_usage", fake_log_usage)
    monkeypatch.setattr(quotas, "async_session", state.quota)
    monkeypatch.setattr(tools, "urlsplit", recording_urlsplit)
    monkeypatch.setattr(urllib.parse, "urlsplit", recording_urlsplit)
    monkeypatch.setattr(tools, "_mint_preflight", preflight)
    monkeypatch.setattr(tools, "_fingerprint_of", lambda root, rel: None)
    monkeypatch.setattr(tools, "_transfer_identity", lambda: SimpleNamespace())
    monkeypatch.setattr(transfer, "fetch_url_guarded", fetch)
    return state


def _call(url):
    async def run():
        tokens = [
            (current_principal, current_principal.set(("api_key", 7))),
            (mcp_auth.current_api_key_id, mcp_auth.current_api_key_id.set(7)),
            (mcp_auth.current_permission, mcp_auth.current_permission.set("readwrite")),
            (current_user_id, current_user_id.set(None)),
        ]
        try:
            return await tools.import_from_url_impl(url, "Attachments/a.pdf")
        finally:
            for var, token in reversed(tokens):
                var.reset(token)

    return asyncio.run(run())


def _sentinel(text):
    last = text.splitlines()[-1]
    assert last.startswith("MCP-REFUSAL "), text[-300:]
    return json.loads(last[len("MCP-REFUSAL "):])


def test_an_over_long_url_is_refused_before_any_fetch(env):
    assert len(OVER) == MAX_IMPORT_URL_CHARS + 1
    result = _call(OVER)
    assert _sentinel(result)["code"] == "argument_too_long"
    assert OVER not in result, "the refusal echoed the argument"
    assert str(MAX_IMPORT_URL_CHARS) in result.replace(",", "")
    assert env.fetched == [] and env.preflights == 0, "the body ran"
    assert env.quota.statements == [], "a quota statement was issued"


def test_a_url_at_the_limit_reaches_the_policy_checks(env):
    assert len(AT) == MAX_IMPORT_URL_CHARS
    result = _call(AT)
    assert env.fetched == [AT]
    assert _sentinel(result)["code"] == "fetch_refused"


def test_the_usage_row_never_parses_the_over_long_value(env):
    _call(OVER)
    assert len(env.rows) == 1
    assert env.rows[0]["params"]["url"] == "<over-long>"
    assert all(len(str(v)) <= MAX_IMPORT_URL_CHARS for v in env.parsed)
    assert OVER not in env.parsed


def test_the_transform_still_logs_the_host_of_a_normal_url():
    assert tools._url_host(AT) == "example.com"
    assert tools._url_host(OVER) == "<over-long>"
    assert tools._url_host("https://[bad") == "<unparsable>"
