"""Regression test for GitHub issue #8.

The `_tracked` usage-logging decorator mapped logged params to call args by
positional zipping: `param_keys[i] -> args[i]`. That assumption breaks for any
tool whose call passes a non-logged positional arg between logged ones.

`edit_note_impl(path, content, ...)` is the live victim: its decorator omits
`content` from `param_keys` on purpose (the note body is large/sensitive), but
the server calls it as `edit_note_impl(path, content, append=..., ...)`. The
old loop matched key="append" -> args[1] == content, so the note body landed in
`usage_logs.params["append"]` and the real `append` boolean (in kwargs) was
dropped.

The fix resolves logged params by NAME via the wrapped function's signature.
These tests exercise `_tracked` directly with a fake function carrying the same
signature shape, capturing the params the decorator would log by stubbing out
`_log_usage`. No DB / network / embedding access — fully offline.
"""

import asyncio
import os
import tempfile

# `src.mcp_server.tools` pulls in `src.config`, whose module-level `Settings()`
# reads `./.env`. The real `.env` on this host carries forbidden host-only keys,
# so provide minimal defaults and chdir to a dir without a `.env` (env_file is
# resolved relative to CWD) BEFORE importing. Keeps the module fully offline.
os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
os.chdir(tempfile.gettempdir())

import src.mcp_server.tools as tools  # noqa: E402


def _capture_logged_params(fn, *args, **kwargs):
    """Invoke a `_tracked`-decorated coroutine and return the params dict the
    decorator handed to `_log_usage`, with the DB call stubbed out."""
    captured = {}

    async def fake_log_usage(tool, params, duration_ms, response_size):
        captured["tool"] = tool
        captured["params"] = params

    original = tools._log_usage
    tools._log_usage = fake_log_usage
    try:
        asyncio.run(fn(*args, **kwargs))
    finally:
        tools._log_usage = original
    return captured["params"]


def _make_edit_note_like():
    """A stand-in with edit_note_impl's exact signature and param_keys: `content`
    is positional but intentionally NOT logged."""

    @tools._tracked(
        "edit_note",
        ["path", "append", "find", "section", "replace_all", "dry_run"],
    )
    async def edit_note_like(
        path: str,
        content: str,
        append: bool = False,
        find: str | None = None,
        section: str | None = None,
        replace_all: bool = False,
        dry_run: bool = False,
    ) -> str:
        return "ok"

    return edit_note_like


def test_non_logged_positional_does_not_shift_param_mapping():
    """The bug case: server-style call with `content` positional and the rest
    as kwargs. `append` must log the boolean, NOT the note body."""
    fn = _make_edit_note_like()
    params = _capture_logged_params(
        fn,
        "notes/foo.md",
        "THE NOTE BODY CONTENT",
        append=True,
        find=None,
        section=None,
        replace_all=False,
        dry_run=False,
    )

    # The real append flag is logged...
    assert params["append"] is True
    # ...and the note body never leaks into any logged field.
    assert "THE NOTE BODY CONTENT" not in params.values()
    assert params["path"] == "notes/foo.md"
    # `content` was deliberately omitted from param_keys and must stay out.
    assert "content" not in params
    assert params["dry_run"] is False


def test_all_positional_call_still_maps_by_name():
    """Even when every arg is positional, mapping is by name, so the
    non-logged `content` slot is skipped rather than mislabelled."""
    fn = _make_edit_note_like()
    params = _capture_logged_params(
        fn,
        "notes/bar.md",
        "BODY",
        True,  # append
        "needle",  # find
        None,  # section
        True,  # replace_all
        False,  # dry_run
    )
    assert params["path"] == "notes/bar.md"
    assert params["append"] is True
    assert params["find"] == "needle"
    assert params["replace_all"] is True
    assert "BODY" not in params.values()
    assert "content" not in params


def test_defaults_are_logged_when_omitted():
    """Params left at their defaults are still logged (apply_defaults)."""
    fn = _make_edit_note_like()
    params = _capture_logged_params(fn, "notes/baz.md", "BODY")
    assert params["append"] is False
    assert params["find"] is None
    assert params["dry_run"] is False
