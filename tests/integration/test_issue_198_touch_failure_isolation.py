"""#198 — a failed `last_seen_at` touch must not cost the request its identity.

The unit tests in `tests/test_issue_198_session_registry.py` assert that the
touch's failure path does not roll the enclosing transaction back. They cannot
assert the *consequence* of having done so, because the consequence is a
SQLAlchemy behaviour no fake has.

**What that consequence actually is.** `Session.rollback()` restores the
identity map to its state at the start of the transaction, which **expunges
every object that became persistent inside it**. The object loaded inside this
one is the authenticated `User` that `get_active_session_user` is about to
return, so rolling back to recover from a failed *telemetry* write handed the
panel a **detached** user.

Detached is quiet. Every column already loaded still reads, so nothing raises
and nothing looks wrong. What breaks is *writing*: a mutation through a
detached instance is not in the session's unit of work, so `commit()` reports
success and persists **nothing**. A refused `last_seen_at` update therefore
turned any later write through the request's own user object into a silent lost
update. (A lazy relationship on a detached instance raises
`DetachedInstanceError`, not the `MissingGreenlet` an *expired* instance would
give — the distinction only changes which message an operator sees, and neither
should happen at all.)

The trigger is narrow and entirely plausible: the `UPDATE` refused while the
`SELECT`s succeed. A revoked `UPDATE` grant on `user_sessions`, a trigger
rejecting the write, a hand-added constraint. This module reproduces it the
blunt way — a `BEFORE UPDATE` trigger that raises — against a **real**
`AsyncSession` on a **real** migrated database, and drives the production
dependency chain (`get_current_user`, then `get_active_session_user`, then
`require_user_panel`) end to end. The assertions are the two things that
actually differ across the fix: the user's **persistence state**, and whether a
write through it survives.

Skipped unless `PGVECTOR_TEST_ADMIN_URL` is set. `make test-schema` runs it.
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime
import os

import pytest
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import _harness

REQUIRED = os.environ.get("OMCP_REQUIRE_SCHEMA_INTEGRATION") == "1"
pytestmark = [] if REQUIRED else [_harness.requires_pgvector]

DIM = 1024

#: A `BEFORE UPDATE` trigger standing in for every way the write can be refused
#: while the reads keep working. `SELECT` is untouched, which is the whole
#: point: a database that had simply gone away would fail the reads too and
#: there would be no resolved session left to lose.
REJECT_UPDATES = (
    """
    CREATE FUNCTION reject_session_updates() RETURNS trigger AS $$
    BEGIN
        RAISE EXCEPTION 'user_sessions is not writable here';
    END;
    $$ LANGUAGE plpgsql
    """,
    """
    CREATE TRIGGER no_touch BEFORE UPDATE ON user_sessions
    FOR EACH ROW EXECUTE FUNCTION reject_session_updates()
    """,
)

#: The one statement the session row is read back with, in both directions.
LAST_SEEN = "SELECT last_seen_at FROM user_sessions WHERE id = :id"

#: Written through the resolved user, to see whether the write survives.
MUTATED = "/vaults/written-through-the-session"


@pytest.fixture(autouse=True)
def multi_user(monkeypatch):
    """The session registry only exists in multi-user mode — `get_current_user`
    short-circuits to the single-user sentinel otherwise and would answer with
    an identity this module never seeded."""
    from src.auth import session as auth_session

    monkeypatch.setattr(auth_session.settings, "multi_user_mode", True)


@contextlib.contextmanager
def _migrated_database():
    generator = _harness.throwaway_database("touch_isolation", DIM)
    url = next(generator)
    try:
        yield url
    finally:
        generator.close()


async def _install_trigger(maker):
    """Two statements, issued apart: asyncpg prepares each one and will not
    accept a script."""
    async with maker() as ddl:
        for statement in REJECT_UPDATES:
            await ddl.execute(sa.text(statement))
        await ddl.commit()


async def _seed(maker, *, sid: str):
    """One active user and one **stale** session row, keyed the way the mint
    keys it — stale so the throttle passes and the touch is actually issued."""
    from src.auth.session import hash_session_id

    now = datetime.datetime.now(datetime.timezone.utc)
    async with maker() as setup:
        await setup.execute(
            sa.text(
                "INSERT INTO users (username, password_hash, is_admin, is_active, "
                "session_version, vault_path) VALUES "
                "('toucher', 'x', false, true, 1, '/vaults/toucher')"
            )
        )
        user_id = (
            await setup.execute(
                sa.text("SELECT id FROM users WHERE username = 'toucher'")
            )
        ).scalar_one()
        await setup.execute(
            sa.text(
                "INSERT INTO user_sessions "
                "(id, user_id, created_at, last_seen_at, expires_at) "
                "VALUES (:id, :uid, :created, :seen, :expires)"
            ),
            {
                "id": hash_session_id(sid),
                "uid": user_id,
                "created": now - datetime.timedelta(hours=2),
                "seen": now - datetime.timedelta(hours=1),
                "expires": now + datetime.timedelta(days=7),
            },
        )
        await setup.commit()
    return user_id


def _cookie(user_id: int, sid: str) -> dict:
    from src.auth.session import SESSION_ID_KEY

    return {
        "user_id": user_id,
        "session_version": 1,
        "is_admin": False,
        "username": "toucher",
        SESSION_ID_KEY: sid,
    }


async def _resolve_and_use(url: str, *, reject_updates: bool):
    """Drive the production chain and then *use* what it returned.

    Resolving is not the assertion — what the resolved object can still do
    afterwards is. Engine created and disposed inside this coroutine: an engine
    outlived by its loop cannot close its connections.
    """
    import session_helpers as sh
    from src.auth import session as auth_session
    from src.control_panel.routes import require_user_panel

    sid = "touch-isolation-session"
    engine = create_async_engine(url)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        user_id = await _seed(maker, sid=sid)
        if reject_updates:
            await _install_trigger(maker)

        request = sh.browser_request(
            method="GET", path="/admin/", session=_cookie(user_id, sid)
        )

        async with maker() as session:
            user = await auth_session.get_current_user(request, session)
            assert user is not None, "the session row is live; this must resolve"

            # The reads the panel performs. These keep working either way — a
            # detached instance still holds every column it loaded — which is
            # exactly why the defect was quiet.
            columns = (user.id, user.username, user.is_admin, user.vault_path)

            # The dependency every panel route is gated on, driven for real.
            gated = await require_user_panel(request=request, user=user, session=session)

            state = sa_inspect(user)
            attached = (state.persistent, state.detached, user in session)

            # And the thing that actually breaks: a write through the request's
            # own user object. Detached, this commits nothing and says nothing.
            user.vault_path = MUTATED
            await session.commit()

        async with maker() as verify:
            persisted = (
                await verify.execute(
                    sa.text("SELECT vault_path FROM users WHERE id = :id"),
                    {"id": user_id},
                )
            ).scalar_one()

        return columns, gated.username, attached, persisted
    finally:
        await engine.dispose()


async def _touch_outcome(url: str, *, reject_updates: bool):
    """`last_seen_at` before and after one resolve, plus whether the injection
    was actually installed."""
    import session_helpers as sh
    from src.auth import session as auth_session
    from src.auth.session import hash_session_id

    sid = "touch-isolation-session"
    engine = create_async_engine(url)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        user_id = await _seed(maker, sid=sid)
        if reject_updates:
            await _install_trigger(maker)

        async with maker() as probe:
            before = (
                await probe.execute(sa.text(LAST_SEEN), {"id": hash_session_id(sid)})
            ).scalar_one()

        request = sh.browser_request(
            method="GET", path="/admin/", session=_cookie(user_id, sid)
        )
        async with maker() as session:
            resolved = await auth_session.get_current_user(request, session)
            assert resolved is not None, "the row is live; this must resolve"

        async with maker() as probe:
            after = (
                await probe.execute(sa.text(LAST_SEEN), {"id": hash_session_id(sid)})
            ).scalar_one()
            installed = (
                await probe.execute(
                    sa.text("SELECT count(*) FROM pg_trigger WHERE tgname = 'no_touch'")
                )
            ).scalar_one()
        return before, after, installed
    finally:
        await engine.dispose()


def test_a_refused_touch_leaves_the_user_attached_and_writable():
    """The regression, against a real `AsyncSession`.

    Before the savepoint this returned a **detached** user and the write below
    was silently discarded: `commit()` succeeded and the column kept its old
    value. Nothing raised, so nothing would have been noticed.
    """
    with _migrated_database() as url:
        columns, username, attached, persisted = asyncio.run(
            _resolve_and_use(url, reject_updates=True)
        )

    assert columns[1] == "toucher"
    assert username == "toucher"

    persistent, detached, in_session = attached
    assert persistent is True, "a failed telemetry write must not detach the user"
    assert detached is False
    assert in_session is True
    assert persisted == MUTATED, (
        "a write through the request's own user object was silently lost — the "
        "rollback in the touch's failure path had expunged it from the session"
    )


def test_the_same_request_behaves_identically_when_the_touch_succeeds():
    """The control.

    Without it the case above passes just as well against a validator that
    never issues the touch at all, and it pins the intended property: a working
    touch and a refused one are indistinguishable to everything downstream.
    """
    with _migrated_database() as url:
        columns, username, attached, persisted = asyncio.run(
            _resolve_and_use(url, reject_updates=False)
        )

    assert columns[1] == "toucher"
    assert username == "toucher"
    assert attached == (True, False, True)
    assert persisted == MUTATED


def test_a_refused_touch_leaves_the_row_exactly_as_it_was():
    """Non-vacuity for the regression above.

    The trigger must actually be installed — otherwise the first test proves
    nothing — and `last_seen_at` must be untouched, because the savepoint rolled
    back the one statement that would have moved it.
    """
    with _migrated_database() as url:
        before, after, installed = asyncio.run(_touch_outcome(url, reject_updates=True))

    assert installed == 1, "the injection must have been installed"
    assert before == after, "the savepoint rolled the only write back"


def test_the_touch_lands_when_nothing_rejects_it():
    """And its mirror: with no trigger the same stale row *is* advanced, so the
    equality above is the refusal's doing and not a touch that never happens."""
    with _migrated_database() as url:
        before, after, installed = asyncio.run(
            _touch_outcome(url, reject_updates=False)
        )

    assert installed == 0
    assert after > before, "a stale row on a GET must be touched"
