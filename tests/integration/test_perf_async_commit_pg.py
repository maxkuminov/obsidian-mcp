"""Real-Postgres gate for the request path's asynchronous commits
(performance-2026-09, #279, tasks 1.7/1.8).

What only a real server can show:

* **`SET LOCAL` does not leak through the pool.** After each of the three
  asynchronously committed writes — the `last_used_at` stamp, the usage row,
  the quota admission and its prune — the *same* pooled connection, checked
  out again, reports `synchronous_commit = on`. The engine here has exactly one
  connection, and the backend pid is compared, so "same connection" is a fact
  and not a hope.
* **The setting is scoped to the write's own transaction**: it follows the
  transaction's BEGIN and precedes the write, with no COMMIT in between.
* **The quota boundary is still exact** with async commit: an asynchronously
  committed increment is visible at commit, so exactly N of more than N
  concurrent admissions succeed.
* **The folded credential read still revokes on the next request** (#66):
  deactivating the user, clearing `vault_path`, deleting the `users` row,
  revoking the key and revoking the OAuth token each take effect on the very
  next request, through the real outer join.
* **Grant and revoke paths stay synchronous**: OAuth code exchange, refresh
  rotation, token revocation, grant-family revocation and transfer-token mint
  issue no `synchronous_commit` statement at all (a cursor-level spy).

Skipped unless `PGVECTOR_TEST_ADMIN_URL` names a throwaway Postgres server; run
it with `make test-integration`.
"""
import asyncio
import datetime as dt
import secrets

import pytest
import pytest_asyncio
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import _harness
import src.mcp_server.auth as mcp_auth
import src.mcp_server.tools as tools
import src.services.quotas as quotas
import src.services.vault as vault
from src.auth.session import current_vault_root
from src.models.db import APIKey, OAuthClient, OAuthCode, OAuthToken, User
from src.oauth import routes as oauth

DIM = 64
SYNC_SQL = "synchronous_commit"
REDIRECT_URI = "https://client.example.com/callback"

pytestmark = [
    pytest.mark.asyncio(loop_scope="module"),
    _harness.requires_pgvector,
]


@pytest.fixture(scope="module")
def migrated_url():
    yield from _harness.throwaway_database("perf_async_commit", DIM)


class _Spy:
    """Every cursor statement and transaction boundary on an engine, in order."""

    def __init__(self, engine):
        self.log: list[tuple[str, str]] = []
        self.rowcounts: list[tuple[str, int]] = []
        sync = engine.sync_engine
        event.listen(sync, "before_cursor_execute", self._before)
        event.listen(sync, "after_cursor_execute", self._after)
        event.listen(sync, "begin", lambda conn: self.log.append(("BEGIN", "")))
        event.listen(sync, "commit", lambda conn: self.log.append(("COMMIT", "")))
        event.listen(sync, "rollback", lambda conn: self.log.append(("ROLLBACK", "")))

    def _before(self, conn, cursor, statement, parameters, context, executemany):
        self.log.append(("SQL", statement))

    def _after(self, conn, cursor, statement, parameters, context, executemany):
        self.rowcounts.append((statement, cursor.rowcount))

    def clear(self):
        self.log.clear()
        self.rowcounts.clear()

    def sql(self):
        return [s for kind, s in self.log if kind == "SQL"]

    def sync_statements(self):
        return [s for s in self.sql() if SYNC_SQL in s]


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def single(migrated_url):
    """One pooled connection, so every checkout is the same backend."""
    engine = create_async_engine(migrated_url, pool_size=1, max_overflow=0)
    spy = _Spy(engine)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield maker, spy
    await engine.dispose()


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def wide(migrated_url):
    """A pool wide enough for genuinely concurrent callers."""
    engine = create_async_engine(migrated_url, pool_size=30, max_overflow=10)
    spy = _Spy(engine)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield maker, spy
    await engine.dispose()


async def _wipe(maker):
    async with maker() as session:
        for table in (
            "usage_logs", "quota_counters", "transfer_tokens", "oauth_tokens",
            "oauth_codes", "oauth_clients", "api_keys", "users",
        ):
            await session.execute(text(f"DELETE FROM {table}"))
        await session.commit()


@pytest_asyncio.fixture(loop_scope="module")
async def db(single, monkeypatch):
    maker, spy = single
    await _wipe(maker)
    for module in (mcp_auth, tools, quotas, oauth):
        monkeypatch.setattr(module, "async_session", maker)
    monkeypatch.setattr(mcp_auth.settings, "multi_user_mode", True, raising=False)
    saved = dict(vault._user_vault_cache)
    spy.clear()
    yield maker, spy
    vault._user_vault_cache.clear()
    vault._user_vault_cache.update(saved)
    await _wipe(maker)


async def _backend(maker):
    """`(pid, synchronous_commit)` on a fresh checkout."""
    async with maker() as session:
        pid = (await session.execute(text("SELECT pg_backend_pid()"))).scalar()
        setting = (await session.execute(text("SHOW synchronous_commit"))).scalar()
        return pid, setting


def _assert_scoped(spy, write_prefix):
    """`SET LOCAL` follows its transaction's BEGIN and precedes the write,
    with no transaction boundary between them."""
    log = spy.log
    idx = next(
        i for i, (k, s) in enumerate(log)
        if k == "SQL" and s.lstrip().upper().startswith(write_prefix)
    )
    # Walk back to the SET LOCAL; nothing between may end a transaction.
    j = idx - 1
    while j >= 0 and not (log[j][0] == "SQL" and SYNC_SQL in log[j][1]):
        assert log[j][0] not in ("COMMIT", "ROLLBACK"), log[j - 3: idx + 1]
        j -= 1
    assert j >= 0, f"no SET LOCAL before {write_prefix}: {log}"
    assert log[j][1].strip() == "SET LOCAL synchronous_commit = off"
    # And the SET LOCAL is inside a transaction that began before it.
    k = j - 1
    while k >= 0 and log[k][0] != "BEGIN":
        assert log[k][0] not in ("COMMIT", "ROLLBACK")
        k -= 1
    assert k >= 0, "the SET LOCAL ran outside an explicit transaction"


# ── seeding ────────────────────────────────────────────────────────────────


async def make_user(maker, username="alice", vault_path="/vaults/alice"):
    async with maker() as session:
        user = User(
            username=username, password_hash="x", is_admin=False,
            is_active=True, vault_path=vault_path,
        )
        session.add(user)
        await session.commit()
        return user.id


async def make_key(maker, user_id, *, last_used_at=None, limit=None, name="agent"):
    raw = "omcp_" + secrets.token_hex(16)
    async with maker() as session:
        key = APIKey(
            name=name, key_hash=mcp_auth.hash_key(raw), key_prefix=raw[:11],
            permission="read", is_active=True, user_id=user_id,
            last_used_at=last_used_at, daily_request_limit=limit,
        )
        session.add(key)
        await session.commit()
        return key.id, raw


async def make_oauth(maker, user_id, client_id="c1"):
    raw = secrets.token_hex(32)
    async with maker() as session:
        session.add(OAuthClient(
            client_id=client_id, client_secret_hash=None,
            token_endpoint_auth_method="none", client_name="Test Client",
            redirect_uris=[REDIRECT_URI], scope="read readwrite offline_access",
            user_id=user_id,
        ))
        await session.flush()
        token = OAuthToken(
            token_hash=oauth._hash(raw), token_type="access", client_id=client_id,
            scope="read", grant_id="g1", user_id=user_id,
            expires_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1),
        )
        session.add(token)
        await session.commit()
        return token.id, raw


_address = iter(range(1, 10_000))


async def drive(bearer):
    """Run the real middleware once; return `(status, bound vault root)`."""
    captured = {}
    sent = []

    async def downstream(scope, receive, send):
        captured["root"] = current_vault_root.get()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive():  # pragma: no cover - never awaited
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    n = next(_address)
    app = mcp_auth.APIKeyMiddleware(downstream)
    await app(
        {
            "type": "http", "method": "POST", "path": "/mcp/",
            "headers": [(b"authorization", f"Bearer {bearer}".encode())],
            "client": (f"198.51.{n // 250}.{n % 250 + 1}", 4242),
        },
        receive,
        send,
    )
    start = next(m for m in sent if m["type"] == "http.response.start")
    return start["status"], captured.get("root")


async def last_used(maker, key_id):
    async with maker() as session:
        return (await session.execute(
            text("SELECT last_used_at FROM api_keys WHERE id = :k"), {"k": key_id}
        )).scalar()


# ── 1. last_used_at ────────────────────────────────────────────────────────


async def test_a_stale_stamp_is_written_async_and_the_setting_does_not_leak(db):
    maker, spy = db
    uid = await make_user(maker)
    old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=2)
    key_id, raw = await make_key(maker, uid, last_used_at=old)
    pid_before, _ = await _backend(maker)
    spy.clear()

    status, root = await drive(raw)

    assert status == 200
    assert root is not None and str(root[1]) == "/vaults/alice"
    sql = spy.sql()
    assert len(sql) == 3, sql
    assert sql[0].lstrip().upper().startswith("SELECT")
    assert "LEFT OUTER JOIN users" in sql[0]
    _assert_scoped(spy, "UPDATE API_KEYS")
    assert await last_used(maker, key_id) > old

    pid_after, setting = await _backend(maker)
    assert pid_after == pid_before, "the pool handed out a different connection"
    assert setting == "on"


async def test_a_never_used_key_is_stamped(db):
    maker, spy = db
    uid = await make_user(maker)
    key_id, raw = await make_key(maker, uid, last_used_at=None)

    assert (await drive(raw))[0] == 200
    assert await last_used(maker, key_id) is not None
    assert (await _backend(maker))[1] == "on"


async def test_a_fresh_stamp_issues_one_statement_and_no_write(db):
    maker, spy = db
    uid = await make_user(maker)
    recent = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=10)
    key_id, raw = await make_key(maker, uid, last_used_at=recent)
    spy.clear()

    assert (await drive(raw))[0] == 200

    sql = spy.sql()
    assert len(sql) == 1, sql
    assert not spy.sync_statements()
    # The read-only transaction still ended before the response.
    assert spy.log[-1][0] in ("COMMIT", "ROLLBACK")
    assert await last_used(maker, key_id) == recent


async def test_two_concurrent_stale_requests_advance_the_stamp_once(wide, monkeypatch):
    maker, spy = wide
    await _wipe(maker)
    monkeypatch.setattr(mcp_auth, "async_session", maker)
    monkeypatch.setattr(mcp_auth.settings, "multi_user_mode", True, raising=False)
    try:
        uid = await make_user(maker)
        old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=2)
        key_id, raw = await make_key(maker, uid, last_used_at=old)
        spy.clear()

        results = await asyncio.gather(drive(raw), drive(raw), drive(raw))

        assert [r[0] for r in results] == [200, 200, 200]
        effective = [
            n for s, n in spy.rowcounts if s.lstrip().upper().startswith("UPDATE API_KEYS")
        ]
        # Each request that issued the UPDATE issued SET LOCAL first ...
        assert len(spy.sync_statements()) == len(effective) >= 1
        # ... and the stored value advanced exactly once.
        assert sum(effective) == 1, effective
        assert await last_used(maker, key_id) > old
    finally:
        await _wipe(maker)


# ── 2. the usage row ───────────────────────────────────────────────────────


def _usage_values(**over):
    values = dict(
        key_id=None, oauth_token_id=None, user_id=None, tool="read_note",
        params={"path": "a.md"}, duration_ms=3, response_size=10,
        actor_kind="api_key", actor_label="agent", actor_ref="omcp_x",
    )
    values.update(over)
    return values


async def test_the_usage_row_commits_async_visible_and_without_leaking(db):
    maker, spy = db
    uid = await make_user(maker)
    key_id, _ = await make_key(maker, uid)
    pid_before, _ = await _backend(maker)
    spy.clear()

    ok = await tools.write_usage_row(_usage_values(key_id=key_id, user_id=uid))

    assert ok is True
    _assert_scoped(spy, "INSERT INTO USAGE_LOGS")
    async with maker() as session:
        assert (await session.execute(
            text("SELECT count(*) FROM usage_logs WHERE key_id = :k"), {"k": key_id}
        )).scalar() == 1
    pid_after, setting = await _backend(maker)
    assert pid_after == pid_before
    assert setting == "on"


async def test_the_fk_retry_is_also_async(db):
    maker, spy = db
    spy.clear()

    # A key id that does not exist: the insert fails on the FK and is retried
    # with the credential columns cleared.
    ok = await tools.write_usage_row(_usage_values(key_id=987654))

    assert ok is True
    inserts = [
        i for i, (k, s) in enumerate(spy.log)
        if k == "SQL" and s.lstrip().upper().startswith("INSERT INTO USAGE_LOGS")
    ]
    assert len(inserts) == 2, spy.log
    sets = [
        i for i, (k, s) in enumerate(spy.log) if k == "SQL" and SYNC_SQL in s
    ]
    assert len(sets) == 2
    assert sets[0] < inserts[0] < sets[1] < inserts[1]
    assert (await _backend(maker))[1] == "on"


# ── 3. the quota ───────────────────────────────────────────────────────────


async def test_the_admission_and_its_prune_commit_async_without_leaking(db):
    maker, spy = db
    uid = await make_user(maker)
    key_id, _ = await make_key(maker, uid, limit=5)
    pid_before, _ = await _backend(maker)
    spy.clear()

    decision = await quotas.admit(key_id, 5)

    assert decision.count == 1  # the INSERT branch, so the prune ran too
    _assert_scoped(spy, "INSERT INTO QUOTA_COUNTERS")
    _assert_scoped(spy, "DELETE FROM QUOTA_COUNTERS")
    assert len(spy.sync_statements()) == 2
    pid_after, setting = await _backend(maker)
    assert pid_after == pid_before
    assert setting == "on"


async def test_exactly_n_of_more_than_n_concurrent_admissions_with_async_commit(
    wide, monkeypatch
):
    maker, spy = wide
    await _wipe(maker)
    monkeypatch.setattr(quotas, "async_session", maker)
    try:
        uid = await make_user(maker)
        key_id, _ = await make_key(maker, uid, limit=7)
        spy.clear()
        released = asyncio.Event()

        async def one():
            await released.wait()
            return await quotas.admit(key_id, 7)

        tasks = [asyncio.create_task(one()) for _ in range(20)]
        await asyncio.sleep(0.05)
        released.set()
        decisions = await asyncio.gather(*tasks)

        assert sum(1 for d in decisions if d.admitted) == 7
        assert sorted(d.count for d in decisions if d.admitted) == list(range(1, 8))
        # Every admission ran under async commit.
        admissions = [s for s in spy.sql() if "INSERT INTO quota_counters" in s]
        assert len(admissions) == 20
        assert len(spy.sync_statements()) >= 20
        async with maker() as session:
            assert (await session.execute(
                text("SELECT count FROM quota_counters WHERE key_id = :k"),
                {"k": key_id},
            )).scalar() == 7
    finally:
        await _wipe(maker)


# ── 4. the folded read still revokes on the next request (#66) ─────────────


async def _authenticated_once(maker, raw):
    status, root = await drive(raw)
    assert status == 200
    assert root is not None and root[1] is not None
    return root[0]


async def test_deactivating_the_user_refuses_the_next_request(db):
    maker, _ = db
    uid = await make_user(maker)
    _, raw = await make_key(maker, uid)
    await _authenticated_once(maker, raw)
    async with maker() as session:
        await session.execute(text("UPDATE users SET is_active = false WHERE id = :u"), {"u": uid})
        await session.commit()

    assert (await drive(raw))[0] == 401
    assert uid not in vault._user_vault_cache


async def test_clearing_vault_path_binds_none_and_evicts(db):
    maker, _ = db
    uid = await make_user(maker)
    _, raw = await make_key(maker, uid)
    await _authenticated_once(maker, raw)
    assert uid in vault._user_vault_cache
    async with maker() as session:
        await session.execute(text("UPDATE users SET vault_path = NULL WHERE id = :u"), {"u": uid})
        await session.commit()

    status, root = await drive(raw)
    assert status == 200
    assert root == (uid, None)  # every tool in this request refuses
    assert uid not in vault._user_vault_cache


async def test_deleting_the_user_row_refuses_the_next_request(db):
    """`api_keys.user_id` is ON DELETE CASCADE, so on a real server deleting
    the user takes the key with it and the next request is an unknown key.
    The outer join's NULL-user branch (a key whose user row is gone) is
    unreachable under that FK; its refusal is pinned hermetically in
    `tests/test_perf_auth_bookkeeping.py`."""
    maker, _ = db
    uid = await make_user(maker)
    key_id, raw = await make_key(maker, uid)
    await _authenticated_once(maker, raw)
    async with maker() as session:
        await session.execute(text("DELETE FROM users WHERE id = :u"), {"u": uid})
        await session.commit()

    assert (await drive(raw))[0] == 401


async def test_revoking_the_key_refuses_the_next_request(db):
    maker, _ = db
    uid = await make_user(maker)
    key_id, raw = await make_key(maker, uid)
    await _authenticated_once(maker, raw)
    async with maker() as session:
        await session.execute(text("UPDATE api_keys SET is_active = false WHERE id = :k"), {"k": key_id})
        await session.commit()

    assert (await drive(raw))[0] == 401


async def test_oauth_reads_the_user_in_one_statement_and_revocation_still_bites(db):
    maker, spy = db
    uid = await make_user(maker)
    token_id, raw = await make_oauth(maker, uid)
    spy.clear()

    status, root = await drive(raw)
    assert status == 200
    assert root == (uid, vault._user_vault_cache[uid])
    assert len(spy.sql()) == 1, spy.sql()
    assert not spy.sync_statements()

    async with maker() as session:
        await session.execute(text("UPDATE users SET is_active = false WHERE id = :u"), {"u": uid})
        await session.commit()
    assert (await drive(raw))[0] == 401

    async with maker() as session:
        await session.execute(text("UPDATE users SET is_active = true WHERE id = :u"), {"u": uid})
        await session.commit()
    assert (await drive(raw))[0] == 200
    async with maker() as session:
        await session.execute(text("UPDATE oauth_tokens SET revoked = true WHERE id = :t"), {"t": token_id})
        await session.commit()
    assert (await drive(raw))[0] == 401


# ── 5. grant and revoke paths stay synchronous ─────────────────────────────


class _FormReq:
    def __init__(self, form):
        self._form = form

    async def form(self):
        return self._form


async def test_grant_and_revoke_paths_issue_no_synchronous_commit(db, monkeypatch):
    maker, spy = db
    uid = await make_user(maker)
    await make_oauth(maker, uid)

    # Authorization-code exchange.
    code, verifier = secrets.token_urlsafe(32), "v" * 64
    async with maker() as session:
        session.add(OAuthCode(
            user_id=uid, code_hash=oauth._hash(code), client_id="c1",
            redirect_uri=REDIRECT_URI, scope="read",
            code_challenge=oauth._base64url_sha256(verifier),
            code_challenge_method="S256",
            expires_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5),
        ))
        await session.commit()
    spy.clear()
    exchanged = await oauth._handle_auth_code({
        "code": code, "client_id": "c1", "code_verifier": verifier,
        "redirect_uri": REDIRECT_URI,
    })
    assert exchanged.status_code == 200, exchanged.body
    import json
    pair = json.loads(exchanged.body)
    exchange_sql = spy.sql()

    # Refresh rotation.
    spy.clear()
    rotated = await oauth._handle_refresh(
        {"refresh_token": pair["refresh_token"], "client_id": "c1"}
    )
    assert rotated.status_code == 200, rotated.body
    rotated_pair = json.loads(rotated.body)
    rotation_sql = spy.sql()

    # Token revocation (RFC 7009).
    spy.clear()
    revoked = await oauth.revoke_token.__wrapped__(
        _FormReq({"token": rotated_pair["refresh_token"], "client_id": "c1"})
    )
    assert revoked.status_code == 200
    revocation_sql = spy.sql()

    # Grant-family revocation (the panel's).
    from src.oauth.grants import revoke_grant_family

    spy.clear()
    async with maker() as session:
        await revoke_grant_family(session, "g1")
        await session.commit()
    family_sql = spy.sql()

    # Transfer-token mint.
    from src.services import transfer

    key_id, _ = await make_key(maker, uid, name="minter")
    monkeypatch.setattr(transfer.settings, "multi_user_mode", False, raising=False)
    monkeypatch.setattr(transfer.settings, "vault_path", "/obsidian", raising=False)
    spy.clear()
    async with maker() as session:
        await transfer.mint_token(
            session, "download", "Attachments/a.png", overwrite=False,
            identity=transfer.Identity(key_id=key_id, user_id=uid),
            vault_root="/obsidian", expected_fingerprint=None,
        )
        await session.commit()
    mint_sql = spy.sql()

    for name, sql in (
        ("code exchange", exchange_sql),
        ("refresh rotation", rotation_sql),
        ("token revocation", revocation_sql),
        ("grant-family revocation", family_sql),
        ("transfer mint", mint_sql),
    ):
        assert sql, f"{name} issued no statements — the spy saw nothing"
        assert not [s for s in sql if SYNC_SQL in s], (name, sql)
