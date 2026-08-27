"""The unmatched-opener refusal is disclosed where callers read it (#150).

The `vault-write` delta carries a scenario about documentation:

    WHEN the `edit_note` and `move_note` docstrings (the MCP-facing
    registrations and the implementation docstrings alike) describe when a
    call is refused
    THEN they SHALL disclose the unmatched-indented-fence-opener refusal
    alongside the defective-frontmatter refusal, and SHALL NOT advertise
    unqualified read/write selector parity on such notes.

Docstrings are the only specification an MCP client ever sees — the registered
`edit_note` description IS the contract the calling model reads before deciding
whether a write is safe — so a refusal that exists in code and not in the
docstring is a refusal the agent discovers by having a write fail. That is why
this scenario is pinned rather than left to review.

**Four docstrings, deliberately.** `server.py`'s registrations are what FastMCP
publishes; `tools.py`'s implementations are what a maintainer reads. The two
have drifted before, and a disclosure present in only one of them is the drift
this test exists to catch.

Properties are pinned, never exact prose: each docstring must *say* the thing,
in whatever words, and the parity claim must be qualified rather than absent.

Follows the setup convention of `tests/test_issue_89_tool_names_in_copy.py`:
minimal env defaults and a chdir away from any `.env` BEFORE importing the
tools module.
"""

import os
import re
import tempfile

os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
os.chdir(tempfile.gettempdir())

import pytest  # noqa: E402

import src.mcp_server.tools as tools  # noqa: E402
from src.mcp_server.server import mcp  # noqa: E402


def _registered_doc(name: str) -> str:
    """The description FastMCP actually publishes for `name`.

    Read out of the server's own registry rather than off the module function,
    for #89's reason: what matters is the string a client receives, and a
    hand-followed reference to `server.edit_note` would keep passing if the
    registration ever stopped pointing at it.
    """
    for tool in mcp._tool_manager.list_tools():
        if tool.name == name:
            doc = tool.description or ""
            assert doc.strip(), f"{name} is registered with an empty description"
            return doc
    raise AssertionError(f"no tool registered under the name {name!r}")


DOCSTRINGS = {
    "server.edit_note": lambda: _registered_doc("edit_note"),
    "server.move_note": lambda: _registered_doc("move_note"),
    "tools.edit_note_impl": lambda: tools.edit_note_impl.__doc__ or "",
    "tools.move_note_impl": lambda: tools.move_note_impl.__doc__ or "",
}

EDIT_DOCS = ["server.edit_note", "tools.edit_note_impl"]
MOVE_DOCS = ["server.move_note", "tools.move_note_impl"]


def _normalised(name: str) -> str:
    """The docstring as one lowercased space-collapsed line.

    Wrapping is not part of the contract: a disclosure that reflowed across a
    line break must not read as a missing disclosure.
    """
    return re.sub(r"\s+", " ", DOCSTRINGS[name]()).lower()


# ── the disclosure itself ───────────────────────────────────────────────────


@pytest.mark.parametrize("name", sorted(DOCSTRINGS))
def test_the_refusal_names_the_shape_it_refuses(name):
    """Not "some fences are refused": the caller has to be able to recognise
    the note in front of them, so the shape is spelled out — an opener
    indented one to three spaces that nothing below it closes."""
    doc = _normalised(name)
    assert "indented" in doc, f"{name} does not mention indentation"
    assert "one to three spaces" in doc, f"{name} does not give the indent range"
    assert re.search(r"nothing (below it |below them )?closes", doc), (
        f"{name} does not say the opener is unclosed"
    )


@pytest.mark.parametrize("name", sorted(DOCSTRINGS))
def test_the_refusal_says_it_refuses(name):
    doc = _normalised(name)
    assert "refus" in doc, f"{name} never uses the word refuse"


@pytest.mark.parametrize("name", EDIT_DOCS)
def test_the_edit_docs_disclose_it_beside_the_frontmatter_refusal(name):
    """"Alongside the defective-frontmatter refusal" is the spec's wording, and
    it matters: an agent that has learned "malformed frontmatter is the one
    thing that refuses a section write" needs the second one in the same
    breath, not in a distant paragraph it may not reach."""
    doc = _normalised(name)
    assert "frontmatter" in doc
    assert "indented" in doc
    # Both refusals, and the write-nothing guarantee that goes with them.
    assert re.search(r"(nothing (was |is )?written|without writing)", doc), (
        f"{name} does not say the refusal writes nothing"
    )


@pytest.mark.parametrize("name", MOVE_DOCS)
def test_the_move_docs_scope_the_refusal_to_rewrite_enabled_moves(name):
    doc = _normalised(name)
    assert "rewrite_links=true" in doc
    assert "rewrite_links=false" in doc, (
        f"{name} does not tell the caller the way through"
    )
    assert "before the rename" in doc, (
        f"{name} does not say the whole move is refused before anything moves"
    )


@pytest.mark.parametrize("name", MOVE_DOCS)
def test_the_move_docs_say_every_source_is_preflighted(name):
    """Including the moved note's own body — a caller reading only "sources
    that link here" would not expect its own note to be able to refuse."""
    doc = _normalised(name)
    assert "every source" in doc
    assert "own body" in doc


# ── the parity claim, qualified ─────────────────────────────────────────────


# Selector parity is now a claim about *resolution on writes this tool
# admits*, not a promise that every readable section is writable — two shapes
# read fine and refuse every write.
#
# Pinning this as "the old sentence is absent" would be vacuous: the old
# sentence lived in the spec and the architecture note, not in either
# docstring, so a regex for it passes on a docstring that says nothing at all.
# What is pinned instead is a property with two halves, and it is the second
# half that bites: **each edit docstring must make a parity statement, and
# every parity statement it makes must carry its qualifier in the same
# sentence.** Deleting the qualification fails half two; deleting the whole
# paragraph fails half one; moving the qualifier into a distant caveat, which
# is the realistic drift, fails half two.
_PARITY_MARKER = re.compile(r"selector parity|same section|the same selectors?")
_QUALIFIER = re.compile(r"admits|admitted|not a promise|about resolution")


def _parity_sentences(name: str) -> list[str]:
    return [
        sentence
        for sentence in re.split(r"(?<=[.]) ", _normalised(name))
        if _PARITY_MARKER.search(sentence)
    ]


@pytest.mark.parametrize("name", EDIT_DOCS)
def test_each_edit_docstring_states_the_parity_relationship(name):
    """Half one. A docstring that simply stops mentioning `read_note`'s
    selectors is not "safely silent" — the ordinals are advertised to callers
    as the reliable selector, and the relationship between the two tools is
    what makes that advertisement usable."""
    assert _parity_sentences(name), (
        f"{name} makes no selector-parity statement at all; the claim was "
        "supposed to be qualified, not deleted"
    )


@pytest.mark.parametrize("name", EDIT_DOCS)
def test_every_parity_statement_carries_its_qualifier(name):
    """Half two, and the one that catches the realistic regression: a parity
    sentence that reads as an unconditional promise, with the "except on notes
    that refuse" caveat somewhere further down where a model summarising the
    tool will not carry it."""
    for sentence in _parity_sentences(name):
        assert _QUALIFIER.search(sentence), (
            f"{name} claims selector parity without qualifying it in the same "
            f"sentence: {sentence.strip()!r}. Parity is about how a selector "
            "resolves on a write this tool ADMITS — a defective frontmatter "
            "block and an unmatched indented fence opener both read fine and "
            "refuse every write."
        )


def test_the_edit_docs_keep_the_read_asymmetry_explicit():
    """Both refusals are asymmetric with reads on purpose. A caller told only
    "this note is refused" may conclude the note is unreachable and stop."""
    for name in EDIT_DOCS:
        doc = _normalised(name)
        assert "read_note" in doc, f"{name} does not point at the read that works"
