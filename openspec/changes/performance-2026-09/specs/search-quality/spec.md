## ADDED Requirements

### Requirement: Read paths SHALL select only the columns they render
`semantic_search`, `keyword_search`, `list_notes`, `get_recent`, `find_orphans` and `get_neighborhood`'s metadata hydration SHALL each select an explicit list of columns, and that list SHALL contain neither `notes_metadata.content_tsvector` nor `note_embeddings.embedding`. `content_tsvector` SHALL be mapped as a deferred attribute that raises when read from a loaded entity, so that no whole-entity load ships it and an accidental reader fails loudly instead of lazy-loading.

The change SHALL NOT alter any predicate, `SET LOCAL`, overfetch, or exact-fallback condition. It SHALL NOT alter any rendered field, including the staleness fields (`content_hash`, `embedded_content_hash`) and `chunks_truncated`, which SHALL remain selected wherever they are rendered today.

`list_notes` and `get_recent` SHALL order by `modified_at DESC, file_path ASC`. `find_orphans` SHALL keep its existing `modified_at DESC NULLS LAST` and add `file_path ASC` after it. Rows with exactly equal `modified_at` therefore have a deterministic order.

Results SHALL be identical to the pre-change implementation (the same result set, the same order, and every non-similarity field byte-equal) with exactly two permitted exceptions, both confined to exact ties:
- **membership at a tied cutoff**: when more rows share the boundary sort key (`modified_at`, `rank` or distance) than fit under the limit, which of them are returned MAY differ;
- **the representative chunk among exact distance ties**: when two chunks of one note have exactly equal distance, the kept `chunk_index` and its preview MAY differ.

No other difference is permitted.

#### Scenario: No detoasted column is selected
- **WHEN** each of the six statements is compiled
- **THEN** its SELECT list SHALL NOT contain `content_tsvector` or `embedding`

#### Scenario: A deferred column read raises
- **WHEN** code loads a `NoteMetadata` entity and reads `content_tsvector`
- **THEN** the access SHALL raise rather than issue a load

#### Scenario: Results match the previous implementation
- **WHEN** the same fixed corpus and query set are run through the previous and the new implementation, covering stale, truncated, filtered, unfiltered and exact-fallback cases
- **THEN** the result sets SHALL be equal, the order SHALL be equal, and every field other than `similarity` SHALL be byte-equal, except for the two permitted tie cases

#### Scenario: The permitted tie differences are exercised
- **WHEN** the corpus contains more notes with an identical `modified_at` than the requested limit, and one note with two chunks at exactly equal distance
- **THEN** the oracle SHALL accept a different membership at that cutoff and a different representative chunk for that note, and SHALL reject any other difference

#### Scenario: Orphans with no modification time stay last
- **WHEN** `find_orphans` returns notes some of which have a NULL `modified_at`
- **THEN** those notes SHALL be ordered after every note with a non-NULL `modified_at`, as before

### Requirement: Vector similarity SHALL be derived from the database's distance
`semantic_search` SHALL report `similarity = 1 − distance`, where the distance is the full-precision cosine distance the database returned for that row, as `find_related` already does. It SHALL NOT fetch stored vectors to recompute similarity. Its in-service re-sort SHALL key on `(distance, file_path, chunk_index)`, so the presented order is monotone in distance and deterministic among exact ties. The reported similarity SHALL differ from the previous NumPy recomputation by no more than 1e-5.

#### Scenario: Similarity is monotone in the presented order
- **WHEN** `semantic_search` returns results
- **THEN** their `similarity` values SHALL be non-increasing in the order presented

#### Scenario: No vector is fetched
- **WHEN** `semantic_search` executes
- **THEN** no statement it issues SHALL select `note_embeddings.embedding` as an output column

### Requirement: A reduced-precision vector index SHALL ship only if it meets the recall SLO, and SHALL change only candidate generation
The `note_embeddings` HNSW index SHALL be replaced by an expression index over `(embedding::halfvec(D)) halfvec_cosine_ops`, where *D* is the configured `EMBEDDING_DIMENSIONS`, **only if** the filtered-recall benchmark meets its SLO with the replacement in place, and SHALL otherwise be left as it is. The benchmark's exact baseline SHALL remain a full-precision `vector` sequential scan. The SLO is set-recall ≥ 0.9 on each of three index rebuilds, for every filter shape and for `find_related`. If the SLO is not met, the index SHALL NOT ship and the measured recall SHALL be recorded in the architecture note.

If the index ships:
- It SHALL be built only when *D* ≤ 2000. That is the condition under which an HNSW index exists today, so no deployment changes from an exact to an approximate scan.
- `semantic_search` and `find_related` SHALL order their index scan by the exact index expression.
- They SHALL also select the full-precision distance, re-sort the fetched candidates by it before per-note dedupe, and report similarity from it. So the reduced precision affects which candidates are fetched and nothing else.
- The pre-warm probe SHALL order by the same expression, so that it warms the index the search uses.
- The reset-embeddings paths SHALL drop and re-create it through the same definition.
- The index name, the build condition, the build statement and the query expression SHALL be defined in one module that all of these call.

#### Scenario: The gate decides
- **WHEN** the recall benchmark runs with the expression index and a full-precision exact baseline
- **THEN** the index SHALL ship only if recall ≥ 0.9 holds on each of three rebuilds for every filter shape and for `find_related`

#### Scenario: The index is used
- **WHEN** the index has shipped and a filtered `semantic_search` or `find_related` statement, or the pre-warm probe, is explained
- **THEN** the plan SHALL use the expression index

#### Scenario: Ranking among candidates is full precision
- **WHEN** two fetched candidates are ordered differently by the reduced-precision and the full-precision distance
- **THEN** the presented order SHALL follow the full-precision distance

#### Scenario: Above the HNSW dimension limit nothing changes
- **WHEN** `EMBEDDING_DIMENSIONS` is greater than 2000
- **THEN** no vector index SHALL be built, the queries SHALL use the plain `vector` distance, and the pre-warm probe SHALL be skipped as before
