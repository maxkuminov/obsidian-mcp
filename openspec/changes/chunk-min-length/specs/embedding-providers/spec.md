## MODIFIED Requirements

### Requirement: The chunking format SHALL be versioned per note and outside the stored embedding fingerprint, while model identity stays global and refusing
The system SHALL track the chunking format as the integer `CHUNKING_VERSION`, record it per note in `notes_metadata.embedded_chunking_version`, and SHALL NOT add it to the stored embedding fingerprint. The chunking format covers:
- the sectioning rule;
- the minimum-length constant `CHUNK_MIN_CHARS` and the effective-minimum rule;
- the grouping and tail rules;
- the window-boundary lookback and its constant;
- the locator rule;
- whether window text is stored raw;
- the prefix and the embedded-input formatter.

The current version SHALL be 2.

A change of chunking format SHALL re-select notes through the embed backlog, as the index-integrity requirement on chunking versions states. It SHALL NOT refuse startup or require the reset workflow. A change to any element of the chunking format SHALL bump `CHUNKING_VERSION`. A change to the input formatter or the prefix builder that can alter a provider input for an unchanged `(embed_prefix, chunk_text)` pair SHALL also reset `INPUT_COMPATIBLE_CHUNKING_VERSIONS` to the new version alone. A bump that leaves both functions unchanged MAY add the new version to the existing set, and version 2 does so, giving `{1, 2}`. `CHUNK_MIN_CHARS` SHALL NOT be a configuration setting and SHALL NOT be part of the stored fingerprint. Its effective value depends only on the chunk size, which is already in the fingerprint.

This is not the per-row model identity that the fingerprint requirement forbids. That prohibition exists because vectors from two models in one column make distance meaningless. A chunking change leaves provider, model and dimensions unchanged, and those stay in the stored, refusing fingerprint, so legacy and re-chunked vectors share one space. A transition still changes ranking, and with it which notes fit inside a bounded page. That is why every vector-search response declares the transition while it lasts, as the index-integrity requirement on chunking versions states.

#### Scenario: A chunking bump does not refuse startup
- **WHEN** a build with a different `CHUNKING_VERSION` starts over an index whose stored fingerprint matches its configuration
- **THEN** startup SHALL proceed silently, and every note whose stamp `IS DISTINCT FROM` the running version, whether lower, higher or NULL, SHALL be in the embed backlog

#### Scenario: A model change still refuses
- **WHEN** the configured model differs from the stored fingerprint's, whatever the chunking version
- **THEN** startup SHALL refuse and name the reset workflow, as before

#### Scenario: The version-2 deploy over a version-1 index
- **WHEN** the version-2 build starts over an index built and certified by the version-1 build, with any previously valid configuration
- **THEN** the computed embedding fingerprint SHALL equal the stored one byte for byte, and startup SHALL proceed
- **AND** every note SHALL be in the backlog, and stored vectors whose `(embed_prefix, chunk_text)` pair recurs under version 2 SHALL be reused
