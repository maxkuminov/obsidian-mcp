"""#190 — the configuration applies in a **real process**, not just in a unit.

The audit note asks for exactly this, and for a good reason: the defect was an
import-order race between the MCP SDK's `configure_logging()` and this app's
own. Any in-process assertion runs after pytest has already touched the root
logger, so it can pass while the shipped process still logs Rich-formatted local
time. This spawns a process that imports `src.main` the way uvicorn does and
looks at what actually came out of stderr.

No database is needed: importing the app builds no connection.
"""
import datetime
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

PROBE = """
import json, logging, sys

import src.main  # noqa: F401  - the import under test

from src.services import error_log

root = logging.getLogger()
ring = error_log.installed_handler()
others = [h for h in root.handlers if h is not ring]

logging.getLogger("probe").warning(
    "auth_failure",
    extra={"reason": "invalid_key", "user_id": 7, "vault_path": "/secret/note.md"},
)

print(
    json.dumps(
        {
            "non_ring_handlers": len(others),
            "streams": [getattr(h, "stream", None) is sys.stderr for h in others],
            "root_level": logging.getLevelName(root.level),
        }
    )
)
"""


PROBE_STDIO = """
import logging, sys

import src.mcp_stdio  # noqa: F401  - the stdio entry point under test

logging.getLogger("probe").warning("auth_failure", extra={"reason": "invalid_key"})
logging.getLogger("probe").info("chatty startup narration")
print("root_level=" + logging.getLevelName(logging.getLogger().level))
"""


def _run(tmp_path, probe=None):
    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "PYTHONPATH": str(ROOT),
        # The same dummies `tests/conftest.py` uses. `MCP_HOSTNAME` empty so no
        # public-routing guard fires; sandbox mode so the app does not try to
        # reach a database or an embedding provider at import.
        "DATABASE_URL": "postgresql+asyncpg://test:test@localhost/test",
        "SECRET_KEY": "test",
        "VAULT_PATH": str(vault),
        "MCP_HOSTNAME": "",
        "MCP_SANDBOX_MODE": "true",
    }
    return subprocess.run(
        [sys.executable, "-c", probe or PROBE],
        # cwd is the tmp dir, not the repo: `Settings` reads a *relative* `.env`
        # and a developer checkout has one with real values in it.
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


def _record_line(stderr: str) -> dict:
    for line in stderr.splitlines():
        if '"msg": "auth_failure"' in line or '"auth_failure"' in line:
            return json.loads(line)
    raise AssertionError(f"no structured record on stderr:\n{stderr}")


def test_the_shipped_process_logs_one_json_line_per_record(tmp_path):
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr

    summary = json.loads(result.stdout.strip().splitlines()[-1])
    # Exactly one handler besides the ring buffer's — the SDK's `RichHandler`
    # was removed, not merely joined.
    assert summary["non_ring_handlers"] == 1
    assert summary["streams"] == [True]
    assert summary["root_level"] == "INFO"

    payload = _record_line(result.stderr)
    assert payload["msg"] == "auth_failure"
    assert payload["level"] == "WARNING"
    # The structured fields the old configuration dropped on the floor.
    assert payload["reason"] == "invalid_key"
    assert payload["user_id"] == 7
    # ...and only those: the allow-list still holds in the real process.
    assert "vault_path" not in payload

    assert payload["ts"].endswith("Z")
    parsed = datetime.datetime.fromisoformat(payload["ts"].replace("Z", "+00:00"))
    assert parsed.utcoffset() == datetime.timedelta(0)


def test_nothing_the_probe_logged_reached_stdout(tmp_path):
    """The stdio entry point depends on this invariant, and the app shares the
    handler: stdout is the MCP protocol channel."""
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "auth_failure" not in result.stdout


def test_the_stdio_entry_point_writes_records_to_stderr_only(tmp_path):
    """Stdout is the MCP protocol channel there, so one log line on it is a
    protocol error — and the SDK's handler, which this displaces, is the one
    that would have written it."""
    result = _run(tmp_path, probe=PROBE_STDIO)
    assert result.returncode == 0, result.stderr

    assert "root_level=WARNING" in result.stdout
    assert "auth_failure" not in result.stdout
    payload = _record_line(result.stderr)
    assert payload["msg"] == "auth_failure"
    assert payload["reason"] == "invalid_key"
    # WARNING, not the configured level: a registry sandbox wants the tool list,
    # not the server's narration.
    assert "chatty startup narration" not in result.stderr
