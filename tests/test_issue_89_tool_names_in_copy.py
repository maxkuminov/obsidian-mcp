"""Regression tests for GitHub issue #89.

`read_note`'s truncation guidance told the calling agent, twice, to narrow with
`search_notes`. No tool is registered under that name — FastMCP takes a tool's
name from the function in `src/mcp_server/server.py`, and the registered name
is `keyword_search`. The notice is emitted precisely when the note was too big
to return, i.e. at the moment the agent most needs a next step, and the next
step it was handed was an unknown-tool error.

This is the *other half* of #78, which found the same wrong name in
`usage_logs.tool`. That fix corrected `_tracked`'s first argument and the panel
and left the agent-facing copy alone.

What is pinned here is the property, not the string, over the producer of that
guidance and no wider.

**There used to be two producers; #149 left one.** The outline was a rendered
string carrying its own "N more sections not shown — narrow with
`keyword_search`" summary; it is now an object whose omission state is data
(`omitted`, `first_ordinal`, `last_ordinal`, `truncated`) and carries no prose
at all. All narrowing guidance therefore lives in the response's `notice`
field, and the tests below isolate that one producer in each of the two shapes
the old pair covered: a truncated read whose outline is *itself* truncated
(what producer 1 covered) and a truncated read of a headingless note (what
producer 2 covered). The outline object is additionally checked to yield no
tool reference at all — data cannot name an unregistered tool.

A source-wide scan of `tools.py` cannot work: `list_files`'s own truncation
line emits a bare `` `pattern` ``, lexically identical to a bare
`` `keyword_search` `` and not a tool. Filtering the candidates against the
registry to suppress it would remove exactly the unregistered names the check
exists to catch, leaving a check that passes over an empty set.

**Why membership on `keyword_search`, and not merely "every name is
registered".** Assertions (a), (c) and (d) below encode one general property —
no agent-facing string names an unregistered tool — and that property is too
weak to say what is actually wanted, which is that *this* guidance names *the
search tool*. Under (a)/(c)/(d) alone, rewriting the summary to narrow with
`` `delete_note` `` satisfies every assertion while pointing the agent at a
destructive tool, and adding a second registered reference beside
`keyword_search` lets its backticks be dropped with the set still non-empty and
fully registered. Three review rounds of this test passed vacuously on that
gap. The registry comparison stays as the broad backstop; assertion (b) is what
pins the specific claim.

Follows the setup convention of `test_read_response_cap.py`: minimal env
defaults and a chdir away from any `.env` BEFORE importing the tools module.
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


# --- the registry ----------------------------------------------------------


def _registered_names() -> set[str]:
    """Every name a tool is actually registered under, by introspection.

    A hand-maintained list is the shape of the bug: nobody noticed the copy
    named a tool that was never registered. Reading the server's own registry
    means a name that stops being registered is caught on the day it stops.
    Same idiom as `tests/test_issue_66_vault_unassignment_revokes_tools.py`.
    """
    return {t.name for t in mcp._tool_manager.list_tools()}


# --- the extraction rule ---------------------------------------------------
#
# A *tool reference* is a backtick-delimited span whose content is either
# exactly an identifier, or such an identifier immediately followed by `(`;
# the referenced name is that identifier. Nothing else is a tool reference —
# not an ordinal (`#7`), not an outline entry (`## Tasks`), not a quoted
# argument (`section="#7"`).

_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"
_SPAN = re.compile(r"`([^`]*)`")
_EXACT = re.compile(rf"\A{_IDENT}\Z")
_CALL = re.compile(rf"\A({_IDENT})\(")


def _tool_references(text: str) -> set[str]:
    names: set[str] = set()
    for span in _SPAN.findall(text):
        if _EXACT.match(span):
            names.add(span)
            continue
        call = _CALL.match(span)
        if call:
            names.add(call.group(1))
    return names


# --- locating the guidance clause ------------------------------------------
#
# The anchor is the narrowing verb both producers' guidance is built around.
# Rewording the copy past the anchor must break this test loudly; update the
# anchor deliberately rather than loosening it. Zero matching clauses, or
# several, is a failure — not a skip.

_ANCHOR = re.compile("narrow", re.IGNORECASE)


def _the_guidance_clause(clauses: list[str], producer: str) -> str:
    hits = [c for c in clauses if _ANCHOR.search(c)]
    assert len(hits) == 1, (
        f"{producer}: expected exactly one clause matching the narrowing "
        f"anchor {_ANCHOR.pattern!r}, found {len(hits)}. Either the guidance "
        f"was removed/reworded past the anchor, or the fixture no longer "
        f"isolates a single guidance clause. Clauses: {clauses!r}"
    )
    return hits[0]


def _assert_guidance_names_the_search_tool(clause: str, producer: str) -> None:
    registered = _registered_names()
    found = _tool_references(clause)

    # (a) the clause offers a next step at all. Without this, a reformatting
    #     that drops the backticks turns the check into a no-op reporting green.
    assert found, (
        f"{producer}: the narrowing guidance yields no tool reference under the "
        f"extraction rule — it offers the agent no callable next step. "
        f"Clause: {clause!r}"
    )

    # (b) it names the search tool. Membership, not equality: a further
    #     registered reference added beside it is permitted, so this does not
    #     freeze the copy — but it cannot be *replaced*, and it cannot be
    #     silently unbackticked behind a second reference.
    assert "keyword_search" in found, (
        f"{producer}: the narrowing guidance does not name `keyword_search`. "
        f"Extracted {sorted(found)!r} from {clause!r}. The point of this "
        f"guidance is to send the agent to the search tool; a different "
        f"registered tool (or a name that lost its backticks) is not that."
    )

    # (c) and nothing in it is a tool that does not exist.
    unknown = found - registered
    assert not unknown, (
        f"{producer}: the narrowing guidance names {sorted(unknown)!r}, which "
        f"no tool is registered under. Registered: {sorted(registered)!r}"
    )


def _assert_whole_output_is_callable(text: str, producer: str) -> None:
    """(d) every name anywhere in the rendered output is registered.

    This is what covers the `read_note(...)` continuation reference, which is
    outside the guidance clause.
    """
    unknown = _tool_references(text) - _registered_names()
    assert not unknown, (
        f"{producer}: the rendered output names {sorted(unknown)!r}, which no "
        f"tool is registered under."
    )


# --- fixtures --------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_usage_log(monkeypatch):
    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(tools, "_log_usage", _noop)


@pytest.fixture
def vault(monkeypatch, tmp_path):
    monkeypatch.setattr(tools.settings, "vault_path", str(tmp_path))
    return tmp_path


@pytest.fixture
def cap(monkeypatch):
    monkeypatch.setattr(tools.settings, "max_read_response_chars", 500)
    return 500


# Neither fixture's headings nor its body may contain the anchor word, or the
# "exactly one clause" assertion would match something that is not guidance.
_HEADINGS = 40
_OUTLINE_CAP = 600


def _note_with_many_headings() -> str:
    return "\n".join(
        f"## Section {i:02d}\n\nbody text for section {i:02d}.\n"
        for i in range(1, _HEADINGS + 1)
    )


_HEADINGLESS_BODY = "plain filler text, one long unbroken line. " * 250


# --- shape 1: the notice beside a TRUNCATED outline -------------------------
#
# What producer 1 used to cover. The outline no longer speaks — it reports its
# omission as data — so the guidance for a caller whose outline was cut short
# is the notice's one narrowing clause, and it must still be callable.


@pytest.mark.asyncio
async def test_a_truncated_outline_still_offers_a_registered_tool(vault, cap):
    (vault / "many.md").write_text(_note_with_many_headings(), encoding="utf-8")
    out = await tools.read_note_impl("many.md")
    assert out.truncated is True
    assert out.outline is not None and out.outline.truncated is True, (
        "fixture no longer truncates the outline; this test would pass vacuously"
    )

    clause = _the_guidance_clause(out.notice.split("\n\n"), "truncated-outline notice")
    _assert_guidance_names_the_search_tool(clause, "truncated-outline notice")
    _assert_whole_output_is_callable(out.notice, "truncated-outline notice")


@pytest.mark.parametrize("outline_cap", [_OUTLINE_CAP, 5_000, 100])
def test_the_outline_object_names_no_tool_at_all(outline_cap):
    """The other half of retiring producer 1: it cannot name a wrong tool now,
    because it carries no prose to name one in.

    Swept over all three states the requirement names — complete, truncated,
    and degraded to the bare marker — because "carries no prose" has to hold in
    every one of them, and the degraded state is the one a future author is
    most likely to reach for a marker string in.
    """
    outline = tools.build_outline(_note_with_many_headings(), outline_cap)
    assert outline is not None
    assert _tool_references(outline.model_dump_json()) == set()


def test_the_three_outline_states_are_all_actually_exercised():
    """Guard on the sweep above: if the fixture drifts so that all three caps
    produce the same state, the sweep silently stops covering anything."""
    note = _note_with_many_headings()
    complete = tools.build_outline(note, 5_000)
    truncated = tools.build_outline(note, _OUTLINE_CAP)
    degraded = tools.build_outline(note, 100)
    assert complete.truncated is False and complete.entries
    assert truncated.truncated is True and truncated.entries
    assert degraded.truncated is True and degraded.entries == []


@pytest.mark.asyncio
async def test_even_a_degraded_outline_leaves_the_notice_callable(vault, monkeypatch):
    """The requirement is over ANY truncated response, and the marker-only
    outline is the state with the least left in it."""
    monkeypatch.setattr(tools.settings, "max_read_response_chars", 100)
    (vault / "many.md").write_text(_note_with_many_headings(), encoding="utf-8")
    out = await tools.read_note_impl("many.md")

    assert out.outline is not None and out.outline.entries == []
    clause = _the_guidance_clause(out.notice.split("\n\n"), "degraded-outline notice")
    _assert_guidance_names_the_search_tool(clause, "degraded-outline notice")
    _assert_whole_output_is_callable(out.notice, "degraded-outline notice")


# --- shape 2: the notice on a headingless note ------------------------------


@pytest.mark.asyncio
async def test_truncation_notice_narrows_with_a_registered_tool(vault, cap):
    """A truncated read of a **headingless** note: no outline exists at all, so
    the notice's narrowing clause is the only guidance in the response."""
    (vault / "flat.md").write_text(_HEADINGLESS_BODY, encoding="utf-8")
    out = await tools.read_note_impl("flat.md")
    assert out.truncated is True
    assert out.outline is None

    # The notice's parts are joined with a blank line.
    clauses = out.notice.split("\n\n")
    clause = _the_guidance_clause(clauses, "truncation notice")

    _assert_guidance_names_the_search_tool(clause, "truncation notice")
    _assert_whole_output_is_callable(out.notice, "truncation notice")


# --- (d) over the shape production actually emits ---------------------------


@pytest.mark.asyncio
async def test_a_with_headings_truncated_read_names_only_registered_tools(vault, cap):
    """The real shape: a note with sections, truncated, outline and all.

    The whole response is checked, not just the notice — the outline rides in
    the same result and any tool name anywhere in it is agent-facing.
    """
    body = _note_with_many_headings() + "\n\n" + _HEADINGLESS_BODY
    (vault / "sectioned.md").write_text(body, encoding="utf-8")
    out = await tools.read_note_impl("sectioned.md")
    assert out.truncated is True

    _assert_whole_output_is_callable(
        out.model_dump_json(), "with-headings truncated read"
    )


# --- the name that was wrong -----------------------------------------------


def test_search_notes_is_not_a_registered_tool():
    """The premise of #89, pinned: the old copy named a tool nobody is offered.

    `src/control_panel/routes.py` deliberately keeps the string as the
    historical spelling of pre-#78 `usage_logs` rows; that is a value in old
    rows, not a tool name, and is not affected by this.
    """
    assert "search_notes" not in _registered_names()
    assert "keyword_search" in _registered_names()
