"""A minimum chunk length and word-boundary windows (#390), offline.

The chunker (design D1–D4, D7), the frozen per-version embedded-text
formatters and the bounded provider input (D6, D9), the constants (D2), and a
property test over generated notes: containment and ordinal parity with
`read_note(section="#N")`, coverage, the length bounds, `under_min`, and the
zero-chunk invariant. The PostgreSQL half (cross-version reuse end to end,
the SQL `clen` twin, the harness's `chunk_lengths`) is
`tests/integration/test_chunk_min_length_pg.py`.
"""
import functools
import json
import random
import re

import pytest

from src.config import MAX_CHUNKS_PER_NOTE, Settings, settings
from src.services import embeddings
from src.services.embeddings import (
    CHUNK_BOUNDARY_LOOKBACK_CHARS,
    CHUNK_MIN_CHARS,
    CHUNKING_VERSION,
    EMBED_PREFIX_MAX_CHARS,
    EMBEDDED_TEXT_FORMATTERS,
    build_embed_prefix,
    chunk_note_bounded,
    chunk_text_bounded,
    clean_for_embedding,
    clen,
    effective_min_chars,
    embedded_text,
    embedding_input,
    lookback,
)
from src.services.index_state import embedding_fingerprint
from src.services.links import BODY, scan_fences
from src.services.vault import (
    _LINE_BREAK_RE,
    _scan_headings,
    extract_section_parts,
    parse_frontmatter,
)

W = 512 * 4
M = 200
P300 = "P" * EMBED_PREFIX_MAX_CHARS
PROSE = (
    "The quick brown fox jumps over the lazy dog while the archivist "
    "records every entry in the ledger and the clerk files themselves "
)


def chunks(body, **kw):
    kw.setdefault("chunk_size", 512)
    kw.setdefault("overlap", 0)
    kw.setdefault("max_chunks", MAX_CHUNKS_PER_NOTE)
    return chunk_note_bounded(body, **kw)


def lengths(texts):
    return [(len(t), clen(t)) for t in texts]


def windows(text, *, chunk_size=512, overlap=0):
    m = effective_min_chars(chunk_size * 4)
    out, cut = chunk_text_bounded(
        text, chunk_size=chunk_size, overlap=overlap, min_chars=m,
        max_chunks=MAX_CHUNKS_PER_NOTE,
    )
    assert cut is False
    return out


def spans_of(text, *, chunk_size=512, overlap=0):
    m = effective_min_chars(chunk_size * 4)
    spans, _cut = embeddings._text_windows(
        text, chunk_size=chunk_size, overlap=overlap, min_chars=m,
        max_chunks=MAX_CHUNKS_PER_NOTE,
    )
    return spans


def prose(n: int, ending: str = "") -> str:
    """Exactly `n` characters of word-separated prose ending in `ending`."""
    head = (PROSE * (n // len(PROSE) + 2))[: n - len(ending)]
    if ending and head and not head.endswith(" "):
        head = head[:-1] + " "
    out = head + ending
    assert len(out) == n
    return out


# ── 1. constants, no setting ──────────────────────────────────────────────


def test_the_constants():
    assert CHUNK_MIN_CHARS == 200
    assert CHUNK_BOUNDARY_LOOKBACK_CHARS == 200
    assert CHUNKING_VERSION == 2


@pytest.mark.parametrize("size,m,lb", [(1, 2, 0), (64, 128, 25), (512, 200, 200), (100, 200, 40)])
def test_effective_minimum_and_lookback(size, m, lb):
    assert effective_min_chars(size * 4) == m
    assert lookback(size * 4) == lb


def test_the_minimum_is_not_a_setting():
    assert "chunk_min_chars" not in Settings.model_fields
    assert "chunk_boundary_lookback_chars" not in Settings.model_fields


@pytest.mark.parametrize("size", [1, 64, 512])
def test_every_previously_valid_chunk_size_loads_with_an_unchanged_fingerprint(
    size, monkeypatch
):
    Settings(chunk_size=size, chunk_overlap=0, secret_key="k" * 64, _env_file=None)
    monkeypatch.setattr(settings, "chunk_size", size)
    monkeypatch.setattr(settings, "chunk_overlap", 0)
    stored = json.loads(embedding_fingerprint())
    # The pre-change literal's key set and values: nothing about the minimum.
    assert set(stored) == {
        "v", "provider", "model", "dimensions", "chunk_size", "chunk_overlap",
        "max_chunks_per_note",
    }
    assert stored["chunk_size"] == size
    assert embedding_fingerprint() == json.dumps(
        stored, sort_keys=True, separators=(",", ":")
    )


# ── 2.6 the chunker: pinned cases ─────────────────────────────────────────


def test_live_fixture_1_a_short_lead_merges_into_its_first_subsection():
    body = (
        "## Email Triage\nSee entries in the Email Triage log\n"
        "### 2026-10-01\n" + prose(300) + "\n"
    )
    specs, _ = chunks(body)
    assert all(clen(s.text) >= M for s in specs)
    first = specs[0]
    assert "in the Email Triage log" in first.text
    assert "### 2026-10-01" in first.text
    assert (first.section_ordinal, first.heading_path) == (1, ("Email Triage",))
    assert not any(s.text.strip() == "See entries in the Email Triage log" for s in specs)


def test_live_fixture_2_no_four_character_tail_and_no_mid_word_cut():
    text = prose(W * 2 + 4, ending="themselves")
    body = "## Notes\n" + text
    specs, _ = chunks(body)
    assert all(s.text != "lves" and s.text.strip() != "lves" for s in specs)
    assert all(clen(s.text) >= M for s in specs)
    # Every boundary is a word start: the prose has whitespace in every lookback.
    pos = 0
    for s in specs:
        assert text.startswith(s.text, pos)
        assert pos == 0 or text[pos - 1] == " "
        pos += len(s.text)
    assert pos == len(text)


def test_codex_b2_a_hard_cut_with_overlap_skips_nothing():
    text = "A" * 400 + "B" * 20 + " " + "C" * 500
    out = windows(text, chunk_size=100, overlap=1)
    assert any("B" * 20 in c for c in out)
    assert all(text.count(ch) <= sum(c.count(ch) for c in out) for ch in "ABC")


def test_codex_m1_an_outline_with_one_tiny_body():
    body = "".join("#" * (1 + i % 3) + f" Heading {i}\n" for i in range(60)) + "## Last\nx\n"
    specs, _ = chunks(body)
    assert len(specs) == 1
    (only,) = specs
    assert only.section_ordinal == 0 and only.heading_path == ()
    assert "x" in only.text and only.text.strip() != "x"


def test_codex_m2_the_maximum_tail_never_exceeds_one_window():
    text = prose(W + M - 1)
    out = windows(text)
    assert len(out) >= 2
    assert all(len(embedding_input(P300, c)) <= W + 302 for c in out)
    assert all(len(c) <= W for c in out)
    assert clen(out[-1]) >= M and clen(out[-2]) >= M
    assert "".join(out) == text


def test_codex_r2_tail_balancing_at_w_400():
    out = windows("A" * 161 + " " + "B" * 239, chunk_size=100)
    assert out == ["A" * 161 + " " + "B" * 38, "B" * 201]
    assert lengths(out) == [(200, 200), (201, 201)]


def test_codex_r2_a_whitespace_heavy_tail_at_the_default_window():
    out = windows("A" * 1800 + " " * 390 + "B" * 10)
    assert out == ["A" * 905, "A" * 895 + " " * 390 + "B" * 10]
    assert lengths(out) == [(905, 905), (1295, 906)]


def test_codex_r3_high_overlap_after_tail_balancing():
    text = "A" * 599
    assert spans_of(text, chunk_size=100, overlap=99) == [(0, 299), (199, 599)]
    out = windows(text, chunk_size=100, overlap=99)
    assert lengths(out) == [(299, 299), (400, 400)]
    assert all(len(embedding_input(P300, c)) <= 400 + 302 for c in out)


def test_whitespace_that_defeats_the_raw_bound():
    text = "A" * 100 + " " * 3000 + "B" * 100
    (only,) = windows(text)
    assert clen(only) == 201 and len(only) > W
    assert only == text
    # The excess over the window is whitespace only.
    assert len(only) - clen(only) == 2999


def test_a_whitespace_window_before_a_long_run_keeps_the_window_bound():
    """Implementation note 5: past a mostly-whitespace window, the first
    admissible word start would end a window of `clen` 5,002; the first
    admissible position is taken instead."""
    text = "A" + " " * 3000 + "B" * 5000 + " C"
    out = windows(text)
    assert all(clen(c) <= W for c in out)
    assert all(clen(c) >= M for c in out)
    assert "".join(out) == text
    assert clen(out[0]) == M


def test_a_short_section_merges_into_a_following_sibling_under_the_parent():
    body = "# Project\n## A\n" + prose(40) + "\n## B\n" + prose(600) + "\n"
    specs, _ = chunks(body)
    holder = next(s for s in specs if prose(40) in s.text)
    assert (holder.section_ordinal, holder.heading_path) == (1, ("Project",))
    assert "## B" in holder.text


def test_a_short_sibling_merge_with_no_parent_takes_zero():
    body = "## A\n" + prose(40) + "\n## B\n" + prose(600) + "\n"
    specs, _ = chunks(body)
    holder = next(s for s in specs if prose(40) in s.text)
    assert (holder.section_ordinal, holder.heading_path) == (0, ())


def test_a_short_last_section_merges_backward():
    tail = "the end of it all here ok"[:30].ljust(30, "z")
    body = "# One\n" + prose(600) + "\n# Two\n" + tail + "\n"
    specs, _ = chunks(body)
    assert specs[-1].text.rstrip().endswith(tail)
    assert all(s.text.strip() != tail for s in specs)
    assert all(clen(s.text) >= M for s in specs)


def test_a_whole_short_note_keeps_one_chunk():
    for body in ["tiny", "# H\ntiny body\n", "intro\n# A\na\n## B\nb\n"]:
        specs, _ = chunks(body)
        assert len(specs) == 1, body
        assert clen(specs[0].text) < M


def test_an_unmerged_section_keeps_its_own_locator():
    body = "# A\n" + prose(400) + "\n## B\n" + prose(400) + "\n"
    specs, _ = chunks(body)
    assert [(s.heading_path, s.section_ordinal) for s in specs] == [
        (("A",), 1), (("A", "B"), 2)
    ]
    # A one-section group is exactly that section's text, no heading line.
    assert specs[0].text == prose(400) + "\n"


def test_the_headings_only_fallback():
    body = "# A\n## B\n### C\n"
    specs, _ = chunks(body)
    assert [(s.text, s.section_ordinal) for s in specs] == [(body, 0)]


def test_the_cap_counts_across_groups():
    body = "".join(f"# S{i}\n{prose(300)}\n" for i in range(1, 8))
    specs, truncated = chunks(body, max_chunks=5)
    assert [s.section_ordinal for s in specs] == [1, 2, 3, 4, 5] and truncated is True
    specs, truncated = chunks(body, max_chunks=7)
    assert len(specs) == 7 and truncated is False


def test_a_run_without_whitespace_is_cut_hard():
    out = windows("x" * 5000)
    assert [len(c) for c in out] == [W, W, 5000 - 2 * W]
    out = windows("x" * 4500)
    assert all(clen(c) >= M for c in out) and "".join(out) == "x" * 4500


def test_termination_with_overlap():
    text = prose(20000)
    for overlap in (1, 64, 511):
        out = windows(text, overlap=overlap) if overlap < 511 else chunk_text_bounded(
            text, overlap=overlap, min_chars=M
        )[0]
        assert out


@pytest.mark.parametrize("nl", ["\n", "\r\n", "\r"], ids=["lf", "crlf", "cr"])
def test_terminators(nl):
    body = nl.join(["# A", "short lead", "## B", prose(300), "## C", prose(300)])
    specs, _ = chunks(body)
    assert specs[0].section_ordinal == 1 and "short lead" in specs[0].text
    assert_note_properties(body, 512, 0)


def test_a_fence_across_a_merge_boundary():
    body = "# A\nlead\n```\n# not a heading\n" + "c" * 50 + "\n```\n## B\n" + prose(300) + "\n"
    specs, _ = chunks(body)
    assert all("not a heading" not in s.text for s in specs)
    assert specs[0].section_ordinal == 1 and "lead" in specs[0].text
    assert_note_properties(body, 512, 0)


@pytest.mark.parametrize("size", [1, 64])
def test_small_chunk_sizes(size):
    body = "# A\n" + prose(700) + "\n## B\nshort\n"
    specs, _ = chunks(body, chunk_size=size)
    assert specs
    assert_note_properties(body, size, 0)


def test_a_note_without_headings_is_the_text_chunker_with_the_minimum():
    for body in ["", "  \n", "short", prose(W * 3 + 17), "a" * (W + 1), "x\n```\ncode\n```\nmore"]:
        assert not _scan_headings(body)
        assert [s.text for s in chunks(body)[0]] == windows(clean_for_embedding(body))


# ── 2.7 the property test ─────────────────────────────────────────────────


PIECES = [
    "# H1\n", "## H2\n", "### H3\n", "## Dup\n", "## Dup\n", "#### Deep\n",
    "plain prose line\n", "\n", "   \n", "a short one\n",
    "```\n# fenced heading\n```\n", "~~~py\n## fenced\n~~~\n",
    "  ```\n# indented fence\n  ```\n", "`# inline` code\n", "- [ ] task\n",
    "#tag not heading\n", "\t- nested item\n",
]


def _generated(rng: random.Random, nl: str) -> str:
    parts = []
    for _ in range(rng.randint(1, 30)):
        roll = rng.random()
        if roll < 0.12:
            parts.append(prose(rng.randint(150, 900)) + "\n")
        elif roll < 0.15:
            parts.append(prose(rng.randint(2000, 5000)) + "\n")
        elif roll < 0.17:
            parts.append("y" * rng.randint(300, 2500) + "\n")
        elif roll < 0.19:
            parts.append(" " * rng.randint(50, 3500) + "\n")
        else:
            parts.append(rng.choice(PIECES))
    body = "".join(parts)
    if rng.random() < 0.1:
        body += "```\n# unterminated\n"
    if rng.random() < 0.2:
        body = rng.choice(["---\ntitle: T\n---\n", "---\nno closer\n"]) + body
        _fm, body = parse_frontmatter(body)
    return body.replace("\n", nl)


@functools.lru_cache(maxsize=4096)
def section_cleaned(body: str, n: int) -> str:
    parts, err = extract_section_parts(body, f"#{n}")
    assert err is None, err
    headings = _scan_headings(body)
    start, end = embeddings_body_span(body, headings, n - 1)
    assert body[start:end] == parts[1]
    return embeddings._clean_span(body, start, end, scan_fences(body, context=BODY).spans)


def embeddings_body_span(body, headings, idx):
    from src.services.vault import _section_body_span

    return _section_body_span(body, headings, idx)


def heading_line_spans(body):
    out = []
    for h in _scan_headings(body):
        end = h["line_end"]
        mo = _LINE_BREAK_RE.match(body, end)
        if mo is not None:
            end = mo.end()
        out.append((h["line_start"], end))
    return out


def assert_note_properties(body: str, size: int, overlap: int):
    w = size * 4
    m = effective_min_chars(w)
    specs, raw_spans, truncated = embeddings._chunk_note(
        body, chunk_size=size, overlap=overlap, max_chunks=10_000
    )
    assert not truncated
    fences = scan_fences(body, context=BODY).spans
    cleaned_body = clean_for_embedding(body)
    # Zero-chunk invariant.
    assert bool(specs) == bool(chunk_text_bounded(cleaned_body, chunk_size=size)[0])
    whole_short = clen(cleaned_body) < m
    for spec, (rs, re_) in zip(specs, raw_spans):
        # The raw span reproduces the chunk.
        assert embeddings._clean_span(body, rs, re_, fences) == spec.text
        # Containment and ordinal parity.
        if spec.section_ordinal:
            assert spec.text in section_cleaned(body, spec.section_ordinal)
            assert spec.heading_path
        else:
            assert spec.heading_path == ()
            assert spec.text in cleaned_body
        # Length bounds.
        c = clen(spec.text)
        assert c <= w, (c, w)
        if not (whole_short and len(specs) == 1):
            assert c >= m, (c, m, body)
        if len(specs) > 1:
            assert c >= m  # D8's under_min is 0
        # The provider input bound for chunks within the window.
        if len(spec.text) <= w:
            assert len(embedding_input(P300, spec.text)) <= w + 302
    # Raw bound. Every chunk's `clen` is at most `W` (above), so any raw
    # excess is whitespace only. For ordinary text — no whitespace run longer
    # than 8 and no unbroken run longer than the lookback — at the default
    # window, no chunk exceeds `W` raw at all. (The task's "no run longer than
    # `W - 2m`" is not sufficient: see design.md, Implementation notes.)
    longest_ws = max((len(r) for r in re.findall(r"[\t\n\v\f\r ]+", cleaned_body)), default=0)
    longest_word = max((len(r) for r in re.findall(r"[^\t\n\v\f\r ]+", cleaned_body)), default=0)
    if w == W and longest_ws <= 8 and longest_word <= lookback(w):
        assert all(len(s.text) <= w for s in specs), body
    # Coverage: every non-whitespace, non-fence, non-heading-line character.
    excluded = sorted(list(fences) + heading_line_spans(body))
    covered = bytearray(len(body))
    for rs, re_ in raw_spans:
        covered[rs:re_] = b"\x01" * (re_ - rs)
    k = 0
    for i, ch in enumerate(body):
        while k < len(excluded) and excluded[k][1] <= i:
            k += 1
        if k < len(excluded) and excluded[k][0] <= i < excluded[k][1]:
            continue
        if ch in " \t\n\v\f\r":
            continue
        assert covered[i], (i, ch, body)


@pytest.mark.parametrize("nl", ["\n", "\r\n", "\r"], ids=["lf", "crlf", "cr"])
@pytest.mark.parametrize("size,overlap", [(512, 0), (512, 64), (100, 0), (100, 37), (25, 0), (25, 12)])
def test_property_over_generated_notes(nl, size, overlap):
    rng = random.Random(f"{nl}{size}{overlap}")
    for _ in range(120):
        assert_note_properties(_generated(rng, nl), size, overlap)


def test_ordinary_text_stays_within_one_window_raw():
    rng = random.Random("ordinary")
    for _ in range(60):
        parts = []
        for _ in range(rng.randint(1, 12)):
            parts.append("#" * rng.randint(1, 4) + f" Heading {rng.randint(0, 9)}\n")
            parts.append(prose(rng.choice([10, 60, 180, 199, 200, 201, 450, 2047, 2049, 4500])) + "\n")
        body = "".join(parts)
        specs, _ = chunks(body)
        assert all(len(s.text) <= W for s in specs)
        assert all(len(embedding_input(P300, s.text)) <= W + 302 for s in specs)
        assert_note_properties(body, 512, 0)


# ── 2.8 the golden formatter test ─────────────────────────────────────────

FROZEN = "frozen formatter changed: add an entry or clear the table"

GOLDEN_INPUTS = [
    "plain text",
    "a\n\nb",
    "x" + " " * 8 + "y",
    "x" + " " * 9 + "y",
    "p" + "\t" * 9 + "q",
    "p" + "\r" * 10 + "q",
    "n" + " " * 12 + "m",
    " " * 12,
    "\n" + " " * 8 + "- deep",
]
GOLDEN = {
    1: GOLDEN_INPUTS,
    2: [
        "plain text",
        "a\n\nb",
        "x" + " " * 8 + "y",
        "x y",
        "p q",
        "p q",
        "n" + " " * 12 + "m",
        " ",
        " - deep",
    ],
}


def test_golden_formatters():
    assert set(EMBEDDED_TEXT_FORMATTERS) == set(GOLDEN), FROZEN
    for version, expected in GOLDEN.items():
        got = [EMBEDDED_TEXT_FORMATTERS[version](t) for t in GOLDEN_INPUTS]
        assert got == expected, f"{FROZEN} (version {version})"


def test_golden_version_1_input_is_what_355_shipped():
    """#355's `embedding_input` was `f"{prefix}\\n\\n{chunk}"`, verbatim."""
    for chunk in GOLDEN_INPUTS:
        assert "P" + "\n\n" + EMBEDDED_TEXT_FORMATTERS[1](chunk) == f"P\n\n{chunk}", FROZEN


def test_golden_embedding_input_and_prefix():
    prefix = build_embed_prefix("Alpha", ("Project", "Plan"), ["work"])
    assert prefix == "Alpha > Project > Plan\ntags: work", FROZEN
    assert embedding_input(prefix, "x" + " " * 9 + "y") == (
        "Alpha > Project > Plan\ntags: work\n\nx y"
    ), FROZEN
    assert embedding_input("T", "a\n\nb") == "T\n\na\n\nb", FROZEN


# ── 2.8a the bounded whitespace in the provider input (D9) ────────────────


def test_a_whitespace_heavy_chunk_is_sent_collapsed_and_stored_raw():
    text = "A" * 100 + " " * 3000 + "B" * 100
    (spec,) = chunks(text)[0]
    assert spec.text == text
    sent = embedding_input("P", spec.text)
    assert sent == "P\n\n" + "A" * 100 + " " + "B" * 100
    assert len(sent) == len("P") + 203


def test_ordinary_whitespace_is_kept():
    for t in ["a" + " " * 8 + "b", "a\n\nb", "a\r\n\r\nb", "a\n" + " " * 7 + "- item"]:
        assert embedded_text(t) == t
    assert embedded_text("a" + "\t" * 9 + "b") == "a b"
    assert embedded_text("a" + "\r" * 9 + "b") == "a b"
    assert embedded_text("a" + " " * 20 + "b") == "a" + " " * 20 + "b"


# ── 3.2 / 4.2 / 4.6 the one under_min statement, and the docs ─────────────


def test_the_recipe_runs_the_harness_under_min_statement():
    import pathlib

    from scripts.search_eval import db

    root = pathlib.Path(__file__).resolve().parent.parent
    assert embeddings.CLEN_SQL.format(col="ne.chunk_text") in db.UNDER_MIN_SQL
    deployment = (root / "DEPLOYMENT.md").read_text(encoding="utf-8")
    assert db.UNDER_MIN_SQL.replace(":m", str(CHUNK_MIN_CHARS)) in deployment
    for doc in [root / "README.md", root / "DEPLOYMENT.md", root / "CLAUDE.md",
                *sorted((root / "docs").rglob("*.md"))]:
        body = doc.read_text(encoding="utf-8")
        assert "IS DISTINCT FROM 1`" not in body, doc
        assert not re.search(r"`CHUNK_MIN_CHARS`\s*(setting|env)", body), doc
