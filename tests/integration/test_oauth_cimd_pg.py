"""Client ID Metadata Documents against real Postgres (#357).

The database half of `tests/test_oauth_cimd.py`:

* an approval upserts the synthetic per-(URL, user) row and its use marker in
  the code's transaction (`created` → `unchanged` → `refreshed`), and a deny
  writes nothing;
* two users connecting the same client get two rows, and neither is refused
  as cross-user;
* a changed authorization projection refuses the approval and writes nothing;
* the code exchange, the refresh and `/revoke` match the caller's URL, never
  the internal id; the internal id is refused **after** the credential and
  the unchanged replay processing — a replayed refresh token under
  `client_id=cimd_nonexistent` still revokes its family (Codex round 3);
* the CIMD sweep branch: an unreferenced old row goes, a referenced one
  stays, `null` disables it, and the DCR sweep never selects a CIMD row.

Skipped unless `PGVECTOR_TEST_ADMIN_URL` names a throwaway Postgres server
(`make test-integration` sets it).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import pytest
import pytest_asyncio
from sqlalchemy import delete as sa_delete
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from starlette.requests import Request

import _harness
from src.models.db import OAuthClient, OAuthCode, OAuthToken, UsageLog, User
from src.oauth import cimd, grants
from src.oauth import routes as oauth
from src.services import indexer

pytestmark = [pytest.mark.asyncio(loop_scope="module"), _harness.requires_pgvector]

URL = "https://claude.ai/oauth/claude-code-client-metadata"
DOC = {
    "client_id": URL,
    "client_name": "Claude Code",
    "redirect_uris": ["http://localhost/callback", "http://127.0.0.1/callback"],
    "grant_types": ["authorization_code", "refresh_token"],
    "response_types": ["code"],
    "token_endpoint_auth_method": "none",
}
REDIRECT = "http://localhost:3118/callback"
VERIFIER = "v" * 64


@pytest.fixture(scope="module")
def migrated_url():
    yield from _harness.throwaway_database("oauth_cimd", 64)


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def sessionmaker(migrated_url):
    engine = create_async_engine(migrated_url)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


@pytest_asyncio.fixture(loop_scope="module")
async def db(sessionmaker, monkeypatch):
    async def wipe():
        async with sessionmaker() as session:
            await session.execute(sa_delete(UsageLog))
            await session.execute(sa_delete(OAuthToken))
            await session.execute(sa_delete(OAuthCode))
            await session.execute(sa_delete(OAuthClient))
            await session.execute(sa_delete(User))
            await session.commit()

    await wipe()
    monkeypatch.setattr(oauth, "async_session", sessionmaker)
    monkeypatch.setattr(indexer, "async_session", sessionmaker)
    monkeypatch.setattr(oauth.settings, "multi_user_mode", False, raising=False)
    monkeypatch.setattr(oauth.settings, "oauth_cimd_enabled", True, raising=False)
    monkeypatch.setattr(oauth.settings, "oauth_client_unused_expiry_days", 30, raising=False)
    monkeypatch.setattr(grants.settings, "oauth_grant_absolute_lifetime_days", 90, raising=False)
    cimd.reset_state_for_tests()
    yield sessionmaker
    cimd.reset_state_for_tests()
    await wipe()


def request() -> Request:
    return Request({
        "type": "http", "http_version": "1.1", "method": "POST", "scheme": "https",
        "path": "/authorize", "raw_path": b"/authorize", "root_path": "",
        "query_string": b"", "headers": [(b"host", b"testserver")],
        "client": ("198.51.100.7", 51234), "server": ("testserver", 443), "state": {},
    })


def projection(document=None):
    projection = cimd.validate_document(URL, document or DOC)
    cimd._cache_put(URL, projection, None)
    return projection


async def approve(document=None, *, bound=None, action="approve", redirect=REDIRECT):
    p = projection(document)
    return await oauth._authorize_post_cimd(
        request(),
        action=action,
        client_id=URL,
        redirect_uri=redirect,
        code_challenge=oauth._base64url_sha256(VERIFIER),
        code_challenge_method="S256",
        scope="read offline_access",
        client_state="st",
        bound_hash=bound if bound is not None else p.authorization_hash,
    )


def code_of(response) -> str:
    query = parse_qs(urlsplit(response.headers["location"]).query)
    assert query["iss"] == [oauth.settings.base_url]
    return query["code"][0]


async def exchange(code, client_id=URL):
    form = {"code": code, "code_verifier": VERIFIER, "redirect_uri": REDIRECT}
    if client_id:
        form["client_id"] = client_id
    return await oauth._handle_auth_code(form)


async def refresh(token, client_id=URL):
    form = {"refresh_token": token}
    if client_id:
        form["client_id"] = client_id
    return await oauth._handle_refresh(form)


def body(response):
    return json.loads(response.body)


async def rows(sessionmaker):
    async with sessionmaker() as session:
        return list((await session.execute(select(OAuthClient))).scalars().all())


async def test_an_approval_creates_the_row_and_a_repeat_reuses_it(db):
    first = await approve()
    assert first.status_code == 302
    (row,) = await rows(db)
    assert row.client_id == cimd.cimd_row_id(URL, None)
    assert row.client_id_metadata_url == URL
    assert row.token_endpoint_auth_method == "none" and row.client_secret_hash is None
    assert row.last_used_at is not None
    await approve()
    renamed = dict(DOC, client_name="Claude Code (renamed)")
    await approve(renamed)
    (row,) = await rows(db)
    assert row.client_name == "Claude Code (renamed)"


async def test_a_deny_writes_nothing(db):
    response = await approve(action="deny")
    assert response.status_code == 302
    location = response.headers["location"]
    assert "error=access_denied" in location and "iss=" in location
    assert await rows(db) == []


async def test_a_changed_projection_refuses_and_writes_nothing(db):
    response = await approve(bound="0" * 64)
    assert response.status_code == 400 and body(response) == {"error": "invalid_client"}
    assert await rows(db) == []


async def test_two_users_get_two_rows_and_no_cross_user_refusal(db, monkeypatch):
    async with db() as session:
        alice = User(username="alice", password_hash="x")
        bob = User(username="bob", password_hash="x")
        session.add_all([alice, bob])
        await session.commit()
    monkeypatch.setattr(oauth.settings, "multi_user_mode", True, raising=False)
    for user in (alice, bob):
        async def who(*_a, _user=user, **_kw):
            return _user

        monkeypatch.setattr(oauth, "get_active_session_user", who)
        response = await approve()
        assert response.status_code == 302, body(response)
    found = await rows(db)
    assert sorted(r.user_id for r in found) == sorted([alice.id, bob.id])
    assert len({r.client_id for r in found}) == 2


async def test_the_full_grant_flow_matches_the_url_never_the_internal_id(db):
    code = code_of(await approve())
    mismatch = await exchange(code, client_id="https://other.example.org/doc")
    assert mismatch.status_code == 400 and body(mismatch) == {"error": "invalid_grant"}
    internal = await exchange(code, client_id=cimd.cimd_row_id(URL, None))
    assert internal.status_code == 401 and body(internal) == {"error": "invalid_client"}
    issued = await exchange(code)
    assert issued.status_code == 200, body(issued)
    tokens = body(issued)

    rotated = await refresh(tokens["refresh_token"])
    assert rotated.status_code == 200, body(rotated)
    live_refresh = body(rotated)["refresh_token"]
    assert (await refresh(live_refresh, client_id=cimd.cimd_row_id(URL, None))).status_code == 401


async def test_a_replay_under_a_synthetic_id_still_revokes_the_family(db):
    """Codex round 3: credential-first replay processing at /token."""
    tokens = body(await exchange(code_of(await approve())))
    rotated = await refresh(tokens["refresh_token"])
    assert rotated.status_code == 200
    replay = await refresh(tokens["refresh_token"], client_id="cimd_nonexistent")
    assert replay.status_code == 400 and body(replay) == {"error": "invalid_grant"}
    async with db() as session:
        live = (
            await session.execute(select(OAuthToken).where(OAuthToken.revoked.is_(False)))
        ).scalars().all()
    assert live == []


async def _revoke(token, client_id):
    form = {"token": token}
    if client_id:
        form["client_id"] = client_id

    class _Req:
        url = type("U", (), {"path": "/revoke"})()
        client = None
        headers = {}
        state = type("S", (), {})()

        async def form(self):
            return form

    return await oauth.revoke_token.__wrapped__(_Req())


async def test_revocation_matches_the_url_and_conceals_the_internal_id(db):
    tokens = body(await exchange(code_of(await approve())))
    noop = await _revoke(tokens["access_token"], cimd.cimd_row_id(URL, None))
    assert noop.status_code == 200 and noop.body == b"{}"
    async with db() as session:
        assert all(
            not t.revoked for t in (await session.execute(select(OAuthToken))).scalars().all()
        )
    done = await _revoke(tokens["access_token"], URL)
    assert done.status_code == 200
    async with db() as session:
        assert all(t.revoked for t in (await session.execute(select(OAuthToken))).scalars().all())


async def test_the_cimd_sweep_branch(db, monkeypatch):
    await approve()
    old = datetime.now(timezone.utc) - timedelta(days=31)
    async with db() as session:
        await session.execute(sa_delete(OAuthCode))
        await session.execute(update(OAuthClient).values(last_used_at=old, created_at=old))
        await session.commit()
    # The DCR sweep never selects a CIMD row, even with a NULL marker.
    assert await indexer._expire_unused_oauth_clients() == 0

    monkeypatch.setattr(oauth.settings, "oauth_client_unused_expiry_days", None, raising=False)
    assert await indexer._expire_unreferenced_cimd_clients() == 0
    monkeypatch.setattr(oauth.settings, "oauth_client_unused_expiry_days", 30, raising=False)
    assert await indexer._expire_unreferenced_cimd_clients() == 1
    assert await rows(db) == []


async def test_a_referenced_cimd_row_is_never_swept(db):
    tokens = body(await exchange(code_of(await approve())))
    assert tokens["access_token"]
    old = datetime.now(timezone.utc) - timedelta(days=400)
    async with db() as session:
        await session.execute(sa_delete(OAuthCode))
        await session.execute(update(OAuthToken).values(revoked=True))
        await session.execute(update(OAuthClient).values(last_used_at=old))
        await session.commit()
    assert await indexer._expire_unreferenced_cimd_clients() == 0
    assert len(await rows(db)) == 1


# ── verifier round (a1a4bbb): /token records, replay alarms, terminal paths ─


@pytest.fixture
def emitted(monkeypatch):
    from src.services import security_events

    records: list[tuple[str, dict]] = []
    real = security_events.emit

    def capture(event, **kwargs):
        records.append((event, kwargs))
        return real(event, **kwargs)

    monkeypatch.setattr(security_events, "emit", capture)
    return records


def classified_request(resource=None) -> Request:
    from src.oauth import resource as oauth_resource

    req = request()
    oauth_resource.begin(req, [resource] if resource else [])
    return req


def named(emitted, event):
    return [kw for name, kw in emitted if name == event]


async def test_a_spent_code_under_a_synthetic_id_revokes_nothing(db, emitted):
    code = code_of(await approve())
    assert (await exchange(code)).status_code == 200
    replay = await exchange(code, client_id="cimd_" + "f" * 59)
    assert replay.status_code == 400 and body(replay) == {"error": "invalid_grant"}
    async with db() as session:
        tokens = (await session.execute(select(OAuthToken))).scalars().all()
    assert tokens and not any(t.revoked for t in tokens)
    reasons = [kw["reason"] for kw in named(emitted, "oauth_token_refused")]
    assert reasons[-1] == "invalid_grant.spent_code_internal_id_submitted"


async def test_the_synthetic_id_replay_emits_the_reuse_alarm(db, emitted):
    tokens = body(await exchange(code_of(await approve())))
    assert (await refresh(tokens["refresh_token"])).status_code == 200
    await refresh(tokens["refresh_token"], client_id="cimd_nonexistent")
    (alarm,) = named(emitted, "oauth_refresh_reuse_detected")
    assert alarm["revoked_tokens"] >= 1


async def test_https_mismatch_at_token_and_revoke_logs_host_and_ref_only(db, emitted):
    other = "https://a.example/x"
    code = code_of(await approve())
    assert (await exchange(code, client_id=other)).status_code == 400
    tokens = body(await exchange(code))
    assert (await refresh(tokens["refresh_token"], client_id=other)).status_code == 400
    assert (await _revoke(tokens["access_token"], other)).status_code == 200
    records = named(emitted, "oauth_token_refused")[-2:] + named(emitted, "oauth_revoke_noop")[-1:]
    assert len(records) == 3
    for kw in records:
        assert kw["cimd_host"] == "a.example"
        assert len(kw["cimd_ref"]) == 16
        assert kw.get("client_id_submitted") is None
    assert other not in json.dumps(emitted, default=str)


async def test_the_token_endpoint_and_refresh_never_fetch(db, monkeypatch):
    """Task 6.5: no document fetch on /token or refresh, whatever the cache holds."""
    code = code_of(await approve())
    cimd.reset_state_for_tests()  # nothing cached: a fetch would be needed

    async def no_fetch(*_a, **_kw):
        raise AssertionError("a document fetch on the token path")

    monkeypatch.setattr(cimd, "resolve", no_fetch)
    monkeypatch.setattr(cimd, "_fetch", no_fetch)
    tokens = body(await exchange(code))
    assert (await refresh(tokens["refresh_token"])).status_code == 200


async def test_every_token_and_revoke_record_carries_the_cimd_fields(db, emitted):
    """Codex impl R1: success, refresh, replay and revoke records, both grants."""
    ref = cimd.ref_for_log(URL)
    code = code_of(await approve())
    tokens = body(await exchange(code))
    rotated = body(await refresh(tokens["refresh_token"]))
    await exchange(code)  # a fully revalidated code replay
    tokens2 = body(await exchange(code_of(await approve())))
    assert (await refresh(tokens2["refresh_token"])).status_code == 200
    await refresh(tokens2["refresh_token"])  # refresh reuse
    fresh = body(await exchange(code_of(await approve())))
    assert (await _revoke(fresh["access_token"], URL)).status_code == 200
    assert rotated["access_token"]
    for event in (
        "oauth_token_issued",
        "oauth_token_refreshed",
        "oauth_code_replay_detected",
        "oauth_refresh_reuse_detected",
        "oauth_grant_revoked",
    ):
        records = named(emitted, event)
        assert records, event
        for kw in records:
            assert kw["cimd_host"] == "claude.ai", event
            assert kw["cimd_ref"] == ref, event
    assert URL not in json.dumps(emitted, default=str)


async def test_referenced_rows_cannot_starve_the_cimd_sweep(db):
    """Codex impl R1: 200 referenced rows ahead of one eligible row."""
    old = datetime.now(timezone.utc) - timedelta(days=60)
    far = datetime.now(timezone.utc) + timedelta(days=1)
    async with db() as session:
        for i in range(201):
            cid = "cimd_" + format(i, "059x")
            session.add(OAuthClient(
                client_id=cid, client_secret_hash=None, token_endpoint_auth_method="none",
                client_name="x", redirect_uris=[REDIRECT], scope="read",
                client_id_metadata_url=f"https://h{i}.example.org/doc", last_used_at=old,
            ))
        await session.flush()
        for i in range(200):  # the first 200 (lowest ids) are referenced
            session.add(OAuthToken(
                token_hash=format(i, "064x"), token_type="access",
                client_id="cimd_" + format(i, "059x"), scope="read", grant_id=f"g{i}",
                grant_issued_at=old, expires_at=far, revoked=True,
            ))
        await session.commit()
    assert await indexer._expire_unreferenced_cimd_clients() == 1
    async with db() as session:
        remaining = (await session.execute(select(OAuthClient.client_id))).scalars().all()
    assert "cimd_" + format(200, "059x") not in remaining
    assert len(remaining) == 200


async def test_token_terminal_paths_carry_the_resource_class(db, emitted, monkeypatch):
    base = oauth.settings.base_url.rstrip("/")
    # issued
    req = classified_request(f"{base}/mcp")
    code = code_of(await approve())
    issued = await oauth._handle_auth_code(
        {"code": code, "code_verifier": VERIFIER, "redirect_uri": REDIRECT, "client_id": URL}, req
    )
    assert issued.status_code == 200
    assert named(emitted, "oauth_token_issued")[-1]["resource_class"] == "accepted_1"
    tokens = body(issued)

    # code replay detected
    req = classified_request("https://other.example/")
    await oauth._handle_auth_code(
        {"code": code, "code_verifier": VERIFIER, "redirect_uri": REDIRECT, "client_id": URL}, req
    )
    (replay,) = named(emitted, "oauth_code_replay_detected")
    assert replay["resource_class"] == "unexpected" and replay["resource_host"] == "other.example"

    # refresh reuse detected
    tokens = body(await exchange(code_of(await approve())))
    assert (await refresh(tokens["refresh_token"])).status_code == 200
    req = classified_request()
    await oauth._handle_refresh({"refresh_token": tokens["refresh_token"], "client_id": URL}, req)
    assert named(emitted, "oauth_refresh_reuse_detected")[-1]["resource_class"] == "absent"

    # rotation failed
    fresh = body(await exchange(code_of(await approve())))

    def boom(*_a, **_kw):
        raise RuntimeError("injected")

    real_clamp = oauth.clamp_token_expiry
    monkeypatch.setattr(oauth, "clamp_token_expiry", boom)
    req = classified_request()
    failed = await oauth._handle_refresh({"refresh_token": fresh["refresh_token"]}, req)
    assert failed.status_code == 500
    assert named(emitted, "oauth_token_rotation_failed")[-1]["resource_class"] == "absent"
    monkeypatch.setattr(oauth, "clamp_token_expiry", real_clamp)

    # client vanished at the code exchange
    code = code_of(await approve())

    async def gone(*_a, **_kw):
        return False

    monkeypatch.setattr(oauth, "_stamp_client_use", gone)
    req = classified_request()
    vanished = await oauth._handle_auth_code(
        {"code": code, "code_verifier": VERIFIER, "redirect_uri": REDIRECT}, req
    )
    assert vanished.status_code == 400
    last = named(emitted, "oauth_token_refused")[-1]
    assert last["reason"] == "invalid_client.client_vanished"
    assert last["resource_class"] == "absent"
