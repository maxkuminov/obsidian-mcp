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
"""
import asyncio
import contextlib
import datetime

import asyncpg
import pytest

import _harness

pytestmark = [_harness.requires_pgvector]

DIM = 64  # irrelevant here; keeps the migration cheap.

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
    """Everything migration 013 promises, read straight out of the catalog."""
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
    if marker_expected:
        assert comment == MARKER, comment
    else:
        assert comment is None, comment

    assert not_null_flags(url) == {
        f"{table}.{column}": True for table, column, _ in NOT_NULL_COLUMNS
    }

    assert_constraint_enforced(url)


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
        assert alembic_version(url) == "013"
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

        assert alembic_version(url) == "013"
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
