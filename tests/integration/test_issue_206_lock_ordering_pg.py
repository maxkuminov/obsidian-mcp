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


# ══════════════════════════════════════════════════════════════════════════
# The wait is not capped by `statement_timeout`
# ══════════════════════════════════════════════════════════════════════════
#
# `lock_timeout` was never the only way to defeat the wait. `src/database.py`
# sets `statement_timeout` to 60 s in the engine's `server_settings`, and
# `pg_advisory_xact_lock` is a statement like any other — so a wait longer than
# a minute was *cancelled*, and since the pass holds the lock for its whole
# transaction (L5b, minutes on a large vault) the maintenance commands did not
# wait for a pass at all. The specified behaviour ("a maintenance operation
# waits for an in-flight pass") was contradicted by the engine, silently, at
# every site the docs asserted it.
#
# The timeout is lowered to 1 s here rather than the pass being made to run for
# a minute: the cap is a parameter and every branch is on the same side of it
# either way, so waiting out the real 60 s would prove the same thing and cost
# a minute per case.

#: One second, so a hold of `HOLD_SECONDS` is unambiguously past it.
IMPATIENT = {"server_settings": {"statement_timeout": "1000"}}

HOLD_SECONDS = 2.5


@pytest_asyncio.fixture(loop_scope="module")
async def impatient(migrated_url):
    """An engine whose connections cancel any statement after one second."""
    engine = create_async_engine(
        migrated_url, poolclass=None, connect_args=IMPATIENT
    )
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield engine, maker
    await engine.dispose()


async def _hold_the_lock(maker, seconds: float, started: asyncio.Event):
    """Stand in for an in-flight pass: take the lock, hold it, commit."""
    async with maker() as session:
        await index_state.acquire_generation_lock(session)
        started.set()
        await asyncio.sleep(seconds)
        await session.commit()


async def test_the_plain_acquisition_is_capped_and_the_unbounded_one_is_not(
    world, impatient
):
    """The regression and the fix, on one connection.

    Two assertions in one test on purpose. The first proves the fixture
    actually bites — without it the second could pass on a build that never
    raised the timeout at all, which is exactly the state this test exists to
    detect. The second proves the raise covers the wait and nothing after it.
    """
    _engine, maker = impatient
    started = asyncio.Event()
    holder = asyncio.create_task(
        _hold_the_lock(world["maker_a"], HOLD_SECONDS, started)
    )
    try:
        await started.wait()

        with pytest.raises(Exception) as capped:
            async with maker() as session:
                await index_state.acquire_generation_lock(session)
        assert "statement timeout" in str(capped.value), (
            "the impatient engine did not cancel the wait, so this test could "
            f"not tell a raised timeout from an absent one: {capped.value}"
        )

        async with maker() as session:
            await index_state.acquire_generation_lock_unbounded(session)
            # The exemption covers the wait and nothing else: the connection's
            # own bound is back in force for everything after it.
            restored = (
                await session.execute(text("SHOW statement_timeout"))
            ).scalar_one()
            await session.rollback()
        assert restored == "1s", (
            "the timeout was left lifted for the rest of the transaction: "
            f"{restored!r}"
        )
    finally:
        await asyncio.wait_for(holder, timeout=30)


async def test_the_rebuild_waits_past_the_statement_timeout(world, impatient):
    """`make rebuild-tsvectors` against a live service, in miniature.

    The driver is the production entry point `scripts/rebuild_tsvectors.py`
    calls, so this asserts the raise where an operator meets it rather than on
    a hand-rolled acquisition.
    """
    _engine, maker = impatient
    started = asyncio.Event()
    holder = asyncio.create_task(
        _hold_the_lock(world["maker_a"], HOLD_SECONDS, started)
    )
    await started.wait()
    async with maker() as session:
        outcomes = await asyncio.wait_for(
            indexer.rebuild_tsvectors_all_scopes(session), timeout=30
        )
        await session.commit()
    await asyncio.wait_for(holder, timeout=30)
    assert outcomes[None].completed, (
        "the rebuild was cancelled instead of waiting for the in-flight pass"
    )


async def test_the_pass_waits_past_the_statement_timeout(
    world, impatient, monkeypatch
):
    """The symmetric case: the pass is the *waiting* side of this lock too.

    Its failure direction was safe — a cancelled acquisition aborts the pass,
    which commits nothing and retries next tick — but "waits" is the documented
    contract on both sides, and a pass that abandons every tick for the
    duration of a long rebuild writes an `indexer_runs` error row per tick
    about a database that is merely busy.
    """
    _engine, maker = impatient
    monkeypatch.setattr(indexer, "async_session", maker)
    monkeypatch.setattr(src.database, "async_session", maker)

    started = asyncio.Event()
    holder = asyncio.create_task(
        _hold_the_lock(world["maker_a"], HOLD_SECONDS, started)
    )
    await started.wait()
    await asyncio.wait_for(indexer.index_vault(user_id=None), timeout=30)
    await asyncio.wait_for(holder, timeout=30)

    async with world["maker_a"]() as session:
        assert (await session.execute(text(
            "SELECT count(*) FROM notes_metadata WHERE file_path = 'Note.md'"
        ))).scalar() == 1, "the pass was cancelled and committed nothing"


async def test_the_reset_script_waits_past_the_statement_timeout(
    world, impatient, monkeypatch
):
    """`scripts/reset_embeddings.py` itself, not a re-implementation of it.

    The script's ordering comment used to argue that the raise had to come
    *after* the acquisition, because a `SET LOCAL` ahead of it "would put a
    statement before the lock". That reading of the ordering rule was wrong —
    the rule is about row and table locks and a `SET LOCAL` takes neither — and
    it left the documented wait capped at 60 s. Running the real `reset()`
    against a held lock is the only thing that proves the correction landed in
    the script an operator actually runs.

    Kept last in the module: it wipes `note_embeddings` and re-types the
    column.
    """
    import importlib.util

    engine, maker = impatient
    started = asyncio.Event()
    holder = asyncio.create_task(
        _hold_the_lock(world["maker_a"], HOLD_SECONDS, started)
    )

    spec = importlib.util.spec_from_file_location(
        "_reset_embeddings_script",
        _harness.ROOT / "scripts" / "reset_embeddings.py",
    )
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)

    monkeypatch.setattr(script, "async_session", maker)
    monkeypatch.setattr(script, "engine", engine)
    monkeypatch.setattr(settings, "embedding_dimensions", DIM, raising=False)

    await started.wait()
    await asyncio.wait_for(script.reset(), timeout=30)
    await asyncio.wait_for(holder, timeout=30)

    async with world["maker_a"]() as session:
        assert (await session.execute(text(
            "SELECT value FROM indexer_state "
            "WHERE key = 'embedding_fingerprint'"
        ))).scalar() is not None, (
            "the reset was cancelled instead of waiting for the in-flight pass"
        )
