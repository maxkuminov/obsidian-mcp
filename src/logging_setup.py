"""The one place the root logger is configured, and the one field allow-list.

## Why this exists at all

`src/main.py` imports `src.mcp_server.server`, whose module-level
`FastMCP(...)` calls the SDK's own `configure_logging()`, which runs
`logging.basicConfig(handlers=[RichHandler(...)], format="%(message)s")` on the
**root** logger at import time. `basicConfig` is a no-op once the root has a
handler, so the `logging.basicConfig(...)` that used to sit in `src/main.py`
never applied: every record in this server was rendered by Rich, in local time,
wrapped at console width, with `extra=` dropped on the floor. Ten
`auth_failure` records a second all looked like `WARNING auth_failure
auth.py:143` and said nothing about which credential, which reason or which
address (#190).

So the configuration has to run **after** that import and has to be forceful.

## Why not `logging.basicConfig(force=True, handlers=[...])`

`force=True` removes **and closes** every handler already on the root — which
includes `src/services/error_log.py`'s ring buffer, the only thing behind the
panel's "recent errors" health page. Today that happens to be safe because the
SDK's `configure_logging()` runs at import time and `error_log.attach()` runs in
the lifespan, so the buffer is attached *after* the reset. "Happens to" is not a
guarantee: the lifespan is re-entered in tests, and a future call ordering would
silently kill the health page with no test to notice.

`configure_logging()` therefore does the same job with the exception written
down: it walks the root's handlers and removes and closes every one **except**
the instance `error_log.installed_handler()` returns. Either call order is safe,
`error_log.attach()` stays idempotent, and "the health page keeps working" is a
property of this code rather than of an import order.

## The field policy, in three disjoint name spaces

`extra=` is attacker-influenced — one of the existing `auth_failure` sites
derives its value from a *presented bearer token* — so the formatter never
serialises `record.__dict__`. It emits an allow-list, and drops everything else:

* **`FORMATTER_OWNED`** — `ts`, `level`, `logger`, `msg`, `stack`. Produced here
  from the `LogRecord` itself. A call site that passes one of these has it
  dropped, so a caller cannot forge a timestamp, a level or a traceback.
* **`EMITTER_CONTROL`** — `permit`, `event`, `subject`, `level`, `exc_info`.
  Consumed by `src/services/security_events.py`'s `acquire`/`emit` and never
  rendered as data. `level` is deliberately in both sets: it is a control
  keyword that the formatter renders from the record, and never a field.
* **`ALLOWED_FIELDS`** — everything a call site may pass, each with a declared
  type and, for strings, a maximum length.

**A value that does not match its declared type is dropped — never converted.**
`user_id="not-an-int"` yields a record with *no* `user_id`, not one whose
`user_id` is a string: a reader who has to ask "is this integer field an integer
today?" cannot query the field at all, and a silent type change is how a Loki
dashboard starts lying. Truncating an over-long string is not a conversion and
stays.

`key_prefix` is deliberately **absent** from the allow-list: the name invites
logging a raw `omcp_` prefix of a presented token, and a dropped field is a
safer failure than a shipped credential. Nothing free-text is allow-listed
except `reason`, which is a closed vocabulary per event. There is no field a
path, a query string or a request body could ride in.

Messages and tracebacks are governed by a different rule (they may carry
operational context such as a vault-relative path, and must not carry credential
material) — see `docs/architecture/security-event-logging.md`.
"""
from __future__ import annotations

import datetime
import json
import logging
import sys
import traceback
from dataclasses import dataclass

# ── Bounds ──────────────────────────────────────────────────────────────────

#: The rendered message is bounded too. It is developer-authored, but it
#: interpolates operational context (vault-relative paths, exception text) and a
#: log line that no ingester will accept is a log line that does not exist.
MAX_MESSAGE_CHARS = 2000

#: A traceback is elided **head and tail**, never merely truncated: the first
#: frames say where the call came from and the last say what actually failed.
STACK_HEAD_BYTES = 4 * 1024
STACK_TAIL_BYTES = 3 * 1024

#: The exception's type line and the traceback's final line are guaranteed
#: present (see `_bound_stack`); each is itself bounded so that a megabyte-long
#: exception message cannot unbound the record through the guarantee.
STACK_GUARANTEED_LINE_BYTES = 1024


@dataclass(frozen=True)
class _FieldSpec:
    """One allow-listed field: its Python type and, for strings, its bound."""

    kind: type
    max_len: int | None = None


_S = _FieldSpec


#: Names the formatter produces from the `LogRecord`. A call site may not pass
#: one; if it does, the formatter's own value wins and the call site's is
#: dropped (and `security_events` raises under its strict flag).
FORMATTER_OWNED: frozenset[str] = frozenset({"ts", "level", "logger", "msg", "stack"})

#: Keywords `security_events.acquire`/`emit` consume. Never rendered as data.
#: `level` is in `FORMATTER_OWNED` too, deliberately: it is a control keyword
#: the formatter renders from the record, and is never a field.
EMITTER_CONTROL: frozenset[str] = frozenset(
    {"permit", "event", "subject", "level", "exc_info"}
)

#: Every field a call site may pass, with its declared type and string bound.
#: `error_type` is the one dual name: the formatter derives it whenever
#: `exc_info` is set, and a call site may pass it otherwise — **when both are
#: present the exception wins**, because the class of the exception being logged
#: is a fact and the passed value is a claim.
ALLOWED_FIELDS: dict[str, _FieldSpec] = {
    "reason": _S(str, 64),
    "outcome": _S(str, 16),
    "route": _S(str, 200),
    "method": _S(str, 8),
    "status": _S(int),
    "tool": _S(str, 100),
    "user_id": _S(int),
    "actor_user_id": _S(int),
    "username": _S(str, 64),
    "username_submitted": _S(str, 64),
    "username_session": _S(str, 64),
    "user_id_session": _S(int),
    "actor_username": _S(str, 64),
    "actor_kind": _S(str, 20),
    "actor_ref": _S(str, 64),
    "key_id": _S(int),
    "oauth_token_id": _S(int),
    "client_id": _S(str, 64),
    "client_id_submitted": _S(str, 64),
    "client_name_submitted": _S(str, 120),
    "grant_id": _S(str, 64),
    "scope": _S(str, 64),
    "token_tag": _S(str, 16),
    "client_ip": _S(str, 45),
    "error_type": _S(str, 100),
    "limit": _S(int),
    "limit_count": _S(int),
    "count": _S(int),
    "day": _S(str, 10),
    "window_seconds": _S(int),
    "duration_ms": _S(int),
    "cleared_user_id": _S(bool),
}

#: Sentinel for "this value did not survive its type check".
_DROP = object()


def coerce_field(name: str, value: object) -> object:
    """The declared value for `name`, or `_DROP`.

    Bools are not integers here even though Python says they are: an `int` field
    holding `True` is exactly the kind of silent type change the drop rule
    exists to prevent.
    """
    spec = ALLOWED_FIELDS.get(name)
    if spec is None:
        return _DROP
    if spec.kind is bool:
        return value if isinstance(value, bool) else _DROP
    if spec.kind is int:
        if isinstance(value, bool) or not isinstance(value, int):
            return _DROP
        return value
    if spec.kind is str:
        if not isinstance(value, str):
            return _DROP
        return value[: spec.max_len] if spec.max_len is not None else value
    return _DROP  # pragma: no cover - no other kind is declared


def _utc_ts(created: float) -> str:
    """ISO-8601 UTC with milliseconds and an explicit `Z`."""
    moment = datetime.datetime.fromtimestamp(created, tz=datetime.timezone.utc)
    return f"{moment.strftime('%Y-%m-%dT%H:%M:%S')}.{moment.microsecond // 1000:03d}Z"


def _bound_line(line: str) -> str:
    encoded = line.encode("utf-8", "replace")
    if len(encoded) <= STACK_GUARANTEED_LINE_BYTES:
        return line
    kept = encoded[:STACK_GUARANTEED_LINE_BYTES].decode("utf-8", "ignore")
    return f"{kept}… [{len(encoded) - STACK_GUARANTEED_LINE_BYTES} bytes elided]"


def _type_line(exc_type: type | None, text: str) -> str:
    """The line of `text` that names the exception, or a synthesised one.

    Normally this *is* the traceback's final line; with `__cause__` chaining or
    exception notes it is not, which is why it is looked up rather than assumed.
    """
    name = getattr(exc_type, "__name__", "") or ""
    if not name:
        return ""
    qualified = f"{getattr(exc_type, '__module__', '')}.{name}"
    candidates = [
        line
        for line in text.splitlines()
        if line.startswith(f"{name}:")
        or line == name
        or line.startswith(f"{qualified}:")
        or line == qualified
    ]
    return candidates[-1] if candidates else name


def _bound_stack(text: str, exc_type: type | None) -> str:
    """Head + tail with an elision marker, the identifying lines guaranteed.

    "The whole traceback, bounded" is a contradiction, so the rule is explicit:
    the first `STACK_HEAD_BYTES` and the last `STACK_TAIL_BYTES` survive with a
    marker naming the dropped byte count between them, and if the elision would
    have removed the exception's type line or the traceback's final line they
    are appended (each bounded by `STACK_GUARANTEED_LINE_BYTES`) — those are the
    two lines that identify a fault. A traceback under the budget is untouched.
    """
    encoded = text.encode("utf-8", "replace")
    if len(encoded) <= STACK_HEAD_BYTES + STACK_TAIL_BYTES:
        return text
    head = encoded[:STACK_HEAD_BYTES].decode("utf-8", "ignore")
    tail = encoded[-STACK_TAIL_BYTES:].decode("utf-8", "ignore")
    dropped = len(encoded) - STACK_HEAD_BYTES - STACK_TAIL_BYTES
    out = f"{head}\n... [{dropped} bytes elided] ...\n{tail}"
    lines = [line for line in text.splitlines() if line.strip()]
    final_line = lines[-1] if lines else ""
    for guaranteed in (_type_line(exc_type, text), final_line):
        if guaranteed and guaranteed not in out:
            out = f"{out}\n{_bound_line(guaranteed)}"
    return out


def build_payload(record: logging.LogRecord) -> dict:
    """The allow-listed object for one record. Never raises."""
    payload: dict[str, object] = {
        "ts": _utc_ts(record.created),
        "level": record.levelname,
        "logger": record.name,
    }
    try:
        message = record.getMessage()
    except Exception:  # noqa: BLE001 - a logging call may not fail on its own args
        message = str(record.msg)
    payload["msg"] = message[:MAX_MESSAGE_CHARS]

    exc_type = None
    if record.exc_info:
        exc_type = record.exc_info[0]
        try:
            formatted = "".join(traceback.format_exception(*record.exc_info))
        except Exception:  # noqa: BLE001
            formatted = ""
        if formatted:
            payload["stack"] = _bound_stack(formatted, exc_type)

    for name in ALLOWED_FIELDS:
        if name not in record.__dict__:
            continue
        value = coerce_field(name, record.__dict__[name])
        if value is _DROP:
            continue
        payload[name] = value

    # The exception wins over any passed `error_type` (see `ALLOWED_FIELDS`).
    if exc_type is not None:
        payload["error_type"] = coerce_field("error_type", exc_type.__name__)

    return payload


def _render_text(payload: dict) -> str:
    """The same object as `ts level logger msg k=v …`, on one line."""
    head = " ".join(
        str(payload.get(key, "")) for key in ("ts", "level", "logger")
    )
    parts = [head, _one_line(str(payload.get("msg", "")))]
    for key, value in payload.items():
        if key in ("ts", "level", "logger", "msg"):
            continue
        parts.append(f"{key}={_one_line(str(value))}")
    return " ".join(parts)


def _one_line(text: str) -> str:
    return text.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r")


class StructuredFormatter(logging.Formatter):
    """Renders one record as exactly one physical line.

    `json.dumps` escapes newlines inside strings, so a traceback rides in the
    `stack` field without breaking Alloy's per-line Docker ingestion. The `text`
    rendering escapes them by hand for the same reason.

    A formatting failure degrades to the record's message. This runs inside
    every `logger.*` call in the process, including the ones in exception
    handlers, so it may not raise.
    """

    def __init__(self, fmt: str = "json") -> None:
        super().__init__()
        self.fmt_name = "text" if fmt == "text" else "json"

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        try:
            payload = build_payload(record)
            if self.fmt_name == "text":
                return _render_text(payload)
            return json.dumps(payload, default=str)
        except Exception:  # noqa: BLE001 - a formatter may not raise
            try:
                return _one_line(str(record.msg))
            except Exception:  # noqa: BLE001
                return "<unrenderable log record>"


#: Marks the handler this module installs, so a second `configure_logging()`
#: can recognise its own predecessor as well as anybody else's.
_INSTALLED_ATTR = "_omcp_structured_handler"


def installed_stream_handler() -> logging.Handler | None:
    """The structured handler currently on the root logger, if any."""
    for handler in logging.getLogger().handlers:
        if getattr(handler, _INSTALLED_ATTR, False):
            return handler
    return None


def configure_logging(
    level: int | str | None = None, fmt: str | None = None
) -> logging.Handler | None:
    """Take the root logger back from the SDK. Idempotent; never raises.

    Called from every process entry point — `src/main.py` immediately after the
    import block (so it runs after `FastMCP` has had its way) and
    `src/mcp_stdio.py`, where stderr matters because stdout is the MCP protocol
    channel.

    Returns the installed handler, or `None` if configuration failed — a server
    that cannot configure its logging still has to start.
    """
    try:
        from src.config import settings
        from src.services import error_log

        resolved_level = level if level is not None else settings.log_level
        resolved_fmt = fmt if fmt is not None else settings.log_format

        root = logging.getLogger()
        exempt = error_log.installed_handler()
        for handler in list(root.handlers):
            if handler is exempt:
                # The health page's ring buffer. Never removed, never closed,
                # in either call order — that is the whole reason this is not
                # `basicConfig(force=True)`.
                continue
            root.removeHandler(handler)
            try:
                handler.close()
            except Exception:  # noqa: BLE001 - a broken handler must not stop us
                pass

        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(StructuredFormatter(resolved_fmt))
        setattr(handler, _INSTALLED_ATTR, True)
        root.addHandler(handler)
        root.setLevel(resolved_level)

        # Re-applied here rather than left where `basicConfig` used to be: these
        # two are chatty at INFO and the floors have to survive a reconfiguration.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        return handler
    except Exception:  # noqa: BLE001 - logging setup may not fail a process
        return None
