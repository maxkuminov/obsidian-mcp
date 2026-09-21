## ADDED Requirements

### Requirement: Unused dynamically registered OAuth clients expire
The maintenance job SHALL delete a dynamically registered OAuth client when it has never been used, holds no authorization code and no token of any state, and was registered longer ago than a configured age. `/register` is unauthenticated dynamic client registration, so nothing else bounds the table; today the cleanup job deletes codes and tokens and never touches a client row, and registrations accumulate without limit.

"Never been used" SHALL be read from a durable per-client marker stamped whenever an authorization code or a token is issued for that client, and MUST NOT be inferred from the absence of child rows alone: a used authorization code is deleted immediately and a token seven days after it expires, so a client that was genuinely used becomes indistinguishable from one that never was. It MUST NOT be inferred from the client having no owner either, because a client authorized in single-user mode is never bound to a user.

The age SHALL be configurable with a sane default, SHALL refuse a zero-day window, and SHALL be disable-able outright. Each pass SHALL log the number of clients it deleted, because a job that silently removes credentials cannot be audited.

#### Scenario: A never-used client older than the age is deleted
- **WHEN** the maintenance pass runs and finds a client with no recorded use, no code row, no token row, and a registration older than the configured age
- **THEN** that client SHALL be deleted

#### Scenario: A never-used client younger than the age is kept
- **WHEN** the same client is younger than the configured age
- **THEN** it SHALL NOT be deleted

#### Scenario: A client that has ever been used is kept
- **WHEN** a client carries a recorded use, however old, and holds no live token
- **THEN** it SHALL NOT be deleted, whatever its age

#### Scenario: A client holding a live token is kept
- **WHEN** a client has an unexpired, unrevoked access or refresh token
- **THEN** it SHALL NOT be deleted

#### Scenario: A client holding a revoked or expired token is kept while that row survives
- **WHEN** a client's only token rows are revoked or expired but have not yet been purged
- **THEN** the client SHALL NOT be deleted, so a deletion can never cascade away a token row the operator can still see

#### Scenario: A client with a pending authorization code is kept
- **WHEN** a client has an unused, unexpired authorization code
- **THEN** it SHALL NOT be deleted

#### Scenario: A client bound to a user is kept
- **WHEN** a client has been claimed by an authorizing user
- **THEN** it SHALL NOT be deleted, because a claimed client has by definition completed an authorization

#### Scenario: Expiry can be switched off
- **WHEN** the configured age is set to the disabled value
- **THEN** no client SHALL be deleted by this pass

#### Scenario: A zero-day window is refused
- **WHEN** the configured age is set to zero days
- **THEN** startup SHALL refuse the configuration rather than delete registrations as they are made

#### Scenario: The deletion is counted
- **WHEN** a pass deletes one or more clients
- **THEN** it SHALL log the number deleted

#### Scenario: Usage attribution survives the expiry
- **WHEN** a client is deleted by this pass and `usage_logs` rows reference tokens it owned
- **THEN** those rows SHALL keep their recorded actor kind, label and reference, exactly as they do when an operator deletes a client from the panel

### Requirement: The expiry sweep MUST NOT race an in-flight authorization
The sweep SHALL lock each candidate client row for update and re-evaluate every eligibility condition inside that lock before deleting, and an authorization that loses the race SHALL fail with an ordinary OAuth client error rather than a server error or a partially written grant. A single conditional delete is not sufficient: inserting an authorization code takes a foreign-key lock on the client row, but when the delete unblocks the database re-evaluates only the target row's own predicate and not the existence subquery, so the delete proceeds and cascades away the code that was just issued.

#### Scenario: An authorization commits first
- **WHEN** an authorization stamps the client's use marker and inserts its code while the sweep is evaluating that client
- **THEN** the sweep SHALL observe the stamped marker inside its lock and SHALL NOT delete the client
- **AND** the issued code SHALL survive

#### Scenario: The sweep commits first
- **WHEN** the sweep deletes a client while an authorization for it is in flight
- **THEN** the authorization SHALL fail with an OAuth client error, SHALL NOT raise an unhandled server error, and SHALL leave no code, token or grant behind

#### Scenario: A contended row is deferred, not forced
- **WHEN** a candidate row is already locked by another transaction
- **THEN** the sweep SHALL skip it and leave it for a later pass rather than waiting on it

#### Scenario: The sweep is bounded per pass
- **WHEN** a pass finds more candidates than its batch size
- **THEN** it SHALL delete at most one batch and leave the remainder for subsequent passes
