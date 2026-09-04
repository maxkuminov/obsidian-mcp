"""A capped link extraction is durable and visible to an agent — #203.

The unit tests prove the rebuild's write traffic is bounded and that it sets
the marker. What needs a real database is the other half: that migration 022's
column exists with the shape the indexer and `get_links` assume, that a full
`index_vault` pass writes the marker through the production upsert path, and
that `get_links` then tells the caller the set is incomplete. A capped set is
exactly `MAX_LINKS_PER_NOTE` rows and is indistinguishable from a complete one
at the query, so if this chain breaks anywhere an agent reads a truncated
graph as the whole graph and acts on it — the silently-wrong-answer failure
this server ranks above almost everything else.

The cap is monkeypatched down to a handful of links. The production value of
10,000 would make the fixture enormous and would test nothing the small value
does not: the cap is a parameter, and every branch here is on the same side of
it either way.

Skipped unless `PGVECTOR_TEST_ADMIN_URL` is set — see `_harness.py`.
"""
import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import src.database
import src.mcp_server.tools as tools
from src.config import settings
from src.models.db import NoteLink, NoteMetadata
from src.services import embeddings as embeddings_service
from src.services import indexer
import _harness

pytestmark = [
    _harness.requires_pgvector,
    pytest.mark.asyncio(loop_scope="module"),
]

DIM = 8

CAP = 5


def over_cap_body(pairs: int = 20) -> str:
    """Both link kinds interleaved, so "the first N in document order" is
    distinguishable from "every wikilink, then markdown links"."""
    parts = []
    for i in range(pairs):
        parts.append(f"[[W{i}]]")
        parts.append(f"[t](m{i}.md)")
    return "# MOC\n\n" + " ".join(parts) + "\n"


UNDER_CAP_BODY = "# MOC\n\nJust [[W0]] and [t](m0.md).\n"


@pytest.fixture(scope="module")
def migrated_url():
    yield from _harness.throwaway_database("links_truncated_203", DIM)


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def sessionmaker(migrated_url):
    engine = create_async_engine(migrated_url, poolclass=None)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest_asyncio.fixture(loop_scope="module")
async def vault(sessionmaker, monkeypatch, tmp_path):
    root = tmp_path / "vault"
    root.mkdir(parents=True)

    monkeypatch.setattr(settings, "vault_path", str(root), raising=False)
    monkeypatch.setattr(indexer.settings, "vault_path", str(root), raising=False)
    monkeypatch.setattr(tools.settings, "vault_path", str(root), raising=False)
    monkeypatch.setattr(indexer, "async_session", sessionmaker)
    monkeypatch.setattr(tools, "async_session", sessionmaker)
    monkeypatch.setattr(src.database, "async_session", sessionmaker)
    monkeypatch.setattr(embeddings_service, "async_session", sessionmaker, raising=False)
    monkeypatch.setattr(indexer, "_is_paused", lambda: False)
    monkeypatch.setattr(indexer, "MAX_LINKS_PER_NOTE", CAP)

    async def noop(*_a, **_k):
        return None

    monkeypatch.setattr(tools, "_log_usage", noop)

    async with sessionmaker() as session:
        await session.execute(text("DELETE FROM note_links"))
        await session.execute(text("DELETE FROM note_embeddings"))
        await session.execute(text("DELETE FROM notes_metadata"))
        await session.execute(text("DELETE FROM users"))
        await session.commit()

    yield root


async def note_row(sessionmaker, path):
    async with sessionmaker() as session:
        result = await session.execute(
            select(
                NoteMetadata.id,
                NoteMetadata.links_truncated,
                NoteMetadata.extraction_version,
            ).where(NoteMetadata.file_path == path)
        )
        return result.one()


async def link_count(sessionmaker, note_id):
    async with sessionmaker() as session:
        return await session.scalar(
            select(func.count(NoteLink.id)).where(NoteLink.source_note_id == note_id)
        )


async def link_targets(sessionmaker, note_id):
    async with sessionmaker() as session:
        result = await session.execute(
            select(NoteLink.target_path)
            .where(NoteLink.source_note_id == note_id)
            .order_by(NoteLink.position)
        )
        return [r[0] for r in result.all()]


# ── the column migration 022 promises ─────────────────────────────────────


async def test_the_marker_column_exists_with_the_shape_the_indexer_assumes(
    sessionmaker, vault
):
    """Not a tautology about the ORM: this reads the catalogue on a database
    built by running the migrations. A nullable column would let `get_links`
    read NULL as "complete" for a note whose links are truncated, and a
    default of true would invent a truncation on every pre-existing row."""
    async with sessionmaker() as session:
        row = (
            await session.execute(
                text(
                    "SELECT a.attnotnull, "
                    "       format_type(a.atttypid, a.atttypmod) AS coltype, "
                    "       pg_get_expr(d.adbin, d.adrelid) AS coldefault "
                    "FROM pg_attribute a "
                    "LEFT JOIN pg_attrdef d "
                    "  ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
                    "WHERE a.attrelid = 'notes_metadata'::regclass "
                    "  AND a.attname = 'links_truncated' "
                    "  AND a.attnum > 0 AND NOT a.attisdropped"
                )
            )
        ).first()

    assert row is not None, "migration 022 did not create links_truncated"
    attnotnull, coltype, coldefault = row
    assert attnotnull is True
    assert coltype == "boolean"
    assert coldefault == "false"


# ── the pass writes it, and get_links reads it ────────────────────────────


async def test_a_capped_note_is_marked_and_get_links_reports_it(sessionmaker, vault):
    (vault / "MOC.md").write_text(over_cap_body(), encoding="utf-8")

    await indexer.index_vault(user_id=None)

    row = await note_row(sessionmaker, "MOC.md")
    assert row.links_truncated is True, "the pass must record the truncation"
    assert await link_count(sessionmaker, row.id) == CAP, (
        "exactly the cap, persisted by the real pass"
    )
    # Document order across both link kinds, not "every wikilink first".
    assert await link_targets(sessionmaker, row.id) == ["W0", "m0", "W1", "m1", "W2"]

    out = await tools.get_links_impl("MOC.md")
    assert "truncated: true" in out, out
    assert "INCOMPLETE" in out, (
        "the header field alone is easy to skim past in a long link list"
    )
    assert str(CAP) in out


async def test_an_ordinary_note_reports_truncated_false(sessionmaker, vault):
    (vault / "Small.md").write_text(UNDER_CAP_BODY, encoding="utf-8")

    await indexer.index_vault(user_id=None)

    row = await note_row(sessionmaker, "Small.md")
    assert row.links_truncated is False

    out = await tools.get_links_impl("Small.md")
    assert "truncated: false" in out, out
    assert "INCOMPLETE" not in out, out


async def test_editing_the_note_back_under_the_cap_clears_the_marker(
    sessionmaker, vault
):
    """The marker is derived state and has to track the note. A note edited
    down to two links must stop claiming an incomplete link set, or the
    warning becomes noise an agent learns to ignore."""
    path = vault / "Shrinking.md"
    path.write_text(over_cap_body(), encoding="utf-8")
    await indexer.index_vault(user_id=None)

    before = await note_row(sessionmaker, "Shrinking.md")
    assert before.links_truncated is True
    assert "truncated: true" in await tools.get_links_impl("Shrinking.md")

    path.write_text(UNDER_CAP_BODY, encoding="utf-8")
    await indexer.index_vault(user_id=None)

    after = await note_row(sessionmaker, "Shrinking.md")
    assert after.id == before.id, "the row survived — this is an edit, not a move"
    assert after.links_truncated is False, "the marker must be cleared"
    assert await link_count(sessionmaker, after.id) == 2

    out = await tools.get_links_impl("Shrinking.md")
    assert "truncated: false" in out, out
    assert "INCOMPLETE" not in out, out


async def test_the_pass_stamps_the_current_extraction_version(sessionmaker, vault):
    """The bump is what makes the next pass re-derive every note's links under
    the new grammar; a note the pass touched must carry it, or the re-derive
    repeats for ever."""
    (vault / "Stamped.md").write_text("# S\n\n[[A]]\n", encoding="utf-8")

    await indexer.index_vault(user_id=None)

    row = await note_row(sessionmaker, "Stamped.md")
    assert row.extraction_version == indexer.CURRENT_EXTRACTION_VERSION == 2
