"""#190 — the field allow-list, its three name spaces, and the AST sweep.

`extra=` is attacker-influenced: one of the ten `auth_failure` sites derives its
value from a *presented bearer token*. A formatter that serialised
`record.__dict__` would ship key prefixes, vault paths and query strings into a
shared sink the moment somebody added a field, so the policy is an allow-list
with declared types and bounds — and a sweep that fails the build when a call
site invents a name.
"""
import ast
import json
import logging
import pathlib
import re

import pytest
from starlette.requests import Request

from src.logging_setup import (
    ALLOWED_FIELDS,
    EMITTER_CONTROL,
    FORMATTER_OWNED,
    StructuredFormatter,
)
from src.services import security_events

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
CATALOGUE_DOC = ROOT / "docs" / "architecture" / "security-event-logging.md"


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture
def captured():
    """Records emitted by `security_events`, with the suppressor out of the way."""
    handler = _Capture()
    logger = security_events.logger
    logger.addHandler(handler)
    propagate = logger.propagate
    level = logger.level
    logger.propagate = False
    # INFO is part of the contract, and the root's default WARNING would filter
    # it before any handler saw it.
    logger.setLevel(logging.DEBUG)
    security_events.reset_state()
    try:
        with security_events.suppression_disabled():
            yield handler.records
    finally:
        logger.removeHandler(handler)
        logger.propagate = propagate
        logger.setLevel(level)
        security_events.reset_state()


def _payload(record):
    return json.loads(StructuredFormatter().format(record))


# ── Dropping ────────────────────────────────────────────────────────────────


def test_an_unknown_field_is_dropped_and_the_rest_survives(captured):
    security_events.emit(
        "auth_failure", subject="ip:198.51.100.7", reason="invalid_key", vault_path="/x"
    )
    payload = _payload(captured[0])
    assert "vault_path" not in payload
    assert payload["reason"] == "invalid_key"
    assert payload["msg"] == "auth_failure"


def test_a_field_the_event_does_not_declare_is_dropped(captured):
    # `grant_id` is allow-listed, but `auth_failure` does not declare it.
    security_events.emit(
        "auth_failure", subject="-", reason="invalid_key", grant_id="g-1"
    )
    assert "grant_id" not in _payload(captured[0])


def test_a_field_the_event_does_not_declare_raises_under_strict(captured):
    with security_events.strict_fields():
        with pytest.raises(security_events.SecurityEventFieldError):
            security_events.emit(
                "auth_failure", subject="-", reason="invalid_key", grant_id="g-1"
            )


def test_an_unregistered_event_raises_under_strict(captured):
    with security_events.strict_fields():
        with pytest.raises(security_events.SecurityEventFieldError):
            security_events.emit("not_in_the_catalogue", subject="-", reason="x")


def test_a_mistyped_value_is_dropped_and_never_converted(captured):
    security_events.emit(
        "auth_failure", subject="-", reason="invalid_key", key_id="not-an-int"
    )
    payload = _payload(captured[0])
    assert "key_id" not in payload
    assert payload["reason"] == "invalid_key"


def test_a_bool_is_not_an_int(captured):
    """`isinstance(True, int)` is True in Python; an `int` field holding `True`
    is exactly the silent type change the drop rule exists to prevent."""
    record = logging.LogRecord("probe", logging.WARNING, __file__, 1, "e", None, None)
    record.user_id = True
    assert "user_id" not in _payload(record)


def test_the_formatter_does_not_raise_on_a_mistyped_value():
    record = logging.LogRecord("probe", logging.WARNING, __file__, 1, "e", None, None)
    record.duration_ms = object()
    assert _payload(record)["msg"] == "e"


def test_a_none_value_is_absent_rather_than_null(captured):
    security_events.emit("auth_failure", subject="-", reason="invalid_key", key_id=None)
    assert "key_id" not in _payload(captured[0])


def test_strings_are_truncated_to_their_declared_bound():
    record = logging.LogRecord("probe", logging.WARNING, __file__, 1, "e", None, None)
    record.reason = "r" * 500
    record.route = "/" + "p" * 500
    payload = _payload(record)
    assert len(payload["reason"]) == ALLOWED_FIELDS["reason"].max_len
    assert len(payload["route"]) == ALLOWED_FIELDS["route"].max_len


def test_key_prefix_is_not_allow_listed():
    """Deliberate: the name invites logging a raw `omcp_` prefix of a presented
    token, and a dropped field is a safer failure than a shipped credential."""
    assert "key_prefix" not in ALLOWED_FIELDS


# ── The redaction ───────────────────────────────────────────────────────────


def test_the_token_tag_is_stable_and_shaped():
    tag = security_events.redacted_token_tag("omcp_" + "a" * 40)
    assert tag == security_events.redacted_token_tag("omcp_" + "a" * 40)
    assert re.fullmatch(r"sha:[0-9a-f]{8}", tag)


def test_no_record_contains_a_long_substring_of_the_token(captured):
    token = "omcp_" + "Zq7" * 14
    security_events.emit(
        "auth_failure",
        subject="-",
        reason="invalid_key",
        token_tag=security_events.redacted_token_tag(token),
    )
    line = StructuredFormatter().format(captured[0])
    for start in range(len(token) - 12 + 1):
        assert token[start : start + 12] not in line


def test_no_tag_is_invented_when_nothing_was_presented(captured):
    assert security_events.redacted_token_tag(None) is None
    assert security_events.redacted_token_tag("") is None
    security_events.emit(
        "auth_failure",
        subject="-",
        reason="invalid_key",
        token_tag=security_events.redacted_token_tag(None),
    )
    assert "token_tag" not in _payload(captured[0])


def test_the_auth_middleware_uses_the_one_definition():
    from src.mcp_server import auth

    assert auth._redacted_prefix("abc") == security_events.redacted_token_tag("abc")


# ── The client identity ─────────────────────────────────────────────────────


def _request(peer: str, headers: list[tuple[bytes, bytes]] | None = None) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/mcp",
            "headers": headers or [],
            "client": (peer, 4444),
            "scheme": "http",
            "server": ("testserver", 80),
            "query_string": b"",
        }
    )


def test_client_ip_ignores_a_forged_forwarded_header():
    """`ProxyHeadersMiddleware` rewrites `scope["client"]` only for peers inside
    the trusted ranges. Reading the header here would accept a forgery from
    anyone, which is the precise thing that middleware exists to prevent."""
    request = _request("203.0.113.9", [(b"x-forwarded-for", b"10.0.0.1")])
    assert security_events.client_ip(request) == "203.0.113.9"


def test_client_ip_is_none_without_a_peer():
    scope = dict(_request("203.0.113.9").scope)
    scope["client"] = None
    assert security_events.client_ip(Request(scope)) is None


def test_the_subject_prefers_a_resolved_user_over_the_address():
    request = _request("203.0.113.9")
    assert security_events.subject_for(user_id=7, request=request) == "user:7"
    assert security_events.subject_for(request=request) == "ip:203.0.113.9"
    assert security_events.subject_for() == "-"


# ── The three name spaces ───────────────────────────────────────────────────


def test_the_field_names_are_disjoint_from_the_names_the_call_site_may_not_pass():
    assert ALLOWED_FIELDS.keys() & FORMATTER_OWNED == set()
    assert ALLOWED_FIELDS.keys() & EMITTER_CONTROL == set()
    # `level` is deliberately in both of the other two: it is a control keyword
    # the formatter renders from the record, and never a field (design D18).
    assert FORMATTER_OWNED & EMITTER_CONTROL == {"level"}


def test_a_formatter_owned_name_cannot_be_forged_by_a_call_site():
    record = logging.LogRecord("probe", logging.WARNING, __file__, 1, "real", None, None)
    record.ts = "1999-01-01T00:00:00.000Z"
    record.logger = "someone-elses-logger"
    record.stack = "not a traceback"
    payload = _payload(record)
    assert payload["ts"] != "1999-01-01T00:00:00.000Z"
    assert payload["logger"] == "probe"
    assert payload["msg"] == "real"
    assert "stack" not in payload


def test_passing_a_formatter_owned_name_raises_under_strict(captured):
    with security_events.strict_fields():
        with pytest.raises(security_events.SecurityEventFieldError):
            security_events.emit("auth_failure", subject="-", msg="forged")


def test_the_exception_wins_over_a_passed_error_type():
    try:
        raise ValueError("nope")
    except ValueError:
        import sys

        record = logging.LogRecord(
            "probe", logging.ERROR, __file__, 1, "e", None, sys.exc_info()
        )
    record.error_type = "AttackerType"
    assert _payload(record)["error_type"] == "ValueError"


def test_a_passed_error_type_survives_without_exc_info():
    record = logging.LogRecord("probe", logging.WARNING, __file__, 1, "e", None, None)
    record.error_type = "OperationalError"
    assert _payload(record)["error_type"] == "OperationalError"


# ── The registry ────────────────────────────────────────────────────────────


def test_every_event_declares_only_allow_listed_fields():
    for event, fields in security_events.EVENT_FIELDS.items():
        unknown = set(fields) - ALLOWED_FIELDS.keys()
        assert not unknown, f"{event} declares {sorted(unknown)}"


def test_no_event_declares_a_formatter_owned_or_control_name():
    for event, fields in security_events.EVENT_FIELDS.items():
        assert not set(fields) & FORMATTER_OWNED, event
        assert not set(fields) & (EMITTER_CONTROL - {"level"}), event


def test_every_catalogue_row_in_the_architecture_note_has_an_entry():
    """The note's table is the registry's prose half; they may not drift."""
    text = CATALOGUE_DOC.read_text()
    documented = set(re.findall(r"^\| `([a-z_]+)` \|", text, flags=re.M))
    assert documented, "the catalogue table was not found in the architecture note"
    missing = documented - security_events.EVENT_FIELDS.keys()
    assert not missing, f"documented but unregistered: {sorted(missing)}"
    undocumented = security_events.EVENT_FIELDS.keys() - documented
    assert not undocumented, f"registered but undocumented: {sorted(undocumented)}"


# ── The AST sweep (design D14) ──────────────────────────────────────────────
#
# A regex over `extra={...}` would miss `security_events.emit(...)` keywords
# entirely and would break on any reformatting, so the sweep parses the tree.
#
# `src/services/security_events.py` is the one exempt module: it is the emitter,
# its single `logger.log(..., extra=fields)` passes a dict this module has
# already policed at runtime, and the rest of this file is the check on it.

_SWEEP_EXEMPT = {SRC / "services" / "security_events.py"}
_LOG_METHODS = {"debug", "info", "warning", "error", "exception", "critical", "log"}

# One dated, per-name gap. The refresh-reuse alarm (#182, archived after this
# change was proposed) logs `event`, `error` and `revoked_tokens` through
# `extra=` from `src/oauth/routes.py`; none of the three is allow-listed —
# `event` duplicates `msg`, and `error` is the bare-string field the allow-list
# deliberately does not have. `src/oauth/routes.py` belongs to Slice B (#191),
# which is where those two call sites move onto `security_events.emit`; this
# entry is what makes the gap visible instead of silent, and Slice B deletes it.
_KNOWN_GAPS: dict[str, set[str]] = {
    "src/oauth/routes.py": {"event", "error", "revoked_tokens"},
}


def _is_logger_call(node: ast.Call) -> bool:
    func = node.func
    if not isinstance(func, ast.Attribute) or func.attr not in _LOG_METHODS:
        return False
    base = func.value
    name = base.id if isinstance(base, ast.Name) else getattr(base, "attr", "")
    return "log" in name.lower()


def _is_emit_call(node: ast.Call) -> bool:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr == "emit" and isinstance(func.value, ast.Name) and (
            func.value.id == "security_events"
        )
    return isinstance(func, ast.Name) and func.id == "emit"


def _sweep() -> list[str]:
    problems: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        if path in _SWEEP_EXEMPT:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        rel = path.relative_to(ROOT)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            event = None
            names: list[str] = []
            if _is_logger_call(node):
                extra = next(
                    (kw.value for kw in node.keywords if kw.arg == "extra"), None
                )
                if extra is None:
                    continue
                if not isinstance(extra, ast.Dict):
                    problems.append(
                        f"{rel}:{node.lineno}: extra= is not a literal dict, so the "
                        "allow-list cannot police it"
                    )
                    continue
                for key in extra.keys:
                    if not isinstance(key, ast.Constant) or not isinstance(
                        key.value, str
                    ):
                        problems.append(
                            f"{rel}:{node.lineno}: extra= expands dynamically"
                        )
                        continue
                    names.append(key.value)
                first = node.args[0] if node.args else None
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    event = first.value
            elif _is_emit_call(node):
                first = node.args[0] if node.args else None
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    event = first.value
                for keyword in node.keywords:
                    if keyword.arg is None:
                        problems.append(
                            f"{rel}:{node.lineno}: emit(**fields) expands dynamically"
                        )
                        continue
                    if keyword.arg in EMITTER_CONTROL:
                        continue
                    names.append(keyword.arg)
            else:
                continue

            for name in names:
                if name in _KNOWN_GAPS.get(str(rel), ()):
                    continue
                if name in FORMATTER_OWNED:
                    problems.append(
                        f"{rel}:{node.lineno}: {name!r} is formatter-owned"
                    )
                elif name not in ALLOWED_FIELDS:
                    problems.append(
                        f"{rel}:{node.lineno}: {name!r} is not allow-listed"
                    )
                elif (
                    event in security_events.EVENT_FIELDS
                    and name not in security_events.EVENT_FIELDS[event]
                ):
                    problems.append(
                        f"{rel}:{node.lineno}: {event!r} does not declare {name!r}"
                    )
    return problems


def test_no_call_site_passes_a_field_the_allow_list_does_not_know():
    assert _sweep() == []


def test_the_sweep_catches_an_unknown_key(tmp_path, monkeypatch):
    offender = tmp_path / "offender.py"
    offender.write_text('logger.warning("e", extra={"vault_path": p})\n')
    monkeypatch.setitem(globals(), "SRC", tmp_path)
    monkeypatch.setitem(globals(), "ROOT", tmp_path)
    problems = _sweep()
    assert len(problems) == 1 and "not allow-listed" in problems[0]


def test_the_sweep_rejects_a_dynamic_field_set(tmp_path, monkeypatch):
    offender = tmp_path / "offender.py"
    offender.write_text(
        "logger.warning('e', extra=fields)\n"
        "logger.warning('e', extra={**base, 'reason': 'x'})\n"
        "security_events.emit('auth_failure', **fields)\n"
    )
    monkeypatch.setitem(globals(), "SRC", tmp_path)
    monkeypatch.setitem(globals(), "ROOT", tmp_path)
    problems = _sweep()
    assert len(problems) == 3
    assert any("not a literal dict" in p for p in problems)
    assert any("extra= expands dynamically" in p for p in problems)
    assert any("emit(**fields)" in p for p in problems)


def test_the_sweep_rejects_a_formatter_owned_name(tmp_path, monkeypatch):
    offender = tmp_path / "offender.py"
    offender.write_text("security_events.emit('auth_failure', stack='forged')\n")
    monkeypatch.setitem(globals(), "SRC", tmp_path)
    monkeypatch.setitem(globals(), "ROOT", tmp_path)
    problems = _sweep()
    assert len(problems) == 1 and "formatter-owned" in problems[0]


def test_the_sweep_checks_the_field_against_the_named_event(tmp_path, monkeypatch):
    offender = tmp_path / "offender.py"
    offender.write_text("security_events.emit('auth_failure', grant_id='g')\n")
    monkeypatch.setitem(globals(), "SRC", tmp_path)
    monkeypatch.setitem(globals(), "ROOT", tmp_path)
    problems = _sweep()
    assert len(problems) == 1 and "does not declare" in problems[0]
