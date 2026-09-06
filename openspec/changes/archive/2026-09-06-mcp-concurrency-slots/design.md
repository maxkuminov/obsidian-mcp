## Modes and observation semantics

MCP_CONCURRENCY_MODE is off|shadow|enforce, default shadow. Off bypasses this
controller, not existing rate/auth/quota controls. Shadow never refuses, delays,
queues or changes a request/tool result because of a concurrency ceiling. It
counts observed live work and evaluates the exact same admission predicate as
enforcement. No automatic promotion or production enforce-mode testing.

MCP_CONCURRENCY_WAIT_SECONDS defaults to 0, a bounded zero wait (not disabled
control). It accepts finite values in 0..5 in enforce mode. Shadow requires 0 at
configuration validation: it reports which arrivals fail the zero-wait predicate
under observed occupancy, not whether a positive queue timeout would occur. No
observer task, hypothetical future deadline or background task per observation.
An enforced zero-wait miss says capacity is unavailable now, not that it waited.

Nested `concurrency_shadow` on the actual usage row has `shadow: true`,
`code: slot_timeout` for tool pressure (or the corresponding transport-stage
code), `basis: observed_occupancy_zero_wait`, and bounded stage/scope/limit
observations. No raw credential or fingerprint enters it. Real `error`,
`body_outcome`, quota flags, duration and response fields are untouched. A row is
still one actual request. This is a precise predicate on real observed occupancy,
not a full counterfactual replay: earlier calls that enforcement might reject
still ran and affected later observations. Document that when calibrating.

Transport observations before authentication are bounded security events. Once
identity is bound, request/auth observations can accompany tool usage rows.
Requests that never invoke a tool retain transport-only telemetry; do not invent
ownerless per-probe usage INSERTs. Existing failed-authentication rate refusals
remain outside the tool sentinel and usage-row contract.

## Fixed identities, classes and defaults

Every registered tool has exactly one explicit class in a closed mapping:
- embedding: semantic_search.
- vector: find_related.
- write: create_note, edit_note, move_note, delete_note, set_frontmatter,
  write_file, delete_file, import_from_url (the existing eight write-class tools).
- other: keyword_search, read_note, list_notes, get_tags, get_recent,
  get_vault_guide, get_backlinks, get_links, get_neighborhood, find_orphans,
  read_file, list_files, request_upload, check_upload, request_download.

Registration tests require this exact coverage and agreement with write_class.
No new registered tool silently falls into other. An explicit resource class may
be supplied for internal test tools without weakening production coverage.

Principal is the existing API-key ID or OAuth grant ID, never the rotating token
ID. Tenant is authenticated user_id, with one stable singleton for NULL-owner
single-user mode. No-principal direct internal calls and registry sandbox mode
retain their existing exemption, not a credential that a client can manufacture.

Provisional positive bounded defaults:
- full requests: global 32, per presented-bearer fingerprint 4;
- concurrent authentication sessions A=2;
- all tool slots G=4; per tenant 3; per principal 2;
- class ceilings embedding=1, vector=1, write=1, other=1;
- pending tool waiters: global 32, per tenant 4, per principal 2;
- active registry entries per keyed dimension 1024;
- usage writers W=1; writer waiters 64; writer wait at most 0.25 seconds
  in enforcement mode only.

SHA-256 of the presented bearer is an ephemeral in-memory request key, erased
when no active lease needs it, never persisted, logged, or treated as a tenant.
Global request/auth controls still bind when tokens rotate. Keyed registries are
bounded; active entries are never evicted/recreated with fresh allowances. Once
full, new identities share a stable overflow counter whose lease releases that
same resolved entry. Collision/overflow may be stricter, never an exemption.
Overflow assignment is sticky: while an overflow entry has any active lease or
pending waiter, every identity without an existing dedicated entry continues to
resolve to overflow even if a dedicated entry becomes free. Only a drained
overflow epoch permits new dedicated assignments. Apply this rule to request
fingerprints, tool tenant/principal dimensions and their waiter accounting. This
prevents one still-active identity splitting its allowance across overflow and a
new dedicated entry. A held lease always releases its captured entry; entries
with active or waiting owners never migrate. Test churn at registry capacity,
capacity freeing during overflow, cancelled waiters and the drained-epoch reset.
State is per worker; existing --workers 1 remains part of the contract.

## Request and auth lifetimes

Keep cheap failed-auth/bearer validation before credential lookup. Acquire the
full-request global+fingerprint lease together before opening any auth session;
release exactly once in outer finally after the downstream ASGI app finishes,
including auth refusal, disconnect, cancellation and exceptions. These are full
request lifetimes: GET/SSE, initialization, listing, notifications and POST all
count. Long-lived SSE intentionally consumes a request/fingerprint lease.

Acquire the separate auth permit immediately before the middleware's own session
opens. It encloses that session's reads, last-used update, cache warm and teardown;
release it before response send and before downstream app/tool execution. Move
invalid-auth response sending out of the DB session without changing bodies,
headers or authentication precedence. No permit wait while a connection is held.

Enforced request/auth pressure returns transport 429 with a bounded explanation
and Retry-After backoff hint; it executes no new credential statement or usage
INSERT. It does not pretend to be an MCP tool result. A per-request fingerprint
is not a fairness guarantee: an attacker rotating valid/invalid bearers may fill
the global envelope, but cannot exceed it or the auth-session ceiling. No claim
of tenant reservation on a path where tenant identity is not known yet.

## Atomic tool admission and one wait

Order: existing general/write buckets -> vault -> argument screens -> tool slots
-> daily quota -> body -> response-neutral telemetry. The quota remains the last
pre-body durable gate. Failed prior gates acquire no tool slots. A denied quota
releases its tool lease without running a body.

Admission checks every class/tenant/principal/global count and increments all
needed counters in one non-awaiting transition. Never hold global while waiting
for a narrower dimension, nor hold class permits while waiting for global. The
explicit other class and sum(class caps) <= G invariant preserve a declared
whole-process budget. There is no per-tenant embedding reservation: at one or two
class slots and N tenants such a promise is impossible. Tenant/principal limits
are ceilings; eligible FIFO waiting is work-conserving, not a starvation SLA.

A failed positive-wait admission uses one monotonic deadline across all dimensions,
holds no resource permits and enters a bounded global/tenant/principal waiting
registry. A full waiting registry refuses immediately. On release, admit the
oldest eligible waiters atomically; an ineligible busy-class waiter cannot block
other eligible classes. Transfer grant ownership before waking a waiter, and
return it if cancellation wins immediately after grant. Avoid polling sleeps.

Timeout/queue-full/zero-wait misses return the closed caller code slot_timeout
with scope, limit and concurrent-call units, a complete final sentinel (including
read_note's structured refusal), and no fabricated release-time guarantee. Record
actual queue_ms separately. Pre-body slot_timeout rows use the existing bounded
coalescer with marker/scope keys; they spend no daily quota. Shadow observations
are not coalesced duplicate request rows: they ride on the actual bounded-rate
call's row. Cancellation propagates and releases every lease/waiter.

Hold a tool lease through quota, body and its telemetry tail, releasing in finally.
No recursive tracked-tool admission is used by current production calls; tests
and docs must identify if one is introduced. Lease objects capture resolved keys
and mode, so completion never releases a new identity or a new controller.

## Usage writers and pool arithmetic

Export pool size/overflow/capacity from one dependency-free module, consumed by
both Settings validation and database engine construction. Keep size5/overflow10.
Use a conservative maximum M=2 simultaneous connections per admitted tool (current
inspected quota, publication, move and import sessions are sequential). Validate
A + M*G + W + R <= P, with P=15 and explicit MCP headroom R=4. Defaults:
2 auth + 2*4 tools + 1 logger + 4 headroom = 15. Also validate coherent positive
bounded hierarchy, class sum <= G, finite waits, registry/waiter limits and modes.

This bounds the configured MCP contribution in enforce mode. It does NOT reserve
connections or prevent other shared indexer/OAuth/panel/transfer consumers from
using the headroom or exhausting the pool. Shadow enforces none of these counts.
Do not call this a universal pool-availability guarantee. Future tool paths with
more than M overlapping sessions require budget/review updates; test actual
checkout peaks for auth, quota, named-user publication, move/import and logging.

One writer permit encloses the COMPLETE write_usage_row operation, including
initial insert, rollback and dangling-FK retry, for ordinary rows, exceptions,
immediate refusals and deferred coalescer flushes. Acquire it before any logging
session. Enforcement has a bounded pending registry and short deadline; no task
per refusal beyond the caller. Queue-full/timeout returns False and emits a
bounded existing usage_log_failed event with a closed reason. The coalescer must
retain/requeue its weight on False or cancellation. Ordinary rows are best-effort,
with bounded failure evidence, and never fail a completed tool response.

Writer controls are observational only in shadow (no new wait/drop); no deadlock
between slot refusal and a logging permit. A logging permit consumes one DB
connection at a time; initial session closes before retry. The tool lease can be
held while awaiting the bounded writer without holding a tool DB connection.

## Lifecycle and operational rollout

One controller instance per process/configuration, reset only at explicit test or
lifespan boundaries, never automatically on a live request. Shutdown wakes pending
waiters without granting new work, releases cancelled leases and flushes existing
refusal counts before engine disposal. No module-level asyncio primitive tied to
a stale test loop. Off/sandbox behavior is exercised explicitly.

Deploy in default shadow mode, verify settings and health, exercise affected live
MCP tools with owned temporary fixtures, inspect outcome/shadow usage rows, remove
fixtures, and report which tools ran. Positive enforcement and saturation tests
run only against isolated test app/database instances, not production tenants.
Activation requires a later deliberate configuration change informed by observed
traffic, with SSE occupancy, class limits and the no-reservation caveat reviewed.

## Verification

Independent specification/defensive reviews before and after implementation.
Deterministic concurrency tests for all ceilings, two tenants, OAuth refresh,
busy-class bypass, a single deadline, queue overflow, registry churn, shadow
identity/result neutrality, cancellation before/after grant, quota denial,
exceptions and writer retry/requeue. Test every registered class and every auth
branch, including slow response send outside auth sessions and long-lived SSE.

Real PostgreSQL tests count actual checked-out connections under concurrent auth,
tools, quota and logging, and verify usage predicates distinguish actual pre-body
slot_timeout from shadow plus real #263 body failures. Run full combined offline,
integration, strict OpenSpec, dependency audit and image build/scan before deploy.
No migration is planned; run db-check after deployment to confirm the live schema.
