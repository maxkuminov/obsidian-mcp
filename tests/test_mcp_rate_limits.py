"""L2/L3 — the two per-principal token buckets, the refusal shape, and the
coalescer (#188, #194). Hermetic.

The properties pinned here are the ones an operator would never see going
wrong:

* **The admitted path issues no statement at all.** A gate on the hottest path
  in the server that quietly started taking a connection would be invisible
  from every surface until the pool ran out, so it is counted, the way
  `tests/test_issue_162_quota_gate.py` counts the quota gate's.
* **Nothing durable is consumed by a call that does not run.** A rate-refused
  call must leave `quota_counters` untouched: a token refills, a daily slot
  does not.
* **The principal is the grant.** Refreshing an access token continues an
  allowance; two grants of the same client and user hold separate ones, because
  #64 made them independently revocable and an operator's revocation has to
  mean something.
* **The coalescer's arithmetic is exact on every interleaving.** `pending`
  counts refusals *no row yet represents*, a rollover has the arriving refusal
  as its row's base and a standalone flush does not — get that asymmetry wrong
  and every burst is over- or under-counted by exactly one.
* **A deferred flush needs no request and no live credential**, because the row
  it writes was captured whole when the window opened.
"""
import ast
import asyncio
import inspect
import json
import pathlib
import textwrap
from types import SimpleNamespace

import pytest

import src.mcp_server.auth as mcp_auth
import src.mcp_server.tools as tools
import src.services.quotas as quotas
import src.services.rate_limits as rate_limits
import src.services.refusals as refusals
from src.auth.session import current_actor, current_principal, current_user_id
from src.config import Settings
from src.mcp_server.read_result import ReadNoteResult


API_KEY_PRINCIPAL = ("api_key", 7)
OTHER_PRINCIPAL = ("api_key", 8)


# ── plumbing ────────────────────────────────────────────────────────────────


class _QuotaSpySession:
    """Stands in for the quota admission's own `async_session()`, counting."""

    def __init__(self, admitted=1):
        self.statements = []
        self.admitted = admitted

    def __call__(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def execute(self, stmt, params=None):
        self.statements.append(str(stmt))
        return SimpleNamespace(
            scalar=lambda: self.admitted, fetchall=lambda: []
        )

    async def commit(self):
        pass

    async def rollback(self):  # pragma: no cover
        pass


@tools._tracked("rate_probe", ["value"])
async def _probe(value: str = "x") -> str:
    return f"ran:{value}"


@tools._tracked("rate_probe_write", ["value"], write_class=True)
async def _write_probe(value: str = "x") -> str:
    return f"wrote:{value}"


@tools._tracked(
    "rate_probe_structured", ["path"], refusal_result=tools._read_note_refusal
)
async def _structured_probe(path: str = "n.md") -> ReadNoteResult:  # pragma: no cover
    return ReadNoteResult(content="body")


def _run(
    fn,
    *args,
    principal=API_KEY_PRINCIPAL,
    limit=None,
    key_id=7,
    quota_spy=None,
    rows=None,
    user_id=None,
    **kwargs,
):
    """Call a `_tracked` function with a bound principal. Returns the result.

    `rows` collects every `usage_logs` row the call writes, so "no statement of
    any kind" is checked by its emptiness rather than by inspection.
    """
    rows = [] if rows is None else rows
    quota_spy = quota_spy or _QuotaSpySession()

    async def fake_log_usage(tool, params, duration_ms, response_size):
        rows.append({"tool": tool, "params": params})
        return True

    mp = pytest.MonkeyPatch()
    mp.setattr(tools, "_log_usage", fake_log_usage)
    mp.setattr(quotas, "async_session", quota_spy)

    async def run():
        tokens = [
            current_principal.set(principal),
            mcp_auth.current_daily_request_limit.set(limit),
            mcp_auth.current_api_key_id.set(key_id),
            current_user_id.set(user_id),
            current_actor.set(("api_key", "probe key", "omcp_abc")),
        ]
        try:
            return await fn(*args, **kwargs)
        finally:
            for var, token in zip(
                (
                    current_principal,
                    mcp_auth.current_daily_request_limit,
                    mcp_auth.current_api_key_id,
                    current_user_id,
                    current_actor,
                ),
                tokens,
            ):
                var.reset(token)

    try:
        return asyncio.run(run())
    finally:
        mp.undo()


def _sentinel(text: str) -> dict:
    """The parsed final line of a refusal, or an assertion failure."""
    last = text.splitlines()[-1]
    assert last.startswith("MCP-REFUSAL "), text[-200:]
    return json.loads(last[len("MCP-REFUSAL ") :])


def _drain(principal=API_KEY_PRINCIPAL, scope=refusals.SCOPE_PRINCIPAL):
    """Spend every token in one bucket, so the next call is refused."""
    while rate_limits.take(principal, scope)[0]:
        pass


# ── 1. the buckets ──────────────────────────────────────────────────────────


def test_sustained_calls_under_the_rate_are_all_admitted(monkeypatch):
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(
        rate_limits, "time", SimpleNamespace(monotonic=lambda: clock.now)
    )
    for _ in range(200):
        clock.now += 0.6  # 100/min, under the configured 120/min
        admitted, _retry = rate_limits.take(API_KEY_PRINCIPAL, refusals.SCOPE_PRINCIPAL)
        assert admitted


def test_the_burst_is_the_capacity_and_the_next_call_is_refused(monkeypatch):
    monkeypatch.setattr(mcp_auth.settings, "mcp_rate_limit_per_minute", 60)
    monkeypatch.setattr(mcp_auth.settings, "mcp_rate_limit_burst", 5)
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(
        rate_limits, "time", SimpleNamespace(monotonic=lambda: clock.now)
    )
    for _ in range(5):
        assert rate_limits.take(API_KEY_PRINCIPAL, refusals.SCOPE_PRINCIPAL)[0]
    admitted, retry_after = rate_limits.take(
        API_KEY_PRINCIPAL, refusals.SCOPE_PRINCIPAL
    )
    assert not admitted
    assert retry_after == 1  # 60/min refills one token per second


def test_the_write_bucket_refuses_writes_while_reads_continue():
    """The write bucket is a *velocity* bound on the calls that change vault
    bytes; the same principal's reads are unaffected."""
    _drain(scope=refusals.SCOPE_PRINCIPAL_WRITE)
    refusal = _run(_write_probe)
    assert _sentinel(refusal)["scope"] == refusals.SCOPE_PRINCIPAL_WRITE
    assert _run(_probe) == "ran:x"


def test_one_principals_burst_does_not_touch_another(monkeypatch):
    _drain(principal=API_KEY_PRINCIPAL)
    assert "MCP-REFUSAL" in _run(_probe, principal=API_KEY_PRINCIPAL)
    assert _run(_probe, principal=OTHER_PRINCIPAL) == "ran:x"


def test_a_caller_with_no_principal_is_exempt_not_refused():
    """Sandbox mode and a direct in-process caller read `None` and are exempt,
    the same shape as `_quota_admission_error`'s "a limit with no key"."""
    for _ in range(500):
        assert rate_limits.take(None, refusals.SCOPE_PRINCIPAL)[0]
    assert _run(_probe, principal=None) == "ran:x"


def test_a_disabled_bucket_admits_everything(monkeypatch):
    monkeypatch.setattr(mcp_auth.settings, "mcp_rate_limit_per_minute", None)
    monkeypatch.setattr(mcp_auth.settings, "mcp_rate_limit_burst", None)
    rate_limits.reset_state_for_tests()
    for _ in range(500):
        assert rate_limits.take(API_KEY_PRINCIPAL, refusals.SCOPE_PRINCIPAL)[0]


# ── 2. the admitted path costs nothing ──────────────────────────────────────


def test_the_admitted_path_issues_zero_statements():
    spy = _QuotaSpySession()
    assert _run(_probe, quota_spy=spy, limit=None) == "ran:x"
    assert spy.statements == [], "the gates issued SQL on the admitted path"


def test_the_bucket_update_has_no_await_between_the_read_and_the_write():
    """On a single-threaded event loop a synchronous update is atomic *by
    construction*, which is why there is no lock. An `await` inside `take`
    would silently remove that property — two tasks could interleave between
    the read and the write and both spend the same token."""
    source = textwrap.dedent(inspect.getsource(rate_limits.TokenBucket.take))
    tree = ast.parse(source)
    assert not [node for node in ast.walk(tree) if isinstance(node, ast.Await)]
    assert not asyncio.iscoroutinefunction(rate_limits.take)


def test_the_limiter_module_never_opens_a_session():
    """No import of the session factory, so no statement can be issued from the
    hot path even by accident. The one place it *does* write — the coalescer's
    flush — reaches the writer through a deferred import inside the function,
    which is a cold path by definition."""
    source = pathlib.Path(inspect.getfile(rate_limits)).read_text()
    tree = ast.parse(source)
    top_level_imports = [
        node
        for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    assert not any(
        "database" in ast.unparse(node) or "async_session" in ast.unparse(node)
        for node in top_level_imports
    )


# ── 3. the gate order ───────────────────────────────────────────────────────


def test_a_rate_refused_call_consumes_no_quota():
    """A token refills and a daily slot does not, so the quota stays the last
    pre-body gate and a call refused above it spends nothing durable."""
    spy = _QuotaSpySession()
    _drain()
    result = _run(_probe, limit=5, quota_spy=spy)
    assert "MCP-REFUSAL" in result
    assert spy.statements == [], "a rate-refused call touched quota_counters"


def test_the_rate_gate_runs_above_the_vault_gate(monkeypatch):
    """A rate-refused call never resolves a vault root — and reveals nothing by
    not doing so, since its content depends only on the caller's own rate."""
    resolved = []

    def _explode(_uid=None):
        resolved.append(True)
        raise RuntimeError("unassigned")

    monkeypatch.setattr(tools, "_vault_root", _explode)
    _drain()
    result = _run(_probe)
    assert _sentinel(result)["code"] == refusals.RATE_LIMITED
    assert resolved == [], "the vault was resolved for a rate-refused call"


def test_an_admitted_call_still_meets_the_vault_gate(monkeypatch):
    def _explode(_uid=None):
        raise RuntimeError("unassigned")

    monkeypatch.setattr(tools, "_vault_root", _explode)
    result = _run(_probe)
    assert result == tools._NO_VAULT_MESSAGE
    assert _sentinel(result)["code"] == refusals.NO_VAULT_ASSIGNED


def test_the_documented_gate_order_is_the_implemented_one():
    """L2 → L3 → L4 → L5 → L6 → body, read off the decorator itself."""
    source = inspect.getsource(tools._tracked)
    order = [
        source.index("_bucket_admission(write_class)"),
        source.index("_vault_admission_error()"),
        source.index("_first_unencodable_argument(screened)"),
        source.index("_first_over_long_argument(screened"),
        source.index("await _quota_admission_error()"),
        source.index("result = await fn(*args, **kwargs)"),
    ]
    assert order == sorted(order)


# ── 4. the principal is the grant ───────────────────────────────────────────


def test_the_oauth_principal_is_the_grant_not_the_token():
    """A refresh mints a new `oauth_tokens` row inside the same grant. Keying
    on the token id would hand a refreshing agent a fresh allowance hourly."""
    before = ("oauth", "grant-1")
    after_refresh = ("oauth", "grant-1")
    assert before == after_refresh
    _drain(principal=before)
    assert not rate_limits.take(after_refresh, refusals.SCOPE_PRINCIPAL)[0]


def test_two_grants_of_one_client_and_user_are_distinct_principals():
    """#64 made two grants independently revocable. Merging them on
    `(client_id, user_id)` would mean revoking one did not free the other's
    allowance — an operator's stop that looks like it did not work."""
    _drain(principal=("oauth", "grant-1"))
    assert rate_limits.take(("oauth", "grant-2"), refusals.SCOPE_PRINCIPAL)[0]


def test_the_middleware_binds_the_grant_id_in_the_oauth_branch():
    source = inspect.getsource(mcp_auth.APIKeyMiddleware.__call__)
    assert 'current_principal.set(("oauth", oauth_token.grant_id))' in source
    assert 'current_principal.set(("api_key", api_key.id))' in source
    assert "current_principal.reset(token_principal)" in source


# ── 5. the refusal shape ────────────────────────────────────────────────────


def test_a_string_tool_exposes_the_parseable_line():
    _drain()
    result = _run(_probe)
    assert result.startswith("Error: this credential exceeded")
    payload = _sentinel(result)
    assert payload["code"] == refusals.RATE_LIMITED
    assert payload["scope"] == refusals.SCOPE_PRINCIPAL
    assert payload["limit_unit"] == refusals.CALLS_PER_MINUTE
    assert isinstance(payload["retry_after_seconds"], int)
    assert payload["retry_after_seconds"] >= 1


def test_a_structured_tool_carries_the_identical_text_in_its_error_field():
    """A bare string from a tool FastMCP validates against an output schema is
    a *protocol* error, not an in-band refusal."""
    _drain()
    result = _run(_structured_probe)
    assert isinstance(result, ReadNoteResult)
    assert result.error is not None
    assert _sentinel(result.error)["code"] == refusals.RATE_LIMITED
    # Identical text, not merely the same code: both kinds of tool expose the
    # same fields.
    rate_limits.reset_state_for_tests()
    _drain()
    string_form = _run(_probe)
    assert _sentinel(result.error) == _sentinel(string_form)


def test_a_futile_refusal_carries_no_retry_after(monkeypatch):
    def _explode(_uid=None):
        raise RuntimeError("unassigned")

    monkeypatch.setattr(tools, "_vault_root", _explode)
    assert "retry_after_seconds" not in _sentinel(_run(_probe))

    payload = _sentinel(tools._unencodable_argument_error("path"))
    assert payload["code"] == refusals.ARGUMENT_NOT_ENCODABLE
    assert "retry_after_seconds" not in payload

    with pytest.raises(refusals.RefusalShapeError):
        refusals.Refusal(code=refusals.NO_VAULT_ASSIGNED, retry_after_seconds=5)


def test_the_existing_prose_is_preserved_byte_for_byte():
    assert tools._NO_VAULT_MESSAGE.startswith(tools._NO_VAULT_PROSE)
    assert tools._NO_VAULT_MESSAGE.count("MCP-REFUSAL") == 1
    assert tools._unencodable_argument_error("q").startswith(
        "Argument 'q' is not valid UTF-8"
    )


def test_the_renderer_is_idempotent():
    """Two altitudes render the same refusal — the message's author and the
    gate that knows the retry interval — and neither may stack a second line."""
    once = refusals.render("prose", refusals.Refusal(code=refusals.OVER_QUOTA))
    twice = refusals.render(once, refusals.Refusal(code=refusals.OVER_QUOTA))
    assert once == twice
    assert once.count("MCP-REFUSAL") == 1


def test_the_code_set_is_closed():
    with pytest.raises(refusals.RefusalShapeError):
        refusals.Refusal(code="something_new")
    with pytest.raises(refusals.RefusalShapeError):
        refusals.Refusal(code=refusals.RATE_LIMITED, retry_after_seconds=0)
    assert {
        "rate_limited",
        "argument_too_long",
        "over_quota",
        "no_vault_assigned",
        "argument_not_encodable",
    } <= refusals.CODES


# ── 6. coalescing ───────────────────────────────────────────────────────────


def _refuse(times=1, rows=None, tool=_probe, user_id=None):
    rows = [] if rows is None else rows
    out = []
    for _ in range(times):
        _drain()
        out.append(_run(tool, rows=rows, user_id=user_id))
    return rows, out


def test_the_first_refusal_writes_its_own_row_and_the_rest_write_nothing():
    rows, _ = _refuse(times=5)
    assert len(rows) == 1, "a refusal inside the window issued a statement"
    row = rows[0]
    assert row["tool"] == "rate_probe"
    assert row["params"]["error"] == tools._RATE_LIMITED_MARKER
    assert row["params"][tools._RATE_LIMIT_SCOPE_PARAM] == refusals.SCOPE_PRINCIPAL
    assert row["params"][tools._SUPPRESSED_PARAM] == 0


def test_one_refusal_then_a_flush_writes_no_second_row(monkeypatch):
    """`pending == 0` means the refusal that opened the window already has its
    row. A flush that wrote anyway would count it twice."""
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(
        rate_limits, "time", SimpleNamespace(monotonic=lambda: clock.now)
    )
    rows, _ = _refuse(times=1)
    clock.now += 100
    written = _flush()
    assert written == []
    assert sum(1 + row["params"][tools._SUPPRESSED_PARAM] for row in rows) == 1


def _flush(monkeypatched_rows=None):
    """Run `flush_expired` with the row writer captured. Returns the rows."""
    written = []

    async def fake_write(values):
        written.append(values)
        return True

    mp = pytest.MonkeyPatch()
    mp.setattr(tools, "write_usage_row", fake_write)
    try:
        asyncio.run(rate_limits.flush_expired())
    finally:
        mp.undo()
    return written


def test_five_refusals_then_a_flush_sum_to_exactly_five(monkeypatch):
    """The standalone flush has no arriving refusal to serve as its row's base,
    so the row stands for one of the pending refusals itself: `pending − 1`."""
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(
        rate_limits, "time", SimpleNamespace(monotonic=lambda: clock.now)
    )
    rows, _ = _refuse(times=5)
    assert len(rows) == 1 and rows[0]["params"][tools._SUPPRESSED_PARAM] == 0

    clock.now += 100
    flushed = _flush()
    assert len(flushed) == 1
    assert flushed[0]["params"][tools._SUPPRESSED_PARAM] == 3
    total = (1 + rows[0]["params"][tools._SUPPRESSED_PARAM]) + (
        1 + flushed[0]["params"][tools._SUPPRESSED_PARAM]
    )
    assert total == 5


def test_a_rollover_counts_the_triggering_refusal_as_the_rows_base(monkeypatch):
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(
        rate_limits, "time", SimpleNamespace(monotonic=lambda: clock.now)
    )
    rows, _ = _refuse(times=5)
    clock.now += 100
    _refuse(times=1, rows=rows)  # the sixth refusal, after the window closed

    assert len(rows) == 2
    assert rows[1]["params"][tools._SUPPRESSED_PARAM] == 4
    assert sum(1 + row["params"][tools._SUPPRESSED_PARAM] for row in rows) == 6

    # …and `pending` was reset, so the next flush writes nothing.
    clock.now += 100
    assert _flush() == []


def test_a_mixed_run_of_rollovers_and_a_final_flush_sums_exactly(monkeypatch):
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(
        rate_limits, "time", SimpleNamespace(monotonic=lambda: clock.now)
    )
    rows = []
    observed = 0
    for burst in (3, 1, 7, 2):
        _refuse(times=burst, rows=rows)
        observed += burst
        clock.now += 100  # close the window between bursts
    flushed = _flush()
    total = sum(
        1 + row["params"][tools._SUPPRESSED_PARAM] for row in rows + flushed
    )
    assert total == observed


def test_the_two_scopes_are_not_merged():
    """`principal_write` and `principal` are different facts about the same
    tool; merging them would attribute a write refusal to the general bucket."""
    rows = []
    _drain(scope=refusals.SCOPE_PRINCIPAL_WRITE)
    _run(_write_probe, rows=rows)
    _drain(scope=refusals.SCOPE_PRINCIPAL)
    _run(_write_probe, rows=rows)
    scopes = [row["params"][tools._RATE_LIMIT_SCOPE_PARAM] for row in rows]
    assert sorted(scopes) == [
        refusals.SCOPE_PRINCIPAL,
        refusals.SCOPE_PRINCIPAL_WRITE,
    ]


def test_a_flush_needs_neither_request_context_nor_a_live_credential(monkeypatch):
    """By flush time the request is gone and the key may have been deleted. The
    row is written from the template captured when the window opened, and its
    denormalised actor columns are what survive the credential."""
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(
        rate_limits, "time", SimpleNamespace(monotonic=lambda: clock.now)
    )
    _refuse(times=4, user_id=1)
    clock.now += 100

    # Every ContextVar cleared, as it is once the request has returned.
    assert current_principal.get() is None
    assert current_actor.get() is None
    assert current_user_id.get() is None

    flushed = _flush()
    assert len(flushed) == 1
    row = flushed[0]
    assert row["actor_kind"] == "api_key"
    assert row["actor_label"] == "probe key"
    assert row["user_id"] == 1
    assert row["key_id"] == 7
    assert row["tool"] == "rate_probe"
    assert row["params"]["error"] == tools._RATE_LIMITED_MARKER


def test_a_flush_whose_write_fails_does_not_raise():
    """A credential deleted between the refusal and the flush makes the insert
    violate a foreign key; the writer's own 23503 recovery handles that, and a
    flush must never turn a failed row into a failed shutdown."""
    _refuse(times=3)
    for window in _windows():
        window.started -= 10_000

    async def failing_write(_values):
        return False

    mp = pytest.MonkeyPatch()
    mp.setattr(tools, "write_usage_row", failing_write)
    try:
        assert asyncio.run(rate_limits.flush_expired()) == 0
    finally:
        mp.undo()


def _windows():
    for entry in list(rate_limits._entries.values()):
        yield from entry.windows.values()


@tools._tracked("rate_probe_hostile", ["value"], transforms={"value": lambda v: 1 / 0})
async def _hostile_probe(value: str = "x") -> str:  # pragma: no cover
    return "ran"


def test_a_raising_transform_cannot_turn_a_refusal_into_an_exception():
    """The coalescer's template runs the tool's own `transforms`. One that
    raises on the value it was given must not escape the gate: a row with no
    arguments is a worse row, a refusal that became a traceback is a worse
    bug — the same trade the telemetry tail already makes."""
    rows = []
    _drain()
    result = _run(_hostile_probe, rows=rows)
    # The caller still receives the refusal it asked about, in the shape the
    # contract promises. (The row is separately lost here, because the
    # telemetry tail runs the same `transforms` and makes the same trade — it
    # records `tool_telemetry_failed` rather than failing a call that already
    # had its answer.)
    assert _sentinel(result)["code"] == refusals.RATE_LIMITED


def test_argument_too_long_is_not_coalesced():
    """It sits *below* the general bucket, so its rate is already bounded by
    that bucket. A second mechanism would buy nothing and cost a code path."""
    source = inspect.getsource(tools._tracked)
    marker_at = source.index("_ARGUMENT_TOO_LONG_MARKER")
    assert "record_rate_refusal" not in source[marker_at - 400 : marker_at + 400]


# ── 7. the flush is actually driven ─────────────────────────────────────────


def test_the_indexer_tick_flushes_and_swallows_its_failure():
    from src.services import indexer

    source = inspect.getsource(indexer.run_indexer_loop)
    assert "await flush_expired()" in source
    guarded = source[source.index("await flush_expired()") :]
    assert "except Exception" in guarded[:400]


def test_the_shutdown_flush_runs_before_the_engine_is_disposed():
    """After `engine.dispose()` there is nothing to write with, and the last
    window's counts would simply be lost."""
    from src import main

    source = inspect.getsource(main.lifespan)
    assert source.index("await flush_expired()") < source.index(
        "await engine.dispose()"
    )


# ── 8. bounded registries ───────────────────────────────────────────────────


def test_past_the_cap_principals_share_one_overflow_bucket(monkeypatch):
    monkeypatch.setattr(mcp_auth.settings, "mcp_limiter_max_tracked_principals", 2)
    monkeypatch.setattr(mcp_auth.settings, "mcp_rate_limit_per_minute", 60)
    monkeypatch.setattr(mcp_auth.settings, "mcp_rate_limit_burst", 2)
    rate_limits.reset_state_for_tests()

    inside = ("api_key", 1)
    assert rate_limits.take(inside, refusals.SCOPE_PRINCIPAL)[0]
    rate_limits.take(("api_key", 2), refusals.SCOPE_PRINCIPAL)

    # Two overflow principals now share one bucket, so one exhausts the other.
    assert rate_limits.take(("api_key", 98), refusals.SCOPE_PRINCIPAL)[0]
    assert rate_limits.take(("api_key", 99), refusals.SCOPE_PRINCIPAL)[0]
    assert not rate_limits.take(("api_key", 98), refusals.SCOPE_PRINCIPAL)[0]

    assert rate_limits.tracked_principals() == 2
    # …and the principal inside the cap is untouched by any of it.
    assert rate_limits.take(inside, refusals.SCOPE_PRINCIPAL)[0]


def test_overflow_coalescer_entries_drop_only_the_principal(monkeypatch):
    monkeypatch.setattr(mcp_auth.settings, "mcp_limiter_max_tracked_principals", 1)
    rate_limits.reset_state_for_tests()
    rate_limits.take(("api_key", 1), refusals.SCOPE_PRINCIPAL)

    template = lambda: {"tool": "t", "params": {}}  # noqa: E731
    assert (
        rate_limits.record_rate_refusal(
            ("api_key", 50), "t", "rate_limited", "principal", template
        )
        == 0
    )
    # A different overflowed principal, same `(tool, marker, scope)`: it folds
    # onto the same entry rather than opening a second window, so the row still
    # names the tool, the marker and the control that fired.
    assert (
        rate_limits.record_rate_refusal(
            ("api_key", 51), "t", "rate_limited", "principal", template
        )
        is None
    )
    assert rate_limits._overflow is not None
    assert list(rate_limits._overflow.windows) == [("t", "rate_limited", "principal")]


def test_a_depleted_or_unflushed_entry_is_never_evicted(monkeypatch):
    """A fresh entry starts full, so evicting a depleted one would hand back
    free capacity — idling would become a way to reset a spent bucket."""
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(
        rate_limits, "time", SimpleNamespace(monotonic=lambda: clock.now)
    )
    # A bucket slow enough that idling past the TTL does **not** refill it, so
    # "depleted" and "idle" are separable facts. At the configured 120/min they
    # never are, which is why the guard is stated as an invariant rather than
    # left to the arithmetic.
    monkeypatch.setattr(mcp_auth.settings, "mcp_rate_limit_per_minute", 1)
    monkeypatch.setattr(mcp_auth.settings, "mcp_rate_limit_burst", 100)
    rate_limits.reset_state_for_tests()

    depleted = ("api_key", 1)
    _drain(principal=depleted)
    entry = rate_limits._entries[depleted]

    clock.now += rate_limits.ENTRY_TTL_SECONDS * 10
    assert not entry.idle_and_full(clock.now)
    rate_limits._sweep(clock.now)
    assert depleted in rate_limits._entries, (
        "an idle but depleted entry was reclaimed, which would hand its "
        "principal a fresh full bucket for free"
    )

    # An idle entry holding an unflushed count is not evictable either.
    unflushed = ("api_key", 2)
    rate_limits.take(unflushed, refusals.SCOPE_PRINCIPAL)
    rate_limits.record_rate_refusal(
        unflushed, "t", "rate_limited", "principal", lambda: {"params": {}}
    )
    clock.now += rate_limits.ENTRY_TTL_SECONDS * 10
    assert not rate_limits._entries[unflushed].idle_and_full(clock.now)


def test_a_full_and_idle_entry_is_reclaimed(monkeypatch):
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(
        rate_limits, "time", SimpleNamespace(monotonic=lambda: clock.now)
    )
    principal = ("api_key", 42)
    rate_limits.take(principal, refusals.SCOPE_PRINCIPAL)
    clock.now += rate_limits.ENTRY_TTL_SECONDS * 10
    rate_limits._sweep(clock.now)
    assert principal not in rate_limits._entries


# ── 9. the boot validator ───────────────────────────────────────────────────


def _settings(**kwargs):
    return Settings(secret_key="not-a-placeholder", _env_file=None, **kwargs)


def test_a_half_configured_bucket_refuses_the_boot():
    with pytest.raises(Exception) as exc:
        _settings(mcp_rate_limit_per_minute=None)
    assert "MCP_RATE_LIMIT_PER_MINUTE" in str(exc.value)
    assert "MCP_RATE_LIMIT_BURST" in str(exc.value)

    with pytest.raises(Exception) as exc:
        _settings(mcp_write_rate_limit_burst=None)
    assert "MCP_WRITE_RATE_LIMIT_PER_MINUTE" in str(exc.value)
    assert "MCP_WRITE_RATE_LIMIT_BURST" in str(exc.value)


def test_an_out_of_domain_default_daily_limit_refuses_the_boot():
    with pytest.raises(Exception) as exc:
        _settings(default_daily_request_limit=1_000_001)
    assert "1..1000000" in str(exc.value)


@pytest.mark.parametrize(
    "field",
    [
        "mcp_auth_failure_limit",
        "mcp_auth_failure_window_seconds",
        "mcp_auth_failure_table_size",
        "mcp_rate_limit_per_minute",
        "mcp_rate_limit_burst",
        "mcp_write_rate_limit_per_minute",
        "mcp_write_rate_limit_burst",
        "mcp_limiter_max_tracked_principals",
        "mcp_refusal_log_interval_seconds",
        "default_daily_request_limit",
    ],
)
def test_zero_is_rejected_everywhere(field):
    """A control that refuses every call reads to an operator as an outage,
    not as a setting (#162's reason). Null is the only disable."""
    with pytest.raises(Exception):
        _settings(**{field: 0})


def test_the_daily_limit_domain_is_mirrored_from_the_model():
    """`src/config.py` cannot import `src/models/db.py` (that module imports
    *this* one), so the domain is mirrored — and pinned equal here, the way the
    `usage_stats` / `tools` marker pairs are pinned."""
    import src.config as config
    from src.models.db import DAILY_REQUEST_LIMIT_MAX, DAILY_REQUEST_LIMIT_MIN

    assert config._DAILY_REQUEST_LIMIT_MIN == DAILY_REQUEST_LIMIT_MIN
    assert config._DAILY_REQUEST_LIMIT_MAX == DAILY_REQUEST_LIMIT_MAX
