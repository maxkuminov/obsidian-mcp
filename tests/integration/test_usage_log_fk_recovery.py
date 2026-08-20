"""Opt-in integration: a usage row survives a credential deleted mid-call.

`_log_usage` writes `key_id` / `oauth_token_id` / `user_id` alongside the
denormalised `actor_*` label. A tool call can outlive its own credential — an
operator revokes and deletes a key, or deletes an OAuth client, while a slow
call is still running — and the insert then names a row that no longer exists.
PostgreSQL raises `foreign_key_violation`, and a blanket `except` around the
commit drops the whole audit line: precisely the call an operator investigating
that credential most wants to see, and precisely the one whose durable
attribution the `actor_*` columns already carry. Denormalising the label
achieves nothing if the row it rides on is the thing discarded.

The offline suite (`tests/test_issue_77_usage_attribution.py`) pins the retry's
*logic* against a faked driver error. This module pins the part a fake cannot:
that PostgreSQL really raises what the recovery matches on — SQLSTATE 23503,
with the constraint name the retry uses to decide whether `user_id` was the FK
that failed — and that the row lands, labelled, with the dangling FKs NULL.

Skipped unless `PGVECTOR_TEST_ADMIN_URL` is set — see `_harness.py`. Each test
creates its own throwaway database. `make test-schema` does not run this module;
`pytest tests/integration/` against a throwaway server does.
"""
import datetime

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import _harness
import src.mcp_server.tools as tools
from src.auth.session import current_actor
from src.config import settings

pytestmark = [
    _harness.requires_pgvector,
    pytest.mark.asyncio(loop_scope="module"),
]

DIM = int(settings.embedding_dimensions)

FUTURE = datetime.datetime(2099, 1, 1, tzinfo=datetime.timezone.utc)

KEY_ACTOR = ("api_key", "nightly sync", "omcp_a1b2c3")
OAUTH_ACTOR = ("oauth", "Claude Desktop", "client-abc")


@pytest.fixture(scope="module")
def migrated_url():
    yield from _harness.throwaway_database("usage_fk_recovery", DIM)


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def sessionmaker(migrated_url):
    engine = create_async_engine(migrated_url, poolclass=None)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest_asyncio.fixture(loop_scope="module")
async def clean(sessionmaker):
    """A fresh cast per test: one user, one key, one OAuth client and token."""
    async with sessionmaker() as session:
        await session.execute(text("DELETE FROM usage_logs"))
        await session.execute(text("DELETE FROM oauth_tokens"))
        await session.execute(text("DELETE FROM oauth_clients"))
        await session.execute(text("DELETE FROM api_keys"))
        await session.execute(text("DELETE FROM users"))
        await session.execute(text(
            "INSERT INTO users (id, username, password_hash, is_admin, is_active, "
            "session_version) VALUES (1, 'alice', 'x', false, true, 1)"
        ))
        await session.execute(text(
            "INSERT INTO api_keys (id, name, key_hash, key_prefix, permission, "
            "is_active, user_id) "
            "VALUES (1, 'nightly sync', 'h', 'omcp_a1b2c3', 'read', true, 1)"
        ))
        await session.execute(text(
            "INSERT INTO oauth_clients (client_id, client_secret_hash, "
            "token_endpoint_auth_method, client_name, redirect_uris, scope) "
            "VALUES ('client-abc', NULL, 'none', 'Claude Desktop', '[]'::jsonb, 'read')"
        ))
        await session.execute(text(
            "INSERT INTO oauth_tokens (id, token_hash, token_type, client_id, "
            "scope, user_id, grant_id, expires_at, revoked) "
            "VALUES (1, 'th', 'access', 'client-abc', 'read', 1, 'g1', :exp, false)"
        ), {"exp": FUTURE})
        await session.commit()
    return sessionmaker


class _Ids:
    """Stands in for the auth ContextVars `_log_usage` reads."""

    def __init__(self, value):
        self._value = value

    def get(self):
        return self._value


async def _log_under(monkeypatch, maker, *, key_id, oauth_token_id, user_id, actor):
    monkeypatch.setattr(tools, "async_session", maker)
    monkeypatch.setattr(tools, "current_api_key_id", _Ids(key_id))
    monkeypatch.setattr(tools, "current_oauth_token_id", _Ids(oauth_token_id))
    monkeypatch.setattr(tools, "current_user_id", _Ids(user_id))
    token = current_actor.set(actor)
    try:
        await tools._log_usage("read_note", {"path": "a.md"}, 12, 34)
    finally:
        current_actor.reset(token)


async def _rows(maker):
    async with maker() as session:
        result = await session.execute(text(
            "SELECT key_id, oauth_token_id, user_id, tool, "
            "       actor_kind, actor_label, actor_ref "
            "FROM usage_logs ORDER BY id"
        ))
        return result.fetchall()


async def test_the_row_lands_intact_when_the_credential_is_still_there(clean, monkeypatch):
    """The control. Without it, a test that only ever sees the retry path
    cannot tell a working recovery from an insert that never worked at all."""
    await _log_under(
        monkeypatch, clean, key_id=1, oauth_token_id=None, user_id=1, actor=KEY_ACTOR
    )

    rows = await _rows(clean)
    assert len(rows) == 1
    assert (rows[0].key_id, rows[0].user_id) == (1, 1)
    assert (rows[0].actor_kind, rows[0].actor_label, rows[0].actor_ref) == KEY_ACTOR


async def test_a_key_deleted_between_tool_start_and_log_keeps_its_label(clean, monkeypatch):
    """The panel's own sequence, run while a call is in flight.

    `delete_key_form` NULLs `usage_logs.key_id` and deletes the key. A call
    that started before that and logs after it carries `current_api_key_id` for
    a row that is gone — SQLSTATE 23503 on `usage_logs_key_id_fkey`.
    """
    async with clean() as session:
        await session.execute(text("DELETE FROM api_keys WHERE id = 1"))
        await session.commit()

    await _log_under(
        monkeypatch, clean, key_id=1, oauth_token_id=None, user_id=1, actor=KEY_ACTOR
    )

    rows = await _rows(clean)
    assert len(rows) == 1, "the audit row was discarded"
    assert rows[0].key_id is None
    # The key FK failing says nothing about the user, and the panel scopes a
    # non-admin's usage page by `user_id` — clearing it would hide the row from
    # the one person entitled to see it.
    assert rows[0].user_id == 1
    assert (rows[0].actor_kind, rows[0].actor_label, rows[0].actor_ref) == KEY_ACTOR


async def test_an_oauth_client_deleted_mid_call_keeps_its_label(clean, monkeypatch):
    """Deleting the client cascades `oauth_tokens`, so the token id in flight
    is dangling by the time the call logs."""
    async with clean() as session:
        await session.execute(
            text("DELETE FROM oauth_clients WHERE client_id = 'client-abc'")
        )
        await session.commit()
        remaining = (await session.execute(
            text("SELECT count(*) FROM oauth_tokens")
        )).scalar_one()
    assert remaining == 0, "the cascade did not run; the test proves nothing"

    await _log_under(
        monkeypatch, clean, key_id=None, oauth_token_id=1, user_id=1, actor=OAUTH_ACTOR
    )

    rows = await _rows(clean)
    assert len(rows) == 1, "the audit row was discarded"
    assert rows[0].oauth_token_id is None
    assert rows[0].user_id == 1
    assert (rows[0].actor_kind, rows[0].actor_label, rows[0].actor_ref) == OAUTH_ACTOR


async def test_a_deleted_user_clears_user_id_and_still_records_the_call(clean, monkeypatch):
    """`usage_logs.user_id` is ON DELETE SET NULL, so an *existing* row survives
    a user delete. A row being inserted at that moment is not covered by that
    and raises on `fk_usage_logs_user_id` — which is the one constraint whose
    violation may drop `user_id`."""
    async with clean() as session:
        await session.execute(text("DELETE FROM users WHERE id = 1"))
        await session.commit()

    await _log_under(
        monkeypatch, clean, key_id=None, oauth_token_id=None, user_id=1, actor=KEY_ACTOR
    )

    rows = await _rows(clean)
    assert len(rows) == 1, "the audit row was discarded"
    assert rows[0].user_id is None
    assert (rows[0].actor_kind, rows[0].actor_label, rows[0].actor_ref) == KEY_ACTOR


async def test_the_real_error_shape_is_what_the_recovery_matches_on(clean):
    """The assumption the offline fakes encode, checked against the real stack.

    The failure arrives wrapped twice: SQLAlchemy's `IntegrityError`, whose
    `.orig` is the asyncpg *dialect's* DBAPI-shaped error, whose `__cause__` is
    asyncpg's own `ForeignKeyViolationError`. The SQLSTATE sits on the middle
    layer and the constraint name only on the innermost one — a distinction no
    fake would have discovered, and one that silently degraded `user_id` on
    every recovery when the first draft read `orig.constraint_name` alone.

    The constraint *names* are pinned too: migration 001 leaves `key_id`'s to
    PostgreSQL's default while 007 and 009 name theirs, so the three do not
    follow one convention and `_violated_user_fk` has to tell them apart.
    """
    async with clean() as session:
        await session.execute(text("DELETE FROM api_keys WHERE id = 1"))
        await session.commit()

    with pytest.raises(Exception) as caught:  # noqa: PT011 - the type is the subject
        async with clean() as session:
            await session.execute(text(
                "INSERT INTO usage_logs (key_id, tool) VALUES (1, 'read_note')"
            ))
            await session.commit()

    assert tools._is_fk_violation(caught.value), caught.value
    assert tools._fk_constraint_name(caught.value) == "usage_logs_key_id_fkey"
    assert not tools._violated_user_fk(caught.value)


async def test_the_user_constraint_is_named_as_the_retry_expects(clean):
    async with clean() as session:
        await session.execute(text("DELETE FROM users WHERE id = 1"))
        await session.commit()

    with pytest.raises(Exception) as caught:  # noqa: PT011 - the type is the subject
        async with clean() as session:
            await session.execute(text(
                "INSERT INTO usage_logs (user_id, tool) VALUES (1, 'read_note')"
            ))
            await session.commit()

    assert tools._fk_constraint_name(caught.value) == tools._USER_FK_CONSTRAINT
    assert tools._violated_user_fk(caught.value)


async def test_a_non_fk_integrity_error_is_not_treated_as_recoverable(clean):
    """The retry must not fire for a different constraint class — a NOT NULL
    violation is not fixed by clearing the credential columns."""
    with pytest.raises(Exception) as caught:  # noqa: PT011 - the type is the subject
        async with clean() as session:
            await session.execute(text("INSERT INTO usage_logs (tool) VALUES (NULL)"))
            await session.commit()

    assert not tools._is_fk_violation(caught.value), caught.value
