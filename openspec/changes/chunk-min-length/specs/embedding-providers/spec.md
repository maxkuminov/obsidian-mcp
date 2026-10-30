## MODIFIED Requirements

### Requirement: The chunking format SHALL be versioned per note and outside the stored embedding fingerprint, while model identity stays global and refusing
The system SHALL track the chunking format as the integer `CHUNKING_VERSION`, recorded per note in `notes_metadata.embedded_chunking_version`, and SHALL NOT add the version to the stored embedding fingerprint. The chunking format covers the sectioning rule, the minimum-length grouping and its tail rule, the window boundary lookback, the locator rule, the prefix and the embedded-input formatter. The current version SHALL be 2.

- A change of chunking format SHALL re-select notes through the embed backlog, as the index-integrity requirement on chunking versions states. It SHALL NOT refuse startup or require the reset workflow.
- Any change to the grouping, the tail rule, the lookback constant or the locator rule SHALL bump `CHUNKING_VERSION`.
- Any change to the input formatter that can alter a provider input for an unchanged `(embed_prefix, chunk_text)` pair SHALL bump `CHUNKING_VERSION`, and SHALL reset `INPUT_COMPATIBLE_CHUNKING_VERSIONS` to the new version alone.
- A bump that leaves the input formatter and the prefix builder unchanged MAY add the new version to the existing set. Version 2 does so, giving `{1, 2}`.

This is not the per-row model identity the fingerprint requirement forbids. That prohibition exists because vectors from two models in one column make distance meaningless. A chunking change leaves the provider, the model and the dimensions unchanged. Those stay in the stored, refusing fingerprint, so legacy and re-chunked vectors share one space. A transition still changes ranking, and with it which notes fit inside a bounded page. That is why every vector-search response declares the transition while it lasts, as the index-integrity requirement on chunking versions states.

#### Scenario: A chunking bump does not refuse startup
- **WHEN** a build with a different `CHUNKING_VERSION` starts over an index whose stored fingerprint matches its configuration
- **THEN** startup SHALL proceed silently
- **AND** every note whose stamp `IS DISTINCT FROM` the running version, whether lower, higher or NULL, SHALL be in the embed backlog

#### Scenario: A model change still refuses
- **WHEN** the configured model differs from the stored fingerprint's, whatever the chunking version
- **THEN** startup SHALL refuse and name the reset workflow, as before

#### Scenario: The version-2 deploy over a version-1 index
- **WHEN** the version-2 build starts at the default `CHUNK_MIN_CHARS` over an index built and certified by the version-1 build
- **THEN** startup SHALL proceed
- **AND** every note SHALL be in the backlog
- **AND** stored vectors whose `(embed_prefix, chunk_text)` pair recurs under version 2 SHALL be reused

## ADDED Requirements

### Requirement: A non-default `CHUNK_MIN_CHARS` SHALL be part of the stored embedding fingerprint, and the default SHALL leave the fingerprint unchanged
The system SHALL accept `CHUNK_MIN_CHARS` as an integer from 0 to 1,000 with a default of 200. It SHALL refuse to start, naming both values, when `CHUNK_MIN_CHARS × 2` exceeds `CHUNK_SIZE × 4`.

The stored embedding fingerprint SHALL include a `chunk_min_chars` field **only** when the configured value differs from the default. At the default, its canonical rendering SHALL be byte-identical to the rendering before this setting existed. A configured value that differs from the one the stored vectors were built under SHALL refuse startup and name the reset workflow, as `CHUNK_SIZE` does. The setting SHALL carry the reset pointer in the configuration example and in the reference documentation. The retrieval fingerprint SHALL include the field under the same rule.

#### Scenario: Default deploy over an existing index
- **WHEN** a build with this setting starts at `CHUNK_MIN_CHARS=200` over an index whose fingerprint was stored before the setting existed
- **THEN** the computed fingerprint SHALL equal the stored one byte for byte, and startup SHALL proceed

#### Scenario: A non-default value refuses
- **WHEN** an index built at the default is started with `CHUNK_MIN_CHARS=300`
- **THEN** startup SHALL refuse, name `chunk_min_chars` as the differing field and name the reset workflow

#### Scenario: Returning to the default over a non-default index refuses
- **WHEN** an index built at `CHUNK_MIN_CHARS=300` is started at 200
- **THEN** startup SHALL refuse and name the reset workflow

#### Scenario: Out-of-bounds values are refused
- **WHEN** `CHUNK_MIN_CHARS` is negative, above 1,000, or more than half of `CHUNK_SIZE × 4`
- **THEN** the server SHALL refuse to start with an error naming the value and the bound it breaks
