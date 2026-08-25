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
     the fallback is asserted offline instead),
  6. the same for `find_related` — the *production* tool, scored against an
     exact baseline built from the statement the tool itself issues.

Every assertion here runs against production code paths. A test that issued
`SET LOCAL hnsw.iterative_scan` itself and then observed its own setting would
prove only that Postgres remembers what it was told; the settings the services
issue are pinned offline in `tests/test_vector_iterative_scan.py`, and their
*effect* is what this module measures.

Recall is a benchmark SLO, not a per-query guarantee: HNSW is approximate and
ANN results move between index builds, which is exactly why (3) repeats across
rebuilds instead of pinning an expected result set.

(6) is the only case that goes through `_tracked`, and that makes it the only
one with an *identity* precondition: the decorator resolves the caller's vault
root before the tool body runs and refuses the whole call if it cannot (issue
#66). So it calls the tool inside `_as_authenticated_request`, which does what
`APIKeyMiddleware` does, and `_parse_find_related` refuses to score a refusal
as an empty result set — issue #99, where exactly that read as three recall
failures and cost an afternoon of search tuning.

Skipped unless `PGVECTOR_TEST_ADMIN_URL` is set — see `_harness.py`.
"""
import math
import random
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import src.mcp_server.tools as tools
from src.auth.session import current_vault_root
from src.models.db import NoteEmbedding, NoteMetadata, User
from src.services import timing
from src.services import vault as vault_service
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

# `find_related` takes no query vector: it averages a source note's own chunks.
# Each hub therefore gets a *ladder* of neighbours at deliberately separated
# cosines, so "the ten nearest notes" is a fact about the corpus rather than a
# coin flip. Without it the benchmark is unrunnable in the honest sense: every
# `B/` note is a uniform-random direction, the ten nearest sit inside a band
#0.006 wide, and which ten an approximate scan returns is noise — measured at
# 0.8–1.0 recall across index rebuilds of the *same* corpus. Real embeddings
# are not uniform in 1,024 dimensions; a note's genuine neighbours stand out.
# 0.03 between rungs is ~5× the width of that noise band.
FIND_RELATED_HUBS = 3
SATELLITES_PER_HUB = 15
SATELLITE_TOP_COSINE = 0.95
SATELLITE_COSINE_STEP = 0.03
# Tight enough that a note's own chunks are nearer to each other than the gap
# between rungs, so per-note dedupe cannot reorder the ladder.
CHUNK_JITTER = 0.001

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


def _at_cosine(rng: random.Random, base: list[float], cosine: float) -> list[float]:
    """A unit vector at *exactly* `cosine` from `base`.

    `_near` cannot do this: at 1,024 dimensions a per-component jitter of any
    useful size has a norm of `jitter × 32`, which swamps the unit base — two
    vectors built with `_near(base, 0.3)` are, in practice, independent random
    directions. Rotating `base` towards a random orthogonal direction places a
    vector at a distance we choose instead of one the dimensionality chooses.
    """
    orthogonal = _random_unit(rng)
    dot = sum(a * b for a, b in zip(orthogonal, base))
    orthogonal = _normalize([a - dot * b for a, b in zip(orthogonal, base)])
    sine = math.sqrt(max(0.0, 1.0 - cosine * cosine))
    return _normalize([cosine * b + sine * o for b, o in zip(base, orthogonal)])


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
    `ef_search` window; `B/` sits further out, which is precisely the shape
    that makes a non-iterative filtered scan come back empty. `alice` owns the
    `A/` crowd and `bob` owns all of `B/`, so the user scope selects the same
    set as the folder/tag/frontmatter filters and lands on the same plan.
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
                    {
                        "tags": ["draft"],
                        "frontmatter": {"status": "done"},
                        "user_id": users["alice"],
                    },
                ))
        # B/: the filtered target. Random directions, so far from every query
        # vector. Two chunks each, so per-note dedupe is exercised on the path
        # under test. Every filter shape selects exactly this set — including
        # the user scope, which is why the crowd belongs to `alice` and all of
        # `B/` to `bob` rather than the two being interleaved.
        #
        # That split is load-bearing for the *plan*, not just for tidiness. A
        # user scope matching a quarter of the corpus (the earlier shape: bob
        # owned every second `B/` note, `A/` belonged to nobody) makes the
        # planner estimate a small enough join to prefer a seq scan + sort over
        # the nested-loop-over-HNSW — measured, not guessed. The user-scope
        # recall case then scored an exact plan and passed for free, which is
        # the same trap the `B/`-is-half-the-corpus sizing above avoids.
        for i in range(B_NOTES):
            base = _random_unit(rng)
            pending.append((
                f"B/note-{i:04d}.md",
                [_near(rng, base, 0.3) for _ in range(CHUNKS_PER_B_NOTE)],
                {
                    "tags": ["reference", f"b{i % 3}"],
                    "frontmatter": {"status": "open"},
                    "user_id": users["bob"],
                },
            ))
        # `B/hub-*` and their satellites: the `find_related` benchmark's
        # subject. Same folder, tags, frontmatter and owner as the rest of
        # `B/`, so every filter shape still selects one set — only their
        # *geometry* is different, and only relative to their own hub.
        for h in range(FIND_RELATED_HUBS):
            base = _random_unit(rng)
            pending.append((
                f"B/hub-{h}.md",
                [_near(rng, base, CHUNK_JITTER) for _ in range(CHUNKS_PER_B_NOTE)],
                {
                    "tags": ["reference", f"b{h % 3}"],
                    "frontmatter": {"status": "open"},
                    "user_id": users["bob"],
                },
            ))
            for s in range(SATELLITES_PER_HUB):
                rung = _at_cosine(
                    rng, base, SATELLITE_TOP_COSINE - SATELLITE_COSINE_STEP * s
                )
                pending.append((
                    f"B/hub-{h}-sat-{s:02d}.md",
                    [_near(rng, rung, CHUNK_JITTER) for _ in range(CHUNKS_PER_B_NOTE)],
                    {
                        "tags": ["reference", f"b{s % 3}"],
                        "frontmatter": {"status": "open"},
                        "user_id": users["bob"],
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
    # `_user_vault_cache` is process-global and `_as_authenticated_request`
    # below writes to it, so the ids this module warms (`alice`/`bob`, from a
    # throwaway database that is about to be dropped) would otherwise ride
    # along into the rest of a whole-suite run. Measured, not assumed: nothing
    # in the suite currently collides with them — `test_issue_66_*` uses id
    # 4242 and its `cold_cache` fixture saves and restores the whole dict — so
    # this is hygiene rather than a fix for an observed failure. Keep it
    # anyway: an assigned root left in a shared dict is precisely the state
    # `_vault_root` refuses to be given for free, and a future module that
    # seeds a low user id would inherit a vault assignment it never made.
    vault_service.clear_user_vault_cache()


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


# ── calling a tool the way an authenticated MCP request does ────────────────
# Anything reached through `_tracked` passes the vault-admission gate first
# (issue #66): the decorator resolves `_vault_root(current_user_id.get())`
# *before* the tool body runs and fails the whole call when it raises. A test
# that binds only `current_user_id` therefore emulates half a request, and the
# half it leaves out is the precondition — every tool call comes back as the
# refusal string instead of results.
#
# So do exactly what `APIKeyMiddleware` does, both halves of it: warm the
# process-global cache from the database *and* bind the answer to this context
# as the request's own snapshot. Binding the ContextVar alone would be enough
# to pass the gate — `_vault_root` prefers the snapshot over the dict — but it
# would also be a fixture that cannot fail the way production fails: the whole
# point of a tool-level benchmark is that the real precondition is what it
# runs against, and in production that precondition is a `users.vault_path`
# row read through `warm_user_vault_cache`. Warming from the corpus's own user
# rows means an unassigned or inactive user would refuse here too.


@asynccontextmanager
async def _as_authenticated_request(sessionmaker, uid):
    """Bind the identity `APIKeyMiddleware` binds, for the body of a `with`."""
    async with sessionmaker() as session:
        root = await vault_service.warm_user_vault_cache(session, uid)
    assert root is not None, (
        f"the corpus fixture's user {uid} has no usable `vault_path`, so every "
        "`_tracked` tool call in this module would be refused before its body "
        "ran; fix the fixture, not the search code"
    )
    uid_token = tools.current_user_id.set(uid)
    root_token = current_vault_root.set((uid, root))
    try:
        yield root
    finally:
        current_vault_root.reset(root_token)
        tools.current_user_id.reset(uid_token)


def _assert_the_tool_body_ran(out: str, *, tool: str) -> None:
    """Fail on a `_tracked` refusal instead of scoring it as a search result.

    Every parser in this module reads a tool's *rendered* output, so a call
    the decorator refused arrives as a plain string with no result lines in
    it — indistinguishable, to a recall count, from a search that genuinely
    found nothing. That is not hypothetical: issue #99 was three parametrised
    recall failures reading `empty, baseline had 10`, and the actual cause was
    this module binding `current_user_id` without a vault root, so
    `find_related_impl` never executed a single statement. The failure text
    sent the investigation at HNSW tuning; the fix was in the harness.

    Keyed off the production constant rather than a copy of its wording, so a
    reworded refusal cannot quietly stop matching here.
    """
    if tools._NO_VAULT_MESSAGE in out:
        raise AssertionError(
            f"{tool} was REFUSED BEFORE ITS BODY RAN by the `_tracked` "
            "vault-admission gate (issue #66) — it issued no query, so this "
            "is a harness/precondition failure, NOT a recall or search "
            "regression. The calling test must establish the vault root the "
            "way `APIKeyMiddleware` does; use `_as_authenticated_request`. "
            f"Tool output was: {out!r}"
        )
    if out.startswith("Error:"):
        raise AssertionError(
            f"{tool} returned a tool-level error rather than results, so "
            "nothing here measures search quality: "
            f"{out!r}"
        )


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
        return await _harness.explain(session, _build_stmt(vec, overfetch, **filters))


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


def _cases(corpus):
    """The filter shapes, each carrying the owner predicate production carries.

    Since #127 the owner mapping is *total*: `apply_note_filters` appends
    `user_id IS NULL` when no user is given, so a shape without one no longer
    describes any statement this server issues — and against this corpus, whose
    every benchmark row belongs to `alice` or `bob`, it would select nothing and
    every recall assertion would pass on an empty baseline. `B/` is exactly
    bob's slice, so pairing each shape with his id keeps the selectivity (and
    therefore the plan) the sizing comments above were measured against.
    """
    owned = {"user_id": corpus["bob"]}
    cases = {name: {**f, **owned} for name, f in FILTER_CASES.items()}
    cases["user_scope"] = dict(owned)
    return cases


def _owned(corpus, **filters):
    """One filter shape plus the owner predicate — for the ad-hoc `B/` calls."""
    return {**filters, "user_id": corpus["bob"]}


# ── 1. the fixture is exercising the index at all ───────────────────────────
async def test_filtered_query_uses_the_hnsw_index(sessionmaker, corpus, queries):
    """Without this, every assertion below passes vacuously on a seq scan.

    Asserted for *every* filter shape the recall SLO covers, not just one.
    The shapes do not share a plan: they differ in what the planner thinks
    they select, and a shape that lands on seq scan + sort answers exactly
    (recall 1.0 by construction) while proving nothing about the approximate
    plan production runs. One shape did exactly that before this test was
    widened.
    """
    cases = _cases(corpus)
    missing = []
    for case, filters in sorted(cases.items()):
        plan = await _explain(sessionmaker, queries[0], mode="iterative", **filters)
        if HNSW_INDEX not in plan:
            missing.append(f"[{case}]\n{plan}")
    assert not missing, "\n\n".join(missing)


async def test_exact_baseline_does_not_use_the_hnsw_index(sessionmaker, corpus, queries):
    plan = await _explain(
        sessionmaker, queries[0], mode="exact", **_owned(corpus, folder="B/")
    )
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
        shape = _owned(corpus, folder="B/")
        baseline = await _run(sessionmaker, vec, mode="exact", **shape)
        off = await _run(sessionmaker, vec, mode="off", **shape)
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
    cases = _cases(corpus)

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
        got = await _run(
            sessionmaker, vec, mode="iterative", **_owned(corpus, folder="B/")
        )
        dists = [d for _, d in got]
        assert dists == sorted(dists), dists


async def test_semantic_search_output_is_monotone(sessionmaker, corpus, queries, monkeypatch):
    async def _fake(_text):
        return queries[0]

    monkeypatch.setattr("src.services.embeddings.get_embedding", _fake)
    async with sessionmaker() as session:
        results = await semantic_search(
            session, "anything", limit=10, folder="B/", user_id=corpus["bob"]
        )
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


async def test_a_non_empty_result_does_not_pay_for_a_fallback(
    sessionmaker, corpus, queries, monkeypatch
):
    """The fallback is armed on every query since #127 (the owner predicate is
    itself a filter), but it still only *fires* on zero rows.

    This case used to be "an unfiltered search does not pay for a fallback",
    passing no `user_id` at all. That shape no longer exists: the mapping is
    total, so such a call now means `user_id IS NULL` — which selects nothing
    here, since every benchmark row belongs to `alice` or `bob`. The property
    worth keeping is the cost one, so it is asserted on a scope that matches.
    """
    async def _fake(_text):
        return queries[0]

    monkeypatch.setattr("src.services.embeddings.get_embedding", _fake)
    token = timing.begin()
    try:
        async with sessionmaker() as session:
            results = await semantic_search(
                session, "anything", limit=10, user_id=corpus["bob"]
            )
        holder = timing.current()
    finally:
        timing.clear(token)

    assert results
    assert holder["exact_fallback"] is False


async def test_an_ownerless_scope_on_an_owned_corpus_falls_back(
    sessionmaker, corpus, queries, monkeypatch
):
    """And the shape that replaced it: `user_id IS NULL` against a database
    whose vectors all belong to named users. Under the old eligibility rule
    this counted as *unfiltered*, so the empty result was believed — while the
    predicate it carried was precisely the one discarding the HNSW window."""
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

    assert results == []
    assert holder["exact_fallback"] is True


# ── 6. find_related ─────────────────────────────────────────────────────────
# `find_related` gets the same treatment as `semantic_search`: the *production*
# entry point is called, and its output is scored against an exact baseline
# taken at the same overfetch with the same dedupe. It is the harder of the two
# to fake convincingly — it embeds nothing, so there is no query vector to hand
# it; the vector is the mean of the source note's own chunks, computed inside
# the tool. The baseline below therefore reconstructs that mean the same way
# and re-runs `tools.find_related_stmt`, the statement the tool itself issues.
FIND_RELATED_SOURCES = [f"B/hub-{h}.md" for h in range(FIND_RELATED_HUBS)]
FIND_RELATED_LIMIT = 10


async def _find_related_query_vector(sessionmaker, path: str, user_id: int):
    """`(source_id, mean chunk vector)` — exactly what the tool computes.

    `find_related_impl` averages the source note's chunk embeddings with
    `numpy.mean` and passes `.tolist()` to pgvector; anything else here would
    make the baseline answer a different question from the tool.
    """
    import numpy as np

    async with sessionmaker() as session:
        source = (await session.execute(
            select(NoteMetadata).where(
                NoteMetadata.file_path == path, NoteMetadata.user_id == user_id
            )
        )).scalar_one()
        chunks = (await session.execute(
            select(NoteEmbedding.embedding).where(
                NoteEmbedding.note_id == source.id
            )
        )).scalars().all()
    assert chunks, path
    avg = np.mean([np.asarray(c, dtype=float) for c in chunks], axis=0)
    return source.id, avg.tolist()


async def _find_related_baseline(sessionmaker, source_id, avg, user_id):
    """The exact filtered sequential answer, same statement, same dedupe.

    Its own session — `SET LOCAL enable_indexscan = off` is transaction-scoped,
    and sharing a transaction with the measured run would make both sides the
    same plan and the comparison vacuous.
    """
    async with sessionmaker() as session:
        await session.execute(text("SET LOCAL enable_indexscan = off"))
        await session.execute(text("SET LOCAL enable_bitmapscan = off"))
        stmt = tools.find_related_stmt(source_id, avg, user_id, FIND_RELATED_LIMIT)
        rows = (await session.execute(stmt)).all()

    seen, out = set(), []
    for r in sorted(rows, key=lambda r: r.distance):
        if r.note_id in seen:
            continue
        seen.add(r.note_id)
        out.append((r.file_path, float(r.distance)))
        if len(out) >= FIND_RELATED_LIMIT:
            break
    return out


def _parse_find_related(out: str) -> list[tuple[str, float]]:
    """`(path, distance)` per result line, from the tool's rendered output.

    The tool prints `sim: 1 - distance`, so the distance is recovered exactly
    at the printed precision — which is all the recall comparison needs, since
    ties are already counted as equivalent within 1e-9... except that rounding
    to three decimals is coarser than that. Distances are therefore compared
    with the printed tolerance in `_recall_printed` below.

    The admission check runs *here*, in the parser, rather than at the one call
    site: this is where a `_tracked` refusal would otherwise be silently turned
    into an empty result list, so every present and future caller inherits the
    guard by parsing through it.
    """
    _assert_the_tool_body_ran(out, tool="find_related")
    results = []
    for line in out.splitlines():
        if not line.startswith("- **") or "sim: " not in line:
            continue
        path = line.split("(`", 1)[1].split("`)", 1)[0]
        results.append((path, 1.0 - float(line.rsplit("sim: ", 1)[1])))
    return results


def _recall_printed(returned, baseline) -> float:
    """`_recall`, with the tolerance the tool's 3-decimal output allows."""
    if not baseline:
        return 1.0
    returned_paths = {p for p, _ in returned}
    returned_dists = [d for _, d in returned]
    covered = 0
    for path, dist in baseline:
        if path in returned_paths or any(
            abs(dist - rd) < 1e-3 for rd in returned_dists
        ):
            covered += 1
    return covered / len(baseline)


@pytest.mark.parametrize("rebuild", [0, 1, 2], indirect=True)
async def test_find_related_recall_meets_the_baseline(sessionmaker, corpus, rebuild):
    """The production tool, scored against an exact baseline, per rebuild.

    `find_related`'s query is always filtered (`note_id != source`, plus the
    user scope), so it is exposed to exactly the post-filter candidate loss
    that made folder-filtered `semantic_search` come back empty.
    """
    uid = corpus["bob"]
    async with sessionmaker() as session:
        owned = set((await session.execute(
            select(NoteMetadata.file_path).where(NoteMetadata.user_id == uid)
        )).scalars().all())

    async with _as_authenticated_request(sessionmaker, uid):
        failures = []
        for path in FIND_RELATED_SOURCES:
            source_id, avg = await _find_related_query_vector(sessionmaker, path, uid)
            baseline = await _find_related_baseline(sessionmaker, source_id, avg, uid)
            # Fixture integrity: the exact answer must be this hub's ladder, in
            # rung order. If the surrounding corpus ever crowds into it, the
            # ten nearest become a near-tie lottery again and the recall number
            # below stops meaning anything — it would drift with the index
            # build rather than with the code.
            stem = path.removesuffix(".md")
            assert [p for p, _ in baseline] == [
                f"{stem}-sat-{s:02d}.md" for s in range(FIND_RELATED_LIMIT)
            ], baseline

            out = await tools.find_related_impl(path, limit=FIND_RELATED_LIMIT)
            assert "has not been embedded yet" not in out, out
            # `_parse_find_related` refuses to read an admission refusal as an
            # empty result set — see `_assert_the_tool_body_ran`.
            got = _parse_find_related(out)

            if not got:
                failures.append(f"{path}: empty, baseline had {len(baseline)}")
                continue
            # Scope: a wrong-but-plausible neighbour from another user is the
            # failure this filter exists to prevent, and it would not show up
            # as a recall miss — the baseline is scoped too.
            stray = {p for p, _ in got} - owned
            assert not stray, f"{path} returned notes outside the user scope: {stray}"
            assert path not in {p for p, _ in got}, f"{path} returned itself"
            assert len({p for p, _ in got}) == len(got), got
            dists = [d for _, d in got]
            assert dists == sorted(dists), f"{path}: not monotone: {dists}"

            recall = _recall_printed(got, baseline)
            # Benchmark output — visible with `-s`, and the thing to look at
            # first when this test starts arguing with you.
            print(f"find_related recall rebuild={rebuild} {path}: {recall:.2f} "
                  f"(returned {len(got)} of {len(baseline)}, "
                  f"nearest {got[0][1]:.4f}, cutoff {got[-1][1]:.4f})")
            if recall < 0.9:
                failures.append(
                    f"{path}: recall {recall:.2f} < 0.90 "
                    f"(got {len(got)}, baseline {len(baseline)})"
                )
        assert not failures, f"rebuild {rebuild}:\n" + "\n".join(failures)


async def test_find_related_statement_uses_the_hnsw_index(sessionmaker, corpus):
    """Without this the recall assertions above could be scoring a seq scan —
    a plan production does not run, and one the bug cannot appear in."""
    uid = corpus["bob"]
    source_id, avg = await _find_related_query_vector(
        sessionmaker, FIND_RELATED_SOURCES[0], uid
    )
    stmt = tools.find_related_stmt(source_id, avg, uid, FIND_RELATED_LIMIT)
    async with sessionmaker() as session:
        # The three settings `find_related_impl` issues, in its order.
        await session.execute(text(f"SET LOCAL hnsw.ef_search = {EF_SEARCH}"))
        await session.execute(text("SET LOCAL random_page_cost = 1.1"))
        await session.execute(text("SET LOCAL hnsw.iterative_scan = 'relaxed_order'"))
        plan = await _harness.explain(session, stmt)
    assert HNSW_INDEX in plan, plan


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
