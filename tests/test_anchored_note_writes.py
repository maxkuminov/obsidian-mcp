"""Note mutations are anchored to a descriptor, not re-resolved per syscall (#59).

`validate_mutable_path` resolved the parent once and handed back
`resolved_parent / name`, which closed the *static* alias vector (#54). It did
not close the live one: every syscall the write then made — the temp create,
the `expected=` read, the publish — handed that **pathname** back to the
kernel, which walked it again. A process that renamed the resolved parent and
dropped a symlink at its name in between sent the write somewhere nobody
validated, and `expected=` could not see it because the decoy may hold
byte-identical bytes.

Every test here fires exactly that race, and every one of them **fails against
the pre-change code** — the write, the delete or the move lands in the decoy.

The sabotage is hung off `open_mutable` so it runs at the one moment the design
cares about: validation has produced its verdict and the tool has not yet acted
on it.
"""
import asyncio
import base64
import errno
import os
import stat
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

import src.mcp_server.tools as tools
from src.config import MAX_NOTE_BYTES
from src.mcp_server.auth import current_permission
from src.services import vault as vault_service
from src.services import vault_fs


@pytest.fixture
def vault(monkeypatch, tmp_path):
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
def sabotage(monkeypatch):
    """Run `action()` once, immediately after a target has been validated."""

    def install(action):
        original = tools.open_mutable
        fired: list[bool] = []

        def hooked(relative_path, user_id=None):
            target = original(relative_path, user_id=user_id)
            if not fired:
                fired.append(True)
                action()
            return target

        monkeypatch.setattr(tools, "open_mutable", hooked)
        return fired

    return install


def _swap_directory_for_a_link(real: Path, decoy: Path):
    """Rename `real` away and leave a symlink to `decoy` at its name.

    The #59 vector in one call: nothing about the *pathname* the tool validated
    has changed, but it now leads somewhere else entirely.
    """

    def action():
        moved = real.with_name(real.name + "-moved")
        os.rename(real, moved)
        real.symlink_to(decoy)

    return action


def _twin_directories(root: Path) -> tuple[Path, Path]:
    """`Real/note.md` and `Decoy/note.md`, byte-identical on purpose.

    Identical because that is what defeats `expected=`: an optimistic
    compare-and-swap against a decoy holding the same bytes passes.
    """
    (root / "Real").mkdir()
    (root / "Decoy").mkdir()
    (root / "Real" / "note.md").write_text("before\n", encoding="utf-8")
    (root / "Decoy" / "note.md").write_text("before\n", encoding="utf-8")
    return root / "Real", root / "Decoy"


# ── the parent directory renamed out from under a write ─────────────────────


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(
            lambda: tools.edit_note_impl("Real/note.md", "after\n"), id="edit_note"
        ),
        pytest.param(
            lambda: tools.edit_note_impl("Real/note.md", "tail", append=True),
            id="edit_note_append",
        ),
        pytest.param(
            lambda: tools.set_frontmatter_impl(
                "Real/note.md", updates={"status": "done"}
            ),
            id="set_frontmatter",
        ),
    ],
)
async def test_renaming_the_parent_mid_write_cannot_redirect_it(
    vault, sabotage, mutate
):
    real, decoy = _twin_directories(vault)
    fired = sabotage(_swap_directory_for_a_link(real, decoy))

    result = await mutate()

    assert fired, "the race never ran"
    assert "note.md" in result, result
    # The write landed in the directory validation opened, wherever its name
    # went…
    moved = vault / "Real-moved" / "note.md"
    assert moved.read_text(encoding="utf-8") != "before\n"
    # …and the decoy the name now points at was never touched.
    assert (decoy / "note.md").read_text(encoding="utf-8") == "before\n"


async def test_renaming_the_parent_mid_create_cannot_redirect_it(vault, sabotage):
    (vault / "Real").mkdir()
    (vault / "Decoy").mkdir()
    fired = sabotage(_swap_directory_for_a_link(vault / "Real", vault / "Decoy"))

    result = await tools.create_note_impl("Real/new.md", "body\n")

    assert fired
    assert "Created note" in result, result
    assert (vault / "Real-moved" / "new.md").read_text(encoding="utf-8") == "body\n"
    assert not (vault / "Decoy" / "new.md").exists()


async def test_renaming_the_parent_mid_raw_write_cannot_redirect_it(
    vault, sabotage
):
    (vault / "Real").mkdir()
    (vault / "Decoy").mkdir()
    fired = sabotage(_swap_directory_for_a_link(vault / "Real", vault / "Decoy"))

    result = await tools.write_file_impl(
        "Real/a.bin", base64.b64encode(b"bytes").decode()
    )

    assert fired
    assert "Wrote" in result, result
    assert (vault / "Real-moved" / "a.bin").read_bytes() == b"bytes"
    assert not (vault / "Decoy" / "a.bin").exists()


async def test_renaming_the_parent_mid_delete_cannot_redirect_it(vault, sabotage):
    real, decoy = _twin_directories(vault)
    fired = sabotage(_swap_directory_for_a_link(real, decoy))

    result = await tools.delete_note_impl("Real/note.md")

    assert fired
    assert "Soft-deleted" in result, result
    # The note that moved is the one that was validated…
    assert not (vault / "Real-moved" / "note.md").exists()
    # …and the decoy's note of the same name is still there.
    assert (decoy / "note.md").read_text(encoding="utf-8") == "before\n"
    trashed = list((vault / ".trash").iterdir())
    assert len(trashed) == 1
    assert trashed[0].read_text(encoding="utf-8") == "before\n"


async def test_renaming_the_parent_mid_permanent_delete_cannot_redirect_it(
    vault, sabotage
):
    real, decoy = _twin_directories(vault)
    fired = sabotage(_swap_directory_for_a_link(real, decoy))

    result = await tools.delete_note_impl("Real/note.md", permanent=True)

    assert fired
    assert "Permanently deleted" in result, result
    assert not (vault / "Real-moved" / "note.md").exists()
    assert (decoy / "note.md").read_text(encoding="utf-8") == "before\n"


async def test_renaming_the_source_parent_mid_move_cannot_redirect_it(
    vault, sabotage, monkeypatch
):
    real, decoy = _twin_directories(vault)

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def execute(self, statement):
            class Empty:
                def all(self):
                    return []

            return Empty()

        async def commit(self):
            return None

    monkeypatch.setattr(tools, "async_session", FakeSession)
    fired = sabotage(_swap_directory_for_a_link(real, decoy))

    result = await tools.move_note_impl("Real/note.md", "moved.md")

    assert fired
    assert "Moved" in result, result
    assert (vault / "moved.md").read_text(encoding="utf-8") == "before\n"
    # The decoy's copy is untouched: the move took the inode behind the
    # descriptor, not the one the pathname now reaches.
    assert (decoy / "note.md").read_text(encoding="utf-8") == "before\n"
    assert not (vault / "Real-moved" / "note.md").exists()


# ── the same hazard one level up: the vault root itself ─────────────────────


async def test_renaming_the_real_vault_root_mid_write_cannot_redirect_it(
    vault, sabotage, monkeypatch, tmp_path_factory
):
    """The vault root is a symlink and the directory *behind* it is renamed.

    Resolving the root once is not enough — the resolved pathname is still a
    pathname. Only the open root descriptor survives this.
    """
    roots = tmp_path_factory.mktemp("roots")
    real_root = roots / "real"
    decoy_root = roots / "decoy"
    real_root.mkdir()
    decoy_root.mkdir()
    (real_root / "note.md").write_text("before\n", encoding="utf-8")
    (decoy_root / "note.md").write_text("before\n", encoding="utf-8")
    link = roots / "vault"
    link.symlink_to(real_root)
    monkeypatch.setattr(vault_service.settings, "vault_path", str(link))

    fired = sabotage(_swap_directory_for_a_link(real_root, decoy_root))

    result = await tools.edit_note_impl("note.md", "after\n")

    assert fired
    assert "Updated note" in result, result
    assert (roots / "real-moved" / "note.md").read_text(
        encoding="utf-8"
    ) == "after\n"
    assert (decoy_root / "note.md").read_text(encoding="utf-8") == "before\n"


async def test_renaming_the_real_vault_root_mid_delete_cannot_redirect_the_trash(
    vault, sabotage, monkeypatch, tmp_path_factory
):
    """`.trash` is anchored to the same root descriptor as the note."""
    roots = tmp_path_factory.mktemp("roots")
    real_root = roots / "real"
    decoy_root = roots / "decoy"
    real_root.mkdir()
    decoy_root.mkdir()
    (real_root / "note.md").write_text("before\n", encoding="utf-8")
    link = roots / "vault"
    link.symlink_to(real_root)
    monkeypatch.setattr(vault_service.settings, "vault_path", str(link))

    fired = sabotage(_swap_directory_for_a_link(real_root, decoy_root))

    result = await tools.delete_note_impl("note.md")

    assert fired
    assert "Soft-deleted" in result, result
    assert (roots / "real-moved" / ".trash").is_dir()
    assert not (decoy_root / ".trash").exists()


# ── the temp file, the fsync, and the ordering around publication ───────────


def test_the_payload_is_fsynced_before_it_is_published(vault, monkeypatch):
    """Without the `fsync`, a crash just after the rename can publish a note
    whose data blocks never reached the disk — the truncation the atomic write
    exists to make impossible. The temp must also live in the destination
    directory, so the publish is a same-directory rename.

    The directory flush that follows publication is the other half (#97): the
    payload's own flush makes the *contents* durable and says nothing about the
    entry that names them, so both sides of the rename are asserted here in
    order.
    """
    (vault / "Folder").mkdir()
    order: list[str] = []
    staged: list[list[str]] = []
    real_fsync = os.fsync
    real_replace = os.replace

    def recording_fsync(fd):
        order.append("fsync-dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "fsync")
        staged.append(sorted(os.listdir(vault / "Folder")))
        return real_fsync(fd)

    def recording_replace(*args, **kwargs):
        order.append("publish")
        return real_replace(*args, **kwargs)

    monkeypatch.setattr(vault_service.os, "fsync", recording_fsync)
    monkeypatch.setattr(vault_service.os, "replace", recording_replace)

    vault_service.write_file("Folder/note.md", "body")

    # The payload, then the publish, then one directory flush per level of the
    # chain above the destination (#97): the parent, then the root. The order is
    # the assertion — every directory flush follows the publish, the payload's
    # precedes it.
    assert order[:2] == ["fsync", "publish"], order
    assert set(order[2:]) == {"fsync-dir"} and len(order) > 2, order
    # Staged next to the destination, not in `.transfer-tmp` or /tmp — a
    # cross-directory publish could not be an atomic rename.
    assert any(name.startswith(".tmp-") for name in staged[0]), staged[0]
    # …and nothing is left behind afterwards.
    assert sorted(p.name for p in (vault / "Folder").iterdir()) == ["note.md"]


def test_a_crash_between_staging_and_publication_leaves_the_note_intact(
    vault, monkeypatch
):
    note = vault / "note.md"
    note.write_text("before\n", encoding="utf-8")

    def boom(*args, **kwargs):
        raise KeyboardInterrupt("killed mid-write")

    monkeypatch.setattr(vault_service.os, "replace", boom)

    with pytest.raises(KeyboardInterrupt):
        vault_service.write_file("note.md", "after\n")

    assert note.read_text(encoding="utf-8") == "before\n"
    assert [p.name for p in vault.iterdir()] == ["note.md"]


# ── no silent fallback when the filesystem cannot do it ─────────────────────


async def test_a_no_clobber_write_refuses_when_hard_links_are_unsupported(
    vault, monkeypatch
):
    """`create_note` must never degrade to a replacing rename."""
    (vault / "note.md").write_text("existing\n", encoding="utf-8")

    def refuse(*args, **kwargs):
        raise OSError(errno.EPERM, "operation not permitted")

    monkeypatch.setattr(vault_service.os, "link", refuse)

    result = await tools.create_note_impl("note.md", "ours\n")

    assert "hard link" in result.lower(), result
    assert (vault / "note.md").read_text(encoding="utf-8") == "existing\n"


async def test_soft_delete_refuses_when_rename_noreplace_is_unsupported(
    vault, monkeypatch
):
    (vault / "note.md").write_text("keep me\n", encoding="utf-8")
    monkeypatch.setattr(
        vault_fs, "_renameat2_raw", lambda *a, **k: errno.ENOSYS
    )

    result = await tools.delete_note_impl("note.md")

    assert "delete_file" in result or "non-replacing" in result, result
    assert (vault / "note.md").read_text(encoding="utf-8") == "keep me\n"


# ── descriptors are not leaked ──────────────────────────────────────────────


def _open_fds() -> int:
    return len(os.listdir("/proc/self/fd"))


needs_proc = pytest.mark.skipif(
    not os.path.exists("/proc/self/fd"), reason="needs /proc"
)


@needs_proc
@pytest.mark.parametrize(
    "call",
    [
        pytest.param(
            lambda i: tools.create_note_impl(f"n{i}.md", "body\n"), id="create_note"
        ),
        pytest.param(
            lambda i: tools.edit_note_impl("kept.md", f"body {i}\n"), id="edit_note"
        ),
        pytest.param(
            lambda i: tools.set_frontmatter_impl("kept.md", updates={"n": i}),
            id="set_frontmatter",
        ),
        pytest.param(
            lambda i: tools.delete_note_impl(f"n{i}.md"), id="delete_note"
        ),
        pytest.param(
            lambda i: tools.write_file_impl(
                f"n{i}.bin", base64.b64encode(b"x").decode()
            ),
            id="write_file",
        ),
        pytest.param(
            lambda i: tools.create_note_impl("alias.md", "clobber"), id="refused"
        ),
    ],
)
async def test_the_tools_do_not_leak_descriptors(vault, call):
    """Anchoring pins fds; every exit path — success and refusal — must close.

    A leak here is invisible until the server has been up for a week and then
    every tool starts failing with EMFILE at once.
    """
    (vault / "kept.md").write_text("body\n", encoding="utf-8")
    (vault / "important.md").write_text("real\n", encoding="utf-8")
    (vault / "alias.md").symlink_to(vault / "important.md")
    for i in range(3):
        (vault / f"n{i}.md").write_text("body\n", encoding="utf-8")

    await call(0)  # warm any one-off caches (the trash probe, the umask read)
    before = _open_fds()
    for i in range(1, 3):
        await call(i)
    assert _open_fds() == before


def _fake_move_session(monkeypatch, rows, backlinks):
    """`async_session` stand-in: the vault index, then the backlink sources."""

    class FakeSession:
        calls = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def execute(self, statement):
            FakeSession.calls += 1
            payload = rows if FakeSession.calls == 1 else backlinks

            class Result:
                def all(self_inner):
                    return payload if FakeSession.calls <= 2 else []

            return Result()

        async def commit(self):
            return None

    monkeypatch.setattr(tools, "async_session", FakeSession)


def _peak_fd_recorder(monkeypatch):
    """Sample the fd count at every target open and every rewrite write.

    A post-call check only proves nothing *leaked*; it says nothing about how
    many descriptors were pinned at once, which is what exhausts the table
    mid-move. These two hooks straddle the window: one fires as each source is
    opened in the preflight, the other as each planned rewrite is published.
    """
    peak = [0]
    real_open = tools.open_mutable
    real_write = tools.write_file_at

    def sample():
        peak[0] = max(peak[0], _open_fds())

    def open_hook(*args, **kwargs):
        target = real_open(*args, **kwargs)
        sample()
        return target

    def write_hook(*args, **kwargs):
        sample()
        return real_write(*args, **kwargs)

    monkeypatch.setattr(tools, "open_mutable", open_hook)
    monkeypatch.setattr(tools, "write_file_at", write_hook)
    return peak


@needs_proc
async def test_a_move_releases_sources_that_need_no_rewrite(vault, monkeypatch):
    """The `continue` paths must not pin a descriptor.

    A hub note's backlink set is whatever the graph says; most of those notes
    may hold no link that this move actually rewrites. Keeping a target open
    for each of them pinned one fd per *source* — hundreds — for the whole
    call, when only the *planned* rewrites need one.
    """
    (vault / "target.md").write_text("body\n", encoding="utf-8")
    linking = [f"src{i}.md" for i in range(5)]
    inert = [f"inert{i}.md" for i in range(120)]
    for name in linking:
        (vault / name).write_text("See [[target]]\n", encoding="utf-8")
    for name in inert:
        # Listed as a backlink source by the (stale) graph, but its body holds
        # nothing this move rewrites — the `n == 0` continue.
        (vault / name).write_text("no links here\n", encoding="utf-8")

    every = linking + inert
    rows = [
        _Row(file_path="target.md", id=1),
        *[_Row(file_path=name, id=i + 2) for i, name in enumerate(every)],
    ]
    _fake_move_session(
        monkeypatch, rows, [_Row(file_path=name) for name in every]
    )
    peak = _peak_fd_recorder(monkeypatch)

    before = _open_fds()
    result = await tools.move_note_impl(
        "target.md", "moved.md", rewrite_links=True
    )

    assert "Moved" in result, result
    assert "rewrote 5 link(s)" in result, result
    assert _open_fds() == before
    # Bounded by the notes actually being rewritten, not by the 125 sources
    # considered. The slack covers the two move endpoints and their roots.
    # One per *planned* rewrite (5), never one per source (125), plus the two
    # move endpoints and the phase's single shared root.
    assert peak[0] - before <= len(linking) + 7, peak[0] - before
    assert peak[0] - before < len(inert), "a descriptor was pinned per source"


async def test_a_move_aborts_when_the_plan_exceeds_the_descriptor_budget(
    vault, monkeypatch
):
    """A hub note's move must not exhaust the *process* descriptor table.

    Anchoring costs one open parent fd per planned rewrite, held from the
    preflight read to the post-move write. EMFILE half way through the loop
    would move the note and silently drop the remaining rewrites — and take
    every concurrent request down with it — so the budget is checked in the
    preflight, where refusing costs nothing.
    """
    (vault / "target.md").write_text("body\n", encoding="utf-8")
    sources = [f"src{i}.md" for i in range(4)]
    for name in sources:
        (vault / name).write_text("See [[target]]\n", encoding="utf-8")

    rows = [
        _Row(file_path="target.md", id=1),
        *[_Row(file_path=name, id=i + 2) for i, name in enumerate(sources)],
    ]
    _fake_move_session(
        monkeypatch, rows, [_Row(file_path=name) for name in sources]
    )
    monkeypatch.setattr(tools, "max_move_rewrite_sources", lambda: 2)

    result = await tools.move_note_impl(
        "target.md", "moved.md", rewrite_links=True
    )

    assert "Move aborted" in result, result
    assert "file descriptors" in result, result
    # Nothing moved, nothing rewritten.
    assert (vault / "target.md").read_text(encoding="utf-8") == "body\n"
    assert not (vault / "moved.md").exists()
    for name in sources:
        assert (vault / name).read_text(encoding="utf-8") == "See [[target]]\n"


def test_the_descriptor_budget_tracks_the_process_limit(monkeypatch):
    """Derived from `RLIMIT_NOFILE`, not pinned: a container with a million
    descriptors must not be held to a laptop's 1024."""
    import resource

    from src import config

    monkeypatch.setattr(
        config.resource, "getrlimit", lambda _: (1024, 1024)
    )
    # The reserve, plus the one vault-root descriptor the rewrite phase shares
    # across every planned rewrite. Charged in the budget rather than absorbed
    # into the reserve, so the arithmetic stays visible.
    assert config.max_move_rewrite_sources() == (
        1024 - config.MOVE_REWRITE_FD_RESERVE - config.MOVE_REWRITE_SHARED_ROOT_FDS
    )
    assert config.MOVE_REWRITE_SHARED_ROOT_FDS == 1, (
        "one shared root for the phase, not one per rewrite — a per-target root "
        "would halve this cap to hold N duplicates of one directory"
    )

    # A limit so small the reserve swallows it refuses outright. There is no
    # floor: a floor would guarantee the exhaustion the cap exists to prevent
    # on exactly the processes that cannot afford it.
    monkeypatch.setattr(config.resource, "getrlimit", lambda _: (128, 128))
    assert config.max_move_rewrite_sources() == 0

    monkeypatch.setattr(
        config.resource,
        "getrlimit",
        lambda _: (resource.RLIM_INFINITY, resource.RLIM_INFINITY),
    )
    assert config.max_move_rewrite_sources() > 10**6


@needs_proc
async def test_a_move_with_many_rewrite_sources_does_not_leak(vault, monkeypatch):
    """Each planned rewrite holds exactly one descriptor, released at its write.

    One fd per planned rewrite is inherent: the preflight read and the
    post-move write must go through the same descriptor, and the preflight has
    to finish before the move commits so an over-cap rewrite can abort it. What
    must not happen is two fds per source, or descriptors surviving the loop.
    """
    (vault / "target.md").write_text("body\n", encoding="utf-8")
    sources = [f"src{i}.md" for i in range(20)]
    for name in sources:
        (vault / name).write_text("See [[target]]\n", encoding="utf-8")

    rows = [
        _Row(file_path="target.md", id=1),
        *[_Row(file_path=name, id=i + 2) for i, name in enumerate(sources)],
    ]
    _fake_move_session(
        monkeypatch, rows, [_Row(file_path=name) for name in sources]
    )
    peak = _peak_fd_recorder(monkeypatch)

    before = _open_fds()
    result = await tools.move_note_impl(
        "target.md", "moved.md", rewrite_links=True
    )
    assert "Moved" in result, result
    assert "rewrote 20 link(s)" in result, result
    assert _open_fds() == before
    # One per planned rewrite, not two — each borrows the phase's single shared
    # root (`share_root`) instead of keeping its own — plus the endpoints and
    # that one shared descriptor.
    assert peak[0] - before <= len(sources) + 7, peak[0] - before
    assert peak[0] - before < 2 * len(sources), "two descriptors per source"


class _Row:
    def __init__(self, **fields):
        self.__dict__.update(fields)


# ── unchanged guarantees ────────────────────────────────────────────────────


async def test_a_missing_note_still_does_not_create_the_trash(vault):
    result = await tools.delete_note_impl("gone.md")

    assert "not found" in result.lower(), result
    assert not (vault / ".trash").exists()


async def test_soft_delete_keeps_a_symlinked_folder_working(vault):
    (vault / "Real").mkdir()
    (vault / "Real" / "note.md").write_text("body\n", encoding="utf-8")
    (vault / "Shared").symlink_to(vault / "Real")

    result = await tools.delete_note_impl("Shared/note.md")

    assert "Soft-deleted" in result, result
    assert "Real/note.md" in result or "Shared/note.md" in result
    trashed = list((vault / ".trash").iterdir())
    assert len(trashed) == 1
    assert trashed[0].read_text(encoding="utf-8") == "body\n"


async def test_a_refused_write_creates_no_directories(vault):
    """Parent creation is deferred to the write, so a refusal leaves no tree.

    Creating the parent during validation would have `create_note` litter the
    vault with empty folders for every over-cap body an agent tries.
    """
    result = await tools.create_note_impl(
        "New/Folder/note.md", "x" * (MAX_NOTE_BYTES + 1)
    )

    assert "max" in result.lower(), result
    assert not (vault / "New").exists()


async def test_a_source_replaced_mid_move_is_relocated_intact(vault, monkeypatch):
    """`RENAME_NOREPLACE` moves whichever inode is at the source when it runs.

    Two properties at once. The kernel one: `link` + `unlink` — the shape this
    replaced — would have published the inode it linked and then unlinked the
    *replacement*, destroying it. And the tool one: the inode is pinned before
    the rename, so a move that did not carry the note is reported as such
    rather than as a successful move whose index entry points at a stranger.
    """
    (vault / "source.md").write_text("original\n", encoding="utf-8")
    executed = _no_db(monkeypatch)

    real = vault_fs.rename_noreplace
    swapped: list[bool] = []

    def replace_source_first(src_dir_fd, src_name, dst_dir_fd, dst_name):
        if not swapped:
            swapped.append(True)
            replacement = vault / "replacement"
            replacement.write_text("replacement\n", encoding="utf-8")
            os.replace(replacement, vault / "source.md")
        return real(src_dir_fd, src_name, dst_dir_fd, dst_name)

    monkeypatch.setattr(vault_fs, "rename_noreplace", replace_source_first)

    result = await tools.move_note_impl("source.md", "destination.md")

    assert swapped
    # The kernel half still holds: the replacement was relocated intact —
    # `link` + `unlink` would have published the inode it linked and then
    # unlinked the replacement, destroying it.
    assert (vault / "destination.md").read_text(encoding="utf-8") == "replacement\n"
    assert not (vault / "source.md").exists()
    # But the tool does not call that a move: the inode pinned before the
    # rename is not the one that arrived, so it refuses to say "Moved" and
    # refuses to key the index to a file that is not the note.
    assert "Move published but" in result, result
    assert "not the file that was moved" in result, result
    # And nothing was moved back — relocating a third party's file on the
    # strength of a name is the mistake this avoids.
    assert executed == []


@pytest.mark.parametrize(
    "call, expected",
    [
        pytest.param(
            lambda: tools.edit_note_impl("gone.md", "x"),
            "Use create_note",
            id="edit_note",
        ),
        pytest.param(
            lambda: tools.set_frontmatter_impl("gone.md", updates={"a": 1}),
            "Note not found",
            id="set_frontmatter",
        ),
        pytest.param(
            lambda: tools.delete_note_impl("gone.md"),
            "Note not found",
            id="delete_note",
        ),
        pytest.param(
            lambda: tools.move_note_impl("gone.md", "there.md"),
            "Source note not found",
            id="move_note",
        ),
    ],
)
async def test_a_genuinely_missing_note_still_reports_missing(vault, call, expected):
    """The leaf re-check distinguishes absent from symlinked; absent keeps its
    original, actionable message."""
    result = await call()

    assert expected in result, result


async def test_a_second_delete_of_the_same_name_gets_a_distinct_trash_entry(vault):
    for _ in range(2):
        (vault / "note.md").write_text("body\n", encoding="utf-8")
        assert "Soft-deleted" in await tools.delete_note_impl("note.md")

    assert len(list((vault / ".trash").iterdir())) == 2


# ── the root is pinned before it is resolved (Codex BLOCKER 1) ──────────────


async def test_swapping_the_root_between_resolution_and_the_open_is_refused(
    vault, monkeypatch, tmp_path_factory
):
    """Containment was computed against a *pathname*, then that pathname was
    reopened. A root renamed away in between anchored a directory the guard
    never checked. The root descriptor is opened first now, and the resolution
    that follows has to agree with it.
    """
    roots = tmp_path_factory.mktemp("roots")
    real_root = roots / "real"
    decoy_root = roots / "decoy"
    real_root.mkdir()
    decoy_root.mkdir()
    (real_root / "note.md").write_text("real\n", encoding="utf-8")
    (decoy_root / "note.md").write_text("decoy\n", encoding="utf-8")
    link = roots / "vault"
    link.symlink_to(real_root)
    monkeypatch.setattr(vault_service.settings, "vault_path", str(link))

    # Fire once, in the window between pinning the root and resolving it.
    real_resolve = vault_service.Path.resolve
    fired: list[bool] = []

    def resolve_then_swap(self, *args, **kwargs):
        result = real_resolve(self, *args, **kwargs)
        if not fired and str(self) == str(link):
            fired.append(True)
            os.rename(real_root, roots / "real-moved")
            os.symlink(decoy_root, real_root)
        return result

    monkeypatch.setattr(vault_service.Path, "resolve", resolve_then_swap)

    result = await tools.edit_note_impl("note.md", "after\n")

    assert fired, "the race never ran"
    assert "changed while the path was being validated" in result, result
    # Neither copy was touched.
    assert (roots / "real-moved" / "note.md").read_text(encoding="utf-8") == "real\n"
    assert (decoy_root / "note.md").read_text(encoding="utf-8") == "decoy\n"


# ── the staged inode, not the staged name (Codex BLOCKER 2 / round-2 #1) ────


def _staging_names_seen(monkeypatch) -> list[str]:
    """Record what the staging descriptor is called, at the moment of `fsync`.

    `readlink("/proc/self/fd/N")` is the only way to ask "does this descriptor
    have a directory entry?". For an `O_TMPFILE` inode the kernel answers with
    a synthetic `#<inode> (deleted)` path — there is no name, which is exactly
    the property the no-clobber path is supposed to have.
    """
    seen: list[str] = []
    real_fsync = os.fsync

    def record(fd):
        seen.append(os.readlink(f"/proc/self/fd/{fd}"))
        return real_fsync(fd)

    monkeypatch.setattr(vault_service.os, "fsync", record)
    return seen


def _replace_the_staging_file(monkeypatch, decoy: bytes):
    """Swap a different inode under the staging name after it is fsynced.

    Only reachable on the **overwrite** path: `renameat` has no by-descriptor
    form, so its source must have a name, and a peer with write access to the
    destination directory could otherwise have *its* bytes published as the
    note.
    """
    real_fsync = os.fsync
    fired: list[str] = []

    def fsync_then_swap(fd):
        result = real_fsync(fd)
        if fired:
            return result
        tmp = Path(os.readlink(f"/proc/self/fd/{fd}"))
        if tmp.name.endswith("(deleted)") or not tmp.exists():
            return result  # nameless staging: nothing to swap
        fired.append(tmp.name)
        os.unlink(tmp)
        tmp.write_bytes(decoy)
        return result

    monkeypatch.setattr(vault_service.os, "fsync", fsync_then_swap)
    return fired


@pytest.mark.parametrize(
    "write",
    [
        pytest.param(
            lambda: tools.create_note_impl("note.md", "ours\n"), id="create_note"
        ),
        pytest.param(
            lambda: tools.write_file_impl(
                "blob.bin", base64.b64encode(b"ours").decode()
            ),
            id="write_file",
        ),
    ],
)
async def test_a_no_clobber_write_never_exposes_a_staging_name(vault, monkeypatch, write):
    """There is nothing to substitute, because there is nothing to name.

    A named staging file has to be unlinked afterwards, and an unlink is by
    name — so it can only be guarded by an identity check followed by the
    removal, which is check-then-act and could delete a substitute planted in
    between. `O_TMPFILE` removes the step rather than guarding it.
    """
    seen = _staging_names_seen(monkeypatch)

    result = await write()

    assert seen, "nothing was staged"
    # The kernel's answer for an inode with no directory entry.
    assert seen[0].endswith("(deleted)"), seen
    assert "Created note" in result or "Wrote" in result, result
    # Published, and no litter left behind under any name.
    published = [p.name for p in vault.iterdir()]
    assert published in (["note.md"], ["blob.bin"]), published


def test_a_nameless_staging_inode_is_still_published_no_clobber(vault):
    """The `O_TMPFILE` inode is published by descriptor, and refuses to clobber."""
    vault_service.write_bytes("blob.bin", b"first", overwrite=False)
    assert (vault / "blob.bin").read_bytes() == b"first"

    with pytest.raises(FileExistsError):
        vault_service.write_bytes("blob.bin", b"second", overwrite=False)
    assert (vault / "blob.bin").read_bytes() == b"first"
    assert [p.name for p in vault.iterdir()] == ["blob.bin"]


def test_a_no_clobber_write_refuses_without_o_tmpfile(vault, monkeypatch):
    """No `O_TMPFILE` means no nameless staging — refuse rather than expose a
    staging name whose cleanup cannot be made race-free."""
    real_open = os.open

    def refuse_tmpfile(path, flags, *args, **kwargs):
        if flags & getattr(os, "O_TMPFILE", 0) == getattr(os, "O_TMPFILE", 0):
            raise OSError(errno.EOPNOTSUPP, "operation not supported")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(vault_service.os, "open", refuse_tmpfile)

    with pytest.raises(vault_fs.UnsupportedFilesystem, match="O_TMPFILE"):
        vault_service.write_bytes("blob.bin", b"ours", overwrite=False)

    assert list(vault.iterdir()) == []


def _refuse_o_tmpfile(monkeypatch):
    real_open = os.open

    def refuse(path, flags, *args, **kwargs):
        if flags & getattr(os, "O_TMPFILE", 0) == getattr(os, "O_TMPFILE", 0):
            raise OSError(errno.EOPNOTSUPP, "operation not supported")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(vault_service.os, "open", refuse)


def test_the_named_staging_fallback_is_off_by_default(vault, monkeypatch):
    """`VAULT_ALLOW_NAMED_STAGING_FALLBACK` defaults false: an O_TMPFILE
    refusal still refuses, even with the fallback machinery present."""
    _refuse_o_tmpfile(monkeypatch)
    assert vault_service.settings.vault_allow_named_staging_fallback is False

    with pytest.raises(vault_fs.UnsupportedFilesystem, match="O_TMPFILE"):
        vault_service.write_bytes("blob.bin", b"ours", overwrite=False)

    assert list(vault.iterdir()) == []
    assert vault_fs.named_staging_fallback_active() is False


def test_the_named_staging_fallback_still_refuses_to_clobber(
    vault, monkeypatch, caplog
):
    """Opting into the fallback survives an O_TMPFILE refusal, still refuses
    to clobber an existing file, leaves no staging name behind either way,
    and warns loudly exactly once per process — not once per write.

    The once-per-process state (`named_staging_fallback_active`) is shared
    with the transfer path via `vault_fs` (D27), not kept per-module — see
    `vault._link_staged_name` / `vault._discard_temp`, which delegate to
    `vault_fs` for the same reason (#105).
    """
    _refuse_o_tmpfile(monkeypatch)
    monkeypatch.setattr(
        vault_service.settings, "vault_allow_named_staging_fallback", True
    )
    vault_fs.reset_named_staging_state()
    assert vault_fs.named_staging_fallback_active() is False

    with caplog.at_level("WARNING"):
        vault_service.write_bytes("blob.bin", b"first", overwrite=False)

    assert (vault / "blob.bin").read_bytes() == b"first"
    assert [p.name for p in vault.iterdir()] == ["blob.bin"], "staging litter left"
    assert vault_fs.named_staging_fallback_active() is True
    assert (
        sum("VAULT_ALLOW_NAMED_STAGING_FALLBACK" in r.message for r in caplog.records)
        == 1
    )

    caplog.clear()
    try:
        with pytest.raises(FileExistsError):
            vault_service.write_bytes("blob.bin", b"second", overwrite=False)

        assert (vault / "blob.bin").read_bytes() == b"first"
        assert [p.name for p in vault.iterdir()] == ["blob.bin"], "staging litter left"
        # Already active — no second warning for the second write.
        assert not any(
            "VAULT_ALLOW_NAMED_STAGING_FALLBACK" in r.message for r in caplog.records
        )
    finally:
        vault_fs.reset_named_staging_state()


def test_the_fallback_warning_names_where_each_path_stages(vault, monkeypatch, caplog):
    """The one warning is accurate for whichever path fired it.

    It used to say writes were staging "under a name in `.transfer-tmp`",
    which is false for a note write: the note path stages **beside the
    destination**, in an ordinary vault directory, which is the *wider* of the
    two windows (D27). One warning per process is the declared semantic, so
    the fix is a warning that names both locations and says which path
    exercised it — not a second warning when the other path follows.
    """
    _refuse_o_tmpfile(monkeypatch)
    monkeypatch.setattr(
        vault_service.settings, "vault_allow_named_staging_fallback", True
    )
    vault_fs.reset_named_staging_state()
    try:
        with caplog.at_level("WARNING"):
            vault_service.write_bytes("blob.bin", b"ours", overwrite=False)

        warnings = [
            r.message
            for r in caplog.records
            if "VAULT_ALLOW_NAMED_STAGING_FALLBACK" in r.message
        ]
        assert len(warnings) == 1
        message = warnings[0]
        assert vault_fs.NAMED_STAGING_NOTE_PATH in message, message
        assert "beside the destination" in message, message
        # And it still names the transfer path's own location, because the
        # other path may take the fallback later with no second warning.
        assert vault_fs.STAGING_DIR in message, message
    finally:
        vault_fs.reset_named_staging_state()


def test_a_failed_staging_creation_neither_warns_nor_flips_health(
    vault, monkeypatch, caplog
):
    """The signal is "a call actually staged under a name".

    The exercised flag was set *before* `_create_temp_exclusively`, so a
    creation that failed every attempt still spent the once-per-process
    warning and made `/health` report a fallback this process never took
    (#104 review). Every attempt fails here — the candidate name is fixed and
    a symlink already sits at it, which `O_EXCL|O_NOFOLLOW` refuses.
    """
    _refuse_o_tmpfile(monkeypatch)
    monkeypatch.setattr(
        vault_service.settings, "vault_allow_named_staging_fallback", True
    )
    monkeypatch.setattr(vault_service, "_temp_candidate", lambda name: ".tmp-fixed")
    (vault / ".tmp-fixed").symlink_to(vault / "decoy.bin")
    vault_fs.reset_named_staging_state()
    try:
        with caplog.at_level("WARNING"):
            with pytest.raises(RuntimeError, match="temporary file"):
                vault_service.write_bytes("blob.bin", b"ours", overwrite=False)

        assert vault_fs.named_staging_fallback_active() is False
        assert not any(
            "VAULT_ALLOW_NAMED_STAGING_FALLBACK" in r.message for r in caplog.records
        )
        assert not (vault / "blob.bin").exists()
        assert not (vault / "decoy.bin").exists(), "wrote through the planted link"
    finally:
        vault_fs.reset_named_staging_state()


def test_a_cleanup_without_an_identity_unlinks_nothing(vault, monkeypatch, caplog):
    """The BLOCKER: a no-clobber write must not destroy somebody else's file.

    On the fallback path the staging name exists for the whole write. If the
    `fstat` that establishes *what we staged* fails afterwards, the cleanup
    has no identity to compare against — and unlinking the name on that basis
    destroys whatever has since taken it. Here another writer renames its
    sole-link file over the staging name inside exactly that window: the file
    must survive a write that published nothing, and the litter must be
    reported rather than removed.
    """
    _refuse_o_tmpfile(monkeypatch)
    monkeypatch.setattr(
        vault_service.settings, "vault_allow_named_staging_fallback", True
    )
    monkeypatch.setattr(vault_service, "_temp_candidate", lambda name: ".tmp-fixed")
    (vault / "victim.bin").write_bytes(b"somebody else's bytes")
    vault_fs.reset_named_staging_state()

    real_fstat = os.fstat

    def boom(fd):
        info = real_fstat(fd)
        if stat.S_ISDIR(info.st_mode):
            return info
        # The concurrent replacement lands in the window the failed `fstat`
        # opens: the name now refers to the victim's inode, not ours.
        os.rename(vault / "victim.bin", vault / ".tmp-fixed")
        raise OSError(errno.EIO, "I/O error")

    monkeypatch.setattr(vault_service.os, "fstat", boom)
    try:
        with caplog.at_level("WARNING"):
            with pytest.raises(OSError):
                vault_service.write_bytes("blob.bin", b"ours", overwrite=False)
    finally:
        monkeypatch.setattr(vault_service.os, "fstat", real_fstat)
        vault_fs.reset_named_staging_state()

    assert (vault / ".tmp-fixed").read_bytes() == b"somebody else's bytes"
    assert not (vault / "blob.bin").exists(), "published despite the failure"
    assert any(
        ".tmp-fixed" in r.message and "left in place" in r.message
        for r in caplog.records
    ), [r.message for r in caplog.records]


def test_a_staging_name_that_vanishes_before_publication_is_reported(
    vault, monkeypatch, caplog
):
    """An absent staging name is ordinary only *after* a consuming publish.

    Returning quietly regardless of `published` hid the other case entirely:
    a staging name that disappeared while the write was still in flight is a
    substitution's first half, and the cleanup is the only thing that sees it.
    """
    _refuse_o_tmpfile(monkeypatch)
    monkeypatch.setattr(
        vault_service.settings, "vault_allow_named_staging_fallback", True
    )
    monkeypatch.setattr(vault_service, "_temp_candidate", lambda name: ".tmp-fixed")
    vault_fs.reset_named_staging_state()
    real_require = vault_service._require_staged_name

    def vanish(dir_fd, tmp, staged):
        os.unlink(tmp, dir_fd=dir_fd)
        return real_require(dir_fd, tmp, staged)

    monkeypatch.setattr(vault_service, "_require_staged_name", vanish)
    try:
        with caplog.at_level("WARNING"):
            with pytest.raises(vault_fs.Conflict):
                vault_service.write_bytes("blob.bin", b"ours", overwrite=False)
    finally:
        vault_fs.reset_named_staging_state()

    assert not (vault / "blob.bin").exists()
    assert any(
        ".tmp-fixed" in r.message and "disappeared before" in r.message
        for r in caplog.records
    ), [r.message for r in caplog.records]


def test_the_shared_discard_is_quiet_only_for_a_consuming_publish(tmp_path, caplog):
    """The primitive itself, in both directions and for both write paths.

    `discard_staged_name` is what the note path and the transfer path both
    clean up through (D27), and the transfer path reaches it with `staged=None`
    on the same shape — an `fstat` that failed after `create_temp` — so the
    refusal belongs in the primitive rather than in one caller.
    """
    dir_fd = os.open(str(tmp_path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        # 1. Consumed by a publish: absent, published, quiet.
        with caplog.at_level("WARNING"):
            assert (
                vault_fs.discard_staged_name(
                    dir_fd, ".tmp-gone", os.stat(tmp_path), published=True
                )
                is True
            )
        assert not caplog.records, [r.message for r in caplog.records]

        # 2. Absent and *not* published: warned, and reported as a failure.
        caplog.clear()
        with caplog.at_level("WARNING"):
            assert (
                vault_fs.discard_staged_name(
                    dir_fd, ".tmp-gone", os.stat(tmp_path), published=False
                )
                is False
            )
        assert any("disappeared before" in r.message for r in caplog.records)

        # 3. No identity at all: nothing is unlinked, on either path.
        (tmp_path / ".tmp-theirs").write_bytes(b"not ours")
        caplog.clear()
        with caplog.at_level("WARNING"):
            assert (
                vault_fs.discard_staged_name(
                    dir_fd, ".tmp-theirs", None, published=False
                )
                is False
            )
            # The transfer path's abandon path is the same call.
            assert vault_fs.discard_temp(dir_fd, ".tmp-theirs", None) is False
        assert (tmp_path / ".tmp-theirs").read_bytes() == b"not ours"
        assert len(
            [r for r in caplog.records if "left in place" in r.message]
        ) == 2

        # 4. Ours, present, unpublished: removed, as it always was.
        staged_path = tmp_path / ".tmp-ours"
        staged_path.write_bytes(b"ours")
        assert (
            vault_fs.discard_staged_name(
                dir_fd, ".tmp-ours", os.stat(staged_path), published=False
            )
            is True
        )
        assert not staged_path.exists()
    finally:
        os.close(dir_fd)


async def test_a_swapped_staging_file_is_refused_by_an_overwrite(
    vault, monkeypatch
):
    """`renameat` is inherently by name, so this one is a check — but it must
    refuse rather than publish the substitute."""
    note = vault / "note.md"
    note.write_text("before\n", encoding="utf-8")
    fired = _replace_the_staging_file(monkeypatch, b"DECOY")

    result = await tools.edit_note_impl("note.md", "after\n")

    assert fired, "the race never ran"
    assert "staged copy" in result, result
    assert note.read_text(encoding="utf-8") == "before\n"


def test_the_overwrite_cleanup_never_unlinks_a_file_it_did_not_stage(
    vault, monkeypatch
):
    """The mirror hazard: answering a substitution by deleting the substitute.

    `_discard_temp` runs on every overwrite path, so unlinking the staging name
    blindly would destroy whatever a peer had put there — the same
    destructive-write class, just aimed at a different file. The failure
    direction is to leave litter, not to remove something unprovable.
    """
    (vault / "note.md").write_text("before\n", encoding="utf-8")
    fired = _replace_the_staging_file(monkeypatch, b"someone else's file")

    with pytest.raises(vault_fs.VaultFSError):
        vault_service.write_file("note.md", "ours", overwrite=True)

    assert fired
    leftovers = [p for p in vault.iterdir() if p.name.startswith(".tmp-")]
    assert len(leftovers) == 1, leftovers
    assert leftovers[0].name == fired[0]
    assert leftovers[0].read_bytes() == b"someone else's file"
    assert (vault / "note.md").read_text(encoding="utf-8") == "before\n"


def test_publishing_by_descriptor_refuses_without_proc(vault, monkeypatch):
    """No `/proc` means no way to publish an inode — refuse, never fall back to
    publishing whatever a staging *name* points at."""
    # The cache lives in `vault_fs` since #92 item 1 — one implementation of
    # by-descriptor publication for both write paths.
    monkeypatch.setattr(vault_fs, "_proc_fd_available_cache", False)

    with pytest.raises(vault_fs.UnsupportedFilesystem, match="/proc"):
        vault_service.write_bytes("blob.bin", b"ours", overwrite=False)

    assert not (vault / "blob.bin").exists()


def test_an_fstat_failure_leaks_no_descriptor_and_unlinks_nothing(
    vault, monkeypatch, caplog
):
    """The `try` opens immediately after the descriptor exists (round-2 #4).

    The descriptor is closed on the way out. The staging **name** is
    deliberately *not*: an `fstat` that failed left this call with no identity
    to compare the name against, so the cleanup cannot prove the name still
    refers to the inode it created, and unlinking it on that basis is the
    destructive write the whole guard exists to refuse (#104 review). The
    declared failure direction is to leave litter and say so — the next
    lines assert exactly that, and the warning names what was left.
    """
    (vault / "note.md").write_text("before\n", encoding="utf-8")
    before = _open_fds()
    real_fstat = os.fstat

    def boom(fd):
        info = real_fstat(fd)
        # Only the staging fd. `open_mutable` fstats *directory* descriptors to
        # confirm the root it pinned, and failing those would refuse the call
        # before anything is staged — the test would then pass for the wrong
        # reason, which is how it was written the first time.
        if stat.S_ISDIR(info.st_mode):
            return info
        raise OSError(errno.EIO, "I/O error")

    monkeypatch.setattr(vault_service.os, "fstat", boom)

    with caplog.at_level("WARNING"):
        for _ in range(3):
            with pytest.raises(OSError):
                vault_service.write_file("note.md", "after\n", overwrite=True)

    monkeypatch.setattr(vault_service.os, "fstat", real_fstat)
    assert _open_fds() == before
    left = sorted(p.name for p in vault.iterdir() if p.name != "note.md")
    assert len(left) == 3, left
    assert all(name.startswith(".tmp-note.md-") for name in left), left
    for name in left:
        assert any(name in r.message for r in caplog.records), name
    assert (vault / "note.md").read_text(encoding="utf-8") == "before\n"


def test_a_failing_close_after_publication_is_not_a_failed_write(
    vault, monkeypatch
):
    """A bare close raising EIO would discard a write that already happened —
    the trap `transfer._close_quietly` exists for."""
    real_close = os.close
    published: list[bool] = []

    def close_hook(fd):
        if published:
            real_close(fd)
            raise OSError(errno.EIO, "I/O error")
        return real_close(fd)

    real_link = vault_service._link_staged_inode

    def link_hook(*args, **kwargs):
        result = real_link(*args, **kwargs)
        published.append(True)
        return result

    monkeypatch.setattr(vault_service, "_link_staged_inode", link_hook)
    monkeypatch.setattr(vault_service.os, "close", close_hook)

    vault_service.write_bytes("blob.bin", b"ours", overwrite=False)

    assert (vault / "blob.bin").read_bytes() == b"ours"


# ── move_note verifies what actually moved (Codex BLOCKER 3) ────────────────


def _no_db(monkeypatch) -> list:
    """`async_session` that records nothing may be executed against it."""
    executed: list = []

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def execute(self, statement):
            executed.append(statement)

            class Empty:
                def all(self):
                    return []

            return Empty()

        async def commit(self):
            return None

    monkeypatch.setattr(tools, "async_session", FakeSession)
    return executed


@pytest.mark.parametrize("kind", ["directory", "symlink"])
async def test_a_source_replaced_by_a_non_file_is_moved_back(
    vault, monkeypatch, kind
):
    """`renameat2` relocates whatever inode is at the source when it runs.

    The regular-file check happens before the preflight, so a directory or a
    link dropped in afterwards is what moves — and "Moved" would then key the
    index to a subtree or an alias.
    """
    source = vault / "source.md"
    source.write_text("note\n", encoding="utf-8")
    (vault / "elsewhere.md").write_text("elsewhere\n", encoding="utf-8")
    executed = _no_db(monkeypatch)

    real = vault_fs.rename_noreplace
    fired: list[bool] = []

    def swap_then_rename(src_dir_fd, src_name, dst_dir_fd, dst_name):
        if not fired:
            fired.append(True)
            os.unlink(source)
            if kind == "directory":
                source.mkdir()
                (source / "inner.md").write_text("inner\n", encoding="utf-8")
            else:
                source.symlink_to(vault / "elsewhere.md")
        return real(src_dir_fd, src_name, dst_dir_fd, dst_name)

    monkeypatch.setattr(vault_fs, "rename_noreplace", swap_then_rename)

    result = await tools.move_note_impl("source.md", "moved.md")

    assert fired
    assert "Move refused" in result, result
    assert "moved back" in result, result
    # Put back where it was, and the destination is clean.
    assert not (vault / "moved.md").exists()
    if kind == "directory":
        assert (source / "inner.md").read_text(encoding="utf-8") == "inner\n"
    else:
        assert source.is_symlink()
        assert (vault / "elsewhere.md").read_text(encoding="utf-8") == "elsewhere\n"
    # And the index was never told about it.
    assert executed == []


async def test_a_failed_rollback_names_the_recovery_location(vault, monkeypatch):
    source = vault / "source.md"
    source.write_text("note\n", encoding="utf-8")
    executed = _no_db(monkeypatch)

    real = vault_fs.rename_noreplace
    calls: list[int] = []

    def swap_then_rename(src_dir_fd, src_name, dst_dir_fd, dst_name):
        calls.append(1)
        if len(calls) == 1:
            os.unlink(source)
            source.mkdir()
            return real(src_dir_fd, src_name, dst_dir_fd, dst_name)
        # The rollback: somebody took the source name over in the meantime.
        raise FileExistsError(17, "File exists", dst_name)

    monkeypatch.setattr(vault_fs, "rename_noreplace", swap_then_rename)

    result = await tools.move_note_impl("source.md", "moved.md")

    assert "Move refused" in result, result
    assert "could not be moved back" in result, result
    assert "moved.md" in result, result  # where to recover it from
    assert (vault / "moved.md").is_dir()
    assert executed == []


# ── round 2: the move verifier identifies what it moved (Codex #2) ──────────


async def test_a_destination_taken_over_after_the_move_is_not_rolled_back(
    vault, monkeypatch
):
    """Only our own inode is ever moved back.

    Rolling back on "the destination is not a regular file" alone would
    relocate a third party's file on the strength of a name — the same
    act-on-a-name mistake, one level up. If what is at the destination is not
    the inode we pinned before the rename, we say so and touch nothing.
    """
    (vault / "source.md").write_text("note\n", encoding="utf-8")
    executed = _no_db(monkeypatch)

    real = vault_fs.rename_noreplace
    fired: list[bool] = []

    def steal_the_destination(src_dir_fd, src_name, dst_dir_fd, dst_name):
        result = real(src_dir_fd, src_name, dst_dir_fd, dst_name)
        if not fired:
            fired.append(True)
            # Somebody replaces the destination straight after our rename.
            os.unlink(vault / dst_name)
            (vault / dst_name).mkdir()
            (vault / dst_name / "theirs.md").write_text("theirs\n", encoding="utf-8")
        return result

    monkeypatch.setattr(vault_fs, "rename_noreplace", steal_the_destination)
    # Pinned to something the destination can never be, so the branch under
    # test is reached deterministically: unlink-then-mkdir can reuse the very
    # inode number we just freed, which would make the identities compare equal
    # for reasons that have nothing to do with the rule.
    monkeypatch.setattr(tools, "_pin_source_inode", lambda target: (-1, -1))

    result = await tools.move_note_impl("source.md", "moved.md")

    assert fired
    assert "not the file that was moved" in result, result
    assert "nothing was moved back" in result, result
    # Their directory is exactly where they put it.
    assert (vault / "moved.md" / "theirs.md").read_text(encoding="utf-8") == "theirs\n"
    assert executed == []


async def test_an_unverifiable_destination_is_reported_not_raised(
    vault, monkeypatch
):
    """Every post-rename failure becomes an explicit result: by then the file
    has been published somewhere, and a traceback leaves the caller with no
    idea where."""
    (vault / "source.md").write_text("note\n", encoding="utf-8")
    executed = _no_db(monkeypatch)

    # Only *after* the rename: `open_mutable` lstats too, and failing there
    # would refuse the call long before the interesting branch.
    real_lstat = vault_service.MutableTarget.lstat
    real_rename = vault_fs.rename_noreplace
    moved: list[bool] = []

    def note_the_move(*args, **kwargs):
        result = real_rename(*args, **kwargs)
        moved.append(True)
        return result

    def unverifiable(self):
        if moved:
            raise OSError(errno.EIO, "I/O error")
        return real_lstat(self)

    monkeypatch.setattr(vault_fs, "rename_noreplace", note_the_move)
    monkeypatch.setattr(
        vault_service.MutableTarget, "lstat", unverifiable, raising=True
    )

    result = await tools.move_note_impl("source.md", "moved.md")

    assert "unverifiable" in result, result
    assert "moved.md" in result, result
    assert executed == []


async def test_an_unpinnable_source_is_reported_rather_than_assumed(
    vault, monkeypatch
):
    (vault / "source.md").write_text("note\n", encoding="utf-8")
    executed = _no_db(monkeypatch)
    monkeypatch.setattr(tools, "_pin_source_inode", lambda target: None)

    result = await tools.move_note_impl("source.md", "moved.md")

    assert "could not be identified" in result, result
    assert executed == []


# ── round 2: the descriptor budget (Codex #3) ───────────────────────────────


@needs_proc
async def test_running_out_of_descriptors_aborts_the_whole_move(
    vault, monkeypatch
):
    """EMFILE mid-preflight is not a per-source rewrite failure.

    Treating it as one moved the note and silently dropped every remaining
    rewrite, leaving `note_links` asserting links the vault bytes do not have —
    while the exhaustion took concurrent requests down as well.
    """
    (vault / "target.md").write_text("body\n", encoding="utf-8")
    sources = [f"src{i}.md" for i in range(6)]
    for name in sources:
        (vault / name).write_text("See [[target]]\n", encoding="utf-8")

    rows = [
        _Row(file_path="target.md", id=1),
        *[_Row(file_path=name, id=i + 2) for i, name in enumerate(sources)],
    ]
    _fake_move_session(
        monkeypatch, rows, [_Row(file_path=name) for name in sources]
    )

    real_open_mutable = tools.open_mutable
    opened: list[str] = []

    def exhaust_after_three(relative_path, user_id=None):
        if relative_path in sources:
            opened.append(relative_path)
            if len(opened) > 2:
                raise OSError(errno.EMFILE, "Too many open files")
        return real_open_mutable(relative_path, user_id=user_id)

    monkeypatch.setattr(tools, "open_mutable", exhaust_after_three)

    result = await tools.move_note_impl(
        "target.md", "moved.md", rewrite_links=True
    )

    assert "Move aborted" in result, result
    assert "file descriptors" in result, result
    # Nothing moved, nothing rewritten.
    assert (vault / "target.md").read_text(encoding="utf-8") == "body\n"
    assert not (vault / "moved.md").exists()
    for name in sources:
        assert (vault / name).read_text(encoding="utf-8") == "See [[target]]\n"


async def test_moves_with_rewrites_are_serialised_process_wide(
    vault, monkeypatch
):
    """Each move can be inside its own budget and still exhaust the table
    between them, so the bound has to hold for the process."""
    for i in (1, 2):
        (vault / f"t{i}.md").write_text("body\n", encoding="utf-8")
    _fake_move_session(monkeypatch, [], [])

    overlap = [0]
    peak = [0]
    real_gate = tools._move_rewrite_gate

    @asynccontextmanager
    async def counting_gate(rewrite_links: bool):
        async with real_gate(rewrite_links):
            overlap[0] += 1
            peak[0] = max(peak[0], overlap[0])
            try:
                await asyncio.sleep(0)  # yield, so an unlocked gate would overlap
                yield
            finally:
                overlap[0] -= 1

    monkeypatch.setattr(tools, "_move_rewrite_gate", counting_gate)

    await asyncio.gather(
        tools.move_note_impl("t1.md", "m1.md", rewrite_links=True),
        tools.move_note_impl("t2.md", "m2.md", rewrite_links=True),
    )

    assert peak[0] == 1, peak[0]
    assert (vault / "m1.md").exists() and (vault / "m2.md").exists()


async def test_a_move_without_rewrites_is_not_serialised(vault, monkeypatch):
    """Two descriptors, no preflight — the gate would only add contention."""
    async with tools._MOVE_REWRITE_LOCK:
        (vault / "note.md").write_text("body\n", encoding="utf-8")
        _fake_move_session(monkeypatch, [], [])
        result = await asyncio.wait_for(
            tools.move_note_impl("note.md", "moved.md"), timeout=5
        )
    assert "Moved" in result, result


# ── round 2: both endpoints acquired inside one guard (Codex #5) ────────────


@needs_proc
async def test_a_failure_opening_the_destination_closes_the_source(
    vault, monkeypatch
):
    """A non-`ValueError` failure on the second `open_mutable` must not strand
    the first one's descriptors."""
    (vault / "source.md").write_text("body\n", encoding="utf-8")
    real_open_mutable = tools.open_mutable

    def fail_on_destination(relative_path, user_id=None):
        if relative_path == "dest.md":
            raise OSError(errno.EMFILE, "Too many open files")
        return real_open_mutable(relative_path, user_id=user_id)

    monkeypatch.setattr(tools, "open_mutable", fail_on_destination)

    before = _open_fds()
    for _ in range(3):
        result = await tools.move_note_impl("source.md", "dest.md")
        assert "Could not open" in result, result
    assert _open_fds() == before
    assert (vault / "source.md").read_text(encoding="utf-8") == "body\n"


# ── #110: the fallback's publish errnos say what actually refused ───────────
#
# The note path stages *beside* its destination, so a genuine `EXDEV` here
# needs an exotic layout and cannot be produced by a bind mount at all — a
# same-directory link or rename does not cross anything. These inject the errno
# instead. That is not a claim the case is common: a message that is wrong
# whenever it fires is wrong, and the transfer path's equivalent branches
# already carry the accurate mapping. One vocabulary across both write paths.


def _fallback(monkeypatch) -> None:
    _refuse_o_tmpfile(monkeypatch)
    monkeypatch.setattr(
        vault_service.settings, "vault_allow_named_staging_fallback", True
    )
    vault_fs.reset_named_staging_state()


def _publish_raises(monkeypatch, name: str, code: int) -> None:
    def boom(*args, **kwargs):
        raise OSError(code, os.strerror(code))

    monkeypatch.setattr(vault_service.os, name, boom)


def test_the_fallback_link_names_the_mount_boundary(vault, monkeypatch):
    """It used to say "the vault filesystem does not support hard links" for
    `EXDEV` — false, and it sends an agent to change filesystems in response to
    a mount layout."""
    _fallback(monkeypatch)
    _publish_raises(monkeypatch, "link", errno.EXDEV)
    try:
        with pytest.raises(vault_fs.MountBoundary) as caught:
            vault_service.write_bytes("blob.bin", b"ours", overwrite=False)
    finally:
        vault_fs.reset_named_staging_state()

    message = str(caught.value)
    assert "different mount" in message
    assert "mount layout is what refuses" in message
    assert "hard link" not in message
    # Still an `UnsupportedFilesystem`, so no typed handler needs a new branch.
    assert isinstance(caught.value, vault_fs.UnsupportedFilesystem)
    assert not (vault / "blob.bin").exists()
    assert list(vault.iterdir()) == [], "staging litter left behind"


def test_a_filesystem_without_hard_links_still_says_so(vault, monkeypatch):
    """Splitting `EXDEV` out must not make `EOPNOTSUPP` vague: for that one the
    old message is exactly right."""
    _fallback(monkeypatch)
    _publish_raises(monkeypatch, "link", errno.EOPNOTSUPP)
    try:
        with pytest.raises(vault_fs.UnsupportedFilesystem) as caught:
            vault_service.write_bytes("blob.bin", b"ours", overwrite=False)
    finally:
        vault_fs.reset_named_staging_state()

    assert not isinstance(caught.value, vault_fs.MountBoundary)
    assert "does not support hard links" in str(caught.value)
    assert not (vault / "blob.bin").exists()


def test_a_denied_link_names_security_policy_not_the_filesystem(vault, monkeypatch):
    """Codex finding 7. A seccomp profile, an LSM or a mount option refuses
    `link` with `EPERM` on filesystems whose hard links work perfectly, so
    diagnosing the filesystem there is the same defect class as blaming it for
    a mount layout."""
    _fallback(monkeypatch)
    _publish_raises(monkeypatch, "link", errno.EPERM)
    try:
        with pytest.raises(vault_fs.UnsupportedFilesystem) as caught:
            vault_service.write_bytes("blob.bin", b"ours", overwrite=False)
    finally:
        vault_fs.reset_named_staging_state()

    message = str(caught.value)
    assert "denied" in message
    assert "security policy" in message
    assert "does not support hard links" not in message
    assert not isinstance(caught.value, vault_fs.MountBoundary)
    assert not (vault / "blob.bin").exists()


def test_the_overwrite_replace_names_the_mount_boundary(vault, monkeypatch):
    """The other half of #110: this branch had no `EXDEV` handling at all, so
    the boundary escaped as a bare `OSError` and the tools rendered it as
    "could not write" plus the kernel's two-word strerror — strictly less than
    the no-clobber branch beside it says, for the same layout."""
    (vault / "blob.bin").write_bytes(b"old")
    _fallback(monkeypatch)
    _publish_raises(monkeypatch, "replace", errno.EXDEV)
    try:
        with pytest.raises(vault_fs.MountBoundary) as caught:
            vault_service.write_bytes("blob.bin", b"new", overwrite=True)
    finally:
        vault_fs.reset_named_staging_state()

    message = str(caught.value)
    assert "different mount" in message
    assert "mount layout is what refuses" in message
    assert (vault / "blob.bin").read_bytes() == b"old"
    assert [p.name for p in vault.iterdir()] == ["blob.bin"], "staging litter left"


def test_an_unrelated_replace_errno_still_propagates(vault, monkeypatch):
    """Only `EXDEV` is reclassified; everything else keeps its own diagnosis."""
    (vault / "blob.bin").write_bytes(b"old")
    _fallback(monkeypatch)
    _publish_raises(monkeypatch, "replace", errno.EIO)
    try:
        with pytest.raises(OSError) as caught:
            vault_service.write_bytes("blob.bin", b"new", overwrite=True)
    finally:
        vault_fs.reset_named_staging_state()

    assert not isinstance(caught.value, vault_fs.VaultFSError)
    assert caught.value.errno == errno.EIO
    assert (vault / "blob.bin").read_bytes() == b"old"
