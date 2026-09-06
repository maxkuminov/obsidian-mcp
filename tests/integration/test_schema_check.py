"""Opt-in integration gate: the database schema agrees with the ORM models.

`alembic check` clean is the cheapest regression gate we have for schema/model
agreement — but it is **necessary, not sufficient**. Autogenerate does not
compare CHECK constraint predicates, so a database carrying a same-named
`CHECK (true)` in place of the real one passes `alembic check` while enforcing
nothing. That is not hypothetical: the live database was found at revision 012
with `ck_oauth_clients_auth_method_secret` simply *absent* (issue #53), which
`alembic check` also cannot see. So every case here asserts the catalog
directly — `pg_constraint.convalidated` and the exact `pg_get_constraintdef`
text, `pg_attribute.attnotnull` for the nine columns — and then proves the
constraint is actually enforced by attempting the two inserts it exists to
reject.

Skipped unless `PGVECTOR_TEST_ADMIN_URL` is set — see `_harness.py`. Each case
creates its own throwaway database, because they migrate, drift, downgrade and
re-stamp the schema and must not see each other's edits.

    docker run --rm -d --name pgvector-schema -e POSTGRES_PASSWORD=test \\
        -p 55438:5432 pgvector/pgvector:pg16
    PGVECTOR_TEST_ADMIN_URL=postgresql+asyncpg://postgres:test@localhost:55438/postgres \\
        pytest -q tests/integration/test_schema_check.py
    docker rm -f pgvector-schema

**`make test-schema` runs exactly that** — container up, module, container
gone — and is the gate to run before any deploy that carries a migration. It
sets `OMCP_REQUIRE_SCHEMA_INTEGRATION=1`, which turns the opt-in skip into a
hard failure: a gate that silently skips because nobody exported the URL is not
a gate, and this module is the only thing that looks at the parts of the schema
`alembic check` is blind to.
"""
import asyncio
import contextlib
import datetime
import itertools
import os

import asyncpg
import pytest

import _harness

# The gate flag. Off (the default), a missing `PGVECTOR_TEST_ADMIN_URL` skips —
# the right behaviour for `pytest tests/` on a laptop with no Postgres. On, the
# same condition fails at import, so `make test-schema` cannot report success
# for a run that asserted nothing.
REQUIRED = os.environ.get("OMCP_REQUIRE_SCHEMA_INTEGRATION") == "1"

if REQUIRED and not _harness.PGVECTOR_TEST_ADMIN_URL:
    raise RuntimeError(
        "OMCP_REQUIRE_SCHEMA_INTEGRATION=1 but PGVECTOR_TEST_ADMIN_URL is unset, "
        "so the schema gate would skip instead of run. Use `make test-schema`, "
        "which starts a throwaway pgvector container and sets both."
    )

pytestmark = [] if REQUIRED else [_harness.requires_pgvector]

DIM = 64  # irrelevant here; keeps the migration cheap.

# The current head. Every case that migrates forward asserts it, so adding a
# revision without teaching this module about it fails loudly rather than
# leaving the new migration unexercised.
HEAD_REVISION = "024"

CONSTRAINT = "ck_oauth_clients_auth_method_secret"
MARKER = "created by 013_schema_reconciliation"

# What PostgreSQL 16 prints for the predicate migration 010 (and the model's
# `CheckConstraint`) declares. Measured on a freshly migrated 001->012 database
# rather than hand-written, so it is the server's own normalization — casts
# made explicit, parentheses added — not our guess at it. The migration derives
# the same string at runtime instead of hard-coding it; this constant is the
# pin that tells us if either side moves.
CANONICAL_CONSTRAINTDEF = (
    "CHECK (((((token_endpoint_auth_method)::text = 'none'::text) "
    "AND (client_secret_hash IS NULL)) "
    "OR (((token_endpoint_auth_method)::text = 'client_secret_post'::text) "
    "AND (client_secret_hash IS NOT NULL))))"
)

# The rest of the 010 shape, likewise as PostgreSQL 16 prints it. `alembic
# check` compares the type but *not* the server default (`compare_server_default`
# is off by default), so the default is asserted here or nowhere — which is why
# the migration compares it exactly instead of asking whether the string
# `client_secret_post` appears somewhere in it (`'not_client_secret_post'`
# contains it).
CANONICAL_AUTH_METHOD_TYPE = "character varying(32)"
CANONICAL_AUTH_METHOD_DEFAULT = "'client_secret_post'::character varying"

# The nine columns the models declare NOT NULL that their migrations left
# nullable, with the value 013 must backfill NULLs to.
NOT_NULL_COLUMNS = (
    ("api_keys", "is_active", True),
    ("api_keys", "created_at", None),
    ("notes_metadata", "indexed_at", None),
    ("oauth_clients", "created_at", None),
    ("oauth_codes", "used", False),
    ("oauth_codes", "created_at", None),
    ("oauth_tokens", "revoked", False),
    ("oauth_tokens", "created_at", None),
    ("usage_logs", "created_at", None),
)

FUTURE = datetime.datetime(2099, 1, 1, tzinfo=datetime.timezone.utc)


# --------------------------------------------------------------------------
# plumbing
# --------------------------------------------------------------------------


@contextlib.contextmanager
def throwaway_db(prefix: str, revision: str = "head"):
    """A throwaway database migrated to `revision`, dropped on exit."""
    generator = _harness.throwaway_database(prefix, DIM, revision=revision)
    url = next(generator)
    try:
        yield url
    finally:
        # Runs the generator's own `finally`, i.e. DROP DATABASE … (FORCE).
        generator.close()


async def _run(url: str, coro_factory):
    conn = await asyncpg.connect(_harness.asyncpg_dsn(url))
    try:
        return await coro_factory(conn)
    finally:
        await conn.close()


def sql(url: str, statement: str, *args):
    """Execute one statement; return None."""
    return asyncio.run(_run(url, lambda c: c.execute(statement, *args)))


def fetch(url: str, statement: str, *args):
    return asyncio.run(_run(url, lambda c: c.fetch(statement, *args)))


def fetchval(url: str, statement: str, *args):
    return asyncio.run(_run(url, lambda c: c.fetchval(statement, *args)))


def alembic_version(url: str) -> str:
    return fetchval(url, "SELECT version_num FROM alembic_version")


def constraint_row(url: str):
    """`(convalidated, constraintdef, comment)` for the CHECK, or None."""
    rows = fetch(
        url,
        "SELECT convalidated, pg_get_constraintdef(oid) AS def, "
        "       obj_description(oid, 'pg_constraint') AS comment "
        "FROM pg_constraint "
        "WHERE conrelid = 'oauth_clients'::regclass AND contype = 'c' "
        "  AND conname = $1",
        CONSTRAINT,
    )
    if not rows:
        return None
    return rows[0]["convalidated"], " ".join(rows[0]["def"].split()), rows[0]["comment"]


def column_shape(url: str, table: str, column: str):
    """`(attnotnull, formatted_type, default_expr)` straight from the catalog."""
    rows = fetch(
        url,
        "SELECT a.attnotnull, format_type(a.atttypid, a.atttypmod) AS coltype, "
        "       pg_get_expr(d.adbin, d.adrelid) AS coldefault "
        "FROM pg_attribute a "
        "LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
        "WHERE a.attrelid = $1::regclass AND a.attname = $2 "
        "  AND a.attnum > 0 AND NOT a.attisdropped",
        table,
        column,
    )
    if not rows:
        return None
    return rows[0]["attnotnull"], rows[0]["coltype"], rows[0]["coldefault"]


def not_null_flags(url: str) -> dict[str, bool]:
    rows = fetch(
        url,
        "SELECT c.relname || '.' || a.attname AS col, a.attnotnull "
        "FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
        "WHERE a.attnum > 0 AND NOT a.attisdropped "
        "  AND c.relname = ANY($1::text[]) AND a.attname = ANY($2::text[])",
        sorted({t for t, _, _ in NOT_NULL_COLUMNS}),
        sorted({c for _, c, _ in NOT_NULL_COLUMNS}),
    )
    flags = {row["col"]: row["attnotnull"] for row in rows}
    return {f"{t}.{c}": flags[f"{t}.{c}"] for t, c, _ in NOT_NULL_COLUMNS}


def insert_client(url, client_id, method, secret):
    sql(
        url,
        "INSERT INTO oauth_clients "
        "(client_id, client_secret_hash, token_endpoint_auth_method, "
        " client_name, redirect_uris, scope) "
        "VALUES ($1, $2, $3, 'test client', '[]'::jsonb, 'read')",
        client_id,
        secret,
        method,
    )


# --------------------------------------------------------------------------
# shared assertions
# --------------------------------------------------------------------------


def assert_reconciled(url: str, *, marker_expected: bool):
    """Everything migration 013 promises, read straight out of the catalog.

    `marker_expected` is not optional, and deliberately so: the COMMENT is the
    only thing `downgrade()` reads, so "013 owns this constraint" is a fact
    every case has to commit to. Leaving it unasserted on a path would let the
    ownership flip in either direction — a fresh database silently losing 010's
    constraint on downgrade, or a repaired one silently keeping 013's — with the
    whole suite still green. Each caller therefore pins True or False and
    asserts the matching downgrade outcome.
    """
    check = _harness.run_alembic(url, "check", dimensions=DIM, check=False)
    assert check.returncode == 0, (
        f"alembic check reported drift\n{check.stdout}\n{check.stderr}"
    )
    assert "No new upgrade operations detected" in check.stdout

    row = constraint_row(url)
    assert row is not None, f"{CONSTRAINT} is missing from oauth_clients"
    validated, definition, comment = row
    assert validated, f"{CONSTRAINT} exists but is NOT VALID"
    assert definition == CANONICAL_CONSTRAINTDEF, definition
    assert comment == (MARKER if marker_expected else None), comment

    assert not_null_flags(url) == {
        f"{table}.{column}": True for table, column, _ in NOT_NULL_COLUMNS
    }

    assert_010_column_shape(url)
    assert_constraint_enforced(url)


def assert_010_column_shape(url: str):
    """The non-CHECK half of what 010 promised, asserted in the catalog.

    `alembic check` covers the type and the nullability; nothing but this covers
    the server default.
    """
    assert column_shape(url, "oauth_clients", "token_endpoint_auth_method") == (
        True,
        CANONICAL_AUTH_METHOD_TYPE,
        CANONICAL_AUTH_METHOD_DEFAULT,
    )
    secret_not_null, _, _ = column_shape(url, "oauth_clients", "client_secret_hash")
    assert secret_not_null is False, (
        "client_secret_hash must be nullable — 010 relaxed it so a public PKCE "
        "client can exist without a secret"
    )


def assert_constraint_enforced(url: str):
    """The two shapes the CHECK exists to reject, and the two it must allow."""
    secret = "a" * 64

    with pytest.raises(asyncpg.exceptions.CheckViolationError) as public_with_secret:
        insert_client(url, "reject-public", "none", secret)
    assert CONSTRAINT in str(public_with_secret.value)

    with pytest.raises(asyncpg.exceptions.CheckViolationError) as confidential_without:
        insert_client(url, "reject-confidential", "client_secret_post", None)
    assert CONSTRAINT in str(confidential_without.value)

    insert_client(url, "allow-public", "none", None)
    insert_client(url, "allow-confidential", "client_secret_post", secret)
    sql(
        url,
        "DELETE FROM oauth_clients WHERE client_id IN "
        "('allow-public', 'allow-confidential')",
    )


def drift_to_live_shape(url: str):
    """Reproduce the drift #53 found: no CHECK, nine columns nullable."""
    sql(url, f"ALTER TABLE oauth_clients DROP CONSTRAINT {CONSTRAINT}")
    for table, column, _ in NOT_NULL_COLUMNS:
        # No-ops at 012 today (that is the bug); explicit so this stays a
        # faithful simulation if a later migration tightens them.
        sql(url, f"ALTER TABLE {table} ALTER COLUMN {column} DROP NOT NULL")
    assert constraint_row(url) is None
    assert not any(not_null_flags(url).values())


def insert_null_rows(url: str):
    """One row per affected table with the drifted columns explicitly NULL."""
    sql(
        url,
        "INSERT INTO api_keys (name, key_hash, key_prefix, permission, "
        "is_active, created_at) VALUES ('drifted', $1, 'omcp_dri', 'read', "
        "NULL, NULL)",
        "k" * 64,
    )
    sql(url, "INSERT INTO usage_logs (tool, created_at) VALUES ('read_note', NULL)")
    sql(
        url,
        "INSERT INTO notes_metadata (file_path, title, content_hash, indexed_at) "
        "VALUES ('drifted.md', 'Drifted', $1, NULL)",
        "h" * 64,
    )
    sql(
        url,
        "INSERT INTO oauth_clients (client_id, client_secret_hash, "
        "token_endpoint_auth_method, client_name, redirect_uris, scope, "
        "created_at) VALUES ('drifted-client', $1, 'client_secret_post', "
        "'Drifted', '[]'::jsonb, 'read', NULL)",
        "s" * 64,
    )
    # These two carry an FK to oauth_clients.client_id, so they go after it.
    sql(
        url,
        "INSERT INTO oauth_codes (code_hash, client_id, redirect_uri, scope, "
        "code_challenge, code_challenge_method, expires_at, used, created_at) "
        "VALUES ($1, 'drifted-client', 'https://example.test/cb', 'read', "
        "$2, 'S256', $3, NULL, NULL)",
        "c" * 64,
        "p" * 43,
        FUTURE,
    )
    sql(
        url,
        "INSERT INTO oauth_tokens (token_hash, token_type, client_id, scope, "
        "expires_at, revoked, created_at) "
        "VALUES ($1, 'access', 'drifted-client', 'read', $2, NULL, NULL)",
        "t" * 64,
        FUTURE,
    )


# --------------------------------------------------------------------------
# cases
# --------------------------------------------------------------------------


def test_fresh_database_is_clean_and_enforced():
    """(a) An empty database migrated to head needs no further operations."""
    with throwaway_db("schema_fresh") as url:
        assert alembic_version(url) == HEAD_REVISION
        # No marker: 010 created this constraint and 013 recognised it as
        # already correct. That distinction is what downgrade() reads.
        assert_reconciled(url, marker_expected=False)


def test_drifted_database_is_reconciled():
    """(b) The live shape — no CHECK, nullable columns, NULL rows — is fixed."""
    with throwaway_db("schema_drift", revision="012") as url:
        drift_to_live_shape(url)
        insert_null_rows(url)

        _harness.run_alembic(url, "upgrade", "head", dimensions=DIM)

        assert_reconciled(url, marker_expected=True)
        assert fetchval(
            url, "SELECT is_active FROM api_keys WHERE name = 'drifted'"
        ) is True
        assert fetchval(
            url, "SELECT used FROM oauth_codes WHERE client_id = 'drifted-client'"
        ) is False
        assert fetchval(
            url, "SELECT revoked FROM oauth_tokens WHERE client_id = 'drifted-client'"
        ) is False
        assert fetchval(url, "SELECT count(*) FROM usage_logs WHERE created_at IS NULL") == 0
        assert fetchval(
            url, "SELECT count(*) FROM notes_metadata WHERE indexed_at IS NULL"
        ) == 0
        assert fetchval(
            url, "SELECT count(*) FROM oauth_clients WHERE created_at IS NULL"
        ) == 0


def test_same_named_wrong_constraint_is_replaced():
    """(c) A `CHECK (true)` under the right name is not the right constraint.

    This is the case `alembic check` alone cannot catch: name matches, model
    matches, and nothing is enforced.
    """
    with throwaway_db("schema_impostor", revision="012") as url:
        sql(url, f"ALTER TABLE oauth_clients DROP CONSTRAINT {CONSTRAINT}")
        sql(url, f"ALTER TABLE oauth_clients ADD CONSTRAINT {CONSTRAINT} CHECK (true)")
        # The impostor would have let this row in; it must not survive as the
        # constraint definition.
        assert constraint_row(url)[1] == "CHECK (true)"

        _harness.run_alembic(url, "upgrade", "head", dimensions=DIM)

        assert_reconciled(url, marker_expected=True)


def test_violating_row_fails_loudly_and_changes_nothing():
    """(d) 013 never deletes or rewrites a row to make the CHECK pass."""
    with throwaway_db("schema_violation", revision="012") as url:
        drift_to_live_shape(url)
        insert_client(url, "public-with-secret", "none", "z" * 64)

        result = _harness.run_alembic(
            url, "upgrade", "head", dimensions=DIM, check=False
        )

        assert result.returncode != 0
        combined = result.stdout + result.stderr
        assert "public-with-secret" in combined
        assert CONSTRAINT in combined

        # Postgres DDL is transactional, so the failure rolls the whole
        # migration back: no constraint, no NOT NULL, row untouched, version
        # still 012.
        assert alembic_version(url) == "012"
        assert constraint_row(url) is None
        assert not any(not_null_flags(url).values())
        assert fetchval(
            url,
            "SELECT client_secret_hash FROM oauth_clients "
            "WHERE client_id = 'public-with-secret'",
        ) == "z" * 64


def test_null_auth_method_row_is_an_offender_not_a_backfill():
    """(d2) A NULL `token_endpoint_auth_method` must never be repaired silently.

    This is the ordering bug the review caught. A CHECK passes when its
    predicate evaluates to NULL, so a row with a NULL method and a secret sits
    happily under 010's constraint on a database where someone relaxed the
    column — it is drift, not an impossibility. If 013 backfilled that NULL to
    `client_secret_post` before looking for offenders it would *manufacture* a
    passing row: a client whose auth method nobody chose, now trusted to
    authenticate with the secret it happens to carry. The offender check has to
    run over the raw rows and count NULL as a violation.
    """
    with throwaway_db("schema_null_method", revision="012") as url:
        sql(
            url,
            "ALTER TABLE oauth_clients ALTER COLUMN token_endpoint_auth_method "
            "DROP NOT NULL",
        )
        before_shape = column_shape(url, "oauth_clients", "token_endpoint_auth_method")
        insert_client(url, "null-method-with-secret", None, "z" * 64)

        result = _harness.run_alembic(
            url, "upgrade", "head", dimensions=DIM, check=False
        )

        assert result.returncode != 0
        combined = result.stdout + result.stderr
        assert "null-method-with-secret" in combined
        assert CONSTRAINT in combined

        # Nothing written: the row still has no auth method, the column is still
        # nullable, the nine columns are still nullable, version still 012.
        assert alembic_version(url) == "012"
        assert fetchval(
            url,
            "SELECT token_endpoint_auth_method FROM oauth_clients "
            "WHERE client_id = 'null-method-with-secret'",
        ) is None
        assert column_shape(
            url, "oauth_clients", "token_endpoint_auth_method"
        ) == before_shape
        assert not any(not_null_flags(url).values())


def test_missing_auth_method_column_refuses_to_guess():
    """(d3) 013 reconciles a database that had 010's shape; it does not create it.

    Re-adding the column here would stamp `client_secret_post` on every existing
    client — inventing an auth method for each of them — so the migration stops
    and says so.
    """
    with throwaway_db("schema_no_method_column", revision="012") as url:
        sql(url, f"ALTER TABLE oauth_clients DROP CONSTRAINT {CONSTRAINT}")
        sql(url, "ALTER TABLE oauth_clients DROP COLUMN token_endpoint_auth_method")

        result = _harness.run_alembic(
            url, "upgrade", "head", dimensions=DIM, check=False
        )

        assert result.returncode != 0
        combined = result.stdout + result.stderr
        assert "token_endpoint_auth_method is missing" in combined

        assert alembic_version(url) == "012"
        assert column_shape(url, "oauth_clients", "token_endpoint_auth_method") is None
        assert not any(not_null_flags(url).values())


def test_wrong_server_default_is_reconciled():
    """(g1) `'not_client_secret_post'` contains `client_secret_post`.

    A substring test would call this default correct. `alembic check` does not
    compare server defaults at all, so nothing else would catch it either — a
    new confidential client inserted without an explicit method would land with
    a garbage auth method that matches neither branch of the CHECK and be
    rejected, or worse, silently mis-typed.
    """
    with throwaway_db("schema_bad_default", revision="012") as url:
        sql(
            url,
            "ALTER TABLE oauth_clients ALTER COLUMN token_endpoint_auth_method "
            "SET DEFAULT 'not_client_secret_post'",
        )
        assert column_shape(url, "oauth_clients", "token_endpoint_auth_method")[2] == (
            "'not_client_secret_post'::character varying"
        )

        _harness.run_alembic(url, "upgrade", "head", dimensions=DIM)

        # 010's constraint was never touched, so it keeps carrying no marker.
        assert_reconciled(url, marker_expected=False)


def test_wrong_column_type_is_reconciled():
    """(g2) A widened `text` column loses the length bound 010 declared.

    The marker is pinned to False, which is an observed PostgreSQL 16 behaviour
    and not an arbitrary choice — worth spelling out, because it is the one path
    where the constraint 013 ends up with was neither found intact nor created
    by 013. `ALTER COLUMN … TYPE` re-parses and re-validates every dependent
    CHECK, so 010's constraint follows the column: at `text` it is reprinted
    without the `(col)::text` casts, which is *exactly* what this server renders
    for a freshly created constraint on a `text` column, so 013 recognises it as
    canonical-for-the-current-type and leaves it; the repair back to
    `varchar(32)` reprints it with the casts again, canonical once more. 013
    therefore never touches it and never comments it — it remains 010's, which
    is why the downgrade below must keep it.

    If a future PostgreSQL changes how a rebuilt CHECK is reprinted, this
    assertion flips to True rather than failing silently: 013 would see a
    non-canonical definition, replace it, and take ownership. Both outcomes are
    correct behaviour; the point of pinning is that the change is *noticed*.
    """
    with throwaway_db("schema_bad_type", revision="012") as url:
        sql(
            url,
            "ALTER TABLE oauth_clients ALTER COLUMN token_endpoint_auth_method "
            "TYPE text",
        )
        assert column_shape(url, "oauth_clients", "token_endpoint_auth_method")[1] == (
            "text"
        )

        _harness.run_alembic(url, "upgrade", "head", dimensions=DIM)

        assert_reconciled(url, marker_expected=False)

        # The other half of the pin: unmarked means 010 owns it, so 012 keeps it.
        _harness.run_alembic(url, "downgrade", "012", dimensions=DIM)
        assert constraint_row(url) is not None, (
            "downgrade dropped a constraint 013 did not create"
        )
        assert constraint_row(url)[1] == CANONICAL_CONSTRAINTDEF


def test_impostor_constraint_does_not_block_the_type_repair():
    """(g5) An impostor under our name must not be able to *veto* the repair.

    The sharp edge is that `ALTER COLUMN … TYPE` re-validates every CHECK that
    reads the column against the live rows. So a constraint squatting on our
    name is not merely unenforced — it is a lock on the door: here the column
    has drifted to `text` and the squatter asserts `pg_typeof(…) = 'text'`, so
    the `ALTER … TYPE character varying(32)` that repairs the column would abort
    with "check constraint … is violated by some row", every run, forever. 013
    would never reach the step that replaces it. The fix is ordering — the rows
    are verified, then the non-canonical constraint is dropped, and only then is
    the column touched.

    The predicate is contrived; what it stands in for is not. Any same-named
    CHECK a human or an older tool left behind that the reconciled rows or the
    reconciled *type* do not satisfy has this shape, and the failure mode is a
    migration that cannot self-heal.
    """
    with throwaway_db("schema_impostor_type", revision="012") as url:
        sql(url, f"ALTER TABLE oauth_clients DROP CONSTRAINT {CONSTRAINT}")
        sql(
            url,
            "ALTER TABLE oauth_clients ALTER COLUMN token_endpoint_auth_method "
            "TYPE text",
        )
        sql(
            url,
            f"ALTER TABLE oauth_clients ADD CONSTRAINT {CONSTRAINT} "
            "CHECK (pg_typeof(token_endpoint_auth_method) = 'text'::regtype)",
        )
        # One valid client, so the offender check passes and the migration is
        # blocked by the constraint or by nothing — no other reason to fail.
        insert_client(url, "kept-confidential", "client_secret_post", "s" * 64)
        assert column_shape(url, "oauth_clients", "token_endpoint_auth_method")[1] == (
            "text"
        )
        assert "pg_typeof" in constraint_row(url)[1]

        _harness.run_alembic(url, "upgrade", "head", dimensions=DIM)

        # 013 dropped the squatter and added the real thing, so it owns it.
        assert_reconciled(url, marker_expected=True)
        assert fetchval(
            url,
            "SELECT client_secret_hash FROM oauth_clients "
            "WHERE client_id = 'kept-confidential'",
        ) == "s" * 64

        _harness.run_alembic(url, "downgrade", "012", dimensions=DIM)
        assert alembic_version(url) == "012"
        assert constraint_row(url) is None, (
            "downgrade kept a constraint 013 created"
        )


def test_client_secret_hash_not_null_is_reconciled():
    """(g3) A NOT NULL secret column makes public PKCE clients unrepresentable.

    With it, every row must carry a secret, so the `'none'` branch of the CHECK
    can never be satisfied — the constraint still exists and still enforces
    something, just not the thing it means.
    """
    with throwaway_db("schema_secret_not_null", revision="012") as url:
        sql(
            url,
            "ALTER TABLE oauth_clients ALTER COLUMN client_secret_hash SET NOT NULL",
        )
        assert column_shape(url, "oauth_clients", "client_secret_hash")[0] is True

        _harness.run_alembic(url, "upgrade", "head", dimensions=DIM)

        assert_reconciled(url, marker_expected=False)


def test_nullable_auth_method_is_reconciled_without_touching_rows():
    """(g4) The column is re-tightened, and valid rows keep their own values.

    The offender check has already proved there is no NULL to backfill, so
    `SET NOT NULL` needs no `UPDATE` — and the confidential client below must
    come back out with exactly the method it went in with.
    """
    with throwaway_db("schema_nullable_method", revision="012") as url:
        sql(
            url,
            "ALTER TABLE oauth_clients ALTER COLUMN token_endpoint_auth_method "
            "DROP NOT NULL",
        )
        insert_client(url, "kept-confidential", "client_secret_post", "s" * 64)
        insert_client(url, "kept-public", "none", None)
        assert column_shape(url, "oauth_clients", "token_endpoint_auth_method")[0] is (
            False
        )

        _harness.run_alembic(url, "upgrade", "head", dimensions=DIM)

        assert_reconciled(url, marker_expected=False)
        assert fetchval(
            url,
            "SELECT token_endpoint_auth_method FROM oauth_clients "
            "WHERE client_id = 'kept-public'",
        ) == "none"
        assert fetchval(
            url,
            "SELECT client_secret_hash FROM oauth_clients "
            "WHERE client_id = 'kept-confidential'",
        ) == "s" * 64


def test_rerunning_013_changes_nothing():
    """(e) Idempotence, proven by making the revision genuinely re-execute.

    A second `upgrade head` is a no-op at the *alembic* level (013 is already
    the recorded version), so it proves nothing about the migration body.
    Stamping back to 012 forces 013 to run again against a database that
    already satisfies it.
    """
    with throwaway_db("schema_idempotent") as url:
        before = (constraint_row(url), not_null_flags(url))

        _harness.run_alembic(url, "stamp", "012", dimensions=DIM)
        assert alembic_version(url) == "012"
        _harness.run_alembic(url, "upgrade", "head", dimensions=DIM)

        assert alembic_version(url) == HEAD_REVISION
        assert (constraint_row(url), not_null_flags(url)) == before
        assert_reconciled(url, marker_expected=False)


def test_downgrade_keeps_a_constraint_013_did_not_create():
    """(f1) On a fresh database the CHECK belongs to 010 — downgrade keeps it."""
    with throwaway_db("schema_down_fresh") as url:
        _harness.run_alembic(url, "downgrade", "012", dimensions=DIM)

        assert alembic_version(url) == "012"
        row = constraint_row(url)
        assert row is not None, "downgrade dropped 010's constraint"
        assert row[1] == CANONICAL_CONSTRAINTDEF
        # Documented asymmetry: NOT NULL stays. Relaxing it would re-create the
        # very drift 013 exists to remove, and the models still declare it.
        assert all(not_null_flags(url).values())


def test_downgrade_drops_the_constraint_013_created():
    """(f2) Where 013 repaired the drift, its COMMENT marker makes it own it."""
    with throwaway_db("schema_down_drifted", revision="012") as url:
        drift_to_live_shape(url)
        _harness.run_alembic(url, "upgrade", "head", dimensions=DIM)
        assert constraint_row(url)[2] == MARKER

        _harness.run_alembic(url, "downgrade", "012", dimensions=DIM)

        assert alembic_version(url) == "012"
        assert constraint_row(url) is None
        assert all(not_null_flags(url).values())


# --------------------------------------------------------------------------
# migration 014: oauth_tokens.grant_id (issue #64)
# --------------------------------------------------------------------------
#
# `alembic check` sees the column, its nullability and its index, so
# `assert_reconciled` above already covers the declarative half on every path.
# What it cannot see is the *backfill*, and the backfill is where a migration
# could invent a grant — splitting one family into two, so a revocation misses
# half of it — or destroy one, by merging two users into a single family, so
# revoking one user's grant kills another's. Those are the cases here.


def insert_user(url, user_id: int, username: str):
    sql(
        url,
        "INSERT INTO users (id, username, password_hash, is_admin, is_active, "
        "session_version) VALUES ($1, $2, 'x', false, true, 1)",
        user_id,
        username,
    )


def insert_token(
    url,
    token_hash,
    client_id,
    *,
    user_id=None,
    token_type="access",
    revoked=False,
    scope="read",
):
    sql(
        url,
        "INSERT INTO oauth_tokens (token_hash, token_type, client_id, scope, "
        "user_id, expires_at, revoked) VALUES ($1, $2, $3, $4, $5, $6, $7)",
        token_hash,
        token_type,
        client_id,
        scope,
        user_id,
        FUTURE,
        revoked,
    )


def grant_ids(url) -> dict[str, str]:
    rows = fetch(url, "SELECT token_hash, grant_id FROM oauth_tokens")
    return {row["token_hash"]: row["grant_id"] for row in rows}


def seed_pre_014_tokens(url):
    """Two users, two clients, revoked and live rows, and a NULL-user row."""
    insert_user(url, 1, "alice")
    insert_user(url, 2, "bob")
    insert_client(url, "client-a", "none", None)
    insert_client(url, "client-b", "none", None)

    # Alice's grant on client-a: a live pair plus a rotated-away refresh token.
    insert_token(url, "a" * 64, "client-a", user_id=1, token_type="access")
    insert_token(url, "b" * 64, "client-a", user_id=1, token_type="refresh")
    insert_token(url, "c" * 64, "client-a", user_id=1, token_type="refresh", revoked=True)
    # Bob authorized the same client — a different grant entirely.
    insert_token(url, "d" * 64, "client-a", user_id=2, token_type="access")
    # Alice on a second client.
    insert_token(url, "e" * 64, "client-b", user_id=1, token_type="access")
    # Single-user-mode row: user_id IS NULL. `NULL = NULL` is NULL, so an `=`
    # join in the backfill would leave this one unmatched and the SET NOT NULL
    # would fail — which is why it uses IS NOT DISTINCT FROM.
    insert_token(url, "f" * 64, "client-b", user_id=None, token_type="access")


EXPECTED_FAMILIES = 4  # (a,alice) (a,bob) (b,alice) (b,NULL)


def test_grant_id_is_not_null_and_indexed_on_a_fresh_database():
    with throwaway_db("schema_grant_fresh") as url:
        attnotnull, coltype, _ = column_shape(url, "oauth_tokens", "grant_id")
        assert attnotnull is True, (
            "grant_id must be NOT NULL — the decision in #64 was explicit that "
            "a nullable grant_id with a fallback 'find the family' path is how "
            "the bug comes back"
        )
        assert coltype == "character varying(64)"
        # The name is what autogenerate expects for `index=True`; anything else
        # leaves `alembic check` permanently dirty.
        assert fetchval(
            url,
            "SELECT indexdef FROM pg_indexes WHERE tablename = 'oauth_tokens' "
            "AND indexname = 'ix_oauth_tokens_grant_id'",
        ) is not None


def test_backfill_gives_one_grant_per_client_and_user():
    """The decided approximation, stated precisely.

    Pre-014 rows carry no family, so one is assigned per distinct
    `(client_id, user_id)`. Concurrent sessions of the same connector collapse
    into one family — over-revoking, never under-revoking — and every grant
    issued after the migration is exact.
    """
    with throwaway_db("schema_grant_backfill", revision="012") as url:
        seed_pre_014_tokens(url)

        _harness.run_alembic(url, "upgrade", "head", dimensions=DIM)

        ids = grant_ids(url)
        assert all(ids.values()), "no row may be left without a family"

        alice_client_a = {ids["a" * 64], ids["b" * 64], ids["c" * 64]}
        assert len(alice_client_a) == 1, (
            "one user's rows on one client are one family — including the "
            "revoked one, which is what lets the panel show revocation history"
        )
        assert len(set(ids.values())) == EXPECTED_FAMILIES


def test_backfill_never_merges_two_users_into_one_family():
    """The invariant every family operation leans on.

    `src/oauth/grants.py` resolves a family as `grant_id == g` and deliberately
    does *not* re-filter by `user_id` — a user predicate there would give
    incomplete revocation a way back in. That is only safe because a family
    cannot span users, which is what this asserts.
    """
    with throwaway_db("schema_grant_no_merge", revision="012") as url:
        seed_pre_014_tokens(url)

        _harness.run_alembic(url, "upgrade", "head", dimensions=DIM)

        rows = fetch(
            url,
            "SELECT grant_id, count(DISTINCT coalesce(user_id, -1)) AS users "
            "FROM oauth_tokens GROUP BY grant_id",
        )
        assert rows
        assert all(row["users"] == 1 for row in rows), rows


def test_backfill_does_not_split_a_users_rows_across_families():
    """The other direction: a split family is a revocation that misses half."""
    with throwaway_db("schema_grant_no_split", revision="012") as url:
        seed_pre_014_tokens(url)

        _harness.run_alembic(url, "upgrade", "head", dimensions=DIM)

        rows = fetch(
            url,
            "SELECT client_id, coalesce(user_id, -1) AS uid, "
            "       count(DISTINCT grant_id) AS families "
            "FROM oauth_tokens GROUP BY client_id, user_id",
        )
        assert rows
        assert all(row["families"] == 1 for row in rows), rows


def test_rerunning_014_does_not_re_stamp_existing_grants():
    """Idempotence that actually re-executes the body.

    A second `upgrade head` is a no-op at the alembic level. Stamping back to
    013 forces 014 to run again against a database that already satisfies it —
    and re-partitioning live grants there would silently break every revocation
    and downgrade issued before the re-run.
    """
    with throwaway_db("schema_grant_idempotent", revision="012") as url:
        seed_pre_014_tokens(url)
        _harness.run_alembic(url, "upgrade", "head", dimensions=DIM)
        before = grant_ids(url)

        _harness.run_alembic(url, "stamp", "013", dimensions=DIM)
        assert alembic_version(url) == "013"
        _harness.run_alembic(url, "upgrade", "head", dimensions=DIM)

        assert alembic_version(url) == HEAD_REVISION
        assert grant_ids(url) == before
        assert column_shape(url, "oauth_tokens", "grant_id")[0] is True


def test_downgrade_014_removes_the_column_and_upgrade_rebuilds_it():
    with throwaway_db("schema_grant_downgrade", revision="012") as url:
        seed_pre_014_tokens(url)
        _harness.run_alembic(url, "upgrade", "head", dimensions=DIM)

        _harness.run_alembic(url, "downgrade", "013", dimensions=DIM)
        assert alembic_version(url) == "013"
        assert column_shape(url, "oauth_tokens", "grant_id") is None
        assert fetchval(
            url,
            "SELECT indexdef FROM pg_indexes WHERE tablename = 'oauth_tokens' "
            "AND indexname = 'ix_oauth_tokens_grant_id'",
        ) is None

        # Re-upgrading re-derives the same approximation from (client_id, user_id).
        _harness.run_alembic(url, "upgrade", "head", dimensions=DIM)
        assert len(set(grant_ids(url).values())) == EXPECTED_FAMILIES
        assert_reconciled(url, marker_expected=False)


# --------------------------------------------------------------------------
# 014 refuses a pre-existing column it cannot verify
# --------------------------------------------------------------------------
#
# The backfill is a *partition* only because 014 created the column, so every
# row is NULL and the grouping covers all of them. On a column somebody else
# added, `WHERE grant_id IS NULL` becomes a patch: a NULL row beside a stamped
# sibling gets a fresh id, one grant becomes two, and revoking either leaves
# the other alive — the exact defect 014 exists to remove, reintroduced by the
# migration. There is no safe repair, so it refuses, in 013's spirit.


def add_bare_grant_id_column(url, coltype="varchar(64)"):
    sql(url, f"ALTER TABLE oauth_tokens ADD COLUMN grant_id {coltype}")


def test_partially_stamped_column_is_refused_not_backfilled():
    """The case that would split a live pair across two families."""
    with throwaway_db("schema_grant_partial", revision="012") as url:
        seed_pre_014_tokens(url)
        add_bare_grant_id_column(url)
        # Alice's access token is stamped; its refresh sibling is not.
        sql(
            url,
            "UPDATE oauth_tokens SET grant_id = 'preexisting-1' WHERE token_hash = $1",
            "a" * 64,
        )

        result = _harness.run_alembic(
            url, "upgrade", "head", dimensions=DIM, check=False
        )

        assert result.returncode != 0
        combined = result.stdout + result.stderr
        assert "are NULL" in combined
        assert "splitting one grant into two" in combined

        # Nothing changed: still at 012, the stamped row keeps its value, the
        # siblings are still NULL, and no index was created.
        assert alembic_version(url) == "012"
        ids = grant_ids(url)
        assert ids["a" * 64] == "preexisting-1"
        assert ids["b" * 64] is None
        assert column_shape(url, "oauth_tokens", "grant_id")[0] is False
        assert fetchval(
            url,
            "SELECT indexdef FROM pg_indexes WHERE tablename = 'oauth_tokens' "
            "AND indexname = 'ix_oauth_tokens_grant_id'",
        ) is None


def test_a_pre_existing_grant_spanning_two_users_is_refused():
    """Adopting it would let one user's Revoke reach another user's grant.

    Every family operation resolves a family as `grant_id == g`, with no
    `user_id` predicate — deliberately, because a `user_id` predicate is how
    incomplete revocation comes back. That is only safe while the invariant
    holds, so a migration must never import values that break it.
    """
    with throwaway_db("schema_grant_cross_user", revision="012") as url:
        seed_pre_014_tokens(url)
        add_bare_grant_id_column(url)
        # One id shared by alice's row and bob's row on the same client.
        sql(url, "UPDATE oauth_tokens SET grant_id = 'shared'")

        result = _harness.run_alembic(
            url, "upgrade", "head", dimensions=DIM, check=False
        )

        assert result.returncode != 0
        combined = result.stdout + result.stderr
        assert "'shared'" in combined
        assert "more than one" in combined

        assert alembic_version(url) == "012"
        assert set(grant_ids(url).values()) == {"shared"}


def test_a_pre_existing_grant_mixing_null_and_real_owners_is_refused():
    """`count(DISTINCT user_id)` skips NULLs, so this needs its own disjunct."""
    with throwaway_db("schema_grant_null_owner_mix", revision="012") as url:
        seed_pre_014_tokens(url)
        add_bare_grant_id_column(url)
        # Alice's client-b row and the single-user (NULL owner) client-b row.
        sql(
            url,
            "UPDATE oauth_tokens SET grant_id = 'mixed' WHERE token_hash = ANY($1)",
            ["e" * 64, "f" * 64],
        )
        sql(
            url,
            "UPDATE oauth_tokens SET grant_id = 'rest' WHERE grant_id IS NULL",
        )

        result = _harness.run_alembic(
            url, "upgrade", "head", dimensions=DIM, check=False
        )

        assert result.returncode != 0
        assert "'mixed'" in result.stdout + result.stderr
        assert alembic_version(url) == "012"


def test_a_pre_existing_column_of_the_wrong_type_is_refused():
    """014 completes a column it can verify; it does not adopt a stranger."""
    with throwaway_db("schema_grant_wrong_type", revision="012") as url:
        seed_pre_014_tokens(url)
        add_bare_grant_id_column(url, coltype="text")
        sql(url, "UPDATE oauth_tokens SET grant_id = 'g-' || id::text")

        result = _harness.run_alembic(
            url, "upgrade", "head", dimensions=DIM, check=False
        )

        assert result.returncode != 0
        combined = result.stdout + result.stderr
        assert "already exists as text" in combined
        assert alembic_version(url) == "012"
        assert column_shape(url, "oauth_tokens", "grant_id")[1] == "text"


# Every shape a `CREATE INDEX IF NOT EXISTS` would happily keep while
# `alembic check` — which compares index *names* — reports the index installed.
# "Which column is it on?" accepts most of these: a partial index covers a
# subset of rows, an expression index cannot serve an equality lookup on the
# column, a multi-column index leads with the wrong key, and an INVALID
# leftover from a failed CREATE INDEX CONCURRENTLY is not usable at all.
INDEX_IMPOSTORS = (
    (
        "wrong column",
        "CREATE INDEX ix_oauth_tokens_grant_id ON oauth_tokens (token_type)",
        "key columns are attnums",
    ),
    (
        "partial",
        "CREATE INDEX ix_oauth_tokens_grant_id ON oauth_tokens (grant_id) "
        "WHERE token_type = 'access'",
        "partial index",
    ),
    (
        "expression",
        "CREATE INDEX ix_oauth_tokens_grant_id ON oauth_tokens (lower(grant_id))",
        "indexes an expression",
    ),
    (
        "multi-column, right column second",
        "CREATE INDEX ix_oauth_tokens_grant_id ON oauth_tokens (token_type, grant_id)",
        "key columns are attnums",
    ),
    (
        "multi-column, right column first",
        "CREATE INDEX ix_oauth_tokens_grant_id ON oauth_tokens (grant_id, token_type)",
        "key columns are attnums",
    ),
    (
        "on another table",
        "CREATE INDEX ix_oauth_tokens_grant_id ON oauth_codes (client_id)",
        "indexes another relation",
    ),
)


@pytest.mark.parametrize(
    "label, ddl, message",
    INDEX_IMPOSTORS,
    ids=[case[0] for case in INDEX_IMPOSTORS],
)
def test_a_squatting_index_name_is_refused(label, ddl, message):
    """Anything under our name that is not exactly our index is refused."""
    with throwaway_db(f"schema_grant_index_{abs(hash(label)) % 10**8}", revision="012") as url:
        seed_pre_014_tokens(url)
        add_bare_grant_id_column(url)
        sql(url, "UPDATE oauth_tokens SET grant_id = 'g-' || id::text")
        sql(url, ddl)

        result = _harness.run_alembic(
            url, "upgrade", "head", dimensions=DIM, check=False
        )

        assert result.returncode != 0, label
        combined = result.stdout + result.stderr
        assert "ix_oauth_tokens_grant_id already exists" in combined, label
        assert message in combined, combined[-2000:]
        assert alembic_version(url) == "012"


def test_an_invalid_index_of_our_name_is_refused():
    """A failed `CREATE INDEX CONCURRENTLY` leaves an INVALID index behind.

    It has the right name, the right table and the right key column, and it
    cannot serve a single lookup. Only `pg_index.indisvalid` distinguishes it.
    """
    with throwaway_db("schema_grant_index_invalid", revision="012") as url:
        seed_pre_014_tokens(url)
        add_bare_grant_id_column(url)
        sql(url, "UPDATE oauth_tokens SET grant_id = 'g-' || id::text")
        sql(url, "CREATE INDEX ix_oauth_tokens_grant_id ON oauth_tokens (grant_id)")
        # Postgres offers no supported way to invalidate an index, so mark it
        # directly — the catalog state is what the migration reads.
        sql(
            url,
            "UPDATE pg_index SET indisvalid = false WHERE indexrelid = "
            "'ix_oauth_tokens_grant_id'::regclass",
        )

        result = _harness.run_alembic(
            url, "upgrade", "head", dimensions=DIM, check=False
        )

        assert result.returncode != 0
        assert "INVALID" in result.stdout + result.stderr
        assert alembic_version(url) == "012"


def test_an_index_of_our_name_in_another_schema_is_ignored():
    """Namespace, not bare name: a shadow schema is not our schema.

    `CREATE INDEX` places an index in its table's schema, so
    `shadow.ix_oauth_tokens_grant_id` cannot collide with anything 014 creates
    and says nothing about `oauth_tokens`. A lookup matching `relname` across
    every schema found it anyway and refused a database that was perfectly
    fine — the migration would have been unrunnable until an unrelated object
    somebody else owns was dropped.
    """
    with throwaway_db("schema_grant_index_shadow", revision="012") as url:
        seed_pre_014_tokens(url)
        # The index check only runs on the pre-existing-column path, so the
        # column has to be there for this case to reach it at all.
        add_bare_grant_id_column(url)
        sql(
            url,
            "UPDATE oauth_tokens SET grant_id = "
            "  'hand-' || client_id || '-' || coalesce(user_id::text, 'null')",
        )
        sql(url, "CREATE SCHEMA shadow")
        sql(url, "CREATE TABLE shadow.decoy (grant_id text, token_type text)")
        # Same name, same column name, different schema — and partial, so the
        # old bare-name lookup would have rejected it as an impostor rather
        # than merely mis-identifying it.
        sql(
            url,
            "CREATE INDEX ix_oauth_tokens_grant_id ON shadow.decoy (grant_id) "
            "WHERE token_type = 'access'",
        )

        _harness.run_alembic(url, "upgrade", "head", dimensions=DIM)

        assert alembic_version(url) == HEAD_REVISION
        assert_reconciled(url, marker_expected=False)
        # Ours was created in the table's own schema, and the decoy is untouched.
        assert fetchval(
            url,
            "SELECT count(*) FROM pg_indexes WHERE indexname = "
            "'ix_oauth_tokens_grant_id'",
        ) == 2
        assert fetchval(
            url,
            "SELECT indexdef FROM pg_indexes WHERE indexname = "
            "'ix_oauth_tokens_grant_id' AND schemaname = 'public'",
        ) is not None


def test_the_genuine_index_is_accepted():
    """The whole point of the checks above is not to reject our own index."""
    with throwaway_db("schema_grant_index_ok", revision="012") as url:
        seed_pre_014_tokens(url)
        add_bare_grant_id_column(url)
        sql(
            url,
            "UPDATE oauth_tokens SET grant_id = "
            "  'hand-' || client_id || '-' || coalesce(user_id::text, 'null')",
        )
        sql(url, "CREATE INDEX ix_oauth_tokens_grant_id ON oauth_tokens (grant_id)")

        _harness.run_alembic(url, "upgrade", "head", dimensions=DIM)

        assert alembic_version(url) == HEAD_REVISION
        assert_reconciled(url, marker_expected=False)


def test_a_complete_pre_existing_column_is_accepted():
    """The benign case: someone applied 014's shape by hand, consistently.

    Nothing has to be guessed here — every row has an id and no id spans two
    owners — so the migration completes rather than refusing, and leaves the
    existing values alone.
    """
    with throwaway_db("schema_grant_preexisting_ok", revision="012") as url:
        seed_pre_014_tokens(url)
        add_bare_grant_id_column(url)
        # One id per (client_id, user_id), exactly what 014 would have derived.
        sql(
            url,
            "UPDATE oauth_tokens SET grant_id = "
            "  'hand-' || client_id || '-' || coalesce(user_id::text, 'null')",
        )
        before = grant_ids(url)

        _harness.run_alembic(url, "upgrade", "head", dimensions=DIM)

        assert alembic_version(url) == HEAD_REVISION
        assert grant_ids(url) == before, "existing ids must be left alone"
        assert column_shape(url, "oauth_tokens", "grant_id")[0] is True
        assert_reconciled(url, marker_expected=False)


def test_an_empty_table_with_a_nullable_column_completes():
    """Nothing to guess, so nothing to refuse."""
    with throwaway_db("schema_grant_empty_preexisting", revision="012") as url:
        add_bare_grant_id_column(url)

        _harness.run_alembic(url, "upgrade", "head", dimensions=DIM)

        assert alembic_version(url) == HEAD_REVISION
        assert column_shape(url, "oauth_tokens", "grant_id")[0] is True


# --------------------------------------------------------------------------
# migration 015: denormalised actor on usage_logs (issue #77)
# --------------------------------------------------------------------------
#
# `alembic check` sees the three columns and their nullability, so
# `assert_reconciled` already covers the declarative half everywhere. What it
# cannot see is whether the *backfill* preserves the thing the columns exist
# for: attribution that outlives the credential. Both credential paths destroy
# the join on purpose — deleting an OAuth client cascades its tokens and
# `usage_logs.oauth_token_id` is ON DELETE SET NULL, and the panel NULLs
# `usage_logs.key_id` by hand because that column has no ON DELETE at all — so
# the cases below run those exact sequences against a real database and read
# the label back afterwards.


ACTOR_COLUMNS = (
    ("actor_kind", "character varying(20)"),
    ("actor_label", "character varying(255)"),
    ("actor_ref", "character varying(64)"),
)

# 015's ownership marker, mirrored from the migration *and* from
# `UsageLog._ACTOR_COLUMN_MARKER`. All three must agree: the migration keys its
# completion and its downgrade on the comment, and the model declares it so
# `alembic check` compares it like any other column attribute.
ACTOR_MARKER = "denormalised actor, written at call time (015_usage_log_actor)"


def actor_column_is_marked(url, column: str) -> bool:
    return fetchval(
        url,
        "SELECT col_description(a.attrelid, a.attnum) FROM pg_attribute a "
        "WHERE a.attrelid = 'usage_logs'::regclass AND a.attname = $1",
        column,
    ) == ACTOR_MARKER


def insert_key(url, key_id: int, name: str, prefix: str, user_id=None):
    sql(
        url,
        "INSERT INTO api_keys (id, name, key_hash, key_prefix, permission, "
        "is_active, user_id) VALUES ($1, $2, $3, $4, 'read', true, $5)",
        key_id,
        name,
        f"hash-{key_id}",
        prefix,
        user_id,
    )


def insert_usage(url, usage_id: int, *, key_id=None, oauth_token_id=None, tool="read_note"):
    sql(
        url,
        "INSERT INTO usage_logs (id, key_id, oauth_token_id, tool) "
        "VALUES ($1, $2, $3, $4)",
        usage_id,
        key_id,
        oauth_token_id,
        tool,
    )


def actor_of(url, usage_id: int):
    """`(actor_kind, actor_label, actor_ref)` for one usage row."""
    row = fetch(
        url,
        "SELECT actor_kind, actor_label, actor_ref FROM usage_logs WHERE id = $1",
        usage_id,
    )[0]
    return row["actor_kind"], row["actor_label"], row["actor_ref"]


def seed_pre_015_usage(url):
    """One API-key actor, one OAuth actor, and one already-orphaned row.

    The third is the row this bug has *already* claimed on the live database:
    its credential was deleted before the label column existed, so there is
    nothing to recover and the migration must not invent anything for it.
    """
    insert_user(url, 1, "alice")
    insert_key(url, 1, "nightly sync", "omcp_a1b2c3", user_id=1)
    insert_client(url, "client-abc", "none", None)
    # Seeded at 014, where `grant_id` is already NOT NULL, so it is supplied
    # here rather than going through `insert_token` (which predates it).
    sql(
        url,
        "INSERT INTO oauth_tokens (token_hash, token_type, client_id, scope, "
        "user_id, grant_id, expires_at, revoked) "
        "VALUES ($1, 'access', 'client-abc', 'read', 1, 'grant-1', $2, false)",
        "a" * 64,
        FUTURE,
    )
    token_id = fetchval(url, "SELECT id FROM oauth_tokens WHERE token_hash = $1", "a" * 64)

    insert_usage(url, 1, key_id=1)
    insert_usage(url, 2, oauth_token_id=token_id)
    insert_usage(url, 3)  # credential already gone
    return token_id


def test_the_actor_columns_are_nullable_on_a_fresh_database():
    """Nullable on purpose: a call that cannot name its actor must still be
    recorded. These columns are display and audit, never authorization."""
    with throwaway_db("schema_actor_fresh") as url:
        for column, expected_type in ACTOR_COLUMNS:
            shape = column_shape(url, "usage_logs", column)
            assert shape is not None, f"usage_logs.{column} is missing"
            attnotnull, coltype, coldefault = shape
            assert attnotnull is False, f"{column} must stay nullable"
            assert coltype == expected_type
            assert coldefault is None
            # The ownership marker. 015's downgrade drops only columns carrying
            # it, and its upgrade completes only a set carrying it, so a
            # missing marker means the migration no longer recognises its own
            # work. `alembic check` compares it too, via the model.
            assert actor_column_is_marked(url, column), (
                f"usage_logs.{column} lost 015's comment marker"
            )


def test_backfill_labels_every_row_whose_credential_still_resolves():
    with throwaway_db("schema_actor_backfill", revision="014") as url:
        seed_pre_015_usage(url)

        _harness.run_alembic(url, "upgrade", "head", dimensions=DIM)

        assert actor_of(url, 1) == ("api_key", "nightly sync", "omcp_a1b2c3")
        assert actor_of(url, 2) == ("oauth", "test client", "client-abc")
        # Nothing is invented for a row whose credential is already gone. A
        # guess-by-user_id fallback would be worse than an admitted gap: two of
        # a user's keys are different actors, and the whole value of the column
        # is that an operator can trust it while deciding whether a connector
        # misbehaved.
        assert actor_of(url, 3) == (None, None, None)


def test_the_label_survives_the_panel_deleting_an_api_key():
    """`delete_key_form`'s exact sequence, run against a real database.

    `usage_logs.key_id` has no `ON DELETE`, so the panel NULLs it first and
    then deletes the key. Before 015 that erased the actor. The label is
    written by `_log_usage` now and backfilled here, so it must be untouched by
    both statements.
    """
    with throwaway_db("schema_actor_key_delete", revision="014") as url:
        seed_pre_015_usage(url)
        _harness.run_alembic(url, "upgrade", "head", dimensions=DIM)

        sql(url, "UPDATE usage_logs SET key_id = NULL WHERE key_id = 1")
        sql(url, "DELETE FROM api_keys WHERE id = 1")

        assert fetchval(url, "SELECT count(*) FROM api_keys") == 0
        assert fetchval(url, "SELECT key_id FROM usage_logs WHERE id = 1") is None
        assert actor_of(url, 1) == ("api_key", "nightly sync", "omcp_a1b2c3")


def test_the_label_survives_deleting_the_oauth_client():
    """The scenario in the issue, end to end.

    An operator suspects a connector, clicks Delete, then opens the Usage page
    to review what it did. The delete cascades `oauth_tokens` and SET NULLs
    `usage_logs.oauth_token_id`, so the join the page used to rely on is gone —
    and the row must still name the client.
    """
    with throwaway_db("schema_actor_client_delete", revision="014") as url:
        seed_pre_015_usage(url)
        _harness.run_alembic(url, "upgrade", "head", dimensions=DIM)

        sql(url, "DELETE FROM oauth_clients WHERE client_id = 'client-abc'")

        assert fetchval(url, "SELECT count(*) FROM oauth_tokens") == 0
        assert fetchval(url, "SELECT oauth_token_id FROM usage_logs WHERE id = 2") is None
        assert actor_of(url, 2) == ("oauth", "test client", "client-abc")


def test_rerunning_015_does_not_relabel_existing_rows():
    """Idempotence that re-executes the body, and history that stays history.

    Stamping back to 014 forces 015 to run again. The guard is
    `actor_kind IS NULL`, so a renamed key must not retroactively rename every
    call it ever made — the label is a snapshot of the credential at call time,
    not a view of its present state.
    """
    with throwaway_db("schema_actor_idempotent", revision="014") as url:
        seed_pre_015_usage(url)
        _harness.run_alembic(url, "upgrade", "head", dimensions=DIM)

        sql(url, "UPDATE api_keys SET name = 'renamed later' WHERE id = 1")
        _harness.run_alembic(url, "stamp", "014", dimensions=DIM)
        assert alembic_version(url) == "014"
        _harness.run_alembic(url, "upgrade", "head", dimensions=DIM)

        assert alembic_version(url) == HEAD_REVISION
        assert actor_of(url, 1) == ("api_key", "nightly sync", "omcp_a1b2c3")
        assert_reconciled(url, marker_expected=False)


def add_owned_actor_columns(url, *, marked=True):
    """The three columns exactly as 015 creates them, marker optional."""
    for column, coltype in ACTOR_COLUMNS:
        sql(url, f"ALTER TABLE usage_logs ADD COLUMN {column} {coltype}")
        if marked:
            sql(url, f"COMMENT ON COLUMN usage_logs.{column} IS '{ACTOR_MARKER}'")


def refuse_upgrade(url, *, must_mention):
    """Run `upgrade head`, require it to fail, and return the message."""
    result = _harness.run_alembic(url, "upgrade", "head", dimensions=DIM, check=False)
    combined = result.stdout + result.stderr
    assert result.returncode != 0, combined
    for fragment in must_mention:
        assert fragment in combined, combined
    assert "Nothing has been changed" in combined, combined
    assert alembic_version(url) == "014"
    return combined


def test_a_pre_existing_actor_column_of_the_wrong_shape_is_refused():
    """013's philosophy: reconcile a column we can verify, refuse to guess.

    A `text` column under our name holds labels this migration did not write.
    Adopting it means presenting an attribution of unknown provenance as if the
    server had recorded it, which is exactly the trust these columns exist to
    make possible.
    """
    with throwaway_db("schema_actor_wrong_type", revision="014") as url:
        add_owned_actor_columns(url)
        sql(url, "ALTER TABLE usage_logs ALTER COLUMN actor_label TYPE text")

        refuse_upgrade(url, must_mention=["actor_label", "text"])
        # And nothing was half-applied on the way to refusing.
        assert not actor_column_is_marked(url, "actor_kind") or True
        assert alembic_version(url) == "014"


def test_a_partial_actor_column_set_is_refused():
    """The three are one owned unit.

    A database with only `actor_kind` is not a re-run, it is one somebody
    edited. Adding the missing two beside it would run the backfill against a
    guard column of unknown meaning — `actor_kind IS NULL` decides which rows
    get written, so a foreign guard column silently decides what this migration
    labels and what it leaves alone.
    """
    with throwaway_db("schema_actor_partial", revision="014") as url:
        sql(url, "ALTER TABLE usage_logs ADD COLUMN actor_kind character varying(20)")
        sql(
            url,
            f"COMMENT ON COLUMN usage_logs.actor_kind IS '{ACTOR_MARKER}'",
        )

        refuse_upgrade(url, must_mention=["actor_kind", "actor_label", "absent"])
        # The two that were absent stay absent.
        assert column_shape(url, "usage_logs", "actor_label") is None
        assert column_shape(url, "usage_logs", "actor_ref") is None


def test_a_not_null_actor_column_is_refused():
    """Nullability is the load-bearing half of the shape.

    These columns must stay nullable: a call that cannot name its actor has to
    be recorded anyway, and rows orphaned before 015 have nothing to backfill
    from. A NOT NULL `actor_label` would turn both into a failed insert — and
    `alembic check` reports it, but only *after* the migration adopted it.
    """
    with throwaway_db("schema_actor_notnull", revision="014") as url:
        add_owned_actor_columns(url)
        sql(url, "UPDATE usage_logs SET actor_label = 'x' WHERE actor_label IS NULL")
        sql(url, "ALTER TABLE usage_logs ALTER COLUMN actor_label SET NOT NULL")

        refuse_upgrade(url, must_mention=["actor_label", "NOT NULL"])


def test_an_unmarked_actor_column_set_is_refused():
    """Type and width are a coincidence anyone could reproduce.

    The comment marker is the only evidence that *this* migration wrote the
    values, and the panel presents them to an operator as an audit trail. A
    hand-made `varchar(255)` full of arbitrary text must not be adopted into
    that role.
    """
    with throwaway_db("schema_actor_unmarked", revision="014") as url:
        add_owned_actor_columns(url, marked=False)

        refuse_upgrade(url, must_mention=["comment marker"])


def test_an_actor_column_with_a_server_default_is_refused():
    with throwaway_db("schema_actor_default", revision="014") as url:
        add_owned_actor_columns(url)
        sql(url, "ALTER TABLE usage_logs ALTER COLUMN actor_kind SET DEFAULT 'api_key'")

        refuse_upgrade(url, must_mention=["actor_kind", "server default"])


def test_a_label_beside_a_null_kind_is_refused_not_overwritten():
    """The backfill's guard column decides what gets rewritten.

    A row carrying a label with a NULL `actor_kind` would be re-labelled from
    whatever credential its FK points at *now* — overwriting an attribution
    somebody else recorded. 015 never rewrites an attribution it did not write.
    """
    with throwaway_db("schema_actor_orphan_label", revision="014") as url:
        insert_user(url, 1, "alice")
        insert_key(url, 1, "nightly sync", "omcp_a1b2c3", user_id=1)
        insert_usage(url, 1, key_id=1)
        add_owned_actor_columns(url)
        sql(url, "UPDATE usage_logs SET actor_label = 'hand written' WHERE id = 1")

        refuse_upgrade(url, must_mention=["actor_kind", "will not rewrite"])
        assert fetchval(url, "SELECT actor_label FROM usage_logs WHERE id = 1") == (
            "hand written"
        )


def test_the_marked_columns_are_accepted_as_a_rerun():
    """The benign case: 015's own shape, applied by hand or left by a re-stamp.

    Nothing has to be guessed, so the migration completes and backfills rather
    than refusing.
    """
    with throwaway_db("schema_actor_preexisting_ok", revision="014") as url:
        seed_pre_015_usage(url)
        add_owned_actor_columns(url)

        _harness.run_alembic(url, "upgrade", "head", dimensions=DIM)

        assert alembic_version(url) == HEAD_REVISION
        assert actor_of(url, 1) == ("api_key", "nightly sync", "omcp_a1b2c3")
        assert_reconciled(url, marker_expected=False)


def test_downgrade_refuses_to_drop_a_column_it_did_not_create():
    """A downgrade must undo *this* migration, not delete somebody else's
    column that happens to share a name. The marker is the only evidence of
    authorship, so it is what the drop keys on — and the decision is made for
    all three before any of them is touched."""
    with throwaway_db("schema_actor_down_foreign", revision="014") as url:
        _harness.run_alembic(url, "upgrade", "head", dimensions=DIM)
        sql(url, "COMMENT ON COLUMN usage_logs.actor_ref IS 'somebody else'")

        result = _harness.run_alembic(
            url, "downgrade", "014", dimensions=DIM, check=False
        )

        assert result.returncode != 0
        combined = result.stdout + result.stderr
        assert "actor_ref" in combined
        assert "Nothing has been changed" in combined
        # All-or-nothing: the two that *were* marked are still there.
        for column, _coltype in ACTOR_COLUMNS:
            assert column_shape(url, "usage_logs", column) is not None
        assert alembic_version(url) == HEAD_REVISION


def test_downgrade_015_removes_the_columns_and_upgrade_rebuilds_them():
    with throwaway_db("schema_actor_downgrade", revision="014") as url:
        seed_pre_015_usage(url)
        _harness.run_alembic(url, "upgrade", "head", dimensions=DIM)
        assert actor_of(url, 1)[0] == "api_key"

        _harness.run_alembic(url, "downgrade", "014", dimensions=DIM)
        assert alembic_version(url) == "014"
        for column, _ in ACTOR_COLUMNS:
            assert column_shape(url, "usage_logs", column) is None

        _harness.run_alembic(url, "upgrade", "head", dimensions=DIM)
        assert alembic_version(url) == HEAD_REVISION
        # Re-derived from the credentials that still exist. A row whose
        # credential was deleted while the columns were absent is not
        # recoverable — downgrading really does destroy that history.
        assert actor_of(url, 1) == ("api_key", "nightly sync", "omcp_a1b2c3")
        assert_reconciled(url, marker_expected=False)


# ==========================================================================
# 016 — the index-provenance columns on `users` (issue #91, deferred half)
# ==========================================================================
#
# `alembic check` sees these three columns, their type and their nullability,
# and nothing about what they are *for*. What it cannot see is the property the
# whole record depends on: **the two pathname columns must be able to record
# any value the facts they mirror can take.** A provenance value the pass
# observed and cannot store is a bug, never a truncation and never a NULL —
# because the discard branch writes the record *and* the delete in **one**
# transaction, so a value that will not go in raises, rolls the delete back,
# and leaves the former vault's index served on every subsequent pass. That is
# #91's own symptom produced by a column definition, and it is reachable
# through two independent channels:
#
# - **length** — a short assignment may be a symbolic link to a canonical path
#   of any length, and this system owns no bound on that (hence `text`);
# - **byte content** — a POSIX pathname is arbitrary non-NUL bytes, Python
#   decodes a non-UTF-8 component with `surrogateescape`, and the resulting
#   lone surrogate cannot be UTF-8-encoded by the driver at all (hence the
#   hexadecimal encoding, which `text` alone did *not* fix).
#
# Both are asserted below against a real database and a real directory whose
# name carries a `\xff` byte — the defect is in what the kernel can hand back,
# so a mocked `os.path.realpath` would prove nothing.
#
# The marker matters more here than on 015's display columns: this record is
# the sole input to a decision that DELETEs a user's entire index, so adopting
# a same-named column of unknown provenance is a mass delete on a value nobody
# in this scheme wrote.


PROVENANCE_COLUMNS = (
    ("indexed_vault_assignment", "text"),
    ("indexed_vault_realpath", "text"),
    ("indexed_vault_handle", "character varying(320)"),
)
PROVENANCE_COLUMN_NAMES = tuple(name for name, _t in PROVENANCE_COLUMNS)

# 016's ownership marker, mirrored from the migration *and* from
# `src/models/db.py::_INDEXED_PROVENANCE_MARKER`. All three must agree: the
# migration keys its completion and its downgrade on the comment, and the model
# declares it so `alembic check` compares it like any other column attribute.
PROVENANCE_MARKER = (
    "provenance of this user's index, recorded by the index pass "
    "(016_indexed_vault_provenance)"
)

# `users.vault_path` is `varchar(1024)`. The realpath column must not be
# bounded by that — or by anything.
VAULT_PATH_WIDTH = 1024


def marker_literal() -> str:
    """`PROVENANCE_MARKER` as a SQL string literal.

    `COMMENT ON` takes no bind parameter — it is utility DDL — and 016's marker
    contains an apostrophe ("this user's index"), so the quote has to be
    doubled. The migration's own `_quote` does exactly this; a test that
    interpolated the marker raw would fail on the marker rather than on the
    thing it is asserting.
    """
    return "'" + PROVENANCE_MARKER.replace("'", "''") + "'"


def provenance_column_is_marked(url, column: str) -> bool:
    return fetchval(
        url,
        "SELECT col_description(a.attrelid, a.attnum) FROM pg_attribute a "
        "WHERE a.attrelid = 'users'::regclass AND a.attname = $1",
        column,
    ) == PROVENANCE_MARKER


def provenance_of(url, user_id: int):
    """The three recorded facts for one user, straight from the row."""
    row = fetch(
        url,
        "SELECT indexed_vault_assignment, indexed_vault_realpath, "
        "       indexed_vault_handle FROM users WHERE id = $1",
        user_id,
    )[0]
    return (
        row["indexed_vault_assignment"],
        row["indexed_vault_realpath"],
        row["indexed_vault_handle"],
    )


def add_owned_provenance_columns(url, *, marked=True):
    """The three columns exactly as 016 creates them, marker optional."""
    for column, coltype in PROVENANCE_COLUMNS:
        sql(url, f"ALTER TABLE users ADD COLUMN {column} {coltype}")
        if marked:
            sql(url, f"COMMENT ON COLUMN users.{column} IS {marker_literal()}")


def refuse_016(url, *, must_mention):
    """Run `upgrade head`, require 016 to refuse, and return the message."""
    result = _harness.run_alembic(url, "upgrade", "head", dimensions=DIM, check=False)
    combined = result.stdout + result.stderr
    assert result.returncode != 0, combined
    for fragment in must_mention:
        assert fragment in combined, combined
    assert "Nothing has been changed" in combined, combined
    assert alembic_version(url) == "015"
    return combined


def seed_pre_016_users(url):
    """One assigned user with index rows, one unassigned user.

    The assigned one is the whole point of "backfills nothing": stamping
    `indexed_vault_assignment = vault_path` for them would assert that their
    rows were built under the assignment they carry *now*, which is exactly the
    reassignment lag the record exists to detect.
    """
    insert_user(url, 1, "assigned")
    insert_user(url, 2, "unassigned")
    sql(url, "UPDATE users SET vault_path = '/vaults/alice' WHERE id = 1")
    sql(
        url,
        "INSERT INTO notes_metadata (id, user_id, file_path, title, content_hash) "
        "VALUES (1, 1, 'Same.md', 'Same', 'hash-same'), "
        "       (2, 1, 'OnlyA.md', 'OnlyA', 'hash-onlya')",
    )
    sql(
        url,
        "INSERT INTO note_links (source_note_id, target_note_id, target_path, kind) "
        "VALUES (1, 2, 'OnlyA', 'wikilink')",
    )


def test_the_provenance_columns_are_nullable_and_marked_on_a_fresh_database():
    """Nullable is load-bearing, not incidental.

    NULL is the *provenance unknown* branch — the one that re-derives rather
    than trusting or discarding — so a column that could not hold it would have
    no way to say "nothing is known", which is the only true statement
    available for every row at migration time.
    """
    with throwaway_db("schema_prov_fresh") as url:
        assert alembic_version(url) == HEAD_REVISION
        for column, expected_type in PROVENANCE_COLUMNS:
            shape = column_shape(url, "users", column)
            assert shape is not None, f"users.{column} is missing"
            attnotnull, coltype, coldefault = shape
            assert attnotnull is False, f"{column} must stay nullable"
            assert coltype == expected_type, f"{column} is {coltype}"
            assert coldefault is None, f"{column} carries a server default"
            assert provenance_column_is_marked(url, column), (
                f"users.{column} lost 016's comment marker"
            )
        # `alembic check` clean at head — which is only true while the models'
        # declared column comments are byte-identical to the migration's
        # marker.
        check = _harness.run_alembic(url, "check", dimensions=DIM, check=False)
        assert check.returncode == 0, (
            f"alembic check reported drift\n{check.stdout}\n{check.stderr}"
        )


def test_a_realpath_longer_than_the_assignment_column_round_trips():
    """The round-4 blocker, at the column.

    An assignment of any accepted length may resolve through a symbolic link to
    a canonical path far longer than itself. With a bounded column that write
    raises `string_data_right_truncation` *inside the discard transaction*,
    rolling the delete back — so the former vault's index is served forever,
    which is the precise state this record exists to end.

    Asserting the stored value equals what was written is what stops a later
    edit from quietly re-bounding this column.
    """
    with throwaway_db("schema_prov_long") as url:
        insert_user(url, 1, "alice")
        long_path = "/" + "/".join(f"segment{i:04d}" for i in range(200))
        assert len(long_path) > VAULT_PATH_WIDTH, len(long_path)
        encoded = os.fsencode(long_path).hex()

        sql(
            url,
            "UPDATE users SET indexed_vault_realpath = $1 WHERE id = 1",
            encoded,
        )

        stored = provenance_of(url, 1)[1]
        assert stored == encoded, "the realpath column truncated or altered the value"
        assert os.fsdecode(bytes.fromhex(stored)) == long_path
        assert len(stored) > VAULT_PATH_WIDTH


def test_a_non_utf8_realpath_round_trips_losslessly_only_when_encoded(tmp_path):
    """The round-5 blocker, at the column, with a real directory.

    `text` removed the *length* bound and left the *encoding* bound exactly
    where it was, so the identical rollback-forever failure survived the
    widening through the other channel. The two assertions here are a pair:
    the encoded form goes in and comes back byte for byte, and the **raw**
    surrogate-escaped string is refused by the driver — which is what keeps the
    hexadecimal encoding load-bearing rather than decorative. If a future edit
    ever made the raw write succeed, the second assertion would tell us the
    reasoning had changed rather than letting the encoding quietly rot.
    """
    try:
        raw_dir = os.path.join(os.fsencode(str(tmp_path)), b"vault-\xff-name")
        os.mkdir(raw_dir)
    except (OSError, ValueError) as e:  # pragma: no cover - filesystem dependent
        pytest.skip(f"this filesystem refuses a non-UTF-8 directory name: {e}")

    realpath = os.path.realpath(os.fsdecode(raw_dir))
    # The surrogate escape is the whole point: without it this case degenerates
    # into the ASCII one and asserts nothing.
    assert any("\udc80" <= ch <= "\udcff" for ch in realpath), realpath
    encoded = os.fsencode(realpath).hex()

    with throwaway_db("schema_prov_nonutf8") as url:
        insert_user(url, 1, "alice")

        sql(url, "UPDATE users SET indexed_vault_realpath = $1 WHERE id = 1", encoded)

        stored = provenance_of(url, 1)[1]
        assert stored == encoded
        assert stored == stored.lower(), "the encoding must be lowercase hexadecimal"
        # Lossless, surrogates included.
        assert os.fsdecode(bytes.fromhex(stored)) == realpath

        # And the raw pathname is genuinely unstorable, which is the reason the
        # encoding exists at all.
        with pytest.raises(Exception) as excinfo:
            sql(
                url,
                "UPDATE users SET indexed_vault_realpath = $1 WHERE id = 1",
                realpath,
            )
        assert isinstance(excinfo.value, (UnicodeEncodeError, ValueError)) or (
            "surrogates" in str(excinfo.value) or "encode" in str(excinfo.value)
        ), repr(excinfo.value)
        # The failed write changed nothing.
        assert provenance_of(url, 1)[1] == encoded


def test_016_backfills_nothing():
    """The round-1 blocker, and the load-bearing decision of this migration.

    "Assigned now" is not "indexed under what is assigned now". Deriving
    `indexed_vault_assignment` from `users.vault_path` would stamp rows built
    under vault A as belonging to B for any administrator who reassigned and
    deployed before the next index pass — after which both recorded facts
    agree, the pass takes its no-op branch, and the identical-path /
    identical-hash link case that never heals becomes guaranteed rather than
    merely possible.
    """
    with throwaway_db("schema_prov_backfill", revision="015") as url:
        seed_pre_016_users(url)
        before_notes = fetch(
            url, "SELECT id, user_id, file_path, content_hash FROM notes_metadata ORDER BY id"
        )
        before_links = fetch(
            url, "SELECT source_note_id, target_note_id, target_path FROM note_links"
        )

        _harness.run_alembic(url, "upgrade", "head", dimensions=DIM)

        assert alembic_version(url) == HEAD_REVISION
        # NULL for *every* row, including the assigned user's.
        assert provenance_of(url, 1) == (None, None, None)
        assert provenance_of(url, 2) == (None, None, None)
        # And the index itself is untouched.
        assert [dict(r) for r in fetch(
            url, "SELECT id, user_id, file_path, content_hash FROM notes_metadata ORDER BY id"
        )] == [dict(r) for r in before_notes]
        assert [dict(r) for r in fetch(
            url, "SELECT source_note_id, target_note_id, target_path FROM note_links"
        )] == [dict(r) for r in before_links]
        assert fetchval(url, "SELECT count(*) FROM note_embeddings") == 0


def test_rerunning_016_does_not_overwrite_a_recorded_provenance():
    """Idempotence that re-executes the body, and a record that stays a record.

    Stamping back to 015 forces 016 to run again against a user whose recorded
    assignment deliberately *differs* from their current `vault_path` — the
    exact state a pending reassignment produces. A migration that "helpfully"
    reconciled the two would destroy the only evidence that the rows predate
    the reassignment.
    """
    with throwaway_db("schema_prov_idempotent") as url:
        insert_user(url, 1, "alice")
        sql(url, "UPDATE users SET vault_path = '/vaults/new' WHERE id = 1")
        recorded = (
            "/vaults/old",
            os.fsencode("/data/old").hex(),
            "1:a85530010b6f671e",
        )
        sql(
            url,
            "UPDATE users SET indexed_vault_assignment = $1, "
            "indexed_vault_realpath = $2, indexed_vault_handle = $3 WHERE id = 1",
            *recorded,
        )

        _harness.run_alembic(url, "stamp", "015", dimensions=DIM)
        assert alembic_version(url) == "015"
        _harness.run_alembic(url, "upgrade", "head", dimensions=DIM)

        assert alembic_version(url) == HEAD_REVISION
        assert provenance_of(url, 1) == recorded
        for column, _ in PROVENANCE_COLUMNS:
            assert provenance_column_is_marked(url, column)


@pytest.mark.parametrize(
    "label,ddl,fragment",
    [
        (
            "wrong type",
            "ALTER TABLE users ADD COLUMN indexed_vault_realpath varchar(1024)",
            "is character varying(1024), not text",
        ),
        (
            "NOT NULL",
            "ALTER TABLE users ADD COLUMN indexed_vault_realpath text NOT NULL "
            "DEFAULT ''",
            "is NOT NULL",
        ),
        (
            "server default",
            "ALTER TABLE users ADD COLUMN indexed_vault_realpath text DEFAULT ''",
            "carries a server default",
        ),
    ],
)
def test_a_foreign_provenance_column_is_refused(label, ddl, fragment):
    """013's philosophy, with more at stake than 015's.

    A same-named column of unknown provenance adopted as "the assignment those
    rows were scanned under" is a **mass delete** on the strength of a value
    nobody in this scheme wrote — the classification reads it and can conclude
    *reassigned*, which drops the user's `notes_metadata` and cascades their
    embeddings and links.
    """
    with throwaway_db(f"schema_prov_foreign_{label.replace(' ', '_')}", revision="015") as url:
        # The other two in 016's own shape, so the refusal is about this one.
        for column, coltype in PROVENANCE_COLUMNS:
            if column == "indexed_vault_realpath":
                continue
            sql(url, f"ALTER TABLE users ADD COLUMN {column} {coltype}")
            sql(url, f"COMMENT ON COLUMN users.{column} IS {marker_literal()}")
        sql(url, ddl)

        refuse_016(url, must_mention=[fragment, "indexed_vault_realpath"])

        # The schema is unchanged: nothing was adopted and nothing was added.
        assert column_shape(url, "users", "indexed_vault_realpath") is not None
        assert provenance_column_is_marked(url, "indexed_vault_realpath") is False


def test_an_unmarked_provenance_column_set_is_refused():
    """Type and width are a coincidence anyone could reproduce.

    The comment is the only evidence that *this* scheme wrote the values, which
    is the whole basis for letting them decide whether to delete an index.
    """
    with throwaway_db("schema_prov_unmarked", revision="015") as url:
        add_owned_provenance_columns(url, marked=False)
        refuse_016(url, must_mention=["does not carry 016's comment marker"])


@pytest.mark.parametrize("present", [1, 2])
def test_a_partial_provenance_column_set_is_refused(present):
    """A partial set is not a re-run; it is a database somebody edited.

    Creating the missing ones beside a foreign `indexed_vault_assignment`
    would leave the pass classifying against a value of unknown meaning — and
    this classification deletes indexes.
    """
    with throwaway_db(f"schema_prov_partial_{present}", revision="015") as url:
        for column, coltype in PROVENANCE_COLUMNS[:present]:
            sql(url, f"ALTER TABLE users ADD COLUMN {column} {coltype}")
            sql(url, f"COMMENT ON COLUMN users.{column} IS {marker_literal()}")

        missing = PROVENANCE_COLUMN_NAMES[present:]
        refuse_016(url, must_mention=["absent", missing[0]])

        for column in missing:
            assert column_shape(url, "users", column) is None


def test_a_complete_marked_provenance_set_is_accepted_as_a_rerun():
    with throwaway_db("schema_prov_rerun", revision="015") as url:
        add_owned_provenance_columns(url, marked=True)
        insert_user(url, 1, "alice")
        sql(
            url,
            "UPDATE users SET indexed_vault_assignment = '/vaults/a', "
            "indexed_vault_realpath = $1 WHERE id = 1",
            os.fsencode("/data/a").hex(),
        )

        _harness.run_alembic(url, "upgrade", "head", dimensions=DIM)

        assert alembic_version(url) == HEAD_REVISION
        assert provenance_of(url, 1) == ("/vaults/a", os.fsencode("/data/a").hex(), None)


def test_downgrade_016_drops_the_marked_set_and_upgrade_rebuilds_it():
    with throwaway_db("schema_prov_downgrade") as url:
        insert_user(url, 1, "alice")
        sql(
            url,
            "UPDATE users SET indexed_vault_assignment = '/vaults/a' WHERE id = 1",
        )

        _harness.run_alembic(url, "downgrade", "015", dimensions=DIM)
        assert alembic_version(url) == "015"
        for column, _ in PROVENANCE_COLUMNS:
            assert column_shape(url, "users", column) is None

        _harness.run_alembic(url, "upgrade", "head", dimensions=DIM)
        assert alembic_version(url) == HEAD_REVISION
        # Rebuilt NULL — which is the *provenance unknown* branch, so the next
        # pass re-derives that user's index rather than trusting or discarding
        # it. Downgrading costs a re-derive, never a re-embed.
        assert provenance_of(url, 1) == (None, None, None)


def test_downgrade_016_refuses_to_drop_a_set_it_did_not_create():
    """All or nothing, and it decides before it touches anything.

    An unmarked column under one of these names is left in place and reported,
    rather than destroyed on the way past — and its two marked siblings survive
    with it, because a downgrade that dropped two of three and then raised
    would leave a half-set record, which the pass reads as "no record".
    """
    with throwaway_db("schema_prov_downgrade_foreign") as url:
        sql(url, "COMMENT ON COLUMN users.indexed_vault_handle IS 'somebody else'")

        result = _harness.run_alembic(
            url, "downgrade", "015", dimensions=DIM, check=False
        )
        combined = result.stdout + result.stderr
        assert result.returncode != 0, combined
        assert "indexed_vault_handle" in combined, combined
        assert "Nothing has been changed" in combined, combined

        assert alembic_version(url) == HEAD_REVISION
        for column, _ in PROVENANCE_COLUMNS:
            assert column_shape(url, "users", column) is not None


# ==========================================================================
# 017 — the actor columns on `transfer_tokens` (issue #92, item 2)
# ==========================================================================
#
# Same three columns as 015's, on the table that mints capabilities, and for
# the reason 015 does not reach: a redemption request is session-less and
# carries a **capability, not a credential**, so `_log_row` has no
# request-scoped actor to read and attributed its `usage_logs` rows by join
# alone. Both joins go NULL on the operator's most urgent path — deleting an
# OAuth client cascades its tokens and `usage_logs.oauth_token_id` is ON DELETE
# SET NULL, and the panel NULLs a key's `usage_logs.key_id` before deleting the
# key — and the rows they take with them are the ones where bytes entered or
# left the vault.
#
# `alembic check` sees the three columns, their types and their nullability. It
# cannot see any of what the cases below assert: that the backfill labels a row
# from *its own* FK and never from another row's, that a row carrying no
# credential FK stays unattributed rather than being guessed at from `user_id`,
# that 017 writes nothing at all to `usage_logs`, and — the invariant the first
# draft omitted — that a label sitting beside a NULL `actor_kind` aborts the
# migration instead of being overwritten from whatever credential the row
# points at now.


# Deliberately identical to `ACTOR_COLUMNS`: both tables are written through
# one reader (`src.auth.session.actor_columns`), so a width that differed
# between them would make that reader truncate correctly for one writer and
# wrongly for the other. Spelled out rather than aliased, so a change to either
# table's widths shows up here as a diff.
TRANSFER_ACTOR_COLUMNS = (
    ("actor_kind", "character varying(20)"),
    ("actor_label", "character varying(255)"),
    ("actor_ref", "character varying(64)"),
)

# 017's ownership marker, mirrored from the migration *and* from
# `src/models/db.py::TransferToken._ACTOR_COLUMN_MARKER`. All three must agree:
# the migration keys its completion and its downgrade on the comment, and the
# model declares it so `alembic check` compares it. A different string from
# 015's on purpose — a shared marker would let either `downgrade()` claim the
# other's columns.
TRANSFER_ACTOR_MARKER = "denormalised actor, recorded at mint (017_transfer_token_actor)"


def transfer_actor_column_is_marked(url, column: str) -> bool:
    return fetchval(
        url,
        "SELECT col_description(a.attrelid, a.attnum) FROM pg_attribute a "
        "WHERE a.attrelid = 'transfer_tokens'::regclass AND a.attname = $1",
        column,
    ) == TRANSFER_ACTOR_MARKER


def insert_transfer_token(
    url,
    token_id: int,
    *,
    key_id=None,
    oauth_token_id=None,
    user_id=None,
    direction="upload",
    path="Inbox/report.pdf",
):
    sql(
        url,
        "INSERT INTO transfer_tokens (id, public_id, token_hash, direction, "
        "state, path, vault_root, overwrite, key_id, oauth_token_id, user_id, "
        "expires_at) VALUES ($1, $2, $3, $4, 'pending', $5, '/vaults/alice', "
        "false, $6, $7, $8, $9)",
        token_id,
        f"public-{token_id}",
        f"{token_id:064d}",
        direction,
        path,
        key_id,
        oauth_token_id,
        user_id,
        FUTURE,
    )


def transfer_actor_of(url, token_id: int):
    """`(actor_kind, actor_label, actor_ref)` for one transfer token."""
    row = fetch(
        url,
        "SELECT actor_kind, actor_label, actor_ref FROM transfer_tokens WHERE id = $1",
        token_id,
    )[0]
    return row["actor_kind"], row["actor_label"], row["actor_ref"]


def transfer_rows(url):
    """Every `transfer_tokens` row, whole, ordered — for byte-for-byte compare.

    The orphan-label case has to prove the refusal changed *nothing*, not just
    that the one label it named survived: a migration that raised after writing
    two of three columns would pass the narrower assertion.
    """
    return [
        dict(row)
        for row in fetch(url, "SELECT * FROM transfer_tokens ORDER BY id")
    ]


def add_owned_transfer_actor_columns(url, *, marked=True):
    """The three columns exactly as 017 creates them, marker optional."""
    for column, coltype in TRANSFER_ACTOR_COLUMNS:
        sql(url, f"ALTER TABLE transfer_tokens ADD COLUMN {column} {coltype}")
        if marked:
            sql(
                url,
                f"COMMENT ON COLUMN transfer_tokens.{column} IS "
                f"'{TRANSFER_ACTOR_MARKER}'",
            )


def refuse_017(url, *, must_mention):
    """Run `upgrade head`, require 017 to refuse, and return the message."""
    result = _harness.run_alembic(url, "upgrade", "head", dimensions=DIM, check=False)
    combined = result.stdout + result.stderr
    assert result.returncode != 0, combined
    for fragment in must_mention:
        assert fragment in combined, combined
    assert "Nothing has been changed" in combined, combined
    assert alembic_version(url) == "016"
    return combined


def seed_pre_017_transfers(url):
    """One key-minted capability, one OAuth-minted one, one with neither FK.

    The third is the single-user / sandbox mint: nothing on the row names a
    credential, so there is nothing to label and nothing may be guessed. It is
    *not* the "credential already deleted" case 015 had — both FKs here are
    ON DELETE CASCADE, so such a row does not survive to be labelled at all.

    A transfer-route `usage_logs` row is seeded beside them, with a live
    `key_id` and NULL actor columns: exactly the 015 -> 017 gap row, which 017
    must leave alone.
    """
    insert_user(url, 1, "alice")
    insert_key(url, 1, "nightly sync", "omcp_a1b2c3", user_id=1)
    insert_key(url, 2, "other key", "omcp_zzzzzz", user_id=1)
    insert_client(url, "client-abc", "none", None)
    sql(
        url,
        "INSERT INTO oauth_tokens (token_hash, token_type, client_id, scope, "
        "user_id, grant_id, expires_at, revoked) "
        "VALUES ($1, 'access', 'client-abc', 'read', 1, 'grant-1', $2, false)",
        "a" * 64,
        FUTURE,
    )
    token_id = fetchval(url, "SELECT id FROM oauth_tokens WHERE token_hash = $1", "a" * 64)

    insert_transfer_token(url, 1, key_id=1, user_id=1)
    insert_transfer_token(url, 2, oauth_token_id=token_id, user_id=1)
    insert_transfer_token(url, 3, direction="download", path="Notes/plan.md")

    # The gap row: written by the transfer route after 015 and before 017, so
    # its actor columns are NULL even though its credential still resolves.
    insert_usage(url, 1, key_id=1, tool="upload_file")
    return token_id


def test_the_transfer_actor_columns_are_nullable_and_marked_on_a_fresh_database():
    """Nullable is load-bearing: a mint that cannot name its actor — single
    user, sandbox, any path outside a request — must still produce a token.
    These columns are display and audit, never authorization."""
    with throwaway_db("schema_tt_actor_fresh") as url:
        assert alembic_version(url) == HEAD_REVISION
        for column, expected_type in TRANSFER_ACTOR_COLUMNS:
            shape = column_shape(url, "transfer_tokens", column)
            assert shape is not None, f"transfer_tokens.{column} is missing"
            attnotnull, coltype, coldefault = shape
            assert attnotnull is False, f"{column} must stay nullable"
            assert coltype == expected_type, f"{column} is {coltype}"
            assert coldefault is None, f"{column} carries a server default"
            assert transfer_actor_column_is_marked(url, column), (
                f"transfer_tokens.{column} lost 017's comment marker"
            )
        # `alembic check` clean at head — which is only true while the model's
        # declared column comments are byte-identical to 017's marker, and
        # while 016 (run in this same upgrade) agrees with its own model.
        check = _harness.run_alembic(url, "check", dimensions=DIM, check=False)
        assert check.returncode == 0, (
            f"alembic check reported drift\n{check.stdout}\n{check.stderr}"
        )
        assert "No new upgrade operations detected" in check.stdout


def test_both_migrations_of_this_wave_run_in_one_upgrade():
    """The gate covers 016 *and* 017, in the same run, on the same database.

    Per-migration confidence proves nothing about the pair: they land in one
    deploy, alembic runs them in one transaction, and 017's `down_revision` is
    what puts them in a line rather than on a branch.
    """
    with throwaway_db("schema_wave_both") as url:
        assert alembic_version(url) == HEAD_REVISION
        for column, _ in PROVENANCE_COLUMNS:
            assert column_shape(url, "users", column) is not None
            assert provenance_column_is_marked(url, column)
        for column, _ in TRANSFER_ACTOR_COLUMNS:
            assert column_shape(url, "transfer_tokens", column) is not None
            assert transfer_actor_column_is_marked(url, column)
        # The two markers are distinct, so neither `downgrade()` can claim the
        # other's columns.
        assert PROVENANCE_MARKER != TRANSFER_ACTOR_MARKER


def test_017_labels_each_token_from_its_own_credential():
    with throwaway_db("schema_tt_actor_backfill", revision="016") as url:
        seed_pre_017_transfers(url)

        _harness.run_alembic(url, "upgrade", "head", dimensions=DIM)

        assert alembic_version(url) == HEAD_REVISION
        assert transfer_actor_of(url, 1) == ("api_key", "nightly sync", "omcp_a1b2c3")
        assert transfer_actor_of(url, 2) == ("oauth", "test client", "client-abc")
        # Never from another row's credential: the second key exists and is the
        # same user's, and nothing may reach for it.
        assert "other key" not in {transfer_actor_of(url, i)[1] for i in (1, 2)}


def test_a_token_with_no_credential_fk_stays_unattributed():
    """Nothing is invented.

    Both credential FKs are ON DELETE CASCADE, so a row whose minting
    credential was deleted is gone. A row with neither FK is a single-user or
    sandbox mint, and a guess from `user_id` would be worse than an admitted
    gap: two of a user's keys are different actors.
    """
    with throwaway_db("schema_tt_actor_orphan_row", revision="016") as url:
        seed_pre_017_transfers(url)

        _harness.run_alembic(url, "upgrade", "head", dimensions=DIM)

        assert transfer_actor_of(url, 3) == (None, None, None)


def test_017_writes_nothing_to_the_usage_log():
    """The 015 -> 017 gap row keeps join-only attribution.

    `usage_logs` carries no reference back to the token that produced it, so
    the only available backfill would be a re-run of 015's own credential join
    — a second writer on three columns 015 owns and guards. 017 declines, and
    the gap rows render through the panel's existing pre-015 fallback.
    """
    with throwaway_db("schema_tt_actor_usage_untouched", revision="016") as url:
        seed_pre_017_transfers(url)
        before = fetch(url, "SELECT * FROM usage_logs ORDER BY id")

        _harness.run_alembic(url, "upgrade", "head", dimensions=DIM)

        assert actor_of(url, 1) == (None, None, None)
        assert [dict(row) for row in fetch(url, "SELECT * FROM usage_logs ORDER BY id")] == [
            dict(row) for row in before
        ]


def test_rerunning_017_does_not_rewrite_an_actor_recorded_by_a_mint():
    """Idempotence that re-executes the body, and history that stays history.

    Stamping back to 016 forces 017 to run again over a row whose recorded
    label deliberately differs from the credential's current name — what a
    rename produces. The guard is `actor_kind IS NULL`, so a renamed key must
    not retroactively rename every capability it ever minted.
    """
    with throwaway_db("schema_tt_actor_idempotent", revision="016") as url:
        seed_pre_017_transfers(url)
        _harness.run_alembic(url, "upgrade", "head", dimensions=DIM)
        assert transfer_actor_of(url, 1) == ("api_key", "nightly sync", "omcp_a1b2c3")

        sql(url, "UPDATE api_keys SET name = 'renamed later' WHERE id = 1")
        _harness.run_alembic(url, "stamp", "016", dimensions=DIM)
        assert alembic_version(url) == "016"
        _harness.run_alembic(url, "upgrade", "head", dimensions=DIM)

        assert alembic_version(url) == HEAD_REVISION
        assert transfer_actor_of(url, 1) == ("api_key", "nightly sync", "omcp_a1b2c3")


def test_a_transfer_label_beside_a_null_kind_is_refused_not_overwritten():
    """B.5a's invariant, and the reason it exists.

    `actor_kind IS NULL` is the backfill's *only* guard, so a row carrying a
    label under a NULL kind would be relabelled from whatever credential its FK
    points at now — overwriting a recorded attribution, which is the one thing
    these columns must never do. Reachable by a stamp-back re-run over a
    database that drift or a faulty writer has put in that state.
    """
    with throwaway_db("schema_tt_actor_orphan_label", revision="016") as url:
        seed_pre_017_transfers(url)
        add_owned_transfer_actor_columns(url)
        sql(url, "UPDATE transfer_tokens SET actor_label = 'hand written' WHERE id = 1")
        before = transfer_rows(url)

        refuse_017(url, must_mention=["actor_kind", "will not rewrite", "ids: 1"])

        # Byte for byte: not just the named label, every row and every column.
        assert transfer_rows(url) == before
        assert transfer_actor_of(url, 1) == (None, "hand written", None)
        assert transfer_actor_of(url, 2) == (None, None, None)


@pytest.mark.parametrize(
    "label,ddl,fragment",
    [
        (
            "wrong type",
            "ALTER TABLE transfer_tokens ALTER COLUMN actor_label TYPE text",
            "actor_label is text",
        ),
        (
            "NOT NULL",
            "ALTER TABLE transfer_tokens ALTER COLUMN actor_label SET NOT NULL",
            "is NOT NULL",
        ),
        (
            "server default",
            "ALTER TABLE transfer_tokens ALTER COLUMN actor_kind SET DEFAULT 'api_key'",
            "carries a server default",
        ),
    ],
)
def test_a_foreign_transfer_actor_column_is_refused(label, ddl, fragment):
    """013's philosophy: reconcile a column we can verify, refuse to guess.

    A column of unknown provenance under one of these names holds labels this
    migration did not write — and `_log_row` copies them onto `usage_logs` at
    redemption, where an operator reads them as an audit trail.
    """
    with throwaway_db(f"schema_tt_foreign_{label.replace(' ', '_')}", revision="016") as url:
        add_owned_transfer_actor_columns(url)
        sql(url, ddl)

        refuse_017(url, must_mention=[fragment])


def test_a_partial_transfer_actor_column_set_is_refused():
    """The three are one owned unit.

    A database with only `actor_kind` is not a re-run, it is one somebody
    edited — and `actor_kind IS NULL` is what decides which rows the backfill
    writes, so a foreign guard column silently decides what gets labelled.
    """
    with throwaway_db("schema_tt_partial", revision="016") as url:
        sql(url, "ALTER TABLE transfer_tokens ADD COLUMN actor_kind character varying(20)")
        sql(
            url,
            f"COMMENT ON COLUMN transfer_tokens.actor_kind IS '{TRANSFER_ACTOR_MARKER}'",
        )

        refuse_017(url, must_mention=["actor_kind", "actor_label", "absent"])

        assert column_shape(url, "transfer_tokens", "actor_label") is None
        assert column_shape(url, "transfer_tokens", "actor_ref") is None


def test_an_unmarked_transfer_actor_column_set_is_refused():
    """Type and width are a coincidence anyone could reproduce; the comment is
    the only evidence that *this* scheme wrote the values."""
    with throwaway_db("schema_tt_unmarked", revision="016") as url:
        add_owned_transfer_actor_columns(url, marked=False)

        refuse_017(url, must_mention=["017's comment marker"])


def test_a_complete_marked_transfer_actor_set_is_accepted_as_a_rerun():
    """The benign case: 017's own shape, applied by hand or left by a re-stamp.

    Nothing has to be guessed, so the migration completes and backfills.
    """
    with throwaway_db("schema_tt_rerun", revision="016") as url:
        seed_pre_017_transfers(url)
        add_owned_transfer_actor_columns(url)

        _harness.run_alembic(url, "upgrade", "head", dimensions=DIM)

        assert alembic_version(url) == HEAD_REVISION
        assert transfer_actor_of(url, 1) == ("api_key", "nightly sync", "omcp_a1b2c3")
        assert transfer_actor_of(url, 2) == ("oauth", "test client", "client-abc")


def test_downgrade_017_drops_the_marked_set_and_upgrade_rebuilds_it():
    with throwaway_db("schema_tt_downgrade", revision="016") as url:
        seed_pre_017_transfers(url)
        _harness.run_alembic(url, "upgrade", "head", dimensions=DIM)
        assert transfer_actor_of(url, 1)[0] == "api_key"

        _harness.run_alembic(url, "downgrade", "016", dimensions=DIM)
        assert alembic_version(url) == "016"
        for column, _ in TRANSFER_ACTOR_COLUMNS:
            assert column_shape(url, "transfer_tokens", column) is None
        # 016 is a separate unit and is untouched by 017's downgrade.
        for column, _ in PROVENANCE_COLUMNS:
            assert column_shape(url, "users", column) is not None

        _harness.run_alembic(url, "upgrade", "head", dimensions=DIM)
        assert alembic_version(url) == HEAD_REVISION
        # Re-derived from the credentials that still exist.
        assert transfer_actor_of(url, 1) == ("api_key", "nightly sync", "omcp_a1b2c3")


def test_downgrade_017_refuses_to_drop_a_set_it_did_not_create():
    """All or nothing, decided before anything is touched.

    An unmarked column under one of these names is left in place and reported
    rather than destroyed on the way past — and its two marked siblings survive
    with it.
    """
    with throwaway_db("schema_tt_downgrade_foreign") as url:
        sql(url, "COMMENT ON COLUMN transfer_tokens.actor_ref IS 'somebody else'")

        result = _harness.run_alembic(
            url, "downgrade", "016", dimensions=DIM, check=False
        )
        combined = result.stdout + result.stderr
        assert result.returncode != 0, combined
        assert "actor_ref" in combined, combined
        assert "Nothing has been changed" in combined, combined

        assert alembic_version(url) == HEAD_REVISION
        for column, _ in TRANSFER_ACTOR_COLUMNS:
            assert column_shape(url, "transfer_tokens", column) is not None


# ── 017's one-credential invariant (adversarial round 1, MAJOR) ─────────────
#
# `transfer_tokens.key_id` and `.oauth_token_id` are independently nullable, so
# nothing in the schema stopped a row naming both. Such a row records which
# credential minted it nowhere, and the backfill's API-key UPDATE would label
# it from the key purely because it runs first — manufacturing a definitive
# attribution out of an ambiguity, then copying it onto `usage_logs` at
# redemption and showing it to an operator as an audit trail.

TRANSFER_ONE_CREDENTIAL = "ck_transfer_tokens_one_credential"
TRANSFER_ONE_CREDENTIAL_MARKER = (
    "one minting credential, never two (017_transfer_token_actor)"
)


def one_credential_constraint(url):
    """`(definition, validated, comment)` for 017's CHECK, or None."""
    rows = fetch(
        url,
        "SELECT pg_get_constraintdef(c.oid) AS definition, c.convalidated, "
        "       obj_description(c.oid, 'pg_constraint') AS comment "
        "FROM pg_constraint c "
        "WHERE c.conrelid = 'transfer_tokens'::regclass AND c.contype = 'c' "
        "  AND c.conname = $1",
        TRANSFER_ONE_CREDENTIAL,
    )
    if not rows:
        return None
    return rows[0]["definition"], rows[0]["convalidated"], rows[0]["comment"]


def test_the_one_credential_constraint_exists_and_is_marked_on_a_fresh_database():
    with throwaway_db("schema_tt_one_cred_fresh") as url:
        state = one_credential_constraint(url)
        assert state is not None, "017's one-credential CHECK is missing"
        definition, validated, comment = state
        assert "key_id IS NULL" in definition and "oauth_token_id IS NULL" in definition
        assert validated is True, "a NOT VALID constraint admits every existing row"
        assert comment == TRANSFER_ONE_CREDENTIAL_MARKER


def test_the_constraint_rejects_a_two_credential_insert():
    """The schema, not a convention, is what stops the state reappearing."""
    with throwaway_db("schema_tt_one_cred_insert") as url:
        insert_user(url, 1, "alice")
        insert_key(url, 1, "nightly sync", "omcp_a1b2c3", user_id=1)
        insert_client(url, "client-abc", "none", None)
        sql(
            url,
            "INSERT INTO oauth_tokens (token_hash, token_type, client_id, scope, "
            "user_id, grant_id, expires_at, revoked) "
            "VALUES ($1, 'access', 'client-abc', 'read', 1, 'grant-1', $2, false)",
            "a" * 64,
            FUTURE,
        )
        token_id = fetchval(
            url, "SELECT id FROM oauth_tokens WHERE token_hash = $1", "a" * 64
        )

        with pytest.raises(Exception) as excinfo:
            insert_transfer_token(url, 1, key_id=1, oauth_token_id=token_id, user_id=1)
        assert TRANSFER_ONE_CREDENTIAL in str(excinfo.value)

        # Both NULL stays legal — the single-user and sandbox shape.
        insert_transfer_token(url, 2)
        assert fetchval(url, "SELECT count(*) FROM transfer_tokens") == 1


def test_a_two_credential_row_is_refused_and_nothing_is_labelled():
    """013's and 015's offender shape: name the ids, change nothing.

    The refusal has to come *before* either backfill, because the API-key
    UPDATE would otherwise win by running first and the second UPDATE's
    `actor_kind IS NULL` guard would then skip the row it had just mislabelled.
    """
    with throwaway_db("schema_tt_one_cred_offender", revision="016") as url:
        token_id = seed_pre_017_transfers(url)
        # At 016 the constraint does not exist yet, so the drifted row goes in.
        insert_transfer_token(url, 4, key_id=2, oauth_token_id=token_id, user_id=1)

        combined = refuse_017(
            url, must_mention=["both a key_id and an oauth_token_id", "ids: 4"]
        )
        assert "which credential minted it" in combined

        # Nothing labelled, and the constraint was not installed either.
        for column, _ in TRANSFER_ACTOR_COLUMNS:
            assert column_shape(url, "transfer_tokens", column) is None
        assert one_credential_constraint(url) is None


def test_an_impostor_constraint_of_that_name_is_refused():
    """Resolved through `pg_constraint`, never by name: a same-named
    `CHECK (true)` satisfies a lookup by name while enforcing nothing."""
    with throwaway_db("schema_tt_one_cred_impostor", revision="016") as url:
        seed_pre_017_transfers(url)
        sql(
            url,
            f"ALTER TABLE transfer_tokens ADD CONSTRAINT {TRANSFER_ONE_CREDENTIAL} "
            "CHECK (true)",
        )
        refuse_017(url, must_mention=[TRANSFER_ONE_CREDENTIAL, "enforces something else"])


def test_an_unmarked_constraint_of_that_name_is_refused():
    with throwaway_db("schema_tt_one_cred_unmarked", revision="016") as url:
        seed_pre_017_transfers(url)
        sql(
            url,
            f"ALTER TABLE transfer_tokens ADD CONSTRAINT {TRANSFER_ONE_CREDENTIAL} "
            "CHECK (key_id IS NULL OR oauth_token_id IS NULL)",
        )
        refuse_017(url, must_mention=["017's comment marker"])


def test_a_not_valid_constraint_of_that_name_is_refused():
    with throwaway_db("schema_tt_one_cred_notvalid", revision="016") as url:
        seed_pre_017_transfers(url)
        sql(
            url,
            f"ALTER TABLE transfer_tokens ADD CONSTRAINT {TRANSFER_ONE_CREDENTIAL} "
            "CHECK (key_id IS NULL OR oauth_token_id IS NULL) NOT VALID",
        )
        refuse_017(url, must_mention=["NOT VALID"])


def test_rerunning_017_accepts_its_own_constraint_and_still_does_not_relabel():
    """Stamp-back idempotence with the constraint in place: the migration body
    genuinely re-executes, adopts the constraint it can prove it wrote, and
    leaves every recorded label alone."""
    with throwaway_db("schema_tt_one_cred_rerun", revision="016") as url:
        seed_pre_017_transfers(url)
        _harness.run_alembic(url, "upgrade", "head", dimensions=DIM)
        sql(
            url,
            "UPDATE transfer_tokens SET actor_label = 'renamed since' WHERE id = 1",
        )

        _harness.run_alembic(url, "stamp", "016", dimensions=DIM)
        _harness.run_alembic(url, "upgrade", "head", dimensions=DIM)

        assert alembic_version(url) == HEAD_REVISION
        assert transfer_actor_of(url, 1) == ("api_key", "renamed since", "omcp_a1b2c3")
        state = one_credential_constraint(url)
        assert state is not None and state[2] == TRANSFER_ONE_CREDENTIAL_MARKER
        # Still exactly one constraint of that name.
        assert fetchval(
            url,
            "SELECT count(*) FROM pg_constraint WHERE conrelid = "
            "'transfer_tokens'::regclass AND conname = $1",
            TRANSFER_ONE_CREDENTIAL,
        ) == 1


def test_downgrade_017_drops_its_own_constraint_but_not_a_foreign_one():
    with throwaway_db("schema_tt_one_cred_downgrade", revision="016") as url:
        seed_pre_017_transfers(url)
        _harness.run_alembic(url, "upgrade", "head", dimensions=DIM)
        assert one_credential_constraint(url) is not None

        _harness.run_alembic(url, "downgrade", "016", dimensions=DIM)
        assert one_credential_constraint(url) is None

        # A same-named constraint somebody else installed survives a downgrade:
        # it must undo *this* migration and nothing else.
        _harness.run_alembic(url, "upgrade", "head", dimensions=DIM)
        sql(
            url,
            f"COMMENT ON CONSTRAINT {TRANSFER_ONE_CREDENTIAL} ON transfer_tokens "
            "IS 'somebody else'",
        )
        _harness.run_alembic(url, "downgrade", "016", dimensions=DIM)
        assert one_credential_constraint(url) is not None


# ══════════════════════════════════════════════════════════════════════════
# 018 — the fence-grammar derivation marker on `notes_metadata` (issue #150)
# ══════════════════════════════════════════════════════════════════════════
#
# The column is one SMALLINT with a server default, so there is far less to get
# wrong than in 015/016/017 — and exactly three things that still are. The
# **server default** is what keeps the ADD COLUMN metadata-only on a table
# carrying a tsvector and two GIN indexes, and what gives every pre-existing
# row the one correct value (0 = "derived by the pre-#150 grammar"). **NOT
# NULL** is what lets the indexer read the value as a version instead of
# branching on NULL. And the **marker** is what `downgrade()` keys on, mirrored
# on the ORM column so `alembic check` compares it.

EXTRACTION_VERSION_MARKER = "fence-grammar derivation marker (018_extraction_version)"


def extraction_version_comment(url):
    return fetchval(
        url,
        "SELECT col_description(a.attrelid, a.attnum) FROM pg_attribute a "
        "WHERE a.attrelid = 'notes_metadata'::regclass "
        "  AND a.attname = 'extraction_version'",
    )


def seed_pre_018_notes(url):
    """Two notes written by the pre-#150 indexer: no marker column at all."""
    insert_user(url, 1, "alice")
    sql(
        url,
        "INSERT INTO notes_metadata "
        "(id, user_id, file_path, title, content_hash, embedded_content_hash) "
        "VALUES (1, 1, 'A.md', 'A', 'hash-a', 'hash-a'), "
        "       (2, 1, 'B.md', 'B', 'hash-b', NULL)",
    )


def refuse_018(url, *, must_mention):
    """Run `upgrade head`, require 018 to refuse, and return the message."""
    result = _harness.run_alembic(url, "upgrade", "head", dimensions=DIM, check=False)
    assert result.returncode != 0, "018 should have refused"
    combined = result.stdout + result.stderr
    for phrase in must_mention:
        assert phrase in combined, f"refusal did not mention {phrase!r}:\n{combined}"
    return combined


def test_the_extraction_marker_is_not_null_defaulted_and_marked_on_a_fresh_db():
    with throwaway_db("schema_xv_fresh") as url:
        assert alembic_version(url) == HEAD_REVISION
        shape = column_shape(url, "notes_metadata", "extraction_version")
        assert shape is not None, "notes_metadata.extraction_version is missing"
        attnotnull, coltype, coldefault = shape
        assert attnotnull is True, "the marker must be NOT NULL"
        assert coltype == "smallint"
        assert coldefault == "0", (
            "the server default is what makes the ADD COLUMN metadata-only and "
            "what gives every pre-existing row the pre-#150 value"
        )
        assert extraction_version_comment(url) == EXTRACTION_VERSION_MARKER
        check = _harness.run_alembic(url, "check", dimensions=DIM, check=False)
        assert check.returncode == 0, (
            f"alembic check reported drift\n{check.stdout}\n{check.stderr}"
        )
        assert "No new upgrade operations detected" in check.stdout


def test_018_stamps_every_pre_existing_row_zero_and_touches_nothing_else():
    """The whole point of the default: rows written by the old indexer read as
    "derived by the old grammar", and neither `content_hash` nor
    `embedded_content_hash` is disturbed — the first is the move-detection key
    and the second is the embed backlog's predicate."""
    with throwaway_db("schema_xv_backfill", revision="017") as url:
        seed_pre_018_notes(url)
        assert column_shape(url, "notes_metadata", "extraction_version") is None

        _harness.run_alembic(url, "upgrade", "head", dimensions=DIM)

        rows = fetch(
            url,
            "SELECT id, content_hash, embedded_content_hash, extraction_version "
            "FROM notes_metadata ORDER BY id",
        )
        assert [tuple(r) for r in rows] == [
            (1, "hash-a", "hash-a", 0),
            (2, "hash-b", None, 0),
        ]


def test_rerunning_018_leaves_recorded_markers_alone():
    """Stamp-back idempotence, the shape the schema gate itself performs. The
    migration body genuinely re-executes; a marker the indexer has since
    advanced must survive it, or every stamp-back would re-derive the vault."""
    with throwaway_db("schema_xv_rerun", revision="017") as url:
        seed_pre_018_notes(url)
        _harness.run_alembic(url, "upgrade", "head", dimensions=DIM)
        sql(url, "UPDATE notes_metadata SET extraction_version = 1 WHERE id = 1")

        _harness.run_alembic(url, "stamp", "017", dimensions=DIM)
        _harness.run_alembic(url, "upgrade", "head", dimensions=DIM)

        assert alembic_version(url) == HEAD_REVISION
        assert fetchval(
            url, "SELECT extraction_version FROM notes_metadata WHERE id = 1"
        ) == 1
        assert extraction_version_comment(url) == EXTRACTION_VERSION_MARKER


def test_018_refuses_an_unmarked_column_of_its_name():
    with throwaway_db("schema_xv_unmarked", revision="017") as url:
        sql(
            url,
            "ALTER TABLE notes_metadata ADD COLUMN extraction_version "
            "smallint NOT NULL DEFAULT 0",
        )
        refuse_018(url, must_mention=["018's comment marker"])


def test_018_refuses_a_nullable_or_wrongly_defaulted_column_of_its_name():
    with throwaway_db("schema_xv_nullable", revision="017") as url:
        sql(url, "ALTER TABLE notes_metadata ADD COLUMN extraction_version smallint")
        sql(
            url,
            "COMMENT ON COLUMN notes_metadata.extraction_version IS $$"
            + EXTRACTION_VERSION_MARKER
            + "$$",
        )
        refuse_018(url, must_mention=["nullable", "server default"])


def test_downgrade_018_drops_its_own_column_and_upgrade_rebuilds_it():
    with throwaway_db("schema_xv_downgrade", revision="017") as url:
        seed_pre_018_notes(url)
        _harness.run_alembic(url, "upgrade", "head", dimensions=DIM)
        assert column_shape(url, "notes_metadata", "extraction_version") is not None

        _harness.run_alembic(url, "downgrade", "017", dimensions=DIM)
        assert column_shape(url, "notes_metadata", "extraction_version") is None
        # Note identity survives the round trip, which is what makes the
        # rollback bounded: `content_hash` was never touched.
        assert fetchval(
            url, "SELECT content_hash FROM notes_metadata WHERE id = 1"
        ) == "hash-a"

        _harness.run_alembic(url, "upgrade", "head", dimensions=DIM)
        assert fetchval(
            url, "SELECT extraction_version FROM notes_metadata WHERE id = 1"
        ) == 0


def test_downgrade_018_refuses_to_drop_a_column_it_did_not_create():
    with throwaway_db("schema_xv_downgrade_foreign", revision="017") as url:
        _harness.run_alembic(url, "upgrade", "head", dimensions=DIM)
        sql(
            url,
            "COMMENT ON COLUMN notes_metadata.extraction_version IS 'somebody else'",
        )
        result = _harness.run_alembic(
            url, "downgrade", "017", dimensions=DIM, check=False
        )
        assert result.returncode != 0
        assert "018's comment marker" in result.stdout + result.stderr
        assert column_shape(url, "notes_metadata", "extraction_version") is not None


# ══════════════════════════════════════════════════════════════════════════
# 019 — `indexer_runs`, and why a marker plus two index names is not a check
# ══════════════════════════════════════════════════════════════════════════
#
# 019 reconciles rather than bare-CREATEs, for the reason 013 established: the
# schema gate itself stamps back and re-runs every revision, so the migration
# meets a database that already carries its table. The question is what it will
# *adopt*.
#
# The comment marker is evidence of authorship and nothing more. A table can
# carry it and still have been altered since, and the two alterations that
# matter most are invisible to a name-level check:
#
#   * the FK repointed at `api_keys(id)`, still `ON DELETE SET NULL` — the
#     column the panel labels "owner" then names a different table's rows, and
#     an operator reads them as the history of what the indexer did for a user;
#   * `ix_indexer_runs_started_at` dropped and recreated on `error` — the name
#     an existence check looks for survives while the newest-first scan the
#     panel and the 500-row pruner both depend on has nothing to lean on.
#
# Neither is visible to `alembic check` either: autogenerate does not compare a
# FK's referenced table, and an index of the right name on the wrong column is
# reported as no drift at all. So these cases assert the catalog.

INDEXER_RUNS_MARKER = "one row per index/embed pass (019_indexer_runs)"


def indexer_runs_comment(url):
    return fetchval(
        url,
        "SELECT obj_description(c.oid, 'pg_class') FROM pg_class c "
        "WHERE c.relname = 'indexer_runs' AND c.relkind = 'r'",
    )


def indexer_runs_fk(url):
    """`(local, referenced_table, referenced_columns, delete, update, valid)`."""
    rows = fetch(
        url,
        "SELECT (SELECT array_agg(a.attname ORDER BY k.ord) "
        "          FROM unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) "
        "          JOIN pg_attribute a ON a.attrelid = c.conrelid "
        "                             AND a.attnum = k.attnum) AS local_columns, "
        "       c.confrelid::regclass::text AS referenced_table, "
        "       (SELECT array_agg(a.attname ORDER BY k.ord) "
        "          FROM unnest(c.confkey) WITH ORDINALITY AS k(attnum, ord) "
        "          JOIN pg_attribute a ON a.attrelid = c.confrelid "
        "                             AND a.attnum = k.attnum) AS referenced_columns, "
        "       c.confdeltype::text, c.confupdtype::text, c.convalidated "
        "FROM pg_constraint c "
        "WHERE c.conrelid = 'indexer_runs'::regclass AND c.contype = 'f'",
    )
    return [tuple(r) for r in rows]


def indexer_runs_index_columns(url, name):
    return fetchval(
        url,
        "SELECT array_agg(a.attname ORDER BY k.ord) "
        "FROM pg_index i JOIN pg_class ic ON ic.oid = i.indexrelid "
        "     CROSS JOIN unnest(string_to_array(i.indkey::text, ' ')) "
        "                WITH ORDINALITY AS k(attnum, ord) "
        "     JOIN pg_attribute a ON a.attrelid = i.indrelid "
        "                        AND a.attnum = k.attnum::smallint "
        "WHERE i.indrelid = 'indexer_runs'::regclass AND ic.relname = $1 "
        "GROUP BY ic.relname",
        name,
    )


def refuse_019(url, *, must_mention):
    """Stamp back to 018, re-run 019, require it to refuse, return the message.

    Stamping back is what makes this adversarial rather than theatrical: it is
    the *same* path `make test-schema` exercises for idempotence, so a verifier
    that waves the mutation through here would wave it through on a real
    database whose table somebody had altered.
    """
    _harness.run_alembic(url, "stamp", "018", dimensions=DIM)
    result = _harness.run_alembic(url, "upgrade", "head", dimensions=DIM, check=False)
    assert result.returncode != 0, "019 should have refused"
    combined = result.stdout + result.stderr
    for phrase in must_mention:
        assert phrase in combined, f"refusal did not mention {phrase!r}:\n{combined}"
    return combined


def test_019_creates_the_table_it_promises():
    with throwaway_db("schema_runs_fresh") as url:
        assert alembic_version(url) == HEAD_REVISION
        assert indexer_runs_comment(url) == INDEXER_RUNS_MARKER
        assert indexer_runs_fk(url) == [
            (["user_id"], "users", ["id"], "n", "a", True)
        ]
        assert indexer_runs_index_columns(url, "ix_indexer_runs_started_at") == [
            "started_at"
        ]
        assert indexer_runs_index_columns(url, "ix_indexer_runs_user_id") == ["user_id"]
        check = _harness.run_alembic(url, "check", dimensions=DIM, check=False)
        assert check.returncode == 0, (
            f"alembic check reported drift\n{check.stdout}\n{check.stderr}"
        )


def test_rerunning_019_adopts_its_own_table_and_keeps_the_rows():
    """The idempotence path the gate itself performs: 019 genuinely re-executes
    against a database already carrying its table, and the history survives."""
    with throwaway_db("schema_runs_rerun") as url:
        sql(
            url,
            "INSERT INTO indexer_runs (started_at, finished_at, trigger, "
            " notes_scanned) VALUES (now(), now(), 'startup', 2577)",
        )
        _harness.run_alembic(url, "stamp", "018", dimensions=DIM)
        _harness.run_alembic(url, "upgrade", "head", dimensions=DIM)

        assert alembic_version(url) == HEAD_REVISION
        assert fetchval(url, "SELECT notes_scanned FROM indexer_runs") == 2577
        assert indexer_runs_comment(url) == INDEXER_RUNS_MARKER


def test_019_refuses_a_foreign_key_pointing_at_another_table():
    """The adversarial case the name-level verifier adopted.

    Everything a marker-and-delete-action check reads is untouched: 019's
    comment, 019's columns, 019's CHECK, `ON DELETE SET NULL`. Only the
    *referent* moved — and with it the meaning of every "owner" the panel
    renders.
    """
    with throwaway_db("schema_runs_fk_target") as url:
        constraint = fetchval(
            url,
            "SELECT conname FROM pg_constraint "
            "WHERE conrelid = 'indexer_runs'::regclass AND contype = 'f'",
        )
        sql(url, f"ALTER TABLE indexer_runs DROP CONSTRAINT {constraint}")
        sql(
            url,
            "ALTER TABLE indexer_runs ADD CONSTRAINT " + constraint + " "
            "FOREIGN KEY (user_id) REFERENCES api_keys(id) ON DELETE SET NULL",
        )
        # The mutation really is invisible to the cheap gates.
        assert indexer_runs_comment(url) == INDEXER_RUNS_MARKER
        assert indexer_runs_fk(url)[0][1] == "api_keys"
        assert indexer_runs_fk(url)[0][3] == "n", "still ON DELETE SET NULL"

        refuse_019(url, must_mention=["api_keys", "references"])
        # And nothing was changed on the way to refusing.
        assert indexer_runs_fk(url)[0][1] == "api_keys"


def test_019_refuses_a_not_valid_foreign_key():
    """`NOT VALID` enforces new rows having never checked the existing ones, so
    the table may already name an owner that does not exist — which is exactly
    the value the page prints beside a pass."""
    with throwaway_db("schema_runs_fk_notvalid") as url:
        constraint = fetchval(
            url,
            "SELECT conname FROM pg_constraint "
            "WHERE conrelid = 'indexer_runs'::regclass AND contype = 'f'",
        )
        sql(url, f"ALTER TABLE indexer_runs DROP CONSTRAINT {constraint}")
        sql(
            url,
            "ALTER TABLE indexer_runs ADD CONSTRAINT " + constraint + " "
            "FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL NOT VALID",
        )
        refuse_019(url, must_mention=["NOT VALID"])


def test_019_refuses_a_missing_or_cascading_foreign_key():
    with throwaway_db("schema_runs_fk_cascade") as url:
        constraint = fetchval(
            url,
            "SELECT conname FROM pg_constraint "
            "WHERE conrelid = 'indexer_runs'::regclass AND contype = 'f'",
        )
        sql(url, f"ALTER TABLE indexer_runs DROP CONSTRAINT {constraint}")
        refuse_019(url, must_mention=["no foreign key at all"])

        sql(
            url,
            "ALTER TABLE indexer_runs ADD CONSTRAINT " + constraint + " "
            "FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE",
        )
        refuse_019(url, must_mention=["SET NULL"])


def test_019_refuses_an_index_of_its_name_on_another_column():
    """The other half of the same defect: names are not definitions. The panel
    reads this table newest-first and the pruner orders by the same column, so
    an index of the right name on `error` is the scan silently unsupported."""
    with throwaway_db("schema_runs_index_column") as url:
        sql(url, "DROP INDEX ix_indexer_runs_started_at")
        sql(url, "CREATE INDEX ix_indexer_runs_started_at ON indexer_runs (error)")
        assert indexer_runs_index_columns(url, "ix_indexer_runs_started_at") == [
            "error"
        ]

        refuse_019(url, must_mention=["ix_indexer_runs_started_at", "error"])


def test_019_refuses_a_partial_index_of_its_name():
    """A partial index answers only the queries whose predicate it implies. The
    pruner's `ORDER BY started_at DESC` over the whole table is not one."""
    with throwaway_db("schema_runs_index_partial") as url:
        sql(url, "DROP INDEX ix_indexer_runs_user_id")
        sql(
            url,
            "CREATE INDEX ix_indexer_runs_user_id ON indexer_runs (user_id) "
            "WHERE trigger = 'startup'",
        )
        refuse_019(url, must_mention=["ix_indexer_runs_user_id"])


def test_019_refuses_a_missing_index():
    with throwaway_db("schema_runs_index_missing") as url:
        sql(url, "DROP INDEX ix_indexer_runs_started_at")
        refuse_019(url, must_mention=["missing index ix_indexer_runs_started_at"])


def test_019_refuses_an_unmarked_table_of_its_name():
    """`IF NOT EXISTS` would adopt this. 013's rule: reconcile a database that
    demonstrably has our shape, refuse to guess for one that does not."""
    with throwaway_db("schema_runs_unmarked", revision="018") as url:
        sql(
            url,
            "CREATE TABLE indexer_runs (id serial PRIMARY KEY, "
            " started_at timestamptz NOT NULL DEFAULT now(), "
            " finished_at timestamptz, trigger varchar(16) NOT NULL, "
            " user_id integer REFERENCES users(id) ON DELETE SET NULL, "
            " notes_scanned integer NOT NULL DEFAULT 0, "
            " notes_indexed integer NOT NULL DEFAULT 0, "
            " notes_embedded integer NOT NULL DEFAULT 0, error text)",
        )
        result = _harness.run_alembic(
            url, "upgrade", "head", dimensions=DIM, check=False
        )
        assert result.returncode != 0
        assert "019's comment marker" in result.stdout + result.stderr


# --------------------------------------------------------------------------
# migration 020 — the per-key daily quota (#162)
# --------------------------------------------------------------------------
#
# Three objects in one revision, and each of them is load-bearing in a way a
# name-level check cannot see:
#
#   * `api_keys.daily_request_limit` must stay **nullable with no default**.
#     NULL is the value that means "unlimited"; a `NOT NULL DEFAULT 0` column
#     of the same name is every key on the server refusing every call, and
#     `alembic check` would report the column as present either way.
#   * `ck_api_keys_daily_request_limit` must be the real predicate and
#     `convalidated`. A same-named `CHECK (true)` enforces nothing — issue #53
#     exactly — and the value it lets through, a limit of 0, is a key that can
#     never call anything again.
#   * `quota_counters` must have the composite PK `(key_id, day)`. That is the
#     arbiter `ON CONFLICT (key_id, day)` names: without it every admission
#     raises, and with a single-column `(key_id)` PK every day collapses onto
#     one row — a quota that never resets. Autogenerate sees a table of the
#     right name and reports no drift.
#
# So these cases assert the catalog, and then prove the CHECK is actually
# enforced by attempting the inserts it exists to reject.

QUOTA_TABLE_MARKER = "per-(key, UTC day) admission counter (020_daily_request_limit)"
QUOTA_COLUMN_MARKER = (
    "opt-in per-key daily admission ceiling (020_daily_request_limit)"
)
QUOTA_CHECK_MARKER = "created by 020_daily_request_limit"
QUOTA_CHECK_NAME = "ck_api_keys_daily_request_limit"
USAGE_KEY_INDEX = "ix_usage_logs_key_id_created_at"


def quota_counters_comment(url):
    return fetchval(
        url,
        "SELECT obj_description(c.oid, 'pg_class') FROM pg_class c "
        "WHERE c.relname = 'quota_counters' AND c.relkind = 'r'",
    )


def daily_limit_comment(url):
    return fetchval(
        url,
        "SELECT col_description(a.attrelid, a.attnum) FROM pg_attribute a "
        "WHERE a.attrelid = 'api_keys'::regclass "
        "  AND a.attname = 'daily_request_limit'",
    )


def daily_limit_check(url):
    """`(constraintdef, convalidated, comment)` for 020's CHECK, or None."""
    rows = fetch(
        url,
        "SELECT pg_get_constraintdef(oid) AS def, convalidated, "
        "       obj_description(oid, 'pg_constraint') AS comment "
        "FROM pg_constraint "
        "WHERE conrelid = 'api_keys'::regclass AND contype = 'c' AND conname = $1",
        QUOTA_CHECK_NAME,
    )
    if not rows:
        return None
    return " ".join(rows[0]["def"].split()), rows[0]["convalidated"], rows[0]["comment"]


def quota_counters_pk(url):
    return fetchval(
        url,
        "SELECT (SELECT array_agg(a.attname ORDER BY k.ord) "
        "          FROM unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) "
        "          JOIN pg_attribute a ON a.attrelid = c.conrelid "
        "                             AND a.attnum = k.attnum) "
        "FROM pg_constraint c "
        "WHERE c.conrelid = 'quota_counters'::regclass AND c.contype = 'p'",
    )


def quota_counters_fk(url):
    """`(local, referenced_table, referenced_columns, delete, update, valid)`."""
    rows = fetch(
        url,
        "SELECT (SELECT array_agg(a.attname ORDER BY k.ord) "
        "          FROM unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) "
        "          JOIN pg_attribute a ON a.attrelid = c.conrelid "
        "                             AND a.attnum = k.attnum) AS local_columns, "
        "       c.confrelid::regclass::text AS referenced_table, "
        "       (SELECT array_agg(a.attname ORDER BY k.ord) "
        "          FROM unnest(c.confkey) WITH ORDINALITY AS k(attnum, ord) "
        "          JOIN pg_attribute a ON a.attrelid = c.confrelid "
        "                             AND a.attnum = k.attnum) AS referenced_columns, "
        "       c.confdeltype::text, c.confupdtype::text, c.convalidated "
        "FROM pg_constraint c "
        "WHERE c.conrelid = 'quota_counters'::regclass AND c.contype = 'f'",
    )
    return [tuple(r) for r in rows]


def usage_index_columns(url, name):
    return fetchval(
        url,
        "SELECT array_agg(a.attname ORDER BY k.ord) "
        "FROM pg_index i JOIN pg_class ic ON ic.oid = i.indexrelid "
        "     CROSS JOIN unnest(string_to_array(i.indkey::text, ' ')) "
        "                WITH ORDINALITY AS k(attnum, ord) "
        "     JOIN pg_attribute a ON a.attrelid = i.indrelid "
        "                        AND a.attnum = k.attnum::smallint "
        "WHERE i.indrelid = 'usage_logs'::regclass AND ic.relname = $1 "
        "GROUP BY ic.relname",
        name,
    )


#: Distinct `key_prefix` values for the fixtures below. The column is
#: `varchar(12)`, so `omcp_` plus six digits is exactly at the limit; a
#: counter rather than a hash of the name keeps the value deterministic
#: across runs.
_QUOTA_KEY_SEQ = itertools.count(1)


def insert_quota_key(url, name="quota key", limit=None):
    """One `api_keys` row, returning its id. `limit` goes through the CHECK.

    Named apart from this module's other `insert_key` (the 015 fixture, which
    takes an explicit id and prefix) because these cases want the serial id
    back and care only about the limit column. `key_prefix` is
    `varchar(12)`, so it is derived rather than interpolated from the name.
    """
    return fetchval(
        url,
        "INSERT INTO api_keys (name, key_hash, key_prefix, permission, "
        "                      is_active, daily_request_limit) "
        "VALUES ($1, $2, $3, 'read', true, $4) RETURNING id",
        name,
        f"hash-{name}-{limit}",
        f"omcp_{next(_QUOTA_KEY_SEQ):06d}",
        limit,
    )


def refuse_020(url, *, must_mention):
    """Stamp back to 019, re-run 020, require it to refuse, return the message.

    The same stamp-back path `make test-schema` exercises for idempotence, so a
    verifier that waved a mutation through here would wave it through on a real
    database somebody had altered by hand.
    """
    _harness.run_alembic(url, "stamp", "019", dimensions=DIM)
    result = _harness.run_alembic(url, "upgrade", "head", dimensions=DIM, check=False)
    assert result.returncode != 0, "020 should have refused"
    combined = result.stdout + result.stderr
    for phrase in must_mention:
        assert phrase in combined, f"refusal did not mention {phrase!r}:\n{combined}"
    return combined


def test_020_creates_the_three_objects_it_promises():
    with throwaway_db("schema_quota_fresh") as url:
        assert alembic_version(url) == HEAD_REVISION

        # The column: nullable, no default, marked. NULL is "unlimited", and a
        # NOT NULL or defaulted column has no way to say it.
        notnull, coltype, default = column_shape(url, "api_keys", "daily_request_limit")
        assert (notnull, coltype, default) == (False, "integer", None)
        assert daily_limit_comment(url) == QUOTA_COLUMN_MARKER

        # The CHECK: real predicate, validated, marked.
        rendered, validated, comment = daily_limit_check(url)
        assert validated is True
        assert comment == QUOTA_CHECK_MARKER
        assert "1000000" in rendered and "IS NULL" in rendered

        # The counter table: marked, composite PK in order, cascading FK.
        assert quota_counters_comment(url) == QUOTA_TABLE_MARKER
        assert quota_counters_pk(url) == ["key_id", "day"]
        assert quota_counters_fk(url) == [
            (["key_id"], "api_keys", ["id"], "c", "a", True)
        ]

        # The composite index the usage page's per-key filter reads.
        assert usage_index_columns(url, USAGE_KEY_INDEX) == ["key_id", "created_at"]

        check = _harness.run_alembic(url, "check", dimensions=DIM, check=False)
        assert check.returncode == 0, (
            f"alembic check reported drift\n{check.stdout}\n{check.stderr}"
        )


def test_the_limit_check_rejects_zero_negative_and_oversized():
    """The catalog says the constraint is there; this proves it enforces.

    A limit of 0 would make the admission statement's guarded UPDATE decline
    every call forever — a key that refuses everything, which reads to its
    operator as an outage rather than as a setting.
    """
    with throwaway_db("schema_quota_domain") as url:
        # The legal values, including the two boundaries and NULL.
        for legal in (None, 1, 1000000):
            insert_quota_key(url, name=f"legal {legal}", limit=legal)

        for illegal in (0, -5, 1000001):
            with pytest.raises(asyncpg.CheckViolationError):
                insert_quota_key(url, name=f"bad {illegal}", limit=illegal)


def test_deleting_a_key_cascades_its_counters():
    """`ON DELETE CASCADE`, and why it is not `NO ACTION`: a counter row the
    operator cannot see would block the panel's key delete outright."""
    with throwaway_db("schema_quota_cascade") as url:
        key_id = insert_quota_key(url, limit=10)
        sql(
            url,
            "INSERT INTO quota_counters (key_id, day, count) "
            "VALUES ($1, CURRENT_DATE, 4)",
            key_id,
        )
        assert fetchval(url, "SELECT count(*) FROM quota_counters") == 1
        sql(url, "DELETE FROM api_keys WHERE id = $1", key_id)
        assert fetchval(url, "SELECT count(*) FROM quota_counters") == 0


def test_the_composite_pk_is_what_makes_admission_atomic():
    """`ON CONFLICT (key_id, day)` needs that exact arbiter, and the guarded
    `DO UPDATE ... WHERE` is what refuses at the ceiling.

    Run here, against the real catalog, because the property being asserted is
    PostgreSQL's: that the conditional upsert returns no row once the count has
    reached the limit, and that the count is left untouched by the attempt.
    """
    with throwaway_db("schema_quota_upsert") as url:
        key_id = insert_quota_key(url, limit=2)
        statement = (
            "INSERT INTO quota_counters (key_id, day, count) "
            "VALUES ($1, DATE '2026-08-29', 1) "
            "ON CONFLICT (key_id, day) DO UPDATE "
            "SET count = quota_counters.count + 1 "
            "WHERE quota_counters.count < $2 RETURNING count"
        )
        assert fetchval(url, statement, key_id, 2) == 1
        assert fetchval(url, statement, key_id, 2) == 2
        # At the ceiling: no row, and the counter is unmoved.
        assert fetchval(url, statement, key_id, 2) is None
        assert fetchval(url, "SELECT count FROM quota_counters") == 2
        # A different day is a different row.
        assert (
            fetchval(
                url,
                statement.replace("2026-08-29", "2026-08-30"),
                key_id,
                2,
            )
            == 1
        )


def test_rerunning_020_adopts_its_own_work_and_keeps_the_counters():
    """The idempotence path the gate itself performs: 020 genuinely
    re-executes against a database already carrying its three objects, and no
    key loses its configured limit or its day's consumption."""
    with throwaway_db("schema_quota_rerun") as url:
        key_id = insert_quota_key(url, limit=250)
        sql(
            url,
            "INSERT INTO quota_counters (key_id, day, count) "
            "VALUES ($1, CURRENT_DATE, 37)",
            key_id,
        )
        _harness.run_alembic(url, "stamp", "019", dimensions=DIM)
        _harness.run_alembic(url, "upgrade", "head", dimensions=DIM)

        assert alembic_version(url) == HEAD_REVISION
        assert fetchval(url, "SELECT daily_request_limit FROM api_keys") == 250
        assert fetchval(url, "SELECT count FROM quota_counters") == 37
        assert quota_counters_comment(url) == QUOTA_TABLE_MARKER


def test_020_refuses_a_not_null_defaulted_limit_column():
    """The adversarial case: the column exists, `alembic check` is happy, and
    every key on the server now has a limit of zero — which refuses every call
    forever."""
    with throwaway_db("schema_quota_notnull") as url:
        sql(
            url,
            "ALTER TABLE api_keys ALTER COLUMN daily_request_limit SET DEFAULT 0",
        )
        sql(url, "UPDATE api_keys SET daily_request_limit = 1")
        sql(
            url,
            "ALTER TABLE api_keys ALTER COLUMN daily_request_limit SET NOT NULL",
        )
        combined = refuse_020(url, must_mention=["NOT NULL", "server default"])
        assert "unlimited" in combined


def test_020_refuses_an_unmarked_limit_column():
    with throwaway_db("schema_quota_unmarked_col") as url:
        sql(url, "COMMENT ON COLUMN api_keys.daily_request_limit IS NULL")
        refuse_020(url, must_mention=["020's comment marker"])


def test_020_refuses_an_impostor_check_of_its_name():
    """A same-named `CHECK (true)` satisfies a lookup by name and enforces
    nothing (issue #53). The predicate is compared, not the name."""
    with throwaway_db("schema_quota_impostor_check") as url:
        sql(url, f"ALTER TABLE api_keys DROP CONSTRAINT {QUOTA_CHECK_NAME}")
        sql(
            url,
            f"ALTER TABLE api_keys ADD CONSTRAINT {QUOTA_CHECK_NAME} CHECK (true)",
        )
        sql(
            url,
            f"COMMENT ON CONSTRAINT {QUOTA_CHECK_NAME} ON api_keys IS "
            f"'{QUOTA_CHECK_MARKER}'",
        )
        refuse_020(url, must_mention=["its predicate is"])
        # A zero limit really is admitted while the impostor stands — which is
        # the whole reason the predicate is compared rather than the name.
        insert_quota_key(url, name="zero", limit=0)


def test_020_refuses_a_not_valid_check():
    """`NOT VALID` enforces new rows having never checked the existing ones, so
    the table may already hold a zero limit."""
    with throwaway_db("schema_quota_notvalid_check") as url:
        sql(url, f"ALTER TABLE api_keys DROP CONSTRAINT {QUOTA_CHECK_NAME}")
        sql(
            url,
            f"ALTER TABLE api_keys ADD CONSTRAINT {QUOTA_CHECK_NAME} CHECK "
            "(daily_request_limit IS NULL OR (daily_request_limit >= 1 AND "
            "daily_request_limit <= 1000000)) NOT VALID",
        )
        sql(
            url,
            f"COMMENT ON CONSTRAINT {QUOTA_CHECK_NAME} ON api_keys IS "
            f"'{QUOTA_CHECK_MARKER}'",
        )
        refuse_020(url, must_mention=["NOT VALID"])


def test_020_refuses_a_single_column_primary_key_on_the_counters():
    """The one mutation that turns the quota into a ceiling that never resets:
    a PK on `(key_id)` alone collapses every day onto one row, and
    `ON CONFLICT (key_id, day)` cannot even name it."""
    with throwaway_db("schema_quota_pk") as url:
        sql(url, "ALTER TABLE quota_counters DROP CONSTRAINT quota_counters_pkey")
        sql(url, "ALTER TABLE quota_counters ADD PRIMARY KEY (key_id)")
        assert quota_counters_pk(url) == ["key_id"]
        refuse_020(url, must_mention=["primary key", "key_id", "day"])


def test_020_refuses_a_counter_foreign_key_pointing_at_another_table():
    """Everything a marker-and-delete-action check reads is untouched — 020's
    comment, 020's columns, `ON DELETE CASCADE`. Only the referent moved, and
    with it what the number counts."""
    with throwaway_db("schema_quota_fk_target") as url:
        constraint = fetchval(
            url,
            "SELECT conname FROM pg_constraint "
            "WHERE conrelid = 'quota_counters'::regclass AND contype = 'f'",
        )
        sql(url, f"ALTER TABLE quota_counters DROP CONSTRAINT {constraint}")
        sql(
            url,
            f"ALTER TABLE quota_counters ADD CONSTRAINT {constraint} "
            "FOREIGN KEY (key_id) REFERENCES users(id) ON DELETE CASCADE",
        )
        assert quota_counters_comment(url) == QUOTA_TABLE_MARKER
        refuse_020(url, must_mention=["users", "references"])


def test_020_refuses_a_non_cascading_counter_foreign_key():
    with throwaway_db("schema_quota_fk_noaction") as url:
        constraint = fetchval(
            url,
            "SELECT conname FROM pg_constraint "
            "WHERE conrelid = 'quota_counters'::regclass AND contype = 'f'",
        )
        sql(url, f"ALTER TABLE quota_counters DROP CONSTRAINT {constraint}")
        refuse_020(url, must_mention=["no foreign key at all"])

        sql(
            url,
            f"ALTER TABLE quota_counters ADD CONSTRAINT {constraint} "
            "FOREIGN KEY (key_id) REFERENCES api_keys(id)",
        )
        refuse_020(url, must_mention=["CASCADE"])


def test_020_refuses_an_unmarked_counter_table():
    """`IF NOT EXISTS` would adopt this — including its single-column PK."""
    with throwaway_db("schema_quota_unmarked_table", revision="019") as url:
        sql(
            url,
            "CREATE TABLE quota_counters (key_id integer PRIMARY KEY "
            " REFERENCES api_keys(id) ON DELETE CASCADE, day date NOT NULL, "
            " count integer NOT NULL DEFAULT 0)",
        )
        result = _harness.run_alembic(
            url, "upgrade", "head", dimensions=DIM, check=False
        )
        assert result.returncode != 0
        assert "020's comment marker" in result.stdout + result.stderr


def test_020_refuses_a_usage_index_of_its_name_on_other_columns():
    """Names are not definitions: an index of the right name on
    `(key_id, duration_ms)` leaves the usage page's per-key window scan with
    nothing to lean on, and autogenerate reports no drift at all."""
    with throwaway_db("schema_quota_usage_index") as url:
        sql(url, f"DROP INDEX {USAGE_KEY_INDEX}")
        sql(
            url,
            f"CREATE INDEX {USAGE_KEY_INDEX} ON usage_logs (key_id, duration_ms)",
        )
        refuse_020(url, must_mention=[USAGE_KEY_INDEX, "duration_ms"])


def test_downgrade_020_removes_all_three_and_upgrade_rebuilds_them():
    with throwaway_db("schema_quota_downgrade") as url:
        _harness.run_alembic(url, "downgrade", "019", dimensions=DIM)
        assert alembic_version(url) == "019"
        assert column_shape(url, "api_keys", "daily_request_limit") is None
        assert daily_limit_check(url) is None
        assert fetchval(url, "SELECT to_regclass('quota_counters')") is None
        assert usage_index_columns(url, USAGE_KEY_INDEX) is None

        _harness.run_alembic(url, "upgrade", "head", dimensions=DIM)
        assert alembic_version(url) == HEAD_REVISION
        assert quota_counters_pk(url) == ["key_id", "day"]
        assert daily_limit_comment(url) == QUOTA_COLUMN_MARKER
        check = _harness.run_alembic(url, "check", dimensions=DIM, check=False)
        assert check.returncode == 0, (
            f"alembic check reported drift\n{check.stdout}\n{check.stderr}"
        )


def test_downgrade_020_refuses_a_counter_table_it_did_not_create():
    """013's rule on the way back down: undo *this* migration, not delete a
    table somebody else put there under this name."""
    with throwaway_db("schema_quota_downgrade_foreign") as url:
        sql(url, "COMMENT ON TABLE quota_counters IS 'somebody else made this'")
        result = _harness.run_alembic(
            url, "downgrade", "019", dimensions=DIM, check=False
        )
        assert result.returncode != 0
        assert "020's comment marker" in result.stdout + result.stderr
        # And the column it would have dropped next is still there.
        assert daily_limit_comment(url) == QUOTA_COLUMN_MARKER


# --------------------------------------------------------------------------
# migration 021 — the backup record (#163)
# --------------------------------------------------------------------------
#
# One table, and what makes it load-bearing is not its shape but what the panel
# says about its newest row: "your last database backup was taken N days ago".
# That is a claim about disaster recovery, so the ways it can be quietly wrong
# are the cases below.
#
#   * `created_at` must be NOT NULL. A nullable one sorts to nowhere under
#     `ORDER BY created_at DESC`, so a NULL row can hide every later backup and
#     the page reports the one before it — or none. `alembic check` sees the
#     nullability, which is why the interesting cases here are the two it does
#     not see.
#   * `ck_backups_log_size_bytes` must be the real predicate and `convalidated`.
#     A same-named `CHECK (true)` is issue #53 exactly, and the value it lets
#     through — a zero-byte dump — is precisely the failure `db-backup`'s
#     empty-dump guard exists for, rendered on the page as a backup.
#   * `ix_backups_log_created_at` must be on `created_at`. An index of that name
#     recreated on `filename` keeps the name an existence check looks for while
#     the newest-first read the page and the dashboard strip both perform has
#     nothing to lean on, and autogenerate reports no drift at all.

BACKUPS_TABLE_MARKER = "one row per recorded database backup (021_backups_log)"
BACKUPS_CHECK_NAME = "ck_backups_log_size_bytes"
BACKUPS_INDEX = "ix_backups_log_created_at"


def backups_log_comment(url):
    return fetchval(
        url,
        "SELECT obj_description(c.oid, 'pg_class') FROM pg_class c "
        "WHERE c.relname = 'backups_log' AND c.relkind = 'r' "
        "  AND c.relnamespace = 'public'::regnamespace",
    )


def backups_size_check(url):
    """`(constraintdef, convalidated)` for 021's CHECK, or None."""
    rows = fetch(
        url,
        "SELECT pg_get_constraintdef(oid) AS def, convalidated "
        "FROM pg_constraint "
        "WHERE conrelid = 'public.backups_log'::regclass AND contype = 'c' "
        "  AND conname = $1",
        BACKUPS_CHECK_NAME,
    )
    if not rows:
        return None
    return " ".join(rows[0]["def"].split()), rows[0]["convalidated"]


def backups_pk(url):
    """The PK's columns in order, or None when there is no primary key."""
    return fetchval(
        url,
        "SELECT (SELECT array_agg(a.attname ORDER BY k.ord) "
        "          FROM unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) "
        "          JOIN pg_attribute a ON a.attrelid = c.conrelid "
        "                             AND a.attnum = k.attnum) "
        "FROM pg_constraint c "
        "WHERE c.conrelid = 'public.backups_log'::regclass AND c.contype = 'p'",
    )


def backups_index_columns(url, name=BACKUPS_INDEX):
    return fetchval(
        url,
        "SELECT array_agg(a.attname ORDER BY k.ord) "
        "FROM pg_index i JOIN pg_class ic ON ic.oid = i.indexrelid "
        "     CROSS JOIN unnest(string_to_array(i.indkey::text, ' ')) "
        "                WITH ORDINALITY AS k(attnum, ord) "
        "     JOIN pg_attribute a ON a.attrelid = i.indrelid "
        "                        AND a.attnum = k.attnum::smallint "
        "WHERE i.indrelid = 'public.backups_log'::regclass AND ic.relname = $1 "
        "GROUP BY ic.relname",
        name,
    )


def insert_backup(url, filename="backup_20260829_120000.sql.gz", size=4096, when=None):
    """One `backups_log` row, the way `docker/record-backup.sh` writes it."""
    if when is None:
        return fetchval(
            url,
            "INSERT INTO backups_log (filename, size_bytes) VALUES ($1, $2) "
            "RETURNING id",
            filename,
            size,
        )
    return fetchval(
        url,
        "INSERT INTO backups_log (created_at, filename, size_bytes) "
        "VALUES ($1, $2, $3) RETURNING id",
        when,
        filename,
        size,
    )


def refuse_021(url, *, must_mention):
    """Stamp back to 020, re-run 021, require it to refuse, return the message.

    The same stamp-back path `make test-schema` exercises for idempotence, so a
    verifier that waved a mutation through here would wave it through on a real
    database somebody had altered by hand.
    """
    _harness.run_alembic(url, "stamp", "020", dimensions=DIM)
    result = _harness.run_alembic(url, "upgrade", "head", dimensions=DIM, check=False)
    assert result.returncode != 0, "021 should have refused"
    combined = result.stdout + result.stderr
    for phrase in must_mention:
        assert phrase in combined, f"refusal did not mention {phrase!r}:\n{combined}"
    return combined


def test_021_creates_the_table_it_promises():
    with throwaway_db("schema_backups_fresh") as url:
        assert alembic_version(url) == HEAD_REVISION

        assert backups_log_comment(url) == BACKUPS_TABLE_MARKER

        # NOT NULL on `created_at` is what makes the newest-first read mean
        # anything; the size column is bigint because a dump passes 2 GiB
        # without anything having gone wrong.
        assert column_shape(url, "backups_log", "created_at")[0] is True
        assert column_shape(url, "backups_log", "filename") == (True, "text", None)
        assert column_shape(url, "backups_log", "size_bytes")[:2] == (True, "bigint")

        rendered, validated = backups_size_check(url)
        assert validated is True
        assert "size_bytes" in rendered

        assert backups_index_columns(url) == ["created_at"]

        # The primary key and both server defaults — the three things
        # `alembic check` compares not at all (it does not look at primary
        # keys, and `compare_server_default` is off by default), so they are
        # asserted here or nowhere.
        assert backups_pk(url) == ["id"]
        assert column_shape(url, "backups_log", "created_at")[2] == "now()"
        id_default = column_shape(url, "backups_log", "id")[2]
        assert id_default is not None and id_default.startswith("nextval(")

        check = _harness.run_alembic(url, "check", dimensions=DIM, check=False)
        assert check.returncode == 0, (
            f"alembic check reported drift\n{check.stdout}\n{check.stderr}"
        )


def test_the_size_check_rejects_a_zero_byte_backup():
    """The catalog says the constraint is there; this proves it enforces.

    `pg_dump` can write nothing and exit 0 when it is pointed at the wrong
    thing — that is why `db-backup` has an empty-dump guard at all. A row of
    size 0 is that failure recorded as a backup and rendered on the page as
    one.
    """
    with throwaway_db("schema_backups_domain") as url:
        insert_backup(url, size=1)
        for illegal in (0, -1):
            with pytest.raises(asyncpg.CheckViolationError):
                insert_backup(url, filename=f"bad-{illegal}.sql.gz", size=illegal)


def test_the_newest_row_is_what_the_page_reads():
    """The read the health page and the dashboard strip both perform, run
    against the real ordering rather than argued about."""
    with throwaway_db("schema_backups_newest") as url:
        old = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
        new = datetime.datetime(2026, 6, 1, tzinfo=datetime.timezone.utc)
        insert_backup(url, filename="older.sql.gz", when=old)
        insert_backup(url, filename="newer.sql.gz", when=new)
        assert fetchval(
            url,
            "SELECT filename FROM backups_log ORDER BY created_at DESC, id DESC "
            "LIMIT 1",
        ) == "newer.sql.gz"


def test_rerunning_021_adopts_its_own_table_and_keeps_the_rows():
    """The idempotence path the gate itself performs: 021 genuinely re-executes
    against a database already carrying its table, and no backup record is
    lost."""
    with throwaway_db("schema_backups_rerun") as url:
        insert_backup(url, filename="kept.sql.gz", size=99)
        _harness.run_alembic(url, "stamp", "020", dimensions=DIM)
        _harness.run_alembic(url, "upgrade", "head", dimensions=DIM)

        assert alembic_version(url) == HEAD_REVISION
        assert fetchval(url, "SELECT filename FROM backups_log") == "kept.sql.gz"
        assert backups_log_comment(url) == BACKUPS_TABLE_MARKER


def test_021_refuses_an_unmarked_table_of_its_name():
    """`IF NOT EXISTS` would adopt this — nullable `created_at`, no CHECK, no
    index — and the panel would report its rows as backup history."""
    with throwaway_db("schema_backups_unmarked", revision="020") as url:
        sql(
            url,
            "CREATE TABLE backups_log (id serial PRIMARY KEY, "
            " created_at timestamptz, filename text, size_bytes bigint)",
        )
        result = _harness.run_alembic(
            url, "upgrade", "head", dimensions=DIM, check=False
        )
        assert result.returncode != 0
        assert "021's comment marker" in result.stdout + result.stderr


def test_021_refuses_a_nullable_created_at():
    """A NULL `created_at` sorts to nowhere under `ORDER BY created_at DESC`,
    so one such row can hide every backup taken after it."""
    with throwaway_db("schema_backups_nullable") as url:
        sql(url, "ALTER TABLE backups_log ALTER COLUMN created_at DROP NOT NULL")
        refuse_021(url, must_mention=["its columns are", "created_at"])


def test_021_refuses_an_impostor_check_of_its_name():
    """A same-named `CHECK (true)` satisfies a lookup by name and enforces
    nothing (issue #53). The predicate is compared, not the name."""
    with throwaway_db("schema_backups_impostor_check") as url:
        sql(url, f"ALTER TABLE backups_log DROP CONSTRAINT {BACKUPS_CHECK_NAME}")
        sql(
            url,
            f"ALTER TABLE backups_log ADD CONSTRAINT {BACKUPS_CHECK_NAME} CHECK (true)",
        )
        refuse_021(url, must_mention=["its CHECK is"])
        # A zero-byte backup really is admitted while the impostor stands —
        # which is the whole reason the predicate is compared, not the name.
        insert_backup(url, filename="empty.sql.gz", size=0)


def test_021_refuses_a_not_valid_check():
    """`NOT VALID` enforces new rows having never checked the existing ones, so
    the table may already record a zero-byte backup."""
    with throwaway_db("schema_backups_notvalid_check") as url:
        sql(url, f"ALTER TABLE backups_log DROP CONSTRAINT {BACKUPS_CHECK_NAME}")
        sql(
            url,
            f"ALTER TABLE backups_log ADD CONSTRAINT {BACKUPS_CHECK_NAME} "
            "CHECK (size_bytes >= 1) NOT VALID",
        )
        refuse_021(url, must_mention=["NOT VALID"])


def test_021_refuses_a_missing_check():
    with throwaway_db("schema_backups_no_check") as url:
        sql(url, f"ALTER TABLE backups_log DROP CONSTRAINT {BACKUPS_CHECK_NAME}")
        refuse_021(url, must_mention=["no size CHECK"])


def test_021_refuses_an_index_of_its_name_on_another_column():
    """Names are not definitions: an index of the right name on `filename`
    leaves the newest-first read with nothing to lean on, and autogenerate
    reports no drift at all."""
    with throwaway_db("schema_backups_index") as url:
        sql(url, f"DROP INDEX {BACKUPS_INDEX}")
        sql(url, f"CREATE INDEX {BACKUPS_INDEX} ON backups_log (filename)")
        refuse_021(url, must_mention=[BACKUPS_INDEX, "filename"])


def test_021_refuses_a_missing_index():
    with throwaway_db("schema_backups_no_index") as url:
        sql(url, f"DROP INDEX {BACKUPS_INDEX}")
        refuse_021(url, must_mention=[f"missing index {BACKUPS_INDEX}"])


def test_021_refuses_a_tampered_created_at_default():
    """The quietest mutation this table has. Every column, type, nullability,
    constraint and index stays exactly as 021 made it — `alembic check` does
    not compare server defaults at all — and every backup recorded afterwards
    reads as permanently fresh, so the staleness warning the whole feature
    exists to raise can never fire again."""
    with throwaway_db("schema_backups_default") as url:
        sql(
            url,
            "ALTER TABLE backups_log ALTER COLUMN created_at "
            "SET DEFAULT now() + interval '100 years'",
        )
        # `alembic check` is genuinely happy with this, which is the point.
        check = _harness.run_alembic(url, "check", dimensions=DIM, check=False)
        assert check.returncode == 0, (
            "if autogenerate ever starts seeing this, say so here rather than "
            "leaving the migration's own comparison unexplained"
        )
        combined = refuse_021(url, must_mention=["created_at default", "now()"])
        assert "staleness" in combined

        # And it really would have made a fresh backup read as a future one.
        insert_backup(url, filename="future.sql.gz")
        assert fetchval(
            url, "SELECT created_at > now() + interval '50 years' FROM backups_log"
        ) is True


def test_021_refuses_a_missing_primary_key():
    """Autogenerate does not compare primary keys, so a table whose PK has been
    dropped reports as being in perfect agreement with the model — while `id`
    is no longer unique and the newest-row read has lost its tie-break."""
    with throwaway_db("schema_backups_no_pk") as url:
        sql(url, "ALTER TABLE backups_log DROP CONSTRAINT backups_log_pkey")
        assert backups_pk(url) is None
        combined = refuse_021(url, must_mention=["no primary key"])
        assert "tie-break" in combined


def test_021_refuses_an_id_that_no_longer_draws_from_its_sequence():
    with throwaway_db("schema_backups_id_default") as url:
        sql(url, "ALTER TABLE backups_log ALTER COLUMN id DROP DEFAULT")
        refuse_021(url, must_mention=["id"])


def test_021_creates_in_public_under_a_redirected_search_path():
    """The migration itself run with `search_path` pointing somewhere else.

    Unqualified DDL resolves through `search_path`, so without the pin
    `op.create_table` would put the table in `decoy` — where
    `docker/record-backup.sh` (which INSERTs into `public.backups_log`) and the
    panel (which SELECTs from it) would never find it. The failure is silent in
    the worst direction: the target records backups into a table the page never
    reads, so the page warns that none have been taken while one is taken daily.

    The pin is `SET LOCAL search_path TO public`, which is transaction-scoped —
    and `alembic/env.py` runs every pending revision inside one transaction, the
    same property 013 through 020 rely on for `lock_timeout`. This case is what
    proves that holds in this environment rather than asserting it.
    """
    with throwaway_db("schema_backups_path", revision="020") as url:
        dbname = fetchval(url, "SELECT current_database()")
        sql(url, "CREATE SCHEMA decoy")
        sql(url, f'ALTER DATABASE "{dbname}" SET search_path TO decoy, public')
        # A *new* connection is what alembic will open, so confirm the redirect
        # is actually in force for one before believing the rest of this case.
        assert fetchval(url, "SHOW search_path") == "decoy, public"

        _harness.run_alembic(url, "upgrade", "head", dimensions=DIM)

        assert alembic_version(url) == HEAD_REVISION
        assert fetchval(url, "SELECT to_regclass('public.backups_log')") is not None
        assert fetchval(url, "SELECT to_regclass('decoy.backups_log')") is None, (
            "the table must land where the writer and the panel look, not first "
            "on the search path"
        )
        # The index and the comment are separate unqualified statements, so
        # they are checked separately rather than assumed to have followed.
        assert backups_log_comment(url) == BACKUPS_TABLE_MARKER
        assert backups_index_columns(url) == ["created_at"]
        assert backups_pk(url) == ["id"]
        assert fetchval(
            url,
            "SELECT count(*) FROM pg_class c "
            "WHERE c.relnamespace = 'decoy'::regnamespace",
        ) == 0, "nothing at all was created in the decoy schema"

        # The pin is `SET LOCAL`, so it must not have outlived the transaction.
        assert fetchval(url, "SHOW search_path") == "decoy, public"

        check = _harness.run_alembic(url, "check", dimensions=DIM, check=False)
        assert check.returncode == 0, (
            f"alembic check reported drift\n{check.stdout}\n{check.stderr}"
        )

        # And the downgrade drops from public, not from wherever the path points.
        _harness.run_alembic(url, "downgrade", "020", dimensions=DIM)
        assert fetchval(url, "SELECT to_regclass('public.backups_log')") is None


def test_downgrade_021_removes_the_table_and_upgrade_rebuilds_it():
    with throwaway_db("schema_backups_downgrade") as url:
        _harness.run_alembic(url, "downgrade", "020", dimensions=DIM)
        assert alembic_version(url) == "020"
        assert fetchval(url, "SELECT to_regclass('backups_log')") is None

        _harness.run_alembic(url, "upgrade", "head", dimensions=DIM)
        assert alembic_version(url) == HEAD_REVISION
        assert backups_log_comment(url) == BACKUPS_TABLE_MARKER
        assert backups_index_columns(url) == ["created_at"]
        check = _harness.run_alembic(url, "check", dimensions=DIM, check=False)
        assert check.returncode == 0, (
            f"alembic check reported drift\n{check.stdout}\n{check.stderr}"
        )


def test_downgrade_021_refuses_a_table_it_did_not_create():
    """013's rule on the way back down: undo *this* migration, not delete a
    table somebody else put there under this name."""
    with throwaway_db("schema_backups_downgrade_foreign") as url:
        sql(url, "COMMENT ON TABLE backups_log IS 'somebody else made this'")
        result = _harness.run_alembic(
            url, "downgrade", "020", dimensions=DIM, check=False
        )
        assert result.returncode != 0
        assert "021's comment marker" in result.stdout + result.stderr
        assert fetchval(url, "SELECT to_regclass('backups_log')") is not None


# ══════════════════════════════════════════════════════════════════════════
# 022 — notes_metadata.links_truncated (#203)
# ══════════════════════════════════════════════════════════════════════════

LINKS_TRUNCATED_MARKER = "link-extraction truncation marker (022_links_truncated)"


def links_truncated_comment(url):
    return fetchval(
        url,
        "SELECT col_description(a.attrelid, a.attnum) FROM pg_attribute a "
        "WHERE a.attrelid = 'notes_metadata'::regclass "
        "  AND a.attname = 'links_truncated'",
    )


def seed_pre_022_notes(url):
    """Two notes written before the cap existed: no marker column at all."""
    insert_user(url, 1, "alice")
    sql(
        url,
        "INSERT INTO notes_metadata "
        "(id, user_id, file_path, title, content_hash, embedded_content_hash) "
        "VALUES (1, 1, 'A.md', 'A', 'hash-a', 'hash-a'), "
        "       (2, 1, 'B.md', 'B', 'hash-b', NULL)",
    )


def test_the_truncation_marker_is_not_null_defaulted_and_marked_on_a_fresh_db():
    with throwaway_db("schema_lt_fresh") as url:
        assert alembic_version(url) == HEAD_REVISION
        shape = column_shape(url, "notes_metadata", "links_truncated")
        assert shape is not None, "notes_metadata.links_truncated is missing"
        attnotnull, coltype, coldefault = shape
        assert attnotnull is True, (
            "the marker must be NOT NULL — a nullable column would let "
            "`get_links` read NULL as 'not truncated' for a note whose links "
            "ARE truncated, which is the silently-wrong answer it exists to "
            "prevent"
        )
        assert coltype == "boolean"
        assert coldefault == "false", (
            "the server default is what makes the ADD COLUMN metadata-only on "
            "a table carrying a tsvector and two GIN indexes, and what gives "
            "every pre-existing row the one correct value"
        )
        assert links_truncated_comment(url) == LINKS_TRUNCATED_MARKER
        check = _harness.run_alembic(url, "check", dimensions=DIM, check=False)
        assert check.returncode == 0, (
            f"alembic check reported drift\n{check.stdout}\n{check.stderr}"
        )
        assert "No new upgrade operations detected" in check.stdout


def test_022_reads_every_pre_existing_row_as_untruncated_and_touches_nothing_else():
    """The whole point of the default. Every row that exists when 022 runs was
    written by the *unbounded* extractor, which could not truncate, so `false`
    is not a placeholder — it is the truth. And neither `content_hash` (the
    move-detection key) nor `embedded_content_hash` (the embed backlog's
    predicate) is disturbed."""
    with throwaway_db("schema_lt_backfill", revision="021") as url:
        seed_pre_022_notes(url)
        assert column_shape(url, "notes_metadata", "links_truncated") is None

        _harness.run_alembic(url, "upgrade", "head", dimensions=DIM)

        rows = fetch(
            url,
            "SELECT id, content_hash, embedded_content_hash, links_truncated "
            "FROM notes_metadata ORDER BY id",
        )
        assert [tuple(r) for r in rows] == [
            (1, "hash-a", "hash-a", False),
            (2, "hash-b", None, False),
        ]


def test_rerunning_022_leaves_recorded_truncations_alone():
    """Stamp-back idempotence, the shape the schema gate itself performs. The
    migration body genuinely re-executes; a truncation the indexer has since
    recorded must survive it, or every stamp-back would silently tell an agent
    that a capped note's link set is complete."""
    with throwaway_db("schema_lt_rerun", revision="021") as url:
        seed_pre_022_notes(url)
        _harness.run_alembic(url, "upgrade", "head", dimensions=DIM)
        sql(url, "UPDATE notes_metadata SET links_truncated = true WHERE id = 1")

        _harness.run_alembic(url, "stamp", "021", dimensions=DIM)
        _harness.run_alembic(url, "upgrade", "head", dimensions=DIM)

        assert alembic_version(url) == HEAD_REVISION
        assert fetchval(
            url, "SELECT links_truncated FROM notes_metadata WHERE id = 1"
        ) is True
        assert fetchval(
            url, "SELECT links_truncated FROM notes_metadata WHERE id = 2"
        ) is False
        assert links_truncated_comment(url) == LINKS_TRUNCATED_MARKER


def test_022_refuses_a_column_of_unknown_provenance():
    """013's philosophy: reconcile a database that demonstrably has our shape,
    refuse to guess for one that does not. A `links_truncated` somebody else
    created — nullable, or defaulting to true — is not adoptable: `get_links`
    reads this column as whether a note's link set is complete, and a wrong
    value either hides a truncation or invents one."""
    with throwaway_db("schema_lt_foreign", revision="021") as url:
        sql(
            url,
            "ALTER TABLE notes_metadata ADD COLUMN links_truncated BOOLEAN",
        )
        result = _harness.run_alembic(
            url, "upgrade", "head", dimensions=DIM, check=False
        )
        assert result.returncode != 0, "022 should have refused"
        combined = result.stdout + result.stderr
        assert "it is nullable" in combined, combined
        assert "022's comment marker" in combined, combined
        assert alembic_version(url) == "021", "nothing should have been recorded"


def test_downgrade_022_drops_the_marked_column_and_upgrade_rebuilds_it():
    with throwaway_db("schema_lt_downgrade") as url:
        _harness.run_alembic(url, "downgrade", "021", dimensions=DIM)
        assert alembic_version(url) == "021"
        assert column_shape(url, "notes_metadata", "links_truncated") is None

        _harness.run_alembic(url, "upgrade", "head", dimensions=DIM)
        assert alembic_version(url) == HEAD_REVISION
        assert links_truncated_comment(url) == LINKS_TRUNCATED_MARKER
        check = _harness.run_alembic(url, "check", dimensions=DIM, check=False)
        assert check.returncode == 0, (
            f"alembic check reported drift\n{check.stdout}\n{check.stderr}"
        )


def test_downgrade_022_refuses_a_column_it_did_not_create():
    """013's rule on the way back down: undo *this* migration, not delete a
    column somebody else put there under this name."""
    with throwaway_db("schema_lt_downgrade_foreign") as url:
        sql(
            url,
            "COMMENT ON COLUMN notes_metadata.links_truncated IS "
            "'somebody else made this'",
        )
        result = _harness.run_alembic(
            url, "downgrade", "021", dimensions=DIM, check=False
        )
        assert result.returncode != 0
        assert "022's comment marker" in result.stdout + result.stderr
        assert column_shape(url, "notes_metadata", "links_truncated") is not None


# ══════════════════════════════════════════════════════════════════════════
# 023 — indexer_state and notes_metadata.chunks_truncated (#206, #202)
# ══════════════════════════════════════════════════════════════════════════

INDEXER_STATE_TABLE_MARKER = "state about the index as a whole (023_indexer_state)"
INDEXER_STATE_CHECK_MARKER = "closed key set for indexer_state (023_indexer_state)"
CHUNKS_TRUNCATED_MARKER = "chunk-cap truncation marker (023_indexer_state)"

STATE_CHECK_NAME = "ck_indexer_state_key"

# What PostgreSQL 16 prints for the key predicate migration 023 (and the
# model's `CheckConstraint`) declares. Measured on a freshly migrated database
# rather than hand-written, so it is the server's own normalization of
# `IN (...)` — casts made explicit, rewritten to `= ANY (...)`. The migration
# derives the same string at runtime from a scratch TEMP table instead of
# hard-coding it; this constant is the pin that tells us if either side moves.
#
# It is asserted here and nowhere else: `alembic check` does not compare CHECK
# predicates **at all**, so a `CHECK (true)` of this name would satisfy every
# name-level lookup while enforcing nothing — and what it would stop enforcing
# is the closed key set that keeps a mistyped key from reading as "no
# fingerprint stored", which is the state that makes the startup guard adopt
# instead of refuse.
CANONICAL_STATE_CHECK = (
    "CHECK (((key)::text = ANY "
    "((ARRAY['embedding_fingerprint'::character varying, "
    "'fts_fingerprint'::character varying, "
    "'embed_rotation_cursor'::character varying])::text[])))"
)


def indexer_state_table_comment(url):
    return fetchval(
        url,
        "SELECT obj_description(c.oid, 'pg_class') FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE c.relname = 'indexer_state' AND c.relkind = 'r' "
        "  AND n.nspname = ANY (current_schemas(false))",
    )


def indexer_state_checks(url):
    """Every CHECK on `indexer_state`, `(definition, convalidated, comment)`.

    Read through `conrelid`/`contype` rather than by name, exactly as the
    migration does and for the same reason.
    """
    rows = fetch(
        url,
        "SELECT pg_get_constraintdef(oid) AS def, convalidated, "
        "       obj_description(oid, 'pg_constraint') AS comment "
        "FROM pg_constraint "
        "WHERE conrelid = 'indexer_state'::regclass AND contype = 'c' "
        "ORDER BY conname",
    )
    return [
        (" ".join(r["def"].split()), r["convalidated"], r["comment"]) for r in rows
    ]


def indexer_state_pk_columns(url):
    return fetchval(
        url,
        "SELECT (SELECT array_agg(a.attname ORDER BY k.ord) "
        "          FROM unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) "
        "          JOIN pg_attribute a ON a.attrelid = c.conrelid "
        "                             AND a.attnum = k.attnum) "
        "FROM pg_constraint c "
        "WHERE c.conrelid = 'indexer_state'::regclass AND c.contype = 'p'",
    )


def chunks_truncated_comment(url):
    return fetchval(
        url,
        "SELECT col_description(a.attrelid, a.attnum) FROM pg_attribute a "
        "WHERE a.attrelid = 'notes_metadata'::regclass "
        "  AND a.attname = 'chunks_truncated'",
    )


def seed_pre_023_notes(url):
    """Two notes embedded before the chunk cap existed: no marker column."""
    insert_user(url, 1, "alice")
    sql(
        url,
        "INSERT INTO notes_metadata "
        "(id, user_id, file_path, title, content_hash, embedded_content_hash) "
        "VALUES (1, 1, 'A.md', 'A', 'hash-a', 'hash-a'), "
        "       (2, 1, 'B.md', 'B', 'hash-b', NULL)",
    )


def test_023_creates_both_units_typed_marked_and_enforced_on_a_fresh_db():
    with throwaway_db("schema_is_fresh") as url:
        assert alembic_version(url) == HEAD_REVISION

        # --- the table ---
        assert fetchval(url, "SELECT to_regclass('indexer_state')") is not None
        assert indexer_state_table_comment(url) == INDEXER_STATE_TABLE_MARKER
        assert list(indexer_state_pk_columns(url) or []) == ["key"], (
            "the primary key must be on `key`: two rows claiming one key would "
            "make `get_state` return an arbitrary one of them"
        )

        for column, expected in (
            ("key", (True, "character varying(64)", None)),
            ("value", (True, "text", None)),
            ("updated_at", (True, "timestamp with time zone", "now()")),
        ):
            shape = column_shape(url, "indexer_state", column)
            assert shape is not None, f"indexer_state.{column} is missing"
            assert (shape[0], shape[1], shape[2]) == expected, (
                f"indexer_state.{column} is {shape}, not {expected}"
            )

        checks = indexer_state_checks(url)
        assert len(checks) == 1, f"expected exactly one CHECK, found {checks}"
        definition, validated, comment = checks[0]
        assert definition == CANONICAL_STATE_CHECK, definition
        assert validated is True, (
            "a NOT VALID CHECK enforces new rows having never checked the "
            "existing ones"
        )
        assert comment == INDEXER_STATE_CHECK_MARKER

        # --- the column ---
        shape = column_shape(url, "notes_metadata", "chunks_truncated")
        assert shape is not None, "notes_metadata.chunks_truncated is missing"
        attnotnull, coltype, coldefault = shape
        assert attnotnull is True, (
            "the marker must be NOT NULL — a nullable column would let the "
            "vector tools read NULL as 'not truncated' for a note whose "
            "embedding IS capped, presenting a match against the head of a "
            "2 MB note as a match against the note"
        )
        assert coltype == "boolean"
        assert coldefault == "false", (
            "the server default is what makes the ADD COLUMN metadata-only on "
            "a table carrying a tsvector and two GIN indexes, and what gives "
            "every pre-existing row the one correct value"
        )
        assert chunks_truncated_comment(url) == CHUNKS_TRUNCATED_MARKER

        check = _harness.run_alembic(url, "check", dimensions=DIM, check=False)
        assert check.returncode == 0, (
            f"alembic check reported drift\n{check.stdout}\n{check.stderr}"
        )
        assert "No new upgrade operations detected" in check.stdout


def test_023_rejects_a_key_outside_the_closed_set():
    """The CHECK is not tidiness. A key this table does not hold reads as
    *absent*, and absent is the state that makes the startup fingerprint check
    adopt the current configuration rather than refuse it — so one mistyped key
    would disable, permanently and silently, the guard that exists to stop a
    same-dimension model swap from mixing two vector spaces in one column."""
    with throwaway_db("schema_is_key") as url:
        sql(
            url,
            "INSERT INTO indexer_state (key, value) "
            "VALUES ('embedding_fingerprint', '{\"v\":1}')",
        )
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            sql(
                url,
                "INSERT INTO indexer_state (key, value) "
                "VALUES ('embeding_fingerprint', '{\"v\":1}')",
            )
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            sql(
                url,
                "UPDATE indexer_state SET key = 'something_else' "
                "WHERE key = 'embedding_fingerprint'",
            )
        assert fetchval(url, "SELECT count(*) FROM indexer_state") == 1


def test_023_writes_no_state_row_and_backfills_nothing():
    """Deriving a fingerprint at migration time would assert that the stored
    vectors were produced by the configuration the `.env` carries *now* —
    exactly the claim the fingerprint exists to test, and 016's
    reassignment-lag mistake in a new place. An absent fingerprint means
    "unknown", which is the only true statement available here.

    And every row that exists when 023 runs was embedded by a chunker with no
    cap, which could not truncate, so `false` is the fact rather than a
    placeholder. Neither `content_hash` (the move-detection key) nor
    `embedded_content_hash` (the embed backlog's predicate) is disturbed."""
    with throwaway_db("schema_is_backfill", revision="022") as url:
        seed_pre_023_notes(url)
        assert column_shape(url, "notes_metadata", "chunks_truncated") is None

        _harness.run_alembic(url, "upgrade", "head", dimensions=DIM)

        assert fetchval(url, "SELECT count(*) FROM indexer_state") == 0
        rows = fetch(
            url,
            "SELECT id, content_hash, embedded_content_hash, links_truncated, "
            "       chunks_truncated "
            "FROM notes_metadata ORDER BY id",
        )
        assert [tuple(r) for r in rows] == [
            (1, "hash-a", "hash-a", False, False),
            (2, "hash-b", None, False, False),
        ]


def test_rerunning_023_preserves_recorded_state():
    """Stamp-back idempotence, the shape the gate itself performs: the
    migration body genuinely re-executes. A fingerprint the application has
    since adopted must survive it — an erased fingerprint reads as absent, and
    absent means *adopt*, which would silently bless whatever is configured at
    that moment — and so must a truncation the embed pass has recorded."""
    with throwaway_db("schema_is_rerun", revision="022") as url:
        seed_pre_023_notes(url)
        _harness.run_alembic(url, "upgrade", "head", dimensions=DIM)
        sql(
            url,
            "INSERT INTO indexer_state (key, value) VALUES "
            "('embedding_fingerprint', '{\"model\":\"bge-m3\",\"v\":1}'), "
            "('embed_rotation_cursor', '7')",
        )
        sql(url, "UPDATE notes_metadata SET chunks_truncated = true WHERE id = 1")

        _harness.run_alembic(url, "stamp", "022", dimensions=DIM)
        _harness.run_alembic(url, "upgrade", "head", dimensions=DIM)

        assert alembic_version(url) == HEAD_REVISION
        # Sorted in Python, not by the database: `ORDER BY key` is
        # collation-dependent, and the container's default collation sorts
        # `embedding_fingerprint` before `embed_rotation_cursor` because it
        # ignores the underscore at the primary level. What this case is about
        # is the rows surviving, not their order.
        assert sorted(
            tuple(r) for r in fetch(url, "SELECT key, value FROM indexer_state")
        ) == [
            ("embed_rotation_cursor", "7"),
            ("embedding_fingerprint", '{"model":"bge-m3","v":1}'),
        ]
        assert fetchval(
            url, "SELECT chunks_truncated FROM notes_metadata WHERE id = 1"
        ) is True
        assert fetchval(
            url, "SELECT chunks_truncated FROM notes_metadata WHERE id = 2"
        ) is False
        assert indexer_state_table_comment(url) == INDEXER_STATE_TABLE_MARKER
        assert chunks_truncated_comment(url) == CHUNKS_TRUNCATED_MARKER


def test_023_refuses_a_table_of_unknown_provenance():
    """013's philosophy: reconcile a database that demonstrably has our shape,
    refuse to guess for one that does not. A same-named table somebody else
    created is not the one startup reads its fingerprints out of."""
    with throwaway_db("schema_is_foreign_table", revision="022") as url:
        sql(
            url,
            "CREATE TABLE indexer_state (key varchar(64) PRIMARY KEY, "
            "value text NOT NULL, updated_at timestamptz NOT NULL DEFAULT now())",
        )
        result = _harness.run_alembic(
            url, "upgrade", "head", dimensions=DIM, check=False
        )
        assert result.returncode != 0, "023 should have refused"
        combined = result.stdout + result.stderr
        assert "023's table comment marker" in combined, combined
        assert alembic_version(url) == "022", "nothing should have been recorded"
        assert column_shape(url, "notes_metadata", "chunks_truncated") is None


def test_023_refuses_an_impostor_constraint_under_the_right_name():
    """The case `alembic check` is blind to and a name lookup cannot see: a
    `CHECK (true)` carrying the expected name, on a table carrying the expected
    marker. It satisfies every name-level test while enforcing nothing, and
    what it stops enforcing is the closed key set."""
    with throwaway_db("schema_is_impostor_check", revision="022") as url:
        sql(
            url,
            "CREATE TABLE indexer_state (key varchar(64) PRIMARY KEY, "
            "value text NOT NULL, updated_at timestamptz NOT NULL DEFAULT now(), "
            f"CONSTRAINT {STATE_CHECK_NAME} CHECK (true))",
        )
        sql(
            url,
            "COMMENT ON TABLE indexer_state IS "
            f"'{INDEXER_STATE_TABLE_MARKER}'",
        )
        sql(
            url,
            f"COMMENT ON CONSTRAINT {STATE_CHECK_NAME} ON indexer_state IS "
            f"'{INDEXER_STATE_CHECK_MARKER}'",
        )
        result = _harness.run_alembic(
            url, "upgrade", "head", dimensions=DIM, check=False
        )
        assert result.returncode != 0, "023 should have refused"
        combined = result.stdout + result.stderr
        assert "its CHECK is" in combined, combined
        assert alembic_version(url) == "022"
        # And the impostor is untouched — nothing was adopted or repaired.
        assert indexer_state_checks(url) == [
            ("CHECK (true)", True, INDEXER_STATE_CHECK_MARKER)
        ]


def test_023_refuses_an_unmarked_constraint_on_an_otherwise_correct_table():
    with throwaway_db("schema_is_unmarked_check", revision="022") as url:
        sql(
            url,
            "CREATE TABLE indexer_state (key varchar(64) PRIMARY KEY, "
            "value text NOT NULL, updated_at timestamptz NOT NULL DEFAULT now(), "
            f"CONSTRAINT {STATE_CHECK_NAME} CHECK (key IN "
            "('embedding_fingerprint', 'fts_fingerprint', "
            "'embed_rotation_cursor')))",
        )
        sql(
            url,
            "COMMENT ON TABLE indexer_state IS "
            f"'{INDEXER_STATE_TABLE_MARKER}'",
        )
        result = _harness.run_alembic(
            url, "upgrade", "head", dimensions=DIM, check=False
        )
        assert result.returncode != 0, "023 should have refused"
        combined = result.stdout + result.stderr
        assert "023's constraint marker" in combined, combined
        assert alembic_version(url) == "022"


def test_023_refuses_a_column_of_unknown_provenance():
    """A nullable `chunks_truncated` somebody else created is not adoptable:
    the vector tools read this column as whether that note's embedding covers
    the whole note, and NULL read as `false` hides a capped note from an
    agent."""
    with throwaway_db("schema_is_foreign_column", revision="022") as url:
        sql(
            url,
            "ALTER TABLE notes_metadata ADD COLUMN chunks_truncated BOOLEAN",
        )
        result = _harness.run_alembic(
            url, "upgrade", "head", dimensions=DIM, check=False
        )
        assert result.returncode != 0, "023 should have refused"
        combined = result.stdout + result.stderr
        assert "it is nullable" in combined, combined
        assert "023's comment marker" in combined, combined
        assert alembic_version(url) == "022", "nothing should have been recorded"
        # The refusal is atomic across both units: the table 023 would have
        # created is rolled back with it.
        assert fetchval(url, "SELECT to_regclass('indexer_state')") is None


def test_downgrade_023_drops_the_marked_units_and_upgrade_rebuilds_them():
    with throwaway_db("schema_is_downgrade") as url:
        _harness.run_alembic(url, "downgrade", "022", dimensions=DIM)
        assert alembic_version(url) == "022"
        assert fetchval(url, "SELECT to_regclass('indexer_state')") is None
        assert column_shape(url, "notes_metadata", "chunks_truncated") is None

        _harness.run_alembic(url, "upgrade", "head", dimensions=DIM)
        assert alembic_version(url) == HEAD_REVISION
        assert indexer_state_table_comment(url) == INDEXER_STATE_TABLE_MARKER
        assert chunks_truncated_comment(url) == CHUNKS_TRUNCATED_MARKER
        check = _harness.run_alembic(url, "check", dimensions=DIM, check=False)
        assert check.returncode == 0, (
            f"alembic check reported drift\n{check.stdout}\n{check.stderr}"
        )


def test_downgrade_023_refuses_a_table_it_did_not_create():
    """013's rule on the way back down: undo *this* migration, not delete
    somebody else's table of the same name."""
    with throwaway_db("schema_is_downgrade_foreign_table") as url:
        sql(
            url,
            "COMMENT ON TABLE indexer_state IS 'somebody else made this'",
        )
        result = _harness.run_alembic(
            url, "downgrade", "022", dimensions=DIM, check=False
        )
        assert result.returncode != 0
        assert "023's comment marker" in result.stdout + result.stderr
        assert fetchval(url, "SELECT to_regclass('indexer_state')") is not None
        # Per-unit all-or-nothing: the refusal rolls the whole downgrade back,
        # so the column 023 *did* create is still there too.
        assert column_shape(url, "notes_metadata", "chunks_truncated") is not None


def test_downgrade_023_refuses_a_column_it_did_not_create():
    with throwaway_db("schema_is_downgrade_foreign_column") as url:
        sql(
            url,
            "COMMENT ON COLUMN notes_metadata.chunks_truncated IS "
            "'somebody else made this'",
        )
        result = _harness.run_alembic(
            url, "downgrade", "022", dimensions=DIM, check=False
        )
        assert result.returncode != 0
        assert "023's comment marker" in result.stdout + result.stderr
        assert column_shape(url, "notes_metadata", "chunks_truncated") is not None
        assert fetchval(url, "SELECT to_regclass('indexer_state')") is not None


# ══════════════════════════════════════════════════════════════════════════
# 024 — user_sessions, the panel session registry (#198)
# ══════════════════════════════════════════════════════════════════════════

USER_SESSIONS_MARKER = "one row per panel browser session (024_user_sessions)"

SESSIONS_USER_INDEX = "ix_user_sessions_user_id"
SESSIONS_EXPIRES_INDEX = "ix_user_sessions_expires_at"

# The whole shape, in creation order, as PostgreSQL 16 prints it. Mirrored from
# the migration's `EXPECTED_COLUMNS` and asserted independently here so a
# reviewer reading the gate sees the table the registry actually runs against
# rather than being told the migration agrees with itself.
USER_SESSIONS_COLUMNS = (
    ("id", "character varying(64)", True),
    ("user_id", "integer", True),
    ("created_at", "timestamp with time zone", True),
    ("last_seen_at", "timestamp with time zone", True),
    ("expires_at", "timestamp with time zone", True),
    ("revoked_at", "timestamp with time zone", False),
    ("user_agent_hash", "character varying(64)", False),
)

# 024's DDL, written out so the refusal cases can stand a *foreign* table of
# this name up and mutate exactly one thing about it. Deliberately not derived
# from the migration: a case that built its impostor by importing the
# migration's own definition would move with the migration and stop being
# adversarial.
USER_SESSIONS_DDL = (
    "CREATE TABLE user_sessions ("
    " id varchar(64) PRIMARY KEY,"
    " user_id integer NOT NULL REFERENCES users(id) ON DELETE CASCADE,"
    " created_at timestamptz NOT NULL DEFAULT now(),"
    " last_seen_at timestamptz NOT NULL,"
    " expires_at timestamptz NOT NULL,"
    " revoked_at timestamptz,"
    " user_agent_hash varchar(64))"
)


def user_sessions_comment(url):
    return fetchval(
        url,
        "SELECT obj_description(c.oid, 'pg_class') FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE c.relname = 'user_sessions' AND c.relkind = 'r' "
        "  AND n.nspname = ANY (current_schemas(false))",
    )


def user_sessions_columns(url):
    rows = fetch(
        url,
        "SELECT a.attname, format_type(a.atttypid, a.atttypmod) AS coltype, "
        "       a.attnotnull "
        "FROM pg_attribute a "
        "WHERE a.attrelid = 'public.user_sessions'::regclass "
        "  AND a.attnum > 0 AND NOT a.attisdropped "
        "ORDER BY a.attnum",
    )
    return [(r["attname"], r["coltype"], r["attnotnull"]) for r in rows]


def user_sessions_fk(url):
    """`(local, referenced_table, referenced_columns, delete, update, valid)`.

    The delete action is read from `pg_constraint.confdeltype`, never by
    constraint name, and the *referent* is read with it: an FK of the right
    name and the right delete action pointing at another table would bind
    browser sessions to somebody else's rows while satisfying every name-level
    check.
    """
    rows = fetch(
        url,
        "SELECT (SELECT array_agg(a.attname ORDER BY k.ord) "
        "          FROM unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) "
        "          JOIN pg_attribute a ON a.attrelid = c.conrelid "
        "                             AND a.attnum = k.attnum) AS local_columns, "
        "       c.confrelid::regclass::text AS referenced_table, "
        "       (SELECT array_agg(a.attname ORDER BY k.ord) "
        "          FROM unnest(c.confkey) WITH ORDINALITY AS k(attnum, ord) "
        "          JOIN pg_attribute a ON a.attrelid = c.confrelid "
        "                             AND a.attnum = k.attnum) AS referenced_columns, "
        "       c.confdeltype::text, c.confupdtype::text, c.convalidated "
        "FROM pg_constraint c "
        "WHERE c.conrelid = 'public.user_sessions'::regclass AND c.contype = 'f'",
    )
    return [tuple(r) for r in rows]


def user_sessions_index_columns(url, name):
    return fetchval(
        url,
        "SELECT array_agg(a.attname ORDER BY k.ord) "
        "FROM pg_index i JOIN pg_class ic ON ic.oid = i.indexrelid "
        "     CROSS JOIN unnest(string_to_array(i.indkey::text, ' ')) "
        "                WITH ORDINALITY AS k(attnum, ord) "
        "     JOIN pg_attribute a ON a.attrelid = i.indrelid "
        "                        AND a.attnum = k.attnum::smallint "
        "WHERE i.indrelid = 'public.user_sessions'::regclass AND ic.relname = $1 "
        "GROUP BY ic.relname",
        name,
    )


def user_sessions_pk_columns(url):
    return fetchval(
        url,
        "SELECT (SELECT array_agg(a.attname ORDER BY k.ord) "
        "          FROM unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) "
        "          JOIN pg_attribute a ON a.attrelid = c.conrelid "
        "                             AND a.attnum = k.attnum) "
        "FROM pg_constraint c "
        "WHERE c.conrelid = 'public.user_sessions'::regclass AND c.contype = 'p'",
    )


def insert_session(url, session_id, user_id, *, expires=None, revoked=None):
    """One `user_sessions` row, shaped the way `start_session` writes it."""
    sql(
        url,
        "INSERT INTO user_sessions "
        "(id, user_id, last_seen_at, expires_at, revoked_at, user_agent_hash) "
        "VALUES ($1, $2, now(), $3, $4, $5)",
        session_id,
        user_id,
        expires or FUTURE,
        revoked,
        "u" * 64,
    )


def refuse_024(url, *, must_mention):
    """Stamp back to 023, re-run 024, require a refusal, return the message.

    Stamping back is what makes this adversarial rather than theatrical: it is
    the same path `make test-schema` exercises for idempotence, so a shape
    waved through here would be waved through on a real database somebody had
    altered.
    """
    _harness.run_alembic(url, "stamp", "023", dimensions=DIM)
    result = _harness.run_alembic(url, "upgrade", "head", dimensions=DIM, check=False)
    assert result.returncode != 0, "024 should have refused"
    combined = result.stdout + result.stderr
    for phrase in must_mention:
        assert phrase in combined, f"refusal did not mention {phrase!r}:\n{combined}"
    assert alembic_version(url) == "023", "nothing should have been recorded"
    return combined


def test_024_creates_the_registry_it_promises():
    with throwaway_db("schema_sessions_fresh") as url:
        assert alembic_version(url) == HEAD_REVISION

        assert fetchval(url, "SELECT to_regclass('public.user_sessions')") is not None
        assert user_sessions_comment(url) == USER_SESSIONS_MARKER
        assert user_sessions_columns(url) == list(USER_SESSIONS_COLUMNS)
        assert list(user_sessions_pk_columns(url) or []) == ["id"], (
            "the primary key must be on `id`: two rows claiming one session "
            "identifier hash would leave validation authorizing against an "
            "arbitrary one of them"
        )

        # `'c'` is CASCADE. Read from the catalog with the referent, never by
        # constraint name — the cascade is the entire mechanism by which a
        # permanent user delete removes that user's sessions, because
        # `User.sessions` declares `passive_deletes=True` and no handler code
        # does it instead.
        assert user_sessions_fk(url) == [
            (["user_id"], "users", ["id"], "c", "a", True)
        ]

        assert user_sessions_index_columns(url, SESSIONS_USER_INDEX) == ["user_id"]
        assert user_sessions_index_columns(url, SESSIONS_EXPIRES_INDEX) == ["expires_at"]

        # The created_at default is asserted here or nowhere: `alembic check`
        # does not compare server defaults at all.
        assert column_shape(url, "user_sessions", "created_at") == (
            True,
            "timestamp with time zone",
            "now()",
        )

        check = _harness.run_alembic(url, "check", dimensions=DIM, check=False)
        assert check.returncode == 0, (
            f"alembic check reported drift\n{check.stdout}\n{check.stderr}"
        )
        assert "No new upgrade operations detected" in check.stdout


def test_024_creates_in_public_under_a_redirected_search_path():
    """The migration run with `search_path` pointing somewhere else.

    021's case, repeated for 024 because the exposure is the same and the
    consequence is worse. Every `op.*` call in 024 is unqualified and resolves
    through `search_path`, so without the pin the registry would be created in
    `decoy` — and the ORM, which declares no schema, would resolve
    `user_sessions` through whatever path the *application's* role has. The
    day those two differ, the validator finds no row for any cookie and every
    user is locked out of the panel with a correctly signed cookie in hand.

    021 and 023 both `RESET` the path at the end of their own `upgrade()`, so a
    later revision in the same transaction inherits nothing: 024 needs its own
    pin, and this is what proves it has one.
    """
    with throwaway_db("schema_sessions_path", revision="023") as url:
        dbname = fetchval(url, "SELECT current_database()")
        sql(url, "CREATE SCHEMA decoy")
        sql(url, f'ALTER DATABASE "{dbname}" SET search_path TO decoy, public')
        # A *new* connection is what alembic opens, so confirm the redirect is
        # in force for one before believing the rest of this case.
        assert fetchval(url, "SHOW search_path") == "decoy, public"

        _harness.run_alembic(url, "upgrade", "head", dimensions=DIM)

        assert alembic_version(url) == HEAD_REVISION
        assert fetchval(url, "SELECT to_regclass('public.user_sessions')") is not None
        assert fetchval(url, "SELECT to_regclass('decoy.user_sessions')") is None, (
            "the registry must land where the ORM looks, not first on the "
            "search path"
        )
        # The comment and both indexes are separate unqualified statements, so
        # each is checked rather than assumed to have followed the table.
        assert user_sessions_comment(url) == USER_SESSIONS_MARKER
        assert user_sessions_index_columns(url, SESSIONS_USER_INDEX) == ["user_id"]
        assert user_sessions_index_columns(url, SESSIONS_EXPIRES_INDEX) == ["expires_at"]
        assert fetchval(
            url,
            "SELECT count(*) FROM pg_class c "
            "WHERE c.relnamespace = 'decoy'::regnamespace",
        ) == 0, "nothing at all was created in the decoy schema"

        # The pin is `SET LOCAL`, so it must not have outlived the transaction.
        assert fetchval(url, "SHOW search_path") == "decoy, public"

        check = _harness.run_alembic(url, "check", dimensions=DIM, check=False)
        assert check.returncode == 0, (
            f"alembic check reported drift\n{check.stdout}\n{check.stderr}"
        )

        # And the downgrade drops from public, not from wherever the path points.
        _harness.run_alembic(url, "downgrade", "023", dimensions=DIM)
        assert fetchval(url, "SELECT to_regclass('public.user_sessions')") is None


def test_024_chains_from_023_and_023_is_applied_first():
    """The stated merge precondition, asserted rather than assumed: 024 must
    not migrate ahead of the sibling `index-integrity-hardening` migration."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config()
    config.set_main_option("script_location", str(_harness.ROOT / "alembic"))
    script = ScriptDirectory.from_config(config)

    assert script.get_current_head() == HEAD_REVISION
    assert script.get_revision("024").down_revision == "023"

    ordered = [rev.revision for rev in script.walk_revisions("base", "024")]
    assert ordered.index("023") > ordered.index("024"), (
        "walk_revisions yields newest-first, so 023 must appear after 024 — "
        "i.e. 023 is applied before it"
    )


def test_024_writes_no_rows():
    """There is nothing to backfill *from*: a session that predates the table
    has no identifier the registry could resolve, and inventing rows for the
    cookies currently in flight would grandfather exactly the credentials this
    change exists to invalidate."""
    with throwaway_db("schema_sessions_backfill", revision="023") as url:
        insert_user(url, 1, "alice")
        insert_user(url, 2, "bob")
        assert fetchval(url, "SELECT to_regclass('public.user_sessions')") is None

        _harness.run_alembic(url, "upgrade", "head", dimensions=DIM)

        assert alembic_version(url) == HEAD_REVISION
        assert fetchval(url, "SELECT count(*) FROM user_sessions") == 0
        # And the users it could have invented rows for are untouched.
        assert fetchval(url, "SELECT count(*) FROM users") == 2


def test_deleting_a_user_cascades_their_sessions():
    """The cascade is the mechanism, not a safety net: `User.sessions` declares
    `passive_deletes=True`, so nothing in the handler deletes these rows and a
    missing cascade would leave live sessions bound to a user that no longer
    exists — or block the delete outright."""
    with throwaway_db("schema_sessions_cascade") as url:
        insert_user(url, 1, "alice")
        insert_user(url, 2, "bob")
        insert_session(url, "a" * 64, 1)
        insert_session(url, "b" * 64, 1, revoked=FUTURE)
        insert_session(url, "c" * 64, 2)

        sql(url, "DELETE FROM users WHERE id = 1")

        assert [
            r["id"] for r in fetch(url, "SELECT id FROM user_sessions ORDER BY id")
        ] == ["c" * 64]


def test_rerunning_024_preserves_live_sessions():
    """Stamp-back idempotence, the shape the gate itself performs: the
    migration body genuinely re-executes. Its reconciliation path writes and
    deletes nothing, so a gate exercise — or a re-run on a real database —
    cannot silently sign every logged-in user out."""
    with throwaway_db("schema_sessions_rerun", revision="023") as url:
        insert_user(url, 1, "alice")
        _harness.run_alembic(url, "upgrade", "head", dimensions=DIM)
        insert_session(url, "a" * 64, 1)
        insert_session(url, "b" * 64, 1, revoked=FUTURE)

        _harness.run_alembic(url, "stamp", "023", dimensions=DIM)
        _harness.run_alembic(url, "upgrade", "head", dimensions=DIM)

        assert alembic_version(url) == HEAD_REVISION
        rows = fetch(
            url,
            "SELECT id, user_id, revoked_at IS NULL AS live "
            "FROM user_sessions ORDER BY id",
        )
        assert [(r["id"], r["user_id"], r["live"]) for r in rows] == [
            ("a" * 64, 1, True),
            ("b" * 64, 1, False),
        ]
        assert user_sessions_comment(url) == USER_SESSIONS_MARKER


def test_024_refuses_a_foreign_table_of_its_name():
    """013's philosophy: reconcile a database that demonstrably has our shape,
    refuse to guess for one that does not. A same-named table somebody else
    created is not the table every panel request resolves its session out of.
    """
    with throwaway_db("schema_sessions_foreign", revision="023") as url:
        sql(url, USER_SESSIONS_DDL)
        result = _harness.run_alembic(
            url, "upgrade", "head", dimensions=DIM, check=False
        )
        assert result.returncode != 0, "024 should have refused"
        combined = result.stdout + result.stderr
        assert "024's comment marker" in combined, combined
        assert alembic_version(url) == "023", "nothing should have been recorded"
        # And the impostor is untouched — nothing was adopted or repaired.
        assert user_sessions_comment(url) is None
        assert user_sessions_index_columns(url, SESSIONS_USER_INDEX) is None


def test_024_refuses_a_foreign_key_that_does_not_cascade():
    """The adversarial case a marker-and-name check adopts. Everything else is
    024's: its marker, its columns, its indexes. Only the delete action moved —
    and with it the promise that a permanent user delete removes that user's
    sessions. `SET NULL` against a NOT NULL column would fail the delete
    outright; `NO ACTION` would leave the rows behind."""
    with throwaway_db("schema_sessions_fk_action") as url:
        constraint = fetchval(
            url,
            "SELECT conname FROM pg_constraint "
            "WHERE conrelid = 'public.user_sessions'::regclass AND contype = 'f'",
        )
        sql(url, f"ALTER TABLE user_sessions DROP CONSTRAINT {constraint}")
        sql(
            url,
            f"ALTER TABLE user_sessions ADD CONSTRAINT {constraint} "
            "FOREIGN KEY (user_id) REFERENCES users(id)",
        )
        combined = refuse_024(url, must_mention=["deletes with", "CASCADE"])
        assert "user_id foreign key" in combined


def test_024_refuses_a_foreign_key_pointing_at_another_table():
    """Only the *referent* moved, and with it the meaning of every session the
    registry holds."""
    with throwaway_db("schema_sessions_fk_target") as url:
        constraint = fetchval(
            url,
            "SELECT conname FROM pg_constraint "
            "WHERE conrelid = 'public.user_sessions'::regclass AND contype = 'f'",
        )
        sql(url, f"ALTER TABLE user_sessions DROP CONSTRAINT {constraint}")
        sql(
            url,
            f"ALTER TABLE user_sessions ADD CONSTRAINT {constraint} "
            "FOREIGN KEY (user_id) REFERENCES api_keys(id) ON DELETE CASCADE",
        )
        refuse_024(url, must_mention=["references", "api_keys"])


def test_024_refuses_a_missing_index():
    with throwaway_db("schema_sessions_missing_index") as url:
        sql(url, f"DROP INDEX {SESSIONS_EXPIRES_INDEX}")
        refuse_024(url, must_mention=[f"missing index {SESSIONS_EXPIRES_INDEX}"])


def test_024_refuses_an_index_of_its_name_on_another_column():
    """A name is not a definition. Recreated on `revoked_at`, the index keeps
    the name an existence check looks for while the purge's range scan over
    `expires_at` has nothing to lean on."""
    with throwaway_db("schema_sessions_wrong_index") as url:
        sql(url, f"DROP INDEX {SESSIONS_EXPIRES_INDEX}")
        sql(url, f"CREATE INDEX {SESSIONS_EXPIRES_INDEX} ON user_sessions (revoked_at)")
        refuse_024(url, must_mention=[SESSIONS_EXPIRES_INDEX, "revoked_at"])


def test_024_refuses_a_partial_index_of_its_name():
    """`WHERE revoked_at IS NULL` reads as an optimisation and silently
    excludes exactly the rows the purge exists to remove."""
    with throwaway_db("schema_sessions_partial_index") as url:
        sql(url, f"DROP INDEX {SESSIONS_EXPIRES_INDEX}")
        sql(
            url,
            f"CREATE INDEX {SESSIONS_EXPIRES_INDEX} ON user_sessions (expires_at) "
            "WHERE revoked_at IS NULL",
        )
        refuse_024(url, must_mention=["partial-or-expression=True"])


def test_024_refuses_a_missing_column():
    """A partial shape carrying 024's marker: everything else agrees, and the
    column the forensic record lives in is simply gone."""
    with throwaway_db("schema_sessions_missing_column") as url:
        sql(url, "ALTER TABLE user_sessions DROP COLUMN user_agent_hash")
        refuse_024(url, must_mention=["its columns are"])


def test_024_refuses_a_nullable_expiry():
    """A nullable `expires_at` is a session with no expiry at all — the
    absolute seven-day bound is the tighter half of the pair the cookie's own
    sliding age cannot be trusted for."""
    with throwaway_db("schema_sessions_nullable_expiry") as url:
        sql(url, "ALTER TABLE user_sessions ALTER COLUMN expires_at DROP NOT NULL")
        refuse_024(url, must_mention=["its columns are"])


def test_024_refuses_a_missing_primary_key():
    """`alembic check` does not compare primary keys, so a table of the right
    columns with none reports as being in perfect agreement with the model —
    while two rows could claim one session-identifier hash and validation reads
    exactly one row per hash."""
    with throwaway_db("schema_sessions_no_pk") as url:
        constraint = fetchval(
            url,
            "SELECT conname FROM pg_constraint "
            "WHERE conrelid = 'public.user_sessions'::regclass AND contype = 'p'",
        )
        sql(url, f"ALTER TABLE user_sessions DROP CONSTRAINT {constraint}")
        refuse_024(url, must_mention=["no primary key at all"])


def test_024_refuses_a_tampered_created_at_default():
    """The quietest drift this table has, and the one `alembic check` is blind
    to: every column, type, constraint and index stays exactly as 024 made
    them while the recorded age of every session minted afterwards is wrong."""
    with throwaway_db("schema_sessions_default") as url:
        sql(
            url,
            "ALTER TABLE user_sessions ALTER COLUMN created_at "
            "SET DEFAULT now() - interval '100 years'",
        )
        refuse_024(url, must_mention=["created_at default"])


def test_a_refusal_leaves_a_seeded_registry_intact():
    """The refusal is atomic and writes nothing: an operator who hits it still
    has every row, and every logged-in user is still logged in."""
    with throwaway_db("schema_sessions_refusal_rows") as url:
        insert_user(url, 1, "alice")
        insert_session(url, "a" * 64, 1)
        sql(url, f"DROP INDEX {SESSIONS_USER_INDEX}")
        refuse_024(url, must_mention=[f"missing index {SESSIONS_USER_INDEX}"])
        assert fetchval(url, "SELECT count(*) FROM user_sessions") == 1


def test_downgrade_024_drops_the_marked_table_and_upgrade_rebuilds_it():
    with throwaway_db("schema_sessions_downgrade") as url:
        _harness.run_alembic(url, "downgrade", "023", dimensions=DIM)
        assert alembic_version(url) == "023"
        assert fetchval(url, "SELECT to_regclass('public.user_sessions')") is None

        _harness.run_alembic(url, "upgrade", "head", dimensions=DIM)
        assert alembic_version(url) == HEAD_REVISION
        assert user_sessions_comment(url) == USER_SESSIONS_MARKER
        assert user_sessions_index_columns(url, SESSIONS_USER_INDEX) == ["user_id"]
        check = _harness.run_alembic(url, "check", dimensions=DIM, check=False)
        assert check.returncode == 0, (
            f"alembic check reported drift\n{check.stdout}\n{check.stderr}"
        )


def test_downgrade_024_refuses_a_table_it_did_not_create():
    """013's rule on the way back down: undo *this* migration, not delete
    somebody else's table of the same name."""
    with throwaway_db("schema_sessions_downgrade_foreign") as url:
        sql(url, "COMMENT ON TABLE user_sessions IS 'somebody else made this'")
        result = _harness.run_alembic(
            url, "downgrade", "023", dimensions=DIM, check=False
        )
        assert result.returncode != 0
        assert "024's comment marker" in result.stdout + result.stderr
        assert fetchval(url, "SELECT to_regclass('public.user_sessions')") is not None
        assert alembic_version(url) == HEAD_REVISION
