"""#192 — the transfer refusal record, and the 404 that must not move.

Two halves, because the change has two halves.

**`classify_token_refusal`** (design D8) is a read-only diagnosis with a
*total* precedence. `lookup_token` and `claim_upload` are one filtered query
each — hash, direction, `state = pending`, `expires_at > now` — and that single
query is the linearizability argument for single-use redemption, so neither was
re-shaped to yield a reason. The helper answers afterwards, from the row's own
columns, and the cases that overlap (expired *and* consumed, expired *and* the
wrong direction) are pinned here rather than left to whichever branch the code
happens to test first.

**The routes** must keep the uniform 404 byte-identical across every cause
while the record says which one it was. The response is the sole external
answer; the log is the only place the reason exists. So the assertions are
about three things at once: the bytes did not move, the reason is distinct, and
the diagnosis read happened only where the design says it may — behind an
acquired permit, never on an accepted request, never on a suppressed one, and
never in a way that can turn a 404 into a 500.
"""
from __future__ import annotations

import datetime
import hashlib
import logging
import os
from dataclasses import dataclass, field

import httpx
import pytest
import pytest_asyncio

from src.limiter import limiter
from src.services import security_events, transfer, vault_fs
from src.transfer import routes as transfer_routes

pytestmark = pytest.mark.asyncio


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


# ── capture ─────────────────────────────────────────────────────────────────


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)

    def named(self, event: str) -> list[logging.LogRecord]:
        return [r for r in self.records if r.getMessage() == event]


@pytest.fixture
def captured():
    """Records from `security_events`, with the suppressor forced open.

    One emission attempt has to mean one record for every test that is about
    *what* was recorded; the tests that are about the bound turn this back on
    explicitly.
    """
    handler = _Capture()
    logger = security_events.logger
    logger.addHandler(handler)
    propagate, level = logger.propagate, logger.level
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    security_events.reset_state()
    try:
        with security_events.suppression_disabled():
            yield handler
    finally:
        logger.removeHandler(handler)
        logger.propagate = propagate
        logger.setLevel(level)
        security_events.reset_state()


@pytest.fixture
def captured_with_suppression():
    """The same capture with the real suppressor in the path."""
    handler = _Capture()
    logger = security_events.logger
    logger.addHandler(handler)
    propagate, level = logger.propagate, logger.level
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    security_events.reset_state()
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.propagate = propagate
        logger.setLevel(level)
        security_events.reset_state()


def fields(record) -> dict:
    """The allow-listed extras a record carries, by name."""
    standard = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__)
    standard |= {"message", "asctime", "taskName"}
    return {k: v for k, v in record.__dict__.items() if k not in standard}


# ── the diagnosis, in isolation ─────────────────────────────────────────────


@dataclass
class _Row:
    direction: str = "upload"
    state: str = "pending"
    expires_at: datetime.datetime = field(
        default_factory=lambda: _now() + datetime.timedelta(minutes=10)
    )
    user_id: int | None = 3
    key_id: int | None = 7
    oauth_token_id: int | None = None
    path: str = "Attachments/shot.png"
    vault_root: str = "/vault"


class _DiagSession:
    """A session that answers exactly one SELECT and records every call.

    `writes` stays empty in every test here, which is the assertion that the
    diagnosis takes no decision and changes nothing: a helper that repaired,
    claimed or expired a row on its way to naming a reason would be a second
    admission path, and the reason it is not one is that it only reads.
    """

    def __init__(self, row):
        self.row = row
        self.statements: list[str] = []
        self.writes: list[str] = []
        self.commits = 0

    async def execute(self, statement, *args, **kwargs):
        text = str(statement)
        self.statements.append(text)
        head = text.strip().split(None, 1)[0].upper()
        if head != "SELECT":
            self.writes.append(text)
        return _DiagResult(self.row)

    async def commit(self):
        self.commits += 1


class _DiagResult:
    def __init__(self, row):
        self._row = row

    def scalars(self):
        return self

    def first(self):
        return self._row


async def _classify(row, direction="upload"):
    session = _DiagSession(row)
    refusal = await transfer.classify_token_refusal(
        session, "tok", direction=direction
    )
    return refusal, session


async def test_no_row_for_the_hash_is_an_unknown_token():
    refusal, _ = await _classify(None)
    assert refusal.reason == "unknown_token"
    assert refusal.row is None


async def test_a_row_of_the_other_direction_is_a_direction_mismatch():
    refusal, _ = await _classify(_Row(direction="download"), direction="upload")
    assert refusal.reason == "wrong_direction"
    assert refusal.row is not None


async def test_the_wrong_direction_beats_expiry():
    """Precedence, not evaluation order. Both are true; one answer is right.

    A download token presented to the upload endpoint that has *also* aged out
    is a caller redeeming the wrong capability, and saying "expired" would send
    an operator looking at TTLs.
    """
    row = _Row(direction="download", expires_at=_now() - datetime.timedelta(hours=1))
    refusal, _ = await _classify(row, direction="upload")
    assert refusal.reason == "wrong_direction"


@pytest.mark.parametrize(
    "state,reason",
    [
        ("claimed", "already_claimed"),
        ("completed", "already_completed"),
        ("consumed", "already_consumed"),
    ],
)
async def test_each_terminal_state_names_itself(state, reason):
    refusal, _ = await _classify(_Row(state=state))
    assert refusal.reason == reason


async def test_state_beats_expiry():
    """An expired *and* consumed row reports consumed.

    It was used. That it later aged out is the less interesting of the two
    true facts, and reporting it would hide a replay behind a TTL.
    """
    row = _Row(state="consumed", expires_at=_now() - datetime.timedelta(hours=1))
    refusal, _ = await _classify(row)
    assert refusal.reason == "already_consumed"


async def test_a_pending_row_past_its_ttl_is_expired():
    row = _Row(state="pending", expires_at=_now() - datetime.timedelta(seconds=1))
    refusal, _ = await _classify(row)
    assert refusal.reason == "expired"


async def test_pending_unexpired_and_the_right_direction_is_a_lost_claim():
    """Nothing is wrong with the row, so the conditional UPDATE lost a race."""
    refusal, _ = await _classify(_Row())
    assert refusal.reason == "claim_lost"


async def test_a_naive_expires_at_does_not_raise():
    """A row built by a test — or a future backend — may be naive.

    A naive/aware compare raises rather than returning a wrong answer, and this
    helper runs on a path whose contract is a fixed 404.
    """
    row = _Row(expires_at=datetime.datetime.now() - datetime.timedelta(seconds=1))
    refusal, _ = await _classify(row)
    assert refusal.reason == "expired"


async def test_the_diagnosis_is_one_read_and_writes_nothing():
    _, session = await _classify(_Row(state="consumed"))
    assert len(session.statements) == 1
    assert session.writes == []
    assert session.commits == 0


# ── the routes ──────────────────────────────────────────────────────────────


PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(64)) * 4


@dataclass
class FakeRow:
    vault_root: str
    id: int = 1
    public_id: str = "pub-1"
    direction: str = "upload"
    state: str = "pending"
    path: str = "Attachments/shot.png"
    overwrite: bool = False
    expected_fingerprint: dict | None = None
    key_id: int | None = 7
    oauth_token_id: int | None = None
    user_id: int | None = 11
    claimed_at: datetime.datetime | None = None
    expires_at: datetime.datetime = field(
        default_factory=lambda: _now() + datetime.timedelta(minutes=10)
    )
    completed_at: datetime.datetime | None = None
    size: int | None = None
    sha256: str | None = None
    mime: str | None = None
    actor_kind: str | None = None
    actor_label: str | None = None
    actor_ref: str | None = None


class FakeSession:
    def __init__(self, harness):
        self.harness = harness

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def add(self, obj):
        self.harness.logs.append(obj)

    async def commit(self):
        self.harness.commits += 1

    async def rollback(self):
        pass


class Harness:
    def __init__(self, row: FakeRow, token: str = "tok-secret-value"):
        self.row = row
        self.token = token
        self.identity_ok = True
        self.root_ok = True
        self.claimable = True
        self.released = 0
        self.consumed = 0
        self.logs: list = []
        self.commits = 0
        #: How many times the *diagnosis* read ran. Stands in for a statement
        #: count: the design's claim is that an accepted request issues none
        #: and a suppressed refusal issues none either.
        self.diagnoses = 0
        self.diagnosis_raises = False

    def session(self):
        return FakeSession(self)


def _expired(row: FakeRow) -> bool:
    return _as_aware(row.expires_at) <= _now()


def _as_aware(value):
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.timezone.utc)
    return value


@pytest.fixture
def vault(tmp_path):
    (tmp_path / "Attachments").mkdir()
    vault_fs.reset_filesystem_probe_cache()
    yield tmp_path
    vault_fs.reset_filesystem_probe_cache()


@pytest.fixture(autouse=True)
def _reset_limiter():
    limiter.reset()
    yield
    limiter.reset()


@pytest.fixture
def harness(vault, monkeypatch):
    h = Harness(FakeRow(vault_root=str(vault)))

    async def lookup_token(session, token, *, direction):
        row = h.row
        if token != h.token or row.direction != direction:
            return None
        if row.state != "pending" or _expired(row):
            return None
        return row

    async def resolve_identity_ok(session, row, *, need_write):
        return h.identity_ok

    async def resolve_root_ok(session, row):
        return h.root_ok

    async def claim_upload(session, token):
        if token != h.token or not h.claimable:
            return None
        if h.row.direction != "upload" or h.row.state != "pending" or _expired(h.row):
            return None
        h.row.state = "claimed"
        h.row.claimed_at = h.row.claimed_at or _now()
        return h.row

    async def release_claim(session, row):
        h.released += 1
        row.state = "pending"
        return True

    async def consume(session, row):
        h.consumed += 1
        row.state = "consumed"
        return True

    async def classify_token_refusal(session, token, *, direction):
        """The real precedence, over the harness's single row.

        Patched rather than run against a database because the point of these
        tests is the *route's* behaviour around the diagnosis — when it runs,
        what happens when it fails, and that the response never moves. The
        precedence itself is pinned above, against the real function.
        """
        h.diagnoses += 1
        if h.diagnosis_raises:
            raise RuntimeError("pool exhausted")
        row = h.row
        if token != h.token:
            return transfer.TransferRefusal("unknown_token")
        if row.direction != direction:
            return transfer.TransferRefusal("wrong_direction", row)
        if row.state != "pending":
            return transfer.TransferRefusal(f"already_{row.state}", row)
        if _expired(row):
            return transfer.TransferRefusal("expired", row)
        return transfer.TransferRefusal("claim_lost", row)

    monkeypatch.setattr(transfer, "lookup_token", lookup_token)
    monkeypatch.setattr(transfer, "resolve_identity_ok", resolve_identity_ok)
    monkeypatch.setattr(transfer, "resolve_root_ok", resolve_root_ok)
    monkeypatch.setattr(transfer, "claim_upload", claim_upload)
    monkeypatch.setattr(transfer, "release_claim", release_claim)
    monkeypatch.setattr(transfer, "consume", consume)
    monkeypatch.setattr(transfer, "classify_token_refusal", classify_token_refusal)
    monkeypatch.setattr(transfer_routes, "async_session", h.session)
    return h


@pytest_asyncio.fixture
async def client():
    from src.main import app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("203.0.113.7", 4242)),
        base_url="http://localhost:8000",
    ) as c:
        yield c


def auth(harness) -> dict:
    return {"Authorization": f"Bearer {harness.token}"}


def signature(response: httpx.Response) -> tuple:
    """Everything a caller can observe. Two refusals must be equal here."""
    return (
        response.status_code,
        response.content,
        tuple(sorted(response.headers.items())),
    )


def reason_of(handler, index: int = -1) -> str:
    return fields(handler.named("transfer_refused")[index])["reason"]


# ── the 404 does not move, and the reasons separate ─────────────────────────


async def test_every_refusal_cause_is_one_response_and_a_distinct_reason(
    client, harness, captured
):
    """The headline property: identical bytes out, different reasons recorded.

    `_not_found()` and `NOT_FOUND_BODY` are untouched, so the caller learns
    nothing it did not learn before — and the operator, who learned nothing at
    all before, now learns which of eight things happened.
    """
    baseline = await client.get("/transfer/upload/info")  # no header
    assert baseline.status_code == 404
    assert baseline.json() == {"error": "not found"}
    assert reason_of(captured) == "missing_token"

    cases = [
        ("expired", lambda h: setattr(h.row, "expires_at", _now() - datetime.timedelta(seconds=1))),
        ("already_completed", lambda h: setattr(h.row, "state", "completed")),
        ("already_claimed", lambda h: setattr(h.row, "state", "claimed")),
        ("already_consumed", lambda h: setattr(h.row, "state", "consumed")),
        ("wrong_direction", lambda h: setattr(h.row, "direction", "download")),
        ("credential_invalid", lambda h: setattr(h, "identity_ok", False)),
        ("root_reassigned", lambda h: setattr(h, "root_ok", False)),
        ("path_invalid", lambda h: setattr(h.row, "path", ".obsidian/x.json")),
    ]
    seen = {signature(baseline)}
    reasons = {"missing_token"}
    for expected, mutate in cases:
        harness.row = FakeRow(vault_root=harness.row.vault_root)
        harness.identity_ok = harness.root_ok = True
        mutate(harness)
        response = await client.get("/transfer/upload/info", headers=auth(harness))
        assert response.status_code == 404, expected
        seen.add(signature(response))
        assert reason_of(captured) == expected
        reasons.add(expected)

    harness.row = FakeRow(vault_root=harness.row.vault_root)
    unknown = await client.get(
        "/transfer/upload/info", headers={"Authorization": "Bearer nope"}
    )
    seen.add(signature(unknown))
    assert reason_of(captured) == "unknown_token"
    reasons.add("unknown_token")

    assert len(seen) == 1, "a refusal became distinguishable"
    assert len(reasons) == 10, reasons


async def test_a_missing_header_logs_no_tag(client, harness, captured):
    """`sha:` of the empty string is a constant that reads like a tag."""
    await client.get("/transfer/upload/info")
    record = fields(captured.named("transfer_refused")[-1])
    assert record["reason"] == "missing_token"
    assert "token_tag" not in record
    assert record["route"] == "/transfer/upload/info"
    assert record["method"] == "GET"
    assert record["client_ip"] == "203.0.113.7"


async def test_the_tag_correlates_without_disclosing(client, harness, captured):
    token = "a-long-unknown-capability-token-value"
    for _ in range(3):
        await client.get(
            "/transfer/upload/info", headers={"Authorization": f"Bearer {token}"}
        )
    tags = {fields(r).get("token_tag") for r in captured.named("transfer_refused")}
    assert len(tags) == 1
    tag = tags.pop()
    assert tag == "sha:" + hashlib.sha256(token.encode()).hexdigest()[:8]
    blob = repr([fields(r) for r in captured.records])
    for start in range(len(token) - 11):
        assert token[start : start + 12] not in blob


async def test_no_identity_is_invented_before_a_row_resolves(
    client, harness, captured
):
    await client.get(
        "/transfer/upload/info", headers={"Authorization": "Bearer nope"}
    )
    record = fields(captured.named("transfer_refused")[-1])
    assert record["reason"] == "unknown_token"
    assert "user_id" not in record
    assert "key_id" not in record
    assert "oauth_token_id" not in record


async def test_the_identity_is_carried_where_a_row_did_resolve(
    client, harness, captured
):
    harness.identity_ok = False
    await client.get("/transfer/upload/info", headers=auth(harness))
    record = fields(captured.named("transfer_refused")[-1])
    assert record["reason"] == "credential_invalid"
    assert record["user_id"] == 11
    assert record["key_id"] == 7


# ── the diagnosis runs where, and only where, it may ────────────────────────


async def test_an_accepted_redemption_issues_no_diagnosis(client, harness, captured):
    response = await client.get("/transfer/upload/info", headers=auth(harness))
    assert response.status_code == 200
    assert harness.diagnoses == 0
    assert captured.named("transfer_refused") == []


async def test_a_reason_the_route_already_knows_issues_no_diagnosis(
    client, harness, captured
):
    """`_load_valid`'s own three predicates do not pay for a second read."""
    harness.root_ok = False
    await client.get("/transfer/upload/info", headers=auth(harness))
    assert harness.diagnoses == 0
    assert reason_of(captured) == "root_reassigned"


async def test_a_suppressed_refusal_issues_no_diagnosis(
    client, harness, captured_with_suppression
):
    """The read is gated by the permit, so a flood cannot amplify it.

    Eleven refusals from one address: ten permits, ten reads, ten records —
    and the eleventh pays for nothing at all while still getting its 404.
    """
    for _ in range(security_events.MAX_EVENTS_PER_WINDOW + 1):
        response = await client.get(
            "/transfer/upload/info", headers={"Authorization": "Bearer nope"}
        )
        assert response.status_code == 404
    assert harness.diagnoses == security_events.MAX_EVENTS_PER_WINDOW
    assert (
        len(captured_with_suppression.named("transfer_refused"))
        == security_events.MAX_EVENTS_PER_WINDOW
    )


async def test_rotating_tokens_from_one_address_share_one_allowance(
    client, harness, captured_with_suppression
):
    """The subject is the address, never the tag (D7).

    Keying on anything the caller supplies would hand every fresh bogus bearer
    a fresh allowance, which is exactly the enumeration this bounds.
    """
    for n in range(25):
        response = await client.get(
            "/transfer/upload/info", headers={"Authorization": f"Bearer token-{n}"}
        )
        assert response.status_code == 404
    assert (
        len(captured_with_suppression.named("transfer_refused"))
        <= security_events.MAX_EVENTS_PER_WINDOW
    )


async def test_a_failed_diagnosis_keeps_the_response_and_names_itself(
    client, harness, captured
):
    """A dead connection in the diagnosis may not become a 500.

    The read happens on a path whose whole contract is a fixed 404, and the
    admission decision was already taken before it ran.
    """
    clean = await client.get(
        "/transfer/upload/info", headers={"Authorization": "Bearer nope"}
    )
    harness.diagnosis_raises = True
    broken = await client.get(
        "/transfer/upload/info", headers={"Authorization": "Bearer nope"}
    )
    assert signature(broken) == signature(clean)
    record = fields(captured.named("transfer_refused")[-1])
    assert record["reason"] == "diagnosis_failed"
    assert record["error_type"] == "RuntimeError"
    assert record["token_tag"].startswith("sha:")


# ── the other endpoints ─────────────────────────────────────────────────────


async def test_the_download_endpoints_record_their_own_route(
    client, harness, captured
):
    harness.row.direction = "download"
    await client.get("/transfer/download/info")
    assert fields(captured.named("transfer_refused")[-1])["route"] == (
        "/transfer/download/info"
    )
    harness.identity_ok = False
    await client.get("/transfer/download/file", headers=auth(harness))
    last = fields(captured.named("transfer_refused")[-1])
    assert last["route"] == "/transfer/download/file"
    assert last["reason"] == "credential_invalid"


def _fingerprint(path) -> dict:
    st = os.stat(path)
    return {
        "dev": st.st_dev,
        "inode": st.st_ino,
        "size": st.st_size,
        "mtime_ns": st.st_mtime_ns,
        "ctime_ns": st.st_ctime_ns,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


async def test_the_download_read_path_names_its_three_refusals(
    client, harness, vault, captured
):
    harness.row.direction = "download"

    # (1) the bound file is gone.
    harness.row.expected_fingerprint = {"size": 1}
    await client.get("/transfer/download/file", headers=auth(harness))
    assert reason_of(captured) == "file_unreadable"

    target = vault / "Attachments" / "shot.png"
    target.write_bytes(PNG)

    # (2) the metadata moved under it.
    harness.row.expected_fingerprint = {"dev": 0, "inode": 0, "size": 1}
    await client.get("/transfer/download/file", headers=auth(harness))
    assert reason_of(captured) == "fingerprint_mismatch"

    # (3) an in-place edit that preserved every stat field. The re-hash from
    # the descriptor is the only thing that catches it, and the record has to
    # say so — "fingerprint_mismatch" would send an operator to the wrong
    # question.
    want = _fingerprint(target)
    want["sha256"] = "b" * 64
    harness.row.expected_fingerprint = want
    await client.get("/transfer/download/file", headers=auth(harness))
    assert reason_of(captured) == "content_changed"


async def test_the_upload_route_names_the_publish_gate_refusal(
    client, harness, monkeypatch, captured
):
    """One generic reason, by decision (D8, residual R9).

    `GateHandle` exposes only `ok`, so revocation, downgrade, reassignment and
    cascade delete are indistinguishable here — an accepted limitation, stated
    rather than papered over with a reason the code cannot support.
    """

    async def stream_to_vault(*args, **kwargs):
        raise transfer.PrePublishAborted("gate said no")

    monkeypatch.setattr(transfer, "stream_to_vault", stream_to_vault)
    response = await client.put(
        "/transfer/upload", headers=auth(harness), content=PNG
    )
    assert response.status_code == 404
    assert reason_of(captured) == "prepublish_revalidation_failed"
    assert harness.released == 1


async def test_the_upload_revalidation_splits_into_three(
    client, harness, captured
):
    """Split from an `or` chain — the short-circuit is unchanged."""
    for setter, expected in (
        (lambda: setattr(harness, "identity_ok", False), "credential_invalid"),
        (lambda: setattr(harness, "root_ok", False), "root_reassigned"),
        (lambda: setattr(harness.row, "path", "../outside.png"), "path_invalid"),
    ):
        harness.row = FakeRow(vault_root=harness.row.vault_root)
        harness.identity_ok = harness.root_ok = True
        setter()
        response = await client.put(
            "/transfer/upload", headers=auth(harness), content=PNG
        )
        assert response.status_code == 404
        assert reason_of(captured) == expected


async def test_an_unusable_vault_root_is_recorded_twice_and_answered_once(
    client, harness, monkeypatch, captured
):
    """The operational error *and* the refusal — they are different facts.

    `transfer_root_unusable` is what an operator pages on; `transfer_refused`
    is the refusal ledger entry. Collapsing them would either lose the 404's
    place in the ledger or promote every enumeration attempt to ERROR.
    """

    def check_publication_support(root):
        raise OSError("root vanished")

    monkeypatch.setattr(
        vault_fs, "check_publication_support", check_publication_support
    )
    response = await client.put(
        "/transfer/upload", headers=auth(harness), content=PNG
    )
    assert response.status_code == 404
    assert response.json() == {"error": "not found"}
    unusable = captured.named("transfer_root_unusable")
    assert len(unusable) == 1
    assert unusable[0].levelno == logging.ERROR
    assert fields(unusable[0])["error_type"] == "OSError"
    assert reason_of(captured) == "publication_unsupported"


@pytest.mark.parametrize(
    "raised,event",
    [
        (vault_fs.MountBoundary, "transfer_refused_mount_boundary"),
        (vault_fs.UnsupportedFilesystem, "transfer_refused_unsupported_fs"),
    ],
)
async def test_the_publication_probe_refusals_keep_their_503_and_gain_a_record(
    client, harness, monkeypatch, captured, raised, event
):
    """Migrated to the emitter, so they pass the permit. Nothing else moved.

    These were bare `logger.error` calls, which reach the sink whatever the
    suppressor says — an unbounded flood channel beside the bounded one. The
    503 bodies are unchanged; only the route to the log is.
    """

    def check_publication_support(root):
        raise raised("no")

    monkeypatch.setattr(
        vault_fs, "check_publication_support", check_publication_support
    )
    response = await client.put(
        "/transfer/upload", headers=auth(harness), content=PNG
    )
    assert response.status_code == 503
    records = captured.named(event)
    assert len(records) == 1
    assert records[0].levelno == logging.ERROR
    assert fields(records[0])["route"] == "/transfer/upload"
    assert captured.named("transfer_refused") == []


async def test_the_migrated_transfer_events_pass_the_suppressor(
    client, harness, monkeypatch, captured_with_suppression
):
    """A caller can drive the probe refusal on demand, so it must be bounded."""

    def check_publication_support(root):
        raise vault_fs.MountBoundary("no")

    monkeypatch.setattr(
        vault_fs, "check_publication_support", check_publication_support
    )
    for _ in range(security_events.MAX_EVENTS_PER_WINDOW + 4):
        # The route's own per-IP limit is 10/minute and would answer 429 before
        # the suppressor was reached. Clearing it isolates the bound under
        # test: this assertion is about the *log*, not about slowapi.
        limiter.reset()
        harness.row = FakeRow(vault_root=harness.row.vault_root)
        response = await client.put(
            "/transfer/upload", headers=auth(harness), content=PNG
        )
        assert response.status_code == 503
    emitted = captured_with_suppression.named("transfer_refused_mount_boundary")
    assert len(emitted) == security_events.MAX_EVENTS_PER_WINDOW
    summaries = captured_with_suppression.named("events_suppressed")
    assert summaries == [] or fields(summaries[-1])["count"] > 0


# ── the accepted set did not move ───────────────────────────────────────────


async def test_a_valid_token_still_redeems_with_the_logging_in_place(
    client, harness, captured
):
    """The whole point of not re-shaping `lookup_token`/`claim_upload`."""
    response = await client.get("/transfer/upload/info", headers=auth(harness))
    assert response.status_code == 200
    assert response.json()["path"] == harness.row.path
    assert captured.records == []


# ── the claim cleanup may not change a decided response ────────────────────


async def test_a_failed_release_keeps_the_404_and_keeps_the_record(
    client, harness, monkeypatch, captured
):
    """Cleanup is bookkeeping; the answer was already chosen.

    `release_claim` is one conditional UPDATE, but it takes a pooled
    connection — and the pool is 5 + 10 on one worker, so it is exactly the
    thing that fails under the load a refusal burst arrives with. When it did,
    the decided 404 became a 500 and the `transfer_refused` record went with
    it: the caller learned *more* from the failure than from the refusal, which
    is the one thing the uniform 404 exists to prevent (design D22).
    """

    async def exploding_release(session, row):
        raise RuntimeError("QueuePool limit of size 5 overflow 10 reached")

    monkeypatch.setattr(transfer, "release_claim", exploding_release)
    harness.identity_ok = False

    response = await client.put(
        "/transfer/upload", headers=auth(harness), content=PNG
    )

    assert response.status_code == 404, "a cleanup failure moved the answer"
    assert response.json() == {"error": "not found"}
    assert reason_of(captured) == "credential_invalid", (
        "the refusal record must survive the cleanup that follows the decision"
    )
    failures = captured.named("transfer_claim_release_failed")
    assert len(failures) == 1
    assert failures[0].levelno == logging.ERROR
    carried = fields(failures[0])
    assert carried["error_type"] == "RuntimeError"
    assert carried["route"] == "/transfer/upload"
    assert carried["method"] == "PUT"
    # Class only — a SQLAlchemy error's text quotes the statement, and the
    # engine hides its parameters; neither belongs in a field.
    assert "QueuePool" not in repr(carried)


async def test_a_failed_release_after_the_gate_refuses_still_answers_404(
    client, harness, monkeypatch, captured
):
    """The site Codex named: `PrePublishAborted` releases and *then* refuses.

    Ordering makes this the worst of the release sites — a raise here loses the
    refusal record that has not been emitted yet, not just the cleanup.
    """

    async def stream_to_vault(*args, **kwargs):
        raise transfer.PrePublishAborted("gate said no")

    async def exploding_release(session, row):
        raise RuntimeError("the pool is gone")

    monkeypatch.setattr(transfer, "stream_to_vault", stream_to_vault)
    monkeypatch.setattr(transfer, "release_claim", exploding_release)

    response = await client.put(
        "/transfer/upload", headers=auth(harness), content=PNG
    )

    assert response.status_code == 404
    assert reason_of(captured) == "prepublish_revalidation_failed"
    assert len(captured.named("transfer_claim_release_failed")) == 1
