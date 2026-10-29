## ADDED Requirements

### Requirement: The keyword vector weights the title and aliases above the body, and ranking uses an explicit weight array

`notes_metadata.content_tsvector` SHALL be built, for every configured FTS config in order, as the title parts for every config, then the aliases parts for every config, then the content parts for every config, concatenated with `||`: each title part `setweight(to_tsvector(cfg, title), 'A')`, each aliases part `setweight(to_tsvector(cfg, aliases), 'A')`, and each content part the pre-change `to_tsvector(cfg, content)` (implicit weight `D`, byte-identical fragment). `title` SHALL be the value `notes_metadata.title` holds for the row — the shared title rule (sanitised frontmatter `title`, else the filename stem, bounded to the column width) — and `aliases` SHALL be the text the shared `note_aliases` rule derives from the note's frontmatter (see `index-integrity`). One builder in `src/services/fts.py` SHALL produce the fragment for every writer, and no writer SHALL compose the expression by hand.

`full_text_search` SHALL rank with `ts_rank_cd(RANK_WEIGHTS, content_tsvector, tsquery)` where `RANK_WEIGHTS` is the single constant `{0.02, 0.2, 0.4, 1.0}` (`D, C, B, A`), so that one cover at weight `A` outweighs fifty covers of the same shape at weight `D`, and SHALL use the same array in every ranking statement it issues. The relative order of two rows that match only in their bodies SHALL be the same as under PostgreSQL's default array.

#### Scenario: A note is found and ranked first by its own title

- **WHEN** a note is titled `Quarterly Review` and its body does not contain that phrase, a second note of about 3,000 words contains the adjacent phrase `quarterly review` thirty times in its body and is not titled with it, both are indexed, and `keyword_search("quarterly review")` runs
- **THEN** the titled note SHALL be the first result and the second note SHALL be present below it

#### Scenario: An alias finds the note

- **WHEN** a note's frontmatter declares `aliases: [Q3 retro, retrospective]` and no note body contains `retrospective`
- **THEN** `keyword_search("retrospective")` SHALL return that note, ranked by an `A` cover

#### Scenario: Body-only order is unchanged by the weight array

- **WHEN** two notes match a query only in their bodies and are ranked under `RANK_WEIGHTS` and under PostgreSQL's default array over the same vectors
- **THEN** their relative order SHALL be the same under both

#### Scenario: Every writer produces the same shape

- **WHEN** the incremental pass, the full rebuild and migration 031's re-head (over a row whose vector carries no weight-`A` lexeme) each write a vector for the same title, aliases and body
- **THEN** the three stored vectors SHALL be byte-equal

### Requirement: Keyword search runs the conjunction first and fills the page from a disjunction derived from PostgreSQL's parse

`full_text_search` SHALL first run the conjunctive statement — `websearch_to_tsquery` per configured config, OR'd across configs, exactly as before — and SHALL return its rows tagged `match: "all"`. When that statement returns fewer rows than `limit`, the service SHALL obtain the text form of each config's `websearch_to_tsquery` parse from PostgreSQL and SHALL parse that text, never the caller's string: a parse that contains no `|` and no parenthesis and splits on `&` into units each of which is a single negated lexeme or a phrase chain of lexemes joined by `<->`/`<N>` is a plain conjunction. For a plain conjunction with at least two positive units the service SHALL run a second statement whose tsquery is, per config, the positive units OR'd and every negated unit retained as an AND NOT, built from the verbatim lexeme tokens and cast with the `tsquery` input function (no re-stemming), OR'd across configs as the first statement's is; that statement SHALL exclude every row matching the conjunctive tsquery, SHALL apply the identical `folder`, `tags`, `frontmatter` and owner predicates, SHALL order by `rank DESC, file_path ASC` under the same `RANK_WEIGHTS`, SHALL be limited to `limit` minus the conjunctive row count, and its rows SHALL be appended after every conjunctive row tagged `match: "any"`. A parse that is not a plain conjunction, or has fewer than two positive units, SHALL produce no second statement. Every statement of one call SHALL run in the one transaction that issued `SET LOCAL random_page_cost = 1.1`. The service SHALL record `fallback` (`ran`, `skipped` or `not_needed`) and `any_rows` in the call's telemetry.

#### Scenario: A three-term query that over-specifies still finds the target

- **WHEN** a note contains the terms `alpha` and `beta` but not `gamma`, no note contains all three, and `keyword_search("alpha beta gamma", limit=10)` runs
- **THEN** the result SHALL contain that note tagged `match: "any"`, and no row SHALL be tagged `match: "all"`

#### Scenario: Conjunctive rows come first and are never displaced

- **WHEN** three notes match all terms of a query, seven more match some, and `limit` is 5
- **THEN** the result SHALL be exactly the three `all` rows in `rank DESC, file_path ASC` order followed by the two best `any` rows in that order, and no `any` row SHALL precede an `all` row

#### Scenario: A full conjunctive page runs no second statement

- **WHEN** the conjunctive statement returns `limit` rows
- **THEN** no disjunctive statement SHALL be issued and telemetry SHALL record `fallback: "not_needed"`

#### Scenario: The user's operators decide the shape

- **WHEN** the query contains `OR`, a negated quoted phrase (`-"a b"`), or exactly one positive term
- **THEN** no disjunctive statement SHALL be issued and telemetry SHALL record `fallback: "skipped"`

#### Scenario: A negation is honoured in the fallback and a phrase stays a phrase

- **WHEN** the query is `alpha "beta gamma" -delta`, no note matches the conjunction, one note contains `alpha` and `delta`, and one contains the adjacent phrase `beta gamma`
- **THEN** the `any` block SHALL contain the phrase note and SHALL NOT contain the `delta` note

#### Scenario: The disjunction is a superset of the conjunction

- **WHEN** a generated corpus of plain-conjunction queries is run against a seeded database
- **THEN** for every query every row matching the conjunctive tsquery SHALL match the derived disjunctive tsquery, and the derived text SHALL cast as a `tsquery` without error

#### Scenario: No caller text reaches a tsquery constructor

- **WHEN** the statements of one call are compiled
- **THEN** the caller's query string SHALL appear only as the bind of `websearch_to_tsquery`, and every disjunctive tsquery SHALL be built from PostgreSQL's parse output

#### Scenario: The disjunctive statement uses the keyword index

- **WHEN** `EXPLAIN (ANALYZE, BUFFERS)` is run for the disjunctive statement over the seeded keyword corpus with the planner setting applied
- **THEN** the plan SHALL use the tsvector GIN index (bitmap or index scan)

### Requirement: Each keyword result carries a snippet computed over the page only

Each row of `keyword_search` SHALL carry `snippet`: `ts_headline(cfg, body_excerpt, q, opts)` where `body_excerpt` is the stored excerpt (see `index-integrity`), `q` is the tsquery that matched the row's block, and `opts` is `MaxFragments=2, MaxWords=18, MinWords=6` with U+0002/U+0003 as `StartSel`/`StopSel` rendered as `«`/`»`. The headline SHALL be computed by a statement whose predicate is the page's row ids (`id = ANY(:ids)`), never over the candidate set, so its cost is a function of `limit` alone, and that statement's time SHALL be recorded as `snippet_ms`. The same statement SHALL project `matched_in` from `ts_filter(content_tsvector, '{a}') @@ q` (title or aliases) and `ts_filter(content_tsvector, '{d}') @@ q` (body), and `matched_terms` — the count of the plain conjunction's positive units present in the row's vector, over its unit count — when the parse was a plain conjunction of at most 32 positive units. A row whose headline contains no highlight SHALL instead render the excerpt's leading words labelled `head:`, taken from `ts_headline(cfg, body_excerpt, q, 'MaxFragments=0, MinWords=18, MaxWords=24')` evaluated only for such rows; a row whose excerpt is NULL SHALL render neither, and the header SHALL carry the count of such rows always, including zero, with a footer naming the rebuild command when the count is positive. The server SHALL NOT cut or mark inside a snippet, and the header SHALL carry the `all` and `any` counts always, including zero.

#### Scenario: A body match is shown highlighted

- **WHEN** a note's body contains `the budget forecast for Q3` within the stored excerpt and `keyword_search("budget forecast")` returns it
- **THEN** its `snippet` SHALL contain `«budget» «forecast»` and `matched_in` SHALL include `body`

#### Scenario: A title-only match shows the head, not a fabricated span

- **WHEN** a note matches only through its title or aliases
- **THEN** the row SHALL render `head:` with the excerpt's leading words, `matched_in` SHALL be `title` alone, and no highlight marker SHALL appear

#### Scenario: A match beyond the excerpt shows the head

- **WHEN** a note's only occurrence of the query term lies past the first 20,000 characters of its indexed text
- **THEN** the row SHALL render `head:` and `matched_in` SHALL include `body`

#### Scenario: The snippet cost is bounded by the page

- **WHEN** a query matches 2,000 notes and `limit` is 20
- **THEN** the snippet statement SHALL be issued with exactly the 20 returned ids and SHALL compute at most 20 headlines per configured config

#### Scenario: A row indexed before migration 031 is counted, not guessed

- **WHEN** a returned row's `body_excerpt` is NULL
- **THEN** the row SHALL carry neither `snippet` nor `head`, the header SHALL count it, and the footer SHALL name `make rebuild-tsvectors`

#### Scenario: Matched terms are counted on the page only

- **WHEN** the query `alpha beta gamma` returns an `any` row containing `alpha` and `gamma`
- **THEN** that row SHALL carry `matched_terms: 2/3`

### Requirement: The keyword_search docstring states the conjunction and the result fields, as a peer of semantic_search

The `keyword_search` docstring SHALL state that bare terms are ANDed and that partial matches follow the full matches tagged `match: any`; SHALL show the `"exact phrase"`, `a OR b` and `-excluded` forms; SHALL describe `snippet`/`head`, `matched_in` and `matched_terms`; and SHALL keep the cross-reference to `semantic_search` in the same shape `semantic_search` uses for it, ranking neither tool above the other.

#### Scenario: The docstring names the semantics

- **WHEN** the registered tool description for `keyword_search` is read
- **THEN** it SHALL contain the AND default, the three operator forms, and the five result-field names

#### Scenario: The two tools remain symmetric peers

- **WHEN** the registered descriptions of `keyword_search` and `semantic_search` are compared
- **THEN** each SHALL point at the other for the cases it is weaker on, and neither SHALL describe itself as the primary or preferred search

## MODIFIED Requirements

### Requirement: Keyword search planner setting and deterministic ordering

`full_text_search` SHALL issue `SET LOCAL random_page_cost = 1.1` in the same transaction as every statement it runs for one call — the conjunctive statement, the disjunctive fallback when one runs, and the page-bounded snippet statement (the same transaction-scoped setting the vector path uses, no global Postgres change) — and SHALL order each block's rows by rank descending with `file_path` ascending as a deterministic tie-break, so membership and order within a block are stable across plans, and SHALL never interleave the two blocks. Matching semantics of the conjunctive statement SHALL be unchanged: for any query and filters the set of rows tagged `match: "all"` SHALL equal the set returned without the planner setting.

#### Scenario: Setting is issued and results are unchanged

- **WHEN** `full_text_search` runs against a populated database with and without the planner setting for a matrix of rare and common terms combined with folder, tag, frontmatter, and user filters
- **THEN** the returned rows SHALL be identical in membership, tag and order in every case

#### Scenario: Index plan on a production-sized corpus

- **WHEN** `EXPLAIN (ANALYZE, BUFFERS)` is run for a rare-term query over a seeded, analysed keyword corpus of at least 3,000 notes with realistic tsvector sizes, once with the setting applied and once with the sequential baseline forced by `SET LOCAL enable_indexscan = off; SET LOCAL enable_bitmapscan = off` (leaving `enable_seqscan` on)
- **THEN** the plan with the setting SHALL use the tsvector index (bitmap or index scan) and SHALL read fewer buffers than the forced sequential plan for the same query (asserted); for a common-term query the plans and buffer counts are recorded, not asserted

#### Scenario: One transaction carries every statement of a call

- **WHEN** a call issues the conjunctive statement, a fallback and a snippet statement
- **THEN** all three SHALL execute on the connection that issued the `SET LOCAL`, before that transaction ends

### Requirement: Read paths SHALL select only the columns they render
`semantic_search`, `keyword_search`, `list_notes`, `get_recent`, `find_orphans` and `get_neighborhood`'s metadata hydration SHALL each select an explicit list of columns, and that list SHALL contain neither `notes_metadata.content_tsvector`, nor `notes_metadata.body_excerpt`, nor `note_embeddings.embedding`. `content_tsvector` and `body_excerpt` SHALL be mapped as deferred attributes that raise when read from a loaded entity, so that no whole-entity load ships them and an accidental reader fails loudly instead of lazy-loading. `keyword_search`'s snippet statement SHALL read `body_excerpt` only inside `ts_headline`, projecting the headline and never the excerpt.

The change SHALL NOT alter any predicate, `SET LOCAL`, overfetch, or exact-fallback condition of the vector paths. It SHALL NOT alter any rendered field of those paths, including the staleness fields (`content_hash`, `embedded_content_hash`) and `chunks_truncated`, which SHALL remain selected wherever they are rendered today.

`list_notes` and `get_recent` SHALL order by `modified_at DESC, file_path ASC`. `find_orphans` SHALL keep its existing `modified_at DESC NULLS LAST` and add `file_path ASC` after it. Rows with exactly equal `modified_at` therefore have a deterministic order.

Results SHALL be identical to the pre-change implementation (the same result set, the same order, and every non-similarity field byte-equal) with exactly three permitted exceptions, all confined to exact ties — where for `keyword_search` "results" means the rows tagged `match: "all"` compared on the pre-change fields, the `any` block having no pre-change counterpart:
- **membership at a tied cutoff**: when more rows share the boundary sort key (`modified_at`, `rank` or distance) than fit under the limit, which of them are returned MAY differ;
- **the representative chunk among exact distance ties**: when two chunks of one note have exactly equal distance, the kept `chunk_index` and its preview MAY differ.
- **order among exact ties**: rows whose sort key (`modified_at`, `rank` or distance) is exactly equal MAY appear in a different relative order, even when all of them fit under the limit (the previous order among such rows was unspecified; the new `file_path ASC` tie-break makes it deterministic).

No other difference is permitted.

#### Scenario: No detoasted column is selected
- **WHEN** each of the six statements and the snippet statement is compiled
- **THEN** no SELECT list SHALL contain `content_tsvector`, `body_excerpt` or `embedding` as a projected column

#### Scenario: A deferred column read raises
- **WHEN** code loads a `NoteMetadata` entity and reads `content_tsvector` or `body_excerpt`
- **THEN** the access SHALL raise rather than issue a load

#### Scenario: Results match the previous implementation
- **WHEN** the same fixed corpus and query set are run through the previous and the new implementation, covering stale, truncated, filtered, unfiltered and exact-fallback cases, with the keyword oracle compared against the `all` block
- **THEN** the result sets SHALL be equal, the order SHALL be equal, and every field other than `similarity` SHALL be byte-equal, except for the three permitted tie cases

#### Scenario: The permitted tie differences are exercised
- **WHEN** the corpus contains more notes with an identical `modified_at` than the requested limit, two notes with an identical `modified_at` that both fit under the limit, and one note with two chunks at exactly equal distance
- **THEN** the oracle SHALL accept a different membership at that cutoff, a different relative order of the two tied notes, and a different representative chunk for that note, and SHALL reject any other difference

#### Scenario: Orphans with no modification time stay last
- **WHEN** `find_orphans` returns notes some of which have a NULL `modified_at`
- **THEN** those notes SHALL be ordered after every note with a non-NULL `modified_at`, as before
