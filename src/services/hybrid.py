"""`hybrid_search`'s service: the keyword arm and the semantic arm in one
snapshot, fused by reciprocal rank fusion over note paths (#354).

The statement order (design D2), all on the one session the caller hands over
with no statement run:

1. `get_embedding(query)` — before any statement, so no pooled connection is
   held during the provider round trip. Any exception here makes the semantic
   arm unavailable (D4); steps 3b and 5 are skipped.
2. `SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY` — legal only
   before the first snapshot-taking statement, which is why it is first.
3. `SET LOCAL random_page_cost = 1.1`
   3b. `SET LOCAL hnsw.ef_search = 80`, `SET LOCAL hnsw.iterative_scan =
       'relaxed_order'` — every setting before the first data statement, so
       no setting changes between data statements except the exact-fallback
       pair in step 5.
4. The keyword arm (`search.keyword_arm_rows`, 50 rows, with the hybrid-only
   `no_excerpt` flag). **The snapshot is taken here** and serves every later
   statement, so a concurrent index commit cannot put a note in one arm and
   not the other within one call.
5. The semantic arm (`embeddings.semantic_arm_rows`, a 50-chunk overfetch,
   deduped, not truncated). After an exact re-run, `SET LOCAL
   enable_indexscan = on` restores the plan before anything else runs: the
   snippet statements below look rows up by id and must not inherit a
   disabled index scan.
6. Fusion, in Python.
7. Keyword snippets for the keyword-arm rows of the fused page, at most
   `KEYWORD_SNIPPET_ROWS` of them.
8. The caller closes the session, which ends the read-only transaction.

The keyword arm runs before the semantic arm so that the only data-plan
change (the exact fallback) can be followed only by its restore and the
snippet statements. A database error in either arm propagates: the arms share
one transaction, so there is no partial result, and there is no semantic-only
mode.

The service records the timing keys but **not** `record_results`: the tool
records the paths it actually rendered (D7), which the response bound can make
a prefix of this page.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from sqlalchemy import text

from src.config import settings
from src.services import embeddings, refusals, search, timing

logger = logging.getLogger(__name__)

#: How deep each arm is taken: the keyword arm's row limit and the semantic
#: arm's chunk overfetch. RRF is computed over these lists only (D3, L3).
HYBRID_ARM_DEPTH = 50

#: The page cap (owner decision 5): past it the fused order is mostly one
#: arm's tail.
HYBRID_MAX_LIMIT = 50

ARMS_BOTH = "both"
ARMS_KEYWORD_ONLY = "keyword_only"
UNAVAILABLE_INPUT_REJECTED = "provider_input_rejected"
UNAVAILABLE_PROVIDER_ERROR = "provider_error"

#: Test seam: awaited with the session between the keyword arm (step 4) and
#: the semantic arm (step 5), when set. The integration suite commits a note
#: on another connection here to prove the one-snapshot property. Never set
#: in production code.
_between_arms_hook: Callable[[object], Awaitable[None]] | None = None


@dataclass
class HybridRow:
    path: str
    title: str | None
    tags: list[str] | None
    score: float
    keyword_rank: int | None = None
    semantic_rank: int | None = None
    # Keyword-arm fields.
    match: str | None = None
    matched_in: list[str] | None = None
    snippet: str | None = None
    head: str | None = None
    no_excerpt: bool = False
    # Semantic-arm fields.
    similarity: float | None = None
    chunk: str | None = None
    stale: bool = False
    embedding_truncated: bool = False

    @property
    def best_rank(self) -> int:
        return min(r for r in (self.keyword_rank, self.semantic_rank) if r is not None)


@dataclass
class HybridResult:
    rows: list[HybridRow] = field(default_factory=list)
    arms: str = ARMS_BOTH
    semantic_unavailable: str | None = None
    rrf_k: int = 60
    #: Notes in the keyword arm, and its `all` / `any` split.
    keyword_rows: int = 0
    keyword_all: int = 0
    keyword_any: int = 0
    #: Notes in the semantic arm (after the dedupe), and the chunk rows the
    #: statement returned before it — `arm_depth`'s `c`.
    semantic_rows: int = 0
    semantic_chunks: int = 0
    #: Notes in both arms.
    overlap: int = 0
    #: `KEYWORD_SNIPPET_ROWS` as it stood for this call.
    snippet_cap: int = 0


def fuse(
    keyword_paths: list[str], semantic_paths: list[str], k: int
) -> list[tuple[str, float, int | None, int | None]]:
    """Reciprocal rank fusion over paths: `(path, score, keyword_rank,
    semantic_rank)`, ordered by score DESC, then the smaller of the note's arm
    ranks ASC, then path ASC — a tie-break that favours no arm (D3). Ranks are
    1-based positions; each list holds a path at most once."""
    kr = {p: i + 1 for i, p in enumerate(keyword_paths)}
    sr = {p: i + 1 for i, p in enumerate(semantic_paths)}
    out = []
    for p in dict.fromkeys([*keyword_paths, *semantic_paths]):
        k_rank, s_rank = kr.get(p), sr.get(p)
        score = sum(1.0 / (k + r) for r in (k_rank, s_rank) if r is not None)
        best = min(r for r in (k_rank, s_rank) if r is not None)
        out.append((p, score, k_rank, s_rank, best))
    out.sort(key=lambda t: (-t[1], t[4], t[0]))
    return [t[:4] for t in out]


async def hybrid_search(
    session,
    query: str,
    *,
    limit: int = 10,
    folder: str | None = None,
    tags: list[str] | None = None,
    frontmatter: dict | None = None,
    user_id: int | None = None,
    rrf_k: int | None = None,
) -> HybridResult:
    """Both arms in one read-only repeatable-read snapshot, fused (D1–D4)."""
    limit = max(1, min(limit, HYBRID_MAX_LIMIT))
    k = int(rrf_k) if rrf_k is not None else settings.hybrid_rrf_k
    filters = dict(folder=folder, tags=tags, frontmatter=frontmatter, user_id=user_id)

    # ── 1. The embedding, before any statement (D2, D4) ─────────────────────
    query_embedding = None
    unavailable: str | None = None
    embed_start = time.monotonic()
    try:
        query_embedding = await embeddings.get_embedding(query)
    except Exception as exc:  # noqa: BLE001 — every provider failure degrades
        unavailable = (
            UNAVAILABLE_INPUT_REJECTED
            if isinstance(exc, refusals.ProviderInputTooLarge)
            else UNAVAILABLE_PROVIDER_ERROR
        )
        # The class name only, never the message: a provider's message can
        # echo the query (D4).
        logger.warning(
            "hybrid_search: semantic arm unavailable (%s, %s); returning the "
            "keyword arm alone",
            unavailable,
            type(exc).__name__,
        )
    timing.add_ms("embed_ms", time.monotonic() - embed_start)

    db_start = time.monotonic()
    # ── 2–3b. The transaction and every setting ─────────────────────────────
    await session.execute(
        text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
    )
    await session.execute(text("SET LOCAL random_page_cost = 1.1"))
    if unavailable is None:
        await session.execute(text("SET LOCAL hnsw.ef_search = 80"))
        await session.execute(text("SET LOCAL hnsw.iterative_scan = 'relaxed_order'"))

    # ── 4. The keyword arm (the snapshot is taken here) ─────────────────────
    k_arm = await search.keyword_arm_rows(
        session, query, limit=HYBRID_ARM_DEPTH, with_excerpt_flag=True, **filters
    )

    if _between_arms_hook is not None:
        await _between_arms_hook(session)

    # ── 5. The semantic arm, and the restore after an exact re-run ──────────
    s_rows: list[dict] = []
    s_chunks = 0
    exact_fallback = False
    if unavailable is None:
        s_arm = await embeddings.semantic_arm_rows(
            session, query_embedding, overfetch=HYBRID_ARM_DEPTH, **filters
        )
        s_rows, s_chunks, exact_fallback = (
            s_arm.rows, s_arm.chunk_rows, s_arm.exact_fallback
        )
        if exact_fallback:
            await session.execute(text("SET LOCAL enable_indexscan = on"))

    # ── 6. Fusion ───────────────────────────────────────────────────────────
    k_by_path = {r["path"]: r for r in k_arm.rows}
    s_by_path = {r["path"]: r for r in s_rows}
    fused = fuse([r["path"] for r in k_arm.rows], [r["path"] for r in s_rows], k)
    page: list[HybridRow] = []
    for path, score, k_rank, s_rank in fused[:limit]:
        kr, sr = k_by_path.get(path), s_by_path.get(path)
        src = kr if kr is not None else sr
        row = HybridRow(
            path=path, title=src["title"], tags=src["tags"], score=score,
            keyword_rank=k_rank, semantic_rank=s_rank,
        )
        if kr is not None:
            row.match = kr["match"]
            row.no_excerpt = bool(kr.get("no_excerpt"))
        if sr is not None:
            row.similarity = sr["similarity"]
            row.chunk = sr["chunk"]
            row.stale = bool(sr["stale"])
            row.embedding_truncated = bool(sr["embedding_truncated"])
        page.append(row)

    # ── 7. Keyword snippets for the page's keyword rows, capped ─────────────
    cap = settings.keyword_snippet_rows
    started = time.monotonic()
    if cap > 0:
        snippet_rows = [k_by_path[r.path] for r in page if r.path in k_by_path][:cap]
        if snippet_rows:
            await search.keyword_snippets(session, snippet_rows, k_arm)
            for kd in snippet_rows:
                row = next(r for r in page if r.path == kd["path"])
                row.snippet = kd.get("snippet")
                row.head = kd.get("head")
                row.matched_in = kd.get("matched_in") or None
    # Recorded on every call, as 0-ish when the cap is off (#354 D7): the key
    # is part of every hybrid usage row.
    timing.add_ms("snippet_ms", time.monotonic() - started)
    timing.add_ms("db_ms", time.monotonic() - db_start)

    overlap = len(k_by_path.keys() & s_by_path.keys())
    keyword_any = sum(1 for r in k_arm.rows if r["match"] == "any")
    result = HybridResult(
        rows=page,
        arms=ARMS_BOTH if unavailable is None else ARMS_KEYWORD_ONLY,
        semantic_unavailable=unavailable,
        rrf_k=k,
        keyword_rows=len(k_arm.rows),
        keyword_all=len(k_arm.rows) - keyword_any,
        keyword_any=keyword_any,
        semantic_rows=len(s_rows),
        semantic_chunks=s_chunks,
        overlap=overlap,
        snippet_cap=cap,
    )

    # ── Telemetry (D7); `result_count` / `result_paths` are the tool's ──────
    timing.record("exact_fallback", exact_fallback)
    timing.record("fallback", k_arm.fallback)
    timing.record("any_rows", keyword_any)
    timing.record("arms", result.arms)
    if unavailable is not None:
        timing.record("semantic_unavailable", unavailable)
    timing.record("keyword_rows", result.keyword_rows)
    timing.record("semantic_rows", result.semantic_rows)
    timing.record("overlap", overlap)
    timing.record("rrf_k", k)
    return result
