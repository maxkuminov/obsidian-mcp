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
- **A non-finite frontmatter number is converted at each JSON boundary, never
  at the parse** (#154). It is valid YAML and invalid JSON, so left alone it
  aborts the whole batch; coerced at the parse it would rewrite the note's own
  bytes through `set_frontmatter`. One shared token helper, `.nan` / `.inf` /
  `-.inf`, keys included — see [the section below](#non-finite-frontmatter-numbers-and-the-one-title-rule-154),
  which also names the one title rule every surface shares.
- **The keyword vector attempts the full note and retreats per note** (#127).
  Both writers bound `content[:100000]`, so every term past that was invisible
  to `keyword_search` on a note the tool still reported. `write_tsvector_bounded`
  attempts the whole body, halving down to a floor of **exactly** 100,000
  characters — today's statement — with each attempt in its own savepoint and
  the `try` **outside** `async with session.begin_nested()`, so the error
  unwinds through the context manager's rollback and leaves the outer
  transaction usable. A floor failure propagates, exactly as before: the
  incremental pass aborts with nothing committed and retries next tick, and
  `_rebuild_tsvectors_single_scope_for_tests` is now **atomic** — its every-500 intermediate commits
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

- **Link extraction is capped per note, and a capped note says so** (#203).
  Extraction was unbounded: one 10 MiB note of `[[a]] ` yields 1.75 M
  `ExtractedLink` objects, an 802 MiB peak against a 2 GB container, multiplied
  by every such note in one pass. `MAX_LINKS_PER_NOTE` (10,000, in
  `src/config.py`) bounds it. The cap is applied in **document order** across
  both link kinds — the extractor runs two sequential loops, so "the first N"
  taken per loop would make a note of 20,000 wikilinks lose every markdown link
  in the file — and the same retreat philosophy as the keyword vector applies:
  the degradation is *declared*, never silent. A capped note keeps its first N
  rows, gets `notes_metadata.links_truncated` set (migration 022), is logged
  once at ERROR naming the path and the cap, and `get_links` answers
  `truncated: true`. **The column is the point, not the log line.** The
  ops-health error buffer is 100 entries and process-lifetime, so the line
  naming a note capped at deploy time is gone by the next restart while the
  capped rows persist — and `get_links`/`get_backlinks`/`get_neighborhood`
  would go on reporting the capped set as complete, which is the
  silently-wrong-answer failure this server ranks highest. **A capped note is
  NOT a skip**, and that is deliberate: A.7a's skip list withholds a
  re-derive's certification, so a tenant with one generated MOC would be held
  in re-derive mode indefinitely with no repair that could ever end it — a
  self-inflicted DoS on the machinery that exists to detect foreign rows. The
  claim A.7a makes is structural, and a capped note does not falsify it: the
  truncation is deterministic and the rows written are exactly the rows
  derived. The carve-out is written at the `skips` declaration in
  `_index_vault_pinned` and in `_format_skips`, which are the two places a
  future reader would look.
- **The link rebuild writes per note; the body buffer is a separate,
  documented residual** (#203). `_update_links_for_changed` used to accumulate
  every changed note's rows into one `new_rows` list and insert the lot at the
  end, so peak link-row memory scaled with the number of *changed notes* — and
  a re-derive makes every note changed. Each note's rows are now inserted
  before the next note's are extracted, so the peak is one note's worth (at
  most `MAX_LINKS_PER_NOTE`) plus one 1,000-row insert batch, whatever the
  vault's size; the one-shot backfill flushes on the same rule, checked after
  every note. Measured by the instrumented test in
  `tests/test_asvs_indexer_bounds.py`: four notes of 2,500 links each peak at
  2,500 buffered rows, against 10,000 for the old shape. What this does **not**
  bound is `path_to_content`, the buffer the scan fills for the tsvector loop
  and the link rebuild reads: it holds every changed note's parsed body for the
  pass, and a re-derive holds the whole vault's. That is bounded by the
  write-side note cap times the number of changed notes and is unchanged here —
  an accepted residual on #203, recorded rather than quietly conflated with the
  link-row bound this fixed.
- **Extraction runs off the event loop.** The indexer's changed-path rebuild,
  the one-shot backfill and the scan's `extract_tags` all dispatch through
  `asyncio.to_thread`. The honest caveat, which the tests state rather than
  hide: a thread only yields between `re` calls, never inside one, so this
  bounds the stall to the longest *single* scan step, not to zero. The linear,
  bounded grammars are what make that step short; `to_thread` on a quadratic
  grammar would have moved a multi-minute burn off the loop without shortening
  it. The observable the tests assert is the dispatch itself — a timing
  assertion against a concurrent request is a flake generator on a shared
  runner.

## Non-finite frontmatter numbers, and the one title rule (#154)

`x: .nan` is valid YAML. `NaN`, `Infinity` and `-Infinity` are not valid JSON,
and PostgreSQL's `jsonb` parser rejects all three. Between those two facts sits
this boundary.

**The failure it closes was total, not per-note.** SQLAlchemy sets no
`json_serializer` on the engine, so a `float('nan')` reaching
`notes_metadata.frontmatter` is serialized by stock `json.dumps`, which emits
the bare token. The batch upsert has no per-note retreat (unlike the keyword
vector's halving): the insert raises, the pass's single transaction aborts,
**nothing** commits, no `content_hash` advances, and every subsequent tick
retries the same fatal batch. One note takes indexing down for the whole owner
— #126's failure mode reached by a new route. Pinned against a real column, in
both halves (the column's own rejection, and the whole-pass outage reproduced
with the pre-fix sanitiser restored), in
`tests/integration/test_issue_154_non_finite_frontmatter_pg.py`.

**The scrub is deliberately not where this is fixed.** `_scrub_frontmatter`'s
predicate is "nothing can render this" — see [the representability
boundary](vault-tools.md#the-representability-boundary-scrub-once-at-the-parse-149),
which this section does not restate — and both YAML and Python render a
non-finite float perfectly well. More decisively, the parsed mapping the scrub
produces is what `set_frontmatter` re-serialises: a coerced `".nan"` string in
it would rewrite `x: .nan` to `x: '.nan'` in the note's own bytes as a side
effect of setting an unrelated key, which is the destructive-write class this
project ranks first. Recording it as `lossy` instead would make
`set_frontmatter` refuse outright on a note whose only defect is a NaN. So the
float stays in the mapping, and **each JSON boundary converts** instead.

**One token helper, four boundaries.** `vault.non_finite_token` renders the
canonical YAML spelling — `.nan`, `.inf`, `-.inf` — and is the only place that
decides it. Its consumers: this module's `_jsonb_value` (the JSONB column),
this module's `_note_title`, `vault.read_file`'s title (which `read_note` and
the control panel both inherit), and `read_note`'s frontmatter view
(`read_result._view_leaf` / `_view_key`). **The token is canonical whatever the
note spelled**: YAML 1.1 accepts `.nan`, `.NaN`, `.NAN`, `.inf`, `.Inf`,
`.INF`, `+.inf` and their negatives, and the parse preserves none of it — by
the time any consumer sees the value it is a Python float. `frontmatter_yaml`
still carries the note's own spelling (LF-normalized), which is where a caller
goes to see it.

**Mapping keys take the token too, and the collision rules differ by
boundary.** A YAML mapping may be *keyed* by a non-finite number (`.nan: 1`),
and both key paths stringify. Coercion can then make two distinct YAML keys
land on one string (`.nan: 1` beside `".nan": 2`):

- **The JSONB sanitiser takes the first key in document order**, stated rather
  than inherited — the dict comprehension it replaced silently kept the *last*,
  an accident of iteration order. The index has no channel through which to
  report a loss and must never fail the pass, so a deterministic, documented
  winner is the whole available remedy.
- **The read view omits the whole view** with a duplicate-key omission whose
  reason code (`duplicate_json_key_after_coercion`) distinguishes it from a
  native `1:` / `"1":` collision. First-wins there would emit a partial mapping,
  and a caller cannot tell a pruned view from a complete one. The two
  boundaries differ because one can report a loss and the other cannot.

The view also records the *value* coercion — reason code `non_finite_float`,
in `metadata_coercions`, a list that is a sibling to `metadata_omissions` and
never a substitute for it: an omission names a field dropped whole, and a
retained-but-altered value is a different fact about a field that is still
there.

**`_sanitize_value` is split, and that is the point of the change, not a
tidy-up.** It served two consumers — the JSONB column *and* `_note_title` — so
one return value silently answered two different questions and a change made
for the column re-keyed titles. It is now `_jsonb_value` ("what may this become
inside a JSON document?") and `_note_title` ("what is this note called?"), each
over the shared token helper.

**One title rule, and it is this module's.** `vault.note_title` is the single
implementation of the indexer's present behaviour — the sanitised value,
falling back to the filename stem when falsy, rendered with `str()` and bounded
to 512 characters, with non-string mapping keys and non-JSON scalars
stringified *inside* a container first — **plus the one exception that a
non-finite number renders as its YAML token.** `read_note` and the control
panel adopt it; `indexer._note_title` is this module's name for it. The
indexer's is the rule to standardise on because it is already the value
`keyword_search`, `list_notes`, `get_recent` and the panel's listings show, it
is bounded to the column's width, and it is the one of the three that a titling
incident (#126) has already hardened.

Adopting it changes what `read_note` and the panel show in three non-NaN cases,
listed rather than discovered — a date inside a container (`['2026-08-25']`,
not a Python `repr` of a date object), a non-string mapping key (`{'1': 'a'}`),
and a title over 512 characters (its first 512). A top-level date, a list of
strings, a numeric title and every falsy title (which falls back to the stem)
are unchanged. The indexed title is unchanged except for the non-finite case.

**Nothing here ever reaches a note's bytes.** The only thing that rewrites a
block is `set_frontmatter`, which serialises the mapping the parse produced —
still a float, which PyYAML dumps back as `.nan` — so setting an unrelated key
on such a note leaves `x: .nan` byte-identical. That is asserted, not assumed.

*Rejected:* `json_serializer=partial(json.dumps, allow_nan=False)` on the
engine. It converts an invalid-JSON write into a `ValueError` instead of a
driver error — still a fatal pass — and spreads the boundary from one function
into the engine configuration, which is the per-consumer screening the #149
boundary doctrine exists to avoid.

## Every pass entry point publishes a vault-root snapshot first (#199)

Two active users whose `vault_path` values overlap make the indexer file one
tenant's notes under the other's `user_id`, which is a silently wrong search
result for as long as the rows survive. The checks themselves, the snapshot's
lifecycle and the limitations live in
[vault-roots-and-tenancy.md](vault-roots-and-tenancy.md); what belongs here is
where the pass calls them and what it records.

- **One `detect_and_publish()`, called from every entry point that can begin a
  pass — and the loop alone was not enough.** `run_indexer_loop` is two of the
  five: the startup block (`detect_root_overlaps("startup")`) and each periodic
  tick (`detect_root_overlaps("periodic")`). The other three reach
  `index_vault` / `embed_vault` / `_rebuild_tsvectors_single_scope_for_tests` without going through it
  at all — `src/main.py::lifespan` (`_publish_first_root_snapshot`, run
  **synchronously before the app serves**), the panel's `_reindex_background`
  (Reindex Now, re-embed and reset embeddings, which mirrors the loop and
  shares `index_pass_lock` and *nothing else*), and
  `scripts/rebuild_tsvectors.py`, which is a **separate process**
  (`docker compose run --rm`) with its own `_active_user_ids()` loop, no
  lifespan and no indexer loop. A detection installed in the loop would have
  been bypassed by the last two, one of them from outside this process
  entirely.
- **Detection runs before `index_pass_lock`, at every one of them.** The check
  must not queue behind the pass it exists to gate. That the entry points
  therefore overlap is expected, not a race: `detect_and_publish` serializes
  observation, the checks and the publication under one process-global lock and
  its publication is monotonic in a sequence taken under that lock, so an older
  detection cannot overwrite a newer quarantine with its own empty result.
- **On the periodic tick it runs *before* `_is_paused()`.** A pause suppresses
  index and embed work; it must not suppress detection, because a pause is
  entered precisely when an operator is doing something destructive and
  watching the panel, which is the worst moment for a quarantine to become
  invisible.
- **A paused iteration still records.** It publishes, and then
  `record_quarantined_runs("scheduled")` logs at ERROR and writes one
  `indexer_runs` row per quarantined user before returning. Only the work is
  suppressed. The row cadence is unchanged — a running deployment already
  writes one row per user per tick.
- **The skip lives in the shared pass helpers, not in each loop.**
  `index_vault`, `link_backfill_pass`, `embed_vault` and `_rebuild_tsvectors_single_scope_for_tests`
  each call `_refuse_quarantined_pass(user_id, stage)` **ahead of resolving the
  root**, so every caller inherits it and a sixth entry point added later gets
  it by routing through the same helper. A skip re-implemented per loop is a
  skip one loop will be missing. It also refuses when *nothing* has been
  published — no pass may begin over a root nothing has checked — and it never
  applies to `user_id is None`, so single-user mode is untouched.
- **Nothing is deleted, pruned or provenance-stamped for a skipped user.** The
  refusal precedes `_vault_root` and the pinned root, so the pass never reaches
  the prune or `classify_provenance`. Preserving the rows is what makes a
  corrected assignment cheap; they are unreachable meanwhile, because the
  admission gate refuses every tool for the same user.
- **Both records, and the ring buffer's lifetime is the reason for both.**
  `VaultRootQuarantined` is a `RuntimeError` carrying the **operator**-facing
  wording — both accounts, both roots, the relation or the cause — so the
  existing per-stage handlers write it into `indexer_runs.error` and log at
  ERROR, and `_index_pass_once` returns False: a skipped user's pass is not
  recorded as a clean run. The log line alone would not do. It reaches the
  in-process error ring buffer, which is 100 entries and process-lifetime,
  while the misconfiguration survives restarts — the same argument that made
  `notes_metadata.links_truncated` a column rather than a log line (#203). The
  run row is what an operator reads *after* a restart, and a pass that quietly
  did no work for a user is otherwise indistinguishable from a pass that found
  nothing to do.
- **Unrelated tenants are indexed exactly as before**, in the same pass — the
  same isolation `_index_pass_once` already gives a user with a broken vault.
- **A detection failure does not abort the caller.** `detect_root_overlaps`
  logs and swallows: it is not a per-root failure (an unopenable root is a
  per-user verdict), so the only way it raises is that the user enumeration
  failed, which means the database is unavailable and the pass would fail
  anyway. Swallowing it opens nothing — either a previous snapshot still stands
  (retained, never cleared) or nothing has been published and
  `_refuse_quarantined_pass` refuses every multi-user stage until a later entry
  point publishes one.
- **The standalone rebuild is the one exception, deliberately: a detection
  failure there is fatal.** `scripts/rebuild_tsvectors.py` calls
  `detect_and_publish()` directly rather than through `detect_root_overlaps`,
  and lets it propagate to the script's `sys.exit(1)`. The caller is an
  operator at a terminal who can read the error and re-run, and rewriting every
  keyword vector in the vault against roots nothing has checked is exactly the
  pass this guard exists to stop — where the long-running server's answer is to
  keep the panel up and retry at the next entry point.

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
  deliberate decision, not something that went wrong. Deliberate asymmetry:
  these swallowed per-note failures do NOT flip `_record_index_run(ok)` — the
  dashboard's "Last run" heartbeat (#78) stays green through a total provider
  outage while the Performance page's run row says `failed`. The heartbeat
  answers "is the loop alive", the run row answers "did the work succeed";
  collapsing them would change #78's semantics, so don't.

The table is display only. Nothing reads it for a decision, which is why
dropping it in `downgrade()` costs an operator a view and nothing else. The
panel surface that reads it is
[usage attribution](usage-attribution.md#the-read-only-consumer-160) and
`/admin/performance`.

## The embed pass reports what it did (#201, #202)

The section above ends at the point #160 could reach: a provider outage folds
a summary into `indexer_runs.error`. It could not reach further, because the
thing it was summarising still lied. `embed_note` returned an **int**, and `0`
meant three unrelated things — a note that cleaned to zero chunks *and was
certified*, a provider exception the function swallowed, and a vector/chunk
cardinality mismatch — while `_embed_vault_pinned` ran `outcome.embedded += 1`
after all three. A total Ollama or OpenAI outage therefore wrote
`notes_embedded = N, error = NULL`: byte for byte the row a healthy pass
writes, with a **positive** count, which is more misleading than the zero #160
was designed against (#201).

### The outcome is typed, and the failure detail rides on it

`embed_note` returns a frozen `EmbedNoteResult` over a five-member
`NoteEmbedOutcome` (`src/services/embeddings.py`). Each outcome answers every
question the pass asks about a note, so no caller has to infer one from a
number:

| outcome | certifies | counts into `notes_embedded` | a failure | an attempt |
| --- | --- | --- | --- | --- |
| `EMBEDDED` | yes | yes | no | yes |
| `CERTIFIED_EMPTY` — cleaned to zero chunks | yes | yes | no | **no** — no provider call |
| `PROVIDER_FAILED` — the swallowed exception | no | **no** | yes | yes |
| `PROVIDER_CARDINALITY_MISMATCH` | no | **no** | yes | yes |
| `GENERATION_MISMATCH` — the configuration moved under the call | no | **no** | **no** | yes |

- **Two chunk counts, not one.** `chunks_submitted` is what went to the
  provider; `chunks_embedded` is what was stored. A single field would have to
  mean one or the other, and the two consumers want different ones: the pass's
  `total_chunks` metric wants what was stored, and the per-tenant budget below
  must debit what was *sent*. A budget debited by stored chunks is not debited
  at all by a failing provider, so a tenant whose every note fails would burn
  the whole pass, every pass, and never reach its own bound — the starvation
  #202 is about, surviving inside the fix for it.
- **The failure detail is on the result, because the caller has nothing left
  to build it from.** `embed_note` swallows the provider exception, so by the
  time `_embed_vault_pinned` sees a failure there is no exception to inspect
  and `EmbedPassResult.first_error` would have read `embed failures: 412 of
  412 — first: None` — an operator's only view of a total outage, saying
  nothing. `EmbedNoteFailure` carries the exception class, the message, and
  the requested/received chunk counts, and
  `EmbedPassResult.record_failure_detail` builds the summary from it.
  `record_failure(exc)` stays for the exceptions that genuinely escape *around*
  the call (a database error, a failed rollback), so the two entry points
  converge on one counter and one `failure_summary`.
- **The message is truncated at capture** (`MAX_EMBED_FAILURE_MESSAGE_CHARS`,
  200), not where the run row is written. `MAX_RUN_ERROR_CHARS` (4,000) bounds
  the *whole* `error` text, and one untruncated provider traceback can exceed
  that on its own and evict the stage labels beside it.
- **A failing outcome without a failure raises**, in `__post_init__`. It is an
  invariant rather than a convention precisely because `record_failure_detail`
  is driven entirely off that field.

**Why a return value and not a raise.** Re-raising from `embed_note` and
letting the caller's existing `except Exception` fire is smaller, and it is
wrong in two places. `_reconcile_exclusions` also calls `embed_note`, and its
declared convergence exception is that a row whose provider call fails is left
unstamped and retried on a later pass — a raise there would have to be
re-caught to preserve that, so nothing is saved. And a raise makes the *type*
of a provider blip indistinguishable from a database error at the call site,
which is the conflation the typed outcome exists to remove.

**There is deliberately no `__int__` and no `__radd__` on `EmbedNoteResult`.**
A first draft of the design claimed an `__int__` would keep
`total_chunks += result` working. It does not: `int.__iadd__` falls back to
`int.__add__(result)`, which returns `NotImplemented`, and with no `__radd__`
the statement raises `TypeError`. The shim was dropped rather than completed,
and explicit is the outcome we want — a caller that must name the field it
means cannot silently go on treating five outcomes as one number.
(`EmbedPassResult.__int__` is a different thing and stays: it is the *pass*'s
embedded count, which the recorder still reads as a number.)

### `attempted` is one sentence, and its exceptions are consequences

> **`attempted` is incremented exactly once per note for which an embedding
> provider call is issued** — at the `embed_note` call sites, and nowhere else.

`EmbedPassResult.record_attempt()` is the only writer. Three things follow, and
they are stated because they change what the run row used to say:

- **It is no longer initialised from the backlog's size.** `attempted` counted
  work *contemplated*; it now counts work done.
- **A zero-chunk note is not an attempt.** It certifies and counts into
  `notes_embedded`, and it makes no provider call, so a pass over 400 notes of
  which 50 clean to nothing reports `… of 350`.
- **A sweep row decided without a provider call is not an attempt.** The
  exclusion sweep scans every certification-current row in the scope (~16,700
  on the production vault) and decides about almost all of them without calling
  anything; counting those would render three failures out of three calls as
  `3 of 16,700`.

Everything else follows from the same rule rather than needing its own clause:
an excluded note, a hash-mismatched note and a note left behind by a pause or a
budget stop all issue no provider call, so none of them moves the denominator.
One sentence with derived consequences is what lets the design, the
requirement, the tasks and the tests agree.

**"At the call site" means at issuance, not at the return** — and that is the
half the first implementation got wrong. Both loops read
`EmbedNoteResult.chunks_submitted` *after* `embed_note` returned, which is
correct for every path that returns and counts nothing at all for every path
that **raises after the provider call**. `certify_embedded` raises
`StaleCertification` when the row moved under the call, and a database error
can escape anywhere between the two: the call had been made, the provider's
time was spent, and neither `attempted` nor the tenant's chunk budget knew. A
tenant losing that race on every note issued provider calls for the whole
stage, every stage, and never became budget-exhaustible — #202's starvation
surviving inside the fix for it, and an `indexer_runs` row reading `2 of 0`.

So `embed_note` takes an `on_provider_call` callback and invokes it
immediately **before** the await, and `_ProviderCallAccounting` is what both
loops hand it: `issued()` records the attempt and debits the budget, exactly
once per note, on every subsequent path including the ones that never produce a
result. `reconcile()` is the backstop in the other direction — a return whose
call nothing announced is still counted — so a future provider-calling path
that forgets the callback under-reports nothing; `_counted` makes the pair
idempotent per note. And `budget.note_finished()` moved into a `finally`, so a
note that reached `embed_note` and then raised still counts as having reached a
note boundary; without that, `exhausted()`'s "at least one note completed"
guard could never be satisfied by exactly the notes burning the provider time.

A certification that matched no row is therefore an **attempt** (its call was
made) and still not a failure and not embedded — the same disposition it always
had, now with a truthful denominator.

**And the denominator is only rendered as a ratio when it is one.**
`record_failure` also catches what escapes *around* the call — a database error
loading the row, a rollback that itself failed, a byte read that raised after
the backlog selected the note — and those move `failures` without moving
`attempted`. Rendered as `N of M`, one such error on a pass that never reached
the provider reads `1 of 0`, and `of 0` says the pass made no calls: the
natural reading is that the counter is broken, and an operator who concludes
that stops trusting the number that would have told them about a real outage.
So `failure_summary` states the two as separate facts when the failures
outnumber the calls — `embed failures: 1 (0 provider call(s) issued)` — and
keeps the ratio otherwise, because that is the shape an outage produces and the
one the existing history is full of. The pass's own ERROR line is built from
the same property, so the log and the `error` column cannot disagree.

**The reconciliation sweep feeds the same accumulator.** It re-embeds
re-included notes and used to swallow their failures into its own
`except Exception` with nothing riding back to the pass — so a provider outage
during a pass whose *backlog was empty*, which is the steady state of a
fully-indexed vault where the sweep is the only thing making provider calls,
still wrote a clean run row. That is #201 surviving in the one code path a
narrower fix would not have touched. The sweep now takes the same
`EmbedPassResult` and calls the same `record_attempt` /
`record_failure_detail`.

**And it increments `embedded`, which it did not at first.** The sweep commits
its vectors through the same `certify_embedded` predicate the backlog uses, so
a note it re-embedded is a note the pass embedded. Leaving the counter alone
made `notes_embedded` under-report exactly the pass whose whole output was the
sweep's — the fully-indexed steady state again, from the other side: an
operator watching a healthy vault repair itself saw `notes_embedded = 0`. Like
the backlog's, the increment is after the per-note commit, so a certification
that rolled back is not reported as embedded.

The **#160 asymmetry is unchanged**: none of this flips the in-process
`_record_index_run` heartbeat. "Is the loop alive" and "did the work succeed"
are still different questions.

### The chunk cap is a declared degradation, and a capped note certifies

`chunk_text` had no bound. `MAX_NOTE_BYTES` is 10 MiB and `CHUNK_SIZE` is 512
tokens (~4 characters each), so one *legal* note is ~5,120 chunks — each of
them one sequential, 30 s-bounded provider call under `index_pass_lock`, with
no `LIMIT` on the backlog behind it. Re-editing one such note kept every later
tenant's new and edited notes out of `notes_metadata`, out of the tsvector
index and out of the embeddings indefinitely, visible only as missing
`indexer_runs` rows (#202).

`MAX_CHUNKS_PER_NOTE` (1,000, a module constant in `src/config.py` beside
`MAX_LINKS_PER_NOTE` — the same call the link cap made) bounds it, through
`chunk_text_bounded(content, *, chunk_size, overlap, max_chunks)` in the shape
#203 gave `extract_links_bounded`. `chunk_text` delegates to it, so "this note
produces no chunks" means the same thing at the embed path and at the exclusion
sweep's zero-chunk probe — and the probe now stops at the first chunk instead
of chunking a 10 MiB note to find out it is non-empty.

The cap is **declared in the same four places** #203 established for links:

- **first N in document order** — the head, so a capped note keeps the part a
  reader would call the note;
- **`notes_metadata.chunks_truncated`** (migration 023), written in the
  certifying transaction so the marker and the vectors it describes land or
  roll back together, and cleared by a later embed that fits under the cap, by
  the exclusion branch, and by a `CERTIFIED_EMPTY` — the last two because both
  leave the note with no vectors at all;
- **one ERROR line naming the path and the cap**;
- **`embedding_truncated: true` on every vector-search row for that note**,
  because the tail of the note is not semantically searchable at all and a
  result from its head reads as a result from the whole note.

Three clauses are load-bearing and a future reader will otherwise undo them:

- **A capped note is CERTIFIED, never held.** An uncertified note is
  re-selected by the backlog on every tick for ever — #127's permanent burn
  arriving by a new route. This narrows what "full coverage" means (it is now
  full coverage of the *bounded requested set*) and the requirement was
  modified to say so rather than quietly contradicted. Like a link-capped note,
  a chunk-capped note is **not a skip**: A.7a's claim is structural, and a
  deterministic truncation whose rows are exactly the rows derived does not
  falsify it.
- **The ERROR follows the commit.** Logging before it would leave a permanent
  ERROR in a bounded, process-lifetime buffer for a truncation that then rolled
  back on a `StaleCertification`, sending an operator after a note that was
  never stored that way.
- **The line can never name the note's true chunk count.** Obtaining it means
  the unbounded chunking the cap exists to prevent. `chunk_text_bounded`
  settles *whether* it capped by generating exactly one window past the cap and
  discarding it — which is also why a note landing on exactly `max_chunks` is
  complete and is not marked.

**Why a cap at all, given #127 removed the aggregate deadline.** #127 removed a
*time* budget that fired on healthy chunks; this is a *count* bound that
changes what is embedded, deterministically, and says so. A note the deadline
killed never certified and burned the same 300 s every tick for ever; a capped
note certifies once and is never re-selected while it is unchanged.

**`CHUNK_OVERLAP` must stay strictly below `CHUNK_SIZE`, and `Settings`
refuses otherwise.** The chunker steps by `max(char_size - char_overlap, 1)` —
#10's infinite-loop guard. At `CHUNK_OVERLAP == CHUNK_SIZE` that step collapses
to **one character**, so ~3 KB of prose produces ~3,000 chunks and every
ordinary note in the vault hits the cap: a configuration typo silently
truncating the embedding of the whole vault, with the cap's ERROR line firing
thousands of times. The guard turned a hang into a quiet catastrophe; it did
not make the configuration sane, so the model validator in `src/config.py`
rejects it at startup naming both values. The cap is a bound on a *note*, and
it only behaves like one while the step is a meaningful fraction of the chunk.

### The rotation cursor is persisted, and it fails open

`_active_user_ids()` had no `ORDER BY`, so its order was the planner's opinion
— stable enough in practice that the same tenant went first every cycle, and
unspecified enough that nothing could be asserted about it. It now orders by
`users.id`, which alone makes the order a fact; a rotation over an unspecified
order is not a rotation.

`_rotated_user_ids()` rotates that list to begin at the smallest id **strictly
greater** than the value stored under `indexer_state['embed_rotation_cursor']`,
wrapping. `_advance_rotation_cursor(uid)` writes it after each user's per-user
sequence finishes, success or failure, in its own short session opened by the
holder of `index_pass_lock` after the wrapped body's session has closed —
`_write_indexer_run`'s discipline, so one task never holds two pooled
connections.

- **It is used by `run_indexer_loop` only** — the startup pass and the periodic
  tick. `_reindex_background` and the keyword rebuild keep the unrotated list:
  an operator-triggered reindex is not the starvation vector, and letting a
  panel click move the periodic pass's rotation would make the schedule a
  function of who clicked what.
- **The cursor stores a user id, never a positional offset.** The active list
  changes when a user is added, deactivated or deleted, so an offset points
  somewhere else on the next cycle. "Resume after id 7" is well defined whether
  or not user 7 still exists, because "the smallest id strictly greater than 7"
  does not require it to — and an out-of-range value needs no special case
  either: it selects nothing and wraps to the first, which is the same outcome
  by the ordinary rule.
- **In-memory would have been a no-op.** In-process state resets on every
  restart and every deploy, and a deploy recreates the container — so the
  tenants at the tail of the order are exactly the ones a restart-truncated
  pass never reaches, and an in-memory cursor would reset precisely when it was
  about to pay off. It is persisted, and it is persisted beside the
  fingerprints because all three are single facts about the index as a whole.
- **A cursor the pass cannot use is logged once at WARNING and ignored, never
  fatal**, and this is the **opposite disposition** from the fingerprints'
  below. The value is text in a key/value table, so drift, a hand-edited row or
  a downgrade can make it non-numeric, negative or wider than the column it is
  compared against; `parse_rotation_cursor` returns `None` for every unusable
  spelling and never raises, and the pass starts at the first tenant in id
  order — a complete, correct pass, and precisely today's behaviour. A cursor
  is scheduling state whose worst consequence is an *order*; a fingerprint is a
  claim about what the stored rows **are**, and its worst consequence is a
  permanently wrong answer. Fail-closed belongs to the second and would be
  absurd on the first: a stray character in a bookkeeping row must not stop
  every tenant's indexing to protect nothing.

### The per-tenant budget is checked at a note boundary and nowhere else

`EmbedBudget` gives each tenant's **embed stage** a chunk allowance
(`EMBED_CHUNK_BUDGET_PER_USER` / `settings.embed_chunk_budget_per_user`, 5,000)
and a wall-clock allowance (`EMBED_TIME_BUDGET_SECONDS_PER_USER` /
`settings.embed_time_budget_seconds_per_user`, 300 — one
`INDEX_INTERVAL_SECONDS`); `0` disables either, and both at `0` disables the
machinery entirely. It is
consumed by the backlog loop and by the reconciliation sweep, since both call
the provider, and it is checked at exactly the two places `_is_paused()`
already sits.

- **It debits chunks *submitted*.** Every provider call debits what it sent,
  whatever it returned: a raise and a cardinality mismatch debit exactly as a
  success does. This is the same argument as the two chunk counts above, and
  the wall clock does not rescue the other choice, because an operator may set
  the time budget to `0` and keep only the chunk budget.
- **Never mid-note.** `embed_note` refuses partial certification, so a note
  abandoned between chunks is uncertified, re-selected next tick, and
  re-performs every provider call it already made — #127, exactly. Checking at
  the boundary means the overrun is at most one note, which the chunk cap has
  already bounded.
- **At least one note, always.** The check runs only after a note of that
  user's pass has completed, so a tenant whose very first note exceeds the
  whole budget still advances by one note per pass instead of zero for ever.
  Without this clause a small budget is a livelock.
- **Only when the pass serves more than one scope** (`_active_scope_count`, and
  `enforced` is `scopes > 1`). In single-user mode, and in a multi-user
  deployment with one active user, there is no other tenant to be fair to; a
  budget there would spread a first index of 2,577 notes over several
  five-minute-spaced passes for no benefit and would look like a stall. This
  clause is what keeps the default deployment's behaviour identical to today's.
  A *failed* scope count returns 1 and leaves the pass unbudgeted, which is the
  safe direction: a bookkeeping query that fails must not start stopping
  tenants short.
- **A stop is not a failure and writes nothing to `error`.** It is a deliberate
  decision, the same class of event as a pause, and writing it into
  `indexer_runs.error` would fire #201's own outage signal on a healthy server.
  It logs once at WARNING per user per pass, whichever stage reached it. The
  operator-visible signal for a tenant that is permanently over budget is the
  **dashboard's pending count**, which stays high across passes — a persistent
  backlog is a property of the index rather than of one pass, which is exactly
  what an operator needs to see.
- **The bound this buys, stated exactly.** Because the budget is evaluated only
  between notes, the delay one tenant can impose on the next is *the budget
  plus one note's embedding time*, and one note's embedding time is bounded by
  `MAX_CHUNKS_PER_NOTE` × the provider's per-call bound — 1,000 × 30 s ≈ **8.3
  hours** on a provider answering every call at the very edge of its timeout.
  That is a pathological-provider figure, not a steady-state one, and it is
  accepted (L4) rather than closed by an aggregate deadline, which is precisely
  the construct #127 removed and which would recreate the never-finishing note
  one size class up.
- **The fairness claim covers the embed stage only.** `index_vault` and
  `link_backfill_pass` run before `embed_vault` in each user's sequence and
  stay unbudgeted (L3). Each is a single transaction over a walk of the vault
  whose cost is bounded by the vault's size and the write-side caps rather than
  by an external provider's latency, and stopping one part-way means either
  committing a partial derive — which A.7a exists to forbid — or discarding the
  whole pass's work. The starvation #202 measured was the embed stage's; the
  scoping is recorded rather than implied.

## The settings fingerprints (#206, migration 023)

`note_embeddings` recorded nothing about the provider, model, chunk size or
overlap that produced it, and `content_tsvector` recorded nothing about
`FTS_CONFIGS`. The dimension guard reads the live column width from
`pg_attribute` — a **physical** fact about the table — so it catches a dump
restored into a differently configured deployment and cannot catch a
**same-dimension model swap**. bge-m3 for another 1024-dim model mixes two
vector spaces in one column permanently, makes cosine distance meaningless, and
every startup after it is clean. Both guards stay: they answer different
questions, and the fingerprint is the logical one.

`indexer_state` (migration 023) is a three-key `key`/`value` table holding
`embedding_fingerprint`, `fts_fingerprint` and `embed_rotation_cursor`, with a
`CHECK` closing the key set. **The CHECK is not tidiness**: a key that does not
exist reads as *absent*, and absent is the state that makes the startup guard
adopt rather than refuse — so one mistyped key would silently disable the guard
whose entire purpose is to prevent a permanent, undetectable corruption. Adding
a key is therefore a migration, which is correct, because every key here has a
startup or a scheduling consequence. **023 backfills nothing**: a fingerprint
derived at migration time would assert that the stored rows were produced by
the configuration the `.env` carries *now*, which is exactly the claim the
fingerprint exists to test.

Both values are canonical JSON — `json.dumps(obj, sort_keys=True,
separators=(",", ":"))` — built in `src/services/index_state.py`, a separate
module so `embeddings.py` and `indexer.py` can both import it without a cycle:

```
embedding_fingerprint() -> {"chunk_overlap":0,"chunk_size":512,"dimensions":1024,
                            "max_chunks_per_note":1000,"model":"bge-m3",
                            "provider":"ollama","v":1}
fts_fingerprint()       -> {"configs":["english"],"v":1}
```

- **`model` is the ACTIVE provider's**, resolved by `active_embedding_model()`
  through the same branch `get_provider()` takes. Reading the inactive
  provider's model while the provider is chosen somewhere else is the exact
  defect this guard exists to catch, so the selection lives in one function.
- **`max_chunks_per_note` is in it.** It changes what a note's stored vector
  set *is*: at cap N a long note holds N chunks and its tail is absent, and the
  same note at cap 2N would hold a different set. Lowering the cap leaves rows
  beyond the new bound; raising it leaves rows silently incomplete against the
  new policy that **nothing will ever re-select**, because their
  `embedded_content_hash` still matches. Including it makes a cap change a
  declared reset instead of a permanent, invisible under-embedding (L7).
- **`configs` is sorted**, so membership is compared and order is not.
  Index-time tsvectors are `||`-concatenated and query-time tsqueries are OR'd,
  and both operators are order-insensitive over lexeme sets, so
  `["english","norwegian"]` and `["norwegian","english"]` produce identical
  stored vectors; comparing them as ordered lists would refuse startup over a
  reordering that changed nothing.
- **Canonical JSON rather than a delimited string.** A model name may contain
  any character, so a delimiter would need an escaping rule that would then
  have to be specified, versioned and tested. JSON has one already, it parses
  on both sides so a mismatch can name **which field changed** rather than
  printing two opaque strings, and `sort_keys` with compact separators admits
  exactly one spelling per configuration — the property byte equality needs.
- **`v` (`FINGERPRINT_VERSION`) makes adding a field a deliberate act.** It
  changes every fingerprint, so the change that adds one must ship either a
  rewrite of the stored value or an instruction to reset.

### Comparison at startup: both fail closed

`_check_embedding_fingerprint()` and `_check_fts_fingerprint()` in
`src/main.py` share one body (`_check_settings_fingerprint`), which is what
makes "identical disposition" structural rather than a coincidence two
functions happen to preserve. They run immediately after
`_validate_fts_configs()` — deliberately, so a *misspelled* config name still
fails with that check's own message listing the configurations the database
has, rather than as an opaque fingerprint diff — and inside the sandbox-mode
short-circuit, so `MCP_SANDBOX_MODE` skips them like every other guard.

| stored | disposition |
| --- | --- |
| table absent | return, deferring to alembic — `state_table_exists` asks with `to_regclass`, because a `SELECT` against a missing relation aborts the transaction and the guard could then not go on to defer |
| `ABSENT` | **adopt**: write the current fingerprint, commit, WARNING that it was *assumed, not verified* |
| `MATCH` | proceed silently |
| `DIFFERS` | CRITICAL naming both fingerprints and the differing fields, pointing at `make reset-embeddings` / `make rebuild-tsvectors`, then `sys.exit(1)` |
| `UNREADABLE` — not JSON, JSON that is not an object, or a `v` this build does not know | the same refusal, saying the stored value could not be interpreted, and **writing nothing** |

- **Absence is adopted, not refused.** Refusing there would take every existing
  deployment down on upgrade over a configuration nobody changed. The cost is
  L8: the first startup after this change blesses whatever it finds, which is
  why the change had to be deployed with the embedding and FTS configuration
  unchanged.
- **Keyword vectors fail closed exactly as embeddings do.** A first draft let
  an FTS mismatch warn and serve, on the reasoning that a stale stemmer is
  *incomplete* rather than wrong. That reasoning does not survive contact with
  the failure: under `["english"]` the token `running` is stored as the lexeme
  `run`, and a query under `["simple"]` for `run` then **matches a note that
  does not contain the word `run`** — a false positive, indistinguishable from
  a real hit, handed to an agent that acts on it without a human ever seeing
  the query. That is this product's second-named expensive failure, not a
  recall shortfall. (Symmetrically, a query for `running` under `simple` misses
  the note entirely.) Changing `FTS_CONFIGS` is a rare operator action, the
  refusal names the one command that repairs it, and there is always a second
  exit — putting `FTS_CONFIGS` back clears the refusal immediately with no
  rebuild at all. That second exit is what distinguishes this from an outage.
- **An unreadable value refuses and is never rewritten.** A value this build
  cannot compare is one it cannot certify the rows against, and overwriting it
  with the current fingerprint would convert an unreadable claim into a
  confident false one. It is `clean_at_version`'s rule — an unknown stamped
  version counts as *differs* — in a new place.
- **Startup never rewrites a fingerprint it has just refused on.** Only the
  maintenance workflows write one after the initial adoption; a guard that can
  clear its own refusal is not a guard.
- **Endpoint URLs are excluded, deliberately, and that is L1.** `OLLAMA_URL`
  and `OPENAI_BASE_URL` are infrastructure: moving between `api.openai.com`, an
  Azure deployment and a compatible proxy usually serves the identical
  artifact, and including them would demand a full vault re-embed for a move
  that changed nothing about the vectors. The consequence is that the
  fingerprint records the **configuration, not the artifact**: `bge-m3` is a
  mutable Ollama tag, so `ollama pull` can replace the weights behind it and a
  second host can serve different weights under the same name. No value
  available to this process distinguishes those cases, and a probe would have
  to trust the endpoint it is checking. **The operator rule that stands in its
  place**, documented beside the model keys in `.env.example` and `README.md`:
  *a change of model artifact — re-pulling a tag, or repointing at a host
  serving different weights — requires `make reset-embeddings`, and nothing
  will detect it if you skip that.*

### A fingerprint write is not instrumentation

"Recording never fails a pass" governs the run history and the rotation cursor,
where a lost write costs an operator a view. A fingerprint is the claim a later
startup **refuses** on, so `set_state` has no internal `try`/`except` and every
fingerprint write lives inside its maintenance operation's own transaction. A
reset that wiped the column and then swallowed a failed record would leave the
stored value naming the **previous** configuration over rows about to be built
under the new one; a rebuild that rebuilt everything and lost its write would
refuse at every subsequent startup over a database that is actually correct.
Swallow-on-failure stays exactly where it was — `_write_indexer_run` and
`_advance_rotation_cursor`.

### The keyword fingerprint is written by a rebuild that proved coverage

`_rebuild_tsvectors_pinned` is **per owner**. Writing the global fingerprint
inside it would claim something a per-owner rebuild cannot establish — that
*every retained row* in `notes_metadata` was rebuilt. Two ordinary shapes
falsify it: user B's rebuild raising after user A's already wrote the
fingerprint, and a scope holding rows the driver never visits at all. Either
way the stored value certifies rows still on the previous configuration, and a
startup that now fails closed would pass while keyword search was exactly as
wrong as before.

So `rebuild_tsvectors_all_scopes(session)` — what `make rebuild-tsvectors`
runs, and beside startup's adoption the **only** writer of `fts_fingerprint`:

1. takes the generation lock **before it reads its first row**, so nothing can
   commit a keyword vector between its snapshot and its record;
2. enumerates the scopes as `SELECT DISTINCT user_id FROM notes_metadata` —
   every scope that **retains rows**, not `_active_user_ids()`;
3. rebuilds each scope through the existing per-owner rebuild, which already
   certifies every row it writes against owner, path and hash;
4. writes the fingerprint in **one transaction with all of them**.

**The per-owner rebuild returns a typed outcome, because `0` already meant two
things.** `RebuildOutcome` is `completed` with a row count, or carries a
`RebuildSkip` — `PROVENANCE_UNSETTLED`, `ROOT_UNPINNABLE`, `ROOT_QUARANTINED`.
A row count of `0` is both "this scope had nothing to do" and "this scope was
**skipped**", and a driver reading `0` as success would record a fingerprint
certifying a scope the rebuild deliberately declined to touch — the exact class
of false claim the coverage proof exists to remove, and invisible from the
outside. **Any retained scope whose outcome is not completed aborts the
driver** (`RebuildCoverageAborted`), names the scope and its reason, rolls every
scope rebuilt so far back, and records nothing.

- **An inactive owner with an assigned vault is rebuilt, not skipped.**
  `_active_user_ids()` and the vault-path cache are keyed to *active* users
  because that is whom the periodic pass serves; the coverage proof asks a
  different question — which rows **exist** — and an inactive user's rows are
  as retained, and as returnable by `keyword_search`, as anyone's.
  `_scope_vault_path` therefore reads `users.vault_path` directly, read-only,
  inside the driver, and pins it the way every other file-reading pass pins a
  root. Nothing about the active-user machinery is widened to do it. Only a
  scope with **no** assigned path, or one whose path cannot be pinned, is
  `ROOT_UNPINNABLE`.
- **NULL-owned retained rows abort under `MULTI_USER_MODE`** (L6). They cannot
  be rebuilt — `_vault_root(None)` refuses in that mode by design, and
  substituting `settings.vault_path` would read one tenant's notes under an
  unowned scope, a tenancy violation performed to satisfy a bookkeeping row.
  Nor may they be silently excluded from the coverage proof: they are retained
  rows `keyword_search` can still return. So the driver names them and their
  count and stops. In single-user mode the ownerless scope is the *only* scope
  and rebuilds normally.
- **All-or-nothing across scopes is a narrow, deliberate carve-out.** The
  standing rule is that the ancillary passes' provenance skip is *per user*, so
  one tenant's unsettled provenance does not block another's indexing. That
  rule is **modified**, not overridden: the carve-out applies only to the
  operator-invoked rebuild that records the fingerprint, it does not weaken the
  per-user gate (a skipped user still gets nothing written), and its reason is
  structural — a fingerprint is one row asserting something about *every*
  retained row, so "all scopes rebuilt" is not a fact that can be established
  one user at a time. The rule's purpose, one tenant not blocking another's
  **ongoing** indexing, is untouched, because this is a one-shot an operator
  ran rather than the loop that keeps the index fresh. The cost is L5, and the
  rebuild is already the cheap path — keyword index only, no provider calls,
  seconds for a few thousand notes.
- `_rebuild_tsvectors_single_scope_for_tests(session, user_id)` stays as the
  single-scope entry point and records **no** fingerprint, for the reason this
  whole subsection exists. It is private, and the name says who it is for —
  see the generation-lock section below.

## The index generation lock (#206)

A startup check is not enough, and the interleaving that defeats it is ordinary
rather than exotic. `make reset-embeddings` runs `docker compose run --rm` on
purpose (#142) so that it reads the edited `.env` and works whether the service
is up or down — and that last property is the hole:

```
old process: read fingerprint A == A, proceed
old process: get_embeddings_batch(note)          <- seconds to minutes
reset:       wipe the column, write fingerprint B, commit
old process: certify_embedded + insert vectors   <- old-model vectors, under B
```

The check and the act are separated by a network call, so the vectors that
interleaving stores are permanently wrong and every later startup is silent,
because the stored fingerprint already matches. The enforcement is therefore a
**transaction-scoped PostgreSQL advisory lock** on one fixed key,
`INDEX_GENERATION_LOCK_KEY` — `8029183045093649969`, the ASCII bytes
`omcpgen1`, **written out literally** in `src/services/index_state.py` and
deliberately never derived at runtime from a hash of a string, a version or a
table name. A key computed at runtime can differ between builds, and two
processes holding different keys are two processes holding no lock at all, with
a failure mode that is silent and permanent.

- **`pg_advisory_xact_lock`, never a session lock.** It releases at commit or
  rollback, so a crashed pass cannot strand it; a session lock leaked into a
  pooled connection would be held by whatever ran next.
- **Every maintenance operation that mutates the generation takes it first** —
  before its wipe, its rebuild or its fingerprint write:
  `scripts/reset_embeddings.py`, the panel's Danger-zone reset paths, and the
  rebuild driver.
- **Every transaction that writes a configuration-dependent derived row takes
  it too**, re-reads the relevant fingerprint under it, and refuses to write on
  a mismatch — certifying nothing, inserting nothing, deleting nothing, and
  leaving the row for a later pass, which is `StaleCertification`'s existing
  disposition. **Acquiring the lock without re-reading buys nothing**: the
  value may have changed while the caller waited for it.
- **`ABSENT` is not a mismatch** on any of these paths, and none of them writes
  a fingerprint. Nothing has been claimed about the stored rows, so there is
  nothing to contradict — and only the maintenance workflows and the startup
  adoption write, which is what stops a refusal from clearing itself.

### The ordering rule is a property of the transaction

> **A transaction that will write any configuration-dependent derived row
> acquires the generation lock and re-validates the fingerprint before its
> first row-locking mutation.**

Stated that way, not as "before the statement that needs the fingerprint",
because the second spelling produces a real deadlock. The incremental index
pass is **one transaction**, and it mutates `notes_metadata` long before it
reaches its tsvector write: the upsert of each changed note, the id-preserving
move UPDATE, the prune DELETE, the `note_links` delete-and-insert, and the
grammar-invalidation `UPDATE … SET embedded_content_hash = NULL` all take row
locks first. Taking the advisory lock at the tsvector write would give:

```
pass:    upsert notes_metadata rows          (holds row locks)
rebuild: pg_advisory_xact_lock               (holds advisory)
rebuild: rebuild those rows                  -> waits on the pass's row locks
pass:    pg_advisory_xact_lock at the write  -> waits on the rebuild's advisory
```

— a cycle the database resolves by killing one side. So the pass calls
`acquire_generation_lock_unbounded(session)` and `_assert_fts_generation_current(session)`
at the **head of `_index_vault_pinned`'s transaction** — ahead of every lock it
takes, with only that helper's own `SET LOCAL statement_timeout` before it — and
anyone adding a mutation to that function must keep it below that line: the
requirement is to audit what the transaction touches, not to reason backwards
from the write that consumes the fingerprint. A mismatch raises
`GenerationMismatch` and the pass commits nothing, exactly as a tsvector floor
failure already does, and retries next tick under whichever configuration is
then current. The abort is deliberately fatal to the pass rather than a skip: a
keyword vector is only ever rewritten when a note's `content_hash` changes, so
a row written under the previous `FTS_CONFIGS` would keep that vector for ever
behind a fingerprint claiming otherwise.

**Lock ordering is one direction everywhere** — the advisory lock before any
row or table lock, in every transaction that takes it — so it cannot close a
cycle with the row locks the pass and the panel already contend for. **Every
writer of `content_tsvector` takes it**, because the same check-then-act gap
exists one step removed on the keyword side: a rebuild can rebuild every
retained row under the new `FTS_CONFIGS` and record the fingerprint while an
old container's *incremental* pass writes `content_tsvector` for a changed note
under the previous one — and a keyword vector is only ever rewritten when a
note's `content_hash` changes, so that row would stay on the old configuration
for ever behind a fingerprint claiming otherwise. In the tree that is the
incremental pass (at the head of its transaction) and the per-owner rebuild
(through its driver, which takes the lock before its first read);
the single-scope entry point is **private and named for its only caller**,
`_rebuild_tsvectors_single_scope_for_tests`. It was `rebuild_tsvectors`, a
public export that commits keyword vectors without taking the lock and without
re-reading the fingerprint under it. It had no production caller, so nothing in
the tree was wrong — but "no caller today" is a fact about today, and a
plausible-looking public function that writes outside the interlock is an
invitation to add one. It survives at all because the tests hold its `int`
contract over a single scope (atomicity, the certified UPDATE predicate, the
provenance gate), and `tests/test_issue_206_verifier_gaps.py` fails if anything
under `src/` or `scripts/` calls it. Anything that does give it a caller must
take the lock at the head of that transaction — and should call the driver
instead.

### On the embed path the window exists only inside `embed_note`

`get_embeddings_batch` and `certify_embedded` are twenty lines apart in the
*same* function, and the caller in `indexer.py` sees only the result: there is
no point between them the indexer can reach. So the acquisition and the
re-read live in `embed_note` itself (`_generation_matches`), **after the
provider call and before the certification** — which is precisely the window
the spec already reserves, so no lock of any kind is ever held across a network
round trip. It is also before the first row lock that per-note transaction
takes (the certification *is* that lock), so here the head-of-transaction rule
and the after-the-provider-call rule agree rather than conflict. Both callers —
the backlog loop and the reconciliation sweep — inherit it, and a mismatch
returns `GENERATION_MISMATCH`: nothing certified, nothing written, not counted
as embedded and **not counted as a failure** (nothing went wrong with the
provider), logged at ERROR. It is an *attempt*, because a provider call was
issued — the `attempted` rule applying unchanged rather than gaining an
exception.

**Discovery SELECTs end their transaction before provider work.** A plain
SELECT takes a table lock even though it takes no row lock. The reconciliation
query's `EXISTS` reads `note_embeddings` and holds `AccessShareLock`; a reset
then takes the generation advisory lock and waits for `AccessExclusiveLock`
while dropping the HNSW index. If the provider returns while that discovery
transaction remains open, certification waits for the reset's advisory lock,
closing a cycle that PostgreSQL breaks by aborting one operation.

The backlog enumeration, reconciliation enumeration, and both per-note ORM
lookups therefore commit their read-only transactions before provider I/O.
The immutable SQL rows retain the verified hash/path snapshots; the session's
`expire_on_commit=False` keeps the loaded note attached and writable for the
later chunk-truncation update. These commits certify nothing. After the
provider returns, `embed_note` opens the certification transaction by taking
the generation lock and re-reading the fingerprint, then uses the same
conditional hash/path certification. A reset can complete during provider
work, and old output is refused without deadlocking or overwriting that reset.
The regression runs the actual reset driver's DROP INDEX, ALTER TABLE and index
recreation, rather than substituting a fingerprint UPDATE that takes no
conflicting table lock.

**An absent `indexer_state` table is checked separately from an absent row.**
`_generation_matches` probes with `state_table_exists` (`to_regclass`) before
reading the fingerprint, so a missing-relation SELECT cannot abort the
transaction after the provider call. Absence proceeds, matching the startup
guard's `ABSENT` disposition. This only handles the missing state table;
it does not certify an unmigrated schema as operational. For example, a
complete revision-022 schema lacks `notes_metadata.chunks_truncated`, which
the backlog query still requires. The tests use an otherwise usable session
double with the state table absent.

**The exclusion branch is exempt, and the exemption is argued rather than
assumed.** It makes no provider call, writes no vector, and stamps a row to
record that an *excluded* note has been dealt with — a claim that is true under
any model, because the correct vector set for an excluded note is the empty
one. It has nothing a generation change can invalidate, so it keeps its
existing `certify_embedded` predicate and its existing per-note commit. Both
branches carry a comment saying so.

**The per-stage fingerprint re-read is a cheap early exit, not the guarantee.**
`_embed_vault_pinned` reads the embedding fingerprint once per user stage
(`_embedding_generation_current`) and, on a mismatch, returns an empty
`EmbedPassResult` — nothing attempted, nothing recorded, no failure — so an old
container abandons the stage within one tick instead of grinding through a
backlog whose every certification the lock will refuse. `ABSENT` proceeds here
too, since a database with no recorded fingerprint makes no claim the stage
could contradict. The lock is what makes the guarantee; the re-read is what
makes it cheap.

### Maintenance waits for an in-flight pass, and that is correct (L5b)

The pass holds the generation lock for the duration of its transaction —
minutes on a large vault — so `make reset-embeddings` and
`make rebuild-tsvectors` **wait** for an in-flight pass instead of interleaving
with it. That is the behaviour we want: a reset must not land mid-pass. The
maintenance paths therefore deliberately do **not** set a short `lock_timeout`
to defeat it.

**And `lock_timeout` was never the only way to defeat it.** `src/database.py`
sets `statement_timeout` to 60 s in the engine's `server_settings`, and
`pg_advisory_xact_lock` is a statement like any other: a wait longer than a
minute is cancelled with a `QueryCanceledError`. Since the pass holds the lock
for minutes, the plain acquisition did not *wait* for it at all — it aborted,
and the operator saw a query-cancelled error that reads as a broken command
rather than as a busy index. An earlier revision of this note argued that the
raise had to come *after* the acquisition, "because a `SET LOCAL` ahead of it
would put a statement before the lock and break the ordering rule". That was
wrong, and it is the reasoning the fix corrects: **the ordering rule is about
row and table locks**, and a `SET LOCAL` takes neither — it is a
session-variable assignment the lock graph cannot see. So every path whose
contract is "it waits" calls `acquire_generation_lock_unbounded`
(`index_state.py`), which lifts `statement_timeout` for the acquisition alone
and puts it back the moment the lock is held — `SET LOCAL statement_timeout =
DEFAULT`, which resets to the *session* default, i.e. the value the engine
delivered in the connection's startup packet, so nothing has to know what that
value is in order to restore the right one. The callers are the two
maintenance commands, both panel Danger-zone resets, and the incremental pass
itself. `scripts/reset_embeddings.py` then raises `statement_timeout` to `5min`
over its destructive DDL, which is the bound those statements were always meant
to run under.

The pass is on that list for the symmetric reason. Its failure direction was
safe — a cancelled acquisition aborts the pass, which commits nothing and
retries next tick — but "waits" is the documented contract on both sides of
this lock, and a pass that abandons every tick for the duration of a long
rebuild writes an `indexer_runs` error row per tick about a database that is
merely busy. `embed_note`'s per-note acquisition keeps the plain, capped form
deliberately: that transaction must not sit on a lock for minutes, and its
`GENERATION_MISMATCH`/retry disposition is already the right answer there.

**The runbook, and why it inverts the old advice.** For any change to the
embedding provider, model, dimensions, chunk size, chunk overlap or the chunk
cap: edit `.env` → `make deploy` (the new image refuses at the fingerprint or
dimension guard and stays down, embedding nothing) → `make reset-embeddings`
while it is down → restart. The pre-fingerprint advice told operators to reset
*before* recreating; that ordering was safe only because nothing then depended
on a stored claim. For `FTS_CONFIGS`: edit `.env` → `make deploy` → the new
container refuses at the keyword fingerprint guard → `make rebuild-tsvectors`,
which rebuilds every scope holding rows and writes the fingerprint only if
every one of them completed → restart. **The lock is what makes an operator who
ignores the runbook lose time rather than correctness.**

*One key, not two.* The two subsystems guard one fact — which configuration the
derived rows were built under — and a single key makes the ordering rule
trivially total. The cost is that an embed certification and a tsvector write
serialise against each other across processes; within a process
`index_pass_lock` already did, and across processes serialising them is the
point.

*Rejected — have the rebuild refuse while an old writer could run.* There is no
way to ask that question: "an old writer" is another container the database
cannot enumerate, and a heuristic over `pg_stat_activity` would be a guess
whose failure direction is silent, permanent staleness.

## What this change does not do (#200, #201, #202, #206)

Every residual is listed here, so none of them is discovered later as a defect.

- **L1 — a model artifact can change under an unchanged name.** Endpoint
  identity is excluded from the fingerprint, so re-pulling a mutable tag or
  repointing at a host serving different weights mixes vector spaces
  undetected. **This is the one an operator can trip with no warning of any
  kind**, because nothing available to the process can see it: the recourse is
  the documented operator rule at the model keys — a change of model artifact
  requires `make reset-embeddings`.
- **L2 — an edit the scan has not yet committed is served as fresh.** The
  staleness signal is derived from `notes_metadata`, so it reports what the
  index knows. Bounded by `INDEX_INTERVAL_SECONDS` plus the pass in flight; see
  [search.md](search.md).
- **L3 — the scan and the one-shot link backfill are unbudgeted.** A tenant
  with an enormous vault still delays the next tenant through those stages.
  Each is a single transaction over a vault walk, so budgeting one means
  committing a partial derive (A.7a forbids it) or discarding the pass's work.
- **L4 — the cross-tenant delay is the budget plus one note's embedding time**,
  ~8.3 h arithmetic worst case on a provider answering at its timeout. The only
  tighter bound is an aggregate deadline, which is what #127 removed.
- **L5 — a multi-tenant keyword rebuild is all-or-nothing**, and a scope that
  cannot be rebuilt blocks the fingerprint and therefore, with FTS failing
  closed, blocks startup after an `FTS_CONFIGS` change. Reachable by an
  *unassigned* user's leftover rows, a tenant still in re-derive mode, a
  quarantined root, or ownerless rows under multi-user mode — **not** by an
  inactive-but-assigned user, whose scope the driver resolves and rebuilds.
  Recourses: settle the scope, delete or reassign the rows, or revert
  `FTS_CONFIGS`.
- **L5b — the incremental pass holds the generation lock for its whole
  transaction**, so the maintenance commands wait minutes on a large vault.
  Waiting is the correct behaviour and the maintenance paths do not defeat it.
- **L6 — NULL-owned `notes_metadata` rows abort the rebuild** while
  `MULTI_USER_MODE` is on. Delete or reassign them.
- **L7 — raising `MAX_CHUNKS_PER_NOTE` forces a full re-embed**, although it
  only widens coverage. A comparison rule that knew which direction was safe
  would have to reason about every field jointly: a larger cap with a smaller
  chunk size is not a widening.
- **L8 — the first startup after this change adopts whatever is configured.**
  There is no prior evidence to compare against, and refusing would take every
  existing deployment down on upgrade. It shipped with the embedding and FTS
  configuration unchanged, for that reason.
- **L9 — a capped note's tail is not semantically searchable at all.** That is
  what the cap is; the note stays fully keyword-searchable, and the truncation
  is marked on the row, in every vector result and on the dashboard.
- **L10 — `semantic_search` still hydrates every candidate's full vector** to
  recompute a similarity the query already returned as `distance`. Pre-existing
  and unrelated to these four findings; filed as a follow-up rather than
  widened into a change that already touches both read paths.

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

### Version 2 is a LINK-grammar bump (#203), and it re-embeds nothing

The rule above generalises, and version 2 is the case that showed it: **bump
the constant in the same commit as any change to the fence grammar *or the link
grammar*.** Both are invisible to `content_hash` for exactly the same reason —
the bytes on disk do not move — and both leave derived state stale in exactly
the same way. The marker is the mechanism the index already has for this; there
is no second one, and there should not be.

Version 2 ships the linear, bounded wikilink and markdown-link grammars, so
every note's `note_links` rows have to be re-derived once. The **fence** grammar
did not change, so version 2's entry in `_EXTRACTION_CLEANERS` is version 1's
function under a new key. That is not redundancy: it is what makes
`_grammar_changed_the_embedding_text` compare equal for every v1-stamped row, so
the pass that re-extracts links and tags for the whole vault makes **no embedding
call on account of the bump**. The cost is one long link-and-tag pass, not a
re-embed of 2,577 notes. `move_note(rewrite_links=True)` is refused for that
user until the pass completes, per the existing requirement — which is the
correct disposition for a grammar change, since the rewrite planner and the
extractor must agree about what a link is.

### The frozen v0 cleaner is a line scanner with the regexes as its oracle

`_v0_clean` must keep producing byte-identical output forever — the
`extraction_version` comparison above and the rollback recipe below both depend
on it — and it was quadratic in the number of unclosed fence openers, because
each `.*?` walked to end of input before the attempt failed and `^` retried at
the next line. A note of ```` ```x\n ```` repeated is ordinary, in-cap input,
and this ran on the event loop inside the indexer.

It is now a line scanner (`_v0_sub_fences`), and the **original regexes live in
`tests/test_asvs_v0_cleaner.py` as the differential test's oracle** — the test
is the proof of equivalence, not the comment. Two clauses are worth repeating
here because they are the opposite of the v1 fence grammar and a future reader
will otherwise "fix" them:

- **Split on `\n` only.** Never `str.splitlines()` (which breaks on `\v`,
  `\f`, `\x1c`, ` `, …) and never the shared recognizer's `_LINE_BREAK_RE`
  (which treats a lone `\r` as a terminator). Under `re.MULTILINE` a lone `\r`
  is an ordinary character, so it must stay one here.
- **A closer's trailing run is `\s` under Unicode semantics.** ```` ```\xa0 ````
  and ```` ```\x0b ```` *do* close a v0 block — where the v1 grammar admits
  only U+0020 and U+0009.

### Rollback is roll-forward, and the plan says so

**A bare redeploy of the previous image does NOT restore derived state.** Old
code ignores `extraction_version` and skips unchanged notes by `content_hash`,
so links, tags and vectors stay derived under the new grammar indefinitely —
the same silent staleness, pointing the other way.

The rollback procedure is:

1. revert the grammar commits on a branch;
2. **bump `CURRENT_EXTRACTION_VERSION`** in that build (to the next unused
   number — 2 is taken by the link-grammar bump above) and
   keep the versioned mechanism and the frozen per-version registry — the
   registry is what lets the pass compare each row's stamped grammar against
   the restored one;
3. deploy.

The owner-scoped re-derivation pass then rebuilds every note's links and tags,
and (span-diff-scoped, direction-aware) its embeddings, under the restored
grammar, without touching `content_hash`. Comparing legacy-to-legacy instead —
which is what a registry without the frozen v1 entry would do — would certify
the stale vectors for ever.
