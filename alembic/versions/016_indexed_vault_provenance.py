"""Record what each user's index was scanned under (issue #91, deferred half).

Nothing anywhere recorded which vault assignment a user's `notes_metadata`
rows were built from. `notes_metadata.file_path` is vault-relative, so after an
administrator repoints a user at another vault the metadata-only tools —
`semantic_search`, `keyword_search`, `list_notes`, `get_recent` and every graph
tool, all served from the database filtered by `user_id` alone — go on
answering from the *previous* vault: its paths, titles, tags, frontmatter and
chunk excerpts. `read_note` on one of those paths then either fails or, worse,
returns a genuinely different note that happens to occupy the same relative
path in the new root.

The obvious objection is mostly right: `index_vault` already prunes every row
whose relative path is absent under the new root. Two things do not reconcile,
and they are the whole justification for a column.

- **The window.** The prune happens at the *next* pass, so between the Save and
  that pass the database-backed tools answer from the previous root.
- **A note identical by relative path *and* content hash in both roots.** The
  scan classes it "no change" and skips it, so its links are never
  re-extracted — while the notes it pointed *at* were pruned, and
  `note_links.target_note_id` is `ON DELETE SET NULL`. The link row keeps its
  `target_path` and loses its resolution, permanently, because nothing will
  ever re-parse a note whose hash keeps matching. That is not a window; it does
  not heal.

## Why a record rather than a comparison in the panel

The transition an operator actually performs is two Saves:

    /old  ->  unassigned  ->  /new

because the vault selector is how an admin takes an account out of service
before repointing it. On the second Save the handler sees `old_vault = None`,
and `None -> /new` is byte for byte the shape of a *restore* — the case #66
deliberately protects, where the rows must be kept so a reassignment costs no
re-embed. The handler cannot tell the two apart, because the only thing that
distinguishes them is a value that no longer exists anywhere by the time it is
needed. What is required is not a comparison but a **record**, independent of
the current assignment and therefore surviving the unassignment.

## The three columns, and what each is *not*

- `indexed_vault_assignment` — the canonical assignment string
  (`str(Path(users.vault_path))`, `transfer.canonical_vault_root`'s form). The
  load-bearing fact: it changes when an operator reassigns and for no other
  reason, because it *is* the operator's saved value.
- `indexed_vault_realpath` — `os.path.realpath` of the directory that
  assignment named when the pass ran, stored as `os.fsencode(...).hex()`. Its
  only job is to stop the assignment comparison from firing destructively on a
  cosmetic rename or an alias. **Not** a proof of directory identity.
- `indexed_vault_handle` — an opaque `name_to_handle_at` token, best-effort
  hardening in the refusing direction only: it can demote a keep to a
  re-derive and can never establish anything. NULL means "no hardening signal",
  never "provenance unknown".

Both pathname columns are `text` under one rule: **a provenance column must be
able to record any value the fact it mirrors can take.** A short assignment may
be a symbolic link to a canonical path of any length, and this system owns no
bound on that. The cost of getting it wrong is not a clipped display string —
the discard branch writes the record *and* the delete in **one** transaction,
so an oversized value raises `string_data_right_truncation`, the delete rolls
back with it, every later pass repeats the failure, and the database-backed
tools keep serving the former vault forever. A column width would have
reproduced #91's own symptom.

`text` alone does not satisfy that rule, which is why the realpath is stored
hex-encoded: a POSIX pathname is arbitrary non-NUL bytes, Python decodes a
non-UTF-8 component with `surrogateescape`, and the resulting lone surrogate
cannot be UTF-8-encoded by the driver — the identical rollback-forever failure
reached through the value domain instead of the length. Hex has no
unrepresentable input, so the column is total by construction.

`indexed_vault_handle` keeps `varchar(320)` and its NULL-on-oversize rule
because it is a different kind of value: a comparison token with a documented
external maximum (`MAX_HANDLE_SZ` = 128 bytes -> 256 hex characters, plus a
type and a separator), whose *absence* is a defined state. A missing pathname
is not a state at all but a half-set record.

## Backfill nothing — the load-bearing decision, not an omission

`indexed_vault_assignment = vault_path` looks free and is not. "Assigned now"
is not "indexed under what is assigned now", and that reassignment lag is the
exact defect this change exists to close. An administrator who reassigns and
deploys before the next index pass would get rows built under vault A stamped
as belonging to B; the next pass then sees both recorded facts agree, takes its
no-op branch, and the identical-path/identical-hash link case above — the one
that never heals — becomes *guaranteed* rather than merely possible.

NULL is the only true statement available at migration time. Under the pass's
classification NULL is not "stamp and move on": it is the **provenance
unknown** branch, which **re-derives** the index rather than discarding it. So
introducing these columns costs no vault-wide re-embed, and every legacy user —
including one reassigned before the upgrade — is repaired once, cheaply, on the
first pass after it.

## Deploy order, and what actually holds

`make deploy` runs `alembic upgrade head` in a one-off container **while the
old container is still running**, and only then recreates it. So an old-code
index pass can be mid-flight when this migration commits and can go on
committing `notes_metadata` rows from the old root afterwards. **Nothing
serialises the two**: `index_pass_lock` is an in-process `asyncio.Lock`, and
there is no advisory lock, no row lock and no cross-container coordination.

What makes the deploy safe is not a lock but the absence of a backfill: **016
writes no provenance, so an old pass's writes have nothing to contradict.** Old
code cannot write any of these three columns — they are not on its models and
no code path sets them — so every row is NULL when the new container starts,
whatever the old pass committed, and the new container's first pass per user
takes the unknown branch and re-derives from the assigned root.

The residual is therefore a property of the deploy command rather than of the
code: a deploy that runs two indexing containers of this service concurrently —
a second replica, a rolling deploy, a manually started container — can let an
old pass commit rows from the old root after a new pass has stamped the new
one. `make deploy` does not do that; an operator who changes it must quiesce
the old container before migrating.

## Ownership

The marker is load-bearing for a stronger reason here than on a display
column. This record is the sole input to a decision that can **delete a user's
entire index** — `notes_metadata` and, by cascade, their embeddings and link
rows. A same-named column of unknown provenance adopted as "the assignment
those rows were scanned under" is a mass delete on the strength of a value
nobody in this scheme wrote. So 016 refuses a pre-existing column of any of the
three names that is not exactly its own, and refuses a partial set, naming what
it found rather than adopting it.

## Locks

Three `ALTER TABLE ... ADD COLUMN` without a default, which take ACCESS
EXCLUSIVE on `users` and hold it to COMMIT but rewrite no row. `lock_timeout` /
`statement_timeout` make a blocked migration fail fast instead of stalling the
deploy while the old container is still serving; both are `RESET` at the end
because alembic runs every pending revision in *one* transaction and `SET
LOCAL` would otherwise leak into a later revision (013, 014 and 015 do the same
for the same reason).

Revision ID: 016
Revises: 015
Create Date: 2026-08-24
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "016"
down_revision: Union[str, None] = "015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TABLE = "users"

# 013's device, and 015's: the migration marks what it created, so
# `downgrade()` can tell its own work from somebody else's and drop only the
# former. Here it does a second and heavier job — it is the only evidence that
# the values in these columns were written by *this* provenance scheme, which
# is the whole basis for letting them decide a mass delete.
#
# Declared on the ORM columns too (`src/models/db.py`), so `alembic check`
# compares it like any other column attribute: a marker that drifted from the
# model, or one silently dropped, is a dirty check rather than a migration that
# quietly stops recognising its own work. Keep the two byte identical.
MARKER = (
    "provenance of this user's index, recorded by the index pass "
    "(016_indexed_vault_provenance)"
)

# (name, SQLAlchemy type, how PostgreSQL prints it). The three are one owned
# unit: all absent, or all present in exactly this shape. See `_reconcile`.
COLUMNS = (
    ("indexed_vault_assignment", sa.Text(), "text"),
    ("indexed_vault_realpath", sa.Text(), "text"),
    ("indexed_vault_handle", sa.String(320), "character varying(320)"),
)
COLUMN_NAMES = tuple(name for name, _t, _e in COLUMNS)


def _quote(value: str) -> str:
    """A single-quoted SQL string literal. `MARKER` is a module constant with
    no quotes in it; the doubling is here so it stays correct if that changes."""
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


def _column_state(bind, column: str):
    """`(formatted_type, attnotnull, default_expr, comment)`, or None if absent."""
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
        {"table": TABLE, "column": column},
    ).first()


def _describe(column: str, state) -> str:
    if state is None:
        return f"{column}: absent"
    coltype, notnull, default, comment = state
    return (
        f"{column}: {coltype}"
        f"{' NOT NULL' if notnull else ''}"
        f"{f' DEFAULT {default}' if default else ''}"
        f"{'' if comment == MARKER else f' comment={comment!r}'}"
    )


def _refuse(states, why: str) -> None:
    found = "; ".join(_describe(name, states[name]) for name in COLUMN_NAMES)
    raise RuntimeError(
        f"{TABLE} index-provenance columns: {why} Found -- {found}. 016 owns "
        "these three columns as one unit: it creates all of them and marks each "
        f"with the comment {MARKER!r}, and it completes only a set it can prove "
        "it wrote. It will not adopt columns of unknown provenance, because "
        "the index pass reads them to decide whether to DELETE a user's entire "
        "index. Resolve by hand -- drop them and let 016 create them, or make "
        "them match -- then re-run. Nothing has been changed."
    )


def _reconcile(bind) -> None:
    """Create the three columns, or verify a pre-existing set is 016's own.

    013's, 014's and 015's philosophy: reconcile a database that demonstrably
    has our shape, refuse to guess for one that does not. Three details are
    load-bearing:

    - **All three or none.** A *partial* set is not a re-run; it is a database
      somebody edited. Creating the missing ones beside a foreign
      `indexed_vault_assignment` would leave the pass classifying against a
      value of unknown meaning — and this classification deletes indexes.
    - **The whole shape, not just the type.** A `NOT NULL` column, or one
      carrying a server default, is not what 016 creates, and neither is
      visible to `alembic check`'s notion of "the column exists". Nullability
      is the load-bearing half: NULL is the *provenance unknown* branch, and a
      column that cannot hold it has no way to say "nothing is known".
    - **The marker.** Type and width alone are a coincidence anyone could
      reproduce; the comment is the only evidence that *this* scheme wrote the
      values. Without it a hand-made `text` column holding an arbitrary
      pathname would be adopted and compared, and one mismatch is a mass
      delete.
    """
    states = {name: _column_state(bind, name) for name in COLUMN_NAMES}
    present = [name for name in COLUMN_NAMES if states[name] is not None]

    if not present:
        for column, type_, _expected in COLUMNS:
            op.add_column(TABLE, sa.Column(column, type_, nullable=True))
            # Stamped immediately, in the same transaction as the ADD, so the
            # marker and the column can never disagree about who made it.
            # `COMMENT ON` takes no bind parameter -- it is utility DDL, not a
            # planned statement -- so the literal is quoted rather than bound.
            op.execute(f"COMMENT ON COLUMN {TABLE}.{column} IS {_quote(MARKER)}")
        return

    if len(present) != len(COLUMN_NAMES):
        missing = [name for name in COLUMN_NAMES if states[name] is None]
        _refuse(states, f"only {', '.join(present)} exist ({', '.join(missing)} absent).")

    for column, _type, expected in COLUMNS:
        coltype, notnull, default, comment = states[column]
        if coltype != expected:
            _refuse(states, f"{column} is {coltype}, not {expected}.")
        if notnull:
            _refuse(states, f"{column} is NOT NULL; 016 creates it nullable.")
        if default is not None:
            _refuse(states, f"{column} carries a server default; 016 creates none.")
        if comment != MARKER:
            _refuse(states, f"{column} does not carry 016's comment marker.")


def upgrade() -> None:
    bind = op.get_bind()

    # Fail fast rather than queueing behind a long-lived transaction: the
    # deploy migrates before recreating the container, so a stalled migration
    # is a stalled deploy while the old container is still serving. Per
    # statement and per lock acquisition, not a budget for the transaction.
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.execute("SET LOCAL statement_timeout = '60s'")

    _reconcile(bind)

    # **No backfill of any kind**, deliberately, and re-running this migration
    # is therefore trivially non-destructive: there is no statement here that
    # could rewrite a provenance the indexer has since recorded. See the
    # module docstring for why deriving the assignment from `users.vault_path`
    # is not the cheap win it looks like.

    # `SET LOCAL` is scoped to the transaction and alembic runs every pending
    # revision in *one*, so without this the next revision would silently
    # inherit these timeouts and blame its own SQL when it tripped them.
    op.execute("RESET lock_timeout")
    op.execute("RESET statement_timeout")


def downgrade() -> None:
    """Drop only the columns carrying 016's marker, all or nothing.

    013's rule, for the same reason: a downgrade must undo *this* migration,
    not delete a column somebody else put there under one of these names. The
    marker is the only evidence of authorship, so it is what the drop keys on —
    an unmarked `indexed_vault_realpath` is left in place and reported, rather
    than destroyed on the way past.

    Downgrading discards every recorded provenance. Re-upgrading recreates the
    columns NULL, which is the *provenance unknown* branch, so the next pass
    re-derives each user's index rather than trusting or discarding it — a
    bounded, non-destructive cost.
    """
    bind = op.get_bind()
    states = {name: _column_state(bind, name) for name in COLUMN_NAMES}

    # Decide before touching anything, so the downgrade is all-or-nothing
    # rather than "dropped two of three and then raised".
    foreign = [
        name
        for name in COLUMN_NAMES
        if states[name] is not None and states[name][3] != MARKER
    ]
    if foreign:
        raise RuntimeError(
            f"{TABLE}.{', '.join(foreign)} do not carry 016's comment marker "
            f"({MARKER!r}), so 016 did not create them and will not drop them. "
            "Nothing has been changed. Remove them by hand if you mean to."
        )

    for name in COLUMN_NAMES:
        if states[name] is not None:
            op.drop_column(TABLE, name)
