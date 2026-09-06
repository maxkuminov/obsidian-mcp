# Open-issue triage — 2026-09-06

Reviewed all 23 open GitHub issues against main at `668abea`, the previous
[rollout report](issue-sweep-2026-09-06.md), current implementation and issue
bodies. No open pull requests existed at triage time. Priorities weigh agent
answer correctness, data integrity, availability, reach and implementation
readiness; severity alone is not a work order.

## Already implemented

| Issues | Current disposition |
| --- | --- |
| #183, #197, #198 | Consent disclosure, password change and session registry shipped in the prior rollout. |
| #188, #194 | Rate controls and default quota shipped; concurrency remains #261. |
| #190–#193 | Structured security events and exception/refusal attribution shipped within their recorded scope; body-result classification remains #263. |
| #200–#202, #206 | Stale result disclosure, typed embed failures, fairness and settings fingerprints shipped. |

These 13 issues need reconciliation against their individual outstanding gates
before closure, not duplicate implementations. The prior rollout report records
real-database and live-server evidence as well as remaining owner confirmations.
No issue is closed by this triage.

## Remaining work in priority order

| Priority | Issues | Decision and reason |
| --- | --- | --- |
| First implementation | #218 | Confirmed false graph edges: code masking turns an authored target into another note name. Fix extraction, preserve valid links, and re-derive existing rows with a version bump. Bounded application change, no migration. |
| Next application change | #263 | A common typed outcome contract makes body refusals visible to agents and operators. Requires a complete inventory across 25 tools and tests distinguishing refusal from successful empty/no-op results; not safe as prose matching or a partial relabeling. |
| Availability project | #261 | Tool concurrency remains unbounded. Design authentication admission, pool arithmetic and tenant/principal/class slots together; ship shadow mode first as already required. |
| Owner decision | #262 | Nested mount grafts can breach tenant isolation. Decide supported mount topologies and fund detection, or explicitly accept the deployment restriction. Existing root identity/containment checks do not solve this. |
| Coordinated infrastructure | #189, #184, #185, #196 | Restrict proxy trust and secure database/internal HTTP hops; reject plaintext API requests at the proxy. Needs stable proxy identity, certificates and shared-service configuration. Application-only enforcement risks outages without completing the control. |
| Lower priority | #195 | CSP remains an explicit architecture decision; no new injection primitive was established by the issue. |
| Documentation follow-up | #178 | Update external registry positioning when those listings are next edited. |

## Scope check for #263

A read-only follow-up found 197 return sites across the 25 tracked
implementations, before shared move, mint and publication helpers. A terminal typed refusal
helper can record a closed marker through the existing request timing context
and render the existing sentinel without changing string return types. The
migration must inventory each terminal outcome: successful empty results and
`check_upload` status reads remain successes; committed partial moves/imports
must never claim `nothing_written`. Parsing response prose or sentinels is
unsafe because ordinary note content can contain those strings. This is a
separate reviewed implementation, not a small follow-up to the extractor fix.

## Selected change: #218

Tracked in `openspec/changes/reject-mask-decided-links/`. Rejected candidates
must not consume the extraction cap, and code only in an alias, anchor or label
must not remove a valid target. Version 3 must repair existing link rows while
preserving embedding certification when the cleaned content is unchanged.
Independent proposal and defensive implementation/spec reviews both returned
PASS with no blocking findings. Strict OpenSpec validation passed all 33 items;
`make audit` found no known vulnerabilities. The complete offline suite passed
with **4,685 passed, 557 skipped**, and two existing dependency warnings. The
sandboxed offline run stalled in an existing async filesystem test; its isolated
unsandboxed run passed, followed by the complete unsandboxed suite. The stalled
runs were stopped and are not counted as validation.

`make test-integration` passed **554 tests**, including the new version-2 graph
repair regression, with one existing warning. It used a dedicated disposable
container; production was untouched. The feature branch is
`codex-fix-mask-decided-links`; publication is the final implementation step. No production
deployment or live MCP exercise has run for this change; the OpenSpec change
remains active for release checks.
