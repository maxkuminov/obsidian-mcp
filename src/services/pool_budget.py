"""Shared pool arithmetic; imports neither settings nor the database.

See design D2 of `concurrency-enforce-ready` (#188): class ceilings are
independent (each ≤ `tools`), and the connection multiplier is per class.
"""
from __future__ import annotations

from typing import Mapping

POOL_SIZE = 5
POOL_OVERFLOW = 10
POOL_CAPACITY = POOL_SIZE + POOL_OVERFLOW
MCP_POOL_HEADROOM = 4

# Connections one admitted tool of each class may hold at once. Pinned by the
# real-PG per-tool checkout-peak test (S2): a tool measuring above its class's
# multiplier must raise the multiplier here, not be waved through.
CLASS_CONNECTIONS: dict[str, int] = {
    "write": 2, "embedding": 1, "vector": 1, "scan": 1, "light": 1,
}


def tool_demand(tools: int, caps: Mapping[str, int]) -> int:
    """Maximum Σ multiplier(c)·n(c) over n(c) ≤ caps[c], Σ n(c) ≤ tools.

    Greedy fill, highest multiplier first, is exact for this bounded-knapsack
    shape (unit weights, per-class caps). Pure: no settings, no state.
    """
    unknown = set(caps) - set(CLASS_CONNECTIONS)
    if unknown:
        raise ValueError(f"unknown concurrency class(es): {sorted(unknown)}")
    remaining = max(0, int(tools))
    demand = 0
    for cls in sorted(caps, key=lambda c: (-CLASS_CONNECTIONS[c], c)):
        n = min(max(0, int(caps[cls])), remaining)
        demand += n * CLASS_CONNECTIONS[cls]
        remaining -= n
    return demand


def budget_terms(*, auth: int, tools: int, caps: Mapping[str, int],
                 writers: int) -> dict:
    """Every term of the validated sum `auth + tool_demand + writers + headroom`."""
    demand = tool_demand(tools, caps)
    total = auth + demand + writers + MCP_POOL_HEADROOM
    return {"auth": auth, "tool_demand": demand, "writers": writers,
            "headroom": MCP_POOL_HEADROOM, "total": total,
            "capacity": POOL_CAPACITY}
