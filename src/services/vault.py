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
  none to clean up.
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

import errno
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
        parent_rel: str,
        dir_fd: int | None,
        root_fd: int,
    ) -> None:
        self.path = path
        self.rel = rel
        self.name = name
        self.root = root
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
                "VAULT_ALLOW_NAMED_STAGING_FALLBACK is the operator switch for "
                "this; it governs the transfer path today, and the note path's "
                "half of it is tracked on #103 — until that lands, this write "
                "refuses whether the flag is set or not."
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


def _atomic_write_at(
    target: MutableTarget,
    *,
    text: str | None = None,
    data: bytes | None = None,
    overwrite: bool = True,
    expected: bytes | None = None,
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
    payload = data if data is not None else (text or "").encode("utf-8")
    dir_fd = target.dir_fd
    name = target.name

    tmp: str | None = None
    if overwrite:
        fd, tmp = _create_temp_exclusively(dir_fd, name)
    else:
        fd = _create_nameless_temp(dir_fd)
    # The `try` opens on the very next line after the descriptor exists: an
    # `EIO` from the `fstat` below would otherwise leak both the descriptor
    # and, on the overwrite path, the staging name.
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
            os.replace(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        else:
            _link_staged_inode(fd, dir_fd, name)
        published = True
        # After publication, before returning: the directory entry the publish
        # created is not durable just because the payload is. Never able to
        # fail the write — see `_flush_publication` and D18.
        _flush_publication(target)
    finally:
        if tmp is not None:
            _discard_temp(dir_fd, tmp, staged=staged)
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


def _staged_identity_matches(dir_fd: int, tmp: str, staged: os.stat_result) -> bool:
    """Whether `tmp` still names the inode we staged."""
    try:
        current = os.stat(tmp, dir_fd=dir_fd, follow_symlinks=False)
    except OSError:
        return False
    return (current.st_dev, current.st_ino) == (staged.st_dev, staged.st_ino)


def _require_staged_name(dir_fd: int, tmp: str, staged: os.stat_result) -> None:
    """Refuse unless the temp name still refers to the bytes we just wrote.

    `renameat` moves whatever is at the source name when it runs, so an
    overwrite publish cannot be made to carry an inode the way `linkat` can.
    Checking here does not close the race — it narrows it to the one syscall —
    and it turns the common case of a peer replacing our staging file from
    "their content published as the note" into a refusal.
    """
    if not _staged_identity_matches(dir_fd, tmp, staged):
        raise vault_fs.Conflict(
            "The staged copy was replaced before it could be published; "
            "nothing was written. Retry the operation."
        )


# `_proc_fd_available` and `_link_staged_inode` live in `vault_fs` (#92 item 1):
# the transfer publish stages and publishes the same way, and a second copy is
# how the two paths drifted apart before. These names are kept as module-level
# aliases so this file reads as it did — and so a test can still hook the
# publish here without reaching into the shared module.
_proc_fd_available = vault_fs.proc_fd_available
_link_staged_inode = vault_fs.link_staged_inode


def _discard_temp(
    dir_fd: int, tmp: str, staged: os.stat_result | None = None
) -> None:
    """Remove the **overwrite** path's staging name — and never anybody else's.

    Reached on two kinds of path: a successful `renameat` (which consumed the
    name, so this is a no-op) and a failure after staging. In the failure case
    the name is unlinked only while it still refers to the inode we staged; if
    it does not, the file is **left in place and logged**. Answering an
    attempted substitution by deleting the substitute is the same
    destructive-write class this module exists to prevent, just aimed at a
    different file, so the failure direction is to leave litter rather than
    remove something we cannot prove is ours.

    The no-clobber path never calls this: it stages into an unnamed
    `O_TMPFILE` inode, so there is no name to remove and no check to race.
    That asymmetry is deliberate — see `_atomic_write_at`.
    """
    if staged is not None and not _staged_identity_matches(dir_fd, tmp, staged):
        logger.warning(
            "Temp name %s no longer refers to the file we staged; leaving it "
            "in place rather than unlinking a file we did not create.",
            tmp,
        )
        return
    try:
        os.unlink(tmp, dir_fd=dir_fd)
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning("Could not remove temporary file %s: %s", tmp, exc)


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
) -> Path:
    """Atomically write a note to an already-validated `MutableTarget`.

    `target` MUST come from `open_mutable` — no traversal, visibility or
    symlink check happens here.
    """
    return _atomic_write_at(
        target, text=content, overwrite=overwrite, expected=expected
    )


def write_bytes_at(
    target: MutableTarget,
    data: bytes,
    overwrite: bool = False,
) -> Path:
    """Atomically write raw bytes to an already-validated `MutableTarget`.

    Same contract as `write_file_at`. Raises `FileExistsError` when the target
    exists and `overwrite` is False.
    """
    return _atomic_write_at(target, data=data, overwrite=overwrite)


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


def move_file_no_clobber(source: MutableTarget, destination: MutableTarget) -> None:
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
    """
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
    """
    with open_mutable(relative_path, user_id=user_id) as target:
        return write_file_at(
            target, content, overwrite=overwrite, expected=expected
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
    """
    with open_mutable(relative_path, user_id=user_id) as target:
        try:
            return write_bytes_at(target, data, overwrite=overwrite)
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
