"""#206 / D7b — the keyword fingerprint is written only over proved coverage.

`_rebuild_tsvectors_pinned` is per owner and the script drove it once per
*active* user. Writing the global fingerprint inside it claimed something a
per-owner rebuild cannot establish — that **every retained row** in the
database was rebuilt under the current `FTS_CONFIGS` — and two ordinary shapes
falsified it: a second owner's rebuild raising after the first had already
written the fingerprint, and a scope holding rows the loop never visited at all
(an inactive or unassigned user; the ownerless scope in a database that also
holds named users). Either way the stored fingerprint certified rows still on
the previous configuration, and the startup guard that now fails closed on it
would pass while keyword search was exactly as wrong as before.

The properties that need a real database rather than a fake session are the
transactional ones: that one scope's failure rolls **another scope's committed
work** back, and that the fingerprint row and the rebuilt vectors land
together or not at all.

Skipped unless `PGVECTOR_TEST_ADMIN_URL` is set — see `_harness.py`.
"""
import hashlib

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import src.database
from src.config import settings
from src.services import indexer
from src.services import index_state
from src.services.index_state import KEY_FTS_FINGERPRINT
import _harness

pytestmark = [
    _harness.requires_pgvector,
    pytest.mark.asyncio(loop_scope="module"),
]

DIM = 8
BODY_A = "alpha running prose for the keyword vector\n"
BODY_B = "beta running prose for the keyword vector\n"


def content_hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


@pytest.fixture(scope="module")
def migrated_url():
    yield from _harness.throwaway_database("rebuild_coverage_206", DIM)


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def sessionmaker(migrated_url):
    engine = create_async_engine(migrated_url, poolclass=None)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest_asyncio.fixture(loop_scope="module")
async def world(sessionmaker, monkeypatch, tmp_path):
    """Two tenants with a vault each, plus the knobs the driver reads.

    `classify_for_pass` is stubbed so settledness is a dial rather than a
    provenance fixture: the gate under test is `_ancillary_pass_is_permitted`'s
    real one, and what varies per case is the verdict it is handed.
    """
    roots = {}
    for owner, body in ((1, BODY_A), (2, BODY_B)):
        root = tmp_path / f"vault{owner}"
        root.mkdir(exist_ok=True)
        (root / "Note.md").write_text(body, encoding="utf-8")
        roots[owner] = root

    monkeypatch.setattr(indexer, "async_session", sessionmaker)
    monkeypatch.setattr(src.database, "async_session", sessionmaker)
    monkeypatch.setattr(settings, "vault_path", str(roots[1]), raising=False)
    monkeypatch.setattr(indexer.settings, "vault_path", str(roots[1]), raising=False)
    monkeypatch.setattr(indexer.settings, "multi_user_mode", True, raising=False)
    monkeypatch.setattr(
        indexer, "_refuse_quarantined_pass", lambda *_a, **_k: None
    )

    verdicts = {1: indexer.PROVENANCE_KEEP, 2: indexer.PROVENANCE_KEEP}

    async def _classify(_session, user_id, _vault, _fd):
        return (
            indexer.Classification(verdicts[user_id], "stubbed for the test"),
            None,
            None,
        )

    monkeypatch.setattr(indexer, "classify_for_pass", _classify)

    async with sessionmaker() as session:
        await session.execute(text("DELETE FROM note_embeddings"))
        await session.execute(text("DELETE FROM notes_metadata"))
        await session.execute(text("DELETE FROM indexer_state"))
        await session.execute(text("DELETE FROM users"))
        # user 1 is active, user 2 is **inactive but assigned** — the case the
        # active-user set would have skipped and the coverage proof must not.
        for owner, active in ((1, True), (2, False)):
            await session.execute(text(
                "INSERT INTO users (id, username, password_hash, is_active, "
                "is_admin, vault_path, created_at) VALUES "
                "(:id, :u, 'x', :active, false, :path, now())"
            ), {"id": owner, "u": f"u{owner}", "active": active,
                "path": str(roots[owner])})
        await session.commit()
    return {"roots": roots, "verdicts": verdicts}


async def _seed_rows(sessionmaker, owners):
    async with sessionmaker() as session:
        await session.execute(text("DELETE FROM notes_metadata"))
        for owner in owners:
            body = BODY_A if owner in (1, None) else BODY_B
            await session.execute(text(
                "INSERT INTO notes_metadata "
                "(user_id, file_path, title, content_hash, file_size, "
                " modified_at, indexed_at) "
                "VALUES (:uid, 'Note.md', 'Note', :h, 10, now(), now())"
            ), {"uid": owner, "h": content_hash(body)})
        await session.commit()


async def _stored_fingerprint(sessionmaker):
    async with sessionmaker() as session:
        return await index_state.get_state(session, KEY_FTS_FINGERPRINT)


async def _tsvectors(sessionmaker):
    async with sessionmaker() as session:
        rows = (await session.execute(text(
            "SELECT user_id, content_tsvector IS NOT NULL AS built "
            "FROM notes_metadata ORDER BY user_id"
        ))).all()
    return {row.user_id: row.built for row in rows}


# ══════════════════════════════════════════════════════════════════════════
# Coverage proved
# ══════════════════════════════════════════════════════════════════════════


async def test_a_complete_rebuild_commits_vectors_and_fingerprint_together(
    sessionmaker, world
):
    """Every scope holding rows completed, so the claim can be made.

    And the inactive-but-assigned owner is **rebuilt, not skipped**: the
    coverage proof is about rows that exist, and an inactive user's rows are as
    retained — and as returnable by `keyword_search` — as anyone's.
    """
    await _seed_rows(sessionmaker, [1, 2])

    async with sessionmaker() as session:
        outcomes = await indexer.rebuild_tsvectors_all_scopes(session)

    assert set(outcomes) == {1, 2}
    assert all(o.completed for o in outcomes.values())
    assert outcomes[2].rows == 1, "the inactive owner's scope was skipped"
    assert await _tsvectors(sessionmaker) == {1: True, 2: True}
    assert await _stored_fingerprint(sessionmaker) == index_state.fts_fingerprint()


async def test_the_active_user_set_is_unchanged(sessionmaker, world):
    """The driver resolves an inactive owner's root inside itself.

    It reads `users.vault_path` directly and read-only; it does not widen
    `_active_user_ids`, and the set of users the periodic pass serves is the
    same before and after.
    """
    before = await indexer._active_user_ids()
    await _seed_rows(sessionmaker, [1, 2])
    async with sessionmaker() as session:
        await indexer.rebuild_tsvectors_all_scopes(session)
    assert await indexer._active_user_ids() == before == [1]


# ══════════════════════════════════════════════════════════════════════════
# Coverage refused
# ══════════════════════════════════════════════════════════════════════════


async def test_an_unsettled_scope_aborts_and_writes_no_fingerprint(
    sessionmaker, world, monkeypatch
):
    """The `0`-rows-looks-like-success case, asserted specifically.

    A skipped scope's per-owner rebuild returns zero rows, and a driver reading
    a row count would have recorded a fingerprint certifying a scope the
    rebuild deliberately declined to touch.
    """
    await _seed_rows(sessionmaker, [1, 2])
    world["verdicts"][2] = indexer.PROVENANCE_REDERIVE

    async with sessionmaker() as session:
        with pytest.raises(indexer.RebuildCoverageAborted) as excinfo:
            await indexer.rebuild_tsvectors_all_scopes(session)

    message = str(excinfo.value)
    assert "user_id=2" in message
    assert "provenance unsettled" in message
    assert await _stored_fingerprint(sessionmaker) is None
    # And scope 1's rebuilt vectors rolled back with it.
    assert await _tsvectors(sessionmaker) == {1: False, 2: False}
    world["verdicts"][2] = indexer.PROVENANCE_KEEP


async def test_an_unassigned_owner_is_root_unpinnable_and_blocks_the_record(
    sessionmaker, world
):
    await _seed_rows(sessionmaker, [1, 2])
    async with sessionmaker() as session:
        await session.execute(
            text("UPDATE users SET vault_path = NULL WHERE id = 2")
        )
        await session.commit()
    try:
        async with sessionmaker() as session:
            with pytest.raises(indexer.RebuildCoverageAborted) as excinfo:
                await indexer.rebuild_tsvectors_all_scopes(session)
        assert "root unpinnable" in str(excinfo.value)
        assert "no assigned vault_path" in str(excinfo.value)
        assert await _stored_fingerprint(sessionmaker) is None
        assert await _tsvectors(sessionmaker) == {1: False, 2: False}
    finally:
        async with sessionmaker() as session:
            await session.execute(text(
                "UPDATE users SET vault_path = :p WHERE id = 2"
            ), {"p": str(world["roots"][2])})
            await session.commit()


async def test_a_second_scope_raising_rolls_the_first_scope_back(
    sessionmaker, world, monkeypatch
):
    """The shape that made a per-owner fingerprint write unsound.

    User B's rebuild raises after user A's rows are already written. With the
    fingerprint inside the per-owner rebuild, A's write would have stood and
    certified B's untouched rows.
    """
    await _seed_rows(sessionmaker, [1, 2])
    real = indexer._rebuild_tsvectors_pinned

    async def _explode(session, user_id, vault, root_fd, log_suffix):
        if user_id == 2:
            raise RuntimeError("the vault went away mid-rebuild")
        return await real(session, user_id, vault, root_fd, log_suffix)

    monkeypatch.setattr(indexer, "_rebuild_tsvectors_pinned", _explode)

    async with sessionmaker() as session:
        with pytest.raises(RuntimeError, match="went away"):
            await indexer.rebuild_tsvectors_all_scopes(session)

    assert await _stored_fingerprint(sessionmaker) is None
    assert await _tsvectors(sessionmaker) == {1: False, 2: False}


# ══════════════════════════════════════════════════════════════════════════
# The ownerless scope
# ══════════════════════════════════════════════════════════════════════════


async def test_ownerless_rows_abort_the_rebuild_under_multi_user_mode(
    sessionmaker, world
):
    """L6, by decision.

    `_vault_root(None)` refuses in this mode by design, and substituting
    `settings.vault_path` would read one tenant's notes under an unowned scope
    — a tenancy violation performed to satisfy a bookkeeping row. Nor may they
    be quietly excluded: they are retained rows `keyword_search` can return.
    """
    await _seed_rows(sessionmaker, [None, 1])

    async with sessionmaker() as session:
        with pytest.raises(indexer.RebuildCoverageAborted) as excinfo:
            await indexer.rebuild_tsvectors_all_scopes(session)

    message = str(excinfo.value)
    assert "user_id IS NULL" in message
    assert "1 notes_metadata row(s)" in message
    assert "Delete or reassign" in message
    assert await _stored_fingerprint(sessionmaker) is None
    assert await _tsvectors(sessionmaker) == {None: False, 1: False}


async def test_the_ownerless_scope_is_normal_in_single_user_mode(
    sessionmaker, world, monkeypatch
):
    """With multi-user off the ownerless scope is the *only* scope."""
    monkeypatch.setattr(indexer.settings, "multi_user_mode", False, raising=False)
    await _seed_rows(sessionmaker, [None])

    async with sessionmaker() as session:
        outcomes = await indexer.rebuild_tsvectors_all_scopes(session)

    assert list(outcomes) == [None]
    assert outcomes[None].completed and outcomes[None].rows == 1
    assert await _tsvectors(sessionmaker) == {None: True}
    assert await _stored_fingerprint(sessionmaker) == index_state.fts_fingerprint()


# ══════════════════════════════════════════════════════════════════════════
# The typed outcome
# ══════════════════════════════════════════════════════════════════════════


async def test_a_skip_and_an_empty_scope_are_not_the_same_value(
    sessionmaker, world
):
    """Both used to be `0`, which is why the driver could not tell them apart."""
    await _seed_rows(sessionmaker, [])
    async with sessionmaker() as session:
        await session.execute(text(
            "INSERT INTO notes_metadata (user_id, file_path, title, "
            "content_hash, file_size, modified_at, indexed_at) VALUES "
            "(1, 'Note.md', 'Note', :h, 10, now(), now())"
        ), {"h": content_hash(BODY_A)})
        await session.commit()

    async with sessionmaker() as session:
        with indexer.pinned_root(world["roots"][2]) as fd:
            empty = await indexer._rebuild_tsvectors_pinned(
                session, 2, world["roots"][2], fd, " (user_id=2)"
            )
        assert empty.completed and empty.rows == 0
        world["verdicts"][2] = indexer.PROVENANCE_DISCARD
        with indexer.pinned_root(world["roots"][2]) as fd:
            skipped = await indexer._rebuild_tsvectors_pinned(
                session, 2, world["roots"][2], fd, " (user_id=2)"
            )
        assert not skipped.completed
        assert skipped.skip is indexer.RebuildSkip.PROVENANCE_UNSETTLED
        assert skipped.rows == 0
    world["verdicts"][2] = indexer.PROVENANCE_KEEP
