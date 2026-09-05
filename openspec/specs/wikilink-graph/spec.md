# wikilink-graph Specification

## Purpose
TBD - created by archiving change wikilink-graph-navigation. Update Purpose after archive.
## Requirements
### Requirement: Link extraction during indexing

The system SHALL extract `[[wikilinks]]`, `![[embeds]]`, and `[label](path.md)` markdown links from every note's body during indexing and persist them to a `note_links` table. Extraction SHALL be bounded per note by `MAX_LINKS_PER_NOTE` (10,000), applied in document order; a note over the cap is a declared degradation (see "Link extraction is linear-time and bounded per note"), not a skip.

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
- **THEN** existing rows in `note_links` for that `source_note_id` SHALL be deleted and replaced with the freshly-extracted set — the first `MAX_LINKS_PER_NOTE` links in document order — in the same database transaction as the metadata upsert

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

### Requirement: Link extraction is linear-time and bounded per note

Link extraction (`extract_links`) and the whole of `move_note`'s link rewrite — the scanners that find the links AND the splice that applies them — SHALL run in time linear in the length of the input for every input, including adversarial ones. Every unbounded character class in the four link regexes SHALL be closed: wikilink target, anchor and alias classes SHALL exclude `[` and `]`; markdown link text SHALL exclude `[`; markdown href classes SHALL be bounded to 2,048 characters. The extraction grammar in `links.py` and the rewrite grammar in `tools.py` SHALL apply these rules identically. Extraction SHALL stop after `MAX_LINKS_PER_NOTE` (10,000) links, selected as the first N by document position across both link kinds, and SHALL report that the note was truncated. The indexer and the `move_note` rewrite planner SHALL invoke extraction through `asyncio.to_thread`, and the planner SHALL do the same for the per-source fence scan it hands the rewriter: both halves of the per-source work are CPU-bound over up to `MAX_NOTE_BYTES`, and they run once per backlink source while the process-wide `move_note` rewrite lock is held.

#### Scenario: Pathological inputs are parsed in linear time

- **WHEN** `extract_links` and the rewrite scanner are invoked on n and 2n bytes of each of `[[`, `]]`, `[[a`, `[[a#`, `[[a|`, `[a](`, `[a](x` (n = 512 KiB)
- **THEN** each SHALL find no links, the 2n time divided by the n time SHALL be below 4, and each run SHALL complete under a generous absolute ceiling

#### Scenario: The rewrite splice is linear on match-dense input

- **WHEN** the `move_note` rewrite runs over n and 2n bytes of `[[Old]] ` (n = 256 KiB), every occurrence of which resolves to the moved note and is rewritten
- **THEN** the 2n time divided by the n time SHALL be below 4
- **AND** the splice alone SHALL apply the 131,072 rewrites of a 1 MiB body in under 500 ms — the retired per-rewrite whole-string rebuild takes ~25 s on the same input
- **AND** the spliced output SHALL be byte-for-byte what that retired implementation produced, asserted against it as an oracle over a randomized corpus of spans

#### Scenario: Valid links are unchanged by the grammar

- **WHEN** the existing link-extraction and `move_note` rewrite test suites run against the linear grammar
- **THEN** every existing expectation SHALL hold

#### Scenario: Accepted differences are enumerated

- **WHEN** the grammar-difference test runs
- **THEN** it SHALL assert exactly these changes and no others: `[[Note|see [1]]]` and `[[Note#Sec [x]]]` produce no row (previously a row with a mangled alias/anchor); `[a[b](x.md)` produces a row to `x.md` whose `link_text` is `[b](x.md)`; an href longer than 2,048 characters produces no row; `[[[Foo]]` produces a row whose target is `Foo` (previously `[Foo`, which could never name a note); `[t](Foo [draft].md)` still produces a row
- **AND** it SHALL assert the write-side consequence of the first difference: a bracketed anchor or alias such as `[[Old#Results [draft]]]` is left untouched by `move_note(rewrite_links=True)` as well as absent from `note_links`, an accepted difference recorded in `docs/architecture/vault-tools.md` and NOT to be closed by re-admitting `[`/`]` into those classes

#### Scenario: Extraction and rewrite grammars agree

- **WHEN** a corpus of single-line links in the bare (non-angle-bracket) href form is run through both the extraction regexes and the rewrite regexes
- **THEN** both SHALL accept and reject the same members
- **AND** the two pre-existing divergences — the rewrite scanner does not match the CommonMark `<href>` form, and its anchor class crosses newlines — SHALL be recorded in the same test as known gaps outside this change

#### Scenario: A note with more links than the cap

- **WHEN** the indexer processes a note containing more than `MAX_LINKS_PER_NOTE` links
- **THEN** exactly the first `MAX_LINKS_PER_NOTE` links in document order SHALL be persisted to `note_links`, `notes_metadata.links_truncated` SHALL be set for that note, one ERROR log line SHALL name the note path and the cap (the true total is not computed — counting it would require the unbounded extraction the cap exists to prevent), and the pass SHALL remain complete

#### Scenario: Extraction is dispatched off the loop

- **WHEN** the indexer extracts links and tags from a note, or `move_note` plans a rewrite
- **THEN** the extraction call SHALL be dispatched through `asyncio.to_thread` (a test asserts the dispatch)
- **AND** for `move_note` the per-source fence scan SHALL be dispatched the same way (the same test asserts both, so dispatching only the rewrite does not pass)

### Requirement: `get_links` reports a truncated extraction

`get_links(path)` SHALL include `truncated: true` in its result when `notes_metadata.links_truncated` is set for that note, so an agent never reads a capped set as complete.

#### Scenario: Truncated note

- **WHEN** `get_links` is invoked on a note whose extraction was capped
- **THEN** the result SHALL carry `truncated: true` alongside the persisted links

#### Scenario: Ordinary note

- **WHEN** `get_links` is invoked on a note under the cap
- **THEN** the result SHALL carry `truncated: false` (or omit the field)

### Requirement: `get_links` bounds its own result

`get_links` SHALL accept a `limit` (default 100, clamped to 1..500 as `get_backlinks` is) and SHALL return at most that many link rows, in document order. When more rows exist than were returned, the result SHALL say how many rows are persisted for that note, so a page is never read as the whole set, SHALL state the effective `limit` it applied, and SHALL state that rows past the hard cap of 500 are not reachable through this tool.

The default SHALL be strictly below the hard cap. A default equal to the cap makes the over-limit notice's "raise `limit`" advice unactionable — the caller is already at the ceiling — so the notice would be instructing an agent to retry a call that cannot return anything new; the notice SHALL therefore offer that advice only while `limit` is below the cap.

Each returned row's `link_text` SHALL be clipped to 120 characters with an ellipsis, as `get_backlinks` clips its excerpt. `link_text` is stored verbatim and a wikilink alias is caller-controlled, so an unclipped row can carry arbitrary text into a tool result — model input — past the response-level caps.

#### Scenario: A note with more link rows than the limit

- **WHEN** `get_links` is invoked with a `limit` smaller than the number of link rows persisted for the note
- **THEN** at most `limit` rows SHALL be returned and the result SHALL state both the number shown and the number of persisted rows (the persisted count, not the size of the returned page, which omits rows resolving outside the caller's own notes)

#### Scenario: An out-of-range limit

- **WHEN** `get_links` is invoked with a `limit` of zero, a negative number, or a number above the hard cap
- **THEN** the limit SHALL be clamped to the 1..500 range rather than refused or honoured unbounded
- **AND** the effective limit SHALL be observable in the result's notice, so the clamp is asserted on what the tool applied and not only on a row count a fixture cannot distinguish

#### Scenario: A note with more rows than the hard cap

- **WHEN** `get_links` is invoked at the hard cap on a note with more than 500 persisted link rows
- **THEN** the result SHALL NOT advise raising `limit`, and SHALL say that the rows past the first 500 are not reachable through this tool

#### Scenario: A row with a very long link text

- **WHEN** a returned link row's `link_text` is longer than 120 characters
- **THEN** the rendered row SHALL carry the first 120 characters followed by an ellipsis, and SHALL NOT carry the remainder

