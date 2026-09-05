r"""Regression test for GitHub issue #20.

Raw-space markdown links to notes (`[a](My Note.md)`, `[a](folder/My Note.md)`,
`[a](My Note.md#anchor)`, and the angle-bracket form `[a](<My Note.md>)`) were
dropped by the markdown-link extractor and rewriter because the href character
class forbade ALL whitespace (`[^)\s]`). Only the `%20`-encoded form matched.

These tests run fully offline: they exercise the pure extraction code in
src/services/links.py and the rewrite grammar used by src/mcp_server/tools.py
without any DB, network, or embedding provider.

Both grammars are now `scan_md_links` (#180 slice A2) rather than a pair of
regexes; the probes below call it directly where they used to call a compiled
pattern. Its exactness against the retired regexes is pinned separately, in
tests/test_asvs_mdlink_scanner.py.
"""

import os
import tempfile

# The autouse fixture in conftest imports `src.services.embeddings`, which pulls
# in `src.config`, whose module-level `Settings()` reads `./.env`. On this host
# the real `.env` carries host-only keys the model forbids. `env_file=".env"` is
# resolved relative to CWD, so chdir to a dir without a `.env` before importing.
os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
os.chdir(tempfile.gettempdir())

from src.services.links import extract_links, scan_md_links  # noqa: E402


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


def test_extract_scanner_matches_raw_space_directly():
    # Direct probe of the extraction grammar, under the extraction flags.
    assert list(scan_md_links("[a](My Note.md)"))
    assert list(scan_md_links("[a](folder/My Note.md)"))


# ── Rewrite grammar used by move_note(rewrite_links=True) ────────────────────
# The same scanner under the two flags that reproduce the rewrite grammar's
# PRE-EXISTING divergences from extraction (no `<href>` alternative; the anchor
# class crosses newlines). Kept as a local literal so these assertions run
# without importing the MCP tools module, which has heavier deps; the
# literal-equality test at the bottom keeps the copy honest.
_REWRITE_FLAGS = {"angle": False, "anchor_crosses_newlines": True}


def _rewrite_scan(content):
    return list(scan_md_links(content, **_REWRITE_FLAGS))


def test_rewrite_scanner_matches_raw_space_href():
    # Before the fix the href group's `[^)\s]` made this return nothing.
    (link,) = _rewrite_scan("[a](My Note.md)")
    assert link.href == "My Note.md"


def test_rewrite_scanner_matches_raw_space_href_with_folder_and_anchor():
    (link,) = _rewrite_scan("[a](folder/My Note.md#anchor)")
    assert link.href == "folder/My Note.md"
    assert link.anchor == "#anchor"


def test_rewrite_scanner_flags_match_source_module_literal():
    # Guard against the in-test copy diverging from the real source flags.
    from src.mcp_server import tools

    assert tools.MDLINK_REWRITE_FLAGS == _REWRITE_FLAGS
