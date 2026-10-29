## ADDED Requirements

### Requirement: Aliases are derived by one shared rule, and the keyword excerpt is a prefix of the indexed text

A note's aliases SHALL be derived by one rule, `note_aliases(frontmatter)`, shared by every keyword-vector writer: it SHALL read the frontmatter keys `aliases` and `alias` in that order, keeping first occurrence; a list SHALL contribute each scalar item rendered as the shared title rule renders a scalar (`str()`, a non-finite number as its YAML token), dropping nested lists and mappings; a scalar string SHALL be split on commas and stripped as the shared tag rule splits a scalar `tags` string; any other scalar SHALL be rendered the same way; U+0000, U+0002 and U+0003 SHALL be removed and empty strings dropped; and the joined text SHALL be cut at `FTS_ALIASES_MAX_CHARS` (2,048) characters. The rule SHALL be total over both a freshly parsed mapping and the stored sanitised JSONB, and SHALL return the same list for the same note from either.

`notes_metadata.body_excerpt` SHALL be, for every row written by this build, the first `KEYWORD_EXCERPT_CHARS` (20,000) characters of exactly the body text the row's successful keyword-vector attempt bound — the same prefix under the halving retreat — with U+0002 and U+0003 removed, written in the same UPDATE statement as the vector. A NULL excerpt SHALL mean the row's vector was last written by a build that did not store one.

#### Scenario: Aliases in every form are indexed the same way

- **WHEN** one note declares `aliases: [Alpha, 2026-01-01, [nested], {k: v}]`, another `aliases: "Alpha, 2026-01-01"`, and a third `alias: Alpha` with `aliases: 2026-01-01`
- **THEN** the first two SHALL derive `["Alpha", "2026-01-01"]` and the third `["2026-01-01", "Alpha"]` (`aliases` read before `alias`), from the parsed mapping and from the stored JSONB alike

#### Scenario: An oversized alias list cannot crowd the vector

- **WHEN** a note declares 10,000 aliases
- **THEN** the aliases text bound to the vector SHALL be at most 2,048 characters and the note's keyword vector SHALL still be written

#### Scenario: The excerpt is the indexed prefix

- **WHEN** a note's full-content attempt fails and the retreat succeeds at a shorter prefix
- **THEN** the stored excerpt SHALL be the first 20,000 characters of that successful prefix, and a note whose body is shorter than 20,000 characters SHALL store it whole

## MODIFIED Requirements

### Requirement: Keyword indexing attempts full content and degrades per-note without aborting the pass

The tsvector build (incremental pass and full rebuild alike) SHALL attempt the note's full content, binding the row's title and the note's aliases (the shared rules) as the vector's weight-`A` head and the body as its weight-`D` content through the one builder in `src/services/fts.py`, and SHALL write `body_excerpt` in the same statement. Each attempt SHALL run inside its own savepoint, entered such that a database error unwinds the savepoint through the context manager's rollback before any retry (the failure handling sits outside the savepoint context, so the outer transaction is never left in the aborted state). On failure the build SHALL retreat by halving the **content** bind only — the title and aliases binds are bounded and never halved — one fresh savepoint per attempt, down to a floor of exactly 100,000 characters, rebinding the excerpt to the prefix of each attempt. A failure at the floor SHALL propagate out of the build, and the two call sites SHALL provide different — but individually stated — guarantees: in the **incremental pass** a floor failure whose SQLSTATE marks it a poison failure SHALL quarantine that note and the pass SHALL be re-run without it, committing every other note (see "A note that fails at a per-note database boundary SHALL be quarantined and removed from the index"), while any other floor failure aborts the pass with nothing committed; the **full rebuild** SHALL be atomic — no intermediate commits — so a floor failure rolls the entire rebuild back and the error surfaces to the operator who invoked it, never leaving a keyword index half-built under two FTS configurations that no periodic pass would repair.

Verification of the savepoint behavior SHALL include a real-PostgreSQL integration test (mocks cannot prove the driver's aborted-transaction state clears): induce a genuine statement failure, observe the bounded retry succeed within the same outer transaction, perform a further update, commit, and verify both rows.

**The full rebuild SHALL update only rows it certifies.** It snapshots the table once and then reads the vault note by note, so both the rows and the files move underneath it — and a keyword vector's body is only ever rewritten again when a note's `content_hash` changes, because both move paths preserve the body part of `content_tsvector` and the ordinary scan skips a row whose hash is unchanged. A row the rebuild steps over, or writes the wrong bytes into, therefore stays on the previous configuration with nothing that would ever revisit it.

The rebuild's snapshot SHALL therefore retain each row's owner, relative path and content hash; the bytes it reads SHALL be verified to hash to that retained content hash before anything is written; its title and aliases SHALL be derived from the frontmatter of those verified bytes; and its UPDATE SHALL be conditional on all four of id, owner, relative path and content hash, and SHALL require that exactly one row matched. A zero-row update, a read failure, or a hash mismatch SHALL NOT be committed around and SHALL NOT be routed through the size-halving retreat, which addresses a size failure and cannot fix a stale target. Each SHALL instead trigger a bounded re-read of the current owner-scoped row: a row that is gone is safely absent and SHALL be skipped; a row whose path or hash has changed SHALL be retried against those fresh values within a bounded number of attempts; and a row that still records the path and hash the rebuild acted on — an unreadable file, or bytes the scan has not caught up with — SHALL abort the whole rebuild, which being one transaction rolls every other note back with it.

#### Scenario: A note moved mid-rebuild is repaired, never stepped over

- **WHEN** a note's `file_path` changes (by either move path) after the rebuild's snapshot and before it reads that row, so the read at the snapshotted path fails
- **THEN** the rebuild SHALL re-read the row, retry against its current path, and write that row's tsvector under the current configuration
- **AND** it SHALL NOT commit the remaining notes while leaving that row on the previous configuration

#### Scenario: A stale write does not land when the content hash advances

- **WHEN** a concurrent index pass commits a new `content_hash` and a matching `content_tsvector` for a row between the rebuild's read of the earlier content and its UPDATE
- **THEN** the certified UPDATE SHALL match no row and the earlier content's tsvector SHALL NOT be written
- **AND** the rebuild SHALL re-read the row and rebuild it against the committed content, so the stored hash and the stored tsvector describe the same content

#### Scenario: A row it cannot certify aborts the whole rebuild

- **WHEN** the rebuild cannot read a note, or the bytes it reads do not hash to the row's `content_hash`, and a re-read shows the row still records that path and that hash
- **THEN** the rebuild SHALL abort and its transaction SHALL roll back, leaving every note's `content_tsvector` unchanged
- **AND** the error SHALL surface to the operator who invoked it, rather than the rebuild committing around that row

#### Scenario: A row deleted mid-rebuild is safely absent

- **WHEN** the row is deleted (or leaves the rebuild's owner scope) between the snapshot and the write
- **THEN** the rebuild SHALL skip it without aborting, because no row remains in scope to leave on the previous configuration

#### Scenario: Terms beyond the former 100K slice are searchable when the full build succeeds

- **WHEN** a valid note carries a distinctive term past 100,000 characters and its full-content tsvector build succeeds
- **THEN** after the next index of that note, `keyword_search` for that term SHALL return the note

#### Scenario: A pathological note degrades alone, and the degradation is bounded and logged

- **WHEN** a note's full-content tsvector exceeds PostgreSQL's size limit
- **THEN** the pass SHALL retreat to a bounded prefix for that note only, log the retreat with the prefix length, index the remaining notes normally, and commit
- **AND** terms present only beyond the successful prefix are accepted as unsearchable for that note — the declared degradation
- **AND** the note's title and aliases SHALL remain fully indexed at weight `A` at every retreat step

#### Scenario: A poison floor failure in the incremental pass costs only that note

- **WHEN** even the 100,000-character floor attempt fails for a note during an incremental index pass with a data-exception (class 22) or program-limit (54000) SQLSTATE
- **THEN** that note SHALL be quarantined, and any existing row at its path SHALL be removed together with its embeddings and outgoing links, so no hash is committed beside derived data from other content
- **AND** the pass SHALL be re-run without that note and every other note in the scope SHALL be committed

#### Scenario: A non-poison floor failure still aborts the incremental pass

- **WHEN** the floor attempt fails for a reason outside the poison SQLSTATE set (for example a connection loss or lock timeout)
- **THEN** the error SHALL propagate and the pass SHALL abort with nothing committed, and the failure SHALL count toward the scope's consecutive failures

#### Scenario: A floor failure during a full rebuild rolls the whole rebuild back

- **WHEN** `rebuild_tsvectors` is processing a vault of more than 500 notes and a note's floor attempt fails after what would previously have been an intermediate commit boundary
- **THEN** the entire rebuild SHALL roll back — no note's tsvector changes — and the error SHALL surface to the invoking operator
- **AND** the keyword index SHALL never be left half-built under two FTS configurations

#### Scenario: The rebuild stores the excerpt and the head

- **WHEN** `make rebuild-tsvectors` completes over a scope whose rows were indexed before migration 031
- **THEN** every rebuilt row SHALL carry a non-NULL `body_excerpt` and a vector whose weight-`A` part is its title and aliases

### Requirement: A move recomputes the title through the shared derivation

Both move paths SHALL leave `notes_metadata.title` exactly what a fresh index of the file at its new path would produce, computed by the one shared title-derivation helper (sanitize the frontmatter `title`, stringify non-strings, fall back to the filename stem for falsy values, bound to the column width), and SHALL in the same statement replace the keyword vector's weight-`A` head with that title and the note's aliases — `head(title, aliases) || ts_filter(content_tsvector, '{d}')` through the shared `rehead` builder — while preserving `body_excerpt` and the body's lexemes, weights and relative positions (absolute positions MAY shift by the previous head's length; matching, phrase search and ranking are invariant under that shift). The indexer's id-preserving move detection SHALL take the title and aliases from the entry it parsed for the new path. `move_note` SHALL read the moved note through the destination's validated descriptor after the move stands, parse its frontmatter with the same parser the indexer uses, and derive the title and aliases from that; only when that read or parse fails MAY it fall back to deriving both from the row's stored sanitized frontmatter, which is best-effort and self-heals at the next content change. Both move paths are keyword-vector writers and SHALL behave as every other one does: the write SHALL happen in a transaction that acquired the index generation lock before any row or table lock and re-read the keyword fingerprint after acquiring it, and SHALL NOT write under a fingerprint that differs from the running configuration. For `move_note` that means the generation lock and the fingerprint check are the metadata transaction's first statements; a lock wait past the statement timeout or a fingerprint mismatch aborts that transaction, the move on disk stands and is reported as a partial outcome, and the next pass's move detection repairs path, title and head.

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
