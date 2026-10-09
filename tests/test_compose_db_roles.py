"""The bundled compose stacks separate the database superuser from the app (#324).

Spec: openspec/changes/compose-db-roles (database-least-privilege), design D9.

Three layers, none needing a database:

* the two compose files parsed as data (`yaml.safe_load`);
* the postgres entrypoint wrapper run under bash with a stub
  `docker-entrypoint.sh` on PATH, over the D3 rule matrix;
* `docker compose config` renders, skipped when `docker` is absent: the `:?`
  guard, the required `postgres.env`, and the sentinel admin password that must
  not reach the application service whatever `.env` holds (design D2).

The real-container cases (fresh volume, half-initialised volume, upgrade
script) live in tests/integration/.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
BUNDLES = ("docker-compose.simple.yml", "docker-compose.proxy.yml")
WRAPPER = ROOT / "docker" / "postgres-entrypoint.sh"
INIT_SCRIPT = ROOT / "docker" / "db-init-compose.sh"
UPGRADE_SCRIPT = ROOT / "docker" / "upgrade-split-db-roles.sql"
APP_SERVICE = "obsidian-mcp"
GUARD = "${OBSIDIAN_DB_PASSWORD:?"


def _load(bundle: str) -> dict:
    return yaml.safe_load((ROOT / bundle).read_text())


def _env(service: dict) -> dict:
    env = service.get("environment") or {}
    if isinstance(env, list):  # pragma: no cover - the bundles use the map form
        env = dict(item.split("=", 1) for item in env)
    return env


def _env_files(service: dict) -> list[str]:
    files = service.get("env_file") or []
    if isinstance(files, str):
        files = [files]
    return [f if isinstance(f, str) else f["path"] for f in files]


# ── The compose files as data ──────────────────────────────────────────────


@pytest.mark.parametrize("bundle", BUNDLES)
def test_postgres_superuser_differs_from_the_app_user(bundle):
    services = _load(bundle)["services"]
    pg_env = _env(services["postgres"])
    url = _env(services[APP_SERVICE])["DATABASE_URL"]
    app_user = urlsplit(url.replace(GUARD, "x", 1)).username
    assert pg_env["POSTGRES_USER"] == "postgres"
    assert app_user == "obsidian_mcp"
    assert pg_env["POSTGRES_USER"] != app_user
    # The init script creates the database with its owner; the image must not.
    assert "POSTGRES_DB" not in pg_env


@pytest.mark.parametrize("bundle", BUNDLES)
def test_runtime_password_is_required_everywhere_it_is_used(bundle):
    services = _load(bundle)["services"]
    assert GUARD in _env(services[APP_SERVICE])["DATABASE_URL"]
    assert _env(services["postgres"])["OBSIDIAN_DB_PASSWORD"].startswith(GUARD)
    text = (ROOT / bundle).read_text()
    for use in re.findall(r"\$\{OBSIDIAN_DB_PASSWORD[^}]*\}", text):
        assert use.startswith(GUARD), use


@pytest.mark.parametrize("bundle", BUNDLES)
def test_no_password_fallback_and_no_changeme(bundle):
    text = (ROOT / bundle).read_text()
    assert not re.search(r"\$\{[^}]*PASSWORD[^}]*:-", text)
    assert "changeme" not in text.lower()


@pytest.mark.parametrize("bundle", BUNDLES)
def test_only_postgres_loads_the_admin_file(bundle):
    services = _load(bundle)["services"]
    assert "postgres.env" in _env_files(services["postgres"])
    for name, service in services.items():
        if name == "postgres":
            continue
        assert "postgres.env" not in _env_files(service), name
    # Blanked explicitly: `environment` beats `env_file`, so a superuser
    # password left in .env by the pre-#324 docs is overridden (design D2).
    assert _env(services[APP_SERVICE])["POSTGRES_PASSWORD"] == ""
    assert _env_files(services[APP_SERVICE]) == [".env"]


@pytest.mark.parametrize("bundle", BUNDLES)
def test_entrypoint_wrapper_and_init_mount(bundle):
    pg = _load(bundle)["services"]["postgres"]
    volumes = pg["volumes"]
    mounted = {}
    for v in volumes:
        parts = v.split(":")
        if parts[0].startswith("./"):
            mounted[parts[1]] = parts[0]
    target = pg["entrypoint"][-1]
    assert pg["entrypoint"][0] == "bash"
    assert mounted[target] == "./docker/postgres-entrypoint.sh"
    assert mounted["/docker-entrypoint-initdb.d/10-obsidian-mcp.sh"] == "./docker/db-init-compose.sh"
    assert pg["command"] == ["postgres"]
    assert WRAPPER.is_file() and INIT_SCRIPT.is_file()
    assert not (ROOT / "docker" / "db-init-simple.sql").exists()


@pytest.mark.parametrize("bundle", BUNDLES)
def test_healthcheck_probes_tcp_not_the_socket(bundle):
    test = _load(bundle)["services"]["postgres"]["healthcheck"]["test"]
    command = test[-1] if isinstance(test, list) else test
    assert "pg_isready" in command
    assert "-h 127.0.0.1" in command


def test_the_two_postgres_services_are_identical():
    a, b = (_load(bundle)["services"]["postgres"] for bundle in BUNDLES)
    assert a == b


def test_nothing_runs_the_upgrade_script_automatically():
    assert UPGRADE_SCRIPT.is_file()
    for path in (*(ROOT / b for b in BUNDLES), WRAPPER, INIT_SCRIPT):
        assert "upgrade-split-db-roles" not in path.read_text(), path


def test_init_script_creates_a_non_superuser_owner():
    text = INIT_SCRIPT.read_text()
    assert re.search(
        r"CREATE ROLE obsidian_mcp LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
        r"NOREPLICATION NOBYPASSRLS PASSWORD :'pw';",
        text,
    )
    assert "CREATE DATABASE obsidian_mcp OWNER obsidian_mcp;" in text
    assert "CREATE EXTENSION IF NOT EXISTS vector;" in text
    # The password reaches SQL only through psql's variable quoting.
    assert '-v pw="$OBSIDIAN_DB_PASSWORD"' in text
    assert "<<'SQL'" in text


def test_admin_file_is_gitignored_and_its_example_tracked():
    ignored = (ROOT / ".gitignore").read_text().splitlines()
    assert "postgres.env" in ignored
    example = (ROOT / "postgres.env.example").read_text()
    assert re.search(r"^POSTGRES_PASSWORD=$", example, re.M)


# ── The entrypoint wrapper (design D3) ─────────────────────────────────────

GOOD_ADMIN = "a" * 16 + "0123456789abcdef"  # 32 chars
GOOD_APP = "b" * 16 + "fedcba9876543210"


def _run_wrapper(tmp_path: Path, env: dict, *, pgdata_files: dict | None = None):
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir(exist_ok=True)
    record = tmp_path / "stub-args"
    stub = stub_dir / "docker-entrypoint.sh"
    stub.write_text(f'#!/bin/bash\nprintf "%s\\n" "$@" > "{record}"\n')
    stub.chmod(0o755)
    pgdata = tmp_path / "pgdata"
    pgdata.mkdir(exist_ok=True)
    for name, content in (pgdata_files or {}).items():
        (pgdata / name).write_text(content)
    full_env = {
        "PATH": f"{stub_dir}:{os.environ['PATH']}",
        "PGDATA": str(pgdata),
        **env,
    }
    result = subprocess.run(
        ["bash", str(WRAPPER), "postgres"],
        env=full_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    invoked = record.read_text().splitlines() if record.exists() else None
    return result, invoked


@pytest.mark.parametrize(
    "env,variable,secret",
    [
        ({"POSTGRES_PASSWORD": "", "OBSIDIAN_DB_PASSWORD": GOOD_APP}, "POSTGRES_PASSWORD", None),
        ({"OBSIDIAN_DB_PASSWORD": GOOD_APP}, "POSTGRES_PASSWORD", None),
        ({"POSTGRES_PASSWORD": GOOD_ADMIN, "OBSIDIAN_DB_PASSWORD": "c" * 23}, "OBSIDIAN_DB_PASSWORD", "c" * 23),
        (
            {"POSTGRES_PASSWORD": "   CHANGE_ME" + " " * 14, "OBSIDIAN_DB_PASSWORD": GOOD_APP},
            "POSTGRES_PASSWORD",
            "CHANGE_ME",
        ),
        (
            {"POSTGRES_PASSWORD": GOOD_ADMIN, "OBSIDIAN_DB_PASSWORD": "d" * 20 + "@xyz1234"},
            "OBSIDIAN_DB_PASSWORD",
            "d" * 20 + "@xyz1234",
        ),
        ({"POSTGRES_PASSWORD": GOOD_ADMIN, "OBSIDIAN_DB_PASSWORD": GOOD_ADMIN}, "POSTGRES_PASSWORD", GOOD_ADMIN),
        ({"POSTGRES_PASSWORD": GOOD_ADMIN}, "OBSIDIAN_DB_PASSWORD", None),
    ],
    ids=[
        "admin-empty",
        "admin-unset",
        "app-23-chars",
        "admin-padded-CHANGE_ME",
        "app-with-at-sign",
        "both-equal",
        "app-unset",
    ],
)
def test_wrapper_refuses_weak_values_before_initdb(tmp_path, env, variable, secret):
    result, invoked = _run_wrapper(tmp_path, env)
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert invoked is None, "the image entrypoint must not run"
    assert variable in output
    if secret:
        assert secret not in output
        assert secret.strip() not in output


def test_wrapper_passes_valid_values_through_unchanged(tmp_path):
    result, invoked = _run_wrapper(
        tmp_path, {"POSTGRES_PASSWORD": GOOD_ADMIN, "OBSIDIAN_DB_PASSWORD": GOOD_APP}
    )
    assert result.returncode == 0, result.stderr
    assert invoked == ["postgres"]
    assert GOOD_ADMIN not in result.stdout + result.stderr


def test_wrapper_refuses_a_half_initialised_volume(tmp_path):
    result, invoked = _run_wrapper(
        tmp_path,
        {"POSTGRES_PASSWORD": GOOD_ADMIN, "OBSIDIAN_DB_PASSWORD": GOOD_APP},
        pgdata_files={"PG_VERSION": "16\n", "obsidian-mcp-init.state": "started\n"},
    )
    assert result.returncode != 0
    assert invoked is None
    assert "half-initialised" in result.stderr
    assert "down -v" in result.stderr


@pytest.mark.parametrize(
    "files",
    [
        {"PG_VERSION": "16\n"},  # a pre-#324 volume: no marker at all
        {"PG_VERSION": "16\n", "obsidian-mcp-init.state": "complete\n"},
        {},  # an empty volume: initdb has not run yet
    ],
    ids=["legacy-volume", "completed", "empty"],
)
def test_wrapper_allows_complete_or_unmarked_volumes(tmp_path, files):
    result, invoked = _run_wrapper(
        tmp_path,
        {"POSTGRES_PASSWORD": GOOD_ADMIN, "OBSIDIAN_DB_PASSWORD": GOOD_APP},
        pgdata_files=files,
    )
    assert result.returncode == 0, result.stderr
    assert invoked == ["postgres"]


# ── `docker compose config` renders (skipped without docker) ───────────────

requires_docker_compose = pytest.mark.skipif(
    shutil.which("docker") is None
    or subprocess.run(
        ["docker", "compose", "version"], capture_output=True
    ).returncode
    != 0,
    reason="docker compose not available",
)

SENTINEL_ADMIN = "SENTINEL0admin0password0from0dotenv0" + "x" * 8
POSTGRES_ENV_ADMIN = "postgres0env0admin0password0" + "y" * 8


def _compose_config(tmp_path: Path, bundle: str, *, dotenv: str, postgres_env: str | None):
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    shutil.copy(ROOT / bundle, project / "compose.yml")
    (project / ".env").write_text(dotenv)
    if postgres_env is not None:
        (project / "postgres.env").write_text(postgres_env)
    # A scrubbed environment: compose interpolates from the shell first, so a
    # developer's exported OBSIDIAN_DB_PASSWORD would mask the guard.
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONFIG")}
    return subprocess.run(
        [
            "docker", "compose", "-p", "omcp-compose-config-test",
            "--project-directory", str(project), "-f", str(project / "compose.yml"),
            "config", "--format", "yaml",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


@requires_docker_compose
@pytest.mark.parametrize("bundle", BUNDLES)
def test_compose_config_fails_without_runtime_password(tmp_path, bundle):
    result = _compose_config(
        tmp_path, bundle, dotenv="SECRET_KEY=x\n", postgres_env=f"POSTGRES_PASSWORD={POSTGRES_ENV_ADMIN}\n"
    )
    assert result.returncode != 0
    assert "OBSIDIAN_DB_PASSWORD" in result.stderr


@requires_docker_compose
@pytest.mark.parametrize("bundle", BUNDLES)
def test_compose_config_fails_without_postgres_env(tmp_path, bundle):
    result = _compose_config(
        tmp_path, bundle, dotenv=f"OBSIDIAN_DB_PASSWORD={GOOD_APP}\n", postgres_env=None
    )
    assert result.returncode != 0
    assert "postgres.env" in result.stderr


@requires_docker_compose
@pytest.mark.parametrize("bundle", BUNDLES)
def test_stale_admin_password_in_dotenv_does_not_reach_the_app(tmp_path, bundle):
    dotenv = (
        f"OBSIDIAN_DB_PASSWORD={GOOD_APP}\n"
        f"POSTGRES_PASSWORD={SENTINEL_ADMIN}\n"
        "DATABASE_URL=postgresql+asyncpg://obsidian_mcp:stale@postgres:5432/obsidian_mcp\n"
    )
    result = _compose_config(
        tmp_path, bundle, dotenv=dotenv, postgres_env=f"POSTGRES_PASSWORD={POSTGRES_ENV_ADMIN}\n"
    )
    assert result.returncode == 0, result.stderr
    rendered = yaml.safe_load(result.stdout)["services"]
    app_env = rendered[APP_SERVICE]["environment"]
    assert SENTINEL_ADMIN not in yaml.safe_dump(rendered[APP_SERVICE])
    assert POSTGRES_ENV_ADMIN not in yaml.safe_dump(rendered[APP_SERVICE])
    assert app_env["POSTGRES_PASSWORD"] == ""
    assert app_env["DATABASE_URL"] == (
        f"postgresql+asyncpg://obsidian_mcp:{GOOD_APP}@postgres:5432/obsidian_mcp"
    )
    pg_env = rendered["postgres"]["environment"]
    assert pg_env["POSTGRES_PASSWORD"] == POSTGRES_ENV_ADMIN
    assert pg_env["OBSIDIAN_DB_PASSWORD"] == GOOD_APP
    assert pg_env["POSTGRES_USER"] == "postgres"
