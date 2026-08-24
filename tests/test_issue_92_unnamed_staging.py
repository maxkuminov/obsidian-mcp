"""Transfer staging holds no directory entry — and the flagged fallback (#92 item 1, #103).

The note path has published from an unnamed `O_TMPFILE` inode since #59: nothing
in the staging directory to observe, replace or race, and nothing to clean up
afterwards, because an inode with no name is freed when the last descriptor
closes. The transfer path staged under a `.tmp-*` name in `.transfer-tmp` for
the whole of a multi-minute body. This module is the gate on that having
converged.

Two things are deliberately *not* asserted here, because the design says they
cannot be:

- the **overwrite** publish is not nameless. `renameat` has no by-descriptor
  form, so the staged inode is given a transient name inside the publish gate,
  immediately before the fingerprint check and the rename (D20). What is
  asserted is where that name lives and how long — the staging directory, and
  only inside the gate — and that a substitution observable at the identity
  check refuses. A substitution landing between that check and the rename is a
  declared residual, and a test that demanded detection there would be a test no
  implementation can pass.
- the **named-staging fallback** reopens the window for the streaming window,
  behind `VAULT_ALLOW_NAMED_STAGING_FALLBACK`, on mounts whose server refuses
  `O_TMPFILE` outright (#103). What is asserted is that it is off by default,
  that the refusal names the flag, that the mode is chosen once by the probe
  rather than per call, that the fallback carries the two guards the pre-change
  path did not have, and that it announces itself once per process and on
  `/health`.
"""
from __future__ import annotations

import errno
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from src.services import transfer, vault_fs
from src.services.transfer import stream_to_vault


# ── scaffolding ─────────────────────────────────────────────────────────────


@dataclass
class FakeRow:
    """The four fields `stream_to_vault` actually reads off a token row."""

    vault_root: str
    path: str
    overwrite: bool = False
    expected_fingerprint: dict | None = None


def deadline_in(seconds: float) -> float:
    return time.monotonic() + seconds


async def chunks_of(*parts: bytes):
    for part in parts:
        yield part


class Gate:
    """Minimal stand-in for the locked pre-publication transaction."""

    def __init__(self, on_enter=None):
        self.entered = 0
        self._on_enter = on_enter

    def __call__(self):
        return self

    async def __aenter__(self):
        self.entered += 1
        if self._on_enter is not None:
            self._on_enter()

        async def record(result, published):
            return None

        return transfer.GateHandle(ok=True, session=self, on_complete=record)

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def vault(tmp_path):
    (tmp_path / "Attachments").mkdir()
    vault_fs.reset_filesystem_probe_cache()
    vault_fs.reset_named_staging_state()
    yield tmp_path
    vault_fs.reset_filesystem_probe_cache()
    vault_fs.reset_named_staging_state()


def staging_entries(vault: Path) -> list[str]:
    staging = vault / vault_fs.STAGING_DIR
    if not staging.is_dir():
        return []
    return sorted(p.name for p in staging.iterdir())


def substitute(path: Path, payload: bytes = b"not ours") -> None:
    """Replace `path` with a different inode, in one step.

    Deliberately not `unlink` + `write_bytes`: a filesystem is free to hand the
    new file the inode number the old one just released, and the identity check
    under test compares exactly that. Staging the decoy first and renaming it
    over the name keeps both inodes alive at once, so the numbers must differ.
    """
    decoy = path.with_name(path.name + "-decoy")
    decoy.write_bytes(payload)
    os.replace(decoy, path)


def refuse_o_tmpfile(monkeypatch) -> None:
    """Make the kernel answer `EOPNOTSUPP` to `O_TMPFILE`, as TrueNAS NFS does.

    Simulated at `os.open` rather than at `create_nameless_temp`, so the probe
    and any streaming path meet the same failure a real mount presents (#103).
    """
    real_open = os.open

    def guarded(path, flags, *args, **kwargs):
        if flags & getattr(os, "O_TMPFILE", 0) == getattr(os, "O_TMPFILE", 0):
            raise OSError(errno.EOPNOTSUPP, "operation not supported")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(vault_fs.os, "open", guarded)


def allow_fallback(monkeypatch, value: bool = True) -> None:
    monkeypatch.setattr(
        vault_fs.settings, "vault_allow_named_staging_fallback", value
    )


async def upload(vault: Path, path: str, payload: bytes = b"payload", **row_kw):
    row = FakeRow(str(vault), path, **row_kw)
    return await stream_to_vault(
        row, chunks_of(payload), max_bytes=1000, deadline=deadline_in(30)
    )


# ════════════════════════════════════════════════════════════════════════════
# 3.7 — the unnamed mode
# ════════════════════════════════════════════════════════════════════════════


async def test_the_staging_directory_holds_no_entry_while_a_body_streams(vault):
    """The property, asserted where it matters: *during* the stream.

    An end-state assertion cannot see the difference — the pre-change path also
    left `.transfer-tmp` empty once the publish had consumed the name. So the
    listing is taken from inside the chunk iterator, between two chunks, with
    the body half-written and the descriptor open.
    """
    seen: list[list[str]] = []

    async def stalling_chunks():
        yield b"first"
        seen.append(staging_entries(vault))
        yield b"second"
        seen.append(staging_entries(vault))

    row = FakeRow(str(vault), "Attachments/a.bin")
    await stream_to_vault(
        row, stalling_chunks(), max_bytes=1000, deadline=deadline_in(30)
    )

    assert seen == [[], []], seen
    assert (vault / vault_fs.STAGING_DIR).is_dir(), "the directory itself stays"
    assert (vault / "Attachments" / "a.bin").read_bytes() == b"firstsecond"


async def test_an_abandoned_upload_leaves_nothing_for_the_sweep(vault):
    """The kernel reclaims an unnamed inode when the last descriptor closes, so
    there is no litter — which is what makes the 24-hour sweep a collector of
    pre-change files rather than a permanent necessity (D19)."""

    class Boom(RuntimeError):
        pass

    async def dying_chunks():
        yield b"half a body"
        raise Boom("the client went away")

    row = FakeRow(str(vault), "Attachments/a.bin")
    with pytest.raises(Boom):
        await stream_to_vault(
            row, dying_chunks(), max_bytes=1000, deadline=deadline_in(30)
        )

    assert staging_entries(vault) == []
    assert not (vault / "Attachments" / "a.bin").exists()


async def test_the_overwrite_transient_name_exists_only_inside_the_gate(vault):
    """It is a name, and it is in `.transfer-tmp` — never in the destination."""
    target = vault / "Attachments" / "a.bin"
    target.write_bytes(b"old")
    root_fd = vault_fs.open_root(vault)
    try:
        dir_fd = vault_fs.open_dir_beneath(root_fd, "Attachments")
        try:
            want = vault_fs.fingerprint(dir_fd, "a.bin", hash_up_to=10_000)
        finally:
            os.close(dir_fd)
    finally:
        os.close(root_fd)

    at_gate: list[list[str]] = []
    at_rename: list[tuple[list[str], list[str]]] = []
    real_replace = vault_fs.os.replace

    def recording_replace(*args, **kwargs):
        at_rename.append(
            (
                staging_entries(vault),
                sorted(p.name for p in (vault / "Attachments").iterdir()),
            )
        )
        return real_replace(*args, **kwargs)

    gate = Gate(on_enter=lambda: at_gate.append(staging_entries(vault)))
    row = FakeRow(str(vault), "Attachments/a.bin", overwrite=True,
                  expected_fingerprint=want)
    monkey = pytest.MonkeyPatch()
    monkey.setattr(vault_fs.os, "replace", recording_replace)
    try:
        await stream_to_vault(
            row,
            chunks_of(b"new"),
            max_bytes=1000,
            deadline=deadline_in(30),
            before_publish=gate,
        )
    finally:
        monkey.undo()

    assert at_gate == [[]], "a name existed before the gate opened"
    staged_at_rename, destination_at_rename = at_rename[0]
    assert len(staged_at_rename) == 1, staged_at_rename
    assert staged_at_rename[0].startswith(".tmp-")
    assert destination_at_rename == ["a.bin"], "the name was in the destination"
    assert target.read_bytes() == b"new"
    assert staging_entries(vault) == [], "the transient name outlived the publish"


async def test_a_substituted_transient_name_refuses_and_is_left_alone(vault):
    """The identity check runs before the rename, and the substitute survives it.

    Answering a substitution by deleting the substitute is the same
    destructive-write class this module exists to prevent, aimed at a different
    file — so the failure direction is to leave litter, never to remove
    something we cannot prove is ours.
    """
    target = vault / "Attachments" / "a.bin"
    target.write_bytes(b"old")
    root_fd = vault_fs.open_root(vault)
    try:
        dir_fd = vault_fs.open_dir_beneath(root_fd, "Attachments")
        try:
            want = vault_fs.fingerprint(dir_fd, "a.bin", hash_up_to=10_000)
        finally:
            os.close(dir_fd)
    finally:
        os.close(root_fd)

    transient: list[str] = []
    real_materialise = vault_fs._materialise_staged_name
    real_committed = vault_fs._require_committed_target

    def capturing_materialise(staged_fd, dir_fd):
        name = real_materialise(staged_fd, dir_fd)
        transient.append(name)
        return name

    def substituting(*args, **kwargs):
        # Between the transient name being created and the identity check —
        # the one interval in which a substitution is *observable*.
        substitute(vault / vault_fs.STAGING_DIR / transient[0])
        return real_committed(*args, **kwargs)

    monkey = pytest.MonkeyPatch()
    monkey.setattr(vault_fs, "_materialise_staged_name", capturing_materialise)
    monkey.setattr(vault_fs, "_require_committed_target", substituting)
    row = FakeRow(str(vault), "Attachments/a.bin", overwrite=True,
                  expected_fingerprint=want)
    try:
        with pytest.raises(vault_fs.Conflict):
            await stream_to_vault(
                row, chunks_of(b"new"), max_bytes=1000, deadline=deadline_in(30)
            )
    finally:
        monkey.undo()

    assert target.read_bytes() == b"old", "the incumbent was overwritten"
    left = vault / vault_fs.STAGING_DIR / transient[0]
    assert left.read_bytes() == b"not ours", "the substitute was unlinked"


def test_a_filesystem_that_cannot_flush_a_directory_is_refused_even_with_the_flag(
    vault, monkeypatch
):
    """The flag buys back named staging, and nothing else.

    A root that does `O_TMPFILE` and `linkat` but rejects a directory `fsync`
    would otherwise take a token and a whole body, publish, and only then strand
    the claim as a post-publication failure. The flag must not become a way past
    the other primitives the probe establishes.
    """
    allow_fallback(monkeypatch)
    real = os.fsync

    def refuse(fd):
        if os.stat(fd).st_mode & 0o170000 == 0o040000:
            raise OSError(errno.EINVAL, "not supported")
        return real(fd)

    monkeypatch.setattr(os, "fsync", refuse)

    with pytest.raises(vault_fs.UnsupportedFilesystem) as exc:
        vault_fs.check_publication_support(vault)

    assert "a directory" in str(exc.value)
    assert staging_entries(vault) == []


# ════════════════════════════════════════════════════════════════════════════
# 3.7a — mode selection and the flagged fallback (D27)
# ════════════════════════════════════════════════════════════════════════════


def test_the_probe_refuses_without_the_flag_and_names_it(vault, monkeypatch):
    """No token is minted and no body is streamed: the refusal is at the probe."""
    refuse_o_tmpfile(monkeypatch)

    with pytest.raises(vault_fs.UnsupportedFilesystem) as exc:
        vault_fs.check_publication_support(vault)

    message = str(exc.value)
    assert "O_TMPFILE" in message
    assert "VAULT_ALLOW_NAMED_STAGING_FALLBACK" in message
    assert staging_entries(vault) == []
    assert list((vault / "Attachments").iterdir()) == []


def test_the_probe_refuses_without_proc_and_names_the_flag(vault, monkeypatch):
    """The other half of the same capability: staging without a name is no use
    if the inode cannot then be published by descriptor."""
    monkeypatch.setattr(vault_fs, "_proc_fd_available_cache", False)

    with pytest.raises(vault_fs.UnsupportedFilesystem) as exc:
        vault_fs.check_publication_support(vault)

    assert "VAULT_ALLOW_NAMED_STAGING_FALLBACK" in str(exc.value)


async def test_a_refused_root_stages_and_publishes_nothing(vault, monkeypatch):
    refuse_o_tmpfile(monkeypatch)

    with pytest.raises(vault_fs.UnsupportedFilesystem):
        await upload(vault, "Attachments/a.bin")

    assert staging_entries(vault) == []
    assert not (vault / "Attachments" / "a.bin").exists()


def test_the_probe_selects_the_fallback_when_the_flag_is_set(vault, monkeypatch):
    refuse_o_tmpfile(monkeypatch)
    allow_fallback(monkeypatch)

    assert (
        vault_fs.check_publication_support(vault) == vault_fs.STAGING_MODE_NAMED
    )


def test_a_supported_root_selects_unnamed_staging(vault):
    assert (
        vault_fs.check_publication_support(vault) == vault_fs.STAGING_MODE_UNNAMED
    )


async def test_the_recorded_mode_drives_every_later_upload_and_is_not_re_decided(
    vault, monkeypatch
):
    """The probe decides once. A root that staged one upload without a name and
    the next one under a name would make the window each upload ran in
    unknowable after the fact."""
    refuse_o_tmpfile(monkeypatch)
    allow_fallback(monkeypatch)

    probes = 0
    real_probe = vault_fs.probe_publication

    def counting_probe(root_fd):
        nonlocal probes
        probes += 1
        return real_probe(root_fd)

    monkeypatch.setattr(vault_fs, "probe_publication", counting_probe)

    staged_names: list[list[str]] = []

    async def peeking_chunks(payload: bytes):
        yield payload
        staged_names.append(staging_entries(vault))

    for name in ("a.bin", "b.bin"):
        row = FakeRow(str(vault), f"Attachments/{name}")
        await stream_to_vault(
            row, peeking_chunks(b"payload"), max_bytes=1000,
            deadline=deadline_in(30),
        )

    assert probes == 1, "the probe ran again for a root it had already answered"
    assert len(staged_names) == 2
    for entries in staged_names:
        assert len(entries) == 1 and entries[0].startswith(".tmp-"), entries
    assert (vault / "Attachments" / "a.bin").read_bytes() == b"payload"
    assert (vault / "Attachments" / "b.bin").read_bytes() == b"payload"


async def test_the_fallback_publish_is_still_no_clobber(vault, monkeypatch):
    """`link()`, never a replacing rename. The existing file is untouched and
    the caller gets the conflict that releases the claim."""
    refuse_o_tmpfile(monkeypatch)
    allow_fallback(monkeypatch)
    target = vault / "Attachments" / "a.bin"
    target.write_bytes(b"incumbent")

    replaced: list[tuple] = []
    real_replace = vault_fs.os.replace

    def recording_replace(*args, **kwargs):
        replaced.append(args)
        return real_replace(*args, **kwargs)

    monkeypatch.setattr(vault_fs.os, "replace", recording_replace)

    with pytest.raises(vault_fs.Conflict):
        await upload(vault, "Attachments/a.bin", b"mine")

    assert target.read_bytes() == b"incumbent"
    assert replaced == [], "the no-clobber publish degraded to a replacing rename"
    assert staging_entries(vault) == []


async def test_the_fallback_verifies_the_staged_name_before_publishing(
    vault, monkeypatch
):
    """The guard the pre-change path did not have. A name that lives for the
    whole streaming window needs it more, not less (D27) — and the substitute is
    left in place rather than unlinked."""
    refuse_o_tmpfile(monkeypatch)
    allow_fallback(monkeypatch)

    staged_name: list[str] = []
    real_require = vault_fs.require_staged_name

    def substituting(dir_fd, name, staged):
        if not staged_name:
            staged_name.append(name)
            substitute(vault / vault_fs.STAGING_DIR / name)
        return real_require(dir_fd, name, staged)

    monkeypatch.setattr(vault_fs, "require_staged_name", substituting)

    with pytest.raises(vault_fs.Conflict):
        await upload(vault, "Attachments/a.bin", b"mine")

    assert not (vault / "Attachments" / "a.bin").exists()
    left = vault / vault_fs.STAGING_DIR / staged_name[0]
    assert left.read_bytes() == b"not ours", "the substitute was unlinked"


async def test_no_cleanup_path_unlinks_a_substituted_staging_name(
    vault, monkeypatch
):
    """The abandon path, not the publish path: an upload that dies mid-body over
    a staging name somebody else has taken must leave that file alone."""
    refuse_o_tmpfile(monkeypatch)
    allow_fallback(monkeypatch)

    class Boom(RuntimeError):
        pass

    async def dying_chunks():
        yield b"half"
        entries = staging_entries(vault)
        substitute(vault / vault_fs.STAGING_DIR / entries[0])
        raise Boom("the client went away")

    row = FakeRow(str(vault), "Attachments/a.bin")
    with pytest.raises(Boom):
        await stream_to_vault(
            row, dying_chunks(), max_bytes=1000, deadline=deadline_in(30)
        )

    left = staging_entries(vault)
    assert len(left) == 1, left
    assert (vault / vault_fs.STAGING_DIR / left[0]).read_bytes() == b"not ours"


async def test_the_fallback_warns_once_per_process_on_first_exercise(
    vault, monkeypatch, caplog
):
    """Setting the flag, starting up and the probe selecting the mode each log
    nothing: the value of the warning is that it distinguishes an operator who
    enabled this defensively from a mount that is taking it."""
    refuse_o_tmpfile(monkeypatch)
    allow_fallback(monkeypatch)

    with caplog.at_level(logging.WARNING, logger="src.services.vault_fs"):
        assert (
            vault_fs.check_publication_support(vault)
            == vault_fs.STAGING_MODE_NAMED
        )
        assert not [
            r for r in caplog.records
            if "VAULT_ALLOW_NAMED_STAGING_FALLBACK" in r.getMessage()
        ], "the probe selecting the mode logged the warning"

        await upload(vault, "Attachments/a.bin")
        await upload(vault, "Attachments/b.bin")

    warned = [
        r for r in caplog.records
        if "VAULT_ALLOW_NAMED_STAGING_FALLBACK" in r.getMessage()
    ]
    assert len(warned) == 1, [r.getMessage() for r in warned]


async def _health() -> dict:
    import src.main as main_module

    return json.loads((await main_module.health()).body)


async def test_health_reports_the_fallback_and_creates_nothing(
    vault, monkeypatch
):
    """One field, shared with the note path, read from process state — a probe
    writes, and a health check must not create a file in the vault."""
    refuse_o_tmpfile(monkeypatch)
    allow_fallback(monkeypatch)

    before = vault / "before"
    before.mkdir()
    assert (await _health())["vault_named_staging_fallback_active"] is False

    await upload(vault, "Attachments/a.bin")

    assert (await _health())["vault_named_staging_fallback_active"] is True
    # The report is process state, so asking for it neither probes nor writes.
    assert staging_entries(vault) == []
    assert sorted(p.name for p in vault.iterdir()) == [
        vault_fs.STAGING_DIR, "Attachments", "before",
    ]


async def test_health_stays_inactive_when_the_flag_is_set_but_unused(
    vault, monkeypatch
):
    """An operator who enabled the flag defensively is not a mount taking the
    fallback, and the field is the place that distinguishes them."""
    allow_fallback(monkeypatch)
    assert (
        vault_fs.check_publication_support(vault) == vault_fs.STAGING_MODE_UNNAMED
    )

    assert (await _health())["vault_named_staging_fallback_active"] is False


async def test_an_abandoned_fallback_upload_is_collected_by_the_sweep(
    vault, monkeypatch
):
    """The fallback produces litter exactly as the pre-change path did, which is
    a second reason the 24-hour sweep stays (D27)."""
    refuse_o_tmpfile(monkeypatch)
    allow_fallback(monkeypatch)

    class Boom(RuntimeError):
        pass

    async def dying_chunks():
        yield b"half"
        # Kill the process's grip on the name without letting the abandon path
        # run: this is the crash case, which is what the sweep is for.
        raise Boom("killed")

    row = FakeRow(str(vault), "Attachments/a.bin")
    real_discard = vault_fs.discard_temp
    monkeypatch.setattr(vault_fs, "discard_temp", lambda *a, **k: False)
    with pytest.raises(Boom):
        await stream_to_vault(
            row, dying_chunks(), max_bytes=1000, deadline=deadline_in(30)
        )
    monkeypatch.setattr(vault_fs, "discard_temp", real_discard)

    left = staging_entries(vault)
    assert len(left) == 1, left

    stale = vault / vault_fs.STAGING_DIR / left[0]
    old = time.time() - 25 * 60 * 60
    os.utime(stale, (old, old))

    root_fd = vault_fs.open_root(vault)
    try:
        assert vault_fs.prune_stale_staging(root_fd) == 1
    finally:
        os.close(root_fd)
    assert staging_entries(vault) == []


@pytest.mark.parametrize("fallback", [False, True])
async def test_a_consumed_staging_name_is_not_reported_as_a_substitution(
    vault, monkeypatch, caplog, fallback
):
    """A successful overwrite publish *consumes* the name, so by the time the
    discard runs there is nothing there. An absent name is the ordinary case and
    must not be logged as somebody having taken it over — that warning is how an
    operator would learn about a real substitution, and it is worthless if every
    overwrite emits one. Both modes reach the same discard: the unnamed mode
    with its transient in-gate name, the fallback with its streaming-window one.
    """
    if fallback:
        refuse_o_tmpfile(monkeypatch)
        allow_fallback(monkeypatch)
    target = vault / "Attachments" / "a.bin"
    target.write_bytes(b"old")
    root_fd = vault_fs.open_root(vault)
    try:
        dir_fd = vault_fs.open_dir_beneath(root_fd, "Attachments")
        try:
            want = vault_fs.fingerprint(dir_fd, "a.bin", hash_up_to=10_000)
        finally:
            os.close(dir_fd)
    finally:
        os.close(root_fd)

    with caplog.at_level(logging.WARNING, logger="src.services.vault_fs"):
        await upload(
            vault, "Attachments/a.bin", b"new",
            overwrite=True, expected_fingerprint=want,
        )

    assert target.read_bytes() == b"new"
    assert not [
        r for r in caplog.records if "no longer refers" in r.getMessage()
    ], [r.getMessage() for r in caplog.records]
