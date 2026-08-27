"""Section addressing and the two automatic-mutation refusals — #150.

The reproduced defect: `edit_note(section="#1")` on a note with an indented
fence replaced only the opening fence line, deleting it and orphaning the
block's contents into the body. The masker now recognises the shape, so the
whole block is `A`'s body and a write replaces it whole.

The one shape the flat grammar genuinely cannot decide — an indented opener
with no closer, which under CommonMark may end at an enclosing list item's end
— is refused by name on both paths that mutate note text automatically:
`edit_note(section=…)` and `move_note(rewrite_links=True)`. Reads keep working
on such a note under the not-a-fence reading, exactly as they do over a
defective frontmatter block.
"""
import asyncio

import pytest

import src.mcp_server.tools as tools
from src.mcp_server.auth import current_permission
from src.services.vault import (
    extract_section,
    outline_sections,
    replace_section,
)


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    monkeypatch.setattr(tools.settings, "vault_path", str(tmp_path))
    from src.services import vault as vault_service

    monkeypatch.setattr(vault_service.settings, "vault_path", str(tmp_path))

    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(tools, "_log_usage", noop)
    token = current_permission.set("readwrite")
    yield tmp_path
    current_permission.reset(token)


def write(vault, name, text):
    path = vault / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")
    return path


def read(vault, name):
    return (vault / name).read_bytes().decode("utf-8")


# ══════════════════════════════════════════════════════════════════════════
# 2.3 — issue #150's reproductions, through the shared section helpers
# ══════════════════════════════════════════════════════════════════════════

INDENTED = "# A\n   ```\n# Hidden\ntext\n   ```\n# B\nb\n"
LONGER_CLOSED = "# A\n```\n# Hidden\n````\n# B\nb\n"


@pytest.mark.parametrize("text", [INDENTED, LONGER_CLOSED])
def test_the_fenced_span_is_the_body_of_a_and_hidden_reaches_nothing(text):
    """`#1` is the whole of section A, block included; `#2` is `# B`; no
    selector reaches `# Hidden`."""
    assert [(s["ordinal"], s["text"]) for s in outline_sections(text)] == [
        (1, "A"),
        (2, "B"),
    ]

    section_a, err = extract_section(text, "#1")
    assert err is None
    assert section_a == text[: text.index("# B")]
    assert "# Hidden" in section_a  # inside the returned span, not addressable

    for selector in ("Hidden", "#3", "A/Hidden"):
        _, err = extract_section(text, selector)
        assert err is not None, f"{selector!r} should not resolve"
        _, err = replace_section(text, selector, "x")
        assert err is not None, f"{selector!r} should not resolve for writes"


@pytest.mark.parametrize("text", [INDENTED, LONGER_CLOSED])
def test_a_write_to_a_replaces_the_whole_block(text):
    """The reproduced destructive write, inverted: the fence is not left
    behind with the new body appended after it."""
    new_text, err = replace_section(text, "#1", "new")
    assert err is None
    assert new_text == "# A\nnew\n# B\nb\n"
    assert "```" not in new_text


def test_no_heading_below_an_unterminated_column_zero_fence_is_selectable():
    text = "# A\nintro\n```\n# Hidden\n# AlsoHidden\n"
    assert [s["text"] for s in outline_sections(text)] == ["A"]
    for selector in ("Hidden", "AlsoHidden", "#2"):
        _, err = extract_section(text, selector)
        assert err is not None


@pytest.mark.parametrize(
    "text",
    [
        INDENTED,
        LONGER_CLOSED,
        "# A\nintro\n```\n# Hidden\n",
        "# A\n~~~x\n# Hidden\n~~~~~\n## B\nb\n### C\nc\n",
        "# A\r\n   ```\r\n# Hidden\r\n   ```\r\n# B\r\nb\r\n",
        "# A\r ```\r# Hidden\r ```\r# B\rb\r",
    ],
)
def test_every_outline_ordinal_round_trips_through_the_resolver(text):
    """The outline and the resolver agree after the grammar change: every `#N`
    the outline emits resolves, for reads and for writes, to the heading the
    outline listed."""
    outline = outline_sections(text)
    for entry in outline:
        selector = f"#{entry['ordinal']}"
        extracted, err = extract_section(text, selector)
        assert err is None, f"{selector} did not resolve: {err}"
        assert extracted.lstrip("#").strip().startswith(entry["text"])
        assert len(extracted) == entry["size"]
        _, err = replace_section(text, selector, "x")
        assert err is None, f"{selector} did not resolve for writes: {err}"


# ══════════════════════════════════════════════════════════════════════════
# 2.2 — the section-write refusal
# ══════════════════════════════════════════════════════════════════════════

LIST_FENCE = "# A\n- item\n  ```\n  code\n\n# B\nkeep\n"


def test_the_list_item_fence_shape_refuses_a_section_write(offline):
    write(offline, "n.md", LIST_FENCE)

    result = asyncio.run(tools.edit_note_impl("n.md", "new", section="A"))

    assert "indented fence opener" in result
    assert "line 3" in result
    assert "```" in result
    assert read(offline, "n.md") == LIST_FENCE  # nothing written


def test_the_same_note_still_reads_by_section(offline):
    """Reads are deliberately asymmetric: they destroy nothing, so the not-a-
    fence reading serves them and `# B` still resolves."""
    assert [s["text"] for s in outline_sections(LIST_FENCE)] == ["A", "B"]
    extracted, err = extract_section(LIST_FENCE, "B")
    assert err is None
    assert extracted == "# B\nkeep\n"


@pytest.mark.parametrize("selector", ["A", "B", "#1", "#2", "NoSuchHeading"])
def test_the_refusal_precedes_every_other_section_outcome(offline, selector):
    """Naming the opener beats reporting a bad selector: the note is refused
    as a whole, not per-selector."""
    write(offline, "n.md", LIST_FENCE)
    result = asyncio.run(tools.edit_note_impl("n.md", "x", section=selector))
    assert "indented fence opener" in result
    assert read(offline, "n.md") == LIST_FENCE


def test_a_dry_run_section_write_is_refused_too(offline):
    write(offline, "n.md", LIST_FENCE)
    result = asyncio.run(
        tools.edit_note_impl("n.md", "x", section="A", dry_run=True)
    )
    assert "indented fence opener" in result


def test_a_matched_indented_fence_writes_normally(offline):
    text = "# A\n- item\n  ```\n  code\n  ```\n# B\nkeep\n"
    write(offline, "n.md", text)

    result = asyncio.run(tools.edit_note_impl("n.md", "new", section="A"))

    assert "indented fence opener" not in result
    assert read(offline, "n.md") == "# A\nnew\n# B\nkeep\n"


def test_the_other_edit_modes_are_unaffected(offline):
    """Only section mode resolves over the masked text, so only section mode
    can be misled by an undecidable fence."""
    write(offline, "n.md", LIST_FENCE)
    result = asyncio.run(tools.edit_note_impl("n.md", "tail\n", append=True))
    assert "indented fence opener" not in result
    assert read(offline, "n.md") == LIST_FENCE + "\ntail\n"


def test_the_opener_position_is_reported_against_the_whole_file(offline):
    """With a valid frontmatter block the write resolves over the stripped
    body, but the position the caller is handed must name the FILE — the only
    coordinate they can act on."""
    block = "---\ntitle: T\n---\n"
    text = block + LIST_FENCE
    write(offline, "n.md", text)

    result = asyncio.run(tools.edit_note_impl("n.md", "x", section="A"))

    assert f"character {text.index('  ```')}" in result
    assert "line 6" in result  # 3 block lines + line 3 of the body
    assert read(offline, "n.md") == text


def test_a_defective_frontmatter_block_still_wins(offline):
    """Precedence, pinned: the frontmatter defect is diagnosed first, so its
    refusal (which names the `replace_frontmatter=True` repair) is what the
    caller gets."""
    text = "---\nkey: [unclosed\n" + LIST_FENCE
    write(offline, "n.md", text)
    result = asyncio.run(tools.edit_note_impl("n.md", "x", section="A"))
    assert "malformed frontmatter block" in result
    assert read(offline, "n.md") == text


def test_a_phantom_second_block_cannot_hide_the_opener(offline):
    """The stripped body's own mapping-shaped prefix is content. If the
    recognizer re-partitioned it, the opener would sit inside a phantom second
    frontmatter block and the refusal would never fire."""
    text = "---\ntitle: T\n---\n---\nkey: value\n  ```\n---\n# Hidden\npayload\n"
    write(offline, "n.md", text)

    result = asyncio.run(tools.edit_note_impl("n.md", "x", section="Hidden"))

    assert "indented fence opener" in result
    assert read(offline, "n.md") == text


# ══════════════════════════════════════════════════════════════════════════
# 2.2b — the rewrite-enabled move preflight
# ══════════════════════════════════════════════════════════════════════════


class _Row:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _fake_session(monkeypatch, result_rows, stale=()):
    """Replay canned rows positionally, except the stale-extraction probe.

    That probe is routed by its SQL rather than by position so a test can say
    "this owner has an unfinished re-derivation" without having to know where
    in the sequence the query lands.
    """
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
            if "extraction_version" in str(statement.compile()):
                return Result(list(stale))
            i = calls["n"]
            calls["n"] += 1
            return Result(result_rows[i] if i < len(result_rows) else [])

        async def commit(self):
            return None

    monkeypatch.setattr(tools, "async_session", lambda: FakeSession())


REFS_WITH_LIST_FENCE = "# Refs\n- item\n  ```\n  see [[Target]]\n\nmore\n"


def _seed_move(offline, monkeypatch, refs_text, stale=()):
    write(offline, "Target.md", "the moved note\n")
    write(offline, "Refs.md", refs_text)
    _fake_session(
        monkeypatch,
        [
            [_Row(file_path="Target.md", id=1), _Row(file_path="Refs.md", id=2)],
            [_Row(file_path="Refs.md")],
        ],
        stale=stale,
    )


async def test_a_rewrite_enabled_move_refuses_on_an_undecidable_source(
    offline, monkeypatch
):
    _seed_move(offline, monkeypatch, REFS_WITH_LIST_FENCE)

    result = await tools.move_note_impl(
        "Target.md", "Moved/Target.md", rewrite_links=True
    )

    assert "Move aborted" in result
    assert "Refs.md" in result
    assert "line 3" in result
    # Refused BEFORE the rename: the note is still where it was, and the
    # source is byte-identical.
    assert (offline / "Target.md").exists()
    assert not (offline / "Moved" / "Target.md").exists()
    assert read(offline, "Refs.md") == REFS_WITH_LIST_FENCE


async def test_the_same_move_without_rewriting_succeeds(offline, monkeypatch):
    _seed_move(offline, monkeypatch, REFS_WITH_LIST_FENCE)

    result = await tools.move_note_impl(
        "Target.md", "Moved/Target.md", rewrite_links=False
    )

    assert "Move aborted" not in result
    assert (offline / "Moved" / "Target.md").exists()
    assert not (offline / "Target.md").exists()
    assert read(offline, "Refs.md") == REFS_WITH_LIST_FENCE


async def test_the_moved_notes_own_body_is_preflighted(offline, monkeypatch):
    """The moved note is a rewrite source too (a self-reference must not end
    up pointing at the old path), so its own undecidable fence refuses."""
    write(offline, "Target.md", "# T\n- item\n  ```\n  [[Target]]\n\ntail\n")
    write(offline, "Refs.md", "see [[Target]]\n")
    _fake_session(
        monkeypatch,
        [
            [_Row(file_path="Target.md", id=1), _Row(file_path="Refs.md", id=2)],
            [_Row(file_path="Refs.md")],
        ],
    )

    result = await tools.move_note_impl(
        "Target.md", "Moved/Target.md", rewrite_links=True
    )

    assert "Move aborted" in result
    assert "Target.md" in result
    assert (offline / "Target.md").exists()
    assert not (offline / "Moved" / "Target.md").exists()


async def test_a_source_with_matched_fences_moves_and_rewrites(offline, monkeypatch):
    _seed_move(
        offline,
        monkeypatch,
        "# Refs\n- item\n  ```\n  code\n  ```\nsee [[Target]]\n",
    )

    result = await tools.move_note_impl(
        "Target.md", "Moved/Renamed.md", rewrite_links=True
    )

    assert "Move aborted" not in result
    assert (offline / "Moved" / "Renamed.md").exists()
    assert "[[Renamed]]" in read(offline, "Refs.md")


async def test_every_offending_source_is_named(offline, monkeypatch):
    write(offline, "Target.md", "the moved note\n")
    write(offline, "One.md", "- x\n  ```\n  [[Target]]\n")
    write(offline, "Two.md", "- y\n   ~~~\n   [[Target]]\n")
    _fake_session(
        monkeypatch,
        [
            [
                _Row(file_path="Target.md", id=1),
                _Row(file_path="One.md", id=2),
                _Row(file_path="Two.md", id=3),
            ],
            [_Row(file_path="One.md"), _Row(file_path="Two.md")],
        ],
    )

    result = await tools.move_note_impl(
        "Target.md", "Moved/Target.md", rewrite_links=True
    )

    assert "One.md" in result and "Two.md" in result
    assert "2 note(s)" in result
    assert (offline / "Target.md").exists()


# ══════════════════════════════════════════════════════════════════════════
# 2.4 — the raw-text consumers, end to end through the tools
# ══════════════════════════════════════════════════════════════════════════
#
# `extract_tags` and `move_note`'s link rewriting both scan RAW note text, so
# they are the two consumers a fence-shaped YAML scalar could silence: with the
# frontmatter block opaque to fence recognition they see the body, and without
# that opacity the indented scalar would open a block swallowing everything
# below it.

FM_SCALAR_NOTE = "---\nliteral: |\n   ```\n---\n#real\nsee [[Target]]\n"


def test_a_fence_shaped_frontmatter_scalar_does_not_hide_the_bodys_tag():
    from src.services.vault import extract_tags, parse_frontmatter

    fm, _body = parse_frontmatter(FM_SCALAR_NOTE)
    assert "real" in extract_tags(FM_SCALAR_NOTE, fm)


async def test_a_fence_shaped_frontmatter_scalar_does_not_block_a_rewrite(
    offline, monkeypatch
):
    _seed_move(offline, monkeypatch, FM_SCALAR_NOTE)

    result = await tools.move_note_impl(
        "Target.md", "Moved/Renamed.md", rewrite_links=True
    )

    assert "Move aborted" not in result
    after = read(offline, "Refs.md")
    assert "[[Renamed]]" in after
    # The block itself is untouched, fence-shaped scalar included.
    assert after.startswith("---\nliteral: |\n   ```\n---\n")


NEWLY_MASKED_SOURCES = {
    "indented": "# A\n   ```\n#buried\nsee [[Target]]\n   ```\ntail\n",
    "longer_closed": "# A\n```\n#buried\nsee [[Target]]\n`````\ntail\n",
    "tilde": "# A\n~~~\n#buried\nsee [[Target]]\n~~~~\ntail\n",
    "unterminated_col_zero": "# A\n```\n#buried\nsee [[Target]]\n",
}


@pytest.mark.parametrize("shape", sorted(NEWLY_MASKED_SOURCES))
def test_a_tag_inside_a_newly_recognised_fence_is_not_extracted(shape):
    from src.services.vault import extract_tags

    assert "buried" not in extract_tags(NEWLY_MASKED_SOURCES[shape], {})


@pytest.mark.parametrize("shape", sorted(NEWLY_MASKED_SOURCES))
async def test_a_link_inside_a_newly_recognised_fence_is_not_rewritten(
    offline, monkeypatch, shape
):
    text = NEWLY_MASKED_SOURCES[shape]
    _seed_move(offline, monkeypatch, text)

    result = await tools.move_note_impl(
        "Target.md", "Moved/Renamed.md", rewrite_links=True
    )

    assert "Move aborted" not in result
    assert read(offline, "Refs.md") == text  # byte-identical: it was code


@pytest.mark.parametrize("shape", sorted(NEWLY_MASKED_SOURCES))
def test_a_link_inside_a_newly_recognised_fence_is_not_extracted(shape):
    from src.services.links import extract_links

    assert extract_links(NEWLY_MASKED_SOURCES[shape]) == []


# ══════════════════════════════════════════════════════════════════════════
# The transition window: a rewrite-enabled move cannot trust stale link rows
# ══════════════════════════════════════════════════════════════════════════
#
# `move_note(rewrite_links=True)` discovers its rewrite sources from
# `note_links`. Under the pre-#150 grammar a note like
# "```code```\n[[Target]]\n```\n" had its link masked as code, so it produced
# NO row; v1 reads the same bytes as prose. Between the deploy and the
# re-derivation pass, that note is a backlink the graph does not know about —
# and a move that rewrites from the graph would report success while leaving
# the link pointing at the old path.


STALE_ERA_INVISIBLE_LINK = "```code```\n[[Target]]\n```\n"


def test_the_stale_era_shape_really_did_hide_its_link_under_v0():
    """The premise, checked rather than asserted: v0 masked this link and v1
    does not. If this ever stops holding, the refusal below is arguing about
    an input that cannot occur."""
    from src.services.embeddings import _v0_clean
    from src.services.links import extract_links

    # v1 reads the whole thing as prose (a backtick info string containing a
    # backtick opens nothing), so the link is extracted.
    assert [link.target for link in extract_links(STALE_ERA_INVISIBLE_LINK)] == [
        "Target"
    ]
    # v0's cleaner removed it, which is the same grammar the v0 masker used to
    # decide the link was code.
    assert "[[Target]]" not in _v0_clean(STALE_ERA_INVISIBLE_LINK)


async def test_a_rewrite_enabled_move_refuses_while_a_stale_marker_remains(
    offline, monkeypatch
):
    _seed_move(
        offline,
        monkeypatch,
        STALE_ERA_INVISIBLE_LINK,
        stale=[_Row(file_path="Refs.md")],
    )

    result = await tools.move_note_impl(
        "Target.md", "Moved/Renamed.md", rewrite_links=True
    )

    assert "Move aborted" in result
    assert "re-derived" in result
    assert "Refs.md" in result  # names the first note still pending
    assert "rewrite_links=False" in result  # and the way through
    # Refused before the rename.
    assert (offline / "Target.md").exists()
    assert not (offline / "Moved" / "Renamed.md").exists()
    assert read(offline, "Refs.md") == STALE_ERA_INVISIBLE_LINK


async def test_the_same_move_without_rewriting_is_unaffected_by_a_stale_marker(
    offline, monkeypatch
):
    _seed_move(
        offline,
        monkeypatch,
        STALE_ERA_INVISIBLE_LINK,
        stale=[_Row(file_path="Refs.md")],
    )

    result = await tools.move_note_impl(
        "Target.md", "Moved/Renamed.md", rewrite_links=False
    )

    assert "Move aborted" not in result
    assert (offline / "Moved" / "Renamed.md").exists()


async def test_the_refusal_clears_once_every_row_is_stamped(offline, monkeypatch):
    """`stale=()` stands for a completed pass: nothing about the note or the
    call changed, only the marker, and the move goes through and rewrites the
    link the old grammar had hidden."""
    _seed_move(offline, monkeypatch, STALE_ERA_INVISIBLE_LINK, stale=())

    result = await tools.move_note_impl(
        "Target.md", "Moved/Renamed.md", rewrite_links=True
    )

    assert "Move aborted" not in result
    assert (offline / "Moved" / "Renamed.md").exists()
    assert "[[Renamed]]" in read(offline, "Refs.md")


async def test_another_owners_stale_marker_does_not_refuse(offline, monkeypatch):
    """Owner-scoped: the probe carries this caller's ownership predicate, so a
    different account's unfinished pass returns no rows here. Without that, one
    idle vault would wedge rewrite-enabled moves for every user."""
    captured: list[str] = []
    _seed_move(offline, monkeypatch, "see [[Target]]\n", stale=())

    real = tools._stale_extraction_error

    async def spy(session, uid):
        from sqlalchemy import select

        from src.models.db import NoteMetadata

        captured.append(
            str(
                select(NoteMetadata.file_path)
                .where(tools._note_owner_predicate(uid))
                .compile()
            )
        )
        return await real(session, uid)

    monkeypatch.setattr(tools, "_stale_extraction_error", spy)

    result = await tools.move_note_impl(
        "Target.md", "Moved/Renamed.md", rewrite_links=True
    )

    assert "Move aborted" not in result
    assert captured and "user_id IS NULL" in captured[0]


# ══════════════════════════════════════════════════════════════════════════
# The read side, at the tool layer
# ══════════════════════════════════════════════════════════════════════════


def test_read_note_still_serves_a_section_of_an_undecidable_note(offline):
    """The helper-layer pin says `extract_section` resolves; this says the
    TOOL does. The refusal is write-only, and a read that started refusing
    would wall off content for no safety gain."""
    write(offline, "n.md", LIST_FENCE)

    response = asyncio.run(tools.read_note_impl("n.md", section="B"))

    assert "indented fence opener" not in response
    assert "keep" in response
    assert "# B" in response


def test_read_note_outlines_an_undecidable_note_under_the_not_a_fence_reading(
    offline,
):
    write(offline, "n.md", LIST_FENCE)

    response = asyncio.run(tools.read_note_impl("n.md", section="A"))

    assert "indented fence opener" not in response
    # `A` runs to `# B` under the not-a-fence reading, so the list and the
    # opener are inside it and `keep` is not.
    assert "- item" in response
    assert "keep" not in response
