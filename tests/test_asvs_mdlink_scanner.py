"""The markdown-link scanner is the retired regexes, in linear time — #180.

Slice A bounded the lazy href scan at 2,048 characters, which made the
markdown-link regex linear. Linear was not enough: the *constant* was 2,048,
because every `](` re-scans up to 2 KiB looking for a `.md` that is not there.
Measured on the development host, 4.7 s for 512 KiB of `[a](` — ≈ 90 s for a
10 MiB note, from a body any authenticated tenant can write. And
`asyncio.to_thread` cannot help: CPython releases the GIL *between* `re`
steps, never inside one, and a scan that matches nothing is a single step, so
all 90 s run with the GIL held and every other tenant's request stops dead.

`scan_md_links` replaces both regexes with a hand-written scanner. This module
is the evidence that the replacement is exact:

1. **Both retired regexes are kept here as oracles.** They are the definition
   of correct; the scanner is the implementation. A future change to the
   grammar changes both, and this file is where the two are forced to agree.
2. **A differential property test** runs oracle and scanner over generated
   inputs covering every shape the grammar has a seam at, and compares match
   spans AND groups, not just "did something match".
3. **A ratio benchmark and an absolute ceiling.** The ceiling is a DoS bound,
   not a regression backstop: 1 MiB of `[a](` or of `[a](.md` must be parsed
   in under 200 ms. The retired regex took ~9.4 s on the first of those.

The scanner takes two keyword arguments, and they exist only to reproduce the
two PRE-EXISTING divergences between extraction and the `move_note` rewrite
(see `tests/test_asvs_link_grammar.py`, which records them as known gaps).
Reproducing them exactly — rather than quietly closing them — keeps slice A2 a
pure performance change with an empty behaviour delta on a destructive path.
"""
import random
import re
import time

import pytest

from src.services.links import MDLINK_HREF_MAX, scan_md_links

# ── the oracles ─────────────────────────────────────────────────────────────
#
# Verbatim `_MDLINK_RE` from `src/services/links.py` and `_MDLINK_REWRITE_RE`
# from `src/mcp_server/tools.py`, as they stood at the commit that retired
# them. Do not "tidy" these: their exact text is the specification.

ORACLE_EXTRACT = re.compile(
    r"\[(?P<text>[^\[\]\n]++)\]\("
    r"(?:<(?P<href_ab>[^>\n]{1,2048}?\.md)(?:#[^>]*+)?>"
    r"|(?P<href>[^)\n]{1,2048}?\.md)(?:#[^)\n]*+)?)\)"
)
ORACLE_REWRITE = re.compile(
    r"\[(?P<text>[^\[\]\n]++)\]\((?P<href>[^)\n]{1,2048}?\.md)(?P<anchor>#[^)]*+)?\)"
)

REWRITE_FLAGS = {"angle": False, "anchor_crosses_newlines": True}


def oracle_extract(text):
    return [
        (m.start(), m.end(), m.group("text"), m.group("href_ab") or m.group("href"))
        for m in ORACLE_EXTRACT.finditer(text)
    ]


def scanner_extract(text):
    return [(x.start, x.end, x.text, x.href) for x in scan_md_links(text)]


def oracle_rewrite(text):
    return [
        (m.start(), m.end(), m.group("text"), m.group("href"), m.group("anchor") or "")
        for m in ORACLE_REWRITE.finditer(text)
    ]


def scanner_rewrite(text):
    return [
        (x.start, x.end, x.text, x.href, x.anchor)
        for x in scan_md_links(text, **REWRITE_FLAGS)
    ]


def assert_agrees(text):
    """Both variants, spans and groups, on one input."""
    assert scanner_extract(text) == oracle_extract(text), f"extract: {text!r}"
    assert scanner_rewrite(text) == oracle_rewrite(text), f"rewrite: {text!r}"


# ── the enumerated seams ────────────────────────────────────────────────────
#
# Every shape where the grammar has a decision to make. Each is a regression
# test in its own right: the fuzz below would eventually generate most of
# them, but "eventually" is not a gate.

BOUND = MDLINK_HREF_MAX  # 2048

SEAMS = [
    # nothing at all
    "", "[", "[]", "[a]", "[a](", "[a]()", "text with no link",
    # the plain forms
    "[a](x.md)", "[a](x.md#sec)", "[a](folder/My Note.md)",
    "[a](./Sub/Note.md#Head)", "[a](x.md#)",
    # the angle-bracket form, with and without an anchor
    "[a](<x.md>)", "[a](<x.md#sec>)", "[a](<folder/My Note.md#a b>)",
    # an unterminated or malformed angle form falls back to the bare
    # alternative with `<` as the first href character
    "[a](<x.md)", "[a](<x.md>", "[a](<x.md", "[a](<>.md>)", "[a](<.md>)",
    "[a](>x.md)", "[a](<a.md>x)", "[a](<x.md#s)>",
    # `.md` inside an anchor, and `.md.md` — the lazy split picks the FIRST
    # `.md` that is followed by `#` or by the closing `)`
    "[a](x.md#a.md)", "[a](x.md.md)", "[a](x.md.md#s)", "[a](a.mdx.md)",
    "[a](<x.md#a.md>)", "[a](x.md#s1#s2)",
    # `)` and `[` inside the link text and the href
    "[a)b](x.md)", "[a[b](x.md)", "[a](Foo [draft].md)", "[a](x[b](y.md)",
    "[a](b.md)[c](d.md)", "[a](b.md](c.md)", "[a]((x.md)", "[a](x.md))",
    # empty and whitespace link text
    "[](x.md)", "[ ](x.md)", "[a\n](x.md)", "a](x.md)",
    # newlines in every position
    "[a](x.md#sec\nmore)", "[a](x.md\n)", "[a](x.md#s\n)", "[a](<x.md#s\n>)",
    "[a](<x.md#s\nmore>)", "[a\n](x.md)", "[a](\nx.md)", "[a](x.md)\n[b](y.md)",
    "[a](x.md#s)\n[b](y.md)",
    # CRLF
    "[a](x.md)\r\n[b](y.md)", "[a](x.md\r\n)", "[a](x.md#s\r\nmore)",
    "[a](<x.md#s\r\n>)",
    # runs of `[`
    "[[[Foo]]", "[[[a](x.md)", "[[[[[[", "[a]([b](x.md)",
    # adjacent and overlapping candidates
    "[a](](x.md)", "[a](b](c.md)", "](](](x.md)", "[a](x.md)[a](x.md)",
    # the 2,048-char href bound, from both sides
    "[a](" + "z" * (BOUND - 1) + ".md)",
    "[a](" + "z" * BOUND + ".md)",
    "[a](" + "z" * (BOUND + 1) + ".md)",
    "[a](" + "z" * (BOUND - 1) + ".md#s)",
    "[a](" + "z" * BOUND + ".md#s)",
    "[a](" + "z" * (BOUND + 1) + ".md#s)",
    "[a](<" + "z" * (BOUND - 1) + ".md>)",
    "[a](<" + "z" * BOUND + ".md>)",
    "[a](<" + "z" * (BOUND + 1) + ".md>)",
    # an over-long first candidate followed by an in-bound one
    "[a](" + "z" * (BOUND + 1) + ".md#" + "y.md)",
    # anchors long enough to matter (the anchor is NOT length-bounded)
    "[a](x.md#" + "s" * 5000 + ")",
    "[a](<x.md#" + "s" * 5000 + ">)",
]


@pytest.mark.parametrize("text", SEAMS, ids=range(len(SEAMS)))
def test_the_scanner_reproduces_both_oracles_at_every_seam(text):
    assert_agrees(text)


# ── the differential property test ──────────────────────────────────────────
#
# Random strings drawn from an alphabet of grammar-significant tokens, so the
# interesting shapes actually occur: a uniform alphabet over printable ASCII
# would produce a `.md` roughly never.

FUZZ_TOKENS = [
    "[a](", "[a](<", "[", "]", "(", ")", "<", ">", "#", "|", "!", "/", " ",
    "x", "note", "folder/", ".md", ".md#", ".md)", ".md>", "\n", "\r\n",
    "[[", "]]", "[t](x.md)", "[t](<x.md#s>)", "[a](x.md#", "z" * 40,
]

# Around the length bound, where an off-by-one is invisible to short inputs.
BOUNDARY_TOKENS = [
    "[a](", "<", ">", ")", "#", "\n", "x", ".md",
    "z" * (BOUND - 1), "z" * BOUND, "z" * (BOUND + 1),
]


@pytest.mark.parametrize("seed", range(8))
def test_the_scanner_and_the_oracles_agree_on_random_input(seed):
    rnd = random.Random(0x180 + seed)
    for _ in range(4000):
        text = "".join(rnd.choice(FUZZ_TOKENS) for _ in range(rnd.randint(1, 12)))
        assert_agrees(text)


@pytest.mark.parametrize("seed", range(4))
def test_the_scanner_and_the_oracles_agree_around_the_length_bound(seed):
    rnd = random.Random(0x2048 + seed)
    for _ in range(300):
        text = "".join(rnd.choice(BOUNDARY_TOKENS) for _ in range(rnd.randint(1, 6)))
        assert_agrees(text)


def test_the_scanner_and_the_oracles_agree_on_multi_line_documents():
    rnd = random.Random(0xD0C)
    for _ in range(4000):
        lines = [
            "".join(rnd.choice(FUZZ_TOKENS) for _ in range(rnd.randint(0, 5)))
            for _ in range(rnd.randint(1, 6))
        ]
        assert_agrees("\n".join(lines))


# ── the two divergences, reproduced rather than closed ──────────────────────


def test_the_rewrite_variant_does_not_see_the_angle_bracket_href():
    """Divergence 1, `angle=False`. `[a](<Old.md>)` is indexed but never
    rewritten — closing that changes what `move_note` mutates on disk."""
    text = "[a](<Old.md>)"
    assert scanner_extract(text) == [(0, 13, "a", "Old.md")]
    assert scanner_rewrite(text) == []


def test_the_rewrite_variant_anchor_crosses_newlines():
    """Divergence 2, `anchor_crosses_newlines=True`. Extraction's anchor class
    is `[^)\\n]`, the rewrite scanner's is `[^)]`."""
    text = "[a](Old.md#sec\nmore)"
    assert scanner_extract(text) == []
    assert scanner_rewrite(text) == [(0, 20, "a", "Old.md", "#sec\nmore")]


def test_the_scanner_reports_the_span_the_rewrite_splices():
    """`move_note` replaces `content[start:end]` wholesale, so a span that is
    one character off silently eats a bracket or leaves a stray `)`."""
    text = "see [label](folder/Old.md#Head) and more"
    (link,) = scan_md_links(text, **REWRITE_FLAGS)
    assert text[link.start:link.end] == "[label](folder/Old.md#Head)"
    assert (link.text, link.href, link.anchor) == ("label", "folder/Old.md", "#Head")
    assert text[:link.start] + f"[{link.text}](New.md{link.anchor})" + text[link.end:] == (
        "see [label](New.md#Head) and more"
    )


def test_a_match_never_starts_inside_the_previous_one():
    """The regexes' leftmost-non-overlapping rule. The `[b](` here lives
    inside the first match's href, so it is consumed, not matched."""
    text = "[a](x[b](y.md) [c](z.md)"
    assert scanner_extract(text) == [
        (0, 14, "a", "x[b](y.md"),
        (15, 24, "c", "z.md"),
    ]


# ── linearity, and the absolute ceiling ─────────────────────────────────────
#
# The ratio proves the shape; the ceiling is the actual availability bound.

BENCH_N = 512 * 1024
MIB = 1024 * 1024

# A DoS bound, not a regression backstop. The retired regex parsed 1 MiB of
# `[a](` in ~9.4 s; the scanner does it in under 1 ms because the `.md`
# tetragram prefilter ends the loop before any per-candidate work. 200 ms
# leaves two orders of magnitude of headroom for a loaded CI runner while
# still failing instantly on any return to the 2,048× constant.
BENCH_CEILING_SECONDS = 0.2

# The scanner's own worst case, which is NOT one of the shapes above: a body
# that is dense in BOTH candidates and `.md` tetragrams, so the prefilter
# cannot prune and every `](` costs one Python loop iteration. Measured
# ~200 ms per MiB on the development host — ~2 s for a 10 MiB note against
# ~90 s before. Documented and bounded rather than hidden.
DENSE_CEILING_SECONDS = 1.5

SCANNERS = {
    "extraction": lambda t: sum(1 for _ in scan_md_links(t)),
    "rewrite": lambda t: sum(1 for _ in scan_md_links(t, **REWRITE_FLAGS)),
}

PATHOLOGICAL_UNITS = ["[a](", "[a](.md", "[a](x", "[a]()", "[a](<", "[a](x.md#"]


def _repeat(unit, size):
    return (unit * (size // len(unit) + 1))[:size]


def _measure(fn, arg):
    """Best of five, stopping after a second — the same noise discipline as
    `tests/test_asvs_link_grammar.py`, for the same reason."""
    best = None
    spent = 0.0
    found = 0
    for _ in range(5):
        start = time.perf_counter()
        found = fn(arg)
        elapsed = time.perf_counter() - start
        best = elapsed if best is None else min(best, elapsed)
        spent += elapsed
        if spent >= 1.0:
            break
    return best, found


@pytest.mark.parametrize("unit", PATHOLOGICAL_UNITS)
@pytest.mark.parametrize("scanner", sorted(SCANNERS))
def test_pathological_input_is_scanned_in_linear_time(scanner, unit):
    fn = SCANNERS[scanner]
    small = _repeat(unit, BENCH_N)
    large = _repeat(unit, 2 * BENCH_N)

    fn(small[:4096])  # warm the interpreter, not the measurement
    t_small, found_small = _measure(fn, small)
    t_large, found_large = _measure(fn, large)

    # None of these is a link. A "fast" run that started finding millions of
    # them would be a different bug wearing this test's clothes.
    assert found_small == 0, f"{unit!r} produced {found_small} links"
    assert found_large == 0, f"{unit!r} produced {found_large} links"

    ratio = t_large / max(t_small, 1e-6)
    assert ratio < 4, (
        f"{scanner} on {unit!r}: ratio {ratio:.2f} "
        f"(n={t_small:.4f}s, 2n={t_large:.4f}s) — superlinear"
    )


@pytest.mark.parametrize("unit", ["[a](", "[a](.md"])
@pytest.mark.parametrize("scanner", sorted(SCANNERS))
def test_one_mebibyte_of_the_named_shapes_is_under_the_dos_bound(scanner, unit):
    """The two shapes the change contract names, at the size that matters."""
    fn = SCANNERS[scanner]
    text = _repeat(unit, MIB)
    fn(text[:4096])
    elapsed, found = _measure(fn, text)
    assert found == 0
    assert elapsed < BENCH_CEILING_SECONDS, (
        f"{scanner} on 1 MiB of {unit!r}: {elapsed:.3f}s"
    )


@pytest.mark.parametrize("scanner", sorted(SCANNERS))
def test_the_densest_shape_the_prefilter_cannot_prune_is_still_bounded(scanner):
    """Candidates every five bytes AND a `.md#` inside every href window, so
    neither the tetragram prefilter nor the monotone cursors can skip: the
    floor of one Python iteration per `](`. This is the scanner's true worst
    case and it is a documented number, not an unexamined one."""
    fn = SCANNERS[scanner]
    text = ("[a]()" * 410 + ".md#") * (MIB // 2054)
    assert len(text) > MIB // 2
    fn(text[:4096])
    elapsed, found = _measure(fn, text)
    assert found == 0
    assert elapsed < DENSE_CEILING_SECONDS, f"{scanner} on the dense shape: {elapsed:.3f}s"
