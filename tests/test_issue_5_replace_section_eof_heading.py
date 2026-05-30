"""Regression test for GitHub issue #5.

`replace_section` corrupted a note when the target was the last heading and
that heading line had no trailing newline: the new body was glued directly
onto the heading text (e.g. "# Notes- item" instead of "# Notes\n- item").

These tests exercise `replace_section` directly. It is a pure
string-to-string function with no DB / network / embedding dependencies, so
the whole module runs fully offline.
"""

import os
import tempfile

# Importing `src.services.vault` pulls in `src.config`, whose module-level
# `Settings()` singleton reads `./.env`. On this host the real `.env` carries
# host-only keys the model forbids, so we must NOT let that file load. Provide
# the same minimal defaults conftest uses and chdir to a dir without a `.env`
# (env_file is resolved relative to CWD) BEFORE importing the module. This
# keeps the test fully self-contained and offline regardless of where pytest
# is invoked from.
os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
os.chdir(tempfile.gettempdir())

from src.services.vault import replace_section  # noqa: E402


def test_eof_heading_without_trailing_newline_is_separated():
    """The bug case: last heading, no trailing newline on the heading line."""
    text = "# Notes"  # no trailing newline at all
    new_text, error = replace_section(text, "Notes", "- item")
    assert error is None
    assert new_text == "# Notes\n- item"
    # The heading text must remain intact (not "# Notes- item").
    assert new_text.startswith("# Notes\n")


def test_eof_heading_with_one_trailing_newline_not_double_spaced():
    """Regression guard: a heading with exactly one trailing newline is
    already correct. The fix must NOT add a spurious blank line here (the
    issue's suggested `body_start == line_end` test would have)."""
    text = "# Notes\n"
    new_text, error = replace_section(text, "Notes", "- item")
    assert error is None
    assert new_text == "# Notes\n- item"


def test_eof_heading_with_existing_body_replaced_cleanly():
    """Last heading with a body already present, no double newline."""
    text = "# Notes\nold line\n"
    new_text, error = replace_section(text, "Notes", "new line")
    assert error is None
    assert new_text == "# Notes\nnew line"


def test_mid_file_section_unaffected():
    """A section followed by another heading still behaves as before."""
    text = "# A\nbody a\n\n# B\nbody b\n"
    new_text, error = replace_section(text, "A", "replaced")
    assert error is None
    assert new_text == "# A\nreplaced\n# B\nbody b\n"


def test_eof_heading_no_newline_multiline_body():
    """EOF heading without trailing newline, multi-line replacement body."""
    text = "intro\n\n# Tasks"
    new_text, error = replace_section(text, "Tasks", "- a\n- b")
    assert error is None
    assert new_text == "intro\n\n# Tasks\n- a\n- b"
