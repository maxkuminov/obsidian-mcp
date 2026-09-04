## ADDED Requirements

### Requirement: `move_note` bounds the rewrites it plans for any one source

`move_note(rewrite_links=True)` SHALL refuse, before any mutation, when the rewrites planned for a single source note exceed `MAX_LINKS_PER_NOTE` (10,000) — the same per-note bound the indexer applies to link extraction — returning a tool-level error that names the offending source and the limit and leaving the note, every source and the database untouched. The refusal SHALL be a refusal and not a truncation: rewriting the first N links and leaving the rest would point them at the path the move just vacated while reporting the move as a success.

The bound is what makes `move_note`'s per-source work bounded at all. Extraction is capped at 10,000 links, so a note over the cap already carries fewer `note_links` rows than it has links; rewriting every link in it would make the vault bytes and the graph disagree in the other direction, and the number of rewrites in one 10 MiB note of `[[Old]] ` is roughly 1.7 million. The disposition is the one the other two `move_note` preflight refusals already use (`MAX_NOTE_BYTES` per rewritten source, `MAX_MOVE_REWRITE_BYTES` in aggregate): compute the whole plan first, abort while aborting is still free, and never leave the link graph asserting something the bytes do not.

#### Scenario: One source holds more rewrites than the cap

- **WHEN** `move_note(rewrite_links=True)` plans more than `MAX_LINKS_PER_NOTE` rewrites in a single backlink source
- **THEN** the move SHALL NOT happen: the note stays at its original path, no source is rewritten, `notes_metadata` and `note_links` are unchanged, and the tool returns an error naming that source and `MAX_LINKS_PER_NOTE`

#### Scenario: A source exactly at the cap

- **WHEN** a source holds exactly `MAX_LINKS_PER_NOTE` rewrites
- **THEN** the move SHALL proceed and rewrite all of them — the refusal is "more than the cap", not "at" it
