# wikilink-graph Specification

## Purpose
TBD - created by archiving change wikilink-graph-navigation. Update Purpose after archive.
## Requirements
### Requirement: Link extraction during indexing

The system SHALL extract `[[wikilinks]]`, `![[embeds]]`, and `[label](path.md)` markdown links from every note's body during indexing and persist them to a `note_links` table.

#### Scenario: Wikilinks captured

- **WHEN** the indexer processes a note containing `[[Project Plan]]` and `[[Folder/Other Note|alias]]`
- **THEN** two rows SHALL be inserted into `note_links` with `source_note_id` equal to the indexed note's ID, `link_text` set to the original wikilink text including any alias/anchor, and `kind` set to `"link"`

#### Scenario: Embeds captured separately

- **WHEN** the indexer processes a note containing `![[Diagram.md]]`
- **THEN** a row SHALL be inserted with `kind = "embed"`

#### Scenario: Markdown links to .md files captured

- **WHEN** the indexer processes a note containing `[See also](./Subfolder/Note.md)`
- **THEN** a row SHALL be inserted with `kind = "markdown"` and `target_path` set to the resolved relative path

#### Scenario: Code blocks ignored

- **WHEN** the indexer processes a note where `[[Foo]]` appears inside a fenced code block (` ``` `) or inline code (`` ` ``)
- **THEN** no row SHALL be inserted for that occurrence

#### Scenario: Re-extraction on content change

- **WHEN** a note's `content_hash` changes between index runs
- **THEN** existing rows in `note_links` for that `source_note_id` SHALL be deleted and replaced with the freshly-extracted set in the same database transaction as the metadata upsert

### Requirement: Wikilink target resolution

The system SHALL resolve each extracted link's target string to a `target_note_id` when an existing note matches; otherwise the row SHALL be stored with `target_note_id = NULL` (dangling).

#### Scenario: Path-style wikilink resolves to exact path

- **WHEN** a wikilink is `[[Folder/Subfolder/Note]]` and a note exists at `Folder/Subfolder/Note.md`
- **THEN** the link's `target_note_id` SHALL be set to that note's ID

#### Scenario: Bare-name wikilink prefers same-folder match

- **WHEN** the link is `[[Foo]]` and notes exist at both `<source-dir>/Foo.md` and `Other/Foo.md`
- **THEN** resolution SHALL prefer `<source-dir>/Foo.md`

#### Scenario: Bare-name wikilink with single match across vault

- **WHEN** the link is `[[Foo]]`, no same-folder match exists, and exactly one note in the vault has stem `Foo`
- **THEN** that note SHALL be selected

#### Scenario: Ambiguous bare-name fallback

- **WHEN** the link is `[[Foo]]`, no same-folder match exists, and multiple notes share the stem `Foo`
- **THEN** the alphabetically-first matching path SHALL be selected and recorded
- **AND** the original target string SHALL still be stored in `target_path`

#### Scenario: Unresolved link stored as dangling

- **WHEN** no note matches the resolution rules
- **THEN** the row SHALL be stored with `target_note_id = NULL` and `target_path` equal to the original target string (without alias or anchor)

#### Scenario: Anchor and alias do not change resolution

- **WHEN** the link is `[[Foo#Heading|alias]]`
- **THEN** resolution SHALL operate on `Foo` only
- **AND** the full original text SHALL be preserved in `link_text`

#### Scenario: Re-resolution when a target is created

- **WHEN** a note is created or its path changes
- **THEN** the indexer SHALL update any rows in `note_links` whose `target_path` would now resolve to that note, setting their `target_note_id` accordingly

#### Scenario: Re-resolution when a target is deleted

- **WHEN** a note is deleted
- **THEN** rows in `note_links` whose `target_note_id` referenced the deleted note SHALL have `target_note_id` set back to NULL

### Requirement: `get_backlinks` MCP tool

The system SHALL expose an MCP tool `get_backlinks(path, limit=50)` that returns notes linking TO `path`, including resolved links only.

#### Scenario: Returns notes linking to the target

- **WHEN** an agent calls `get_backlinks(path="Projects/Foo.md")` and three other notes contain `[[Foo]]` resolving to that file
- **THEN** the system SHALL return up to `limit` rows, each containing `source_path`, `source_title`, `link_text`, and `position` of the link in the source note

#### Scenario: No backlinks

- **WHEN** no resolved links target the supplied path
- **THEN** the system SHALL return an empty result set with a "no backlinks" message

#### Scenario: Path not found

- **WHEN** the supplied `path` does not match any indexed note
- **THEN** the system SHALL return an error message identifying the missing path

### Requirement: `get_links` MCP tool

The system SHALL expose an MCP tool `get_links(path)` that returns the list of links emanating FROM the note at `path`, distinguishing resolved and dangling links.

#### Scenario: Resolved and dangling shown together

- **WHEN** the source note has both resolved links and dangling references
- **THEN** the response SHALL include both, each row carrying `target_path`, `target_title` (NULL for dangling), `kind` (`link`/`embed`/`markdown`), `link_text`, `resolved` (boolean), and `position`

#### Scenario: Source note has no outgoing links

- **WHEN** the source note contains no link of any kind
- **THEN** the system SHALL return an empty result set with an explanatory message

### Requirement: `get_neighborhood` MCP tool

The system SHALL expose an MCP tool `get_neighborhood(path, depth=1, limit=50)` that returns the connected subgraph reachable from `path` via incoming or outgoing resolved links, up to `depth` hops.

#### Scenario: Default depth=1 returns immediate neighbors

- **WHEN** an agent calls `get_neighborhood(path="X.md")` with default depth and `X.md` has 5 outgoing and 7 incoming resolved links
- **THEN** the system SHALL return up to 12 distinct neighbor notes (deduplicated), each with `path`, `title`, `tags`, `distance=1`, and `via=path` of the source

#### Scenario: Depth >1 expands further

- **WHEN** an agent calls `get_neighborhood(path="X.md", depth=2)`
- **THEN** the system SHALL perform breadth-first expansion treating links as undirected and return distinct notes at distance 1 and 2, each annotated with the discovered `distance` and `via` path

#### Scenario: Limit enforced

- **WHEN** the BFS would return more than `limit` distinct notes
- **THEN** expansion SHALL stop once `limit` is reached and the response SHALL flag that results were truncated

#### Scenario: Limit clamped

- **WHEN** the agent passes `limit > 200`
- **THEN** the system SHALL clamp `limit` to 200

### Requirement: `find_related` MCP tool

The system SHALL expose an MCP tool `find_related(path, limit=10)` that returns notes most semantically similar to the source note based on chunk embeddings.

#### Scenario: Returns embedding-based neighbors

- **WHEN** an agent calls `find_related(path="X.md")` and the note has embedded chunks
- **THEN** the system SHALL average the source note's chunk embeddings, run a cosine-distance query against `note_embeddings`, exclude the source note, deduplicate to one row per note (keeping the highest similarity), and return the top `limit` results
- **AND** each result SHALL include `path`, `title`, `tags`, `similarity`, and a snippet (≤200 chars) from the best-matching chunk

#### Scenario: Source note not yet embedded

- **WHEN** the source note has no rows in `note_embeddings`
- **THEN** the system SHALL return a message indicating the note has not been embedded yet (rather than an empty list)

#### Scenario: Source note not found

- **WHEN** the supplied `path` does not match any indexed note
- **THEN** the system SHALL return an error message identifying the missing path

### Requirement: `find_orphans` MCP tool

The system SHALL expose an MCP tool `find_orphans(folder=None, limit=50)` that returns notes with zero incoming AND zero outgoing resolved links, optionally constrained by folder prefix.

#### Scenario: Returns isolated notes

- **WHEN** an agent calls `find_orphans()` and notes A and B have no resolved links to or from any other note
- **THEN** A and B SHALL appear in the result set, ordered by `modified_at` descending, with `path`, `title`, `tags`, `modified_at`

#### Scenario: Folder filter

- **WHEN** an agent calls `find_orphans(folder="Cards/")`
- **THEN** the system SHALL only consider notes whose `file_path` starts with `Cards/`

#### Scenario: Limit clamped

- **WHEN** the agent passes `limit > 500`
- **THEN** the system SHALL clamp `limit` to 500

### Requirement: Graph stats on the control panel

The control panel dashboard SHALL display graph health metrics: total links, dangling-link count, orphan-note count, and the top 5 most-linked-to notes.

#### Scenario: Dashboard widget renders

- **WHEN** an authenticated panel user loads `/admin/`
- **THEN** the page SHALL include a "Graph" section showing `total_links`, `dangling_links`, `orphan_count`, and a list of the 5 notes with the highest `target_note_id` count, each as a clickable link to the vault page

#### Scenario: Empty graph

- **WHEN** `note_links` is empty (e.g. before backfill completes)
- **THEN** the section SHALL render the metrics as zero with a "Link extraction in progress" indicator if the indexer reports it is still backfilling

### Requirement: Backfill on first deploy

The system SHALL backfill `note_links` for the entire vault on first deploy after this change ships.

#### Scenario: Empty links table on startup

- **WHEN** the indexer starts and `note_links` is empty
- **THEN** before the first periodic embed pass, the indexer SHALL iterate every note in `notes_metadata`, extract links, resolve targets, and bulk-insert rows
- **AND** the indexer SHALL log progress periodically (e.g. every 500 notes)

#### Scenario: Backfill is idempotent

- **WHEN** the backfill is interrupted and the indexer restarts
- **THEN** running it again SHALL produce a consistent `note_links` state without duplicate rows

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
