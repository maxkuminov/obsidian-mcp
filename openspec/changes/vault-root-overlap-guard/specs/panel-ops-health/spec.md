## ADDED Requirements

### Requirement: A detected vault-root overlap SHALL be surfaced to administrators
The dashboard's health strip and the health page SHALL show, to administrators, that a vault-root overlap has been detected, naming every user in the overlap set and the root each of them is assigned. The surface SHALL be visible while the condition stands and SHALL disappear once a pass publishes an empty overlap set; it SHALL NOT be a transient flash message.

An overlap is a misconfiguration that persists until an operator acts, and every other record of it decays. The container log rotates with the container; the in-process error ring buffer holds a hundred entries for the life of the process, so the line naming the overlap is gone after a restart while the two roots are still overlapping. The panel is the surface an operator opens when something is wrong, and the strip is the newest thing on it — a condition that has silently disabled two tenants' tools has to be legible there, not reconstructed from a run row.

Naming both users and both roots is correct here and only here: this surface is admin-only, the operator has to know which two assignments to look at, and the same detail is deliberately withheld from the tool-facing refusal, whose reader is a tenant's agent.

The condition SHALL be read from the same published overlap set the pass computes, not recomputed by the request handler — the panel must not open directories on a page render, and two independent computations of "do these roots overlap" is how the panel and the enforcement come to disagree.

#### Scenario: The strip names the affected users

- **WHEN** an administrator opens the dashboard while two users' roots overlap
- **THEN** the health strip SHALL state that a vault-root overlap has been detected
- **AND** SHALL name both users and both assigned roots

#### Scenario: The health page carries the same condition

- **WHEN** an administrator opens the health page while the overlap stands
- **THEN** the page SHALL show the same condition alongside the run history, the error buffer and the backup age

#### Scenario: A non-administrator does not see the operator detail

- **WHEN** a regular panel user opens the dashboard while an overlap stands
- **THEN** the page SHALL NOT name another user or another user's vault path, consistent with the existing operator-only split on the strip

#### Scenario: The surface clears when the overlap is corrected

- **WHEN** an administrator corrects one of the two assignments and the next pass publishes an empty overlap set
- **THEN** the strip and the page SHALL stop showing the condition, with no operator dismissal required

#### Scenario: No overlap renders nothing

- **WHEN** no overlap has been detected — including on a freshly started process that has not yet completed a pass
- **THEN** neither surface SHALL show the condition, and neither SHALL treat its absence as an error state

#### Scenario: The panel opens no directories

- **WHEN** the dashboard or the health page renders the condition
- **THEN** the handler SHALL read the set the indexer published and SHALL NOT itself open, stat or resolve any vault root
