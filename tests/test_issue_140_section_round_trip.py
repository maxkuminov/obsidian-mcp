"""Section read/write parity (#140).

`read_note(section=…)` and `edit_note(section=…)` disagreed about where a
section's body *begins*. `_ATX_HEADING_RE`'s trailing run was `\\s*`, which is
not restricted to horizontal whitespace, so the heading match ran across blank
lines — and, because `_scan_headings` scans masked text where a fenced block is
an equal-length run of spaces, straight through a fenced block sitting under a
heading too. Everything the run swallowed was returned by `extract_section`
(which starts from `line_start`) and lay *outside* the span `replace_section`
replaces (which starts one terminator past `line_end`): readable but
unwritable. An agent that read a section, changed it, and wrote it back
duplicated whatever sat in that gap.

The fix narrows the trailing run to `[^\\S\\r\\n]*` and makes both of
`replace_section`'s separator insertions conditional on a non-empty body. The
contract that follows is one sentence: **a section's body begins on the line
immediately after the heading line, and a section write replaces all of it.**

These tests exercise the pure helpers in `src.services.vault` — no DB, no
vault, no async — except for the tool-level cases at the bottom, which use a
`tmp_path` vault and a stubbed usage log.
"""

import asyncio
import os
import re
import tempfile

# See `test_issue_5_replace_section_eof_heading.py` for why this must happen
# before importing `src.services.vault`: `src.config`'s module-level
# `Settings()` reads `./.env` relative to CWD, and a host's real `.env` carries
# keys these tests must never load.
os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
os.chdir(tempfile.gettempdir())

import pytest  # noqa: E402

import src.mcp_server.tools as tools  # noqa: E402
from src.mcp_server.auth import current_permission  # noqa: E402
from src.services import vault as vault_service  # noqa: E402
from src.services import vault_fs  # noqa: E402
from src.services.vault import (  # noqa: E402
    _scan_headings,
    extract_section,
    outline_sections,
    replace_section,
)

# ── helpers ─────────────────────────────────────────────────────────────────

_FIRST_LINE_RE = re.compile(r"[^\r\n]*(?:\r\n|\n|\r)?")


def body_of(section_text: str) -> str:
    """A section's text minus its heading line and that line's terminator.

    This is a *test* helper over raw note text. It is deliberately NOT a
    documented procedure over a rendered `read_note` response: that response is
    an envelope interpolating note-controlled values, and every textual rule
    for recovering the selected content from it proved forgeable. See
    `docs/architecture/vault-tools.md` and issue #149.
    """
    return section_text[_FIRST_LINE_RE.match(section_text).end():]


def round_trip(text: str, selector: str) -> str:
    """Read a section, write its body straight back, return the new note."""
    section, err = extract_section(text, selector)
    assert err is None, err
    new_text, err = replace_section(text, selector, body_of(section))
    assert err is None, err
    return new_text


# ── the corpus ──────────────────────────────────────────────────────────────

LF_CORPUS = {
    "blank_line_after_heading": "# A\n\nbody\n\n# B\nb\n",
    "two_blank_lines_after_heading": "# A\n\n\nbody\n# B\nb\n",
    "fence_under_heading": "# A\n```\n## Hidden\ntext\n```\n# B\nb\n",
    "tilde_fence_under_heading": "# A\n~~~\n# Hidden\ntext\n~~~\n# B\nb\n",
    "fence_with_language_and_blanks": (
        "# A\n\n```python\n# not a heading\nx = 1\n```\n\ntail\n# B\nb\n"
    ),
    "inline_code_on_heading_line": "# A `# code` tail\nbody\n# B\nb\n",
    "eof_heading_with_trailing_newline": "# A\nbody\n# Notes\n",
    "eof_heading_without_trailing_newline": "# A\nbody\n# Notes",
    "eof_body_without_trailing_newline": "# A\nbody\n# Notes\n- item",
    "trailing_spaces_and_tabs": "# A   \nbody\n# B\t\t\nb\n",
    # The two characters after `A` in the next entry are U+00A0 NBSP, not
    # spaces. The trailing class must be "horizontal whitespace", not "space or
    # tab" — the separator class was widened for exactly that reason, and
    # narrowing the trailing one must not narrow it past Unicode.
    "trailing_nbsp": "# A  \nbody\n# B\nb\n",
    "headings_only": "# A\n## B\n### C\n# D\n",
    "empty_section_between_headings": "# A\n# B\nb\n",
    "empty_section_at_eof_no_terminator": "# A\nbody\n# B",
    "nested_deeper_then_shallower": "# A\nbody a\n## A1\nsub\n### A2\nsubsub\n# B\nb\n",
    "shallower_follows_deeper": "## Deep\nd\n# Shallow\ns\n",
    "preamble_before_first_heading": "intro text\n\n# A\nbody\n# B\nb\n",
}

# Every LF note again in the other two dialects. #128 established that the
# section helpers must treat CRLF as a unit and a lone CR as a terminator; this
# change must not disturb either.
CORPUS: dict[str, str] = {}
for _name, _text in LF_CORPUS.items():
    CORPUS[_name] = _text
    CORPUS[_name + "@crlf"] = _text.replace("\n", "\r\n")
    CORPUS[_name + "@cr"] = _text.replace("\n", "\r")

# No corpus entry carries frontmatter. Section mode's frontmatter rules live at
# the tool layer (#128) and are exercised there, at the bottom of this module —
# including the deliberate asymmetry a defective block keeps: readable by
# section, refused for section writes.


# ── 1. the two reported reproductions ───────────────────────────────────────


def test_a_fenced_block_under_a_heading_is_not_duplicated():
    """Reproduction 1 (#140). The masker turns the fence into a run of spaces,
    the old trailing `\\s*` ran through it, and the write span began *after*
    the block — so writing the read body back left the original in place and
    inserted a second copy. Before the fix this produced
    `'# A\\n```\\n## Hidden\\ntext\\n```\\n```\\n## Hidden\\ntext\\n```\\n# B\\nb\\n'`.
    """
    text = "# A\n```\n## Hidden\ntext\n```\n# B\nb\n"
    result = round_trip(text, "#1")
    assert result == text
    assert result.count("```") == 2


def test_blank_lines_do_not_accumulate_across_round_trips():
    """Reproduction 2 (#140). A blank line after a heading was inside what the
    read returned and outside what the write replaced, so every round trip
    re-emitted it on top of the retained one. Measured growth before the fix:
    +1, +2, +4 blank lines over three round trips."""
    text = "# A\n\nbody\n\n# B\nb\n"
    current = text
    for _ in range(3):
        current = round_trip(current, "#1")
        assert current == text


# ── 3.1/3.2/3.3 the differential property, over the corpus ──────────────────


@pytest.mark.parametrize("name", sorted(CORPUS))
def test_every_section_round_trips_byte_identically(name):
    """The spec requirement in executable form: for every ordinal in the note,
    writing back exactly what the read returned below the heading line leaves
    the note unchanged."""
    text = CORPUS[name]
    for ordinal in range(1, len(_scan_headings(text)) + 1):
        assert round_trip(text, f"#{ordinal}") == text, f"{name} #{ordinal}"


@pytest.mark.parametrize("name", sorted(CORPUS))
def test_a_round_trip_is_idempotent(name):
    """Repeating the round trip cannot drift. The blank-line bug was only
    visible because each pass fed the previous pass's larger prefix back in."""
    text = CORPUS[name]
    for ordinal in range(1, len(_scan_headings(text)) + 1):
        current = text
        for _ in range(3):
            current = round_trip(current, f"#{ordinal}")
        assert current == text, f"{name} #{ordinal}"


@pytest.mark.parametrize("name", sorted(n for n in CORPUS if n.endswith("@crlf")))
def test_a_crlf_note_keeps_crlf_through_a_helper_round_trip(name):
    """3.3 — at the helper level the note's own dialect survives: the helpers
    work on raw text, so no terminator is rewritten. (The tool level *does*
    normalise; that declared residual is pinned separately below.)"""
    text = CORPUS[name]
    for ordinal in range(1, len(_scan_headings(text)) + 1):
        result = round_trip(text, f"#{ordinal}")
        assert result == text
        stripped = result.replace("\r\n", "")
        assert "\n" not in stripped and "\r" not in stripped


@pytest.mark.parametrize("name", sorted(n for n in CORPUS if n.endswith("@cr")))
def test_a_lone_cr_note_keeps_lone_cr_through_a_helper_round_trip(name):
    text = CORPUS[name]
    for ordinal in range(1, len(_scan_headings(text)) + 1):
        result = round_trip(text, f"#{ordinal}")
        assert result == text
        assert "\n" not in result


# ── 3.4 the non-regression that makes the narrowing admissible ──────────────

# Explicit values, captured from the tree BEFORE the narrowing and asserted
# against it after. Not a comparison of two regexes: these literals are the
# old behaviour, frozen. `(depth, trimmed text, line_start)` per heading, in
# document order. If any of these move, a `#N` ordinal or a selector moved with
# it, and existing notes have been silently re-addressed.
EXPECTED_HEADINGS = {
    'blank_line_after_heading': [(1, 'A', 0), (1, 'B', 11)],
    'blank_line_after_heading@crlf': [(1, 'A', 0), (1, 'B', 15)],
    'blank_line_after_heading@cr': [(1, 'A', 0), (1, 'B', 11)],
    'two_blank_lines_after_heading': [(1, 'A', 0), (1, 'B', 11)],
    'two_blank_lines_after_heading@crlf': [(1, 'A', 0), (1, 'B', 15)],
    'two_blank_lines_after_heading@cr': [(1, 'A', 0), (1, 'B', 11)],
    'fence_under_heading': [(1, 'A', 0), (1, 'B', 27)],
    'fence_under_heading@crlf': [(1, 'A', 0), (1, 'B', 32)],
    'fence_under_heading@cr': [(1, 'A', 0), (1, 'B', 27)],
    'tilde_fence_under_heading': [(1, 'A', 0), (1, 'B', 26)],
    'tilde_fence_under_heading@crlf': [(1, 'A', 0), (1, 'B', 31)],
    'tilde_fence_under_heading@cr': [(1, 'A', 0), (1, 'B', 26)],
    'fence_with_language_and_blanks': [(1, 'A', 0), (1, 'B', 47)],
    'fence_with_language_and_blanks@crlf': [(1, 'A', 0), (1, 'B', 55)],
    'fence_with_language_and_blanks@cr': [(1, 'A', 0), (1, 'B', 47)],
    'inline_code_on_heading_line': [(1, 'A          tail', 0), (1, 'B', 23)],
    'inline_code_on_heading_line@crlf': [(1, 'A          tail', 0), (1, 'B', 25)],
    'inline_code_on_heading_line@cr': [(1, 'A          tail', 0), (1, 'B', 23)],
    'eof_heading_with_trailing_newline': [(1, 'A', 0), (1, 'Notes', 9)],
    'eof_heading_with_trailing_newline@crlf': [(1, 'A', 0), (1, 'Notes', 11)],
    'eof_heading_with_trailing_newline@cr': [(1, 'A', 0), (1, 'Notes', 9)],
    'eof_heading_without_trailing_newline': [(1, 'A', 0), (1, 'Notes', 9)],
    'eof_heading_without_trailing_newline@crlf': [(1, 'A', 0), (1, 'Notes', 11)],
    'eof_heading_without_trailing_newline@cr': [(1, 'A', 0), (1, 'Notes', 9)],
    'eof_body_without_trailing_newline': [(1, 'A', 0), (1, 'Notes', 9)],
    'eof_body_without_trailing_newline@crlf': [(1, 'A', 0), (1, 'Notes', 11)],
    'eof_body_without_trailing_newline@cr': [(1, 'A', 0), (1, 'Notes', 9)],
    'trailing_spaces_and_tabs': [(1, 'A', 0), (1, 'B', 12)],
    'trailing_spaces_and_tabs@crlf': [(1, 'A', 0), (1, 'B', 14)],
    'trailing_spaces_and_tabs@cr': [(1, 'A', 0), (1, 'B', 12)],
    'trailing_nbsp': [(1, 'A', 0), (1, 'B', 11)],
    'trailing_nbsp@crlf': [(1, 'A', 0), (1, 'B', 13)],
    'trailing_nbsp@cr': [(1, 'A', 0), (1, 'B', 11)],
    'headings_only': [(1, 'A', 0), (2, 'B', 4), (3, 'C', 9), (1, 'D', 15)],
    'headings_only@crlf': [(1, 'A', 0), (2, 'B', 5), (3, 'C', 11), (1, 'D', 18)],
    'headings_only@cr': [(1, 'A', 0), (2, 'B', 4), (3, 'C', 9), (1, 'D', 15)],
    'empty_section_between_headings': [(1, 'A', 0), (1, 'B', 4)],
    'empty_section_between_headings@crlf': [(1, 'A', 0), (1, 'B', 5)],
    'empty_section_between_headings@cr': [(1, 'A', 0), (1, 'B', 4)],
    'empty_section_at_eof_no_terminator': [(1, 'A', 0), (1, 'B', 9)],
    'empty_section_at_eof_no_terminator@crlf': [(1, 'A', 0), (1, 'B', 11)],
    'empty_section_at_eof_no_terminator@cr': [(1, 'A', 0), (1, 'B', 9)],
    'nested_deeper_then_shallower': [(1, 'A', 0), (2, 'A1', 11), (3, 'A2', 21), (1, 'B', 35)],
    'nested_deeper_then_shallower@crlf': [(1, 'A', 0), (2, 'A1', 13), (3, 'A2', 25), (1, 'B', 41)],
    'nested_deeper_then_shallower@cr': [(1, 'A', 0), (2, 'A1', 11), (3, 'A2', 21), (1, 'B', 35)],
    'shallower_follows_deeper': [(2, 'Deep', 0), (1, 'Shallow', 10)],
    'shallower_follows_deeper@crlf': [(2, 'Deep', 0), (1, 'Shallow', 12)],
    'shallower_follows_deeper@cr': [(2, 'Deep', 0), (1, 'Shallow', 10)],
    'preamble_before_first_heading': [(1, 'A', 12), (1, 'B', 21)],
    'preamble_before_first_heading@crlf': [(1, 'A', 14), (1, 'B', 25)],
    'preamble_before_first_heading@cr': [(1, 'A', 12), (1, 'B', 21)],
}

# 4.3 — `outline_sections` reports `size = body_end - line_start`. The
# narrowing moves neither endpoint, so ordinals AND sizes are unchanged. Any
# shift here means the fix went further than intended. `(ordinal, depth, text,
# size)`.
EXPECTED_OUTLINE = {
    'blank_line_after_heading': [(1, 1, 'A', 11), (2, 1, 'B', 6)],
    'blank_line_after_heading@crlf': [(1, 1, 'A', 15), (2, 1, 'B', 8)],
    'blank_line_after_heading@cr': [(1, 1, 'A', 11), (2, 1, 'B', 6)],
    'two_blank_lines_after_heading': [(1, 1, 'A', 11), (2, 1, 'B', 6)],
    'two_blank_lines_after_heading@crlf': [(1, 1, 'A', 15), (2, 1, 'B', 8)],
    'two_blank_lines_after_heading@cr': [(1, 1, 'A', 11), (2, 1, 'B', 6)],
    'fence_under_heading': [(1, 1, 'A', 27), (2, 1, 'B', 6)],
    'fence_under_heading@crlf': [(1, 1, 'A', 32), (2, 1, 'B', 8)],
    'fence_under_heading@cr': [(1, 1, 'A', 27), (2, 1, 'B', 6)],
    'tilde_fence_under_heading': [(1, 1, 'A', 26), (2, 1, 'B', 6)],
    'tilde_fence_under_heading@crlf': [(1, 1, 'A', 31), (2, 1, 'B', 8)],
    'tilde_fence_under_heading@cr': [(1, 1, 'A', 26), (2, 1, 'B', 6)],
    'fence_with_language_and_blanks': [(1, 1, 'A', 47), (2, 1, 'B', 6)],
    'fence_with_language_and_blanks@crlf': [(1, 1, 'A', 55), (2, 1, 'B', 8)],
    'fence_with_language_and_blanks@cr': [(1, 1, 'A', 47), (2, 1, 'B', 6)],
    'inline_code_on_heading_line': [(1, 1, 'A          tail', 23), (2, 1, 'B', 6)],
    'inline_code_on_heading_line@crlf': [(1, 1, 'A          tail', 25), (2, 1, 'B', 8)],
    'inline_code_on_heading_line@cr': [(1, 1, 'A          tail', 23), (2, 1, 'B', 6)],
    'eof_heading_with_trailing_newline': [(1, 1, 'A', 9), (2, 1, 'Notes', 8)],
    'eof_heading_with_trailing_newline@crlf': [(1, 1, 'A', 11), (2, 1, 'Notes', 9)],
    'eof_heading_with_trailing_newline@cr': [(1, 1, 'A', 9), (2, 1, 'Notes', 8)],
    'eof_heading_without_trailing_newline': [(1, 1, 'A', 9), (2, 1, 'Notes', 7)],
    'eof_heading_without_trailing_newline@crlf': [(1, 1, 'A', 11), (2, 1, 'Notes', 7)],
    'eof_heading_without_trailing_newline@cr': [(1, 1, 'A', 9), (2, 1, 'Notes', 7)],
    'eof_body_without_trailing_newline': [(1, 1, 'A', 9), (2, 1, 'Notes', 14)],
    'eof_body_without_trailing_newline@crlf': [(1, 1, 'A', 11), (2, 1, 'Notes', 15)],
    'eof_body_without_trailing_newline@cr': [(1, 1, 'A', 9), (2, 1, 'Notes', 14)],
    'trailing_spaces_and_tabs': [(1, 1, 'A', 12), (2, 1, 'B', 8)],
    'trailing_spaces_and_tabs@crlf': [(1, 1, 'A', 14), (2, 1, 'B', 10)],
    'trailing_spaces_and_tabs@cr': [(1, 1, 'A', 12), (2, 1, 'B', 8)],
    'trailing_nbsp': [(1, 1, 'A', 11), (2, 1, 'B', 6)],
    'trailing_nbsp@crlf': [(1, 1, 'A', 13), (2, 1, 'B', 8)],
    'trailing_nbsp@cr': [(1, 1, 'A', 11), (2, 1, 'B', 6)],
    'headings_only': [(1, 1, 'A', 15), (2, 2, 'B', 11), (3, 3, 'C', 6), (4, 1, 'D', 4)],
    'headings_only@crlf': [(1, 1, 'A', 18), (2, 2, 'B', 13), (3, 3, 'C', 7), (4, 1, 'D', 5)],
    'headings_only@cr': [(1, 1, 'A', 15), (2, 2, 'B', 11), (3, 3, 'C', 6), (4, 1, 'D', 4)],
    'empty_section_between_headings': [(1, 1, 'A', 4), (2, 1, 'B', 6)],
    'empty_section_between_headings@crlf': [(1, 1, 'A', 5), (2, 1, 'B', 8)],
    'empty_section_between_headings@cr': [(1, 1, 'A', 4), (2, 1, 'B', 6)],
    'empty_section_at_eof_no_terminator': [(1, 1, 'A', 9), (2, 1, 'B', 3)],
    'empty_section_at_eof_no_terminator@crlf': [(1, 1, 'A', 11), (2, 1, 'B', 3)],
    'empty_section_at_eof_no_terminator@cr': [(1, 1, 'A', 9), (2, 1, 'B', 3)],
    'nested_deeper_then_shallower': [(1, 1, 'A', 35), (2, 2, 'A1', 24), (3, 3, 'A2', 14), (4, 1, 'B', 6)],
    'nested_deeper_then_shallower@crlf': [(1, 1, 'A', 41), (2, 2, 'A1', 28), (3, 3, 'A2', 16), (4, 1, 'B', 8)],
    'nested_deeper_then_shallower@cr': [(1, 1, 'A', 35), (2, 2, 'A1', 24), (3, 3, 'A2', 14), (4, 1, 'B', 6)],
    'shallower_follows_deeper': [(1, 2, 'Deep', 10), (2, 1, 'Shallow', 12)],
    'shallower_follows_deeper@crlf': [(1, 2, 'Deep', 12), (2, 1, 'Shallow', 14)],
    'shallower_follows_deeper@cr': [(1, 2, 'Deep', 10), (2, 1, 'Shallow', 12)],
    'preamble_before_first_heading': [(1, 1, 'A', 9), (2, 1, 'B', 6)],
    'preamble_before_first_heading@crlf': [(1, 1, 'A', 11), (2, 1, 'B', 8)],
    'preamble_before_first_heading@cr': [(1, 1, 'A', 9), (2, 1, 'B', 6)],
}


def test_the_expected_tables_cover_the_whole_corpus():
    """A missing key would make the two tests below silently vacuous."""
    assert set(EXPECTED_HEADINGS) == set(CORPUS)
    assert set(EXPECTED_OUTLINE) == set(CORPUS)


@pytest.mark.parametrize("name", sorted(CORPUS))
def test_no_selector_changes_meaning(name):
    """3.4. Depth, trimmed text, `line_start` and document order are what every
    selector — exact text, path chain, and `#N` ordinal — resolves through."""
    observed = [
        (h["depth"], h["text"], h["line_start"]) for h in _scan_headings(CORPUS[name])
    ]
    assert observed == [tuple(e) for e in EXPECTED_HEADINGS[name]]


@pytest.mark.parametrize("name", sorted(CORPUS))
def test_outline_sections_is_entirely_unchanged(name):
    """4.3. Ordinals and sizes alike — the truncation outline a caller
    navigates by must mean exactly what it meant before."""
    observed = [
        (e["ordinal"], e["depth"], e["text"], e["size"])
        for e in outline_sections(CORPUS[name])
    ]
    assert observed == [tuple(e) for e in EXPECTED_OUTLINE[name]]


# ── 3.3c the residual this change did NOT fix, closed by #150 ───────────────

# The pre-#150 `_FENCE_RE` recognised only a column-zero opener closed by a
# fence of exactly the same length, so these two shapes were NOT masked: a
# heading inside them was visible to the scanner, and a section write there
# deleted the opening fence and orphaned the contents. #140 froze the bytes the
# tree then wrote as evidence that the hazard predated it; the fence-grammar
# change (#150) widened the masker and superseded that residual, so what is
# frozen here now is the CURRENT behaviour beside the historic one.
#
# Each entry: (text, pre-#150 headings, pre-#150 `#1`-write, pre-#150 empty
# `#1`-write, post-#150 headings, post-#150 `#1`-write, post-#150 empty write).

UNMASKED_FENCE_SHAPES = {
    "indented_opener": (
        "# A\n   ```\n# Hidden\nx\n   ```\n# B\nb\n",
        [(1, "A", 0), (1, "Hidden", 11), (1, "B", 29)],
        "# A\nnew\n# Hidden\nx\n   ```\n# B\nb\n",
        "# A\n# Hidden\nx\n   ```\n# B\nb\n",
        [(1, "A", 0), (1, "B", 29)],
        "# A\nnew\n# B\nb\n",
        "# A\n# B\nb\n",
    ),
    "longer_closer": (
        "# A\n```\n# Hidden\nx\n`````\n# B\nb\n",
        [(1, "A", 0), (1, "Hidden", 8), (1, "B", 25)],
        "# A\nnew\n# Hidden\nx\n`````\n# B\nb\n",
        "# A\n# Hidden\nx\n`````\n# B\nb\n",
        [(1, "A", 0), (1, "B", 25)],
        "# A\nnew\n# B\nb\n",
        "# A\n# B\nb\n",
    ),
}


@pytest.mark.parametrize("name", sorted(UNMASKED_FENCE_SHAPES))
def test_a_newly_masked_fence_no_longer_exposes_its_heading(name):
    """#150 closed the residual: `# Hidden` is inside code, so it is not a
    heading, does not occupy an ordinal, and does not bound `A`. `# B` keeps
    its `line_start`, so the ordinal shift is exactly the removal of the
    heading that was never one."""
    text, before_headings, _, _, after_headings, expected_write, _ = (
        UNMASKED_FENCE_SHAPES[name]
    )
    observed = [(h["depth"], h["text"], h["line_start"]) for h in _scan_headings(text)]
    assert observed != before_headings
    assert observed == after_headings
    new_text, err = replace_section(text, "#1", "new")
    assert err is None
    assert new_text == expected_write
    # The destructive half of the #140 contract, now covering these shapes:
    # the whole block is the body of `A` and a write replaces it entire.
    assert "Hidden" not in new_text
    assert "```" not in new_text


@pytest.mark.parametrize("name", sorted(UNMASKED_FENCE_SHAPES))
def test_a_newly_masked_fence_still_inserts_no_separator_when_emptied(name):
    """The separator-conditionality rule #140 pinned is unchanged by the wider
    masker: an empty replacement body inserts no blank line, on these shapes as
    on every other."""
    text, _, _, _, _, _, expected_empty = UNMASKED_FENCE_SHAPES[name]
    new_text, err = replace_section(text, "#1", "")
    assert err is None
    assert new_text == expected_empty


# ── 2.1b/spec scenarios: the empty body ─────────────────────────────────────


def test_an_empty_section_between_headings_gains_nothing():
    """Spec scenario. Unconditional separator insertion meant `# A\\n# B\\nb\\n`
    became `# A\\n\\n# B\\nb\\n` on every round trip."""
    new_text, err = replace_section("# A\n# B\nb\n", "#1", "")
    assert err is None
    assert new_text == "# A\n# B\nb\n"


def test_an_unterminated_eof_heading_gains_nothing():
    """Spec scenario. `# A` used to become `# A\\n`."""
    new_text, err = replace_section("# A", "#1", "")
    assert err is None
    assert new_text == "# A"


@pytest.mark.parametrize("eol", ["\n", "\r\n", "\r"])
def test_an_empty_body_is_stable_in_every_dialect(eol):
    text = "# A{0}# B{0}b{0}".format(eol)
    new_text, err = replace_section(text, "#1", "")
    assert err is None
    assert new_text == text


def test_emptying_a_populated_section_removes_the_whole_body():
    """The other side of the same rule: an empty replacement is a deletion, and
    it leaves no separator behind either."""
    new_text, err = replace_section("# A\nold\n# B\nb\n", "#1", "")
    assert err is None
    assert new_text == "# A\n# B\nb\n"


def test_a_non_empty_body_is_still_separated():
    """Spec scenario, and the #5 behaviour the conditional must not disturb."""
    assert replace_section("# Notes", "Notes", "- item")[0] == "# Notes\n- item"
    assert replace_section("# A\n# B\nb\n", "#1", "x")[0] == "# A\nx\n# B\nb\n"


# ── spec scenarios: what the body now contains ──────────────────────────────


def test_a_blank_line_after_a_heading_belongs_to_the_body():
    """Spec scenario, and the declared compat break in its mildest form."""
    new_text, err = replace_section("# A\n\nold\n# B\nb\n", "#1", "new")
    assert err is None
    assert new_text == "# A\nnew\n# B\nb\n"


def test_a_wanted_blank_separator_is_the_callers_to_send():
    new_text, err = replace_section("# A\n\nold\n# B\nb\n", "#1", "\nnew\n")
    assert err is None
    assert new_text == "# A\n\nnew\n# B\nb\n"


def test_a_fenced_block_under_a_heading_is_replaced_not_retained():
    """Spec scenario. This is the destructive half of the declared break: the
    block is gone unless `content` resends it."""
    text = "# A\n```\n## Hidden\ntext\n```\n# B\nb\n"
    new_text, err = replace_section(text, "#1", "new")
    assert err is None
    assert new_text == "# A\nnew\n# B\nb\n"
    assert "```" not in new_text


def test_the_declared_content_loss_is_exactly_what_the_docs_promise():
    """The example `vault-tools.md` carries.

    Note the exact bytes: `# A\\nnew`, with NO trailing newline. The proposal
    and design for #140 render this example as `# A\\nnew\\n`; that extra
    terminator is an illustration slip, not the contract. `A` is the last
    heading, so no following heading needs separating from the new body, and
    the trailing separator is only ever inserted to prevent that gluing — the
    same rule `tests/test_issue_5_replace_section_eof_heading.py` has pinned
    since #5. A caller who wants the note to end in a newline sends one.
    """
    text = "# A\n```\nimportant\n```\nold\n"
    new_text, err = replace_section(text, "A", "new")
    assert err is None
    assert new_text == "# A\nnew"
    # …and the caller's own terminator is respected, unchanged.
    assert replace_section(text, "A", "new\n")[0] == "# A\nnew\n"


def test_trailing_whitespace_stays_on_the_heading_line():
    """Spec scenario. Horizontal whitespace after the heading text is part of
    the heading line, never the body — and the trimmed text is unchanged, so
    the exact-text and path-style selectors still reach it."""
    for pad in ("   ", "\t\t", " "):
        text = f"# A{pad}\nbody\n# B\nb\n"
        headings = _scan_headings(text)
        assert headings[0]["text"] == "A"
        section, err = extract_section(text, "A")
        assert err is None
        assert section == f"# A{pad}\nbody\n"
        assert body_of(section) == "body\n"
        assert replace_section(text, "A", "new\n")[0] == f"# A{pad}\nnew\n# B\nb\n"


def test_a_deeper_heading_stays_inside_its_parents_body():
    text = "# A\nbody a\n## A1\nsub\n# B\nb\n"
    section, err = extract_section(text, "#1")
    assert err is None
    assert section == "# A\nbody a\n## A1\nsub\n"
    assert replace_section(text, "#1", "new\n")[0] == "# A\nnew\n# B\nb\n"


# ── the tool layer ──────────────────────────────────────────────────────────


@pytest.fixture
def vault(monkeypatch, tmp_path):
    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(vault_service.settings, "vault_path", str(tmp_path))
    monkeypatch.setattr(tools, "_log_usage", noop)
    vault_fs.reset_filesystem_probe_cache()
    token = current_permission.set("readwrite")
    yield tmp_path
    current_permission.reset(token)
    vault_fs.reset_filesystem_probe_cache()


def write(vault, name, text):
    (vault / name).write_text(text, encoding="utf-8", newline="")
    return vault / name


def read(vault, name):
    return (vault / name).read_bytes().decode("utf-8")


# 5.3 — an end-to-end exercise of the two tools an agent actually calls, using
# note bytes the test itself authored rather than anything parsed out of a
# response. Tools exercised: `create_note` (`create_note_impl`), `read_note`
# (`read_note_impl`, with and without `section=`) and `edit_note`
# (`edit_note_impl`, `section=`).

TOOL_ROUND_TRIPS = {
    # (note bytes, selector, the body those bytes put under that heading)
    "body_starts_with_a_blank_line": ("# A\n\nbody\n# B\nb\n", "#1", "\nbody\n"),
    "body_starts_with_a_fence": (
        "# A\n```\n## Hidden\ntext\n```\ntail\n# B\nb\n",
        "#1",
        "```\n## Hidden\ntext\n```\ntail\n",
    ),
    "empty_section": ("# A\n# B\nb\n", "#1", ""),
    "final_section": ("# A\na\n# B\n\nb\n", "#2", "\nb\n"),
    "valid_frontmatter": (
        "---\ntitle: T\n---\n# A\n\nbody\n# B\nb\n",
        "#1",
        "\nbody\n",
    ),
}


@pytest.mark.parametrize("name", sorted(TOOL_ROUND_TRIPS))
def test_a_tool_level_section_round_trip_leaves_the_note_byte_identical(vault, name):
    note, selector, body = TOOL_ROUND_TRIPS[name]
    created = asyncio.run(tools.create_note_impl("n.md", note))
    assert "Created" in created or "created" in created, created
    assert read(vault, "n.md") == note

    # The read must actually carry the section it claims to. Section mode
    # resolves over the frontmatter-stripped body (#128), so that is the text
    # the expected section is taken from.
    _, scanned = vault_service.parse_frontmatter(note)
    section, err = extract_section(scanned, selector)
    assert err is None
    assert section.startswith("# ")
    assert body_of(section) == body, "the test's own idea of the body is wrong"

    # Post-#149 the tool answers in fields, so the agreement is checkable
    # against the RESPONSE and not only against the helpers: `content` **is**
    # the body, so there is no recovery procedure left to get wrong.
    response = asyncio.run(tools.read_note_impl("n.md", section=selector))
    assert response.error is None
    assert response.heading + "\n" + response.content == section or (
        response.content == "" and response.heading == section.rstrip("\n")
    )
    assert response.content == body

    result = asyncio.run(tools.edit_note_impl("n.md", response.content, section=selector))
    assert "Updated note" in result, result
    assert read(vault, "n.md") == note

    # And the whole-note read still shows the note it started as.
    whole = asyncio.run(tools.read_note_impl("n.md"))
    assert whole.content == scanned


def test_the_declared_content_loss_is_reachable_from_the_tool(vault):
    """The break, as a caller meets it: `content` that does not resend the
    fenced block deletes it."""
    write(vault, "n.md", "# A\n```\nimportant\n```\nold\n")
    result = asyncio.run(tools.edit_note_impl("n.md", "new", section="A"))
    assert "Updated note" in result
    # No trailing newline: `A` is the last heading, so nothing needs separating
    # from the new body. See the helper-level test above.
    assert read(vault, "n.md") == "# A\nnew"


# 3.3b — the declared newline residual, pinned as bytes so it cannot drift.


def test_a_crlf_note_comes_back_with_an_lf_body(vault):
    """`read_note` applies universal-newline translation; `edit_note` rewrites
    raw bytes. So the *selected body* is rewritten LF while the heading line
    and everything outside the section keep their own terminators."""
    write(vault, "n.md", "# A\r\nold\r\n# B\r\nkeep\r\n")
    # "old\n" is what the read path hands back for that body.
    asyncio.run(tools.edit_note_impl("n.md", "old\n", section="#1"))
    assert read(vault, "n.md") == "# A\r\nold\n# B\r\nkeep\r\n"


def test_the_normalisation_is_per_terminator_not_per_note(vault):
    """A mixed-ending note is not "a CRLF note": every terminator inside the
    selected body comes back as LF, and only those."""
    write(vault, "n.md", "# A\r\none\r\ntwo\nthree\r# B\rkeep\r")
    asyncio.run(tools.edit_note_impl("n.md", "one\ntwo\nthree\n", section="#1"))
    assert read(vault, "n.md") == "# A\r\none\ntwo\nthree\n# B\rkeep\r"


# 4.2 — the frontmatter rules #128 established, unchanged by this change.


def test_a_valid_block_is_reattached_byte_identically(vault):
    block = "---\ntitle: Keep\ntags:\n  - a\n# a comment\n---\n"
    write(vault, "n.md", block + "## Tasks\n\nold\n## Notes\nkeep\n")
    asyncio.run(tools.edit_note_impl("n.md", "new\n", section="Tasks"))
    assert read(vault, "n.md") == block + "## Tasks\nnew\n## Notes\nkeep\n"


def test_a_defective_block_is_readable_by_section_and_refused_for_writes(vault):
    """The asymmetry is deliberate and this change does not relax it (#128).

    A read destroys nothing, so it proceeds over the raw bytes. A write over
    those same bytes could select a `#` line inside the broken block and
    replace across the closing fence — the corruption #128 removed. So the
    round-trip property above deliberately does NOT extend here: the guarantee
    such a note gets is the refusal.
    """
    original = "---\n# Tasks\n---\n# Body\nkeep\n"
    write(vault, "n.md", original)

    # The read succeeds, over the raw bytes: `# Tasks` is heading #1 there.
    response = asyncio.run(tools.read_note_impl("n.md", section="#1"))
    assert response.error is None
    assert response.heading == "# Tasks"

    # The write refuses, by name, and touches nothing.
    for selector in ("#1", "Tasks", "Parent/Tasks"):
        result = asyncio.run(tools.edit_note_impl("n.md", "x", section=selector))
        assert "malformed frontmatter block" in result
        assert "not a mapping" in result
        assert "replace_frontmatter=True" in result
    assert read(vault, "n.md") == original
