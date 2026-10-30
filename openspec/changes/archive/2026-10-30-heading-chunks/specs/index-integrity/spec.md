## ADDED Requirements

### Requirement: The indexer SHALL chunk a note at its ATX headings using the section-addressing scanner, so a chunk's ordinal is the one `read_note` resolves
`embed_note` SHALL split the note body it embeds into sections using the same heading scanner that `read_note(section=…)` and `edit_note(section=…)` resolve selectors with. It SHALL scan the same frontmatter-stripped body, so a line inside fenced or inline code is never a boundary. Section *k* (k ≥ 1) SHALL be the text of the *k*-th heading in document order, from the line after the heading line up to the next heading of any depth or the end of the body. Section 0 SHALL be the text before the first heading, or the whole body when there is none. Each section SHALL be cleaned by removing the fence spans the shared recognizer finds over the **whole** body, and then windowed by the existing bounded chunker at `CHUNK_SIZE` with the configured overlap. Heading lines SHALL NOT be part of any chunk's stored text.

Each chunk row SHALL record its section's ordinal (`section_ordinal`, 0 for section 0) and its heading path (`heading_path`: the enclosing headings outermost first, ending with the section's own heading, each component trimmed and cut at 200 characters with a marked cut; the empty array for section 0). Every chunk of a section longer than one window SHALL carry the same ordinal and heading path, in consecutive `chunk_index` order. A section whose cleaned text is whitespace-only SHALL yield no chunk.

A note with no headings SHALL yield chunk texts byte-identical to the pre-change chunker's output for the same body. If every section yields no chunk while the pre-change chunker would yield at least one for the cleaned body, the note SHALL fall back to that whole-body chunking as section 0, so a note yields zero chunks exactly when the pre-change chunker yields zero. The per-note chunk cap SHALL count across all sections in document order, and the truncation marker SHALL keep its meaning.

#### Scenario: A heading inside a code fence is not a boundary
- **WHEN** a note contains a fenced block whose content includes a line `# Not a heading`, inside a section headed `## Real`
- **THEN** no chunk boundary SHALL fall at that line, and every chunk of that section SHALL record the ordinal of `## Real`

#### Scenario: Ordinal parity with section addressing
- **WHEN** any note, with LF, CRLF or lone-CR terminators, with or without a valid or defective frontmatter block, is chunked, and a chunk records ordinal N ≥ 1
- **THEN** `read_note(path, section="#N")` SHALL resolve, and the chunk's text SHALL be a substring of that section's body with the whole-body fence spans removed

#### Scenario: Text before the first heading
- **WHEN** a note has prose above its first heading
- **THEN** that prose SHALL be chunked as section 0 with an empty heading path

#### Scenario: A parent with only subsections
- **WHEN** a `## Parent` heading is followed directly by `### Child` with body text
- **THEN** no chunk SHALL record `## Parent`'s ordinal, and the child's chunks SHALL record the path `["Parent", "Child"]`

#### Scenario: A long section
- **WHEN** a single section's cleaned text spans three windows
- **THEN** three chunks SHALL be stored, with the same ordinal and heading path and consecutive chunk indexes

#### Scenario: No headings
- **WHEN** a note has no ATX heading
- **THEN** its chunk texts SHALL equal the pre-change chunker's, all with ordinal 0

#### Scenario: A note of headings only
- **WHEN** a note consists only of heading lines
- **THEN** it SHALL be chunked by the whole-body fallback as section 0, and SHALL NOT be certified as empty

#### Scenario: The cap counts across sections
- **WHEN** a note's sections together yield more than `MAX_CHUNKS_PER_NOTE` chunks
- **THEN** the first `MAX_CHUNKS_PER_NOTE` in document order SHALL be stored and the note SHALL be marked truncated

### Requirement: The text sent to the provider for a chunk SHALL be a bounded title-and-heading prefix followed by the chunk, and the stored chunk text SHALL stay un-prefixed
For every chunk, the input `embed_note` sends to the provider SHALL be `embed_prefix + "\n\n" + chunk_text`, built by one function. `embed_prefix` SHALL be the note's title (the shared title rule) followed by ` > ` and each heading-path component, each component whitespace-collapsed and cut at 80 characters with a marked cut, then, only when the whole prefix including it is at most 300 characters, a line `tags: ` followed by the note's tags, comma-separated, in stored order. When the breadcrumb alone exceeds 300 characters, the outermost headings after the title SHALL be replaced by one `…` component until it fits. The title and the innermost heading SHALL always be kept. The row SHALL store `embed_prefix` and the un-prefixed `chunk_text`. Previews SHALL be drawn from `chunk_text` only.

#### Scenario: Prefix shape
- **WHEN** a chunk of section `## Plan` under `# Project` in a note titled `Alpha` tagged `work` is embedded
- **THEN** the provider input SHALL be `Alpha > Project > Plan\ntags: work\n\n` followed by the chunk text, and the stored `chunk_text` SHALL NOT contain the prefix

#### Scenario: Tags that do not fit are omitted entirely
- **WHEN** adding the tags line would make the prefix longer than 300 characters
- **THEN** the prefix SHALL contain no tags line

#### Scenario: A deep, long breadcrumb is cut from the outside
- **WHEN** a six-level heading path's breadcrumb exceeds 300 characters
- **THEN** the prefix SHALL keep the title and the innermost heading, SHALL replace outer headings with `…`, and SHALL be at most 300 characters

### Requirement: Notes embedded under an older chunking version SHALL be re-selected per note, held to the embed budget in every mode, and the transition SHALL be declared on every vector-search response
`certify_embedded` SHALL stamp `notes_metadata.embedded_chunking_version = CHUNKING_VERSION` in the same conditional UPDATE that stamps `embedded_content_hash`, on every certifying branch (embedded, certified-empty, excluded). The embed backlog SHALL select a note whose `embedded_content_hash` is not current **or** whose `embedded_chunking_version IS DISTINCT FROM CHUNKING_VERSION`, through one predicate shared with the dashboard pending count and the re-embedding progress endpoint. It SHALL take content-stale notes first and version-only notes after them, each by `modified_at` descending. The per-tenant embed budget SHALL apply to the version-only notes whatever the number of active scopes, with the existing note-boundary and at-least-one-note rules. A budget stop SHALL remain a non-failure.

The staleness annotation SHALL remain `embedded_content_hash IS DISTINCT FROM content_hash`, so a note selected only for its chunking version SHALL be reported fresh and keep its preview. Its stored vectors SHALL be served until its new rows commit, which replace all of them in one transaction. A version change SHALL NOT clear `embedded_content_hash`, SHALL NOT alter the stored embedding fingerprint and SHALL NOT refuse startup.

**Mixed generations change ranking, and therefore membership at the cutoff.** A prefixed vector and a legacy vector of the same model sit at different distances from the same query. A re-embedded note can therefore displace a still-legacy note from a bounded page, so such a page is not a complete answer and the system SHALL NOT present it as one. While any note in the caller's owner scope **holds vector rows** and has `embedded_chunking_version IS DISTINCT FROM CHUNKING_VERSION` (the running build's version, so a NULL, lower **or higher** stamp counts), every `semantic_search` and `find_related` response SHALL carry this line on its own, directly after the header: `index_transition: N notes not yet re-chunked; cross-generation ranking is approximate, re-run after the backlog drains`.
- N SHALL be the owner-scoped count of such notes, read in the same `REPEATABLE READ, READ ONLY` transaction as the vector query.
- A note with no vector rows SHALL NOT be counted: it is not a generation mix, and counting it would raise the line for every note created since the last embed pass.
- Caller-supplied text echoed in the header (the query, the source path) SHALL be rendered as one escaped line, so it can neither displace the line nor forge one above it.
- The line SHALL be present on every return path that runs the query, the empty result included.
- The line SHALL be absent when N is zero.

The ranking itself remains mixed during the transition. That is a declared limitation.

**Two predicates, each expressed once.** The embed backlog, the dashboard pending count and the re-embedding progress endpoint SHALL use the owner-scoped predicate `embedded_chunking_version IS DISTINCT FROM :CHUNKING_VERSION`. The response marker and the `/health` state SHALL use the narrower `EXISTS (SELECT 1 FROM note_embeddings e WHERE e.note_id = n.id) AND n.embedded_chunking_version IS DISTINCT FROM :CHUNKING_VERSION` — notes that hold vectors of another generation, the definition the evaluation harness's `legacy_chunked_notes` uses. Each SHALL be expressed once, as paired SQL text and expression. None of them SHALL compare with `<` or `>`.

The length of the re-embed SHALL NOT by itself increment any indexer health counter or degrade `/health`. `/health` is unauthenticated, so it SHALL NOT carry any count derived from the transition. It SHALL carry only `indexer.rechunk`, a process-wide state of `"in_progress"` or `"complete"`, held in process memory and set by the embed stages. It reads `"in_progress"` while the most recent embed stage of any scope observed a non-zero count under the marker's predicate, and `"complete"` otherwise. It SHALL NOT affect `status` or the HTTP status code, and it SHALL NOT name a scope, a path or a number. The exact per-owner counts SHALL be available only on the authenticated surfaces: the dashboard pending count, the progress endpoint, and the owner-scoped `index_transition:` line.

#### Scenario: First passes after deploy
- **WHEN** the 034 build starts over a fully embedded pre-034 index
- **THEN** startup SHALL proceed and every note SHALL be in the backlog
- **AND** every note SHALL remain reachable by `semantic_search` and SHALL NOT be marked stale
- **AND** every response SHALL carry the `index_transition:` line

#### Scenario: Membership at the cutoff across budgeted passes
- **WHEN** a search runs with `limit=1`, legacy note A was nearer the query than note B before deploy, B is re-embedded on the first budgeted pass and A on the second
- **THEN** after the first pass the response SHALL carry the `index_transition:` line, whichever note it returns
- **AND** after the first pass `/health` SHALL report `indexer.rechunk` `"in_progress"`, with `status` `ok` and no transition count anywhere in the body
- **AND** after the second pass the response SHALL carry no `index_transition:` line, and `indexer.rechunk` SHALL be `"complete"`

#### Scenario: A rollback to a lower version is still a transition
- **WHEN** a build with `CHUNKING_VERSION` 2 re-embeds notes B and C of a scope and then stops, and a Recreate rollback deploys the build with version 1 without a schema downgrade
- **THEN** the version-1 build's responses SHALL carry the `index_transition:` line, counting B and C
- **AND** the line SHALL remain until every note in the scope is stamped 1
- **AND** the embed backlog SHALL re-select B and C and re-embed them under version 1, with no reuse from their version-2 rows

#### Scenario: /health discloses no count
- **WHEN** any scope has notes pending re-chunking
- **THEN** the `/health` body SHALL contain `indexer.rechunk` `"in_progress"` and SHALL contain no number derived from the transition

#### Scenario: The marker is owner-scoped
- **WHEN** another tenant has legacy notes and the caller's scope has none
- **THEN** the caller's responses SHALL carry no `index_transition:` line

#### Scenario: A new note without vectors raises no marker
- **WHEN** a note is created after the deploy and the embed pass has not reached it yet
- **THEN** it SHALL be in the embed backlog and the dashboard pending count
- **AND** no response SHALL carry the `index_transition:` line on its account

#### Scenario: A legacy note whose certification was cleared is still counted
- **WHEN** a note still serving pre-034 vectors has its certification cleared by a tag edit
- **THEN** the marker SHALL still count it until its new rows commit

#### Scenario: A multiline query cannot displace or forge the marker
- **WHEN** a query contains line breaks, control characters or the text `index_transition:` and the scope has notes on another generation
- **THEN** the header SHALL render the query on one escaped line, and the only `index_transition:` line SHALL be the one directly after it

#### Scenario: Single-user transition is spread over passes
- **WHEN** a single-user deployment's backlog holds only version-only notes whose embedding exceeds the time budget
- **THEN** the pass SHALL stop at a note boundary once the budget is spent, log one WARNING, record no run error, and continue on the next pass

#### Scenario: A real edit goes first
- **WHEN** a note is edited while thousands of version-only notes are pending
- **THEN** the next pass SHALL embed the edited note before any version-only note

#### Scenario: Health stays ok through a long re-embed
- **WHEN** the transition takes many passes and the provider answers every call
- **THEN** `/health` SHALL report `ok`

#### Scenario: Excluded and empty notes converge without a provider call
- **WHEN** a pre-034 note is excluded or cleans to zero chunks
- **THEN** it SHALL be stamped with the current chunking version without a provider call, and SHALL NOT be selected again

### Requirement: The certification SHALL cover a digest of the exact provider input, and a change to any derived value in that input SHALL invalidate the note in the same transaction
`certify_embedded` SHALL stamp `notes_metadata.embedded_input_digest`, in the same conditional UPDATE as `embedded_content_hash`. The digest is the SHA-256 hex of the canonical encoding of the ordered list of provider inputs: `embedding_input(prefix, chunk)` for every chunk, and the empty list for a certified-empty or excluded note.

Some index-pass statements change a row's title or tags while leaving `content_hash` unchanged: a re-derive, an extraction-version re-upsert, and either move path. When the stored title or tags are distinct from the new ones, such a statement SHALL also set `embedded_content_hash = NULL`, in that same statement, so the backlog re-selects the note.

A change to the heading scan, or to any other part of sectioning, SHALL bump `CHUNKING_VERSION`.

As a backstop, every full-hash pass SHALL recompute, for each certified note it reads, the digest of the inputs the current code would send. In the pass transaction, it SHALL clear `embedded_content_hash` for every note whose chunking version is current and whose stored digest is NULL or differs. The comparison makes no provider call.

#### Scenario: A tag-grammar change with identical cleaned text
- **WHEN** an extraction-version bump changes a note's extracted tags while its cleaned-for-embedding text is identical under both versions
- **THEN** the re-deriving pass SHALL clear the note's `embedded_content_hash` in the same transaction
- **AND** the next embed pass SHALL re-embed the note with the new tags line

#### Scenario: Unchanged inputs are not invalidated
- **WHEN** a re-derive rewrites a row whose title and tags are unchanged
- **THEN** `embedded_content_hash` SHALL be unchanged

#### Scenario: The full-hash backstop
- **WHEN** a certified note's stored input digest differs from the digest of its current provider inputs
- **THEN** the next full-hash pass SHALL clear its certification, and a later embed pass SHALL re-embed it

## MODIFIED Requirements

### Requirement: An unchanged chunk SHALL reuse its stored vector only when that vector provably belongs to the current generation
`embed_note` SHALL reuse a stored vector of the note for a chunk whose **embedded input** is byte-identical to the stored row's, that is, whose `(embed_prefix, chunk_text)` pair equals the stored row's pair, and send only the remaining chunks to the provider. It SHALL do so only if all of the following hold, and SHALL otherwise embed every chunk as before:
- **Lookup transaction.** The lookup of stored `(id, chunk_text, embed_prefix, embedding)` rows, of the note's `embedded_chunking_version` and of the stored embedding fingerprint runs in a read-only transaction that is committed before any provider call.
- **Fingerprint.** The stored embedding fingerprint is present and equal to the current one. An absent fingerprint SHALL disable reuse.
- **Chunking version.** The note's `embedded_chunking_version` equals `CHUNKING_VERSION`. Otherwise reuse SHALL be disabled for the whole note.
- **Prefix present.** A row whose `embed_prefix` is NULL SHALL NOT be reused.
- **Rows survive.** Under the generation lock, after the provider call and before certification, every reused row still exists for this note with the same `chunk_text` and `embed_prefix`. Otherwise the attempt SHALL certify nothing, write nothing and report `GENERATION_MISMATCH`.

A row's `heading_path` and `section_ordinal` SHALL NOT be part of the reuse key. Every re-inserted row SHALL carry the locator of the chunk it now stores.

The cardinality check SHALL apply to the chunks actually sent. The note SHALL be certified only when every requested chunk has a vector, whether reused or fresh. After certification, all of the note's rows SHALL be deleted and re-inserted in document order.

A note whose chunks are all reusable SHALL make no provider call. It SHALL NOT be counted as a provider attempt, even when the under-lock check fails and it reports `GENERATION_MISMATCH`, and it SHALL debit no chunk budget.

#### Scenario: An append re-embeds only the new tail
- **WHEN** a note's content is extended so that every earlier chunk's text, prefix and chunking version are unchanged
- **THEN** only the new chunks SHALL be sent to the provider and the note SHALL be certified with every chunk present

#### Scenario: No fingerprint, no reuse
- **WHEN** the stored embedding fingerprint is absent or differs from the current one
- **THEN** no stored vector SHALL be reused

#### Scenario: A legacy row is never reused
- **WHEN** a note's stored rows predate migration 034 (NULL `embed_prefix`), or its `embedded_chunking_version` is not `CHUNKING_VERSION`
- **THEN** every chunk SHALL be sent to the provider

#### Scenario: A changed prefix defeats reuse
- **WHEN** a note's tags or title change and its chunk texts do not
- **THEN** no stored vector SHALL be reused

#### Scenario: An inserted heading shifts ordinals without re-embedding
- **WHEN** a heading is inserted above existing sections so that their ordinals shift and their prefixes and texts do not change
- **THEN** their vectors SHALL be reused and the re-inserted rows SHALL carry the new ordinals

#### Scenario: A reset during the provider call defeats reuse
- **WHEN** a reset deletes the note's stored vectors while the provider call for the remaining chunks is in flight
- **THEN** the attempt SHALL certify nothing and write nothing, and SHALL not deadlock with the reset

#### Scenario: Nothing to send
- **WHEN** every chunk of a changed note has a stored row with the same prefix and text, under the current chunking version
- **THEN** no provider call SHALL be made, the note SHALL be certified, and the pass's attempt count and chunk budget SHALL be unchanged

#### Scenario: An all-reuse note loses a reset race without an attempt
- **WHEN** every chunk of a note is reusable and a reset deletes the stored rows between the lookup and the under-lock check
- **THEN** the outcome SHALL be `GENERATION_MISMATCH`, nothing SHALL be written, and the pass's attempt count SHALL be unchanged

### Requirement: A masker grammar change forces re-derivation without corrupting note identity

Link rows, inline tags, and embedded vectors are derived through the shared fence recognizer, but re-derivation is normally gated on `content_hash`, which does not change when only the grammar changes. A grammar change SHALL therefore ship with a versioned re-derivation mechanism: a per-note extraction-version marker, distinct from `content_hash`, compared against a code-level current version.

When a note's marker is stale, the next index pass SHALL re-extract its links and tags, and SHALL then stamp the marker.

Grammar-attributable embedding invalidation SHALL occur exactly when the note's **provider input differs** between the grammar version that stamped the note's recorded version and the current grammar. That is the case when either of these differs:
- the embedded text, that is, the cleaned-for-embedding output;
- **any derived value the embedding prefix carries (title, tags).**

Each version's cleaning function is retained, frozen, while any row remains stamped with it, so the comparison is direction-aware and works for rollbacks as well as upgrades.

The comparison is over cleaned output, not recognised-span tuples, because span equality is neither necessary nor sufficient for embedded-text equality. The v0 cleaner applied its two regexes sequentially, so adjacent mixed fences produce identical spans but different cleaned text, and vice versa.

Independent invalidators are cumulative, and the cleaned-output comparison SHALL NOT suppress an invalidation any other requirement mandates. The independent invalidators are:
- a content change;
- a `file_path` change;
- a changed title or tags;
- a provider or configuration change;
- exclusion reconciliation;
- a chunking-version change.

`content_hash` SHALL always hold the true hash of the note's bytes. It SHALL NOT be nulled or overwritten with a sentinel, so hash-based move/rename matching keeps working throughout the remediation window.

#### Scenario: The fence-grammar deploy refreshes links and tags

- **WHEN** this change's migration has run and the indexer completes its next pass
- **THEN** every note's `note_links` rows and extracted tags SHALL reflect the new grammar

#### Scenario: Embedding invalidation is scoped to affected notes

- **WHEN** the re-derivation pass processes a note whose cleaned-for-embedding output is identical under the stamped version's frozen cleaner and the current one, whose title and tags are unchanged, and to which no independent invalidator applies (no content change, no `file_path` change, no provider or configuration change, no exclusion reconciliation)
- **THEN** the grammar migration SHALL NOT cause that note to be re-embedded
- **AND** the cleaned-output comparison SHALL govern only invalidation attributable to the grammar change, and SHALL NOT suppress an invalidation another requirement mandates
- **WHEN** the pass processes a note whose cleaned output differs (for example, one containing an indented fence around text)
- **THEN** that note's embeddings SHALL be rebuilt from the newly cleaned text on a subsequent embed pass

#### Scenario: Identical cleaned text, different tags

- **WHEN** the re-derivation pass processes a note whose cleaned output is identical under both versions but whose extracted tags differ
- **THEN** that note's certification SHALL be cleared in the pass transaction
- **AND** its embeddings SHALL be rebuilt with the new prefix on a subsequent embed pass

#### Scenario: Move detection survives the remediation window

- **WHEN** a note is externally renamed after the migration runs but before the re-derivation pass reaches it
- **THEN** the indexer's content-hash move matching SHALL still identify it as the same note (its `content_hash` is real), preserving row identity rather than deleting and reinserting it
- **AND** the path-change invalidation the existing requirements mandate for a move SHALL still apply
- **AND** the ID-preserving move branch SHALL re-derive that note's links and tags under the current grammar and stamp its marker in the same transaction, so the note does not need a second pass to satisfy the next-pass refresh promise

#### Scenario: Rewrite-enabled moves refuse while re-derivation is incomplete

- **WHEN** any note in the caller's owner scope still carries a stale extraction marker and a client calls `move_note(from, to, rewrite_links=True)`
- **THEN** the move SHALL be refused before the rename, naming the in-progress re-derivation
- **AND** the reason is that rewrite-source discovery reads `note_links` rows that a stale-grammar extraction may have wrongly omitted: a link inside a span the old grammar masked but the new grammar reads as prose would otherwise be silently left broken
- **AND** the same move with `rewrite_links=False` SHALL be unaffected, and the refusal SHALL clear on its own once the re-derivation pass completes
