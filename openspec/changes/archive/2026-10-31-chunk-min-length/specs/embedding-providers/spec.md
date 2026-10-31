## MODIFIED Requirements

### Requirement: The chunking format SHALL be versioned per note and outside the stored embedding fingerprint, while model identity stays global and refusing
The system SHALL track the chunking format as the integer `CHUNKING_VERSION`, record it per note in `notes_metadata.embedded_chunking_version`, and SHALL NOT add it to the stored embedding fingerprint. The chunking format covers:
- the sectioning rule;
- the minimum-length constant `CHUNK_MIN_CHARS` and the effective-minimum rule;
- the grouping and tail rules;
- the window-boundary lookback and its constant;
- the locator rule;
- whether window text is stored raw;
- the prefix, the embedded-text formatter (version 2 collapses whitespace runs longer than 8 characters) and the embedded-input formatter.

The current version SHALL be 2.

A change of chunking format SHALL re-select notes through the embed backlog, as the index-integrity requirement on chunking versions states. It SHALL NOT refuse startup or require the reset workflow. A change to any element of the chunking format SHALL bump `CHUNKING_VERSION`. The embedded-text formatter of each version SHALL be frozen in a per-version table (`EMBEDDED_TEXT_FORMATTERS`), in the same way as the per-version extraction cleaners. Vector reuse SHALL compare the stored row's text under its stamped version's formatter with the new text under the current formatter, so a version that changes the embedded-text formatter adds an entry and never edits one. Version 2 adds the collapse of whitespace runs longer than 8 characters, giving the entries `{1, 2}`. A version that changes the prefix builder SHALL clear the table to its own entry. `CHUNK_MIN_CHARS` SHALL NOT be a configuration setting and SHALL NOT be part of the stored fingerprint. Its effective value depends only on the chunk size, which is already in the fingerprint.

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
- **AND** every note SHALL be in the backlog, and a stored vector SHALL be reused when its prefix and its version-1 embedded text equal a new chunk's prefix and version-2 embedded text
