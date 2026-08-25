"""#127 / D3 — a move recomputes the stem-derived title.

`notes_metadata.title` falls back to the filename stem when the frontmatter
sets none, so it is *derived from the path*. Neither move path recomputed it:
`Alpha.md → Beta.md` left the row saying `Alpha`, in `list_notes`,
`get_recent`, every graph tool and both searches — for ever, because a move
changes no content and the scan therefore never revisits the row.

Two shapes were rejected before this one, and each test below pins the
difference:

* deriving from the **row's** stored frontmatter trusts a copy that may be
  older than the file, so a `title:` added or removed since the last pass would
  be honoured after it was gone;
* a SQL `CASE` over the JSONB disagrees with `_note_title` on every *falsy*
  title — `false`, `0`, `[]`, `{}`, `""` all fall back to the stem in Python
  and do not in SQL.

So both paths go through the one shared helper, and `move_note` reads the file.
Real PostgreSQL, real tools. Skipped unless `PGVECTOR_TEST_ADMIN_URL` is set.
"""
import hashlib

import pytest
import pytest_asyncio
import yaml
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import src.database
import src.mcp_server.tools as tools
from src.config import settings
from src.mcp_server.auth import current_permission
from src.models.db import NoteMetadata
from src.services import indexer
import _harness

pytestmark = [
    _harness.requires_pgvector,
    pytest.mark.asyncio(loop_scope="module"),
]

DIM = 8


def body(front: dict | None) -> str:
    if front is None:
        return "just a body\n"
    return "---\n" + yaml.safe_dump(front, sort_keys=False) + "---\n\njust a body\n"


def content_hash(text_body: str) -> str:
    return hashlib.sha256(text_body.encode("utf-8")).hexdigest()


@pytest.fixture(scope="module")
def migrated_url():
    yield from _harness.throwaway_database("move_title_127", DIM)


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
    monkeypatch.setattr(tools.settings, "vault_path", str(root), raising=False)
    monkeypatch.setattr(indexer.settings, "vault_path", str(root), raising=False)
    monkeypatch.setattr(indexer, "async_session", sessionmaker)
    monkeypatch.setattr(tools, "async_session", sessionmaker)
    monkeypatch.setattr(src.database, "async_session", sessionmaker)
    monkeypatch.setattr(indexer, "_is_paused", lambda: False)

    async def noop(*_a, **_k):
        return None

    monkeypatch.setattr(tools, "_log_usage", noop)
    permission = current_permission.set("readwrite")

    async with sessionmaker() as session:
        await session.execute(text("DELETE FROM note_links"))
        await session.execute(text("DELETE FROM note_embeddings"))
        await session.execute(text("DELETE FROM notes_metadata"))
        await session.commit()

    try:
        yield root
    finally:
        current_permission.reset(permission)


async def index_once(root):
    await indexer.index_vault(user_id=None)


async def title_of(sessionmaker, path: str) -> str | None:
    async with sessionmaker() as session:
        return (await session.execute(
            select(NoteMetadata.title).where(NoteMetadata.file_path == path)
        )).scalar_one_or_none()


async def move_with_the_tool(root, frm, to):
    out = await tools.move_note_impl(frm, to)
    assert "Moved" in out, out


async def move_with_the_indexer(root, frm, to):
    """The id-preserving move-detection path: rename on disk, then scan."""
    (root / frm).rename(root / to)
    await indexer.index_vault(user_id=None)


MOVERS = {"move_note": move_with_the_tool, "indexer": move_with_the_indexer}


@pytest.mark.parametrize("mover", list(MOVERS))
async def test_a_rename_with_no_frontmatter_title_updates_the_title(
    sessionmaker, vault, mover
):
    (vault / "Alpha.md").write_text(body(None), encoding="utf-8")
    await index_once(vault)
    assert await title_of(sessionmaker, "Alpha.md") == "Alpha"

    await MOVERS[mover](vault, "Alpha.md", "Beta.md")

    assert await title_of(sessionmaker, "Alpha.md") is None
    assert await title_of(sessionmaker, "Beta.md") == "Beta"


@pytest.mark.parametrize("mover", list(MOVERS))
async def test_an_explicit_frontmatter_title_survives_a_move(
    sessionmaker, vault, mover
):
    (vault / "Alpha.md").write_text(body({"title": "Roadmap"}), encoding="utf-8")
    await index_once(vault)
    assert await title_of(sessionmaker, "Alpha.md") == "Roadmap"

    await MOVERS[mover](vault, "Alpha.md", "Beta.md")

    assert await title_of(sessionmaker, "Beta.md") == "Roadmap"


@pytest.mark.parametrize("mover", list(MOVERS))
@pytest.mark.parametrize(
    ("front", "why"),
    [
        ({"title": False}, "a YAML boolean false"),
        ({"title": 0}, "a YAML zero"),
        ({"title": []}, "an empty YAML list"),
        ({"title": {}}, "an empty YAML mapping"),
        ({"title": ""}, "an empty string"),
    ],
)
async def test_falsy_frontmatter_titles_agree_with_a_fresh_index(
    sessionmaker, vault, mover, front, why
):
    """Every one of these falls back to the stem in `_note_title` and would
    *not* under a SQL `CASE` over the stored JSONB — which is why the earlier
    draft was rejected. Asserted against what a fresh index actually writes,
    not against a hand-copied expectation."""
    (vault / "Alpha.md").write_text(body(front), encoding="utf-8")
    await index_once(vault)
    assert await title_of(sessionmaker, "Alpha.md") == "Alpha", why

    await MOVERS[mover](vault, "Alpha.md", "Beta.md")
    moved = await title_of(sessionmaker, "Beta.md")

    # The parity oracle: index a *fresh* copy of the same bytes at the same
    # name and compare. Nothing here restates the derivation rule.
    (vault / "Gamma.md").write_text(body(front), encoding="utf-8")
    await index_once(vault)
    fresh = await title_of(sessionmaker, "Gamma.md")
    assert moved == "Beta", (why, moved)
    assert (moved, fresh) == ("Beta", "Gamma"), (why, moved, fresh)


async def test_move_note_decides_from_the_file_not_the_stale_row(
    sessionmaker, vault
):
    """The row's frontmatter is a copy, and it can be older than the file.

    Here `title: Old` is what the last pass recorded; the file has since lost
    the key entirely. Deriving from the row would resurrect `Old` for a note
    whose frontmatter no longer has a title — the row's copy outliving the
    thing it copies.
    """
    (vault / "Alpha.md").write_text(body({"title": "Old"}), encoding="utf-8")
    await index_once(vault)
    assert await title_of(sessionmaker, "Alpha.md") == "Old"

    # Change the file without letting the scan run: the row still says `Old`.
    (vault / "Alpha.md").write_text(body(None), encoding="utf-8")

    await tools.move_note_impl("Alpha.md", "Beta.md")

    assert await title_of(sessionmaker, "Beta.md") == "Beta"


async def test_move_note_falls_back_to_the_row_when_the_file_cannot_be_read(
    sessionmaker, vault, monkeypatch
):
    """Declared best-effort. A move that has already stood must never be
    reported as failed because the title read did not work, so the fallback
    derives from the row's sanitized JSONB and self-heals at the next pass."""
    (vault / "Alpha.md").write_text(body({"title": "Kept"}), encoding="utf-8")
    await index_once(vault)

    def _boom(*_a, **_k):
        raise OSError("the read failed")

    monkeypatch.setattr(tools, "read_bytes_at", _boom)
    out = await tools.move_note_impl("Alpha.md", "Beta.md")
    monkeypatch.undo()

    assert "Moved" in out, out
    assert await title_of(sessionmaker, "Beta.md") == "Kept"


async def test_the_fallback_uses_the_new_stem_when_the_row_has_no_title(
    sessionmaker, vault, monkeypatch
):
    (vault / "Alpha.md").write_text(body(None), encoding="utf-8")
    await index_once(vault)

    def _boom(*_a, **_k):
        raise OSError("the read failed")

    monkeypatch.setattr(tools, "read_bytes_at", _boom)
    out = await tools.move_note_impl("Alpha.md", "Beta.md")
    monkeypatch.undo()

    assert "Moved" in out, out
    assert await title_of(sessionmaker, "Beta.md") == "Beta"
