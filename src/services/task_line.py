"""The pure task-line parser and minimal one-line editor (#428, design D1, D4,
D5, D6, D8, D9).

Everything here is a pure function of its arguments: no I/O, no database, no
settings, no clock. The write tools (`task_write`) and the extractor
(`tasks`) build on it; it decides which bytes of a user's note change, so its
contract is byte-exactness.

## Line anatomy (D4)

A task line ``L`` (the content between line start and its terminator; a line
never contains CR or LF here) is::

    prefix  = blockquote/indent + list marker + whitespace   (up to "[")
    box     = "[" status "]"
    gap     = [ \\t]*                                        (after "]")
    text    = the rest, through the last non-whitespace character
    tail_ws = trailing whitespace (a hard break of >= 2 spaces included)

A line is a task exactly when the extractor (`src/services/tasks.py`) says so:
the **masked** line matches the checkbox grammar at its start and the
original equals the masked text through the closing ``]`` (the #218 rule).
``text`` is the extractor's uncut ``full_text``: the original after ``]`` and
its following ``[ \\t]*``, ``str.rstrip()``-ed.

## The trailing run (R§1.4)

Inside ``text`` the **trailing run** is found exactly as the Tasks plugin
parses it (8.6.0, ``DefaultTaskSerializer.deserialize``), on the original
text:

1. a block link `` \\^[a-zA-Z0-9-]+$`` is stripped first;
2. then, for at most 21 passes, each end-anchored pattern is tried once per
   pass in the plugin's order — priority, done, cancelled, due, scheduled,
   start, created, recurrence, onCompletion, trailing ``#tag``, id,
   dependsOn — removing its match and trimming; the loop stops after a pass
   that matched nothing.

Each field pattern is ``symbol + U+FE0F? + ' *' + value + '$'``. What remains
is the **description region**; the removed suffix is the run, a sequence of
tokens with exact spans.

## Refusal preconditions (D4 (a)–(d), ``task_malformed``)

For every field family a call touches (and every family named in
``LineChanges.strict_families``):

* (b) the family's signifier occurs more than once in the masked text;
* (c) it occurs once, but not as a run token (a *stranded* signifier — the
  plugin would not parse it and the extractor would, so the server will not
  pick);
* (d) a date field the call changes (or a strict family) holds a value that
  is not a calendar date;

and, when the call adds, changes or removes any field, (a) the masked and the
original text differ inside the run (inline code there).

## Operations (D4)

All operations are computed together against the original line and returned
as non-overlapping `Edit`s in original-line coordinates; every byte outside
the edits is untouched by construction. Changing a value replaces only the
value characters; removing deletes the token plus one adjacent separating
space (the preceding one, else the following one, never the gap or the
trailing whitespace); adding inserts ``" " + symbol + " " + value`` at the
canonical-rank slot; tags are added at the end of the description region and
removed whole-tag, case-insensitively, never hierarchically; replacing the
description keeps the old description's tags (re-appended in original order
and spelling unless the new text already has them). After composing, the new
line is re-parsed and checked against the intended result; any mismatch is a
``task_malformed`` refusal, never a write.

This module never re-serialises an existing line in the plugin's canonical
order (supervisor ruling 1). Only a brand-new line (`serialize_new_task`, D8)
is written canonically.
"""
from __future__ import annotations

import datetime
import hashlib
import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from src.services.links import BODY, mask_code
from src.services.task_recurrence import Rule, parse_rule

# ── constants ────────────────────────────────────────────────────────────────

#: Field families, in the plugin's canonical serialisation order (R§1.3).
ID = "id"
DEPENDS_ON = "depends_on"
PRIORITY = "priority"
RECURRENCE = "recurrence"
ON_COMPLETION = "on_completion"
CREATED = "created"
START = "start"
SCHEDULED = "scheduled"
DUE = "due"
CANCELLED = "cancelled"
DONE = "done"

#: ``C`` of D4: the canonical rank of each family.
CANONICAL_ORDER = (
    ID, DEPENDS_ON, PRIORITY, RECURRENCE, ON_COMPLETION,
    CREATED, START, SCHEDULED, DUE, CANCELLED, DONE,
)
CANONICAL_RANK = {family: rank for rank, family in enumerate(CANONICAL_ORDER)}
FAMILIES = frozenset(CANONICAL_ORDER)
DATE_FAMILIES = frozenset({CREATED, START, SCHEDULED, DUE, CANCELLED, DONE})

#: Every code point the extractor or the plugin reads as a family's signifier
#: (R§1.2, plus the variants `tasks.py` accepts).
READ_SYMBOLS: dict[str, frozenset[str]] = {
    DUE: frozenset({"\U0001F4C5", "\U0001F4C6", "\U0001F5D3"}),  # 📅 📆 🗓
    SCHEDULED: frozenset({"⏳", "⌛"}),  # ⏳ ⌛
    START: frozenset({"\U0001F6EB"}),  # 🛫
    CREATED: frozenset({"➕"}),  # ➕
    DONE: frozenset({"✅"}),  # ✅
    CANCELLED: frozenset({"❌"}),  # ❌
    RECURRENCE: frozenset({"\U0001F501"}),  # 🔁
    ON_COMPLETION: frozenset({"\U0001F3C1"}),  # 🏁
    ID: frozenset({"\U0001F194"}),  # 🆔
    DEPENDS_ON: frozenset({"⛔"}),  # ⛔
    PRIORITY: frozenset({"\U0001F53A", "⏫", "\U0001F53C", "\U0001F53D", "⏬"}),
}
ALL_SIGNIFIERS = frozenset().union(*READ_SYMBOLS.values())
_SYMBOL_FAMILY = {sym: fam for fam, syms in READ_SYMBOLS.items() for sym in syms}

#: The plugin's write symbols (R§1.2, "Written as").
WRITE_SYMBOLS = {
    DUE: "\U0001F4C5",
    SCHEDULED: "⏳",
    START: "\U0001F6EB",
    CREATED: "➕",
    DONE: "✅",
    CANCELLED: "❌",
    RECURRENCE: "\U0001F501",
    ON_COMPLETION: "\U0001F3C1",
    ID: "\U0001F194",
    DEPENDS_ON: "⛔",
}
PRIORITY_SYMBOLS = {
    "highest": "\U0001F53A",
    "high": "⏫",
    "medium": "\U0001F53C",
    "low": "\U0001F53D",
    "lowest": "⏬",
}
PRIORITY_NAMES = {sym: name for name, sym in PRIORITY_SYMBOLS.items()}

VS16 = "\ufe0f"

DESCRIPTION_MAX_CHARS = 1_000
TAG_MAX_CHARS = 128
TAG_GRAMMAR_RE = re.compile(r"[A-Za-z][A-Za-z0-9_/-]*")

TASK_MALFORMED = "task_malformed"
INVALID_ARGUMENT = "invalid_argument"
RECURRENCE_UNSUPPORTED = "recurrence_unsupported"
#: D8: at most 32 tags on a new task.
MAX_NEW_TASK_TAGS = 32

# The extractor's checkbox grammar (`tasks._CHECKBOX_RE`), copied rather than
# imported (it is private there); `tests/test_task_line.py` pins agreement.
CHECKBOX_RE = re.compile(
    r"(?:[ \t]*+>)*+[ \t]*+(?:[-*+]|[0-9]{1,9}[.)])[ \t]++"
    r"\[(?P<s> |[^\s\[\]])\](?=[ \t]|$)"
)
_GAP_RE = re.compile(r"[ \t]*+")

# The shared inline-tag rule (`vault.INLINE_TAG_RE`), copied so this module
# imports no settings; `tests/test_task_line.py` pins equality.
INLINE_TAG_RE = re.compile(r"(?:^|\s)#([a-zA-Z][a-zA-Z0-9_/-]*)")

# ── the plugin's end-anchored patterns (R§1.2, R§1.4) ────────────────────────

_DATE_VALUE = r"[0-9]{4}-[0-9]{2}-[0-9]{2}"
_TASK_ID = r"[a-zA-Z0-9_-]"


def _field_re(symbols: str, value: str | None) -> re.Pattern:
    source = f"(?P<sym>{symbols}){VS16}?"
    if value is not None:
        source += f" *(?P<val>{value})"
    return re.compile(source + "$")


def _class(syms: Iterable[str]) -> str:
    return "[" + "".join(sorted(syms)) + "]"


_PLUGIN_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    (PRIORITY, _field_re(_class(READ_SYMBOLS[PRIORITY]), None)),
    (DONE, _field_re(_class(READ_SYMBOLS[DONE]), _DATE_VALUE)),
    (CANCELLED, _field_re(_class(READ_SYMBOLS[CANCELLED]), _DATE_VALUE)),
    (DUE, _field_re(_class(READ_SYMBOLS[DUE]), _DATE_VALUE)),
    (SCHEDULED, _field_re(_class(READ_SYMBOLS[SCHEDULED]), _DATE_VALUE)),
    (START, _field_re(_class(READ_SYMBOLS[START]), _DATE_VALUE)),
    (CREATED, _field_re(_class(READ_SYMBOLS[CREATED]), _DATE_VALUE)),
    (RECURRENCE, _field_re(_class(READ_SYMBOLS[RECURRENCE]), r"[a-zA-Z0-9, !]+")),
    (ON_COMPLETION, _field_re(_class(READ_SYMBOLS[ON_COMPLETION]), r"[a-zA-Z]+")),
    ("#tag", re.compile(r"(?:^|\s)(?P<val>#[^ !@#$%^&*(),.?\":{}|<>]+)$")),
    (ID, _field_re(_class(READ_SYMBOLS[ID]), _TASK_ID + "+")),
    # Possessive quantifiers: the same language as the plugin's
    # `id( *, *id *)*`, without exponential backtracking on a failing tail.
    (DEPENDS_ON, _field_re(
        _class(READ_SYMBOLS[DEPENDS_ON]),
        _TASK_ID + r"++(?: *+, *+" + _TASK_ID + r"++ *+)*+",
    )),
)
_BLOCK_LINK_RE = re.compile(r" (?P<val>\^[a-zA-Z0-9-]+)$")
_MAX_PASSES = 21

_RECURRENCE_VALUE_RE = re.compile(r"[a-zA-Z0-9, !]+")
_FORBIDDEN_DESCRIPTION_CHARS = frozenset("\r\n\x85\u2028\u2029")
_BLOCK_SUFFIX_RE = re.compile(r"(?:^|\s)\^[a-zA-Z0-9-]+$")


# ── data ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Token:
    """One run token. Offsets are in the coordinates of the parsed string.

    ``kind`` is ``"field"``, ``"tag"`` or ``"block"``. ``family`` is a field
    family for fields, else None. ``start``/``end`` span the token (symbol
    through value, ``#tag``, ``^block``); ``value_start``/``value_end`` span
    the value characters (the priority emoji itself for a priority);
    ``symbol`` is the signifier as written, including any U+FE0F;
    ``sep_start`` is the start of the whitespace run directly before the
    token (``== start`` when there is none, never before the text start).
    """

    kind: str
    family: str | None
    start: int
    end: int
    value_start: int
    value_end: int
    value: str
    symbol: str
    sep_start: int


@dataclass(frozen=True)
class RunParse:
    """The plugin's backward parse of one task text (R§1.4)."""

    text: str
    description_end: int
    tokens: tuple[Token, ...]  # fields and trailing tags, document order
    block: Token | None

    @property
    def description(self) -> str:
        return self.text[: self.description_end]

    @property
    def trailing_tags(self) -> tuple[str, ...]:
        return tuple(t.value for t in self.tokens if t.kind == "tag")


@dataclass(frozen=True)
class TaskLine:
    """A parsed task line (D4 anatomy). All offsets index ``line``."""

    line: str
    masked: str
    status: str
    box_start: int
    text_start: int
    description_end: int
    text_end: int
    tokens: tuple[Token, ...]
    block: Token | None

    @property
    def prefix(self) -> str:
        return self.line[: self.box_start]

    @property
    def box(self) -> str:
        return self.line[self.box_start : self.box_start + 3]

    @property
    def gap(self) -> str:
        return self.line[self.box_start + 3 : self.text_start]

    @property
    def full_text(self) -> str:
        """The extractor's uncut text (D1's key input)."""
        return self.line[self.text_start : self.text_end]

    @property
    def description(self) -> str:
        """The D4 description region (trailing tags excluded)."""
        return self.line[self.text_start : self.description_end]

    @property
    def tail_ws(self) -> str:
        return self.line[self.text_end :]

    @property
    def run_start(self) -> int:
        starts = [t.start for t in self.tokens]
        if self.block is not None:
            starts.append(self.block.start)
        return min(starts) if starts else self.text_end

    def run_fields(self, family: str) -> tuple[Token, ...]:
        return tuple(t for t in self.tokens if t.kind == "field" and t.family == family)

    def family_count(self, family: str) -> int:
        """Signifier occurrences of ``family`` in the masked text (D4 (b))."""
        syms = READ_SYMBOLS[family]
        return sum(1 for ch in self.masked[self.text_start : self.text_end] if ch in syms)

    def run_value(self, family: str) -> str | None:
        """The plugin's value for ``family``: the leftmost run token wins
        (R§1.4). For listing only; a write uses the strict checks."""
        found = self.run_fields(family)
        return found[0].value if found else None

    @property
    def recurrence(self) -> str | None:
        """The run's ``🔁`` rule text, trimmed (the plugin's rule, R§2a)."""
        value = self.run_value(RECURRENCE)
        return value.strip() if value is not None else None

    @property
    def tags(self) -> tuple[str, ...]:
        """Extractor-grammar tags of the text, document order, unmasked only."""
        return tuple(m.tag for m in _tag_matches(self.line, self.masked, self.text_start, self.text_end))


@dataclass(frozen=True)
class Edit:
    """Replace ``old[start:end]`` with ``text`` (original-line coordinates)."""

    start: int
    end: int
    text: str


@dataclass(frozen=True)
class LineEdit:
    """A composed edit: the old line, the new line, and the exact spans."""

    old: str
    line: str
    edits: tuple[Edit, ...]

    @property
    def changed(self) -> bool:
        return self.line != self.old


@dataclass(frozen=True)
class TaskLineRefusal:
    """An expected refusal: nothing may be written. ``code`` is
    ``task_malformed`` (the line cannot be edited unambiguously),
    ``recurrence_unsupported`` (`serialize_new_task` with a non-tier-1 rule) or
    ``invalid_argument`` (the requested value is not acceptable)."""

    code: str
    reason: str


@dataclass(frozen=True)
class LineChanges:
    """Every change one call makes to one task line, applied together.

    ``set_fields`` maps a family to its new value: a ``datetime.date`` for a
    date family, a priority name for ``priority``, the rule text for
    ``recurrence``. A family present once in the run has its value replaced;
    an absent family is added at its canonical slot. ``remove_fields`` names
    families to delete (absent is a no-op). ``description`` replaces the
    description region (tags preserved, D4). Then ``remove_tags``, then
    ``add_tags``. ``strict_families`` get the (b)/(c)/(d) checks even when
    untouched (a recurring completion passes every date family, D4 (d)).
    """

    status: str | None = None
    set_fields: Mapping[str, object] = field(default_factory=dict)
    remove_fields: frozenset[str] = frozenset()
    remove_block_link: bool = False
    description: str | None = None
    add_tags: tuple[str, ...] = ()
    remove_tags: tuple[str, ...] = ()
    strict_families: frozenset[str] = frozenset()


# ── parsing ──────────────────────────────────────────────────────────────────


# ── the linear end-anchored matcher ──────────────────────────────────────────
#
# Every plugin pattern is ``prefix + value + '$'`` where everything after the
# prefix (the signifier and its optional U+FE0F, or the tag's ``#``, or the
# block link's `` ^``) is drawn from a small alphabet that never contains the
# prefix characters. So the only place a match ending at ``end`` can start is
# immediately before the maximal run of that alphabet ending at ``end``: the
# leftmost match a ``re.search`` from the start would find is that one
# candidate. Runs are found on the reversed text with a forward ``match``
# (C speed), and cached per alphabet while ``end`` stays inside the run, so
# a pass looks only at the suffix it is about to consume and the whole parse
# is linear in the text (#428; the old rescan from the start of the text on
# each of up to 21 passes took ~25 s on a 10 MiB line).

_WS_RUN = re.compile(r"\s*")
_WS_CHAR = re.compile(r"\s")
_ALPHABETS = {
    "date": re.compile(r"[ 0-9-]*"),
    "recurrence": re.compile(r"[a-zA-Z0-9, !]*"),
    "on_completion": re.compile(r"[ a-zA-Z]*"),
    "id": re.compile(r"[ a-zA-Z0-9_-]*"),
    "depends_on": re.compile(r"[ ,a-zA-Z0-9_-]*"),
    "tag": re.compile(r"[^ !@#$%^&*(),.?\":{}|<>]*"),
    "block": re.compile(r"[a-zA-Z0-9-]*"),
}
_FAMILY_ALPHABET = {
    DONE: "date", CANCELLED: "date", DUE: "date", SCHEDULED: "date", START: "date",
    CREATED: "date", RECURRENCE: "recurrence", ON_COMPLETION: "on_completion",
    ID: "id", DEPENDS_ON: "depends_on",
}


class _Backward:
    """Backward runs over one text, via its reversal, cached per alphabet."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.rev = text[::-1]
        self.n = len(text)
        self.cache: dict[str, tuple[int, int]] = {}

    def run_start(self, name: str, pattern: re.Pattern, end: int) -> int:
        """Start of the maximal run of ``pattern``'s alphabet ending at ``end``."""
        cached = self.cache.get(name)
        if cached is not None and cached[0] <= end <= cached[1]:
            return cached[0]
        start = self.n - pattern.match(self.rev, self.n - end).end()
        self.cache[name] = (start, end)
        return start

    def rstrip_end(self, end: int) -> int:
        """``len(text[:end].rstrip())``."""
        return self.run_start("ws", _WS_RUN, end)

    def whitespace_start(self, pos: int, floor: int) -> int:
        return max(self.run_start("ws", _WS_RUN, pos), min(floor, pos))

    def match(self, family: str, pattern: re.Pattern, end: int):
        """The leftmost match of ``pattern`` ending exactly at ``end``, or
        None — the same match ``pattern.search(text, 0, end)`` returns."""
        text = self.text
        if end <= 0:
            return None
        if family == PRIORITY:
            candidates = (end - 1, end - 2)
        elif family == "#tag":
            s = self.run_start("tag", _ALPHABETS["tag"], end)
            if s == end or s < 1 or text[s - 1] != "#":
                return None
            if s - 1 > 0 and _WS_CHAR.match(text, s - 2) is None:
                return None
            return pattern.match(text, max(s - 2, 0), end) if s - 1 > 0 else pattern.match(text, 0, end)
        else:
            s = self.run_start(_FAMILY_ALPHABET[family], _ALPHABETS[_FAMILY_ALPHABET[family]], end)
            candidates = (s - 1, s - 2)
        for pos in candidates:
            if pos < 0:
                continue
            m = pattern.match(text, pos, end)
            if m is not None:
                return m
        return None

    def block_link(self, end: int):
        s = self.run_start("block", _ALPHABETS["block"], end)
        if s == end or s < 2 or self.text[s - 1] != "^" or self.text[s - 2] != " ":
            return None
        return _BLOCK_LINK_RE.match(self.text, s - 2, end)


def parse_run(text: str, *, offset: int = 0, floor: int | None = None) -> RunParse:
    """The plugin's backward parse of ``text`` (R§1.4), tokens in document
    order. ``offset`` shifts every reported span (so a caller can parse a
    slice of a line); ``floor`` bounds ``sep_start`` (defaults to ``offset``).
    Linear in ``len(text)`` (see `_Backward`).
    """
    floor = offset if floor is None else floor
    back = _Backward(text)
    end = back.rstrip_end(len(text))
    block = None
    m = back.block_link(end)
    if m is not None:
        b_start = m.start("val")
        block = Token(
            kind="block", family=None,
            start=offset + b_start, end=offset + m.end(),
            value_start=offset + b_start, value_end=offset + m.end(),
            value=m.group("val"), symbol="^",
            sep_start=offset + back.whitespace_start(b_start, floor - offset),
        )
        end = back.rstrip_end(m.start())

    found: list[Token] = []
    for _ in range(_MAX_PASSES):
        matched = False
        for family, pattern in _PLUGIN_PATTERNS:
            m = back.match(family, pattern, end)
            if m is None:
                continue
            matched = True
            if family == "#tag":
                start = m.start("val")
                found.append(Token(
                    kind="tag", family=None, start=start, end=m.end(),
                    value_start=start, value_end=m.end(), value=m.group("val"),
                    symbol="#", sep_start=start,
                ))
            else:
                start = m.start("sym")
                if family == PRIORITY:
                    v_start, v_end = start, m.end("sym")
                    value = PRIORITY_NAMES[m.group("sym")]
                else:
                    v_start, v_end = m.start("val"), m.end("val")
                    value = m.group("val")
                symbol_end = m.end("sym")
                if text.startswith(VS16, symbol_end):
                    symbol_end += 1
                found.append(Token(
                    kind="field", family=family, start=start, end=m.end(),
                    value_start=v_start, value_end=v_end, value=value,
                    symbol=text[start:symbol_end], sep_start=start,
                ))
            end = back.rstrip_end(m.start())
        if not matched:
            break

    tokens = []
    for t in sorted(found, key=lambda t: t.start):
        sep = back.whitespace_start(t.start, floor - offset)
        tokens.append(Token(
            kind=t.kind, family=t.family,
            start=offset + t.start, end=offset + t.end,
            value_start=offset + t.value_start, value_end=offset + t.value_end,
            value=t.value, symbol=t.symbol, sep_start=offset + sep,
        ))
    return RunParse(text=text, description_end=end, tokens=tuple(tokens), block=block)


def parse(line: str, *, masked: str | None = None) -> TaskLine | None:
    """Parse one task line, or None when the extractor would not call it a
    task.

    ``line`` excludes its terminator. ``masked`` is the line's slice of the
    whole-body mask (`mask_code(body, context=BODY)`), which is what the
    extractor reads; it defaults to masking the line alone, which agrees
    except where an inline code span crosses a line break.
    """
    if "\r" in line or "\n" in line:
        raise ValueError("task_line.parse: a line excludes its terminator")
    if masked is None:
        masked = mask_code(line, context=BODY)
    if len(masked) != len(line):
        raise ValueError("task_line.parse: masked text has a different length")
    m = CHECKBOX_RE.match(masked)
    if m is None:
        return None
    marker_end = m.end()
    if line[:marker_end] != masked[:marker_end]:
        return None  # the #218 rule
    text_start = _GAP_RE.match(line, marker_end).end()
    text_end = text_start + len(line[text_start:].rstrip())
    run = parse_run(line[text_start:text_end], offset=text_start)
    return TaskLine(
        line=line,
        masked=masked,
        status=m.group("s"),
        box_start=m.start("s") - 1,
        text_start=text_start,
        description_end=text_start + run.description_end,
        text_end=text_end,
        tokens=run.tokens,
        block=run.block,
    )


def description_region(text: str) -> str:
    """The D4 description region of a task text: fields, trailing tags and
    the block link removed (the brief row's ``<desc>``, D12)."""
    return parse_run(text).description


def plugin_description(text: str) -> str:
    """The plugin's description: the region plus the trailing tags, which
    the plugin re-appends to it (R§1.4). D9's input."""
    run = parse_run(text)
    tags = run.trailing_tags
    return run.description + ("" if not tags else " " + " ".join(tags))


@dataclass(frozen=True)
class _TagMatch:
    tag: str
    start: int  # the "#"
    end: int


def _tag_matches(line: str, masked: str, start: int, end: int):
    """Unmasked inline-tag matches inside ``[start, end)``, on the masked
    slice exactly as the extractor scans it (``^`` matches at the slice
    start), so a tag in code is not a tag."""
    for m in INLINE_TAG_RE.finditer(masked[start:end]):
        s, e = start + m.start(1) - 1, start + m.end(1)
        if line[s:e] == masked[s:e]:
            yield _TagMatch(m.group(1), s, e)


# ── identity and normalisation (D1, D9, D11) ─────────────────────────────────

_ID_RE = re.compile(r"(?P<key>[0-9a-f]{12})(?:\.(?P<i>[1-9][0-9]{0,3})of(?P<n>[1-9][0-9]{0,3}))?")


@dataclass(frozen=True)
class TaskId:
    """A parsed id. ``ordinal``/``count`` are None for a bare key (and for
    ``.1of1``, which D1 makes equivalent to the bare key)."""

    key: str
    ordinal: int | None = None
    count: int | None = None

    @property
    def suffixed(self) -> bool:
        return self.ordinal is not None


def task_key(full_text: str) -> str:
    """D1: 12 hex of SHA-256 over ``omcp-task-v1\\0`` + the uncut text."""
    return hashlib.sha256(b"omcp-task-v1\x00" + full_text.encode("utf-8")).hexdigest()[:12]


def render_id(key: str, ordinal: int, count: int) -> str:
    """D1: the bare key when ``count == 1``, else ``<key>.<ordinal>of<count>``."""
    if not re.fullmatch(r"[0-9a-f]{12}", key):
        raise ValueError("render_id: malformed key")
    if not (1 <= ordinal <= count <= 9999):
        raise ValueError("render_id: need 1 <= ordinal <= count <= 9999")
    return key if count == 1 else f"{key}.{ordinal}of{count}"


def parse_id(value: str) -> TaskId | None:
    """Parse a rendered id (D1 grammar); None when malformed or the ordinal
    exceeds the count. ``<key>.1of1`` parses as the bare key."""
    if not isinstance(value, str):
        return None
    m = _ID_RE.fullmatch(value)
    if m is None:
        return None
    if m.group("i") is None:
        return TaskId(m.group("key"))
    ordinal, count = int(m.group("i")), int(m.group("n"))
    if ordinal > count:
        return None
    if count == 1:
        return TaskId(m.group("key"))
    return TaskId(m.group("key"), ordinal, count)


def normalise_description(text: str) -> str:
    """D9: the plugin description of a task text (fields and block link
    removed, tags kept), NFC, ``casefold()``, whitespace runs collapsed to one
    space, stripped."""
    folded = unicodedata.normalize("NFC", plugin_description(text)).casefold()
    return " ".join(folded.split())


def description_key(norm: str) -> str:
    """D9: 16 hex of SHA-256 over ``omcp-desc-v1\\0`` + the normalised text."""
    return hashlib.sha256(b"omcp-desc-v1\x00" + norm.encode("utf-8")).hexdigest()[:16]


def fold_text(text: str) -> str:
    """NFC then ``casefold()``: one folding for the stored ``text_fold`` and
    the ``list_tasks(text=…)`` terms, so the two always agree (D12)."""
    return unicodedata.normalize("NFC", text).casefold()


_SLASH_RUN_RE = re.compile(r"/{2,}")


def normalise_tag(tag: str) -> str:
    """D11/D12: lower-case, collapse every run of ``/`` to one, strip a
    trailing ``/``. Stored tag prefixes and the ``tags`` / ``exclude_tags``
    filter values go through this one function, so both sides agree."""
    return _SLASH_RUN_RE.sub("/", tag.lower()).rstrip("/")


def tag_prefixes(tags: Iterable[str]) -> list[str]:
    """D11: for each stored tag, the normalised tag (`normalise_tag`) cut
    immediately before each ``/``, plus the whole normalised tag;
    deduplicated in first-seen order and never capped."""
    out: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        norm = normalise_tag(tag)
        if not norm:
            continue
        candidates = [norm[:i] for i, ch in enumerate(norm) if ch == "/"]
        candidates.append(norm)
        for c in candidates:
            if c not in seen:
                seen.add(c)
                out.append(c)
    return out


# ── validation helpers ───────────────────────────────────────────────────────


def valid_tag(tag: str) -> bool:
    """The inline-tag grammar, at most 128 characters (D8)."""
    return isinstance(tag, str) and len(tag) <= TAG_MAX_CHARS and TAG_GRAMMAR_RE.fullmatch(tag) is not None


def _norm_tag_arg(tag: str) -> str | None:
    if isinstance(tag, str) and tag.startswith("#"):
        tag = tag[1:]
    return tag if valid_tag(tag) else None


def validate_description(text: str) -> str | TaskLineRefusal:
    """The D4 description rules: stripped, 1–1,000 characters, one line, no
    control or line-separator character, no field signifier, no ``^block``
    suffix. Returns the stripped text or an ``invalid_argument`` refusal."""
    if not isinstance(text, str):
        return TaskLineRefusal(INVALID_ARGUMENT, "text must be a string")
    if any(ch in _FORBIDDEN_DESCRIPTION_CHARS for ch in text):
        return TaskLineRefusal(INVALID_ARGUMENT, "text must be a single line")
    if any(unicodedata.category(ch) == "Cc" and ch != "\t" for ch in text):
        return TaskLineRefusal(INVALID_ARGUMENT, "text must not contain control characters")
    stripped = text.strip()
    if not 1 <= len(stripped) <= DESCRIPTION_MAX_CHARS:
        return TaskLineRefusal(INVALID_ARGUMENT, f"text must be 1-{DESCRIPTION_MAX_CHARS} characters")
    if any(ch in ALL_SIGNIFIERS for ch in stripped):
        return TaskLineRefusal(INVALID_ARGUMENT, "text must not contain a task field signifier")
    if _BLOCK_SUFFIX_RE.search(stripped):
        return TaskLineRefusal(INVALID_ARGUMENT, "text must not end with a ^block link")
    return stripped


def valid_status(status: str) -> bool:
    return (
        isinstance(status, str) and len(status) == 1
        and (status == " " or not (status.isspace() or status in "[]"))
    )


def _field_value_text(family: str, value: object) -> str | TaskLineRefusal:
    if family in DATE_FAMILIES:
        if not isinstance(value, datetime.date) or isinstance(value, datetime.datetime):
            return TaskLineRefusal(INVALID_ARGUMENT, f"{family} must be a date")
        text = value.isoformat()
        if not re.fullmatch(_DATE_VALUE, text):
            return TaskLineRefusal(INVALID_ARGUMENT, f"{family} is out of range")
        return text
    if family == PRIORITY:
        if value not in PRIORITY_SYMBOLS:
            return TaskLineRefusal(INVALID_ARGUMENT, f"unknown priority {value!r}")
        return PRIORITY_SYMBOLS[value]
    if family == RECURRENCE:
        if not isinstance(value, str) or value != value.strip() or not _RECURRENCE_VALUE_RE.fullmatch(value):
            return TaskLineRefusal(INVALID_ARGUMENT, "recurrence rule text is not representable")
        return value
    return TaskLineRefusal(INVALID_ARGUMENT, f"{family} cannot be set")


def _calendar_date(value: str) -> bool:
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        return False
    return bool(re.fullmatch(_DATE_VALUE, value))


def check_families(tl: TaskLine, families: Iterable[str], *, dates_must_be_valid: bool = True) -> TaskLineRefusal | None:
    """D4 (b), (c) and, for date families, (d): each family occurs at most
    once in the masked text, and when once, as a run token with a calendar
    date. Absent is fine."""
    for family in sorted(set(families), key=CANONICAL_RANK.__getitem__):
        count = tl.family_count(family)
        in_run = tl.run_fields(family)
        if count > 1 or len(in_run) > 1:
            return TaskLineRefusal(TASK_MALFORMED, f"the {family} field occurs more than once")
        if count == 1 and not in_run:
            return TaskLineRefusal(TASK_MALFORMED, f"the {family} signifier is outside the trailing field run")
        if in_run and count == 0:
            # A run token whose signifier the mask hid: code decided it.
            return TaskLineRefusal(TASK_MALFORMED, f"the {family} field is inside code")
        if dates_must_be_valid and family in DATE_FAMILIES and in_run and not _calendar_date(in_run[0].value):
            return TaskLineRefusal(TASK_MALFORMED, f"the {family} date {in_run[0].value} is not a calendar date")
    return None


def field_date(tl: TaskLine, family: str) -> datetime.date | None:
    """The single run token's date for ``family``, or None (absent or not a
    calendar date). Call `check_families` first for a strict reading."""
    found = tl.run_fields(family)
    if len(found) != 1:
        return None
    try:
        return datetime.date.fromisoformat(found[0].value)
    except ValueError:
        return None


# ── composition ──────────────────────────────────────────────────────────────


def compose(old: str, edits: Iterable[Edit]) -> str:
    """Apply non-overlapping edits (sorted by position; zero-length edits at
    one position keep their given order)."""
    out = []
    pos = 0
    for e in edits:
        if e.start < pos or e.end < e.start:
            raise ValueError("compose: overlapping or reversed edits")
        out.append(old[pos : e.start])
        out.append(e.text)
        pos = e.end
    out.append(old[pos:])
    return "".join(out)


@dataclass
class _Plan:
    tl: TaskLine
    # Non-empty ranges (deletions / replacements), keyed by start.
    ranges: list[Edit] = field(default_factory=list)
    # Insertions: (position, sort key, core text).
    inserts: list[tuple[int, tuple, str]] = field(default_factory=list)
    claimed: set[int] = field(default_factory=set)

    def replace(self, start: int, end: int, text: str) -> None:
        self.ranges.append(Edit(start, end, text))
        self.claimed.update(range(start, end))

    def remove_token(self, start: int, end: int, *, lo: int, hi: int) -> None:
        """Delete ``[start, end)`` plus one adjacent separating whitespace
        character inside ``[lo, hi)``: the preceding one, else the following
        one, never one another removal already took."""
        line = self.tl.line
        if start - 1 >= lo and line[start - 1] in " \t" and (start - 1) not in self.claimed:
            start -= 1
        elif end < hi and line[end] in " \t" and end not in self.claimed:
            end += 1
        self.replace(start, end, "")


def _insertion_point_before(token: Token) -> int:
    return token.sep_start if token.sep_start < token.start else token.start


def _field_insertion_point(tl: TaskLine, family: str) -> int:
    """D4 placement of an added field (rank rule, before a block link and
    before ``tail_ws``)."""
    fields = [t for t in tl.tokens if t.kind == "field"]
    end_of_text = tl.block.sep_start if tl.block is not None else tl.text_end
    if not fields:
        return end_of_text
    ranks = [CANONICAL_RANK[t.family] for t in fields]
    if all(a <= b for a, b in zip(ranks, ranks[1:])):
        new_rank = CANONICAL_RANK[family]
        for t in fields:
            if CANONICAL_RANK[t.family] > new_rank:
                return _insertion_point_before(t)
        return fields[-1].end
    # Order not determinable: the end of the run (after trailing tags).
    return max(t.end for t in tl.tokens)


_UNDELIMITED_TAG = "the tag {tag!r} is attached to other characters and cannot be removed cleanly"


def _removable_tag_spans(
    text: str, masked: str, start: int, end: int, removing: set[str],
    skip: list[tuple[int, int]] = (),  # type: ignore[assignment]
) -> list[tuple[int, int]] | TaskLineRefusal:
    """The ``#tag`` spans to remove for the named tags. A tag is removed
    only where its token is delimited: preceded by the start or whitespace
    (the grammar guarantees it) and followed by whitespace or the end of
    ``[start, end)``. A named tag with any undelimited occurrence is refused
    — never a partial cut."""
    spans = []
    for m in _tag_matches(text, masked, start, end):
        if m.tag.lower() not in removing:
            continue
        if any(s <= m.start < e for s, e in skip):
            continue  # inside a token that is being replaced whole
        if m.end < end and not text[m.end].isspace():
            return TaskLineRefusal(TASK_MALFORMED, _UNDELIMITED_TAG.format(tag=m.tag))
        spans.append((m.start, m.end))
    return spans


def _description_with_tags(old_desc: str, new_text: str, remove: list[str], add: list[str], run_tags_after: set[str], extra_tags: list[str]) -> str | TaskLineRefusal:
    """D4 description replacement with tag re-append (each tag as a clean
    ``" #" + tag`` token, exactly as the extractor's grammar reads it), then
    the tag removals and additions that fall in the description, as one
    string. ``extra_tags`` are tags of run tokens that carried attached
    characters and are being replaced with the description."""
    old_mask = mask_code(old_desc, context=BODY)
    new_mask = mask_code(new_text, context=BODY)
    present = {m.tag.lower() for m in _tag_matches(new_text, new_mask, 0, len(new_text))}
    result = new_text
    for tag in [m.tag for m in _tag_matches(old_desc, old_mask, 0, len(old_desc))] + extra_tags:
        if tag.lower() not in present:
            present.add(tag.lower())
            result += " #" + tag
    return _string_tag_ops(result, remove, add, run_tags_after)


def _string_tag_ops(text: str, remove: list[str], add: list[str], run_tags_after: set[str]) -> str | TaskLineRefusal:
    removing = {t.lower() for t in remove}
    if removing:
        masked = mask_code(text, context=BODY)
        spans = _removable_tag_spans(text, masked, 0, len(text), removing)
        if isinstance(spans, TaskLineRefusal):
            return spans
        edits = []
        claimed: set[int] = set()
        for start, end in spans:
            if start - 1 >= 0 and text[start - 1] in " \t" and (start - 1) not in claimed:
                start -= 1
            elif end < len(text) and text[end] in " \t" and end not in claimed:
                end += 1
            claimed.update(range(start, end))
            edits.append(Edit(start, end, ""))
        text = compose(text, sorted(edits, key=lambda e: e.start))
    masked = mask_code(text, context=BODY)
    present = {m.tag.lower() for m in _tag_matches(text, masked, 0, len(text))} | run_tags_after
    for tag in add:
        if tag.lower() not in present:
            present.add(tag.lower())
            text += " #" + tag
    return text


def apply_changes(tl: TaskLine, changes: LineChanges) -> LineEdit | TaskLineRefusal:
    """Compose every change of ``changes`` against ``tl`` (D4).

    Returns the `LineEdit` (possibly unchanged — the caller answers "No
    changes") or a `TaskLineRefusal`; never raises for an expected outcome.
    """
    line = tl.line
    set_fields = dict(changes.set_fields)
    remove_fields = set(changes.remove_fields)

    # ── argument validation ──
    if changes.status is not None and not valid_status(changes.status):
        return TaskLineRefusal(INVALID_ARGUMENT, "status must be one marker character")
    unknown = (set(set_fields) | remove_fields | set(changes.strict_families)) - FAMILIES
    if unknown:
        return TaskLineRefusal(INVALID_ARGUMENT, f"unknown field family {sorted(unknown)[0]!r}")
    if set(set_fields) & remove_fields:
        return TaskLineRefusal(INVALID_ARGUMENT, "a field cannot be both set and removed")
    value_texts: dict[str, str] = {}
    for family, value in set_fields.items():
        text = _field_value_text(family, value)
        if isinstance(text, TaskLineRefusal):
            return text
        value_texts[family] = text
    add_tags: list[str] = []
    for tag in changes.add_tags:
        norm = _norm_tag_arg(tag)
        if norm is None:
            return TaskLineRefusal(INVALID_ARGUMENT, f"invalid tag {tag!r}")
        add_tags.append(norm)
    remove_tags: list[str] = []
    for tag in changes.remove_tags:
        norm = _norm_tag_arg(tag)
        if norm is None:
            return TaskLineRefusal(INVALID_ARGUMENT, f"invalid tag {tag!r}")
        remove_tags.append(norm)
    new_description = None
    if changes.description is not None:
        new_description = validate_description(changes.description)
        if isinstance(new_description, TaskLineRefusal):
            return new_description

    # ── D4 (a)–(d) ──
    touched = set(set_fields) | remove_fields
    refusal = check_families(tl, touched | set(changes.strict_families), dates_must_be_valid=False)
    if refusal is not None:
        return refusal
    for family in (set(set_fields) & DATE_FAMILIES) | (set(changes.strict_families) & DATE_FAMILIES):
        found = tl.run_fields(family)
        if found and not _calendar_date(found[0].value):
            return TaskLineRefusal(TASK_MALFORMED, f"the {family} date {found[0].value} is not a calendar date")
    if touched or changes.remove_block_link:
        rs = tl.run_start
        if line[rs : tl.text_end] != tl.masked[rs : tl.text_end]:
            return TaskLineRefusal(TASK_MALFORMED, "the trailing field run contains code")

    plan = _Plan(tl)
    lo, hi = tl.text_start, tl.text_end

    # Status.
    if changes.status is not None and changes.status != tl.status:
        plan.replace(tl.box_start + 1, tl.box_start + 2, changes.status)

    # Field changes in place, and additions.
    for family, text in value_texts.items():
        found = tl.run_fields(family)
        if found:
            t = found[0]
            if line[t.value_start : t.value_end] != text:
                plan.replace(t.value_start, t.value_end, text)
        else:
            pos = _field_insertion_point(tl, family)
            if family == PRIORITY:
                core = text
            else:
                core = WRITE_SYMBOLS[family] + " " + text
            plan.inserts.append((pos, (2, CANONICAL_RANK[family]), core))

    # Removals (fields, block link, run tags), left to right for the
    # adjacent-space claim rule.
    removals: list[tuple[int, int]] = []
    for family in remove_fields:
        for t in tl.run_fields(family):
            removals.append((t.start, t.end))
    if changes.remove_block_link and tl.block is not None:
        removals.append((tl.block.start, tl.block.end))
    removing = {t.lower() for t in remove_tags}
    tag_scan_start = tl.description_end if new_description is not None else tl.text_start
    # A description replacement also replaces run tag tokens that carry
    # attached characters (``#a]]``): the token goes, and its tag (as the
    # extractor reads it) is re-appended clean.
    unclean_tags: list[str] = []
    replaced_spans: list[tuple[int, int]] = []
    if new_description is not None:
        for t in tl.tokens:
            if t.kind != "tag":
                continue
            m = INLINE_TAG_RE.match(" " + t.value)
            clean = m is not None and "#" + m.group(1) == t.value
            if clean or line[t.start : t.end] != tl.masked[t.start : t.end]:
                continue
            removals.append((t.start, t.end))
            replaced_spans.append((t.start, t.end))
            if m is not None and m.group(1).lower() not in removing:
                unclean_tags.append(m.group(1))
    if removing:
        spans = _removable_tag_spans(
            line, tl.masked, tag_scan_start, tl.text_end, removing, skip=replaced_spans
        )
        if isinstance(spans, TaskLineRefusal):
            return spans
        removals.extend(spans)
    for start, end in sorted(set(removals)):
        plan.remove_token(start, end, lo=lo, hi=hi)

    # Tags that survive in the run after removals (for add_tags no-op).
    surviving_run_tags = {
        m.tag.lower()
        for m in _tag_matches(line, tl.masked, tl.description_end, tl.text_end)
        if m.tag.lower() not in removing
    }

    if new_description is not None:
        desc = _description_with_tags(
            tl.description, new_description, remove_tags, add_tags, surviving_run_tags, unclean_tags
        )
        if isinstance(desc, TaskLineRefusal):
            return desc
        if tl.description_end > tl.text_start:
            plan.replace(tl.text_start, tl.description_end, desc)
        else:
            plan.inserts.append((tl.text_start, (0,), desc))
    elif add_tags:
        present = {
            m.tag.lower()
            for m in _tag_matches(line, tl.masked, tl.text_start, tl.text_end)
            if m.tag.lower() not in removing
        }
        for i, tag in enumerate(add_tags):
            if tag.lower() not in present:
                present.add(tag.lower())
                plan.inserts.append((tl.description_end, (1, i), "#" + tag))

    # ── compose ──
    ranges = sorted(plan.ranges, key=lambda e: e.start)
    for a, b in zip(ranges, ranges[1:]):
        if b.start < a.end:
            return TaskLineRefusal(TASK_MALFORMED, "the requested changes overlap on this line")
    groups: dict[int, list[tuple[tuple, str]]] = {}
    for pos, key, core in plan.inserts:
        inside = [r for r in ranges if r.start < pos < r.end]
        if inside:
            if inside[0].text:
                # Inside a replacement: genuinely undeterminable.
                return TaskLineRefusal(TASK_MALFORMED, "a field cannot be placed unambiguously on this line")
            # Inside a pure deletion (a removed token and its separating
            # space): the deleted bytes vanish either way, so place the
            # insertion at the deletion's start; spacing is settled below.
            pos = inside[0].start
        groups.setdefault(pos, []).append((key, core))
    # Pieces in output order: an insertion group sorts before a range that
    # starts at the same position.
    pieces: list[tuple[int, int, str, bool]] = [(r.start, r.end, r.text, False) for r in ranges]
    for pos, items in groups.items():
        pieces.append((pos, pos, " ".join(c for _, c in sorted(items, key=lambda kc: kc[0])), True))
    pieces.sort(key=lambda pc: (pc[0], not pc[3]))
    # Emitted text between pieces, so every inserted token is separated from
    # its neighbours by whitespace whatever else the call removes or inserts.
    gaps = []
    cursor = 0
    for start, end, _text, _ins in pieces:
        gaps.append(line[cursor:start])
        cursor = end
    gaps.append(line[cursor:])
    emitted: list[str] = []
    for i, (start, end, text, is_insert) in enumerate(pieces):
        emitted.append(gaps[i])
        if not is_insert:
            emitted.append(text)
            continue
        before = "".join(emitted)
        lead = " " if before and not before[-1].isspace() else ""
        # The next emitted character: a following insertion adds its own
        # leading space, so only original or replacement text counts.
        following = ""
        for j in range(i + 1, len(pieces) + 1):
            if gaps[j]:
                following = gaps[j]
                break
            if j == len(pieces) or pieces[j][3]:
                break  # end of line, or an insertion with its own lead
            if pieces[j][2]:
                following = pieces[j][2]
                break
        trail = " " if following and not following[0].isspace() else ""
        emitted.append(lead + text + trail)
        pieces[i] = (start, end, lead + text + trail, True)
    emitted.append(gaps[-1])
    new_line = "".join(emitted)
    edits = [Edit(start, end, text) for start, end, text, _ in pieces]
    if compose(line, edits) != new_line:  # belt: the spans describe the result
        return TaskLineRefusal(TASK_MALFORMED, "internal: the edit spans do not describe the result")
    result = LineEdit(old=line, line=new_line, edits=tuple(edits))

    problem = _verify(tl, changes, value_texts, add_tags, remove_tags, new_description, result)
    if problem is not None:
        return TaskLineRefusal(TASK_MALFORMED, problem)
    return result


def _verify(tl, changes, value_texts, add_tags, remove_tags, new_description, result: LineEdit) -> str | None:
    """Re-parse the composed line and compare it with the intended result.
    A mismatch means the edit would re-parse differently: refuse."""
    old_view = parse(tl.line)
    new = parse(result.line)
    if new is None or old_view is None:
        return "the edited line would no longer be a task"
    # Compared as bytes at their positions: with an empty text the gap and
    # the trailing whitespace are adjacent and the re-parse cannot tell them
    # apart.
    after_box = result.line[new.box_start + 3 :]
    if (
        new.prefix != tl.prefix
        or not after_box.startswith(tl.gap)
        or not result.line.endswith(tl.tail_ws)
        or len(after_box) < len(tl.gap) + len(tl.tail_ws)
    ):
        return "the edit would change the line's prefix or trailing whitespace"
    if new.status != (changes.status if changes.status is not None else tl.status):
        return "the edit would not set the intended status"
    for family in CANONICAL_ORDER:
        old_tokens = [result.old[t.start : t.end] for t in old_view.run_fields(family)]
        new_found = new.run_fields(family)
        new_tokens = [result.line[t.start : t.end] for t in new_found]
        if family in value_texts:
            if len(new_found) != 1 or new.family_count(family) != 1:
                return f"the {family} field would not re-parse once"
            got = result.line[new_found[0].value_start : new_found[0].value_end]
            if got != value_texts[family]:
                return f"the {family} field would re-parse with another value"
            if old_view.run_fields(family) and new_found[0].symbol != old_view.run_fields(family)[0].symbol:
                if family != PRIORITY:
                    return f"the {family} signifier would change"
        elif family in changes.remove_fields:
            if new_found or new.family_count(family) != 0:
                return f"the {family} field would survive its removal"
        else:
            if new_tokens != old_tokens or new.family_count(family) != old_view.family_count(family):
                return f"the untouched {family} field would change"
    old_block = old_view.block.value if old_view.block else None
    new_block = new.block.value if new.block else None
    if new_block != (None if changes.remove_block_link else old_block):
        return "the block link would change"
    expected = {t.lower() for t in old_view.tags}
    if new_description is not None:
        expected |= {m.tag.lower() for m in _tag_matches(new_description, mask_code(new_description, context=BODY), 0, len(new_description))}
    expected -= {t.lower() for t in remove_tags}
    expected |= {t.lower() for t in add_tags}
    if {t.lower() for t in new.tags} != expected:
        return "the edit would not conserve the task's tags"
    return None


# ── convenience operations ───────────────────────────────────────────────────


def set_status(tl: TaskLine, status: str) -> LineEdit | TaskLineRefusal:
    """Replace the one status character inside the box (D4 "Status")."""
    return apply_changes(tl, LineChanges(status=status))


def set_field(tl: TaskLine, family: str, value: object) -> LineEdit | TaskLineRefusal:
    """Change a field's value in place, or add it at its canonical slot."""
    return apply_changes(tl, LineChanges(set_fields={family: value}))


def remove_field(tl: TaskLine, family: str) -> LineEdit | TaskLineRefusal:
    """Delete a field token and one adjacent separating space."""
    return apply_changes(tl, LineChanges(remove_fields=frozenset({family})))


def add_tag(tl: TaskLine, tag: str) -> LineEdit | TaskLineRefusal:
    """Insert ``" #tag"`` at the end of the description region (no-op when
    present, whole tag, case-insensitive)."""
    return apply_changes(tl, LineChanges(add_tags=(tag,)))


def remove_tag(tl: TaskLine, tag: str) -> LineEdit | TaskLineRefusal:
    """Remove every unmasked whole-tag, case-insensitive match (never
    hierarchical) with one adjacent space; absent is a no-op."""
    return apply_changes(tl, LineChanges(remove_tags=(tag,)))


def replace_description(tl: TaskLine, text: str) -> LineEdit | TaskLineRefusal:
    """Replace the description region, keeping its tags (D4)."""
    return apply_changes(tl, LineChanges(description=text))


def next_occurrence_line(
    tl: TaskLine,
    dates: Mapping[str, datetime.date],
    *,
    created: datetime.date | None,
) -> LineEdit | TaskLineRefusal:
    """D7 "The next line": the completed task's ORIGINAL line with status
    ``[ ]``, the start/scheduled/due values replaced in place by ``dates``
    (from `task_recurrence.next_occurrence`; only families present on the
    line), ``✅ ❌ 🆔 ⛔`` and the block link removed, and ``➕`` set to
    ``created`` (replaced in place or added) or removed when ``created`` is
    None. Every date family is checked strictly (D4 (d))."""
    for family in dates:
        if family not in (START, SCHEDULED, DUE):
            raise ValueError(f"next_occurrence_line: {family} does not shift")
        if not tl.run_fields(family):
            raise ValueError(f"next_occurrence_line: {family} is not on the line")
    set_fields: dict[str, object] = dict(dates)
    remove = {DONE, CANCELLED, ID, DEPENDS_ON}
    if created is not None:
        set_fields[CREATED] = created
    else:
        remove.add(CREATED)
    return apply_changes(tl, LineChanges(
        status=" ",
        set_fields=set_fields,
        remove_fields=frozenset(remove),
        remove_block_link=True,
        strict_families=DATE_FAMILIES,
    ))


# ── a brand-new line (D8) ────────────────────────────────────────────────────


def serialize_new_task(
    text: str,
    *,
    marker: str = "- ",
    tags: Iterable[str] = (),
    priority: str | None = None,
    recurrence: str | None = None,
    created: datetime.date | None = None,
    start: datetime.date | None = None,
    scheduled: datetime.date | None = None,
    due: datetime.date | None = None,
) -> str | TaskLineRefusal:
    """D8: ``marker + "[ ] " + text [#tag…] [priority] [🔁 rule] [➕] [🛫]
    [⏳] [📅]`` — R§1.3's canonical order, one space before and after each
    signifier. ``marker`` is everything before ``[`` (blockquote prefix and
    bullet, e.g. ``"> 3. "``). Tags already in ``text`` or repeated are not
    added twice (case-insensitive). The result is re-parsed and checked."""
    desc = validate_description(text)
    if isinstance(desc, TaskLineRefusal):
        return desc
    tags = list(tags)
    if len(tags) > MAX_NEW_TASK_TAGS:
        return TaskLineRefusal(INVALID_ARGUMENT, f"at most {MAX_NEW_TASK_TAGS} tags")
    if recurrence is not None:
        rule = parse_rule(recurrence) if isinstance(recurrence, str) else None
        if not isinstance(rule, Rule):
            return TaskLineRefusal(RECURRENCE_UNSUPPORTED, "the recurrence rule is outside the supported grammar")
    parts = [desc]
    present = {m.tag.lower() for m in _tag_matches(desc, mask_code(desc, context=BODY), 0, len(desc))}
    added_tags = []
    for tag in tags:
        norm = _norm_tag_arg(tag)
        if norm is None:
            return TaskLineRefusal(INVALID_ARGUMENT, f"invalid tag {tag!r}")
        if norm.lower() not in present:
            present.add(norm.lower())
            parts.append("#" + norm)
            added_tags.append(norm)
    expected: dict[str, str] = {}
    for family, value in ((PRIORITY, priority), (RECURRENCE, recurrence), (CREATED, created),
                          (START, start), (SCHEDULED, scheduled), (DUE, due)):
        if value is None:
            continue
        vt = _field_value_text(family, value)
        if isinstance(vt, TaskLineRefusal):
            return vt
        expected[family] = vt
        parts.append(vt if family == PRIORITY else WRITE_SYMBOLS[family] + " " + vt)
    line = marker + "[ ] " + " ".join(parts)
    if "\r" in line or "\n" in line:
        return TaskLineRefusal(INVALID_ARGUMENT, "marker must be one line")
    tl = parse(line)
    if tl is None or tl.prefix != marker or tl.status != " " or tl.full_text != " ".join(parts):
        return TaskLineRefusal(INVALID_ARGUMENT, "the new line would not parse as one task")
    for family in CANONICAL_ORDER:
        found = tl.run_fields(family)
        want = expected.get(family)
        got = None if not found else line[found[0].value_start : found[0].value_end]
        if len(found) > 1 or tl.family_count(family) != (1 if want else 0) or got != want:
            return TaskLineRefusal(INVALID_ARGUMENT, "the new line would not re-parse with its fields")
    if {t.lower() for t in tl.tags} != present:
        return TaskLineRefusal(INVALID_ARGUMENT, "the new line would not re-parse with its tags")
    return line


__all__ = [
    "ALL_SIGNIFIERS", "CANCELLED", "CANONICAL_ORDER", "CANONICAL_RANK", "CREATED",
    "DATE_FAMILIES", "DEPENDS_ON", "DESCRIPTION_MAX_CHARS", "DONE", "DUE", "Edit",
    "FAMILIES", "ID", "INVALID_ARGUMENT", "MAX_NEW_TASK_TAGS", "RECURRENCE_UNSUPPORTED", "LineChanges", "LineEdit", "ON_COMPLETION",
    "PRIORITY", "PRIORITY_NAMES", "PRIORITY_SYMBOLS", "READ_SYMBOLS", "RECURRENCE",
    "RunParse", "SCHEDULED", "START", "TASK_MALFORMED", "TaskId", "TaskLine",
    "TaskLineRefusal", "Token", "WRITE_SYMBOLS", "add_tag", "apply_changes",
    "check_families", "compose", "description_key", "description_region", "field_date",
    "fold_text", "next_occurrence_line", "normalise_description", "normalise_tag", "parse", "parse_id",
    "parse_run", "plugin_description", "remove_field", "remove_tag", "render_id",
    "replace_description", "serialize_new_task", "set_field", "set_status",
    "tag_prefixes", "task_key", "valid_status", "valid_tag", "validate_description",
]
