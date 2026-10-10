"""Render a `usage_logs.params` document into a form PostgreSQL `jsonb` stores.

Issue #310. The values that reach `usage_logs.params` are a caller's own tool
arguments plus server telemetry, and SQLAlchemy hands the driver a plain
`json.dumps` of them (no `json_serializer` is set on the engine). Python's JSON
layer accepts three things `jsonb` refuses, each with a class-22 SQLSTATE:

* U+0000 in any string — `json.dumps` writes `"\\u0000"`, `jsonb` rejects it
  (22P05);
* an unpaired surrogate — `json.dumps` writes the escape, `jsonb` rejects it;
* a non-finite float — `json.dumps` writes the bare `NaN` / `Infinity`, which
  is not JSON (22P02).

All three arrive over the wire: the Streamable HTTP transport parses with
`json.loads`, which accepts every one of them. Unrendered, the audit row was
lost — so a NUL in any argument kept a call out of `usage_logs` — and a
coalesced refusal row was retried on every tick forever.

**The rule** (design D1). Rendering is decided per top-level key. A top-level
value is stored byte-for-byte unless it contains, at any depth, an unstorable
string (U+0000 or an unpaired surrogate, in a key or a value), a non-finite
float, a non-string mapping key, or a value JSON has no type for. Such a value
is *rendered*, and then **every** string inside it follows one escape grammar:

====================  =====================================================
input                 stored as
====================  =====================================================
``\\``                ``\\\\`` (two characters)
U+0000                ``\\x00`` (four characters)
unpaired surrogate    ``\\udXXX`` — lowercase hex, what ``backslashreplace``
                      emits
====================  =====================================================

Inside a rendered value every backslash begins exactly one of those three
sequences, so a NUL the caller sent (`\\x00`) cannot be confused with the
literal four characters `\\x00` (`\\\\x00`). `\\0` was rejected: it is
ambiguous against a following digit in the C and Python octal escapes.

A non-finite float becomes `.nan` / `.inf` / `-.inf` — the #154 tokens, so the
usage log spells a NaN the way `read_note` and the index do. A tuple, set or
frozenset becomes a list; any other non-JSON value its `str()`, escaped. A
non-string mapping key becomes its string form (a non-finite float key its
token), escaped, and **the first key wins** a post-render collision — the
indexer's `_jsonb_value` rule.

`params["rendered_params"]` lists, sorted, the top-level keys that were
rendered, and is absent when none was: a clean row's shape does not change.

The walk is iterative with no depth limit (the house rule — a depth limit is
"a hole with a number on it", `_first_unencodable_argument`), terminates on a
self-referential container (rendered as `<cycle>`), and **never raises**: a
top-level value that cannot be rendered is stored as `<unrenderable>` and
listed, and every other key — the server's outcome markers above all — is
kept.
"""

from __future__ import annotations

import math
import re

from src.services.vault import canonical_key, non_finite_token

#: The marker key. Reserved: no tool parameter, outcome marker or telemetry key
#: may use it (a test enumerates the registry to prove it).
RENDERED_PARAMS_KEY = "rendered_params"

#: Placeholder for a container that is already on the current path.
CYCLE = "<cycle>"
#: Placeholder for a top-level value the renderer could not render.
UNRENDERABLE = "<unrenderable>"

# What makes a string unstorable, and what the grammar escapes once a value is
# being rendered (the backslash too, which is what makes the grammar
# unambiguous).
_UNSTORABLE_CHAR = re.compile("[\x00\ud800-\udfff]")
_ESCAPED_CHAR = re.compile("[\\\\\x00\ud800-\udfff]")

_SCALARS = (str, int, float, bool, type(None))
_SEQUENCES = (list, tuple, set, frozenset)


def _escape_match(match: re.Match) -> str:
    ch = match.group(0)
    if ch == "\\":
        return "\\\\"
    if ch == "\x00":
        return "\\x00"
    return f"\\u{ord(ch):04x}"


def escape_string(value: str) -> str:
    """`value` in the escape grammar. Only called inside a rendered value."""
    return _ESCAPED_CHAR.sub(_escape_match, value)


def _string_unstorable(value: str) -> bool:
    return _UNSTORABLE_CHAR.search(value) is not None


def _needs_render(root) -> bool:
    """Does `root` hold anything `jsonb` (or `json.dumps`) would refuse?

    Iterative; a container met again while it is still on the current path is
    a cycle, which `json.dumps` refuses too, so it needs rendering. A container
    met again *off* the path (a shared reference) has already been checked.
    """
    on_path: set[int] = set()
    checked: set[int] = set()
    stack: list = [(False, root)]
    while stack:
        leaving, value = stack.pop()
        if leaving:
            on_path.discard(value)
            continue
        if isinstance(value, str):
            if _string_unstorable(value):
                return True
            continue
        if isinstance(value, bool) or value is None or isinstance(value, int):
            continue
        if isinstance(value, float):
            if not math.isfinite(value):
                return True
            continue
        if isinstance(value, dict):
            ident = id(value)
            if ident in on_path:
                return True
            if ident in checked:
                continue
            checked.add(ident)
            on_path.add(ident)
            stack.append((True, ident))
            for key, item in value.items():
                if not isinstance(key, str) or _string_unstorable(key):
                    return True
                stack.append((False, item))
            continue
        if isinstance(value, list):
            ident = id(value)
            if ident in on_path:
                return True
            if ident in checked:
                continue
            checked.add(ident)
            on_path.add(ident)
            stack.append((True, ident))
            stack.extend((False, item) for item in value)
            continue
        # A tuple, a set, or anything else JSON has no type for.
        return True
    return False


def _render_key(key) -> str:
    return escape_string(canonical_key(key))


def _render_scalar(value):
    """A non-container value, rendered. May raise (a hostile `__str__`)."""
    if isinstance(value, str):
        return escape_string(value)
    if isinstance(value, bool) or value is None or isinstance(value, int):
        return value
    if isinstance(value, float):
        token = non_finite_token(value)
        return value if token is None else token
    return escape_string(str(value))


def _render_value(root):
    """`root` rendered in full, iteratively. May raise; the caller catches."""
    holder: list = [None]
    on_path: set[int] = set()
    # ("visit", value, container, slot) | ("leave", ident)
    stack: list = [("visit", root, holder, 0)]
    while stack:
        frame = stack.pop()
        if frame[0] == "leave":
            on_path.discard(frame[1])
            continue
        _, value, container, slot = frame
        if isinstance(value, (dict, *_SEQUENCES)):
            ident = id(value)
            if ident in on_path:
                container[slot] = CYCLE
                continue
            on_path.add(ident)
            stack.append(("leave", ident))
            if isinstance(value, dict):
                out: dict = {}
                container[slot] = out
                children = []
                for key, item in value.items():
                    rendered_key = _render_key(key)
                    if rendered_key in out:
                        continue  # first key wins, stated rather than inherited
                    out[rendered_key] = None
                    children.append(("visit", item, out, rendered_key))
            else:
                items = list(value)
                out_list: list = [None] * len(items)
                container[slot] = out_list
                children = [
                    ("visit", item, out_list, index)
                    for index, item in enumerate(items)
                ]
            stack.extend(reversed(children))
            continue
        container[slot] = _render_scalar(value)
    return holder[0]


def render_usage_params(params):
    """`params` in a form `jsonb` stores; see the module docstring.

    Never raises. A non-dict input (not produced by any writer today) is
    rendered as a whole or replaced by `<unrenderable>`.
    """
    if not isinstance(params, dict):
        try:
            return _render_value(params) if _needs_render(params) else params
        except Exception:  # noqa: BLE001 - the audit row must still land
            return UNRENDERABLE
    out: dict = {}
    rendered: set[str] = set()
    for key, value in params.items():
        if key == RENDERED_PARAMS_KEY:
            continue  # recomputed below; never trusted from the input
        try:
            if isinstance(key, str) and not _string_unstorable(key):
                out_key, key_rendered = key, False
            else:
                out_key, key_rendered = _render_key(key), True
        except Exception:  # noqa: BLE001
            out_key, key_rendered = UNRENDERABLE, True
        if out_key in out:
            continue  # first key wins
        try:
            if key_rendered or _needs_render(value):
                out[out_key] = _render_value(value)
                rendered.add(out_key)
            else:
                out[out_key] = value
        except Exception:  # noqa: BLE001 - a hostile __str__, say
            out[out_key] = UNRENDERABLE
            rendered.add(out_key)
    if rendered:
        out[RENDERED_PARAMS_KEY] = sorted(rendered)
    return out
