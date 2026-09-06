## Why

Issue #261: arrival-rate buckets do not bound concurrent authentication, tool
work or refusal logging. A retrying client can occupy the shared 15-connection
pool. Earlier concurrency proposals failed at their interactions; this change
specifies the complete admission and observation contract before enabling it.

## What Changes

- Add a process-local controller with off, shadow and enforce modes. Default to
  shadow, with no automatic promotion. Production testing deploys shadow only.
- Bound full MCP request occupancy globally and per transient credential
  fingerprint, and bound auth sessions separately before checking out a DB
  connection. Enforcement uses immediate transport refusals at these gates.
- Give all 25 tools exactly one of embedding/vector/write/other classes. Acquire
  class, tenant, principal and global dimensions atomically before quota, with
  one bounded wait and a bounded waiting registry in enforcement mode.
- Bound all tool-usage writers, including refusal/coalescer writes and FK retries,
  so refusal traffic cannot bypass the pool budget through its own logging.
- Validate explicit conservative pool arithmetic at boot. State its limited
  guarantee: MCP contributions are bounded in enforce mode, not all consumers
  of the shared pool; headroom is not reserved capacity.
- Observe the same zero-wait admission predicate in shadow, attach namespaced
  observations to real usage rows, and preserve actual outcomes from #263.
  Zero-wait observations are not a replay of an enforced traffic history.

## Capabilities

### Modified Capabilities
- `mcp-request-routing`: request/auth/tool/writer ceilings, complete class map,
  atomic admission, cancellation-safe lifetimes and shadow-first rollout.
- `usage-quotas`: slot refusals precede the durable daily counter.
- `panel-performance-views`: distinguish actual slot refusals from shadow
  observations without corrupting actual error markers or request counts.

## Impact

New concurrency controller and shared pool constants; config, database engine,
auth middleware, `_tracked` and usage writer, existing coalescer integration,
main lifecycle, tests, `.env.example`, README, CLAUDE.md and rate-limit/tenancy/
usage architecture notes. No migration or new dependency. Existing issue #261
coordinates the work; #263 supplies terminal body outcomes independently.
