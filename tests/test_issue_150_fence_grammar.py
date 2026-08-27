"""The shared fence grammar (`code-masking`), scenario by scenario — #150.

Every scenario in `openspec/specs/code-masking/spec.md` is a case here, and the
masked spans are pinned by exact offsets rather than by "the heading went away":
a grammar this many consumers read from cannot be pinned by its symptoms. The
same-length invariant is asserted on every case, because it is what makes the
positions the masker's consumers report valid against the ORIGINAL text.
"""
import pytest

from src.services.links import (
    BODY,
    FULL_NOTE,
    extract_links,
    mask_code,
    scan_fences,
    unmatched_indented_openers,
)
from src.services.vault import extract_tags


def spans(text, context=BODY):
    return scan_fences(text, context=context).spans


def masked(text, context=BODY):
    out = mask_code(text, context=context)
    # The invariant, asserted on every single case that goes through here.
    assert len(out) == len(text), "masking changed the text's code-point length"
    return out


def unmasked_regions(text, context=BODY):
    """The text with every masked span blanked — what a consumer still sees."""
    return masked(text, context=context)


# ── opener and closer shapes ────────────────────────────────────────────────


@pytest.mark.parametrize("indent", ["", " ", "  ", "   "])
def test_an_opener_indented_up_to_three_spaces_is_a_fence(indent):
    text = f"# A\n{indent}```\n# Hidden\ntext\n{indent}```\n# B\n"
    (start, end), = spans(text)
    assert text[start:start + len(indent) + 3] == indent + "```"
    # Through the closing line's last character, terminator excluded.
    assert text[end] == "\n"
    assert text[end - 3:end] == "```"
    assert "# Hidden" not in masked(text)
    assert "# B" in masked(text)


@pytest.mark.parametrize("indent", ["", " ", "  ", "   "])
def test_a_closer_indented_up_to_three_spaces_closes(indent):
    text = f"```\ncode\n{indent}```\n# B\n"
    (start, end), = spans(text)
    assert start == 0
    assert text[end] == "\n"
    assert "code" not in masked(text)
    assert "# B" in masked(text)


def test_four_space_indentation_is_never_a_fence():
    """Indented code blocks are a documented divergence: not masked, and an
    opener at four spaces cannot open one either."""
    text = "# A\n    ```\n# Hidden\ntext\n    ```\n# B\n"
    assert spans(text) == ()
    # Every heading survives: nothing was masked as a block. (The inline-code
    # masker still blanks the backtick pair inside each run — its single-line
    # approximation is unchanged by this grammar and is not a block.)
    from src.services.vault import outline_sections

    assert [s["text"] for s in outline_sections(text)] == ["A", "Hidden", "B"]
    # And a four-space CLOSER does not close a real fence.
    text2 = "```\ncode\n    ```\nstill code\n"
    (start, end), = spans(text2)
    assert (start, end) == (0, len(text2))  # unterminated, column zero → EOF


def test_a_longer_closer_closes_the_block():
    text = "# A\n```\n# Hidden\n````\n# B\nb\n"
    (start, end), = spans(text)
    assert text[start:start + 3] == "```"
    assert text[end - 4:end] == "````"
    assert "# Hidden" not in masked(text)
    assert masked(text).endswith("\n# B\nb\n")


def test_a_shorter_run_does_not_close():
    text = "````\n```\nstill code\n````\ndone\n"
    (start, end), = spans(text)
    assert text[end - 4:end] == "````"
    assert "still code" not in masked(text)
    assert "done" in masked(text)


def test_the_other_fence_character_does_not_close():
    for opener, impostor in (("```", "~~~"), ("~~~", "```")):
        text = f"{opener}\ncode\n{impostor}\nmore code\n"
        # Nothing closes it, so the column-zero rule runs it to end of note.
        assert spans(text) == ((0, len(text)),)
        assert masked(text).strip() == ""


def test_tilde_fences_are_recognised():
    text = "# A\n~~~python\n# Hidden\nprint(1)\n~~~~\n# B\n"
    (start, end), = spans(text)
    assert text[start:start + 3] == "~~~"
    assert text[end - 4:end] == "~~~~"
    assert "# Hidden" not in masked(text)
    assert "# B" in masked(text)


def test_a_backtick_info_string_containing_a_backtick_is_not_an_opener():
    text = "# A\n```code```\n# B\nb\n"
    assert spans(text) == ()
    # The inline-code masker still blanks the run, but no block opened, so
    # `# B` survives as a heading.
    assert "# B" in masked(text)


def test_a_tilde_info_string_may_contain_anything():
    text = "~~~ruby ~~~ whatever\ncode\n~~~\n# B\n"
    (start, end), = spans(text)
    assert start == 0
    assert "code" not in masked(text)
    assert "# B" in masked(text)


@pytest.mark.parametrize("suffix", ["", " ", "\t", " \t  "])
def test_only_space_and_tab_may_follow_a_closing_run(suffix):
    text = f"```\ncode\n```{suffix}\n# B\n"
    (start, end), = spans(text)
    assert text[end - len(suffix) - 3:end - len(suffix)] == "```"
    assert "# B" in masked(text)


@pytest.mark.parametrize("suffix", [" ", "x", " "])
def test_a_non_space_tab_suffix_does_not_close(suffix):
    text = f"```\ncode\n```{suffix}\n# B\n"
    # Never closed → the column-zero rule masks to end of note.
    assert spans(text) == ((0, len(text)),)
    assert "# B" not in masked(text)


# ── unterminated openers ────────────────────────────────────────────────────


def test_an_unterminated_column_zero_fence_masks_to_end_of_note():
    text = "# A\n```\n# Hidden\n[[Link]]\n#tag\n"
    assert spans(text) == ((4, len(text)),)
    out = masked(text)
    assert out.startswith("# A\n")
    assert out[4:] == " " * (len(text) - 4)
    assert extract_links(text) == []
    assert unmatched_indented_openers(text, context=BODY) == ()


def test_an_unterminated_indented_opener_is_not_a_fence_but_is_reported():
    text = "# A\n- item\n  ```\n  code\n\n# B\nkeep\n"
    assert spans(text) == ()  # no block was opened at all
    (opener,) = unmatched_indented_openers(text, context=BODY)
    assert opener.position == text.index("  ```")
    assert opener.line == 3
    assert opener.text == "  ```"
    assert "line 3" in opener.describe()
    # `# B` is still a heading, which is the whole point of not fabricating an
    # end-of-note extent.
    from src.services.vault import outline_sections

    assert [s["text"] for s in outline_sections(text)] == ["A", "B"]


def test_several_unmatched_indented_openers_are_all_reported():
    text = " ```\nx\n  ~~~\ny\n"
    openers = unmatched_indented_openers(text, context=BODY)
    assert [(o.line, o.text) for o in openers] == [(1, " ```"), (3, "  ~~~")]
    assert spans(text) == ()


def test_a_matched_indented_opener_is_not_reported():
    text = "- item\n  ```\n  code\n  ```\n# B\n"
    assert unmatched_indented_openers(text, context=BODY) == ()
    assert len(spans(text)) == 1


# ── line terminators ────────────────────────────────────────────────────────


@pytest.mark.parametrize("eol", ["\n", "\r\n", "\r"])
def test_a_heading_immediately_after_the_closer_survives_in_every_dialect(eol):
    text = "# A{0}```{0}# Hidden{0}```{0}# B{0}b{0}".format(eol)
    (start, end), = spans(text)
    # The closing line's terminator is NOT masked — that surviving line
    # boundary is what keeps `# B` matchable at a line start.
    assert text[end:end + len(eol)] == eol
    out = masked(text)
    assert out[end:end + len(eol)] == eol
    assert "# Hidden" not in out

    from src.services.vault import outline_sections

    assert [s["text"] for s in outline_sections(text)] == ["A", "B"]


@pytest.mark.parametrize("eol", ["\n", "\r\n", "\r"])
def test_fences_are_recognised_in_every_dialect(eol):
    text = "# A{0}   ~~~{0}# Hidden{0}   ~~~~{0}# B{0}".format(eol)
    assert len(spans(text)) == 1
    assert "# Hidden" not in masked(text)


def test_a_lone_cr_note_masks_and_keeps_its_length():
    text = "```\r# Hidden\r```\r# Real\rold\r"
    out = masked(text)
    assert "# Hidden" not in out
    assert "# Real" in out


# ── spanning a heading boundary ─────────────────────────────────────────────


def test_a_fence_spanning_a_heading_boundary_masks_across_it():
    text = "# A\n```\n# Hidden\nx\n# AlsoHidden\n```\n# B\nb\n"
    (start, end), = spans(text)
    assert start == 4
    out = masked(text)
    assert "# Hidden" not in out and "# AlsoHidden" not in out

    from src.services.vault import outline_sections

    assert [s["text"] for s in outline_sections(text)] == ["A", "B"]


# ── frontmatter opacity, and the at-most-once partition ─────────────────────


FM_SCALAR_NOTE = "---\nliteral: |\n   ```\n---\n#real\n[[Old]]\n"


def test_a_fence_shaped_yaml_scalar_does_not_swallow_the_body():
    """Spec scenario. In FULL_NOTE context the valid line-1 block is opaque, so
    the indented fence-shaped scalar opens nothing and the body is extracted."""
    assert spans(FM_SCALAR_NOTE, context=FULL_NOTE) == ()
    assert unmatched_indented_openers(FM_SCALAR_NOTE, context=FULL_NOTE) == ()
    assert "real" in extract_tags(FM_SCALAR_NOTE, {})
    body = FM_SCALAR_NOTE.split("---\n", 2)[2]
    assert [link.target for link in extract_links(body)] == ["Old"]


def test_scanning_the_same_note_as_a_body_would_have_swallowed_it():
    """Why the context parameter exists at all: the identical bytes read as an
    unmatched indented opener when nothing tells the recognizer they carry a
    frontmatter block."""
    assert spans(FM_SCALAR_NOTE, context=BODY) == ()
    (opener,) = unmatched_indented_openers(FM_SCALAR_NOTE, context=BODY)
    assert opener.text == "   ```"


def test_a_defective_frontmatter_note_is_scanned_raw():
    """No valid line-1 block → the whole raw text is scanned, so a column-zero
    fence inside the would-be block masks like any other."""
    text = "---\nkey: [unclosed\n```\ncode\n```\n# B\n"
    fm_spans = spans(text, context=FULL_NOTE)
    assert fm_spans == spans(text, context=BODY)
    assert len(fm_spans) == 1
    assert "code" not in masked(text, context=FULL_NOTE)


STRIPPED_BODY_WITH_PHANTOM_BLOCK = (
    "---\nkey: value\n  ```\n---\n# Hidden\npayload\n"
)


def test_a_stripped_body_is_never_re_partitioned():
    """Spec scenario. A body whose own prefix is mapping-shaped is CONTENT: the
    unmatched indented opener inside it must be reported, not hidden inside a
    phantom second frontmatter block where the refusal would never see it."""
    assert spans(STRIPPED_BODY_WITH_PHANTOM_BLOCK, context=BODY) == ()
    (opener,) = unmatched_indented_openers(
        STRIPPED_BODY_WITH_PHANTOM_BLOCK, context=BODY
    )
    assert opener.text == "  ```"
    assert opener.line == 3
    # FULL_NOTE on the same bytes is the mistake this parameter prevents.
    assert unmatched_indented_openers(
        STRIPPED_BODY_WITH_PHANTOM_BLOCK, context=FULL_NOTE
    ) == ()


def test_the_context_argument_is_required_and_validated():
    with pytest.raises(TypeError):
        mask_code("x")  # type: ignore[call-arg]
    with pytest.raises(ValueError):
        scan_fences("x", context="whatever")  # type: ignore[arg-type]


# ── inline code ─────────────────────────────────────────────────────────────


def test_inline_code_cannot_span_lines():
    """Documented divergence, unchanged by this change."""
    text = "a ` b\n# Real\nc ` d\n"
    out = masked(text)
    assert "# Real" in out


def test_inline_code_is_still_masked():
    text = "Use `[[NotALink]]` here\n"
    assert extract_links(text) == []
    assert "NotALink" not in masked(text)


# ── the length invariant, including non-ASCII ───────────────────────────────


NON_ASCII_CASES = [
    "# Ünïcödé\n   ```\n# Hïddén\n日本語のテキスト\n   ```\n# Bé\n",
    "```\n🎉🎉🎉\n````\n# Aprés\n",
    "~~~\né\r\n~~~\r\n# Bür\r\n",
    "  ```\nunmatched indented\n# Still A Heading\n",
    "---\nliteral: |\n   ```\n---\n#étiquette\n[[Nöte]]\n",
]


@pytest.mark.parametrize("text", NON_ASCII_CASES)
@pytest.mark.parametrize("context", [BODY, FULL_NOTE])
def test_masking_preserves_code_point_length_on_non_ascii(text, context):
    out = mask_code(text, context=context)
    assert len(out) == len(text)
    # Positions are code points, not bytes: every span index must index the
    # ORIGINAL text at the same character.
    for start, end in scan_fences(text, context=context).spans:
        assert out[start:end] == " " * (end - start)
        assert 0 <= start < end <= len(text)


def test_a_span_never_overlaps_its_neighbour():
    text = "```\na\n```\ntext\n~~~\nb\n~~~\n"
    got = spans(text)
    assert len(got) == 2
    assert got[0][1] <= got[1][0]


def test_many_unmatched_indented_openers_do_not_blow_up():
    """The memoised-failure guard: without it this is quadratic."""
    # Info strings keep every line an OPENER and never a closer, so nothing
    # here can pair off — the shape the memoisation exists for.
    text = "".join(f" ```x\nline {i}\n" for i in range(4000))
    assert spans(text) == ()
    assert len(unmatched_indented_openers(text, context=BODY)) == 4000
