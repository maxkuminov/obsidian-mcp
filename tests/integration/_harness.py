"""Throwaway-pgvector harness shared by the opt-in integration modules.

Extracted from `tests/integration/test_pgvector_search.py`, which keeps its own
copy so that module stays self-contained. Same contract:

`PGVECTOR_TEST_ADMIN_URL` names a **server** to create a throwaway database on.
The harness never touches the database in the URL itself beyond using it as a
maintenance connection: it `CREATE DATABASE test_<something>_<uuid>`, migrates
*that*, hands it to the test, and drops it in teardown — so a mistyped URL
cannot cost data. Pointing it at the production database name is a hard
failure, not a skip.

    docker run --rm -d --name pgvector-search-test -e POSTGRES_PASSWORD=test \\
        -p 55433:5432 pgvector/pgvector:pg16
    PGVECTOR_TEST_ADMIN_URL=postgresql+asyncpg://postgres:test@localhost:55433/postgres \\
        pytest -q tests/integration/
    docker rm -f pgvector-search-test

Note the harness lets a module choose `EMBEDDING_DIMENSIONS` for the migration
subprocess. The benchmarks need thousands of vectors for the planner to prefer
the HNSW index at all; at the production 1024 dims that is minutes of insert
time for no extra coverage, since none of the code under test reads the width.
"""
import asyncio
import os
import secrets
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit

import pytest
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.expression import ClauseElement, Executable

PGVECTOR_TEST_ADMIN_URL = os.environ.get("PGVECTOR_TEST_ADMIN_URL")

# The production database name. Refusing it is a backstop against someone
# exporting the live URL here: the harness would otherwise open a maintenance
# connection to it (and, on a bad edit, run migrations against it).
FORBIDDEN_DB_NAMES = {"obsidian_mcp"}

ROOT = Path(__file__).resolve().parent.parent.parent

requires_pgvector = pytest.mark.skipif(
    not PGVECTOR_TEST_ADMIN_URL,
    reason="set PGVECTOR_TEST_ADMIN_URL to run pgvector integration tests",
)


class Explain(Executable, ClauseElement):
    """`EXPLAIN <statement>`, with the statement's parameters bound normally.

    The obvious alternative — compiling with `literal_binds` and interpolating
    into `text("EXPLAIN …")` — cannot render every type the production
    statements carry: a `frontmatter @> :jsonb` filter has no literal renderer
    and raises `CompileError`. Silently skipping that shape would leave the one
    filter whose plan nobody had ever looked at unasserted, which is the exact
    failure mode these EXPLAIN assertions exist to prevent. Going through the
    driver instead lets JSONB, `vector`, and arrays bind the way they do in
    production.
    """

    inherit_cache = False

    def __init__(self, statement):
        self.statement = statement


@compiles(Explain, "postgresql")
def _compile_explain(element, compiler, **kw):  # pragma: no cover - via tests
    return "EXPLAIN " + compiler.process(element.statement, **kw)


async def explain(session, statement) -> str:
    """The plan for `statement`, as one string, on `session`'s transaction."""
    rows = (await session.execute(Explain(statement))).fetchall()
    return "\n".join(row[0] for row in rows)


def _with_database(url: str, dbname: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path=f"/{dbname}"))


def _asyncpg_dsn(url: str) -> str:
    """SQLAlchemy URL → a DSN asyncpg.connect() accepts."""
    parts = urlsplit(url)
    return urlunsplit(parts._replace(scheme=parts.scheme.split("+", 1)[0]))


def _admin_database_name(url: str) -> str:
    """The database an admin URL points at, normalized for comparison.

    A URL path is percent-encoded (`/obsidian%5Fmcp`) and Postgres identifiers
    fold case, so the raw path is not what the server will resolve.
    """
    return unquote(urlsplit(url).path).lstrip("/").casefold()


async def _run_maintenance(admin_url: str, statement: str) -> None:
    """Run a CREATE/DROP DATABASE on the admin URL's own database.

    asyncpg is autocommit outside an explicit transaction, which is what
    CREATE/DROP DATABASE requires.
    """
    import asyncpg

    conn = await asyncpg.connect(_asyncpg_dsn(admin_url))
    try:
        await conn.execute(statement)
    finally:
        await conn.close()


def throwaway_database(prefix: str, dimensions: int):
    """Generator body for a module-scoped fixture: yields a migrated URL."""
    admin_db = _admin_database_name(PGVECTOR_TEST_ADMIN_URL)
    if admin_db in FORBIDDEN_DB_NAMES:
        pytest.fail(
            "PGVECTOR_TEST_ADMIN_URL points at the production database name "
            f"{admin_db!r}. Point it at a throwaway server (see the module "
            "docstring); this harness creates and drops databases."
        )

    dbname = f"test_{prefix}_{uuid.uuid4().hex}"
    try:
        # CREATE sits inside the try so the DROP below runs even if creation is
        # interrupted: the name is generated, `IF EXISTS` makes the drop a
        # no-op when it never got made, and leaving a stray database behind is
        # the one outcome this harness must not have.
        asyncio.run(
            _run_maintenance(PGVECTOR_TEST_ADMIN_URL, f'CREATE DATABASE "{dbname}"')
        )
        url = _with_database(PGVECTOR_TEST_ADMIN_URL, dbname)
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=ROOT,
            env={
                **os.environ,
                "DATABASE_URL": url,
                "EMBEDDING_DIMENSIONS": str(dimensions),
                # Several offline test modules `os.environ.setdefault(
                # "SECRET_KEY", "test")` at import time, and that sticks for
                # the whole process. `Settings` rejects placeholder secrets, so
                # the migration subprocess would fail to import `src.config`
                # depending on which modules pytest collected first. Give it a
                # real one.
                "SECRET_KEY": secrets.token_hex(32),
            },
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert result.returncode == 0, (
            f"alembic upgrade head failed\n{result.stdout}\n{result.stderr}"
        )
        yield url
    finally:
        # FORCE terminates any connection the test left behind, so a failing
        # test still cleans up. Best effort: a drop that itself fails must not
        # mask the test's own error.
        try:
            asyncio.run(
                _run_maintenance(
                    PGVECTOR_TEST_ADMIN_URL,
                    f'DROP DATABASE IF EXISTS "{dbname}" (FORCE)',
                )
            )
        except Exception as e:  # pragma: no cover - cleanup best effort
            print(f"warning: could not drop throwaway database {dbname}: {e}")
