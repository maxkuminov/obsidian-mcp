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

What is pinned here is the property, not the string, over the **two** producers
of that guidance and no wider:

  * producer 1 — the omitted-sections summary inside `_outline_text`;
  * producer 2 — the truncation notice `read_note_impl` builds.

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


# --- producer 1: the outline's omitted-sections summary ---------------------


def test_outline_summary_narrows_with_a_registered_tool():
    """`_outline_text`'s summary must offer `keyword_search`, and only real tools.

    The cap is deliberately well above the summary's own length: the degenerate
    branch (`limit=1`) hard-truncates the outline mid-string and can cut the
    name out of it, which would test the truncation rather than the copy.
    """
    outline = tools._outline_text(_note_with_many_headings(), _OUTLINE_CAP)
    assert outline is not None
    assert len(outline) <= _OUTLINE_CAP

    # The summary is its own line, so newlines are this producer's clause
    # separator. If nothing was omitted there is no summary and no clause
    # matches the anchor — which fails loudly rather than passing vacuously.
    clauses = outline.split("\n")
    clause = _the_guidance_clause(clauses, "outline summary")

    _assert_guidance_names_the_search_tool(clause, "outline summary")
    _assert_whole_output_is_callable(outline, "outline summary")


# --- producer 2: the read_note truncation notice ----------------------------


@pytest.mark.asyncio
async def test_truncation_notice_narrows_with_a_registered_tool(vault, cap):
    """A truncated read of a **headingless** note isolates producer 2's clause.

    Headingless is the point: `_outline_text` returns None, so producer 1's
    summary is not embedded in the notice and the clause the notice appends is
    the only narrowing guidance in the output.
    """
    (vault / "flat.md").write_text(_HEADINGLESS_BODY, encoding="utf-8")
    out = await tools.read_note_impl("flat.md")
    assert "[TRUNCATED]" in out

    # The notice's parts are joined with a blank line.
    clauses = out.split("\n\n")
    clause = _the_guidance_clause(clauses, "truncation notice")

    _assert_guidance_names_the_search_tool(clause, "truncation notice")
    _assert_whole_output_is_callable(out, "truncation notice")


# --- (d) over the shape production actually emits ---------------------------


@pytest.mark.asyncio
async def test_a_with_headings_truncated_read_names_only_registered_tools(vault, cap):
    """The real shape: producer 1's summary embedded inside producer 2's notice.

    No clause is isolated here — two narrowing clauses coexist — so only the
    whole-output registry check applies.
    """
    body = _note_with_many_headings() + "\n\n" + _HEADINGLESS_BODY
    (vault / "sectioned.md").write_text(body, encoding="utf-8")
    out = await tools.read_note_impl("sectioned.md")
    assert "[TRUNCATED]" in out

    _assert_whole_output_is_callable(out, "with-headings truncated read")


# --- the name that was wrong -----------------------------------------------


def test_search_notes_is_not_a_registered_tool():
    """The premise of #89, pinned: the old copy named a tool nobody is offered.

    `src/control_panel/routes.py` deliberately keeps the string as the
    historical spelling of pre-#78 `usage_logs` rows; that is a value in old
    rows, not a tool name, and is not affected by this.
    """
    assert "search_notes" not in _registered_names()
    assert "keyword_search" in _registered_names()
