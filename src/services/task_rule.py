"""The task rule: which Markdown checkboxes count as tasks (#434, design D1–D9).

One per owner scope, part of `EffectiveTaskSettings` (`task_rule`,
`task_tags`, `global_filter`, `task_rule_unusable`):

- `all` — every checkbox is a task (the default; no predicate is built, so
  every statement and every response is byte-identical to v1.3.0);
- `fields` — a checkbox carrying a task field (due, scheduled, start,
  created, done or cancelled date; priority; recurrence rule) or one of the
  scope's task tags (matched as `list_tasks(tags=…)` matches);
- `filter` — a checkbox whose stored text contains the global filter text,
  a case-sensitive substring test, as the Tasks plugin's global filter.

**One place.** `rule_clause` is the rule as SQL. It is composed into the
default-scope keep predicate `task_query.scope_clause`, which the `list_tasks`
page and counts, the `task_agenda` totals and rows, and `add_task`'s
index-side duplicate scan all use; nothing else decides what is in scope.
`extracted_is_task` is its Python twin over an `ExtractedTask`, used only for
the write tools' statement ("not a task under your task rule").

**Never silent.** A row whose verdict depended on cut data is kept and
counted (`unsure_clause`, D3); under `fields`, rows awaiting the version-5
backfill are judged approximately and the rule line says so
(`pending_approximate`, D2); a rule the query cannot evaluate is treated as
`all` by `task_settings._merge`, which records why (`resolve`, D8).

**Pure and import-light**: `config.py` calls the validators from inside
`Settings` field validators, so this module imports nothing that imports
`src.config` at module load. SQLAlchemy and the models are imported inside
the functions that build SQL.
"""
from __future__ import annotations

import json
import re
from typing import Any

from src.services import task_line

RULES = ("all", "fields", "filter")
#: `task_settings.task_rule` is `varchar(8)`; NULL means "the server's rule".
RULE_ALL, RULE_FIELDS, RULE_FILTER = RULES
#: D9: the filter text, `task_settings.global_filter` `varchar(64)`.
FILTER_MAX_CHARS = 64
#: D8: task tags per scope, each `varchar(128)`.
TASK_TAGS_MAX = 32
TAG_MAX_CHARS = 128
#: The card's tag grammar after one leading `#` is stripped and the tag
#: lower-cased: the inline-tag rule, no trailing `/` (shared with
#: `task_settings.normalise_tag`).
TAG_RE = re.compile(r"[a-z][a-z0-9_/-]*")

#: D2: the version-5 signifiers a pending row is judged by under `fields`.
_PENDING_SIGNIFIERS = (
    "\U0001F501",  # 🔁 recurrence
    "➕",  # ➕ created
    "❌",  # ❌ cancelled
)

#: The reasons `resolve` gives for treating a configured rule as `all`.
UNUSABLE_UNKNOWN = "unknown rule"
UNUSABLE_NO_FILTER = "filter rule without filter text"
UNUSABLE_BAD_FILTER = "filter text not allowed"

FILTER_NEEDS_TEXT = (
    "The “filter” rule needs a global filter text. Enter the text every task "
    "line contains (for example #task), or choose another rule."
)


# ── the structure check shared with task_write ──────────────────────────────

_CONTEXT_STRUCTURE_RE = re.compile(
    r"[ \t]*(?:[-*+](?:[ \t]|$)|[0-9]{1,9}[.)](?:[ \t]|$)|\[.\]|#{1,6}(?:[ \t]|$)|>)"
)
_FENCE_RUN_RE = re.compile(r"`{3,}|~{3,}")


def structure_problem(text: str, *, context: bool) -> str | None:
    """The caller-text structure rules (#428 S5 audit item 4), as a problem
    phrase or None: an odd number of ``%%`` (an Obsidian comment left open),
    an HTML comment opener without its closer (or a closer without an
    opener, or a nested opener), a run of three or more backticks or tildes;
    and, with ``context``, text beginning with a list marker, a checkbox, a
    heading marker or ``>``. `task_write._structure_refusal` is this check
    plus its refusal, and the global filter validator calls it too (D9), so
    there is one definition."""
    if text.count("%%") % 2:
        return "an unbalanced %% comment marker"
    if _FENCE_RUN_RE.search(text):
        return "a run of three or more backticks or tildes"
    depth = 0
    pos = 0
    while True:
        o, c = text.find("<!--", pos), text.find("-->", pos)
        if o < 0 and c < 0:
            break
        if o >= 0 and (c < 0 or o < c):
            if depth:
                return "a nested HTML comment opener"
            depth, pos = 1, o + 4
        else:
            if not depth:
                return "an HTML comment closer without its opener"
            depth, pos = 0, c + 3
    if depth:
        return "an HTML comment opener without its closer"
    if context and _CONTEXT_STRUCTURE_RE.match(text):
        return "text that begins with a list marker, checkbox, heading marker or '>'"
    return None


# ── validators (D9): shared by `Settings` and `task_settings.save` ──────────

#: Every C0/C1 control, DEL, U+0085 and the line/paragraph separators.
_SINGLE_LINE_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u0085  ]")
_FE0F = "️"


def normalise_rule(value: Any) -> tuple[str | None, str | None]:
    """`(rule, error)` for an environment value: stripped, case-insensitive,
    one of `RULES`."""
    text = ("" if value is None else str(value)).strip().lower()
    if text in RULES:
        return text, None
    return None, f"must be one of {', '.join(RULES)} (got {str(value)[:32]!r})."


def normalise_task_tag(value: Any) -> tuple[str | None, str | None]:
    """`(lower-case tag without '#', error)` for one task-tag entry."""
    raw = value if isinstance(value, str) else str(value)
    shown = raw[:80]
    tag = raw[1:] if raw.startswith("#") else raw
    tag = tag.lower()
    if len(tag) > TAG_MAX_CHARS:
        return None, f"Task tag {shown!r}… is longer than {TAG_MAX_CHARS} characters."
    if not TAG_RE.fullmatch(tag) or tag.endswith("/"):
        return None, (
            f"Task tag {shown!r} is not a tag: it must start with a letter and "
            "contain only letters, digits, '_', '-' and '/', with no trailing '/'."
        )
    return tag, None


def normalise_task_tags(values) -> tuple[list[str], list[str]]:
    """`(tags, errors)` for a list of entries: each normalised, duplicates
    dropped, at most `TASK_TAGS_MAX`."""
    out: list[str] = []
    errors: list[str] = []
    for item in values:
        tag, error = normalise_task_tag(item)
        if error is not None:
            errors.append(error)
        elif tag not in out:
            out.append(tag)
    if len(out) > TASK_TAGS_MAX:
        errors.append(f"At most {TASK_TAGS_MAX} task tags are allowed ({len(out)} given).")
    return out, errors


def normalise_filter(value: Any) -> tuple[str | None, str | None]:
    """`(filter text, error)` by the D9 filter rule; blank is `(None, None)`.

    Spaces and tabs are stripped at both ends, then the text must be 1–64
    characters, not whitespace only, one line (no control character, line
    break, U+0085, U+2028 or U+2029), with no backtick, no Tasks field emoji
    and no U+FE0F, no ``%%``, ``<!--`` or ``-->`` at all (balanced or not),
    and it must pass `structure_problem(context=True)` — it begins every
    description `add_task` prepends it to. Each check has its own message.
    """
    if value is None:
        return None, None
    raw = value if isinstance(value, str) else str(value)
    text = raw.strip(" \t")
    if not text:
        return None, None
    shown = json.dumps(text[:FILTER_MAX_CHARS + 1], ensure_ascii=True)
    if len(text) > FILTER_MAX_CHARS:
        return None, f"The global filter text is longer than {FILTER_MAX_CHARS} characters."
    if _SINGLE_LINE_RE.search(text):
        return None, (
            "The global filter text must be a single line: no line break or "
            "control character."
        )
    if all(ch.isspace() for ch in text):
        return None, "The global filter text must not be only whitespace."
    if "`" in text:
        return None, (
            f"The global filter text {shown} contains a backtick, which would "
            "start inline code in every task line it is added to."
        )
    if _FE0F in text or any(ch in task_line.ALL_SIGNIFIERS for ch in text):
        return None, (
            f"The global filter text {shown} contains a Tasks field emoji, which "
            "would add a field to every task line it is added to."
        )
    if "%%" in text:
        return None, (
            f"The global filter text {shown} contains %%, an Obsidian comment "
            "marker."
        )
    if "<!--" in text or "-->" in text:
        return None, (
            f"The global filter text {shown} contains an HTML comment marker "
            "(<!-- or -->)."
        )
    problem = structure_problem(text, context=True)
    if problem is not None:
        return None, f"The global filter text {shown} contains {problem}."
    return text, None


# ── the effective rule (D8) ──────────────────────────────────────────────────


def resolve(kind: Any, tags, filter_text: Any) -> tuple[str, tuple[str, ...], str | None, str | None]:
    """`(kind, task_tags, global_filter, unusable_reason)`: the configured
    rule if it can be evaluated, else `("all", (), None, reason)` (D8's
    defensive fallback: an unknown kind, `filter` with NULL, empty or blank
    text, or a stored filter text the filter rule refuses). `fields` with no
    task tags is valid ("fields only")."""
    # A stored tag outside the tag grammar (only a manual edit can store one)
    # can match no stored task tag, and would be echoed on the rule line, so
    # it is dropped: what is used and echoed is always grammar text.
    tags_t: tuple[str, ...] = ()
    for t in tags or ():
        norm, error = normalise_task_tag(t) if isinstance(t, str) else (None, "x")
        if error is None and norm not in tags_t:
            tags_t += (norm,)
    if kind not in RULES:
        return RULE_ALL, (), None, UNUSABLE_UNKNOWN
    if kind == RULE_FILTER:
        if not isinstance(filter_text, str) or not filter_text.strip():
            return RULE_ALL, (), None, UNUSABLE_NO_FILTER
        checked, error = normalise_filter(filter_text)
        if error is not None or checked != filter_text:
            return RULE_ALL, (), None, UNUSABLE_BAD_FILTER
    return kind, tags_t, filter_text if isinstance(filter_text, str) else None, None


def _kind(eff) -> str:
    return getattr(eff, "task_rule", RULE_ALL) or RULE_ALL


def _tag_values(eff) -> list[str]:
    """The task tags normalised exactly as `list_tasks(tags=…)` values."""
    out: list[str] = []
    for tag in getattr(eff, "task_tags", ()) or ():
        norm = task_line.normalise_tag(tag)
        if norm and norm not in out:
            out.append(norm)
    return out


def in_force(eff) -> bool:
    """True when the rule removes anything (`fields` or `filter`)."""
    return _kind(eff) in (RULE_FIELDS, RULE_FILTER)


def line_present(eff) -> bool:
    """D5: a default-scope response carries the rule line iff the rule is in
    force or a configured rule could not be applied."""
    return in_force(eff) or bool(getattr(eff, "task_rule_unusable", None))


def pending_approximate(eff) -> bool:
    """D2: only `fields` reads version-5 columns a pending row lacks."""
    return _kind(eff) == RULE_FIELDS


# ── SQL ──────────────────────────────────────────────────────────────────────


def _has_field_sql():
    from sqlalchemy import and_, func, or_
    from src.models.db import NoteTask

    # A row awaiting the version-5 backfill (NULL `task_key`) has NULL
    # `recurrence` / `created` / `cancelled`; it is judged by their
    # signifiers in its stored text instead (D2, approximate, stated).
    pending = and_(
        NoteTask.task_key.is_(None),
        or_(*(func.strpos(NoteTask.text, s) > 0 for s in _PENDING_SIGNIFIERS)),
    )
    return or_(
        NoteTask.due.isnot(None), NoteTask.scheduled.isnot(None),
        NoteTask.start.isnot(None), NoteTask.done_on.isnot(None),
        NoteTask.priority.isnot(None), NoteTask.recurrence.isnot(None),
        NoteTask.created.isnot(None), NoteTask.cancelled.isnot(None),
        pending,
    )


def _has_tag_sql(values: list[str]):
    from sqlalchemy import not_
    from src.services.task_query import tags_none_clause

    # `tags_any` = NOT `tags_none`: the NULL-safe exclude_tags clause, with
    # its pending-row branch over the stored `tags` (#428 D12).
    return not_(tags_none_clause(values))


def _filter_sql(text: str):
    from sqlalchemy import String, bindparam, func
    from src.models.db import NoteTask

    return func.strpos(NoteTask.text, bindparam(None, text, type_=String)) > 0


def rule_clause(eff):
    """The rule's *keep* predicate (D1, D2, D3), never NULL on any row, or
    None under `all`. Every column it reads is NOT NULL or tested with
    `IS [NOT] NULL`, so `NOT rule_clause` is exactly the excluded rows."""
    from sqlalchemy import and_, or_
    from src.models.db import NoteTask

    kind = _kind(eff)
    if kind == RULE_FIELDS:
        values = _tag_values(eff)
        alts = [_has_field_sql()]
        if values:
            alts.append(_has_tag_sql(values))
            alts.append(NoteTask.tags_cut.is_(True))
        return or_(*alts)
    if kind == RULE_FILTER:
        text = getattr(eff, "global_filter", None)
        if not text:  # pragma: no cover - `resolve` turns this into `all`
            return None
        return or_(_filter_sql(text), NoteTask.text_cut.is_(True))
    return None


def unsure_clause(eff):
    """D3: kept rows whose verdict depended on cut data, or None."""
    from sqlalchemy import and_, not_
    from src.models.db import NoteTask

    kind = _kind(eff)
    if kind == RULE_FIELDS:
        values = _tag_values(eff)
        if not values:
            return None
        return and_(NoteTask.tags_cut.is_(True), not_(_has_field_sql()),
                    not_(_has_tag_sql(values)))
    if kind == RULE_FILTER:
        text = getattr(eff, "global_filter", None)
        if not text:  # pragma: no cover
            return None
        return and_(NoteTask.text_cut.is_(True), not_(_filter_sql(text)))
    return None


# ── Python twin (D7) ─────────────────────────────────────────────────────────


def extracted_is_task(task, eff) -> bool:
    """`rule_clause` over one `ExtractedTask` (a version-5 extraction, as the
    write tools have): True when the line is a task under the rule."""
    kind = _kind(eff)
    if kind == RULE_FIELDS:
        if any(getattr(task, name, None) is not None for name in (
                "due", "scheduled", "start", "done_on", "priority", "recurrence",
                "created", "cancelled")):
            return True
        values = _tag_values(eff)
        if not values:
            return False
        prefixes = task.tag_prefixes
        if prefixes is None:
            prefixes = task_line.tag_prefixes(task.tags or ())
        return bool(set(prefixes) & set(values)) or bool(task.tags_cut)
    if kind == RULE_FILTER:
        text = getattr(eff, "global_filter", None)
        if not text:  # pragma: no cover
            return True
        return text in (task.text or "") or bool(task.text_cut)
    return True


# ── response text ────────────────────────────────────────────────────────────


def quote(value: str) -> str:
    """A user-set string as one JSON string literal on one line: quotes,
    backslashes and controls escaped, and U+0085, U+2028 and U+2029 too
    (`json.dumps` leaves those raw), so it cannot forge a line, a token or
    end its own quotes."""
    out = json.dumps(value, ensure_ascii=False)
    for ch in ("\u0085", " ", " "):
        out = out.replace(ch, f"\\u{ord(ch):04x}")
    return out


def not_task_note(eff) -> str | None:
    """D7: the write tools' statement for a line that is not a task."""
    kind = _kind(eff)
    if kind == RULE_FIELDS:
        why = "no task field and no task tag"
    elif kind == RULE_FILTER:
        why = f"the line does not contain {quote(eff.global_filter or '')}"
    else:
        return None
    return (f'note: not a task under your task rule "{kind}" ({why}), so default '
            'listings will not show it; list_tasks(scope="all") will.')


def digest_part(eff) -> dict | None:
    """D6: what the cursor digest carries for the rule (None under `all`)."""
    kind = _kind(eff)
    if kind == RULE_FIELDS:
        return {"kind": RULE_FIELDS, "tags": sorted(_tag_values(eff))}
    if kind == RULE_FILTER:
        return {"kind": RULE_FILTER, "filter": eff.global_filter}
    return None


def describe(kind: str, tags, filter_text: str | None) -> str:
    """The rule in words, for the panel card."""
    if kind == RULE_FIELDS:
        words = "only checkboxes with task details or a task tag"
        if tags:
            words += ", task tags: " + ", ".join("#" + t for t in tags)
        return words
    if kind == RULE_FILTER:
        return f"only checkboxes that contain {quote(filter_text or '')}"
    return "every checkbox is a task"


__all__ = [
    "FILTER_MAX_CHARS", "FILTER_NEEDS_TEXT", "RULES", "TASK_TAGS_MAX",
    "describe", "digest_part", "extracted_is_task", "in_force", "line_present",
    "normalise_filter", "normalise_rule", "normalise_task_tag", "normalise_task_tags",
    "not_task_note", "pending_approximate", "quote", "resolve", "rule_clause",
    "structure_problem", "unsure_clause",
]
