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

These tests exercise the real tool impls with an empty vault cache. A refusal
happens *before* the body, so no DB, network or embedding access occurs — if
the gate regressed, the tools would try to open a database connection and the
tests would fail loudly rather than silently pass.
"""

import asyncio
import os
import tempfile

# `src.mcp_server.tools` pulls in `src.config`, whose module-level `Settings()`
# reads `./.env`. Provide minimal defaults and chdir to a dir without a `.env`
# BEFORE importing, keeping the module fully offline. (Same preamble as
# tests/test_issue_8_tracked_param_mapping.py.)
os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
os.chdir(tempfile.gettempdir())

import pytest  # noqa: E402

import src.mcp_server.tools as tools  # noqa: E402
import src.services.vault as vault  # noqa: E402
from src.auth.session import _SingleUserSentinel, current_user_id  # noqa: E402


UNASSIGNED_UID = 4242


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
    try:
        yield UNASSIGNED_UID
    finally:
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


# --- (a) DB-backed and graph tools refuse ------------------------------------


@pytest.mark.parametrize(
    "name, call",
    [
        # DB-only: never touched `_vault_root` before this fix.
        ("semantic_search", lambda: tools.semantic_search_impl("salary negotiation")),
        ("keyword_search", lambda: tools.search_notes_impl("salary negotiation")),
        ("list_notes", lambda: tools.list_notes_impl()),
        ("get_recent", lambda: tools.get_recent_impl()),
        ("get_tags", lambda: tools.get_tags_impl()),
        # Graph tools: same position.
        ("get_backlinks", lambda: tools.get_backlinks_impl("Projects/Alpha.md")),
        ("get_links", lambda: tools.get_links_impl("Projects/Alpha.md")),
        ("get_neighborhood", lambda: tools.get_neighborhood_impl("Projects/Alpha.md")),
        ("find_orphans", lambda: tools.find_orphans_impl()),
        ("find_related", lambda: tools.find_related_impl("Projects/Alpha.md")),
        # Disk-touching tools already errored, but must keep doing so through
        # the shared gate rather than by accident deeper down.
        ("read_note", lambda: tools.read_note_impl("Projects/Alpha.md")),
        ("get_vault_guide", lambda: tools.get_vault_guide_impl()),
        ("list_files", lambda: tools.list_files_impl()),
    ],
)
def test_unassigned_user_is_refused_by_every_tool(as_unassigned_user, name, call):
    result, params, _ = _run_capturing_log(call)
    assert isinstance(result, str), f"{name} returned {type(result)!r}"
    assert result == tools._NO_VAULT_MESSAGE, f"{name} did not refuse: {result[:200]!r}"
    # The refusal must not name a path, a note or an excerpt.
    assert "Projects/Alpha.md" not in result
    assert params is not None, f"{name} refusal was not logged"


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


def test_warm_user_vault_cache_evicts_when_the_row_is_gone(cold_cache):
    """`warm_user_vault_cache` runs on every authenticated MCP request. It used
    to be a silent no-op for a NULL `vault_path`, which left a previously
    cached root in place — so a mid-session unassignment stayed invisible in
    any worker process that did not handle the panel request. It must now
    *evict*, making the gate independent of the panel's
    `clear_user_vault_cache` call and of the worker that served it."""
    from pathlib import Path

    class _EmptyResult:
        def first(self):
            return None

    class _FakeSession:
        async def execute(self, stmt):
            return _EmptyResult()

    vault._user_vault_cache[UNASSIGNED_UID] = Path("/vaults/alpha")
    asyncio.run(vault.warm_user_vault_cache(_FakeSession(), UNASSIGNED_UID))
    assert UNASSIGNED_UID not in vault._user_vault_cache

    with pytest.raises(RuntimeError):
        vault._vault_root(UNASSIGNED_UID)


def test_gate_refuses_on_a_cold_cache_rather_than_raising(as_unassigned_user):
    """A fresh process that never warmed this user must refuse with a tool
    error, not a 500. (`warm_user_vault_cache` is a no-op there — there is
    nothing to evict — so the gate cannot lean on eviction alone.)"""
    assert UNASSIGNED_UID not in vault._user_vault_cache
    result, params, _ = _run_capturing_log(lambda: tools.get_backlinks_impl("A.md"))
    assert result == tools._NO_VAULT_MESSAGE
    assert params["error"] == tools._NO_VAULT_MARKER
