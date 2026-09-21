# Issue sweep recovery — 2026-09-06

Resumed the interrupted issue sweep from `c583428`, preserving Claude's
worktrees. The recovery branch incorporates PRs #266, #267 and #268, the
uncommitted index-integrity follow-up, and the missing raw-file portion of
`write-preconditions`.

## Current batch

| Issues | Work | Verification status |
| --- | --- | --- |
| #205, #154 | Note/raw-file write preconditions and non-finite frontmatter boundary | Independent implementation/specification reviews passed; 4,644-test merged offline suite passed |
| #198, #197, #183 | Session lifecycle, password change and consent disclosure | Session failure fix reviewed; merged offline suite passed |
| #194, #188 | Rate limits and refusal accounting | Independent review passed after cancellation, eviction and claim-release fixes; concurrency remains #261 |
| #201, #200, #202, #206 | Index outcomes, stale-result disclosure, fair embedding work and configuration fingerprints | Independent implementation review passed after actual reset-DDL deadlock regression |
| #190–#193 | Structured security events and failure attribution | Independent review passed within accepted scope, including the existing R10 limitation; deployment pending |
| #219 | Source inode witness retained through move verification | Flake traced to inode reuse; proposal and implementation reviews passed (185 focused tests) |
| #220 | Archived sweep imports | Both wrappers run: 332/316 color declarations, no literals outside tokens |
| #244 | Load-sensitive scanner ratio | CPU-time ratio retains absolute wall-time ceilings; 109 scanner tests passed |

The first merged offline run found four failures: two test doubles/assertions
needed to reflect index discovery transactions, and the guarded raw deletion
needed to use a shared filesystem helper. The final run passed: **4,644 passed, 556 skipped**, with two existing
dependency warnings. The skipped database cases run separately under
`make test-integration`; the first run is not a passing gate.

## Remaining triage

These remain open; they are not silently accepted or marked fixed by this batch.

| Issues | Disposition |
| --- | --- |
| #218 | Confirmed graph accuracy follow-up: reject targets decided by code masking, with an extraction-version bump and dedicated specification |
| #261 | Separate concurrency design; existing requirement is shadow mode before enforcement |
| #262 | Owner decision remains open for nested mount grafts; current overlap guard does not detect them |
| #263 | Separate typed in-body outcome contract; existing logging does not classify every body refusal |
| #184, #185 | Database/internal transport TLS needs coordinated shared-service configuration |
| #189 | Proxy trust configuration needs coordinated deployment/network changes |
| #196 | Plaintext API rejection needs proxy routing changes; redirecting does not prevent first-hop credential exposure |
| #195 | Low-priority CSP follow-up; current no-CSP decision remains documented, no injection primitive established in the assessment |
| #178 | External registry listing updates remain outstanding |

## Rollout checks

Read-only production preflight: zero ownerless notes; both retained user scopes
are active, assigned and have settled provenance. Production was on revision
022 before this rollout. The dependency audit found no known vulnerabilities.
Completed release checks on runtime commit `2db9fde`:

- Full real-PostgreSQL integration suite: **553 passed** (one existing warning).
- Explicit schema gate: **182 passed**, including migration/model checks.
- All five GitHub checks passed: tests, schema gate, audit, OpenSpec and Trivy.
- Release image built and scanned: no fixable HIGH/CRITICAL vulnerabilities.
- The staged image migrated a disposable database and started healthy. Real HTTP
  authentication smoke passed logout replay rejection, password change, sibling
  session revocation and retention of the changing session. Synthetic account
  cleanup and disposal of the isolated environment completed successfully.

Staged registry digest:
`sha256:a90b136f4a3d6494bad50e63a9eb3d4ff89095f8033e9e0dc8fdef08ea44d41f`.
The previous production image is retained under the local rollback tag
`obsidian-mcp:pre-codex-20260906`. A database backup is still required before
migration; an image rollback alone does not reverse schema changes.

The user explicitly authorized production deployment and restart after the
initial approval-review rejection. Deployment then completed successfully:
a fresh compressed database backup was taken, migrations advanced from 022 to
**024 (head)**, and the service was recreated using the tested image.
`make db-check` reported **No new upgrade operations detected**. Docker health
is healthy, `WRITE_PRECONDITION_REQUIRED` remains false, and sandbox mode is
false. The rollback image remains retained.

Live MCP smoke passed **314 assertions**, exercising `create_note`, `read_note`,
`edit_note`, `set_frontmatter`, `read_file`, `write_file`, `move_note`,
`delete_note`, `delete_file`, `keyword_search`, `semantic_search`, `get_links`
and `list_notes`. Matching/stale/malformed preconditions, refusal immutability,
read hashes and move destination hashes passed. All owned test files were
removed; an empty diagnostics directory may remain.

Live session smoke passed real HTTP logout replay rejection, password change,
sibling-session revocation and retention of the changing session. The synthetic
account and all its owned authentication rows were removed. These checks do
not claim the entire outstanding live matrix of every pending change.

Post-startup logs contained no ERROR/CRITICAL events. Initial embedding and FTS
fingerprint adoption warnings were expected for the new state table; both stored
fingerprints match the running configuration. Embedding warm-up completed and
the periodic indexer started. Two completed runs scanned 3,951 notes with zero
run errors. Historical embedding provenance remains assumed under the existing
adoption contract. Strict OpenSpec validation after the move archive passed
all 32 remaining items.
Existing browser sessions need to sign in again. Confirmation from both
existing users remains an owner task; a synthetic-account smoke does not
satisfy that confirmation.

Overlapping pending OpenSpec deltas now carry the same union of schema and
latency requirements so later archival cannot erase a sibling change's
scenarios. The completed `move-inode-lifetime` change is archived after its live check.
Other changes remain active with their outstanding gates and owner decisions;
no issues have been closed by this recovery PR yet.

## Archive — 2026-09-21

Bookkeeping only: no `src/`, `tests/`, `alembic/`, Dockerfile or compose change.
The seven changes deployed on 2026-09-06 were archived sequentially, each
followed by `openspec validate --all --strict` and a diff of the touched
`openspec/specs/**/spec.md`. **No requirement or scenario that existed before an
archive was lost, and no near-duplicate requirement was added.** The harmonized
overlaps held: the three `panel-performance-views` "Per-tool latency aggregates"
MODIFIED blocks and the two `schema-integrity` "The schema gate covers both
migrations of this wave before deploy" MODIFIED blocks were byte-identical, so
the later archives applied them as no-ops (`panel-sessions-and-consent` task 8.6
confirmed in place). The one superseded scenario is `schema-integrity`'s
"Head at 017", deliberately replaced by "The gate asserts the current head and
023 is in the chain", "Head at 024" and "The earlier waves' cases still run".
No delta header or requirement name needed fixing. Final validation: **30
passed, 0 failed**.

Archived, in order: `reject-mask-decided-links`, `security-event-logging`,
`vault-root-overlap-guard`, `write-preconditions`, `mcp-rate-limits`,
`index-integrity-hardening`, `panel-sessions-and-consent`.

Stale checkboxes were reconciled against this record, `git log` and the issue
tracker. Tasks that the record does **not** evidence were left unchecked and
annotated in place rather than ticked:

- **reject-mask-decided-links** — 7: deployed, but no record of version-3
  re-derivation being confirmed or of a live graph-tool exercise; the fix merged
  as PR #270 after the release image was built.
- **write-preconditions** — 5.8: the ASVS report row for #205 in the vault.
- **panel-sessions-and-consent** — 8.4: production-caller grep for the nine new
  exports; 8.5: the mint-site / validate-entry-point / second-`AsyncSession`
  enumeration on the merged tree; 8.10: the browser pass with a replayed cookie
  and the `/authorize` consent flows (the live smoke was synthetic-account HTTP
  only); 8.11: sign-in confirmation from both production users, which this
  document already records as an outstanding owner task.
- **security-event-logging** — 5.5: `make logs` inspected for one JSON object
  per line and populated `auth_failure` fields (the deploy and clean
  `make db-check` are evidenced); 5.6: the read-only-credential refusal exercise
  and the `/admin/usage` confirmation; 5.7: the tool-exception path; 5.8: the
  ASVS vault rows and residuals R1–R9 as follow-up issues.
- **vault-root-overlap-guard** — 8.9: the panel users-page live check against
  production's two users, including the nested-path refusal and restore.
- **mcp-rate-limits** — 6.6: the auth-failure WARNING and boot-time settings
  validation in `make logs`; 6.7: the whole live rate-limit exercise (8,193-char
  query, bucket bursts, transfer 429, unauthenticated burst,
  `/admin/performance`, restart flush); 6.8: the operator follow-up setting a
  daily limit on the five pre-existing keys; 6.10: #194 and #188 are still open
  and the accepted residuals are not recorded on #194; 7.4: follows from those.
- **index-integrity-hardening** — 8.9: the deterministic stale-embedding
  exercise with the provider blocked; 8.10: the post-deploy dashboard pending
  count and coverage bar; 8.11: the ASVS vault rows and L1/L2/L3/L10 as
  follow-up issues.

Follow-up issues that **were** filed, so those tasks are ticked: #261
(`mcp-concurrency-slots`, since shipped and closed), #262
(`vault-root-mount-graft-detection`, open pending the owner's L1/L2 decision)
and #263 (typed in-band tool outcomes, since shipped and closed).
