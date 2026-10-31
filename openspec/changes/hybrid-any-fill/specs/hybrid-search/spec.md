## MODIFIED Requirements

### Requirement: hybrid_search SHALL be a read-only peer search tool fusing a keyword arm and a semantic arm by reciprocal rank fusion over note paths
The server SHALL register a tool `hybrid_search(query, limit=10, folder=None, tags=None, frontmatter=None)` that returns notes ranked by reciprocal rank fusion of two arms run for the same query, owner and filters. The keyword arm is the `keyword_search` ranking (the conjunctive `match: all` block, then the `match: any` fill when the block under-fills, with today's weights, parse and ordering) taken to 50 rows. The semantic arm is the `semantic_search` ranking (the three transaction-scoped settings, the `halfvec`-ordered statement, the full-precision re-sort, the per-note dedupe and the zero-row exact re-run) over a 50-chunk overfetch, with no truncation after dedupe. Each arm's rank SHALL be the 1-based position of a note in that arm's list. The fused score of a path SHALL be the sum, over the arms containing it, of `1 / (k + R)`, where `k` is `HYBRID_RRF_K` and `R` is the arm's fusion rank. The fusion rank SHALL equal the arm rank for every semantic row and every `match: all` keyword row. When both arms ran, the fusion rank of a `match: any` keyword row SHALL be its keyword arm rank plus `HYBRID_ANY_FILL_OFFSET`, a constant equal to `HYBRID_ARM_DEPTH` (50) that is neither a setting nor a per-call argument (#403), so that every `match: any` keyword term (at most `1/(k+51)`) is strictly smaller than every `match: all` keyword term (at least `1/(k+50)`) for every `k`. A `match: any` row's term SHALL still be added to the same note's semantic term. When the semantic arm was unavailable, no offset SHALL be applied, and the keyword-only page SHALL be byte-identical to its rendering before #403. Results SHALL be ordered by fused score descending, then by the smaller of the note's **unoffset** arm ranks ascending, then by path ascending. Against the implementation before #403, on the same corpus, filters and arguments: the fused candidate set (the union of both arms before `limit`) SHALL be identical; a call that fused no `match: any` row SHALL render a byte-identical response; and the relative order of the fused rows that carry no `match: any` keyword rank SHALL be unchanged. Only the positions, scores and keyword `explain` terms of rows carrying a `match: any` keyword rank, and therefore which rows fall inside `limit` or the rerank window, MAY differ. The page SHALL be the first `limit` fused rows, with `limit` clamped to 1–50. Both arms SHALL filter through the same `apply_note_filters` predicate, including the owner predicate. Reciprocal rank fusion SHALL be computed over the top 50 of each arm only, not over the arms' full rankings: a note outside both top-50 lists SHALL NOT be a candidate, even when its score over the full rankings would exceed a returned row's. The header SHALL always carry `arm_depth: keyword <n>/50, semantic <c>/50 chunks`, where `n` is the number of keyword rows returned and `c` the number of chunk rows the semantic statement returned, so that a full arm (`50/50`) tells the agent that deeper candidates were not considered. `keyword_search` and `semantic_search` SHALL keep their arguments, statements, ordering and output unchanged.

#### Scenario: A note in both arms outranks a note in one
- **WHEN** with `k = 60` note A is keyword rank 1 and semantic rank 2, and note B is semantic rank 1 and absent from the keyword arm
- **THEN** A's score SHALL be `1/61 + 1/62`, B's SHALL be `1/61`, and A SHALL be listed before B

#### Scenario: Ties are broken without favouring an arm
- **WHEN** note P is semantic rank 1 only and note Q is keyword rank 1 only
- **THEN** both SHALL score `1/(k+1)`, both have best rank 1, and the one with the smaller path SHALL come first

#### Scenario: The k setting changes the fused order
- **WHEN** the integration corpus is searched with `HYBRID_RRF_K=60` and again with `HYBRID_RRF_K=1`
- **THEN** each result SHALL equal the order computed by the formula for that k on the two arms' recorded ranks

#### Scenario: A consensus note beyond both arm depths is not a candidate
- **WHEN** with `k = 60` note A would be rank 51 in both arms' full rankings (full-ranking score `2/111 ≈ 0.0180`) and note B is semantic rank 1 only (score `1/61 ≈ 0.0164`), and both arms return a full 50
- **THEN** B SHALL be returned and A SHALL NOT, as the documented and expected omission, and the header SHALL read `arm_depth: keyword 50/50, semantic 50/50 chunks`

#### Scenario: A filtered-out note is in neither arm
- **WHEN** `folder="Projects/"` is given and a note outside that folder matches the query lexically and semantically
- **THEN** it SHALL appear in neither arm and not in the result

#### Scenario: The existing tools are unchanged
- **WHEN** the projection-identity, keyword-ranking and search-recall suites run after the service refactor
- **THEN** they SHALL pass unmodified

#### Scenario: A fill row cannot outrank an all-terms row through the keyword arm
- **WHEN** with `k = 60` and both arms run, note A is keyword rank 50 (`match: all`) only and note B is keyword rank 1 (`match: any`) only
- **THEN** A SHALL score `1/110` and B `1/111`, and A SHALL be listed before B

#### Scenario: A fill row still adds to a semantic hit
- **WHEN** with `k = 60` note E is semantic rank 2 and keyword rank 20 (`match: any`), and note J is semantic rank 9 and keyword rank 1 (`match: any`)
- **THEN** E SHALL score `1/62 + 1/130` and J `1/69 + 1/111`, and E SHALL be listed before J (before #403, J's `1/69 + 1/61` led)

#### Scenario: A fill-only note sorts below every semantic-only note
- **WHEN** with `k = 60` and both arms run, note F is keyword rank 1 (`match: any`) only and note S is semantic rank 50 only
- **THEN** F SHALL score `1/111` and S `1/110`, and S SHALL be listed before F

#### Scenario: The tie-break uses unoffset ranks
- **WHEN** two notes have equal fused scores and one of them carries a `match: any` keyword rank
- **THEN** the note with the smaller unoffset arm rank SHALL come first, then the smaller path

#### Scenario: No fill row, byte-identical
- **WHEN** the existing hybrid fixtures whose keyword arm holds only `match: all` rows (`keyword_any = 0`) are rendered before and after #403
- **THEN** the responses SHALL be byte-identical

#### Scenario: The keyword-only page is unchanged
- **WHEN** the embedding provider fails and the keyword arm holds `match: all` and `match: any` rows
- **THEN** the page SHALL be byte-identical to its rendering before #403: keyword order, each row's `explain` the unoffset `1/(k+rank), keyword only`

#### Scenario: Identity oracle on fill-bearing pages
- **WHEN** a corpus whose queries produce both `match: all` and `match: any` keyword rows is searched before and after #403 with `limit = 50`
- **THEN** the set of fused paths SHALL be identical, and the subsequence of rows without a `match: any` keyword rank SHALL be in the same order with byte-identical lines except for any abbreviation the response bound applies

### Requirement: Each result SHALL render on one line with every note-controlled field as a JSON string literal, followed by the server-authored provenance, so that no note content can forge a row or a field
Each result SHALL be exactly one line of the form `- {"title":…,"path":…,"tags":[…],"preview_kind":…,"preview":…}`, then the separator `"} — ` (whose `"` closes the `preview` literal), then the server-authored fields. Every note-controlled value (title, path, each tag and the preview) SHALL be encoded as a JSON string literal under the `get_changes` rule (`change_feed.quote`): `"` and `\` escaped, and C0 and C1 controls, DEL, U+2028 and U+2029 escaped. The object's keys SHALL be fixed and always present, in that order, with `preview` last. When there is no preview, `preview` is `""` and `preview_kind` is `"none"`. The object therefore always ends with an unescaped `"}`, which no literal can contain. The server-authored fields SHALL follow the separator in this order: the fused score; `semantic_rank` (with similarity) or `-`; `keyword_rank` (with `match`, and `matched_in` when computed) or `-`; `explain`; `stale: true` / `embedding_truncated: true` when true; and any abbreviation labels. `explain` is built only from server values: the `1/(k+R)` term of each arm present, where `R` is that arm's fusion rank as the fusion requirement defines it (for a fused `match: any` keyword row, `keyword_rank + 50`, so the rendered terms always sum to the rendered score), and `semantic only` or `keyword only` for a single-arm row. `keyword_rank` SHALL remain the unoffset arm position, and the preview choice SHALL compare unoffset ranks. No header token SHALL be added for the offset: the row's `match: any` and the header's `(all a, any b)` split disclose the fill. `preview_kind` SHALL be `snippet` (a keyword body highlight in `«»`), `head` (the keyword head text) or `chunk` (at most 200 characters of the stored chunk), taken from the arm with the smaller rank. On equal ranks it SHALL be the keyword highlight when one exists, and otherwise the chunk. When the chosen arm has no preview, the other arm's SHALL be used. A stale row's chunk SHALL be replaced by the stale-preview notice (`preview_kind: "stale_notice"`) and never shown. The query echoed in the header SHALL also be a JSON string literal, cut by rendered length as the response-bound requirement defines, with `query_cut` in the header when it was cut. The header SHALL always carry `arms`, `rrf_k`, `arm_depth`, the per-arm note counts (with the keyword `all`/`any` split), the count in both arms, the stale and truncated counts, `shown`, `without excerpt`, and `confidence: ok` or `confidence: low`, each stated even when zero. The `confidence` token SHALL be computed by the rule the `search-quality` capability defines ("`semantic_search` and `hybrid_search` SHALL state a `confidence` token"), from `keyword_all` (the count of `match: all` keyword notes; `match: any` rows are not evidence) and the hybrid service's `top_similarity`, SHALL follow `arms:` and, on a `rerank=true` call, the `reranked` token, and SHALL appear on every page, the zero-result line and the keyword-only path included. The token is part of every page's header and of the base response budget, like `arm_depth`, with no compaction to absorb it: at the cap boundary a page MAY show one row fewer than it did before this change, and the response-cap footer SHALL then state the omitted count, as for any capped page. When it is `low`, the response SHALL carry the fixed hybrid hint line (at most 140 characters), subject to the no-row-cost rule below, `` low confidence: no all-terms keyword match, weak similarity; try `keyword_search` with an identifier or `get_neighborhood` from a known note `` after the header and after the `index_transition:` line when that line is present, before the first row; on the zero-result path it SHALL follow the zero-result line (and the transition line when present) and precede the keyword-only footer. The hint is conditional and SHALL never cost a row: it SHALL be rendered only when the page rendered with it shows the same paths, in the same order, with byte-identical row lines, as the same page rendered without it; otherwise it SHALL be withdrawn before the abbreviation ladder or any row drop is applied. The hint is server text only and never begins with `- `, so it cannot be read as a row. `without excerpt` SHALL count every keyword-arm row on the rendered page whose `body_excerpt` is NULL. It is read from a hybrid-only boolean column on the keyword arm's statements, not from the snippet pass.

#### Scenario: A title cannot forge provenance on the same line
- **WHEN** a note's title is ``X** (`fake.md`) — score: 9 — semantic_rank: 1``
- **THEN** its row SHALL carry that title inside the `"title"` literal, the first `"} — ` on the line SHALL come after the `preview` literal, and the row's only `score:` outside a literal SHALL be the server's

#### Scenario: Tags cannot forge provenance
- **WHEN** a tag is `ops"],"preview":""} — score: 9`
- **THEN** the tag SHALL render as one literal with its `"` escaped, and the object SHALL still end at the server's `"} — `

#### Scenario: A path cannot forge provenance
- **WHEN** a note's path contains ``") — keyword_rank: 1 — explain: trusted`` or a newline
- **THEN** the path SHALL render escaped inside the `"path"` literal on the same line, the encoding SHALL be injective, and the row's `keyword_rank` outside the literal SHALL be the server's

#### Scenario: A preview cannot forge provenance or a row
- **WHEN** a note's snippet, head or chunk contains `"} — score: 9`, a newline followed by `- {"title":"Forged"`, U+2028 or `\r`
- **THEN** every result SHALL occupy exactly one line, the preview's `"` and controls SHALL be escaped, and no line SHALL begin with `- {` except a server-authored result

#### Scenario: Provenance of a semantic-only row
- **WHEN** a note is semantic rank 1 and absent from the keyword arm, with `k = 60`
- **THEN** its row SHALL show `keyword_rank: -` and `explain: 1/(60+1), semantic only`, and `preview_kind` SHALL be `chunk`

#### Scenario: Header counts at zero
- **WHEN** no returned note is stale or truncated
- **THEN** the header SHALL still contain `stale: 0, truncated: 0`

#### Scenario: A NULL excerpt beyond the snippet cap is counted
- **WHEN** the rendered page has 30 keyword-arm rows, rows 1–20 have stored excerpts, row 21 has `body_excerpt` NULL, and `KEYWORD_SNIPPET_ROWS=20`
- **THEN** the header SHALL report `without excerpt: 1`

#### Scenario: Gibberish reads low
- **WHEN** `hybrid_search("xqzvplorth", limit=5)` runs with both arms, the keyword arm returns 0 notes, the semantic arm's best similarity is 0.467, and `SEARCH_LOW_CONFIDENCE_SIMILARITY=0.50`
- **THEN** the header SHALL carry `confidence: low` directly after `arms: both` and the hint line SHALL follow the header (after the `index_transition:` line when present)

#### Scenario: Disjunctive filler is not evidence
- **WHEN** `hybrid_search("xqzvplorth notes")` gets 0 `match: all` rows, 30 `match: any` rows on the word `notes`, and a best similarity of 0.467 with the threshold at 0.50
- **THEN** the header SHALL carry `confidence: low`, the `any` rows SHALL be listed as fusion ranks them, and the hint SHALL be present when it costs no row

#### Scenario: A real paraphrase reads ok
- **WHEN** a paraphrase query has 0 `match: all` rows and a best similarity of 0.618 with the threshold at 0.50
- **THEN** the header SHALL carry `confidence: ok` and no hint line SHALL appear

#### Scenario: An all-terms keyword match is never low
- **WHEN** the keyword arm returns at least one `match: all` note, whatever the similarities
- **THEN** the header SHALL carry `confidence: ok`

#### Scenario: The keyword-only path
- **WHEN** the embedding provider fails (either code) and the keyword arm finds three `match: all` notes
- **THEN** the header SHALL carry `confidence: ok`; and when it finds no `match: all` note (none at all, or only `any` rows) with the threshold above 0, the header or zero-result line SHALL carry `confidence: low`, and on the zero-result path the hint line SHALL precede the keyword-only footer

#### Scenario: The hint never costs a row at the cap boundary
- **WHEN** a `low` page of 10 rows is rendered under a `MAX_READ_RESPONSE_CHARS` at which the 10th row fits without the hint reserved and does not fit with it
- **THEN** the response SHALL show the same 10 paths with byte-identical row lines as the page rendered with the hint withdrawn, and SHALL carry no hint line; and at a cap 200 characters larger the same 10 rows SHALL be shown with the hint present

#### Scenario: The token can cost a boundary row, and the footer says so
- **WHEN** a `rerank=true` page of 10 candidates is rendered under a `MAX_READ_RESPONSE_CHARS` at which, before this change, all 10 rows fitted with fewer than 17 characters to spare
- **THEN** the page SHALL show 9 rows, the header's `shown` SHALL be 9, and the footer SHALL read `Response cap: 1 omitted — more: true; narrow with limit= or folder=.`; the same page with `rerank=false` SHALL end with `Response cap reached: 1 more result omitted; narrow the query.`

#### Scenario: Reranking does not change the token
- **WHEN** the same call is made with `rerank=false` and with `rerank=true`, reranked or falling back
- **THEN** both responses SHALL carry the same `confidence` value

#### Scenario: A fill row's explain shows the fusion rank
- **WHEN** with `k = 60` and both arms run, a note is semantic rank 12 and keyword rank 5 with `match: any` and `matched_in: title,body`
- **THEN** its row SHALL show `keyword_rank: 5 (match: any, matched_in: title,body)` and `explain: 1/(60+12) + 1/(60+55)`, and its `score` SHALL equal the sum of those terms at `.4g`

### Requirement: The response SHALL be bounded by the read-response character cap at every setting, measured on rendered (escaped) length, dropping whole rows and always rendering a bounded first row
`hybrid_search`'s response SHALL be at most `MAX_READ_RESPONSE_CHARS` characters (minimum 1,000), for every value of `HYBRID_RRF_K` and on both the both-arms and the keyword-only path. Every length in this requirement is a **rendered** length: characters after `change_feed.quote` escaping, in which a control character costs six (`\u00XX`) and `"` or `\` costs two. A cut never splits an escape sequence and never adds a character inside a literal; the cut is stated only by a server label after the separator or in the header. The query echo SHALL be the longest prefix of whole characters of the query whose rendered literal content is at most 64 characters. When that is not the whole query, the header SHALL carry `query_cut`. On a `rerank=false` call the header SHALL be at most 405 characters in its worst case: the 66-character query literal, `query_cut`, `rrf_k: 1000`, the keyword-only token with the longer code, the `confidence` token, and every count, each at most 100 by construction (arms ≤ 50, union ≤ 100, snippet cap ≤ 100). The `confidence: low` worst case (no `match: all` row, up to 50 `match: any` rows, the snippet-cap token) and the `confidence: ok` worst case SHALL both be measured and both be at most 405. On a `rerank=true` call the header SHALL be at most 444 rendered characters under the same inputs plus the longest `reranked` token, for both `ok` and the low-plus-`any` case. The low-confidence hint line is never part of the bound: it is rendered only when it changes no row (see the rendering requirement), so the arithmetic below holds without it. The keyword-only footer SHALL be one fixed sentence of at most 75 characters, and the response-cap footer SHALL be at most 75 characters. Rows SHALL be appended in fused order while each fits after the header and footers are reserved. The first row that does not fit SHALL end the page. The header's `shown` SHALL count the rendered rows, and the footer SHALL state how many results were omitted. Later rows SHALL be dropped whole and never cut. When the first row does not fit, it SHALL be abbreviated in order, stopping as soon as it fits: drop the tags (`tags omitted`); drop the preview (`preview omitted`); then cut the title (`title_cut`) and the path (`path_cut`). Each cut keeps the shortest prefix of whole characters whose rendered content is at least 40 characters, so the kept content is 40 to 45 rendered characters, which may be as few as 7 raw characters of a control-heavy value. A value whose whole rendered content is at most 45 characters is not cut. If even that does not fit, the field SHALL render as the omitted form `"title":""` / `"path":""` with `title_cut` / `path_cut` and `title omitted` / `path omitted`. If even the omitted form does not fit, the row's `explain` field SHALL be replaced by the label `explain omitted` (its terms follow from the row's ranks and the header's `rrf_k`). This last step exists for the 1,000-character floor on a both-arms `rerank=true` page carrying the `index_transition:` line. Every label SHALL be server-authored and placed after the separator. The row SHALL be marked `abbreviated`. The abbreviated row's structure SHALL be at most 345 characters (340 before #391; the longest `matched_in` value grew from `title+body` to `title+tags+body`, five characters). The structure is `- `, the fixed JSON keys and quotes with empty values, the separator, every server field at its worst case (`k = 1000`, ranks up to 50, a `match: any` keyword fusion rank up to 100 in `explain` (#403), `matched_in: title+tags+body`, both markers) and every label of the cut form (`tags omitted`, `preview omitted`, `title_cut`, `path_cut`, `abbreviated`). The omitted form is always shorter than the cut form: it removes at least 80 rendered characters of title and path content and adds the 31 characters of `title omitted` and `path omitted`. The worst case at the floor SHALL be header 405 + row structure 345 + title 45 + path 45 + keyword-only footer 75 + cap footer 75 + at most 5 line breaks = 995 ≤ 1,000. The measured worst cases at the floor (8,192-control-character query, `HYBRID_RRF_K=1000`, the ten-digit `index_transition:` line where both arms ran), each re-measured and pinned by the tests for both `ok` and low-plus-`any`, SHALL be: keyword-only, `rerank=false`: cut form 981; keyword-only, `rerank=true`: cut form 1,008, omitted form 989; both arms, `rerank=false`: cut form 1,000; both arms, `rerank=true`: omitted form 1,008, `explain omitted` form 988 (each five more than with `title,body`, re-measured with #391's longest `matched_in`; the both-arms cut and omitted forms one more again since #403, whose three-digit fill fusion rank lengthens the keyword `explain` term; the keyword-only figures and the `explain omitted` form do not carry the offset and are unchanged). Every page SHALL be at most `MAX_READ_RESPONSE_CHARS`.

#### Scenario: Worst case at the minimum cap, both arms, control-heavy
- **WHEN** `MAX_READ_RESPONSE_CHARS=1000` and `HYBRID_RRF_K=1000`; the query is 8,192 control characters; and the first result, found by both arms, has a 512-character title of control characters, a 1,024-character path holding control characters, `"` and `\`, 1,000 tags and a preview
- **THEN** the response SHALL be at most 1,000 rendered characters; the header SHALL carry a query literal of at most 64 rendered content characters and `query_cut`; and the response SHALL contain one abbreviated row whose title and path literals each hold 40–45 rendered characters with no split escape, with `title_cut`, `path_cut`, `tags omitted`, `preview omitted` and `abbreviated`

#### Scenario: Worst case at the minimum cap, keyword-only, control-heavy
- **WHEN** the same inputs are searched while the provider raises `ProviderInputTooLarge`
- **THEN** the response SHALL be at most 1,000 rendered characters, and SHALL carry `query_cut`, `arms: keyword-only (semantic unavailable: provider_input_rejected)`, the fixed keyword-only footer, and the abbreviated row with 40–45 rendered characters each of title and path

#### Scenario: The cut never splits an escape
- **WHEN** a title consists of `"` and U+0001 alternating, and the row is abbreviated
- **THEN** the kept title literal SHALL parse as a JSON string, its content SHALL be 40–45 rendered characters, and it SHALL decode to a prefix of the title

#### Scenario: The omitted form when the kept prefix cannot fit
- **WHEN** the test first measures the rendered length `L_omit` of the response whose first row is in the omitted form and the length `L_cut` of the same response in the cut form, asserts `L_cut > L_omit`, and then calls the renderer directly with the budget `L_omit`
- **THEN** it SHALL render `"title":""` and `"path":""` with `title_cut`, `title omitted`, `path_cut` and `path omitted`, and the response SHALL be at most `L_omit` characters

#### Scenario: Low confidence at the minimum cap keeps the bound
- **WHEN** `MAX_READ_RESPONSE_CHARS=1000`, `HYBRID_RRF_K=1000`, the query is 8,192 control characters, the keyword arm holds 50 `match: any` rows and no `match: all` row, the best similarity is below the threshold, the caller has 9,999,999,999 notes pending re-chunk, `rerank=true` falls back with `malformed_response`, and the first row has a control-heavy title and path, 1,000 tags, a section locator and both markers
- **THEN** the response SHALL be at most 1,000 rendered characters, the header SHALL carry `confidence: low`, the hint line SHALL be absent, and the first row SHALL carry `abbreviated`

#### Scenario: The explain-omitted form when only it fits
- **WHEN** the test measures the first row's omitted form and calls the abbreviation with a room in which only the `explain omitted` form fits
- **THEN** the row SHALL render the label `explain omitted` in place of the `explain` field, after both omitted forms, ending `abbreviated`; the D5 figures 1,007 → 988 are structural sums of the worst-case terms, not rendered pages

#### Scenario: The rerank=true header bound
- **WHEN** the keyword-only worst-case header is rendered with `reranked: false (malformed_response)`, once with 50 `match: all` rows (`ok`) and once with 50 `match: any` rows (`low`)
- **THEN** each header SHALL be at most 444 characters

#### Scenario: The longest matched_in value is inside the pinned structure bound

- **WHEN** the abbreviated first row's structure is rendered at its worst case with `matched_in: title+tags+body`, `HYBRID_RRF_K=1000`, ranks of 50 and both markers, on a `rerank=false` page
- **THEN** its length SHALL be at most 345 characters, the pinned constant SHALL be 345, and the worst-case control-heavy response at `MAX_READ_RESPONSE_CHARS=1000` SHALL keep title and path literals of 40–45 rendered characters each

#### Scenario: The fill fusion rank is inside the pinned structure bound
- **WHEN** the abbreviated first row's structure is rendered at its worst case with `HYBRID_RRF_K=1000`, semantic rank 50, keyword rank 50 with `match: any` (fusion rank 100), `matched_in: title+tags+body` and both markers, on a `rerank=false` and on a `rerank=true` page
- **THEN** the lengths SHALL be at most 345 and 367 respectively, and the both-arms `rerank=false` worst-case control-heavy response at `MAX_READ_RESPONSE_CHARS=1000` SHALL still keep title and path literals of 40–45 rendered characters each

### Requirement: The three search tools SHALL describe themselves as peers with one shared routing sentence
The published descriptions of `keyword_search`, `semantic_search` and `hybrid_search` SHALL each contain the routing sentence: exact identifiers, code symbols, titles or known phrases → `keyword_search`; conceptual or paraphrased questions → `semantic_search`; unsure which → `hybrid_search`. None of the three SHALL describe itself or another as primary, default, best or first. `hybrid_search`'s description SHALL state the fusion (reciprocal rank fusion of both arms' top 50 with equal votes), that a `match: any` keyword row enters at `rank + 50`, below every all-terms row (#403), the per-row provenance fields, the keyword-only marker, and that a single arm can outrank the fusion on its own kind of query. Each description SHALL stay at most 4,000 characters on the wire.

#### Scenario: Symmetric routing
- **WHEN** the three descriptions are read from the registry
- **THEN** each SHALL name all three tools in the routing sentence, and none SHALL contain "primary", "default search" or "best"

#### Scenario: The description states the fill offset
- **WHEN** `hybrid_search`'s description is read from the registry
- **THEN** it SHALL state that a `match: any` keyword row enters fusion at `rank + 50`, and SHALL be at most 4,000 characters
