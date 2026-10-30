"""`hybrid_search` (#354) against real PostgreSQL.

What only a database can show:

* **The fused order on a seeded corpus**, for k = 60 and k = 1, equals the RRF
  formula applied to the ranks the two production arms (`full_text_search`,
  `semantic_search`) give the same query, owner and filters.
* **The filters and the owner predicate apply to both arms**: a note outside
  the folder, the tags, the frontmatter or the caller's tenancy is in neither.
* **The zero-row exact fallback inside the hybrid transaction**, after which
  `enable_indexscan` is back on before the snippet statements and the
  snippets are right.
* **Keyword-only on a raising provider.**
* **One snapshot.** A note committed on another connection between the
  keyword arm and the semantic arm is in neither arm of that call and in both
  of the next, and the transaction reads `repeatable read`.
* **The panel route** renders a `hybrid_search` query table (#354 D10).

Skipped unless `PGVECTOR_TEST_ADMIN_URL` is set — see `_harness.py`.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import _harness
from src.models.db import NoteEmbedding, NoteMetadata, UsageLog, User
from src.services import embeddings, hybrid, search, timing, vector_index
from src.services.embeddings import semantic_search
from src.services.fts import index_tsvector_sql
from src.services.hybrid import fuse
from src.services.search import full_text_search

pytestmark = [_harness.requires_pgvector, pytest.mark.asyncio(loop_scope="module")]

DIM = 8
QUERY = "zx81"


def _unit(*pairs: tuple[int, float]) -> list[float]:
    v = [0.0] * DIM
    for i, x in pairs:
        v[i] = x
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v]


Q = _unit((0, 1.0))


@pytest.fixture(scope="module")
def migrated_url():
    yield from _harness.throwaway_database("hybrid_354", DIM)


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def maker(migrated_url):
    engine = create_async_engine(migrated_url)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


async def _add_note(session, *, path, body, vectors, user_id, title=None, tags=None,
                    frontmatter=None):
    fragment, cfg_params = index_tsvector_sql()
    note = NoteMetadata(
        file_path=path, title=title or path.rsplit("/", 1)[-1].removesuffix(".md"),
        content_hash=path, embedded_content_hash=path, tags=tags or [],
        frontmatter=frontmatter or {}, user_id=user_id, body_excerpt=body,
    )
    session.add(note)
    await session.flush()
    for ci, vec in enumerate(vectors):
        session.add(NoteEmbedding(note_id=note.id, chunk_index=ci,
                                  chunk_text=f"{body} (chunk {ci})", embedding=vec))
    await session.flush()
    await session.execute(
        text(f"UPDATE notes_metadata SET content_tsvector = {fragment} WHERE id = :id"),
        {"id": note.id, "content": body, "title": note.title, "aliases": "", **cfg_params},
    )
    return note.id


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def corpus(maker, migrated_url):
    async with maker() as session:
        await session.execute(text(vector_index.drop_index_sql()))
        users = {}
        for name in ("u7", "u8"):
            user = User(username=name, password_hash="x", vault_path=f"/vaults/{name}")
            session.add(user)
            await session.flush()
            users[name] = user.id
        u7, u8 = users["u7"], users["u8"]
        notes = [
            # The identifier note: lexically exact, semantically far.
            dict(path="Projects/zx81-runbook.md", title="ZX81 runbook",
                 body="the zx81 deploy steps", vectors=[_unit((1, 1.0))], user_id=u7,
                 frontmatter={"status": "other"}),
            # Both arms.
            dict(path="Projects/both.md", body="zx81 rollback and deploy",
                 vectors=[_unit((0, 1.0), (2, 0.25))], user_id=u7, tags=["keep"],
                 frontmatter={"status": "keep"}),
            # The paraphrase note: semantically closest, no lexical match.
            dict(path="Projects/paraphrase.md", body="how we ship things",
                 vectors=[_unit((0, 1.0), (3, 0.05))], user_id=u7, tags=["keep"]),
            # Outside the folder, matching both arms.
            dict(path="Archive/outside.md", body="zx81 zx81", vectors=[list(Q)],
                 user_id=u7, tags=["keep"], frontmatter={"status": "keep"}),
            # Another tenant and the ownerless slice, matching both arms.
            dict(path="Projects/tenant8.md", body="zx81 for user eight", vectors=[list(Q)],
                 user_id=u8),
            dict(path="Projects/ownerless.md", body="zx81 for nobody", vectors=[list(Q)],
                 user_id=None),
            # Lexical match, no vectors: the zero-row exact fallback.
            dict(path="Empty/no-vectors.md", body="zx81 without any embedding", vectors=[],
                 user_id=u7),
        ]
        for i in range(6):
            notes.append(dict(
                path=f"Projects/filler-{i}.md", body=f"unrelated filler text number {i}",
                vectors=[_unit((0, 0.3 + 0.1 * i), (4 + i % 4, 1.0))], user_id=u7))
        notes.append(dict(path="Projects/zx81-also.md", body="zx81 mentioned once in passing "
                          + "padding " * 30, vectors=[_unit((0, 0.2), (5, 1.0))], user_id=u7))
        for n in notes:
            await _add_note(session, **n)
        await session.commit()
    engine = create_async_engine(migrated_url, poolclass=NullPool,
                                 isolation_level="AUTOCOMMIT")
    async with engine.connect() as conn:
        await conn.execute(text("VACUUM (ANALYZE) notes_metadata"))
        await conn.execute(text("VACUUM (ANALYZE) note_embeddings"))
    await engine.dispose()
    return users


@pytest.fixture(autouse=True)
def _fixed_embedding(monkeypatch):
    async def _embed(_text):
        return list(Q)

    monkeypatch.setattr(embeddings, "get_embedding", _embed)


async def _hybrid(maker, **kw):
    kw.setdefault("limit", 50)
    async with maker() as session:
        return await hybrid.hybrid_search(session, QUERY, **kw)


async def _arm_orders(maker, **filters):
    async with maker() as session:
        kw = await full_text_search(session, QUERY, limit=50, **filters)
    async with maker() as session:
        sem = await semantic_search(session, QUERY, limit=50, **filters)
    return [r["path"] for r in kw], [r["path"] for r in sem]


@pytest.mark.parametrize("k", [60, 1])
async def test_the_fused_order_is_the_formula_on_the_arms_ranks(maker, corpus, k):
    filters = dict(folder="Projects/", user_id=corpus["u7"])
    keyword, semantic = await _arm_orders(maker, **filters)
    assert "Projects/zx81-runbook.md" in keyword and "Projects/paraphrase.md" in semantic
    assert "Projects/paraphrase.md" not in keyword
    result = await _hybrid(maker, rrf_k=k, **filters)
    expected = fuse(keyword, semantic, k)
    assert [(r.path, r.keyword_rank, r.semantic_rank) for r in result.rows] == [
        (p, kr, sr) for p, _, kr, sr in expected]
    for row, (_, score, _, _) in zip(result.rows, expected):
        assert row.score == pytest.approx(score)
    if k == 60:
        assert result.rows[0].path == "Projects/both.md", "the consensus note leads"
    else:
        # At k = 1 each arm's rank 1 dominates: the same corpus orders differently.
        assert result.rows[0].path != "Projects/both.md"
    assert (result.arms, result.rrf_k) == ("both", k)
    assert result.keyword_rows == len(keyword) and result.semantic_rows == len(semantic)
    assert result.overlap == len(set(keyword) & set(semantic))


async def test_the_folder_filter_applies_to_both_arms(maker, corpus):
    unfiltered = await _hybrid(maker, user_id=corpus["u7"])
    outside = next(r for r in unfiltered.rows if r.path == "Archive/outside.md")
    assert outside.keyword_rank is not None and outside.semantic_rank is not None
    result = await _hybrid(maker, folder="Projects/", user_id=corpus["u7"])
    assert "Archive/outside.md" not in [r.path for r in result.rows]


async def test_tags_and_frontmatter_apply_to_both_arms(maker, corpus):
    u7 = corpus["u7"]
    # Unfiltered, these two notes are in BOTH arms; the filters must remove
    # them from both, not from one.
    unfiltered = {r.path: r for r in (await _hybrid(maker, folder="Projects/", user_id=u7)).rows}
    for path in ("Projects/zx81-runbook.md", "Projects/zx81-also.md"):
        assert unfiltered[path].keyword_rank is not None, path
        assert unfiltered[path].semantic_rank is not None, path
    for filters in (dict(tags=["keep"]), dict(frontmatter={"status": "keep"})):
        keyword, semantic = await _arm_orders(maker, folder="Projects/", user_id=u7, **filters)
        res = await _hybrid(maker, folder="Projects/", user_id=u7, **filters)
        for path in ("Projects/zx81-runbook.md", "Projects/zx81-also.md"):
            assert path not in keyword and path not in semantic, (filters, path)
            assert path not in [r.path for r in res.rows], (filters, path)
        assert res.keyword_rows == len(keyword) and res.semantic_rows == len(semantic)
    tagged = await _hybrid(maker, folder="Projects/", tags=["keep"], user_id=u7)
    assert {r.path for r in tagged.rows} == {"Projects/both.md", "Projects/paraphrase.md"}
    both = next(r for r in tagged.rows if r.path == "Projects/both.md")
    assert both.keyword_rank == 1 and both.semantic_rank is not None
    fm = await _hybrid(maker, folder="Projects/", frontmatter={"status": "keep"}, user_id=u7)
    assert [r.path for r in fm.rows] == ["Projects/both.md"]


async def test_the_owner_predicate_applies_to_both_arms(maker, corpus):
    u7, u8 = corpus["u7"], corpus["u8"]
    seven = {r.path for r in (await _hybrid(maker, folder="Projects/", user_id=u7)).rows}
    assert "Projects/tenant8.md" not in seven and "Projects/ownerless.md" not in seven
    eight = await _hybrid(maker, folder="Projects/", user_id=u8)
    assert [(r.path, r.keyword_rank, r.semantic_rank) for r in eight.rows] == [
        ("Projects/tenant8.md", 1, 1)]
    nobody = await _hybrid(maker, folder="Projects/", user_id=None)
    assert [r.path for r in nobody.rows] == ["Projects/ownerless.md"]


async def test_the_exact_fallback_is_restored_before_correct_snippets(maker, corpus, monkeypatch):
    seen = []
    real = search.keyword_snippets

    async def spy(session, rows, arm):
        seen.append((await session.execute(text("SHOW enable_indexscan"))).scalar())
        return await real(session, rows, arm)

    monkeypatch.setattr(search, "keyword_snippets", spy)
    token = timing.begin()
    try:
        result = await _hybrid(maker, folder="Empty/", user_id=corpus["u7"])
        holder = timing.current()
    finally:
        timing.clear(token)
    assert holder["exact_fallback"] is True
    assert seen == ["on"], "the snippet statements must not inherit the exact plan"
    (row,) = result.rows
    assert row.path == "Empty/no-vectors.md" and row.semantic_rank is None
    assert row.snippet and "«zx81»" in row.snippet
    assert row.matched_in == ["body"]


async def test_keyword_only_with_a_raising_provider(maker, corpus, monkeypatch):
    async def _down(_text):
        raise ConnectionError("provider unreachable")

    monkeypatch.setattr(embeddings, "get_embedding", _down)
    filters = dict(folder="Projects/", user_id=corpus["u7"])
    result = await _hybrid(maker, **filters)
    async with maker() as session:
        keyword = [r["path"] for r in await full_text_search(session, QUERY, limit=50, **filters)]
    assert (result.arms, result.semantic_unavailable) == ("keyword_only", "provider_error")
    assert [r.path for r in result.rows] == keyword
    assert all(r.semantic_rank is None for r in result.rows)


async def test_one_snapshot_serves_both_arms(maker, corpus, monkeypatch):
    u7 = corpus["u7"]
    late = "Projects/late.md"
    isolation = []

    async def commit_between_arms(session):
        isolation.append((await session.execute(text("SHOW transaction_isolation"))).scalar())
        async with maker() as other:
            await _add_note(other, path=late, body="zx81 arrived late", vectors=[list(Q)],
                            user_id=u7)
            await other.commit()

    monkeypatch.setattr(hybrid, "_between_arms_hook", commit_between_arms)
    try:
        first = await _hybrid(maker, folder="Projects/", user_id=u7)
    finally:
        monkeypatch.setattr(hybrid, "_between_arms_hook", None)
    try:
        assert isolation == ["repeatable read"]
        assert late not in [r.path for r in first.rows], "absent from both arms of that call"
        second = await _hybrid(maker, folder="Projects/", user_id=u7)
        row = next(r for r in second.rows if r.path == late)
        assert row.keyword_rank is not None and row.semantic_rank is not None
    finally:
        async with maker() as session:
            await session.execute(delete(NoteMetadata).where(NoteMetadata.file_path == late))
            await session.commit()


async def test_the_search_analytics_page_has_a_hybrid_table(migrated_url, maker, corpus,
                                                            monkeypatch):
    """The route indexes `_SEARCH_TOOL_LABELS[tool]` for every query tool; with
    a `hybrid_search` row in the window it renders a third query table."""
    from fastapi.testclient import TestClient

    from src import main
    from src.config import settings
    from src.control_panel import routes as panel_routes
    from src.database import get_session

    async with maker() as session:
        admin = User(username="hs-admin", password_hash="x", is_admin=True, is_active=True,
                     vault_path="/vaults/admin")
        session.add(admin)
        session.add(UsageLog(
            tool="hybrid_search", user_id=corpus["u7"], duration_ms=5, response_size=10,
            created_at=datetime.now(timezone.utc),
            params={"query": "hybrid-panel-probe", "result_count": 0, "result_paths": [],
                    "arms": "both", "rrf_k": 60},
        ))
        await session.commit()
        admin_id = admin.id

    async def _session():
        engine = create_async_engine(migrated_url, poolclass=NullPool)
        try:
            async with async_sessionmaker(engine, class_=AsyncSession)() as session:
                yield session
        finally:
            await engine.dispose()

    async def _panel_user():
        engine = create_async_engine(migrated_url, poolclass=NullPool)
        try:
            async with async_sessionmaker(engine, class_=AsyncSession,
                                          expire_on_commit=False)() as session:
                return await session.get(User, admin_id)
        finally:
            await engine.dispose()

    monkeypatch.setattr(settings, "panel_csp", "enforce")
    overrides = dict(main.app.dependency_overrides)
    main.app.dependency_overrides[get_session] = _session
    main.app.dependency_overrides[panel_routes.require_user_panel] = _panel_user
    try:
        response = TestClient(main.app, base_url="https://localhost").get(
            "/admin/search-analytics", params={"window": "24h"})
    finally:
        main.app.dependency_overrides.clear()
        main.app.dependency_overrides.update(overrides)
        async with maker() as session:
            await session.execute(delete(UsageLog).where(UsageLog.tool == "hybrid_search"))
            await session.execute(delete(User).where(User.id == admin_id))
            await session.commit()
    assert response.status_code == 200, response.text[:500]
    body = response.text
    assert "Keyword search" in body and "Semantic search" in body
    assert "Hybrid search" in body
    assert "hybrid-panel-probe" in body
