## ADDED Requirements

### Requirement: Migration 016 owns the indexed-root identity columns as one marked unit
Migration 016 SHALL add `users.indexed_vault_path` as a nullable `character varying(1024)` and `users.indexed_vault_handle` as a nullable `character varying(320)`, both with no server default and each stamped with 016's ownership marker as its column comment. It SHALL leave both columns null for **every** existing row and SHALL perform no backfill of any kind. `downgrade()` SHALL drop the columns only if they carry the marker, all-or-nothing. After migrating to head, `alembic check` SHALL report no pending operations, and the same marker string SHALL be declared on the ORM columns so the check compares it.

The width of `indexed_vault_handle` follows the kernel's own bound. A file handle is at most `MAX_HANDLE_SZ` (128) bytes of opaque payload, which is 256 hexadecimal characters, plus a handle type and a separator; 320 characters holds the largest handle any filesystem may return with room to spare, and the column stores text so that nothing is ever tempted to interpret it. On the ext4 and xfs filesystems this system declares support for, the payload is eight bytes.

Not backfilling is the load-bearing decision, not an omission. Deriving `indexed_vault_path` from `users.vault_path` would assert that an assigned user's index was built from the root assigned *now*, which is exactly the reassignment lag the record exists to detect: an administrator who reassigns and deploys before the next index pass would have rows built from one vault stamped as belonging to another, after which both identity signals agree, the pass takes its no-op branch, and the identical-path/identical-content link case that never heals becomes guaranteed rather than merely possible. A null record means "provenance unknown", which is the only true statement available at migration time, and the pass repairs such a user by re-deriving the index rather than by discarding it — so introducing the columns costs no vault-wide re-embed.

Not backfilling is also what makes the deploy order safe without any cross-container coordination. An index pass running under the previous code during or after the migration cannot write these columns, so it cannot produce a record for the new code to trust; every row is null when the new container starts, whatever that pass committed.

The marker is load-bearing for a stronger reason here than on a display column. This record is the sole input to a decision that can **delete a user's entire index** — `notes_metadata` and, by cascade, their embeddings and link rows. A same-named column of unknown provenance adopted as "the directory those rows came from" is a mass delete on the strength of a value nobody in this scheme wrote. So 016 SHALL refuse a pre-existing column of either name that is not exactly its own — wrong type, wrong width, `NOT NULL`, carrying a server default, or unmarked — and SHALL refuse a partial set in which one column is present and the other absent, naming what it found, rather than adopting it.

#### Scenario: Fresh database

- **WHEN** an empty database is migrated to head
- **THEN** `users.indexed_vault_path` SHALL exist as nullable `character varying(1024)` and `users.indexed_vault_handle` as nullable `character varying(320)`, both with no server default and both carrying 016's marker as their column comment
- **AND** `alembic check` SHALL report no new upgrade operations

#### Scenario: The migration backfills nothing

- **WHEN** 016 runs on a database holding both assigned and unassigned users, each with existing `notes_metadata` rows
- **THEN** both columns SHALL be null for every user, including every assigned one
- **AND** no `notes_metadata`, `note_embeddings` or `note_links` row SHALL be modified or deleted

#### Scenario: Re-running the migration does not overwrite a record

- **WHEN** the database is stamped back to 015 and upgraded again, after the indexer has recorded an identity that differs from the current `vault_path`
- **THEN** the recorded identity SHALL be left unchanged

#### Scenario: A foreign column of the same name is refused

- **WHEN** `users` already carries a column named `indexed_vault_path` or `indexed_vault_handle` that is not exactly 016's column — a different type or width, `NOT NULL`, carrying a server default, or lacking the marker — and 016 runs
- **THEN** the migration SHALL fail naming what it found
- **AND** SHALL NOT adopt the column and SHALL leave the schema unchanged

#### Scenario: A partial set is refused

- **WHEN** exactly one of the two columns is present, however well formed, and 016 runs
- **THEN** the migration SHALL fail naming what it found and SHALL leave the schema unchanged

#### Scenario: A complete pre-existing set is accepted

- **WHEN** both columns are already present, nullable, exactly typed, default-free and marked, and 016 runs
- **THEN** the migration SHALL accept them as its own and complete without error

#### Scenario: The model and the migration agree on the marker

- **WHEN** `alembic check` runs at head
- **THEN** it SHALL report no operation for either column, which is only true while the models' declared column comments are byte-identical to the migration's marker

#### Scenario: Downgrade drops the marked set, all or nothing

- **WHEN** a database at 016 is downgraded to 015
- **THEN** both marked columns SHALL be dropped
- **AND** a set in which either column lacks the marker SHALL be left in place instead
- **AND** no other column SHALL be altered

### Requirement: Migration 017 owns the transfer-token actor columns as one marked unit
Migration 017 SHALL add `transfer_tokens.actor_kind`, `actor_label` and `actor_ref` as nullable `character varying(20)`, `character varying(255)` and `character varying(64)` with no server defaults, each stamped with 017's ownership marker as its column comment. It SHALL backfill them from the row's own surviving credential foreign key — through `api_keys` for a key-minted row and through `oauth_tokens` → `oauth_clients` for an OAuth-minted one — guarded on `actor_kind IS NULL` so a re-run cannot rewrite a value minting has since recorded. `downgrade()` SHALL drop only marked columns, all-or-nothing. After migrating to head, `alembic check` SHALL report no pending operations, and the marker SHALL be declared on the ORM columns so the check compares it.

The three columns are one owned unit: 017 SHALL complete only a set that is all present, exactly typed, nullable, default-free **and** marked, and SHALL refuse anything else — a partial set, a `NOT NULL` column, one carrying a server default, or a foreign column of the same name — naming what it found. Type and width are a coincidence anyone could reproduce; the marker is the only evidence that this scheme wrote the values, which is the whole basis for rendering them to an operator as an audit trail.

Before it writes anything, 017 SHALL enforce migration 015's orphan-label invariant on `transfer_tokens`: no row may carry an `actor_label` or an `actor_ref` while its `actor_kind` is null. The backfill's only guard is `actor_kind IS NULL`, so such a row would be relabelled from whatever credential its foreign key points at now, overwriting a recorded attribution — the one thing these columns must never do. On finding any such row the migration SHALL fail naming the offending rows and SHALL change nothing. This state is reached by drift or a faulty writer rather than by any current application path, which is why the check is cheap insurance rather than a hot path; it is also the invariant that makes the marker pattern safe on a stamp-back re-run rather than merely well typed.

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

#### Scenario: A label beside a null kind aborts the migration

- **WHEN** the database is stamped back to 016 and upgraded again, and a `transfer_tokens` row carries an `actor_label` or an `actor_ref` while its `actor_kind` is null
- **THEN** the migration SHALL fail naming the offending rows
- **AND** SHALL leave every `transfer_tokens` row exactly as it was, rewriting no label from the credential the row points at now

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
