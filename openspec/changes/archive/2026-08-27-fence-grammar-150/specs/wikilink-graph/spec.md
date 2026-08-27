## ADDED Requirements

### Requirement: Link and tag extraction ignore code per the code-masking grammar

Wikilink, embed, and markdown-link extraction, and inline tag extraction, SHALL scan text masked under the `code-masking` capability's grammar, so an occurrence inside an indented, longer-closed, tilde, or unterminated column-zero fenced block produces no `note_links` row and no tag, and — per the frontmatter-boundary requirement — a fence-shaped line inside a valid frontmatter block never suppresses extraction from the body.

#### Scenario: A link inside an indented fence is not extracted

- **WHEN** the indexer processes a note where `[[Foo]]` appears between an opening fence indented by three spaces and its closer
- **THEN** no `note_links` row SHALL be inserted for that occurrence

#### Scenario: A tag below an unterminated column-zero fence is not extracted

- **WHEN** a note opens a column-zero fence that is never closed and `#tag` appears below it
- **THEN** `#tag` SHALL NOT appear in the note's extracted tags

#### Scenario: Frontmatter cannot suppress body extraction

- **WHEN** a note's valid frontmatter contains a YAML scalar line shaped like an indented fence opener, and its body contains `#real` and `[[Old]]`
- **THEN** the tag and the link SHALL be extracted, and `move_note(rewrite_links=True)` SHALL rewrite `[[Old]]`
