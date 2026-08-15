"""Hermeticity cases — run ONLY as a subprocess from
`tests/test_conftest_env_hermeticity.py`.

The question is what `src.config.settings` looks like when the *host* has
deployment variables exported. It can only be asked in a fresh interpreter:
`src.config` is imported once by `tests/conftest.py` and the singleton is built
there. The spawner exports hostile values (`MCP_HOSTNAME=evil.example`, and
friends) and this module asserts the singleton ignored every one of them.

The leading underscore keeps it out of normal collection (`pytest.ini` only
collects `test_*.py`).
"""
import os

from src.config import settings


def test_hostile_env_did_not_reach_the_singleton():
    assert os.environ.get("MCP_HOSTNAME") == "evil.example", (
        "the spawner must export the hostile values for this case to mean anything"
    )
    assert settings.mcp_hostname is None
    assert settings.base_url == "http://localhost:8000"
    assert settings.allowed_origins == ["http://localhost:8000"]
    assert settings.allowed_hosts == ["localhost"]


def test_other_wiring_settings_fall_back_to_defaults():
    assert settings.vault_path == "/tmp/test-vault"
    assert settings.database_url == "postgresql+asyncpg://test:test@localhost/test"
    assert settings.embedding_provider == "ollama"
    assert settings.multi_user_mode is False
    assert settings.mcp_sandbox_mode is False
    assert settings.fts_configs == ["english"]


def test_the_host_environment_is_restored_after_the_import():
    """The scrub is scoped to the import; it must not leak into the tests."""
    assert os.environ.get("BASE_URL") == "https://evil.example"
    assert os.environ.get("EMBEDDING_PROVIDER") == "openai"
