"""#154 — a non-finite frontmatter number against a real `jsonb` column.

The half of this issue that cannot be shown offline. `notes_metadata.frontmatter`
is JSONB; SQLAlchemy sets no `json_serializer` on the engine, so a
`float('nan')` reaching the column is serialized by stock `json.dumps` as the
bare token `NaN`, which is not JSON and which PostgreSQL's `jsonb` parser
rejects. The batch upsert has no per-note retreat, so the failure is total:
the pass's single transaction aborts, nothing commits, no `content_hash`
advances, and every subsequent tick retries the same fatal batch — indexing
dead for the whole owner because of one note (#126's failure mode by a new
route).

**Observed, not reasoned** (task 4.1 — the design's account of today came from
reading the code; this is where it became fact). Against a real pgvector:pg16
container, inserting a row whose `frontmatter` mapping held a `float('nan')`
raised, with the serialized payload visible in the bound parameters:

    sqlalchemy.exc.DBAPIError:
      <class 'asyncpg.exceptions.InvalidTextRepresentationError'>:
      invalid input syntax for type json
      DETAIL:  Token "NaN" is invalid.
      [parameters: (None, 'p.md', 'P', [], '{"x": NaN}', 'h')]

and the surrounding transaction was left aborted, so the ordinary note batched
beside it did not commit either. The two tests in section 1 pin both halves:
the column's own rejection (bypassing the sanitiser, so it survives the fix and
stays the reason the fix exists) and the whole-pass outage, reproduced by
restoring the pre-fix sanitiser. Everything after them exercises the fixed
path.

Skipped unless `PGVECTOR_TEST_ADMIN_URL` is set (see `_harness`).
"""
import hashlib

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import src.database
import src.mcp_server.tools as tools
from src.config import settings
from src.mcp_server.auth import current_permission
from src.models.db import NoteMetadata
from src.services import indexer
from src.services import vault as vault_service
import _harness

pytestmark = [
    _harness.requires_pgvector,
    pytest.mark.asyncio(loop_scope="module"),
]

DIM = 8
NAN = float("nan")


@pytest.fixture(scope="module")
def migrated_url():
    yield from _harness.throwaway_database("non_finite_154", DIM)


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
    vault_service.clear_user_vault_cache()

    async with sessionmaker() as session:
        await session.execute(text("DELETE FROM note_links"))
        await session.execute(text("DELETE FROM note_embeddings"))
        await session.execute(text("DELETE FROM notes_metadata"))
        await session.commit()

    try:
        yield root
    finally:
        current_permission.reset(permission)


def write(root, name: str, body: str) -> None:
    (root / name).write_text(body, encoding="utf-8")


async def row_for(sessionmaker, path: str):
    async with sessionmaker() as session:
        return (await session.execute(
            select(NoteMetadata).where(NoteMetadata.file_path == path)
        )).scalar_one_or_none()


# ══════════════════════════════════════════════════════════════════════════
# 1. today's failure, pinned against the live column
# ══════════════════════════════════════════════════════════════════════════


async def test_the_column_still_rejects_a_raw_non_finite_float(sessionmaker, vault):
    """One un-sanitised note aborts the transaction, taking the batch with it.

    Deliberately bypasses `_sanitize_frontmatter` — this is the mechanism, not
    the code path, so it must keep failing after the fix. What it shows is why
    the fix cannot be "retry the batch": the ordinary row inserted first in the
    same transaction is gone too, because the transaction itself is aborted.
    """
    ordinary = {
        "user_id": None,
        "file_path": "ordinary.md",
        "title": "Ordinary",
        "tags": [],
        "frontmatter": {"x": 1},
        "content_hash": "h1",
    }
    poisoned = dict(ordinary, file_path="poisoned.md", frontmatter={"x": NAN})

    async with sessionmaker() as session:
        await session.execute(NoteMetadata.__table__.insert().values(**ordinary))
        with pytest.raises(DBAPIError) as caught:
            await session.execute(NoteMetadata.__table__.insert().values(**poisoned))
        assert "NaN" in str(caught.value) or "json" in str(caught.value).lower()
        await session.rollback()

    # Nothing committed — not the poisoned note, and not the good one beside it.
    assert await row_for(sessionmaker, "poisoned.md") is None
    assert await row_for(sessionmaker, "ordinary.md") is None


def _legacy_sanitize(v):
    """`_sanitize_value` exactly as it stood before this change.

    Kept here, in the test, so the failure it caused stays reproducible without
    keeping the broken code in the module it was removed from.
    """
    if isinstance(v, (str, int, float, bool, type(None))):
        return v
    if isinstance(v, list):
        return [_legacy_sanitize(i) for i in v]
    if isinstance(v, dict):
        return {
            (k if isinstance(k, str) else str(k)): _legacy_sanitize(val)
            for k, val in v.items()
        }
    return str(v)


async def test_the_pre_fix_sanitiser_takes_the_whole_pass_down(
    sessionmaker, vault, monkeypatch
):
    """The end-to-end failure, reproduced by restoring the old sanitiser.

    One note carrying `x: .nan`, one ordinary note beside it. With the pre-fix
    pass-through in place the pass raises out of the batch upsert and **neither**
    note's row exists afterwards — the ordinary note is collateral, which is
    what makes this an owner-wide outage rather than one bad note.
    """
    monkeypatch.setattr(indexer, "_sanitize_frontmatter", _legacy_sanitize)
    write(vault, "legacy_nan.md", "---\nx: .nan\n---\nbody\n")
    write(vault, "legacy_plain.md", "---\nx: 1\n---\nbody\n")

    with pytest.raises(Exception) as caught:
        await indexer.index_vault(user_id=None)
    assert "NaN" in str(caught.value) or "json" in str(caught.value).lower()

    assert await row_for(sessionmaker, "legacy_nan.md") is None
    assert await row_for(sessionmaker, "legacy_plain.md") is None


# ══════════════════════════════════════════════════════════════════════════
# 2. the fixed path
# ══════════════════════════════════════════════════════════════════════════


async def test_a_note_with_non_finite_numbers_indexes_with_the_batch(
    sessionmaker, vault
):
    write(vault, "nan.md", "---\nx: .nan\na: .inf\nb: -.inf\n---\nfirst body\n")
    write(vault, "plain.md", "---\nx: 1\n---\nplain body\n")

    await indexer.index_vault(user_id=None)

    poisoned = await row_for(sessionmaker, "nan.md")
    assert poisoned is not None
    assert poisoned.frontmatter == {"x": ".nan", "a": ".inf", "b": "-.inf"}
    # Every other note in the same batch committed.
    assert (await row_for(sessionmaker, "plain.md")) is not None


async def test_one_such_note_cannot_wedge_the_index(sessionmaker, vault):
    """The body changes between two passes and the hash advances."""
    write(vault, "wedge.md", "---\na: .inf\nb: -.inf\n---\nbody one\n")
    await indexer.index_vault(user_id=None)
    first = await row_for(sessionmaker, "wedge.md")
    assert first is not None
    assert first.content_hash == hashlib.sha256(
        "---\na: .inf\nb: -.inf\n---\nbody one\n".encode()
    ).hexdigest()

    edited = "---\na: .inf\nb: -.inf\n---\nbody two\n"
    write(vault, "wedge.md", edited)
    await indexer.index_vault(user_id=None)
    second = await row_for(sessionmaker, "wedge.md")
    assert second.content_hash == hashlib.sha256(edited.encode()).hexdigest()
    assert second.content_hash != first.content_hash


async def test_alternate_spellings_are_stored_canonically(sessionmaker, vault):
    write(vault, "spelling.md", "---\nx: .NaN\ny: +.inf\nz: -.Inf\n---\nbody\n")
    await indexer.index_vault(user_id=None)

    row = await row_for(sessionmaker, "spelling.md")
    assert row.frontmatter == {"x": ".nan", "y": ".inf", "z": "-.inf"}


async def test_a_non_finite_mapping_key_stores_with_the_first_key_winning(
    sessionmaker, vault
):
    write(vault, "keys.md", '---\n.nan: 1\n".nan": 2\n---\nbody\n')
    await indexer.index_vault(user_id=None)

    row = await row_for(sessionmaker, "keys.md")
    assert row.frontmatter == {".nan": 1}


async def test_keyword_search_matches_the_stored_token(sessionmaker, vault):
    write(vault, "searchable.md", "---\nx: .nan\n---\nfindable body text\n")
    await indexer.index_vault(user_id=None)

    out = await tools.search_notes_impl("findable", frontmatter={"x": ".nan"})
    assert "searchable.md" in out, out


# ══════════════════════════════════════════════════════════════════════════
# 3. one title, three surfaces (design D10b)
# ══════════════════════════════════════════════════════════════════════════
#
# The stored `notes_metadata.title` is the indexed surface; `read_note`'s
# `title` and the control panel's note view are both `vault.read_file()`'s.

TITLE_NOTES = {
    "t_nan.md": ("title: .nan\n", ".nan"),
    "t_inf.md": ("title: .inf\n", ".inf"),
    "t_date_in_list.md": ("title: [2026-08-25]\n", "['2026-08-25']"),
    "t_int_key.md": ("title:\n  1: a\n", "{'1': 'a'}"),
    "t_long.md": ("title: " + "t" * 600 + "\n", "t" * 512),
    "t_number.md": ("title: 5\n", "5"),
    "t_date.md": ("title: 2026-08-25\n", "2026-08-25"),
    "t_list.md": ("title: [a, b]\n", "['a', 'b']"),
    "t_falsy_zero.md": ("title: 0\n", "t_falsy_zero"),
    "t_falsy_false.md": ("title: false\n", "t_falsy_false"),
    "t_falsy_empty.md": ("title: ''\n", "t_falsy_empty"),
    "t_falsy_list.md": ("title: []\n", "t_falsy_list"),
}


async def test_every_surface_agrees_on_what_a_note_is_called(sessionmaker, vault):
    for name, (block, _) in TITLE_NOTES.items():
        write(vault, name, f"---\n{block}---\nbody\n")
    await indexer.index_vault(user_id=None)

    for name, (_, expected) in TITLE_NOTES.items():
        row = await row_for(sessionmaker, name)
        assert row is not None, name
        indexed = row.title
        # `read_note`'s title and the panel's are the same call.
        panel = vault_service.read_file(name)["title"]
        read = (await tools.read_note_impl(name)).title
        assert indexed == expected, (name, indexed)
        assert panel == expected, (name, panel)
        assert read == expected, (name, read)
