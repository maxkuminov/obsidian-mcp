"""#127 / D1, D1a — read-path owner scoping is total, and closed over joins.

`apply_note_filters(user_id=None)` used to append **no** owner predicate, while
every write path maps `None` to `user_id IS NULL`. `MULTI_USER_MODE` can be
turned off after users exist, and an ownerless credential (`api_keys.user_id`
NULL) is a legitimate single-user shape — so a database holding rows owned by
named users could be read, in full, by a credential that owns none of them:
paths, titles, tags, frontmatter and chunk excerpts, from every index-backed
tool at once.

The graph tools needed more than a swapped predicate. `note_links` carries no
`user_id`, so ownership can only be expressed through the endpoint rows, and
where it is expressed decides two different things:

* in a JOIN's ON clause a cross-owner target simply fails to resolve, and the
  link is still reported from the caller's own `target_path` string. As a WHERE
  on the joined row it would discard every *dangling* link too — the rows
  `get_links` exists to show.
* an edge admitted into the neighborhood BFS or the orphan calculus changes
  what the answer **is**, not merely what is printed: it occupies a slot
  against `limit`, it can bridge two owned notes through a row the caller
  cannot see, and on the target side it silently strips an owned note's orphan
  status.

Every assertion here runs against a real PostgreSQL, through the production
tool entry points, on a database seeded with both NULL-owned and named-user
rows. Skipped unless `PGVECTOR_TEST_ADMIN_URL` is set — see `_harness.py`.
"""
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import src.database
import src.mcp_server.tools as tools
from src.config import settings
from src.models.db import NoteEmbedding, NoteLink, NoteMetadata, User
from src.services import embeddings as embeddings_service
import _harness

pytestmark = [
    _harness.requires_pgvector,
    pytest.mark.asyncio(loop_scope="module"),
]

DIM = 8

# The vector every seeded chunk carries, and the query vector. One direction
# for everything: the ranking is irrelevant here, only *membership* is.
UNIT = [1.0] + [0.0] * (DIM - 1)


@pytest.fixture(scope="module")
def migrated_url():
    yield from _harness.throwaway_database("owner_scope_127", DIM)


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def sessionmaker(migrated_url):
    engine = create_async_engine(migrated_url, poolclass=None)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest_asyncio.fixture(loop_scope="module")
async def corpus(sessionmaker, monkeypatch, tmp_path):
    """Two owners in one database: NULL (the ownerless caller) and `mallory`.

    Shape, chosen so every enumerated tool has something to get wrong:

        NULL-owned      mine/root.md   →  mine/leaf.md        (owned edge)
                        mine/root.md   →  theirs/secret.md    (cross-owner)
                        mine/root.md   →  Nowhere             (dangling)
                        mine/lonely.md                        (a true orphan)
                        mine/claimed.md ← theirs/secret.md    (cross-owner in)
        mallory-owned   theirs/secret.md → theirs/inner.md
    """
    root = tmp_path / "vault"
    root.mkdir()
    monkeypatch.setattr(settings, "vault_path", str(root), raising=False)
    monkeypatch.setattr(tools.settings, "vault_path", str(root), raising=False)
    monkeypatch.setattr(tools, "async_session", sessionmaker)
    monkeypatch.setattr(src.database, "async_session", sessionmaker)
    monkeypatch.setattr(embeddings_service, "async_session", sessionmaker, raising=False)

    async def _fake_embedding(_text):
        return list(UNIT)

    monkeypatch.setattr(embeddings_service, "get_embedding", _fake_embedding)

    async def noop(*_a, **_k):
        return None

    monkeypatch.setattr(tools, "_log_usage", noop)

    async with sessionmaker() as session:
        await session.execute(text("DELETE FROM note_links"))
        await session.execute(text("DELETE FROM note_embeddings"))
        await session.execute(text("DELETE FROM notes_metadata"))
        await session.execute(text("DELETE FROM users"))
        await session.commit()

    async with sessionmaker() as session:
        mallory = User(username="mallory", password_hash="x", vault_path="/v/m")
        session.add(mallory)
        await session.flush()
        mid = mallory.id

        ids = {}
        spec = [
            ("mine/root.md", None, ["mine"], {"status": "open"}),
            ("mine/leaf.md", None, ["mine"], {"status": "open"}),
            ("mine/lonely.md", None, ["mine"], {"status": "open"}),
            ("mine/claimed.md", None, ["mine"], {"status": "open"}),
            ("theirs/secret.md", mid, ["theirs-only-tag"], {"status": "secret"}),
            ("theirs/inner.md", mid, ["theirs-only-tag"], {"status": "secret"}),
        ]
        for path, owner, tags, fm in spec:
            note = NoteMetadata(
                user_id=owner,
                file_path=path,
                title=path.rsplit("/", 1)[-1].removesuffix(".md"),
                tags=tags,
                frontmatter=fm,
                content_hash=path,
            )
            session.add(note)
            await session.flush()
            ids[path] = note.id
            session.add(NoteEmbedding(
                note_id=note.id, chunk_index=0,
                chunk_text=f"chunk of {path}", embedding=list(UNIT),
            ))

        def link(src, tgt_path, tgt_id, kind="wikilink", pos=0):
            session.add(NoteLink(
                source_note_id=ids[src], target_path=tgt_path,
                target_note_id=tgt_id, link_text=tgt_path, kind=kind,
                position=pos,
            ))

        link("mine/root.md", "mine/leaf.md", ids["mine/leaf.md"], pos=0)
        # The adversarial edge: an owned note whose link row resolves to a row
        # the caller does not own.
        link("mine/root.md", "theirs/secret.md", ids["theirs/secret.md"], pos=1)
        link("mine/root.md", "Nowhere", None, pos=2)
        # The other direction: a *foreign* note pointing at an owned one. This
        # is what used to strip `mine/claimed.md` of its orphan status.
        link("theirs/secret.md", "mine/claimed.md", ids["mine/claimed.md"], pos=0)
        link("theirs/secret.md", "theirs/inner.md", ids["theirs/inner.md"], pos=1)
        await session.commit()

    yield {"mallory": mid, "ids": ids}


@pytest_asyncio.fixture(loop_scope="module", autouse=True)
async def _ownerless(monkeypatch):
    """Every test in this module calls as the ownerless caller unless it says
    otherwise. `_vault_root(None)` answers from `settings.vault_path`, which is
    the single-user shape these tools are reached through."""
    token = tools.current_user_id.set(None)
    yield
    tools.current_user_id.reset(token)


THEIRS = ("theirs/secret.md", "theirs/inner.md")


def _leaks(out: str) -> list[str]:
    return [p for p in THEIRS if p in out] + (
        ["secret"] if "secret" in out.replace("theirs/secret.md", "") else []
    )


# ── the flat read tools ─────────────────────────────────────────────────────
async def test_list_notes_returns_only_null_owned_rows(corpus):
    out = await tools.list_notes_impl(limit=100)
    assert "mine/root.md" in out
    assert not _leaks(out), out


async def test_get_recent_returns_only_null_owned_rows(corpus):
    out = await tools.get_recent_impl(limit=100)
    assert "mine/root.md" in out
    assert not _leaks(out), out


async def test_keyword_search_returns_only_null_owned_rows(corpus, sessionmaker):
    # Give the NULL-owned and the foreign rows a tsvector so both *could* match.
    async with sessionmaker() as session:
        await session.execute(text(
            "UPDATE notes_metadata "
            "SET content_tsvector = to_tsvector('english', 'quartzite')"
        ))
        await session.commit()
    out = await tools.search_notes_impl("quartzite", limit=100)
    assert "mine/root.md" in out
    assert not _leaks(out), out


async def test_get_tags_does_not_count_another_owners_tags(corpus):
    out = await tools.get_tags_impl(limit=100)
    assert "#mine" in out
    assert "theirs-only-tag" not in out, out


async def test_semantic_search_returns_only_null_owned_rows(corpus):
    out = await tools.semantic_search_impl("anything", limit=50)
    assert "mine/root.md" in out
    assert not _leaks(out), out


async def test_find_related_returns_only_null_owned_rows(corpus):
    out = await tools.find_related_impl("mine/root.md", limit=50)
    assert not _leaks(out), out


async def test_a_named_users_note_is_not_even_addressable(corpus):
    """The source lookups are scoped too, so a foreign path is `not found`
    rather than a note whose graph the caller may walk."""
    for call in (
        tools.get_links_impl("theirs/secret.md"),
        tools.get_backlinks_impl("theirs/secret.md"),
        tools.get_neighborhood_impl("theirs/secret.md"),
        tools.find_related_impl("theirs/secret.md"),
    ):
        out = await call
        assert "Note not found" in out, out


# ── the graph closure ───────────────────────────────────────────────────────
async def test_get_links_hides_a_cross_owner_target_but_keeps_the_dangling_row(
    corpus,
):
    """The cross-owner edge must not print the other owner's title or path,
    and the genuinely dangling link must still be reported — which is what an
    ownership predicate applied as a WHERE on the outer-joined row would have
    destroyed."""
    out = await tools.get_links_impl("mine/root.md")

    assert "mine/leaf.md" in out, out
    assert "Nowhere" in out, "the dangling link must still be reported"
    assert not _leaks(out), out
    # One resolved (the owned edge), one dangling (`Nowhere`). The cross-owner
    # row is neither: it is omitted rather than printed as dangling, because
    # its `target_path` *is* the other owner's path.
    assert "1 resolved, 1 dangling" in out, out


async def test_get_backlinks_hides_a_cross_owner_source(corpus):
    out = await tools.get_backlinks_impl("mine/claimed.md")
    assert "No backlinks" in out, out


async def test_a_cross_owner_edge_is_not_a_neighborhood_bridge(corpus):
    """`mine/root.md → theirs/secret.md → theirs/inner.md` must contribute
    nothing: not the foreign notes themselves, and not a distance-2 path
    through a row the caller cannot see."""
    out = await tools.get_neighborhood_impl("mine/root.md", depth=3, limit=100)
    assert "mine/leaf.md" in out
    assert not _leaks(out), out
    # `mine/claimed.md` is reachable only *through* mallory's note.
    assert "mine/claimed.md" not in out, out


async def test_a_cross_owner_edge_does_not_change_orphan_status(corpus):
    """`mine/claimed.md` has exactly one incoming link, from a note owned by
    somebody else. Counting it made an orphan look connected — an answer
    decided by a row the caller has no way to see."""
    out = await tools.find_orphans_impl(limit=100)
    assert "mine/lonely.md" in out, out
    assert "mine/claimed.md" in out, out
    # `mine/root.md` has a real outgoing link and `mine/leaf.md` a real
    # incoming one; neither is an orphan.
    assert "mine/root.md" not in out, out
    assert "mine/leaf.md" not in out, out
    assert not _leaks(out), out


# ── D1a: the owner predicate makes every vector query a filtered one ───────
async def test_an_ownerless_semantic_query_reads_only_its_own_slice(
    corpus, sessionmaker
):
    """A folder both owners have notes in: the ownerless caller sees one."""
    async with sessionmaker() as session:
        note = NoteMetadata(
            user_id=None, file_path="theirs/mine-too.md", title="mine-too",
            tags=["mine"], frontmatter={}, content_hash="theirs/mine-too.md",
        )
        session.add(note)
        await session.flush()
        session.add(NoteEmbedding(
            note_id=note.id, chunk_index=0,
            chunk_text="chunk of theirs/mine-too.md", embedding=list(UNIT),
        ))
        await session.commit()

    out = await tools.semantic_search_impl("anything", limit=50, folder="theirs/")
    assert "theirs/mine-too.md" in out, out
    assert not _leaks(out), out


async def test_an_ownerless_zero_row_query_re_runs_exactly(corpus, sessionmaker):
    """The code path the old eligibility rule skipped, on a real database.

    An ownerless call carries no `folder`/`tags`/`frontmatter` and no named
    user, so it used to count as *unfiltered* and its empty result was
    believed — while the predicate it did carry, `user_id IS NULL`, was
    precisely the one throwing the HNSW window away on a database whose
    vectors mostly belong to somebody else.

    Deliberately asserted on `exact_fallback` and on membership rather than by
    forcing the approximate scan to miss a row it should have found: HNSW
    assigns node levels randomly at build time, so a starved scan's outcome
    changes with every `CREATE INDEX` (measured in `test_search_recall.py`).
    The *recovery* is pinned deterministically in
    `tests/test_vector_iterative_scan.py`, where the empty first result is
    given rather than gambled on. The service is called directly because
    `_tracked` clears the timing holder in its own `finally`.
    """
    from src.services import timing
    from src.services.embeddings import semantic_search

    token = timing.begin()
    try:
        async with sessionmaker() as session:
            results = await semantic_search(
                session, "anything", limit=5, folder="nothing-here/", user_id=None
            )
        holder = timing.current()
        recorded = dict(holder) if holder else {}
    finally:
        timing.clear(token)

    assert results == []
    assert recorded.get("exact_fallback") is True, recorded


async def test_a_named_user_scope_that_matches_still_skips_the_fallback(
    corpus, sessionmaker
):
    """The fallback is armed everywhere, but it only *fires* on zero rows —
    a non-empty result must not pay for an O(n) exact scan."""
    from src.services import timing
    from src.services.embeddings import semantic_search

    token = timing.begin()
    try:
        async with sessionmaker() as session:
            results = await semantic_search(
                session, "anything", limit=5, user_id=corpus["mallory"]
            )
        holder = timing.current()
        recorded = dict(holder) if holder else {}
    finally:
        timing.clear(token)

    assert {r["path"] for r in results} <= set(THEIRS), results
    assert results, "mallory owns embedded notes"
    assert recorded.get("exact_fallback") is False, recorded


# ── named-user scoping is unchanged ─────────────────────────────────────────
async def test_named_user_scoping_is_unchanged(corpus, monkeypatch):
    monkeypatch.setattr(
        tools, "_vault_root", lambda _uid: tools.Path(settings.vault_path)
    )
    token = tools.current_user_id.set(corpus["mallory"])
    try:
        out = await tools.list_notes_impl(limit=100)
        assert "theirs/secret.md" in out
        assert "mine/root.md" not in out, out

        tags = await tools.get_tags_impl(limit=100)
        assert "#theirs-only-tag" in tags
        assert "#mine" not in tags, tags

        links = await tools.get_links_impl("theirs/secret.md")
        assert "theirs/inner.md" in links
        # Symmetric: mallory's cross-owner edge is omitted from her view too.
        assert "mine/claimed.md" not in links, links
        assert "1 resolved, 0 dangling" in links, links
    finally:
        tools.current_user_id.reset(token)
