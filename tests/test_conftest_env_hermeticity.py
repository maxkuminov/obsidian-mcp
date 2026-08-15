"""`tests/conftest.py` builds the settings singleton from a controlled env.

`os.environ.setdefault` left a hole: a developer (or CI) with `BASE_URL`,
`MCP_HOSTNAME`, `EMBEDDING_PROVIDER`, … exported still fed those into the
singleton, because environment variables outrank the dotenv source that the
conftest already suppresses. What the app's CORS/TrustedHost middleware allowed
— and so what the suite asserted — then depended on the machine.

The end-to-end check has to run in a fresh interpreter (the singleton is built
at conftest import), so it lives in `tests/_hermetic_env_case.py` and is
spawned here with hostile values exported.
"""
import subprocess
import sys
from pathlib import Path

from src.config import Settings

from tests.conftest import CONTROLLED_TEST_ENV, SETTINGS_ENV_KEYS

ROOT = Path(__file__).resolve().parent.parent
CASE = ROOT / "tests" / "_hermetic_env_case.py"

HOSTILE_ENV = {
    "MCP_HOSTNAME": "evil.example",
    "BASE_URL": "https://evil.example",
    "ALLOWED_ORIGINS": '["https://evil.example"]',
    "ALLOWED_HOSTS": '["evil.example"]',
    "VAULT_PATH": "/nonexistent/evil-vault",
    "DATABASE_URL": "postgresql+asyncpg://evil:evil@evil.example/evil",
    "EMBEDDING_PROVIDER": "openai",
    "OPENAI_API_KEY": "sk-evil",
    "MULTI_USER_MODE": "true",
    "FTS_CONFIGS": "simple,norwegian",
}


def test_env_key_list_covers_every_settings_field():
    """The static list must not drift from the model it is shadowing."""
    expected = {name.upper() for name in Settings.model_fields}
    assert set(SETTINGS_ENV_KEYS) == expected
    assert set(CONTROLLED_TEST_ENV) <= set(SETTINGS_ENV_KEYS)


def test_exported_deployment_env_does_not_reach_the_singleton(tmp_path):
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        **HOSTILE_ENV,
    }
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(CASE), "-q", "-p", "no:cacheprovider"],
        # cwd is the tmp dir, not the repo: a checkout's `.env` is a second,
        # separately-handled source of the same values.
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, (
        f"hermeticity cases failed\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
