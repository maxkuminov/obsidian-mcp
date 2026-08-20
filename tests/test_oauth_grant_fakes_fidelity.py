"""The fake session must not be more forgiving than Postgres.

Every concurrency and listing property in the grant-family suite is asserted
*through* `tests/_oauth_grant_fakes.FakeSession`, so a fake that answers a
question more loosely than the database does converts a real regression into a
green run. Two loosenesses were found by review and are pinned here:

* **Lock detection by substring.** `"pg_advisory_xact_lock" in str(stmt)` counts
  a decoy — the function name inside a SQL comment, or a statement that merely
  mentions it — as a lock that was taken. Every "the lock came first" assertion
  would then pass against a handler that never locked.
* **Expiry ignored.** `oauth_page` splits its listing with `expires_at > now`
  and `expires_at <= now`. A fake that ignored both returned every row to both
  queries, so the live/dead split — the thing that keeps a live grant on the
  page and caps only history — was never exercised, and an expired token would
  have rendered as live.
"""
import datetime

from sqlalchemy import or_, select, text

from src.models.db import OAuthToken

from _oauth_grant_fakes import (
    ADVISORY_LOCK_SQL,
    FakeSession,
    FakeToken,
    _is_advisory_lock,
    in_hours,
    utcnow,
)


# --- the lock matcher -----------------------------------------------------


def test_the_real_lock_statement_is_recognised():
    assert _is_advisory_lock(text(ADVISORY_LOCK_SQL)) is True
    # Whitespace is normalized, so a reformatted but identical statement counts.
    assert _is_advisory_lock(text("SELECT   pg_advisory_xact_lock(:key)\n")) is True


def test_a_decoy_mentioning_the_lock_is_not_a_lock():
    """A comment naming the function must not be counted as taking it."""
    decoys = [
        text("SELECT 1 -- pg_advisory_xact_lock(:key)"),
        text("/* pg_advisory_xact_lock */ SELECT 1"),
        text("SELECT pg_advisory_xact_lock(:key), 1"),
        text("SELECT pg_try_advisory_xact_lock(:key)"),
        select(OAuthToken).where(OAuthToken.grant_id == "pg_advisory_xact_lock(:key)"),
    ]
    for stmt in decoys:
        assert _is_advisory_lock(stmt) is False, str(stmt)


def test_a_decoy_is_not_recorded_as_an_advisory_lock():
    """End to end through the fake: the decoy must not land in the log.

    This is the assertion that matters — `advisory_locks` is what the ordering
    tests read, so a decoy counted there is a lock the handler never took. The
    fake refuses the statement outright instead, which is the right second
    half: an unmodelled statement must fail loudly, not pass as something else.
    """
    import asyncio

    import pytest

    session = FakeSession(tokens=[])

    with pytest.raises(AssertionError, match="unexpected statement"):
        asyncio.run(
            session.execute(
                text("SELECT 1 -- pg_advisory_xact_lock(:key)"), {"key": 99}
            )
        )

    assert session.advisory_locks == []


# --- expiry, both directions ---------------------------------------------


def _tokens():
    return [
        FakeToken(grant_id="g1", token_type="access", expires_at=in_hours(1)),
        FakeToken(grant_id="g1", token_type="refresh", expires_at=in_hours(-1)),
    ]


def _run(session, stmt):
    import asyncio

    async def go():
        result = await session.execute(stmt)
        return result.scalars().all()

    return asyncio.run(go())


def test_a_greater_than_comparison_keeps_only_unexpired_rows():
    tokens = _tokens()
    session = FakeSession(tokens=tokens)
    now = utcnow()

    rows = _run(session, select(OAuthToken).where(OAuthToken.expires_at > now))

    assert [t.token_type for t in rows] == ["access"]


def test_a_less_than_or_equal_comparison_keeps_only_expired_rows():
    tokens = _tokens()
    session = FakeSession(tokens=tokens)
    now = utcnow()

    rows = _run(session, select(OAuthToken).where(OAuthToken.expires_at <= now))

    assert [t.token_type for t in rows] == ["refresh"]


def test_the_history_disjunction_keeps_revoked_but_unexpired_rows():
    """`revoked OR expires_at <= now` is a disjunction, not two AND filters.

    Evaluating the halves separately would drop every revoked-but-unexpired
    row — exactly the revocation the operator just performed, and the row the
    panel lists so that revocation is visible.
    """
    revoked_live = FakeToken(
        grant_id="g1", token_type="access", revoked=True, expires_at=in_hours(5)
    )
    expired = FakeToken(grant_id="g1", token_type="refresh", expires_at=in_hours(-1))
    healthy = FakeToken(grant_id="g1", token_type="access", expires_at=in_hours(5))
    session = FakeSession(tokens=[revoked_live, expired, healthy])
    now = utcnow()

    rows = _run(
        session,
        select(OAuthToken).where(
            or_(OAuthToken.revoked == True, OAuthToken.expires_at <= now)  # noqa: E712
        ),
    )

    assert {t.id for t in rows} == {revoked_live.id, expired.id}


def test_an_unmodelled_comparison_fails_loudly():
    """Silence would be the failure mode this whole module is about."""
    import asyncio

    import pytest

    session = FakeSession(tokens=_tokens())
    stmt = select(OAuthToken).where(
        OAuthToken.expires_at.between(
            utcnow(), utcnow() + datetime.timedelta(hours=2)
        )
    )

    with pytest.raises(AssertionError, match="expires_at"):
        asyncio.run(session.execute(stmt))
