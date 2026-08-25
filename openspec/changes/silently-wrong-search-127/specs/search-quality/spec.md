## ADDED Requirements

### Requirement: The owner predicate makes every vector query a filtered query

Because read-path owner scoping is total, `semantic_search` and `find_related` SHALL treat the owner predicate as a filter for exact-fallback eligibility: whenever the approximate vector query returns zero rows — under any combination of `folder`, `tags`, `frontmatter`, and owner scope, an ownerless (`user_id IS NULL`) scope included — the service SHALL re-run the identical statement as an exact sequential scan and return its results, recording `exact_fallback: true` in the usage log. Within those two tools there SHALL be no code path on which a vector query runs unfiltered or on which a zero-row approximate result is returned without the exact re-run. (The indexer's internal pre-warm probe issues an unfiltered nearest-neighbor statement by design; it returns nothing to any caller and is out of this requirement's scope.)

#### Scenario: Ownerless zero-row result on a mixed database falls back

- **WHEN** the database holds many named-user vectors and at least one matching NULL-owned vector, and an ownerless `semantic_search` HNSW query returns zero rows after the owner predicate discards every candidate
- **THEN** the service SHALL run the exact filtered scan and return the matching NULL-owned notes, and the usage log SHALL record `exact_fallback: true`
