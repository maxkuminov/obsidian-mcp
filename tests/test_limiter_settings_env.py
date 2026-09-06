"""One representation for "off", proven through a real environment file (#194).

`.env` has no JSON `null`, so every nullable limiter setting needs a *textual*
spelling for "this control is disabled" — and one shared validator supplies it
for all of them, so no two controls can end up disabled differently. An empty
value, or the literal `null` or `none`, stripped and case-insensitive.

**Through a written file, not through constructor arguments.** `Settings(x=None)`
proves that the model accepts a Python `None`; it proves nothing about the path
an operator actually uses, where every value arrives as a *string* and a
mis-declared field turns `MCP_RATE_LIMIT_PER_MINUTE=` into a validation error
at boot — which is to say into a container that will not start. The file is the
interface; this test uses it.

Zero is not a spelling of "off": a control that refuses every call reads to an
operator as an outage rather than as a setting (#162's reason), so it is
refused everywhere and null is the only disable.
"""
import pytest

from src.config import Settings


NULLABLE = [
    "MCP_AUTH_FAILURE_LIMIT",
    "MCP_RATE_LIMIT_PER_MINUTE",
    "MCP_RATE_LIMIT_BURST",
    "MCP_WRITE_RATE_LIMIT_PER_MINUTE",
    "MCP_WRITE_RATE_LIMIT_BURST",
    "DEFAULT_DAILY_REQUEST_LIMIT",
]

#: The buckets are set-together-or-null-together, so a file that disables one
#: half has to disable the other or the boot is refused (that refusal has its
#: own test below).
PAIRED = {
    "MCP_RATE_LIMIT_PER_MINUTE": "MCP_RATE_LIMIT_BURST",
    "MCP_RATE_LIMIT_BURST": "MCP_RATE_LIMIT_PER_MINUTE",
    "MCP_WRITE_RATE_LIMIT_PER_MINUTE": "MCP_WRITE_RATE_LIMIT_BURST",
    "MCP_WRITE_RATE_LIMIT_BURST": "MCP_WRITE_RATE_LIMIT_PER_MINUTE",
}

BASE = {
    "SECRET_KEY": "an-actual-secret-not-a-placeholder",
    "DATABASE_URL": "postgresql+asyncpg://test:test@localhost/test",
    "VAULT_PATH": "/tmp/test-vault",
}


def _write_env(tmp_path, values: dict, name="mrl-a.env"):
    path = tmp_path / name
    path.write_text(
        "\n".join(f"{key}={value}" for key, value in {**BASE, **values}.items())
        + "\n",
        encoding="utf-8",
    )
    return path


def _load(tmp_path, values: dict) -> Settings:
    return Settings(_env_file=str(_write_env(tmp_path, values)))


@pytest.mark.parametrize("setting", NULLABLE)
@pytest.mark.parametrize("spelling", ["", "null", "none", "NULL", "None", "  null  "])
def test_every_documented_spelling_of_off_resolves_to_null(
    tmp_path, setting, spelling
):
    values = {setting: spelling}
    partner = PAIRED.get(setting)
    if partner:
        values[partner] = spelling
    loaded = _load(tmp_path, values)
    assert getattr(loaded, setting.lower()) is None
    if partner:
        assert getattr(loaded, partner.lower()) is None


def test_the_other_controls_still_apply_when_one_is_disabled(tmp_path):
    loaded = _load(
        tmp_path,
        {"MCP_RATE_LIMIT_PER_MINUTE": "", "MCP_RATE_LIMIT_BURST": ""},
    )
    assert loaded.mcp_rate_limit_per_minute is None
    assert loaded.mcp_write_rate_limit_per_minute == 60
    assert loaded.mcp_auth_failure_limit == 60
    assert loaded.default_daily_request_limit == 5000


def test_an_ordinary_value_still_loads_from_the_file(tmp_path):
    loaded = _load(
        tmp_path,
        {"MCP_RATE_LIMIT_PER_MINUTE": "240", "MCP_RATE_LIMIT_BURST": "60"},
    )
    assert loaded.mcp_rate_limit_per_minute == 240
    assert loaded.mcp_rate_limit_burst == 60


@pytest.mark.parametrize(
    "setting",
    NULLABLE
    + [
        "MCP_AUTH_FAILURE_WINDOW_SECONDS",
        "MCP_AUTH_FAILURE_TABLE_SIZE",
        "MCP_LIMITER_MAX_TRACKED_PRINCIPALS",
        "MCP_REFUSAL_LOG_INTERVAL_SECONDS",
    ],
)
def test_zero_is_refused_from_the_file(tmp_path, setting):
    with pytest.raises(Exception) as exc:
        _load(tmp_path, {setting: "0"})
    assert setting.lower() in str(exc.value).lower()


def test_a_half_configured_bucket_refuses_the_boot_with_both_names(tmp_path):
    """The error names *both* settings, because the one that is missing is the
    one the operator has to write."""
    with pytest.raises(Exception) as exc:
        _load(tmp_path, {"MCP_RATE_LIMIT_PER_MINUTE": "null"})
    message = str(exc.value)
    assert "MCP_RATE_LIMIT_PER_MINUTE" in message
    assert "MCP_RATE_LIMIT_BURST" in message

    with pytest.raises(Exception) as exc:
        _load(tmp_path, {"MCP_WRITE_RATE_LIMIT_BURST": ""})
    message = str(exc.value)
    assert "MCP_WRITE_RATE_LIMIT_PER_MINUTE" in message
    assert "MCP_WRITE_RATE_LIMIT_BURST" in message


def test_an_out_of_domain_default_daily_limit_refuses_the_boot(tmp_path):
    with pytest.raises(Exception) as exc:
        _load(tmp_path, {"DEFAULT_DAILY_REQUEST_LIMIT": "1000001"})
    assert "1..1000000" in str(exc.value)


def test_a_non_numeric_value_is_refused_rather_than_read_as_off(tmp_path):
    """`off`, `false` and `no` are *not* spellings of null. Accepting them
    would make the vocabulary open-ended, which is how two controls end up
    disabled differently — and a typo would silently disable a limiter."""
    for value in ("off", "false", "disabled", "-1"):
        with pytest.raises(Exception):
            _load(tmp_path, {"MCP_AUTH_FAILURE_LIMIT": value})
