# Proposal: nested-mount-honest-refusals

## Why

Four follow-ups from the D23 nested-mount work and the #104 named-staging-fallback merge are open (#108, #109, #110, #115), and all four are the same defect class: an operation that fails (or a warning that fires) for a mount-layout or lifecycle reason while the message names a different cause. For this product's consumer — an agent that acts on error text — a wrong cause is actionable misinformation: "the filesystem does not support hard links" sends it to change filesystems when the fix is the mount layout, and a false "staging name disappeared" warning trains an operator to ignore the true substitution warning.

## What Changes

- **#110 — vault-side note-path errno mapping.** The named-staging fallback's no-clobber publish (`vault._link_staged_name`) maps `EXDEV` into a false "does not support hard links" `UnsupportedFilesystem`; the fallback's overwrite publish (`os.replace` in `vault._atomic_write_at`) has no `EXDEV` mapping at all and escapes as a bare `OSError`. Both will raise `vault_fs.MountBoundary` naming the mount boundary, matching the transfer path's vocabulary (the two `vault_fs` sites issue #110's body names were already fixed by group 4; this closes the vault-side pair its final paragraph asks for).
- **#108 — soft delete across a nested mount.** `.trash` is opened beneath the root, so a soft delete of a file on a mount beneath the vault root fails `EXDEV`, surfaced as "`.trash/` cannot receive a non-replacing rename" — blaming a filesystem that renames fine. Add a best-effort early refusal comparing the source parent's mount with `.trash`'s (`vault_fs.same_mount`, the group-4 primitive) that names the mount boundary, and map the residual `EXDEV` out of the rename to `MountBoundary` as the backstop. The delete still fails on such a layout (per-mount trash is explicitly not in scope); the error stops lying, and only cross-boundary deletes are affected.
- **#109 — move_note across a nested-mount boundary.** Same shape: no probe or preflight sees it, and the `renameat2` `EXDEV` is mapped to an `UnsupportedFilesystem` blaming the filesystem. Add `same_mount(source_parent_fd, dest_parent_fd)` before the rename with an accurate `MountBoundary` refusal, and map the residual `EXDEV` too. Copy-based fallbacks stay refused (they break "whichever inode is at the source is what moves").
- **#115 — false "staging name disappeared" warning.** `stream_to_vault`'s outer cleanup discards the staging name via `vault_fs.discard_temp`, which hardcodes `published=False`; after a successful named-fallback publish whose post-publication `fsync` fails (correctly `PostPublishFailure`, claim stranded), the discard warns about a name the publish legitimately consumed. Propagate the actual published state into the discard, as `vault.py`'s own cleanup already does.

Constraint carried through all mount-boundary preflights: `same_mount` refuses to answer where `STATX_MNT_ID` is unavailable. The early refusal is best-effort — where the primitive cannot answer, the operation proceeds and the residual errno mapping is the backstop; degraded kernels must not lose soft delete and move outright.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `vault-write`: note-path named-fallback publish failures across a mount boundary must name the mount boundary, not hard-link support (#110); `move_note` refuses a cross-mount move naming the mount boundary, best-effort before the rename and via errno mapping after (#109); `delete_note`'s soft delete does the same for a source on a different mount than `.trash` (#108).
- `file-access`: `delete_file`'s soft delete inherits the same accurate mount-boundary refusal (#108 — same primitive, `soft_delete_at`).
- `file-transfer`: the named-fallback staging discard after a post-publication failure must not report a legitimately consumed staging name as disappeared (#115).

## Impact

- `src/services/vault.py` — `_link_staged_name` errno mapping; overwrite `os.replace` errno mapping; `move_note`'s publish preflight.
- `src/services/vault_fs.py` — `soft_delete_at` mount preflight + residual `EXDEV` mapping; possibly a small `same_mount`-based helper shared by the move and soft-delete preflights.
- `src/services/transfer.py` — one call site: propagate `state["published"]` into the outer cleanup's discard.
- Tests: extend the existing `tests/_nested_mount_cases.py` namespace harness (#108, #109, #110) plus a fallback-mode unit test for #115.
- No migrations, no API surface changes, no new configuration. Error text and one warning change; agents keying on `UnsupportedFilesystem` are unaffected (`MountBoundary` subclasses it).
