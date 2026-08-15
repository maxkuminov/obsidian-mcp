"""Password hashing contract — see `openspec/specs/panel-password-hashing`.

The fixtures below are real hashes produced by the *previous* implementation
(`passlib` 1.7.4 `CryptContext(schemes=["bcrypt"])` on `bcrypt` 4.0.1), captured
before passlib was removed. They stand in for the rows already in the `users`
table: if a refactor of `src/auth/passwords.py` ever stops verifying them, every
existing account is locked out, and that is what these tests are here to catch.
Hashes are not secrets — the plaintexts are throwaway test values.
"""
import bcrypt
import pytest

from src.auth.passwords import hash_password, verify_password

# --- passlib-era fixtures (bcrypt 4.0.1) ----------------------------------

LEGACY_SHORT_PLAIN = "correct horse battery staple"
LEGACY_SHORT_HASH = "$2b$12$EP.Yz26zxEONEDzupWEFwOOugqIHNMeWdz5kIt3Aw9.0xfJ33EGG6"

# 86 UTF-8 bytes — longer than bcrypt's 72-byte input limit, so passlib hashed
# only its first 72 bytes and this hash is only verifiable by an implementation
# that truncates the same way.
LEGACY_LONG_PLAIN = (
    "correct horse battery staple correct horse battery staple "
    "correct horse battery staple"
)
LEGACY_LONG_HASH = "$2b$12$hpykISqy/.5RoCprr39L5OLWW4f8xiQqvXi7yurXfkM0fN5ECgQQy"

# Identical to LEGACY_LONG_PLAIN through byte 72, different after it. Under the
# documented truncation semantics this is the *same* password.
LEGACY_LONG_TWIN = LEGACY_LONG_PLAIN.encode("utf-8")[:72].decode("utf-8") + "XXXXXX"

LEGACY_UNICODE_PLAIN = "pässwörd-æøå-日本語-🔐"
LEGACY_UNICODE_HASH = "$2b$12$BMtmZzFDc0is8PwUdcKG1eg1cS1izHqkAUci2pJ4LVRvqpQQo/4Pq"


# --- Round trip -----------------------------------------------------------


def test_round_trip_accepts_same_password():
    assert verify_password(LEGACY_SHORT_PLAIN, hash_password(LEGACY_SHORT_PLAIN))


def test_round_trip_rejects_different_password():
    assert not verify_password("not the password", hash_password(LEGACY_SHORT_PLAIN))


def test_hash_is_bcrypt_2b_cost_12():
    assert hash_password("hunter2hunter2").startswith("$2b$12$")


def test_each_hash_uses_a_fresh_salt():
    first = hash_password(LEGACY_SHORT_PLAIN)
    second = hash_password(LEGACY_SHORT_PLAIN)
    assert first != second
    assert verify_password(LEGACY_SHORT_PLAIN, first)
    assert verify_password(LEGACY_SHORT_PLAIN, second)


# --- Legacy (passlib-era) hashes still verify -----------------------------


@pytest.mark.parametrize(
    ("plain", "hashed"),
    [
        (LEGACY_SHORT_PLAIN, LEGACY_SHORT_HASH),
        (LEGACY_LONG_PLAIN, LEGACY_LONG_HASH),
        (LEGACY_UNICODE_PLAIN, LEGACY_UNICODE_HASH),
    ],
    ids=["short", "over-72-bytes", "non-ascii"],
)
def test_legacy_passlib_hash_verifies(plain, hashed):
    assert verify_password(plain, hashed)


@pytest.mark.parametrize(
    ("plain", "hashed"),
    [
        ("wrong password entirely", LEGACY_SHORT_HASH),
        (LEGACY_SHORT_PLAIN, LEGACY_UNICODE_HASH),
    ],
    ids=["wrong-plaintext", "hash-of-another-password"],
)
def test_legacy_hash_rejects_wrong_password(plain, hashed):
    assert not verify_password(plain, hashed)


# --- 72-byte truncation semantics -----------------------------------------


def test_long_password_hashes_and_verifies():
    assert len(LEGACY_LONG_PLAIN.encode("utf-8")) > 72
    assert verify_password(LEGACY_LONG_PLAIN, hash_password(LEGACY_LONG_PLAIN))


def test_72_byte_prefix_twin_verifies():
    """Documented (and inherited) semantics: bcrypt only reads 72 bytes."""
    encoded = LEGACY_LONG_TWIN.encode("utf-8")
    assert encoded[:72] == LEGACY_LONG_PLAIN.encode("utf-8")[:72]
    assert encoded != LEGACY_LONG_PLAIN.encode("utf-8")
    # Against a hash we make now...
    assert verify_password(LEGACY_LONG_TWIN, hash_password(LEGACY_LONG_PLAIN))
    # ...and against the passlib-era one, which truncated identically.
    assert verify_password(LEGACY_LONG_TWIN, LEGACY_LONG_HASH)


def test_password_differing_within_first_72_bytes_is_rejected():
    changed = "X" + LEGACY_LONG_PLAIN[1:]
    assert not verify_password(changed, LEGACY_LONG_HASH)


def test_truncation_splitting_a_multibyte_character_is_survivable():
    """Truncation is on bytes, so it can cut a character in half.

    That must not raise (a `str` slice would have been the tempting, wrong
    implementation: it would truncate at 72 *characters*, i.e. well past
    bcrypt's limit for any non-ASCII password, and re-raise the bug this
    change exists to fix).
    """
    plain = "a" * 71 + "é"  # 'é' straddles the 72-byte boundary
    assert len(plain.encode("utf-8")) == 73
    hashed = hash_password(plain)
    assert verify_password(plain, hashed)


# --- Non-ASCII ------------------------------------------------------------


@pytest.mark.parametrize(
    "plain",
    ["pässwörd", "日本語のパスワード", "🔐🔑🗝️ emoji password", "æøå-ÆØÅ-ß"],
)
def test_non_ascii_round_trip(plain):
    hashed = hash_password(plain)
    assert verify_password(plain, hashed)
    assert not verify_password(plain + "x", hashed)


# --- Malformed stored hashes ----------------------------------------------


@pytest.mark.parametrize("stored", ["", "not-a-hash", "$2b$12$short", "$9z$99$" + "a" * 53])
def test_malformed_stored_hash_fails_closed(stored):
    """A junk `password_hash` column must fail the login, not 500 it."""
    assert verify_password("anything", stored) is False


# --- passlib is gone ------------------------------------------------------


def test_passlib_is_not_installed():
    """The dependency is dropped, not merely unused (spec: `passlib` SHALL NOT
    be a dependency). A stale install in a developer venv would otherwise let
    an accidental re-import pass review."""
    with pytest.raises(ImportError):
        __import__("passlib")


def test_bcrypt_is_at_least_5():
    """The pin exists because <5 is what the old passlib pin forced. If a
    resolver quietly walks bcrypt back, say so here rather than in production."""
    major = int(bcrypt.__version__.split(".")[0])
    assert major >= 5
