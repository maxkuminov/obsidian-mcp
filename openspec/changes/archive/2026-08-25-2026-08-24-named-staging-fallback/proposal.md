## Why

Issue #103: `_create_nameless_temp()` stages no-clobber writes (`create_note`,
`write_file(overwrite=False)`) into an unnamed `O_TMPFILE` inode and refuses
with `UnsupportedFilesystem` when the kernel rejects `O_TMPFILE`
(`EOPNOTSUPP`/`EISDIR`/`ENOSYS`/`EINVAL`). On a real production vault mount —
TrueNAS SCALE 25.10.5/25.10.6, NFS export over NFSv4.1 and NFSv4.2 — that
refusal fires on every no-clobber write. Verified against the live export, not
guessed: `EOPNOTSUPP` (errno 95) as non-root and as root (rules out
permissions), reproduced on a second unrelated export (server-wide, not one
dataset), reproduced fresh under both NFS minor versions, and absent on local
ext4 on the same client kernel. The overwrite path (`edit_note`,
`set_frontmatter`, `write_file(overwrite=True)`), which stages via
`O_CREAT|O_EXCL` named temp + `renameat` rather than `O_TMPFILE`, was tested by
hand against the identical mount and works — so named staging + rename is a
proven-working mechanism on this filesystem; only the unnamed-inode path is
blocked. A vault on such a mount could not `create_note` or
`write_file(overwrite=False)` at all.

The refusal itself is deliberate — see the "threat #59" write-up in
`src/services/vault.py` — and stays the default. This change adds an opt-in
escape valve rather than weakening the default guarantee.

## What Changes

- `Settings.vault_allow_named_staging_fallback` (env
  `VAULT_ALLOW_NAMED_STAGING_FALLBACK`), off by default. Refusal behavior is
  completely unchanged unless an operator turns this on.
- When set, `_atomic_write_at`'s no-clobber branch falls back to **named**
  staging (`_create_temp_exclusively`, the same primitive the overwrite path
  already uses successfully on this filesystem) and publishes with `link()`
  instead of `linkat` through `/proc/self/fd` — still no-clobber (`EEXIST` on
  an existing destination) — when `_create_nameless_temp` reports
  `UnsupportedFilesystem`. This reopens the named-staging substitution window
  `O_TMPFILE` staging exists to close (the parent directory holds a real,
  observable entry between staging and publish).
- The staged name is still created `O_CREAT|O_EXCL|O_NOFOLLOW` through the
  same pinned parent descriptor (`MutableTarget.dir_fd`, from `open_mutable`)
  that every other mutating write already uses — the fallback inherits the
  anchored-write guarantees; it does not resolve any pathname the kernel
  hasn't already been handed.
- The trade-off is declared, not silent: a `WARNING` log fires exactly once
  per process the first time the fallback is actually *exercised* (not merely
  when the flag is set — `named_staging_fallback_active()` /
  `_warn_named_staging_fallback_once()`), `/health` gains a
  `vault_named_staging_fallback_active` boolean, and the refusal error (flag
  off) names the env var so an operator doesn't have to source-dive to find
  it.
- Tests in `tests/test_anchored_note_writes.py`: default-off behavior
  unchanged, fallback still refuses to clobber, no staging litter under
  either outcome, warns exactly once across repeated writes.

Hardening applied at this repo's pre-merge adversarial gate (#104 review),
all of it in the shared cleanup primitive both write paths already use, so the
transfer path gets the same treatment:

- the cleanup **never unlinks a staging name it cannot prove is its own**. An
  `fstat` that failed after the exclusive creation leaves no identity to
  compare against, and unlinking on that basis let a no-clobber write that
  published nothing destroy a file that had taken the name over. It now warns
  and leaves the litter, the same direction an identified substitute already
  got.
- an **absent** staging name is quiet only when the write published (a
  `renameat` consumed it); a name that vanished mid-flight is warned about and
  reported as a failed discard.
- the once-per-process signal is spent **after** the staging name exists, so a
  creation that failed every attempt neither warns nor flips `/health`.
- the one warning no longer attributes `.transfer-tmp` to the note path: it
  takes the exercising path kind and names where each path stages, the note
  path's window being the wider of the two.

Filed as maxkuminov/obsidian-mcp#103 first (design conversation before a PR);
accepted as shaped, with three follow-ups this change also covers: (1) this
OpenSpec delta, (2) confirming the fallback stages through the pinned parent
descriptor rather than reopening a pathname, (3) flagging the write-path
adversarial review this repo runs on changes in this area.

On the accepted residual: the substitution window this reopens requires an
adversary with write access to the **destination directory** — the same
adversary the overwrite path's residual already accepts (see "threat #59"),
on the grounds that such an adversary could simply edit the note directly.
This is that same declared residual, extended to the no-clobber path behind
an explicit operator opt-in, not a new threat.

## Impact

- `src/config.py` — new `Settings` field.
- `src/services/vault.py` — fallback branch in `_atomic_write_at`,
  `_link_staged_name`, `named_staging_fallback_active`,
  `_warn_named_staging_fallback_once`.
- `src/main.py` — `/health` field.
- `tests/test_anchored_note_writes.py` — new tests.
- No database, migration, or API-surface change. No change to the overwrite
  path or to default (flag-off) behavior.
