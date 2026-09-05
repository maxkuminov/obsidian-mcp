## MODIFIED Requirements

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

## ADDED Requirements

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
