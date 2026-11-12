"""#392 — the opt-in exclusion of sync-conflict copies, against PostgreSQL.

What needs the database: that turning the setting on deletes a copy's row
with its embeddings, task rows and keyword vector, leaves inbound links
dangling and the original untouched, and records exactly the feed events the
change-feed delta states; that turning it off indexes the copies as new notes
(`created`, or `reset` over an empty scope, or nothing when an agent's MCP
event already says it, limitation L9); that move pairing never crosses the
exclusion boundary; that a directory the walk cannot list still protects its
rows; that the empty-root guard counts excluded files as discovered; and that
`read_note` / `edit_note` / `move_note` keep working on an excluded copy by
exact path.

Skipped unless `PGVECTOR_TEST_ADMIN_URL` is set (see `_harness`).
"""
import logging
import os

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import src.database
import src.mcp_server.tools as tools
from src.auth.session import current_actor, current_principal
from src.config import settings
from src.mcp_server.auth import current_permission
from src.models.db import (
    IndexerRun, NoteEmbedding, NoteLink, NoteMetadata, NoteTask, User,
)
from src.services import change_feed, indexer, indexer_health
from src.services import embeddings as embeddings_service
from src.services import vault as vault_service
from src.services.note_hash import canonical_note_hash
from src.services.transfer import canonical_vault_root
import _harness
import _poison

pytestmark = [
    _harness.requires_pgvector,
    pytest.mark.asyncio(loop_scope="module"),
]

DIM = 8
VECTOR = [1.0] + [0.0] * (DIM - 1)
FEED_TABLES = ("note_events", "note_event_scopes", "note_event_gaps")
COPY = "A (conflict 2026-05-29-19-45-39).md"
running_as_root = hasattr(os, "geteuid") and os.geteuid() == 0


@pytest.fixture(scope="module")
def migrated_url():
    yield from _harness.throwaway_database("conflict_copies_392", DIM)


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def sessionmaker(migrated_url):
    engine = create_async_engine(migrated_url, poolclass=None)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await _poison.install(maker)
    yield maker
    await _poison.uninstall(maker)
    await engine.dispose()


@pytest_asyncio.fixture(loop_scope="module")
async def vault(sessionmaker, monkeypatch, tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    for target in (settings, tools.settings, indexer.settings, change_feed.settings):
        monkeypatch.setattr(target, "vault_path", str(root), raising=False)
    monkeypatch.setattr(indexer.settings, "multi_user_mode", False, raising=False)
    monkeypatch.setattr(indexer.settings, "embedding_exclude_patterns", [], raising=False)
    monkeypatch.setattr(indexer.settings, "index_exclude_conflict_files", False)
    monkeypatch.setattr(indexer, "async_session", sessionmaker)
    monkeypatch.setattr(tools, "async_session", sessionmaker)
    monkeypatch.setattr(src.database, "async_session", sessionmaker)
    monkeypatch.setattr(embeddings_service, "async_session", sessionmaker, raising=False)
    monkeypatch.setattr(indexer, "_is_paused", lambda: False)

    async def fake_batch(chunks, **_kw):
        return [list(VECTOR) for _ in chunks]

    async def fake_one(_text, **_kw):
        return list(VECTOR)

    async def noop(*_a, **_k):
        return None

    monkeypatch.setattr(embeddings_service, "get_embeddings_batch", fake_batch)
    monkeypatch.setattr(embeddings_service, "get_embedding", fake_one)
    monkeypatch.setattr(tools, "_log_usage", noop)
    tokens = [
        (current_permission, current_permission.set("readwrite")),
        (current_actor, current_actor.set(("api_key", "laptop", "omcp_lap"))),
        (current_principal, current_principal.set(("api_key", 12))),
    ]
    indexer._last_full_hash.clear()
    indexer.clear_quarantine()
    indexer.excluded_conflict_copies.clear()
    vault_service.clear_user_vault_cache()
    async with sessionmaker() as session:
        await session.execute(text("DELETE FROM poison_rules"))
        for table in FEED_TABLES + ("note_tasks", "note_links", "note_embeddings",
                                    "notes_metadata", "indexer_runs", "users"):
            await session.execute(text(f"DELETE FROM {table}"))
        await session.commit()
    try:
        yield root
    finally:
        for var, token in reversed(tokens):
            var.reset(token)
        indexer._last_full_hash.clear()
        indexer.clear_quarantine()
        indexer.excluded_conflict_copies.clear()


def exclude(monkeypatch, on: bool) -> None:
    monkeypatch.setattr(indexer.settings, "index_exclude_conflict_files", on)


def write(root, rel: str, body: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


async def rows(sessionmaker) -> dict[str, NoteMetadata]:
    async with sessionmaker() as session:
        found = (await session.execute(
            select(NoteMetadata).where(NoteMetadata.user_id.is_(None))
        )).scalars()
        return {r.file_path: r for r in found}


async def count(sessionmaker, model, column, note_id) -> int:
    async with sessionmaker() as session:
        return (await session.execute(
            select(func.count()).select_from(model).where(column == note_id)
        )).scalar_one()


async def link_target(sessionmaker, source_id: int) -> tuple[str, int | None]:
    async with sessionmaker() as session:
        row = (await session.execute(
            select(NoteLink.target_path, NoteLink.target_note_id)
            .where(NoteLink.source_note_id == source_id)
        )).one()
        return row.target_path, row.target_note_id


async def events(sessionmaker):
    async with sessionmaker() as session:
        return (await session.execute(text(
            "SELECT kind, path, from_path, reason, source, content_hash "
            "FROM note_events WHERE user_id IS NULL ORDER BY seq"
        ))).fetchall()


async def last_run(sessionmaker) -> IndexerRun:
    async with sessionmaker() as session:
        return (await session.execute(
            select(IndexerRun).order_by(IndexerRun.id.desc()).limit(1)
        )).scalar_one()


async def seeded(root, sessionmaker):
    """A.md, its conflict copy (with a task), and B.md linking to the copy,
    indexed and embedded with the setting off."""
    write(root, "A.md", "alpha current\n")
    write(root, COPY, "alpha older\n\n- [ ] reconcile the copy\n")
    write(root, "B.md", "see [[A (conflict 2026-05-29-19-45-39)]]\n")
    await indexer.index_vault()
    await indexer.embed_vault()
    before = await rows(sessionmaker)
    assert set(before) == {"A.md", COPY, "B.md"}
    copy_id = before[COPY].id
    assert await count(sessionmaker, NoteEmbedding, NoteEmbedding.note_id, copy_id) >= 1
    assert await count(sessionmaker, NoteTask, NoteTask.note_id, copy_id) == 1
    assert (await link_target(sessionmaker, before["B.md"].id))[1] == copy_id
    return before


# ── Toggle on, then off ───────────────────────────────────────────────────


async def test_toggle_on_removes_the_copy_and_nothing_else_then_off_restores(
    vault, sessionmaker, monkeypatch
):
    before = await seeded(vault, sessionmaker)
    copy_id = before[COPY].id
    copy_bytes = (vault / COPY).read_bytes()
    n_events = len(await events(sessionmaker))

    exclude(monkeypatch, True)
    # Forget the seeding pass's backstop record, so the assertion below can
    # only pass if THIS (full-hash) pass advanced the clock.
    indexer._last_full_hash.clear()
    assert await indexer._index_pass_once(None) is True
    after = await rows(sessionmaker)
    assert set(after) == {"A.md", "B.md"}
    assert await count(sessionmaker, NoteEmbedding, NoteEmbedding.note_id, copy_id) == 0
    assert await count(sessionmaker, NoteTask, NoteTask.note_id, copy_id) == 0
    assert await link_target(sessionmaker, before["B.md"].id) == (
        "A (conflict 2026-05-29-19-45-39)", None
    ), "the inbound link is dangling"
    a0, a1 = before["A.md"], after["A.md"]
    assert (a1.id, a1.content_hash, a1.embedded_content_hash) == (
        a0.id, a0.content_hash, a0.embedded_content_hash
    )
    assert (vault / COPY).read_bytes() == copy_bytes
    run = await last_run(sessionmaker)
    assert run.error is None
    assert indexer_health.run_outcome(run.error) == indexer_health.RUN_OK
    assert indexer.excluded_conflict_copies[None] == 1
    assert None in indexer._last_full_hash, "an exclusion does not block the backstop"

    new = (await events(sessionmaker))[n_events:]
    assert [(e.kind, e.path, e.reason, e.source) for e in new] == [
        ("deleted", COPY, "not_indexable", "index")
    ]

    # A second pass writes nothing and records nothing.
    result = await indexer.index_vault()
    assert result.notes_indexed == 0 and result.excluded == 1
    assert result.not_indexable == 0
    assert len(await events(sessionmaker)) == n_events + 1

    # Off again: the copy is a new note, recorded `created`, and the dangling
    # link resolves to it.
    exclude(monkeypatch, False)
    await indexer.index_vault()
    restored = await rows(sessionmaker)
    assert COPY in restored and restored[COPY].id != copy_id
    assert (await link_target(sessionmaker, before["B.md"].id))[1] == restored[COPY].id
    tail = (await events(sessionmaker))[n_events + 1:]
    assert [(e.kind, e.path, e.content_hash) for e in tail] == [
        ("created", COPY, canonical_note_hash(copy_bytes))
    ]
    out = await tools.get_changes_impl.__wrapped__(since="ev:0")
    assert f'"{COPY}" reason=not_indexable by human/sync' in out


async def test_toggle_off_over_an_empty_scope_is_one_reset(
    vault, sessionmaker, monkeypatch
):
    exclude(monkeypatch, True)
    write(vault, COPY, "only a copy\n")
    write(vault, "X.sync-conflict-20260529-194539-ABC2DEF.md", "another\n")
    await indexer.index_vault()
    assert await rows(sessionmaker) == {}
    assert await events(sessionmaker) == []

    exclude(monkeypatch, False)
    await indexer.index_vault()
    assert len(await rows(sessionmaker)) == 2
    assert [e.kind for e in await events(sessionmaker)] == ["reset"]


async def test_toggle_off_after_an_agent_edit_records_no_created_l9(
    vault, sessionmaker, monkeypatch
):
    """Pinned limitation L9: the existing suppression treats the agent's
    `modified` event (same hash) as already saying it."""
    write(vault, "seed.md", "seed\n")
    await indexer.index_vault()
    exclude(monkeypatch, True)
    write(vault, COPY, "copy v1\n")
    await indexer.index_vault()
    out = await tools.edit_note_impl(COPY, "copy v2, merged by an agent\n")
    assert "MCP-REFUSAL" not in out, out
    await indexer.index_vault()
    assert COPY not in await rows(sessionmaker)
    mark = len(await events(sessionmaker))
    assert (await events(sessionmaker))[-1].kind == "modified"

    exclude(monkeypatch, False)
    await indexer.index_vault()
    assert COPY in await rows(sessionmaker), "the note is fully indexed"
    assert (await events(sessionmaker))[mark:] == [], "and no feed line says so (L9)"


# ── Move pairing ──────────────────────────────────────────────────────────


async def test_a_conflict_copy_is_never_a_move_destination(
    vault, sessionmaker, monkeypatch
):
    write(vault, "B.md", "bravo body\n")
    await indexer.index_vault()
    exclude(monkeypatch, True)
    mark = len(await events(sessionmaker))
    (vault / "B.md").rename(vault / "B.sync-conflict-20260529-194539-ABC2DEF.md")
    await indexer.index_vault()
    assert await rows(sessionmaker) == {}
    assert [(e.kind, e.path, e.reason) for e in (await events(sessionmaker))[mark:]] == [
        ("deleted", "B.md", "removed")
    ]


async def test_a_vanished_conflict_row_pairs_as_an_ordinary_move(
    vault, sessionmaker, monkeypatch
):
    write(vault, "seed.md", "seed\n")
    write(vault, COPY, "the only good version\n")
    await indexer.index_vault()
    copy_id = (await rows(sessionmaker))[COPY].id
    exclude(monkeypatch, True)
    (vault / COPY).rename(vault / "A2.md")
    await indexer.index_vault()
    after = await rows(sessionmaker)
    assert after["A2.md"].id == copy_id and COPY not in after


# ── #309 interplay ────────────────────────────────────────────────────────


@pytest.mark.skipif(running_as_root, reason="root ignores directory modes")
async def test_an_unlistable_directory_still_protects_its_conflict_rows(
    vault, sessionmaker, monkeypatch
):
    write(vault, "sub/C (conflict 2026-05-29-19-45-39).md", "c\n")
    write(vault, "D (conflict 2026-05-29-19-45-39).md", "d\n")
    write(vault, "keep.md", "keep\n")
    await indexer.index_vault()
    exclude(monkeypatch, True)
    os.chmod(vault / "sub", 0)
    try:
        assert await indexer._index_pass_once(None) is True
    finally:
        os.chmod(vault / "sub", 0o755)
    assert set(await rows(sessionmaker)) == {
        "sub/C (conflict 2026-05-29-19-45-39).md", "keep.md"
    }
    run = await last_run(sessionmaker)
    # The index stage's line is exactly #309's; the exclusion adds nothing to
    # it. (The embed stage, which this pass also runs, reports its own failure
    # to read the protected, never-embedded row beneath `sub`.)
    first = run.error.split("\n")[0]
    assert first == "walk incomplete: 1 dir(s) not listed: sub"
    assert "sync-conflict" not in run.error and "excluded" not in run.error


async def test_the_empty_root_guard_counts_excluded_files_as_discovered(
    vault, sessionmaker, monkeypatch
):
    write(vault, "n1.md", "one\n")
    write(vault, "n2.md", "two\n")
    write(vault, COPY, "copy\n")
    await indexer.index_vault()
    exclude(monkeypatch, True)
    (vault / "n1.md").unlink()
    (vault / "n2.md").unlink()
    result = await indexer.index_vault()
    assert result.excluded == 1
    assert await rows(sessionmaker) == {}, "determinate: every row is deleted"

    # With no `.md` file at all over an existing index: indeterminate, as today.
    write(vault, "n3.md", "three\n")
    await indexer.index_vault()
    (vault / "n3.md").unlink()
    (vault / COPY).unlink()
    with pytest.raises(indexer.IndexIndeterminate):
        await indexer.index_vault()
    assert set(await rows(sessionmaker)) == {"n3.md"}


# ── Tools by exact path ───────────────────────────────────────────────────


async def test_read_edit_and_move_out_of_an_excluded_name(
    vault, sessionmaker, monkeypatch
):
    write(vault, "seed.md", "seed\n")
    write(vault, COPY, "the only surviving version\n")
    exclude(monkeypatch, True)
    monkeypatch.setattr(tools.settings, "index_exclude_conflict_files", True)
    await indexer.index_vault()
    assert set(await rows(sessionmaker)) == {"seed.md"}

    read = await tools.read_note_impl(COPY)
    assert read.error is None and "the only surviving version" in read.content
    out = await tools.edit_note_impl(COPY, "the only surviving version, merged\n")
    assert "MCP-REFUSAL" not in out, out
    out = await tools.move_note_impl(COPY, "A.md")
    assert out.startswith(f"Moved {COPY} → A.md") and "\n" not in out, out
    await indexer.index_vault()
    after = await rows(sessionmaker)
    assert set(after) == {"seed.md", "A.md"}
    assert (vault / "A.md").read_text() == "the only surviving version, merged\n"


async def test_move_onto_an_excluded_name_notices_and_the_next_pass_removes(
    vault, sessionmaker, monkeypatch
):
    write(vault, "E.md", "echo body\n\n- [ ] an echo task\n")
    write(vault, "F.md", "links to [[E]]\n")
    await indexer.index_vault()
    await indexer.embed_vault()
    e_id = (await rows(sessionmaker))["E.md"].id

    exclude(monkeypatch, True)
    monkeypatch.setattr(tools.settings, "index_exclude_conflict_files", True)
    target = "E (conflict 2026-05-29-19-45-39).md"
    out = await tools.move_note_impl("E.md", target)
    assert out.endswith("\n" + tools.CONFLICT_EXCLUSION_NOTICE), out

    # Until the next pass the row is re-keyed onto the excluded name (L10).
    interim = await rows(sessionmaker)
    assert interim[target].id == e_id

    await indexer.index_vault()
    after = await rows(sessionmaker)
    assert set(after) == {"F.md"}
    assert await count(sessionmaker, NoteEmbedding, NoteEmbedding.note_id, e_id) == 0
    assert await count(sessionmaker, NoteTask, NoteTask.note_id, e_id) == 0
    assert await count(sessionmaker, NoteLink, NoteLink.source_note_id, e_id) == 0
    assert (vault / target).exists()


# ── Review r1 additions ───────────────────────────────────────────────────


async def test_a_quarantine_restart_never_pairs_a_conflict_copy_as_destination(
    vault, sessionmaker, monkeypatch
):
    """C: the attempt restarts on a PoisonNote with copies of the scan's
    verdicts; the copy stays not-indexable across the restart."""
    write(vault, "B.md", "bravo body\n")
    await indexer.index_vault()
    b_id = (await rows(sessionmaker))["B.md"].id
    exclude(monkeypatch, True)
    mark = len(await events(sessionmaker))
    target = "B.sync-conflict-20260529-194539-ABC2DEF.md"
    (vault / "B.md").rename(vault / target)
    write(vault, "P.md", "a new note the database refuses\n")
    await _poison.poison(sessionmaker, "upsert", "P.md", "22001")

    result = await indexer.index_vault()
    assert result.quarantined == ("P.md",), "a PoisonNote restart happened"
    assert result.excluded == 1
    after = await rows(sessionmaker)
    assert after == {}, after
    assert b_id not in {r.id for r in after.values()}
    new = [(e.kind, e.path, e.from_path, e.reason)
           for e in (await events(sessionmaker))[mark:]]
    assert ("deleted", "B.md", None, "removed") in new
    assert not [e for e in new if e[0] == "moved"]
    assert not [e for e in new if e[1] == target]


async def tenant(sessionmaker, root, name: str) -> int:
    root.mkdir(exist_ok=True)
    async with sessionmaker() as session:
        user = User(username=name, password_hash="x", vault_path=str(root))
        session.add(user)
        await session.commit()
        await vault_service.warm_user_vault_cache(session, user_id=user.id)
        return user.id


async def test_a_re_derive_over_conflict_copy_rows_records(
    vault, sessionmaker, monkeypatch
):
    """D: rows with no recorded provenance force a re-derive; the
    conflict-copy row is deleted (no withholding) and the stamp is written."""
    root = vault / "tenant"
    uid = await tenant(sessionmaker, root, "rederive392")
    try:
        write(root, "a.md", "a\n")
        write(root, COPY, "a copy\n")
        async with sessionmaker() as session:
            for path in ("a.md", COPY):
                session.add(NoteMetadata(
                    user_id=uid, file_path=path, title="foreign", tags=[],
                    frontmatter={}, content_hash="0" * 64,
                ))
            await session.commit()
        exclude(monkeypatch, True)
        result = await indexer.index_vault(user_id=uid)
        assert result.rederive == indexer.REDERIVE_RECORDED
        assert result.excluded == 1
        async with sessionmaker() as session:
            paths = (await session.execute(
                select(NoteMetadata.file_path).where(NoteMetadata.user_id == uid)
            )).scalars().all()
            recorded = (await session.execute(
                select(User.indexed_vault_assignment).where(User.id == uid)
            )).scalar_one()
        assert sorted(paths) == ["a.md"]
        assert recorded == canonical_vault_root(root)
        assert indexer.excluded_conflict_copies[uid] == 1
    finally:
        vault_service.clear_user_vault_cache(user_id=uid)


async def test_a_quarantined_note_that_becomes_excluded_is_cleared(
    vault, sessionmaker, monkeypatch
):
    """E: a quarantined note renamed onto a conflict name, and a quarantine
    entry already at a conflict path when the setting turns on."""
    write(vault, "keep.md", "keep\n")
    write(vault, "Bad.md", "refused\n")
    await _poison.poison(sessionmaker, "upsert", "Bad.md", "22001")
    await indexer.index_vault()
    assert indexer.quarantined_paths(None) == ["Bad.md"]
    await _poison.cure(sessionmaker)

    exclude(monkeypatch, True)
    renamed = "Bad (conflict 2026-05-29-19-45-39).md"
    (vault / "Bad.md").rename(vault / renamed)
    await indexer.index_vault()
    assert indexer.quarantined_paths(None) == []
    assert set(await rows(sessionmaker)) == {"keep.md"}

    # An entry at an excluded path itself (quarantined before the toggle).
    exclude(monkeypatch, False)
    await _poison.poison(sessionmaker, "upsert", renamed, "22001")
    await indexer.index_vault()
    assert indexer.quarantined_paths(None) == [renamed]
    await _poison.cure(sessionmaker)
    exclude(monkeypatch, True)
    result = await indexer.index_vault()
    assert indexer.quarantined_paths(None) == []
    assert result.excluded == 1 and result.quarantined == ()
    assert set(await rows(sessionmaker)) == {"keep.md"}


async def test_the_removal_and_count_lines(vault, sessionmaker, monkeypatch, caplog):
    """B: one INFO removal line through `_format_skips` (20 paths, then
    `, and N more`), one INFO count line, no WARNING naming a copy."""
    write(vault, "keep.md", "k\n")
    names = [f"N{i:02d} (conflict 2026-05-29-19-45-39).md" for i in range(23)]
    for n in names:
        write(vault, n, f"body {n}\n")
    await indexer.index_vault()
    exclude(monkeypatch, True)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="src.services.indexer"):
        await indexer.index_vault()
    messages = [(r.levelno, r.getMessage()) for r in caplog.records]
    removal = [m for lvl, m in messages if m.startswith("Removed the index row(s)")]
    assert len(removal) == 1, removal
    assert removal[0].startswith(
        "Removed the index row(s) of 23 excluded sync-conflict copies: "
    )
    assert removal[0].endswith(", and 3 more")
    assert sum(n in removal[0] for n in names) == 20
    counts = [m for lvl, m in messages if m.startswith("Excluded ")]
    assert counts == [
        "Excluded 23 sync-conflict copies from the index (INDEX_EXCLUDE_CONFLICT_FILES)"
    ]
    assert all(lvl == logging.INFO for lvl, m in messages if m in removal + counts)
    assert not [m for lvl, m in messages
                if lvl >= logging.WARNING and "conflict 2026" in m]

    # The next pass: the count line again, no removal line.
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="src.services.indexer"):
        await indexer.index_vault()
    msgs = [r.getMessage() for r in caplog.records]
    assert not [m for m in msgs if m.startswith("Removed the index row(s)")]
    assert [m for m in msgs if m.startswith("Excluded ")] == counts

    # Singular, and nothing at all with zero.
    for n in names[1:]:
        (vault / n).unlink()
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="src.services.indexer"):
        await indexer.index_vault()
    assert [r.getMessage() for r in caplog.records
            if r.getMessage().startswith("Excluded ")] == [
        "Excluded 1 sync-conflict copy from the index (INDEX_EXCLUDE_CONFLICT_FILES)"
    ]
    (vault / names[0]).unlink()
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="src.services.indexer"):
        await indexer.index_vault()
    assert not [r for r in caplog.records if "sync-conflict" in r.getMessage()]


async def test_a_link_rewriting_move_out_of_an_excluded_name_notices(
    vault, sessionmaker, monkeypatch
):
    """A: backlinks to an excluded copy cannot be discovered; the move
    succeeds and says so. Both notices, each once, when the destination is
    excluded too. Byte-identical output otherwise."""
    write(vault, "B.md", "see [[A (conflict 2026-05-29-19-45-39)]]\n")
    write(vault, COPY, "the copy\n")
    write(vault, "C (conflict 2026-05-29-19-45-39).md", "another copy\n")
    write(vault, "D.md", "plain\n")
    exclude(monkeypatch, True)
    monkeypatch.setattr(tools.settings, "index_exclude_conflict_files", True)
    await indexer.index_vault()
    assert set(await rows(sessionmaker)) == {"B.md", "D.md"}
    b_before = (vault / "B.md").read_text()

    out = await tools.move_note_impl(COPY, "A.md", rewrite_links=True)
    assert out.endswith("\n" + tools.CONFLICT_REWRITE_NOTICE), out
    assert out.count("\n") == 1 and tools.CONFLICT_EXCLUSION_NOTICE not in out
    assert (vault / "B.md").read_text() == b_before, "the link was not rewritten"
    assert (vault / "A.md").exists()

    both = await tools.move_note_impl(
        "C (conflict 2026-05-29-19-45-39).md",
        "C.sync-conflict-20260529-194539-ABC2DEF.md",
        rewrite_links=True,
    )
    assert both.endswith(
        "\n" + tools.CONFLICT_REWRITE_NOTICE + "\n" + tools.CONFLICT_EXCLUSION_NOTICE
    ), both
    assert both.count(tools.CONFLICT_REWRITE_NOTICE) == 1
    assert both.count(tools.CONFLICT_EXCLUSION_NOTICE) == 1

    # No rewrite notice: rewrite_links=False, or a non-matching source.
    plain = await tools.move_note_impl(
        "C.sync-conflict-20260529-194539-ABC2DEF.md", "C.md"
    )
    assert "\n" not in plain, plain
    moved = await tools.move_note_impl("D.md", "D2.md", rewrite_links=True)
    assert "\n" not in moved, moved

    # Setting off: the same shape of call carries no notice at all.
    monkeypatch.setattr(tools.settings, "index_exclude_conflict_files", False)
    write(vault, "E (conflict 2026-05-29-19-45-39).md", "e\n")
    off = await tools.move_note_impl(
        "E (conflict 2026-05-29-19-45-39).md", "E.md", rewrite_links=True
    )
    assert "\n" not in off, off
