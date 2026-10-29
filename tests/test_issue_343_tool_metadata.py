"""Tool metadata: titles, annotations, server instructions, description cap (#343).

What an MCP client learns about a tool before calling it is exactly what
`tools/list` and `initialize` carry: `title`, `annotations`, `description`,
`inputSchema`, and the server `instructions`. This module pins that surface:

- every tool carries a title and all four hints as explicit booleans, so no
  client falls back to the protocol's worst-case defaults;
- the classification is reconciled against the *code* — the write-bucket set
  `_tracked(write_class=True)` defines, via `production_write_bucket_tools()`,
  and `concurrency.TOOL_CLASSES` — never against a list kept here;
- the instructions fit their two limits and their lead carries the contract,
  with the read→write round trip stated under its conditions;
- no description exceeds 4,000 characters on the wire, and the four longest
  lead with their contract;
- the tool surface is otherwise unchanged: names, order, `inputSchema` and
  `outputSchema` equal a fixture generated on `main` at 5a391ca.

Everything is read from the server's own registry (#89's reason): the string a
client receives is the contract, not the module function a test might follow.

Regenerating the fixture (only when a schema change is *intended*):

    OLLAMA_URL=http://127.0.0.1:11434 .venv/bin/python - <<'EOF'
    import asyncio, json, os, sys, tempfile
    os.environ.setdefault("SECRET_KEY", "test")
    os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
    os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
    sys.path.insert(0, os.getcwd()); os.chdir(tempfile.gettempdir())
    from src.mcp_server.server import mcp
    rows = [{"name": t.name, "inputSchema": t.inputSchema, "outputSchema": t.outputSchema}
            for t in asyncio.run(mcp.list_tools())]
    json.dump(rows, open(sys.argv[1], "w"), indent=2, sort_keys=True, ensure_ascii=False)
    EOF

Follows the setup convention of `tests/test_issue_150_docstrings.py`: minimal
env defaults and a chdir away from any `.env` BEFORE importing the server.
"""

import json
import os
import re
import tempfile
from pathlib import Path

os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
os.chdir(tempfile.gettempdir())

import pytest  # noqa: E402
from mcp.types import ToolAnnotations  # noqa: E402

import src.mcp_server.tools as tools  # noqa: E402
from src.mcp_server import server  # noqa: E402
from src.mcp_server.server import mcp  # noqa: E402
from src.services import concurrency  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "tools_list_schema_5a391ca.json"

DESCRIPTION_CAP = 4_000
LEAD_CHARS = 1_500
INSTRUCTIONS_CAP = 2_000
INSTRUCTIONS_LEAD = 512

# The fifteen tools that neither change vault bytes nor mint a capability. Named
# explicitly so a read tool added without annotations fails on its name, not
# only on a count.
READ_TOOLS = frozenset({
    "keyword_search", "semantic_search", "read_note", "list_notes", "get_tags",
    "get_recent", "get_vault_guide", "read_file", "list_files", "get_backlinks",
    "get_links", "get_neighborhood", "find_related", "find_orphans", "check_upload",
})
RESTRUCTURED_WRITE_TOOLS = ("edit_note", "move_note", "set_frontmatter")
# The refusal statement is qualified in every description that makes one: the
# three restructured tools plus `create_note` and `delete_note`, whose
# pre-existing sentence had the same inaccuracy (truth fix found in review).
REFUSAL_QUALIFIED_TOOLS = RESTRUCTURED_WRITE_TOOLS + ("create_note", "delete_note")


# ── pollution probes ─────────────────────────────────────────────────────────
#
# The same shape as `write_probe` in tests/test_concurrency_provenance.py and
# `rate_probe_write` in tests/test_mcp_rate_limits.py, decorated at module
# scope so they exist for the whole run. Neither may move the production
# write-bucket set: one carries a non-production name, the other a production
# name with the opposite flag.


@tools._tracked("probe_write_343", [], write_class=True, resource_class="write")
async def _probe_write():
    return "written"


@tools._tracked("edit_note", [], write_class=False, resource_class="write")
async def _probe_named_like_production():
    return "not edit_note"


# ── helpers ──────────────────────────────────────────────────────────────────


def listed():
    return mcp._tool_manager.list_tools()


def registered(name):
    for tool in listed():
        if tool.name == name:
            return tool
    raise AssertionError(f"{name} is not registered")


def description(name) -> str:
    doc = registered(name).description or ""
    assert doc.strip(), f"{name} is registered with an empty description"
    return doc


def registered_names() -> frozenset[str]:
    return frozenset(t.name for t in listed())


def first_words() -> frozenset[str]:
    return frozenset(name.split("_")[0] for name in registered_names())


_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"
_SPAN = re.compile(r"`([^`\n]*)`")
_CALL = re.compile(rf"\A({_IDENT})\(")
_DOTTED = re.compile(rf"\A({_IDENT})\.{_IDENT}")
_BARE = re.compile(rf"\A({_IDENT})\Z")


def tool_shaped_references(text: str, words: frozenset[str]) -> set[str]:
    """Backticked spans that are shaped like a tool reference.

    A span counts when it is in call form (``name(``), when it is dotted
    (``name.field``) and its head is tool-shaped, or when it is a bare
    identifier that contains an underscore and whose first ``_``-delimited
    word is the first word of a registered tool's name. Argument and field
    names (`content`, `expected_hash`, `find`) are not counted: none begins
    with a registered first word *and* contains an underscore — the underscore
    rule exists for `find`, which shares its first word with `find_orphans`.
    """
    found: set[str] = set()
    for span in _SPAN.findall(text):
        call = _CALL.match(span)
        if call:
            found.add(call.group(1))
            continue
        dotted = _DOTTED.match(span)
        bare = _BARE.match(span)
        head = (dotted or bare).group(1) if (dotted or bare) else None
        if head and "_" in head and head.split("_")[0] in words:
            found.add(head)
    return found


# ── 1. completeness ──────────────────────────────────────────────────────────


def test_exactly_25_tools():
    assert len(listed()) == 25


@pytest.mark.parametrize("tool", listed(), ids=lambda t: t.name)
def test_every_tool_has_a_title_and_four_explicit_hints(tool):
    assert isinstance(tool.title, str) and tool.title.strip(), tool.name
    assert isinstance(tool.annotations, ToolAnnotations), tool.name
    for hint in ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"):
        value = getattr(tool.annotations, hint)
        assert value is True or value is False, f"{tool.name}.{hint} is {value!r}"
    assert tool.annotations.title is None or tool.annotations.title == tool.title


def test_titles_are_distinct():
    titles = [t.title for t in listed()]
    assert len(set(titles)) == len(titles), titles


# ── 2. the classification reconciles with the code ───────────────────────────


def _set(predicate) -> frozenset[str]:
    return frozenset(t.name for t in listed() if predicate(t.annotations))


def test_the_read_tools_are_read_only_idempotent_and_closed():
    for name in sorted(READ_TOOLS):
        a = registered(name).annotations
        assert (a.readOnlyHint, a.destructiveHint, a.idempotentHint, a.openWorldHint) == (
            True, False, True, False
        ), name
    assert _set(lambda a: a.readOnlyHint) == READ_TOOLS


def test_the_write_bucket_set_is_the_production_one():
    w = tools.production_write_bucket_tools()
    assert w == frozenset(t for t, c in concurrency.TOOL_CLASSES.items() if c == "write")
    assert w <= registered_names()
    assert len(w) == 8


def test_destructive_is_the_write_bucket_minus_create_note_plus_request_upload():
    w = tools.production_write_bucket_tools()
    assert _set(lambda a: a.destructiveHint) == (w - {"create_note"}) | {"request_upload"}
    assert _set(lambda a: not a.readOnlyHint) == w | {"request_upload", "request_download"}
    assert _set(lambda a: a.openWorldHint) == {"import_from_url"}
    assert _set(lambda a: a.idempotentHint) == _set(lambda a: a.readOnlyHint)
    for name in ("create_note", "request_download"):
        a = registered(name).annotations
        assert (a.readOnlyHint, a.destructiveHint) == (False, False), name
    a = registered("import_from_url").annotations
    assert (a.destructiveHint, a.openWorldHint) == (True, True)


def test_a_test_probe_cannot_pollute_the_write_bucket_set():
    """Both probes are stamped exactly like a production tool — so the guard
    is the enumeration rule, not an accident of the probes being unmarked."""
    assert _probe_write.__tracked_tool__ == "probe_write_343"
    assert _probe_write.__write_class__ is True
    assert _probe_named_like_production.__tracked_tool__ == "edit_note"
    assert _probe_named_like_production.__write_class__ is False
    w = tools.production_write_bucket_tools()
    assert "probe_write_343" not in w
    assert "edit_note" in w
    assert w == frozenset({
        "create_note", "edit_note", "move_note", "delete_note", "set_frontmatter",
        "write_file", "delete_file", "import_from_url",
    })


# ── 3. instructions ──────────────────────────────────────────────────────────


def test_instructions_come_from_the_constant_and_fit():
    assert mcp.instructions is server.SERVER_INSTRUCTIONS
    assert len(server.SERVER_INSTRUCTIONS) <= INSTRUCTIONS_CAP


@pytest.mark.parametrize(
    "token",
    [
        "memory", "markdown", "`keyword_search`", "`semantic_search`", "`read_note`",
        "`create_note`", "`edit_note`", "dry_run", "`get_vault_guide`",
        "`expected_hash`", "optional", "overwrite",
    ],
)
def test_the_instructions_lead_carries_the_contract(token):
    lead = server.SERVER_INSTRUCTIONS[:INSTRUCTIONS_LEAD].lower()
    assert token.lower() in lead, token


def test_the_instructions_state_the_round_trip_with_its_conditions():
    text = server.SERVER_INSTRUCTIONS
    assert "truncated=false" in text
    assert "offset=0" in text
    assert "same `section=`" in text
    assert "truncated read" in text
    assert "back to `edit_note` unchanged" not in text


def test_the_instructions_frame_the_guide_neutrally():
    lowered = server.SERVER_INSTRUCTIONS.lower()
    assert "`get_vault_guide`" in lowered
    assert "must call" not in lowered
    assert "important" not in lowered


def test_the_instructions_name_only_registered_tools():
    found = tool_shaped_references(server.SERVER_INSTRUCTIONS, first_words())
    assert found, "the instructions name no tool at all"
    assert found <= registered_names(), sorted(found - registered_names())


# ── 4. description cap and lead ──────────────────────────────────────────────


@pytest.mark.parametrize("tool", listed(), ids=lambda t: t.name)
def test_no_description_exceeds_the_cap(tool):
    assert len(tool.description or "") <= DESCRIPTION_CAP, (
        f"{tool.name}: {len(tool.description or '')} characters on the wire"
    )


@pytest.mark.parametrize("name", RESTRUCTURED_WRITE_TOOLS)
def test_the_write_tools_lead_with_their_contract(name):
    tool = registered(name)
    lead = (tool.description or "")[:LEAD_CHARS]
    for prop in tool.parameters["properties"]:
        assert prop in lead, f"{name}: argument {prop!r} is not in the first {LEAD_CHARS} chars"
    for token in ("readwrite", "expected_hash", "MCP-REFUSAL"):
        assert token in lead, f"{name}: {token!r} is not in the first {LEAD_CHARS} chars"


def _normalised(text: str) -> str:
    """Lowercased, whitespace-collapsed: wrapping is not part of the contract."""
    return re.sub(r"\s+", " ", text).lower()


# The refusal statement, asserted as a clause rather than a token: the sentence
# must say refusals END WITH the line (not "is not emitted"), and the same
# description must carry the protocol-validation exception — an undeclared or
# mistyped argument is rejected before `_tracked` and carries no such line.
_REFUSAL_CLAUSE = re.compile(
    r"refusal the server raises ends with one machine-readable "
    r"`mcp-refusal \{\"code\":…\}` line"
)
_REFUSAL_EXCEPTION = re.compile(
    r"undeclared or mistyped argument is rejected by protocol validation before the "
    r"tool runs and carries no such line"
)
_REFUSAL_NEGATED = re.compile(r"mcp-refusal[^.]*\bnot (emitted|appended|returned)")
# `expected_hash` is schema-optional and unguarded writes proceed *by default*,
# but WRITE_PRECONDITION_REQUIRED=true turns an omitted hash into a refusal. Every
# description that accepts the argument on a guardable write says so in one
# clause; `create_note` is the exception — it accepts the argument only to
# refuse it `no_incumbent`, and creation is exempt from the setting.
_OPTIONAL_CLAUSE = re.compile(
    r"optional by default[^.]*unless the deployment requires it[^.]*`precondition_required`"
)
_UNQUALIFIED_OPTIONAL = (
    "without it a stale edit overwrites",
    "optional; omit it and nothing changes",
    "the deployment may require a hash",
)
GUARDABLE_WRITE_TOOLS = (
    "edit_note", "set_frontmatter", "move_note", "delete_note", "write_file", "delete_file",
)


@pytest.mark.parametrize("name", REFUSAL_QUALIFIED_TOOLS)
def test_the_refusal_statement_is_qualified(name):
    text = _normalised(description(name))
    assert _REFUSAL_CLAUSE.search(text), f"{name}: no positive refusal clause"
    assert _REFUSAL_EXCEPTION.search(text), f"{name}: protocol-validation exception missing"
    assert not _REFUSAL_NEGATED.search(text), f"{name}: refusal statement is negated"


def test_the_guardable_set_is_every_expected_hash_tool_but_create_note():
    """Guard the parametrisation: a tool that gains `expected_hash` later must
    join the list (or be argued out of it here), not slip past unqualified."""
    accepting = {
        t.name for t in listed() if "expected_hash" in t.parameters["properties"]
    }
    assert accepting == set(GUARDABLE_WRITE_TOOLS) | {"create_note"}


@pytest.mark.parametrize("name", GUARDABLE_WRITE_TOOLS)
def test_expected_hash_is_described_as_optional_by_default_only(name):
    text = _normalised(description(name))
    assert _OPTIONAL_CLAUSE.search(text), f"{name}: optional-by-default clause missing or unqualified"
    for phrase in _UNQUALIFIED_OPTIONAL:
        assert phrase not in text, f"{name}: still says {phrase!r}"


def test_create_note_describes_its_hash_as_always_refused():
    text = _normalised(description("create_note"))
    assert "no_incumbent" in text
    assert not _OPTIONAL_CLAUSE.search(text)


def test_the_instructions_qualify_both_statements():
    text = _normalised(server.SERVER_INSTRUCTIONS)
    assert re.search(
        r"server refusals end with one machine-readable line, `mcp-refusal \{\"code\":…\}`", text
    )
    assert "protocol-layer argument errors do not carry it" in text
    assert re.search(
        r"optional by default \(an unguarded stale edit overwrites\) unless the deployment "
        r"requires it \(`precondition_required`\)",
        text,
    )
    assert "without it a stale edit overwrites" not in text


def test_move_note_states_the_markdown_link_restriction():
    """`MDLINK_REWRITE_FLAGS` sets `angle=False`: `[x](<Old.md>)` is indexed but
    not rewritten, and the move still reports plain success — so the description
    must not promise "markdown links" generally (Codex adversarial round 1)."""
    text = _normalised(description("move_note"))
    assert "bare-href markdown links `[text](old.md)`" in text
    assert re.search(
        r"angle-bracket hrefs `\[text\]\(<old\.md>\)` are \*\*not\*\* rewritten", text
    ), text
    assert not re.search(r"path-style and markdown links", text)
    assert tools.MDLINK_REWRITE_FLAGS["angle"] is False  # the fact the sentence states


def test_move_note_keeps_the_residual_race_in_its_lead():
    """The precondition is compared at preflight; the rename pins inode identity
    and does not compare bytes again. That limits what `expected_hash` buys at
    call time, so it stays where the agent reads, beside the precondition."""
    # Wrapping is not part of the contract: collapse whitespace before matching,
    # as tests/test_issue_150_docstrings.py does.
    lead = re.sub(r"\s+", " ", description("move_note")[:LEAD_CHARS]).lower()
    assert "preflight" in lead
    assert "pins inode identity" in lead
    assert re.search(r"(does not|without) (re-?)?compar", lead), lead


def test_read_note_leads_with_its_fields():
    tool = registered("read_note")
    lead = (tool.description or "")[:LEAD_CHARS]
    for prop in tool.parameters["properties"]:
        assert prop in lead, prop
    for token in ("`content`", "`heading`", "`content_hash`", "`error`"):
        assert token in lead, token


# ── 5. the surface is otherwise unchanged ────────────────────────────────────


@pytest.mark.asyncio
async def test_names_order_and_schemas_match_the_5a391ca_fixture():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    live = await mcp.list_tools()
    assert [t.name for t in live] == [row["name"] for row in fixture]
    for tool, row in zip(live, fixture):
        got_in = json.loads(json.dumps(tool.inputSchema, sort_keys=True))
        got_out = json.loads(json.dumps(tool.outputSchema, sort_keys=True))
        assert got_in == row["inputSchema"], tool.name
        assert got_out == row["outputSchema"], tool.name
    # On 5a391ca every tool but `read_file` carries an outputSchema (the SDK
    # wraps `str` returns); pin the shape of that set so a regeneration that
    # silently drops one is noticed.
    without = [t.name for t in live if t.outputSchema is None]
    assert without == ["read_file"]


# ── 6. every tool-shaped reference is registered ─────────────────────────────


@pytest.mark.parametrize("tool", listed(), ids=lambda t: t.name)
def test_every_description_names_only_registered_tools(tool):
    found = tool_shaped_references(tool.description or "", first_words())
    unknown = found - registered_names()
    assert not unknown, f"{tool.name} names unregistered tool(s): {sorted(unknown)}"


def test_the_extractor_recognises_each_form_and_skips_arguments():
    words = first_words()
    assert tool_shaped_references("see `read_note`", words) == {"read_note"}
    assert tool_shaped_references("call `read_note(path)` now", words) == {"read_note"}
    assert tool_shaped_references("the `read_note.frontmatter_yaml` field", words) == {"read_note"}
    assert tool_shaped_references("pass `content` and `expected_hash`; use `find`", words) == set()
    # A dotted typo is caught: its head is extracted and is not registered.
    typo = tool_shaped_references("the `read_notes.frontmatter_yaml` field", words)
    assert typo == {"read_notes"} and not typo <= registered_names()


def test_the_live_dotted_reference_is_covered():
    """`read_file`'s description carries the one dotted tool reference the
    first draft of the extractor missed (Codex spec review, round 1)."""
    text = description("read_file")
    assert "`read_note.frontmatter_yaml`" in text
    assert "read_note" in tool_shaped_references(text, first_words())
