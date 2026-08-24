import asyncio
import contextlib
import ctypes
import errno
import fnmatch
import hashlib
import logging
import os
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import delete, func, literal, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert

from src.config import settings
from src.database import async_session
from src.models.db import NoteEmbedding, NoteLink, NoteMetadata, OAuthCode, OAuthToken, User
from src.services.embeddings import embed_note
from src.services.fts import index_tsvector_sql
from src.services.links import build_vault_index, extract_links, resolve_target
from src.services.transfer import canonical_vault_root
from src.services.vault import (
    _vault_root,
    extract_tags,
    parse_frontmatter,
    warm_user_vault_cache,
)

# Module-level flag the dashboard reads to surface "link extraction in
# progress" while the one-shot backfill is running.
link_backfill_in_progress: bool = False

# In-process heartbeat for the indexer: when a pass last *finished*, and
# whether it finished cleanly. The dashboard used to infer this from
# `max(notes_metadata.indexed_at)`, but that column only moves for notes a
# pass actually upserted or moved — a pass over an unchanged vault writes it
# nowhere. So a healthy indexer on an idle vault reported a last run of days
# ago, which is an invitation to reach for the Danger zone and re-embed the
# whole vault for nothing (#78).
#
# Deliberately in-process, not a table: it answers "is this process's loop
# alive", which is exactly a property of this process, and it costs no write
# per tick. It resets to None on restart — the startup pass sets it moments
# later, so the window where the dashboard reads "Never" is the window in
# which the first pass has genuinely not finished yet.
last_index_run_at: datetime | None = None
last_index_run_ok: bool | None = None


def _record_index_run(ok: bool) -> None:
    global last_index_run_at, last_index_run_ok
    last_index_run_at = datetime.now(timezone.utc)
    last_index_run_ok = ok

# Serializes full index/embed passes so the periodic loop and a
# panel-triggered on-demand reindex can never run index_vault/embed_vault
# concurrently for the same scope. Two overlapping passes share no DB lock and
# would race on move-detection, deleted-path removal, and per-note embedding
# delete+insert (duplicate-key errors, lost/duplicated rows). Both
# `run_indexer_loop` and `_reindex_background` acquire this before doing work.
index_pass_lock: asyncio.Lock = asyncio.Lock()


def _sanitize_value(v):
    """Recursively coerce a frontmatter value into a JSON-serializable form.

    Lists and dicts are walked element-by-element; non-string dict keys and
    any non-serializable scalar (e.g. a YAML date/datetime) are stringified.
    """
    if isinstance(v, (str, int, float, bool, type(None))):
        return v
    elif isinstance(v, list):
        return [_sanitize_value(i) for i in v]
    elif isinstance(v, dict):
        return {
            (k if isinstance(k, str) else str(k)): _sanitize_value(val)
            for k, val in v.items()
        }
    else:
        return str(v)


def _sanitize_frontmatter(fm: dict) -> dict:
    """Convert non-JSON-serializable values (dates, etc) to strings."""
    return _sanitize_value(fm)

logger = logging.getLogger(__name__)


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


# ══════════════════════════════════════════════════════════════════════════
# Index provenance (issue #91, migration 016)
# ══════════════════════════════════════════════════════════════════════════
#
# The question this record answers is **"did the assignment change?"**, not
# "is this the same directory?". The event it exists to detect is an operator
# repointing a user at another vault, which is a change to a value this system
# itself stores and writes; detecting that is exact and no input defeats it.
# Proving directory identity across time is a different and unwinnable
# question — a bit-identical clone of a filesystem presents the same inode
# numbers, generation counters and therefore the same file handles, at the same
# pathname, under the same assignment — and **filesystem substitution behind an
# unchanged assignment is a declared non-goal**. Do not add a heuristic for it
# (content overlap, path overlap, a mount identifier, a filesystem UUID): its
# failure direction is a silent keep on two vaults that merely resemble each
# other, and three review rounds rejected exactly that escalation.

# Verdicts. Total over every combination of inputs; see `classify_provenance`.
PROVENANCE_KEEP = "same_assignment"
PROVENANCE_REDERIVE = "provenance_unresolved"
PROVENANCE_DISCARD = "reassigned"
PROVENANCE_INDETERMINATE = "indeterminate"

# `struct file_handle`'s payload bound (linux/fs.h). A handle is at most this
# many opaque bytes; on ext4 and xfs it is eight — the inode number plus the
# inode's generation counter, which the kernel bumps precisely so a reused
# inode is not mistaken for the old one.
MAX_HANDLE_SZ = 128
AT_EMPTY_PATH = 0x1000

# The width of `users.indexed_vault_handle`. A token that will not fit is
# treated as unobtainable — recorded NULL — never truncated: a truncated token
# compared by byte equality is a signal that can produce a spurious match.
HANDLE_COLUMN_CHARS = 320

# How many offending paths an incomplete re-derive names before it summarises
# the remainder. 013's and 015's offender-report shape.
SKIP_REPORT_LIMIT = 20


class _FileHandle(ctypes.Structure):
    _fields_ = [
        ("handle_bytes", ctypes.c_uint),
        ("handle_type", ctypes.c_int),
        ("f_handle", ctypes.c_ubyte * MAX_HANDLE_SZ),
    ]


def _resolve_name_to_handle_at():
    """The glibc `name_to_handle_at` wrapper, or None if there is none.

    **Wrapper-first and wrapper-only**, unlike `vault_fs`'s `renameat2` shim:
    glibc has exported `name_to_handle_at` since 2.14 and it resolves on every
    version this project runs on, so there is no raw-syscall fallback and no
    architecture number table. A missing symbol is simply "no handle
    available" — not an error, not a guess, and not a degraded mode.
    """
    try:
        libc = ctypes.CDLL(None, use_errno=True)
    except OSError:  # pragma: no cover - no libc to bind against
        return None
    try:
        fn = libc.name_to_handle_at
    except AttributeError:  # pragma: no cover - glibc < 2.14
        return None
    fn.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.POINTER(_FileHandle),
        ctypes.POINTER(ctypes.c_int),
        ctypes.c_int,
    ]
    fn.restype = ctypes.c_int
    return fn


_name_to_handle_at_cache: tuple | None = None


def _name_to_handle_at_fn():
    """Cached `_resolve_name_to_handle_at`; the lookup is per-process."""
    global _name_to_handle_at_cache
    if _name_to_handle_at_cache is None:
        _name_to_handle_at_cache = (_resolve_name_to_handle_at(),)
    return _name_to_handle_at_cache[0]


def read_dir_handle(fd: int) -> str | None:
    """An opaque `"<handle_type>:<hex>"` token for the pinned directory, or None.

    **Best-effort hardening in the refusing direction only.** Where a handle is
    recorded for a user *and* one can be read now, and the two differ, a
    verdict that would otherwise be *keep* is demoted to *re-derive*. A
    matching handle grants nothing. Every "no": `EOPNOTSUPP` (procfs, sysfs,
    overlayfs and some FUSE mounts), `ENOSYS`, a missing symbol, an oversized
    payload — all return None, record NULL, log nothing and change no verdict.

    The token is **opaque**: compared by byte equality, never parsed, and
    **never fed to `open_by_handle_at`**, which needs `CAP_DAC_READ_SEARCH`
    that the container does not have. A handle is a value to compare, never a
    door to open.

    `mount_id` is deliberately ignored. It is not stable across a remount, and
    the handle bytes for one directory are identical on the host and inside a
    bind-mounting container whose `mount_id` differs.
    """
    fn = _name_to_handle_at_fn()
    if fn is None:  # pragma: no cover - glibc always has it in practice
        return None
    fh = _FileHandle()
    fh.handle_bytes = MAX_HANDLE_SZ
    mount_id = ctypes.c_int()
    ctypes.set_errno(0)
    rc = fn(fd, b"", ctypes.byref(fh), ctypes.byref(mount_id), AT_EMPTY_PATH)
    if rc != 0:
        # The errno is read the way `vault_fs._renameat2_raw` reads it, and
        # then deliberately **not** branched on. `EOPNOTSUPP`, `ENOSYS`,
        # `EOVERFLOW` and anything else all mean the same thing here — no
        # hardening signal for this root — and distinguishing them would only
        # invite a degraded mode that the design does not have. Bound so a
        # future reader sees the value was considered rather than dropped.
        _errno = ctypes.get_errno()
        del _errno
        return None
    size = int(fh.handle_bytes)
    if size < 0 or size > MAX_HANDLE_SZ:  # pragma: no cover - kernel contract
        return None
    token = f"{int(fh.handle_type)}:{bytes(fh.f_handle[:size]).hex()}"
    if len(token) > HANDLE_COLUMN_CHARS:  # pragma: no cover - 320 > any real handle
        return None
    return token


def encode_realpath(realpath: str) -> str:
    """`os.fsencode(realpath).hex()` — the form the record stores and compares.

    A POSIX pathname is an arbitrary sequence of non-NUL bytes under no
    obligation to be valid UTF-8, and Python decodes such a component with
    `surrogateescape`, so `os.path.realpath` can return a string carrying a
    lone surrogate like `'\\udcff'` that asyncpg cannot UTF-8-encode. The
    discard branch writes this value *and* the delete in **one** transaction,
    so an encode failure here would roll the delete back on every later pass
    and serve the former vault's index forever — #91's own symptom, produced by
    a value domain. Hex has no unrepresentable input, so the column is total
    over the fact by construction rather than by a bound.

    Comparison is **encode-then-compare on both sides**: never decode the
    stored value in order to compare it. `decode_realpath` exists only to
    render it in a log.
    """
    return os.fsencode(realpath).hex()


def decode_realpath(stored: str) -> str:
    """The inverse of `encode_realpath`, for **log rendering only**.

    `os.fsdecode(bytes.fromhex(stored))` returns the observed string exactly,
    surrogates included. Never call this to compare two provenances.
    """
    return os.fsdecode(bytes.fromhex(stored))


@dataclass(frozen=True)
class RootFacts:
    """The three provenance facts, all observed at one moment from one pinned
    descriptor.

    `realpath` is kept beside `realpath_hex` for logging; only `realpath_hex`
    is ever compared or stored.
    """

    assignment: str
    realpath: str
    realpath_hex: str
    handle: str | None


@dataclass(frozen=True)
class Classification:
    """One of the four verdicts, plus the human-readable reason for the log."""

    verdict: str
    reason: str


@contextlib.contextmanager
def pinned_root(vault: Path) -> Iterator[int]:
    """Open the assigned root once and hold it for the pass.

    **What the pin buys is deliberately narrow.** Within one pass, the facts
    observed, the files discovered and the bytes read all come from **one
    inode**, so a pass cannot record provenance describing a directory it did
    not scan. It does **not** prove the pinned directory is the one earlier
    rows came from; nothing proves that.

    Observing facts through a pathname and then scanning that pathname is
    check-then-act, and the interval is exploitable in both directions: an
    assignment naming a symbolic link can be retargeted after the observation
    and before the scan, so the pass indexes one directory and records another,
    and retargeting it back before the following pass leaves that record
    standing over rows the pass never derived from it. A directory descriptor
    keeps naming the same directory however its pathname is later renamed or
    relinked — which is why the mutation path is already anchored this way
    (#59).

    An unopenable root raises, which is the **indeterminate** verdict's
    "nothing at all, and the pass fails": no delete, no record. That is a
    change from the pathname-based scan, where `Path.rglob` on a missing
    directory silently yielded nothing and the ordinary prune then deleted
    every row the user had.
    """
    fd = os.open(vault, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        yield fd
    finally:
        os.close(fd)


def observe_root_facts(vault: Path, root_fd: int) -> RootFacts | None:
    """The three facts for the pinned root, or None when they are indeterminate.

    The realpath is bound to the descriptor the way #59's
    `_require_same_directory` binds its root: `os.stat(os.path.realpath(vault))`
    must report the same `(st_dev, st_ino)` as `os.fstat(root_fd)`. **That is
    the only use of device and inode numbers in this design** — a
    within-one-moment check that the realpath being recorded describes the
    inode being pinned. They are never stored and never compared across passes,
    because a reused inode makes two different directories agree.

    A disagreement is *indeterminate*, not a mismatch: the root's pathname is
    moving under the pass, and nothing observed can be trusted to describe what
    was scanned.
    """
    try:
        pinned = os.fstat(root_fd)
        realpath = os.path.realpath(vault)
        named = os.stat(realpath)
    except OSError:
        return None
    if (named.st_dev, named.st_ino) != (pinned.st_dev, pinned.st_ino):
        return None
    return RootFacts(
        assignment=canonical_vault_root(vault),
        realpath=realpath,
        realpath_hex=encode_realpath(realpath),
        handle=read_dir_handle(root_fd),
    )


def classify_provenance(
    recorded_assignment: str | None,
    recorded_realpath_hex: str | None,
    recorded_handle: str | None,
    facts: RootFacts | None,
) -> Classification:
    """The six-row classification, total over every combination of inputs.

    | observed vs. recorded | verdict |
    | --- | --- |
    | the root cannot be opened, or its realpath no longer names the pinned inode | **indeterminate** — nothing at all |
    | no record present (both null, or a half-set record) | **re-derive** |
    | assignment equal and realpath equal, no observable handle mismatch | **keep** |
    | assignment equal and realpath equal, but the recorded and observed handles differ | **re-derive** |
    | assignment differs **and** realpath differs | **discard** |
    | exactly one of assignment and realpath differs | **re-derive** |

    A record counts as **present** only when both the assignment string and the
    realpath are non-null. Both are always observable for a root the pass could
    pin, so a half-set record is drift rather than a state this code writes —
    and the safe reading of drift is that nothing is known, not that the half
    that is set may be trusted.

    A handle mismatch is **observable** only when a handle is recorded *and*
    one was read now. Either being absent means there is nothing to observe,
    **not** a degraded mode: the pass decides on the other two facts, does not
    re-derive on that account, and says nothing to the operator.

    Which error this prefers, said plainly. Ambiguity never resolves toward
    *keeping*, because silently wrong search results are the failure this
    product ranks highest — an agent acts on them without a human seeing the
    query. Ambiguity never resolves toward *discarding* either, because a
    discard costs a full re-embed of the vault. Everything between goes to a
    branch that asserts nothing and destroys nothing, and only **unanimous**
    disagreement destroys.

    This is the one function that computes "settled", so the scan and the gated
    ancillary passes cannot come to mean two different things by it.
    """
    if facts is None:
        return Classification(
            PROVENANCE_INDETERMINATE,
            "the assigned root could not be pinned, or its real path no longer "
            "names the directory that was pinned",
        )

    present = recorded_assignment is not None and recorded_realpath_hex is not None
    if not present:
        half = recorded_assignment is not None or recorded_realpath_hex is not None
        return Classification(
            PROVENANCE_REDERIVE,
            "a half-set provenance record is no record at all"
            if half
            else "no provenance is recorded for this user",
        )

    assignment_equal = recorded_assignment == facts.assignment
    realpath_equal = recorded_realpath_hex == facts.realpath_hex

    if assignment_equal and realpath_equal:
        # The handle can refuse a keep. It can never establish one, and it can
        # never establish a discard.
        if (
            recorded_handle is not None
            and facts.handle is not None
            and recorded_handle != facts.handle
        ):
            return Classification(
                PROVENANCE_REDERIVE,
                "the assignment and the real path agree but the recorded file "
                "handle does not match the one read now — the directory was "
                "probably replaced at the same path",
            )
        return Classification(
            PROVENANCE_KEEP, "the assignment and the real path are unchanged"
        )

    if not assignment_equal and not realpath_equal:
        return Classification(
            PROVENANCE_DISCARD,
            f"the assignment changed from {recorded_assignment!r} to "
            f"{facts.assignment!r} and the real path changed with it",
        )

    if assignment_equal:
        return Classification(
            PROVENANCE_REDERIVE,
            "the assignment is unchanged but the real path it names differs "
            "from the one recorded",
        )
    return Classification(
        PROVENANCE_REDERIVE,
        f"the assignment changed from {recorded_assignment!r} to "
        f"{facts.assignment!r} while the real path it names is unchanged",
    )


async def _read_recorded_provenance(session, user_id: int):
    """`(assignment, realpath_hex, handle)` for a user, all None if no row."""
    row = (
        await session.execute(
            select(
                User.indexed_vault_assignment,
                User.indexed_vault_realpath,
                User.indexed_vault_handle,
            ).where(User.id == user_id)
        )
    ).first()
    if row is None:
        return None, None, None
    return row[0], row[1], row[2]


async def _stamp_provenance(session, user_id: int, facts: RootFacts) -> None:
    """Write **all three** facts, NULL for anything not observed.

    There is no partial stamp. No branch may update one column and leave
    another describing a root it does not describe — that single rule is what
    makes a later observation safe to compare, because it can never be measured
    against a root the stamp did not cover. In particular a stamp taken with no
    handle available NULLs a previously recorded handle rather than leaving it
    beside a freshly observed pathname pair.

    Does not commit: the caller decides which transaction this belongs to, and
    on the discard path that is emphatically the same one as the delete.
    """
    await session.execute(
        update(User)
        .where(User.id == user_id)
        .values(
            indexed_vault_assignment=facts.assignment,
            indexed_vault_realpath=facts.realpath_hex,
            indexed_vault_handle=facts.handle,
        )
    )


async def classify_for_pass(session, user_id: int, vault: Path, root_fd: int):
    """`(Classification, RootFacts | None, recorded_triple)` for a pinned root.

    The one entry point the scan and both gated ancillary passes use, so
    "settled" cannot come to mean two different things in two places. The
    recorded triple comes back with it so a discard can log what it is
    replacing without issuing a second SELECT.
    """
    facts = observe_root_facts(vault, root_fd)
    recorded = await _read_recorded_provenance(session, user_id)
    return classify_provenance(*recorded, facts), facts, recorded


def describe_recorded(recorded) -> str:
    """The recorded provenance as an operator reads it, in a log line.

    The realpath is stored hex-encoded and is **decoded only here** — never in
    order to compare it. `decode_realpath` is lossless, surrogates included, so
    a pathname that cannot be spelled in UTF-8 still renders.
    """
    assignment, realpath_hex, handle = recorded
    if assignment is None and realpath_hex is None:
        return "no record"
    try:
        realpath = repr(decode_realpath(realpath_hex)) if realpath_hex else "none"
    except ValueError:  # pragma: no cover - a hand-edited column
        realpath = f"<undecodable: {realpath_hex!r}>"
    return (
        f"assignment={assignment!r} realpath={realpath} "
        f"handle={handle if handle is not None else 'none'}"
    )


def _format_skips(skips: list[str]) -> str:
    """013's and 015's offender-report shape: the first N, then a count."""
    shown = skips[:SKIP_REPORT_LIMIT]
    suffix = (
        f", and {len(skips) - len(shown)} more"
        if len(skips) > len(shown)
        else ""
    )
    return ", ".join(shown) + suffix


# ══════════════════════════════════════════════════════════════════════════
# The anchored, read-only walk
# ══════════════════════════════════════════════════════════════════════════
#
# Deliberately **not** a `vault_fs` helper, and that is a design decision
# rather than an ownership one. `vault_fs` is the *mutation* primitive module:
# every helper in it writes or refuses, and its containment contract forbids a
# symbolic link anywhere in the path, ever. The indexer needs the opposite leaf
# policy and must keep it — a markdown file reached through a symbolic link is
# indexed today. A shared helper would have to fork its symlink policy per
# caller, and a future editor unifying the two forks would silently change
# either what the index contains or what a transfer may write. Two walks with
# two policies, in the two modules that own those policies.


@dataclass(frozen=True)
class DiscoveredFile:
    """One discovered note, addressed by its **parent descriptor** and name.

    The parent descriptor is open only while the consumer is being handed this
    entry: the walk closes each directory once its children are done, so it
    costs one descriptor per level of depth rather than one per file. Read the
    file *now*, through `read_note_at`, or not at all.
    """

    rel: str
    parent_fd: int
    name: str


def discover_markdown_files_at(
    root_fd: int, *, skips: list[str] | None = None
) -> Iterator[DiscoveredFile]:
    """Walk the pinned root depth-first, yielding every indexable note.

    The same rule `Path.rglob` applied, now enforced by the kernel per descent
    rather than by a library's traversal habit:

    - dot-directories are skipped (`.obsidian`, `.git`, `.trash`, `.smart-env`);
    - directory symbolic links are **not** descended — `O_DIRECTORY |
      O_NOFOLLOW` per descent, and the resulting `ELOOP`/`ENOTDIR` is a
      deliberate non-descent, **not** a skip;
    - a symbolic link at a discovered `.md` file is left alone here and read as
      it is today (see `read_note_at`), because anchoring is about *which
      directory is scanned* and must not change what the index contains.

    A directory that could not be opened for any *other* reason is a genuine
    skip and is appended to `skips` when one is supplied — a re-derive that
    could not visit a subtree has not visited the root it is about to certify.
    """

    def walk(parent_fd: int, prefix: str) -> Iterator[DiscoveredFile]:
        try:
            with os.scandir(parent_fd) as entries:
                children = list(entries)
        except OSError as e:
            if skips is not None:
                skips.append(f"{prefix or '.'} (directory: {e})")
            return
        # Sorted so a pass's discovery order is stable, which keeps the
        # offender report and the move-detection pairing reproducible.
        for entry in sorted(children, key=lambda e: e.name):
            name = entry.name
            if name.startswith("."):
                continue
            rel = f"{prefix}/{name}" if prefix else name
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError as e:  # pragma: no cover - dirent type is cached
                if skips is not None:
                    skips.append(f"{rel} ({e})")
                continue
            if is_dir:
                try:
                    child_fd = os.open(
                        name,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                        dir_fd=parent_fd,
                    )
                except OSError as e:
                    # ELOOP/ENOTDIR: a directory symbolic link, or a directory
                    # that vanished. `rglob` declines to descend the former and
                    # silently drops the latter; neither is a skip.
                    if (
                        e.errno not in (errno.ELOOP, errno.ENOTDIR, errno.ENOENT)
                        and skips is not None
                    ):
                        skips.append(f"{rel} (directory: {e})")
                    continue
                try:
                    yield from walk(child_fd, rel)
                finally:
                    os.close(child_fd)
                continue
            if not name.endswith(".md"):
                continue
            yield DiscoveredFile(rel=rel, parent_fd=parent_fd, name=name)

    yield from walk(root_fd, "")


def discover_markdown_files(vault: Path) -> dict[str, Path]:
    """Every indexable note under `vault`, as `vault-relative str -> abs Path`.

    A thin pathname-taking wrapper over `discover_markdown_files_at`: it opens
    the root, drains the walk and closes. Kept callable this way so
    `tests/test_symlink_mutation_guard.py` — which asserts what discovery finds
    under a symlinked folder — passes **unchanged**, which is how we know the
    anchoring did not change what the index contains.

    This is the single definition of "what the index contains", so it also
    decides what `notes_metadata.file_path` holds — which the write tools must
    agree with. Two properties matter:

    - dot-directories are skipped (`.obsidian`, `.git`, `.trash`, …);
    - directory symbolic links are **not** descended, so a note under a
      symlinked folder is discovered once, at its real path (`Real/A.md`),
      never at the alias (`Shared/A.md`). `open_mutable` reports that same real
      path as the target's `rel`, which is why `move_note` keys its DB updates
      on it.
    """
    with pinned_root(vault) as root_fd:
        return {
            found.rel: vault / found.rel
            for found in discover_markdown_files_at(root_fd)
        }


def read_note_at(parent_fd: int, name: str) -> tuple[str, os.stat_result]:
    """`(text, stat)` for one note, both from **one** open descriptor.

    Deliberately **no** `O_NOFOLLOW` on the leaf: a symlinked `.md` is read
    today and this change must not alter what the index contains. Containment
    at the leaf was never claimed here and `open_mutable` remains the guard
    that matters for writes.

    The size and modification time come from `os.fstat` on the descriptor whose
    bytes were just read, replacing a second, independent pathname resolution
    that could describe a different file from the one that was hashed. Not the
    reason for the anchoring; a free consequence of it.

    Text mode with the default universal-newline translation, exactly as the
    `Path.read_text` it replaces — a binary read would leave `\\r\\n` intact and
    silently change every CRLF note's `content_hash`, forcing a re-embed.
    """
    fd = os.open(name, os.O_RDONLY | os.O_CLOEXEC, dir_fd=parent_fd)
    try:
        stat = os.fstat(fd)
        with open(fd, "r", encoding="utf-8", errors="strict", closefd=False) as handle:
            return handle.read(), stat
    finally:
        os.close(fd)


def open_beneath(root_fd: int, rel_path: str) -> tuple[int, str]:
    """`(parent_fd, name)` for a vault-relative path beneath the pinned root.

    For the passes that read a note the database already named rather than one
    a walk just discovered. Descends with the walk's rule — `O_DIRECTORY |
    O_NOFOLLOW` per component — and leaves the leaf alone, so a symlinked `.md`
    still reads. The caller owns `parent_fd` and must close it.
    """
    parts = [p for p in rel_path.split("/") if p and p != "."]
    if not parts or any(p == ".." for p in parts):
        raise OSError(f"not a vault-relative path: {rel_path!r}")
    fd = os.open(".", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC, dir_fd=root_fd)
    try:
        for part in parts[:-1]:
            child = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=fd,
            )
            os.close(fd)
            fd = child
    except BaseException:
        os.close(fd)
        raise
    return fd, parts[-1]


def read_note_beneath(root_fd: int, rel_path: str) -> tuple[str, os.stat_result]:
    """`read_note_at` for a path the caller names rather than one it walked."""
    parent_fd, name = open_beneath(root_fd, rel_path)
    try:
        return read_note_at(parent_fd, name)
    finally:
        os.close(parent_fd)


async def _reconcile_provenance(
    user_id: int, vault: Path, root_fd: int, log_suffix: str
) -> tuple[bool, RootFacts | None]:
    """Classify the pinned root and act, **before any file under it is read**.

    Returns `(re_derive, facts)`. `facts` is what a later stamp must write; it
    is None only when there is nothing to stamp.

    - **keep** — nothing at all, and nothing to stamp.
    - **discard** — delete the user's `notes_metadata` (embeddings cascade,
      links cascade on `source_note_id` and null out on `target_note_id`) and
      stamp the new provenance **in one committed transaction**, so no pass can
      leave rows from one vault beside a record naming another. The pass then
      indexes the new root ordinarily: the index is empty, so there is nothing
      to re-derive, and a pass that fails after this retries cleanly because
      the next one finds both facts in agreement.
    - **re-derive** — no delete and no stamp here; the stamp is withheld until
      the pass has finished, and only if it raised nothing *and* skipped
      nothing.
    - **indeterminate** — nothing at all, and the pass fails, because an index
      cannot be re-derived from a directory that cannot be read and destroying
      one because a bind mount was briefly unavailable buys nothing and costs
      the full re-embed.
    """
    async with async_session() as session:
        classification, facts, recorded = await classify_for_pass(
            session, user_id, vault, root_fd
        )

    if classification.verdict == PROVENANCE_INDETERMINATE:
        raise RuntimeError(
            f"Index provenance indeterminate{log_suffix}: {classification.reason}. "
            "Nothing was deleted and no provenance was recorded."
        )

    if classification.verdict == PROVENANCE_KEEP:
        return False, None

    assert facts is not None  # every non-indeterminate verdict observed them

    if classification.verdict == PROVENANCE_DISCARD:
        async with async_session() as session:
            result = await session.execute(
                delete(NoteMetadata).where(NoteMetadata.user_id == user_id)
            )
            await _stamp_provenance(session, user_id, facts)
            await session.commit()
        logger.warning(
            "Vault reassignment detected%s: %s. Discarded %s notes_metadata "
            "row(s) (embeddings and links cascade). Was [%s]; now recorded "
            "[assignment=%r realpath=%r handle=%s].",
            log_suffix,
            classification.reason,
            result.rowcount,
            describe_recorded(recorded),
            facts.assignment,
            facts.realpath,
            facts.handle if facts.handle is not None else "none",
        )
        return False, facts

    logger.info(
        "Re-deriving index%s: %s. Every discovered file will be re-parsed and "
        "every link row re-extracted; note_embeddings are kept.",
        log_suffix,
        classification.reason,
    )
    return True, facts


async def index_vault(user_id: int | None = None):
    """Scan vault, upsert notes_metadata with tsvector, remove deleted files.

    Single-user mode (`user_id is None`) keeps the legacy behavior: queries
    and inserts do not filter by `user_id` (NULL passes through every guard),
    and the index-provenance record is neither read nor written — single-user
    mode has no `users` row. Multi-user mode (`user_id` int) scopes
    existing-row lookups, stamps `user_id` on every upserted row, and
    reconciles the provenance record at the head of the pass.

    **The whole pass runs beneath one pinned root descriptor** (`pinned_root`):
    the facts observed, the files discovered and the bytes read all come from
    one inode, so a pass cannot record provenance describing a directory it did
    not scan. The reconciliation lives here rather than in any one caller, so
    the startup pass, the periodic tick and an operator-triggered reindex all
    inherit it.
    """
    vault = _vault_root(user_id)
    log_suffix = f" (user_id={user_id})" if user_id is not None else ""
    logger.info(f"Starting vault index scan...{log_suffix}")

    with pinned_root(vault) as root_fd:
        await _index_vault_pinned(user_id, vault, root_fd, log_suffix)


async def _index_vault_pinned(
    user_id: int | None, vault: Path, root_fd: int, log_suffix: str
):
    re_derive = False
    facts: RootFacts | None = None
    if user_id is not None:
        re_derive, facts = await _reconcile_provenance(
            user_id, vault, root_fd, log_suffix
        )

    # Anything the pass discovered but could not fully process. **A non-empty
    # list makes a re-derive incomplete and withholds the stamp** (A.7a): the
    # re-derive's whole claim is that every surviving row was written by this
    # pass from a file under the assigned root, and one skipped path falsifies
    # it — the ordinary prune keeps a row whose relative path exists under the
    # new root, which is exactly the row a re-derive exists to replace. The
    # repairs are still performed; only the certification is withheld.
    skips: list[str] = []

    async with async_session() as session:
        # Get existing hashes (scoped to this user when set)
        existing_stmt = select(NoteMetadata.file_path, NoteMetadata.content_hash)
        if user_id is None:
            existing_stmt = existing_stmt.where(NoteMetadata.user_id.is_(None))
        else:
            existing_stmt = existing_stmt.where(NoteMetadata.user_id == user_id)
        result = await session.execute(existing_stmt)
        existing = {row.file_path: row.content_hash for row in result.fetchall()}

        # Determine changes
        to_upsert = []
        # Body text parsed during this scan, keyed by rel_path. The tsvector
        # loop and the link rebuild below both reuse these instead of
        # re-reading from disk — a concurrent delete between the passes would
        # otherwise raise FileNotFoundError and leave the just-committed row's
        # content_tsvector null/stale, or silently drop that note's links while
        # the row the scan wrote stands.
        #
        # Memory shape, because re-derive mode changes it: an ordinary pass
        # buffers only the *changed* notes' parsed bodies, while a re-deriving
        # pass treats every note as changed and therefore holds the whole
        # vault's parsed bodies for the duration of the pass.
        path_to_content: dict[str, str] = {}
        # The set of discovered relative paths, accumulated as the walk yields
        # them. Discovery is a generator rather than a dict so the walk can
        # close each directory once its children are done — one descriptor per
        # level of depth, not one per file — which means each file must be read
        # *now*, while its parent descriptor is open.
        seen: set[str] = set()
        walk = discover_markdown_files_at(root_fd, skips=skips)
        with contextlib.closing(walk):
            for found in walk:
                rel_path = found.rel
                seen.add(rel_path)
                try:
                    raw, stat = read_note_at(found.parent_fd, found.name)
                except UnicodeDecodeError:
                    logger.warning(f"Skipping non-UTF8 file: {rel_path}")
                    skips.append(f"{rel_path} (not valid UTF-8)")
                    continue
                except Exception as e:
                    logger.warning(f"Failed to read {rel_path}: {e}")
                    skips.append(f"{rel_path} ({e})")
                    continue

                h = _content_hash(raw)
                # **Content-hash change detection is disabled under a
                # re-derive**, so every discovered file is parsed and upserted
                # regardless of its hash — which is also what makes every note
                # "changed" for the link rebuild below, and therefore what
                # deletes and re-extracts every one of this user's link rows.
                if not re_derive and rel_path in existing and existing[rel_path] == h:
                    continue  # No change

                try:
                    frontmatter, content = parse_frontmatter(raw)
                    tags = extract_tags(raw, frontmatter)
                except Exception as e:
                    logger.warning(f"Failed to parse {rel_path}: {e}")
                    skips.append(f"{rel_path} (parse: {e})")
                    continue
                path_to_content[rel_path] = content
                title = frontmatter.get("title") or os.path.splitext(found.name)[0]

                to_upsert.append({
                    "user_id": user_id,
                    "file_path": rel_path,
                    "title": title,
                    "tags": tags,
                    "frontmatter": _sanitize_frontmatter(frontmatter),
                    "content_hash": h,
                    "file_size": stat.st_size,
                    "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
                })

        logger.info(f"Found {len(seen)} markdown files{log_suffix}")

        # Compute deleted paths up front so the move-detection block can
        # repair them before the delete/insert pipeline tears them apart.
        deleted_paths = set(existing.keys()) - seen

        # ── Move detection ────────────────────────────────────────────────
        # An external move (file dragged in Obsidian) looks like
        # "delete old + insert new" from a path-only POV. Pair deleted paths
        # with genuinely-new paths sharing the same content_hash and update
        # `file_path` in place — preserving the row id keeps embeddings,
        # incoming `note_links.target_note_id` refs, and avoids a dangling-
        # link window. Falls back to delete+insert when a hash matches more
        # than one path on either side (ambiguous — could be a real duplicate).
        new_by_hash: dict[str, list[str]] = {}
        for e in to_upsert:
            if e["file_path"] not in existing:
                new_by_hash.setdefault(e["content_hash"], []).append(e["file_path"])
        deleted_by_hash: dict[str, list[str]] = {}
        for p in deleted_paths:
            deleted_by_hash.setdefault(existing[p], []).append(p)

        moves: list[tuple[str, str]] = []
        for h, olds in deleted_by_hash.items():
            news = new_by_hash.get(h, [])
            if len(olds) == 1 and len(news) == 1:
                moves.append((olds[0], news[0]))

        moved_new_paths: set[str] = set()
        if moves:
            user_clause = "user_id IS NULL" if user_id is None else "user_id = :uid"
            move_upd_sql = (
                "UPDATE notes_metadata "
                "SET file_path = :new, file_size = :size, "
                "modified_at = :mtime, indexed_at = now() "
                f"WHERE file_path = :old AND {user_clause}"
            )
            # Rewrite stored `target_path` strings that referenced the old
            # path. Two forms: the full path (`Folder/Old.md`) and the
            # extension-stripped form (`Folder/Old`) the extractor stores for
            # markdown links. Bare-name `[[noteName]]` references survive
            # untouched — their `target_note_id` is preserved via id reuse
            # and the stem doesn't change on a folder-only move.
            move_tp_sql = (
                "UPDATE note_links SET target_path = :new "
                "WHERE target_path = :old "
                f"AND source_note_id IN (SELECT id FROM notes_metadata WHERE {user_clause})"
            )

            entry_by_path = {e["file_path"]: e for e in to_upsert}
            for old, new in moves:
                e = entry_by_path[new]
                params: dict = {
                    "new": new, "old": old,
                    "size": e["file_size"], "mtime": e["modified_at"],
                }
                if user_id is not None:
                    params["uid"] = user_id
                await session.execute(text(move_upd_sql), params)

                old_no_ext = old[:-3] if old.endswith(".md") else old
                new_no_ext = new[:-3] if new.endswith(".md") else new
                for o, n in [(old, new), (old_no_ext, new_no_ext)]:
                    tp_params: dict = {"new": n, "old": o}
                    if user_id is not None:
                        tp_params["uid"] = user_id
                    await session.execute(text(move_tp_sql), tp_params)

                moved_new_paths.add(new)

            logger.info(
                f"Detected {len(moves)} file move(s) — preserved ids{log_suffix}"
            )

            # The existing delete+insert pipeline below mustn't touch the
            # paths we just repaired in place.
            to_upsert = [e for e in to_upsert if e["file_path"] not in moved_new_paths]
            deleted_paths -= {old for old, _ in moves}

        # Upsert changed files
        if to_upsert:
            for batch_start in range(0, len(to_upsert), 100):
                batch = to_upsert[batch_start:batch_start + 100]
                stmt = insert(NoteMetadata).values(batch)
                stmt = stmt.on_conflict_do_update(
                    # Match the composite UNIQUE(user_id, file_path) on
                    # notes_metadata (migration 009). The constraint is
                    # declared NULLS NOT DISTINCT so single-user-mode
                    # rows (user_id IS NULL) still collide and upsert
                    # correctly. Without NULLS NOT DISTINCT, PG 15+ would
                    # treat each NULL user_id as distinct and silently
                    # duplicate rows on every indexer pass.
                    index_elements=["user_id", "file_path"],
                    set_={
                        "title": stmt.excluded.title,
                        "tags": stmt.excluded.tags,
                        "frontmatter": stmt.excluded.frontmatter,
                        "content_hash": stmt.excluded.content_hash,
                        "file_size": stmt.excluded.file_size,
                        "modified_at": stmt.excluded.modified_at,
                        "indexed_at": text("now()"),
                    },
                )
                await session.execute(stmt)
            logger.info(f"Upserted {len(to_upsert)} notes")

        # Update tsvectors for changed notes
        if to_upsert:
            paths = [n["file_path"] for n in to_upsert]
            # In multi-user mode the same `file_path` can exist for multiple
            # users, so the UPDATE scopes by user: `user_id IS NULL` in
            # single-user mode, `user_id = :uid` (never NULL) in multi-user
            # mode. The tsvector expression is built from `settings.fts_configs`
            # (see `src/services/fts.py`) so index-time configs match the
            # query-time configs in `search.py`.
            tsv_frag, tsv_params = index_tsvector_sql("content")
            if user_id is None:
                tsv_sql = f"""
                    UPDATE notes_metadata
                    SET content_tsvector = {tsv_frag}
                    WHERE file_path = :path
                      AND user_id IS NULL
                """
            else:
                tsv_sql = f"""
                    UPDATE notes_metadata
                    SET content_tsvector = {tsv_frag}
                    WHERE file_path = :path
                      AND user_id = :uid
                """
            for path in paths:
                # Reuse the body parsed during the scan loop above instead of
                # re-reading from disk; a concurrent delete between the passes
                # would otherwise leave content_tsvector null/stale (issue #18).
                # A changed path with no buffered body is a skip, not a silent
                # `continue`: it leaves a row whose keyword vector this pass did
                # not write, which a re-derive must not certify.
                if path not in path_to_content:
                    skips.append(f"{path} (no buffered body for the keyword vector)")
                    continue
                content = path_to_content[path]
                try:
                    params: dict = {"content": content[:100000], "path": path, **tsv_params}
                    if user_id is not None:
                        params["uid"] = user_id
                    await session.execute(text(tsv_sql), params)
                except Exception:
                    logger.exception(f"Failed to update tsvector for {path}")
                    raise
            logger.info(f"Updated tsvectors for {len(paths)} notes{log_suffix}")

        # Remove deleted files (scoped to this user when set). `deleted_paths`
        # was computed earlier and any entries that turned out to be moves
        # have already been stripped out by the move-detection block above.
        if deleted_paths:
            del_stmt = delete(NoteMetadata).where(
                NoteMetadata.file_path.in_(deleted_paths)
            )
            if user_id is None:
                del_stmt = del_stmt.where(NoteMetadata.user_id.is_(None))
            else:
                del_stmt = del_stmt.where(NoteMetadata.user_id == user_id)
            await session.execute(del_stmt)
            logger.info(f"Removed {len(deleted_paths)} deleted notes{log_suffix}")

        # ── Link extraction for changed notes ───────────────────────────
        # We rebuild the vault_index here (post-commit), then for each
        # changed note delete-and-reinsert its rows in `note_links`. New or
        # renamed notes also get a re-resolution pass that updates any
        # previously-dangling rows now matching their path.
        # Moved notes need outgoing-link re-extraction too: same-folder
        # resolution can change once the note sits in a different directory.
        if to_upsert or deleted_paths or moved_new_paths:
            await _update_links_for_changed(
                session,
                vault,
                [n["file_path"] for n in to_upsert] + list(moved_new_paths),
                user_id=user_id,
                path_to_content=path_to_content,
                skips=skips,
            )

        # ── The tail stamp ────────────────────────────────────────────────
        # Written where the state it describes is established. On the re-derive
        # branch that state is "every surviving row was derived from this
        # root", which is not true until the pass has finished — so the stamp
        # is issued after the pass's last write and **only if it skipped
        # nothing**. Head-stamping a re-derive would be exactly the false
        # provenance this record exists to prevent, written by our own code
        # instead of by a migration.
        #
        # Committing it with the pass's own writes rather than afterwards makes
        # a crash mid-repair leave the previous record untouched, so the next
        # pass repairs again: bounded, idempotent, and never a stamp over a
        # half-repaired index.
        if re_derive and facts is not None:
            if skips:
                logger.warning(
                    "Re-derive incomplete%s: %d discovered path(s) were not "
                    "fully processed, so no provenance was recorded and the "
                    "next pass will re-derive again. Offenders: %s",
                    log_suffix,
                    len(skips),
                    _format_skips(skips),
                )
            else:
                await _stamp_provenance(session, user_id, facts)
                logger.info(
                    "Re-derive complete%s: recorded provenance "
                    "assignment=%r realpath=%r handle=%s",
                    log_suffix,
                    facts.assignment,
                    facts.realpath,
                    facts.handle if facts.handle is not None else "none",
                )
        elif skips:
            logger.warning(
                "%d discovered path(s) were not fully processed%s: %s",
                len(skips),
                log_suffix,
                _format_skips(skips),
            )

        # Metadata hashes, keyword vectors, deletions, and link rows describe
        # one filesystem snapshot. Commit them together so a failure in a
        # later stage cannot leave a new hash paired with stale search data
        # (which would make the next scan incorrectly skip the note).
        await session.commit()

    logger.info(f"Vault index scan complete{log_suffix}")


async def _update_links_for_changed(
    session,
    vault: Path,
    changed_paths: list[str],
    user_id: int | None = None,
    path_to_content: dict[str, str] | None = None,
    skips: list[str] | None = None,
):
    """Re-extract and upsert links for the given changed paths.

    Builds a fresh `vault_index` from `notes_metadata`, then for every changed
    note: deletes existing rows, extracts links, resolves targets, inserts.
    Finally, runs a re-resolution pass to attach previously-dangling rows
    whose `target_path` matches any of the changed notes.

    In multi-user mode the vault_index is scoped to `user_id` so a user's
    wikilinks cannot resolve to another user's note (they share the same
    `file_path` string but live in distinct `notes_metadata.id`s).

    **This reads no file.** It used to re-read each changed note from disk,
    which was both a second read of bytes the scan had already parsed and a
    second window in which the file could change or vanish — a disappearance
    between the scan and the rebuild silently dropped that note's links while
    the row the scan wrote stood. It now extracts from `path_to_content`, the
    buffer the scan already fills for the tsvector loop, which holds exactly
    the post-frontmatter body `extract_links` consumes. A changed path missing
    from the buffer is recorded in `skips` rather than silently passed over: it
    means a link row this pass was supposed to write is absent.

    `vault` is retained in the signature and is deliberately unused for reads.
    """
    bodies = path_to_content if path_to_content is not None else {}
    # Build vault_index once for the entire pass — scoped to this user when set.
    vi_stmt = select(NoteMetadata.file_path, NoteMetadata.id)
    if user_id is None:
        vi_stmt = vi_stmt.where(NoteMetadata.user_id.is_(None))
    else:
        vi_stmt = vi_stmt.where(NoteMetadata.user_id == user_id)
    rows = (await session.execute(vi_stmt)).all()
    vault_index = build_vault_index([(r.file_path, r.id) for r in rows])
    paths_to_id: dict[str, int] = vault_index["paths"]

    if changed_paths:
        # Process changed notes' outgoing links.
        change_ids = [paths_to_id[p] for p in changed_paths if p in paths_to_id]
        if change_ids:
            await session.execute(
                delete(NoteLink).where(NoteLink.source_note_id.in_(change_ids))
            )
            new_rows: list[dict] = []
            for path in changed_paths:
                src_id = paths_to_id.get(path)
                if src_id is None:
                    continue
                content = bodies.get(path)
                if content is None:
                    if skips is not None:
                        skips.append(f"{path} (no buffered body for the link rebuild)")
                    continue
                for link in extract_links(content):
                    target_id = resolve_target(link.target, path, vault_index)
                    new_rows.append({
                        "source_note_id": src_id,
                        "target_note_id": target_id,
                        "target_path": link.target[:1024],
                        "link_text": link.link_text,
                        "kind": link.kind,
                        "position": link.position,
                    })
            if new_rows:
                for batch_start in range(0, len(new_rows), 1000):
                    await session.execute(
                        insert(NoteLink).values(
                            new_rows[batch_start:batch_start + 1000]
                        )
                    )
            logger.info(
                f"Re-extracted links for {len(change_ids)} notes "
                f"({len(new_rows)} link rows)"
            )

    # Re-resolution pass: any newly-arrived note may resolve previously
    # dangling rows. We patch `target_note_id` for rows whose `target_path`
    # matches one of the changed paths in a few canonical forms.
    #
    # In multi-user mode we restrict the UPDATE to rows whose source note
    # belongs to the same user — otherwise alice's newly-created `foo.md`
    # would silently get attached as the target of bob's dangling
    # `[[foo]]` link.
    #
    # The bare-stem form (`[[Foo]]`) is only safe to match when exactly one
    # note in the vault carries that stem. With a shared stem the resolver
    # (`resolve_target`) uses same-folder preference and an alphabetical
    # tie-break, so a blind `target_path = stem` match here would mis-attach
    # dangling rows that belong to a *different* note. Ambiguous stems stay
    # dangling and resolve later when their own source note is reindexed.
    stems: dict[str, list[tuple[str, int]]] = vault_index["stems"]
    for path in changed_paths:
        nid = paths_to_id.get(path)
        if nid is None:
            continue
        stem = os.path.splitext(os.path.basename(path))[0]
        path_no_ext = path[:-3] if path.endswith(".md") else path
        # Always-safe canonical forms keyed to the exact stored path.
        params: dict = {
            "nid": nid,
            "full": path,
            "no_ext": path_no_ext,
        }
        # Only fold in the bare stem when it maps to a single note.
        if len(stems.get(stem, [])) == 1:
            params["stem"] = stem
        in_clause = ", ".join(f":{p}" for p in ("full", "no_ext", "stem")
                              if p in params)
        where_extra = ""
        if user_id is None:
            where_extra = (
                " AND source_note_id IN ("
                "SELECT id FROM notes_metadata WHERE user_id IS NULL)"
            )
        else:
            params["uid"] = user_id
            where_extra = (
                " AND source_note_id IN ("
                "SELECT id FROM notes_metadata WHERE user_id = :uid)"
            )
        reresolve_sql = (
            "UPDATE note_links "
            "SET target_note_id = :nid "
            "WHERE target_note_id IS NULL "
            f"AND target_path IN ({in_clause})"
            f"{where_extra}"
        )
        await session.execute(text(reresolve_sql), params)


async def _ancillary_pass_is_permitted(
    session, user_id: int | None, vault: Path, root_fd: int, label: str
) -> bool:
    """May this unverified ancillary pass write rows for this user?

    **Only on the `same assignment` verdict.** The one-shot link backfill and
    the keyword-vector rebuild both read `vault / file_path` and write rows the
    provenance is a claim about — `note_links`, `content_tsvector` — with **no
    verification of any kind** that the bytes they read belong to the row they
    write against. And neither may assume the scan settled that claim a moment
    ago: a user whose notes contain no links leaves the link backfill eligible
    on *every* startup, and a reassignment can commit between the scan and
    either of them. Allowing them to write under an unresolved provenance is
    exactly what lets a link row extracted from one root be committed against a
    metadata row from another.

    Verification is not merely unimplemented in those two. A link row's
    *resolution* is a function of the whole set of notes under a root rather
    than of one file's bytes, so no per-file check could license the backfill.
    The keyword rebuild could in principle be verified the way the embedding
    pass is, and is still gated, because nothing records what a tsvector was
    built from — there is no keyword analogue of `embedded_content_hash`, so a
    vector built from foreign bytes leaves no evidence a later pass could act
    on.

    Skipping costs them nothing even for a user whose provenance never settles:
    the re-derive branch does both of their jobs itself on every pass. So this
    is a delay, never a loss.

    **`embed_vault` is deliberately not gated** — see its own docstring. Its
    hash verification makes it safe by construction, and gating it composes
    with the completeness rule into indefinite staleness.

    The skip is **per user**: one unsettled user must not stop the pass for
    everybody else. Single-user mode has no `users` row and is ungated, exactly
    as it behaves today.
    """
    if user_id is None:
        return True
    classification, _facts, _recorded = await classify_for_pass(
        session, user_id, vault, root_fd
    )
    if classification.verdict == PROVENANCE_KEEP:
        return True
    logger.info(
        "%s skipped for user_id=%s: provenance is not settled (%s). No row was "
        "written for this user; the next index pass will settle it.",
        label,
        user_id,
        classification.reason,
    )
    return False


async def link_backfill_pass(user_id: int | None = None):
    """One-shot backfill that populates `note_links` for every note.

    Runs on startup when this user's graph has no rows. Rebuilds the graph in one transaction so a
    restart after any batch rolls back cleanly instead of mistaking a partial
    graph for a completed backfill.

    In multi-user mode each user's pass scopes its scan + vault_index to its
    own `notes_metadata` rows and replaces only links sourced by those rows.
    """
    global link_backfill_in_progress
    vault = _vault_root(user_id)
    with pinned_root(vault) as root_fd:
        await _link_backfill_pinned(user_id, vault, root_fd)


async def _link_backfill_pinned(user_id: int | None, vault: Path, root_fd: int):
    global link_backfill_in_progress
    async with async_session() as session:
        if not await _ancillary_pass_is_permitted(
            session, user_id, vault, root_fd, "Link backfill"
        ):
            return

        # Completion is inferred per user, never from the global table. The
        # rebuild itself commits atomically below, so any visible row proves a
        # prior pass for this scope completed (a zero-link vault is harmlessly
        # rescanned on the next startup).
        existing_stmt = (
            select(func.count(NoteLink.id))
            .join(NoteMetadata, NoteLink.source_note_id == NoteMetadata.id)
        )
        if user_id is None:
            existing_stmt = existing_stmt.where(NoteMetadata.user_id.is_(None))
        else:
            existing_stmt = existing_stmt.where(NoteMetadata.user_id == user_id)
        existing = (await session.execute(existing_stmt)).scalar() or 0
        if existing > 0:
            return

        rows_stmt = select(NoteMetadata.id, NoteMetadata.file_path)
        if user_id is None:
            rows_stmt = rows_stmt.where(NoteMetadata.user_id.is_(None))
        else:
            rows_stmt = rows_stmt.where(NoteMetadata.user_id == user_id)
        rows = (await session.execute(rows_stmt)).all()
        if not rows:
            return

        link_backfill_in_progress = True
        log_suffix = f" (user_id={user_id})" if user_id is not None else ""
        logger.info(f"Starting link backfill across {len(rows)} notes{log_suffix}")

        vault_index = build_vault_index([(r.file_path, r.id) for r in rows])

        try:
            note_ids = [r.id for r in rows]
            await session.execute(
                delete(NoteLink).where(NoteLink.source_note_id.in_(note_ids))
            )
            buffer: list[dict] = []
            for i, row in enumerate(rows, start=1):
                try:
                    raw, _stat = read_note_beneath(root_fd, row.file_path)
                except (UnicodeDecodeError, OSError):
                    continue
                _, content = parse_frontmatter(raw)
                for link in extract_links(content):
                    target_id = resolve_target(link.target, row.file_path, vault_index)
                    buffer.append({
                        "source_note_id": row.id,
                        "target_note_id": target_id,
                        "target_path": link.target[:1024],
                        "link_text": link.link_text,
                        "kind": link.kind,
                        "position": link.position,
                    })
                if len(buffer) >= 1000:
                    await session.execute(insert(NoteLink).values(buffer))
                    buffer.clear()
                if i % 500 == 0:
                    logger.info(f"Link backfill: {i}/{len(rows)} notes")

            if buffer:
                await session.execute(insert(NoteLink).values(buffer))

            await session.commit()

            logger.info(f"Link backfill complete: {len(rows)} notes scanned")
        finally:
            link_backfill_in_progress = False


async def embed_vault(user_id: int | None = None):
    """Embed notes that don't have embeddings yet or have changed.

    Multi-user mode: only embeds notes belonging to `user_id`. Each note's
    embeddings go into `note_embeddings`, which inherits user scope via its
    `note_id` FK back to `notes_metadata`. No `user_id` column on
    `note_embeddings` itself.

    **This pass is deliberately NOT gated on settled provenance, and it
    verifies every hash it certifies. The two halves are one decision.**

    Gating it was specified first and was wrong, because the two rules it would
    sit between compose into indefinite staleness. A permanently unreadable
    file withholds the provenance record forever — by design, so that nothing
    certifies a root the pass could not fully visit — and the gate would turn
    that withheld record into a permanent refusal to embed *anything* for that
    user. Meanwhile the scan keeps working: a readable note the user edits gets
    a fresh `content_hash` on every pass while its `note_embeddings` still hold
    the chunk text of the content it used to have, and `semantic_search` reads
    `chunk_text` with **no** `embedded_content_hash = content_hash` guard. One
    unreadable file would have converted that user's semantic search into a
    silently wrong one, indefinitely, for an agent consumer that acts on the
    result without a human ever seeing the query.

    Running ungated is sound only because of the verification below, and the
    argument is exact. The gate existed to stop a pass writing a row derived
    from one root against a metadata row derived from another. An embedding is
    a **pure function of content**, and the verification refuses to embed any
    bytes that do not hash to the `content_hash` the selected row records — so
    a chunk vector is written against a row only when the bytes it was built
    from are the bytes that row describes, and which directory supplied them is
    not a fact the vector depends on. Under a wrong root the hashes disagree
    and the pass skips.

    Reads are anchored beneath a root this pass pins itself, for the same
    within-pass-consistency reason the scan pins one.
    """
    vault = _vault_root(user_id)
    log_suffix = f" (user_id={user_id})" if user_id is not None else ""
    logger.info(f"Starting embedding pass...{log_suffix}")

    with pinned_root(vault) as root_fd:
        await _embed_vault_pinned(user_id, root_fd, log_suffix)


async def _embed_vault_pinned(user_id: int | None, root_fd: int, log_suffix: str):
    async with async_session() as session:
        # Find notes without embeddings or with stale embeddings, scoped
        # to this user when set. We bind the user_id parameter even in
        # single-user mode and compare with `IS NOT DISTINCT FROM` so the
        # NULL case still selects all rows without a separate branch.
        if user_id is None:
            sql = """
                SELECT nm.id, nm.file_path, nm.content_hash
                FROM notes_metadata nm
                WHERE nm.user_id IS NULL
                  AND (nm.embedded_content_hash IS NULL
                       OR nm.embedded_content_hash != nm.content_hash)
                ORDER BY nm.modified_at DESC
            """
            params: dict = {}
        else:
            sql = """
                SELECT nm.id, nm.file_path, nm.content_hash
                FROM notes_metadata nm
                WHERE nm.user_id = :uid
                  AND (nm.embedded_content_hash IS NULL
                       OR nm.embedded_content_hash != nm.content_hash)
                ORDER BY nm.modified_at DESC
            """
            params = {"uid": user_id}
        result = await session.execute(text(sql), params)
        unembedded = result.fetchall()

        if not unembedded:
            logger.info(f"All notes already embedded{log_suffix}")
            return

        logger.info(f"Embedding {len(unembedded)} notes...{log_suffix}")
        exclude_patterns = settings.embedding_exclude_patterns or []
        total_chunks = 0
        skipped_excluded = 0
        for i, row in enumerate(unembedded):
            # Re-check the pause flag every iteration so a panel-driven pause
            # (e.g. reset-embeddings) stops an in-flight embed pass promptly
            # instead of grinding through the whole backlog first (issue #19).
            if _is_paused():
                logger.info(f"Embedding pass paused, stopping early{log_suffix}")
                break
            try:
                # Skip files matching exclude patterns. Drop any pre-existing
                # embeddings (in case the file was indexed before exclusion was
                # configured) and stamp embedded_content_hash so the indexer
                # doesn't keep re-checking it.
                if any(fnmatch.fnmatch(row.file_path, pat) for pat in exclude_patterns):
                    await session.execute(
                        delete(NoteEmbedding).where(NoteEmbedding.note_id == row.id)
                    )
                    await session.execute(
                        text(
                            "UPDATE notes_metadata SET embedded_content_hash = :h "
                            "WHERE id = :i"
                        ),
                        {"h": row.content_hash, "i": row.id},
                    )
                    await session.commit()
                    skipped_excluded += 1
                    continue

                try:
                    raw, _stat = read_note_beneath(root_fd, row.file_path)
                except UnicodeDecodeError:
                    logger.warning(f"Skipping non-UTF8 file: {row.file_path}")
                    continue

                # ── Verify the hash before certifying it ──────────────────
                # `embed_note` marks a row embedded by copying the **row's**
                # `content_hash`, not a hash of the bytes it just embedded. So
                # a file that differs from its row at embedding time would be
                # embedded and then permanently marked as embedded for a hash
                # it does not have, and nothing would ever re-embed it.
                #
                # This check does two load-bearing jobs. It is what makes the
                # re-derive branch's retention of `note_embeddings` sound —
                # that branch keeps a vector *because* a matching content hash
                # proves it is the right vector for that file. And it is the
                # **entire licence** for this pass running ungated on
                # provenance (see the docstring): refusing bytes that do not
                # hash to the selected row's `content_hash` means the vector
                # and the row describe the same content whatever directory
                # supplied the bytes.
                #
                # Anyone removing this must re-gate `embed_vault` on settled
                # provenance in the same change.
                if _content_hash(raw) != row.content_hash:
                    logger.info(
                        "Skipping %s: its bytes no longer hash to the indexed "
                        "content_hash, so nothing may be certified against that "
                        "row. A later pass will embed it once the scan has "
                        "refreshed the row.",
                        row.file_path,
                    )
                    continue

                _, content = parse_frontmatter(raw)

                # Get the NoteMetadata object
                note_result = await session.execute(
                    select(NoteMetadata).where(NoteMetadata.id == row.id)
                )
                note = note_result.scalar_one()

                chunks = await embed_note(session, note, content)
                total_chunks += chunks
                await session.commit()

                if (i + 1) % 50 == 0:
                    logger.info(f"Embedded {i + 1}/{len(unembedded)} notes ({total_chunks} chunks)")
            except Exception as e:
                logger.warning(f"Failed to embed {row.file_path}: {e}")
                await session.rollback()

        logger.info(
            f"Embedding complete{log_suffix}: {len(unembedded)} notes, {total_chunks} chunks"
            + (f", {skipped_excluded} skipped by exclude patterns" if skipped_excluded else "")
        )


async def rebuild_tsvectors(session, user_id: int | None = None) -> int:
    """Recompute `content_tsvector` for every indexed note under the currently
    configured `FTS_CONFIGS` (see `src/services/fts.py`). Returns the count of
    notes updated.

    Run this after changing `FTS_CONFIGS`, since `notes_metadata` stores no raw
    body column — the tsvector must be rebuilt by re-reading each note's file.
    This rebuilds the KEYWORD index only: it does NOT touch embeddings and makes
    NO API calls, so it's cheap (seconds for a few thousand notes), unlike
    `reset-embeddings`.

    Scoped to `user_id` when set (multi-user mode); single-user mode passes
    `None` and rebuilds every note. Reuses `index_tsvector_sql` so the rebuilt
    tsvector is byte-identical to what the indexer would write for the same
    config(s).

    **Gated on settled provenance, per user**, and anchored beneath a root it
    pins itself — see `_ancillary_pass_is_permitted` for why an unverified
    writer of rows the provenance is a claim about may not run under an
    unresolved one. A skipped user is logged once and returns zero.
    """
    vault = _vault_root(user_id)
    log_suffix = f" (user_id={user_id})" if user_id is not None else ""
    with pinned_root(vault) as root_fd:
        return await _rebuild_tsvectors_pinned(
            session, user_id, vault, root_fd, log_suffix
        )


async def _rebuild_tsvectors_pinned(
    session, user_id: int | None, vault: Path, root_fd: int, log_suffix: str
) -> int:
    if not await _ancillary_pass_is_permitted(
        session, user_id, vault, root_fd, "Keyword-vector rebuild"
    ):
        return 0

    tsv_frag, tsv_params = index_tsvector_sql("content")
    upd_sql = text(
        f"UPDATE notes_metadata SET content_tsvector = {tsv_frag} WHERE id = :id"
    )

    rows_stmt = select(NoteMetadata.id, NoteMetadata.file_path)
    if user_id is None:
        rows_stmt = rows_stmt.where(NoteMetadata.user_id.is_(None))
    else:
        rows_stmt = rows_stmt.where(NoteMetadata.user_id == user_id)
    rows = (await session.execute(rows_stmt)).all()
    logger.info(f"Rebuilding tsvectors for {len(rows)} notes{log_suffix}")

    updated = 0
    for row in rows:
        try:
            raw, _stat = read_note_beneath(root_fd, row.file_path)
        except (UnicodeDecodeError, OSError):
            continue
        _, content = parse_frontmatter(raw)
        await session.execute(
            upd_sql, {"content": content[:100000], "id": row.id, **tsv_params}
        )
        updated += 1
        if updated % 500 == 0:
            await session.commit()
            logger.info(f"rebuild_tsvectors: {updated}/{len(rows)} notes{log_suffix}")
    await session.commit()
    logger.info(f"rebuild_tsvectors complete: {updated} notes{log_suffix}")
    return updated


async def cleanup_expired_tokens():
    """Delete OAuth codes and tokens that are more than 7 days dead.

    The token half used to read ``expires_at < cutoff OR revoked``, and that
    second disjunct carried **no age condition at all** despite this
    docstring's "older than 7 days" — every revoked token was deleted on the
    next pass, i.e. within `INDEX_INTERVAL_SECONDS` (5 minutes by default).

    That is wrong now that the panel lists revoked tokens as a grant's
    revocation history (issue #64). Filtering revoked rows *out of the page*
    is what made a Revoke that did nothing read as success — the row simply
    disappeared. Deleting them out of the table five minutes later reproduces
    the same blank space, just with a delay.

    **The 7-day window is measured from `expires_at`, not `created_at`, and
    that choice is load-bearing.** Revocation time is not stored anywhere, so
    the window has to be derived from a column we do have:

    * A token can only be revoked while it exists, so its revocation time R
      satisfies ``R <= expires_at`` for every revocation the panel or the token
      endpoint performs. Deleting only when ``expires_at < now - 7d`` therefore
      *guarantees* ``R < now - 7d`` — every revoked row is visible for at least
      seven days after it was revoked.
    * `created_at` gives no such guarantee and gets it backwards: a refresh
      token minted 30 days ago and revoked one minute ago is already 30 days
      past `created_at`, so it would be purged immediately — precisely the case
      the operator most needs to see.

    Age-gating the revoked branch makes it a strict subset of the expiry
    branch, so the correct implementation is the single predicate below rather
    than a redundant `or_`. Revoked tokens are still deleted; they are deleted
    seven days after they would have expired anyway, which is the same
    retention their unrevoked siblings get. The auth-code half is unchanged: a
    used code is spent immediately and has no history value.

    Edge case, stated rather than hidden: a family revocation also flips
    `revoked` on tokens that had *already* expired, so for those R can exceed
    `expires_at`. Such a row is deleted on the ordinary expiry schedule and can
    therefore disappear sooner than seven days after that flip — but it was
    already dead and already displayed as "Expired" before the revocation
    touched it, so no revocation the operator performed becomes invisible.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)

    async with async_session() as session:
        # Clean up expired/used auth codes
        result = await session.execute(
            delete(OAuthCode).where(
                or_(
                    OAuthCode.expires_at < cutoff,
                    OAuthCode.used == True,
                )
            )
        )
        codes_deleted = result.rowcount

        # Clean up tokens more than 7 days past their expiry — revoked ones
        # included, and on the same schedule. See the docstring: an unqualified
        # `OR revoked` deleted a revocation's evidence within five minutes of
        # the operator creating it.
        result = await session.execute(
            delete(OAuthToken).where(OAuthToken.expires_at < cutoff)
        )
        tokens_deleted = result.rowcount

        await session.commit()

        if codes_deleted or tokens_deleted:
            logger.info(f"Token cleanup: {codes_deleted} codes, {tokens_deleted} tokens removed")


def _is_paused() -> bool:
    """Check if a panel-driven action has paused the indexer."""
    try:
        from src.control_panel import routes as panel_routes
        return bool(getattr(panel_routes, "indexer_paused", False))
    except Exception:
        return False


async def _active_user_ids() -> list[int]:
    """Return ids of active users with a non-null `vault_path`. Empty list in
    single-user mode (the caller already takes the legacy NULL-user path)."""
    async with async_session() as session:
        # Warm the in-process vault-path cache for every active user before
        # the indexer kicks off — saves a per-user lookup later.
        await warm_user_vault_cache(session)
        result = await session.execute(
            select(User.id).where(
                User.is_active.is_(True),
                User.vault_path.isnot(None),
            )
        )
        return [row[0] for row in result.all()]


# One wall-clock bound for the whole pre-warm. It runs while holding
# `index_pass_lock`, so an unbounded hang would block the panel's reindex and
# reset-embeddings actions indefinitely.
PREWARM_TIMEOUT_SECONDS = 15.0

_HNSW_INDEX_NAME = "ix_note_embeddings_embedding_hnsw"

# Tri-state cache for "does an HNSW index exist on note_embeddings.embedding".
# None = not yet looked up. Deployments with EMBEDDING_DIMENSIONS > 2000 have
# no such index (pgvector's HNSW limit) and must not pay a sequential scan
# every five minutes just to warm a cache. `reset_embeddings` drops and
# recreates the index, so it invalidates this via `invalidate_hnsw_index_cache`.
_hnsw_index_present: bool | None = None


def invalidate_hnsw_index_cache() -> None:
    """Forget the cached `pg_indexes` lookup.

    Called by the panel's reset-embeddings action, which drops the HNSW index
    and only recreates it when the configured dimension allows one — so the
    cached answer can go stale in either direction.
    """
    global _hnsw_index_present
    _hnsw_index_present = None


async def _hnsw_index_exists(session) -> bool:
    global _hnsw_index_present
    if _hnsw_index_present is None:
        result = await session.execute(
            text(
                "SELECT 1 FROM pg_indexes "
                "WHERE tablename = 'note_embeddings' AND indexname = :name"
            ),
            {"name": _HNSW_INDEX_NAME},
        )
        _hnsw_index_present = result.first() is not None
    return _hnsw_index_present


def _probe_vector() -> list[float]:
    """A deterministic non-zero unit vector of `EMBEDDING_DIMENSIONS`.

    Non-zero matters: a zero vector has no cosine direction, so
    `embedding <=> '[0,...]'` is undefined and the scan would not traverse the
    graph — it would warm nothing.
    """
    dim = int(settings.embedding_dimensions)
    return [1.0] + [0.0] * (dim - 1)


# The planner hint the search path uses. Named so the probe and the test that
# EXPLAINs it cannot drift apart from each other.
PROBE_PLANNER_SETTING = "SET LOCAL random_page_cost = 1.1"


def probe_statement():
    """The HNSW probe statement, exactly as `_prewarm_once` issues it.

    Factored out so `tests/integration/test_prewarm_probe.py` can EXPLAIN the
    statement production runs (under `PROBE_PLANNER_SETTING`) instead of a
    hand-copied lookalike: the whole point of the probe is that it walks the
    HNSW index, and only the plan of *this* statement can show that.
    """
    return (
        select(literal(1))
        .select_from(NoteEmbedding)
        .order_by(NoteEmbedding.embedding.cosine_distance(_probe_vector()))
        .limit(1)
    )


async def _prewarm_once() -> tuple[float | None, float | None]:
    """The body of the pre-warm. Returns `(embed_ms, probe_ms)`, either None
    when that half was skipped. Raises freely — the caller contains it."""
    embed_ms: float | None = None
    probe_ms: float | None = None

    # Only local providers have warm state worth keeping. A remote API would
    # just be billed once per tick for nothing.
    if settings.embedding_provider == "ollama":
        from src.services.embeddings import get_embedding

        start = time.monotonic()
        await get_embedding("warmup")
        embed_ms = (time.monotonic() - start) * 1000

    async with async_session() as session:
        if not await _hnsw_index_exists(session):
            logger.info(
                "Pre-warm: HNSW probe skipped (no %s index; "
                "embedding_dimensions=%s exceeds pgvector's 2000-dim limit?)",
                _HNSW_INDEX_NAME,
                settings.embedding_dimensions,
            )
            return embed_ms, None

        # Same planner hint the search path uses, so the probe walks the index
        # and pulls the pages a real search would need — a seq scan here would
        # warm the heap instead, which is not what goes cold.
        await session.execute(text(PROBE_PLANNER_SETTING))
        stmt = probe_statement()
        start = time.monotonic()
        await session.execute(stmt)
        probe_ms = (time.monotonic() - start) * 1000

    return embed_ms, probe_ms


async def prewarm_search_caches() -> None:
    """Keep the embedding model resident and the HNSW hot pages cached.

    `semantic_search` latency is bimodal: ~0.47 s warm, ~17.5 s cold — 14 s of
    that is Ollama reloading bge-m3 after eviction, ~3 s is HNSW index pages
    missing from a 128 MB `shared_buffers` shared with another tenant. As the
    median gap between calls grew from 135 s to 1,676 s, more calls paid the
    cold price (p50 1.2 s → 4.8 s over five weeks). One warm-up per indexer
    tick costs ≈ 0.4 s + 6 ms per five minutes and removes both.

    Runs under `index_pass_lock` (the caller holds it), so it can never overlap
    an index pass, a panel reindex, or a reset-embeddings. Never raises for an
    ordinary failure: a broken embedding provider is the indexer's business,
    not the pre-warm's, and the loop's failure counter must not react to it.
    `CancelledError` is re-raised so lifespan shutdown still stops the loop.
    """
    if settings.mcp_sandbox_mode:
        return
    # Re-checked here, not just before the pass: a panel action can set the
    # pause flag *during* a long index pass, and it does so precisely because
    # it is about to run destructive statements.
    if _is_paused():
        logger.info("Pre-warm skipped (paused)")
        return

    try:
        embed_ms, probe_ms = await asyncio.wait_for(
            _prewarm_once(), timeout=PREWARM_TIMEOUT_SECONDS
        )
    except asyncio.CancelledError:
        # Lifespan shutdown. Must propagate, or the indexer task outlives the
        # app and keeps holding DB sessions.
        raise
    except TimeoutError:
        logger.warning(
            "Pre-warm exceeded %.0fs and was abandoned", PREWARM_TIMEOUT_SECONDS
        )
    except Exception as e:  # noqa: BLE001 - pre-warm must never break the loop
        logger.warning("Pre-warm failed (non-fatal): %s", e)
    else:
        logger.info(
            "Pre-warm complete (embed_ms=%s, probe_ms=%s)",
            "skipped" if embed_ms is None else f"{embed_ms:.0f}",
            "skipped" if probe_ms is None else f"{probe_ms:.0f}",
        )


async def _index_pass_once(user_id: int | None) -> bool:
    """One full index + embed pass for a single user (or single-user mode).

    Returns True only if both stages completed. Failures are swallowed per
    stage so one user's broken vault cannot stop every other user's pass, but
    the caller has to *know* they happened: a tick that logged two failures
    must not stamp the heartbeat as a healthy run (#78). Swallowing and
    returning True is exactly the "reports fine, is not" defect the heartbeat
    exists to remove.
    """
    ok = True
    try:
        await index_vault(user_id=user_id)
    except Exception as e:
        ok = False
        logger.error(f"Index failed (user_id={user_id}): {e}")
    try:
        await embed_vault(user_id=user_id)
    except Exception as e:
        ok = False
        logger.error(f"Embedding failed (user_id={user_id}): {e}")
    return ok


async def run_indexer_loop():
    """Run indexer on startup and then periodically.

    Multi-user mode iterates active users sequentially per pass (v1 simplicity;
    parallelism can come later). Single-user mode runs one legacy pass with
    `user_id=None`.
    """
    # Hold `index_pass_lock` for the initial pass too, so a panel-triggered
    # `_reindex_background` fired during startup is serialized against it.
    startup_ok = True
    async with index_pass_lock:
        if settings.multi_user_mode:
            # Initial pass per user.
            user_ids = await _active_user_ids()
            for uid in user_ids:
                try:
                    await index_vault(user_id=uid)
                except Exception as e:
                    startup_ok = False
                    logger.error(f"Initial index failed (user_id={uid}): {e}")
            try:
                # Link backfill still uses the global "table empty" guard but
                # runs the per-user pass when triggered. Iterate every user so
                # each user's notes get their links resolved against their own
                # vault_index.
                for uid in user_ids:
                    await link_backfill_pass(user_id=uid)
            except Exception as e:
                startup_ok = False
                logger.error(f"Link backfill failed: {e}")
            for uid in user_ids:
                try:
                    await embed_vault(user_id=uid)
                except Exception as e:
                    startup_ok = False
                    logger.error(f"Initial embedding failed (user_id={uid}): {e}")
        else:
            try:
                await index_vault()
            except Exception as e:
                startup_ok = False
                logger.error(f"Initial index failed: {e}")

            try:
                await link_backfill_pass()
            except Exception as e:
                startup_ok = False
                logger.error(f"Link backfill failed: {e}")

            try:
                await embed_vault()
            except Exception as e:
                startup_ok = False
                logger.error(f"Initial embedding failed: {e}")

    # The startup pass counts as a run: it does the same work a tick does, and
    # without it the dashboard would read "Never" for a whole interval after
    # every restart.
    _record_index_run(startup_ok)

    consecutive_failures = 0
    logger.info(
        f"Periodic indexer loop armed (interval={settings.index_interval_seconds}s, "
        f"multi_user={settings.multi_user_mode})"
    )
    while True:
        await asyncio.sleep(settings.index_interval_seconds)
        logger.info("Periodic indexer tick")
        if _is_paused():
            logger.info("Periodic tick skipped (paused)")
            continue
        try:
            # Hold `index_pass_lock` for the whole index/embed pass so a
            # concurrent panel-triggered `_reindex_background` cannot run a
            # second index_vault/embed_vault over the same scope.
            tick_ok = True
            async with index_pass_lock:
                if settings.multi_user_mode:
                    # Re-fetch the user list every cycle so newly-added or
                    # newly-deactivated users are picked up without a restart.
                    # One user's failure does not abort the others, but it
                    # does make the whole tick a failed run.
                    for uid in await _active_user_ids():
                        if not await _index_pass_once(uid):
                            tick_ok = False
                else:
                    await index_vault()
                    await embed_vault()
                # Still under the lock: serialised against a panel reindex and
                # against reset-embeddings, which also takes this lock. It
                # never raises, so `consecutive_failures` cannot react to it,
                # and it delays the next tick by at most PREWARM_TIMEOUT_SECONDS.
                await prewarm_search_caches()
            await cleanup_expired_tokens()
            consecutive_failures = 0
            # Heartbeat: the tick completed. Recorded whether or not the pass
            # found anything to index — that is the whole point (#78) — but
            # `tick_ok` is False if any per-user pass swallowed an exception,
            # so a multi-user tick that failed for every user is not stamped
            # healthy just because the loop itself survived.
            _record_index_run(tick_ok)
        except Exception as e:
            consecutive_failures += 1
            # A tick that raised still *ran*; the dashboard says so and marks
            # it failed rather than showing a stale-looking success.
            # `CancelledError` is a BaseException and does not land here, so
            # lifespan shutdown is not recorded as a failed pass.
            _record_index_run(False)
            logger.error(f"Periodic task failed ({consecutive_failures} consecutive): {e}")
            if consecutive_failures >= 5:
                logger.critical("Indexer has failed 5+ consecutive times — manual intervention required")
