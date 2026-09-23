## MODIFIED Requirements

### Requirement: Actual slot refusals and shadow observations remain distinct
Usage statistics SHALL classify enforced slot_timeout as pre-body, and SHALL keep shadow observations and queue-mode overruns separate from real outcomes and executed-work statistics. Neither `concurrency_shadow` nor `concurrency_queue` SHALL change the classification that the existing executed and pre-body predicates assign to a row.

#### Scenario: Enforced refusal is coalesced
- **WHEN** slot_timeout refusals are coalesced
- **THEN** existing weighted refusal counts SHALL read their full represented count
- **AND** their rows SHALL not enter body latency percentiles

#### Scenario: Shadow pressure accompanies a body error
- **WHEN** a call executes under shadow pressure and returns a typed #263 body refusal
- **THEN** the row SHALL preserve that post-body error and remain one executed request
- **AND** the shadow object SHALL not be interpreted as an actual slot refusal

#### Scenario: A queue-mode overrun whose body ran is executed work
- **WHEN** a row carries `concurrency_queue` with `overrun: true` and `code: slot_timeout`, and no pre-body refusal marker
- **THEN** it SHALL be counted as an executed request inside the latency aggregates, and SHALL NOT be counted as a pre-body refusal

#### Scenario: An overrun later refused by quota stays a pre-body refusal
- **WHEN** a row carries `concurrency_queue.overrun: true` and `over_quota: true`
- **THEN** it SHALL be counted as a pre-body refusal and excluded from latency aggregates, exactly as any other `over_quota` row

## ADDED Requirements

### Requirement: The performance view shows concurrency pressure and readiness
The performance view SHALL include a concurrency section computed from `usage_logs` rows carrying `params.concurrency.v = 2`, over the page's selected window and with the page's existing user scoping. For each tool and class it SHALL show:
- executed calls;
- tool-pressured calls;
- overruns;
- weighted `slot_timeout` refusals;
- `queue_ms` p50, p95, p99 and max.

For administrators only, the section SHALL also show:
- the current mode, the effective limits, the epoch and the computed pool demand;
- live active and waiting counts;
- the window's durable counters: requests, transport outcomes, writer outcomes, pool checkout timeouts and high-water;
- coverage gaps;
- the readiness evaluator's verdict for the next mode.

The section SHALL add no inline script or handler and SHALL render under the existing nonce CSP.

#### Scenario: A regular user sees only their own pressure
- **WHEN** a non-admin user opens the performance view
- **THEN** the concurrency aggregates SHALL count only that user's rows
- **AND** no live occupancy, durable counters, pool gauge or readiness verdict SHALL be rendered

#### Scenario: Tool pressure is counted from observations, not from the code
- **WHEN** a window holds schema-2 shadow rows whose tool-stage observation is not the first observation
- **THEN** each such row SHALL still be counted as tool-pressured

#### Scenario: Coverage gaps are visible
- **WHEN** the selected window contains an uncovered interval, such as the gap after a run that ended without a clean-shutdown flush
- **THEN** the administrator view SHALL list each uncovered interval's start and length, and the durable watermark, and the readiness verdict SHALL read INSUFFICIENT_DATA for every criterion

#### Scenario: Empty window
- **WHEN** the window holds no rows carrying concurrency data
- **THEN** the section SHALL render an explicit empty state rather than a table of zeroes

#### Scenario: No CSP violation
- **WHEN** an administrator loads the performance view with the concurrency section populated
- **THEN** the browser SHALL report zero Content-Security-Policy violations
