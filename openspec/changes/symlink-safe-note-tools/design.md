## Context
`validate_path` returns `(vault/rel).resolve()`; `validate_visible_path` adds the dot-dir check on the resolved relative path. Every write then uses that resolved `Path`, so a symlink is transparently followed. `delete_file` (#50) instead canonicalises lexically and walks with `O_NOFOLLOW`. Migrating all note writes to the anchored helper is the long-term follow-up recorded in CLAUDE.md; this change is the minimal, safe step: refuse the one dangerous shape.

## Goals / Non-Goals
Goals: no mutating tool can act on a file other than the one named; symlinked directories inside the vault keep working; clear error messages. Non-goals: anchored (`dir_fd`) rewrite of `_atomic_write`; changing read behaviour; symlink support policy for the indexer.

## Decisions
1. **Refuse symlinked final component; allow symlinked ancestors that resolve inside the vault.** Refusing ancestors would break shared-attachment-folder setups; the data-loss vector is the final component (act on B while naming A). Ancestor links resolving *outside* the vault are already rejected by containment.
2. **Return the lexical path for writes.** `validate_mutable_path` normalises `rel` with `PurePosixPath` (reject `..`, absolute, NUL, empty), lstat's `vault/rel` (final component), and returns `vault/rel` (lexical) — so `_atomic_write`'s temp file lands in the named directory and `os.replace`/`link` target the named entry. Containment still verified on the resolved form.
3. **Error text names the target**: `"alias.md is a symbolic link to important.md — mutating tools act only on the named file; operate on the target instead."`
4. **Dangling symlink at a destination** (create/write/move-to): refuse (it is a symlink) — no write.
5. **`move_note` destination:** refuse if the destination's final component is a symlink (existing no-clobber would otherwise fail on the link name anyway, but message clarity matters); source symlink refused.
6. **Reads unchanged** — documented; `read_note` of an alias returns the target's content as before.

## Risks / Trade-offs
- [Users who edit through aliases lose that ability] → they get a message naming the real path; safer than silent retargeting.
- [Lexical vs resolved mismatch elsewhere (e.g. DB `file_path` uses the lexical rel)] → the DB already stores the *named* relative path; this change makes writes consistent with it.
- [TOCTOU: link swapped in after lstat] → same optimistic level as the rest of the note tools (documented; the anchored helper migration closes it later).

## Migration Plan
Deploy; post-deploy e2e via live tools: create a symlink in a scratch folder on the host, `edit_note`/`delete_note`/`set_frontmatter`/`write_file` on it → refused, target byte-identical; `read_note` still works; a symlinked scratch dir inside the vault still accepts `create_note`.
