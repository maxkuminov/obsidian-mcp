"""Read-only aggregation for the panel's search-analytics view (#161).

One source, `usage_logs`, read through the result telemetry
`src/services/timing.py` records — `result_count`, `result_paths`, and
`find_related`'s `source_path`. Nothing here writes anything. The contract for
those keys, including why their byte budget is enforced at the record site,
is in `docs/architecture/search.md`.

The question the page answers is "what do my agents think this vault knows":
which queries they run, which come back empty, which notes retrieval surfaces
and which it never does. Three properties of this module are load-bearing.

## 1. Identity is `(user_id, path)`, matched NULL-safely

A path is unique only within an owner — `uq_notes_metadata_user_id_file_path`
says exactly that in the schema, with `NULLS NOT DISTINCT` — so
`Daily/2026-08-29.md` names a different note in every user's vault. **Every**
grouping and every coverage join here therefore keys on the pair, and joins it
with `IS NOT DISTINCT FROM` rather than `=`.

Both halves matter. Dropping `user_id` from the key would merge two tenants'
analytics into one row and show each of them facts about the other's vault —
a coverage list is a list of note paths, which is exactly the thing a tenant
must not learn about another. Using `=` would silently drop the ownerless
slice: `user_id` is nullable on both tables, single-user mode writes NULL on
every log row and every note, and `usage_logs.user_id` is `ON DELETE SET NULL`,
so a deleted user's history joins the ownerless notes rather than disappearing.
That is the honest reading — those rows genuinely are unattributed now — and it
matches how the ownerless indexer passes are treated on `/admin/performance`.

The per-user scope on top of that is the panel's usual one: an admin passes
`user_id=None` and sees every row (grouped per owner, never merged); a regular
user passes their id and sees only their own.

## 2. "Not a result set" is a *broad* error match here, deliberately

`/admin/performance` enumerates exactly the pre-body refusal markers and warns
at length that a broad `params->>'error' IS NOT NULL` is wrong there. It is
wrong there because a post-body refusal is still an expensive call and belongs
in a latency percentile. Here the question is the opposite one: did this call
produce a result set an operator can reason about? A row carrying *any* error
marker did not — the body may have run and even done real database work, but
it returned an error string, not results. So the filter is
`pre_body_refusal_sql()` (imported, never re-written — one predicate, no drift)
**plus** `params->>'error' IS NULL`.

`find_related`'s two operational markers are the reason the second half exists.
A missing or not-yet-embedded source note comes back with no neighbours, and
without a marker it would land in the zero-result view as "the vault holds
nothing near this note" — the one claim this page makes that an operator would
act on, applied to a note the tool never got as far as looking at.

## 3. The casts are guarded

`/admin/performance` evaluates `(params->>'embed_ms')::double precision`
unguarded, which makes those keys reserved: one row carrying a non-numeric
value takes the page down with a 500 for the whole window, for every user,
until it ages out. The same reservation is declared for these keys
(`docs/architecture/usage-attribution.md`) *and* this module guards anyway —
an integer pattern before the `::bigint`, a `jsonb_typeof(...) = 'array'`
before the unnest. Two belts, because these keys are written on the busiest
read path in the server and a bad row here would break a page nobody could
then use to find the bad row.
"""
from __future__ import annotations

from sqlalchemy import text

from src.services.usage_stats import (
    PRE_BODY_REFUSAL_BINDS,
    WINDOWS,
    pre_body_refusal_sql,
)

#: The two tools whose calls are grouped by their query text, in the order the
#: page shows them.
QUERY_TOOLS: tuple[str, ...] = ("keyword_search", "semantic_search")

#: `find_related` takes a note, not a query, so it gets its own tables keyed by
#: the source note. Kept separate from `QUERY_TOOLS` everywhere rather than
#: folded in with a NULL query: "the query was empty" and "this tool has no
#: query" are different rows on a page about what agents ask for.
RELATED_TOOL = "find_related"

#: The `params` key each tool is grouped by. Interpolated into SQL, so it is
#: resolved through this map and never taken from a caller — `_group_key`
#: refuses anything else.
GROUP_KEYS: dict[str, str] = {
    "keyword_search": "query",
    "semantic_search": "query",
    RELATED_TOOL: "source_path",
}

#: Rows per table. A page, not an export: the tail of a long-tail query
#: distribution is not something an operator reads down.
TABLE_LIMIT = 20

#: Rows in each coverage list. Longer than a query table because the
#: never-retrieved list is the one an operator scans for a note they expected
#: to see, and it carries its own total so the cap is never mistaken for the
#: answer.
COVERAGE_LIMIT = 50

#: What `timing.record_results` logs per call, mirrored for the page's copy.
#: The ranking is named after this number — "top-logged retrievals" — because
#: it bounds what the log can see, not what the tools returned.
LOGGED_RESULTS_PER_CALL = 10

_WINDOW_CLAUSE = "ul.created_at >= now() - (:window_seconds * INTERVAL '1 second')"

#: `result_count` as a number, or NULL when the row does not carry one that can
#: be read as an integer. Bounded to nine digits so the cast cannot overflow on
#: a hand-written row; see the module docstring on why this is guarded at all.
_RESULT_COUNT = (
    "CASE WHEN ul.params->>'result_count' ~ '^-?[0-9]{1,9}$' "
    "THEN (ul.params->>'result_count')::bigint END"
)

#: The unnest guard. `jsonb_array_elements_text` raises on a non-array, which
#: on this path would be a 500 for the whole window rather than one missing row.
_PATHS_ARRAY = "jsonb_typeof(ul.params->'result_paths') = 'array'"


def analysable_sql(alias: str = "ul", params_column: str = "params") -> str:
    """A boolean fragment: this row carries a result set worth aggregating.

    The imported refusal predicate (never a second copy of it) plus "no error
    marker of any kind". See the module docstring for why the broad error match
    that would be wrong on `/admin/performance` is right here.

    Never NULL: `params` is nullable and `->>` on a missing key yields NULL, so
    the error half is `COALESCE`d the way the imported predicate's halves are.
    """
    params = f"{alias}.{params_column}"
    return (
        f"(NOT {pre_body_refusal_sql(alias, params_column)}"
        f" AND COALESCE({params}->>'error' IS NULL, true))"
    )


def _group_key(tool: str) -> str:
    """The `params` key `tool` is grouped by, or a refusal.

    This value is interpolated into SQL. It is resolved from `GROUP_KEYS` and
    nothing else, so a caller cannot reach the string that gets interpolated —
    the same stance `usage_stats` takes with its marker binds.
    """
    try:
        return GROUP_KEYS[tool]
    except KeyError:
        raise ValueError(f"no analytics grouping is defined for tool {tool!r}")


def _scope(user_id: int | None, alias: str = "ul", column: str = "user_id") -> str:
    """The owner filter: admins (`None`) see every row, a user only their own.

    Identical in shape to `usage_stats._scope`; it takes an alias because this
    module also scopes `notes_metadata` for the coverage lists.
    """
    return "" if user_id is None else f" AND {alias}.{column} = :uid"


def _params(window: str, user_id: int | None, **extra) -> dict:
    params: dict = {"window_seconds": WINDOWS[window], **PRE_BODY_REFUSAL_BINDS}
    if user_id is not None:
        params["uid"] = user_id
    params.update(extra)
    return params


async def _grouped(
    session,
    window: str,
    user_id: int | None,
    tool: str,
    *,
    zero_only: bool,
    limit: int,
) -> list[dict]:
    """One table: calls per (owner, group key) for `tool` in `window`.

    `zero_only` switches the ordering and adds the `zero_calls > 0` filter, so
    the "most frequent" and "came back empty" tables are the same aggregation
    read two ways rather than two hand-written queries that agree until one of
    them is edited.
    """
    key = _group_key(tool)
    having = " WHERE g.zero_calls > 0" if zero_only else ""
    order = (
        "g.zero_calls DESC, g.calls DESC, g.grp ASC"
        if zero_only
        else "g.calls DESC, g.grp ASC"
    )
    sql = f"""
        WITH calls AS (
            SELECT
                ul.user_id                 AS user_id,
                ul.params->>'{key}'        AS grp,
                {_RESULT_COUNT}            AS result_count
            FROM usage_logs ul
            WHERE {_WINDOW_CLAUSE}{_scope(user_id)}
              AND ul.tool = :tool
              AND {analysable_sql()}
              AND ul.params->>'{key}' IS NOT NULL
        ),
        grouped AS (
            SELECT
                user_id,
                grp,
                count(*)                                        AS calls,
                count(result_count)                             AS measured,
                avg(result_count)                               AS mean_results,
                count(*) FILTER (WHERE result_count = 0)        AS zero_calls
            FROM calls
            GROUP BY user_id, grp
        )
        SELECT g.user_id, g.grp, g.calls, g.measured, g.mean_results,
               g.zero_calls, u.username AS owner
        FROM grouped g
        LEFT JOIN users u ON u.id = g.user_id
        {having}
        ORDER BY {order}
        LIMIT :limit
    """
    rows = (
        await session.execute(
            text(sql), _params(window, user_id, tool=tool, limit=limit)
        )
    ).fetchall()
    return [
        {
            "user_id": r.user_id,
            "owner": r.owner,
            "value": r.grp,
            "calls": int(r.calls or 0),
            "zero_calls": int(r.zero_calls or 0),
            # NULL when no row in the group carried a readable `result_count`
            # — every call predates the telemetry. Rendered as "—", never as a
            # zero, which would read as "this query finds nothing".
            "mean_results": (
                None if r.mean_results is None else round(float(r.mean_results), 1)
            ),
            "measured": int(r.measured or 0),
        }
        for r in rows
    ]


async def top_queries(
    session, window: str, user_id: int | None, tool: str, limit: int = TABLE_LIMIT
) -> list[dict]:
    """The window's most frequent calls for `tool`, with their mean result count."""
    return await _grouped(
        session, window, user_id, tool, zero_only=False, limit=limit
    )


async def zero_result_queries(
    session, window: str, user_id: int | None, tool: str, limit: int = TABLE_LIMIT
) -> list[dict]:
    """The window's calls for `tool` that came back empty, most-empty first.

    Every row here is a search that ran to completion and found nothing: the
    error-marked rows — including `find_related`'s missing and not-yet-embedded
    sources — are already excluded by `analysable_sql`.
    """
    return await _grouped(
        session, window, user_id, tool, zero_only=True, limit=limit
    )


# --- Retrieval coverage ---------------------------------------------------
#
# Both lists are bounded by what the log holds, which is the first
# `LOGGED_RESULTS_PER_CALL` paths of each call. The ranking is named for that
# ("top-logged retrievals") and the never-retrieved list is an upper bound, and
# the page states the caveat beside both — a coverage figure read as exact is
# worse than no coverage figure, because it is the kind of number an operator
# would delete notes on.

_LOGGED_PATHS_CTE = f"""
    SELECT ul.user_id AS user_id, p.path AS path
    FROM usage_logs ul
    CROSS JOIN LATERAL
        jsonb_array_elements_text(ul.params->'result_paths') AS p(path)
    WHERE {{window}}{{scope}}
      AND {analysable_sql()}
      AND {_PATHS_ARRAY}
"""


async def top_logged_retrievals(
    session, window: str, user_id: int | None, limit: int = COVERAGE_LIMIT
) -> list[dict]:
    """Notes ranked by appearances in the window's logged result lists.

    `indexed` is resolved through the `(user_id, path)` identity, not the path
    alone: a retrieved path with no matching row for that owner is a note that
    has since been renamed or deleted, and saying so is the point of showing it.
    """
    logged = _LOGGED_PATHS_CTE.format(window=_WINDOW_CLAUSE, scope=_scope(user_id))
    sql = f"""
        WITH logged AS ({logged})
        SELECT
            l.user_id,
            l.path,
            count(*) AS appearances,
            EXISTS (
                SELECT 1 FROM notes_metadata nm
                WHERE nm.file_path = l.path
                  AND nm.user_id IS NOT DISTINCT FROM l.user_id
            ) AS indexed,
            (SELECT u.username FROM users u WHERE u.id = l.user_id) AS owner
        FROM logged l
        GROUP BY l.user_id, l.path
        ORDER BY count(*) DESC, l.path ASC
        LIMIT :limit
    """
    rows = (
        await session.execute(text(sql), _params(window, user_id, limit=limit))
    ).fetchall()
    return [
        {
            "user_id": r.user_id,
            "owner": r.owner,
            "path": r.path,
            "appearances": int(r.appearances or 0),
            "indexed": bool(r.indexed),
        }
        for r in rows
    ]


async def never_retrieved(
    session, window: str, user_id: int | None, limit: int = COVERAGE_LIMIT
) -> tuple[list[dict], int]:
    """Indexed notes absent from every logged result in the window.

    Returns `(rows, total)`; `total` is the full count, so the page can say
    "50 of 1,204" rather than letting the cap read as the answer.

    Newest-modified first: a note nobody's search has surfaced since it was
    last edited is the interesting end of this list, and alphabetical order
    would bury it under a folder name.
    """
    logged = _LOGGED_PATHS_CTE.format(window=_WINDOW_CLAUSE, scope=_scope(user_id))
    note_scope = _scope(user_id, alias="nm")
    sql = f"""
        WITH logged AS (SELECT DISTINCT user_id, path FROM ({logged}) AS l)
        SELECT
            nm.user_id,
            nm.file_path AS path,
            nm.title     AS title,
            nm.modified_at,
            (SELECT u.username FROM users u WHERE u.id = nm.user_id) AS owner,
            count(*) OVER () AS total
        FROM notes_metadata nm
        WHERE true{note_scope}
          AND NOT EXISTS (
              SELECT 1 FROM logged l
              WHERE l.path = nm.file_path
                AND l.user_id IS NOT DISTINCT FROM nm.user_id
          )
        ORDER BY nm.modified_at DESC NULLS LAST, nm.file_path ASC
        LIMIT :limit
    """
    rows = (
        await session.execute(text(sql), _params(window, user_id, limit=limit))
    ).fetchall()
    total = int(rows[0].total) if rows else 0
    return [
        {
            "user_id": r.user_id,
            "owner": r.owner,
            "path": r.path,
            "title": r.title,
            "modified_at": r.modified_at.isoformat() if r.modified_at else None,
        }
        for r in rows
    ], total
