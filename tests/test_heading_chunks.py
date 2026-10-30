"""Heading-aware chunking (#355, design D1): the sectioner, its edge cases, and
ordinal parity with section addressing.

The chunker must use the section-addressing scanner, so the ordinal a chunk
records is the one `read_note(section="#N")` resolves for the same bytes, and
each chunk's text is a substring of that section's body with the whole-body
fence spans removed (containment). These tests pin that over hand-written
shapes and over a seeded generator of notes built from the awkward pieces:
fences of every kind, inline code, CRLF and lone CR, frontmatter, duplicate
headings and empty sections.
"""
import random

import pytest

from src.config import MAX_CHUNKS_PER_NOTE
from src.services import embeddings
from src.services.embeddings import (
    CHUNKING_VERSION,
    ChunkSpec,
    _remove_spans,
    chunk_note_bounded,
    chunk_text_bounded,
    clean_for_embedding,
)
from src.services.links import BODY, scan_fences
from src.services.vault import (
    _LINE_BREAK_RE,
    _scan_headings,
    extract_section_parts,
    parse_frontmatter,
)

WINDOW = 512 * 4


def chunks(body, **kw):
    kw.setdefault("chunk_size", 512)
    kw.setdefault("overlap", 0)
    kw.setdefault("max_chunks", MAX_CHUNKS_PER_NOTE)
    return chunk_note_bounded(body, **kw)


def ordinals(specs):
    return [s.section_ordinal for s in specs]


def heading_lines_removed(body: str) -> str:
    """`body` with every heading line (and its one terminator) and every
    whole-body fence span removed: the concatenation the sections must equal."""
    spans = list(scan_fences(body, context=BODY).spans)
    for h in _scan_headings(body):
        end = h["line_end"]
        m = _LINE_BREAK_RE.match(body, end)
        if m is not None:
            end = m.end()
        spans.append((h["line_start"], end))
    return _remove_spans(body, sorted(spans))


def section_cleaned(body: str, n: int) -> str:
    """`read_note(section="#n")`'s body with the whole-body fence spans
    removed — the containment target."""
    parts, err = extract_section_parts(body, f"#{n}")
    assert err is None, err
    _heading, section_body = parts
    # Locate the section body by the scanner's own offsets, never by search.
    headings = _scan_headings(body)
    h = headings[n - 1]
    start = h["line_end"]
    m = _LINE_BREAK_RE.match(body, start)
    if m is not None:
        start = m.end()
    end = start + len(section_body)
    assert body[start:end] == section_body
    spans = scan_fences(body, context=BODY).spans
    return embeddings._clean_span(body, start, end, spans)


def assert_parity(body: str):
    specs, _ = chunks(body)
    headings = _scan_headings(body)
    preamble_end = headings[0]["line_start"] if headings else len(body)
    preamble = embeddings._clean_span(
        body, 0, preamble_end, scan_fences(body, context=BODY).spans
    )
    fallback = clean_for_embedding(body)
    for spec in specs:
        if spec.section_ordinal == 0:
            assert spec.heading_path == ()
            assert spec.text in preamble or spec.text in fallback
            continue
        assert spec.heading_path, spec
        assert spec.heading_path[-1] == embeddings._cut(
            headings[spec.section_ordinal - 1]["text"], 200
        )
        assert spec.text in section_cleaned(body, spec.section_ordinal), (
            spec, body,
        )


# ── basics ─────────────────────────────────────────────────────────────────


def test_chunking_version_is_one():
    assert CHUNKING_VERSION == 1


def test_sections_and_paths():
    body = "pre\n# A\n\nalpha\n## B\nbeta\n### C\n# D\ndelta"
    specs, truncated = chunks(body)
    assert not truncated
    assert [(s.text, s.heading_path, s.section_ordinal) for s in specs] == [
        ("pre\n", (), 0),
        ("\nalpha\n", ("A",), 1),
        ("beta\n", ("A", "B"), 2),
        ("delta", ("D",), 4),
    ]
    assert_parity(body)


def test_heading_lines_are_not_chunk_text():
    body = "# Title\nbody one\n## Sub\nbody two\n"
    for spec in chunks(body)[0]:
        assert "#" not in spec.text


def test_preamble_is_section_zero():
    specs, _ = chunks("intro text\n\n## Real\nreal body\n")
    assert specs[0] == ChunkSpec("intro text\n\n", (), 0)
    assert specs[1].section_ordinal == 1


def test_parent_with_only_subsections():
    specs, _ = chunks("## Parent\n### Child\nchild text\n")
    assert [(s.heading_path, s.section_ordinal) for s in specs] == [
        (("Parent", "Child"), 2)
    ]


def test_empty_sections_yield_nothing():
    specs, _ = chunks("# A\n\n   \n# B\nb\n# C\n\t\n")
    assert ordinals(specs) == [2]


def test_duplicate_sibling_headings_have_distinct_ordinals():
    body = "# P\n## Same\none\n## Same\ntwo\n"
    specs, _ = chunks(body)
    assert [(s.heading_path, s.section_ordinal) for s in specs] == [
        (("P", "Same"), 2),
        (("P", "Same"), 3),
    ]
    assert_parity(body)


def test_a_slash_in_a_heading_is_harmless():
    body = "# a/b\ntext\n"
    specs, _ = chunks(body)
    assert specs[0].heading_path == ("a/b",)
    assert_parity(body)


def test_a_long_heading_component_is_cut_and_marked():
    long = "H" * 300
    specs, _ = chunks(f"# {long}\ntext\n")
    (component,) = specs[0].heading_path
    assert len(component) == 200 and component.endswith("…")


# ── fences and inline code ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "fence",
    [
        "```\n# Not a heading\n```\n",
        "~~~\n# Not a heading\n~~~\n",
        "````\n# Not a heading\n```\nstill code\n````\n",
        "   ```\n# Not a heading\n   ```\n",
    ],
    ids=["backtick", "tilde", "longer_closer", "indented"],
)
def test_a_heading_inside_a_fence_is_not_a_boundary(fence):
    body = f"## Real\nbefore\n{fence}after\n"
    specs, _ = chunks(body)
    assert {s.section_ordinal for s in specs} == {1}
    assert all("Not a heading" not in s.text for s in specs)
    assert_parity(body)


def test_an_unterminated_column_zero_fence_masks_to_the_end():
    body = "## Real\nbefore\n```\n# Not a heading\nmore\n"
    specs, _ = chunks(body)
    assert ordinals(specs) == [1]
    assert specs[0].text == "before\n"
    assert_parity(body)


def test_inline_code_holding_a_heading_is_text():
    body = "## Real\n`# not a heading` inline\n"
    specs, _ = chunks(body)
    assert ordinals(specs) == [1]
    assert "`# not a heading`" in specs[0].text


# ── terminators and frontmatter ───────────────────────────────────────────


@pytest.mark.parametrize("nl", ["\n", "\r\n", "\r"], ids=["lf", "crlf", "cr"])
def test_terminators(nl):
    body = nl.join(["pre", "# A", "alpha", "## B", "beta", "```", "# x", "```", "# C", "c"])
    specs, _ = chunks(body)
    assert ordinals(specs) == [0, 1, 2, 3]
    assert specs[1].text.strip() == "alpha"
    assert_parity(body)


@pytest.mark.parametrize(
    "raw",
    [
        "---\ntitle: T\n---\n# A\nalpha\n",
        "---\n   \n---\n# A\nalpha\n",
        "---\ntitle: [unclosed\n---\n# A\nalpha\n",
        "---\nno closer\n# A\nalpha\n",
    ],
    ids=["valid", "whitespace_only", "defective", "unterminated"],
)
def test_frontmatter_partition(raw):
    _fm, body = parse_frontmatter(raw)
    specs, _ = chunks(body)
    assert specs, raw
    assert_parity(body)


# ── no headings, headings only ────────────────────────────────────────────


NO_HEADING_CORPUS = [
    "",
    "   \n\t",
    "short",
    "a" * WINDOW,
    "a" * (WINDOW + 1),
    ("lorem ipsum " * 900),
    "text\n```\ncode\n```\nmore",
    "x\n~~~\nunterminated",
    "#not-a-heading\n#tag\n",
    "line\r\nline\r\n",
]


@pytest.mark.parametrize("body", NO_HEADING_CORPUS)
def test_no_headings_is_byte_identical_to_the_pre_change_chunker(body):
    assert not _scan_headings(body)
    legacy, legacy_cut = chunk_text_bounded(clean_for_embedding(body))
    specs, cut = chunks(body)
    assert [s.text for s in specs] == legacy
    assert cut == legacy_cut
    assert all(s.section_ordinal == 0 and s.heading_path == () for s in specs)


def test_headings_only_falls_back_to_whole_body_chunking():
    body = "# A\n## B\n### C\n"
    specs, _ = chunks(body)
    assert specs == [ChunkSpec(body, (), 0)]
    assert [s.text for s in specs] == chunk_text_bounded(clean_for_embedding(body))[0]


def test_zero_chunks_exactly_when_the_pre_change_chunker_gives_zero():
    for body in ["", "  \n", "```\nonly code\n```\n", "# A\n```\ncode\n```\n"]:
        legacy = chunk_text_bounded(clean_for_embedding(body))[0]
        assert bool(chunks(body)[0]) == bool(legacy), body


# ── windows and the cap ───────────────────────────────────────────────────


def test_a_section_of_exactly_one_window():
    specs, _ = chunks("# A\n" + "x" * WINDOW)
    assert len(specs) == 1


def test_one_window_plus_one_character():
    specs, _ = chunks("# A\n" + "x" * (WINDOW + 1))
    assert len(specs) == 2
    assert {s.section_ordinal for s in specs} == {1}


def test_a_long_section_spans_three_windows():
    specs, _ = chunks("# A\n" + "x" * (WINDOW * 3))
    assert len(specs) == 3
    assert {(s.heading_path, s.section_ordinal) for s in specs} == {(("A",), 1)}


def test_the_cap_counts_across_sections():
    body = "".join(f"# S{i}\ntext {i}\n" for i in range(1, 8))
    specs, truncated = chunks(body, max_chunks=5)
    assert ordinals(specs) == [1, 2, 3, 4, 5]
    assert truncated is True


def test_exactly_at_the_cap_is_not_truncated():
    body = "".join(f"# S{i}\ntext {i}\n" for i in range(1, 6))
    specs, truncated = chunks(body, max_chunks=5)
    assert len(specs) == 5 and truncated is False


def test_the_cap_inside_a_section():
    specs, truncated = chunks("# A\na\n# B\n" + "x" * (WINDOW * 4), max_chunks=3)
    assert ordinals(specs) == [1, 2, 2] and truncated is True


def test_empty_sections_after_the_cap_do_not_mark_truncation():
    body = "".join(f"# S{i}\ntext {i}\n" for i in range(1, 6)) + "# E\n\n# F\n"
    specs, truncated = chunks(body, max_chunks=5)
    assert len(specs) == 5 and truncated is False


# ── parity property ───────────────────────────────────────────────────────


PIECES = [
    "# H1\n", "## H2\n", "### H3\n", "## Dup\n", "## Dup\n", "#### Deep\n",
    "plain prose line\n", "\n", "   \n",
    "```\n# fenced heading\n```\n", "~~~py\n## fenced\n~~~\n",
    "````\n```\n# inner\n````\n", "  ```\n# indented fence\n  ```\n",
    "`# inline` code\n", "- [ ] task\n", "#tag not heading\n",
    "x" * 700 + "\n", "trailing words",
]


def _generated(seed: int, nl: str) -> str:
    rng = random.Random(seed)
    body = "".join(rng.choice(PIECES) for _ in range(rng.randint(1, 30)))
    if rng.random() < 0.15:
        body += "```\n# unterminated\n"
    return body.replace("\n", nl)


@pytest.mark.parametrize("nl", ["\n", "\r\n", "\r"], ids=["lf", "crlf", "cr"])
def test_parity_property_over_generated_notes(nl):
    for seed in range(250):
        body = _generated(seed, nl)
        assert_parity(body)


@pytest.mark.parametrize("nl", ["\n", "\r\n", "\r"], ids=["lf", "crlf", "cr"])
def test_concatenation_equals_clean_body_minus_heading_lines(nl):
    for seed in range(250):
        body = _generated(seed, nl)
        joined = "".join(
            embeddings._clean_span(body, start, end, scan_fences(body, context=BODY).spans)
            for start, end, _path, _ordinal in embeddings._note_sections(body)
        )
        assert joined == heading_lines_removed(body), body


def test_every_chunk_ordinal_resolves_through_read_note_section_selector():
    body = "intro\n# A\na\n## B\nb\n## B\nb2\n# C\n```\n# x\n```\nc\n"
    for spec in chunks(body)[0]:
        if spec.section_ordinal:
            parts, err = extract_section_parts(body, f"#{spec.section_ordinal}")
            assert err is None
            assert parts[0].lstrip("#").strip() == spec.heading_path[-1]


def test_a_setext_heading_is_body_text():
    """L6: only ATX headings are boundaries, as in section addressing."""
    body = "Title\n=====\nbody\n"
    specs, _ = chunks(body)
    assert specs == [ChunkSpec(body, (), 0)]
    _parts, err = extract_section_parts(body, "#1")
    assert err is not None
