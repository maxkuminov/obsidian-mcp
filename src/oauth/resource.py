"""RFC 8707 `resource` classification for /authorize and /token (#357, D11).

Log-only in this change, by owner decision: every request is classified into
one low-cardinality `resource_class` and **nothing is refused**. The class
rides every security record the request emits; a terminal path that emits no
required outcome event gets one classification-only `oauth_resource_classified`
record from the handler's `finally`. The 14-day gate for a later
`invalid_target` change is defined on exactly this evidence.

`resource_host` is recorded only for `unexpected`, and only as a host — a
`resource` value is caller text and its path or query can carry anything.
"""
from __future__ import annotations

import logging
from urllib.parse import urlsplit

from src.config import settings
from src.oauth.trust import to_ascii_host
from src.services import security_events

ABSENT = "absent"
UNEXPECTED = "unexpected"
UNCLASSIFIED_RATE_LIMITED = "unclassified_rate_limited"
UNCLASSIFIED_MALFORMED = "unclassified_malformed"
UNPARSEABLE_HOST = "<unparseable>"

CLASSES = frozenset({
    ABSENT, "accepted_1", "accepted_2", "accepted_3", "accepted_4",
    UNEXPECTED, UNCLASSIFIED_RATE_LIMITED, UNCLASSIFIED_MALFORMED,
})

_STATE_CLASS = "oauth_resource_class"
_STATE_HOST = "oauth_resource_host"
_STATE_ATTACHED = "oauth_resource_attached"


def accepted_spellings() -> tuple[str, str, str, str]:
    base = (settings.base_url or "").rstrip("/")
    return (f"{base}/mcp", f"{base}/mcp/", base, f"{base}/")


def _host_of(value) -> str:
    try:
        host = to_ascii_host(urlsplit(value).hostname) if isinstance(value, str) else None
    except Exception:  # noqa: BLE001 - classification may not raise
        host = None
    return host or UNPARSEABLE_HOST


def classify(values) -> tuple[str, str | None]:
    """(`resource_class`, `resource_host` or None) for the values present."""
    values = [v for v in (values or [])]
    if not values:
        return ABSENT, None
    spellings = accepted_spellings()
    first = values[0]
    if isinstance(first, str) and first in spellings and all(v == first for v in values):
        return f"accepted_{spellings.index(first) + 1}", None
    unexpected = next(
        (v for v in values if not (isinstance(v, str) and v in spellings)), first
    )
    return UNEXPECTED, _host_of(unexpected)


def _state(request):
    try:
        return request.state
    except Exception:  # noqa: BLE001 - handlers are called with stand-ins
        return None


def begin(request, values) -> None:
    """Classify once at handler entry and remember it on `request.state`."""
    state = _state(request)
    if state is None:
        return
    cls, host = classify(values)
    setattr(state, _STATE_CLASS, cls)
    setattr(state, _STATE_HOST, host)
    setattr(state, _STATE_ATTACHED, False)


def set_fixed(request, cls: str) -> None:
    state = _state(request)
    if state is None:
        return
    setattr(state, _STATE_CLASS, cls)
    setattr(state, _STATE_HOST, None)
    setattr(state, _STATE_ATTACHED, False)


def resource_class(request) -> str | None:
    """The request's class, marking it attached to the record being built."""
    state = _state(request)
    cls = getattr(state, _STATE_CLASS, None) if state is not None else None
    if cls is not None:
        setattr(state, _STATE_ATTACHED, True)
    return cls


def resource_host(request) -> str | None:
    state = _state(request)
    return getattr(state, _STATE_HOST, None) if state is not None else None


def finish(request) -> None:
    """Emit `oauth_resource_classified` iff no record carried the class."""
    state = _state(request)
    if state is None or getattr(state, _STATE_CLASS, None) is None:
        return
    if getattr(state, _STATE_ATTACHED, False):
        return
    setattr(state, _STATE_ATTACHED, True)
    try:
        route = request.url.path
    except Exception:  # noqa: BLE001
        route = None
    security_events.emit(
        "oauth_resource_classified",
        level=logging.INFO,
        subject=security_events.subject_for(request=request),
        resource_class=getattr(state, _STATE_CLASS),
        resource_host=getattr(state, _STATE_HOST, None),
        route=route,
    )
