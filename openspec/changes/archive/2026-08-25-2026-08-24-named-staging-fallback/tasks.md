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
- [x] 4.2 `openspec validate 2026-08-24-named-staging-fallback --strict` passes (run at the merge gate, where the CLI is available)
- [x] 4.3 The MODIFIED requirement is rebased onto the **current** `openspec/specs/vault-write/spec.md` text, not the base the PR was written against: every paragraph and scenario promoted since (complete ancestor-chain durability, move/delete/rollback flushes, backlink-rewrite root sharing, `write_file` coverage, the split crash scenarios) is preserved verbatim, and only the unconditional no-name/refusal clauses change

## 5. Upstream PR (maxkuminov/obsidian-mcp#103)

- [x] 5.1 Carry this OpenSpec delta in the PR
- [x] 5.2 Note in the PR description that the staged name is created through the pinned `open_mutable` parent descriptor (ask 2)
- [x] 5.3 Acknowledge the write-path adversarial review gate in the PR description (ask 3)

## 6. Pre-merge adversarial gate (this repo's write-path review)

- [x] 6.1 BLOCKER: `vault_fs.discard_staged_name` refuses to unlink when
  `staged is None` — an `fstat` that failed after the exclusive creation left
  the cleanup with no identity, and unlinking on that basis let a no-clobber
  write destroy a concurrent replacement. Fail closed: warn, leave the litter.
  Fixed in the shared primitive, so the transfer path's own `staged=None`
  shapes (`discard_temp` after a failed `fstat`, `publish`'s fallback `lstat`)
  are covered by the same change
- [x] 6.2 MINOR: an absent staging name is quiet only when `published` — an
  unpublished disappearance is warned about and reported as a failed discard
- [x] 6.3 MINOR: `note_named_staging_exercised()` is called **after**
  `_create_temp_exclusively` succeeds, so a creation that failed every attempt
  neither spends the warn-once budget nor flips `/health`
- [x] 6.4 MINOR: the shared warning no longer claims `.transfer-tmp` for the
  note path — it takes the exercising path kind and names both locations,
  keeping warn-once semantics
- [x] 6.5 Regression tests for each, including the concurrent replacement over
  the staging name on the fallback path


## Archive record (2026-08-24)

All three PR-process tasks were satisfied by PR #104 as submitted (the delta
was carried, the pinned-descriptor staging stated, the gate acknowledged).
The gate then ran on the merged result: 1 BLOCKER + 1 MAJOR + 3 MINOR, fixed
forward in `b2ee20d` (round-2 verdict PASS with one cosmetic residual, filed
as #115). Merged via #114 with the contributor's commits intact; deployed;
`/health` reports the fallback inactive on the production ext4 mount, which
is the expected reading — the live exercise of the fallback itself belongs to
an O_TMPFILE-less filesystem, and the contributor's TrueNAS deployment is the
real one; the suite's fallback tests (both no-clobber semantics and the
fail-closed cleanup) stand in for it here, and #103 asks for their
confirmation. #105 closed by this change's consolidation.
