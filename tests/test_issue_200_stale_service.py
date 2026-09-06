"""#200 — `semantic_search` annotates a stale row and withholds its preview.

Both vector paths returned the stored `chunk_text[:500]` with no
`embedded_content_hash` predicate, so during the window #201 hides an agent was
handed *superseded note text* as a current result with nothing marking it.
Every other field on that row — path, title, tags, frontmatter — had been
refreshed by the scan; only the chunk text was out of date, and only the chunk
text is quotable as the note's content.

Filtering was rejected in the issue and in the design: on hash equality it
would hide every note edited in the last five minutes and the entire vault
during a provider outage, and because the owner predicate makes every query
here a filtered one whose zero-row result re-runs exactly, it would turn an
outage into an O(n) scan of the embedding table on every search. So the row
stays, ranked where its stored vector puts it, and says what it is.

This file exercises the **service**; the rendered tool output is the tool
slice's. Fully offline: the session and the query embedding are faked.
"""
from __future__ import annotations

import os
import tempfile

import pytest

os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
os.chdir(tempfile.gettempdir())

from src.services import embeddings, timing  # noqa: E402
from src.services.embeddings import semantic_search  # noqa: E402


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class _Result:
    def __init__(self, rows):
        self._rows = list(rows)

    def fetchall(self):
        return self._rows


class _Session:
    """Replays one scripted batch per non-`SET LOCAL` statement."""

    def __init__(self, batches):
        self.statements: list[str] = []
        self._batches = list(batches)

    async def execute(self, clause, *_a, **_k):
        sql = str(clause)
        self.statements.append(sql)
        if sql.lstrip().upper().startswith("SET LOCAL"):
            return _Result([])
        return _Result(self._batches.pop(0) if self._batches else [])


class _Chunk:
    def __init__(self, note_id, text):
        self.note_id = note_id
        self.chunk_index = 0
        self.embedding = [1.0, 0.0, 0.0]
        self.chunk_text = text


class _Note:
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
        self.tags = ["fixture"]
        self.content_hash = content_hash
        self.embedded_content_hash = embedded_content_hash
        self.chunks_truncated = chunks_truncated


def _row(note, *, text="the note's stored chunk text", distance=0.1):
    return (_Chunk(note.id, text), note, distance)


@pytest.fixture(autouse=True)
def _no_provider(monkeypatch):
    async def fake_get_embedding(_text):
        return [1.0, 0.0, 0.0]

    monkeypatch.setattr(embeddings, "get_embedding", fake_get_embedding)


@pytest.fixture
def holder():
    """A `_tracked`-owned timing holder, so `record`/`add_ms` have somewhere to
    write."""
    token = timing.begin()
    yield timing.current()
    timing.clear(token)


# --------------------------------------------------------------------------- #
# Staleness
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_a_stale_row_is_returned_marked_and_has_no_preview(holder):
    note = _Note(1, "Projects/Raft.md", content_hash="h2", embedded_content_hash="h1")
    session = _Session([[_row(note, text="Raft elects a leader by timeout")]])

    results = await semantic_search(session, "consensus", limit=10)

    assert [r["path"] for r in results] == ["Projects/Raft.md"]
    assert results[0]["stale"] is True
    # The preview is **withheld**, not clipped: it is the only field that is a
    # verbatim quotation of the note's text and the one an agent would paste
    # into an answer.
    assert results[0]["chunk"] is None
    # Everything else survives. `path`/`title`/`tags` came from the metadata
    # row the *scan* refreshed — which is why the row is stale at all — and the
    # similarity is a retrieval score, not a claim about content.
    assert results[0]["title"] == "Projects/Raft"
    assert results[0]["tags"] == ["fixture"]
    assert isinstance(results[0]["similarity"], float)
    assert results[0]["chunk_index"] == 0


@pytest.mark.asyncio
async def test_a_null_embedded_hash_is_stale(holder):
    """`IS DISTINCT FROM`, never `!=`: a note that has never been embedded, or
    whose certification a move cleared, holds NULL and must read stale rather
    than NULL-propagating into "fresh"."""
    note = _Note(2, "New.md", content_hash="h9", embedded_content_hash=None)
    session = _Session([[_row(note)]])

    results = await semantic_search(session, "q", limit=10)

    assert results[0]["stale"] is True
    assert results[0]["chunk"] is None


@pytest.mark.asyncio
async def test_a_fresh_row_keeps_its_five_hundred_character_preview(holder):
    note = _Note(3, "Fresh.md", content_hash="h2", embedded_content_hash="h2")
    long_text = "z" * 900
    session = _Session([[_row(note, text=long_text)]])

    results = await semantic_search(session, "q", limit=10)

    assert results[0]["stale"] is False
    assert results[0]["chunk"] == "z" * 500


@pytest.mark.asyncio
async def test_the_withheld_preview_carries_no_text_from_the_note(holder):
    """The substitute must not be the note's current leading text either: that
    is a different span from the one that matched, presented where the matching
    span goes — a fabricated excerpt, worse than none. At the service layer the
    field is simply absent."""
    note = _Note(4, "Moved.md", content_hash="h2", embedded_content_hash="h1")
    session = _Session([[_row(note, text="SUPERSEDED SENTENCE")]])

    results = await semantic_search(session, "q", limit=10)

    assert results[0]["chunk"] is None
    assert "SUPERSEDED" not in repr(results[0])


# --------------------------------------------------------------------------- #
# Truncation
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_embedding_truncated_mirrors_the_column(holder):
    """Read from the durable marker, never inferred from the number of chunk
    rows: a capped note holds exactly the cap and is indistinguishable by count
    from a note that legitimately produces that many."""
    capped = _Note(5, "Big.md", chunks_truncated=True)
    plain = _Note(6, "Small.md", chunks_truncated=False)
    session = _Session([[_row(capped, distance=0.1), _row(plain, distance=0.2)]])

    results = await semantic_search(session, "q", limit=10)

    by_path = {r["path"]: r for r in results}
    assert by_path["Big.md"]["embedding_truncated"] is True
    assert by_path["Small.md"]["embedding_truncated"] is False
    # Truncation and staleness are independent: a capped note is not stale.
    assert by_path["Big.md"]["stale"] is False
    assert by_path["Big.md"]["chunk"] is not None


# --------------------------------------------------------------------------- #
# The result set does not move
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_membership_and_order_are_unchanged_by_staleness(holder):
    """The recall SLO's baseline is set-recall over notes returned, so nothing
    may leave a result set on account of being stale — the whole vault is stale
    during a provider outage."""
    rows = [
        _row(_Note(1, "a.md", embedded_content_hash="old"), distance=0.9),
        _row(_Note(2, "b.md"), distance=0.1),
        _row(_Note(3, "c.md", embedded_content_hash=None), distance=0.4),
    ]
    session = _Session([rows])

    results = await semantic_search(session, "q", limit=10)

    # Re-sorted by distance, as before; two of the three are stale and both
    # kept their rank.
    assert [r["path"] for r in results] == ["b.md", "c.md", "a.md"]
    assert [r["stale"] for r in results] == [False, True, True]


@pytest.mark.asyncio
async def test_an_all_stale_corpus_returns_its_usual_results_without_a_fallback(
    holder,
):
    """A total provider outage leaves every note edited since the last pass —
    eventually all of them. The search must still answer, and staleness must
    not arm the O(n) exact fallback."""
    rows = [
        _row(_Note(i, f"n{i}.md", embedded_content_hash="old"), distance=i / 10)
        for i in range(1, 6)
    ]
    session = _Session([rows])

    results = await semantic_search(session, "q", limit=10)

    assert len(results) == 5
    assert all(r["stale"] is True for r in results)
    assert all(r["chunk"] is None for r in results)
    assert "SET LOCAL enable_indexscan = off" not in " | ".join(session.statements)
    assert timing.current()["exact_fallback"] is False


# --------------------------------------------------------------------------- #
# The exact fallback annotates identically
# --------------------------------------------------------------------------- #
#
# The zero-row safety net re-runs the *identical* statement with index scans
# off, and staleness is computed after it, from the already-hydrated
# `NoteMetadata`. Nothing in the annotation is supposed to know which of the
# two runs produced the rows — but "supposed to" is the part a later change
# breaks silently, because the fallback path is the rare one and every ordinary
# test drives the fast one. These pin the annotation on the path an agent hits
# exactly when the index is least able to help it.


@pytest.mark.asyncio
async def test_the_exact_fallback_annotates_a_mixed_result_identically(holder):
    """Fresh, stale-by-hash and never-embedded, in one fallback result.

    Order and membership are the fast path's — the fallback is the same
    statement — and the markers are per row rather than per path.
    """
    fresh = _Note(1, "Fresh.md", content_hash="h2", embedded_content_hash="h2")
    edited = _Note(2, "Edited.md", content_hash="h2", embedded_content_hash="h1")
    never = _Note(3, "Never.md", content_hash="h9", embedded_content_hash=None)
    rows = [
        _row(fresh, text="a current excerpt", distance=0.1),
        _row(edited, text="SUPERSEDED text", distance=0.2),
        _row(never, text="SUPERSEDED text", distance=0.3),
    ]
    # The first run comes back empty, which is what arms the fallback.
    session = _Session([[], rows])

    results = await semantic_search(session, "q", limit=10)

    assert "SET LOCAL enable_indexscan = off" in " | ".join(session.statements)
    assert timing.current()["exact_fallback"] is True
    assert [r["path"] for r in results] == [
        "Fresh.md", "Edited.md", "Never.md"
    ], "the fallback changed the order or the membership of the result"
    assert [r["stale"] for r in results] == [False, True, True], (
        "`IS DISTINCT FROM`: a NULL `embedded_content_hash` is stale, not fresh"
    )
    assert results[0]["chunk"] == "a current excerpt"
    assert [r["chunk"] for r in results[1:]] == [None, None]
    assert "SUPERSEDED" not in repr(results)


@pytest.mark.asyncio
async def test_the_exact_fallback_withholds_every_preview_when_all_are_stale(
    holder,
):
    """The outage shape, reached through the fallback.

    A provider outage eventually makes every row stale, and an outage is also
    when the HNSW window is most likely to come back empty and arm the exact
    re-run. The two compose: every row is returned, ranked and named, and not
    one of them quotes superseded text.
    """
    rows = [
        _row(
            _Note(i, f"n{i}.md", embedded_content_hash="old"),
            text="SUPERSEDED text",
            distance=i / 10,
        )
        for i in range(1, 4)
    ]
    session = _Session([[], rows])

    results = await semantic_search(session, "q", limit=10)

    assert timing.current()["exact_fallback"] is True
    assert [r["path"] for r in results] == ["n1.md", "n2.md", "n3.md"]
    assert all(r["stale"] is True for r in results)
    assert all(r["chunk"] is None for r in results)


@pytest.mark.asyncio
async def test_no_predicate_or_set_local_changed(holder):
    """D2 adds columns and post-processing only. The `SET LOCAL`s are
    correctness, not tuning (see `docs/architecture/search.md`), and the
    staleness columns are read from the already-hydrated metadata row rather
    than filtered on."""
    note = _Note(1, "a.md", embedded_content_hash="old")
    session = _Session([[_row(note)]])

    await semantic_search(session, "q", limit=5)

    set_locals = [
        s for s in session.statements if s.lstrip().upper().startswith("SET LOCAL")
    ]
    assert set_locals == [
        "SET LOCAL hnsw.ef_search = 80",
        "SET LOCAL random_page_cost = 1.1",
        "SET LOCAL hnsw.iterative_scan = 'relaxed_order'",
    ]
    selects = [
        s for s in session.statements if not s.lstrip().upper().startswith("SET LOCAL")
    ]
    assert len(selects) == 1
    sql = selects[0]
    assert "embedded_content_hash" not in sql.split("WHERE", 1)[-1]
    assert "chunks_truncated" not in sql.split("WHERE", 1)[-1]


@pytest.mark.asyncio
async def test_every_row_carries_both_annotations(holder):
    """Always present, including when nothing is stale: an absent token is not
    evidence of absence, and a caller must be able to tell "nothing here is
    stale" from "this build does not report staleness"."""
    session = _Session([[_row(_Note(1, "a.md")), _row(_Note(2, "b.md"), distance=0.2)]])

    results = await semantic_search(session, "q", limit=10)

    for r in results:
        assert set(r) == {
            "path", "title", "tags", "chunk", "chunk_index", "similarity",
            "stale", "embedding_truncated",
        }
        assert r["stale"] is False
        assert r["embedding_truncated"] is False


@pytest.mark.asyncio
async def test_the_dedupe_keeps_the_best_chunk_and_annotates_it(holder):
    """One row per note, at its nearest chunk — and the annotation belongs to
    the note, so both of its chunks would have carried the same one."""
    stale = _Note(1, "a.md", embedded_content_hash="old")
    rows = [
        _row(stale, text="far chunk", distance=0.8),
        _row(stale, text="near chunk", distance=0.1),
    ]
    session = _Session([rows])

    results = await semantic_search(session, "q", limit=10)

    assert len(results) == 1
    assert results[0]["stale"] is True
    assert results[0]["chunk"] is None
