"""#311 — re-derive progress per row, against a real PostgreSQL.

A scope whose provenance is unresolved is re-derived; before #311 every
re-deriving pass rewrote the whole scope, so one row-backed unreadable file
made every tick a full-scope upsert, keyword-vector rewrite and link rebuild.
`notes_metadata.derived_under` (migration 030) records per row that a
re-deriving pass under the same root facts fully derived it, bound to the
row's path, hash and extraction version.

What only a real database shows: that a carried-forward row is not rewritten
at all (its tuple's `xmin` is unchanged — `indexed_at` and link ids would not
see a keyword-vector UPDATE), that the stamp's in-transaction re-read sees a
row no skip named, and that `move_note`'s `target_path` rewrite clears the
markers of the sources it mutates (Codex spec review r1, MAJOR).

Read failures are injected through `read_note_at`, not `chmod 000`, which root
ignores. Skipped unless `PGVECTOR_TEST_ADMIN_URL` is set (see `_harness`).
"""
import errno
import hashlib

import pytest
import pytest_asyncio
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import src.database
import src.mcp_server.tools as tools
from src.config import settings
from src.mcp_server.auth import current_permission
from src.models.db import NoteLink, NoteMetadata, User
from src.services import embeddings as embeddings_service
from src.services import indexer, indexer_health
from src.services import vault as vault_service
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
    yield from _harness.throwaway_database("rederive_progress_311", DIM)


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def sessionmaker(migrated_url):
    engine = create_async_engine(migrated_url, poolclass=None)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield maker
    await engine.dispose()


#: Names `read_note_at` refuses, mutable per test.
UNREADABLE: set[str] = set()


@pytest_asyncio.fixture(loop_scope="module")
async def vault(sessionmaker, monkeypatch, tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    monkeypatch.setattr(settings, "vault_path", str(root), raising=False)
    monkeypatch.setattr(indexer.settings, "vault_path", str(root), raising=False)
    monkeypatch.setattr(
        indexer.settings, "embedding_exclude_patterns", [], raising=False
    )
    monkeypatch.setattr(indexer, "async_session", sessionmaker)
    monkeypatch.setattr(tools, "async_session", sessionmaker)
    monkeypatch.setattr(src.database, "async_session", sessionmaker)
    monkeypatch.setattr(embeddings_service, "async_session", sessionmaker, raising=False)
    monkeypatch.setattr(indexer, "_is_paused", lambda: False)

    async def noop(*_a, **_k):
        return None

    monkeypatch.setattr(tools, "_log_usage", noop)

    real = indexer.read_note_at

    def failing(parent_fd, name, rel=None):
        if name in UNREADABLE:
            raise PermissionError(errno.EACCES, "Permission denied", name)
        return real(parent_fd, name, rel)

    monkeypatch.setattr(indexer, "read_note_at", failing)
    UNREADABLE.clear()
    indexer._last_full_hash.clear()
    indexer_health.reset()
    vault_service.clear_user_vault_cache()

    async with sessionmaker() as session:
        for table in ("note_links", "note_embeddings", "notes_metadata", "users"):
            await session.execute(text(f"DELETE FROM {table}"))
        await session.commit()

    yield root
    UNREADABLE.clear()
    indexer._last_full_hash.clear()
    indexer_health.reset()


# ── helpers ───────────────────────────────────────────────────────────────


def write(root, files: dict[str, str]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for rel, body in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")


async def tenant(sessionmaker, root, name: str) -> int:
    root.mkdir(parents=True, exist_ok=True)
    async with sessionmaker() as session:
        user = User(username=name, password_hash="x", vault_path=str(root))
        session.add(user)
        await session.commit()
        await vault_service.warm_user_vault_cache(session, user_id=user.id)
        return user.id


async def reassign(sessionmaker, uid: int, root) -> None:
    async with sessionmaker() as session:
        await session.execute(
            update(User).where(User.id == uid).values(vault_path=str(root))
        )
        await session.commit()
        vault_service.clear_user_vault_cache(user_id=uid)
        await vault_service.warm_user_vault_cache(session, user_id=uid)


async def provenance(sessionmaker, uid: int):
    async with sessionmaker() as session:
        return (
            await session.execute(
                select(User.indexed_vault_assignment).where(User.id == uid)
            )
        ).scalar_one()


async def foreign_row(sessionmaker, uid: int, path: str) -> int:
    """A row no pass derived from the current root — the previous vault's."""
    async with sessionmaker() as session:
        note = NoteMetadata(
            user_id=uid, file_path=path, title="foreign", tags=[],
            frontmatter={}, content_hash=sha("from the previous vault"),
        )
        session.add(note)
        await session.commit()
        return note.id


async def tuples(sessionmaker, uid: int) -> dict[str, tuple]:
    """`file_path → (xmin, derived_under)` — xmin moves on any UPDATE."""
    async with sessionmaker() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT file_path, xmin::text AS x, derived_under "
                    "FROM notes_metadata WHERE user_id = :u"
                ),
                {"u": uid},
            )
        ).fetchall()
    return {r.file_path: (r.x, r.derived_under) for r in rows}


async def link_xmins(sessionmaker, uid: int) -> dict[int, str]:
    async with sessionmaker() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT l.id, l.xmin::text AS x FROM note_links l "
                    "JOIN notes_metadata n ON n.id = l.source_note_id "
                    "WHERE n.user_id = :u"
                ),
                {"u": uid},
            )
        ).fetchall()
    return {r.id: r.x for r in rows}


async def links_of(sessionmaker, uid: int, path: str) -> list[tuple]:
    async with sessionmaker() as session:
        rows = (
            await session.execute(
                select(NoteLink.target_path, NoteLink.target_note_id)
                .join(NoteMetadata, NoteLink.source_note_id == NoteMetadata.id)
                .where(NoteMetadata.user_id == uid, NoteMetadata.file_path == path)
            )
        ).all()
    return sorted((r.target_path, r.target_note_id) for r in rows)


async def note_id(sessionmaker, uid: int, path: str):
    async with sessionmaker() as session:
        return (
            await session.execute(
                select(NoteMetadata.id).where(
                    NoteMetadata.user_id == uid, NoteMetadata.file_path == path
                )
            )
        ).scalar_one_or_none()


async def run(uid: int):
    result = await indexer.index_vault(user_id=uid)
    indexer.record_index_outcome(uid, True, result)
    return result


# ══════════════════════════════════════════════════════════════════════════
# The #311 reproduction
# ══════════════════════════════════════════════════════════════════════════


async def test_a_repeated_re_derive_rewrites_nothing_already_derived(
    sessionmaker, vault, monkeypatch
):
    root = vault / "t311"
    notes = {f"n{i}.md": f"note {i} links [[n{(i + 1) % 5}]]\n" for i in range(5)}
    write(root, {**notes, "Stuck.md": "unreadable\n"})
    uid = await tenant(sessionmaker, root, "repro")
    try:
        await foreign_row(sessionmaker, uid, "Stuck.md")
        UNREADABLE.add("Stuck.md")
        monkeypatch.setattr(
            indexer.settings, "indexer_degraded_after_failures", 3, raising=False
        )

        first = await run(uid)
        assert first.rederive == indexer.REDERIVE_INCOMPLETE
        assert first.rederive_pending == 1
        after_first = await tuples(sessionmaker, uid)
        assert all(after_first[p][1] is not None for p in notes), "marked"
        assert after_first["Stuck.md"][1] is None
        links_before = await link_xmins(sessionmaker, uid)
        assert links_before

        for _ in range(2):
            again = await run(uid)
            assert again.rederive == indexer.REDERIVE_INCOMPLETE
            assert again.rederive_pending == 1
            assert again.notes_indexed == 0, "no row was rewritten"
        # Not one tuple rewritten: no upsert, no keyword-vector UPDATE, no
        # link delete-and-insert, no marker rewrite.
        assert await tuples(sessionmaker, uid) == after_first
        assert await link_xmins(sessionmaker, uid) == links_before
        assert await provenance(sessionmaker, uid) is None

        health = indexer_health.snapshot()
        assert health["status"] == "degraded", "a possibly foreign row is served"
        assert health["failing_scopes"] >= 1
    finally:
        vault_service.clear_user_vault_cache(user_id=uid)


async def test_the_file_becoming_readable_records_and_resets(
    sessionmaker, vault
):
    root = vault / "readable"
    write(root, {"a.md": "a\n", "b.md": "b\n", "Stuck.md": "now readable\n"})
    uid = await tenant(sessionmaker, root, "readable")
    try:
        await foreign_row(sessionmaker, uid, "Stuck.md")
        UNREADABLE.add("Stuck.md")
        assert (await run(uid)).rederive == indexer.REDERIVE_INCOMPLETE
        before = await tuples(sessionmaker, uid)

        UNREADABLE.clear()
        done = await run(uid)
        assert done.rederive == indexer.REDERIVE_RECORDED
        assert done.rederive_pending == 0
        assert done.notes_indexed == 1, "only the unresolved row was rewritten"
        after = await tuples(sessionmaker, uid)
        assert after["a.md"] == before["a.md"] and after["b.md"] == before["b.md"]
        assert after["Stuck.md"][1] is not None
        assert await provenance(sessionmaker, uid) == canonical_vault_root(root)
        assert indexer_health.snapshot()["status"] == "ok"

        keep = await run(uid)
        assert keep.rederive is None and keep.notes_indexed == 0
    finally:
        vault_service.clear_user_vault_cache(user_id=uid)


async def test_the_unreadable_file_deleted_is_pruned_and_records(
    sessionmaker, vault
):
    root = vault / "deleted"
    write(root, {"a.md": "a\n", "Stuck.md": "x\n"})
    uid = await tenant(sessionmaker, root, "deleted")
    try:
        await foreign_row(sessionmaker, uid, "Stuck.md")
        UNREADABLE.add("Stuck.md")
        assert (await run(uid)).rederive == indexer.REDERIVE_INCOMPLETE
        (root / "Stuck.md").unlink()
        done = await run(uid)
        assert done.rederive == indexer.REDERIVE_RECORDED
        assert set(await tuples(sessionmaker, uid)) == {"a.md"}
    finally:
        vault_service.clear_user_vault_cache(user_id=uid)


async def test_progress_survives_a_restart(sessionmaker, vault):
    root = vault / "restart"
    write(root, {"a.md": "a\n", "b.md": "b\n", "Stuck.md": "x\n"})
    uid = await tenant(sessionmaker, root, "restart")
    try:
        await foreign_row(sessionmaker, uid, "Stuck.md")
        UNREADABLE.add("Stuck.md")
        await run(uid)
        before = await tuples(sessionmaker, uid)

        # A restart forgets every in-process record: the backstop clock (so
        # the next pass is a full-hash pass), the sweep and the quarantine.
        indexer._last_full_hash.clear()
        indexer.clear_sweep_state()
        indexer.clear_quarantine(uid)
        again = await run(uid)
        assert again.notes_indexed == 0
        assert await tuples(sessionmaker, uid) == before
    finally:
        vault_service.clear_user_vault_cache(user_id=uid)


async def test_a_handle_mismatch_trusts_no_marker(sessionmaker, vault, monkeypatch):
    root = vault / "handle"
    write(root, {"a.md": "a\n", "b.md": "b\n", "Stuck.md": "x\n"})
    uid = await tenant(sessionmaker, root, "handle")
    try:
        await foreign_row(sessionmaker, uid, "Stuck.md")
        UNREADABLE.add("Stuck.md")
        monkeypatch.setattr(indexer, "read_dir_handle", lambda _fd: "1:aa")
        await run(uid)
        monkeypatch.setattr(indexer, "read_dir_handle", lambda _fd: "1:bb")
        again = await run(uid)
        assert again.notes_indexed == 2, "every readable row re-derived"
    finally:
        vault_service.clear_user_vault_cache(user_id=uid)


# ══════════════════════════════════════════════════════════════════════════
# A→B→A
# ══════════════════════════════════════════════════════════════════════════


async def test_a_b_a_keeps_only_progress_that_is_genuinely_as(
    sessionmaker, vault
):
    a = vault / "A"
    b = vault / "B"
    write(a, {"n1.md": "a1\n", "keep.md": "kept in A\n", "Stuck.md": "x\n"})
    write(b, {"n1.md": "b1\n", "keep.md": "B's keep\n", "Stuck.md": "x\n"})
    uid = await tenant(sessionmaker, a, "aba")
    try:
        await foreign_row(sessionmaker, uid, "Stuck.md")
        UNREADABLE.add("Stuck.md")
        await run(uid)  # under A: n1, keep marked under A
        under_a = await tuples(sessionmaker, uid)

        await reassign(sessionmaker, uid, b)
        UNREADABLE.add("keep.md")  # B cannot rewrite keep.md
        under_b = await run(uid)
        assert under_b.rederive == indexer.REDERIVE_INCOMPLETE
        b_rows = await tuples(sessionmaker, uid)
        assert b_rows["keep.md"] == under_a["keep.md"], "B never rewrote it"
        assert b_rows["n1.md"][1] != under_a["n1.md"][1], "re-marked under B"

        await reassign(sessionmaker, uid, a)
        UNREADABLE.discard("keep.md")
        back = await run(uid)
        a_rows = await tuples(sessionmaker, uid)
        # keep.md still carries A's marker for A's bytes: current, untouched.
        assert a_rows["keep.md"] == under_a["keep.md"]
        # n1.md was rewritten under B: not current under A, so re-derived.
        assert a_rows["n1.md"][0] != b_rows["n1.md"][0]
        assert back.notes_indexed == 1
        async with sessionmaker() as session:
            h = (
                await session.execute(
                    select(NoteMetadata.content_hash).where(
                        NoteMetadata.user_id == uid,
                        NoteMetadata.file_path == "n1.md",
                    )
                )
            ).scalar_one()
        assert h == sha("a1\n")
    finally:
        vault_service.clear_user_vault_cache(user_id=uid)


async def test_move_note_under_b_cannot_carry_mutated_links_into_a(
    sessionmaker, vault
):
    """Codex spec review r1, MAJOR: S marked under A; reassigned to B;
    `move_note(T.md, U.md)` rewrites S's link row's `target_path` before any
    pass under B rewrites S; back under A, S must be re-extracted from A's
    bytes before provenance A is recorded."""
    a = vault / "A2"
    b = vault / "B2"
    # `[[T.md]]` stores `target_path = 'T.md'`, the form `move_note`'s
    # `target_path` rewrite matches.
    write(a, {"S.md": "See [[T.md]]\n", "T.md": "t\n", "Stuck.md": "x\n"})
    write(b, {"S.md": "See [[T.md]]\n", "T.md": "t\n"})
    uid = await tenant(sessionmaker, a, "movenote")
    perm = current_permission.set("readwrite")
    who = tools.current_user_id.set(uid)
    try:
        await foreign_row(sessionmaker, uid, "Stuck.md")
        UNREADABLE.add("Stuck.md")
        await run(uid)
        assert (await tuples(sessionmaker, uid))["S.md"][1] is not None
        assert [t for t, _ in await links_of(sessionmaker, uid, "S.md")] == ["T.md"]

        await reassign(sessionmaker, uid, b)
        out = await tools.move_note_impl("T.md", "U.md", rewrite_links=False)
        assert "Moved" in out, out
        assert [t for t, _ in await links_of(sessionmaker, uid, "S.md")] == [
            "U.md"
        ], "the mutation under B that the marker must not carry forward"
        assert (await tuples(sessionmaker, uid))["S.md"][1] is None, (
            "move_note clears the marker of every source whose links it rewrote"
        )
        assert (await tuples(sessionmaker, uid))["U.md"][1] is None

        await reassign(sessionmaker, uid, a)
        UNREADABLE.clear()
        done = await run(uid)
        assert done.rederive == indexer.REDERIVE_RECORDED
        # S's link is what A's bytes say, resolved against A's final rows.
        t_id = await note_id(sessionmaker, uid, "T.md")
        assert await links_of(sessionmaker, uid, "S.md") == [("T.md", t_id)]
    finally:
        tools.current_user_id.reset(who)
        current_permission.reset(perm)
        vault_service.clear_user_vault_cache(user_id=uid)


# ══════════════════════════════════════════════════════════════════════════
# Moves, skips, withholding, the invariant, completion re-resolution
# ══════════════════════════════════════════════════════════════════════════


async def test_move_note_on_a_current_row_is_re_derived_next_pass(
    sessionmaker, vault
):
    root = vault / "mv"
    write(root, {"a.md": "a\n", "b.md": "b\n", "Stuck.md": "x\n"})
    uid = await tenant(sessionmaker, root, "mv")
    perm = current_permission.set("readwrite")
    who = tools.current_user_id.set(uid)
    try:
        await foreign_row(sessionmaker, uid, "Stuck.md")
        UNREADABLE.add("Stuck.md")
        await run(uid)
        out = await tools.move_note_impl("a.md", "c.md")
        assert "Moved" in out, out
        assert (await tuples(sessionmaker, uid))["c.md"][1] is None
        again = await run(uid)
        assert again.notes_indexed == 1
        assert (await tuples(sessionmaker, uid))["c.md"][1] is not None
    finally:
        tools.current_user_id.reset(who)
        current_permission.reset(perm)
        vault_service.clear_user_vault_cache(user_id=uid)


async def test_an_external_rename_is_move_paired_and_marked(sessionmaker, vault):
    root = vault / "ren"
    write(root, {"a.md": "body a\n", "src.md": "See [[a]]\n", "Stuck.md": "x\n"})
    uid = await tenant(sessionmaker, root, "ren")
    try:
        await foreign_row(sessionmaker, uid, "Stuck.md")
        UNREADABLE.add("Stuck.md")
        await run(uid)
        a_id = await note_id(sessionmaker, uid, "a.md")
        (root / "a.md").rename(root / "z.md")
        await run(uid)
        rows = await tuples(sessionmaker, uid)
        assert await note_id(sessionmaker, uid, "z.md") == a_id, "id preserved"
        assert rows["z.md"][1] is not None, "re-marked in the pass"
        # The move rewrote src.md's link `target_path`, so src.md's marker was
        # cleared; it is re-derived by the next pass.
        assert rows["src.md"][1] is None
        third = await run(uid)
        assert third.notes_indexed == 1
        assert (await tuples(sessionmaker, uid))["src.md"][1] is not None
    finally:
        vault_service.clear_user_vault_cache(user_id=uid)


async def test_a_link_skip_leaves_the_row_unmarked_and_withholds(
    sessionmaker, vault, monkeypatch
):
    root = vault / "linkskip"
    write(root, {"a.md": "See [[b]]\n", "b.md": "b\n"})
    uid = await tenant(sessionmaker, root, "linkskip")
    try:
        real = indexer._update_links_for_changed

        async def drop_a(*args, **kwargs):
            bodies = dict(kwargs.get("path_to_content") or {})
            bodies.pop("a.md", None)
            kwargs["path_to_content"] = bodies
            return await real(*args, **kwargs)

        monkeypatch.setattr(indexer, "_update_links_for_changed", drop_a)
        result = await run(uid)
        assert result.rederive == indexer.REDERIVE_INCOMPLETE
        rows = await tuples(sessionmaker, uid)
        assert rows["a.md"][1] is None
        assert rows["b.md"][1] is not None
    finally:
        vault_service.clear_user_vault_cache(user_id=uid)


async def test_a_quarantine_restart_marks_what_the_re_run_derives(
    sessionmaker, vault
):
    root = vault / "poison"
    write(root, {"a.md": "See [[b]]\n", "b.md": "b\n", "bad.md": "See [[a]]\n"})
    uid = await tenant(sessionmaker, root, "poison")
    await _poison.install(sessionmaker)
    try:
        await _poison.poison(sessionmaker, "link", "bad.md")
        result = await run(uid)
        assert result.quarantined == ("bad.md",)
        assert result.rederive == indexer.REDERIVE_RECORDED
        rows = await tuples(sessionmaker, uid)
        assert "bad.md" not in rows
        assert all(marker is not None for _x, marker in rows.values())
    finally:
        await _poison.uninstall(sessionmaker)
        indexer.clear_quarantine(uid)
        vault_service.clear_user_vault_cache(user_id=uid)


async def test_an_unlisted_directory_withholds_only_over_an_unresolved_row(
    sessionmaker, vault, monkeypatch
):
    root = vault / "walk"
    write(root, {"a.md": "a\n", "sub/b.md": "b\n", "Stuck.md": "x\n"})
    uid = await tenant(sessionmaker, root, "walk")
    try:
        await foreign_row(sessionmaker, uid, "Stuck.md")
        UNREADABLE.add("Stuck.md")
        await run(uid)  # sub/b.md marked; Stuck unresolved

        real_walk = indexer.discover_markdown_files_at

        def failing_walk(root_fd, *, skips, failed_prefixes):
            for found in real_walk(
                root_fd, skips=skips, failed_prefixes=failed_prefixes
            ):
                if found.rel.startswith("sub/"):
                    continue
                yield found
            skips.append("sub (Permission denied)")
            failed_prefixes.append("sub")

        monkeypatch.setattr(indexer, "discover_markdown_files_at", failing_walk)

        UNREADABLE.clear()  # Stuck resolves; only the unlisted dir remains
        result = await run(uid)
        assert result.walk_incomplete is True
        assert result.rederive == indexer.REDERIVE_RECORDED, (
            "every row beneath sub/ is derived under this root"
        )
        assert (await tuples(sessionmaker, uid))["sub/b.md"][1] is not None
    finally:
        vault_service.clear_user_vault_cache(user_id=uid)


async def test_an_unlisted_directory_over_a_foreign_row_withholds(
    sessionmaker, vault, monkeypatch
):
    root = vault / "walk2"
    write(root, {"a.md": "a\n", "sub/b.md": "b\n"})
    uid = await tenant(sessionmaker, root, "walk2")
    try:
        await foreign_row(sessionmaker, uid, "sub/b.md")
        real_walk = indexer.discover_markdown_files_at

        def failing_walk(root_fd, *, skips, failed_prefixes):
            for found in real_walk(
                root_fd, skips=skips, failed_prefixes=failed_prefixes
            ):
                if found.rel.startswith("sub/"):
                    continue
                yield found
            skips.append("sub (Permission denied)")
            failed_prefixes.append("sub")

        monkeypatch.setattr(indexer, "discover_markdown_files_at", failing_walk)
        result = await run(uid)
        assert result.rederive == indexer.REDERIVE_INCOMPLETE
        assert await provenance(sessionmaker, uid) is None
    finally:
        vault_service.clear_user_vault_cache(user_id=uid)


async def test_an_unmarked_row_with_no_named_skip_still_withholds(
    sessionmaker, vault, monkeypatch
):
    """D6: the invariant re-read, not the skip list, is authoritative."""
    root = vault / "inv"
    write(root, {"a.md": "a\n"})
    uid = await tenant(sessionmaker, root, "inv")
    try:
        injected = False
        orig_update = indexer._update_links_for_changed

        async def insert_then_rebuild(session, *args, **kwargs):
            nonlocal injected
            out = await orig_update(session, *args, **kwargs)
            if not injected:
                injected = True
                # A row this pass never saw, written into its transaction
                # after the upsert — what a concurrent insert looks like to
                # the READ COMMITTED re-read.
                await session.execute(
                    text(
                        "INSERT INTO notes_metadata (user_id, file_path, title, "
                        "content_hash) VALUES (:u, 'ghost.md', 'g', :h)"
                    ),
                    {"u": uid, "h": sha("ghost")},
                )
            return out

        monkeypatch.setattr(indexer, "_update_links_for_changed", insert_then_rebuild)
        result = await run(uid)
        assert result.rederive == indexer.REDERIVE_INCOMPLETE
        assert result.rederive_pending == 1
        assert await provenance(sessionmaker, uid) is None
    finally:
        vault_service.clear_user_vault_cache(user_id=uid)


async def test_completion_re_resolves_a_link_to_a_pruned_protected_row(
    sessionmaker, vault, monkeypatch
):
    """D7: N's bare `[[X]]` resolved to `old/X.md` (a foreign row protected
    beneath an unlisted directory); once that directory is listable and the
    row pruned, the recording pass resolves N's link to `new/X.md`."""
    root = vault / "reres"
    write(root, {"N.md": "See [[X]]\n", "new/X.md": "x\n"})
    (root / "old").mkdir()
    uid = await tenant(sessionmaker, root, "reres")
    try:
        await foreign_row(sessionmaker, uid, "old/X.md")
        real_walk = indexer.discover_markdown_files_at
        state = {"fail": True}

        def walk(root_fd, *, skips, failed_prefixes):
            yield from real_walk(
                root_fd, skips=skips, failed_prefixes=failed_prefixes
            )
            if state["fail"]:
                skips.append("old (Permission denied)")
                failed_prefixes.append("old")

        monkeypatch.setattr(indexer, "discover_markdown_files_at", walk)
        first = await run(uid)
        assert first.rederive == indexer.REDERIVE_INCOMPLETE
        old_id = await note_id(sessionmaker, uid, "old/X.md")
        new_id = await note_id(sessionmaker, uid, "new/X.md")
        assert await links_of(sessionmaker, uid, "N.md") == [("X", new_id)] or (
            await links_of(sessionmaker, uid, "N.md") == [("X", old_id)]
        )
        # Force the carried-forward resolution to the foreign row, as an
        # earlier pass with a different row set would have left it.
        async with sessionmaker() as session:
            await session.execute(
                text(
                    "UPDATE note_links SET target_note_id = :o WHERE source_note_id = "
                    "(SELECT id FROM notes_metadata WHERE user_id = :u AND file_path = 'N.md')"
                ),
                {"o": old_id, "u": uid},
            )
            await session.commit()

        state["fail"] = False
        done = await run(uid)
        assert done.rederive == indexer.REDERIVE_RECORDED
        assert await note_id(sessionmaker, uid, "old/X.md") is None
        assert await links_of(sessionmaker, uid, "N.md") == [("X", new_id)]
    finally:
        vault_service.clear_user_vault_cache(user_id=uid)


async def test_the_sweep_record_is_kept_by_a_re_derive_that_changed_nothing(
    sessionmaker, vault
):
    root = vault / "sweep"
    write(root, {"a.md": "a\n", "Stuck.md": "x\n"})
    uid = await tenant(sessionmaker, root, "sweep")
    try:
        await foreign_row(sessionmaker, uid, "Stuck.md")
        UNREADABLE.add("Stuck.md")
        await run(uid)
        indexer._swept[uid] = "fingerprint"
        indexer._last_full_hash[uid] = 10**12  # not a backstop pass
        again = await run(uid)
        assert again.notes_indexed == 0
        assert indexer._swept.get(uid) == "fingerprint"
    finally:
        indexer._swept.pop(uid, None)
        vault_service.clear_user_vault_cache(user_id=uid)
