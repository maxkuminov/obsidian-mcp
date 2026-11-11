"""Task write planning (#428, design D2–D9, D16).

Everything between "the note's current text" and "the bytes to publish", as
pure plans, plus one database query (`duplicate_scan`). This module does
**not** publish, touch the filesystem, or refresh the index: the wiring in
`tools.py` reads the note through the anchored descriptor, calls a planner
here, verifies, publishes with `write_file_at(…, expected=existing_bytes)`,
and refreshes the task rows (D2 steps 5–16).

## Contract

* A plan is a list of `Span`s over the ORIGINAL text — never a regenerated
  note. Every byte outside the spans is the original byte (D4, D5).
* Every planner re-derives the tasks from the text it is handed, with the one
  shared extraction function (`tasks.tasks_from_note_text`, over the
  canonical text; `_NoteView` reads the same partition and mask), and carries nothing from one call to the next: the wiring's
  retry-on-conflict loop (D2, up to 3 attempts) simply calls again on the
  fresh bytes.
* A refusal (`TaskWriteRefusal`) writes nothing. When in doubt the planner
  refuses with the specific code; it never guesses.
* `verify` (D5) is the last line of defence. It re-derives everything it
  checks from the two texts — the line structure, both extractions, both
  owner maps, the code masks and the headings — and trusts the plan only for
  what the plan *declares* (its spans, the target line, the intent of the
  call and the declared new lines). Every planner calls it before returning,
  and the wiring may call it again; it is pure and cheap to repeat.

## The continuation block (D8)

A task's continuation block is every following line that has a greater
blockquote depth than the task, or the same depth and a greater indentation
width (a tab counts as 4), measured after the blockquote prefix (one optional
space after the last ``>`` belongs to the prefix). Blank lines (whitespace
only after the blockquote prefix) are crossed while a later non-blank line
still belongs; the block ends at its last belonging non-blank line. There is
no line or character cap: placement (`add_task`, `append_context`, the
below-placed next occurrence) always uses the whole block, so no insertion
can land inside another task's children, and D5 check 5 proves that no
pre-existing line changes its owning task.
"""
from __future__ import annotations

import bisect
import dataclasses
import datetime
import functools
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from src.config import TASK_SIMILAR_SCAN_MAX
from src.services import refusals
from src.services import task_line as tl_mod
from src.services import task_rule
from src.services.links import BODY, mask_code, unmatched_indented_openers
from src.services.task_line import (
    CANCELLED,
    CREATED,
    DATE_FAMILIES,
    DEPENDS_ON,
    DONE,
    DUE,
    ID,
    ON_COMPLETION,
    PRIORITY,
    RECURRENCE,
    SCHEDULED,
    START,
    LineChanges,
    LineEdit,
    TaskLineRefusal,
)
from src.services.task_recurrence import Rule, Unsupported, next_occurrence, parse_rule
from src.services import tasks as tasks_mod
from src.services.tasks import STATE_OPEN
from src.services.vault import (
    _ATX_HEADING_RE,
    _resolve_section_index,
    _scan_headings,
    _section_body_span,
    parse_frontmatter,
    parse_frontmatter_diagnose,
)

# ── the shared extraction ──────────────────────────────────────────


_LINE_BREAK_RE = re.compile(r"\r\n|\n|\r")


# ── data ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TaskRef:
    """A task named in a refusal or a duplicate listing. ``id`` is None for
    an index row whose key is still pending (D11). ``path`` is set by
    `duplicate_scan`; the pure planners leave it None (the wiring knows the
    path)."""

    line: int
    id: str | None
    text: str
    path: str | None = None
    score: float | None = None


@dataclass(frozen=True)
class TaskWriteRefusal:
    """An expected refusal: nothing may be written.

    ``code`` is one of `refusals.CODES`. ``matches`` lists the tasks a
    ``task_ambiguous`` or ``duplicate_task`` refusal names. ``detail`` is set
    for ``content_unsafe`` (the section-write defect refusals, D8) as
    ``("frontmatter_defect", diagnosis)`` or
    ``("unmatched_fence", scan_text, diagnosis)`` so the wiring can render the
    same prose `edit_note` does.
    """

    code: str
    reason: str
    matches: tuple[TaskRef, ...] = ()
    detail: object = None

    def __post_init__(self) -> None:
        if self.code not in refusals.CODES:
            raise ValueError(f"undeclared refusal code {self.code!r}")


@dataclass(frozen=True)
class Span:
    """Replace ``old[start:end]`` with ``text`` (whole-note coordinates)."""

    start: int
    end: int
    text: str


@dataclass(frozen=True)
class NewLine:
    """A line the plan writes, in the NEW text. ``kind`` is ``target`` (the
    edited task line), ``next`` (a next occurrence), ``task`` (an added task)
    or ``context`` (a context line). ``line`` is 1-based."""

    kind: str
    line: int
    text: str


@dataclass(frozen=True)
class Expect:
    """What a declared new task line must re-extract as (D5 check 2).
    ``fields`` maps every family in `_CHECKED_FAMILIES` to its value text
    (ISO date, priority name, trimmed rule text) or None for absent."""

    status: str
    fields: Mapping[str, str | None]
    tags: frozenset[str]


@dataclass(frozen=True)
class Inserted:
    """A declared inserted line, in new-text order. ``owner`` is the planned
    owning task: ``("old", n)`` (old 1-based line), ``("new", k)`` (the k-th
    inserted line) or None."""

    kind: str
    text: str
    expect: Expect | None
    owner: tuple[str, int] | None


@dataclass(frozen=True)
class Intent:
    """The change a call makes to the target line, as the call states it
    (not as the planner composed it): `verify` derives the target's expected
    fields from the old line and this."""

    status: str | None = None
    set_fields: Mapping[str, str] = field(default_factory=dict)
    remove_fields: frozenset[str] = frozenset()
    add_tags: tuple[str, ...] = ()
    remove_tags: tuple[str, ...] = ()
    typed_tags: tuple[str, ...] = ()
    description: str | None = None


@dataclass(frozen=True)
class Plan:
    """A verified plan. ``changed`` False means "No changes" (no spans, no
    publication, no event, no refresh). ``new_ids`` maps ``target`` /
    ``next`` / ``task`` to the rendered id in the new text."""

    spans: tuple[Span, ...]
    new_text: str
    new_lines: tuple[NewLine, ...]
    new_ids: Mapping[str, str]
    changed: bool = True
    target_line: int | None = None
    intent: Intent | None = None
    inserted: tuple[Inserted, ...] = ()
    # Response metadata.
    old_line: str | None = None
    replaced_status: str | None = None
    position: str | None = None
    fields_changed: tuple[str, ...] = ()
    norm: str | None = None
    terms: tuple[str, ...] = ()
    #: #434 D7: the global filter text `plan_add` prepended, or None.
    filter_added: str | None = None
    #: #434 D7: under a `filter` rule, every normalised description an
    #: index-side exact duplicate may have (`duplicate_scan(extra_norms=…)`);
    #: empty under any other rule.
    dup_norms: tuple[str, ...] = ()


@dataclass(frozen=True)
class CompleteArgs:
    """`complete_task`. ``done_on`` is already resolved (D19)."""

    id: str
    done_on: datetime.date
    hash_supplied: bool = False


@dataclass(frozen=True)
class UpdateArgs:
    """`update_task`. Dates are already resolved (D19). ``clear`` names any
    of ``due``, ``scheduled``, ``start``, ``priority``, ``recurrence``."""

    id: str
    hash_supplied: bool = False
    due: datetime.date | None = None
    scheduled: datetime.date | None = None
    start: datetime.date | None = None
    priority: str | None = None
    add_tags: tuple[str, ...] = ()
    remove_tags: tuple[str, ...] = ()
    recurrence: str | None = None
    status: str | None = None
    text: str | None = None
    append_context: str | None = None
    clear: frozenset[str] = frozenset()


@dataclass(frozen=True)
class AddArgs:
    """`add_task`. Dates are already resolved (D19)."""

    text: str
    section: str | None = None
    due: datetime.date | None = None
    scheduled: datetime.date | None = None
    start: datetime.date | None = None
    priority: str | None = None
    tags: tuple[str, ...] = ()
    recurrence: str | None = None
    context: str | None = None
    allow_duplicate: bool = False


_CHECKED_FAMILIES = (DUE, SCHEDULED, START, DONE, CREATED, CANCELLED, PRIORITY, RECURRENCE)
_CLEARABLE = {"due": DUE, "scheduled": SCHEDULED, "start": START, "priority": PRIORITY, "recurrence": RECURRENCE}
_STATUS_CHARS = {"open": " ", "in_progress": "/", "cancelled": "-"}
_DUPLICATE_LIST_MAX = 5
SIMILAR_MIN_SCORE = 0.5
SIMILAR_CONTAINMENT_MIN_TERMS = 2
SIMILAR_LIST_MAX = 5
SIMILAR_TERMS_MAX = 8


def _refuse(code: str, reason: str, **kw) -> TaskWriteRefusal:
    return TaskWriteRefusal(code, reason, **kw)


def _from_line_refusal(r: TaskLineRefusal) -> TaskWriteRefusal:
    return TaskWriteRefusal(r.code, r.reason)


# ── the note view ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Line:
    start: int
    end: int
    term: str


class _NoteView:
    """A whole note split under the universal-newline rule, with the body
    partition and code mask the extractor uses.

    The partition and the mask are derived from the CANONICAL text — the
    one `tasks.tasks_from_note_text` extracts from (every terminator → LF;
    NUL notes are refused before a view is built) — so the planner and the
    extractor read the same lines the same way. Canonicalisation rewrites
    only terminators, so line ``i``'s content and length are identical in
    both texts and positions map line by line; edits are spans over the
    ORIGINAL text, whose terminators are untouched."""

    def __init__(self, text: str) -> None:
        if "\x00" in text:
            raise ValueError("task_write: the note contains NUL")
        self.text = text
        lines: list[_Line] = []
        pos = 0
        for m in _LINE_BREAK_RE.finditer(text):
            lines.append(_Line(pos, m.start(), m.group()))
            pos = m.end()
        lines.append(_Line(pos, len(text), ""))
        self.lines = lines
        canon = "\n".join(text[ln.start : ln.end] for ln in lines)
        if canon != tasks_mod._canonical_text(text):
            raise ValueError("task_write: canonical text does not map line for line")
        canon_starts = []
        p = 0
        for ln in lines:
            canon_starts.append(p)
            p += ln.end - ln.start + 1
        self._canon_starts = canon_starts
        _fm, body = parse_frontmatter(canon)
        if len(body) > len(canon) or not canon.endswith(body):
            raise ValueError("task_write: the body is not a suffix of the note")
        offset = len(canon) - len(body)
        self.body_first = canon[:offset].count("\n")
        if body == "" and offset == len(canon) and canon_starts[-1] != len(canon):
            # A frontmatter block that ends the file without a terminator:
            # the body is empty and has no line of its own.
            self.body_first = len(lines)
            self.body_offset = len(text)
        elif canon_starts[self.body_first] != offset:
            raise ValueError("task_write: the body does not start at a line start")
        else:
            self.body_offset = lines[self.body_first].start
        self._canon_body_offset = offset
        self.masked = mask_code(body, context=BODY)
        if len(self.masked) != len(body):
            raise ValueError("task_write: masking changed the length")
        self.first_term = next((ln.term for ln in lines if ln.term), "")

    def content(self, i: int) -> str:
        ln = self.lines[i]
        return self.text[ln.start : ln.end]

    def contents(self) -> list[str]:
        return [self.text[ln.start : ln.end] for ln in self.lines]

    def masked_line(self, i: int) -> str:
        ln = self.lines[i]
        if i < self.body_first:
            return self.content(i)
        start = self._canon_starts[i] - self._canon_body_offset
        return self.masked[start : start + ln.end - ln.start]

    def parse(self, i: int):
        return tl_mod.parse(self.content(i), masked=self.masked_line(i))

    def term_for(self, i: int | None) -> str:
        """D7/D8 ``T``: line ``i``'s own terminator, else the note's first
        terminator, else LF."""
        if i is not None and self.lines[i].term:
            return self.lines[i].term
        return self.first_term or "\n"


def _view(text: str) -> _NoteView | TaskWriteRefusal:
    if "\x00" in text:
        return _refuse("task_malformed", "the note contains a NUL character; task tools do not edit it")
    try:
        return _NoteView(text)
    except ValueError as e:
        return _refuse("task_malformed", str(e))


def _extract(text: str) -> tuple[tuple, bool] | TaskWriteRefusal:
    try:
        return _extract_cached(text)
    except ValueError as e:
        return _refuse("task_malformed", str(e))


@functools.lru_cache(maxsize=8)
def _extract_cached(text: str) -> tuple[tuple, bool]:
    """`tasks.tasks_from_note_text` (the one shared extraction), memoised on the exact text. The function is
    pure, so this only saves the repeated extractions of one call (plan,
    verify, ids); it carries no decision between calls."""
    tasks, truncated = tasks_mod.tasks_from_note_text(text)
    return tuple(tasks), truncated


# ── shapes, continuation blocks, owners (D8, D5 check 5) ─────────────────────

_BQ_RE = re.compile(r"(?:[ \t]*+>)*+")


def _shape(line: str) -> tuple[int, int, bool]:
    """``(blockquote depth, indentation width, blank)`` of one line (D8)."""
    bq_end = _BQ_RE.match(line).end()
    depth = line.count(">", 0, bq_end)
    pos = bq_end
    if depth and line.startswith(" ", pos):
        pos += 1
    width = 0
    n = len(line)
    while pos < n and line[pos] in " \t":
        width += 4 if line[pos] == "\t" else 1
        pos += 1
    return depth, width, pos == n


def _belongs(task: tuple[int, int, bool], line: tuple[int, int, bool]) -> bool:
    return line[0] > task[0] or (line[0] == task[0] and line[1] > task[1])


def continuation_block(lines: Sequence[str], i: int) -> int:
    """D8: the EXCLUSIVE end index of task line ``i``'s continuation block.

    ``lines`` are line contents without terminators (0-based); the block is
    ``lines[i + 1 : end]`` (empty when ``end == i + 1``). Uncapped: blank
    lines are crossed while a later non-blank line still belongs, and the
    block ends at its last belonging non-blank line.
    """
    task = _shape(lines[i])
    end = i + 1
    for j in range(i + 1, len(lines)):
        shape = _shape(lines[j])
        if shape[2]:
            continue
        if not _belongs(task, shape):
            break
        end = j + 1
    return end


def _owners(lines: Sequence[str], task_lines: set[int]) -> list[int | None]:
    """Each line's owning task (0-based index) or None: the nearest preceding
    task whose continuation block contains it. One pass with a stack —
    blocks nest (a line belonging to an inner task belongs to every task
    whose block holds that task), so the innermost open block is the owner.
    Blank lines get the owner of the block that continues past them."""
    owners: list[int | None] = [None] * len(lines)
    stack: list[tuple[int, tuple[int, int, bool]]] = []
    pending: list[int] = []
    for j, line in enumerate(lines):
        shape = _shape(line)
        if shape[2]:
            pending.append(j)
            continue
        while stack and not _belongs(stack[-1][1], shape):
            stack.pop()
        top = stack[-1][0] if stack else None
        for p in pending:
            owners[p] = top
        pending.clear()
        owners[j] = top
        if j in task_lines:
            stack.append((j, shape))
    return owners


# ── resolution (D2) ──────────────────────────────────────────────────────────


def _render(t) -> str:
    return tl_mod.render_id(t.task_key, t.key_ordinal, t.key_count)


def resolve(tasks: Sequence, id: str, *, hash_supplied: bool):
    """D2: the task ``id`` names among ``tasks`` (the current bytes'
    extraction, document order), or a `TaskWriteRefusal`.

    * malformed id → ``invalid_argument``;
    * a suffixed id without a supplied ``expected_hash`` →
      ``precondition_required`` (in every mode; checked before matching);
    * no task with the key → ``task_not_found``;
    * a bare key matching several → ``task_ambiguous`` (``matches`` lists
      the suffixed ids with their lines);
    * a suffixed id whose count differs → ``task_changed``;
    * otherwise the task (the i-th occurrence for a suffixed id).
    """
    parsed = tl_mod.parse_id(id)
    if parsed is None:
        return _refuse("invalid_argument", "id is not a task id (12 hex characters, optionally .<i>of<n>)")
    if parsed.suffixed and not hash_supplied:
        return _refuse(
            "precondition_required",
            "a suffixed task id names an occurrence among identical lines and is "
            "accepted only with expected_hash: read_note the note and pass its "
            "content_hash as expected_hash",
        )
    matches = [t for t in tasks if t.task_key == parsed.key]
    if not matches:
        return _refuse("task_not_found", "no task in the note's current text has this id; list the tasks again")
    if not parsed.suffixed:
        if len(matches) == 1:
            return matches[0]
        refs = tuple(
            TaskRef(line=t.line, id=tl_mod.render_id(parsed.key, k, len(matches)), text=t.text)
            for k, t in enumerate(matches, start=1)
        )
        return _refuse(
            "task_ambiguous",
            f"{len(matches)} identical tasks share this key; name one by its suffixed "
            "id, which needs expected_hash (read_note's content_hash)",
            matches=refs,
        )
    if parsed.count != len(matches):
        return _refuse(
            "task_changed",
            f"the id counts {parsed.count} identical tasks but the note now holds {len(matches)}",
        )
    chosen = matches[parsed.ordinal - 1]
    if chosen.key_ordinal != parsed.ordinal:  # belt: ordinals are document order
        return _refuse("task_changed", "the task ordinals do not match the note's current text")
    return chosen


# ── shared planning helpers ──────────────────────────────────────────────────


def _locate(text: str, id: str, hash_supplied: bool):
    """View, extraction and resolved target (with its parsed line). The id
    grammar and the suffixed-id precondition are checked before the text is
    looked at (D2: a suffixed id without a hash is refused at entry)."""
    parsed_id = tl_mod.parse_id(id)
    if parsed_id is None or (parsed_id.suffixed and not hash_supplied):
        return resolve((), id, hash_supplied=hash_supplied)
    view = _view(text)
    if isinstance(view, TaskWriteRefusal):
        return view
    bad = _frontmatter_refusal(text)
    if bad is not None:
        return bad
    got = _extract(text)
    if isinstance(got, TaskWriteRefusal):
        return got
    tasks, truncated = got
    if truncated:
        return _refuse(
            "size_limit",
            "the note holds more tasks than the per-note task cap, so a task id "
            "cannot be verified against it",
        )
    target = resolve(tasks, id, hash_supplied=hash_supplied)
    if isinstance(target, TaskWriteRefusal):
        return target
    i = target.line - 1
    parsed = view.parse(i)
    if parsed is None or tl_mod.task_key(parsed.full_text) != target.task_key:
        return _refuse("task_malformed", "the task line does not re-parse as the task the id names")
    return view, tasks, target, i, parsed


def _line_spans(view: _NoteView, i: int, edit: LineEdit) -> list[Span]:
    base = view.lines[i].start
    return [Span(base + e.start, base + e.end, e.text) for e in edit.edits if not (e.start == e.end and e.text == "")]


def _insertion(view: _NoteView, after: int | None, lines: Sequence[str], term: str) -> Span:
    """Insert whole ``lines`` after line ``after`` (0-based), or at the body
    start when ``after`` is None. A file without a final terminator keeps
    none (``T`` is then prepended instead of appended), and an empty file
    becomes the lines themselves."""
    text = view.text
    if after is None:
        pos = view.body_offset
    else:
        ln = view.lines[after]
        pos = ln.end + len(ln.term)
    if pos == len(text) and (text == "" or not text.endswith(("\n", "\r"))):
        if text == "":
            return Span(pos, pos, term.join(lines))
        return Span(pos, pos, "".join(term + line for line in lines))
    return Span(pos, pos, "".join(line + term for line in lines))


def _compose(text: str, spans: Sequence[Span]) -> str:
    out = []
    pos = 0
    for s in spans:
        if s.start < pos or s.end < s.start:
            raise ValueError("task_write: overlapping spans")
        out.append(text[pos : s.start])
        out.append(s.text)
        pos = s.end
    out.append(text[pos:])
    return "".join(out)


_LEAD_RE = re.compile(r"(?:[ \t]*+>)*+[ \t]*+")
_LIST_ITEM_RE = re.compile(r"(?:[ \t]*+>)*+[ \t]*+(?:[-*+]|[0-9]{1,9}[.)])(?:[ \t]|$)")


def _note_nests_with_tabs(view: _NoteView) -> bool:
    """Whether the note's nested list items (indentation width > 0 after the
    blockquote prefix) are indented with tabs."""
    for i in range(view.body_first, len(view.lines)):
        masked = view.masked_line(i)
        if not _LIST_ITEM_RE.match(masked):
            continue
        line = view.content(i)
        bq_end = _BQ_RE.match(line).end()
        indent = line[bq_end:_LEAD_RE.match(line).end()]
        if "\t" in indent:
            return True
    return False


def _context_line(view: _NoteView, i: int | None, task_line: str, text: str) -> str:
    """D8 (as amended by the S5 audit): the context line under task line
    ``i`` (None for a task this plan adds, which has no children).

    * The task has direct child list items in its continuation block: the
      first shallowest one's exact leading text (blockquote prefix and
      indentation) is copied, so the new bullet is their sibling and owned by
      the task.
    * Otherwise: the task's own leading text plus one unit — a tab when the
      task's indentation or the note's nested list items use tabs, else four
      spaces.

    Inserted after the full continuation block; D5 check 5 proves the owner.
    """
    if i is not None:
        contents = view.contents()
        depth = _shape(task_line)[0]
        best = None
        for j in range(i + 1, continuation_block(contents, i)):
            if not _LIST_ITEM_RE.match(view.masked_line(j)):
                continue
            shape = _shape(contents[j])
            if shape[0] != depth:
                continue
            if best is None or shape[1] < best[0]:
                best = (shape[1], j)
        if best is not None:
            line = contents[best[1]]
            return line[:_LEAD_RE.match(line).end()] + "- " + text
    lead_end = _LEAD_RE.match(task_line).end()
    bq_end = _BQ_RE.match(task_line).end()
    tabbed = "\t" in task_line[bq_end:lead_end] or _note_nests_with_tabs(view)
    return task_line[:lead_end] + ("\t" if tabbed else "    ") + "- " + text


def _structure_refusal(text: str, *, context: bool, what: str) -> TaskWriteRefusal | None:
    """Caller text that would smuggle structure into the note (S5 audit
    item 4), for a description (`add_task` / `update_task` ``text``) and a
    context line (`append_context`, `add_task(context=…)`): an odd number of
    ``%%`` (an Obsidian comment left open), an HTML comment opener without
    its closer on the same text (or a closer without an opener), a run of
    three or more backticks or tildes (a fence); and, for a context line,
    text that begins with a list marker, a checkbox, a heading marker or
    ``>``. The one check every entry point uses; its rules live in
    `task_rule.structure_problem`, which the global filter validator (#434
    D9) shares."""
    problem = task_rule.structure_problem(text, context=context)
    if problem is None:
        return None
    return _refuse("invalid_argument", f"{what} contains {problem}; nothing was written")


def _validate_context(text) -> str | TaskWriteRefusal:
    got = tl_mod.validate_description(text)
    if isinstance(got, TaskLineRefusal):
        return _refuse("invalid_argument", "context: " + got.reason)
    bad = _structure_refusal(got, context=True, what="context")
    return bad if bad is not None else got


def _frontmatter_refusal(text: str) -> TaskWriteRefusal | None:
    """S5 audit item 1: a task write never touches a note whose frontmatter
    the server cannot delimit. The exact diagnosis `edit_note(section=…)`
    refuses on (`parse_frontmatter_diagnose(...).defect`)."""
    _fm, _body, diagnosis = parse_frontmatter_diagnose(text)
    if diagnosis.defect is not None:
        return _refuse("content_unsafe", "the note has a malformed frontmatter block",
                       detail=("frontmatter_defect", diagnosis))
    return None


def _typed_tags(text: str) -> tuple[str, ...]:
    masked = mask_code(text, context=BODY)
    return tuple(m.tag for m in tl_mod._tag_matches(text, masked, 0, len(text)))


def _finish(old: str, spans: list[Span], **plan_fields) -> Plan | TaskWriteRefusal:
    """Compose, compute new lines and ids, verify."""
    try:
        new_text = _compose(old, spans)
    except ValueError as e:
        return _refuse("task_malformed", str(e))
    got = _extract(new_text)
    if isinstance(got, TaskWriteRefusal):
        return got
    new_tasks, truncated = got
    if truncated:
        return _refuse("size_limit", "the result would hold more tasks than the per-note task cap")
    plan = Plan(spans=tuple(spans), new_text=new_text, new_lines=(), new_ids={}, **plan_fields)
    mapping = _line_map(old, plan)
    if isinstance(mapping, TaskWriteRefusal):
        return mapping
    old_to_new, inserted_lines = mapping
    by_line = {t.line: t for t in new_tasks}
    try:
        new_view = _NoteView(new_text)
    except ValueError as e:
        return _refuse("task_malformed", str(e))
    new_lines: list[NewLine] = []
    ids: dict[str, str] = {}
    if plan.target_line is not None:
        n = old_to_new[plan.target_line - 1] + 1
        new_lines.append(NewLine("target", n, new_view.content(n - 1)))
        if n in by_line:
            ids["target"] = _render(by_line[n])
    for k, idx in enumerate(inserted_lines):
        n = idx + 1
        kind = plan.inserted[k].kind if k < len(plan.inserted) else "?"
        new_lines.append(NewLine(kind, n, new_view.content(idx)))
        if kind in ("next", "task") and n in by_line:
            ids[kind] = _render(by_line[n])
    plan = dataclasses.replace(plan, new_lines=tuple(new_lines), new_ids=ids)
    problem = verify(old, new_text, plan)
    if problem is not None:
        return problem
    return plan


# ── complete_task (D6, D7) ───────────────────────────────────────────────────


def plan_complete(note_text: str, args: CompleteArgs, settings, today: datetime.date) -> Plan | TaskWriteRefusal:
    """D6/D7: mark the task done (``[x]``, plus ``✅ <done_on>`` when
    ``settings.set_done_date``) and, for a recurring task, write its next
    occurrence in the same plan — above the completed line, or after the
    completed task's full continuation block when
    ``settings.next_recurrence_below``. ``today`` (the caller's timezone)
    dates the next line's ``➕`` when ``settings.set_created_date``."""
    got = _locate(note_text, args.id, args.hash_supplied)
    if isinstance(got, TaskWriteRefusal):
        return got
    view, _tasks, target, i, tl = got
    if tl.status in ("x", "X"):
        return _refuse("already_completed", "the task is already completed")
    if tl.status == "-":
        return _refuse("task_cancelled", "the task is cancelled; reopen it with update_task(status=\"open\") first")
    for tok in tl.run_fields(ON_COMPLETION):
        if tok.value.strip().lower() == "delete":
            return _refuse(
                "on_completion_unsupported",
                "the task's 🏁 delete would delete the line on completion; this server never deletes a line implicitly",
            )

    recurring = tl.family_count(RECURRENCE) > 0 or bool(tl.run_fields(RECURRENCE))
    shift = None
    rule = None
    if recurring:
        bad = tl_mod.check_families(tl, {RECURRENCE} | set(DATE_FAMILIES))
        if bad is not None:
            return _from_line_refusal(bad)
        rule = parse_rule(tl.recurrence or "")
        if isinstance(rule, Unsupported) or not isinstance(rule, Rule):
            return _refuse("recurrence_unsupported", f"the recurrence rule is outside the supported grammar ({getattr(rule, 'reason', 'unrecognised')})")
        shift = next_occurrence(
            rule,
            due=tl_mod.field_date(tl, DUE),
            scheduled=tl_mod.field_date(tl, SCHEDULED),
            start=tl_mod.field_date(tl, START),
            done_on=args.done_on,
        )
        if isinstance(shift, Unsupported):
            return _refuse("recurrence_unsupported", f"the next occurrence cannot be computed ({shift.reason})")

    set_fields = {DONE: args.done_on} if settings.set_done_date else {}
    done = tl_mod.apply_changes(tl, LineChanges(
        status="x",
        set_fields=set_fields,
        strict_families=frozenset(DATE_FAMILIES) if recurring else frozenset(),
    ))
    if isinstance(done, TaskLineRefusal):
        return _from_line_refusal(done)
    intent = Intent(status="x", set_fields={DONE: args.done_on.isoformat()} if settings.set_done_date else {})
    spans: list[Span] = []
    inserted: list[Inserted] = []
    position = None
    if shift is not None:
        created = today if settings.set_created_date else None
        nxt = tl_mod.next_occurrence_line(tl, shift.as_mapping(), created=created)
        if isinstance(nxt, TaskLineRefusal):
            return _from_line_refusal(nxt)
        owners = _owners(view.contents(), {t.line - 1 for t in _tasks})
        owner = owners[i]
        expect_fields = {f: _value_text(tl, f) for f in _CHECKED_FAMILIES}
        for fam, value in ((DUE, shift.due), (SCHEDULED, shift.scheduled), (START, shift.start)):
            expect_fields[fam] = value.isoformat() if value is not None else None
        expect_fields[DONE] = None
        expect_fields[CANCELLED] = None
        expect_fields[CREATED] = created.isoformat() if created is not None else None
        expect = Expect(" ", expect_fields, frozenset(t.lower() for t in tl.tags))
        term = view.term_for(i)
        if settings.next_recurrence_below:
            position = "below"
            end = continuation_block(view.contents(), i)
            line_spans = _line_spans(view, i, done)
            ins = _insertion(view, end - 1, [nxt.line], term)
            spans = line_spans + [ins]
        else:
            position = "above"
            pos = view.lines[i].start
            spans = [Span(pos, pos, nxt.line + term)] + _line_spans(view, i, done)
        inserted.append(Inserted("next", nxt.line, expect, None if owner is None else ("old", owner + 1)))
    else:
        spans = _line_spans(view, i, done)
    return _finish(
        note_text, spans,
        target_line=target.line, intent=intent, inserted=tuple(inserted),
        old_line=tl.line, replaced_status=tl.status, position=position,
        fields_changed=("status",) + (("done",) if settings.set_done_date else ()),
    )


def _value_text(tl, family: str) -> str | None:
    found = tl.run_fields(family)
    if not found:
        return None
    v = found[0].value
    return v.strip() if family == RECURRENCE else v


# ── update_task (D4, D6, D8) ─────────────────────────────────────────────────


def plan_update(note_text: str, args: UpdateArgs, settings, today: datetime.date) -> Plan | TaskWriteRefusal:
    """D4/D6/D8: change fields, status, description or context of one task;
    never complete it. ``today`` dates ``❌`` on cancellation when
    ``settings.set_cancelled_date``. A call that changes no byte returns a
    `Plan` with ``changed=False``."""
    if args.status == "done":
        return _refuse("invalid_argument", "update_task cannot complete a task: use complete_task")
    if args.status is not None and args.status not in _STATUS_CHARS:
        return _refuse("invalid_argument", "status must be open, in_progress or cancelled")
    clear = set(args.clear or ())
    unknown = clear - set(_CLEARABLE)
    if unknown:
        return _refuse("invalid_argument", f"clear names an unknown field {sorted(unknown)[0]!r}")
    set_fields: dict[str, object] = {}
    for name, value in (("due", args.due), ("scheduled", args.scheduled), ("start", args.start),
                        ("priority", args.priority), ("recurrence", args.recurrence)):
        if value is not None:
            if name in clear:
                return _refuse("invalid_argument", f"{name} is both set and cleared")
            set_fields[_CLEARABLE[name]] = value
    if args.priority is not None and args.priority not in tl_mod.PRIORITY_SYMBOLS:
        return _refuse("invalid_argument", "priority must be highest, high, medium, low or lowest")
    if args.recurrence is not None:
        rule = parse_rule(args.recurrence) if isinstance(args.recurrence, str) else None
        if not isinstance(rule, Rule):
            return _refuse("recurrence_unsupported", "the recurrence rule is outside the supported grammar")
    for tags in (args.add_tags, args.remove_tags):
        if len(tags) > tl_mod.MAX_NEW_TASK_TAGS:
            return _refuse("invalid_argument", f"at most {tl_mod.MAX_NEW_TASK_TAGS} tags")
    if args.text is not None:
        desc = tl_mod.validate_description(args.text)
        if isinstance(desc, str):
            bad = _structure_refusal(desc, context=False, what="text")
            if bad is not None:
                return bad
    context = None
    if args.append_context is not None:
        context = _validate_context(args.append_context)
        if isinstance(context, TaskWriteRefusal):
            return context
    if not (set_fields or clear or args.status is not None or args.add_tags or args.remove_tags
            or args.text is not None or context is not None):
        return _refuse("invalid_argument", "update_task needs at least one change")

    got = _locate(note_text, args.id, args.hash_supplied)
    if isinstance(got, TaskWriteRefusal):
        return got
    view, tasks, target, i, tl = got

    remove = {_CLEARABLE[n] for n in clear}
    status = None
    if args.status is not None:
        status = _STATUS_CHARS[args.status]
        if args.status in ("open", "in_progress"):
            remove |= {DONE, CANCELLED}
        else:
            remove.add(DONE)
            if settings.set_cancelled_date and tl.family_count(CANCELLED) == 0:
                set_fields[CANCELLED] = today
    edit = tl_mod.apply_changes(tl, LineChanges(
        status=status,
        set_fields=set_fields,
        remove_fields=frozenset(remove),
        description=args.text,
        add_tags=tuple(args.add_tags),
        remove_tags=tuple(args.remove_tags),
    ))
    if isinstance(edit, TaskLineRefusal):
        return _from_line_refusal(edit)

    norm_add = tuple(t[1:] if t.startswith("#") else t for t in args.add_tags)
    norm_remove = tuple(t[1:] if t.startswith("#") else t for t in args.remove_tags)
    desc = tl_mod.validate_description(args.text) if args.text is not None else None
    intent = Intent(
        status=status,
        set_fields={f: _value_text_of(f, v) for f, v in set_fields.items()},
        remove_fields=frozenset(remove),
        add_tags=norm_add,
        remove_tags=norm_remove,
        typed_tags=_typed_tags(desc) if isinstance(desc, str) else (),
        description=desc if isinstance(desc, str) else None,
    )
    spans = _line_spans(view, i, edit) if edit.changed else []
    inserted: list[Inserted] = []
    if context is not None:
        line = _context_line(view, i, tl.line, context)
        end = continuation_block(view.contents(), i)
        spans.append(_insertion(view, end - 1, [line], view.term_for(end - 1)))
        inserted.append(Inserted("context", line, None, ("old", target.line)))
    changed_names = tuple(sorted({*(n for n, v in (("due", args.due), ("scheduled", args.scheduled),
                                                  ("start", args.start), ("priority", args.priority),
                                                  ("recurrence", args.recurrence)) if v is not None),
                                  *clear,
                                  *(("status",) if args.status is not None else ()),
                                  *(("text",) if args.text is not None else ()),
                                  *(("add_tags",) if args.add_tags else ()),
                                  *(("remove_tags",) if args.remove_tags else ()),
                                  *(("append_context",) if context is not None else ())}))
    if not spans:
        return Plan(
            spans=(), new_text=note_text, new_lines=(), new_ids={"target": _render(target)},
            changed=False, target_line=target.line, intent=intent, old_line=tl.line,
            replaced_status=tl.status, fields_changed=(),
        )
    return _finish(
        note_text, spans,
        target_line=target.line, intent=intent, inserted=tuple(inserted),
        old_line=tl.line, replaced_status=tl.status, fields_changed=changed_names,
    )


def _value_text_of(family: str, value: object) -> str:
    if isinstance(value, datetime.date):
        return value.isoformat()
    return str(value).strip() if family == RECURRENCE else str(value)


# ── add_task (D8, D9) ────────────────────────────────────────────────────────

_MARKER_RE = re.compile(r"((?:[ \t]*+>)*+[ \t]*+)(?:([-*+])|([0-9]{1,9})([.)]))([ \t]++)")


def _new_marker(anchor_prefix: str) -> str | None:
    m = _MARKER_RE.fullmatch(anchor_prefix)
    if m is None:
        return None
    if m.group(2) is not None:
        return m.group(1) + m.group(2) + m.group(5)
    return m.group(1) + str(int(m.group(3)) + 1) + m.group(4) + m.group(5)


def plan_add(note_text: str, args: AddArgs, settings, today: datetime.date) -> Plan | TaskWriteRefusal:
    """D8/D9: insert one canonical task line (and an optional context line)
    next to the region's task list. ``today`` dates ``➕`` when
    ``settings.set_created_date``. Refuses an exact open duplicate among the
    note's current bytes (``duplicate_task``) unless ``allow_duplicate``; the
    index-side duplicate and similar scans are `duplicate_scan`, fed with the
    returned plan's ``norm`` and ``terms``."""
    if len(args.tags) > tl_mod.MAX_NEW_TASK_TAGS:
        return _refuse("invalid_argument", f"at most {tl_mod.MAX_NEW_TASK_TAGS} tags")
    for tag in args.tags:
        t = tag[1:] if isinstance(tag, str) and tag.startswith("#") else tag
        if not tl_mod.valid_tag(t):
            return _refuse("invalid_argument", f"invalid tag {tag!r}")
    if args.priority is not None and args.priority not in tl_mod.PRIORITY_SYMBOLS:
        return _refuse("invalid_argument", "priority must be highest, high, medium, low or lowest")
    desc = tl_mod.validate_description(args.text)
    if isinstance(desc, str):
        bad = _structure_refusal(desc, context=False, what="text")
        if bad is not None:
            return bad
    context = None
    if args.context is not None:
        context = _validate_context(args.context)
        if isinstance(context, TaskWriteRefusal):
            return context

    view = _view(note_text)
    if isinstance(view, TaskWriteRefusal):
        return view
    bad = _frontmatter_refusal(note_text)
    if bad is not None:
        return bad
    got = _extract(note_text)
    if isinstance(got, TaskWriteRefusal):
        return got
    tasks, truncated = got
    if truncated:
        return _refuse("size_limit", "the note already holds the per-note task cap")

    # Region R.
    region = _region(view, args.section)
    if isinstance(region, TaskWriteRefusal):
        return region
    region_lines, heading_line = region
    contents = view.contents()
    task_idx = {t.line - 1 for t in tasks}
    anchor = None
    for idx in reversed(region_lines):
        if idx in task_idx and _shape(contents[idx])[1] == 0:
            anchor = idx
            break
    owners = _owners(contents, task_idx)
    marker = "- "
    owner = None
    if anchor is not None:
        parsed = view.parse(anchor)
        if parsed is None:
            return _refuse("task_malformed", "the anchor task does not re-parse")
        marker = _new_marker(parsed.prefix)
        if marker is None:
            return _refuse("task_malformed", "the anchor task's list marker cannot be continued")
        after = continuation_block(contents, anchor) - 1
        owner = None if owners[anchor] is None else ("old", owners[anchor] + 1)
    else:
        nonblank = [idx for idx in region_lines if contents[idx].strip(" \t") != ""]
        if nonblank:
            after = nonblank[-1]
        else:
            after = heading_line  # None: the body start

    created = today if settings.set_created_date else None
    line = tl_mod.serialize_new_task(
        args.text, marker=marker, tags=tuple(args.tags), priority=args.priority,
        recurrence=args.recurrence, created=created, start=args.start,
        scheduled=args.scheduled, due=args.due,
    )
    if isinstance(line, TaskLineRefusal):
        return _from_line_refusal(line)
    new_tl = tl_mod.parse(line)
    plain_tl = new_tl  # the caller's line, before any global filter is added
    filter_added = None
    if getattr(settings, "task_rule", "all") == task_rule.RULE_FILTER and settings.global_filter:
        # #434 D7: under a `filter` rule a line whose body lacks the filter
        # text gets it prepended, as the plugin's task modal does
        # (`GlobalFilter.prependTo`), and the line is serialised again.
        f_text = settings.global_filter
        if f_text not in new_tl.full_text:
            got = _prepend_filter(f_text, desc if isinstance(desc, str) else args.text)
            if isinstance(got, TaskWriteRefusal):
                return got
            line = tl_mod.serialize_new_task(
                got, marker=marker, tags=tuple(args.tags), priority=args.priority,
                recurrence=args.recurrence, created=created, start=args.start,
                scheduled=args.scheduled, due=args.due,
            )
            if isinstance(line, TaskLineRefusal):
                return _from_line_refusal(line)
            new_tl = tl_mod.parse(line)
            filter_added = f_text
    norm = tl_mod.normalise_description(new_tl.full_text)

    # #434 D7: under a `filter` rule the same task may already be written with
    # the filter text in front or as its trailing token; both are candidate
    # keys everywhere (`index_norms`, also handed to `duplicate_scan`), and the
    # target note's own bytes (rule-independent) also match the caller's bare
    # description. Under any other rule: `norm` alone.
    index_norms, note_norms = _duplicate_candidates(norm, plain_tl, settings)
    dup_norms = note_norms
    if not args.allow_duplicate:
        dups = []
        for t in tasks:
            if t.state != STATE_OPEN:
                continue
            p = view.parse(t.line - 1)
            if p is not None and tl_mod.normalise_description(p.full_text) in dup_norms:
                dups.append(TaskRef(line=t.line, id=_render(t), text=t.text))
        if dups:
            return _refuse(
                "duplicate_task",
                "an open task with the same description is already in this note; "
                "pass allow_duplicate=true to add it anyway",
                matches=tuple(dups[:_DUPLICATE_LIST_MAX]),
            )

    new_lines = [line]
    inserted = [Inserted("task", line, Expect(" ", _expect_new(args, created, line), frozenset(t.lower() for t in new_tl.tags)), owner)]
    if context is not None:
        cl = _context_line(view, None, line, context)
        new_lines.append(cl)
        inserted.append(Inserted("context", cl, None, ("new", 0)))
    term = view.term_for(after) if after is not None else view.term_for(view.body_first - 1 if view.body_first > 0 else None)
    span = _insertion(view, after, new_lines, term)
    return _finish(
        note_text, [span],
        target_line=None, intent=None, inserted=tuple(inserted),
        norm=norm, terms=_similarity_terms_for(norm, settings), filter_added=filter_added,
        dup_norms=index_norms if _filter_text(settings) else (),
    )


def _prepend_filter(filter_text: str, desc: str) -> str | TaskWriteRefusal:
    """#434 D7: `filter_text + " " + desc`, checked as the final description
    is: the 1,000-character bound (naming the global filter), then the one
    structure check every entry point uses, run again on the combined text —
    so a filter text that reached the settings row some other way still
    cannot open a comment or a fence in the note — then the description
    rules. Before any dry-run answer and before anything is published."""
    # Layer (b) does not trust the settings: the filter text it is about to
    # prepend must pass the one validator the settings use (every rule), or
    # nothing is planned.
    checked, error = task_rule.normalise_filter(filter_text)
    if error is not None or checked != filter_text:
        why = error.rstrip(".") if error else "it has surrounding whitespace"
        return _refuse("invalid_argument",
                       f"the global filter text cannot be added ({why}); nothing was written")
    combined = f"{filter_text} {desc}"
    if len(combined) > tl_mod.DESCRIPTION_MAX_CHARS:
        return _refuse(
            "invalid_argument",
            f"text plus the global filter text ({len(combined)} characters) exceeds "
            f"{tl_mod.DESCRIPTION_MAX_CHARS:,} characters",
        )
    bad = _structure_refusal(combined, context=False, what="text plus the global filter text")
    if bad is not None:
        return bad
    got = tl_mod.validate_description(combined)
    if isinstance(got, TaskLineRefusal):
        return _refuse("invalid_argument", "text plus the global filter text: " + got.reason)
    return got


def _filter_text(settings) -> str | None:
    if getattr(settings, "task_rule", "all") != task_rule.RULE_FILTER:
        return None
    return getattr(settings, "global_filter", None) or None


def _duplicate_candidates(norm: str, plain_tl, settings) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """#434 D7: `(index candidates, target-note candidates)`.

    Under a `filter` rule an existing open line is an exact duplicate when its
    normalised description equals (1) the final line's (`norm`, the filter
    prepended) or (2) the caller's description with the filter as its
    trailing token instead of in front. Each is computed with
    `normalise_description` over a constructed text, as the extractor
    computes a stored line's — never by editing a key. The target note's own
    bytes (a rule-independent check) also match (3) the caller's bare
    description. Under any other rule both are `(norm,)`. A filter in the
    middle of an existing description is not matched (L13)."""
    f_text = _filter_text(settings)
    if f_text is None:
        return (norm,), (norm,)
    desc = tl_mod.plugin_description(plain_tl.full_text)
    if desc.startswith(f_text + " "):
        desc = desc[len(f_text) + 1:]
    trailing = tl_mod.normalise_description(f"{desc} {f_text}")
    bare = tl_mod.normalise_description(desc)
    index = tuple(dict.fromkeys((norm, trailing)))
    return index, tuple(dict.fromkeys((norm, trailing, bare)))


def _filter_terms(settings) -> frozenset[str]:
    f_text = _filter_text(settings)
    if f_text is None:
        return frozenset()
    return term_set(" ".join(unicodedata.normalize("NFC", f_text).casefold().split()))


def _similar_term_set(norm: str, settings) -> frozenset[str]:
    """The similarity terms of `norm`; under a `filter` rule the filter text
    is not a term (on either side of the comparison, #434 D7)."""
    return term_set(norm) - _filter_terms(settings)


def _similarity_terms_for(norm: str, settings) -> tuple[str, ...]:
    drop = _filter_terms(settings)
    return tuple(t for t in similarity_terms(norm) if t not in drop)


def extracted_task_at(text: str, line: int):
    """The `ExtractedTask` at 1-based `line` of `text` (a planned new text),
    or None — from the same memoised extraction the planner and `verify`
    used, so the write tools' rule statement (#434 D7) judges exactly the
    bytes the plan writes."""
    got = _extract(text)
    if isinstance(got, TaskWriteRefusal):
        return None
    for t in got[0]:
        if t.line == line:
            return t
    return None


def _expect_new(args: AddArgs, created, line: str) -> dict[str, str | None]:
    out: dict[str, str | None] = {f: None for f in _CHECKED_FAMILIES}
    out[DUE] = args.due.isoformat() if args.due else None
    out[SCHEDULED] = args.scheduled.isoformat() if args.scheduled else None
    out[START] = args.start.isoformat() if args.start else None
    out[CREATED] = created.isoformat() if created else None
    out[PRIORITY] = args.priority
    out[RECURRENCE] = args.recurrence.strip() if args.recurrence else None
    return out


def _region(view: _NoteView, section: str | None):
    """``(region line indices, heading line index or None)`` (D8)."""
    if section is None:
        return list(range(view.body_first, len(view.lines))), None
    _fm, stripped, diagnosis = parse_frontmatter_diagnose(view.text)
    if diagnosis.defect is not None:
        return _refuse("content_unsafe", "the note has a malformed frontmatter block",
                       detail=("frontmatter_defect", diagnosis))
    scan_text = stripped if diagnosis.valid else view.text
    if unmatched_indented_openers(scan_text, context=BODY):
        return _refuse("content_unsafe", "the note contains an indented fence opener that nothing below it closes",
                       detail=("unmatched_fence", scan_text, diagnosis))
    base = len(view.text) - len(scan_text)
    if base != view.body_offset:
        return _refuse("task_malformed", "the section scan and the task extraction disagree on the body")
    headings = _scan_headings(scan_text)
    idx, err = _resolve_section_index(headings, section)
    if err is not None:
        return _refuse("selector_unresolved", err)
    body_start, body_end = _section_body_span(scan_text, headings, idx)
    h_pos = base + headings[idx]["line_start"]
    heading_line = next(k for k, ln in enumerate(view.lines) if ln.start <= h_pos <= ln.end)
    lo, hi = base + body_start, base + body_end
    lines = [k for k in range(heading_line + 1, len(view.lines))
             if lo <= view.lines[k].start < hi]
    return lines, heading_line


# ── D9 terms ─────────────────────────────────────────────────────────────────


def _terms(norm: str) -> list[str]:
    out: list[str] = []
    cur: list[str] = []
    for ch in norm:
        if ch.isspace() or unicodedata.category(ch).startswith("P"):
            if cur:
                out.append("".join(cur))
                cur = []
        else:
            cur.append(ch)
    if cur:
        out.append("".join(cur))
    return [t for t in out if len(t) >= 2 and not t.isdigit()]


def term_set(norm: str) -> frozenset[str]:
    """D9: every term of a normalised description (split on whitespace and
    Unicode punctuation; at least 2 code points; not all digits)."""
    return frozenset(_terms(norm))


def similarity_terms(norm: str) -> tuple[str, ...]:
    """D9: the up to 8 longest distinct terms (ties in first-seen order) —
    the candidate filter of the similar-task scan."""
    seen: dict[str, int] = {}
    for k, t in enumerate(_terms(norm)):
        seen.setdefault(t, k)
    ordered = sorted(seen, key=lambda t: (-len(t), seen[t]))
    return tuple(ordered[:SIMILAR_TERMS_MAX])


def contains(a: frozenset[str], b: frozenset[str]) -> bool:
    """D9 containment: one term set holds the other, and the smaller set has
    at least 2 terms (a one-term description never matches this way)."""
    small, big = (a, b) if len(a) <= len(b) else (b, a)
    return len(small) >= SIMILAR_CONTAINMENT_MIN_TERMS and small <= big


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    union = a | b
    return len(a & b) / len(union) if union else 0.0


# ── verify (D5) ──────────────────────────────────────────────────────────────


def _fail(reason: str) -> TaskWriteRefusal:
    return _refuse("task_malformed", "verification failed: " + reason + "; nothing was written")


def _line_map(old: str, plan: Plan):
    """``(old index → new index, [new indices of inserted lines])`` derived
    from the old text's line structure and the plan's spans, or a refusal
    when a span is not a line-confined edit of the target or a whole-line
    insertion at a line boundary."""
    v = _NoteView(old) if "\x00" not in old else None
    if v is None:
        return _fail("the old text contains NUL")
    starts = {ln.start: k for k, ln in enumerate(v.lines)}
    target = None if plan.target_line is None else plan.target_line - 1
    if target is not None and not 0 <= target < len(v.lines):
        return _fail("the declared target line is outside the note")
    shift_before: list[tuple[int, int]] = []  # (old line index the lines precede, count)
    eof_append = 0
    prev_end = -1
    for s in plan.spans:
        if s.start < prev_end or s.end < s.start:
            return _fail("the spans overlap")
        prev_end = s.end
        if not _LINE_BREAK_RE.search(s.text) and not (old == "" and s.start == s.end == 0 and s.text):
            # A line-confined edit: only of the declared target's content.
            if target is None:
                return _fail("a span edits a line although no target is declared")
            tln = v.lines[target]
            if not (tln.start <= s.start and s.end <= tln.end):
                return _fail("a span edits text outside the target line")
            continue
        if s.start != s.end:
            return _fail("a span replaces text outside the target line")
        pieces = _LINE_BREAK_RE.split(s.text)
        terms = _LINE_BREAK_RE.findall(s.text)
        if old == "":
            count = len(pieces)
            if s.start != 0:
                return _fail("an insertion into an empty note is not at its start")
            eof_append += count
            continue
        if s.start == len(old) and not old.endswith(("\n", "\r")):
            if not s.text or pieces[0] != "" or not terms:
                return _fail("an end-of-file insertion does not start with a terminator")
            eof_append += len(terms)
            continue
        if s.start not in starts:
            return _fail("an insertion is not at a line boundary")
        if not s.text or pieces[-1] != "":
            return _fail("an inserted line has no terminator")
        if any(p == "" for p in pieces[:-1]):
            return _fail("an inserted line is empty")
        shift_before.append((starts[s.start], len(terms)))
    # Map old lines.
    n_old = 0 if old == "" else len(v.lines)
    old_to_new: list[int] = []
    added = 0
    ins_new: list[int] = []
    pending = sorted(shift_before)
    p = 0
    for k in range(n_old):
        while p < len(pending) and pending[p][0] == k:
            for c in range(pending[p][1]):
                ins_new.append(k + added + c)
            added += pending[p][1]
            p += 1
        old_to_new.append(k + added)
    last_new = (old_to_new[-1] + 1) if old_to_new else 0
    for c in range(eof_append):
        ins_new.append(last_new + c)
    return old_to_new, ins_new


@dataclass(frozen=True)
class _LineSpec:
    """What a derived line must be relative to its ORIGINAL line: the status,
    per family ``("same",)`` / ``("set", value_text)`` / ``("absent",)``,
    whether the block link goes, the tags whose tokens may differ, the
    expected tag set, and — for a replaced description — the exact composed
    description the new text must start with."""

    status: str
    families: Mapping[str, tuple]
    remove_block: bool
    touched_tags: frozenset[str]
    tags: frozenset[str]
    description: str | None


def _tag_spans(text: str, masked: str, lo: int, hi: int, tags: set[str]) -> list[tuple[int, int]]:
    return [(m.start, m.end) for m in tl_mod._tag_matches(text, masked, lo, hi) if m.tag.lower() in tags]


def _drop(text: str, spans: list[tuple[int, int]], lo: int, hi: int,
          marks: Sequence[tuple[int, int]] = ()) -> str:
    """``text[lo:hi]`` with each span removed together with ONE adjacent
    separating space or tab — the preceding one, else the following one,
    never one another removal took (the D4 removal rule) — and each ``marks``
    span (a value changed in place) replaced by one marker character, so its
    position stays pinned."""
    claimed: set[int] = set()
    cuts = []
    for start, end in sorted(set(spans)):
        if start - 1 >= lo and text[start - 1] in " \t" and (start - 1) not in claimed:
            start -= 1
        elif end < hi and text[end] in " \t" and end not in claimed:
            end += 1
        claimed.update(range(start, end))
        cuts.append((start, end, ""))
    cuts += [(a, b, "\x00") for a, b in marks]
    out, pos = [], lo
    for start, end, repl in sorted(cuts):
        out.append(text[pos:max(pos, start)])
        out.append(repl)
        pos = max(pos, end)
    out.append(text[pos:hi])
    return "".join(out)


def _skeleton(tl, lo: int, spec: _LineSpec) -> str:
    """The line's text from ``lo`` with every token the spec may change
    removed (touched families, touched tags, a dropped block link). Two lines
    whose skeletons are equal differ only in those tokens."""
    in_place = {f for f, w in spec.families.items() if w[0] == "set" and len(w) > 2 and w[2]}
    touched = {f for f, w in spec.families.items() if w[0] != "same"} - in_place
    spans = [(t.start, t.end) for t in tl.tokens if t.kind == "field" and t.family in touched and t.start >= lo]
    marks = [(t.value_start, t.value_end) for t in tl.tokens
             if t.kind == "field" and t.family in in_place and t.start >= lo]
    if spec.remove_block and tl.block is not None:
        spans.append((tl.block.start, tl.block.end))
    spans += _tag_spans(tl.line, tl.masked, lo, tl.text_end, set(spec.touched_tags))
    return _drop(tl.line, spans, lo, tl.text_end, marks)


def _target_spec(old_tl, intent: Intent) -> _LineSpec:
    status = intent.status if intent.status is not None else old_tl.status
    fams: dict[str, tuple] = {}
    for fam in tl_mod.CANONICAL_ORDER:
        if fam in intent.set_fields:
            # A value changed in place keeps its token's position.
            fams[fam] = ("set", intent.set_fields[fam], bool(old_tl.run_fields(fam)))
        elif fam in intent.remove_fields:
            fams[fam] = ("absent",)
        else:
            fams[fam] = ("same",)
    add = {t.lower() for t in intent.add_tags}
    remove = {t.lower() for t in intent.remove_tags}
    tags = {t.lower() for t in old_tl.tags} | {t.lower() for t in intent.typed_tags}
    tags = (tags - remove) | add
    comp = None
    if intent.description is not None:
        # The composition, derived here independently of the planner: the
        # caller's text, then every tag of the old description region the
        # text lacks (original order and spelling), then `remove_tags`, then
        # the `add_tags` present nowhere else on the line.
        comp = intent.description
        present = {t.lower() for t in _typed_tags(comp)}
        for tag in _typed_tags(old_tl.line[old_tl.text_start:old_tl.description_end]):
            if tag.lower() not in present:
                present.add(tag.lower())
                comp += " #" + tag
        if remove:
            cm = mask_code(comp, context=BODY)
            comp = _drop(comp, _tag_spans(comp, cm, 0, len(comp), remove), 0, len(comp))
        rest = {m.tag.lower() for m in tl_mod._tag_matches(
            old_tl.line, old_tl.masked, old_tl.description_end, old_tl.text_end)} - remove
        have = {t.lower() for t in _typed_tags(comp)} | rest
        for t in intent.add_tags:
            if t.lower() not in have:
                have.add(t.lower())
                comp += " #" + t
    return _LineSpec(status, fams, False, frozenset(add | remove), frozenset(tags), comp)


def _next_spec(old_tl, expect: Expect) -> _LineSpec:
    """The next occurrence against the ORIGINAL line (D7): only the status,
    the shifted dates, ``➕`` and the dropped families may differ."""
    fams: dict[str, tuple] = {f: ("same",) for f in tl_mod.CANONICAL_ORDER}
    for fam in (DUE, SCHEDULED, START, CREATED):
        v = expect.fields.get(fam)
        fams[fam] = ("set", v, bool(old_tl.run_fields(fam))) if v is not None else ("absent",)
    for fam in (DONE, CANCELLED, ID, DEPENDS_ON):
        fams[fam] = ("absent",)
    return _LineSpec(" ", fams, True, frozenset(), frozenset(t.lower() for t in old_tl.tags), None)


_EXTRACTED_ATTR = {DUE: "due", SCHEDULED: "scheduled", START: "start", DONE: "done_on",
                   CREATED: "created", CANCELLED: "cancelled", PRIORITY: "priority", RECURRENCE: "recurrence"}


def _reading(task, family: str):
    v = getattr(task, _EXTRACTED_ATTR[family], None)
    if isinstance(v, datetime.date):
        return v.isoformat()
    return v.strip() if isinstance(v, str) and family == RECURRENCE else v


def _check_derived_line(old_tl, old_task, new_tl, new_task, spec: _LineSpec, what: str) -> str | None:
    """A target or next-occurrence line against its ORIGINAL line: prefix,
    gap and trailing whitespace byte-identical; the description identical,
    or exactly the expected composition with nothing after it; every token
    the spec does not touch byte-identical and in order (equal skeletons);
    touched families exactly as intended; untouched families read by the
    extractor exactly as they read before; the tag set as expected."""
    if new_tl is None or new_task is None:
        return f"the {what} line would not be a task"
    if new_tl.status != spec.status or new_task.status_char != spec.status:
        return f"the {what} line would not have the intended status"
    if new_tl.prefix != old_tl.prefix or new_tl.gap != old_tl.gap or new_tl.tail_ws != old_tl.tail_ws:
        return f"the {what} line's prefix, gap or trailing whitespace would change"
    if spec.description is not None:
        start = new_tl.text_start
        end = start + len(spec.description)
        if new_tl.line[start:end] != spec.description or (end < new_tl.text_end and new_tl.line[end] not in " \t"):
            return f"the {what} line's description would not be exactly the replacement text"
        old_lo, new_lo = old_tl.description_end, end
    else:
        old_lo, new_lo = old_tl.text_start, new_tl.text_start
    if _skeleton(old_tl, old_lo, spec) != _skeleton(new_tl, new_lo, spec):
        return f"the {what} line would change outside the fields the call names"
    for fam, want in spec.families.items():
        found = new_tl.run_fields(fam)
        if want[0] == "same":
            if new_tl.family_count(fam) != old_tl.family_count(fam):
                return f"the {what} line's untouched {fam} field would change"
        elif want[0] == "absent":
            if found or new_tl.family_count(fam):
                return f"the {what} line would carry an unintended {fam} field"
        else:
            want_v = want[1].strip() if fam == RECURRENCE else want[1]
            if len(found) != 1 or new_tl.family_count(fam) != 1 or _value_text(new_tl, fam) != want_v:
                return f"the {what} line's {fam} field would not re-extract as intended"
    for fam in _EXTRACTED_ATTR:
        want = spec.families[fam]
        got = _reading(new_task, fam)
        if want[0] == "same":
            ok = got == _reading(old_task, fam)
        elif want[0] == "absent":
            ok = got is None
        else:
            ok = got == (want[1].strip() if fam == RECURRENCE else want[1])
        if not ok:
            return f"the {what} line's {fam} would not be extracted as intended"
    if {t.lower() for t in new_tl.tags} != set(spec.tags):
        return f"the {what} line's tags would not be conserved"
    if not new_task.tags_cut and {t.lower() for t in (new_task.tags or [])} != set(spec.tags):
        return f"the {what} line's extracted tags would not be conserved"
    block_old = old_tl.block.value if old_tl.block else None
    block_new = new_tl.block.value if new_tl.block else None
    if block_new != (None if spec.remove_block else block_old):
        return f"the {what} line's block link would change"
    return None


def _extracted_value(task, family: str):
    v = {DUE: task.due, SCHEDULED: task.scheduled, START: task.start, DONE: task.done_on}.get(family)
    return v.isoformat() if v is not None else None


def _check_new_task(new_tl, new_task, expect: Expect, what: str) -> str | None:
    if new_tl is None or new_task is None:
        return f"the {what} line would not be a task"
    if new_tl.status != expect.status or new_task.status_char != expect.status:
        return f"the {what} line would not have the intended status"
    for fam in _CHECKED_FAMILIES:
        want = expect.fields.get(fam)
        found = new_tl.run_fields(fam)
        if want is None:
            if found or new_tl.family_count(fam):
                return f"the {what} line would carry an unintended {fam} field"
            continue
        got = _value_text(new_tl, fam)
        if len(found) != 1 or new_tl.family_count(fam) != 1 or got != want:
            return f"the {what} line's {fam} field would not re-extract as intended"
    for fam in (ID, DEPENDS_ON):
        if new_tl.family_count(fam):
            return f"the {what} line would carry a {fam} field"
    for fam in (DUE, SCHEDULED, START, DONE):
        if _extracted_value(new_task, fam) != expect.fields.get(fam):
            return f"the {what} line's {fam} date would not be extracted as intended"
    if new_task.priority != expect.fields.get(PRIORITY):
        return f"the {what} line's priority would not be extracted as intended"
    if {t.lower() for t in new_tl.tags} != set(expect.tags):
        return f"the {what} line's tags would not re-extract as intended"
    if not new_task.tags_cut and {t.lower() for t in (new_task.tags or [])} != set(expect.tags):
        return f"the {what} line's tags would not be extracted as intended"
    return None


_COMPARED = ("status_char", "state", "text", "text_cut", "due", "scheduled", "start",
             "done_on", "priority", "tags", "tags_cut", "heading_path", "task_key")


def verify(old: str, new: str, plan: Plan) -> TaskWriteRefusal | None:
    """D5: every check, re-derived from the two texts. None when the plan is
    safe to publish; otherwise ``task_malformed`` (``size_limit`` when the
    result would exceed the per-note task cap), nothing written.

    1. ``new`` is ``old`` with exactly the declared spans applied, and every
       span is either a line-confined edit of the declared target line or a
       whole-line insertion at a line boundary; every other line keeps its
       content and terminator.
    2. The target and every declared inserted task re-extract (extractor and
       `task_line.parse`) with exactly the intended status and fields.
    3. Every other task extracts identically, shifted by the inserted count.
    4. No undeclared task appears (and none disappears).
    5. No pre-existing line changes its owning task; declared lines are owned
       as planned.
    6. The target's tags are the old tags plus ``add_tags`` and the tags
       typed in a replacement ``text``, minus ``remove_tags``.

    Plus: the frontmatter block, every heading and every pre-existing line's
    code mask are unchanged, and no inserted line is inside code.
    """
    if "\x00" in new:
        return _fail("the new text contains NUL")
    try:
        if _compose(old, plan.spans) != new:
            return _fail("the new text is not the old text with the declared spans")
    except ValueError:
        return _fail("the spans overlap")
    mapping = _line_map(old, plan)
    if isinstance(mapping, TaskWriteRefusal):
        return mapping
    old_to_new, ins_new = mapping
    if len(ins_new) != len(plan.inserted):
        return _fail("the inserted lines differ from the declared ones")
    try:
        ov = _NoteView(old)
        nv = _NoteView(new)
    except ValueError as e:
        return _fail(str(e))
    n_old = len(old_to_new)
    if len(nv.lines) != n_old + len(ins_new):
        return _fail("the new text has an unexpected number of lines")
    target = None if plan.target_line is None else plan.target_line - 1
    eof_extended = (
        old != "" and not old.endswith(("\n", "\r"))
        and any(s.start == s.end == len(old) and _LINE_BREAK_RE.search(s.text) for s in plan.spans)
    )
    for k in range(n_old):
        a, b = ov.lines[k], nv.lines[old_to_new[k]]
        if k != target and ov.content(k) != nv.content(old_to_new[k]):
            return _fail("a line other than the target would change")
        if a.term != b.term and not (eof_extended and k == n_old - 1 and a.term == ""):
            return _fail("a line terminator would change")
    for idx, decl in zip(ins_new, plan.inserted):
        if nv.content(idx) != decl.text:
            return _fail("an inserted line differs from its declaration")
    if nv.body_first != ov.body_first or new[: ov.body_offset] != old[: ov.body_offset]:
        return _fail("the frontmatter block would change")

    got_old = _extract(old)
    got_new = _extract(new)
    if isinstance(got_old, TaskWriteRefusal):
        return got_old
    if isinstance(got_new, TaskWriteRefusal):
        return got_new
    old_tasks, old_trunc = got_old
    new_tasks, new_trunc = got_new
    if new_trunc:
        return _refuse("size_limit", "the result would hold more tasks than the per-note task cap; nothing was written")
    if old_trunc:
        return _fail("the old note exceeds the per-note task cap")
    old_by = {t.line - 1: t for t in old_tasks}
    new_by = {t.line - 1: t for t in new_tasks}
    declared_tasks = {idx for idx, d in zip(ins_new, plan.inserted) if d.kind in ("next", "task")}
    expected_lines = {old_to_new[k] for k in old_by} | declared_tasks
    if set(new_by) != expected_lines:
        return _fail("the set of task lines would change beyond the declared ones")

    # 3. untouched tasks.
    for k, t in old_by.items():
        if k == target:
            continue
        u = new_by[old_to_new[k]]
        for name in _COMPARED:
            if getattr(t, name) != getattr(u, name):
                return _fail(f"another task's {name} would change")

    # 2, 6 and the byte-level pin. The target, against its old line.
    if target is not None:
        if target not in old_by:
            return _fail("the declared target is not a task")
        old_tl = ov.parse(target)
        if old_tl is None:
            return _fail("the declared target is not a task")
        spec = _target_spec(old_tl, plan.intent or Intent())
        problem = _check_derived_line(
            old_tl, old_by[target], nv.parse(old_to_new[target]), new_by.get(old_to_new[target]), spec, "target")
        if problem is not None:
            return _fail(problem)

    # 2. declared inserted tasks; context lines.
    for idx, decl in zip(ins_new, plan.inserted):
        if decl.kind == "next":
            if target is None or decl.expect is None:
                return _fail("a next occurrence needs its completed task")
            old_tl = ov.parse(target)
            problem = _check_derived_line(
                old_tl, old_by[target], nv.parse(idx), new_by.get(idx), _next_spec(old_tl, decl.expect), "next")
            if problem is not None:
                return _fail(problem)
        elif decl.kind == "task":
            if decl.expect is None:
                return _fail("an added task has no expectation")
            problem = _check_new_task(nv.parse(idx), new_by.get(idx), decl.expect, decl.kind)
            if problem is not None:
                return _fail(problem)
        elif decl.kind == "context":
            if idx in new_by:
                return _fail("a context line would be a task")
        else:
            return _fail(f"unknown inserted kind {decl.kind!r}")
        if idx >= nv.body_first and nv.masked_line(idx) != mask_code(nv.content(idx), context=BODY):
            return _fail("an inserted line would be inside code")

    # Code masks and headings.
    for k in range(n_old):
        if k == target or k < ov.body_first:
            continue
        if ov.masked_line(k) != nv.masked_line(old_to_new[k]):
            return _fail("a line's code status would change")
    oh = _heading_lines(ov)
    nh = _heading_lines(nv)
    if [(old_to_new[k], d, t) for k, d, t in oh] != nh:
        return _fail("the note's headings would change")

    # 5. owners.
    old_own = _owners(ov.contents()[:n_old], set(old_by))
    new_own = _owners(nv.contents(), set(new_by))
    for k in range(n_old):
        want = None if old_own[k] is None else old_to_new[old_own[k]]
        if new_own[old_to_new[k]] != want:
            return _fail("a pre-existing line would change its owning task")
    for idx, decl in zip(ins_new, plan.inserted):
        if decl.owner is None:
            want = None
        elif decl.owner[0] == "old":
            want = old_to_new[decl.owner[1] - 1]
        else:
            want = ins_new[decl.owner[1]]
        if new_own[idx] != want:
            return _fail("an inserted line would not be owned as planned")
    return None


def _heading_lines(v: _NoteView) -> list[tuple[int, int, str]]:
    out = []
    starts = [c - v._canon_body_offset for c in v._canon_starts]
    for m in _ATX_HEADING_RE.finditer(v.masked):
        k = bisect.bisect_right(starts, m.start()) - 1
        out.append((k, len(m.group(1)), m.group(2).strip()))
    return out


# ── duplicate scan (D9) ──────────────────────────────────────────────────────


async def duplicate_scan(session, uid, settings, norm: str, terms: Sequence[str], *,
                         exclude_path: str | None = None, extra_norms: Sequence[str] = ()):
    """D9: ``(exact, similar, stopped)`` among the caller's OPEN tasks in the
    default scope, owner-scoped (the total `NoteMetadata` owner mapping).

    * ``exact`` — up to 5 `TaskRef`s whose ``description_key`` equals
      ``description_key(norm)``, ordered by ``(file_path, line)``. Rows of
      ``exclude_path`` (the target note, whose current bytes `plan_add`
      already checked) are left out.
    * ``similar`` — up to 5 non-exact candidates whose term set contains
      or is contained in ``norm``'s (the smaller set holding at least 2
      terms), or whose Jaccard score is at least 0.5; containment matches
      first, then score, then ``(file_path, line)``. Candidates are the
      rows whose ``text_fold`` contains any of ``terms``, ordered by
      ``(file_path, line)``, at most ``TASK_SIMILAR_SCAN_MAX``.
    * ``stopped`` — True when the candidate scan hit that bound.

    Never refuses; the caller decides (``duplicate_task`` unless
    ``allow_duplicate``)."""
    from sqlalchemy import and_, func, or_, select, true

    from src.models.db import NoteMetadata, NoteTask

    key = tl_mod.description_key(norm)
    # #434 D7: under a `filter` rule `plan_add` hands every candidate form an
    # exact duplicate may take (`Plan.dup_norms`); otherwise `norm` alone.
    keys = list(dict.fromkeys(
        [key] + [tl_mod.description_key(n) for n in extra_norms]))
    # The one owner mapping (`tools._note_owner_predicate`, the predicate
    # `list_tasks` joins under), imported at call time like `task_query` does.
    from src.mcp_server import tools as _tools

    owner = _tools._note_owner_predicate(uid)
    from src.services.task_query import scope_clause

    # The listing's own default-scope predicate (D12/D14): a note `list_tasks`
    # excludes under the caller's settings is excluded here by construction.
    scope = scope_clause(settings)
    if scope is None:
        scope = true()
    join_on = and_(NoteMetadata.id == NoteTask.note_id, owner)
    cols = (NoteMetadata.file_path, NoteTask.line, NoteTask.task_key, NoteTask.key_ordinal,
            NoteTask.key_count, NoteTask.text, NoteTask.description_key)

    exact_stmt = (
        select(*cols).select_from(NoteTask).join(NoteMetadata, join_on)
        .where(NoteTask.state == STATE_OPEN,
               NoteTask.description_key == key if len(keys) == 1
               else NoteTask.description_key.in_(keys), scope)
    )
    if exclude_path is not None:
        exact_stmt = exact_stmt.where(NoteMetadata.file_path != exclude_path)
    exact_stmt = exact_stmt.order_by(NoteMetadata.file_path, NoteTask.line).limit(_DUPLICATE_LIST_MAX)
    exact = [_ref(r) for r in (await session.execute(exact_stmt)).all()]

    similar: list[TaskRef] = []
    stopped = False
    terms = [t for t in terms if t]
    if terms:
        mine = _similar_term_set(norm, settings)
        cand_stmt = (
            select(*cols).select_from(NoteTask).join(NoteMetadata, join_on)
            .where(NoteTask.state == STATE_OPEN, scope,
                   or_(*(func.strpos(NoteTask.text_fold, t) > 0 for t in terms)))
            .order_by(NoteMetadata.file_path, NoteTask.line)
            .limit(TASK_SIMILAR_SCAN_MAX + 1)
        )
        rows = (await session.execute(cand_stmt)).all()
        if len(rows) > TASK_SIMILAR_SCAN_MAX:
            stopped = True
            rows = rows[:TASK_SIMILAR_SCAN_MAX]
        scored = []
        for r in rows:
            if r.description_key in keys:
                continue
            theirs = _similar_term_set(tl_mod.normalise_description(r.text), settings)
            contained = contains(mine, theirs)
            score = jaccard(mine, theirs)
            if contained or score >= SIMILAR_MIN_SCORE:
                scored.append((0 if contained else 1, -score, r.file_path, r.line, r, score))
        scored.sort(key=lambda x: x[:4])
        similar = [_ref(r, score=s) for *_k, r, s in scored[:SIMILAR_LIST_MAX]]
    return exact, similar, stopped


def _ref(row, score: float | None = None) -> TaskRef:
    rid = None
    if row.task_key is not None and row.key_ordinal is not None and row.key_count is not None:
        rid = tl_mod.render_id(row.task_key, row.key_ordinal, row.key_count)
    return TaskRef(line=row.line, id=rid, text=row.text, path=row.file_path, score=score)


__all__ = [
    "AddArgs", "CompleteArgs", "Expect", "Inserted", "Intent", "NewLine", "Plan",
    "Span", "TaskRef", "TaskWriteRefusal", "UpdateArgs", "continuation_block",
    "contains", "duplicate_scan", "extracted_task_at", "jaccard", "plan_add", "plan_complete", "plan_update",
    "resolve", "similarity_terms", "term_set", "verify",
]
