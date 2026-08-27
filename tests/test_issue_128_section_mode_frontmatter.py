"""Section mode cannot touch a frontmatter block (#128, D5/task 3.2).

`read_note` resolves headings over the frontmatter-stripped body; `edit_note`
resolved them over the **raw** file. The base spec already promises that "a
selector that names a section for reading names the same section for writing",
and on any note whose frontmatter carries a `#`-prefixed line that promise was
simply false: the write side counted the YAML comment as heading #1, so every
ordinal was off by one and `section="Tasks"` could select a line inside the
block. A replacement then ran from inside the block to the next body heading —
deleting the closing fence and everything between.

Two rules restore it. Over a **valid** block, resolution, replacement and the
not-found/ambiguity listings all run on the stripped body and the block is
reattached byte-identically. Over a **defective** block the write is refused by
name: resolving over raw bytes there is exactly the corruption above, and there
is no stripped body to resolve over instead.
"""

import asyncio
import os
import tempfile

os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
os.chdir(tempfile.gettempdir())

import pytest  # noqa: E402

import src.mcp_server.tools as tools  # noqa: E402
from src.mcp_server.auth import current_permission  # noqa: E402
from src.services import vault as vault_service  # noqa: E402
from src.services import vault_fs  # noqa: E402


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


# ── a YAML comment is not a heading ─────────────────────────────────────────


def test_a_yaml_comment_is_not_selectable_as_a_heading(vault):
    """The spec's own scenario. `# Tasks` lives inside the block; the note's
    only real heading is `# Body`. Selecting `Tasks` must report it missing and
    list what IS there."""
    original = "---\n# Tasks\nstatus: draft\n---\n# Body\nkeep\n"
    write(vault, "n.md", original)
    result = asyncio.run(tools.edit_note_impl("n.md", "x", section="Tasks"))
    assert "not found" in result
    assert "Body" in result
    assert "Tasks" in result  # the selector is echoed
    assert read(vault, "n.md") == original


def test_a_yaml_comment_is_not_counted_by_an_ordinal(vault):
    """Off-by-one was the live damage: with the comment counted, `#1` named
    the YAML line and `#2` named the body's first heading."""
    write(vault, "n.md", "---\n# Not A Heading\na: 1\n---\n## First\nx\n## Second\ny\n")
    asyncio.run(tools.edit_note_impl("n.md", "REPLACED\n", section="#1"))
    assert read(vault, "n.md") == (
        "---\n# Not A Heading\na: 1\n---\n## First\nREPLACED\n## Second\ny\n"
    )


def test_ordinals_agree_between_read_note_and_edit_note(vault):
    """The parity the base spec promises, on the note that used to break it.
    Whatever `read_note(section='#N')` extracts is what `edit_note` edits."""
    write(
        vault,
        "n.md",
        "---\n# comment one\n# comment two\ntitle: T\n---\n"
        "# Alpha\na\n# Beta\nb\n# Gamma\nc\n",
    )
    for ordinal, heading in [("#1", "# Alpha"), ("#2", "# Beta"), ("#3", "# Gamma")]:
        extracted = asyncio.run(tools.read_note_impl("n.md", section=ordinal))
        assert extracted.heading == heading
        result = asyncio.run(
            tools.edit_note_impl("n.md", f"body-{ordinal}\n", section=ordinal)
        )
        assert "Updated note" in result

    assert read(vault, "n.md") == (
        "---\n# comment one\n# comment two\ntitle: T\n---\n"
        "# Alpha\nbody-#1\n# Beta\nbody-#2\n# Gamma\nbody-#3\n"
    )


# ── the block is reattached byte-identically ────────────────────────────────


def test_a_section_edit_never_alters_the_block(vault):
    block = "---\ntitle: Keep\ntags:\n  - a\n# a comment\n---\n"
    write(vault, "n.md", block + "## Tasks\nold\n## Notes\nkeep\n")
    asyncio.run(tools.edit_note_impl("n.md", "new\n", section="Tasks"))
    after = read(vault, "n.md")
    assert after.startswith(block)
    assert after == block + "## Tasks\nnew\n## Notes\nkeep\n"


def test_a_trailing_whitespace_closer_survives_a_section_edit(vault):
    """The closer `parse_frontmatter` accepts is preserved as written — the
    reattachment is a byte slice, not a re-render."""
    block = "---\ntitle: Sloppy\n---\t \n"
    write(vault, "n.md", block + "## Tasks\nold\n")
    asyncio.run(tools.edit_note_impl("n.md", "new\n", section="Tasks"))
    assert read(vault, "n.md") == block + "## Tasks\nnew\n"


def test_everything_outside_the_replaced_section_is_untouched(vault):
    """Fence-line deletion is the failure mode this pins: with raw resolution a
    replacement could start inside the block and run to the next body heading,
    taking the closing fence with it."""
    original = "---\n# fm comment\nstatus: draft\n---\n## A\naaa\n## B\nbbb\n## C\nccc\n"
    write(vault, "n.md", original)
    asyncio.run(tools.edit_note_impl("n.md", "BBB\n", section="B"))
    after = read(vault, "n.md")
    assert after == "---\n# fm comment\nstatus: draft\n---\n## A\naaa\n## B\nBBB\n## C\nccc\n"
    # Both fences still there, exactly twice.
    assert after.count("---\n") == 2
    assert after.startswith("---\n# fm comment\nstatus: draft\n---\n")


def test_the_ambiguity_listing_counts_body_headings_only(vault):
    write(
        vault,
        "n.md",
        # A `#` comment BESIDE a real key, so the block stays a valid mapping —
        # a comment-only block is a non-mapping and would be refused instead.
        "---\n# Dup\ntitle: T\n---\n## Parent\n### Dup\na\n### Dup\nb\n",
    )
    result = asyncio.run(tools.edit_note_impl("n.md", "x", section="Dup"))
    assert "2 headings" in result
    # The ordinals offered are the body's, so following the advice works.
    assert "#2" in result and "#3" in result
    assert "#4" not in result


def test_an_empty_block_note_resolves_over_its_body(vault):
    write(vault, "n.md", "---\n---\n## Tasks\nold\n")
    asyncio.run(tools.edit_note_impl("n.md", "new\n", section="Tasks"))
    assert read(vault, "n.md") == "---\n---\n## Tasks\nnew\n"


def test_a_note_with_no_fence_resolves_over_raw_as_today(vault):
    write(vault, "n.md", "## Tasks\nold\n## Notes\nkeep\n")
    asyncio.run(tools.edit_note_impl("n.md", "new\n", section="Tasks"))
    assert read(vault, "n.md") == "## Tasks\nnew\n## Notes\nkeep\n"


def test_a_valid_block_with_an_empty_body_reports_no_headings(vault):
    original = "---\ntitle: Meta\n---\n"
    write(vault, "n.md", original)
    result = asyncio.run(tools.edit_note_impl("n.md", "x", section="Tasks"))
    assert "no ATX headings" in result
    assert read(vault, "n.md") == original


# ── a defective block refuses ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "original,defect",
    [
        # The spec's own example: comment-only YAML is a NON-MAPPING, and it is
        # precisely the shape whose `#` line would be selected as a heading.
        ("---\n# Tasks\n---\n# Body\nkeep\n", "not a mapping"),
        ("---\nunclosed\n## Tasks\nold\n", "never closed"),
        ("---\na: [\n---\n## Tasks\nold\n", "does not parse as YAML"),
        ("---\n- a\n- b\n---\n## Tasks\nold\n", "not a mapping"),
        ("---\nnull\n---\n## Tasks\nold\n", "not a mapping"),
    ],
)
@pytest.mark.parametrize("selector", ["Tasks", "#1", "Parent/Tasks"])
def test_a_defective_block_refuses_a_section_write(vault, original, defect, selector):
    write(vault, "n.md", original)
    result = asyncio.run(tools.edit_note_impl("n.md", "x", section=selector))
    assert "malformed frontmatter block" in result
    assert defect in result
    assert "replace_frontmatter=True" in result
    assert read(vault, "n.md") == original


def test_the_refusal_precedes_heading_resolution(vault):
    """Naming the defect is more useful than naming a heading that could not be
    resolved safely anyway — and resolving first would mean scanning the raw
    bytes the refusal exists to avoid."""
    original = "---\nbroken: [\n---\n## Tasks\nold\n"
    write(vault, "n.md", original)
    result = asyncio.run(
        tools.edit_note_impl("n.md", "x", section="NoSuchHeading")
    )
    assert "malformed frontmatter block" in result
    assert "not found" not in result
    assert read(vault, "n.md") == original


def test_dry_run_refuses_a_defective_block_too(vault):
    original = "---\nbroken: [\n---\n## Tasks\nold\n"
    write(vault, "n.md", original)
    result = asyncio.run(
        tools.edit_note_impl("n.md", "x", section="Tasks", dry_run=True)
    )
    assert "malformed frontmatter block" in result
    assert read(vault, "n.md") == original


def test_reads_are_deliberately_not_refused(vault):
    """The asymmetry is stated in the docstrings: a read destroys nothing, so
    `read_note` keeps extracting from a malformed-block note."""
    write(vault, "n.md", "---\nbroken: [\n---\n## Tasks\nold\n")
    extracted = asyncio.run(tools.read_note_impl("n.md", section="Tasks"))
    assert extracted.error is None
    assert extracted.heading == "## Tasks"
    assert extracted.content == "old\n"


# ── lone-CR notes ───────────────────────────────────────────────────────────


def test_a_lone_cr_block_is_held_out_of_the_section_scan(vault):
    """With the block unrecognised, the `#`-comment line inside it was a
    heading on the write side and the replacement could run from inside the
    block."""
    original = "---\r# Not A Heading\rtitle: T\r---\r## Tasks\rold\r## Notes\rkeep\r"
    write(vault, "n.md", original)
    asyncio.run(tools.edit_note_impl("n.md", "new\n", section="#1"))
    after = read(vault, "n.md")
    # `#1` is the body's first heading, not the `#` comment inside the block —
    # which is what proves the block was held out of the scan.
    assert after == "---\r# Not A Heading\rtitle: T\r---\r## Tasks\rnew\n## Notes\rkeep\r"
    # The block is reattached byte-identically, CR terminators included.
    assert after.startswith("---\r# Not A Heading\rtitle: T\r---\r")


def test_a_defective_lone_cr_block_refuses_a_section_write(vault):
    original = "---\r# Tasks\r---\r# Body\rkeep\r"
    write(vault, "n.md", original)
    result = asyncio.run(tools.edit_note_impl("n.md", "x", section="Tasks"))
    assert "malformed frontmatter block" in result
    assert "not a mapping" in result
    assert read(vault, "n.md") == original


# ── code masking obeys the same terminator rule as everything else ──────────
#
# The masker feeds the heading scanner, and it was LF-only. On a lone-CR note
# that diverged in BOTH directions against `read_note`, which sees the same
# file through universal-newline translation:
#
#   * a `~~~`-fenced block was never masked, so a heading INSIDE code was
#     selectable by `edit_note(section=…)` while `read_note` hid it — and the
#     replacement span then ran through the closing fence and deleted it;
#   * inline code, whose class ran happily across `\r`, joined two lines and
#     masked a REAL heading that `read_note` could see.


def _write_side(text):
    from src.services.vault import _scan_headings
    return [(h["depth"], h["text"]) for h in _scan_headings(text)]


def _read_side(text):
    """What `read_note` scans: the same bytes after universal-newline
    translation, which is what `Path.read_text` hands the read path."""
    from src.services.vault import _scan_headings
    return [(h["depth"], h["text"]) for h in _scan_headings(text.replace("\r", "\n"))]


@pytest.mark.parametrize(
    "body",
    [
        # Tilde fence: never masked on the raw side, so `## Hidden` was a
        # selectable heading on write and invisible on read.
        "~~~\r## Hidden\r~~~\r## Real\rold\r## Next\rkeep\r",
        "~~~~\r## Hidden\r~~~~\r## Real\rold\r",
        # Backtick fences, including one carrying an info string.
        "```\r## Hidden\r```\r## Real\rold\r## Next\rkeep\r",
        "```py\r## Hidden\r```\r## Real\rold\r",
        # Inline code over-reach: an odd backtick on one CR line paired with
        # one on a later line, masking `## Real` on the write side only.
        "a ` b\r## Real\rc ` d\r## Next\rx\r",
    ],
)
def test_read_and_write_agree_about_headings_inside_cr_code(body):
    assert _write_side(body) == _read_side(body)


def test_a_heading_inside_a_cr_fenced_block_is_not_selectable(vault):
    """The BLOCKER in tool terms: `#1` must mean the same section to both
    tools, and a section write must never reach inside a code fence."""
    original = (
        "---\rtitle: T\r---\r```\r## Hidden\r```\r## Real\rold\r## Next\rkeep\r"
    )
    write(vault, "n.md", original)

    extracted = asyncio.run(tools.read_note_impl("n.md", section="#1"))
    assert extracted.heading == "## Real"
    assert "## Hidden" not in extracted.content

    asyncio.run(tools.edit_note_impl("n.md", "new\n", section="#1"))
    after = read(vault, "n.md")
    # `#1` resolved to `Real` on the write side too, and the code fence — both
    # of its delimiters and the heading inside it — is untouched.
    assert after == (
        "---\rtitle: T\r---\r```\r## Hidden\r```\r## Real\rnew\n## Next\rkeep\r"
    )
    assert after.count("```") == 2
    assert "## Hidden" in after


def test_a_cr_fenced_block_is_masked_without_moving_offsets(vault):
    """The masker must stay offset-stable — `_scan_headings` reports positions
    into the UNMASKED text and `extract_links` records byte offsets against
    the original."""
    from src.services.links import BODY, FULL_NOTE, mask_code

    for text in [
        "~~~\r## Hidden\r~~~\r## Real\rold\r",
        "```\r## Hidden\r```\r## Real\rold\r",
        "a ` b\r## Real\rc ` d\r",
        "~~~\n## Hidden\n~~~\n## Real\nold\n",
    ]:
        # `context` became explicit with the #150 fence grammar; the
        # same-length invariant is a property of both contexts.
        assert len(mask_code(text, context=BODY)) == len(text)
        assert len(mask_code(text, context=FULL_NOTE)) == len(text)


def test_a_unicode_space_heading_survives_on_an_lf_note(vault):
    """The separator class is "all whitespace except the three terminators".
    An earlier CR-aware draft used `[ \\t]`, which dropped every heading
    separated by a non-ASCII space — shifting names and `#N` ordinals on
    existing LF-only vaults."""
    nbsp, thin, ideo = " ", " ", "　"
    # Siblings at one depth, so replacing the middle one bounds cleanly.
    original = (
        f"##{nbsp}Legacy\nalpha\n"
        f"##{thin}Thin\nbeta\n"
        f"##{ideo}Ideo\ngamma\n"
    )
    write(vault, "n.md", original)
    assert _write_side(original) == [(2, "Legacy"), (2, "Thin"), (2, "Ideo")]
    # ...and the ordinals an agent is told to rely on still address them.
    assert _write_side(original) == _read_side(original)

    result = asyncio.run(tools.edit_note_impl("n.md", "BETA\n", section="Thin"))
    assert "Updated note" in result
    assert read(vault, "n.md") == (
        f"##{nbsp}Legacy\nalpha\n"
        f"##{thin}Thin\nBETA\n"
        f"##{ideo}Ideo\ngamma\n"
    )
