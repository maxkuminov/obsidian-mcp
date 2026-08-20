"""Regression tests for GitHub issue #66.

Unassigning a user's `vault_path` in the control panel was presented to the
operator as "vault tools error", but only the *disk*-touching tools actually
stopped. `semantic_search`, `keyword_search`, `list_notes`, `get_recent` and
every graph tool are served from `notes_metadata` / `note_embeddings` filtered
by `user_id` alone — they never called `_vault_root`. Nothing prunes those rows
either (the indexer's `_active_user_ids()` skips users with a NULL
`vault_path`), so an unassigned user kept an indefinite, fully queryable mirror
of the content they last held, reachable with an unchanged API key.

The fix puts the admission gate in `_tracked`, the decorator every MCP tool
shares: resolve the caller's vault root once, before the tool body runs, and
refuse the whole call when it cannot be resolved.

The second half of the fix is *which* root the gate reads. `_user_vault_cache`
is process-global and the indexer's bulk warm writes to it, so a bulk `SELECT`
issued before an admin cleared `vault_path` can land after the per-request warm
evicted the entry — re-admitting a revoked user mid-call. `APIKeyMiddleware`
therefore binds the root it read to the request (`current_vault_root`), and the
gate prefers that snapshot; `test_stale_bulk_warm_cannot_readmit_*` pins the
ordered interleaving.

These tests exercise the real tool impls with an empty vault cache. A refusal
happens *before* the body, so no DB, network or embedding access occurs — if
the gate regressed, the tools would try to open a database connection and the
tests would fail loudly rather than silently pass.
"""

import asyncio
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

# `src.mcp_server.tools` pulls in `src.config`, whose module-level `Settings()`
# reads `./.env`. Provide minimal defaults and chdir to a dir without a `.env`
# BEFORE importing, keeping the module fully offline. (Same preamble as
# tests/test_issue_8_tracked_param_mapping.py.)
os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
_TEMPLATES = Path(__file__).resolve().parent.parent / "src" / "control_panel" / "templates"
os.chdir(tempfile.gettempdir())

import pytest  # noqa: E402

import src.mcp_server.auth as mcp_auth  # noqa: E402
import src.mcp_server.tools as tools  # noqa: E402
import src.services.vault as vault  # noqa: E402
from src.auth.session import (  # noqa: E402
    UNSET_VAULT_ROOT,
    _SingleUserSentinel,
    current_user_id,
    current_vault_root,
)
from src.mcp_server.server import mcp  # noqa: E402


UNASSIGNED_UID = 4242
STALE_ROOT = Path("/vaults/alpha")


@pytest.fixture
def cold_cache():
    """A process-level vault cache with no entry for `UNASSIGNED_UID`.

    This is exactly the state after `clear_user_vault_cache(target.id)` on the
    NULL transition — and also the state of a freshly started worker whose
    per-request warm found no row. Both must refuse.
    """
    saved = dict(vault._user_vault_cache)
    vault._user_vault_cache.clear()
    try:
        yield
    finally:
        vault._user_vault_cache.clear()
        vault._user_vault_cache.update(saved)


@pytest.fixture
def as_unassigned_user(cold_cache):
    token = current_user_id.set(UNASSIGNED_UID)
    root_token = current_vault_root.set(UNSET_VAULT_ROOT)
    try:
        yield UNASSIGNED_UID
    finally:
        current_vault_root.reset(root_token)
        current_user_id.reset(token)


def _run_capturing_log(coro_fn, *args, **kwargs):
    """Run a `_tracked` tool impl, returning `(result, logged_params, tool)`.

    `_log_usage` is stubbed so no database is touched by the *logging*; if the
    gate failed to refuse, the tool body itself would still try to connect.
    """
    captured = {}

    async def fake_log_usage(tool, params, duration_ms, response_size):
        captured["tool"] = tool
        captured["params"] = params

    original = tools._log_usage
    tools._log_usage = fake_log_usage
    try:
        result = asyncio.run(coro_fn(*args, **kwargs))
    finally:
        tools._log_usage = original
    return result, captured.get("params"), captured.get("tool")


# --- (a) every registered tool refuses ---------------------------------------


def _registered_tools():
    """Every tool the MCP server actually exposes, by introspection.

    A hand-maintained list is the shape of bug #66 itself: the tools that
    leaked were the ones nobody thought to add to a list. Enumerating the
    server's own registry means a tool added later is covered on the day it is
    registered, or this test fails.
    """
    return sorted(mcp._tool_manager.list_tools(), key=lambda t: t.name)


def _dummy_args(fn):
    """Plausible values for a tool's required parameters.

    The values are never used — the gate refuses before the body runs — but the
    call must be well-formed enough to reach the wrapper.
    """
    import inspect

    kwargs = {}
    for name, param in inspect.signature(fn).parameters.items():
        if param.default is not inspect.Parameter.empty:
            continue
        annotation = str(param.annotation)
        if "int" in annotation:
            kwargs[name] = 1
        elif "bool" in annotation:
            kwargs[name] = False
        elif "list" in annotation:
            kwargs[name] = []
        elif "dict" in annotation:
            kwargs[name] = {}
        else:
            kwargs[name] = "Projects/Alpha.md"
    return kwargs


def test_the_registry_is_not_empty():
    """Guard the guard: an introspection matrix that finds zero tools would
    pass vacuously."""
    assert len(_registered_tools()) >= 25


@pytest.mark.parametrize("tool", _registered_tools(), ids=lambda t: t.name)
def test_unassigned_user_is_refused_by_every_registered_tool(as_unassigned_user, tool):
    """No exemptions. `get_vault_guide` returns the vault's own CLAUDE.md and
    `check_upload` reports a published vault path, so every registered tool
    reads or writes vault content or vault metadata."""
    result, params, _ = _run_capturing_log(lambda: tool.fn(**_dummy_args(tool.fn)))
    assert isinstance(result, str), f"{tool.name} returned {type(result)!r}"
    assert result == tools._NO_VAULT_MESSAGE, (
        f"{tool.name} did not refuse: {result[:200]!r}"
    )
    # The refusal must not name a path, a note or an excerpt.
    assert "Projects/Alpha.md" not in result
    assert params is not None, f"{tool.name} refusal was not logged"


def test_every_registered_tool_delegates_to_a_tracked_impl():
    """design.md claims the gate is "enforced by construction" — a new tool
    inherits it by being registered. That is only true while every registered
    tool delegates to a `_tracked`-wrapped impl, so check it structurally
    rather than trusting the parametrized matrix above to be re-read."""
    import src.mcp_server.server as server

    unwrapped = []
    for tool in _registered_tools():
        referenced = [
            getattr(server, name, None) for name in tool.fn.__code__.co_names
        ]
        if not any(hasattr(obj, "__tracked_tool__") for obj in referenced):
            unwrapped.append(tool.name)
    assert unwrapped == [], f"tools not delegating to a _tracked impl: {unwrapped}"


def test_semantic_search_refusal_leaks_no_chunk_text(as_unassigned_user):
    """The concrete harm in #66: `semantic_search` returned 200-char excerpts
    of the user's notes. The refusal is a fixed string with no query echo."""
    result, _, _ = _run_capturing_log(
        lambda: tools.semantic_search_impl("salary negotiation")
    )
    assert "salary negotiation" not in result


# --- (b) single-user mode is unaffected --------------------------------------


def test_single_user_sentinel_id_is_none():
    """The gate keys off `current_user_id`, which is None in single-user mode
    precisely because the sentinel's id is None."""
    assert _SingleUserSentinel().id is None


def test_single_user_mode_passes_the_gate(cold_cache):
    """`current_user_id` unset (single-user mode / sandbox mode) → the gate
    resolves `settings.vault_path` and the tool body runs, even though the
    multi-user cache is completely empty."""
    ran = {}

    @tools._tracked("fake_tool", ["q"])
    async def fake_tool(q: str) -> str:
        ran["yes"] = True
        return "served"

    assert current_user_id.get() is None
    result, params, tool = _run_capturing_log(lambda: fake_tool("hello"))
    assert ran.get("yes") is True
    assert result == "served"
    assert tool == "fake_tool"
    assert params == {"q": "hello"}
    assert "error" not in params


def test_single_user_mode_ignores_a_snapshot_for_another_user(cold_cache):
    """A bound snapshot must never reach `_vault_root(None)`: single-user mode
    answers from settings, full stop."""
    token = current_vault_root.set((UNASSIGNED_UID, None))
    try:
        assert current_user_id.get() is None
        assert vault._vault_root(None) == Path(vault.settings.vault_path)
    finally:
        current_vault_root.reset(token)


def test_warm_cache_makes_an_assigned_user_pass(as_unassigned_user, tmp_path):
    """Sanity check on the other side of the gate: with the user's root in the
    cache the body runs. Guards against a gate that refuses everyone."""
    ran = {}

    @tools._tracked("fake_tool", [])
    async def fake_tool() -> str:
        ran["yes"] = True
        return "served"

    vault._user_vault_cache[UNASSIGNED_UID] = tmp_path
    result, params, _ = _run_capturing_log(lambda: fake_tool())
    assert ran.get("yes") is True
    assert result == "served"
    assert "error" not in params


def test_snapshot_for_a_different_user_falls_through_to_the_cache(
    as_unassigned_user, tmp_path
):
    """The snapshot is keyed by user id. A context carrying somebody else's
    snapshot must not answer for this user — in either direction."""
    vault._user_vault_cache[UNASSIGNED_UID] = tmp_path
    token = current_vault_root.set((UNASSIGNED_UID + 1, None))
    try:
        assert vault._vault_root(UNASSIGNED_UID) == tmp_path
    finally:
        current_vault_root.reset(token)


# --- (c) the refusal is logged ----------------------------------------------


def test_refusal_is_logged_with_an_error_marker(as_unassigned_user):
    result, params, tool = _run_capturing_log(
        lambda: tools.list_notes_impl(folder="Private", limit=50)
    )
    assert result == tools._NO_VAULT_MESSAGE
    assert tool == "list_notes"
    assert params["error"] == tools._NO_VAULT_MARKER
    # The usual allow-listed params are still recorded, and nothing beyond
    # them: a refusal must not become a new disclosure channel.
    assert params["folder"] == "Private"
    assert params["limit"] == 50
    assert set(params) == {"folder", "limit", "tags", "frontmatter", "error"}


# --- cache semantics: the refusal must not depend on cache warmth ------------


class _EmptyResult:
    def first(self):
        return None

    def all(self):
        return []


class _RowsResult:
    def __init__(self, rows):
        self._rows = rows

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return list(self._rows)


class _FakeSession:
    """A session whose `execute` always answers with `result`."""

    def __init__(self, result):
        self._result = result

    async def execute(self, stmt):
        return self._result


def test_warm_user_vault_cache_evicts_when_the_row_is_gone(cold_cache):
    """`warm_user_vault_cache` runs on every authenticated MCP request. It used
    to be a silent no-op for a NULL `vault_path`, which left a previously
    cached root in place. It must now *evict*, and report the None so the
    caller can bind it to the request."""
    vault._user_vault_cache[UNASSIGNED_UID] = STALE_ROOT
    got = asyncio.run(
        vault.warm_user_vault_cache(_FakeSession(_EmptyResult()), UNASSIGNED_UID)
    )
    assert got is None
    assert UNASSIGNED_UID not in vault._user_vault_cache

    with pytest.raises(RuntimeError):
        vault._vault_root(UNASSIGNED_UID)


def test_warm_user_vault_cache_returns_the_assigned_root(cold_cache):
    row = SimpleNamespace(id=UNASSIGNED_UID, vault_path=str(STALE_ROOT))
    got = asyncio.run(
        vault.warm_user_vault_cache(_FakeSession(_RowsResult([row])), UNASSIGNED_UID)
    )
    assert got == STALE_ROOT
    assert vault._user_vault_cache[UNASSIGNED_UID] == STALE_ROOT


def test_gate_refuses_on_a_cold_cache_rather_than_raising(as_unassigned_user):
    """A fresh process that never warmed this user must refuse with a tool
    error, not a 500. (`warm_user_vault_cache` is a no-op there — there is
    nothing to evict — so the gate cannot lean on eviction alone.)"""
    assert UNASSIGNED_UID not in vault._user_vault_cache
    result, params, _ = _run_capturing_log(lambda: tools.get_backlinks_impl("A.md"))
    assert result == tools._NO_VAULT_MESSAGE
    assert params["error"] == tools._NO_VAULT_MARKER


# --- the ordered race: a stale bulk warm must not re-admit -------------------


def _replay_the_race():
    """Drive the exact interleaving, in order, on one thread.

    1. user 4242 holds /vaults/alpha; the indexer's bulk `SELECT` is issued
       (snapshot taken — it still sees the old row);
    2. the admin commits `vault_path = NULL`;
    3. an MCP request authenticates: the per-request warm reads NULL, evicts
       the cache entry and hands the None to the middleware;
    4. the *older* bulk query finally returns and re-inserts /vaults/alpha into
       the shared dict;
    5. the request, still in flight, calls a tool.

    Returns the value `_vault_root` produced at step 5 — or the RuntimeError.
    """
    vault._user_vault_cache[UNASSIGNED_UID] = STALE_ROOT
    stale_row = SimpleNamespace(id=UNASSIGNED_UID, vault_path=str(STALE_ROOT))

    fresh = asyncio.run(
        vault.warm_user_vault_cache(_FakeSession(_EmptyResult()), UNASSIGNED_UID)
    )
    assert fresh is None
    assert UNASSIGNED_UID not in vault._user_vault_cache

    # The stale bulk result lands afterwards. The bulk form is add-only by
    # design (see design.md), so it *does* put the revoked root back.
    asyncio.run(vault.warm_user_vault_cache(_FakeSession(_RowsResult([stale_row]))))
    assert vault._user_vault_cache[UNASSIGNED_UID] == STALE_ROOT
    return fresh


def test_stale_bulk_warm_really_does_repopulate_the_shared_dict(
    as_unassigned_user,
):
    """The negative control that makes the next test meaningful: without a
    request-scoped snapshot, the shared dict alone would re-admit the revoked
    user, and a write tool would then target the vault they no longer hold."""
    _replay_the_race()
    assert vault._vault_root(UNASSIGNED_UID) == STALE_ROOT


def test_stale_bulk_warm_cannot_readmit_a_revoked_user(as_unassigned_user):
    """With the middleware's own answer bound to the request, the late bulk
    result cannot re-admit: the gate reads the snapshot, not the dict."""
    fresh = _replay_the_race()
    token = current_vault_root.set((UNASSIGNED_UID, fresh))
    try:
        with pytest.raises(RuntimeError):
            vault._vault_root(UNASSIGNED_UID)
        result, params, _ = _run_capturing_log(
            lambda: tools.edit_note_impl("Projects/Alpha.md", "clobbered")
        )
        assert result == tools._NO_VAULT_MESSAGE
        assert params["error"] == tools._NO_VAULT_MARKER
    finally:
        current_vault_root.reset(token)


# --- end to end through APIKeyMiddleware ------------------------------------


class _MiddlewareSession:
    """Answers the four statements `APIKeyMiddleware` issues on the API-key
    path, dispatching on the rendered SQL so the test does not depend on
    call order."""

    def __init__(self, api_key, user_active=True, vault_row=None):
        self.api_key = api_key
        self.user_active = user_active
        self.vault_row = vault_row
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def commit(self):
        self.committed = True

    async def execute(self, stmt):
        sql = str(stmt)
        if sql.startswith("UPDATE"):
            return _EmptyResult()
        if "vault_path" in sql:
            return _RowsResult([self.vault_row] if self.vault_row else [])
        if "FROM api_keys" in sql:
            return _RowsResult([self.api_key])
        # select(User.is_active)
        return _ScalarResult(self.user_active)


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


def _patch_rows_result_scalar():
    """`_RowsResult` also has to answer `.scalar_one_or_none()` for the
    api_keys lookup."""
    _RowsResult.scalar_one_or_none = lambda self: (
        self._rows[0] if self._rows else None
    )


_patch_rows_result_scalar()


def test_api_key_middleware_binds_the_snapshot_and_the_tool_refuses(cold_cache):
    """R3 end to end: an unchanged, still-active API key belonging to a user
    whose `vault_path` was cleared authenticates fine, and the tool call it
    carries is refused — even though a stale bulk warm repopulates the shared
    cache while the request is in flight."""
    from src.models.db import APIKey

    api_key = APIKey(
        id=7,
        key_hash="x",
        permission="readwrite",
        user_id=UNASSIGNED_UID,
        expires_at=None,
        is_active=True,
    )
    vault._user_vault_cache[UNASSIGNED_UID] = STALE_ROOT
    stale_row = SimpleNamespace(id=UNASSIGNED_UID, vault_path=str(STALE_ROOT))

    captured = {}

    async def downstream(scope, receive, send):
        # The user's row is already NULL, so the middleware's warm evicted the
        # entry. Now the indexer's older bulk query lands mid-request.
        assert UNASSIGNED_UID not in vault._user_vault_cache
        await vault.warm_user_vault_cache(_FakeSession(_RowsResult([stale_row])))
        assert vault._user_vault_cache[UNASSIGNED_UID] == STALE_ROOT

        captured["user_id"] = current_user_id.get()
        captured["snapshot"] = current_vault_root.get()
        captured["result"] = await tools.semantic_search_impl("salary negotiation")
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive():  # pragma: no cover - never awaited
        return {"type": "http.request", "body": b"", "more_body": False}

    sent = []

    async def send(message):
        sent.append(message)

    async def run():
        original_session = mcp_auth.async_session
        original_log = tools._log_usage

        async def fake_log_usage(*a, **kw):
            return None

        mcp_auth.async_session = lambda: _MiddlewareSession(api_key, vault_row=None)
        tools._log_usage = fake_log_usage
        try:
            app = mcp_auth.APIKeyMiddleware(downstream)
            await app(
                {
                    "type": "http",
                    "method": "POST",
                    "path": "/mcp/",
                    "headers": [(b"authorization", b"Bearer omcp_testkey")],
                },
                receive,
                send,
            )
        finally:
            mcp_auth.async_session = original_session
            tools._log_usage = original_log

    asyncio.run(run())

    assert sent and sent[0]["status"] == 200, "the request was not authenticated"
    assert captured["user_id"] == UNASSIGNED_UID
    assert captured["snapshot"] == (UNASSIGNED_UID, None)
    assert captured["result"] == tools._NO_VAULT_MESSAGE
    # And the snapshot does not outlive the request.
    assert current_vault_root.get() is UNSET_VAULT_ROOT


# --- panel copy --------------------------------------------------------------


def test_unassigned_option_states_what_the_code_does():
    """The option label asserted an enforcement outcome the code did not
    deliver. Assert the *rendered* text, not the source line, so a template
    refactor cannot quietly restore the old promise."""
    from jinja2 import ChainableUndefined, ChoiceLoader, DictLoader, Environment, FileSystemLoader

    env = Environment(
        loader=ChoiceLoader([
            # Stub the layout: this test is about one option in one template.
            DictLoader({"base.html": "{% block title %}{% endblock %}{% block content %}{% endblock %}"}),
            FileSystemLoader(str(_TEMPLATES)),
        ]),
        undefined=ChainableUndefined,
        autoescape=True,
    )
    rendered = env.get_template("user_edit.html").render(
        target=SimpleNamespace(
            id=1,
            username="bob",
            vault_path="/vaults/alpha",
            is_admin=False,
            is_active=True,
        ),
        available_vaults=["/vaults/alpha"],
        csrf_token="t",
        is_self=False,
    )
    assert (
        '<option value="">(unassigned — every MCP tool refuses; '
        "index kept for reassignment)</option>"
    ) in rendered
    assert "vault tools error" not in rendered


# --- ownerless credentials in multi-user mode --------------------------------
#
# A key or token whose `user_id` is NULL is the single-user shape. It survives
# a configuration cycle: mint a key with multi-user off, then turn multi-user
# on *after* users already exist, and the bootstrap backfill in
# `src/auth/routes.py` — which only claims NULL rows while `users` is empty —
# never adopts it. Every layer then treated that credential as single-user: the
# middleware skipped the warm, `current_user_id` stayed None, and
# `_vault_root(None)` handed back the global `settings.vault_path`. An
# ownerless *readwrite* key could edit the whole vault.


@pytest.fixture
def multi_user_mode(monkeypatch):
    monkeypatch.setattr(vault.settings, "multi_user_mode", True)
    monkeypatch.setattr(mcp_auth.settings, "multi_user_mode", True)
    yield


def test_vault_root_refuses_an_ownerless_caller_in_multi_user_mode(multi_user_mode):
    with pytest.raises(RuntimeError) as excinfo:
        vault._vault_root(None)
    assert "multi-user" in str(excinfo.value)


def test_vault_root_still_serves_single_user_mode():
    """The same call, with multi-user off, is the legacy path and must work."""
    assert vault.settings.multi_user_mode is False
    assert vault._vault_root(None) == Path(vault.settings.vault_path)


def test_tools_refuse_an_ownerless_caller_in_multi_user_mode(
    cold_cache, multi_user_mode
):
    """Belt and braces: even if such a credential reached a tool, the gate
    refuses instead of resolving the global vault."""
    assert current_user_id.get() is None
    result, params, _ = _run_capturing_log(
        lambda: tools.edit_note_impl("Projects/Alpha.md", "clobbered")
    )
    assert result == tools._NO_VAULT_MESSAGE
    assert params["error"] == tools._NO_VAULT_MARKER


class _OAuthMiddlewareSession(_MiddlewareSession):
    """Same as `_MiddlewareSession`, for the OAuth branch: the first select
    hits `oauth_tokens` rather than `api_keys`."""

    async def execute(self, stmt):
        sql = str(stmt)
        if sql.startswith("UPDATE"):
            return _EmptyResult()
        if "vault_path" in sql:
            return _RowsResult([self.vault_row] if self.vault_row else [])
        if "FROM oauth_tokens" in sql:
            return _RowsResult([self.api_key])
        return _ScalarResult(self.user_active)


def _run_middleware(credential, *, token_value, downstream=None, oauth=False):
    """Drive `APIKeyMiddleware` with a faked session; return the sent messages."""
    sent = []

    async def _send(message):
        sent.append(message)

    async def _receive():  # pragma: no cover - never awaited
        return {"type": "http.request", "body": b"", "more_body": False}

    async def _ok(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def run():
        original = mcp_auth.async_session
        if oauth:
            mcp_auth.async_session = lambda: _OAuthMiddlewareSession(
                credential, vault_row=None
            )
        else:
            mcp_auth.async_session = lambda: _MiddlewareSession(
                credential, vault_row=None
            )
        try:
            app = mcp_auth.APIKeyMiddleware(downstream or _ok)
            await app(
                {
                    "type": "http",
                    "method": "POST",
                    "path": "/mcp/",
                    "headers": [(b"authorization", f"Bearer {token_value}".encode())],
                },
                _receive,
                _send,
            )
        finally:
            mcp_auth.async_session = original

    asyncio.run(run())
    return sent


def _body_of(sent):
    return b"".join(
        m.get("body", b"") for m in sent if m["type"] == "http.response.body"
    )


def test_middleware_rejects_an_ownerless_api_key_in_multi_user_mode(
    cold_cache, multi_user_mode
):
    from src.models.db import APIKey

    key = APIKey(
        id=9,
        key_hash="x",
        permission="readwrite",
        user_id=None,
        expires_at=None,
        is_active=True,
    )
    sent = _run_middleware(key, token_value="omcp_ownerless")
    assert sent[0]["status"] == 401
    # Same body as any other rejected key: which check failed is not disclosed.
    assert b"Invalid or revoked key" in _body_of(sent)


def test_middleware_rejects_an_ownerless_oauth_token_in_multi_user_mode(
    cold_cache, multi_user_mode
):
    from datetime import datetime, timedelta, timezone

    from src.models.db import OAuthToken

    tok = OAuthToken(
        id=11,
        token_hash="x",
        token_type="access",
        scope="readwrite",
        user_id=None,
        revoked=False,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    sent = _run_middleware(tok, token_value="oauth_ownerless", oauth=True)
    assert sent[0]["status"] == 401
    assert b"Invalid or revoked token" in _body_of(sent)


def test_middleware_accepts_an_ownerless_api_key_in_single_user_mode(cold_cache):
    """Single-user mode is the whole point of a NULL `user_id`. Untouched."""
    from src.models.db import APIKey

    assert mcp_auth.settings.multi_user_mode is False
    key = APIKey(
        id=9,
        key_hash="x",
        permission="readwrite",
        user_id=None,
        expires_at=None,
        is_active=True,
    )
    sent = _run_middleware(key, token_value="omcp_legacy")
    assert sent[0]["status"] == 200


# --- the panel vault browser loses the same race -----------------------------


def test_panel_vault_browser_refuses_when_the_warm_reports_unassigned(
    cold_cache, multi_user_mode
):
    """`vault_page` warmed the cache and then re-read the shared dict, so the
    same stale bulk warm that could re-admit a tool call could hand the panel
    an unassigned user's vault. It must use what the warm returned."""
    from starlette.requests import Request

    import src.control_panel.routes as panel

    captured = {}

    class _FakeTemplates:
        def TemplateResponse(self, request, name, ctx):
            captured["name"] = name
            captured["ctx"] = ctx
            return "rendered"

    async def fake_warm(session, user_id):
        # The request's own read says NULL, so it evicts...
        vault._user_vault_cache.pop(user_id, None)
        # ...and the indexer's older bulk query lands right here, putting the
        # revoked root back into the shared dict.
        vault._user_vault_cache[user_id] = STALE_ROOT
        return None

    original_templates = panel.templates
    original_warm = panel.warm_user_vault_cache
    panel.templates = _FakeTemplates()
    panel.warm_user_vault_cache = fake_warm
    try:
        request = Request({
            "type": "http",
            "method": "GET",
            "path": "/admin/vault",
            "query_string": b"",
            "headers": [],
        })
        user = SimpleNamespace(
            id=UNASSIGNED_UID, is_admin=False, username="bob", is_active=True
        )
        result = asyncio.run(panel.vault_page(request, session=None, user=user))
    finally:
        panel.templates = original_templates
        panel.warm_user_vault_cache = original_warm

    assert result == "rendered"
    assert captured["name"] == "vault.html"
    ctx = captured["ctx"]
    assert ctx["vault_error"], "the page did not refuse"
    assert str(UNASSIGNED_UID) in ctx["vault_error"]
    assert ctx["notes"] == [] and ctx["folders"] == []
    # And it did not browse the stale root the bulk warm restored.
    assert vault._user_vault_cache[UNASSIGNED_UID] == STALE_ROOT


# --- the gate costs no database round trip -----------------------------------


def test_admission_gate_issues_no_database_statements(cold_cache, tmp_path):
    """The gate runs on every tool call, so it must stay a cache/ContextVar
    read. Both database entry points reachable from it are booby-trapped: a
    single statement would raise."""
    import inspect

    # A synchronous function cannot await a DB round trip in the first place.
    assert not inspect.iscoroutinefunction(tools._vault_admission_error)

    ran = {}

    @tools._tracked("fake_tool", [])
    async def fake_tool() -> str:
        ran["yes"] = True
        return "served"

    def _boom(*a, **kw):
        raise AssertionError("the admission gate opened a database session")

    original_session = tools.async_session
    original_warm = vault.warm_user_vault_cache
    uid_token = current_user_id.set(UNASSIGNED_UID)
    root_token = current_vault_root.set((UNASSIGNED_UID, tmp_path))
    tools.async_session = _boom
    vault.warm_user_vault_cache = _boom
    try:
        result, _, _ = _run_capturing_log(lambda: fake_tool())
    finally:
        tools.async_session = original_session
        vault.warm_user_vault_cache = original_warm
        current_vault_root.reset(root_token)
        current_user_id.reset(uid_token)

    assert ran.get("yes") is True
    assert result == "served"
