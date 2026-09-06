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

**NUL bytes are rejected**, as passlib rejected them. The C bcrypt of the
`$2b$` era treated an embedded NUL as end-of-string, so `"secret\\0anything"`
and `"secret"` hashed identically — a password whose entropy silently stops at
its first NUL. Raw `bcrypt` 5.x no longer enforces this, so the check lives
here: `hash_password` raises and `verify_password` returns False before bcrypt
is ever called. This is the same policy the previous implementation had, so no
stored hash can have been produced from a NUL-containing password.

**A malformed stored hash fails closed.** A `password_hash` column that is not
a well-formed bcrypt hash makes `checkpw` raise; we record one
`password_hash_malformed` event (no password, no hash bytes — only the row's
own id, when the caller knows it) and return False. It can never match
anything, and a failed login is a better answer than a 500 on the login route.

The record goes through `src/services/security_events.py` rather than straight
to a logger because **a caller drives it through the login form**: one corrupted
column plus a scripted login loop is an unbounded flood channel, and every
caller-triggerable refusal in this server is bounded by the same suppressor.
"""
import bcrypt

from src.services import security_events

# bcrypt's own default is also 12; stated explicitly so the cost factor is a
# decision recorded here rather than whatever a future release picks.
_BCRYPT_ROUNDS = 12

# bcrypt ignores everything past this many bytes of the password.
_MAX_PASSWORD_BYTES = 72


def _prepare(plain: str) -> bytes:
    """UTF-8 encode and truncate to bcrypt's 72-byte input limit."""
    return plain.encode("utf-8")[:_MAX_PASSWORD_BYTES]


def hash_password(plain: str) -> str:
    """Return a `$2b$12$…` modular-crypt hash of `plain`.

    Raises `ValueError` if `plain` contains a NUL byte (passlib's policy —
    see the module docstring).
    """
    if "\x00" in plain:
        raise ValueError("password must not contain NUL bytes")
    return bcrypt.hashpw(_prepare(plain), bcrypt.gensalt(rounds=_BCRYPT_ROUNDS)).decode("ascii")


def verify_password(plain: str, hashed: str, *, user_id: int | None = None) -> bool:
    """Check `plain` against a stored bcrypt hash.

    `checkpw` accepts the `$2a$` / `$2b$` / `$2y$` prefixes, so hashes from any
    era of this application verify. Missing, non-string, or malformed stored
    values and NUL-containing candidates all return False rather than raising.

    `user_id` is optional and names the row whose column is corrupt, so an
    operator can fix the one account rather than search for it. It is the only
    thing the record carries: never the candidate password, never the stored
    bytes.
    """
    if hashed is None or not isinstance(hashed, str):
        return False
    if "\x00" in plain:
        # No hash we could have produced came from such a password.
        return False
    try:
        return bcrypt.checkpw(_prepare(plain), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        security_events.emit(
            "password_hash_malformed",
            subject=security_events.subject_for(user_id=user_id),
            user_id=user_id,
        )
        return False
