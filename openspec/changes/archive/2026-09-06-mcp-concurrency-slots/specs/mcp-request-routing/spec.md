## ADDED Requirements

### Requirement: Concurrency ships as zero-wait shadow observation
The server SHALL default concurrency control to shadow mode, evaluate the same
zero-wait capacity predicate as enforcement against observed occupancy, and leave
actual admission, results and outcomes unchanged by these concurrency ceilings.

#### Scenario: An overloaded call still runs in shadow
- **WHEN** observed concurrent work exceeds a configured ceiling in shadow mode
- **THEN** the call SHALL execute subject to existing non-concurrency gates without a concurrency wait or refusal
- **AND** an actual tool usage row SHALL carry bounded namespaced shadow metadata without changing its actual error, disposition or request count

#### Scenario: A positive wait cannot be misrepresented in shadow
- **WHEN** shadow mode is configured with a positive tool wait
- **THEN** startup SHALL reject that configuration with guidance to use zero wait
- **AND** observation docs SHALL distinguish observed-occupancy decisions from a counterfactual replay

### Requirement: Request and authentication occupancy have separate lifetimes
The middleware SHALL control global and fingerprint request occupancy before DB
lookup and control auth-session occupancy only while its own session is open.

#### Scenario: Authentication capacity is full in enforce mode
- **WHEN** a new request cannot acquire request or auth capacity
- **THEN** it SHALL receive a transport refusal without a new credential query or per-request usage INSERT

#### Scenario: A stream outlives authentication
- **WHEN** an authenticated SSE or other request remains open after auth completes
- **THEN** its request/fingerprint lease SHALL remain held and its auth permit SHALL be released
- **AND** completion, exceptions and cancellation SHALL release each lease exactly once

#### Scenario: Authentication sends a refusal slowly
- **WHEN** the transport stalls while sending an invalid-credential response
- **THEN** the middleware SHALL already have exited its auth DB session and released that auth permit

### Requirement: Tool admission acquires the entire class lattice atomically
Every registered tool SHALL have an explicit embedding, vector, write or other
class and SHALL acquire class, tenant, principal and global dimensions together.

#### Scenario: A busy class cannot occupy another class's capacity while waiting
- **WHEN** a class is full and another eligible class has capacity
- **THEN** the waiting call SHALL hold no partial permits and the eligible call SHALL be able to proceed

#### Scenario: Rotating credentials does not reset tenant capacity
- **WHEN** calls share a user across API keys or OAuth grants, or an OAuth grant refreshes its token
- **THEN** tenant limits SHALL remain shared and grant-level principal limits SHALL remain stable across refresh

#### Scenario: Positive waiting has one deadline
- **WHEN** an enforced call waits on several saturated dimensions
- **THEN** it SHALL have one monotonic bounded deadline and bounded waiter registration
- **AND** timeout, zero-wait miss or waiter overflow SHALL return the typed slot_timeout result

#### Scenario: Cancellation races with a grant
- **WHEN** a queued call is cancelled before or immediately after a grant
- **THEN** no active permit or queued entry SHALL be leaked or released twice

### Requirement: Configured MCP pool demand includes refusal logging
The server SHALL validate auth, conservative tool and usage-writer demand against
its single defined 15-connection pool capacity with explicit headroom.

#### Scenario: Configuration exceeds the pool budget
- **WHEN** auth + twice global tools + writers + headroom exceeds pool capacity
- **THEN** startup SHALL refuse and name the conflicting configuration

#### Scenario: Refusal logging is flooded
- **WHEN** enforced usage writers or their bounded waiting registry are full
- **THEN** no more than the writer ceiling SHALL check out logging connections
- **AND** coalesced refused-row counts SHALL survive unsuccessful writes or cancellation
- **AND** a completed tool result SHALL not fail because its usage row could not be written

#### Scenario: Shared consumers use the headroom
- **WHEN** non-MCP components use the shared pool
- **THEN** operational documentation SHALL state that arithmetic headroom is not reserved capacity or a universal availability guarantee


### Requirement: Overflow identities cannot gain a fresh allowance
Bounded keyed registries SHALL keep overflow ownership stable while any active
lease or pending waiter still belongs to the overflow epoch.

#### Scenario: Dedicated capacity opens while an overflow identity is active
- **WHEN** an identity is active in overflow and another dedicated entry drains
- **THEN** a new request from that overflow identity SHALL remain subject to the same shared allowance
- **AND** identities without a dedicated entry SHALL remain in overflow until the epoch drains
