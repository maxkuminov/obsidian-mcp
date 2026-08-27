"""`read_note`'s structured result, and the budgets that keep it bounded (#149).

Why this module exists, in one paragraph. `read_note` used to render one
string: a `# <title>` / `**Path:**` / `**Tags:**` / `**Frontmatter:**` envelope,
a `"\\n---\\n"` separator, then the selected content. Every component of that
envelope is note-controlled, so a *valid* note could forge the separator — two
reproductions, on two different fields, are recorded in
`docs/architecture/vault-tools.md` — and any textual procedure an agent used to
recover the section body from the response was therefore forgeable into a
destructive `edit_note(section=…)`. Sanitising the fields one at a time
demonstrably did not close the class. Fields do: there is no frame to forge
when the frame is the protocol's.

Three consequences run through everything below.

1. **Every value is JSON-safe when the model is built, never in a serializer.**
   The MCP SDK (1.29 FastMCP) renders the text block from the *returned object*
   (`pydantic_core.to_json`) and `structuredContent` from the *validated dump*
   (`model_dump(mode="json")`). Coercing a value inside a serializer would let
   the two renderings disagree; `tests/test_issue_149_read_note_framing.py`
   pins them equal.
2. **Absent means absent.** Optional pydantic fields serialize as `null` by
   default, and a `null` in `content` is not the same statement as no `content`
   at all. `_OmitNone` drops `None` from both renderings.
3. **Every note-controlled field has a budget, and overflow drops the field
   whole.** A dropped field is *never* replaced by an in-band marker: a marker
   inside a note-controlled field is indistinguishable from note content, which
   is the exact forgery class this module exists to end. Omissions are reported
   out of band, in the server-controlled `metadata_omissions` list.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import math
from typing import Any

from pydantic import BaseModel, model_serializer

from src.services.vault import outline_sections

logger = logging.getLogger(__name__)


# ── the "absent, not null" base ─────────────────────────────────────────────


class _OmitNone(BaseModel):
    """A model whose `None` fields vanish from both renderings.

    `mode="wrap"` runs the standard serializer first and filters its output, so
    nested models, JSON-mode coercion and `by_alias` all keep working. The
    filter is on `None` only: `""`, `[]`, `{}` and `False` are answers, and
    callers must be able to tell "the section body is empty" from "there is no
    section body in this response".
    """

    @model_serializer(mode="wrap")
    def _drop_none(self, handler) -> dict[str, Any]:  # noqa: ANN001
        return {key: value for key, value in handler(self).items() if value is not None}


# ── metadata omissions ──────────────────────────────────────────────────────

# Reason codes. They are part of the tool's contract, so they are stable
# strings rather than an enum whose rename would be invisible here.
OMITTED_BUDGET = "metadata_budget"
OMITTED_BUDGET_ELIDED = "metadata_budget_elided"
OMITTED_NOT_REPRESENTABLE = "not_json_representable"
OMITTED_DUPLICATE_KEY = "duplicate_json_key"


class MetadataOmission(_OmitNone):
    """One metadata field this response could not carry, and why.

    Server-authored, every field of it. This is the *only* channel that reports
    a dropped field: nothing is ever signalled by writing a marker into the
    note-controlled field itself.
    """

    field: str
    reason: str
    detail: str


def _omission(field: str, reason: str, detail: str) -> MetadataOmission:
    return MetadataOmission(
        field=field,
        reason=reason,
        # Every omission says how to get the value anyway, and the answer is
        # always the same: the note's own bytes.
        detail=f"{detail} Read the note's raw text with `read_file` to see it in full.",
    )


# ── the frontmatter JSON view ───────────────────────────────────────────────
#
# `frontmatter_yaml` is authoritative and lossless. This view is a convenience,
# and it is built defensively because a *valid* YAML block has shapes JSON
# simply does not have:
#
#   * recursive aliases — `x: &X [*X]` loads into a self-referential list that
#     crashes (or hangs) a naive walk;
#   * non-string keys — `1:` and `"1":` are two distinct YAML keys that collapse
#     onto one JSON key, silently losing one of them;
#   * dates and timestamps — no JSON form at all.
#
# So: bounded depth, bounded node count, bounded total string length, cycle
# detection along the current path, and — for the two lossy shapes we cannot
# represent honestly — omit the whole view and say so. Never a partial view: a
# caller cannot tell a pruned map from the real one.

_VIEW_MAX_DEPTH = 12
_VIEW_MAX_NODES = 5_000
_VIEW_MAX_CHARS = 100_000

# Ints outside this range are rendered as strings. JSON has no integer width,
# but consumers do, and a YAML `10**5000` is a denial-of-service dressed as a
# number.
_VIEW_INT_BOUND = 2 ** 63


class _ViewUnrepresentable(Exception):
    """Depth, size, node count, or a cycle — the view cannot be built."""


class _ViewKeyCollision(Exception):
    """Two YAML keys would land on one JSON key."""


def _view_key(key: Any) -> str:
    if isinstance(key, str):
        return key
    if isinstance(key, (_dt.datetime, _dt.date, _dt.time)):
        return key.isoformat()
    return str(key)


def _view_leaf(value: Any, budget: dict) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value if -_VIEW_INT_BOUND < value < _VIEW_INT_BOUND else _view_str(str(value), budget)
    if isinstance(value, float):
        return value if math.isfinite(value) else _view_str(str(value), budget)
    if isinstance(value, str):
        return _view_str(value, budget)
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return _view_str(value.isoformat(), budget)
    if isinstance(value, bytes):
        return _view_str(value.decode("utf-8", "replace"), budget)
    return _view_str(str(value), budget)


def _view_str(text: str, budget: dict) -> str:
    budget["chars"] -= len(text)
    if budget["chars"] < 0:
        raise _ViewUnrepresentable("frontmatter view exceeded its character budget")
    return text


def _view_walk(node: Any, depth: int, path: frozenset[int], budget: dict) -> Any:
    budget["nodes"] -= 1
    if budget["nodes"] < 0:
        raise _ViewUnrepresentable("frontmatter view exceeded its node budget")
    if depth > _VIEW_MAX_DEPTH:
        raise _ViewUnrepresentable("frontmatter nests deeper than the view allows")

    if isinstance(node, dict):
        if id(node) in path:
            raise _ViewUnrepresentable("frontmatter contains a recursive alias")
        inner = path | {id(node)}
        out: dict[str, Any] = {}
        for key, value in node.items():
            rendered = _view_key(key)
            if rendered in out:
                raise _ViewKeyCollision(rendered)
            _view_str(rendered, budget)
            out[rendered] = _view_walk(value, depth + 1, inner, budget)
        return out

    if isinstance(node, (list, tuple, set, frozenset)):
        if id(node) in path:
            raise _ViewUnrepresentable("frontmatter contains a recursive alias")
        inner = path | {id(node)}
        return [_view_walk(item, depth + 1, inner, budget) for item in node]

    return _view_leaf(node, budget)


def frontmatter_view(frontmatter: dict) -> tuple[dict | None, MetadataOmission | None]:
    """Best-effort JSON view of a parsed frontmatter mapping.

    Returns `(view, omission)`. `view` is None when the block has nothing to
    show (no block, or an empty mapping) — that is not an omission — and also
    when construction failed, in which case `omission` says why. Never raises:
    a note must not be able to make a read fail.
    """
    if not frontmatter:
        return None, None
    budget = {"nodes": _VIEW_MAX_NODES, "chars": _VIEW_MAX_CHARS}
    try:
        return _view_walk(frontmatter, 0, frozenset(), budget), None
    except _ViewKeyCollision as exc:
        return None, _omission(
            "frontmatter",
            OMITTED_DUPLICATE_KEY,
            f"Two frontmatter keys both render as the JSON key {str(exc)!r}, so the "
            "JSON view would silently lose one of them; frontmatter_yaml carries "
            "both.",
        )
    except _ViewUnrepresentable as exc:
        return None, _omission(
            "frontmatter",
            OMITTED_NOT_REPRESENTABLE,
            f"The frontmatter has no bounded JSON form ({exc}); frontmatter_yaml "
            "carries it verbatim.",
        )
    except Exception:  # pragma: no cover — defence in depth, not a known path
        logger.exception("frontmatter JSON view construction failed")
        return None, _omission(
            "frontmatter",
            OMITTED_NOT_REPRESENTABLE,
            "The frontmatter could not be rendered as JSON; frontmatter_yaml "
            "carries it verbatim.",
        )


def _view_cost(view: dict) -> int:
    """How many characters the JSON view will spend in the response."""
    try:
        return len(json.dumps(view, ensure_ascii=False, separators=(",", ":")))
    except (TypeError, ValueError):  # pragma: no cover — the walk guarantees JSON-safe
        return _VIEW_MAX_CHARS


# ── the outline ─────────────────────────────────────────────────────────────

_MAX_OUTLINE_TITLE = 80


class OutlineEntry(_OmitNone):
    """One section of a truncated whole-note read's heading outline."""

    ordinal: int
    depth: int
    text: str
    size: int
    exceeds_cap: bool
    duplicate: bool


class NoteOutline(_OmitNone):
    """The outline, with its degraded states as data rather than as prose.

    `truncated` is the explicit marker the requirement asks for: when the
    budget cannot hold even one entry, `entries` is empty and `truncated` is
    true, which is a statement, not a silence. `omitted`, `first_ordinal` and
    `last_ordinal` are present exactly when the listing is incomplete — a
    complete listing carries no omission summary, so nothing has to be reserved
    for one.
    """

    entries: list[OutlineEntry] = []
    truncated: bool = False
    omitted: int | None = None
    first_ordinal: int | None = None
    last_ordinal: int | None = None


def _outline_entry(section: dict, cap: int, duplicate: bool) -> OutlineEntry:
    title = section["text"]
    if len(title) > _MAX_OUTLINE_TITLE:
        title = title[:_MAX_OUTLINE_TITLE - 1] + "…"
    return OutlineEntry(
        ordinal=section["ordinal"],
        depth=section["depth"],
        text=title,
        # `size` and `exceeds_cap` keep the pre-#149 measure: the heading line
        # plus its body, i.e. what `extract_section` spans. That is
        # conservative in the safe direction now that a section read returns
        # the body alone — a section whose heading-plus-body fits the cap
        # always has a body that fits.
        size=section["size"],
        exceeds_cap=section["size"] > cap,
        # A repeated title can only be addressed by its ordinal; the path-style
        # form cannot separate duplicate siblings.
        duplicate=duplicate,
    )


def _serialized_len(model: BaseModel) -> int:
    return len(model.model_dump_json())


def build_outline(content: str, cap: int) -> NoteOutline | None:
    """The heading outline for a truncated whole-note read, or None.

    None means "this note has no ATX headings", or "not even the bare
    truncation marker fits the budget" — the outline never exceeds `cap`, and
    the cap is the binding constraint: there is no output it may exceed the cap
    to produce. It accompanies a response that exists *because* the content was
    too large, so an unbounded outline recreates the failure being prevented (a
    1,000-heading note once produced an outline 213× the cap).

    The budget is spent in the same layered way the rendered outline spent it,
    now measured over the serialized object:

    1. Overlong titles are elided first, so one heading cannot eat the listing.
    2. If the complete listing fits, emit it — and reserve nothing for an
       omission summary that is not needed. Charging that reservation
       unconditionally used to drop entries that had room.
    3. Otherwise fill greedily with room reserved for the summary fields, which
       are themselves serialized text and must be paid for before being spent.
    4. Verify against the real serialization and drop entries until it fits,
       because a greedy estimate is an estimate.
    """
    sections = outline_sections(content)
    if not sections:
        return None

    counts: dict[str, int] = {}
    for section in sections:
        counts[section["text"]] = counts.get(section["text"], 0) + 1
    entries = [
        _outline_entry(section, cap, counts[section["text"]] > 1)
        for section in sections
    ]

    complete = NoteOutline(entries=entries)
    if _serialized_len(complete) <= cap:
        return complete

    total = len(sections)

    def _truncated(kept: list[OutlineEntry]) -> NoteOutline:
        return NoteOutline(
            entries=kept,
            truncated=True,
            omitted=total - len(kept),
            first_ordinal=1,
            last_ordinal=total,
        )

    # Greedy fill against an estimate: the empty truncated form plus each
    # entry's own serialization and its separating comma.
    overhead = _serialized_len(_truncated([]))
    used = overhead
    kept: list[OutlineEntry] = []
    for entry in entries:
        cost = _serialized_len(entry) + 1
        if used + cost > cap:
            break
        kept.append(entry)
        used += cost

    outline = _truncated(kept)
    while kept and _serialized_len(outline) > cap:
        kept.pop()
        outline = _truncated(kept)
    if _serialized_len(outline) > cap:
        # Degenerate budget: not even the marker fits. Emitting nothing is the
        # only answer that respects the cap, and the cap wins.
        return None
    return outline


# ── the result ──────────────────────────────────────────────────────────────


class ReadNoteResult(_OmitNone):
    """What `read_note` returns.

    Every field is either server-controlled or a note-controlled value sitting
    alone in a field of its own. Nothing here is composed into a frame, so
    nothing note-controlled can change which field another value appears in —
    that is the whole point (#149).

    On an error result the content-bearing fields are absent: `error` is the
    answer, and a caller must never find a half-response beside it.
    """

    # Always exact, never elided, never marked: two paths that differ only by a
    # character a lossy rendering would collapse must stay distinguishable.
    # Bounded instead at admission, by `vault.MAX_PATH_CHARS`.
    path: str | None = None

    title: str | None = None
    tags: list[str] | None = None

    # The block's YAML source as stored, fence lines excluded. Authoritative
    # and lossless whenever present — under budget pressure it is dropped
    # whole, never truncated.
    frontmatter_yaml: str | None = None
    # Best-effort JSON view of the same block. Convenience only: mutate
    # frontmatter through `set_frontmatter`, or through the raw block, never by
    # round-tripping this.
    frontmatter: dict[str, Any] | None = None

    # Section reads: the matched heading line, without its terminator.
    heading: str | None = None
    # Whole-note reads: the body with a valid frontmatter block stripped.
    # Section reads: the body ONLY — exactly the span `edit_note(section=…)`
    # replaces, so it is byte-exact input for that write.
    content: str | None = None

    truncated: bool | None = None
    offset: int | None = None
    next_offset: int | None = None
    total_chars: int | None = None

    outline: NoteOutline | None = None
    metadata_omissions: list[MetadataOmission] | None = None
    notice: str | None = None
    error: str | None = None


# ── the metadata budget ─────────────────────────────────────────────────────


def _tag_cost(tags: list[str]) -> int:
    # Two characters of JSON per tag for the quotes, one for the comma.
    return sum(len(tag) + 3 for tag in tags)


def apply_metadata_budget(
    result: ReadNoteResult,
    view_omission: MetadataOmission | None,
    budget: int,
) -> None:
    """Bring `result`'s metadata fields inside `budget`, recording every drop.

    `read_note` reaches the disk through `vault.read_file()`, which has no byte
    limit, so a note can carry a multi-megabyte frontmatter block above a
    one-character body. Without this the response would be governed by the
    content cap and still be megabytes wide.

    Drop order is fixed and stated in the spec: the lossy JSON view first (the
    raw block says everything it does), then `frontmatter_yaml`, then `tags`,
    then the heading is elided, then the title. Fields are dropped **whole** —
    a truncated `frontmatter_yaml` is a *corrupt* YAML block that looks valid,
    which is worse than no block at all. Elision, where it happens, is a plain
    cut with **no** marker character: a marker inside a note-controlled field
    is indistinguishable from note content.
    """
    omissions: list[MetadataOmission] = []
    if view_omission is not None:
        omissions.append(view_omission)

    def _cost() -> int:
        total = 0
        if result.title:
            total += len(result.title)
        if result.tags:
            total += _tag_cost(result.tags)
        if result.frontmatter_yaml:
            total += len(result.frontmatter_yaml)
        if result.frontmatter:
            total += _view_cost(result.frontmatter)
        if result.heading:
            total += len(result.heading)
        return total

    if _cost() > budget and result.frontmatter is not None:
        result.frontmatter = None
        omissions.append(_omission(
            "frontmatter",
            OMITTED_BUDGET,
            "The frontmatter JSON view did not fit this response's metadata budget.",
        ))

    if _cost() > budget and result.frontmatter_yaml:
        result.frontmatter_yaml = None
        omissions.append(_omission(
            "frontmatter_yaml",
            OMITTED_BUDGET,
            "The frontmatter block did not fit this response's metadata budget "
            "and is omitted whole rather than truncated, because half a YAML "
            "block still parses.",
        ))

    if _cost() > budget and result.tags:
        dropped = len(result.tags)
        result.tags = None
        omissions.append(_omission(
            "tags",
            OMITTED_BUDGET,
            f"{dropped:,} tag(s) did not fit this response's metadata budget.",
        ))

    if _cost() > budget and result.heading:
        room = max(0, budget - (len(result.title) if result.title else 0))
        result.heading = result.heading[:room]
        omissions.append(_omission(
            "heading",
            OMITTED_BUDGET_ELIDED,
            f"The heading line was cut to {room:,} character(s) to fit this "
            "response's metadata budget; no marker is added, so what is here is "
            "a prefix of the real heading.",
        ))

    if _cost() > budget and result.title:
        room = max(0, budget - (len(result.heading) if result.heading else 0))
        result.title = result.title[:room]
        omissions.append(_omission(
            "title",
            OMITTED_BUDGET_ELIDED,
            f"The title was cut to {room:,} character(s) to fit this response's "
            "metadata budget; no marker is added, so what is here is a prefix of "
            "the real title.",
        ))

    if omissions:
        result.metadata_omissions = omissions
