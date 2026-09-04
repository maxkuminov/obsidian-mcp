## ADDED Requirements

### Requirement: Link extraction is linear-time and bounded per note

Link extraction (`extract_links`) and the `move_note` link-rewrite scanner SHALL run in time linear in the length of the input for every input, including adversarial ones, and SHALL stop after `MAX_LINKS_PER_NOTE` (10,000) links, reporting that the note was truncated. The link grammar SHALL exclude `[` and `]` from wikilink targets and from markdown link text, matching Obsidian's own rules for note names and link targets, so that a run of `[[` or `]]` fails at each position in constant time. The indexer and the `move_note` rewrite planner SHALL invoke extraction through a worker thread so the event loop is not held while a note is parsed.

#### Scenario: A pathological run of brackets is parsed in linear time

- **WHEN** `extract_links` is invoked on 1 MiB of `[[` (and separately `]]`, `[[a`, and `[a](`)
- **THEN** it SHALL return within a bounded time (the regression test asserts under 0.5 s on the CI runner) and SHALL find no links

#### Scenario: Valid links are unchanged by the grammar

- **WHEN** the existing link-extraction and `move_note` rewrite test suites run against the linear grammar
- **THEN** every existing expectation SHALL hold, and the one accepted difference — a stray `[` inside markdown link text, e.g. `[a[b](x.md)` — SHALL be enumerated in a test

#### Scenario: A note with more links than the cap

- **WHEN** the indexer processes a note containing more than `MAX_LINKS_PER_NOTE` links
- **THEN** exactly the first `MAX_LINKS_PER_NOTE` links SHALL be persisted to `note_links`, one ERROR log line SHALL name the note path, the cap and the total count, and the pass SHALL remain complete (the truncation is a declared degradation, not a skip)

#### Scenario: Extraction does not hold the event loop

- **WHEN** the indexer extracts links and tags from a note
- **THEN** the extraction SHALL run in a worker thread (`asyncio.to_thread`), and a concurrent request on the same loop SHALL be served while it runs
