"""Grant families: the unit an operator actually consents to and revokes.

A `/authorize` approval produces **one grant**. The token endpoint then mints
an access/refresh pair from it, and every later rotation mints another pair.
Before issue #64 nothing tied those rows together, so the panel could only
offer per-row controls -- and a per-row Revoke was close to a no-op, because
the sibling refresh token minted a fresh, identically-scoped access token on
the client's next ordinary 401 retry. A per-row scope *downgrade* was worse: it
silently restored itself within the hour.

`oauth_tokens.grant_id` (migration 014) is that missing identifier. Every row
minted from one consent shares it, rotation inherits it, and revocation and
downgrade act on the whole family. There is exactly one way to find a family --
`grant_id == g` -- because the decision in #64 was explicit that two code paths
for "find the family" is how this bug comes back.

**Invariant: one `grant_id` belongs to exactly one `(client_id, user_id)`.**
It is established at every write site (`_handle_auth_code` stamps one id on one
user's pair; `_handle_refresh` copies both `grant_id` and `user_id` from the
row it rotates) and by 014's backfill, which groups by exactly those two
columns. Family operations therefore do *not* re-filter by `user_id`: an
incomplete revocation is the failure this module exists to prevent, and a
`user_id` predicate would reintroduce a way for one to happen.

This module is dependency-light on purpose -- the control panel and the OAuth
routes both use it, and #67 warned against the panel reaching into
`src/oauth/routes.py`.
"""
import hashlib
import secrets

from sqlalchemy import text, update

from src.models.db import OAuthToken

# 32 URL-safe characters. Opaque: nothing may parse it, and it is never shown
# to a client -- it exists only to join rows minted from one consent.
GRANT_ID_BYTES = 24

# Postgres advisory locks are keyed by bigint, so the opaque id is folded to
# one. A separate namespace key keeps us from colliding with any other advisory
# lock this application might take later.
_ADVISORY_NAMESPACE = 0x0A17  # "oauth grant"

# The single-user -> multi-user bootstrap claims every ownerless row for the
# first admin with `UPDATE ... WHERE user_id IS NULL` (`register_submit` in
# `src/auth/routes.py`), and it already holds this key for that transaction.
# The token endpoint takes it too, because that UPDATE's snapshot is taken when
# the statement starts: a mint committing afterwards inserts a *new* NULL-owner
# token the claim can no longer see, and it survives as a token belonging to
# nobody. Sharing one key makes the two mutually exclusive.
#
# **The value is a wire constant, not an implementation detail.** During a
# rolling deploy an old process holds it under the literal below while a new
# one imports it from here; changing it would silently un-serialize exactly the
# window this exists to close.
USER_BOOTSTRAP_LOCK_KEY = 7283910429


def new_grant_id() -> str:
    """A fresh grant identifier for one consent event."""
    return secrets.token_urlsafe(GRANT_ID_BYTES)


def grant_lock_key(grant_id: str) -> int:
    """Signed 64-bit advisory-lock key for a grant id.

    Derived in Python rather than with Postgres' `hashtext()`, which is
    undocumented and whose algorithm has changed between majors.
    """
    digest = hashlib.sha256(
        _ADVISORY_NAMESPACE.to_bytes(2, "big") + grant_id.encode()
    ).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


async def lock_user_bootstrap(session) -> None:
    """Hold off the ownerless-row claim for this transaction, and vice versa.

    Taken by both token-minting handlers and by `register_submit`'s claim, so
    a token cannot be inserted with `user_id IS NULL` in the window between the
    claim's UPDATE and its COMMIT. Without it the bootstrap reports success,
    the claim covers every row it could see, and the pair a client rotated into
    a moment later belongs to no user at all.

    **Lock order is this lock first, then any per-grant lock.** `_handle_refresh`
    takes both; the panel's family operations take only the grant lock and
    never this one; the bootstrap takes only this one. So no path holds a grant
    lock while asking for this, and there is no cycle.

    Taken unconditionally rather than only under `multi_user_mode`: the flag can
    change between processes during a deploy, and a global lock on a path that
    runs once per consent and once per hourly refresh costs nothing.
    """
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"),
        {"key": USER_BOOTSTRAP_LOCK_KEY},
    )


async def lock_grant(session, grant_id: str) -> None:
    """Serialize everything that touches one grant family, for this transaction.

    Without this, revocation loses a race it looks like it wins. Under READ
    COMMITTED an `UPDATE ... WHERE grant_id = :g` takes its snapshot when the
    statement starts, so rows a concurrent refresh *inserts* afterwards are
    invisible to it: the panel reports "revoked", every row it saw is revoked,
    and the brand-new access/refresh pair the client just rotated into survives.
    Row locks cannot close that -- the offending rows do not exist yet when the
    lock would be taken. A lock on the *family* does.

    Both sides take this before touching any family row, and nothing else, so
    the acquisition order is total and there is no cycle to deadlock on. It is
    a transaction-scoped lock: it releases on COMMIT or ROLLBACK, never leaks,
    and needs no explicit unlock.

    The caller must issue this as its own statement *before* the statement that
    reads or writes the family -- that later statement then takes a fresh
    snapshot, which is precisely what makes a concurrently-inserted row visible.
    """
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"),
        {"key": grant_lock_key(grant_id)},
    )


async def revoke_grant_family(session, grant_id: str) -> int:
    """Revoke every still-live token in one grant family. Does not commit.

    Access tokens are included, and that is the point: rotation is allowed to
    let an old access token run to its natural expiry, but revocation is not.
    An hour of surviving write access after the operator clicked Revoke is
    exactly the failure this replaced.

    Returns the number of rows actually flipped, so a caller can tell "already
    fully revoked" from "revoked N tokens" without a second query.
    """
    await lock_grant(session, grant_id)
    result = await session.execute(
        update(OAuthToken)
        .where(OAuthToken.grant_id == grant_id, OAuthToken.revoked == False)  # noqa: E712
        .values(revoked=True)
    )
    return result.rowcount or 0


async def set_grant_family_scope(session, grant_id: str, scope: str) -> int:
    """Write one scope onto every still-live token in a family. Does not commit.

    Revoked rows are left alone so the panel's history keeps showing what each
    one carried when it died; rewriting them would forge the record.

    The caller is responsible for having clamped `scope` to the client's
    registration first -- this function is the *write*, not the policy.
    """
    await lock_grant(session, grant_id)
    result = await session.execute(
        update(OAuthToken)
        .where(OAuthToken.grant_id == grant_id, OAuthToken.revoked == False)  # noqa: E712
        .values(scope=scope)
    )
    return result.rowcount or 0
