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
- Dependency audit: `make audit` passed with no known vulnerabilities.

Combined integration review, full test matrix, deployment and live MCP results
will be recorded below when completed.

## Earlier issues reconciled

The following issues were already implemented on main. Each received an
issue-specific evidence comment and was closed: #183, #190, #191, #192, #193,
#197, #198, #200, #201, #202 and #206. The comments distinguish the historical
release validation in [the September 6 sweep](issue-sweep-2026-09-06.md) from
this read-only closure audit and retain documented residual limitations.

#188 remains open while the concurrency work is completed. #194 remains open
because expiry of unused dynamic OAuth client registrations is explicitly
outstanding; the existing rate controls do not implement it. Both received
comments explaining the remaining work. #261 and #263 received coordination
comments identifying this branch and the approved scope.
