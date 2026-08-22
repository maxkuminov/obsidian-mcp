"""Transfer service unit tests (task 3.4): streaming writer + SSRF guard.

The DB-backed half of the lifecycle (claim linearizability, the FOR UPDATE
publish barrier, expiry, cascade) lives in `tests/integration/test_transfer_pg.py`
because a fake session cannot prove a transaction boundary. What is here is
everything whose truth does not depend on Postgres: the pure token helpers, the
streaming writer against a real temp vault, and the outbound-fetch policy.

Two properties get more attention than their line count suggests:

* **"no connection was attempted"** — every pre-connect rejection is asserted
  by making `PinnedTransport` explode if it is ever constructed. A guard that
  rejects *after* opening the socket has already leaked the request.
* **the pinned connection keeps the caller's identity** — peer address, `Host`
  header and TLS SNI are asserted separately, because pinning to an IP by
  rewriting the URL is exactly the change that silently breaks SNI and turns
  certificate verification into a no-op.
"""
import asyncio
import hashlib
import http.server
import os
import socket
import ssl
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from src.services import transfer, vault_fs
from src.services.transfer import (
    PrePublishAborted,
    SSRFError,
    Timeout,
    TooLarge,
    canonicalise,
    clamp_expires_in,
    fetch_url_guarded,
    hash_token,
    is_forbidden_address,
    resolve_and_check,
    stream_to_vault,
)
from src.services.vault_fs import Conflict, UnsafePath

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


# ════════════════════════════════════════════════════════════════════════════
# Pure token helpers
# ════════════════════════════════════════════════════════════════════════════


def test_hash_token_is_sha256_hex():
    assert hash_token("abc") == hashlib.sha256(b"abc").hexdigest()


def test_new_token_is_256_bits_and_url_safe():
    token = transfer.new_token()
    # token_urlsafe(32) is 32 random bytes rendered base64url without padding.
    assert len(token) >= 43
    assert set(token) <= set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    )
    assert token != transfer.new_token()


@pytest.mark.parametrize(
    "given,expected",
    [(5, 60), (0, 60), (-10, 60), (60, 60), (600, 600), (3600, 3600), (99999, 3600)],
)
def test_clamp_expires_in(given, expected):
    assert clamp_expires_in(given) == expected


def test_clamp_expires_in_defaults_to_the_setting(monkeypatch):
    monkeypatch.setattr(transfer.settings, "transfer_token_ttl_seconds", 900)
    assert clamp_expires_in(None) == 900


def test_canonical_vault_root_matches_vault_root_normalisation():
    from src.services import vault

    assert transfer.canonical_vault_root("/obsidian/") == str(vault.Path("/obsidian"))
    assert transfer.canonical_vault_root("/vaults/a//b") == "/vaults/a/b"


# ════════════════════════════════════════════════════════════════════════════
# Streaming writer
# ════════════════════════════════════════════════════════════════════════════


@dataclass
class FakeRow:
    """The four fields `stream_to_vault` actually reads off a token row."""

    vault_root: str
    path: str
    overwrite: bool = False
    expected_fingerprint: dict | None = None


async def chunks_of(*parts: bytes):
    for part in parts:
        yield part


def deadline_in(seconds: float) -> float:
    return time.monotonic() + seconds


@pytest.fixture
def vault(tmp_path):
    (tmp_path / "Attachments").mkdir()
    return tmp_path


def temps_under(directory: Path) -> list[str]:
    return [
        str(p.relative_to(directory))
        for p in directory.rglob(".tmp-*")
    ]


async def test_stream_writes_exact_bytes_hash_and_mime(vault):
    row = FakeRow(str(vault), "Attachments/shot.png")
    result = await stream_to_vault(
        row,
        chunks_of(PNG[:10], PNG[10:]),
        max_bytes=1_000_000,
        content_length=len(PNG),
        deadline=deadline_in(30),
    )
    assert result == {
        "size": len(PNG),
        "sha256": hashlib.sha256(PNG).hexdigest(),
        "mime": "image/png",
    }
    assert (vault / "Attachments" / "shot.png").read_bytes() == PNG
    assert temps_under(vault) == []


async def test_uploaded_file_keeps_the_umask_default_mode(vault):
    """Staging at 0600 must not leak into the published upload's permissions.

    Publication links the staging inode into place, so without the relax step
    an upload lands 0600 while every note beside it is 0644 — unreadable to
    anything sharing the vault under a different uid or group (#95).
    """
    row = FakeRow(str(vault), "Attachments/shot.png")
    await stream_to_vault(
        row,
        chunks_of(PNG),
        max_bytes=1_000_000,
        content_length=len(PNG),
        deadline=deadline_in(30),
    )
    reference = vault / "Attachments" / "reference.png"
    reference.write_bytes(PNG)

    published = (vault / "Attachments" / "shot.png").stat().st_mode & 0o777
    assert published == reference.stat().st_mode & 0o777
    assert published == vault_fs.default_file_mode()


async def test_stream_creates_missing_parent_directories(vault):
    row = FakeRow(str(vault), "New/Deep/file.bin")
    await stream_to_vault(
        row, chunks_of(b"x"), max_bytes=100, content_length=None, deadline=deadline_in(30)
    )
    assert (vault / "New" / "Deep" / "file.bin").read_bytes() == b"x"


async def test_declared_content_length_over_cap_is_refused_before_any_io(vault):
    row = FakeRow(str(vault), "Attachments/big.bin")

    async def must_not_be_read():
        raise AssertionError("body was read despite an over-cap Content-Length")
        yield b""  # pragma: no cover

    with pytest.raises(TooLarge):
        await stream_to_vault(
            row,
            must_not_be_read(),
            max_bytes=10,
            content_length=11,
            deadline=deadline_in(30),
        )
    assert temps_under(vault) == []
    assert not (vault / "Attachments" / "big.bin").exists()


async def test_undeclared_oversize_body_aborts_at_the_cap(vault):
    row = FakeRow(str(vault), "Attachments/big.bin")
    with pytest.raises(TooLarge):
        await stream_to_vault(
            row,
            chunks_of(b"x" * 8, b"x" * 8),
            max_bytes=10,
            content_length=None,
            deadline=deadline_in(30),
        )
    assert temps_under(vault) == []
    assert not (vault / "Attachments" / "big.bin").exists()


async def test_lying_content_length_still_aborts_at_the_cap(vault):
    """A body larger than its declared length is caught by the running count."""
    row = FakeRow(str(vault), "Attachments/big.bin")
    with pytest.raises(TooLarge):
        await stream_to_vault(
            row,
            chunks_of(b"x" * 50),
            max_bytes=10,
            content_length=5,
            deadline=deadline_in(30),
        )
    assert temps_under(vault) == []


async def test_no_clobber_target_appeared(vault):
    (vault / "Attachments" / "a.bin").write_bytes(b"incumbent")
    row = FakeRow(str(vault), "Attachments/a.bin")
    with pytest.raises(Conflict):
        await stream_to_vault(
            row, chunks_of(b"mine"), max_bytes=100, deadline=deadline_in(30)
        )
    assert (vault / "Attachments" / "a.bin").read_bytes() == b"incumbent"
    assert temps_under(vault) == []


async def test_overwrite_fingerprint_mismatch(vault):
    target = vault / "Attachments" / "a.bin"
    target.write_bytes(b"old")
    root_fd = vault_fs.open_root(vault)
    dir_fd = vault_fs.open_dir_beneath(root_fd, "Attachments")
    try:
        want = vault_fs.fingerprint(dir_fd, "a.bin", hash_up_to=1000)
    finally:
        os.close(dir_fd)
        os.close(root_fd)
    target.write_bytes(b"edited by someone else")

    row = FakeRow(str(vault), "Attachments/a.bin", overwrite=True, expected_fingerprint=want)
    with pytest.raises(Conflict):
        await stream_to_vault(
            row, chunks_of(b"mine"), max_bytes=100, deadline=deadline_in(30)
        )
    assert target.read_bytes() == b"edited by someone else"
    assert temps_under(vault) == []


async def test_symlinked_ancestor_is_refused(vault, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    os.symlink(outside, vault / "link")
    row = FakeRow(str(vault), "link/pwned.bin")
    with pytest.raises(UnsafePath):
        await stream_to_vault(
            row, chunks_of(b"x"), max_bytes=100, deadline=deadline_in(30)
        )
    assert list(outside.iterdir()) == []


async def test_traversal_in_the_bound_path_is_refused(vault):
    row = FakeRow(str(vault), "../escape.bin")
    with pytest.raises(UnsafePath):
        await stream_to_vault(
            row, chunks_of(b"x"), max_bytes=100, deadline=deadline_in(30)
        )


async def test_idle_timeout(vault):
    async def stalls():
        yield b"start"
        await asyncio.sleep(5)
        yield b"never"  # pragma: no cover

    row = FakeRow(str(vault), "Attachments/a.bin")
    with pytest.raises(Timeout) as exc:
        await stream_to_vault(
            row, stalls(), max_bytes=100, deadline=deadline_in(30), idle_timeout=0.05
        )
    assert "stalled" in str(exc.value)
    assert temps_under(vault) == []


async def test_overall_deadline(vault):
    async def drips():
        for _ in range(100):
            await asyncio.sleep(0.02)
            yield b"x"

    row = FakeRow(str(vault), "Attachments/a.bin")
    with pytest.raises(Timeout) as exc:
        await stream_to_vault(
            row, drips(), max_bytes=1000, deadline=deadline_in(0.1), idle_timeout=30
        )
    assert "deadline" in str(exc.value)
    assert temps_under(vault) == []
    assert not (vault / "Attachments" / "a.bin").exists()


# ── the pre-publication gate ────────────────────────────────────────────────


@dataclass
class RecordingGate:
    """An async CM standing in for the locked pre-publication transaction."""

    allow: bool = True
    target: Path | None = None
    entered: int = 0
    seen_on_exit: list[bool] = field(default_factory=list)
    completions: list[tuple[dict, bool]] = field(default_factory=list)
    target_at_completion: list[bool] = field(default_factory=list)
    fail_on_exit: bool = False

    def __call__(self):
        return self

    async def __aenter__(self):
        self.entered += 1

        async def record(result: dict, published: bool) -> None:
            self.completions.append((result, published))
            if self.target is not None:
                # `complete` runs with the file already in place and the
                # transaction still open — that is the invariant the route
                # depends on to commit completion under the publish locks.
                self.target_at_completion.append(self.target.exists())

        return transfer.GateHandle(ok=self.allow, session=self, on_complete=record)

    async def __aexit__(self, *exc):
        if self.target is not None:
            self.seen_on_exit.append(self.target.exists())
        if self.fail_on_exit and exc[0] is None:
            # Stands in for a commit that fails after the bytes have landed.
            raise RuntimeError("commit failed")
        return False


async def test_gate_stays_open_across_the_publish(vault):
    target = vault / "Attachments" / "a.bin"
    gate = RecordingGate(allow=True, target=target)
    row = FakeRow(str(vault), "Attachments/a.bin")
    result = await stream_to_vault(
        row,
        chunks_of(b"payload"),
        max_bytes=100,
        deadline=deadline_in(30),
        before_publish=gate,
    )
    assert gate.entered == 1
    assert gate.seen_on_exit == [True]
    assert target.read_bytes() == b"payload"
    # Completion is recorded inside the context, after the publish, with the
    # service's own numbers — not reconstructed by the caller afterwards.
    assert gate.completions == [(result, True)]
    assert gate.target_at_completion == [True]


async def test_the_destination_is_resolved_at_publish_time_not_at_open_time(vault):
    """Renaming the destination folder mid-stream must not redirect the publish.

    A directory descriptor opened before the stream keeps pointing at the same
    directory after it is renamed — including into `.trash` or out of the vault
    — so publishing through it would write somewhere the token never named.
    Bytes are staged under `.transfer-tmp/` and the destination parent is
    re-walked from the root inside the gate.
    """
    moved = vault / "Moved-Away"

    class RenamingGate(RecordingGate):
        async def __aenter__(self):
            # Fires between the last byte and the publish, i.e. exactly in the
            # window a rename would have to hit.
            os.rename(vault / "Attachments", moved)
            return await super().__aenter__()

    gate = RenamingGate(allow=True)
    row = FakeRow(str(vault), "Attachments/a.bin")
    await stream_to_vault(
        row,
        chunks_of(b"payload"),
        max_bytes=100,
        deadline=deadline_in(30),
        before_publish=gate,
    )
    # Published at the path the token committed to, as it resolves now.
    assert (vault / "Attachments" / "a.bin").read_bytes() == b"payload"
    assert not (moved / "a.bin").exists()
    assert temps_under(vault) == []


async def test_bytes_are_staged_outside_the_destination_folder(vault):
    """A crashed upload leaves nothing in the folder the agent can see."""
    seen: list[list[str]] = []

    class PeekingGate(RecordingGate):
        async def __aenter__(self):
            seen.append(sorted(p.name for p in (vault / "Attachments").iterdir()))
            return await super().__aenter__()

    row = FakeRow(str(vault), "Attachments/a.bin")
    await stream_to_vault(
        row,
        chunks_of(b"payload"),
        max_bytes=100,
        deadline=deadline_in(30),
        before_publish=PeekingGate(allow=True),
    )
    assert seen == [[]], "the temp file was staged in the destination folder"
    assert (vault / vault_fs.STAGING_DIR).is_dir()
    assert list((vault / vault_fs.STAGING_DIR).iterdir()) == []


async def test_a_commit_failure_after_publication_is_its_own_error(vault):
    """The one failure a caller must not treat as "nothing happened"."""
    target = vault / "Attachments" / "a.bin"
    gate = RecordingGate(allow=True, target=target, fail_on_exit=True)
    row = FakeRow(str(vault), "Attachments/a.bin")
    with pytest.raises(transfer.PostPublishFailure):
        await stream_to_vault(
            row,
            chunks_of(b"payload"),
            max_bytes=100,
            deadline=deadline_in(30),
            before_publish=gate,
        )
    # The bytes are there; only the bookkeeping failed. Releasing the claim on
    # this would make a token replayable over a written path.
    assert target.read_bytes() == b"payload"
    assert temps_under(vault) == []


def arm_close_faults(monkeypatch, *, limit: int = 3) -> dict:
    """Make the first `limit` `os.close` calls *after* publication fail.

    Three descriptors are closed once the bytes are in place — the destination
    parent, the staging directory, and the vault root — and none of them is
    load-bearing: the file is already at its path. The question this asks is
    whether any of those closes can still convince the caller that nothing was
    published, which is the failure that makes an upload token replayable over
    a written path and makes an import report a write it actually performed as
    a failure.

    Patching `os.close` wholesale is deliberate: the test should not have to
    know which fd is which, and arming only after `publish` returns plus a hard
    cap of `limit` failures keeps the window to exactly those three calls. The
    descriptor is really closed before the error is raised, so nothing leaks.
    """
    import errno as _errno

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
            raise OSError(_errno.EIO, "Input/output error")
        return real_close(fd)

    monkeypatch.setattr(vault_fs, "publish", arming_publish)
    monkeypatch.setattr(os, "close", faulty_close)
    return state


async def test_a_failing_close_after_publication_does_not_hide_the_publish(
    vault, monkeypatch
):
    """MAJOR regression: publication is recorded before any cleanup runs.

    `_publish_into_current_parent` used to close the destination descriptor in
    a `finally` *before* its return value reached the caller, so an `EIO` there
    discarded the `Published` outcome and surfaced as a bare `OSError` — which
    the upload route reads as "demonstrably pre-publication" and answers by
    releasing the claim, over a path that already holds the file.
    """
    target = vault / "Attachments" / "a.bin"
    gate = RecordingGate(allow=True, target=target)
    row = FakeRow(str(vault), "Attachments/a.bin")
    state = arm_close_faults(monkeypatch)

    result = await stream_to_vault(
        row,
        chunks_of(b"payload"),
        max_bytes=100,
        deadline=deadline_in(30),
        before_publish=gate,
    )
    monkeypatch.undo()

    assert state["failures"] == 3, "the injected close failures never fired"
    # The call succeeded, the completion was recorded as published, and the
    # file is where the token said it would be.
    assert result["size"] == len(b"payload")
    assert gate.completions == [(result, True)]
    assert target.read_bytes() == b"payload"
    assert temps_under(vault) == []


async def test_a_failing_close_after_publication_never_raises_a_bare_oserror(
    vault, monkeypatch
):
    """Even when the *gate* also fails, the error names the publication.

    The contract the upload route leans on is that `PostPublishFailure` is the
    only exception raised once the bytes are in place. A close failure stacked
    on a commit failure must not downgrade it to a generic `OSError`.
    """
    target = vault / "Attachments" / "a.bin"
    gate = RecordingGate(allow=True, target=target, fail_on_exit=True)
    row = FakeRow(str(vault), "Attachments/a.bin")
    arm_close_faults(monkeypatch)

    with pytest.raises(transfer.PostPublishFailure):
        await stream_to_vault(
            row,
            chunks_of(b"payload"),
            max_bytes=100,
            deadline=deadline_in(30),
            before_publish=gate,
        )
    monkeypatch.undo()
    assert target.read_bytes() == b"payload"


async def test_a_failing_close_after_a_gateless_publish_still_succeeds(
    vault, monkeypatch
):
    """The no-gate path (a direct `stream_to_vault`) gets the same treatment."""
    row = FakeRow(str(vault), "Attachments/a.bin")
    arm_close_faults(monkeypatch)

    result = await stream_to_vault(
        row,
        chunks_of(b"payload"),
        max_bytes=100,
        deadline=deadline_in(30),
    )
    monkeypatch.undo()

    assert result["size"] == len(b"payload")
    assert (vault / "Attachments" / "a.bin").read_bytes() == b"payload"
    assert temps_under(vault) == []


async def test_gate_refusal_publishes_nothing(vault):
    gate = RecordingGate(allow=False)
    row = FakeRow(str(vault), "Attachments/a.bin")
    with pytest.raises(PrePublishAborted):
        await stream_to_vault(
            row,
            chunks_of(b"payload"),
            max_bytes=100,
            deadline=deadline_in(30),
            before_publish=gate,
        )
    assert not (vault / "Attachments" / "a.bin").exists()
    assert temps_under(vault) == []


async def test_gate_is_not_opened_until_the_body_is_complete(vault):
    """An oversize body must fail before any lock is taken."""
    gate = RecordingGate(allow=True)
    row = FakeRow(str(vault), "Attachments/a.bin")
    with pytest.raises(TooLarge):
        await stream_to_vault(
            row,
            chunks_of(b"x" * 50),
            max_bytes=10,
            deadline=deadline_in(30),
            before_publish=gate,
        )
    assert gate.entered == 0


async def test_concurrent_streams_are_bounded_by_the_semaphore(vault, monkeypatch):
    monkeypatch.setattr(transfer.settings, "transfer_max_concurrent_uploads", 2)
    transfer._upload_semaphores.clear()

    live = 0
    peak = 0
    release = asyncio.Event()

    async def slow(marker: bytes):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        try:
            yield marker
            await release.wait()
            yield marker
        finally:
            live -= 1

    async def one(i: int):
        row = FakeRow(str(vault), f"Attachments/f{i}.bin")
        return await stream_to_vault(
            row,
            slow(bytes([65 + i])),
            max_bytes=100,
            deadline=deadline_in(30),
            idle_timeout=30,
        )

    tasks = [asyncio.create_task(one(i)) for i in range(5)]
    await asyncio.sleep(0.05)
    assert peak <= 2, f"semaphore let {peak} streams run at once"
    release.set()
    await asyncio.gather(*tasks)
    for i in range(5):
        assert (vault / "Attachments" / f"f{i}.bin").exists()
    transfer._upload_semaphores.clear()


# ════════════════════════════════════════════════════════════════════════════
# SSRF guard — canonicalisation
# ════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def no_connections(monkeypatch):
    """Make any attempt to build a connection an outright test failure."""

    class Exploding:
        def __init__(self, *args, **kwargs):
            raise AssertionError("a connection was attempted after a rejection")

    monkeypatch.setattr(transfer, "PinnedTransport", Exploding)


def test_https_url_is_accepted():
    parts = canonicalise("https://example.com/a.png?x=1", allow_http=False)
    assert parts.scheme == "https"
    assert parts.host == "example.com"
    assert parts.port == 443
    assert parts.url == "https://example.com/a.png?x=1"


def test_fragment_is_dropped():
    assert canonicalise("https://example.com/a#frag", allow_http=False).url == (
        "https://example.com/a"
    )


def test_empty_path_becomes_root():
    assert canonicalise("https://example.com", allow_http=False).url == (
        "https://example.com/"
    )


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/a",
        "file:///etc/passwd",
        "gopher://example.com/",
        "//example.com/a",
        "/just/a/path",
    ],
)
def test_non_http_schemes_are_refused(url):
    with pytest.raises(SSRFError):
        canonicalise(url, allow_http=False)


def test_http_is_refused_by_default():
    with pytest.raises(SSRFError, match="IMPORT_ALLOW_HTTP"):
        canonicalise("http://example.com/a", allow_http=False)


def test_http_is_accepted_when_enabled():
    parts = canonicalise("http://example.com/a", allow_http=True)
    assert parts.port == 80


@pytest.mark.parametrize(
    "url",
    [
        "https://user:pw@example.com/a",
        "https://user@example.com/a",
    ],
)
def test_userinfo_is_refused(url):
    with pytest.raises(SSRFError, match="credentials"):
        canonicalise(url, allow_http=False)


def test_ipv6_zone_id_is_refused():
    with pytest.raises(SSRFError, match="zone"):
        canonicalise("https://[fe80::1%25eth0]/a", allow_http=False)


@pytest.mark.parametrize("host", ["localhost", "x.localhost", "db.local", "x.internal", "y.home.arpa"])
def test_local_scope_names_are_refused(host):
    with pytest.raises(SSRFError, match="local/internal"):
        canonicalise(f"https://{host}/a", allow_http=False)


@pytest.mark.parametrize("host", ["db", "postgres", "ollama", "registry"])
def test_single_label_hosts_are_refused(host):
    with pytest.raises(SSRFError, match="Single-label"):
        canonicalise(f"https://{host}/a", allow_http=False)


@pytest.mark.parametrize("host", ["2130706433", "0x7f000001", "0177.0.0.1", "127.1", "010.0.0.1"])
def test_ambiguous_numeric_hosts_are_refused(host):
    """`getaddrinfo` reads all of these as 127.0.0.1; `ipaddress` reads none of them."""
    with pytest.raises(SSRFError, match="Ambiguous numeric"):
        canonicalise(f"https://{host}/a", allow_http=False)


@pytest.mark.parametrize("port", [5432, 11434, 5000, 8080, 22, 3000])
def test_https_ports_other_than_443_8443_are_refused(port):
    with pytest.raises(SSRFError, match="not allowed for https"):
        canonicalise(f"https://example.com:{port}/a", allow_http=False)


@pytest.mark.parametrize("port", [443, 8443])
def test_https_allowed_ports(port):
    assert canonicalise(f"https://example.com:{port}/a", allow_http=False).port == port


@pytest.mark.parametrize("port", [443, 8443, 5432])
def test_http_may_not_borrow_https_ports(port):
    with pytest.raises(SSRFError, match="not allowed for http"):
        canonicalise(f"http://example.com:{port}/a", allow_http=True)


def test_trailing_dot_is_normalised():
    assert canonicalise("https://example.com./a", allow_http=False).host == "example.com"


def test_idna_host_is_encoded():
    parts = canonicalise("https://bücher.example/a", allow_http=False)
    assert parts.host == "xn--bcher-kva.example"


@pytest.mark.parametrize(
    "host",
    [
        "svc.prod。internal",  # IDEOGRAPHIC FULL STOP
        "svc.prod．internal",  # FULLWIDTH FULL STOP
        "svc.prod｡internal",  # HALFWIDTH IDEOGRAPHIC FULL STOP
        "svc。prod。internal",
        "ｉｎｔｅｒｎａｌ",  # fullwidth "internal"
        "db。local",
        "x．home．arpa",
    ],
)
def test_alternative_full_stops_cannot_smuggle_a_forbidden_name(host):
    """UTS-46 folds these to `.`; the checks must run *after* that folding.

    The stdlib `idna` codec is IDNA 2003 and only ever splits on U+002E, so
    running the suffix/single-label checks before normalisation let
    `svc.prod。internal` through and then resolved it as `svc.prod.internal`.
    """
    with pytest.raises(SSRFError):
        canonicalise(f"https://{host}/a", allow_http=False)


@pytest.mark.parametrize(
    "host",
    [
        "２１３０７０６４３３",  # fullwidth 2130706433
        "０ｘ７ｆ０００００１",  # fullwidth 0x7f000001
    ],
)
def test_fullwidth_numeric_hosts_are_refused(host):
    """Fullwidth digits fold to ASCII digits, i.e. to a packed IPv4 spelling."""
    with pytest.raises(SSRFError):
        canonicalise(f"https://{host}/a", allow_http=False)


def test_fullwidth_public_name_is_folded_not_rejected():
    parts = canonicalise("https://ｅｘａｍｐｌｅ.com/a", allow_http=False)
    assert parts.host == "example.com"
    assert parts.url == "https://example.com/a"


@pytest.mark.parametrize("host", ["under_score.example", "-lead.example", "a..b.example"])
def test_hosts_that_are_not_ldh_are_refused(host):
    with pytest.raises(SSRFError):
        canonicalise(f"https://{host}/a", allow_http=False)


def test_ip_literal_is_canonicalised():
    parts = canonicalise("https://[2606:4700:4700::1111]/a", allow_http=False)
    assert parts.host == "2606:4700:4700::1111"
    assert parts.url.startswith("https://[2606:4700:4700::1111]/")


# ── address policy ──────────────────────────────────────────────────────────


FORBIDDEN_ADDRESSES = [
    "127.0.0.1",
    "127.255.255.254",
    "10.0.0.5",
    "172.16.0.1",
    "192.168.1.1",
    "169.254.169.254",  # cloud metadata
    "100.64.0.1",  # CGNAT
    "0.0.0.0",
    "192.0.0.1",
    "192.0.2.1",
    "198.18.0.1",
    "198.51.100.1",
    "203.0.113.1",
    "224.0.0.1",  # multicast
    "240.0.0.1",
    "255.255.255.255",
    "::1",
    "::",
    "ff02::1",  # IPv6 multicast — is_global does not exclude this
    "fc00::1",  # ULA
    "fe80::1",  # link-local
    "2001:db8::1",
    "::ffff:10.0.0.5",  # IPv4-mapped
    "::ffff:127.0.0.1",
    "::a00:5",  # IPv4-compatible
    "64:ff9b::a00:5",  # NAT64 /96
    "64:ff9b:1:0:0a00:0:0500:0",  # NAT64 /48
    "2002:0a00:0005::1",  # 6to4
    "2001:0:0:0:0:0:f5ff:fffa",  # Teredo embedding 10.0.0.5
    "100::1",  # discard-only
]

ALLOWED_ADDRESSES = ["8.8.8.8", "1.1.1.1", "93.184.216.34", "2606:4700:4700::1111"]


@pytest.mark.parametrize("text", FORBIDDEN_ADDRESSES)
def test_forbidden_addresses(text):
    import ipaddress

    assert is_forbidden_address(ipaddress.ip_address(text)) is True


@pytest.mark.parametrize("text", ALLOWED_ADDRESSES)
def test_allowed_addresses(text):
    import ipaddress

    assert is_forbidden_address(ipaddress.ip_address(text)) is False


async def test_ip_literal_hosts_are_checked_without_dns():
    called = []

    async def resolver(host, port):
        called.append(host)
        return ["8.8.8.8"]

    parts = canonicalise("https://127.0.0.1/a", allow_http=False)
    with pytest.raises(SSRFError, match="not a globally routable"):
        await resolve_and_check(parts, resolver=resolver)
    assert called == []


async def test_every_resolved_answer_must_pass():
    """One public + one private answer is a rebinding attack, not a lucky pick."""

    async def resolver(host, port):
        return ["93.184.216.34", "10.0.0.5"]

    parts = canonicalise("https://example.com/a", allow_http=False)
    with pytest.raises(SSRFError, match="10.0.0.5"):
        await resolve_and_check(parts, resolver=resolver)


async def test_empty_resolution_is_an_error():
    async def resolver(host, port):
        return []

    parts = canonicalise("https://example.com/a", allow_http=False)
    with pytest.raises(SSRFError, match="did not resolve"):
        await resolve_and_check(parts, resolver=resolver)


async def test_resolution_failure_is_reported():
    async def resolver(host, port):
        raise OSError("nxdomain")

    parts = canonicalise("https://nope.example/a", allow_http=False)
    with pytest.raises(SSRFError, match="Could not resolve"):
        await resolve_and_check(parts, resolver=resolver)


# ── fetch-level rejections never open a socket ──────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/a",
        "https://user:pw@example.com/a",
        "https://localhost/a",
        "https://db/a",
        "https://x.internal/a",
        "https://example.com:5432/a",
        "https://2130706433/a",
        "https://127.0.0.1/a",
        "https://[::1]/a",
        "https://[64:ff9b::a00:5]/a",
    ],
)
async def test_pre_connect_rejections_attempt_no_connection(url, no_connections):
    with pytest.raises(SSRFError):
        async with fetch_url_guarded(url, allow_http=False, max_bytes=1000):
            pass  # pragma: no cover


async def test_resolved_private_address_attempts_no_connection(no_connections):
    async def resolver(host, port):
        return ["10.0.0.5"]

    with pytest.raises(SSRFError, match="10.0.0.5"):
        async with fetch_url_guarded(
            "https://evil.example/a", allow_http=False, max_bytes=1000, resolver=resolver
        ):
            pass  # pragma: no cover


# ════════════════════════════════════════════════════════════════════════════
# SSRF guard — against a real local server
# ════════════════════════════════════════════════════════════════════════════

HTTP_TEST_PORT = 8080  # the only non-default http port the policy admits
HTTPS_TEST_PORT = 8443  # ditto for https


def _port_is_free(port: int) -> bool:
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    captured: dict = {}

    def log_message(self, *args):  # keep the test output clean
        pass

    def do_GET(self):  # noqa: N802
        type(self).captured = {
            "host": self.headers.get("Host"),
            "peer": self.client_address[0],
            "accept_encoding": self.headers.get("Accept-Encoding"),
            "path": self.path,
        }
        routes = {
            "/payload": (200, PNG, {"Content-Type": "image/png"}),
            "/gzipped": (200, b"nope", {"Content-Encoding": "gzip"}),
            "/notfound": (404, b"missing", {}),
            "/absolute-redirect": (302, b"", {"Location": "https://10.0.0.5/x"}),
            "/relative-redirect": (302, b"", {"Location": "/payload"}),
            "/relative-forbidden": (302, b"", {"Location": "//localhost/x"}),
            "/scheme-downgrade": (302, b"", {"Location": "http://example.com:443/x"}),
            "/no-location": (302, b"", {}),
        }
        if self.path.startswith("/hop"):
            n = int(self.path[4:] or 0)
            status, body, headers = 302, b"", {"Location": f"/hop{n + 1}"}
        elif self.path == "/huge":
            status, body, headers = 200, b"y" * 4096, {"Content-Type": "text/plain"}
        elif self.path == "/slow":
            time.sleep(2)
            status, body, headers = 200, b"late", {}
        else:
            status, body, headers = routes.get(self.path, (404, b"?", {}))

        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)


class _QuietServer(http.server.ThreadingHTTPServer):
    """A client that walks away mid-response is a *tested* case here.

    Tests that abort a stream (oversize body, deadline) close the socket while
    the handler is still writing; the stdlib would print a full traceback for
    every one of them and drown the real output.
    """

    daemon_threads = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


@pytest.fixture
def local_http():
    if not _port_is_free(HTTP_TEST_PORT):
        pytest.skip(
            f"port {HTTP_TEST_PORT} is busy; the guard's scheme-paired port rule "
            "leaves no other http port to test against"
        )
    server = _QuietServer(("127.0.0.1", HTTP_TEST_PORT), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _Handler.captured = {}
    try:
        yield f"http://127.0.0.1:{HTTP_TEST_PORT}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def loopback_policy(ip) -> bool:
    """Test-only policy: admit exactly 127.0.0.1, deny everything else.

    Injected rather than relaxing the production checker, so `canonicalise` and
    `is_forbidden_address` run completely unmodified in these tests.
    """
    return ip.compressed == "127.0.0.1"


@pytest.fixture
def recording_transport(monkeypatch):
    """Wrap `PinnedTransport` to capture what it actually put on the wire."""
    seen: dict = {}
    real = transfer.PinnedTransport

    class Recording(real):
        async def handle_async_request(self, request):
            response = await super().handle_async_request(request)
            seen["url"] = str(request.url)
            seen["host_header"] = request.headers.get("Host")
            seen["sni_hostname"] = request.extensions.get("sni_hostname")
            return response

    monkeypatch.setattr(transfer, "PinnedTransport", Recording)
    return seen


async def test_local_fetch_pins_the_address_and_keeps_the_identity(
    local_http, recording_transport
):
    async with fetch_url_guarded(
        f"{local_http}/payload",
        allow_http=True,
        max_bytes=1_000_000,
        policy=loopback_policy,
    ) as result:
        body = b"".join([chunk async for chunk in result.chunks])

    assert body == PNG
    assert result.content_type == "image/png"
    assert _Handler.captured["peer"] == "127.0.0.1"
    assert _Handler.captured["host"] == f"127.0.0.1:{HTTP_TEST_PORT}"
    assert _Handler.captured["accept_encoding"] == "identity"
    # The URL was rewritten to the pinned address while Host and SNI kept the
    # caller's name — here they coincide because the host *is* the literal.
    assert recording_transport["sni_hostname"] == "127.0.0.1"
    assert recording_transport["host_header"] == f"127.0.0.1:{HTTP_TEST_PORT}"


async def test_content_encoding_is_rejected(local_http):
    with pytest.raises(SSRFError, match="content-encoded"):
        async with fetch_url_guarded(
            f"{local_http}/gzipped", allow_http=True, max_bytes=1000, policy=loopback_policy
        ):
            pass  # pragma: no cover


async def test_non_200_final_status_is_rejected(local_http):
    with pytest.raises(SSRFError, match="got 404"):
        async with fetch_url_guarded(
            f"{local_http}/notfound", allow_http=True, max_bytes=1000, policy=loopback_policy
        ):
            pass  # pragma: no cover


async def test_declared_oversize_response_is_rejected(local_http):
    with pytest.raises(TooLarge):
        async with fetch_url_guarded(
            f"{local_http}/huge", allow_http=True, max_bytes=100, policy=loopback_policy
        ):
            pass  # pragma: no cover


async def test_absolute_redirect_to_a_private_address_is_rejected(local_http):
    with pytest.raises(SSRFError, match="10.0.0.5"):
        async with fetch_url_guarded(
            f"{local_http}/absolute-redirect",
            allow_http=True,
            max_bytes=1000,
            policy=loopback_policy,
        ):
            pass  # pragma: no cover


async def test_relative_redirect_is_resolved_and_followed(local_http):
    async with fetch_url_guarded(
        f"{local_http}/relative-redirect",
        allow_http=True,
        max_bytes=1_000_000,
        policy=loopback_policy,
    ) as result:
        body = b"".join([chunk async for chunk in result.chunks])
    assert body == PNG
    assert result.final_url.endswith("/payload")


async def test_relative_redirect_to_a_forbidden_host_is_rejected(local_http):
    with pytest.raises(SSRFError, match="local/internal"):
        async with fetch_url_guarded(
            f"{local_http}/relative-forbidden",
            allow_http=True,
            max_bytes=1000,
            policy=loopback_policy,
        ):
            pass  # pragma: no cover


async def test_redirect_that_downgrades_scheme_but_keeps_the_port_is_rejected(local_http):
    """Ports are re-paired with the scheme at every hop, not just the first."""
    with pytest.raises(SSRFError, match="not allowed for http"):
        async with fetch_url_guarded(
            f"{local_http}/scheme-downgrade",
            allow_http=True,
            max_bytes=1000,
            policy=loopback_policy,
        ):
            pass  # pragma: no cover


async def test_redirect_without_location_is_rejected(local_http):
    with pytest.raises(SSRFError, match="Location"):
        async with fetch_url_guarded(
            f"{local_http}/no-location",
            allow_http=True,
            max_bytes=1000,
            policy=loopback_policy,
        ):
            pass  # pragma: no cover


async def test_more_than_five_redirects_is_rejected(local_http):
    with pytest.raises(SSRFError, match="More than 5 redirects"):
        async with fetch_url_guarded(
            f"{local_http}/hop0", allow_http=True, max_bytes=1000, policy=loopback_policy
        ):
            pass  # pragma: no cover


async def test_deadline_covers_the_whole_fetch(local_http):
    # The service's own error, not `asyncio.timeout`'s builtin: a caller
    # should not have to know which layer ran out of time.
    with pytest.raises(Timeout, match="deadline"):
        async with fetch_url_guarded(
            f"{local_http}/slow",
            allow_http=True,
            max_bytes=1000,
            deadline=0.3,
            policy=loopback_policy,
        ):
            pass  # pragma: no cover


async def test_fetched_body_streams_into_the_vault(local_http, vault):
    """The import path end to end: guarded fetch → capped, anchored publish."""
    row = FakeRow(str(vault), "Attachments/imported.png")
    async with fetch_url_guarded(
        f"{local_http}/payload", allow_http=True, max_bytes=1_000_000, policy=loopback_policy
    ) as result:
        written = await stream_to_vault(
            row, result.chunks, max_bytes=1_000_000, deadline=deadline_in(30)
        )
    assert written["sha256"] == hashlib.sha256(PNG).hexdigest()
    assert (vault / "Attachments" / "imported.png").read_bytes() == PNG


async def test_oversize_streamed_body_leaves_nothing_behind(local_http, vault):
    row = FakeRow(str(vault), "Attachments/imported.png")
    with pytest.raises(TooLarge):
        async with fetch_url_guarded(
            f"{local_http}/huge", allow_http=True, max_bytes=1_000_000, policy=loopback_policy
        ) as result:
            await stream_to_vault(
                row, result.chunks, max_bytes=100, deadline=deadline_in(30)
            )
    assert not (vault / "Attachments" / "imported.png").exists()
    assert temps_under(vault) == []


# ── TLS: SNI and certificate verification survive the pinning ───────────────

TLS_NAME = "transfer.test"


def _openssl(*args, cwd):
    subprocess.run(
        ["openssl", *args], cwd=cwd, check=True, capture_output=True, timeout=60
    )


@pytest.fixture(scope="module")
def tls_material(tmp_path_factory):
    """A throwaway CA plus a leaf for `transfer.test`, generated at test time."""
    if not any(
        os.access(os.path.join(p, "openssl"), os.X_OK)
        for p in os.environ.get("PATH", "").split(os.pathsep)
        if p
    ):
        pytest.skip("openssl CLI is not available")

    d = tmp_path_factory.mktemp("tls")
    try:
        _openssl(
            "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", "ca.key", "-out", "ca.pem", "-days", "1",
            "-subj", "/CN=transfer-test-ca",
            cwd=d,
        )
        _openssl(
            "req", "-newkey", "rsa:2048", "-nodes",
            "-keyout", "leaf.key", "-out", "leaf.csr",
            "-subj", f"/CN={TLS_NAME}",
            cwd=d,
        )
        (d / "ext.cnf").write_text(
            f"subjectAltName=DNS:{TLS_NAME}\nbasicConstraints=CA:FALSE\n"
        )
        _openssl(
            "x509", "-req", "-in", "leaf.csr", "-CA", "ca.pem", "-CAkey", "ca.key",
            "-CAcreateserial", "-out", "leaf.pem", "-days", "1",
            "-extfile", "ext.cnf",
            cwd=d,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        pytest.skip(f"could not generate TLS material with openssl: {exc}")
    return d


@pytest.fixture
def local_https(tls_material):
    if not _port_is_free(HTTPS_TEST_PORT):
        pytest.skip(f"port {HTTPS_TEST_PORT} is busy")

    sni_seen: list[str | None] = []
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(tls_material / "leaf.pem", tls_material / "leaf.key")

    def on_sni(sock, name, ctx):
        sni_seen.append(name)

    context.sni_callback = on_sni

    server = _QuietServer(("127.0.0.1", HTTPS_TEST_PORT), _Handler)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _Handler.captured = {}
    try:
        yield sni_seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def trusting_transport(monkeypatch, tls_material):
    """`PinnedTransport` that trusts only the throwaway CA."""
    real = transfer.PinnedTransport
    seen: dict = {}

    class Trusting(real):
        def __init__(self, **kwargs):
            kwargs.setdefault("verify", str(tls_material / "ca.pem"))
            super().__init__(**kwargs)

        async def handle_async_request(self, request):
            response = await super().handle_async_request(request)
            seen["url"] = str(request.url)
            seen["host_header"] = request.headers.get("Host")
            seen["sni_hostname"] = request.extensions.get("sni_hostname")
            return response

    monkeypatch.setattr(transfer, "PinnedTransport", Trusting)
    return seen


async def test_pinned_tls_connection_sends_sni_and_verifies_the_name(
    local_https, trusting_transport
):
    """The pinned connection must still *be* a connection to `transfer.test`.

    Rewriting the URL host to an IP is what makes pinning work, and it is also
    what would silently turn certificate verification into "verify against
    127.0.0.1" — i.e. always fail, or worse, be disabled to make it pass. This
    asserts both halves: the server saw SNI `transfer.test`, and the client
    verified the leaf against that name using only the throwaway CA.
    """
    async def resolver(host, port):
        assert host == TLS_NAME
        return ["127.0.0.1"]

    url = f"https://{TLS_NAME}:{HTTPS_TEST_PORT}/payload"
    async with fetch_url_guarded(
        url,
        allow_http=False,
        max_bytes=1_000_000,
        resolver=resolver,
        policy=loopback_policy,
    ) as result:
        body = b"".join([chunk async for chunk in result.chunks])

    assert body == PNG
    assert local_https == [TLS_NAME], f"server saw SNI {local_https}"
    assert trusting_transport["sni_hostname"] == TLS_NAME
    assert trusting_transport["host_header"] == f"{TLS_NAME}:{HTTPS_TEST_PORT}"
    # The socket really did go to the pinned address, not to a DNS lookup.
    assert "127.0.0.1" in trusting_transport["url"]
    assert _Handler.captured["peer"] == "127.0.0.1"


async def test_tls_verification_fails_for_the_wrong_name(local_https, trusting_transport):
    """Certificate verification is real: another name on the same address fails."""

    async def resolver(host, port):
        return ["127.0.0.1"]

    with pytest.raises(SSRFError, match="Fetch failed"):
        async with fetch_url_guarded(
            f"https://other.test:{HTTPS_TEST_PORT}/payload",
            allow_http=False,
            max_bytes=1000,
            resolver=resolver,
            policy=loopback_policy,
        ):
            pass  # pragma: no cover
