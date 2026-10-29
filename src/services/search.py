"""`keyword_search`'s service: the conjunctive block, the disjunctive fill and
the page-bounded snippet (#344; design D2–D4, D8).

Three kinds of statement, all in the one transaction that opens with
`SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY` and issued
`SET LOCAL random_page_cost = 1.1` — one snapshot for every statement, so a
concurrent index commit between the conjunctive and the disjunctive statement
cannot drop a note from both blocks (Codex C-1):

1. **The conjunctive statement** — `websearch_to_tsquery` per config OR'd,
   `@@`, the shared filters, `rank DESC, file_path ASC`, `LIMIT limit`. Its
   plan shape is the one `tests/integration/test_keyword_plan.py` pins; it
   gained `id` in the projection and `RANK_WEIGHTS` in the rank, nothing else.
   Rows are tagged `match: "all"`.
2. **The disjunctive statement**, only when the first under-fills the page and
   at least one config's parse is eligible (`fts.fallback_plan`): the same
   statement with the combined fallback tsquery, `AND NOT (v @@ <and query>)`,
   `LIMIT limit - n`. Rows are tagged `match: "any"` and appended after every
   `all` row; the two blocks are never interleaved (an `any` row's rank is
   against a different query).
3. **The snippet statements**, over the ids of the first
   `KEYWORD_SNIPPET_ROWS` rows of the assembled page and nothing else: one per
   block for the highlighted headline per config, `matched_in` and the
   per-config `matched_terms` counts; then one more per block for the `head:`
   text of the rows whose body part did not match or whose excerpt held no
   highlight. Their cost is a function
   of the row cap, the excerpt cap and the config count — never of `limit` —
   and is recorded as `snippet_ms`.
"""
import time

from sqlalchemy import Integer, cast, func, literal_column, select, text
from sqlalchemy.dialects.postgresql import REGCONFIG, TSQUERY
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import bindparam

from src.config import settings
from src.models.db import NoteMetadata
from src.services import timing
from src.services.filters import apply_note_filters
from src.services.fts import (
    HEAD_OPTIONS,
    HEADLINE_OPTIONS,
    HEADLINE_START,
    HEADLINE_STOP,
    FallbackPlan,
    strip_sentinels,
    best_matched_terms,
    combined_tsquery,
    fallback_plan,
    fallback_tsquery,
    matched_terms_units,
    rank_expression,
    tsquery_text_sql,
)

#: How the tool renders PostgreSQL's highlight sentinels.
RENDER_START = "«"
RENDER_STOP = "»"


async def _ranked_block(
    session, tsquery, *, folder, tags, frontmatter, user_id, limit, exclude=None
):
    """One ranking statement: the conjunctive one (`exclude=None`) or the
    disjunctive one (`exclude=<the conjunctive tsquery>`)."""
    rank = rank_expression(NoteMetadata.content_tsvector, tsquery).label("rank")
    # Explicit projection (#280, design D6): `id` for the snippet statement
    # and the four columns the result renders. `ts_rank_cd` and `@@` still
    # read the tsvector server-side; what is never shipped is the vector, the
    # excerpt or `frontmatter`. Both are deferred with raiseload on the model.
    stmt = (
        select(
            NoteMetadata.id,
            NoteMetadata.file_path,
            NoteMetadata.title,
            NoteMetadata.tags,
            rank,
        )
        .where(NoteMetadata.content_tsvector.op("@@")(tsquery))
    )
    if exclude is not None:
        # The conjunctive block was not full, so every row matching the
        # conjunctive query is already in it: this excludes exactly those.
        stmt = stmt.where(~NoteMetadata.content_tsvector.op("@@")(exclude))
    stmt = apply_note_filters(
        stmt, folder=folder, tags=tags, frontmatter=frontmatter, user_id=user_id
    )
    # `file_path` breaks rank ties deterministically. `ts_rank_cd` produces
    # plenty of them, and which tied row survived the LIMIT otherwise depended
    # on the plan — so the planner hint would have changed *results*, not just
    # speed. With the tie-break, membership and order are stable across plans.
    stmt = stmt.order_by(rank.desc(), NoteMetadata.file_path.asc()).limit(limit)
    return (await session.execute(stmt)).all()


async def _parsed_texts(session, query: str) -> list[str | None]:
    """The text form of each config's websearch parse, in config order. No
    table access; padded with None if the driver returns fewer rows."""
    cfg_params = {f"fts_cfg_{i}": cfg for i, cfg in enumerate(settings.fts_configs)}
    rows = (await session.execute(text(tsquery_text_sql()), {"q": query, **cfg_params})).all()
    parsed: list[str | None] = [None] * len(settings.fts_configs)
    for row in rows:
        try:
            ord_, value = row[0], row[1]
        except (TypeError, IndexError):
            continue
        if isinstance(ord_, int) and 0 <= ord_ < len(parsed):
            parsed[ord_] = value
    return parsed


def _a_part(column):
    return func.ts_filter(column, literal_column("'{a}'"))


def _d_part(column):
    return func.ts_filter(column, literal_column("'{d}'"))


async def _snippets(session, ids: list[int], tsquery, plan: FallbackPlan | None) -> dict:
    """Headlines, `matched_in` and per-config `matched_terms` for `ids`.

    Returns `{id: {"snippet", "matched_in", "matched_terms", "has_excerpt"}}`.
    `body_excerpt` is read only inside `ts_headline`; the headline is what is
    projected. A second statement supplies the `head:` text for the rows whose
    excerpt held no highlight (design D4) — one more parse for those rows only.
    """
    if not ids:
        return {}
    v = NoteMetadata.content_tsvector
    columns = [NoteMetadata.id.label("id")]
    for i, cfg in enumerate(settings.fts_configs):
        columns.append(
            func.ts_headline(
                cast(bindparam(f"hl_cfg_{i}", value=cfg), REGCONFIG),
                NoteMetadata.body_excerpt,
                tsquery,
                bindparam(f"hl_opts_{i}", value=HEADLINE_OPTIONS),
            ).label(f"hl_{i}")
        )
    columns.append(_a_part(v).op("@@")(tsquery).label("matched_title"))
    columns.append(_d_part(v).op("@@")(tsquery).label("matched_body"))
    columns.append((NoteMetadata.body_excerpt.is_not(None)).label("has_excerpt"))
    units_by_config = matched_terms_units(plan) if plan is not None else []
    for i, units in units_by_config:
        total = None
        for j, unit in enumerate(units):
            term = cast(
                v.op("@@")(cast(bindparam(f"mt_{i}_{j}", value=unit), TSQUERY)),
                Integer,
            )
            total = term if total is None else total + term
        columns.append(total.label(f"mt_{i}"))
    stmt = select(*columns).where(NoteMetadata.id.in_(ids))
    rows = (await session.execute(stmt)).all()

    out: dict = {}
    need_head: list[int] = []
    for row in rows:
        m = row._mapping
        snippet = None
        # A headline is the matching span only when the body part itself
        # matched the block's query: with no full cover `ts_headline` still
        # marks single query words, so a title-only or split match would
        # otherwise render a body "highlight" that is not the match (V-B1).
        if m.get("matched_body"):
            for i in range(len(settings.fts_configs)):
                hl = m.get(f"hl_{i}")
                if hl and HEADLINE_START in hl:
                    snippet = hl.replace(HEADLINE_START, RENDER_START).replace(
                        HEADLINE_STOP, RENDER_STOP
                    )
                    break
        matched_in = []
        if m.get("matched_title"):
            matched_in.append("title")
        if m.get("matched_body"):
            matched_in.append("body")
        if not matched_in:
            # Every id here matched the block's query over the whole vector,
            # so neither part alone matching means the match needs both —
            # one term in the title, another in the body (V-N5).
            matched_in.append("title+body")
        pairs = [
            (int(m.get(f"mt_{i}") or 0), len(units)) for i, units in units_by_config
        ]
        has_excerpt = bool(m.get("has_excerpt"))
        out[m["id"]] = {
            "snippet": snippet,
            "head": None,
            "matched_in": matched_in,
            "matched_terms": best_matched_terms(pairs),
            "has_excerpt": has_excerpt,
        }
        if snippet is None and has_excerpt:
            need_head.append(m["id"])

    if need_head:
        cfg0 = settings.fts_configs[0]
        head_stmt = select(
            NoteMetadata.id.label("id"),
            func.ts_headline(
                cast(bindparam("head_cfg", value=cfg0), REGCONFIG),
                NoteMetadata.body_excerpt,
                tsquery,
                bindparam("head_opts", value=HEAD_OPTIONS),
            ).label("head"),
        ).where(NoteMetadata.id.in_(need_head))
        for row in (await session.execute(head_stmt)).all():
            m = row._mapping
            if m["id"] in out:
                # Same sentinels as the fragment mode (`HEAD_OPTIONS`); the
                # head is never highlighted, so they are removed, not rendered.
                head = m["head"]
                out[m["id"]]["head"] = strip_sentinels(head) if head else head
    return out


def _row_dict(row, match: str) -> dict:
    return {
        "id": row.id,
        "path": row.file_path,
        "title": row.title,
        "tags": row.tags,
        "rank": float(row.rank),
        "match": match,
    }


async def full_text_search(
    session: AsyncSession,
    query: str,
    folder: str | None = None,
    limit: int = 20,
    tags: list[str] | None = None,
    frontmatter: dict | None = None,
    user_id: int | None = None,
) -> list[dict]:
    """Full-text search over notes_metadata using tsvector.

    The tsquery is built from `settings.fts_configs` (see `src/services/fts.py`)
    so query-time configs match the index-time configs in `indexer.py`. Each
    returned dict carries `path`, `title`, `tags`, `rank`, `match`
    (`"all"` | `"any"`), and — for the first `KEYWORD_SNIPPET_ROWS` rows —
    `snippet`, `head`, `matched_in`, `matched_terms` and `has_excerpt`.
    """
    limit = max(1, min(limit, 500))
    and_query = combined_tsquery(query)

    # The same transaction-scoped hint the vector path uses, for the same
    # reason: the planner's cost model does not see detoast I/O. It costs the
    # `notes_metadata` heap at `relpages` (~481) and happily picks a Seq Scan,
    # which then detoasts every one of ~3,800 tsvectors out of a 36 MB TOAST
    # table — 13,086 buffers per query. Lowering `random_page_cost` flips it to
    # a GIN bitmap scan: 1,146 buffers, 6–9 ms warm instead of 26–37 ms.
    # Lifetime counters before this: 5 index scans vs 3,655 sequential ones.
    # SET LOCAL scopes to this transaction only — the database is shared with
    # other tenants, so no global setting is touched. Every statement of this
    # call runs on this session, inside this transaction.
    #
    # And under one snapshot (Codex C-1): the disjunctive statement excludes
    # `v @@ <and query>` on the premise that every such row is already in the
    # conjunctive block, which holds only if both statements see the same
    # data. At READ COMMITTED an index commit between them can drop a note
    # from both blocks. `SET TRANSACTION` must precede the first statement that
    # takes a snapshot; `SET LOCAL` takes none, and the caller hands over a
    # session with no statement run (`search_notes_impl`). READ ONLY: this
    # path never writes, and a read-only REPEATABLE READ transaction cannot
    # raise a serialization failure.
    await session.execute(
        text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
    )
    await session.execute(text("SET LOCAL random_page_cost = 1.1"))

    filters = dict(folder=folder, tags=tags, frontmatter=frontmatter, user_id=user_id)
    all_rows = await _ranked_block(session, and_query, limit=limit, **filters)
    results = [_row_dict(r, "all") for r in all_rows]

    snippet_rows = settings.keyword_snippet_rows
    plan: FallbackPlan | None = None
    fallback = "not_needed"
    any_query = None
    if len(results) < limit or snippet_rows > 0:
        # PostgreSQL's parse of the caller's query, never the caller's string
        # (design D3). Needed for the fallback and for `matched_terms`.
        plan = fallback_plan(await _parsed_texts(session, query))
    if len(results) < limit:
        if plan is not None and plan.eligible:
            any_query = fallback_tsquery(plan, query)
            any_rows = await _ranked_block(
                session, any_query, limit=limit - len(results),
                exclude=and_query, **filters,
            )
            results.extend(_row_dict(r, "any") for r in any_rows)
            fallback = "ran"
        else:
            fallback = "skipped"

    if snippet_rows > 0:
        started = time.monotonic()
        page = results[:snippet_rows]
        details: dict = {}
        all_ids = [r["id"] for r in page if r["match"] == "all"]
        any_ids = [r["id"] for r in page if r["match"] == "any"]
        if all_ids:
            details.update(await _snippets(session, all_ids, and_query, plan))
        if any_ids and any_query is not None:
            details.update(await _snippets(session, any_ids, any_query, plan))
        for r in page:
            r.update(
                details.get(
                    r["id"],
                    {
                        "snippet": None, "head": None, "matched_in": [],
                        "matched_terms": None, "has_excerpt": False,
                    },
                )
            )
        timing.add_ms("snippet_ms", time.monotonic() - started)

    for r in results:
        r.pop("id", None)
    # Result telemetry, recorded where the result set is final (#161). The
    # bounds live in `timing.record_results`, which is the record site the
    # `_tracked` merge cannot truncate — see `src/services/timing.py`. Outside
    # a tracked tool call (the panel, the benchmarks) there is no holder and
    # this is a no-op.
    timing.record("fallback", fallback)
    timing.record("any_rows", sum(1 for r in results if r["match"] == "any"))
    timing.record_results(r["path"] for r in results)
    return results
