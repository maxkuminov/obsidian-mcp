"""Tests for the five file-transfer MCP tools (task 5.7).

Fully offline: the vault is a per-test tmp dir, usage logging is captured
rather than written, and the mint tools talk to a fake session that records the
rows they add. What the tools own — and what these tests are about — is
everything that happens *before* a row exists: permission, public origin, path
guards, no-clobber, the fingerprint decision, and never letting a token or a
URL that carries one reach `usage_logs`.

The redemption side lives in `tests/test_transfer_routes.py`; the transaction
boundaries live in `tests/integration/test_transfer_pg.py`.
"""

import os
import re
import tempfile
from pathlib import Path

os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
os.chdir(tempfile.gettempdir())

import pytest  # noqa: E402

import src.mcp_server.tools as tools  # noqa: E402
from src.mcp_server.auth import (  # noqa: E402
    current_api_key_id,
    current_permission,
)
from src.models.db import TransferToken  # noqa: E402
from src.services import transfer, vault_fs  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + bytes(range(64))

# A handle of the exact shape `request_upload` mints (22 URL-safe characters).
# `check_upload` refuses anything else before it reaches the log or the lookup.
PUB_ID = transfer.new_public_id()


# ── harness ─────────────────────────────────────────────────────────────────


class FakeSession:
    """Records what the mint tools write; hands back the row they built."""

    def __init__(self, store):
        self.store = store

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def add(self, obj):
        self.store.append(obj)

    async def execute(self, *args, **kwargs):
        return None  # the opportunistic prune

    async def commit(self):
        return None

    async def refresh(self, obj):
        return None


@pytest.fixture(autouse=True)
def usage_log(monkeypatch):
    """Capture usage-log calls instead of writing them."""
    entries = []

    async def _capture(tool, params, duration_ms, response_size):
        entries.append({"tool": tool, "params": params})

    monkeypatch.setattr(tools, "_log_usage", _capture)
    return entries


@pytest.fixture
def minted(monkeypatch):
    """A fake session for the mint path; returns the rows that were added."""
    rows: list[TransferToken] = []
    monkeypatch.setattr(tools, "async_session", lambda: FakeSession(rows))
    return rows


@pytest.fixture
def vault(monkeypatch, tmp_path):
    monkeypatch.setattr(tools.settings, "vault_path", str(tmp_path))
    monkeypatch.setattr(tools.settings, "mcp_hostname", "vault.example.com")
    monkeypatch.setattr(tools.settings, "base_url", "https://vault.example.com")
    monkeypatch.setattr(tools.settings, "_public_origin_explicit", True)
    (tmp_path / "Attachments").mkdir()
    vault_fs.reset_filesystem_probe_cache()
    yield tmp_path
    vault_fs.reset_filesystem_probe_cache()


@pytest.fixture
def publish_gate(monkeypatch, minted):
    """Stand in for the locked identity gate `import_from_url` publishes through.

    The gate's real work — `SELECT … FOR UPDATE` on the caller's credential and
    user rows, held across the publish — is a transaction boundary and is tested
    against real Postgres in `tests/integration/test_transfer_pg.py`. What this
    proves is that the tool *goes through* it, with the right identity and root,
    and publishes nothing when it says no.
    """
    from contextlib import asynccontextmanager

    state = {"calls": [], "ok": True}

    @asynccontextmanager
    async def fake(session, identity, *, vault_root, need_write=True, on_complete=None):
        state["calls"].append(
            {"identity": identity, "vault_root": vault_root, "need_write": need_write}
        )
        yield transfer.GateHandle(ok=state["ok"])

    monkeypatch.setattr(transfer, "lock_identity_for_publish", fake)
    return state


@pytest.fixture
def readwrite():
    perm = current_permission.set("readwrite")
    key = current_api_key_id.set(11)
    try:
        yield
    finally:
        current_permission.reset(perm)
        current_api_key_id.reset(key)


@pytest.fixture
def readonly():
    perm = current_permission.set("read")
    key = current_api_key_id.set(11)
    try:
        yield
    finally:
        current_permission.reset(perm)
        current_api_key_id.reset(key)


def token_from(result: str) -> str:
    """The secret out of a mint tool's response, for assertions about it."""
    line = next(l for l in result.splitlines() if "#" in l and "https://" in l)
    return line.split("#", 1)[1].strip()


# ── 5.1 request_upload ──────────────────────────────────────────────────────


async def test_request_upload_mints_a_bound_link(vault, readwrite, minted):
    result = await tools.request_upload_impl("Attachments/shot.png")
    assert "https://vault.example.com/transfer/upload#" in result
    assert "Attachments/shot.png" in result
    assert "max_bytes" in result

    (row,) = minted
    assert row.direction == "upload"
    assert row.path == "Attachments/shot.png"
    assert row.vault_root == str(vault)
    assert row.key_id == 11
    assert row.overwrite is False
    assert row.expected_fingerprint is None
    # Only the hash is stored; the token exists in the response and nowhere else.
    assert row.token_hash == transfer.hash_token(token_from(result))
    assert row.public_id in result


async def test_request_upload_is_refused_for_a_read_only_key(vault, readonly, minted):
    result = await tools.request_upload_impl("Attachments/shot.png")
    assert "Permission denied" in result
    assert minted == []


async def test_request_upload_refuses_an_existing_target(vault, readwrite, minted):
    (vault / "Attachments" / "shot.png").write_bytes(PNG)
    result = await tools.request_upload_impl("Attachments/shot.png")
    assert "already exists" in result
    assert "overwrite=True" in result
    assert minted == []


async def test_request_upload_with_overwrite_records_the_fingerprint(
    vault, readwrite, minted
):
    target = vault / "Attachments" / "shot.png"
    target.write_bytes(PNG)
    result = await tools.request_upload_impl("Attachments/shot.png", overwrite=True)
    assert "transfer/upload#" in result

    (row,) = minted
    assert row.overwrite is True
    assert row.expected_fingerprint["size"] == len(PNG)
    assert row.expected_fingerprint["inode"] == target.stat().st_ino
    assert row.expected_fingerprint["sha256"] is not None


async def test_overwrite_against_an_absent_target_records_the_absence_sentinel(
    vault, readwrite, minted
):
    """`None` here means "expect absence", which the publish step enforces."""
    result = await tools.request_upload_impl("Attachments/new.png", overwrite=True)
    assert "transfer/upload#" in result
    (row,) = minted
    assert row.overwrite is True
    assert row.expected_fingerprint is None


@pytest.mark.parametrize(
    "path", [".obsidian/config.json", "../escape.png", ".trash/old.png"]
)
async def test_request_upload_rejects_hidden_and_traversing_paths(
    vault, readwrite, minted, path
):
    result = await tools.request_upload_impl(path)
    assert "denied" in result.lower()
    assert minted == []


async def test_request_upload_needs_a_public_origin(vault, readwrite, minted, monkeypatch):
    monkeypatch.setattr(tools.settings, "_public_origin_explicit", False)
    result = await tools.request_upload_impl("Attachments/shot.png")
    assert "MCP_HOSTNAME" in result and "BASE_URL" in result
    assert minted == []


@pytest.mark.parametrize("asked,expected", [(5, 60), (99999, 3600), (None, 600)])
async def test_request_upload_clamps_expires_in(vault, readwrite, minted, asked, expected):
    """Clamped rather than rejected: an agent asking for a week gets an hour."""
    import datetime

    before = datetime.datetime.now(datetime.timezone.utc)
    await tools.request_upload_impl("Attachments/shot.png", expires_in=asked)
    (row,) = minted
    ttl = (row.expires_at - before).total_seconds()
    assert expected - 5 <= ttl <= expected + 5


async def test_request_upload_reports_an_unsupported_filesystem(
    vault, readwrite, minted, monkeypatch
):
    def refuse(root_fd):
        raise vault_fs.UnsupportedFilesystem("no hard links here")

    monkeypatch.setattr(vault_fs, "probe_publication", refuse)
    vault_fs.reset_filesystem_probe_cache()
    result = await tools.request_upload_impl("Attachments/shot.png")
    assert "no hard links here" in result
    assert minted == []


# ── 5.2 check_upload ────────────────────────────────────────────────────────


class _Found:
    """A fake session whose `lookup_by_public_id` answer is fixed per test."""

    def __init__(self, row):
        self.row = row

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def looked_up(monkeypatch):
    """Install a canned `lookup_by_public_id` and record what it was asked."""
    state = {"row": None, "calls": []}

    async def lookup(session, public_id, *, identity, direction):
        state["calls"].append((public_id, identity, direction))
        return state["row"]

    monkeypatch.setattr(tools, "async_session", lambda: _Found(None))
    monkeypatch.setattr(transfer, "lookup_by_public_id", lookup)
    return state


def _row(**kwargs) -> TransferToken:
    import datetime

    defaults = dict(
        public_id=PUB_ID,
        token_hash="x" * 64,
        direction="upload",
        state="pending",
        path="Attachments/shot.png",
        vault_root="/obsidian",
        overwrite=False,
        expires_at=datetime.datetime.now(datetime.timezone.utc)
        + datetime.timedelta(minutes=5),
    )
    defaults.update(kwargs)
    return TransferToken(**defaults)


async def test_check_upload_reports_pending(vault, readwrite, looked_up):
    looked_up["row"] = _row()
    result = await tools.check_upload_impl(PUB_ID)
    assert result.startswith("pending")
    assert "Attachments/shot.png" in result


async def test_check_upload_reports_uploading_with_the_stuck_advice(
    vault, readwrite, looked_up
):
    looked_up["row"] = _row(state="claimed")
    result = await tools.check_upload_impl(PUB_ID)
    assert result.startswith("uploading")
    assert "mint a new link" in result


async def test_check_upload_reports_completed_with_the_hash(vault, readwrite, looked_up):
    import datetime

    looked_up["row"] = _row(
        state="completed",
        size=1234,
        sha256="a" * 64,
        mime="image/png",
        completed_at=datetime.datetime.now(datetime.timezone.utc),
    )
    result = await tools.check_upload_impl(PUB_ID)
    assert result.startswith("completed")
    assert "a" * 64 in result
    assert "1,234 bytes" in result


async def test_check_upload_reports_expired(vault, readwrite, looked_up):
    import datetime

    looked_up["row"] = _row(
        expires_at=datetime.datetime.now(datetime.timezone.utc)
        - datetime.timedelta(seconds=1)
    )
    result = await tools.check_upload_impl(PUB_ID)
    assert result.startswith("expired")


async def test_check_upload_reports_a_consumed_link_as_expired(
    vault, readwrite, looked_up
):
    looked_up["row"] = _row(state="consumed")
    result = await tools.check_upload_impl(PUB_ID)
    assert result.startswith("expired")
    assert "cut short" in result


async def test_check_upload_scopes_the_lookup_to_the_calling_identity(
    vault, readwrite, looked_up
):
    looked_up["row"] = None
    other = transfer.new_public_id()
    result = await tools.check_upload_impl(other)
    assert result.startswith("not found")
    (public_id, identity, direction) = looked_up["calls"][0]
    assert public_id == other
    assert direction == "upload"
    assert identity.key_id == 11


@pytest.mark.parametrize(
    "bad",
    [
        "https://vault.example.com/transfer/upload#" + "t" * 43,
        "t" * 43,  # the raw token
        "pub-1",
        "",
        "  " + PUB_ID,
        PUB_ID + "=",
    ],
)
async def test_check_upload_refuses_an_off_shape_id_before_the_lookup(
    vault, readwrite, looked_up, usage_log, bad
):
    """The handle is 22 URL-safe characters. Anything else never gets looked up."""
    result = await tools.check_upload_impl(bad)
    assert result.startswith("not found")
    assert looked_up["calls"] == []


@pytest.mark.parametrize(
    "bad", ["https://vault.example.com/transfer/upload#" + "s" * 43, "s" * 43]
)
async def test_a_pasted_url_or_token_never_reaches_usage_logs(
    vault, readwrite, looked_up, usage_log, bad
):
    """The mistake an agent actually makes must not write a capability to a log."""
    await tools.check_upload_impl(bad)
    entry = next(e for e in usage_log if e["tool"] == "check_upload")
    assert entry["params"] == {"upload_id": "<invalid>"}
    logged = str(usage_log)
    assert "s" * 43 not in logged
    assert "transfer/upload#" not in logged


# ── 5.3 request_download ────────────────────────────────────────────────────


async def test_request_download_mints_a_fingerprinted_link(vault, readonly, minted):
    target = vault / "Attachments" / "spec.pdf"
    target.write_bytes(b"%PDF-1.4\n" + b"x" * 500)

    result = await tools.request_download_impl("Attachments/spec.pdf")
    assert "https://vault.example.com/transfer/download#" in result
    assert "application/pdf" in result

    (row,) = minted
    assert row.direction == "download"
    assert row.path == "Attachments/spec.pdf"
    assert row.expected_fingerprint["inode"] == target.stat().st_ino
    assert row.expected_fingerprint["sha256"] is not None
    assert row.token_hash == transfer.hash_token(token_from(result))


async def test_request_download_needs_no_write_permission(vault, readonly, minted):
    (vault / "Attachments" / "spec.pdf").write_bytes(b"%PDF-1.4\n")
    result = await tools.request_download_impl("Attachments/spec.pdf")
    assert "Permission denied" not in result
    assert len(minted) == 1


async def test_request_download_refuses_a_missing_file(vault, readonly, minted):
    result = await tools.request_download_impl("Attachments/nope.pdf")
    assert "not found" in result.lower()
    assert minted == []


async def test_request_download_refuses_a_directory(vault, readonly, minted):
    result = await tools.request_download_impl("Attachments")
    assert "regular file" in result
    assert minted == []


async def test_request_download_refuses_a_symlink(vault, readonly, minted, tmp_path):
    secret = vault / "secret.txt"
    secret.write_bytes(b"not yours")
    (vault / "Attachments" / "alias.txt").symlink_to(secret)
    result = await tools.request_download_impl("Attachments/alias.txt")
    assert "symlink" in result
    assert minted == []


async def test_request_download_refuses_a_hidden_path(vault, readonly, minted):
    result = await tools.request_download_impl(".obsidian/app.json")
    assert "denied" in result.lower()
    assert minted == []


def _tree(root: Path) -> set:
    return {p.relative_to(root) for p in root.rglob("*")}


async def test_a_read_only_mint_writes_nothing_to_the_vault(vault, readonly, minted):
    """A read must not probe: the probe creates a temp file, a link and `.trash`.

    On a fresh vault the first `request_download` would otherwise be the thing
    that created directories in it — a read-only capability performing writes
    nobody asked for.
    """
    (vault / "Attachments" / "spec.pdf").write_bytes(b"%PDF-1.4\n")
    before = _tree(vault)

    result = await tools.request_download_impl("Attachments/spec.pdf")
    assert "transfer/download#" in result

    assert _tree(vault) == before
    assert not (vault / ".trash").exists()
    assert not (vault / vault_fs.STAGING_DIR).exists()


async def test_request_download_does_not_probe_the_filesystem(
    vault, readonly, minted, monkeypatch
):
    """Pinned as an explicit call assertion, not only as an absence of files."""
    def fail(*args, **kwargs):
        pytest.fail("a read path probed the filesystem")

    (vault / "Attachments" / "spec.pdf").write_bytes(b"%PDF-1.4\n")
    monkeypatch.setattr(vault_fs, "probe_publication", fail)
    monkeypatch.setattr(vault_fs, "probe_trash", fail)
    vault_fs.reset_filesystem_probe_cache()
    assert "transfer/download#" in await tools.request_download_impl(
        "Attachments/spec.pdf"
    )


# ── 5.4 import_from_url ─────────────────────────────────────────────────────


async def test_import_requires_write_permission(vault, readonly):
    result = await tools.import_from_url_impl(
        "https://example.com/a.png", "Attachments/a.png"
    )
    assert "Permission denied" in result


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1/a.png",
        "https://10.0.0.5/a.png",
        "https://169.254.169.254/latest/meta-data",
        "http://example.com/a.png",
        "https://localhost/a.png",
        "https://db/a.png",
        "https://user:pw@example.com/a.png",
        "https://example.com:5432/a.png",
        "https://2130706433/a.png",
        "ftp://example.com/a.png",
    ],
)
async def test_import_refuses_a_guarded_url_without_writing(vault, readwrite, url):
    result = await tools.import_from_url_impl(url, "Attachments/a.png")
    assert result.startswith("Refused to fetch that URL:")
    assert not (vault / "Attachments" / "a.png").exists()
    assert list((vault / "Attachments").iterdir()) == []


async def test_import_refuses_an_existing_target(vault, readwrite):
    (vault / "Attachments" / "a.png").write_bytes(b"mine")
    result = await tools.import_from_url_impl(
        "https://example.com/a.png", "Attachments/a.png"
    )
    assert "already exists" in result
    assert (vault / "Attachments" / "a.png").read_bytes() == b"mine"


@pytest.fixture
def canned_fetch(monkeypatch):
    """Replace the guarded fetch with a two-chunk canned stream."""
    from contextlib import asynccontextmanager

    async def body():
        yield PNG[:16]
        yield PNG[16:]

    @asynccontextmanager
    async def fake_fetch(url, **kwargs):
        yield transfer.FetchResult(
            chunks=body(),
            final_url="https://cdn.example.com/a.png",
            content_type="image/png",
        )

    monkeypatch.setattr(transfer, "fetch_url_guarded", fake_fetch)


async def test_import_writes_the_fetched_body(
    vault, readwrite, canned_fetch, publish_gate
):
    """The happy path, with the guarded fetch replaced by a canned stream."""
    result = await tools.import_from_url_impl(
        "https://example.com/a.png", "Attachments/a.png"
    )
    assert "Imported" in result
    assert "https://cdn.example.com/a.png" in result
    assert (vault / "Attachments" / "a.png").read_bytes() == PNG


async def test_import_publishes_through_the_locked_identity_gate(
    vault, readwrite, canned_fetch, publish_gate
):
    """The tool's own identity and root are what the gate re-validates."""
    await tools.import_from_url_impl("https://example.com/a.png", "Attachments/a.png")
    (call,) = publish_gate["calls"]
    assert call["identity"].key_id == 11
    assert call["vault_root"] == str(vault)
    assert call["need_write"] is True


async def test_import_publishes_nothing_when_the_gate_refuses(
    vault, readwrite, canned_fetch, publish_gate
):
    """A key revoked or repointed mid-fetch must not land the bytes."""
    publish_gate["ok"] = False
    result = await tools.import_from_url_impl(
        "https://example.com/a.png", "Attachments/a.png"
    )
    assert "no longer valid" in result
    assert "Nothing was written" in result
    assert not (vault / "Attachments" / "a.png").exists()
    assert list((vault / "Attachments").iterdir()) == []
    staging = vault / vault_fs.STAGING_DIR
    assert not staging.exists() or list(staging.iterdir()) == []


def _arm_close_faults(monkeypatch, *, limit: int = 3) -> dict:
    """Make the first `limit` `os.close` calls *after* publication fail.

    Exactly three descriptors are closed once the bytes are in place — the
    destination parent, the staging directory, the vault root — and none of
    them can change whether the file exists.
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


async def test_import_survives_a_failing_close_after_publication(
    vault, readwrite, canned_fetch, publish_gate, monkeypatch
):
    """A close that fails once the bytes have landed is not an import failure.

    Before the fix, an `OSError` from a descriptor close on the publish path
    reached `import_from_url`'s `except OSError` and returned "Could not write
    …" for a file that was sitting at the requested path. An agent reads that
    as "nothing happened" and retries.
    """
    state = _arm_close_faults(monkeypatch)
    result = await tools.import_from_url_impl(
        "https://example.com/a.png", "Attachments/a.png"
    )
    monkeypatch.undo()

    assert state["failures"] == 3, "the injected close failures never fired"
    assert "Imported" in result
    assert "Could not write" not in result
    assert (vault / "Attachments" / "a.png").read_bytes() == PNG


async def test_import_reports_a_post_publish_failure_as_written(
    vault, readwrite, canned_fetch, publish_gate, monkeypatch
):
    """When the bookkeeping genuinely fails, the message still says "in place".

    `PostPublishFailure` used to escape the tool entirely (it is not an
    `OSError`), surfacing as an unhandled exception. Either way the agent had
    no way to learn that the file exists.
    """
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def failing_gate(session, identity, *, vault_root, need_write=True):
        yield transfer.GateHandle(ok=True)
        raise RuntimeError("commit failed")

    monkeypatch.setattr(transfer, "lock_identity_for_publish", failing_gate)
    result = await tools.import_from_url_impl(
        "https://example.com/a.png", "Attachments/a.png"
    )
    monkeypatch.undo()

    assert "IS in place" in result
    assert "Nothing was written" not in result
    assert (vault / "Attachments" / "a.png").read_bytes() == PNG


async def test_import_logs_only_the_url_host(vault, readwrite, usage_log):
    await tools.import_from_url_impl(
        "https://example.com/secret-path?token=SUPERSECRET", "Attachments/a.png"
    )
    entry = next(e for e in usage_log if e["tool"] == "import_from_url")
    assert entry["params"]["url"] == "example.com"
    assert "SUPERSECRET" not in str(entry["params"])
    assert "secret-path" not in str(entry["params"])


# ── 5.5 delete_file ─────────────────────────────────────────────────────────


async def test_delete_file_soft_deletes_into_trash(vault, readwrite):
    target = vault / "Attachments" / "shot.png"
    target.write_bytes(PNG)
    result = await tools.delete_file_impl("Attachments/shot.png")
    assert ".trash/" in result
    assert not target.exists()
    trashed = list((vault / ".trash").iterdir())
    assert len(trashed) == 1
    assert trashed[0].read_bytes() == PNG
    # `<YYYYMMDD-HHMMSS>-<basename>-<8 hex>`: the random tail is what makes two
    # same-second deletes of the same basename land on different names.
    assert re.fullmatch(r"\d{8}-\d{6}-shot\.png-[0-9a-f]{8}", trashed[0].name)


async def test_delete_file_permanent_leaves_no_trash_copy(vault, readwrite):
    target = vault / "Attachments" / "shot.png"
    target.write_bytes(PNG)
    result = await tools.delete_file_impl("Attachments/shot.png", permanent=True)
    assert "Permanently deleted" in result
    assert not target.exists()
    assert not (vault / ".trash").exists() or list((vault / ".trash").iterdir()) == []


async def test_delete_file_refuses_markdown_and_points_at_delete_note(vault, readwrite):
    note = vault / "note.md"
    note.write_text("# hi", encoding="utf-8")
    result = await tools.delete_file_impl("note.md")
    assert "delete_note" in result
    assert note.exists()


@pytest.mark.parametrize(
    "spelling", ["note.md/.", "note.md/", "a//note.md", "NOTE.MD", "./note.md"]
)
async def test_delete_file_refuses_markdown_however_it_is_spelled(
    vault, readwrite, spelling
):
    """The guard must run on the component the filesystem will open.

    `path.lower().endswith(".md")` reads the caller's string, and the caller's
    string is not the path: `note.md/.` and `a//note.md` both name a note while
    failing that test, which would delete it with the tool that knows nothing
    about the index or the backlink graph.
    """
    (vault / "a").mkdir()
    for note in (vault / "note.md", vault / "NOTE.MD", vault / "a" / "note.md"):
        note.write_text("# hi", encoding="utf-8")

    result = await tools.delete_file_impl(spelling)
    assert "delete_note" in result, result
    assert (vault / "note.md").exists()
    assert (vault / "NOTE.MD").exists()
    assert (vault / "a" / "note.md").exists()
    assert not (vault / ".trash").exists()


async def test_permanent_delete_does_not_create_the_trash(vault, readwrite):
    """`permanent=True` never uses `.trash`, so it must not probe it either."""
    (vault / "Attachments" / "shot.png").write_bytes(PNG)
    await tools.delete_file_impl("Attachments/shot.png", permanent=True)
    assert not (vault / ".trash").exists()


async def test_delete_file_refuses_a_directory(vault, readwrite):
    result = await tools.delete_file_impl("Attachments")
    assert "regular file" in result
    assert (vault / "Attachments").is_dir()


async def test_delete_file_refuses_a_symlink(vault, readwrite):
    secret = vault / "secret.txt"
    secret.write_bytes(b"not yours")
    link = vault / "Attachments" / "alias.txt"
    link.symlink_to(secret)
    result = await tools.delete_file_impl("Attachments/alias.txt")
    assert "symlink" in result
    assert link.is_symlink()
    assert secret.exists()


async def test_delete_file_is_refused_for_a_read_only_key(vault, readonly):
    target = vault / "Attachments" / "shot.png"
    target.write_bytes(PNG)
    result = await tools.delete_file_impl("Attachments/shot.png")
    assert "Permission denied" in result
    assert target.exists()


async def test_delete_file_refuses_a_hidden_path(vault, readwrite):
    result = await tools.delete_file_impl(".obsidian/app.json")
    assert "denied" in result.lower()


async def test_delete_file_reports_a_missing_file(vault, readwrite):
    result = await tools.delete_file_impl("Attachments/nope.png")
    assert "not found" in result.lower()


# ── 5.6 logging ─────────────────────────────────────────────────────────────


async def test_no_mint_tool_ever_logs_a_token(vault, readwrite, minted, usage_log):
    up = await tools.request_upload_impl("Attachments/shot.png", overwrite=False)
    (vault / "Attachments" / "spec.pdf").write_bytes(b"%PDF-1.4\n")
    down = await tools.request_download_impl("Attachments/spec.pdf")

    logged = str([e["params"] for e in usage_log])
    for result in (up, down):
        assert token_from(result) not in logged
    assert "transfer/upload#" not in logged
    assert "transfer/download#" not in logged


async def test_mint_tools_log_the_allow_listed_params(vault, readwrite, minted, usage_log):
    await tools.request_upload_impl("Attachments/shot.png", overwrite=True, expires_in=120)
    entry = next(e for e in usage_log if e["tool"] == "request_upload")
    assert entry["params"] == {
        "path": "Attachments/shot.png",
        "overwrite": True,
        "expires_in": 120,
    }


async def test_check_upload_logs_only_the_public_id(vault, readwrite, looked_up, usage_log):
    looked_up["row"] = _row()
    await tools.check_upload_impl(PUB_ID)
    entry = next(e for e in usage_log if e["tool"] == "check_upload")
    assert entry["params"] == {"upload_id": PUB_ID}


# ── the symlink-retargeting regression ──────────────────────────────────────


async def test_a_symlink_never_retargets_the_operation(vault, readwrite, minted):
    """`validate_visible_path` resolves; the path we act on must not.

    An in-vault symlink passes the traversal guard, and taking the relative
    path from the *resolved* result made `delete_file("Attachments/alias.png")`
    delete `real.png` and report success for a path nobody named. Caught by
    these tests before it shipped; this is the pin.
    """
    real = vault / "real.png"
    real.write_bytes(PNG)
    (vault / "Attachments" / "alias.png").symlink_to(real)

    assert tools._vault_context("Attachments/alias.png", None)[1] == (
        "Attachments/alias.png"
    )
    assert "symlink" in await tools.delete_file_impl("Attachments/alias.png")
    assert real.read_bytes() == PNG
    assert "symlink" in await tools.request_download_impl("Attachments/alias.png")
    assert minted == []


async def test_request_upload_refuses_a_symlinked_ancestor(vault, readwrite, minted):
    (vault / "Elsewhere").mkdir()
    (vault / "Attachments" / "linked").symlink_to(
        vault / "Elsewhere", target_is_directory=True
    )
    result = await tools.request_upload_impl("Attachments/linked/shot.png")
    assert "symlink" in result
    assert minted == []
