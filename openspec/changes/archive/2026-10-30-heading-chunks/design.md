# Design: heading-aware chunks, a context prefix, and query-side instructions (#355, #367 provider half)

## Context

Today, `embed_note` runs `clean_for_embedding(body)` (the shared fence
recognizer, `BODY` context), then `chunk_text_bounded(cleaned, chunk_size=512,
overlap=0, max_chunks=1000)`. That function makes character windows of
`512 × 4` characters, strips each window, keeps an unstripped single chunk when
the whole text fits, and stops one past the cap. It embeds each chunk's text as
is. `note_embeddings` holds `(note_id, chunk_index, chunk_text, embedding)`.
Reuse (#281, D16) keys on byte-identical `chunk_text`, needs the stored
fingerprint to be `MATCH`, and is rechecked under the generation lock by row id
and text.

Section addressing (`src/services/vault.py`) resolves `read_note(section=…)`
over the frontmatter-stripped body. `_scan_headings` runs the ATX pattern over
`mask_code(body, context=BODY)`, so headings inside fenced or inline code are
not headings. `#N` is the Nth entry of that list, 1-based and positional.

Two facts from reading the code shape this design. Neither is the obvious
assumption.

1. **A fingerprint change is not an automatic reset. It is a refused
   startup.** `_check_embedding_fingerprint` exits non-zero on `DIFFERS`. The
   documented runbook is: deploy, the new image refuses, `make
   reset-embeddings` while it is down, restart. The reset drops and recreates
   the vector column and clears every `embedded_content_hash`, so semantic
   search answers nothing useful until the re-embed finishes. Production
   deploys on merge (Flux image automation on k3s). If a chunking bump were
   folded into the fingerprint, merging this change would put the new pod in a
   crash loop until an operator ran a reset from a one-off Job, and then serve
   an empty vector index for hours.
2. **Clearing `embedded_content_hash` is not a quiet re-select either.** Under
   #200, a note whose `embedded_content_hash IS DISTINCT FROM content_hash` is
   annotated stale and its preview is withheld. Mass-clearing the hashes to
   force a re-embed would mark every result stale and withhold every preview
   for the whole re-embed, even though every stored chunk still quotes the
   note's current text.

Numbers. The vault has about 2,577 notes. The last documented chunk count is
about 16.7k (docs/architecture/search.md and the 2026-08 notes), about 6.5
chunks per note. The production deployment is multi-user, so the per-tenant
embed budget (`EMBED_CHUNK_BUDGET_PER_USER` 5,000, `EMBED_TIME_BUDGET_SECONDS_PER_USER`
300) is enforced. Measured batched Ollama throughput (performance-2026-09, task
9.6) is one 9-chunk request in 1.57 s, about 6 chunks/s, and the batches of 16
will be somewhat faster.

## Goals / Non-Goals

**Goals**

- A chunk never spans an ATX heading, and the ordinal it records is the one
  `read_note(section="#N")` resolves.
- The embedded text carries the note title, the heading breadcrumb and the
  tags. Previews stay un-prefixed.
- One re-embed of the vault, in the background, with no refused startup, no
  reset, no empty index and no stale markers during it, and no `/health`
  degradation caused by its length.
- A provider `kind` and a query-only instruction, so instruction-tuned models
  can be trialled on the harness without code changes.

**Non-Goals**

- The reranker (`RERANK_URL`, `rerank: bool`). It is a separate, later change.
- Changing the default embedding model. The A/B is operator work on #356.
- Late chunking, LLM-generated context, overlap, and boundary snapping inside a
  section. Windows inside a section are today's character windows.
- Setext headings. Section addressing does not recognize them either.
- Moving `chunk_size` / `chunk_overlap` / the cap from the refusing fingerprint
  to the per-note mechanism. It is possible later and out of scope here.

## D1: Sections come from the section-addressing scanner, over the same body

The chunker takes the same body `read_note` scans: the indexer's
`parse_frontmatter(raw)[1]`, which is the partition `read_file` applies. It
obtains headings from `_scan_headings(body)`, imported, not copied and not
re-implemented (the "no private grammar" rule from #150). For heading `i` (index
`i`, ordinal `i + 1`), the **own-text span** runs from that heading's
`body_start`, which is `line_end` plus one terminator (the same step
`_section_body_span` takes), up to the next heading's `line_start` **of any
depth**, or the end of the body. Section 0, the **preamble**, is
`body[0 : headings[0].line_start]`, or the whole body when there are no
headings.

Using "next heading of any depth", rather than `read_note`'s "next heading of
depth ≤ mine", means no text is in two sections. `read_note(section="#N")`
returns a superset: heading N's own text plus its subsections. So the
guarantee is **containment**: each chunk's text is a substring of section N's
body as `read_note` returns it, after the same fence removal. The tests assert
exactly that.

**The heading path** is the chain of enclosing headings, outermost first, ending
with heading `i`'s own text, built with `_path_chain_match`'s depth rule (each
step to a strictly shallower heading). Each component is stored trimmed, as
`_scan_headings` returns it, and cut at 200 characters (the cut is marked `…`).
A component is never interpreted as a selector, so a `/` inside a heading is
harmless. The ordinal is the selector. The preamble's path is the empty array,
with ordinal 0.

**Cleaning is computed once over the whole body.** The fence spans come from
`scan_fences(body, context=BODY)`, one call per note. Each section's embedded
text is its own-text span with the intersecting whole-body spans removed. A
matched fence never crosses a heading, because headings inside it are masked.
An unterminated column-zero fence masks to the end of the note, so no heading
follows it. So the result equals cleaning each segment, but the one-scan rule
removes any doubt, and the concatenation of the sections' cleaned texts equals
`clean_for_embedding(body)` with the heading lines removed (a test pins it).

**Windowing** is `chunk_text_bounded` itself, applied to each section's cleaned
text. The cap is a single counter across sections in document order. It stops
one chunk past `MAX_CHUNKS_PER_NOTE`, so `truncated` keeps its meaning and its
four declarations unchanged.

**Edge cases, decided:**

- **No headings.** One section (the preamble, the whole cleaned body), which is
  exactly today's `chunk_text_bounded(clean_for_embedding(body))`. The chunk
  texts are byte-identical to today's. Only the embedded input gains the title
  prefix.
- **An empty section** (own text is whitespace-only after cleaning) yields no
  chunk. That covers a parent heading whose content is all in subsections. Its
  text still appears in its children's heading paths and prefixes.
- **A long section** yields several chunks that share `heading_path` and
  `section_ordinal`, with consecutive `chunk_index`. There is no sub-ordinal
  column, because `chunk_index` already orders them and the follow-up call is
  the same for all of them.
- **Heading lines are not chunk text.** The heading is in the prefix and in the
  locator. A preview starts at the section's body.
- **The zero-chunk invariant.** If every section yields nothing but
  `chunk_text_bounded(clean_for_embedding(body))` would yield chunks (a note of
  headings only, such as a template skeleton), the note falls back to that
  whole-body chunking as section 0. So *a note has zero chunks exactly when
  today's chunker gives it zero*. `CERTIFIED_EMPTY`, the exclusion sweep's
  zero-chunk probe and #127's convergence exceptions keep their meaning
  unchanged. The sweep's probe calls the new chunker.
- **Line terminators.** `_scan_headings` and the span step already handle LF,
  CRLF and lone CR. The parity tests run all three.
- **An unmatched indented fence opener** (the shape `edit_note` refuses) is read
  under the not-a-fence reading, as `read_note` reads it. Reads stay
  asymmetric, as vault-tools.md states.

Rejected: splitting the cleaned text. Cleaning first moves offsets and can
change what looks like a heading, so the ordinals would no longer be
`read_note`'s.

## D2: The context prefix, and its cap

```
{title} > {h1} > … > {hk}
tags: {t1}, {t2}, …
                                   ← blank line
{chunk_text}
```

- `title` is `notes_metadata.title` (the one shared title rule, #154). Headings
  are the section's heading path. In each component, every run of whitespace
  is collapsed to one space, and the component is cut at 80 characters with a
  trailing `…`.
- **The cap is `EMBED_PREFIX_MAX_CHARS = 300`, a module constant.** If the
  breadcrumb is longer than 300, the outermost headings after the title are
  replaced by a single `…` component, one at a time, until it fits. The title
  and the innermost heading are always kept, and at 80 characters each they
  always fit. The `tags:` line, from `notes_metadata.tags` in stored order, is
  included **only if the whole prefix including it is at most 300**. It is
  never partially included.
- The stored `embed_prefix` is the breadcrumb plus the optional tags line. The
  embedded input is always `embed_prefix + "\n\n" + chunk_text`, built by one
  function, `embedding_input(prefix, chunk)`. The window still bounds the
  chunk. The prefix adds at most 300 characters (about 75 tokens), far inside
  bge-m3's 8K and Qwen3's 32K context.
- Section 0 has a title-only breadcrumb.

**Owner decision (default): tags are in the prefix, as #355 proposes.** The
cost is L4: a tag edit changes every chunk's prefix, so reuse misses and the
whole note is re-embedded. Today #281 makes that free. A later
`CHUNKING_VERSION` bump can drop them.

Rejected: a setting to turn the prefix off. The harness compares the incumbent
with the bundle. One less knob and one less fingerprint dimension.

## D3: Reuse keys on the exact embedded input, and the chunking version gates it

A vector is a function of (model, input string) (L6 of #281). The input is now
prefix plus chunk, so reuse requires all of the following, and a doubt anywhere
re-embeds:

1. the stored embedding fingerprint is present and `MATCH` (unchanged);
2. the note's `embedded_chunking_version` equals `CHUNKING_VERSION`. All of a
   note's rows are written in one transaction under one version, so this also
   covers a change to the input formatter (the separator, the tag line format)
   that leaves prefix and chunk byte-equal;
3. the row's `embed_prefix` is not NULL, and `(embed_prefix, chunk_text)`
   equals the new chunk's pair. A pre-034 row (NULL prefix) is never reused;
4. under the generation lock, `_generation_matches` rechecks every reused row id
   for this note with the same `(chunk_text, embed_prefix)` pair.

`heading_path` and `section_ordinal` are **not** part of the key. They describe
where the text sits, not what was embedded. A reused vector is re-inserted with
the new chunk's locator, so inserting a heading above (which shifts every later
`#N`) re-embeds nothing and still records correct ordinals.

A rename that changes a stem-derived title already clears
`embedded_content_hash` (both move paths, #127). The prefix changes, so that
note is re-embedded in full. A folder move keeps the title and stays free.

## D3a: The certification covers the exact provider input (Codex r1 M1)

Before this change, `embedded_content_hash` plus the cleaned-text comparison of
an extraction-version bump proved a stored vector current. Now the provider
input also depends on the title and tags. Those can change while the bytes do
not (a rename) and while the cleaned text does not (a tag-grammar revision).
Three mechanisms cover this, all in this change:

1. **`notes_metadata.embedded_input_digest`** (`varchar(64)`, migration 034).
   It is the SHA-256 hex of the canonical JSON of the ordered list of provider
   inputs (`embedding_input(prefix, chunk)` per chunk, `[]` for an empty or
   excluded note), stamped by `certify_embedded` in the same conditional
   UPDATE as the hash and the chunking version.
2. **Transactional invalidation on a changed prefix input.** Some index-pass
   statements rewrite a row's `title` or `tags` while its `content_hash` is
   unchanged: the extraction-version re-upsert, the re-derive, and both move
   paths. These statements add `embedded_content_hash = CASE WHEN title IS
   DISTINCT FROM :t OR tags IS DISTINCT FROM :g THEN NULL ELSE
   embedded_content_hash END`. The moves already clear it unconditionally, and
   the existing index-integrity grammar requirement is MODIFIED so that a
   differing title or tags counts as grammar-attributable invalidation.
   Headings come from the bytes. A heading-scan change is a `CHUNKING_VERSION`
   bump by rule.
3. **The full-hash backstop.** The full-hash pass already reads every file.
   For each certified note it reads under the current chunking version, it
   recomputes the input digest (CPU only, no provider call) and clears
   certification on a mismatch or a NULL digest. That bounds any missed
   invalidation path to `INDEX_FULL_HASH_INTERVAL_HOURS`.

## D4: The re-embed is a per-note chunking-version backlog, outside the fingerprint

**This departs from the brief's recommendation, and the reason is the two facts
in Context.** `CHUNKING_VERSION` (an int in `embeddings.py`, first value **1**;
NULL means "pre-034 chunking") is stamped on `notes_metadata.embedded_chunking_version`
by `certify_embedded`, in the same conditional UPDATE that stamps
`embedded_content_hash`. That UPDATE is the one write site shared by every
certifying branch: embedded, certified-empty and excluded.

- **The backlog predicate** gains `OR nm.embedded_chunking_version IS DISTINCT
  FROM :cv`. It is expressed once in Python and used by `_embed_vault_pinned`
  and `_vectors_not_current` (dashboard pending count and the progress
  endpoint), so those three cannot disagree.
- **Staleness is unchanged.** It is still `embedded_content_hash IS DISTINCT
  FROM content_hash`. A version-only note's stored chunks quote its current
  text, so it is fresh, its preview is shown, and its legacy row prints no
  locator.
- **Why this is not the "lazy per-note re-embed" the embedding-providers spec
  forbids.** That requirement forbids per-row model identity because two
  *models* in one column make cosine distance meaningless. Here the model,
  dimension and provider are unchanged and stay in the refusing fingerprint.
  Legacy and heading-era vectors are the same model's embeddings of
  overlapping text, in one space. **Mixing them is not correctness-safe for a
  bounded page** (Codex r1 B1). A prefix changes a note's distance to a query,
  so a re-embedded note can push a still-legacy note out of the top `limit`,
  and that page would then omit a note that was reachable before the deploy.
  So the transition is *declared* rather than hidden. While any note in the
  caller's scope is on an older chunking version, every `semantic_search` and
  `find_related` response carries, directly after its header, the line
  `index_transition: N notes not yet re-chunked; cross-generation ranking is
  approximate, re-run after the backlog drains`. N is owner-scoped and read
  in the same `REPEATABLE READ, READ ONLY` transaction as the vector query.
  The line appears on the empty path too, and is absent at zero. `/health`
  reports only `indexer.rechunk: "in_progress" | "complete"`, process-wide and
  non-volumetric, because the endpoint is unauthenticated and a count would be
  tenant-derived (Codex r2 B1). It never changes `status`. Exact per-owner
  counts stay on the panel and in the owner-scoped marker. Every surface
  (marker, backlog, dashboard, progress endpoint, health) uses
  `embedded_chunking_version IS DISTINCT FROM :CHUNKING_VERSION`. A rollback
  to a lower version therefore re-selects and marks notes stamped higher
  (Codex r2 B2). **Amended after the implementation review:** the marker
  and `/health` additionally require the note to hold vector rows
  (`EXISTS (SELECT 1 FROM note_embeddings …) AND … IS DISTINCT FROM`, the
  harness's `legacy_chunked_notes` definition), so a note created since the
  last embed pass, with no vectors at all, does not raise the line in steady
  state; the backlog, dashboard and progress endpoint keep the broad
  predicate. The ranking itself is still mixed (L1). A wipe would have avoided
  the mix but left semantic search returning nothing for hours, which is
  worse. A new ADDED requirement in embedding-providers states the boundary:
  model identity is global and refuses, while the chunking format is per note
  and re-selects.
- **The stored fingerprint is untouched** (`v` stays 1), so the deploy neither
  refuses nor resets. A later chunking change is one constant bump plus its
  tests.
- **Ordering and budget.** The backlog SELECT orders content-stale notes first
  (`embedded_content_hash IS DISTINCT FROM content_hash`, by `modified_at
  DESC`, as today), then version-only notes (by `modified_at DESC`). The
  existing `EmbedBudget` applies to the version-only segment **in every mode**,
  single-user included, with the same at-least-one-note and note-boundary rules.
  In a multi-scope pass it applies to both segments, as today. Without it, a
  single-user deployment's first pass after deploy would hold the indexer for
  the whole re-embed (an hour or more), and edits made meanwhile would reach the
  keyword index only afterwards. With it, each tick spends at most 300 s on the
  transition, and real edits go first. A budget stop is not a failure, as
  today.
- **`/health`.** Nothing keys on backlog size or pass length. The transition
  increments no failure counter unless the provider actually fails, in which
  case the existing per-scope accounting applies as it would to any backlog.

**No build overlap in the supported deployments (L2).** The repository's
Kubernetes bundle (`deploy/kubernetes/base/deployment.yaml`) runs one replica
with `strategy: Recreate`, and migration 034 runs in the replacement pod's
init container. Compose `make deploy` stops the app before migrating. In
neither path can a 033 process write after 034 has migrated. Only a
deployment modified outside the repository has such a window, for example
one with a rolling update or a second replica. There, a note that the 033
build re-embeds after the 034 build stamped it keeps legacy rows (NULL
locator, never reused) until its next edit or the next full-hash backstop
(D3a). A note that is still unstamped is simply re-selected.

## D5: Migration 034

The five columns, all nullable, with no default, no backfill and no index:

| table | column | type | note |
|---|---|---|---|
| `note_embeddings` | `heading_path` | `text[]` | `{}` for section 0 |
| `note_embeddings` | `section_ordinal` | `integer` | `0` for section 0; integer because a 10 MiB note can hold more than 32,767 headings |
| `note_embeddings` | `embed_prefix` | `text` | at most 300 characters, enforced by the app |
| `notes_metadata` | `embedded_chunking_version` | `smallint` | NULL means pre-034 |
| `notes_metadata` | `embedded_input_digest` | `varchar(64)` | NULL means pre-034, or never certified since (D3a) |

- `ck_note_embeddings_heading_meta`: all three row columns NULL, or all three
  non-NULL with `section_ordinal >= 0` and `(section_ordinal = 0) =
  (cardinality(heading_path) = 0)`. It is added `NOT VALID` and then
  `VALIDATE`d, so the scan holds only `SHARE UPDATE EXCLUSIVE`.
  `ck_notes_metadata_embedded_chunking_version`: NULL or `>= 1`.
- Each column carries a marker comment byte-identical to the ORM's (`… (034_heading_chunks)`).
  On a re-run, a same-named column must have the exact type, nullability, no
  default and the marker, and a same-named constraint the exact definition.
  Anything else is refused by name (the 026/030 shape). `search_path` is pinned
  and asserted; `lock_timeout` / `statement_timeout` are set and `RESET`.
- It needs only table ownership (ALTER TABLE ADD COLUMN / ADD CONSTRAINT /
  COMMENT), so `test_nonsuperuser_migrations_pg.py` passes unchanged.
- **Downgrade**: in one transaction, take `pg_advisory_xact_lock(8029183045093649969)`
  (the generation key, written literally), set `embedded_content_hash = NULL`
  on every note that holds a row with `embed_prefix IS NOT NULL`, delete those
  rows, then drop the marked constraints and columns. The 033 build reuses on
  exact `chunk_text` alone, so left in place, those vectors (of prefixed input)
  would be reused as if unprefixed. Deleting them costs semantic search those
  notes until the old build re-embeds them (L7). It is derived data and is
  rebuilt.
- Both tables are already **derived** for backups (#348). The classification
  does not change.
- `alembic check` is clean after upgrade and after a downgrade/upgrade round
  trip. The schema gate's head literal becomes `034`, with cases for the fresh
  shape, the chain from 033 with rows (untouched, all NULL), the stamp-back
  re-run, an impostor of each unit, the CHECKs through `pg_constraint`, the
  downgrade's delete-and-uncertify, and the round trip.

## D6: Rendering the locator

The service result for each vector row gains `section: {"path": [...],
"ordinal": n} | None`. It is `None` when the row is stale or when its
`heading_path` is NULL (pre-034). Both statements project `heading_path` and
`section_ordinal`. Neither projects `embed_prefix`, and neither projects
`embedding` (search-quality's projection rule).

On the result line, after the similarity and the degradation suffix:

- ordinal ≥ 1: ` · § "Parent/Child" (#7)`. The quoted part is
  `change_feed.quote("/".join(path))`: one JSON string literal with C0/C1
  controls, U+2028/U+2029 and DEL escaped. A note cannot close the quote, so the
  `(#7)` after it is server-authored and cannot be forged, and a forged `(#3)`
  inside a heading stays inside the quotes. If the joined path is longer than
  160 characters, outer components are dropped and the string starts `…/`, so
  the innermost heading is kept.
- ordinal 0: ` · § (top of note)`. The follow-up is `read_note(path)`, which
  starts there.
- None: nothing, which is exactly today's line.

**A stale row prints no locator**, for the reason its preview is withheld:
`#N` is positional, and the stored ordinal describes content the index knows
is superseded. A fresh row edited on disk but not yet scanned keeps its locator
(the declared staleness bound, L3). `read_note(section=…)` returns the `heading`
line, so an agent can check it.

**Previews go on one line in both tools.** Today `semantic_search` prints
`chunk[:200]` raw, so a chunk holding `\n- **Forged** (`x.md`) — similarity:
0.999` prints a line identical to a server row. `find_related` replaces `\n`
only. The service now passes the **uncut** `chunk_text` (it used to cut at 500
before rendering). One helper, `_preview_line(text, 200)`, then:
1. collapses every maximal run of `str.isspace()` characters (CR, LF, tab,
   U+2028 and U+2029 included) to one space;
2. trims leading and trailing spaces;
3. escapes the remaining C0 controls, DEL and C1 controls as `\uXXXX`;
4. cuts at 200 characters.

Each tool keeps its existing ellipsis. Trimming is a stated change. A chunk of
600 spaces followed by `answer` previews as `answer` (Codex r1 m1). The helper
is not `_one_line`, which escapes nothing.

Each row grows by at most about 185 characters. At the 50-row maximum that is
under 10k characters, inside the response bounds.

**The docstrings** of `semantic_search` and `find_related` state the locator,
its two forms, that it is omitted on stale and pre-034 rows, and that the
follow-up is `read_note(path, section="#N")`. The two describe it in the same
words, as peers, neither ranked above the other. Both stay within the
4,000-character wire cap (`tests/test_issue_343_tool_metadata.py`).

## D7: Provider `kind` and `EMBEDDING_QUERY_INSTRUCTION`

- `EmbeddingKind = Literal["query", "document"]`. `get_embedding(text, *, kind)`,
  `get_embeddings_batch(texts, *, kind)`, and the `EmbeddingProvider` protocol's
  `embed_one` / `embed_batch` take it as a **required keyword**, with no
  default, so every call site must choose. An AST sweep test fails on any call
  without it. The call sites are: `embed_note` → `document`; `semantic_search`
  → `query` (and the harness's `semantic` arm calls `semantic_search`); the
  prewarm in `indexer.py` and the startup warm-up in `main.py` → `query` (they
  warm the path a search takes). `find_related` averages stored vectors and
  embeds nothing.
- **The instruction is applied in one place**, the shared `get_embedding` /
  `get_embeddings_batch` layer, only for `kind="query"`: `input =
  settings.embedding_query_instruction + text`, verbatim, with no separator
  added, because the operator writes the model's own template. Providers
  receive `kind` so a future backend with a native input type can map it.
  Ollama and OpenAI accept no such field for these models and ignore it.
- **Setting**: `embedding_query_instruction: str = ""`, at most 1,000
  characters, and refused at load if it contains NUL or a C0 control other than
  `\n` and `\t`. In `.env`, a double-quoted value turns `\n` into a newline
  (python-dotenv). In Kubernetes it is a literal newline in the YAML.
  `MAX_SEARCH_QUERY_CHARS` still bounds the caller's query. A provider
  input-limit refusal is translated as today.
- **The fingerprint rule.** The stored, startup-compared `embedding_fingerprint()`
  does **not** include the instruction. Every stored vector is a document
  vector, embedded without it, so an instruction change invalidates nothing,
  must not refuse startup and must not re-embed. A new non-stored
  `retrieval_fingerprint()` in `index_state.py` is canonical JSON of the
  embedding fingerprint's fields plus `chunking_version` and
  `query_instruction_sha256` (the first 16 hex digits of SHA-256 over the
  instruction's UTF-8, or `null` when it is empty). It is never written to
  `indexer_state` and never compared. It is logged once at startup (`Embedding
  retrieval: …`, the digest only, never the text) and recorded by the harness.
  So two deployments that differ only in the instruction share a stored
  fingerprint and have different retrieval fingerprints.
- **A model switch (bge-m3 → Qwen3) still changes the stored fingerprint**, and
  takes the existing refuse-then-reset runbook. That is correct, because it is a
  new vector space. The README default is not changed here.

## D8: The evaluation gate and the operator recipe

The CLAUDE.md eval row applies. The baseline needs the incumbent code on a
pre-034 copy, and the candidate needs the new code on the same copy after a
re-embed, so the comparison uses the harness's `runfile:` arms (#356 named this
route for #355 and #367):

1. `make search-eval-export USER_ID=<id>`, then `BACKUP_FULL=1 make db-backup`,
   restored into a scratch pgvector container (DEPLOYMENT.md steps 1–3).
2. `make search-eval-snapshot` on the copy, before anything is applied.
3. **Baseline generation**, from a checkout of the incumbent release (the last
   tag before this change): `make search-eval … ARMS="keyword semantic"`. Keep
   that generation's staged `semantic` arm file.
4. **Upgrade and re-embed the copy** with the candidate image as a one-off
   container. `DATABASE_URL` is the scratch copy, the vault is mounted
   **read-only** at the path the copy's `users.vault_path` names, no port is
   published, and the provider settings are production's. Its startup migrates
   to 034, and its indexer re-embeds. To finish in one sitting, the operator may
   set `EMBED_TIME_BUDGET_SECONDS_PER_USER=0` and
   `EMBED_CHUNK_BUDGET_PER_USER=0` on that container only. Wait until the
   backlog predicate counts nothing, `SELECT count(*) FROM notes_metadata
   WHERE embedded_content_hash IS DISTINCT FROM content_hash OR
   embedded_chunking_version IS DISTINCT FROM 1` is 0, then stop it. A residue
   that does not drain is notes whose bytes changed since the backup. Report
   it, and do not chase it. Record `SELECT count(*) FROM note_embeddings` and
   the per-pass durations from `indexer_runs`. That measurement is the
   production estimate.
5. **Candidate generation**, from the candidate checkout:
   `make search-eval … ARMS="keyword semantic runfile:<baseline semantic file>"
   BASELINE=runfile:<…>`. The report records each semantic arm's retrieval
   fingerprint and the copy's legacy-chunked note count. That count must be 0
   for the report to be evidence.
6. Attach `report.json` only if `attachable: true`. `CORPUS_LOSS` invalidates
   it.
7. **#367 A/B (operator work, optional, after this change merges):** a second
   copy, re-embedded with `EMBEDDING_MODEL=qwen3-embedding:0.6b` and
   `EMBEDDING_QUERY_INSTRUCTION=…` (the fingerprint refuses, so run `make
   reset-embeddings` against the copy first), scored the same way. Whether the
   README default changes is decided on that report, in a later change.

**Harness changes are small.** A DB-backed arm whose membership includes
`semantic` records `retrieval_fingerprint()` and the copy's legacy-chunked note
count (one read-only `SELECT`, `null` when the column does not exist) in its
staged `meta` and in its `report.json` arm entry. A `runfile:` arm carries what
its meta holds, or `null` from an older harness. Fixture arms are `metadata`
membership and gain no key, so the committed golden fixture is unchanged. The
fields hold no query text and no path.

## Rollout: what the operator sees

- **Deploy.** The migration is metadata-only plus one CHECK validation over
  about 17k–40k rows, which takes seconds. Startup does not refuse: the
  fingerprint is unchanged. The startup log gains the `Embedding retrieval:`
  line.
- **Re-embed estimate.** Today there are about 16.7k chunks. Splitting at
  headings adds at least one chunk boundary per non-empty section. On
  heading-dense agent-written notes, expect **about 1.3× to 2.5× as many**,
  **22k to 42k chunks**, each with up to 75 tokens of prefix. At about 6 to 10
  chunks/s batched on the GPU host, that is **40 to 115 minutes of provider
  time**. The version-only segment is held to 300 s of embedding per tenant per
  tick, with ticks spaced by `INDEX_INTERVAL_SECONDS` (300 s), so wall clock is
  about **1.5 to 4 hours**. Step 4 of the recipe replaces this band with a
  measurement before the production deploy.
- **During it.** `semantic_search` and `find_related` serve every note
  throughout. A note not yet re-embedded answers from its legacy vectors, with
  its preview and no `§`. A re-embedded note answers from heading-era vectors
  with `§`. Nothing is stale on account of the transition. Ranking is a mix of
  the two (L1), so every response carries the `index_transition: N …` line
  until the caller's scope has drained, and `/health` shows
  `indexer.rechunk: "in_progress"`. The dashboard's pending count starts near the note count and
  falls each pass. Each budget stop writes one WARNING per tenant per pass and
  no run error. `/health` stays `ok` unless the provider fails. Query embeddings
  share Ollama with the batches, so a search may wait up to one batch, about
  2–3 s (L9).
- **Rollback.** Roll forward if at all possible. Otherwise run `alembic
  downgrade 033` with the new image (which deletes heading-era rows and
  un-certifies their notes, D5), then deploy the old image. The old build
  re-embeds those notes.

## Risks / Trade-offs

- **Re-embed cost and provider load.** It is the one-off price of the change,
  bounded per tick by the budget and measured on the copy first. If it is
  unacceptable at that point, pause from the panel. The backlog resumes where
  it stopped.
- **Mixed ranking during the transition** (L1). It can change which notes fit
  on a bounded page. That is declared on every response by the
  `index_transition:` line, and it is bounded by the re-embed duration. The
  harness measures the end state, not the transition.
- **More, smaller chunks.** The halfvec HNSW index grows proportionally (45 MB
  today, about 60–110 MB). The `hnsw.max_scan_tuples` (20,000) note in
  search.md becomes relevant above 20k chunks. Filtered recall is re-checked by
  the existing `tests/integration/test_search_recall.py` benchmark after
  re-embed (task 10.4), and the GUC is the documented knob if it drops.
- **Heading text is note content** (L5). It is quoted and capped, but the words
  can still mislead. The ordinal is authoritative and `read_note` returns the
  heading for checking.
- **A wrong query instruction silently degrades queries** (L8). The harness is
  the control. The default is empty.

## Owner decisions (taken by default, 2026-10-30)

1. The chunking version is a **per-note stamp** with a backlog, not a field of
   the refusing fingerprint (D4). The brief recommended the fingerprint. That
   route crash-loops deploy-on-merge and empties semantic search for hours.
2. **Tags are in the prefix** (D2), accepting L4.
3. **Heading lines are excluded from chunk text**, with the zero-chunk
   fallback for heading-only notes (D1).
4. **No sub-ordinal column.** Several chunks share `(heading_path, ordinal)`.
5. **The locator is withheld on stale rows**, and previews in both tools go on
   one line (D6).
6. **The instruction is outside the stored fingerprint** but inside a logged,
   harness-recorded retrieval fingerprint (D7).
7. **The version-only backlog is budgeted in every mode**, single-user
   included (D4).
8. **No prefix on/off setting** (D2). The README default model is unchanged.

## Codex spec review, round 1 (2026-10-30)

FAIL on d2503a4, with 2 BLOCKER, 2 MAJOR, 1 MINOR and 1 NIT. All six were
accepted and folded in.

| # | Severity | Finding | Resolution |
|---|---|---|---|
| B1 | BLOCKER | A mixed-generation bounded page can silently omit a note that was reachable before deploy, because prefixed vectors move distances. The claim that mixing was correctness-safe was false, and index-integrity promised "the same notes". | The gradual re-embed is kept (a wipe would leave semantic search dark for hours). The safety claim is removed. A transition contract is added: the owner-scoped `index_transition: N …` line, read from the same snapshot, appears on every `semantic_search` / `find_related` response while N > 0, empty path included. `/health` gets an `indexer.rechunk_pending` count. A `limit=1` two-pass membership scenario and test are added (task 7.1a). L1 is restated as the residual. |
| B2 | BLOCKER | `legacy_chunked_notes` was unscoped, so an attachable `USER_ID=` report could disclose another tenant's state. | The count is scoped to the arm's provenance owner, and covers all owners only for a `"multi"`, non-attachable report. A two-tenant scenario and test are added (task 8.2). |
| M1 | MAJOR | Tags and title are now in the provider input, so identical cleaned text no longer proves a vector current. A tag-grammar revision would leave stale prefixes in place permanently. | D3a adds `embedded_input_digest` (migration 034), clears certification in the same statement on a changed title or tags, MODIFIES the index-integrity grammar requirement, and adds a full-hash backstop that recomputes the digest. |
| M2 | MAJOR | The follow-up scenario required raw-body containment, which a chunk spanning a removed fence cannot meet. | The scenario now uses fence-cleaned containment, the same contract as index-integrity and task 7.4, and still requires the ordinal to resolve to the correct raw section. |
| m1 | MINOR | `_one_line` trims, contradicting "no other change", and the service cut at 500 before normalising. | The service passes the uncut chunk. The normalisation is defined exactly (collapse, trim, escape C0/C1/DEL, cut at 200), and the requirement and the `_preview_line` helper match. |
| n1 | NIT | The repository's k3s bundle is `Recreate` with one replica and migrates in an init container, so it has no overlap. | L2 and the D4 note are rewritten so the window applies only to externally modified deployments. |

Declined: none.

## Codex spec review, round 2 (2026-10-30)

FAIL on 59d6ece. All six round-1 fixes were VERIFIED (B1, B2, M1, M2, m1, n1).
Two new BLOCKERs were found, both accepted and folded in.

| # | Severity | Finding | Resolution |
|---|---|---|---|
| B1 | BLOCKER | `/health` is unauthenticated, and `indexer.rechunk_pending` was an exact, tenant-derived count. | The number is removed. `/health` carries only `indexer.rechunk: "in_progress" \| "complete"`, which is process-wide and non-volumetric. It never affects `status` or the HTTP code. Its privacy contract is documented beside the "counts only, no path/error/user id" rule (task 7.1a). Exact counts stay on the authenticated panel and in the owner-scoped `index_transition:` line. |
| B2 | BLOCKER | The marker used `NULL or < CHUNKING_VERSION`, so after a Recreate rollback to a lower version, notes stamped higher read as complete while the backlog (`IS DISTINCT FROM`) still re-selected them. | One owner-scoped predicate everywhere: `embedded_chunking_version IS DISTINCT FROM :CHUNKING_VERSION`, for the marker, backlog, dashboard, progress endpoint and health state. Added the higher-stamp rollback scenario (version 2 re-embeds B and C, a rollback to 1 keeps the marker until every note matches, and the backlog re-selects B and C) and a grep test against `<`/`>` comparisons (tasks 5.1, 7.1a). |

Declined: none.

Amendment (after the implementation review, 2026-10-30): B2's single
predicate is kept for the backlog, dashboard and progress endpoint, and
narrowed for the marker and `/health` to notes that hold vectors of another
generation (see D4). `IS DISTINCT FROM` stays on every surface.

## Codex spec review, round 3 (2026-10-30)

PASS — both round-2 fixes VERIFIED, no findings. B1 (`/health` exposes only the
process-wide, non-volumetric `indexer.rechunk`, never a number) and B2 (one
`IS DISTINCT FROM :CHUNKING_VERSION` predicate on every surface, with the
higher-stamp rollback scenario) close their failing inputs and do not regress the
round-1 fixes. Strict OpenSpec validation also passes.

## Codex implementation review, round 1 (2026-10-30)

FAIL on 8c89da6, with 1 MAJOR and 2 MINOR, all accepted and fixed; the
`openspec-verifier` pass found 0 blocking and 8 minor items, all folded in.

| # | Severity | Finding | Resolution |
|---|---|---|---|
| M1 | MAJOR | `semantic_search` interpolated the raw query into its header, so a multiline query displaced the `index_transition:` line or forged one above it. | Query and source path are echoed through `_header_scalar` (one escaped line); tests with multiline and control-bearing queries assert the marker is the only one and directly after the header. |
| m1 | MINOR | The render tests never called `find_related_impl`. | Tests for non-empty and exact-fallback-empty results, named-owner scoping, locator rendering, transition placement and one-line previews. |
| m2 | MINOR | search.md's projection table omitted the locator columns and called the two vector projections one shape. | Both shapes listed separately, `heading_path` / `section_ordinal` included, `embed_prefix` named as never projected. |

Verifier items folded in: the marker / `/health` predicate narrowed to notes
holding vectors (D4 amendment, two new scenarios); a setext test; NULL vs `{}`
tags in the upsert; excluded and empty legacy notes converging without a
provider call; `semantic_search` refusing a weaker open transaction, with a
PostgreSQL test of the isolation in the tool path; the `_provenance_owner`
reading; L11; bounded timeouts in the 034 downgrade.

Declined: none.

## Codex implementation review, round 2 (2026-10-30)

PASS on f2edbf9: all three round-1 findings (M1, m1, m2) VERIFIED, no BLOCKER
or MAJOR in the full adversarial pass, including the hybrid integration and
the migration interleavings. One MINOR, fixed: `search.md`'s hybrid
accepted-limitations paragraph still said the semantic chunk preview is not
one-lined; it now says `_preview_line` normalises it in all three vector
tools (closing #385), keeping only the raw path/title clause, and the
archived hybrid design's L9 records that #355 closes the preview half.

## Implementation notes

Choices the spec left open, one line each.

- `_scan_headings` and `_LINE_BREAK_RE` are imported from `vault.py` as they are (locally, inside `_note_sections`), not renamed.
- An empty section after the cap is reached does not mark the note truncated; only a non-empty one does (one chunk past the cap, as `chunk_text_bounded`).
- A heading-path component is cut to 199 characters plus `…` (200 total); prefix components to 79 plus `…`.
- `input_digest` is SHA-256 over `json.dumps(inputs, ensure_ascii=True, separators=(",", ":"))`.
- `certify_embedded` takes the inputs as a required keyword, `provider_inputs`; the excluded and certified-empty branches pass `[]`.
- The upsert's title/tags invalidation is a `CASE` in `on_conflict_do_update`, so it applies to every re-upsert (content-changed rows are stale anyway).
- The full-hash backstop keeps every body on that pass (`keep_bodies=backstop`, a re-derive's memory shape once per interval) and re-checks rows certified under the current version, both unchanged rows and same-hash re-upserts, after the upsert; it compares with `= :cv`, which is not one of the five transition surfaces.
- The predicate helpers (`vectors_not_current_sql/_expr`, `not_rechunked_sql/_expr`) live in `embeddings.py` beside `CHUNKING_VERSION` and inline the version as a literal read at call time.
- `EmbedBudget.enter_version_only()` starts a fresh allowance (clock and chunk counter) at the first version-only note of a single-scope pass; `leave_version_only()` restores the unbudgeted state before the exclusion sweep.
- `/health`'s rechunk state is observed by one owner-scoped `count(*)` under the marker's predicate (`legacy_vectors_sql`) at the end of each embed stage (after the sweep); a failed count keeps the previous observation.
- `semantic_search` issues `SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY` on a session with no open transaction, and otherwise reads `transaction_isolation` / `transaction_read_only` and raises unless they are `repeatable read` / `on`; `find_related_impl` issues it first; the tools then call `rechunk_pending_count` on the same session.
- The transition line goes on the line after the header (the header's trailing blank line moves below it); on the empty paths it is the second line.
- `_preview_line` maps each `str.isspace()` character to a space and collapses runs, then escapes `[\x00-\x1f\x7f-\x9f]`; U+0085 is whitespace in Python and so collapses rather than escapes.
- The locator's 160-character cut drops whole outer components behind `…/`; a single innermost component still over 160 is itself cut with `…`.
- `EMBEDDING_QUERY_INSTRUCTION` refuses C0 controls other than LF/TAB (as specified); DEL and C1 are accepted.
- `_provenance_owner`: the harness owner for `legacy_chunked_notes` is read from the `user_id` of a single-user report's queries (the miner guarantees one owner, NULL included); a single-user report with no queries records `null`; a `"multi"` report counts all owners. The search-evaluation delta says the same.
- Header echoes of caller text (the semantic query, the `find_related` source path, also in the stale-source line) go through `_header_scalar` — the inside of `change_feed.quote`'s JSON literal — keeping the existing quote marks; the argument caps bound the length.
- Verifier reading (b): the full-hash backstop clears a note whose exclusion pattern changed (same `fnmatch` as the embed pass), and the backlog repairs it before the sweep; there is no oscillation, because the repaired note's digest then matches.
- Verifier reading (c): the #150 `Plain.md` fixture now seeds `[]` tags; under the MODIFIED grammar requirement a tag difference alone invalidates, and `[]` is what extraction yields.
- Verifier reading (d): L4 replaces #281's "a metadata-only edit makes no provider call" for tag and title edits; other frontmatter edits are still free.
- Verifier reading (g): `_preview_line` collapses U+0085 and U+001C–U+001F as whitespace (they are `str.isspace()`); "expressed once" means each predicate is a paired SQL text and SQLAlchemy expression side by side in `embeddings.py`; containment holds because a chunk ends at the next heading of any depth, while `read_note`'s section runs further.
- The startup line is `Embedding retrieval: <canonical JSON>`, logged right after the embedding transport line.
- The upsert's tags comparison treats NULL and `{}` as equal (`COALESCE(tags, '{}')`), since both build the same prefix; otherwise a pre-existing NULL-tag row would be re-embedded by its first re-upsert.
- Because an excluded note's digest is of `[]`, the full-hash backstop also clears the certification of a note whose exclusion pattern changed (in either direction); the backlog then repairs it, ahead of the exclusion sweep.
- Task 7.5 (downgrade on real data) is covered by the schema-gate case `test_downgrade_034_deletes_prefixed_rows_and_uncertifies_their_notes` (mixed legacy and prefixed rows, downgrade, upgrade, `alembic check`); task 7.3's reuse cases are in `test_heading_chunks_pg.py`, and `test_perf_chunk_reuse_pg.py` was updated to the prefixed input rather than extended.

## Accepted limitations

- **L1** — During the transition, legacy and heading-era vectors of the same
  model coexist. A note's rank, and so whether it fits on a bounded page,
  depends on whether it has been re-embedded yet. The page is never presented
  as complete: the `index_transition:` line marks every response until the
  caller's scope drains. The mixed ranking itself is the residual, bounded by
  the re-embed duration.
- **L2** — Only in a deployment modified outside the repository (a rolling
  update or a second replica) can the 033 build write after 034 migrated. A
  note it re-embeds after the 034 build stamped it keeps legacy rows until its
  next edit or the next full-hash backstop. The repository's Recreate
  Kubernetes bundle and compose deploys have no such window.
- **L3** — An edit not yet scanned leaves a fresh-looking row whose `#N` may
  name a different section, which is the existing staleness bound.
  `read_note`'s `heading` field lets the agent check.
- **L4** — A tag edit, or a title change by rename or frontmatter, re-embeds
  the whole note, because every prefix changes. This undoes #281's reuse for
  metadata edits.
- **L5** — The `§` path quotes note-controlled heading text. It cannot forge
  the ordinal, but its words are the author's.
- **L6** — Only ATX headings are boundaries, as in section addressing. Setext
  headings are body text.
- **L7** — A downgrade to 033 deletes heading-era vectors. Those notes are
  absent from semantic search until the old build re-embeds them.
- **L8** — The query instruction is not checked against the model.
- **L9** — Searches during the re-embed may wait up to one embedding batch on a
  shared Ollama.
- **L10** — The heading-path components and the prefix are cut at fixed
  lengths, so a very long heading is embedded and shown truncated, marked `…`.
- **L11** — The full-hash backstop keeps every note body in memory for the
  pass, once per `INDEX_FULL_HASH_INTERVAL_HOURS` and at startup: the same
  memory profile as a re-derive.
