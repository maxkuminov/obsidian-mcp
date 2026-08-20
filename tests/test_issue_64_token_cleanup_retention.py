"""The periodic cleanup must not delete a revocation's evidence.

`cleanup_expired_tokens` ran `expires_at < cutoff OR revoked` — and the second
disjunct carried **no age condition at all**, despite the docstring promising
"older than 7 days". It runs from the indexer loop every
`INDEX_INTERVAL_SECONDS` (5 minutes by default), so every revoked token was
gone within minutes of being revoked.

That was harmless while the panel filtered revoked rows out of the page. It is
not harmless now: issue #64 made the OAuth page *list* revoked tokens
precisely because a Revoke that made the row vanish read as success even when
it had done nothing. Purging the row five minutes later recreates the same
blank space with a delay.

The window is measured from `expires_at`. Revocation time is not stored, but a
token can only be revoked while it exists, so its revocation time R satisfies
`R <= expires_at`; deleting only when `expires_at < now - 7d` therefore
guarantees `R < now - 7d`. `created_at` would give the opposite: a refresh
token minted 30 days ago and revoked a minute ago is already 30 days past
`created_at` and would be purged at once.
"""
import asyncio
import datetime

import pytest
from sqlalchemy import or_
from sqlalchemy.sql import operators
from sqlalchemy.sql.dml import Delete
from sqlalchemy.sql.elements import (
    BinaryExpression,
    BindParameter,
    BooleanClauseList,
    False_,
    True_,
)

from src.models.db import OAuthCode, OAuthToken
from src.services import indexer

FROZEN_NOW = datetime.datetime(2026, 8, 20, 12, 0, tzinfo=datetime.timezone.utc)
RETENTION = datetime.timedelta(days=7)
EXPECTED_CUTOFF = FROZEN_NOW - RETENTION


class _FrozenDatetime(datetime.datetime):
    @classmethod
    def now(cls, tz=None):
        return FROZEN_NOW if tz is None else FROZEN_NOW.astimezone(tz)


class _Result:
    rowcount = 0


class _RecordingSession:
    """Captures the DELETE statements the cleanup emits, executing nothing."""

    def __init__(self):
        self.statements = []
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def execute(self, stmt, *_a, **_kw):
        self.statements.append(stmt)
        return _Result()

    async def commit(self):
        self.committed = True


def run_cleanup(monkeypatch) -> _RecordingSession:
    session = _RecordingSession()
    monkeypatch.setattr(indexer, "async_session", lambda: session)
    monkeypatch.setattr(indexer, "datetime", _FrozenDatetime)
    asyncio.run(indexer.cleanup_expired_tokens())
    assert session.committed is True
    return session


def statement_for(session, entity) -> Delete:
    for stmt in session.statements:
        assert isinstance(stmt, Delete), stmt
        if stmt.table.name == entity.__tablename__:
            return stmt
    raise AssertionError(f"no DELETE emitted for {entity.__name__}")


# --- the predicate itself -------------------------------------------------


def test_token_cleanup_is_age_gated_and_carries_no_bare_revoked_disjunct(monkeypatch):
    """Structural, not a string grep: the WHERE clause is compared as a tree."""
    session = run_cleanup(monkeypatch)
    where = statement_for(session, OAuthToken).whereclause

    assert where.compare(OAuthToken.expires_at < EXPECTED_CUTOFF), (
        "the token cleanup must delete on expiry age alone"
    )
    # The shape this replaces. Pinned explicitly so a revert is a test failure
    # rather than a silent five-minute retention window.
    assert not where.compare(
        or_(OAuthToken.expires_at < EXPECTED_CUTOFF, OAuthToken.revoked == True)  # noqa: E712
    )


def test_the_cutoff_is_seven_days_before_now(monkeypatch):
    session = run_cleanup(monkeypatch)
    params = statement_for(session, OAuthToken).compile().params

    assert params == {"expires_at_1": EXPECTED_CUTOFF}


def test_auth_code_cleanup_is_deliberately_unchanged(monkeypatch):
    """A used code is spent immediately and has no history value.

    Pinned so the fix above is visibly scoped to tokens: an editor who
    "simplified" both branches together would drop the `used` disjunct and
    leave spent codes in the table until their expiry.
    """
    session = run_cleanup(monkeypatch)
    where = statement_for(session, OAuthCode).whereclause

    assert where.compare(
        or_(OAuthCode.expires_at < EXPECTED_CUTOFF, OAuthCode.used == True)  # noqa: E712
    )


# --- what that predicate does to actual rows ------------------------------


def _literal(node):
    """The Python value behind a comparison's right-hand side."""
    if isinstance(node, BindParameter):
        return node.value
    if isinstance(node, True_):
        return True
    if isinstance(node, False_):
        return False
    raise AssertionError(f"unsupported literal in the cleanup predicate: {node!r}")


def evaluate(clause, row: dict) -> bool:
    """Evaluate the emitted WHERE clause against a row, in Python.

    Deliberately a real (if tiny) evaluator rather than a peek at one bind
    parameter: reading the cutoff alone would keep passing if somebody put the
    unqualified `OR revoked` disjunct back, since the cutoff itself would not
    change. Walking the tree means these cases actually depend on the shape.
    Only the node types this cleanup can emit are supported — anything else
    fails loudly instead of being silently treated as false.
    """
    if isinstance(clause, BooleanClauseList):
        parts = [evaluate(part, row) for part in clause.clauses]
        if clause.operator is operators.or_:
            return any(parts)
        if clause.operator is operators.and_:
            return all(parts)
        raise AssertionError(f"unsupported boolean operator: {clause.operator}")
    if isinstance(clause, BinaryExpression):
        value = row[clause.left.name]
        other = _literal(clause.right)
        if clause.operator is operators.lt:
            return value < other
        if clause.operator is operators.eq:
            return value == other
        raise AssertionError(f"unsupported comparison: {clause.operator}")
    raise AssertionError(f"unsupported clause: {clause!r}")


def deleted_by(session, expires_at: datetime.datetime, revoked: bool = True) -> bool:
    """Would the emitted token predicate delete this row?

    `revoked` defaults to True because every case below is about a token the
    operator has just revoked — the rows whose disappearance is the bug.
    """
    where = statement_for(session, OAuthToken).whereclause
    return evaluate(where, {"expires_at": expires_at, "revoked": revoked})


@pytest.mark.parametrize(
    "label, expires_at, expected",
    [
        # A refresh token revoked a moment ago still has 30 days to run.
        ("just revoked, 30 days of life left", FROZEN_NOW + datetime.timedelta(days=30), False),
        # An access token revoked a moment ago dies in an hour — and its row
        # must outlive it by a week so the operator can see the revocation.
        ("just revoked, expires in an hour", FROZEN_NOW + datetime.timedelta(hours=1), False),
        ("expired an hour ago", FROZEN_NOW - datetime.timedelta(hours=1), False),
        ("expired six days ago", FROZEN_NOW - datetime.timedelta(days=6), False),
        ("expired eight days ago", FROZEN_NOW - datetime.timedelta(days=8), True),
        ("expired a year ago", FROZEN_NOW - datetime.timedelta(days=365), True),
    ],
)
def test_retention_window_by_expiry(monkeypatch, label, expires_at, expected):
    session = run_cleanup(monkeypatch)
    assert deleted_by(session, expires_at) is expected, label


def test_revocation_evidence_survives_at_least_the_retention_window(monkeypatch):
    """The property the column choice buys, stated as a test.

    A token can only be revoked while it exists, so R <= expires_at. Sweeping
    across every plausible remaining lifetime, no row a revocation could have
    just touched is deletable.
    """
    session = run_cleanup(monkeypatch)
    for days_of_life_left in (0, 1, 7, 30):
        expires_at = FROZEN_NOW + datetime.timedelta(days=days_of_life_left)
        # Revoked "now"; the row must still be there.
        assert deleted_by(session, expires_at) is False

    # And a `created_at`-based window would have failed exactly here: a token
    # minted 30 days ago, revoked now, is 30 days past creation.
    created_at = FROZEN_NOW - datetime.timedelta(days=30)
    assert created_at < EXPECTED_CUTOFF, "the rejected alternative really does purge it"


def test_revoked_and_unrevoked_rows_are_treated_identically(monkeypatch):
    """`revoked` must not appear in the predicate at all any more.

    Retention is a function of expiry age alone, so the same row is deleted or
    kept regardless of the flag. This is the assertion that fails first if the
    unqualified disjunct comes back.
    """
    session = run_cleanup(monkeypatch)
    for offset_days in (-365, -8, -7, -1, 0, 1, 30):
        expires_at = FROZEN_NOW + datetime.timedelta(days=offset_days)
        assert deleted_by(session, expires_at, revoked=True) is deleted_by(
            session, expires_at, revoked=False
        ), offset_days
