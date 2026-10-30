"""#309 — a pass does not prune what it could not see, against PostgreSQL.

What only a real database shows: that rows (and their cascading embeddings)
beneath an unlisted directory survive the pass and are not move-paired; that
an empty root over an existing index aborts with nothing deleted and is
recorded as a failed run; that the operator's empty-prune permission
authorises exactly one prune, is consumed by the next pass either way, and
does not survive its expiry; and that the locked re-check sees a row the
snapshot did not.

Skipped unless `PGVECTOR_TEST_ADMIN_URL` is set (see `_harness`).
"""
import contextlib
import os

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import src.database
from src.config import settings
from src.models.db import IndexerRun, NoteEmbedding, NoteMetadata, User
from src.services import embeddings as embeddings_service
from src.services import empty_prune, indexer, indexer_health
from src.services import vault as vault_service
from src.services.transfer import canonical_vault_root
import _harness

pytestmark = [
    _harness.requires_pgvector,
    pytest.mark.asyncio(loop_scope="module"),
]

DIM = 8
VECTOR = [1.0] + [0.0] * (DIM - 1)
running_as_root = hasattr(os, "geteuid") and os.geteuid() == 0


@pytest.fixture(scope="module")
def migrated_url():
    yield from _harness.throwaway_database("prune_guards_309", DIM)


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def sessionmaker(migrated_url):
    engine = create_async_engine(migrated_url, poolclass=None)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest_asyncio.fixture(loop_scope="module")
async def vault(sessionmaker, monkeypatch, tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    monkeypatch.setattr(settings, "vault_path", str(root), raising=False)
    monkeypatch.setattr(indexer.settings, "vault_path", str(root), raising=False)
    monkeypatch.setattr(indexer.settings, "multi_user_mode", False, raising=False)
    monkeypatch.setattr(
        indexer.settings, "embedding_exclude_patterns", [], raising=False
    )
    monkeypatch.setattr(indexer, "async_session", sessionmaker)
    monkeypatch.setattr(src.database, "async_session", sessionmaker)
    monkeypatch.setattr(embeddings_service, "async_session", sessionmaker, raising=False)
    monkeypatch.setattr(indexer, "_is_paused", lambda: False)

    async def fake_batch(chunks, **_kw):
        return [list(VECTOR) for _ in chunks]

    async def fake_one(_text, **_kw):
        return list(VECTOR)

    monkeypatch.setattr(embeddings_service, "get_embeddings_batch", fake_batch)
    monkeypatch.setattr(embeddings_service, "get_embedding", fake_one)
    indexer._last_full_hash.clear()
    vault_service.clear_user_vault_cache()

    async with sessionmaker() as session:
        for table in ("note_links", "note_embeddings", "notes_metadata",
                      "indexer_runs", "users"):
            await session.execute(text(f"DELETE FROM {table}"))
        await session.commit()

    yield root

    indexer._last_full_hash.clear()


async def rows(sessionmaker, user_id=None) -> dict[str, NoteMetadata]:
    async with sessionmaker() as session:
        owner = (
            NoteMetadata.user_id.is_(None)
            if user_id is None
            else NoteMetadata.user_id == user_id
        )
        found = (await session.execute(select(NoteMetadata).where(owner))).scalars()
        return {r.file_path: r for r in found}


async def embedding_count(sessionmaker, note_ids) -> int:
    async with sessionmaker() as session:
        return (
            await session.execute(
                select(func.count()).select_from(NoteEmbedding).where(
                    NoteEmbedding.note_id.in_(list(note_ids))
                )
            )
        ).scalar_one()


async def last_run(sessionmaker) -> IndexerRun:
    async with sessionmaker() as session:
        return (
            await session.execute(
                select(IndexerRun).order_by(IndexerRun.id.desc()).limit(1)
            )
        ).scalar_one()


def write(root, rel: str, body: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


@contextlib.contextmanager
def unreadable(path):
    os.chmod(path, 0)
    try:
        yield
    finally:
        os.chmod(path, 0o755)


async def tenant(sessionmaker, root, name: str) -> int:
    root.mkdir(exist_ok=True)
    async with sessionmaker() as session:
        user = User(username=name, password_hash="x", vault_path=str(root))
        session.add(user)
        await session.commit()
        await vault_service.warm_user_vault_cache(session, user_id=user.id)
        return user.id


# ══════════════════════════════════════════════════════════════════════════
# D2 — rows beneath an unlisted directory
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.skipif(running_as_root, reason="root ignores directory modes")
async def test_rows_under_an_unreadable_folder_survive_and_recovery_rewrites_nothing(
    sessionmaker, vault
):
    for i in range(3):
        write(vault, f"sub/n{i}.md", f"protected body {i}\n")
    write(vault, "sub/deep/d.md", "deep body\n")
    write(vault, "top.md", "top body\n")
    write(vault, "subway.md", "subway body\n")
    await indexer.index_vault()
    await indexer.embed_vault()
    before = await rows(sessionmaker)
    protected = {p: r for p, r in before.items() if p.startswith("sub/")}
    assert len(protected) == 4
    embedded_before = await embedding_count(
        sessionmaker, [r.id for r in protected.values()]
    )
    assert embedded_before >= 4

    # The pass under test: `sub` unreadable, `subway.md` deleted, `top.md`
    # edited, and a new file elsewhere with the same bytes as `sub/n0.md`.
    (vault / "subway.md").unlink()
    write(vault, "top.md", "top body, edited\n")
    write(vault, "elsewhere/copy.md", "protected body 0\n")
    with unreadable(vault / "sub"):
        assert await indexer._index_pass_once(None) is True
        after = await rows(sessionmaker)

    for path, row in protected.items():
        assert path in after, f"{path} was pruned"
        assert after[path].id == row.id and after[path].content_hash == row.content_hash
    assert await embedding_count(
        sessionmaker, [r.id for r in protected.values()]
    ) == embedded_before, "the protected notes keep their embeddings"
    assert "subway.md" not in after, "a shared-name sibling is still pruned"
    assert after["top.md"].content_hash != before["top.md"].content_hash
    assert after["elsewhere/copy.md"].id not in {r.id for r in before.values()}, (
        "the same-hash file is inserted as new, not a move of the protected row"
    )

    run = await last_run(sessionmaker)
    assert "walk incomplete: 1 dir(s) not listed: sub" in run.error
    assert indexer_health.run_outcome(run.error) == indexer_health.RUN_FAILED
    assert indexer_health._scopes[None].walk_incomplete == 1

    # Readable again, unchanged: nothing beneath it is re-inserted or
    # re-embedded.
    result = await indexer.index_vault()
    assert result.walk_failed == () and result.notes_indexed == 0
    recovered = await rows(sessionmaker)
    for path, row in protected.items():
        assert recovered[path].id == row.id
        assert recovered[path].indexed_at == after[path].indexed_at
        assert recovered[path].embedded_content_hash == row.content_hash
    embedded = await indexer.embed_vault()
    assert int(embedded) == 0
    indexer.record_index_outcome(None, True, result)
    assert indexer_health._scopes[None].walk_incomplete == 0


async def test_an_entry_whose_type_lookup_fails_protects_its_subtree(
    sessionmaker, vault, monkeypatch
):
    write(vault, "sub/a.md", "a\n")
    write(vault, "sub/x/b.md", "b\n")
    write(vault, "keep.md", "keep\n")
    await indexer.index_vault()

    real = os.scandir

    class Entry:
        def __init__(self, entry):
            self._e, self.name = entry, entry.name

        def is_dir(self, *, follow_symlinks=True):
            if self.name == "sub":
                raise OSError(5, "Input/output error")
            return self._e.is_dir(follow_symlinks=follow_symlinks)

    @contextlib.contextmanager
    def fake(target):
        with real(target) as entries:
            yield [Entry(e) for e in entries]

    monkeypatch.setattr(os, "scandir", fake)
    result = await indexer.index_vault()
    monkeypatch.setattr(os, "scandir", real)

    assert result.walk_failed == ("sub",) and result.walk_protected == 2
    assert set(await rows(sessionmaker)) == {"sub/a.md", "sub/x/b.md", "keep.md"}


# ══════════════════════════════════════════════════════════════════════════
# D3 / D5 — the indeterminate root and the empty-prune permission
# ══════════════════════════════════════════════════════════════════════════


def empty(root) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        path.rmdir() if path.is_dir() else path.unlink()


async def test_an_empty_root_deletes_nothing_until_confirmed_once(
    sessionmaker, vault, monkeypatch
):
    monkeypatch.setattr(
        indexer.settings, "indexer_degraded_after_failures", 3, raising=False
    )
    for i in range(50):
        write(vault, f"n{i:02}.md", f"note {i}\n")
    await indexer.index_vault()
    await indexer.embed_vault()
    ids = [r.id for r in (await rows(sessionmaker)).values()]
    embedded = await embedding_count(sessionmaker, ids)
    empty(vault)

    for _ in range(3):
        assert await indexer._index_pass_once(None) is False
        assert len(await rows(sessionmaker)) == 50
    assert await embedding_count(sessionmaker, ids) == embedded
    run = await last_run(sessionmaker)
    assert "IndexIndeterminate" in run.error and "50 note(s)" in run.error
    assert str(vault) not in run.error
    assert indexer_health.run_outcome(run.error) == indexer_health.RUN_FAILED
    assert indexer_health._scopes[None].index_consecutive_failures == 3
    assert indexer_health.snapshot()["status"] == "degraded"

    # Confirmed: the next pass prunes once and consumes the permission.
    empty_prune.grant(None, canonical_vault_root(vault))
    result = await indexer.index_vault()
    assert tuple(result) == (0, 0)
    assert await rows(sessionmaker) == {}
    assert not empty_prune.pending(None)

    # Indexed again, emptied again: refused without a new permission.
    for i in range(5):
        write(vault, f"m{i}.md", f"again {i}\n")
    await indexer.index_vault()
    empty(vault)
    with pytest.raises(indexer.IndexIndeterminate):
        await indexer.index_vault()
    assert len(await rows(sessionmaker)) == 5


async def test_a_populated_pass_consumes_the_permission_without_pruning(
    sessionmaker, vault
):
    write(vault, "a.md", "a\n")
    write(vault, "b.md", "b\n")
    await indexer.index_vault()
    empty_prune.grant(None, canonical_vault_root(vault))
    await indexer.index_vault()
    assert not empty_prune.pending(None)
    assert set(await rows(sessionmaker)) == {"a.md", "b.md"}
    empty(vault)
    with pytest.raises(indexer.IndexIndeterminate):
        await indexer.index_vault()
    assert set(await rows(sessionmaker)) == {"a.md", "b.md"}


async def test_a_permission_that_expires_during_the_lock_wait_authorises_nothing(
    sessionmaker, vault, monkeypatch
):
    write(vault, "a.md", "a\n")
    await indexer.index_vault()
    empty(vault)
    now = [1000.0]
    monkeypatch.setattr(empty_prune, "_clock", lambda: now[0])
    empty_prune.grant(None, canonical_vault_root(vault))

    real = indexer.acquire_generation_lock_unbounded

    async def slow_lock(session):
        now[0] += empty_prune.DEFAULT_TTL_SECONDS + 1
        await real(session)

    monkeypatch.setattr(indexer, "acquire_generation_lock_unbounded", slow_lock)
    with pytest.raises(indexer.IndexIndeterminate):
        await indexer.index_vault()
    assert set(await rows(sessionmaker)) == {"a.md"}
    assert not empty_prune.pending(None), "never returned to the registry"


async def test_a_reassignment_before_the_prune_authorises_nothing(
    sessionmaker, vault, monkeypatch
):
    write(vault, "a.md", "a\n")
    await indexer.index_vault()
    empty(vault)
    empty_prune.grant(None, canonical_vault_root(vault))

    real = indexer.acquire_generation_lock_unbounded

    async def reassigning_lock(session):
        monkeypatch.setattr(
            indexer.settings, "vault_path", str(vault) + "-elsewhere", raising=False
        )
        await real(session)

    monkeypatch.setattr(indexer, "acquire_generation_lock_unbounded", reassigning_lock)
    with pytest.raises(indexer.IndexIndeterminate):
        await indexer.index_vault()
    assert set(await rows(sessionmaker)) == {"a.md"}


async def test_the_locked_recheck_sees_a_row_the_snapshot_did_not(
    sessionmaker, vault, monkeypatch
):
    real = indexer.acquire_generation_lock_unbounded

    async def racing_lock(session):
        async with sessionmaker() as other:
            other.add(NoteMetadata(
                user_id=None, file_path="raced.md", title="raced", tags=[],
                frontmatter={}, content_hash="0" * 64,
            ))
            await other.commit()
        await real(session)

    monkeypatch.setattr(indexer, "acquire_generation_lock_unbounded", racing_lock)
    with pytest.raises(indexer.IndexIndeterminate) as exc:
        await indexer.index_vault()
    assert "1 note(s)" in str(exc.value)
    assert set(await rows(sessionmaker)) == {"raced.md"}


async def test_an_unlistable_root_deletes_nothing_even_with_a_permission(
    sessionmaker, vault, monkeypatch
):
    write(vault, "a.md", "a\n")
    await indexer.index_vault()
    empty_prune.grant(None, canonical_vault_root(vault))

    def failing(_target):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(os, "scandir", failing)
    with pytest.raises(indexer.IndexIndeterminate) as exc:
        await indexer.index_vault()
    assert "could not be listed" in str(exc.value)
    assert set(await rows(sessionmaker)) == {"a.md"}


async def test_a_scope_with_no_rows_is_not_refused(sessionmaker, vault):
    result = await indexer.index_vault()
    assert tuple(result) == (0, 0)


async def test_one_tenants_empty_root_does_not_affect_another(sessionmaker, vault):
    a_root, b_root = vault / "a", vault / "b"
    a = await tenant(sessionmaker, a_root, "tenant-a")
    b = await tenant(sessionmaker, b_root, "tenant-b")
    try:
        write(a_root, "x.md", "a's note\n")
        write(b_root, "y.md", "b's note\n")
        await indexer.index_vault(user_id=a)
        await indexer.index_vault(user_id=b)
        empty(a_root)
        write(b_root, "z.md", "b's new note\n")

        with pytest.raises(indexer.IndexIndeterminate):
            await indexer.index_vault(user_id=a)
        result = await indexer.index_vault(user_id=b)
        assert result.notes_indexed == 1
        assert set(await rows(sessionmaker, a)) == {"x.md"}
        assert set(await rows(sessionmaker, b)) == {"y.md", "z.md"}

        # A permission for one tenant is not the other's.
        empty_prune.grant(b, canonical_vault_root(b_root))
        with pytest.raises(indexer.IndexIndeterminate):
            await indexer.index_vault(user_id=a)
        assert empty_prune.pending(b)
    finally:
        vault_service.clear_user_vault_cache(user_id=a)
        vault_service.clear_user_vault_cache(user_id=b)


async def test_index_scope_now_touches_only_its_scope(
    sessionmaker, vault, monkeypatch
):
    async def no_overlaps(_where):
        return True

    monkeypatch.setattr(indexer, "detect_root_overlaps", no_overlaps)
    a_root, b_root = vault / "a", vault / "b"
    a = await tenant(sessionmaker, a_root, "scope-a")
    b = await tenant(sessionmaker, b_root, "scope-b")
    try:
        write(a_root, "x.md", "a\n")
        write(b_root, "y.md", "b\n")
        await indexer.index_vault(user_id=a)
        await indexer.index_vault(user_id=b)
        empty(a_root)
        write(b_root, "new.md", "b new\n")
        empty_prune.grant(a, canonical_vault_root(a_root))

        assert await indexer.index_scope_now(a) is True
        assert await rows(sessionmaker, a) == {}
        assert set(await rows(sessionmaker, b)) == {"y.md"}, "b is untouched"
        async with sessionmaker() as session:
            runs = (await session.execute(select(IndexerRun))).scalars().all()
        assert [(r.trigger, r.user_id) for r in runs] == [("manual", a)]
    finally:
        vault_service.clear_user_vault_cache(user_id=a)
        vault_service.clear_user_vault_cache(user_id=b)


async def test_a_permission_that_expires_during_the_assignment_read_authorises_nothing(
    sessionmaker, vault, monkeypatch
):
    """Review r1 (Codex C2/C3): the decisive check follows every await.

    The clock passes expiry while the re-check is awaiting the `users` read,
    after any expiry test placed before that await would have passed.
    """
    import sys

    from sqlalchemy.ext.asyncio import AsyncSession as _Session

    a_root = vault / "t"
    a = await tenant(sessionmaker, a_root, "expiring")
    try:
        write(a_root, "x.md", "x\n")
        write(a_root, "y.md", "y\n")
        await indexer.index_vault(user_id=a)
        await indexer.embed_vault(user_id=a)
        before = await rows(sessionmaker, a)
        ids = [r.id for r in before.values()]
        embedded = await embedding_count(sessionmaker, ids)
        assert embedded >= 2
        empty(a_root)

        now = [1000.0]
        monkeypatch.setattr(empty_prune, "_clock", lambda: now[0])
        empty_prune.grant(a, canonical_vault_root(a_root))

        real_execute = _Session.execute
        advanced: list[bool] = []

        async def execute(self, statement, *args, **kwargs):
            frame = sys._getframe(1)
            while frame is not None:
                if frame.f_code.co_name == "_recheck_empty_prune":
                    if not advanced:
                        advanced.append(True)
                        result = await real_execute(self, statement, *args, **kwargs)
                        now[0] += empty_prune.DEFAULT_TTL_SECONDS + 1
                        return result
                    break
                frame = frame.f_back
            return await real_execute(self, statement, *args, **kwargs)

        monkeypatch.setattr(_Session, "execute", execute)
        with pytest.raises(indexer.IndexIndeterminate):
            await indexer.index_vault(user_id=a)
        assert advanced, "the re-check's assignment read was reached"
        after = await rows(sessionmaker, a)
        assert set(after) == set(before)
        assert {p: r.id for p, r in after.items()} == {p: r.id for p, r in before.items()}
        assert await embedding_count(sessionmaker, ids) == embedded
        assert not empty_prune.pending(a)
    finally:
        vault_service.clear_user_vault_cache(user_id=a)


@pytest.mark.skipif(running_as_root, reason="root ignores directory modes")
async def test_a_quarantine_entry_beneath_an_unlisted_directory_is_kept(
    sessionmaker, vault
):
    """Verifier r1: the "not seen" sweep spares entries under a failed prefix,
    and clears them once the directory is listed and the note is gone."""
    write(vault, "sub/bad.md", "bad body\n")
    write(vault, "top.md", "top\n")
    await indexer.index_vault()
    bad = (await rows(sessionmaker))["sub/bad.md"]
    indexer._quarantine_note(None, indexer.PoisonCandidate(
        "sub/bad.md", bad.content_hash, "22021", "row"
    ))
    # Quarantined by the next pass: its row is pruned, its entry stays.
    await indexer.index_vault()
    assert "sub/bad.md" not in await rows(sessionmaker)
    assert indexer.quarantined_paths(None) == ["sub/bad.md"]

    with unreadable(vault / "sub"):
        result = await indexer.index_vault()
    assert result.walk_failed == ("sub",)
    assert indexer.quarantined_paths(None) == ["sub/bad.md"], (
        "unseen because its directory was not listed, not because it is gone"
    )

    (vault / "sub" / "bad.md").unlink()
    result = await indexer.index_vault()
    assert result.walk_failed == ()
    assert indexer.quarantined_paths(None) == []
