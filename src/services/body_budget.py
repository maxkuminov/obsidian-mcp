"""The process-wide `/mcp` body-memory budget (#322).

`/mcp` accepts bodies up to `Settings.mcp_max_request_body_bytes` (61 MiB by
default) *per request*, and the SDK copies every body several times before any
tool gate runs (≈ 6.7× peak RSS per body byte, measured). Nothing bounded the
bytes in flight *across* requests, so a handful of near-limit envelopes could
OOM the single worker. This module is that bound.

- **Always on.** It ignores `MCP_CONCURRENCY_MODE`: no shadow mode, no off
  switch. It is a memory-safety bound, not a tuning control (design D1).
- **Derived, not guessed** (D2). `derive_body_budget()` turns the process's
  own cgroup memory limit into raw-byte lanes; `configure()` checks the result
  and installs it. Both run in the web application's lifespan, **never** in
  `Settings` construction or on `import src.config`: the migration init
  container imports the settings under its own, smaller limit and must not be
  refused for it. This module therefore imports nothing from `src.config` at
  import time.
- **Two lanes** (D5). Requests declaring at most 1 MiB use a reserved small
  lane, and borrow large-lane bytes only while no large request waits. Large
  requests use the large lane in strict FIFO order, with no barging, so a
  maximum-size write cannot starve.
- **Bounded waiting** (D6). A request that does not fit waits FIFO for at most
  `MCP_BODY_BUDGET_WAIT_SECONDS`, with at most `MCP_BODY_BUDGET_WAITERS`
  waiters across both lanes, then is refused. Disconnect-awareness is the
  caller's `disconnected` event (the middleware's `ReceiveWatch`).
- **Release on every exit** (D7). A lease's `release()` is idempotent, and a
  waiter that is cancelled, times out or disconnects hands back any grant that
  raced its exit.

Single event loop, single worker (`--workers 1`): no lock is needed, and no
`await` sits inside an accounting transition.
"""
from __future__ import annotations

import asyncio
import logging
import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

MiB = 1024 * 1024
GiB = 1024 * MiB

#: A request declaring at most this many bytes is *small* (D5). Equal to
#: `src.config._MCP_ENVELOPE_ALLOWANCE_BYTES`; a test pins the equality,
#: because importing `src.config` here would construct `Settings` on import.
SMALL_REQUEST_MAX_BYTES = 1 * MiB

#: The memory budget when no cgroup limit is readable (D2).
FALLBACK_MEMORY_BUDGET_BYTES = 1 * GiB

#: Fixed non-body headroom kept free of any budget: the 378 MiB observed peak
#: baseline, rounded up. The replay budget is added on top (D2).
FIXED_HEADROOM_BYTES = 384 * MiB

#: The largest share of a readable limit any budget may take (D2).
SAFE_FRACTION = 0.5

#: The small lane is `capacity // SMALL_LANE_DIVISOR` (D2).
SMALL_LANE_DIVISOR = 8

#: cgroup v1 reports "unlimited" as a value near 2⁶³; anything at or above
#: this is no limit.
UNLIMITED_THRESHOLD = 1 << 60

#: The settings every boot-check error names, in this order (D2).
BOOT_CHECK_SETTINGS = (
    "MCP_BODY_MEMORY_BUDGET_BYTES",
    "MCP_BODY_MEMORY_FRACTION",
    "MCP_BODY_MEMORY_MULTIPLIER",
    "MAX_FILE_WRITE_BYTES",
)


class BodyBudgetConfigError(ValueError):
    """The derived budget cannot admit what the server promises (D2)."""


# ── the cgroup reader (D2, Codex spec review finding 5) ────────────────────

_OCTAL_ESCAPE = re.compile(r"\\([0-7]{3})")


def _unescape(field_: str) -> str:
    """mountinfo escapes space, tab, newline and backslash as `\\ooo`."""
    return _OCTAL_ESCAPE.sub(lambda m: chr(int(m.group(1), 8)), field_)


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text()
    except (OSError, UnicodeDecodeError, ValueError):
        return None


def _read_limit(path: Path) -> int | None:
    """One limit file: a finite positive byte count, or None.

    `max` (v2's unlimited), an unreadable or unparseable file, and a value at
    or above 2⁶⁰ (v1's unlimited) are all "no limit".
    """
    text = _read_text(path)
    if text is None:
        return None
    text = text.strip()
    if not text.isascii() or not text.isdigit():
        return None
    value = int(text)
    if value <= 0 or value >= UNLIMITED_THRESHOLD:
        return None
    return value


def _mounts(root: Path) -> list[tuple[str, str, str, str]] | None:
    """`(fstype, mount_root, mount_point, super_options)` per mountinfo line."""
    text = _read_text(root / "proc/self/mountinfo")
    if text is None:
        return None
    mounts = []
    for line in text.splitlines():
        fields = line.split()
        try:
            sep = fields.index("-")
            mount_root, mount_point = _unescape(fields[3]), _unescape(fields[4])
            fstype = fields[sep + 1]
            super_options = fields[sep + 3] if len(fields) > sep + 3 else ""
        except (ValueError, IndexError):
            continue
        mounts.append((fstype, mount_root, mount_point, super_options))
    return mounts


def _proc_cgroups(root: Path) -> tuple[str | None, str | None]:
    """`(v2 path, v1 memory path)` from `/proc/self/cgroup`."""
    text = _read_text(root / "proc/self/cgroup")
    v2 = v1_memory = None
    if text is None:
        return None, None
    for line in text.splitlines():
        parts = line.split(":", 2)
        if len(parts) != 3:
            continue
        hierarchy, controllers, path = parts
        if hierarchy == "0" and controllers == "":
            v2 = path
        elif "memory" in controllers.split(","):
            v1_memory = path
    return v2, v1_memory


def _relative_parts(path: str, mount_root: str) -> list[str]:
    """`path` below `mount_root`, as components; [] when it cannot be placed."""
    if mount_root in ("", "/"):
        rel = path
    elif path == mount_root or path.startswith(mount_root.rstrip("/") + "/"):
        rel = path[len(mount_root.rstrip("/")):]
    else:
        return []
    parts = [p for p in rel.split("/") if p]
    if any(p in (".", "..") for p in parts):
        return []
    return parts


def _read_v2(root: Path, mounts) -> int | None:
    if mounts is None:
        v2_mount = ("/", "/sys/fs/cgroup")
    else:
        v2_mount = next(((r, p) for fs, r, p, _ in mounts if fs == "cgroup2"), None)
        if v2_mount is None:
            return None
    mount_root, mount_point = v2_mount
    path, _ = _proc_cgroups(root)
    parts = _relative_parts(path, mount_root) if path is not None else []
    base = root / mount_point.lstrip("/")
    limits = []
    # The cgroup's own directory and every ancestor up to the mount point;
    # the smallest finite `memory.max` on that chain is what applies.
    for depth in range(len(parts), -1, -1):
        value = _read_limit(base.joinpath(*parts[:depth], "memory.max"))
        if value is not None:
            limits.append(value)
    return min(limits) if limits else None


def _read_v1(root: Path, mounts) -> int | None:
    if mounts is None:
        mount_point = "/sys/fs/cgroup/memory"
    else:
        mount_point = next(
            (p for fs, _, p, opts in mounts
             if fs == "cgroup" and "memory" in opts.split(",")),
            None,
        )
        if mount_point is None:
            return None
    # The controller's mount root only (L10): in a container that root is
    # the container's own cgroup.
    return _read_limit(root / mount_point.lstrip("/") / "memory.limit_in_bytes")


def read_cgroup_memory_limit(root: str | Path = "/") -> int | None:
    """The process's effective cgroup memory limit in bytes, or None.

    cgroup v2 first: the process's own path from `/proc/self/cgroup`, placed
    under the `cgroup2` mount from `/proc/self/mountinfo`, and the minimum
    finite `memory.max` from that directory up to the mount point. Only when
    v2 yields nothing (pure v1 or hybrid), v1's `memory.limit_in_bytes` at the
    memory controller's mount root. `root` exists for tests (a fake tree).
    """
    root = Path(root)
    mounts = _mounts(root)
    value = _read_v2(root, mounts)
    if value is not None:
        return value
    return _read_v1(root, mounts)


# ── the derivation (D2) ────────────────────────────────────────────────────

@dataclass(frozen=True)
class BodyBudgetPlan:
    """The resolved derivation, logged once at startup."""
    source: str  # "setting" | "cgroup" | "fallback"
    memory_budget: int
    multiplier: int
    capacity: int
    small_lane: int
    large_lane: int
    cgroup_limit: int | None
    safe_allocation: int | None
    max_request_body: int


def safe_allocation(limit: int, replay_budget_bytes: int) -> int:
    """The largest memory budget a readable limit `limit` may carry (D2)."""
    return min(int(limit * SAFE_FRACTION),
               limit - (FIXED_HEADROOM_BYTES + int(replay_budget_bytes)))


def derive_body_budget(settings, cgroup_reader: Callable[[], int | None] | None = None
                       ) -> BodyBudgetPlan:
    """Pure derivation: settings plus the cgroup limit → lanes. Never raises
    for a too-small result; `check_plan` decides that."""
    reader = cgroup_reader if cgroup_reader is not None else read_cgroup_memory_limit
    try:
        limit = reader()
    except Exception:  # noqa: BLE001 - an unreadable limit is "no limit"
        limit = None
    explicit = settings.mcp_body_memory_budget_bytes
    if explicit is not None:
        source, memory_budget = "setting", int(explicit)
    elif limit is not None:
        source = "cgroup"
        memory_budget = int(settings.mcp_body_memory_fraction * limit)
    else:
        source, memory_budget = "fallback", FALLBACK_MEMORY_BUDGET_BYTES
    multiplier = int(settings.mcp_body_memory_multiplier)
    capacity = memory_budget // multiplier
    small_lane = capacity // SMALL_LANE_DIVISOR
    return BodyBudgetPlan(
        source=source,
        memory_budget=memory_budget,
        multiplier=multiplier,
        capacity=capacity,
        small_lane=small_lane,
        large_lane=capacity - small_lane,
        cgroup_limit=limit,
        safe_allocation=(None if limit is None else
                         safe_allocation(limit, settings.mcp_concurrency_replay_budget_bytes)),
        max_request_body=int(settings.mcp_max_request_body_bytes),
    )


def plan_problems(plan: BodyBudgetPlan) -> list[str]:
    """Every reason `plan` must not be served; empty when it is sound."""
    problems = []
    if plan.large_lane < plan.max_request_body:
        problems.append(
            f"the large lane ({plan.large_lane} bytes) cannot hold one maximum "
            f"request body ({plan.max_request_body} bytes), so a supported "
            "maximum write could never be admitted")
    if plan.small_lane < SMALL_REQUEST_MAX_BYTES:
        problems.append(
            f"the small lane ({plan.small_lane} bytes) cannot hold one "
            f"{SMALL_REQUEST_MAX_BYTES}-byte envelope")
    if plan.safe_allocation is not None and plan.memory_budget > plan.safe_allocation:
        problems.append(
            f"the memory budget ({plan.memory_budget} bytes) exceeds the safe "
            f"allocation ({plan.safe_allocation} bytes) of the cgroup limit "
            f"({plan.cgroup_limit} bytes): min(0.5 x limit, limit - "
            f"({FIXED_HEADROOM_BYTES} + MCP_CONCURRENCY_REPLAY_BUDGET_BYTES))")
    return problems


def check_plan(plan: BodyBudgetPlan) -> None:
    """The boot check (D2). Raises naming the four settings."""
    problems = plan_problems(plan)
    if problems:
        raise BodyBudgetConfigError(
            "The /mcp body-memory budget is unusable: " + "; ".join(problems)
            + f". Derivation: source={plan.source} memory_budget={plan.memory_budget} "
            f"multiplier={plan.multiplier} capacity={plan.capacity}. Adjust "
            + ", ".join(BOOT_CHECK_SETTINGS[:-1]) + f" or {BOOT_CHECK_SETTINGS[-1]} "
            "(a smaller MAX_FILE_WRITE_BYTES shrinks the per-request limit), or "
            "give the container more memory (about 1.1 GiB minimum with the "
            "defaults).")


def log_plan(plan: BodyBudgetPlan) -> None:
    """The one startup line (D2): WARNING when no cgroup limit was readable."""
    level = logging.WARNING if plan.cgroup_limit is None else logging.INFO
    log.log(level,
            "MCP body budget: source=%s memory_budget=%d multiplier=%d capacity=%d "
            "small_lane=%d large_lane=%d cgroup_limit=%s max_request_body=%d",
            plan.source, plan.memory_budget, plan.multiplier, plan.capacity,
            plan.small_lane, plan.large_lane,
            "none" if plan.cgroup_limit is None else plan.cgroup_limit,
            plan.max_request_body)


# ── the budget (D5–D7) ─────────────────────────────────────────────────────

class BodyLease:
    """One granted reservation. `release()` is idempotent."""

    def __init__(self, budget: "BodyBudget | None", lane: str | None, size: int):
        self.budget = budget
        self.lane = lane  # the lane the bytes came from ("small" | "large")
        self.size = size
        self.released = False

    def release(self) -> None:
        if self.released:
            return
        self.released = True
        if self.budget is not None and self.lane is not None and self.size:
            self.budget._credit(self.lane, self.size)
            self.budget._pump()


@dataclass
class BodyAdmission:
    """The outcome of one `reserve()`.

    `scope` is the request's lane (`small` or `large`) and `limit` that lane's
    capacity: the two values the 429 carries. `lease` is None for a refusal or
    a disconnect. `refused_reason` is `deadline`, `waiters` or `too_large`.
    """
    lease: BodyLease | None
    scope: str
    limit: int
    queue_ms: float = 0.0
    refused_reason: str | None = None
    disconnected: bool = False

    @property
    def admitted(self) -> bool:
        return self.lease is not None

    @property
    def lane(self) -> str | None:
        return None if self.lease is None else self.lease.lane


@dataclass(eq=False)
class _Waiter:
    size: int
    small: bool
    future: asyncio.Future
    lease: BodyLease | None = field(default=None)


class BodyBudget:
    def __init__(self, *, small_lane: int, large_lane: int, wait_seconds: float,
                 waiters: int, plan: BodyBudgetPlan | None = None):
        self.plan = plan
        self.small_capacity = int(small_lane)
        self.large_capacity = int(large_lane)
        self.wait_seconds = float(wait_seconds)
        self.max_waiters = int(waiters)
        self.small_used = 0
        self.large_used = 0
        self.small_queue: deque[_Waiter] = deque()
        self.large_queue: deque[_Waiter] = deque()

    @classmethod
    def from_plan(cls, plan: BodyBudgetPlan, settings) -> "BodyBudget":
        return cls(small_lane=plan.small_lane, large_lane=plan.large_lane,
                   wait_seconds=settings.mcp_body_budget_wait_seconds,
                   waiters=settings.mcp_body_budget_waiters, plan=plan)

    # ── views ──────────────────────────────────────────────────────────
    @property
    def reserved(self) -> int:
        return self.small_used + self.large_used

    @property
    def waiting(self) -> int:
        return len(self.small_queue) + len(self.large_queue)

    def snapshot(self) -> dict:
        return {"small": {"capacity": self.small_capacity, "used": self.small_used,
                          "waiting": len(self.small_queue)},
                "large": {"capacity": self.large_capacity, "used": self.large_used,
                          "waiting": len(self.large_queue)},
                "max_waiters": self.max_waiters, "wait_seconds": self.wait_seconds}

    # ── accounting (no await in any of these) ──────────────────────────
    def _fit(self, size: int, small: bool) -> str | None:
        """The lane `size` can be taken from right now, ignoring queue order."""
        if small:
            if self.small_used + size <= self.small_capacity:
                return "small"
            # Borrow only while no large request waits, so a stream of small
            # requests cannot keep slipping in front of a waiting large one.
            if not self.large_queue and self.large_used + size <= self.large_capacity:
                return "large"
            return None
        if self.large_used + size <= self.large_capacity:
            return "large"
        return None

    def _take(self, lane: str, size: int) -> BodyLease:
        if lane == "small":
            self.small_used += size
        else:
            self.large_used += size
        return BodyLease(self, lane, size)

    def _credit(self, lane: str, size: int) -> None:
        if lane == "small":
            self.small_used = max(0, self.small_used - size)
        else:
            self.large_used = max(0, self.large_used - size)

    def _pump(self) -> None:
        """Re-run admission: the small FIFO, then the large FIFO (D5)."""
        for queue, small in ((self.small_queue, True), (self.large_queue, False)):
            while queue:
                waiter = queue[0]
                if waiter.future.done():
                    # Cancelled or already resolved: its owner cleans up.
                    queue.popleft()
                    continue
                lane = self._fit(waiter.size, small)
                if lane is None:
                    break  # strict FIFO: nothing overtakes the head
                queue.popleft()
                waiter.lease = self._take(lane, waiter.size)
                waiter.future.set_result(None)

    def _satisfiable(self, size: int, small: bool) -> bool:
        if small:
            return size <= max(self.small_capacity, self.large_capacity)
        return size <= self.large_capacity

    # ── admission ─────────────────────────────────────────────────────
    async def reserve(self, size: int, *, small: bool, deadline: float | None = None,
                      disconnected: asyncio.Event | None = None) -> BodyAdmission:
        """Reserve `size` bytes, waiting FIFO up to the deadline.

        Everything up to the first `await` is synchronous, so an immediate
        grant or refusal completes without suspending (the middleware's
        `_admit` starts its receive watcher only for a real wait).
        `deadline` is an absolute `loop.time()`; default now + the configured
        wait.
        """
        size = max(0, int(size))
        scope = "small" if small else "large"
        limit = self.small_capacity if small else self.large_capacity
        if size == 0:
            return BodyAdmission(BodyLease(None, None, 0), scope, limit)
        if not self._satisfiable(size, small):
            return BodyAdmission(None, scope, limit, refused_reason="too_large")
        queue = self.small_queue if small else self.large_queue
        if not queue:
            # No barging: an immediate grant only when nobody in this lane's
            # FIFO is ahead.
            lane = self._fit(size, small)
            if lane is not None:
                return BodyAdmission(self._take(lane, size), scope, limit)
        if self.waiting >= self.max_waiters:
            return BodyAdmission(None, scope, limit, refused_reason="waiters")
        loop = asyncio.get_running_loop()
        started = loop.time()
        if deadline is None:
            deadline = started + self.wait_seconds
        if deadline <= started:
            return BodyAdmission(None, scope, limit, refused_reason="deadline")
        if disconnected is not None and disconnected.is_set():
            return BodyAdmission(None, scope, limit, disconnected=True)
        waiter = _Waiter(size, small, loop.create_future())
        queue.append(waiter)
        watch = None
        try:
            waits = [waiter.future]
            if disconnected is not None:
                watch = loop.create_task(disconnected.wait())
                waits.append(watch)
            # `asyncio.wait` never cancels what it waits on, so a grant that
            # `_pump` made survives a timeout or a cancellation of this task,
            # and is returned or handed back below exactly once.
            await asyncio.wait(waits, timeout=deadline - started,
                               return_when=asyncio.FIRST_COMPLETED)
            queue_ms = (loop.time() - started) * 1000
            lease = waiter.lease
            if disconnected is not None and disconnected.is_set():
                if lease is not None:
                    lease.release()
                return BodyAdmission(None, scope, limit, queue_ms=queue_ms,
                                     disconnected=True)
            if lease is not None:
                # Capacity wins over a deadline that passed in the same tick.
                return BodyAdmission(lease, scope, limit, queue_ms=queue_ms)
            return BodyAdmission(None, scope, limit, queue_ms=queue_ms,
                                 refused_reason="deadline")
        except BaseException:
            # Cancelled (or failed) while waiting: a grant that raced the exit
            # goes back.
            if waiter.lease is not None:
                waiter.lease.release()
            raise
        finally:
            if watch is not None:
                watch.cancel()
            if waiter in queue:
                queue.remove(waiter)
            if not waiter.future.done():
                waiter.future.cancel()
            # A departed head may unblock the waiters behind it.
            self._pump()


# ── the process singleton ─────────────────────────────────────────────────

_budget: BodyBudget | None = None


def configure(settings=None, *, cgroup_reader: Callable[[], int | None] | None = None,
              log_line: bool = True) -> BodyBudget:
    """Derive, check and install the process budget (the lifespan's call).

    Raises `BodyBudgetConfigError` when the plan cannot admit one maximum
    request body or a budget exceeds the safe allocation. Never called from
    `Settings` (Codex spec review finding 4).
    """
    global _budget
    if settings is None:
        from src.config import settings
    plan = derive_body_budget(settings, cgroup_reader)
    check_plan(plan)
    if log_line:
        log_plan(plan)
    _budget = BodyBudget.from_plan(plan, settings)
    return _budget


def get_body_budget() -> BodyBudget:
    """The process budget. Configured by the lifespan before serving; a
    process without a lifespan (a test, an in-process caller) derives it on
    first use, with the same check."""
    if _budget is None:
        return configure()
    return _budget


def reset_body_budget(budget: BodyBudget | None = None) -> BodyBudget | None:
    """Test boundary only: install `budget`, or clear so the next use derives."""
    global _budget
    _budget = budget
    return _budget
