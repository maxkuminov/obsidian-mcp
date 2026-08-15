"""Opt-in benchmark: `full_text_search` uses its tsvector index, and the
planner hint does not change what matches.

On the live vault the keyword query planned as a Seq Scan on every call — 5
index scans lifetime against 3,655 sequential ones — because the planner costs
the `notes_metadata` heap at `relpages` and does not model detoast I/O. Each
query therefore detoasted all ~3,800 tsvectors out of a 36 MB TOAST table:
13,086 buffers, 26–37 ms warm, 3,926 disk reads cold.

The corpus here has to be big enough and its tsvectors TOASTed enough for that
cost model to matter; a few dozen short notes would fit in-line and the planner
would be right to seq-scan them. Hence 3,000+ notes with multi-kilobyte bodies.

Two claims are separated on purpose:

  * **Semantics are unchanged** — asserted for rare *and* common terms across
    every filter shape. This is the one that would be a correctness bug, and
    it is why the change also added the `file_path` tie-break: `ts_rank_cd`
    produces many ties, so a plan change would otherwise silently reshuffle
    which tied rows survive the LIMIT.
  * **The index is used** — asserted only for the rare term, which is the case
    the hint exists for. A very common term legitimately seq-scans; its plan
    and buffer counts are recorded, not asserted.

Skipped unless `PGVECTOR_TEST_ADMIN_URL` is set — see `_harness.py`.
"""
import random

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import _harness
from src.services.fts import index_tsvector_sql
from src.services.search import full_text_search

pytestmark = [
    _harness.requires_pgvector,
    pytest.mark.asyncio(loop_scope="module"),
]

DIM = 64  # irrelevant here; keeps the migration cheap.
SEED = 4321
N_NOTES = 3200
TSVECTOR_INDEX = "ix_notes_metadata_tsvector"

# A term planted in a handful of notes (what the hint is for) and one planted
# in most of them (where a seq scan is a legitimate plan).
RARE_TERM = "zygomorphic"
COMMON_TERM = "meeting"

_WORDS = [
    "project", "meeting", "roadmap", "budget", "invoice", "deadline",
    "retrospective", "migration", "database", "vector", "index", "planner",
    "obsidian", "vault", "backlog", "sprint", "review", "estimate",
    "architecture", "latency", "throughput", "incident", "postmortem",
]


def _body(rng: random.Random, index: int) -> str:
    """A multi-kilobyte body, so the tsvector is large enough to TOAST."""
    words = [rng.choice(_WORDS) for _ in range(900)]
    if index % 400 == 0:
        words.append(RARE_TERM)
    if index % 3 != 0:
        words.append(COMMON_TERM)
    return " ".join(words)


@pytest.fixture(scope="module")
def migrated_url():
    yield from _harness.throwaway_database("keyword_plan", DIM)


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def sessionmaker(migrated_url):
    engine = create_async_engine(migrated_url, poolclass=None)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def corpus(sessionmaker):
    rng = random.Random(SEED)
    fragment, cfg_params = index_tsvector_sql()

    async with sessionmaker() as session:
        user = (await session.execute(
            text(
                "INSERT INTO users (username, password_hash, is_admin, is_active, "
                "session_version, vault_path) "
                "VALUES ('bob', 'x', false, true, 1, '/vaults/bob') RETURNING id"
            )
        )).scalar_one()

        stmt = text(
            "INSERT INTO notes_metadata "
            "(user_id, file_path, title, tags, frontmatter, content_hash, "
            " content_tsvector) "
            f"VALUES (:user_id, :path, :title, :tags, CAST(:fm AS jsonb), :hash, {fragment})"
        )
        for i in range(N_NOTES):
            folder = "Projects" if i % 2 == 0 else "Archive"
            await session.execute(stmt, {
                "user_id": user if i % 5 == 0 else None,
                "path": f"{folder}/note-{i:05d}.md",
                "title": f"note {i}",
                "tags": ["work"] if i % 4 == 0 else ["personal"],
                "fm": '{"status": "open"}' if i % 7 == 0 else '{"status": "done"}',
                "hash": f"h{i}",
                "content": _body(rng, i),
                **cfg_params,
            })
            if i % 400 == 0:
                await session.commit()
        await session.commit()

    # `VACUUM`, not just `ANALYZE`. A GIN index's cost estimate comes from its
    # *metapage* statistics, and those are written by VACUUM — `ANALYZE` alone
    # leaves them empty, and `gincostestimate` then assumes the whole index has
    # to be scanned. On this corpus that is the difference between costing the
    # bitmap scan at 621 (never chosen) and at 4.15 (always chosen), so without
    # it the module would "prove" that the planner hint does not work. A real
    # deployment gets this from autovacuum; a freshly-seeded one does not.
    # VACUUM cannot run inside a transaction block, hence AUTOCOMMIT.
    engine = sessionmaker.kw["bind"]
    async with engine.connect() as conn:
        conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
        await conn.execute(text("VACUUM ANALYZE notes_metadata"))
    return user


FILTER_CASES = {
    "none": {},
    "folder": {"folder": "Projects/"},
    "tags": {"tags": ["work"]},
    "frontmatter": {"frontmatter": {"status": "open"}},
}


async def _search_with_baseline_plan(session, query, **filters):
    """The same query with the hint neutralised, so the comparison isolates the
    plan rather than the SQL."""
    await session.execute(text("SET LOCAL enable_indexscan = off"))
    await session.execute(text("SET LOCAL enable_bitmapscan = off"))
    return await full_text_search(session, query, limit=50, **filters)


@pytest.mark.parametrize("term", [RARE_TERM, COMMON_TERM])
@pytest.mark.parametrize("case", sorted(FILTER_CASES))
async def test_results_identical_with_and_without_the_planner_setting(
    sessionmaker, corpus, term, case
):
    """Membership *and* order, tie-break included. This is the assertion that
    makes the hint safe to ship."""
    filters = dict(FILTER_CASES[case])
    async with sessionmaker() as session:
        with_hint = await full_text_search(session, term, limit=50, **filters)
    async with sessionmaker() as session:
        seq = await _search_with_baseline_plan(session, term, **filters)

    assert [r["path"] for r in with_hint] == [r["path"] for r in seq], (
        f"[{case}/{term}] the planner setting changed the result order"
    )
    assert {r["path"] for r in with_hint} == {r["path"] for r in seq}


async def test_user_scope_is_unchanged_by_the_setting(sessionmaker, corpus):
    uid = corpus
    async with sessionmaker() as session:
        with_hint = await full_text_search(session, COMMON_TERM, limit=50, user_id=uid)
    async with sessionmaker() as session:
        seq = await _search_with_baseline_plan(session, COMMON_TERM, user_id=uid)
    assert [r["path"] for r in with_hint] == [r["path"] for r in seq]
    assert with_hint, "the user-scoped corpus should match the common term"


async def test_ordering_is_deterministic_across_repeated_runs(sessionmaker, corpus):
    """Without the `file_path` tie-break, tied-rank rows come back in whatever
    order the plan produced them."""
    runs = []
    for _ in range(3):
        async with sessionmaker() as session:
            runs.append([r["path"] for r in await full_text_search(
                session, COMMON_TERM, limit=50
            )])
    assert runs[0] == runs[1] == runs[2]


async def _explain_buffers(session, term, *, hint: bool) -> tuple[str, int]:
    """Run the real query shape under EXPLAIN (ANALYZE, BUFFERS)."""
    if hint:
        await session.execute(text("SET LOCAL random_page_cost = 1.1"))
    else:
        await session.execute(text("SET LOCAL enable_indexscan = off"))
        await session.execute(text("SET LOCAL enable_bitmapscan = off"))
    rows = (await session.execute(
        text(
            "EXPLAIN (ANALYZE, BUFFERS) "
            "SELECT notes_metadata.*, "
            "  ts_rank_cd(content_tsvector, websearch_to_tsquery('english', :q)) AS rank "
            "FROM notes_metadata "
            "WHERE content_tsvector @@ websearch_to_tsquery('english', :q) "
            "ORDER BY rank DESC, file_path ASC LIMIT 50"
        ),
        {"q": term},
    )).fetchall()
    plan = "\n".join(r[0] for r in rows)
    buffers = 0
    for line in plan.splitlines():
        if "Buffers:" in line:
            for part in line.split("Buffers:", 1)[1].split(","):
                part = part.strip()
                if part.startswith("shared"):
                    for token in part.split():
                        if token.startswith("read=") or token.startswith("hit="):
                            buffers += int(token.split("=", 1)[1])
    return plan, buffers


async def test_rare_term_uses_the_tsvector_index_and_reads_fewer_buffers(
    sessionmaker, corpus
):
    async with sessionmaker() as session:
        hinted_plan, hinted_buffers = await _explain_buffers(
            session, RARE_TERM, hint=True
        )
    async with sessionmaker() as session:
        seq_plan, seq_buffers = await _explain_buffers(
            session, RARE_TERM, hint=False
        )

    print(f"\n[rare/{RARE_TERM}] hinted buffers={hinted_buffers}\n{hinted_plan}")
    print(f"\n[rare/{RARE_TERM}] seq buffers={seq_buffers}\n{seq_plan}")

    assert TSVECTOR_INDEX in hinted_plan, hinted_plan
    assert "Seq Scan" in seq_plan, seq_plan
    assert hinted_buffers < seq_buffers, (
        f"index plan read {hinted_buffers} buffers vs {seq_buffers} sequential"
    )


async def test_common_term_plans_are_recorded_not_asserted(sessionmaker, corpus):
    """A very common term may legitimately seq-scan; the hint is not a promise
    of index usage for every query shape. Recorded so a future regression has a
    reference point."""
    async with sessionmaker() as session:
        hinted_plan, hinted_buffers = await _explain_buffers(
            session, COMMON_TERM, hint=True
        )
    async with sessionmaker() as session:
        seq_plan, seq_buffers = await _explain_buffers(
            session, COMMON_TERM, hint=False
        )
    print(f"\n[common/{COMMON_TERM}] hinted buffers={hinted_buffers}\n{hinted_plan}")
    print(f"\n[common/{COMMON_TERM}] seq buffers={seq_buffers}\n{seq_plan}")
    assert hinted_plan and seq_plan


async def test_tsvectors_are_actually_toasted(sessionmaker, corpus):
    """If the bodies fit in-line, this module is not measuring the cost model
    gap it exists for."""
    async with sessionmaker() as session:
        toast_bytes = (await session.execute(text(
            "SELECT pg_total_relation_size(reltoastrelid) FROM pg_class "
            "WHERE relname = 'notes_metadata'"
        ))).scalar_one()
    print(f"\nnotes_metadata TOAST size: {toast_bytes} bytes")
    assert toast_bytes > 1_000_000, toast_bytes
