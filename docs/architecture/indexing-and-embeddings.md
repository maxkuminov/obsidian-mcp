# Indexing and embeddings

> Deep rationale extracted from `CLAUDE.md`. Read before touching the indexer, the embed pass, the tsvector writers, or the panel actions that pause them.

## Embedding providers
- `EMBEDDING_PROVIDER=ollama` (default) — uses `OLLAMA_URL` and
  `EMBEDDING_MODEL`; serial single-input HTTP per chunk.
- `EMBEDDING_PROVIDER=openai` — requires `OPENAI_API_KEY` (validated at
  startup). Uses `OPENAI_BASE_URL` (default `https://api.openai.com/v1`)
  and `OPENAI_EMBEDDING_MODEL` (default `text-embedding-3-small`). Native
  batching: up to 96 inputs per `/v1/embeddings` POST, with sub-batching
  for larger lists. Retries 429/5xx with exponential backoff, max 3
  attempts.
- `EMBEDDING_DIMENSIONS` (default 1024) controls both the pgvector column
  width and the `dimensions` param on OpenAI requests.
- Reset workflow: `make reset-embeddings` (or **Settings → Danger zone →
  Reset embeddings** in the panel) drops & recreates `note_embeddings.embedding`
  at the configured dim and clears every `embedded_content_hash`. The next
  indexer pass re-embeds the vault.
- Dimension-mismatch guard: lifespan startup queries `pg_attribute` for
  the live column dim and `sys.exit(1)`s if it disagrees with
  `EMBEDDING_DIMENSIONS`, with a log message pointing to
  `make reset-embeddings`.
- pgvector version guard (`_check_pgvector_version`, next to the dim guard in
  `src/main.py`): reads `pg_extension.extversion` for `vector` and
  `sys.exit(1)`s below **0.8.0**, naming `hnsw.iterative_scan`. Skipped in
  sandbox mode and when the extension is not installed yet (alembic's job).
  See "Filtered vector search" in [search.md](search.md) for why an older
  backend fails *silently* without it.

## Indexing decisions

- Embeddings: pluggable provider, `EmbeddingProvider` Protocol with two
  implementations (Ollama, OpenAI). Single `EMBEDDING_PROVIDER` env var
  picks the backend; `get_provider()` is a cached singleton. Default is
  Ollama bge-m3 at 1024 dim, 512 token chunks, no overlap.
- **Each embed pass ends with an exclusion reconciliation sweep** (#127). The
  backlog selects on `embedded_content_hash IS NULL OR != content_hash`, so it
  is driven entirely by *content* changes and an `EMBEDDING_EXCLUDE_PATTERNS`
  edit reached nothing: adding a pattern left an excluded note answering
  `semantic_search` for ever, removing one left a now-included note stamped
  with zero vectors, hash-equal, never re-selected, silently absent. The sweep
  therefore reads the rows the backlog *cannot* see — certification-current
  (`embedded_content_hash IS NOT DISTINCT FROM content_hash`, owner-scoped) —
  and writes only where the config and the stored vectors disagree. **Every
  write goes through `certify_embedded`'s `id + content_hash + file_path`
  predicate, stamp before delete, per-note commit — never a delete by id**: a
  move changes `file_path` with an unchanged `content_hash`, so a decision
  about an excluded path would otherwise delete the vectors of a row that is
  now included and record it embedded with none. Convergence is defined for a
  *completed* sweep, with three declared exceptions — zero-chunk notes (already
  correct, and deliberately never rewritten, which is the one file read the
  sweep repeats per pass), bytes that no longer hash to the row (the backlog
  owns it next pass), and a failed provider call (left unstamped). A pause
  stops it between notes; the next pass runs a fresh, idempotent sweep.
- **Both move paths recompute the stem-derived `title`** (#127). It falls back
  to the filename stem, so a rename left `Alpha` on a note called `Beta.md`
  for ever — a move changes no content, so the scan never revisits the row.
  The indexer's id-preserving branch binds the title from the entry it already
  parsed for the new path; `move_note` reads the moved file through the
  destination target's descriptor after `_verify_the_moved_inode`, parses it
  with `parse_frontmatter` and derives through the same `_note_title`. **Not a
  SQL `CASE` over the stored JSONB**: that disagreed with `_note_title` on
  every falsy title (`false`, `0`, `[]`, `{}`, `""` all fall back to the stem)
  and trusted a copy that can be older than the file. The JSONB derivation
  survives only as the read/parse-failure fallback, declared best-effort.
- **The keyword vector attempts the full note and retreats per note** (#127).
  Both writers bound `content[:100000]`, so every term past that was invisible
  to `keyword_search` on a note the tool still reported. `write_tsvector_bounded`
  attempts the whole body, halving down to a floor of **exactly** 100,000
  characters — today's statement — with each attempt in its own savepoint and
  the `try` **outside** `async with session.begin_nested()`, so the error
  unwinds through the context manager's rollback and leaves the outer
  transaction usable. A floor failure propagates, exactly as before: the
  incremental pass aborts with nothing committed and retries next tick, and
  `rebuild_tsvectors` is now **atomic** — its every-500 intermediate commits
  are gone, so a floor failure rolls the whole rebuild back instead of leaving
  a keyword index half-built under two FTS configs that no periodic pass would
  repair. Verified against a real PostgreSQL
  (`tests/integration/test_tsvector_bounded_pg.py`); a mocked savepoint cannot
  show the driver's aborted-transaction state clearing.
- **The rebuild certifies what it writes, and the reason is that nothing else
  would ever repair it.** It snapshots the table once and then reads the vault
  note by note, while a keyword vector is rewritten again only when a note's
  `content_hash` changes — both move paths preserve `content_tsvector` and the
  scan skips an unchanged hash. So a row it steps over, or writes stale bytes
  into, stays on the *previous* `FTS_CONFIGS` for ever: `'running'` stored as
  the english stem `run`, never matching a `simple` query. Two shapes did
  exactly that — a note moved after the snapshot failed its old-path read and
  was a silent `continue`, and an UPDATE by `id` alone overwrote a concurrent
  pass's `tsvector(C2)` with `tsvector(C1)` while the hash stayed `C2`, so every
  later scan skipped it. The snapshot now retains owner, path and hash, the
  bytes are verified against that hash, and the UPDATE names all four and
  requires exactly one row. A zero-row update, a read failure or a hash mismatch
  is **never** routed through the halving retreat (that addresses size, not
  staleness): it re-reads the current owner-scoped row — gone → safely absent,
  moved or advanced → retried against the fresh values within
  `MAX_REBUILD_REREADS`, and still recording the same path and hash →
  `TsvectorRebuildAborted`, rolling the single transaction back rather than
  committing around it.
- **`OllamaProvider.embed_batch` has no aggregate deadline** (#127); the 30 s
  per-call `wait_for` is the only liveness bound. The old fixed 300 s
  whole-batch budget could fire only when every chunk was individually healthy
  — i.e. exactly on a note with more chunks than 300 s of normal latency
  covers, which then never certified and was re-selected every tick: a
  permanent 300 s burn under `index_pass_lock` that could never finish. A
  *proportional* replacement re-introduces the same boundary one size class up
  and was rejected. `OpenAIProvider` is untouched. The cost is a giant note
  holding the pass for 30 s × chunks once; the pause is honoured at the next
  note boundary, as always. `embed_note` still refuses to certify partial chunk
  coverage.
- Indexer runs on startup then every 5 minutes, hash-based change detection.
  Each periodic tick ends with `prewarm_search_caches()` **inside**
  `index_pass_lock`: one `get_embedding("warmup")` (Ollama only — a remote API
  has no warm state) and one HNSW probe with a deterministic non-zero unit
  vector, the whole thing under a single 15 s `asyncio.wait_for`. It exists
  because `semantic_search` is bimodal (≈0.47 s warm, ≈17.5 s cold: 14 s of
  Ollama reloading bge-m3, 3 s of HNSW pages missing from a 128 MB shared
  `shared_buffers`) and the median gap between calls has grown to ~28 min.
  It logs and swallows ordinary failures (the indexer's `consecutive_failures`
  must not react to it) but **re-raises `CancelledError`** so lifespan shutdown
  still stops the loop.
- **The dashboard's "Last run" is an in-process heartbeat, not
  `max(notes_metadata.indexed_at)`.** `indexer.last_index_run_at` /
  `last_index_run_ok` are stamped at the end of the startup pass and of every
  periodic tick (`False` in the `except Exception` branch; `CancelledError` is
  a `BaseException` so shutdown is not recorded as a failure). `indexed_at`
  only moves for notes a pass actually upserted or moved, so a pass over an
  unchanged vault writes it nowhere and a healthy indexer looked stalled for
  days on an idle vault — an invitation to reach for the Danger zone and
  re-embed the whole vault for nothing (#78). `max(indexed_at)` is still shown,
  under its own label "Last change detected". No migration and no per-tick
  write: it answers "is *this process's* loop alive", which is a property of
  the process, and it resets to `None` on restart until the startup pass lands.

## Re-deriving after a grammar change (#150)

`clean_for_embedding` no longer carries its own fence regexes. It consumes the
shared recognizer in `src/services/links.py` — the `code-masking` capability,
grammar written out in
[vault tools](vault-tools.md#the-fence-grammar-every-consumer-shares-150) — in
`BODY` context, because what it is handed is a note's post-frontmatter body.
Its private pair (LF-only, column-zero, exact-length closer) disagreed with the
masker heading resolution uses, so an indented or longer-closed block was
embedded as prose while the same block was invisible to `read_note(section=…)`.

**A grammar change moves no bytes, so `content_hash` cannot see it.** Links,
tags and vectors are all derived through that recognizer, and re-derivation is
gated on the hash of the note's bytes — which is unchanged. Ship a widened
grammar with nothing else and every unchanged note keeps answering from link
rows and vectors built under the old one, permanently: the scan skips it, the
embed backlog's `embedded_content_hash != content_hash` predicate is false, and
nothing else ever revisits it.

`notes_metadata.extraction_version` (SMALLINT NOT NULL DEFAULT 0, migration
018) is the marker, and `CURRENT_EXTRACTION_VERSION` in
`src/services/indexer.py` is what it is compared against. **Bump that constant
in the same commit as any change to the fence grammar.** A grammar change
without a bump is the silent-staleness bug above; a bump without a grammar
change costs one no-op pass.

- **A stale marker makes a note changed for the whole pass** — parsed,
  re-tagged, re-linked, re-tsvectored, upserted, and stamped. The stamp goes in
  the same statement as the state it certifies, inside the pass's one
  transaction, so a pass that dies part way through commits neither: a stale
  marker never survives beside re-derived rows, and a current marker never
  survives beside stale ones. Retry is simply the next tick.
- **Embedding invalidation is scoped, not blanket.** Re-embedding 2,577 notes
  to fix a grammar that affects a handful is the failure mode the marker exists
  to avoid. The pass compares the fence spans the row's **stamped** grammar
  recognised in that note's body against the ones the **current** grammar
  recognises, and clears `embedded_content_hash` only when they differ. It only
  ever clears, so it can never suppress an invalidation another rule mandates —
  a content change, a `file_path` change, a provider change, the exclusion
  reconciliation sweep above.
- **Per-version frozen recognizers**, `_EXTRACTION_GRAMMARS` in
  `src/services/embeddings.py`. Version 0 is the two pre-#150 regexes copied
  verbatim; each entry stays while any row is stamped with it and is removable
  once none is. What is registered is each version's *embedding* fence grammar,
  because that is what determined the vectors a row stamped with that version
  carries — links and tags are re-derived unconditionally on a stale marker and
  need no version-to-version diff. An unknown stamped version (a build
  downgraded past a bump) counts as *differs*: a row whose grammar this build
  cannot reproduce must be re-embedded rather than certified against a
  comparison that was never made.
- **`content_hash` is never nulled and never sentinelled.** It is `NOT NULL`,
  and it is the move detector's key: a sentinel would make an external rename
  landing between the migration and the pass look like delete-plus-insert,
  which destroys the row and cascade-deletes its `note_embeddings`. A re-embed
  of the whole note, to fix a marker. So the marker lives beside the hash, and
  move detection keeps working throughout the remediation window.
- **The id-preserving move branch deliberately does not stamp.** It repairs a
  row's path without re-extracting its tags, and the marker means "this row's
  derived state came out of the current grammar" — stamping one over tags
  nobody re-derived is exactly the false certification the marker prevents.
  Leaving it stale costs one extra pass (the row's hash now matches its new
  path, so the ordinary branch picks it up on the next tick), and that branch
  already NULLs `embedded_content_hash`, so the vectors rebuild regardless.

### Rollback is roll-forward, and the plan says so

**A bare redeploy of the previous image does NOT restore derived state.** Old
code ignores `extraction_version` and skips unchanged notes by `content_hash`,
so links, tags and vectors stay derived under the new grammar indefinitely —
the same silent staleness, pointing the other way.

The rollback procedure is:

1. revert the grammar commits on a branch;
2. **bump `CURRENT_EXTRACTION_VERSION`** in that build (to 2, then 3, …) and
   keep the versioned mechanism and the frozen per-version registry — the
   registry is what lets the pass compare each row's stamped grammar against
   the restored one;
3. deploy.

The owner-scoped re-derivation pass then rebuilds every note's links and tags,
and (span-diff-scoped, direction-aware) its embeddings, under the restored
grammar, without touching `content_hash`. Comparing legacy-to-legacy instead —
which is what a registry without the frozen v1 entry would do — would certify
the stale vectors for ever.
