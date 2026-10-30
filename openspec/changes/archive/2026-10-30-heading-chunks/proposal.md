## Why

GitHub #355, plus the embedding-provider half of #367. `chunk_text` cuts a note's
cleaned body into fixed 512-token character windows. It ignores headings, and it
embeds no title, heading or tag context. `note_embeddings` records no heading and
no section ordinal for a chunk. So a semantic preview can start mid-word, and after
a hit the agent must read the whole note, or guess a heading, before it can narrow
with `read_note(section=…)`, even though `read_note` and `edit_note` already
address a section by path and by ordinal. The research behind #355 found that
structure-based chunking is the best of the cheap strategies, that overlap does not
help, and that a contextual prefix moves retrieval quality. A deterministic
`Title > H2 > H3` breadcrumb is the version that needs no LLM.

#367 adds the second half. Qwen3-Embedding-0.6B (and other instruction-tuned
embedders) expect a task instruction on **queries only**, and the provider
abstraction cannot tell a query from a document today. Both changes alter what is
embedded, so each forces a full re-embed. They ship together, and the vault is
re-embedded once.

The opt-in reranker of #367 (`RERANK_URL`, `rerank: bool`) is **not** in this
change. It is a separate change after this one.

## What Changes

- **Heading-aware chunking.** The indexer splits a note's body at ATX headings
  with the function section addressing uses (`_scan_headings` over the
  fence-masked body), so a `#` inside a code fence is never a boundary, and the
  ordinal a chunk records is the ordinal `read_note(section="#N")` resolves for the
  same bytes. Each heading's own text (up to the next heading of any depth) is one
  section, and the text before the first heading is section 0. Each section is
  cleaned with the whole-body fence spans and windowed by today's
  `chunk_text_bounded`, with `CHUNK_SIZE` and overlap 0. Heading lines are not part
  of the stored chunk text. An empty section yields no chunk. A long section yields
  several chunks that share its heading path and ordinal. A note with no headings
  is one section and chunks exactly as today. A note whose sections are all empty
  but whose cleaned body is not falls back to today's whole-body chunking, so a
  note has zero chunks exactly when it has zero chunks today. `MAX_CHUNKS_PER_NOTE`
  still counts across the whole note in document order.
- **Context prefix.** The text sent to the provider is a prefix plus the chunk:
  `{title} > {heading} > … > {heading}`, then a `tags:` line when the whole prefix
  stays within 300 characters, then a blank line, then the chunk. The stored
  `chunk_text` stays un-prefixed for previews. The prefix is stored per row
  (`embed_prefix`).
- **Reuse keyed on the exact embedded input.** A stored vector is reused only when
  the stored fingerprint matches (as today), the note's stored chunking version is
  the current `CHUNKING_VERSION`, and the row's `(embed_prefix, chunk_text)` equals
  the new chunk's, rechecked under the generation lock. Pre-034 rows have no prefix
  and are never reused.
- **The re-embed is a per-note chunking-version backlog, not a fingerprint
  reset.** `notes_metadata.embedded_chunking_version` is stamped by
  `certify_embedded` on every certifying branch. The embed backlog, the dashboard
  pending count and the progress poller also select a note whose stamp is not
  `CHUNKING_VERSION`. The stored embedding fingerprint is unchanged, so the deploy
  does not refuse at startup, needs no `make reset-embeddings`, and semantic search
  keeps serving the old vectors until each note's new rows commit. The model,
  and so the vector space, is unchanged, but mixed generations still change
  ranking. So while the caller's scope has notes on an older version, every
  vector-search response carries an `index_transition: N notes not yet
  re-chunked; …` line. `/health` reports only the non-volumetric `indexer.rechunk:
  in_progress | complete`. Every surface uses one rollback-safe predicate,
  `IS DISTINCT FROM CHUNKING_VERSION` (D4).
  Version-only notes
  are taken after content-changed notes and are always held to the per-tenant embed
  budget, so the re-embed runs in the background over several passes.
- **Migration 034** (`down_revision "033"`): `note_embeddings.heading_path text[]`,
  `section_ordinal integer`, `embed_prefix text` (all nullable, NULL for pre-034
  rows, with an all-or-none CHECK), plus `notes_metadata.embedded_chunking_version
  smallint` and `embedded_input_digest varchar(64)` (both nullable). The
  certification covers a digest of the exact provider input. A changed title or
  tags clears certification in the same statement, and the full-hash pass
  recomputes the digest as a backstop (D3a). All are marked, reconciled rather than adopted, with no
  backfill and no index. The downgrade deletes heading-era vector rows and
  un-certifies their notes before dropping the marked units.
- **Rendering.** Every fresh `semantic_search` and `find_related` row prints
  `§ "Parent/Child" (#7)` on its result line, or `§ (top of note)` for section 0.
  The path is a JSON-quoted string literal, so note text cannot forge the
  server-authored ordinal after it. A stale row prints no locator (its ordinal
  describes superseded content), and neither does a pre-034 row. Previews in
  both tools are normalised from the uncut chunk: whitespace is collapsed and
  trimmed, controls are escaped, and the result is cut at 200. The docstrings name
  `read_note(path, section="#N")` as the follow-up.
- **Provider `kind`.** `get_embedding` / `get_embeddings_batch` and the provider
  protocol take a required keyword `kind: "query" | "document"`. The indexer passes
  `document`. `semantic_search`, the warm-ups and therefore the harness `semantic`
  arm pass `query`. `find_related` embeds nothing.
- **`EMBEDDING_QUERY_INSTRUCTION`** (default empty, so bge-m3 is unchanged).
  When it is set, it is prepended verbatim to query inputs only, never to
  documents. It is **not** part of the stored, startup-compared embedding
  fingerprint, because no stored vector depends on it, so changing it neither
  refuses startup nor re-embeds anything. A non-stored **retrieval fingerprint**
  (the embedding fingerprint fields, `chunking_version`, and a digest of the
  instruction) is logged at startup and recorded per semantic arm by the harness,
  so two deployments that differ only in the instruction are labelled apart.
- **Harness.** Semantic-membership arms record the retrieval fingerprint and the
  replay copy's count of notes still on a legacy chunking version. DEPLOYMENT.md
  gains the operator recipe for the before/after comparison on a re-embedded
  restored copy. The bge-m3 vs Qwen3 A/B is operator work on that harness (#356).
  **The README's default model does not change in this change.**

No **BREAKING** tool-contract change: the parameters are unchanged. Result lines
gain a `§` segment, and previews lose their embedded line breaks.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `index-integrity`: heading-aware chunking and its ordinal parity with section
  addressing, the context prefix, the reuse key (MODIFIED), the
  chunking-version backlog with its transition marker and health count, the
  input-digest certification, and the grammar-invalidation rule (MODIFIED).
- `embedding-providers`: query/document `kind`, `EMBEDDING_QUERY_INSTRUCTION`,
  the retrieval fingerprint, and the rule that the chunking version and the
  instruction are outside the stored fingerprint.
- `search-quality`: the section locator on vector rows, the one-line preview, the
  stale-row withholding of the locator, and the two docstrings.
- `schema-integrity`: migration 034.
- `panel-ops-health`: the pending count's "not current" includes a stale chunking
  version (MODIFIED).
- `search-evaluation`: arm labelling with the retrieval fingerprint and the legacy
  chunking count. The real-data report requirement applies as it stands.

## Impact

- **Schema**: `alembic/versions/034_heading_chunks.py`; `src/models/db.py`
  (`NoteEmbedding`, `NoteMetadata`, markers).
- **Code**: `src/services/embeddings.py` (sectioner, prefix, `CHUNKING_VERSION`,
  `kind`, the instruction, `certify_embedded`'s stamp, reuse lookup and under-lock
  recheck, `semantic_search` projection); `src/services/indexer.py` (backlog
  predicate and ordering, version-only budget, sweep probe, prewarm `kind`);
  `src/services/index_state.py` (`retrieval_fingerprint()`); `src/main.py`
  (warm-up `kind`, startup line); `src/config.py` (one setting);
  `src/control_panel/routes.py` (`_vectors_not_current`);
  `src/mcp_server/tools.py` (both renderers, `find_related_stmt` projection);
  `src/mcp_server/server.py` (two docstrings); `scripts/search_eval/`
  (arm metadata).
- **Tests**: offline chunker tests (fences, CRLF and lone CR, frontmatter, empty
  and long sections, no headings, heading-only fallback, cap, ordinal parity with
  `read_note`), prefix and reuse tests, render and forgery tests, a `kind`
  call-site sweep, settings tests, PostgreSQL integration tests (backlog,
  transition with mixed rows, reuse recheck, downgrade), and schema-gate cases for
  034.
- **Docs, in the same PR**: README (setting, Qwen3 example, result format),
  `.env.example`, DEPLOYMENT.md (034 upgrade note, what the re-embed looks like,
  the eval recipe), `docs/architecture/indexing-and-embeddings.md`, `search.md`,
  `vault-tools.md`, `schema-and-migrations.md`, CLAUDE.md (key decision bullet).
- **Not touched**: the stored fingerprint format (`v` stays 1), extraction version
  (stays 4), the halfvec index, panel templates (so no browser pass), the rate
  buckets, `SERVER_INSTRUCTIONS`, the reranker.
- **Gates**: search path, chunking and embedding change, so the adversarial Codex
  pass is mandatory; real-data `make search-eval` report attached if attachable;
  `make test-schema`; `alembic check` clean.
