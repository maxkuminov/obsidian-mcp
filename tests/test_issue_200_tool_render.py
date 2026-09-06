"""#200 / #202 — what the two vector tools actually hand an agent.

The service withholds a stale row's chunk (`tests/test_issue_200_stale_service.py`);
this file is about the *rendered* result, which is the only thing a caller ever
sees. Three properties, each of them a decision rather than a formatting
choice:

* **The counts are on the header line always, including zero.** `get_links`'s
  rule: an absent token is not evidence of absence, and a caller cannot
  otherwise distinguish "nothing here is stale" from a build that does not
  report staleness.
* **A stale row's preview is replaced by a notice, and the notice contains no
  text read from the note.** Substituting the note's *current* leading text
  would be a different span from the one that matched, presented where the
  matching span goes — a fabricated excerpt, worse than none.
* **`find_related` states a stale source once, on every return path where the
  source row was loaded — the empty one included.** "No related notes" from a
  stale source is the reading a caller acts on when the truth is that the
  vector searched with describes content the note no longer has.

Fully offline: the session, the query embedding and the usage sink are faked.
"""
from __future__ import annotations

import os
import tempfile

import pytest

os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
os.chdir(tempfile.gettempdir())

import src.mcp_server.tools as tools  # noqa: E402
from src.config import MAX_CHUNKS_PER_NOTE  # noqa: E402
from src.services import embeddings, timing  # noqa: E402


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class _Result:
    def __init__(self, rows=()):
        self._rows = list(rows)

    def fetchall(self):
        return self._rows

    def all(self):
        return self._rows

    def scalars(self):
        return self

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class _Session:
    def __init__(self, batches=()):
        self._batches = list(batches)

    async def execute(self, clause, *_a, **_k):
        if str(clause).lstrip().upper().startswith("SET LOCAL"):
            return _Result()
        return _Result(self._batches.pop(0) if self._batches else [])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return None


class _FindRelatedSession(_Session):
    """`find_related` reads the source note and then its chunk vectors before
    the vector query; both come off a preamble so a test can empty either."""

    def __init__(self, source, chunks, batches=()):
        super().__init__(batches)
        self._preamble = [[source] if source else [], chunks]

    async def execute(self, clause, *_a, **_k):
        if not str(clause).lstrip().upper().startswith("SET LOCAL") and self._preamble:
            return _Result(self._preamble.pop(0))
        return await super().execute(clause, *_a, **_k)


class _Note:
    """A `notes_metadata` row as `semantic_search` hydrates it."""

    def __init__(
        self,
        note_id,
        path,
        *,
        content_hash="h2",
        embedded_content_hash="h2",
        chunks_truncated=False,
    ):
        self.id = note_id
        self.file_path = path
        self.title = path.removesuffix(".md")
        self.tags = ["topic"]
        self.content_hash = content_hash
        self.embedded_content_hash = embedded_content_hash
        self.chunks_truncated = chunks_truncated


class _Chunk:
    def __init__(self, note_id, text):
        self.note_id = note_id
        self.chunk_index = 0
        self.embedding = [1.0, 0.0, 0.0]
        self.chunk_text = text


def _semantic_row(note, *, text="SUPERSEDED SENTENCE from the stored chunk", d=0.1):
    return (_Chunk(note.id, text), note, d)


def _related_row(
    note_id,
    path,
    *,
    distance=0.1,
    text="SUPERSEDED SENTENCE from the stored chunk",
    content_hash="h2",
    embedded_content_hash="h2",
    chunks_truncated=False,
):
    """A row of `find_related_stmt`'s widened projection."""
    return type("R", (), {
        "note_id": note_id,
        "file_path": path,
        "title": path.removesuffix(".md"),
        "tags": ["topic"],
        "chunk_text": text,
        "distance": distance,
        "content_hash": content_hash,
        "embedded_content_hash": embedded_content_hash,
        "chunks_truncated": chunks_truncated,
    })()


class _Captured:
    """Every `_tracked` params dict this call would log."""

    def __init__(self):
        self.rows: list[tuple[str, dict, int]] = []

    async def __call__(self, tool, params, duration_ms, response_size):
        self.rows.append((tool, params, duration_ms))

    def params_for(self, tool):
        return next(p for t, p, _ in self.rows if t == tool)


@pytest.fixture
def captured(monkeypatch):
    cap = _Captured()
    monkeypatch.setattr(tools, "_log_usage", cap)
    return cap


@pytest.fixture(autouse=True)
def _no_provider(monkeypatch):
    async def fake_get_embedding(_text):
        return [1.0, 0.0, 0.0]

    monkeypatch.setattr(embeddings, "get_embedding", fake_get_embedding)


# --------------------------------------------------------------------------- #
# semantic_search
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_a_stale_row_is_present_marked_and_carries_no_note_text(
    monkeypatch, captured
):
    note = _Note(1, "Projects/Paxos.md", content_hash="h2", embedded_content_hash="h1")
    monkeypatch.setattr(
        tools, "async_session", lambda: _Session([[_semantic_row(note)]])
    )

    out = await tools.semantic_search_impl("consensus", limit=5)

    # Still found, still named, still ranked — nothing left the result set.
    assert "Projects/Paxos.md" in out
    assert "**Paxos**" in out or "Projects/Paxos" in out
    assert "stale: true" in out
    # The header count, and the trailing block that says what it means.
    assert "1 stale" in out
    assert "One of these notes changed after it was embedded" in out
    assert "read_note" in out
    # And **no text from the note**: not the stored chunk, and not a
    # substituted excerpt of its current bytes either.
    assert "SUPERSEDED" not in out
    assert "preview withheld" in out


@pytest.mark.asyncio
async def test_an_all_stale_result_set_survives_and_says_so(monkeypatch, captured):
    """A provider outage eventually leaves every note stale. The search must
    still answer, and the trailing block must count all of them — the plural
    branch of the footer."""
    rows = [
        _semantic_row(
            _Note(i, f"n{i}.md", content_hash="h2", embedded_content_hash="h1"),
            d=i / 10,
        )
        for i in range(1, 4)
    ]
    monkeypatch.setattr(tools, "async_session", lambda: _Session([rows]))

    out = await tools.semantic_search_impl("q", limit=5)

    assert out.count("stale: true") == 3
    assert "3 stale" in out
    assert "3 of these notes changed after they were embedded" in out
    assert out.count("preview withheld") == 3
    assert "SUPERSEDED" not in out
    # Every note is still named and still ranked.
    for i in range(1, 4):
        assert f"n{i}.md" in out


@pytest.mark.asyncio
async def test_the_header_count_is_rendered_when_it_is_zero(monkeypatch, captured):
    """An absent token is not evidence of absence: a caller must be able to
    tell "nothing here is stale" from a build that does not report it."""
    note = _Note(1, "Fresh.md")
    monkeypatch.setattr(
        tools,
        "async_session",
        lambda: _Session([[_semantic_row(note, text="a fresh excerpt")]]),
    )

    out = await tools.semantic_search_impl("q", limit=5)

    assert "0 stale" in out
    assert "0 truncated" in out
    # Per-row, only the true markers are rendered — `stale: false` on every row
    # is noise, not information.
    assert "stale: false" not in out
    assert "changed after they were embedded" not in out


@pytest.mark.asyncio
async def test_a_fresh_rows_preview_is_unchanged(monkeypatch, captured):
    note = _Note(1, "Fresh.md")
    monkeypatch.setattr(
        tools,
        "async_session",
        lambda: _Session([[_semantic_row(note, text="z" * 400)]]),
    )

    out = await tools.semantic_search_impl("q", limit=5)

    assert f"  > {'z' * 200}..." in out
    assert "preview withheld" not in out


@pytest.mark.asyncio
async def test_a_capped_note_is_marked_in_semantic_search(monkeypatch, captured):
    note = _Note(1, "Big.md", chunks_truncated=True)
    monkeypatch.setattr(
        tools,
        "async_session",
        lambda: _Session([[_semantic_row(note, text="the head of a huge note")]]),
    )

    out = await tools.semantic_search_impl("q", limit=5)

    assert "embedding_truncated: true" in out
    assert "1 truncated" in out
    assert str(MAX_CHUNKS_PER_NOTE) in out
    assert "tail is not reachable by semantic search" in out
    assert "`keyword_search` still covers the whole note" in out
    # Truncation is not staleness: the preview is kept.
    assert "the head of a huge note" in out


@pytest.mark.asyncio
async def test_a_row_can_be_both_stale_and_truncated(monkeypatch, captured):
    note = _Note(
        1, "BigAndOld.md", content_hash="h2", embedded_content_hash="h1",
        chunks_truncated=True,
    )
    monkeypatch.setattr(
        tools, "async_session", lambda: _Session([[_semantic_row(note)]])
    )

    out = await tools.semantic_search_impl("q", limit=5)

    assert "stale: true" in out
    assert "embedding_truncated: true" in out
    assert "1 stale" in out and "1 truncated" in out
    assert "SUPERSEDED" not in out


@pytest.mark.asyncio
async def test_an_edit_the_scan_has_not_seen_is_not_marked(monkeypatch, captured):
    """The declared bound (L2), pinned so it is not later re-described as a
    guarantee. Staleness is derived from `notes_metadata`, so it reports what
    the index has *committed*: a note edited on disk but not yet scanned still
    has two agreeing hashes and comes back unmarked."""
    not_yet_scanned = _Note(1, "JustEdited.md", content_hash="h1",
                            embedded_content_hash="h1")
    monkeypatch.setattr(
        tools,
        "async_session",
        lambda: _Session([[_semantic_row(not_yet_scanned, text="the old excerpt")]]),
    )

    out = await tools.semantic_search_impl("q", limit=5)

    assert "stale: true" not in out
    assert "0 stale" in out
    assert "the old excerpt" in out


@pytest.mark.asyncio
async def test_semantic_search_adds_no_usage_params_key(monkeypatch, captured):
    """The markers ride the rendered string. `usage_logs.params` keeps exactly
    the keys the analytics pages already read."""
    note = _Note(1, "Stale.md", embedded_content_hash="old")
    monkeypatch.setattr(
        tools, "async_session", lambda: _Session([[_semantic_row(note)]])
    )

    await tools.semantic_search_impl("q", limit=5)

    params = captured.params_for("semantic_search")
    assert "stale" not in params
    assert "stale_count" not in params
    assert "embedding_truncated" not in params
    assert set(params) <= {
        "query", "limit", "folder", "tags", "frontmatter",
        "embed_ms", "db_ms", "exact_fallback", "result_count", "result_paths",
    }


# --------------------------------------------------------------------------- #
# find_related
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_find_related_marks_a_stale_neighbour_and_withholds_it(
    monkeypatch, captured
):
    source = _Note(1, "Source.md")
    row = _related_row(2, "Neighbour.md", embedded_content_hash="old")
    monkeypatch.setattr(
        tools,
        "async_session",
        lambda: _FindRelatedSession(source, [[1.0, 0.0, 0.0]], [[row]]),
    )

    out = await tools.find_related_impl("Source.md", limit=5)

    assert "Neighbour.md" in out
    assert "stale: true" in out
    assert "1 stale" in out
    assert "SUPERSEDED" not in out
    assert "preview withheld" in out
    # The source itself is fresh, so no source line — asserted on the source
    # line's own wording, which names the path, rather than on a phrase the
    # per-row withheld notice happens to share.
    assert "`Source.md` changed after it was embedded" not in out
    assert "superseded question" not in out


@pytest.mark.asyncio
async def test_find_related_states_a_stale_source_on_the_ranked_path(
    monkeypatch, captured
):
    source = _Note(1, "Source.md", content_hash="h2", embedded_content_hash="h1")
    row = _related_row(2, "Neighbour.md", text="a fresh neighbour excerpt")
    monkeypatch.setattr(
        tools,
        "async_session",
        lambda: _FindRelatedSession(source, [[1.0, 0.0, 0.0]], [[row]]),
    )

    out = await tools.find_related_impl("Source.md", limit=5)

    assert "`Source.md` changed after it was embedded" in out
    assert "superseded question" in out
    # The neighbours are still returned, and the fresh one keeps its preview.
    assert "Neighbour.md" in out
    assert "a fresh neighbour excerpt" in out


@pytest.mark.asyncio
async def test_find_related_states_a_stale_source_on_the_empty_result_too(
    monkeypatch, captured
):
    """Where it matters most. A bare "no related notes" from a stale source is
    the reading a caller acts on — that the note has no neighbours — when the
    truth is that the vector searched with describes content the note no longer
    has."""
    source = _Note(1, "Source.md", content_hash="h2", embedded_content_hash="h1")
    monkeypatch.setattr(
        tools,
        "async_session",
        # Two empty batches: the vector query and its exact-fallback re-run.
        lambda: _FindRelatedSession(source, [[1.0, 0.0, 0.0]], [[], []]),
    )

    out = await tools.find_related_impl("Source.md", limit=5)

    assert "No related notes for `Source.md`" in out
    assert "changed after it was embedded" in out
    assert "superseded question" in out


@pytest.mark.asyncio
async def test_a_fresh_source_with_no_neighbours_stays_a_bare_zero_result(
    monkeypatch, captured
):
    source = _Note(1, "Source.md")
    monkeypatch.setattr(
        tools,
        "async_session",
        lambda: _FindRelatedSession(source, [[1.0, 0.0, 0.0]], [[], []]),
    )

    out = await tools.find_related_impl("Source.md", limit=5)

    assert out == "No related notes for `Source.md`"


@pytest.mark.asyncio
async def test_the_not_embedded_branch_is_unchanged(monkeypatch, captured):
    """A source with no vectors at all is a different fact with a different
    fix, and it keeps its own message and its own marker — it must not be
    replaced by the stale-source statement even though its hashes differ."""
    source = _Note(1, "Source.md", content_hash="h2", embedded_content_hash=None)
    monkeypatch.setattr(
        tools, "async_session", lambda: _FindRelatedSession(source, [])
    )

    out = await tools.find_related_impl("Source.md", limit=5)

    assert out == (
        "`Source.md` has not been embedded yet — "
        "the indexer is still catching up. Try again in a few minutes."
    )
    assert captured.params_for("find_related")["error"] == (
        tools._RELATED_SOURCE_NOT_EMBEDDED_MARKER
    )


@pytest.mark.asyncio
async def test_the_not_found_branch_is_unchanged(monkeypatch, captured):
    monkeypatch.setattr(
        tools, "async_session", lambda: _FindRelatedSession(None, [])
    )

    out = await tools.find_related_impl("Nope.md", limit=5)

    assert out == "Note not found: Nope.md"
    assert captured.params_for("find_related")["error"] == (
        tools._RELATED_SOURCE_NOT_FOUND_MARKER
    )


@pytest.mark.asyncio
async def test_find_related_renders_its_counts_at_zero(monkeypatch, captured):
    source = _Note(1, "Source.md")
    row = _related_row(2, "Neighbour.md", text="a fresh neighbour excerpt")
    monkeypatch.setattr(
        tools,
        "async_session",
        lambda: _FindRelatedSession(source, [[1.0, 0.0, 0.0]], [[row]]),
    )

    out = await tools.find_related_impl("Source.md", limit=5)

    assert "0 stale" in out and "0 truncated" in out
    assert "stale: false" not in out


@pytest.mark.asyncio
async def test_a_capped_note_is_marked_in_find_related(monkeypatch, captured):
    source = _Note(1, "Source.md")
    row = _related_row(2, "Big.md", text="the head of it", chunks_truncated=True)
    monkeypatch.setattr(
        tools,
        "async_session",
        lambda: _FindRelatedSession(source, [[1.0, 0.0, 0.0]], [[row]]),
    )

    out = await tools.find_related_impl("Source.md", limit=5)

    assert "embedding_truncated: true" in out
    assert "1 truncated" in out
    assert "the head of it" in out


@pytest.mark.asyncio
async def test_both_tools_treat_the_same_stale_note_the_same_way(
    monkeypatch, captured
):
    """A caller must not get a different answer depending on which vector tool
    it reached for."""
    note = _Note(9, "Same.md", content_hash="h2", embedded_content_hash="h1")
    monkeypatch.setattr(
        tools, "async_session", lambda: _Session([[_semantic_row(note)]])
    )
    semantic = await tools.semantic_search_impl("q", limit=5)

    source = _Note(1, "Source.md")
    row = _related_row(9, "Same.md", embedded_content_hash="h1")
    monkeypatch.setattr(
        tools,
        "async_session",
        lambda: _FindRelatedSession(source, [[1.0, 0.0, 0.0]], [[row]]),
    )
    related = await tools.find_related_impl("Source.md", limit=5)

    for out in (semantic, related):
        assert "Same.md" in out
        assert "stale: true" in out
        assert "preview withheld" in out
        assert "SUPERSEDED" not in out


@pytest.mark.asyncio
async def test_find_related_adds_no_usage_params_key(monkeypatch, captured):
    source = _Note(1, "Source.md", content_hash="h2", embedded_content_hash="h1")
    row = _related_row(2, "Neighbour.md", embedded_content_hash="old")
    monkeypatch.setattr(
        tools,
        "async_session",
        lambda: _FindRelatedSession(source, [[1.0, 0.0, 0.0]], [[row]]),
    )

    await tools.find_related_impl("Source.md", limit=5)

    params = captured.params_for("find_related")
    assert "stale" not in params
    assert "source_stale" not in params
    assert "embedding_truncated" not in params
