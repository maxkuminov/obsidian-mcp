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

## The pass record (#160, migration 019)

The heartbeat above and `indexer_runs` answer different questions and neither
replaces the other. The heartbeat is in-process and resets on restart, which is
exactly right for "is this process's loop alive" and says nothing at all about
"how long has an embed pass been taking lately". `indexer_runs` is the second
question, and it has to survive a redeploy to be worth asking — which is why
parsing container logs was rejected in the design: logs rotate with the
container.

- **One row per pass, written in a `finally`.** `record_indexer_run(trigger,
  user_id)` wraps a pass and yields the `PassStats` its stages fill in; the row
  goes in on the way out, **including when the pass raised**, with its
  `finished_at` and the exception in `error`. A scheme that recorded only
  successes would have nothing to say about the case an operator actually comes
  looking for. `CancelledError` is the one exception: that is lifespan shutdown,
  not a failed pass, and it is treated exactly as `_record_index_run` treats it.
- **A raising pass is never a skipped one.** `PassStats.skipped` suppresses the
  write, and the link backfill sets it **up-front**, clearing it only once every
  guard has passed. So a guard phase that *raised* — an unreadable root, a
  failed provenance probe, a database blip in the "does this scope already have
  links" query — left the flag standing and the row was suppressed: the one pass
  worth reading recorded nothing. The `except BaseException` arm clears the flag
  before re-raising. An exception is evidence the pass ran, so nothing that
  raised can be filed as "did no work".
- **A swallowed stage failure still reaches the row.** `_index_pass_once`
  deliberately swallows per-stage exceptions so one user's broken vault cannot
  stop every other user's pass; those exceptions are written into `error`
  anyway. A row that came out clean because the loop swallowed the failure
  would reproduce the "reports fine, is not" defect (#78) one layer down — the
  log line scrolls away, the row is what survives.
- **The trigger says who asked.** `startup` (the initial pass), `scheduled` (a
  periodic tick), `manual` (the panel's Reindex Now and re-embed, threaded from
  `_reindex_background`), `backfill` (the one-shot link backfill, which records
  its own row because it is a different kind of pass with different timings —
  folding it into the startup row it usually runs beside would hide a slow
  rebuild inside a slow-looking startup). A pass that decided there was nothing
  to do — the backfill's "this scope already has link rows" probe, which fires
  on every startup after the first — sets `PassStats.skipped` and records
  nothing. Noise in a 500-row history evicts what an operator came for.
- **The multi-user startup pass is one per-user sequence, not three loops.** It
  was three: index every user, then backfill every user, then embed every user.
  A run row spanning two of those loops would have to be held open across them,
  and a pass's start and finish would then describe the whole startup rather
  than that user's pass. Per-user ordering (index → backfill → embed) is
  unchanged and is the ordering that matters. One consequence is deliberate: a
  user's failed backfill no longer aborts the backfill of every user after
  them, which is the isolation the index and embed stages already had.
- **Pruning to the newest 500 rides in the same transaction as the insert**, so
  the table is never briefly over the cap and a rollback loses both. It orders
  by `started_at DESC`, not by `id`: a pass is inserted at its *finish*, so two
  passes that started minutes apart can land in the other order, and the
  history is read by start time.
- **Session discipline.** The recorder is only ever called by the *holder* of
  `index_pass_lock`, never by something waiting for it, and it opens and closes
  its own short-lived session inside that call — after the wrapped body's
  session has closed, so one task never holds two pooled connections. That is
  the same rule the panel's destructive actions follow from the other side
  (`_pass_lock_without_a_connection`, see
  [control panel](control-panel.md)): a waiter that keeps a pooled connection
  while blocking on the lock deadlocks against a holder that needs one to
  finish.
- **Recording never fails a pass.** The write is wrapped and logged; it runs in
  a `finally`, where a raise would also replace the exception the operator needs
  to see. Instrumentation that can fail the thing it measures is worse than no
  instrumentation.
- **A user deleted mid-pass costs the owner label, not the row.** `user_id` is
  captured when the pass starts and inserted when it finishes, and a pass over a
  large vault runs for minutes. An administrator deleting that user in between
  makes the FK reject the INSERT — `ON DELETE SET NULL` cannot help, because it
  fires on rows that already exist and this one never got in. Left to the
  handler above, the whole pass would have vanished: the longest passes are the
  likeliest to lose the race, and "the operator just deleted a user" is exactly
  when they open the page. An FK violation (SQLSTATE `23503`, read off the
  driver rather than matched in a localised message) is therefore retried once
  with a NULL owner in a **fresh session** — the first one's transaction is
  aborted. Nothing else is retried: a connection failure retried with a NULL
  owner is one more failed write and a lost owner label for nothing.
- `index_vault` returns `(notes_scanned, notes_indexed)` and `embed_vault` an
  `EmbedPassResult`, purely so the recorder has something to record. Both
  accumulators tolerate `None`, and `record_embedded` still takes a bare int:
  those two functions are replaced by bare no-op coroutines throughout the test
  suite, and instrumentation that insisted on a shape would turn every one of
  those into a failure about recording rather than about indexing.
- **A provider outage is not a clean pass.** `embed_vault` catches every
  per-note exception, logs a warning and carries on — the right behaviour, since
  one poisoned note must not stop the backlog, and it used to be the pass's
  *only* record of it. A total Ollama or OpenAI outage therefore embedded
  nothing, raised nothing, and wrote `notes_embedded = 0, error = NULL`: byte
  for byte the row a pass with nothing to embed writes. An operator watching the
  history through an afternoon of a downed provider would have seen a wall of
  healthy passes. So the failures ride back with the count in `EmbedPassResult`
  and `record_embedded` folds a summary — `embed failures: N of M — first:
  <msg>` — into the row's `error` beside a `notes_embedded` that stays truthful.
  Only genuine failures count: a note skipped by an exclude pattern, skipped
  because its bytes no longer hash to its row, or left behind by a pause is a
  deliberate decision, not something that went wrong.

The table is display only. Nothing reads it for a decision, which is why
dropping it in `downgrade()` costs an operator a view and nothing else. The
panel surface that reads it is
[usage attribution](usage-attribution.md#the-read-only-consumer-160) and
`/admin/performance`.

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
  to avoid. The pass compares what the row's **stamped** version would have
  embedded for that note's body against what the **current** one embeds, and
  clears `embedded_content_hash` only when the two differ. It only ever clears,
  so it can never suppress an invalidation another rule mandates — a content
  change, a `file_path` change, a provider change, the exclusion reconciliation
  sweep above.
- **The comparison is over cleaned OUTPUT, never over recognised spans**, and
  that is not a stylistic preference. v0's cleaner applied its two regexes
  **sequentially**, so the first substitution changed the text the second
  matched against and the two patterns' `$`-anchored spans could overlap. Span
  equality is therefore neither necessary nor sufficient for embedded-text
  equality, in both directions, with real inputs:
  `"~~~\ncode\n~~~\n` ``` `\n# H\ncode\n` ``` `\n[[X]]\n"` has identical spans
  and different cleaned text (span comparison would certify a stale vector),
  and ``` "```\n~~~\ncode\n~~~\n```" ``` has different spans and identical
  cleaned text (span comparison would re-embed for nothing). Both are pinned in
  `tests/test_clean_for_embedding.py`.
- **Per-version frozen cleaners**, `_EXTRACTION_CLEANERS` in
  `src/services/embeddings.py`, reached through `clean_at_version`. Version 0
  is `_v0_clean` — the two pre-#150 regexes, in the order the old cleaner
  applied them, copied verbatim; do not "simplify" it into one pass. Each entry
  stays while any row is stamped with it and is removable once none is. Only
  the *embedding* cleaner needs a frozen history, because links and tags are
  re-derived unconditionally on a stale marker. An unknown stamped version (a
  build downgraded past a bump) counts as *differs*: a row whose grammar this
  build cannot reproduce must be re-embedded rather than certified against a
  comparison that was never made.
- **`content_hash` is never nulled and never sentinelled.** It is `NOT NULL`,
  and it is the move detector's key: a sentinel would make an external rename
  landing between the migration and the pass look like delete-plus-insert,
  which destroys the row and cascade-deletes its `note_embeddings`. A re-embed
  of the whole note, to fix a marker. So the marker lives beside the hash, and
  move detection keeps working throughout the remediation window.
- **The id-preserving move branch re-derives and stamps in its own
  transaction.** It is the one writer that touches a note during the
  remediation window, so deferring to "the ordinary branch will pick it up next
  tick" left a moved note carrying old-grammar tags for a whole pass — the
  next-pass refresh promise, broken by a rename. It therefore binds `tags` from
  the entry the scan already parsed under the current grammar (links come free:
  `moved_new_paths` feeds `_update_links_for_changed`) and stamps
  `extraction_version` in the same UPDATE. The marker never outruns the
  derivation, because both are one statement. `embedded_content_hash = NULL`
  there is unconditional and unrelated: it is #127's path-change invalidation.

### The transition window: two controls (#150)

Between "018 has run" and "the re-derivation pass has finished", derived state
is a mix of the two grammars. Reads degrade gracefully — a stale link row or
tag is wrong in the way it was wrong yesterday — but one **write** path reads
derived state and mutates notes from it, and that one is closed:

**`move_note(rewrite_links=True)` refuses while any row in the caller's owner
scope carries a stale marker.** Its rewrite-source inventory is `note_links`,
so it is only as good as the grammar that built it. Under v0 a note like
`` ```code```\n[[Target]]\n``` `` had its `[[Target]]` masked as code and
produced no row at all; v1 reads the same bytes as prose. Move `Target.md` with
`rewrite_links=True` before the pass reaches that note and the move succeeds,
reports success, and silently strands the link — the exact class of failure the
rest of this tool's preflight exists to prevent. So the preflight adds one
`LIMIT 1` query (`_stale_extraction_error` in `src/mcp_server/tools.py`) before
the rename. It is **owner-scoped**: another user's unfinished pass says nothing
about this caller's rows, and refusing on it would wedge every account behind
one idle vault. The refusal names the first pending note and clears itself when
the pass completes; `rewrite_links=False` is untouched and is the way through.

`edit_note(section=…)` needs no such control: it resolves headings from the
note's own bytes, never from `note_links`.

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
