## Context

One uvicorn worker, one asyncio loop, one `index_pass_lock`, and **one sequential indexer pass that serves every tenant** (`run_indexer_loop`, `src/services/indexer.py:3344` startup and `:3425` periodic). The four findings are four ways that pass lies about, degrades, or monopolises the derived index. They are grouped into one change because they share the same three call sites — `embed_note` (`src/services/embeddings.py:476`), `_embed_vault_pinned` (`src/services/indexer.py:2483`) and the two vector read paths — and because fixing one without the others produces a worse intermediate state: annotating staleness (#200) without recording the outage (#201) tells an agent the vault is stale and tells the operator nothing.

Constraints that shape every decision below:

- **`embed_note` refuses partial certification** (`index-integrity`, "A many-chunk note completes, and certifies only on full coverage"). Anything that stops a note *mid-chunks* leaves it uncertified, the backlog re-selects it every tick, and that is #127's permanent-burn defect — a 300 s budget that could only fire on a note it could never finish. The fix for #202 must therefore never preempt inside a note.
- **`certify_embedded` is the single stamping path** (`embeddings.py:411`), predicated on `id + file_path + content_hash`, stamp-before-delete, per-note commit. Every new outcome must route through it or write nothing (`index-integrity`, "The embedding pass is not gated on provenance, because it verifies every hash it certifies").
- **The `#160` asymmetry is deliberate**: swallowed per-note failures flip the `indexer_runs` row's `error`, not the in-process `_record_index_run` heartbeat. "Is the loop alive" and "did the work succeed" are different questions and collapsing them changes #78's semantics.
- **A degradation is declared, never silent** (#203): a durable marker on the row, one ERROR line, and an agent-visible field — because the ops-health ring buffer is 100 entries and process-lifetime (`src/services/error_log.py:66`) while the degraded rows persist.
- **`_reconcile_exclusions` runs on every embed pass** (`indexer.py:2676`), calls `chunk_text` as a zero-chunk probe (`:2796`) and `embed_note` for re-inclusions (`:2807`). Every change to those two functions lands there too.
- **Both vector tools return markdown strings, not structured data** (`tools.py:1164` `semantic_search_impl`, `tools.py:1935` `find_related_impl`). A new field is a rendered token, and `get_links_impl` (`tools.py:1721-1769`) is the house pattern for one.
- **`index_pass_lock` must not be split** — the #12 reindex mutual-exclusion invariant depends on it being one lock.

## Goals / Non-Goals

**Goals:**
- The `indexer_runs` row is true: a pass that embedded nothing because the provider was down says so, and its `notes_embedded` excludes the notes it did not embed.
- No vector-search result presents superseded text as the note's current content, and no result disappears because it is stale.
- One tenant's backlog cannot deny another tenant's index freshness for longer than one budgeted pass, with no note preempted mid-chunks.
- A configuration change that silently invalidates every stored vector fails the process at startup, with a named remedy; one that merely degrades keyword recall warns and keeps serving.

**Non-Goals:**
- **Filtering stale rows out of vector search.** Rejected in the issue and here: on `embedded_content_hash = content_hash` it would hide every note edited in the last five minutes and the entire vault during an outage, and — because both paths run the O(n) exact fallback on *any* zero-row filtered result (`search-quality`, "The owner predicate makes every vector query a filtered query") — it would convert an outage into a sequential scan of the whole embedding table on every query.
- **Per-row embedding fingerprints.** `provider`, `model`, `dimensions`, `chunk_size` and `chunk_overlap` are global settings; a per-row copy is 16,700 identical strings whose only use would be a lazy per-note re-embed, and *that* is the design that leaves two vector spaces coexisting in one HNSW index for the whole migration window. Cosine distance between two spaces is meaningless, so a partially-migrated index answers wrongly for longer than a refused startup does. `extraction_version` is per-row precisely because its repair is per-note and scoped; this one's repair is a wipe.
- **Splitting `index_pass_lock`, or a per-tenant worker.** Out of scope and forbidden by #12.
- **Batching Ollama `/api/embed` inputs.** A throughput win, not a fairness fix (the issue says so), and it changes the provider contract `embedding-providers` pins.
- **A `LIMIT` on the backlog SELECT** (`indexer.py:2493`). The budget below bounds the *work*, which is what starves other tenants; a `LIMIT` bounds only the row fetch, and a fetch of every id/path/hash triple for one tenant is not the cost.
- **Bounding `semantic_search`'s hydration of the full `NoteEmbedding` row** (`embeddings.py:613`, which loads every 1024-float vector for `overfetch` rows to recompute a similarity the query already returned as `distance`). Real, unrelated to these four findings, recorded as a residual for a follow-up.
- **A second "coverage" definition.** The dashboard counts notes with *any* vector (`routes.py:398-407`); the poller counts notes with a *current* one (`routes.py:2092`). This change shows both rather than redefining either — silently changing what the gauge means would make every historical screenshot wrong.
- **Per-user `FTS_CONFIGS` or per-user embedding settings.** Global today (README:966), global after this.

## Decisions

**D1 — `embed_note` returns a typed outcome; `embed_vault` counts only the certifying ones.**
Today `embed_note` returns `0` for three unrelated things — a note that cleaned to zero chunks and *was* certified (`embeddings.py:505-519`), a provider exception it swallowed (`:521-525`), and a vector/chunk cardinality mismatch (`:527-532`) — and `_embed_vault_pinned` does `outcome.embedded += 1` unconditionally afterwards (`indexer.py:2630`). The return becomes a frozen dataclass `EmbedNoteResult(outcome: NoteEmbedOutcome, chunks: int, truncated: bool)` over an enum with exactly these members:

| outcome | certifies | counts into `notes_embedded` | counts as a failure |
| --- | --- | --- | --- |
| `EMBEDDED` | yes | yes | no |
| `CERTIFIED_EMPTY` (cleaned to zero chunks) | yes | yes | no |
| `PROVIDER_FAILED` (the swallowed exception) | no | **no** | **yes** |
| `PROVIDER_CARDINALITY_MISMATCH` | no | **no** | **yes** |

The exclusion branch, the hash-mismatch skip and `StaleCertification` stay where they are in `embed_vault` and are unchanged: they never reach `embed_note`, and the architecture note already declares them deliberate decisions rather than failures. `EmbedPassResult.record_failure` is called for the two failing outcomes, so `failure_summary` ("embed failures: N of M — first: …") reaches `PassStats.errors` and the run row's `error` exactly as an escaped exception does today.

*Alternative rejected — re-raise from `embed_note` and let `embed_vault`'s existing `except Exception` fire.* It is smaller, and it is wrong in two places. `_reconcile_exclusions` also calls `embed_note` (`indexer.py:2807`) and its declared convergence exception is that "a row whose provider call fails SHALL be left unstamped … and retried on a later pass" (`index-integrity`, exclusion reconciliation) — a raise there would have to be re-caught to preserve that, so nothing is saved. And a raise makes the *type* of a provider blip indistinguishable from a database error at the call site, which is the conflation this decision exists to remove.

*Alternative rejected — keep the int and add an out-parameter or a counter on the session.* An int that means four things is what produced #201; a second channel beside it means the two can disagree.

`EmbedNoteResult.__int__` returns `chunks`, so `total_chunks += result` and the test suite's bare-int expectations keep working — the same accommodation `EmbedPassResult.__int__` already makes (`indexer.py:148`).

**D2 — Stale rows are annotated and their chunk preview is withheld; every other field is kept.**
The predicate is `embedded_content_hash IS DISTINCT FROM content_hash` — `IS DISTINCT FROM`, not `!=`, so a NULL `embedded_content_hash` (never embedded, or invalidated by a move) is stale rather than NULL-propagating to "not stale".

`semantic_search` already hydrates the whole `NoteMetadata` entity (`embeddings.py:613`), so both hashes are in hand and the statement is unchanged. `find_related_stmt` (`tools.py:1912-1931`) is a six-column projection and gains two: `NoteMetadata.content_hash` and `NoteMetadata.embedded_content_hash`. It is the statement `tests/integration/test_search_recall.py` EXPLAINs, and adding two scalar columns from an already-joined table changes no plan.

**Why the preview and nothing else.** Of the fields a vector result carries, `path`, `title` and `tags` come from `notes_metadata`, which the *scan* refreshed — a row is stale precisely because the scan already committed the new `content_hash`, so those fields describe the note as it stands now. `similarity`/`distance` is a retrieval score, not a claim about content. `chunk` is the only field that is a **verbatim quotation of the note's text**, it is the only one that is out of date, and it is the one an agent will paste into an answer. Withholding it converts a silently wrong answer into a visibly degraded one whose remedy — `read_note`, which reads the file — is one call away and always correct. Keeping the row is what preserves the retrieval the issue insists on: the note is still *found*, still ranked, still named.

The withheld preview is replaced by an explicit notice in the same position, so the shape of the result does not change:

```
Found 12 semantic matches for 'consensus protocol' — 2 stale:

- **Raft notes** (`Projects/Raft.md`) [distributed] — similarity: 0.812
  > Raft elects a leader by randomized timeout…
- **Paxos notes** (`Projects/Paxos.md`) [distributed] — similarity: 0.774 — stale: true
  > (preview withheld — this note changed after it was embedded; the match is
    against superseded text. Call read_note for the current content.)

**2 of these notes changed after they were embedded.** Their previews are
withheld and their ranking reflects their previous content; the paths and
titles are current. Read them with `read_note`.
```

The header carries the stale count **always, including `— 0 stale`**, for `get_links`'s reason: an absent token is not evidence of absence, and an agent cannot distinguish "no stale rows" from "this build does not report staleness". Per-row, only `stale: true` is rendered — the header count is the always-present signal, and `stale: false` on every one of fifteen rows is noise, not information.

`find_related` gets the same treatment plus one more line: **the source note can itself be stale**, in which case the averaged query vector is an average of the note's *previous* content and every neighbour is an answer to a superseded question. That is invisible in the per-row flags, so the result states it once, above the list. This goes beyond what #200 asks for and is included because it is the same silence in the same tool.

*Alternative rejected — keep the preview and only flag it.* The flag is metadata; the preview is content. An agent that summarises three results into a paragraph quotes the previews and drops the metadata, which is exactly the failure `CLAUDE.md` ranks second.
*Alternative rejected — return the note's current first 500 characters instead.* It would be a different span from the one that matched, presented where the matching span goes: a fabricated excerpt, worse than none.

**D3 — `MAX_CHUNKS_PER_NOTE`, applied in `chunk_text`, as a declared degradation that still certifies.**
`chunk_text` (`embeddings.py:222`) gains a bounded sibling in the shape #203 established for links: `chunk_text_bounded(content, *, chunk_size, overlap, max_chunks) -> tuple[list[str], bool]`, returning the first N chunks in document order and whether it truncated. `MAX_CHUNKS_PER_NOTE` is a module constant in `src/config.py` beside `MAX_LINKS_PER_NOTE`, not a settings field — the same call the link cap made.

A capped note:
- has its **first N chunks embedded and is certified**, through the unchanged `certify_embedded` predicate. It is emphatically **not** held uncertified: an uncertified note is re-selected by the backlog on every tick for ever, which is #127's permanent burn arriving by a new route. `MAX_NOTE_BYTES` is 10 MiB and `chunk_size` is 512 tokens, so an over-cap note is reachable with legal input.
- sets `notes_metadata.chunks_truncated` (migration 023), cleared when a later embed of that note fits under the cap — exactly `links_truncated`'s lifecycle. The exclusion branch and the zero-chunk certification clear it too, because both leave the note with no vectors at all.
- emits **one ERROR line** naming the path and the cap (ERROR, not WARNING, so it reaches the ops-health buffer), and never the true chunk count — computing that needs the unbounded chunking the cap exists to prevent.
- is marked in every vector-search row for that note (`embedding_truncated: true`), because the tail of the note is not semantically searchable at all and a result from its head reads as a result from the whole note.

Cardinality is unchanged in substance and re-stated in scope: the pass accepts a batch only when it holds exactly one vector for every **requested** chunk, and the requested chunks are now the capped list.

**Why a cap at all, given #127 removed the aggregate deadline.** #127 removed a *time* budget that fired on healthy chunks; this is a *count* bound that changes what is embedded, deterministically, and says so. The two are not the same trade: a note the deadline killed never certified and burned the same 300 s every tick for ever; a capped note certifies once and is never re-selected.

The zero-chunk probe in `_reconcile_exclusions` (`indexer.py:2796`) uses the bounded form as well, so "has chunks" means the same thing in both places and the probe stops at the first chunk instead of chunking a 10 MiB note to find out it is non-empty.

**D4 — `_active_user_ids()` is ordered; the periodic and startup passes rotate from a cursor persisted in `indexer_state`.**
`_active_user_ids()` (`indexer.py:3130`) has no `ORDER BY`, so its order is whatever the planner returns — stable enough in practice that the same tenant goes first every cycle, and unspecified enough that nothing can be asserted about it. It gains `ORDER BY users.id`, which alone makes the order a fact.

Rotation is applied by a new `_rotated_user_ids()` used by `run_indexer_loop` only — the startup pass (`:3358`) and the periodic tick (`:3431`). It reads `indexer_state['embed_rotation_cursor']`, and returns the ordered list rotated to begin at the smallest id **strictly greater** than the stored one, wrapping. After each user's per-user sequence finishes — success or failure, in a short session opened by the lock holder after that user's pass has closed its own, the discipline `_write_indexer_run` already follows (`indexer.py:262`) — the cursor is written to that user's id. Writing it can never fail the pass; it is logged and swallowed.

**The cursor stores a user id, not an index.** An integer offset into a list whose membership changes when a user is added, deactivated or deleted points somewhere else on the next cycle; "start after id 7" is well-defined whether or not user 7 still exists, because the successor query does not require it to.

**Rotating a freshly re-fetched list is what makes this worth a table.** In-process state resets on every restart and every deploy, and a deploy recreates the container: the tenants at the tail of the order are exactly the ones a restart-truncated pass never reaches, so an in-memory cursor would be reset precisely when it was about to pay off. That is why it is persisted, and it is persisted next to the fingerprints because both are single facts about the index as a whole.

`_reindex_background` (`routes.py:2129`) and `scripts/rebuild_tsvectors.py` keep calling `_active_user_ids()` unrotated: an operator-triggered reindex is not the starvation vector, and giving it a cursor would let a panel click move the periodic pass's rotation.

**D5 — A per-user chunk and time budget, checked only at the note boundary, and only where fairness exists to be had.**
Two settings, `EMBED_CHUNK_BUDGET_PER_USER` (default 5,000) and `EMBED_TIME_BUDGET_SECONDS_PER_USER` (default 300, matching one `INDEX_INTERVAL_SECONDS`); `0` disables either. The budget is consumed by both the backlog loop and the reconciliation sweep, since both call the provider, and is checked at exactly the two places `_is_paused()` already sits — `indexer.py:2530` and `:2744` — **before** a note is started, never inside one.

Three clauses are load-bearing:

- **Never mid-note.** `embed_note` refuses partial certification, so a note abandoned between chunks is uncertified, re-selected next tick, and re-burns everything it did — #127, exactly. Checking only at the boundary means the overrun is bounded by one note, which D3 has already bounded to `MAX_CHUNKS_PER_NOTE`.
- **At least one note, always.** The check runs *after* the first note of that user's pass, so a tenant whose very first note exceeds the whole budget still advances by one note per pass instead of zero for ever. Without this clause a small budget is a livelock.
- **Only when the pass serves more than one user scope.** In single-user mode, and in a multi-user deployment with one active user, there is no other tenant to be fair to; a budget there would turn an initial index of 2,577 notes into several passes separated by five-minute sleeps for no benefit, and would look like a stall. This is the clause that makes the default deployment's behaviour identical to today's.

A budget stop is **not** a failure and does not touch `error` — it is a deliberate decision, the same class as a pause, and writing it into `error` would fire #201's own outage signal on a healthy server. It logs once at WARNING per user per pass. The operator-visible signal for a tenant that is permanently over budget is the dashboard's **pending count** (D7), which stays high across passes: a persistent backlog is what the operator needs to see, and it is a property of the index rather than of one pass.

*Alternative rejected — a `LIMIT` on the backlog SELECT.* It bounds the fetch, not the provider calls, and it interacts badly with the `ORDER BY modified_at DESC` the backlog already has: the oldest edits would never be reached.

**D6 — One `indexer_state` key/value table (migration 023) holding two fingerprints and the cursor.**
```
indexer_state
  key         VARCHAR(64)  PRIMARY KEY
  value       TEXT         NOT NULL
  updated_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
  CHECK (key IN ('embedding_fingerprint', 'fts_fingerprint', 'embed_rotation_cursor'))
  table comment: 023's ownership marker
```

*Why a table and not columns on an existing row.* There is no singleton row to hang them on: `users` is per-tenant, `notes_metadata` is per-note (and a per-note copy is D-Non-Goal), and `indexer_runs` is an append-only display history that this change must not start reading for a decision — its own docstring says nothing reads it for one. The three facts share a lifecycle ("state about the index as a whole, written by the pass or by a maintenance command") and differ in shape, which is what a key/value table is for.

*Why the CHECK, given a key/value table.* A mistyped key reads as **absent**, and absent means *adopt* for a fingerprint (below) — so a typo silently disables the guard that exists to prevent a permanent, silent corruption. `ck_indexer_runs_trigger` exists for the weaker version of the same argument ("a typo'd trigger would render as a silent fourth category nobody notices"). Adding a key is then a migration, which is correct: each of these keys has a startup or a scheduling consequence.

The migration follows 022's device exactly — reconcile-or-refuse against `pg_class`/`pg_constraint`, an ownership marker as the table comment mirrored on the ORM model so `alembic check` compares it, `downgrade()` drops only what carries the marker, `lock_timeout`/`statement_timeout` set and `RESET` at the end. It also adds `notes_metadata.chunks_truncated BOOLEAN NOT NULL DEFAULT FALSE` with its own column marker, `links_truncated`'s shape for `links_truncated`'s reasons: the constant server default keeps it a catalogue-only `ADD COLUMN` on a table carrying a `tsvector` and two GIN indexes, and `false` is the *true* value for every pre-existing row, since every existing vector set was produced by an uncapped chunker.

**023 backfills nothing.** Neither fingerprint is derived from the current settings at migration time, for 016's reason inverted: a stamp written by the migration would assert that the stored rows were produced by the configuration the `.env` carries *now*, which is exactly the claim the fingerprint exists to test.

**D7 — The fingerprint format, and the comparison rule.**
Both values are canonical JSON — `json.dumps(obj, sort_keys=True, separators=(",", ":"))` — built by two functions in a new `src/services/index_state.py` (a new module, so `embeddings.py` and `indexer.py` can both import it without a cycle):

```
embedding_fingerprint() -> {"v":1,"provider":"ollama","model":"bge-m3",
                            "dimensions":1024,"chunk_size":512,"chunk_overlap":0}
fts_fingerprint()       -> {"v":1,"configs":["english"]}
```

- `model` is the **active** provider's model — `settings.embedding_model` for `ollama`, `settings.openai_embedding_model` for `openai` — selected by the same branch `get_provider()` takes. Reading the inactive one is the exact bug this guard exists to catch, so the selection lives in one function.
- `configs` is `sorted(settings.fts_configs)`. Index-time tsvectors are `||`-concatenated and query-time tsqueries are OR'd, and both operators are order-insensitive over lexeme sets, so `["english","norwegian"]` and `["norwegian","english"]` produce identical stored vectors. Comparing them as ordered lists would warn about a reordering that changed nothing.
- **`OPENAI_BASE_URL` is deliberately excluded.** Moving between `api.openai.com`, an Azure deployment and a compatible proxy with the same model name is an infrastructure change, and including it would demand a full vault re-embed for it. The residual — a proxy that serves something *else* under a name it borrows — is an operator-trust question the fingerprint cannot answer anyway, and is recorded as a risk.
- Canonical JSON rather than a `|`-joined string: a model name may contain any character, so a delimiter needs an escaping rule that would then have to be specified, versioned and tested. JSON has one already, it parses on both sides so the mismatch message can name **which field changed** rather than printing two opaque strings, and `sort_keys` makes exactly one spelling per value — the property byte-equality comparison needs, and the reason `indexed_vault_realpath` chose hex over base64.
- The `v` field is what makes adding a future field a deliberate act: it changes every fingerprint, so the change that adds one must ship a migration that rewrites the stored value or an instruction to reset. Recorded as a risk rather than solved in advance.

**Comparison, at startup, in `_check_embedding_dim`'s shape** (`src/main.py:40`) and inserted immediately after `_validate_fts_configs()` in lifespan (`main.py:275`) — after it deliberately, so a typo'd config name still fails with `validate_fts_configs`'s own message listing the installed configs, rather than as an opaque fingerprint diff. Both are inside the sandbox-mode short-circuit at `main.py:263`, so `MCP_SANDBOX_MODE` skips them like every other guard.

| stored | embeddings | FTS |
| --- | --- | --- |
| absent | **adopt**: write the current fingerprint, log at WARNING that it was assumed and not verified | same |
| equal | proceed silently | proceed silently |
| different | log at CRITICAL naming both fingerprints and the differing fields, point at `make reset-embeddings`, `sys.exit(1)` | log at **ERROR** naming both and pointing at `make rebuild-tsvectors`; **start anyway** |
| table absent | return, deferring to alembic (`_check_embedding_dim`'s stance for a missing column) | same |

**Why embeddings fail closed and FTS does not.** Two vectors from different models in one column are not "less accurate": cosine distance between them is meaningless, HNSW has already built a graph over both, and the result is arbitrary and permanent. A stale stemmer is a different kind of wrong — `'running'` stored as `run` still matches a real lexeme of the real note, so keyword search is *incomplete*, self-heals on the next content change, and is fully repaired by a cheap, no-API-call rebuild. Refusing to serve for that would turn a config edit into an outage, which is `_check_mount_identity_support`'s precedent at `main.py:168` — warn and keep serving where the damage is bounded to one capability.

**Only the maintenance flows write a fingerprint after adoption**, and this is what stops a warning from silencing itself. `scripts/reset_embeddings.py` writes `embedding_fingerprint` in the same transaction that recreates the column and clears every `embedded_content_hash`; the panel's Danger-zone reset does the same under the pass lock; `rebuild_tsvectors` writes `fts_fingerprint` inside its single atomic transaction, so a rebuild that rolls back does not claim to have rebuilt. If startup rewrote the FTS fingerprint after warning about it, the second boot would be silent while the index stayed stale — a guard that fires once and then lies.

The dimension guard stays. It reads the live column width from `pg_attribute`, a physical fact about the table; the fingerprint reads the configuration the rows were generated under, a logical fact about their content. A restored dump into a differently configured deployment trips the first; a same-dimension model swap trips only the second.

`.env.example:127` (`EMBEDDING_MODEL`) and `:140` (`OPENAI_EMBEDDING_MODEL`) get the file's existing sentence, verbatim in form: *"Changing post-deploy requires `make reset-embeddings`."* README's "Switching providers" section (`README.md:915`) is widened to name a model change within one provider, and the config table's two model rows carry the same pointer.

**D8 — The dashboard shows currency beside coverage, and does not redefine coverage.**
`stats.embedding_pct` counts notes with at least one vector row (`routes.py:398-407`); the reset poller counts notes whose `embedded_content_hash` equals their `content_hash` (`routes.py:2092`). They are different questions and both are worth answering, so the dashboard gains `notes_pending` (stale or never-embedded) and `notes_chunks_truncated` beside the existing bar, and the bar's meaning is unchanged.

The poller's query is admin-only and **unscoped** — it counts the whole table. The dashboard is scoped by `_scope_user_id(user)`, so the new counts must be scoped the same way as the coverage numbers directly above them, or a regular user reads another tenant's backlog as their own. The shared predicate goes in one helper used by both call sites so the two cannot drift; the poller keeps its unscoped, admin-only behaviour.

A non-zero pending count on an otherwise-idle vault is exactly the shape a provider outage now takes, and it is the operator-visible signal D5's budget stop deliberately does not write into `error`.

## Degradation markers

Every bound this change adds is declared in the same four places, matching #203's:

| Degradation | Durable marker | Log | Agent-visible surface | Certification |
| --- | --- | --- | --- | --- |
| Link extraction over `MAX_LINKS_PER_NOTE` (#203, shipped) | `notes_metadata.links_truncated` | one ERROR per capping | `get_links` → `truncated: true` | note is **not** a skip; re-derive still certifies |
| Chunking over `MAX_CHUNKS_PER_NOTE` (#202, this change) | `notes_metadata.chunks_truncated` | one ERROR per capping | `semantic_search` / `find_related` → `embedding_truncated: true` | note **is** certified; never re-selected while unchanged |
| Vectors older than the note's bytes (#200) | none needed — `embedded_content_hash != content_hash` **is** the marker | none (it is a normal, self-healing state) | `stale: true` per row, a stale count in the header, preview withheld | unchanged; the backlog owns the repair |
| Provider outage (#201) | `indexer_runs.error` via `EmbedPassResult.failure_summary` | WARNING per note, one aggregate ERROR | dashboard pending count; no tool-level marker (every affected row is already `stale`) | nothing is certified, by construction |
| Per-tenant budget exhausted (#202) | none — deliberately | one WARNING per user per pass | none (it is not a wrong answer, only a late one) | nothing is certified early |
| Keyword vector retreat (#127, shipped) | none | one line with the prefix length | none | unchanged |

The row that has no durable marker is the budget stop, and that is the decision: a marker would have to live somewhere, and the only honest place is the pending count, which already exists.

## Risks / Trade-offs

- **[D2 withholds a preview an agent was using]** → the row, its rank, its path and its title are all still there, and `read_note` returns the true text. During an outage every preview is withheld, which is loud — that is the point, and the alternative is quoting superseded text for the length of the outage.
- **[D2's staleness is a 0–5 minute normal state]** → a note edited a minute ago is stale by construction until the next pass. The header count makes that visible without making it alarming, and the notice names the cause. If the steady-state noise proves too high, the remedy is a shorter `INDEX_INTERVAL_SECONDS`, not a threshold on the marker — a time threshold would re-introduce a silent window.
- **[D3's cap changes what is searchable]** → a note over `MAX_CHUNKS_PER_NOTE` is ~2 MB of prose; its tail becomes semantically unsearchable while staying fully keyword-searchable (the tsvector path has its own, separate 100,000-character floor and retreat). Marked on the row and in every result.
- **[D5's budget delays a legitimate large import]** → bounded to one budget per pass per tenant, only when more than one tenant is active, with the pending count showing the backlog. `0` disables.
- **[D6's CHECK makes a new key a migration]** → intended; see D6.
- **[D7 adopts on absence, so the first startup blesses whatever is configured]** → if an operator changes the embedding model *in the same deploy* that ships this change, the guard adopts the new fingerprint over old vectors and never fires. Unavoidable — there is no evidence to compare against — and stated in the migration plan: **deploy this change with the embedding configuration unchanged.** After that first startup the stored value is real.
- **[D7 excludes `OPENAI_BASE_URL`]** → a proxy serving a different model under a borrowed name is not detected. Named as a non-goal above rather than half-solved.
- **[D7's `v` field]** → adding a sixth field later invalidates every stored fingerprint and would refuse every deployment's startup. Any change that adds a field must ship the rewrite or the reset instruction with it; the `v` is there so the omission is visible in review.
- **[D1 changes `embed_note`'s return type]** → `__int__` keeps `total_chunks += result` and the suite's bare-int expectations working, but every call site is enumerated in the tasks and each is changed deliberately rather than left to duck typing.
- **[Migration 023 on a deploy]** → one new table plus one additive constant-default column; `make test-schema` is the gate and `alembic check` must be clean after.

## Call sites this change touches

Enumerated so no slice discovers one late.

- **`chunk_text`** — `embeddings.py:504` (`embed_note`), `indexer.py:2796` (`_reconcile_exclusions` zero-chunk probe). Only those two in production.
- **`embed_note`** — `indexer.py:2622` (backlog), `indexer.py:2807` (reconciliation sweep). Both consume the new result.
- **`certify_embedded`** — `embeddings.py:510`, `:541`, `indexer.py:2555`, `:2767`. Unchanged, but the capped path must still route through it.
- **`_active_user_ids()`** — `indexer.py:3358` (startup, rotates), `indexer.py:3431` (tick, rotates), `routes.py:2129` (`_reindex_background`, does **not** rotate), `scripts/rebuild_tsvectors.py:19,30` (does not rotate).
- **`_is_paused()`** — `indexer.py:2530` and `:2744` gain the budget check beside them; `:3276` (prewarm) and `:3417` (tick) are untouched.
- **`semantic_search`** (service) — one caller, `tools.py:1170`.
- **`find_related_stmt`** — `tools.py:2005` and `tests/integration/test_search_recall.py`.
- **Startup guards** — `main.py:273-275`; the two new checks are appended in that block.
- **Fingerprint writers** — `scripts/reset_embeddings.py:43` (before commit), `routes.py:2007-2013`/`:2028`/`:2057` (the panel's reset paths), `indexer.py` `_rebuild_tsvectors_pinned` (~`:2907`, inside its single transaction).
- **Dashboard** — `routes.py:389-425` and `:483-489`; `templates/dashboard.html:110-125`.

## Invariants each delta must preserve

Named, so a reviewer can check them one by one.

1. **"A many-chunk note completes, and certifies only on full coverage"** (`index-integrity`) — D3 narrows *what is requested*, never what is required of a batch; D5 never preempts inside a note.
2. **"The embedding pass is not gated on provenance, because it verifies every hash it certifies"** (`index-integrity`) — every new outcome either goes through `certify_embedded`'s `id + file_path + content_hash` predicate or writes nothing. `PROVIDER_FAILED` writes nothing.
3. **"Exclusion-pattern changes reconcile on the next completed embed pass"** (`index-integrity`) — convergence is defined for a *completed* sweep with three declared exceptions; a budget stop is added to the pause's clause (stops between notes, next pass runs a fresh sweep), and the provider-failure exception is unchanged in substance and now typed.
4. **"A re-derive that skipped any file is incomplete"** / A.7a (`index-integrity`) — a chunk-capped note is **not** a skip, for the same reason a link-capped note is not: the truncation is deterministic and what was written is exactly what was derived.
5. **#160's deliberate asymmetry** — `_record_index_run` stays green through a provider outage; only the `indexer_runs` row turns red.
6. **"Recording never fails a pass"** — the cursor write and the fingerprint writes are wrapped, logged and swallowed, like `_write_indexer_run`.
7. **Session discipline under `index_pass_lock`** — the cursor write opens its own short session after the wrapped body's session closes, so one task never holds two pooled connections (`_pass_lock_without_a_connection`'s rule from the other side).
8. **"Filtered vector search — the `SET LOCAL`s are correctness"** and **"The owner predicate makes every vector query a filtered query"** (`search-quality`) — D2 adds columns and post-processing only; no predicate, no `SET LOCAL`, no overfetch, no exact-fallback eligibility changes.
9. **The recall SLO's baseline** — set-recall is measured over notes returned, and D2 removes no note from any result set.
10. **`usage_logs.tool` holds the registered name** and `timing`'s budget is enforced at the record site — the new markers ride existing `timing.record` calls and add no new params key beyond a boolean.
11. **`content_hash` is never nulled and never sentinelled** — nothing here writes it.
12. **`index_pass_lock` stays one lock** (#12).
13. **`alembic check` clean** — 023's marker is mirrored on the ORM table and column.

## Migration Plan

1. `make test-schema` before the deploy (023 carries a table, a CHECK and a column).
2. **Deploy this change with `EMBEDDING_PROVIDER`, the model name, `EMBEDDING_DIMENSIONS`, `CHUNK_SIZE`, `CHUNK_OVERLAP` and `FTS_CONFIGS` unchanged.** The first startup adopts whatever it finds; adopting a configuration the operator changed in the same deploy would bless a mixed vector space (D7 risk).
3. `make deploy` → `make db-check` clean.
4. First startup writes both fingerprints and logs the adoption at WARNING. `indexer_state` then holds two rows; the cursor row appears after the first pass.
5. No re-embed and no re-extraction: `CURRENT_EXTRACTION_VERSION` is untouched, `chunks_truncated` defaults to the correct value, and no note's `embedded_content_hash` is cleared.
6. Rollback: revert the code. `indexer_state` and `chunks_truncated` are inert to the previous build — it neither reads nor writes them — so a revert costs nothing and a re-deploy adopts the fingerprints again. The migration's `downgrade()` drops only what carries 023's marker.

## Open Questions

1. **`MAX_CHUNKS_PER_NOTE` = 1,000?** At `CHUNK_SIZE=512` that is ~2 MB of cleaned text per note, far above anything in either production vault, and it bounds one note's hold on `index_pass_lock` to ~1,000 sequential Ollama calls. *Recommendation: 1,000.*
2. **Budget defaults, and the multi-tenant-only clause.** `EMBED_CHUNK_BUDGET_PER_USER=5000`, `EMBED_TIME_BUDGET_SECONDS_PER_USER=300`, enforced only when the pass serves more than one active user scope. The clause is what keeps single-user deployments byte-identical to today; the alternative (always enforce) makes a first index of 2,577 notes span several five-minute-spaced passes. *Recommendation: as stated.*
3. **Withholding the stale chunk preview (D2).** It is the one decision here that changes what an agent sees on a healthy day, since a note edited minutes ago is legitimately stale. *Recommendation: withhold — the preview is the only field that is a quotation of the note's text, and it is the only stale one.*
4. **`OPENAI_BASE_URL` out of the embedding fingerprint.** Including it forces a full re-embed on an infrastructure move; excluding it misses a proxy that borrows a model name. *Recommendation: exclude, and record the residual.*
