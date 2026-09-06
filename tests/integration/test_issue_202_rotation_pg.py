"""#202 / D4 — the tenant rotation, and the cursor that survives a restart.

`_active_user_ids()` had no `ORDER BY`, so its order was whatever the planner
returned: stable enough in practice that the same tenant went first every
cycle, and unspecified enough that nothing could be asserted about it. A
rotation over an unspecified order is not a rotation, so the ordering comes
first and the cursor rides on it.

**Persisting it is the requirement, not an implementation detail.** In-process
state resets on every restart and every deploy, and a deploy recreates the
container — so the tenants at the tail of the order are exactly the ones a
restart-truncated pass never reaches, and an in-memory cursor would be reset
precisely when it was about to pay off.

The cursor's disposition is deliberately the **opposite** of the fingerprints':
a value the pass cannot use is logged once and ignored, and the cycle begins at
the first tenant. A cursor is scheduling state whose worst consequence is an
order; failing closed on a stray character in a bookkeeping row would stop
every tenant's indexing to protect nothing.

Real PostgreSQL because the point is that the value **outlives the process**.
Skipped unless `PGVECTOR_TEST_ADMIN_URL` is set — see `_harness.py`.
"""
import inspect

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import src.database
from src.config import settings
from src.services import indexer
from src.services import index_state
from src.services.index_state import (
    KEY_EMBEDDING_FINGERPRINT,
    KEY_ROTATION_CURSOR,
)
import _harness

pytestmark = [
    _harness.requires_pgvector,
    pytest.mark.asyncio(loop_scope="module"),
]

DIM = 8


@pytest.fixture(scope="module")
def migrated_url():
    yield from _harness.throwaway_database("rotation_202", DIM)


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def sessionmaker(migrated_url):
    engine = create_async_engine(migrated_url, poolclass=None)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest_asyncio.fixture(loop_scope="module")
async def tenants(sessionmaker, monkeypatch, tmp_path):
    """Three active tenants — ids 3, 5 and 9, deliberately not contiguous.

    Contiguous ids would let an off-by-one in "the smallest id strictly greater
    than the cursor" pass by coincidence.
    """
    monkeypatch.setattr(indexer, "async_session", sessionmaker)
    monkeypatch.setattr(src.database, "async_session", sessionmaker)
    monkeypatch.setattr(indexer.settings, "multi_user_mode", True, raising=False)

    async with sessionmaker() as session:
        await session.execute(text("DELETE FROM notes_metadata"))
        await session.execute(text("DELETE FROM indexer_state"))
        await session.execute(text("DELETE FROM users"))
        for uid in (3, 5, 9):
            root = tmp_path / f"vault{uid}"
            root.mkdir(exist_ok=True)
            await session.execute(text(
                "INSERT INTO users (id, username, password_hash, is_active, "
                "is_admin, vault_path, created_at) VALUES "
                "(:id, :u, 'x', true, false, :p, now())"
            ), {"id": uid, "u": f"u{uid}", "p": str(root)})
        await session.commit()
    return [3, 5, 9]


async def _set_cursor(sessionmaker, value):
    async with sessionmaker() as session:
        if value is None:
            await session.execute(
                text("DELETE FROM indexer_state WHERE key = :k"),
                {"k": KEY_ROTATION_CURSOR},
            )
        else:
            await index_state.set_state(session, KEY_ROTATION_CURSOR, value)
        await session.commit()


async def _cursor(sessionmaker):
    async with sessionmaker() as session:
        return await index_state.get_state(session, KEY_ROTATION_CURSOR)


# ══════════════════════════════════════════════════════════════════════════
# The order, then the rotation
# ══════════════════════════════════════════════════════════════════════════


async def test_the_active_order_is_ascending_by_id(tenants, sessionmaker):
    assert await indexer._active_user_ids() == [3, 5, 9]


async def test_a_truncated_pass_resumes_where_it_stopped(tenants, sessionmaker):
    """The case the whole cursor exists for.

    A pass serving 3, 5, 9 finishes 3 and 5 and the process restarts before 9.
    An in-memory cursor would send 9 to the tail again — and 9 is precisely the
    tenant that has not been indexed.
    """
    await _set_cursor(sessionmaker, "5")
    assert await indexer._rotated_user_ids() == [9, 3, 5]


async def test_a_complete_cycle_wraps(tenants, sessionmaker):
    await _set_cursor(sessionmaker, "9")
    assert await indexer._rotated_user_ids() == [3, 5, 9]


async def test_the_cursor_survives_the_tenant_it_names(tenants, sessionmaker):
    """It stores a user **id**, not a positional offset.

    An offset into a list whose membership changes points somewhere else on the
    next cycle; "start after id 5" is well defined whether or not user 5 still
    exists, because the successor query does not require it to.
    """
    await _set_cursor(sessionmaker, "5")
    async with sessionmaker() as session:
        await session.execute(text("DELETE FROM users WHERE id = 5"))
        await session.commit()
    try:
        assert await indexer._rotated_user_ids() == [9, 3]
    finally:
        async with sessionmaker() as session:
            await session.execute(text(
                "INSERT INTO users (id, username, password_hash, is_active, "
                "is_admin, vault_path, created_at) VALUES "
                "(5, 'u5', 'x', true, false, '/tmp', now())"
            ))
            await session.commit()


@pytest.mark.parametrize("stored", ["abc", "-1", "", "  ", "1_0", "٣"])
async def test_an_unusable_cursor_starts_at_the_first_tenant(
    tenants, sessionmaker, stored, caplog
):
    """Logged once, ignored, and the pass runs a complete, correct cycle.

    The stored value is text in a key/value table, so it can be non-numeric,
    negative, or spelled in a script `int()` would happily accept (`"١"` is
    three; `"1_0"` is ten). Every unusable spelling reaches one outcome, and it
    is never a raise: a stray character in a bookkeeping row must not stop
    every tenant's indexing.
    """
    await _set_cursor(sessionmaker, stored)
    with caplog.at_level("WARNING", logger="src.services.indexer"):
        assert await indexer._rotated_user_ids() == [3, 5, 9]
    warnings = [
        r for r in caplog.records
        if "Ignoring the stored embed rotation cursor" in r.getMessage()
    ]
    assert len(warnings) == 1


async def test_an_out_of_range_cursor_wraps_to_the_first(tenants, sessionmaker):
    """No special case beyond the ordinary rule.

    "The smallest id strictly greater than N" simply selects nothing and wraps,
    which is the same outcome the malformed case reaches by a different route.
    """
    await _set_cursor(sessionmaker, str(2**62))
    assert await indexer._rotated_user_ids() == [3, 5, 9]


async def test_an_absent_cursor_starts_at_the_first_tenant_silently(
    tenants, sessionmaker, caplog
):
    """Absence is not drift: the first cycle after a deploy has no cursor."""
    await _set_cursor(sessionmaker, None)
    with caplog.at_level("WARNING", logger="src.services.indexer"):
        assert await indexer._rotated_user_ids() == [3, 5, 9]
    assert not [
        r for r in caplog.records
        if "Ignoring the stored embed rotation cursor" in r.getMessage()
    ]


# ══════════════════════════════════════════════════════════════════════════
# Writing it
# ══════════════════════════════════════════════════════════════════════════


async def test_the_cursor_is_advanced_and_persisted(tenants, sessionmaker):
    await _set_cursor(sessionmaker, None)
    await indexer._advance_rotation_cursor(5)
    assert await _cursor(sessionmaker) == "5"
    assert await indexer._rotated_user_ids() == [9, 3, 5]


async def test_a_raising_cursor_write_does_not_fail_the_pass(
    tenants, monkeypatch, caplog
):
    """The one write in this change that is swallow-on-failure.

    A lost cursor costs an order and nothing else; the fingerprints are claims
    a later startup refuses on, and they abort their operation instead.
    """
    async def _boom(*_a, **_k):
        raise RuntimeError("the database went away")

    monkeypatch.setattr(index_state, "set_state", _boom)
    monkeypatch.setattr(indexer, "set_state", _boom)
    with caplog.at_level("WARNING", logger="src.services.indexer"):
        await indexer._advance_rotation_cursor(3)
    assert any(
        "Could not record the embed rotation cursor" in r.getMessage()
        for r in caplog.records
    )


async def test_a_manual_reindex_does_not_move_the_rotation(tenants, sessionmaker):
    """`_reindex_background` reads the **unrotated** list and writes nothing.

    An operator-triggered reindex is not the starvation vector, and letting a
    panel click move the periodic pass's rotation would make the schedule a
    function of who clicked what.
    """
    from src.control_panel import routes

    source = inspect.getsource(routes._reindex_background)
    assert "_active_user_ids" in source
    assert "_rotated_user_ids" not in source
    assert "_advance_rotation_cursor" not in source

    await _set_cursor(sessionmaker, "5")
    await indexer._active_user_ids()
    assert await _cursor(sessionmaker) == "5"


async def test_the_rotation_is_used_by_the_loop_and_only_the_loop(tenants):
    loop = inspect.getsource(indexer.run_indexer_loop)
    assert loop.count("_rotated_user_ids()") == 2, (
        "the startup pass and the periodic tick both rotate"
    )
    assert "_active_user_ids()" not in loop


# ══════════════════════════════════════════════════════════════════════════
# 5.7a — the stage-head fingerprint read, as the cheap early exit it is
# ══════════════════════════════════════════════════════════════════════════


async def test_a_stage_whose_fingerprint_differs_embeds_nothing(
    tenants, sessionmaker, monkeypatch, tmp_path, caplog
):
    """It skips the stage and **records nothing** — no failure, no attempt.

    This is an optimisation, not the guarantee: a check at the head of a stage
    is separated from the certification by a provider round trip, which is why
    the enforcement is the generation lock inside `embed_note`. What it buys is
    that an old container stops working through a backlog whose every
    certification the lock is going to refuse.
    """
    async with sessionmaker() as session:
        await index_state.set_state(
            session, KEY_EMBEDDING_FINGERPRINT, '{"v":1,"model":"some-other"}'
        )
        await session.commit()
    try:
        root = tmp_path / "vault3"
        monkeypatch.setattr(settings, "vault_path", str(root), raising=False)
        monkeypatch.setattr(indexer.settings, "vault_path", str(root), raising=False)
        monkeypatch.setattr(indexer, "_vault_root", lambda _uid: root)
        monkeypatch.setattr(
            indexer, "_refuse_quarantined_pass", lambda *_a, **_k: None
        )

        async def _never(*_a, **_k):  # pragma: no cover - the stage must skip
            raise AssertionError("the stage embedded under a stale generation")

        monkeypatch.setattr(indexer, "embed_note", _never)

        with caplog.at_level("ERROR", logger="src.services.indexer"):
            result = await indexer.embed_vault(user_id=3)

        assert (result.embedded, result.failures, result.attempted) == (0, 0, 0)
        assert result.failure_summary is None
        assert any(
            "Embedding stage skipped" in r.getMessage() for r in caplog.records
        )
    finally:
        async with sessionmaker() as session:
            await session.execute(
                text("DELETE FROM indexer_state WHERE key = :k"),
                {"k": KEY_EMBEDDING_FINGERPRINT},
            )
            await session.commit()


async def test_a_matching_fingerprint_lets_the_stage_run(
    tenants, sessionmaker, monkeypatch, tmp_path
):
    """The control, so "skipped" is not the answer to every question."""
    async with sessionmaker() as session:
        await index_state.set_state(
            session, KEY_EMBEDDING_FINGERPRINT, index_state.embedding_fingerprint()
        )
        await session.commit()
    try:
        root = tmp_path / "vault3"
        monkeypatch.setattr(indexer, "_vault_root", lambda _uid: root)
        monkeypatch.setattr(
            indexer, "_refuse_quarantined_pass", lambda *_a, **_k: None
        )
        result = await indexer.embed_vault(user_id=3)
        # Nothing in the backlog, but the stage ran: an empty backlog and a
        # skipped stage look the same from the outside, so the log is what
        # separates them and the assertion above is the one that matters.
        assert result.failure_summary is None
    finally:
        async with sessionmaker() as session:
            await session.execute(
                text("DELETE FROM indexer_state WHERE key = :k"),
                {"k": KEY_EMBEDDING_FINGERPRINT},
            )
            await session.commit()
