"""Regression test for issue #2.

The SECRET_KEY weak-default guard previously only rejected the exact
lowercase literal "changeme", letting the actual shipped .env.example
placeholder "CHANGE_ME" boot with a public, predictable signing secret.

These tests run fully offline: they construct `Settings` directly with
`_env_file=None` and an explicit `secret_key`, so no .env, DB, network, or
embedding provider is touched.
"""
import pytest
from pydantic import ValidationError

from src.config import Settings


@pytest.mark.parametrize(
    "placeholder",
    [
        "CHANGE_ME",  # the value actually shipped in .env.example
        "changeme",
        "Changeme",
        "change_me",
        "change-me",
        "  CHANGE_ME  ",  # surrounding whitespace must not bypass the guard
        "",
    ],
)
def test_placeholder_secret_key_rejected(placeholder):
    with pytest.raises(ValidationError) as exc:
        Settings(secret_key=placeholder, _env_file=None)
    assert "SECRET_KEY" in str(exc.value)


def test_real_secret_key_accepted():
    s = Settings(
        secret_key="9f8c1d2e3a4b5c6d7e8f90112233445566778899aabbccddeeff001122334455",
        _env_file=None,
    )
    assert s.secret_key.startswith("9f8c")
