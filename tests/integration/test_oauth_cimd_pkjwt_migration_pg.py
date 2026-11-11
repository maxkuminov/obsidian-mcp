"""Migration 038 (#420): `private_key_jwt` columns, three CHECKs, refusing downgrade.

Skipped unless `PGVECTOR_TEST_ADMIN_URL` names a throwaway Postgres server
(`make test-integration` sets it).
"""
import asyncio
import contextlib

import asyncpg
import pytest

import _harness

pytestmark = _harness.requires_pgvector

DIM = 64
# The current head (040 since #434; 039 since #428). A downgrade 038 refuses
# rolls back the whole run, the later steps included, so the version stays at
# head. Bump this with every new migration: it is a literal on purpose (the
# test must notice a head it was not written for), and the full integration
# suite is what catches a stale value.
HEAD_REVISION = "040"
URL = "https://chatgpt.com/oauth/client.json"
JWKS_URI = "https://chatgpt.com/oauth/jwks.json"
SNAPSHOT = '{"keys": [{"kty": "RSA", "kid": "k1", "n": "AQAB", "e": "AQAB"}]}'
NONE_URL = "https://claude.ai/oauth/claude-code-client-metadata"

PKJWT_COLUMNS = (
    "jwks_uri",
    "token_endpoint_auth_signing_alg",
    "jwks_snapshot",
    "jwks_fetched_at",
)
CHECKS = (
    "ck_oauth_clients_auth_method_secret",
    "ck_oauth_clients_cimd_shape",
    "ck_oauth_clients_private_key_jwt_shape",
)
PKJWT_SHAPE = "ck_oauth_clients_private_key_jwt_shape"


def _cimd(c: str) -> str:
    return "cimd_" + c * 59


@contextlib.contextmanager
def throwaway_db(prefix: str, revision: str = "head"):
    generator = _harness.throwaway_database(prefix, DIM, revision=revision)
    url = next(generator)
    try:
        yield url
    finally:
        generator.close()


async def _run(url, fn):
    conn = await asyncpg.connect(_harness.asyncpg_dsn(url))
    try:
        return await fn(conn)
    finally:
        await conn.close()


def sql(url, statement, *args):
    return asyncio.run(_run(url, lambda c: c.execute(statement, *args)))


def fetch(url, statement, *args):
    return asyncio.run(_run(url, lambda c: c.fetch(statement, *args)))


def fetchval(url, statement, *args):
    return asyncio.run(_run(url, lambda c: c.fetchval(statement, *args)))


def version(url):
    return fetchval(url, "SELECT version_num FROM alembic_version")


def insert_legacy(url, client_id, method, secret=None, metadata_url=None):
    """A row as 037 writes it: no 038 columns named."""
    sql(
        url,
        "INSERT INTO oauth_clients (client_id, client_secret_hash, "
        "token_endpoint_auth_method, client_name, redirect_uris, scope, "
        "client_id_metadata_url, last_used_at) "
        "VALUES ($1, $2, $3, 'x', '[]'::jsonb, 'read', $4, now())",
        client_id,
        secret,
        method,
        metadata_url,
    )


def insert_pkjwt(
    url,
    client_id,
    *,
    method="private_key_jwt",
    metadata_url=URL,
    secret=None,
    jwks_uri=JWKS_URI,
    alg="RS256",
    snapshot=SNAPSHOT,
    fetched=True,
):
    sql(
        url,
        "INSERT INTO oauth_clients (client_id, client_secret_hash, "
        "token_endpoint_auth_method, client_name, redirect_uris, scope, "
        "client_id_metadata_url, last_used_at, jwks_uri, "
        "token_endpoint_auth_signing_alg, jwks_snapshot, jwks_fetched_at) "
        "VALUES ($1, $2, $3, 'x', '[]'::jsonb, 'read', $4, now(), $5, $6, "
        "$7::jsonb, CASE WHEN $8 THEN now() ELSE NULL END)",
        client_id,
        secret,
        method,
        metadata_url,
        jwks_uri,
        alg,
        snapshot,
        fetched,
    )


def rows(url):
    return [
        tuple(r)
        for r in fetch(
            url,
            "SELECT client_id, client_secret_hash, token_endpoint_auth_method, "
            "client_id_metadata_url FROM oauth_clients ORDER BY client_id",
        )
    ]


def checks(url):
    """{name: (convalidated, definition, comment)} for oauth_clients' CHECKs."""
    return {
        r["conname"]: (r["convalidated"], r["def"], r["comment"])
        for r in fetch(
            url,
            "SELECT conname, convalidated, pg_get_constraintdef(oid) AS def, "
            "obj_description(oid, 'pg_constraint') AS comment FROM pg_constraint "
            "WHERE conrelid = 'oauth_clients'::regclass AND contype = 'c'",
        )
    }


def columns(url):
    return {
        r["column_name"]
        for r in fetch(
            url,
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'oauth_clients'",
        )
    }


def seed_legacy(url):
    insert_legacy(url, "a" * 32, "client_secret_post", secret="f" * 64)
    insert_legacy(url, "b" * 32, "none")
    insert_legacy(url, _cimd("c"), "none", metadata_url=NONE_URL)


def refuse(url, *, must_mention):
    before = (checks(url), columns(url))
    result = _harness.run_alembic(url, "upgrade", "head", dimensions=DIM, check=False)
    assert result.returncode != 0, "038 should have refused"
    combined = result.stdout + result.stderr
    for phrase in [*must_mention, "Nothing has been changed"]:
        assert phrase in combined, f"refusal did not mention {phrase!r}:\n{combined}"
    assert version(url) == "037", "nothing should have been recorded"
    assert (checks(url), columns(url)) == before, "a refusal changed the schema"


@pytest.fixture(scope="module")
def head_url():
    yield from _harness.throwaway_database("cimd_pkjwt_038", DIM)


# --------------------------------------------------------------------------
# upgrade over existing rows, and the re-run
# --------------------------------------------------------------------------


def test_upgrade_leaves_dcr_and_none_cimd_rows_unchanged_and_valid():
    with throwaway_db("pkjwt_038_upgrade", revision="037") as url:
        seed_legacy(url)
        before = rows(url)
        assert not set(PKJWT_COLUMNS) & columns(url)

        _harness.run_alembic(url, "upgrade", "head", dimensions=DIM)

        assert version(url) == HEAD_REVISION
        assert rows(url) == before
        assert set(PKJWT_COLUMNS) <= columns(url)
        assert fetchval(
            url,
            "SELECT count(*) FROM oauth_clients WHERE jwks_uri IS NOT NULL "
            "OR token_endpoint_auth_signing_alg IS NOT NULL "
            "OR jwks_snapshot IS NOT NULL OR jwks_fetched_at IS NOT NULL",
        ) == 0
        found = checks(url)
        for name in CHECKS:
            assert found[name][0] is True, f"{name} is missing or NOT VALID"


def test_rerun_reconciles_and_changes_nothing():
    with throwaway_db("pkjwt_038_rerun") as url:
        seed_legacy(url)
        insert_pkjwt(url, _cimd("d"))
        before = (checks(url), columns(url), rows(url))

        _harness.run_alembic(url, "stamp", "037", dimensions=DIM)
        _harness.run_alembic(url, "upgrade", "head", dimensions=DIM)

        assert version(url) == HEAD_REVISION
        assert (checks(url), columns(url), rows(url)) == before


def test_rerun_replaces_a_predecessor_definition_left_by_a_stamp_back():
    """036's CHECK under its own name at 037 is the predecessor: replaced."""
    with throwaway_db("pkjwt_038_predecessor") as url:
        expected = checks(url)
        sql(url, "ALTER TABLE oauth_clients DROP CONSTRAINT ck_oauth_clients_cimd_shape")
        sql(
            url,
            "ALTER TABLE oauth_clients ADD CONSTRAINT ck_oauth_clients_cimd_shape CHECK ("
            "((client_id LIKE 'cimd\\_%') = (client_id_metadata_url IS NOT NULL)) AND "
            "(client_id_metadata_url IS NULL OR "
            "(token_endpoint_auth_method = 'none' AND client_secret_hash IS NULL)))",
        )
        _harness.run_alembic(url, "stamp", "037", dimensions=DIM)
        _harness.run_alembic(url, "upgrade", "head", dimensions=DIM)
        assert checks(url) == expected


# --------------------------------------------------------------------------
# foreign objects of our names
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ddl, fragment",
    [
        ("ALTER TABLE oauth_clients ADD COLUMN jwks_uri text", "jwks_uri is text"),
        (
            "ALTER TABLE oauth_clients ADD COLUMN jwks_snapshot jsonb",
            "jwks_snapshot does not carry 038's comment marker",
        ),
        (
            "ALTER TABLE oauth_clients ADD COLUMN jwks_fetched_at "
            "timestamptz NOT NULL DEFAULT now()",
            "jwks_fetched_at is NOT NULL",
        ),
    ],
)
def test_a_foreign_column_of_our_name_is_refused(ddl, fragment):
    with throwaway_db("pkjwt_038_foreign_col", revision="037") as url:
        seed_legacy(url)
        sql(url, ddl)
        refuse(url, must_mention=[fragment])


def test_a_foreign_new_constraint_of_our_name_is_refused():
    with throwaway_db("pkjwt_038_foreign_ck", revision="037") as url:
        sql(url, f"ALTER TABLE oauth_clients ADD CONSTRAINT {PKJWT_SHAPE} CHECK (true)")
        refuse(url, must_mention=[f"{PKJWT_SHAPE} already exists but is not 038's"])


def test_an_unmarked_copy_of_the_new_constraint_is_refused():
    """The right definition without the marker has unknown provenance."""
    with throwaway_db("pkjwt_038_unmarked") as url:
        sql(url, f"COMMENT ON CONSTRAINT {PKJWT_SHAPE} ON oauth_clients IS NULL")
        _harness.run_alembic(url, "stamp", "037", dimensions=DIM)
        refuse(url, must_mention=["marker absent"])


def test_a_foreign_replaced_constraint_of_our_name_is_refused():
    with throwaway_db("pkjwt_038_foreign_cimd", revision="037") as url:
        sql(url, "ALTER TABLE oauth_clients DROP CONSTRAINT ck_oauth_clients_cimd_shape")
        sql(url, "ALTER TABLE oauth_clients ADD CONSTRAINT ck_oauth_clients_cimd_shape CHECK (true)")
        refuse(url, must_mention=["ck_oauth_clients_cimd_shape exists with a definition"])


# --------------------------------------------------------------------------
# the CHECK matrix at head
# --------------------------------------------------------------------------


def test_a_well_formed_private_key_jwt_row_is_admitted(head_url):
    insert_pkjwt(head_url, _cimd("e"))
    insert_pkjwt(head_url, _cimd("f"), alg=None)  # the document named no alg
    sql(head_url, "DELETE FROM oauth_clients")


@pytest.mark.parametrize(
    "client_id, kwargs",
    [
        ("1" * 32, {"metadata_url": None}),  # private_key_jwt on a DCR row
        (_cimd("2"), {"snapshot": None}),  # without a snapshot
        (_cimd("3"), {"jwks_uri": None}),  # without a jwks_uri
        (_cimd("4"), {"fetched": False}),  # without jwks_fetched_at
        (_cimd("5"), {"secret": "f" * 64}),  # with a secret
        (_cimd("6"), {"method": "none"}),  # a none row carrying a jwks_uri
        ("7" * 32, {"method": "none", "metadata_url": None}),  # DCR none with keys
        (_cimd("8"), {"alg": "HS256"}),
        (_cimd("9"), {"alg": "none"}),
        (_cimd("a"), {"alg": "EdDSA"}),
    ],
)
def test_malformed_rows_are_refused(head_url, client_id, kwargs):
    with pytest.raises(asyncpg.CheckViolationError):
        insert_pkjwt(head_url, client_id, **kwargs)


def test_a_none_row_with_only_an_alg_is_refused(head_url):
    with pytest.raises(asyncpg.CheckViolationError):
        insert_pkjwt(
            head_url, _cimd("b"), method="none", jwks_uri=None, snapshot=None,
            fetched=False, alg="RS256",
        )


# --------------------------------------------------------------------------
# downgrade
# --------------------------------------------------------------------------


def test_downgrade_refuses_while_a_private_key_jwt_row_exists():
    with throwaway_db("pkjwt_038_downgrade_baseline", revision="037") as baseline:
        expected_037 = checks(baseline)
    with throwaway_db("pkjwt_038_downgrade") as url:
        seed_legacy(url)
        insert_pkjwt(url, _cimd("d"))
        before = (checks(url), columns(url))

        result = _harness.run_alembic(url, "downgrade", "037", dimensions=DIM, check=False)
        assert result.returncode != 0
        assert "038 downgrade refused: 1 private_key_jwt client row(s)" in (
            result.stdout + result.stderr
        )
        assert version(url) == HEAD_REVISION
        assert (checks(url), columns(url)) == before

        sql(url, "DELETE FROM oauth_clients WHERE token_endpoint_auth_method = 'private_key_jwt'")
        legacy = rows(url)
        _harness.run_alembic(url, "downgrade", "037", dimensions=DIM)

        assert version(url) == "037"
        assert not set(PKJWT_COLUMNS) & columns(url)
        # 036's exact two definitions are back, and nothing else.
        assert checks(url) == expected_037
        assert rows(url) == legacy

        _harness.run_alembic(url, "upgrade", "head", dimensions=DIM)
        assert version(url) == HEAD_REVISION
        assert checks(url) == before[0]
