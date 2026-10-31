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

- `…:make_semantic_arm_incumbent` / `…:make_hybrid_arm_incumbent` (#402,
  rerank-documents D10): the **baseline arms** — the same candidates, sent
  with the frozen pre-#402 builder, one `title + chunk|excerpt` document per
  note. Use one as `BASELINE` against the matching arm above, at `--depth`
  10, 20 and 50 (the call limit is the depth).

All four rerank through `src.services.rerank.rerank_groups`; the two
production arms allocate with `semantic_document_groups` /
`hybrid_document_groups`, the functions the tools use, calling the services directly (never `_tracked`) with one fresh
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


# ── The incumbent builder, frozen (rerank-documents D10) ───────────────────
#
# The document construction before #402, copied here so the baseline arms can
# replay it after `src/` moved on: one `title + "\n\n" + text` document per
# candidate note, the text being the cosine-best chunk (a fresh semantic row)
# or the excerpt (any other hybrid row), cut to 2,048 code points. **Frozen:
# never edit these two functions**; a different incumbent is a new arm.

_INCUMBENT_DOC_CHARS = 2048


def _incumbent_bounded_text(title: str | None, text: str | None) -> str | None:
    if text is None:
        return None
    room = _INCUMBENT_DOC_CHARS - len(title or "") - 2
    return text[: max(0, room)]


def _incumbent_document(title: str | None, text: str | None) -> str:
    title = title or ""
    doc = f"{title}\n\n{text}" if text else title
    return doc[:_INCUMBENT_DOC_CHARS]


def _semantic_incumbent_groups(rows: list[dict]) -> list[list[str]]:
    return [[_incumbent_document(
        r["title"], None if r["stale"] else _incumbent_bounded_text(r["title"], r["chunk"]))]
        for r in rows]


def _hybrid_incumbent_groups(rows) -> list[list[str]]:
    out = []
    for r in rows:
        if r.semantic_rank is not None and not r.stale and r.chunk is not None:
            text = _incumbent_bounded_text(r.title, r.chunk)
        else:
            text = r.rerank_text  # the excerpt, already bounded by `_fill_excerpts`
        out.append([_incumbent_document(r.title, text)])
    return out


def _production_semantic_groups(rows):
    from src.services import rerank

    return [[d.text for d in g] for g in rerank.semantic_document_groups(rows)]


def _production_hybrid_groups(rows):
    from src.services import rerank

    return [[d.text for d in g] for g in rerank.hybrid_document_groups(rows)]


def _semantic_arm(ctx: ArmContext, groups_of) -> Arm:
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
        outcome = await rerank.rerank_groups(q.text, groups_of(candidates))
        if _empty(outcome):
            arm.empty_candidates += 1
            return []
        _require(outcome)
        return [candidates[i]["path"] for i in outcome.order][:limit]

    arm.empty_candidates = 0
    return arm


def _hybrid_arm(ctx: ArmContext, groups_of) -> Arm:
    try:
        from src.services.hybrid import ARMS_BOTH, hybrid_search
    except ImportError as e:  # pragma: no cover - the service ships with #354
        raise UsageError("the hybrid rerank arms need the hybrid_search service (#354)") from e
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
        outcome = await rerank.rerank_groups(q.text, groups_of(candidates))
        if _empty(outcome):
            arm.empty_candidates += 1
            return []
        _require(outcome)
        return [candidates[i].path for i in outcome.order][:limit]

    arm.empty_candidates = 0
    return arm


def make_semantic_arm(ctx: ArmContext) -> Arm:
    """`semantic_search(rerank=true)` as production allocates it (#402)."""
    return _semantic_arm(ctx, _production_semantic_groups)


def make_hybrid_arm(ctx: ArmContext) -> Arm:
    """`hybrid_search(rerank=true)` as production allocates it (#402)."""
    return _hybrid_arm(ctx, _production_hybrid_groups)


def make_semantic_arm_incumbent(ctx: ArmContext) -> Arm:
    """The baseline arm: the pre-#402 builder, one document per candidate."""
    return _semantic_arm(ctx, _semantic_incumbent_groups)


def make_hybrid_arm_incumbent(ctx: ArmContext) -> Arm:
    """The baseline arm: the pre-#402 builder, one document per candidate."""
    return _hybrid_arm(ctx, _hybrid_incumbent_groups)


make_semantic_arm.membership = "semantic"
make_hybrid_arm.membership = "keyword_or_semantic"
make_semantic_arm_incumbent.membership = "semantic"
make_hybrid_arm_incumbent.membership = "keyword_or_semantic"
for _f in (make_semantic_arm, make_hybrid_arm, make_semantic_arm_incumbent,
           make_hybrid_arm_incumbent):
    _f.requires_db = True
    _f.preflight = preflight
