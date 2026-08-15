## 1. Helper
- [x] 1.1 `src/services/vault.py`: `validate_mutable_path(relative_path, user_id=None) -> Path` per design D2 — normalise (`PurePosixPath`; reject `..`, absolute, NUL, empty, trailing slash); `resolved_parent = (vault/rel).parent.resolve()` must be inside `vault.resolve()` (else the existing traversal error); hidden-path check on the resolved relative path; `os.lstat(resolved_parent/name)` → `S_ISLNK` raises `ValueError` naming the canonical vault-relative target (via `os.path.realpath`, relative to the vault when inside, else 'outside the vault'); returns `resolved_parent/name`. Unit tests: alias (canonical target text incl. nested `Folder/alias.md -> ../real.md`), dangling link, symlinked ancestor inside vault → returns `Real/name`, escaping ancestor rejected, hidden, `..`, per-user root.
## 2. Call sites
- [ ] 2.1 `create_note_impl`, `edit_note_impl` (all modes incl. dry_run: still refuse — no write, but say so), `set_frontmatter_impl`, `move_note_impl` (source + destination; `from_rel`/`to_rel` for DB/link work derived from the returned resolved paths relative to the vault root), `delete_note_impl`, `write_file_impl` → use `validate_mutable_path` for the path they mutate; keep reads on `validate_visible_path`. Confirm `_atomic_write` receives `resolved_parent/name`.
- [ ] 2.2 Docstrings (`server.py`) one sentence each; `CLAUDE.md` write-tools + file-access sections.
## 3. Tests
- [ ] 3.1 Per tool: alias → error names canonical target, target + link byte-identical (mtime too); symlinked dir inside vault → write lands in the real dir; move through a symlinked folder → file at `Real/B.md`, fake-session journal shows `notes_metadata`/`note_links` updates keyed on `Real/A.md`→`Real/B.md` and backlink rewrite discovery uses the resolved path; dangling link destination → refused; escaping → traversal error; `read_note`/`read_file` through the alias still work; multi-user root case; existing suites green.
## 4. Verify & ship
- [ ] 4.1 `openspec validate symlink-safe-note-tools --strict`; suite.
- [ ] 4.2 `openspec-verifier`; adversarial Codex (write tools). Iterate to no BLOCKER/MAJOR.
- [ ] 4.3 `make deploy`; live e2e per design; record tools called.
- [ ] 4.4 Archive, PR closing #54, merge.
