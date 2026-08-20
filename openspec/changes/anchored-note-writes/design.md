# Design — anchored note writes

## D1. Why the anchored walk may resolve symlinked ancestors first

`vault_fs.open_dir_beneath` refuses a symlinked component outright. That is
right for `/transfer/*`, where the vault-relative path is taken lexically from
the caller and the walk is the only thing that meets a link. It is wrong for
the note tools, where a symlinked in-vault folder (`Shared -> Real`) is a
supported Obsidian layout and #54 explicitly kept it working.

Two ways to reconcile them were available:

1. relax the walk — add a variant that follows a component when it resolves
   inside the vault;
2. resolve the parent first (as `validate_mutable_path` already does), then
   walk the **resolved** path with the strict, unmodified helper.

(2) wins and (1) was rejected. A walk that follows a link has to re-derive
containment *per component*, which is the same class of check-then-act the
change exists to remove — and it would weaken a primitive the transfer routes
depend on. Under (2) the walk sees a `realpath`, which by construction contains
no symlinks, so the strict helper succeeds for exactly the paths the
containment check accepted. If it *does* hit a link, something changed between
`resolve()` and the walk: that is a refusal, not a case to handle.

Consequence worth stating: `resolve()` still runs, so the *ancestor* half of
the guarantee is "the directory that was the resolved parent at validation
time", not "the directory the pathname names now". That is the intended
semantics — the tool acts on what it validated.

## D2. Deferred parent creation

`open_mutable` does **not** create a missing parent. `create_note("New/Folder/x.md")`
validates fine with `dir_fd = None`, and the directory is created by
`MutableTarget.ensure_parent()` on first use of `dir_fd` — which happens inside
the write, after the size check.

Creating at validation time would leave directory trees behind for calls that
are then refused for an unrelated reason (an over-cap body, a permission
error). Reads go through `parent_fd`, which never creates: a read helper that
`mkdir`s is how an absent note becomes a new empty tree.

## D3. `link` for the no-clobber publish, `renameat2` for the moves

Two different publications, two different primitives, deliberately:

- **temp → destination, no-clobber** (`create_note`, `write_file` default) is
  `linkat`. The source is a temp file whose name we created with `O_EXCL` and
  which nothing else can address, so the "unlink the wrong inode" hazard does
  not exist; `link` is kernel-atomic no-clobber and is what `vault_fs.publish`
  already uses. `EPERM`/`EOPNOTSUPP`/`EXDEV` raise `UnsupportedFilesystem` —
  never a fallback to a replacing rename.
- **an existing file → a new name** (`move_note`, the `.trash` rename) is
  `renameat2(RENAME_NOREPLACE)`. Here the source *is* addressable by others, so
  both halves of the syscall's guarantee are needed: the destination is created
  or refused, and whichever inode is at the source when the call runs is what
  moves. `link` + `unlink` has only the first half — it can unlink an inode
  that replaced the one it linked. `ENOSYS`/`EINVAL`/`EXDEV` raise
  `UnsupportedFilesystem`; there is no safe fallback and none is offered.

## D4. Staging beside the destination, not in `.transfer-tmp/`

The transfer paths stage in `<root>/.transfer-tmp/` because an upload streams
for minutes and the destination parent must be re-resolved at publish time. A
note write has no such stream: it is one call, the bytes are in memory, and the
parent descriptor is already open. Staging in the destination directory keeps
the publish a same-directory rename (so `EXDEV` is structurally impossible) and
keeps a crashed write's leftovers next to the note they belong to. The temp
name stays `.tmp-<name>-<pid>-<hex>`, dot-prefixed and therefore invisible to
the indexer.

## D5. `fsync` before publication

Without it, `renameat` can be durable while the temp file's data blocks are
not, so a crash immediately after the publish can leave a note that exists and
is empty or garbage — precisely the truncation the atomic write exists to make
impossible. The `fsync` is on the payload only; the directory is not `fsync`ed,
so a crash may lose the *rename* and leave the previous content. That is the
all-or-nothing guarantee the spec asks for, and the cheaper half.

## D6. The trash-name change is accepted, not worked around

Sharing `vault_fs.soft_delete_at` with `delete_file` means `delete_note` trash
entries become `.trash/<stamp>-<basename>-<8 hex>` instead of
`.trash/<stamp>-<basename>` (with a `-1`, `-2` counter on collision). The
alternative — keeping the old naming — would mean keeping a second, weaker
soft-delete implementation, which is how the two paths drift. The name is not
an interface anything depends on: it is reported back in the tool's response
and the indexer ignores `.trash` entirely. The `delete_note` timestamp stays
UTC (passed in as `stamp=`) so the change is naming only.

## D7. A raced leaf is named, not reported missing

`open_mutable` refuses a symlinked leaf; the tools re-check through the parent
descriptor immediately before acting, because the leaf can be swapped in the
interval. The re-check must not answer "note not found" for a leaf that is now
a link: the obvious next move after "not found" is `create_note`, and a
no-clobber create over a link publishes through it. So `_leaf_state_error`
distinguishes absent / symlink / non-regular and says which.

## D8. Pin the root, then resolve — not the other way round

The first implementation resolved the root to a pathname, computed containment
against it, and only then `open`ed that pathname. Every check therefore rested
on a name that the `open` re-walked: rename the resolved root away, leave a
symlink at its name, and the descriptor the whole call anchors to is a
directory containment never saw.

Opening first inverts the dependency — the root is an inode before any
pathname work happens. The pathname work still happens (resolution is what
lets in-vault symlinked ancestors keep working, D1), so it is *checked against*
the pinned inode: `_require_same_directory` compares `fstat(root_fd)` with
`stat(vault.resolve())` and refuses on a mismatch. A substitution after that
point cannot hurt: `resolved_parent` would then resolve outside
`vault_resolved` and fail the containment check.

## D9. Publish the inode, not the name

`link(tmp, name)` publishes *whatever `tmp` refers to when the call runs*. A
peer with write access to the destination directory could unlink or rename over
`.tmp-…` after the `fsync` and have its own inode published as the note — the
staging file's exclusive `O_CREAT|O_EXCL|O_NOFOLLOW` creation says nothing
about what the name means later.

The staging descriptor is the one handle no rename can take away, so the
no-clobber publish goes through it: `linkat(AT_FDCWD, "/proc/self/fd/<fd>",
dir_fd, name, AT_SYMLINK_FOLLOW)`. Two properties follow:

- what is published is provably the inode we wrote, whatever the staging name
  now says;
- it **fails closed**. Detaching our inode from every name drops its link count
  to zero, and `linkat` on a zero-link inode is `ENOENT` unless the caller holds
  `CAP_DAC_READ_SEARCH`. An attacker can therefore prevent the write, never
  substitute it. (Verified experimentally, both directions: unlink and
  rename-over both produce `ENOENT`.)

`/proc` is required, which the Linux-only declared semantics already assume; if
it is absent we raise `UnsupportedFilesystem` rather than fall back to the
by-name form the review rejected.

The **overwrite** publish cannot use this: `renameat` has no by-descriptor form
(`RENAME_EXCHANGE` does not help — it still names the source). It is preceded
by an identity check instead, which narrows the window to the single rename
syscall rather than leaving it open across the whole fsync-to-publish interval.

The mirror hazard is real and handled: `_discard_temp` runs on every path, so
unlinking the staging name blindly would answer an attempted substitution by
*deleting the substitute* — the same destructive-write class, aimed at a
different file. It unlinks only while the name still refers to our inode.

## D10. What the overwrite window actually costs

Stated rather than implied, because "narrowed" is not "closed": an adversary
who can write to the **destination directory itself** can still win the
`renameat` race on an overwrite. That adversary can equally well open the note
and rewrite it — no race required. It is therefore outside the threat #59
addresses, which is redirection through an **ancestor** or the **root**, where
the attacker never had access to the destination at all. The no-clobber publish
has no such window in any case.

## D11. `move_note` verifies what moved, and refuses links too

`renameat2` relocates whichever inode sits at the source when it runs. That is
the property that stops a file which replaced the source from being destroyed
(D3) — and it means the regular-file check performed before the preflight does
not bind the commit. A directory or a symlink dropped at the source in between
is what actually moves, and the tool would report "Moved" and key
`notes_metadata` to it.

So the destination is `lstat`ed through its parent descriptor after the rename
and a non-regular result is rolled back with a second `RENAME_NOREPLACE`, the
same shape `vault_fs._refuse_a_moved_directory` uses. The database is never
touched: the tool returns first. If the rollback loses the source name, the
error says where the object is so it can be recovered by hand.

`soft_delete` deliberately lets a **symlink** ride into `.trash` — a link is
inert there and was never followed. A move is different: a link published at
the destination becomes what the index points at, and the caller is told a note
moved that no longer exists. So `move_note` refuses both kinds.
