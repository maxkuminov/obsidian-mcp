"""The bundles' postgres service, started for real by `docker compose` (#324).

Design D1, D3, D4. The bundle file is copied into a scratch project with only
the postgres service's `container_name` made unique, and `docker compose up
postgres` brings it up through the real entrypoint wrapper, init script and
TCP healthcheck on a fresh, project-scoped volume.

* fresh volume: the runtime role exists with every privileged attribute off,
  owns the database, `vector` is installed and owned by `postgres`, the init
  marker reads `complete`, the runtime password authenticates over TCP, the
  upgrade script's self-check reports it "already split", and a restart on the
  initialised volume comes back healthy;
* injected init failure: the init script is made to fail after its marker
  reads `started`; on the automatic restart the wrapper refuses the
  half-initialised volume with the recovery instructions.
"""
from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

import pytest
import yaml

from tests.integration._docker_pg import requires_docker, unique
from tests.integration._harness import ROOT

pytestmark = requires_docker

ADMIN_PW = "compose0admin0password0" + "4" * 10
APP_PW = "compose0runtime0password0" + "5" * 10


class Project:
    def __init__(self, tmp_path: Path, *, break_init: bool = False):
        self.dir = tmp_path / "project"
        (self.dir / "docker").mkdir(parents=True)
        self.name = unique("compose")
        spec = yaml.safe_load((ROOT / "docker-compose.proxy.yml").read_text())
        spec["services"]["postgres"]["container_name"] = f"{self.name}-postgres"
        (self.dir / "compose.yml").write_text(yaml.safe_dump(spec))
        shutil.copy(ROOT / "docker" / "postgres-entrypoint.sh", self.dir / "docker")
        init = (ROOT / "docker" / "db-init-compose.sh").read_text()
        if break_init:
            target = "CREATE EXTENSION IF NOT EXISTS vector;"
            assert target in init
            init = init.replace(target, "CREATE EXTENSION IF NOT EXISTS no_such_extension_324;")
        (self.dir / "docker" / "db-init-compose.sh").write_text(init)
        (self.dir / ".env").write_text(f"OBSIDIAN_DB_PASSWORD={APP_PW}\nSECRET_KEY=x\n")
        (self.dir / "postgres.env").write_text(f"POSTGRES_PASSWORD={ADMIN_PW}\n")

    def compose(self, *args: str, check: bool = True, timeout: int = 180, input: str | None = None):
        result = subprocess.run(
            ["docker", "compose", "-p", self.name, "--project-directory", str(self.dir),
             "-f", str(self.dir / "compose.yml"), *args],
            capture_output=True, text=True, timeout=timeout, input=input,
        )
        if check:
            assert result.returncode == 0, f"compose {args[0]} failed:\n{result.stdout}\n{result.stderr}"
        return result

    def sql(self, statement: str, user: str = "postgres") -> list[str]:
        out = self.compose("exec", "-T", "postgres", "psql", "-X", "-qAt", "-v", "ON_ERROR_STOP=1",
                           "-U", user, "-d", "obsidian_mcp", "-c", statement).stdout
        return [line for line in out.splitlines() if line]

    def logs(self) -> str:
        r = self.compose("logs", "--no-color", "postgres", check=False)
        return r.stdout + r.stderr

    def down(self):
        self.compose("down", "-v", "--remove-orphans", check=False)


def test_fresh_volume_gets_role_separation(tmp_path):
    p = Project(tmp_path)
    try:
        p.compose("up", "-d", "--wait", "--wait-timeout", "150", "postgres", timeout=200)
        assert p.sql(
            "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls, rolcanlogin "
            "FROM pg_roles WHERE rolname = 'obsidian_mcp'"
        ) == ["f|f|f|f|f|t"]
        assert p.sql("SELECT rolname FROM pg_roles WHERE oid = 10") == ["postgres"]
        assert p.sql("SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = 'obsidian_mcp'") == [
            "obsidian_mcp"
        ]
        assert p.sql("SELECT pg_get_userbyid(extowner) FROM pg_extension WHERE extname = 'vector'") == [
            "postgres"
        ]
        marker = p.compose("exec", "-T", "postgres", "cat",
                           "/var/lib/postgresql/data/obsidian-mcp-init.state").stdout
        assert marker.strip() == "complete"
        # The runtime password authenticates over TCP (scram), as the app will.
        who = p.compose("exec", "-T", "-e", f"PGPASSWORD={APP_PW}", "postgres", "psql", "-X", "-qAt",
                        "-h", "127.0.0.1", "-U", "obsidian_mcp", "-d", "obsidian_mcp",
                        "-c", "SELECT current_user, (SELECT rolsuper FROM pg_roles WHERE rolname = current_user)")
        assert who.stdout.strip() == "obsidian_mcp|f"
        # The admin password is the one from postgres.env.
        admin = p.compose("exec", "-T", "-e", f"PGPASSWORD={ADMIN_PW}", "postgres", "psql", "-X", "-qAt",
                          "-h", "127.0.0.1", "-U", "postgres", "-d", "postgres", "-c", "SELECT 1")
        assert admin.stdout.strip() == "1"
        # The upgrade script's full self-check accepts the fresh-install shape
        # as already split (and so would refuse anything less).
        script = (ROOT / "docker" / "upgrade-split-db-roles.sql").read_text()
        upgrade = p.compose(
            "exec", "-T", "postgres", "sh", "-c",
            'psql -X -v ON_ERROR_STOP=1 -U obsidian_mcp -d obsidian_mcp '
            '-v admin_pw="$POSTGRES_PASSWORD" -v app_pw="$OBSIDIAN_DB_PASSWORD"',
            input=script, check=False,
        )
        assert upgrade.returncode == 0, upgrade.stdout + upgrade.stderr
        assert "already split" in upgrade.stdout
        # An initialised volume with a complete marker restarts cleanly.
        p.compose("restart", "postgres")
        p.compose("up", "-d", "--wait", "--wait-timeout", "120", "postgres", timeout=160)
        assert "half-initialised" not in p.logs()
    finally:
        p.down()


def test_half_initialised_volume_is_refused_on_next_start(tmp_path):
    p = Project(tmp_path, break_init=True)
    try:
        p.compose("up", "-d", "postgres")
        deadline = time.monotonic() + 150
        logs = ""
        while time.monotonic() < deadline:
            logs = p.logs()
            if "half-initialised" in logs:
                break
            time.sleep(2)
        assert "no_such_extension_324" in logs, logs  # the injected failure ran
        assert "half-initialised" in logs, logs
        assert "down -v" in logs
        for secret in (ADMIN_PW, APP_PW):
            assert secret not in logs
    finally:
        p.down()
