"""Vault filesystem service: note/file reads, and every mutation entry point.

**Reads follow symlinks; mutations do not, and mutations are anchored.**

A read (`read_file`, `read_bytes`, `list_dir`) resolves the pathname and acts on
what it finds — an alias reading as its target is what a user expects from an
alias, and a read cannot destroy anything.

A mutation goes through `open_mutable`, which resolves the parent once, opens it
as a descriptor, refuses a symlinked final component, and hands back a
`MutableTarget`. Every syscall the mutation then makes — the temp create, the
`expected=` read, the publish, the soft delete — runs against that descriptor.
Nothing re-resolves a pathname, so neither a repointed ancestor symlink nor a
renamed parent directory can redirect a write mid-call. See `MutableTarget`.

**The residual, precisely.** With #59 closed there is no window left in which a
mutation can be sent to a directory the guard did not open: the parent is
resolved and opened before the first byte moves, and a directory descriptor
keeps naming the same directory however its pathname is renamed or relinked
afterwards. What remains is not redirection but *substitution at the leaf*,
inside the directory the caller named:

- the leaf can be swapped for a symlink between the guard's `lstat` and the
  read or write. `O_NOFOLLOW` turns that into `ELOOP`, which the tools report;
  the link is never followed and nothing outside the named directory is touched.
- an adversary who can write to the **destination directory itself** can still
  win the `renameat` race on an overwrite publish. That adversary can also just
  edit the note directly, so it is outside the threat #59 addresses —
  redirection through an *ancestor* or the *root*, where the attacker never had
  access to the destination at all. The window is narrowed to that one syscall
  by an identity check (`_require_staged_name`). The no-clobber publish has no
  such window at all: it stages into an unnamed `O_TMPFILE` inode and publishes
  it through `/proc/self/fd`, so there is no staging name to substitute and
  none to clean up. (`VAULT_ALLOW_NAMED_STAGING_FALLBACK` is the declared
  exception: on a filesystem that rejects `O_TMPFILE`, opting in reopens this
  window for no-clobber writes too — see `_link_staged_name`.)
- **creating a missing parent has no beneath-root form** (#87, D22). The lookup
  itself is now one `openat2(RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS |
  RESOLVE_NO_MAGICLINKS)`, so there is no interval between components for a
  rename to exploit — but `mkdirat` has no such form, and no syscall creates a
  directory *and* proves the path it created it under stayed beneath a root.
  `MutableTarget.ensure_parent` therefore descends one component at a time,
  carrying **no** descriptor across a creation: each `mkdirat` goes through a
  fresh beneath-root lookup of the prefix that already exists and that
  descriptor is dropped at once, and the descriptor the write anchors to comes
  from a fresh lookup of the whole parent performed afterwards. What a race can
  still cost is at most one **empty** directory per component, per creation
  descent (a note write performs one), in a place the renaming process already
  controls — never a note, never note content, and never something the tool
  reports success about. It is not cleaned up: an `rmdir` by name is the same
  delete-the-substitute hazard `_discard_temp` refuses.
- **a lookup proves containment when it resolves, not afterwards** (#87, D26).
  A directory descriptor keeps naming the same directory however its pathname
  is later renamed — which is exactly the property this design depends on, so
  that a mutation lands in the directory that was validated rather than in a
  substitute left at its name. The other side of it: a process that can rename
  a vault ancestor can move the resolved parent out of the vault between
  `open_mutable` and the publish, and the note then lands there while the tool
  reports success for the path the caller named. Nothing was *redirected* — the
  bytes went to the directory the caller named, which somebody else moved — and
  excluding it would need an operation the kernel does not offer. Retained, not
  introduced: the per-component walk had this interval too, underneath the
  larger window it did not close.
- a read-modify-write overwrite (`edit_note`, `set_frontmatter`, `move_note`'s
  link rewrites) is optimistic, not linearizable: `expected=` compares the
  current bytes immediately before the rename, and a writer that lands inside
  that window is still overwritten.
- `write_file(overwrite=True)` — the raw byte tool — has no conflict detection
  at all and is an unconditional replace: it takes whole-file content from the
  caller, so there is no prior read to compare against and `expected=` is never
  passed. `request_upload(overwrite=True)` is the path that binds to the
  incumbent's fingerprint.
- the no-clobber publish (`create_note`, `write_file` by default) has no window
  — it is `link()`, which the kernel makes atomic — and neither does the soft
  delete or `move_note`'s own publication, which are one
  `renameat2(RENAME_NOREPLACE)`.

These are declared limits — of optimistic concurrency for the leaf ones, of
what the kernel offers for the last two — not open holes. Every below-root
directory descriptor a call uses as a pathname anchor comes from a lookup the
kernel proved beneath the vault root at the moment it resolved, and no
directory descriptor retained from a creation descent is ever returned to a
caller or used as a pathname anchor — so no operation is ever redirected into
a directory that was never beneath the root. The write lands on the path the
caller named, in the directory that was validated.
"""

import contextlib
import errno
import inspect
import logging
import mimetypes
import os
import posixpath
import re
import stat
import uuid
from pathlib import Path, PurePosixPath

import yaml
from sqlalchemy import select

from src.auth.session import _UnsetVaultRoot, current_vault_root
from src.config import settings
from src.services import vault_fs

logger = logging.getLogger(__name__)


# Process-level cache of `user_id -> Path(user.vault_path)` for multi-user mode.
# Populated via `warm_user_vault_cache(session, ...)` and invalidated by
# `clear_user_vault_cache(user_id=...)` when the admin edits a user. Single-user
# mode never touches this cache because `_vault_root()` is called with
# `user_id=None` everywhere.
#
# The single-user form of `warm_user_vault_cache` is *authoritative*: it writes
# the current value or removes the entry, and returns what it read. It is not an
# add-only warm. The bulk form still is — which is why the value it returns is
# also bound to the request (`current_vault_root`) rather than trusted from this
# dict: see `_vault_root`.
_user_vault_cache: dict[int, Path] = {}


async def warm_user_vault_cache(session, user_id: int | None = None) -> Path | None:
    """Populate `_user_vault_cache` for one user (or every active user).

    Called by the indexer at the start of each multi-user pass, by the API-key
    middleware after authenticating a user, and (in phase 4) by panel routes
    before they hit vault tools. In single-user mode the cache is unused so
    callers can skip the warmup; nothing breaks if they don't.

    The **single-user form returns the freshly read root, or None** when the
    user has no usable assignment. `APIKeyMiddleware` binds that answer to the
    request via `current_vault_root`, which is what makes admission immune to
    the bulk warm racing it — see `_vault_root`. The bulk form returns None.
    """
    from src.models.db import User

    if user_id is not None:
        result = await session.execute(
            select(User.id, User.vault_path).where(
                User.id == user_id,
                User.is_active.is_(True),
                User.vault_path.isnot(None),
            )
        )
        row = result.first()
        if row is None:
            # No usable assignment any more: unassigned, deactivated, or the
            # row is gone. **Evict** rather than leaving the previous value in
            # place. This warm runs on every authenticated MCP request, and
            # `_vault_root` is now the admission gate every tool passes through
            # (`_tracked`), so a stale entry would keep a revoked assignment
            # queryable until the process restarts — issue #66. Callers that
            # mutate `users.vault_path` still call `clear_user_vault_cache`;
            # this makes the refusal independent of them, and of the fact that
            # each worker process holds its own cache.
            _user_vault_cache.pop(user_id, None)
            return None
        root = Path(row.vault_path)
        _user_vault_cache[row.id] = root
        return root

    result = await session.execute(
        select(User.id, User.vault_path).where(
            User.is_active.is_(True),
            User.vault_path.isnot(None),
        )
    )
    for row in result.all():
        _user_vault_cache[row.id] = Path(row.vault_path)
    return None


def clear_user_vault_cache(user_id: int | None = None) -> None:
    """Drop one user (or every user) from the in-process vault-path cache.

    Phase 4's admin user-edit endpoint calls this whenever it mutates
    `users.vault_path` so the next vault op picks up the new value. With no
    argument, clears the whole cache (useful for tests).
    """
    if user_id is None:
        _user_vault_cache.clear()
    else:
        _user_vault_cache.pop(user_id, None)


UNOWNED_IN_MULTI_USER_ERROR = (
    "No vault root: multi-user mode is enabled and this credential is not "
    "bound to a user."
)


def vault_unassigned_error(user_id: int) -> str:
    """The one wording for "this user has no usable vault assignment".

    Shared by `_vault_root` and the panel's vault browser so the operator and
    the agent are told the same thing.
    """
    return (
        f"Vault path for user_id={user_id} is not assigned. The user has no "
        "`vault_path`, or is inactive."
    )


def _vault_root(user_id: int | None = None) -> Path:
    """Return the vault root for the given user.

    Single-user mode / `user_id is None` → `settings.vault_path` (legacy
    behavior). **In multi-user mode `user_id is None` raises instead** — see
    the ownerless-credential note below. Multi-user mode → cached
    `users.vault_path` lookup. The cache
    must have been warmed for this user (auth middleware / indexer / panel
    routes do this before invoking tools); a miss raises a clear RuntimeError
    rather than silently falling back to the global path or silently blocking
    the event loop on a sync DB call.

    This is also the admission gate for every MCP tool: `_tracked` calls it
    once before the tool body runs and refuses the call when it raises, so a
    user with no vault assignment cannot reach the database-backed tools
    either (issue #66). Keep it a pure cache lookup — the per-request warm in
    `APIKeyMiddleware` is what makes it correct, and a DB query here would be
    a query on every tool call.

    **The authenticated request's own snapshot wins over the shared dict.**
    `_user_vault_cache` is process-global and the indexer's bulk warm writes to
    it, so a bulk `SELECT` issued *before* an admin cleared `vault_path` can
    land *after* the per-request warm evicted the entry and re-admit a user
    whose assignment was already revoked — mid-call, with a write tool in
    flight. `current_vault_root` is bound once per authenticated request by
    `APIKeyMiddleware` and no other task can overwrite it, so consulting it
    first makes admission fail closed under that interleaving. It is keyed by
    user id: a snapshot for a different user (nested contexts, a panel route
    resolving somebody else's root) falls through to the dict rather than
    answering for the wrong vault.
    """
    if user_id is None:
        if settings.multi_user_mode:
            # An ownerless credential in multi-user mode. `APIKeyMiddleware`
            # already refuses those (see `ownerless_credential`), so reaching
            # here means some other path resolved a root with no user — and
            # falling back to `settings.vault_path` would hand it the *global*
            # vault, which in multi-user mode belongs to nobody in particular
            # and is exactly the write nobody authorised. Fail closed.
            raise RuntimeError(UNOWNED_IN_MULTI_USER_ERROR)
        return Path(settings.vault_path)
    snapshot = current_vault_root.get()
    if not isinstance(snapshot, _UnsetVaultRoot) and snapshot[0] == user_id:
        root = snapshot[1]
        if root is None:
            raise RuntimeError(vault_unassigned_error(user_id))
        return root
    cached = _user_vault_cache.get(user_id)
    if cached is None:
        raise RuntimeError(
            f"Vault path for user_id={user_id} is not in cache. "
            "Call `warm_user_vault_cache(session, user_id)` before using "
            "vault tools, or check that the user has `vault_path` set and "
            "`is_active=True`."
        )
    return cached


def validate_vault_root_path(p: str) -> tuple[str | None, str | None]:
    """Validate an absolute container path as an acceptable vault root.

    Returns ``(normalized_path, error)`` — at most one of the two is set.
    Empty / None input → ``(None, None)``: callers that allow clearing a
    vault_path assignment treat this as "no change".

    Accepted values:
    - ``settings.vault_path`` (the legacy single-user ``/obsidian`` mount)
    - Any non-empty subpath of ``/vaults/``

    The ``/vaults/`` restriction prevents an admin from accidentally (or
    deliberately) pointing a user at ``/etc``, the host home dir, or another
    container path that happens to be visible.  The directory-existence check
    catches a docker-compose mount that was configured but not yet applied.
    """
    raw = (p or "").strip()
    if not raw:
        return None, None
    if ".." in Path(raw).parts:
        return None, "Vault path may not contain '..' traversal."
    normalized = os.path.normpath(raw)
    legacy = settings.vault_path.rstrip("/")
    if normalized != legacy and not normalized.startswith("/vaults/"):
        return None, (
            f"Vault path must be either '{legacy}' (legacy mount) or a "
            "subpath of '/vaults/'."
        )
    if not Path(normalized).is_dir():
        return None, (
            f"Vault path '{normalized}' does not exist as a directory "
            "inside the container. Check the docker-compose volume mount."
        )
    return normalized, None


def validate_path(relative_path: str, user_id: int | None = None) -> Path:
    """Resolve a relative path within the vault, preventing traversal."""
    vault = _vault_root(user_id)
    resolved = (vault / relative_path).resolve()
    try:
        resolved.relative_to(vault.resolve())
    except ValueError:
        raise ValueError(f"Path traversal denied: {relative_path}")
    return resolved


def is_hidden_path(rel: "str | Path") -> bool:
    """True if any component of a vault-relative path starts with a dot.

    Mirrors the indexer's visibility rule
    (`any(part.startswith(".") for part in rel.parts)`) so the file-access
    tools keep `.obsidian`, `.git`, `.trash`, `.smart-env`, … out of reach.
    """
    return any(part.startswith(".") for part in Path(rel).parts)


def validate_visible_path(relative_path: str, user_id: int | None = None) -> Path:
    """`validate_path` plus the dot-dir visibility guard.

    Rejects path traversal (via `validate_path`) and any path that resolves
    into a dot-directory. Used by the raw file-access tools so they expose
    exactly the files the indexer would consider.
    """
    resolved = validate_path(relative_path, user_id=user_id)
    vault = _vault_root(user_id).resolve()
    rel = resolved.relative_to(vault)
    if is_hidden_path(rel):
        raise ValueError(f"Hidden path denied: {relative_path}")
    return resolved


# ────────────────────────────────────────────────────────────────────────────
# The pre-publish vault-root confirmation (#88)
# ────────────────────────────────────────────────────────────────────────────
#
# `APIKeyMiddleware` binds `current_vault_root` once, at admission, and that
# snapshot is deliberately immutable — it is what makes #66's admission gate
# fail closed under a concurrent bulk cache warm. The cost is that the snapshot
# is *stale by design* for the whole of a request: an administrator can commit
# a reassignment, the panel can report it complete, and a write already in
# flight still publishes into the former root.
#
# The answer is not a lock. The transfer routes hold their credential and user
# rows `SELECT … FOR UPDATE` across the publish because they have a token row,
# an already-open session and a bounded byte stream; a note mutation has none
# of those, and holding those rows across `move_note`'s link rewrites — or
# across an `edit_note` on a note near `MAX_NOTE_BYTES` — would put arbitrary
# vault I/O inside a lock every authenticated request contends for. That gate
# stays where it is and is **not** weakened to this form.
#
# So: one fresh `SELECT users.vault_path, users.is_active` immediately before
# each publishing operation, compared against the root the request bound, and a
# refusal on change. It narrows the window from *one request's lifetime* down
# to staging, the durability flush and one publishing call. It does not close
# it — see `RootConfirmation` — and nothing here claims a reassignment is
# linearizable with an in-flight mutation.


class VaultAssignmentChanged(RuntimeError):
    """**Operational**: the caller's vault assignment changed mid-call.

    An administrator reassigned, unassigned, deactivated or deleted the acting
    user between admission and this publication. Nothing has been written. A
    distinct type from `UnconfirmedPublication` because the two say opposite
    things to a log reader: this one is an event the operator caused, that one
    is a bug in this repository.

    `bound_root` is the root the request was admitted for; `reason` is a short
    machine-ish token for the four conditions (`reassigned`, `unassigned`,
    `inactive`, `missing`).
    """

    def __init__(self, message: str, *, bound_root: str | None = None,
                 reason: str = "reassigned") -> None:
        super().__init__(message)
        self.bound_root = bound_root
        self.reason = reason


class VaultAnchorUnavailable(RuntimeError):
    """**Admission**: the root this request is anchored to cannot be named.

    A cold process cache, or an ownerless credential under `MULTI_USER_MODE`.
    `APIKeyMiddleware` and `_vault_root` already refuse both before a tool body
    runs, so it is unreachable in a normal request — but a mutation whose
    anchor cannot be named must not publish either, and it is the *admission*
    failure that describes it, not an administrator's reassignment.

    A `RuntimeError` because that is what `_vault_root` has always raised for
    this condition, and callers that catch `RuntimeError` keep working. It is a
    **distinct type** so the tool seam can tell it apart from the operational
    refusal without catching `RuntimeError` broadly — which would also swallow
    every `RuntimeError` a publish body raises.
    """


class VaultConfirmationUnavailable(Exception):
    """**Infrastructure**: the assignment could not be read at all.

    The confirming `SELECT` failed — the database is unreachable, the pool is
    exhausted, the statement timed out. It says nothing about the assignment,
    so it must never be reported as one: an operator reading "an administrator
    reassigned your vault" during a database outage is being told something
    nobody did.

    Deliberately **not** a `RuntimeError`. The tool bodies catch
    `ValueError`/`RuntimeError`/`OSError` around their publishes and render
    them as ordinary failure strings, and a confirmation outage rendered as
    "Write failed" is an infrastructure incident reported as a bad write.
    Before the first publication of a call it propagates and the call fails;
    after a publication has already stood it is caught explicitly, by the one
    caller that has something partial to report (`move_note`).
    """


class UnconfirmedPublication(Exception):
    """**Programming error**: a publish helper reached an unconfirmed target.

    Every destructive operation on a `MutableTarget` — the atomic write, the
    no-clobber move, the soft delete and the permanent unlink — takes a
    `RootConfirmation` for the operation it is about to perform. A publication
    that reaches one without a confirmation, with a confirmation somebody has
    already spent, or with one taken for a different user or a different root
    means somebody added a mutating tool (or a new publication inside an
    existing one) and did not confirm the assignment first, which is exactly
    the hole this scheme exists to make impossible.

    Deliberately **not** a `RuntimeError`: the tool bodies catch `ValueError` /
    `RuntimeError` / `OSError` around their publishes and turn them into
    strings, and a missing confirmation must not be quietly rendered as a
    failed write. It escapes as a loud error instead.
    """


def _canonical_root(path) -> str:
    """`transfer.canonical_vault_root`, imported at call time (import cycle)."""
    from src.services.transfer import canonical_vault_root

    return canonical_vault_root(path)


class RootConfirmation:
    """One fresh confirmation of the caller's vault assignment.

    Produced by `_confirm_vault_assignment` and **consumed by exactly one
    publishing operation**. Four properties, all structural rather than
    conventional, and each of them was a hole in an earlier implementation:

    - **Leased to one synchronous callback.** `confirmed_publication`
      activates the confirmation, calls the publish callback, and invalidates
      it in a `finally` on *every* exit path. An inactive confirmation cannot
      be consumed, so a callback that squirrels the object away
      (`saved.append(c)`) and publishes with it after the `await` returns —
      or after an administrator reassigns — is refused rather than obeyed.
      Adversarial round 2 found exactly that: single-consumption alone bounds
      *how many times* a confirmation is used, never *when*.
    - **Exactly one consumption per successful publication.** A callback that
      returns normally without spending its confirmation is a programming
      error, not a silent no-op: it means a publish path was added that does
      not go through a publish helper.
    - **Intrinsically single-consumption.** The spent flag lives on the
      confirmation itself, not on the target it was handed to, so the same
      object cannot be spent twice by presenting it to two targets.
    - **Target-bound.** `consume` checks the acting user id and the canonical
      assignment string against the target's own, so a confirmation taken for
      one user or one root cannot authorise a publication into another.

    **The residual, stated rather than implied.** The confirming read is not a
    lock. A reassignment that commits after it and before the publishing
    operation completes — including one that commits while the syscall is
    running — still lands in the former root, and the tool reports success.
    That is the same optimistic guarantee level the system declares for
    `edit_note(expected=…)` and for the transfer fingerprint check.

    `queried` is False in single-user mode, where there is no user row to read
    and the spec says no query is issued at all.
    """

    __slots__ = ("user_id", "root", "queried", "_spent", "_active")

    def __init__(self, user_id: int | None, root: str, queried: bool) -> None:
        self.user_id = user_id
        self.root = root
        self.queried = queried
        self._spent = False
        # Inert until a wrapper leases it, and inert again the moment that
        # wrapper's callback returns. A confirmation that never reaches
        # `confirmed_publication` — a hand-built one in a test, or
        # `_single_shot_confirmation`'s — is leased explicitly by whoever
        # publishes with it.
        self._active = False

    @property
    def spent(self) -> bool:
        return self._spent

    @property
    def active(self) -> bool:
        """Whether a publication may still be authorised by this object."""
        return self._active

    def _lease(self) -> None:
        """Begin the one dynamic extent in which this may authorise a publish."""
        self._active = True

    def _revoke(self) -> None:
        """End it. Idempotent, and called from a `finally` on every exit."""
        self._active = False

    def _refuse(self, operation: str, why: str) -> "UnconfirmedPublication":
        return UnconfirmedPublication(
            f"Refusing to {operation}: {why} A confirmation authorises exactly "
            "one publication, for the user and root it was taken for, and only "
            "while the confirmed-publication call that leased it is still on "
            "the stack. Await vault.confirmed_publication instead of retaining "
            "one (#88)."
        )

    def consume(self, user_id: int | None, assignment: str, operation: str) -> None:
        """Spend this confirmation for `operation` under `(user_id, assignment)`.

        Checked-and-set: the lease and the spent flag are read and written
        here, in the same synchronous step that authorises the publication, so
        there is no state anywhere else that a second publication could find
        still set.
        """
        if not self._active:
            raise self._refuse(
                operation,
                "its vault-root confirmation is not leased to a publication in "
                "progress — it was either never leased or the confirmed "
                "publication that leased it has already returned.",
            )
        if self._spent:
            raise self._refuse(
                operation, "its vault-root confirmation has already been spent."
            )
        if self.user_id != user_id:
            raise self._refuse(
                operation,
                f"its vault-root confirmation was taken for user_id={self.user_id!r}, "
                f"not user_id={user_id!r}.",
            )
        if self.root != assignment:
            raise self._refuse(
                operation,
                f"its vault-root confirmation was taken for {self.root!r}, "
                f"not {assignment!r}.",
            )
        self._spent = True

    def consume_for(self, target: "MutableTarget", operation: str) -> None:
        """`consume`, reading the user and the assignment off the target."""
        self.consume(target.user_id, target.assignment, f"{operation} {target.rel}")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"RootConfirmation(user_id={self.user_id!r}, root={self.root!r}, "
            f"queried={self.queried!r}, spent={self._spent!r}, "
            f"active={self._active!r})"
        )


@contextlib.contextmanager
def _leased(confirmation: RootConfirmation):
    """Lease `confirmation` for the body, and revoke it however the body ends.

    The `finally` is the whole mechanism: an exception, an early return, a
    callback that retained the object — every path revokes, and `consume`
    refuses an unleased confirmation. Nothing outside this context manager
    calls `_lease`.
    """
    confirmation._lease()
    try:
        yield confirmation
    finally:
        confirmation._revoke()


# Module-private issuance token. A `MovePermit` cannot be constructed without
# it, so a caller cannot hand `move_file_no_clobber` a permit for a move that
# never happened — which adversarial round 2 demonstrated against the public
# constructor: `MovePermit(destination, source)` authorised a rename with no
# confirmation at all. The token is only half of it; the other half is that
# `authorise` requires the *lease* the forward move ran under to still be
# active, so even a permit built with the token is inert outside the confirmed
# publication that issued it.
_PERMIT_ISSUE = object()


class _EndpointFacts:
    """The immutable facts a permit remembers about one end of a move."""

    __slots__ = ("user_id", "assignment", "rel")

    def __init__(self, target: "MutableTarget") -> None:
        self.user_id = target.user_id
        self.assignment = target.assignment
        self.rel = target.rel

    def matches(self, target: "MutableTarget") -> bool:
        return (
            self.user_id == target.user_id
            and self.assignment == target.assignment
            and self.rel == target.rel
        )


class MovePermit:
    """Licence to undo **one** no-clobber move, and nothing else.

    `move_note`'s failure handling may have to move the file straight back:
    `_verify_the_moved_inode` calls `move_file_no_clobber` with the endpoints
    swapped when what arrived at the destination is our inode but is a
    directory or a symbolic link. Refusing that for want of a confirmation
    would strand the note somewhere nobody named.

    The first implementation paid for that by stamping the one confirmation
    onto **both** endpoints, which made a reusable token out of it. The second
    made a permit object — but with a public constructor, so a caller could
    build one out of thin air and rename with no confirmation at all. This is
    the closed form:

    - **Unforgeable.** `__init__` refuses without the module-private issuance
      token, and only a *successful, confirmed* forward move inside
      `move_file_no_clobber` passes it.
    - **Bound to the lease that issued it.** `authorise` requires the
      confirmation's lease to still be active, so a permit is inert the moment
      the enclosing `confirmed_publication` returns — a rollback is part of the
      publication it undoes or it is nothing.
    - **Bound to immutable endpoint facts.** Object identity *and* the
      `(user_id, assignment, rel)` triple of each end, so it cannot be pointed
      at a different pair, nor at the same objects after they have been
      revalidated for somebody else.
    - **Single-use.** One rollback, in the reverse direction only.

    It is not a re-confirmation and does not claim to be: the rollback undoes
    the very publication the confirmation covered, synchronously, with no
    `await` in between, so it is inside that publication's window rather than a
    new one.
    """

    __slots__ = (
        "_confirmation",
        "_source",
        "_destination",
        "_source_facts",
        "_destination_facts",
        "_spent",
    )

    def __init__(
        self,
        issue_token=None,
        *,
        confirmation: "RootConfirmation | None" = None,
        source: "MutableTarget | None" = None,
        destination: "MutableTarget | None" = None,
    ) -> None:
        if issue_token is not _PERMIT_ISSUE:
            raise UnconfirmedPublication(
                "A move permit is issued only by a confirmed forward move, "
                "never constructed. One built by hand would authorise a rename "
                "for which no vault-root confirmation was ever taken (#88)."
            )
        self._confirmation = confirmation
        self._source = source
        self._destination = destination
        self._source_facts = _EndpointFacts(source)
        self._destination_facts = _EndpointFacts(destination)
        self._spent = False

    def authorise(self, source: "MutableTarget", destination: "MutableTarget") -> None:
        if self._spent:
            raise UnconfirmedPublication(
                "This move permit has already been used; a rollback is "
                "authorised once, for the move it undoes (#88)."
            )
        if not self._confirmation.active:
            raise UnconfirmedPublication(
                "This move permit's confirmed publication has already "
                "returned, so the move it undoes is no longer in progress. A "
                "rollback is part of the publication it reverses or it is "
                "nothing (#88)."
            )
        if source is not self._destination or destination is not self._source:
            raise UnconfirmedPublication(
                "A move permit authorises only the reverse of the move that "
                f"produced it ({self._destination_facts.rel} → "
                f"{self._source_facts.rel}), not {source.rel} → "
                f"{destination.rel} (#88)."
            )
        if not (
            self._destination_facts.matches(source)
            and self._source_facts.matches(destination)
        ):
            raise UnconfirmedPublication(
                "A move permit's endpoints no longer carry the user and vault "
                "assignment they were issued for, so the rollback would run "
                "under facts the forward move never confirmed (#88)."
            )
        self._spent = True


def _require_one_vault(source: "MutableTarget", destination: "MutableTarget") -> None:
    """Both ends of a move belong to one caller, one assignment, one root inode.

    `rename_noreplace` is destructive at **both** ends — it removes the source
    directory entry as surely as it creates the destination one — but only the
    destination's confirmation was ever consumed. Adversarial round 2's failing
    input pairs a source opened for user 7 under `/vaults/alice` with a
    destination opened for user 8 under `/vaults/bob` and confirms only user 8:
    Alice's note is removed without her assignment ever being read.

    Requiring the two ends to agree is what makes the single consumption
    sufficient — confirming the destination then confirms the source too,
    because they are the same user and the same assignment. The root inode is
    compared as well (`fstat` of each target's pinned root descriptor, not its
    pathname), because two assignments can spell the same string while
    `open_mutable` pinned different directories.

    Unreachable from the tools today: `move_note` opens both ends with one
    `uid`. It is checked here because this is a shared primitive and the next
    caller may not.
    """
    if source.user_id != destination.user_id:
        raise UnconfirmedPublication(
            f"Refusing to move {source.rel} → {destination.rel}: the two ends "
            f"were validated for different callers (user_id={source.user_id!r} "
            f"and user_id={destination.user_id!r}). A move removes the source "
            "as surely as it creates the destination, so one confirmation "
            "cannot cover both (#88)."
        )
    if source.assignment != destination.assignment:
        raise UnconfirmedPublication(
            f"Refusing to move {source.rel} → {destination.rel}: the two ends "
            f"were validated under different vault assignments "
            f"({source.assignment!r} and {destination.assignment!r}) (#88)."
        )
    try:
        src_root = os.fstat(source.root_fd)
        dst_root = os.fstat(destination.root_fd)
    except OSError as exc:
        raise UnconfirmedPublication(
            f"Refusing to move {source.rel} → {destination.rel}: the pinned "
            f"vault root of one end could not be inspected ({exc}) (#88)."
        ) from None
    if (src_root.st_dev, src_root.st_ino) != (dst_root.st_dev, dst_root.st_ino):
        raise UnconfirmedPublication(
            f"Refusing to move {source.rel} → {destination.rel}: the two ends "
            "are anchored to different vault-root directories, so the "
            "assignment string they share does not describe one vault (#88)."
        )


def _require_confirmation(
    confirmation: "RootConfirmation | None", target: "MutableTarget", operation: str
) -> None:
    """Spend `confirmation` for `operation` on `target`, or refuse to publish.

    The structural half of #88, and the reason it lives in one function: every
    publish helper calls it before it acts, so a mutating tool added later
    cannot publish without confirming the assignment first — the same way a
    tool added later cannot skip the admission gate. The refusal is
    `UnconfirmedPublication`, deliberately distinguishable from
    `VaultAssignmentChanged`: the first is a bug here, the second is an
    administrator's action.
    """
    if confirmation is None:
        raise UnconfirmedPublication(
            f"Refusing to {operation} {target.rel}: no vault-root confirmation "
            "was taken for this publication. Publish through one of the "
            "confirmed-publication wrappers "
            "(vault.confirmed_publication) — it awaits the assignment read and "
            "publishes before yielding (#88)."
        )
    confirmation.consume_for(target, operation)


_RESIDUAL_NOTE = (
    "The assignment is checked immediately before publication, which narrows "
    "the window to staging, the durability flush and one publishing call — it "
    "does not close it, the same optimistic guarantee as edit_note(expected=…)."
)


def _assignment_changed_error(user_id: int, bound: str, reason: str) -> str:
    """The one wording for a refused publication, for every mutating tool."""
    what = {
        "reassigned": "now points somewhere else",
        "unassigned": "has been cleared",
        "inactive": "belongs to an account that is no longer active",
        "missing": "belongs to an account that no longer exists",
    }[reason]
    return (
        f"Vault assignment changed while this call was in flight: this request "
        f"was admitted for {bound}, and the vault assignment for user_id="
        f"{user_id} {what}. Nothing was written, published, renamed or "
        f"unlinked. Re-authenticate and retry against the current assignment. "
        f"({_RESIDUAL_NOTE})"
    )


CONFIRMATION_UNAVAILABLE_ERROR = (
    "The vault assignment could not be re-read before publishing: the "
    "database is unreachable. This is a confirmation outage, not a "
    "reassignment — nobody changed the assignment, and this server cannot "
    "currently tell whether anybody did. Nothing was published under an "
    "unverified assignment."
)


async def _confirm_vault_assignment(user_id: int | None = None) -> RootConfirmation:
    """Re-read the caller's vault assignment and confirm it is unchanged.

    **Private on purpose.** The public surface is the async wrappers below.
    Handing a caller a confirmation object is what let one be retained across
    an `await`, stamped onto a second target, or carried past the work it
    covered; the wrappers award one and spend it in the same synchronous step,
    so there is no window in which a caller holds an unspent one.

    One `SELECT users.vault_path, users.is_active WHERE id = :uid` on its own
    short-lived session, canonicalised through `transfer.canonical_vault_root`
    — the single normaliser, shared with the index-provenance record, used
    exactly as it stands — and compared against the root this request bound at
    admission.

    **It is a fresh database read on purpose.** Reading `_user_vault_cache` or
    `current_vault_root` would be a tautology: those are the two values being
    checked. The snapshot is bound once at admission and is immutable by
    design, and the process cache is add-only from the indexer's side. So this
    reintroduces, for mutations only, the per-call query #66 forbade in
    `_vault_root` — and the reconciliation is exactly that: #66's rule is about
    *every* tool call, and search, read, list and the graph tools (which
    dominate the call mix) are untouched. `_vault_root` stays a pure cache
    lookup.

    **The comparison is on the canonical pathname, never a `resolve()`d form.**
    The fact being checked is what the operator saved, not what the disk
    currently looks like; re-resolving here would reintroduce the check-then-act
    #59 removed, and a symlink retarget behind an unchanged assignment is
    deliberately outside this check because #59 pins the parent descriptor
    precisely so a relinked pathname cannot redirect a write.

    Raises `VaultAssignmentChanged` when the assignment differs, is now NULL,
    the row is gone or the user is inactive — the same four conditions
    `APIKeyMiddleware` and `transfer._credential_ok` already treat as loss of
    entitlement; `VaultAnchorUnavailable` when the bound root itself cannot be
    named; `VaultConfirmationUnavailable` when the read fails outright.

    `user_id is None` outside multi-user mode has no user row to re-read, so it
    **issues no query at all** and confirms `settings.vault_path`. Inside
    multi-user mode it is an ownerless credential, which `APIKeyMiddleware`
    already 401s and `_vault_root` already refuses — fail closed here too
    rather than confirm a root that belongs to nobody in particular.
    """
    if user_id is None:
        if settings.multi_user_mode:
            # An ownerless credential. `APIKeyMiddleware` 401s it and
            # `_vault_root` refuses it, so this is unreachable in a normal
            # request — and it carries `_vault_root`'s own message, because
            # "this credential belongs to nobody" is the *admission* failure
            # and must not be logged as an administrator having changed an
            # assignment that never existed.
            raise VaultAnchorUnavailable(UNOWNED_IN_MULTI_USER_ERROR)
        # Single-user mode: no user row exists to disagree with. No query.
        return RootConfirmation(
            None, _canonical_root(settings.vault_path), queried=False
        )

    # The comparand is the root this request is anchored to — `_vault_root`
    # prefers the request's own bound snapshot and falls back to the process
    # cache, which is exactly the value `open_mutable` validated the target
    # against. A cold cache raises here and the mutation is refused, for the
    # same reason the admission gate refuses one.
    try:
        bound = _canonical_root(_vault_root(user_id))
    except RuntimeError as exc:
        raise VaultAnchorUnavailable(str(exc)) from None

    from src.database import async_session
    from src.models.db import User

    try:
        async with async_session() as session:
            row = (
                await session.execute(
                    select(User.vault_path, User.is_active).where(User.id == user_id)
                )
            ).first()
    except (VaultAssignmentChanged, VaultAnchorUnavailable):  # pragma: no cover
        raise
    except Exception as exc:
        # The read failed; the assignment is unknown, which is not the same
        # fact as "the assignment changed" and must not be reported as one.
        logger.warning(
            "publication_refused_confirmation_unavailable",
            extra={"user_id": user_id, "error": str(exc)},
        )
        raise VaultConfirmationUnavailable(
            f"{CONFIRMATION_UNAVAILABLE_ERROR} ({exc})"
        ) from exc

    if row is None:
        reason = "missing"
    elif not row.is_active:
        reason = "inactive"
    elif row.vault_path is None:
        reason = "unassigned"
    elif _canonical_root(row.vault_path) != bound:
        reason = "reassigned"
    else:
        return RootConfirmation(user_id, bound, queried=True)

    logger.warning(
        "publication_refused_vault_assignment_changed",
        extra={"user_id": user_id, "reason": reason},
    )
    raise VaultAssignmentChanged(
        _assignment_changed_error(user_id, bound, reason),
        bound_root=bound,
        reason=reason,
    )


def _reject_deferred_publish(publish) -> None:
    """Refuse a callback that would not have published by the time it returns.

    A coroutine function, a generator function and an async-generator function
    all share the property that calling them runs none of the body — or stops
    it at the first `yield` — so the publication happens later, on somebody
    else's schedule, which is precisely the window the wrapper exists to close.
    Adversarial round 2 found that the first two of these evaded the original
    `iscoroutinefunction` check entirely.
    """
    if inspect.iscoroutinefunction(publish):
        raise UnconfirmedPublication(
            "A confirmed publication must be a synchronous function: an "
            "`await` between the confirming read and the write is exactly the "
            "window the confirmation narrows (#88)."
        )
    if inspect.isasyncgenfunction(publish):
        raise UnconfirmedPublication(
            "A confirmed publication must not be an async generator function: "
            "its body runs on the consumer's schedule, not inside this call "
            "(#88)."
        )
    if inspect.isgeneratorfunction(publish):
        raise UnconfirmedPublication(
            "A confirmed publication must not be a generator function: "
            "calling it runs none of its body, so nothing would have been "
            "published when it returns (#88)."
        )


def _reject_deferred_result(result) -> None:
    """The same rule applied to what the callback handed back.

    The callable checks above do not see a callable *object* whose `__call__`
    is a generator, nor a factory that returns somebody else's coroutine, so
    the result is checked too. **Nothing is closed here**: `close()` on an
    unknown awaitable is arbitrary code of a stranger's choosing, and it buys
    nothing — the confirmation's lease has already been revoked by the
    `finally` above, so driving the object later cannot publish.
    """
    if inspect.isasyncgen(result) or inspect.isgenerator(result):
        raise UnconfirmedPublication(
            "A confirmed publication returned a generator, so its body had not "
            "run when control came back. It was not driven (#88)."
        )
    if inspect.isawaitable(result):
        raise UnconfirmedPublication(
            "A confirmed publication returned an awaitable, so it had not "
            "published when control came back. It was not awaited (#88)."
        )


async def confirmed_publication(user_id: int | None, publish):
    """Confirm the assignment, then run `publish` before yielding control.

    The one seam every mutation goes through (#88), and the shape is the whole
    point: the confirming read is the *last* `await` before the publication.
    `publish` is a **synchronous** callable taking the `RootConfirmation` and
    performing exactly that one publishing operation with it, so between the
    read and the write there is no scheduling point at which a caller could do
    anything else — including awaiting a reassignment into existence.

    **The confirmation is leased for the callback's dynamic extent and revoked
    in a `finally`.** Single-consumption alone bounded how many times a
    confirmation could be used and never *when*: a callback that stashed the
    object and published with it after this coroutine returned — after an
    administrator's reassignment had committed — was obeyed. The lease closes
    that, and a callback that returns normally without spending its
    confirmation is refused, because that is what a publish path added outside
    the helpers looks like.

    Coroutine, generator and async-generator callbacks are refused rather than
    driven, and so is a deferred *result*: each of them would publish on
    somebody else's schedule.

    Raises `VaultAssignmentChanged`, `VaultAnchorUnavailable` or
    `VaultConfirmationUnavailable` from the confirming read; anything `publish`
    raises propagates untouched, so a caller can tell a refused confirmation
    from a failed write by type alone.
    """
    _reject_deferred_publish(publish)
    confirmation = await _confirm_vault_assignment(user_id)
    with _leased(confirmation):
        result = publish(confirmation)
    # Only on a normal return: an exception from `publish` has already
    # propagated past this, and the lease was revoked on the way out.
    _reject_deferred_result(result)
    if not confirmation.spent:
        raise UnconfirmedPublication(
            "A confirmed publication returned without consuming its vault-root "
            "confirmation, so nothing it did was authorised by one. Publish "
            "through a helper that takes `confirmation=` (#88)."
        )
    return result


def _single_shot_confirmation(user_id: int | None) -> RootConfirmation:
    """The confirmation for the *synchronous*, single-shot convenience writers.

    `write_file(path_str, …)` and `write_bytes(path_str, …)` validate and
    publish in one synchronous call and have no production callers — every
    tool holds its own `MutableTarget` and publishes through
    `confirmed_publication`. In single-user mode the confirmation issues no
    query by specification, so it is available without an `await` and these
    keep working unchanged.

    A multi-user caller is refused rather than exempted: confirming a user's
    assignment needs a database read, which a synchronous function cannot do,
    and quietly publishing unconfirmed is the hole the confirmation exists to
    close.
    """
    if user_id is None and not settings.multi_user_mode:
        return RootConfirmation(
            None, _canonical_root(settings.vault_path), queried=False
        )
    raise UnconfirmedPublication(
        "write_file()/write_bytes() are synchronous single-shot conveniences "
        "and cannot confirm a multi-user vault assignment before publishing. "
        "Open the target with open_mutable() and publish through "
        "vault.confirmed_publication()."
    )


class VaultRootMismatch(RuntimeError):
    """A shared vault-root descriptor is not the root a target was proved under.

    Raised only by `MutableTarget.share_root`, and only for the identity check —
    the other refusals there are programming errors and stay plain
    `RuntimeError`. A distinct type because the one caller has to tell "this
    vault root moved under us, abort the whole call" apart from "this one source
    could not be rewritten", and matching on a message to do that is how the two
    get confused later.
    """


class MutableTarget:
    """A validated mutation target, **anchored to an open parent descriptor**.

    `validate_mutable_path` resolves the parent once and hands back
    `resolved_parent / name`. That closes the static-alias vector (#54) but not
    the live one (#59): every syscall a write then makes — `mkdir`, the temp
    create, the `expected=` read, the publish — hands that *pathname* back to
    the kernel, which walks it again. A concurrent process that renames the
    resolved parent and drops a symlink at its name, or repoints a symlinked
    vault root, between two of those syscalls redirects the write to a
    directory nobody validated. `expected=` cannot catch it: the decoy may hold
    byte-identical bytes.

    So the parent is resolved **once**, by a single kernel-enforced
    beneath-root lookup from an open root descriptor
    (`vault_fs.open_dir_beneath`, an `openat2` carrying `RESOLVE_BENEATH |
    RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS`), and that descriptor is what
    the rest of the call uses. A directory descriptor keeps pointing at the
    same directory across a rename of that directory, and no pathname is ever
    re-resolved, so there is nothing left for a mid-call rename or relink to
    redirect. The lookup being one call rather than a per-component walk is
    #87: an ancestor renamed out of the vault *between* two such opens used to
    yield a parent descriptor outside the root, with every mutation anchored to
    it and the tool reporting success for the path the caller named.

    Fields:

    - `path` — `resolved_parent / name`, for error messages, `relative_to` and
      the database rows. Never handed back to a syscall on a mutation path.
    - `rel` — the vault-relative POSIX path of `path`; what the indexer stores.
    - `name` — the final component, taken exactly as the caller named it.
    - `root` — the resolved vault root.
    - `user_id` — the caller this target was validated for.
    - `assignment` — the *canonical assignment string* that user was anchored
      to, i.e. `_vault_root(user_id)` normalised, not the resolved root. A
      `RootConfirmation` is checked against `user_id` and `assignment` before
      it may authorise a publication here (#88).
    - `dir_fd` — the parent directory descriptor.

    The caller owns the descriptors and must `close()` (or use `with`). A
    target whose parent does not exist yet has no `dir_fd` until
    `ensure_parent()` creates it — deferred so a call refused for an unrelated
    reason (an over-cap body) leaves no directories behind.
    """

    __slots__ = (
        "path",
        "rel",
        "name",
        "root",
        "user_id",
        "assignment",
        "parent_rel",
        "created",
        "_dir_fd",
        "_root_fd",
        "_root_owned",
    )

    def __init__(
        self,
        *,
        path: Path,
        rel: str,
        name: str,
        root: Path,
        user_id: int | None,
        assignment: str,
        parent_rel: str,
        dir_fd: int | None,
        root_fd: int,
    ) -> None:
        self.path = path
        self.rel = rel
        self.name = name
        self.root = root
        # Who this target was validated for, and the canonical assignment
        # string that user was anchored to. A `RootConfirmation` is checked
        # against both before it may authorise a publication here (#88): a
        # confirmation taken for one user, or for one root, must not be able to
        # publish into another's target.
        self.user_id = user_id
        self.assignment = assignment
        self.parent_rel = parent_rel
        # Directories `ensure_parent` created on the way to this target, if
        # any. What #97's ancestor flush walks; empty for the ordinary write
        # into a folder that was already there.
        self.created: list[str] = []
        self._dir_fd = dir_fd
        self._root_fd: int | None = root_fd
        # Whether closing this target closes the root descriptor. False once
        # `share_root` has swapped in a descriptor somebody else's lifetime
        # governs — see there.
        self._root_owned = True

    # ── descriptors ─────────────────────────────────────────────────────────

    @property
    def root_fd(self) -> int:
        """The vault-root descriptor this target's parent was walked from."""
        if self._root_fd is None:
            raise RuntimeError(
                f"MutableTarget for {self.rel} has no root descriptor "
                "(closed, or released by release_root)"
            )
        return self._root_fd

    def release_root(self) -> None:
        """Drop the root descriptor, keeping the parent one.

        Refuses when the parent is not open yet, which would leave the target
        unusable.

        **A target with no root cannot flush its ancestor chain**, and since the
        chain rule (#97) every successful publication owes that chain. So this
        is now only for a target that will not publish. A caller that holds many
        targets *and* publishes through them wants `share_root` instead: one
        descriptor for all of them, rather than none.
        """
        if self._dir_fd is None:
            raise RuntimeError(
                f"Cannot release the root of {self.rel}: its parent directory "
                "is not open, so the target would become unusable"
            )
        if self._root_fd is not None:
            if self._root_owned:
                vault_fs.close_quietly(self._root_fd, f"vault root for {self.rel}")
            self._root_fd = None

    def share_root(self, root_fd: int) -> None:
        """Swap this target's own root descriptor for a shared, borrowed one.

        The descriptor stays usable — `ensure_parent` and the post-publication
        ancestor flush both still work — but this target no longer owns it and
        `close()` will not close it. The caller's lifetime governs it.

        **Why this exists.** `move_note(rewrite_links=True)` pins one target per
        backlink source from its preflight read until its post-move write, and
        the number of sources is unbounded, so holding two descriptors each
        (parent + root) would halve how large a move the process can afford.
        Releasing the root instead was the previous answer and it is wrong now:
        a target with no root cannot look its ancestors up, so its publication
        flushed only the leaf's parent and silently skipped the chain the rule
        promises. Every rewrite target resolves the *same* vault root, so one
        borrowed descriptor serves all of them — one fd for the whole phase
        rather than one per source.

        **The identity check is what makes the sharing sound**, and it is why
        this must be called *instead of* `release_root` rather than after it: a
        target whose root has already been dropped cannot prove which root it
        was validated against, and adopting one on faith would anchor its
        ancestor lookups — and any directory creation — to a root the kernel
        never proved this target's parent lies beneath. A mismatch means the
        vault root pathname was repointed mid-call, which is precisely the
        substitution surface #59 exists for, so it raises rather than guessing.
        """
        if self._dir_fd is None:
            raise RuntimeError(
                f"Cannot share a root with {self.rel}: its parent directory is "
                "not open, so the target would become unusable"
            )
        if self._root_fd is None:
            raise RuntimeError(
                f"Cannot share a root with {self.rel}: its own root descriptor "
                "is already gone, so there is nothing to verify the shared one "
                "against"
            )
        mine = os.fstat(self._root_fd)
        theirs = os.fstat(root_fd)
        if (mine.st_dev, mine.st_ino) != (theirs.st_dev, theirs.st_ino):
            raise VaultRootMismatch(
                f"the vault root was repointed while this call was running, so "
                f"{self.rel} was validated against a different root than the "
                "one the call is now anchored to."
            )
        if self._root_owned:
            vault_fs.close_quietly(self._root_fd, f"vault root for {self.rel}")
        self._root_fd = root_fd
        self._root_owned = False

    @property
    def dir_fd(self) -> int:
        """The parent directory descriptor, created on demand if it is missing.

        Reading it is what a mutation does, so a missing parent is created here
        rather than left for a pathname-based `mkdir` to walk to.
        """
        self.ensure_parent()
        return self._dir_fd  # type: ignore[return-value]

    @property
    def parent_fd(self) -> int | None:
        """The parent descriptor if it is already open, **without creating it**.

        What reads use. `dir_fd` creates a missing parent because a write is
        entitled to; a read is not, and a read helper that quietly `mkdir`s is
        how an absent note turns into a new empty directory tree.
        """
        return self._dir_fd

    def ensure_parent(self) -> None:
        """Open — creating if needed — the parent directory descriptor.

        The deferred-creation site, and it inherits #87 whole: `mkdirat` has no
        beneath-root form, so `open_dir_beneath(create=True)` creates one
        component at a time — but carries no descriptor across a creation, and
        the descriptor stored here always comes from a fresh beneath-root
        lookup of the whole parent performed *after* the creation. So the
        descriptor a write anchors to is never one a creation produced. The
        residual that leaves — at most one empty directory per component, per
        creation descent — is stated in `open_dir_beneath`'s docstring and in
        this module's own.

        Whatever it creates is recorded in `self.created`, so
        `_flush_publication` can make those directory *entries* durable too
        (#97): flushing the note's own parent while the entry that names that
        parent is still only in the page cache means a crash loses the folder
        and the note the tool reported writing.
        """
        if self._dir_fd is not None:
            return
        try:
            self._dir_fd = vault_fs.open_dir_beneath(
                self.root_fd, self.parent_rel, create=True, created=self.created
            )
        except vault_fs.UnsafePath as exc:
            raise ValueError(str(exc)) from None

    def close(self) -> None:
        if self._dir_fd is not None:
            vault_fs.close_quietly(self._dir_fd, f"parent directory for {self.rel}")
            self._dir_fd = None
        if self._root_fd is not None:
            # A borrowed root belongs to whoever lent it (`share_root`); closing
            # it here would pull it out from under every other target sharing
            # it, and from under the caller's own cleanup.
            if self._root_owned:
                vault_fs.close_quietly(self._root_fd, f"vault root for {self.rel}")
            self._root_fd = None

    def __enter__(self) -> "MutableTarget":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # ── anchored stats ──────────────────────────────────────────────────────

    def lstat(self) -> os.stat_result | None:
        """`lstat` the final component through the parent fd, or None if absent.

        Never follows a link — the leaf is taken as named, exactly as the
        validation `lstat` did.
        """
        if self._dir_fd is None:
            return None
        try:
            return os.stat(self.name, dir_fd=self._dir_fd, follow_symlinks=False)
        except (FileNotFoundError, NotADirectoryError):
            return None

    def exists(self) -> bool:
        return self.lstat() is not None

    def is_file(self) -> bool:
        info = self.lstat()
        return info is not None and stat.S_ISREG(info.st_mode)


def _mutable_parts(relative_path: str) -> list[str]:
    """Lexical half of the mutable-path guard: components, or a `ValueError`."""
    raw = str(relative_path).replace(os.sep, "/")
    if "\x00" in raw:
        raise ValueError(f"Path traversal denied: {relative_path}")
    if raw.endswith("/"):
        raise ValueError(f"Not a file path: {relative_path!r}")
    rel = PurePosixPath(raw)
    if rel.is_absolute():
        raise ValueError(f"Path traversal denied: {relative_path}")
    parts = [part for part in rel.parts if part not in ("", ".")]
    if any(part == ".." for part in parts):
        # A `..` that stays inside the vault is not an attack — it is an agent
        # passing a path it built by hand. Refusing it is still right (a
        # mutating tool must not resolve components away; that is the whole
        # point of this validator), but "Path traversal denied" tells the
        # caller nothing it can act on. Name the normalised path instead.
        normalised = posixpath.normpath("/".join(parts))
        if normalised == ".":
            raise ValueError(f"Not a file path: {relative_path!r}")
        if normalised == ".." or normalised.startswith("../"):
            raise ValueError(f"Path traversal denied: {relative_path}")
        raise ValueError(
            f"{relative_path} contains a '..' segment. Mutating tools take the "
            "path as named and never resolve a component away — pass the "
            f"normalised path instead: {normalised}. (Reads still accept '..'.)"
        )
    if not parts:
        raise ValueError(f"Not a file path: {relative_path!r}")
    return parts


def open_mutable(relative_path: str, user_id: int | None = None) -> MutableTarget:
    """Validate a path a tool is about to **mutate** and anchor it to a fd.

    `validate_path` returns `(vault / rel).resolve()`, which follows symlinks —
    so an in-vault alias `alias.md -> important.md` made every write tool act on
    `important.md` while reporting success for `alias.md`. That is a destructive
    write on a path nobody named. Reads may keep following links (an alias
    reading as its target is what a user expects); mutations may not.

    The rule (see `openspec/specs/vault-write`):

    - the *parent* directory is resolved and must stay inside the vault, so
      symlinked directories inside the vault (shared attachment folders, a
      common Obsidian setup) keep working while an escaping one is still
      rejected by the containment check;
    - the resolved parent is then **re-opened by descriptor**, with one
      `openat2(RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS)`
      from the vault root (#87) rather than one `O_NOFOLLOW` component at a
      time. `resolve()` output holds no symlinks by construction, so the lookup
      succeeds for exactly the paths the containment check accepted — and
      refuses if a component became a link in between. That is how the lookup
      stays strict while symlinked ancestors keep working: they are resolved
      away *before* it, never traversed by it. The kernel proves containment
      for the whole path inside the single call, so no rename *during* the
      lookup can hand back a descriptor outside the root;
    - the final component is taken **as named** and `lstat`ed through that
      descriptor: if it is a symbolic link — including a dangling one — the
      operation is refused with an error naming the link's canonical
      vault-relative target;
    - `path` is `resolved_parent / name`, i.e. the real directory entry the
      indexer sees. Callers that record vault-relative paths in the database
      (`move_note`) use `rel`, so they match what the indexer stores for notes
      under a symlinked directory.

    Every subsequent syscall of the mutation runs against `dir_fd`, so nothing
    after this point re-resolves a pathname.

    Raises `ValueError` for traversal, a hidden (dot-directory) path, a
    non-file-shaped path, an in-vault `..` segment, a symlinked final
    component, or a parent that is not a usable directory.
    """
    parts = _mutable_parts(relative_path)
    name = parts[-1]
    vault = _vault_root(user_id)

    # **The root descriptor is opened first, and never reopened by name.**
    # Resolving the root to a pathname and only then `open`ing that pathname
    # left the whole guard resting on a name: between the two, the resolved
    # root could be renamed away and a symlink left at its name, and the
    # descriptor everything below is anchored to would be a directory the
    # containment check never saw. Pinning first inverts that — from here on
    # the root is an inode, and the pathname work below is checked against it.
    try:
        root_fd = vault_fs.open_root(vault)
    except (OSError, vault_fs.VaultFSError) as exc:
        raise ValueError(f"Vault root is not usable: {exc}") from None

    try:
        vault_resolved = vault.resolve()
        # `vault_resolved` is used for the containment check and for `rel`, so
        # it has to describe the directory we just pinned. If the root's
        # pathname was repointed between the open and the resolve, it does not
        # — refuse rather than compute a relative path against one root and
        # walk it from another.
        _require_same_directory(
            root_fd, vault_resolved, f"the vault root ({vault})"
        )

        resolved_parent = vault.joinpath(*parts[:-1]).resolve()
        try:
            parent_rel = resolved_parent.relative_to(vault_resolved)
        except ValueError:
            raise ValueError(f"Path traversal denied: {relative_path}") from None

        target_path = resolved_parent / name
        if is_hidden_path(parent_rel / name):
            raise ValueError(f"Hidden path denied: {relative_path}")

        parent_rel_posix = parent_rel.as_posix()
        if parent_rel_posix == ".":
            parent_rel_posix = ""

        dir_fd: int | None
        try:
            dir_fd = vault_fs.open_dir_beneath(
                root_fd, parent_rel_posix, create=False
            )
        except FileNotFoundError:
            # The parent does not exist yet. Creating it here would leave
            # directories behind for a call about to be refused for an
            # unrelated reason, so it is deferred to `ensure_parent()`.
            dir_fd = None
        except vault_fs.UnsafePath as exc:
            raise ValueError(str(exc)) from None

        target = MutableTarget(
            path=target_path,
            rel=(parent_rel / name).as_posix(),
            name=name,
            root=vault_resolved,
            user_id=user_id,
            # The *assignment* string, not the resolved root: it is what
            # `_confirm_vault_assignment` compares, and re-resolving would
            # reintroduce the check-then-act #59 removed.
            assignment=_canonical_root(vault),
            parent_rel=parent_rel_posix,
            dir_fd=dir_fd,
            root_fd=root_fd,
        )
    except BaseException:
        vault_fs.close_quietly(root_fd, "vault root")
        raise

    # Inside its own guard: an `lstat` that raises `EIO` would otherwise leak
    # both descriptors, and the message below reads the link while the target
    # is still anchored — never by pathname after the fds are gone.
    try:
        info = target.lstat()
        if info is not None and stat.S_ISLNK(info.st_mode):
            label = _link_target_label(target, vault_resolved)
            raise ValueError(
                f"{relative_path} is a symbolic link to {label} — mutating "
                "tools act only on the named file; operate on the target "
                "instead."
            )
    except BaseException:
        target.close()
        raise
    return target


def _require_same_directory(dir_fd: int, path: Path, what: str) -> None:
    """Refuse unless `path` still names the directory `dir_fd` is open on."""
    try:
        by_name = os.stat(path)
    except OSError as exc:
        raise ValueError(f"Vault root is not usable: {exc}") from None
    anchored = os.fstat(dir_fd)
    if (by_name.st_dev, by_name.st_ino) != (anchored.st_dev, anchored.st_ino):
        raise ValueError(
            f"Refusing to continue: {what} changed while the path was being "
            "validated. Retry the operation."
        )


def validate_mutable_path(relative_path: str, user_id: int | None = None) -> Path:
    """`open_mutable`, discarding the descriptors and keeping only the path.

    The single-shot form, for a caller that needs the guard's *verdict* and
    nothing else. A tool that makes more than one syscall against the returned
    `Path` has reopened the window `open_mutable` closes; take the target
    instead.

    **Nothing in `src/` calls this** — every production mutation path now goes
    through `open_mutable` (including `write_file` / `write_bytes`, which open
    and close their own target). It is kept because the boundary it draws is
    the thing worth naming: a path-shaped answer is only ever safe when it is
    the *whole* answer. If a new caller appears here, that is the signal to
    check whether it should be holding a target instead. The symlink-guard
    tests exercise it directly for the same reason: the guard's verdict is
    testable without an anchored write.
    """
    with open_mutable(relative_path, user_id=user_id) as target:
        return target.path


def _link_target_label(target: "MutableTarget", vault_resolved: Path) -> str:
    """Name a symlink's ultimate target for an error message.

    Vault-relative POSIX when the target lands inside the vault (dangling links
    included — the caller still learns which note the alias was meant to name),
    otherwise the literal string `outside the vault`.

    **The link itself is read through the parent descriptor**, not by pathname.
    Naming a target requires resolving one, and a resolution done after the
    descriptors are gone can describe a link that no longer exists — the
    message would then send the agent at a note that was never involved, which
    is worse than admitting we do not know. So `readlink` is anchored; only the
    *display* resolution of what it points at is by pathname, and if the link
    cannot be read at all the message says so instead of guessing.
    """
    dir_fd = target.parent_fd
    if dir_fd is None:  # pragma: no cover - the leaf cannot exist without one
        return "a target that could not be read (the link changed)"
    try:
        literal = os.readlink(target.name, dir_fd=dir_fd)
    except OSError:
        return "a target that could not be read (the link changed)"
    destination = Path(literal)
    if not destination.is_absolute():
        destination = target.path.parent / destination
    try:
        resolved = Path(os.path.realpath(destination))
        return resolved.relative_to(vault_resolved).as_posix()
    except (ValueError, OSError):
        return "outside the vault"


def read_file(relative_path: str, user_id: int | None = None) -> dict:
    """Read a note, returning frontmatter + content."""
    path = validate_visible_path(relative_path, user_id=user_id)
    if not path.is_file():
        raise FileNotFoundError(f"Note not found: {relative_path}")
    raw = path.read_text(encoding="utf-8")
    frontmatter, content = parse_frontmatter(raw)
    title = frontmatter.get("title") or path.stem
    tags = extract_tags(raw, frontmatter)
    return {
        "path": relative_path,
        "title": title,
        "frontmatter": frontmatter,
        "tags": tags,
        "content": content,
        "size": path.stat().st_size,
        "modified": path.stat().st_mtime,
    }


# How many `.tmp-…` names `_atomic_write_at` tries before giving up. A collision
# is astronomically unlikely; the retry exists so a process that pre-creates
# the name (a symlink decoy, or a leftover from a crash) cannot wedge a write.
_TEMP_ATTEMPTS = 3

def _temp_candidate(name: str) -> str:
    """A fresh temp *name* for `name`, to be created in the same directory.

    A bare component, never a path: everything downstream of validation is
    resolved against a directory descriptor, so there is nothing here for the
    kernel to walk. Factored out so a test can make the name predictable and
    pre-create it.
    """
    return f".tmp-{name}-{os.getpid()}-{uuid.uuid4().hex[:8]}"


def _create_temp_exclusively(dir_fd: int, name: str) -> tuple[int, str]:
    """Create a *named* temp file for `name` inside `dir_fd`; return `(fd, tmp)`.

    Only the **overwrite** path needs this: `renameat` has no by-descriptor
    form, so its source must have a name. `O_CREAT|O_EXCL|O_NOFOLLOW` means the
    name we write through cannot pre-exist and cannot be a symlink — a process
    that guessed the temp name and planted `.tmp-note.md-…  ->  /some/decoy`
    would otherwise have had our write truncate the decoy. `O_EXCL` reports
    that as `EEXIST` (a symlink at the final component fails the same way), so
    we simply take another name.

    The no-clobber path uses `_create_nameless_temp` instead and never puts a
    name in the directory at all.
    """
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    last: OSError | None = None
    for _ in range(_TEMP_ATTEMPTS):
        tmp = _temp_candidate(name)
        try:
            return os.open(tmp, flags, 0o600, dir_fd=dir_fd), tmp
        except FileExistsError as exc:
            last = exc
        except OSError as exc:  # ELOOP on platforms that prefer it for symlinks
            if getattr(exc, "errno", None) != errno.ELOOP:
                raise
            last = exc
    raise RuntimeError(
        f"Could not create a temporary file next to {name}"
    ) from last


def _create_nameless_temp(dir_fd: int) -> int:
    """Stage a no-clobber write in an **unnamed** inode inside `dir_fd`.

    `O_TMPFILE` gives a file with no directory entry at all — nothing for
    another process to observe, replace or race, and nothing for us to clean up
    afterwards. That last part is the point: a named staging file has to be
    unlinked, and an unlink is by *name*, so it can only be guarded by an
    identity check followed by the removal — check-then-act, which could delete
    a substitute planted in between. With no name there is no such step; the
    inode is freed by closing the descriptor.

    **`O_EXCL` must not be set.** With `O_TMPFILE` it means "this file can never
    be linked into the filesystem", which makes the publish below impossible
    (`ENOENT`) — the opposite of its usual meaning, and an easy thing to add by
    reflex.

    Publication is `_link_staged_inode`. A filesystem without `O_TMPFILE`
    (`EOPNOTSUPP`, or `EISDIR`/`EINVAL` on kernels that report it that way)
    raises `UnsupportedFilesystem`: ext4/xfs both support it, and the
    alternative is reintroducing the staging name this exists to remove.
    """
    flags = os.O_TMPFILE | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
    try:
        return os.open(".", flags, 0o600, dir_fd=dir_fd)
    except OSError as exc:
        if getattr(exc, "errno", None) in (
            errno.EOPNOTSUPP,
            errno.EISDIR,
            errno.ENOSYS,
            errno.EINVAL,
        ):
            raise vault_fs.UnsupportedFilesystem(
                "The vault filesystem does not support O_TMPFILE, which the "
                "no-clobber write stages into so that no temporary name is "
                "ever exposed. Refusing rather than staging under a name. "
                "Set VAULT_ALLOW_NAMED_STAGING_FALLBACK=true to accept named "
                "staging instead — a declared, weaker guarantee, not a fix."
            ) from exc
        raise


def _flush_target_dirs(target: MutableTarget, what: str) -> None:
    """`fsync` a target's parent and every directory the call created there.

    Never raises. Shared by the two kinds of publication a note tool performs —
    the staged-payload write and the `renameat2` of a move — because both leave
    the same thing unconfirmed and both take D18's direction with it.
    """
    vault_fs.flush_dir_quietly(target.dir_fd, f"{what} for {target.rel}")
    # Every directory above the destination parent, up to the vault root — not
    # only the ones this call created. `ensure_parent` has the same abort shape
    # as an upload: a write that creates `New/Folder` and then fails (an
    # `expected=` mismatch, an over-cap result) leaves them behind unflushed,
    # and the retry that succeeds records no creations and would flush only the
    # leaf's own parent. `RuntimeError` from a released root descriptor is
    # swallowed with everything else here — see `flush_publication_ancestors_quietly`.
    try:
        root_fd = target.root_fd
    except RuntimeError as exc:
        # `release_root` has dropped the root descriptor — `move_note`'s link
        # rewrites do that to conserve descriptors — so there is nothing to look
        # the ancestors up from. Same direction as everything else here: this
        # must not fail an operation that already landed. Accessed inside the
        # guard rather than passed as an argument, because the property itself
        # is what raises.
        logger.warning(
            "Published %s but could not flush the directories above it: %s. "
            "The operation stands.",
            target.rel,
            exc,
        )
        return
    parent = str(PurePosixPath(str(target.rel)).parent)
    vault_fs.flush_publication_ancestors_quietly(
        root_fd,
        "" if parent == "." else parent,
        target.created,
        str(target.rel),
    )


def _flush_publication(target: MutableTarget) -> None:
    """Make a published note write durable — and never fail the write for it.

    Two flushes, both after publication: the destination directory, so the
    entry the `link`/`replace` created is durable and not only its contents;
    and the parent of every directory `ensure_parent` created on the way,
    outward to the first one that already existed, so `create_note` on a new
    `New/Folder/x.md` cannot lose `New` in a crash and take the note with it.

    **Every failure here is logged and swallowed, and the write is reported as
    the success it is (D18).** This is deliberately the opposite direction from
    the transfer path, where the same failure strands the token and surfaces as
    `PostPublishFailure`. The asymmetry is retry safety: an upload's source
    bytes are gone, so the ambiguity has to reach the human or it is lost,
    while a note tool that reports a false failure gets *retried* — and
    `edit_note(append=True)` retried after a write that actually landed appends
    the same block twice. A false failure on this path manufactures a
    destructive outcome; on the transfer path it merely wastes a link. The
    payload was flushed before publication either way, so what is unconfirmed
    is only the durability of a directory entry, and the previous content
    survives regardless.
    """
    _flush_target_dirs(target, "destination directory")


def _link_staged_name(dir_fd: int, tmp: str, name: str) -> None:
    """Publish a **named** staging file as `name`, no-clobber.

    The `VAULT_ALLOW_NAMED_STAGING_FALLBACK` fallback for filesystems that
    reject `O_TMPFILE` (`_create_nameless_temp`'s `UnsupportedFilesystem`).
    `link()` creates the destination name or fails `EEXIST` — the same
    no-clobber guarantee `_link_staged_inode` gives — but `tmp` still exists
    afterwards either way, unlike a rename; the caller's `finally` removes it
    via `_discard_temp` (which delegates to `vault_fs.discard_staged_name`,
    the shared implementation the transfer path already uses), same as the
    overwrite path.

    This reopens the exact named-staging race `O_TMPFILE` publication exists
    to close: `tmp` is a real directory entry between staging and this call,
    so another writer to the directory can observe or replace it.
    `_require_staged_name`, called immediately before this by the shared
    caller, narrows that to the single syscall between the check and the
    link — it does not close it. That is the accepted, declared trade-off of
    opting into this fallback; see `Settings.vault_allow_named_staging_fallback`.

    Process-state accounting (the once-per-process warning, `/health`'s
    `vault_named_staging_fallback_active`) is shared with the transfer path
    via `vault_fs.note_named_staging_exercised()` / `named_staging_fallback_active()`
    (D27) — this module does not keep its own copy of that flag.
    """
    try:
        os.link(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd, follow_symlinks=False)
    except FileExistsError:
        raise
    except OSError as exc:
        code = getattr(exc, "errno", None)
        if code == errno.EXDEV:
            # A boundary, not a filesystem without hard links (#110). The note
            # path stages *beside* its destination, so this needs an exotic
            # layout to fire at all — but a message that is wrong whenever it
            # fires is wrong, and the transfer path's equivalent branch
            # (`vault_fs._link_no_clobber`) already says this. One vocabulary
            # across both write paths.
            raise vault_fs.MountBoundary(
                f"{name} is on a different mount from the staging file the "
                "named-staging fallback wrote beside it, so the link that "
                "publishes the write cannot reach it (EXDEV). The filesystem "
                "is fine; the mount layout is what refuses."
            ) from exc
        if code == errno.EOPNOTSUPP:
            raise vault_fs.UnsupportedFilesystem(
                "The vault filesystem does not support hard links, which the "
                "no-clobber write depends on even in named-staging fallback "
                "mode; refusing rather than replacing an existing file."
            ) from exc
        if code == errno.EPERM:
            # Kept apart from `EOPNOTSUPP` deliberately: a seccomp profile, an
            # LSM or a mount option refuses `link` with `EPERM` on filesystems
            # whose hard links work perfectly, and diagnosing the filesystem
            # there is the same defect class as blaming it for a mount layout.
            raise vault_fs.UnsupportedFilesystem(
                "Hard-link publication was denied (EPERM), and the no-clobber "
                "write depends on it even in named-staging fallback mode. "
                "That is not necessarily missing filesystem support: a "
                "security policy (seccomp, an LSM) or a mount option can "
                "refuse `link` where hard links otherwise work. Refusing "
                "rather than replacing an existing file."
            ) from exc
        raise


def _atomic_write_at(
    target: MutableTarget,
    *,
    text: str | None = None,
    data: bytes | None = None,
    overwrite: bool = True,
    expected: bytes | None = None,
    confirmation: RootConfirmation | None = None,
) -> Path:
    """Write `text` (UTF-8) or `data` (raw bytes) to `target`, atomically.

    Shared core for `write_file_at` (notes) and `write_bytes_at` (raw files).
    **Every syscall runs against `target.dir_fd`** — the staging, the
    `expected=` read and the publication — so the destination directory is the
    one validation opened, whatever happens to its pathname meanwhile. Missing
    parent directories are created (through the same anchored walk) on first
    use of the descriptor.

    The two publication modes stage differently, and that difference is the
    whole reason this function has a branch:

    * **no-clobber** (`create_note`, `write_file` by default) stages into an
      `O_TMPFILE` inode that never has a directory entry, and publishes it with
      `linkat` through `/proc/self/fd/<fd>`. Nothing in the directory can be
      observed, replaced or raced, and there is nothing to clean up — the inode
      is freed when the descriptor closes. `link` either creates the name or
      fails `EEXIST`, so nothing can be destroyed by a no-clobber publish.
      When `_create_nameless_temp` reports `UnsupportedFilesystem` (some NFS
      servers reject `O_TMPFILE` outright) and
      `settings.vault_allow_named_staging_fallback` is set, this falls back to
      **named** staging + `_link_staged_name` — the no-clobber write still
      cannot destroy an existing file, but it reopens the named-staging race
      the `O_TMPFILE` form exists to close. Off by default: refuse, as before.
      See `_link_staged_name` for what the fallback does and does not close.
    * **overwrite** (`edit_note`, `set_frontmatter`,
      `write_file(overwrite=True)`) needs `renameat`, which has no
      by-descriptor form, so its source must have a name. That name is created
      `O_CREAT|O_EXCL|O_NOFOLLOW`, and `_require_staged_name` checks
      immediately before the rename that it still refers to the inode we wrote
      — narrowing the substitution window to the single rename syscall rather
      than leaving it open from the `fsync`.

    Ordering, and why each step is where it is:

    1. staging (above); the descriptor is then held until publication, as the
       only handle on the bytes that no rename or unlink can take away;
    2. the payload is written and **`fsync`ed before publication**. A crash
       between the two leaves the destination untouched; without the `fsync` a
       crash just after the rename could leave the destination published but
       its data blocks unwritten, which is the truncation this is supposed to
       make impossible;
    3. `expected` (when given) is compared against the current bytes read
       through the same descriptor — optimistic conflict detection, immediately
       before publication;
    4. the destination directory — and the parent of every directory this call
       created — is **`fsync`ed after publication** (#97). The payload's flush
       makes the contents durable and says nothing about the entry that names
       them, so without this a crash can lose the write entirely. A failure of
       *this* flush is logged and the write reported as the success it is; see
       `_flush_publication` for why that is the opposite of what the transfer
       path does with the same failure.

    **What the overwrite window means, precisely.** An adversary who can write
    to the *destination directory itself* can still win the rename race. That
    adversary can also just edit the note directly, so it is outside the threat
    #59 addresses: redirection through an **ancestor** or the **root**, where
    the attacker never had access to the destination at all. Stated rather than
    implied.
    """
    # #88: before anything is staged and before `dir_fd` can create a missing
    # parent — a refused publication must leave no directories behind. The
    # confirmation is spent here, checked against this target's own user and
    # assignment; there is no way to reach this helper without one.
    _require_confirmation(confirmation, target, "write")

    payload = data if data is not None else (text or "").encode("utf-8")
    dir_fd = target.dir_fd
    name = target.name

    tmp: str | None = None
    if overwrite:
        fd, tmp = _create_temp_exclusively(dir_fd, name)
    else:
        try:
            fd = _create_nameless_temp(dir_fd)
        except vault_fs.UnsupportedFilesystem:
            if not settings.vault_allow_named_staging_fallback:
                raise
            fd, tmp = _create_temp_exclusively(dir_fd, name)
            # **After** the name exists, exactly as the transfer path does it:
            # the signal is "a call actually staged under a name", so a
            # creation that failed every attempt must not spend the warn-once
            # budget or flip `/health` to a fallback this process never took.
            vault_fs.note_named_staging_exercised(vault_fs.NAMED_STAGING_NOTE_PATH)
    # The `try` opens on the very next line after the descriptor exists: an
    # `EIO` from the `fstat` below would otherwise leak the descriptor.
    # `staged` stays `None` until that `fstat` succeeds, and a `None` there is
    # what makes the cleanup **leave** any staging name instead of unlinking
    # it: with no identity, nothing can prove the name still refers to the
    # inode we created, and a write that published nothing must not remove a
    # file that took the name over. Litter, deliberately — see `_discard_temp`.
    staged: os.stat_result | None = None
    published = False
    try:
        staged = os.fstat(fd)
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(payload)
            stream.flush()
        os.fchmod(fd, vault_fs.default_file_mode())
        os.fsync(fd)

        if expected is not None:
            try:
                current = _read_fd_bytes(dir_fd, name)
            except FileNotFoundError:
                raise RuntimeError(f"File changed while editing: {name}") from None
            if current != expected:
                raise RuntimeError(f"File changed while editing: {name}")

        if tmp is not None:
            _require_staged_name(dir_fd, tmp, staged)
            if overwrite:
                try:
                    os.replace(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
                except OSError as exc:
                    if getattr(exc, "errno", None) != errno.EXDEV:
                        raise
                    # #110's other half. Without this the boundary escapes as a
                    # bare `OSError` and the tools render it as "could not
                    # write" with the kernel's own two-word strerror — strictly
                    # less than the no-clobber branch beside it says, for the
                    # same layout.
                    raise vault_fs.MountBoundary(
                        f"{name} is on a different mount from the staging file "
                        "written beside it, so the rename that publishes the "
                        "overwrite cannot reach it (EXDEV). The filesystem is "
                        "fine; the mount layout is what refuses."
                    ) from exc
            else:
                _link_staged_name(dir_fd, tmp, name)
        else:
            _link_staged_inode(fd, dir_fd, name)
        published = True
        # After publication, before returning: the directory entry the publish
        # created is not durable just because the payload is. Never able to
        # fail the write — see `_flush_publication` and D18.
        _flush_publication(target)
    finally:
        if tmp is not None:
            _discard_temp(dir_fd, tmp, staged=staged, published=published)
        # Quiet only once publication has settled: a bare close raising `EIO`
        # here would discard a write that already happened and surface as a
        # generic OSError, which the tools report as a failure — the trap
        # `transfer._close_quietly` exists for. Before publication a close
        # error is real and must not be swallowed.
        if published:
            vault_fs.close_quietly(fd, f"staged copy of {name}")
        else:
            os.close(fd)
    return target.path


# `_proc_fd_available`, `_link_staged_inode`, `_staged_identity_matches` and
# `_require_staged_name` all live in `vault_fs` (#92 item 1, and — as of this
# fallback — the rest of the by-name publication primitives too, closing #105
# in the same move): the transfer publish stages, verifies and discards the
# same way, and a second copy is how the two paths drifted apart before (a
# drift that is exactly how #105 happened — `vault_fs.discard_staged_name`
# already treated an absent staging name as "the publish consumed it, not a
# substitution"; this module's own copy did not, and warned on every
# successful overwrite publish as a result). These names are kept as
# module-level aliases so this file reads as it did — and so a test can still
# hook the publish here without reaching into the shared module.
_proc_fd_available = vault_fs.proc_fd_available
_link_staged_inode = vault_fs.link_staged_inode
_staged_identity_matches = vault_fs.staged_identity_matches
_require_staged_name = vault_fs.require_staged_name


def _discard_temp(
    dir_fd: int,
    tmp: str,
    staged: os.stat_result | None = None,
    *,
    published: bool = False,
) -> None:
    """Remove a **named** staging file — and never anybody else's.

    Delegates to `vault_fs.discard_staged_name`, the same primitive the
    transfer path uses (#105): reached on three kinds of path — a successful
    `renameat` (overwrite, which consumed the name, so this is a no-op), a
    successful `link()` (no-clobber named-staging fallback, which leaves
    `tmp` behind pointing at the same inode as the now-published `name`), and
    a failure after staging on either path. **An absent name is not a
    substitution** — it is the ordinary outcome of the first case — and only
    a name that exists and refers to a *different* inode is treated as one,
    logged and left in place rather than unlinked. Answering an attempted
    substitution by deleting the substitute is the same destructive-write
    class this module exists to prevent, just aimed at a different file, so
    the failure direction is to leave litter rather than remove something we
    cannot prove is ours. A `staged` of `None` — the `fstat` after the
    exclusive creation failed — is the same refusal with even less evidence:
    nothing is unlinked, and the name is left and logged.

    An absent name is quiet only when the write **published**; a staging name
    that disappeared while the write was still in flight is warned about,
    because that is a substitution's first half and not an ordinary outcome.

    The **default** no-clobber path never calls this: it stages into an
    unnamed `O_TMPFILE` inode, so there is no name to remove and no check to
    race. That asymmetry is deliberate — see `_atomic_write_at`. It is only
    reached for no-clobber when `VAULT_ALLOW_NAMED_STAGING_FALLBACK` put a
    name in the directory in the first place — see `_link_staged_name`.
    """
    vault_fs.discard_staged_name(dir_fd, tmp, staged, published=published)


def _bound_read(fd: int, label: str, max_bytes: int | None) -> bytes:
    """Read and bound an already-open regular file. Closes nothing."""
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"Not a regular file: {label}")
    if max_bytes is not None and info.st_size > max_bytes:
        raise ValueError(
            f"File too large: {label} is {info.st_size:,} bytes "
            f"(max {max_bytes:,})"
        )
    with os.fdopen(fd, "rb", closefd=False) as stream:
        payload = stream.read(None if max_bytes is None else max_bytes + 1)
    if max_bytes is not None and len(payload) > max_bytes:
        raise ValueError(f"File too large: {label} exceeds {max_bytes:,} bytes")
    return payload


def _read_fd_bytes(dir_fd: int, name: str, max_bytes: int | None = None) -> bytes:
    """Read `name` inside `dir_fd` through one `O_NOFOLLOW` descriptor.

    The anchored counterpart of `_read_path_bytes`, and the read primitive
    every *mutation* uses: the read and the write that follows it refer to the
    same directory descriptor, so nothing between them can redirect either.
    `O_NOFOLLOW` turns a leaf swapped for a symlink into an `ELOOP` `OSError`
    rather than a silent read of the link's target.
    """
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(name, flags, dir_fd=dir_fd)
    try:
        return _bound_read(fd, name, max_bytes)
    finally:
        os.close(fd)


def _read_path_bytes(path: Path, max_bytes: int | None = None) -> bytes:
    """Read and bound a regular file named by pathname, through one descriptor.

    The **read-only** primitive: `read_bytes` follows symlinks by design, so it
    has no anchored parent to work from. Mutation paths must use
    `_read_fd_bytes` instead — see `MutableTarget`.
    """
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        return _bound_read(fd, path.name, max_bytes)
    finally:
        os.close(fd)


# ────────────────────────────────────────────────────────────────────────────
# Resolve once, then act on the descriptor — the `*_at` helpers
# ────────────────────────────────────────────────────────────────────────────
#
# `open_mutable` resolves the parent exactly once, opens it, and hands back a
# `MutableTarget`. That guarantee only holds if the caller then *uses* the
# target. A tool that validates a string, keeps the verdict, and then calls
# `read_bytes(path_str)` / `write_file(path_str)` re-resolves the caller's
# string a second and a third time; even a tool that keeps the resolved `Path`
# hands that pathname back to the kernel on every syscall, and a parent
# directory renamed out from under it (a symlink dropped at its name) redirects
# the write to a directory nobody validated. The `expected=` compare-and-swap
# does not catch it — the decoy can hold byte-identical content.
#
# So every read-modify-write inside one tool call goes through the helpers
# below, which take an ALREADY-VALIDATED `MutableTarget` and operate purely
# against its parent descriptor. The string-taking `read_bytes` / `write_file`
# / `write_bytes` wrappers remain for single-shot callers and are thin:
# validate, delegate, close.


def write_file_at(
    target: MutableTarget,
    content: str,
    *,
    overwrite: bool = True,
    expected: bytes | None = None,
    confirmation: RootConfirmation | None = None,
) -> Path:
    """Atomically write a note to an already-validated `MutableTarget`.

    `target` MUST come from `open_mutable` — no traversal, visibility or
    symlink check happens here.
    """
    return _atomic_write_at(
        target,
        text=content,
        overwrite=overwrite,
        expected=expected,
        confirmation=confirmation,
    )


def write_bytes_at(
    target: MutableTarget,
    data: bytes,
    overwrite: bool = False,
    *,
    confirmation: RootConfirmation | None = None,
) -> Path:
    """Atomically write raw bytes to an already-validated `MutableTarget`.

    Same contract as `write_file_at`. Raises `FileExistsError` when the target
    exists and `overwrite` is False.
    """
    return _atomic_write_at(
        target, data=data, overwrite=overwrite, confirmation=confirmation
    )


def read_bytes_at(
    target: MutableTarget,
    max_bytes: int | None = None,
    *,
    label: str | None = None,
) -> bytes:
    """Read a validated `MutableTarget` through one `O_NOFOLLOW` fd.

    The counterpart to `write_file_at`: the read and the subsequent write go
    through the same parent descriptor, so nothing between them can redirect
    either. `label` (a vault-relative path) replaces the bare filename in size
    errors so the caller sees the path it passed. Raises `FileNotFoundError`
    when the file is gone, `OSError` (ELOOP) when the leaf has been swapped for
    a symlink, and `ValueError` for a non-regular file or an over-cap size.
    """
    dir_fd = target.parent_fd
    if dir_fd is None:
        # No parent directory, so no file — and a read must not create one.
        raise FileNotFoundError(f"File not found: {label or target.rel}")
    try:
        return _read_fd_bytes(dir_fd, target.name, max_bytes=max_bytes)
    except ValueError as exc:
        if label is None:
            raise
        raise ValueError(str(exc).replace(target.name, label, 1)) from exc


def _refuse_a_cross_mount_move(
    source: MutableTarget, destination: MutableTarget
) -> None:
    """Refuse a forward move whose two ends are definitely on different mounts.

    Best-effort and forward-only, for the same reason the soft delete's twin is
    (#109). `rename(2)` cannot cross a mount boundary at all, so the move fails
    either way; what this buys is that the refusal names the mount layout
    *before* the rename rather than leaving the caller to interpret an `EXDEV`.
    Where `STATX_MNT_ID` is unavailable `vault_fs.cross_mount_definitely`
    answers `False` and the rename's own mapping is the backstop — failing
    closed there would take `move_note` away from a kernel that serves it.

    **It creates nothing, and that is a constraint on how it asks the
    question.** `MutableTarget.dir_fd` creates a missing parent on first use,
    so comparing against *that* would `mkdir` the destination folder and then
    refuse the move — a mutation performed by the check that exists to refuse
    before any mutation. So it uses the never-creating `parent_fd`, and where
    the parent does not exist yet it compares against the destination's deepest
    **existing** ancestor: a directory created beneath an ancestor is created
    on that ancestor's mount, which is the same reasoning
    `vault_fs.require_destination_mount` uses at transfer mint time.

    A source with no open parent is skipped rather than materialised for the
    same reason; such a move has no source to rename and fails `ENOENT` on its
    own.
    """
    src_fd = source.parent_fd
    if src_fd is None:
        return
    dst_fd = destination.parent_fd
    if dst_fd is not None:
        crossed = vault_fs.cross_mount_definitely(src_fd, dst_fd)
    else:
        probe_fd, _rel = vault_fs.deepest_existing_dir(
            destination.root_fd, destination.parent_rel
        )
        try:
            crossed = vault_fs.cross_mount_definitely(src_fd, probe_fd)
        finally:
            vault_fs.close_quietly(
                probe_fd, f"mount check for {destination.rel}"
            )
    if not crossed:
        return
    raise vault_fs.MountBoundary(
        f"Refusing to move {source.rel} → {destination.rel}: the two "
        "directories are on different mounts, so the non-replacing rename "
        "that performs the move cannot cross the boundary. The filesystem is "
        "fine; the mount layout is what refuses — a move relocates the very "
        "inode that sits at the source, and no rename can do that across "
        "mounts. Choose a destination on the source's mount."
    )


def move_file_no_clobber(
    source: MutableTarget,
    destination: MutableTarget,
    *,
    confirmation: RootConfirmation | None = None,
    permit: MovePermit | None = None,
) -> MovePermit:
    """Move one regular file between two validated targets, never replacing.

    One `renameat2(RENAME_NOREPLACE)` against the two parent descriptors. The
    kernel gives both halves at once and both are load-bearing:

    * the destination is *created or refused* — `EEXIST` rather than a silent
      overwrite, whoever put a file there and whenever;
    * whichever inode sits at the source when the call runs is what moves, so a
      writer that replaced the source a microsecond earlier is relocated intact
      rather than destroyed.

    `link` + `unlink` — the shape this replaces — has the first half and not
    the second: it can unlink a *different* inode than the one it linked. See
    `vault_fs.rename_noreplace`; a filesystem that cannot do the non-replacing
    form raises `UnsupportedFilesystem` and there is no safe fallback.

    Two parents on different mounts raise `MountBoundary` instead, naming the
    layout — best-effort before the rename (`_refuse_a_cross_mount_move`) and
    from the rename's own `EXDEV` otherwise. A copying fallback is **not** the
    answer to it: copy-and-unlink relocates a *new* inode and unlinks whatever
    is at the source afterwards, which is precisely the guarantee above that
    the single `renameat2` exists to give.
    """
    # #88. The destination is the publication endpoint, so it is the one the
    # confirmation is checked against. Exactly one of `confirmation` and
    # `permit` authorises the call:
    #
    # * `confirmation` — the forward move. Spent here, and a `MovePermit`
    #   naming these two targets is returned so the caller can undo *this*
    #   move and nothing else.
    # * `permit` — the rollback `_verify_the_moved_inode` performs when what
    #   arrived at the destination is our inode but is a directory or a
    #   symbolic link. It is not a second confirmation and does not claim to
    #   be: it undoes the publication the confirmation covered, synchronously,
    #   inside that same window.
    if (confirmation is None) == (permit is None):
        raise UnconfirmedPublication(
            f"Refusing to move {source.rel} → {destination.rel}: a no-clobber "
            "move takes exactly one of a vault-root confirmation (the forward "
            "move) or a move permit (its rollback) (#88)."
        )
    # Both ends must belong to one caller, one assignment and one pinned root
    # inode, on the forward move **and** on the rollback: the rename removes
    # the source entry as surely as it creates the destination one, and only
    # one confirmation is ever consumed for the pair.
    _require_one_vault(source, destination)
    if permit is not None:
        permit.authorise(source, destination)
    else:
        _require_confirmation(confirmation, destination, "move into")
        # Forward moves only (#109), and after the confirmation bookkeeping, so
        # a refusal here leaves exactly the state any other refused rename does
        # — the confirmation spent, nothing renamed and nothing created.
        #
        # **A rollback never preflights.** It must attempt its rename whatever
        # a mount check says: refusing one strands the note at the destination
        # on the strength of a preflight, and a forward rename that landed is
        # itself proof that both parents share a mount, so the check could only
        # ever misfire there.
        _refuse_a_cross_mount_move(source, destination)

    vault_fs.rename_noreplace(
        source.dir_fd, source.name, destination.dir_fd, destination.name
    )
    # Both ends of the rename, after it has landed (#97). One `renameat2`
    # writes two directory entries — the destination's new one and the
    # source's removal — and a crash that keeps only one of them leaves the
    # note duplicated or gone. Every failure is logged and swallowed, D18's
    # direction: the move *has* happened, and a tool that reported otherwise
    # would be retried against a source that is no longer there. The rollback
    # in `_verify_the_moved_inode` calls this with the targets swapped, so a
    # restore is made durable by the same code.
    _flush_target_dirs(destination, "destination directory")
    vault_fs.flush_dir_quietly(
        source.dir_fd, f"source directory for {source.rel}"
    )
    # The licence to undo exactly this move, and nothing else. Issued only
    # here, only after the rename has landed, and bound to the confirmation's
    # lease — so it is inert the moment the enclosing `confirmed_publication`
    # returns. A rollback of a rollback is refused (the permit is single-use),
    # and so is a permit pointed at any other pair of targets.
    if permit is not None:
        return permit
    return MovePermit(
        _PERMIT_ISSUE,
        confirmation=confirmation,
        source=source,
        destination=destination,
    )


def soft_delete_target(
    target: MutableTarget,
    *,
    stamp: str | None = None,
    label: str | Path | None = None,
    confirmation: RootConfirmation | None = None,
) -> str:
    """Soft-delete a validated target into `.trash/`, confirmation required.

    The thin `vault`-side seam over `vault_fs.soft_delete_at` (#88): the note
    tools used to call the anchored primitive directly, which put a destructive
    publication outside the set of helpers that can refuse an unconfirmed
    target. Semantics are unchanged — one `renameat2(RENAME_NOREPLACE)` from
    the note's own parent descriptor into `.trash`, walked from the same
    resolved root, with the caller still owning both descriptors.
    """
    _require_confirmation(confirmation, target, "soft-delete")
    return vault_fs.soft_delete_at(
        target.dir_fd,
        target.name,
        target.root_fd,
        stamp=stamp,
        label=target.rel if label is None else label,
    )


def unlink_at(
    target: MutableTarget, *, confirmation: RootConfirmation | None = None
) -> None:
    """Permanently unlink a validated target through its parent descriptor.

    `delete_note(permanent=True)` reached a bare
    `os.unlink(target.name, dir_fd=target.dir_fd)` — the only mutating syscall
    left on the `MutableTarget` seam that no publish helper mediated, which
    made "the publish helpers refuse an unconfirmed target" an accurate
    description of five sixths of a destructive-write surface and a false
    description of the whole (#88). Behaviour is otherwise identical: the
    unlink runs against the parent descriptor validation opened, follows no
    symlink, and the directory entry is flushed afterwards because an entry
    that survives a crash resurrects a note the agent was told is gone (#97).
    That flush is logged and swallowed — the note *is* unlinked, and reporting
    a failure would invite a retry of a delete that already happened.
    """
    _require_confirmation(confirmation, target, "permanently delete")
    dir_fd = target.dir_fd
    os.unlink(target.name, dir_fd=dir_fd)
    vault_fs.flush_dir_quietly(dir_fd, f"parent directory of {target.rel}")


def write_file(
    relative_path: str,
    content: str,
    user_id: int | None = None,
    *,
    overwrite: bool = True,
    expected: bytes | None = None,
) -> Path:
    """Write content to a note atomically (tmp file in same dir + `renameat`).

    Validation goes through `open_mutable`, so a symlinked final component is
    refused rather than silently retargeted and the write runs against the
    parent descriptor that validation opened.

    Single-shot convenience only. A tool that already validated the path (or
    that reads before it writes) must use `write_file_at` on its own
    `MutableTarget` instead: re-passing the string here validates and anchors
    a second time, which is exactly the window `open_mutable` closes.

    **Single-user mode only, since #88.** The pre-publish confirmation is a
    database read, which a synchronous function cannot perform; in single-user
    mode there is no user row to disagree and the specification says no query
    is issued, so this keeps working there and refuses everywhere else. See
    `_single_shot_confirmation`.
    """
    with open_mutable(relative_path, user_id=user_id) as target:
        with _leased(_single_shot_confirmation(user_id)) as confirmation:
            return write_file_at(
                target,
                content,
                overwrite=overwrite,
                expected=expected,
                confirmation=confirmation,
            )


def read_bytes(
    relative_path: str,
    user_id: int | None = None,
    max_bytes: int | None = None,
) -> bytes:
    """Read raw bytes of an arbitrary vault file (dot-dirs rejected).

    Validates path + visibility, then reads through a single `O_NOFOLLOW`
    descriptor whose `fstat` bounds the size against `max_bytes` (when given),
    so an over-cap file is refused without loading it into memory. Raises
    `FileNotFoundError` for a missing file and `ValueError` for traversal, a
    hidden path, or an over-cap size (the message reports the actual size and
    path).

    Reads follow symlinks by design (`validate_visible_path` resolves), so this
    is *not* the helper for the read half of a read-modify-write — use
    `read_bytes_at` on the validated `MutableTarget` for that.
    """
    path = validate_visible_path(relative_path, user_id=user_id)
    if not path.is_file():
        raise FileNotFoundError(f"File not found: {relative_path}")
    try:
        return _read_path_bytes(path, max_bytes=max_bytes)
    except ValueError as exc:
        raise ValueError(
            str(exc).replace(path.name, relative_path, 1)
        ) from exc


def write_bytes(
    relative_path: str,
    data: bytes,
    overwrite: bool = False,
    user_id: int | None = None,
) -> Path:
    """Write raw bytes to an arbitrary vault file atomically (dot-dirs rejected).

    Validates path + visibility, enforces no-clobber unless `overwrite`,
    creates any missing parent directories, and routes through the shared
    atomic temp-file + `os.replace` write. Raises `FileExistsError` when the
    target exists and `overwrite` is False, leaving the existing file
    untouched, and `ValueError` when the final component is a symlink
    (`validate_mutable_path`) — writing through an alias would clobber the
    target under a path the caller never named.

    **Single-user mode only, since #88** — see `write_file`.
    """
    with open_mutable(relative_path, user_id=user_id) as target:
        try:
            with _leased(_single_shot_confirmation(user_id)) as confirmation:
                return write_bytes_at(
                    target, data, overwrite=overwrite, confirmation=confirmation
                )
        except FileExistsError:
            raise FileExistsError(
                f"File already exists: {relative_path}"
            ) from None


# ────────────────────────────────────────────────────────────────────────────
# File-access helpers: MIME classification + directory listing
# ────────────────────────────────────────────────────────────────────────────


def _sniff_image_mime(head: bytes) -> str | None:
    """Return an image MIME type if `head` carries a known image signature.

    Covers the common web image formats (PNG, JPEG, GIF, WebP). Used to
    confirm — and to catch mislabeled — images independent of the file
    extension. Returns None when no signature matches.
    """
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def _is_text_like_mime(mime: str | None) -> bool:
    """Whether a MIME type is safe to return as decoded text.

    `text/*` plus the common structured-text application types
    (JSON, JavaScript, XML, and any `*+xml`/`*+json` suffix).
    """
    if not mime:
        return False
    if mime.startswith("text/"):
        return True
    if mime in {
        "application/json",
        "application/javascript",
        "application/xml",
        "application/x-yaml",
        "application/yaml",
    }:
        return True
    return mime.endswith("+xml") or mime.endswith("+json")


def classify_bytes(data: bytes, name: str) -> tuple[str, str]:
    """Classify a file as ``"text"`` / ``"image"`` / ``"other"`` plus its MIME.

    Detection uses a magic-byte sniff (authoritative for images, so a
    mislabeled image still renders and a non-image with an image extension
    does not) and falls back to stdlib `mimetypes` for the text/binary split.
    """
    img_mime = _sniff_image_mime(data[:16])
    if img_mime is not None:
        return "image", img_mime
    mime, _ = mimetypes.guess_type(name)
    if _is_text_like_mime(mime):
        return "text", mime  # type: ignore[return-value]
    return "other", mime or "application/octet-stream"


def list_dir(
    folder: str = ".",
    pattern: str = "*",
    recursive: bool = False,
    limit: int = 200,
    user_id: int | None = None,
) -> tuple[list[dict], bool]:
    """Browse the vault filesystem, excluding dot-directories.

    Returns ``(entries, truncated)``. Each entry is a dict with
    ``path`` (vault-relative POSIX), ``is_dir``, ``size`` (bytes), and
    ``mtime`` (epoch float). Dot-directories — and a dot-directory `folder` —
    are rejected/omitted via `validate_visible_path`.

    Non-recursive (default): immediate children only — subdirectories (always)
    and files whose name matches `pattern`. Recursive: files matching `pattern`
    beneath `folder`, pruning dot-directories. At most `limit` entries are
    returned; `truncated` is True when more matched.
    """
    import fnmatch

    base = validate_visible_path(folder or ".", user_id=user_id)
    if not base.is_dir():
        raise NotADirectoryError(f"Not a directory: {folder}")
    vault = _vault_root(user_id).resolve()

    def _entry(p: Path, is_dir: bool) -> dict:
        st = p.stat()
        return {
            "path": p.resolve().relative_to(vault).as_posix(),
            "is_dir": is_dir,
            "size": st.st_size,
            "mtime": st.st_mtime,
        }

    entries: list[dict] = []
    truncated = False

    if recursive:
        for root, dirnames, filenames in os.walk(base):
            # Prune dot-directories in place so os.walk never descends them.
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            for fname in sorted(filenames):
                if fname.startswith(".") or not fnmatch.fnmatch(fname, pattern):
                    continue
                if len(entries) >= limit:
                    truncated = True
                    return entries, truncated
                entries.append(_entry(Path(root) / fname, False))
    else:
        children = sorted(base.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        for child in children:
            if child.name.startswith("."):
                continue
            is_dir = child.is_dir()
            if not is_dir and not fnmatch.fnmatch(child.name, pattern):
                continue
            if len(entries) >= limit:
                truncated = True
                break
            entries.append(_entry(child, is_dir))

    return entries, truncated


def parse_frontmatter(raw: str) -> tuple[dict, str]:
    """Split YAML frontmatter from content.

    The fence (`---`) MUST be on line 1 (Obsidian's rule). Anything else is
    treated as no frontmatter, even if a `---` fence appears further down.

    Returns `(metadata, body)`. `body` preserves leading whitespace exactly
    as it appears after the closing `---\n`; only a single newline separator
    is consumed.
    """
    if not raw.startswith("---"):
        return {}, raw
    # Require the opening fence to occupy line 1 alone (allow trailing CR).
    first_line_end = raw.find("\n")
    if first_line_end == -1:
        return {}, raw
    first_line = raw[:first_line_end].rstrip("\r")
    if first_line != "---":
        return {}, raw

    # Find the closing fence on its own line.
    rest = raw[first_line_end + 1:]
    closing_re = re.compile(r"(?m)^---[ \t]*\r?$")
    m = closing_re.search(rest)
    if m is None:
        return {}, raw
    yaml_text = rest[:m.start()]
    body_start = m.end()
    # Skip the single newline after the closing fence, if present.
    if body_start < len(rest) and rest[body_start] == "\n":
        body_start += 1
    body = rest[body_start:]
    try:
        fm = yaml.safe_load(yaml_text)
    except yaml.YAMLError:
        return {}, raw
    if not isinstance(fm, dict):
        return {}, raw
    return fm, body


def serialize_frontmatter(meta: dict, body: str) -> str:
    """Re-assemble a note from a frontmatter dict and a body string.

    Empty / missing `meta` → returns `body` unchanged (no fence is emitted).
    Otherwise emits `---\\n<yaml>---\\n<body>`. PyYAML `safe_dump` does NOT
    preserve YAML comments — callers should document this caveat.
    """
    if not meta:
        return body
    yaml_text = yaml.safe_dump(
        meta,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
    )
    return f"---\n{yaml_text}---\n{body}"


_ATX_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)


def _scan_headings(text: str) -> list[dict]:
    """Find ATX headings (1–6 `#`) outside code, with byte positions.

    Returns dicts with `depth`, `text` (trimmed), `line_start` (byte pos at
    the leading `#`) and `line_end` (byte pos at end of heading text, before
    any trailing newline). Code blocks are masked first so headings inside
    fenced or inline code are ignored.
    """
    from src.services.links import mask_code

    masked = mask_code(text)
    out: list[dict] = []
    for m in _ATX_HEADING_RE.finditer(masked):
        out.append({
            "depth": len(m.group(1)),
            "text": m.group(2).strip(),
            "line_start": m.start(),
            "line_end": m.end(),
        })
    return out


def _format_heading_list(headings: list[dict]) -> str:
    if not headings:
        return "(no headings)"
    return "; ".join(f"{'#' * h['depth']} {h['text']}" for h in headings)


def _format_ordinal_choices(candidates: list[int]) -> str:
    """Render candidate indices as the `#N` selectors a caller can pass back."""
    return ", ".join(f"#{i + 1}" for i in candidates)


def _path_chain_match(target_idx: int, headings: list[dict], ancestors: list[str]) -> bool:
    """Check if the heading at `target_idx` has `ancestors` (outermost-first)
    as a leading sequence of its enclosing-heading chain (innermost-first).
    """
    if not ancestors:
        return True
    target = headings[target_idx]
    cur_depth = target["depth"]
    chain: list[str] = []  # innermost-first
    for j in range(target_idx - 1, -1, -1):
        if headings[j]["depth"] < cur_depth:
            chain.append(headings[j]["text"])
            cur_depth = headings[j]["depth"]
    expected = list(reversed(ancestors))
    if len(expected) > len(chain):
        return False
    return chain[: len(expected)] == expected


def _resolve_section_index(
    headings: list[dict], heading: str
) -> tuple[int | None, str | None]:
    """Resolve a heading selector to an index into `headings`.

    Returns `(idx, error)`. `heading` may be plain heading text (`Tasks`), a
    path-style chain (`Parent/Child`) where the last part is the target and the
    preceding parts are ancestors in outermost-first order, or a `#N` ordinal
    (`#7`) selecting the Nth heading in document order, 1-based.

    The ordinal form is the only way to address duplicate *sibling* headings,
    which the path-style form cannot disambiguate because they share ancestors.
    Auto-generated notes hit this routinely (e.g. the same source filename
    extracted twice under one parent).

    **A bare `#N` selector always means the ordinal**, even if some heading's
    literal text is also `#N`. The outline emitted with a truncated read
    advertises these ordinals as the reliable way to reach a section, so they
    cannot be shadowed by note content — otherwise a section could become
    unaddressable by the very selector we told the caller to use. A heading
    genuinely titled `#2` stays reachable two ways: by its own ordinal, or by
    the path-style form (`Parent/#2`), which never takes the ordinal branch.
    """
    if not headings:
        return None, (
            f"Section heading '{heading}' not found: note has no ATX headings."
        )

    ordinal = heading.strip()
    if "/" not in ordinal and ordinal.startswith("#") and ordinal[1:].isdigit():
        n = int(ordinal[1:])
        if not 1 <= n <= len(headings):
            return None, (
                f"Section ordinal '{heading}' is out of range: this note has "
                f"{len(headings)} headings (valid #1–#{len(headings)})."
            )
        return n - 1, None

    if "/" in heading:
        parts = [p.strip() for p in heading.split("/")]
        ancestors = parts[:-1]
        target_text = parts[-1]
        candidates = [
            i for i, h in enumerate(headings)
            if h["text"] == target_text and _path_chain_match(i, headings, ancestors)
        ]
        if not candidates:
            return None, (
                f"Section heading '{heading}' not found. "
                f"Headings present: {_format_heading_list(headings)}."
            )
        if len(candidates) > 1:
            return None, (
                f"Section heading '{heading}' is still ambiguous "
                f"({len(candidates)} matches). Add more ancestors to the path, "
                f"or select one by ordinal: {_format_ordinal_choices(candidates)}."
            )
        return candidates[0], None

    candidates = [i for i, h in enumerate(headings) if h["text"] == heading]
    if not candidates:
        return None, (
            f"Section heading '{heading}' not found. "
            f"Headings present: {_format_heading_list(headings)}."
        )
    if len(candidates) > 1:
        return None, (
            f"Section heading '{heading}' matches {len(candidates)} headings. "
            "Use the path-style form 'Parent/Child' to disambiguate, or select "
            f"one by ordinal: {_format_ordinal_choices(candidates)}."
        )
    return candidates[0], None


def _section_body_span(text: str, headings: list[dict], idx: int) -> tuple[int, int]:
    """Return `(body_start, body_end)` for the section at `idx`.

    The body runs from the line after the matched heading up to (but not
    including) the next heading at depth less than or equal to the matched
    depth, or end of text.
    """
    matched = headings[idx]
    matched_depth = matched["depth"]

    body_end = len(text)
    for j in range(idx + 1, len(headings)):
        if headings[j]["depth"] <= matched_depth:
            body_end = headings[j]["line_start"]
            break

    body_start = matched["line_end"]
    if body_start < len(text) and text[body_start] == "\n":
        body_start += 1
    return body_start, body_end


def outline_sections(text: str) -> list[dict]:
    """Summarise a note's ATX headings without returning their bodies.

    Each entry carries `depth`, `text`, `size` (characters spanned by the
    heading line plus its body, i.e. what `extract_section` would return), and
    `ordinal` (1-based document order, usable as an `#N` section selector).
    Used to let a caller navigate a note too large to return whole.
    """
    headings = _scan_headings(text)
    out: list[dict] = []
    for i, h in enumerate(headings):
        _, body_end = _section_body_span(text, headings, i)
        out.append({
            "depth": h["depth"],
            "text": h["text"],
            "size": body_end - h["line_start"],
            "ordinal": i + 1,
        })
    return out


def extract_section(text: str, heading: str) -> tuple[str | None, str | None]:
    """Return the heading line plus its body for a named ATX heading.

    Returns `(section_text, error)`; exactly one is non-None. Selector syntax
    matches `replace_section`.
    """
    headings = _scan_headings(text)
    idx, err = _resolve_section_index(headings, heading)
    if err is not None:
        return None, err
    _, body_end = _section_body_span(text, headings, idx)
    return text[headings[idx]["line_start"]:body_end], None


def replace_section(text: str, heading: str, new_body: str) -> tuple[str | None, str | None]:
    """Replace the body under a named ATX heading.

    Returns `(new_text, error)`. On success: `error is None`. On failure:
    `new_text is None` and `error` is an actionable message.

    `heading` may be a plain heading text (e.g. `Tasks`) or a path-style
    chain (`Parent/Child`, `Outer/Inner/Leaf`, …) where the last part is
    the target heading and the preceding parts are ancestors in
    outermost-first order. The replacement runs from the line after the
    matched heading up to (but not including) the next heading at depth
    less than or equal to the matched depth, or end of file.
    """
    headings = _scan_headings(text)
    idx, err = _resolve_section_index(headings, heading)
    if err is not None:
        return None, err

    body_start, body_end = _section_body_span(text, headings, idx)
    # `body_end` lands on the next same-or-shallower heading, or end of text.
    next_heading_line_start = body_end if body_end < len(text) else None

    inserted = new_body
    # If the retained prefix lacks a trailing newline (an end-of-file heading
    # with no trailing newline, where the heading match consumed to EOF),
    # the new body would glue directly onto the heading text. Prepend one
    # newline to separate them. No-op when the prefix already ends in "\n".
    prefix = text[:body_start]
    if prefix and not prefix.endswith("\n"):
        inserted = "\n" + inserted
    if next_heading_line_start is not None and not inserted.endswith("\n"):
        inserted = inserted + "\n"

    new_text = text[:body_start] + inserted + text[body_end:]
    return new_text, None


def extract_tags(raw: str, frontmatter: dict) -> list[str]:
    """Extract tags from frontmatter and inline #tags."""
    tags = set()
    # Frontmatter tags
    fm_tags = frontmatter.get("tags", [])
    if isinstance(fm_tags, list):
        tags.update(str(t) for t in fm_tags)
    elif isinstance(fm_tags, str):
        tags.update(t.strip() for t in fm_tags.split(","))
    # Inline #tags (not inside code blocks)
    from src.services.links import mask_code

    masked = mask_code(raw)
    for match in re.finditer(r"(?:^|\s)#([a-zA-Z][a-zA-Z0-9_/-]*)", masked):
        tags.add(match.group(1))
    return sorted(tags)
