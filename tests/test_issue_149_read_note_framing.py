"""`read_note` returns fields, not an envelope (#149).

The bug this closes is a *destructive write*, reached through a read. Every
`read_note` response used to be one string — `# <title>`, `**Path:**`, optional
`**Tags:**` / `**Frontmatter:**`, then `"\\n---\\n"`, then the selected content
— and every component of that envelope is note-controlled. Two reproductions,
on two different fields, are pinned below: a note can make the rendered
response contain a line that is exactly `---`, so an agent that recovered the
section body by splitting on the separator recovered a *crafted string*, and
writing that back through `edit_note(section=…)` clobbered the section. The
consumer is an agent, so nobody sees the query.

Sanitising the fields one at a time does not close the class — one audit round
patched the title, the next found a frontmatter key — and per-component
invariants do not compose into an envelope invariant. So the response is
fields: there is no frame to forge when the frame is the protocol's.

What is pinned here:

  * the two forgeries, at the tool layer and end to end through `edit_note`;
  * structured/text parity, and absent-not-null, at the MCP layer;
  * the frontmatter contract — raw block authoritative, JSON view best-effort
    and omitted (with a stated reason) when it cannot be honest;
  * the budgets, including the metadata budget's drop order and the fact that
    a dropped field is reported out of band and never marked in place;
  * the outline's degraded states as data;
  * error precedence, and empty-content-at-offset-0 as a success.

Offline: a `tmp_path` vault, `_log_usage` stubbed, no DB.
"""

import json
import os
import tempfile

os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
os.chdir(tempfile.gettempdir())

import pydantic_core  # noqa: E402
import pytest  # noqa: E402

import src.mcp_server.tools as tools  # noqa: E402
from src.mcp_server.auth import current_permission  # noqa: E402
from src.mcp_server.read_result import ReadNoteResult  # noqa: E402
from src.mcp_server.server import mcp  # noqa: E402
from src.services import vault as vault_service  # noqa: E402


# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _no_usage_log(monkeypatch):
    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(tools, "_log_usage", _noop)


@pytest.fixture(autouse=True)
def _writable(monkeypatch):
    token = current_permission.set("readwrite")
    yield
    current_permission.reset(token)


@pytest.fixture
def vault(monkeypatch, tmp_path):
    monkeypatch.setattr(tools.settings, "vault_path", str(tmp_path))
    vault_service.clear_user_vault_cache()
    return tmp_path


@pytest.fixture
def cap(monkeypatch):
    monkeypatch.setattr(tools.settings, "max_read_response_chars", 500)
    return 500


def write(vault, name, text):
    (vault / name).write_text(text, encoding="utf-8")
    return name


def read(vault, name):
    # Bytes, decoded by hand: `read_text` applies universal-newline
    # translation, which would hide exactly the CRLF residual pinned below.
    return (vault / name).read_bytes().decode("utf-8")


def outline_size(outline) -> int:
    return len(outline.model_dump_json())


def registered(name):
    for tool in mcp._tool_manager.list_tools():
        if tool.name == name:
            return tool
    raise AssertionError(f"{name} is not registered")


# ══════════════════════════════════════════════════════════════════════════
# 1. the two forgeries
# ══════════════════════════════════════════════════════════════════════════
#
# Both notes are VALID. Neither is an attack on the parser; each simply
# contains a `---` line in a place the old renderer interpolated verbatim.

# Reproduction 1: a block scalar title whose second line is exactly `---`.
FORGED_TITLE = (
    "---\n"
    "title: |-\n"
    "  safe\n"
    "  ---\n"
    "  forged\n"
    "---\n"
    "# A\n"
    "old body\n"
    "# B\n"
    "keep\n"
)

# Reproduction 2: a quoted frontmatter KEY whose decoded value embeds the
# separator. The title fix of round 1 does not touch this.
FORGED_KEY = '---\n"safe\\n---\\nforged": value\n---\n# A\nold body\n# B\nkeep\n'


@pytest.mark.asyncio
async def test_a_multiline_yaml_title_cannot_forge_the_frame(vault):
    write(vault, "n.md", FORGED_TITLE)
    out = await tools.read_note_impl("n.md", section="#1")

    # The forged line exists only inside the title's own value...
    assert out.title == "safe\n---\nforged"
    # ...and the content field is exactly the section body, nothing else.
    assert out.heading == "# A"
    assert out.content == "old body\n"
    # In the serialized response the forgery is escaped text inside one string.
    assert '"safe\\n---\\nforged"' in out.model_dump_json()


@pytest.mark.asyncio
async def test_a_quoted_frontmatter_key_cannot_forge_the_frame(vault):
    write(vault, "n.md", FORGED_KEY)
    out = await tools.read_note_impl("n.md", section="#1")

    assert out.frontmatter == {"safe\n---\nforged": "value"}
    assert out.heading == "# A"
    assert out.content == "old body\n"


@pytest.mark.parametrize("note", [FORGED_TITLE, FORGED_KEY], ids=["title", "key"])
@pytest.mark.asyncio
async def test_a_forged_note_round_trips_through_a_section_write(vault, note):
    """The destructive sink, closed. Read a section, pass `content` straight
    back to `edit_note(section=…)`, and the file is byte-identical."""
    write(vault, "n.md", note)
    for selector in ("#1", "#2"):
        out = await tools.read_note_impl("n.md", section=selector)
        assert out.error is None
        result = await tools.edit_note_impl("n.md", out.content, section=selector)
        assert "Updated note" in result, result
        assert read(vault, "n.md") == note


@pytest.mark.parametrize("note", [FORGED_TITLE, FORGED_KEY], ids=["title", "key"])
@pytest.mark.asyncio
async def test_a_forged_note_round_trips_through_a_full_replace(vault, note):
    write(vault, "n.md", note)
    out = await tools.read_note_impl("n.md")
    assert out.truncated is False and out.offset == 0
    await tools.edit_note_impl("n.md", out.content)
    assert read(vault, "n.md") == note


@pytest.mark.asyncio
async def test_distinct_paths_stay_distinguishable(vault):
    """A lossy rendering collapsed `a\\nb.md` and `a b.md` onto one string.

    Collapsing terminators was the "make the components safe" repair the audit
    rejected: it trades a destructive write for a silently wrong read.
    """
    (vault / "sub").mkdir()
    write(vault, "sub/a b.md", "spaced\n")
    newline_name = "sub/a\nb.md"
    (vault / newline_name).write_text("newlined\n", encoding="utf-8")

    spaced = await tools.read_note_impl("sub/a b.md")
    newlined = await tools.read_note_impl(newline_name)
    assert spaced.path == "sub/a b.md"
    assert newlined.path == newline_name
    assert spaced.content == "spaced\n"
    assert newlined.content == "newlined\n"


# ══════════════════════════════════════════════════════════════════════════
# 2. the MCP layer: schema, parity, absent-not-null
# ══════════════════════════════════════════════════════════════════════════


def test_read_note_declares_an_output_schema():
    tool = registered("read_note")
    assert set(tool.parameters["properties"]) >= {"path", "section", "offset", "limit"}
    schema = tool.output_schema
    assert schema is not None
    props = schema["properties"]
    for field in (
        "path", "title", "tags", "frontmatter_yaml", "frontmatter", "heading",
        "content", "truncated", "offset", "next_offset", "total_chars",
        "outline", "metadata_omissions", "notice", "error",
    ):
        assert field in props, field


@pytest.mark.asyncio
async def test_the_text_block_is_the_json_of_the_structured_content(vault, cap):
    """The SDK renders the text block from the returned object and
    `structuredContent` from the validated dump. A value coerced in a
    serializer would appear in one and not the other; nothing may be coerced
    anywhere but at model-build time."""
    write(vault, "n.md", FORGED_TITLE + "x" * 5_000)
    blocks, structured = await mcp.call_tool("read_note", {"path": "n.md"})
    assert len(blocks) == 1
    assert json.loads(blocks[0].text) == structured
    # And the structured content still validates against the declared model.
    ReadNoteResult.model_validate(structured)


@pytest.mark.asyncio
async def test_absent_fields_are_absent_from_both_renderings(vault):
    write(vault, "n.md", "# A\nbody\n")
    blocks, structured = await mcp.call_tool("read_note", {"path": "n.md", "section": "#1"})
    for field in ("error", "notice", "outline", "next_offset", "tags",
                  "frontmatter", "frontmatter_yaml", "metadata_omissions"):
        assert field not in structured, field
        assert f'"{field}"' not in blocks[0].text, field
    # An EMPTY value is not an absent one, and must survive as itself.
    write(vault, "e.md", "# A\n# B\nb\n")
    _, structured = await mcp.call_tool("read_note", {"path": "e.md", "section": "#1"})
    assert structured["content"] == ""
    assert structured["truncated"] is False


@pytest.mark.asyncio
async def test_the_admission_refusal_is_typed_at_the_mcp_layer(vault, monkeypatch):
    """A bare string from a tool with an output schema is not an in-band error
    — FastMCP fails output validation and the agent sees a protocol error."""
    monkeypatch.setattr(tools, "_vault_admission_error", lambda: tools._NO_VAULT_MESSAGE)
    blocks, structured = await mcp.call_tool("read_note", {"path": "n.md"})
    assert structured == {"error": tools._NO_VAULT_MESSAGE}
    assert json.loads(blocks[0].text) == structured
    ReadNoteResult.model_validate(structured)


@pytest.mark.asyncio
async def test_the_logged_response_size_is_the_serialized_length(vault, monkeypatch):
    write(vault, "n.md", "# A\nbody\n")
    seen = {}

    async def _capture(tool, params, duration_ms, response_size):
        seen["size"] = response_size

    monkeypatch.setattr(tools, "_log_usage", _capture)
    out = await tools.read_note_impl("n.md")
    expected = len(pydantic_core.to_json(out, fallback=str, indent=2).decode())
    assert seen["size"] == expected
    # Not the pydantic repr, which is what `len(str(result))` used to record.
    assert seen["size"] != len(str(out))


# ══════════════════════════════════════════════════════════════════════════
# 3. frontmatter: raw block authoritative, JSON view best-effort
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_the_raw_block_is_returned_verbatim_without_its_fences(vault):
    write(vault, "n.md", "---\ntitle: T\nstatus: draft\n---\nbody\n")
    out = await tools.read_note_impl("n.md")
    assert out.frontmatter_yaml == "title: T\nstatus: draft\n"
    assert out.frontmatter == {"title": "T", "status": "draft"}
    assert out.content == "body\n"


@pytest.mark.asyncio
async def test_an_empty_block_is_an_empty_string_not_an_absent_one(vault):
    write(vault, "n.md", "---\n---\nbody\n")
    out = await tools.read_note_impl("n.md")
    assert out.frontmatter_yaml == ""
    assert out.frontmatter is None      # nothing to view, and no omission
    assert out.metadata_omissions is None


@pytest.mark.asyncio
async def test_a_defective_block_yields_no_frontmatter_fields(vault):
    """It is not a block, so it is body — exactly as the read path has always
    treated it, and exactly what the section-write refusal keys off."""
    write(vault, "n.md", "---\nbroken: [\n---\nbody\n")
    out = await tools.read_note_impl("n.md")
    assert out.frontmatter_yaml is None
    assert out.frontmatter is None
    assert out.content == "---\nbroken: [\n---\nbody\n"


@pytest.mark.asyncio
async def test_a_recursive_alias_omits_the_view_without_raising(vault):
    """`x: &X [*X]` loads into a self-referential list. A naive walk crashes or
    hangs; serializing it fails. The read must still succeed."""
    write(vault, "n.md", "---\nx: &X [*X]\n---\nbody\n")
    out = await tools.read_note_impl("n.md")
    assert out.error is None
    assert out.content == "body\n"
    assert out.frontmatter is None
    assert out.frontmatter_yaml == "x: &X [*X]\n"       # still lossless
    [omission] = out.metadata_omissions
    assert omission.field == "frontmatter"
    assert omission.reason == "not_json_representable"
    assert "read_file" in omission.detail


@pytest.mark.asyncio
async def test_a_json_key_collision_omits_the_view(vault):
    """`1:` and `"1":` are two YAML keys and one JSON key. Rendering the view
    anyway would silently drop one of them."""
    write(vault, "n.md", '---\n1: a\n"1": b\n---\nbody\n')
    out = await tools.read_note_impl("n.md")
    assert out.frontmatter is None
    assert out.frontmatter_yaml == '1: a\n"1": b\n'
    [omission] = out.metadata_omissions
    assert (omission.field, omission.reason) == ("frontmatter", "duplicate_json_key")


@pytest.mark.asyncio
async def test_dates_are_lossy_in_the_view_and_exact_in_the_raw_block(vault):
    write(vault, "n.md", "---\nd: 2020-01-02\nt: 2020-01-02 03:04:05\n---\nbody\n")
    out = await tools.read_note_impl("n.md")
    assert out.frontmatter == {"d": "2020-01-02", "t": "2020-01-02T03:04:05"}
    assert out.frontmatter_yaml == "d: 2020-01-02\nt: 2020-01-02 03:04:05\n"


@pytest.mark.asyncio
async def test_nested_maps_and_lists_survive_the_view(vault):
    write(
        vault, "n.md",
        "---\nmeta:\n  a: 1\n  b: [x, y]\n  c:\n    d: true\n---\nbody\n",
    )
    out = await tools.read_note_impl("n.md")
    assert out.frontmatter == {"meta": {"a": 1, "b": ["x", "y"], "c": {"d": True}}}


@pytest.mark.asyncio
async def test_a_deeply_nested_block_omits_the_view_rather_than_recursing(vault):
    depth = 60
    body = "".join("  " * i + f"k{i}:\n" for i in range(depth)) + "  " * depth + "v\n"
    write(vault, "n.md", f"---\n{body}---\nbody\n")
    out = await tools.read_note_impl("n.md")
    assert out.error is None
    assert out.frontmatter is None
    assert out.metadata_omissions[0].reason == "not_json_representable"


# ── parser-accepted values the server cannot represent ─────────────────────
#
# A note can be *valid YAML* and still hold something no JSON serializer will
# take. These cost at most the affected fields: never a raise, never a protocol
# error, never a divergence between the text block and structuredContent.

SURROGATES = {
    # A lone HIGH surrogate and a lone LOW surrogate. PyYAML decodes both
    # escapes into real (unpaired) code points; `pydantic_core` — which renders
    # BOTH halves of the MCP result — refuses to encode either.
    "high": "\\uD800",
    "low": "\\uDC00",
}


@pytest.mark.parametrize("half", sorted(SURROGATES))
@pytest.mark.asyncio
async def test_a_lone_surrogate_in_the_title_costs_the_title_only(vault, half):
    escape = SURROGATES[half]
    write(vault, "n.md", f'---\ntitle: "{escape}"\n---\nbody\n')
    out = await tools.read_note_impl("n.md")

    assert out.error is None
    assert out.content == "body\n"
    assert out.title is None
    assert out.frontmatter is None
    # The raw block is UNAFFECTED: in the file the escape is six literal ASCII
    # characters, so the authoritative copy carries it losslessly.
    assert out.frontmatter_yaml == f'title: "{escape}"\n'
    reasons = {(o.field, o.reason) for o in out.metadata_omissions}
    assert ("title", "unpaired_surrogate") in reasons
    assert ("frontmatter", "unpaired_surrogate") in reasons


@pytest.mark.parametrize("half", sorted(SURROGATES))
@pytest.mark.asyncio
async def test_a_lone_surrogate_does_not_become_a_protocol_error(vault, half):
    """The BLOCKER, at the layer it fired on: FastMCP renders the text block
    with `pydantic_core.to_json`, which raises `PydanticSerializationError` on
    an unpaired surrogate. Note-controlled frontmatter must not be able to
    manufacture a transport failure."""
    escape = SURROGATES[half]
    write(vault, "n.md", f'---\ntitle: "{escape}"\nk: "{escape}"\n---\nbody\n')
    blocks, structured = await mcp.call_tool("read_note", {"path": "n.md"})

    assert json.loads(blocks[0].text) == structured
    assert structured["content"] == "body\n"
    assert "title" not in structured
    assert "frontmatter" not in structured
    assert structured["frontmatter_yaml"].count(escape) == 2
    ReadNoteResult.model_validate(structured)


@pytest.mark.parametrize("half", sorted(SURROGATES))
@pytest.mark.asyncio
async def test_a_lone_surrogate_in_a_tag_costs_the_tags(vault, half):
    escape = SURROGATES[half]
    write(vault, "n.md", f'---\ntags: ["ok", "{escape}"]\n---\nbody\n')
    out = await tools.read_note_impl("n.md")

    assert out.error is None
    assert out.tags is None
    assert ("tags", "unpaired_surrogate") in {
        (o.field, o.reason) for o in out.metadata_omissions
    }


@pytest.mark.asyncio
async def test_a_surrogate_bearing_path_is_a_bounded_typed_error(vault):
    """Not an omission — a path that is not UTF-8 cannot name a file at all, so
    it is refused at admission, and the refusal does not quote it back."""
    blocks, structured = await mcp.call_tool("read_note", {"path": "a\ud800.md"})

    assert json.loads(blocks[0].text) == structured
    assert "path" not in structured            # never echoed
    assert "not valid UTF-8" in structured["error"]
    assert "\ud800" not in structured["error"]
    ReadNoteResult.model_validate(structured)


@pytest.mark.asyncio
async def test_a_surrogate_bearing_selector_is_a_bounded_typed_error(vault):
    write(vault, "n.md", "# A\nbody\n")
    blocks, structured = await mcp.call_tool(
        "read_note", {"path": "n.md", "section": "A\ud800"}
    )

    assert json.loads(blocks[0].text) == structured
    assert "not valid UTF-8" in structured["error"]
    assert "\ud800" not in structured["error"]
    assert "content" not in structured


@pytest.mark.asyncio
async def test_the_vault_layer_refuses_an_unencodable_path_for_every_tool(vault):
    """The guard sits beside the length bound, so a write tool cannot be handed
    a path the read tool refuses."""
    assert vault_service.is_encodable("ok.md") is True
    assert vault_service.is_encodable("a\ud800.md") is False
    for validator in (vault_service.validate_path, vault_service.validate_mutable_path):
        with pytest.raises(ValueError) as exc:
            validator("a\ud800.md")
        assert "not valid UTF-8" in str(exc.value)


# A well-formed YAML integer past CPython's int-string digit limit. PyYAML's
# *constructor* raises a bare `ValueError` — not a `YAMLError` — which used to
# escape `parse_frontmatter_diagnose` and take the whole read out.
_BIG_INT_NOTE = "---\nn: " + "1" * 6_000 + "\n---\n# A\nbody\n"

# Same shape, different exception: PyYAML's composer blows the stack on deeply
# nested flow collections and raises `RecursionError`.
_DEEP_NOTE = "---\na: " + "[" * 3_000 + "]" * 3_000 + "\n---\n# A\nbody\n"


@pytest.mark.parametrize(
    "note", [_BIG_INT_NOTE, _DEEP_NOTE], ids=["big_int", "deep_nesting"]
)
@pytest.mark.asyncio
async def test_a_parser_refused_block_never_raises_or_becomes_a_protocol_error(
    vault, note
):
    write(vault, "n.md", note)
    blocks, structured = await mcp.call_tool("read_note", {"path": "n.md"})

    assert json.loads(blocks[0].text) == structured
    assert "error" not in structured
    # The block is classified as defective, so it stays in the body — every
    # byte of the note is still returned, nothing is silently lost.
    assert structured["content"] == note
    assert "frontmatter" not in structured
    assert "frontmatter_yaml" not in structured


@pytest.mark.parametrize(
    "note", [_BIG_INT_NOTE, _DEEP_NOTE], ids=["big_int", "deep_nesting"]
)
def test_a_parser_refused_block_is_diagnosed_as_a_yaml_error(vault, note):
    """The read and the write sides must agree about what this block is.

    Classifying it `yaml_error` is what keeps `set_frontmatter` and section
    writes refusing it by name. The rejected alternative — calling it valid
    with an empty mapping so the read could still expose `frontmatter_yaml` —
    hands `set_frontmatter` a `{}` to merge into and `safe_dump` over the top
    of a block it never parsed: a destructive write on a note whose only defect
    is that it is large.
    """
    fm, body, diagnosis = vault_service.parse_frontmatter_diagnose(note)
    assert (fm, body) == ({}, note)
    assert diagnosis.valid is False
    assert diagnosis.defect == "yaml_error"


@pytest.mark.parametrize(
    "note", [_BIG_INT_NOTE, _DEEP_NOTE], ids=["big_int", "deep_nesting"]
)
@pytest.mark.asyncio
async def test_the_write_side_refuses_a_parser_refused_block_by_name(vault, note):
    write(vault, "n.md", note)
    for result in (
        await tools.edit_note_impl("n.md", "x", section="#1"),
        await tools.set_frontmatter_impl("n.md", {"a": 1}),
    ):
        assert "malformed frontmatter block" in result, result
        assert "replace_frontmatter=True" in result
    assert read(vault, "n.md") == note


@pytest.mark.asyncio
async def test_a_non_string_title_does_not_break_the_model(vault):
    """`title:` is whatever YAML says it is. The coercion happens at model
    build, not in a serializer, or the two renderings would disagree."""
    write(vault, "n.md", "---\ntitle: [a, b]\n---\nbody\n")
    out = await tools.read_note_impl("n.md")
    assert out.title == "['a', 'b']"
    assert json.loads(out.model_dump_json())["title"] == out.title


# ══════════════════════════════════════════════════════════════════════════
# 4. budgets
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_a_multi_megabyte_frontmatter_beside_a_one_char_body_stays_bounded(
    vault, cap
):
    """`read_note` reaches disk through `read_file()`, which has no byte cap.
    Without a metadata budget the response is governed by the content cap and
    is still megabytes wide."""
    huge = "k: " + "v" * 2_000_000 + "\n"
    write(vault, "n.md", f"---\n{huge}---\nx")
    out = await tools.read_note_impl("n.md")

    assert out.content == "x"
    assert out.frontmatter is None
    assert out.frontmatter_yaml is None
    reasons = {(o.field, o.reason) for o in out.metadata_omissions}
    # Two omissions with DISTINCT reasons, as the requirement asks: the view
    # never even builds (its own construction bound stops it), and the raw
    # block is then dropped by the metadata budget.
    assert ("frontmatter", "not_json_representable") in reasons
    assert ("frontmatter_yaml", "metadata_budget") in reasons
    assert len(out.model_dump_json()) < 4 * cap


@pytest.mark.asyncio
async def test_a_constructible_view_that_does_not_fit_is_dropped_first(vault, monkeypatch):
    """The other half: a view that builds fine but does not fit is dropped for
    budget, and the raw block survives — it says everything the view did."""
    monkeypatch.setattr(tools.settings, "max_read_response_chars", 900)
    write(vault, "n.md", "---\nk: " + "v" * 600 + "\n---\nbody\n")
    out = await tools.read_note_impl("n.md")

    assert out.frontmatter is None
    assert out.frontmatter_yaml == "k: " + "v" * 600 + "\n"
    [omission] = out.metadata_omissions
    assert (omission.field, omission.reason) == ("frontmatter", "metadata_budget")


def _crowded_note(title_len=100, heading_len=300):
    return (
        "---\ntitle: " + "T" * title_len
        + "\ntags: [" + ", ".join("t" * 20 for _ in range(6)) + "]"
        + "\nfiller: " + "f" * 300
        + "\n---\n## " + "H" * heading_len + "\nbody\n"
    )


@pytest.mark.asyncio
async def test_the_drop_order_is_the_declared_one(vault, monkeypatch):
    """All five steps, in order: view (the raw block says everything it does),
    raw block, tags, heading, title. The budget is small enough that even the
    title alone does not fit, so the last step is exercised too — it used to be
    unreachable in this fixture and therefore untested."""
    monkeypatch.setattr(tools.settings, "max_read_response_chars", 60)
    write(vault, "n.md", _crowded_note())
    out = await tools.read_note_impl("n.md", section="#1")

    assert [(o.field, o.reason) for o in out.metadata_omissions] == [
        ("frontmatter", "metadata_budget"),
        ("frontmatter_yaml", "metadata_budget"),
        ("tags", "metadata_budget"),
        ("heading", "metadata_budget"),
        ("title", "metadata_budget"),
    ]
    assert out.frontmatter is None
    assert out.frontmatter_yaml is None
    assert out.tags is None
    assert out.heading is None
    assert out.title is None
    # The content field is NOT metadata and is untouched by any of this.
    assert out.content == "body\n"


@pytest.mark.asyncio
async def test_the_order_stops_as_soon_as_the_remainder_fits(vault, monkeypatch):
    """Not "drop everything" — drop until it fits, then stop. A budget the
    heading and title clear between them keeps both."""
    monkeypatch.setattr(tools.settings, "max_read_response_chars", 500)
    write(vault, "n.md", _crowded_note())
    out = await tools.read_note_impl("n.md", section="#1")

    assert [o.field for o in out.metadata_omissions] == [
        "frontmatter", "frontmatter_yaml",
    ]
    assert out.frontmatter is None
    assert out.frontmatter_yaml is None
    assert out.tags is not None
    assert out.heading == "## " + "H" * 300
    assert out.title == "T" * 100


@pytest.mark.asyncio
async def test_an_oversized_title_never_leaves_an_empty_heading(vault, monkeypatch):
    """Regression on the ordering bug the whole-field rework removed.

    The heading's room used to be computed from the *un-dropped* title, so a
    title larger than the whole budget cut the heading to `""` — a
    present-but-empty note-controlled field, which collides with this tool's
    own convention that `""` is an answer (an empty section body) rather than
    an absence. The heading still goes, because the declared order is a
    priority list and `heading` precedes `title` in it — but it goes as an
    absence with a stated reason, not as a zero-length value.
    """
    monkeypatch.setattr(tools.settings, "max_read_response_chars", 200)
    write(vault, "n.md", "---\ntitle: " + "T" * 5_000 + "\n---\n## Short\nbody\n")
    out = await tools.read_note_impl("n.md", section="#1")

    assert out.heading is None                 # absent, never ""
    assert out.title is None
    assert [o.field for o in out.metadata_omissions] == [
        "frontmatter", "frontmatter_yaml", "heading", "title",
    ]
    # Absent, not present-and-empty: the distinction the `""` cut destroyed.
    assert "heading" not in json.loads(out.model_dump_json())


@pytest.mark.asyncio
async def test_no_field_is_ever_a_prefix_of_itself(vault, monkeypatch):
    """The whole point. A shortened value inside a note-controlled field is
    indistinguishable from note content — that is the forgery class."""
    monkeypatch.setattr(tools.settings, "max_read_response_chars", 120)
    write(vault, "n.md", "---\nk: " + "v" * 5_000 + "\n---\n## " + "H" * 400 + "\nb\n")
    out = await tools.read_note_impl("n.md", section="#1")

    assert out.frontmatter_yaml is None          # omitted whole, not cut
    assert out.heading is None                   # omitted whole, not cut to fit
    rendered = out.model_dump_json()
    assert "TRUNCATED" not in rendered
    assert "…" not in rendered
    # Whatever survived is the real value, not a prefix of one.
    assert out.content == "b\n"


@pytest.mark.asyncio
async def test_the_raw_block_is_lossless_whenever_it_is_present(vault, monkeypatch):
    """Never truncated in place: half a YAML block still parses, so a cut one
    is a *corrupt* block that looks valid."""
    monkeypatch.setattr(tools.settings, "max_read_response_chars", 400)
    block = "k: " + "v" * 300 + "\n"
    write(vault, "n.md", f"---\n{block}---\nbody\n")
    out = await tools.read_note_impl("n.md")
    assert out.frontmatter_yaml in (None, block)


@pytest.mark.asyncio
async def test_control_characters_in_content_stay_within_the_stated_worst_case(
    vault, cap
):
    """JSON escaping expands a control character up to six-fold (`\\u0001`).
    The doc's worst case multiplies the budgets by that; the response must sit
    under it rather than under the un-escaped figure."""
    write(vault, "n.md", "\x01" * 40_000)
    out = await tools.read_note_impl("n.md")
    assert len(out.content) == cap
    serialized = out.model_dump_json()
    assert len(serialized) <= 6 * (3 * cap) + 2_000
    # And the escaping really is happening — otherwise this proves nothing.
    assert "\\u0001" in serialized


# The prose allowance in the bound below. `notice` and `path` are the only
# components with no budget of their own: both are fixed server text plus
# interpolations bounded at admission (`MAX_PATH_CHARS`, `_NOTICE_SELECTOR_MAX`),
# so they are a constant, not a term that scales with the cap.
_FIXED_PROSE = 4_000


@pytest.mark.asyncio
async def test_the_whole_mcp_response_meets_the_documented_combined_bound(vault, cap):
    """The bound as the architecture doc states it, measured on the wire.

    Everything at once and everything hostile: content at the cap and made
    entirely of control characters (six JSON characters each), an outline whose
    titles are at the per-title limit on a note with more sections than fit,
    and metadata large enough to exercise the metadata budget. Measured over
    the MCP text block PLUS the serialized structuredContent, because the
    result carries both and the caller pays for both — that doubling is the
    `× 2` in the documented figure, and measuring only one half would hide it.
    """
    # Outline titles are plain ASCII at the per-title limit: that is the
    # configuration that actually FILLS the outline budget. Control-character
    # titles would escape six-fold, so fewer entries fit and the outline gets
    # smaller — the budget doing its job, but a weaker test of the total.
    sections = "".join(
        f"## {'S' * 90} {i}\n" + "\x01" * 200 + "\n" for i in range(400)
    )
    # A raw control character is not legal in a YAML plain scalar, so the
    # escape-heavy title goes in as a double-quoted scalar with `\\u0001`
    # escapes — valid YAML whose *value* is 600 control characters.
    write(
        vault, "n.md",
        '---\ntitle: "' + "\\u0001" * 600 + '"'
        + "\ntags: [" + ", ".join("t" * 40 for _ in range(50)) + "]"
        + "\nfiller: " + "f" * 30_000
        + "\n---\n" + sections,
    )

    blocks, structured = await mcp.call_tool("read_note", {"path": "n.md"})
    text = blocks[0].text
    assert json.loads(text) == structured

    # Every budgeted component is actually loaded, or the bound proves nothing.
    assert len(structured["content"]) == cap
    assert structured["truncated"] is True
    assert structured["outline"]["truncated"] is True
    assert structured["outline"]["entries"], "no outline entries — fixture is wrong"
    assert structured["metadata_omissions"], "no drops — fixture is wrong"
    assert "\\u0001" in text, "no escaping happened — the bound proves nothing"

    on_the_wire = len(text) + len(json.dumps(structured, ensure_ascii=False))
    documented = 2 * (6 * (3 * cap) + _FIXED_PROSE)
    assert on_the_wire <= documented, (
        f"{on_the_wire} characters on the wire, documented worst case is "
        f"{documented}"
    )


@pytest.mark.asyncio
async def test_an_unbounded_selector_is_not_echoed_whole_into_the_notice(vault, cap):
    write(vault, "n.md", "# A\n" + "b" * 5_000 + "\n")
    out = await tools.read_note_impl("n.md", section="A" * 10_000)
    assert out.error is not None
    assert len(out.error) <= cap


def test_the_vault_layer_bounds_a_path_at_admission(vault):
    """`path` is returned exactly, never elided — so the value itself has to be
    bounded, and the bound sits at the door."""
    assert vault_service.MAX_PATH_CHARS == 1024
    with pytest.raises(ValueError) as exc:
        vault_service.validate_path("x" * 1025)
    # The refusal does not quote the path back: repeating an over-long value is
    # the unbounded interpolation the limit exists to prevent.
    assert "x" * 100 not in str(exc.value)
    assert "1,024" in str(exc.value)


@pytest.mark.asyncio
async def test_an_over_long_path_is_an_in_band_error_with_no_path_field(vault):
    out = await tools.read_note_impl("y" * 2_000 + ".md")
    assert out.path is None
    assert "Path too long" in out.error
    assert "y" * 100 not in out.error


# ══════════════════════════════════════════════════════════════════════════
# 5. the outline's degraded states, as data
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_a_complete_outline_carries_no_omission_summary(vault, cap):
    write(vault, "n.md", "# A\n" + "a" * 600 + "\n# B\nb\n")
    out = await tools.read_note_impl("n.md")
    assert out.outline.truncated is False
    assert out.outline.omitted is None
    assert out.outline.first_ordinal is None
    assert [e.ordinal for e in out.outline.entries] == [1, 2]


@pytest.mark.asyncio
async def test_an_incomplete_outline_reports_its_omission_as_data(vault, cap):
    write(vault, "n.md", "".join(f"## S{i}\nbody\n" for i in range(400)))
    out = await tools.read_note_impl("n.md")
    outline = out.outline
    assert outline.truncated is True
    assert outline.omitted == 400 - len(outline.entries)
    assert (outline.first_ordinal, outline.last_ordinal) == (1, 400)
    assert len(outline.model_dump_json()) <= cap


def test_one_oversized_entry_does_not_suppress_the_ones_that_fit():
    """"At least one entry SHALL be emitted whenever one fits" is a claim about
    *any* entry, not about the first.

    Stopping at the first entry that does not fit made it false for the
    commonest real shape there is: a note whose opening section has a very long
    heading. Codex's fixture — a 300-character first title, a short second, a
    160-character budget — produced zero entries beside a bare omission count.
    """
    note = "# " + "T" * 300 + "\nbody\n## Short\nb\n"
    # 170 is chosen so the short entry fits and the elided-but-still-long first
    # one does not — the whole point is that entry 2 survives entry 1.
    out = tools.build_outline(note, 170)

    assert [e.ordinal for e in out.entries] == [2]
    assert out.entries[0].text == "Short"
    assert out.truncated is True
    assert out.omitted == 1
    # Skipping must not renumber anything: the ordinals still address the note.
    assert (out.first_ordinal, out.last_ordinal) == (1, 2)
    assert outline_size(out) <= 170

    # And the cap still wins: below the size of even the short entry, the
    # outline degrades to its marker rather than emitting something oversized.
    degraded = tools.build_outline(note, 160)
    assert degraded.entries == []
    assert degraded.truncated is True
    assert outline_size(degraded) <= 160


@pytest.mark.asyncio
async def test_duplicate_titles_are_flagged_and_the_notice_names_the_remedy(
    vault, cap
):
    """The archived duplicate-headings scenario has two halves, and the second
    one — *direct the caller to the ordinal* — used to live in the outline's
    rendered `← duplicate title, use the ordinal` suffix. The flag is now data,
    so the guidance has to be pinned where it moved to."""
    write(
        vault, "n.md",
        "# Top\n" + "x" * 600 + "\n## Report\na\n## Other\nb\n## Report\nc\n",
    )
    out = await tools.read_note_impl("n.md")

    by_ordinal = {e.ordinal: e for e in out.outline.entries}
    assert by_ordinal[2].duplicate is True and by_ordinal[4].duplicate is True
    assert by_ordinal[3].duplicate is False
    # And the caller is told what to do about it, by name.
    assert "ordinal" in out.notice
    assert 'section="#7"' in out.notice
    assert "titles repeat" in out.notice


@pytest.mark.asyncio
async def test_a_truncated_section_read_omits_the_outline(vault, cap):
    write(vault, "n.md", "# A\n" + "a" * 5_000 + "\n# B\nb\n")
    out = await tools.read_note_impl("n.md", section="#1")
    assert out.truncated is True
    assert out.outline is None


# ══════════════════════════════════════════════════════════════════════════
# 6. errors: precedence, in-band, and the empty read
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_path_resolution_beats_parameter_validation(vault):
    out = await tools.read_note_impl("missing.md", offset=-1, limit=0, section="nope")
    assert "Note not found" in out.error
    assert "offset" not in out.error


@pytest.mark.asyncio
async def test_parameter_validation_beats_section_resolution(vault):
    write(vault, "n.md", "# A\nbody\n")
    out = await tools.read_note_impl("n.md", offset=-1, section="nope")
    assert "offset must be >= 0" in out.error

    out = await tools.read_note_impl("n.md", limit=0, section="nope")
    assert "limit must be >= 1" in out.error


@pytest.mark.asyncio
async def test_exactly_one_error_and_no_content_beside_it(vault):
    write(vault, "n.md", "# A\nbody\n")
    out = await tools.read_note_impl("n.md", section="nope")
    assert out.error is not None
    assert out.content is None
    assert out.heading is None
    assert out.truncated is None
    assert out.total_chars is None
    assert out.notice is None
    assert out.path == "n.md"


@pytest.mark.asyncio
async def test_an_empty_section_at_offset_zero_is_a_successful_read(vault):
    write(vault, "n.md", "# A\n# B\nb\n")
    out = await tools.read_note_impl("n.md", section="#1")
    assert out.error is None
    assert out.content == ""
    assert out.total_chars == 0
    assert out.truncated is False
    assert out.next_offset is None


@pytest.mark.asyncio
async def test_an_empty_note_at_offset_zero_is_a_successful_read(vault):
    write(vault, "n.md", "")
    out = await tools.read_note_impl("n.md")
    assert out.error is None
    assert out.content == ""


@pytest.mark.asyncio
async def test_the_end_reached_error_applies_only_to_continuation_offsets(vault):
    write(vault, "n.md", "# A\n# B\nb\n")
    out = await tools.read_note_impl("n.md", section="#1", offset=1)
    assert "past the end" in out.error


# ══════════════════════════════════════════════════════════════════════════
# 7. the docstrings teach the field round trip
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("layer", ["registered", "impl"])
def test_no_docstring_prescribes_a_textual_recovery(layer):
    """No "split on the separator", no "drop the first line" — the structured
    field IS the recovery, and every textual rule was forgeable."""
    if layer == "registered":
        texts = [registered("read_note").description, registered("edit_note").description]
    else:
        texts = [tools.read_note_impl.__doc__, tools.edit_note_impl.__doc__]
    for text in texts:
        lowered = text.lower()
        assert "split on" not in lowered
        assert "drop the first line" not in lowered
        assert "\\n---\\n" not in text


@pytest.mark.parametrize("layer", ["registered", "impl"])
def test_both_edit_note_layers_name_the_content_field_as_the_body(layer):
    text = (
        registered("edit_note").description if layer == "registered"
        else tools.edit_note_impl.__doc__
    )
    assert "`heading` field" in text
    assert "`content` field" in text
    assert "read_note(path, section=...)" in text or "read_note(section=" in text
    # The declared CRLF residual stays declared rather than being claimed away.
    assert "LF" in text


@pytest.mark.parametrize("layer", ["registered", "impl"])
def test_both_read_note_layers_point_frontmatter_mutation_elsewhere(layer):
    text = (
        registered("read_note").description if layer == "registered"
        else tools.read_note_impl.__doc__
    )
    assert "set_frontmatter" in text
    assert "frontmatter_yaml" in text


@pytest.mark.asyncio
async def test_a_crlf_frontmatter_block_comes_back_lf_normalized(vault):
    """`frontmatter_yaml` is content-lossless, not byte-exact.

    The read path hands the shared parser `read_text()` output, which has
    already applied universal-newline translation — the same declared residual
    a section body carries, and for the same reason. Re-reading the file as
    bytes to serve one field would give `read_note` a second, disagreeing
    partition of the same note. `edit_note` still reattaches the CRLF block
    byte-identically, because it works from raw bytes.
    """
    original = "---\r\ntitle: T\r\nk: v\r\n---\r\n# A\r\nbody\r\n"
    write(vault, "n.md", original)
    out = await tools.read_note_impl("n.md")

    assert out.frontmatter_yaml == "title: T\nk: v\n"       # LF, not CRLF
    assert out.frontmatter == {"title": "T", "k": "v"}
    assert out.content == "# A\nbody\n"

    # And the write side is unaffected: the block goes back byte-identically.
    await tools.edit_note_impl("n.md", out.content)
    assert read(vault, "n.md").startswith("---\r\ntitle: T\r\nk: v\r\n---\r\n")


@pytest.mark.parametrize("layer", ["registered", "impl"])
def test_both_read_note_layers_declare_the_frontmatter_lf_residual(layer):
    text = (
        registered("read_note").description if layer == "registered"
        else tools.read_note_impl.__doc__
    )
    assert "LF-normalized" in text or "LF-normalised" in text


@pytest.mark.asyncio
async def test_a_crlf_section_round_trip_matches_the_declared_residual(vault):
    """Pinned as bytes: the selected body's terminators become LF, everything
    else is untouched. Declared in both docstrings, not claimed as identity."""
    write(vault, "n.md", "# A\r\nold\r\n# B\r\nkeep\r\n")
    out = await tools.read_note_impl("n.md", section="#1")
    assert out.content == "old\n"
    await tools.edit_note_impl("n.md", out.content, section="#1")
    assert read(vault, "n.md") == "# A\r\nold\n# B\r\nkeep\r\n"
