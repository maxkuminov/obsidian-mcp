"""`make db-backup` dumps the derived index tables schema-only (#348).

Five tables are a cache of the vault plus settings: the index pass rewrites
`notes_metadata` and `note_links`, the embed pass refills `note_embeddings`,
`indexer_runs` is display history and `indexer_state`'s fingerprints are
re-adopted at startup when absent. Their rows are the bulk of every dump, so
`pg_dump` carries `--exclude-table-data` for each of them unless
`BACKUP_FULL=1`. The other twelve tables are durable — credentials, grants,
users, usage, quotas, `backups_log`, the concurrency evidence — and must never
appear in an exclusion.

Hermetic: the Makefile, the Kubernetes CronJob script and `src/models/db.py`
are read as text. The one thing that needs `make` (the recipe's expansion in
both modes) skips where it is absent.

The two table sets below are exact on purpose. A new ORM table fails
`test_every_table_is_classified` until it is added to one of them, which is
the moment to decide whether a restore can rebuild it from the vault.
"""
import os
import re
import shutil
import subprocess

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.join(HERE, "..")

DERIVED = {
    "notes_metadata",
    "note_embeddings",
    "note_links",
    "indexer_runs",
    "indexer_state",
}
DURABLE = {
    "users",
    "api_keys",
    "usage_logs",
    "oauth_clients",
    "oauth_codes",
    "oauth_tokens",
    "transfer_tokens",
    "quota_counters",
    "backups_log",
    "user_sessions",
    "concurrency_counters",
    "concurrency_runs",
}


def _read(*parts: str) -> str:
    with open(os.path.join(REPO, *parts)) as fh:
        return fh.read()


def _orm_tables() -> set[str]:
    return set(re.findall(r'^\s*__tablename__\s*=\s*"([a-z_]+)"', _read("src", "models", "db.py"), re.M))


def _makefile_derived_list() -> list[str]:
    make = _read("Makefile")
    match = re.search(r"^BACKUP_DERIVED_TABLES\s*:=\s*(.+)$", make, re.M)
    assert match, "the Makefile must define BACKUP_DERIVED_TABLES"
    return match.group(1).split()


def _cronjob_derived_list() -> list[str]:
    script = _read("deploy", "kubernetes", "components", "db-backup", "configmap.yaml")
    match = re.search(r"for t in ([a-z_ ]+); do\s*\n\s*exclude\+=\(\"--exclude-table-data=\$t\"\)", script)
    assert match, "the CronJob script must build --exclude-table-data flags from a table list"
    return match.group(1).split()


def test_every_table_is_classified():
    """Each ORM table is either derived (rebuilt from the vault after a
    restore) or durable (why backups and alembic cannot go away) — and the
    two sets are the whole schema, with nothing in both."""
    tables = _orm_tables()
    assert DERIVED & DURABLE == set()
    assert tables == DERIVED | DURABLE, (
        f"unclassified or stale tables: {tables ^ (DERIVED | DURABLE)}; "
        "add a new table to DERIVED or DURABLE above, and to the Makefile's "
        "BACKUP_DERIVED_TABLES and the CronJob script if it is derived"
    )


def test_makefile_excludes_exactly_the_derived_tables():
    assert set(_makefile_derived_list()) == DERIVED
    make = _read("Makefile")
    recipe = make[make.index("\ndb-backup:") : make.index("\ndb-restore:")]
    dump_lines = [line for line in recipe.splitlines() if "pg_dump" in line and "$(DB_EXEC)" in line]
    assert len(dump_lines) == 1, dump_lines
    assert "$(BACKUP_EXCLUDE_DATA)" in dump_lines[0]
    # The flag is built from the list, once, and only when BACKUP_FULL is unset or 0.
    definition = re.search(r"^BACKUP_EXCLUDE_DATA\s*=\s*(.+)$", make, re.M)
    assert definition
    assert "--exclude-table-data=$(t)" in definition.group(1)
    assert "BACKUP_FULL" in definition.group(1)
    for table in DURABLE:
        assert f"--exclude-table-data={table}" not in make


def test_cronjob_script_mirrors_the_makefile_list():
    assert set(_cronjob_derived_list()) == DERIVED
    script = _read("deploy", "kubernetes", "components", "db-backup", "configmap.yaml")
    assert 'if [ "${BACKUP_FULL:-0}" = 0 ]' in script
    assert 'pg_dump --no-owner ${exclude[@]+"${exclude[@]}"} --dbname="$url"' in script
    for table in DURABLE:
        assert f"--exclude-table-data={table}" not in script
    cronjob = _read("deploy", "kubernetes", "components", "db-backup", "cronjob.yaml")
    assert "name: BACKUP_FULL" in cronjob


def test_restore_and_recording_assume_nothing_about_the_derived_rows():
    """`db-restore.sh` verifies the file and loads it; `record-backup.sh`
    writes one `backups_log` row. Neither may count or read a derived table,
    or a schema-only dump would restore or record differently from a full one."""
    for script in ("db-restore.sh", "record-backup.sh"):
        text = _read("docker", script)
        for table in DERIVED:
            assert table not in text, (script, table)
        assert "backups_log" in text or script == "db-restore.sh"


@pytest.mark.skipif(shutil.which("make") is None, reason="GNU make is not installed")
@pytest.mark.parametrize("full", [False, True])
def test_recipe_expands_to_the_expected_pg_dump_line(full):
    """`make -n` prints the expanded recipe without running it: the dump line
    carries one `--exclude-table-data` per derived table by default, and none
    at all under BACKUP_FULL=1."""
    args = ["make", "-n", "db-backup", "DATA_DIR=/nonexistent"]
    if full:
        args.append("BACKUP_FULL=1")
    result = subprocess.run(args, cwd=REPO, capture_output=True, text=True, check=True)
    dump_lines = [line for line in result.stdout.splitlines() if "pg_dump -U postgres" in line]
    assert len(dump_lines) == 1, result.stdout
    flags = set(re.findall(r"--exclude-table-data=([a-z_]+)", dump_lines[0]))
    assert flags == (set() if full else DERIVED)
    # The database name stays the last positional argument, after the flags.
    assert re.search(r"pg_dump -U postgres(?: --exclude-table-data=[a-z_]+)* +obsidian_mcp >", dump_lines[0])
