"""Panel/OAuth user password hashing — bcrypt, called directly.

This module previously wrapped `passlib.context.CryptContext(schemes=["bcrypt"])`.
passlib has been unmaintained since 2020 and its backend probe hashes a
>72-byte password on import; bcrypt 4.1+ raises instead of truncating, so
`CryptContext` blew up at construction time and took every login and every
password change with it. passlib was used in exactly this one module, so it is
gone and `bcrypt` is called directly.

Output format is unchanged: modular-crypt `$2b$12$…`, which is exactly what
passlib's bcrypt backend produced. Every hash already in the `users` table
verifies under this implementation — no re-hash, no migration.

**The 72-byte rule.** bcrypt only ever consumed the first 72 bytes of a
password. passlib truncated silently; bcrypt 5.x raises `ValueError` instead.
Both functions here therefore truncate the UTF-8 *encoding* of the password to
72 bytes before handing it to bcrypt, which reproduces passlib's semantics
exactly. That matters for compatibility, not just for convenience: a stored
hash for a 100-byte password was computed over its first 72 bytes only, and
would become unverifiable — locking that user out — if we started rejecting or
hashing the full string instead.

The consequence, inherited from bcrypt itself and unchanged by this rewrite:
**two passwords whose first 72 UTF-8 bytes are identical are the same password
here.** Password policy is unaffected (minimum 8 characters, no maximum).

Truncation is deliberately on bytes, not characters: slicing the encoding can
split a multi-byte character and yield invalid UTF-8, which is fine because
bcrypt hashes bytes and we never decode them back.
"""
import bcrypt

# bcrypt's own default is also 12; stated explicitly so the cost factor is a
# decision recorded here rather than whatever a future release picks.
_BCRYPT_ROUNDS = 12

# bcrypt ignores everything past this many bytes of the password.
_MAX_PASSWORD_BYTES = 72


def _prepare(plain: str) -> bytes:
    """UTF-8 encode and truncate to bcrypt's 72-byte input limit."""
    return plain.encode("utf-8")[:_MAX_PASSWORD_BYTES]


def hash_password(plain: str) -> str:
    """Return a `$2b$12$…` modular-crypt hash of `plain`."""
    return bcrypt.hashpw(_prepare(plain), bcrypt.gensalt(rounds=_BCRYPT_ROUNDS)).decode("ascii")


def verify_password(plain: str, hashed: str) -> bool:
    """Check `plain` against a stored bcrypt hash.

    `checkpw` accepts the `$2a$` / `$2b$` / `$2y$` prefixes, so hashes from any
    era of this application verify. A stored value that is not a well-formed
    bcrypt hash returns False rather than raising: it can never match anything,
    and a 500 on the login route would be a worse answer than a failed login.
    """
    try:
        return bcrypt.checkpw(_prepare(plain), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False
