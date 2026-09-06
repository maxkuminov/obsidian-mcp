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
Schema, full integration, deployment, live-tool verification and archive status
must be recorded from their actual results, not inferred from passing slices.
