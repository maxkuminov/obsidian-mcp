"""#127 / D2 — `EMBEDDING_EXCLUDE_PATTERNS` edits converge on the next pass.

The embed pass selects on `embedded_content_hash IS NULL OR != content_hash`,
so it is driven entirely by *content* changes. The exclusion patterns are
configuration: editing them changes no note's content, so the new configuration
only ever reached notes that happened to be edited afterwards. Both directions
were permanent and both are the failure this product ranks highest —

* **adding** a pattern left the matching notes' vectors in place, so an
  excluded note kept answering `semantic_search` for ever;
* **removing** one left the stamp the exclusion branch wrote beside zero
  vectors, so a now-included note stayed silently absent from
  `semantic_search`: hash-equal, never re-selected, nothing to indicate it.

The dangerous naive fix is a sweep that deletes `note_embeddings` by note id.
It must not be that: a move changes `file_path` while leaving `content_hash`
untouched, so a decision taken about an excluded path could delete the vectors
of a row that has since become an included one and stamp it embedded with none.
Every write here therefore goes through `certify_embedded`'s
`id + content_hash + file_path` predicate, stamp before delete, per-note commit.

Real PostgreSQL, real `embed_vault`. Skipped unless `PGVECTOR_TEST_ADMIN_URL`
is set — see `_harness.py`.
"""
import hashlib

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import src.database
from src.config import settings
from src.models.db import NoteEmbedding, NoteMetadata
from src.services import embeddings as embeddings_service
from src.services import indexer
import _harness

pytestmark = [
    _harness.requires_pgvector,
    pytest.mark.asyncio(loop_scope="module"),
]

DIM = 8
BODY = "a body with enough words in it to produce exactly one chunk\n"
EXCLUDED = "Private/A.md"
INCLUDED = "Public/A.md"


def content_hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


@pytest.fixture(scope="module")
def migrated_url():
    yield from _harness.throwaway_database("exclusion_reconcile_127", DIM)


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def sessionmaker(migrated_url):
    engine = create_async_engine(migrated_url, poolclass=None)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield maker
    await engine.dispose()


class _Patterns:
    """The exclusion configuration, rebindable mid-test.

    It has to be settable *between* passes: the whole point is that the pass
    reads the configuration as it is now, not as it was when the row was
    stamped.
    """

    def __init__(self, monkeypatch):
        self._monkeypatch = monkeypatch
        self.set(["Private/*"])

    def set(self, patterns):
        self._monkeypatch.setattr(
            settings, "embedding_exclude_patterns", patterns, raising=False
        )
        self._monkeypatch.setattr(
            indexer.settings, "embedding_exclude_patterns", patterns, raising=False
        )


@pytest_asyncio.fixture(loop_scope="module")
async def vault(sessionmaker, monkeypatch, tmp_path):
    root = tmp_path / "vault"
    (root / "Private").mkdir(parents=True)
    (root / "Public").mkdir(parents=True)
    monkeypatch.setattr(settings, "vault_path", str(root), raising=False)
    monkeypatch.setattr(indexer.settings, "vault_path", str(root), raising=False)
    monkeypatch.setattr(indexer, "async_session", sessionmaker)
    monkeypatch.setattr(src.database, "async_session", sessionmaker)
    monkeypatch.setattr(indexer, "_is_paused", lambda: False)

    calls = {"n": 0}

    async def fake_batch(chunks):
        calls["n"] += 1
        return [[1.0] + [0.0] * (DIM - 1) for _ in chunks]

    monkeypatch.setattr(embeddings_service, "get_embeddings_batch", fake_batch)

    async with sessionmaker() as session:
        await session.execute(text("DELETE FROM note_embeddings"))
        await session.execute(text("DELETE FROM notes_metadata"))
        await session.commit()

    yield root, _Patterns(monkeypatch), calls


async def seed(sessionmaker, root, rel: str, body: str = BODY) -> int:
    (root / rel).write_text(body, encoding="utf-8")
    async with sessionmaker() as session:
        note = NoteMetadata(
            user_id=None, file_path=rel, title=rel.rsplit("/", 1)[-1][:-3],
            tags=[], frontmatter={}, content_hash=content_hash(body),
            embedded_content_hash=None,
        )
        session.add(note)
        await session.commit()
        return note.id


async def state(sessionmaker, note_id: int):
    """`(file_path, embedded_content_hash, vector count)`."""
    async with sessionmaker() as session:
        row = (await session.execute(
            select(NoteMetadata.file_path, NoteMetadata.embedded_content_hash)
            .where(NoteMetadata.id == note_id)
        )).first()
        vectors = (await session.execute(
            select(NoteEmbedding.id).where(NoteEmbedding.note_id == note_id)
        )).all()
    return row[0], row[1], len(vectors)


# ── the two directions ──────────────────────────────────────────────────────
async def test_adding_a_pattern_removes_existing_vectors(sessionmaker, vault):
    root, patterns, _ = vault
    note_id = await seed(sessionmaker, root, INCLUDED)

    await indexer.embed_vault(user_id=None)
    path, stamp, vectors = await state(sessionmaker, note_id)
    assert (path, stamp) == (INCLUDED, content_hash(BODY))
    assert vectors > 0, "an included note should have been embedded"

    # The operator adds a pattern. No content changes, so the backlog selects
    # nothing at all — this is the exact state the sweep exists for.
    patterns.set(["Private/*", "Public/*"])
    await indexer.embed_vault(user_id=None)

    path, stamp, vectors = await state(sessionmaker, note_id)
    assert (path, stamp) == (INCLUDED, content_hash(BODY))
    assert vectors == 0, "an excluded note must stop answering semantic search"


async def test_removing_a_pattern_restores_vectors(sessionmaker, vault):
    root, patterns, _ = vault
    note_id = await seed(sessionmaker, root, EXCLUDED)

    await indexer.embed_vault(user_id=None)
    path, stamp, vectors = await state(sessionmaker, note_id)
    assert (path, stamp, vectors) == (EXCLUDED, content_hash(BODY), 0)

    patterns.set([])
    await indexer.embed_vault(user_id=None)

    path, stamp, vectors = await state(sessionmaker, note_id)
    assert (path, stamp) == (EXCLUDED, content_hash(BODY))
    assert vectors > 0, "a now-included note must be searchable again"


async def test_the_sweep_is_idempotent_across_passes(sessionmaker, vault):
    """A second pass over an already-converged database writes nothing.

    The failure this rules out is a sweep that re-stamps or re-embeds every
    certification-current row on every tick — 2,500 notes' worth of provider
    calls every five minutes.
    """
    root, patterns, calls = vault
    await seed(sessionmaker, root, INCLUDED)
    await seed(sessionmaker, root, EXCLUDED)
    await indexer.embed_vault(user_id=None)

    before = calls["n"]
    await indexer.embed_vault(user_id=None)
    await indexer.embed_vault(user_id=None)
    assert calls["n"] == before, "a converged database must cost no provider call"


# ── the three defined exceptions ────────────────────────────────────────────
async def test_a_genuinely_empty_note_is_not_rewritten(sessionmaker, vault):
    """Zero chunks is already the correct state — current certification, zero
    vectors — and rewriting it would re-stamp an unchanged row every pass."""
    root, patterns, calls = vault
    empty_body = "```\njust a fenced code block, which cleans to nothing\n```\n"
    note_id = await seed(sessionmaker, root, "Public/Empty.md", body=empty_body)

    await indexer.embed_vault(user_id=None)
    path, stamp, vectors = await state(sessionmaker, note_id)
    assert (stamp, vectors) == (content_hash(empty_body), 0)

    async with sessionmaker() as session:
        before = (await session.execute(text(
            "SELECT indexed_at FROM notes_metadata WHERE id = :i"
        ), {"i": note_id})).scalar_one()

    calls_before = calls["n"]
    await indexer.embed_vault(user_id=None)

    assert calls["n"] == calls_before, "no provider call for an empty note"
    async with sessionmaker() as session:
        after = (await session.execute(text(
            "SELECT indexed_at FROM notes_metadata WHERE id = :i"
        ), {"i": note_id})).scalar_one()
    assert after == before
    assert (await state(sessionmaker, note_id))[1:] == (
        content_hash(empty_body), 0
    )


async def test_bytes_that_no_longer_match_the_row_are_left_to_the_backlog(
    sessionmaker, vault
):
    """A row whose on-disk bytes have changed but whose scan has not run yet.

    Nothing may be certified against it, so the sweep writes nothing and the
    ordinary backlog picks it up once the scan refreshes the hash.
    """
    root, patterns, _ = vault
    note_id = await seed(sessionmaker, root, EXCLUDED)
    await indexer.embed_vault(user_id=None)
    assert (await state(sessionmaker, note_id))[1:] == (content_hash(BODY), 0)

    # Include the note *and* change the file underneath the stale row.
    patterns.set([])
    (root / EXCLUDED).write_text("entirely different bytes\n", encoding="utf-8")

    await indexer.embed_vault(user_id=None)
    path, stamp, vectors = await state(sessionmaker, note_id)
    assert stamp == content_hash(BODY), "nothing may be re-certified"
    assert vectors == 0, "and nothing may be written on a stale decision"


async def test_a_concurrent_move_defeats_the_write_not_the_vault(
    sessionmaker, vault, monkeypatch
):
    """The certified predicate is what makes the sweep safe.

    A move commits a new `file_path` with an unchanged `content_hash` between
    the sweep's decision and its certifying UPDATE. Stamping by id would delete
    the vectors of a row that is now *included* and record it embedded with
    none — hash-equal, never re-selected, silently absent from
    `semantic_search` for ever.
    """
    root, patterns, _ = vault
    note_id = await seed(sessionmaker, root, INCLUDED)
    await indexer.embed_vault(user_id=None)
    assert (await state(sessionmaker, note_id))[2] > 0

    patterns.set(["Public/*"])

    # The move lands the instant the sweep has decided and is about to write.
    real_certify = indexer.certify_embedded
    moved = {"done": False}

    async def moving_certify(session, nid, chash, cpath, **kw):
        if not moved["done"] and nid == note_id:
            moved["done"] = True
            async with sessionmaker() as other:
                await other.execute(text(
                    "UPDATE notes_metadata SET file_path = :p WHERE id = :i"
                ), {"p": "Elsewhere/A.md", "i": note_id})
                await other.commit()
        return await real_certify(session, nid, chash, cpath, **kw)

    monkeypatch.setattr(indexer, "certify_embedded", moving_certify)
    await indexer.embed_vault(user_id=None)
    monkeypatch.undo()

    path, stamp, vectors = await state(sessionmaker, note_id)
    assert moved["done"], "the race did not happen; the test proves nothing"
    assert path == "Elsewhere/A.md"
    assert vectors > 0, "no vector may be deleted on a stale decision"


# ── the pause ───────────────────────────────────────────────────────────────
async def test_a_pause_stops_between_notes_and_the_next_pass_converges(
    sessionmaker, vault, monkeypatch
):
    """Per-note commits mean an interrupted sweep leaves repaired rows repaired
    and un-repaired rows exactly as they were — never a half-written note."""
    root, patterns, _ = vault
    a = await seed(sessionmaker, root, "Public/One.md")
    b = await seed(sessionmaker, root, "Public/Two.md")
    c = await seed(sessionmaker, root, "Public/Three.md")
    await indexer.embed_vault(user_id=None)
    for nid in (a, b, c):
        assert (await state(sessionmaker, nid))[2] > 0

    patterns.set(["Public/*"])

    # Pause after the first note the sweep repairs.
    seen = {"n": 0}

    def paused():
        seen["n"] += 1
        # The backlog is empty on this pass, so every call comes from the
        # sweep, once per note before it is touched: let the first through
        # and pause before the second.
        return seen["n"] > 1

    monkeypatch.setattr(indexer, "_is_paused", paused)
    await indexer.embed_vault(user_id=None)
    monkeypatch.setattr(indexer, "_is_paused", lambda: False)

    repaired = [nid for nid in (a, b, c) if (await state(sessionmaker, nid))[2] == 0]
    assert len(repaired) == 1, "the pause must stop the sweep between notes"

    await indexer.embed_vault(user_id=None)
    for nid in (a, b, c):
        path, stamp, vectors = await state(sessionmaker, nid)
        assert vectors == 0, "a fresh sweep must complete the remainder"
        assert stamp == content_hash(BODY)
