"""The usage page's filtered reads (#162): chart, log, and per-actor totals.

`/admin/usage` was one undifferentiated stream. Every attribution column it
needed already existed on `usage_logs` — `user_id`, `key_id`, the denormalised
`actor_*` triple (issue #77) — and nothing read them selectively, so on a
server with several users and several agents there was no way to answer "who is
driving this".

This module is that reading. It writes nothing, and it is a **peer** of
`src/services/usage_stats.py` rather than part of it: that module is the
performance page's aggregation, with its own load-bearing rule about which rows
count as executed, and folding a second page's queries into it would put two
different questions behind one set of helpers. The window vocabulary is shared
by import, because two lists of selectable windows is how the two pages start
disagreeing about what "7d" means.

## Filters compose into one WHERE clause, and are validated as identifiers

A filter is an equality on an indexed column, and the three of them plus the
window compose by conjunction. Values arrive from a query string, so each is
**checked against the set of values the page itself offered** and silently
dropped when it is not one — the same treatment `normalize_window` gives an
unknown window, and for the same reason: the selector is a set of links, and a
hand-edited URL should render the page rather than a 422. Dropping is safe
because the fallback is always *less* specific, never more: an unrecognised key
id shows every key the viewer may already see, and the owner scope below is
applied separately and unconditionally.

Every value travels as a bind parameter. Nothing is interpolated.

## The owner scope is not a filter

`user_id` appears twice and the two are different things. `scope_user_id` is
the viewer's own tenancy — NULL for an admin, their id for everybody else,
exactly as `/admin/usage` and `/admin/performance` already scope — and it is
applied whether or not any filter is set. The `user` *filter* is an admin's
choice of whose rows to look at, offered only when the scope is unrestricted.
A non-admin cannot widen their view by editing the query string, because the
scope clause is added after and cannot be removed by anything the client sends.

## Deleted actors stay visible

The per-actor totals group by the denormalised `actor_*` columns first and fall
back to the LEFT JOINs, which is `_usage_actor`'s rule verbatim (#77): resolving
by join alone renders "unknown" for precisely the credential an operator has
just deleted and come to investigate. Rows from a deleted key therefore appear
under their recorded label whenever no key filter is set — the key filter
itself can only offer keys that still exist, which is why the unfiltered view is
where that history lives.

## The composite index

`ix_usage_logs_key_id_created_at` (migration 020) is what makes the per-key
filter a range scan rather than a window scan that discards most of what it
reads. `ix_usage_logs_created_at` still serves the unfiltered and by-tool
cases, and `ix_usage_logs_user_id` the by-user one.
"""
from __future__ import annotations

from urllib.parse import urlencode

from sqlalchemy import text

from src.services.usage_stats import WINDOWS, normalize_window

__all__ = [
    "LOG_LIMIT",
    "Filters",
    "actor_totals",
    "chart_series",
    "filter_options",
    "recent_logs",
    "resolve_filters",
]

#: How many log rows the page renders. Unchanged from the pre-filter page: the
#: request log is a tail, not an export, and the filters are what make a
#: hundred rows enough to answer a question.
LOG_LIMIT = 100

#: The bucket the chart groups by, per window. An hour for 24h — a single bar
#: for "today" is not a chart — and a day for the longer two, which is what the
#: page drew before it had a window selector at all.
_BUCKETS = {"24h": "hour", "7d": "day", "30d": "day"}

_WINDOW_CLAUSE = "ul.created_at >= now() - (:window_seconds * INTERVAL '1 second')"

# The join chain every actor-resolving query needs, written once. `_usage_actor`
# reads all six of the columns these produce.
_ACTOR_JOINS = """
        LEFT JOIN api_keys ak ON ul.key_id = ak.id
        LEFT JOIN oauth_tokens ot ON ul.oauth_token_id = ot.id
        LEFT JOIN oauth_clients oc ON ot.client_id = oc.client_id
"""

_ACTOR_COLUMNS = """
            ul.actor_kind, ul.actor_label, ul.actor_ref,
            ak.name        AS api_key_name,
            ak.key_prefix  AS api_key_prefix,
            oc.client_name AS oauth_client_name
"""


class Filters:
    """The page's resolved state: a window plus up to three equalities.

    Deliberately a small object rather than a dict, because it is threaded
    through four queries and the template, and one misspelled key in any of
    them is a filter that silently stops applying.
    """

    __slots__ = ("window", "user_id", "key_id", "tool", "scope_user_id")

    def __init__(self, window, user_id, key_id, tool, scope_user_id):
        self.window = window
        self.user_id = user_id
        self.key_id = key_id
        self.tool = tool
        self.scope_user_id = scope_user_id

    @property
    def any_active(self) -> bool:
        return (
            self.user_id is not None
            or self.key_id is not None
            or self.tool is not None
        )

    def _clauses(self) -> tuple[str, dict]:
        clauses = [_WINDOW_CLAUSE]
        params: dict = {"window_seconds": WINDOWS[self.window]}
        # The viewer's own tenancy, applied unconditionally and last-word: a
        # non-admin cannot widen it by editing the query string.
        if self.scope_user_id is not None:
            clauses.append("ul.user_id = :scope_uid")
            params["scope_uid"] = self.scope_user_id
        elif self.user_id is not None:
            clauses.append("ul.user_id = :filter_uid")
            params["filter_uid"] = self.user_id
        if self.key_id is not None:
            clauses.append("ul.key_id = :filter_key")
            params["filter_key"] = self.key_id
        if self.tool is not None:
            clauses.append("ul.tool = :filter_tool")
            params["filter_tool"] = self.tool
        return " AND ".join(clauses), params

    def query_string(self, **overrides) -> str:
        """This state as a query string, with `overrides` applied.

        Used by the template to build each selector's links, so a window change
        keeps the actor filters and vice versa — a selector that silently drops
        the other filters is how an operator concludes the page is broken.
        """
        state = {
            "window": self.window,
            "user": self.user_id,
            "key": self.key_id,
            "tool": self.tool,
        }
        state.update(overrides)
        return urlencode({k: v for k, v in state.items() if v not in (None, "")})


async def filter_options(session, window: str, scope_user_id: int | None) -> dict:
    """The values each selector offers: users, keys, tools.

    Users and keys are read from their own tables rather than from
    `usage_logs`, so a credential that has not been used yet can still be
    selected — the operator picking it is usually asking "has this made any
    calls at all", and an option list built from the log answers that question
    by omitting the option.

    Tools come from the window's own rows: the list of registered tools is not
    a fact this module has, and a tool that never appears cannot be filtered to
    anything but an empty page.
    """
    users = []
    if scope_user_id is None:
        rows = (
            await session.execute(
                text("SELECT id, username FROM users ORDER BY lower(username)")
            )
        ).fetchall()
        users = [{"id": r.id, "label": r.username} for r in rows]

    key_sql = "SELECT id, name, key_prefix FROM api_keys"
    key_params: dict = {}
    if scope_user_id is not None:
        key_sql += " WHERE user_id = :uid"
        key_params["uid"] = scope_user_id
    key_sql += " ORDER BY created_at DESC"
    key_rows = (await session.execute(text(key_sql), key_params)).fetchall()
    keys = [
        {"id": r.id, "label": r.name, "detail": r.key_prefix} for r in key_rows
    ]

    tool_sql = f"SELECT DISTINCT ul.tool FROM usage_logs ul WHERE {_WINDOW_CLAUSE}"
    tool_params: dict = {"window_seconds": WINDOWS[window]}
    if scope_user_id is not None:
        tool_sql += " AND ul.user_id = :uid"
        tool_params["uid"] = scope_user_id
    tool_sql += " ORDER BY ul.tool"
    tools = [
        r.tool for r in (await session.execute(text(tool_sql), tool_params)).fetchall()
    ]

    return {"users": users, "keys": keys, "tools": tools}


def resolve_filters(
    window,
    user,
    key,
    tool,
    scope_user_id: int | None,
    options: dict,
) -> Filters:
    """Clamp query-string values onto what the page actually offered.

    An unknown value becomes "no filter", never a 422 and never a value passed
    through to SQL: the selectors are links, a stale bookmark should still
    render, and the fallback is always the *less* specific view. The owner
    scope is untouched by any of it.
    """
    window = normalize_window(window)

    def as_int(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    user_id = as_int(user)
    if scope_user_id is not None or user_id not in {u["id"] for u in options["users"]}:
        user_id = None

    key_id = as_int(key)
    if key_id not in {k["id"] for k in options["keys"]}:
        key_id = None

    tool_name = tool if tool in set(options["tools"]) else None

    return Filters(window, user_id, key_id, tool_name, scope_user_id)


async def recent_logs(session, filters: Filters, limit: int = LOG_LIMIT):
    """The newest matching calls, with the columns `_usage_actor` reads.

    Three outcome markers ride along, **all three as raw text and none of them
    cast**. `params` is `JSONB` and `->>` always yields `text`; a `::boolean`
    on `over_quota` would 500 the whole page for every user the moment one row
    carried something that is not `true`/`false`, and it would keep doing so
    until that row aged out of the window. `/admin/performance`'s unguarded
    casts are the standing example of that hazard, and this query deliberately
    does not join it.

    Nothing is discarded between here and the template either: the mapping to
    a displayed outcome happens once, in the route (design D9), and a value the
    mapping does not recognise is *shown*, not dropped.
    """
    where, params = filters._clauses()
    params["limit"] = max(1, min(int(limit), LOG_LIMIT))
    sql = f"""
        SELECT
            ul.id, ul.tool, ul.duration_ms, ul.created_at,
            ul.params->>'error'      AS error_marker,
            ul.params->>'error_type' AS error_type,
            ul.params->>'over_quota' AS over_quota,
{_ACTOR_COLUMNS}
        FROM usage_logs ul
{_ACTOR_JOINS}
        WHERE {where}
        ORDER BY ul.created_at DESC
        LIMIT :limit
    """
    return (await session.execute(text(sql), params)).fetchall()


async def chart_series(session, filters: Filters) -> dict:
    """`{labels, values}` — matching requests per bucket, oldest first.

    Buckets are `date_trunc`ed in the database and rendered here. Empty buckets
    are not filled in: the chart is a bar per bucket that had traffic, which is
    what it drew before the filters existed, and inventing zero-height bars for
    a filter that matched nothing would make an empty result look like a busy
    one.
    """
    where, params = filters._clauses()
    bucket = _BUCKETS[filters.window]
    sql = f"""
        SELECT date_trunc('{bucket}', ul.created_at) AS bucket, count(*) AS cnt
        FROM usage_logs ul
        WHERE {where}
        GROUP BY bucket
        ORDER BY bucket
    """
    rows = (await session.execute(text(sql), params)).fetchall()
    fmt = "%H:%M" if bucket == "hour" else "%m/%d"
    return {
        "labels": [r.bucket.strftime(fmt) for r in rows],
        "values": [r.cnt for r in rows],
    }


async def actor_totals(session, filters: Filters):
    """Per-actor request counts for the filtered window, busiest first.

    Grouped by the denormalised triple *and* the joined fallbacks, so a
    credential that has since been deleted keeps its own line under the label
    recorded at call time instead of collapsing into everyone else's "unknown"
    (#77). The caller resolves each row through the same `_usage_actor` reader
    the log table uses, so the two never disagree about how an actor is named.
    """
    where, params = filters._clauses()
    sql = f"""
        SELECT
{_ACTOR_COLUMNS},
            count(*)              AS requests,
            max(ul.created_at)    AS last_seen
        FROM usage_logs ul
{_ACTOR_JOINS}
        WHERE {where}
        GROUP BY
            ul.actor_kind, ul.actor_label, ul.actor_ref,
            ak.name, ak.key_prefix, oc.client_name
        ORDER BY count(*) DESC, ul.actor_label ASC
    """
    return (await session.execute(text(sql), params)).fetchall()
