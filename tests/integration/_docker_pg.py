"""Throwaway `pgvector/pgvector:pg16` containers for the #324 role tests.

The compose-db-roles tests need clusters the shared integration server cannot
provide: one initialised in the pre-#324 shape (bootstrap superuser named
`obsidian_mcp`), and fresh volumes started through the bundles' own entrypoint
wrapper and init script. Each test starts its own container (and, where it
needs one, its own named volume) with a unique name and a randomly published
loopback port, so parallel runs and a developer's own containers never collide.

Gated like the rest of tests/integration: a run that has not set
`PGVECTOR_TEST_ADMIN_URL` is not an integration run, and these also need a
working `docker`. Names start with `$OMCP_DOCKER_TEST_PREFIX` (default
`omcp-it`).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid

import pytest

IMAGE = os.environ.get("SCHEMA_TEST_IMAGE", "pgvector/pgvector:pg16")
PREFIX = os.environ.get("OMCP_DOCKER_TEST_PREFIX", "omcp-it")


def _docker_works() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


requires_docker = pytest.mark.skipif(
    not os.environ.get("PGVECTOR_TEST_ADMIN_URL") or not _docker_works(),
    reason="set PGVECTOR_TEST_ADMIN_URL (an integration run) and have a working docker",
)


def unique(name: str) -> str:
    return f"{PREFIX}-{name}-{uuid.uuid4().hex[:8]}"


def docker(*args: str, check: bool = True, input: str | None = None, timeout: int = 120):
    result = subprocess.run(
        ["docker", *args], capture_output=True, text=True, input=input, timeout=timeout
    )
    if check:
        assert result.returncode == 0, f"docker {' '.join(args[:3])} failed:\n{result.stdout}\n{result.stderr}"
    return result


class Container:
    """One `docker run -d` postgres container; `remove()` in a finally."""

    def __init__(self, name: str, run_args: list[str], command: list[str] | None = None):
        self.name = name
        docker("run", "-d", "--name", name, "-p", "127.0.0.1::5432", *run_args, IMAGE, *(command or []))

    @property
    def port(self) -> int:
        out = docker("port", self.name, "5432/tcp").stdout.strip().splitlines()[0]
        return int(out.rsplit(":", 1)[1])

    def url(self, user: str, password: str, db: str = "obsidian_mcp") -> str:
        return f"postgresql+asyncpg://{user}:{password}@127.0.0.1:{self.port}/{db}"

    def running(self) -> bool:
        out = docker("inspect", "-f", "{{.State.Running}}", self.name, check=False).stdout.strip()
        return out == "true"

    def logs(self) -> str:
        r = docker("logs", self.name, check=False)
        return r.stdout + r.stderr

    def wait_ready(self, user: str, timeout: float = 90) -> None:
        """Ready over TCP: the image's init-phase server listens on the socket only."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.running():
                raise AssertionError(f"{self.name} exited:\n{self.logs()}")
            r = docker("exec", self.name, "pg_isready", "-h", "127.0.0.1", "-U", user, "-q", check=False)
            if r.returncode == 0:
                return
            time.sleep(1)
        raise AssertionError(f"{self.name} never became ready:\n{self.logs()}")

    def psql(self, sql: str, *, user: str, db: str = "obsidian_mcp", extra: list[str] | None = None,
             env: dict[str, str] | None = None, check: bool = True):
        """psql over the local socket, SQL on stdin (unaligned, tuples only)."""
        env_args = [a for k, v in (env or {}).items() for a in ("-e", f"{k}={v}")]
        return docker(
            "exec", "-i", *env_args, self.name,
            "psql", "-X", "-qAt", "-v", "ON_ERROR_STOP=1", "-U", user, "-d", db, *(extra or []),
            input=sql, check=check,
        )

    def remove(self) -> None:
        docker("rm", "-f", "-v", self.name, check=False)
