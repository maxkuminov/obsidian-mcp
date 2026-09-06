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

import src.config as config
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


# ── the ceilings ───────────────────────────────────────────────────────────
#
# "No upper bound" is not the same as "no limit". pydantic accepts an
# arbitrarily long integer literal from the environment, so a limiter setting
# with only a floor booted cleanly on a 401-digit number and then handed one
# principal a bucket no real traffic could exhaust — a control that is
# *configured* and does nothing, which is the failure an operator cannot see
# from any surface. The ceiling turns that into a refused boot.

CEILINGS = {
    "MCP_AUTH_FAILURE_LIMIT": config.LIMITER_COUNT_MAX,
    "MCP_RATE_LIMIT_PER_MINUTE": config.LIMITER_COUNT_MAX,
    "MCP_RATE_LIMIT_BURST": config.LIMITER_COUNT_MAX,
    "MCP_WRITE_RATE_LIMIT_PER_MINUTE": config.LIMITER_COUNT_MAX,
    "MCP_WRITE_RATE_LIMIT_BURST": config.LIMITER_COUNT_MAX,
    "MCP_LIMITER_MAX_TRACKED_PRINCIPALS": config.LIMITER_COUNT_MAX,
    "MCP_AUTH_FAILURE_WINDOW_SECONDS": config.LIMITER_WINDOW_SECONDS_MAX,
    "MCP_REFUSAL_LOG_INTERVAL_SECONDS": config.REFUSAL_INTERVAL_SECONDS_MAX,
    "MCP_AUTH_FAILURE_TABLE_SIZE": config.AUTH_FAILURE_TABLE_SIZE_MAX,
    # `DEFAULT_DAILY_REQUEST_LIMIT`'s ceiling is the column constraint's, and
    # the model validator owns the message, so it is asserted separately below.
}


@pytest.mark.parametrize("setting", sorted(CEILINGS))
def test_the_ceiling_itself_loads(tmp_path, setting):
    loaded = _load(tmp_path, {setting: str(CEILINGS[setting])})
    assert getattr(loaded, setting.lower()) == CEILINGS[setting]


@pytest.mark.parametrize("setting", sorted(CEILINGS))
def test_one_past_the_ceiling_refuses_the_boot(tmp_path, setting):
    with pytest.raises(Exception) as exc:
        _load(tmp_path, {setting: str(CEILINGS[setting] + 1)})
    assert setting.lower() in str(exc.value).lower()


@pytest.mark.parametrize(
    "setting", sorted(CEILINGS) + ["DEFAULT_DAILY_REQUEST_LIMIT"]
)
def test_a_401_digit_integer_is_refused_at_boot(tmp_path, setting):
    """The shape the finding named: an operator (or a broken deployment
    template) writes a number with no plausible upper bound and the boot has to
    refuse it rather than accept a control that cannot fire."""
    absurd = "9" * 401
    assert len(absurd) == 401
    with pytest.raises(Exception) as exc:
        _load(tmp_path, {setting: absurd})
    assert setting.lower() in str(exc.value).lower() or "1..1000000" in str(exc.value)


def test_the_daily_default_keeps_the_column_constraints_domain(tmp_path):
    """Its ceiling is `ck_api_keys_daily_request_limit`'s, stated once where
    the operator-facing message is — a field-level bound would name the field
    and not the domain, and the domain is what an operator has to satisfy."""
    assert _load(
        tmp_path, {"DEFAULT_DAILY_REQUEST_LIMIT": "1000000"}
    ).default_daily_request_limit == 1_000_000
    with pytest.raises(Exception) as exc:
        _load(tmp_path, {"DEFAULT_DAILY_REQUEST_LIMIT": "1000001"})
    assert "1..1000000" in str(exc.value)


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
