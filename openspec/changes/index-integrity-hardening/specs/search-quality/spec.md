## ADDED Requirements

### Requirement: A stale vector result is annotated and never filtered out
`semantic_search` and `find_related` SHALL mark every returned note whose stored vectors predate its indexed content — `embedded_content_hash IS DISTINCT FROM content_hash` — as stale, and SHALL continue to return it. Neither tool SHALL add a staleness predicate to its vector query, and neither SHALL drop a row on account of staleness.

The comparison SHALL be `IS DISTINCT FROM`, so a note that has never been embedded, or whose certification was cleared by a move, is stale rather than falling through a NULL comparison as fresh.

**Filtering is refused, and the refusal is the requirement.** A hash-equality filter would remove every note edited since the last completed embed pass — a window of minutes in normal operation and the whole vault during a provider outage — and, because every vector query in these two tools is a filtered query whose zero-row result triggers an exact sequential re-run, it would convert an outage into an O(n) scan of the embedding table on every search. A slightly stale hit that says so is better for an agent than a missing note.

Each tool's result SHALL carry a count of stale rows **whenever it returns any rows at all, including when that count is zero**, so that a caller can distinguish "nothing here is stale" from a build that does not report staleness. Per-row marking SHALL identify which rows are stale.

`find_related` SHALL additionally state, once, when the **source** note is itself stale, because in that case the averaged query vector describes the source's previous content and every neighbour answers a superseded question — a fact no per-row marker can express. It SHALL state it on **every return path on which the source row was loaded, the empty one included**. "No related notes for this note" from a stale source is the reading a consumer acts on — that the note has no neighbours — when the truth is that the vector searched with describes content the note no longer has, so the empty result is where the statement matters most. The distinct "the source has not been embedded yet" refusal keeps its own message: a source with no vectors at all is a different fact with a different fix.

**The guarantee is scoped to what the index has committed, and the residual is declared rather than closed.** Staleness is derived from the metadata row, so a note reads as stale only once the scan has committed its new content hash. Between an edit landing on disk and the next scan reaching that note — bounded by the index interval plus the pass in flight — the row's two hashes still agree while the stored chunk text is already superseded, and the result is presented as fresh. Closing that would require hashing the file on disk for every returned row, putting a filesystem read on the hot path of every search and still racing the writer. The system SHALL therefore state the guarantee as *"no result presents text the index knows to be superseded"*, SHALL document the residual window where the tools are described, and SHALL NOT restate it as a stronger claim.

#### Scenario: A stale note is still returned, and is marked

- **WHEN** a note's content changes and a `semantic_search` whose nearest chunk belongs to that note runs before the next embed pass completes
- **THEN** the note SHALL appear in the results at the rank its stored vector earns
- **AND** it SHALL be marked stale, and the result SHALL report at least one stale row

#### Scenario: A provider outage does not empty the results

- **WHEN** the embedding provider has been unavailable long enough that every note in the vault has been edited since it was last embedded
- **THEN** `semantic_search` SHALL return its usual number of results
- **AND** every one of them SHALL be marked stale
- **AND** no exact sequential fallback SHALL be triggered by staleness

#### Scenario: The stale count is present when nothing is stale

- **WHEN** every returned note's stored vectors match its indexed content
- **THEN** the result SHALL still report its stale count, as zero

#### Scenario: A stale source is stated once

- **WHEN** `find_related` is called on a note whose own `embedded_content_hash` differs from its `content_hash`
- **THEN** the result SHALL state that the source note changed after it was embedded and that the neighbours were computed from its previous content
- **AND** the neighbours SHALL still be returned

#### Scenario: A stale source is stated on the empty result too

- **WHEN** `find_related` is called on a stale source that has vectors and the query returns no neighbour after the exact fallback
- **THEN** the result SHALL still state that the source changed after it was embedded
- **AND** it SHALL NOT present the empty result as a bare statement that the note has no related notes

#### Scenario: An unembedded source keeps its own refusal

- **WHEN** `find_related` is called on a note that has no vectors at all
- **THEN** the existing "not embedded yet" message and its usage marker SHALL be returned unchanged
- **AND** it SHALL NOT be replaced by the stale-source statement

#### Scenario: An edit the scan has not yet seen is not marked, and that bound is declared

- **WHEN** a note is edited on disk and a vector search returns it before the next index pass has committed its new content hash
- **THEN** the row SHALL NOT be marked stale, because the index does not yet know the note changed
- **AND** this SHALL be documented as the declared bound of the staleness signal rather than described as a case the signal covers

#### Scenario: No query predicate changes

- **WHEN** either tool executes its vector query
- **THEN** the statement SHALL carry the same owner, folder, tag and frontmatter predicates, the same overfetch, the same `SET LOCAL` settings and the same zero-row exact-fallback eligibility as before
- **AND** the staleness columns SHALL be read from the joined metadata row rather than filtered on

### Requirement: A stale row's chunk preview is withheld rather than shown
Where a vector-search row is stale, the tool SHALL withhold that row's chunk preview and SHALL replace it with an explicit notice naming `read_note` as the way to obtain the note's current text. Every other field of the row — path, title, tags, and the similarity or distance — SHALL be returned unchanged.

The distinction is what each field is. Path, title and tags are read from the metadata row, which the scan refreshed at the moment it committed the new content hash, so they describe the note as it stands. The similarity is a retrieval score, not an assertion about content. The chunk preview is the only field that is a **verbatim quotation of the note's text**, it is the only field that is out of date, and it is the field a consumer will reproduce in an answer. Withholding it turns a silently wrong result into a visibly degraded one whose remedy is one call away.

The tool SHALL NOT substitute the note's current leading text for the withheld preview: that text is a different span from the one that matched, presented where the matching span belongs, and it would read as an excerpt the search actually found.

#### Scenario: The preview is withheld and the row survives

- **WHEN** a stale note is returned by `semantic_search`
- **THEN** its chunk preview SHALL be absent from the result
- **AND** a notice in its place SHALL say that the note changed after it was embedded and that `read_note` returns the current content
- **AND** its path, title, tags and similarity SHALL be present and unchanged

#### Scenario: A fresh row keeps its preview

- **WHEN** a returned note's stored vectors match its indexed content
- **THEN** its chunk preview SHALL be returned exactly as before this change

#### Scenario: The substitute is not the note's current text

- **WHEN** a stale row's preview is withheld
- **THEN** the replacement SHALL NOT contain any text read from the note's current bytes

#### Scenario: Both vector tools behave the same way

- **WHEN** the same stale note is returned by `semantic_search` and by `find_related`
- **THEN** both SHALL withhold the preview and both SHALL mark the row stale

### Requirement: A note whose chunking was capped is marked in vector results
`semantic_search` and `find_related` SHALL mark a returned note whose embedding was truncated at the per-note chunk cap, so that a match drawn from the note's head is not read as a match against the whole note.

The marker SHALL be read from the note's durable `chunks_truncated` column and SHALL NOT be inferred from the number of chunk rows: a capped note holds exactly the cap and is indistinguishable by row count from a note that legitimately produces that many.

#### Scenario: A capped note's result says so

- **WHEN** a note whose `chunks_truncated` is true is returned by either vector tool
- **THEN** its row SHALL be marked as having a truncated embedding
- **AND** the marking SHALL state that the note's tail was not embedded and is therefore not reachable by semantic search

#### Scenario: An uncapped note is not marked

- **WHEN** a note whose `chunks_truncated` is false is returned
- **THEN** no truncation marking SHALL appear on its row
