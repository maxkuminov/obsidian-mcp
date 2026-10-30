"""#308 — NUL-free derivation, not-indexable paths and the re-derive stamp,
against a real PostgreSQL (design D1, D2, D7, D8).

What only a real database can show: `text` rejecting U+0000 is the whole of
#308, `jsonb` rejecting `\\u0000` is the frontmatter half, and an unencodable
bind parameter is asyncpg's client-side refusal. Each test runs the real
`index_vault` (and, where it matters, `embed_vault` and the two search
functions) over a `tmp_path` vault.

Skipped unless `PGVECTOR_TEST_ADMIN_URL` is set (see `_harness`).
"""
import errno
import hashlib
import os

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import src.database
from src.config import settings
from src.models.db import NoteEmbedding, NoteLink, NoteMetadata, User
from src.services import embeddings as embeddings_service
from src.services import indexer
from src.services import vault as vault_service
from src.services.search import full_text_search
from src.services.transfer import canonical_vault_root
from src.services.vault import MAX_PATH_CHARS
import _harness

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
    yield from _harness.throwaway_database("nul_derivation_308", DIM)


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
        await session.execute(text("DELETE FROM users"))
        await session.commit()

    yield root
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


def unreadable(monkeypatch, *names: str) -> None:
    """`read_note_at` raising EACCES for `names` — bytes not obtained."""
    real = indexer.read_note_at

    def failing(parent_fd, name, rel=None):
        if name in names:
            raise PermissionError(errno.EACCES, "Permission denied", name)
        return real(parent_fd, name, rel)

    monkeypatch.setattr(indexer, "read_note_at", failing)


# ══════════════════════════════════════════════════════════════════════════
# D1 — NUL in the body
# ══════════════════════════════════════════════════════════════════════════


async def test_the_308_repro_indexes_is_searchable_and_the_next_tick_writes_nothing(
    sessionmaker, vault
):
    (vault / "n.md").write_bytes(b"hello\x00world\n")
    (vault / "other.md").write_text("ordinary note\n", encoding="utf-8")

    first = await indexer.index_vault()
    assert first.notes_indexed == 2

    stored = await row(sessionmaker, "n.md")
    assert stored.content_hash == sha("helloworld\n")
    assert await keyword(sessionmaker, "helloworld") == ["n.md"]
    assert (vault / "n.md").read_bytes() == b"hello\x00world\n", "file untouched"

    # The next tick, unchanged vault — full-hash or not, nothing is rewritten.
    indexed_at = stored.indexed_at
    indexer._last_full_hash.clear()  # force a full-hash pass: it still re-reads
    second = await indexer.index_vault()
    assert second.notes_indexed == 0
    assert (await row(sessionmaker, "n.md")).indexed_at == indexed_at
    third = await indexer.index_vault()
    assert third.notes_indexed == 0


async def test_a_nul_note_embeds_and_certifies(sessionmaker, vault):
    (vault / "n.md").write_bytes(b"some\x00 words\x00 for a chunk\n")
    await indexer.index_vault()
    await indexer.embed_vault(user_id=None)

    stored = await row(sessionmaker, "n.md")
    assert stored.embedded_content_hash == stored.content_hash == sha(
        "some words for a chunk\n"
    )
    async with sessionmaker() as session:
        chunks = (
            await session.execute(
                select(NoteEmbedding.chunk_text).where(
                    NoteEmbedding.note_id == stored.id
                )
            )
        ).scalars().all()
    assert chunks and all("\x00" not in c for c in chunks)
    assert "n.md" in await semantic(sessionmaker, "words")


# ══════════════════════════════════════════════════════════════════════════
# D2 — frontmatter, tags, links
# ══════════════════════════════════════════════════════════════════════════


async def test_a_yaml_escaped_nul_in_title_tags_and_key_indexes(sessionmaker, vault):
    (vault / "y.md").write_text(
        '---\ntitle: "a\\0b"\ntags: ["x\\0y"]\n"k\\0": 1\n---\nbody\n',
        encoding="utf-8",
    )
    await indexer.index_vault()
    stored = await row(sessionmaker, "y.md")
    assert stored.title == "ab"
    assert "xy" in stored.tags
    assert stored.frontmatter.get("k") == 1
    assert stored.frontmatter.get("title") == "ab"


async def test_an_over_long_tag_is_dropped_and_the_note_indexes(sessionmaker, vault):
    big = "t" * 3000
    (vault / "t.md").write_text(
        f"---\ntags: [{big}, ordinary]\n---\nbody\n", encoding="utf-8"
    )
    await indexer.index_vault()
    stored = await row(sessionmaker, "t.md")
    assert stored is not None
    assert stored.tags == ["ordinary"]


async def test_a_percent_encoded_nul_does_not_invent_a_link(sessionmaker, vault):
    (vault / "badtarget.md").write_text("target\n", encoding="utf-8")
    (vault / "src.md").write_text(
        "[x](bad%00target.md) and [[badtarget]]\n", encoding="utf-8"
    )
    await indexer.index_vault()

    source = await row(sessionmaker, "src.md")
    target = await row(sessionmaker, "badtarget.md")
    async with sessionmaker() as session:
        links = (
            await session.execute(
                select(NoteLink.target_path, NoteLink.link_text).where(
                    NoteLink.source_note_id == source.id
                )
            )
        ).all()
        backlinks = (
            await session.execute(
                select(func.count(NoteLink.id)).where(
                    NoteLink.target_note_id == target.id
                )
            )
        ).scalar_one()
    # Only the wikilink: the `%00` href produced no row at all.
    assert [link.target_path for link in links] == ["badtarget"]
    assert all("%00" not in link.link_text for link in links)
    assert backlinks == 1


# ══════════════════════════════════════════════════════════════════════════
# D7 — present but not indexable
# ══════════════════════════════════════════════════════════════════════════


def latin1_file(root, body: bytes = b"latin-1 name\n") -> str:
    raw = os.path.join(os.fsencode(str(root)), b"caf\xe9.md")
    try:
        with open(raw, "wb") as fh:
            fh.write(body)
    except OSError as e:  # pragma: no cover - filesystem-dependent
        pytest.skip(f"this filesystem refuses a non-UTF-8 filename: {e}")
    return os.fsdecode(b"caf\xe9.md")


async def test_a_latin1_filename_is_skipped_and_the_rest_commits(sessionmaker, vault):
    for i in range(20):
        (vault / f"note{i}.md").write_text(f"note {i}\n", encoding="utf-8")
    latin1_file(vault)

    first = await indexer.index_vault()
    assert first.notes_indexed == 20
    assert first.not_indexable == 1
    assert await paths(sessionmaker) == {f"note{i}.md" for i in range(20)}

    again = await indexer.index_vault()
    assert again.notes_indexed == 0, "the next tick repeats no write"


async def test_an_over_long_path_is_skipped_and_the_rest_commits(sessionmaker, vault):
    parts = ["d" * 250] * 4 + ["n" * 30 + ".md"]
    directory = vault.joinpath(*parts[:-1])
    directory.mkdir(parents=True)
    (directory / parts[-1]).write_text("deep\n", encoding="utf-8")
    assert len("/".join(parts)) > MAX_PATH_CHARS
    (vault / "short.md").write_text("short\n", encoding="utf-8")

    result = await indexer.index_vault()
    assert result.notes_indexed == 1
    assert await paths(sessionmaker) == {"short.md"}


async def test_an_indexed_note_rewritten_as_latin1_is_removed_not_served_stale(
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

    (vault / "victim.md").write_bytes(b"caf\xe9 zanzibarite latin-1\n")
    await indexer.index_vault()

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
    assert "victim.md" not in await semantic(sessionmaker, "zanzibarite")
    assert (vault / "victim.md").read_bytes() == b"caf\xe9 zanzibarite latin-1\n"


async def test_an_unreadable_file_keeps_its_row(sessionmaker, vault, monkeypatch):
    (vault / "locked.md").write_text("still here\n", encoding="utf-8")
    await indexer.index_vault()
    before = await row(sessionmaker, "locked.md")

    unreadable(monkeypatch, "locked.md")
    indexer._last_full_hash.clear()  # a full-hash pass must read it
    await indexer.index_vault()

    after = await row(sessionmaker, "locked.md")
    assert after is not None
    assert (after.id, after.content_hash) == (before.id, before.content_hash)


# ══════════════════════════════════════════════════════════════════════════
# D8 — the re-derive stamp
# ══════════════════════════════════════════════════════════════════════════


async def tenant(sessionmaker, root, name: str) -> int:
    root.mkdir(exist_ok=True)
    async with sessionmaker() as session:
        user = User(username=name, password_hash="x", vault_path=str(root))
        session.add(user)
        await session.commit()
        await vault_service.warm_user_vault_cache(session, user_id=user.id)
        return user.id


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


async def test_a_re_derive_with_a_row_less_unreadable_file_stamps_and_settles(
    sessionmaker, vault, monkeypatch
):
    root = vault / "tenant"
    uid = await tenant(sessionmaker, root, "rowless")
    try:
        (root / "a.md").write_text("a\n", encoding="utf-8")
        (root / "b.md").write_text("b\n", encoding="utf-8")
        (root / "Gone.md").write_text("never readable\n", encoding="utf-8")
        unreadable(monkeypatch, "Gone.md")
        assert await provenance(sessionmaker, uid) is None

        first = await indexer.index_vault(user_id=uid)
        assert first.rederive == indexer.REDERIVE_RECORDED
        assert await provenance(sessionmaker, uid) == canonical_vault_root(root)

        second = await indexer.index_vault(user_id=uid)
        assert second.rederive is None, "the scope left re-derive"
        assert second.notes_indexed == 0, "no full-scope rewrite on the next tick"
    finally:
        vault_service.clear_user_vault_cache(user_id=uid)


async def test_a_re_derive_with_an_unreadable_file_that_has_a_row_does_not_stamp(
    sessionmaker, vault, monkeypatch
):
    root = vault / "tenant"
    uid = await tenant(sessionmaker, root, "withrow")
    try:
        (root / "a.md").write_text("a\n", encoding="utf-8")
        (root / "Same.md").write_text("unreadable now\n", encoding="utf-8")
        foreign_id = await foreign_row(sessionmaker, uid, "Same.md")
        unreadable(monkeypatch, "Same.md")

        result = await indexer.index_vault(user_id=uid)
        assert result.rederive == indexer.REDERIVE_INCOMPLETE
        assert result.rederive_incomplete is True
        assert await provenance(sessionmaker, uid) is None
        kept = await row(sessionmaker, "Same.md", uid)
        assert kept is not None and kept.id == foreign_id, "the row is kept"
    finally:
        vault_service.clear_user_vault_cache(user_id=uid)


async def test_a_foreign_row_behind_an_undecodable_path_is_deleted_not_certified(
    sessionmaker, vault
):
    root = vault / "tenant"
    uid = await tenant(sessionmaker, root, "undecodable")
    try:
        (root / "a.md").write_text("a\n", encoding="utf-8")
        (root / "Same.md").write_bytes(b"caf\xe9\n")
        await foreign_row(sessionmaker, uid, "Same.md")

        result = await indexer.index_vault(user_id=uid)
        assert await row(sessionmaker, "Same.md", uid) is None
        assert result.rederive == indexer.REDERIVE_RECORDED
        assert await provenance(sessionmaker, uid) == canonical_vault_root(root)

        again = await indexer.index_vault(user_id=uid)
        assert again.rederive is None and again.notes_indexed == 0
    finally:
        vault_service.clear_user_vault_cache(user_id=uid)
