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
import src.services.indexer as indexer
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

    Returns the list every executed statement is appended to, so a test can
    assert on the SQL the tool actually issued rather than on SQL it rebuilt
    for itself.
    """
    calls = {"n": 0}
    executed: list = []

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
            executed.append(statement)
            if "extraction_version" in str(statement.compile()):
                return Result(list(stale))
            i = calls["n"]
            calls["n"] += 1
            return Result(result_rows[i] if i < len(result_rows) else [])

        async def commit(self):
            return None

    monkeypatch.setattr(tools, "async_session", lambda: FakeSession())
    return executed


REFS_WITH_LIST_FENCE = "# Refs\n- item\n  ```\n  see [[Target]]\n\nmore\n"


def _seed_move(offline, monkeypatch, refs_text, stale=()):
    """Two notes and a fake session; returns the executed-statement list."""
    write(offline, "Target.md", "the moved note\n")
    write(offline, "Refs.md", refs_text)
    return _fake_session(
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
# A fence-shaped YAML scalar must not silence extraction from the body. Two
# mechanisms carry that, and they are NOT the same mechanism:
#
#   * `move_note`'s link rewriting genuinely scans RAW note text — it splices
#     back into those bytes — so it relies on the block being **opaque** to
#     fence recognition in `FULL_NOTE` context.
#   * `extract_tags` does not scan raw text at all any more: it takes the
#     already-partitioned **body**, so the block is simply not there to be
#     scanned. Frontmatter tags come from the parsed mapping it is handed.

FM_SCALAR_NOTE = "---\nliteral: |\n   ```\n---\n#real\nsee [[Target]]\n"

# The same shape, but with a body line that **closes** the scalar's fence. This
# is the discriminating input, and the reason the unmatched variant above
# cannot stand in for it: on the unmatched note, handing `extract_tags` the raw
# text still yields the tag (the opener matches nothing, so nothing is masked)
# and a callsite that regressed to raw text would go unnoticed. Here the two
# openers pair up, so raw-text scanning masks `#real` and body scanning does
# not — the misuse is observable.
#
#   raw, BODY-scanned:  line 2 `   ```` opens, line 5 `   ```` closes,
#                       `#real` is inside the span → no tag.
#   body, BODY-scanned: the opener is gone with the block; the body's own
#                       `   ```` is an unmatched indented opener, not a
#                       fence → `#real` survives.
FM_SCALAR_MATCHED_BY_BODY = "---\nliteral: |\n   ```\n---\n#real\n   ```\ntail\n"


def test_a_fence_shaped_frontmatter_scalar_does_not_hide_the_bodys_tag():
    from src.services.vault import extract_tags, parse_frontmatter

    fm, body = parse_frontmatter(FM_SCALAR_NOTE)
    assert "real" in extract_tags(body, fm)


def test_the_matched_scalar_shape_distinguishes_body_scanning_from_raw():
    """The premise of the end-to-end test below, checked rather than assumed.

    If this ever stops discriminating, the regression guard underneath it has
    quietly become a tautology.
    """
    from src.services.vault import extract_tags, parse_frontmatter

    fm, body = parse_frontmatter(FM_SCALAR_MATCHED_BY_BODY)
    assert fm == {"literal": "```\n"}  # the block really is valid
    assert "real" in extract_tags(body, fm)
    # The misuse this shape exists to expose.
    assert "real" not in extract_tags(FM_SCALAR_MATCHED_BY_BODY, fm)


def test_read_file_extracts_the_body_tag_through_the_production_path(offline):
    """The real callsite, not the helper: `vault.read_file` is what
    `read_note` serves from, and it is one of the two places that decide what
    `notes_metadata.tags` and every tag-filtered search see.

    A callsite that regressed to passing the raw note would lose `#real` here
    — the scalar's opener would pair with the body's closer and mask it — so
    this pins the *wiring*, not the helper's behaviour.
    """
    from src.services.vault import read_file

    write(offline, "n.md", FM_SCALAR_MATCHED_BY_BODY)

    note = read_file("n.md")

    assert "real" in note["tags"]
    assert note["frontmatter"] == {"literal": "```\n"}
    # And the block was stripped from what the reader is served, as always.
    assert note["content"] == "#real\n   ```\ntail\n"


# The indexer is the other production callsite, and the one whose output is
# persisted into `notes_metadata.tags`. It is pinned end to end against a real
# database in
# `tests/integration/test_issue_150_extraction_version_pg.py::test_the_indexer_extracts_a_body_tag_a_frontmatter_scalar_would_have_masked`.


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


class _RecordingSession:
    """Records every statement and answers each one with no rows.

    Enough for `_stale_extraction_error`, whose whole contract is "which query
    do you issue, and do you refuse when it returns something".
    """

    def __init__(self, rows=()):
        self.statements: list = []
        self._rows = list(rows)

    async def execute(self, statement):
        self.statements.append(statement)
        return type("R", (), {"all": lambda _self: list(self._rows)})()


def _sql_of(statement) -> str:
    """The statement as one line of SQL with its bind values inlined.

    Inlined, because the owner predicate for a named user is a *bound
    parameter* — asserting on `:user_id_1` would pass whatever value the
    predicate carried, including one from another account.
    """
    return " ".join(
        str(statement.compile(compile_kwargs={"literal_binds": True})).split()
    )


OWNER_CLAUSES = {
    None: "notes_metadata.user_id IS NULL",
    7: "notes_metadata.user_id = 7",
}


@pytest.mark.parametrize("uid", sorted(OWNER_CLAUSES, key=lambda u: str(u)))
async def test_the_stale_probe_carries_the_callers_ownership_predicate(uid):
    """The predicate is asserted on the statement `_stale_extraction_error`
    ACTUALLY executes, not on one the test rebuilt for itself.

    An earlier version of this test built its own correctly-scoped query in a
    spy and asserted on that, which is circular: deleting
    `.where(_note_owner_predicate(uid))` from the probe left it green. The
    scoping is what keeps one user's unfinished re-derivation from wedging
    rewrite-enabled moves for every other account, so it needs a test that can
    actually fail.
    """
    session = _RecordingSession()

    assert await tools._stale_extraction_error(session, uid) is None

    assert len(session.statements) == 1
    sql = _sql_of(session.statements[0])
    assert OWNER_CLAUSES[uid] in sql, sql
    # Bound to the constant, not to a literal: the marker moves whenever a
    # grammar changes (version 2 is the link-grammar bump, #203), and a test
    # that pins the number turns every legitimate bump into a spurious
    # failure while proving nothing about the probe.
    assert (
        f"extraction_version != {indexer.CURRENT_EXTRACTION_VERSION}" in sql
    ), sql
    assert "LIMIT 1" in sql, sql
    # And no OTHER owner's clause leaked in.
    for other_uid, clause in OWNER_CLAUSES.items():
        if other_uid != uid:
            assert clause not in sql, sql


@pytest.mark.parametrize("uid", sorted(OWNER_CLAUSES, key=lambda u: str(u)))
async def test_the_stale_probe_refuses_on_a_row_in_its_own_scope(uid):
    """The other half: the query is scoped AND its result is acted on. Without
    this, a probe that returned rows and ignored them would pass the scoping
    assertion above."""
    session = _RecordingSession(rows=[_Row(file_path="Pending.md")])

    err = await tools._stale_extraction_error(session, uid)

    assert err is not None
    assert "re-derived" in err
    assert "Pending.md" in err


async def test_the_move_issues_the_owner_scoped_probe_before_the_rename(
    offline, monkeypatch
):
    """End to end through the tool: the probe the move actually issues is the
    scoped one, and it runs while the note is still at its old path."""
    executed = _seed_move(offline, monkeypatch, "see [[Target]]\n", stale=())

    result = await tools.move_note_impl(
        "Target.md", "Moved/Renamed.md", rewrite_links=True
    )

    assert "Move aborted" not in result
    probes = [
        _sql_of(statement)
        for statement in executed
        if "extraction_version" in _sql_of(statement)
    ]
    assert len(probes) == 1, probes
    assert "notes_metadata.user_id IS NULL" in probes[0]


# ══════════════════════════════════════════════════════════════════════════
# The read side, at the tool layer
# ══════════════════════════════════════════════════════════════════════════


def test_read_note_still_serves_a_section_of_an_undecidable_note(offline):
    """The helper-layer pin says `extract_section` resolves; this says the
    TOOL does. The refusal is write-only, and a read that started refusing
    would wall off content for no safety gain."""
    write(offline, "n.md", LIST_FENCE)

    response = asyncio.run(tools.read_note_impl("n.md", section="B"))

    assert response.error is None
    assert response.heading == "# B"
    assert "keep" in response.content


def test_read_note_outlines_an_undecidable_note_under_the_not_a_fence_reading(
    offline,
):
    write(offline, "n.md", LIST_FENCE)

    response = asyncio.run(tools.read_note_impl("n.md", section="A"))

    assert response.error is None
    # `A` runs to `# B` under the not-a-fence reading, so the list and the
    # opener are inside it and `keep` is not.
    assert "- item" in response.content
    assert "keep" not in response.content
