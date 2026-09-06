"""Atomic, process-local MCP occupancy control (one uvicorn worker).

No database, logging, authentication or asyncio primitives at import time.
Leases capture counter identities; keyed overflow stays sticky until drained.
Shadow counts real occupancy, not an imaginary replay of rejected traffic.
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Hashable

TOOL_CLASSES = {
    "semantic_search": "embedding", "find_related": "vector",
    **dict.fromkeys(("create_note", "edit_note", "move_note", "delete_note",
                    "set_frontmatter", "write_file", "delete_file", "import_from_url"), "write"),
    **dict.fromkeys(("keyword_search", "read_note", "list_notes", "get_tags", "get_recent",
                    "get_vault_guide", "get_backlinks", "get_links", "get_neighborhood",
                    "find_orphans", "read_file", "list_files", "request_upload",
                    "check_upload", "request_download"), "other"),
}
CLASSES = frozenset(("embedding", "vector", "write", "other"))


@dataclass(frozen=True)
class Pressure:
    stage: str
    scope: str
    limit: int

    @property
    def code(self) -> str:
        return "slot_timeout" if self.stage == "tool" else f"{self.stage}_concurrency_limited"


def shadow_metadata(observations) -> dict | None:
    """Bounded transport/tool/writer observations, with no identity fields."""
    unique = []
    for p in observations:
        if p is not None and p not in unique:
            unique.append(p)
        if len(unique) == 4:
            break
    if not unique:
        return None
    return {"shadow": True, "code": unique[-1].code,
            "basis": "observed_occupancy_zero_wait",
            "observations": [{"stage": p.stage, "scope": p.scope, "limit": p.limit}
                             for p in unique]}


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
    lease: Lease | None
    pressure: Pressure | None = None
    shadow: dict | None = None
    queue_ms: float = 0

    @property
    def admitted(self):
        return self.lease is not None


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


class Controller:
    def __init__(self, settings):
        # Capture configuration once, never replace state on a live request.
        self.mode = "off" if settings.mcp_sandbox_mode else settings.mcp_concurrency_mode
        self.limits = {name.removeprefix("mcp_concurrency_"): getattr(settings, name)
                       for name in type(settings).model_fields
                       if name.startswith("mcp_concurrency_")}
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

    def _pressure(self, stage, dimensions):
        for counter, limit, scope in dimensions:
            if counter.active >= limit:
                return Pressure(stage, scope, limit)
        return None

    @staticmethod
    def _drop_refs(refs):
        for registry, counter in refs:
            registry.drop(counter)

    def _grant(self, dimensions, refs, pressure=None):
        for counter, _, _ in dimensions:
            counter.active += 1
        return Admission(Lease(self, dimensions, refs), pressure,
                         shadow_metadata((pressure,)) if self.mode == "shadow" else None)

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
        if pressure is not None and self.mode == "enforce":
            self._drop_refs(refs)
            return Admission(None, pressure)
        return self._grant(dimensions, refs, pressure)

    def request(self, fingerprint: str) -> Admission:
        if self.mode == "off":
            return Admission(Lease())
        entry = self.fingerprints.retain(fingerprint)
        return self._immediate("request", (
            (self.requests, self.limits["requests"], "global"),
            (entry, self.limits["fingerprint"], "fingerprint")), ((self.fingerprints, entry),))

    def auth(self) -> Admission:
        return self._immediate("auth", ((self.authentication, self.limits["auth"], "global"),))

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
        return await self._admit_wait("tool", dimensions, refs, wait_dimensions,
                                      self.limits["wait_seconds"])

    async def writer(self) -> Admission:
        return await self._admit_wait("writer",
            ((self.writers, self.limits["writers"], "global"),), (),
            ((self.writers, self.limits["writer_waiters"], "writer_waiters"),),
            self.limits["writer_wait_seconds"])

    async def _admit_wait(self, stage, dimensions, refs, wait_dimensions, wait):
        if self.mode != "enforce" or self._closed(stage):
            return self._immediate(stage, dimensions, refs)
        pressure = self._pressure(stage, dimensions)
        if pressure is None:
            return self._grant(dimensions, refs)
        for counter, limit, scope in wait_dimensions:
            if counter.waiting >= limit:
                self._drop_refs(refs)
                return Admission(None, Pressure(stage, scope, limit))
        if wait == 0:
            self._drop_refs(refs)
            return Admission(None, pressure)
        loop = asyncio.get_running_loop()
        started = loop.time()
        waiter = _Waiter(stage, dimensions, refs, wait_dimensions, loop.create_future(),
                         started + wait, pressure, started)
        for counter, _, _ in wait_dimensions:
            counter.waiting += 1
        self.pending.append(waiter)
        try:
            # Shield ensures cancellation does not erase a grant already
            # transferred to this waiter by release(). The owner returns it.
            return await asyncio.wait_for(asyncio.shield(waiter.future), wait)
        except TimeoutError:
            if waiter.grant is not None and waiter.grant.lease is not None:
                waiter.grant.lease.release()
            return Admission(None, pressure, queue_ms=(loop.time()-started)*1000)
        except BaseException:
            if waiter.grant is not None and waiter.grant.lease is not None:
                waiter.grant.lease.release()
            raise
        finally:
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
            expired = loop.time() >= waiter.deadline
            if not closed and not expired and self._pressure(waiter.stage, waiter.dimensions):
                continue
            self.pending.remove(waiter)
            self._unwait(waiter)
            if closed or expired or waiter.future.cancelled():
                self._drop_refs(waiter.refs)
                result = Admission(None, Pressure(waiter.stage, "shutdown", 0) if closed
                                   else waiter.pressure)
            else:
                result = self._grant(waiter.dimensions, waiter.refs)
            result.queue_ms = (loop.time()-waiter.started)*1000
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


_controller: Controller | None = None


def get_controller() -> Controller:
    global _controller
    if _controller is None:
        from src.config import settings
        _controller = Controller(settings)
    return _controller


def reset_controller(settings=None) -> Controller:
    """Explicit lifespan/test boundary only; captured leases retain old owners."""
    global _controller
    if _controller is not None:
        _controller.shutdown(close_writers=True)
    if settings is None:
        from src.config import settings
    _controller = Controller(settings)
    return _controller
