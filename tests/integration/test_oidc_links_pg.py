"""Real-Postgres tests for `src/services/oidc_links.py` (#416, slice A).

What needs a real database: the UNIQUE/CHECK constraints the operations rely
on, the account guard serialising two binds of one identity, the guard's
re-entry by `start_session` in the same transaction, the database clock that
drives expiry, and a SAVEPOINT that survives a unique violation.

Skipped unless `PGVECTOR_TEST_ADMIN_URL` names a throwaway Postgres server
(`make test-integration` sets it).
"""
import asyncio
import datetime

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from starlette.requests import Request

import _harness
from src.auth.session import start_session
from src.models.db import User, UserSession
from src.services import oidc_links

DIM = 64
ISS = "https://idp.example.test/realms/home"
OTHER_ISS = "https://old.example.test/realms/home"

pytestmark = [
    pytest.mark.asyncio(loop_scope="module"),
    _harness.requires_pgvector,
]


@pytest.fixture(scope="module")
def migrated_url():
    yield from _harness.throwaway_database("oidc_links", DIM)


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def maker(migrated_url):
    engine = create_async_engine(migrated_url, pool_size=10, max_overflow=5)
    sessionmaker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield sessionmaker
    await engine.dispose()


@pytest_asyncio.fixture(loop_scope="module")
async def db(maker):
    async def wipe():
        async with maker() as session:
            await session.execute(delete(UserSession))
            await session.execute(delete(User))
            await session.commit()

    await wipe()
    yield maker
    await wipe()


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


async def make_user(maker, username, *, is_admin=False, is_active=True,
                    bound=None, pending=None, pending_age_days=0):
    async with maker() as session:
        user = User(username=username, password_hash="x", is_admin=is_admin,
                    is_active=is_active, session_version=1)
        session.add(user)
        await session.flush()
        values = {}
        if bound:
            values.update(oidc_issuer=bound[0], oidc_subject=bound[1],
                          oidc_linked_at=text("now()"))
        if pending:
            values.update(
                oidc_expected_issuer=pending[0], oidc_expected_email=pending[1],
                oidc_expected_set_at=text(f"now() - interval '{pending_age_days} days'"),
            )
        if values:
            await session.execute(update(User).where(User.id == user.id).values(**values))
        await session.commit()
        return user.id


async def row(maker, user_id):
    async with maker() as session:
        return (
            await session.execute(select(User).where(User.id == user_id))
        ).scalar_one_or_none()


def link_columns(user):
    return (user.oidc_issuer, user.oidc_subject, user.oidc_linked_at is not None,
            user.oidc_expected_issuer, user.oidc_expected_email,
            user.oidc_expected_set_at is not None)


UNLINKED = (None, None, False, None, None, False)


def request():
    return Request({
        "type": "http", "method": "GET", "path": "/admin/auth/oidc/callback",
        "headers": [(b"user-agent", b"pytest")], "query_string": b"",
        "client": ("10.0.0.1", 1), "session": {}, "app": None,
    })


async def login(maker, iss, sub, email, verified, *, mint=True):
    """resolve → prepare_login → start_session, the callback's DB phase."""
    async with maker() as session:
        res = await oidc_links.resolve(session, iss, sub, email, verified)
        prepared = await oidc_links.prepare_login(session, res)
        sid = None
        if prepared.ok and mint:
            sid = await start_session(
                request(), session, prepared.user_id,
                expected_session_version=prepared.session_version,
            )
        elif prepared.ok:
            await session.commit()
        return res, prepared, sid


# --------------------------------------------------------------------------
# resolve + prepare_login
# --------------------------------------------------------------------------


async def test_pending_bind_happy_path(db):
    uid = await make_user(db, "max", pending=(ISS, "max@example.com"), pending_age_days=1)
    res, prepared, sid = await login(db, ISS, "sub-max", "  Max@Example.COM ", True)
    assert res.kind == "pending" and res.user_id == uid and res.email == "max@example.com"
    assert prepared.ok and prepared.linked and prepared.user_id == uid
    assert prepared.session_version == 1
    assert sid is not None
    user = await row(db, uid)
    assert link_columns(user) == (ISS, "sub-max", True, None, None, False)
    assert user.last_login_at is not None

    # After the bind only (iss, sub) is consulted: no email, still the user.
    res2, prepared2, sid2 = await login(db, ISS, "sub-max", None, False)
    assert res2.kind == "bound" and prepared2.ok and not prepared2.linked and sid2


async def test_expired_expectation_does_not_match(db):
    uid = await make_user(db, "max", pending=(ISS, "max@example.com"), pending_age_days=8)
    res, prepared, sid = await login(db, ISS, "sub-max", "max@example.com", True)
    assert res.kind == "none" and prepared.reason == "unlinked" and sid is None
    assert link_columns(await row(db, uid))[:3] == (None, None, False)


async def test_expectation_under_another_issuer_does_not_match(db):
    uid = await make_user(db, "max", pending=(OTHER_ISS, "max@example.com"))
    res, prepared, _ = await login(db, ISS, "sub-max", "max@example.com", True)
    assert res.kind == "none" and prepared.reason == "unlinked"
    assert (await row(db, uid)).oidc_expected_issuer == OTHER_ISS


@pytest.mark.parametrize("verified", [False, "true", None, 1])
async def test_unverified_email_refuses_and_writes_nothing(db, verified):
    uid = await make_user(db, "max", pending=(ISS, "max@example.com"))
    res, prepared, _ = await login(db, ISS, "sub-max", "max@example.com", verified)
    assert res.kind == "unverified" and prepared.reason == "email_unverified"
    user = await row(db, uid)
    assert user.oidc_subject is None and user.oidc_expected_email == "max@example.com"
    assert user.last_login_at is None


@pytest.mark.parametrize("email", [None, "max@@example.com", "x" * 300, "not-an-email"])
async def test_absent_or_malformed_email_on_the_pending_branch_is_unlinked(db, email):
    await make_user(db, "max", pending=(ISS, "max@example.com"))
    res, prepared, _ = await login(db, ISS, "sub-max", email, True)
    assert res.kind == "none" and prepared.reason == "unlinked"


async def test_bound_user_resolves_with_no_email(db):
    uid = await make_user(db, "max", bound=(ISS, "sub-max"))
    res, prepared, sid = await login(db, ISS, "sub-max", None, False)
    assert res == oidc_links.Resolution("bound", ISS, "sub-max", user_id=uid)
    assert prepared.ok and not prepared.linked and sid
    user = await row(db, uid)
    assert user.last_login_at is not None
    assert link_columns(user)[:2] == (ISS, "sub-max")


async def test_bound_identity_wins_over_another_users_pending_match(db):
    a = await make_user(db, "a", bound=(ISS, "sub-a"))
    b = await make_user(db, "b", pending=(ISS, "shared@example.com"))
    res, prepared, sid = await login(db, ISS, "sub-a", "shared@example.com", True)
    assert res.kind == "bound" and res.user_id == a and prepared.user_id == a and sid
    assert (await row(db, b)).oidc_expected_email == "shared@example.com"


async def test_reassigned_address_with_a_new_subject_is_not_the_bound_user(db):
    await make_user(db, "a", bound=(ISS, "sub-a"))
    res, prepared, _ = await login(db, ISS, "sub-new", "a@example.com", True)
    assert res.kind == "none" and prepared.reason == "unlinked"


async def test_inactive_user_is_refused_before_any_write(db):
    uid = await make_user(db, "max", is_active=False, pending=(ISS, "max@example.com"))
    res, prepared, _ = await login(db, ISS, "sub-max", "max@example.com", True)
    assert res.kind == "pending" and prepared.reason == "account"
    user = await row(db, uid)
    assert user.oidc_subject is None and user.last_login_at is None

    bound = await make_user(db, "old", is_active=False, bound=(ISS, "sub-old"))
    _, prepared, _ = await login(db, ISS, "sub-old", None, False)
    assert prepared.reason == "account"
    assert (await row(db, bound)).last_login_at is None


async def test_unlink_between_resolve_and_prepare_refuses(db):
    uid = await make_user(db, "max", bound=(ISS, "sub-max"))
    async with db() as session:
        res = await oidc_links.resolve(session, ISS, "sub-max", None, False)
        assert res.kind == "bound"
        async with db() as admin:
            assert (await oidc_links.unlink(admin, uid)).ok
            await admin.commit()
        prepared = await oidc_links.prepare_login(session, res)
    assert prepared.reason == "unlinked"
    assert (await row(db, uid)).last_login_at is None


async def test_expectation_replaced_between_resolve_and_prepare_refuses(db):
    uid = await make_user(db, "max", pending=(ISS, "max@example.com"))
    async with db() as session:
        res = await oidc_links.resolve(session, ISS, "sub-max", "max@example.com", True)
        async with db() as admin:
            assert (await oidc_links.set_expectation(admin, uid, ISS, "new@example.com")).ok
            await admin.commit()
        prepared = await oidc_links.prepare_login(session, res)
    assert prepared.reason == "unlinked"
    assert (await row(db, uid)).oidc_subject is None


async def test_concurrent_double_bind_of_one_identity_exactly_one_wins(db):
    """Two users pending on different addresses; one provider subject whose
    token carries each address in turn. The first bind holds the guard
    uncommitted while the second is issued; the second must wait, then see
    the first's commit and refuse `link_conflict`."""
    a = await make_user(db, "a", pending=(ISS, "a@example.com"))
    b = await make_user(db, "b", pending=(ISS, "b@example.com"))

    first = db()
    second = db()
    try:
        res_a = await oidc_links.resolve(first, ISS, "sub-x", "a@example.com", True)
        res_b = await oidc_links.resolve(second, ISS, "sub-x", "b@example.com", True)
        assert res_a.kind == res_b.kind == "pending"

        prepared_a = await oidc_links.prepare_login(first, res_a)
        assert prepared_a.ok and prepared_a.linked

        pending = asyncio.create_task(oidc_links.prepare_login(second, res_b))
        await asyncio.sleep(0.5)
        assert not pending.done(), "the second bind must wait on the account guard"

        await first.commit()
        prepared_b = await asyncio.wait_for(pending, timeout=10)
        assert prepared_b.reason == "link_conflict"
    finally:
        await first.close()
        await second.close()

    assert (await row(db, a)).oidc_subject == "sub-x"
    user_b = await row(db, b)
    assert user_b.oidc_subject is None and user_b.oidc_expected_email == "b@example.com"


async def test_start_session_reenters_the_guard_in_the_same_transaction(db):
    """`prepare_login` holds the account guard; `start_session` takes it
    again on the same connection. Advisory locks are re-entrant, so this must
    not block, and the bind, `last_login_at` and the session row commit
    together."""
    uid = await make_user(db, "max", pending=(ISS, "max@example.com"))
    async with db() as session:
        res = await oidc_links.resolve(session, ISS, "sub-max", "max@example.com", True)
        prepared = await oidc_links.prepare_login(session, res)
        sid = await asyncio.wait_for(
            start_session(request(), session, prepared.user_id,
                          expected_session_version=prepared.session_version),
            timeout=5,
        )
    assert sid
    async with db() as session:
        count = (await session.execute(
            select(UserSession).where(UserSession.user_id == uid))).scalars().all()
    assert len(count) == 1
    assert (await row(db, uid)).oidc_subject == "sub-max"


async def test_a_refused_mint_leaves_no_binding_and_no_last_login(db):
    uid = await make_user(db, "max", pending=(ISS, "max@example.com"))
    async with db() as session:
        res = await oidc_links.resolve(session, ISS, "sub-max", "max@example.com", True)
        prepared = await oidc_links.prepare_login(session, res)
        assert prepared.ok
        sid = await start_session(request(), session, prepared.user_id,
                                  expected_session_version=prepared.session_version + 1)
        assert sid is None
    user = await row(db, uid)
    assert user.oidc_subject is None and user.oidc_expected_email == "max@example.com"
    assert user.last_login_at is None


# --------------------------------------------------------------------------
# reachable_admins_after (D15)
# --------------------------------------------------------------------------


async def test_reachable_admins_after_counts_only_bound_active_admins(db):
    a = await make_user(db, "a", is_admin=True, bound=(ISS, "sa"))
    b = await make_user(db, "b", is_admin=True, bound=(ISS, "sb"))
    unbound_admin = await make_user(db, "c", is_admin=True)
    await make_user(db, "d", bound=(ISS, "sd"))  # bound, not an admin
    await make_user(db, "e", is_admin=True, is_active=False, bound=(ISS, "se"))
    await make_user(db, "f", is_admin=True, bound=(OTHER_ISS, "sf"))  # previous issuer
    await make_user(db, "g", is_admin=True, pending=(ISS, "g@example.com"))  # pending only

    async with db() as session:
        for state in ("unlinked", "demoted", "deactivated", "deleted"):
            assert await oidc_links.reachable_admins_after(
                session, ISS, target_id=a, target_after=state) == 1, state
            assert await oidc_links.reachable_admins_after(
                session, ISS, target_id=unbound_admin, target_after=state) == 2, state
        # Under the previous issuer only f is reachable.
        assert await oidc_links.reachable_admins_after(
            session, OTHER_ISS, target_id=a, target_after="unlinked") == 1

        # Sees this transaction's own write: unlink b, then a is the last one.
        assert (await oidc_links.unlink(session, b)).ok
        assert await oidc_links.reachable_admins_after(
            session, ISS, target_id=a, target_after="demoted") == 0
        await session.rollback()


# --------------------------------------------------------------------------
# admin operations
# --------------------------------------------------------------------------


async def test_set_expectation_normalizes_replaces_and_refuses(db):
    uid = await make_user(db, "max")
    other = await make_user(db, "other", pending=(ISS, "taken@example.com"))
    bound = await make_user(db, "bound", bound=(ISS, "sub-b"))
    async with db() as session:
        assert (await oidc_links.set_expectation(session, uid, ISS, "Max@Example.com")).outcome == "ok"
        await session.commit()
    user = await row(db, uid)
    assert (user.oidc_expected_issuer, user.oidc_expected_email) == (ISS, "max@example.com")
    first_set = user.oidc_expected_set_at

    async with db() as session:
        # Replacing a pending expectation is allowed.
        assert (await oidc_links.set_expectation(session, uid, ISS, "max2@example.com")).ok
        assert (await oidc_links.set_expectation(session, uid, ISS, "TAKEN@example.com")).outcome == "conflict"
        assert (await oidc_links.set_expectation(session, bound, ISS, "b@example.com")).outcome == "bound_already"
        assert (await oidc_links.set_expectation(session, uid, ISS, "two@@x")).outcome == "invalid"
        assert (await oidc_links.set_expectation(session, uid, ISS, None)).outcome == "invalid"
        assert (await oidc_links.set_expectation(session, 999_999, ISS, "x@y.z")).outcome == "invalid"
        await session.commit()
    user = await row(db, uid)
    assert user.oidc_expected_email == "max2@example.com"
    assert user.oidc_expected_set_at >= first_set
    assert (await row(db, other)).oidc_expected_email == "taken@example.com"
    assert link_columns(await row(db, bound))[:2] == (ISS, "sub-b")


async def test_bind_subject_binds_clears_pending_and_refuses(db):
    uid = await make_user(db, "max", pending=(ISS, "max@example.com"))
    await make_user(db, "holder", bound=(ISS, "sub-taken"))
    async with db() as session:
        assert (await oidc_links.bind_subject(session, uid, ISS, "sub-taken")).outcome == "conflict"
        for bad in ("", "has space", "s" * 256, "ünï", None):
            assert (await oidc_links.bind_subject(session, uid, ISS, bad)).outcome == "invalid", bad
        assert (await oidc_links.bind_subject(session, uid, ISS, "sub-max")).outcome == "ok"
        await session.commit()
    assert link_columns(await row(db, uid)) == (ISS, "sub-max", True, None, None, False)
    async with db() as session:
        # A bound user is never re-pointed, not even to the same subject.
        assert (await oidc_links.bind_subject(session, uid, ISS, "sub-other")).outcome == "bound_already"
        assert (await oidc_links.bind_subject(session, uid, ISS, "sub-max")).outcome == "bound_already"
        await session.rollback()


async def test_unlink_clears_all_six_columns_and_revokes_sessions(db):
    uid = await make_user(db, "max", bound=(ISS, "sub-max"))
    now = datetime.datetime.now(datetime.timezone.utc)
    async with db() as session:
        for i in range(2):
            session.add(UserSession(id=f"{i:064d}", user_id=uid, created_at=now,
                                    last_seen_at=now,
                                    expires_at=now + datetime.timedelta(hours=1)))
        await session.commit()
    async with db() as session:
        result = await oidc_links.unlink(session, uid)
        assert result == oidc_links.AdminResult("ok", revoked_sessions=2)
        await session.commit()
    assert link_columns(await row(db, uid)) == UNLINKED
    async with db() as session:
        rows = (await session.execute(
            select(UserSession.revoked_at).where(UserSession.user_id == uid))).scalars().all()
    assert len(rows) == 2 and all(r is not None for r in rows)

    async with db() as session:
        assert (await oidc_links.unlink(session, uid)).outcome == "invalid"
        assert (await oidc_links.unlink(session, 999_999)).outcome == "invalid"
        await session.rollback()


async def test_unlink_cancels_a_pending_expectation(db):
    uid = await make_user(db, "max", pending=(ISS, "max@example.com"))
    async with db() as session:
        assert (await oidc_links.unlink(session, uid)).ok
        await session.commit()
    assert link_columns(await row(db, uid)) == UNLINKED


async def test_a_unique_violation_in_an_admin_write_is_a_conflict_that_keeps_the_transaction(db):
    """The SAVEPOINT path: a write that the database refuses on a link
    UNIQUE reports False, and the caller's transaction (and its guard) is
    still usable afterwards."""
    from src.oauth.grants import lock_account_guard

    uid = await make_user(db, "max")
    await make_user(db, "holder", bound=(ISS, "sub-taken"))
    async with db() as session:
        await lock_account_guard(session)
        written = await oidc_links._write_guarded(session, uid, {
            "oidc_issuer": ISS, "oidc_subject": "sub-taken",
            "oidc_linked_at": text("now()"),
        })
        assert written is False
        assert (await session.execute(select(User.id).where(User.id == uid))).scalar() == uid
        held = (await session.execute(text(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
            "AND pid = pg_backend_pid()"))).scalar()
        assert held >= 1, "the guard must survive the rolled-back savepoint"
        await session.rollback()


async def test_permanent_delete_leaves_no_link_residue(db):
    uid = await make_user(db, "gone", bound=(ISS, "sub-x"), )
    other = await make_user(db, "next")
    async with db() as session:
        await session.execute(delete(User).where(User.id == uid))
        await session.commit()
    async with db() as session:
        res = await oidc_links.resolve(session, ISS, "sub-x", None, False)
        assert res.kind == "none"
        assert (await oidc_links.bind_subject(session, other, ISS, "sub-x")).ok
        await session.commit()
    assert (await row(db, other)).oidc_subject == "sub-x"
