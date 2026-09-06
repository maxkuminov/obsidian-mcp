"""Vault-root overlap detection: the two checks, the snapshot, and its accessors.

Two active users whose `users.vault_path` values name overlapping directories
are not a *partial* leak — they are one tenant wearing two names, in both
directions, for every tool this server has. `openat2(RESOLVE_BENEATH)` confines
every lookup to the caller's root and agrees that the *other* tenant's files are
beneath it; the indexer files one tenant's notes under the other's `user_id`, so
`semantic_search` and `keyword_search` answer with them. The panel's historic
check was string equality of the two assignments, which sees none of it.

This module is the one place that decides "do these two roots collide", and the
one place that publishes the answer. It holds:

* **Observation.** One root, one opened directory descriptor, and the three
  facts taken from it in one moment — `st_dev`, `st_ino` and the canonical real
  path bound to that inode. Never from the assignment string, which is exactly
  the thing that can be a symlink to somewhere else.
* **The two checks.** Identity `(st_dev, st_ino)`, and containment as a
  *component-wise* ancestor test over the two canonical real paths in both
  directions. Neither implies the other; see `relation_between`.
* **The snapshot.** An immutable mapping from user id to a structured reason,
  carrying the facts as observed, published by one assignment under a
  process-global lock, monotonic in a sequence number.

Read `docs/architecture/vault-roots-and-tenancy.md` before changing any of it.

**What this module does NOT detect,** written out because a reader who assumes
otherwise will trust it too far: a bind mount that grafts one tenant's vault —
or any mount nested inside it — to a path *inside* another tenant's root. Both
root inodes stay distinct and both canonical real paths stay outside each other,
so neither check sees it, and the consequence is a full cross-tenant read,
overwrite and delete. That is accepted limitation **L1** (with **L2**, an
accessible alias of a root that could not be examined, as the same class), and
it goes to the follow-up issue `vault-root-mount-graft-detection`. There is
deliberately **no `/proc/self/mountinfo` check here**: it was specified and
removed after three review rounds each produced a new mount configuration it
failed to cover, which is the signal that it was a heuristic pretending to be a
rule — and it would have sat on the admission gate for two live tenants.
"""

import asyncio
import datetime
import errno as errno_module
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Iterable, Mapping

from src.config import settings

logger = logging.getLogger(__name__)


# ── Relations and causes ────────────────────────────────────────────────────

#: The two roots are one directory object — same superblock, same inode.
RELATION_IDENTICAL = "identical"
#: The subject's root canonically contains the peer's.
RELATION_CONTAINS = "contains"
#: The subject's root is canonically inside the peer's.
RELATION_CONTAINED_BY = "contained_by"

_INVERSE_RELATION = {
    RELATION_IDENTICAL: RELATION_IDENTICAL,
    RELATION_CONTAINS: RELATION_CONTAINED_BY,
    RELATION_CONTAINED_BY: RELATION_CONTAINS,
}

#: `RootUnexaminable.cause` when the observation exceeded its deadline. Distinct
#: from an `errno` because a hung mount and a missing directory are different
#: incidents and an operator does different things about them.
CAUSE_TIMEOUT = "timeout"
#: `RootUnexaminable.cause` when the root opened but its canonical real path did
#: not name the inode that was opened — the pathname is moving under us, so
#: nothing observed describes one directory. Also a "could not look" verdict,
#: never an overlap.
CAUSE_UNSTABLE = "unstable"


def canonical_assignment(path: "str | Path") -> str:
    """A vault assignment normalised exactly as `_vault_root` yields it.

    Delegates to `transfer.canonical_vault_root` (imported at call time — the
    transfer module imports `vault`, and `vault` imports this one) so there is
    one definition of the normal form the panel, the transfer gate and this
    detector all compare.
    """
    from src.services.transfer import canonical_vault_root

    return canonical_vault_root(path)


# ── Observation ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RootObservation:
    """What one moment's look at one vault root established.

    `st_dev`, `st_ino` and `realpath` are all None exactly when `cause` is set:
    an observation either produced the three facts together or produced a reason
    it could not. There is no half-observed state, because a check run over half
    of one would be a check over a directory nobody looked at.
    """

    assignment: str
    user_id: int | None = None
    username: str | None = None
    st_dev: int | None = None
    st_ino: int | None = None
    realpath: str | None = None
    cause: int | str | None = None

    @property
    def examinable(self) -> bool:
        """True when the three facts were taken; False when a cause was recorded."""
        return self.cause is None


def observe_root_blocking(
    assignment: str,
    *,
    user_id: int | None = None,
    username: str | None = None,
) -> RootObservation:
    """Open one root and take `(st_dev, st_ino, realpath)` from the descriptor.

    `O_RDONLY | O_DIRECTORY | O_CLOEXEC`, so a non-directory is refused by the
    kernel rather than by a `stat` race, and no descriptor leaks across an
    `exec`. **The descriptor is the source of every fact and is closed on every
    exit path** — nothing downstream needs it open, and holding one across the
    awaited pairwise phase would be a leak proportional to the tenant count.

    The real path is bound to the descriptor the way
    `indexer.observe_root_facts` binds it: `os.stat(os.path.realpath(...))` must
    report the same `(st_dev, st_ino)` as `os.fstat(fd)`. A disagreement is not
    a mismatch to report — it means the pathname moved between the two calls, so
    neither fact describes one directory — and it yields `CAUSE_UNSTABLE`.

    A failure is a **verdict, not an exception**: the `errno` is returned in
    `cause`. An unopenable root is never an overlap and names no peer.

    Blocking on purpose: `os.open`, `os.fstat` and `os.path.realpath` on a
    network- or FUSE-backed bind mount can sit in the kernel for minutes. Call
    it through `observe_root`, which dispatches it off the event loop under a
    deadline.
    """
    canonical = canonical_assignment(assignment)
    fd: int | None = None
    try:
        fd = os.open(canonical, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        pinned = os.fstat(fd)
        realpath = os.path.realpath(canonical)
        named = os.stat(realpath)
    except OSError as exc:
        return RootObservation(
            assignment=canonical,
            user_id=user_id,
            username=username,
            cause=exc.errno if exc.errno is not None else errno_module.EIO,
        )
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:  # pragma: no cover - close of a valid fd
                logger.warning("failed to close vault-root descriptor", exc_info=True)
    if (named.st_dev, named.st_ino) != (pinned.st_dev, pinned.st_ino):
        return RootObservation(
            assignment=canonical,
            user_id=user_id,
            username=username,
            cause=CAUSE_UNSTABLE,
        )
    return RootObservation(
        assignment=canonical,
        user_id=user_id,
        username=username,
        st_dev=pinned.st_dev,
        st_ino=pinned.st_ino,
        realpath=realpath,
    )


async def observe_root(
    assignment: str,
    *,
    user_id: int | None = None,
    username: str | None = None,
    timeout: float | None = None,
) -> RootObservation:
    """`observe_root_blocking` off the event loop, under a finite deadline.

    `VAULT_ROOT_OBSERVE_TIMEOUT_SECONDS` (default 10) bounds it. Expiry is a
    **per-user verdict** — `RootUnexaminable(CAUSE_TIMEOUT)` — and never a
    failure of the detection as a whole: the remaining roots are still observed
    and the snapshot is still published. Treating it as a detection failure
    would retain the previous snapshot on every iteration a slow mount was slow,
    so an overlap appearing later would never be published.

    The bound is what makes the startup detection safe to run *synchronously*
    before the application serves. Without it, one hung mount holds the process
    before its first request and takes the control panel down at exactly the
    moment an operator opens it to find out why.

    **The deadline abandons the wait, not the syscall.** A Python thread blocked
    in `open(2)` cannot be cancelled; it stays parked until the filesystem
    answers or the process ends, so a pathological mount accumulates one thread
    per detection. That is accepted limitation **L4**: the thread holds no lock
    and no pooled connection, the condition is loud (the user is quarantined and
    named on the panel), and the alternative is a server that will not start.
    """
    if timeout is None:
        timeout = float(settings.vault_root_observe_timeout_seconds)
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(
                observe_root_blocking,
                assignment,
                user_id=user_id,
                username=username,
            ),
            timeout,
        )
    except (asyncio.TimeoutError, TimeoutError):
        return RootObservation(
            assignment=canonical_assignment(assignment),
            user_id=user_id,
            username=username,
            cause=CAUSE_TIMEOUT,
        )


# ── The two checks ──────────────────────────────────────────────────────────


def _components(realpath: str) -> tuple[str, ...]:
    """The path's components. `realpath` is already absolute and normalised."""
    return PurePosixPath(realpath).parts


def contains_path(ancestor: str, descendant: str) -> bool:
    """True when `descendant` lies strictly inside `ancestor`, component-wise.

    **Never a string prefix comparison.** `/vaults/team` is not an ancestor of
    `/vaults/team-2`, and a raw `startswith` says it is — which would refuse an
    assignment that overlaps nothing and quarantine two healthy tenants, the
    false-positive direction this codebase treats as the expensive failure.

    Strict: a path does not contain itself. Identity is check 1's job and is
    reported as its own relation, so that the exact-duplicate wording operators
    already know is still selectable.
    """
    a = _components(ancestor)
    d = _components(descendant)
    return len(d) > len(a) and d[: len(a)] == a


def roots_identical(a: RootObservation, b: RootObservation) -> bool:
    """**Check 1 — identity.** One directory object: same superblock, same inode.

    Catches a symlink alias, a same-filesystem bind mount of one directory to
    two pathnames, and a directory hard link where the kernel permits one — the
    aliases string equality cannot see, because the two strings differ.

    It proves **nothing about containment**: two distinct inodes nest all the
    time, which is check 2's job. And `st_dev` on its own is unsound in both
    directions — a bind mount of a subtree reports the same device while being a
    different mount, and a filesystem mounted *inside* another tenant's root
    gives total overlap across two different devices — which is why this
    compares the pair and never the device alone.
    """
    if not (a.examinable and b.examinable):
        return False
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


def relation_between(a: RootObservation, b: RootObservation) -> str | None:
    """The relation `a` bears to `b`, or None when the two roots do not collide.

    This is the **one** predicate for "do these roots collide" — the panel's
    assignment-time refusal and the periodic detection both call it, because two
    functions answering the same question is how the two drift apart.

    It takes the two canonical **assignment strings** alongside the observed
    facts (both live on `RootObservation`) so that an exactly duplicated
    assignment reports `identical` even when neither root could be opened, and
    the caller can select the exact-duplicate wording operators already know
    instead of describing an equal pair as a containment.

    Order of the two checks is not arbitrary: identity is reported in preference
    to containment because an identical pair satisfies neither containment
    direction and would otherwise fall through as "no relation".
    """
    if a.assignment == b.assignment:
        return RELATION_IDENTICAL
    if roots_identical(a, b):
        return RELATION_IDENTICAL
    if not (a.examinable and b.examinable):
        # Nothing was observed to compare. An unexaminable root is its own
        # verdict (`RootUnexaminable`) and is never reported as an overlap.
        return None
    if contains_path(a.realpath, b.realpath):
        return RELATION_CONTAINS
    if contains_path(b.realpath, a.realpath):
        return RELATION_CONTAINED_BY
    return None


def inverse_relation(relation: str) -> str:
    """The relation the peer bears to the subject."""
    return _INVERSE_RELATION[relation]


# ── Reasons and the snapshot ────────────────────────────────────────────────


@dataclass(frozen=True)
class Overlap:
    """This root and that user's root are the same directory, or nested.

    Carries the peer's identity and canonical assignment **as observed**, not a
    reference to a `users` row: the operator's first move on reading "vault root
    overlaps <peer>'s" is to edit or delete one of the two accounts, and a
    surface that resolved names at render time would show a changed path — or a
    blank, where a deleted peer was — beside a condition still in force.
    """

    peer_user_id: int
    peer_username: str
    peer_assignment: str
    relation: str


@dataclass(frozen=True)
class RootUnexaminable:
    """The root could not be opened, so no overlap could be ruled out.

    **Not an overlap, and it names no peer** — none was observed. Calling it one
    would send an operator looking for a second account that does not exist.
    `cause` is an `errno`, `CAUSE_TIMEOUT` or `CAUSE_UNSTABLE`.

    It quarantines **only this user**. The peers it could not be compared
    against keep being served: fail closed for the user whose status is unknown,
    fail open for users against whom nothing was observed, so one broken mount
    does not take the deployment offline.
    """

    cause: int | str


Reason = Overlap | RootUnexaminable


@dataclass(frozen=True)
class QuarantineEntry:
    """One quarantined user, and the facts as they stood at detection time.

    Immutable, and the surfaces render *these* rather than re-reading `users` —
    which is also what makes the staleness honest, because a surface can label
    them "as at last check" rather than presenting them as the present state.
    """

    user_id: int
    username: str
    assignment: str
    reason: Reason
    detected_at: datetime.datetime


@dataclass(frozen=True)
class QuarantineSnapshot:
    """One complete detection result. Immutable, and published by one assignment.

    `sequence` is assigned when the detection *begins*, inside the detection
    lock, and `publish` drops a snapshot whose sequence is not greater than the
    published one. The lock is the mechanism; the sequence is the invariant, and
    the invariant is what stays true for a future caller — a test, a fixture, an
    entry point added later — that publishes without taking the lock.
    """

    sequence: int
    detected_at: datetime.datetime
    entries: Mapping[int, QuarantineEntry] = field(
        default_factory=lambda: MappingProxyType({})
    )

    def reason_for(self, user_id: int | None) -> Reason | None:
        """This user's quarantine reason, or None when the snapshot admits them."""
        if user_id is None:
            return None
        entry = self.entries.get(user_id)
        return None if entry is None else entry.reason

    def entry_for(self, user_id: int | None) -> QuarantineEntry | None:
        """This user's full recorded facts, or None when they are not named."""
        if user_id is None:
            return None
        return self.entries.get(user_id)

    def names(self, user_id: int | None) -> bool:
        """True when the snapshot quarantines this user."""
        return self.entry_for(user_id) is not None


def _snapshot(
    sequence: int,
    detected_at: datetime.datetime,
    entries: Iterable[QuarantineEntry],
) -> QuarantineSnapshot:
    return QuarantineSnapshot(
        sequence=sequence,
        detected_at=detected_at,
        entries=MappingProxyType({entry.user_id: entry for entry in entries}),
    )


# ── Publication ─────────────────────────────────────────────────────────────

# The tri-state. `None` is **never published**, which is a refusal and not an
# all-clear: between the first accepted connection and the first published
# snapshot a tool call would be served against roots nothing had checked, and a
# first detection that *raised* would leave the process permissive for the life
# of the container. The other two states are "published and empty" (everything
# was checked and nothing overlaps) and "published with reasons".
_published: QuarantineSnapshot | None = None

# Serializes the WHOLE operation — observation, the checks and the publication
# in one critical section, not the publication alone. Every entry point calls
# the detection *before* taking `index_pass_lock` (correctly: the check must not
# queue behind the pass it exists to gate), so a periodic tick and a
# panel-triggered reindex overlap trivially. Holding this only across the
# publication leaves exactly the interleaving that fails **open**: a detection
# that began before an overlap appeared, stalled on a slow `open`, and finished
# last would publish its own *empty* result over the newer quarantine and
# re-admit both tenants. Atomicity of the swap does not help — both writes are
# individually atomic and the wrong one is last.
_lock = asyncio.Lock()

_next_sequence = 0


def _take_sequence() -> int:
    """The next sequence number. Called under `_lock` by the detection."""
    global _next_sequence
    _next_sequence += 1
    return _next_sequence


def published_snapshot() -> QuarantineSnapshot | None:
    """The published snapshot, or None when none has been published here.

    One attribute read: no session, no statement, no syscall. This is what
    `vault._vault_root` consults on the hot path of every tool call.
    """
    return _published


def is_published() -> bool:
    """True once a snapshot has been published in this process."""
    return _published is not None


def publish(snapshot: QuarantineSnapshot) -> bool:
    """Install `snapshot` unless an equal-or-newer one is already published.

    Returns whether it was installed. **Monotonic in the sequence number**, so
    an older detection's result is dropped rather than published, and
    **atomic**: one immutable object swapped in by a single assignment, so no
    reader ever observes a half-built snapshot.
    """
    global _published
    current = _published
    if current is not None and snapshot.sequence <= current.sequence:
        logger.warning(
            "vault-root snapshot seq=%s dropped: not newer than the published "
            "seq=%s",
            snapshot.sequence,
            current.sequence,
        )
        return False
    _published = snapshot
    return True


def publish_synthetic_snapshot(
    entries: Iterable[QuarantineEntry] = (),
    *,
    detected_at: datetime.datetime | None = None,
) -> QuarantineSnapshot:
    """Publish a snapshot without running a detection. **Tests and fixtures.**

    The suite's autouse fixture publishes an empty one so the readiness refusal
    does not turn every multi-user test into a vault-unavailable failure, and
    slices that need a quarantined caller publish a synthetic reason rather than
    arranging a real overlap on disk.

    It takes the next sequence **without** the detection lock, which is the
    point: the sequence guard has to hold for a caller that publishes outside
    the critical section, and this is that caller.
    """
    snapshot = _snapshot(
        _take_sequence(),
        detected_at or datetime.datetime.now(datetime.timezone.utc),
        entries,
    )
    publish(snapshot)
    return snapshot


def reset_snapshot_state() -> None:
    """Return this process to the never-published state. **Tests only.**

    Production has no un-publish: clearing a published snapshot back to
    never-published is explicitly forbidden, because a transient database blip
    must not become a deployment-wide refusal.

    It also replaces the detection lock. An `asyncio.Lock` binds itself to the
    first event loop that awaits it and refuses every other one, and the suite
    gives each test its own loop — so the one process-global lock the design
    requires would be a *stale* lock from the second test onward. Production has
    one event loop per process (uvicorn's, and the rebuild script's own), so
    nothing there ever needs this; it is the test door, and it is here rather
    than in a fixture so that the reset is one operation nobody can half-do.
    """
    global _published, _next_sequence, _lock
    _published = None
    _next_sequence = 0
    _lock = asyncio.Lock()


# ── Detection ───────────────────────────────────────────────────────────────


async def _active_assignments(session_factory) -> list[tuple[int, str, str]]:
    """`(id, username, vault_path)` for every active user holding an assignment.

    The same population `indexer._active_user_ids` passes over, and the same one
    the assignment-time check queries: a user who is inactive or unassigned can
    create no overlap, because nothing serves or indexes them.
    """
    from sqlalchemy import select

    from src.models.db import User

    async with session_factory() as session:
        result = await session.execute(
            select(User.id, User.username, User.vault_path)
            .where(User.is_active.is_(True), User.vault_path.isnot(None))
            .order_by(User.id)
        )
        return [(row.id, row.username, row.vault_path) for row in result.all()]


async def _detect(sequence: int, session_factory) -> QuarantineSnapshot:
    """Observe every active assigned root once and evaluate the pairs."""
    detected_at = datetime.datetime.now(datetime.timezone.utc)

    if settings.mcp_sandbox_mode:
        # Sandbox mode has no users and skips the indexer, and it must still be
        # *ready* — otherwise every registered tool would refuse for a reason
        # the sandbox cannot fix. No filesystem is touched.
        return _snapshot(sequence, detected_at, ())

    rows = await _active_assignments(session_factory)
    if not rows:
        # Single-user mode reaches here too: `_active_user_ids` is empty there,
        # so the snapshot is empty and every pass behaves exactly as today.
        return _snapshot(sequence, detected_at, ())

    observations = await asyncio.gather(
        *(
            observe_root(assignment, user_id=user_id, username=username)
            for user_id, username, assignment in rows
        )
    )

    entries: dict[int, QuarantineEntry] = {}
    for observation in observations:
        if observation.examinable:
            continue
        entries[observation.user_id] = QuarantineEntry(
            user_id=observation.user_id,
            username=observation.username,
            assignment=observation.assignment,
            reason=RootUnexaminable(observation.cause),
            detected_at=detected_at,
        )

    examinable = [o for o in observations if o.examinable]
    for i, a in enumerate(examinable):
        for b in examinable[i + 1 :]:
            relation = relation_between(a, b)
            if relation is None:
                continue
            entries.setdefault(
                a.user_id,
                QuarantineEntry(
                    user_id=a.user_id,
                    username=a.username,
                    assignment=a.assignment,
                    reason=Overlap(b.user_id, b.username, b.assignment, relation),
                    detected_at=detected_at,
                ),
            )
            entries.setdefault(
                b.user_id,
                QuarantineEntry(
                    user_id=b.user_id,
                    username=b.username,
                    assignment=b.assignment,
                    reason=Overlap(
                        a.user_id,
                        a.username,
                        a.assignment,
                        inverse_relation(relation),
                    ),
                    detected_at=detected_at,
                ),
            )

    return _snapshot(sequence, detected_at, entries.values())


async def detect_and_publish(session_factory=None) -> QuarantineSnapshot | None:
    """Detect vault-root overlaps and publish the result. **The only detection.**

    Every code path that can begin a pass over a vault root calls this before it
    reads a byte — the lifespan (synchronously, before the app serves), the
    indexer's startup block, each periodic tick, the panel's on-demand reindex,
    and the standalone tsvector rebuild, which is a separate process publishing
    and consuming its own snapshot. The rule the specs pin is not "call it in
    these five places" but *no pass begins without a snapshot published in this
    process by this routine*.

    Issues **no database write**. The quarantine is derived from the filesystem
    at every entry point; persisting it would create a second source of truth
    that can disagree with the directory it describes.

    Failure handling, which is the part with two different right answers:

    * **A snapshot already stands** → it is retained, the failure is logged at
      ERROR, and the retained snapshot is returned. It is emphatically NOT
      cleared back to never-published: a transient database blip must not become
      a deployment-wide refusal, and a stale snapshot of a condition that
      persists until an operator acts is the better of the two errors.
    * **Nothing has been published yet** → the exception propagates, so the
      caller can log it, and the gate stays in the never-published state, which
      refuses. A failed first detection must not become an all-clear.

    A root that cannot be opened is a *per-user verdict*, not a failure of this
    routine; the only way this raises is that the user enumeration failed, which
    means the database is unavailable and the tools cannot serve anyway. So the
    process keeps serving the panel and retries at the next entry point rather
    than exiting — the same partial-capability posture
    `_check_mount_identity_support` takes.
    """
    if session_factory is None:
        from src.database import async_session

        session_factory = async_session

    async with _lock:
        sequence = _take_sequence()
        try:
            snapshot = await _detect(sequence, session_factory)
        except Exception:
            retained = _published
            if retained is None:
                logger.error(
                    "vault-root overlap detection failed and no snapshot has "
                    "been published; every multi-user tool call stays refused "
                    "until a later entry point publishes one",
                    exc_info=True,
                )
                raise
            logger.error(
                "vault-root overlap detection failed; retaining the snapshot "
                "published at %s (seq=%s)",
                retained.detected_at.isoformat(),
                retained.sequence,
                exc_info=True,
            )
            return retained
        publish(snapshot)
        _log_snapshot(snapshot)
        return snapshot


def _log_snapshot(snapshot: QuarantineSnapshot) -> None:
    """One ERROR line per quarantined user, so the ops-health ring buffer sees it.

    The log is only half the record — it reaches a 100-entry, process-lifetime
    buffer while the misconfiguration survives restarts — which is why the pass
    also writes the reason to the affected user's `indexer_runs` row.
    """
    for entry in snapshot.entries.values():
        logger.error("vault root quarantine: %s", operator_text(entry))


# ── Wordings ────────────────────────────────────────────────────────────────
#
# The operator surfaces — the panel, the log line, the `indexer_runs` row and
# the assignment refusal — name everything: both accounts, both roots, the
# relation and the cause. The *agent*-facing refusals name nothing at all; those
# wordings live beside the exception types in `src/services/vault.py`, because
# the caller of a tool is a tenant's agent and must not learn another tenant's
# username, path or notes exist.


def cause_text(cause: int | str) -> str:
    """An `errno`, a timeout or an unstable pathname, in one operator-facing phrase."""
    if cause == CAUSE_TIMEOUT:
        return (
            f"the observation exceeded "
            f"{settings.vault_root_observe_timeout_seconds:g}s (the mount is "
            "not answering)"
        )
    if cause == CAUSE_UNSTABLE:
        return (
            "its real path no longer named the directory that was opened (the "
            "pathname is changing under the check)"
        )
    if isinstance(cause, int):
        name = errno_module.errorcode.get(cause, str(cause))
        return f"{name} ({os.strerror(cause)})"
    return str(cause)


def relation_text(relation: str) -> str:
    """The relation as an operator reads it, subject-first."""
    if relation == RELATION_IDENTICAL:
        return "is the same directory as"
    if relation == RELATION_CONTAINS:
        return "contains"
    if relation == RELATION_CONTAINED_BY:
        return "is inside"
    return relation


def assignment_conflict_message(
    normalized: str,
    relation: str,
    peer_username: str,
) -> str:
    """The panel's refusal for an assignment that collides with a peer's root.

    Exported so the assignment-time check does not re-implement the
    relation-to-message mapping — one implementation of "do these roots
    collide", and one of how to say so.

    **The `identical` wording is the message operators already know** and is
    preserved verbatim from the string-equality check this predicate subsumes.
    The other two name the relation, because "already assigned" would be false
    for them and would send an operator looking for a duplicate string.
    """
    if relation == RELATION_IDENTICAL:
        return f"Vault path '{normalized}' is already assigned to user '{peer_username}'."
    if relation == RELATION_CONTAINS:
        return (
            f"Vault path '{normalized}' contains the vault of user "
            f"'{peer_username}'. Two vault roots may not overlap: every tool "
            "and the indexer would reach across both accounts."
        )
    return (
        f"Vault path '{normalized}' is inside the vault of user "
        f"'{peer_username}'. Two vault roots may not overlap: every tool and "
        "the indexer would reach across both accounts."
    )


def peer_unexaminable_message(peer_assignment: str, cause: int | str) -> str:
    """The panel's refusal when a *peer* root could not be opened.

    It reports what was observed and no more: the overlap could not be ruled
    out, which is not the same as an overlap that was seen. Admitting on "we
    could not look" is the direction this codebase treats as the expensive
    error — identity is precisely the check that catches what string equality
    already misses — and `validate_vault_root_path` sets the precedent by
    refusing the *candidate* for the same missing-mount case.
    """
    return (
        f"Vault path '{peer_assignment}', assigned to another active user, "
        f"could not be examined: {cause_text(cause)}. The assignment is "
        "refused because an overlap with it could not be ruled out."
    )


def operator_text(entry: QuarantineEntry) -> str:
    """One line naming the account, its root and why it is not served.

    For the log, the `indexer_runs` row and the panel. Names everything,
    deliberately: the operator has to fix it, and the two reasons are worded
    apart because an operator investigating a misconfiguration and an operator
    investigating a missing mount do different things.
    """
    reason = entry.reason
    if isinstance(reason, Overlap):
        return (
            f"user '{entry.username}' (id={entry.user_id}) is not served: its "
            f"vault root '{entry.assignment}' {relation_text(reason.relation)} "
            f"the vault root '{reason.peer_assignment}' of user "
            f"'{reason.peer_username}' (id={reason.peer_user_id}), as at "
            f"{entry.detected_at.isoformat()}. The index is retained."
        )
    return (
        f"user '{entry.username}' (id={entry.user_id}) is not served: its vault "
        f"root '{entry.assignment}' could not be examined — "
        f"{cause_text(reason.cause)} — so no overlap could be ruled out, as at "
        f"{entry.detected_at.isoformat()}. No peer was observed. The index is "
        "retained."
    )
