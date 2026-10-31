## MODIFIED Requirements

### Requirement: A semantic arm SHALL record the retrieval fingerprint and the replay copy's legacy-chunked note count
For every database-backed arm whose membership includes `semantic`, the harness's `run` step SHALL record three values, in the arm's staged metadata and in its `report.json` arm entry:

- **`retrieval_fingerprint()`**, as the producing process computed it.
- **`legacy_chunked_notes`**: the number of the replay copy's notes, **within the arm's provenance owner**, that hold vector rows and whose `embedded_chunking_version` is not the current `CHUNKING_VERSION`.
- **`chunk_lengths`**: `{chunks, p10, p50, p90, whole_short, under_min, raw_over_window}` over the provenance owner's stored chunks, or null when the owner has no chunks or the copy predates migration 034.
  - `clen` SHALL be the index-integrity collapsed length, computed in SQL as `length(btrim(regexp_replace(chunk_text, '[\t\n\v\f\r ]+', ' ', 'g')))`. That expression and the chunker's Python function SHALL be kept as one pair and tested for agreement.
  - `m` SHALL be the running effective minimum and `W` the running window in characters.
  - The percentiles SHALL be discrete percentiles of `clen`.
  - `whole_short` SHALL count chunks with `clen < m` whose note holds exactly one chunk.
  - `under_min` SHALL count chunks with `clen < m` whose note holds more than one chunk, with no other exception, and SHALL be 0 for a conforming chunker at any `CHUNK_SIZE` above 100 (below it, index-integrity's small-window residue may count).
  - `raw_over_window` SHALL count chunks whose raw `length(chunk_text)` exceeds `W`.

**Provenance owner.** It is the `user_id` the export was filtered to (`USER_ID=` or `mine --user-id`), read from the `user_id` of the report's queries. The miner guarantees one owner for a single-user report. It SHALL be compared NULL-safe, so the ownerless scope is covered. A single-user report with no queries records null for both counts. Only when the report's provenance is `"multi"`, and the report is therefore not attachable, SHALL the counts cover all owners.

**Computation.**
- Each count SHALL be computed by one read-only `SELECT` carrying that owner predicate.
- `legacy_chunked_notes` SHALL be null when the column does not exist.
- A `runfile:` arm SHALL carry the values its staged metadata holds, or null when it holds none.
- Arms of `metadata` membership SHALL NOT gain any of these keys, so the committed fixture report is unchanged.
- No value SHALL contain query text, a note path, chunk text or the instruction text.

#### Scenario: A candidate run on a fully re-embedded copy
- **WHEN** the built-in `semantic` arm runs on a copy whose every embedded note carries the current chunking version
- **THEN** its report entry SHALL carry the retrieval fingerprint, `legacy_chunked_notes: 0`, and a `chunk_lengths` object with `under_min: 0`

#### Scenario: A baseline from an older harness
- **WHEN** a `runfile:` arm staged by a harness without these fields is scored
- **THEN** its report entry SHALL carry null for each missing field, and scoring SHALL proceed

#### Scenario: Fixture report unchanged
- **WHEN** `make search-eval` runs on the committed synthetic fixture
- **THEN** its stable projection SHALL equal the committed golden file

#### Scenario: Another tenant's rows cannot change the counts
- **WHEN** a restored copy holds users 7 and 8, the export used `USER_ID=7`, user 7 has no legacy-chunked notes and user 8 has 37, and user 8 holds chunks shorter than the minimum
- **THEN** the staged metadata and the attachable report SHALL carry `legacy_chunked_notes: 0`, and a `chunk_lengths` computed from user 7's chunks alone
- **AND** changing, adding or deleting user 8's rows SHALL NOT change any byte of the staged metadata or of the report
