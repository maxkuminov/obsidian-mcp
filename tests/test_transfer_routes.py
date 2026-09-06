"""Route-level tests for `/transfer/*` — everything provable without Postgres.

The database is stubbed here on purpose. What these tests are about is the
*route's* behaviour: which status code, which body, which headers, what lands on
disk, and what never does. The three properties that are genuinely about a
transaction boundary — the concurrent claim, the `FOR UPDATE` publish barrier,
and `ON DELETE CASCADE` — cannot be faked and live in
`tests/integration/test_transfer_pg.py`, which fails rather than skips.

The filesystem is real: every upload publishes into a `tmp_path` vault and the
assertions read it back.
"""
from __future__ import annotations

import ast
import asyncio
import datetime
import errno
import hashlib
import inspect
import logging
import os
import pathlib
import re
from dataclasses import dataclass, field
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

from src.config import settings
from src.limiter import limiter
from src.services import rate_limits, refusals, transfer, vault_fs
from src.transfer import routes as transfer_routes

pytestmark = pytest.mark.asyncio


PNG = (
    b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + bytes(range(64)) * 8
)


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


# ── stubs ───────────────────────────────────────────────────────────────────


@dataclass
class FakeRow:
    """Exactly the columns the routes read off a `transfer_tokens` row."""

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
    user_id: int | None = None
    claimed_at: datetime.datetime | None = None
    expires_at: datetime.datetime = field(default_factory=lambda: _now() + datetime.timedelta(minutes=10))
    completed_at: datetime.datetime | None = None
    size: int | None = None
    sha256: str | None = None
    mime: str | None = None
    # Recorded at mint from the minting request's credential (issue #92,
    # migration 017) and copied onto the redemption's `usage_logs` row, because
    # this request carries a capability and has no credential of its own to
    # name. Defaulted None here — the pre-017 row shape — so the route is
    # exercised on the path where there is nothing to copy as well as the one
    # where there is.
    actor_kind: str | None = None
    actor_label: str | None = None
    actor_ref: str | None = None


class FakeSession:
    """Enough of `AsyncSession` for the routes: `add`, `commit`, `begin`."""

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
        self.harness.rollbacks += 1

    def begin(self):
        return _FakeBegin(self.harness)


class _FakeBegin:
    def __init__(self, harness):
        self.harness = harness

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.harness.commits += 1
        else:
            self.harness.rollbacks += 1
            # Anything the gate wrote is discarded, exactly as a rollback would.
            self.harness.logs = [
                item for item in self.harness.logs if item not in self.harness.gate_logs
            ]
        return False


class Harness:
    """Canned answers for every transfer-service call the routes make."""

    def __init__(self, row: FakeRow, token: str = "tok-secret-value"):
        self.row = row
        self.token = token
        self.identity_ok = True
        #: What `resolve_identity` hands back beside its verdict — the minting
        #: credential row. Only the OAuth branch of `minting_principal` reads
        #: it (for `grant_id`), so it stays `None` for the API-key default.
        self.credential = None
        self.root_ok = True
        self.lock_ok = True
        self.locked_ok = True
        self.claimable = True
        self.released = 0
        self.consumed = 0
        self.completed: list[tuple] = []
        self.logs: list = []
        self.gate_logs: list = []
        self.commits = 0
        self.rollbacks = 0

    def session(self):
        return FakeSession(self)


def _expired(row: FakeRow) -> bool:
    return row.expires_at <= _now()


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

    async def resolve_identity(session, row, *, need_write):
        """The pair the upload route reads: the verdict and the credential.

        `upload()` takes the credential off this call rather than looking it up
        again, because the OAuth branch's `grant_id` is what the write bucket
        is keyed on. The harness hands back whatever `h.credential` is set to —
        `None` for the API-key rows every other test uses, since that branch
        reads `key_id` off the token and never touches the credential.
        """
        return h.identity_ok, (h.credential if h.identity_ok else None)

    async def resolve_root_ok(session, row):
        return h.root_ok

    async def claim_upload(session, token):
        if token != h.token or not h.claimable:
            return None
        if h.row.direction != "upload" or h.row.state != "pending" or _expired(h.row):
            return None
        h.row.state = "claimed"
        if h.row.claimed_at is None:
            h.row.claimed_at = _now()
        return h.row

    async def release_claim(session, row):
        h.released += 1
        row.state = "pending"
        return True

    async def consume(session, row):
        h.consumed += 1
        row.state = "consumed"
        return True

    async def complete_upload(session, row, size, sha256, mime, *, commit=True):
        h.completed.append((size, sha256, mime))
        row.state = "completed"
        row.size, row.sha256, row.mime = size, sha256, mime
        return True

    async def lock_for_publish(session, token_id):
        if not h.lock_ok:
            return None
        return transfer.LockedRows(token=h.row, credential=object(), user=None)

    def locked_rows_ok(locked, *, need_write):
        return h.locked_ok

    monkeypatch.setattr(transfer, "lookup_token", lookup_token)
    monkeypatch.setattr(transfer, "resolve_identity_ok", resolve_identity_ok)
    monkeypatch.setattr(transfer, "resolve_identity", resolve_identity)
    monkeypatch.setattr(transfer, "resolve_root_ok", resolve_root_ok)
    monkeypatch.setattr(transfer, "claim_upload", claim_upload)
    monkeypatch.setattr(transfer, "release_claim", release_claim)
    monkeypatch.setattr(transfer, "consume", consume)
    monkeypatch.setattr(transfer, "complete_upload", complete_upload)
    monkeypatch.setattr(transfer, "lock_for_publish", lock_for_publish)
    monkeypatch.setattr(transfer, "locked_rows_ok", locked_rows_ok)
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


def temp_files(directory) -> list:
    return sorted(p.name for p in directory.iterdir() if p.name.startswith(".tmp-"))


# ── 4.2 the static pages ────────────────────────────────────────────────────


@pytest.mark.parametrize("path", ["/transfer/upload", "/transfer/download"])
async def test_pages_are_static_and_nonce_guarded(client, harness, path):
    response = await client.get(path)
    assert response.status_code == 200
    csp = response.headers["content-security-policy"]
    nonce = re.search(r"script-src 'nonce-([^']+)'", csp).group(1)
    assert f"style-src 'nonce-{nonce}'" in csp
    assert "default-src 'none'" in csp
    assert "connect-src 'self'" in csp
    assert "form-action 'none'" in csp
    body = response.text
    assert f'<script nonce="{nonce}">' in body
    assert f'<style nonce="{nonce}">' in body
    # Self-contained: nothing may be fetched from another origin.
    assert "http://" not in body.replace("http://www.w3.org", "")
    assert "cdn" not in body.lower()


@pytest.mark.parametrize("path", ["/transfer/upload", "/transfer/download"])
async def test_pages_never_carry_the_token_or_the_bound_path(client, harness, path):
    """The page is one static document; the token lives in the fragment.

    An XSS-shaped filename cannot reach it because the filename never reaches
    the server-side render at all.
    """
    harness.row.path = 'Attachments/</script><img src=x onerror=alert(1)>".png'
    plain = (await client.get(path)).text
    with_token = await client.get(
        f"{path}?token={harness.token}", headers=auth(harness)
    )
    assert with_token.status_code == 200
    # Query string ignored, token never echoed, no path rendered server-side.
    assert harness.token not in with_token.text
    assert "onerror" not in with_token.text
    scrub = lambda t: re.sub(r'nonce="[A-Za-z0-9_-]+"', 'nonce="X"', t)
    assert scrub(plain) == scrub(with_token.text)


# ── 4.1 / 4.3 the uniform 404 matrix ────────────────────────────────────────


async def test_the_upload_page_states_the_mode_it_will_act_in(client, harness):
    """The consent step must distinguish a replace from a create.

    This page press is the only session-less write path in the app, and the
    human pressing it is the consent authority for a destructive write. Until
    the Mode row existed, an `overwrite=True` link rendered identically to one
    that creates a new file.
    """
    body = (await client.get("/transfer/upload")).text
    assert 'id="mode"' in body
    assert "Replaces the existing file at" in body
    assert "Creates a new file" in body

    # The path reaches the DOM through `textContent`, never `innerHTML` — the
    # JSON→DOM step is the page's only injection surface, and the mode row is
    # the second place the bound path is rendered.
    assert '$("path").textContent = info.path;' in body
    assert '$("mode").textContent = overwrite' in body
    # `.innerHTML` never appears as a property access anywhere on the page
    # (the bare word survives only in the comment saying not to use it).
    assert ".innerHTML" not in body

    # The destructive copy and button label live *inside* the `overwrite`
    # branch: a create-a-new-file link that shouted REPLACE would train the
    # human to ignore the warning on the link that means it.
    branch = body.index("if (overwrite) {")
    otherwise = body.index("} else {", branch)
    for destructive in (
        '$("send").textContent = "Replace file";',
        '"This will REPLACE the existing file at "',
    ):
        assert branch < body.index(destructive) < otherwise
    assert body.index('"Choose a file, then press Upload.') > otherwise

    # Still self-contained: the mode is decided from the JSON, not a new asset.
    assert "http://" not in body.replace("http://www.w3.org", "")
    assert "cdn" not in body.lower()


async def test_info_returns_the_bound_metadata(client, harness):
    response = await client.get("/transfer/upload/info", headers=auth(harness))
    assert response.status_code == 200
    body = response.json()
    assert body["path"] == harness.row.path
    assert body["max_bytes"] == settings.max_file_write_bytes
    assert body["expires_at"].startswith(str(_now().year))
    # The page renders the Mode row from this field.
    assert body["overwrite"] is False


async def test_info_reports_an_overwrite_token_as_such(client, harness):
    harness.row.overwrite = True
    response = await client.get("/transfer/upload/info", headers=auth(harness))
    assert response.json()["overwrite"] is True


async def test_download_info_reports_the_minted_size(client, harness):
    harness.row.direction = "download"
    harness.row.expected_fingerprint = {"size": 4096, "sha256": "a" * 64}
    response = await client.get("/transfer/download/info", headers=auth(harness))
    assert response.status_code == 200
    assert response.json()["size"] == 4096


async def _bodies(client, harness, mutate) -> httpx.Response:
    mutate(harness)
    return await client.get("/transfer/upload/info", headers=auth(harness))


async def test_every_unusable_token_gets_one_indistinguishable_404(client, harness):
    """Missing, unknown, expired, completed, revoked, downgraded, reassigned."""
    baseline = await client.get("/transfer/upload/info")  # no header at all
    assert baseline.status_code == 404

    cases = {
        "expired": lambda h: setattr(h.row, "expires_at", _now() - datetime.timedelta(seconds=1)),
        "completed": lambda h: setattr(h.row, "state", "completed"),
        "claimed": lambda h: setattr(h.row, "state", "claimed"),
        "consumed": lambda h: setattr(h.row, "state", "consumed"),
        "dead-credential": lambda h: setattr(h, "identity_ok", False),
        "root-reassigned": lambda h: setattr(h, "root_ok", False),
        "hidden-path": lambda h: setattr(h.row, "path", ".obsidian/x.json"),
        "escaping-path": lambda h: setattr(h.row, "path", "../outside.png"),
    }
    seen = set()
    for name, mutate in cases.items():
        fresh_row = FakeRow(vault_root=harness.row.vault_root)
        harness.row = fresh_row
        harness.identity_ok = harness.root_ok = True
        response = await _bodies(client, harness, mutate)
        assert response.status_code == 404, name
        seen.add(response.content)

    # An unknown token, with a valid row sitting in the database.
    harness.row = FakeRow(vault_root=harness.row.vault_root)
    harness.identity_ok = harness.root_ok = True
    unknown = await client.get(
        "/transfer/upload/info", headers={"Authorization": "Bearer nope"}
    )
    assert unknown.status_code == 404
    seen.add(unknown.content)
    seen.add(baseline.content)
    assert len(seen) == 1, "the refusals are distinguishable"
    assert baseline.json() == {"error": "not found"}


async def test_a_download_token_cannot_redeem_the_upload_endpoint(client, harness):
    harness.row.direction = "download"
    assert (await client.get("/transfer/upload/info", headers=auth(harness))).status_code == 404
    assert (await client.put("/transfer/upload", headers=auth(harness), content=b"x")).status_code == 404


async def test_token_in_the_query_string_is_ignored(client, harness):
    response = await client.get(f"/transfer/upload/info?token={harness.token}")
    assert response.status_code == 404
    assert response.json() == {"error": "not found"}


async def test_token_in_the_path_is_ignored(client, harness):
    response = await client.get(f"/transfer/upload/info/{harness.token}")
    assert response.status_code == 404


async def test_a_non_bearer_authorization_header_is_not_a_token(client, harness):
    response = await client.get(
        "/transfer/upload/info", headers={"Authorization": f"Basic {harness.token}"}
    )
    assert response.status_code == 404


# ── 4.6 middleware routing and the method matrix ────────────────────────────


async def test_api_key_middleware_does_not_intercept_transfer(client, harness):
    """A bearer that is not an API key must reach the transfer route.

    `APIKeyMiddleware` wraps only the `/mcp` mount and `RootMCPProxyMiddleware`
    only rewrites a bare `/`, so no prefix exclusion is needed — but "no
    exclusion needed" is a claim about routing, and this is the assertion that
    keeps it true.
    """
    response = await client.get("/transfer/upload/info", headers=auth(harness))
    assert response.status_code == 200

    rejected = await client.get(
        "/transfer/upload/info", headers={"Authorization": "Bearer omcp_not_a_key"}
    )
    assert rejected.status_code == 404
    assert rejected.json() == {"error": "not found"}
    assert "www-authenticate" not in rejected.headers


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/transfer/upload"),
        ("DELETE", "/transfer/upload"),
        ("PATCH", "/transfer/upload"),
        ("OPTIONS", "/transfer/upload"),
        ("PUT", "/transfer/download"),
        ("POST", "/transfer/upload/info"),
        ("PUT", "/transfer/download/file"),
        ("POST", "/transfer/download/file"),
        ("DELETE", "/transfer/download/info"),
    ],
)
async def test_method_matrix(client, harness, method, path):
    response = await client.request(method, path, headers=auth(harness))
    assert response.status_code == 405


async def test_error_responses_never_echo_the_authorization_header(client, harness):
    harness.identity_ok = False
    response = await client.get(
        "/transfer/upload/info", headers=auth(harness) | {"X-Trace": "t"}
    )
    assert harness.token not in response.text
    for value in response.headers.values():
        assert harness.token not in value


# ── 4.4 PUT ─────────────────────────────────────────────────────────────────


async def test_upload_round_trip(client, harness, vault):
    response = await client.put("/transfer/upload", headers=auth(harness), content=PNG)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["path"] == "Attachments/shot.png"
    assert body["size"] == len(PNG)
    assert body["sha256"] == hashlib.sha256(PNG).hexdigest()
    assert body["mime"] == "image/png"

    written = vault / "Attachments" / "shot.png"
    assert written.read_bytes() == PNG
    assert temp_files(vault / "Attachments") == []

    # Completion and the usage-log row were written by the gate, from the same
    # numbers the response reports.
    assert harness.completed == [(len(PNG), body["sha256"], "image/png")]
    assert harness.row.state == "completed"
    assert [log.tool for log in harness.logs] == ["upload_file"]
    log = harness.logs[0]
    assert log.key_id == harness.row.key_id
    assert harness.token not in str(log.params)


async def test_the_usage_row_carries_the_actor_recorded_at_mint(client, harness, vault):
    """This request has no credential to name (issue #92).

    It authenticates with a capability, so the actor was recorded on the token
    at mint and is copied onto the usage row here — which is what keeps the
    line attributed after the operator deletes the credential and the joins go
    NULL. A route that dropped it would still pass every other case in this
    file, because they read the FKs.
    """
    harness.row.actor_kind = "oauth"
    harness.row.actor_label = "Claude Desktop"
    harness.row.actor_ref = "client-abc"

    response = await client.put("/transfer/upload", headers=auth(harness), content=PNG)
    assert response.status_code == 200, response.text

    log = harness.logs[0]
    assert (log.actor_kind, log.actor_label, log.actor_ref) == (
        "oauth",
        "Claude Desktop",
        "client-abc",
    )
    assert harness.token not in str(log.params)


def arm_close_faults(monkeypatch, *, limit: int = 3) -> dict:
    """Make the first `limit` `os.close` calls *after* publication fail.

    Exactly three descriptors are closed once the bytes are in place — the
    destination parent, the staging directory, the vault root — and none of
    them can change whether the file exists. Arming only after `publish`
    returns, and capping the failures, keeps the window to those three.
    """
    import errno

    state = {"armed": False, "failures": 0}
    real_close = os.close
    real_publish = vault_fs.publish

    def arming_publish(*args, **kwargs):
        outcome = real_publish(*args, **kwargs)
        if outcome.published:
            state["armed"] = True
        return outcome

    def faulty_close(fd):
        if state["armed"] and state["failures"] < limit:
            state["failures"] += 1
            real_close(fd)
            raise OSError(errno.EIO, "Input/output error")
        return real_close(fd)

    monkeypatch.setattr(vault_fs, "publish", arming_publish)
    monkeypatch.setattr(os, "close", faulty_close)
    return state


async def test_a_failing_close_after_publication_keeps_the_token_completed(
    client, harness, vault, monkeypatch
):
    """The route must never see a post-publication failure as pre-publication.

    A bare `os.close` on the publish path used to be able to raise `EIO` after
    the file had landed. That reached the route's catch-all, which treats every
    non-`PostPublishFailure` error as demonstrably pre-publication and releases
    the claim — handing back a replayable token over a path that already holds
    the uploaded file. The token must end `completed` (or at worst `claimed`);
    `pending` is the wrong answer.
    """
    state = arm_close_faults(monkeypatch)
    response = await client.put("/transfer/upload", headers=auth(harness), content=PNG)
    monkeypatch.undo()

    assert state["failures"] == 3, "the injected close failures never fired"
    assert response.status_code == 200, response.text
    assert (vault / "Attachments" / "shot.png").read_bytes() == PNG
    assert harness.row.state == "completed"
    assert harness.released == 0
    assert harness.row.state != "pending"


async def test_a_failing_close_plus_a_failing_commit_still_strands_the_claim(
    client, harness, vault, monkeypatch
):
    """Stacked failures still resolve to `claimed`, never `pending`."""

    async def exploding(session, row, size, sha256, mime, *, commit=True):
        raise RuntimeError("database went away")

    monkeypatch.setattr(transfer, "complete_upload", exploding)
    arm_close_faults(monkeypatch)

    with pytest.raises(transfer.PostPublishFailure):
        await client.put("/transfer/upload", headers=auth(harness), content=PNG)
    monkeypatch.undo()

    assert (vault / "Attachments" / "shot.png").read_bytes() == PNG
    assert harness.released == 0
    assert harness.consumed == 0
    assert harness.row.state == "claimed"


async def test_upload_creates_missing_parent_folders(client, harness, vault):
    harness.row.path = "Inbox/2026/scan.png"
    response = await client.put("/transfer/upload", headers=auth(harness), content=PNG)
    assert response.status_code == 200
    assert (vault / "Inbox" / "2026" / "scan.png").read_bytes() == PNG


async def test_body_is_not_read_before_the_claim(client, harness, vault):
    """An unknown token with a huge body costs a header parse, not a disk write."""
    harness.claimable = False
    consumed = 0

    async def body():
        nonlocal consumed
        for _ in range(64):
            consumed += 1
            yield b"x" * 65536

    response = await client.put("/transfer/upload", headers=auth(harness), content=body())
    assert response.status_code == 404
    assert response.json() == {"error": "not found"}
    # Not one chunk was pulled off the wire: the claim runs to completion first,
    # so an unknown token cannot make us spool gigabytes.
    assert consumed == 0
    assert temp_files(vault / "Attachments") == []
    assert not (vault / "Attachments" / "shot.png").exists()


async def test_declared_oversize_is_refused_before_anything_opens(client, harness, vault, monkeypatch):
    monkeypatch.setattr(settings, "max_file_write_bytes", 64)
    response = await client.put(
        "/transfer/upload", headers=auth(harness), content=b"y" * 4096
    )
    assert response.status_code == 413
    assert temp_files(vault / "Attachments") == []
    assert harness.released == 1
    assert harness.row.state == "pending"


async def test_undeclared_oversize_aborts_mid_stream(client, harness, vault, monkeypatch):
    monkeypatch.setattr(settings, "max_file_write_bytes", 4096)

    async def body():
        for _ in range(8):
            yield b"z" * 4096

    response = await client.put("/transfer/upload", headers=auth(harness), content=body())
    assert response.status_code == 413
    assert not (vault / "Attachments" / "shot.png").exists()
    assert temp_files(vault / "Attachments") == []
    assert harness.released == 1
    assert harness.row.state == "pending"


async def test_target_that_appeared_since_mint_is_never_clobbered(client, harness, vault):
    victim = vault / "Attachments" / "shot.png"
    victim.write_bytes(b"i was here first")

    response = await client.put("/transfer/upload", headers=auth(harness), content=PNG)
    assert response.status_code == 409
    assert victim.read_bytes() == b"i was here first"
    assert temp_files(vault / "Attachments") == []
    assert harness.released == 1
    assert harness.row.state == "pending"


async def test_overwrite_token_with_a_null_fingerprint_requires_absence(client, harness, vault):
    """The null fingerprint is an expected-*absence* sentinel, not a skip."""
    harness.row.overwrite = True
    harness.row.expected_fingerprint = None
    victim = vault / "Attachments" / "shot.png"
    victim.write_bytes(b"appeared after the mint")

    response = await client.put("/transfer/upload", headers=auth(harness), content=PNG)
    assert response.status_code == 409
    assert victim.read_bytes() == b"appeared after the mint"


async def test_overwrite_token_rejects_a_changed_target(client, harness, vault):
    target = vault / "Attachments" / "shot.png"
    target.write_bytes(b"original content")
    st = target.stat()
    harness.row.overwrite = True
    harness.row.expected_fingerprint = {
        "dev": st.st_dev,
        "inode": st.st_ino,
        "size": st.st_size,
        "mtime_ns": st.st_mtime_ns,
        "ctime_ns": st.st_ctime_ns,
        # Same length, same metadata, different bytes: only re-hashing the
        # descriptor can tell, which is precisely what must happen.
        "sha256": hashlib.sha256(b"different       ").hexdigest(),
    }

    response = await client.put("/transfer/upload", headers=auth(harness), content=PNG)
    assert response.status_code == 409
    assert target.read_bytes() == b"original content"
    assert harness.released == 1


async def test_overwrite_token_replaces_a_matching_target(client, harness, vault):
    target = vault / "Attachments" / "shot.png"
    target.write_bytes(b"original content")
    st = target.stat()
    harness.row.overwrite = True
    harness.row.expected_fingerprint = {
        "dev": st.st_dev,
        "inode": st.st_ino,
        "size": st.st_size,
        "mtime_ns": st.st_mtime_ns,
        "ctime_ns": st.st_ctime_ns,
        "sha256": hashlib.sha256(b"original content").hexdigest(),
    }

    response = await client.put("/transfer/upload", headers=auth(harness), content=PNG)
    assert response.status_code == 200
    assert target.read_bytes() == PNG


async def test_a_symlink_that_escapes_the_vault_is_refused_before_any_io(
    client, harness, vault, tmp_path
):
    """Caught by the path guard, so the answer is the uniform 404, not a 409."""
    outside = tmp_path.parent / f"outside-{vault.name}"
    outside.mkdir(exist_ok=True)
    (vault / "Attachments" / "linked").symlink_to(outside, target_is_directory=True)
    harness.row.path = "Attachments/linked/shot.png"

    response = await client.put("/transfer/upload", headers=auth(harness), content=PNG)
    assert response.status_code == 404
    assert response.json() == {"error": "not found"}
    assert not (outside / "shot.png").exists()
    assert harness.released == 1


async def test_a_symlinked_ancestor_inside_the_vault_is_refused(client, harness, vault):
    """Stays inside the root, so it is the anchored walk that must refuse it."""
    (vault / "Elsewhere").mkdir()
    (vault / "Attachments" / "linked").symlink_to(
        vault / "Elsewhere", target_is_directory=True
    )
    harness.row.path = "Attachments/linked/shot.png"

    response = await client.put("/transfer/upload", headers=auth(harness), content=PNG)
    assert response.status_code == 409
    assert not (vault / "Elsewhere" / "shot.png").exists()
    assert harness.released == 1


async def test_a_symlinked_target_inside_the_vault_is_refused(client, harness, vault):
    victim = vault / "Elsewhere.txt"
    victim.write_bytes(b"do not touch")
    (vault / "Attachments" / "shot.png").symlink_to(victim)

    response = await client.put("/transfer/upload", headers=auth(harness), content=PNG)
    assert response.status_code == 409
    assert victim.read_bytes() == b"do not touch"
    assert (vault / "Attachments" / "shot.png").is_symlink()


async def test_the_deadline_consumes_the_token(client, harness, vault, monkeypatch):
    monkeypatch.setattr(settings, "transfer_max_upload_seconds", 1)
    harness.row.claimed_at = _now() - datetime.timedelta(seconds=30)

    async def body():
        yield b"a" * 16
        await asyncio.sleep(0.05)
        yield b"b" * 16

    response = await client.put("/transfer/upload", headers=auth(harness), content=body())
    assert response.status_code == 408
    assert harness.consumed == 1
    assert harness.released == 0
    assert harness.row.state == "consumed"
    assert temp_files(vault / "Attachments") == []


async def test_a_gate_delayed_past_the_deadline_publishes_nothing(
    client, harness, vault, monkeypatch
):
    """The deadline is re-checked inside the locked gate, not only in `_drain`.

    `_drain` bounds the *body*, but the gate runs afterwards and can wait
    arbitrarily long on `SELECT … FOR UPDATE` — behind another publisher, or a
    migration. A body that finished a moment inside the deadline would
    otherwise publish (an overwrite included) long after the capability
    expired, while `check_upload` was already answering `unknown` for it.

    The clock is stepped from inside `lock_for_publish`, i.e. exactly between
    drain completion and the publish. The refusal must be the existing
    `Timeout`, because the state machine says a request that ran past its
    deadline *consumes* its token: a retry mints afresh rather than replaying a
    link whose window is gone.
    """
    stub = transfer.lock_for_publish
    stepped = _now() + datetime.timedelta(hours=1)

    async def slow_lock(session, token_id):
        monkeypatch.setattr(transfer, "now_utc", lambda: stepped)
        return await stub(session, token_id)

    monkeypatch.setattr(transfer, "lock_for_publish", slow_lock)

    response = await client.put("/transfer/upload", headers=auth(harness), content=PNG)

    assert response.status_code == 408
    assert not (vault / "Attachments" / "shot.png").exists()
    assert temp_files(vault / "Attachments") == []
    assert harness.completed == []
    assert harness.consumed == 1
    assert harness.released == 0
    assert harness.row.state == "consumed"


async def test_a_gate_delayed_past_the_deadline_leaves_an_overwrite_target_alone(
    client, harness, vault, monkeypatch
):
    """The destructive case: the incumbent must survive untouched."""
    target = vault / "Attachments" / "shot.png"
    target.write_bytes(b"the original bytes")
    harness.row.overwrite = True
    harness.row.expected_fingerprint = None  # not read: we never reach publish

    stub = transfer.lock_for_publish
    stepped = _now() + datetime.timedelta(hours=1)

    async def slow_lock(session, token_id):
        monkeypatch.setattr(transfer, "now_utc", lambda: stepped)
        return await stub(session, token_id)

    monkeypatch.setattr(transfer, "lock_for_publish", slow_lock)

    response = await client.put("/transfer/upload", headers=auth(harness), content=PNG)

    assert response.status_code == 408
    assert target.read_bytes() == b"the original bytes"
    assert temp_files(vault / "Attachments") == []
    assert harness.row.state == "consumed"


async def test_an_idle_stall_consumes_the_token(client, harness, monkeypatch):
    """The idle timeout is 30 s in production; the route's mapping is what matters."""

    async def stalled(*args, **kwargs):
        raise transfer.Timeout("Upload stalled for more than 30s")

    monkeypatch.setattr(transfer, "stream_to_vault", stalled)
    response = await client.put("/transfer/upload", headers=auth(harness), content=b"x")
    assert response.status_code == 408
    assert harness.consumed == 1
    assert harness.released == 0


async def test_a_client_disconnect_releases_the_claim(client, harness, monkeypatch):
    from starlette.requests import ClientDisconnect

    async def disconnected(*args, **kwargs):
        raise ClientDisconnect()

    monkeypatch.setattr(transfer, "stream_to_vault", disconnected)
    with pytest.raises(ClientDisconnect):
        await client.put("/transfer/upload", headers=auth(harness), content=b"x")
    assert harness.released == 1
    assert harness.consumed == 0


async def test_a_failure_after_publication_leaves_the_token_claimed(
    client, harness, vault, monkeypatch
):
    """The one case where the claim must NOT be released.

    From an unexpected error we cannot prove nothing was published, and a
    replayable token is the worse of the two failures.
    """

    async def exploding(session, row, size, sha256, mime, *, commit=True):
        raise RuntimeError("database went away")

    monkeypatch.setattr(transfer, "complete_upload", exploding)
    with pytest.raises(transfer.PostPublishFailure):
        await client.put("/transfer/upload", headers=auth(harness), content=PNG)
    assert harness.released == 0
    assert harness.consumed == 0
    assert harness.row.state == "claimed"


def _fail_directory_fsync(monkeypatch) -> list[int]:
    """Fail every *directory* `fsync`; leave the payload flush working.

    The post-publication half of #97. What it stands in for is a filesystem or
    a device that will not make a directory entry durable — the file is at the
    path, and whether it survives a crash is unknown.
    """
    import stat as _stat

    failed: list[int] = []
    real = os.fsync

    def maybe_fail(fd):
        if _stat.S_ISDIR(os.fstat(fd).st_mode):
            failed.append(fd)
            raise OSError(errno.EIO, "input/output error")
        return real(fd)

    monkeypatch.setattr(os, "fsync", maybe_fail)
    return failed


async def test_a_failing_directory_flush_strands_the_claim(
    client, harness, vault, monkeypatch
):
    """The route's strand-or-release decision, driven end to end (#97).

    The flush runs after `on_published` has recorded the publication, so
    `stream_to_vault` converts it to `PostPublishFailure` and the route leaves
    the token `claimed`. Were it to escape as a bare `OSError` — which it would
    if the flush ran before the callback — the catch-all would read it as
    "demonstrably pre-publication" and release the claim, handing back a
    replayable token over a path that already holds the uploaded file.
    """
    # Prime the publication probe first. It now exercises a directory flush
    # itself (#97 task 2.5a), so arming the fault before it would be refused at
    # the probe — correct behaviour, but a different test. What is under test
    # here is the environment that fails *later*, at the real destination.
    vault_fs.check_publication_support(harness.row.vault_root)
    failed = _fail_directory_fsync(monkeypatch)

    with pytest.raises(transfer.PostPublishFailure):
        await client.put("/transfer/upload", headers=auth(harness), content=PNG)
    monkeypatch.undo()

    assert failed, "no directory flush was attempted"
    # The bytes are in the vault — which is precisely why `pending` would lie.
    assert (vault / "Attachments" / "shot.png").read_bytes() == PNG
    assert harness.row.state == "claimed"
    assert harness.released == 0
    assert harness.consumed == 0
    # And nothing was recorded `completed`: `check_upload` must not answer with
    # a sha256 for a file whose durability we cannot vouch for.
    assert harness.completed == []
    assert temp_files(vault / vault_fs.STAGING_DIR) == []


async def test_a_failing_payload_flush_releases_the_claim(
    client, harness, vault, monkeypatch
):
    """The mirror image, and the reason the payload flush sits before the gate.

    Nothing has been published, so stranding the capability for its whole TTL
    would cost the human a re-mint over a transfer that never touched the vault.
    """
    def boom(fd):
        raise OSError(errno.EIO, "input/output error")

    monkeypatch.setattr(transfer, "_fsync_payload", boom)

    with pytest.raises(OSError) as exc:
        await client.put("/transfer/upload", headers=auth(harness), content=PNG)

    assert not isinstance(exc.value, transfer.PostPublishFailure)
    assert harness.released == 1
    assert harness.consumed == 0
    assert harness.row.state == "pending"
    assert not (vault / "Attachments" / "shot.png").exists()
    assert temp_files(vault / vault_fs.STAGING_DIR) == []


async def test_a_full_disk_mid_stream_releases_the_claim(
    client, harness, vault, monkeypatch
):
    """An ENOSPC while staging is demonstrably before publication.

    Leaving the token `claimed` there would strand the capability for its whole
    TTL over a transfer that never touched the vault. `claimed`-forever is
    reserved for `PostPublishFailure`, where we genuinely cannot tell.
    """
    import errno

    def no_space(fd, data):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(transfer, "_write_all", no_space)
    with pytest.raises(OSError):
        await client.put("/transfer/upload", headers=auth(harness), content=PNG)

    assert harness.released == 1
    assert harness.consumed == 0
    assert harness.row.state == "pending"
    assert not (vault / "Attachments" / "shot.png").exists()
    assert temp_files(vault / vault_fs.STAGING_DIR) == []


async def test_a_gate_entry_failure_releases_the_claim(client, harness, vault, monkeypatch):
    """The gate raising on the way *in* means the publish never happened."""

    async def exploding(session, token_id):
        raise RuntimeError("could not reach the database")

    monkeypatch.setattr(transfer, "lock_for_publish", exploding)
    with pytest.raises(RuntimeError):
        await client.put("/transfer/upload", headers=auth(harness), content=PNG)

    assert harness.released == 1
    assert harness.row.state == "pending"
    assert not (vault / "Attachments" / "shot.png").exists()
    assert temp_files(vault / vault_fs.STAGING_DIR) == []


async def test_the_gate_refusing_publishes_nothing(client, harness, vault):
    harness.locked_ok = False
    response = await client.put("/transfer/upload", headers=auth(harness), content=PNG)
    assert response.status_code == 404
    assert not (vault / "Attachments" / "shot.png").exists()
    assert temp_files(vault / "Attachments") == []
    assert harness.completed == []
    assert harness.logs == []


async def test_a_cascaded_token_row_publishes_nothing(client, harness, vault):
    harness.lock_ok = False
    response = await client.put("/transfer/upload", headers=auth(harness), content=PNG)
    assert response.status_code == 404
    assert not (vault / "Attachments" / "shot.png").exists()


async def test_identity_revoked_between_mint_and_put(client, harness, vault):
    harness.identity_ok = False
    response = await client.put("/transfer/upload", headers=auth(harness), content=PNG)
    assert response.status_code == 404
    assert response.json() == {"error": "not found"}
    assert not (vault / "Attachments" / "shot.png").exists()
    assert harness.released == 1


async def test_no_token_ever_reaches_the_logs(client, harness, vault, caplog):
    """A failing upload with log capture on: the capability must not be in there."""
    (vault / "Attachments" / "shot.png").write_bytes(b"conflict me")
    with caplog.at_level(logging.DEBUG):
        response = await client.put("/transfer/upload", headers=auth(harness), content=PNG)
    assert response.status_code == 409
    assert harness.token not in caplog.text
    assert harness.token not in response.text


# ── the session scope, the queue, and the markdown cap (#208) ───────────────
#
# The route used to keep one session open around the whole handler: the claim
# committed, but the two re-validation queries immediately autobegan a fresh
# transaction that was never committed, so a pooled connection stayed checked
# out across the semaphore wait *and* the whole body stream. 15 slow uploads
# from one tenant then pinned all 5 + 10 connections and every other caller got
# a 500. The pool arithmetic itself is only provable against a real engine and
# lives in `tests/integration/test_transfer_pg.py`; what is provable here is the
# route's half — what it reads off the detached row, and how it answers the two
# new ways a slot wait can end.


@pytest.fixture(autouse=True)
def _clear_upload_semaphores():
    transfer._upload_semaphores.clear()
    yield
    transfer._upload_semaphores.clear()


async def _hold_every_slot(monkeypatch) -> asyncio.Semaphore:
    """Occupy the loop's upload semaphore so the next stream must queue."""
    monkeypatch.setattr(settings, "transfer_max_concurrent_uploads", 1)
    transfer._upload_semaphores.clear()
    sem = transfer.upload_semaphore()
    await sem.acquire()
    assert sem.locked()
    return sem


def _with_slot_timeout(monkeypatch, seconds: float) -> None:
    """Shorten the route's (defaulted) slot timeout without faking the wait.

    The route deliberately does not pass `slot_timeout` — 30 s is the service's
    default and the route has no business overriding it — so the only honest way
    to test the real wait in under 30 s is to inject the shorter bound at the
    call. Everything else about the wait, including which exception comes back,
    is the production path.
    """
    real = transfer.stream_to_vault

    async def quick(*args, **kwargs):
        kwargs["slot_timeout"] = seconds
        return await real(*args, **kwargs)

    monkeypatch.setattr(transfer, "stream_to_vault", quick)


async def test_a_full_queue_is_a_503_that_releases_the_claim(
    client, harness, vault, monkeypatch
):
    """Busy, not expired: `Retry-After`, `pending` again, nothing staged.

    The distinction from the 408 below is the whole point. Nothing was streamed
    and the capability's own window is still open, so the very same link may be
    retried — which is what `release_claim` says and what `consume` would have
    made impossible.
    """
    await _hold_every_slot(monkeypatch)
    _with_slot_timeout(monkeypatch, 0.1)

    response = await client.put("/transfer/upload", headers=auth(harness), content=PNG)

    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"
    assert harness.released == 1
    assert harness.consumed == 0
    assert harness.row.state == "pending"
    assert not (vault / "Attachments" / "shot.png").exists()
    assert temp_files(vault / "Attachments") == []
    # A 503 is only ever reached *after* the claim and the re-validation
    # succeeded, so it tells an attacker nothing a 404 was hiding.
    assert response.json() != {"error": "not found"}


async def test_a_deadline_already_overrun_before_the_wait_consumes_the_token(
    client, harness, vault, monkeypatch
):
    """408 beats 503, and no slot is taken on the way out.

    Unreachable in production — `claim_upload` guarantees a positive remainder
    the instant phase 1 ends — so it is produced the endorsed way, by advancing
    `transfer.now_utc`, the single clock every transfer deadline is measured
    against. A monotonic stand-in could not express it at all.
    """
    sem = await _hold_every_slot(monkeypatch)
    stepped = _now() + datetime.timedelta(hours=1)
    monkeypatch.setattr(transfer, "now_utc", lambda: stepped)

    response = await client.put("/transfer/upload", headers=auth(harness), content=PNG)

    assert response.status_code == 408
    assert harness.consumed == 1
    assert harness.released == 0
    assert harness.row.state == "consumed"
    assert not (vault / "Attachments" / "shot.png").exists()
    assert temp_files(vault / "Attachments") == []
    # It never queued for, nor took, a streaming slot.
    assert sem.locked()


@pytest.mark.parametrize("suffix", [".md", ".MD", ".Md"])
async def test_a_markdown_upload_is_held_to_the_note_cap(
    client, harness, vault, monkeypatch, suffix
):
    """No transport may land a `.md` the note tools would refuse.

    The indexer treats any `.md` as a note, so the cap follows the extension and
    not the tool. Case-insensitively: `Notes/Big.MD` is a note on every
    filesystem this runs on.
    """
    monkeypatch.setattr(transfer_routes, "MAX_NOTE_BYTES", 64)
    monkeypatch.setattr(settings, "max_file_write_bytes", 1_000_000)
    harness.row.path = f"Attachments/note{suffix}"

    response = await client.put(
        "/transfer/upload", headers=auth(harness), content=b"m" * 4096
    )

    assert response.status_code == 413
    assert "64" in response.json()["error"]
    assert harness.released == 1
    assert harness.row.state == "pending"
    assert not (vault / "Attachments" / f"note{suffix}").exists()
    assert temp_files(vault / "Attachments") == []


async def test_the_note_cap_binds_only_markdown(client, harness, vault, monkeypatch):
    """The same body at the same settings, to a `.png`, is fine."""
    monkeypatch.setattr(transfer_routes, "MAX_NOTE_BYTES", 64)
    monkeypatch.setattr(settings, "max_file_write_bytes", 1_000_000)

    response = await client.put(
        "/transfer/upload", headers=auth(harness), content=PNG
    )
    assert response.status_code == 200
    assert (vault / "Attachments" / "shot.png").read_bytes() == PNG


async def test_a_lowered_file_cap_still_wins_for_markdown(
    client, harness, vault, monkeypatch
):
    """`min(MAX_NOTE_BYTES, MAX_FILE_WRITE_BYTES)` — the smaller one binds.

    An operator who lowers `MAX_FILE_WRITE_BYTES` below the note cap must not
    discover that markdown became the *more* permissive destination.
    """
    monkeypatch.setattr(transfer_routes, "MAX_NOTE_BYTES", 1_000_000)
    monkeypatch.setattr(settings, "max_file_write_bytes", 64)
    harness.row.path = "Attachments/note.md"

    response = await client.put(
        "/transfer/upload", headers=auth(harness), content=b"m" * 4096
    )
    assert response.status_code == 413
    assert "64" in response.json()["error"]


async def test_upload_info_advertises_the_cap_the_route_enforces(
    client, harness, monkeypatch
):
    """The consent page prints this number; it must be the one that binds."""
    monkeypatch.setattr(transfer_routes, "MAX_NOTE_BYTES", 64)
    monkeypatch.setattr(settings, "max_file_write_bytes", 1_000_000)

    harness.row.path = "Attachments/note.md"
    payload = (await client.get("/transfer/upload/info", headers=auth(harness))).json()
    assert payload["max_bytes"] == 64

    harness.row.path = "Attachments/shot.png"
    payload = (await client.get("/transfer/upload/info", headers=auth(harness))).json()
    assert payload["max_bytes"] == 1_000_000


async def test_the_engine_writes_its_pool_timeout_down():
    """The bound a pool-exhaustion outage is measured against is in the config.

    30 s is SQLAlchemy's own default, so this asserts documentation rather than
    behaviour — which is the point. An assessment of #208 had to read
    SQLAlchemy's source to learn how long every other tenant waits before the
    500s start; a number that load-bearing does not belong in another library's
    defaults. The behaviour that the value actually binds is asserted against a
    real pool in `tests/integration/test_transfer_pg.py`.
    """
    from src.database import engine as app_engine

    assert app_engine.pool.timeout() == 30


class _DetachedRow:
    """A `FakeRow` proxy that records every read made after phase 1 closes.

    Standing in for the real thing: after phase 1 commits and exits, the route
    holds a **detached** ORM instance. Reads of plain columns are fine
    (`expire_on_commit=False` leaves them populated and `close()` expunges
    without expiring); a read of anything lazy would raise
    `DetachedInstanceError` in production and 500 the request. A fake session
    cannot raise that, so the reads are recorded instead and checked against the
    real mapper.
    """

    def __init__(self, row):
        object.__setattr__(self, "_row", row)
        object.__setattr__(self, "detached", False)
        object.__setattr__(self, "reads_after_detach", set())

    def __getattr__(self, name):
        if object.__getattribute__(self, "detached"):
            object.__getattribute__(self, "reads_after_detach").add(name)
        return getattr(object.__getattribute__(self, "_row"), name)

    def __setattr__(self, name, value):
        if name == "detached":
            object.__setattr__(self, name, value)
        else:
            setattr(object.__getattribute__(self, "_row"), name, value)


async def test_the_transfer_token_row_carries_nothing_lazy():
    """The premise of using the claimed row detached, asserted on the model.

    Add a relationship or a deferred column to `TransferToken` and the upload
    route starts issuing a lazy load on a closed session — a 500 on the write
    path, from a model edit that looks unrelated. This is the tripwire.
    """
    import sqlalchemy as sa
    from sqlalchemy.orm import ColumnProperty

    from src.models.db import TransferToken

    mapper = sa.inspect(TransferToken)
    assert not mapper.relationships
    for prop in mapper.attrs:
        assert isinstance(prop, ColumnProperty), prop
        assert prop.deferred is False, prop


async def test_only_plain_loaded_columns_are_read_after_phase_one(
    client, harness, vault, monkeypatch
):
    """Whatever the route touches after the session closes must be a column."""
    import sqlalchemy as sa

    from src.models.db import TransferToken

    watched = _DetachedRow(harness.row)

    async def claim_upload(session, token):
        if token != harness.token or not harness.claimable:
            return None
        harness.row.state = "claimed"
        harness.row.claimed_at = _now()
        return watched

    class ClosingSession(FakeSession):
        async def __aexit__(self, *exc):
            watched.detached = True
            return await super().__aexit__(*exc)

    monkeypatch.setattr(transfer, "claim_upload", claim_upload)
    monkeypatch.setattr(
        transfer_routes, "async_session", lambda: ClosingSession(harness)
    )

    response = await client.put("/transfer/upload", headers=auth(harness), content=PNG)
    assert response.status_code == 200, response.text
    assert (vault / "Attachments" / "shot.png").read_bytes() == PNG

    reads = watched.reads_after_detach
    # It really did keep using the row after the session went away.
    assert {"path", "vault_root"} <= reads, reads
    columns = set(sa.inspect(TransferToken).columns.keys())
    assert reads <= columns, reads - columns


# ── 4b the write bucket at redemption (#194) ────────────────────────────────
#
# `PUT /transfer/upload` publishes vault bytes without ever passing through
# `_tracked`, so bounding only the eight write tools would leave the write rate
# escapable: mint capabilities at the general rate, then redeem them without
# limit. The redemption therefore spends the write bucket of the principal that
# **minted** the capability (design D5a), and these tests are about the four
# properties that makes load-bearing — where the charge sits, whose allowance
# it comes from, what a refusal does to the claim, and what it costs to derive.


class _Events(logging.Handler):
    """Every `security_events` record, so a refusal can be read back by name."""

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)

    def named(self, event: str) -> list[logging.LogRecord]:
        return [r for r in self.records if r.getMessage() == event]


@pytest.fixture
def events():
    handler = _Events()
    logger = logging.getLogger("security_events")
    logger.addHandler(handler)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)


def _fields(record) -> dict:
    """The allow-listed extras a record carries, by name."""
    standard = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__)
    standard |= {"message", "asctime", "taskName"}
    return {k: v for k, v in record.__dict__.items() if k not in standard}


@pytest.fixture
def write_bucket(monkeypatch):
    """A narrow write bucket on a clock the test drives.

    Narrow because the per-IP `BYTES_LIMIT` on this route is 10/minute while
    the shipped write burst is 15: a burst driven over HTTP at the configured
    numbers would be refused by slowapi first and prove nothing about *this*
    control. Two tokens refilling at one a second makes the third redemption
    the interesting one and the refill observable without sleeping.

    Returns the clock, so a test can advance it and watch the bucket refill.
    """
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(settings, "mcp_write_rate_limit_per_minute", 60)
    monkeypatch.setattr(settings, "mcp_write_rate_limit_burst", 2)
    monkeypatch.setattr(
        rate_limits, "time", SimpleNamespace(monotonic=lambda: clock.now)
    )
    rate_limits.reset_state_for_tests()
    return clock


@pytest.fixture
def charges(monkeypatch):
    """Every `(principal, scope)` the route charges, in order.

    Wraps rather than replaces `take`, so the bucket still decides — a spy that
    always admitted would make the refusal tests pass for the wrong reason.
    """
    seen: list[tuple] = []
    real = rate_limits.take

    def spy(principal, scope):
        seen.append((principal, scope))
        return real(principal, scope)

    monkeypatch.setattr(transfer_routes.rate_limits, "take", spy)
    return seen


def _rearm(harness, name: str) -> None:
    """Put the harness's single row back to a fresh, pending capability.

    One `FakeRow` stands in for the whole table here, so redeeming twice means
    resetting the state a completed upload left behind and pointing the next
    redemption at a path that does not exist yet — otherwise the second PUT
    would be answered by the no-clobber publish rather than by the bucket.
    """
    harness.row.state = "pending"
    harness.row.claimed_at = None
    harness.row.completed_at = None
    harness.row.size = harness.row.sha256 = harness.row.mime = None
    harness.row.path = f"Attachments/{name}.png"


async def test_redemptions_above_the_write_rate_are_refused(
    client, harness, vault, write_bucket, charges, events
):
    """The third redemption inside the burst is a 429, and nothing else moves.

    Every assertion here is one clause of the requirement: the status and the
    `Retry-After`, the claim **released** rather than consumed, no bytes staged
    and none published, and no completion or usage row for a redemption that
    did not happen.
    """
    for name in ("first", "second"):
        _rearm(harness, name)
        ok = await client.put("/transfer/upload", headers=auth(harness), content=PNG)
        assert ok.status_code == 200, ok.text

    _rearm(harness, "third")
    response = await client.put(
        "/transfer/upload", headers=auth(harness), content=PNG
    )

    assert response.status_code == 429
    assert int(response.headers["retry-after"]) >= 1
    # Not the uniform 404: the token is fine and stays redeemable. Telling a
    # legitimate redeemer their link had died would make them mint another,
    # which is the one behaviour a rate control must not provoke.
    assert response.json() != {"error": "not found"}
    assert "Retry-After" in response.text or "retry" in response.text.lower()

    assert harness.released == 1
    assert harness.consumed == 0
    assert harness.row.state == "pending"
    assert not (vault / "Attachments" / "third.png").exists()
    assert temp_files(vault / "Attachments") == []
    # Two uploads happened, so two completions and two usage rows — the refused
    # third contributed neither.
    assert len(harness.completed) == 2
    assert [log.tool for log in harness.logs] == ["upload_file", "upload_file"]

    # Three redemptions, three charges, all against the write bucket.
    assert charges == [(("api_key", 7), refusals.SCOPE_PRINCIPAL_WRITE)] * 3

    record = events.named("transfer_refused_rate_limited")
    assert len(record) == 1
    carried = _fields(record[0])
    assert carried["reason"] == refusals.SCOPE_PRINCIPAL_WRITE
    assert carried["route"] == "/transfer/upload"
    assert carried["method"] == "PUT"
    assert carried["key_id"] == 7
    assert harness.token not in str(carried)


async def test_the_charge_happens_before_a_single_body_byte_is_read(
    client, harness, vault, write_bucket
):
    """A refused redemption costs a header parse, not a spooled body.

    The same property `test_body_is_not_read_before_the_claim` pins for the
    claim: a control that only refused *after* the bytes arrived would have let
    the write it is bounding consume the bandwidth and the staging directory
    anyway.
    """
    rate_limits.take(("api_key", 7), refusals.SCOPE_PRINCIPAL_WRITE)
    rate_limits.take(("api_key", 7), refusals.SCOPE_PRINCIPAL_WRITE)
    chunks = 0

    async def body():
        nonlocal chunks
        for _ in range(64):
            chunks += 1
            yield b"x" * 65536

    response = await client.put(
        "/transfer/upload", headers=auth(harness), content=body()
    )

    assert response.status_code == 429
    assert chunks == 0
    assert temp_files(vault / "Attachments") == []
    assert not (vault / "Attachments" / "shot.png").exists()


async def test_the_same_capability_is_redeemable_once_the_bucket_refills(
    client, harness, vault, write_bucket
):
    """A refusal is a deferral, not a revocation.

    This is why the refusal releases the claim instead of consuming it — the
    same reason the `QueueTimeout` 503 does. The capability the server declined
    to serve *right now* is a promise still outstanding, and the identical link
    has to work once the tokens are back.
    """
    for name in ("first", "second"):
        _rearm(harness, name)
        assert (
            await client.put("/transfer/upload", headers=auth(harness), content=PNG)
        ).status_code == 200

    _rearm(harness, "third")
    refused = await client.put(
        "/transfer/upload", headers=auth(harness), content=PNG
    )
    assert refused.status_code == 429

    # The claim went back to `pending`, so nothing is re-armed here: this is
    # the very same capability, redeemed again after the bucket refilled.
    write_bucket.now += 5.0
    accepted = await client.put(
        "/transfer/upload", headers=auth(harness), content=PNG
    )

    assert accepted.status_code == 200, accepted.text
    assert (vault / "Attachments" / "third.png").read_bytes() == PNG
    assert harness.row.state == "completed"


async def test_the_bucket_belongs_to_the_minter_not_the_presenter(
    client, harness, vault, write_bucket, charges
):
    """A capability cannot be used to spend another principal's allowance.

    The request carries no credential of its own — that is what a capability
    *is* — so the only identity available is the token's, and it is the minting
    one. Draining the minter's bucket refuses its own links while a capability
    minted by a different key redeems unaffected.
    """
    _rearm(harness, "drained")
    while rate_limits.take(("api_key", 7), refusals.SCOPE_PRINCIPAL_WRITE)[0]:
        pass
    charges.clear()  # the drain went through the same spy; only the route counts

    refused = await client.put(
        "/transfer/upload", headers=auth(harness), content=PNG
    )
    assert refused.status_code == 429
    assert charges == [(("api_key", 7), refusals.SCOPE_PRINCIPAL_WRITE)]

    # A capability minted by another key, presented by the same client over the
    # same connection: a different bucket, still full.
    harness.row.key_id = 9
    _rearm(harness, "other-minter")
    accepted = await client.put(
        "/transfer/upload", headers=auth(harness), content=PNG
    )
    assert accepted.status_code == 200, accepted.text
    assert charges[-1] == (("api_key", 9), refusals.SCOPE_PRINCIPAL_WRITE)
    assert (vault / "Attachments" / "other-minter.png").read_bytes() == PNG


async def test_an_oauth_minted_capability_is_charged_to_its_grant(
    client, harness, vault, write_bucket, charges
):
    """The OAuth key is the **grant**, not the access token (issue #64, D1).

    Refreshing an access token mints a new `oauth_tokens` row for the same
    grant; keying on the row would hand out a fresh allowance for the price of
    a refresh, on the one surface that writes vault bytes without a tool call.
    """
    harness.row.key_id = None
    harness.row.oauth_token_id = 41
    harness.credential = SimpleNamespace(grant_id="grant-abc")
    _rearm(harness, "oauth")

    response = await client.put(
        "/transfer/upload", headers=auth(harness), content=PNG
    )

    assert response.status_code == 200, response.text
    assert charges == [(("oauth", "grant-abc"), refusals.SCOPE_PRINCIPAL_WRITE)]

    # The same grant, a *different* access token row: one bucket, not two.
    harness.row.oauth_token_id = 42
    _rearm(harness, "oauth-refreshed")
    assert (
        await client.put("/transfer/upload", headers=auth(harness), content=PNG)
    ).status_code == 200
    assert charges[-1] == (("oauth", "grant-abc"), refusals.SCOPE_PRINCIPAL_WRITE)


async def test_deriving_the_principal_issues_no_query_of_its_own():
    """`minting_principal` has no session, so it cannot have a round trip.

    The requirement is "no additional query", and the cheapest way to keep it
    true forever is a signature that makes the alternative impossible: a plain
    synchronous function over the row and the credential `resolve_identity` has
    already loaded. If a later edit needed a lookup it would have to change
    this signature, and this assertion is what would notice.
    """
    assert not inspect.iscoroutinefunction(transfer.minting_principal)
    parameters = list(inspect.signature(transfer.minting_principal).parameters)
    assert parameters == ["row", "credential"]
    assert "session" not in parameters


async def test_the_oauth_principal_costs_exactly_the_identity_check(monkeypatch):
    """Statement-counting on the real `resolve_identity`: two, as before.

    The credential row and the owner row — precisely what `resolve_identity_ok`
    read before this change. Loading the `OAuthToken` a second time to reach
    `grant_id` would have added a third to the phase-one window that #208 exists
    to keep short, which is why the verdict and the row travel together.
    """
    from src.models.db import OAuthToken, User

    cred = OAuthToken(
        id=41,
        grant_id="grant-abc",
        user_id=3,
        scope="offline_access readwrite",
        revoked=False,
        expires_at=_now() + datetime.timedelta(hours=1),
    )
    owner = User(id=3, is_active=True)

    class _Scalar:
        def __init__(self, value):
            self._value = value

        def scalar_one_or_none(self):
            return self._value

    class _CountingSession:
        def __init__(self):
            self.statements = 0
            self.answers = [cred, owner]

        async def execute(self, _statement):
            self.statements += 1
            return _Scalar(self.answers.pop(0))

    row = SimpleNamespace(key_id=None, oauth_token_id=41, user_id=3)
    session = _CountingSession()

    ok, credential = await transfer.resolve_identity(session, row, need_write=True)

    assert ok is True
    assert session.statements == 2
    assert transfer.minting_principal(row, credential) == ("oauth", "grant-abc")
    # And nothing was read to say it.
    assert session.statements == 2


async def test_a_token_naming_neither_credential_is_exempt_rather_than_refused():
    """No principal means *exempt*, the same rule `_tracked` applies.

    The single-user / sandbox shape. There is no allowance to charge such a row
    to, and inventing one — or refusing outright — would turn a bookkeeping gap
    into an outage. The row is unusable for an unrelated reason anyway
    (`_load_credential` cannot resolve it), which is what keeps this from being
    a way to mint an unbounded capability.
    """
    row = SimpleNamespace(key_id=None, oauth_token_id=None, user_id=None)
    assert transfer.minting_principal(row, None) is None
    assert rate_limits.take(None, refusals.SCOPE_PRINCIPAL_WRITE) == (True, 0)


async def test_a_capability_with_no_credential_still_redeems(
    client, harness, vault, write_bucket, charges
):
    """The exemption, end to end: charged to nobody, refused by nothing."""
    harness.row.key_id = None
    harness.row.oauth_token_id = None

    for index in range(4):  # past the burst of two, and still admitted
        _rearm(harness, f"ownerless-{index}")
        response = await client.put(
            "/transfer/upload", headers=auth(harness), content=PNG
        )
        assert response.status_code == 200, response.text

    assert charges == [(None, refusals.SCOPE_PRINCIPAL_WRITE)] * 4


async def test_the_upload_metadata_read_consumes_nothing(
    client, harness, write_bucket, charges
):
    """Only the redemption is charged; nothing else on the upload side is.

    `/transfer/upload/info` is the route half of `check_upload`. Billing both
    the mint and the redemption would count one write twice, and charging a
    metadata read would let a capability be exhausted without a byte ever being
    written.
    """
    assert (await client.get("/transfer/upload")).status_code == 200
    assert (
        await client.get("/transfer/upload/info", headers=auth(harness))
    ).status_code == 200

    assert charges == []


async def test_a_download_redemption_consumes_nothing(
    client, harness, download, write_bucket, charges
):
    """The *write* bucket bounds writes. A download moves no vault bytes."""
    assert (
        await client.get("/transfer/download/info", headers=auth(harness))
    ).status_code == 200
    assert (
        await client.get("/transfer/download/file", headers=auth(harness))
    ).status_code == 200

    assert charges == []


async def test_the_mint_tools_are_not_write_class_and_import_still_is():
    """The other half of "no double charge", read off `_tracked` itself.

    `request_upload` / `request_download` / `check_upload` create or read
    capability rows and write no vault bytes, so they stay unclassed and the
    redemption is the single charge for the transfer. `import_from_url` is the
    opposite case and must keep its own: it writes bytes *at the tool call*,
    with no redemption to charge later, so its token comes from Slice A's gate
    and this route never sees it.
    """
    # Anchored on this file, not the working directory: a sibling test that
    # chdirs would otherwise turn this into a spurious failure.
    source = pathlib.Path(__file__).resolve().parents[1] / "src" / "mcp_server" / "tools.py"
    tree = ast.parse(source.read_text())
    classes: dict[str, bool] = {}
    for node in ast.walk(tree):
        for decorator in getattr(node, "decorator_list", []):
            if not isinstance(decorator, ast.Call):
                continue
            func = decorator.func
            if not isinstance(func, ast.Name) or func.id != "_tracked":
                continue
            name = decorator.args[0].value
            classes[name] = any(
                keyword.arg == "write_class" and keyword.value.value
                for keyword in decorator.keywords
            )

    assert classes["request_upload"] is False
    assert classes["request_download"] is False
    assert classes["check_upload"] is False
    assert classes["import_from_url"] is True


# ── 4.5 download ────────────────────────────────────────────────────────────


@pytest.fixture
def download(harness, vault):
    """A download token bound to a real file, with a real fingerprint."""
    target = vault / "Attachments" / "spec.pdf"
    target.write_bytes(b"%PDF-1.4\n" + b"payload " * 200)
    st = target.stat()
    harness.row.direction = "download"
    harness.row.path = "Attachments/spec.pdf"
    harness.row.expected_fingerprint = {
        "dev": st.st_dev,
        "inode": st.st_ino,
        "size": st.st_size,
        "mtime_ns": st.st_mtime_ns,
        "ctime_ns": st.st_ctime_ns,
        "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
    }
    return target


async def test_download_round_trip(client, harness, download):
    response = await client.get("/transfer/download/file", headers=auth(harness))
    assert response.status_code == 200
    assert response.content == download.read_bytes()
    assert response.headers["content-type"].startswith("application/pdf")
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["accept-ranges"] == "none"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert 'filename="spec.pdf"' in response.headers["content-disposition"]
    assert "filename*=UTF-8''spec.pdf" in response.headers["content-disposition"]
    assert [log.tool for log in harness.logs] == ["download_file"]


async def test_download_is_multi_use_within_the_ttl(client, harness, download):
    for _ in range(3):
        assert (await client.get("/transfer/download/file", headers=auth(harness))).status_code == 200


async def test_gzip_leaves_the_download_alone(client, harness, download):
    headers = auth(harness) | {"Accept-Encoding": "gzip"}
    got = await client.get("/transfer/download/file", headers=headers)
    head = await client.head("/transfer/download/file", headers=headers)
    assert got.headers["content-encoding"] == "identity"
    assert head.headers["content-encoding"] == "identity"
    assert got.headers["content-length"] == head.headers["content-length"]
    assert int(got.headers["content-length"]) == download.stat().st_size
    assert head.content == b""


async def test_range_is_ignored(client, harness, download):
    response = await client.get(
        "/transfer/download/file", headers=auth(harness) | {"Range": "bytes=0-99"}
    )
    assert response.status_code == 200
    assert response.content == download.read_bytes()
    assert response.headers["accept-ranges"] == "none"


async def test_download_404s_when_the_file_was_replaced(client, harness, download):
    replacement = download.with_suffix(".tmp")
    replacement.write_bytes(b"%PDF-1.4\nsomething else entirely")
    os.replace(replacement, download)
    response = await client.get("/transfer/download/file", headers=auth(harness))
    assert response.status_code == 404
    assert response.json() == {"error": "not found"}


async def test_download_404s_on_an_in_place_edit_that_kept_the_metadata(
    client, harness, download
):
    """Equal length, restored mtime — only the re-hash from the descriptor sees it."""
    original = download.read_bytes()
    edited = b"%PDF-1.4\n" + b"CHANGED " * 200
    assert len(edited) == len(original)
    with open(download, "r+b") as fh:
        fh.write(edited)
    st = download.stat()
    harness.row.expected_fingerprint = {
        "dev": st.st_dev,
        "inode": st.st_ino,
        "size": st.st_size,
        "mtime_ns": st.st_mtime_ns,
        "ctime_ns": st.st_ctime_ns,
        "sha256": hashlib.sha256(original).hexdigest(),
    }
    response = await client.get("/transfer/download/file", headers=auth(harness))
    assert response.status_code == 404


async def test_download_404s_when_the_file_is_gone(client, harness, download):
    download.unlink()
    assert (await client.get("/transfer/download/file", headers=auth(harness))).status_code == 404


async def test_download_refuses_a_symlinked_target(client, harness, vault, download):
    secret = vault / "secret.txt"
    secret.write_bytes(b"not yours")
    download.unlink()
    download.symlink_to(secret)
    response = await client.get("/transfer/download/file", headers=auth(harness))
    assert response.status_code == 404


@pytest.mark.skipif(not os.path.exists("/proc/self/fd"), reason="needs /proc")
async def test_refused_downloads_do_not_leak_descriptors(client, harness, download):
    """Every early return past the open must close the file.

    The fingerprint compare happens with the file already open, and a token
    bound to a file that has since changed can be retried up to the rate limit,
    so a descriptor leaked on that path is slow fd exhaustion of the whole
    process — not a one-off.
    """
    harness.row.expected_fingerprint = dict(harness.row.expected_fingerprint)
    harness.row.expected_fingerprint["size"] += 1  # guaranteed mismatch

    before = len(os.listdir("/proc/self/fd"))
    for _ in range(20):
        limiter.reset()  # the byte endpoints allow 10/minute; this is about fds
        response = await client.get("/transfer/download/file", headers=auth(harness))
        assert response.status_code == 404
    assert len(os.listdir("/proc/self/fd")) <= before + 2


@pytest.mark.skipif(not os.path.exists("/proc/self/fd"), reason="needs /proc")
async def test_head_downloads_do_not_leak_descriptors(client, harness, download):
    """`HEAD` returns the headers and no body, so nothing streams the fd out."""
    before = len(os.listdir("/proc/self/fd"))
    for _ in range(20):
        limiter.reset()
        assert (
            await client.head("/transfer/download/file", headers=auth(harness))
        ).status_code == 200
    assert len(os.listdir("/proc/self/fd")) <= before + 2


async def test_disposition_strips_crlf_and_quotes(client, harness, vault):
    nasty = 'we"ird\r\nInjected: yes.txt'
    (vault / "Attachments" / nasty).write_bytes(b"hello")
    st = (vault / "Attachments" / nasty).stat()
    harness.row.direction = "download"
    harness.row.path = f"Attachments/{nasty}"
    harness.row.expected_fingerprint = {
        "dev": st.st_dev,
        "inode": st.st_ino,
        "size": st.st_size,
        "mtime_ns": st.st_mtime_ns,
        "ctime_ns": st.st_ctime_ns,
        "sha256": hashlib.sha256(b"hello").hexdigest(),
    }
    response = await client.get("/transfer/download/file", headers=auth(harness))
    assert response.status_code == 200
    disposition = response.headers["content-disposition"]
    assert "\r" not in disposition and "\n" not in disposition
    assert 'filename="weirdInjected: yes.txt"' in disposition
    assert "injected" not in {k.lower() for k in response.headers}


# ── 4.6 rate limits ─────────────────────────────────────────────────────────


async def test_info_is_rate_limited(harness):
    from src.main import app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("198.51.100.9", 1)),
        base_url="http://localhost:8000",
    ) as c:
        statuses = [
            (await c.get("/transfer/upload/info", headers=auth(harness))).status_code
            for _ in range(32)
        ]
    assert statuses[:30] == [200] * 30
    assert 429 in statuses[30:]


async def test_the_byte_endpoints_have_a_tighter_limit(harness, vault):
    from src.main import app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("198.51.100.10", 1)),
        base_url="http://localhost:8000",
    ) as c:
        results = await asyncio.gather(
            *[
                c.put("/transfer/upload", headers=auth(harness), content=b"x")
                for _ in range(14)
            ]
        )
    assert any(r.status_code == 429 for r in results)


# ── #87: an unavailable beneath-root lookup must not become an oracle ────────


async def test_download_still_404s_when_openat2_is_unavailable(
    client, harness, download, monkeypatch
):
    """D21. `MCP_SANDBOX_MODE` is the one configuration that skips the startup
    probe, so it is the one in which a call site can be reached with the
    syscall unavailable. What each surface answers there is its existing
    contract, not a new one — and on this bearer-protected **read** that is the
    uniform 404.

    Making it distinguishable would report a server property to an
    unauthenticated bearer and turn the endpoint into an oracle: answering one
    status for unknown, expired, consumed, deleted and replaced is what keeps
    it from being one. Precision comes from `check_upload` and the mint tools,
    which are authenticated.
    """
    real = vault_fs._openat2_raw
    monkeypatch.setattr(
        vault_fs, "_openat2_raw", lambda *a, **k: (-1, errno.ENOSYS)
    )
    response = await client.get("/transfer/download/file", headers=auth(harness))
    assert response.status_code == 404
    head = await client.head("/transfer/download/file", headers=auth(harness))
    assert head.status_code == 404

    # Byte-identical to the answer for a file that is simply gone. Restore only
    # this one patch — `monkeypatch.undo()` would take the harness's database
    # stubs with it.
    monkeypatch.setattr(vault_fs, "_openat2_raw", real)
    download.unlink()
    gone = await client.get("/transfer/download/file", headers=auth(harness))
    assert gone.status_code == 404
    assert gone.content == response.content
