"""Wikilink and markdown-link extraction + resolution.

Two-pass parser:
1. `extract_links(content)` strips fenced/inline code blocks, then runs three
   regexes for `[[wikilinks]]`, `![[embeds]]`, and `[label](path.md)` markdown
   links. Returns one `ExtractedLink` per match.
2. `resolve_target(target, source_path, vault_index)` maps the raw target
   string to a note ID using Obsidian-style filename-first resolution with
   same-folder preference.

This module also owns the shared fence recognizer (`scan_fences`, `mask_code`)
every consumer that must ignore fenced code goes through — the `code-masking`
capability. Read the grammar comment before changing it.
"""

from __future__ import annotations

import os
import re
import urllib.parse
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal, NamedTuple


@dataclass(frozen=True)
class ExtractedLink:
    target: str  # target string with alias and anchor stripped
    link_text: str  # full original text (e.g. "[[Foo|Bar]]")
    kind: str  # "link" | "embed" | "markdown"
    position: int  # code-point offset in the (un-stripped) source


# ────────────────────────────────────────────────────────────────────────────
# The link grammar — every class is closed (issue #180)
# ────────────────────────────────────────────────────────────────────────────
#
# These two regexes, and the two rewrite regexes in `src/mcp_server/tools.py`
# that must apply the SAME rules, run synchronously on the one event loop this
# server has, over any note body a tenant can write (up to `MAX_NOTE_BYTES`).
# A character class that can swallow the rest of a line before the tail fails
# is therefore not a performance detail: it is a cross-tenant availability
# bug. 20 KB of `[[` held the loop for 18 seconds; `[[a#`, `[[a|`, `[a](` and
# `[a](x` were the same shape at 11.8 s, 4.9 s, 3.6 s and 2.4 s. Closing only
# some of the classes moves the burn to the next one, so ALL of them are
# closed, and the quantifiers are possessive so no class can be re-entered by
# backtracking:
#
# * WIKILINK target/anchor/alias exclude `[` and `]`. Obsidian's *link syntax*
#   forbids both inside `[[...]]`, so no well-formed wikilink changes.
# * MARKDOWN link text excludes `[` (it already excluded `]`).
# * MARKDOWN hrefs cannot exclude brackets — `[t](Foo [draft].md)` is a legal
#   link to a legal filename — so they are LENGTH-bounded instead:
#   `{1,2048}?`, which exceeds `MAX_PATH_CHARS` (1,024) plus any anchor. The
#   lazy scan is O(n × 2048) rather than O(n²).
#
# The accepted differences, each pinned in `tests/test_asvs_link_grammar.py`:
#
#   `[[Note|see [1]]]`, `[[Note#Sec [x]]]`   → no row at all (before: a row
#                                              with a mangled alias/anchor)
#   `[[[Foo]]`                               → target `Foo` (before: `[Foo`) —
#                                              the same rule seen from the
#                                              other side: the match now
#                                              starts at the second `[`
#   `[a[b](x.md)`                            → still a row to `x.md`, but
#                                              `link_text` is `[b](x.md)`
#   an href longer than 2,048 characters     → no row (it cannot name a note)
#
# Anything that changes here changes what `note_links` holds, so it needs a
# `CURRENT_EXTRACTION_VERSION` bump in the same change — the same rule the
# fence grammar below carries.

# Wikilink: optional `!` for embeds, then `[[Target(#Anchor)?(|Alias)?]]`.
# Target is "anything but [, ], |, #" so anchors and aliases peel cleanly.
_WIKILINK_RE = re.compile(
    r"(?P<embed>!)?\[\[(?P<target>[^\[\]\|#\n]++)"
    r"(?:#(?P<anchor>[^\[\]\|\n]*+))?"
    r"(?:\|(?P<alias>[^\[\]\n]*+))?\]\]"
)

# Markdown link: `[text](href.md)`, `[text](href.md#anchor)`, or the
# CommonMark angle-bracket form `[text](<href.md>)`. Href must end in `.md`
# (with optional `#anchor`) — we ignore non-note links here. The href class
# forbids only newlines (not all whitespace), so raw-space note names like
# `My Note.md` and `folder/My Note.md` are captured; it is length-bounded
# rather than bracket-free because brackets are legal in filenames.
#
# ## Why this is a hand-written scanner and not a regex (#180, slice A2)
#
# The regex form of this grammar was
#
#   \[(?P<text>[^\[\]\n]++)\]\(
#     (?:<(?P<href_ab>[^>\n]{1,2048}?\.md)(?:#[^>]*+)?>
#      |(?P<href>[^)\n]{1,2048}?\.md)(?:#[^)\n]*+)?)\)
#
# and it is *linear* — the `{1,2048}?` bound is what stopped the quadratic
# blow-up — but linear with a 2,048× constant: every `](` re-scans up to
# 2 KiB looking for a `.md` that is not there. Measured on the development
# host: 4.7 s for 512 KiB of `[a](`, so ≈ 90 s for a 10 MiB note, from a body
# any authenticated tenant can write.
#
# `asyncio.to_thread` does not rescue that. CPython releases the GIL *between*
# `re` steps, never inside one, and a scan that matches nothing is a single
# step — so the whole 90 s runs with the GIL held and every other tenant's
# request stops dead. Dispatching off the loop bounds the stall at the longest
# single scan; making that scan short is the actual fix.
#
# The scanner below reproduces the two regexes EXACTLY (the retired patterns
# are kept as oracles in `tests/test_asvs_mdlink_scanner.py` and fuzzed
# against it) while touching each character a bounded number of times:
#
# * candidates come from `_MDLINK_PREFIX_RE`, the unchanged `[text](` prefix,
#   which is possessive and therefore linear on its own;
# * every "where is the next X" question is answered by a MONOTONE cursor —
#   `str.find` from a low-water mark that only moves forward — so the sum of
#   all forward scans is O(len(content)), not O(len(content) × 2048);
# * a candidate can only match if a `.md#`, `.md)` or `.md>` tetragram starts
#   within `[p+1, p+2049]` of the href start, so `_MDLINK_TAIL_RE` prunes the
#   whole loop before any per-candidate work: 1 MiB of `[a](` or of `[a](.md`
#   contains none, and the scan ends after one C-level pass.
#
# The grammar it implements, derived from the regexes rather than described
# alongside them:
#
# * A candidate is a `](` whose nearest preceding `[`, with no `[`, `]` or
#   newline in between and at least one character of link text, is at or after
#   the previous match's end. That `[` is the match start.
# * BARE form. Let `q` be the first `)` or newline at or after the href start
#   `p`. The lazy `[^)\n]{1,2048}?\.md` picks the SMALLEST split of
#   `content[p:q]` into `H + ".md" + R` with `1 <= len(H) <= 2048` and `R`
#   empty or starting with `#`. So there are exactly two shapes to test and
#   the first wins: the earliest `.md#` at or after `p+1` (anchor case), else
#   a `.md` sitting immediately before `q` (bare case). Both need
#   `content[q] == ")"` — except for the anchor case under
#   `anchor_crosses_newlines`, below.
# * ANGLE form, tried first and only when `content[p] == "<"`. Same split
#   against `r`, the first `>` or newline after `p`; the closing `>` for an
#   anchored href is the first `>` after `p` wherever it is, because that
#   anchor class is `[^>]` and crosses newlines. A failed angle attempt falls
#   through to the bare form with `<` as the first character of the href,
#   exactly as the regex alternation did.
#
# ## The two parameters, and why they exist
#
# `move_note`'s rewrite grammar (`src/mcp_server/tools.py`) differs from
# extraction's in two PRE-EXISTING ways, recorded as known gaps in
# `tests/test_asvs_link_grammar.py`. They are reproduced here rather than
# closed, because closing either changes what `move_note` mutates on disk and
# that needs its own change and its own adversarial pass:
#
#   `angle=False`                  the rewrite grammar has no `<href>`
#                                  alternative, so `[a](<Old.md>)` is indexed
#                                  but never rewritten.
#   `anchor_crosses_newlines=True` the rewrite anchor class is `[^)]` where
#                                  extraction's is `[^)\n]`, so a `#anchor`
#                                  running past a line break is rewritable but
#                                  not extractable.
#
# Each costs one branch. Reproducing them exactly keeps slice A2 a pure
# performance change with a provably empty behaviour delta on the destructive
# path — worth more than the two branches cost.

# The href length bound. Exceeds `MAX_PATH_CHARS` (1,024) plus any anchor, so
# nothing that can name a note is excluded; see the accepted differences.
MDLINK_HREF_MAX = 2048

# The `[text](` prefix, unchanged from the retired regexes: link text excludes
# `[`, `]` and newline, is possessive, and must be non-empty.
_MDLINK_PREFIX_RE = re.compile(r"\[(?P<text>[^\[\]\n]++)\]\(")

# The necessary condition for ANY match: the href's terminating `.md` is
# always followed by `#`, `)` or `>`. Used only to prune candidates — every
# hit is then re-checked precisely.
_MDLINK_TAIL_RE = re.compile(r"\.md[#)>]")


class MdLinkMatch(NamedTuple):
    """One markdown link, in the shape the retired regexes reported it.

    `start`/`end` are the full match span (so `content[start:end]` is what
    `m.group(0)` was), `href` carries the trailing `.md` and NOT the angle
    brackets, and `anchor` is `"#..."` or `""`.

    `text_start`, `anchor_start` and `href_start` are where those three
    slices were taken from, so a caller that scanned a *masked* copy can
    re-slice the same bytes out of the unmasked original: `text` is
    `content[text_start:text_start + len(text)]`, and `anchor` and `href`
    likewise, each contiguous. Masking is a same-length substitution, so the
    offsets are valid against either string — `move_note`'s rewriter depends
    on exactly that (#211), both to write back the note's own bytes and to
    refuse to rewrite a link whose href span differs between the two, which
    means the mask decided a target the author never wrote. An empty `anchor`
    still carries the position it would have occupied. `href_start` points
    INSIDE the angle brackets for the angle form, matching `href`, which
    excludes them.
    """

    start: int
    end: int
    text: str
    href: str
    anchor: str
    angle: bool
    text_start: int
    anchor_start: int
    href_start: int


def scan_md_links(
    content: str,
    *,
    angle: bool = True,
    anchor_crosses_newlines: bool = False,
) -> Iterator[MdLinkMatch]:
    """Yield every `[text](href.md#anchor)` in `content`, left to right.

    Exactly the retired `_MDLINK_RE` (`angle=True`,
    `anchor_crosses_newlines=False`) or `_MDLINK_REWRITE_RE` (`angle=False`,
    `anchor_crosses_newlines=True`), in time linear in `len(content)` with a
    small constant. Matches never overlap: the scan resumes at the end of each
    one, which is what makes a `[` inside a matched href unable to start a
    second link — the regexes' leftmost-non-overlapping rule.

    Read the comment above before changing anything here; the retired regexes
    are kept as differential oracles in `tests/test_asvs_mdlink_scanner.py`.
    """
    n = len(content)
    find = content.find
    search_prefix = _MDLINK_PREFIX_RE.search
    search_tail = _MDLINK_TAIL_RE.search
    bound = MDLINK_HREF_MAX

    def _from(sub: str, start: int) -> int:
        i = find(sub, start)
        return n if i < 0 else i

    # Monotone cursors. Each holds "the first index at or after the low-water
    # mark it was last refreshed from", `n` meaning none. Every refresh moves
    # that mark forward, so the total scanning across the whole loop is one
    # pass per cursor — this is the whole reason the scanner is linear rather
    # than O(n × 2048).
    nl_at = _from("\n", 0)      # newline: ends the bare and angle segments
    rp_at = _from(")", 0)       # `)`: ditto, and the only way to close
    gt_at = _from(">", 0)       # `>`: the angle form's closer
    hash_at = _from(".md#", 0)  # the anchored-href split point
    close_at = rp_at            # `)` again, tracked from the anchor's start
    tail = search_tail(content)
    tail_at = tail.start() if tail else n

    pos = 0
    while True:
        m = search_prefix(content, pos)
        if m is None:
            return
        p = m.end()
        # A failed candidate resumes here, not at `m.start() + 1`: the link
        # text holds no `[`, and `]` and `(` cannot start one, so no match can
        # begin in between.
        pos = p

        if tail_at < p + 1:
            tail = search_tail(content, p + 1)
            tail_at = tail.start() if tail else n
        if tail_at >= n:
            return  # no `.md` tetragram left anywhere: nothing can match
        if tail_at > p + bound + 1:
            continue  # none within reach of this href start

        if nl_at < p:
            nl_at = _from("\n", p)
        if rp_at < p:
            rp_at = _from(")", p)
        if hash_at < p + 1:
            hash_at = _from(".md#", p + 1)

        q = rp_at if rp_at < nl_at else nl_at
        closes = rp_at < nl_at  # `content[q] == ")"`
        # Captured before the angle branch may advance the cursor past it: a
        # failed angle attempt falls through to the BARE form, whose split
        # point starts one character earlier.
        j = hash_at
        found = None

        if angle and p < n and content[p] == "<":
            if gt_at < p:
                gt_at = _from(">", p)
            r = gt_at if gt_at < nl_at else nl_at
            # The angle href starts at p+1, so its split point is at p+2 or
            # later. Advancing the shared cursor is safe for later candidates:
            # the next one asks from p'+1 >= p+2.
            ja = j
            if ja < p + 2:
                ja = hash_at = _from(".md#", p + 2)
            if (
                ja + 3 < r
                and ja - p - 1 <= bound
                and gt_at + 1 < n
                and content[gt_at + 1] == ")"
            ):
                # `[t](<href.md#anchor>)` — that anchor may cross newlines, so
                # the closing `>` is the first one anywhere after `p`.
                found = MdLinkMatch(
                    m.start(), gt_at + 2, m.group("text"),
                    content[p + 1:ja + 3], content[ja + 3:gt_at], True,
                    m.start() + 1, ja + 3, p + 1,
                )
            elif (
                gt_at < nl_at
                and gt_at + 1 < n
                and content[gt_at + 1] == ")"
                and gt_at - 3 >= p + 2
                and gt_at - p - 4 <= bound
                and content[gt_at - 3:gt_at] == ".md"
            ):
                # `[t](<href.md>)`
                found = MdLinkMatch(
                    m.start(), gt_at + 2, m.group("text"),
                    content[p + 1:gt_at], "", True,
                    m.start() + 1, gt_at, p + 1,
                )

        if found is None:
            if j < n and j + 3 < q and j - p <= bound:
                # `[t](href.md#anchor)` — the earliest `.md#` wins, and if it
                # cannot close then no later one can either (they share one
                # terminating `)`), so this is the only split to test.
                if anchor_crosses_newlines:
                    if close_at < j + 4:
                        close_at = _from(")", j + 4)
                    if close_at < n:
                        found = MdLinkMatch(
                            m.start(), close_at + 1, m.group("text"),
                            content[p:j + 3], content[j + 3:close_at], False,
                            m.start() + 1, j + 3, p,
                        )
                elif closes:
                    found = MdLinkMatch(
                        m.start(), q + 1, m.group("text"),
                        content[p:j + 3], content[j + 3:q], False,
                        m.start() + 1, j + 3, p,
                    )
            if (
                found is None
                and closes
                and q - 3 >= p + 1
                and q - p - 3 <= bound
                and content[q - 3:q] == ".md"
            ):
                # `[t](href.md)`
                found = MdLinkMatch(
                    m.start(), q + 1, m.group("text"), content[p:q], "", False,
                    m.start() + 1, q, p,
                )

        if found is not None:
            yield found
            pos = found.end

# ────────────────────────────────────────────────────────────────────────────
# The fence grammar (capability `code-masking`)
# ────────────────────────────────────────────────────────────────────────────
#
# This is the ONE definition of "what counts as fenced code" in the server.
# Heading resolution for section addressing (`_scan_headings`), wikilink and
# markdown-link extraction, inline tag extraction, `move_note` link rewriting
# and `clean_for_embedding` all consume the spans this scanner returns. No
# consumer may carry a private fence grammar: `clean_for_embedding` did, and
# the two grammars disagreed, so semantic search embedded code the masker hid.
#
# ## The grammar, a pinned CommonMark subset
#
# * OPENER — 0–3 leading U+0020 spaces, a run of ≥3 backticks or ≥3 tildes,
#   then an info string. A backtick fence's info string may NOT contain a
#   backtick (so a one-line ```` ```code``` ```` inline span never opens a
#   block); a tilde fence's is unrestricted.
# * CLOSER — 0–3 leading U+0020 spaces, a run of the SAME character at least
#   as long as the opener's, then nothing but U+0020 SPACE and U+0009 TAB. A
#   shorter run does not close; the other fence character does not close; an
#   NBSP after the run does not close.
# * SPAN — the first character of the opening fence line through the last
#   character of the closing fence line, **excluding the closing line's
#   terminator**. That exclusion is load-bearing, not cosmetic: masking is a
#   same-length substitution and `_ATX_HEADING_RE` matches only at a line
#   start, so swallowing the closer's terminator would leave an immediately
#   following heading with no line boundary in front of it and hide it from
#   the read AND write sides alike. Terminators *inside* the span are masked
#   like any other character.
# * UNTERMINATED, column zero — the block runs to the end of the note, because
#   a document is CommonMark's outermost container and it closes the block at
#   end of input.
# * UNTERMINATED, indented 1–3 spaces — **not a fence at all**. Flat scanning
#   cannot know the enclosing container's extent (the opener may be a list
#   item's child, whose block ends at the item's end), and fabricating an
#   end-of-note extent would let one stray line swallow every later section —
#   a new destructive class, worse than the one this grammar closes. The
#   opener is reported to callers instead, and the automatic-mutation paths
#   refuse: `edit_note(section=…)` and `move_note(rewrite_links=True)`. See
#   `docs/architecture/vault-tools.md`.
# * TERMINATORS — LF, CRLF as a unit, or a lone CR, the same universal-newline
#   rule `read_file` applies before the read path ever parses a note and the
#   same one the frontmatter partition and the ATX heading scan use.
# * FRONTMATTER — a valid line-1 block is opaque: no line inside it opens or
#   closes a fence. Without that, a fence-shaped YAML scalar (indent-matchable
#   since this change) swallows the whole body for the raw-text consumers.
#   The partition is computed AT MOST ONCE per note, against the full raw
#   text, so the recognizer takes its context explicitly — `FULL_NOTE`
#   discovers and skips the block, `BODY` never re-partitions. Auto-detecting
#   on an already-stripped body would eat a mapping-shaped body prefix as a
#   phantom second block and hide an unmatched opener from the refusal.
#
# ## Deliberate divergences from CommonMark
#
# * Container blocks (lists, blockquotes) are not parsed, so a MATCHED fence's
#   extent is computed flat even when its opener sits inside a list item.
# * 4+-space indented code blocks are not masked.
# * ATX headings stay column-zero only (`_ATX_HEADING_RE`).
# * Inline code masking is a single-line, backtick-delimited approximation of
#   CommonMark's equal-length-run pairing; it never crosses a terminator.
#
# A single `(?s)` regex cannot express "closer at least as long as the opener"
# or the indented-unterminated exclusion — the widened regex that tried is
# exactly what hid issue #150 — so this is a line scanner whose clauses can be
# audited one at a time.

FULL_NOTE = "full_note"
BODY = "body"
FenceContext = Literal["full_note", "body"]

_FENCE_CHARS = "`~"

# Line terminators, universal-newline order: CRLF ahead of the bare CR, or a
# terminator is split down the middle.
_LINE_BREAK_RE = re.compile(r"\r\n|\n|\r")


@dataclass(frozen=True)
class UnmatchedFenceOpener:
    """An indented (1–3 space) fence opener with no closer below it.

    Not a fence — see the grammar above — but the one shape whose extent the
    flat grammar cannot decide, so every automatic mutation refuses a note
    that contains one and names it from these fields.

    `position` is a code-point offset into the text that was scanned and
    `line` its 1-based line number there; `shifted` re-bases both onto the
    full note when the caller scanned a frontmatter-stripped body.
    """

    position: int
    line: int
    text: str

    def shifted(self, *, chars: int, lines: int) -> UnmatchedFenceOpener:
        return UnmatchedFenceOpener(
            position=self.position + chars,
            line=self.line + lines,
            text=self.text,
        )

    def describe(self) -> str:
        return f"line {self.line} (character {self.position}): {self.text!r}"


@dataclass(frozen=True)
class FenceScan:
    """Recognised fenced-code spans, plus what could not be decided.

    `spans` are half-open `(start, end)` code-point offsets into the scanned
    text, in document order and non-overlapping.
    """

    spans: tuple[tuple[int, int], ...]
    unmatched_indented_openers: tuple[UnmatchedFenceOpener, ...]


@dataclass(frozen=True)
class _Line:
    start: int        # offset of the line's first character
    content_end: int  # offset of the terminator, or of end-of-text
    end: int          # offset past the terminator, or end-of-text


def _split_lines(text: str, start: int) -> list[_Line]:
    """Split `text[start:]` into lines under the universal-newline rule."""
    lines: list[_Line] = []
    i = start
    n = len(text)
    while True:
        j = i
        while j < n and text[j] not in "\r\n":
            j += 1
        if j >= n:
            lines.append(_Line(start=i, content_end=n, end=n))
            return lines
        end = j + 2 if text[j] == "\r" and j + 1 < n and text[j + 1] == "\n" else j + 1
        lines.append(_Line(start=i, content_end=j, end=end))
        i = end


def _fence_run(text: str, line: _Line) -> tuple[str, int, int, int] | None:
    """`(char, run_length, indent, offset_after_run)` for a fence-shaped line.

    None when the line cannot be any fence line: 4+ spaces of indentation (an
    indented code block — a documented divergence, never masked), a leading
    tab, or fewer than three fence characters.
    """
    i = line.start
    indent = 0
    while i < line.content_end and text[i] == " ":
        indent += 1
        i += 1
    if indent > 3 or i >= line.content_end:
        return None
    char = text[i]
    if char not in _FENCE_CHARS:
        return None
    run = 0
    while i < line.content_end and text[i] == char:
        run += 1
        i += 1
    if run < 3:
        return None
    return char, run, indent, i


def _opens(text: str, line: _Line) -> tuple[str, int, int] | None:
    """`(char, run_length, indent)` if this line opens a block, else None."""
    found = _fence_run(text, line)
    if found is None:
        return None
    char, run, indent, after = found
    if char == "`" and "`" in text[after:line.content_end]:
        return None
    return char, run, indent


def _closes(text: str, line: _Line, char: str, length: int) -> bool:
    """Does this line close a block opened with `length` × `char`?"""
    found = _fence_run(text, line)
    if found is None:
        return False
    found_char, run, _indent, after = found
    if found_char != char or run < length:
        return False
    # Only U+0020 and U+0009 may follow the run. `\s` would admit an NBSP,
    # which CommonMark does not.
    return all(c in " \t" for c in text[after:line.content_end])


def _frontmatter_scan_start(text: str) -> int:
    """Offset of the first character after a valid line-1 frontmatter block.

    Zero when the block is absent or defective, in which case the whole raw
    text is scanned. Imported lazily: `vault` reaches back into this module
    for `mask_code`, and a module-level import would close the cycle.
    """
    from src.services.vault import parse_frontmatter_diagnose

    _fm, _body, diagnosis = parse_frontmatter_diagnose(text)
    return len(diagnosis.block) if diagnosis.valid else 0


def scan_fences(text: str, *, context: FenceContext) -> FenceScan:
    """Recognise fenced-code spans under the grammar documented above.

    `context` has no default on purpose: `FULL_NOTE` discovers and skips a
    valid line-1 frontmatter block, `BODY` scans from the first character and
    never re-partitions. `FULL_NOTE` on an already-stripped body eats a
    mapping-shaped prefix as a phantom second block; `BODY` on a raw note lets
    a fence-shaped YAML scalar swallow everything below it.
    """
    if context not in (FULL_NOTE, BODY):
        raise ValueError(
            f"scan_fences: context must be {FULL_NOTE!r} or {BODY!r} "
            f"(got {context!r})"
        )
    start = _frontmatter_scan_start(text) if context == FULL_NOTE else 0
    lines = _split_lines(text, start)

    spans: list[tuple[int, int]] = []
    unmatched: list[UnmatchedFenceOpener] = []
    # Memoised failure, per fence character: once no closer of `char` with a
    # run of at least L exists below some line, none exists below any LATER
    # line either. Without it a note of k unmatched indented openers rescans
    # to end of note k times.
    unclosable_at: dict[str, int] = {}

    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        opened = _opens(text, line)
        if opened is None:
            i += 1
            continue
        char, run, indent = opened

        closer = -1
        if run < unclosable_at.get(char, run + 1):
            for candidate in range(i + 1, n):
                if _closes(text, lines[candidate], char, run):
                    closer = candidate
                    break
        if closer >= 0:
            # Through the closing line's LAST CHARACTER, terminator excluded.
            spans.append((line.start, lines[closer].content_end))
            i = closer + 1
            continue

        unclosable_at[char] = min(unclosable_at.get(char, run), run)
        if indent == 0:
            spans.append((line.start, len(text)))
            break
        unmatched.append(
            UnmatchedFenceOpener(
                position=line.start,
                line=_line_number(text, line.start),
                text=text[line.start:line.content_end],
            )
        )
        i += 1

    return FenceScan(
        spans=tuple(spans), unmatched_indented_openers=tuple(unmatched)
    )


def _line_number(text: str, position: int) -> int:
    """1-based line number of `position` under the universal-newline rule."""
    return len(_LINE_BREAK_RE.findall(text, 0, position)) + 1


# Inline code: backtick-delimited runs that don't span a line, in any dialect.
_INLINE_CODE_RE = re.compile(r"`[^`\r\n]*`")


def _mask_code(text: str, *, context: FenceContext) -> str:
    """Replace fenced/inline code with same-length whitespace.

    Preserves offsets so `position` values in `ExtractedLink` are valid
    against the original content and `_scan_headings` can report positions
    into the unmasked text. Every substitution is exactly as long as what it
    replaces, stated in **code points** — Python `str` offsets are what every
    consumer stores and reports, so non-ASCII content is covered by the same
    invariant.
    """
    return apply_fence_mask(text, scan_fences(text, context=context))


def apply_fence_mask(text: str, scan: FenceScan) -> str:
    """`mask_code`'s body, for a caller that already has the scan.

    A consumer that needs both the unmatched-opener report and the masked text
    — `move_note`'s rewrite preflight — would otherwise scan each source twice,
    and the second scan would re-run the frontmatter partition the recognizer
    promises to run at most once per note. `scan` MUST have been produced from
    this exact `text`, in the context that consumer declares.
    """
    if scan.spans:
        out: list[str] = []
        cursor = 0
        for start, end in scan.spans:
            out.append(text[cursor:start])
            out.append(" " * (end - start))
            cursor = end
        out.append(text[cursor:])
        text = "".join(out)

    def _spaces(match: re.Match) -> str:
        return " " * (match.end() - match.start())

    return _INLINE_CODE_RE.sub(_spaces, text)


def extract_links(
    content: str, *, context: FenceContext = BODY, max_links: int | None = None
) -> list[ExtractedLink]:
    """Extract every wikilink/embed/markdown-link from a note body.

    `context` defaults to `BODY` because every caller hands this the
    post-frontmatter body the indexer already parsed. A caller with raw note
    text must say `context=FULL_NOTE` so the frontmatter block stays opaque to
    fence recognition.

    `max_links` bounds the result to the first N links in DOCUMENT order (see
    `extract_links_bounded`, which also reports whether the bound bit).
    `None` — the default, and what every pre-existing caller passes — is
    unbounded.
    """
    links, _ = extract_links_bounded(content, context=context, max_links=max_links)
    return links


def extract_links_bounded(
    content: str, *, context: FenceContext = BODY, max_links: int | None = None
) -> tuple[list[ExtractedLink], bool]:
    """`extract_links`, plus whether `max_links` truncated the result.

    Returns `(links, truncated)`. `truncated` is True only when the note
    genuinely holds more links than `max_links`; a caller that persists a
    truncated set must record that fact durably, because a capped set read as
    a complete one is a silently-wrong graph answer.

    **Document order, not scan order.** Extraction runs two sequential loops
    (wikilinks, then markdown links), so "the first N" is meaningless per
    loop: a note with 20,000 wikilinks would otherwise lose every markdown
    link in the file. The two loops are merged by `position` and cut to N
    against that merged order. Each loop is itself capped at N first — a link
    in the merged first N is necessarily within the first N of its own kind —
    so peak memory is bounded at 2N links rather than at the note's true link
    count (one 10 MiB note of `[[a]] ` yields 1.75 M links unbounded).
    """
    masked = _mask_code(content, context=context)
    wiki: list[ExtractedLink] = []
    md: list[ExtractedLink] = []
    overflowed = False

    for m in _WIKILINK_RE.finditer(masked):
        # Compare the raw deciding span BEFORE strip/cap accounting (#218).
        # Masking `x` in [[`x`Old]] must not invent a link to Old; changes
        # confined to the alias or anchor do not decide the target.
        start, end = m.span("target")
        if content[start:end] != masked[start:end]:
            continue
        target = m.group("target").strip()
        if not target:
            continue
        if max_links is not None and len(wiki) >= max_links:
            overflowed = True
            break
        kind = "embed" if m.group("embed") else "link"
        wiki.append(ExtractedLink(
            target=target,
            link_text=m.group(0),
            kind=kind,
            position=m.start(),
        ))

    for link in scan_md_links(masked):
        # href_start excludes angle brackets and uses the same code-point
        # offsets as the original string. Labels and anchors stay outside
        # this comparison; rejected candidates cannot cause truncation.
        end = link.href_start + len(link.href)
        if content[link.href_start:end] != link.href:
            continue
        href = link.href.strip()
        if not href:
            continue
        if max_links is not None and len(md) >= max_links:
            overflowed = True
            break
        # Decode percent-encoded characters (e.g. `%20` → space).
        try:
            decoded = urllib.parse.unquote(href)
        except Exception:
            decoded = href
        # Strip a trailing `.md` for resolver consistency — resolver tries
        # both with and without the extension.
        target = decoded[:-3] if decoded.endswith(".md") else decoded
        md.append(ExtractedLink(
            target=target,
            link_text=masked[link.start:link.end],
            kind="markdown",
            position=link.start,
        ))

    if max_links is None:
        # Unbounded: the historical scan order (every wikilink, then every
        # markdown link). Callers that predate the cap depend on it, and with
        # no cut to make there is no selection for document order to inform.
        return wiki + md, False

    if not md:
        out = wiki
    elif not wiki:
        out = md
    else:
        out = sorted(wiki + md, key=lambda link: link.position)
    truncated = overflowed or len(out) > max_links
    return out[:max_links], truncated


# ────────────────────────────────────────────────────────────────────────────
# Resolution
# ────────────────────────────────────────────────────────────────────────────


def _normalize(target: str) -> str:
    """Strip alias/anchor fragments. Wikilinks already strip them at extraction
    time; markdown-link decoded paths might contain `#anchor` if they were
    embedded oddly. Be defensive."""
    target = target.strip()
    if "|" in target:
        target = target.split("|", 1)[0]
    if "#" in target:
        target = target.split("#", 1)[0]
    return target.strip()


def _source_dir(source_path: str) -> str:
    return os.path.dirname(source_path)


def resolve_target(
    target: str,
    source_path: str,
    vault_index: dict,
) -> int | None:
    """Resolve a raw link target to a `notes_metadata.id`.

    `vault_index` is expected to carry two sub-dicts:
      - `vault_index["paths"]`: dict[file_path, id]
      - `vault_index["stems"]`: dict[stem, list[(file_path, id)]]

    Resolution order (mirrors Obsidian defaults):
      1. Path-style: target contains `/` → try `<target>.md`, then `<target>`.
      2. Same-folder: `<source_dir>/<target>.md`.
      3. Bare-name unique: exactly one note in the vault has stem `<target>`.
      4. Bare-name ambiguous: pick the alphabetically first match.
      5. Fall through: return None (dangling).
    """
    name = _normalize(target)
    if not name:
        return None

    paths: dict[str, int] = vault_index.get("paths", {})
    stems: dict[str, list[tuple[str, int]]] = vault_index.get("stems", {})

    # Strip a trailing `.md` so the rest of the resolver treats `![[Foo.md]]`
    # the same as `![[Foo]]`. The path-style branch re-adds it as needed.
    has_md = name.endswith(".md")
    name_no_ext = name[:-3] if has_md else name

    # Path-style attempt — fires whenever the target contains a slash OR
    # already carries a `.md` extension. This catches `[[Folder/Foo]]` and
    # `[label](Folder/Foo.md)` (the .md was stripped by the extractor) alike.
    if "/" in name_no_ext or has_md:
        # Normalize `./` and `../` against the source's folder so markdown
        # links like `[label](./Foo.md)` resolve correctly.
        if name_no_ext.startswith("./") or name_no_ext.startswith("../"):
            base = _source_dir(source_path)
            normalized = (
                os.path.normpath(os.path.join(base, name_no_ext))
                if base else os.path.normpath(name_no_ext)
            )
            normalized = normalized.replace(os.sep, "/")
        else:
            normalized = name_no_ext
        candidate_md = f"{normalized}.md"
        if candidate_md in paths:
            return paths[candidate_md]
        if normalized in paths:
            return paths[normalized]

    # Same-folder bias.
    src_dir = _source_dir(source_path)
    if src_dir:
        local = f"{src_dir}/{name_no_ext}.md"
        if local in paths:
            return paths[local]

    # Bare-name lookup by stem.
    stem_key = os.path.basename(name_no_ext)
    candidates = stems.get(stem_key, [])
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0][1]
    # Multiple — prefer same folder, else alphabetical.
    same_folder = [c for c in candidates if os.path.dirname(c[0]) == src_dir]
    if same_folder:
        same_folder.sort(key=lambda c: c[0])
        return same_folder[0][1]
    candidates_sorted = sorted(candidates, key=lambda c: c[0])
    return candidates_sorted[0][1]


def build_vault_index(rows) -> dict:
    """Build a `vault_index` dict from an iterable of `(file_path, id)` tuples."""
    paths: dict[str, int] = {}
    stems: dict[str, list[tuple[str, int]]] = {}
    for file_path, note_id in rows:
        paths[file_path] = note_id
        stem = os.path.splitext(os.path.basename(file_path))[0]
        stems.setdefault(stem, []).append((file_path, note_id))
    return {"paths": paths, "stems": stems}


def normalize_target(target: str) -> str:
    """Public wrapper for the alias/anchor-stripping helper."""
    return _normalize(target)


def mask_code(text: str, *, context: FenceContext) -> str:
    """Public wrapper around `_mask_code`.

    Replaces fenced and inline code blocks with whitespace of equal code-point
    length, for downstream scanners (heading parsers, link and tag extractors)
    that must avoid false positives inside code. `context` says whether `text`
    is a whole note (`FULL_NOTE`) or an already-stripped body (`BODY`) — see
    `scan_fences`.
    """
    return _mask_code(text, context=context)


def unmatched_indented_openers(
    text: str, *, context: FenceContext
) -> tuple[UnmatchedFenceOpener, ...]:
    """The indented fence openers in `text` that no line below them closes.

    Empty for every note whose fences the flat grammar can decide. A non-empty
    result is what the write paths refuse on.
    """
    return scan_fences(text, context=context).unmatched_indented_openers
