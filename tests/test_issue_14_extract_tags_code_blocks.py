"""Regression test for GitHub issue #14.

`extract_tags` scanned inline `#tags` over the *raw* note body, so `#token`
forms inside fenced code blocks or inline backtick spans were captured as
vault tags (e.g. `#anothercomment` in a python block, or `#inlinecode` in a
backtick span). The docstring/comment claimed code blocks were excluded, but
the body was never code-masked. The fix masks code via `mask_code` before the
inline-tag scan, matching the precedent in `_scan_headings`.

`extract_tags` is a pure dict/string -> list function with no DB / network /
embedding dependencies, so the whole module runs fully offline.
"""

import os
import tempfile

# Importing `src.services.vault` pulls in `src.config`, whose module-level
# `Settings()` singleton reads `./.env`. On this host the real `.env` carries
# host-only keys the model forbids, so we must NOT let that file load. Provide
# the same minimal defaults conftest uses and chdir to a dir without a `.env`
# (env_file is resolved relative to CWD) BEFORE importing the module.
os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
os.chdir(tempfile.gettempdir())

from src.services.vault import extract_tags  # noqa: E402


def test_fenced_code_block_tags_are_not_captured():
    """The bug case: `#tags` inside a fenced code block must be ignored."""
    raw = (
        "Intro with a #realtag here.\n"
        "\n"
        "```python\n"
        "x = 1  #anothercomment\n"
        "```\n"
        "\n"
        "Trailing #afterblock tag.\n"
    )
    tags = extract_tags(raw, {})
    assert "realtag" in tags
    assert "afterblock" in tags
    # The comment inside the python fence must NOT become a vault tag.
    assert "anothercomment" not in tags


def test_inline_code_span_tags_are_not_captured():
    """`#tags` inside a backtick span must be ignored."""
    raw = "See `#inlinecode` but keep #realtag.\n"
    tags = extract_tags(raw, {})
    assert "realtag" in tags
    assert "inlinecode" not in tags


def test_legitimate_body_tags_still_captured():
    """Regression guard: real inline tags outside code are preserved,
    including nested forms and start-of-line tags."""
    raw = "#start of line tag\nmid #project/sub line\nend #done\n"
    tags = extract_tags(raw, {})
    assert tags == ["done", "project/sub", "start"]


def test_frontmatter_tags_unaffected():
    """Frontmatter tag handling stays unchanged and combines with body tags."""
    raw = "Body with #bodytag.\n"
    tags = extract_tags(raw, {"tags": ["fm-one", "fm-two"]})
    assert "fm-one" in tags
    assert "fm-two" in tags
    assert "bodytag" in tags


def test_tag_immediately_after_fence_close():
    """A tag on the line right after a closing fence must still match.

    `mask_code` replaces the fence with same-length whitespace but preserves
    newlines, so the `^`/`\\s` boundary still fires for `#afterfence`.
    """
    raw = "```\ncode #hidden\n```\n#afterfence works\n"
    tags = extract_tags(raw, {})
    assert "afterfence" in tags
    assert "hidden" not in tags
