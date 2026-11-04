"""OIDC account links on `users` (#416).

Native OIDC login (#415) resolved a verified ID token to a local user through
an environment map, `OIDC_SUBJECT_USER_IDS`. This revision moves the link into
the database, where an administrator manages it from the panel. Six nullable
columns on `users`, in two triples:

| column | type | meaning |
| --- | --- | --- |
| `oidc_issuer` | `VARCHAR(2048)` | issuer the subject is bound under (exact `iss`) |
| `oidc_subject` | `VARCHAR(255)` | the bound `sub` |
| `oidc_linked_at` | `TIMESTAMPTZ` | when the binding was made |
| `oidc_expected_issuer` | `VARCHAR(2048)` | issuer a pending expectation was set under |
| `oidc_expected_email` | `VARCHAR(254)` | normalized expected email |
| `oidc_expected_set_at` | `TIMESTAMPTZ` | when the admin set it (drives the 7-day expiry) |

and five named constraints:

- `uq_users_oidc_identity UNIQUE (oidc_issuer, oidc_subject)`: one identity
  maps to at most one user. NULLs are distinct, so unlinked rows never collide.
- `uq_users_oidc_expected UNIQUE (oidc_expected_issuer, oidc_expected_email)`:
  two users cannot wait for the same address, so a match is never ambiguous.
- `ck_users_oidc_bound_complete` / `ck_users_oidc_expected_complete`: each
  triple is all NULL or all set.
- `ck_users_oidc_not_both`: a row is never bound and pending at once.

## Why columns and no foreign key

One identity per user is the whole requirement, and a side table would need a
foreign key to `users`; an FK to `users` reopened the permanent-deletion
deadlock for the change-feed tables. On the row, a user delete removes the
link with no extra purge step, and the user-row lock every admin action
already takes covers the link too. The btree row bound (2,048 + 255 characters
plus overhead) stays under PostgreSQL's 2,704-byte index row limit for the
ASCII issuers and subjects the settings admit.

## No backfill

#415 kept its mapping in the environment, so there is nothing to migrate.
Every existing row reads NULL in all six columns, which means "not linked".

## Provenance: marker-owned, reconcile or refuse

The schema gate stamps back and re-runs this body against a database that
already carries its objects. Each of the six columns carries `COLUMN_MARKER`
(mirrored byte-identically as `_OIDC_LINK_COLUMN_MARKER` in
`src/models/db.py`, so `alembic check` compares it), and each of the five
constraints carries `CONSTRAINT_MARKER` (autogenerate does not compare
constraint comments; the gate reads it from the catalogue).

**All eleven objects are checked before anything is touched.** A same-named
column is adopted only with the exact type, nullable, no default and the
column marker. A same-named constraint is adopted only with the canonical
`pg_get_constraintdef` (measured off a scratch TEMP table, 013's device, so it
is the server's own normalisation), `convalidated` and the constraint marker.
Anything else — the right shape without the marker included — is refused by
name, and nothing changes: an unmarked object of the right shape has unknown
provenance, and adopting it would let `downgrade()` drop something 037 did not
create.

## Downgrade loses every link

`downgrade()` decides each object on its own marker, constraints first, and
prints a skip for an unmarked one (023's per-object rule). A marked column
that a surviving (unmarked) constraint still references is skipped too, since
dropping it would silently drop that constraint. **Dropping the columns loses
every binding and every pending expectation**; a re-upgrade leaves every user
unlinked, and an administrator re-links each one. The previous build neither
reads nor writes these columns.

`search_path` is pinned to `public` and asserted; `lock_timeout` /
`statement_timeout` are set; all three are `RESET`, because alembic runs every
pending revision in one transaction. Nothing here needs superuser: the
database-owning role owns `users`, and the scratch table needs only `TEMP`.

Revision ID: 037
Revises: 036
Create Date: 2026-11-04
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "037"
down_revision: Union[str, None] = "036"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TABLE = "users"
QUALIFIED = "public.users"

#: Byte identical to `_OIDC_LINK_COLUMN_MARKER` in src/models/db.py.
COLUMN_MARKER = "OIDC account link (037_oidc_account_links)"
#: On each of the five constraints. Not compared by `alembic check`.
CONSTRAINT_MARKER = "OIDC account link constraint (037_oidc_account_links)"

# (column, format_type as PostgreSQL prints it, SQLAlchemy type), in creation
# order.
COLUMNS = (
    ("oidc_issuer", "character varying(2048)", sa.String(2048)),
    ("oidc_subject", "character varying(255)", sa.String(255)),
    ("oidc_linked_at", "timestamp with time zone", sa.DateTime(timezone=True)),
    ("oidc_expected_issuer", "character varying(2048)", sa.String(2048)),
    ("oidc_expected_email", "character varying(254)", sa.String(254)),
    ("oidc_expected_set_at", "timestamp with time zone", sa.DateTime(timezone=True)),
)
COLUMN_NAMES = tuple(name for name, _f, _t in COLUMNS)

# Byte identical to the `_OIDC_*_PREDICATE` constants in src/models/db.py.
BOUND_COMPLETE_PREDICATE = (
    "(oidc_issuer IS NULL AND oidc_subject IS NULL AND oidc_linked_at IS NULL) OR "
    "(oidc_issuer IS NOT NULL AND oidc_subject IS NOT NULL "
    "AND oidc_linked_at IS NOT NULL)"
)
EXPECTED_COMPLETE_PREDICATE = (
    "(oidc_expected_issuer IS NULL AND oidc_expected_email IS NULL "
    "AND oidc_expected_set_at IS NULL) OR "
    "(oidc_expected_issuer IS NOT NULL AND oidc_expected_email IS NOT NULL "
    "AND oidc_expected_set_at IS NOT NULL)"
)
NOT_BOTH_PREDICATE = "oidc_subject IS NULL OR oidc_expected_email IS NULL"

# (name, contype, the body after `ADD CONSTRAINT <name>`), in creation order.
CONSTRAINTS = (
    ("uq_users_oidc_identity", "u", "UNIQUE (oidc_issuer, oidc_subject)"),
    (
        "uq_users_oidc_expected",
        "u",
        "UNIQUE (oidc_expected_issuer, oidc_expected_email)",
    ),
    ("ck_users_oidc_bound_complete", "c", f"CHECK ({BOUND_COMPLETE_PREDICATE})"),
    (
        "ck_users_oidc_expected_complete",
        "c",
        f"CHECK ({EXPECTED_COMPLETE_PREDICATE})",
    ),
    ("ck_users_oidc_not_both", "c", f"CHECK ({NOT_BOTH_PREDICATE})"),
)
CONSTRAINT_NAMES = tuple(name for name, _k, _b in CONSTRAINTS)

_KIND = {"u": "UNIQUE", "c": "CHECK"}


def _quote(value: str) -> str:
    """A single-quoted SQL string literal (`COMMENT ON` takes no bind)."""
    return "'" + value.replace("'", "''") + "'"


# --------------------------------------------------------------------------
# catalogue reads
# --------------------------------------------------------------------------


def _oid(bind, name: str):
    """The OID `name` resolves to, or None. `to_regclass` never raises."""
    return bind.execute(
        sa.text("SELECT CAST(to_regclass(:name) AS oid)"), {"name": name}
    ).scalar()


def _column_state(bind, column: str):
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
        {"table": QUALIFIED, "column": column},
    ).first()


def _constraint_state(bind, name: str):
    """`(contype, definition, convalidated, comment, column names)` of the
    constraint `name` on `public.users`, or None if absent."""
    return bind.execute(
        sa.text(
            "SELECT c.contype::text AS contype, "
            "       pg_get_constraintdef(c.oid) AS definition, "
            "       c.convalidated, "
            "       obj_description(c.oid, 'pg_constraint') AS comment, "
            "       ARRAY(SELECT a.attname::text FROM pg_attribute a "
            "             WHERE a.attrelid = c.conrelid "
            "               AND a.attnum = ANY (c.conkey)) AS columns "
            "FROM pg_constraint c "
            "WHERE c.conrelid = CAST(:table AS regclass) AND c.conname = :name"
        ),
        {"table": QUALIFIED, "name": name},
    ).first()


def _foreign_relation(bind, name: str) -> bool:
    """A relation named `name` in `public` that is not the index backing our
    own same-named UNIQUE. `ADD CONSTRAINT … UNIQUE` creates an index of the
    constraint's name, so a squatting index or table would make it fail."""
    return bool(
        bind.execute(
            sa.text(
                "SELECT count(*) FROM pg_class r "
                "JOIN pg_namespace n ON n.oid = r.relnamespace "
                "WHERE n.nspname = 'public' AND r.relname = :name "
                "  AND NOT EXISTS (SELECT 1 FROM pg_constraint c "
                "                  WHERE c.conindid = r.oid "
                "                    AND c.conrelid = CAST(:table AS regclass) "
                "                    AND c.conname = :name)"
            ),
            {"table": QUALIFIED, "name": name},
        ).scalar()
    )


def _canonical_definitions(bind) -> dict[str, str]:
    """What this server renders each of the five constraints as, measured off
    a scratch TEMP table carrying the identical declaration, so the comparison
    cannot drift with a PostgreSQL major's normalisation."""
    scratch = "_omcp_037_constraint_probe"
    bind.execute(sa.text(f"DROP TABLE IF EXISTS pg_temp.{scratch}"))
    columns = ", ".join(f"{name} {fmt}" for name, fmt, _t in COLUMNS)
    constraints = ", ".join(
        f"CONSTRAINT {name}_probe {body}" for name, _k, body in CONSTRAINTS
    )
    bind.execute(sa.text(f"CREATE TEMP TABLE {scratch} ({columns}, {constraints})"))
    rows = bind.execute(
        sa.text(
            "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = CAST(:scratch AS regclass)"
        ),
        {"scratch": f"pg_temp.{scratch}"},
    ).fetchall()
    bind.execute(sa.text(f"DROP TABLE IF EXISTS pg_temp.{scratch}"))
    rendered = {name.removesuffix("_probe"): definition for name, definition in rows}
    missing = set(CONSTRAINT_NAMES) - set(rendered)
    if missing:  # pragma: no cover - would be a bug in this file
        raise RuntimeError(f"037 could not measure {sorted(missing)}")
    return rendered


# --------------------------------------------------------------------------
# search_path and timeouts
# --------------------------------------------------------------------------


def _pin_and_assert(bind) -> None:
    """Pin `search_path`, bound the locks, prove the unqualified name is ours.

    `op.add_column`, `COMMENT ON` and the constraint DDL are unqualified and
    resolve through `search_path`; link columns added to a decoy `users`
    would leave the callback reading one table and the panel writing another.
    """
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.execute("SET LOCAL statement_timeout = '60s'")
    op.execute("SET LOCAL search_path TO public")
    qualified = _oid(bind, QUALIFIED)
    unqualified = _oid(bind, TABLE)
    if qualified is None or qualified != unqualified:
        raise RuntimeError(
            f"037's {TABLE} is not the table {QUALIFIED} resolves to "
            f"(search_path-relative oid {unqualified!r}, qualified oid "
            f"{qualified!r}). Set the migration role's search_path so `public` "
            "comes first, then re-run. Nothing has been changed."
        )


def _reset() -> None:
    op.execute("RESET lock_timeout")
    op.execute("RESET statement_timeout")
    op.execute("RESET search_path")


# --------------------------------------------------------------------------
# upgrade
# --------------------------------------------------------------------------


def _column_problems(bind) -> tuple[list[str], list[tuple]]:
    problems: list[str] = []
    missing: list[tuple] = []
    for name, fmt, satype in COLUMNS:
        state = _column_state(bind, name)
        if state is None:
            missing.append((name, satype))
            continue
        coltype, notnull, default, comment = state
        found = []
        if coltype != fmt:
            found.append(f"it is {coltype}, not {fmt}")
        if notnull:
            found.append("it is NOT NULL; 037 creates it nullable (NULL is 'not linked')")
        if default is not None:
            found.append(f"it has a server default of {default!r}; 037 creates none")
        if comment != COLUMN_MARKER:
            found.append("it does not carry 037's column marker")
        if found:
            problems.append(f"{TABLE}.{name} already exists but {'; '.join(found)}")
    return problems, missing


def _constraint_problems(bind, canonical: dict[str, str]) -> tuple[list[str], list[tuple]]:
    problems: list[str] = []
    missing: list[tuple] = []
    for name, kind, body in CONSTRAINTS:
        state = _constraint_state(bind, name)
        if state is None:
            if _foreign_relation(bind, name):
                problems.append(
                    f"a relation named {name} already exists in public and is not "
                    f"037's {_KIND[kind]} constraint"
                )
            missing.append((name, body))
            continue
        contype, definition, validated, comment, _cols = state
        found = []
        if contype != kind:
            found.append(f"it is not a {_KIND[kind]} constraint (contype {contype!r})")
        # `pg_get_constraintdef` appends " NOT VALID" to an unvalidated
        # constraint; validity is judged on its own line below, so the
        # definition is compared without it.
        elif definition.removesuffix(" NOT VALID") != canonical[name]:
            found.append(f"its definition is {definition!r}, not {canonical[name]!r}")
        if not validated:
            found.append("it is NOT VALID, so existing rows were never checked")
        if comment != CONSTRAINT_MARKER:
            found.append("it does not carry 037's constraint marker")
        if found:
            problems.append(f"constraint {name} on {TABLE} already exists but {'; '.join(found)}")
    return problems, missing


def upgrade() -> None:
    bind = op.get_bind()
    _pin_and_assert(bind)

    canonical = _canonical_definitions(bind)
    column_problems, missing_columns = _column_problems(bind)
    constraint_problems, missing_constraints = _constraint_problems(bind, canonical)
    problems = column_problems + constraint_problems
    if problems:
        raise RuntimeError(
            f"037 will not adopt OIDC link objects of unknown provenance: "
            f"{'. '.join(problems)}. The OIDC callback signs a person in as the "
            "user these columns name, so a shape this migration did not create "
            "is a login it cannot vouch for, and a downgrade could drop "
            "something 037 never made. Resolve by hand (drop the object and let "
            "037 create it, or make it match), then re-run. Nothing has been "
            "changed."
        )

    for name, satype in missing_columns:
        op.add_column(TABLE, sa.Column(name, satype, nullable=True))
        op.execute(f"COMMENT ON COLUMN {TABLE}.{name} IS {_quote(COLUMN_MARKER)}")
    for name, body in missing_constraints:
        op.execute(f"ALTER TABLE {TABLE} ADD CONSTRAINT {name} {body}")
        op.execute(
            f"COMMENT ON CONSTRAINT {name} ON {TABLE} IS {_quote(CONSTRAINT_MARKER)}"
        )

    _reset()


# --------------------------------------------------------------------------
# downgrade
# --------------------------------------------------------------------------


def _skip(what: str, why: str) -> None:
    """023's rule: a per-object skip, printed to stdout because
    `alembic/env.py` configures no logging."""
    print(
        f"037 downgrade: leaving {what} in place. {why} Remove it by hand if "
        "you mean to.",
        flush=True,
    )


def downgrade() -> None:
    """Drop the five constraints and six columns that carry 037's markers.

    **This loses every OIDC link and every pending expectation.** Each object
    is decided on its own marker, constraints first; an unmarked one is left
    in place with a printed line, and so is a marked column a surviving
    constraint still references (dropping it would drop that constraint
    implicitly).
    """
    bind = op.get_bind()
    _pin_and_assert(bind)

    surviving_columns: set[str] = set()
    for name in CONSTRAINT_NAMES:
        state = _constraint_state(bind, name)
        if state is None:
            continue
        _contype, _definition, _validated, comment, columns = state
        if comment != CONSTRAINT_MARKER:
            _skip(
                f"constraint {name} on {TABLE}",
                f"It does not carry 037's constraint marker ({CONSTRAINT_MARKER!r}) "
                f"— its comment is {comment!r} — so 037 did not create it and "
                "will not drop it.",
            )
            surviving_columns.update(columns or ())
            continue
        op.execute(f"ALTER TABLE {TABLE} DROP CONSTRAINT {name}")

    for name in COLUMN_NAMES:
        state = _column_state(bind, name)
        if state is None:
            continue
        if state[3] != COLUMN_MARKER:
            _skip(
                f"{TABLE}.{name}",
                f"It does not carry 037's column marker ({COLUMN_MARKER!r}) — its "
                f"comment is {state[3]!r} — so 037 did not create it and will "
                "not drop it.",
            )
            continue
        if name in surviving_columns:
            _skip(
                f"{TABLE}.{name}",
                "A constraint 037 did not create still references it, and "
                "dropping the column would drop that constraint too.",
            )
            continue
        op.drop_column(TABLE, name)

    _reset()
