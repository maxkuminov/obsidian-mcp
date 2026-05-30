"""Regression test for GitHub issue #9.

`edit_note(..., find="", replace_all=True)` corrupted the note. The
find/replace branch only checked `find is not None`, so an empty string flowed
through to `existing.replace("", content)`, which inserts `content` between
every character of the note (e.g. "Hi" -> "XHXiX"). `existing.count("")` is
`len(existing) + 1` (positive), so the "not found" guard never tripped; with
`replace_all=True` the "multiple matches" guard is bypassed and the corrupting
replace runs.

The fix rejects an empty `find` with a clear error and leaves the note
untouched. This test invokes the real `edit_note_impl` against a temp-dir
vault, with the usage-logging DB call stubbed out — fully offline (no DB,
network, or embedding access).
"""

import asyncio
import os
import tempfile

# `src.mcp_server.tools` pulls in `src.config`, whose module-level `Settings()`
# reads `./.env`. Provide minimal defaults and point the vault at a temp dir
# BEFORE importing, and chdir to a dir without a `.env` (env_file is resolved
# relative to CWD). Keeps the module fully offline.
_VAULT_DIR = tempfile.mkdtemp(prefix="issue9-vault-")
os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ["VAULT_PATH"] = _VAULT_DIR
os.chdir(tempfile.gettempdir())

import src.mcp_server.tools as tools  # noqa: E402
from src.mcp_server.auth import current_permission  # noqa: E402
from src.config import settings  # noqa: E402

# The module-level `settings` singleton may already have been constructed by an
# earlier test in the suite (with a different VAULT_PATH), in which case setting
# `os.environ["VAULT_PATH"]` above has no effect. Pin the vault root directly so
# `_vault_root(None)` -> `Path(settings.vault_path)` resolves to our temp vault
# regardless of import order.
settings.vault_path = _VAULT_DIR


def _run_edit(path, **kwargs):
    """Invoke the real edit_note_impl with usage logging stubbed and a
    readwrite permission set on the contextvar."""
    async def _noop_log(tool, params, duration_ms, response_size):
        return None

    original = tools._log_usage
    tools._log_usage = _noop_log
    perm_token = current_permission.set("readwrite")
    try:
        return asyncio.run(tools.edit_note_impl(path, **kwargs))
    finally:
        current_permission.reset(perm_token)
        tools._log_usage = original


_ORIGINAL = "# Title\n\nHello world, this is a note.\n"


def _fresh_note(name="note.md"):
    full = os.path.join(_VAULT_DIR, name)
    with open(full, "w", encoding="utf-8") as fh:
        fh.write(_ORIGINAL)
    return name, full


def test_empty_find_with_replace_all_is_rejected_and_note_untouched():
    """The corruption case: empty find + replace_all bypasses the
    multiple-match guard and inserts content between every character."""
    rel, full = _fresh_note("empty_find.md")

    result = _run_edit(rel, content="INJECTED", find="", replace_all=True)

    # An informative error is returned...
    assert "non-empty" in result.lower() or "empty" in result.lower()
    assert "INJECTED" not in result
    # ...and crucially the note on disk is byte-identical (not corrupted).
    with open(full, encoding="utf-8") as fh:
        assert fh.read() == _ORIGINAL


def test_empty_find_single_match_path_is_rejected():
    """Empty find without replace_all must also be rejected up front (rather
    than relying on the incidental multiple-match guard)."""
    rel, full = _fresh_note("empty_find_single.md")

    result = _run_edit(rel, content="INJECTED", find="")

    assert "non-empty" in result.lower() or "empty" in result.lower()
    with open(full, encoding="utf-8") as fh:
        assert fh.read() == _ORIGINAL


def test_empty_find_dry_run_does_not_corrupt():
    rel, full = _fresh_note("empty_find_dry.md")

    result = _run_edit(
        rel, content="INJECTED", find="", replace_all=True, dry_run=True
    )

    assert "empty" in result.lower() or "non-empty" in result.lower()
    with open(full, encoding="utf-8") as fh:
        assert fh.read() == _ORIGINAL


def test_nonempty_find_still_replaces():
    """Sanity: the guard does not break ordinary find/replace."""
    rel, full = _fresh_note("normal_find.md")

    result = _run_edit(rel, content="Goodbye world", find="Hello world")

    assert "Updated note" in result
    with open(full, encoding="utf-8") as fh:
        body = fh.read()
    assert "Goodbye world" in body
    assert "Hello world" not in body
