#!/bin/bash
# First-start initialisation for the bundled compose stacks' postgres service
# (#324). Mounted at /docker-entrypoint-initdb.d/10-obsidian-mcp.sh, so the
# image runs it once, against an empty data directory, as the bootstrap
# superuser (POSTGRES_USER=postgres) over the temporary server's Unix socket.
#
# It creates the application's role as a NON-superuser that owns the
# application database, and installs pgvector as the superuser. The app's
# first migration runs `CREATE EXTENSION IF NOT EXISTS vector`, which is then a
# no-op needing no privilege. Same shape as the Kubernetes bundle
# (deploy/kubernetes/postgres/configmap-initdb.yaml); see
# openspec/changes/compose-db-roles/design.md D1 and D4.
#
# The passwords were validated by docker/postgres-entrypoint.sh before initdb
# ran, so nothing here can fail on input.
#
# Completion marker: `started` is written to $PGDATA/obsidian-mcp-init.state
# before the first statement and atomically replaced by `complete` after the
# last. A failure in between leaves `started`, and the entrypoint wrapper then
# refuses the half-initialised volume on the next start instead of letting the
# image skip initialisation and serve a cluster without the application role.
#
# The body runs in a subshell: the image *sources* an init script that is not
# executable, and `set -u` must not leak into its own entrypoint.
(
    set -euo pipefail
    : "${OBSIDIAN_DB_PASSWORD:?OBSIDIAN_DB_PASSWORD must be set (.env)}"
    state_file="${PGDATA:?}/obsidian-mcp-init.state"
    printf 'started\n' > "$state_file"

    # psql variables (:'pw') quote the password; it is never shell-interpolated
    # into SQL text.
    psql -X -v ON_ERROR_STOP=1 -v pw="$OBSIDIAN_DB_PASSWORD" \
         --username "$POSTGRES_USER" --dbname postgres <<'SQL'
CREATE ROLE obsidian_mcp LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD :'pw';
CREATE DATABASE obsidian_mcp OWNER obsidian_mcp;
SQL
    psql -X -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname obsidian_mcp <<'SQL'
CREATE EXTENSION IF NOT EXISTS vector;
SQL

    printf 'complete\n' > "$state_file.tmp"
    mv -f "$state_file.tmp" "$state_file"
)
