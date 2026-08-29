"""Record one row per index/embed pass (#160).

`indexer.last_index_run_at` is an in-process heartbeat: it answers "is this
process's loop alive" and resets to None on restart. That is the right answer
to the dashboard's question and no answer at all to "how long has an embed pass
been taking lately", which is the question the performance page exists for. The
alternative considered and rejected was parsing container logs — they rotate
with the container, so the history would not survive a redeploy.

## Shape

`id, started_at, finished_at, trigger, user_id, notes_scanned, notes_indexed,
notes_embedded, error`. The counters are `NOT NULL DEFAULT 0` so a row written
by a pass that died before it counted anything reads as a pass that did
nothing, not as a pass whose numbers are unknown.

`user_id` is nullable with `ON DELETE SET NULL`, not CASCADE. Single-user mode
has no `users` row at all, so nullable is the ordinary case rather than a
degraded one; and deleting a user must not erase the record that the server
spent an hour indexing their vault. Same reasoning as `usage_logs.user_id`.

`trigger` carries a CHECK rather than only a Python-side enum: the panel groups
and labels by this value, so a typo'd trigger would render as a silent fifth
category. The four values are mirrored in `src/models/db.py` as
`INDEXER_RUN_TRIGGERS` — keep the two in step.

## No backfill

There is nothing to backfill from: no record of any pass exists anywhere that
survives a container restart. A fresh table is the honest starting point, and
the first pass after the deploy writes the first row.

## Reconciliation, and why it is not a bare CREATE TABLE

The schema gate exercises idempotence by `alembic stamp 018` then
`upgrade head`, so this revision runs against a database that already carries
its table. A bare `CREATE TABLE` raises there; `IF NOT EXISTS` is worse,
because it adopts *any* table of that name — including one with a nullable
`trigger`, no CHECK, or an `ON DELETE CASCADE` on `user_id` — and the panel
then renders its rows to an operator as pass history. So 013's rule applies:
reconcile a database that demonstrably has our shape, refuse to guess for one
that does not.

The table carries a COMMENT marker, exactly as 013/015/016/017/018 mark what
they create, and that marker is what `downgrade()` keys on: a table of this
name that 019 did not create is never dropped.

The CHECK predicate is compared against **the server's own normalization of
the same declaration**, derived at runtime from a scratch `TEMP` table, not
against a string pinned to one PostgreSQL major. That needs the TEMP privilege
on the database — the deploy's owner role has it, as 013 already requires.

## Locks

`CREATE TABLE` blocks nothing already in use apart from the `ACCESS SHARE` the
FK to `users` takes to validate. `lock_timeout` / `statement_timeout` make a
blocked migration fail fast instead of stalling the deploy, and both are
`RESET` at the end because alembic runs every pending revision in one
transaction and `SET LOCAL` would otherwise leak into a later revision (013
through 018 do the same).

Revision ID: 019
Revises: 018
Create Date: 2026-08-29
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "019"
down_revision: Union[str, None] = "018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TABLE = "indexer_runs"
CHECK_NAME = "ck_indexer_runs_trigger"
STARTED_INDEX = "ix_indexer_runs_started_at"
USER_INDEX = "ix_indexer_runs_user_id"

# Mirrored from `src.models.db.INDEXER_RUN_TRIGGERS`. Written out as a literal
# rather than imported: a migration must keep describing the schema it created
# even after the model moves on, so it pins its own copy.
TRIGGERS = ("startup", "scheduled", "manual", "backfill")

TRIGGER_PREDICATE = "trigger IN (" + ", ".join(f"'{t}'" for t in TRIGGERS) + ")"

# 013's device, and 015's, 016's, 017's and 018's: the migration marks what it
# created, so `downgrade()` can tell its own work from somebody else's and drop
# only the former.
MARKER = "one row per index/embed pass (019_indexer_runs)"

# `(column, format_type, attnotnull)` — the whole shape, in creation order.
EXPECTED_COLUMNS = (
    ("id", "integer", True),
    ("started_at", "timestamp with time zone", True),
    ("finished_at", "timestamp with time zone", False),
    ("trigger", "character varying(16)", True),
    ("user_id", "integer", False),
    ("notes_scanned", "integer", True),
    ("notes_indexed", "integer", True),
    ("notes_embedded", "integer", True),
    ("error", "text", False),
)


def _quote(value: str) -> str:
    """A single-quoted SQL string literal. `MARKER` is a module constant with
    no quotes in it; the doubling is here so it stays correct if that changes."""
    return "'" + value.replace("'", "''") + "'"


def _table_comment(bind) -> str | None:
    return bind.execute(
        sa.text(
            "SELECT obj_description(c.oid, 'pg_class') "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.relname = :table AND c.relkind = 'r' "
            "  AND n.nspname = ANY (current_schemas(false))"
        ),
        {"table": TABLE},
    ).scalar()


def _table_exists(bind) -> bool:
    return bool(
        bind.execute(
            sa.text("SELECT to_regclass(:qualified)"), {"qualified": TABLE}
        ).scalar()
    )


def _columns(bind):
    return [
        (row[0], row[1], row[2])
        for row in bind.execute(
            sa.text(
                "SELECT a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull "
                "FROM pg_attribute a "
                "WHERE a.attrelid = CAST(:table AS regclass) "
                "  AND a.attnum > 0 AND NOT a.attisdropped "
                "ORDER BY a.attnum"
            ),
            {"table": TABLE},
        ).fetchall()
    ]


def _canonical_check(bind) -> str:
    """What this server renders `TRIGGER_PREDICATE` as, measured not guessed.

    013's scratch-TEMP-table device. A hand-written expected string pins the
    migration to one PostgreSQL major's normalization of `IN (...)`; deriving
    it from an empty temp table carrying the identical declaration cannot
    drift.
    """
    scratch = "_omcp_019_check_probe"
    bind.execute(sa.text(f"DROP TABLE IF EXISTS pg_temp.{scratch}"))
    bind.execute(
        sa.text(
            f"CREATE TEMP TABLE {scratch} "
            f'("trigger" varchar(16), CONSTRAINT {CHECK_NAME}_probe '
            f"CHECK ({TRIGGER_PREDICATE}))"
        )
    )
    rendered = bind.execute(
        sa.text(
            "SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c "
            "WHERE c.conrelid = CAST(:scratch AS regclass) AND c.contype = 'c'"
        ),
        {"scratch": f"pg_temp.{scratch}"},
    ).scalar()
    bind.execute(sa.text(f"DROP TABLE IF EXISTS pg_temp.{scratch}"))
    return rendered


def _live_check(bind) -> str | None:
    return bind.execute(
        sa.text(
            "SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c "
            "WHERE c.conrelid = CAST(:table AS regclass) AND c.contype = 'c' "
            "  AND c.convalidated"
        ),
        {"table": TABLE},
    ).scalar()


def _user_fk_delete_action(bind) -> str | None:
    """`confdeltype` for the FK on `user_id`: 'n' is SET NULL, 'c' CASCADE."""
    return bind.execute(
        sa.text(
            "SELECT c.confdeltype::text FROM pg_constraint c "
            "WHERE c.conrelid = CAST(:table AS regclass) AND c.contype = 'f' "
            "  AND c.conkey = ARRAY[(SELECT a.attnum FROM pg_attribute a "
            "        WHERE a.attrelid = c.conrelid AND a.attname = 'user_id')]"
        ),
        {"table": TABLE},
    ).scalar()


def _index_names(bind) -> set:
    return {
        row[0]
        for row in bind.execute(
            sa.text(
                "SELECT indexname FROM pg_indexes WHERE tablename = :table "
                "AND schemaname = ANY (current_schemas(false))"
            ),
            {"table": TABLE},
        ).fetchall()
    }


def _create(bind) -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("trigger", sa.String(length=16), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column(
            "notes_scanned", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "notes_indexed", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "notes_embedded", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.CheckConstraint(TRIGGER_PREDICATE, name=CHECK_NAME),
    )
    # The panel reads this table newest-first over a bounded window, and the
    # pruner orders by the same column.
    op.create_index(STARTED_INDEX, TABLE, ["started_at"])
    op.create_index(USER_INDEX, TABLE, ["user_id"])
    # Stamped in the same transaction as the CREATE, so the marker and the
    # table can never disagree about who made it. `COMMENT ON` is utility DDL
    # and takes no bind parameter, so the literal is quoted.
    op.execute(f"COMMENT ON TABLE {TABLE} IS {_quote(MARKER)}")


def _verify(bind) -> None:
    """Accept a pre-existing table only if it is exactly 019's."""
    problems = []

    if _table_comment(bind) != MARKER:
        problems.append("it does not carry 019's comment marker")

    columns = _columns(bind)
    if columns != list(EXPECTED_COLUMNS):
        problems.append(f"its columns are {columns}, not {list(EXPECTED_COLUMNS)}")

    live = _live_check(bind)
    canonical = _canonical_check(bind)
    if live != canonical:
        problems.append(
            f"its CHECK is {live!r}, not the trigger predicate {canonical!r}"
        )

    delete_action = _user_fk_delete_action(bind)
    if delete_action != "n":
        problems.append(
            f"its user_id foreign key deletes with {delete_action!r}, not SET NULL"
        )

    missing = {STARTED_INDEX, USER_INDEX} - _index_names(bind)
    if missing:
        problems.append(f"it is missing index(es) {sorted(missing)}")

    if problems:
        raise RuntimeError(
            f"{TABLE} already exists but {'; '.join(problems)}. 019 will not "
            "adopt a table of unknown provenance: the panel renders its rows "
            "to an operator as the history of what the indexer did, and a "
            "table this migration did not create is not that history. Resolve "
            "by hand — drop it and let 019 create it, or make it match — then "
            "re-run. Nothing has been changed."
        )


def upgrade() -> None:
    bind = op.get_bind()

    # Fail fast rather than queueing behind a long-lived transaction: the
    # deploy migrates before recreating the container, so a stalled migration
    # is a stalled deploy while the old container is still serving. Per
    # statement and per lock acquisition, not a budget for the transaction.
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.execute("SET LOCAL statement_timeout = '60s'")

    if _table_exists(bind):
        _verify(bind)
    else:
        _create(bind)

    # `SET LOCAL` is scoped to the transaction and alembic runs every pending
    # revision in *one*, so without this the next revision would silently
    # inherit these timeouts and blame its own SQL when it tripped them.
    op.execute("RESET lock_timeout")
    op.execute("RESET statement_timeout")


def downgrade() -> None:
    """Drop the table only if it carries 019's marker.

    013's rule: a downgrade must undo *this* migration, not delete a table
    somebody else put there under this name. The marker is the only evidence
    of authorship.

    Dropping loses an operator's view of the last 500 passes and nothing else
    — the history is display only, and no code reads it for a decision. The
    next upgrade starts a fresh one.
    """
    bind = op.get_bind()
    if not _table_exists(bind):
        return
    if _table_comment(bind) != MARKER:
        raise RuntimeError(
            f"{TABLE} does not carry 019's comment marker ({MARKER!r}), so 019 "
            "did not create it and will not drop it. Nothing has been changed. "
            "Remove it by hand if you mean to."
        )
    op.drop_index(USER_INDEX, table_name=TABLE)
    op.drop_index(STARTED_INDEX, table_name=TABLE)
    op.drop_table(TABLE)
