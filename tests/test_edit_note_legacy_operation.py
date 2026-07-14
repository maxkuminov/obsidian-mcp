"""Regression tests for clients using the legacy ``operation`` selector.

An older client sent ``operation="append"``. If that argument is discarded,
``edit_note`` falls through to its default full-replace mode and destroys the
existing note. Keep the alias explicit so that call shape is safe.
"""

import asyncio
import os
import tempfile

_VAULT_DIR = tempfile.mkdtemp(prefix="edit-operation-vault-")
os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ["VAULT_PATH"] = _VAULT_DIR
os.chdir(tempfile.gettempdir())

import src.mcp_server.tools as tools  # noqa: E402
from src.config import settings  # noqa: E402
from src.mcp_server.auth import current_permission  # noqa: E402

settings.vault_path = _VAULT_DIR


def _run_edit(path, **kwargs):
    async def _noop_log(tool, params, duration_ms, response_size):
        return None

    original_log = tools._log_usage
    original_vault_path = settings.vault_path
    tools._log_usage = _noop_log
    settings.vault_path = _VAULT_DIR
    token = current_permission.set("readwrite")
    try:
        return asyncio.run(tools.edit_note_impl(path, **kwargs))
    finally:
        current_permission.reset(token)
        settings.vault_path = original_vault_path
        tools._log_usage = original_log


def _fresh_note(name, content):
    full = os.path.join(_VAULT_DIR, name)
    with open(full, "w", encoding="utf-8") as fh:
        fh.write(content)
    return full


def test_legacy_append_operation_preserves_frontmatter_and_body():
    original = "---\nstatus: scratch\n---\n\n# Title\n\n## Log\n\n- original\n"
    full = _fresh_note("legacy-append.md", original)

    result = _run_edit(
        "legacy-append.md", content="- appended", operation="append"
    )

    assert "Updated note" in result
    with open(full, encoding="utf-8") as fh:
        assert fh.read() == original + "\n- appended"


def test_unknown_operation_is_rejected_without_writing():
    original = "# Keep me\n"
    full = _fresh_note("unknown-operation.md", original)

    result = _run_edit(
        "unknown-operation.md", content="replacement", operation="prepend"
    )

    assert "must be" in result
    with open(full, encoding="utf-8") as fh:
        assert fh.read() == original


def test_replace_operation_cannot_be_combined_with_append():
    original = "# Keep me\n"
    full = _fresh_note("conflicting-operation.md", original)

    result = _run_edit(
        "conflicting-operation.md",
        content="replacement",
        append=True,
        operation="replace",
    )

    assert "choose at most one" in result
    with open(full, encoding="utf-8") as fh:
        assert fh.read() == original
