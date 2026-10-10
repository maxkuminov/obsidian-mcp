"""docker/upgrade-split-db-roles.sql converts a pre-#324 cluster (design D7, D9).

A throwaway `pgvector/pgvector:pg16` initialised the way the bundles did before
#324 (`POSTGRES_USER=obsidian_mcp`, so the app's role IS the bootstrap
superuser), migrated to head as that role, then seeded as it with every object
category the script promises to move. The script is then run with the
documented invocation and the result is asserted through the catalogs, not
through `alembic check` alone (Codex spec review, MAJOR).

The tests in this module run in order against one cluster.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tests.integration._docker_pg import Container, requires_docker, unique
from tests.integration._harness import ROOT, run_alembic

pytestmark = requires_docker

SCRIPT = (ROOT / "docker" / "upgrade-split-db-roles.sql").read_text()
OLD_PW = "old0superuser0password0" + "1" * 8
ADMIN_PW = "new0admin0password0" + "2" * 12
APP_PW = "new0runtime0password0" + "3" * 12

SEED = """
CREATE TABLE canary (v text);
INSERT INTO canary VALUES ('survives the split');
CREATE SEQUENCE free_seq;
CREATE TABLE serial_t (id serial PRIMARY KEY, idn int GENERATED ALWAYS AS IDENTITY);
CREATE FUNCTION f_one(int) RETURNS int LANGUAGE sql AS 'SELECT $1';
CREATE PROCEDURE p_one() LANGUAGE sql AS 'SELECT 1';
CREATE AGGREGATE my_sum(int) (sfunc = int4pl, stype = int);
CREATE TYPE mood AS ENUM ('calm', 'busy');
CREATE DOMAIN posint AS int CHECK (VALUE > 0);
CREATE TYPE pair AS (x int, y text);
CREATE TYPE floatrange AS RANGE (subtype = float8);
CREATE VIEW v_one AS SELECT 1 AS x;
CREATE MATERIALIZED VIEW mv_one AS SELECT 1 AS x;
CREATE SCHEMA extra;
CREATE TABLE extra.t (id int);
CREATE STATISTICS st_one ON id, idn FROM serial_t;
CREATE ROLE reader NOLOGIN;
ALTER DEFAULT PRIVILEGES GRANT SELECT ON TABLES TO reader;
ALTER DEFAULT PRIVILEGES REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA extra GRANT USAGE ON SEQUENCES TO reader;
SELECT lo_create(0) IS NOT NULL;
"""

# Every (catalog, object, owner) in the database: the before/after snapshot
# for the no-op re-run, and the source of the ownership assertions.
OWNERSHIP = """
SELECT 'db|' || datname || '|' || pg_get_userbyid(datdba) FROM pg_database WHERE datname = current_database()
UNION ALL SELECT 'nsp|' || nspname || '|' || pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname !~ '^pg_' AND nspname <> 'information_schema'
UNION ALL SELECT 'rel|' || c.oid::regclass::text || '|' || c.relkind::text || '|' || pg_get_userbyid(c.relowner)
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
UNION ALL SELECT 'proc|' || p.oid::regprocedure::text || '|' || pg_get_userbyid(p.proowner)
  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
UNION ALL SELECT 'type|' || t.oid::regtype::text || '|' || pg_get_userbyid(t.typowner)
  FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
UNION ALL SELECT 'ext|' || extname || '|' || pg_get_userbyid(extowner) FROM pg_extension
UNION ALL SELECT 'stx|' || stxname || '|' || pg_get_userbyid(stxowner) FROM pg_statistic_ext
UNION ALL SELECT 'lo|' || pg_get_userbyid(lomowner) FROM pg_largeobject_metadata
UNION ALL SELECT 'defacl|' || pg_get_userbyid(defaclrole) || '|' || defaclnamespace::regnamespace::text || '|' || defaclobjtype::text || '|' || defaclacl::text FROM pg_default_acl
ORDER BY 1;
"""

DOCUMENTED = (
    'psql -X -v ON_ERROR_STOP=1 -U obsidian_mcp -d obsidian_mcp '
    '-v admin_pw="$POSTGRES_PASSWORD" -v app_pw="$OBSIDIAN_DB_PASSWORD"'
)


@pytest.fixture(scope="module")
def cluster():
    c = Container(
        unique("presplit"),
        ["-e", "POSTGRES_USER=obsidian_mcp", "-e", f"POSTGRES_PASSWORD={OLD_PW}",
         "-e", "POSTGRES_DB=obsidian_mcp"],
    )
    try:
        c.wait_ready("obsidian_mcp")
        run_alembic(c.url("obsidian_mcp", OLD_PW), "upgrade", "head")
        c.psql(SEED, user="obsidian_mcp")
        yield c
    finally:
        c.remove()


def run_script(c: Container, *, env: dict[str, str] | None = None, command: str = DOCUMENTED):
    """The documented invocation: `exec -T postgres sh -c '<psql …>' < script`.

    The passwords are the container's environment, as they are in the new
    postgres service (postgres.env and .env).
    """
    env_args = [a for k, v in (env or {}).items() for a in ("-e", f"{k}={v}")]
    from tests.integration._docker_pg import docker

    return docker("exec", "-i", *env_args, c.name, "sh", "-c", command, input=SCRIPT, check=False)


def q(c: Container, sql: str, user: str = "postgres") -> list[str]:
    return [line for line in c.psql(sql, user=user).stdout.splitlines() if line]


def owners(c: Container, user: str) -> list[str]:
    return q(c, OWNERSHIP, user=user)


def roles(c: Container, user: str) -> list[str]:
    return q(c, "SELECT oid || '|' || rolname || '|' || CASE WHEN rolsuper THEN 't' ELSE 'f' END FROM pg_roles "
                "WHERE rolname IN ('obsidian_mcp','postgres','obsidian_mcp_split_tmp') ORDER BY rolname", user=user)


def test_missing_input_aborts_before_any_change(cluster):
    before_roles = roles(cluster, "obsidian_mcp")
    before_owners = owners(cluster, "obsidian_mcp")
    result = run_script(cluster, env={"POSTGRES_PASSWORD": ADMIN_PW},
                        command='psql -X -v ON_ERROR_STOP=1 -U obsidian_mcp -d obsidian_mcp '
                                '-v admin_pw="$POSTGRES_PASSWORD"')
    assert result.returncode != 0
    assert "app_pw is not set" in result.stderr
    assert ADMIN_PW not in result.stdout + result.stderr
    assert roles(cluster, "obsidian_mcp") == before_roles
    assert owners(cluster, "obsidian_mcp") == before_owners
    # An empty value (the variable passed but unset in the environment) too.
    result = run_script(cluster, env={"POSTGRES_PASSWORD": ADMIN_PW})
    assert result.returncode != 0
    assert "app_pw is empty" in result.stderr
    assert roles(cluster, "obsidian_mcp") == before_roles
    assert any(line.startswith("10|obsidian_mcp|t") for line in before_roles)


def test_missing_admin_password_aborts_before_any_change(cluster):
    before_roles = roles(cluster, "obsidian_mcp")
    before_owners = owners(cluster, "obsidian_mcp")
    result = run_script(cluster, env={"OBSIDIAN_DB_PASSWORD": APP_PW},
                        command='psql -X -v ON_ERROR_STOP=1 -U obsidian_mcp -d obsidian_mcp '
                                '-v app_pw="$OBSIDIAN_DB_PASSWORD"')
    assert result.returncode != 0
    assert "admin_pw is not set" in result.stderr
    assert APP_PW not in result.stdout + result.stderr
    assert roles(cluster, "obsidian_mcp") == before_roles
    assert owners(cluster, "obsidian_mcp") == before_owners
    # Passed but empty in the environment (no postgres.env value). The
    # container's own POSTGRES_PASSWORD (the old one) is blanked for the exec.
    result = run_script(cluster, env={"POSTGRES_PASSWORD": "", "OBSIDIAN_DB_PASSWORD": APP_PW})
    assert result.returncode != 0
    assert "admin_pw is empty" in result.stderr
    assert roles(cluster, "obsidian_mcp") == before_roles
    assert owners(cluster, "obsidian_mcp") == before_owners


def test_pre_split_cluster_is_converted(cluster):
    import asyncpg

    # A session that authenticated as the old superuser before the conversion,
    # over TCP like the app: it must not survive the commit (Codex, MAJOR).
    loop = asyncio.new_event_loop()
    dsn = f"postgresql://%s:%s@127.0.0.1:{cluster.port}/obsidian_mcp"
    held = loop.run_until_complete(asyncpg.connect(dsn % ("obsidian_mcp", OLD_PW)))
    try:
        assert loop.run_until_complete(held.fetchval("SELECT usesuper FROM pg_user WHERE usesysid = 10"))
        assert loop.run_until_complete(held.fetchval(
            "SELECT rolsuper FROM pg_roles WHERE rolname = current_user"))

        result = run_script(cluster, env={"POSTGRES_PASSWORD": ADMIN_PW, "OBSIDIAN_DB_PASSWORD": APP_PW})
        output = result.stdout + result.stderr
        assert result.returncode == 0, output
        assert "upgrade-split-db-roles: done" in result.stdout
        assert "restart postgres" in result.stdout
        for secret in (ADMIN_PW, APP_PW, OLD_PW):
            assert secret not in output
            assert secret not in cluster.logs()

        # The held session was terminated by the script.
        with pytest.raises((asyncpg.PostgresError, asyncpg.InterfaceError, ConnectionError, OSError)):
            loop.run_until_complete(asyncio.wait_for(held.fetchval("SELECT 1"), 10))
        assert held.is_closed()
        assert q(cluster, "SELECT count(*) FROM pg_stat_activity WHERE usesysid = 10 "
                          "AND backend_type = 'client backend' AND pid <> pg_backend_pid()") == ["0"]

        # Nor can the old identity come back: neither name accepts the old password.
        for user in ("obsidian_mcp", "postgres"):
            with pytest.raises(asyncpg.InvalidPasswordError):
                loop.run_until_complete(asyncpg.connect(dsn % (user, OLD_PW)))
    finally:
        if not held.is_closed():
            held.terminate()
        loop.close()

    r = dict(line.split("|", 1)[::-1] for line in roles(cluster, "postgres"))
    assert r["postgres|t"] == "10"
    assert "obsidian_mcp|f" in r
    assert not any(k.startswith("obsidian_mcp_split_tmp") for k in r)
    attrs = q(cluster, "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls, rolcanlogin "
                       "FROM pg_roles WHERE rolname = 'obsidian_mcp'")
    assert attrs == ["f|f|f|f|f|t"]


def test_every_promised_category_changed_owner(cluster):
    own = owners(cluster, "postgres")
    by_key = {}
    for line in own:
        parts = line.split("|")
        by_key[(parts[0], parts[1])] = parts[-1]

    assert by_key[("db", "obsidian_mcp")] == "obsidian_mcp"
    assert by_key[("nsp", "extra")] == "obsidian_mcp"
    assert by_key[("nsp", "public")] == "pg_database_owner"  # PG >= 15, left alone
    for rel in ("alembic_version", "canary", "api_keys", "note_embeddings", "free_seq",
                "serial_t", "serial_t_id_seq", "serial_t_idn_seq", "v_one", "mv_one", "extra.t",
                "serial_t_pkey"):
        assert by_key[("rel", rel)] == "obsidian_mcp", rel
    for proc in ("f_one(integer)", "p_one()", "my_sum(integer)"):
        assert by_key[("proc", proc)] == "obsidian_mcp", proc
    for typ in ("mood", "posint", "pair", "floatrange", "floatmultirange", "mood[]", "canary"):
        assert by_key[("type", typ)] == "obsidian_mcp", typ
    assert by_key[("stx", "st_one")] == "obsidian_mcp"
    assert by_key[("lo", "obsidian_mcp")] == "obsidian_mcp"

    # Every relation in the application schemas, whatever migrations created.
    rels = [line for line in own if line.startswith("rel|")]
    assert rels and all(line.endswith("|obsidian_mcp") for line in rels), [
        line for line in rels if not line.endswith("|obsidian_mcp")
    ]

    # The extension and its whole dependency closure stay with postgres.
    assert by_key[("ext", "vector")] == "postgres"
    closure_owners = q(cluster, """
        WITH RECURSIVE closure(classid, objid) AS (
            SELECT classid, objid FROM pg_depend
             WHERE deptype = 'e' AND refobjid = (SELECT oid FROM pg_extension WHERE extname = 'vector')
            UNION
            SELECT d.classid, d.objid FROM pg_depend d
              JOIN closure c ON d.refclassid = c.classid AND d.refobjid = c.objid
             WHERE d.deptype = 'i')
        SELECT DISTINCT c.classid::regclass::text || '|' || pg_get_userbyid(COALESCE(
            (SELECT typowner FROM pg_type WHERE oid = c.objid AND c.classid = 'pg_type'::regclass),
            (SELECT proowner FROM pg_proc WHERE oid = c.objid AND c.classid = 'pg_proc'::regclass),
            (SELECT oprowner FROM pg_operator WHERE oid = c.objid AND c.classid = 'pg_operator'::regclass),
            (SELECT opcowner FROM pg_opclass WHERE oid = c.objid AND c.classid = 'pg_opclass'::regclass),
            (SELECT opfowner FROM pg_opfamily WHERE oid = c.objid AND c.classid = 'pg_opfamily'::regclass),
            (SELECT relowner FROM pg_class WHERE oid = c.objid AND c.classid = 'pg_class'::regclass),
            10))
          FROM closure c WHERE c.classid <> 'pg_cast'::regclass ORDER BY 1
    """)
    assert closure_owners, "the vector extension has no members?"
    assert {line.split("|")[0] for line in closure_owners} >= {
        "pg_type", "pg_proc", "pg_operator", "pg_opclass", "pg_opfamily"
    }
    assert all(line.endswith("|postgres") for line in closure_owners), closure_owners
    assert q(cluster, "SELECT typname || '|' || pg_get_userbyid(typowner) FROM pg_type "
                      "WHERE typname IN ('vector', '_vector', 'halfvec', '_halfvec') ORDER BY 1") == [
        "_halfvec|postgres", "_vector|postgres", "halfvec|postgres", "vector|postgres"
    ]

    # Nothing user-created is the superuser's any more, in any catalog.
    leftovers = q(cluster, """
        SELECT 'pg_class|' || oid::regclass FROM pg_class WHERE relowner = 10 AND oid >= 16384
           AND relnamespace NOT IN (SELECT oid FROM pg_namespace WHERE nspname ~ '^pg_')
           AND oid NOT IN (SELECT objid FROM pg_depend WHERE deptype = 'e')
        UNION ALL SELECT 'pg_proc|' || oid::regprocedure FROM pg_proc WHERE proowner = 10 AND oid >= 16384
           AND oid NOT IN (SELECT objid FROM pg_depend WHERE deptype = 'e')
        UNION ALL SELECT 'pg_type|' || oid::regtype FROM pg_type t WHERE typowner = 10 AND oid >= 16384
           AND oid NOT IN (SELECT objid FROM pg_depend WHERE deptype = 'e')
           AND NOT EXISTS (SELECT 1 FROM pg_depend d JOIN pg_depend e ON e.objid = d.refobjid AND e.deptype = 'e'
                            WHERE d.objid = t.oid AND d.deptype = 'i')
           AND typnamespace NOT IN (SELECT oid FROM pg_namespace WHERE nspname ~ '^pg_')
        UNION ALL SELECT 'pg_namespace|' || nspname FROM pg_namespace WHERE nspowner = 10 AND oid >= 16384
           AND nspname !~ '^pg_'
    """)
    assert leftovers == []

    # Default privileges moved: none left for OID 10, equivalent ones for obsidian_mcp.
    assert q(cluster, "SELECT count(*) FROM pg_default_acl WHERE defaclrole = 10") == ["0"]
    acl = q(cluster, """
        SELECT coalesce(d.defaclnamespace::regnamespace::text, '-') || '|' || d.defaclobjtype::text || '|' ||
               CASE e.grantee WHEN 0 THEN 'PUBLIC' ELSE pg_get_userbyid(e.grantee) END || '|' || e.privilege_type
          FROM pg_default_acl d, aclexplode(d.defaclacl) e
         WHERE d.defaclrole = (SELECT oid FROM pg_roles WHERE rolname = 'obsidian_mcp')
         ORDER BY 1
    """)
    assert "-|r|reader|SELECT" in acl
    assert "-|r|obsidian_mcp|SELECT" in acl  # the owner's own default stays
    assert "-|f|obsidian_mcp|EXECUTE" in acl
    assert "-|f|PUBLIC|EXECUTE" not in acl  # the revoke carried over
    assert "extra|S|reader|USAGE" in acl


def test_new_credentials_work_and_schema_is_clean(cluster):
    import asyncpg

    async def check():
        dsn = f"postgresql://%s:%s@127.0.0.1:{cluster.port}/obsidian_mcp"
        conn = await asyncpg.connect(dsn % ("postgres", ADMIN_PW))
        try:
            assert await conn.fetchval("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
        finally:
            await conn.close()
        conn = await asyncpg.connect(dsn % ("obsidian_mcp", APP_PW))
        try:
            assert await conn.fetchval("SELECT current_user") == "obsidian_mcp"
            assert not await conn.fetchval("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
            assert await conn.fetchval("SELECT v FROM canary") == "survives the split"
            # The runtime role can still write the tables it now owns.
            await conn.execute("INSERT INTO canary VALUES ('written after')")
            await conn.execute("REFRESH MATERIALIZED VIEW mv_one")
        finally:
            await conn.close()

    asyncio.run(check())
    # Only the runtime role can drop what it owns; the seeded tables would
    # otherwise be reported as drift by `alembic check`.
    cluster.psql("DROP TABLE canary, serial_t;", user="obsidian_mcp")
    run_alembic(cluster.url("obsidian_mcp", APP_PW), "upgrade", "head")
    result = run_alembic(cluster.url("obsidian_mcp", APP_PW), "check")
    assert "No new upgrade operations detected" in result.stdout + result.stderr


def test_rerun_is_a_no_op(cluster):
    before = (roles(cluster, "postgres"), owners(cluster, "postgres"))
    result = run_script(cluster, env={"POSTGRES_PASSWORD": ADMIN_PW, "OBSIDIAN_DB_PASSWORD": APP_PW})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "already split" in result.stdout
    assert (roles(cluster, "postgres"), owners(cluster, "postgres")) == before


def test_left_behind_temp_role_is_removed(cluster):
    # The state an interrupted run leaves: conversion committed, temp role alive.
    cluster.psql("CREATE ROLE obsidian_mcp_split_tmp LOGIN SUPERUSER;", user="postgres")
    assert any("obsidian_mcp_split_tmp" in line for line in roles(cluster, "postgres"))
    result = run_script(cluster, env={"POSTGRES_PASSWORD": ADMIN_PW, "OBSIDIAN_DB_PASSWORD": APP_PW})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "already split" in result.stdout
    assert not any("obsidian_mcp_split_tmp" in line for line in roles(cluster, "postgres"))


def test_unrecognised_shape_is_refused():
    # Bootstrap superuser `postgres` with no `obsidian_mcp` role: neither shape.
    c = Container(unique("noshape"), ["-e", f"POSTGRES_PASSWORD={OLD_PW}"])
    try:
        c.wait_ready("postgres")
        c.psql("CREATE DATABASE obsidian_mcp;", user="postgres", db="postgres")
        result = run_script(
            c, env={"POSTGRES_PASSWORD": ADMIN_PW, "OBSIDIAN_DB_PASSWORD": APP_PW},
            command=DOCUMENTED.replace("-U obsidian_mcp", "-U postgres"),
        )
        assert result.returncode != 0
        assert "unrecognised cluster shape" in result.stderr
        assert "Nothing was changed" in result.stderr
        assert q(c, "SELECT count(*) FROM pg_roles WHERE rolname LIKE 'obsidian_mcp%'") == ["0"]
    finally:
        c.remove()


# The fresh-install shape, as docker/db-init-compose.sh builds it.
FRESH_SHAPE = """
CREATE ROLE obsidian_mcp LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD 'x';
CREATE DATABASE obsidian_mcp OWNER obsidian_mcp;
"""

# (defect applied as postgres in obsidian_mcp, its undo, what the refusal must name)
MALFORMED_SPLITS = [
    ("ALTER DATABASE obsidian_mcp OWNER TO postgres;",
     "ALTER DATABASE obsidian_mcp OWNER TO obsidian_mcp;",
     ["database obsidian_mcp is owned by postgres"]),
    ("ALTER ROLE obsidian_mcp CREATEDB CREATEROLE;",
     "ALTER ROLE obsidian_mcp NOCREATEDB NOCREATEROLE;",
     ["role obsidian_mcp has CREATEDB, CREATEROLE"]),
    ("CREATE TABLE leftover (i int); CREATE FUNCTION leftover_fn() RETURNS int LANGUAGE sql AS 'SELECT 1';",
     "DROP TABLE leftover; DROP FUNCTION leftover_fn();",
     ["objects still owned by the superuser:", "table leftover", "function leftover_fn()"]),
    ("ALTER DEFAULT PRIVILEGES FOR ROLE postgres GRANT SELECT ON TABLES TO PUBLIC;",
     "ALTER DEFAULT PRIVILEGES FOR ROLE postgres REVOKE SELECT ON TABLES FROM PUBLIC;",
     ["default privileges still held by the superuser: TABLES database-wide"]),
    ("ALTER DATABASE obsidian_mcp OWNER TO postgres; ALTER ROLE obsidian_mcp BYPASSRLS;",
     "ALTER DATABASE obsidian_mcp OWNER TO obsidian_mcp; ALTER ROLE obsidian_mcp NOBYPASSRLS;",
     ["database obsidian_mcp is owned by postgres", "role obsidian_mcp has BYPASSRLS"]),
    # Codex round 2: a plain obsidian_mcp that can SET ROLE to the superuser.
    ("GRANT postgres TO obsidian_mcp;",
     "REVOKE postgres FROM obsidian_mcp;",
     ["role obsidian_mcp is a member (directly or through other roles) of privileged role(s) postgres (SUPERUSER, "]),
    # ... and through an intermediate role, to two privileged roles at once.
    ("CREATE ROLE omcp_mid NOLOGIN; CREATE ROLE omcp_maker NOLOGIN CREATEROLE;"
     " GRANT postgres TO omcp_mid; GRANT omcp_mid TO obsidian_mcp; GRANT omcp_maker TO omcp_mid;",
     "DROP ROLE omcp_mid; DROP ROLE omcp_maker;",
     ["of privileged role(s) omcp_maker (CREATEROLE), postgres (SUPERUSER, "]),
    # A predefined superuser-equivalent role (server filesystem access).
    ("GRANT pg_read_server_files TO obsidian_mcp;",
     "REVOKE pg_read_server_files FROM obsidian_mcp;",
     ["of privileged role(s) pg_read_server_files (server file/program access)"]),
    # Codex round 2: the extension owned by the runtime role.
    ("DROP EXTENSION vector; ALTER ROLE obsidian_mcp SUPERUSER; SET ROLE obsidian_mcp;"
     " CREATE EXTENSION vector; RESET ROLE; ALTER ROLE obsidian_mcp NOSUPERUSER;",
     "DROP EXTENSION vector; CREATE EXTENSION vector;",
     ["extension vector is owned by obsidian_mcp, not postgres"]),
]

MEMBERSHIPS = """
SELECT pg_get_userbyid(roleid) || '>' || pg_get_userbyid(member) || '|' || admin_option::text
  FROM pg_auth_members ORDER BY 1
"""


def test_partly_split_cluster_is_refused():
    # Bootstrap superuser `postgres` plus a non-superuser `obsidian_mcp`: the
    # name-level "already split" shape. Only the full self-check may call it
    # done (Codex, MINOR).
    c = Container(unique("partsplit"), ["-e", f"POSTGRES_PASSWORD={OLD_PW}"])
    env = {"POSTGRES_PASSWORD": ADMIN_PW, "OBSIDIAN_DB_PASSWORD": APP_PW}
    try:
        c.wait_ready("postgres")
        c.psql(FRESH_SHAPE, user="postgres", db="postgres")
        c.psql("CREATE EXTENSION vector;", user="postgres")

        # The real fresh-install shape passes.
        result = run_script(c, env=env)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "already split" in result.stdout

        for defect, undo, named in MALFORMED_SPLITS:
            c.psql(defect, user="postgres")
            # A temporary role left by an interrupted run is still removed.
            c.psql("CREATE ROLE obsidian_mcp_split_tmp LOGIN SUPERUSER;", user="postgres")
            before = (roles(c, "postgres"), owners(c, "postgres"), q(c, MEMBERSHIPS))
            result = run_script(c, env=env)
            output = result.stdout + result.stderr
            assert result.returncode != 0, (defect, output)
            assert "already split" not in result.stdout, defect
            assert "not in the fresh-install shape" in result.stderr, (defect, output)
            for text in named:
                assert text in result.stderr, (defect, text, output)
            for secret in (ADMIN_PW, APP_PW):
                assert secret not in output
            after_roles = roles(c, "postgres")
            assert not any("obsidian_mcp_split_tmp" in line for line in after_roles)
            assert after_roles == [r for r in before[0] if "obsidian_mcp_split_tmp" not in r]
            assert owners(c, "postgres") == before[1]
            assert q(c, MEMBERSHIPS) == before[2]
            c.psql(undo, user="postgres")

        result = run_script(c, env=env)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "already split" in result.stdout
    finally:
        c.remove()
