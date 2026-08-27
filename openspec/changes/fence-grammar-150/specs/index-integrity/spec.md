## ADDED Requirements

### Requirement: A masker grammar change forces re-derivation without corrupting note identity

Link rows, inline tags, and embedded vectors are derived through the shared fence recognizer, but re-derivation is normally gated on `content_hash`, which does not change when only the grammar changes. A grammar change SHALL therefore ship with a versioned re-derivation mechanism: a per-note extraction-version marker, distinct from `content_hash`, compared against a code-level current version. When a note's marker is stale, the next index pass SHALL re-extract its links and tags, and SHALL then stamp the marker. Grammar-attributable embedding invalidation SHALL occur exactly when the note's recognised fence spans differ between **the grammar that stamped the note's recorded version and the current grammar** — each grammar version's recognizer is retained frozen while any row remains stamped with it, so the comparison is direction-aware and works for rollbacks as well as upgrades. Independent invalidators (content change, `file_path` change, provider or configuration change, exclusion reconciliation) are cumulative: the span comparison SHALL NOT suppress an invalidation any other requirement mandates. `content_hash` SHALL always hold the true hash of the note's bytes — it SHALL NOT be nulled or overwritten with a sentinel — so hash-based move/rename matching keeps working throughout the remediation window.

#### Scenario: The fence-grammar deploy refreshes links and tags

- **WHEN** this change's migration has run and the indexer completes its next pass
- **THEN** every note's `note_links` rows and extracted tags SHALL reflect the new grammar

#### Scenario: Embedding invalidation is scoped to affected notes

- **WHEN** the re-derivation pass processes a note whose recognised fence spans are identical under the old and new grammars, and no independent invalidator applies (no content change, no `file_path` change, no provider or configuration change, no exclusion reconciliation)
- **THEN** the grammar migration SHALL NOT cause that note to be re-embedded — span comparison governs only invalidation attributable to the grammar change and never suppresses an invalidation another requirement mandates
- **WHEN** it processes a note whose spans differ (e.g. one containing an indented fence around text)
- **THEN** that note's embeddings SHALL be rebuilt from the newly cleaned text on a subsequent embed pass

#### Scenario: Move detection survives the remediation window

- **WHEN** a note is externally renamed after the migration runs but before the re-derivation pass reaches it
- **THEN** the indexer's content-hash move matching SHALL still identify it as the same note (its `content_hash` is real), preserving row identity rather than delete-and-reinserting — the path-change invalidation the existing requirements mandate for a move still applies
