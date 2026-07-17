"""Verifies provider config validation at instantiation time."""
import pytest
from pydantic import ValidationError

from src.config import Settings


def test_openai_provider_requires_api_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ValidationError) as exc:
        Settings(
            embedding_provider="openai",
            openai_api_key=None,
            _env_file=None,
        )
    assert "OPENAI_API_KEY" in str(exc.value)


def test_openai_provider_with_empty_string_api_key():
    with pytest.raises(ValidationError):
        Settings(
            embedding_provider="openai",
            openai_api_key="   ",
            _env_file=None,
        )


def test_openai_provider_with_valid_key():
    s = Settings(
        embedding_provider="openai",
        openai_api_key="sk-test",
        _env_file=None,
    )
    assert s.embedding_provider == "openai"


def test_ollama_default_no_key_required():
    s = Settings(_env_file=None)
    assert s.embedding_provider == "ollama"


def test_invalid_provider_value():
    with pytest.raises(ValidationError):
        Settings(embedding_provider="cohere", _env_file=None)


def test_public_base_url_requires_https():
    with pytest.raises(ValidationError) as exc:
        Settings(base_url="http://mcp.example.com", _env_file=None)
    assert "HTTPS" in str(exc.value)


def test_loopback_base_url_may_use_http():
    settings = Settings(base_url="http://127.0.0.1:8000/", _env_file=None)
    assert settings.base_url == "http://127.0.0.1:8000"


def test_explicit_empty_allowed_hosts_still_allows_localhost_healthcheck():
    settings = Settings(allowed_hosts=[], _env_file=None)
    assert settings.allowed_hosts == ["localhost"]


def test_base_url_must_match_public_hostname():
    with pytest.raises(ValidationError):
        Settings(
            mcp_hostname="mcp.example.com",
            base_url="https://other.example.com",
            _env_file=None,
        )
