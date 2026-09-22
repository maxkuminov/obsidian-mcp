"""Real-Postgres gate for the unused-client expiry sweep (#194).

Three things here have no meaningful fake-session equivalent, because what
they assert *is* the database's behaviour:

* **The backfill.** `alembic check` sees the column, its type and its
  nullability, and nothing about which row got which marker. The invariant the
  whole sweep rests on — after 025 a NULL marker means "registered after 025
  and never used" and nothing else — is a statement about rows.
* **The guards.** "A revoked token row still in the table keeps its client
  alive" is decided by a correlated `NOT EXISTS` against real rows. A fake
  answering it would be asserting the test's own re-implementation of the
  predicate.
* **The race.** Inserting an `oauth_codes` row takes a `FOR KEY SHARE` lock on
  the parent through the foreign key; a delete takes `FOR UPDATE`. They
  conflict, so they serialise — but under READ COMMITTED an unblocked
  `DELETE ... WHERE NOT EXISTS (...)` re-evaluates only the *target row's* own
  predicate and not the subquery, so it proceeds and `ON DELETE CASCADE`
  removes the code that was just issued. That is the failure this design
  exists to prevent, and only two real transactions on a real lock manager can
  show whether it does. The naive statement is run **beside** the real sweep
  under the identical interleaving, so the case fails if the two ever start
  behaving the same way.

  Every one of those cases forces its interleaving rather than sleeping
  through it. One transaction holds the row and the other is either awaited to
  completion inside that lock (where the assertion is that it does *not*
  block) or polled in `pg_stat_activity` until PostgreSQL reports it waiting
  (`_wait_until_lock_blocked`, bounded, failing the test if the wait never
  happens). A `sleep` standing in for that is not synchronisation: it lets a
  slow container run the two serially, which passes the positive case having
  proved nothing and fails the negative control for no reason.

The migration's own marker, drift, downgrade and stamp-back cases live in
`tests/integration/test_schema_check.py`, which is the module `make
test-schema` actually runs.

Skipped unless `PGVECTOR_TEST_ADMIN_URL` names a throwaway Postgres *server*
(this module creates and drops its own databases):

    docker run --rm -d --name pgvector-test -e POSTGRES_PASSWORD=test \\
        -p 55432:5432 pgvector/pgvector:pg16
    PGVECTOR_TEST_ADMIN_URL=postgresql+asyncpg://postgres:test@localhost:55432/postgres \\
        pytest -q tests/integration/test_asvs_client_expiry_pg.py
    docker rm -f pgvector-test
"""
import asyncio
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit

import pytest
import pytest_asyncio
from sqlalchemy import delete as sa_delete
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.models.db import OAuthClient, OAuthCode, OAuthToken, User
from src.oauth import routes as oauth
from src.services import indexer

PGVECTOR_TEST_ADMIN_URL = os.environ.get("PGVECTOR_TEST_ADMIN_URL")
FORBIDDEN_DB_NAMES = {"obsidian_mcp"}
ROOT = Path(__file__).resolve().parent.parent.parent

pytestmark = [
    pytest.mark.asyncio(loop_scope="module"),
    pytest.mark.skipif(
        not PGVECTOR_TEST_ADMIN_URL,
        reason="PGVECTOR_TEST_ADMIN_URL not set",
    ),
]

REDIRECT_URI = "https://client.example.com/callback"
FUTURE = datetime(2099, 1, 1, tzinfo=timezone.utc)
PAST = datetime(2020, 1, 1, tzinfo=timezone.utc)


# ── throwaway database harness (same shape as test_oauth_grants_pg.py) ──────


def _with_database(url: str, dbname: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path=f"/{dbname}"))


def _asyncpg_dsn(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(parts._replace(scheme=parts.scheme.split("+", 1)[0]))


async def _run_maintenance(admin_url: str, statement: str) -> None:
    import asyncpg

    conn = await asyncpg.connect(_asyncpg_dsn(admin_url))
    try:
        await conn.execute(statement)
    finally:
        await conn.close()


def _alembic(url: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env={
            **os.environ,
            "DATABASE_URL": url,
            "SECRET_KEY": os.environ.get("SECRET_KEY") or "test-migration-key",
            "EMBEDDING_ALLOW_PLAINTEXT": "true",  # #185: default OLLAMA_URL is plaintext
        },
        capture_output=True,
        text=True,
        timeout=300,
    )


def _guard_admin_url():
    admin_db = unquote(urlsplit(PGVECTOR_TEST_ADMIN_URL).path).lstrip("/").casefold()
    if admin_db in FORBIDDEN_DB_NAMES:
        pytest.fail(
            "PGVECTOR_TEST_ADMIN_URL points at the production database name; "
            "this module creates and drops databases."
        )


@pytest.fixture(scope="module")
def migrated_database():
    _guard_admin_url()
    dbname = f"test_client_expiry_{uuid.uuid4().hex}"
    try:
        asyncio.run(
            _run_maintenance(PGVECTOR_TEST_ADMIN_URL, f'CREATE DATABASE "{dbname}"')
        )
        url = _with_database(PGVECTOR_TEST_ADMIN_URL, dbname)
        result = _alembic(url, "upgrade", "head")
        assert result.returncode == 0, (
            f"alembic upgrade head failed\n{result.stdout}\n{result.stderr}"
        )
        yield url
    finally:
        try:
            asyncio.run(
                _run_maintenance(
                    PGVECTOR_TEST_ADMIN_URL,
                    f'DROP DATABASE IF EXISTS "{dbname}" (FORCE)',
                )
            )
        except Exception as e:  # pragma: no cover - cleanup best effort
            print(f"warning: could not drop throwaway database {dbname}: {e}")


@pytest_asyncio.fixture(loop_scope="module")
async def database_at_024():
    """A throwaway database stopped one revision *short* of 025.

    Function-scoped and separate from the module database: the backfill cases
    seed rows and then run the migration over them, which is the only way to
    observe it.

    **Async, and its blocking work goes through `asyncio.to_thread`.** A sync
    fixture calling `asyncio.run` here leaves the main thread with no current
    event loop afterwards, and the module-scoped loop every other case in this
    file runs on is then gone — the symptom is an unrelated-looking
    "There is no current event loop in thread 'MainThread'".
    """
    _guard_admin_url()
    dbname = f"test_client_backfill_{uuid.uuid4().hex}"
    try:
        await _run_maintenance(PGVECTOR_TEST_ADMIN_URL, f'CREATE DATABASE "{dbname}"')
        url = _with_database(PGVECTOR_TEST_ADMIN_URL, dbname)
        result = await asyncio.to_thread(_alembic, url, "upgrade", "024")
        assert result.returncode == 0, (
            f"alembic upgrade 024 failed\n{result.stdout}\n{result.stderr}"
        )
        yield url
    finally:
        try:
            await _run_maintenance(
                PGVECTOR_TEST_ADMIN_URL,
                f'DROP DATABASE IF EXISTS "{dbname}" (FORCE)',
            )
        except Exception as e:  # pragma: no cover - cleanup best effort
            print(f"warning: could not drop throwaway database {dbname}: {e}")


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def engine(migrated_database):
    eng = create_async_engine(migrated_database)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def sessionmaker(engine):
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture(scope="module")
def monkeypatch_module():
    mp = pytest.MonkeyPatch()
    yield mp
    mp.undo()


@pytest_asyncio.fixture(loop_scope="module")
async def clean(sessionmaker, monkeypatch_module):
    """Empty the tables this module writes to, and point both surfaces here."""
    async with sessionmaker() as session:
        await session.execute(sa_delete(OAuthToken))
        await session.execute(sa_delete(OAuthCode))
        await session.execute(sa_delete(OAuthClient))
        await session.execute(sa_delete(User))
        await session.commit()
    monkeypatch_module.setattr(oauth, "async_session", sessionmaker)
    monkeypatch_module.setattr(indexer, "async_session", sessionmaker)
    monkeypatch_module.setattr(
        indexer.settings, "oauth_client_unused_expiry_days", 30, raising=False
    )
    yield sessionmaker


# ── seeding ────────────────────────────────────────────────────────────────


async def seed_client(
    sessionmaker,
    client_id,
    *,
    created_at=PAST,
    last_used_at=None,
    user_id=None,
):
    async with sessionmaker() as session:
        session.add(
            OAuthClient(
                client_id=client_id,
                client_secret_hash=None,
                token_endpoint_auth_method="none",
                client_name="Test Client",
                redirect_uris=[REDIRECT_URI],
                scope="read readwrite offline_access",
                user_id=user_id,
                created_at=created_at,
                last_used_at=last_used_at,
            )
        )
        await session.commit()


async def seed_token(
    sessionmaker, client_id, *, revoked=False, expires_at=FUTURE, token_hash=None
):
    async with sessionmaker() as session:
        session.add(
            OAuthToken(
                token_hash=token_hash or uuid.uuid4().hex * 2,
                token_type="access",
                client_id=client_id,
                scope="read",
                grant_id=uuid.uuid4().hex,
                expires_at=expires_at,
                revoked=revoked,
            )
        )
        await session.commit()


async def _wait_until_lock_blocked(engine, *, pid=None, timeout=20.0):
    """Wait until PostgreSQL itself reports a backend waiting on a lock.

    This is the synchronisation the race cases below need, and the reason a
    `sleep` is not one. A fixed sleep asserts nothing about the interleaving:
    on a loaded container the other transaction can commit inside it, after
    which the "concurrent" case runs serially and passes having proved nothing
    — and its negative control can fail for the same reason, spuriously.

    Polling `pg_stat_activity` waits on the **lock manager's own view** of
    progress instead, so the case proceeds at the instant the contention is
    real. The deadline is bounded and expiring it FAILS the test rather than
    letting it continue unsynchronised.

    `pid` is an optional callable naming the backend that must be the blocked
    one; it is late-bound because the waiting task usually learns its own pid
    only after it has started.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    async with engine.connect() as observer:
        while loop.time() < deadline:
            blocked = list(
                (
                    await observer.execute(
                        text(
                            "SELECT pid FROM pg_stat_activity "
                            "WHERE datname = current_database() "
                            "  AND state = 'active' "
                            "  AND wait_event_type = 'Lock' "
                            "  AND pid <> pg_backend_pid()"
                        )
                    )
                ).scalars()
            )
            # A fresh transaction each poll, or the view never moves.
            await observer.rollback()
            wanted = pid() if pid is not None else None
            if blocked and (wanted is None or wanted in blocked):
                return blocked
            await asyncio.sleep(0.02)
    pytest.fail(
        "no backend ever blocked on a lock within "
        f"{timeout}s — the interleaving this case asserts did not happen, so "
        "its result says nothing about concurrency"
    )


async def seed_code(sessionmaker, client_id, *, used=False, expires_at=FUTURE):
    async with sessionmaker() as session:
        session.add(
            OAuthCode(
                code_hash=uuid.uuid4().hex * 2,
                client_id=client_id,
                redirect_uri=REDIRECT_URI,
                scope="read",
                code_challenge="c" * 43,
                code_challenge_method="S256",
                expires_at=expires_at,
                used=used,
            )
        )
        await session.commit()


async def client_ids(sessionmaker) -> list[str]:
    async with sessionmaker() as session:
        rows = (
            await session.execute(select(OAuthClient.client_id).order_by(OAuthClient.id))
        ).scalars().all()
        return list(rows)


# ── the eligibility guards ─────────────────────────────────────────────────


async def test_a_never_used_client_older_than_the_age_is_deleted(clean):
    await seed_client(clean, "old-unused")

    assert await indexer._expire_unused_oauth_clients() == 1
    assert await client_ids(clean) == []


async def test_a_never_used_client_younger_than_the_age_is_kept(clean):
    await seed_client(clean, "young", created_at=datetime.now(timezone.utc))

    assert await indexer._expire_unused_oauth_clients() == 0
    assert await client_ids(clean) == ["young"]


async def test_a_client_that_has_ever_been_used_is_kept_however_old(clean):
    """The marker is the point: no child row survives, the registration is
    ancient, and it is still not a candidate."""
    await seed_client(clean, "used-long-ago", created_at=PAST, last_used_at=PAST)

    assert await indexer._expire_unused_oauth_clients() == 0
    assert await client_ids(clean) == ["used-long-ago"]


async def test_a_registration_predating_the_marker_is_never_swept(clean):
    """What migration 025's backfill buys, stated as a sweep case: a pre-025
    row carries the migration's own timestamp, so it can never be NULL and can
    never be reached — however old, and with no surviving child row."""
    stamped = datetime.now(timezone.utc) - timedelta(days=400)
    await seed_client(clean, "pre-025", created_at=PAST, last_used_at=stamped)

    assert await indexer._expire_unused_oauth_clients() == 0
    assert await client_ids(clean) == ["pre-025"]


async def test_a_client_holding_a_live_token_is_kept(clean):
    await seed_client(clean, "with-token")
    await seed_token(clean, "with-token")

    assert await indexer._expire_unused_oauth_clients() == 0
    assert await client_ids(clean) == ["with-token"]


async def test_a_client_whose_only_token_is_revoked_is_kept(clean):
    """A revoked token the operator can still see in the panel must never be
    cascaded away by this delete."""
    await seed_client(clean, "revoked-token")
    await seed_token(clean, "revoked-token", revoked=True)

    assert await indexer._expire_unused_oauth_clients() == 0
    assert await client_ids(clean) == ["revoked-token"]


async def test_a_client_whose_only_token_is_expired_is_kept(clean):
    await seed_client(clean, "expired-token")
    await seed_token(clean, "expired-token", expires_at=PAST)

    assert await indexer._expire_unused_oauth_clients() == 0
    assert await client_ids(clean) == ["expired-token"]


async def test_a_client_with_a_pending_code_is_kept(clean):
    await seed_client(clean, "pending-code")
    await seed_code(clean, "pending-code")

    assert await indexer._expire_unused_oauth_clients() == 0
    assert await client_ids(clean) == ["pending-code"]


async def test_a_client_with_a_spent_code_row_is_kept_while_the_row_survives(clean):
    await seed_client(clean, "spent-code")
    await seed_code(clean, "spent-code", used=True, expires_at=PAST)

    assert await indexer._expire_unused_oauth_clients() == 0
    assert await client_ids(clean) == ["spent-code"]


async def test_a_client_bound_to_a_user_is_kept(clean):
    async with clean() as session:
        session.add(
            User(
                id=1,
                username="alice",
                password_hash="x",
                is_admin=False,
                is_active=True,
                session_version=1,
            )
        )
        await session.commit()
    await seed_client(clean, "owned", user_id=1)

    assert await indexer._expire_unused_oauth_clients() == 0
    assert await client_ids(clean) == ["owned"]


async def test_the_disabled_setting_deletes_nothing(clean, monkeypatch):
    await seed_client(clean, "old-unused")
    monkeypatch.setattr(
        indexer.settings, "oauth_client_unused_expiry_days", None, raising=False
    )

    assert await indexer._expire_unused_oauth_clients() == 0
    assert await client_ids(clean) == ["old-unused"]


async def test_the_pass_is_bounded_by_its_batch(clean, monkeypatch):
    for n in range(5):
        await seed_client(clean, f"bulk-{n}")
    monkeypatch.setattr(indexer, "OAUTH_CLIENT_EXPIRY_BATCH", 2)

    assert await indexer._expire_unused_oauth_clients() == 2
    assert len(await client_ids(clean)) == 3
    assert await indexer._expire_unused_oauth_clients() == 2
    assert len(await client_ids(clean)) == 1


async def test_only_the_eligible_client_is_removed_from_a_mixed_table(clean):
    await seed_client(clean, "keep-token")
    await seed_token(clean, "keep-token")
    await seed_client(clean, "keep-young", created_at=datetime.now(timezone.utc))
    await seed_client(clean, "keep-marked", last_used_at=PAST)
    await seed_client(clean, "collect-me")

    assert await indexer._expire_unused_oauth_clients() == 1
    assert sorted(await client_ids(clean)) == [
        "keep-marked",
        "keep-token",
        "keep-young",
    ]


# ── the race ───────────────────────────────────────────────────────────────


async def test_a_contended_row_is_deferred_and_its_code_survives(clean, engine):
    """The authorization gets there first.

    A second connection holds the client row (an `UPDATE` stamping the marker,
    exactly what `_stamp_client_use` issues) and has inserted its code but not
    committed. The sweep must skip the row rather than block on it, and both
    rows must be there afterwards.
    """
    await seed_client(clean, "in-flight")

    holder = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with holder() as authorizing:
        assert await oauth._stamp_client_use(authorizing, "in-flight") is True
        authorizing.add(
            OAuthCode(
                code_hash="r" * 64,
                client_id="in-flight",
                redirect_uri=REDIRECT_URI,
                scope="read",
                code_challenge="c" * 43,
                code_challenge_method="S256",
                expires_at=FUTURE,
                used=False,
            )
        )
        await authorizing.flush()

        # The sweep runs while the row is locked. `SKIP LOCKED` must make this
        # return promptly rather than waiting on the consent.
        deleted = await asyncio.wait_for(
            indexer._expire_unused_oauth_clients(), timeout=10
        )
        assert deleted == 0, "a contended row must be deferred, not forced"

        await authorizing.commit()

    assert await client_ids(clean) == ["in-flight"]
    async with clean() as session:
        codes = (
            await session.execute(
                select(OAuthCode.code_hash).where(OAuthCode.client_id == "in-flight")
            )
        ).scalars().all()
    assert codes == ["r" * 64]


async def test_the_authorization_fails_cleanly_when_the_sweep_committed_first(clean):
    """The reverse ordering. The conditional `UPDATE ... RETURNING` matches
    nothing, so the handler answers `invalid_client` — never a foreign-key
    violation into a 500, and never a half-written grant."""
    await seed_client(clean, "swept")

    assert await indexer._expire_unused_oauth_clients() == 1

    async with clean() as session:
        assert await oauth._stamp_client_use(session, "swept") is False
        # Nothing was written on the way to learning that.
        codes = (await session.execute(select(OAuthCode.id))).scalars().all()
        assert codes == []
    assert await client_ids(clean) == []


class _ConsentRequest:
    """The handful of attributes `authorize_post` reads off the request."""

    def __init__(self, signed_cookie):
        self.cookies = {"oauth_state": signed_cookie}
        self.session = {}
        self.client = None
        self.url = None
        self.headers = {}


async def test_a_real_authorization_blocked_on_the_sweeps_lock_gets_invalid_client(
    clean, engine
):
    """The reverse ordering, forced on the lock rather than on a clock.

    The sweep holds its candidate lock — the same `SELECT ... FOR UPDATE` the
    real pass takes — and the **real consent handler** is started against that
    row. Its stamp is an `UPDATE` on the locked row, so it blocks; the case
    proceeds only once PostgreSQL reports that backend waiting, which is what
    makes this an interleaving rather than a sequence. The sweep then deletes
    and commits, and the unblocked authorization must discover the row is gone
    and answer an ordinary `invalid_client` — never a foreign-key violation
    into a 500, and never half a grant.
    """
    await seed_client(clean, "doomed")

    sweeper_maker = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    server_state = "csrfstatetoken1234567890"
    signed = oauth._state_serializer().dumps(server_state)

    async with sweeper_maker() as sweeping:
        # The sweep's own candidate statement, held open.
        locked = (
            await sweeping.execute(
                text(
                    "SELECT client_id FROM oauth_clients "
                    "WHERE client_id = 'doomed' FOR UPDATE"
                )
            )
        ).scalars().all()
        assert list(locked) == ["doomed"]

        consent = asyncio.create_task(
            oauth.authorize_post(
                _ConsentRequest(signed),
                action="approve",
                client_id="doomed",
                redirect_uri=REDIRECT_URI,
                code_challenge="A" * 43,
                code_challenge_method="S256",
                scope="read",
                state=server_state,
                client_state="clientecho",
            )
        )

        # Not a sleep: the consent must be observably waiting on this lock.
        await _wait_until_lock_blocked(engine)
        assert not consent.done(), "the authorization should still be blocked"

        await sweeping.execute(
            text("DELETE FROM oauth_clients WHERE client_id = 'doomed'")
        )
        await sweeping.commit()

        response = await asyncio.wait_for(consent, timeout=20)

    assert response.status_code == 400
    assert json.loads(bytes(response.body))["error"] == "invalid_client"
    assert await client_ids(clean) == []
    async with clean() as session:
        codes = (await session.execute(select(OAuthCode.id))).scalars().all()
    assert list(codes) == [], "a refused authorization may write nothing"


async def test_the_naive_conditional_delete_really_does_cascade_the_new_code(
    clean, engine
):
    """The failure the design exists to prevent, demonstrated rather than
    asserted — and then shown not to happen with the real sweep under the
    identical interleaving.

    `DELETE ... WHERE NOT EXISTS (SELECT 1 FROM oauth_codes ...)` blocks on the
    foreign key's `FOR KEY SHARE` lock; when the authorization commits and the
    delete unblocks, READ COMMITTED re-evaluates the target row's own
    predicate and **not** the subquery, so it proceeds and `ON DELETE CASCADE`
    takes the code with it.
    """
    await seed_client(clean, "naive-victim")

    holder = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    other = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with holder() as authorizing:
        authorizing.add(
            OAuthCode(
                code_hash="n" * 64,
                client_id="naive-victim",
                redirect_uri=REDIRECT_URI,
                scope="read",
                code_challenge="c" * 43,
                code_challenge_method="S256",
                expires_at=FUTURE,
                used=False,
            )
        )
        await authorizing.flush()

        sweeper_pid: dict[str, int] = {}

        async def _naive_delete():
            async with other() as sweeper:
                sweeper_pid["pid"] = (
                    await sweeper.execute(text("SELECT pg_backend_pid()"))
                ).scalar()
                await sweeper.execute(
                    text(
                        "DELETE FROM oauth_clients c "
                        "WHERE c.last_used_at IS NULL AND c.user_id IS NULL "
                        "  AND c.created_at < now() - interval '30 days' "
                        "  AND NOT EXISTS (SELECT 1 FROM oauth_codes o "
                        "                   WHERE o.client_id = c.client_id) "
                        "  AND NOT EXISTS (SELECT 1 FROM oauth_tokens t "
                        "                   WHERE t.client_id = c.client_id)"
                    )
                )
                await sweeper.commit()

        task = asyncio.create_task(_naive_delete())
        # Wait until the delete is *observably* blocked on the foreign key's
        # lock, then release it by committing the insert. A sleep here would
        # let a slow container run the two serially, and this control would
        # then fail spuriously — the naive delete would find the committed
        # code and correctly skip the row.
        await _wait_until_lock_blocked(engine, pid=lambda: sweeper_pid.get("pid"))
        await authorizing.commit()
        await asyncio.wait_for(task, timeout=10)

    assert await client_ids(clean) == [], (
        "the naive conditional delete is expected to win here — if it stopped "
        "doing so, this case no longer proves anything about the real sweep"
    )


async def test_the_real_sweep_survives_the_interleaving_the_naive_delete_loses(
    clean, engine
):
    """The same interleaving, the shipped implementation.

    The authorization holds the parent row (it stamps the marker, which the
    naive statement's inserting-only equivalent did not), so the candidate
    scan's `SKIP LOCKED` defers it; and even once the lock is released the
    in-lock re-read sees the committed marker and the committed code.

    **The interleaving is forced by construction, with no sleep.** The whole
    sweep is awaited to completion *inside* the uncommitted authorization
    transaction, so the row lock provably covers every statement the sweep
    issues. Nothing is polled because there is nothing to wait for: the real
    sweep must not block here, and `SKIP LOCKED` is what makes that true.
    """
    await seed_client(clean, "real-survivor")

    holder = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with holder() as authorizing:
        assert await oauth._stamp_client_use(authorizing, "real-survivor") is True
        authorizing.add(
            OAuthCode(
                code_hash="s" * 64,
                client_id="real-survivor",
                redirect_uri=REDIRECT_URI,
                scope="read",
                code_challenge="c" * 43,
                code_challenge_method="S256",
                expires_at=FUTURE,
                used=False,
            )
        )
        await authorizing.flush()

        # Held lock, whole sweep, bounded wait: a sweep that blocked instead of
        # skipping would expire this timeout rather than pass.
        deleted = await asyncio.wait_for(
            indexer._expire_unused_oauth_clients(), timeout=15
        )
        assert deleted == 0
        await authorizing.commit()

    assert await client_ids(clean) == ["real-survivor"]
    async with clean() as session:
        codes = (
            await session.execute(
                select(OAuthCode.code_hash).where(
                    OAuthCode.client_id == "real-survivor"
                )
            )
        ).scalars().all()
    assert codes == ["s" * 64]

    # And a second pass, with no contention at all, still keeps it: the marker
    # committed by the authorization is what the sweep now reads.
    assert await indexer._expire_unused_oauth_clients() == 0
    assert await client_ids(clean) == ["real-survivor"]


# ── what the delete does not take with it ──────────────────────────────────


async def test_usage_attribution_survives_the_expiry(clean):
    """The audit trail outlives the registration it describes.

    The premise has to be built deliberately, because the sweep only ever
    deletes a client with **no** token rows: a `usage_logs` line can only meet
    the sweep if its own token was purged first, which is exactly what happens
    seven days after a token expires. So the row is left as that purge leaves
    it — `oauth_token_id` already NULL by `ON DELETE SET NULL` — and the
    denormalised actor columns (#77, migration 015) are what must still be
    readable once the client row is gone. They are written at call time from
    the credential that authenticated the request, precisely so that deleting
    the credential cannot rewrite history into "unknown".
    """
    await seed_client(clean, "retired")

    async with clean() as session:
        await session.execute(
            text(
                "INSERT INTO usage_logs (oauth_token_id, tool, actor_kind, "
                " actor_label, actor_ref) "
                "VALUES (NULL, 'keyword_search', 'oauth', 'Retired Connector', "
                "        'retired')"
            )
        )
        await session.commit()

    assert await indexer._expire_unused_oauth_clients() == 1
    assert await client_ids(clean) == []

    async with clean() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT tool, actor_kind, actor_label, actor_ref "
                    "FROM usage_logs"
                )
            )
        ).all()
        await session.execute(text("DELETE FROM usage_logs"))
        await session.commit()

    assert [tuple(row) for row in rows] == [
        ("keyword_search", "oauth", "Retired Connector", "retired")
    ]


# ── the backfill ───────────────────────────────────────────────────────────


def _seed_and_migrate(url, statements):
    """Seed rows on a 024 database, then run 025 over them.

    Drives its own event loop, so its callers reach it through
    `asyncio.to_thread`: the module's `pytestmark` puts every case on one
    module-scoped loop, and `asyncio.run` inside a running loop raises.
    """

    async def _run():
        engine = create_async_engine(url)
        try:
            async with engine.begin() as conn:
                for statement in statements:
                    await conn.execute(text(statement))
        finally:
            await engine.dispose()

    asyncio.run(_run())
    result = _alembic(url, "upgrade", "head")
    assert result.returncode == 0, (
        f"alembic upgrade head failed\n{result.stdout}\n{result.stderr}"
    )


def _markers(url) -> dict:
    async def _run():
        engine = create_async_engine(url)
        try:
            async with engine.connect() as conn:
                rows = (
                    await conn.execute(
                        text("SELECT client_id, last_used_at FROM oauth_clients")
                    )
                ).all()
        finally:
            await engine.dispose()
        return {row[0]: row[1] for row in rows}

    return asyncio.run(_run())


CLIENT_INSERT = (
    "INSERT INTO oauth_clients (client_id, client_secret_hash, "
    " token_endpoint_auth_method, client_name, redirect_uris, scope, created_at) "
    "VALUES ('{cid}', NULL, 'none', 'seeded', '[]'::jsonb, 'read', "
    "        TIMESTAMPTZ '2020-01-01')"
)


async def test_the_backfill_marks_from_tokens_codes_or_its_own_clock(
    database_at_024,
):
    """The three cases in one migration run, so the statement is exercised
    exactly as it will run on the live database rather than three times over
    three shapes."""
    url = database_at_024
    await asyncio.to_thread(
        _seed_and_migrate,
        url,
        [
            CLIENT_INSERT.format(cid="has-token"),
            CLIENT_INSERT.format(cid="has-code"),
            CLIENT_INSERT.format(cid="has-neither"),
            "INSERT INTO oauth_tokens (token_hash, token_type, client_id, scope, "
            " grant_id, expires_at, revoked, created_at) VALUES "
            "('" + "t" * 64 + "', 'access', 'has-token', 'read', 'g1', "
            " TIMESTAMPTZ '2099-01-01', false, TIMESTAMPTZ '2021-05-05')",
            "INSERT INTO oauth_tokens (token_hash, token_type, client_id, scope, "
            " grant_id, expires_at, revoked, created_at) VALUES "
            "('" + "u" * 64 + "', 'refresh', 'has-token', 'read', 'g1', "
            " TIMESTAMPTZ '2099-01-01', false, TIMESTAMPTZ '2021-01-01')",
            "INSERT INTO oauth_codes (code_hash, client_id, redirect_uri, scope, "
            " code_challenge, code_challenge_method, expires_at, used, created_at) "
            "VALUES ('" + "c" * 64 + "', 'has-code', 'https://x.invalid/cb', 'read', "
            "'" + "d" * 43 + "', 'S256', TIMESTAMPTZ '2099-01-01', false, "
            " TIMESTAMPTZ '2022-02-02')",
        ],
    )

    markers = await asyncio.to_thread(_markers, url)
    assert markers["has-token"] == datetime(2021, 5, 5, tzinfo=timezone.utc)
    assert markers["has-code"] == datetime(2022, 2, 2, tzinfo=timezone.utc)
    assert markers["has-neither"] is not None, (
        "a client with no surviving child row must be stamped with the "
        "migration's own timestamp, never left NULL"
    )
    assert markers["has-neither"] > datetime(2023, 1, 1, tzinfo=timezone.utc)
    assert all(value is not None for value in markers.values())


async def test_a_backfilled_client_is_never_swept(database_at_024):
    """The end-to-end statement of the invariant: a client that predates 025
    and has no surviving evidence is stamped by the migration, and the sweep
    then cannot reach it however old it is."""
    url = database_at_024
    await asyncio.to_thread(
        _seed_and_migrate, url, [CLIENT_INSERT.format(cid="ancient")]
    )

    engine = create_async_engine(url)
    try:
        maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        mp = pytest.MonkeyPatch()
        try:
            mp.setattr(indexer, "async_session", maker)
            mp.setattr(
                indexer.settings,
                "oauth_client_unused_expiry_days",
                30,
                raising=False,
            )
            assert await indexer._expire_unused_oauth_clients() == 0
            async with maker() as session:
                remaining = (
                    await session.execute(select(OAuthClient.client_id))
                ).scalars().all()
            assert list(remaining) == ["ancient"]
        finally:
            mp.undo()
    finally:
        await engine.dispose()
