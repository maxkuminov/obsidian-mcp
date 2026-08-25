"""A refused cross-mount move changes no database row (round 1, MINOR).

The offline proof of this is a session spy — it asserts that no `Update`,
`Insert` or `Delete` reached the session and that nothing was committed
(`tests/test_nested_mount_publication.py`). Both reviewers independently made
the same point about it: a spy tests the statements the tool *issues*, and the
claim the spec makes is about the state of two tables. Those are the same thing
only for as long as nothing else can write — an ORM flush on a dirty instance,
a cascade, a future commit-on-exit — so the claim deserves to be measured where
it is made.

So this module drives the real `move_note` against a real PostgreSQL and
compares full row snapshots of `notes_metadata` and `note_links` taken either
side of the refusal, in both the ordinary and the `rewrite_links=True` shape.
The spy tests stay: they are what pins *how* the refusal avoids writing (no DML
at all, rather than a write that happened to be a no-op), and they run on every
machine. This is what pins the outcome.

**The mount boundary is stubbed here, deliberately.** A real one needs a mount
namespace, which `tests/_nested_mount_cases.py` already provides and which
cannot reach a database — the two halves of the claim are unreachable from one
another, which is why they are asserted separately. This module's job is the
database half, so the cheapest honest way to reach the refusal is to make the
preflight's comparison answer "different mounts" for the pair that involves
`M/`; the branch it selects, the error it raises and the tool's handling of it
are all production code from there on.

Skipped unless `PGVECTOR_TEST_ADMIN_URL` is set — see `_harness.py`.
"""
import hashlib
import os

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import src.database
import src.mcp_server.tools as tools
from src.mcp_server.auth import current_permission
from src.models.db import NoteLink, NoteMetadata
from src.services import vault_fs
import _harness
from src.config import settings

pytestmark = [
    _harness.requires_pgvector,
    pytest.mark.asyncio(loop_scope="module"),
]

DIM = int(settings.embedding_dimensions)

MOVED = "M/a.md"
BACKLINK = "b.md"
BACKLINK_BODY = "see [[M/a]] for more\n"


def content_hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


@pytest.fixture(scope="module")
def migrated_url():
    yield from _harness.throwaway_database("cross_mount_move", DIM)


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def sessionmaker(migrated_url):
    engine = create_async_engine(migrated_url, poolclass=None)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest_asyncio.fixture(loop_scope="module")
async def vault(sessionmaker, monkeypatch, tmp_path):
    """A single-user vault with `M/` in it, wired to the throwaway database.

    Single-user (`user_id=None`) keeps the tools out of the multi-user
    admission machinery: what is under test is which rows a refusal leaves
    alone, not who is asking.
    """
    root = tmp_path / "vault"
    (root / "M").mkdir(parents=True)
    (root / "Archive").mkdir()

    monkeypatch.setattr(settings, "vault_path", str(root), raising=False)
    monkeypatch.setattr(tools.settings, "vault_path", str(root), raising=False)
    monkeypatch.setattr(tools, "async_session", sessionmaker)
    monkeypatch.setattr(src.database, "async_session", sessionmaker)

    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(tools, "_log_usage", noop)
    permission = current_permission.set("readwrite")
    vault_fs.reset_filesystem_probe_cache()
    try:
        yield root
    finally:
        current_permission.reset(permission)
        vault_fs.reset_filesystem_probe_cache()


def refuse_moves_involving_the_mount(monkeypatch, marker: str = "/M") -> None:
    """Answer the move preflight as a mount boundary for the `M/` pair only.

    Exactly one end below `M/` is a boundary; two ends on the same side are
    not. Making it unconditional would refuse the control move too, and the
    control is what proves the snapshot comparison can see a change at all.
    """

    def fake(fd_a: int, fd_b: int) -> bool:
        sides = [
            os.readlink(f"/proc/self/fd/{fd}").endswith(marker)
            for fd in (fd_a, fd_b)
        ]
        return sides[0] != sides[1]

    monkeypatch.setattr(vault_fs, "cross_mount_definitely", fake)


async def seed(sessionmaker, root):
    """The moved note, a backlink source that really links to it, and the row.

    The `note_links` row matters as much as the two `notes_metadata` ones: the
    refused move's second statement is an `UPDATE note_links SET target_path`,
    and a snapshot that only covered notes would not see it run.
    """
    (root / MOVED).write_text("body\n", encoding="utf-8")
    (root / BACKLINK).write_text(BACKLINK_BODY, encoding="utf-8")
    async with sessionmaker() as session:
        await session.execute(text("DELETE FROM note_links"))
        await session.execute(text("DELETE FROM notes_metadata"))
        moved = NoteMetadata(
            file_path=MOVED,
            title="A",
            content_hash=content_hash("body\n"),
            embedded_content_hash=content_hash("body\n"),
            user_id=None,
        )
        source = NoteMetadata(
            file_path=BACKLINK,
            title="B",
            content_hash=content_hash(BACKLINK_BODY),
            embedded_content_hash=content_hash(BACKLINK_BODY),
            user_id=None,
        )
        session.add_all([moved, source])
        await session.flush()
        session.add(
            NoteLink(
                source_note_id=source.id,
                target_note_id=moved.id,
                target_path=MOVED,
                link_text="M/a",
                kind="link",
            )
        )
        await session.commit()
        return moved.id, source.id


async def snapshot(sessionmaker) -> tuple[list, list]:
    """Every column of both tables that a move can change, in a fixed order."""
    async with sessionmaker() as session:
        notes = (
            await session.execute(
                select(
                    NoteMetadata.id,
                    NoteMetadata.file_path,
                    NoteMetadata.title,
                    NoteMetadata.content_hash,
                    NoteMetadata.embedded_content_hash,
                    NoteMetadata.user_id,
                ).order_by(NoteMetadata.id)
            )
        ).all()
        links = (
            await session.execute(
                select(
                    NoteLink.id,
                    NoteLink.source_note_id,
                    NoteLink.target_note_id,
                    NoteLink.target_path,
                    NoteLink.link_text,
                    NoteLink.kind,
                ).order_by(NoteLink.id)
            )
        ).all()
    return [tuple(r) for r in notes], [tuple(r) for r in links]


@pytest.mark.parametrize("rewrite_links", [False, True])
async def test_a_refused_cross_mount_move_leaves_both_tables_identical(
    sessionmaker, vault, monkeypatch, rewrite_links
):
    """The claim as the spec states it: not "no DML was issued" but "the rows
    are the same rows". Under `rewrite_links=True` the tool has additionally
    read the vault index, planned a backlink rewrite and pinned its descriptor
    before the refusal — the expensive path, and the one where a write that
    escaped would be hardest to notice."""
    await seed(sessionmaker, vault)
    before_notes, before_links = await snapshot(sessionmaker)
    backlink_bytes = (vault / BACKLINK).read_bytes()
    refuse_moves_involving_the_mount(monkeypatch)

    result = await tools.move_note_impl(MOVED, "a.md", rewrite_links=rewrite_links)

    assert "different mounts" in result, result
    after_notes, after_links = await snapshot(sessionmaker)
    assert after_notes == before_notes
    assert after_links == before_links
    # And the vault, which is the other half of "nothing happened".
    assert (vault / MOVED).read_text(encoding="utf-8") == "body\n"
    assert not (vault / "a.md").exists()
    assert (vault / BACKLINK).read_bytes() == backlink_bytes


async def test_the_snapshot_comparison_can_see_a_move_that_is_allowed(
    sessionmaker, vault, monkeypatch
):
    """Guard the guard. Two identical snapshots prove nothing unless the same
    comparison, on the same tables, changes when a move is permitted to run —
    otherwise a fixture that quietly never reached the database would pass the
    pair above."""
    await seed(sessionmaker, vault)
    before_notes, before_links = await snapshot(sessionmaker)
    refuse_moves_involving_the_mount(monkeypatch)

    # Both ends outside `M/`, so the stubbed comparison answers "same mount"
    # and the move proceeds exactly as it does in production.
    result = await tools.move_note_impl(BACKLINK, "Archive/b.md")

    assert "different mounts" not in result, result
    after_notes, after_links = await snapshot(sessionmaker)
    assert after_notes != before_notes
    assert sorted(r[1] for r in after_notes) == sorted([MOVED, "Archive/b.md"])
    assert after_links == before_links, (
        "the moved note is the link *source* here, so only notes_metadata moves"
    )
