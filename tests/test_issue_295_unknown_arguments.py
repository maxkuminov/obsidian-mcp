"""Undeclared tool arguments are refused, on every tool (#295).

FastMCP 1.29 builds each tool's argument model on `ArgModelBase`, which sets no
`extra`, so pydantic's default `ignore` silently dropped any name a tool does
not declare: `keyword_search(folders=...)` returned unfiltered results the
agent believed were filtered. `_forbid_unknown_arguments` in
`src/mcp_server/server.py` swaps every tool's arg model for an
`extra="forbid"` subclass and publishes `additionalProperties: false`.

That reaches into SDK internals (`mcp._tool_manager`,
`Tool.fn_metadata.arg_model`, `Tool.parameters`). This module is the pin: an
SDK upgrade that moves any of them must fail here rather than quietly revert
to ignore. Every assertion iterates the live registry, so a tool added later
is covered without being listed.

No tool body ever runs: each call goes through `Tool.run` on a copy of the
tool whose `fn` is a recording sentinel, so no vault or database is touched.

Follows the setup convention of `tests/test_issue_150_docstrings.py`.
"""

import os
import subprocess
import sys
import tempfile
import textwrap
import types
import typing
from pathlib import Path

os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
os.chdir(tempfile.gettempdir())

import pytest  # noqa: E402
from mcp.server.fastmcp.exceptions import ToolError  # noqa: E402

from src.config import settings  # noqa: E402
from src.mcp_server.server import mcp  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

TOOLS = mcp._tool_manager.list_tools()
TOOL_NAMES = [t.name for t in TOOLS]


def test_setting_defaults_on():
    assert settings.mcp_reject_unknown_arguments is True


def test_every_tool_is_covered():
    # The registry the rest of this module iterates is the real one, not empty.
    assert len(TOOLS) == 25


class _Sentinel:
    """Stands in for a tool function; records whether it was ever reached."""

    def __init__(self):
        self.calls: list[dict] = []

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return "sentinel"


def _with_sentinel(tool):
    sentinel = _Sentinel()
    return tool.model_copy(update={"fn": sentinel}), sentinel


def _sample(annotation):
    """A value the declared `annotation` accepts — never `None`."""
    origin = typing.get_origin(annotation)
    if origin in (typing.Union, types.UnionType):
        return _sample(next(a for a in typing.get_args(annotation) if a is not type(None)))
    base = origin or annotation
    return {str: "x", int: 1, bool: False, dict: {}, list: []}[base]


def _allows_none(annotation):
    return type(None) in typing.get_args(annotation)


def _required_args(tool) -> dict:
    return {
        name: _sample(field.annotation)
        for name, field in tool.fn_metadata.arg_model.model_fields.items()
        if field.is_required()
    }


def _all_declared_args(tool) -> dict:
    """Every declared argument: optional ones as `None` where the type allows."""
    args = {}
    for name, field in tool.fn_metadata.arg_model.model_fields.items():
        if not field.is_required() and _allows_none(field.annotation):
            args[name] = None
        else:
            args[name] = _sample(field.annotation)
    return args


# ── 2.1 an extra argument is refused, naming it, before the body runs ────────


@pytest.mark.parametrize("tool", TOOLS, ids=TOOL_NAMES)
async def test_extra_argument_refused_before_body(tool):
    copy, sentinel = _with_sentinel(tool)
    with pytest.raises(ToolError) as exc:
        await copy.run({**_required_args(tool), "bogus_arg": 1})
    message = str(exc.value)
    assert "bogus_arg" in message
    assert "Extra inputs are not permitted" in message
    assert sentinel.calls == []


@pytest.mark.parametrize(
    "name, args, extra",
    [
        ("keyword_search", {"query": "x", "folders": "Projects/"}, "folders"),
        ("semantic_search", {"query": "x", "user_id": 2}, "user_id"),
    ],
)
async def test_spec_scenarios_refused(name, args, extra):
    copy, sentinel = _with_sentinel(mcp._tool_manager.get_tool(name))
    with pytest.raises(ToolError, match=extra):
        await copy.run(args)
    assert sentinel.calls == []


# ── 2.2 published schemas forbid extras ─────────────────────────────────────


async def test_listed_schemas_forbid_additional_properties():
    listed = await mcp.list_tools()
    assert sorted(t.name for t in listed) == sorted(TOOL_NAMES)
    for t in listed:
        assert t.inputSchema.get("additionalProperties") is False, t.name


@pytest.mark.parametrize("tool", TOOLS, ids=TOOL_NAMES)
def test_published_schema_matches_swapped_model(tool):
    # The patched `parameters` is exactly what the strict model would generate:
    # the swap changed nothing but `additionalProperties`.
    assert tool.parameters == tool.fn_metadata.arg_model.model_json_schema(by_alias=True)


@pytest.mark.parametrize("tool", TOOLS, ids=TOOL_NAMES)
def test_swapped_model_keeps_name_and_inherits_config(tool):
    model = tool.fn_metadata.arg_model
    assert model.__name__ == f"{tool.name}Arguments"
    assert model.model_config["extra"] == "forbid"
    assert model.model_config.get("arbitrary_types_allowed") is True


# ── 2.3 declared arguments still validate ────────────────────────────────────


@pytest.mark.parametrize("tool", TOOLS, ids=TOOL_NAMES)
async def test_required_arguments_still_validate(tool):
    copy, sentinel = _with_sentinel(tool)
    assert await copy.run(_required_args(tool)) == "sentinel"
    assert len(sentinel.calls) == 1


@pytest.mark.parametrize("tool", TOOLS, ids=TOOL_NAMES)
async def test_all_declared_arguments_still_validate(tool):
    copy, sentinel = _with_sentinel(tool)
    args = _all_declared_args(tool)
    assert await copy.run(args) == "sentinel"
    assert sentinel.calls == [args]


async def test_invalid_type_on_declared_argument_still_refused():
    copy, sentinel = _with_sentinel(mcp._tool_manager.get_tool("keyword_search"))
    with pytest.raises(ToolError, match="folder"):
        await copy.run({"query": "x", "folder": 123})
    assert sentinel.calls == []


# ── 2.4 rollback: MCP_REJECT_UNKNOWN_ARGUMENTS=false leaves the SDK alone ────


_ROLLBACK_SCRIPT = textwrap.dedent(
    """
    import asyncio

    from src.config import settings
    from src.mcp_server.server import mcp

    assert settings.mcp_reject_unknown_arguments is False

    async def main():
        tools = mcp._tool_manager.list_tools()
        assert len(tools) == 25
        for tool in tools:
            assert "additionalProperties" not in tool.parameters, tool.name
            assert tool.fn_metadata.arg_model.model_config.get("extra") is None, tool.name
        for listed in await mcp.list_tools():
            assert "additionalProperties" not in listed.inputSchema, listed.name

        seen = []

        async def sentinel(**kwargs):
            seen.append(kwargs)
            return "ok"

        tool = mcp._tool_manager.get_tool("keyword_search")
        copy = tool.model_copy(update={"fn": sentinel})
        assert await copy.run({"query": "x", "folders": "Projects/"}) == "ok"
        assert len(seen) == 1 and "folders" not in seen[0]

    asyncio.run(main())
    print("ROLLBACK-OK")
    """
)


def test_rollback_flag_restores_sdk_behaviour(tmp_path):
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "PYTHONPATH": str(ROOT),
        "DATABASE_URL": "postgresql+asyncpg://test:test@localhost/test",
        "SECRET_KEY": "test",
        "VAULT_PATH": str(tmp_path),
        "EMBEDDING_ALLOW_PLAINTEXT": "true",
        "MCP_REJECT_UNKNOWN_ARGUMENTS": "false",
    }
    result = subprocess.run(
        [sys.executable, "-c", _ROLLBACK_SCRIPT],
        # Not the repo: `Settings` reads a relative `.env`.
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "ROLLBACK-OK" in result.stdout


# ── 1.3 the rollback is logged at WARNING on start ───────────────────────────


@pytest.mark.parametrize("enabled, warned", [(True, False), (False, True)])
def test_rollback_logged_at_warning(monkeypatch, caplog, enabled, warned):
    import logging

    from src import main

    monkeypatch.setattr(main.settings, "mcp_reject_unknown_arguments", enabled)
    with caplog.at_level(logging.WARNING, logger=main.__name__):
        main._log_unknown_arguments_mode()
    hits = [r for r in caplog.records if "MCP_REJECT_UNKNOWN_ARGUMENTS" in r.getMessage()]
    assert bool(hits) is warned
    assert all(r.levelno == logging.WARNING for r in hits)
