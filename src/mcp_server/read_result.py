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
from typing import Any

from pydantic import BaseModel, PrivateAttr, model_serializer
from src.services.tool_outcomes import BodyOutcome

from src.services.vault import is_encodable, non_finite_token, outline_sections

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
OMITTED_NOT_REPRESENTABLE = "not_json_representable"
OMITTED_DUPLICATE_KEY = "duplicate_json_key"
#: The same loss, but *caused by* this server's own non-finite coercion
#: (`.nan: 1` beside `".nan": 2`) rather than by two keys YAML itself renders
#: alike (`1:` beside `"1":`). A caller can act on the difference — the first
#: is a property of the note, the second is a property of the note *plus* this
#: rendering rule — so it is a distinct code rather than the same one (#154,
#: design D10, L14).
OMITTED_COERCED_DUPLICATE_KEY = "duplicate_json_key_after_coercion"
OMITTED_UNPAIRED_SURROGATE = "unpaired_surrogate"
OMITTED_TOO_DEEP = "excessive_depth"


class MetadataOmission(_OmitNone):
    """One metadata field this response could not carry, and why.

    Server-authored, every field of it. This is the *only* channel that reports
    a dropped field: nothing is ever signalled by writing a marker into the
    note-controlled field itself.
    """

    field: str
    reason: str
    detail: str


# ── metadata coercions ──────────────────────────────────────────────────────
#
# An omission and a coercion are different facts and must not share a list.
# `metadata_omissions` names a field this response **dropped whole**; a value
# the server rendered in a form the note does not literally contain is a
# different statement about a field that is still there. Reporting the second
# through the first would make "the entry names a field you did not receive"
# false, and a caller cannot act on a list whose entries mean two things.

#: The one reason code for a non-finite YAML number rendered as its canonical
#: token (#154). The literal is the contract — the spec, this module and the
#: tests use this same string.
COERCED_NON_FINITE_FLOAT = "non_finite_float"


class MetadataCoercion(_OmitNone):
    """One metadata value this response rendered in a canonicalized form.

    Server-authored, every field of it, exactly as `MetadataOmission` is: the
    channel is out of band precisely so that nothing has to be signalled inside
    the note-controlled field itself.
    """

    field: str
    reason: str
    detail: str


def non_finite_coercion(field: str) -> MetadataCoercion:
    """The one wording for "a non-finite number was rendered as a YAML token".

    `NaN`, `Infinity` and `-Infinity` are not JSON, and Python's `nan` / `inf`
    spelling is not a form any note contains, so the view renders YAML's own
    `.nan` / `.inf` / `-.inf`. The coercion is not reversible from the view —
    `x: .nan` and `x: ".nan"` render identically, and `.NaN` renders as `.nan`
    — so the entry points at the block's own text.
    """
    return MetadataCoercion(
        field=field,
        reason=COERCED_NON_FINITE_FLOAT,
        detail=(
            f"The note's {field} carries a non-finite number, rendered here as "
            "its canonical YAML token (`.nan`, `.inf`, `-.inf`) because JSON "
            "has no form for it. The value itself is unchanged in the note. "
            "Read `frontmatter_yaml`, or the note's raw text with `read_file`, "
            "to see the spelling the note uses."
        ),
    )


_UNRENDERABLE_DETAIL = {
    OMITTED_UNPAIRED_SURROGATE: (
        "contains an unpaired surrogate code point, which is not valid UTF-8 "
        "and cannot be serialized into this response"
    ),
    OMITTED_NOT_REPRESENTABLE: (
        "holds a value the server cannot render as text at all (an integer far "
        "past the digit limit CPython will convert, for instance)"
    ),
    OMITTED_TOO_DEEP: (
        "nests deeper than this server can render, so the subtree below that "
        "point is not carried"
    ),
}


def unrenderable_omission(field: str, reason: str) -> MetadataOmission:
    """The one wording for "this value refuses to become JSON-safe text".

    Two failure modes, both reachable from a note a user can save, both of them
    *valid YAML* — see `vault.coerce_text` for the shapes. Either one would
    otherwise escape as an exception from a code path that promises in-band
    errors, so the value is dropped at model-build time and reported here.

    The raw block is unaffected in both cases: whatever the value decodes to,
    in the file it is ordinary text, so `frontmatter_yaml` carries it losslessly
    and remains the authoritative copy.
    """
    return _omission(
        field,
        reason,
        f"The note's {field} {_UNRENDERABLE_DETAIL[reason]}.",
    )


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
    """Two YAML keys would land on one JSON key.

    `coerced` says whether this server's non-finite coercion is what made them
    collide, which is a different fact from `1:` beside `"1":` and gets its own
    reason code.
    """

    def __init__(self, key: str, coerced: bool = False) -> None:
        super().__init__(key)
        self.key = key
        self.coerced = coerced


class _ViewNotEncodable(Exception):
    """A string in the block is not UTF-8-encodable (an unpaired surrogate)."""


def _view_key(key: Any) -> str:
    """A mapping key as one JSON string. Non-finite floats take YAML's token.

    A YAML mapping may be *keyed* by a number, non-finite ones included
    (`.nan: 1`), and today's `str(key)` renders Python's `nan` in a place the
    note spells `.nan`. Same helper as the leaf below and as the indexer, so
    the view, the index and the block cannot disagree (#154).
    """
    token = non_finite_token(key)
    if token is not None:
        return token
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
        # `NaN` / `Infinity` / `-Infinity` are not JSON. This branch has always
        # coerced them; what changes with #154 is *what it coerces to* and that
        # it says so: YAML's own `.nan` / `.inf` / `-.inf` rather than Python's
        # `nan` / `inf`, which is a spelling no note contains, and a
        # `metadata_coercions` entry so the caller can tell this from a note
        # whose value really is the string `"nan"`.
        token = non_finite_token(value)
        if token is None:
            return value
        budget["coerced"] = True
        return _view_str(token, budget)
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
    # Checked HERE, at model-build time, and not in a serializer: the SDK
    # renders the text block from the returned object and `structuredContent`
    # from the validated dump, so a value only one of them can encode is a
    # desynchronized response at best and a protocol error at worst.
    if not is_encodable(text):
        raise _ViewNotEncodable(text)
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
        # Which rendered keys exist only because a non-finite float was
        # coerced. A collision involving one of them is caused by this
        # rendering rule, not by the note alone, and is reported as such.
        coerced_keys: set[str] = set()
        for key, value in node.items():
            key_token = non_finite_token(key)
            rendered = _view_key(key)
            if rendered in out:
                raise _ViewKeyCollision(
                    rendered, coerced=key_token is not None or rendered in coerced_keys
                )
            if key_token is not None:
                coerced_keys.add(rendered)
                budget["coerced"] = True
            _view_str(rendered, budget)
            out[rendered] = _view_walk(value, depth + 1, inner, budget)
        return out

    if isinstance(node, (list, tuple, set, frozenset)):
        if id(node) in path:
            raise _ViewUnrepresentable("frontmatter contains a recursive alias")
        inner = path | {id(node)}
        return [_view_walk(item, depth + 1, inner, budget) for item in node]

    return _view_leaf(node, budget)


def frontmatter_view(
    frontmatter: dict,
) -> tuple[dict | None, MetadataOmission | None, list[MetadataCoercion]]:
    """Best-effort JSON view of a parsed frontmatter mapping.

    Returns `(view, omission, coercions)`. `view` is None when the block has
    nothing to show (no block, or an empty mapping) — that is not an omission —
    and also when construction failed, in which case `omission` says why.
    `coercions` names values the view rendered in a canonical form the note
    does not literally contain; it is empty for the overwhelming majority of
    notes and non-empty only alongside a `view` that was actually built, since
    a coercion is a statement about a field the caller *received*. Never
    raises: a note must not be able to make a read fail.
    """
    if not frontmatter:
        return None, None, []
    budget = {"nodes": _VIEW_MAX_NODES, "chars": _VIEW_MAX_CHARS, "coerced": False}
    try:
        view = _view_walk(frontmatter, 0, frozenset(), budget)
    except _ViewNotEncodable:
        return None, unrenderable_omission("frontmatter", OMITTED_UNPAIRED_SURROGATE), []
    except _ViewKeyCollision as exc:
        # A coercion-induced collision keeps #149's omit-whole-view rule rather
        # than the index's first-key-wins: first-wins here would emit a partial
        # view, and a caller cannot tell a pruned mapping from a complete one.
        # The reason code says which of the two collisions this was (D10, L14).
        return None, _omission(
            "frontmatter",
            OMITTED_COERCED_DUPLICATE_KEY if exc.coerced else OMITTED_DUPLICATE_KEY,
            (
                f"Two frontmatter keys both render as the JSON key {exc.key!r} once a "
                "non-finite number is written as its canonical YAML token, so the "
                "JSON view would silently lose one of them; frontmatter_yaml carries "
                "both, with the spellings the note uses."
                if exc.coerced
                else f"Two frontmatter keys both render as the JSON key {exc.key!r}, so "
                "the JSON view would silently lose one of them; frontmatter_yaml "
                "carries both."
            ),
        ), []
    except _ViewUnrepresentable as exc:
        return None, _omission(
            "frontmatter",
            OMITTED_NOT_REPRESENTABLE,
            f"The frontmatter has no bounded JSON form ({exc}); frontmatter_yaml "
            "carries it verbatim.",
        ), []
    except Exception:
        # Reached, not hypothetical: `str()` on a hex integer past CPython's
        # digit limit raises from inside the walk. `debug`, not `exception` —
        # this is a note the server is handling correctly, not an incident, and
        # a stack trace per read of such a note is noise in the log.
        logger.debug("frontmatter JSON view construction failed", exc_info=True)
        return None, _omission(
            "frontmatter",
            OMITTED_NOT_REPRESENTABLE,
            "The frontmatter could not be rendered as JSON; frontmatter_yaml "
            "carries it verbatim.",
        ), []
    return view, None, ([non_finite_coercion("frontmatter")] if budget["coerced"] else [])


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

    # Fill against an estimate: the empty truncated form plus each entry's own
    # serialization and its separating comma.
    #
    # It does NOT stop at the first entry that does not fit. "At least one
    # entry SHALL be emitted whenever one fits" is a claim about *any* entry,
    # not about the first, and stopping made it false for the commonest real
    # shape there is: a note whose opening section has a very long heading. One
    # 300-character title ahead of fifty short ones produced an outline with
    # zero entries and a bare omission count, which is the least useful answer
    # available while a perfectly good listing was affordable. Skipping keeps
    # scanning, so the omission count and the ordinal range still describe the
    # whole note and every ordinal an entry carries is still its own.
    # The most the `omitted` count can shrink as entries are kept: it starts at
    # `total` and bottoms out at one digit.
    slack = len(str(total)) - 1
    kept: list[OutlineEntry] = []
    used = _serialized_len(_truncated([]))
    for entry in entries:
        # Two steps, and the order matters. The cheap step is a *lower* bound —
        # never rejects an entry that could have fitted — so it only skips work.
        # The decision itself measures the candidate outline, because no
        # per-entry arithmetic gets it right: charging a separating comma
        # over-counts by exactly one for the first entry (there is nothing to
        # separate it from), which rejected an entry that fitted the cap
        # precisely, and the omission count's own digit width shrinks as entries
        # are kept. Without the cheap filter this is quadratic on a note with
        # ten thousand headings.
        entry_len = _serialized_len(entry)
        if used + entry_len + (1 if kept else 0) - slack > cap:
            continue
        candidate = _truncated(kept + [entry])
        candidate_len = _serialized_len(candidate)
        if candidate_len <= cap:
            kept.append(entry)
            used = candidate_len

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

    _body_outcome: BodyOutcome | None = PrivateAttr(default=None)

    # Always exact, never elided, never marked: two paths that differ only by a
    # character a lossy rendering would collapse must stay distinguishable.
    # Bounded instead at admission, by `vault.MAX_PATH_CHARS`.
    path: str | None = None

    # The whole file's digest, `sha256:<64 lowercase hex>`, from the same read
    # that built this response (`vault.content_hash_for_bytes`). Server-
    # controlled and of fixed width (`vault.CONTENT_HASH_CHARS`), so it is a
    # **fixed allocation beside `path`** in the worst-case arithmetic and does
    # not enter the metadata budget: it is never dropped and never appears in
    # `metadata_omissions`. Dropping it would silently disable the caller's
    # only write precondition on exactly the notes large enough to be worth
    # guarding.
    #
    # It is the whole file's in every mode — a section read's and a truncated
    # read's alike — so the token means one thing wherever it came from, and it
    # is **not** a hash of `content`: a note with frontmatter, or with CRLF
    # terminators, has a digest the returned text cannot reproduce.
    content_hash: str | None = None

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
    # Retained-but-altered values, never drops. Sibling to the list above and
    # never a substitute for it: each list is used only for its own case, and
    # one response may carry both.
    metadata_coercions: list[MetadataCoercion] | None = None
    notice: str | None = None
    error: str | None = None


# ── the metadata budget ─────────────────────────────────────────────────────


def _tag_cost(tags: list[str]) -> int:
    # Two characters of JSON per tag for the quotes, one for the comma.
    return sum(len(tag) + 3 for tag in tags)


def screen_unrenderable(
    result: "ReadNoteResult", lossy: dict[str, str]
) -> list[MetadataOmission]:
    """Turn the parse's loss record into this response's omissions.

    This function no longer *screens* anything: `_scrub_frontmatter` removed
    every unrenderable value at the parse, so by the time a mapping reaches
    here it is safe, and so are the `title` and `tags` derived from it. What is
    left is the assembly job — deciding what a dropped key costs *this
    response*, which is a question only the response shape can answer:

    * `title` and `tags` are dropped whole, because `read_file`'s fallback
      would otherwise hand the agent the filename as a title that the note does
      not carry, and a tag list quietly missing an entry;
    * the `frontmatter` JSON view is dropped for **any** loss anywhere in the
      block, including one nested three levels down under an unrelated key,
      because a pruned mapping is indistinguishable from the real one and this
      tool does not emit a partial view. `frontmatter_yaml` is untouched and
      remains the authoritative copy of what was actually in the note.

    `heading`, `content`, `frontmatter_yaml` and the outline's titles are slices
    of `Path.read_text(encoding="utf-8")`, which is strict, so they cannot be
    anything but renderable text. `path` and the section selector are refused
    at admission, where a bad one is an error rather than an omission.
    """
    omissions: list[MetadataOmission] = []
    for field in ("title", "tags"):
        reason = lossy.get(field)
        if reason is None:
            continue
        setattr(result, field, None)
        omissions.append(unrenderable_omission(field, reason))
    if lossy:
        # Unconditionally, not `if result.frontmatter is not None`: when the
        # dropped key was the block's *only* key the view is already empty, and
        # silently showing nothing is exactly the pruned-mapping answer this
        # tool does not give. The caller is told the view is unavailable.
        result.frontmatter = None
        omissions.append(unrenderable_omission("frontmatter", _worst_reason(lossy)))
    # Belt and braces on the two fields that are not pure slices of the note's
    # decoded bytes: `title` falls back to the filename, which the OS can hand
    # back through surrogate-escape. Nothing should reach here that the parse
    # did not already catch, but the cost of being wrong is a protocol error and
    # the cost of the check is two `encode` calls.
    if result.title is not None and not is_encodable(result.title):
        result.title = None
        omissions.append(unrenderable_omission("title", OMITTED_UNPAIRED_SURROGATE))
    if result.tags is not None and not all(is_encodable(tag) for tag in result.tags):
        result.tags = None
        omissions.append(unrenderable_omission("tags", OMITTED_UNPAIRED_SURROGATE))
    return omissions


def _worst_reason(lossy: dict[str, str]) -> str:
    """One reason for the view's omission when several keys were dropped.

    An unpaired surrogate is the more alarming of the two — it is the one that
    would have produced a protocol error — so it wins when both are present.
    """
    reasons = set(lossy.values())
    if OMITTED_UNPAIRED_SURROGATE in reasons:
        return OMITTED_UNPAIRED_SURROGATE
    if OMITTED_NOT_REPRESENTABLE in reasons:
        return OMITTED_NOT_REPRESENTABLE
    return OMITTED_TOO_DEEP


def apply_metadata_budget(
    result: ReadNoteResult,
    carried: list[MetadataOmission],
    budget: int,
    coercions: list[MetadataCoercion] | None = None,
) -> None:
    """Bring `result`'s metadata fields inside `budget`, recording every drop.

    `read_note` reaches the disk through `vault.read_file()`, which has no byte
    limit, so a note can carry a multi-megabyte frontmatter block above a
    one-character body. Without this the response would be governed by the
    content cap and still be megabytes wide.

    Drop order is fixed and stated in the spec: the lossy JSON view first (the
    raw block says everything it does), then `frontmatter_yaml`, then `tags`,
    then `heading`, then `title`.

    **Every drop is a whole field.** Nothing is truncated in place, cut short,
    or replaced by a marker — not even the two short fields where an in-place
    cut looks harmless. A shortened value inside a note-controlled field is
    indistinguishable from note content, which is the forgery class this whole
    change exists to end, and a `heading` cut to `""` would additionally
    collide with the convention that `""` is an *answer* (an empty section
    body) rather than an absence. An earlier revision elided `heading` and
    `title`; it also computed the heading's room from the *un-dropped* title,
    so an oversized title silently zeroed the heading. Both are gone.

    `carried` holds omissions decided before the budget ran — the JSON view's
    own construction failures, and the unencodable-value screen — so the list
    the caller sees is in decision order.

    `coercions` holds values rendered in a canonical form (#154). They are
    filtered here rather than set by the caller because the budget decides
    which fields survive, and a coercion is a statement about a field the
    response **still carries**: an entry naming a field this function has just
    dropped whole would contradict the omission beside it.

    **`path` and `content_hash` are not in the drop order and not in the
    cost.** Both are server-controlled and of fixed width, and both are
    accounted for as fixed allocations in the worst-case arithmetic instead.
    Dropping the hash under pressure would disable the caller's write
    precondition on precisely the notes big enough to need one — a budget is
    not a reason to hand back a response that silently cannot be written back.
    """
    omissions = list(carried)

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

    def _drop(field: str, detail: str) -> None:
        setattr(result, field, None)
        omissions.append(_omission(field, OMITTED_BUDGET, detail))

    if _cost() > budget and result.frontmatter is not None:
        _drop(
            "frontmatter",
            "The frontmatter JSON view did not fit this response's metadata budget.",
        )

    if _cost() > budget and result.frontmatter_yaml:
        _drop(
            "frontmatter_yaml",
            "The frontmatter block did not fit this response's metadata budget "
            "and is omitted whole rather than truncated, because half a YAML "
            "block still parses.",
        )

    if _cost() > budget and result.tags:
        dropped = len(result.tags)
        _drop(
            "tags",
            f"{dropped:,} tag(s) did not fit this response's metadata budget.",
        )

    if _cost() > budget and result.heading:
        _drop(
            "heading",
            "The heading line did not fit this response's metadata budget. It is "
            "omitted whole rather than cut, so nothing here is a prefix "
            "masquerading as a full heading; `content` is still the section's "
            "body and is unaffected.",
        )

    if _cost() > budget and result.title:
        _drop(
            "title",
            "The title did not fit this response's metadata budget and is "
            "omitted whole rather than cut.",
        )

    if omissions:
        result.metadata_omissions = omissions

    kept = [c for c in (coercions or []) if getattr(result, c.field, None) is not None]
    if kept:
        result.metadata_coercions = kept
