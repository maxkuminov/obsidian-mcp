"""#191 — the 429 leaves a record, and the response is still slowapi's.

`src/main.py`'s `_rate_limit_handler` is the *one* hook every rate-limited route
in this app passes through: the panel login, key creation, DCR, `/token`, the
transfer pages. A per-route record would have been N chances to forget one, so
there is exactly one — and because it is exactly one, nothing else asserts it.

Two properties, and they pull against each other, which is why they are checked
together:

* **The record exists and is complete.** A 429 is the only externally visible
  sign that somebody is hammering a credential surface, and an operator who
  cannot see which route, from which address, against which limit learns
  nothing from it.
* **The response does not move.** The record is a side effect. The handler
  returns slowapi's own response, byte for byte — same status, same body — so
  wrapping the delegate can never change what a caller observes.
"""
import logging
from types import SimpleNamespace

import limits
import pytest
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from slowapi.wrappers import Limit
from starlette.requests import Request

import src.main as main
from src.limiter import limiter
from src.services import security_events

PEER = "203.0.113.44"


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)

    def named(self, event: str) -> list[logging.LogRecord]:
        return [r for r in self.records if r.getMessage() == event]


@pytest.fixture
def events(monkeypatch):
    """Records plus the `acquire` count, with the suppressor forced open.

    "Exactly one emission" is a claim about the *attempt*, not about what
    survived a flood bound — a handler that emitted twice under a suppressor
    that swallowed the second would look identical to a correct one.
    """
    handler = _Capture()
    logger = security_events.logger
    logger.addHandler(handler)
    propagate, level = logger.propagate, logger.level
    logger.propagate = False
    logger.setLevel(logging.DEBUG)

    acquires: list[tuple] = []
    real_acquire = security_events.acquire

    def counting_acquire(event, subject=None, **kwargs):
        acquires.append((event, subject))
        return real_acquire(event, subject, **kwargs)

    monkeypatch.setattr(security_events, "acquire", counting_acquire)
    security_events.reset_state()
    limiter.reset()
    try:
        with security_events.suppression_disabled():
            yield handler, acquires
    finally:
        logger.removeHandler(handler)
        logger.propagate = propagate
        logger.setLevel(level)
        security_events.reset_state()
        limiter.reset()


def fields(record) -> dict:
    standard = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__)
    standard |= {"message", "asctime", "taskName"}
    return {k: v for k, v in record.__dict__.items() if k not in standard}


def _limit(spec: str = "5/minute") -> Limit:
    """The wrapper slowapi hands the exception, built the way slowapi builds it."""
    return Limit(
        limit=limits.parse(spec),
        key_func=get_remote_address,
        scope=None,
        per_method=False,
        methods=None,
        error_message=None,
        exempt_when=None,
        cost=1,
        override_defaults=False,
    )


def _request(path: str = "/admin/auth/login", method: str = "POST") -> Request:
    """A real request, carrying what the delegate reaches into.

    `_rate_limit_exceeded_handler` reads `request.app.state.limiter` and
    `request.state.view_rate_limit`, so a stand-in object will not do: the point
    of this test is that the *real* delegate still produces the response.
    """
    limit = _limit()
    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "https",
            "path": path,
            "raw_path": path.encode(),
            "root_path": "",
            "query_string": b"",
            "headers": [(b"host", b"testserver")],
            "client": (PEER, 51234),
            "server": ("testserver", 443),
            "app": SimpleNamespace(state=SimpleNamespace(limiter=limiter)),
            "state": {},
        }
    )
    request.state.view_rate_limit = (limit.limit, [PEER])
    return request


def test_the_shared_handler_records_the_429_once_and_completely(events):
    handler, acquires = events
    request = _request()

    response = main._rate_limit_handler(request, RateLimitExceeded(_limit()))

    assert [event for event, _ in acquires] == ["rate_limit_exceeded"], (
        "one 429 is one emission attempt — not zero, and not one per route "
        "decorator in the chain"
    )
    (record,) = handler.named("rate_limit_exceeded")
    assert record.levelno == logging.WARNING
    assert fields(record) == {
        "route": "/admin/auth/login",
        "method": "POST",
        "client_ip": PEER,
        "limit_count": 5,
        "window_seconds": 60,
    }
    assert response.status_code == 429


def test_the_response_is_slowapis_own_and_unchanged(events):
    """The delegate's answer, byte for byte.

    Compared against a second call to slowapi's handler with the same inputs
    rather than against a hard-coded body: the assertion is "we did not change
    it", which stays true if slowapi rewords its own error.
    """
    exc = RateLimitExceeded(_limit())

    wrapped = main._rate_limit_handler(_request(), exc)
    direct = _rate_limit_exceeded_handler(_request(), exc)

    assert wrapped.status_code == direct.status_code == 429
    assert wrapped.body == direct.body
    assert wrapped.media_type == direct.media_type


def test_the_route_is_the_path_and_never_the_query_string(events):
    """`route` is `request.url.path`. There is no field a query could ride in
    (design D2), and this is the one handler that sees an arbitrary URL."""
    handler, _ = events
    request = _request(path="/oauth/token")
    request.scope["query_string"] = b"client_secret=hunter2&code=abc"

    main._rate_limit_handler(request, RateLimitExceeded(_limit()))

    (record,) = handler.named("rate_limit_exceeded")
    assert fields(record)["route"] == "/oauth/token"
    blob = repr(fields(record)) + record.getMessage()
    assert "hunter2" not in blob and "client_secret" not in blob


def test_a_broken_limit_object_still_records_and_still_answers(events):
    """Telemetry may not break a 429.

    The two numbers are dug out of slowapi's internals, which is exactly the
    kind of reach that breaks on an upgrade. When it does, the record loses two
    fields — every field is optional — and the caller still gets its 429.
    """
    handler, _ = events
    exc = RateLimitExceeded(_limit())
    exc.limit = SimpleNamespace(limit=None)

    response = main._rate_limit_handler(_request(), exc)

    (record,) = handler.named("rate_limit_exceeded")
    carried = fields(record)
    assert "limit_count" not in carried and "window_seconds" not in carried
    assert carried["route"] == "/admin/auth/login"
    assert response.status_code == 429


def test_the_subject_is_the_trusted_address(events):
    """A 429 is unauthenticated by construction, so the address is the only
    accounting key there is — and it must be the peer, never a header."""
    handler, acquires = events
    request = _request()
    request.scope["headers"] = [
        (b"host", b"testserver"),
        (b"x-forwarded-for", b"198.51.100.9"),
    ]

    main._rate_limit_handler(request, RateLimitExceeded(_limit()))

    (_, subject) = acquires[0]
    assert PEER in subject and "198.51.100.9" not in subject
    (record,) = handler.named("rate_limit_exceeded")
    assert fields(record)["client_ip"] == PEER
