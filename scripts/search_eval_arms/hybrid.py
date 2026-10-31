"""The replayable `hybrid_search` arm (#354, design D9).

Two factories, because `--arm-opt` is one dict shared by every arm of a run
(search.md L8, design L11): one report can then hold `hybrid@k` and
`hybrid@k'` on the same qrels and snapshot.

- `scripts.search_eval_arms.hybrid:make_arm` takes the fusion constant from
  `--arm-opt hybrid_k=…`, defaulting to the `HYBRID_RRF_K` setting;
- `scripts.search_eval_arms.hybrid:make_arm_alt` requires
  `--arm-opt hybrid_alt_k=…` (the run exits 64 without it), and takes the
  optional `--arm-opt hybrid_alt_any_offset=0|50` (default 50, #403). With 0
  it replays the fusion before #403 — fill (`match: any`) keyword rows at
  their unoffset rank — by setting the service's task-local override around
  its own call, so `make_arm_alt hybrid_alt_k=60 hybrid_alt_any_offset=0` is
  the incumbent-fusion `BASELINE` for #403's gate. No other arm, and no live
  call, sees the override.

Both call the production service `src.services.hybrid.hybrid_search`
directly, never through `_tracked`, with one fresh session per query from the
read-only session factory, so replay writes nothing and records no telemetry.
Their membership is `keyword_or_semantic`: the hybrid retrieves the union of
its arms, so a path is searchable before when either constituent reached it,
and losing either constituent is corpus loss.

**A keyword-only answer fails the run.** When the embedding provider fails
during replay the service degrades to the keyword arm; scoring that as the
fusion would be wrong evidence, so the arm raises and the generation is
recorded `failed`.
"""
from __future__ import annotations

from scripts.search_eval import UsageError
from scripts.search_eval.arms import Arm, ArmContext, EvalQuery

#: The service's page cap.
_MAX_LIMIT = 50


class KeywordOnlyAnswer(RuntimeError):
    """The service answered keyword-only (the semantic arm was unavailable)."""


def _k(ctx: ArmContext, key: str, default: int | None) -> int:
    raw = ctx.options.get(key)
    if raw is None:
        if default is None:
            raise UsageError(f"the hybrid arm needs --arm-opt {key}=<1..1000>")
        return default
    try:
        value = int(raw)
    except ValueError as e:
        raise UsageError(f"--arm-opt {key} must be an integer") from e
    if not 1 <= value <= 1000:
        raise UsageError(f"--arm-opt {key} must be between 1 and 1000")
    return value


#: The values `hybrid_alt_any_offset` accepts: the incumbent (0) and the
#: shipped constant (50).
_ANY_OFFSETS = (0, 50)


def _any_offset(ctx: ArmContext) -> int | None:
    """`hybrid_alt_any_offset`, validated; `None` when absent (the live
    constant applies)."""
    raw = ctx.options.get("hybrid_alt_any_offset")
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError as e:
        raise UsageError("--arm-opt hybrid_alt_any_offset must be 0 or 50") from e
    if value not in _ANY_OFFSETS:
        raise UsageError("--arm-opt hybrid_alt_any_offset must be 0 or 50")
    return value


def check_any_offset_option(options: dict) -> None:
    """Called by the runner before a generation is reserved, so a bad value
    is a usage error (exit 64), not a failed generation."""
    _any_offset(ArmContext(session_factory=None, corpus=None, options=options))


def _hybrid_arm(ctx: ArmContext, k: int, any_offset: int | None = None) -> Arm:
    from src.services import hybrid as hybrid_service
    from src.services.hybrid import ARMS_BOTH, hybrid_search

    async def arm(q: EvalQuery, depth: int) -> list[str]:
        token = (
            None if any_offset is None
            else hybrid_service._any_fill_offset_override.set(any_offset)
        )
        try:
            async with ctx.session_factory() as session:
                result = await hybrid_search(
                    session, q.text, limit=min(depth, _MAX_LIMIT), folder=q.folder,
                    tags=q.tags, frontmatter=q.frontmatter, user_id=q.user_id, rrf_k=k,
                )
        finally:
            if token is not None:
                hybrid_service._any_fill_offset_override.reset(token)
        if result.arms != ARMS_BOTH:
            raise KeywordOnlyAnswer(
                f"hybrid arm answered keyword-only ({result.semantic_unavailable}); "
                "a degraded arm is not evidence for the fusion"
            )
        return [r.path for r in result.rows]

    return arm


def make_arm(ctx: ArmContext) -> Arm:
    from src.config import settings

    return _hybrid_arm(ctx, _k(ctx, "hybrid_k", settings.hybrid_rrf_k))


def make_arm_alt(ctx: ArmContext) -> Arm:
    return _hybrid_arm(ctx, _k(ctx, "hybrid_alt_k", None), _any_offset(ctx))


for _f in (make_arm, make_arm_alt):
    _f.membership = "keyword_or_semantic"
    _f.requires_db = True
make_arm_alt.required_options = ("hybrid_alt_k",)
make_arm_alt.check_options = check_any_offset_option
