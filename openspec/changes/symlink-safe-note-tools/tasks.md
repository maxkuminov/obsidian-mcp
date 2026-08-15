## 1. Helper
- [ ] 1.1 `src/services/vault.py`: `validate_mutable_path(relative_path, user_id=None) -> Path` — normalise (`PurePosixPath`; reject `..`, absolute, NUL, empty), containment via the existing resolved check, hidden-path check, `os.lstat(vault/rel)` final component → `S_ISLNK` raises `ValueError` naming `os.readlink` target (relative to the vault when possible); returns the lexical `vault/rel`. Unit tests: alias, dangling link, symlinked ancestor inside vault (allowed), escaping ancestor (rejected), hidden, `..`.
## 2. Call sites
- [ ] 2.1 `create_note_impl`, `edit_note_impl` (all modes incl. dry_run: still refuse — no write, but say so), `set_frontmatter_impl`, `move_note_impl` (source + destination; keep DB `file_path` semantics), `delete_note_impl`, `write_file_impl` → use `validate_mutable_path` for the path they mutate; keep reads on `validate_visible_path`. Confirm `_atomic_write` receives the lexical path.
- [ ] 2.2 Docstrings (`server.py`) one sentence each; `CLAUDE.md` write-tools + file-access sections.
## 3. Tests
- [ ] 3.1 Per tool: alias → error names target, target + link byte-identical (mtime too); symlinked dir inside vault → write succeeds; dangling link destination → refused; escaping → traversal error; `read_note`/`read_file` through the alias still work; existing suites green.
## 4. Verify & ship
- [ ] 4.1 `openspec validate symlink-safe-note-tools --strict`; suite.
- [ ] 4.2 `openspec-verifier`; adversarial Codex (write tools). Iterate to no BLOCKER/MAJOR.
- [ ] 4.3 `make deploy`; live e2e per design; record tools called.
- [ ] 4.4 Archive, PR closing #54, merge.
