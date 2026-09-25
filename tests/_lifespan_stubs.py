"""Shared stubs for offline tests that drive the real `lifespan` (#184, #185).

The lifespan's first database contact is `check_database_transport()`, which
opens a real session, and `log_embedding_transport()` emits a record. A test
that stubs the other startup guards must stub these two as well, or it would
attempt a real connection. The dedicated startup tests
(`tests/test_internal_transport_startup.py`) exercise the real functions
against a mocked session instead.
"""


async def _noop_async():
    return None


async def _noop_flush(*args, **kwargs):
    return False


def stub_transport_checks(monkeypatch, main_module):
    """Replace the lifespan's transport probe and report with no-ops.

    Also the durable concurrency-counter run registration and its shutdown
    flush (#188): both open a real session, which an offline lifespan test
    must not attempt.
    """
    monkeypatch.setattr(main_module, "check_database_transport", _noop_async)
    monkeypatch.setattr(main_module, "log_embedding_transport", lambda: None)
    counters = main_module.concurrency_counters
    monkeypatch.setattr(counters, "register_run", _noop_flush)
    monkeypatch.setattr(counters, "flush", _noop_flush)
