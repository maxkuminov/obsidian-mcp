"""The link grammar is linear, bounded, and off the loop — issue #180, ASVS.

Four regexes parse links: `_WIKILINK_RE` / `_MDLINK_RE` in
`src/services/links.py` (extraction, run by the indexer) and
`_WIKILINK_REWRITE_RE` / `_MDLINK_REWRITE_RE` in `src/mcp_server/tools.py`
(the `move_note` rewrite scanner). All four ran on the single event loop with
character classes that could swallow the rest of a line before the tail
failed, which made ordinary in-cap input — 20 KB of `[[` — an 18-second stall
for every other tenant on the server.

This module pins the three things that fix has to be:

1. **Linear** — a ratio benchmark (2n ÷ n < 4) over every pathological shape,
   not a single wall-clock assertion, because a shared CI runner will flake
   one of those sooner or later.
2. **Behaviour-preserving except where enumerated** — the accepted
   differences are listed once, here, and asserted exactly.
3. **Agreed between the two grammars** — with the two PRE-EXISTING
   divergences recorded as known gaps rather than silently inherited.
"""
import asyncio
import time

import pytest

import src.mcp_server.tools as tools
from src.auth.session import current_user_id
from src.mcp_server.auth import current_permission
from src.services import vault as vault_service
from src.services.links import (
    _MDLINK_RE,
    _WIKILINK_RE,
    build_vault_index,
    extract_links,
    extract_links_bounded,
)

# ── the seven shapes, and the two entry points ──────────────────────────────
#
# Each is a run where a class consumed the rest of the line and the tail then
# failed. Measured before the fix at 40 KB: `[[` 18 s, `[[a#` 11.8 s, `[[a|`
# 4.9 s, `[a](` 3.6 s, `[a](x` 2.4 s.
PATHOLOGICAL_UNITS = ["[[", "]]", "[[a", "[[a#", "[[a|", "[a](", "[a](x"]

BENCH_N = 512 * 1024
# Generous by design. The markdown href cannot exclude brackets (they are legal
# in filenames), so it is LENGTH-bounded at 2,048 instead — O(n × 2048) rather
# than O(n²). That constant is real: ~4.7 s for 512 KiB of `[a](` on the
# development host. The ratio is what proves linearity; this is only a
# backstop against a regression to quadratic.
BENCH_CEILING_SECONDS = 60.0


def _repeat(unit: str, size: int) -> str:
    return (unit * (size // len(unit) + 1))[:size]


def _rewrite_index():
    """The smallest `pre_move_index` that makes the rewrite scanner run its
    regexes rather than return early on a missing `from_rel`."""
    return build_vault_index([("Old.md", 1), ("New.md", 2)])


def _run_extraction(text: str) -> int:
    return len(extract_links(text))


def _run_rewrite(text: str) -> int:
    _, n = tools._rewrite_links_in_text(
        text, "Old.md", "New.md", "src.md", _rewrite_index()
    )
    return n


SCANNERS = {"extract_links": _run_extraction, "rewrite_scanner": _run_rewrite}


def _measure(fn, arg):
    """Best of up to five runs, stopping once a second has been spent.

    A single wall-clock sample of a 50 ms run on a shared runner is mostly
    scheduler noise — three-times spreads between the fastest and slowest of
    five identical runs are routine here — and that noise is exactly what
    turns a ratio assertion into a flaky test. Taking the minimum measures it
    away instead of papering over it with a looser bound. The one-second
    budget matters as much as the count: with a smaller budget the 2n size
    gets one noisy sample where n gets five, and the *asymmetry* alone can
    manufacture a ratio above 4. The expensive shapes exceed the budget on
    their first run and are sampled once, which is fine — they are seconds
    long, well clear of the noise floor."""
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
def test_pathological_input_is_parsed_in_linear_time(scanner, unit):
    fn = SCANNERS[scanner]
    small = _repeat(unit, BENCH_N)
    large = _repeat(unit, 2 * BENCH_N)

    fn(small[:4096])  # warm the interpreter, not the measurement
    t_small, found_small = _measure(fn, small)
    t_large, found_large = _measure(fn, large)

    # None of these shapes is a link. A "fast" run that started finding
    # millions of them would be a different bug wearing this test's clothes.
    assert found_small == 0, f"{unit!r} produced {found_small} links"
    assert found_large == 0, f"{unit!r} produced {found_large} links"

    assert t_large < BENCH_CEILING_SECONDS, (
        f"{scanner} on {unit!r}: {t_large:.2f}s at {2 * BENCH_N} bytes"
    )
    ratio = t_large / max(t_small, 1e-6)
    assert ratio < 4, (
        f"{scanner} on {unit!r}: ratio {ratio:.2f} "
        f"(n={t_small:.4f}s, 2n={t_large:.4f}s) — superlinear"
    )


# ── the accepted differences, enumerated ────────────────────────────────────


def _targets(text):
    return [(link.kind, link.target, link.link_text) for link in extract_links(text)]


def test_a_bracket_inside_an_alias_is_no_longer_a_link():
    """Was: a row whose alias was mangled at the first `]`. Now: no row.

    `[` and `]` are forbidden by Obsidian's own wikilink syntax, so nothing
    well-formed is lost — and leaving the class open is what made 20 KB of
    `[[a|` a 4.9-second stall."""
    assert _targets("[[Note|see [1]]]") == []


def test_a_bracket_inside_an_anchor_is_no_longer_a_link():
    assert _targets("[[Note#Sec [x]]]") == []


def test_a_stray_leading_bracket_now_starts_the_match_one_character_later():
    """The same rule seen from the other side, and the only *incidental*
    difference: `[[[Foo]]` used to yield the target `[Foo` (a note name that
    cannot exist). The match now begins at the second `[`, so the target is
    `Foo`."""
    assert _targets("[[[Foo]]") == [("link", "Foo", "[[Foo]]")]


def test_a_bracket_in_markdown_link_text_shortens_link_text():
    """Still a link to `x.md` — but `link_text`, which is the field
    `get_links` returns and `move_note` splices on, is now the inner form."""
    assert _targets("[a[b](x.md)") == [("markdown", "x", "[b](x.md)")]


def test_an_href_longer_than_the_bound_is_not_extracted():
    """It cannot name a note: `MAX_PATH_CHARS` is 1,024."""
    assert _targets("[t](" + "a" * 2100 + ".md)") == []


def test_an_href_within_the_bound_is_extracted():
    href = "a" * 2040
    assert _targets(f"[t]({href}.md)") == [("markdown", href, f"[t]({href}.md)")]


def test_brackets_in_a_filename_still_extract():
    """The href is length-bounded rather than bracket-free precisely because
    `Foo [draft].md` is a legal file name."""
    assert _targets("[t](Foo [draft].md)") == [
        ("markdown", "Foo [draft]", "[t](Foo [draft].md)")
    ]


UNCHANGED_FORMS = [
    ("[[Note]]", [("link", "Note", "[[Note]]")]),
    ("[[Folder/Other Note|alias]]", [("link", "Folder/Other Note", "[[Folder/Other Note|alias]]")]),
    ("[[Note#Section]]", [("link", "Note", "[[Note#Section]]")]),
    ("[[Note#Section|alias]]", [("link", "Note", "[[Note#Section|alias]]")]),
    ("![[Diagram.md]]", [("embed", "Diagram.md", "![[Diagram.md]]")]),
    ("[See also](./Subfolder/Note.md)", [("markdown", "./Subfolder/Note", "[See also](./Subfolder/Note.md)")]),
    ("[a](My Note.md)", [("markdown", "My Note", "[a](My Note.md)")]),
    ("[a](folder/My Note.md#anchor)", [("markdown", "folder/My Note", "[a](folder/My Note.md#anchor)")]),
    ("[a](<My Note.md>)", [("markdown", "My Note", "[a](<My Note.md>)")]),
    ("[a](x.md.md)", [("markdown", "x.md", "[a](x.md.md)")]),
]


@pytest.mark.parametrize("text,expected", UNCHANGED_FORMS)
def test_well_formed_links_are_untouched_by_the_closed_classes(text, expected):
    assert _targets(text) == expected


# ── extraction and rewrite agree ────────────────────────────────────────────

AGREEMENT_CORPUS = [
    "[[Old]]",
    "![[Old]]",
    "[[Old|alias]]",
    "[[Old#Sec]]",
    "[[Old#Sec|alias]]",
    "[[Folder/Old.md]]",
    "[[Old|see [1]]]",
    "[[Old#Sec [x]]]",
    "[[[Old]]",
    "[[]]",
    "[[ ]]",
    "[a](Old.md)",
    "[a](Old.md#anchor)",
    "[a](folder/My Note.md)",
    "[a](Foo [draft].md)",
    "[a[b](Old.md)",
    "[a](" + "z" * 2100 + ".md)",
    "[a](" + "z" * 2040 + ".md)",
    "[a](Old.txt)",
    "[a]()",
    "text with no link at all",
]


@pytest.mark.parametrize("text", AGREEMENT_CORPUS)
def test_extraction_and_rewrite_grammars_accept_the_same_members(text):
    """One line at a time, bare-href form only — the two known gaps below are
    exactly the cases this corpus must avoid."""
    extracted = bool(_WIKILINK_RE.search(text) or _MDLINK_RE.search(text))
    rewritten = bool(
        tools._WIKILINK_REWRITE_RE.search(text)
        or tools._MDLINK_REWRITE_RE.search(text)
    )
    assert extracted == rewritten, (
        f"{text!r}: extraction={extracted} rewrite={rewritten}"
    )


def test_known_gap_the_rewrite_scanner_does_not_see_the_angle_bracket_href():
    """PRE-EXISTING, and deliberately not closed here: `_MDLINK_RE` has a
    CommonMark `<href>` alternative, `_MDLINK_REWRITE_RE` has none. So
    `[a](<Old.md>)` is indexed as a link but `move_note` never rewrites it.
    Closing it changes what `move_note` mutates, which needs its own change
    and its own adversarial pass."""
    text = "[a](<Old.md>)"
    assert _MDLINK_RE.search(text) is not None
    assert tools._MDLINK_REWRITE_RE.search(text) is None


def test_known_gap_the_rewrite_anchor_class_crosses_newlines():
    """PRE-EXISTING: extraction's anchor class is `[^)\\n]`, the rewrite
    scanner's is `[^)]`. A `#anchor` that runs past a line break is therefore
    rewritable but not extractable. Also untouched by this change."""
    text = "[a](Old.md#sec\nmore)"
    assert _MDLINK_RE.search(text) is None
    assert tools._MDLINK_REWRITE_RE.search(text) is not None


# ── the per-note cap, in document order ─────────────────────────────────────


def test_the_cap_selects_the_first_n_links_in_document_order():
    """Not the first N wikilinks then the first N markdown links: extraction
    runs two sequential loops, so a note with thousands of wikilinks would
    otherwise lose every markdown link in the file."""
    body = "".join(f"[[W{i}]] [m{i}](M{i}.md)\n" for i in range(50))

    links, truncated = extract_links_bounded(body, max_links=10)

    assert truncated is True
    assert len(links) == 10
    assert [link.position for link in links] == sorted(
        link.position for link in links
    )
    # Five of each — they alternate in the document.
    assert [link.kind for link in links].count("link") == 5
    assert [link.kind for link in links].count("markdown") == 5
    assert [link.target for link in links][:4] == ["W0", "M0", "W1", "M1"]


def test_a_note_under_the_cap_is_not_reported_as_truncated():
    body = "".join(f"[[W{i}]]\n" for i in range(10))
    links, truncated = extract_links_bounded(body, max_links=10)
    assert truncated is False
    assert len(links) == 10


def test_the_cap_reports_truncation_when_one_kind_alone_overflows():
    body = "".join(f"[[W{i}]]\n" for i in range(25))
    links, truncated = extract_links_bounded(body, max_links=10)
    assert truncated is True
    assert [link.target for link in links] == [f"W{i}" for i in range(10)]


def test_an_uncapped_call_keeps_the_historical_scan_order():
    """Every pre-existing caller passes no cap and depends on this."""
    body = "[m](M.md) [[W]]"
    assert [link.kind for link in extract_links(body)] == ["link", "markdown"]
    assert extract_links_bounded(body) == (extract_links(body), False)


def test_the_cap_bounds_peak_memory_not_just_the_result():
    """Two loops, each cut at N first: a link in the merged first N is
    necessarily within the first N of its own kind, so the intermediate lists
    never grow past 2N even for a note with 1.75 M links."""
    body = "[[a]] " * 60_000
    links, truncated = extract_links_bounded(body, max_links=100)
    assert truncated is True
    assert len(links) == 100


# ── the dispatch off the event loop ─────────────────────────────────────────


class _Row:
    def __init__(self, **fields):
        self.__dict__.update(fields)


def _fake_session(*result_rows):
    class Result:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return self._rows

    calls = {"n": 0}

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


async def test_move_note_dispatches_the_rewrite_through_to_thread(
    rewrite_vault, monkeypatch
):
    """The rewrite is a pure function of a string, and a hub note's backlink
    sources are read one after another — linear work on near-cap notes is
    still dead air for every other tenant if it runs on the loop."""
    (rewrite_vault / "Old.md").write_text("moved\n", encoding="utf-8")
    (rewrite_vault / "src.md").write_text("see [[Old]]\n", encoding="utf-8")
    monkeypatch.setattr(
        tools,
        "async_session",
        _fake_session(
            [_Row(file_path="Old.md", id=1), _Row(file_path="src.md", id=2)],
            [_Row(file_path="src.md")],
        ),
    )

    dispatched = []
    real_to_thread = asyncio.to_thread

    async def spy(fn, /, *args, **kwargs):
        dispatched.append(getattr(fn, "__name__", repr(fn)))
        return await real_to_thread(fn, *args, **kwargs)

    monkeypatch.setattr(tools.asyncio, "to_thread", spy)

    result = await tools.move_note_impl("Old.md", "New.md", rewrite_links=True)

    assert "Moved Old.md → New.md" in result, result
    assert "_rewrite_links_in_text" in dispatched, dispatched
    assert (rewrite_vault / "src.md").read_text(encoding="utf-8") == "see [[New]]\n"
