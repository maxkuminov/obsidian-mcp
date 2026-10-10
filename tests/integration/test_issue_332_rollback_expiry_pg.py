"""Real-Postgres regression for #332: refusal branches that touch ORM state
after `session.rollback()`.

`AsyncSession.rollback()` expires every persistent instance in the identity
map, whatever `expire_on_commit` says. The panel's acting `User` is loaded by
`get_current_user` through the *same* request session the handler writes on,
so after a rollback any attribute read on it (`user.id`, `user.username`) is a
lazy load — which under asyncio raises `sqlalchemy.exc.MissingGreenlet` and
turns a refusal into a 500.

The hermetic tests could not see it: they pass a `SimpleNamespace` user and a
fake session whose `rollback()` only counts, and the #323 cap test passed a
`User` loaded in a *different* (closed) session, i.e. detached, which a
rollback does not expire. Every test here therefore loads the acting user into
the very session the handler is given, exactly as the dependency does.

Skipped unless `PGVECTOR_TEST_ADMIN_URL` names a throwaway Postgres server
(`make test-integration` sets it).
"""
import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from starlette.requests import Request

import _harness
import src.api.routes as api
import src.control_panel.routes as panel
import src.control_panel.users as panel_users
from src.auth.passwords import hash_password
from src.config import settings
from src.control_panel.flash import FLASH_SESSION_KEY
from src.limiter import limiter
from src.models.db import APIKey, QuotaCounter, UsageLog, User, UserSession
from src.services import rate_limits, security_events

DIM = 64

pytestmark = [
    pytest.mark.asyncio(loop_scope="module"),
    _harness.requires_pgvector,
]


@pytest.fixture(scope="module")
def migrated_url():
    yield from _harness.throwaway_database("rollback_332", DIM)


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def sessionmaker(migrated_url):
    engine = create_async_engine(migrated_url, pool_size=5, max_overflow=5)
    # The application's own sessionmaker settings (src/database.py).
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
            await session.execute(delete(UserSession))
            await session.execute(delete(User))
            await session.commit()

    monkeypatch.setattr(settings, "multi_user_mode", True)
    monkeypatch.setattr(settings, "key_creation_account_limit", 1)
    monkeypatch.setattr(settings, "key_creation_address_limit", None)
    monkeypatch.setattr(settings, "key_creation_window_seconds", 3600)
    monkeypatch.setattr(settings, "key_max_active_per_account", None)
    rate_limits.reset_key_creation_for_tests()
    limiter.reset()
    await wipe()
    yield sessionmaker
    await wipe()
    limiter.reset()
    rate_limits.reset_key_creation_for_tests()


@pytest.fixture
def events(monkeypatch):
    seen = []

    def _log(event, level, exc_info, fields):
        seen.append((event, fields))

    monkeypatch.setattr(security_events, "_log", _log)
    security_events.reset_state()
    with security_events.suppression_disabled():
        yield seen
    security_events.reset_state()


_addresses = iter(f"10.33.{n // 250}.{n % 250 + 1}" for n in range(10_000))


def _request(path, *, method="POST"):
    return Request({
        "type": "http", "http_version": "1.1", "method": method,
        "scheme": "http", "path": path, "raw_path": path.encode(),
        "headers": [(b"host", b"testserver")], "query_string": b"",
        "client": (next(_addresses), 1), "server": ("testserver", 80),
        "state": {}, "app": None, "session": {},
    })


async def _make_user(maker, username, *, is_admin=False, password="x"):
    async with maker() as session:
        user = User(
            username=username, password_hash=password, is_admin=is_admin,
            is_active=True,
        )
        session.add(user)
        await session.commit()
        return user.id


async def _acting(session, user_id):
    """The acting user as `get_current_user` hands it over: a persistent
    instance in the handler's own session."""
    user = (await session.execute(select(User).where(User.id == user_id))).scalar_one()
    assert user in session
    return user


async def _key_count(maker, user_id):
    async with maker() as session:
        return (
            await session.execute(
                select(func.count(APIKey.id)).where(APIKey.user_id == user_id)
            )
        ).scalar()


def _flash(request):
    raw = request.session.get(FLASH_SESSION_KEY)
    return raw.get("message") if isinstance(raw, dict) else None


# --------------------------------------------------------------------------
# key-creation budget refusal: the #332 report
# --------------------------------------------------------------------------


async def _json_create(maker, uid, name):
    """One `POST /api/keys`: a fresh request session, the user loaded into it."""
    async with maker() as session:
        user = await _acting(session, uid)
        return await api.create_key(
            request=_request("/api/keys"),
            req=api.CreateKeyRequest(name=name, daily_request_limit=10),
            session=session, user=user,
        )


async def _form_create(maker, uid, name):
    """One `POST /admin/keys/create`. Returns `(response, request)`."""
    async with maker() as session:
        user = await _acting(session, uid)
        request = _request("/admin/keys/create")
        response = await panel.create_key_form(
            request=request, name=name, permission="read",
            daily_request_limit="10", unlimited="", session=session, user=user,
        )
        return response, request


async def test_json_create_budget_refusal_is_a_429_with_retry_after(db, events):
    uid = await _make_user(db, "alice")
    first = await _json_create(db, uid, "one")
    assert first.key.startswith("omcp_")

    with pytest.raises(HTTPException) as exc:
        await _json_create(db, uid, "two")

    assert exc.value.status_code == 429
    assert int(exc.value.headers["Retry-After"]) > 0
    throttled = [f for e, f in events if e == "key_creation_throttled"]
    assert len(throttled) == 1
    assert throttled[0]["actor_user_id"] == uid
    assert throttled[0]["actor_username"] == "alice"
    assert throttled[0]["reason"] == "account_budget"
    assert await _key_count(db, uid) == 1


async def test_panel_create_budget_refusal_flashes_and_redirects(db, events):
    uid = await _make_user(db, "bob")
    first, ok_request = await _form_create(db, uid, "one")
    assert first.status_code == 303
    assert ok_request.session.get("flash_new_key")

    response, refused_request = await _form_create(db, uid, "two")

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/keys"
    assert "Too many keys created recently" in refused_request.session["flash_key_error"]
    assert "flash_new_key" not in refused_request.session
    throttled = [f for e, f in events if e == "key_creation_throttled"]
    assert len(throttled) == 1
    assert throttled[0]["actor_user_id"] == uid
    assert throttled[0]["actor_username"] == "bob"
    assert await _key_count(db, uid) == 1


async def test_json_and_form_share_the_budget_and_both_refuse_cleanly(db, events):
    """One allowance across both surfaces; the refusal on each is answered."""
    uid = await _make_user(db, "bea")
    await _form_create(db, uid, "one")
    with pytest.raises(HTTPException) as exc:
        await _json_create(db, uid, "two")
    assert exc.value.status_code == 429
    response, request = await _form_create(db, uid, "three")
    assert response.status_code == 303 and request.session.get("flash_key_error")
    assert len([1 for e, _ in events if e == "key_creation_throttled"]) == 2
    assert await _key_count(db, uid) == 1


async def test_cap_refusal_on_both_routes_is_answered_not_a_500(db, monkeypatch):
    """The cap branch also rolls back; it must not touch the expired user."""
    monkeypatch.setattr(settings, "key_creation_account_limit", None)
    monkeypatch.setattr(settings, "key_max_active_per_account", 0)
    uid = await _make_user(db, "carol")

    with pytest.raises(HTTPException) as exc:
        await _json_create(db, uid, "x")
    assert exc.value.status_code == 409

    response, request = await _form_create(db, uid, "x")
    assert response.status_code == 303
    assert request.session.get("flash_key_error")
    assert await _key_count(db, uid) == 0


# --------------------------------------------------------------------------
# the same class elsewhere in src/control_panel
# --------------------------------------------------------------------------


async def test_password_change_wrong_current_password_is_a_refusal(db, events):
    uid = await _make_user(db, "dave", password=hash_password("correct horse battery"))

    async with db() as session:
        user = await _acting(session, uid)
        request = _request("/admin/account/password")
        response = await panel.change_password(
            request=request,
            current_password="not the password",
            new_password="a brand new passphrase",
            new_password_confirm="a brand new passphrase",
            session=session, user=user,
        )

    assert response.status_code == 303
    assert _flash(request)
    refused = [f for e, f in events if e == "panel_password_change_refused"]
    assert [f["reason"] for f in refused] == ["wrong_current_password"]
    assert refused[0]["user_id"] == uid


async def test_password_change_same_as_current_is_a_refusal(db, events):
    uid = await _make_user(db, "erin", password=hash_password("correct horse battery"))

    async with db() as session:
        user = await _acting(session, uid)
        request = _request("/admin/account/password")
        response = await panel.change_password(
            request=request,
            current_password="correct horse battery",
            new_password="correct horse battery",
            new_password_confirm="correct horse battery",
            session=session, user=user,
        )

    assert response.status_code == 303
    refused = [f for e, f in events if e == "panel_password_change_refused"]
    assert [f["reason"] for f in refused] == ["same_as_current"]
    assert refused[0]["user_id"] == uid


async def test_admin_demoted_while_queued_is_refused_and_recorded(db, events):
    """`_actor_still_privileged` fails → rollback → `actor_revoked` record."""
    admin_id = await _make_user(db, "frank", is_admin=True)
    target_id = await _make_user(db, "gina")

    async with db() as session:
        user = await _acting(session, admin_id)
        # Another admin's demotion commits while this request is queued.
        async with db() as other:
            await other.execute(
                update(User).where(User.id == admin_id).values(is_admin=False)
            )
            await other.commit()

        request = _request(f"/admin/users/{target_id}/delete")
        response = await panel_users.delete_user(
            user_id=target_id, request=request, session=session, user=user,
        )

    assert response.status_code == 303
    assert _flash(request)
    revoked = [
        f for e, f in events
        if e == "panel_forbidden" and f.get("reason") == "actor_revoked"
    ]
    assert len(revoked) == 1
    assert revoked[0]["actor_user_id"] == admin_id
    assert revoked[0]["actor_username"] == "frank"


async def test_health_strip_failure_degrades_without_touching_the_expired_user(
    db, events, monkeypatch
):
    uid = await _make_user(db, "hal", is_admin=True)

    async def broken(session, user):
        await session.execute(select(func.count(User.id)))
        raise RuntimeError("strip read failed")

    monkeypatch.setattr(panel, "_health_strip", broken)

    async with db() as session:
        user = await _acting(session, uid)
        strip = await panel._health_strip_or_degraded(session, user)
        # The dashboard's next step reads the user for the page chrome.
        assert user.username == "hal" and user.is_admin is True

    assert strip["unavailable"] is True and strip["show_ops"] is True
    failed = [f for e, f in events if e == "panel_health_strip_failed"]
    assert failed
