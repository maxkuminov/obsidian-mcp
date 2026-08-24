## ADDED Requirements

### Requirement: Migration 016 owns the index-provenance columns as one marked unit
Migration 016 SHALL add `users.indexed_vault_assignment` and `users.indexed_vault_realpath` as nullable `text`, and `users.indexed_vault_handle` as nullable `character varying(320)`, all three with no server default and each stamped with 016's ownership marker as its column comment. It SHALL leave all three columns null for **every** existing row and SHALL perform no backfill of any kind. `downgrade()` SHALL drop the columns only if they carry the marker, all-or-nothing. After migrating to head, `alembic check` SHALL report no pending operations, and the same marker string SHALL be declared on the ORM columns so the check compares it.

The three columns are one unit because they are written as one: every stamp writes all three, with null for a fact the pass could not observe. `indexed_vault_assignment` holds the canonical assignment string the pass ran under and is the fact the keep/discard decision turns on; `indexed_vault_realpath` holds the real path that assignment named at that moment and exists so that a cosmetic rename does not cost a full re-embed; `indexed_vault_handle` holds an opaque kernel file handle where the filesystem produced one, and is best-effort hardening that can only refuse a keep.

**The provenance columns SHALL be able to record any value the facts they mirror can take.** That rule, not a width, is why the two pathname columns are `text` — and it is not satisfied by a type alone. A column is total over a fact only when neither the *length* nor the *byte content* of an observable value can be rejected, so the rule governs the column's representation as well as its DDL. A value the pass observed and cannot store is a bug, not a null: such a value SHALL NOT be truncated, SHALL NOT be recorded as null, and SHALL NOT be allowed to fail the write.

The realpath is what makes the width question real. An assignment of any accepted length may resolve through a symbolic link to a canonical path far longer than itself, and this system owns no bound on that. The consequence is not a lost display string: the discard branch writes the record and the delete in **one** transaction, so a value that will not fit raises `string_data_right_truncation`, that transaction rolls back its delete, every later pass repeats the failure, and the database-backed tools keep serving the former vault indefinitely — the precise state this record exists to end, produced by a column width.

**`indexed_vault_realpath` SHALL store the observed pathname's bytes, hexadecimal-encoded — not the pathname as text.** The value written SHALL be the lowercase hexadecimal encoding of `os.fsencode(realpath)`: the exact byte sequence the kernel returned for the directory the pass scanned. Comparison SHALL be encode-then-compare — the real path observed now is reduced to that same representation and compared with the stored one as text. The stored value SHALL NOT be decoded back to a pathname in order to compare it, and SHALL NOT be shown to an operator as a pathname without being decoded through `os.fsdecode` first: a log line may render the decoded form, but the value that is recorded and compared is the hexadecimal one.

That encoding is what makes the totality rule true rather than merely stated, and `text` alone did not make it true. A POSIX pathname is an arbitrary sequence of non-null bytes under no obligation to be valid UTF-8; Python decodes such a component with `surrogateescape`, so `os.path.realpath` can return a string carrying a lone surrogate such as `'\udcff'`, which a UTF-8 database cannot encode and will not accept. Widening the column to `text` removed the *width* bound and left the *encoding* bound exactly where it was, so the identical failure survived the widening through the other channel: the discard branch writes the record and the delete in **one** transaction, the parameter fails to encode, that transaction rolls back its delete, every later pass repeats the failure, and the database-backed tools keep serving the former vault indefinitely. Hexadecimal has no unrepresentable input — each of the 256 byte values has exactly one two-character spelling — so the column is total over the fact by construction rather than by a bound that happens to be large enough, and the round trip is lossless: `os.fsdecode(bytes.fromhex(stored))` returns the observed string exactly, surrogates included.

Hexadecimal rather than base64, for three reasons that point the same way. `indexed_vault_handle` already spells opaque bytes as hexadecimal, so the record keeps one convention for "bytes carried in a text column" instead of two. Base64 has variant alphabets and optional padding, and two spellings of one value break the byte-equality comparison this record is decided by, whereas `bytes.hex()` admits exactly one spelling per input and is already case-canonical. The doubled length is the only cost, and it is precisely the cost this column is `text` in order to absorb.

`indexed_vault_assignment` is deliberately **not** encoded and stays a plain pathname string, because the two facts differ in origin rather than in degree. The assignment is `str(Path(users.vault_path))` — a purely lexical normalisation that reads no directory and whose output contains no non-ASCII character its input did not already contain — applied to a value the database itself supplied. A UTF-8 database cannot be holding a byte sequence it would refuse to accept back, so that fact round-trips by definition and the totality rule is already satisfied for it without an encoding. The real path is derived from the kernel and is constrained by nothing. The one pathname in this system that *is* environment-derived, `settings.vault_path`, never reaches this column: the classification is skipped entirely when there is no `users` row, and for an assigned user the root is read from `users.vault_path`. Encoding the assignment as well would buy no totality it does not already have, and would make unreadable the one fact an operator actually reads in a discard log.

Bounding the assignment string at `users.vault_path`'s own width would be sufficient today and is still refused. That sufficiency is a property of another column's DDL and of the current normaliser, not of this record: widening `vault_path`, or a normaliser that ever lengthens what it is given, silently restores the same failing transaction on the one write that must not roll back. The two pathname facts are also written and read as one unit, and giving them different types invites exactly the per-column "which one can hold what" reasoning that produced the defect. `text` and `character varying(n)` are the same storage in this database, so the bound buys nothing to trade against any of that.

`indexed_vault_handle` keeps its name and its width of 320 deliberately. The name is exactly what it stores — demoting a signal from proof to hardening does not change what the value is, and a vaguer name would make the column harder to reason about, not easier. The width follows the kernel's bound for the filesystems this system declares support for: a file handle is at most `MAX_HANDLE_SZ` (128) bytes of opaque payload, which is 256 hexadecimal characters, plus a handle type and a separator, and NFSv4's own maximum is the same 128 bytes; on ext4 and xfs the payload is eight bytes. This SHALL NOT be stated as an eternal maximum for every filesystem that may ever exist. A handle that does not fit SHALL be treated as unobtainable — recorded as null, so the hardening is simply absent for that root — and SHALL NOT be truncated, because a truncated handle compared by byte equality is a signal that can produce a spurious match. Shrinking the column would convert an absent hardening signal into a migration for no storage saving, since a `varchar` in this database costs only what it holds.

The handle is the one column the totality rule above does not govern, and the difference is what each column *is*. A handle is a **comparison token** with a documented external maximum, and its absence is a state the design already defines and handles — no hardening signal, no verdict changed — so refusing an oversized one costs nothing the design cannot express. A pathname is a **fact being mirrored**, and its absence is not a defined state: a record missing either pathname is no record at all, so a null-on-oversize rule there would convert an observation the pass actually made into "provenance unknown" and leave that user re-deriving on every pass forever.

Not backfilling is the load-bearing decision, not an omission. Deriving `indexed_vault_assignment` from `users.vault_path` would assert that an assigned user's index was built under the assignment it carries *now*, which is exactly the reassignment lag the record exists to detect: an administrator who reassigns and deploys before the next index pass would have rows built under one assignment stamped as belonging to another, after which both recorded facts agree, the pass takes its no-op branch, and the identical-path/identical-content link case that never heals becomes guaranteed rather than merely possible. A null record means "provenance unknown", which is the only true statement available at migration time, and the pass repairs such a user by re-deriving the index rather than by discarding it — so introducing the columns costs no vault-wide re-embed.

Not backfilling is also what makes the deploy order safe without any cross-container coordination. An index pass running under the previous code during or after the migration cannot write these columns, so it cannot produce a record for the new code to trust; every row is null when the new container starts, whatever that pass committed.

The marker is load-bearing for a stronger reason here than on a display column. This record is the sole input to a decision that can **delete a user's entire index** — `notes_metadata` and, by cascade, their embeddings and link rows. A same-named column of unknown provenance adopted as "the assignment those rows were scanned under" is a mass delete on the strength of a value nobody in this scheme wrote. So 016 SHALL refuse a pre-existing column of any of the three names that is not exactly its own — wrong type, wrong width, `NOT NULL`, carrying a server default, or unmarked — and SHALL refuse a partial set in which some of the three are present and others absent, naming what it found, rather than adopting it.

#### Scenario: Fresh database

- **WHEN** an empty database is migrated to head
- **THEN** `users.indexed_vault_assignment` and `users.indexed_vault_realpath` SHALL exist as nullable `text` and `users.indexed_vault_handle` as nullable `character varying(320)`, all three with no server default and all three carrying 016's marker as their column comment
- **AND** `alembic check` SHALL report no new upgrade operations

#### Scenario: A pathname longer than the assignment column's width is stored, not truncated

- **WHEN** a provenance record is written whose observed real path is longer than the width of `users.vault_path` — as when a short assignment is a symbolic link to a deeply nested directory
- **THEN** the write SHALL succeed and decoding the stored value SHALL yield the observed path exactly
- **AND** it SHALL NOT be truncated and SHALL NOT be stored as null

#### Scenario: A real path containing a non-UTF-8 component round-trips losslessly

- **WHEN** a provenance record is written for a real directory one of whose pathname components is not valid UTF-8, so that `os.path.realpath` returns a string carrying a surrogate escape
- **THEN** the write SHALL succeed rather than failing to encode
- **AND** the stored value SHALL be the lowercase hexadecimal encoding of `os.fsencode` of the observed path
- **AND** decoding the stored value SHALL yield the observed string exactly, surrogates included
- **AND** it SHALL NOT be truncated and SHALL NOT be stored as null

#### Scenario: The migration backfills nothing

- **WHEN** 016 runs on a database holding both assigned and unassigned users, each with existing `notes_metadata` rows
- **THEN** all three columns SHALL be null for every user, including every assigned one
- **AND** no `notes_metadata`, `note_embeddings` or `note_links` row SHALL be modified or deleted

#### Scenario: Re-running the migration does not overwrite a record

- **WHEN** the database is stamped back to 015 and upgraded again, after the indexer has recorded provenance whose assignment differs from the current `vault_path`
- **THEN** the recorded provenance SHALL be left unchanged in all three columns

#### Scenario: A foreign column of the same name is refused

- **WHEN** `users` already carries a column named `indexed_vault_assignment`, `indexed_vault_realpath` or `indexed_vault_handle` that is not exactly 016's column — a different type or width, `NOT NULL`, carrying a server default, or lacking the marker — and 016 runs
- **THEN** the migration SHALL fail naming what it found
- **AND** SHALL NOT adopt the column and SHALL leave the schema unchanged

#### Scenario: A partial set is refused

- **WHEN** one or two of the three columns are present, however well formed, and 016 runs
- **THEN** the migration SHALL fail naming what it found and SHALL leave the schema unchanged

#### Scenario: A complete pre-existing set is accepted

- **WHEN** all three columns are already present, nullable, exactly typed, default-free and marked, and 016 runs
- **THEN** the migration SHALL accept them as its own and complete without error

#### Scenario: The model and the migration agree on the marker

- **WHEN** `alembic check` runs at head
- **THEN** it SHALL report no operation for any of the three columns, which is only true while the models' declared column comments are byte-identical to the migration's marker

#### Scenario: Downgrade drops the marked set, all or nothing

- **WHEN** a database at 016 is downgraded to 015
- **THEN** all three marked columns SHALL be dropped
- **AND** a set in which any column lacks the marker SHALL be left in place instead
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
