"""Pytest configuration.

Forces an in-process default config so importing `src.config` succeeds even
when `.env` is missing on the test host. Individual tests can monkeypatch
specific settings as needed.
"""
import os
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Every `Settings` field, upper-cased — i.e. every environment variable that can
# change how the app is wired. Kept as a static list because it has to be known
# *before* `src.config` is imported. `tests/test_conftest_env_hermeticity.py`
# asserts it still matches `Settings.model_fields`, so adding a setting without
# adding it here is a test failure rather than a silent hole.
SETTINGS_ENV_KEYS = (
    "DATABASE_URL",
    "OLLAMA_URL",
    "OLLAMA_KEEP_ALIVE",
    "VAULT_PATH",
    "SECRET_KEY",
    "INDEX_INTERVAL_SECONDS",
    "EMBEDDING_MODEL",
    "EMBEDDING_DIMENSIONS",
    "CHUNK_SIZE",
    "CHUNK_OVERLAP",
    "EMBEDDING_EXCLUDE_PATTERNS",
    "MCP_HOSTNAME",
    "BASE_URL",
    "ALLOWED_ORIGINS",
    "ALLOWED_HOSTS",
    "FTS_CONFIGS",
    "EMBEDDING_PROVIDER",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_EMBEDDING_MODEL",
    "MAX_FILE_READ_BYTES",
    "MAX_FILE_WRITE_BYTES",
    "MAX_READ_RESPONSE_CHARS",
    "MULTI_USER_MODE",
    "SESSION_MAX_AGE",
    "SESSION_COOKIE_NAME",
    "MCP_SANDBOX_MODE",
)

# The values the singleton is built from. Everything else in SETTINGS_ENV_KEYS
# is *removed* for the duration of the import so the model defaults apply.
CONTROLLED_TEST_ENV = {
    "DATABASE_URL": "postgresql+asyncpg://test:test@localhost/test",
    "SECRET_KEY": "test",
    "VAULT_PATH": "/tmp/test-vault",
}

# Escape hatch for the import-isolated subprocess suites
# (`tests/_transport_body_limit_cases.py`), which deliberately choose
# import-time settings — MAX_FILE_WRITE_BYTES, MCP_SANDBOX_MODE, VAULT_PATH —
# and pass them in a *scrubbed* environment of their own construction. Only
# those spawners set it; a developer shell must not.
TRUST_ENV_VAR = "OMCP_TEST_TRUST_ENV"


@contextmanager
def _hermetic_settings_env():
    """Replace every `Settings`-shaped variable with controlled values.

    `os.environ.setdefault` was not enough: a developer with `BASE_URL` or
    `MCP_HOSTNAME` exported (both are real deployment variables, and `.env` is
    routinely sourced) still fed them into the singleton, so what the app's
    CORS/TrustedHost middleware allowed — and therefore what the suite asserted
    — depended on the machine. Env vars outrank dotenv in pydantic-settings, so
    suppressing the `.env` file alone does not close this.
    """
    saved = {k: os.environ.get(k) for k in SETTINGS_ENV_KEYS}
    try:
        for key in SETTINGS_ENV_KEYS:
            os.environ.pop(key, None)
        os.environ.update(CONTROLLED_TEST_ENV)
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


# Build the `src.config.settings` singleton *without* the repo-root `.env` and
# without the host's environment. A developer checkout has a real `.env` (it
# doubles as the compose env file), and its BASE_URL / MCP_HOSTNAME would
# otherwise decide what the app's CORS and TrustedHost middleware allow —
# making test results depend on the machine. Doing it here, before any test
# module is imported, means the first import of `src.config` is always the
# hermetic one; individual test modules no longer have to win the
# collection-order race to get a clean config.
import pydantic_settings  # noqa: E402

_orig_settings_init = pydantic_settings.BaseSettings.__init__


def _init_without_env_file(self, *args, **kwargs):
    kwargs.setdefault("_env_file", None)
    _orig_settings_init(self, *args, **kwargs)


if os.environ.get(TRUST_ENV_VAR) == "1":
    # The spawner owns this process's environment; leave it alone.
    for _key, _value in CONTROLLED_TEST_ENV.items():
        os.environ.setdefault(_key, _value)
    pydantic_settings.BaseSettings.__init__ = _init_without_env_file
    try:
        import src.config  # noqa: F401,E402
    finally:
        pydantic_settings.BaseSettings.__init__ = _orig_settings_init
else:
    with _hermetic_settings_env():
        pydantic_settings.BaseSettings.__init__ = _init_without_env_file
        try:
            import src.config  # noqa: F401,E402
        finally:
            pydantic_settings.BaseSettings.__init__ = _orig_settings_init


@pytest.fixture(autouse=True)
def _reset_provider_cache():
    """Clear the cached provider singleton between tests so changes to
    `settings.embedding_provider` propagate."""
    from src.services import embeddings

    embeddings.get_provider.cache_clear()
    yield
    embeddings.get_provider.cache_clear()
