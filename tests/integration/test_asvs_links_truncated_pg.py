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


# ── get_links is bounded, and says what it is bounding ────────────────────


async def test_get_links_pages_a_capped_note_and_names_the_persisted_total(
    sessionmaker, vault
):
    """`get_links` selected every row a note had. For a note the indexer
    capped, that is up to `MAX_LINKS_PER_NOTE` (10,000 in production) rows
    rendered into a single tool result — the payload the read caps exist to
    prevent, on the one tool guaranteed to meet the biggest note in the vault.

    The truncation notice quotes the **persisted** row count, not `len(rows)`:
    the scoped join omits any row that resolved to a note outside the owned
    set, so a number taken from the page would be neither the page size nor
    the total and would silently understate how much the caller has not seen.
    """
    (vault / "MOC.md").write_text(over_cap_body(), encoding="utf-8")
    await indexer.index_vault(user_id=None)

    row = await note_row(sessionmaker, "MOC.md")
    persisted = await link_count(sessionmaker, row.id)
    assert persisted == CAP

    out = await tools.get_links_impl("MOC.md", limit=2)

    # Two link bullets, not five.
    assert out.count("\n- ") == 2, out
    assert f"showing 2 of {persisted:,} link rows" in out, out
    assert "limit=2" in out
    # The extraction-cap notice quotes the persisted total, not the page.
    assert f"The {persisted:,} rows persisted for it" in out, out
    assert "INCOMPLETE" in out


async def test_get_links_says_nothing_about_paging_when_the_page_holds_it_all(
    sessionmaker, vault
):
    (vault / "Small.md").write_text(UNDER_CAP_BODY, encoding="utf-8")
    await indexer.index_vault(user_id=None)

    out = await tools.get_links_impl("Small.md")

    assert "showing" not in out, out
    assert "truncated: false" in out


@pytest.mark.parametrize("asked,shown", [(0, 1), (-5, 1), (99999, CAP)])
async def test_get_links_clamps_its_limit_like_get_backlinks(
    sessionmaker, vault, asked, shown
):
    """Same clamp as `get_backlinks`: `max(1, min(limit, 500))`. A zero or a
    negative must not turn into "no rows" or into an unbounded select."""
    (vault / "MOC.md").write_text(over_cap_body(), encoding="utf-8")
    await indexer.index_vault(user_id=None)

    out = await tools.get_links_impl("MOC.md", limit=asked)

    assert out.count("\n- ") == shown, out


# ── a capped note does not withhold the re-derive's certification ─────────


async def test_a_capped_note_still_lets_the_re_derive_record_its_provenance(
    sessionmaker, vault, monkeypatch
):
    """D4's load-bearing carve-out, end to end against the real pass.

    A.7a withholds a re-derive's provenance stamp when the pass skipped any
    discovered path — the re-derive's whole claim is that every surviving row
    was written by it. A truncated note is deliberately **not** a skip: the
    truncation is deterministic and the rows written are exactly the rows
    derived, so the structural claim still holds. Treating it as one would
    park a tenant with a single generated MOC in re-derive mode for ever, with
    no repair that could ever end it — a self-inflicted DoS on the
    index-integrity machinery, which is precisely the failure this carve-out
    exists to prevent.

    The unit tests assert the carve-out at the call site. This runs
    `index_vault` for a real user with no recorded provenance (which
    classifies as `provenance_unresolved` → re-derive) over a vault whose only
    note is over the cap, and reads `users.indexed_vault_*` afterwards.
    """
    from src.models.db import User
    from src.services import vault as vault_service
    from src.services.transfer import canonical_vault_root

    root = vault / "tenant"
    root.mkdir()
    (root / "MOC.md").write_text(over_cap_body(), encoding="utf-8")

    async with sessionmaker() as session:
        user = User(username="capped", password_hash="x", vault_path=str(root))
        session.add(user)
        await session.commit()
        uid = user.id
        # The indexer resolves the root through the process cache, exactly as
        # the production pass does after its own bulk warm.
        await vault_service.warm_user_vault_cache(session, user_id=uid)

    try:
        async with sessionmaker() as session:
            before = (
                await session.execute(
                    select(
                        User.indexed_vault_assignment,
                        User.indexed_vault_realpath,
                    ).where(User.id == uid)
                )
            ).one()
        assert before == (None, None), "no provenance yet — this is a re-derive"

        await indexer.index_vault(user_id=uid)

        async with sessionmaker() as session:
            after = (
                await session.execute(
                    select(
                        User.indexed_vault_assignment,
                        User.indexed_vault_realpath,
                    ).where(User.id == uid)
                )
            ).one()
            capped = (
                await session.execute(
                    select(NoteMetadata.links_truncated).where(
                        NoteMetadata.user_id == uid,
                        NoteMetadata.file_path == "MOC.md",
                    )
                )
            ).scalar_one()

        assert capped is True, "the fixture must actually exercise the cap"
        assert after.indexed_vault_assignment == canonical_vault_root(root), (
            "the capped note withheld the re-derive's certification"
        )
        assert after.indexed_vault_realpath is not None
    finally:
        vault_service.clear_user_vault_cache(user_id=uid)
