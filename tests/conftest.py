"""Pytest configuration.

Forces an in-process default config so importing `src.config` succeeds even
when `.env` is missing on the test host. Individual tests can monkeypatch
specific settings as needed.
"""
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Provide minimal env defaults so `Settings()` instantiation doesn't pick up
# stray env from the host. Tests that need a particular value set it via
# monkeypatch + a `Settings` reload helper.
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")

# Build the `src.config.settings` singleton *without* the repo-root `.env`.
# A developer checkout has a real one (it doubles as the compose env file), and
# its BASE_URL / MCP_HOSTNAME would otherwise decide what the app's CORS and
# TrustedHost middleware allow — making test results depend on the machine.
# Doing it here, before any test module is imported, means the first import of
# `src.config` is always the hermetic one; individual test modules no longer
# have to win the collection-order race to get a clean config.
import pydantic_settings  # noqa: E402

_orig_settings_init = pydantic_settings.BaseSettings.__init__


def _init_without_env_file(self, *args, **kwargs):
    kwargs.setdefault("_env_file", None)
    _orig_settings_init(self, *args, **kwargs)


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
