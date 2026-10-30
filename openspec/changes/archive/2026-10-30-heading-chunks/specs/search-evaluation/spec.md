## ADDED Requirements

### Requirement: A semantic arm SHALL record the retrieval fingerprint and the replay copy's legacy-chunked note count
For every database-backed arm whose membership includes `semantic`, the harness's `run` step SHALL record, in the arm's staged metadata and in its `report.json` arm entry, `retrieval_fingerprint()` as the producing process computed it, and `legacy_chunked_notes`. `legacy_chunked_notes` is the number of the replay copy's notes, **within the arm's provenance owner**, that hold vector rows and whose `embedded_chunking_version` is not the current `CHUNKING_VERSION`. The provenance owner is the `user_id` the export was filtered to (`USER_ID=` or `mine --user-id`), read from the `user_id` of the report's queries (the miner guarantees one owner for a single-user report), and compared NULL-safe so the ownerless scope is covered. A single-user report with no queries records null. Only when the report's provenance is `"multi"`, and the report is therefore not attachable, SHALL the count cover all owners. It SHALL be computed by one read-only `SELECT` carrying that owner predicate, and SHALL be null when the column does not exist. A `runfile:` arm SHALL carry the values its staged metadata holds, or null when it holds none. Arms of `metadata` membership SHALL NOT gain either key, so the committed fixture report is unchanged. Neither value SHALL contain query text, a note path or the instruction text.

#### Scenario: A candidate run on a fully re-embedded copy
- **WHEN** the built-in `semantic` arm runs on a copy whose every embedded note carries the current chunking version
- **THEN** its report entry SHALL carry the retrieval fingerprint and `legacy_chunked_notes: 0`

#### Scenario: A baseline from an older harness
- **WHEN** a `runfile:` arm staged by a harness without these fields is scored
- **THEN** its report entry SHALL carry null for both, and scoring SHALL proceed

#### Scenario: Fixture report unchanged
- **WHEN** `make search-eval` runs on the committed synthetic fixture
- **THEN** its stable projection SHALL equal the committed golden file

#### Scenario: Another tenant's rows cannot change the count
- **WHEN** a restored copy holds users 7 and 8, the export used `USER_ID=7`, user 7 has no legacy-chunked notes and user 8 has 37
- **THEN** the staged metadata and the attachable report SHALL carry `legacy_chunked_notes: 0`
- **AND** changing, adding or deleting user 8's rows SHALL NOT change any byte of the staged metadata or of the report
