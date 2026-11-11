"""The `list_tasks` and `task_agenda` queries (#428, design D12, D13, D18).

Reads `note_tasks` joined to `notes_metadata` under the total owner mapping
(`_note_owner_predicate`, carried in the JOIN's ON clause exactly as the #350
implementation does), never a file. Every statement of one call — the page,
every header count, the compact-cursor resolution — runs in one
`REPEATABLE READ, READ ONLY` snapshot, so the counts describe the page.

This module only *queries*: it validates the arguments (every refusal is
returned before a session is opened), resolves relative dates once in the
caller's task timezone (`task_dates`), and returns the rows plus every count a
response must state. Rendering, the character budget and the decision of
which rows are shown live in `src/mcp_server/task_render.py`, which mints the
version-2 cursor from the last row it actually rendered (`encode_cursor`).

**A listing is never silently wrong.** Each exclusion the query applies is
counted, under the same snapshot, so the renderer can state it:

- `excluded_by_scope` — rows matching every other filter that the default
  scope (the caller's excluded folders and excluded note tags) removed;
- `ids_pending` — matching rows with a NULL `task_key` (awaiting the
  version-5 backfill). With `text` or `recurring` given, the count ignores
  those two filters, because they read version-5 columns a pending row does
  not have yet (`text_fold`, `recurrence`): it is the conservative "rows that
  might match" count;
- `cut_tags` (with `tags` / `exclude_tags`) and `cut_text` (with `text`) —
  rows matching every *other* filter whose stored tags / text were cut;
- `not_scanned`, `failed`, `capped` — the #350 note-level counts.

**Tag matching** is hierarchical by `/` segment and case-insensitive
(`waiting-for` matches `#waiting-for` and `#Waiting-For/chase`, never
`#waiting-forever`). Filter values go through `task_line.normalise_tag`, the
one function the extractor uses for `tag_prefixes`, so both sides agree. A row
not yet re-derived at version 5 has a NULL `tag_prefixes`; for such a row the
same rule is evaluated over its stored `tags` in SQL (lower-case, collapse
`/` runs, strip a trailing `/` — the stored tags are ASCII by the inline-tag
grammar, so `lower()` agrees with Python's under any ctype). A tag filter
therefore never silently drops or keeps a row during the backfill.

**Cursor version 2** (D12): `{"v":2,"o","f","ob","k":[…],"p"|"h","l"}`. `f`
digests every normalised filter with the *resolved* dates, the scope, the
effective exclusion lists, and — when any relative date was resolved — the
resolved "today". A version-1 cursor is refused (`invalid_argument`).
"""
from __future__ import annotations

import base64
import binascii
import dataclasses
import datetime
import hashlib
import json
import re
from typing import Any

from src.services import task_dates, task_line, task_rule

# ── constants ────────────────────────────────────────────────────────────────

STATUSES = ("open", "done", "cancelled", "all")
ORDER_BYS = ("due", "priority", "scheduled", "done", "path")
DETAILS = ("full", "brief")
SCOPES = ("default", "all")
PRIORITY_NAMES = ("highest", "high", "medium", "none", "low", "lowest")
#: R§1.2: the plugin's priority ranks; `none` is a NULL priority.
PRIORITY_RANK = {"highest": 0, "high": 1, "medium": 2, "none": 3, "low": 4, "lowest": 5}

DEFAULT_LIMIT = 100
MAX_LIMIT = 500
MAX_TAG_ARGS = 32
TAG_ARG_MAX_CHARS = 128
TEXT_MAX_CHARS = 256
TEXT_MAX_TERMS = 8
CURSOR_VERSION = 2
CURSOR_MAX_CHARS = 2048
AGENDA_MAX_DAYS = 31

#: D13: the five sections, their caps and their `list_tasks` order.
AGENDA_SECTIONS = (
    ("overdue", 25, "due"),
    ("today", 25, "due"),
    ("scheduled_undated", 15, "scheduled"),
    ("scheduled_later", 15, "scheduled"),
    ("upcoming", 50, "due"),
)

_TAG_ARG_RE = re.compile(r"[a-z][a-z0-9_/-]*")
_HEX16_RE = re.compile(r"[0-9a-f]{16}")
_HEX32_RE = re.compile(r"[0-9a-f]{32}")
_ISO_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")

DATE_ARGS = ("due_from", "due_by", "scheduled_from", "scheduled_by", "done_from", "done_by")
#: The arguments `list_tasks` had before #428; a call using only these keeps
#: its #350 meaning (the 1.0 tool-contract promise).
LEGACY_ARGS = ("folder", "status", "due_by", "scheduled_by", "tags", "limit", "cursor")

#: The plugin's recurrence signifier (R§1.2), read from a pending row's text.
RECURRENCE_SIGNIFIER = "\U0001F501"


def pending_v5_filters(n: "Normalised") -> list[str]:
    """The filters of this call that read a version-5 column and are only
    approximated on a row awaiting the backfill (NULL `task_key`): `text`
    (folded text; falls back to `lower(text)`) and `recurring` (falls back
    to the 🔁 signifier in the stored text). Tags are exact on such rows (the
    stored `tags` are evaluated by the same rule); dates, priority,
    `has_due`, `undated`, the scope and every order read version-4 columns."""
    out = []
    if n.terms:
        out.append("text")
    if n.recurring is not None:
        out.append("recurring")
    return out


CURSOR_FOREIGN = "list_tasks: cursor does not belong to this query; start without one."
CURSOR_GONE = (
    "list_tasks: the note this cursor stopped at is no longer indexed; start "
    "without a cursor."
)


# ── argument and result types ────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class ListTasksArgs:
    """The `list_tasks` arguments exactly as the tool receives them (D12).

    Every #350 argument keeps its name, position and default; every new one
    is optional."""

    folder: str | None = None
    status: str = "open"
    due_by: str | None = None
    scheduled_by: str | None = None
    tags: list[str] | None = None
    limit: int = DEFAULT_LIMIT
    cursor: str | None = None
    due_from: str | None = None
    scheduled_from: str | None = None
    done_from: str | None = None
    done_by: str | None = None
    undated: bool | None = None
    has_due: bool | None = None
    text: str | None = None
    priority: list[str] | None = None
    recurring: bool | None = None
    exclude_tags: list[str] | None = None
    path: str | None = None
    order_by: str = "due"
    detail: str = "full"
    context: bool = False
    scope: str = "default"


@dataclasses.dataclass(frozen=True)
class AgendaArgs:
    """The `task_agenda` arguments (D13)."""

    date: str = "today"
    days: int = 7
    folder: str | None = None
    tags: list[str] | None = None
    exclude_tags: list[str] | None = None
    scope: str = "default"


@dataclasses.dataclass(frozen=True)
class QueryRefusal:
    """A typed refusal: `code` is the `MCP-REFUSAL` code string
    (`invalid_argument`, `response_cap_too_small`); `prose` the message. The
    wiring maps it to a `BodyOutcome`; nothing was written (a read)."""

    code: str
    prose: str


@dataclasses.dataclass(frozen=True)
class CursorSpec:
    """What the renderer needs to mint a version-2 cursor for this query."""

    owner: str
    filters: str
    order_by: str


@dataclasses.dataclass(frozen=True)
class ResolvedDate:
    """One date argument: the value as given and the date it resolved to."""

    name: str
    given: str
    value: datetime.date
    relative: bool


@dataclasses.dataclass(frozen=True)
class Exclusions:
    """The caller's scope as applied (D12 `scope`)."""

    scope: str
    folders: tuple[str, ...]
    note_tags: tuple[str, ...]
    excluded: int


@dataclasses.dataclass(frozen=True)
class RuleCounts:
    """The task rule as one default-scope call applied it (#434, D4, D5).

    Present on a page only while the rule line is: under `scope="default"`
    with a rule in force (`fields` / `filter`) or a configured rule that
    could not be applied (`unusable` set, `kind` then `all`). `excluded`
    counts *other* ∧ L ∧ ¬R, `unsure` the kept rows whose verdict depended on
    cut data, `pending` (under `fields`) the matching rows awaiting the
    version-5 backfill, which the rule judges approximately."""

    kind: str
    tags: tuple[str, ...]
    global_filter: str | None
    unusable: str | None
    excluded: int = 0
    unsure: int = 0
    pending: int = 0


@dataclasses.dataclass
class Normalised:
    """Validated, normalised arguments (internal; also carried to the
    renderer for the echoes)."""

    folder: str | None
    status: str
    dates: dict[str, ResolvedDate]
    tags: list[str]
    exclude_tags: list[str]
    undated: bool | None
    has_due: bool | None
    text: str | None
    terms: list[str]
    priority: list[str]
    recurring: bool | None
    path: str | None
    order_by: str
    detail: str
    context: bool
    scope: str
    limit: int
    timezone: str
    timezone_unavailable: bool
    today: datetime.date
    any_relative: bool


@dataclasses.dataclass
class TaskPage:
    """`list_tasks_query`'s result: the fetched rows (at most `limit`, in
    order) and every count the response must state.

    `rows` are dicts with the #350 keys (`status_char`, `file_path`, `line`,
    `due`, `scheduled`, `start`, `done_on`, `priority`, `tags`, `tags_cut`,
    `heading_path`, `text`, `text_cut`, `state`) plus `task_key`,
    `key_ordinal`, `key_count`, `context`, `context_cut`. `more_rows` is
    True when the query had a row past them."""

    args: Normalised
    rows: list[dict]
    more_rows: bool
    not_scanned: int
    failed: int
    capped: int
    ids_pending: int
    cut_tags: int | None
    cut_text: int | None
    exclusions: Exclusions
    query_line: bool
    cursor: CursorSpec
    #: #434: the rule line's facts, or None when no rule line is shown.
    rule: RuleCounts | None = None


@dataclasses.dataclass
class AgendaSection:
    """One D13 section. `rows` hold at most `cap` rows in the section's
    `order_by`; `total` is the full count; `continuation` the `list_tasks`
    arguments (dates resolved, in order) that continue it, and `cursor` the
    spec for exactly that `list_tasks` query (same `o`, same `f`)."""

    key: str
    cap: int
    total: int
    rows: list[dict]
    continuation: list[tuple[str, Any]]
    cursor: CursorSpec


@dataclasses.dataclass
class AgendaPage:
    timezone: str
    timezone_unavailable: bool
    date: datetime.date
    date_given: str
    date_relative: bool
    days: int
    folder: str | None
    tags: list[str]
    exclude_tags: list[str]
    exclusions: Exclusions
    not_scanned: int
    failed: int
    capped: int
    ids_pending: int
    cut_tags: int | None
    sections: list[AgendaSection]
    #: #434: the rule line's facts, or None when no rule line is shown.
    rule: RuleCounts | None = None


# ── helpers shared with tools.py (imported lazily: tools imports us) ─────────


def _tools():
    from src.mcp_server import tools

    return tools


def owner_discriminator(uid: int | None) -> str:
    """`o`: the #350 owner discriminator, unchanged."""
    return _tools()._task_owner_discriminator(uid)


def _encodable(value: str) -> bool:
    from src.services.vault import is_encodable

    return "\x00" not in value and is_encodable(value)


def _shown(value: Any) -> str:
    return _tools()._echo(str(value))


def _refuse(message: str) -> QueryRefusal:
    return QueryRefusal("invalid_argument", message)


# ── validation ───────────────────────────────────────────────────────────────


def _validate_tags(name: str, tags: Any, tool: str) -> list[str] | QueryRefusal:
    """D12 `tags` / `exclude_tags`: one leading `#` stripped, lower-cased, the
    tag grammar without a trailing `/`, ≤ 32 entries of ≤ 128 characters,
    then normalised with the extractor's own `task_line.normalise_tag`."""
    if tags is None:
        return []
    if not isinstance(tags, list) or len(tags) > MAX_TAG_ARGS:
        return _refuse(f"{tool}: {name} accepts at most {MAX_TAG_ARGS} entries.")
    out: list[str] = []
    for entry in tags:
        if not isinstance(entry, str):
            return _refuse(f"{tool}: every {name} entry must be a string.")
        stripped = entry[1:] if entry.startswith("#") else entry
        if not 1 <= len(stripped) <= TAG_ARG_MAX_CHARS:
            return _refuse(
                f"{tool}: each {name} entry must be 1 to {TAG_ARG_MAX_CHARS} "
                "characters after a leading '#' is removed."
            )
        # Normalised exactly like a stored tag (lower-case, `/` runs
        # collapsed, a trailing `/` stripped), then checked: `work/` means
        # `work`, hierarchically.
        norm = task_line.normalise_tag(stripped)
        if not norm or not _TAG_ARG_RE.fullmatch(norm):
            return _refuse(
                f"{tool}: {name} entry {_shown(stripped)!r} is not a tag: it must "
                "start with a letter and contain only letters, digits, '_', '-' "
                "and '/'."
            )
        if norm not in out:
            out.append(norm)
    return out


def _validate_folder(folder: Any, tool: str) -> str | None | QueryRefusal:
    if folder is None or folder == "":
        return None
    if not isinstance(folder, str) or not _encodable(folder):
        return _refuse(f"{tool}: folder must be a string without NUL or unpaired surrogates.")
    return folder


def _validate_scope(scope: Any, tool: str) -> str | QueryRefusal:
    if scope not in SCOPES:
        return _refuse(f"{tool}: scope must be one of {', '.join(SCOPES)} (got {_shown(scope)!r}).")
    return scope


def _validate_bool(name: str, value: Any) -> QueryRefusal | None:
    if value is not None and not isinstance(value, bool):
        return _refuse(f"list_tasks: {name} must be true or false.")
    return None


def _resolve_date(name: str, value: Any, zone, now, tool: str) -> ResolvedDate | QueryRefusal:
    if not isinstance(value, str):
        return _refuse(f"{tool}: {name} must be a string date.")
    resolved = task_dates.resolve(value, zone, now=now)
    if isinstance(resolved, task_dates.InvalidDate):
        return _refuse(
            f"{tool}: {name} must be a YYYY-MM-DD date or one of today, tomorrow, "
            f"yesterday, +Nd, -Nd (N from 0 to {task_dates.MAX_RELATIVE_DAYS}) "
            f"(got {_shown(value)!r})."
        )
    return ResolvedDate(name, value.lower(), resolved, task_dates.is_relative(value))


def snapshot_now(now: datetime.datetime | None) -> datetime.datetime:
    """The call's one clock read (aware UTC). Every relative date, the echoed
    "today" and the cursor digest derive from this instant, so a call that
    spans midnight cannot resolve its dates against two different days."""
    return now if now is not None else datetime.datetime.now(datetime.timezone.utc)


def _zone(task_settings):
    zone = task_dates.effective_zone(task_settings.timezone)
    unavailable = bool(task_settings.timezone_fallback) or zone.unavailable
    return zone, unavailable


def normalise_list_args(
    args: ListTasksArgs, task_settings, *, now: datetime.datetime | None = None
) -> Normalised | QueryRefusal:
    """Validate and normalise every `list_tasks` argument, before any query.
    Pure apart from the clock (`now`, injectable)."""
    if args.status not in STATUSES:
        return _refuse(
            f"list_tasks: status must be one of {', '.join(STATUSES)} "
            f"(got {_shown(args.status)!r})."
        )
    if args.order_by not in ORDER_BYS:
        return _refuse(
            f"list_tasks: order_by must be one of {', '.join(ORDER_BYS)} "
            f"(got {_shown(args.order_by)!r})."
        )
    if args.detail not in DETAILS:
        return _refuse(
            f"list_tasks: detail must be one of {', '.join(DETAILS)} "
            f"(got {_shown(args.detail)!r})."
        )
    scope = _validate_scope(args.scope, "list_tasks")
    if isinstance(scope, QueryRefusal):
        return scope
    for name in ("undated", "has_due", "recurring"):
        bad = _validate_bool(name, getattr(args, name))
        if bad:
            return bad
    if not isinstance(args.context, bool):
        return _refuse("list_tasks: context must be true or false.")
    folder = _validate_folder(args.folder, "list_tasks")
    if isinstance(folder, QueryRefusal):
        return folder

    now = snapshot_now(now)
    zone, unavailable = _zone(task_settings)
    today = task_dates.today(zone, now=now)
    dates: dict[str, ResolvedDate] = {}
    for name in DATE_ARGS:
        value = getattr(args, name)
        if value is None:
            continue
        r = _resolve_date(name, value, zone, now, "list_tasks")
        if isinstance(r, QueryRefusal):
            return r
        dates[name] = r
    for lo, hi in (("due_from", "due_by"), ("scheduled_from", "scheduled_by"),
                   ("done_from", "done_by")):
        if lo in dates and hi in dates and dates[lo].value > dates[hi].value:
            return _refuse(
                f"list_tasks: {lo} ({dates[lo].value.isoformat()}) is later than "
                f"{hi} ({dates[hi].value.isoformat()})."
            )
    if args.undated is not None:
        clash = [n for n in DATE_ARGS[:4] if n in dates]
        if clash or args.has_due is True:
            what = clash[0] if clash else "has_due=true"
            return _refuse(
                f"list_tasks: undated cannot be combined with {what}: undated "
                "selects on the due and scheduled dates itself."
            )

    tags = _validate_tags("tags", args.tags, "list_tasks")
    if isinstance(tags, QueryRefusal):
        return tags
    exclude = _validate_tags("exclude_tags", args.exclude_tags, "list_tasks")
    if isinstance(exclude, QueryRefusal):
        return exclude

    terms: list[str] = []
    text = args.text
    if text is not None:
        if not isinstance(text, str) or not 1 <= len(text) <= TEXT_MAX_CHARS:
            return _refuse(f"list_tasks: text must be 1 to {TEXT_MAX_CHARS} characters.")
        if not _encodable(text):
            return _refuse("list_tasks: text must not contain NUL or unpaired surrogates.")
        raw_terms = text.split()
        if not raw_terms or len(raw_terms) > TEXT_MAX_TERMS:
            return _refuse(
                f"list_tasks: text must hold 1 to {TEXT_MAX_TERMS} whitespace-separated terms."
            )
        for t in raw_terms:
            folded = task_line.fold_text(t)
            if folded not in terms:
                terms.append(folded)

    priority: list[str] = []
    if args.priority is not None:
        if (not isinstance(args.priority, list) or not 1 <= len(args.priority) <= 6
                or any(p not in PRIORITY_NAMES for p in args.priority)):
            return _refuse(
                "list_tasks: priority must be a list of 1 to 6 of "
                f"{', '.join(PRIORITY_NAMES)}."
            )
        priority = sorted(set(args.priority), key=PRIORITY_RANK.__getitem__)

    path = args.path
    if path is not None:
        from src.services.vault import MAX_PATH_CHARS

        if (not isinstance(path, str) or not path or len(path) > MAX_PATH_CHARS
                or not _encodable(path)):
            return _refuse(
                f"list_tasks: path must be a vault-relative note path of 1 to "
                f"{MAX_PATH_CHARS} characters."
            )

    limit = args.limit
    if isinstance(limit, bool) or not isinstance(limit, int):
        return _refuse("list_tasks: limit must be an integer.")
    limit = max(1, min(limit, MAX_LIMIT))

    return Normalised(
        folder=folder, status=args.status, dates=dates, tags=tags, exclude_tags=exclude,
        undated=args.undated, has_due=args.has_due, text=text, terms=terms,
        priority=priority, recurring=args.recurring, path=path, order_by=args.order_by,
        detail=args.detail, context=args.context, scope=scope, limit=limit,
        timezone=zone.name, timezone_unavailable=unavailable, today=today,
        any_relative=any(d.relative for d in dates.values()),
    )


def uses_new_arguments(args: ListTasksArgs) -> bool:
    """True when any argument after `cursor` differs from its default."""
    default = ListTasksArgs()
    return any(
        getattr(args, f.name) != getattr(default, f.name)
        for f in dataclasses.fields(ListTasksArgs)
        if f.name not in LEGACY_ARGS
    )


def filters_digest(n: Normalised, task_settings) -> str:
    """`f` (D12): every normalised filter with the **resolved** dates, the
    scope and the effective exclusion lists; the resolved "today" when any
    relative date was used. `limit`, `order_by` (carried as `ob`), `detail`
    and `context` are excluded: they do not change the row set."""
    payload = {
        "folder": n.folder or "",
        "status": n.status,
        "dates": {k: v.value.isoformat() for k, v in sorted(n.dates.items())},
        "tags": sorted(n.tags),
        "exclude_tags": sorted(n.exclude_tags),
        "undated": n.undated,
        "has_due": n.has_due,
        "terms": sorted(n.terms),
        "priority": list(n.priority),
        "recurring": n.recurring,
        "path": n.path,
        "scope": n.scope,
        "excluded_folders": sorted(task_settings.excluded_folders),
        "excluded_note_tags": sorted(task_settings.excluded_note_tags),
        "today": n.today.isoformat() if n.any_relative else None,
    }
    # #434 D6: the rule only when it applies, so every payload under `all` (and
    # under `scope="all"`) is byte-identical to v1.3.0's and old cursors live.
    if n.scope == "default":
        part = task_rule.digest_part(task_settings)
        if part is not None:
            payload["rule"] = part
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


# ── cursor version 2 ─────────────────────────────────────────────────────────


def _date_key(value: datetime.date | None, *, low: bool = False) -> str:
    if value is None:
        return "-infinity" if low else "infinity"
    return value.isoformat()


def sort_key_values(row: dict, order_by: str) -> list:
    """`k`: the order's leading key values for one row (D12 table)."""
    if order_by == "due":
        return [_date_key(row["due"])]
    if order_by == "scheduled":
        return [_date_key(row["scheduled"])]
    if order_by == "done":
        return [_date_key(row["done_on"], low=True)]
    if order_by == "priority":
        return [PRIORITY_RANK[row["priority"] or "none"], _date_key(row["due"])]
    return []


def encode_cursor(spec: CursorSpec, row: dict, *, compact: bool) -> str:
    """The version-2 `next` token for the row last rendered. The full form
    carries the path, the compact form its hash; never a `notes_metadata.id`."""
    tools = _tools()
    payload: dict = {"v": CURSOR_VERSION, "o": spec.owner, "f": spec.filters,
                     "ob": spec.order_by, "k": sort_key_values(row, spec.order_by)}
    if compact:
        payload["h"] = tools._task_path_hash(row["file_path"])
    else:
        payload["p"] = row["file_path"]
    payload["l"] = row["line"]
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return tools._b64url(raw.encode("utf-8"))


def _valid_key_date(value: Any, *, low: bool) -> bool:
    if not isinstance(value, str):
        return False
    if value == ("-infinity" if low else "infinity"):
        return True
    if not _ISO_RE.fullmatch(value):
        return False
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def decode_cursor(token: Any, *, owner: str, filters: str, order_by: str) -> dict | QueryRefusal:
    """Validate a version-2 token. Returns `{"k", "l", "p" | "h"}` or a
    refusal (`invalid_argument`): undecodable, wrong version (a version-1
    cursor included) or shape, another owner, other filters, another order."""
    from src.services.vault import MAX_PATH_CHARS

    if not isinstance(token, str) or len(token) > CURSOR_MAX_CHARS:
        return _refuse(CURSOR_FOREIGN)
    try:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", token):
            raise ValueError("alphabet")
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return _refuse(CURSOR_FOREIGN)
    if not isinstance(payload, dict):
        return _refuse(CURSOR_FOREIGN)
    if set(payload) not in ({"v", "o", "f", "ob", "k", "p", "l"},
                            {"v", "o", "f", "ob", "k", "h", "l"}):
        return _refuse(CURSOR_FOREIGN)
    v, o, f, ob, k, line = (payload[x] for x in ("v", "o", "f", "ob", "k", "l"))
    if type(v) is not int or v != CURSOR_VERSION:
        return _refuse(CURSOR_FOREIGN)
    if not (isinstance(o, str) and _HEX16_RE.fullmatch(o)) or not (
        isinstance(f, str) and _HEX16_RE.fullmatch(f)
    ):
        return _refuse(CURSOR_FOREIGN)
    if ob not in ORDER_BYS or not isinstance(k, list):
        return _refuse(CURSOR_FOREIGN)
    if ob in ("due", "scheduled"):
        ok = len(k) == 1 and _valid_key_date(k[0], low=False)
    elif ob == "done":
        ok = len(k) == 1 and _valid_key_date(k[0], low=True)
    elif ob == "priority":
        ok = (len(k) == 2 and type(k[0]) is int and 0 <= k[0] <= 5
              and _valid_key_date(k[1], low=False))
    else:
        ok = k == []
    if not ok:
        return _refuse(CURSOR_FOREIGN)
    if type(line) is not int or not 1 <= line <= 2_147_483_647:
        return _refuse(CURSOR_FOREIGN)
    out: dict = {"k": k, "l": line}
    if "p" in payload:
        p = payload["p"]
        if (not isinstance(p, str) or not p or len(p) > MAX_PATH_CHARS
                or not _encodable(p)):
            return _refuse(CURSOR_FOREIGN)
        out["p"] = p
    else:
        h = payload["h"]
        if not isinstance(h, str) or not _HEX32_RE.fullmatch(h):
            return _refuse(CURSOR_FOREIGN)
        out["h"] = h
    # The binding checks: another owner's boundary, another query's, another order's.
    if o != owner or f != filters or ob != order_by:
        return _refuse(CURSOR_FOREIGN)
    return out


# ── SQL building blocks ──────────────────────────────────────────────────────

def hier_any(column, values: list[str], *, strip_hash: bool = False):
    """`EXISTS` an element of the `varchar[]` `column` that, normalised like
    `task_line.normalise_tag`, equals one of `values` or lies beneath it
    (`value + "/"` prefix) — hierarchical, case-insensitive. `values` are
    already normalised. Correlates to the outer row."""
    from sqlalchemy import func, literal, or_, select

    t = func.unnest(column).table_valued("v").render_derived(name="t")
    v = func.regexp_replace(t.c.v, "^#", "") if strip_hash else t.c.v
    norm = func.rtrim(func.regexp_replace(func.lower(v), "/{2,}", "/", "g"), "/")
    conds = [or_(norm == value, func.left(norm, len(value) + 1) == value + "/")
             for value in values]
    return select(literal(1)).select_from(t).where(or_(*conds)).exists()


def _prefix_array(values: list[str]):
    from sqlalchemy import String, bindparam, cast
    from sqlalchemy.dialects.postgresql import ARRAY

    return cast(bindparam(None, values, type_=ARRAY(String)), ARRAY(String(128)))


def tags_all_clause(values: list[str]):
    """`tags` (D12): every value present, hierarchically. `tag_prefixes @>`
    for a version-5 row; the same rule over the stored `tags` for a row not
    yet re-derived (NULL `tag_prefixes`)."""
    from sqlalchemy import and_, or_
    from src.models.db import NoteTask

    return or_(
        and_(NoteTask.tag_prefixes.isnot(None),
             NoteTask.tag_prefixes.contains(_prefix_array(values))),
        and_(NoteTask.tag_prefixes.is_(None), *[hier_any(NoteTask.tags, [v]) for v in values]),
    )


def tags_none_clause(values: list[str]):
    """`exclude_tags` (D12): no value present, hierarchically. NULL-safe: a
    NULL `tag_prefixes` (pre-version-5 row) is judged by its stored `tags`,
    never dropped by a NULL comparison."""
    from sqlalchemy import and_, not_, or_
    from src.models.db import NoteTask

    return or_(
        and_(NoteTask.tag_prefixes.isnot(None),
             not_(NoteTask.tag_prefixes.overlap(_prefix_array(values)))),
        and_(NoteTask.tag_prefixes.is_(None), not_(hier_any(NoteTask.tags, values))),
    )


def scope_clause(task_settings):
    """The default scope's *keep* predicate (never NULL), or None when it
    keeps everything: the location part (`location_clause`: no excluded
    folder, no excluded note tag) AND the caller's task rule
    (`task_rule.rule_clause`, #434 D4). The one place that decides what is
    in the default scope: the `list_tasks` page and counts, the `task_agenda`
    totals and rows, and `add_task`'s duplicate scan all call it."""
    from sqlalchemy import and_

    parts = [p for p in (location_clause(task_settings), rule_clause(task_settings))
             if p is not None]
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    return and_(*parts)


def rule_clause(task_settings):
    """The task rule's keep predicate (`task_rule.rule_clause`), or None."""
    return task_rule.rule_clause(task_settings)


def location_clause(task_settings):
    """The default scope's location *keep* predicate (never NULL): the note
    is under no excluded folder (the `folder_prefix_clause` semantics) and
    none of its note tags hierarchically matches an excluded note tag. None
    when nothing is excluded. (#428's `scope_clause` body, moved verbatim.)"""
    from sqlalchemy import and_, not_
    from src.services.filters import folder_prefix_clause

    keep = []
    for folder in task_settings.excluded_folders:
        keep.append(not_(folder_prefix_clause(folder)))
    tags = [task_line.normalise_tag(t) for t in task_settings.excluded_note_tags]
    tags = [t for t in tags if t]
    if tags:
        from src.models.db import NoteMetadata

        # Frontmatter may store a note tag as `#private`: one leading `#` is
        # stripped before the same hierarchical, case-insensitive rule.
        keep.append(not_(hier_any(NoteMetadata.tags, tags, strip_hash=True)))
    if not keep:
        return None
    return and_(*keep)


def _infinity(value: str):
    from sqlalchemy import Date, String, bindparam, cast

    return cast(bindparam(None, value, type_=String), Date)


def _rank_expr():
    from sqlalchemy import case
    from src.models.db import NoteTask

    return case(
        {"highest": 0, "high": 1, "medium": 2, "low": 4, "lowest": 5},
        value=NoteTask.priority,
        else_=3,
    )


def order_columns(order_by: str):
    """The D12 sort key as SQL expressions, in order, with their direction."""
    from sqlalchemy import func
    from src.models.db import NoteMetadata, NoteTask

    inf = func.coalesce
    if order_by == "due":
        return [inf(NoteTask.due, _infinity("infinity")), NoteMetadata.file_path, NoteTask.line]
    if order_by == "scheduled":
        return [inf(NoteTask.scheduled, _infinity("infinity")), NoteMetadata.file_path,
                NoteTask.line]
    if order_by == "priority":
        return [_rank_expr(), inf(NoteTask.due, _infinity("infinity")),
                NoteMetadata.file_path, NoteTask.line]
    if order_by == "done":
        return [inf(NoteTask.done_on, _infinity("-infinity")).desc(),
                NoteMetadata.file_path, NoteTask.line]
    return [NoteMetadata.file_path, NoteTask.line]


def keyset_clause(order_by: str, boundary: dict):
    """Rows strictly after `boundary` in `order_by` (D12). `done` mixes
    directions, so its predicate is expanded:
    `d < :d OR (d = :d AND (p, l) > (:p, :l))`."""
    from sqlalchemy import Date, Integer, String, and_, func, or_, tuple_, bindparam, cast
    from src.models.db import NoteMetadata, NoteTask

    p = cast(bindparam(None, boundary["p"], type_=String), String)
    line = bindparam(None, boundary["l"], type_=Integer)
    k = boundary["k"]

    def d(value):
        return cast(bindparam(None, value, type_=String), Date)

    if order_by == "due":
        return tuple_(func.coalesce(NoteTask.due, _infinity("infinity")),
                      NoteMetadata.file_path, NoteTask.line) > tuple_(d(k[0]), p, line)
    if order_by == "scheduled":
        return tuple_(func.coalesce(NoteTask.scheduled, _infinity("infinity")),
                      NoteMetadata.file_path, NoteTask.line) > tuple_(d(k[0]), p, line)
    if order_by == "priority":
        return tuple_(_rank_expr(), func.coalesce(NoteTask.due, _infinity("infinity")),
                      NoteMetadata.file_path, NoteTask.line) > tuple_(
            bindparam(None, k[0], type_=Integer), d(k[1]), p, line)
    if order_by == "done":
        dn = func.coalesce(NoteTask.done_on, _infinity("-infinity"))
        return or_(dn < d(k[0]),
                   and_(dn == d(k[0]), tuple_(NoteMetadata.file_path, NoteTask.line)
                        > tuple_(p, line)))
    return tuple_(NoteMetadata.file_path, NoteTask.line) > tuple_(p, line)


ROW_COLUMNS = (
    "status_char", "line", "due", "scheduled", "start", "done_on", "priority",
    "tags", "tags_cut", "heading_path", "text", "text_cut", "state",
    "task_key", "key_ordinal", "key_count", "context", "context_cut",
)


def _row_select():
    from sqlalchemy import select
    from src.models.db import NoteMetadata, NoteTask

    cols = [getattr(NoteTask, c) for c in ROW_COLUMNS]
    return select(NoteMetadata.file_path, *cols).select_from(NoteTask)


def _join_on(uid):
    from sqlalchemy import and_
    from src.models.db import NoteMetadata, NoteTask

    return and_(NoteMetadata.id == NoteTask.note_id, _tools()._note_owner_predicate(uid))


class _Filters:
    """The predicate groups of one `list_tasks` call, so each count can drop
    exactly the group it reports on."""

    def __init__(self, n: Normalised, task_settings):
        from sqlalchemy import and_, func, not_, or_
        from src.models.db import NoteMetadata, NoteTask
        from src.services.filters import folder_prefix_clause

        base = []
        if n.folder:
            base.append(folder_prefix_clause(n.folder))
        if n.path is not None:
            base.append(NoteMetadata.file_path == n.path)
        if n.status != "all":
            base.append(NoteTask.state == n.status)
        cols = {"due": NoteTask.due, "scheduled": NoteTask.scheduled, "done": NoteTask.done_on}
        for name, rd in n.dates.items():
            family, bound = name.rsplit("_", 1)
            col = cols[family]
            base.append(col >= rd.value if bound == "from" else col <= rd.value)
        if n.undated is True:
            base.append(and_(NoteTask.due.is_(None), NoteTask.scheduled.is_(None)))
        elif n.undated is False:
            base.append(or_(NoteTask.due.isnot(None), NoteTask.scheduled.isnot(None)))
        if n.has_due is True:
            base.append(NoteTask.due.isnot(None))
        elif n.has_due is False:
            base.append(NoteTask.due.is_(None))
        if n.priority:
            named = [p for p in n.priority if p != "none"]
            alts = []
            if named:
                alts.append(NoteTask.priority.in_(named))
            if "none" in n.priority:
                alts.append(NoteTask.priority.is_(None))
            base.append(or_(*alts))
        self.base = base
        # Version-5 filters: a pending row (NULL `text_fold` / `recurrence`)
        # cannot be judged by them, so `ids_pending` drops them.
        self.text = [
            func.strpos(func.coalesce(NoteTask.text_fold, func.lower(NoteTask.text)), term) > 0
            for term in n.terms
        ]
        # A pending row (NULL `task_key`: not yet re-derived at version 5)
        # has no `recurrence` column value to read, so it is judged by its
        # stored text: recurring when the text holds the 🔁 signifier. That
        # is approximate (the rule must be in the trailing field run), so
        # the response always says so (`pending_v5_filters`).
        is_recurring = or_(
            and_(NoteTask.task_key.isnot(None), NoteTask.recurrence.isnot(None)),
            and_(NoteTask.task_key.is_(None), func.strpos(NoteTask.text, RECURRENCE_SIGNIFIER) > 0),
        )
        self.recurring = []
        if n.recurring is True:
            self.recurring.append(is_recurring)
        elif n.recurring is False:
            self.recurring.append(not_(is_recurring))
        tag = []
        if n.tags:
            tag.append(tags_all_clause(n.tags))
        if n.exclude_tags:
            tag.append(tags_none_clause(n.exclude_tags))
        self.tag = tag
        self.scope = scope_clause(task_settings) if n.scope == "default" else None
        # #434 D4: the scope's two parts, so each exclusion is counted once.
        default = n.scope == "default"
        self.location = location_clause(task_settings) if default else None
        self.rule = rule_clause(task_settings) if default else None
        self.unsure = task_rule.unsure_clause(task_settings) if default else None
        self.rule_line = default and task_rule.line_present(task_settings)
        self.pending_ignores_rule = default and task_rule.pending_approximate(task_settings)

    def all(self, *, tag: bool = True, text: bool = True, recurring: bool = True,
            scope: bool = True) -> list:
        """Every predicate, less the groups switched off."""
        out = list(self.base)
        if text:
            out += self.text
        if recurring:
            out += self.recurring
        if tag:
            out += self.tag
        if scope and self.scope is not None:
            out.append(self.scope)
        return out

    def other_and_location(self, **kw) -> list:
        """Every predicate except the default scope's rule part."""
        out = self.all(scope=False, **kw)
        if self.location is not None:
            out.append(self.location)
        return out


def rule_counts(task_settings, *, excluded: int = 0, unsure: int = 0,
                pending: int = 0) -> RuleCounts:
    return RuleCounts(
        kind=task_settings.task_rule, tags=tuple(task_settings.task_tags),
        global_filter=task_settings.global_filter,
        unusable=task_settings.task_rule_unusable,
        excluded=excluded, unsure=unsure, pending=pending,
    )


def _session_factory(session_factory):
    if session_factory is not None:
        return session_factory
    from src import database

    return database.async_session


async def _begin_snapshot(session):
    from sqlalchemy import text

    await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))


async def _resolve_hashed(session, uid, boundary) -> bool:
    """The compact cursor's path, resolved only against the caller's rows."""
    from sqlalchemy import func, select
    from src.models.db import NoteMetadata

    matches = (await session.execute(
        select(NoteMetadata.file_path).where(
            _tools()._note_owner_predicate(uid),
            func.substr(
                func.encode(func.sha256(func.convert_to(NoteMetadata.file_path, "UTF8")), "hex"),
                1, 32,
            ) == boundary["h"],
        ).limit(2)
    )).scalars().all()
    if len(matches) != 1:
        return False
    boundary["p"] = matches[0]
    return True


async def _note_counts(session, uid, *, folder: str | None, path: str | None):
    """`not_scanned` over every note of the owner (the #350 count), and
    `failed` / `capped` over the notes the folder / path select."""
    from sqlalchemy import func, select
    from src.models.db import NoteMetadata
    from src.services.filters import folder_prefix_clause
    from src.services.tasks import TASKS_SINCE_EXTRACTION_VERSION

    owner = _tools()._note_owner_predicate(uid)
    not_scanned = (await session.execute(
        select(func.count()).select_from(NoteMetadata).where(
            owner, NoteMetadata.extraction_version < TASKS_SINCE_EXTRACTION_VERSION,
        )
    )).scalar_one()
    stmt = select(
        func.count().filter(NoteMetadata.tasks_failed.is_(True)),
        func.count().filter(NoteMetadata.tasks_truncated.is_(True)),
    ).select_from(NoteMetadata).where(owner)
    if folder:
        stmt = stmt.where(folder_prefix_clause(folder))
    if path is not None:
        stmt = stmt.where(NoteMetadata.file_path == path)
    failed, capped = (await session.execute(stmt)).one()
    return not_scanned, failed, capped


def _row_dict(r) -> dict:
    return dict(r._mapping)


# ── list_tasks ───────────────────────────────────────────────────────────────


async def list_tasks_query(
    uid: int | None,
    args: ListTasksArgs,
    settings,
    *,
    now: datetime.datetime | None = None,
    session_factory=None,
) -> TaskPage | QueryRefusal:
    """`list_tasks` (D12): validate, then read the page and every count in
    one `REPEATABLE READ, READ ONLY` snapshot. `settings` is the caller's
    `EffectiveTaskSettings` (`task_settings.effective`). Returns at most
    `limit` rows plus `more_rows`; the renderer decides how many are shown
    and mints the cursor from the last one."""
    from sqlalchemy import func, select
    from src.models.db import NoteMetadata, NoteTask

    now = snapshot_now(now)
    n = normalise_list_args(args, settings, now=now)
    if isinstance(n, QueryRefusal):
        return n
    owner = owner_discriminator(uid)
    filters = filters_digest(n, settings)
    boundary = None
    if args.cursor:
        boundary = decode_cursor(args.cursor, owner=owner, filters=filters, order_by=n.order_by)
        if isinstance(boundary, QueryRefusal):
            return boundary

    f = _Filters(n, settings)
    join_on = _join_on(uid)

    def count_stmt(conds):
        return select(func.count()).select_from(NoteTask).join(NoteMetadata, join_on).where(*conds)

    async with _session_factory(session_factory)() as session:
        await _begin_snapshot(session)
        if boundary is not None and "h" in boundary:
            if not await _resolve_hashed(session, uid, boundary):
                await session.rollback()
                return _refuse(CURSOR_GONE)
        not_scanned, failed, capped = await _note_counts(
            session, uid, folder=n.folder, path=n.path)
        from sqlalchemy import not_

        excluded = 0
        if f.location is not None:
            # `excluded by scope` (#434 D4): *other* ∧ ¬L, the rule not
            # consulted — under `all` this is #428's statement exactly.
            excluded = (await session.execute(
                count_stmt(f.all(scope=False) + [not_(f.location)])
            )).scalar_one()
        rule = None
        if f.rule_line:
            by_rule = unsure = 0
            if f.rule is not None:
                by_rule = (await session.execute(
                    count_stmt(f.other_and_location() + [not_(f.rule)])
                )).scalar_one()
            if f.unsure is not None:
                unsure = (await session.execute(
                    count_stmt(f.all() + [f.unsure])
                )).scalar_one()
            rule = rule_counts(settings, excluded=by_rule, unsure=unsure)
        if f.pending_ignores_rule:
            # Under `fields` the pending count ignores the rule (D2): it reads
            # columns a pending row does not have yet.
            pending_conds = f.other_and_location(text=False, recurring=False)
        else:
            pending_conds = f.all(text=False, recurring=False)
        ids_pending = (await session.execute(
            count_stmt(pending_conds + [NoteTask.task_key.is_(None)])
        )).scalar_one()
        if rule is not None and f.pending_ignores_rule:
            rule = dataclasses.replace(rule, pending=ids_pending)
        cut_tags = None
        if n.tags or n.exclude_tags:
            cut_tags = (await session.execute(
                count_stmt(f.all(tag=False) + [NoteTask.tags_cut.is_(True)])
            )).scalar_one()
        cut_text = None
        if n.terms:
            cut_text = (await session.execute(
                count_stmt(f.all(text=False) + [NoteTask.text_cut.is_(True)])
            )).scalar_one()
        page = _row_select().join(NoteMetadata, join_on).where(*f.all())
        if boundary is not None:
            page = page.where(keyset_clause(n.order_by, boundary))
        page = page.order_by(*order_columns(n.order_by)).limit(n.limit + 1)
        fetched = [_row_dict(r) for r in (await session.execute(page)).all()]
        await session.rollback()

    exclusions = Exclusions(
        scope=n.scope, folders=tuple(settings.excluded_folders),
        note_tags=tuple(settings.excluded_note_tags),
        excluded=excluded,
    )
    query_line = (
        uses_new_arguments(args)
        or n.any_relative
        or (n.scope == "default" and bool(settings.excluded_folders or settings.excluded_note_tags))
        # #434 D5: the rule line follows the query line, so it is shown too.
        or rule is not None
    )
    return TaskPage(
        args=n, rows=fetched[: n.limit], more_rows=len(fetched) > n.limit,
        not_scanned=not_scanned, failed=failed, capped=capped, ids_pending=ids_pending,
        cut_tags=cut_tags, cut_text=cut_text, exclusions=exclusions,
        query_line=query_line,
        cursor=CursorSpec(owner=owner, filters=filters, order_by=n.order_by),
        rule=rule,
    )


# ── task_agenda ──────────────────────────────────────────────────────────────


def agenda_continuation(key: str, d: datetime.date, days: int) -> tuple[list[tuple[str, Any]], dict]:
    """The D13 table: a section's `list_tasks` continuation arguments (dates
    resolved, in the order rendered) and the same as `ListTasksArgs` kwargs."""
    day = datetime.timedelta(days=1)
    end = d + datetime.timedelta(days=days)
    if key == "overdue":
        pairs = [("due_by", (d - day).isoformat()), ("order_by", "due")]
    elif key == "today":
        pairs = [("due_from", d.isoformat()), ("due_by", d.isoformat())]
    elif key == "scheduled_undated":
        pairs = [("scheduled_by", d.isoformat()), ("has_due", False),
                 ("order_by", "scheduled")]
    elif key == "scheduled_later":
        pairs = [("scheduled_by", d.isoformat()), ("due_from", (end + day).isoformat()),
                 ("order_by", "scheduled")]
    elif key == "upcoming":
        pairs = [("due_from", (d + day).isoformat()), ("due_by", end.isoformat())]
    else:
        raise ValueError(key)
    return pairs, dict(pairs)


def _agenda_predicate(key: str, d: datetime.date, days: int):
    """D13's predicates, disjoint by construction (every section is open)."""
    from sqlalchemy import and_
    from src.models.db import NoteTask

    day = datetime.timedelta(days=1)
    end = d + datetime.timedelta(days=days)
    if key == "overdue":
        return NoteTask.due <= d - day
    if key == "today":
        return NoteTask.due == d
    if key == "scheduled_undated":
        return and_(NoteTask.scheduled <= d, NoteTask.due.is_(None))
    if key == "scheduled_later":
        return and_(NoteTask.scheduled <= d, NoteTask.due >= end + day)
    return and_(NoteTask.due >= d + day, NoteTask.due <= end)


async def task_agenda_query(
    uid: int | None,
    args: AgendaArgs,
    settings,
    *,
    now: datetime.datetime | None = None,
    session_factory=None,
) -> AgendaPage | QueryRefusal:
    """`task_agenda` (D13): the five disjoint sections of open tasks, each
    with its total and at most its cap of rows, from one `REPEATABLE READ,
    READ ONLY` snapshot. Each section carries the exact `list_tasks`
    continuation and the cursor spec (owner, filter digest, order) of that
    `list_tasks` query, so a cursor minted from a shown row is accepted by
    `list_tasks_query` and continues exactly."""
    from sqlalchemy import func, not_, or_, select
    from src.models.db import NoteMetadata, NoteTask

    now = snapshot_now(now)
    zone, unavailable = _zone(settings)
    if not isinstance(args.date, str):
        return _refuse("task_agenda: date must be a string date.")
    rd = _resolve_date("date", args.date, zone, now, "task_agenda")
    if isinstance(rd, QueryRefusal):
        return rd
    days = args.days
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= AGENDA_MAX_DAYS:
        return _refuse(f"task_agenda: days must be an integer from 1 to {AGENDA_MAX_DAYS}.")
    try:
        # The section bounds reach D−1 and D+days+1; refuse a date at the
        # calendar's edge rather than fail while building them.
        rd.value + datetime.timedelta(days=days + 1)
        rd.value - datetime.timedelta(days=1)
    except OverflowError:
        return _refuse("task_agenda: the date window is out of range.")
    folder = _validate_folder(args.folder, "task_agenda")
    if isinstance(folder, QueryRefusal):
        return folder
    tags = _validate_tags("tags", args.tags, "task_agenda")
    if isinstance(tags, QueryRefusal):
        return tags
    exclude = _validate_tags("exclude_tags", args.exclude_tags, "task_agenda")
    if isinstance(exclude, QueryRefusal):
        return exclude
    scope = _validate_scope(args.scope, "task_agenda")
    if isinstance(scope, QueryRefusal):
        return scope

    d = rd.value
    owner = owner_discriminator(uid)
    # One Normalised per section, exactly as `list_tasks_query` would build it
    # from the continuation arguments, so `f` matches by construction.
    specs = []
    for key, cap, order in AGENDA_SECTIONS:
        pairs, kwargs = agenda_continuation(key, d, days)
        largs = ListTasksArgs(folder=folder, tags=args.tags if tags else None,
                              exclude_tags=args.exclude_tags if exclude else None,
                              scope=scope, **kwargs)
        n = normalise_list_args(largs, settings, now=now)
        if isinstance(n, QueryRefusal):  # pragma: no cover - the arguments are ours
            return n
        specs.append((key, cap, order, pairs, CursorSpec(owner, filters_digest(n, settings), order)))

    join_on = _join_on(uid)
    common = [NoteTask.state == "open"]
    if folder:
        from src.services.filters import folder_prefix_clause

        common.append(folder_prefix_clause(folder))
    tag_conds = []
    if tags:
        tag_conds.append(tags_all_clause(tags))
    if exclude:
        tag_conds.append(tags_none_clause(exclude))
    keep = scope_clause(settings) if scope == "default" else None
    location = location_clause(settings) if scope == "default" else None
    rule_keep = rule_clause(settings) if scope == "default" else None
    unsure_cond = task_rule.unsure_clause(settings) if scope == "default" else None
    show_rule = scope == "default" and task_rule.line_present(settings)
    pending_ignores_rule = scope == "default" and task_rule.pending_approximate(settings)
    union = or_(*[_agenda_predicate(key, d, days) for key, *_ in AGENDA_SECTIONS])
    scoped = common + tag_conds + ([keep] if keep is not None else [])
    located = common + tag_conds + ([location] if location is not None else [])

    def count_stmt(conds):
        return select(func.count()).select_from(NoteTask).join(NoteMetadata, join_on).where(*conds)

    factory = _session_factory(session_factory)
    async with factory() as session:
        await _begin_snapshot(session)
        not_scanned, failed, capped = await _note_counts(session, uid, folder=folder, path=None)
        excluded = 0
        if location is not None:
            excluded = (await session.execute(
                count_stmt(common + tag_conds + [union, not_(location)])
            )).scalar_one()
        rule = None
        if show_rule:
            by_rule = unsure = 0
            if rule_keep is not None:
                by_rule = (await session.execute(
                    count_stmt(located + [union, not_(rule_keep)])
                )).scalar_one()
            if unsure_cond is not None:
                unsure = (await session.execute(
                    count_stmt(scoped + [union, unsure_cond])
                )).scalar_one()
            rule = rule_counts(settings, excluded=by_rule, unsure=unsure)
        ids_pending = (await session.execute(
            count_stmt((located if pending_ignores_rule else scoped)
                       + [union, NoteTask.task_key.is_(None)])
        )).scalar_one()
        if rule is not None and pending_ignores_rule:
            rule = dataclasses.replace(rule, pending=ids_pending)
        cut_tags = None
        if tags or exclude:
            cut_tags = (await session.execute(
                count_stmt(common + ([keep] if keep is not None else [])
                           + [union, NoteTask.tags_cut.is_(True)])
            )).scalar_one()
        totals_stmt = select(*[
            func.count().filter(_agenda_predicate(key, d, days)) for key, *_ in AGENDA_SECTIONS
        ]).select_from(NoteTask).join(NoteMetadata, join_on).where(*scoped)
        totals = (await session.execute(totals_stmt)).one()
        sections = []
        for (key, cap, order, pairs, spec), total in zip(specs, totals):
            rows: list[dict] = []
            if total:
                stmt = (_row_select().join(NoteMetadata, join_on)
                        .where(*scoped, _agenda_predicate(key, d, days))
                        .order_by(*order_columns(order)).limit(cap))
                rows = [_row_dict(r) for r in (await session.execute(stmt)).all()]
            sections.append(AgendaSection(key=key, cap=cap, total=total, rows=rows,
                                          continuation=pairs, cursor=spec))
        await session.rollback()

    return AgendaPage(
        timezone=zone.name, timezone_unavailable=unavailable, date=d,
        date_given=rd.given, date_relative=rd.relative, days=days, folder=folder,
        tags=tags, exclude_tags=exclude,
        exclusions=Exclusions(scope=scope, folders=tuple(settings.excluded_folders),
                              note_tags=tuple(settings.excluded_note_tags), excluded=excluded),
        not_scanned=not_scanned, failed=failed, capped=capped, ids_pending=ids_pending,
        cut_tags=cut_tags, sections=sections, rule=rule,
    )
