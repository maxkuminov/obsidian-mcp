"""Real-Postgres gate for the non-admin active-key cap (#323, design D4).

The cap's exactness under concurrency is a fact about PostgreSQL's row lock —
`SELECT … FOR UPDATE` on the owning `users` row serialising the count-then-
insert of two concurrent creates — so it has no fake-session equivalent.

To make "concurrent" mean something, the first create is held *after* it has
counted and while it still holds the lock; the second is started meanwhile.
Without the lock, both would count `cap - 1` and both would insert.

Skipped unless `PGVECTOR_TEST_ADMIN_URL` names a throwaway Postgres server
(`make test-integration` sets it).
"""
import asyncio

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from starlette.requests import Request

import _harness
import src.api.routes as api
from src.config import settings
from src.models.db import APIKey, QuotaCounter, UsageLog, User
from src.services import api_keys as key_issuance

DIM = 64

pytestmark = [
    pytest.mark.asyncio(loop_scope="module"),
    _harness.requires_pgvector,
]


@pytest.fixture(scope="module")
def migrated_url():
    yield from _harness.throwaway_database("keycap_323", DIM)


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def sessionmaker(migrated_url):
    engine = create_async_engine(migrated_url, pool_size=10, max_overflow=5)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest_asyncio.fixture(loop_scope="module")
async def db(sessionmaker, monkeypatch):
    async def wipe():
        async with sessionmaker() as session:
            await session.execute(delete(QuotaCounter))
            await session.execute(delete(UsageLog))
            await session.execute(delete(APIKey))
            await session.execute(delete(User))
            await session.commit()

    # The cap is under test, not the velocity budget: keep it out of the way.
    monkeypatch.setattr(settings, "key_creation_account_limit", None)
    monkeypatch.setattr(settings, "key_creation_address_limit", None)
    monkeypatch.setattr(settings, "key_max_active_per_account", 3)
    await wipe()
    yield sessionmaker
    await wipe()


_addresses = iter(f"10.9.{n // 250}.{n % 250 + 1}" for n in range(10_000))


def _request():
    """A real starlette request with a distinct client address, so the JSON
    route's own 5/min slowapi limit never becomes the thing that refuses."""
    return Request({
        "type": "http", "method": "POST", "path": "/api/keys", "headers": [],
        "query_string": b"", "client": (next(_addresses), 1), "state": {},
        "app": None,
    })


async def _make_user(maker, username, *, is_admin=False):
    async with maker() as session:
        user = User(username=username, password_hash="x", is_admin=is_admin, is_active=True)
        session.add(user)
        await session.commit()
        return user


async def _make_keys(maker, user_id, n, *, active=True):
    async with maker() as session:
        for i in range(n):
            session.add(APIKey(
                name=f"k{i}", key_hash=f"hash-{user_id}-{active}-{i}",
                key_prefix=f"omcp_{i:06d}", permission="read",
                is_active=active, user_id=user_id, daily_request_limit=100,
            ))
        await session.commit()


async def _count(maker, user_id, *, active_only=True):
    async with maker() as session:
        q = select(func.count(APIKey.id)).where(APIKey.user_id == user_id)
        if active_only:
            q = q.where(APIKey.is_active.is_(True))
        return (await session.execute(q)).scalar()


async def _create(maker, user):
    async with maker() as session:
        return await api.create_key(
            request=_request(),
            req=api.CreateKeyRequest(name="new", daily_request_limit=10),
            session=session,
            user=user,
        )


async def test_two_concurrent_creates_at_cap_minus_one_create_exactly_one(db, monkeypatch):
    user = await _make_user(db, "alice")
    await _make_keys(db, user.id, 2)  # cap is 3

    real = key_issuance.assert_active_key_capacity
    first_counted = asyncio.Event()

    async def held(session, u):
        result = await real(session, u)
        if not first_counted.is_set():
            # The first caller has counted and still holds the row lock; hold
            # it long enough that the second is certainly waiting on it.
            first_counted.set()
            await asyncio.sleep(0.5)
        return result

    monkeypatch.setattr(key_issuance, "assert_active_key_capacity", held)

    async def second():
        await first_counted.wait()
        return await _create(db, user)

    results = await asyncio.gather(_create(db, user), second(), return_exceptions=True)

    created = [r for r in results if not isinstance(r, BaseException)]
    refused = [r for r in results if isinstance(r, HTTPException)]
    assert len(created) == 1, results
    assert len(refused) == 1 and refused[0].status_code == 409, results
    assert "Revoke" in refused[0].detail
    assert await _count(db, user.id) == 3


async def test_revoked_keys_do_not_count(db):
    user = await _make_user(db, "bob")
    await _make_keys(db, user.id, 2)
    await _make_keys(db, user.id, 5, active=False)

    await _create(db, user)
    assert await _count(db, user.id) == 3

    with pytest.raises(HTTPException) as exc:
        await _create(db, user)
    assert exc.value.status_code == 409
    assert await _count(db, user.id) == 3


async def test_an_admin_is_not_capped(db):
    admin = await _make_user(db, "root", is_admin=True)
    await _make_keys(db, admin.id, 5)  # already over the cap

    await _create(db, admin)
    assert await _count(db, admin.id) == 6


async def test_an_account_over_the_cap_is_grandfathered_not_revoked(db):
    user = await _make_user(db, "carol")
    await _make_keys(db, user.id, 5)

    with pytest.raises(HTTPException) as exc:
        await _create(db, user)
    assert exc.value.status_code == 409
    assert await _count(db, user.id) == 5, "the cap revoked or altered existing keys"
