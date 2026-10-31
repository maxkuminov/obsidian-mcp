"""#358 — `tools/list` scoped by the authorization presented on the request.

An OAuth grant without `readwrite` lists 19 tools: the full list minus the
nine names in `tool_permissions.WRITE_REQUIRED_TOOLS`, the one registry that
also drives `_tracked`'s body-boundary write gate. Everyone else — API keys of
either permission, `readwrite` grants, sandbox mode, in-process callers —
lists all 28, byte-identical to before. Enforcement is unchanged in outcome.

**The oracle.** `tests/fixtures/tools_list_full_v1.json` is the complete
`tools/list` result (all 28 tools, every serialised field) captured over the
Streamable HTTP transport for a `readwrite` API-key principal on the pre-change
tree (c491326 + spec commits, no `src/` change; committed alone, before the
code). It is the 1.0 contract-freeze fixture: the 5a391ca / additions /
amendments fixtures pin only names and schemas. Comparison is byte identity of
the canonical serialisation (UTF-8, keys sorted, no insignificant whitespace);
the file itself is stored indented for review. Regenerate — only for a change
that deliberately alters the published surface and says so in its spec delta —
with:

    REGEN_TOOLS_LIST_ORACLE=1 .venv/bin/python -m pytest \\
        tests/test_issue_358_scoped_tools_list.py -k oracle_capture

The HTTP harness is `tests/_scoped_tools_harness.py` (real transport, real
low-level server; authentication is the one stand-in, documented there). The
real `APIKeyMiddleware` is used where it is the thing under test (401, sandbox).
"""
from __future__ import annotations

import ast
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import _scoped_tools_harness as h
import src.mcp_server.server as server
import src.mcp_server.tools as tools
from src.auth.session import current_principal
from src.mcp_server import tool_permissions
from src.mcp_server.auth import current_permission
from src.mcp_server.server import SERVER_INSTRUCTIONS, mcp
from src.services import rate_limits

FIXTURES = Path(__file__).resolve().parent / "fixtures"
ORACLE = FIXTURES / "tools_list_full_v1.json"
READONLY = FIXTURES / "tools_list_readonly_oauth.json"
REPO = Path(__file__).resolve().parent.parent

NINE = {
    "create_note", "edit_note", "move_note", "delete_note", "set_frontmatter",
    "write_file", "delete_file", "import_from_url", "request_upload",
}


def oracle() -> dict:
    return json.loads(ORACLE.read_text(encoding="utf-8"))


def readonly_names() -> list[str]:
    return json.loads(READONLY.read_text(encoding="utf-8"))


def refusal_code(message: dict) -> str | None:
    """The `MCP-REFUSAL` code of a tools/call response, if it carries one."""
    result = message.get("result") or {}
    for block in result.get("content", []):
        for line in block.get("text", "").splitlines():
            if line.startswith("MCP-REFUSAL "):
                return json.loads(line[len("MCP-REFUSAL "):]).get("code")
    return None


@pytest.fixture
def vault_root(tmp_path, monkeypatch):
    from src.services import vault as vault_service

    monkeypatch.setattr(tools.settings, "vault_path", str(tmp_path))
    monkeypatch.setattr(vault_service.settings, "vault_path", str(tmp_path))

    async def no_usage(*a, **kw):
        return True

    monkeypatch.setattr(tools, "_log_usage", no_usage)
    return tmp_path


# ══════════════════════════════════════════════════════════════════════════
# 1. The oracle and byte identity of the full list
# ══════════════════════════════════════════════════════════════════════════


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


# ══════════════════════════════════════════════════════════════════════════
# 2. The filtered list
# ══════════════════════════════════════════════════════════════════════════


def test_the_readonly_fixture_is_the_oracle_minus_the_nine():
    expected = [t["name"] for t in oracle()["tools"] if t["name"] not in NINE]
    assert readonly_names() == expected
    assert len(expected) == 19


@pytest.mark.parametrize("cred", [h.READ_GRANT, h.READ_GRANT_2], ids=lambda c: c.bearer)
async def test_a_readonly_grant_lists_19_entries_identical_to_the_oracle(cred, vault_root):
    by_name = {t["name"]: t for t in oracle()["tools"]}
    async with h.running_client(mcp, vault_root) as client:
        result = await h.list_result(client, cred)
    assert [t["name"] for t in result["tools"]] == readonly_names()
    for entry in result["tools"]:
        assert h.canonical(entry) == h.canonical(by_name[entry["name"]]), entry["name"]
    assert "nextCursor" not in result


@pytest.mark.parametrize(
    "cred,count",
    [(h.READ_GRANT, 19), (h.READ_KEY, 28), (h.READWRITE_KEY, 28), (h.READWRITE_GRANT, 28)],
    ids=lambda v: getattr(v, "bearer", str(v)),
)
async def test_a_cursor_does_not_change_the_result(cred, count, vault_root):
    async with h.running_client(mcp, vault_root) as client:
        plain = await h.list_result(client, cred)
        cursored = await h.list_result(client, cred, {"cursor": "anything-at-all"})
    assert h.canonical(cursored) == h.canonical(plain)
    assert len(plain["tools"]) == count
    assert "nextCursor" not in cursored


def _set(principal, permission):
    return current_principal.set(principal), current_permission.set(permission)


def _reset(tokens):
    p, q = tokens
    current_permission.reset(q)
    current_principal.reset(p)


@pytest.mark.parametrize(
    "principal,permission,count",
    [
        (("oauth", "g"), "read", 19),
        (("oauth", "g"), "readwrite", 28),
        (("api_key", 1), "read", 28),
        (("api_key", 1), "readwrite", 28),
        (None, "read", 28),
        (None, "readwrite", 28),
    ],
)
async def test_in_process_listing_per_principal(principal, permission, count):
    tokens = _set(principal, permission)
    try:
        listed = await mcp.list_tools()
    finally:
        _reset(tokens)
    assert len(listed) == count
    if count == 28:
        # The same order and schemas as the oracle and the #343 fixtures.
        assert [t.name for t in listed] == [t["name"] for t in oracle()["tools"]]
        for tool, row in zip(listed, oracle()["tools"]):
            assert tool.inputSchema == row["inputSchema"]
            assert tool.outputSchema == row.get("outputSchema")
    else:
        assert [t.name for t in listed] == readonly_names()


def test_is_filtered_truth_table():
    f = tool_permissions.is_filtered
    assert f(("oauth", "g"), "read") is True
    assert f(("oauth", "g"), "readwrite") is False
    assert f(("api_key", 1), "read") is False
    assert f(("api_key", 1), "readwrite") is False
    assert f(None, "read") is False
    assert f(None, "readwrite") is False


def test_visible_preserves_order_and_identity():
    class T:
        def __init__(self, name):
            self.name = name

    items = [T(n) for n in ("read_note", "edit_note", "keyword_search", "request_upload", "list_tasks")]
    kept = tool_permissions.visible(items, principal=("oauth", "g"), permission="read")
    assert [t.name for t in kept] == ["read_note", "keyword_search", "list_tasks"]
    assert all(any(k is i for i in items) for k in kept)
    full = tool_permissions.visible(items, principal=("api_key", 1), permission="read")
    assert full == items and all(a is b for a, b in zip(full, items))


async def test_sequential_lists_are_deterministic(vault_root):
    async with h.running_client(mcp, vault_root) as client:
        first = await h.list_result(client, h.READ_GRANT)
        seen = []
        for i in range(10):
            seen.append(await h.list_result(client, h.READ_GRANT))
            if i % 3 == 0:
                full = await h.list_result(client, h.READWRITE_GRANT)
                assert h.canonical(full) == h.canonical(oracle())
        other = await h.list_result(client, h.READ_GRANT_2)
    assert all(h.canonical(s) == h.canonical(first) for s in seen)
    assert h.canonical(other) == h.canonical(first)


async def test_the_list_follows_a_scope_change_on_the_next_request(vault_root):
    upgraded = h.Credential("oauth", "readwrite", h.READ_GRANT.ident)
    h.ALL[upgraded.bearer] = upgraded
    try:
        async with h.running_client(mcp, vault_root) as client:
            before = await h.list_result(client, h.READ_GRANT)
            after = await h.list_result(client, upgraded)
    finally:
        h.ALL.pop(upgraded.bearer, None)
    assert len(before["tools"]) == 19
    assert h.canonical(after) == h.canonical(oracle())


async def test_initialize_never_advertises_list_changed_and_instructions_are_unfiltered(vault_root):
    params = {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "t", "version": "0"},
    }
    async with h.running_client(mcp, vault_root) as client:
        results = {}
        for cred in (h.READ_GRANT, h.READ_KEY):
            response = await h.post(client, cred, h.rpc("initialize", params))
            results[cred.bearer] = h.payload(response)["result"]
    for result in results.values():
        assert result["instructions"] == SERVER_INSTRUCTIONS
        assert result["capabilities"]["tools"].get("listChanged") is False
    a, b = results.values()
    assert a["instructions"] == b["instructions"]


# ══════════════════════════════════════════════════════════════════════════
# 3. The real middleware: no credential never lists; sandbox lists everything
# ══════════════════════════════════════════════════════════════════════════


class _Spy:
    def __init__(self):
        self.calls = 0


async def test_no_credential_is_refused_before_the_handler(monkeypatch):
    import httpx

    from src.mcp_server.auth import APIKeyMiddleware

    spy = _Spy()

    async def downstream(scope, receive, send):  # pragma: no cover - must not run
        spy.calls += 1

    monkeypatch.setattr(server.settings, "mcp_sandbox_mode", False)
    app = APIKeyMiddleware(downstream)
    transport = httpx.ASGITransport(app=app, client=("203.0.113.9", 4000))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/mcp/", json=h.rpc("tools/list"), headers=h.JSON_HEADERS)
    assert response.status_code == 401
    assert spy.calls == 0


async def test_sandbox_mode_lists_all_28(monkeypatch, vault_root):
    """The real middleware in sandbox mode passes straight through with no
    principal set, so the registry introspection a sandbox exists for sees
    every tool."""
    import httpx
    from mcp.server.fastmcp.server import StreamableHTTPASGIApp
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

    from src.mcp_server.auth import APIKeyMiddleware

    monkeypatch.setattr(server.settings, "mcp_sandbox_mode", True)
    manager = StreamableHTTPSessionManager(app=mcp._mcp_server, stateless=True)
    app = APIKeyMiddleware(StreamableHTTPASGIApp(manager))
    async with manager.run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post("/", json=h.rpc("tools/list"), headers=h.JSON_HEADERS)
    result = h.payload(response)["result"]
    assert h.canonical(result) == h.canonical(oracle())


# ══════════════════════════════════════════════════════════════════════════
# 4. Enforcement unchanged (design D6)
# ══════════════════════════════════════════════════════════════════════════

REFUSAL_PROSE = (
    "Permission denied: this credential has read-only access. Write "
    "permission is required — a 'readwrite' API key, or an OAuth token "
    "carrying the 'readwrite' scope."
)

HIDDEN_CALLS = {
    "edit_note": {"path": "a.md", "content": "x"},
    "delete_note": {"path": "a.md"},
    "request_upload": {"path": "a.bin"},
    "import_from_url": {"url": "https://example.com/x", "path": "a.bin"},
}


@pytest.mark.parametrize("prime", ["full", "filtered"])
async def test_a_named_hidden_tool_is_refused_the_same_way_in_either_cache_state(prime, vault_root):
    responses = {}
    async with h.running_client(mcp, vault_root) as client:
        await h.list_result(client, h.READWRITE_KEY if prime == "full" else h.READ_GRANT)
        for name, args in HIDDEN_CALLS.items():
            message = await h.call_tool(client, h.READ_GRANT, name, args)
            assert "error" not in message, "a tool result, never an unknown-tool protocol error"
            assert refusal_code(message) == "permission_denied", (name, message)
            text = message["result"]["content"][0]["text"]
            assert text.startswith(REFUSAL_PROSE)
            responses[name] = h.canonical(message["result"])
    test_a_named_hidden_tool_is_refused_the_same_way_in_either_cache_state.seen.setdefault(prime, responses)
    seen = test_a_named_hidden_tool_is_refused_the_same_way_in_either_cache_state.seen
    if len(seen) == 2:
        assert seen["full"] == seen["filtered"]


test_a_named_hidden_tool_is_refused_the_same_way_in_either_cache_state.seen = {}


@pytest.fixture
def read_only_in_process(vault_root):
    from src.auth.session import current_user_id, current_vault_root

    tokens = [
        (current_permission, current_permission.set("read")),
        (current_principal, current_principal.set(("oauth", "g"))),
        (current_user_id, current_user_id.set(7)),
        (current_vault_root, current_vault_root.set((7, vault_root))),
    ]
    yield vault_root
    for var, tok in reversed(tokens):
        var.reset(tok)


@pytest.mark.parametrize(
    "call",
    [
        lambda: tools.write_file_impl("a.bin", "AAAA", expected_hash="not-a-hash"),
        lambda: tools.delete_file_impl("a.bin", expected_hash="not-a-hash"),
    ],
    ids=["write_file", "delete_file"],
)
async def test_l5_malformed_hash_from_a_readonly_caller_is_permission_denied(call, read_only_in_process):
    result = await call()
    assert result.refusal.code == "permission_denied"
    perm = current_permission.set("readwrite")
    try:
        again = await call()
    finally:
        current_permission.reset(perm)
    assert again.refusal is not None and again.refusal.code != "permission_denied", (
        "a readwrite caller still gets the syntax refusal"
    )


# ══════════════════════════════════════════════════════════════════════════
# 5. One registry (design D2)
# ══════════════════════════════════════════════════════════════════════════


def test_the_registry_is_the_nine():
    assert tool_permissions.WRITE_REQUIRED_TOOLS == NINE
    assert tool_permissions.hidden_names() is tool_permissions.WRITE_REQUIRED_TOOLS


def test_the_registry_is_the_write_bucket_plus_request_upload():
    assert tool_permissions.WRITE_REQUIRED_TOOLS == tools.production_write_bucket_tools() | {"request_upload"}


def test_the_registry_is_the_destructive_set_plus_create_note():
    destructive = {t["name"] for t in oracle()["tools"] if t["annotations"]["destructiveHint"]}
    assert tool_permissions.WRITE_REQUIRED_TOOLS == destructive | {"create_note"}
    assert "request_download" not in tool_permissions.WRITE_REQUIRED_TOOLS
    assert "check_upload" not in tool_permissions.WRITE_REQUIRED_TOOLS
    registered = {t.name for t in mcp._tool_manager.list_tools()}
    assert tool_permissions.WRITE_REQUIRED_TOOLS <= registered


def _minimal_args(tool) -> dict:
    """A value for every required argument, by declared type."""
    args = {}
    schema = tool.parameters
    for name in schema.get("required", []):
        prop = schema["properties"][name]
        kind = prop.get("type") or next(
            (o.get("type") for o in prop.get("anyOf", []) if o.get("type") != "null"), "string"
        )
        args[name] = {
            "string": "https://example.com/x" if name == "url" else "a.md",
            "integer": 1,
            "boolean": False,
            "array": [],
            "object": {"k": "v"},
            "number": 1.0,
        }.get(kind, "a.md")
    return args


async def test_every_tool_under_a_readonly_grant_is_refused_iff_in_the_registry(read_only_in_process, monkeypatch):
    """All 28, called through the server with a read-only OAuth principal.
    Anything that would reach the database fails fast instead; only the
    `permission_denied` set matters here."""

    class _NoDB:
        def __call__(self, *a, **kw):
            raise RuntimeError("no database in this test")

    monkeypatch.setattr(tools, "async_session", _NoDB())
    denied = set()
    for tool in mcp._tool_manager.list_tools():
        try:
            content = await mcp.call_tool(tool.name, _minimal_args(tool))
        except Exception:  # noqa: BLE001 - read tools may fail without a DB
            continue
        blocks = content[0] if isinstance(content, tuple) else content
        text = "".join(getattr(b, "text", "") for b in blocks)
        if '"code":"permission_denied"' in text.replace(" ", ""):
            denied.add(tool.name)
    assert denied == tool_permissions.WRITE_REQUIRED_TOOLS


@tools._tracked("probe_gate_358", [], resource_class="light")
async def probe_gate_358() -> str:
    """No in-body permission check: only the body-boundary gate can refuse it."""
    return "ran"


async def test_the_gate_reads_the_registry(read_only_in_process, monkeypatch):
    assert await probe_gate_358() == "ran"
    monkeypatch.setattr(
        tool_permissions, "WRITE_REQUIRED_TOOLS",
        tool_permissions.WRITE_REQUIRED_TOOLS | {"probe_gate_358"},
    )
    refused = await probe_gate_358()
    assert refused.refusal.code == "permission_denied"

    class T:
        name = "probe_gate_358"

    assert tool_permissions.visible([T()], principal=("oauth", "g"), permission="read") == []


async def test_a_member_removed_from_the_registry_is_still_refused_in_its_body(read_only_in_process, monkeypatch):
    monkeypatch.setattr(
        tool_permissions, "WRITE_REQUIRED_TOOLS",
        tool_permissions.WRITE_REQUIRED_TOOLS - {"delete_note"},
    )
    result = await tools.delete_note_impl("a.md")
    assert result.refusal.code == "permission_denied"


def _tracked_name(node: ast.AST) -> str | None:
    for deco in getattr(node, "decorator_list", []):
        if isinstance(deco, ast.Call) and getattr(deco.func, "id", None) == "_tracked":
            if deco.args and isinstance(deco.args[0], ast.Constant):
                return deco.args[0].value
    return None


def _own_calls(fn: ast.AST):
    """Calls in `fn`'s own body, not in functions nested inside it."""
    stack = list(fn.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(node, ast.Call):
            yield node
        stack.extend(ast.iter_child_nodes(node))


def test_every_in_body_write_check_belongs_to_a_registry_member():
    tree = ast.parse((REPO / "src" / "mcp_server" / "tools.py").read_text(encoding="utf-8"))
    sites: list[tuple[str, str | None]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for call in _own_calls(node):
            if not isinstance(call, ast.Call):
                continue
            fname = getattr(call.func, "id", None)
            write_preflight = fname == "_mint_preflight" and any(
                k.arg == "need_write" and isinstance(k.value, ast.Constant) and k.value.value is True
                for k in call.keywords
            )
            if fname == "_require_write" or write_preflight:
                sites.append((node.name, _tracked_name(node)))
    owners = {name for _, name in sites if name is not None}
    helpers = {fn for fn, name in sites if name is None}
    # The gate in `_tracked`'s wrapper, and `_mint_preflight`'s own check.
    assert helpers == {"wrapper", "_mint_preflight"}, helpers
    assert owners == tool_permissions.WRITE_REQUIRED_TOOLS


def test_the_rule_and_registry_import_no_sdk_even_when_invoked():
    code = (
        "import sys\n"
        f"sys.path.insert(0, {str(REPO)!r})\n"
        "from src.mcp_server import tool_permissions as t\n"
        "class T:\n"
        "    def __init__(self, n): self.name = n\n"
        "assert 'edit_note' in t.hidden_names()\n"
        "kept = t.visible([T('edit_note'), T('read_note')], principal=('oauth', 'g'), permission='read')\n"
        "assert [k.name for k in kept] == ['read_note']\n"
        "bad = sorted(m for m in sys.modules if m == 'mcp' or m.startswith('mcp.'))\n"
        "print(','.join(bad))\n"
    )
    out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, cwd=str(REPO))
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == ""


def test_visible_has_one_production_caller():
    callers = []
    for path in (REPO / "src").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if path.name != "tool_permissions.py" and "visible(" in text and "tool_permissions" in text:
            callers.append(path.name)
    assert callers == ["server.py"]


# ══════════════════════════════════════════════════════════════════════════
# 6. Concurrency (design D6, Codex round 1)
# ══════════════════════════════════════════════════════════════════════════

ROUNDS = 50


async def _round(client, validated, edit_schema):
    barrier = asyncio.Barrier(5)

    async def go(coro_fn):
        await barrier.wait()
        return await coro_fn()

    before = validated.count(edit_schema["title"])
    ro_list, key_list, rw_list, ro_call, rw_call = await asyncio.gather(
        go(lambda: h.list_result(client, h.READ_GRANT)),
        go(lambda: h.list_result(client, h.READWRITE_KEY)),
        go(lambda: h.list_result(client, h.READWRITE_GRANT)),
        go(lambda: h.call_tool(client, h.READ_GRANT, "delete_note", {"path": "n.md"})),
        go(lambda: h.call_tool(client, h.READWRITE_GRANT, "edit_note",
                               {"path": "n.md", "content": "y", "dry_run": True})),
    )
    assert [t["name"] for t in ro_list["tools"]] == readonly_names()
    assert h.canonical(key_list) == h.canonical(oracle())
    assert h.canonical(rw_list) == h.canonical(oracle())
    assert refusal_code(ro_call) == "permission_denied"
    assert refusal_code(rw_call) is None, rw_call
    assert rw_call["result"].get("isError") is not True
    assert validated.count(edit_schema["title"]) > before, "the readwrite call was output-validated"


@pytest.fixture
def output_validation_spy(monkeypatch):
    import mcp.server.lowlevel.server as lowlevel

    validated: list[str] = []
    real = lowlevel.jsonschema.validate

    def spy(instance, schema, *a, **kw):
        validated.append(schema.get("title") if isinstance(schema, dict) else None)
        return real(instance, schema, *a, **kw)

    monkeypatch.setattr(lowlevel.jsonschema, "validate", spy)
    return validated


@pytest.mark.parametrize("prime", ["full", "filtered"])
@pytest.mark.parametrize("injected_yield", [False, True], ids=["plain", "yield"])
async def test_concurrent_requests_each_get_their_own_answer(
    prime, injected_yield, vault_root, output_validation_spy, monkeypatch
):
    (vault_root / "n.md").write_text("x\n", encoding="utf-8")
    if injected_yield:
        # The SDK registered the bound override at construction, so the yield is
        # injected one level down: `super().list_tools()` resolves through the
        # MRO at call time, and a suspension there sits inside the override,
        # before it reads the principal.
        from mcp.server.fastmcp import FastMCP

        original = FastMCP.list_tools

        async def yielding(self):
            await asyncio.sleep(0)
            full = await original(self)
            await asyncio.sleep(0)
            return full

        monkeypatch.setattr(FastMCP, "list_tools", yielding)
    edit_schema = mcp._tool_manager.get_tool("edit_note").output_schema
    async with h.running_client(mcp, vault_root) as client:
        for _ in range(ROUNDS):
            # The write bucket (burst 30) would otherwise refuse the read-only
            # delete_note as `rate_limited` before round 31; reset between
            # rounds, never inside one.
            rate_limits.reset_state_for_tests()
            await h.list_result(client, h.READWRITE_KEY if prime == "full" else h.READ_GRANT)
            await _round(client, output_validation_spy, edit_schema)
