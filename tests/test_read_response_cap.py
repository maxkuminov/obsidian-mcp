"""Tests for the context-safe cap on read_note / read_file responses.

Regression cover for the 2026-08-05 SkateGPT outage: `obsidian_read_note` on a
2.9 MB auto-generated "Key Documents" note returned a 3.5 MB tool result, and
OpenRouter rejected the follow-up request with "Your input exceeds the context
window of this model". Nothing in the server bounded what a single note read
could put into the caller's context.

Since #149 `read_note` returns a structured `ReadNoteResult` rather than a
rendered string, so every assertion here reads a **field**. The cap itself is
unchanged: it governs the `content` field, the outline keeps its own equal
budget, and the metadata fields share a third. `size()` below measures what the
caller actually receives — the serialized JSON the MCP text block carries.

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


def size(result) -> int:
    """Characters the caller pays for: the serialized response."""
    return len(result.model_dump_json())


def outline_size(outline) -> int:
    return len(outline.model_dump_json())


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
    assert len(out.content) == cap
    assert out.truncated is True
    assert out.total_chars == 50_000
    assert size(out) < 2_000


@pytest.mark.asyncio
async def test_small_note_is_returned_verbatim_with_no_notice(vault, cap):
    _write(vault, "small.md", "just a little note")
    out = await tools.read_note_impl("small.md")
    assert out.content == "just a little note"
    assert out.truncated is False
    assert out.next_offset is None
    assert out.notice is None


@pytest.mark.asyncio
async def test_truncation_notice_offers_a_usable_next_offset(vault, cap):
    _write(vault, "huge.md", "abcdefghij" * 500)  # 5,000 chars
    first = await tools.read_note_impl("huge.md")
    assert first.next_offset == 500

    second = await tools.read_note_impl("huge.md", offset=500)
    assert (second.offset, second.total_chars) == (500, 5_000)
    assert "showing characters 500–1,000 of 5,000" in second.notice
    # The windows must be adjacent, not overlapping or gapped.
    assert second.next_offset == 1_000
    assert first.content + second.content == "abcdefghij" * 100


@pytest.mark.asyncio
async def test_final_window_has_no_continue_hint(vault, cap):
    _write(vault, "huge.md", "y" * 900)
    out = await tools.read_note_impl("huge.md", offset=500)
    assert "showing characters 500–900 of 900" in out.notice
    assert out.next_offset is None
    assert "Continue with" not in out.notice


@pytest.mark.asyncio
async def test_offset_past_end_is_reported_not_silently_empty(vault, cap):
    _write(vault, "huge.md", "z" * 900)
    out = await tools.read_note_impl("huge.md", offset=5_000)
    assert "past the end" in out.error
    assert out.content is None


@pytest.mark.asyncio
async def test_negative_offset_and_zero_limit_are_rejected(vault, cap):
    _write(vault, "n.md", "body")
    assert "offset must be >= 0" in (await tools.read_note_impl("n.md", offset=-1)).error
    assert "limit must be >= 1" in (await tools.read_note_impl("n.md", limit=0)).error


@pytest.mark.asyncio
async def test_limit_can_lower_the_cap_but_never_raise_it(vault, cap):
    _write(vault, "huge.md", "q" * 50_000)
    lowered = await tools.read_note_impl("huge.md", limit=100)
    assert len(lowered.content) == 100
    assert lowered.next_offset == 100

    # A caller asking for more than the server cap still gets the server cap.
    raised = await tools.read_note_impl("huge.md", limit=40_000)
    assert len(raised.content) == cap


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
    listed = {e.text: e for e in out.outline.entries}
    assert "Balance Sheet.xlsx" in listed
    assert "Payroll Register.pdf" in listed
    assert 'section="<heading>"' in out.notice
    # The oversized section is flagged so the model knows paging is still needed.
    assert listed["Balance Sheet.xlsx"].exceeds_cap is True
    assert listed["Payroll Register.pdf"].exceeds_cap is False


@pytest.mark.asyncio
async def test_reading_one_section_avoids_the_rest_of_the_note(vault, cap):
    padded = KEY_DOCS.replace("BALANCE_BODY", "b" * 30_000)
    _write(vault, "keydocs.md", padded)
    out = await tools.read_note_impl("keydocs.md", section="Payroll Register.pdf")
    assert out.heading == "## Payroll Register.pdf"
    assert out.content == "\nPAYROLL_BODY\n\n"
    assert "b" * 100 not in out.content
    assert out.truncated is False


@pytest.mark.asyncio
async def test_section_read_is_itself_capped_and_pages_within_the_section(vault, cap):
    padded = KEY_DOCS.replace("BALANCE_BODY", "b" * 2_000)
    _write(vault, "keydocs.md", padded)
    out = await tools.read_note_impl("keydocs.md", section="Balance Sheet.xlsx")
    assert out.truncated is True
    assert "section 'Balance Sheet.xlsx'" in out.notice
    assert 'section="Balance Sheet.xlsx", offset=500)' in out.notice
    # A section window must not re-offer the whole-note outline.
    assert out.outline is None
    assert "Payroll Register.pdf" not in out.notice


@pytest.mark.asyncio
async def test_unknown_section_lists_the_headings_present(vault, cap):
    _write(vault, "keydocs.md", KEY_DOCS)
    out = await tools.read_note_impl("keydocs.md", section="No Such Heading")
    assert "not found" in out.error
    assert "Balance Sheet.xlsx" in out.error
    assert out.content is None and out.heading is None


@pytest.mark.asyncio
async def test_headingless_oversized_note_still_truncates(vault, cap):
    _write(vault, "flat.md", "no headings here. " * 200)
    out = await tools.read_note_impl("flat.md")
    assert out.truncated is True
    assert out.outline is None
    # The notice offers a narrowing tool, and offers it by the name the tool is
    # actually registered under (#89) — `search_notes` was never registered.
    assert "keyword_search" in out.notice
    assert "search_notes" not in out.notice


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


def test_bare_ordinal_always_selects_by_position():
    """A bare `#N` must mean the ordinal even when a heading is titled '#N'.

    The outline advertises ordinals as the reliable selector, so note content
    must not be able to shadow one — otherwise the section we told the caller
    to request by `#2` is unreachable by `#2`.
    """
    text = "# Top\n\n## Alpha\n\naaa\n\n## #2\n\nliteral\n"
    section, err = extract_section(text, "#2")
    assert err is None
    assert "aaa" in section          # ordinal 2 == '## Alpha'
    assert "literal" not in section


def test_a_heading_titled_like_an_ordinal_stays_reachable():
    """Shadowing the literal title is only acceptable if it stays addressable."""
    text = "# Top\n\n## Alpha\n\naaa\n\n## #2\n\nliteral\n"

    # ...by the path-style form, which never takes the ordinal branch
    section, err = extract_section(text, "Top/#2")
    assert err is None
    assert "literal" in section

    # ...and by its own ordinal (it is the 3rd heading)
    section, err = extract_section(text, "#3")
    assert err is None
    assert "literal" in section


@pytest.mark.asyncio
async def test_outline_flags_duplicates_and_prints_ordinals(vault, cap):
    padded = DUPES.replace("FIRST", "f" * 2_000)
    _write(vault, "dupes.md", padded)
    out = await tools.read_note_impl("dupes.md")
    by_ordinal = {e.ordinal: e for e in out.outline.entries}
    assert by_ordinal[2].duplicate is True
    assert by_ordinal[4].duplicate is True
    assert by_ordinal[3].duplicate is False


# --- the outline must not itself blow the cap -------------------------------
#
# Caught in pre-merge review: the outline is appended to a response that exists
# *because* the content was too large. Unbounded, a note with many headings
# produced an outline 213x the cap — reintroducing the exact failure this
# module prevents.


def test_outline_never_exceeds_the_cap():
    """The invariant is `<= cap`, exactly — not `cap` plus slack.

    An earlier version of this test allowed `cap + 400`, which let a real
    budget leak pass: the omitted-sections summary was appended without being
    paid for. Assert the specified invariant, not a padded one.
    """
    big = "# Top\n\n" + "".join(f"## {'S' * 78}{i}\n\nbody\n\n" for i in range(1000))
    out = tools.build_outline(big, 500)
    assert outline_size(out) <= 500, f"outline was {outline_size(out)} chars for a 500 cap"
    assert out.truncated is True
    assert out.omitted == 1001 - len(out.entries)
    assert (out.first_ordinal, out.last_ordinal) == (1, 1001)


@pytest.mark.parametrize("n_sections", [1, 2, 50, 1_000, 10_000])
@pytest.mark.parametrize("cap", [1, 2, 40, 120, 500, 5_000])
def test_outline_honors_every_cap_for_every_shape(cap, n_sections):
    """Sweep the extremes: tiny caps, huge caps, one section, ten thousand."""
    note = "# Top\n\n" + "".join(f"## S{i}\n\nbody\n\n" for i in range(n_sections))
    out = tools.build_outline(note, cap)
    # None is the only cap-respecting answer when not even the bare truncation
    # marker fits; anything else is an outline that exceeded its budget.
    assert out is None or outline_size(out) <= cap, (
        f"{outline_size(out)} chars for cap={cap}, {n_sections} sections"
    )


def test_outline_honors_the_cap_with_duplicate_titles():
    """Duplicate markers add per-line text; they must be paid for too."""
    note = "# Top\n\n" + "".join("## Same\n\nbody\n\n" for _ in range(1_000))
    for cap in (1, 50, 200, 500, 5_000):
        out = tools.build_outline(note, cap)
        assert out is None or outline_size(out) <= cap, f"cap={cap}"


def test_outline_honors_the_cap_with_multibyte_titles():
    note = "# Top\n\n" + "".join(f"## Раздел документа {i} 文書 {'я' * 40}\n\nb\n\n"
                                 for i in range(500))
    for cap in (1, 80, 500, 5_000):
        out = tools.build_outline(note, cap)
        assert out is None or outline_size(out) <= cap, f"cap={cap}"


def test_outline_elides_overlong_titles():
    text = "# Top\n\n## " + "T" * 300 + "\n\nbody\n"
    out = tools.build_outline(text, 5_000)
    elided = out.entries[1].text
    assert elided.endswith("…")
    assert len(elided) == 80
    assert "T" * 300 not in out.model_dump_json()


def test_outline_degrades_to_its_truncation_marker_when_no_entry_fits():
    """The degraded state is DATA, not a marker character inside a field.

    Pre-#149 the outline was a string and a too-small budget hard-truncated it
    mid-sentence. An outline that is a field can say "I was truncated" without
    writing anything into the note-controlled text.
    """
    text = "# Top\n\n## " + "T" * 300 + "\n\nbody\n\n## Second\n\nmore\n"
    out = tools.build_outline(text, 100)
    assert out.entries == []
    assert out.truncated is True
    assert out.omitted == 3
    assert (out.first_ordinal, out.last_ordinal) == (1, 3)
    assert outline_size(out) <= 100


def test_the_cap_wins_over_the_marker_when_even_that_does_not_fit():
    """"There is no output the outline may exceed the cap to produce."""
    text = "# Top\n\n## " + "T" * 300 + "\n\nbody\n\n## Second\n\nmore\n"
    assert tools.build_outline(text, 20) is None


# The length sweep above is satisfied by an implementation that returns one
# arbitrary character, so it cannot be the only constraint. These pin the
# outline's usefulness: when the listing fits, it must actually be the listing.


def test_a_listing_that_fits_is_emitted_whole_with_no_summary():
    """Regression: the summary reservation used to be charged unconditionally.

    `_outline_text("# A\\nbody\\n", 160)` returned a 157-char "1 more section
    not shown" summary instead of the 22-char entry that fit comfortably.
    """
    out = tools.build_outline("# A\nbody\n", 160)
    assert len(out.entries) == 1
    entry = out.entries[0]
    assert (entry.ordinal, entry.depth, entry.text, entry.size) == (1, 1, "A", 9)
    assert (entry.exceeds_cap, entry.duplicate) == (False, False)
    assert out.truncated is False
    assert out.omitted is None


def test_every_section_is_listed_when_the_budget_allows():
    note = "# Top\n\n" + "".join(f"## Section {i}\n\nbody\n\n" for i in range(20))
    out = tools.build_outline(note, 5_000)
    assert out.truncated is False
    assert out.omitted is None
    assert len(out.entries) == 21               # 1 H1 + 20 H2
    assert [e.text for e in out.entries[1:]] == [f"Section {i}" for i in range(20)]
    assert [e.ordinal for e in out.entries] == list(range(1, 22))


def test_truncated_outline_still_lists_as_many_entries_as_fit():
    """Not just one entry and a summary — the budget should be spent on entries."""
    note = "# Top\n\n" + "".join(f"## Section {i}\n\nbody\n\n" for i in range(500))
    out = tools.build_outline(note, 2_000)
    assert len(out.entries) >= 10, (
        f"only {len(out.entries)} entries used out of a 2,000 char budget"
    )
    assert out.truncated is True
    assert out.omitted == 501 - len(out.entries)
    assert outline_size(out) <= 2_000


@pytest.mark.asyncio
async def test_truncated_response_stays_within_its_documented_worst_case(vault, cap):
    """End-to-end bound, stated as the design does (#149): three budgets.

    Three components of `cap` can appear in one response — the content window,
    the outline, and the metadata fields together — plus the notice and the
    path, which are fixed-size. Anything beyond that means a component escaped
    its budget. (The doc's worst case additionally multiplies by 2 for the
    structured/text duplication and by 6 for JSON escaping; this fixture's
    content is plain ASCII, so it measures the un-escaped single copy.)
    """
    NOTICE_ALLOWANCE = 1_200
    big = "# Top\n\n" + "".join(f"## {'S' * 78}{i}\n\nbody\n\n" for i in range(1000))
    _write(vault, "many.md", big)
    out = await tools.read_note_impl("many.md")
    limit = 3 * cap + NOTICE_ALLOWANCE
    assert size(out) <= limit, f"response was {size(out)} chars, worst case is {limit}"


# --- offset exactly at the end ----------------------------------------------


@pytest.mark.asyncio
async def test_offset_exactly_at_end_is_distinguished_from_past_it(vault, cap):
    _write(vault, "n.md", "z" * 900)
    at_end = await tools.read_note_impl("n.md", offset=900)
    past = await tools.read_note_impl("n.md", offset=901)
    assert "exactly the end" in at_end.error and "nothing further" in at_end.error
    assert "past the end" in past.error
    assert "exactly the end" not in past.error


@pytest.mark.asyncio
async def test_read_file_offset_exactly_at_end(vault, cap):
    _write(vault, "f.txt", "abcde")
    out = await tools.read_file_impl("f.txt", offset=5)
    assert "exactly the end" in out
    assert "past the end" not in out


@pytest.mark.asyncio
async def test_read_note_accepts_an_ordinal_section(vault, cap):
    _write(vault, "dupes.md", DUPES.replace("FIRST", "f" * 2_000))
    out = await tools.read_note_impl("dupes.md", section="#4")
    assert out.heading == "## Report.xlsx"
    assert "SECOND" in out.content
    assert "f" * 100 not in out.content
