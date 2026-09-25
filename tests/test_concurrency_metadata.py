"""Metadata ordering and code selection (D4), epoch/provenance/snapshot (D8),
the replay budget (D1) and the per-class pool budget (D2)."""
import asyncio

import pytest

from src.config import Settings
from src.services import concurrency
from src.services.concurrency import (
    Admission, Controller, Pressure, epoch, provenance, queue_metadata,
    replay_budget, shadow_metadata,
)
from src.services.pool_budget import (
    CLASS_CONNECTIONS, MCP_POOL_HEADROOM, POOL_CAPACITY,
    budget_terms, tool_demand,
)

SECRET = "issue-261-test-secret-only-0123456789abcdef"
WAITS = {"transport": 2000, "tool": 5000}

REQ = Pressure("request", "fingerprint", 20)
AUTH = Pressure("auth", "global", 2)
TOOL = Pressure("tool", "light", 4)
WRITER = Pressure("writer", "global", 1)


def settings(**kw):
    return Settings(_env_file=None, secret_key=SECRET, **kw)


# ── shadow ───────────────────────────────────────────────────────────────

def test_shadow_later_writer_does_not_mask_tool():
    meta = shadow_metadata((TOOL, WRITER), configured_wait_ms=WAITS)
    assert meta["code"] == "slot_timeout"
    assert meta["observations"][0]["stage"] == "tool"
    assert meta["schema"] == 2 and meta["shadow"] is True
    assert meta["basis"] == "observed_occupancy_zero_wait"
    assert meta["configured_wait_ms"] == WAITS


def test_shadow_transport_outranks_tool_whatever_the_arrival_order():
    meta = shadow_metadata((TOOL, REQ), configured_wait_ms=WAITS)
    assert meta["code"] == "request_concurrency_limited"
    assert [o["stage"] for o in meta["observations"]] == ["request", "tool"]


def test_shadow_truncation_keeps_earliest_stage():
    tools = [Pressure("tool", s, 1) for s in ("light", "principal", "tenant", "global")]
    meta = shadow_metadata((*tools, REQ), configured_wait_ms=WAITS)
    assert len(meta["observations"]) == 4
    assert meta["observations"][0] == {"stage": "request", "scope": "fingerprint", "limit": 20}
    assert meta["code"] == "request_concurrency_limited"


def test_shadow_dedupes_and_accepts_stored_dicts():
    stored = shadow_metadata((TOOL,), configured_wait_ms=WAITS)["observations"]
    meta = shadow_metadata((*stored, TOOL, WRITER), configured_wait_ms=WAITS)
    assert [o["stage"] for o in meta["observations"]] == ["tool", "writer"]


def test_shadow_metadata_carries_no_identity():
    meta = shadow_metadata((REQ, AUTH, TOOL, WRITER), configured_wait_ms=WAITS)
    assert set(meta) == {"shadow", "schema", "code", "basis", "configured_wait_ms", "observations"}
    for obs in meta["observations"]:
        assert set(obs) == {"stage", "scope", "limit"}


def test_shadow_default_configured_waits_come_from_the_controller():
    concurrency.reset_controller(settings(mcp_concurrency_wait_seconds=7,
                                          mcp_concurrency_transport_wait_seconds=1.5))
    assert shadow_metadata((TOOL,))["configured_wait_ms"] == {"transport": 1500, "tool": 7000}
    assert shadow_metadata(()) is None and shadow_metadata((None,)) is None


# ── queue ────────────────────────────────────────────────────────────────

def test_queue_ordinary_wait_gives_null_code():
    meta = queue_metadata((Pressure("tool", "light", 4, waited_ms=40),))
    assert meta == {"schema": 2, "overrun": False, "code": None,
                    "observations": [{"stage": "tool", "scope": "light", "limit": 4,
                                      "waited_ms": 40, "overrun": False}]}


def test_queue_earlier_wait_then_later_overrun():
    auth_wait = Pressure("auth", "global", 2, waited_ms=40)
    tool_overrun = Pressure("tool", "light", 4, waited_ms=5000, overrun=True)
    meta = queue_metadata((tool_overrun, auth_wait))
    assert meta["code"] == "slot_timeout" and meta["overrun"] is True
    first = meta["observations"][0]
    assert first["stage"] == "auth" and first["overrun"] is False


def test_queue_code_is_earliest_overrun():
    meta = queue_metadata((Pressure("writer", "global", 1, 250, True),
                           Pressure("tool", "light", 4, 5000, True)))
    assert meta["code"] == "slot_timeout"


def test_queue_truncation_keeps_the_overrun():
    waits = [Pressure("request", "global", 64, 10), Pressure("request", "fingerprint", 20, 11),
             Pressure("auth", "global", 2, 12), Pressure("auth", "auth_waiters", 32, 13)]
    overrun = Pressure("tool", "light", 4, 5000, True)
    meta = queue_metadata((overrun, *waits))
    assert len(meta["observations"]) == 4
    assert meta["observations"][-1]["stage"] == "tool" and meta["observations"][-1]["overrun"]
    assert [o["stage"] for o in meta["observations"]] == ["request", "request", "auth", "tool"]
    assert meta["code"] == "slot_timeout"


def test_queue_nothing_observed_is_none():
    assert queue_metadata(()) is None


def test_admission_observation():
    p = Pressure("tool", "light", 4)
    assert Admission(None).observation is None
    assert Admission(None, p, queue_ms=39.6).observation == Pressure("tool", "light", 4, 40)
    over = Admission(None, p, queue_ms=5001.2, overrun=p).observation
    assert over.overrun and over.waited_ms == 5001


# ── epoch, provenance, snapshot ──────────────────────────────────────────

def test_epoch_is_stable_across_modes_and_moves_with_limits():
    base = epoch(settings())
    assert len(base) == 12 and int(base, 16) >= 0
    for mode in ("off", "shadow", "queue", "enforce"):
        assert epoch(settings(mcp_concurrency_mode=mode)) == base
    assert epoch(settings(mcp_concurrency_light=3)) != base
    assert epoch(settings(mcp_concurrency_transport_wait_seconds=1)) != base
    assert epoch(settings(mcp_concurrency_replay_budget_bytes=2 ** 20)) != base
    # An int default and a parsed float for the same value hash the same.
    assert epoch(settings(mcp_concurrency_wait_seconds=5.0)) == base
    assert epoch(settings(mcp_concurrency_wait_seconds="5")) == base


def test_provenance_names_live_mode_and_epoch():
    s = settings(mcp_concurrency_mode="queue")
    concurrency.reset_controller(s)
    assert provenance() == {"v": 2, "mode": "queue", "epoch": epoch(s)}


def test_startup_line_logs_effective_settings_and_epoch(caplog):
    s = settings(mcp_concurrency_mode="queue")
    with caplog.at_level("INFO", logger="src.services.concurrency"):
        concurrency.reset_controller(s)
    lines = [r.getMessage() for r in caplog.records
             if r.name == "src.services.concurrency" and "effective settings" in r.getMessage()]
    assert len(lines) == 1
    assert "mode=queue" in lines[0] and f"epoch={epoch(s)}" in lines[0]
    assert '"light": 4' in lines[0] and '"total": 14' in lines[0]


@pytest.mark.asyncio
async def test_snapshot_reports_live_counts():
    c = Controller(settings(mcp_concurrency_mode="enforce", mcp_concurrency_wait_seconds=1))
    held = await c.tool("semantic_search", 1, ("api_key", 1))
    waiter = asyncio.create_task(c.tool("semantic_search", 2, ("api_key", 2)))
    await asyncio.sleep(0)
    snap = c.snapshot()
    assert snap["mode"] == "enforce" and snap["epoch"] == c.epoch
    assert snap["pool_demand"]["total"] == 14
    assert snap["active"]["tools"] == 1 and snap["active"]["classes"]["embedding"] == 1
    assert snap["waiting"]["tools"] == 1 and snap["pending"] == 1
    assert snap["limits"]["light"] == 4
    held.lease.release()
    (await waiter).lease.release()
    assert c.snapshot()["active"]["tools"] == 0


# ── replay budget ────────────────────────────────────────────────────────

def test_replay_budget_never_refuses_a_consumed_message():
    concurrency.reset_controller(settings(mcp_concurrency_replay_budget_bytes=2 ** 20))
    budget = replay_budget()
    assert budget is replay_budget() and budget.capacity == 2 ** 20
    assert budget.try_reserve(1000) and not budget.exhausted
    # One message larger than the whole budget is still accounted (kept);
    # the answer tells the watcher to stop consuming.
    assert budget.try_reserve(2 ** 21) is False and budget.exhausted
    assert budget.used == 2 ** 21 + 1000
    budget.release(2 ** 21)
    assert not budget.exhausted and budget.used == 1000
    budget.release(10 ** 9)
    assert budget.used == 0


# ── pool budget ──────────────────────────────────────────────────────────

DEFAULT_CAPS = {"write": 1, "embedding": 1, "vector": 1, "scan": 2, "light": 4}


def test_class_connections():
    assert CLASS_CONNECTIONS == {"write": 2, "embedding": 1, "vector": 1, "scan": 1, "light": 1}
    assert POOL_CAPACITY == 15 and MCP_POOL_HEADROOM == 4


@pytest.mark.parametrize("tools,caps,expected", [
    (6, DEFAULT_CAPS, 7),                                   # write 1x2 + 5x1
    (8, dict(DEFAULT_CAPS, write=3), 11),                   # write 3x2 + 5x1
    (1, DEFAULT_CAPS, 2),                                   # the one slot goes to write
    (6, dict(DEFAULT_CAPS, write=6), 12),
    (15, DEFAULT_CAPS, 10),                                  # capped by the class sum (9)
    (4, {"light": 4}, 4),
    (0, DEFAULT_CAPS, 0),
])
def test_tool_demand_table(tools, caps, expected):
    assert tool_demand(tools, caps) == expected


def test_tool_demand_rejects_unknown_class():
    with pytest.raises(ValueError):
        tool_demand(4, {"other": 1})


def test_budget_terms_defaults_and_the_18_case():
    assert budget_terms(auth=2, tools=6, caps=DEFAULT_CAPS, writers=1)["total"] == 14
    over = budget_terms(auth=2, tools=8, caps=dict(DEFAULT_CAPS, write=3), writers=1)
    assert over == {"auth": 2, "tool_demand": 11, "writers": 1, "headroom": 4,
                    "total": 18, "capacity": 15}
