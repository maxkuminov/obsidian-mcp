"""The replayable rerank arms (#367, design D12).

Two factories, because `--arm-opt` is one dict shared by every arm of a run:
an option would rerank the incumbent `semantic` arm too, and the A/B would
compare the candidate with itself.

- `scripts.search_eval_arms.rerank:make_semantic_arm` (membership `semantic`)
  fetches its candidates with `embeddings.semantic_candidates` at
  `limit = min(depth, 50)` — the same up-to-50 notes production scores, so a
  note at vector rank 23 can be promoted at depth 20 exactly as
  `semantic_search(limit=20, rerank=true)` promotes it;
- `scripts.search_eval_arms.rerank:make_hybrid_arm` (membership
  `keyword_or_semantic`, hybrid's per-constituent corpus loss) takes the hybrid
  service's first 50 fused rows, with `hybrid_k` from `--arm-opt` (default
  `HYBRID_RRF_K`).

Both rerank through `src.services.rerank.rerank_candidates`, the function the
tools use, calling the services directly (never `_tracked`) with one fresh
read-only session per query, closed before the rerank exchange. They embed
each query through the configured provider, exactly as the incumbent
`semantic` arm does, and add one hop: `RERANK_URL`, through
`rerank_http_client` (design L15).

**Before the first answer each arm runs the startup probe** (the factory's
`preflight`, awaited by the runner after the replay URL is resolved and before
the generation is reserved): no `RERANK_URL` or a failing probe is a usage
error (exit 64). **`no_candidates` is an empty answer**: the query is scored as
returning no results and counted per arm as `empty_candidates` in the
manifest. **Every other answer that is not reranked fails the run** (exit 1, a
`failed` manifest, `UnrerankedAnswer`) — a timeout, an HTTP error, anything —
and so does a keyword-only hybrid answer: a degraded answer scored as the
reranker is wrong evidence. Raise `RERANK_TIMEOUT_MS` for
replay if the reranker is slow (L11), and say so in the report.
"""
from __future__ import annotations

from scripts.search_eval import UsageError
from scripts.search_eval.arms import Arm, ArmContext, EvalQuery

#: The services' page cap (and `RERANK_MAX_CANDIDATES`).
_MAX_LIMIT = 50


class UnrerankedAnswer(RuntimeError):
    """The reranker did not rerank this answer; the run must fail."""


async def preflight(options: dict[str, str]) -> None:
    """The D8 probe in this process, before any query is answered. Enables
    the module state the shared rerank function reads; raises `UsageError`
    (exit 64) when no reranker is configured or the probe fails."""
    from src.config import settings
    from src.services import rerank

    if not settings.rerank_url:
        raise UsageError("the rerank arms need RERANK_URL (the reranker to measure)")
    code = await rerank.probe_attempt(rerank.RERANK_PROBE_TIMEOUT_S)
    if code is not None:
        await rerank.close_rerank_client()
        raise UsageError(f"the reranker failed its probe ({code}); no rerank arm can run")
    rerank._set_enabled()


def _empty(outcome) -> bool:
    """`no_candidates`: the search found nothing, so there was nothing to
    rerank. An empty answer, scored as returning no results and counted in
    the manifest as `empty_candidates` — not an unreranked answer."""
    from src.services import rerank

    return not outcome.reranked and outcome.reason == rerank.NO_CANDIDATES


def _require(outcome) -> None:
    if not outcome.reranked:
        raise UnrerankedAnswer(
            f"rerank arm answered unreranked ({outcome.reason}); "
            "an unreranked answer is not evidence for the reranker"
        )


def make_semantic_arm(ctx: ArmContext) -> Arm:
    from src.services import rerank
    from src.services.embeddings import semantic_candidates

    async def arm(q: EvalQuery, depth: int) -> list[str]:
        limit = min(depth, _MAX_LIMIT)
        async with ctx.session_factory() as session:
            found = await semantic_candidates(
                session, q.text, limit=limit, folder=q.folder, tags=q.tags,
                frontmatter=q.frontmatter, user_id=q.user_id,
            )
        candidates = found.rows
        outcome = await rerank.rerank_candidates(
            q.text, [(r["title"], r.get("rerank_text")) for r in candidates]
        )
        if _empty(outcome):
            arm.empty_candidates += 1
            return []
        _require(outcome)
        return [candidates[i]["path"] for i in outcome.order][:limit]

    arm.empty_candidates = 0
    return arm


def make_hybrid_arm(ctx: ArmContext) -> Arm:
    try:
        from src.services.hybrid import ARMS_BOTH, hybrid_search
    except ImportError as e:  # pragma: no cover - the service ships with #354
        raise UsageError("make_hybrid_arm needs the hybrid_search service (#354)") from e
    from scripts.search_eval_arms.hybrid import _k
    from src.config import settings
    from src.services import rerank

    k = _k(ctx, "hybrid_k", settings.hybrid_rrf_k)

    async def arm(q: EvalQuery, depth: int) -> list[str]:
        limit = min(depth, _MAX_LIMIT)
        async with ctx.session_factory() as session:
            result = await hybrid_search(
                session, q.text, limit=limit, folder=q.folder, tags=q.tags,
                frontmatter=q.frontmatter, user_id=q.user_id, rrf_k=k,
                rerank_candidates=True,
            )
        if result.arms != ARMS_BOTH:
            raise UnrerankedAnswer(
                f"hybrid rerank arm answered keyword-only ({result.semantic_unavailable})"
            )
        candidates = result.rows
        outcome = await rerank.rerank_candidates(
            q.text, [(r.title, r.rerank_text) for r in candidates]
        )
        if _empty(outcome):
            arm.empty_candidates += 1
            return []
        _require(outcome)
        return [candidates[i].path for i in outcome.order][:limit]

    arm.empty_candidates = 0
    return arm


make_semantic_arm.membership = "semantic"
make_hybrid_arm.membership = "keyword_or_semantic"
for _f in (make_semantic_arm, make_hybrid_arm):
    _f.requires_db = True
    _f.preflight = preflight
