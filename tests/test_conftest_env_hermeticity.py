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
LOWERCASE_CASE = ROOT / "tests" / "_hermetic_lowercase_env_case.py"

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

# The same attack in lower case, and *only* lower case. pydantic-settings reads
# the environment case-insensitively, so these are just as dangerous as the
# upper-case spellings while looking nothing like them.
LOWERCASE_HOSTILE_ENV = {
    "mcp_hostname": "lower.example",
    "base_url": "https://lower.example",
    "allowed_origins": '["https://lower.example"]',
    "vault_path": "/nonexistent/lower-vault",
    "database_url": "postgresql+asyncpg://lower:lower@lower.example/lower",
    "embedding_provider": "openai",
    "openai_api_key": "sk-lower",
    "multi_user_mode": "true",
}


def test_env_key_list_covers_every_settings_field():
    """The static list must not drift from the model it is shadowing."""
    expected = {name.upper() for name in Settings.model_fields}
    assert set(SETTINGS_ENV_KEYS) == expected
    assert set(CONTROLLED_TEST_ENV) <= set(SETTINGS_ENV_KEYS)


def _run_case(case: Path, hostile: dict, tmp_path: Path):
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        **hostile,
    }
    return subprocess.run(
        [sys.executable, "-m", "pytest", str(case), "-q", "-p", "no:cacheprovider"],
        # cwd is the tmp dir, not the repo: a checkout's `.env` is a second,
        # separately-handled source of the same values.
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


def test_exported_deployment_env_does_not_reach_the_singleton(tmp_path):
    result = _run_case(CASE, HOSTILE_ENV, tmp_path)
    assert result.returncode == 0, (
        f"hermeticity cases failed\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )


def test_lowercase_exported_env_does_not_reach_the_singleton(tmp_path):
    """Lower-case spellings are the same attack: pydantic-settings folds case."""
    result = _run_case(LOWERCASE_CASE, LOWERCASE_HOSTILE_ENV, tmp_path)
    assert result.returncode == 0, (
        f"lower-case hermeticity cases failed\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
