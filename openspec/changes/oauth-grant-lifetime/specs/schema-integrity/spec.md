## ADDED Requirements

### Requirement: Migration 029 SHALL own the code-lineage and grant-issuance columns as one marked unit
Migration 029 SHALL add `oauth_codes.grant_id` (`varchar(64)`, nullable, no server default) and `oauth_tokens.grant_issued_at` (`timestamptz`, NOT NULL, server default `now()`), each carrying the migration's ownership marker as its column comment, mirrored byte-identically in `src/models/db.py` so that `alembic check` compares it. Where a same-named column already exists with a different type, nullability or default, 029 SHALL refuse and name what disagreed rather than adopt it; a column of the exact shape carrying the marker SHALL be reconciled without change. `downgrade()` SHALL drop only columns carrying the marker and SHALL fail naming an unmarked one.

The issuance column carries a server default, unlike 025's marker, only so that a process still running the previous image during a rolling deploy can insert tokens without violating NOT NULL. Application code MUST NOT rely on it (see the token-construction requirement in `oauth-authorization-integrity`).

#### Scenario: Fresh upgrade
- **WHEN** a database at 028 is upgraded to 029
- **THEN** both columns SHALL exist with the stated type, nullability, default and marker
- **AND** every pre-existing `oauth_codes` row SHALL read NULL in `grant_id`

#### Scenario: Pre-existing tokens are backfilled with one timestamp
- **WHEN** the migration runs against a database holding token rows
- **THEN** every pre-existing `oauth_tokens` row SHALL carry the same `grant_issued_at`, equal to the migration transaction's timestamp

#### Scenario: An impostor column is refused
- **WHEN** `oauth_tokens.grant_issued_at` already exists as a nullable column, without a default, or with another type, or `oauth_codes.grant_id` already exists as NOT NULL or with another type
- **THEN** the migration SHALL fail, naming the column and the disagreement, and change nothing

#### Scenario: Stamp-back re-runs cleanly
- **WHEN** the schema gate stamps the database back to 028 and upgrades again
- **THEN** 029 SHALL reconcile its marked columns without error and SHALL NOT change any `grant_issued_at` or `grant_id` value

#### Scenario: Downgrade drops only what 029 created
- **WHEN** a downgrade runs against marked columns
- **THEN** both columns SHALL be dropped
- **AND** a downgrade against an unmarked same-named column SHALL fail naming it

#### Scenario: The schema agrees with the model afterwards
- **WHEN** `alembic check` runs after the migration has been applied
- **THEN** it SHALL report no new upgrade operations
- **AND** the schema gate SHALL verify the `grant_issued_at` server default through the catalogue, since autogenerate does not compare server defaults
