## ADDED Requirements

### Requirement: Migration 016 owns the indexed-root column as a marked unit
Migration 016 SHALL add `users.indexed_vault_path` as a nullable `character varying(1024)` with no server default, stamped with 016's ownership marker as its column comment; SHALL backfill it from `users.vault_path` for every row whose `vault_path` is not null; SHALL leave it null for every row whose `vault_path` is null; and SHALL guard the backfill so that a re-run cannot overwrite a value the indexer has since written. `downgrade()` SHALL drop the column only if it carries the marker. After migrating to head, `alembic check` SHALL report no pending operations.

The marker is load-bearing for a stronger reason here than on a display column. This value is the sole input to a decision that **deletes a user's entire index** — `notes_metadata` and, by cascade, their embeddings and link rows. A same-named `varchar(1024)` of unknown provenance adopted as "the root those rows came from" is a mass delete on the strength of a string nobody in this scheme wrote. So 016 SHALL refuse a pre-existing `indexed_vault_path` that is not exactly its own column — wrong type, wrong width, `NOT NULL`, carrying a server default, or unmarked — naming what it found, rather than adopting it. The same marker string SHALL be declared on the ORM column so `alembic check` compares it and the two cannot silently drift.

The backfill asserts a fact the indexer's own scoping rule guarantees: only a user with a non-null `vault_path` is ever indexed, so an assigned user's rows were built from the root currently assigned. It asserts nothing about an unassigned user, whose previous root was never recorded anywhere; such an account is left null and therefore gets exactly one reassignment without reconciliation. That is a one-time consequence of introducing the column, not a rule, and it MUST NOT be closed by guessing a root.

#### Scenario: Fresh database

- **WHEN** an empty database is migrated to head
- **THEN** `users.indexed_vault_path` SHALL exist as nullable `character varying(1024)` with no server default and SHALL carry 016's marker as its column comment
- **AND** `alembic check` SHALL report no new upgrade operations

#### Scenario: Backfill on a populated database

- **WHEN** 016 runs on a database holding both assigned and unassigned users
- **THEN** every assigned user's `indexed_vault_path` SHALL equal that user's own `vault_path`
- **AND** every unassigned user's `indexed_vault_path` SHALL be null
- **AND** no user SHALL be stamped with another user's root

#### Scenario: Re-running the migration does not overwrite a stamp

- **WHEN** the database is stamped back to 015 and upgraded again, after the indexer has recorded a root that differs from the current `vault_path`
- **THEN** the recorded root SHALL be left unchanged

#### Scenario: A foreign column of the same name is refused

- **WHEN** `users` already carries a column named `indexed_vault_path` that is not exactly 016's column — a different type or width, `NOT NULL`, carrying a server default, or lacking the marker — and 016 runs
- **THEN** the migration SHALL fail naming what it found
- **AND** SHALL NOT adopt the column, SHALL NOT backfill it, and SHALL leave the schema unchanged

#### Scenario: A complete pre-existing column is accepted

- **WHEN** `users.indexed_vault_path` is already present, nullable, exactly typed, default-free and marked, and 016 runs
- **THEN** the migration SHALL accept it as its own and complete without error

#### Scenario: The model and the migration agree on the marker

- **WHEN** `alembic check` runs at head
- **THEN** it SHALL report no operation for `users.indexed_vault_path`, which is only true while the model's declared column comment is byte-identical to the migration's marker

#### Scenario: Downgrade

- **WHEN** a database at 016 is downgraded to 015
- **THEN** the marked column SHALL be dropped
- **AND** no other column SHALL be altered

#### Scenario: Downgrade leaves an unmarked column alone

- **WHEN** a database at 016 whose `indexed_vault_path` does not carry the marker is downgraded to 015
- **THEN** the column SHALL be left in place rather than dropped

### Requirement: Migration 017 owns the transfer-token actor columns as one marked unit
Migration 017 SHALL add `transfer_tokens.actor_kind`, `actor_label` and `actor_ref` as nullable `character varying(20)`, `character varying(255)` and `character varying(64)` with no server defaults, each stamped with 017's ownership marker as its column comment. It SHALL backfill them from the row's own surviving credential foreign key — through `api_keys` for a key-minted row and through `oauth_tokens` → `oauth_clients` for an OAuth-minted one — guarded on `actor_kind IS NULL` so a re-run cannot rewrite a value minting has since recorded. `downgrade()` SHALL drop only marked columns, all-or-nothing. After migrating to head, `alembic check` SHALL report no pending operations, and the marker SHALL be declared on the ORM columns so the check compares it.

The three columns are one owned unit: 017 SHALL complete only a set that is all present, exactly typed, nullable, default-free **and** marked, and SHALL refuse anything else — a partial set, a `NOT NULL` column, one carrying a server default, or a foreign column of the same name — naming what it found. Type and width are a coincidence anyone could reproduce; the marker is the only evidence that this scheme wrote the values, which is the whole basis for rendering them to an operator as an audit trail.

Nothing is invented. 017 labels a row from the credential its own foreign key points at, or leaves it null. Because `transfer_tokens.key_id` and `transfer_tokens.oauth_token_id` are both `ON DELETE CASCADE`, a row whose minting credential has been deleted does not survive to be labelled at all; the rows the backfill leaves null are therefore the ones that carry no credential foreign key. 017 SHALL NOT write to `usage_logs`: a transfer usage row written before 017 carries no reference to the token that produced it, and the only alternative — re-running migration 015's own credential join — would put a second writer on columns 015 owns and guards.

#### Scenario: Fresh database

- **WHEN** an empty database is migrated to head
- **THEN** the three columns SHALL exist on `transfer_tokens` as nullable, exactly typed, default-free, and each carrying 017's marker
- **AND** `alembic check` SHALL report no new upgrade operations

#### Scenario: Backfill labels a row from its own credential

- **WHEN** 017 runs on a database holding transfer tokens minted by an API key and by an OAuth token whose client still exists
- **THEN** the key-minted rows SHALL carry `actor_kind = 'api_key'` with that key's name and `omcp_` prefix
- **AND** the OAuth-minted rows SHALL carry `actor_kind = 'oauth'` with that client's name and `client_id`
- **AND** no row SHALL be labelled from a credential other than the one its own foreign key names

#### Scenario: A row with no credential foreign key stays unattributed

- **WHEN** 017 runs on a database holding a transfer token with both credential foreign keys null
- **THEN** all three columns SHALL remain null for that row
- **AND** the migration SHALL NOT infer an actor from `user_id` or from any other row

#### Scenario: The migration writes nothing to the usage log

- **WHEN** 017 runs on a database holding transfer-route `usage_logs` rows with null actor columns
- **THEN** those rows SHALL be left exactly as they were

#### Scenario: Re-running the migration does not rewrite a recorded actor

- **WHEN** the database is stamped back to 016 and upgraded again, after a mint has recorded an actor whose label differs from the credential's current name
- **THEN** the recorded actor SHALL be left unchanged

#### Scenario: A partial or foreign set is refused

- **WHEN** `transfer_tokens` already carries some but not all three columns, or carries all three with one of them `NOT NULL`, wrongly typed, carrying a server default, or unmarked, and 017 runs
- **THEN** the migration SHALL fail naming what it found and SHALL leave the schema unchanged

#### Scenario: A complete pre-existing set is accepted

- **WHEN** all three columns are already present, nullable, exactly typed, default-free and marked, and 017 runs
- **THEN** the migration SHALL accept them and complete without error

#### Scenario: Downgrade drops the marked set, all or nothing

- **WHEN** a database at 017 is downgraded to 016
- **THEN** all three marked columns SHALL be dropped
- **AND** a set in which any column lacks the marker SHALL be left in place instead

### Requirement: The schema gate covers both migrations of this wave before deploy
The schema gate SHALL exercise 016 and 017 on a throwaway database, in the same run, and SHALL assert `alembic check` clean at the resulting head. The head revision the gate asserts SHALL be updated to `017`, so a later migration added without updating the gate fails loudly rather than silently widening what "head" means.

Idempotence SHALL be exercised by stamping the revision back and upgrading again, not by a second `upgrade head` — the latter is a no-op at the alembic level and proves nothing about the migration body.

#### Scenario: Head at 017

- **WHEN** a throwaway database is migrated to head
- **THEN** `alembic_version` SHALL read `017`
- **AND** `alembic check` SHALL report no new upgrade operations

#### Scenario: Idempotence is exercised by stamping back

- **WHEN** the gate tests that 016 or 017 can re-run
- **THEN** it SHALL stamp the database to the preceding revision and upgrade again, so the migration body genuinely re-executes
