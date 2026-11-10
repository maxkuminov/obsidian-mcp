"""Real-Postgres tests for `src/services/task_settings.py` (#428, design D14).

What needs a real database: the NULL-scope row and its NULLS NOT DISTINCT
key, owner scoping between users, the upsert under `FOR UPDATE` /
`ON CONFLICT`, a refused save writing nothing, and the cascade that removes a
user's row when the user is permanently deleted.

Skipped unless `PGVECTOR_TEST_ADMIN_URL` names a throwaway Postgres server
(`make test-integration` sets it).
"""
import asyncio

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import _harness
from src.models.db import TaskSettings, User
from src.services import task_settings

DIM = 64

pytestmark = [
    pytest.mark.asyncio(loop_scope="module"),
    _harness.requires_pgvector,
]


@pytest.fixture(scope="module")
def migrated_url():
    yield from _harness.throwaway_database("task_settings", DIM)


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def maker(migrated_url):
    engine = create_async_engine(migrated_url, pool_size=10, max_overflow=5)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


@pytest_asyncio.fixture(loop_scope="module")
async def db(maker):
    async def wipe():
        async with maker() as session:
            await session.execute(delete(TaskSettings))
            await session.execute(delete(User))
            await session.commit()

    await wipe()
    yield maker
    await wipe()


async def make_user(maker, username) -> int:
    async with maker() as session:
        user = User(username=username, password_hash="x", is_admin=False,
                    is_active=True, session_version=1)
        session.add(user)
        await session.commit()
        return user.id


async def save(maker, uid, form):
    async with maker() as session:
        errors = await task_settings.save(session, uid, form)
        await session.commit()
        return errors


async def effective(maker, uid):
    async with maker() as session:
        return await task_settings.effective(session, uid)


async def row_count(maker):
    async with maker() as session:
        return (await session.execute(select(func.count()).select_from(TaskSettings))).scalar_one()


async def test_no_row_means_plugin_defaults(db):
    eff = await effective(db, None)
    assert eff == task_settings.defaults()
    assert (eff.set_done_date, eff.set_cancelled_date, eff.set_created_date,
            eff.next_recurrence_below) == (True, True, False, False)
    assert eff.excluded_folders == () and eff.excluded_note_tags == ()


async def test_single_user_scope_is_the_null_row(db):
    assert await save(db, None, {
        "timezone": "Europe/Berlin",
        "excluded_folders": "Archive",
        "excluded_note_tags": "#Ticket",
        "set_done_date": "on",
        "set_cancelled_date": "on",
    }) == []
    async with db() as session:
        row = (await session.execute(select(TaskSettings))).scalar_one()
    assert row.user_id is None
    assert row.timezone == "Europe/Berlin"
    assert row.excluded_folders == ["Archive/"]
    assert row.excluded_note_tags == ["ticket"]
    # The two flags the form did not send stay NULL (follow the server default);
    # an absent key never stores False (S2 audit, #428).
    assert (row.set_done_date, row.set_cancelled_date, row.set_created_date,
            row.next_recurrence_below) == (True, True, None, None)

    eff = await effective(db, None)
    assert eff.timezone == "Europe/Berlin"
    assert (eff.set_created_date, eff.next_recurrence_below) == (False, False)
    assert eff.excluded_folders == ("Archive/",)

    # A second save updates the same row; it never inserts a second NULL row.
    assert await save(db, None, {"timezone": "", "set_created_date": "default"}) == []
    assert await row_count(db) == 1
    eff = await effective(db, None)
    assert eff.timezone == "UTC" and eff.configured_timezone is None
    assert eff.excluded_folders == ()
    assert eff.set_done_date is True  # absent: the stored True is kept
    assert eff.set_created_date is False  # "default": NULL, the env default
    async with db() as session:
        row = (await session.execute(select(TaskSettings))).scalar_one()
    assert (row.set_done_date, row.set_cancelled_date, row.set_created_date,
            row.next_recurrence_below) == (True, True, None, None)


async def test_a_save_without_flags_never_switches_a_default_off(db):
    """Regression: posting only `timezone` used to store False for all four
    flags, turning the done and cancelled dates off for the scope."""
    seven = await make_user(db, "seven")
    assert await save(db, seven, {"timezone": "Asia/Tokyo"}) == []
    async with db() as session:
        row = (await session.execute(select(TaskSettings))).scalar_one()
    assert (row.set_done_date, row.set_cancelled_date, row.set_created_date,
            row.next_recurrence_below) == (None, None, None, None)
    eff = await effective(db, seven)
    assert (eff.set_done_date, eff.set_cancelled_date) == (True, True)
    # An explicit value sticks; a later flag-less save leaves it alone.
    assert await save(db, seven, {"set_done_date": "off"}) == []
    assert await save(db, seven, {"timezone": "Europe/Berlin"}) == []
    eff = await effective(db, seven)
    assert eff.set_done_date is False and eff.timezone == "Europe/Berlin"


async def test_scopes_never_see_each_other(db):
    seven = await make_user(db, "seven")
    eight = await make_user(db, "eight")
    assert await save(db, seven, {"timezone": "Asia/Tokyo",
                                  "excluded_folders": "Archive/"}) == []
    assert await save(db, None, {"timezone": "Europe/Berlin"}) == []
    assert (await effective(db, seven)).timezone == "Asia/Tokyo"
    assert (await effective(db, None)).timezone == "Europe/Berlin"
    assert await effective(db, eight) == task_settings.defaults()

    assert await save(db, eight, {"excluded_note_tags": "ops"}) == []
    assert (await effective(db, seven)).excluded_note_tags == ()
    assert (await effective(db, seven)).excluded_folders == ("Archive/",)
    assert (await effective(db, eight)).excluded_note_tags == ("ops",)
    assert await row_count(db) == 3


async def test_a_refused_save_stores_nothing(db):
    seven = await make_user(db, "seven")
    assert await save(db, seven, {"timezone": "Asia/Tokyo"}) == []
    before = await effective(db, seven)
    errors = await save(db, seven, {"timezone": "Europe/Berlin",
                                    "excluded_folders": "../other"})
    assert errors and "Excluded folder" in errors[0]
    assert await effective(db, seven) == before
    # Nothing is inserted for a scope that had no row either.
    eight = await make_user(db, "eight")
    assert await save(db, eight, {"excluded_note_tags": "1bad"})
    assert await row_count(db) == 1


async def test_two_first_saves_of_one_scope_leave_one_row(db):
    seven = await make_user(db, "seven")
    results = await asyncio.gather(
        save(db, seven, {"timezone": "Asia/Tokyo"}),
        save(db, seven, {"timezone": "Europe/Berlin"}),
        save(db, None, {"timezone": "Asia/Tokyo"}),
        save(db, None, {"timezone": "Europe/Berlin"}),
    )
    assert results == [[], [], [], []]
    assert await row_count(db) == 2
    assert (await effective(db, seven)).timezone in ("Asia/Tokyo", "Europe/Berlin")


async def test_updated_at_moves_on_save(db):
    assert await save(db, None, {"timezone": "Asia/Tokyo"}) == []
    async with db() as session:
        first = (await session.execute(select(TaskSettings.updated_at))).scalar_one()
    await asyncio.sleep(0.01)
    assert await save(db, None, {"timezone": "Europe/Berlin"}) == []
    async with db() as session:
        second = (await session.execute(select(TaskSettings.updated_at))).scalar_one()
    assert second > first


async def test_permanently_deleting_a_user_removes_their_settings(db):
    seven = await make_user(db, "seven")
    eight = await make_user(db, "eight")
    assert await save(db, seven, {"timezone": "Asia/Tokyo"}) == []
    assert await save(db, eight, {"timezone": "Asia/Tokyo"}) == []
    assert await save(db, None, {"timezone": "Asia/Tokyo"}) == []
    async with db() as session:
        # The panel's permanent delete: `session.delete(target)`, the database
        # cascade does the rest (no ORM relationship loads the settings).
        await session.delete(await session.get(User, seven))
        await session.commit()
    async with db() as session:
        owners = (await session.execute(
            select(TaskSettings.user_id).order_by(TaskSettings.user_id.nulls_first())
        )).scalars().all()
    assert owners == [None, eight]
    assert await effective(db, seven) == task_settings.defaults()


async def test_an_unloadable_stored_zone_falls_back_without_refusing(db):
    async with db() as session:
        session.add(TaskSettings(user_id=None, timezone="Mars/Olympus"))
        await session.commit()
    eff = await effective(db, None)
    assert eff.timezone == "UTC" and eff.timezone_fallback
    assert eff.configured_timezone == "Mars/Olympus"
