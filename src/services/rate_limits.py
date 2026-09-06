"""The in-process rate controls for `/mcp`: two per-principal token buckets,
the per-address failed-authentication budget, and the coalescer that keeps the
refusals from becoming the load.

Read `docs/architecture/rate-limits.md` before changing anything here. The four
properties that must survive every edit:

* **No I/O, no lock, no `await` between a read and a write.** A bucket update
  is a synchronous function — read `(tokens, updated)`, compute, write back —
  so on a single-threaded event loop it is atomic *by construction* and needs
  no lock. Nothing in the admitted path issues a statement or checks out a
  session; that is pinned by a statement-counting test.
* **Bounded state, and each registry says how it is bounded.** Addresses are
  unauthenticated and free to mint, so eviction is a losing game: they get a
  **fixed-size table** of counters indexed by a per-process randomly salted
  hash, where memory is O(size), there is nothing to evict, collisions only
  make the control stricter, and nobody can *choose* to collide with a victim.
  Principals are authenticated, so their cardinality is bounded by the
  credentials that exist: a dict with a hard cap and an amortised sweep, past
  which further principals share one overflow entry.
* **An entry is evictable only when it is full and idle.** A fresh entry starts
  full, so evicting a *depleted* one would hand back free capacity — idling
  through the sweep would be a way to reset a spent bucket. An entry holding an
  unflushed pending refusal count must not be evicted either, because that
  count is the only record of refusals no row represents yet.
* **The coalescer owns a complete row.** Its entry captures the whole
  attribution of the row it will write when the window opened, so a deferred
  flush reads **no** request-scoped context variable and depends on **no** live
  credential. By flush time the request is long gone and the key may have been
  deleted; `_log_usage`'s existing foreign-key recovery is what lands the row
  anyway.

**State is in-process and is not persisted**, which is sound only because the
deployment runs exactly one uvicorn worker (`Dockerfile`, `--workers 1`). A
second worker multiplies every rate here by the worker count.
"""
from __future__ import annotations

import hashlib
import math
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from src.config import settings
from src.services.refusals import SCOPE_PRINCIPAL, SCOPE_PRINCIPAL_WRITE

#: A principal is `("api_key", api_keys.id)` or `("oauth", oauth_tokens.grant_id)`.
#: The OAuth half is the **grant** — see `src/auth/session.py`.
Principal = tuple[str, Any]

#: How long an entry may sit untouched before the sweep may reclaim it — and
#: only then if it is also *full* and holds no unflushed count. Five minutes is
#: an arbitrary bound on churn rather than a control: a full, idle entry is
#: behaviourally indistinguishable from the fresh one that would replace it, so
#: when this number is wrong nothing observable changes.
ENTRY_TTL_SECONDS = 300.0

#: Entries examined per insert. The sweep is amortised on insert and bounded
#: per admission — deliberately not a background task, which would be a second
#: thing to start, stop and reason about at shutdown for a dictionary.
SWEEP_SCAN = 32


# ── The bucket ──────────────────────────────────────────────────────────────


class TokenBucket:
    """One principal's allowance for one control. Synchronous, by design.

    `take()` is the whole implementation: refill by the elapsed monotonic time,
    spend one token if there is one, otherwise say how long the caller must
    wait for the next. `time.monotonic()` and not the wall clock, because a
    clock adjustment must not hand out free capacity or refuse a caller for an
    hour.
    """

    __slots__ = ("capacity", "per_second", "tokens", "updated")

    def __init__(self, rate_per_minute: int, burst: int, now: float) -> None:
        self.capacity = float(burst)
        self.per_second = rate_per_minute / 60.0
        # A new principal starts with a full burst. That is also why a depleted
        # bucket may never be evicted: re-creating it would be a refill.
        self.tokens = float(burst)
        self.updated = now

    def take(self, now: float) -> tuple[bool, int]:
        """`(admitted, retry_after_seconds)`. No `await`, no lock, no I/O.

        `retry_after_seconds` is 0 when admitted and a whole number of at least
        one second when refused — a refusal that quotes "retry in 0 seconds"
        invites the tightest possible loop.
        """
        elapsed = now - self.updated
        if elapsed > 0:
            self.tokens = min(self.capacity, self.tokens + elapsed * self.per_second)
            self.updated = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True, 0
        deficit = 1.0 - self.tokens
        return False, max(1, math.ceil(deficit / self.per_second))

    def would_be_full(self, now: float) -> bool:
        """Would this bucket be at capacity if it refilled right now?

        The refill is lazy — it happens inside `take` — so `tokens` alone says
        what the bucket held when it was last *used*, not what it holds. The
        sweep has to ask the second question: an entry whose bucket has since
        refilled to capacity is indistinguishable from the fresh entry that
        would replace it, and is therefore safe to reclaim, while one that is
        still short is not (evicting it would hand back the capacity it spent).
        """
        elapsed = max(0.0, now - self.updated)
        return min(self.capacity, self.tokens + elapsed * self.per_second) >= (
            self.capacity
        )


# ── The coalescer's window ──────────────────────────────────────────────────


@dataclass
class _Window:
    """One `(tool, marker, scope)` coalescing window inside one entry.

    `pending` counts the refusals **no row yet represents**, and every reader
    takes a written row to stand for `1 + suppressed` refusals. The two flush
    paths are deliberately asymmetric and getting that wrong double-counts:
    a rollover has the *arriving* refusal as its row's base, while a standalone
    flush has none and the row must therefore stand for one of the pending
    refusals itself.
    """

    started: float
    pending: int
    #: The complete, immutable attribution of the row this window will write,
    #: captured when the window opened. A flush renders from this alone.
    template: dict


@dataclass
class _Entry:
    """One principal's limiter state — or the shared overflow entry's.

    The coalescer's windows live **inside** the entry, keyed on
    `(tool, marker, scope)`, which is what makes the overflow behaviour fall
    out rather than being a second mechanism: for a principal-keyed entry the
    effective coalescing key is `(principal, tool, marker, scope)`, and for the
    shared overflow entry it is `(tool, marker, scope)` — the principal, and
    only the principal, is dropped.
    """

    general: TokenBucket | None
    write: TokenBucket | None
    last_seen: float
    windows: dict[tuple[str, str, str], _Window] = field(default_factory=dict)
    # Planned rows still belong to this registered entry until acknowledged.
    in_flight: int = 0

    def idle_and_full(self, now: float) -> bool:
        if self.in_flight:
            return False
        if now - self.last_seen < ENTRY_TTL_SECONDS:
            return False
        if any(window.pending for window in self.windows.values()):
            return False
        if self.windows:
            # A window with `pending == 0` is still a promise: its opening row
            # is written, but dropping it early would let the next refusal open
            # a *new* window immediately, writing a second full row inside one
            # interval. Cheap to keep; the flush retires it.
            return False
        for bucket in (self.general, self.write):
            if bucket is not None and not bucket.would_be_full(now):
                return False
        return True


# ── Module state ────────────────────────────────────────────────────────────

_entries: dict[Principal, _Entry] = {}
_overflow: _Entry | None = None

#: The sweep's rotating cursor: a snapshot of the registry's keys, consumed
#: `SWEEP_SCAN` at a time and rebuilt when it empties. See `_sweep`.
_sweep_queue: list[Principal] = []

#: Per-process and random, so that a caller cannot compute which addresses
#: share a slot with a victim's. Never derived from a setting or a secret an
#: operator rotates: the table is meaningless across a restart anyway.
_address_salt: bytes = secrets.token_bytes(16)
_address_table: list["_AddressSlot | None"] = []


@dataclass
class _AddressSlot:
    window_start: float
    count: int
    warned: bool = False


def reset_state_for_tests() -> None:
    """Drop every limiter registry and re-salt the address table.

    In-process state with no persistence has exactly one hazard, and it is a
    test-suite hazard: one test's depleted bucket refusing the next test's
    first call. Production never calls this.
    """
    global _overflow, _address_salt, _address_table, _sweep_queue
    _entries.clear()
    _overflow = None
    _sweep_queue = []
    _address_salt = secrets.token_bytes(16)
    _address_table = []


# ── Configuration ───────────────────────────────────────────────────────────


def _bucket_settings(scope: str) -> tuple[int | None, int | None]:
    if scope == SCOPE_PRINCIPAL:
        return settings.mcp_rate_limit_per_minute, settings.mcp_rate_limit_burst
    if scope == SCOPE_PRINCIPAL_WRITE:
        return (
            settings.mcp_write_rate_limit_per_minute,
            settings.mcp_write_rate_limit_burst,
        )
    raise ValueError(f"unknown bucket scope {scope!r}")


def bucket_limit(scope: str) -> int | None:
    """The configured sustained rate for `scope`, for the refusal's `limit`."""
    return _bucket_settings(scope)[0]


def _new_entry(now: float) -> _Entry:
    """An entry with the buckets configuration says exist, both full.

    A bucket whose rate is null is simply absent, and `take` on an absent
    bucket admits: null is the one representation of "off" (see `config.py`),
    and the settings validator has already refused a half-configured pair, so
    an absent rate here cannot mean a burst was silently ignored.
    """

    def build(scope: str) -> TokenBucket | None:
        rate, burst = _bucket_settings(scope)
        if rate is None or burst is None:
            return None
        return TokenBucket(rate, burst, now)

    return _Entry(
        general=build(SCOPE_PRINCIPAL), write=build(SCOPE_PRINCIPAL_WRITE),
        last_seen=now,
    )


# ── The principal registry ──────────────────────────────────────────────────


def _sweep(now: float) -> None:
    """Reclaim at most `SWEEP_SCAN` full-and-idle entries. Bounded work.

    **A rotating cursor, not a fixed prefix.** The first version scanned the
    first `SWEEP_SCAN` entries in insertion order, which meant that with more
    than `SWEEP_SCAN` live entries the tail of the registry was *never*
    examined: on a deployment sitting just above the scan width, the entries
    inserted last could only ever be reclaimed by a restart, so the registry
    ratcheted towards its cap and the overflow entry with it. The cursor is a
    snapshot of the keys, consumed `SWEEP_SCAN` at a time and rebuilt when it
    empties — one O(n) rebuild per full rotation, so the work per sweep is
    still bounded amortised, and every entry is eventually examined.

    A key that has been deleted or re-created since the snapshot is simply
    looked up again: the queue is a scan order, never a source of truth.
    """
    global _sweep_queue
    if not _sweep_queue:
        _sweep_queue = list(_entries.keys())
    examined = 0
    while _sweep_queue and examined < SWEEP_SCAN:
        key = _sweep_queue.pop()
        examined += 1
        entry = _entries.get(key)
        if entry is not None and entry.idle_and_full(now):
            del _entries[key]


def _entry_for(principal: Principal, now: float) -> _Entry:
    """This principal's entry, creating or overflowing as the cap requires."""
    global _overflow
    entry = _entries.get(principal)
    if entry is not None:
        entry.last_seen = now
        return entry
    _sweep(now)
    if len(_entries) < settings.mcp_limiter_max_tracked_principals:
        entry = _new_entry(now)
        _entries[principal] = entry
        return entry
    # Past the cap. One shared entry rather than fail-open (which lets the
    # flood succeed) or fail-closed (which turns a bookkeeping cap into an
    # outage for a legitimate credential). Its coalescer windows are keyed on
    # `(tool, marker, scope)`, so an overflowed row still names the tool, the
    # marker and the control that fired: only the per-principal attribution is
    # lost, never the count.
    if _overflow is None:
        _overflow = _new_entry(now)
    _overflow.last_seen = now
    return _overflow


def take(principal: Principal | None, scope: str) -> tuple[bool, int]:
    """Spend one token of `scope` for `principal`.

    Returns `(admitted, retry_after_seconds)`. **A caller with no principal is
    exempt, not refused**: sandbox mode short-circuits the middleware and a
    direct in-process caller never passes it, so both read `None` here — the
    same shape as the quota gate's "a limit with no key is exempt rather than a
    crash". Nothing untrusted reaches that path; untrusted traffic arrives
    through the middleware, which binds a principal or answers 401/429.
    """
    if principal is None:
        return True, 0
    rate, burst = _bucket_settings(scope)
    if rate is None or burst is None:
        return True, 0
    now = time.monotonic()
    entry = _entry_for(principal, now)
    bucket = entry.general if scope == SCOPE_PRINCIPAL else entry.write
    if bucket is None:
        # Configuration changed under a live entry (only a test does this).
        return True, 0
    return bucket.take(now)


def tracked_principals() -> int:
    """How many principals hold their own entry. **Tests only.**

    Deliberately not wired to any operator surface: the panel reads durable
    facts out of `usage_logs`, and this number is a property of one process's
    memory that a restart resets to zero. It exists so the registry's cap and
    its overflow behaviour can be asserted rather than inferred.
    """
    return len(_entries)


# ── The refusal coalescer ───────────────────────────────────────────────────


#: The `usage_logs` columns a row written from the **shared overflow** entry
#: must not carry. Past the registry cap many principals coalesce onto one
#: entry keyed `(tool, marker, scope)`, so the row stands for traffic from
#: several credentials at once; stamping it with whichever of them happened to
#: open the window would attribute an aggregate to one member — a false fact
#: about a specific credential, which is worse than the missing attribution the
#: overflow already accepts. The count survives; the name does not.
_ATTRIBUTION_COLUMNS = (
    "user_id",
    "key_id",
    "oauth_token_id",
    "actor_kind",
    "actor_label",
    "actor_ref",
)


#: The `usage_logs.params` key carrying how many refusals a row stands behind.
#: **One definition**, here, because this module is the only writer: `tools.py`
#: re-exports it under its private name for the marker register, and a second
#: literal is how the writer and the reader start disagreeing about a key that
#: is read with a cast.
SUPPRESSED_PARAM = "suppressed"


def _unattributed(template: dict) -> dict:
    """`template` with every attribution column explicitly NULL."""
    stripped = dict(template)
    for column in _ATTRIBUTION_COLUMNS:
        stripped[column] = None
    return stripped


@dataclass
class PlannedRow:
    """One `usage_logs` row the coalescer has decided to write, **not yet
    known durable**.

    This type is the whole of the acknowledgement contract. The coalescer's
    state advances when a row is *planned*, and the count that row carries only
    stops being the coalescer's responsibility when the write is **confirmed**:
    until then a failure has to put the weight back, or the refusals the row
    represents are lost with no trace anywhere — the state advanced, the row
    never landed, and `Σ (1 + suppressed)` silently undercounts.

    `weight` is `1 + suppressed`: the number of observed refusals this row
    stands for, which is exactly what must return to `pending` if it does not
    land.
    """

    #: The complete row, ready for `write_usage_row`.
    values: dict
    #: `1 + suppressed` — the refusals this row represents.
    weight: int
    #: Where to put the weight back, and under which key.
    entry: "_Entry"
    key: tuple[str, str, str]
    #: The template and window start to restore when the window has since been
    #: retired (the flush path deletes it before awaiting).
    template: dict
    started: float


def record_rate_refusal(
    principal: Principal | None,
    tool: str,
    marker: str,
    scope: str,
    template_factory: Callable[[], dict],
) -> PlannedRow | None:
    """Account for one rate refusal.

    Returns the `PlannedRow` the caller must write **and acknowledge** through
    `write_planned_row`, or `None` when this refusal is folded into an open
    window and no statement of any kind may be issued for it.

    * **Window opening.** The first refusal for a key plans its own row with
      `suppressed = 0` and sets `pending = 0`; that row represents exactly
      itself.
    * **Inside an open window.** `pending += 1`, no INSERT and no UPDATE.
    * **Rollover.** A refusal arriving after the window closed plans a row with
      `suppressed = pending` and *itself* as that row's base, then resets.

    `template_factory` is called **only** when a window opens or rolls over, so
    an agent looping inside one window pays neither a statement nor the cost of
    building a row it will not write.

    The row is planned from the **captured template**, never from the request
    context at write time: one code path builds every `rate_limited` row,
    immediate and deferred alike, so a deferred flush cannot be the only path
    anybody has checked.
    """
    now = time.monotonic()
    entry = _entry_for(principal, now) if principal is not None else _entry_for(
        _NO_PRINCIPAL, now
    )
    key = (tool, marker, scope)
    window = entry.windows.get(key)
    interval = settings.mcp_refusal_log_interval_seconds

    def capture() -> dict:
        template = template_factory()
        # The shared overflow entry serves many principals, so its rows are
        # explicitly unattributed rather than stamped with whichever one
        # opened the window.
        return _unattributed(template) if entry is _overflow else template

    if window is None:
        template = capture()
        entry.windows[key] = _Window(started=now, pending=0, template=template)
        return _planned(entry, key, template, now, suppressed=0)
    if now - window.started < interval:
        window.pending += 1
        return None
    suppressed = window.pending
    template = capture()
    window.started = now
    window.pending = 0
    window.template = template
    return _planned(entry, key, template, now, suppressed=suppressed)


def _planned(
    entry: "_Entry",
    key: tuple[str, str, str],
    template: dict,
    started: float,
    *,
    suppressed: int,
) -> PlannedRow:
    """One row, with `suppressed` stamped into a copy of the template."""
    values = dict(template)
    params = dict(values.get("params") or {})
    params[SUPPRESSED_PARAM] = suppressed
    values["params"] = params
    entry.in_flight += 1
    return PlannedRow(
        values=values,
        weight=1 + suppressed,
        entry=entry,
        key=key,
        template=template,
        started=started,
    )


def requeue(planned: PlannedRow) -> None:
    """Put an unwritten row's whole weight back into the coalescer.

    Called when `write_usage_row` answered `False` or raised. The row
    represented `1 + suppressed` observed refusals and none of them is recorded
    anywhere, so all of them go back into `pending` — the count that means
    "refusals no row yet represents", which is precisely what they now are.

    Two shapes, because the two write paths leave the window differently:

    * The window still exists (the immediate path, where planning only reset
      it): add the weight to whatever has accumulated since.
    * The window is gone (the flush path deletes before it awaits, so a
      concurrent refusal cannot be counted on both sides): re-create it with
      the captured template and its **original** start, so the row is due again
      on the very next tick rather than after another whole interval.
    """
    planned.entry.in_flight -= 1
    window = planned.entry.windows.get(planned.key)
    if window is None:
        planned.entry.windows[planned.key] = _Window(
            started=planned.started,
            pending=planned.weight,
            template=planned.template,
        )
        return
    window.pending += planned.weight


async def write_planned_row(planned: PlannedRow) -> bool:
    """Write one planned row, and acknowledge or requeue it.

    Returns whether the row landed. A `False` is not an error the caller has to
    handle — the count is back in the coalescer and the next rollover or tick
    will carry it — which is what makes this safe to call from the request path
    and from housekeeping alike.
    """
    # Deferred import, not a cycle: `tools.py` imports this module at load
    # time, so the reverse edge can only exist inside a function body. The
    # writer lives there because it is the one that already knows how to land
    # a row whose credential has been deleted — the 23503 recovery that clears
    # the foreign keys and keeps the denormalised actor columns (#77), which is
    # exactly the path a deferred flush needs.
    from src.mcp_server.tools import write_usage_row

    try:
        landed = await write_usage_row(planned.values)
    except Exception:  # noqa: BLE001 - a failed row must not fail its caller
        landed = False
    except BaseException:
        # Cancellation must propagate, but the unconfirmed weight survives.
        requeue(planned)
        raise
    if not landed:
        requeue(planned)
    else:
        planned.entry.in_flight -= 1
    return landed


#: The principal a refusal is coalesced under when there is none. Unreachable
#: from the middleware — no principal means every per-principal control admits,
#: so no rate refusal can be produced — and present so the coalescer has a
#: total function rather than a branch nothing exercises.
_NO_PRINCIPAL: Principal = ("none", None)


def _due_rows(now: float, *, every_window: bool = False) -> list[PlannedRow]:
    """Retire the windows that owe a row and plan those rows. Synchronous.

    Retiring before the first `await` is what makes the flush safe against a
    refusal arriving mid-flush: the window is either already gone (and the
    arriving refusal opens a fresh one, planning its own row) or untouched.
    Deciding and then awaiting *before* mutating would let one refusal be
    counted on both sides — and a row that then fails to land is re-created by
    `requeue`, with its original start, so it is due again immediately rather
    than lost.

    `every_window` retires the open ones too. That is the shutdown rule and
    only the shutdown rule: a window that is still open holds refusals no row
    represents, and at shutdown there is no later tick to collect them.
    """
    interval = settings.mcp_refusal_log_interval_seconds
    planned: list[PlannedRow] = []
    entries = list(_entries.values())
    if _overflow is not None:
        entries.append(_overflow)
    for entry in entries:
        for key, window in list(entry.windows.items()):
            if not every_window and now - window.started < interval:
                continue
            del entry.windows[key]
            if window.pending == 0:
                # The refusal that opened this window already has its row.
                # Writing here would count it twice.
                continue
            # A standalone flush has no arriving refusal to serve as the row's
            # base, so this row must stand for one of the pending refusals
            # itself: `pending - 1` are suppressed behind it.
            planned.append(
                _planned(
                    entry,
                    key,
                    window.template,
                    window.started,
                    suppressed=window.pending - 1,
                )
            )
    return planned


async def _write_all(planned: list[PlannedRow]) -> int:
    """Write each planned row, acknowledging or requeueing it. Returns the
    number that landed."""
    written = 0
    for index, row in enumerate(planned):
        try:
            if await write_planned_row(row):
                written += 1
        except BaseException:
            # The active writer restores its own row. Every later row was
            # also retired by _due_rows and has not reached a writer yet.
            for unattempted in planned[index + 1:]:
                requeue(unattempted)
            raise
    return written


async def flush_expired(now: float | None = None) -> int:
    """Write the rows every **closed** coalescing window still owes.

    Driven from the indexer's periodic tick, so a principal that was refused in
    a burst and then went quiet still has its count land — otherwise the last
    window of every burst would wait for the *next* refusal, which by
    definition may never come.

    Returns the number of rows written. Never raises for a row that failed:
    the writer already records that as `usage_log_failed`, the count goes back
    into the coalescer, and the next tick tries again with the exact same
    weight.
    """
    return await _write_all(_due_rows(time.monotonic() if now is None else now))


async def flush_all() -> int:
    """Write what **every** window owes, open or closed. The shutdown flush.

    `flush_expired` deliberately leaves an open window alone: inside its
    interval more refusals may still arrive, and coalescing them is the whole
    point. At shutdown that reasoning inverts — there is no next tick and no
    next refusal, so an open window's pending count is simply lost unless it is
    retired now. Called from the lifespan **before `engine.dispose()`**, while
    a connection is still obtainable.

    A row that fails here is requeued like any other; the process is going away
    and the count goes with it, which is the accepted limitation an abrupt kill
    has anyway.
    """
    return await _write_all(_due_rows(time.monotonic(), every_window=True))


# ── The failed-authentication budget ────────────────────────────────────────


@dataclass(frozen=True)
class AuthBudgetRefusal:
    """What the middleware needs to answer an over-budget address."""

    retry_after_seconds: int
    #: True on the first refusal for this slot in this window, which is the one
    #: that gets a WARNING. Every later refusal in the same window is the same
    #: fact and would be a flood channel of its own.
    first: bool
    limit: int
    window_seconds: int


def _table() -> list["_AddressSlot | None"]:
    global _address_table
    size = max(1, settings.mcp_auth_failure_table_size)
    if len(_address_table) != size:
        _address_table = [None] * size
    return _address_table


def _slot_index(address: str | None, size: int) -> int:
    """Which counter this address is charged to.

    Slot 0 is **reserved** for a request with no resolvable client address.
    Charging those to one shared slot rather than exempting them is the point:
    exempting is a bypass that anyone able to strip the header gets for free.

    The rest are a salted digest modulo the remaining size. Collisions merge
    two addresses into one budget, which only makes the control stricter, and
    the per-process random salt means nobody can choose whom to collide with.
    """
    if not address:
        return 0
    if size <= 1:
        return 0
    digest = hashlib.blake2b(
        address.encode("utf-8", "replace"), key=_address_salt, digest_size=8
    ).digest()
    return 1 + int.from_bytes(digest, "big") % (size - 1)


def check_auth_failures(address: str | None) -> AuthBudgetRefusal | None:
    """Is this address over its failed-authentication budget?

    `None` means carry on. Called **before the credential lookup**, so a
    refused probe costs no database session and no query — bounding the
    database work an unauthenticated caller can force, which is what this
    control is for. It is *not* a defence against credential guessing.

    It has one side effect, and `first` is how the caller sees it: the slot
    records that it has already been warned about, so the middleware emits one
    record per slot per window rather than one per refused request — which
    would be an unbounded channel opened by the control that exists to bound
    one.

    The threshold is inclusive on the recorded count — with a limit of 60 the
    61st failure is the first refused — and a refused request does **not**
    increment, because it never reached authentication. Both halves of that
    arithmetic live here, in one helper, so they cannot drift.
    """
    limit = settings.mcp_auth_failure_limit
    if limit is None:
        return None
    window = settings.mcp_auth_failure_window_seconds
    table = _table()
    slot = table[_slot_index(address, len(table))]
    if slot is None:
        return None
    now = time.monotonic()
    elapsed = now - slot.window_start
    if elapsed >= window:
        return None
    if slot.count < limit:
        return None
    first = not slot.warned
    slot.warned = True
    return AuthBudgetRefusal(
        retry_after_seconds=max(1, math.ceil(window - elapsed)),
        first=first,
        limit=limit,
        window_seconds=window,
    )


def record_auth_failure(address: str | None) -> None:
    """Charge one authentication failure to this address's slot.

    Called from `_emit_auth_failure`, which **every** 401 branch of the
    middleware goes through — missing bearer, unknown credential, ownerless,
    inactive user, expired, cross-user grant, missing vault scope. A prober
    picks the cheapest branch, so a budget that covered six of seven would
    bound nothing.
    """
    if settings.mcp_auth_failure_limit is None:
        return
    window = settings.mcp_auth_failure_window_seconds
    table = _table()
    index = _slot_index(address, len(table))
    slot = table[index]
    now = time.monotonic()
    if slot is None or now - slot.window_start >= window:
        table[index] = _AddressSlot(window_start=now, count=1)
        return
    slot.count += 1
