#!/bin/bash
# Record a completed database backup in `backups_log` (migration 021, #163).
#
# Called by `make db-backup` after the dump has been written, gzipped and
# checked. It exists as a script rather than as five more continuation lines in
# the Makefile recipe for one reason: the guard below has three branches with
# three different exit dispositions, and a guard nobody can run in a test is a
# guard nobody has ever seen take its unhappy path. `make db-init` already
# delegates to `docker/db-init.sh` the same way.
#
#   record-backup.sh <path-to-dump>
#
#   DB_CONTAINER   the PostgreSQL container (the Makefile passes its own value,
#                  which `Makefile.local` may override)
#   DB_NAME        the database, defaulting to the deployment's `obsidian_mcp`
#
# ## The three branches, and why they differ
#
# The insert goes through **the same `docker exec … psql` channel the dump
# itself used**. It has to: the container that runs the application cannot see
# the backups directory (deliberately — mounting it would put a host path into
# a public repo's compose file), so this script is host-side, and the only
# database handle a host-side script has is the one `pg_dump` just used.
#
#   1. `backups_log` absent → **warn loudly, exit 0.** `make deploy` runs
#      `db-backup` *before* `db-migrate`, so on the deploy that ships migration
#      021 the table does not exist yet when the backup runs. The backup is the
#      only way back from a bad migration; failing the target here would abort
#      the very deploy that creates the table, which is a bookkeeping row
#      blocking a disaster-recovery step. The dump is on disk and is valid — it
#      is simply unrecorded, and the message says so.
#   2. `backups_log` present, insert lands → silent success, one line printed.
#   3. `backups_log` present, insert fails → **exit non-zero.** Once the table
#      exists, the record is part of what a backup *is*: the panel reports the
#      newest row as the age of the last backup, so a dump that silently failed
#      to record itself makes the page claim the database has not been backed
#      up for longer than it has — which is the direction that gets a deploy
#      run against a stale safety net.
#
# A probe that cannot answer at all is treated as branch 3. The database
# answered a full `pg_dump` seconds ago through this exact channel; a `psql`
# that then cannot run one catalog lookup is a real fault, and guessing "the
# table is probably absent" would convert every such fault into branch 1's
# warning-and-continue.
set -u

GREEN='\033[0;32m'
YELLOW='\033[0;33m'
RED='\033[0;31m'
NC='\033[0m'

BACKUP_PATH="${1:-}"
DB_CONTAINER="${DB_CONTAINER:-}"
DB_NAME="${DB_NAME:-obsidian_mcp}"

if [ -z "$BACKUP_PATH" ] || [ -z "$DB_CONTAINER" ]; then
    echo -e "${RED}record-backup.sh: usage: DB_CONTAINER=<container> record-backup.sh <dump-path>${NC}" >&2
    exit 2
fi

if [ ! -f "$BACKUP_PATH" ]; then
    echo -e "${RED}record-backup.sh: $BACKUP_PATH does not exist${NC}" >&2
    exit 2
fi

# The basename, never the path: the backups directory is a deployment-local
# constant (`DATA_DIR`, overridden in the gitignored `Makefile.local`) and a
# host path does not belong in a shared table.
FILENAME=$(basename "$BACKUP_PATH")
SIZE=$(wc -c < "$BACKUP_PATH" | tr -d '[:space:]')

# The SQL arrives on **stdin**, not through `-c`. psql does not interpolate its
# `:'var'` variables into a `-c` command — that string goes to the server as
# typed, and `:'filename'` is a syntax error there — while a statement read from
# stdin is interpolated and quoted by psql itself. That quoting is the point:
# the filename never becomes shell text inside a SQL string.
psql_run() {
    docker exec -i "$DB_CONTAINER" psql -U postgres -d "$DB_NAME" \
        -v ON_ERROR_STOP=1 -qtAX "$@"
}

if ! TABLE_PRESENT=$(psql_run <<<"SELECT to_regclass('public.backups_log') IS NOT NULL" 2>&1); then
    echo -e "${RED}Backup RECORDING FAILED: could not ask the database whether backups_log exists${NC}" >&2
    echo "$TABLE_PRESENT" >&2
    echo -e "${YELLOW}The dump itself is intact at $BACKUP_PATH. This is the same docker exec psql channel pg_dump just used, so a failure here is a real fault rather than a missing table.${NC}" >&2
    exit 1
fi

# `-qtAX` prints the bare value; trim in case a future psql pads it.
TABLE_PRESENT=$(printf '%s' "$TABLE_PRESENT" | tr -d '[:space:]')

if [ "$TABLE_PRESENT" != "t" ]; then
    echo -e "${YELLOW}WARNING: backup taken but not recorded; table arrives with migration 021.${NC}"
    echo -e "${YELLOW}         $FILENAME is on disk and valid — backups_log does not exist on this database yet,${NC}"
    echo -e "${YELLOW}         which is expected on the deploy that ships 021 (db-backup runs before db-migrate).${NC}"
    echo -e "${YELLOW}         The health page will keep reporting the previous recorded backup, or none at all,${NC}"
    echo -e "${YELLOW}         until the next db-backup after 021 is live.${NC}"
    exit 0
fi

if ! INSERT_OUTPUT=$(psql_run -v filename="$FILENAME" -v size_bytes="$SIZE" 2>&1 <<'SQL'
INSERT INTO backups_log (filename, size_bytes) VALUES (:'filename', :'size_bytes');
SQL
); then
    echo -e "${RED}Backup RECORDING FAILED: backups_log exists but the row did not land${NC}" >&2
    echo "$INSERT_OUTPUT" >&2
    echo -e "${YELLOW}The dump itself is intact at $BACKUP_PATH. Failing loudly because the panel reads the newest backups_log row as the age of the last backup: an unrecorded backup makes it report a staler safety net than you have.${NC}" >&2
    exit 1
fi

echo -e "${GREEN}Recorded: $FILENAME ($SIZE bytes)${NC}"
