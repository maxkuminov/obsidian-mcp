"""The one frontmatter partition, pinned branch by branch (#128, task 1.2).

`parse_frontmatter` used to return `({}, raw)` for six distinct inputs, which
conflated "this note has no frontmatter" with "this note's frontmatter is
broken". Every silently-wrong write in #128 grew out of that conflation:
`set_frontmatter` prepended a second block above a broken one, and section mode
scanned the broken block's `#` comments as headings.

`parse_frontmatter_diagnose` separates them, and it does so through the *same*
`_partition_frontmatter` the read parser calls — the predicates cannot drift
apart, because there is only one copy of them. What is asserted here is that
partition, exhaustively, plus the one deliberate behaviour change to the read
parser (D3): whitespace-only fenced YAML is a valid EMPTY mapping.

Fully offline: pure string in, tuple out.
"""

import os
import tempfile

os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
os.chdir(tempfile.gettempdir())

import pytest  # noqa: E402

from src.services.vault import (  # noqa: E402
    parse_frontmatter,
    parse_frontmatter_diagnose,
)


# ── valid blocks ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,fm,body,block",
    [
        # LF, ordinary mapping.
        ("---\na: 1\n---\nbody\n", {"a": 1}, "body\n", "---\na: 1\n---\n"),
        # CRLF throughout. The block keeps its CR bytes; the body keeps its own.
        (
            "---\r\na: 1\r\n---\r\nbody\r\n",
            {"a": 1},
            "body\r\n",
            "---\r\na: 1\r\n---\r\n",
        ),
        # Trailing spaces on the closing fence — accepted by the read parser
        # today, so the write side must see one block here too.
        ("---\na: 1\n---   \nbody", {"a": 1}, "body", "---\na: 1\n---   \n"),
        # Trailing tabs, same rule.
        ("---\na: 1\n---\t\nbody", {"a": 1}, "body", "---\na: 1\n---\t\n"),
        # Empty body after a complete block.
        ("---\na: 1\n---\n", {"a": 1}, "", "---\na: 1\n---\n"),
        # Metadata-only, closing fence at EOF with NO trailing newline. This is
        # the input the separator rule (D2) exists for.
        ("---\na: 1\n---", {"a": 1}, "", "---\na: 1\n---"),
    ],
)
def test_a_valid_block_is_partitioned_and_its_span_is_exact(raw, fm, body, block):
    got_fm, got_body, diagnosis = parse_frontmatter_diagnose(raw)
    assert diagnosis.valid is True
    assert diagnosis.defect is None
    assert got_fm == fm
    assert got_body == body
    assert diagnosis.block == block
    # The span is the parser's own, not `raw[:-len(body)]` — which is exactly
    # what an empty body would break.
    assert diagnosis.block + got_body == raw
    # The read parser agrees, because it is the same partition.
    assert parse_frontmatter(raw) == (got_fm, got_body)


# ── the one deliberate change: the whitespace-only block ────────────────────


@pytest.mark.parametrize(
    "raw,body",
    [
        ("---\n---\n", ""),
        ("---\n---\nbody\n", "body\n"),
        ("---\n\n \t\n---\nbody\n", "body\n"),
        ("---\r\n---\r\nbody\r\n", "body\r\n"),
        # Empty block, closing fence at EOF without a newline.
        ("---\n---", ""),
    ],
)
def test_a_whitespace_only_block_is_a_valid_empty_mapping(raw, body):
    """D3. This is the single behaviour change to `parse_frontmatter`, and it
    has to land in the READ parser as well as the diagnosing sibling: if the
    read side kept treating `---\\n---\\n` as absent while the write side
    preserved it as a block, feeding a read body back to default full
    replacement would DUPLICATE the block."""
    fm, got_body, diagnosis = parse_frontmatter_diagnose(raw)
    assert diagnosis.valid is True
    assert diagnosis.defect is None
    assert fm == {}
    assert got_body == body
    assert diagnosis.block + got_body == raw
    # The shared partition: the read parser strips it too.
    assert parse_frontmatter(raw) == ({}, body)


# ── unclosed fence ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw",
    [
        "---\nnope",
        "---\na: 1\n",
        "---\n",
        # A file that is nothing but an unterminated fence line, with and
        # without a CR. Named in the spec explicitly.
        "---",
        "---\r",
        # A `---` that is not on its own line does not close anything.
        "---\na: 1\n--- x\nbody\n",
    ],
)
def test_an_unclosed_fence_is_a_named_defect(raw):
    fm, body, diagnosis = parse_frontmatter_diagnose(raw)
    assert diagnosis.valid is False
    assert diagnosis.defect == "unclosed_fence"
    assert "never closed" in diagnosis.message
    # The read parser's long-standing answer is unchanged: block stays in body.
    assert (fm, body) == ({}, raw)
    assert parse_frontmatter(raw) == ({}, raw)


# ── YAML that does not parse ────────────────────────────────────────────────


@pytest.mark.parametrize("raw", ["---\na: [\n---\nbody\n", "---\n\ta: 1\n---\nb\n"])
def test_unparseable_yaml_carries_the_parser_message(raw):
    fm, body, diagnosis = parse_frontmatter_diagnose(raw)
    assert diagnosis.valid is False
    assert diagnosis.defect == "yaml_error"
    # PyYAML's own text, not a paraphrase: the caller has to be able to find
    # the offending line.
    assert "does not parse as YAML" in diagnosis.message
    assert len(diagnosis.message) > len("the frontmatter block does not parse as YAML: ")
    assert (fm, body) == ({}, raw)
    assert parse_frontmatter(raw) == ({}, raw)


# ── YAML that parses but is not a mapping ───────────────────────────────────


@pytest.mark.parametrize(
    "raw",
    [
        "---\n- a\n- b\n---\nbody\n",     # list
        "---\njust a string\n---\nbody\n",  # scalar
        "---\n42\n---\nbody\n",           # scalar
        "---\nnull\n---\nbody\n",         # explicit null
        "---\n~\n---\nbody\n",            # explicit null, other spelling
        "---\n# Tasks\n---\n# Body\nkeep\n",  # comment-only
    ],
)
def test_non_mapping_yaml_is_a_named_defect(raw):
    """Comment-only YAML must NOT be valid. If it were, its `#` line would sit
    inside a block section mode treats as frontmatter on the write side while
    `read_note`'s heading scan sees it as a heading — the read/write selector
    parity this change exists to restore."""
    fm, body, diagnosis = parse_frontmatter_diagnose(raw)
    assert diagnosis.valid is False
    assert diagnosis.defect == "not_a_mapping"
    assert "not a mapping" in diagnosis.message
    assert (fm, body) == ({}, raw)
    assert parse_frontmatter(raw) == ({}, raw)


# ── absent: not a defect, and not a block ───────────────────────────────────


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "no frontmatter here\n",
        "----\nx\n----\nbody\n",          # four dashes is not the fence
        "--- x\n---\nbody\n",             # content on the fence line
        "\n---\na: 1\n---\nbody\n",       # leading blank line: not line 1
        "text\n---\na: 1\n---\nbody\n",   # leading content: not line 1
        "--\n---\n",                      # does not even start with three
    ],
)
def test_absent_frontmatter_is_neither_valid_nor_defective(raw):
    """The has-valid-block signal is `valid`, never `defect is None`: absent
    and valid both have no defect to name, and the write paths treat them
    completely differently."""
    fm, body, diagnosis = parse_frontmatter_diagnose(raw)
    assert diagnosis.valid is False
    assert diagnosis.defect is None
    assert diagnosis.block == ""
    assert (fm, body) == ({}, raw)
    assert parse_frontmatter(raw) == ({}, raw)
