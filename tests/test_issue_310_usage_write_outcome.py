"""#310 — the three-valued usage write outcome and the coalescer drop (D5, D6)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import src.mcp_server.tools as tools
import src.services.rate_limits as rate_limits
from src.config import Settings
from src.services import concurrency, security_events

Outcome = tools.UsageWriteOutcome


class _SQLStateError(Exception):
    def __init__(self, sqlstate: str):
        super().__init__(f"simulated {sqlstate}")
        self.sqlstate = sqlstate


class InterfaceError(Exception):
    """Named like a DB-API interface failure: never poison."""


@pytest.fixture
def harness(monkeypatch):
    """Real writer path, fake `_insert_usage`, captured security events."""
    controller = concurrency.Controller(Settings(
        _env_file=None, secret_key="issue-310-test-secret-only-0123456789abcdef",
        mcp_concurrency_mode="off"))
    monkeypatch.setattr(concurrency, "_controller", controller)
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        security_events, "emit", lambda event, **kw: events.append((event, kw))
    )
    state = SimpleNamespace(inserts=[], fail=[], events=events)

    async def insert(values):
        state.inserts.append(values)
        if state.fail:
            exc = state.fail.pop(0)
            if exc is not None:
                raise exc

    monkeypatch.setattr(tools, "_insert_usage", insert)
    monkeypatch.setattr(
        tools, "_is_fk_violation",
        lambda exc: getattr(exc, "sqlstate", None) == "23503",
    )
    monkeypatch.setattr(tools, "_violated_user_fk", lambda exc: False)
    rate_limits.reset_state_for_tests()
    yield state
    rate_limits.reset_state_for_tests()


def _row(**params):
    return dict(tool="probe", params=params, user_id=7, key_id=3,
                oauth_token_id=None, duration_ms=0, response_size=0)


# ── outcome classification (design Tests 3) ──────────────────────────────


async def test_success_is_landed_and_bool_is_true(harness):
    assert await tools.write_usage_row_outcome(_row(a=1)) is Outcome.LANDED
    assert await tools.write_usage_row(_row(a=1)) is True


@pytest.mark.parametrize("code", ["22P05", "22P02", "22021", "54000"])
async def test_data_class_failure_is_unstorable(harness, code):
    harness.fail = [_SQLStateError(code)]
    assert await tools.write_usage_row_outcome(_row()) is Outcome.UNSTORABLE
    harness.fail = [_SQLStateError(code)]
    assert await tools.write_usage_row(_row()) is False


async def test_bare_unicode_encode_error_is_unstorable(harness):
    try:
        "\ud800".encode("utf-8")
    except UnicodeEncodeError as exc:
        harness.fail = [exc]
    assert await tools.write_usage_row_outcome(_row()) is Outcome.UNSTORABLE


async def test_fk_violation_then_data_failure_is_unstorable(harness):
    harness.fail = [_SQLStateError("23503"), _SQLStateError("22P05")]
    assert await tools.write_usage_row_outcome(_row()) is Outcome.UNSTORABLE
    assert len(harness.inserts) == 2
    assert harness.inserts[1]["key_id"] is None


@pytest.mark.parametrize(
    "exc", [InterfaceError("gone"), OSError("reset"), RuntimeError("pool"),
            _SQLStateError("40001"), TypeError("not serialisable")]
)
async def test_other_failures_are_failed(harness, exc):
    harness.fail = [exc]
    assert await tools.write_usage_row_outcome(_row()) is Outcome.FAILED


async def test_writer_refusal_is_failed(harness, monkeypatch):
    controller = concurrency.Controller(Settings(
        _env_file=None, secret_key="issue-310-test-secret-only-0123456789abcdef",
        mcp_concurrency_mode="enforce", mcp_concurrency_writer_wait_seconds=0.02))
    monkeypatch.setattr(concurrency, "_controller", controller)
    holders = []
    while True:
        admission = await controller.writer()
        if not admission.admitted:
            break
        holders.append(admission)
    try:
        assert await tools.write_usage_row_outcome(_row()) is Outcome.FAILED
    finally:
        for holder in holders:
            holder.lease.release()
    assert harness.inserts == []


async def test_usage_log_failed_fields_and_reasons_are_unchanged(harness):
    harness.fail = [_SQLStateError("22P05")]
    await tools.write_usage_row_outcome(_row())
    failed = [kw for e, kw in harness.events if e == "usage_log_failed"]
    assert failed == [dict(subject="user:7", tool="probe", reason="initial",
                           error_type="_SQLStateError")]


# ── params rendering at the boundary (D2, Codex finding 4) ───────────────


async def test_params_are_rendered_before_the_insert_and_the_retry(harness):
    harness.fail = [_SQLStateError("23503")]
    assert await tools.write_usage_row(_row(path="a\x00b")) is True
    first, retry = harness.inserts
    assert first["params"] == {"path": "a\\x00b", "rendered_params": ["path"]}
    assert retry["params"] == first["params"]


async def test_absent_params_stay_absent(harness):
    values = dict(tool="probe", user_id=7)
    assert await tools.write_usage_row(values) is True
    assert "params" not in harness.inserts[0]


async def test_null_params_stay_null(harness):
    assert await tools.write_usage_row(dict(tool="probe", params=None, user_id=7))
    assert harness.inserts[0]["params"] is None


@tools._tracked("issue_310_telemetry_probe", ["query"], resource_class="light")
async def _telemetry_probe(query: str = "") -> str:
    from src.services import timing

    # `find_related`'s source path: a vault path, which a non-UTF-8 filename
    # decoded with `surrogateescape` turns into a lone surrogate.
    # `record_source_path` is total on it (`surrogatepass`). (`record_results`
    # is not — it raises measuring such a path, a separate defect noted in
    # the #310 report, so `result_paths` cannot carry one today.)
    timing.record_source_path("a\udc80.md")
    return "ran"


async def test_server_telemetry_is_rendered_at_the_insert_boundary(harness, monkeypatch):
    """Scenario "Server telemetry is covered". Telemetry is merged into
    the row *after* `named_params()`, so this pins the renderer at the insert
    boundary: rendering inside `named_params()` would leave the surrogate in
    the stored row and fail this test."""
    from src.auth.session import current_principal, current_user_id

    monkeypatch.setattr(tools, "_bucket_admission", lambda write: None)
    monkeypatch.setattr(tools, "_vault_admission_error", lambda: None)

    async def no_quota():
        return None

    monkeypatch.setattr(tools, "_quota_admission_error", no_quota)
    principal = current_principal.set(("api_key", 3102))
    uid = current_user_id.set(7)
    try:
        assert await _telemetry_probe(query="clean") == "ran"
    finally:
        current_user_id.reset(uid)
        current_principal.reset(principal)
    (row,) = [v for v in harness.inserts if v["tool"] == "issue_310_telemetry_probe"]
    params = row["params"]
    assert params["query"] == "clean"
    assert params["source_path"] == "a\\udc80.md"
    assert params["rendered_params"] == ["source_path"]


async def test_caller_values_are_not_mutated(harness):
    params = {"path": "a\x00b"}
    await tools.write_usage_row(_row(**params) | {"params": params})
    assert params == {"path": "a\x00b"}


# ── the coalescer (design Tests 4) ───────────────────────────────────────


def _plan(principal=("api_key", 1), params=None):
    template = dict(
        tool="read_note", user_id=7, key_id=1, oauth_token_id=None,
        duration_ms=0, response_size=0,
        params=dict(params or {"path": "x"}, error="rate_limited",
                    rate_limit_scope="principal"),
    )
    return rate_limits.record_rate_refusal(
        principal, "read_note", "rate_limited", "principal", lambda: template
    )


def _dropped(events):
    return [kw for e, kw in events if e == "usage_refusal_row_dropped"]


async def test_unstorable_immediate_row_is_dropped_without_inflating_pending(harness):
    harness.fail = [_SQLStateError("22P05")]
    planned = _plan()
    assert await rate_limits.write_planned_row(planned) is False
    entry = planned.entry
    assert entry.in_flight == 0
    window = entry.windows[planned.key]
    assert window.pending == 0
    assert _dropped(harness.events) == [dict(
        subject="user:7", tool="read_note", reason="rate_limited", count=1, user_id=7,
    )]
    # The window closes with nothing pending: a flush writes nothing at all.
    window.started -= 10_000
    harness.inserts.clear()
    assert await rate_limits.flush_expired() == 0
    assert harness.inserts == []


async def test_unstorable_flushed_row_is_dropped_once_and_not_retried(harness):
    planned = _plan()
    assert await rate_limits.write_planned_row(planned) is True
    for _ in range(3):
        assert _plan() is None  # folded into the open window
    entry = planned.entry
    entry.windows[planned.key].started -= 10_000
    harness.inserts.clear()
    harness.fail = [_SQLStateError("22P05")]
    assert await rate_limits.flush_expired() == 0
    assert len(harness.inserts) == 1  # exactly one attempt
    assert planned.key not in entry.windows  # no window re-created
    assert entry.in_flight == 0
    dropped = _dropped(harness.events)
    assert len(dropped) == 1 and dropped[0]["count"] == 3
    # Next tick: no INSERT of any kind for the dropped row.
    harness.inserts.clear()
    assert await rate_limits.flush_expired() == 0
    assert await rate_limits.flush_all() == 0
    assert harness.inserts == []


async def test_failed_flushed_row_still_requeues_with_original_start(harness):
    planned = _plan()
    await rate_limits.write_planned_row(planned)
    assert _plan() is None
    assert _plan() is None
    entry = planned.entry
    entry.windows[planned.key].started -= 10_000
    started = entry.windows[planned.key].started
    harness.fail = [OSError("connection reset")]
    assert await rate_limits.flush_expired() == 0
    window = entry.windows[planned.key]
    assert window.started == started and window.pending == 2
    assert _dropped(harness.events) == []
    harness.inserts.clear()
    assert await rate_limits.flush_expired() == 1
    assert harness.inserts[0]["params"]["suppressed"] == 1


async def test_failed_immediate_row_still_requeues(harness):
    harness.fail = [RuntimeError("pool")]
    planned = _plan()
    assert await rate_limits.write_planned_row(planned) is False
    assert planned.entry.windows[planned.key].pending == 1
    assert _dropped(harness.events) == []


async def test_writer_exception_still_requeues(harness, monkeypatch):
    async def boom(values):
        raise RuntimeError("raised out of the writer")

    monkeypatch.setattr(tools, "write_usage_row_outcome", boom)
    planned = _plan()
    assert await rate_limits.write_planned_row(planned) is False
    assert planned.entry.windows[planned.key].pending == 1
    assert _dropped(harness.events) == []


async def test_a_failing_emit_cannot_fail_the_drop(harness, monkeypatch):
    def explode(event, **kw):
        raise RuntimeError("log sink gone")

    monkeypatch.setattr(security_events, "emit", explode)
    harness.fail = [_SQLStateError("22P05")]
    planned = _plan()
    assert await rate_limits.write_planned_row(planned) is False
    assert planned.entry.in_flight == 0


async def test_slot_timeout_drop_names_its_marker(harness):
    template = dict(tool="probe", user_id=None, key_id=None, oauth_token_id=None,
                    duration_ms=0, response_size=0, params={"error": "slot_timeout"})
    planned = rate_limits.record_rate_refusal(
        ("api_key", 2), "probe", "slot_timeout", "other", lambda: template
    )
    harness.fail = [_SQLStateError("22021")]
    assert await rate_limits.write_planned_row(planned) is False
    dropped = _dropped(harness.events)
    assert dropped[0]["reason"] == "slot_timeout" and dropped[0]["user_id"] is None


def test_the_drop_emission_passes_the_strict_catalogue(monkeypatch):
    """The real emitter, strict: the fields `_report_dropped` passes are
    exactly the declared ones (the drop path swallows emit failures, so this
    is the only place a catalogue mismatch would surface)."""
    rate_limits.reset_state_for_tests()
    template = dict(tool="read_note", user_id=7, params={"error": "rate_limited"})
    planned = rate_limits.record_rate_refusal(
        ("api_key", 9), "read_note", "rate_limited", "principal", lambda: template
    )
    calls = []
    real_emit = security_events.emit

    def strict_emit(event, **kw):
        with security_events.strict_fields():
            real_emit(event, **kw)
        calls.append(event)

    monkeypatch.setattr(security_events, "emit", strict_emit)
    rate_limits._report_dropped(planned)
    assert calls == ["usage_refusal_row_dropped"]
    rate_limits.reset_state_for_tests()


def test_the_event_is_catalogued_with_exactly_its_four_fields():
    assert security_events.EVENT_FIELDS["usage_refusal_row_dropped"] == frozenset(
        {"tool", "reason", "count", "user_id"}
    )


async def test_an_unstorable_argument_lands_with_the_real_renderer(harness):
    """With the renderer in place, the same row lands on its first attempt."""
    planned = _plan(params={"path": "\ud800\x00", "frontmatter": {"x": float("nan")}})
    assert await rate_limits.write_planned_row(planned) is True
    stored = harness.inserts[0]["params"]
    assert stored["path"] == "\\ud800\\x00"
    assert stored["frontmatter"] == {"x": ".nan"}
    assert stored["rendered_params"] == ["frontmatter", "path"]
    assert asyncio.iscoroutinefunction(tools.write_usage_row_outcome)
