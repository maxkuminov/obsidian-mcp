## MODIFIED Requirements

### Requirement: A re-derive that skipped any file is incomplete, and an incomplete re-derive is not recorded
Any per-file skip during a re-deriving pass SHALL make that re-derive **incomplete**, and an incomplete re-derive SHALL NOT record provenance for that user. A skip is any discovered file the pass did not fully process — a directory it could not open, a file it could not read, stat, decode or parse, or a changed note whose links it could not extract. A note whose link extraction was **truncated at the declared cap** (`MAX_LINKS_PER_NOTE`) is NOT a skip: the cap is a bounded, deterministic, logged degradation, the rows the pass wrote are exactly the rows it derived, and the note is marked `links_truncated` so the truncation is durably visible. An incomplete pass SHALL still perform every repair it can, SHALL log the paths that kept it unrecorded, and the next pass SHALL re-derive again.

Without this rule the pass's structural claim is false. The scan continues past a file it cannot decode or read, and ordinary pruning keeps a row whose relative path exists under the assigned root — which is exactly the row a re-derive exists to replace. A vault that supplies a note at the same relative path as the previous vault, but whose bytes cannot be decoded, therefore leaves the previous vault's metadata row and its link rows untouched while the pass completes and records the new directory over them. One skipped file is enough to certify a foreign row.

The rule fails toward re-work rather than toward wrongness, and that is the trade the system SHALL take. The alternative — transactionally deleting the stale rows for each skipped path, as a fresh index would — is a second deletion path for index contents, and it destroys a row that may be the correct row for a file that was merely unreadable at that moment.

The system SHALL accept, and document, that a file which is permanently unreadable keeps that user in re-derive mode indefinitely. That is preferable to the alternative, which is recording a claim the pass could not establish: the record would then license the keep branch over rows the pass never visited. The cost is bounded — a re-derive parses and upserts a vault the pass already reads in full and makes no embedding call for unchanged content — and it SHALL be operator-visible: the pass SHALL name the offending paths in its log on every pass, so the file to fix is identified rather than left as an unexplained recurring cost. A capped note, by contrast, does not keep the user in re-derive mode: it is complete by construction and its degradation is carried on the row, not in the pass's skip list.

#### Scenario: An undecodable file withholds the record

- **WHEN** a re-deriving pass discovers a file it cannot decode and completes the rest of its work
- **THEN** the pass SHALL record no provenance for that user
- **AND** the next pass SHALL re-derive again

#### Scenario: A foreign row behind a skipped path is never certified

- **WHEN** a user was indexed from one vault, is assigned another, and the newly assigned vault holds a file at the same relative path whose bytes cannot be decoded
- **THEN** the pass SHALL NOT record the newly assigned root's provenance
- **AND** a later pass SHALL NOT take the keep branch over the row that path still carries

#### Scenario: A file that disappears during the scan withholds the record

- **WHEN** a file is discovered by a re-deriving pass and can no longer be read when the pass reaches it
- **THEN** the pass SHALL treat that path as a skip and SHALL record no provenance for that user

#### Scenario: Every link-extraction skip is recorded, including the unreachable one

- **WHEN** a re-deriving pass reaches a changed note it cannot extract links for — because it holds no buffered body for that path, or because that path has no index row to attach the links to
- **THEN** both cases SHALL be recorded as skips, so the record is withheld
- **AND** neither SHALL be dropped silently, whatever its likelihood, because the record is a claim that every surviving link row was written by that pass

#### Scenario: A capped note does not withhold the record

- **WHEN** a re-deriving pass reaches a changed note with more than `MAX_LINKS_PER_NOTE` links and processes every other discovered file without a skip
- **THEN** the first `MAX_LINKS_PER_NOTE` links SHALL be written, `links_truncated` SHALL be set on the note, an ERROR line SHALL be logged, and the pass SHALL record the provenance of the directory it scanned

#### Scenario: The skipped paths are named

- **WHEN** a re-deriving pass is incomplete
- **THEN** it SHALL log the paths responsible, bounded to a stated number with a count of the remainder

#### Scenario: A complete re-derive is recorded

- **WHEN** a re-deriving pass processes every discovered file without a skip and raises nothing
- **THEN** it SHALL record the provenance of the directory it scanned, after its last write

## ADDED Requirements

### Requirement: The link rebuild writes per note

During a pass, the link rows derived from a changed note SHALL be inserted before the next changed note's links are extracted, so that peak memory for link rows is bounded by one note's derived rows (at most `MAX_LINKS_PER_NOTE`) plus one insert batch, rather than by the number of changed notes in the pass. Body buffering for the pass is unchanged by this requirement.

#### Scenario: Many changed link-heavy notes in one pass

- **WHEN** a pass processes N changed notes each carrying the maximum number of links
- **THEN** the number of link rows held in memory at any instant SHALL NOT exceed one note's worth plus one insert batch (asserted by instrumenting the insert path), and every note's rows SHALL be present in `note_links` when the pass commits

### Requirement: A truncated extraction is recorded on the note

`notes_metadata` SHALL carry a `links_truncated` boolean (default false) that the pass sets when a note's link extraction was capped and clears when a later extraction of that note completes under the cap. The marker SHALL survive restarts (it is a column, not a log entry) and SHALL be what `get_links` reads.

#### Scenario: Marker set and cleared

- **WHEN** a note is indexed with more than `MAX_LINKS_PER_NOTE` links, then edited down to fewer, and indexed again
- **THEN** `links_truncated` SHALL be true after the first pass and false after the second

### Requirement: The link-grammar change is re-derived through the extraction version

Because the link grammar changed and `content_hash` cannot see a grammar change, `CURRENT_EXTRACTION_VERSION` SHALL be bumped with this change, with the new version's cleaning function identical to the previous one, so that the next pass re-extracts links and tags for every note under the new grammar and stamps the marker, while the cleaned-output comparison finds no difference and no note is re-embedded.

#### Scenario: One re-extraction pass, no re-embedding

- **WHEN** this change is deployed and the indexer completes its next pass
- **THEN** every note's `note_links` rows SHALL reflect the linear grammar, every note SHALL carry the new extraction version, and no note SHALL have been re-embedded on account of the version change

### Requirement: The frozen v0 cleaner is linear and byte-identical

The frozen v0 cleaning function retained for `extraction_version` comparison SHALL run in time linear in the input length, including inputs with many unclosed fence openers, and SHALL produce output byte-identical to the original regex-based implementation for every input. It SHALL split on `\n` only — never on `\r`, `\v`, `\f`, ` ` or any other separator `str.splitlines()` recognises — and SHALL treat a closer line's trailing run under the regex's Unicode `\s` semantics (so NBSP, `\x0b` and ` ` after the fence run close). Equivalence SHALL be established by a differential test that keeps the original regexes as an oracle and compares outputs over generated inputs covering unclosed openers, orphan closers, nested and adjacent mixed backtick/tilde fences, trailing ASCII and Unicode whitespace on closer lines, blank-line runs after closers, CRLF and lone `\r`, indented fences, empty blocks, and closer runs longer than the opener, plus every existing v0/v1 fixture.

#### Scenario: Many unclosed openers

- **WHEN** the v0 cleaner is invoked on n and 2n bytes of ```` ```x\n ```` repeated (n = 160 KB)
- **THEN** the 2n time divided by the n time SHALL be below 4, each run SHALL complete under a generous absolute ceiling, and both outputs SHALL equal the oracle's

#### Scenario: Differential equivalence

- **WHEN** the differential test generates inputs from the covered classes
- **THEN** for every input the scanner's output SHALL equal the oracle regex pair's output byte for byte
