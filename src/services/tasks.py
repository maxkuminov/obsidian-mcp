"""Checkbox task extraction from note bodies (#350, design D2/D3).

One pure, synchronous function, `extract_tasks_bounded`, turns a note's
post-frontmatter body into the rows the index pass stores in `note_tasks`.
It never evaluates a Tasks-plugin or Dataview query block: those are fenced
text, masked like any other code.

## The grammar

1. The body is masked with the shared code-masking grammar
   (`mask_code(..., context=BODY)`), so fenced and inline code are spaces of
   equal length and offsets agree between the masked and original text.
2. Lines are split under the universal-newline rule (`\\r\\n`, `\\n`, `\\r`).
3. A line is a task when, at its start, the **masked** line matches
   `_CHECKBOX_RE`: optional blockquote/callout prefixes, any horizontal
   indentation, a bullet (`-`, `*`, `+`) or ordered marker (1–9 digits then
   `.` or `)`), horizontal whitespace, and a bracketed one-code-point status
   (U+0020, or any non-whitespace character but `[` and `]`) followed by
   horizontal whitespace or the end of the line. Possessive quantifiers only.
4. **The #218 rule for tasks**: the original and masked text must be equal
   from the line start through the closing `]`; otherwise masking decided the
   marker and the line is not a task.
5. Text is the original line after `]` and its following horizontal
   whitespace, trailing whitespace removed, cut at `TASK_TEXT_MAX_CHARS`.
6. Fields come from the **masked** slice of the same span, last occurrence of
   each family wins; an invalid date is NULL and never an error. Tags use the
   shared inline-tag rule (`vault.INLINE_TAG_RE`): longer than
   `TASK_TAG_MAX_CHARS` is dropped, the first `TASK_MAX_TAGS` distinct are
   kept in document order, and `tags_cut` records either drop.
7. The heading path is the stack of column-zero ATX headings
   (`vault._ATX_HEADING_RE`) above the task in the masked body.
8. At most `MAX_TASKS_PER_NOTE` tasks, in document order; the bound reports
   `truncated=True`.

**Any change to this grammar — or to any of the extraction caps — is a
`CURRENT_EXTRACTION_VERSION` bump** in `src/services/indexer.py`, the same rule
the fence and link grammars carry: a stored row is re-derived only when its
content hash or its extraction version changes.

## Composition rule for per-note derivations (design D4)

The index pass derives `note_links` and `note_tasks` per changed note. A
per-note derivation, and any third one added next to these (the
`change-feed` change, #351):

1. is keyed by `notes_metadata.id` with `ON DELETE CASCADE`;
2. is a pure function of the buffered body and the extraction version;
3. runs after the link rebuild in the same transaction, for the same changed
   list;
4. records a missing buffered body or id in the shared `skips` /
   `skipped_paths` lists;
5. never reads a file;
6. **isolates its own failure**: a per-note savepoint, one retry, then an
   owner-visible failure bit on the note's row — never the quarantine and
   never a pass failure.

A third derivation is one more call after `_update_tasks_for_changed`.

**The write-time refresh exception (#428, design D10; #432).** Rule 3 has
exactly one exception: a write-time refresh, by a task tool or by a generic
note writer (`edit_note`, `set_frontmatter`, a `write_file` to a note path,
`delete_note`; #432), through `src.services.task_index.refresh_note_tasks`,
may rewrite one note's task rows outside the pass, under that note's
`notes_metadata` row lock, from the bytes its own call published, or, after
`delete_note`, from the absence it caused, and from nothing else. It records which bytes
the rows describe in `notes_metadata.tasks_source_hash`; the pass reads every
row whose `tasks_source_hash` differs from its `content_hash` and keeps a
refresh's rows only when they were written after its scan began (R2-R4).

## One definition of "a note's tasks" (#428, design D2/D10)

`tasks_from_note_text(raw)` is the frontmatter split, the line offset and the
extraction in one call. The write-time refresh and the write planner call it;
the index pass composes exactly the same three steps over the same canonical
text (it already holds the split body for its other derivations), and a test
pins the two equal. So a task id the planner resolves, the rows a refresh
writes and the rows the pass writes can never disagree about what a note's
tasks are.

## Version 5 (#428, design D1, D9, D11)

Each task also carries its D1 key, ordinal and count (over the **uncut**
text, among the note's kept tasks), the D9 description key, the folded text,
the tag prefixes, the run-based recurrence rule (`task_line.parse_run`, as the
Tasks plugin reads it), the created and cancelled dates (the #350 date rules)
and its stored context run (`_context`).
"""
from __future__ import annotations

import datetime
import re
from dataclasses import dataclass

from src.config import (
    MAX_TASKS_PER_NOTE,
    TASK_CONTEXT_MAX_LINES,
    TASK_MAX_TAGS,
    TASK_TAG_MAX_CHARS,
    TASK_TEXT_MAX_CHARS,
)
from src.services import task_line
from src.services.links import BODY, mask_code
from src.services.vault import _ATX_HEADING_RE, INLINE_TAG_RE, parse_frontmatter

#: The extraction version that introduced task rows. A row stamped below it
#: has not been scanned for tasks yet; `list_tasks` counts such rows in every
#: header rather than reporting "no tasks" for them (design D5).
TASKS_SINCE_EXTRACTION_VERSION = 4

#: The extraction version that introduced task ids and the other version-5
#: columns (#428, design D11). Kept for the operator's backfill progress only:
#: a listing renders an id from the row (`task_key` populated), never from the
#: note's version, so no listing decision reads this.
TASK_IDS_SINCE_EXTRACTION_VERSION = 5

#: The stored context run (D12, `note-tasks`): at most
#: `TASK_CONTEXT_MAX_LINES` lines, each cut to this many characters, joined by
#: `\n` and cut to `TASK_CONTEXT_MAX_CHARS` in total; `context_cut` when any
#: of the three bit. Write placement never uses these (D8).
TASK_CONTEXT_LINE_MAX_CHARS = 160
TASK_CONTEXT_MAX_CHARS = 600
#: `recurrence` is cut at this many characters (`recurrence_cut`).
TASK_RECURRENCE_MAX_CHARS = 256

STATE_OPEN = "open"
STATE_DONE = "done"
STATE_CANCELLED = "cancelled"
STATES = (STATE_OPEN, STATE_DONE, STATE_CANCELLED)
PRIORITIES = ("highest", "high", "medium", "low", "lowest")

HEADING_PART_MAX_CHARS = 200
HEADING_PATH_MAX_CHARS = 512
HEADING_SEPARATOR = " > "

_LINE_BREAK_RE = re.compile(r"\r\n|\n|\r")

# Anchored by `.match(text, pos, endpos)` at each line start, so no `^`.
# Possessive throughout: no class can be re-entered by backtracking.
#: Everything through the closing `]`. Split from the lookahead only so the
#: write-time gate (`may_hold_tasks`) is built from the same string.
_CHECKBOX_PREFIX = (
    r"(?:[ \t]*+>)*+[ \t]*+(?:[-*+]|[0-9]{1,9}[.)])[ \t]++"
    r"\[(?P<s> |[^\s\[\]])\]"
)
_CHECKBOX_LOOKAHEAD = r"(?=[ \t]|$)"
_CHECKBOX_RE = re.compile(_CHECKBOX_PREFIX + _CHECKBOX_LOOKAHEAD)

#: The gate of the generic writers' task refresh (#432, design D2): the
#: checkbox prefix at any line start of the canonical text. The lookahead is
#: deliberately absent: the extractor evaluates it on the MASKED text, where a
#: code span directly after `]` reads as spaces, so the original line need not
#: satisfy it. The prefix itself the original always satisfies (the #218 rule:
#: original and masked are equal through `]`), which is what makes this a
#: superset of the extractor.
_CHECKBOX_GATE_RE = re.compile("^" + _CHECKBOX_PREFIX, re.MULTILINE)
_FOLLOWING_SPACE_RE = re.compile(r"[ \t]*+")

_DATE_TAIL = r"️?(?:[ \t]*+(?P<y>[0-9]{4})-(?P<m>[0-9]{2})-(?P<d>[0-9]{2})(?![0-9]))?"
_DUE_RE = re.compile("[\U0001F4C5\U0001F4C6\U0001F5D3]" + _DATE_TAIL)
_SCHEDULED_RE = re.compile("[⏳⌛]" + _DATE_TAIL)
_START_RE = re.compile("\U0001F6EB" + _DATE_TAIL)
_DONE_RE = re.compile("✅" + _DATE_TAIL)
_CREATED_RE = re.compile("➕" + _DATE_TAIL)
_CANCELLED_RE = re.compile("❌" + _DATE_TAIL)
_PRIORITY_RE = re.compile("(?P<p>[\U0001F53A⏫\U0001F53C\U0001F53D⏬])️?")
_PRIORITY_NAMES = {
    "\U0001F53A": "highest",
    "⏫": "high",
    "\U0001F53C": "medium",
    "\U0001F53D": "low",
    "⏬": "lowest",
}


@dataclass(frozen=True)
class ExtractedTask:
    """One checkbox list item, as `note_tasks` stores it."""

    line: int
    status_char: str
    state: str
    text: str
    text_cut: bool
    due: datetime.date | None
    scheduled: datetime.date | None
    start: datetime.date | None
    done_on: datetime.date | None
    priority: str | None
    tags: list[str] | None
    tags_cut: bool
    heading_path: str | None
    # ── Version 5 (#428). Defaults only so a hand-built instance in a test
    # stays valid; the extractor always sets every one. ────────────────────
    #: D1: 12 hex over the uncut text, and this task's 1-based rank among the
    #: note's kept tasks with the same key / their number.
    task_key: str | None = None
    key_ordinal: int | None = None
    key_count: int | None = None
    #: D9: 16 hex over the normalised plugin description of the uncut text.
    description_key: str | None = None
    #: NFC + casefold of the stored (cut) `text`.
    text_fold: str | None = None
    #: Every normalised `/`-prefix of every stored tag; `[]` with no tags, so
    #: a version-5 row is never NULL here (only a pre-backfill row is).
    tag_prefixes: list[str] | None = None
    #: The run's `🔁` rule as the plugin reads it, trimmed, cut at 256.
    recurrence: str | None = None
    recurrence_cut: bool = False
    created: datetime.date | None = None
    cancelled: datetime.date | None = None
    #: The stored context run, `\n`-joined; None when there is none.
    context: str | None = None
    context_cut: bool = False


def state_for(status_char: str) -> str:
    """Design D2: `x`/`X` done, `-` cancelled, every other marker open."""
    if status_char in ("x", "X"):
        return STATE_DONE
    if status_char == "-":
        return STATE_CANCELLED
    return STATE_OPEN


def _last_date(pattern: re.Pattern, masked: str, start: int, end: int):
    last = None
    for last in pattern.finditer(masked, start, end):
        pass
    if last is None or last.group("y") is None:
        return None
    try:
        return datetime.date(int(last.group("y")), int(last.group("m")), int(last.group("d")))
    except ValueError:
        return None


def _priority(masked: str, start: int, end: int) -> str | None:
    last = None
    for last in _PRIORITY_RE.finditer(masked, start, end):
        pass
    return None if last is None else _PRIORITY_NAMES[last.group("p")]


def _tags(masked_slice: str) -> tuple[list[str] | None, bool]:
    kept: list[str] = []
    seen: set[str] = set()
    cut = False
    for match in INLINE_TAG_RE.finditer(masked_slice):
        tag = match.group(1)
        if len(tag) > TASK_TAG_MAX_CHARS:
            cut = True
            continue
        if tag in seen:
            continue
        if len(kept) >= TASK_MAX_TAGS:
            cut = True
            continue
        seen.add(tag)
        kept.append(tag)
    return (kept or None), cut


def _heading_path(stack: list[tuple[int, str]]) -> str | None:
    if not stack:
        return None
    path = HEADING_SEPARATOR.join(text[:HEADING_PART_MAX_CHARS] for _, text in stack)
    return path[:HEADING_PATH_MAX_CHARS]


def _lines(text: str):
    """`(start, content_end)` of every line, universal-newline rule."""
    pos = 0
    for brk in _LINE_BREAK_RE.finditer(text):
        yield pos, brk.start()
        pos = brk.end()
    yield pos, len(text)


# ── context (D12) ────────────────────────────────────────────────────────────

_BLOCKQUOTE_RE = re.compile(r"(?:[ \t]*+>)*+")
_LEADING_HSPACE_RE = re.compile(r"[ \t]*+")
_HEADING_LINE_RE = re.compile(r"#{1,6}(?:[ \t]|$)")


def _indent_width(whitespace: str) -> int:
    """Indentation width with a tab counting as 4 (D8, D12)."""
    return sum(4 if ch == "\t" else 1 for ch in whitespace)


def _quote_and_indent(line: str) -> tuple[str, int, str]:
    """`(blockquote prefix, indentation width, rest after the indentation)`."""
    quote = _BLOCKQUOTE_RE.match(line).group()
    after = line[len(quote):]
    indent = _LEADING_HSPACE_RE.match(after).group()
    return quote, _indent_width(indent), after[len(indent):]


def _context(body: str, lines: list[tuple[int, int]], index: int) -> tuple[str | None, bool]:
    """The stored context of the task on `lines[index]` (`note-tasks`, D12).

    The maximal run of non-blank lines directly below the task that have a
    greater blockquote depth than the task, or the same blockquote prefix and
    a greater indentation width (tab = 4). The run stops at a blank line, a
    heading, a fence line, the first line that does not belong, or the
    `TASK_CONTEXT_MAX_LINES` cap (a further belonging line sets the cut flag).
    Each line is stripped and cut to `TASK_CONTEXT_LINE_MAX_CHARS`; the joined
    text is cut to `TASK_CONTEXT_MAX_CHARS`. **Storage and rendering only**:
    write placement scans the uncapped continuation block (D8), which crosses
    blank lines.
    """
    start, end = lines[index]
    task_quote, task_indent, _ = _quote_and_indent(body[start:end])
    task_depth = task_quote.count(">")
    parts: list[str] = []
    cut = False
    # Indexed, never sliced: a slice of `lines` per task is quadratic.
    for following in range(index + 1, len(lines)):
        s, e = lines[following]
        quote, indent, rest = _quote_and_indent(body[s:e])
        if not rest.strip():
            break  # a blank line (inside the quote or not)
        if _HEADING_LINE_RE.match(rest) or rest.startswith(("```", "~~~")):
            break
        depth = quote.count(">")
        if not (depth > task_depth or (quote == task_quote and indent > task_indent)):
            break
        if len(parts) >= TASK_CONTEXT_MAX_LINES:
            cut = True
            break
        part = body[s:e].strip()
        if len(part) > TASK_CONTEXT_LINE_MAX_CHARS:
            part = part[:TASK_CONTEXT_LINE_MAX_CHARS]
            cut = True
        parts.append(part)
    if not parts:
        return None, cut
    joined = "\n".join(parts)
    if len(joined) > TASK_CONTEXT_MAX_CHARS:
        joined = joined[:TASK_CONTEXT_MAX_CHARS].rstrip("\n")
        cut = True
    return joined, cut


def _run_derived(full_text: str) -> tuple[str | None, bool, str]:
    """`(recurrence, recurrence_cut, description_key)` from **one** backward
    parse of the uncut text (`task_line.parse_run`, the plugin's R§1.4 parse).

    One parse, not two: `parse_run` costs a bounded number of whole-text
    scans per pass, which dominates extraction on a long line. The two
    derivations below are therefore spelled out over that one `RunParse`, and
    `tests/test_note_tasks_extractor.py` pins them equal to
    `task_line.parse(…).recurrence` (`TaskLine.run_value`: the leftmost run
    token of the family, trimmed) and to
    `task_line.description_key(task_line.normalise_description(text))`, the
    write planner's definitions. `parse(line)` parses exactly this text: the
    line after the box and gap, right-stripped.
    """
    run = task_line.parse_run(full_text)
    rule = next(
        (t.value for t in run.tokens
         if t.kind == "field" and t.family == task_line.RECURRENCE),
        None,
    )
    rule = rule.strip() if rule is not None else None
    if not rule:
        rule, rule_cut = None, False
    elif len(rule) > TASK_RECURRENCE_MAX_CHARS:
        rule, rule_cut = rule[:TASK_RECURRENCE_MAX_CHARS], True
    else:
        rule_cut = False
    tags = run.trailing_tags
    described = run.description + ("" if not tags else " " + " ".join(tags))
    norm = " ".join(task_line.fold_text(described).split())
    return rule, rule_cut, task_line.description_key(norm)


def extract_tasks_bounded(
    body: str, *, line_offset: int, max_tasks: int = MAX_TASKS_PER_NOTE
) -> tuple[list[ExtractedTask], bool]:
    """The tasks of a post-frontmatter `body`, and whether `max_tasks` bit.

    `line_offset` is the number of line terminators in the frontmatter block,
    so `line` counts from the file's first line. `body` is NUL-free by the
    indexer's decode rule (#308); a NUL is a caller bug and raises rather than
    being repaired.
    """
    if "\x00" in body:
        raise ValueError("extract_tasks_bounded: body contains U+0000")
    masked = mask_code(body, context=BODY)
    if len(masked) != len(body):  # the masking invariant; never expected
        raise ValueError("extract_tasks_bounded: masking changed the length")

    headings = [
        (m.start(), len(m.group(1)), m.group(2).strip())
        for m in _ATX_HEADING_RE.finditer(masked)
    ]
    heading_index = 0
    stack: list[tuple[int, str]] = []

    lines = list(_lines(body))
    fields: list[dict] = []
    truncated = False
    for index, (start, end) in enumerate(lines):
        while heading_index < len(headings) and headings[heading_index][0] < start:
            _, depth, text = headings[heading_index]
            while stack and stack[-1][0] >= depth:
                stack.pop()
            stack.append((depth, text))
            heading_index += 1

        m = _CHECKBOX_RE.match(masked, start, end)
        if m is None:
            continue
        marker_end = m.end()
        if body[start:marker_end] != masked[start:marker_end]:
            continue  # the #218 rule: masking decided the marker
        if len(fields) >= max_tasks:
            truncated = True
            break

        text_start = _FOLLOWING_SPACE_RE.match(body, marker_end, end).end()
        full_text = body[text_start:end].rstrip()
        text_cut = len(full_text) > TASK_TEXT_MAX_CHARS
        stored_text = full_text[:TASK_TEXT_MAX_CHARS]
        tags, tags_cut = _tags(masked[text_start:end])
        status_char = m.group("s")
        recurrence, recurrence_cut, desc_key = _run_derived(full_text)
        context, context_cut = _context(body, lines, index)
        fields.append(dict(
            line=index + 1 + line_offset,
            status_char=status_char,
            state=state_for(status_char),
            text=stored_text,
            text_cut=text_cut,
            due=_last_date(_DUE_RE, masked, text_start, end),
            scheduled=_last_date(_SCHEDULED_RE, masked, text_start, end),
            start=_last_date(_START_RE, masked, text_start, end),
            done_on=_last_date(_DONE_RE, masked, text_start, end),
            priority=_priority(masked, text_start, end),
            tags=tags,
            tags_cut=tags_cut,
            heading_path=_heading_path(stack),
            task_key=task_line.task_key(full_text),
            description_key=desc_key,
            text_fold=task_line.fold_text(stored_text),
            tag_prefixes=task_line.tag_prefixes(tags or ()),
            recurrence=recurrence,
            recurrence_cut=recurrence_cut,
            created=_last_date(_CREATED_RE, masked, text_start, end),
            cancelled=_last_date(_CANCELLED_RE, masked, text_start, end),
            context=context,
            context_cut=context_cut,
        ))

    # D1: ordinal-of-count among the kept tasks (document order, the cap).
    counts: dict[str, int] = {}
    for f in fields:
        counts[f["task_key"]] = counts.get(f["task_key"], 0) + 1
    ranks: dict[str, int] = {}
    tasks: list[ExtractedTask] = []
    for f in fields:
        key = f["task_key"]
        ranks[key] = ranks.get(key, 0) + 1
        tasks.append(ExtractedTask(key_ordinal=ranks[key], key_count=counts[key], **f))
    return tasks, truncated


def note_task_row(note_id: int, t: ExtractedTask) -> dict:
    """The `note_tasks` row for one extracted task: **the one mapping** the
    index pass and the write-time refresh both insert, so the two writers'
    rows agree column for column (#428, D10). Every column but the serial id."""
    return {
        "note_id": note_id,
        "line": t.line,
        "status_char": t.status_char,
        "state": t.state,
        "text": t.text,
        "text_cut": t.text_cut,
        "due": t.due,
        "scheduled": t.scheduled,
        "start": t.start,
        "done_on": t.done_on,
        "priority": t.priority,
        "tags": t.tags,
        "tags_cut": t.tags_cut,
        "heading_path": t.heading_path,
        "task_key": t.task_key,
        "key_ordinal": t.key_ordinal,
        "key_count": t.key_count,
        "description_key": t.description_key,
        "text_fold": t.text_fold,
        "tag_prefixes": t.tag_prefixes,
        "recurrence": t.recurrence,
        "recurrence_cut": t.recurrence_cut,
        "created": t.created,
        "cancelled": t.cancelled,
        "context": t.context,
        "context_cut": t.context_cut,
    }


def body_line_offset(frontmatter_block: str) -> int:
    """Line terminators in the frontmatter block, universal-newline rule."""
    return len(_LINE_BREAK_RE.findall(frontmatter_block))


def frontmatter_line_offset(raw: str, body: str) -> int:
    """`body_line_offset` of the block `parse_frontmatter` stripped from `raw`.

    The parser returns the body as a suffix of the raw text (only the block and
    its one closing terminator are removed), so the block is the prefix. A body
    that is not a suffix — never expected — counts from line 1.
    """
    if len(body) >= len(raw) or not raw.endswith(body):
        return 0
    return body_line_offset(raw[: len(raw) - len(body)])


def _canonical_text(raw: str | bytes) -> str:
    """The indexer's canonical text of a note (`note_hash`, #351 D3): a strict
    UTF-8 decode for bytes, then CRLF and lone CR to LF and every U+0000
    removed. Idempotent, so the pass's already-canonical text is unchanged."""
    if isinstance(raw, (bytes, bytearray, memoryview)):
        from src.services.note_hash import canonical_note_text_strict

        return canonical_note_text_strict(bytes(raw))
    if "\r" in raw:
        raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    if "\x00" in raw:
        raw = raw.replace("\x00", "")
    return raw


def tasks_from_note_text(
    raw: str | bytes, *, max_tasks: int = MAX_TASKS_PER_NOTE
) -> tuple[list[ExtractedTask], bool]:
    """A whole note's tasks, and whether `max_tasks` bit (#428, D2 step 8, D10).

    **The one definition** of a note's tasks: the canonical text (the text
    the index hashes as `content_hash`), `parse_frontmatter`, the frontmatter
    line offset, then `extract_tasks_bounded` over the body. The write-time
    refresh and the write planner call this; the index pass composes the same
    three steps over the same canonical text. Line numbers count from the
    file's first line, and terminators are split by the universal-newline
    rule either way, so a CRLF note numbers its lines as the index does.

    `raw` is the note's bytes or its decoded text. Bytes that are not UTF-8
    raise `UnicodeDecodeError` (the index will not index them either).
    """
    text = _canonical_text(raw)
    _frontmatter, body = parse_frontmatter(text)
    return extract_tasks_bounded(
        body, line_offset=frontmatter_line_offset(text, body), max_tasks=max_tasks
    )


def may_hold_tasks(data: bytes) -> bool:
    """Whether `data` may hold a task (#432, design D2): the generic writers'
    refresh gate.

    True when `data` is not UTF-8 (the refresh then reports `not_utf8`), else
    whether the extractor's checkbox prefix matches at any line start of the
    canonical text, frontmatter included. A **superset** of
    `tasks_from_note_text`: every input on which it finds a task makes this
    true (pinned by a property test). It over-matches checkboxes in code and
    in frontmatter, which costs a refresh, never a wrong answer. Linear time
    (possessive quantifiers); reads nothing but its argument.
    """
    from src.services.note_hash import canonical_note_text

    text = canonical_note_text(bytes(data))
    if text is None:
        return True
    return _CHECKBOX_GATE_RE.search(text) is not None
