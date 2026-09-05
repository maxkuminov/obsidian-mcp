"""`move_note`'s rewrite splices bytes from the note, not from the mask (#211).

`_rewrite_links_in_text` runs both recognizers over the fence/inline-code
**masked** copy of the source — links inside code must not be rewritten, and
that is the whole point of the mask. It then splices a replacement over the
match's span in the **unmasked** content. Before this fix it built that
replacement out of pieces it had read off the masked string: the wikilink
`rest` (`#anchor|alias`) and the markdown link's text and anchor. Inline code
inside any of those is a run of spaces in the mask, so::

    IN : See [the `foo` option](Old.md) and [[Old]] here.
    OUT: See [the       option](New.md) and [[New]] here.

A silent destructive write on every `move_note(rewrite_links=True)` whose
backlink sources hold inline code inside a link — the expensive failure class
for this product, because an agent never sees the bytes it just lost.

The fix is one property: masking is a **same-length** substitution, so a span
discovered in `masked` indexes the identical region of `content`, and every
byte written back is sliced from `content` at that span. What the recognizers
*see* is unchanged, so which links get rewritten is unchanged too — the
fenced-code and inline-code exclusions below are the same assertions they were
before, kept here so a future "just splice from the mask, it is simpler"
cannot pass.
"""
import pytest

import src.mcp_server.tools as tools
from src.auth.session import current_user_id
from src.mcp_server.auth import current_permission
from src.services import vault as vault_service
from src.services.links import build_vault_index


def _index():
    return build_vault_index([("Old.md", 1), ("New.md", 2)])


def _rewrite(content: str, source_path: str = "src.md"):
    return tools._rewrite_links_in_text(
        content, "Old.md", "New.md", source_path, _index()
    )


def _assert_only_these_links_changed(content, rewritten, count, edits):
    """Byte identity everywhere outside the rewritten link spans.

    `edits` is `[(old_link, new_link), …]`, each unique in `content`. Applying
    exactly those substitutions to the input must reproduce the output
    character for character — frontmatter, fences, inline code, whitespace,
    non-ASCII and all — which is a stronger statement than "the new link is in
    there somewhere".
    """
    expected = content
    for old, new in edits:
        assert expected.count(old) == 1, old
        expected = expected.replace(old, new)
    assert rewritten == expected
    assert count == len(edits)


# ── the bug: inline code inside a rewritten link ────────────────────────────


def test_inline_code_in_markdown_link_text_survives_the_rewrite():
    content = "See [the `foo` option](Old.md) for the flag.\n"

    rewritten, count = _rewrite(content)

    assert rewritten == "See [the `foo` option](New.md) for the flag.\n"
    _assert_only_these_links_changed(
        content, rewritten, count,
        [("[the `foo` option](Old.md)", "[the `foo` option](New.md)")],
    )


def test_inline_code_in_a_wikilink_alias_survives_the_rewrite():
    content = "See [[Old|the `foo` option]] for the flag.\n"

    rewritten, count = _rewrite(content)

    assert rewritten == "See [[New|the `foo` option]] for the flag.\n"
    _assert_only_these_links_changed(
        content, rewritten, count,
        [("[[Old|the `foo` option]]", "[[New|the `foo` option]]")],
    )


def test_inline_code_in_a_wikilink_anchor_survives_the_rewrite():
    content = "See [[Old#the `foo` section]] and ![[Old#`bar`|`baz` alias]].\n"

    rewritten, count = _rewrite(content)

    _assert_only_these_links_changed(
        content, rewritten, count,
        [
            ("[[Old#the `foo` section]]", "[[New#the `foo` section]]"),
            ("![[Old#`bar`|`baz` alias]]", "![[New#`bar`|`baz` alias]]"),
        ],
    )


def test_inline_code_in_a_markdown_anchor_survives_the_rewrite():
    """The anchor was spliced from the mask too, for the same reason."""
    content = "See [t](Old.md#the `foo` section) for the flag.\n"

    rewritten, count = _rewrite(content)

    _assert_only_these_links_changed(
        content, rewritten, count,
        [
            (
                "[t](Old.md#the `foo` section)",
                "[t](New.md#the `foo` section)",
            )
        ],
    )


def test_a_note_full_of_code_keeps_every_byte_outside_its_links():
    """One document carrying every shape at once, asserted byte-for-byte.

    Frontmatter, a fenced block, an inline span outside a link, non-ASCII, a
    CRLF line and four rewritable links with code inside them. The masker is a
    same-length substitution over all of it, so nothing outside the four link
    spans may move by a single character.
    """
    content = (
        "---\ntitle: Notes — `raw`\n---\n\n"
        "Prose with `inline code` and an ünicöde — dash.\r\n"
        "A markdown link: [the `foo` option](Old.md#sec `x`).\n"
        "A wikilink: [[Old|`bar` baz]].\n"
        "An embed: ![[Old#`h`]].\n"
        "```python\n"
        "x = [not_a_link](Old.md)  # inside a fence\n"
        "```\n"
        "A plain one: [[Old]].\n"
    )

    rewritten, count = _rewrite(content)

    _assert_only_these_links_changed(
        content, rewritten, count,
        [
            (
                "[the `foo` option](Old.md#sec `x`)",
                "[the `foo` option](New.md#sec `x`)",
            ),
            ("[[Old|`bar` baz]]", "[[New|`bar` baz]]"),
            ("![[Old#`h`]]", "![[New#`h`]]"),
            ("[[Old]]", "[[New]]"),
        ],
    )
    # And the fenced link is still there, still naming the old path.
    assert "x = [not_a_link](Old.md)  # inside a fence" in rewritten


# ── the exclusions the mask exists for, unchanged ───────────────────────────


def test_a_link_entirely_inside_inline_code_is_still_not_rewritten():
    """The pre-existing behaviour, and the reason the recognizers keep running
    over `masked`: a link written as an example inside backticks is text about
    a link, not a link."""
    content = "Write it as `[x](Old.md)` or `[[Old]]`, not otherwise.\n"

    rewritten, count = _rewrite(content)

    assert count == 0
    assert rewritten == content


def test_a_link_inside_a_fenced_block_is_still_not_rewritten():
    content = (
        "```\n[x](Old.md)\n[[Old]]\n```\n"
        "~~~md\n![[Old]]\n~~~\n"
        "and [[Old]] outside\n"
    )

    rewritten, count = _rewrite(content)

    _assert_only_these_links_changed(
        content, rewritten, count, [("[[Old]] outside", "[[New]] outside")]
    )


def test_a_lone_backtick_masks_nothing_and_is_kept():
    """`_INLINE_CODE_RE` pairs backticks; a single one on the line is not a
    code span, so the link is masked nowhere, is rewritten, and keeps its
    backtick — the slice-from-content path with nothing to restore."""
    content = "See [a ` b](Old.md) here.\n"

    rewritten, count = _rewrite(content)

    _assert_only_these_links_changed(
        content, rewritten, count, [("[a ` b](Old.md)", "[a ` b](New.md)")]
    )


def test_two_backticks_around_a_link_still_hide_it_from_the_rewrite():
    """The other side of the same rule, and PRE-EXISTING: two lone backticks on
    one line pair with each other, so everything between them — including a
    whole link — is a code span and is not rewritten. Unchanged by #211: the
    fix moved where the written-back bytes come from, not what the recognizers
    see."""
    content = "See [a ` b](Old.md) and [[Old|c ` d]] here.\n"

    rewritten, count = _rewrite(content)

    assert count == 0
    assert rewritten == content


def test_backticks_on_two_different_lines_do_not_pair():
    """`_INLINE_CODE_RE` never crosses a terminator, so these two backticks are
    two lone backticks and the link between them is a link."""
    content = "one ` line\n[[Old]] between\ntwo ` line\n"

    rewritten, count = _rewrite(content)

    _assert_only_these_links_changed(
        content, rewritten, count, [("[[Old]]", "[[New]]")]
    )


# ── end to end, through `move_note` ─────────────────────────────────────────
#
# The unit assertions above pin the function; this pins the tool, because the
# corruption reached disk. Same fixture shape as
# `tests/test_asvs_link_grammar.py`'s dispatch test.


class _Row:
    def __init__(self, **fields):
        self.__dict__.update(fields)


def _fake_session(*result_rows):
    calls = {"n": 0}

    class Result:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return self._rows

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def execute(self, statement):
            i = calls["n"]
            calls["n"] += 1
            return Result(result_rows[i] if i < len(result_rows) else [])

        async def commit(self):
            return None

    return FakeSession


@pytest.fixture
def rewrite_vault(monkeypatch, tmp_path):
    monkeypatch.setattr(vault_service.settings, "vault_path", str(tmp_path))

    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(tools, "_log_usage", noop)
    perm = current_permission.set("readwrite")
    uid = current_user_id.set(None)
    yield tmp_path
    current_user_id.reset(uid)
    current_permission.reset(perm)


async def test_move_note_does_not_blank_inline_code_in_a_backlink_source(
    rewrite_vault, monkeypatch
):
    source = (
        "See [the `foo` option](Old.md) and [[Old|`bar` baz]].\n"
        "`[x](Old.md)` is only an example.\n"
    )
    (rewrite_vault / "Old.md").write_text("moved\n", encoding="utf-8")
    (rewrite_vault / "src.md").write_text(source, encoding="utf-8")
    monkeypatch.setattr(
        tools,
        "async_session",
        _fake_session(
            [_Row(file_path="Old.md", id=1), _Row(file_path="src.md", id=2)],
            [_Row(file_path="src.md")],
        ),
    )

    result = await tools.move_note_impl("Old.md", "New.md", rewrite_links=True)

    assert "Moved Old.md → New.md" in result, result
    assert (rewrite_vault / "src.md").read_text(encoding="utf-8") == (
        "See [the `foo` option](New.md) and [[New|`bar` baz]].\n"
        "`[x](Old.md)` is only an example.\n"
    )


# ── the property the fix rests on ───────────────────────────────────────────


def test_the_scanner_reports_where_it_took_text_and_anchor_from():
    """`MdLinkMatch.text_start` / `anchor_start` are what let the rewriter
    re-slice from the unmasked note, so they have to name the exact region the
    scanner's own `text` and `anchor` came from — in every branch, extraction's
    and the rewriter's alike, including the angle form the rewriter never
    reaches and the empty anchor that still carries a position."""
    from src.services.links import scan_md_links

    corpus = (
        "[plain](a.md) [anchored](b.md#sec) [angle](<c.md>) "
        "[angle anchored](<d.md#sec>) [with `code`](e.md#an `chor`)\n"
        "[crossing](f.md#sec\nmore)\n"
    )
    for flags in ({}, tools.MDLINK_REWRITE_FLAGS):
        links = list(scan_md_links(corpus, **flags))
        assert links, flags
        for link in links:
            assert (
                corpus[link.text_start:link.text_start + len(link.text)]
                == link.text
            ), (flags, link)
            assert (
                corpus[link.anchor_start:link.anchor_start + len(link.anchor)]
                == link.anchor
            ), (flags, link)
            # The reported spans sit inside the match they belong to.
            assert link.start < link.text_start
            assert link.anchor_start + len(link.anchor) <= link.end




def test_the_mask_is_the_same_length_as_what_it_replaces():
    """Stated here as well as in `src/services/links.py` because the rewrite's
    correctness now depends on it directly: if masking ever stopped preserving
    offsets, every slice taken from `content` at a masked match's span would
    silently read the wrong bytes."""
    from src.services.links import FULL_NOTE, apply_fence_mask, scan_fences

    content = (
        "---\nt: `y`\n---\n"
        "a `b` c\n```\nfenced `code`\n```\n~~~\nmore\n~~~\nünicöde `x`\r\n"
    )
    masked = apply_fence_mask(content, scan_fences(content, context=FULL_NOTE))

    assert len(masked) == len(content)
    # It really did mask, so the length assertion is not vacuous — and every
    # masked character is a space, never a shorter or longer stand-in.
    assert "`" not in masked
    assert all(
        m == c or m == " " for m, c in zip(masked, content, strict=True)
    )
    # An inline span, unlike a fenced one, leaves its line terminators alone —
    # only a fence masks across lines.
    inline_only = "a `b` c\nd `e` f\r\n"
    inline_masked = apply_fence_mask(
        inline_only, scan_fences(inline_only, context=FULL_NOTE)
    )
    assert inline_masked == "a     c\nd     f\r\n"
