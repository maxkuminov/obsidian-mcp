"""Published file permissions on a real filesystem (#447).

Every file the server wrote used to be published with `0o666 & ~umask`: under
a directory default ACL that `chmod` cut a group's mask to `r--`, and an edit
replaced whatever mode the file had (a 0600 note became 0644). The fix keeps a
replaced file's bits, access ACL and gid, gives a new file exactly what
`open(O_CREAT, 0o666)` gets in its folder, and translates the owning group to
a named entry where the server cannot set it.

Everything here runs against `tmp_path` on the real filesystem. ACLs are
written with `os.setxattr` and a packed struct, so there is no `setfacl`
dependency. The oracles are **control files** made by the kernel itself in
the same directory (`os.open(O_CREAT, 0o666)`, `os.mkdir(0o777)`), and, where
a group cannot be kept, an effective-access helper that walks `acl(5)`'s
evaluation order (design K7).

A filesystem without POSIX ACLs skips the tests that need them — unless
`OMCP_TEST_REQUIRE_POSIX_ACL=1`, which CI sets, turns that skip into a
failure: a gate that silently skips is not a gate.

Each test names the spec scenario it covers in its docstring.
"""
from __future__ import annotations

import contextlib
import errno
import hashlib
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

import src.mcp_server.tools as tools
from src.mcp_server.auth import current_permission
from src.services import transfer, vault_fs
from src.services import vault as vault_service
from src.services.transfer import stream_to_vault
from src.services.vault_fs import (
    ACL_GROUP,
    ACL_GROUP_OBJ,
    ACL_MASK,
    ACL_OTHER,
    ACL_UNDEFINED_ID,
    ACL_USER,
    ACL_USER_OBJ,
    Conflict,
)

# The route harness, for the scenarios whose observable is the token state.
from tests.test_transfer_routes import (  # noqa: F401 - fixtures by name
    _reset_limiter,
    auth,
    client,
    harness,
    temp_files,
    vault,
)

REQUIRE_ACL_ENV = "OMCP_TEST_REQUIRE_POSIX_ACL"

ACCESS = vault_fs.ACL_ACCESS_XATTR
DEFAULT = vault_fs.ACL_DEFAULT_XATTR

# The real xattr calls, for assertions made while a test has replaced them.
_getxattr = os.getxattr
_setxattr = os.setxattr

# Arbitrary ids for named entries. The kernel accepts a named entry for any id
# from the inode's owner (K3); none of these needs to exist.
NAMED = 4242
OTHER_NAMED = 4243

# Principals that are neither the server nor any file's previous owner.
P_UID_A, P_UID_B, P_UID_C, P_UID_D = 50001, 50002, 50003, 50004

U = ACL_UNDEFINED_ID


def group_rwx_default() -> tuple:
    """`u::rwx,g::rwx,g:<NAMED>:rwx,m::rwx,o::r-x`, the issue's folder."""
    return (
        (ACL_USER_OBJ, 7, U),
        (ACL_GROUP_OBJ, 7, U),
        (ACL_GROUP, 7, NAMED),
        (ACL_MASK, 7, U),
        (ACL_OTHER, 5, U),
    )


def private_default() -> tuple:
    """`u::rwx,g::---,o::---`."""
    return ((ACL_USER_OBJ, 7, U), (ACL_GROUP_OBJ, 0, U), (ACL_OTHER, 0, U))


# ── filesystem helpers ──────────────────────────────────────────────────────


def set_acl(path: Path, entries, *, which: str = DEFAULT) -> None:
    _setxattr(str(path), which, vault_fs.pack_acl(entries))


def xattr(path: Path, which: str = ACCESS) -> bytes | None:
    try:
        return _getxattr(str(path), which, follow_symlinks=False)
    except OSError as exc:
        if exc.errno == errno.ENODATA:
            return None
        raise


def perms(path: Path) -> tuple[int, int, bytes | None]:
    """`(st_mode & 0o777, st_gid, access ACL bytes or None)` — the oracle."""
    st = os.lstat(path)
    return stat.S_IMODE(st.st_mode) & 0o777, st.st_gid, xattr(path)


@contextlib.contextmanager
def _dirfd_cm(directory: Path):
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        yield fd
    finally:
        os.close(fd)


def control_file(directory: Path, name: str = "control.bin") -> Path:
    """What the kernel gives `open(O_CREAT, 0o666)` in `directory`."""
    with _dirfd_cm(directory) as dfd:
        fd = os.open(name, os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o666, dir_fd=dfd)
        os.close(fd)
    return directory / name


def acl_entries(path: Path) -> tuple | None:
    raw = xattr(path)
    return None if raw is None else vault_fs.parse_acl(raw)


def effective(path: Path, uid: int, groups: set[int]) -> frozenset[str]:
    """Read/write `uid` with `groups` gets on `path`, by acl(5)'s order (K7).

    1. the owner: `USER_OBJ`; 2. a named user: that entry, masked; 3. the
    owning group and named groups that match, masked: granted when any one
    holds the permission, refused when some matched and none did; 4. other.
    Without an ACL the same walk runs over the mode's minimal ACL.
    """
    st = os.lstat(path)
    entries = acl_entries(path) or vault_fs.minimal_acl(stat.S_IMODE(st.st_mode))
    mask = next((p for t, p, _ in entries if t == ACL_MASK), None)

    def masked(perm: int) -> int:
        return perm if mask is None else perm & mask

    def grants(bit: int) -> bool:
        if uid == st.st_uid:
            return bool(next(p for t, p, _ in entries if t == ACL_USER_OBJ) & bit)
        for tag, perm, ident in entries:
            if tag == ACL_USER and ident == uid:
                return bool(masked(perm) & bit)
        matched = False
        granted = False
        for tag, perm, ident in entries:
            if (tag == ACL_GROUP_OBJ and st.st_gid in groups) or (
                tag == ACL_GROUP and ident in groups
            ):
                matched = True
                granted = granted or bool(masked(perm) & bit)
        if matched:
            return granted
        return bool(next(p for t, p, _ in entries if t == ACL_OTHER) & bit)

    return frozenset(
        name for name, bit in (("r", 4), ("w", 2)) if grants(bit)
    )


def snapshot(path: Path) -> tuple:
    return path.read_bytes(), os.lstat(path).st_mode, xattr(path)


def refusal_code(result: str) -> str | None:
    last = result.rstrip().splitlines()[-1]
    if not last.startswith("MCP-REFUSAL "):
        return None
    import json

    return json.loads(last[len("MCP-REFUSAL "):])["code"]


# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def acl_fs(tmp_path):
    """The probe: can this filesystem hold a default ACL at all?

    Skips on `EOPNOTSUPP`/`ENOTSUP`; fails instead when
    `OMCP_TEST_REQUIRE_POSIX_ACL=1`. Read through `os.environ` only — it is
    not a `Settings` field, so the conftest's hermetic environment is
    untouched.
    """
    probe = tmp_path / ".acl-probe"
    probe.mkdir()
    try:
        set_acl(probe, private_default())
    except OSError as exc:
        if exc.errno not in (errno.EOPNOTSUPP, errno.ENOTSUP):
            raise
        message = f"no POSIX ACLs on the test filesystem ({tmp_path})"
        if os.environ.get(REQUIRE_ACL_ENV) == "1":
            pytest.fail(f"{message}, and {REQUIRE_ACL_ENV}=1")
        pytest.skip(message)
    finally:
        probe.rmdir()
    return tmp_path


@pytest.fixture
def other_gid():
    """A supplementary group of this process, other than its effective gid.

    Lets a test give a file a gid the staged inode does not get by itself,
    which is what makes the server call `fchown` at all. Its absence is a
    property of the test user, not of the filesystem, so it is always a skip —
    `OMCP_TEST_REQUIRE_POSIX_ACL` is about ACL support only. The translation
    itself stays covered without one: `test_apply_plan_translates_when_the_
    kernel_refuses_the_group` asks the kernel for a group nobody holds.
    """
    egid = os.getegid()
    candidates = sorted(g for g in os.getgroups() if g != egid)
    if not candidates:
        pytest.skip(
            "the test user has no supplementary group to give a file a gid "
            "other than the one a new file gets"
        )
    return candidates[0]


@pytest.fixture
def notes(monkeypatch, tmp_path):
    """The note tools pointed at `tmp_path`, offline, with write permission."""

    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(vault_service.settings, "vault_path", str(tmp_path))
    monkeypatch.setattr(tools, "_log_usage", noop)
    vault_fs.reset_filesystem_probe_cache()
    token = current_permission.set("readwrite")
    yield tmp_path
    current_permission.reset(token)
    vault_fs.reset_filesystem_probe_cache()


@pytest.fixture
def group_not_kept(monkeypatch):
    """`fchown` refuses as it does for a group the process is not in (K5)."""
    calls: list[int] = []

    def refuse(fd, uid, gid):
        calls.append(gid)
        raise PermissionError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(os, "fchown", refuse)
    return calls


@pytest.fixture
def no_xattrs(monkeypatch):
    """Every xattr call fails `EOPNOTSUPP`: a filesystem without ACLs."""

    def unsupported(*args, **kwargs):
        raise OSError(errno.EOPNOTSUPP, "Operation not supported")

    for name in ("getxattr", "setxattr", "removexattr", "listxattr"):
        monkeypatch.setattr(os, name, unsupported)


@pytest.fixture(autouse=True)
def _container_umask(monkeypatch):
    """Every test runs under umask 0o022, the reference container's.

    Not cosmetic: under a developer's 0o002 the old fixed mode was 0o664,
    which happens to give a group-`rwx` folder `mask::rw-`, so the bug this
    module exists for would not reproduce.
    """
    previous = os.umask(0o022)
    monkeypatch.setattr(vault_fs, "_default_file_mode_cache", None)
    yield
    os.umask(previous)
    vault_fs._default_file_mode_cache = None


@pytest.fixture
def umask(monkeypatch, _container_umask):
    """Set another process umask for one test, with the cached default reset."""

    def set_to(value: int) -> None:
        os.umask(value)
        monkeypatch.setattr(vault_fs, "_default_file_mode_cache", None)

    return set_to


@dataclass
class Row:
    vault_root: str
    path: str
    overwrite: bool = False
    expected_fingerprint: dict | None = None


async def chunks(*parts: bytes):
    for part in parts:
        yield part


def fingerprint_of(path: Path) -> dict:
    st = os.lstat(path)
    return {
        "dev": st.st_dev,
        "inode": st.st_ino,
        "size": st.st_size,
        "mtime_ns": st.st_mtime_ns,
        "ctime_ns": st.st_ctime_ns,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


async def upload(root: Path, rel: str, body: bytes = b"payload", **row_kwargs):
    return await stream_to_vault(
        Row(str(root), rel, **row_kwargs),
        chunks(body),
        max_bytes=1_000_000,
        content_length=len(body),
        deadline=time.monotonic() + 30,
    )


def note_in(directory: Path, name: str, body: str = "before\n") -> Path:
    """A note made by an ordinary `open(O_CREAT, 0o666)` in `directory`."""
    path = control_file(directory, name)
    path.write_text(body, encoding="utf-8")
    return path


def strip_acl(path: Path) -> None:
    with contextlib.suppress(OSError):
        os.removexattr(str(path), ACCESS)


# ════════════════════════════════════════════════════════════════════════════
# Helpers (vault_fs): codec, plans, translation
# ════════════════════════════════════════════════════════════════════════════


def test_acl_codec_round_trips_a_kernel_written_value(acl_fs):
    """The codec reads what the kernel writes and writes it back byte-equal."""
    d = acl_fs / "d"
    d.mkdir()
    set_acl(d, group_rwx_default())
    raw = xattr(d, DEFAULT)
    entries = vault_fs.parse_acl(raw)
    assert entries == group_rwx_default()
    assert vault_fs.pack_acl(entries) == raw
    # Packing sorts into the kernel's order.
    assert vault_fs.pack_acl(reversed(entries)) == raw


@pytest.mark.parametrize(
    "raw,why",
    [
        (b"\x02\x00\x00", "bad length"),
        (vault_fs.pack_acl(vault_fs.minimal_acl(0o644)) + b"\x00", "bad length"),
        (b"\x01\x00\x00\x00" + vault_fs.pack_acl(vault_fs.minimal_acl(0o644))[4:], "version"),
        (vault_fs.pack_acl(vault_fs.minimal_acl(0o644)) + b"\x40\x00\x07\x00\xff\xff\xff\xff", "unknown tag"),
        (vault_fs.pack_acl(((ACL_USER_OBJ, 6, U), (ACL_OTHER, 4, U))), "base entry"),
        (
            b"\x02\x00\x00\x00"
            + b"".join(
                vault_fs._ACL_ENTRY.pack(*e)
                for e in ((ACL_GROUP_OBJ, 6, U), (ACL_USER_OBJ, 6, U), (ACL_OTHER, 4, U))
            ),
            "out of order",
        ),
        (
            vault_fs.pack_acl(((ACL_USER_OBJ, 6, U), (ACL_GROUP_OBJ, 6, U), (ACL_GROUP, 6, NAMED), (ACL_OTHER, 4, U))),
            "no mask",
        ),
        (vault_fs.pack_acl(((ACL_USER_OBJ, 8, U), (ACL_GROUP_OBJ, 6, U), (ACL_OTHER, 4, U))), "rwx"),
    ],
)
def test_malformed_acl_values_are_refused(raw, why):
    """The parser refuses a wrong version, a bad length, an unknown tag, a
    missing base entry and out-of-order entries (D1)."""
    with pytest.raises(ValueError):
        vault_fs.parse_acl(raw)


def test_an_unparsable_leaf_acl_is_a_permissions_failure(monkeypatch, tmp_path):
    """Scenario family "Failing to apply the permission rule SHALL refuse":
    an unparsable ACL is `PermissionsNotApplied`, never a guess."""
    f = tmp_path / "f"
    f.write_bytes(b"x")
    real = os.getxattr

    def garbage(target, name, *a, **k):
        if name == ACCESS:
            return b"\x02\x00\x00\x00\x99"
        return real(target, name, *a, **k)

    monkeypatch.setattr(os, "getxattr", garbage)
    with _dirfd_cm(tmp_path) as dfd, pytest.raises(vault_fs.PermissionsNotApplied) as exc:
        vault_fs.capture_leaf(dfd, "f")
    assert "nothing was written" in str(exc.value)
    assert not isinstance(exc.value, vault_fs.VaultFSError)


def test_create_plan_from_a_default_acl_follows_the_kernel_rule():
    """K1: USER_OBJ, MASK and OTHER ANDed with rw-, named entries copied."""
    directory = vault_fs.Dir(
        dev=1, ino=2, setgid=False, gid=7, default_acl=group_rwx_default(), acl_capable=True
    )
    plan = vault_fs.create_plan(directory, staged_gid=9)
    assert plan.wanted_gid == 9
    assert plan.mode == 0o664
    assert plan.acl == (
        (ACL_USER_OBJ, 6, U),
        (ACL_GROUP_OBJ, 7, U),
        (ACL_GROUP, 7, NAMED),
        (ACL_MASK, 6, U),
        (ACL_OTHER, 4, U),
    )
    setgid = vault_fs.Dir(
        dev=1, ino=2, setgid=True, gid=7, default_acl=private_default(), acl_capable=True
    )
    private = vault_fs.create_plan(setgid, staged_gid=9)
    assert (private.wanted_gid, private.mode, private.acl) == (7, 0o600, None)


def test_create_plan_without_a_default_acl_is_the_umask_default():
    directory = vault_fs.Dir(dev=1, ino=2, setgid=False, gid=7, default_acl=None, acl_capable=True)
    plan = vault_fs.create_plan(directory, staged_gid=9)
    assert (plan.wanted_gid, plan.mode, plan.acl) == (9, vault_fs.default_file_mode(), None)


def test_edit_plan_drops_special_bits_and_keeps_the_acl():
    leaf = vault_fs.Leaf(
        dev=1, ino=2, ctime_ns=3, gid=5, mode=0o664, acl_raw=b"x",
        acl=group_rwx_default(), acl_capable=True,
    )
    assert vault_fs.edit_plan(leaf) == vault_fs.Plan(5, 0o664, group_rwx_default())


def test_translation_of_a_minimal_rule():
    """D3 on a rule without an ACL: the owning group's bits move to a named
    entry, and the mask is the OR of the group class."""
    plan = vault_fs.Plan(wanted_gid=77, mode=0o664, acl=None)
    t = vault_fs.translate(plan, staged_gid=9, acl_capable=True)
    assert t.wanted_gid == 9
    assert t.mode == 0o664
    assert t.acl == (
        (ACL_USER_OBJ, 6, U),
        (ACL_GROUP_OBJ, 0, U),
        (ACL_GROUP, 6, 77),
        (ACL_MASK, 6, U),
        (ACL_OTHER, 4, U),
    )


def test_translation_of_a_masked_rule_keeps_the_mask():
    rule = (
        (ACL_USER_OBJ, 6, U),
        (ACL_USER, 7, 1234),
        (ACL_GROUP_OBJ, 7, U),
        (ACL_GROUP, 5, NAMED),
        (ACL_MASK, 4, U),
        (ACL_OTHER, 0, U),
    )
    t = vault_fs.translate(vault_fs.Plan(77, 0o640, rule), staged_gid=9, acl_capable=True)
    assert t.acl == (
        (ACL_USER_OBJ, 6, U),
        (ACL_USER, 7, 1234),
        (ACL_GROUP_OBJ, 0, U),
        (ACL_GROUP, 7, 77),
        (ACL_GROUP, 5, NAMED),
        (ACL_MASK, 4, U),
        (ACL_OTHER, 0, U),
    )
    assert t.mode == 0o640


def test_translation_leaves_a_pre_existing_named_entry_alone():
    """No union: the kernel grants a request only through one matching entry."""
    rule = (
        (ACL_USER_OBJ, 6, U),
        (ACL_GROUP_OBJ, 4, U),
        (ACL_GROUP, 2, 77),
        (ACL_MASK, 6, U),
        (ACL_OTHER, 0, U),
    )
    t = vault_fs.translate(vault_fs.Plan(77, 0o660, rule), staged_gid=9, acl_capable=True)
    assert (ACL_GROUP, 2, 77) in t.acl
    assert (ACL_GROUP, 6, 77) not in t.acl and (ACL_GROUP, 4, 77) not in t.acl
    assert (ACL_GROUP_OBJ, 0, U) in t.acl


def test_mode_only_translation():
    plan = vault_fs.Plan(wanted_gid=77, mode=0o664, acl=None)
    t = vault_fs.translate(plan, staged_gid=9, acl_capable=False)
    assert (t.wanted_gid, t.mode, t.acl) == (9, 0o604, None)
    # A rule with an ACL never takes the mode-only form.
    with_acl = vault_fs.Plan(77, 0o664, group_rwx_default())
    assert vault_fs.translate(with_acl, staged_gid=9, acl_capable=False).acl is not None


# ════════════════════════════════════════════════════════════════════════════
# The edit rule
# ════════════════════════════════════════════════════════════════════════════


async def test_an_edited_note_in_a_group_acl_folder_keeps_group_write(notes, acl_fs):
    """Scenario: An edited note in a group-ACL folder keeps its group write access."""
    d = notes / "Shared"
    d.mkdir()
    set_acl(d, group_rwx_default())
    note = note_in(d, "ticket.md")
    before = perms(note)
    assert before[0] == 0o664
    assert (ACL_MASK, 6, U) in acl_entries(note)

    result = await tools.edit_note_impl("Shared/ticket.md", "after\n")

    assert refusal_code(result) is None, result
    assert note.read_text() == "after\n"
    assert perms(note) == before
    assert effective(note, P_UID_A, {NAMED}) == {"r", "w"}


async def test_a_private_note_stays_private_after_an_edit(notes):
    """Scenario: A private note stays private after an edit."""
    note = notes / "private.md"
    note.write_text("before\n")
    os.chmod(note, 0o600)
    strip_acl(note)

    result = await tools.edit_note_impl("private.md", "after\n")

    assert refusal_code(result) is None, result
    assert perms(note)[0] == 0o600
    assert xattr(note) is None


async def test_a_file_without_an_acl_does_not_gain_one_from_the_folder(notes, acl_fs):
    """Scenario: A file without an ACL does not gain one from the folder."""
    d = notes / "Shared"
    d.mkdir()
    set_acl(d, group_rwx_default())
    note = d / "plain.md"
    note.write_text("---\nstatus: old\n---\nbody\n")
    strip_acl(note)
    os.chmod(note, 0o640)
    assert xattr(note) is None

    result = await tools.set_frontmatter_impl("Shared/plain.md", updates={"status": "new"})

    assert refusal_code(result) is None, result
    assert "status: new" in note.read_text()
    assert xattr(note) is None
    assert perms(note)[0] == 0o640


async def test_special_bits_are_not_carried(notes):
    """Scenario: Special bits are not carried."""
    f = notes / "tool.bin"
    f.write_bytes(b"old")
    os.chmod(f, 0o4755)
    assert stat.S_IMODE(os.lstat(f).st_mode) == 0o4755

    result = await tools.write_file_impl("tool.bin", "new", encoding="text", overwrite=True)

    assert refusal_code(result) is None, result
    assert f.read_bytes() == b"new"
    assert stat.S_IMODE(os.lstat(f).st_mode) == 0o755


async def test_a_group_the_server_holds_is_kept(notes, acl_fs, other_gid):
    """Scenario: A group the server holds is kept."""
    d = notes / "Shared"
    d.mkdir()
    set_acl(d, group_rwx_default())
    note = note_in(d, "team.md")
    os.chown(note, -1, other_gid)
    before = perms(note)
    assert before[1] == other_gid != os.getegid()

    result = await tools.edit_note_impl("Shared/team.md", "after\n")

    assert refusal_code(result) is None, result
    assert perms(note) == before


async def test_issue_reproduction_table(notes, acl_fs):
    """The issue's table: an ordinary create, a server create and a server edit
    in `u::rwx,g::rwx,g:<group>:rwx,m::rwx,o::r-x` all end `mask::rw-`."""
    d = notes / "d"
    d.mkdir()
    set_acl(d, group_rwx_default())
    ordinary = control_file(d, "ordinary.md")

    assert refusal_code(await tools.create_note_impl("d/created.md", "x")) is None
    edited = note_in(d, "edited.md")
    assert refusal_code(await tools.edit_note_impl("d/edited.md", "y")) is None

    for path in (ordinary, d / "created.md", edited):
        assert (ACL_MASK, 6, U) in acl_entries(path), path
        assert effective(path, P_UID_A, {NAMED}) == {"r", "w"}, path


# ════════════════════════════════════════════════════════════════════════════
# Group translation
# ════════════════════════════════════════════════════════════════════════════


async def test_an_edit_of_a_note_whose_group_the_server_is_not_in(
    notes, acl_fs, other_gid, group_not_kept
):
    """Scenario: An edit of a note whose group the server is not in."""
    note = notes / "theirs.md"
    note.write_text("before\n")
    strip_acl(note)
    os.chmod(note, 0o664)
    os.chown(note, -1, other_gid)
    server_gid = os.getegid()
    principals = {
        "member of <g> only": (P_UID_A, {other_gid}),
        "member of the server's group only": (P_UID_B, {server_gid}),
        "in neither group": (P_UID_C, set()),
    }
    before = {k: effective(note, *p) for k, p in principals.items()}

    result = await tools.edit_note_impl("theirs.md", "after\n")

    assert refusal_code(result) is None, result
    assert group_not_kept == [other_gid]
    mode, gid, _raw = perms(note)
    assert gid == server_gid
    assert mode == 0o664
    entries = acl_entries(note)
    assert (ACL_GROUP_OBJ, 0, U) in entries
    assert (ACL_GROUP, 6, other_gid) in entries
    assert (ACL_MASK, 6, U) in entries
    after = {k: effective(note, *p) for k, p in principals.items()}
    for key in principals:
        assert after[key] <= before[key], key
    assert after["member of <g> only"] == before["member of <g> only"] == {"r", "w"}


async def test_a_private_file_stays_private_when_its_group_cannot_be_kept(
    notes, acl_fs, other_gid, group_not_kept
):
    """Scenario: A private file stays private when its group cannot be kept."""
    d = notes / "Open"
    d.mkdir()
    set_acl(
        d,
        (
            (ACL_USER_OBJ, 7, U),
            (ACL_GROUP_OBJ, 7, U),
            (ACL_GROUP, 6, NAMED),
            (ACL_MASK, 7, U),
            (ACL_OTHER, 6, U),
        ),
    )
    note = d / "mine.md"
    note.write_text("before\n")
    strip_acl(note)
    os.chmod(note, 0o600)
    os.chown(note, -1, other_gid)

    result = await tools.edit_note_impl("Open/mine.md", "after\n")

    assert refusal_code(result) is None, result
    for uid, groups in (
        (P_UID_A, {other_gid}),
        (P_UID_B, {NAMED}),
        (P_UID_C, {os.getegid()}),
        (P_UID_D, set()),
    ):
        assert effective(note, uid, groups) == frozenset(), (uid, groups)
    entries = acl_entries(note) or ()
    assert not any(tag == ACL_GROUP and ident == NAMED for tag, _p, ident in entries)
    assert stat.S_IMODE(os.lstat(note).st_mode) & 0o077 == 0


async def test_group_translation_on_a_filesystem_without_acls(
    notes, other_gid, no_xattrs, group_not_kept
):
    """Scenario: Group translation on a filesystem without ACLs."""
    note = notes / "theirs.md"
    note.write_text("before\n")
    os.chmod(note, 0o664)
    os.chown(note, -1, other_gid)

    result = await tools.edit_note_impl("theirs.md", "after\n")

    assert refusal_code(result) is None, result
    assert stat.S_IMODE(os.lstat(note).st_mode) == 0o604
    assert os.lstat(note).st_gid == os.getegid()


async def test_an_upload_into_a_setgid_folder_whose_group_the_server_is_not_in(
    acl_fs, other_gid, group_not_kept
):
    """Scenario: An upload into a setgid folder whose group the server is not in."""
    d = acl_fs / "Team"
    d.mkdir()
    os.chown(d, -1, other_gid)
    os.chmod(d, 0o2775)
    set_acl(d, ((ACL_USER_OBJ, 7, U), (ACL_GROUP_OBJ, 7, U), (ACL_OTHER, 5, U)))
    control = control_file(d)

    await upload(acl_fs, "Team/drop.bin")

    published = d / "drop.bin"
    assert group_not_kept == [other_gid]
    assert os.lstat(published).st_gid == os.getegid()
    entries = acl_entries(published)
    assert (ACL_GROUP_OBJ, 0, U) in entries
    assert (ACL_GROUP, 6, other_gid) in entries
    assert (ACL_MASK, 6, U) in entries
    member = (P_UID_A, {other_gid})
    assert effective(published, *member) == effective(control, *member) == {"r", "w"}


# ════════════════════════════════════════════════════════════════════════════
# The create rule
# ════════════════════════════════════════════════════════════════════════════


async def test_create_note_under_a_group_writable_default_acl(notes, acl_fs):
    """Scenario: Group-writable default ACL."""
    d = notes / "Shared"
    d.mkdir()
    set_acl(d, group_rwx_default())
    control = control_file(d)

    result = await tools.create_note_impl("Shared/new.md", "hello")

    assert refusal_code(result) is None, result
    assert perms(d / "new.md") == perms(control)
    assert perms(control)[0] == 0o664
    assert (ACL_MASK, 6, U) in acl_entries(control)


async def test_write_file_under_a_private_default_acl(notes, acl_fs):
    """Scenario: Private default ACL."""
    d = notes / "Private"
    d.mkdir()
    set_acl(d, private_default())
    control = control_file(d)

    result = await tools.write_file_impl("Private/data.bin", "hello", encoding="text")

    assert refusal_code(result) is None, result
    mode, gid, raw = perms(d / "data.bin")
    assert mode == 0o600
    assert gid == perms(control)[1]
    assert raw is None and perms(control)[2] is None


async def test_create_note_with_no_default_acl(notes):
    """Scenario: No default ACL."""
    result = await tools.create_note_impl("plain.md", "hello")
    assert refusal_code(result) is None, result
    mode, _gid, raw = perms(notes / "plain.md")
    assert mode == 0o666 & ~_current_umask()
    assert raw is None


def _current_umask() -> int:
    mask = os.umask(0)
    os.umask(mask)
    return mask


async def test_an_overwrite_mode_write_to_a_name_that_does_not_exist_yet(notes, acl_fs):
    """Scenario: An overwrite-mode write to a name that does not exist yet."""
    d = notes / "Shared"
    d.mkdir()
    set_acl(d, group_rwx_default())
    control = control_file(d)

    result = await tools.write_file_impl(
        "Shared/fresh.bin", "hello", encoding="text", overwrite=True
    )

    assert refusal_code(result) is None, result
    assert perms(d / "fresh.bin") == perms(control)


async def test_an_upload_into_a_foreign_setgid_folder_without_acls(
    tmp_path, other_gid, no_xattrs, group_not_kept, umask
):
    """Scenario: Upload into a foreign setgid folder on a filesystem without ACLs
    (L13: narrower than an ordinary create, the same as before this change)."""
    umask(0o002)
    d = tmp_path / "Team"
    d.mkdir()
    os.chown(d, -1, other_gid)
    os.chmod(d, 0o2775)

    await upload(tmp_path, "Team/drop.bin")

    published = d / "drop.bin"
    assert os.lstat(published).st_gid == os.getegid()
    assert stat.S_IMODE(os.lstat(published).st_mode) == 0o604


async def test_an_upload_staged_under_a_different_default_acl(acl_fs):
    """Scenarios: Upload staged under a different default ACL (file-permissions)
    and A new upload gets what an ordinary new file gets (file-transfer)."""
    set_acl(
        acl_fs,
        (
            (ACL_USER_OBJ, 7, U),
            (ACL_GROUP_OBJ, 0, U),
            (ACL_GROUP, 7, OTHER_NAMED),
            (ACL_MASK, 7, U),
            (ACL_OTHER, 0, U),
        ),
    )
    d = acl_fs / "Dest"
    d.mkdir()
    set_acl(d, group_rwx_default())
    control = control_file(d)

    await upload(acl_fs, "Dest/drop.bin")

    published = d / "drop.bin"
    assert perms(published) == perms(control)
    # `.transfer-tmp` inherited the root's default ACL; none of it leaks.
    staging_default = vault_fs.parse_acl(xattr(acl_fs / vault_fs.STAGING_DIR, DEFAULT))
    assert (ACL_GROUP, 7, OTHER_NAMED) in staging_default
    assert not any(t == ACL_GROUP and i == OTHER_NAMED for t, _p, i in acl_entries(published))


# ════════════════════════════════════════════════════════════════════════════
# Directories
# ════════════════════════════════════════════════════════════════════════════


async def test_a_new_folder_under_a_group_writable_default_acl(notes, acl_fs):
    """Scenario: A new folder under a group-writable default ACL."""
    set_acl(notes, group_rwx_default())
    control = notes / "Control"
    os.mkdir(control, 0o777)

    result = await tools.create_note_impl("New/x.md", "hello")

    assert refusal_code(result) is None, result
    new = notes / "New"
    assert stat.S_IMODE(os.lstat(new).st_mode) == stat.S_IMODE(os.lstat(control).st_mode)
    assert xattr(new) == xattr(control)
    assert xattr(new, DEFAULT) == xattr(control, DEFAULT)
    assert (ACL_MASK, 7, U) in vault_fs.parse_acl(xattr(control))


async def test_a_new_folder_with_no_default_acl_is_unchanged(notes, umask):
    """Scenario: A new folder with no default ACL is unchanged."""
    umask(0o000)
    result = await tools.create_note_impl("New/x.md", "hello")
    assert refusal_code(result) is None, result
    assert stat.S_IMODE(os.lstat(notes / "New").st_mode) == 0o755


async def test_the_staging_directory_stays_owner_only(acl_fs, monkeypatch):
    """Scenario: The staging directory stays owner-only — made with the same
    0o755 as before (never 0o777 through the root's default ACL), then 0700."""
    set_acl(acl_fs, group_rwx_default())
    modes: dict[str, int] = {}
    real_mkdir = os.mkdir

    def recording_mkdir(path, mode=0o777, *args, **kwargs):
        modes[str(path)] = mode
        return real_mkdir(path, mode, *args, **kwargs)

    monkeypatch.setattr(os, "mkdir", recording_mkdir)
    await upload(acl_fs, "New/drop.bin")
    assert modes[vault_fs.STAGING_DIR] == 0o755
    assert modes["New"] == 0o777
    assert stat.S_IMODE(os.lstat(acl_fs / vault_fs.STAGING_DIR).st_mode) == 0o700


# ════════════════════════════════════════════════════════════════════════════
# Mode-only filesystems
# ════════════════════════════════════════════════════════════════════════════


async def test_acl_calls_unsupported(notes, no_xattrs):
    """Scenario: ACL calls unsupported."""
    result = await tools.create_note_impl("new.md", "hello")
    assert refusal_code(result) is None, result
    assert stat.S_IMODE(os.lstat(notes / "new.md").st_mode) == 0o666 & ~_current_umask()

    note = notes / "private.md"
    note.write_text("before\n")
    os.chmod(note, 0o600)
    result = await tools.edit_note_impl("private.md", "after\n")
    assert refusal_code(result) is None, result
    assert stat.S_IMODE(os.lstat(note).st_mode) == 0o600


# ════════════════════════════════════════════════════════════════════════════
# The failure policy
# ════════════════════════════════════════════════════════════════════════════


def _fail_staged_setxattr(monkeypatch, code: int) -> list:
    """`fsetxattr` on a descriptor fails with `code`; path calls are real."""
    calls: list = []

    def failing(target, name, value, *args, **kwargs):
        if isinstance(target, int):
            calls.append(name)
            raise OSError(code, os.strerror(code))
        return _setxattr(target, name, value, *args, **kwargs)

    monkeypatch.setattr(os, "setxattr", failing)
    return calls


async def test_an_acl_write_failing_during_an_edit_refuses(notes, acl_fs, monkeypatch, caplog):
    """Scenario: An ACL write fails during an edit."""
    d = notes / "Shared"
    d.mkdir()
    set_acl(d, group_rwx_default())
    note = note_in(d, "ticket.md")
    before = snapshot(note)
    calls = _fail_staged_setxattr(monkeypatch, errno.EIO)

    with caplog.at_level("ERROR"):
        result = await tools.edit_note_impl("Shared/ticket.md", "after\n")

    assert calls == [ACCESS]
    assert refusal_code(result) == "io_failure", result
    assert "nothing was written" in result
    assert snapshot(note) == before
    assert [p.name for p in d.iterdir() if p.name.startswith(".tmp-")] == []
    assert any(
        "file permissions not applied" in r.getMessage() and "Shared/ticket.md" in r.getMessage()
        for r in caplog.records
    )


async def test_a_translated_acl_refused_as_unsupported_when_the_rule_has_an_acl(
    notes, acl_fs, other_gid, group_not_kept, monkeypatch
):
    """Scenario: A translated ACL refused as unsupported when the rule has an ACL
    (R2-1: a mode cannot express the denying named entry)."""
    note = notes / "guarded.md"
    note.write_text("before\n")
    os.chown(note, -1, other_gid)
    set_acl(
        note,
        (
            (ACL_USER_OBJ, 6, U),
            (ACL_GROUP_OBJ, 6, U),
            (ACL_GROUP, 0, OTHER_NAMED),
            (ACL_MASK, 6, U),
            (ACL_OTHER, 6, U),
        ),
        which=ACCESS,
    )
    before = snapshot(note)
    _fail_staged_setxattr(monkeypatch, errno.EOPNOTSUPP)

    result = await tools.edit_note_impl("guarded.md", "after\n")

    assert group_not_kept == [other_gid], "the translation path was not taken"
    assert refusal_code(result) == "io_failure", result
    assert "nothing was written" in result
    assert snapshot(note) == before


async def test_a_translated_acl_unsupported_for_a_rule_without_an_acl_takes_mode_only(
    notes, acl_fs, other_gid, group_not_kept, monkeypatch
):
    """Mode-only translation when writing the translated ACL is refused
    `EOPNOTSUPP` and the rule has no ACL (group translation requirement)."""
    note = notes / "plain.md"
    note.write_text("before\n")
    strip_acl(note)
    os.chmod(note, 0o664)
    os.chown(note, -1, other_gid)
    calls = _fail_staged_setxattr(monkeypatch, errno.EOPNOTSUPP)

    result = await tools.edit_note_impl("plain.md", "after\n")

    assert refusal_code(result) is None, result
    assert group_not_kept == [other_gid]
    assert calls == [ACCESS]
    assert note.read_text() == "after\n"
    assert stat.S_IMODE(os.lstat(note).st_mode) == 0o604
    assert os.lstat(note).st_gid == os.getegid()
    assert xattr(note) is None


@pytest.mark.parametrize("filesystem", ["acl", "mode-only"])
def test_apply_plan_translates_when_the_kernel_refuses_the_group(
    acl_fs, monkeypatch, filesystem
):
    """Group translation with the kernel's own `EPERM` (K5): a wanted gid no
    supplementary group of this process holds. Needs no supplementary group."""
    if os.geteuid() == 0:
        pytest.skip("root may set any group, so the kernel never refuses")
    foreign = max(os.getgroups() + [os.getegid()]) + 4_000_000
    if filesystem == "mode-only":
        def unsupported(*args, **kwargs):
            raise OSError(errno.EOPNOTSUPP, "Operation not supported")

        for name in ("getxattr", "setxattr", "removexattr"):
            monkeypatch.setattr(os, name, unsupported)
    staged = acl_fs / "staged.bin"
    fd = os.open(staged, os.O_CREAT | os.O_RDWR | os.O_EXCL, 0o600)
    try:
        applied = vault_fs.apply_plan(
            fd, vault_fs.Plan(wanted_gid=foreign, mode=0o664, acl=None),
            acl_capable=filesystem == "acl",
        )
    finally:
        os.close(fd)
    assert os.lstat(staged).st_gid == os.getegid()
    if filesystem == "acl":
        assert stat.S_IMODE(os.lstat(staged).st_mode) == 0o664
        entries = vault_fs.parse_acl(_getxattr(str(staged), ACCESS))
        assert entries == applied.acl
        assert (ACL_GROUP_OBJ, 0, U) in entries
        assert (ACL_GROUP, 6, foreign) in entries
        assert (ACL_MASK, 6, U) in entries
    else:
        assert stat.S_IMODE(os.lstat(staged).st_mode) == 0o604
        assert applied.acl is None


def _without_proc(monkeypatch) -> None:
    """A host with no `/proc/self/fd`: the probe says so, and a path through
    it does not resolve."""
    monkeypatch.setattr(vault_fs, "proc_fd_available", lambda: False)
    real = os.getxattr

    def no_proc(target, *args, **kwargs):
        if isinstance(target, str) and target.startswith("/proc/self/fd/"):
            raise FileNotFoundError(errno.ENOENT, "No such file or directory", target)
        return real(target, *args, **kwargs)

    monkeypatch.setattr(os, "getxattr", no_proc)


async def test_edits_without_proc_keep_mode_and_acl(notes, acl_fs, monkeypatch):
    """Implementation review round 1: the leaf's ACL is read without `/proc`
    through a readable descriptor, so overwrites of an ordinary note — guarded
    (`edit_note`) and unguarded (`write_file(overwrite=True)`) — still keep its
    mode and ACL."""
    d = notes / "Shared"
    d.mkdir()
    set_acl(d, group_rwx_default())
    note = note_in(d, "ticket.md")
    raw = note_in(d, "data.bin")
    os.chmod(raw, 0o640)
    before_note, before_raw = perms(note), perms(raw)
    assert before_note[2] is not None and before_raw[2] is not None
    _without_proc(monkeypatch)

    result = await tools.edit_note_impl("Shared/ticket.md", "after\n")
    assert refusal_code(result) is None, result
    result = await tools.write_file_impl(
        "Shared/data.bin", "new", encoding="text", overwrite=True
    )
    assert refusal_code(result) is None, result

    assert note.read_text() == "after\n" and raw.read_bytes() == b"new"
    assert perms(note) == before_note
    assert perms(raw) == before_raw


async def test_an_unreadable_file_without_proc_is_refused(notes, monkeypatch):
    """Without `/proc` an unreadable leaf's ACL cannot be read at all: the
    write is the `io_failure` refusal and nothing is written."""
    if os.geteuid() == 0:
        pytest.skip("root reads any file")
    f = notes / "locked.bin"
    f.write_bytes(b"old")
    os.chmod(f, 0o200)
    _without_proc(monkeypatch)

    result = await tools.write_file_impl("locked.bin", "new", encoding="text", overwrite=True)

    assert refusal_code(result) == "io_failure", result
    assert "nothing was written" in result
    os.chmod(f, 0o600)
    assert f.read_bytes() == b"old"
    assert [p.name for p in notes.iterdir() if p.name.startswith(".tmp-")] == []


async def test_an_acl_write_failing_during_an_upload_releases_the_claim(
    client, harness, vault, monkeypatch, acl_fs
):
    """Scenario: An ACL write fails during an upload."""
    set_acl(vault / "Attachments", group_rwx_default())
    _fail_staged_setxattr(monkeypatch, errno.EIO)

    with pytest.raises(vault_fs.PermissionsNotApplied):
        await client.put("/transfer/upload", headers=auth(harness), content=b"bytes")

    assert harness.released == 1
    assert harness.row.state == "pending"
    assert not (vault / "Attachments" / "shot.png").exists()
    assert temp_files(vault / vault_fs.STAGING_DIR) == []


# ════════════════════════════════════════════════════════════════════════════
# Capture, the single flush, and the in-call conflict
# ════════════════════════════════════════════════════════════════════════════


async def test_staged_bytes_are_private_while_being_written(notes, acl_fs, monkeypatch):
    """Scenario: Staged bytes are private while being written — a note under a
    permissive default ACL, and an upload."""
    set_acl(notes, group_rwx_default())
    seen: list[int] = []
    real_fdopen = os.fdopen

    def watching_fdopen(fd, mode="r", *args, **kwargs):
        if mode == "wb":
            seen.append(stat.S_IMODE(os.fstat(fd).st_mode))
        return real_fdopen(fd, mode, *args, **kwargs)

    monkeypatch.setattr(os, "fdopen", watching_fdopen)
    assert refusal_code(await tools.create_note_impl("n.md", "hello")) is None
    monkeypatch.setattr(os, "fdopen", real_fdopen)

    real_write_all = transfer._write_all

    def watching_write_all(fd, data):
        seen.append(stat.S_IMODE(os.fstat(fd).st_mode))
        return real_write_all(fd, data)

    monkeypatch.setattr(transfer, "_write_all", watching_write_all)
    await upload(notes, "u.bin")

    assert seen and all(mode == 0o600 for mode in seen), [oct(m) for m in seen]


def _count_file_fsyncs(monkeypatch) -> list[int]:
    inodes: list[int] = []
    real = os.fsync

    def counting(fd):
        st = os.fstat(fd)
        if stat.S_ISREG(st.st_mode):
            inodes.append(st.st_ino)
        return real(fd)

    monkeypatch.setattr(os, "fsync", counting)
    return inodes


async def test_one_flush_per_write(notes, acl_fs, monkeypatch):
    """Scenario: One flush per write — `edit_note`, `create_note`, an upload."""
    set_acl(notes, group_rwx_default())
    note = note_in(notes, "e.md")
    # Warm the once-per-root publication probe, which flushes files of its own.
    await upload(notes, "warm.bin")
    fsyncs = _count_file_fsyncs(monkeypatch)

    assert refusal_code(await tools.edit_note_impl("e.md", "after")) is None
    assert fsyncs == [os.lstat(note).st_ino]

    fsyncs.clear()
    assert refusal_code(await tools.create_note_impl("c.md", "x")) is None
    assert fsyncs == [os.lstat(notes / "c.md").st_ino]

    fsyncs.clear()
    await upload(notes, "u.bin")
    assert fsyncs == [os.lstat(notes / "u.bin").st_ino]


def _after_the_staged_flush(monkeypatch, action) -> list[bool]:
    """Run `action` right after the staged copy's `fsync`: past P1, before P2."""
    fired: list[bool] = []
    real = os.fsync

    def hooked(fd):
        result = real(fd)
        if not fired and stat.S_ISREG(os.fstat(fd).st_mode):
            fired.append(True)
            action()
        return result

    monkeypatch.setattr(os, "fsync", hooked)
    return fired


async def test_permissions_changed_during_an_edit(notes, monkeypatch):
    """Scenario: Permissions changed during an edit."""
    note = notes / "n.md"
    note.write_text("before\n")
    strip_acl(note)
    os.chmod(note, 0o664)
    fired = _after_the_staged_flush(monkeypatch, lambda: os.chmod(note, 0o600))

    result = await tools.edit_note_impl("n.md", "after\n")

    assert fired == [True]
    assert refusal_code(result) == "concurrent_write", result
    assert "File changed while editing: n.md" in result
    assert note.read_text() == "before\n"
    assert stat.S_IMODE(os.lstat(note).st_mode) == 0o600
    assert [p.name for p in notes.iterdir() if p.name.startswith(".tmp-")] == []


async def test_an_unguarded_overwrite_whose_target_changes_mode_mid_call(notes, monkeypatch):
    """Scenario: Permissions changed during an edit, without `expected=` — the
    `O_PATH` capture stands where the comparison would."""
    f = notes / "f.bin"
    f.write_bytes(b"old")
    os.chmod(f, 0o664)
    _after_the_staged_flush(monkeypatch, lambda: os.chmod(f, 0o600))

    result = await tools.write_file_impl("f.bin", "new", encoding="text", overwrite=True)

    assert refusal_code(result) == "concurrent_write", result
    assert f.read_bytes() == b"old"
    assert stat.S_IMODE(os.lstat(f).st_mode) == 0o600


async def test_a_file_that_appears_during_an_overwrite_mode_write(notes, monkeypatch):
    """Scenario: A file that appears during an overwrite-mode write."""
    target = notes / "late.bin"
    _after_the_staged_flush(monkeypatch, lambda: target.write_bytes(b"theirs"))

    result = await tools.write_file_impl("late.bin", "ours", encoding="text", overwrite=True)

    assert "File changed while editing: late.bin" in result
    assert refusal_code(result) == "concurrent_write", result
    assert target.read_bytes() == b"theirs"


async def test_an_acl_changed_in_the_middle_of_a_capture(notes, acl_fs, monkeypatch):
    """Scenario: An ACL changed in the middle of a capture."""
    note = notes / "n.md"
    note.write_text("before\n")
    before_bytes = note.read_bytes()
    real_read_acl = vault_fs._read_acl
    fired: list[bool] = []

    def racing(target, xattr_name, label):
        if xattr_name == ACCESS and not fired:
            fired.append(True)
            # Past the capture's first `fstat`, before its read. The sleep puts
            # the change on a later coarse timestamp tick than the note's
            # creation, so the ctime really moves.
            time.sleep(0.05)
            set_acl(
                note,
                (
                    (ACL_USER_OBJ, 6, U),
                    (ACL_GROUP_OBJ, 4, U),
                    (ACL_GROUP, 6, NAMED),
                    (ACL_MASK, 6, U),
                    (ACL_OTHER, 4, U),
                ),
                which=ACCESS,
            )
        return real_read_acl(target, xattr_name, label)

    monkeypatch.setattr(vault_fs, "_read_acl", racing)

    result = await tools.edit_note_impl("n.md", "after\n")

    assert fired == [True]
    assert refusal_code(result) == "concurrent_write", result
    assert note.read_bytes() == before_bytes


async def test_an_overwrite_token_whose_target_changed_mode_after_the_mint(tmp_path):
    """Scenario: An overwrite token's target whose permissions changed after the
    mint — refused at the one capture, before the publish gate is entered."""
    (tmp_path / "A").mkdir()
    target = tmp_path / "A" / "t.bin"
    target.write_bytes(b"old")
    os.chmod(target, 0o664)
    fp = fingerprint_of(target)
    time.sleep(0.05)  # a later ctime tick than the mint
    os.chmod(target, 0o600)
    entered: list[bool] = []

    @contextlib.asynccontextmanager
    async def gate():
        entered.append(True)
        yield transfer.GateHandle(ok=True, session=None, on_complete=None)

    with pytest.raises(Conflict, match="changed since the token was minted"):
        await stream_to_vault(
            Row(str(tmp_path), "A/t.bin", overwrite=True, expected_fingerprint=fp),
            chunks(b"new"),
            max_bytes=100,
            content_length=3,
            deadline=time.monotonic() + 30,
            before_publish=gate,
        )
    assert entered == []
    assert target.read_bytes() == b"old"
    assert stat.S_IMODE(os.lstat(target).st_mode) == 0o600


# ════════════════════════════════════════════════════════════════════════════
# file-transfer: overwrite tokens and the gate's folder check
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("shape", ["private", "named group"])
async def test_an_upload_over_an_existing_file_keeps_its_permissions(acl_fs, shape):
    """Scenario: An upload over an existing file keeps that file's permissions."""
    (acl_fs / "A").mkdir()
    target = acl_fs / "A" / "t.bin"
    target.write_bytes(b"old")
    if shape == "private":
        os.chmod(target, 0o600)
    else:
        set_acl(
            target,
            (
                (ACL_USER_OBJ, 6, U),
                (ACL_GROUP_OBJ, 4, U),
                (ACL_GROUP, 6, NAMED),
                (ACL_MASK, 6, U),
                (ACL_OTHER, 0, U),
            ),
            which=ACCESS,
        )
    before = perms(target)

    await upload(
        acl_fs, "A/t.bin", b"new", overwrite=True, expected_fingerprint=fingerprint_of(target)
    )

    assert target.read_bytes() == b"new"
    assert perms(target) == before


async def test_the_destination_folder_replaced_during_the_upload(
    client, harness, vault, monkeypatch
):
    """Scenario: The destination folder was replaced during the upload."""
    real_lock = transfer.lock_for_publish

    async def renaming_lock(session, token_id):
        os.rename(vault / "Attachments", vault / "Moved")
        (vault / "Attachments").mkdir()
        return await real_lock(session, token_id)

    monkeypatch.setattr(transfer, "lock_for_publish", renaming_lock)

    response = await client.put("/transfer/upload", headers=auth(harness), content=b"bytes")

    assert response.status_code == 409
    assert harness.released == 1
    assert harness.row.state == "pending"
    assert not (vault / "Attachments" / "shot.png").exists()
    assert not (vault / "Moved" / "shot.png").exists()
    assert temp_files(vault / vault_fs.STAGING_DIR) == []


# ════════════════════════════════════════════════════════════════════════════
# Implementation review round 2: the named staged copy, and a symlink at P2
# ════════════════════════════════════════════════════════════════════════════


def _alter_staged_copy(directory: Path, how: str):
    """Rewrite the `.tmp-*` staging file in `directory` as a peer could."""

    def action():
        (staged,) = [p for p in directory.iterdir() if p.name.startswith(".tmp-")]
        fd = os.open(staged, os.O_WRONLY | (os.O_APPEND if how == "longer" else 0))
        try:
            if how == "longer":
                os.write(fd, b"\nappended by a peer\n")
            else:
                size = os.fstat(fd).st_size
                os.pwrite(fd, b"X" * size, 0)
        finally:
            os.close(fd)

    return action


@pytest.mark.parametrize("how", ["same length", "longer"])
async def test_a_staged_copy_altered_before_publication(notes, acl_fs, monkeypatch, how):
    """Scenario: A staged copy altered before publication — an edit in a
    group-ACL folder, whose staged copy carries the note's group write."""
    d = notes / "Shared"
    d.mkdir()
    set_acl(d, group_rwx_default())
    note = note_in(d, "ticket.md")
    before = snapshot(note)
    fired = _after_the_staged_flush(monkeypatch, _alter_staged_copy(d, how))

    result = await tools.edit_note_impl("Shared/ticket.md", "after, ours\n")

    assert fired == [True]
    assert refusal_code(result) == "concurrent_write", result
    assert "File changed while editing: ticket.md" in result
    assert snapshot(note) == before
    assert [p.name for p in d.iterdir() if p.name.startswith(".tmp-")] == []


async def test_a_staged_copy_altered_under_an_unguarded_overwrite(notes, monkeypatch):
    """Scenario: A staged copy altered before publication — `write_file
    (overwrite=True)` without `expected_hash`, and at an absent name."""
    f = notes / "f.bin"
    f.write_bytes(b"old")
    _after_the_staged_flush(monkeypatch, _alter_staged_copy(notes, "same length"))
    result = await tools.write_file_impl("f.bin", "new", encoding="text", overwrite=True)
    assert refusal_code(result) == "concurrent_write", result
    assert f.read_bytes() == b"old"

    # A second hook wraps the spent first one.
    _after_the_staged_flush(monkeypatch, _alter_staged_copy(notes, "longer"))
    result = await tools.write_file_impl("g.bin", "new", encoding="text", overwrite=True)
    assert refusal_code(result) == "concurrent_write", result
    assert not (notes / "g.bin").exists()
    assert [p.name for p in notes.iterdir() if p.name.startswith(".tmp-")] == []


async def test_a_staged_copy_altered_under_named_no_clobber_staging(notes, monkeypatch):
    """Scenario: A staged copy altered before publication — the
    `VAULT_ALLOW_NAMED_STAGING_FALLBACK` no-clobber path (`create_note`)."""

    def no_tmpfile(_dir_fd):
        raise vault_fs.UnsupportedFilesystem("O_TMPFILE refused (test)")

    monkeypatch.setattr(vault_service, "_create_nameless_temp", no_tmpfile)
    monkeypatch.setattr(vault_service.settings, "vault_allow_named_staging_fallback", True)
    _after_the_staged_flush(monkeypatch, _alter_staged_copy(notes, "same length"))

    try:
        result = await tools.create_note_impl("new.md", "ours")
    finally:
        # Process state set on first exercise; later modules assume it clear.
        vault_fs.reset_named_staging_state()

    assert refusal_code(result) == "concurrent_write", result
    assert not (notes / "new.md").exists()
    assert [p.name for p in notes.iterdir() if p.name.startswith(".tmp-")] == []


async def test_named_staging_still_publishes_with_one_flush(notes, acl_fs, monkeypatch):
    """The verification reads the staged copy back; it never flushes again."""
    set_acl(notes, group_rwx_default())
    note = note_in(notes, "e.md")
    raw = notes / "r.bin"
    raw.write_bytes(b"old")
    os.chmod(raw, 0o200)  # no owner read: the read-back must not reopen by name
    fsyncs = _count_file_fsyncs(monkeypatch)

    assert refusal_code(await tools.edit_note_impl("e.md", "x" * 3_000_000)) is None
    assert fsyncs == [os.lstat(note).st_ino]
    assert note.read_text() == "x" * 3_000_000

    fsyncs.clear()
    result = await tools.write_file_impl("r.bin", "new", encoding="text", overwrite=True)
    assert refusal_code(result) is None, result
    assert fsyncs == [os.lstat(raw).st_ino]
    assert stat.S_IMODE(os.lstat(raw).st_mode) == 0o200


async def test_a_symlink_swapped_in_before_the_comparison_is_a_conflict(notes, monkeypatch):
    """R2-2(a): a symlink at the name when the `expected=` comparison opens it
    is the regular file having vanished — `concurrent_write`, not `io_failure`."""
    note = notes / "n.md"
    note.write_text("before\n")
    elsewhere = notes / "elsewhere.md"
    elsewhere.write_text("untouched\n")

    def swap():
        note.unlink()
        note.symlink_to(elsewhere)

    _after_the_staged_flush(monkeypatch, swap)

    result = await tools.edit_note_impl("n.md", "after\n")

    assert refusal_code(result) == "concurrent_write", result
    assert note.is_symlink()
    assert elsewhere.read_text() == "untouched\n"


def _refuse_staged_read_back(monkeypatch) -> list[int]:
    """`pread` on a `.tmp-*` staging descriptor fails `EACCES`, as on a network
    filesystem that re-checks permissions on every read (L15)."""
    refused: list[int] = []
    real = os.pread

    def pread(fd, n, offset):
        if Path(os.readlink(f"/proc/self/fd/{fd}")).name.startswith(".tmp-"):
            refused.append(fd)
            raise PermissionError(errno.EACCES, "Permission denied")
        return real(fd, n, offset)

    monkeypatch.setattr(os, "pread", pread)
    return refused


async def test_a_refused_read_back_skips_the_comparison_not_the_write(notes, monkeypatch):
    """L15: where the staged copy cannot be read back, the content comparison
    is skipped and the write proceeds, keeping the replaced file's mode."""
    f = notes / "drop.bin"
    f.write_bytes(b"old")
    os.chmod(f, 0o200)
    refused = _refuse_staged_read_back(monkeypatch)

    result = await tools.write_file_impl("drop.bin", "new", encoding="text", overwrite=True)

    assert refusal_code(result) is None, result
    assert refused, "the read-back was never attempted"
    assert stat.S_IMODE(os.lstat(f).st_mode) == 0o200
    os.chmod(f, 0o600)
    assert f.read_bytes() == b"new"


async def test_a_refused_read_back_still_checks_the_size(notes, monkeypatch):
    """L15: identity and size are still checked before the read-back, so a
    staged copy of another length is the in-call conflict."""
    f = notes / "drop.bin"
    f.write_bytes(b"old")
    _refuse_staged_read_back(monkeypatch)
    _after_the_staged_flush(monkeypatch, _alter_staged_copy(notes, "longer"))

    result = await tools.write_file_impl("drop.bin", "new", encoding="text", overwrite=True)

    assert refusal_code(result) == "concurrent_write", result
    assert f.read_bytes() == b"old"
    assert [p.name for p in notes.iterdir() if p.name.startswith(".tmp-")] == []


async def test_a_refused_read_back_of_an_owner_readable_copy_refuses_the_write(
    notes, acl_fs, monkeypatch
):
    """L15, round 4: the skip is only for a copy the applied plan made
    unreadable to its owner. Where the plan gives the owner read, a refused
    read-back (someone revoked it) refuses the write rather than publishing
    bytes nobody compared — here, a same-length alteration."""
    d = notes / "Shared"
    d.mkdir()
    set_acl(d, group_rwx_default())
    note = note_in(d, "ticket.md")
    before = snapshot(note)
    _after_the_staged_flush(monkeypatch, _alter_staged_copy(d, "same length"))
    refused = _refuse_staged_read_back(monkeypatch)

    result = await tools.edit_note_impl("Shared/ticket.md", "after, ours\n")

    assert refused, "the read-back was never attempted"
    assert refusal_code(result) == "io_failure", result
    assert snapshot(note) == before
    assert [p.name for p in d.iterdir() if p.name.startswith(".tmp-")] == []
