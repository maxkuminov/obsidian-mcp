"""Which tools require write permission, and who sees them listed (#358).

`WRITE_REQUIRED_TOOLS` is the **single source** for two consumers:

* **enforcement** — `_tracked` (`src/mcp_server/tools.py`) runs
  `_require_write()` at the body boundary, after every admission gate, for a
  tool named here, and returns its refusal without running the body;
* **discovery** — the server's `list_tools()` override (`server.py`) drops
  these names from `tools/list` for an OAuth grant without `readwrite`.

One literal, so the list and the gate cannot drift, and nothing can append to
it (the #343 probe-pollution lesson). It must equal the write bucket
(`_tracked(write_class=True)`) plus `request_upload`, whose write charge is
taken at `PUT /transfer/upload` redemption rather than at the call;
`tests/test_issue_358_scoped_tools_list.py` reconciles it with the bucket, the
destructive annotations, the in-body `_require_write` call sites and the
refusals actually produced.

**This module imports nothing from the MCP SDK, directly or transitively** —
including when `hidden_names()` and `visible()` run — so the rule survives the
SDK 2.x rename (`FastMCP` → `MCPServer`) untouched. It operates on any objects
carrying a `.name`. Do not import `src.mcp_server.tools` (or anything that
does) from here.

The filter is a presentation of what a credential can use, not a control:
read-only enforcement stays `_require_write`, reached through `_tracked`.
See docs/architecture/vault-tools.md, "Who sees which tools (#358)".
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import TypeVar

T = TypeVar("T")

WRITE_REQUIRED_TOOLS: frozenset[str] = frozenset(
    {
        "create_note",
        "edit_note",
        "move_note",
        "delete_note",
        "set_frontmatter",
        "write_file",
        "delete_file",
        "import_from_url",
        "request_upload",
    }
)


def is_filtered(principal, permission) -> bool:
    """True only for an OAuth grant whose permission is not `readwrite`.

    `principal` is `current_principal` (`("api_key", id)`, `("oauth",
    grant_id)` or None); `permission` is `current_permission`, the very value
    `_require_write` reads, so the list and the refusal cannot disagree.
    API keys of either permission, sandbox mode and in-process callers (no
    principal) get the full list (owner decision, design D1/D3).
    """
    return (
        isinstance(principal, tuple)
        and len(principal) == 2
        and principal[0] == "oauth"
        and permission != "readwrite"
    )


def hidden_names() -> frozenset[str]:
    """The names a filtered list omits: the write-required registry itself."""
    return WRITE_REQUIRED_TOOLS


def visible(tools: Iterable[T], *, principal, permission) -> list[T]:
    """`tools` in their given order, minus the hidden names when filtered.

    Returns the same objects, so a kept entry is identical to its full-list
    entry. Depends on nothing but its arguments and the registry.
    """
    tools = list(tools)
    if not is_filtered(principal, permission):
        return tools
    hidden = hidden_names()
    return [tool for tool in tools if tool.name not in hidden]
