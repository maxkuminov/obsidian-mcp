"""The panel session registry: one revocable row per browser session (#198).

`logout()` used to call `request.session.clear()` and nothing else. Starlette
answers that with an expiring `Set-Cookie` — but the cookie already issued
stays a valid, correctly-signed credential until its itsdangerous timestamp
passes `session_max_age`, which is seven days. The verification on #198
replayed a pre-logout cookie against the container's installed Starlette and
got the user back after they had signed out. A signed cookie cannot be
un-signed; the only thing that can be revoked is a row, so this revision
creates the row.

## Shape, and why the primary key is a hash

`user_sessions (id varchar(64) PK, user_id integer NOT NULL REFERENCES
users(id) ON DELETE CASCADE, created_at timestamptz NOT NULL DEFAULT now(),
last_seen_at timestamptz NOT NULL, expires_at timestamptz NOT NULL, revoked_at
timestamptz NULL, user_agent_hash varchar(64) NULL)`.

`id` is `sha256(sid).hexdigest()`, where `sid` is the `secrets.token_urlsafe(32)`
identifier the signed cookie carries. **The identifier itself is never
stored.** It is a bearer credential for seven days, and a `pg_dump` of this
database is taken before every migration and retained for thirty days — the
invariant `docs/architecture/schema-and-migrations.md` records is that a dump
holds password, API-key, OAuth and transfer-token *hashes* and deliberately no
plaintext credential. Storing `sid` verbatim would make every retained dump a
file full of live panel sessions, which is the invariant inverted. The digest
is unkeyed on purpose: 256 bits of CSPRNG output has nothing to brute-force,
and an HMAC under `SECRET_KEY` would make the whole table unreadable after a
key rotation an operator may need to perform.

**The `ON DELETE CASCADE` is load-bearing.** A permanent user delete removes
that user's sessions with no handler code at all, and `User.sessions` declares
`passive_deletes=True` so the database cascade is what actually fires rather
than a per-row ORM delete that would leave the schema's cascade untested. It is
verified here through `pg_constraint.confdeltype` and never by constraint
name — a same-named FK pointing at another table, or one that deletes with
`SET NULL` against a NOT NULL column, satisfies every name-level lookup while
being a different constraint.

Both indexes exist for a query that runs constantly: `ix_user_sessions_user_id`
for `WHERE user_id = :u AND revoked_at IS NULL` (every revocation), and
`ix_user_sessions_expires_at` for the purge's `WHERE expires_at < cutoff`.
They are verified through `pg_index` as column lists plus uniqueness, validity
and whether they are partial or over an expression — a name recreated on
another column keeps the name an existence check looks for while the scan has
nothing to lean on (019's and 021's rule).

## Why nothing is backfilled

**024 writes no row on any path.** There is nothing to backfill *from*: a
session that predates this table has no identifier the registry could resolve,
and inventing rows for the cookies currently in flight would grandfather
exactly the credentials this change exists to invalidate. The deploy therefore
signs every live panel session out once, which is the accepted cost — two users
and two logins, against keeping the #198 replay window open for a further seven
days after the fix ships.

Because nothing is written, a stamp-back re-run (the schema gate does
`alembic stamp 023` then `upgrade head`) reconciles the existing table and
writes and deletes nothing, so a gate exercise cannot sign a live session out.

## Reconciliation, and why it is not a bare CREATE TABLE

The gate re-runs this body against a database that already carries the table,
so a bare `CREATE TABLE` raises there and `IF NOT EXISTS` is worse: it would
adopt *any* table of that name — one whose `user_id` FK deletes with
`SET NULL`, one with no expiry index, one whose `expires_at` is nullable — and
the session validator would then be authorizing browsers against a schema
nothing verified. 013's rule applies: reconcile a database that demonstrably
has our shape, refuse to guess for one that does not, and **name what
disagreed** so an operator can fix it by hand.

The marker is a `COMMENT ON TABLE` stamped in the same transaction as the
create, mirrored in `src/models/db.py` as `_USER_SESSIONS_TABLE_MARKER` so
`alembic check` compares it like any other attribute; `downgrade()` drops the
table only if it carries that marker.

The **primary key and the `created_at` server default** are read for a reason
`alembic check` makes necessary: autogenerate compares neither. A table whose
PK has been dropped reports as being in perfect agreement with the model while
two rows could then claim one session-identifier hash — and validation reads
exactly one row per hash. A `created_at` default that is not the current time
is quieter still: every column, type, constraint and index stays exactly as 024
made them while the age of every session this table records is wrong.

## search_path

`op.create_table`, `op.create_index`, `COMMENT ON`, `op.drop_index` and
`op.drop_table` are all unqualified and resolve through `search_path`, so on a
database or role whose path does not start with `public` this would create the
registry somewhere the application never looks — and the session validator
would then find no row for any cookie, locking every user out of the panel.
That is 021's lesson and 023's repetition of it: 021 and 023 both `RESET` the
path at the end of their own `upgrade()`, so a later revision in the same
transaction inherits nothing and **024 needs its own pin**. `upgrade()` then
asserts that what it created or adopted really is what the qualified name
resolves to. Pinning rather than passing `schema="public"` to each `op.*` call
is deliberate, for 021's reason: a schema-qualified table in alembic's eyes
does not match a model that declares no schema, and `alembic check` would
report drift for ever after.

## Locks

One `CREATE TABLE` with a foreign key, which takes a brief `ACCESS SHARE` (in
fact `SHARE ROW EXCLUSIVE`) on `users` to validate the reference and blocks
nothing already in use, and two `CREATE INDEX` on the table it just made.
`lock_timeout` / `statement_timeout` make a blocked migration fail fast instead
of stalling the deploy, and both are `RESET` at the end because alembic runs
every pending revision in one transaction and `SET LOCAL` would otherwise leak
into a later revision (013 through 023 do the same).

Revision ID: 024
Revises: 023
Create Date: 2026-09-05
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "024"
# The sibling `index-integrity-hardening` migration. 024 must not be merged or
# migrated ahead of it; the `schema-integrity` spec delta names the ordering.
down_revision: Union[str, None] = "023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TABLE = "user_sessions"

# **Every catalog lookup below resolves this**, not the bare name, for 021's
# reason: an unqualified reference resolves through `search_path`, so a role
# pointing elsewhere would have the application and this migration each
# addressing a different table of that name.
QUALIFIED = "public.user_sessions"

USER_INDEX = "ix_user_sessions_user_id"
EXPIRES_INDEX = "ix_user_sessions_expires_at"

# 013's device, and 015's through 023's: the migration marks what it created,
# so `downgrade()` can tell its own work from somebody else's and drop only the
# former. Must stay byte identical to `_USER_SESSIONS_TABLE_MARKER` in
# `src/models/db.py`, where it is declared as the table comment so
# `alembic check` compares it like any other attribute.
MARKER = "one row per panel browser session (024_user_sessions)"

# `(column, format_type, attnotnull)` — the whole shape, in creation order.
EXPECTED_COLUMNS = (
    ("id", "character varying(64)", True),
    ("user_id", "integer", True),
    ("created_at", "timestamp with time zone", True),
    ("last_seen_at", "timestamp with time zone", True),
    ("expires_at", "timestamp with time zone", True),
    ("revoked_at", "timestamp with time zone", False),
    ("user_agent_hash", "character varying(64)", False),
)

#: `(columns, unique, usable, restricted)` for each index `_create` makes.
EXPECTED_INDEXES = {
    USER_INDEX: (["user_id"], False, True, False),
    EXPIRES_INDEX: (["expires_at"], False, True, False),
}


def _quote(value: str) -> str:
    """A single-quoted SQL string literal. `MARKER` is a module constant with
    no quotes in it; the doubling is here so it stays correct if that
    changes."""
    return "'" + value.replace("'", "''") + "'"


# --------------------------------------------------------------------------
# catalogue reads
# --------------------------------------------------------------------------


def _oid(bind, name: str):
    """The OID `name` resolves to, or None. `to_regclass` never raises."""
    return bind.execute(
        sa.text("SELECT CAST(to_regclass(:name) AS oid)"), {"name": name}
    ).scalar()


def _table_exists(bind) -> bool:
    return _oid(bind, QUALIFIED) is not None


def _table_comment(bind) -> str | None:
    return bind.execute(
        sa.text("SELECT obj_description(CAST(:qualified AS regclass), 'pg_class')"),
        {"qualified": QUALIFIED},
    ).scalar()


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
            {"table": QUALIFIED},
        ).fetchall()
    ]


def _column_default(bind, column: str) -> str | None:
    """The rendered server default for `column`, or None when it has none."""
    return bind.execute(
        sa.text(
            "SELECT pg_get_expr(d.adbin, d.adrelid) "
            "FROM pg_attribute a "
            "LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
            "WHERE a.attrelid = CAST(:table AS regclass) AND a.attname = :column "
            "  AND a.attnum > 0 AND NOT a.attisdropped"
        ),
        {"table": QUALIFIED, "column": column},
    ).scalar()


def _primary_key_columns(bind):
    """The PK's columns **in order**, or None when there is no PK.

    Not tidiness, and `alembic check` does not compare primary keys at all: a
    table of the right columns with no PK reports as being in perfect agreement
    with the model while two rows could claim the same session-identifier hash.
    Validation reads exactly one row per hash and would then be authorizing
    against an arbitrary one of them.
    """
    row = bind.execute(
        sa.text(
            "SELECT (SELECT array_agg(a.attname ORDER BY k.ord) "
            "          FROM unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) "
            "          JOIN pg_attribute a ON a.attrelid = c.conrelid "
            "                             AND a.attnum = k.attnum) AS columns "
            "FROM pg_constraint c "
            "WHERE c.conrelid = CAST(:table AS regclass) AND c.contype = 'p'"
        ),
        {"table": QUALIFIED},
    ).first()
    return list(row.columns) if row is not None and row.columns else None


def _regclass_text(bind, name: str) -> str:
    """How this server renders `name` as a `regclass`, so both sides of the
    foreign-key comparison are rendered by the same code rather than one of
    them being a guess at whether `public.` is included."""
    return bind.execute(
        sa.text("SELECT CAST(CAST(:name AS regclass) AS text)"), {"name": name}
    ).scalar()


def _foreign_keys(bind):
    """Every FK on the table, **completely** described (019's reader).

    The delete action alone is not the constraint. A table carrying 024's
    marker, 024's columns and 024's indexes whose `user_id` FK points at
    `api_keys(id)` passes a delete-action check unchanged — and the registry
    would then be binding sessions to rows in another table. So the referenced
    table, the referenced column, the local column, both referential actions
    and the validated state are all read, **resolved through `confrelid` and
    `confdeltype` rather than by constraint name**, and the count is checked
    too: an *extra* constraint is drift as much as a wrong one.

    `NOT VALID` matters for its own reason: such an FK enforces new rows having
    never checked the existing ones, so the table may already hold a session
    bound to a user that does not exist.
    """
    return bind.execute(
        sa.text(
            "SELECT c.conname, "
            "       CAST(c.confrelid AS regclass)::text AS referenced_table, "
            "       c.confdeltype::text AS delete_action, "
            "       c.confupdtype::text AS update_action, "
            "       c.convalidated, "
            "       (SELECT array_agg(a.attname ORDER BY k.ord) "
            "          FROM unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) "
            "          JOIN pg_attribute a ON a.attrelid = c.conrelid "
            "                             AND a.attnum = k.attnum) AS local_columns, "
            "       (SELECT array_agg(a.attname ORDER BY k.ord) "
            "          FROM unnest(c.confkey) WITH ORDINALITY AS k(attnum, ord) "
            "          JOIN pg_attribute a ON a.attrelid = c.confrelid "
            "                             AND a.attnum = k.attnum) AS referenced_columns "
            "FROM pg_constraint c "
            "WHERE c.conrelid = CAST(:table AS regclass) AND c.contype = 'f' "
            "ORDER BY c.conname"
        ),
        {"table": QUALIFIED},
    ).fetchall()


def _index_definitions(bind) -> dict:
    """`{name: (columns, unique, usable, restricted)}` — 019's/021's reader.

    Names are not definitions. `ix_user_sessions_expires_at` dropped and
    recreated on `revoked_at` keeps the name an existence check looks for while
    the purge's range scan has nothing to lean on.

    `indkey` is an `int2vector`, which has no direct array cast; going through
    its text rendering is the portable idiom. An expression index has `attnum`
    0 there and joins to nothing, which is why `restricted` is read separately
    rather than inferred from a short column list. A **partial** index is read
    for a reason specific to this table: `WHERE revoked_at IS NULL` looks like
    an optimisation and would silently exclude exactly the rows the purge
    exists to remove.
    """
    rows = bind.execute(
        sa.text(
            "SELECT ic.relname AS name, "
            "       (SELECT array_agg(a.attname ORDER BY k.ord) "
            "          FROM unnest(string_to_array(CAST(i.indkey AS text), ' ')) "
            "               WITH ORDINALITY AS k(attnum, ord) "
            "          JOIN pg_attribute a ON a.attrelid = i.indrelid "
            "                             AND a.attnum = CAST(k.attnum AS smallint)"
            "       ) AS columns, "
            "       i.indisunique, "
            "       (i.indisvalid AND i.indisready) AS usable, "
            "       (i.indpred IS NOT NULL OR i.indexprs IS NOT NULL) AS restricted "
            "FROM pg_index i JOIN pg_class ic ON ic.oid = i.indexrelid "
            "WHERE i.indrelid = CAST(:table AS regclass)"
        ),
        {"table": QUALIFIED},
    ).fetchall()
    return {
        row.name: (
            list(row.columns or []),
            row.indisunique,
            row.usable,
            row.restricted,
        )
        for row in rows
    }


def _pk_index_name(bind) -> str | None:
    """The index PostgreSQL built to back the primary key.

    Read rather than assumed: `op.create_table` names it `<table>_pkey` today,
    but the point of the check below is that the index set is *complete*, and a
    hard-coded name would make a renamed PK index read as an unexpected one.
    """
    return bind.execute(
        sa.text(
            "SELECT ic.relname FROM pg_constraint c "
            "JOIN pg_class ic ON ic.oid = c.conindid "
            "WHERE c.conrelid = CAST(:table AS regclass) AND c.contype = 'p'"
        ),
        {"table": QUALIFIED},
    ).scalar()


def _extra_constraints(bind):
    """Every UNIQUE, CHECK and EXCLUSION constraint on the table.

    024 creates none of these, so **any** of them is somebody else's. They are
    enumerated rather than ignored because the damaging one is silent: a
    `UNIQUE (user_id)` added by hand leaves the columns, the primary key, the
    foreign key and both named indexes exactly as 024 made them, so every other
    check here passes — and then the second session a user opens, or the
    re-issue that follows their own password change, fails on a constraint
    violation the application has no branch for. A CHECK is the same shape of
    trap one layer down (`revoked_at IS NULL`, say, would make revocation
    itself raise), and an EXCLUSION constraint is a UNIQUE with more reach.

    `conname` and the rendered definition are both returned: an operator has to
    be able to find the thing, and `pg_get_constraintdef` is what names it.
    """
    return bind.execute(
        sa.text(
            # `contype` is PostgreSQL's `"char"`, which the driver hands back
            # as a one-byte `bytes`. Cast in SQL rather than decoding in
            # Python, so the value that reaches the lookup below is a `str`
            # whatever driver is underneath.
            "SELECT c.conname, CAST(c.contype AS text) AS contype, "
            "       pg_get_constraintdef(c.oid) AS definition "
            "FROM pg_constraint c "
            "WHERE c.conrelid = CAST(:table AS regclass) "
            "  AND c.contype IN ('u', 'c', 'x') "
            "ORDER BY c.conname"
        ),
        {"table": QUALIFIED},
    ).fetchall()


def _canonical_created_at_default(bind) -> str:
    """What this server renders `now()` as for a `timestamptz` column.

    013's scratch-`TEMP`-table device, as 021 applies it to a default: a
    hand-written expected string pins the migration to one PostgreSQL major's
    rendering, while an empty temp table carrying the identical declaration
    cannot drift. Needs the TEMP privilege on the database, as 013 already
    requires.
    """
    scratch = "_omcp_024_default_probe"
    bind.execute(sa.text(f"DROP TABLE IF EXISTS pg_temp.{scratch}"))
    bind.execute(
        sa.text(
            f"CREATE TEMP TABLE {scratch} "
            "(created_at timestamptz NOT NULL DEFAULT now())"
        )
    )
    rendered = bind.execute(
        sa.text(
            "SELECT pg_get_expr(d.adbin, d.adrelid) "
            "FROM pg_attribute a "
            "JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
            "WHERE a.attrelid = CAST(:scratch AS regclass) AND a.attname = 'created_at'"
        ),
        {"scratch": f"pg_temp.{scratch}"},
    ).scalar()
    bind.execute(sa.text(f"DROP TABLE IF EXISTS pg_temp.{scratch}"))
    return rendered


# --------------------------------------------------------------------------
# create / verify
# --------------------------------------------------------------------------


def _pin_search_path() -> None:
    """Pin `search_path` to `public` for the rest of this transaction.

    021's device, and 023's repetition of it. Every `op.*` call below is
    unqualified and resolves through `search_path`; giving each one
    `schema="public"` would make the table a *schema-qualified* object in
    alembic's eyes while the ORM model declares no schema, so autogenerate
    would see the two disagree and `alembic check` would never be clean again.
    Pinning the path instead makes the unqualified names resolve to `public`
    while leaving both sides schema-less.

    `SET LOCAL` is transaction-scoped, which is exactly how this runs —
    `alembic/env.py` executes every pending revision in one transaction — and
    is why `upgrade()` and `downgrade()` `RESET` it at the end rather than
    leaving it for a later revision to inherit. **021 and 023 both `RESET`
    their own, which is precisely why 024 cannot rely on either pin still being
    in force.**
    """
    op.execute("SET LOCAL search_path TO public")


def _assert_is_the_qualified_table(bind) -> None:
    """What the unqualified name resolves to is `public.user_sessions`.

    Belt and braces behind the pin, in 021's and 023's shape: if the pin ever
    fails to take effect this fails closed, rather than creating the registry
    in a schema the application never reads — a state whose symptom is that no
    cookie resolves to a row and every user is locked out of the panel.
    """
    qualified = _oid(bind, QUALIFIED)
    unqualified = _oid(bind, TABLE)
    if qualified is None or qualified != unqualified:
        raise RuntimeError(
            f"024's {TABLE} is not the table {QUALIFIED} resolves to "
            f"(search_path-relative oid {unqualified!r}, qualified oid "
            f"{qualified!r}). The session validator reads the unqualified "
            "name, so a table elsewhere on the path would leave every panel "
            "cookie resolving to nothing. Set the migration role's search_path "
            "so `public` comes first, then re-run."
        )


def _create() -> None:
    op.create_table(
        TABLE,
        # `sha256(sid).hexdigest()` — never the identifier the cookie carries.
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("user_agent_hash", sa.String(length=64), nullable=True),
        # The cascade is the whole mechanism of "a permanent user delete
        # removes their sessions"; `User.sessions` declares
        # `passive_deletes=True` so this is what fires.
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )
    # Every revocation is `WHERE user_id = :u AND revoked_at IS NULL`; the
    # purge is a range scan over `expires_at`.
    op.create_index(USER_INDEX, TABLE, ["user_id"])
    op.create_index(EXPIRES_INDEX, TABLE, ["expires_at"])
    # Stamped in the same transaction as the CREATE, so the marker and the
    # table can never disagree about who made it. `COMMENT ON` is utility DDL
    # and takes no bind parameter, so the literal is quoted.
    op.execute(f"COMMENT ON TABLE {TABLE} IS {_quote(MARKER)}")


def _primary_key_problems(bind) -> list:
    pk = _primary_key_columns(bind)
    if pk == ["id"]:
        return []
    if pk is None:
        return [
            "it has no primary key at all, so two rows could claim one session "
            "identifier hash and validation reads exactly one row per hash"
        ]
    return [f"its primary key is {pk}, not ['id']"]


def _default_problems(bind) -> list:
    """`created_at`'s server default, compared against a server-derived
    canonical.

    `alembic check` does not compare server defaults (`compare_server_default`
    is off by default), so this comparison is the only thing that sees the
    drift — and it is a quiet one: a default that is not the current time
    leaves every column, type, constraint and index exactly as 024 made them
    while the recorded age of every session is wrong.
    """
    live = _column_default(bind, "created_at")
    canonical = _canonical_created_at_default(bind)
    if live == canonical:
        return []
    return [
        f"its created_at default is {live!r}, not {canonical!r} — the recorded "
        "age of every session minted afterwards would be wrong"
    ]


def _foreign_key_problems(bind) -> list:
    """Exactly one FK, and it is `user_id -> users.id ON DELETE CASCADE`."""
    fks = _foreign_keys(bind)
    if len(fks) != 1:
        return [
            "it carries "
            + (
                "no foreign key at all, so a permanent user delete would leave "
                "live session rows bound to a user that no longer exists"
                if not fks
                else f"{len(fks)} foreign keys ({[f.conname for f in fks]}), not one"
            )
        ]

    fk = fks[0]
    expected_target = _regclass_text(bind, "users")
    problems = []
    if list(fk.local_columns or []) != ["user_id"]:
        problems.append(
            f"its foreign key is on {list(fk.local_columns or [])}, not ['user_id']"
        )
    if fk.referenced_table != expected_target:
        problems.append(
            f"its foreign key references {fk.referenced_table!r}, not "
            f"{expected_target!r} — the registry would bind browser sessions to "
            "another table's rows"
        )
    if list(fk.referenced_columns or []) != ["id"]:
        problems.append(
            "its foreign key references column(s) "
            f"{list(fk.referenced_columns or [])}, not ['id']"
        )
    if fk.delete_action != "c":
        problems.append(
            f"its user_id foreign key deletes with {fk.delete_action!r}, not "
            "CASCADE ('c') — the cascade is the entire mechanism by which a "
            "permanent user delete removes that user's sessions, and "
            "`passive_deletes=True` means no handler code does it instead"
        )
    if fk.update_action != "a":
        problems.append(
            f"its user_id foreign key updates with {fk.update_action!r}, not "
            "NO ACTION ('a')"
        )
    if not fk.convalidated:
        problems.append(
            "its user_id foreign key is NOT VALID, so existing rows were never "
            "checked and the table may already hold a session bound to a user "
            "that does not exist"
        )
    return problems


def _constraint_problems(bind) -> list:
    """024 creates no UNIQUE, CHECK or EXCLUSION constraint, so there are none.

    The complete set is required, not a subset: a check that only looks for
    what 024 *made* adopts a table that also carries something 024 did not,
    and the additions that matter here are the ones every other check passes.
    """
    problems = []
    for row in _extra_constraints(bind):
        kind = {"u": "UNIQUE", "c": "CHECK", "x": "EXCLUSION"}[row.contype]
        problems.append(
            f"it carries an unexpected {kind} constraint {row.conname!r} "
            f"({row.definition}) that 024 does not create"
        )
    return problems


def _index_problems(bind) -> list:
    """Each index present *and defined as 024 defines it*, **and no others**.

    The second half is the one that was missing. An extra index is not merely
    untidy here: a unique one changes what the table permits, and since a
    `UNIQUE` constraint is implemented as an index this is also the backstop
    for `_constraint_problems` — a bare `CREATE UNIQUE INDEX ... (user_id)`
    creates no constraint row at all and would otherwise be invisible to every
    check in this module while breaking the second session a user opens.
    """
    live = _index_definitions(bind)
    problems = []
    for name, expected in EXPECTED_INDEXES.items():
        actual = live.get(name)
        if actual is None:
            problems.append(f"it is missing index {name}")
            continue
        if actual != expected:
            columns, unique, usable, restricted = actual
            problems.append(
                f"its index {name} is on {columns} "
                f"(unique={unique}, usable={usable}, partial-or-expression="
                f"{restricted}), not {expected[0]} as a plain, valid, "
                "non-unique index"
            )

    permitted = set(EXPECTED_INDEXES)
    pk_index = _pk_index_name(bind)
    if pk_index is not None:
        permitted.add(pk_index)
    for name in sorted(set(live) - permitted):
        columns, unique, _usable, _restricted = live[name]
        problems.append(
            f"it carries an unexpected index {name!r} on {columns} "
            f"(unique={unique}) that 024 does not create"
        )
    return problems


def _verify(bind) -> None:
    """Accept a pre-existing table only if it is exactly 024's.

    **Complete, not minimal.** Every column, the primary key, the default, the
    foreign key, the constraint set and the index set are all checked, and the
    last two are checked as *sets* — a shape that has everything 024 makes
    **plus** something it does not is not 024's shape. That is not pedantry: a
    hand-added `UNIQUE (user_id)` passes a subset check unchanged and then
    breaks the second session a user opens and every post-password-change
    re-issue, on a constraint no handler expects.

    013's philosophy: reconcile a database that demonstrably has our shape,
    refuse to guess for one that does not — and **name what disagreed**, so an
    operator can repair the one thing rather than search for it. Nothing is
    patched: a table of this name somebody else created is not this migration's
    table, and adopting it would leave the session validator authorizing
    browsers against a schema nothing verified.
    """
    problems = []

    if _table_comment(bind) != MARKER:
        problems.append("it does not carry 024's comment marker")

    columns = _columns(bind)
    if columns != list(EXPECTED_COLUMNS):
        problems.append(f"its columns are {columns}, not {list(EXPECTED_COLUMNS)}")

    problems.extend(_primary_key_problems(bind))
    problems.extend(_default_problems(bind))
    problems.extend(_foreign_key_problems(bind))
    problems.extend(_constraint_problems(bind))
    problems.extend(_index_problems(bind))

    if problems:
        raise RuntimeError(
            f"{TABLE} already exists but {'; '.join(problems)}. 024 will not "
            "adopt a table of unknown provenance: every panel request resolves "
            "its browser session out of this table and every logout, password "
            "change and administrative reset revokes rows in it, so a shape "
            "this migration did not create is a shape nothing verified. "
            "Resolve by hand — drop it and let 024 create it, or make it match "
            "— then re-run. Nothing has been changed."
        )


# --------------------------------------------------------------------------
# upgrade / downgrade
# --------------------------------------------------------------------------


def upgrade() -> None:
    bind = op.get_bind()

    # Fail fast rather than queueing behind a long-lived transaction: the
    # deploy migrates before recreating the container, so a stalled migration
    # is a stalled deploy while the old container is still serving. Per
    # statement and per lock acquisition, not a budget for the transaction.
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.execute("SET LOCAL statement_timeout = '60s'")
    _pin_search_path()

    if _table_exists(bind):
        _verify(bind)
    else:
        _create()
    _assert_is_the_qualified_table(bind)

    # **No backfill and no data write on any path**, deliberately — see the
    # module docstring. A session that predates this table has no identifier
    # the registry could resolve, and inventing rows for the cookies currently
    # in flight would grandfather exactly the credentials this change exists to
    # invalidate. It is also what makes the gate's stamp-back re-run
    # non-destructive: the reconciliation path above writes and deletes
    # nothing, so live sessions survive it rather than being signed out by a
    # gate exercise.

    # `SET LOCAL` is scoped to the transaction and alembic runs every pending
    # revision in *one*, so without this the next revision would silently
    # inherit these settings and blame its own SQL when it tripped them. The
    # same applies to the `search_path` pin — 021 and 023 both `RESET` theirs,
    # which is exactly why this revision needed a pin of its own.
    op.execute("RESET lock_timeout")
    op.execute("RESET statement_timeout")
    op.execute("RESET search_path")


def downgrade() -> None:
    """Drop the table only if it carries 024's marker.

    013's rule: a downgrade must undo *this* migration, not delete a table
    somebody else put there under this name. The marker is the only evidence of
    authorship.

    Dropping the registry signs every panel session out — the previous build
    neither reads nor writes the table, so it is safe for it to be absent, and
    a re-upgrade creates it empty and the next login mints a fresh row. Nothing
    is lost that a login does not restore, and 024 is additive, so the previous
    image runs against the new table without a downgrade at all.
    """
    bind = op.get_bind()
    # `op.drop_index` / `op.drop_table` resolve through `search_path` too, so
    # the same pin decides *which* objects a downgrade would remove, and the
    # identity assertion stays as the belt-and-braces check that it took
    # effect before anything is dropped.
    _pin_search_path()
    if not _table_exists(bind):
        op.execute("RESET search_path")
        return
    _assert_is_the_qualified_table(bind)
    if _table_comment(bind) != MARKER:
        raise RuntimeError(
            f"{TABLE} does not carry 024's comment marker ({MARKER!r}), so 024 "
            "did not create it and will not drop it. Nothing has been changed. "
            "Remove it by hand if you mean to."
        )
    op.drop_index(EXPIRES_INDEX, table_name=TABLE)
    op.drop_index(USER_INDEX, table_name=TABLE)
    op.drop_table(TABLE)
    op.execute("RESET search_path")
