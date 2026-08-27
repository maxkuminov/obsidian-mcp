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


@pytest.mark.asyncio
async def test_the_drop_order_is_the_declared_one(vault, monkeypatch):
    """View first (the raw block says everything it does), then the raw block,
    then tags, then the heading is elided, then the title."""
    monkeypatch.setattr(tools.settings, "max_read_response_chars", 200)
    write(
        vault, "n.md",
        "---\ntitle: " + "T" * 60 + "\ntags: [" + ", ".join("t" * 20 for _ in range(6))
        + "]\nfiller: " + "f" * 300 + "\n---\n## " + "H" * 300 + "\nbody\n",
    )
    out = await tools.read_note_impl("n.md", section="#1")
    dropped = [(o.field, o.reason) for o in out.metadata_omissions]
    assert dropped[0] == ("frontmatter", "metadata_budget")
    assert dropped[1] == ("frontmatter_yaml", "metadata_budget")
    assert dropped[2] == ("tags", "metadata_budget")
    assert dropped[3] == ("heading", "metadata_budget_elided")
    assert out.tags is None


@pytest.mark.asyncio
async def test_a_dropped_field_is_never_marked_in_place(vault, monkeypatch):
    """The whole point. A marker inside a note-controlled field is
    indistinguishable from note content — that is the forgery class."""
    monkeypatch.setattr(tools.settings, "max_read_response_chars", 120)
    write(vault, "n.md", "---\nk: " + "v" * 5_000 + "\n---\n## " + "H" * 400 + "\nb\n")
    out = await tools.read_note_impl("n.md", section="#1")

    assert out.frontmatter_yaml is None          # omitted whole, not cut
    assert out.heading is not None
    assert "…" not in out.heading                # elided with no marker
    assert "TRUNCATED" not in out.model_dump_json()
    assert out.heading == ("## " + "H" * 400)[:len(out.heading)]


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
async def test_a_crlf_section_round_trip_matches_the_declared_residual(vault):
    """Pinned as bytes: the selected body's terminators become LF, everything
    else is untouched. Declared in both docstrings, not claimed as identity."""
    write(vault, "n.md", "# A\r\nold\r\n# B\r\nkeep\r\n")
    out = await tools.read_note_impl("n.md", section="#1")
    assert out.content == "old\n"
    await tools.edit_note_impl("n.md", out.content, section="#1")
    assert read(vault, "n.md") == "# A\r\nold\n# B\r\nkeep\r\n"
