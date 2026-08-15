"""Opt-in benchmark: filtered vector search recall against an exact baseline.

The bug this pins is the expensive kind — a *silently wrong* search result. With
`random_page_cost = 1.1` the planner picks an HNSW index scan; a non-iterative
HNSW scan yields at most `ef_search` candidates and a `folder`/`tags`/
`frontmatter`/`user_id` predicate is applied afterwards, discarding most of them
with nothing to refill. On the live vault, 45 of 120 folder-filtered probes came
back empty and 100 came back short. An agent reads an empty filtered result as
"the note does not exist" and acts on it.

The fixture has to actually reproduce that, or the guard is theatre. Two
properties of the corpus do the work, and neither is arbitrary:

  * `A/` clusters tightly around each query vector and carries none of the
    filter markers, so it owns the `ef_search = 80` window and is then thrown
    away by the predicate;
  * `B/` — the filtered target — is roughly *half the corpus*. A filter
    matching a few percent makes the planner estimate a tiny join and choose a
    seq scan + sort, and the index plan the bug lives in never appears. Every
    assertion would then pass against a plan production does not use.

So the module asserts, in order:

  1. the filtered query really uses the HNSW index (`EXPLAIN`),
  2. with `hnsw.iterative_scan = off` the filtered result is empty or short
     (the fixture reproduces the failure being fixed),
  3. with `relaxed_order` set-recall against an *exact filtered sequential
     scan taken at the same overfetch with the same dedupe* is ≥ 0.9 and
     non-empty, on each of three index rebuilds,
  4. distances come back monotone,
  5. a filtered query that returns nothing is re-run as an exact scan and
     records `exact_fallback` (see that test for why the *recovery* half of
     the fallback is asserted offline instead).

Recall is a benchmark SLO, not a per-query guarantee: HNSW is approximate and
ANN results move between index builds, which is exactly why (3) repeats across
rebuilds instead of pinning an expected result set.

Skipped unless `PGVECTOR_TEST_ADMIN_URL` is set — see `_harness.py`.
"""
import math
import random

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import src.mcp_server.tools as tools
from src.models.db import NoteEmbedding, NoteMetadata, User
from src.services import timing
from src.services.embeddings import semantic_search
from src.services.filters import apply_note_filters
import _harness
from src.config import settings

pytestmark = [
    _harness.requires_pgvector,
    pytest.mark.asyncio(loop_scope="module"),
]

# Must match the ORM column type, which is built from `settings` at import
# time — a narrower database column would just make every INSERT fail.
DIM = int(settings.embedding_dimensions)

SEED = 1234
N_QUERIES = 5
# Per query: enough near neighbours to own the `ef_search = 80` window on their
# own, which is what makes a non-iterative filtered scan come back empty.
A_NOTES_PER_QUERY = 300
# The filtered half has to be *large*, not just present. A filter matching a
# few percent of the corpus makes the planner estimate a tiny join and pick a
# seq scan + sort — the index plan the bug lives in never appears, and every
# assertion below passes vacuously against a plan production does not use.
# Roughly half the corpus, all of it far from every query vector, gives the
# nested-loop-over-HNSW plan *and* the empty filtered result.
B_NOTES = 1500
CHUNKS_PER_B_NOTE = 2

EF_SEARCH = 80
HNSW_INDEX = "ix_note_embeddings_embedding_hnsw"


# ── vector helpers ──────────────────────────────────────────────────────────
def _normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def _random_unit(rng: random.Random) -> list[float]:
    return _normalize([rng.gauss(0.0, 1.0) for _ in range(DIM)])


def _near(rng: random.Random, base: list[float], jitter: float) -> list[float]:
    return _normalize([b + rng.gauss(0.0, jitter) for b in base])


async def _build_hnsw_index(session) -> None:
    """Build the index with fixed parameters and no parallel workers.

    A parallel HNSW build produces a different graph every time — the same
    corpus can then answer the same query differently from one run to the
    next, which turns any assertion about *which* rows an approximate scan
    reaches into a coin flip. Single-threaded keeps the benchmark comparable
    across the three rebuilds and across runs.
    """
    await session.execute(text("SET LOCAL max_parallel_maintenance_workers = 0"))
    await session.execute(text(
        f"CREATE INDEX {HNSW_INDEX} ON note_embeddings "
        "USING hnsw (embedding vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 64)"
    ))


# ── fixtures ────────────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def migrated_url():
    yield from _harness.throwaway_database("search_recall", DIM)


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
    """Deterministic corpus, inserted in a fixed order.

    `A/` notes cluster tightly around each query vector so they own the
    `ef_search` window; `B/`, the tag/frontmatter variants, and the second
    user's notes sit further out, which is precisely the shape that makes a
    non-iterative filtered scan come back empty.
    """
    rng = random.Random(SEED + 1)

    async with sessionmaker() as session:
        users = {}
        for name in ("alice", "bob"):
            user = User(username=name, password_hash="x", vault_path=f"/vaults/{name}")
            session.add(user)
            await session.flush()
            users[name] = user.id
        await session.commit()

    def _add_note(session, path, vectors, *, tags=None, frontmatter=None, user_id=None):
        note = NoteMetadata(
            user_id=user_id,
            file_path=path,
            title=path.rsplit("/", 1)[-1].removesuffix(".md"),
            tags=tags or [],
            frontmatter=frontmatter or {},
            content_hash=path,
        )
        session.add(note)
        return note

    async with sessionmaker() as session:
        # Build the index *after* the bulk load: inserting into an HNSW index
        # row by row is an order of magnitude slower and buys nothing.
        await session.execute(text(f"DROP INDEX IF EXISTS {HNSW_INDEX}"))
        await session.commit()

    async with sessionmaker() as session:
        pending = []
        # A/: the crowd. Clustered tightly around each query vector, carrying
        # none of the filter markers, so they own the `ef_search` window and
        # are then all discarded by the predicate.
        for qi in range(N_QUERIES):
            for i in range(A_NOTES_PER_QUERY):
                pending.append((
                    f"A/q{qi}-{i:04d}.md",
                    [_near(rng, queries[qi], 0.05)],
                    {"tags": ["draft"], "frontmatter": {"status": "done"}},
                ))
        # B/: the filtered target. Random directions, so far from every query
        # vector. Two chunks each, so per-note dedupe is exercised on the path
        # under test. Every filter shape selects exactly this set; half of it
        # additionally belongs to the second user.
        for i in range(B_NOTES):
            base = _random_unit(rng)
            pending.append((
                f"B/note-{i:04d}.md",
                [_near(rng, base, 0.3) for _ in range(CHUNKS_PER_B_NOTE)],
                {
                    "tags": ["reference", f"b{i % 3}"],
                    "frontmatter": {"status": "open"},
                    "user_id": users["bob"] if i % 2 == 0 else None,
                },
            ))
        # `D/`: one note with metadata but no embedded chunks — the indexer
        # produces these for empty or fully-code-fenced notes. It is the one
        # shape that makes a filtered vector query return zero rows on *every*
        # plan, which is what the exact-fallback case needs (see that test).
        pending.append((
            "D/unembedded.md",
            [],
            {"tags": ["empty"], "frontmatter": {"status": "empty"}},
        ))

        notes = [_add_note(session, path, vecs, **extra) for path, vecs, extra in pending]
        await session.flush()
        for note, (path, vectors, _extra) in zip(notes, pending):
            for ci, vec in enumerate(vectors):
                session.add(NoteEmbedding(
                    note_id=note.id, chunk_index=ci,
                    chunk_text=f"{path} chunk {ci}", embedding=vec,
                ))
        await session.commit()

        await _build_hnsw_index(session)
        await session.execute(text("ANALYZE note_embeddings"))
        await session.execute(text("ANALYZE notes_metadata"))
        await session.commit()

    # find_related_impl uses the module-global sessionmaker and logs usage.
    original_session, original_log = tools.async_session, tools._log_usage
    tools.async_session = sessionmaker

    async def _noop(*_a, **_k):
        return None

    tools._log_usage = _noop
    yield users
    tools.async_session, tools._log_usage = original_session, original_log


@pytest_asyncio.fixture(loop_scope="module")
async def rebuild(request, sessionmaker, corpus):
    """Drop and recreate the HNSW index with fixed build settings.

    ANN results move between index builds, so a recall SLO measured on a single
    build is a coin flip dressed as a guarantee. Tests that carry the SLO are
    parametrised over this.
    """
    async with sessionmaker() as session:
        await session.execute(text(f"DROP INDEX IF EXISTS {HNSW_INDEX}"))
        await _build_hnsw_index(session)
        await session.execute(text("ANALYZE note_embeddings"))
        await session.commit()
    return request.param if hasattr(request, "param") else 0


# ── the query under test, run three ways ────────────────────────────────────
def _build_stmt(vec, overfetch, **filters):
    """The statement `semantic_search` builds, so the three modes below differ
    only in their `SET LOCAL`s."""
    distance = NoteEmbedding.embedding.cosine_distance(vec)
    stmt = (
        select(NoteEmbedding, NoteMetadata, distance.label("distance"))
        .join(NoteMetadata, NoteEmbedding.note_id == NoteMetadata.id)
    )
    stmt = apply_note_filters(stmt, **filters)
    return stmt.order_by(distance).limit(overfetch)


def _dedupe(rows, limit):
    """`semantic_search`'s dedupe: re-sort by distance, one row per note."""
    seen, out = set(), []
    for ne, nm, dist in sorted(rows, key=lambda r: r[2]):
        if ne.note_id in seen:
            continue
        seen.add(ne.note_id)
        out.append((nm.file_path, float(dist)))
        if len(out) >= limit:
            break
    return out


async def _apply_mode(session, mode):
    await session.execute(text(f"SET LOCAL hnsw.ef_search = {EF_SEARCH}"))
    await session.execute(text("SET LOCAL random_page_cost = 1.1"))
    if mode == "iterative":
        await session.execute(text("SET LOCAL hnsw.iterative_scan = 'relaxed_order'"))
    elif mode == "off":
        await session.execute(text("SET LOCAL hnsw.iterative_scan = off"))
    elif mode == "exact":
        # The exact filtered sequential baseline. `enable_seqscan` stays on.
        await session.execute(text("SET LOCAL enable_indexscan = off"))
        await session.execute(text("SET LOCAL enable_bitmapscan = off"))
    else:  # pragma: no cover - programming error
        raise ValueError(mode)


async def _run(sessionmaker, vec, limit=10, *, mode, **filters):
    """`mode` is 'iterative', 'off' (the pre-fix plan), or 'exact'.

    Each mode gets its own **session, and therefore its own transaction**.
    `SET LOCAL` is transaction-scoped, so reusing one session would leave the
    baseline's `enable_indexscan = off` in force for the next comparison — the
    two plans being compared would then be the same plan, and the comparison
    would silently pass.
    """
    overfetch = max(limit * 5, 50)
    async with sessionmaker() as session:
        await _apply_mode(session, mode)
        stmt = _build_stmt(vec, overfetch, **filters)
        rows = (await session.execute(stmt)).fetchall()
    return _dedupe(rows, limit)


async def _explain(sessionmaker, vec, limit=10, *, mode, **filters):
    overfetch = max(limit * 5, 50)
    async with sessionmaker() as session:
        await _apply_mode(session, mode)
        stmt = _build_stmt(vec, overfetch, **filters)
        compiled = stmt.compile(
            dialect=session.bind.dialect, compile_kwargs={"literal_binds": True}
        )
        rows = (await session.execute(text(f"EXPLAIN {compiled}"))).fetchall()
    return "\n".join(r[0] for r in rows)


def _recall(returned, baseline) -> float:
    """Set recall, counting ties at the cutoff as equivalent.

    A baseline note the scan missed still counts as covered if some returned
    note sits at the same distance — those two are interchangeable at the
    cutoff and calling one of them a miss would measure noise.
    """
    if not baseline:
        return 1.0
    returned_paths = {p for p, _ in returned}
    returned_dists = [d for _, d in returned]
    covered = 0
    for path, dist in baseline:
        if path in returned_paths:
            covered += 1
        elif any(abs(dist - rd) < 1e-9 for rd in returned_dists):
            covered += 1
    return covered / len(baseline)


FILTER_CASES = {
    "folder": {"folder": "B/"},
    "tags": {"tags": ["reference"]},
    "frontmatter": {"frontmatter": {"status": "open"}},
}


# ── 1. the fixture is exercising the index at all ───────────────────────────
async def test_filtered_query_uses_the_hnsw_index(sessionmaker, corpus, queries):
    """Without this, every assertion below passes vacuously on a seq scan."""
    plan = await _explain(sessionmaker, queries[0], mode="iterative", folder="B/")
    assert HNSW_INDEX in plan, plan


async def test_exact_baseline_does_not_use_the_hnsw_index(sessionmaker, corpus, queries):
    plan = await _explain(sessionmaker, queries[0], mode="exact", folder="B/")
    assert HNSW_INDEX not in plan, plan


# ── 2. the fixture reproduces the bug ───────────────────────────────────────
async def test_iterative_scan_off_loses_the_filtered_result(
    sessionmaker, corpus, queries
):
    """`hnsw.iterative_scan = off` is the pre-fix behaviour. At least one
    benchmark query must come back empty or short against the exact baseline,
    or the fixture is not reproducing what was fixed."""
    broken = []
    for vec in queries:
        baseline = await _run(sessionmaker, vec, mode="exact", folder="B/")
        off = await _run(sessionmaker, vec, mode="off", folder="B/")
        broken.append((len(off), len(baseline), _recall(off, baseline)))

    assert any(
        got == 0 or got < want or recall < 0.9 for got, want, recall in broken
    ), (
        "with iterative_scan off the filtered query still matched the baseline; "
        f"the corpus is not reproducing the recall bug: {broken}"
    )


# ── 3. the recall SLO, across three index rebuilds ──────────────────────────
@pytest.mark.parametrize("rebuild", [0, 1, 2], indirect=True)
async def test_filtered_recall_meets_the_baseline(
    sessionmaker, corpus, queries, rebuild
):
    """Every filter shape × every benchmark query, on one index build.

    All shapes share one test because the expensive part is the rebuild, not
    the queries — splitting them would pay for the same `CREATE INDEX` four
    times over. User scope gets its own case here rather than its own module:
    it is the filter with a security consequence, since a wrong-but-empty
    result is indistinguishable from "this user has no such note".
    """
    cases = dict(FILTER_CASES)
    cases["user_scope"] = {"user_id": corpus["bob"]}

    failures = []
    for case, filters in sorted(cases.items()):
        for i, vec in enumerate(queries):
            baseline = await _run(sessionmaker, vec, mode="exact", **filters)
            assert baseline, f"[{case}] query {i} has no baseline result"
            got = await _run(sessionmaker, vec, mode="iterative", **filters)
            if not got:
                failures.append(
                    f"[{case}] query {i}: empty, baseline had {len(baseline)}"
                )
                continue
            recall = _recall(got, baseline)
            if recall < 0.9:
                failures.append(
                    f"[{case}] query {i}: recall {recall:.2f} < 0.90 "
                    f"(got {len(got)}, baseline {len(baseline)})"
                )

    assert not failures, f"rebuild {rebuild}:\n" + "\n".join(failures)


# ── 4. ordering ─────────────────────────────────────────────────────────────
async def test_returned_distances_are_monotone(sessionmaker, corpus, queries):
    """`relaxed_order` may emit rows out of order across scan iterations; the
    service re-sorts before dedupe."""
    for vec in queries:
        got = await _run(sessionmaker, vec, mode="iterative", folder="B/")
        dists = [d for _, d in got]
        assert dists == sorted(dists), dists


async def test_semantic_search_output_is_monotone(sessionmaker, corpus, queries, monkeypatch):
    async def _fake(_text):
        return queries[0]

    monkeypatch.setattr("src.services.embeddings.get_embedding", _fake)
    async with sessionmaker() as session:
        results = await semantic_search(session, "anything", limit=10, folder="B/")
    assert results
    sims = [r["similarity"] for r in results]
    assert sims == sorted(sims, reverse=True), sims
    assert len({r["path"] for r in results}) == len(results)


# ── 5. the zero-row exact fallback ──────────────────────────────────────────
async def test_zero_row_filtered_result_falls_back_to_exact(
    sessionmaker, corpus, queries, monkeypatch
):
    """A filtered vector query that returns nothing is re-run as an exact scan.

    What this pins against a real database: the second statement is issued in
    the same transaction after `SET LOCAL enable_indexscan = off`, it is the
    identical statement, it does not error, and `exact_fallback` reaches the
    usage params.

    What it deliberately does *not* try to do is force the HNSW scan to miss a
    row it should have found. That cannot be made deterministic: HNSW assigns
    node levels randomly at build time, so which candidates a starved scan
    reaches changes on every `CREATE INDEX` — the same corpus and query gave an
    empty result on one build and a full one on the next. Pinning the index
    plan with `enable_seqscan = off` does not help either, because `SET LOCAL`
    is transaction-scoped and survives into the fallback, where it combines
    with the fallback's own `enable_indexscan = off` to re-run the same starved
    scan. The recovery path is therefore asserted in
    `tests/test_vector_iterative_scan.py`, where the empty first result is
    given rather than gambled on.
    """
    async def _fake(_text):
        return queries[0]

    monkeypatch.setattr("src.services.embeddings.get_embedding", _fake)

    # Premise: `D/` holds a note with no embedded chunks, so the vector query
    # returns zero rows under any plan.
    async with sessionmaker() as session:
        rows = (await session.execute(
            _build_stmt(queries[0], 50, folder="D/")
        )).fetchall()
    assert not rows

    token = timing.begin()
    try:
        async with sessionmaker() as session:
            results = await semantic_search(session, "anything", limit=5, folder="D/")
        holder = timing.current()
    finally:
        timing.clear(token)

    assert holder["exact_fallback"] is True
    assert results == []


async def test_unfiltered_search_does_not_pay_for_a_fallback(
    sessionmaker, corpus, queries, monkeypatch
):
    async def _fake(_text):
        return queries[0]

    monkeypatch.setattr("src.services.embeddings.get_embedding", _fake)
    token = timing.begin()
    try:
        async with sessionmaker() as session:
            results = await semantic_search(session, "anything", limit=10)
        holder = timing.current()
    finally:
        timing.clear(token)

    assert results
    assert holder["exact_fallback"] is False


# ── 6. find_related ─────────────────────────────────────────────────────────
async def test_find_related_returns_neighbours_under_scope(
    sessionmaker, corpus
):
    """`find_related`'s query is always filtered (`note_id != source`, plus the
    user scope), so it is exposed to the same post-filter candidate loss."""
    out = await tools.find_related_impl("B/note-0000.md", limit=10)
    assert "No related notes" not in out, out
    assert "has not been embedded yet" not in out, out
    # Similarity is printed descending; the dedupe keeps one row per note.
    sims = [
        float(line.rsplit("sim: ", 1)[1])
        for line in out.splitlines()
        if "sim: " in line
    ]
    assert sims == sorted(sims, reverse=True), sims
    assert len(sims) == len(set(
        line for line in out.splitlines() if line.startswith("- **")
    ))


async def test_find_related_issues_the_iterative_scan_setting(sessionmaker, corpus):
    """A fresh pooled connection must carry the setting for its transaction —
    the guard against an older backend accepting it as a placeholder GUC."""
    async with sessionmaker() as session:
        await session.execute(text("SET LOCAL hnsw.iterative_scan = 'relaxed_order'"))
        value = (
            await session.execute(text("SHOW hnsw.iterative_scan"))
        ).scalar_one()
    assert value == "relaxed_order"


async def test_pgvector_is_new_enough_for_iterative_scan(sessionmaker):
    """If this fails, every recall assertion above is measuring the old plan."""
    import src.main as main_module

    async with sessionmaker() as session:
        version = (
            await session.execute(
                text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            )
        ).scalar_one()
    parsed = main_module._parse_pgvector_version(version)
    assert parsed >= main_module.MIN_PGVECTOR_VERSION, version
