"""Lower-case hermeticity case — run ONLY as a subprocess from
`tests/test_conftest_env_hermeticity.py`.

pydantic-settings resolves environment variables case-insensitively, so
`base_url=https://lower.example` reaches `Settings` exactly like `BASE_URL`.
The spawner exports the hostile values in lower case *only*; the conftest scrub
has to fold names before matching, or the singleton picks them up.

The leading underscore keeps it out of normal collection (`pytest.ini` only
collects `test_*.py`).
"""
import os

from src.config import settings


def test_lowercase_hostile_env_did_not_reach_the_singleton():
    assert os.environ.get("base_url") == "https://lower.example", (
        "the spawner must export the lower-case values for this case to mean anything"
    )
    assert os.environ.get("BASE_URL") is None, (
        "this case is only meaningful without the upper-case spelling"
    )
    assert settings.base_url == "http://localhost:8000"
    assert settings.mcp_hostname is None
    assert settings.allowed_origins == ["http://localhost:8000"]
    assert settings.embedding_provider == "ollama"
    assert settings.multi_user_mode is False


def test_controlled_values_beat_lowercase_host_entries():
    """The controlled upper-case values are what the singleton was built from."""
    assert os.environ.get("vault_path") == "/nonexistent/lower-vault"
    assert settings.vault_path == "/tmp/test-vault"
    assert settings.database_url == "postgresql+asyncpg://test:test@localhost/test"


def test_the_lowercase_host_entries_are_restored_after_the_import():
    """The scrub restores the exact original entries, casing included."""
    assert os.environ.get("base_url") == "https://lower.example"
    assert os.environ.get("mcp_hostname") == "lower.example"
    assert os.environ.get("multi_user_mode") == "true"
    # ...and it left no upper-case ghosts behind: neither the hostile entries
    # under a new spelling, nor the controlled values it introduced.
    assert os.environ.get("MCP_HOSTNAME") is None
    assert os.environ.get("VAULT_PATH") is None
    assert os.environ.get("DATABASE_URL") is None
