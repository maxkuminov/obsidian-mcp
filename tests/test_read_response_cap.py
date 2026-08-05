"""Tests for the context-safe cap on read_note / read_file responses.

Regression cover for the 2026-08-05 SkateGPT outage: `obsidian_read_note` on a
2.9 MB auto-generated "Key Documents" note returned a 3.5 MB tool result, and
OpenRouter rejected the follow-up request with "Your input exceeds the context
window of this model". Nothing in the server bounded what a single note read
could put into the caller's context.

Follows the setup convention of `test_file_access_tools.py`: minimal env
defaults and a chdir away from any `.env` BEFORE importing the tools module.
"""

import os
import tempfile

os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
os.chdir(tempfile.gettempdir())

import pytest  # noqa: E402

import src.mcp_server.tools as tools  # noqa: E402
from src.services.vault import extract_section, outline_sections  # noqa: E402


@pytest.fixture(autouse=True)
def _no_usage_log(monkeypatch):
    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(tools, "_log_usage", _noop)


@pytest.fixture
def vault(monkeypatch, tmp_path):
    monkeypatch.setattr(tools.settings, "vault_path", str(tmp_path))
    return tmp_path


@pytest.fixture
def cap(monkeypatch):
    """Shrink the response cap so fixtures stay small and readable."""
    monkeypatch.setattr(tools.settings, "max_read_response_chars", 500)
    return 500


def _write(vault, name, body):
    p = vault / name
    p.write_text(body, encoding="utf-8")
    return name


# --- the actual regression -------------------------------------------------


@pytest.mark.asyncio
async def test_oversized_note_is_truncated_not_returned_whole(vault, cap):
    """The bug: a huge note came back in full and blew the context window."""
    _write(vault, "huge.md", "x" * 50_000)
    out = await tools.read_note_impl("huge.md")
    assert len(out) < 2_000
    assert "[TRUNCATED]" in out
    assert "of 50,000" in out


@pytest.mark.asyncio
async def test_small_note_is_returned_verbatim_with_no_notice(vault, cap):
    _write(vault, "small.md", "just a little note")
    out = await tools.read_note_impl("small.md")
    assert "just a little note" in out
    assert "TRUNCATED" not in out


@pytest.mark.asyncio
async def test_truncation_notice_offers_a_usable_next_offset(vault, cap):
    _write(vault, "huge.md", "abcdefghij" * 500)  # 5,000 chars
    first = await tools.read_note_impl("huge.md")
    assert "offset=500)" in first

    second = await tools.read_note_impl("huge.md", offset=500)
    assert "Showing chars 500–1,000 of 5,000" in second
    # The windows must be adjacent, not overlapping or gapped.
    assert "offset=1000)" in second


@pytest.mark.asyncio
async def test_final_window_has_no_continue_hint(vault, cap):
    _write(vault, "huge.md", "y" * 900)
    out = await tools.read_note_impl("huge.md", offset=500)
    assert "Showing chars 500–900 of 900" in out
    assert "Continue with" not in out


@pytest.mark.asyncio
async def test_offset_past_end_is_reported_not_silently_empty(vault, cap):
    _write(vault, "huge.md", "z" * 900)
    out = await tools.read_note_impl("huge.md", offset=5_000)
    assert "past the end" in out


@pytest.mark.asyncio
async def test_negative_offset_and_zero_limit_are_rejected(vault, cap):
    _write(vault, "n.md", "body")
    assert "offset must be >= 0" in await tools.read_note_impl("n.md", offset=-1)
    assert "limit must be >= 1" in await tools.read_note_impl("n.md", limit=0)


@pytest.mark.asyncio
async def test_limit_can_lower_the_cap_but_never_raise_it(vault, cap):
    _write(vault, "huge.md", "q" * 50_000)
    lowered = await tools.read_note_impl("huge.md", limit=100)
    assert "Showing chars 0–100" in lowered

    # A caller asking for more than the server cap still gets the server cap.
    raised = await tools.read_note_impl("huge.md", limit=40_000)
    assert f"Showing chars 0–{cap}" in raised


# --- section navigation ----------------------------------------------------


KEY_DOCS = """# Client — Key Docs

Full-text extracts.

## Balance Sheet.xlsx

BALANCE_BODY

## Payroll Register.pdf

PAYROLL_BODY

## Notes

trailing
"""


@pytest.mark.asyncio
async def test_oversized_note_lists_its_sections_for_navigation(vault, cap):
    padded = KEY_DOCS.replace("BALANCE_BODY", "b" * 2_000)
    _write(vault, "keydocs.md", padded)
    out = await tools.read_note_impl("keydocs.md")
    assert "## Balance Sheet.xlsx" in out
    assert "## Payroll Register.pdf" in out
    assert 'section="<heading>"' in out
    # The oversized section is flagged so the model knows paging is still needed.
    assert "over the cap, will page" in out


@pytest.mark.asyncio
async def test_reading_one_section_avoids_the_rest_of_the_note(vault, cap):
    padded = KEY_DOCS.replace("BALANCE_BODY", "b" * 30_000)
    _write(vault, "keydocs.md", padded)
    out = await tools.read_note_impl("keydocs.md", section="Payroll Register.pdf")
    assert "PAYROLL_BODY" in out
    assert "b" * 100 not in out
    assert "TRUNCATED" not in out


@pytest.mark.asyncio
async def test_section_read_is_itself_capped_and_pages_within_the_section(vault, cap):
    padded = KEY_DOCS.replace("BALANCE_BODY", "b" * 2_000)
    _write(vault, "keydocs.md", padded)
    out = await tools.read_note_impl("keydocs.md", section="Balance Sheet.xlsx")
    assert "[TRUNCATED]" in out
    assert "section 'Balance Sheet.xlsx'" in out
    assert 'section="Balance Sheet.xlsx", offset=500)' in out
    # A section window must not re-offer the whole-note outline.
    assert "Payroll Register.pdf" not in out


@pytest.mark.asyncio
async def test_unknown_section_lists_the_headings_present(vault, cap):
    _write(vault, "keydocs.md", KEY_DOCS)
    out = await tools.read_note_impl("keydocs.md", section="No Such Heading")
    assert "not found" in out
    assert "Balance Sheet.xlsx" in out


@pytest.mark.asyncio
async def test_headingless_oversized_note_still_truncates(vault, cap):
    _write(vault, "flat.md", "no headings here. " * 200)
    out = await tools.read_note_impl("flat.md")
    assert "[TRUNCATED]" in out
    assert "search_notes" in out


# --- read_file -------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_file_text_is_capped_and_pages(vault, cap):
    _write(vault, "big.csv", "a,b,c\n" * 2_000)
    out = await tools.read_file_impl("big.csv")
    assert "[TRUNCATED]" in out
    assert 'offset=500)' in out

    nxt = await tools.read_file_impl("big.csv", offset=500)
    assert "Showing chars 500–1,000" in nxt


@pytest.mark.asyncio
async def test_read_file_forced_text_encoding_is_capped_too(vault, cap):
    _write(vault, "big.txt", "t" * 5_000)
    out = await tools.read_file_impl("big.txt", encoding="text")
    assert "[TRUNCATED]" in out


@pytest.mark.asyncio
async def test_read_file_small_text_is_unchanged(vault, cap):
    _write(vault, "tiny.txt", "hello")
    assert await tools.read_file_impl("tiny.txt") == "hello"


# --- vault-layer helpers ---------------------------------------------------


def test_extract_section_returns_heading_and_body_only():
    section, err = extract_section(KEY_DOCS, "Balance Sheet.xlsx")
    assert err is None
    assert section.startswith("## Balance Sheet.xlsx")
    assert "BALANCE_BODY" in section
    assert "PAYROLL_BODY" not in section


def test_extract_section_stops_at_same_depth_not_at_deeper():
    text = "# Top\n\nintro\n\n## Child\n\nchild body\n\n# Next\n\nafter\n"
    section, err = extract_section(text, "Top")
    assert err is None
    assert "child body" in section  # deeper heading is part of the section
    assert "after" not in section   # same-depth heading ends it


def test_extract_section_disambiguates_by_path():
    text = "# A\n\n## Detail\n\nfrom A\n\n# B\n\n## Detail\n\nfrom B\n"
    _, err = extract_section(text, "Detail")
    assert err is not None and "matches 2 headings" in err

    section, err = extract_section(text, "B/Detail")
    assert err is None
    assert "from B" in section and "from A" not in section


def test_outline_sizes_match_extracted_sections():
    for entry in outline_sections(KEY_DOCS):
        section, err = extract_section(KEY_DOCS, f"#{entry['ordinal']}")
        assert err is None
        assert entry["size"] == len(section)


# --- duplicate sibling headings -------------------------------------------
#
# The 2.9 MB note that caused the outage lists the same source filename twice
# under one parent. Path-style selectors cannot separate duplicate siblings
# (they share every ancestor), so without an ordinal those sections were
# unreachable and the model had to fall back to paging 2.9 MB.

DUPES = "# Key Docs\n\n## Report.xlsx\n\nFIRST\n\n## Other.pdf\n\nmid\n\n## Report.xlsx\n\nSECOND\n"


def test_duplicate_siblings_are_addressable_by_ordinal():
    first, err = extract_section(DUPES, "#2")
    assert err is None and "FIRST" in first and "SECOND" not in first

    second, err = extract_section(DUPES, "#4")
    assert err is None and "SECOND" in second and "FIRST" not in second


def test_ambiguous_title_error_names_the_ordinals_to_use():
    _, err = extract_section(DUPES, "Report.xlsx")
    assert err is not None
    assert "#2" in err and "#4" in err


def test_ordinal_out_of_range_is_reported():
    _, err = extract_section(DUPES, "#99")
    assert err is not None and "out of range" in err
    assert "#1–#4" in err


def test_ordinal_selector_does_not_shadow_a_literal_heading():
    """A heading literally named '#2' must still win over the ordinal form."""
    text = "# Top\n\n## Alpha\n\naaa\n\n## #2\n\nliteral\n"
    section, err = extract_section(text, "#2")
    assert err is None
    assert "literal" in section
    assert "aaa" not in section


@pytest.mark.asyncio
async def test_outline_flags_duplicates_and_prints_ordinals(vault, cap):
    padded = DUPES.replace("FIRST", "f" * 2_000)
    _write(vault, "dupes.md", padded)
    out = await tools.read_note_impl("dupes.md")
    assert "`#2`" in out and "`#4`" in out
    assert "duplicate title, use the ordinal" in out


@pytest.mark.asyncio
async def test_read_note_accepts_an_ordinal_section(vault, cap):
    _write(vault, "dupes.md", DUPES.replace("FIRST", "f" * 2_000))
    out = await tools.read_note_impl("dupes.md", section="#4")
    assert "SECOND" in out
    assert "f" * 100 not in out
