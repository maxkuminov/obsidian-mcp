"""#74 — `check_upload` must survive a routine OAuth refresh rotation.

`lookup_by_public_id` used to scope an OAuth-minted transfer to the exact
`oauth_tokens.id` row that minted it. An access token lives one hour and
rotation mints a *new* row for the same user, the same client and the same
consent, so an hour after minting an upload link the agent's own
`check_upload` answered "no upload link with id … was minted by this
identity" — the message reserved for somebody else's handle — about a
completed upload it had minted itself.

The behavioural proof lives in `tests/integration/test_transfer_pg.py`, which
runs the real correlated `EXISTS` against Postgres with real rotated rows.
What is pinned *here* is the predicate the service builds, because that is
what the always-on suite can see: the OAuth branch must key on the grant
family and must not pin the presenting token's row id, and the API-key branch
must stay exactly as narrow as it was.
"""
from __future__ import annotations

import asyncio
import datetime
import inspect

import pytest
from sqlalchemy.dialects import postgresql

from src.models.db import OAuthToken, TransferToken
from src.oauth import scope as oauth_scope
from src.services import transfer


def _sql(identity: transfer.Identity) -> str:
    """The statement `lookup_by_public_id` would issue, as literal SQL.

    Captured from the service itself rather than rebuilt here: a test that
    reconstructs the query pins its own copy of the predicate, not the one
    production runs.
    """
    captured: list = []

    class _Result:
        def scalar_one_or_none(self):
            return None

    class _Session:
        async def execute(self, stmt):
            captured.append(stmt)
            return _Result()

    asyncio.run(
        transfer.lookup_by_public_id(
            _Session(),
            "a" * 22,
            identity=identity,
            direction="upload",
        )
    )
    assert len(captured) == 1, "the lookup must stay a single statement"
    return str(
        captured[0].compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


OAUTH = transfer.Identity(oauth_token_id=9, user_id=7)
API_KEY = transfer.Identity(key_id=3, user_id=7)
NO_CREDENTIAL = transfer.Identity()


def test_oauth_lookup_keys_on_the_grant_family_not_the_token_row():
    sql = _sql(OAUTH)
    # The presenting token's row id may only appear as the *entry point* into
    # its family, never as a constraint on the transfer row itself. This is
    # the exact predicate #74 is about.
    assert "transfer_tokens.oauth_token_id = 9" not in sql
    assert "grant_id" in sql, "the family is what an OAuth handle belongs to"
    assert "presenting_token.id = 9" in sql
    assert "minting_token.id = transfer_tokens.oauth_token_id" in sql
    assert "presenting_token.grant_id = minting_token.grant_id" in sql


def test_oauth_lookup_keeps_every_other_scoping_predicate():
    sql = _sql(OAUTH)
    assert "transfer_tokens.public_id = '" + "a" * 22 + "'" in sql
    assert "transfer_tokens.direction = 'upload'" in sql
    # Defence in depth on top of the family: a grant belongs to one
    # (client_id, user_id), but the user comparison stays regardless.
    assert "transfer_tokens.user_id = 7" in sql
    # An OAuth caller must never reach an API-key-minted row.
    assert "transfer_tokens.key_id IS NULL" in sql


def test_api_key_lookup_is_not_widened():
    """A key does not rotate, so nothing about it becomes a family."""
    sql = _sql(API_KEY)
    assert "transfer_tokens.key_id = 3" in sql
    assert "transfer_tokens.oauth_token_id IS NULL" in sql
    assert "oauth_tokens" not in sql
    assert "grant_id" not in sql


def test_credential_less_identity_still_matches_only_credential_less_rows():
    sql = _sql(NO_CREDENTIAL)
    assert "transfer_tokens.key_id IS NULL" in sql
    assert "transfer_tokens.oauth_token_id IS NULL" in sql
    assert "transfer_tokens.user_id IS NULL" in sql
    assert "oauth_tokens" not in sql


def test_lookup_returns_none_when_nothing_matches():
    """The capture harness stands in for a real miss; the answer is `None`."""

    class _Result:
        def scalar_one_or_none(self):
            return None

    class _Session:
        async def execute(self, stmt):
            return _Result()

    assert (
        asyncio.run(
            transfer.lookup_by_public_id(
                _Session(), "b" * 22, identity=OAUTH, direction="upload"
            )
        )
        is None
    )


def test_write_scope_test_is_the_shared_helper():
    """The private `"readwrite" not in scope.split()` copy is gone.

    Behaviour is unchanged — both forms are membership tests — but a fourth
    private copy of "does this scope grant write" is exactly what
    `src/oauth/scope.py` was created to prevent (#67).
    """
    assert transfer.token_has_write is oauth_scope.token_has_write
    source = inspect.getsource(transfer)
    assert '"readwrite" not in' not in source
    assert "'readwrite' not in" not in source


@pytest.mark.parametrize(
    "scope,expected",
    [
        ("readwrite", True),
        ("offline_access readwrite", True),
        ("read", False),
        (None, False),
    ],
)
def test_oauth_write_predicate_reads_scope_as_a_set(scope, expected):
    """`_credential_ok`'s write test, through whichever helper it now calls.

    A scope string is a space-separated *set*: `"offline_access readwrite"` is
    a write grant. Unchanged behaviour, asserted so the delegation cannot
    quietly become an equality test.
    """
    token = OAuthToken(
        user_id=None,
        token_hash="x" * 64,
        token_type="access",
        client_id="c",
        scope=scope,
        grant_id="g",
        expires_at=datetime.datetime.now(datetime.timezone.utc)
        + datetime.timedelta(hours=1),
        revoked=False,
    )
    row = TransferToken(user_id=None)
    assert transfer._credential_ok(token, need_write=True, row=row) is expected
