"""Identity oracle for the read-path projection (#280, design D5–D7).

Six read paths stopped loading whole entities: `semantic_search`,
`keyword_search`, `list_notes`, `get_recent`, `find_orphans` and
`get_neighborhood`'s metadata hydration. Similarity became `1 - distance`, and
exact ties gained a `file_path` tie-break. The promise is that **results do not
change**, which is a claim about search correctness: an agent acts on these
results without a person reviewing them. So this module runs the pre-change
implementations, copied here verbatim as oracles (`_old_*`), next to the
production ones on one corpus and compares their output.

"Identical" is the definition from design D7 and the search-quality delta:
- the same result set and the same order;
- every field other than `similarity` byte-equal;
- `similarity` within 1e-5;
- with exactly three permitted exceptions, all confined to exact ties of the
  sort key (`modified_at`, `rank` or distance):
  (a) **membership at a tied cutoff**: when more rows share the boundary key
      than fit under the limit, which of them are returned may differ;
  (b) **order among exact ties**: tied rows may appear in a different relative
      order even when all of them fit;
  (c) **the representative chunk among exact distance ties**: when two chunks
      of one note are exactly equidistant, the kept `chunk_index` and its
      preview may differ.

`_assert_identical` implements that definition, and the `test_oracle_rejects_*`
cases prove it is not a rubber stamp: every non-permitted perturbation fails
it. The corpus plants each permitted case on purpose, and each test asserts
that its case really occurred, so no test can pass vacuously.

The vector corpus is the recall benchmark's shape (the `A/` crowd around each
query vector, and `B/` as the filtered half). Stale, truncated, keyword, link,
tie and orphan structure is layered on top.

**`semantic_search` is compared under exact ordering in both arms.** Since
#283 (D19) the production query orders by the half-precision expression the
`halfvec` HNSW index is built on, while the old oracle orders by the full
`vector` distance, which no index serves any more. Comparing an approximate
index walk against an exact scan would measure ANN recall, not projection
identity — and recall is `test_search_recall.py`'s job. So `_semantic_pair`
issues `SET LOCAL enable_indexscan = off` in both arms' transactions: both are
exact scans, and what differs between them is the projection, similarity as
`1 - distance`, the tie-breaks and the full-precision re-sort — the things
this module claims. The half-precision *candidate* ordering can only move rows
at the overfetch boundary (5x the limit), far below the compared top `limit`.

The old oracles select `NoteMetadata` entities, which after D5 no longer carry
`content_tsvector`. That changes what is shipped, not what is returned, and
what is returned is the thing being compared.

Skipped unless `PGVECTOR_TEST_ADMIN_URL` is set — see `_harness.py`.
"""
import copy
import datetime
import random
import re
from datetime import timezone

import numpy as np
import pytest
import pytest_asyncio
from sqlalchemy import and_, or_, select, text, union
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import _harness
import src.mcp_server.tools as tools
from src.config import settings
from src.models.db import NoteEmbedding, NoteLink, NoteMetadata, User
from src.services import timing, vector_index
from src.services import vault as vault_service
from src.services.embeddings import semantic_search
from src.services.filters import apply_note_filters
from src.services.fts import combined_tsquery, index_tsvector_sql
from src.services.search import full_text_search
from test_search_recall import (
    _as_authenticated_request,
    _build_hnsw_index,
    _near,
    _random_unit,
)

pytestmark = [
    _harness.requires_pgvector,
    pytest.mark.asyncio(loop_scope="module"),
]

DIM = int(settings.embedding_dimensions)
SEED = 1234  # the recall benchmark's seed
N_QUERIES = 5
A_NOTES_PER_QUERY = 100
B_NOTES = 1500
CHUNKS_PER_B_NOTE = 2
SIM_TOLERANCE = 1e-5

# 40 distinct modification times over 1,500 `B/` notes: every value is shared
# by ~37 notes, so a limit of 20 cuts through a tie group (case a) and a limit
# of 100 contains whole tie groups (case b).
MTIME_BUCKETS = 40
T0 = datetime.datetime(2026, 1, 1, tzinfo=timezone.utc)

_WORDS = ["meeting", "roadmap", "budget", "vector", "planner", "vault", "sprint"]


def _b_body(i: int) -> str:
    # Few distinct bodies, so `ts_rank_cd` produces large exact-tie groups.
    return " ".join(_WORDS[: 2 + i % 4]) + (" meeting" * (i % 2))


# ── the corpus ──────────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def migrated_url():
    yield from _harness.throwaway_database("perf_projection", DIM)


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def sessionmaker(migrated_url):
    engine = create_async_engine(migrated_url, poolclass=None)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest.fixture(scope="module")
def queries():
    rng = random.Random(SEED)
    return [_random_unit(rng) for _ in range(N_QUERIES)]


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def corpus(sessionmaker, queries):
    rng = random.Random(SEED + 1)

    async with sessionmaker() as session:
        users = {}
        for name in ("alice", "bob"):
            user = User(username=name, password_hash="x", vault_path=f"/vaults/{name}")
            session.add(user)
            await session.flush()
            users[name] = user.id
        await session.execute(text(vector_index.drop_index_sql()))
        await session.commit()

    alice, bob = users["alice"], users["bob"]
    pending = []  # (path, vectors, attrs, body or None)

    # A/: the recall crowd, owned by alice.
    for qi in range(N_QUERIES):
        for i in range(A_NOTES_PER_QUERY):
            pending.append((
                f"A/q{qi}-{i:04d}.md",
                [_near(rng, queries[qi], 0.05)],
                {"tags": ["draft"], "frontmatter": {"status": "done"}, "user_id": alice,
                 "modified_at": T0},
                None,
            ))

    # B/: the filtered half, owned by bob. It carries stale rows (a differing
    # and a NULL embedded hash), truncated rows, NULL modification times,
    # heavily tied modification times and heavily tied keyword bodies.
    for i in range(B_NOTES):
        base = _random_unit(rng)
        if i % 7 == 0:
            embedded = "old-hash"
        elif i % 11 == 0:
            embedded = None
        else:
            embedded = f"B{i}"
        pending.append((
            f"B/note-{i:04d}.md",
            [_near(rng, base, 0.3) for _ in range(CHUNKS_PER_B_NOTE)],
            {
                "tags": ["reference", f"b{i % 3}"],
                "frontmatter": {"status": "open"},
                "user_id": bob,
                "content_hash": f"B{i}",
                "embedded_content_hash": embedded,
                "chunks_truncated": i % 5 == 0,
                "modified_at": (
                    None if i % 13 == 0
                    else T0 + datetime.timedelta(minutes=i % MTIME_BUCKETS)
                ),
                "file_size": 100 + i,
            },
            _b_body(i),
        ))

    # E/twin.md: two chunks with the *same* vector near query 0, so they are
    # exactly equidistant from it (case c). Owned by bob, tagged like `B/`.
    twin = _near(rng, queries[0], 0.02)
    pending.append((
        "E/twin.md", [twin, list(twin)],
        {"tags": ["reference"], "frontmatter": {"status": "open"}, "user_id": bob,
         "modified_at": T0},
        None,
    ))
    # Its competitor: one more note at a distinct distance.
    pending.append((
        "E/other.md", [_near(rng, queries[0], 0.2)],
        {"tags": ["reference"], "frontmatter": {"status": "open"}, "user_id": bob,
         "modified_at": T0},
        None,
    ))

    # T/: two notes with an identical modification time that both fit under
    # any limit (case b, planted explicitly), plus one before and one after.
    top = T0 + datetime.timedelta(days=30)
    for name, mtime in (
        ("T/a-first.md", top + datetime.timedelta(minutes=2)),
        ("T/m-tied.md", top + datetime.timedelta(minutes=1)),
        ("T/c-tied.md", top + datetime.timedelta(minutes=1)),
        ("T/z-last.md", top),
    ):
        pending.append((
            name, [_random_unit(rng)],
            {"tags": ["tie"], "frontmatter": {}, "user_id": bob, "modified_at": mtime},
            "meeting roadmap",
        ))

    # N/: orphans, some with a NULL modification time, which must stay last.
    for k in range(6):
        pending.append((
            f"N/orphan-{k}.md", [],
            {"tags": ["orph"], "frontmatter": {}, "user_id": bob,
             "modified_at": None if k % 2 else top + datetime.timedelta(hours=k)},
            None,
        ))

    # D/: metadata with no chunks, so a filtered vector query returns zero rows
    # under every plan and takes the exact fallback.
    pending.append((
        "D/unembedded.md", [],
        {"tags": ["empty"], "frontmatter": {"status": "empty"}, "modified_at": T0},
        None,
    ))
    # O/: an ownerless embedded note, far from every query, for the
    # `user_id IS NULL` shape.
    pending.append((
        "O/ownerless.md", [_random_unit(rng)],
        {"tags": ["own"], "frontmatter": {}, "modified_at": T0},
        None,
    ))

    fragment, cfg_params = index_tsvector_sql()
    async with sessionmaker() as session:
        notes = []
        for path, _vecs, attrs, _body in pending:
            attrs = dict(attrs)
            content_hash = attrs.pop("content_hash", path)
            embedded = attrs.pop("embedded_content_hash", content_hash)
            note = NoteMetadata(
                file_path=path,
                title=path.rsplit("/", 1)[-1].removesuffix(".md"),
                content_hash=content_hash,
                embedded_content_hash=embedded,
                **attrs,
            )
            session.add(note)
            notes.append(note)
        await session.flush()
        ids = {n.file_path: n.id for n in notes}
        for note, (path, vectors, _attrs, _body) in zip(notes, pending):
            for ci, vec in enumerate(vectors):
                session.add(NoteEmbedding(
                    note_id=note.id, chunk_index=ci,
                    chunk_text=f"{path} chunk {ci} " + "x" * 600, embedding=vec,
                ))
        await session.flush()
        tsv = text(
            f"UPDATE notes_metadata SET content_tsvector = {fragment} WHERE id = :id"
        )
        for path, _vecs, _attrs, body in pending:
            if body is not None:
                await session.execute(tsv, {"id": ids[path], "content": body, **cfg_params})

        # Links for `get_neighborhood` and `find_orphans`: a chain over every
        # third `B/` note, and a star around B/note-0003. `N/`, `T/`, `E/` and
        # most of `B/` are left unlinked, so they are orphans.
        def _link(src, dst):
            session.add(NoteLink(
                source_note_id=ids[src], target_note_id=ids[dst],
                target_path=dst, kind="link",
            ))

        for i in range(0, 300, 3):
            _link(f"B/note-{i:04d}.md", f"B/note-{i + 3:04d}.md")
        for i in range(1, 30, 3):
            _link(f"B/note-{i:04d}.md", "B/note-0003.md")
        await session.commit()

        await _build_hnsw_index(session)
        await session.execute(text("ANALYZE note_embeddings"))
        await session.execute(text("ANALYZE notes_metadata"))
        await session.commit()

    original_session, original_log = tools.async_session, tools._log_usage
    tools.async_session = sessionmaker

    async def _noop(*_a, **_k):
        return None

    tools._log_usage = _noop
    yield {"alice": alice, "bob": bob, "ids": ids}
    tools.async_session, tools._log_usage = original_session, original_log
    vault_service.clear_user_vault_cache()


@pytest.fixture
def embed_as(monkeypatch):
    """Make `get_embedding` return a chosen vector, for old and new alike."""
    state = {"vec": None}

    async def _fake(_text):
        return state["vec"]

    monkeypatch.setattr("src.services.embeddings.get_embedding", _fake)

    def _set(vec):
        state["vec"] = vec

    return _set


# ── the pre-change implementations (oracles), copied verbatim ───────────────
async def _old_full_text_search(session, query, folder=None, limit=20, tags=None,
                                frontmatter=None, user_id=None):
    from sqlalchemy import func

    limit = max(1, min(limit, 500))
    tsquery = combined_tsquery(query)
    rank = func.ts_rank_cd(NoteMetadata.content_tsvector, tsquery).label("rank")
    await session.execute(text("SET LOCAL random_page_cost = 1.1"))
    stmt = (
        select(NoteMetadata, rank)
        .where(NoteMetadata.content_tsvector.op("@@")(tsquery))
    )
    stmt = apply_note_filters(
        stmt, folder=folder, tags=tags, frontmatter=frontmatter, user_id=user_id
    )
    stmt = stmt.order_by(rank.desc(), NoteMetadata.file_path.asc()).limit(limit)
    rows = (await session.execute(stmt)).all()
    return [
        {"path": nm.file_path, "title": nm.title, "tags": nm.tags, "rank": float(r)}
        for nm, r in rows
    ]


async def _old_semantic_search(session, query_embedding, limit=15, folder=None,
                               tags=None, frontmatter=None, user_id=None):
    limit = max(1, min(limit, 50))
    await session.execute(text("SET LOCAL hnsw.ef_search = 80"))
    await session.execute(text("SET LOCAL random_page_cost = 1.1"))
    await session.execute(text("SET LOCAL hnsw.iterative_scan = 'relaxed_order'"))
    overfetch = max(limit * 5, 50)
    distance = NoteEmbedding.embedding.cosine_distance(query_embedding)
    stmt = (
        select(NoteEmbedding, NoteMetadata, distance.label("distance"))
        .join(NoteMetadata, NoteEmbedding.note_id == NoteMetadata.id)
    )
    stmt = apply_note_filters(
        stmt, folder=folder, tags=tags, frontmatter=frontmatter, user_id=user_id
    )
    stmt = stmt.order_by(distance).limit(overfetch)
    rows = (await session.execute(stmt)).fetchall()
    exact_fallback = False
    if not rows:
        await session.execute(text("SET LOCAL enable_indexscan = off"))
        rows = (await session.execute(stmt)).fetchall()
        exact_fallback = True
    rows = sorted(rows, key=lambda r: r[2])
    seen, deduped = set(), []
    for ne, nm, _d in rows:
        if ne.note_id in seen:
            continue
        seen.add(ne.note_id)
        deduped.append((ne, nm))
        if len(deduped) >= limit:
            break
    results = [
        {
            "path": nm.file_path,
            "title": nm.title,
            "tags": nm.tags,
            "chunk": None if stale else ne.chunk_text[:500],
            "chunk_index": ne.chunk_index,
            "similarity": float(np.dot(ne.embedding, query_embedding) / (
                np.linalg.norm(ne.embedding) * np.linalg.norm(query_embedding)
            )),
            "stale": stale,
            "embedding_truncated": bool(nm.chunks_truncated),
        }
        for ne, nm, stale in (
            (ne, nm, nm.embedded_content_hash != nm.content_hash) for ne, nm in deduped
        )
    ]
    return results, exact_fallback


async def _old_list_notes(sessionmaker, uid, folder="", limit=50, tags=None,
                          frontmatter=None):
    limit = tools._clamp_limit(limit)
    async with sessionmaker() as session:
        stmt = select(NoteMetadata).order_by(NoteMetadata.modified_at.desc())
        stmt = apply_note_filters(
            stmt, folder=folder or None, tags=tags, frontmatter=frontmatter, user_id=uid
        )
        notes = (await session.execute(stmt.limit(limit))).scalars().all()
    if not notes:
        return f"No markdown files in '{folder or '/'}'"
    lines = [f"Found {len(notes)} notes in '{folder or '/'}':\n"]
    for n in notes:
        if n.modified_at:
            mod = n.modified_at.astimezone(timezone.utc).strftime("%Y-%m-%d")
        else:
            mod = "unknown"
        size = n.file_size or 0
        lines.append(f"- `{n.file_path}` ({size:,}B, modified {mod})")
    return "\n".join(lines)


async def _old_get_recent(sessionmaker, uid, limit=20, folder=None, tags=None,
                          frontmatter=None):
    limit = tools._clamp_limit(limit)
    async with sessionmaker() as session:
        query = select(NoteMetadata).order_by(NoteMetadata.modified_at.desc())
        query = apply_note_filters(
            query, folder=folder, tags=tags, frontmatter=frontmatter, user_id=uid
        )
        notes = (await session.execute(query.limit(limit))).scalars().all()
    if not notes:
        return "No recent notes found"
    lines = [f"Last {len(notes)} modified notes:\n"]
    for n in notes:
        mod = n.modified_at.strftime("%Y-%m-%d %H:%M") if n.modified_at else "unknown"
        tags_str = f" [{', '.join(n.tags)}]" if n.tags else ""
        lines.append(f"- `{n.file_path}` — {n.title}{tags_str} (modified {mod})")
    return "\n".join(lines)


async def _old_find_orphans(sessionmaker, uid, folder=None, limit=50):
    limit = max(1, min(limit, 500))
    async with sessionmaker() as session:
        owned_ids = select(NoteMetadata.id).where(tools._note_owner_predicate(uid))
        edge_within_owned_set = and_(
            NoteLink.source_note_id.in_(owned_ids),
            or_(
                NoteLink.target_note_id.is_(None),
                NoteLink.target_note_id.in_(owned_ids),
            ),
        )
        sources = select(NoteLink.source_note_id.label("nid")).where(
            NoteLink.source_note_id.isnot(None), edge_within_owned_set
        )
        targets = select(NoteLink.target_note_id.label("nid")).where(
            NoteLink.target_note_id.isnot(None), edge_within_owned_set
        )
        connected = union(sources, targets).subquery()
        stmt = select(NoteMetadata).where(NoteMetadata.id.notin_(select(connected.c.nid)))
        stmt = apply_note_filters(stmt, folder=folder, user_id=uid)
        stmt = stmt.order_by(NoteMetadata.modified_at.desc().nullslast()).limit(limit)
        notes = (await session.execute(stmt)).scalars().all()
    if not notes:
        scope = f" in `{folder}`" if folder else ""
        return f"No orphan notes{scope}"
    lines = [f"Found {len(notes)} orphan notes:\n"]
    for n in notes:
        mod = n.modified_at.strftime("%Y-%m-%d") if n.modified_at else "unknown"
        tags_str = f" [{', '.join(n.tags)}]" if n.tags else ""
        lines.append(f"- `{n.file_path}` — {n.title}{tags_str} (modified {mod})")
    return "\n".join(lines)


async def _old_get_neighborhood(sessionmaker, uid, path, depth=1, limit=50):
    """The pre-change `get_neighborhood_impl` body, verbatim but for `_tracked`."""
    from sqlalchemy.orm import aliased

    depth = max(1, min(depth, 5))
    limit = max(1, min(limit, 200))
    async with sessionmaker() as session:
        src_stmt = select(NoteMetadata).where(
            NoteMetadata.file_path == path, tools._note_owner_predicate(uid)
        )
        source = (await session.execute(src_stmt)).scalar_one_or_none()
        assert source is not None
        seen: dict[int, dict] = {source.id: {"distance": 0, "via": None}}
        frontier: list[int] = [source.id]
        truncated = False
        for d in range(1, depth + 1):
            if not frontier:
                break
            SrcMeta = aliased(NoteMetadata)
            TgtMeta = aliased(NoteMetadata)
            stmt = (
                select(NoteLink.source_note_id, NoteLink.target_note_id)
                .join(SrcMeta, and_(
                    NoteLink.source_note_id == SrcMeta.id,
                    tools._owner_predicate_for(SrcMeta, uid),
                ))
                .join(TgtMeta, and_(
                    NoteLink.target_note_id == TgtMeta.id,
                    tools._owner_predicate_for(TgtMeta, uid),
                ))
                .where(
                    or_(
                        NoteLink.source_note_id.in_(frontier),
                        NoteLink.target_note_id.in_(frontier),
                    ),
                    NoteLink.target_note_id.isnot(None),
                )
            )
            edges = (await session.execute(stmt)).all()
            next_frontier: list[int] = []
            for src_id, tgt_id in edges:
                for from_id, to_id in ((src_id, tgt_id), (tgt_id, src_id)):
                    if from_id in seen and to_id not in seen:
                        seen[to_id] = {"distance": d, "via": from_id}
                        next_frontier.append(to_id)
                        if len(seen) - 1 >= limit:
                            truncated = True
                            break
                if truncated:
                    break
            frontier = next_frontier
            if truncated:
                break
        ids = [nid for nid in seen if nid != source.id]
        if not ids:
            return f"`{path}` has no resolved-link neighbors"
        meta_stmt = select(NoteMetadata).where(
            NoteMetadata.id.in_(ids), tools._note_owner_predicate(uid)
        )
        meta_rows = (await session.execute(meta_stmt)).scalars().all()
        meta_by_id = {m.id: m for m in meta_rows}
        ids = [i for i in ids if i in meta_by_id]
        if not ids:
            return f"`{path}` has no resolved-link neighbors"
        via_ids = {seen[nid]["via"] for nid in ids if seen[nid]["via"] is not None}
        via_paths = {source.id: source.file_path}
        if via_ids - {source.id}:
            via_stmt = select(NoteMetadata.id, NoteMetadata.file_path).where(
                NoteMetadata.id.in_(via_ids), tools._note_owner_predicate(uid)
            )
            for vid, vpath in (await session.execute(via_stmt)).all():
                via_paths[vid] = vpath
    ordered = sorted(ids, key=lambda nid: (seen[nid]["distance"], meta_by_id[nid].file_path))
    lines = [
        f"Neighborhood of `{path}` (depth ≤ {depth}, {len(ordered)} notes"
        + (", truncated" if truncated else "") + "):\n"
    ]
    for nid in ordered:
        m = meta_by_id[nid]
        info = seen[nid]
        via_path = via_paths.get(info["via"], "?")
        tags_str = f" [{', '.join(m.tags)}]" if m.tags else ""
        lines.append(
            f"- d={info['distance']} **{m.title}** (`{m.file_path}`){tags_str} via `{via_path}`"
        )
    return "\n".join(lines)


# ── the comparator: D7's definition of "identical" ──────────────────────────
class NotIdentical(AssertionError):
    pass


def _runs(keys):
    """[(start, end)) of maximal runs of equal consecutive keys."""
    out, start = [], 0
    for i in range(1, len(keys) + 1):
        if i == len(keys) or keys[i] != keys[start]:
            out.append((start, i))
            start = i
    return out


def _assert_identical(old, new, *, ident, key, limit, same_item):
    """Raise `NotIdentical` unless `new` equals `old` up to the permitted ties.

    `ident(item)` names a result (its path); `key(item)` is its exact sort key;
    `same_item(o, n)` compares everything else about two results with the same
    identity (and raises on a difference). Membership may differ only in the
    final run of equal keys when the list is exactly `limit` long (case a).
    Order may differ only inside a run of equal keys (case b).
    """
    if len(old) != len(new):
        raise NotIdentical(f"length {len(old)} != {len(new)}")
    ko, kn = [key(x) for x in old], [key(x) for x in new]
    if ko != kn:
        raise NotIdentical(f"sort keys differ: {ko} != {kn}")
    by_old = {ident(x): x for x in old}
    by_new = {ident(x): x for x in new}
    if len(by_old) != len(old) or len(by_new) != len(new):
        raise NotIdentical("a result appears twice")
    for start, end in _runs(kn):
        o_ids = {ident(x) for x in old[start:end]}
        n_ids = {ident(x) for x in new[start:end]}
        at_cutoff = end == len(new) and len(new) == limit
        if o_ids != n_ids and not at_cutoff:
            raise NotIdentical(
                f"tie run {start}:{end} differs and is not at the cutoff: "
                f"{sorted(o_ids ^ n_ids)}"
            )
        for p in o_ids & n_ids:
            same_item(by_old[p], by_new[p])


def _fields_equal(o, n, *, except_=()):
    for k in set(o) | set(n):
        if k in except_:
            continue
        if o.get(k, object()) != n.get(k, object()) or type(o.get(k)) is not type(n.get(k)):
            raise NotIdentical(f"{o.get('path')}: field {k!r}: {o.get(k)!r} != {n.get(k)!r}")


def _semantic_same(dist):
    """Per-item check for `semantic_search`: similarity within tolerance; the
    representative chunk may differ only between exactly equidistant chunks."""

    def check(o, n):
        if abs(o["similarity"] - n["similarity"]) > SIM_TOLERANCE:
            raise NotIdentical(
                f"{n['path']}: similarity {o['similarity']} vs {n['similarity']}"
            )
        if o["chunk_index"] != n["chunk_index"]:
            if dist[(o["path"], o["chunk_index"])] != dist[(n["path"], n["chunk_index"])]:
                raise NotIdentical(
                    f"{n['path']}: representative chunk changed without a tie"
                )
            _fields_equal(o, n, except_=("similarity", "chunk_index", "chunk"))
        else:
            _fields_equal(o, n, except_=("similarity",))

    return check


async def _chunk_distances(sessionmaker, vec):
    """Every chunk's exact distance, computed by the same operator the searches
    order by, so an exact tie there is an exact tie here."""
    d = NoteEmbedding.embedding.cosine_distance(vec)
    async with sessionmaker() as session:
        await session.execute(text("SET LOCAL enable_indexscan = off"))
        rows = (await session.execute(
            select(NoteMetadata.file_path, NoteEmbedding.chunk_index, d)
            .join(NoteMetadata, NoteEmbedding.note_id == NoteMetadata.id)
        )).all()
    return {(p, ci): float(x) for p, ci, x in rows}


async def _mtimes(sessionmaker):
    async with sessionmaker() as session:
        rows = (await session.execute(
            select(NoteMetadata.file_path, NoteMetadata.modified_at)
        )).all()
    return dict(rows)


_LINE = re.compile(r"^- `([^`]+)`")


def _rendered_items(out: str):
    """(header, [{"path", "line"}]) from a list-style tool render."""
    header, _, body = out.partition("\n\n")
    items = []
    for line in body.splitlines():
        m = _LINE.match(line)
        assert m, line
        items.append({"path": m.group(1), "line": line})
    return header, items


def _line_same(o, n):
    if o["line"] != n["line"]:
        raise NotIdentical(f"{o['line']!r} != {n['line']!r}")


def _assert_rendered_identical(old_out, new_out, *, limit, key):
    ho, io = _rendered_items(old_out)
    hn, inn = _rendered_items(new_out)
    if ho != hn:
        raise NotIdentical(f"header {ho!r} != {hn!r}")
    _assert_identical(io, inn, ident=lambda x: x["path"], key=key, limit=limit,
                      same_item=_line_same)
    return inn


def _desc_nulls_first_key(mt):
    # Postgres `DESC` puts NULLs first; the key only has to be equal on ties.
    return lambda x: mt[x["path"]]


# ── the oracle is strict: every non-permitted difference is rejected ────────
_BASE = [
    {"path": "a", "k": 3, "v": 1},
    {"path": "b", "k": 2, "v": 1},
    {"path": "c", "k": 2, "v": 1},
    {"path": "d", "k": 1, "v": 1},
]


def _cmp(old, new, limit=10):
    _assert_identical(
        old, new, ident=lambda x: x["path"], key=lambda x: x["k"], limit=limit,
        same_item=lambda o, n: _fields_equal(o, n),
    )


async def test_oracle_accepts_the_permitted_differences():
    _cmp(_BASE, copy.deepcopy(_BASE))
    swapped = copy.deepcopy(_BASE)
    swapped[1], swapped[2] = swapped[2], swapped[1]
    _cmp(_BASE, swapped)  # (b) order among exact ties
    cut_old = _BASE[:3]
    cut_new = [dict(x) for x in _BASE[:2]] + [{"path": "z", "k": 2, "v": 1}]
    _cmp(cut_old, cut_new, limit=3)  # (a) membership at a tied cutoff


async def test_oracle_rejects_a_reorder_across_keys():
    bad = copy.deepcopy(_BASE)
    bad[0], bad[3] = bad[3], bad[0]
    with pytest.raises(NotIdentical):
        _cmp(_BASE, bad)


async def test_oracle_rejects_a_changed_field():
    bad = copy.deepcopy(_BASE)
    bad[1]["v"] = 2
    with pytest.raises(NotIdentical):
        _cmp(_BASE, bad)


async def test_oracle_rejects_a_membership_change_inside_the_limit():
    bad = copy.deepcopy(_BASE)
    bad[2] = {"path": "z", "k": 2, "v": 1}
    with pytest.raises(NotIdentical):
        _cmp(_BASE, bad)  # the tie run is not at a cutoff


async def test_oracle_rejects_a_membership_change_at_an_untied_cutoff():
    bad = [dict(x) for x in _BASE[:3]]
    bad[0] = {"path": "z", "k": 3, "v": 1}
    with pytest.raises(NotIdentical):
        _cmp(_BASE[:3], bad, limit=3)


async def test_oracle_rejects_a_dropped_or_added_result():
    with pytest.raises(NotIdentical):
        _cmp(_BASE, _BASE[:3])
    with pytest.raises(NotIdentical):
        _cmp(_BASE[:3], _BASE)


async def test_oracle_rejects_a_chunk_change_without_a_distance_tie():
    dist = {("a", 0): 0.1, ("a", 1): 0.2, ("b", 0): 0.3, ("b", 1): 0.3}
    o = {"path": "a", "chunk_index": 0, "chunk": "x", "similarity": 0.9,
         "title": "a", "tags": [], "stale": False, "embedding_truncated": False}
    n = dict(o, chunk_index=1, chunk="y")
    with pytest.raises(NotIdentical):
        _semantic_same(dist)(o, n)
    ob = dict(o, path="b")
    _semantic_same(dist)(ob, dict(ob, chunk_index=1, chunk="y"))  # (c) permitted
    with pytest.raises(NotIdentical):
        _semantic_same(dist)(o, dict(o, similarity=0.9 + 2e-5))
    with pytest.raises(NotIdentical):
        _semantic_same(dist)(ob, dict(ob, chunk_index=1, chunk="y", stale=True))


# ── semantic_search ─────────────────────────────────────────────────────────
async def _semantic_pair(sessionmaker, embed_as, vec, **kw):
    """Run both arms as exact scans (see the module docstring): the old query's
    full-precision ordering has no index to use, so the new one must not walk
    the `halfvec` HNSW index either, or this compares recall, not projection."""
    embed_as(vec)
    async with sessionmaker() as session:
        await session.execute(text("SET LOCAL enable_indexscan = off"))
        old, old_fb = await _old_semantic_search(session, vec, **kw)
    token = timing.begin()
    try:
        async with sessionmaker() as session:
            await session.execute(text("SET LOCAL enable_indexscan = off"))
            new = await semantic_search(session, "q", **kw)
        new_fb = timing.current()["exact_fallback"]
    finally:
        timing.clear(token)
    return old, new, old_fb, new_fb


def _semantic_cases(corpus):
    bob = corpus["bob"]
    return {
        "user_scope": {"user_id": bob},
        "folder": {"folder": "B/", "user_id": bob},
        "tags": {"tags": ["reference"], "user_id": bob},
        "frontmatter": {"frontmatter": {"status": "open"}, "user_id": bob},
        "alice_scope": {"user_id": corpus["alice"]},
        "twin_folder": {"folder": "E/", "user_id": bob},
        "fallback_empty": {"folder": "D/"},
        "fallback_ownerless": {},
    }


@pytest.mark.parametrize("limit", [5, 15, 50])
async def test_semantic_search_is_identical(sessionmaker, corpus, queries, embed_as, limit):
    seen = {"stale": False, "truncated": False, "fallback": False, "results": 0}
    for qi, vec in enumerate(queries):
        dist = await _chunk_distances(sessionmaker, vec)
        for name, kw in _semantic_cases(corpus).items():
            old, new, old_fb, new_fb = await _semantic_pair(
                sessionmaker, embed_as, vec, limit=limit, **kw
            )
            assert old_fb == new_fb, (qi, name)
            seen["fallback"] |= new_fb
            _assert_identical(
                old, new, ident=lambda r: r["path"],
                key=lambda r: dist[(r["path"], r["chunk_index"])],
                limit=limit, same_item=_semantic_same(dist),
            )
            sims = [r["similarity"] for r in new]
            assert sims == sorted(sims, reverse=True), (qi, name)
            seen["results"] += len(new)
            seen["stale"] |= any(r["stale"] for r in new)
            seen["truncated"] |= any(r["embedding_truncated"] for r in new)
    # The corpus exercised what it claims to.
    assert seen["results"] > 0
    assert seen["stale"] and seen["truncated"] and seen["fallback"]


async def test_the_representative_chunk_tie_is_exercised(sessionmaker, corpus, queries,
                                                          embed_as):
    """Case (c): `E/twin.md`'s two chunks are exactly equidistant from query 0,
    so either may represent it. The new tie-break keeps the lower index."""
    vec = queries[0]
    dist = await _chunk_distances(sessionmaker, vec)
    assert dist[("E/twin.md", 0)] == dist[("E/twin.md", 1)]
    old, new, _, _ = await _semantic_pair(
        sessionmaker, embed_as, vec, limit=5, folder="E/", user_id=corpus["bob"]
    )
    assert [r["path"] for r in new][0] == "E/twin.md"
    assert new[0]["chunk_index"] == 0
    _assert_identical(
        old, new, ident=lambda r: r["path"],
        key=lambda r: dist[(r["path"], r["chunk_index"])],
        limit=5, same_item=_semantic_same(dist),
    )


# ── keyword_search ──────────────────────────────────────────────────────────
KEYWORD_QUERIES = ["meeting", "roadmap budget", "vault", "sprint planner", "nothing-here"]


@pytest.mark.parametrize("limit", [5, 20, 200])
async def test_keyword_search_is_identical(sessionmaker, corpus, limit):
    bob = corpus["bob"]
    cases = {
        "user_scope": {"user_id": bob},
        "folder": {"folder": "B/", "user_id": bob},
        "tags": {"tags": ["b1"], "user_id": bob},
        "frontmatter": {"frontmatter": {"status": "open"}, "user_id": bob},
        "ownerless": {},
    }
    cutoff_tie_seen = False
    for q in KEYWORD_QUERIES:
        for name, kw in cases.items():
            async with sessionmaker() as session:
                old = await _old_full_text_search(session, q, limit=limit, **kw)
            async with sessionmaker() as session:
                new = await full_text_search(session, q, limit=limit, **kw)
            _assert_identical(
                old, new, ident=lambda r: r["path"], key=lambda r: r["rank"],
                limit=limit, same_item=lambda o, n: _fields_equal(o, n),
            )
            if len(new) == limit and len(new) > 1 and new[-1]["rank"] == new[-2]["rank"]:
                cutoff_tie_seen = True
    if limit < 200:
        assert cutoff_tie_seen, "the corpus must put a rank tie at the cutoff"


# ── list_notes / get_recent ─────────────────────────────────────────────────
@pytest.mark.parametrize("limit", [20, 100, 500])
async def test_list_notes_is_identical(sessionmaker, corpus, limit):
    bob = corpus["bob"]
    mt = await _mtimes(sessionmaker)
    for kw in ({"folder": ""}, {"folder": "B/"}, {"folder": "T/"},
               {"folder": "", "tags": ["reference"]},
               {"folder": "", "frontmatter": {"status": "open"}}):
        old = await _old_list_notes(sessionmaker, bob, limit=limit, **kw)
        async with _as_authenticated_request(sessionmaker, bob):
            new = await tools.list_notes_impl(limit=limit, **kw)
        _assert_rendered_identical(old, new, limit=limit, key=_desc_nulls_first_key(mt))


@pytest.mark.parametrize("limit", [20, 100, 500])
async def test_get_recent_is_identical(sessionmaker, corpus, limit):
    bob = corpus["bob"]
    mt = await _mtimes(sessionmaker)
    for kw in ({}, {"folder": "B/"}, {"folder": "T/"}, {"tags": ["b2"]},
               {"frontmatter": {"status": "open"}}):
        old = await _old_get_recent(sessionmaker, bob, limit=limit, **kw)
        async with _as_authenticated_request(sessionmaker, bob):
            new = await tools.get_recent_impl(limit=limit, **kw)
        _assert_rendered_identical(old, new, limit=limit, key=_desc_nulls_first_key(mt))


async def test_the_modified_at_tie_cases_are_exercised(sessionmaker, corpus):
    """(a) a tie group larger than the limit is cut, and (b) two tied notes
    that both fit come back in `file_path` order, deterministically."""
    bob = corpus["bob"]
    mt = await _mtimes(sessionmaker)
    async with _as_authenticated_request(sessionmaker, bob):
        cut = await tools.get_recent_impl(limit=20, folder="B/")
        tied = await tools.get_recent_impl(limit=10, folder="T/")
    _, items = _rendered_items(cut)
    boundary = mt[items[-1]["path"]]
    group = [p for p, m in mt.items() if p.startswith("B/") and m == boundary]
    shown = [x["path"] for x in items if mt[x["path"]] == boundary]
    assert len(group) > len(shown), "case (a) did not occur"
    assert shown == sorted(shown)
    _, t_items = _rendered_items(tied)
    assert [x["path"] for x in t_items] == [
        "T/a-first.md", "T/c-tied.md", "T/m-tied.md", "T/z-last.md",
    ]
    assert mt["T/c-tied.md"] == mt["T/m-tied.md"]


# ── find_orphans ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("limit", [5, 50, 500])
async def test_find_orphans_is_identical(sessionmaker, corpus, limit):
    bob = corpus["bob"]
    mt = await _mtimes(sessionmaker)

    def key(x):
        m = mt[x["path"]]
        return (m is None, m)

    for folder in (None, "B/", "N/", "T/"):
        old = await _old_find_orphans(sessionmaker, bob, folder=folder, limit=limit)
        async with _as_authenticated_request(sessionmaker, bob):
            new = await tools.find_orphans_impl(folder=folder, limit=limit)
        _assert_rendered_identical(old, new, limit=limit, key=key)


async def test_orphans_with_no_modification_time_stay_last(sessionmaker, corpus):
    bob = corpus["bob"]
    mt = await _mtimes(sessionmaker)
    async with _as_authenticated_request(sessionmaker, bob):
        out = await tools.find_orphans_impl(folder="N/", limit=50)
    _, items = _rendered_items(out)
    nulls = [mt[x["path"]] is None for x in items]
    assert any(nulls) and not all(nulls)
    first_null = nulls.index(True)
    assert all(nulls[first_null:]), [x["path"] for x in items]
    null_paths = [x["path"] for x in items[first_null:]]
    assert null_paths == sorted(null_paths)


# ── get_neighborhood ────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "path,depth,limit",
    [("B/note-0003.md", 1, 50), ("B/note-0003.md", 3, 50),
     ("B/note-0000.md", 5, 200), ("B/note-0003.md", 2, 5)],
)
async def test_get_neighborhood_is_identical(sessionmaker, corpus, path, depth, limit):
    """The BFS is unchanged and orders by `(distance, file_path)`, which has no
    ties, so the whole render must be byte-equal to the pre-change one."""
    bob = corpus["bob"]
    old = await _old_get_neighborhood(sessionmaker, bob, path, depth=depth, limit=limit)
    async with _as_authenticated_request(sessionmaker, bob):
        new = await tools.get_neighborhood_impl(path, depth=depth, limit=limit)
    assert new.count("\n- d=") >= 1, new
    assert "[reference, b" in new, "tags must be rendered on some line"
    assert old == new
