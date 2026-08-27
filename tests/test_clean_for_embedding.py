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
