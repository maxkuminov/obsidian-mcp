"""Verifies clean_for_embedding strips fenced code blocks before embedding."""
from src.services.embeddings import clean_for_embedding


def test_strips_backtick_fence():
    text = "Hello\n\n```json\n{\"a\": 1}\n```\n\nMore prose."
    out = clean_for_embedding(text)
    assert "{" not in out
    assert "Hello" in out
    assert "More prose." in out


def test_strips_tilde_fence():
    text = "Hello\n\n~~~python\nprint('hi')\n~~~\n\nMore."
    out = clean_for_embedding(text)
    assert "print" not in out
    assert "Hello" in out
    assert "More." in out


def test_preserves_inline_code():
    text = "Use `pgvector` with `<=>` for cosine."
    out = clean_for_embedding(text)
    assert out == text


def test_strips_multiple_fences():
    text = (
        "Intro\n\n```\nfirst block\n```\n\n"
        "Middle\n\n```js\nsecond block\n```\n\nEnd."
    )
    out = clean_for_embedding(text)
    assert "first block" not in out
    assert "second block" not in out
    assert "Intro" in out
    assert "Middle" in out
    assert "End." in out


def test_excalidraw_like_payload():
    """Realistic Excalidraw-shaped content: small prose + giant fenced JSON."""
    payload = "x" * 5000
    text = (
        "# Excalidraw Data\n\n## Text Elements\nLabel A\n\n"
        f"## Drawing\n```compressed-json\n{payload}\n```\n"
    )
    out = clean_for_embedding(text)
    assert payload not in out
    assert "Label A" in out
    assert len(out) < 200


def test_no_fences_unchanged():
    text = "Just prose, no code blocks at all.\n\nMultiple paragraphs."
    assert clean_for_embedding(text) == text


def test_empty_string():
    assert clean_for_embedding("") == ""


def test_only_a_fence():
    text = "```\nthe whole file is a code block\n```"
    out = clean_for_embedding(text)
    assert "code block" not in out


# ── #150: the shared grammar, not a private one ─────────────────────────────
#
# This module carried its own LF-only, column-zero, exact-closer regexes until
# the fence-grammar change. They disagreed with the masker heading resolution
# uses, so an indented or longer-closed block was embedded as prose while the
# same block was invisible to `read_note(section=…)`.


def test_strips_an_indented_fence():
    text = "Hello\n\n   ```json\n{\"a\": 1}\n   ```\n\nMore prose."
    out = clean_for_embedding(text)
    assert "{" not in out
    assert "Hello" in out and "More prose." in out


def test_strips_a_longer_closed_fence():
    text = "Hello\n\n```\npayload here\n`````\n\nMore prose."
    out = clean_for_embedding(text)
    assert "payload here" not in out
    assert "Hello" in out and "More prose." in out


def test_strips_a_fence_in_every_dialect():
    for eol in ("\n", "\r\n", "\r"):
        text = "Hello{0}~~~{0}payload{0}~~~{0}More.".format(eol)
        out = clean_for_embedding(text)
        assert "payload" not in out, f"not stripped with {eol!r}"
        assert "Hello" in out and "More." in out


def test_an_unterminated_column_zero_fence_takes_the_rest_of_the_body():
    text = "Prose\n```\neverything below is code\n"
    out = clean_for_embedding(text)
    assert "everything below" not in out
    assert "Prose" in out


def test_an_unterminated_indented_opener_is_not_stripped():
    """Not a fence — the flat grammar cannot decide its extent, so the text
    below it stays in the embedding rather than silently vanishing from search."""
    text = "Prose\n- item\n  ```\nstill prose\n"
    assert clean_for_embedding(text) == text


def test_a_backtick_info_string_with_a_backtick_strips_nothing():
    text = "Prose\n```code```\nmore prose\n"
    assert clean_for_embedding(text) == text


def test_the_shorter_impostor_closer_does_not_end_the_block():
    text = "Intro\n````\nfirst\n```\nsecond\n````\nEnd."
    out = clean_for_embedding(text)
    assert "first" not in out and "second" not in out
    assert "Intro" in out and "End." in out


# ── the frozen v0 cleaner, and why the comparison is over OUTPUT ────────────
#
# `notes_metadata.extraction_version` decides whether a note's vectors survive
# a grammar change by comparing what the stamped version would have embedded
# against what the current one embeds. An earlier draft compared recognised
# *span tuples* instead, which is wrong in BOTH directions because v0's cleaner
# applied its two regexes sequentially: the backtick pass ran first and changed
# the text the tilde pass matched against, and the two patterns' `$`-anchored
# spans could overlap. These two inputs are the counterexamples, and they are
# what makes the output comparison load-bearing rather than stylistic.

IDENTICAL_SPANS_DIFFERENT_TEXT = "~~~\ncode\n~~~\n```\n# H\ncode\n```\n[[X]]\n"
DIFFERENT_SPANS_IDENTICAL_TEXT = "```\n~~~\ncode\n~~~\n```"


def _v1_spans(body):
    from src.services.links import BODY, scan_fences

    return scan_fences(body, context=BODY).spans


def _v0_spans(body):
    """What the retired span-based comparator computed for version 0.

    The two v0 regexes now live in `tests/test_asvs_v0_cleaner.py`, which
    keeps them as the oracle the linear `_v0_clean` scanner is proved against
    (#180); this module imports them from there rather than from the service,
    so there is exactly one copy in the tree.
    """
    from tests.test_asvs_v0_cleaner import (
        V0_FENCE_BACKTICK_RE,
        V0_FENCE_TILDE_RE,
    )

    return tuple(sorted(
        (m.start(), m.end())
        for rx in (V0_FENCE_BACKTICK_RE, V0_FENCE_TILDE_RE)
        for m in rx.finditer(body)
    ))


def test_equal_spans_can_still_mean_different_embedded_text():
    """Span comparison would have certified a stale vector here."""
    from src.services.embeddings import clean_at_version

    body = IDENTICAL_SPANS_DIFFERENT_TEXT
    assert _v0_spans(body) == _v1_spans(body)
    assert clean_at_version(0, body) != clean_at_version(1, body)


def test_different_spans_can_still_mean_identical_embedded_text():
    """And here it would have re-embedded for nothing."""
    from src.services.embeddings import clean_at_version

    body = DIFFERENT_SPANS_IDENTICAL_TEXT
    assert _v0_spans(body) != _v1_spans(body)
    assert clean_at_version(0, body) == clean_at_version(1, body)


def test_the_indexer_predicate_follows_the_output_not_the_spans():
    from src.services.indexer import _grammar_changed_the_embedding_text

    assert _grammar_changed_the_embedding_text(0, IDENTICAL_SPANS_DIFFERENT_TEXT)
    assert not _grammar_changed_the_embedding_text(0, DIFFERENT_SPANS_IDENTICAL_TEXT)


def test_the_current_version_never_compares_against_itself():
    from src.services.indexer import (
        CURRENT_EXTRACTION_VERSION,
        _grammar_changed_the_embedding_text,
    )

    assert not _grammar_changed_the_embedding_text(
        CURRENT_EXTRACTION_VERSION, IDENTICAL_SPANS_DIFFERENT_TEXT
    )


def test_an_unknown_stamped_version_counts_as_differing():
    """A build downgraded past a bump cannot reproduce the grammar that wrote
    the row, so it must re-embed rather than certify a comparison it never
    made."""
    from src.services.embeddings import clean_at_version
    from src.services.indexer import _grammar_changed_the_embedding_text

    assert clean_at_version(99, "anything") is None
    assert _grammar_changed_the_embedding_text(99, "no fences at all\n")


def test_the_v0_cleaner_is_the_pre_150_behaviour_verbatim():
    """The frozen entry is a copy, not a reimplementation: it must still be
    LF-only, column-zero and exact-closer, which is precisely what makes it
    disagree with the current grammar on the shapes #150 widened."""
    from src.services.embeddings import clean_at_version

    for widened in (
        "   ```\ncode\n   ```\n",       # indented
        "```\ncode\n`````\n",           # longer closer
        "~~~\rcode\r~~~\r",             # lone CR
    ):
        assert clean_at_version(0, widened) == widened, widened
        assert clean_at_version(1, widened) != widened, widened
