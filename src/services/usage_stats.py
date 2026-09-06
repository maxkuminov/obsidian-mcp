"""Read-only aggregation for the panel's performance view.

Two sources: `usage_logs` for everything about requests, and `indexer_runs` for
the pass history at the bottom of the page (`recent_indexer_runs`, last section
of this file). Nothing here writes either of them.

`usage_logs` is written in exactly one place — `_log_usage`
via the `_tracked` decorator in `src/mcp_server/tools.py` — and this module is a
consumer of what that contract already records
(`docs/architecture/usage-attribution.md`).

## The one predicate that says "the body did not run"

`_tracked` refuses some calls **before** the tool body executes, and logs the
refusal like any other call: same tool name, same actor, a `duration_ms` of
roughly zero and a `response_size` of the refusal string. Those rows are real
history and must stay in the log, but folding them into a latency percentile is
how a tool that is refusing 5,000 times an hour comes out looking fast.

So the aggregates filter on `pre_body_refusal_sql()`, and the per-tool refusal
count uses **the same expression**. One helper, two consumers, no drift — the
alternative is two hand-written predicates that agree until somebody adds a
third marker to one of them.

**It enumerates exactly the pre-body markers and nothing else.** A broad match
such as `params ? 'error'` or `params->>'error' IS NOT NULL` is wrong in a way
that is invisible on the page: `_tracked`'s post-body markers
(`vault_assignment_changed`, `vault_confirmation_unavailable`,
`vault_anchor_lost_at_publish`) are written by tools whose bodies *ran*, did the
work of resolving a vault, and then refused to publish. Excluding those hides
the slowest write path in the server from the one view built to find slow paths.
Every marker added to `_tracked` therefore has to be classified deliberately —
pre-body or not — and only the pre-body ones belong here.

`vault_anchor_lost_at_publish` is the one that had to be *split out* rather than
merely classified. The publish-time `VaultAnchorUnavailable` branch used to log
the admission gate's `no_vault_assigned`, so a row whose body had resolved a
root, read a note and computed a write was filed under the value this module
reads as "never started" — reachable through the #88 race, and dropped from
every percentile exactly when it mattered. Sharing a marker between the two
sides of the body/no-body line is the failure this enumeration exists to make
impossible.

The enumerated set:

* `over_quota: true` — the quota gate (#162). Read NULL-safely as
  `COALESCE((params->>'over_quota')::boolean, false)` so the overwhelming
  majority of rows, which carry no such key, are simply not refusals. It was
  declared here ahead of the gate that writes it so the two would land as one
  contract rather than as a page that silently mis-measures for a release; the
  gate now **imports** `OVER_QUOTA_PARAM` from this module rather than keeping a
  copy, so the writer and this predicate cannot drift at all.
* `error = 'no_vault_assigned'` — the admission gate: the caller has no
  resolvable vault root, so no tool body runs.
* `error = 'argument_not_encodable'` — the unpaired-surrogate screen, which
  refuses at the same altitude for the same reason.
* `error = 'vault_root_overlap'`, `error = 'vault_root_unexaminable'` and
  `error = 'vault_root_not_ready'` — the vault-root quarantine (#199), written
  by the *same* admission gate for the three reasons `_vault_root` can refuse
  a caller whose account is otherwise assigned and active: its root collides
  with another account's, its root could not be examined, or no quarantine
  snapshot has been published in this process yet. Three values rather than
  one because an operator acts differently on each — an assignment corrected,
  a mount restored, a detection that is failing — and the register in
  `docs/architecture/usage-attribution.md` says so. All three are pre-body by
  construction: the gate runs before the body, which is the same position
  `no_vault_assigned` holds.

The five string values are mirrored from `_NO_VAULT_MARKER`,
`_UNENCODABLE_ARG_MARKER` and the three `_VAULT_ROOT_*_MARKER` constants in
`src/mcp_server/tools.py`. They are *mirrored*
rather than imported because #162's quota gate will import this module from
`tools.py`, and an import in the other direction closes the cycle.
`tests/test_issue_160_refusal_predicate.py` asserts the copies are equal,
so the mirror cannot drift silently.
"""
from __future__ import annotations

from sqlalchemy import text

# Mirrored from `src.mcp_server.tools`. Pinned by
# `tests/test_issue_160_refusal_predicate.py`; see the module docstring for why
# this is a mirror and not an import.
NO_VAULT_MARKER = "no_vault_assigned"
UNENCODABLE_ARG_MARKER = "argument_not_encodable"

# The three vault-root quarantine markers (#199), mirrored the same way and
# pinned by the same test. They are written by the admission gate — the same
# `_tracked` branch that writes `NO_VAULT_MARKER` — so they sit on the same
# side of the body/no-body line by construction, not by judgement.
VAULT_ROOT_OVERLAP_MARKER = "vault_root_overlap"
VAULT_ROOT_UNEXAMINABLE_MARKER = "vault_root_unexaminable"
VAULT_ROOT_NOT_READY_MARKER = "vault_root_not_ready"

#: The `params.error` values `_tracked` writes *before* a tool body runs.
#:
#: **`permission_denied` and `tool_exception` are deliberately absent** (#192,
#: #193). Both are post-body by the classification rule above, and both were
#: classified before they were written rather than after somebody noticed the
#: page reporting the wrong numbers:
#:
#: * `permission_denied` is recorded by `_require_write`, which is called from
#:   *inside* a tool body that has already passed the vault gate, the argument
#:   screen and the quota gate whose marker this module owns — and has already
#:   spent its quota slot. The cost of leaving it out is that a read-only
#:   credential probing `create_note` dilutes that tool's percentiles with
#:   near-zero rows;
#:   the refusal is made visible on `/admin/usage` instead. Moving the write
#:   gate up into `_tracked` would make it a genuine pre-body refusal and would
#:   change quota accounting and refusal ordering for nine tools, which is a
#:   change of its own.
#: * `tool_exception` is by definition a body that ran, and a tool that raises
#:   after eight seconds of I/O is the slowest path there is. Enumerating it
#:   here would hide precisely the calls this page exists to surface.
#:
#: Adding either to this tuple is not a tuning knob: it silently drops those
#: rows out of every percentile and moves them into the refusal count.
PRE_BODY_REFUSAL_ERROR_MARKERS: tuple[str, ...] = (
    NO_VAULT_MARKER,
    UNENCODABLE_ARG_MARKER,
    VAULT_ROOT_OVERLAP_MARKER,
    VAULT_ROOT_UNEXAMINABLE_MARKER,
    VAULT_ROOT_NOT_READY_MARKER,
)

#: The boolean `params` key the quota gate (#162) sets on a refusal.
#: `src/mcp_server/tools.py` imports this constant rather than declaring its
#: own — the one direction the import can run without closing a cycle — so the
#: writer and the predicate below cannot name different keys.
OVER_QUOTA_PARAM = "over_quota"

# The marker values travel as bind parameters, not as interpolated literals.
# They are module constants today, so quoting them would be safe — but a
# fragment that interpolates is a fragment somebody extends with a value that
# is not a constant.
_BIND_PREFIX = "omcp_pre_body_refusal_"

#: Merge into the parameter dict of any query using the fragment below.
PRE_BODY_REFUSAL_BINDS: dict[str, str] = {
    f"{_BIND_PREFIX}{i}": marker
    for i, marker in enumerate(PRE_BODY_REFUSAL_ERROR_MARKERS)
}


def pre_body_refusal_sql(alias: str = "ul", params_column: str = "params") -> str:
    """A boolean SQL fragment: this row was refused before its body ran.

    Never NULL. `params` is nullable and `->>` on a missing key yields NULL, so
    both halves are wrapped in `COALESCE(..., false)`; without that the negation
    used by the executed-row filter would be NULL and PostgreSQL would drop
    every ordinary row on the floor.

    Bind `PRE_BODY_REFUSAL_BINDS` alongside the query's own parameters.
    """
    params = f"{alias}.{params_column}"
    placeholders = ", ".join(f":{name}" for name in PRE_BODY_REFUSAL_BINDS)
    return (
        f"(COALESCE(({params}->>'{OVER_QUOTA_PARAM}')::boolean, false)"
        f" OR COALESCE({params}->>'error' IN ({placeholders}), false))"
    )


def executed_sql(alias: str = "ul", params_column: str = "params") -> str:
    """The complement: this row's tool body actually ran.

    Includes rows whose body ran and then failed — a post-body error marker, an
    exception surfaced in-band, a timeout. Those are the expensive calls, and a
    latency view that hides them is worse than no latency view.
    """
    return f"(NOT {pre_body_refusal_sql(alias, params_column)})"


# --- Windows --------------------------------------------------------------

#: Selectable windows, in the order the page offers them, as **seconds**.
#: Seconds and not an interval literal: asyncpg infers a parameter's type from
#: its context, so `CAST(:window AS interval)` demands a `timedelta` and
#: rejects the string `'24 hours'` outright. Multiplying a bound integer by
#: `INTERVAL '1 second'` keeps the value a parameter (never interpolated) and
#: leaves the driver inferring a number, which is what it is.
#:
#: Capped at 30 days by construction: `percentile_cont` over `usage_logs` is an
#: ordered-set aggregate with no index to lean on beyond
#: `ix_usage_logs_created_at`, and the window is what bounds the scan.
WINDOWS: dict[str, int] = {
    "24h": 24 * 60 * 60,
    "7d": 7 * 24 * 60 * 60,
    "30d": 30 * 24 * 60 * 60,
}

DEFAULT_WINDOW = "24h"

WINDOW_LABELS: dict[str, str] = {
    "24h": "Last 24 hours",
    "7d": "Last 7 days",
    "30d": "Last 30 days",
}


def normalize_window(value: str | None) -> str:
    """Clamp a query-string window onto the offered set.

    An unknown value is the default, not an error: the selector is a link, and
    a hand-edited URL should render the page rather than a 422.
    """
    return value if value in WINDOWS else DEFAULT_WINDOW


def _scope(user_id: int | None) -> str:
    """The owner filter. Admins pass `None` and see every row, exactly as
    `/admin/usage` does; a regular user sees only their own."""
    return "" if user_id is None else " AND ul.user_id = :uid"


def _base_params(window: str, user_id: int | None) -> dict:
    params: dict = {"window_seconds": WINDOWS[window], **PRE_BODY_REFUSAL_BINDS}
    if user_id is not None:
        params["uid"] = user_id
    return params


_WINDOW_CLAUSE = "ul.created_at >= now() - (:window_seconds * INTERVAL '1 second')"


async def tool_aggregates(session, window: str, user_id: int | None) -> list[dict]:
    """Per-tool executed count, refusal count, duration percentiles, sizes.

    Percentiles and size aggregates are `FILTER`ed to executed rows; the
    refusal count is the same predicate un-negated, so the two always partition
    the window's rows for that tool. A tool that only ever refused in the window
    still appears — with `executed = 0` and NULL percentiles, which the template
    renders as an em dash rather than a zero it would be wrong to draw.
    """
    executed = executed_sql()
    refused = pre_body_refusal_sql()
    sql = f"""
        SELECT
            ul.tool AS tool,
            count(*) FILTER (WHERE {executed})                       AS executed,
            count(*) FILTER (WHERE {refused})                        AS refusals,
            percentile_cont(0.5) WITHIN GROUP (ORDER BY ul.duration_ms)
                FILTER (WHERE {executed} AND ul.duration_ms IS NOT NULL) AS p50,
            percentile_cont(0.95) WITHIN GROUP (ORDER BY ul.duration_ms)
                FILTER (WHERE {executed} AND ul.duration_ms IS NOT NULL) AS p95,
            percentile_cont(0.99) WITHIN GROUP (ORDER BY ul.duration_ms)
                FILTER (WHERE {executed} AND ul.duration_ms IS NOT NULL) AS p99,
            avg(ul.response_size)
                FILTER (WHERE {executed} AND ul.response_size IS NOT NULL) AS mean_size,
            max(ul.response_size)
                FILTER (WHERE {executed} AND ul.response_size IS NOT NULL) AS max_size
        FROM usage_logs ul
        WHERE {_WINDOW_CLAUSE}{_scope(user_id)}
        GROUP BY ul.tool
        ORDER BY count(*) FILTER (WHERE {executed}) DESC, ul.tool ASC
    """
    rows = (await session.execute(text(sql), _base_params(window, user_id))).fetchall()
    return [
        {
            "tool": r.tool,
            "executed": int(r.executed or 0),
            "refusals": int(r.refusals or 0),
            "p50": _ms(r.p50),
            "p95": _ms(r.p95),
            "p99": _ms(r.p99),
            "mean_size": _int(r.mean_size),
            "max_size": _int(r.max_size),
        }
        for r in rows
    ]


async def phase_breakdown(session, window: str, user_id: int | None) -> list[dict]:
    """Mean and p95 of the per-phase timings `src/services/timing.py` records.

    Only rows that actually carry the key contribute. A note tool logs no
    `embed_ms`, and rows written before the phase timings existed carry
    neither; treating a missing key as a zero would drag every mean toward the
    number of tools that do not measure, which is not a fact about anything.
    That is why the filter is `IS NOT NULL` on the extracted text rather than a
    `COALESCE(..., 0)`.
    """
    executed = executed_sql()
    selects = []
    for phase in ("embed_ms", "db_ms"):
        present = f"({executed} AND (ul.params->>'{phase}') IS NOT NULL)"
        value = f"(ul.params->>'{phase}')::double precision"
        selects.append(
            f"count(*) FILTER (WHERE {present}) AS {phase}_n, "
            f"avg({value}) FILTER (WHERE {present}) AS {phase}_mean, "
            f"percentile_cont(0.95) WITHIN GROUP (ORDER BY {value}) "
            f"FILTER (WHERE {present}) AS {phase}_p95"
        )
    sql = f"""
        SELECT {', '.join(selects)}
        FROM usage_logs ul
        WHERE {_WINDOW_CLAUSE}{_scope(user_id)}
    """
    row = (await session.execute(text(sql), _base_params(window, user_id))).first()
    out = []
    for phase, label in (("embed_ms", "Embedding"), ("db_ms", "Database")):
        count = int(getattr(row, f"{phase}_n", 0) or 0) if row is not None else 0
        out.append({
            "phase": phase,
            "label": label,
            "count": count,
            "mean": _ms(getattr(row, f"{phase}_mean", None)) if row is not None else None,
            "p95": _ms(getattr(row, f"{phase}_p95", None)) if row is not None else None,
        })
    return out


async def slowest_requests(
    session, window: str, user_id: int | None, limit: int = 50
) -> list:
    """The window's slowest executed calls, newest-slowest first, capped at 50.

    Actor attribution comes from the denormalised `actor_*` columns migration
    015 writes at call time; the LEFT JOINs are the fallback for rows written
    before it, exactly as on `/admin/usage`. Resolving by join alone renders
    "unknown" for precisely the credential an operator has just deleted and
    come to investigate (#77).
    """
    limit = max(1, min(int(limit), 50))
    executed = executed_sql()
    sql = f"""
        SELECT
            ul.created_at, ul.tool, ul.duration_ms, ul.response_size,
            ul.actor_kind, ul.actor_label, ul.actor_ref,
            ak.name        AS api_key_name,
            ak.key_prefix  AS api_key_prefix,
            oc.client_name AS oauth_client_name
        FROM usage_logs ul
        LEFT JOIN api_keys ak ON ul.key_id = ak.id
        LEFT JOIN oauth_tokens ot ON ul.oauth_token_id = ot.id
        LEFT JOIN oauth_clients oc ON ot.client_id = oc.client_id
        WHERE {_WINDOW_CLAUSE}{_scope(user_id)}
          AND {executed}
          AND ul.duration_ms IS NOT NULL
        ORDER BY ul.duration_ms DESC, ul.created_at DESC
        LIMIT :limit
    """
    params = _base_params(window, user_id)
    params["limit"] = limit
    return (await session.execute(text(sql), params)).fetchall()


# --- The pass history ------------------------------------------------------
#
# The page's second source, and the only one that is not `usage_logs`. It is
# here rather than in its own module because it is read by exactly one route
# and shares that route's scoping rule; `src/services/indexer.py` owns the
# *writing* of these rows (see
# `docs/architecture/indexing-and-embeddings.md`).

#: How many passes the performance page shows. A summary, not the history —
#: the full, filterable view belongs to `panel-ops-health` (#163), and the
#: table itself keeps 500 rows.
RECENT_RUNS_LIMIT = 20


async def recent_indexer_runs(
    session, user_id: int | None, limit: int = RECENT_RUNS_LIMIT
) -> list[dict]:
    """The newest passes, newest first, with their owner resolved live.

    **The owner is joined, not denormalised.** `usage_logs` keeps `actor_*`
    columns precisely so a deleted credential still renders (#77); this table
    deliberately does not, because its FK is `ON DELETE SET NULL` — when the
    user goes, the row's claim about *whose* vault this pass indexed stops being
    true, and a denormalised label would keep asserting it. So a run whose
    `user_id` is NULL is rendered as having no owner rather than as an owner
    whose name we happen to remember, and the two ways a row gets there are
    distinguished in the template's copy rather than invented here.

    `owner_missing` is the third case and should not arise: a non-NULL
    `user_id` that joins to nothing. The FK forbids it, but the FK can be
    `NOT VALID` on a database somebody repaired by hand (which is why migration
    019 refuses one), and rendering that as "no owner" would quietly agree with
    the corruption.

    Scoped like everything else on the page: an admin sees every pass, a
    regular user only their own — including, deliberately, none of the
    ownerless single-user/global passes, which are not theirs to read.
    """
    limit = max(1, min(int(limit), 200))
    scope = "" if user_id is None else " WHERE r.user_id = :uid"
    sql = f"""
        SELECT
            r.id, r.started_at, r.finished_at, r.trigger, r.user_id,
            r.notes_scanned, r.notes_indexed, r.notes_embedded, r.error,
            u.username AS owner_username,
            EXTRACT(EPOCH FROM (r.finished_at - r.started_at)) AS duration_seconds
        FROM indexer_runs r
        LEFT JOIN users u ON u.id = r.user_id
        {scope}
        ORDER BY r.started_at DESC, r.id DESC
        LIMIT :limit
    """
    params: dict = {"limit": limit}
    if user_id is not None:
        params["uid"] = user_id
    rows = (await session.execute(text(sql), params)).fetchall()
    return [
        {
            "id": r.id,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "duration": _duration(r.duration_seconds),
            "trigger": r.trigger,
            "user_id": r.user_id,
            "owner": r.owner_username,
            # A non-NULL owner that joined to nothing. Named rather than folded
            # into "no owner", so a broken FK reads as broken.
            "owner_missing": r.user_id is not None and r.owner_username is None,
            "notes_scanned": r.notes_scanned,
            "notes_indexed": r.notes_indexed,
            "notes_embedded": r.notes_embedded,
            "error": r.error,
        }
        for r in rows
    ]


def _duration(seconds) -> str | None:
    """A pass duration for display, or None when the row never finished.

    Seconds below a minute, then minutes and seconds: a pass is measured in
    minutes and a millisecond figure would be noise on a row whose point is
    "this one took ten times as long as its neighbours".
    """
    if seconds is None:
        return None
    total = float(seconds)
    if total < 60:
        return f"{total:.1f}s"
    minutes, remainder = divmod(int(round(total)), 60)
    return f"{minutes}m {remainder}s"


def _ms(value) -> float | None:
    """A millisecond figure rounded for display, or None when there was no
    sample. `percentile_cont` returns NULL for an empty filtered set, and that
    is rendered as "no data", never as 0."""
    return None if value is None else round(float(value), 1)


def _int(value) -> int | None:
    return None if value is None else int(round(float(value)))
