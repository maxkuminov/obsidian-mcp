"""`notes_metadata.body_excerpt`, and a weight-`A` head on every keyword vector (#344).

`keyword_search` could not find a note by its own title: the vector held the
body alone, unweighted, so `ts_rank_cd` ranked a long note mentioning the
words many times above the note whose title *is* the query. Since this
migration a keyword vector is `A(title) || A(aliases) || D(body)` per FTS
config (`src/services/fts.py`), and each row stores the first
`KEYWORD_EXCERPT_CHARS` characters of the text its vector was built from, for
`ts_headline`.

## The column

`body_excerpt text NULL`, no default, **no backfill** — nothing in the
database holds the body, so the excerpt is written by the next content change
of each note or by one `make rebuild-tsvectors` (the k8s Job in
`deploy/kubernetes/base/rebuild-tsvectors-job.example.yaml`); startup logs
how many rows still lack one. 030's reconcile-or-refuse shape: a same-named
column of another type, NOT NULL or with a default is refused by name.

## The re-head, in SQL, with no file access

Every pre-031 vector is entirely weight `D` (`to_tsvector` assigns no other),
so `head(title, aliases) || ts_filter(content_tsvector, '{d}')` over stored
columns equals what a fresh build writes — byte-exact on the first application,
measured on PostgreSQL 16. Ranking is therefore right at deploy with no
operator step. Three rules make it safe:

- **Under the index generation lock**, acquired before the first row is read,
  so no pass can commit a vector between this migration's read and its write.
  The compose `make deploy` also stops the app before a pending migration
  (design D5), and k8s `Recreate`s, so there is normally nothing to wait for.
- **Under the configs the stored keyword fingerprint names**, not the
  environment's: the stored body lexemes were built under those, and a head
  under a different stemmer would mix two configurations in one vector — the
  exact mixed-stemmer state the startup fingerprint guard exists to refuse.
  Absent fingerprint → the environment's configs (what startup's adoption
  would bless, #206 L8); unreadable → refuse naming the key, as the guard does.
- **Skip a row that already carries a weight-`A` lexeme**
  (`length(ts_filter(content_tsvector, '{a}')) = 0`). `ts_filter` keeps the
  body's absolute positions as the previous concatenation assigned them, so a
  second re-head would shift them by the previous head's length; the
  predicate makes a stamp-back re-run byte-idempotent. A row whose head stems
  to nothing (title `The`) has no `A` part either, and an empty head shifts
  nothing.

Each UPDATE runs in its own savepoint. SQLSTATE 54000 (the 1 MiB tsvector
limit, for a vector already at the edge) leaves that row unchanged, counted
and logged by id — its next edit or the rebuild writes it through the halving
retreat; any other error fails the migration with nothing committed.

## Downgrade restores the old semantics, not only the schema

Under the generation lock, every vector is set to `ts_filter(content_tsvector,
'{d}')` — no `A` lexeme survives, so the previous image sees the pre-031
matching semantics and its later D-only writes do not mix with headed rows —
and only then is a marked `body_excerpt` dropped.

Revision ID: 031
Revises: 030
Create Date: 2026-10-29
"""
import json
import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.exc import DBAPIError

from src.config import settings
from src.services.fts import aliases_text, rehead_tsvector_sql

revision: str = "031"
down_revision: Union[str, None] = "030"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")


def _say(message: str, *args) -> None:
    """Log and print: the alembic subprocess may run with no logging
    configured (env.py installs none), and the operator reading a deploy log
    needs the re-head summary and any row left behind."""
    rendered = message % args if args else message
    logger.info(rendered)
    print(rendered, flush=True)

TABLE = "notes_metadata"
QUALIFIED = "public.notes_metadata"
COLUMN = "body_excerpt"
EXPECTED_TYPE = "text"

# Byte identical to `_BODY_EXCERPT_COLUMN_MARKER` in `src/models/db.py`,
# where it is the column's comment so `alembic check` compares it.
MARKER = "keyword excerpt (031_keyword_head_and_excerpt)"

#: `index_state.INDEX_GENERATION_LOCK_KEY`, written out literally as a
#: migration must; `tests/test_migration_031_literals.py` pins equality.
INDEX_GENERATION_LOCK_KEY = 8029183045093649969

#: `index_state.KEY_FTS_FINGERPRINT` / `FINGERPRINT_VERSION`, likewise pinned.
KEY_FTS_FINGERPRINT = "fts_fingerprint"
FINGERPRINT_VERSION = 1

REHEAD_STATEMENT_TIMEOUT = "15min"


def _quote(value: str) -> str:
    """A single-quoted SQL string literal (`COMMENT ON` takes no bind)."""
    return "'" + value.replace("'", "''") + "'"


def _oid(bind, name: str):
    return bind.execute(
        sa.text("SELECT CAST(to_regclass(:name) AS oid)"), {"name": name}
    ).scalar()


def _column_state(bind):
    """`(format_type, attnotnull, default_expr, comment)`, or None if absent."""
    return bind.execute(
        sa.text(
            "SELECT format_type(a.atttypid, a.atttypmod) AS coltype, "
            "       a.attnotnull, "
            "       pg_get_expr(d.adbin, d.adrelid) AS coldefault, "
            "       col_description(a.attrelid, a.attnum) AS comment "
            "FROM pg_attribute a "
            "LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
            "WHERE a.attrelid = CAST(:table AS regclass) AND a.attname = :column "
            "  AND a.attnum > 0 AND NOT a.attisdropped"
        ),
        {"table": QUALIFIED, "column": COLUMN},
    ).first()


def _pin_search_path() -> None:
    op.execute("SET LOCAL search_path TO public")


def _assert_is_the_qualified_table(bind) -> None:
    qualified = _oid(bind, QUALIFIED)
    unqualified = _oid(bind, TABLE)
    if qualified is None or qualified != unqualified:
        raise RuntimeError(
            f"031's {TABLE} is not the table {QUALIFIED} resolves to "
            f"(search_path-relative oid {unqualified!r}, qualified oid "
            f"{qualified!r}). Set the migration role's search_path so "
            "`public` comes first, then re-run."
        )


def _reconcile_column(bind) -> None:
    state = _column_state(bind)
    if state is None:
        op.add_column(TABLE, sa.Column(COLUMN, sa.Text(), nullable=True))
        op.execute(f"COMMENT ON COLUMN {TABLE}.{COLUMN} IS {_quote(MARKER)}")
        return
    coltype, notnull, default, comment = state
    problems = []
    if coltype != EXPECTED_TYPE:
        problems.append(f"{TABLE}.{COLUMN} is {coltype}, not {EXPECTED_TYPE}")
    if notnull:
        problems.append(
            f"{TABLE}.{COLUMN} is NOT NULL; 031 creates it nullable, and NULL "
            "is the value that means 'no excerpt stored yet'"
        )
    if default is not None:
        problems.append(
            f"{TABLE}.{COLUMN} has a server default of {default!r}; 031 "
            "creates none, and a default would present text the index never held"
        )
    if comment != MARKER:
        problems.append(f"{TABLE}.{COLUMN} does not carry 031's comment marker")
    if problems:
        raise RuntimeError(
            f"031 will not adopt an excerpt column of unknown provenance: "
            f"{'; '.join(problems)}. keyword_search renders this column's text "
            "as the note's own, so a shape this migration did not create is "
            "text of unknown origin presented as a snippet. Resolve by hand — "
            "drop the column and let 031 create it — then re-run. Nothing has "
            "been changed."
        )


def _take_generation_lock(bind) -> None:
    bind.execute(
        sa.text("SELECT pg_advisory_xact_lock(:key)"),
        {"key": INDEX_GENERATION_LOCK_KEY},
    )


def _stored_configs(bind) -> list[str]:
    """The FTS configs the stored vectors were built under (see the module
    docstring): the fingerprint's, the environment's when absent, refuse when
    unreadable."""
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
            f"031 cannot read indexer_state.{KEY_FTS_FINGERPRINT} ({exc.__class__.__name__}); "
            "the stored keyword fingerprint names the configs the vectors were built "
            "under, and a head built under a guessed config would mix two stemmers "
            "in one vector. Repair or remove the row, then re-run. Nothing has been changed."
        ) from exc
    configs = parsed.get("configs") if isinstance(parsed, dict) else None
    if (
        not isinstance(parsed, dict)
        or parsed.get("v") != FINGERPRINT_VERSION
        or not isinstance(configs, list)
        or not configs
        or not all(isinstance(c, str) and c for c in configs)
    ):
        raise RuntimeError(
            f"031 cannot interpret indexer_state.{KEY_FTS_FINGERPRINT} = {stored!r} "
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
    """The first SQLSTATE in the wrapper / `orig` / `__cause__` chain, the way
    `indexer.poison_sqlstate` walks it (never `__context__`)."""
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


def _rehead_every_row(bind, configs: list[str]) -> tuple[int, int, int]:
    """Returns `(re-headed, already headed, left at the size limit)`."""
    frag, cfg_params = rehead_tsvector_sql("head_title", "head_aliases", configs)
    update = sa.text(
        f"UPDATE {TABLE} SET content_tsvector = {frag} "
        "WHERE id = :id AND content_tsvector IS NOT NULL "
        "  AND length(ts_filter(content_tsvector, '{a}')) = 0"
    )
    total = bind.execute(
        sa.text(f"SELECT count(*) FROM {TABLE} WHERE content_tsvector IS NOT NULL")
    ).scalar() or 0
    rows = bind.execute(
        sa.text(
            f"SELECT id, title, frontmatter FROM {TABLE} "
            "WHERE content_tsvector IS NOT NULL "
            "  AND length(ts_filter(content_tsvector, '{a}')) = 0 "
            "ORDER BY id"
        )
    ).all()
    reheaded = skipped_size = 0
    for row_id, title, frontmatter in rows:
        params = {
            "id": row_id,
            "head_title": title or "",
            "head_aliases": aliases_text(_frontmatter(frontmatter)),
            **cfg_params,
        }
        try:
            with bind.begin_nested():
                result = bind.execute(update, params)
        except DBAPIError as exc:
            if _sqlstate(exc) != "54000":
                raise
            skipped_size += 1
            _say(
                "031: notes_metadata id=%s keeps its unweighted keyword vector — "
                "the re-headed vector exceeds PostgreSQL's size limit (54000). Its "
                "next edit, or `make rebuild-tsvectors`, writes it through the "
                "halving retreat.",
                row_id,
            )
            continue
        reheaded += result.rowcount
    return reheaded, total - len(rows), skipped_size


def upgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.execute(f"SET LOCAL statement_timeout = '{REHEAD_STATEMENT_TIMEOUT}'")
    _pin_search_path()

    _assert_is_the_qualified_table(bind)
    _reconcile_column(bind)

    # The generation lock before the first row is read, under the lock_timeout
    # above: on compose the app is stopped before a pending migration and on
    # k8s the old pod is gone, so a holder here is a pass somebody left
    # running against the deploy order — fail and say so rather than wait.
    _take_generation_lock(bind)
    configs = _stored_configs(bind)
    reheaded, already, at_limit = _rehead_every_row(bind, configs)
    _say(
        "031: keyword vectors re-headed under FTS configs %s — %d re-headed, "
        "%d already carried a head, %d left at the size limit. body_excerpt is "
        "NULL on every row until the note is edited or `make rebuild-tsvectors` "
        "runs (keyword_search says so per row).",
        configs, reheaded, already, at_limit,
    )

    op.execute("RESET lock_timeout")
    op.execute("RESET statement_timeout")
    op.execute("RESET search_path")


def downgrade() -> None:
    """Strip every weight-`A` lexeme, then drop the column if it carries 031's
    marker (see the module docstring)."""
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
            f"UPDATE {TABLE} SET content_tsvector = ts_filter(content_tsvector, '{{d}}') "
            "WHERE content_tsvector IS NOT NULL "
            "  AND length(ts_filter(content_tsvector, '{a}')) > 0"
        )
    ).rowcount
    _say("031 downgrade: stripped the weight-A head from %d keyword vector(s)", stripped)
    state = _column_state(bind)
    if state is not None:
        if state[3] != MARKER:
            print(
                f"031 downgrade: leaving {TABLE}.{COLUMN} in place. It does not "
                f"carry 031's marker ({MARKER!r}) — its comment is "
                f"{state[3]!r} — so 031 did not create it and will not drop it. "
                "Remove it by hand if you mean to.",
                flush=True,
            )
        else:
            op.drop_column(TABLE, COLUMN)
    op.execute("RESET statement_timeout")
    op.execute("RESET search_path")
