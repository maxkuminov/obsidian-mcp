"""Atomic, process-local MCP occupancy control (one uvicorn worker).

No database, logging configuration, authentication or asyncio primitives at
import time. Leases capture counter identities; keyed overflow stays sticky
until drained. Shadow counts real occupancy, not an imaginary replay of
rejected traffic.

Modes (#188 `concurrency-enforce-ready`, design D7):

- ``off``: no accounting at all.
- ``shadow``: never waits; a would-refuse is recorded as pressure.
- ``queue``: waits exactly like ``enforce`` (deadlines, waiter bounds,
  eligible-FIFO) and, wherever ``enforce`` would refuse **for capacity**,
  grants the lease with an ``overrun`` mark instead. Shutdown still refuses.
- ``enforce``: waits, then refuses.

Besides the controller this module holds three process-wide, in-memory
objects: the durable-counter accumulator (``counters()``, drained by the
periodic flush), the transport replay-byte budget (``replay_budget()``) and the
row provenance (``provenance()``).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Hashable, Iterable, Mapping

from src.services.pool_budget import CLASS_CONNECTIONS, budget_terms

# Bump whenever TOOL_CLASSES changes shape: it is part of the epoch, so rows
# and counters from a different classification never mix in one window.
CLASS_MAPPING_VERSION = 2

TOOL_CLASSES = {
    "semantic_search": "embedding",
    "find_related": "vector",
    **dict.fromkeys(("create_note", "edit_note", "move_note", "delete_note",
                     "set_frontmatter", "write_file", "delete_file",
                     "import_from_url"), "write"),
    **dict.fromkeys(("keyword_search", "list_notes", "get_tags",
                     "get_neighborhood", "find_orphans", "list_files"), "scan"),
    **dict.fromkeys(("read_note", "read_file", "get_recent", "get_vault_guide",
                     "get_backlinks", "get_links", "request_upload",
                     "check_upload", "request_download"), "light"),
}
CLASSES = frozenset(("embedding", "vector", "write", "scan", "light"))
assert CLASSES == frozenset(CLASS_CONNECTIONS)

# Pipeline order of the admission stages (design D4).
STAGE_ORDER = {"request": 0, "auth": 1, "tool": 2, "writer": 3}
MAX_OBSERVATIONS = 4
PROVENANCE_VERSION = 2
METADATA_SCHEMA = 2

log = logging.getLogger(__name__)


def _ms(seconds_ms: float) -> int:
    return int(round(seconds_ms))


@dataclass(frozen=True)
class Pressure:
    """One capacity observation at one stage.

    ``waited_ms`` and ``overrun`` are only meaningful for queue-mode
    observations (``Admission.observation``); a shadow observation keeps both at
    their defaults.
    """
    stage: str
    scope: str
    limit: int
    waited_ms: int = 0
    overrun: bool = False

    @property
    def code(self) -> str:
        return "slot_timeout" if self.stage == "tool" else f"{self.stage}_concurrency_limited"

    @classmethod
    def coerce(cls, value) -> "Pressure":
        if isinstance(value, Pressure):
            return value
        if isinstance(value, Mapping):
            return cls(str(value["stage"]), str(value["scope"]), int(value["limit"]),
                       int(value.get("waited_ms") or 0), bool(value.get("overrun", False)))
        raise TypeError(f"not a concurrency observation: {value!r}")


def _ordered(observations: Iterable) -> list[Pressure]:
    """Deduplicate, then order by pipeline stage (stable within a stage)."""
    unique: list[Pressure] = []
    for item in observations:
        if item is None:
            continue
        p = Pressure.coerce(item)
        if p not in unique:
            unique.append(p)
    unique.sort(key=lambda p: STAGE_ORDER.get(p.stage, len(STAGE_ORDER)))
    return unique


def _truncate(ordered: list[Pressure], deciding: Pressure) -> list[Pressure]:
    """Keep at most four, never dropping the deciding observation."""
    if len(ordered) <= MAX_OBSERVATIONS or deciding in ordered[:MAX_OBSERVATIONS]:
        return ordered[:MAX_OBSERVATIONS]
    # The deciding observation is later in stage order than the first three,
    # so appending it keeps the list stage-ordered.
    return ordered[:MAX_OBSERVATIONS - 1] + [deciding]


def shadow_metadata(observations, *, configured_wait_ms: Mapping[str, int] | None = None) -> dict | None:
    """Bounded shadow observations, stage-ordered, with no identity fields.

    Every shadow observation is a zero-wait capacity miss, so ``code`` is the
    earliest stage's (D4). ``configured_wait_ms`` reports the waits shadow did
    **not** apply; when omitted it is read from the live controller.
    """
    ordered = _ordered(observations)
    if not ordered:
        return None
    if configured_wait_ms is None:
        configured_wait_ms = get_controller().configured_wait_ms()
    deciding = ordered[0]
    kept = _truncate(ordered, deciding)
    return {"shadow": True, "schema": METADATA_SCHEMA, "code": deciding.code,
            "basis": "observed_occupancy_zero_wait",
            "configured_wait_ms": dict(configured_wait_ms),
            "observations": [{"stage": p.stage, "scope": p.scope, "limit": p.limit}
                             for p in kept]}


def queue_metadata(observations) -> dict | None:
    """Bounded queue-mode observations (ordinary waits and overruns).

    ``code`` is the earliest-stage **overrun**'s code, and ``None`` when nothing
    overran: an ordinary wait never sets a code (D4).
    """
    ordered = _ordered(observations)
    if not ordered:
        return None
    overruns = [p for p in ordered if p.overrun]
    deciding = overruns[0] if overruns else None
    kept = _truncate(ordered, deciding if deciding is not None else ordered[0])
    return {"schema": METADATA_SCHEMA, "overrun": deciding is not None,
            "code": deciding.code if deciding is not None else None,
            "observations": [{"stage": p.stage, "scope": p.scope, "limit": p.limit,
                              "waited_ms": p.waited_ms, "overrun": p.overrun}
                             for p in kept]}


request_observations: ContextVar[tuple[Pressure, ...]] = ContextVar(
    "concurrency_request_observations", default=())


@dataclass(eq=False)
class _Counter:
    active: int = 0
    waiting: int = 0
    refs: int = 0


class _Registry:
    def __init__(self, capacity):
        self.capacity = capacity
        self.entries: dict[Hashable, _Counter] = {}
        self.overflow = _Counter()
        self.keys: dict[_Counter, Hashable] = {}

    def retain(self, key):
        entry = self.entries.get(key)
        if entry is None:
            if self.overflow.refs or len(self.entries) >= self.capacity:
                entry = self.overflow
            else:
                entry = self.entries[key] = _Counter()
                self.keys[entry] = key
        entry.refs += 1
        return entry

    def drop(self, entry):
        entry.refs -= 1
        if entry.refs == 0 and entry is not self.overflow:
            # No lease stores the credential key. Only this bounded registry
            # does, and deletion erases it as soon as its last owner drains.
            key = self.keys.pop(entry)
            del self.entries[key]


class Lease:
    def __init__(self, controller=None, dimensions=(), refs=()):
        self.controller = controller
        self.dimensions = dimensions
        self.refs = refs
        self.released = False

    def release(self):
        if self.released:
            return
        self.released = True
        for counter, _, _ in self.dimensions:
            counter.active -= 1
        for registry, counter in self.refs:
            registry.drop(counter)
        if self.controller is not None:
            self.controller._pump()


@dataclass
class Admission:
    """The outcome of one admission attempt.

    - ``lease`` is ``None`` for a refusal or a disconnect.
    - ``pressure``: the capacity pressure met (shadow would-refuse, a wait that
      was granted, a refusal, or an overrun).
    - ``overrun``: queue mode only: enforce would have refused for this
      capacity, and the lease was granted anyway.
    - ``disconnected``: the ``disconnected`` event released the waiter.
    - ``queue_ms``: milliseconds spent waiting at this stage.
    """
    lease: Lease | None
    pressure: Pressure | None = None
    shadow: dict | None = None
    queue_ms: float = 0
    overrun: Pressure | None = None
    disconnected: bool = False

    @property
    def admitted(self):
        return self.lease is not None

    @property
    def observation(self) -> Pressure | None:
        """This stage's entry for ``shadow_metadata`` / ``queue_metadata``."""
        if self.overrun is not None:
            return replace(self.overrun, waited_ms=_ms(self.queue_ms), overrun=True)
        if self.pressure is not None:
            return replace(self.pressure, waited_ms=_ms(self.queue_ms))
        return None


@dataclass(eq=False)
class _Waiter:
    stage: str
    dimensions: tuple
    refs: tuple
    wait_dimensions: tuple
    future: asyncio.Future
    deadline: float
    pressure: Pressure
    started: float
    grant: Admission | None = None


def _settings_limits(settings) -> dict:
    return {name.removeprefix("mcp_concurrency_"): getattr(settings, name)
            for name in type(settings).model_fields
            if name.startswith("mcp_concurrency_")}


def _canonical(value):
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        # `Field(5)` keeps an int default while ".env" parses "5" to 5.0: the
        # same effective setting must hash the same.
        return float(value)
    return value


def epoch(settings) -> str:
    """12 hex digits naming every concurrency setting except ``mode``.

    A mode change keeps the epoch (so collected evidence stays attributable);
    any limit change moves it. The class-mapping version is part of it.
    """
    payload = {name: _canonical(value)
               for name, value in _settings_limits(settings).items() if name != "mode"}
    payload["class_mapping_version"] = CLASS_MAPPING_VERSION
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


class Controller:
    def __init__(self, settings):
        # Capture configuration once, never replace state on a live request.
        self.mode = "off" if settings.mcp_sandbox_mode else settings.mcp_concurrency_mode
        self.limits = _settings_limits(settings)
        self.epoch = epoch(settings)
        self.requests = _Counter()
        self.authentication = _Counter()
        self.tools = _Counter()
        self.writers = _Counter()
        self.classes = {name: _Counter() for name in CLASSES}
        self.fingerprints = _Registry(self.limits["registry_size"])
        self.tenants = _Registry(self.limits["registry_size"])
        self.principals = _Registry(self.limits["registry_size"])
        self.pending: list[_Waiter] = []
        self.closing = False
        self.writers_closed = False

    # ── configuration views ────────────────────────────────────────────
    def configured_wait_ms(self) -> dict:
        return {"transport": _ms(self.limits["transport_wait_seconds"] * 1000),
                "tool": _ms(self.limits["wait_seconds"] * 1000)}

    def pool_demand(self) -> dict:
        return budget_terms(auth=self.limits["auth"], tools=self.limits["tools"],
                            caps={c: self.limits[c] for c in CLASSES},
                            writers=self.limits["writers"])

    def transport_deadline(self) -> float:
        """One monotonic deadline for the request and auth stages (D1)."""
        return asyncio.get_running_loop().time() + self.limits["transport_wait_seconds"]

    def snapshot(self) -> dict:
        """Live in-process occupancy plus the effective configuration."""
        return {
            "mode": self.mode,
            "epoch": self.epoch,
            "limits": dict(self.limits),
            "pool_demand": self.pool_demand(),
            "active": {"requests": self.requests.active,
                       "auth": self.authentication.active,
                       "tools": self.tools.active,
                       "writers": self.writers.active,
                       "classes": {c: self.classes[c].active for c in sorted(CLASSES)}},
            "waiting": {"requests": self.requests.waiting,
                        "auth": self.authentication.waiting,
                        "tools": self.tools.waiting,
                        "writers": self.writers.waiting},
            "pending": len(self.pending),
            "registries": {"fingerprints": len(self.fingerprints.entries),
                           "tenants": len(self.tenants.entries),
                           "principals": len(self.principals.entries)},
            "closing": self.closing,
        }

    # ── admission internals ────────────────────────────────────────────
    def _pressure(self, stage, dimensions):
        for counter, limit, scope in dimensions:
            if counter.active >= limit:
                return Pressure(stage, scope, limit)
        return None

    @staticmethod
    def _drop_refs(refs):
        for registry, counter in refs:
            registry.drop(counter)

    def _grant(self, dimensions, refs, pressure=None, *, overrun=None, queue_ms=0):
        for counter, _, _ in dimensions:
            counter.active += 1
        shadow = None
        if self.mode == "shadow" and pressure is not None:
            shadow = shadow_metadata((pressure,), configured_wait_ms=self.configured_wait_ms())
        return Admission(Lease(self, dimensions, refs), pressure, shadow,
                         queue_ms=queue_ms, overrun=overrun)

    def _capacity_miss(self, stage, dimensions, refs, pressure, queue_ms=0):
        """Where enforce refuses for capacity, queue grants with an overrun."""
        if self.mode == "queue":
            return self._grant(dimensions, refs, pressure, overrun=pressure, queue_ms=queue_ms)
        self._drop_refs(refs)
        return Admission(None, pressure, queue_ms=queue_ms)

    def _closed(self, stage):
        return self.writers_closed if stage == "writer" else self.closing

    def _immediate(self, stage, dimensions, refs=()):
        if self.mode == "off":
            self._drop_refs(refs)
            return Admission(Lease())
        if self._closed(stage):
            self._drop_refs(refs)
            return Admission(None, Pressure(stage, "shutdown", 0))
        pressure = self._pressure(stage, dimensions)
        if pressure is not None and self.mode in ("enforce", "queue"):
            return self._capacity_miss(stage, dimensions, refs, pressure)
        return self._grant(dimensions, refs, pressure)

    # ── admission points ───────────────────────────────────────────────
    async def request(self, fingerprint: str, deadline: float | None = None,
                      disconnected: asyncio.Event | None = None) -> Admission:
        """Global + fingerprint envelope for the full ASGI request lifetime.

        ``deadline`` comes from ``transport_deadline()`` and is shared with
        ``auth()``. The fingerprint entry stays retained while waiting.
        """
        if self.mode == "off":
            return Admission(Lease())
        entry = self.fingerprints.retain(fingerprint)
        if deadline is None:
            deadline = self.transport_deadline()
        return await self._admit_wait(
            "request",
            ((self.requests, self.limits["requests"], "global"),
             (entry, self.limits["fingerprint"], "fingerprint")),
            ((self.fingerprints, entry),),
            ((self.requests, self.limits["request_waiters"], "request_waiters"),
             (entry, self.limits["fingerprint_waiters"], "fingerprint_waiters")),
            deadline, disconnected)

    async def auth(self, deadline: float | None = None,
                   disconnected: asyncio.Event | None = None) -> Admission:
        """The permit around the middleware's own DB session."""
        if deadline is None and self.mode in ("enforce", "queue"):
            deadline = self.transport_deadline()
        return await self._admit_wait(
            "auth", ((self.authentication, self.limits["auth"], "global"),), (),
            ((self.authentication, self.limits["auth_waiters"], "auth_waiters"),),
            deadline, disconnected)

    async def tool(self, tool_name, tenant, principal, *, resource_class=None) -> Admission:
        # Explicit internal classes are useful for test tools, but cannot
        # override the closed production registry's declared classification.
        registered = TOOL_CLASSES.get(tool_name)
        if resource_class is None:
            resource_class = registered
        if resource_class not in CLASSES or (registered and registered != resource_class):
            raise ValueError(f"No matching explicit concurrency class for {tool_name!r}")
        if self.mode == "off" or principal is None:
            return Admission(Lease())
        tenant_entry = self.tenants.retain(tenant)
        principal_entry = self.principals.retain(principal)
        dimensions = ((self.classes[resource_class], self.limits[resource_class], resource_class),
                      (principal_entry, self.limits["principal"], "principal"),
                      (tenant_entry, self.limits["tenant"], "tenant"),
                      (self.tools, self.limits["tools"], "global"))
        refs = ((self.tenants, tenant_entry), (self.principals, principal_entry))
        wait_dimensions = ((principal_entry, self.limits["principal_waiters"], "principal_waiters"),
                           (tenant_entry, self.limits["tenant_waiters"], "tenant_waiters"),
                           (self.tools, self.limits["waiters"], "global_waiters"))
        deadline = asyncio.get_running_loop().time() + self.limits["wait_seconds"]
        return await self._admit_wait("tool", dimensions, refs, wait_dimensions, deadline)

    async def writer(self) -> Admission:
        deadline = asyncio.get_running_loop().time() + self.limits["writer_wait_seconds"]
        return await self._admit_wait(
            "writer", ((self.writers, self.limits["writers"], "global"),), (),
            ((self.writers, self.limits["writer_waiters"], "writer_waiters"),),
            deadline)

    async def _admit_wait(self, stage, dimensions, refs, wait_dimensions, deadline,
                          disconnected: asyncio.Event | None = None):
        if self.mode not in ("enforce", "queue") or self._closed(stage):
            return self._immediate(stage, dimensions, refs)
        pressure = self._pressure(stage, dimensions)
        if pressure is None:
            return self._grant(dimensions, refs)
        for counter, limit, scope in wait_dimensions:
            if counter.waiting >= limit:
                return self._capacity_miss(stage, dimensions, refs, Pressure(stage, scope, limit))
        loop = asyncio.get_running_loop()
        started = loop.time()
        if deadline <= started:
            return self._capacity_miss(stage, dimensions, refs, pressure)
        if disconnected is not None and disconnected.is_set():
            self._drop_refs(refs)
            return Admission(None, pressure, disconnected=True)
        waiter = _Waiter(stage, dimensions, refs, wait_dimensions, loop.create_future(),
                         deadline, pressure, started)
        for counter, _, _ in wait_dimensions:
            counter.waiting += 1
        self.pending.append(waiter)
        watch = None
        try:
            waits = [waiter.future]
            if disconnected is not None:
                watch = loop.create_task(disconnected.wait())
                waits.append(watch)
            # asyncio.wait never cancels what it waits on, so a grant that
            # _pump() transferred to this waiter survives a timeout or a
            # cancellation of this task; the owner below returns or releases it.
            await asyncio.wait(waits, timeout=deadline - started,
                               return_when=asyncio.FIRST_COMPLETED)
            queue_ms = (loop.time() - started) * 1000
            grant = waiter.grant
            if disconnected is not None and disconnected.is_set():
                if grant is not None and grant.lease is not None:
                    grant.lease.release()
                return Admission(None, pressure, queue_ms=queue_ms, disconnected=True)
            if grant is not None:
                # Resolved by _pump(): an ordinary grant, a queue overrun, or a
                # refusal. A grant racing the deadline is returned exactly once.
                return grant
            # The deadline passed and nothing resolved this waiter: resolve it
            # here, once, while it is still registered.
            self.pending.remove(waiter)
            self._unwait(waiter)
            return self._capacity_miss(stage, dimensions, refs, pressure, queue_ms)
        except BaseException:
            if waiter.grant is not None and waiter.grant.lease is not None:
                waiter.grant.lease.release()
            raise
        finally:
            if watch is not None:
                watch.cancel()
            if waiter in self.pending:
                self.pending.remove(waiter)
                self._unwait(waiter)
                self._drop_refs(refs)
            if not waiter.future.done():
                waiter.future.cancel()
            self._pump()

    @staticmethod
    def _unwait(waiter):
        for counter, _, _ in waiter.wait_dimensions:
            counter.waiting -= 1

    def _pump(self):
        # Oldest ELIGIBLE first; a full embedding class cannot park global
        # capacity that a different class can use. No await in this transition.
        for waiter in list(self.pending):
            loop = waiter.future.get_loop()
            closed = self._closed(waiter.stage)
            blocked = self._pressure(waiter.stage, waiter.dimensions) is not None
            expired = loop.time() >= waiter.deadline
            if not closed and blocked and not expired and not waiter.future.cancelled():
                continue
            self.pending.remove(waiter)
            self._unwait(waiter)
            queue_ms = (loop.time() - waiter.started) * 1000
            if waiter.future.cancelled():
                self._drop_refs(waiter.refs)
                continue
            if closed:
                self._drop_refs(waiter.refs)
                result = Admission(None, Pressure(waiter.stage, "shutdown", 0), queue_ms=queue_ms)
            elif not blocked:
                # Capacity wins over an expired deadline: never refuse (or
                # mark overrun) a waiter that can be granted right now.
                result = self._grant(waiter.dimensions, waiter.refs, waiter.pressure,
                                     queue_ms=queue_ms)
            else:
                result = self._capacity_miss(waiter.stage, waiter.dimensions, waiter.refs,
                                             waiter.pressure, queue_ms)
            waiter.grant = result
            if not waiter.future.done():
                waiter.future.set_result(result)
            elif result.lease is not None:
                result.lease.release()

    def shutdown(self, *, close_writers=False):
        """Stop new tools/requests, drain flush writers before closing them."""
        self.closing = True
        self.writers_closed = self.writers_closed or close_writers
        self._pump()


# ── process-wide durable-counter accumulator (design D8) ─────────────────

COUNT_METRICS = ("requests", "transport_pressured", "transport_waited",
                 "transport_overrun", "transport_refused", "writer_overrun",
                 "writer_refused", "pool_checkout_timeout")
GAUGE_METRICS = ("pool_high_water", "transport_wait_max_ms")
METRICS = frozenset(COUNT_METRICS + GAUGE_METRICS)
REQUEST_OUTCOMES = ("none", "pressured", "waited", "overrun", "refused")
MAX_UNFLUSHED_MINUTES = 60


def floor_minute(at: datetime) -> datetime:
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return at.astimezone(timezone.utc).replace(second=0, microsecond=0)


class Counters:
    """In-memory ``(event-time minute, metric) -> (count, max)`` accumulator.

    Bounded to ``MAX_UNFLUSHED_MINUTES`` distinct minutes: past the cap the
    oldest minute is dropped and ``lossy`` is set (sticky for the process),
    never silently re-attributed. Guarded by a thread lock because the pool
    checkout hook may run outside the event loop's thread; it holds no
    asyncio primitive.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._buckets: dict[datetime, dict[str, list]] = {}
        self.lossy = False

    def _slot(self, minute: datetime) -> dict | None:
        slot = self._buckets.get(minute)
        if slot is not None:
            return slot
        if len(self._buckets) >= MAX_UNFLUSHED_MINUTES:
            self.lossy = True
            oldest = min(self._buckets)
            if minute < oldest:
                return None
            del self._buckets[oldest]
        slot = self._buckets[minute] = {}
        return slot

    def _add(self, minute, metric, count, value):
        slot = self._slot(minute)
        if slot is None:
            return
        entry = slot.get(metric)
        if entry is None:
            slot[metric] = [count, value]
            return
        entry[0] += count
        if value is not None:
            entry[1] = value if entry[1] is None else max(entry[1], value)

    @staticmethod
    def _at(at):
        return floor_minute(at if at is not None else datetime.now(timezone.utc))

    def record_request(self, worst: str, at: datetime | None = None) -> None:
        """One /mcp request that reached admission, by its worst outcome."""
        if worst not in REQUEST_OUTCOMES:
            raise ValueError(f"unknown transport outcome {worst!r}")
        minute = self._at(at)
        with self._lock:
            self._add(minute, "requests", 1, None)
            if worst != "none":
                self._add(minute, "transport_" + worst, 1, None)

    def record(self, metric: str, n: int = 1, at: datetime | None = None) -> None:
        if metric not in COUNT_METRICS:
            raise ValueError(f"not a count metric: {metric!r}")
        if n < 0:
            raise ValueError("count increments are non-negative")
        minute = self._at(at)
        with self._lock:
            self._add(minute, metric, int(n), None)

    def gauge(self, metric: str, value: float, at: datetime | None = None) -> None:
        if metric not in GAUGE_METRICS:
            raise ValueError(f"not a gauge metric: {metric!r}")
        minute = self._at(at)
        with self._lock:
            self._add(minute, metric, 1, int(round(value)))

    def drain(self) -> dict:
        with self._lock:
            out = {(minute, metric): (entry[0], entry[1])
                   for minute, slot in self._buckets.items()
                   for metric, entry in slot.items()}
            self._buckets.clear()
            return out

    def merge_back(self, drained: Mapping) -> None:
        """Return a failed flush's entries under their original keys."""
        with self._lock:
            for (minute, metric), (count, value) in sorted(drained.items(),
                                                           key=lambda kv: kv[0][0]):
                if metric not in METRICS:
                    raise ValueError(f"unknown metric {metric!r}")
                self._add(minute, metric, count, value)

    def minutes(self) -> int:
        with self._lock:
            return len(self._buckets)


# ── process-wide replay-byte budget (design D1) ─────────────────────────

class ReplayBudget:
    """Bytes of ``/mcp`` messages held for replay while requests wait.

    It never refuses an already-consumed message: the watcher reserves each
    message **after** ``receive`` returned it, so ``try_reserve`` always
    accounts the bytes and answers whether the budget still has room (``True``)
    or is now exhausted (``False``, stop calling ``receive``). ``exhausted`` is
    the check before each ``receive``.
    """

    def __init__(self, capacity: int):
        self.capacity = int(capacity)
        self.used = 0

    @property
    def exhausted(self) -> bool:
        return self.used >= self.capacity

    def try_reserve(self, n: int) -> bool:
        self.used += max(0, int(n))
        return not self.exhausted

    def release(self, n: int) -> None:
        self.used = max(0, self.used - max(0, int(n)))


_controller: Controller | None = None
_counters: Counters | None = None
_replay_budget: ReplayBudget | None = None


def get_controller() -> Controller:
    global _controller
    if _controller is None:
        from src.config import settings
        _controller = Controller(settings)
    return _controller


def log_effective_settings(controller: Controller) -> None:
    """The startup INFO line an operator compares with the intended block."""
    log.info(
        "MCP concurrency effective settings: mode=%s epoch=%s pool_demand=%s limits=%s",
        controller.mode, controller.epoch,
        json.dumps(controller.pool_demand(), sort_keys=True),
        json.dumps(controller.limits, sort_keys=True))


def reset_controller(settings=None) -> Controller:
    """Explicit lifespan/test boundary only; captured leases retain old owners."""
    global _controller, _replay_budget
    if _controller is not None:
        _controller.shutdown(close_writers=True)
    if settings is None:
        from src.config import settings
    _controller = Controller(settings)
    _replay_budget = None
    log_effective_settings(_controller)
    return _controller


def provenance() -> dict:
    """``params.concurrency`` for every tracked usage row (design D8)."""
    controller = get_controller()
    return {"v": PROVENANCE_VERSION, "mode": controller.mode, "epoch": controller.epoch}


def counters() -> Counters:
    global _counters
    if _counters is None:
        _counters = Counters()
    return _counters


def reset_counters() -> Counters:
    """Test boundary only."""
    global _counters
    _counters = Counters()
    return _counters


def replay_budget() -> ReplayBudget:
    global _replay_budget
    if _replay_budget is None:
        _replay_budget = ReplayBudget(get_controller().limits["replay_budget_bytes"])
    return _replay_budget
