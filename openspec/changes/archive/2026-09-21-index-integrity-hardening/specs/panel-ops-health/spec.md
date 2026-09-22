## ADDED Requirements

### Requirement: The dashboard reports embedding currency beside coverage
The dashboard SHALL show, next to the embedding coverage bar, the number of notes whose vectors are **not current** — never embedded, or embedded before the note's indexed content — and the number of notes whose embedding was truncated at the per-note chunk cap. Both counts SHALL be scoped to the viewer exactly as the coverage numbers beside them are, so that a non-admin never reads another tenant's backlog as their own.

The existing coverage bar SHALL keep its present meaning — the proportion of notes holding at least one vector row — and SHALL NOT be redefined. Coverage answers "is this note represented at all" and the pending count answers "is that representation current"; they are different questions, they disagree during every embed backlog, and collapsing them would silently change what every previously recorded coverage figure meant.

The predicate behind the pending count SHALL be the same one the re-embedding progress endpoint uses, expressed once and called from both, so the page and the poller cannot come to disagree about what "pending" means. The progress endpoint SHALL keep its existing admin-only, whole-database behaviour.

The pending count is the operator-visible consequence of two things nothing else surfaces on this page: a provider outage, which now marks the pass record but leaves coverage reading whatever it read yesterday; and a tenant whose embedding is repeatedly stopped at its per-pass budget, which is deliberately not recorded as a pass error. A backlog that does not shrink across passes is the signal, and it is a property of the index rather than of any one pass.

#### Scenario: A stale vault reads as fully covered and not current

- **WHEN** every note has vectors and every note has been edited since it was last embedded
- **THEN** the coverage bar SHALL read 100%
- **AND** the pending count beside it SHALL equal the number of notes

#### Scenario: The counts are scoped like their neighbours

- **WHEN** a non-admin user with an assigned vault opens the dashboard while another tenant has a large embedding backlog
- **THEN** the pending count SHALL count only that user's notes

#### Scenario: A truncated note is counted

- **WHEN** a note's embedding was capped at the per-note chunk cap
- **THEN** the dashboard SHALL report at least one note with a truncated embedding

#### Scenario: A healthy vault reports zero pending

- **WHEN** every note's stored vectors match its indexed content
- **THEN** the pending count SHALL be zero and SHALL still be rendered

#### Scenario: The progress endpoint is unchanged

- **WHEN** an administrator polls the re-embedding progress endpoint
- **THEN** it SHALL return the same whole-database counts under the same keys as before this change
