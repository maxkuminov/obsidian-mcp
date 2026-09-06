# Tool outcomes and concurrency admission (#263, #261)

## Scope

#263 gives every terminal tool-body refusal or partial result an explicit
internal outcome. Caller prose is preserved with a final machine-readable
refusal line. Usage classification reads the typed result, never note text.
Successful empty results and no-op writes stay successful; partial publications
retain their actual write semantics. The complete 25-tool inventory accompanies
the OpenSpec change.

#261 adds bounded admission for full MCP requests, authentication sessions,
tools and usage writers. The rollout defaults to **shadow**: observations do not
reject or delay calls, consume additional quota, or replace the actual outcome.
Enforcement is exercised in isolated tests. Its connection budget reserves no
connections against panel, OAuth, indexer or transfer consumers; the documented
headroom can still be exhausted by those consumers.

The branch is based on main independently of #218's existing PR #270. There
are no schema migrations in this change.

## Review and validation

The available Codex agents supplied independent proposal, specification and
defensive implementation reviews. Opus and the named Claude workflow agents
were unavailable; no cross-family or unavailable named gate is claimed.

- #263 proposal: PASS. Implementation round 1 found a regression-test gap in
  partial-publication outcomes. Round 2 passed after adding typed outcomes and
  actual usage assertions to those fault-injection tests, plus the 25-tool
  coverage registry. Independent focused run: 399 passed.
- #261 proposal: PASS after fixing shared-overflow ownership when dedicated
  registry capacity becomes available. Independent core review: PASS; 25
  controller/auth/config tests passed.
- Combined #261 integration and #263 interfaces: independent PASS, 217 focused
  tests passed. The PostgreSQL coverage audit also passed: actual checkouts,
  auth, quota, named publication, move/import, writer contention/FK retry,
  cancellation and persisted actual/shadow outcomes.
- Full offline testing exposed controller shutdown state leaking between tests.
  An explicit test-boundary reset fixed the reproduced lifecycle-to-quota
  failure without changing production behavior. Independent review passed and
  its ordered regression sequence passed 120 tests. The full suite is rerun
  after this correction. The remaining obsolete exact-parameter assertion was
  updated to require the new closed disposition field; its independently run
  module passed 91 tests (one documented skip).
- Dependency audit: `make audit` passed with no known vulnerabilities.
- Strict OpenSpec validation: 34 items passed before archival; 33 items passed
  after both changes were archived. The relocated inventory registry test also
  passed. Runtime source did not change after the full gates or image build.

## Full validation

- Offline: `OMCP_ALLOW_SKIP_TRANSFER_INTEGRATION=1 .venv/bin/python -m pytest
  -q -rs tests` — **4,747 passed, 563 skipped**, two existing library warnings,
  198.82 seconds. Database-only tests are covered by the separate mandatory
  PostgreSQL run; the explicit transfer skip applies only to this offline run.
- Image: `make build trivy` passed, with no fixable High/Critical findings.
  Candidate image: `sha256:fbb65bdcee03a9aa0c838a8bb66a6bee37b86994bbfaf1a106f56048aedb6bb4`.

- PostgreSQL: `make test-integration SCHEMA_TEST_CONTAINER=obsidian-261-263-integration
  SCHEMA_TEST_PORT=55440` — **560 passed**, one existing settings warning,
  822.06 seconds. The six new #261 tests and #263 JSONB regression ran in this
  non-author gate against a disposable pgvector database; no database skips.

## Deployment and live validation

The user authorized redeployment on this Docker host. The prior healthy image
was preserved under `obsidian-mcp:rollback-before-261-263`. The canonical
`make deploy` pipeline reused the exact successful build and scan via
`make deploy -o build -o trivy`, after checking candidate image identity and
unchanged runtime source trees. Registry push, database backup, migration
check, recreation and the no-backups-mount check succeeded.

The running container is healthy on the candidate image. Effective configuration
is `mode=shadow`, `wait=0.0`; enforcement was not enabled. `make db-check`
reported **No new upgrade operations detected**.

Live MCP exercise on September 6, 2026, 17:07 UTC: **29 calls**, no rate retries,
**67 assertions** and four cleanup assertions passed. Tools actually called:
`create_note`, `read_note`, `edit_note`, `set_frontmatter`, `read_file`,
`write_file`, `move_note`, `delete_note`, and `delete_file`.

The exercise covered existing-note refusal, missing reads, malformed/stale
preconditions, hashes and byte preservation, forged sentinel text in a successful
note read, a no-op frontmatter update, empty/raw files, five concurrent small
reads, a move, and guarded permanent deletion of owned fixtures. All four files
and the exact empty UUID directory were removed. Existing credentials were used
in memory; no credential was minted and no vault content or secret was reported.

A read-only query scoped to those exact fixture paths and the smoke window
verified **29 usage rows**: one `already_exists`, one `malformed_precondition`,
one `stale_precondition`, and six `not_found` outcomes, each marked `refused`.
Three successful rows carried valid shadow observations. This small live sample
did not produce shadow/body-refusal overlap; that coexistence, cancellation,
fairness, enforced no-quota refusal and pool occupancy are established by the
isolated tests, not claimed from this smoke sample.

## Earlier issues reconciled

The following issues were already implemented on main. Each received an
issue-specific evidence comment and was closed: #183, #190, #191, #192, #193,
#197, #198, #200, #201, #202 and #206. The comments distinguish the historical
release validation in [the September 6 sweep](issue-sweep-2026-09-06.md) from
this read-only closure audit and retain documented residual limitations.

#188 remains open while production concurrency enforcement is disabled. #194 remains open
because expiry of unused dynamic OAuth client registrations is explicitly
outstanding; the existing rate controls do not implement it. Both received
comments explaining the remaining work. #261 and #263 received coordination
comments identifying this branch and the approved scope.

## Pull request

[PR #271](https://github.com/maxkuminov/obsidian-mcp/pull/271) targets main and
closes #263 and #261 on merge. The branch and completed specification records
are published; merging remains a separate action.
