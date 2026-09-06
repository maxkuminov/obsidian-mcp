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


@tools._tracked("rate_probe", ["value"], resource_class="other")
async def _probe(value: str = "x") -> str:
    return f"ran:{value}"


@tools._tracked("rate_probe_write", ["value"], write_class=True, resource_class="other")
async def _write_probe(value: str = "x") -> str:
    return f"wrote:{value}"


@tools._tracked(
    "rate_probe_structured", ["path"], refusal_result=tools._read_note_refusal, resource_class="other"
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
    writes_fail=False,
    **kwargs,
):
    """Call a `_tracked` function with a bound principal. Returns the result.

    `rows` collects every `usage_logs` row the call writes, so "no statement of
    any kind" is checked by its emptiness rather than by inspection. **Both**
    writers are captured: the decorator's own tail (`_log_usage`) and the
    coalescer's (`write_usage_row`), which is the one every `rate_limited` row
    goes through — immediate and deferred alike.

    `writes_fail` makes the coalescer's writer answer `False`, which is how the
    acknowledgement contract is exercised: a row that does not land must put
    its whole weight back rather than disappear between an advanced counter and
    a failed insert.
    """
    rows = [] if rows is None else rows
    quota_spy = quota_spy or _QuotaSpySession()

    async def fake_log_usage(tool, params, duration_ms, response_size):
        rows.append({"tool": tool, "params": params})
        return True

    async def fake_write_usage_row(values):
        if writes_fail:
            return False
        rows.append({"tool": values.get("tool"), "params": values.get("params"), **values})
        return True

    mp = pytest.MonkeyPatch()
    mp.setattr(tools, "_log_usage", fake_log_usage)
    mp.setattr(tools, "write_usage_row", fake_write_usage_row)
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
    source = inspect.getsource(mcp_auth.APIKeyMiddleware)
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


def _refuse(times=1, rows=None, tool=_probe, user_id=None, writes_fail=False):
    rows = [] if rows is None else rows
    out = []
    for _ in range(times):
        _drain()
        out.append(
            _run(tool, rows=rows, user_id=user_id, writes_fail=writes_fail)
        )
    return rows, out


def _weight(row) -> int:
    """What a written row stands for: `1 + suppressed`."""
    return 1 + row["params"][tools._SUPPRESSED_PARAM]


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


def _flush(*, fails=False, every_window=False):
    """Run a flush with the row writer captured. Returns the rows it wrote.

    `fails` makes every write answer `False`, which must leave the coalescer
    holding the exact same weight for the next attempt rather than dropping it.
    """
    written = []

    async def fake_write(values):
        if fails:
            return False
        written.append(values)
        return True

    mp = pytest.MonkeyPatch()
    mp.setattr(tools, "write_usage_row", fake_write)
    try:
        asyncio.run(
            rate_limits.flush_all() if every_window else rate_limits.flush_expired()
        )
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


# ── 6b. the acknowledgement contract ───────────────────────────────────────
#
# The coalescer's state advances when a row is *planned*. That is the only
# workable order — the alternative is holding a lock across a database write on
# the hottest path in the server — but it means the count a planned row carries
# is in flight, owned by nobody, until the write is confirmed. Before the
# acknowledgement existed, a write that answered `False` (the credential was
# deleted mid-call, the pool was exhausted, the row was rejected) left the
# window already advanced and the row never written: `1 + suppressed` observed
# refusals disappeared with no trace on any surface, and `Σ (1 + suppressed)`
# undercounted silently — the one thing this arithmetic exists to make exact.


def test_a_failed_opening_write_is_not_lost(monkeypatch):
    """The opening row fails, one more refusal folds in, and the later flush
    represents **both** — the failed row's weight went back into `pending`."""
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(
        rate_limits, "time", SimpleNamespace(monotonic=lambda: clock.now)
    )
    rows, _ = _refuse(times=1, writes_fail=True)
    assert rows == [], "the write was made to fail"

    _refuse(times=1, rows=rows)  # a second refusal, inside the same window
    assert rows == [], "still inside the window: no statement"

    clock.now += 100
    flushed = _flush()
    assert len(flushed) == 1
    assert flushed[0]["params"][tools._SUPPRESSED_PARAM] == 1
    assert sum(_weight(row) for row in flushed) == 2


def test_a_failed_opening_write_alone_is_still_represented(monkeypatch):
    """No second refusal: the requeued weight is one, and the flush writes a
    row for exactly one refusal rather than nothing."""
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(
        rate_limits, "time", SimpleNamespace(monotonic=lambda: clock.now)
    )
    rows, _ = _refuse(times=1, writes_fail=True)
    assert rows == []
    clock.now += 100
    flushed = _flush()
    assert len(flushed) == 1
    assert flushed[0]["params"][tools._SUPPRESSED_PARAM] == 0
    assert sum(_weight(row) for row in flushed) == 1


def test_a_failed_rollover_write_keeps_the_whole_weight(monkeypatch):
    """A rollover row stands for `1 + pending` refusals. When it does not land,
    all of them go back — losing them would be the largest single undercount
    the coalescer can produce."""
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(
        rate_limits, "time", SimpleNamespace(monotonic=lambda: clock.now)
    )
    rows, _ = _refuse(times=5)              # opening row + 4 pending
    assert len(rows) == 1
    clock.now += 100
    _refuse(times=1, rows=rows, writes_fail=True)  # the rollover row fails
    assert len(rows) == 1, "the rollover write was made to fail"

    clock.now += 100
    flushed = _flush()
    assert len(flushed) == 1
    # The rollover would have carried `suppressed = 4` (weight 5); all five go
    # back, so the flush row stands for five and the total is six.
    assert flushed[0]["params"][tools._SUPPRESSED_PARAM] == 4
    assert sum(_weight(row) for row in rows + flushed) == 6


def test_a_failed_flush_is_retried_on_the_next_tick_with_the_exact_count(
    monkeypatch,
):
    """A closed window whose flush fails is due again **immediately**, not
    after another interval, and carries the same weight it did."""
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(
        rate_limits, "time", SimpleNamespace(monotonic=lambda: clock.now)
    )
    rows, _ = _refuse(times=5)
    clock.now += 100

    assert _flush(fails=True) == []
    # The very next tick, with no time passing at all, finds it due again.
    flushed = _flush()
    assert len(flushed) == 1
    assert flushed[0]["params"][tools._SUPPRESSED_PARAM] == 3
    assert sum(_weight(row) for row in rows + flushed) == 5


def test_a_write_that_raises_is_requeued_like_one_that_returns_false(
    monkeypatch,
):
    """`write_usage_row` is best-effort and does not raise today. If it ever
    does — a pool that is gone, a driver that throws before its own handler —
    the count must survive that too, because an exception is not evidence the
    row landed."""
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(
        rate_limits, "time", SimpleNamespace(monotonic=lambda: clock.now)
    )
    _refuse(times=3)
    clock.now += 100

    async def exploding_write(_values):
        raise RuntimeError("the pool is gone")

    mp = pytest.MonkeyPatch()
    mp.setattr(tools, "write_usage_row", exploding_write)
    try:
        assert asyncio.run(rate_limits.flush_expired()) == 0
    finally:
        mp.undo()

    flushed = _flush()
    assert len(flushed) == 1
    assert flushed[0]["params"][tools._SUPPRESSED_PARAM] == 1


def test_a_requeued_flush_row_merges_with_refusals_that_arrived_meanwhile(
    monkeypatch,
):
    """The flush retires the window before it awaits, so a refusal arriving
    mid-flush opens a fresh one. Requeueing must then *add* to that window
    rather than overwrite it — otherwise the arriving refusal, or the failed
    row, is lost depending on the order."""
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(
        rate_limits, "time", SimpleNamespace(monotonic=lambda: clock.now)
    )
    rows, _ = _refuse(times=3)          # opening row + 2 pending
    clock.now += 100

    planned = rate_limits._due_rows(clock.now)
    assert len(planned) == 1 and planned[0].weight == 2

    # A refusal lands while the row is "in flight": a new window opens and
    # writes its own row.
    _refuse(times=1, rows=rows)
    assert len(rows) == 2

    rate_limits.requeue(planned[0])
    clock.now += 100
    flushed = _flush()
    assert len(flushed) == 1
    assert sum(_weight(row) for row in rows + flushed) == 4


def test_the_immediate_row_is_written_from_the_captured_template():
    """One code path builds every `rate_limited` row. The immediate one used to
    be assembled in the decorator's tail from the request context instead,
    which meant the deferred path — the one with no request context at all —
    was the only one anybody had exercised end to end."""
    rows, _ = _refuse(times=1, user_id=1)
    assert len(rows) == 1
    # Template-shaped: the full row values, not just `(tool, params)`.
    assert rows[0]["duration_ms"] == 0
    assert rows[0]["response_size"] == 0
    assert rows[0]["tool"] == "rate_probe"
    assert rows[0]["params"][tools._SUPPRESSED_PARAM] == 0


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


@tools._tracked("rate_probe_hostile", ["value"], transforms={"value": lambda v: 1 / 0}, resource_class="other")
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


def _drive_one_tick(monkeypatch, *, paused=False, index_raises=False, flush_raises=False):
    """Run exactly one iteration of the periodic loop. Returns the flush count.

    The startup half runs first and is neutered; the tick then takes whichever
    path the arguments ask for, and the second `sleep` cancels the loop.
    """
    from src.services import indexer

    calls = {"flush": 0}

    async def _noop(*_a, **_k):
        return None

    async def _index(*_a, **_k):
        if index_raises:
            raise RuntimeError("the pass blew up")
        return (0, 0)

    async def _flush_counter(*_a, **_k):
        calls["flush"] += 1
        if flush_raises:
            raise RuntimeError("the flush itself failed")
        return 0

    sleeps = {"n": 0}

    async def _sleep(*_a, **_k):
        sleeps["n"] += 1
        if sleeps["n"] > 1:
            raise asyncio.CancelledError

    class _FakeSession:
        async def execute(self, *_a, **_k):
            return None

        async def commit(self):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

    monkeypatch.setattr(indexer, "async_session", lambda: _FakeSession())
    monkeypatch.setattr(indexer.settings, "multi_user_mode", False)
    monkeypatch.setattr(indexer.settings, "index_interval_seconds", 0)
    monkeypatch.setattr(indexer, "_is_paused", lambda: paused)
    monkeypatch.setattr(indexer, "detect_root_overlaps", _noop)
    monkeypatch.setattr(indexer, "record_quarantined_runs", _noop)
    monkeypatch.setattr(indexer, "link_backfill_pass", _noop)
    monkeypatch.setattr(indexer, "prewarm_search_caches", _noop)
    monkeypatch.setattr(indexer, "cleanup_expired_tokens", _noop)
    monkeypatch.setattr(indexer, "embed_vault", _noop)
    monkeypatch.setattr(indexer, "index_vault", _index)
    monkeypatch.setattr(indexer, "flush_expired", _flush_counter)
    monkeypatch.setattr(indexer.asyncio, "sleep", _sleep)

    try:
        asyncio.run(indexer.run_indexer_loop())
    except asyncio.CancelledError:
        pass
    return calls["flush"]


def test_the_indexer_tick_flushes_on_a_healthy_pass(monkeypatch):
    assert _drive_one_tick(monkeypatch) == 1


def test_a_paused_tick_still_flushes(monkeypatch):
    """A pause suppresses index and embed work. It must not suppress the
    coalescer's flush: the flush used to sit after `cleanup_expired_tokens()`,
    which a paused tick `continue`s straight past, so a deployment left paused
    accumulated refusal counts in memory and wrote them only if it happened to
    shut down cleanly — and a pause is entered precisely when an operator is
    doing something they will want the log of."""
    assert _drive_one_tick(monkeypatch, paused=True) == 1


def test_a_failing_tick_still_flushes(monkeypatch):
    """Same for the failure branch, which jumps past the old position into the
    exception handler. A failing indexer is the other state an operator reads
    `/admin/performance` in."""
    assert _drive_one_tick(monkeypatch, index_raises=True) == 1


def test_the_flush_failure_is_swallowed_by_the_tick(monkeypatch):
    """Housekeeping may never fail a pass — the `quota_counters` prune
    precedent."""
    from src.services import indexer

    # Reaching the end of the loop without the tick raising is the assertion:
    # the flush was attempted, it blew up, and the loop carried on.
    assert _drive_one_tick(monkeypatch, flush_raises=True) == 1

    source = inspect.getsource(indexer.run_indexer_loop)
    guarded = source[source.index("await flush_expired()") :]
    assert "except Exception" in guarded[:400]


def test_the_shutdown_flush_retires_the_open_window_too(monkeypatch):
    """`flush_expired` deliberately leaves an **open** window alone — inside its
    interval more refusals may arrive and coalescing them is the point. At
    shutdown that reasoning inverts: there is no next tick and no next refusal,
    so an open window's pending count is lost unless it is retired now. Using
    the periodic flush at shutdown dropped the current interval on every clean
    restart.
    """
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(
        rate_limits, "time", SimpleNamespace(monotonic=lambda: clock.now)
    )
    rows, _ = _refuse(times=4)  # one row, then three folded into the open window

    # The window is still open, so the periodic flush writes nothing…
    assert _flush() == []
    # …and the shutdown flush writes the row those three refusals are owed.
    flushed = _flush(every_window=True)
    assert len(flushed) == 1
    assert flushed[0]["params"][tools._SUPPRESSED_PARAM] == 2
    assert sum(_weight(row) for row in rows + flushed) == 4


def test_the_lifespan_shutdown_calls_the_all_windows_flush():
    """The wiring, since the behaviour above cannot see which one main calls."""
    from src import main

    source = inspect.getsource(main.lifespan)
    assert source.index("await flush_all()") < source.index(
        "await engine.dispose()"
    )
    assert "await flush_expired()" not in source


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

    def template():
        return {
            "tool": "t",
            "params": {},
            "user_id": 4,
            "key_id": 50,
            "oauth_token_id": None,
            "actor_kind": "api_key",
            "actor_label": "one member of the overflow",
            "actor_ref": "omcp_aaaa",
        }

    planned = rate_limits.record_rate_refusal(
        ("api_key", 50), "t", "rate_limited", "principal", template
    )
    assert planned is not None and planned.weight == 1
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


def test_an_overflow_row_is_written_unattributed(monkeypatch):
    """Past the cap the entry is **shared**, so its row stands for traffic from
    several credentials at once. Stamping it with whichever member happened to
    open the window would attribute an aggregate to one specific credential —
    a false fact about a named key, which is worse than the missing attribution
    the overflow already accepts. The count survives; the name does not.
    """
    monkeypatch.setattr(mcp_auth.settings, "mcp_limiter_max_tracked_principals", 1)
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(
        rate_limits, "time", SimpleNamespace(monotonic=lambda: clock.now)
    )
    rate_limits.reset_state_for_tests()
    rate_limits.take(("api_key", 1), refusals.SCOPE_PRINCIPAL)  # fills the cap

    def template():
        return {
            "tool": "t",
            "params": {"error": "rate_limited"},
            "user_id": 4,
            "key_id": 50,
            "oauth_token_id": None,
            "actor_kind": "api_key",
            "actor_label": "one member of the overflow",
            "actor_ref": "omcp_aaaa",
        }

    planned = rate_limits.record_rate_refusal(
        ("api_key", 50), "t", "rate_limited", "principal", template
    )
    assert planned is not None
    for column in rate_limits._ATTRIBUTION_COLUMNS:
        assert planned.values[column] is None, column
    # The row still names what fired, which is the whole point of keeping it.
    assert planned.values["tool"] == "t"
    assert planned.values["params"]["error"] == "rate_limited"

    # And the deferred half is unattributed too: a second refusal folds in, the
    # window closes, and the flushed row carries the same nulls.
    rate_limits.record_rate_refusal(
        ("api_key", 51), "t", "rate_limited", "principal", template
    )
    clock.now += 100
    flushed = _flush()
    assert len(flushed) == 1
    for column in rate_limits._ATTRIBUTION_COLUMNS:
        assert flushed[0][column] is None, column


def test_an_in_cap_principals_row_keeps_its_attribution():
    """The counterpart: below the cap the entry belongs to one principal, so
    its row carries the whole `usage_logs` attribution as it always did."""
    rows, _ = _refuse(times=1, user_id=1)
    assert len(rows) == 1
    assert rows[0]["user_id"] == 1
    assert rows[0]["key_id"] == 7
    assert rows[0]["actor_kind"] == "api_key"
    assert rows[0]["actor_label"] == "probe key"


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


def test_the_sweep_eventually_examines_every_entry(monkeypatch):
    """A fixed insertion-order prefix never reaches the tail of the registry.

    With `SWEEP_SCAN` = 32 and 33 live entries, the 33rd could only ever be
    reclaimed by a restart: every sweep re-examined the same first 32, so a
    deployment sitting just above the scan width ratcheted towards its cap and
    into the shared overflow entry, permanently. The rotating cursor is what
    makes "bounded work per admission" and "every entry is eventually
    examined" both true.
    """
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(
        rate_limits, "time", SimpleNamespace(monotonic=lambda: clock.now)
    )
    monkeypatch.setattr(
        mcp_auth.settings, "mcp_limiter_max_tracked_principals", 1_000
    )
    rate_limits.reset_state_for_tests()

    population = rate_limits.SWEEP_SCAN + 1
    for index in range(population):
        rate_limits.take(("api_key", index), refusals.SCOPE_PRINCIPAL)
    assert rate_limits.tracked_principals() == population

    # Every entry is now idle and refilled. Sweeping repeatedly must reclaim
    # all of them, the last-inserted included.
    clock.now += rate_limits.ENTRY_TTL_SECONDS * 10
    for _ in range(10):
        rate_limits._sweep(clock.now)
        if not rate_limits.tracked_principals():
            break
    assert rate_limits.tracked_principals() == 0


def test_the_sweep_does_a_bounded_amount_of_work_per_call(monkeypatch):
    """The cursor is a rotation, not a full scan: one call examines at most
    `SWEEP_SCAN` entries, so the amortised cost per admission stays bounded."""
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(
        rate_limits, "time", SimpleNamespace(monotonic=lambda: clock.now)
    )
    monkeypatch.setattr(
        mcp_auth.settings, "mcp_limiter_max_tracked_principals", 1_000
    )
    rate_limits.reset_state_for_tests()
    for index in range(rate_limits.SWEEP_SCAN * 3):
        rate_limits.take(("api_key", index), refusals.SCOPE_PRINCIPAL)

    clock.now += rate_limits.ENTRY_TTL_SECONDS * 10
    before = rate_limits.tracked_principals()
    rate_limits._sweep(clock.now)
    assert before - rate_limits.tracked_principals() <= rate_limits.SWEEP_SCAN


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


@pytest.mark.parametrize("flush", [False, True])
async def test_cancelled_writer_preserves_current_and_unattempted_counts(monkeypatch, flush):
    """A cancelled periodic flush must leave shutdown enough state to recover."""
    rate_limits.reset_state_for_tests()
    rows = []

    async def persist(values):
        rows.append(values)
        return True

    monkeypatch.setattr(tools, "write_usage_row", persist)

    def refuse(principal):
        return rate_limits.record_rate_refusal(
            principal, "read_note", "rate_limited", "principal", lambda: {"params": {}}
        )

    principals = [("api_key", 1), ("api_key", 2)]
    if flush:
        for principal in principals:
            await rate_limits.write_planned_row(refuse(principal))
            assert refuse(principal) is None
        operation = rate_limits.flush_all()
        expected = 4
    else:
        operation = rate_limits.write_planned_row(refuse(principals[0]))
        expected = 1

    entered = asyncio.Event()

    async def blocked(values):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(tools, "write_usage_row", blocked)
    task = asyncio.create_task(operation)
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert all(entry.in_flight == 0 for entry in rate_limits._entries.values())
    monkeypatch.setattr(tools, "write_usage_row", persist)
    await rate_limits.flush_all()
    assert sum(_weight(row) for row in rows) == expected
    assert await rate_limits.flush_all() == 0


@pytest.mark.parametrize("landed", [False, True])
async def test_in_flight_flush_pins_idle_entry_without_exceeding_cap(monkeypatch, landed):
    rate_limits.reset_state_for_tests()
    monkeypatch.setattr(rate_limits.settings, "mcp_limiter_max_tracked_principals", 1)
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(rate_limits, "time", SimpleNamespace(monotonic=lambda: clock.now))
    rows = []

    async def persist(values):
        rows.append(values)
        return True

    monkeypatch.setattr(tools, "write_usage_row", persist)
    principal = ("api_key", 1)

    def refuse():
        return rate_limits.record_rate_refusal(
            principal, "read_note", "rate_limited", "principal", lambda: {"params": {}}
        )

    await rate_limits.write_planned_row(refuse())
    assert refuse() is None
    entry = rate_limits._entries[principal]
    clock.now += rate_limits.ENTRY_TTL_SECONDS + 1

    async def write_during_admission(values):
        assert not entry.windows  # retired, but still owns this row
        assert entry.in_flight == 1
        rate_limits.take(("api_key", 2), refusals.SCOPE_PRINCIPAL)
        assert rate_limits._entries[principal] is entry
        assert rate_limits.tracked_principals() == 1
        assert rate_limits._overflow is not None
        if landed:
            rows.append(values)
        return landed

    monkeypatch.setattr(tools, "write_usage_row", write_during_admission)
    await rate_limits.flush_expired()
    assert entry.in_flight == 0
    monkeypatch.setattr(tools, "write_usage_row", persist)
    await rate_limits.flush_all()
    assert sum(_weight(row) for row in rows) == 2
    rate_limits._sweep(clock.now)
    assert principal not in rate_limits._entries
