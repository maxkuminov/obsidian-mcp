## MODIFIED Requirements

### Requirement: A move recomputes the title through the shared derivation

Both move paths SHALL leave `notes_metadata.title` exactly what a fresh index of the file at its new path would produce, computed by the one shared title-derivation helper (sanitize the frontmatter `title`, stringify non-strings, fall back to the filename stem for falsy values, bound to the column width), and SHALL in the same statement replace the keyword vector's head — the weight-`A` title and aliases parts and the weight-`B` tags part — with that title, the note's aliases and the note's tags — `head(title, aliases, tags) || ts_filter(content_tsvector, '{d}')` through the shared `rehead` builder, the tags text from the shared `tags_text` rule — while preserving `body_excerpt` and the body's lexemes, weights and relative positions (absolute positions MAY shift by the previous head's length; matching, phrase search and ranking are invariant under that shift). The indexer's id-preserving move detection SHALL take the title, aliases and tags from the entry it parsed for the new path. `move_note` SHALL read the moved note through the destination's validated descriptor after the move stands, parse its frontmatter with the same parser the indexer uses, and derive the title and aliases from that and the tags from that body and frontmatter through the shared tag extraction; only when that read or parse fails MAY it fall back to deriving the title and aliases from the row's stored sanitized frontmatter and the tags from the row's stored `tags`, which is best-effort and self-heals at the next content change. Both move paths are keyword-vector writers and SHALL behave as every other one does: the write SHALL happen in a transaction that acquired the index generation lock before any row or table lock and re-read the keyword fingerprint after acquiring it, and SHALL NOT write under a fingerprint that differs from the running configuration. For `move_note` that means the generation lock and the fingerprint check are the metadata transaction's first statements; a lock wait past the statement timeout or a fingerprint mismatch aborts that transaction, the move on disk stands and is reported as a partial outcome, and the next pass's move detection repairs path, title and head.

**Accepted limitation (V-N6):** because the generation lock is taken inside the metadata transaction, a `move_note` that arrives while a pass or rebuild holds the lock may wait up to the lock's bound (60 s) while it still holds its move lock and its concurrency leases; the wait ends in the move or in the partial outcome above, never in a wrong write.

#### Scenario: A move racing the keyword rebuild cannot leave a stale head

- **WHEN** `rebuild_tsvectors_all_scopes` holds the generation lock and `move_note(Alpha.md → Beta.md)` is called concurrently
- **THEN** the move's metadata transaction SHALL wait for the rebuild's transaction to end before writing, and afterwards the row SHALL carry `beta` and not `alpha` at weight `A`
- **AND** when instead the move commits before the rebuild reaches that row, the rebuild's certified UPDATE SHALL miss, re-read, and rebuild the row at its new path with the new title

#### Scenario: A fingerprint mismatch aborts the move's metadata write

- **WHEN** the stored keyword fingerprint names a different `FTS_CONFIGS` than the running process and `move_note` moves a note
- **THEN** the file SHALL be moved, no `notes_metadata` row SHALL be written by that call, the result SHALL report the partial outcome, and the next index pass SHALL repair the row through its move detection

#### Scenario: Renaming a note with no frontmatter title updates the title

- **WHEN** `Alpha.md` (no frontmatter `title`) is renamed to `Beta.md`, by either an external move the indexer detects or by `move_note`
- **THEN** index-backed tools SHALL report the note's title as `Beta` after the move is indexed

#### Scenario: An explicit frontmatter title survives a move

- **WHEN** a note whose frontmatter sets `title: Roadmap` is moved or renamed
- **THEN** its title SHALL remain `Roadmap`

#### Scenario: Falsy frontmatter titles agree with a fresh index

- **WHEN** a note whose frontmatter `title` is `false`, `0`, `[]`, `{}`, or the empty string is moved from `Alpha.md` to `Beta.md`
- **THEN** the stored title SHALL be `Beta` — the same value a fresh index of the file would derive — on both move paths

#### Scenario: Stale indexed frontmatter does not decide the title

- **WHEN** a note's frontmatter `title` was added or removed on disk after the last index pass, and `move_note` then moves the note
- **THEN** the stored title SHALL be derived from the file's current frontmatter, not from the stale indexed copy

#### Scenario: A rename is searchable by its new name and not its old one

- **WHEN** `Alpha.md` is renamed to `Beta.md` by either move path and no body mentions either word
- **THEN** `keyword_search("Beta")` SHALL return the note with `matched_in: title`, `keyword_search("Alpha")` SHALL NOT return it, `body_excerpt` SHALL be byte-equal to its value before the move, and the body part of the vector (`ts_filter(content_tsvector, '{d}')`) SHALL hold the same lexemes, weights and relative positions as before — matching every body phrase query and ranking it identically — its absolute positions being permitted to shift by the previous head's length

#### Scenario: A move keeps the note findable by its tags

- **WHEN** a note carrying the tag `hoa` (and no `hoa` in its title, aliases or body) is moved from `Alpha.md` to `Beta.md` by either move path
- **THEN** `keyword_search("hoa")` SHALL return it at `Beta.md` with `matched_in: tags`, and its vector SHALL still hold `hoa` at weight `B`

#### Scenario: move_note's stored-tags fallback keeps the tag searchable through the move and the next pass

- **WHEN** in multi-user mode two users each hold `Alpha.md`, user 1's tagged `hoa` and user 2's tagged `other`, neither with `hoa` in its title, aliases or body; user 1 calls `move_note(Alpha.md → Beta.md)` and the destination read fails after the rename stands (forced by the test)
- **THEN** the re-head SHALL bind the tags selected from user 1's own row (the owner predicate in the fallback statement), user 1's vector SHALL hold `hoa` and not `other` at weight `B`, and user 1's `keyword_search("hoa")` SHALL return `Beta.md` with `matched_in: tags`
- **AND** after the next incremental pass, in which the note's content hash is unchanged and only its stat is refreshed, the vector SHALL still hold `hoa` at weight `B` and the search SHALL still return it

## ADDED Requirements

### Requirement: Tags enter the keyword vector by one shared rule, bounded, with nested tags expanded

The tags text bound to every keyword-vector writer SHALL be `tags_text(tags)` in `src/services/fts.py`, over the list the shared tag extraction (`vault.extract_tags`) returns for the note — the list `notes_metadata.tags` stores — or, for migration 035 and `move_note`'s fallback, over the stored `notes_metadata.tags`. For each tag in list order it SHALL emit one line holding the tag, followed, when the tag contains `/`, by each non-empty `/`-separated segment separated by spaces; lines SHALL be joined with a newline; U+0000, U+0002 and U+0003 SHALL be removed; and the text SHALL be cut at `FTS_TAGS_MAX_CHARS` (2,048) characters at a whole-line boundary, dropping the line that would cross the bound and every later line. `None` and an empty list SHALL give the empty string. The incremental pass SHALL bind the tags of the same entry whose `tags` column it upserts in the same transaction, the full rebuild SHALL derive them from the bytes it verified against the row's hash, and the indexer SHALL rewrite a note's keyword vector in the same pass whenever an edit to its frontmatter `tags` or inline `#tags` changes its content hash.

#### Scenario: A nested tag yields its full form and its segments

- **WHEN** a note's extracted tags are `["ctx/phone", "hoa"]`
- **THEN** `tags_text` SHALL return `ctx/phone ctx phone` and `hoa` on two lines, and the vector SHALL hold `ctx/phone`, `ctx`, `phone` and `hoa` at weight `B`

#### Scenario: The tags text is bounded at whole tags

- **WHEN** a note carries 1,000 tags of 30 characters each
- **THEN** the tags text SHALL be at most 2,048 characters, SHALL end at the end of a whole tag line, and the note's keyword vector SHALL still be written

#### Scenario: A tag edit alone updates the vector

- **WHEN** a note's only change is a frontmatter edit from `tags: [alpha]` to `tags: [beta]`, and the next incremental pass commits
- **THEN** `keyword_search("beta")` SHALL return the note with `matched_in: tags`, and `keyword_search("alpha")` SHALL NOT return it

#### Scenario: An inline tag inside code is not indexed as a tag

- **WHEN** a note's only `#gamma` is inside a fenced code block
- **THEN** its vector SHALL hold no weight-`B` lexeme for `gamma`, as `notes_metadata.tags` holds no `gamma`

