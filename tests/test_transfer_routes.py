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

import asyncio
import datetime
import hashlib
import logging
import os
import re
from dataclasses import dataclass, field

import httpx
import pytest
import pytest_asyncio

from src.config import settings
from src.limiter import limiter
from src.services import transfer, vault_fs
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
