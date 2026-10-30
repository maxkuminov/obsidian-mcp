"""#308, design D3/D5 — a poison note is quarantined and removed from the
index, against a real PostgreSQL.

What only a real database can show: that a genuine statement failure at each
per-note write site (the id-preserving move, a row of the batch upsert, the
keyword vector at its floor, a note's link inserts) unwinds through its
savepoint, that the attempt then rolls back in full and the pass re-runs
without the note, and that the rest of the scope commits. The failures are
planted with triggers (`_poison`) because D1/D2 left no ordinary note content
that reaches these sites — except the lone surrogate a YAML escape produces,
which asyncpg refuses client-side and which is tested here as it is.

Skipped unless `PGVECTOR_TEST_ADMIN_URL` is set (see `_harness`).
"""
import asyncio
import hashlib
import os

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import src.database
from src.config import settings
from src.models.db import IndexerRun, NoteEmbedding, NoteLink, NoteMetadata, User
from src.services import embeddings as embeddings_service
from src.services import indexer, indexer_health
from src.services import vault as vault_service
from src.services.search import full_text_search
from src.services.transfer import canonical_vault_root
import _harness
import _poison

pytestmark = [
    _harness.requires_pgvector,
    pytest.mark.asyncio(loop_scope="module"),
]

DIM = 8
VECTOR = [1.0] + [0.0] * (DIM - 1)


def sha(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


@pytest.fixture(scope="module")
def migrated_url():
    yield from _harness.throwaway_database("quarantine_308", DIM)


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
        await session.execute(text("DELETE FROM note_links"))
        await session.execute(text("DELETE FROM note_embeddings"))
        await session.execute(text("DELETE FROM notes_metadata"))
        await session.execute(text("DELETE FROM indexer_runs"))
        await session.execute(text("DELETE FROM users"))
        await session.commit()
    await _poison.install(sessionmaker)

    yield root

    await _poison.uninstall(sessionmaker)
    indexer._last_full_hash.clear()


async def row(sessionmaker, path: str, user_id: int | None = None):
    async with sessionmaker() as session:
        owner = (
            NoteMetadata.user_id.is_(None)
            if user_id is None
            else NoteMetadata.user_id == user_id
        )
        return (
            await session.execute(
                select(NoteMetadata).where(NoteMetadata.file_path == path, owner)
            )
        ).scalar_one_or_none()


async def paths(sessionmaker) -> set[str]:
    async with sessionmaker() as session:
        return set(
            (await session.execute(select(NoteMetadata.file_path))).scalars().all()
        )


async def keyword(sessionmaker, query: str) -> list[str]:
    async with sessionmaker() as session:
        return [r["path"] for r in await full_text_search(session, query)]


async def semantic(sessionmaker, query: str) -> list[str]:
    async with sessionmaker() as session:
        return [
            r["path"]
            for r in await embeddings_service.semantic_search(session, query, limit=50)
        ]


def restarts(caplog) -> int:
    return sum(
        1 for r in caplog.records if "Re-running the index pass" in r.getMessage()
    )


# ══════════════════════════════════════════════════════════════════════════
# Each attributable write site, after an earlier successful move
# ══════════════════════════════════════════════════════════════════════════

SITES = [
    # (site, SQLSTATE the trigger raises)
    ("upsert", "22P02"),
    ("tsvector", "54000"),
    ("link", "22021"),
    ("move", "54000"),
]


@pytest.mark.parametrize("site,code", SITES, ids=[s for s, _ in SITES])
async def test_poison_at_each_site_is_quarantined_and_the_rest_commits(
    sessionmaker, vault, caplog, site, code
):
    (vault / "Mover.md").write_text("mover body kumquatish\n", encoding="utf-8")
    (vault / "Old.md").write_text("old body persimmonish\n", encoding="utf-8")
    for i in range(5):
        (vault / f"note{i}.md").write_text(f"first {i}\n", encoding="utf-8")
    await indexer.index_vault()
    mover_id = (await row(sessionmaker, "Mover.md")).id

    # The pass under test: a successful id-preserving move, five edits, and
    # one poison note — a move destination for the `move` site, a new note
    # with a link otherwise.
    (vault / "Sub").mkdir()
    os.rename(vault / "Mover.md", vault / "Sub" / "Mover.md")
    for i in range(5):
        (vault / f"note{i}.md").write_text(
            f"edited {i} [[note{(i + 1) % 5}]]\n", encoding="utf-8"
        )
    if site == "move":
        os.rename(vault / "Old.md", vault / "New.md")
        bad = "New.md"
    else:
        (vault / "Bad.md").write_text("bad body [[note1]]\n", encoding="utf-8")
        bad = "Bad.md"
    await _poison.poison(sessionmaker, site, bad, code)

    with caplog.at_level("INFO", logger="src.services.indexer"):
        result = await indexer.index_vault()

    assert result.quarantined == (bad,)
    assert restarts(caplog) == 1
    quarantine_logs = [
        r for r in caplog.records
        if r.levelname == "ERROR" and r.getMessage().startswith("Quarantined ")
    ]
    assert len(quarantine_logs) == 1 and code in quarantine_logs[0].getMessage()
    assert indexer.quarantine_count() == 1

    moved = await row(sessionmaker, "Sub/Mover.md")
    assert moved is not None and moved.id == mover_id, "the earlier move commits"
    assert await row(sessionmaker, bad) is None
    if site == "move":
        assert await row(sessionmaker, "Old.md") is None, (
            "the move source is deleted as for a vanished file"
        )
    for i in range(5):
        r = await row(sessionmaker, f"note{i}.md")
        assert r.content_hash == sha(f"edited {i} [[note{(i + 1) % 5}]]\n")
    async with sessionmaker() as session:
        links = (await session.execute(select(func.count(NoteLink.id)))).scalar_one()
    assert links == 5, "every other note's links commit"

    # The next tick does not retry it while its content is unchanged.
    caplog.clear()
    with caplog.at_level("INFO", logger="src.services.indexer"):
        again = await indexer.index_vault()
    assert again.notes_indexed == 0
    assert again.quarantined == (bad,)
    assert restarts(caplog) == 0
    assert await row(sessionmaker, bad) is None


async def test_three_poison_rows_in_one_batch_are_quarantined_in_one_restart(
    sessionmaker, vault, caplog
):
    for i in range(10):
        (vault / f"n{i}.md").write_text(f"body {i}\n", encoding="utf-8")
    for i in (2, 5, 7):
        await _poison.poison(sessionmaker, "upsert", f"n{i}.md", "22001")

    with caplog.at_level("INFO", logger="src.services.indexer"):
        result = await indexer.index_vault()

    assert result.quarantined == ("n2.md", "n5.md", "n7.md")
    assert restarts(caplog) == 1, "every row that fails alone, in one restart"
    assert await paths(sessionmaker) == {
        f"n{i}.md" for i in range(10) if i not in (2, 5, 7)
    }


async def test_a_batch_failure_no_single_row_reproduces_is_not_poison(
    sessionmaker, vault
):
    """A class-22 failure of the multi-row statement that no row produces
    alone is not attributable to a note: the replay finds nothing, the
    original error propagates, nothing commits and nothing is quarantined."""
    for i in range(3):
        (vault / f"n{i}.md").write_text(f"body {i}\n", encoding="utf-8")
    await _poison.poison(sessionmaker, "batch", "*", "22000")

    with pytest.raises(Exception) as excinfo:
        await indexer.index_vault()
    assert indexer.poison_sqlstate(excinfo.value) == "22000"
    assert "batch-only" in str(excinfo.value)
    assert indexer.quarantine_count() == 0
    assert await paths(sessionmaker) == set()


# ══════════════════════════════════════════════════════════════════════════
# Removed, not served stale; and the way back
# ══════════════════════════════════════════════════════════════════════════


async def test_an_indexed_and_embedded_note_that_turns_poison_is_removed(
    sessionmaker, vault
):
    (vault / "target.md").write_text("[[victim]]\n", encoding="utf-8")
    (vault / "victim.md").write_text(
        "zanzibarite content [[target]]\n", encoding="utf-8"
    )
    await indexer.index_vault()
    await indexer.embed_vault(user_id=None)
    victim = await row(sessionmaker, "victim.md")
    assert victim.embedded_content_hash == victim.content_hash
    assert await keyword(sessionmaker, "zanzibarite") == ["victim.md"]
    assert "victim.md" in await semantic(sessionmaker, "zanzibarite")

    (vault / "victim.md").write_text(
        "zanzibarite then quokkaism [[target]]\n", encoding="utf-8"
    )
    await _poison.poison(sessionmaker, "tsvector", "victim.md")
    result = await indexer.index_vault()
    assert result.quarantined == ("victim.md",)

    assert await row(sessionmaker, "victim.md") is None
    async with sessionmaker() as session:
        vectors = (
            await session.execute(
                select(func.count(NoteEmbedding.id)).where(
                    NoteEmbedding.note_id == victim.id
                )
            )
        ).scalar_one()
        outgoing = (
            await session.execute(
                select(func.count(NoteLink.id)).where(
                    NoteLink.source_note_id == victim.id
                )
            )
        ).scalar_one()
    assert vectors == 0 and outgoing == 0
    assert await keyword(sessionmaker, "zanzibarite") == []
    assert await keyword(sessionmaker, "quokkaism") == []
    assert "victim.md" not in await semantic(sessionmaker, "zanzibarite")
    assert (vault / "victim.md").read_text(encoding="utf-8").startswith("zanzibarite then")


async def test_editing_a_quarantined_note_retries_it_and_clears_the_entry(
    sessionmaker, vault
):
    (vault / "Bad.md").write_text("poison version\n", encoding="utf-8")
    (vault / "Good.md").write_text("good\n", encoding="utf-8")
    await _poison.poison(sessionmaker, "tsvector", "Bad.md")
    await indexer.index_vault()
    assert indexer.quarantined_paths(None) == ["Bad.md"]

    # The cause is gone but the content is not changed: still quarantined —
    # the entry is keyed to the hash that failed.
    await _poison.cure(sessionmaker)
    still = await indexer.index_vault()
    assert still.quarantined == ("Bad.md",)
    assert await row(sessionmaker, "Bad.md") is None

    (vault / "Bad.md").write_text("fixed version\n", encoding="utf-8")
    fixed = await indexer.index_vault()
    assert fixed.quarantined == ()
    assert (await row(sessionmaker, "Bad.md")).content_hash == sha("fixed version\n")
    assert indexer.quarantine_count() == 0


async def test_deleting_a_quarantined_note_clears_the_entry(sessionmaker, vault):
    (vault / "Bad.md").write_text("poison version\n", encoding="utf-8")
    await _poison.poison(sessionmaker, "upsert", "Bad.md", "22001")
    await indexer.index_vault()
    assert indexer.quarantine_count() == 1

    (vault / "Bad.md").unlink()
    await indexer.index_vault()
    assert indexer.quarantine_count() == 0


async def test_a_non_poison_error_at_a_site_still_aborts_the_pass(
    sessionmaker, vault
):
    (vault / "Good.md").write_text("good\n", encoding="utf-8")
    (vault / "Bad.md").write_text("bad [[Good]]\n", encoding="utf-8")
    await _poison.poison(sessionmaker, "link", "Bad.md", "55P03")
    with pytest.raises(Exception):
        await indexer.index_vault()
    assert await paths(sessionmaker) == set()
    assert indexer.quarantine_count() == 0


# ══════════════════════════════════════════════════════════════════════════
# C8: a failure that does not reproduce is not the note's
# ══════════════════════════════════════════════════════════════════════════

ONE_SHOT = [
    # (site, shots, SQLSTATE). The batch upsert replays the failing batch a
    # row at a time before it confirms, so its single-row statement is the
    # second shot — two shots reach the confirm-retry there.
    ("upsert", 2, "22P02"),
    ("tsvector", 1, "54000"),
    ("link", 1, "22021"),
    ("move", 1, "54000"),
]


@pytest.mark.parametrize(
    "site,shots,code", ONE_SHOT, ids=[s for s, _, _ in ONE_SHOT]
)
async def test_a_failure_the_retry_does_not_reproduce_is_not_quarantined(
    sessionmaker, vault, caplog, site, shots, code
):
    """A note whose write fails once and then succeeds alone was not refused
    for its data: nothing is quarantined, the pass fails as an ordinary
    failure with nothing committed, and the next tick indexes the note."""
    (vault / "Good.md").write_text("good\n", encoding="utf-8")
    if site == "move":
        (vault / "Old.md").write_text("old body\n", encoding="utf-8")
        await indexer.index_vault()
        os.rename(vault / "Old.md", vault / "New.md")
        bad = "New.md"
    else:
        (vault / "Bad.md").write_text("bad body [[Good]]\n", encoding="utf-8")
        bad = "Bad.md"
    before = await paths(sessionmaker)
    await _poison.poison(sessionmaker, site, bad, code, shots=shots)

    with caplog.at_level("INFO", logger="src.services.indexer"):
        with pytest.raises(Exception) as excinfo:
            await indexer.index_vault()
    assert indexer.poison_sqlstate(excinfo.value) == code
    assert indexer.quarantine_count() == 0
    assert restarts(caplog) == 0
    assert not any(
        r.getMessage().startswith("Quarantined ") for r in caplog.records
    )
    assert any(
        "succeeded when retried alone" in r.getMessage() for r in caplog.records
    ), "the confirm-retry itself is what declined the quarantine"
    assert await paths(sessionmaker) == before, "nothing from the pass commits"

    result = await indexer.index_vault()
    assert result.quarantined == ()
    assert await row(sessionmaker, bad) is not None, "indexed on the next tick"
    if site == "move":
        assert await row(sessionmaker, "Old.md") is None


async def test_a_quarantined_keyword_vector_logs_one_error_without_driver_text(
    sessionmaker, vault, caplog
):
    """F4: the floor failure of a note that is quarantined is reported once,
    by the quarantine's own ERROR (path and SQLSTATE) — not also by the
    helper's `logger.exception`, whose driver message can quote the note."""
    (vault / "Bad.md").write_text("zanzibarish secret body\n", encoding="utf-8")
    await _poison.poison(sessionmaker, "tsvector", "Bad.md", "22P02")

    with caplog.at_level("INFO", logger="src.services.indexer"):
        result = await indexer.index_vault()
    assert result.quarantined == ("Bad.md",)
    errors = [r for r in caplog.records if r.levelno >= 40]
    assert len(errors) == 1, [r.getMessage() for r in errors]
    message = errors[0].getMessage()
    assert message.startswith("Quarantined ") and "22P02" in message
    assert errors[0].exc_info is None
    assert "zanzibarish" not in message and "synthetic" not in message


# ══════════════════════════════════════════════════════════════════════════
# The lone surrogate: asyncpg's client-side refusal
# ══════════════════════════════════════════════════════════════════════════


async def test_what_asyncpg_raises_for_a_lone_surrogate_bind(sessionmaker):
    """The client-side encode failure, as the driver actually surfaces it.

    asyncpg cannot UTF-8-encode a lone surrogate, so it refuses the bind
    before anything reaches the server — and reports it as
    `asyncpg.exceptions.DataError` carrying `sqlstate == "22000"`, wrapped by
    SQLAlchemy in `DBAPIError` whose `.orig.sqlstate` is the same. So the
    class-22 rule covers it without a special case, and the savepoint leaves
    the transaction usable like a server-side failure.
    """
    import asyncpg
    from sqlalchemy.exc import DBAPIError

    async with sessionmaker() as session:
        with pytest.raises(DBAPIError) as excinfo:
            async with session.begin_nested():
                await session.execute(text("SELECT CAST(:v AS text)"), {"v": "a\ud800b"})
        orig = excinfo.value.orig
        assert orig.sqlstate == "22000"
        assert isinstance(orig.__cause__, asyncpg.exceptions.DataError)
        assert isinstance(orig.__cause__.__cause__, UnicodeEncodeError)
        assert indexer.poison_sqlstate(excinfo.value) == "22000"
        assert (await session.execute(text("SELECT 1"))).scalar_one() == 1


async def test_a_lone_surrogate_from_yaml_is_already_sanitised_upstream(
    sessionmaker, vault
):
    """Every route a YAML `"\\ud800"` escape has to the database is closed
    before the write, so none needs the quarantine: `coerce_text` (#126)
    refuses an unencodable title (the stem is used) and tag (dropped), and
    `parse_frontmatter`'s scrub removes an unencodable value before the JSONB
    boundary. The note indexes and nothing is quarantined — the quarantine
    stays the backstop for whatever the next route turns out to be."""
    (vault / "Surrogate.md").write_text(
        '---\ntitle: "a\\ud800b"\ntags: ["x\\ud800y", "ok"]\n'
        'other: "v\\ud800"\nkeep: 1\n---\nbody\n',
        encoding="utf-8",
    )
    result = await indexer.index_vault()
    assert result.quarantined == ()
    note = await row(sessionmaker, "Surrogate.md")
    assert note.title == "Surrogate" and note.tags == ["ok"]
    assert note.frontmatter.get("keep") == 1 and "other" not in note.frontmatter


# ══════════════════════════════════════════════════════════════════════════
# Re-derive, the retry bound, and every entrypoint
# ══════════════════════════════════════════════════════════════════════════


async def tenant(sessionmaker, root, name: str) -> int:
    root.mkdir(exist_ok=True)
    async with sessionmaker() as session:
        user = User(username=name, password_hash="x", vault_path=str(root))
        session.add(user)
        await session.commit()
        await vault_service.warm_user_vault_cache(session, user_id=user.id)
        return user.id


async def test_a_quarantine_during_a_re_derive_stamps_and_settles(
    sessionmaker, vault
):
    root = vault / "tenant"
    uid = await tenant(sessionmaker, root, "rederive")
    try:
        (root / "a.md").write_text("a\n", encoding="utf-8")
        (root / "Same.md").write_text("new vault's poison\n", encoding="utf-8")
        async with sessionmaker() as session:
            session.add(NoteMetadata(
                user_id=uid, file_path="Same.md", title="foreign", tags=[],
                frontmatter={}, content_hash=sha("from the previous vault"),
            ))
            await session.commit()
        await _poison.poison(sessionmaker, "tsvector", "Same.md")

        result = await indexer.index_vault(user_id=uid)
        assert result.quarantined == ("Same.md",)
        assert result.rederive == indexer.REDERIVE_RECORDED
        assert await row(sessionmaker, "Same.md", uid) is None, (
            "the previous vault's row is deleted, not certified"
        )
        async with sessionmaker() as session:
            recorded = (
                await session.execute(
                    select(User.indexed_vault_assignment).where(User.id == uid)
                )
            ).scalar_one()
        assert recorded == canonical_vault_root(root)
        a_before = (await row(sessionmaker, "a.md", uid)).indexed_at

        again = await indexer.index_vault(user_id=uid)
        assert again.rederive is None, "the scope left re-derive"
        assert again.notes_indexed == 0, "the next tick re-upserts nothing"
        assert (await row(sessionmaker, "a.md", uid)).indexed_at == a_before
    finally:
        vault_service.clear_user_vault_cache(user_id=uid)


async def test_the_retry_bound_fails_the_pass_and_health_counts_it(
    sessionmaker, vault, monkeypatch
):
    monkeypatch.setattr(
        indexer.settings, "indexer_quarantine_retries_per_tick", 0, raising=False
    )

    async def no_embed(*_a, **_k):
        return None

    monkeypatch.setattr(indexer, "embed_vault", no_embed)
    indexer_health.set_quarantine_count_provider(indexer.quarantine_count)
    (vault / "Good.md").write_text("good\n", encoding="utf-8")
    (vault / "Bad.md").write_text("bad\n", encoding="utf-8")
    await _poison.poison(sessionmaker, "tsvector", "Bad.md")

    assert await indexer._index_pass_once(None) is False
    assert await paths(sessionmaker) == set(), "nothing of the last attempt commits"
    assert indexer_health._scopes[None].index_consecutive_failures == 1
    assert indexer.quarantined_paths(None) == ["Bad.md"], (
        "the note found stays quarantined for the next tick"
    )

    assert await indexer._index_pass_once(None) is True
    assert await paths(sessionmaker) == {"Good.md"}
    assert indexer_health._scopes[None].index_consecutive_failures == 0, (
        "a quarantining pass is a successful pass"
    )
    health = indexer_health.snapshot()
    assert health["quarantined_notes"] == 1 and health["status"] == "degraded"
    assert "Bad.md" not in repr(health)

    async with sessionmaker() as session:
        errors = (
            await session.execute(
                select(IndexerRun.error).order_by(IndexerRun.id)
            )
        ).scalars().all()
    assert "QuarantineRetriesExhausted" in errors[0]
    assert errors[1] == "quarantined 1 note(s): Bad.md"


async def test_the_startup_pass_recovers_the_same_way(
    sessionmaker, vault, monkeypatch
):
    async def noop(*_a, **_k):
        return None

    monkeypatch.setattr(indexer, "detect_root_overlaps", noop)
    monkeypatch.setattr(indexer, "link_backfill_pass", noop)
    monkeypatch.setattr(indexer, "embed_vault", noop)
    monkeypatch.setattr(indexer.settings, "index_interval_seconds", 3600, raising=False)
    monkeypatch.setattr(indexer, "last_index_run_at", None)
    (vault / "Good.md").write_text("good\n", encoding="utf-8")
    (vault / "Bad.md").write_text("bad\n", encoding="utf-8")
    await _poison.poison(sessionmaker, "upsert", "Bad.md", "22001")

    task = asyncio.create_task(indexer.run_indexer_loop())
    try:
        for _ in range(400):
            if indexer.last_index_run_at is not None or task.done():
                break
            await asyncio.sleep(0.05)
        assert indexer.last_index_run_at is not None
        assert indexer.last_index_run_ok is True
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert await paths(sessionmaker) == {"Good.md"}
    assert indexer.quarantined_paths(None) == ["Bad.md"]
    assert indexer_health._scopes[None].index_consecutive_failures == 0
    async with sessionmaker() as session:
        run = (
            await session.execute(select(IndexerRun.trigger, IndexerRun.error))
        ).one()
    assert run.trigger == "startup"
    assert run.error == "quarantined 1 note(s): Bad.md"


async def test_the_manual_reindex_recovers_the_same_way(
    sessionmaker, vault, monkeypatch
):
    from src.control_panel import routes

    async def noop(*_a, **_k):
        return None

    monkeypatch.setattr(indexer, "detect_root_overlaps", noop)
    monkeypatch.setattr(indexer, "embed_vault", noop)
    monkeypatch.setattr(routes.settings, "multi_user_mode", False, raising=False)
    (vault / "Good.md").write_text("good\n", encoding="utf-8")
    (vault / "Bad.md").write_text("bad [[Good]]\n", encoding="utf-8")
    await _poison.poison(sessionmaker, "link", "Bad.md", "22021")

    await routes._reindex_background(full_hash=True)

    assert await paths(sessionmaker) == {"Good.md"}
    assert indexer.quarantined_paths(None) == ["Bad.md"]
    assert indexer_health._scopes[None].index_consecutive_failures == 0
    async with sessionmaker() as session:
        run = (
            await session.execute(select(IndexerRun.trigger, IndexerRun.error))
        ).one()
    assert run.trigger == "manual"
    assert run.error == "quarantined 1 note(s): Bad.md"
