## ADDED Requirements

### Requirement: Quota admission SHALL commit asynchronously, and a crash SHALL only ever undercount
The quota admission statement and the prune that follows it SHALL each be committed with `SET LOCAL synchronous_commit = off` in their own transactions. Everything else about the quota stays as it is:
- the conditional increment's atomicity;
- its position as the last pre-body gate, after the per-principal token buckets;
- the fact that nothing durable is consumed by a call refused at an earlier gate;
- the fail-closed disposition on an admission error;
- the retry interval derived from the admission's own clock read.

An asynchronously committed increment is visible to every other session at commit, so the limit SHALL still admit at most `limit` calls per key per UTC day under any concurrency.

The accepted limitation SHALL be stated in the architecture note, as an owner decision taken by default. A crash of the PostgreSQL server or host may lose increments committed in the preceding ~600 ms. The consequence is that the key is **undercounted, in the caller's favour**. It SHALL NOT be possible for asynchronous commit to overcount a key or to refuse a call that the synchronous form would have admitted.

#### Scenario: The concurrency boundary is still exact
- **WHEN** a key with limit N receives more than N concurrent calls in one UTC day with asynchronous admission commits
- **THEN** exactly N tool bodies SHALL execute and every excess call SHALL be refused

#### Scenario: Gate order is unchanged
- **WHEN** a limited key is refused by a per-principal token bucket
- **THEN** no quota statement SHALL be issued and its counter SHALL be unchanged

#### Scenario: The admission transaction sets asynchronous commit first
- **WHEN** a limited key's call reaches the admission
- **THEN** its transaction SHALL issue `SET LOCAL synchronous_commit = off` before the admission statement
