"""Config surface for the binary-file-transfer change (tasks 1.3 / design D13).

`public_base_url` is the interesting one: `base_url` always has a value because
`_derive_public_urls` falls back to `http://localhost:8000`, so "no public
origin configured" is not observable from `base_url`. The mint tools must be
able to observe it, or they hand the agent a link nobody can open.
"""
import pytest
from pydantic import ValidationError

from src.config import Settings as _Settings


def Settings(**kwargs):  # noqa: N802 - stands in for the real class
    """`Settings` with a non-placeholder SECRET_KEY and no `.env`.

    `SECRET_KEY` has no usable default (the placeholder guard rejects
    `changeme`), so every construction here would otherwise fail for a reason
    unrelated to what is being tested — and would depend on whether an earlier
    test happened to leave one in the environment.
    """
    kwargs.setdefault("secret_key", "test")
    kwargs.setdefault("_env_file", None)
    return _Settings(**kwargs)


@pytest.fixture(autouse=True)
def _no_ambient_origin(monkeypatch):
    """A developer with MCP_HOSTNAME / BASE_URL exported must not flip these."""
    for name in ("MCP_HOSTNAME", "BASE_URL", "mcp_hostname", "base_url"):
        monkeypatch.delenv(name, raising=False)


# ── public_base_url ─────────────────────────────────────────────────────────


def test_public_base_url_is_none_when_origin_is_derived():
    s = Settings()
    # The fallback still fills base_url — that is precisely why the flag exists.
    assert s.base_url == "http://localhost:8000"
    assert s.public_base_url is None


def test_public_base_url_set_from_mcp_hostname():
    s = Settings(mcp_hostname="mcp.example.com")
    assert s.public_base_url == "https://mcp.example.com"


def test_public_base_url_set_from_base_url():
    s = Settings(base_url="https://mcp.example.com")
    assert s.public_base_url == "https://mcp.example.com"


def test_public_base_url_set_from_loopback_base_url():
    """An operator who *chose* loopback gets a usable (if local) origin.

    The distinction is explicitness, not routability: this is a development
    posture, and silently returning None would be indistinguishable from the
    unconfigured case the mint tools must report.
    """
    s = Settings(base_url="http://127.0.0.1:8000")
    assert s.public_base_url == "http://127.0.0.1:8000"


def test_public_base_url_ignores_derived_allowed_origins():
    """Setting only ALLOWED_ORIGINS does not make an origin operator-supplied."""
    s = Settings(allowed_origins=["https://elsewhere.example"])
    assert s.public_base_url is None


def test_public_origin_flag_is_recorded_before_derivation(monkeypatch):
    """Env-supplied values count too, not just constructor kwargs."""
    monkeypatch.setenv("MCP_HOSTNAME", "env.example.com")
    s = Settings()
    assert s.public_base_url == "https://env.example.com"


# ── transfer knobs ──────────────────────────────────────────────────────────


def test_transfer_defaults():
    s = Settings()
    assert s.transfer_token_ttl_seconds == 600
    assert s.transfer_max_upload_seconds == 600
    assert s.transfer_max_concurrent_uploads == 4
    assert s.import_allow_http is False


@pytest.mark.parametrize("value", [59, 3601, 0, -1])
def test_transfer_ttl_outside_the_documented_window_is_refused(value):
    with pytest.raises(ValidationError):
        Settings(transfer_token_ttl_seconds=value)


@pytest.mark.parametrize("value", [60, 600, 3600])
def test_transfer_ttl_accepts_the_documented_window(value):
    assert Settings(transfer_token_ttl_seconds=value).transfer_token_ttl_seconds == value


def test_transfer_concurrency_must_be_positive():
    with pytest.raises(ValidationError):
        Settings(transfer_max_concurrent_uploads=0)


def test_transfer_upload_seconds_must_be_positive():
    with pytest.raises(ValidationError):
        Settings(transfer_max_upload_seconds=0)


def test_import_allow_http_from_env(monkeypatch):
    monkeypatch.setenv("IMPORT_ALLOW_HTTP", "true")
    assert Settings().import_allow_http is True
