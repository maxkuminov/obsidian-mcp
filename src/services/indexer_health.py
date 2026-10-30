"""Indexer failure accounting, and the `/health` view of it (#308, D4).

For 8.5 days an indexer failed every tick while `/health` answered `ok` and the
only signal was one CRITICAL log line nobody read — and in multi-user mode the
failures were not even counted, because the counter was a local of the
single-user branch of `run_indexer_loop`. This module is the one place those
counts now live, so that every entrypoint (the startup pass, the periodic tick
in both modes, the panel's Reindex now) updates the same state and `/health`
can read it.

**Per scope** — `None` for single-user mode, else the user id:

- `index_consecutive_failures`: an index pass that raised (after the
  quarantine recovery); reset by one that returned;
- `embed_consecutive_failures`: an embed pass that reported failures (or
  raised); reset by one with none;
- `rederive_incomplete`: a committed re-derive that withheld its provenance
  stamp; reset by one that recorded it, or by a pass that was not a re-derive;
- `walk_incomplete`: a committed pass that could not list one or more
  directories beneath the root (#309, D4); reset by one that listed them all;
- `last_success_at` / `last_failure_at` of the index stage;
- `rechunk_pending`: the notes the scope's last embed stage counted as not yet
  re-chunked (#355). Published only as the process-wide, non-volumetric
  `rechunk: "in_progress" | "complete"` — never a number, never in `status`.

**Process-wide**: consecutive tick-level failures not attributable to a scope
(user enumeration, root-overlap detection, a tick that raised outside any
scope's stages); whether the indexer task is still running; whether indexing
is disabled (`MCP_SANDBOX_MODE`); and a pluggable count of quarantined notes.

**In-process only and never persisted**: a restart starts clean, which is the
same horizon as the task it describes. `/health` reads `snapshot()`, which
carries counts only — no path, error text, SQLSTATE or user id, because
`/health` is unauthenticated and publicly routed.

The CRITICAL "manual intervention required" line is logged once when a counter
first reaches `INDEXER_DEGRADED_AFTER_FAILURES`, and re-armed when the counter
resets — once per episode, not once per tick.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Iterable

from src.config import settings

logger = logging.getLogger(__name__)

Scope = int | None

_INDEX = "index"
_EMBED = "embed"
_REDERIVE = "rederive"
_WALK = "walk"

#: The start of the run-record line a pass that quarantined notes writes into
#: `indexer_runs.error` (#308, D5). Produced by `indexer.format_quarantined`
#: and recognised by `run_outcome` — one constant, so the two cannot drift.
QUARANTINED_RUN_PREFIX = "quarantined "
_QUARANTINED_RUN_LINE = re.compile(
    re.escape(QUARANTINED_RUN_PREFIX) + r"\d+ note\(s\): "
)

#: The start of the run-record line a pass that could not list a directory
#: writes (#309, D2). Produced by `indexer.format_walk_incomplete`. Not
#: recognised as a quarantine line, so such a run is labelled `failed`.
WALK_INCOMPLETE_RUN_PREFIX = "walk incomplete: "

RUN_OK = "ok"
RUN_QUARANTINED = "quarantined"
RUN_FAILED = "failed"


def run_outcome(error: str | None) -> str:
    """How the panel labels one `indexer_runs` row by its `error` text.

    A pass that quarantined notes **succeeded** (D5) but names them in
    `error`, so a non-empty `error` is not on its own a failure. The row is
    `quarantined` only when every line of `error` is a quarantine line —
    `format_quarantined` escapes line breaks in the paths it names, so a line
    of the record is either wholly that line or another stage's report, and
    any other line (a stage that raised, embed failures) makes it `failed`.
    """
    if not error:
        return RUN_OK
    lines = error.split("\n")
    if all(_QUARANTINED_RUN_LINE.match(line) for line in lines):
        return RUN_QUARANTINED
    return RUN_FAILED


@dataclass
class ScopeHealth:
    index_consecutive_failures: int = 0
    embed_consecutive_failures: int = 0
    rederive_incomplete: int = 0
    walk_incomplete: int = 0
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    #: Counters whose CRITICAL line has fired this episode.
    alerted: set[str] = field(default_factory=set)
    #: Notes of this scope not yet re-chunked (#355, D4), as the scope's most
    #: recent embed stage counted them under the one predicate. In process
    #: memory only, and **never** exposed: `/health` publishes only whether
    #: any scope's count is non-zero.
    rechunk_pending: int = 0


_scopes: dict[Scope, ScopeHealth] = {}
_enumeration_failures = 0
_enumeration_alerted = False
_task_running = True
_disabled = False
_quarantine_count_provider: Callable[[], int] | None = None


def _threshold() -> int:
    return settings.indexer_degraded_after_failures


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _scope(scope: Scope) -> ScopeHealth:
    state = _scopes.get(scope)
    if state is None:
        state = _scopes[scope] = ScopeHealth()
    return state


def _describe(scope: Scope) -> str:
    return "single-user scope" if scope is None else f"user_id={scope}"


def _bump(scope: Scope, state: ScopeHealth, counter: str, attr: str, what: str) -> None:
    value = getattr(state, attr) + 1
    setattr(state, attr, value)
    if value >= _threshold() and counter not in state.alerted:
        state.alerted.add(counter)
        logger.critical(
            "Indexer %s has failed %d consecutive times (%s) — manual "
            "intervention required",
            what,
            value,
            _describe(scope),
        )


def _reset(state: ScopeHealth, counter: str, attr: str) -> None:
    setattr(state, attr, 0)
    state.alerted.discard(counter)


def record_index(scope: Scope, ok: bool) -> None:
    """One index pass for `scope` returned (`ok`) or raised."""
    state = _scope(scope)
    if ok:
        _reset(state, _INDEX, "index_consecutive_failures")
        state.last_success_at = _now()
    else:
        state.last_failure_at = _now()
        _bump(scope, state, _INDEX, "index_consecutive_failures", "index pass")


def record_embed(scope: Scope, failures: int) -> None:
    """One embed pass for `scope` completed with `failures` failed notes.

    Zero resets the counter; any failure (or a raised stage, recorded by the
    caller as 1) counts the pass as failed.
    """
    state = _scope(scope)
    if failures:
        _bump(scope, state, _EMBED, "embed_consecutive_failures", "embed pass")
    else:
        _reset(state, _EMBED, "embed_consecutive_failures")


def record_rederive(scope: Scope, complete: bool | None) -> None:
    """A committed pass's re-derive outcome.

    `True` — the re-derive recorded provenance; `False` — it withheld the
    stamp (incomplete); `None` — the pass was not a re-derive, which also
    resets the counter (the scope left re-derive).
    """
    state = _scope(scope)
    if complete is False:
        _bump(scope, state, _REDERIVE, "rederive_incomplete", "re-derive")
    else:
        _reset(state, _REDERIVE, "rederive_incomplete")


def record_walk(scope: Scope, incomplete: bool) -> None:
    """A committed pass's walk: `incomplete` when it could not list one or
    more directories beneath the root (#309, D4); a complete walk resets."""
    state = _scope(scope)
    if incomplete:
        _bump(scope, state, _WALK, "walk_incomplete", "walk")
    else:
        _reset(state, _WALK, "walk_incomplete")


def record_rechunk(scope: Scope, pending: int) -> None:
    """The scope's embed stage counted `pending` notes whose
    `embedded_chunking_version IS DISTINCT FROM CHUNKING_VERSION` (#355).

    Not a failure counter: a long re-embed degrades nothing, alerts nothing
    and never changes `status` or the HTTP code.
    """
    _scope(scope).rechunk_pending = max(0, int(pending))


def record_enumeration(ok: bool) -> None:
    """One tick's scope-independent work succeeded (`ok`) or failed."""
    global _enumeration_failures, _enumeration_alerted
    if ok:
        _enumeration_failures = 0
        _enumeration_alerted = False
        return
    _enumeration_failures += 1
    if _enumeration_failures >= _threshold() and not _enumeration_alerted:
        _enumeration_alerted = True
        logger.critical(
            "Indexer tick (user enumeration / overlap detection) has failed "
            "%d consecutive times — manual intervention required",
            _enumeration_failures,
        )


def retain_scopes(active: Iterable[int]) -> None:
    """Forget per-user scopes no longer active (multi-user mode).

    A deactivated or deleted user whose last passes failed would otherwise
    hold `/health` at `degraded` until the next restart.
    """
    keep = set(active)
    for scope in [s for s in _scopes if s is not None and s not in keep]:
        del _scopes[scope]


def mark_task_stopped() -> None:
    """The indexer task ended by exception or returned while serving."""
    global _task_running
    _task_running = False


def mark_disabled() -> None:
    """Indexing is intentionally not run (`MCP_SANDBOX_MODE`)."""
    global _disabled, _task_running
    _disabled = True
    _task_running = False


def set_quarantine_count_provider(provider: Callable[[], int] | None) -> None:
    """Install the callable that returns the number of quarantined notes."""
    global _quarantine_count_provider
    _quarantine_count_provider = provider


def _quarantined_notes() -> int:
    if _quarantine_count_provider is None:
        return 0
    try:
        return max(0, int(_quarantine_count_provider()))
    except Exception as e:  # noqa: BLE001 - /health must never raise
        logger.error("Quarantine count unavailable for /health: %s", type(e).__name__)
        return 0


def snapshot() -> dict:
    """The `/health` `indexer` object. Counts only — nothing identifying."""
    threshold = _threshold()
    states = list(_scopes.values())
    # Distinct scopes with any index-side counter at the threshold (#309,
    # D4): a scope failing its index *and* its walk is one failing scope.
    failing = sum(
        1
        for s in states
        if max(
            s.index_consecutive_failures,
            s.rederive_incomplete,
            s.walk_incomplete,
        )
        >= threshold
    )
    embed_failing = sum(
        1 for s in states if s.embed_consecutive_failures >= threshold
    )
    max_failures = max(
        [_enumeration_failures]
        + [
            max(
                s.index_consecutive_failures,
                s.embed_consecutive_failures,
                s.rederive_incomplete,
                s.walk_incomplete,
            )
            for s in states
        ]
    )
    quarantined = _quarantined_notes()
    successes = [s.last_success_at for s in states if s.last_success_at is not None]
    last_success = max(successes).isoformat() if successes else None

    degraded = (
        failing > 0
        or embed_failing > 0
        or _enumeration_failures >= threshold
        or quarantined > 0
        or (not _task_running and not _disabled)
    )
    if degraded:
        status = "degraded"
    elif _disabled:
        status = "disabled"
    else:
        status = "ok"
    return {
        "status": status,
        "task_running": _task_running,
        "failing_scopes": failing,
        "embedding_failing_scopes": embed_failing,
        "max_consecutive_failures": max_failures,
        "quarantined_notes": quarantined,
        "last_success_at": last_success,
        # #355, D4: process-wide and **non-volumetric**, because `/health` is
        # unauthenticated and a count would be tenant-derived (Codex r2 B1).
        # Exact per-owner counts live on the panel and in the owner-scoped
        # `index_transition:` line. Never part of `status`.
        "rechunk": (
            "in_progress"
            if any(s.rechunk_pending > 0 for s in states)
            else "complete"
        ),
    }


def reset() -> None:
    """Forget everything. Tests only."""
    global _enumeration_failures, _enumeration_alerted, _task_running, _disabled
    global _quarantine_count_provider
    _scopes.clear()
    _enumeration_failures = 0
    _enumeration_alerted = False
    _task_running = True
    _disabled = False
    _quarantine_count_provider = None
