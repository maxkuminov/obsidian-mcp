"""#358 — `tools/list` scoped by the authorization presented on the request.

**The oracle.** `tests/fixtures/tools_list_full_v1.json` is the complete
`tools/list` result (all 28 tools, every serialised field) captured over the
Streamable HTTP transport for a `readwrite` API-key principal on the pre-change
tree (c491326 + spec commits, no `src/` change). It is the 1.0 contract-freeze
fixture: the 5a391ca / additions / amendments fixtures pin only names and
schemas. Comparison is byte identity of the canonical serialisation (UTF-8,
keys sorted, no insignificant whitespace); the file itself is stored indented
for review. Regenerate — only for a change that deliberately alters the
published surface and says so in its spec delta — with:

    REGEN_TOOLS_LIST_ORACLE=1 .venv/bin/python -m pytest \\
        tests/test_issue_358_scoped_tools_list.py -k oracle_capture

The HTTP harness is `tests/_scoped_tools_harness.py` (real transport, real
low-level server; authentication is the one stand-in, documented there).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import _scoped_tools_harness as h
from src.mcp_server.server import mcp

FIXTURES = Path(__file__).resolve().parent / "fixtures"
ORACLE = FIXTURES / "tools_list_full_v1.json"


def oracle() -> dict:
    return json.loads(ORACLE.read_text(encoding="utf-8"))


@pytest.fixture
def vault_root(tmp_path, monkeypatch):
    import src.mcp_server.tools as tools
    from src.services import vault as vault_service

    monkeypatch.setattr(tools.settings, "vault_path", str(tmp_path))
    monkeypatch.setattr(vault_service.settings, "vault_path", str(tmp_path))
    return tmp_path


@pytest.mark.skipif(not os.environ.get("REGEN_TOOLS_LIST_ORACLE"), reason="regeneration only")
async def test_oracle_capture(vault_root):
    async with h.running_client(mcp, vault_root) as client:
        result = await h.list_result(client, h.READWRITE_KEY)
    ORACLE.write_text(
        json.dumps(result, sort_keys=True, indent=1, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def test_the_oracle_is_the_complete_28_tool_result():
    result = oracle()
    names = [t["name"] for t in result["tools"]]
    assert len(names) == 28 and len(set(names)) == 28
    assert "nextCursor" not in result
    for tool in result["tools"]:
        assert {"name", "title", "description", "inputSchema", "annotations"} <= tool.keys()


@pytest.mark.parametrize(
    "cred", [h.READ_KEY, h.READWRITE_KEY, h.READWRITE_GRANT], ids=lambda c: c.bearer
)
async def test_the_full_list_is_byte_identical_to_the_oracle(cred, vault_root):
    async with h.running_client(mcp, vault_root) as client:
        result = await h.list_result(client, cred)
    assert h.canonical(result) == h.canonical(oracle())
