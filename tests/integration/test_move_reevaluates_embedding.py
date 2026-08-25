"""A move re-opens the exclusion decision (adversarial round 3, MAJOR).

`embedded_content_hash` records that a row's **current content** has been dealt
with. What it does not record is *how* — and one of the inputs to that is the
path, because `embed_vault`'s exclusion branch matches
`EMBEDDING_EXCLUDE_PATTERNS` against `file_path`. A move changes that answer
while changing no content, so a stamp carried across it freezes the old
decision permanently: the pass selects on
`embedded_content_hash IS NULL OR embedded_content_hash != content_hash`, and a
preserved stamp makes both disjuncts false forever.

Round 2 closed the *move-before-certify* ordering with a conditional
certification predicate that includes the path. This module is about the mirror
ordering, **certify-then-move**, which that predicate cannot see: the stamp is
already committed and correct when the move happens.

Both boundary directions are wrong, and both are permanent:

* `Private/A.md` → `Public/A.md` — the exclusion branch had deleted the note's
  vectors and stamped it. After the move the note is *included*, has no
  vectors, is never selected again, and is silently absent from
  `semantic_search`.
* `Public/A.md` → `Private/A.md` — the note keeps the vectors it was embedded
  with. After the move it is *excluded* and still searchable.

The repair is one clause on each of the two statements that change
`file_path` — `move_note`'s metadata UPDATE and the indexer's id-preserving
move detection — setting `embedded_content_hash` to NULL. NULL means
"re-evaluate at the next pass" and needs no knowledge of the exclusion
configuration at move time.

This module drives the **real** `embed_vault` and the **real** move paths
against a throwaway PostgreSQL, because the property under test is a database
one: what the next pass's selection predicate does with the row the move left
behind. Skipped unless `PGVECTOR_TEST_ADMIN_URL` is set — see `_harness.py`.
"""
import hashlib

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import src.database
import src.mcp_server.tools as tools
from src.mcp_server.auth import current_permission
from src.models.db import NoteEmbedding, NoteMetadata
from src.services import embeddings as embeddings_service
from src.services import indexer
import _harness
from src.config import settings

pytestmark = [
    _harness.requires_pgvector,
    pytest.mark.asyncio(loop_scope="module"),
]

DIM = int(settings.embedding_dimensions)

EXCLUDED = "Private/A.md"
INCLUDED = "Public/A.md"
BODY = "the note body, unchanged by any move\n"


def content_hash(text_body: str) -> str:
    """Exactly what the indexer records, so the selection predicate agrees."""
    return hashlib.sha256(text_body.encode("utf-8")).hexdigest()


@pytest.fixture(scope="module")
def migrated_url():
    yield from _harness.throwaway_database("move_reembed", DIM)


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def sessionmaker(migrated_url):
    engine = create_async_engine(migrated_url, poolclass=None)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest_asyncio.fixture(loop_scope="module")
async def vault(sessionmaker, monkeypatch, tmp_path):
    """A single-user vault with both folders, wired to the throwaway database.

    Single-user mode (`user_id=None`) keeps the tools out of the multi-user
    admission and confirmation machinery — the property under test is about the
    embedding certification, not about who is asking.
    """
    root = tmp_path / "vault"
    (root / "Private").mkdir(parents=True)
    (root / "Public").mkdir(parents=True)

    monkeypatch.setattr(settings, "vault_path", str(root), raising=False)
    monkeypatch.setattr(tools.settings, "vault_path", str(root), raising=False)
    monkeypatch.setattr(indexer.settings, "vault_path", str(root), raising=False)
    monkeypatch.setattr(
        settings, "embedding_exclude_patterns", ["Private/*"], raising=False
    )
    monkeypatch.setattr(
        indexer.settings, "embedding_exclude_patterns", ["Private/*"], raising=False
    )
    monkeypatch.setattr(indexer, "async_session", sessionmaker)
    monkeypatch.setattr(tools, "async_session", sessionmaker)
    monkeypatch.setattr(src.database, "async_session", sessionmaker)
    monkeypatch.setattr(indexer, "_is_paused", lambda: False)

    async def fake_batch(chunks):
        # One deterministic non-zero unit vector per chunk. The values do not
        # matter here; that a vector *exists* is the whole assertion.
        return [[1.0] + [0.0] * (DIM - 1) for _ in chunks]

    monkeypatch.setattr(embeddings_service, "get_embeddings_batch", fake_batch)

    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(tools, "_log_usage", noop)
    permission = current_permission.set("readwrite")

    async with sessionmaker() as session:
        await session.execute(text("DELETE FROM note_embeddings"))
        await session.execute(text("DELETE FROM notes_metadata"))
        await session.commit()

    try:
        yield root
    finally:
        current_permission.reset(permission)


async def seed(sessionmaker, root, rel: str, *, with_vector: bool):
    """One indexed note at `rel`, optionally already carrying a vector."""
    (root / rel).write_text(BODY, encoding="utf-8")
    async with sessionmaker() as session:
        note = NoteMetadata(
            file_path=rel,
            title="A",
            content_hash=content_hash(BODY),
            embedded_content_hash=None,
            user_id=None,
        )
        session.add(note)
        await session.flush()
        if with_vector:
            session.add(
                NoteEmbedding(
                    note_id=note.id,
                    chunk_index=0,
                    chunk_text="stale chunk",
                    embedding=[1.0] + [0.0] * (DIM - 1),
                )
            )
        await session.commit()
        return note.id


async def state(sessionmaker, note_id: int):
    """`(file_path, embedded_content_hash, vector count)` for the row."""
    async with sessionmaker() as session:
        row = (
            await session.execute(
                select(NoteMetadata.file_path, NoteMetadata.embedded_content_hash)
                .where(NoteMetadata.id == note_id)
            )
        ).first()
        vectors = (
            await session.execute(
                select(NoteEmbedding.id).where(NoteEmbedding.note_id == note_id)
            )
        ).all()
    return row[0], row[1], len(vectors)


async def move_with_the_tool(root, frm: str, to: str) -> None:
    result = await tools.move_note_impl(frm, to)
    assert "Moved" in result, result


async def move_with_the_indexer(root, frm: str, to: str) -> None:
    """The id-preserving move-detection path: rename on disk, then scan.

    Content is unchanged, so the pass matches the vanished path's hash against
    the new one and updates the row in place rather than pruning and
    re-inserting it — which is exactly why this path needs the clause: the
    prune-and-insert path would have produced a fresh row with a null stamp.
    """
    (root / frm).rename(root / to)
    await indexer.index_vault(user_id=None)


MOVERS = {"move_note": move_with_the_tool, "indexer": move_with_the_indexer}


# ── certify, then move ──────────────────────────────────────────────────────


@pytest.mark.parametrize("mover", list(MOVERS))
async def test_certify_then_move_out_of_the_exclusion_re_embeds(
    sessionmaker, vault, mover
):
    """The failing input: excluded → included, stamped before the move.

    Without the repair the note ends up included, with zero vectors, and
    unselectable forever — absent from `semantic_search` with nothing to say so.
    """
    note_id = await seed(sessionmaker, vault, EXCLUDED, with_vector=True)

    await indexer.embed_vault(user_id=None)
    path, stamp, vectors = await state(sessionmaker, note_id)
    assert (path, stamp, vectors) == (EXCLUDED, content_hash(BODY), 0), (
        "the exclusion branch should have stamped the row and dropped its vectors"
    )

    await MOVERS[mover](vault, EXCLUDED, INCLUDED)
    path, stamp, _ = await state(sessionmaker, note_id)
    assert path == INCLUDED
    assert stamp is None, "the move must re-open the exclusion decision"

    await indexer.embed_vault(user_id=None)
    path, stamp, vectors = await state(sessionmaker, note_id)
    assert path == INCLUDED
    assert stamp == content_hash(BODY)
    assert vectors > 0, "an included note must be searchable after the move"


@pytest.mark.parametrize("mover", list(MOVERS))
async def test_certify_then_move_into_the_exclusion_drops_the_vectors(
    sessionmaker, vault, mover
):
    """The other direction: included → excluded, embedded before the move.

    Without the repair the note keeps the vectors it was embedded with and
    stays searchable although it is now excluded.
    """
    note_id = await seed(sessionmaker, vault, INCLUDED, with_vector=False)

    await indexer.embed_vault(user_id=None)
    path, stamp, vectors = await state(sessionmaker, note_id)
    assert path == INCLUDED and stamp == content_hash(BODY)
    assert vectors > 0, "an included note should have been embedded"

    await MOVERS[mover](vault, INCLUDED, EXCLUDED)
    path, stamp, _ = await state(sessionmaker, note_id)
    assert path == EXCLUDED
    assert stamp is None, "the move must re-open the exclusion decision"

    await indexer.embed_vault(user_id=None)
    path, stamp, vectors = await state(sessionmaker, note_id)
    assert path == EXCLUDED
    assert stamp == content_hash(BODY)
    assert vectors == 0, "an excluded note must not stay searchable"


# ── move, then certify ──────────────────────────────────────────────────────


@pytest.mark.parametrize("mover", list(MOVERS))
async def test_move_then_certify_out_of_the_exclusion_embeds(
    sessionmaker, vault, mover
):
    """The ordinary order, asserted as the control for the pair above.

    The row is unstamped when it moves, so the next pass selects it whatever
    the repair does; what this pins is the *end state* — vectors present
    because the note is now included.
    """
    note_id = await seed(sessionmaker, vault, EXCLUDED, with_vector=True)

    await MOVERS[mover](vault, EXCLUDED, INCLUDED)
    await indexer.embed_vault(user_id=None)

    path, stamp, vectors = await state(sessionmaker, note_id)
    assert (path, stamp) == (INCLUDED, content_hash(BODY))
    assert vectors > 0


@pytest.mark.parametrize("mover", list(MOVERS))
async def test_move_then_certify_into_the_exclusion_drops_the_vectors(
    sessionmaker, vault, mover
):
    note_id = await seed(sessionmaker, vault, INCLUDED, with_vector=True)

    await MOVERS[mover](vault, INCLUDED, EXCLUDED)
    await indexer.embed_vault(user_id=None)

    path, stamp, vectors = await state(sessionmaker, note_id)
    assert (path, stamp) == (EXCLUDED, content_hash(BODY))
    assert vectors == 0


# ── the id is preserved, so this really is the move path ────────────────────


@pytest.mark.parametrize("mover", list(MOVERS))
async def test_the_move_preserves_the_row_id(sessionmaker, vault, mover):
    """Guard the guard.

    Every assertion above reads the row *by id*. If a mover pruned and
    re-inserted instead of updating in place, the stamp would be null for a
    reason that has nothing to do with the repair and these tests would pass
    against the unrepaired code.
    """
    note_id = await seed(sessionmaker, vault, EXCLUDED, with_vector=True)
    await indexer.embed_vault(user_id=None)
    await MOVERS[mover](vault, EXCLUDED, INCLUDED)

    async with sessionmaker() as session:
        rows = (
            await session.execute(
                select(NoteMetadata.id, NoteMetadata.file_path)
            )
        ).all()
    assert [(r.id, r.file_path) for r in rows] == [(note_id, INCLUDED)]
