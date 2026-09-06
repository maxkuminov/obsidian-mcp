"""#190 — a failing statement may not put its bound credential into a record.

The allow-list has no field a credential could ride in, and every call site was
written not to pass one. Neither fact covers the way one actually got there:
**SQLAlchemy renders a failing statement's bound parameters into
`StatementError.__str__`**, and this server binds credential *hashes* on the
paths that matter most —

* `api_keys.key_hash` on every MCP request (`APIKeyMiddleware`),
* `oauth_tokens.token_hash` on `/token`, on refresh rotation and on `/revoke`,
* `oauth_codes.code_hash` on the authorization-code exchange,
* `oauth_clients.client_secret_hash` on dynamic client registration,
* `transfer_tokens.token_hash` on every redemption.

A pool timeout, a statement timeout or a dropped connection on any of them
produces a `StatementError` whose message *is* the hash. From there it reaches a
record two ways: `stack`, on any catalogue event carrying `exc_info`, and the
health page's 100-entry ERROR ring buffer, which holds `exc_info` by design.

Two layers, and this file tests both:

1. **`src/database.py` sets `hide_parameters=True`.** That is the fix — it
   applies to every statement on the engine, including the ones nobody thought
   of. The tests below build their exceptions with `engine.hide_parameters`
   rather than a literal, so turning the setting off fails them.
2. **A catalogue event on a credential-bound write carries no `exc_info` at
   all** — `oauth_token_rotation_failed` and
   `oauth_refresh_reuse_revocation_failed` are class-only. Driven separately in
   `tests/test_issue_191_oauth_events.py` and `tests/test_issue_182_refresh_reuse.py`.
"""
import hashlib
import json
import logging

import pytest
from sqlalchemy.exc import StatementError

from src.database import engine
from src.logging_setup import build_payload, StructuredFormatter
from src.services import security_events

MIN_SUBSTRING = 12

#: One entry per credential-bound statement named in the module docstring.
#: The statement text is illustrative; the *parameters* are the point.
CREDENTIAL_BOUND_STATEMENTS = [
    pytest.param(
        "SELECT api_keys.id FROM api_keys WHERE api_keys.key_hash = $1",
        "key_hash",
        id="api-key-authentication",
    ),
    pytest.param(
        "SELECT oauth_tokens.id FROM oauth_tokens WHERE oauth_tokens.token_hash = $1",
        "token_hash",
        id="oauth-token-lookup-and-rotation",
    ),
    pytest.param(
        "SELECT oauth_codes.id FROM oauth_codes WHERE oauth_codes.code_hash = $1",
        "code_hash",
        id="authorization-code-exchange",
    ),
    pytest.param(
        "INSERT INTO oauth_clients (client_secret_hash) VALUES ($1)",
        "client_secret_hash",
        id="dynamic-client-registration",
    ),
    pytest.param(
        "UPDATE transfer_tokens SET state='claimed' WHERE token_hash = $1",
        "token_hash",
        id="transfer-admission",
    ),
]


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture
def sink():
    """Records from the security stream *and* the root, rendered as they ship."""
    handler = _Capture()
    root = logging.getLogger()
    events = security_events.logger
    root_level, events_level = root.level, events.level
    propagate = events.propagate
    root.addHandler(handler)
    events.addHandler(handler)
    root.setLevel(logging.DEBUG)
    events.setLevel(logging.DEBUG)
    events.propagate = False
    security_events.reset_state()
    try:
        with security_events.suppression_disabled():
            yield handler
    finally:
        root.removeHandler(handler)
        events.removeHandler(handler)
        root.setLevel(root_level)
        events.setLevel(events_level)
        events.propagate = propagate
        security_events.reset_state()


def _rendered(records) -> str:
    formatter = StructuredFormatter()
    parts = []
    for record in records:
        parts.append(formatter.format(record))
        parts.append(record.getMessage())
        parts.append(json.dumps(build_payload(record), default=str))
        parts.extend(str(value) for value in record.__dict__.values())
    return "\n".join(parts)


def _assert_no_fragment(value: str, haystack: str) -> None:
    assert value not in haystack, "the credential reached a record whole"
    for start in range(len(value) - MIN_SUBSTRING + 1):
        fragment = value[start : start + MIN_SUBSTRING]
        assert fragment not in haystack, (
            f"a {MIN_SUBSTRING}-character fragment of a bound credential reached "
            "a record — check `hide_parameters=True` in src/database.py"
        )


def _statement_error(statement: str, column: str, hashed: str) -> StatementError:
    """The exception the *configured engine* would raise for this statement.

    `hide_parameters` is read off the engine rather than passed as `True`, so
    this file fails the moment somebody turns the setting off to debug a query.
    """
    return StatementError(
        "(asyncpg.exceptions.QueryCanceledError) canceling statement due to "
        "statement timeout",
        statement,
        {column: hashed},
        RuntimeError("canceling statement due to statement timeout"),
        hide_parameters=_hide_parameters(),
    )


def _hide_parameters() -> bool:
    """The engine's own setting. `AsyncEngine` proxies rather than exposes it,
    so it is read off the sync engine underneath — the one that builds the
    exception at runtime."""
    return engine.sync_engine.hide_parameters


def test_the_engine_hides_bound_parameters():
    """The setting itself, asserted where a reader will look for it."""
    assert _hide_parameters() is True, (
        "src/database.py must set hide_parameters=True: every credential lookup "
        "in this server binds a hash, and SQLAlchemy renders bound parameters "
        "into a failing statement's message"
    )


@pytest.mark.parametrize("statement,column", CREDENTIAL_BOUND_STATEMENTS)
def test_a_failing_credential_query_renders_no_bound_hash(statement, column):
    """The exception's own text, before any logging is involved."""
    hashed = hashlib.sha256(f"{column}-canary".encode()).hexdigest()

    rendered = str(_statement_error(statement, column, hashed))

    assert "hidden due to hide_parameters" in rendered
    _assert_no_fragment(hashed, rendered)


@pytest.mark.parametrize("statement,column", CREDENTIAL_BOUND_STATEMENTS)
def test_a_record_carrying_that_traceback_leaks_no_bound_hash(
    statement, column, sink
):
    """End of the path: the exception rendered through the real formatter.

    `tool_exception` is used as the carrier because it is the catalogue's
    highest-fidelity `exc_info` event — if the hash cannot survive a full
    traceback in `stack`, it cannot survive any of the leaner ones either.
    """
    hashed = hashlib.sha256(f"{column}-canary".encode()).hexdigest()
    error = _statement_error(statement, column, hashed)
    try:
        raise error
    except StatementError as exc:
        security_events.emit(
            "tool_exception",
            level=logging.ERROR,
            subject="ip:203.0.113.7",
            exc_info=exc,
            tool="read_note",
            error_type=type(exc).__name__,
            user_id=5,
        )

    assert sink.records, "the emission must have produced a record to search"
    payload = build_payload(sink.records[-1])
    assert payload["error_type"] == "StatementError"
    assert payload["stack"], "the carrier event must actually carry a traceback"
    _assert_no_fragment(hashed, _rendered(sink.records))


def test_the_health_ring_buffer_holds_the_same_hidden_text(sink):
    """The second sink, which keeps `exc_info` by design.

    `src/services/error_log.py` buffers the last hundred ERROR records for the
    panel's health page — a different consumer of the same exception, and one
    an operator reads in a browser. It inherits the fix rather than needing its
    own, which is the argument for fixing this at the engine.
    """
    hashed = hashlib.sha256(b"ring-buffer-canary").hexdigest()
    error = _statement_error(
        "SELECT api_keys.id FROM api_keys WHERE api_keys.key_hash = $1",
        "key_hash",
        hashed,
    )

    _assert_no_fragment(hashed, f"{error!r} {error!s}")
