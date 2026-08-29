"""`backups_log` and the `db-backup` recording guard, against real PostgreSQL.

Two things a fake cannot answer.

**The guard.** `docker/record-backup.sh` has three branches with three
different exit dispositions, and getting one of them wrong is not visible until
the day it matters: a target that fails when `backups_log` is absent aborts the
very deploy that creates the table, and one that succeeds when the insert failed
leaves the panel reporting a staler safety net than the operator has. So the
script is executed **verbatim** here — the real `psql`, the real SQL, the real
exit codes — with only `docker exec <container>` shimmed away, because the
throwaway Postgres these tests already run against is reachable directly.

**The read.** `latest_backup` probes `to_regclass` and then orders by
`created_at DESC`; both halves are SQL and are asserted against a database that
has actually been migrated by 021, not against a fake that agrees with them.

Skipped unless `PGVECTOR_TEST_ADMIN_URL` is set — see `_harness.py`.
"""
import asyncio
import datetime
import os
import stat
import subprocess
from urllib.parse import unquote, urlsplit

import asyncpg
import pytest

import _harness

pytestmark = _harness.requires_pgvector

DIM = 64

REPO = _harness.ROOT
SCRIPT = os.path.join(REPO, "docker", "record-backup.sh")

# What the absent-table branch must print. The wording is part of the contract:
# an operator reading a deploy log has to be able to tell "this backup is not
# recorded" from "this backup failed".
ABSENT_WARNING = "backup taken but not recorded; table arrives with migration 021"


@pytest.fixture
def db(request):
    """A throwaway database at `head`, or at the revision the marker names."""
    revision = getattr(request, "param", "head")
    generator = _harness.throwaway_database(
        f"ops_health_{revision.replace('-', '_')}", DIM, revision=revision
    )
    url = next(generator)
    try:
        yield url
    finally:
        generator.close()


# --------------------------------------------------------------------------
# plumbing
# --------------------------------------------------------------------------


def _pg_env(url: str) -> dict:
    """`PG*` variables psql can connect with, from a SQLAlchemy URL."""
    parts = urlsplit(_harness.asyncpg_dsn(url))
    return {
        "PGHOST": parts.hostname or "localhost",
        "PGPORT": str(parts.port or 5432),
        "PGUSER": unquote(parts.username or "postgres"),
        "PGPASSWORD": unquote(parts.password or ""),
    }


def _dbname(url: str) -> str:
    return unquote(urlsplit(url).path).lstrip("/")


def _noisy_docker_shim(tmp_path):
    """As `_docker_shim`, but the "server" also writes a warning to stderr.

    Exactly what a healthy PostgreSQL does after a libc upgrade — the
    collation-version mismatch notice is emitted on connection. The value on
    stdout is still a perfectly good `t`.
    """
    bindir = tmp_path / "noisy-bin"
    bindir.mkdir(exist_ok=True)
    shim = bindir / "docker"
    shim.write_text(
        "#!/bin/bash\n"
        'echo "WARNING: database \\"x\\" has a collation version mismatch" >&2\n'
        'if [ "$1" != "exec" ]; then exit 99; fi\n'
        "shift\n"
        'while [ $# -gt 0 ] && [ "${1:0:1}" = "-" ]; do shift; done\n'
        "shift\n"
        'exec "$@"\n'
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return bindir


def _docker_shim(tmp_path):
    """A `docker` on PATH that runs `docker exec <name> <cmd…>` as `<cmd…>`.

    The point is to leave the script's own invocation untouched — the real
    `psql`, the real flags, the real `:'var'` interpolation and `ON_ERROR_STOP`
    semantics — while pointing it at the Postgres this test already has. A
    shim that reimplemented the query would be testing itself.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    shim = bindir / "docker"
    shim.write_text(
        "#!/bin/bash\n"
        'if [ "$1" != "exec" ]; then\n'
        '  echo "shim: expected \'docker exec\', got: $*" >&2; exit 99\n'
        "fi\n"
        "shift\n"
        "# drop docker exec's own flags (-i, -t, …), then the container name\n"
        'while [ $# -gt 0 ] && [ "${1:0:1}" = "-" ]; do shift; done\n'
        "shift\n"
        'exec "$@"\n'
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return bindir


def run_record(url, tmp_path, *, contents=b"fake dump bytes", db_name=None, noisy=False):
    """Run `docker/record-backup.sh` against `url`'s database."""
    dump = tmp_path / "backup_20260829_031500.sql.gz"
    dump.write_bytes(contents)
    bindir = _noisy_docker_shim(tmp_path) if noisy else _docker_shim(tmp_path)
    env = {
        **os.environ,
        **_pg_env(url),
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "DB_CONTAINER": "pretend-postgres",
        "DB_NAME": db_name or _dbname(url),
    }
    result = subprocess.run(
        ["bash", SCRIPT, str(dump)],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return result, dump


async def _fetch(url, statement, *args):
    conn = await asyncpg.connect(_harness.asyncpg_dsn(url))
    try:
        return await conn.fetch(statement, *args)
    finally:
        await conn.close()


def fetch(url, statement, *args):
    return asyncio.run(_fetch(url, statement, *args))


async def _execute(url, statement, *args):
    conn = await asyncpg.connect(_harness.asyncpg_dsn(url))
    try:
        return await conn.execute(statement, *args)
    finally:
        await conn.close()


def sql(url, statement, *args):
    return asyncio.run(_execute(url, statement, *args))


# --------------------------------------------------------------------------
# 1. The recording guard's three branches
# --------------------------------------------------------------------------


@pytest.mark.parametrize("db", ["020"], indirect=True)
def test_a_pre_021_database_warns_loudly_and_still_succeeds(db, tmp_path):
    """The bootstrap deploy, exactly: `make deploy` backs up **before** it
    migrates, so the dump that ships 021 runs against a database with no
    `backups_log`. Failing here would abort the deploy that creates the table —
    a bookkeeping row blocking a disaster-recovery step."""
    result, dump = run_record(db, tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert ABSENT_WARNING in result.stdout
    assert dump.exists(), "the dump itself is untouched by the recording step"


def test_a_successful_record_lands_the_row_the_page_reads(db, tmp_path):
    contents = b"x" * 4242
    result, dump = run_record(db, tmp_path, contents=contents)

    assert result.returncode == 0, result.stdout + result.stderr
    rows = fetch(db, "SELECT filename, size_bytes, created_at FROM backups_log")
    assert len(rows) == 1
    # The **basename**, never the path: the backups directory is a
    # deployment-local constant and a host path does not belong in a shared
    # table.
    assert rows[0]["filename"] == dump.name
    assert "/" not in rows[0]["filename"]
    assert rows[0]["size_bytes"] == len(contents)
    assert rows[0]["created_at"] is not None


def test_a_failing_insert_fails_the_target_loudly(db, tmp_path):
    """Once the table exists the disposition inverts. The panel reports the
    newest row as the age of the last backup, so a dump that silently failed to
    record itself makes the page claim a staler safety net than there is."""
    sql(
        db,
        "CREATE FUNCTION _refuse_backup_row() RETURNS trigger AS $$ "
        "BEGIN RAISE EXCEPTION 'no room at the inn'; END $$ LANGUAGE plpgsql",
    )
    sql(
        db,
        "CREATE TRIGGER _refuse_backup_row BEFORE INSERT ON backups_log "
        "FOR EACH ROW EXECUTE FUNCTION _refuse_backup_row()",
    )

    result, _dump = run_record(db, tmp_path)

    assert result.returncode != 0, "a failed record must fail the backup target"
    combined = result.stdout + result.stderr
    assert "Backup RECORDING FAILED" in combined
    assert "no room at the inn" in combined, "the database's own reason is surfaced"
    assert ABSENT_WARNING not in combined, (
        "a failed insert must never be reported as the benign absent-table case"
    )
    assert fetch(db, "SELECT count(*) AS n FROM backups_log")[0]["n"] == 0


def test_a_server_warning_on_stderr_does_not_look_like_a_missing_table(db, tmp_path):
    """The BLOCKER this guard was rewritten for. A healthy server writes
    warnings to stderr — a collation-version mismatch after a libc upgrade is
    emitted on connection. Folded into stdout the probe's value becomes
    `WARNING: …\\nt`, which is not the string `t`, and the absent-table branch
    then exits 0 without recording anything. Forever, on a table that exists."""
    result, dump = run_record(db, tmp_path, noisy=True)

    assert result.returncode == 0, result.stdout + result.stderr
    assert ABSENT_WARNING not in result.stdout, (
        "a warning on stderr must not be read as 'the table is absent'"
    )
    rows = fetch(db, "SELECT filename FROM backups_log")
    assert [r["filename"] for r in rows] == [dump.name], (
        "the row must land despite the noise on stderr"
    )


def test_an_unrecognised_probe_answer_fails_rather_than_being_guessed_at(db, tmp_path):
    """Neither `t` nor `f` on a successful exit means the value being read is
    not the value that was asked for. Classifying that as 'absent' is how a
    healthy database silently stops recording backups while the target keeps
    exiting 0, so it fails instead."""
    bindir = tmp_path / "liar-bin"
    bindir.mkdir()
    shim = bindir / "docker"
    shim.write_text("#!/bin/bash\necho 'surprise banner'\nexit 0\n")
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    dump = tmp_path / "backup_20260829_031500.sql.gz"
    dump.write_bytes(b"bytes")
    result = subprocess.run(
        ["bash", SCRIPT, str(dump)],
        env={
            **os.environ,
            **_pg_env(db),
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "DB_CONTAINER": "pretend-postgres",
            "DB_NAME": _dbname(db),
        },
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "neither 't' nor 'f'" in combined
    assert ABSENT_WARNING not in combined
    assert fetch(db, "SELECT count(*) AS n FROM backups_log")[0]["n"] == 0


def test_the_row_lands_in_public_even_when_search_path_points_elsewhere(db, tmp_path):
    """An unqualified INSERT resolves through `search_path`. With a decoy table
    of the same name earlier on the path, an unqualified writer records into it
    while the panel — which reads `public.backups_log` — never sees the row, and
    the target reports success either way."""
    sql(db, "CREATE SCHEMA decoy")
    sql(
        db,
        "CREATE TABLE decoy.backups_log (id serial PRIMARY KEY, "
        " created_at timestamptz NOT NULL DEFAULT now(), filename text NOT NULL, "
        " size_bytes bigint NOT NULL)",
    )
    sql(db, f'ALTER DATABASE "{_dbname(db)}" SET search_path TO decoy, public')

    result, dump = run_record(db, tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert fetch(db, "SELECT count(*) AS n FROM decoy.backups_log")[0]["n"] == 0, (
        "the decoy must stay empty"
    )
    rows = fetch(db, "SELECT filename FROM public.backups_log")
    assert [r["filename"] for r in rows] == [dump.name]

    # And the panel's own read finds it, for the same reason.
    assert _latest(db)["filename"] == dump.name


def test_a_probe_that_cannot_answer_fails_rather_than_warning(db, tmp_path):
    """Neither branch: the database answered a full `pg_dump` through this exact
    channel seconds earlier, so a `psql` that cannot run one catalog lookup is a
    fault. Guessing "the table is probably absent" would convert every such
    fault into the benign warning."""
    result, _dump = run_record(db, tmp_path, db_name="no_such_database_here")

    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "could not ask the database" in combined
    assert ABSENT_WARNING not in combined


def test_a_zero_byte_dump_cannot_be_recorded_as_a_backup(db, tmp_path):
    """`db-backup` refuses an empty dump before it ever gets here; the CHECK is
    that refusal made true of the database rather than of one shell script."""
    result, _dump = run_record(db, tmp_path, contents=b"")

    assert result.returncode != 0
    assert "ck_backups_log_size_bytes" in result.stdout + result.stderr
    assert fetch(db, "SELECT count(*) AS n FROM backups_log")[0]["n"] == 0


# --------------------------------------------------------------------------
# 2. The read the page performs
# --------------------------------------------------------------------------


def _latest(url):
    """`ops_health.latest_backup` over a real session."""
    import sys

    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from src.services.ops_health import latest_backup

    async def _go():
        engine = create_async_engine(url)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                return await latest_backup(session)
        finally:
            await engine.dispose()

    return asyncio.run(_go())


@pytest.mark.parametrize("db", ["020"], indirect=True)
def test_the_read_survives_a_database_without_the_table(db):
    """The same pre-021 database the guard warns on. A page that 500s here is a
    page nobody can use to find out why their deploy went wrong."""
    assert _latest(db) is None


def test_the_read_returns_the_newest_row_with_its_age(db):
    old = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=40)
    recent = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=2)
    sql(
        db,
        "INSERT INTO backups_log (created_at, filename, size_bytes) "
        "VALUES ($1, 'older.sql.gz', 10), ($2, 'newer.sql.gz', 20)",
        old,
        recent,
    )
    backup = _latest(db)
    assert backup["filename"] == "newer.sql.gz"
    assert backup["age_days"] == 2
    assert backup["stale"] is False


def test_a_backup_older_than_the_threshold_reads_as_stale(db):
    sql(
        db,
        "INSERT INTO backups_log (created_at, filename, size_bytes) VALUES "
        "($1, 'ancient.sql.gz', 10)",
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=40),
    )
    backup = _latest(db)
    assert backup["stale"] is True
    assert backup["age_days"] == 40


def test_an_empty_table_reads_as_nothing_recorded(db):
    assert _latest(db) is None
