"""Unused dynamic client registrations expire, and issuance stamps the marker.

`/register` is unauthenticated RFC 7591 dynamic client registration and
nothing bounded `oauth_clients`: the maintenance pass deleted codes, tokens
and panel sessions and never touched a client row (#194, ASVS edge
hardening).

What is asserted here is the *shape* of the two halves and the three call
sites, all of which are decidable without a database:

* the sweep's candidate query — the three eligibility columns, the batch
  bound, and `FOR UPDATE SKIP LOCKED`;
* the **in-lock re-check**, which is a second statement precisely so it gets a
  fresh READ COMMITTED snapshot. A single `DELETE ... WHERE NOT EXISTS (...)`
  is not equivalent: when it unblocks, PostgreSQL re-evaluates only the target
  row's own predicate and not the subquery, so it would cascade away an
  authorization code issued microseconds earlier;
* that a delete is only ever keyed on what the re-check returned;
* the disabled spelling and the refusal of a zero-day window;
* the INFO count line;
* and that `/authorize`, the code exchange and the refresh each stamp
  `last_used_at`, with `/authorize` answering `invalid_client` — not a 500 and
  not a half-written grant — when the sweep won the race.

The **row-level** guards (a live, revoked or expired token; a pending code; an
owned client; the age boundary) live in
`tests/integration/test_asvs_client_expiry_pg.py`, where a real database
decides them. A fake that answered those questions would be asserting the
test's own re-implementation of the predicate, not the predicate.
"""
import asyncio
import datetime
import json
import logging

import pytest
from fastapi.responses import RedirectResponse
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.dml import Delete, Update
from sqlalchemy.sql.selectable import Select

from src.config import Settings
from src.models.db import OAuthClient
from src.oauth import routes as oauth
from src.services import indexer

from _oauth_grant_fakes import FakeClient, SeqSession, is_client_use_stamp

UTC = datetime.timezone.utc
FROZEN_NOW = datetime.datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
REGISTERED_URI = "https://client.example.com/callback"


class _FrozenDatetime(datetime.datetime):
    @classmethod
    def now(cls, tz=None):
        return FROZEN_NOW if tz is None else FROZEN_NOW.astimezone(tz)


# ── the sweep ──────────────────────────────────────────────────────────────


class _Scalars:
    def __init__(self, rows):
        self._rows = list(rows)

    def all(self):
        return list(self._rows)


class _Result:
    def __init__(self, rows=(), rowcount=0):
        self._rows = list(rows)
        self.rowcount = rowcount

    def scalars(self):
        return _Scalars(self._rows)


class _SweepSession:
    """Answers the sweep's two SELECTs from a script, recording everything.

    The candidate set and the re-check's answer are supplied separately, which
    is what lets a case stage the interleaving the whole design exists for: a
    row that qualified when the batch was selected and does not qualify any
    more by the time the lock is held.
    """

    def __init__(self, candidates=(), still_eligible=None):
        self._candidates = list(candidates)
        self._still = (
            list(candidates) if still_eligible is None else list(still_eligible)
        )
        self.statements = []
        self.committed = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def execute(self, stmt, *_a, **_kw):
        self.statements.append(stmt)
        if isinstance(stmt, Select):
            # The first SELECT is the locking candidate scan; the second is
            # the in-lock re-check.
            selects = [s for s in self.statements if isinstance(s, Select)]
            if len(selects) == 1:
                return _Result(self._candidates)
            return _Result(self._still)
        if isinstance(stmt, Delete):
            keyed = _delete_keys(stmt)
            return _Result(rowcount=len(keyed))
        raise AssertionError(f"unexpected statement from the sweep: {stmt}")

    async def commit(self):
        self.committed += 1


def _selects(session):
    return [s for s in session.statements if isinstance(s, Select)]


def _deletes(session):
    return [s for s in session.statements if isinstance(s, Delete)]


def _delete_keys(stmt) -> list:
    """The `client_id`s an `IN (...)` delete names."""
    values = []
    for value in stmt.compile().params.values():
        if isinstance(value, (list, tuple)):
            values.extend(value)
        else:
            values.append(value)
    return values


def run_sweep(monkeypatch, *, candidates=(), still_eligible=None, days=30):
    session = _SweepSession(candidates, still_eligible)
    monkeypatch.setattr(indexer, "async_session", lambda: session)
    monkeypatch.setattr(indexer, "datetime", _FrozenDatetime)
    monkeypatch.setattr(
        indexer.settings, "oauth_client_unused_expiry_days", days, raising=False
    )
    deleted = asyncio.run(indexer._expire_unused_oauth_clients())
    return session, deleted


def test_the_candidate_scan_locks_and_skips_contended_rows(monkeypatch):
    """`FOR UPDATE SKIP LOCKED`, not a bare SELECT and not a blocking one.

    The lock is what makes the re-check below meaningful, and `SKIP LOCKED` is
    what stops one in-flight consent from stalling the whole maintenance tick
    — a row another transaction holds is precisely a row that is about to stop
    qualifying.
    """
    session, _ = run_sweep(monkeypatch, candidates=["c1"])
    statement = _selects(session)[0]

    # Read off the statement *and* off its PostgreSQL rendering: `SKIP LOCKED`
    # is dialect-specific, so the default `str()` drops it silently and an
    # assertion over that string would pass on a plain `FOR UPDATE` — which
    # blocks the maintenance tick behind an in-flight consent.
    assert statement._for_update_arg is not None
    assert statement._for_update_arg.skip_locked is True
    rendered = " ".join(
        str(statement.compile(dialect=postgresql.dialect())).split()
    ).upper()
    assert "FOR UPDATE SKIP LOCKED" in rendered


def test_the_candidate_scan_carries_all_three_eligibility_columns(monkeypatch):
    session, _ = run_sweep(monkeypatch, candidates=["c1"])
    rendered = " ".join(str(_selects(session)[0]).split())

    assert "oauth_clients.last_used_at IS NULL" in rendered
    assert "oauth_clients.user_id IS NULL" in rendered
    assert "oauth_clients.created_at <" in rendered


def test_the_cutoff_is_the_configured_number_of_days(monkeypatch):
    session, _ = run_sweep(monkeypatch, candidates=["c1"], days=7)
    params = _selects(session)[0].compile().params

    assert params["created_at_1"] == FROZEN_NOW - datetime.timedelta(days=7)


def test_the_batch_bound_is_applied_to_the_candidate_scan(monkeypatch):
    session, _ = run_sweep(monkeypatch, candidates=["c1"])
    limit = _selects(session)[0]._limit_clause

    assert limit is not None
    assert limit.value == indexer.OAUTH_CLIENT_EXPIRY_BATCH


def test_eligibility_is_re_evaluated_inside_the_lock(monkeypatch):
    """The second statement is the whole race control.

    A single `DELETE ... WHERE NOT EXISTS (...)` would look equivalent and is
    not: under READ COMMITTED an unblocked delete re-evaluates the target
    row's own predicate and **not** the existence subquery, so it proceeds and
    `ON DELETE CASCADE` removes the authorization code that had just been
    issued. The re-read is a separate statement so it gets a fresh snapshot.
    """
    session, _ = run_sweep(monkeypatch, candidates=["c1"])
    recheck = " ".join(str(_selects(session)[1]).split())

    assert "NOT (EXISTS" in recheck, recheck
    assert "oauth_codes" in recheck
    assert "oauth_tokens" in recheck
    # And the marker, the owner and the age are all read again, not assumed
    # from the first snapshot.
    assert "oauth_clients.last_used_at IS NULL" in recheck
    assert "oauth_clients.user_id IS NULL" in recheck
    assert "oauth_clients.created_at <" in recheck


def test_the_recheck_names_no_token_state_at_all(monkeypatch):
    """Any token row disqualifies — live, expired or revoked.

    A revoked token the operator can still see in the panel must never be
    cascaded away by this delete, so the guard must not narrow itself to live
    rows.
    """
    session, _ = run_sweep(monkeypatch, candidates=["c1"])
    recheck = " ".join(str(_selects(session)[1]).split())

    assert "oauth_tokens.revoked" not in recheck
    assert "oauth_tokens.expires_at" not in recheck


def test_a_row_that_stops_qualifying_under_the_lock_is_not_deleted(monkeypatch):
    """The interleaving the re-check exists for: two candidates, one of which
    was stamped (or given a code) between the scan and the lock."""
    session, deleted = run_sweep(
        monkeypatch, candidates=["c1", "c2"], still_eligible=["c2"]
    )

    assert deleted == 1
    assert _delete_keys(_deletes(session)[0]) == ["c2"]


def test_nothing_is_deleted_when_the_lock_disqualifies_every_candidate(monkeypatch):
    session, deleted = run_sweep(
        monkeypatch, candidates=["c1"], still_eligible=[]
    )

    assert deleted == 0
    assert _deletes(session) == []


def test_no_statement_is_issued_when_there_are_no_candidates(monkeypatch):
    session, deleted = run_sweep(monkeypatch, candidates=[])

    assert deleted == 0
    assert _deletes(session) == []
    assert len(_selects(session)) == 1


def test_the_disabled_setting_skips_the_sweep_entirely(monkeypatch):
    """The kill switch is `null`, and it must not merely delete nothing — it
    must not look at the table at all."""
    session, deleted = run_sweep(monkeypatch, candidates=["c1"], days=None)

    assert deleted == 0
    assert session.statements == []


def test_the_delete_is_keyed_only_on_what_the_recheck_returned(monkeypatch):
    session, _ = run_sweep(
        monkeypatch, candidates=["c1", "c2", "c3"], still_eligible=["c3"]
    )
    statement = _deletes(session)[0]

    assert statement.table.name == "oauth_clients"
    assert _delete_keys(statement) == ["c3"]


def test_a_deleting_pass_logs_the_count(monkeypatch, caplog):
    with caplog.at_level(logging.INFO, logger=indexer.logger.name):
        _, deleted = run_sweep(monkeypatch, candidates=["c1", "c2"])

    assert deleted == 2
    lines = [r.getMessage() for r in caplog.records if "OAuth client cleanup" in r.getMessage()]
    assert len(lines) == 1, caplog.text
    assert "2" in lines[0]
    assert "30" in lines[0]


def test_a_pass_that_deletes_nothing_logs_nothing(monkeypatch, caplog):
    with caplog.at_level(logging.INFO, logger=indexer.logger.name):
        run_sweep(monkeypatch, candidates=[])

    assert not [
        r for r in caplog.records if "OAuth client cleanup" in r.getMessage()
    ]


def test_the_periodic_cleanup_actually_calls_the_sweep(monkeypatch):
    """Fan-out ships green-but-unwired code: the sweep is only a control if
    the maintenance tick runs it."""
    called = []

    async def _spy():
        called.append(True)
        return 0

    class _Result0:
        rowcount = 0

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def execute(self, *_a, **_kw):
            return _Result0()

        async def commit(self):
            pass

    monkeypatch.setattr(indexer, "async_session", lambda: _Session())
    monkeypatch.setattr(indexer, "_expire_unused_oauth_clients", _spy)
    asyncio.run(indexer.cleanup_expired_tokens())

    assert called == [True]


# ── the setting ────────────────────────────────────────────────────────────


def _settings(**overrides):
    return Settings(
        secret_key="x" * 48,
        database_url="postgresql+asyncpg://u:p@localhost/db",
        vault_path="/tmp",
        **overrides,
    )


def test_the_expiry_age_defaults_to_thirty_days():
    assert _settings().oauth_client_unused_expiry_days == 30


def test_a_zero_day_window_is_refused_at_settings_construction():
    """`ge=1`, the house rule: a control that deletes a registration the
    moment it is made reads to an operator as an outage, not as a setting."""
    with pytest.raises(Exception) as excinfo:
        _settings(oauth_client_unused_expiry_days=0)

    assert "oauth_client_unused_expiry_days" in str(excinfo.value)


@pytest.mark.parametrize("spelling", ["", "null", "none", "NONE", " null "])
def test_the_off_spellings_disable_the_sweep(spelling):
    assert _settings(oauth_client_unused_expiry_days=spelling).oauth_client_unused_expiry_days is None


def test_a_negative_window_is_refused():
    with pytest.raises(Exception):
        _settings(oauth_client_unused_expiry_days=-1)


# ── the three stamp sites ──────────────────────────────────────────────────


class _ConsentSession:
    """Just enough for `authorize_post`'s approve path.

    `client_present` stages the race the sweep can win: the conditional
    `UPDATE ... RETURNING` matches nothing because the row is gone.
    """

    def __init__(self, client, *, client_present=True):
        self._client = client
        self.client_present = client_present
        self.added = []
        self.committed = False
        self.stamps = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def execute(self, stmt, *_a, **_kw):
        from _oauth_grant_fakes import _Result

        if is_client_use_stamp(stmt):
            self.stamps.append(dict(stmt.compile().params).get("client_id_1"))
            return _Result(["client123"] if self.client_present else [])
        if isinstance(stmt, Update):
            return _Result([])
        return _Result([self._client])

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed = True


class _FakeRequest:
    def __init__(self, signed_cookie):
        self.cookies = {"oauth_state": signed_cookie}
        self.session = {}
        self.client = None
        self.url = None
        self.headers = {}


def _approve(session, monkeypatch):
    monkeypatch.setattr(oauth.settings, "multi_user_mode", False, raising=False)
    monkeypatch.setattr(oauth, "async_session", lambda: session)
    server_state = "csrfstatetoken1234567890"
    signed = oauth._state_serializer().dumps(server_state)
    return asyncio.run(
        oauth.authorize_post(
            _FakeRequest(signed),
            action="approve",
            client_id="client123",
            redirect_uri=REGISTERED_URI,
            code_challenge="A" * 43,
            code_challenge_method="S256",
            scope="readwrite",
            state=server_state,
            client_state="clientecho",
        )
    )


def test_consent_stamps_the_marker_in_the_transaction_that_mints_the_code(monkeypatch):
    session = _ConsentSession(FakeClient(redirect_uris=[REGISTERED_URI]))
    response = _approve(session, monkeypatch)

    assert isinstance(response, RedirectResponse)
    assert session.stamps == ["client123"]
    assert len(session.added) == 1
    assert session.committed is True


def test_consent_answers_invalid_client_when_the_sweep_won_the_race(monkeypatch):
    """Never a 500 and never a half-written grant: the conditional UPDATE
    matched nothing, so there is no row to hang a code on."""
    session = _ConsentSession(
        FakeClient(redirect_uris=[REGISTERED_URI]), client_present=False
    )
    response = _approve(session, monkeypatch)

    assert response.status_code == 400
    assert json.loads(response.body)["error"] == "invalid_client"
    assert session.added == [], "no authorization code may be minted"
    assert session.committed is False


def test_the_stamp_precedes_the_code_insert(monkeypatch):
    """Order matters: the `UPDATE` takes the row lock the sweep contends for,
    so learning the row is gone must happen before anything is written."""
    session = _ConsentSession(
        FakeClient(redirect_uris=[REGISTERED_URI]), client_present=False
    )
    _approve(session, monkeypatch)

    assert session.stamps == ["client123"]
    assert session.added == []


# ── the sweep's win is not a cross-user conflict ───────────────────────────
#
# Multi-user consent claims an unclaimed client with a conditional
# `UPDATE ... WHERE user_id IS NULL RETURNING`. When that matches nothing the
# handler re-reads the owner, and the sweep deleting the row mid-consent used
# to land in the *other-owner* branch: a 403 `access_denied` naming an owner
# that does not exist, plus an `oauth_cross_user_client_refused` record about a
# user who did nothing. A vanished registration is the sweep's ordinary work
# and answers `invalid_client`, exactly as the stamp's own loss does.


def _selects_only_owner(stmt) -> bool:
    """Is this the `select(OAuthClient.user_id)` owner re-read?"""
    try:
        descriptions = stmt.column_descriptions
    except Exception:
        return False
    return (
        len(descriptions) == 1
        and getattr(descriptions[0].get("expr"), "key", None) == "user_id"
    )


class _ClaimRaceSession(_ConsentSession):
    """A consent whose conditional claim matches nothing.

    `owner_after_claim` is what the follow-up re-read finds: another user's id
    for a genuine conflict, `None` for a row the sweep has deleted.
    """

    def __init__(self, owner_after_claim):
        super().__init__(FakeClient(redirect_uris=[REGISTERED_URI], user_id=None))
        self._owner_after_claim = owner_after_claim
        self.claim_attempts = 0

    async def execute(self, stmt, *_a, **_kw):
        from _oauth_grant_fakes import _Result

        if is_client_use_stamp(stmt):
            self.stamps.append(dict(stmt.compile().params).get("client_id_1"))
            return _Result(["client123"])
        if isinstance(stmt, Update):
            self.claim_attempts += 1
            return _Result([])
        if _selects_only_owner(stmt):
            return _Result(
                [] if self._owner_after_claim is None else [self._owner_after_claim]
            )
        return _Result([self._client])


class _EventCapture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


def _approve_multi_user(session, monkeypatch):
    """The approve path with a signed-in user, capturing security events."""
    from src.services import security_events

    monkeypatch.setattr(oauth.settings, "multi_user_mode", True, raising=False)
    monkeypatch.setattr(oauth, "async_session", lambda: session)

    class _SessionUser:
        id = 2

    async def _resolve(_request, _session):
        return _SessionUser()

    monkeypatch.setattr(oauth, "get_active_session_user", _resolve)

    handler = _EventCapture()
    logger = security_events.logger
    logger.addHandler(handler)
    propagate, level = logger.propagate, logger.level
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    server_state = "csrfstatetoken1234567890"
    signed = oauth._state_serializer().dumps(server_state)
    try:
        with security_events.suppression_disabled():
            response = asyncio.run(
                oauth.authorize_post(
                    _FakeRequest(signed),
                    action="approve",
                    client_id="client123",
                    redirect_uri=REGISTERED_URI,
                    code_challenge="A" * 43,
                    code_challenge_method="S256",
                    scope="readwrite",
                    state=server_state,
                    client_state="clientecho",
                )
            )
    finally:
        logger.removeHandler(handler)
        logger.propagate = propagate
        logger.setLevel(level)
    return response, [record.getMessage() for record in handler.records]


def test_a_client_the_sweep_deleted_mid_consent_is_not_a_cross_user_conflict(
    monkeypatch,
):
    """`invalid_client`, and no security record naming an innocent owner."""
    session = _ClaimRaceSession(owner_after_claim=None)
    response, events = _approve_multi_user(session, monkeypatch)

    assert session.claim_attempts == 1
    assert response.status_code == 400
    assert json.loads(response.body)["error"] == "invalid_client"
    assert "oauth_cross_user_client_refused" not in events, events
    assert session.added == [], "no authorization code may be minted"
    assert session.committed is False


def test_the_vanished_client_answers_exactly_as_a_lost_stamp_does(monkeypatch):
    """The two branches are the same event and must not drift apart."""
    vanished, _ = _approve_multi_user(_ClaimRaceSession(None), monkeypatch)
    lost_stamp = _approve(
        _ConsentSession(FakeClient(redirect_uris=[REGISTERED_URI]), client_present=False),
        monkeypatch,
    )

    assert vanished.status_code == lost_stamp.status_code
    assert json.loads(vanished.body) == json.loads(lost_stamp.body)


def test_a_client_another_user_claimed_is_still_refused_as_before(monkeypatch):
    """The genuine conflict is untouched: 403 and the cross-user record."""
    session = _ClaimRaceSession(owner_after_claim=1)
    response, events = _approve_multi_user(session, monkeypatch)

    assert session.claim_attempts == 1
    assert response.status_code == 403
    assert json.loads(response.body)["error"] == "access_denied"
    assert events.count("oauth_cross_user_client_refused") == 1, events
    assert session.added == []
    assert session.committed is False


def _exchange(session, monkeypatch, form):
    monkeypatch.setattr(oauth.settings, "multi_user_mode", False, raising=False)
    monkeypatch.setattr(oauth, "async_session", lambda: session)
    return asyncio.run(oauth._handle_auth_code(form, request=None))


def test_the_code_exchange_stamps_the_marker(monkeypatch):
    """The second issuance path. The `SeqSession` records the stamp instead of
    consuming a canned row, so this asserts the handler took it."""
    from _oauth_grant_fakes import FakeToken  # noqa: F401  (documents the shape)

    code = "a" * 64
    verifier = "v" * 64

    class _Code:
        code_hash = oauth._hash(code)
        client_id = "client123"
        redirect_uri = REGISTERED_URI
        scope = "read"
        code_challenge = oauth._base64url_sha256(verifier)
        code_challenge_method = "S256"
        expires_at = FROZEN_NOW + datetime.timedelta(minutes=10)
        used = False
        user_id = None

    client = FakeClient(redirect_uris=[REGISTERED_URI])
    session = SeqSession([_Code(), client])
    form = {
        "code": code,
        "client_id": "client123",
        "redirect_uri": REGISTERED_URI,
        "code_verifier": verifier,
    }
    monkeypatch.setattr(oauth, "datetime", _FrozenDatetime)
    response = _exchange(session, monkeypatch, form)

    assert response.status_code == 200
    assert session.use_stamps == ["client123"]
    assert session.committed is True


def test_the_code_exchange_and_the_refresh_both_stamp_in_the_source():
    """Both grant types, pinned structurally as well as behaviourally.

    The refresh path's fake is the module's heaviest; asserting the call site
    here keeps the "both grant types" requirement from resting on one of them
    alone, and fails loudly if a refactor drops the stamp from either.
    """
    import inspect

    for handler in (oauth._handle_auth_code, oauth._handle_refresh):
        source = inspect.getsource(handler)
        assert "_stamp_client_use" in source, handler.__name__
        assert "invalid_client" in source, handler.__name__


def test_the_stamp_helper_writes_only_the_marker():
    """`last_used_at` and nothing else — a stamp that also touched `user_id`
    or `scope` would be a silent re-binding on every token issuance."""

    class _Capture:
        def __init__(self):
            self.stmt = None

        async def execute(self, stmt, *_a, **_kw):
            from _oauth_grant_fakes import _Result

            self.stmt = stmt
            return _Result(["client123"])

    capture = _Capture()
    assert asyncio.run(oauth._stamp_client_use(capture, "client123")) is True

    assert capture.stmt.table.name == "oauth_clients"
    assert {col.key for col in capture.stmt._values} == {"last_used_at"}
    assert dict(capture.stmt.compile().params)["client_id_1"] == "client123"


def test_the_model_and_the_migration_agree_on_the_marker():
    """The house rule for a marked unit: byte identical on both sides, or
    `alembic check` reports a pending `alter_column(comment=...)` instead of a
    `downgrade()` that has stopped recognising its own work."""
    import importlib.util
    from pathlib import Path

    path = (
        Path(__file__).resolve().parent.parent
        / "alembic"
        / "versions"
        / "025_oauth_client_last_used.py"
    )
    spec = importlib.util.spec_from_file_location("_migration_025", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.COLUMN_MARKER == OAuthClient._LAST_USED_COLUMN_MARKER
    assert (
        OAuthClient.__table__.c.last_used_at.comment
        == OAuthClient._LAST_USED_COLUMN_MARKER
    )
    assert OAuthClient.__table__.c.last_used_at.nullable is True
    assert OAuthClient.__table__.c.last_used_at.server_default is None
