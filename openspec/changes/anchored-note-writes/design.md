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
