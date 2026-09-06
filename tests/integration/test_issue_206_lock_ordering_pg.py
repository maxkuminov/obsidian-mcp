"""#206 / D7c3 — the deadlock the acquisition point exists to avoid.

Taking the generation lock where the tsvector is written would have inverted
the ordering rule and produced a real deadlock, not merely an inelegance. The
incremental index pass is **one transaction** and it mutates `notes_metadata`
long before it reaches the tsvector — the changed-note upsert, the id-preserving
move UPDATE, the prune DELETE, the `note_links` delete-and-insert and the
grammar-invalidation `UPDATE … SET embedded_content_hash = NULL`. So:

    pass:    upsert notes_metadata rows         (holds row locks)
    rebuild: pg_advisory_xact_lock              (holds advisory)
    rebuild: rebuild those rows                 -> waits on the pass's row locks
    pass:    pg_advisory_xact_lock at the write -> waits on the advisory

— a cycle the database resolves by killing one side.

The rule is therefore a property of the **transaction**, not of the statement
that happens to need the fingerprint: a transaction that will write any
configuration-dependent derived row acquires the lock and re-validates the
fingerprint before its first row-locking mutation. Two connections and a real
PostgreSQL, because "neither side was chosen as a deadlock victim" is a fact
about the server's lock graph and nothing else can produce it.

Skipped unless `PGVECTOR_TEST_ADMIN_URL` is set — see `_harness.py`.
"""
import asyncio
import hashlib

import pytest
import pytest_asyncio
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import src.database
from src.config import settings
from src.services import index_state, indexer
from src.services.index_state import INDEX_GENERATION_LOCK_KEY
import _harness

pytestmark = [
    _harness.requires_pgvector,
    pytest.mark.asyncio(loop_scope="module"),
]

DIM = 8
BODY = "prose with several running words for the keyword vector\n"

#: The statements that take a row or table lock in the pass's transaction. The
#: advisory lock must precede every one of them.
ROW_LOCKING = (
    "insert into notes_metadata",
    "update notes_metadata",
    "delete from notes_metadata",
    "insert into note_links",
    "update note_links",
    "delete from note_links",
)


def content_hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


@pytest.fixture(scope="module")
def migrated_url():
    yield from _harness.throwaway_database("lock_ordering_206", DIM)


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def engines(migrated_url):
    a = create_async_engine(migrated_url, poolclass=None)
    b = create_async_engine(migrated_url, poolclass=None)
    yield a, b
    await a.dispose()
    await b.dispose()


@pytest_asyncio.fixture(loop_scope="module")
async def world(engines, monkeypatch, tmp_path):
    engine_a, engine_b = engines
    maker_a = async_sessionmaker(engine_a, class_=AsyncSession, expire_on_commit=False)
    maker_b = async_sessionmaker(engine_b, class_=AsyncSession, expire_on_commit=False)

    root = tmp_path / "vault"
    root.mkdir(exist_ok=True)
    (root / "Note.md").write_text(BODY, encoding="utf-8")

    monkeypatch.setattr(settings, "vault_path", str(root), raising=False)
    monkeypatch.setattr(indexer.settings, "vault_path", str(root), raising=False)
    monkeypatch.setattr(indexer.settings, "multi_user_mode", False, raising=False)
    monkeypatch.setattr(indexer, "async_session", maker_a)
    monkeypatch.setattr(src.database, "async_session", maker_a)
    monkeypatch.setattr(indexer, "_is_paused", lambda: False)
    monkeypatch.setattr(
        indexer, "_refuse_quarantined_pass", lambda *_a, **_k: None
    )

    async with maker_a() as session:
        await session.execute(text("DELETE FROM note_embeddings"))
        await session.execute(text("DELETE FROM notes_metadata"))
        await session.execute(text("DELETE FROM indexer_state"))
        await session.execute(text(
            "INSERT INTO notes_metadata (user_id, file_path, title, "
            "content_hash, file_size, modified_at, indexed_at) VALUES "
            "(NULL, 'Note.md', 'Note', :h, 10, now(), now())"
        ), {"h": content_hash(BODY)})
        await session.commit()
    return {"root": root, "maker_a": maker_a, "maker_b": maker_b,
            "engine_a": engine_a}


# ══════════════════════════════════════════════════════════════════════════
# The ordering, read off the wire
# ══════════════════════════════════════════════════════════════════════════


async def test_the_pass_locks_before_its_first_row_locking_statement(
    world, monkeypatch
):
    """Read off the statements the pass actually issued, not off its source.

    Reasoning backwards from the tsvector write is exactly what produced the
    deadlock, so the assertion is over **every** mutation in the transaction:
    the upsert, the move update, the prune, the link rows and the
    certification invalidation alike.
    """
    issued: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        issued.append(" ".join(statement.split()).lower())

    event.listen(world["engine_a"].sync_engine, "before_cursor_execute", _record)
    try:
        # A new file and a deleted one, so the pass performs an insert *and* a
        # prune rather than only the cheapest mutation.
        (world["root"] / "Fresh.md").write_text("brand new\n", encoding="utf-8")
        async with world["maker_a"]() as session:
            await session.execute(text(
                "INSERT INTO notes_metadata (user_id, file_path, title, "
                "content_hash, file_size, modified_at, indexed_at) VALUES "
                "(NULL, 'Gone.md', 'Gone', 'deadbeef', 10, now(), now())"
            ))
            await session.commit()
        issued.clear()
        await indexer.index_vault(user_id=None)
    finally:
        event.remove(
            world["engine_a"].sync_engine, "before_cursor_execute", _record
        )
        (world["root"] / "Fresh.md").unlink()

    lock_at = next(
        i for i, sql in enumerate(issued) if "pg_advisory_xact_lock" in sql
    )
    mutations = [
        i for i, sql in enumerate(issued)
        if any(sql.startswith(prefix) for prefix in ROW_LOCKING)
    ]
    assert mutations, "the pass performed no row-locking mutation to order against"
    assert lock_at < min(mutations), (
        "the pass took a row lock before the generation lock — the ordering "
        "that deadlocks against a rebuild holding the advisory lock"
    )


# ══════════════════════════════════════════════════════════════════════════
# The interleaving itself
# ══════════════════════════════════════════════════════════════════════════


async def _rebuild_after(delay, maker):
    await asyncio.sleep(delay)
    async with maker() as session:
        return await indexer.rebuild_tsvectors_all_scopes(session)


async def test_a_pass_and_a_rebuild_do_not_deadlock(world):
    """Neither transaction may be aborted as a deadlock victim.

    That holds only because the pass acquired the generation lock at the head
    of its transaction: the rebuild then waits for the pass rather than the two
    waiting on each other.
    """
    maker_a, maker_b = world["maker_a"], world["maker_b"]

    async def _pass():
        async with maker_a() as session:
            # The head of the pass transaction, in the order the real pass
            # uses it.
            await index_state.acquire_generation_lock(session)
            await session.execute(text(
                "UPDATE notes_metadata SET title = 'touched' "
                "WHERE file_path = 'Note.md'"
            ))
            # Long enough that the rebuild is definitely waiting on the
            # advisory lock rather than racing past it.
            await asyncio.sleep(0.6)
            await session.commit()
        return "pass committed"

    pass_task = asyncio.create_task(_pass())
    rebuild_task = asyncio.create_task(_rebuild_after(0.1, maker_b))
    done = await asyncio.wait_for(
        asyncio.gather(pass_task, rebuild_task), timeout=30
    )

    assert done[0] == "pass committed"
    assert done[1][None].completed, "the rebuild was killed or skipped"
    async with maker_a() as session:
        assert (await session.execute(text(
            "SELECT title FROM notes_metadata WHERE file_path = 'Note.md'"
        ))).scalar() == "touched", "the pass was killed as a deadlock victim"


async def test_a_reset_waits_for_an_in_flight_pass(world):
    """L5b, asserted as behaviour rather than left as a comment.

    The pass holds the lock for its whole transaction, so a maintenance
    operation *waits* instead of interleaving with it. That is the required
    behaviour — a reset must not land mid-pass — and it is why those paths
    deliberately do not set a short `lock_timeout`.
    """
    maker_a, maker_b = world["maker_a"], world["maker_b"]
    order: list[str] = []

    async def _pass():
        async with maker_a() as session:
            await index_state.acquire_generation_lock(session)
            order.append("pass took the lock")
            await asyncio.sleep(0.5)
            order.append("pass committed")
            await session.commit()

    async def _reset():
        await asyncio.sleep(0.1)
        async with maker_b() as session:
            # Exactly what `reset_embeddings` does first: take the lock, then
            # wipe, then record the fingerprint.
            await index_state.acquire_generation_lock(session)
            order.append("reset took the lock")
            await session.execute(text("DELETE FROM note_embeddings"))
            await session.commit()

    await asyncio.wait_for(asyncio.gather(_pass(), _reset()), timeout=30)
    assert order == [
        "pass took the lock", "pass committed", "reset took the lock"
    ], "the reset interleaved with an in-flight pass instead of waiting"


async def test_the_lock_is_one_key_so_the_ordering_is_total(world):
    """One key, not two.

    The two subsystems guard one fact — which configuration the derived rows
    were built under — and a single key makes the ordering rule trivially
    total. Two keys would need an ordering *between* them, which is one more
    thing a later change could get wrong.
    """
    maker_a, maker_b = world["maker_a"], world["maker_b"]
    async with maker_a() as sa:
        await index_state.acquire_generation_lock(sa)
        async with maker_b() as sb:
            free = (await sb.execute(
                text("SELECT pg_try_advisory_xact_lock(:k)"),
                {"k": INDEX_GENERATION_LOCK_KEY},
            )).scalar()
            await sb.rollback()
        await sa.rollback()
    assert free is False, (
        "an embed certification and a keyword write did not serialise"
    )
