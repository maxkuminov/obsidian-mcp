"""`edit_note` full replacement preserves frontmatter by default (#128).

The bug: the natural agent read-modify-write — read a note, edit the content
portion of the response, pass it back through full replacement — silently
DELETED the note's YAML frontmatter, because `read_note` strips the block while
full replacement writes exactly what it is given. A destructive write nobody
asked for, reported as success.

Three Codex audit rounds killed every attempt to *infer* whether incoming
content was meant to carry frontmatter. A line-1 `---` test breaks on bodies
that open with a thematic break; a complete-valid-block test breaks on a
stripped body that itself begins with a mapping-shaped fenced block, which
`read_note` legitimately produces. The conclusion is structural and is what
these tests pin: **content is never classified**, and destructive intent is
asked for explicitly through `replace_frontmatter=True`.

Offline: a tmp_path vault, `_log_usage` stubbed, no DB.
"""

import asyncio
import os
import tempfile

os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
os.chdir(tempfile.gettempdir())

import pytest  # noqa: E402

import src.mcp_server.tools as tools  # noqa: E402
from src.config import MAX_NOTE_BYTES  # noqa: E402
from src.mcp_server.auth import current_permission  # noqa: E402
from src.mcp_server.server import mcp  # noqa: E402
from src.services import vault as vault_service  # noqa: E402
from src.services import vault_fs  # noqa: E402


@pytest.fixture
def vault(monkeypatch, tmp_path):
    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(vault_service.settings, "vault_path", str(tmp_path))
    monkeypatch.setattr(tools, "_log_usage", noop)
    vault_fs.reset_filesystem_probe_cache()
    token = current_permission.set("readwrite")
    yield tmp_path
    current_permission.reset(token)
    vault_fs.reset_filesystem_probe_cache()


def write(vault, name, text):
    (vault / name).write_text(text, encoding="utf-8", newline="")
    return vault / name


def read(vault, name):
    # `Path.read_text(newline=...)` is 3.13+; decode the raw bytes so CRLF is
    # never translated away under us — this whole module is about exact bytes.
    return (vault / name).read_bytes().decode("utf-8")


# ── the default: the block survives, whatever the body looks like ───────────


def test_a_body_only_replacement_keeps_the_block_byte_identically(vault):
    note = write(vault, "n.md", "---\ntitle: Keep Me\ntags: [a]\n---\nold body\n")
    result = asyncio.run(tools.edit_note_impl("n.md", "new body\n"))
    assert "Updated note" in result
    assert read(vault, "n.md") == "---\ntitle: Keep Me\ntags: [a]\n---\nnew body\n"
    assert note.read_bytes().decode("utf-8").startswith(
        "---\ntitle: Keep Me\ntags: [a]\n---\n"
    )


@pytest.mark.parametrize(
    "body",
    [
        # The round-1 breaker: a body whose first line is a thematic break.
        "---\n\nA horizontal rule opens this note.\n",
        # The round-3 breaker: a stripped body that itself begins with a
        # COMPLETE mapping-shaped fenced block. `read_note` returns exactly
        # this for a note whose body opens that way, so any "does the content
        # look like frontmatter?" test deletes the real block and promotes
        # this one.
        "---\nnot: frontmatter\n---\nThis fenced block is body text.\n",
        # An unclosed fence in the body.
        "---\ndangling\n",
        # A body that is only a fence line.
        "---",
        # Ordinary prose, for contrast.
        "just words\n",
    ],
)
def test_content_shape_never_changes_the_decision(vault, body):
    """Destructive intent cannot be inferred from content shape — the whole
    conclusion of the audit rounds. Every one of these bodies is written as
    BODY, under the preserved block."""
    original_block = "---\ntitle: Real\n---\n"
    write(vault, "n.md", original_block + "old\n")
    asyncio.run(tools.edit_note_impl("n.md", body))
    assert read(vault, "n.md") == original_block + body


def test_the_whole_note_read_round_trips_byte_identically(vault):
    """End-to-end: `read_note` -> take the content portion -> default
    `edit_note`. The note must be unchanged."""
    original = "---\ntitle: Round Trip\nstatus: draft\n---\n# Heading\n\nBody text.\n"
    write(vault, "n.md", original)

    rendered = asyncio.run(tools.read_note_impl("n.md"))
    assert "[TRUNCATED]" not in rendered
    # The content portion is everything after the rendered header's separator.
    body = rendered.split("\n---\n", 1)[1]

    asyncio.run(tools.edit_note_impl("n.md", body))
    assert read(vault, "n.md") == original


def test_an_empty_block_note_round_trips_and_is_not_duplicated(vault):
    """The round-4 blocker in one test. If the read parser treated
    `---\\n---\\n` as absent while the write side preserved it, the read body
    would still carry the block and the write would put a second one above
    it."""
    original = "---\n---\nBody under an empty block.\n"
    write(vault, "n.md", original)

    rendered = asyncio.run(tools.read_note_impl("n.md"))
    body = rendered.split("\n---\n", 1)[1]
    assert body == "Body under an empty block.\n"

    asyncio.run(tools.edit_note_impl("n.md", body))
    assert read(vault, "n.md") == original
    assert read(vault, "n.md").count("---") == 2


def test_a_crlf_note_keeps_its_crlf_block_while_the_body_arrives_lf(vault):
    """Declared in the spec: the block is preserved byte-identically, CRLF
    fences included. The BODY comes back through `read_note`'s pre-existing
    universal-newline translation as LF — the round trip preserves content,
    not the body's original newline bytes. Pre-existing property of the read
    path, not a regression introduced here."""
    original = "---\r\ntitle: CRLF\r\n---\r\nline one\r\nline two\r\n"
    write(vault, "n.md", original)

    rendered = asyncio.run(tools.read_note_impl("n.md"))
    body = rendered.split("\n---\n", 1)[1]
    asyncio.run(tools.edit_note_impl("n.md", body))

    after = read(vault, "n.md")
    assert after.startswith("---\r\ntitle: CRLF\r\n---\r\n")
    assert after == "---\r\ntitle: CRLF\r\n---\r\n" + "line one\nline two\n"


# ── the separator (D2) ──────────────────────────────────────────────────────


def test_a_metadata_only_note_gains_exactly_one_separator(vault):
    """The closing fence sits at EOF with no newline. Without the inserted
    `\\n` the result would be `---Body`, which is not a fence at all."""
    write(vault, "n.md", "---\ntitle: Meta\n---")
    asyncio.run(tools.edit_note_impl("n.md", "Body\n"))
    assert read(vault, "n.md") == "---\ntitle: Meta\n---\nBody\n"


def test_no_separator_is_added_for_empty_content(vault):
    write(vault, "n.md", "---\ntitle: Meta\n---")
    asyncio.run(tools.edit_note_impl("n.md", ""))
    assert read(vault, "n.md") == "---\ntitle: Meta\n---"


def test_a_block_already_ending_in_a_newline_gains_nothing(vault):
    write(vault, "n.md", "---\ntitle: Meta\n---\nold\n")
    asyncio.run(tools.edit_note_impl("n.md", "Body\n"))
    assert read(vault, "n.md") == "---\ntitle: Meta\n---\nBody\n"


def test_a_trailing_whitespace_closing_fence_is_preserved_as_written(vault):
    """`parse_frontmatter` accepts trailing spaces/tabs on the closer, so the
    write side must see one block here too — and must not normalise it."""
    write(vault, "n.md", "---\ntitle: Sloppy\n---   \nold\n")
    asyncio.run(tools.edit_note_impl("n.md", "new\n"))
    assert read(vault, "n.md") == "---\ntitle: Sloppy\n---   \nnew\n"


# ── replace_frontmatter=True ────────────────────────────────────────────────


def test_the_flag_replaces_the_whole_file(vault):
    write(vault, "n.md", "---\ntitle: Gone\n---\nold\n")
    asyncio.run(
        tools.edit_note_impl("n.md", "exactly this\n", replace_frontmatter=True)
    )
    assert read(vault, "n.md") == "exactly this\n"


def test_the_flag_is_how_a_block_is_repaired(vault):
    write(vault, "n.md", "---\na: [\n---\nbody\n")
    asyncio.run(
        tools.edit_note_impl(
            "n.md", "---\na: 1\n---\nbody\n", replace_frontmatter=True
        )
    )
    assert read(vault, "n.md") == "---\na: 1\n---\nbody\n"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"append": True},
        {"find": "old"},
        {"section": "Tasks"},
    ],
)
def test_the_flag_conflicts_with_every_other_mode(vault, kwargs):
    original = "---\ntitle: T\n---\n## Tasks\nold\n"
    write(vault, "n.md", original)
    result = asyncio.run(
        tools.edit_note_impl("n.md", "x", replace_frontmatter=True, **kwargs)
    )
    assert "choose at most one" in result
    assert "replace_frontmatter=True" in result
    assert read(vault, "n.md") == original


def test_the_flag_with_operation_replace_is_not_a_conflict(vault):
    """Both name full replacement. Treating them as two modes would refuse a
    perfectly coherent call."""
    write(vault, "n.md", "---\ntitle: T\n---\nold\n")
    result = asyncio.run(
        tools.edit_note_impl(
            "n.md", "new\n", operation="replace", replace_frontmatter=True
        )
    )
    assert "choose at most one" not in result
    assert read(vault, "n.md") == "new\n"


# ── notes with nothing valid to preserve ────────────────────────────────────


@pytest.mark.parametrize(
    "original",
    [
        "plain note, no fence\n",              # absent
        "---\nunclosed\n",                     # defective: unclosed
        "---\na: [\n---\nbody\n",              # defective: yaml error
        "---\n- a\n---\nbody\n",               # defective: not a mapping
        "---\n# only a comment\n---\nbody\n",  # defective: comment-only
    ],
)
def test_no_valid_block_means_wholesale_without_the_flag(vault, original):
    """The repair path stays open without the flag: there is nothing valid to
    preserve, so preserving would mean preserving a broken block."""
    write(vault, "n.md", original)
    asyncio.run(tools.edit_note_impl("n.md", "replacement\n"))
    assert read(vault, "n.md") == "replacement\n"


# ── ordering: compose, then cap, then expected= (D4) ────────────────────────


def test_dry_run_diffs_the_composed_result_and_writes_nothing(vault):
    original = "---\ntitle: T\n---\nold\n"
    write(vault, "n.md", original)
    diff = asyncio.run(tools.edit_note_impl("n.md", "new\n", dry_run=True))
    assert "-old" in diff and "+new" in diff
    # The block is common to both sides, so it must NOT appear as a change.
    assert "-title: T" not in diff
    assert "+title: T" not in diff
    assert read(vault, "n.md") == original


def test_dry_run_matches_what_a_real_write_produces(vault):
    write(vault, "n.md", "---\ntitle: T\n---\nold\n")
    dry = asyncio.run(tools.edit_note_impl("n.md", "new\n", dry_run=True))
    asyncio.run(tools.edit_note_impl("n.md", "new\n"))
    after = read(vault, "n.md")
    write(vault, "m.md", "---\ntitle: T\n---\nold\n")
    dry_again = asyncio.run(tools.edit_note_impl("m.md", "new\n", dry_run=True))
    assert dry.replace("n.md", "X") == dry_again.replace("m.md", "X")
    assert after == "---\ntitle: T\n---\nnew\n"


def test_the_cap_applies_to_the_composed_result(vault):
    """The block counts toward `MAX_NOTE_BYTES`. A body that fits on its own
    but overflows once the block is prepended must be refused, or the tool's
    own cap would be the one thing the composition escapes."""
    block = "---\ntitle: T\n---\n"
    write(vault, "n.md", block + "old\n")
    body = "x" * (MAX_NOTE_BYTES - len(block) + 1)
    result = asyncio.run(tools.edit_note_impl("n.md", body))
    assert "too large" in result
    assert read(vault, "n.md") == block + "old\n"


def test_a_composed_result_exactly_at_the_cap_succeeds(vault):
    block = "---\ntitle: T\n---\n"
    write(vault, "n.md", block + "old\n")
    body = "x" * (MAX_NOTE_BYTES - len(block))
    result = asyncio.run(tools.edit_note_impl("n.md", body))
    assert "Updated note" in result
    assert len(read(vault, "n.md").encode("utf-8")) == MAX_NOTE_BYTES


def test_a_concurrent_frontmatter_only_change_conflicts(vault, monkeypatch):
    """`expected=` compares the COMPLETE raw file, so a concurrent edit that
    touched only the frontmatter still conflicts. Preservation must not narrow
    the conflict check to the body."""
    path = write(vault, "n.md", "---\ntitle: Before\n---\nbody\n")

    original_write = tools.write_file_at
    fired: list[bool] = []

    def hooked(target, *args, **kwargs):
        if not fired:
            fired.append(True)
            path.write_text(
                "---\ntitle: After\n---\nbody\n", encoding="utf-8", newline=""
            )
        return original_write(target, *args, **kwargs)

    monkeypatch.setattr(tools, "write_file_at", hooked)
    result = asyncio.run(tools.edit_note_impl("n.md", "new body\n"))
    assert fired
    assert "changed" in result.lower() or "conflict" in result.lower()
    assert read(vault, "n.md") == "---\ntitle: After\n---\nbody\n"


# ── the other modes are untouched ───────────────────────────────────────────


def test_append_still_appends_to_the_raw_file(vault):
    write(vault, "n.md", "---\ntitle: T\n---\nold\n")
    asyncio.run(tools.edit_note_impl("n.md", "more\n", append=True))
    assert read(vault, "n.md") == "---\ntitle: T\n---\nold\n\nmore\n"


def test_find_still_operates_on_the_raw_file_including_the_block(vault):
    """Find/replace is deliberately raw — the one mode that can intentionally
    edit frontmatter text in place."""
    write(vault, "n.md", "---\ntitle: Old Title\n---\nbody\n")
    asyncio.run(tools.edit_note_impl("n.md", "New Title", find="Old Title"))
    assert read(vault, "n.md") == "---\ntitle: New Title\n---\nbody\n"


# ── the destructive-intent flag reaches usage_logs (task 2.1b) ──────────────


def _logged_params(coro_factory):
    captured = {}

    async def fake_log_usage(tool, params, duration_ms, response_size):
        captured["tool"] = tool
        captured["params"] = params

    original = tools._log_usage
    tools._log_usage = fake_log_usage
    try:
        asyncio.run(coro_factory())
    finally:
        tools._log_usage = original
    return captured


def test_replace_frontmatter_is_recorded_in_the_usage_log(vault):
    """An operator reading `usage_logs` after a block went missing has to be
    able to see whether wholesale replacement was asked for. The note body
    itself stays out of the log, as it always has."""
    write(vault, "n.md", "---\ntitle: T\n---\nold\n")
    logged = _logged_params(
        lambda: tools.edit_note_impl(
            "n.md", "SECRET BODY", replace_frontmatter=True
        )
    )
    assert logged["tool"] == "edit_note"
    assert logged["params"]["replace_frontmatter"] is True
    assert logged["params"]["path"] == "n.md"
    assert "content" not in logged["params"]
    assert "SECRET BODY" not in str(logged["params"])


def test_the_default_is_recorded_too(vault):
    write(vault, "n.md", "---\ntitle: T\n---\nold\n")
    logged = _logged_params(lambda: tools.edit_note_impl("n.md", "body"))
    assert logged["params"]["replace_frontmatter"] is False


# ── the contract is where agents read it (D7, task 2.4) ─────────────────────


def _registered(name):
    for t in mcp._tool_manager.list_tools():
        if t.name == name:
            return t
    raise AssertionError(f"{name} is not registered")


def test_the_registered_edit_note_takes_the_parameter():
    """The `tools.py` impl signature is invisible to MCP clients — what they
    see is the registered wrapper's schema. A flag added to one and not the
    other is unreachable."""
    schema = _registered("edit_note").parameters
    assert "replace_frontmatter" in schema["properties"]
    assert schema["properties"]["replace_frontmatter"].get("default") is False


@pytest.mark.parametrize(
    "phrase",
    [
        "replace_frontmatter",
        "preserved byte-identically",
        "round-trip guarantee",
        "[TRUNCATED]",
        "includes the heading",
    ],
)
def test_the_registered_edit_note_description_states_the_contract(phrase):
    """The registered description is the contract an agent actually reads. The
    old text said full replacement "overwrites the entire file", which this
    change makes false by default — leaving it would be a lie an agent acts
    on."""
    description = _registered("edit_note").description
    assert phrase in description


def test_the_registered_edit_note_no_longer_claims_a_bare_whole_file_overwrite():
    description = _registered("edit_note").description
    assert "the entire file is overwritten" not in description


@pytest.mark.parametrize(
    "phrase",
    ["[TRUNCATED]", "includes the heading", "round-trips"],
)
def test_the_registered_read_note_states_the_same_scoping(phrase):
    assert phrase in _registered("read_note").description


@pytest.mark.parametrize(
    "phrase",
    ["refused", "replace_frontmatter=True", "valid empty mapping", "last key"],
)
def test_the_registered_set_frontmatter_states_the_refusal(phrase):
    assert phrase in _registered("set_frontmatter").description
