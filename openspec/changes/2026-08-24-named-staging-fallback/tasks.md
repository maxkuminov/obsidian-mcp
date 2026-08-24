## 1. Opt-in fallback (#103)

- [x] 1.1 `src/config.py`: `Settings.vault_allow_named_staging_fallback: bool = False` (env `VAULT_ALLOW_NAMED_STAGING_FALLBACK`)
- [x] 1.2 `src/services/vault.py::_atomic_write_at`: on `UnsupportedFilesystem` from `_create_nameless_temp`, fall back to `_create_temp_exclusively` + `_link_staged_name` only when the flag is set; re-raise otherwise
- [x] 1.3 `_create_nameless_temp`'s refusal error names `VAULT_ALLOW_NAMED_STAGING_FALLBACK`
- [x] 1.4 Confirm the fallback stages through `MutableTarget.dir_fd` (the pinned parent descriptor from `open_mutable`), not a re-resolved pathname — no new descriptor-open call added

## 2. Observability (#103)

- [x] 2.1 `named_staging_fallback_active()` / `_warn_named_staging_fallback_once()`: module-level flag, warns exactly once per process on first actual fallback use (not on flag-set)
- [x] 2.2 `src/main.py::health`: `vault_named_staging_fallback_active` field

## 3. Tests (#103)

- [x] 3.1 `test_the_named_staging_fallback_is_off_by_default`
- [x] 3.2 `test_the_named_staging_fallback_still_refuses_to_clobber`
- [x] 3.3 No staging litter left under either outcome
- [x] 3.4 Warns exactly once across repeated writes

## 4. Spec

- [x] 4.1 Spec delta under `specs/vault-write/` (MODIFIED: Atomic write invariant)
- [ ] 4.2 `openspec validate named-staging-fallback --strict` passes (no local `openspec` CLI available in this environment — validated by hand against the format of prior archived changes)

## 5. Upstream PR (maxkuminov/obsidian-mcp#103)

- [ ] 5.1 Carry this OpenSpec delta in the PR
- [ ] 5.2 Note in the PR description that the staged name is created through the pinned `open_mutable` parent descriptor (ask 2)
- [ ] 5.3 Acknowledge the write-path adversarial review gate in the PR description (ask 3)
