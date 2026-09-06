"""L5b and L7 — the query length cap, and the provider's own limit (#194).

Two halves of one caller-facing failure mode, on opposite sides of the
body/no-body line:

* **Pre-body.** `MAX_SEARCH_QUERY_CHARS` is enforced declaratively on the
  shared decorator, beside the unencodable-argument screen — so before the
  embedding-provider call, before the `tsquery` parse, before any search or
  quota statement, and before the value is interpolated into a server-authored
  string like `f"No results for '{query}'"`. Its marker is `argument_too_long`
  and it is **pre-body**.
* **Post-body.** A character cap cannot promise a *token* limit: 8,192
  characters of a densely-tokenizing script can still exceed what a provider
  accepts. When the provider says so, the tool translates it into the same
  caller-facing `argument_too_long` **code** carrying the provider's reason —
  but under a **different marker**, `provider_input_rejected`, classified
  post-body because the body ran, resolved a vault and made a network round
  trip. Enumerating that as a refusal would drop a real provider call out of
  the latency percentiles.

The refusal never echoes the argument: it is the argument that was too large,
and a tool result is itself model context.
"""
import asyncio
import json
from types import SimpleNamespace

import pytest

import src.mcp_server.auth as mcp_auth
import src.mcp_server.tools as tools
import src.services.embeddings as embeddings
import src.services.quotas as quotas
import src.services.refusals as refusals
from src.auth.session import current_principal, current_user_id
from src.config import MAX_SEARCH_QUERY_CHARS
from src.mcp_server.read_result import ReadNoteResult


AT_THE_LIMIT = "q" * MAX_SEARCH_QUERY_CHARS
OVER_THE_LIMIT = "q" * (MAX_SEARCH_QUERY_CHARS + 1)


class _QuotaSpySession:
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

    async def rollback(self):  # pragma: no cover
        pass


@tools._tracked(
    "cap_probe_structured",
    ["query"],
    refusal_result=tools._read_note_refusal,
    arg_char_caps={"query": MAX_SEARCH_QUERY_CHARS},
)
async def _structured_probe(query: str) -> ReadNoteResult:  # pragma: no cover
    return ReadNoteResult(content="body")


def _run(fn, *args, rows=None, limit=None, quota_spy=None, **kwargs):
    rows = [] if rows is None else rows
    quota_spy = quota_spy or _QuotaSpySession()

    async def fake_log_usage(tool, params, duration_ms, response_size):
        rows.append({"tool": tool, "params": params})
        return True

    mp = pytest.MonkeyPatch()
    mp.setattr(tools, "_log_usage", fake_log_usage)
    mp.setattr(quotas, "async_session", quota_spy)

    async def run():
        tokens = [
            current_principal.set(("api_key", 7)),
            mcp_auth.current_api_key_id.set(7),
            mcp_auth.current_daily_request_limit.set(limit),
            current_user_id.set(None),
        ]
        try:
            return await fn(*args, **kwargs)
        finally:
            for var, token in zip(
                (
                    current_principal,
                    mcp_auth.current_api_key_id,
                    mcp_auth.current_daily_request_limit,
                    current_user_id,
                ),
                tokens,
            ):
                var.reset(token)

    try:
        return asyncio.run(run())
    finally:
        mp.undo()


def _sentinel(text: str) -> dict:
    last = text.splitlines()[-1]
    assert last.startswith("MCP-REFUSAL "), text[-200:]
    return json.loads(last[len("MCP-REFUSAL ") :])


class _NoSearch:
    """Every search entry point, wired to fail the test if it is reached."""

    def __init__(self):
        self.calls = []

    async def __call__(self, *args, **kwargs):
        self.calls.append(args)
        return []


@pytest.fixture
def search_stubs(monkeypatch):
    """Neuter everything below the gate, and record whether it was reached."""
    keyword = _NoSearch()
    semantic = _NoSearch()

    async def _no_embedding(_text):  # pragma: no cover - reaching this fails
        raise AssertionError("an embedding was requested for a refused query")

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

    monkeypatch.setattr(tools, "full_text_search", keyword)
    monkeypatch.setattr(tools, "semantic_search", semantic)
    monkeypatch.setattr(tools, "async_session", _Session)
    monkeypatch.setattr(embeddings, "get_embedding", _no_embedding)
    return SimpleNamespace(keyword=keyword, semantic=semantic)


# ── the cap ─────────────────────────────────────────────────────────────────


def test_a_query_at_the_limit_is_accepted(search_stubs):
    result = tools.search_notes_impl, tools.semantic_search_impl
    for impl, stub in zip(result, (search_stubs.keyword, search_stubs.semantic)):
        stub.calls.clear()
        out = _run(impl, query=AT_THE_LIMIT)
        assert "MCP-REFUSAL" not in out
        assert stub.calls, "the body did not run for a query at the limit"


def test_one_character_over_is_refused_before_anything_runs(search_stubs):
    """No embedding call, no search statement, no quota statement — the gate is
    above all three."""
    spy = _QuotaSpySession()
    rows = []
    out = _run(
        tools.semantic_search_impl,
        query=OVER_THE_LIMIT,
        rows=rows,
        limit=5,
        quota_spy=spy,
    )
    assert search_stubs.semantic.calls == []
    assert spy.statements == [], "a refused call consumed a quota statement"
    payload = _sentinel(out)
    assert payload["code"] == refusals.ARGUMENT_TOO_LONG
    assert payload["limit"] == MAX_SEARCH_QUERY_CHARS
    assert payload["limit_unit"] == refusals.CHARACTERS


def test_the_refusal_names_the_setting_and_the_lengths(search_stubs):
    out = _run(tools.search_notes_impl, query=OVER_THE_LIMIT)
    assert "MAX_SEARCH_QUERY_CHARS" in out
    assert str(MAX_SEARCH_QUERY_CHARS) in out
    assert str(MAX_SEARCH_QUERY_CHARS + 1) in out


def test_the_query_is_not_echoed_back(search_stubs):
    """The argument is the thing that was too large; quoting it back into the
    complaint spends the caller's context on repeating what it just sent."""
    out = _run(tools.semantic_search_impl, query=OVER_THE_LIMIT)
    assert OVER_THE_LIMIT not in out
    assert "q" * 100 not in out


def test_the_refusal_writes_its_own_row_and_is_not_coalesced(search_stubs):
    """It sits *below* the general bucket, so its rate is already bounded by
    that bucket — one row per refusal, no second mechanism."""
    rows = []
    for _ in range(4):
        _run(tools.search_notes_impl, query=OVER_THE_LIMIT, rows=rows)
    assert len(rows) == 4
    for row in rows:
        assert row["params"]["error"] == tools._ARGUMENT_TOO_LONG_MARKER
        assert tools._SUPPRESSED_PARAM not in row["params"]


def test_a_structured_tool_refuses_in_its_own_shape():
    result = _run(_structured_probe, query=OVER_THE_LIMIT)
    assert isinstance(result, ReadNoteResult)
    assert _sentinel(result.error)["code"] == refusals.ARGUMENT_TOO_LONG


def test_the_cap_is_declared_on_both_search_tools():
    for impl in (tools.search_notes_impl, tools.semantic_search_impl):
        source = f"{impl.__wrapped__.__name__}"
        assert source  # the impls exist; the declaration is checked below
    import inspect

    source = inspect.getsource(tools)
    assert source.count('arg_char_caps={"query": MAX_SEARCH_QUERY_CHARS}') == 2


# ── the provider's own limit ────────────────────────────────────────────────


def test_a_provider_input_rejection_becomes_the_same_caller_facing_code(
    monkeypatch, search_stubs
):
    """One actionable failure mode for "the query was too large", whichever
    limit actually applied — this server's characters, or the provider's
    tokens."""
    reason = "input exceeds the model's maximum context length of 8192 tokens"

    async def _rejecting(*_args, **_kwargs):
        raise refusals.ProviderInputTooLarge(reason, provider="openai")

    monkeypatch.setattr(tools, "semantic_search", _rejecting)
    rows = []
    out = _run(tools.semantic_search_impl, query="dense but short", rows=rows)

    payload = _sentinel(out)
    assert payload["code"] == refusals.ARGUMENT_TOO_LONG
    assert reason in out, "the provider's stated reason must reach the caller"
    assert "Traceback" not in out

    # …under the **distinct, post-body** marker.
    assert len(rows) == 1
    assert rows[0]["params"]["error"] == tools._PROVIDER_INPUT_REJECTED_MARKER
    assert rows[0]["params"]["error"] != tools._ARGUMENT_TOO_LONG_MARKER


def test_the_post_body_marker_is_deliberately_not_a_pre_body_refusal():
    from src.services import usage_stats

    fragment = usage_stats.pre_body_refusal_sql()
    assert tools._PROVIDER_INPUT_REJECTED_MARKER not in fragment, (
        "the provider rejection is post-body: the body ran, resolved a vault "
        "and made a network call, so enumerating it would drop a real round "
        "trip out of the latency percentiles"
    )


def test_the_exception_type_lives_in_the_dependency_free_module():
    """Declared in `refusals.py`, so the code that raises it (the providers)
    and the code that handles it (the search tools) share one contract and
    neither depends on the other's module."""
    assert refusals.ProviderInputTooLarge.__module__ == "src.services.refusals"
    exc = refusals.ProviderInputTooLarge("too big", provider="ollama")
    assert exc.reason == "too big"
    assert exc.provider == "ollama"
