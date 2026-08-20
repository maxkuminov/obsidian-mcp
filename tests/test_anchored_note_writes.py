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
import base64
import errno
import os
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
    directory, so the publish is a same-directory rename."""
    (vault / "Folder").mkdir()
    order: list[str] = []
    staged: list[list[str]] = []
    real_fsync = os.fsync
    real_replace = os.replace

    def recording_fsync(fd):
        order.append("fsync")
        staged.append(sorted(os.listdir(vault / "Folder")))
        return real_fsync(fd)

    def recording_replace(*args, **kwargs):
        order.append("publish")
        return real_replace(*args, **kwargs)

    monkeypatch.setattr(vault_service.os, "fsync", recording_fsync)
    monkeypatch.setattr(vault_service.os, "replace", recording_replace)

    vault_service.write_file("Folder/note.md", "body")

    assert order == ["fsync", "publish"]
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
    assert peak[0] - before <= len(linking) + 6, peak[0] - before


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
    assert config.max_move_rewrite_sources() == 1024 - config.MOVE_REWRITE_FD_RESERVE

    # A limit so small the reserve would swallow it still allows a small move.
    monkeypatch.setattr(config.resource, "getrlimit", lambda _: (128, 128))
    assert config.max_move_rewrite_sources() == config._MIN_MOVE_REWRITE_FDS

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
    # One per planned rewrite, not two (`release_root`), plus the endpoints.
    assert peak[0] - before <= len(sources) + 6, peak[0] - before


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

    `link` + `unlink` — the shape this replaced — would have published the
    inode it linked and then unlinked the *replacement*, destroying it.
    """
    (vault / "source.md").write_text("original\n", encoding="utf-8")

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
    assert "Moved" in result, result
    # The replacement moved intact; nothing was unlinked behind its back.
    assert (vault / "destination.md").read_text(encoding="utf-8") == "replacement\n"
    assert not (vault / "source.md").exists()


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


# ── the staged inode, not the staged name (Codex BLOCKER 2) ─────────────────


def _replace_the_staging_file(monkeypatch, decoy: bytes):
    """Swap a different inode under the `.tmp-…` name after it is fsynced.

    A peer with write access to the destination directory could otherwise have
    *its* bytes published as the note: the publish named the staging file, and
    by then the name no longer meant what we wrote.
    """
    real_fsync = os.fsync
    fired: list[str] = []

    def fsync_then_swap(fd):
        result = real_fsync(fd)
        if fired:
            return result
        # Find our staging name through the fd's own directory.
        link = os.readlink(f"/proc/self/fd/{fd}")
        tmp = Path(link)
        fired.append(tmp.name)
        os.unlink(tmp)
        tmp.write_bytes(decoy)
        return result

    monkeypatch.setattr(vault_service.os, "fsync", fsync_then_swap)
    return fired


async def test_a_swapped_staging_file_is_never_published_by_create(
    vault, monkeypatch
):
    fired = _replace_the_staging_file(monkeypatch, b"DECOY")

    result = await tools.create_note_impl("note.md", "ours\n")

    assert fired, "the race never ran"
    # Either the note was never created, or it holds exactly our bytes — never
    # the decoy's. (Detaching our inode drops its link count to zero, which is
    # what makes `linkat` refuse.)
    assert "DECOY" not in result
    if (vault / "note.md").exists():
        assert (vault / "note.md").read_bytes() == b"ours\n"
    else:
        assert "staged copy" in result, result


def test_a_swapped_staging_file_is_never_published_by_a_raw_write(
    vault, monkeypatch
):
    _replace_the_staging_file(monkeypatch, b"DECOY")

    with pytest.raises(vault_fs.VaultFSError, match="staged copy"):
        vault_service.write_bytes("blob.bin", b"ours", overwrite=False)

    assert not (vault / "blob.bin").exists()


async def test_a_swapped_staging_file_is_refused_by_an_overwrite(
    vault, monkeypatch
):
    """`renameat` is inherently by name, so this one is a check — but it must
    refuse rather than publish the substitute."""
    note = vault / "note.md"
    note.write_text("before\n", encoding="utf-8")
    fired = _replace_the_staging_file(monkeypatch, b"DECOY")

    result = await tools.edit_note_impl("note.md", "after\n")

    assert fired
    assert "staged copy" in result, result
    assert note.read_text(encoding="utf-8") == "before\n"


def test_the_temp_sweep_never_unlinks_a_file_it_did_not_stage(vault, monkeypatch):
    """The mirror hazard: answering a substitution by deleting the substitute.

    `_discard_temp` runs on every path, so unlinking the staging name blindly
    would destroy whatever a peer had put there — the same destructive-write
    class, just aimed at a different file.
    """
    fired = _replace_the_staging_file(monkeypatch, b"someone else's file")

    with pytest.raises(vault_fs.VaultFSError):
        vault_service.write_bytes("blob.bin", b"ours", overwrite=False)

    leftovers = [p for p in vault.iterdir() if p.name.startswith(".tmp-")]
    assert len(leftovers) == 1, leftovers
    assert leftovers[0].name == fired[0]
    assert leftovers[0].read_bytes() == b"someone else's file"


def test_publishing_by_descriptor_refuses_without_proc(vault, monkeypatch):
    """No `/proc` means no way to publish an inode — refuse, never fall back to
    publishing whatever the staging *name* points at."""
    monkeypatch.setattr(vault_service, "_proc_fd_available_cache", False)

    with pytest.raises(vault_fs.UnsupportedFilesystem, match="/proc"):
        vault_service.write_bytes("blob.bin", b"ours", overwrite=False)

    assert not (vault / "blob.bin").exists()


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
