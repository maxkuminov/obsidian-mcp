## ADDED Requirements

### Requirement: Link and tag extraction ignore code per the code-masking grammar

Wikilink, embed, and markdown-link extraction, and inline tag extraction, SHALL scan text masked under the `code-masking` capability's CommonMark-subset fence grammar, so an occurrence inside an indented, longer-closed, tilde, or unterminated fenced block produces no `note_links` row and no tag.

#### Scenario: A link inside an indented fence is not extracted

- **WHEN** the indexer processes a note where `[[Foo]]` appears between an opening fence indented by three spaces and its closer
- **THEN** no `note_links` row SHALL be inserted for that occurrence

#### Scenario: A tag below an unterminated fence is not extracted

- **WHEN** a note opens a fence that is never closed and `#tag` appears below it
- **THEN** `#tag` SHALL NOT appear in the note's extracted tags

### Requirement: A masker grammar change forces re-extraction of derived rows

Because link and tag extraction are skipped when a note's `content_hash` is unchanged, a change to the masking grammar SHALL ship with a remediation that forces the next indexer pass to re-extract links and tags for every note, without forcing re-embedding.

#### Scenario: The fence-grammar deploy refreshes the graph

- **WHEN** this change's data migration has run and the indexer completes its next pass
- **THEN** every note's `note_links` rows and extracted tags SHALL reflect the new grammar

#### Scenario: No re-embedding is triggered

- **WHEN** the remediation clears `content_hash` and the indexer recomputes it to the same value
- **THEN** notes whose `embedded_content_hash` equals the recomputed hash SHALL NOT be re-embedded
