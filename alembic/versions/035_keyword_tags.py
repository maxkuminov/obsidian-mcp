"""Tags in the keyword vector: a weight-`B` part between the head and the body (#391).

`keyword_search` could not find a note by a word it carried only as a tag:
the vector was `A(title) || A(aliases) || D(body)` (031, #344) and
`notes_metadata.tags` sat in no `tsvector`. Since this migration it is
**`A(title) || A(aliases) || B(tags) || D(body)`** per FTS config, the tags
text being `fts.tags_text` over the stored tags — each tag's full form plus
its `/`-segments, cut at 2,048 characters on whole tags. No schema object
changes; `alembic check` stays clean.

## The re-head, in SQL, with no file access

`head(title, aliases, tags) || ts_filter(content_tsvector, '{d}')` from the
stored `title`, `frontmatter` and `tags` columns, through the shared
`rehead_tsvector_sql` builder — 031's shape, for the same reason: with
deploy-on-merge, ranking that is wrong until an operator runs a rebuild is
the bug shipped under a new name (design D5). The rules:

- **Under the index generation lock**, before the first row is read; the
  compose `make deploy` stops the app before a pending migration and k8s
  `Recreate`s, so normally nothing holds it.
- **Under the configs the stored keyword fingerprint names** (present → its
  `configs`; absent → the environment's; unreadable → refuse naming the key,
  nothing changed), so no vector mixes two stemmers.
- **Two skip predicates in the UPDATE's own `WHERE`:**
  `length(ts_filter(content_tsvector, '{b}')) = 0` — a row already carrying
  a tags part is skipped, because `rehead` is not position-idempotent (each
  application shifts the body by the previous head's length), and this is
  what makes a stamp-back re-run change no byte; and `length(<tags part>) >
  0` — a row whose tags produce no lexeme (none, or only stop words) is
  already byte-equal to what the new builder writes (an empty `B` part shifts
  nothing), and re-heading it would only shift its body.

## The size preflight, then all-or-nothing

Before the first UPDATE, read-only and under the same lock and configs, every
row the rewrite would change is estimated as `S(ts_filter(v, '{d}')) +
S(head(title, aliases, tags))`, where `S(v)` sums over `unnest(v)` the
lexeme's bytes rounded up to even plus `2 + 2 × npos` when it has positions —
PostgreSQL's own accounting of a tsvector's string area, an upper bound for a
concatenation (a lexeme in two parts is stored once). Any estimate above
1,048,511 (the 1,048,575-byte limit less a 64-byte margin; measured within
four bytes of PostgreSQL's own count) aborts the migration before anything is
written, naming the count and the row ids — so a Kubernetes `migrate`
initContainer fails in seconds, not after rewriting rows.

The rewrite is **all-or-nothing** (Codex r1 B1): no savepoints; any error on
any row, SQLSTATE 54000 included, fails the whole migration with nothing
committed and names the row's id and the SQLSTATE — never the path or any
note text (the driver's message, which can carry the bound parameters, is
not chained). A migration that succeeded left no tagged row untagged. The
remedy is in `DEPLOYMENT.md`: the database is still at 034; serve the
previous image, shorten the named note, let it re-index, deploy again.

## Downgrade

Under the generation lock, every vector carrying a weight-`B` lexeme becomes
`ts_filter(content_tsvector, '{a,d}')`: the previous image sees the pre-035
semantics, and a lexeme in both the tags and the body keeps its body
positions.

Revision ID: 035
Revises: 034
Create Date: 2026-10-30
"""
import json
import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.exc import DBAPIError

from src.config import settings
from src.services.fts import (
    aliases_text,
    head_tsvector_sql,
    rehead_tsvector_sql,
    tags_text,
    tags_tsvector_sql,
)

revision: str = "035"
down_revision: Union[str, None] = "034"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

TABLE = "notes_metadata"
QUALIFIED = "public.notes_metadata"

#: `index_state.INDEX_GENERATION_LOCK_KEY`, written out literally as a
#: migration must; `tests/test_migration_035_literals.py` pins equality.
INDEX_GENERATION_LOCK_KEY = 8029183045093649969

#: `index_state.KEY_FTS_FINGERPRINT` / `FINGERPRINT_VERSION`, likewise pinned.
KEY_FTS_FINGERPRINT = "fts_fingerprint"
FINGERPRINT_VERSION = 1

REHEAD_STATEMENT_TIMEOUT = "15min"

#: PostgreSQL's tsvector string-area limit (`MAXSTRPOS`, 2^20 - 1) less a
#: 64-byte margin for the estimate's measured four-byte slack (design D5).
TSVECTOR_LIMIT_BYTES = 1048575
PREFLIGHT_MAX_BYTES = TSVECTOR_LIMIT_BYTES - 64

#: How many flagged ids the preflight's refusal lists before an ellipsis.
PREFLIGHT_REPORT_LIMIT = 50


def _say(message: str, *args) -> None:
    """Log and print: the alembic subprocess may run with no logging
    configured, and the operator reading a deploy log needs the summary."""
    rendered = message % args if args else message
    logger.info(rendered)
    print(rendered, flush=True)


def size_sql(vector_sql: str) -> str:
    """`S(v)` as a scalar subquery over `vector_sql` (design D5): Σ over
    `unnest(v)` of the lexeme's bytes rounded up to even, plus `2 + 2 ×
    npos` when it has positions."""
    return (
        "(SELECT coalesce(sum((octet_length(lexeme) + 1) / 2 * 2 "
        "+ CASE WHEN cardinality(positions) > 0 "
        "THEN 2 + 2 * cardinality(positions) ELSE 0 END), 0) "
        f"FROM unnest({vector_sql}))"
    )


def _oid(bind, name: str):
    return bind.execute(
        sa.text("SELECT CAST(to_regclass(:name) AS oid)"), {"name": name}
    ).scalar()


def _pin_search_path() -> None:
    op.execute("SET LOCAL search_path TO public")


def _assert_is_the_qualified_table(bind) -> None:
    qualified = _oid(bind, QUALIFIED)
    unqualified = _oid(bind, TABLE)
    if qualified is None or qualified != unqualified:
        raise RuntimeError(
            f"035's {TABLE} is not the table {QUALIFIED} resolves to "
            f"(search_path-relative oid {unqualified!r}, qualified oid "
            f"{qualified!r}). Set the migration role's search_path so "
            "`public` comes first, then re-run. Nothing has been changed."
        )


def _take_generation_lock(bind) -> None:
    bind.execute(
        sa.text("SELECT pg_advisory_xact_lock(:key)"),
        {"key": INDEX_GENERATION_LOCK_KEY},
    )


def _stored_configs(bind) -> list[str]:
    """The FTS configs the stored vectors were built under: the fingerprint's,
    the environment's when absent, refuse when unreadable (031's rule)."""
    if _oid(bind, "public.indexer_state") is None:
        return list(settings.fts_configs)
    stored = bind.execute(
        sa.text("SELECT value FROM indexer_state WHERE key = :key"),
        {"key": KEY_FTS_FINGERPRINT},
    ).scalar()
    if stored is None:
        return list(settings.fts_configs)
    try:
        parsed = json.loads(stored)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"035 cannot read indexer_state.{KEY_FTS_FINGERPRINT} ({exc.__class__.__name__}); "
            "the stored keyword fingerprint names the configs the vectors were built "
            "under, and a tags part built under a guessed config would mix two "
            "stemmers in one vector. Repair or remove the row, then re-run. Nothing "
            "has been changed."
        ) from None
    configs = parsed.get("configs") if isinstance(parsed, dict) else None
    if (
        not isinstance(parsed, dict)
        or parsed.get("v") != FINGERPRINT_VERSION
        or not isinstance(configs, list)
        or not configs
        or not all(isinstance(c, str) and c for c in configs)
    ):
        raise RuntimeError(
            f"035 cannot interpret indexer_state.{KEY_FTS_FINGERPRINT} = {stored!r} "
            f"(expected a v{FINGERPRINT_VERSION} object with a non-empty 'configs' list). "
            "Repair or remove the row, then re-run. Nothing has been changed."
        )
    return [str(c) for c in configs]


def _frontmatter(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, (str, bytes)):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _sqlstate(exc: BaseException) -> str | None:
    """The first SQLSTATE in the wrapper / `orig` / `__cause__` chain (031's
    walk; never `__context__`)."""
    seen: set[int] = set()
    queue: list = [exc]
    while queue:
        e = queue.pop(0)
        if e is None or id(e) in seen:
            continue
        seen.add(id(e))
        for attr in ("sqlstate", "pgcode"):
            code = getattr(e, attr, None)
            if isinstance(code, str) and len(code) == 5:
                return code
        queue.extend([getattr(e, "orig", None), e.__cause__])
    return None


class RowFailure(RuntimeError):
    """035 failed on one row; nothing is committed. Carries the id and the
    SQLSTATE only — never the path, the bound parameters or note text."""


def _row_failure(stage: str, row_id, exc: BaseException) -> RowFailure:
    code = _sqlstate(exc) or "unknown"
    hint = (
        " — the tagged keyword vector would exceed PostgreSQL's 1 MiB tsvector "
        "limit" if code == "54000" else ""
    )
    return RowFailure(
        f"035 failed during the {stage} at notes_metadata id={row_id} "
        f"(SQLSTATE {code}){hint}. Nothing has been committed: the database is "
        "still at revision 034. Serve the previous image, shorten that note's "
        "body or tags, let the previous image re-index it, then deploy again "
        "(DEPLOYMENT.md, 'Upgrading across migration 035')."
    )


def _binds(row, configs: list[str]) -> dict:
    _id, title, frontmatter, tags = row
    return {
        "id": _id,
        "head_title": title or "",
        "head_aliases": aliases_text(_frontmatter(frontmatter)),
        "head_tags": tags_text(list(tags) if tags is not None else None),
    }


def _candidates(bind) -> tuple[int, list]:
    """`(rows with a vector, rows with a vector and no tags part yet)`."""
    total = bind.execute(
        sa.text(f"SELECT count(*) FROM {TABLE} WHERE content_tsvector IS NOT NULL")
    ).scalar() or 0
    rows = bind.execute(
        sa.text(
            f"SELECT id, title, frontmatter, tags FROM {TABLE} "
            "WHERE content_tsvector IS NOT NULL "
            "  AND length(ts_filter(content_tsvector, '{b}')) = 0 "
            "ORDER BY id"
        )
    ).all()
    return total, rows


def preflight(bind, configs: list[str], rows) -> tuple[list[dict], int]:
    """The read-only size preflight (design D5). Returns `(the rows the
    rewrite will change, with their binds; the count without tag lexemes)`,
    or raises naming every row whose estimate exceeds `PREFLIGHT_MAX_BYTES`
    before anything is written."""
    head, cfg_params = head_tsvector_sql(
        "head_title", "head_aliases", configs, tags_bind="head_tags"
    )
    tags_part, _ = tags_tsvector_sql("head_tags", configs)
    body_size = size_sql("ts_filter(content_tsvector, '{d}')")
    estimate = sa.text(
        f"SELECT length({tags_part}) AS tag_lexemes, "
        f"{body_size} + {size_sql(head)} AS bytes "
        f"FROM {TABLE} WHERE id = :id"
    )
    work: list[dict] = []
    without_lexemes = 0
    flagged: list = []
    for row in rows:
        params = _binds(row, configs)
        if not params["head_tags"]:
            without_lexemes += 1
            continue
        try:
            got = bind.execute(estimate, {**params, **cfg_params}).first()
        except DBAPIError as exc:
            raise _row_failure("size preflight", params["id"], exc) from None
        if got is None:
            continue
        if not got.tag_lexemes:
            without_lexemes += 1
            continue
        if got.bytes > PREFLIGHT_MAX_BYTES:
            flagged.append(params["id"])
        work.append(params)
    if flagged:
        shown = ", ".join(str(i) for i in flagged[:PREFLIGHT_REPORT_LIMIT])
        more = " …" if len(flagged) > PREFLIGHT_REPORT_LIMIT else ""
        raise RuntimeError(
            f"035 size preflight: {len(flagged)} tagged keyword vector(s) would "
            f"exceed PostgreSQL's 1 MiB tsvector limit once the tags part is "
            f"added (estimate over {PREFLIGHT_MAX_BYTES} bytes): notes_metadata "
            f"id(s) {shown}{more}. Nothing has been changed: the database is "
            "still at revision 034. Serve the previous image, shorten each "
            "named note's body or tags, let the previous image re-index it, "
            "then deploy again (DEPLOYMENT.md, 'Upgrading across migration 035')."
        )
    return work, without_lexemes


def rewrite(bind, configs: list[str], work: list[dict]) -> int:
    """The all-or-nothing re-head of `work` (no savepoints). Returns the
    number of rows re-headed; any row error raises `RowFailure`."""
    frag, cfg_params = rehead_tsvector_sql(
        "head_title", "head_aliases", configs, tags_bind="head_tags"
    )
    tags_part, _ = tags_tsvector_sql("head_tags", configs)
    update = sa.text(
        f"UPDATE {TABLE} SET content_tsvector = {frag} "
        "WHERE id = :id AND content_tsvector IS NOT NULL "
        "  AND length(ts_filter(content_tsvector, '{b}')) = 0 "
        f"  AND length({tags_part}) > 0"
    )
    reheaded = 0
    for params in work:
        try:
            result = bind.execute(update, {**params, **cfg_params})
        except DBAPIError as exc:
            raise _row_failure("rewrite", params["id"], exc) from None
        reheaded += result.rowcount
    return reheaded


def upgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.execute(f"SET LOCAL statement_timeout = '{REHEAD_STATEMENT_TIMEOUT}'")
    _pin_search_path()
    _assert_is_the_qualified_table(bind)

    # The generation lock before the first row is read (031's order): a
    # holder here is a pass left running against the deploy order.
    _take_generation_lock(bind)
    configs = _stored_configs(bind)
    total, rows = _candidates(bind)
    work, without_lexemes = preflight(bind, configs, rows)
    reheaded = rewrite(bind, configs, work)
    _say(
        "035: tags added to the keyword vectors under FTS configs %s — %d "
        "re-headed, %d already tagged, %d without tag lexemes.",
        configs, reheaded, total - len(rows), without_lexemes,
    )

    op.execute("RESET lock_timeout")
    op.execute("RESET statement_timeout")
    op.execute("RESET search_path")


def downgrade() -> None:
    """Strip every weight-`B` lexeme (see the module docstring)."""
    bind = op.get_bind()
    _pin_search_path()
    if _oid(bind, QUALIFIED) is None:
        op.execute("RESET search_path")
        return
    _assert_is_the_qualified_table(bind)
    op.execute(f"SET LOCAL statement_timeout = '{REHEAD_STATEMENT_TIMEOUT}'")
    _take_generation_lock(bind)
    stripped = bind.execute(
        sa.text(
            f"UPDATE {TABLE} SET content_tsvector = ts_filter(content_tsvector, '{{a,d}}') "
            "WHERE content_tsvector IS NOT NULL "
            "  AND length(ts_filter(content_tsvector, '{b}')) > 0"
        )
    ).rowcount
    _say("035 downgrade: stripped the weight-B tags part from %d keyword vector(s)", stripped)
    op.execute("RESET statement_timeout")
    op.execute("RESET search_path")
