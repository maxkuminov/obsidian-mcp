r"""Regression test for GitHub issue #20.

Raw-space markdown links to notes (`[a](My Note.md)`, `[a](folder/My Note.md)`,
`[a](My Note.md#anchor)`, and the angle-bracket form `[a](<My Note.md>)`) were
dropped by the markdown-link extractor and rewriter because the href character
class forbade ALL whitespace (`[^)\s]`). Only the `%20`-encoded form matched.

These tests run fully offline: they exercise the pure regex / extraction code
in src/services/links.py and the rewrite regex in src/mcp_server/tools.py
without any DB, network, or embedding provider.
"""

import os
import re
import tempfile

# The autouse fixture in conftest imports `src.services.embeddings`, which pulls
# in `src.config`, whose module-level `Settings()` reads `./.env`. On this host
# the real `.env` carries host-only keys the model forbids. `env_file=".env"` is
# resolved relative to CWD, so chdir to a dir without a `.env` before importing.
os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
os.chdir(tempfile.gettempdir())

from src.services.links import _MDLINK_RE, extract_links  # noqa: E402


def _markdown_targets(content: str) -> list[str]:
    return [
        link.target
        for link in extract_links(content)
        if link.kind == "markdown"
    ]


def test_raw_space_markdown_link_extracted():
    # Before the fix, this produced no markdown ExtractedLink.
    targets = _markdown_targets("See [a](My Note.md) for details.")
    assert "My Note" in targets


def test_raw_space_markdown_link_with_folder_extracted():
    targets = _markdown_targets("See [a](folder/My Note.md).")
    assert "folder/My Note" in targets


def test_raw_space_markdown_link_with_anchor_extracted():
    targets = _markdown_targets("See [label](My Note.md#anchor).")
    assert "My Note" in targets


def test_angle_bracket_markdown_link_extracted():
    targets = _markdown_targets("See [a](<My Note.md>).")
    assert "My Note" in targets


def test_percent_encoded_form_still_works():
    # Regression guard: the previously-working encoded form is unchanged.
    targets = _markdown_targets("See [a](My%20Note.md).")
    assert "My Note" in targets


def test_non_md_href_still_rejected():
    # An image link followed later by a `.md)` must not be slurped into one
    # giant match. `.png)` closes the paren before any `.md`.
    targets = _markdown_targets("![img](image.png) then file.md)")
    assert "image" not in [t.lower() for t in targets]
    # And a plain non-note href is ignored entirely.
    assert _markdown_targets("[ext](https://example.com/page) text") == []


def test_extract_regex_matches_raw_space_directly():
    # Direct probe of the compiled module-level regex.
    assert _MDLINK_RE.search("[a](My Note.md)") is not None
    assert _MDLINK_RE.search("[a](folder/My Note.md)") is not None


# ── Rewrite regex used by move_note(rewrite_links=True) ──────────────────────
# Mirrors src/mcp_server/tools.py::_MDLINK_REWRITE_RE. Kept as a local literal
# so this assertion runs without importing the MCP tools module (which has
# heavier deps); if the source regex drifts, update this copy too.

# Kept in step with `tools._MDLINK_REWRITE_RE` by the literal-equality test at
# the bottom of this module. The closed classes and the 2,048-char href bound
# are #180's linear grammar — see `src/services/links.py`.
_MDLINK_REWRITE_RE = re.compile(
    r"\[(?P<text>[^\[\]\n]++)\]\((?P<href>[^)\n]{1,2048}?\.md)(?P<anchor>#[^)]*+)?\)"
)


def test_rewrite_regex_matches_raw_space_href():
    # Before the fix the href group's `[^)\s]` made this return None.
    m = _MDLINK_REWRITE_RE.search("[a](My Note.md)")
    assert m is not None
    assert m.group("href") == "My Note.md"


def test_rewrite_regex_matches_raw_space_href_with_folder_and_anchor():
    m = _MDLINK_REWRITE_RE.search("[a](folder/My Note.md#anchor)")
    assert m is not None
    assert m.group("href") == "folder/My Note.md"
    assert m.group("anchor") == "#anchor"


def test_rewrite_regex_matches_source_module_literal():
    # Guard against the in-test copy diverging from the real source regex.
    from src.mcp_server import tools

    assert tools._MDLINK_REWRITE_RE.pattern == _MDLINK_REWRITE_RE.pattern
