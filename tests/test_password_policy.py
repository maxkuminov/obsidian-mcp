"""The password policy and the session/consent settings it ships beside (#197).

Three things are pinned here, all of them foundations the rest of
`panel-sessions-and-consent` builds on:

1. **`validate_new_password` is the one place the policy lives.** Four handlers
   set a password — the self-service change, bootstrap registration, the
   administrator reset and administrative user creation — and each used to
   carry its own minimum (or none) and to pass user input straight into
   `hash_password`, which **raises** on an embedded NUL. So the validator's
   job is both the rule and the four latent 500s.
2. **`hash_password` / `verify_password` are untouched.** The 72-byte
   truncation and the NUL rejection reproduce passlib's historical semantics
   and every stored hash depends on them; a case here proves a >72-byte
   password still hashes and still verifies, so a future "fix" of the
   truncation fails loudly rather than locking a user out silently.
3. **The new settings' bounds are enforced at construction**, not assumed by
   the code that reads them, and the redirect-host allow-list refuses a
   pattern rather than accepting an entry that would match nothing.
"""
import pathlib

import pytest
from pydantic import ValidationError

from src.auth.passwords import (
    MIN_PASSWORD_LENGTH,
    hash_password,
    validate_new_password,
    verify_password,
)
from src.config import Settings


# --------------------------------------------------------------------------
# the policy
# --------------------------------------------------------------------------


def test_the_minimum_is_one_constant_and_it_is_twelve():
    """One constant, so an administrator cannot set a password its owner is
    then forbidden from setting again."""
    assert MIN_PASSWORD_LENGTH == 12


def test_one_character_below_the_minimum_is_refused():
    message = validate_new_password("a" * (MIN_PASSWORD_LENGTH - 1))
    assert message is not None
    assert str(MIN_PASSWORD_LENGTH) in message


def test_exactly_the_minimum_is_accepted():
    assert validate_new_password("a" * MIN_PASSWORD_LENGTH) is None


def test_a_confirmation_mismatch_is_refused():
    message = validate_new_password("correct-horse-battery", "correct-horse-batteryy")
    assert message is not None
    assert "match" in message.lower()


def test_a_matching_confirmation_is_accepted():
    assert validate_new_password("correct-horse-battery", "correct-horse-battery") is None


def test_no_confirmation_means_only_the_intrinsic_rules_apply():
    """Callers with no confirmation field pass nothing rather than passing the
    new password twice, so the mismatch branch is not accidentally load-bearing
    for them."""
    assert validate_new_password("correct-horse-battery") is None


def test_a_nul_byte_returns_a_message_instead_of_raising():
    """The whole reason the NUL check is in the validator rather than left to
    the hasher: `hash_password` raises `ValueError` on an embedded NUL —
    passlib's policy, preserved deliberately — and four handlers pass form
    input straight into it, so without this a NUL in a password field is an
    unhandled exception and a 500."""
    message = validate_new_password("correct-horse-battery\x00tail")
    assert message is not None
    assert "NUL" in message

    # And the underlying rule the validator is protecting callers from is
    # still exactly as it was.
    with pytest.raises(ValueError):
        hash_password("correct-horse-battery\x00tail")


def test_a_nul_is_refused_even_when_it_is_the_whole_difference():
    """`"secret\\0anything"` and `"secret"` hashed identically under the C
    bcrypt of the `$2b$` era — a password whose entropy silently stops at its
    first NUL. The validator refuses before that can matter."""
    assert validate_new_password("a" * 20 + "\x00") is not None


def test_a_short_password_that_also_mismatches_is_still_refused():
    assert validate_new_password("short", "different") is not None


def test_no_refusal_message_echoes_the_submitted_password():
    """A refusal is rendered back into a page, and on the administrator paths
    it may be read by somebody other than the person who typed it."""
    secret = "hunter2-is-my-password"
    for new, confirm in (
        ("shrt", None),
        (secret, secret + "x"),
        (secret + "\x00", None),
    ):
        message = validate_new_password(new, confirm)
        assert message is not None
        assert new not in message
        assert secret not in message


# --------------------------------------------------------------------------
# the hashing semantics the policy must not disturb
# --------------------------------------------------------------------------


def test_a_password_longer_than_72_bytes_is_accepted_and_still_verifies():
    """bcrypt only ever consumed the first 72 bytes; passlib truncated
    silently and this module reproduces that. A stored hash for a 100-byte
    password was computed over its first 72 bytes only and would become
    unverifiable — locking that user out — if the truncation were ever
    "fixed". The policy is a *character* minimum and adds no maximum."""
    password = "p" * 100
    assert len(password.encode()) > 72
    assert validate_new_password(password, password) is None

    hashed = hash_password(password)
    assert hashed.startswith("$2b$12$")
    assert verify_password(password, hashed) is True


def test_two_passwords_sharing_their_first_72_bytes_are_the_same_password():
    """The documented consequence of the truncation, pinned so a change to it
    is visible here rather than as a login that stops working."""
    hashed = hash_password("q" * 72 + "tail-one")
    assert verify_password("q" * 72 + "tail-two", hashed) is True


def test_a_multibyte_password_at_the_boundary_round_trips():
    """Truncation is on bytes, not characters, so it can split a multi-byte
    character and yield invalid UTF-8 — which is fine, because bcrypt hashes
    bytes and nothing decodes them back."""
    password = "é" * 40  # 80 bytes
    assert len(password) >= MIN_PASSWORD_LENGTH
    assert validate_new_password(password) is None
    assert verify_password(password, hash_password(password)) is True


# --------------------------------------------------------------------------
# the settings that ship with the session registry
# --------------------------------------------------------------------------

_SECRET = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"


def _settings(**kwargs):
    return Settings(secret_key=_SECRET, _env_file=None, **kwargs)


def test_the_session_settings_default_as_documented():
    settings = _settings()
    assert settings.session_touch_interval_seconds == 60
    assert settings.session_purge_retain_days == 7


def test_a_zero_touch_interval_fails_settings_construction():
    """A zero or negative interval turns a throttled hint into an UPDATE plus a
    commit on every panel request. The bound is enforced at construction so a
    bad value stops the container rather than degrading it silently."""
    with pytest.raises(ValidationError):
        _settings(session_touch_interval_seconds=0)
    with pytest.raises(ValidationError):
        _settings(session_touch_interval_seconds=-1)


def test_a_zero_retention_window_fails_settings_construction():
    """An administrative reset revokes every unrevoked row of a user,
    already-expired ones included; with a zero window such a row is deleted on
    the next indexer tick, erasing the record of a revocation minutes after an
    operator performed it. That is the #64 blank space the retention exists to
    prevent."""
    with pytest.raises(ValidationError):
        _settings(session_purge_retain_days=0)
    with pytest.raises(ValidationError):
        _settings(session_purge_retain_days=-1)


def test_the_known_redirect_hosts_default_to_the_two_connector_hosts():
    assert _settings().oauth_known_redirect_hosts == ["claude.ai", "chatgpt.com"]


def test_a_comma_separated_list_parses_to_two_hosts_with_whitespace_stripped():
    """An operator writing `claude.ai, chatgpt.com` means two hosts. Without
    the strip the second entry would be `" chatgpt.com"`, which — since
    matching is exact equality — equals nothing at all, and every consent
    screen for that client would warn while the operator believed it was
    allow-listed."""
    settings = _settings(oauth_known_redirect_hosts="claude.ai, chatgpt.com")
    assert settings.oauth_known_redirect_hosts == ["claude.ai", "chatgpt.com"]


def test_a_json_list_parses_and_is_lower_cased_and_deduped():
    settings = _settings(
        oauth_known_redirect_hosts='["Claude.AI", "claude.ai", "ChatGPT.com"]'
    )
    assert settings.oauth_known_redirect_hosts == ["claude.ai", "chatgpt.com"]


def test_json_that_does_not_parse_fails_loudly_rather_than_csv_splitting():
    with pytest.raises(ValidationError) as exc:
        _settings(oauth_known_redirect_hosts='["claude.ai"')
    assert "OAUTH_KNOWN_REDIRECT_HOSTS" in str(exc.value)


def test_an_empty_list_is_accepted_and_means_nothing_is_recognised():
    """The safe direction: every consent screen warns. A stale list is safe and
    a wrong one is loud."""
    assert _settings(oauth_known_redirect_hosts="").oauth_known_redirect_hosts == []
    assert _settings(oauth_known_redirect_hosts=[]).oauth_known_redirect_hosts == []


@pytest.mark.parametrize(
    "entry",
    [
        "*.claude.ai",
        "claude.ai/callback",
        "user@claude.ai",
        "claude ai",
        "claude\tai",
    ],
)
def test_a_pattern_entry_is_refused_at_configuration_time(entry):
    """Matching is exact-host equality, so an entry containing a wildcard, a
    path, userinfo or an internal space matches **nothing** — the operator
    would have allow-listed a host that can never be recognised while believing
    the opposite. Refusing at startup makes that a container that will not
    start."""
    with pytest.raises(ValidationError) as exc:
        _settings(oauth_known_redirect_hosts=entry)
    assert "patterns are not supported" in str(exc.value)


def test_a_pattern_inside_an_otherwise_valid_list_is_refused():
    with pytest.raises(ValidationError):
        _settings(oauth_known_redirect_hosts="claude.ai,*.evil.example")


def test_a_non_list_value_is_refused():
    with pytest.raises(ValidationError):
        _settings(oauth_known_redirect_hosts=17)


# --------------------------------------------------------------------------
# every setter's form reads the constant too
# --------------------------------------------------------------------------

#: The four password-setting forms and the handler whose context feeds each.
#: A `minlength` written into the markup is how the number drifted in the first
#: place: the server moved to twelve and three of these went on promising
#: eight, so the browser accepted exactly what the handler then refused.
PASSWORD_FORMS = {
    "src/control_panel/templates/register.html": ("src.auth.routes", "_render_register"),
    "src/control_panel/templates/account.html": ("src.control_panel.routes", "account_page"),
    "src/control_panel/templates/users.html": ("src.control_panel.users", "list_users"),
    "src/control_panel/templates/user_edit.html": ("src.control_panel.users", "edit_user_form"),
}


#: Resolved from this file, not from the working directory — another module in
#: the suite `chdir`s, and a relative path here would read whatever happened to
#: sit at that name (or nothing) depending on collection order.
REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("template", sorted(PASSWORD_FORMS))
def test_no_password_form_hard_codes_a_minimum(template):
    markup = (REPO_ROOT / template).read_text()
    assert 'minlength="{{ min_password_length }}"' in markup
    # No numeric `minlength` on these forms at any value: a literal that
    # happens to equal today's constant is still a literal.
    assert 'minlength="8"' not in markup
    assert f'minlength="{MIN_PASSWORD_LENGTH}"' not in markup


@pytest.mark.parametrize("template", sorted(PASSWORD_FORMS))
def test_every_password_form_is_handed_the_constant(template):
    """A template reading `min_password_length` off a context that has none
    renders `minlength=""` — silently no minimum at all — so the binding is
    asserted at the handler, not only in the markup."""
    import importlib
    import inspect

    module_name, function_name = PASSWORD_FORMS[template]
    module = importlib.import_module(module_name)
    source = inspect.getsource(getattr(module, function_name))
    assert '"min_password_length": MIN_PASSWORD_LENGTH' in source
