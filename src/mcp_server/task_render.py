"""Rendering of `list_tasks` and `task_agenda` responses (#428, D12, D13).

Pure: no database, no clock. Takes the results of `src/services/task_query.py`
and a character budget (`MAX_READ_RESPONSE_CHARS`) and returns the response
text, or a typed `QueryRefusal` (`response_cap_too_small`) when not even one
abbreviated row (or, for the agenda, the section skeleton) fits.

**The #350 structure is reused, not copied.** The header's #350 tokens come
from `tools.render_task_header`, full rows from `tools.render_task_row`, the
abbreviated first row from `tools.render_abbreviated_task_row`, the closing
notice from `tools.render_incomplete_notice`, and every note-controlled field
goes through `tools._one_line` / `tools._escape_task_path`. The id token
(` — id <id>`, D12) is inserted after `path:line` by handing those renderers
the line slot as `"<line> — id <id>"` — the only place they interpolate
`row["line"]` — so every other byte of a #350 row is unchanged and `text:`
stays last.

`tools` is imported as a module and read at call time, so `tools.py` may
import this module (lazily or at top level) without an import cycle.

**Nothing is silent.** Line 1 always states the #350 counts plus
`task ids pending`; the query line states the timezone, today, every resolved
date, the scope, the number excluded by scope and the exclusion lists; cut
text, tags and context are labelled; an abbreviated row says what it omitted.
"""
from __future__ import annotations

import datetime
import json

from src.services import task_line, task_rule
from src.services.task_query import (
    AGENDA_SECTIONS,
    DATE_ARGS,
    AgendaPage,
    AgendaSection,
    QueryRefusal,
    RuleCounts,
    TaskPage,
    encode_cursor,
    pending_v5_filters,
)

# ── fixed bounds (each pinned by `tests/test_task_render.py`) ────────────────

#: D1: `<12 hex>.9999of9999`.
ID_MAX = 23
#: D12: a brief row, excluding its escaped path.
BRIEF_ROW_MAX = 260
BRIEF_TAGS_MAX = 48
BRIEF_DESC_MAX = 80
#: D12: the query line.
QUERY_LINE_MAX = 600
QUERY_ECHO_MAX = 32
#: When the worst case of every echo at 32 would pass `QUERY_LINE_MAX`, the
#: four free-text echoes (text, priority, exclude_tags, path) are cut here
#: instead, so the line bound holds without cutting a mandatory part.
QUERY_ECHO_TIGHT = 24
#: D12: line 1 (the #350 bound, kept with the new tokens).
HEADER_MAX = 480
#: The #350 footer bound, kept for the version-2 compact cursor.
FOOTER_MAX = 200
#: D12: an abbreviated row's fixed structure, plus the id token.
ABBREVIATED_STRUCTURE_MAX = 100
ABBREVIATED_ID_TOKEN_MAX = 3 + 3 + ID_MAX  # " — id " + id
#: The note an abbreviated row adds when `context=true` and it had context.
ABBREVIATED_CONTEXT_NOTE = len(" — context omitted")
#: D12 guarantees: at these caps one abbreviated row always renders (no
#: refusal) — without a query line at the 1,000 minimum, with one at 1,600.
LIST_MIN_BUDGET = 1000
LIST_WITH_QUERY_MIN_BUDGET = 1600
#: Measured by rendering the synthetic worst case (every count 10 digits, a
#: 10-digit line, the 23-character id, all four dates and a priority): the
#: caps from which that abbreviated row keeps 40 characters each of path and
#: text. The paper figures (985 / 1,600) did not include the id token and
#: the `task ids pending` count at their widest.
LIST_ABBREVIATED_FULL_BUDGET = 1031
LIST_WITH_QUERY_FULL_BUDGET = 1641
#: D13: below this the agenda is refused.
AGENDA_MIN_BUDGET = 2800
#: #434 D5: the rule line. Every part but the task-tag names is mandatory and
#: never cut; with ten-digit counts the measured worst cases are
#: `RULE_LINE_FIELDS_MANDATORY` (names fill the rest) and
#: `RULE_LINE_FILTER_MAX` (a 64-character filter of characters JSON escapes
#: to two), both pinned by `tests/test_task_render.py`.
RULE_LINE_MAX = 320
RULE_LINE_FIELDS_MANDATORY = 235
RULE_LINE_FILTER_MAX = 284
#: #434 D5 guarantees with the rule line (the #428 figures plus at most
#: `RULE_LINE_MAX` + 1): one row always renders, never refused, from
#: `LIST_WITH_RULE_MIN_BUDGET`; 40 + 40 characters of path and text, with the
#: incomplete notice, from `LIST_WITH_RULE_FULL_BUDGET`. Pinned by
#: `test_rule_line_min_budget` / `test_rule_line_full_budget`.
LIST_WITH_RULE_MIN_BUDGET = LIST_WITH_QUERY_MIN_BUDGET + RULE_LINE_MAX + 1
LIST_WITH_RULE_FULL_BUDGET = LIST_WITH_QUERY_FULL_BUDGET + RULE_LINE_MAX + 1
#: Measured by rendering that synthetic worst case (ten-digit counts, every
#: echo at its widest, the worst `fields` rule line with 32 task tags and the
#: approximate token): the smallest caps at which the row renders, and at
#: which it keeps 40 + 40. The guarantees above stay the stated figures.
LIST_WITH_RULE_MIN_MEASURED = 1878
LIST_WITH_RULE_FULL_MEASURED = 1955
#: #434 D5: the agenda with its rule line is refused below this.
AGENDA_WITH_RULE_MIN_BUDGET = AGENDA_MIN_BUDGET + RULE_LINE_MAX + 1
RULE_SCOPE_ALL = 'pass scope="all" to list every checkbox'
CONTEXT_PREFIX = "    ┆ "
CONTEXT_CUT_LINE = CONTEXT_PREFIX + "(context cut)"
CAP_SETTING = "MAX_READ_RESPONSE_CHARS"
SCOPE_TAIL = "with this call's folder, tags, exclude_tags and scope"


def _t():
    from src.mcp_server import tools

    return tools


def _cut(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"


def cap_refusal(budget: int, needed: int | None = None, what: str = "list_tasks") -> QueryRefusal:
    """`response_cap_too_small` (D12/D13): never an empty page with a cursor."""
    need = f" (this response needs at least {needed})" if needed else ""
    return QueryRefusal(
        "response_cap_too_small",
        f"{what}: {CAP_SETTING} is {budget}, too small for even one abbreviated "
        f"row of this response{need}; raise {CAP_SETTING} or narrow the query.",
    )


# ── ids and rows ─────────────────────────────────────────────────────────────


def task_id(row: dict) -> str | None:
    """The D1 id of a row whose `task_key` is populated, whatever its note's
    extraction version (D11); None (rendered `id pending`) otherwise."""
    key = row.get("task_key")
    if key is None:
        return None
    try:
        return task_line.render_id(str(key).strip(), row["key_ordinal"], row["key_count"])
    except (TypeError, ValueError, KeyError):
        return None


def _id_token(row: dict) -> str:
    tid = task_id(row)
    return f"id {tid}" if tid else "id pending"


def _with_id_slot(row: dict, extra: str = "") -> dict:
    """The row with its line slot carrying ` — id <id>` (see module doc)."""
    return {**row, "line": f"{row['line']} — {_id_token(row)}{extra}"}


def render_full_row(row: dict) -> str:
    """D12 `detail="full"`: the #350 row with ` — id <id>` after `path:line`."""
    return _t().render_task_row(_with_id_slot(row))


def _has_context(row: dict) -> bool:
    return bool(row.get("context")) or bool(row.get("context_cut"))


def render_context_lines(row: dict) -> list[str]:
    """D12 `context=true`: each stored context line as `    ┆ ` + the line on
    one line; `    ┆ (context cut)` when the stored context was cut. A line
    can never begin with `- [`."""
    one_line = _t()._one_line
    out = []
    for raw in (row.get("context") or "").split("\n"):
        shown = one_line(raw)
        if shown:
            out.append(CONTEXT_PREFIX + shown)
    if row.get("context_cut"):
        out.append(CONTEXT_CUT_LINE)
    return out


def _brief_tags(tags: list[str] | None, cut: bool) -> str:
    one_line = _t()._one_line
    tokens = ["#" + one_line(t) for t in (tags or [])]
    for k in range(len(tokens), -1, -1):
        hidden = len(tokens) - k
        parts = tokens[:k]
        if hidden:
            parts = parts + [f"…+{hidden}"]
        if cut:
            parts = parts + ["tags cut"]
        seg = " ".join(parts)
        if len(seg) <= BRIEF_TAGS_MAX:
            return seg
    return ""  # pragma: no cover - k == 0 always fits


def _brief_desc(row: dict) -> str:
    desc = _t()._one_line(task_line.description_region(row["text"] or ""))
    if len(desc) > BRIEF_DESC_MAX:
        return desc[: BRIEF_DESC_MAX - 1] + "…"
    if row.get("text_cut"):
        return (desc[: BRIEF_DESC_MAX - 1] if len(desc) >= BRIEF_DESC_MAX else desc) + "…"
    return desc


def render_brief_row(row: dict) -> str:
    """D12 `detail="brief"`: `- [s] <id> \\`<path>\\`:<line> — <dates> — p:<priority>
    — <tags> — <desc>`. At most `BRIEF_ROW_MAX` characters plus the escaped
    path, which is never cut here. A row without an id shows `(id pending)`."""
    tools = _t()
    tid = task_id(row) or "(id pending)"
    head = f"- [{row['status_char']}] {tid} `{tools._escape_task_path(row['file_path'])}`:{row['line']}"
    parts = [head]
    dates = " ".join(
        f"{label} {value.isoformat()}"
        for label, value in (("due", row.get("due")), ("sched", row.get("scheduled")),
                             ("start", row.get("start")), ("done", row.get("done_on")))
        if value is not None
    )
    if dates:
        parts.append(dates)
    if row.get("priority"):
        parts.append(f"p:{row['priority']}")
    tags = _brief_tags(row.get("tags"), bool(row.get("tags_cut")))
    if tags:
        parts.append(tags)
    parts.append(_brief_desc(row))
    return " — ".join(parts)


def render_abbreviated_row(row: dict, room: int, *, context_omitted: bool = False) -> str:
    """The D12 abbreviated first row: the #350 abbreviation (dates and
    priority kept, tags and heading omitted with a note, text and if need be
    the path cut, `— abbreviated` last) with the id token after `path:line`
    and, when context was asked for and the row has some, `— context
    omitted`."""
    extra = " — context omitted" if context_omitted else ""
    return _t().render_abbreviated_task_row(_with_id_slot(row, extra), room)


# ── list_tasks header ────────────────────────────────────────────────────────


def render_header_line1(page: TaskPage, *, shown: int, more: bool) -> str:
    return _header_line1(page, shown=shown, more=more, line_max=HEADER_MAX)


def _header_line1(page: TaskPage, *, shown: int, more: bool, line_max: int) -> str:
    """Line 1: the #350 header, tokens and order unchanged, plus
    `task ids pending: N tasks` after `over the task cap`, then `tasks with
    cut tags` (with `tags` / `exclude_tags`) and `tasks with cut text` (with
    `text`). Echoes cut at 64; at most `HEADER_MAX`."""
    n = page.args
    due_by = n.dates.get("due_by")
    scheduled_by = n.dates.get("scheduled_by")

    def build(folder: str | None) -> str:
        line = _t().render_task_header(
            status=n.status, folder=folder,
            due_by=due_by.given if due_by else None,
            scheduled_by=scheduled_by.given if scheduled_by else None,
            tags=n.tags, shown=shown, more=more, not_scanned=page.not_scanned,
            failed=page.failed, capped=page.capped, cut_tags=None,
        )
        line += f" — task ids pending: {page.ids_pending} tasks"
        if page.cut_tags is not None:
            line += f" — tasks with cut tags: {page.cut_tags}"
        if page.cut_text is not None:
            line += f" — tasks with cut text: {page.cut_text}"
        return line

    line = build(n.folder)
    if len(line) > line_max and n.folder:
        # Only at the synthetic extreme (every count 10 digits, both cut
        # counts, a 64-character folder echo): the folder echo gives way so
        # the #350 line bound holds; it stays labelled cut (`…`).
        over = len(line) - line_max
        keep = max(min(len(n.folder), _legacy_echo_max()) - 1 - over, 1)
        line = build(n.folder[:keep] + "…")
        while len(line) > line_max and keep > 1:
            # An escaped character is wider than one: shrink until it fits.
            keep -= 1
            line = build(n.folder[:keep] + "…")
    return line


def _legacy_echo_max() -> int:
    return _t().LIST_TASKS_ECHO_MAX


def _tz_part(timezone: str, unavailable: bool) -> str:
    return f"timezone: {timezone}" + (" (configured zone unavailable)" if unavailable else "")


def _names_part(label: str, names: tuple[str, ...], room: int) -> str:
    """`<label>: N [a, b +K more]`, listing names (each cut at 32) while the
    part stays within `room`; the count is always present."""
    tools = _t()
    base = f"{label}: {len(names)}"
    if not names:
        return base
    shown: list[str] = []
    for i, name in enumerate(names):
        candidate = shown + [tools._echo_path(name, QUERY_ECHO_MAX)]
        rest = len(names) - len(candidate)
        part = f"{base} [{', '.join(candidate)}{f' +{rest} more' if rest else ''}]"
        if len(part) > room:
            break
        shown = candidate
    rest = len(names) - len(shown)
    if not shown:
        return f"{base} [+{rest} more]" if len(f"{base} [+{rest} more]") <= room else base
    return f"{base} [{', '.join(shown)}{f' +{rest} more' if rest else ''}]"


def render_query_line(page: TaskPage) -> str:
    """Line 2 (D12): `Query — timezone: <tz> — today: <date> — <echoes> —
    scope: … — excluded by scope: N tasks — excluded folders: N [names…] —
    excluded note tags: N [names…]`, at most `QUERY_LINE_MAX`. The timezone,
    today and every resolved date are never cut; the counts are always
    present; names are listed while they fit."""
    return _query_line(page)


def _query_line(page: TaskPage, *, line_max: int = QUERY_LINE_MAX,
                echo_max: int | None = None) -> str:
    """`render_query_line` with a smaller line or echo bound: an empty page
    whose header would not fit the response cap shortens its echoes instead
    of being refused (the mandatory parts are never cut)."""
    tools = _t()
    n = page.args
    ex = page.exclusions

    def build(echo_max: int) -> list[str]:
        parts = [f"Query — {_tz_part(n.timezone, n.timezone_unavailable)}",
                 f"today: {n.today.isoformat()}"]
        for name in DATE_ARGS:
            rd = n.dates.get(name)
            if rd is None:
                continue
            if rd.relative:
                parts.append(f"{name}: {rd.given}={rd.value.isoformat()}")
            elif name not in ("due_by", "scheduled_by"):  # line 1 echoes those
                parts.append(f"{name}: {rd.value.isoformat()}")
        for name in ("undated", "has_due", "recurring"):
            value = getattr(n, name)
            if value is not None:
                parts.append(f"{name}: {'true' if value else 'false'}")
        if n.text is not None:
            parts.append(f"text: {_cut(tools._one_line(n.text), echo_max)}")
        if n.priority:
            parts.append(f"priority: {_cut(','.join(n.priority), echo_max)}")
        if n.exclude_tags:
            parts.append(
                f"exclude_tags: {_cut(' '.join('#' + t for t in n.exclude_tags), echo_max)}")
        if n.path is not None:
            parts.append(f"path: {tools._echo_path(n.path, echo_max)}")
        parts.append(f"scope: {ex.scope}")
        parts.append(f"excluded by scope: {ex.excluded} tasks")
        return parts

    counts = [f"excluded folders: {len(ex.folders)}", f"excluded note tags: {len(ex.note_tags)}"]
    if echo_max is not None:
        parts = build(echo_max)
    else:
        parts = build(QUERY_ECHO_MAX)
        if len(" — ".join(parts + counts)) > line_max:
            parts = build(QUERY_ECHO_TIGHT)
    fixed = len(" — ".join(parts + counts))
    # Names fill what is left, folders first; each part keeps its count.
    spare = line_max - fixed
    folders = _names_part("excluded folders", ex.folders, len(counts[0]) + max(spare, 0))
    spare -= len(folders) - len(counts[0])
    tags = _names_part("excluded note tags", ex.note_tags, len(counts[1]) + max(spare, 0))
    return " — ".join(parts + [folders, tags])


# ── the rule line (#434, D5) ─────────────────────────────────────────────────


def render_rule_line(rule: RuleCounts, *, line_max: int = RULE_LINE_MAX) -> str:
    """The rule line, at most `line_max` characters (default
    `RULE_LINE_MAX`). Every count is stated, even at zero; the filter text is
    one JSON string literal (`task_rule.quote`), so it cannot forge a token,
    a row, an id or a cursor; task-tag names (grammar text) are listed,
    each cut at 32, only while the line stays within `line_max` — a smaller
    `line_max` makes them give way first (an empty page's header)."""
    tail = RULE_SCOPE_ALL
    if rule.unusable:
        return (f"Rule — task rule: all (the configured rule cannot be applied: "
                f"{rule.unusable}) — {tail}")
    if rule.kind == task_rule.RULE_FILTER:
        return (f"Rule — task rule: filter {task_rule.quote(rule.global_filter or '')} — "
                f"excluded by rule: {rule.excluded} tasks — kept though unsure (text cut): "
                f"{rule.unsure} tasks — {tail}")
    approx = (f" — rule approximate for {rule.pending} tasks until the next index pass"
              if rule.pending else "")
    head = "Rule — task rule: fields"
    rest = (f"excluded by rule: {rule.excluded} tasks — kept though unsure (tags cut): "
            f"{rule.unsure} tasks{approx} — {tail}")
    count = f"task tags: {len(rule.tags)}"
    room = line_max - len(f"{head} —  — {rest}")
    names = _names_part("task tags", tuple(rule.tags), max(room, len(count)))
    return f"{head} — {names} — {rest}"


def _rule_cost(rule: RuleCounts | None) -> int:
    return len(render_rule_line(rule)) + 1 if rule is not None else 0


# ── list_tasks response ──────────────────────────────────────────────────────


def render_task_notice(page: TaskPage) -> str | None:
    """The closing incomplete-list notice. Byte-identical to #350's
    (`tools.render_incomplete_notice`) unless this call uses a filter that a
    row awaiting the version-5 backfill can only approximate (`text`,
    `recurring` — `task_query.pending_v5_filters`) while such rows exist
    among the matches of every other filter: then it names those filters,
    and that the next index pass fixes it (the count is line 1's
    `task ids pending`). Never silent."""
    tools = _t()
    pending = pending_v5_filters(page.args) if page.ids_pending else []
    if not pending:
        return tools.render_incomplete_notice(page.not_scanned, page.failed)
    # Short on purpose: it is reserved before any row, and the counts are
    # in line 1 (`task ids pending`). At most 120 characters.
    reasons = []
    if page.not_scanned:
        reasons.append("unscanned notes")
    if page.failed:
        reasons.append("unindexable tasks")
    lead = "Incomplete list: " + (", ".join(reasons) + "; " if reasons else "")
    names = " and ".join(pending)
    return (f"{lead}{names} filter{'s' if len(pending) > 1 else ''} approximate "
            "until the next index pass.")


def _row_unit(row: dict, *, detail: str, context: bool) -> list[str]:
    line = render_brief_row(row) if detail == "brief" else render_full_row(row)
    return [line, *(render_context_lines(row) if context else [])]


def _footer(spec, row: dict, *, so_far: int, budget: int) -> str:
    full = "next: " + encode_cursor(spec, row, compact=False)
    if len(full) - len("next: ") <= 2048 and so_far + len(full) <= budget:
        return full
    return "next: " + encode_cursor(spec, row, compact=True)


def render_list_tasks(page: TaskPage, *, budget: int) -> str | QueryRefusal:
    """The `list_tasks` response (D12), at most `budget` characters.

    Line 1, the query line when `page.query_line`, the rows (each with its
    context lines when asked), the closing incomplete notice, and the
    version-2 `next:` cursor from the last row actually rendered. A first row
    that does not fit is abbreviated; when not even that fits — or the
    header alone does not — the result is `response_cap_too_small`."""
    tools = _t()
    n = page.args
    worst_header = render_header_line1(page, shown=999, more=True)
    qline = render_query_line(page) if page.query_line else None
    rline = render_rule_line(page.rule) if page.rule is not None else None
    notice = render_task_notice(page)
    fixed = len(worst_header) + (len(qline) + 1 if qline else 0) + (len(notice) + 1 if notice else 0)
    fixed += len(rline) + 1 if rline else 0
    room = budget - fixed - 1 - FOOTER_MAX - 1

    rendered: list[list[str]] = []
    used = 0
    last_index = -1
    for i, row in enumerate(page.rows):
        unit = _row_unit(row, detail=n.detail, context=n.context)
        cost = sum(len(x) + 1 for x in unit)
        if used + cost <= room:
            rendered.append(unit)
            used += cost
            last_index = i
            continue
        if not rendered:
            # Exact accounting for the one abbreviated row: the real header
            # (`shown: 1`) and the real compact footer of this very row, or
            # none when nothing follows it.
            omitted = n.context and _has_context(row)
            more_after = page.more_rows or i < len(page.rows) - 1
            exact = len(render_header_line1(page, shown=1, more=more_after)) + 1
            exact += (len(qline) + 1 if qline else 0) + (len(notice) + 1 if notice else 0)
            exact += len(rline) + 1 if rline else 0
            if more_after:
                exact += len("next: " + encode_cursor(page.cursor, row, compact=True)) + 1
            row_room = budget - exact
            line = render_abbreviated_row(row, max(row_room, 0), context_omitted=omitted)
            if len(line) > row_room:
                # Not even the abbreviated row fits: refuse rather than
                # return an empty page with a cursor (D12, owner decision).
                return cap_refusal(budget, fixed + 2 + FOOTER_MAX + len(line))
            rendered.append([line])
            last_index = i
        break
    if not page.rows:
        return _render_empty(page, budget=budget, qline=qline, notice=notice)
    more = page.more_rows or last_index < len(page.rows) - 1
    lines = [render_header_line1(page, shown=len(rendered), more=more)]
    if qline:
        lines.append(qline)
    if rline:
        lines.append(rline)
    for unit in rendered:
        lines.extend(unit)
    if notice:
        lines.append(notice)
    if more and last_index >= 0:
        so_far = len("\n".join(lines)) + 1
        lines.append(_footer(page.cursor, page.rows[last_index], so_far=so_far, budget=budget))
    out = "\n".join(lines)
    if len(out) > budget:
        return cap_refusal(budget, len(out))
    return out


def _render_empty(page: TaskPage, *, budget: int, qline: str | None,
                  notice: str | None) -> str:
    """An empty result is the header, never a refusal (spec "Empty
    result"). When it does not fit the cap, the echoes give way step by
    step: the query line's free-text echoes and exclusion names, then line
    1's folder echo. The mandatory parts (timezone, today, every resolved
    date, every count) are never cut. A rule line (#434) gives way first:
    its task-tag names go before any other echo."""
    tail = [notice] if notice else []
    steps = [(HEADER_MAX, None, QUERY_LINE_MAX, RULE_LINE_MAX),
             (HEADER_MAX, None, QUERY_LINE_MAX, 0),
             (HEADER_MAX, 8, 0, 0), (HEADER_MAX, 1, 0, 0), (0, 1, 0, 0)]
    if page.rule is None:
        steps = steps[:1] + steps[2:]
    out = ""
    for header_max, echo_max, line_max, rule_max in steps:
        lines = [_header_line1(page, shown=0, more=False, line_max=header_max)]
        if page.query_line:
            lines.append(qline if echo_max is None and qline else
                         _query_line(page, line_max=line_max, echo_max=echo_max))
        if page.rule is not None:
            lines.append(render_rule_line(page.rule, line_max=rule_max))
        out = "\n".join(lines + tail)
        if len(out) <= budget:
            return out
    return out


# ── task_agenda ──────────────────────────────────────────────────────────────


def _section_title(section: AgendaSection, d: datetime.date, days: int) -> str:
    day = datetime.timedelta(days=1)
    end = d + datetime.timedelta(days=days)
    titles = {
        "overdue": f"Overdue (due on or before {(d - day).isoformat()})",
        "today": f"Due today ({d.isoformat()})",
        "scheduled_undated": f"Scheduled and now due, no due date (scheduled on or before {d.isoformat()})",
        "scheduled_later": (f"Scheduled and now due, due later (scheduled on or before "
                            f"{d.isoformat()}, due after {end.isoformat()})"),
        "upcoming": f"Upcoming (due {(d + day).isoformat()} to {end.isoformat()})",
    }
    return titles[section.key]


def render_section_header(section: AgendaSection, d: datetime.date, days: int, shown: int) -> str:
    """`## <title> — total: N tasks[ — showing k | — not shown (response cap)]`."""
    line = f"## {_section_title(section, d, days)} — total: {section.total} tasks"
    if shown < section.total:
        line += " — not shown (response cap)" if shown == 0 else f" — showing {shown}"
    return line


def render_continuation(section: AgendaSection, cursor: str | None) -> str:
    """`continue: list_tasks(<args, dates resolved>[, cursor="…"]) with this
    call's folder, tags, exclude_tags and scope`."""
    args = [f"{name}={json.dumps(value)}" for name, value in section.continuation]
    if cursor is not None:
        args.append(f"cursor={json.dumps(cursor)}")
    return f"continue: list_tasks({', '.join(args)}) {SCOPE_TAIL}"


#: The longest compact version-2 cursor (pinned by a test).
COMPACT_CURSOR_MAX = FOOTER_MAX - len("next: ")


def render_agenda_header(page: AgendaPage) -> str:
    tools = _t()
    date = (f"{page.date_given}={page.date.isoformat()}" if page.date_relative
            else page.date.isoformat())
    parts = [f"Agenda — {_tz_part(page.timezone, page.timezone_unavailable)}",
             f"date: {date}", f"days: {page.days}"]
    if page.folder:
        parts.append(f"folder: {tools._echo_path(page.folder, QUERY_ECHO_MAX)}")
    if page.tags:
        parts.append(f"tags: {_cut(' '.join('#' + t for t in page.tags), QUERY_ECHO_MAX)}")
    if page.exclude_tags:
        parts.append(
            f"exclude_tags: {_cut(' '.join('#' + t for t in page.exclude_tags), QUERY_ECHO_MAX)}")
    ex = page.exclusions
    parts += [
        f"scope: {ex.scope}",
        f"excluded by scope: {ex.excluded} tasks",
        f"excluded folders: {len(ex.folders)}",
        f"excluded note tags: {len(ex.note_tags)}",
        f"not yet scanned for tasks: {page.not_scanned} notes",
        f"tasks could not be indexed: {page.failed} notes",
        f"over the task cap: {page.capped} notes",
        f"task ids pending: {page.ids_pending} tasks",
    ]
    if page.cut_tags is not None:
        parts.append(f"tasks with cut tags: {page.cut_tags}")
    return " — ".join(parts)


def render_task_agenda(page: AgendaPage, *, budget: int) -> str | QueryRefusal:
    """The `task_agenda` response (D13), at most `budget` characters.

    Sections render in table order with brief rows; upcoming rows are grouped
    under `### <due date>` lines. Room for every later section's header and
    cursor-less continuation is reserved before a section's rows, so every
    section always states its total and, when not complete, its exact
    continuation — with a cursor only when it showed at least one row."""
    minimum = AGENDA_MIN_BUDGET if page.rule is None else AGENDA_WITH_RULE_MIN_BUDGET
    if budget < minimum:
        return cap_refusal(budget, minimum, what="task_agenda")
    tools = _t()
    header = render_agenda_header(page)
    rline = render_rule_line(page.rule) if page.rule is not None else None
    notice = tools.render_incomplete_notice(page.not_scanned, page.failed)
    d, days = page.date, page.days

    def minimal(section: AgendaSection) -> int:
        cost = len(render_section_header(section, d, days, 0)) + 1
        if section.total:
            cost += len(render_continuation(section, None)) + 1
        return cost

    used = len(header) + 1 + (len(notice) + 1 if notice else 0)
    out: list[str] = [header]
    if rline:
        out.append(rline)
        used += len(rline) + 1
    for idx, section in enumerate(page.sections):
        future = sum(minimal(s) for s in page.sections[idx + 1:])
        head_worst = len(render_section_header(section, d, days, max(section.total - 1, 1))) + 1
        head_worst = max(head_worst, len(render_section_header(section, d, days, 0)) + 1)
        cont_worst = (len(render_continuation(section, None)) + len(', cursor=""')
                      + COMPACT_CURSOR_MAX + 1)
        room = budget - used - future - head_worst - cont_worst
        body: list[str] = []
        shown_rows: list[dict] = []
        body_cost = 0
        group = None
        for row in section.rows:
            unit = []
            if section.key == "upcoming" and row["due"] != group:
                unit.append(f"### {row['due'].isoformat()}")
            unit.append(render_brief_row(row))
            cost = sum(len(x) + 1 for x in unit)
            if body_cost + cost > room:
                break
            if section.key == "upcoming":
                group = row["due"]
            body.extend(unit)
            body_cost += cost
            shown_rows.append(row)
        shown = len(shown_rows)
        lines = [render_section_header(section, d, days, shown), *body]
        if shown < section.total:
            cursor = None
            if shown_rows:
                full = encode_cursor(section.cursor, shown_rows[-1], compact=False)
                compact = encode_cursor(section.cursor, shown_rows[-1], compact=True)
                cursor = full if len(full) <= len(compact) else compact
            lines.append(render_continuation(section, cursor))
        out.extend(lines)
        used += sum(len(x) + 1 for x in lines)
    if notice:
        out.append(notice)
    text = "\n".join(out)
    if len(text) > budget:  # pragma: no cover - the reservations prevent it
        return cap_refusal(budget, len(text), what="task_agenda")
    return text


__all__ = [
    "ABBREVIATED_ID_TOKEN_MAX", "ABBREVIATED_STRUCTURE_MAX", "AGENDA_MIN_BUDGET",
    "AGENDA_WITH_RULE_MIN_BUDGET", "LIST_WITH_RULE_FULL_BUDGET", "LIST_WITH_RULE_MIN_BUDGET",
    "RULE_LINE_MAX", "render_rule_line",
    "AGENDA_SECTIONS", "BRIEF_ROW_MAX", "COMPACT_CURSOR_MAX", "FOOTER_MAX", "HEADER_MAX",
    "ID_MAX", "LIST_MIN_BUDGET", "LIST_WITH_QUERY_MIN_BUDGET", "QUERY_LINE_MAX",
    "cap_refusal", "render_abbreviated_row", "render_agenda_header",
    "render_brief_row", "render_context_lines", "render_continuation", "render_full_row",
    "render_header_line1", "render_list_tasks", "render_query_line", "render_section_header",
    "render_task_agenda", "task_id",
]
