#!/bin/bash
# Entrypoint wrapper for the bundled compose stacks' postgres service (#324).
#
# Runs on EVERY start of the postgres container, before the image's own
# `docker-entrypoint.sh`, and therefore before `initdb` can ever run. A failing
# /docker-entrypoint-initdb.d script leaves an initialised data directory
# behind and the next start skips initialisation, so validating inside the
# init script would be too late: a weak password must never initialise a
# volume. See openspec/changes/compose-db-roles/design.md D3.
#
# Checks, each reported on its own line naming the variable and the file it
# belongs in. A password VALUE is never printed.
#
#   1. POSTGRES_PASSWORD (postgres.env) and OBSIDIAN_DB_PASSWORD (.env) are set
#      and non-empty;
#   2. each is at least 24 characters long;
#   3. neither is a shipped placeholder (compared case-insensitively after
#      trimming surrounding whitespace);
#   4. OBSIDIAN_DB_PASSWORD uses only A-Z a-z 0-9 . _ ~ - (compose splices it
#      into DATABASE_URL unencoded);
#   5. the two differ;
#   6. the volume is not half-initialised: if PG_VERSION exists and the init
#      script's state marker exists but does not read `complete`, the init
#      script started and did not finish (design D4).
#
# Then `exec docker-entrypoint.sh "$@"`, unchanged.
set -u

MIN_LEN=24
# The placeholders this repository has ever shipped, lower-cased.
PLACEHOLDERS=" changeme change_me change-me "

failed=0
fail() {
    echo "postgres-entrypoint: $*" >&2
    failed=1
}

trim() {
    local v="$1"
    v="${v#"${v%%[![:space:]]*}"}"
    v="${v%"${v##*[![:space:]]}"}"
    printf '%s' "$v"
}

# check_password NAME FILE
check_password() {
    local name="$1" file="$2" value folded
    if [ -z "${!name+x}" ] || [ -z "${!name}" ]; then
        fail "$name is not set or empty. Set it in $file (see DEPLOYMENT.md)."
        return
    fi
    value="${!name}"
    if [ "${#value}" -lt "$MIN_LEN" ]; then
        fail "$name is shorter than $MIN_LEN characters. Set a longer value in $file (openssl rand -hex 32)."
    fi
    folded="$(trim "$value" | tr '[:upper:]' '[:lower:]')"
    case "$PLACEHOLDERS" in
        *" $folded "*)
            fail "$name is a shipped placeholder. Generate a real value in $file (openssl rand -hex 32)."
            ;;
    esac
}

check_password POSTGRES_PASSWORD postgres.env
check_password OBSIDIAN_DB_PASSWORD .env

if [ -n "${OBSIDIAN_DB_PASSWORD:-}" ] && ! [[ "$OBSIDIAN_DB_PASSWORD" =~ ^[A-Za-z0-9._~-]+$ ]]; then
    fail "OBSIDIAN_DB_PASSWORD in .env may contain only A-Z a-z 0-9 . _ ~ - (it is placed into DATABASE_URL unencoded). openssl rand -hex 32 produces a valid value."
fi

if [ -n "${POSTGRES_PASSWORD:-}" ] && [ -n "${OBSIDIAN_DB_PASSWORD:-}" ] \
        && [ "$POSTGRES_PASSWORD" = "$OBSIDIAN_DB_PASSWORD" ]; then
    fail "POSTGRES_PASSWORD (postgres.env) and OBSIDIAN_DB_PASSWORD (.env) are equal. The superuser and the application role must not share a password."
fi

data_dir="${PGDATA:-/var/lib/postgresql/data}"
state_file="$data_dir/obsidian-mcp-init.state"
if [ -s "$data_dir/PG_VERSION" ] && [ -e "$state_file" ] \
        && [ "$(cat "$state_file" 2>/dev/null)" != "complete" ]; then
    fail "the database volume is half-initialised: the first-start init script (docker/db-init-compose.sh) began but did not finish, and PostgreSQL will not run it again. The volume holds no application data yet. Read the earlier postgres log for the cause, fix it, then recreate the volume: docker compose -f <bundle> down -v (removes this stack's volumes) or docker volume rm <project>_pg_data, and start again."
fi

if [ "$failed" -ne 0 ]; then
    echo "postgres-entrypoint: refusing to start PostgreSQL. See DEPLOYMENT.md (Step 2: configure)." >&2
    exit 1
fi

exec docker-entrypoint.sh "$@"
