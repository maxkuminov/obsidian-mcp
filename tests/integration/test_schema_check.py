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
