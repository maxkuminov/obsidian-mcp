"""`private_key_jwt` for Client ID Metadata Document clients (#420).

ChatGPT's connector presents a CIMD `client_id` whose document declares
`"token_endpoint_auth_method": "private_key_jwt"` with a same-origin
`jwks_uri`. The synthetic `oauth_clients` row (036) must therefore pin which
method the client authenticates with at `/token` and `/revoke`, and the keys it
authenticates against. Four nullable columns, all NULL on every row that is not
`private_key_jwt`:

| column | type | meaning |
| --- | --- | --- |
| `jwks_uri` | `VARCHAR(2048)` | the document's pinned, same-origin JWKS URL |
| `token_endpoint_auth_signing_alg` | `VARCHAR(16)` | the document's pinned alg, if it declared one |
| `jwks_snapshot` | `JSONB` | the validated, normalised `{"keys": [...]}` verification reads |
| `jwks_fetched_at` | `TIMESTAMPTZ` | the snapshot's **origin** fetch time (drives the 24 h stale refetch) |

## Why the existing method column is reused

`oauth_clients.token_endpoint_auth_method` (`VARCHAR(32) NOT NULL`, 010)
already exists and already reads `none` on every CIMD row, which is exactly
what an existing CIMD client is. A second "CIMD auth method" column would need
a backfill, would be a second answer to the question `_client_authenticated`
already asks of this one, and the two could disagree. Reusing it means no
backfill and no existing row changes meaning.

## Three CHECKs

- `ck_oauth_clients_auth_method_secret` (010, reconciled by 013) is
  **replaced**: its `none` and `client_secret_post` clauses are unchanged, and
  a third admits `private_key_jwt` with no secret hash.
- `ck_oauth_clients_cimd_shape` (036) is **replaced**: a metadata URL exists
  exactly when the id starts `cimd_`, and such a row has no secret and a method
  in (`none`, `private_key_jwt`).
- `ck_oauth_clients_private_key_jwt_shape` is **new**: `private_key_jwt`
  implies a CIMD row with `jwks_uri`, `jwks_snapshot` and `jwks_fetched_at`
  all set; any other method implies all four new columns NULL; the alg is NULL
  or one of RS256, PS256, ES256. A DCR row can never be `private_key_jwt`, and
  a `private_key_jwt` row without keys is unrepresentable.

A replaced constraint keeps its `COMMENT`: 013 marks the auth-method CHECK when
it repaired #53's drift, and its downgrade reads that marker, so dropping it
here would silently change what a later downgrade to 012 does.

## Re-runnable, by reconciliation (036's rule)

The schema gate stamps back and re-runs. Each column is adopted only with the
exact type, nullable, no default and its comment marker (mirrored in
`src/models/db.py`). A replaced CHECK whose definition is 038's is kept; one
whose definition is its predecessor's is replaced; anything else is refused by
name. The new CHECK is adopted only with 038's definition, validated, and
`CONSTRAINT_MARKER`. Definitions are compared against what this server prints
for a scratch probe constraint, never a hand-written rendering. A refusal
raises inside the migration transaction, so nothing changes. No backfill.

## Downgrade

Refuses while any `private_key_jwt` row exists, naming the count: dropping the
columns would leave grants whose client can no longer authenticate. Delete
those clients in the control panel first. Otherwise it drops the new CHECK and
the four columns and restores 036's exact two CHECK definitions.

Nothing here needs superuser: table ownership suffices
(`test_nonsuperuser_migrations_pg.py`).

Revision ID: 038
Revises: 037
Create Date: 2026-11-07
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "038"
down_revision: Union[str, None] = "037"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "oauth_clients"

#: Byte-identical to the `_OAUTH_CLIENT_PKJWT_*_MARKER` constants in src/models/db.py.
JWKS_URI_MARKER = "created by 038_oauth_cimd_private_key_jwt: pinned JWKS URI (#420)"
SIGNING_ALG_MARKER = (
    "created by 038_oauth_cimd_private_key_jwt: pinned assertion signing alg (#420)"
)
SNAPSHOT_MARKER = (
    "created by 038_oauth_cimd_private_key_jwt: validated JWKS snapshot (#420)"
)
FETCHED_AT_MARKER = (
    "created by 038_oauth_cimd_private_key_jwt: JWKS snapshot origin fetch time (#420)"
)

# (name, SQLAlchemy type, format_type rendering, marker)
COLUMNS = (
    ("jwks_uri", sa.String(2048), "character varying(2048)", JWKS_URI_MARKER),
    (
        "token_endpoint_auth_signing_alg",
        sa.String(16),
        "character varying(16)",
        SIGNING_ALG_MARKER,
    ),
    ("jwks_snapshot", JSONB(), "jsonb", SNAPSHOT_MARKER),
    (
        "jwks_fetched_at",
        sa.DateTime(timezone=True),
        "timestamp with time zone",
        FETCHED_AT_MARKER,
    ),
)

AUTH_SECRET = "ck_oauth_clients_auth_method_secret"
#: 010's predicate, as 013 reconciles it — the definition 038 replaces.
AUTH_SECRET_SQL_OLD = (
    "(token_endpoint_auth_method = 'none' AND client_secret_hash IS NULL) OR "
    "(token_endpoint_auth_method = 'client_secret_post' AND client_secret_hash IS NOT NULL)"
)
AUTH_SECRET_SQL = (
    "(token_endpoint_auth_method = 'none' AND client_secret_hash IS NULL) OR "
    "(token_endpoint_auth_method = 'client_secret_post' AND client_secret_hash IS NOT NULL) OR "
    "(token_endpoint_auth_method = 'private_key_jwt' AND client_secret_hash IS NULL)"
)

CIMD_SHAPE = "ck_oauth_clients_cimd_shape"
#: 036's predicate, verbatim — the definition 038 replaces.
CIMD_SHAPE_SQL_OLD = (
    "((client_id LIKE 'cimd\\_%') = (client_id_metadata_url IS NOT NULL)) AND "
    "(client_id_metadata_url IS NULL OR "
    "(token_endpoint_auth_method = 'none' AND client_secret_hash IS NULL))"
)
CIMD_SHAPE_SQL = (
    "((client_id LIKE 'cimd\\_%') = (client_id_metadata_url IS NOT NULL)) AND "
    "(client_id_metadata_url IS NULL OR "
    "(token_endpoint_auth_method IN ('none', 'private_key_jwt') "
    "AND client_secret_hash IS NULL))"
)

PKJWT_SHAPE = "ck_oauth_clients_private_key_jwt_shape"
PKJWT_SHAPE_SQL = (
    "((token_endpoint_auth_method = 'private_key_jwt' "
    "AND client_id_metadata_url IS NOT NULL AND jwks_uri IS NOT NULL "
    "AND jwks_snapshot IS NOT NULL AND jwks_fetched_at IS NOT NULL) OR "
    "(token_endpoint_auth_method <> 'private_key_jwt' "
    "AND jwks_uri IS NULL AND token_endpoint_auth_signing_alg IS NULL "
    "AND jwks_snapshot IS NULL AND jwks_fetched_at IS NULL)) AND "
    "(token_endpoint_auth_signing_alg IS NULL OR "
    "token_endpoint_auth_signing_alg IN ('RS256', 'PS256', 'ES256'))"
)
#: Written as a COMMENT on the new constraint; a re-run adopts only a marked one.
CONSTRAINT_MARKER = "created by 038_oauth_cimd_private_key_jwt (#420)"

#: (name, 038's definition, its predecessor's definition)
REPLACED = (
    (AUTH_SECRET, AUTH_SECRET_SQL, AUTH_SECRET_SQL_OLD),
    (CIMD_SHAPE, CIMD_SHAPE_SQL, CIMD_SHAPE_SQL_OLD),
)


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _normalize(definition: str) -> str:
    return " ".join(definition.replace(" NOT VALID", "").split())


def _column_state(bind, column: str):
    return bind.execute(
        sa.text(
            "SELECT format_type(a.atttypid, a.atttypmod), a.attnotnull, "
            "       pg_get_expr(d.adbin, d.adrelid), "
            "       col_description(a.attrelid, a.attnum) "
            "FROM pg_attribute a "
            "LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
            "WHERE a.attrelid = CAST('public.oauth_clients' AS regclass) "
            "  AND a.attname = :column AND a.attnum > 0 AND NOT a.attisdropped"
        ),
        {"column": column},
    ).first()


def _constraint(bind, name: str):
    """`(convalidated, normalised definition, comment)` or None."""
    row = bind.execute(
        sa.text(
            "SELECT convalidated, pg_get_constraintdef(oid), "
            "       obj_description(oid, 'pg_constraint') "
            "FROM pg_constraint "
            "WHERE conrelid = CAST('public.oauth_clients' AS regclass) "
            "  AND conname = :name AND contype = 'c'"
        ),
        {"name": name},
    ).first()
    if row is None:
        return None
    return row[0], _normalize(row[1]), row[2]


def _render(bind, predicate: str) -> str:
    """What this server prints for `predicate` on `oauth_clients` (036's probe)."""
    op.execute(f"ALTER TABLE {TABLE} ADD CONSTRAINT ck_038_probe CHECK ({predicate}) NOT VALID")
    printed = bind.execute(
        sa.text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = CAST('public.oauth_clients' AS regclass) "
            "  AND conname = 'ck_038_probe'"
        )
    ).scalar_one()
    op.execute(f"ALTER TABLE {TABLE} DROP CONSTRAINT ck_038_probe")
    return _normalize(printed)


def _comment(name: str, comment) -> None:
    if comment is not None:
        op.execute(f"COMMENT ON CONSTRAINT {name} ON {TABLE} IS {_quote(comment)}")


def _replace(name: str, predicate: str, comment) -> None:
    """Swap a CHECK's definition, carrying its COMMENT (013's ownership marker)."""
    op.execute(f"ALTER TABLE {TABLE} DROP CONSTRAINT {name}")
    op.create_check_constraint(name, TABLE, predicate)
    _comment(name, comment)


def _pin_and_assert(bind) -> None:
    """036's device: pin `search_path` and prove the unqualified name is ours."""
    op.execute("SET LOCAL search_path TO public")
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.execute("SET LOCAL statement_timeout = '60s'")
    qualified = bind.execute(
        sa.text("SELECT CAST(to_regclass('public.oauth_clients') AS oid)")
    ).scalar()
    unqualified = bind.execute(
        sa.text("SELECT CAST(to_regclass('oauth_clients') AS oid)")
    ).scalar()
    if qualified is None or qualified != unqualified:
        raise RuntimeError(
            "038's oauth_clients is not the table public.oauth_clients resolves "
            f"to (search_path-relative oid {unqualified!r}, qualified oid "
            f"{qualified!r}). Set the migration role's search_path so `public` "
            "comes first, then re-run. Nothing has been changed."
        )


def _reset() -> None:
    # alembic runs every pending revision in one transaction (036's note).
    op.execute("RESET lock_timeout")
    op.execute("RESET statement_timeout")
    op.execute("RESET search_path")


def upgrade() -> None:
    bind = op.get_bind()
    _pin_and_assert(bind)

    # Every column is judged before any is added, so a refusal names them all.
    problems = []
    missing = []
    for column, _type, expected_type, marker in COLUMNS:
        state = _column_state(bind, column)
        if state is None:
            missing.append((column, _type, marker))
            continue
        coltype, notnull, default, comment = state
        if coltype != expected_type:
            problems.append(f"{TABLE}.{column} is {coltype}, not {expected_type}")
        if notnull:
            problems.append(f"{TABLE}.{column} is NOT NULL")
        if default is not None:
            problems.append(f"{TABLE}.{column} has a server default of {default!r}")
        if comment != marker:
            problems.append(f"{TABLE}.{column} does not carry 038's comment marker")
    if problems:
        raise RuntimeError(
            f"{'; '.join(problems)}. 038 will not adopt a column of unknown "
            "provenance. Resolve by hand (drop it and let 038 create it), then "
            "re-run. Nothing has been changed."
        )
    for column, _type, marker in missing:
        op.add_column(TABLE, sa.Column(column, _type, nullable=True))
        op.execute(f"COMMENT ON COLUMN {TABLE}.{column} IS {_quote(marker)}")

    # The replaced CHECKs: ours is kept, the predecessor's is replaced (keeping
    # its comment), absent is created, anything else is refused.
    for name, predicate, predecessor in REPLACED:
        existing = _constraint(bind, name)
        if existing is None:
            op.create_check_constraint(name, TABLE, predicate)
            continue
        validated, definition, comment = existing
        if validated and definition == _render(bind, predicate):
            continue
        if validated and definition == _render(bind, predecessor):
            _replace(name, predicate, comment)
            continue
        raise RuntimeError(
            f"{name} exists with a definition that is neither 038's nor the one "
            f"038 replaces ({definition!r}, validated={validated}). 038 will not "
            "adopt or replace it. Drop it and re-run. Nothing has been changed."
        )

    existing = _constraint(bind, PKJWT_SHAPE)
    if existing is None:
        op.create_check_constraint(PKJWT_SHAPE, TABLE, PKJWT_SHAPE_SQL)
        _comment(PKJWT_SHAPE, CONSTRAINT_MARKER)
    else:
        validated, definition, comment = existing
        if not (
            validated
            and comment == CONSTRAINT_MARKER
            and definition == _render(bind, PKJWT_SHAPE_SQL)
        ):
            raise RuntimeError(
                f"{PKJWT_SHAPE} already exists but is not 038's (definition "
                f"{definition!r}, validated={validated}, marker "
                f"{'present' if comment == CONSTRAINT_MARKER else 'absent'}). 038 "
                "will not adopt it. Drop it and re-run. Nothing has been changed."
            )

    _reset()


def downgrade() -> None:
    bind = op.get_bind()
    _pin_and_assert(bind)
    count = bind.execute(
        sa.text(
            f"SELECT count(*) FROM {TABLE} "
            "WHERE token_endpoint_auth_method = 'private_key_jwt'"
        )
    ).scalar_one()
    if count:
        raise RuntimeError(
            f"038 downgrade refused: {count} private_key_jwt client row(s) exist in "
            f"{TABLE}. Delete those clients in the control panel (their grants go "
            "with them) and run the downgrade again."
        )
    if _constraint(bind, PKJWT_SHAPE) is not None:
        op.drop_constraint(PKJWT_SHAPE, TABLE, type_="check")
    for name, _predicate, predecessor in REPLACED:
        existing = _constraint(bind, name)
        if existing is None:
            op.create_check_constraint(name, TABLE, predecessor)
        else:
            _replace(name, predecessor, existing[2])
    for column, _type, _expected, _marker in COLUMNS:
        if _column_state(bind, column) is not None:
            op.drop_column(TABLE, column)
    _reset()
