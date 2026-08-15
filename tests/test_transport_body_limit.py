"""The MCP transport body limit is derived from the write caps.

The formula assertions run in-process. The end-to-end HTTP assertions cannot:
`src/mcp_server/server.py` builds the `FastMCP` instance at import and
`src/main.py` calls `mcp.streamable_http_app()` at import, so the limit is
fixed before any test can monkeypatch a setting. They therefore live in
`tests/_transport_body_limit_cases.py` and are run here as an import-isolated
subprocess with the settings they need in the environment.
"""
import subprocess
import sys
from pathlib import Path

import pytest

from src.config import MAX_NOTE_BYTES, Settings

ROOT = Path(__file__).resolve().parent.parent
CASES = ROOT / "tests" / "_transport_body_limit_cases.py"
ENVELOPE = 1024 * 1024


# ── (g) the formula itself ──────────────────────────────────────────────────


@pytest.mark.parametrize("write_cap", [1, 65536, 25 * 1024 * 1024, 40 * 1024 * 1024])
def test_derived_limit_matches_the_formula(write_cap):
    settings = Settings(max_file_write_bytes=write_cap, _env_file=None)
    assert settings.mcp_max_request_body_bytes == (
        max(2 * write_cap, 6 * MAX_NOTE_BYTES) + ENVELOPE
    )


def test_default_limit_is_61_mib():
    assert Settings(_env_file=None).mcp_max_request_body_bytes == 61 * 1024 * 1024


def test_small_write_cap_still_admits_a_worst_case_note_write():
    """The MAX_NOTE_BYTES term is what makes the note guarantee cap-independent."""
    settings = Settings(max_file_write_bytes=1024, _env_file=None)
    assert settings.mcp_max_request_body_bytes > 6 * MAX_NOTE_BYTES


def test_raising_the_write_cap_raises_the_transport_limit():
    """Above 3 × MAX_NOTE_BYTES the write cap is the binding term."""
    write_cap = 40 * 1024 * 1024
    settings = Settings(max_file_write_bytes=write_cap, _env_file=None)
    assert settings.mcp_max_request_body_bytes == 2 * write_cap + ENVELOPE


def test_transport_limit_exceeds_every_tool_cap():
    """A supported write must never be rejected only by the transport."""
    settings = Settings(_env_file=None)
    assert settings.mcp_max_request_body_bytes > settings.max_file_write_bytes
    assert settings.mcp_max_request_body_bytes > MAX_NOTE_BYTES


# ── (a)–(f) the transport, end to end, in a subprocess ──────────────────────


def test_transport_body_limit_cases_pass_in_an_isolated_process(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        # Import-time settings the cases module asserts on. A 64 KiB write cap
        # makes the MAX_NOTE_BYTES term the binding one, which is the case the
        # note-write guarantee depends on.
        "MAX_FILE_WRITE_BYTES": "65536",
        "MCP_SANDBOX_MODE": "true",
        "VAULT_PATH": str(vault),
        # Same dummies tests/conftest.py sets. MCP_HOSTNAME must be empty:
        # sandbox mode is refused on a publicly-routed deployment.
        "DATABASE_URL": "postgresql+asyncpg://test:test@localhost/test",
        "SECRET_KEY": "test",
        "MCP_HOSTNAME": "",
    }
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(CASES), "-q", "-p", "no:cacheprovider"],
        # cwd is the tmp dir, not the repo: `Settings` reads a *relative*
        # `.env`, and a developer checkout has one with real values in it.
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, (
        f"transport body-limit cases failed\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
