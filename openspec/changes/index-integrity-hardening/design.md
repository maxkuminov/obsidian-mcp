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
- **The keyword rebuild is per owner** (`_rebuild_tsvectors_pinned`, driven per user by `scripts/rebuild_tsvectors.py`). Nothing in it today is a statement about the table as a whole, which is why D7b exists.

A pre-code Codex review of the first draft of this design returned 7 BLOCKER, 9 MAJOR and 3 MINOR findings. Every one is folded in below or recorded under **Rejected findings**; the two owner decisions it escalated (fail closed for FTS; keep the chunk cap at 1,000 and declare the worst case) are taken as given.

## Goals / Non-Goals

**Goals:**
- The `indexer_runs` row is true: a pass that embedded nothing because the provider was down says so, and its `notes_embedded` excludes the notes it did not embed.
- No vector-search result presents superseded text as the note's current content, and no result disappears because it is stale.
- One tenant's **embed stage** cannot deny another tenant's index freshness for longer than one budgeted pass plus one note, with no note preempted mid-chunks.
- A configuration change that silently invalidates stored derived rows fails the process at startup, with a named remedy, and the remedy is the only thing that can clear the refusal.

**Non-Goals:**
- **Filtering stale rows out of vector search.** Rejected in the issue and here: on `embedded_content_hash = content_hash` it would hide every note edited in the last five minutes and the entire vault during an outage, and — because both paths run the O(n) exact fallback on *any* zero-row filtered result (`search-quality`, "The owner predicate makes every vector query a filtered query") — it would convert an outage into a sequential scan of the whole embedding table on every query.
- **Per-row embedding fingerprints.** `provider`, `model`, `dimensions`, `chunk_size`, `chunk_overlap` and the chunk cap are global settings; a per-row copy is 16,700 identical strings whose only use would be a lazy per-note re-embed, and *that* is the design that leaves two vector spaces coexisting in one HNSW index for the whole migration window. Cosine distance between two spaces is meaningless, so a partially-migrated index answers wrongly for longer than a refused startup does. `extraction_version` is per-row precisely because its repair is per-note and scoped; this one's repair is a wipe.
- **Budgeting the scan or the link backfill (D5b).** The fairness claim of this change is **scoped to the embed stage**. `index_vault` and `link_backfill_pass` run before `embed_vault` in each user's sequence (`indexer.py:3361`, `:3370`, `:3379`) and are not budgeted: each is a single transaction over a walk of the vault whose cost is bounded by the vault's size and the write-side caps rather than by an external provider's latency, and stopping one part-way means either committing a partial derive — which A.7a exists to forbid — or discarding the whole pass's work. The starvation the issue measured is the embed stage's (a 10 MiB note is ~5,120 sequential 30 s-bounded provider calls); the scan's cost per tenant is minutes at worst and is not multiplied by a remote service. Recorded as a residual on #202 rather than half-solved.
- **Splitting `index_pass_lock`, or a per-tenant worker.** Out of scope and forbidden by #12.
- **Batching Ollama `/api/embed` inputs.** A throughput win, not a fairness fix (the issue says so), and it changes the provider contract `embedding-providers` pins.
- **A `LIMIT` on the backlog SELECT** (`indexer.py:2493`). The budget below bounds the *work*, which is what starves other tenants; a `LIMIT` bounds only the row fetch, and a fetch of every id/path/hash triple for one tenant is not the cost.
- **Bounding `semantic_search`'s hydration of the full `NoteEmbedding` row** (`embeddings.py:613`, which loads every 1024-float vector for `overfetch` rows to recompute a similarity the query already returned as `distance`). Real, unrelated to these four findings, recorded as a residual for a follow-up.
- **A second "coverage" definition.** The dashboard counts notes with *any* vector (`routes.py:398-407`); the poller counts notes with a *current* one (`routes.py:2092`). This change shows both rather than redefining either — silently changing what the gauge means would make every historical screenshot wrong.
- **Per-user `FTS_CONFIGS` or per-user embedding settings.** Global today (README:966), global after this.
- **Detecting a stale chunk before the scan has seen the edit.** See D2c: the staleness signal is derived from `notes_metadata`, so it can only report what the scan has committed. Declared, bounded, and tested as a residual rather than closed.

## Decisions

**D1 — `embed_note` returns a typed outcome carrying bounded error detail; `embed_vault` counts only the certifying ones.**
Today `embed_note` returns `0` for three unrelated things — a note that cleaned to zero chunks and *was* certified (`embeddings.py:505-519`), a provider exception it swallowed (`:521-525`), and a vector/chunk cardinality mismatch (`:527-532`) — and `_embed_vault_pinned` does `outcome.embedded += 1` unconditionally afterwards (`indexer.py:2630`). The return becomes a frozen dataclass over an enum:

```python
class NoteEmbedOutcome(enum.Enum):
    EMBEDDED = "embedded"
    CERTIFIED_EMPTY = "certified_empty"
    PROVIDER_FAILED = "provider_failed"
    PROVIDER_CARDINALITY_MISMATCH = "provider_cardinality_mismatch"

@dataclass(frozen=True)
class EmbedNoteFailure:
    exc_type: str            # type(exc).__name__, or "CardinalityMismatch"
    message: str             # truncated to MAX_EMBED_FAILURE_MESSAGE_CHARS (200)
    requested: int | None    # chunks requested — set for both failing outcomes
    received: int | None     # vectors returned — set for the mismatch

@dataclass(frozen=True)
class EmbedNoteResult:
    outcome: NoteEmbedOutcome
    chunks: int
    truncated: bool
    failure: EmbedNoteFailure | None   # non-null exactly for the two failing outcomes
```

| outcome | certifies | counts into `notes_embedded` | counts as a failure | `failure` |
| --- | --- | --- | --- | --- |
| `EMBEDDED` | yes | yes | no | `None` |
| `CERTIFIED_EMPTY` (cleaned to zero chunks) | yes | yes | no | `None` |
| `PROVIDER_FAILED` (the swallowed exception) | no | **no** | **yes** | class + truncated message + `requested` |
| `PROVIDER_CARDINALITY_MISMATCH` | no | **no** | **yes** | `"CardinalityMismatch"` + `requested` + `received` |

**Why the failure detail is on the result and not left to the caller (BLOCKER).** `EmbedPassResult.first_error` is the only thing an operator sees for a failed pass, and it is built from an exception the caller catches. Once `embed_note` swallows the provider exception, the caller has nothing to build it from: a first draft would have produced `error = "embed failures: 412 of 412 — first: None"`. So `record_failure` gains a second entry point, `record_failure_detail(failure: EmbedNoteFailure)`, which sets `first_error` to `f"{exc_type}: {message}"` for a provider raise and to `f"CardinalityMismatch: {received} vectors for {requested} chunks"` for the mismatch, and increments the same counter. The existing `record_failure(exc)` stays for exceptions that genuinely escape (a database error around the call), so the two paths converge on one counter and one `failure_summary`. The message is truncated at the source, not at the run row: `MAX_RUN_ERROR_CHARS` (4,000) bounds the *whole* `error` text, and a single provider traceback message can exceed it and evict the stage labels beside it.

*Alternative rejected — re-raise from `embed_note` and let `embed_vault`'s existing `except Exception` fire.* It is smaller, and it is wrong in two places. `_reconcile_exclusions` also calls `embed_note` (`indexer.py:2807`) and its declared convergence exception is that "a row whose provider call fails SHALL be left unstamped … and retried on a later pass" (`index-integrity`, exclusion reconciliation) — a raise there would have to be re-caught to preserve that, so nothing is saved. And a raise makes the *type* of a provider blip indistinguishable from a database error at the call site, which is the conflation this decision exists to remove.

**No `__int__` compatibility shim (BLOCKER).** The first draft claimed `EmbedNoteResult.__int__` would keep `total_chunks += result` working. It would not: `int.__iadd__` falls back to `int.__add__(result)`, which returns `NotImplemented`, and `EmbedNoteResult` defines no `__radd__`, so the statement raises `TypeError`. Every caller therefore reads `result.chunks` explicitly — `indexer.py:2629` and the equivalent in `_reconcile_exclusions` — and the existing tests that compare `embed_note`'s return against an integer are updated in the slice that owns them, named in `tasks.md` rather than left to be discovered. Explicit is also the outcome we want: a caller that must name the field cannot silently keep treating four outcomes as one number.

**D2 — Stale rows are annotated and their chunk preview is withheld; every other field is kept.**
The predicate is `embedded_content_hash IS DISTINCT FROM content_hash` — `IS DISTINCT FROM`, not `!=`, so a NULL `embedded_content_hash` (never embedded, or invalidated by a move) is stale rather than NULL-propagating to "not stale".

`semantic_search` already hydrates the whole `NoteMetadata` entity (`embeddings.py:613`), so both hashes are in hand and the statement is unchanged. `find_related_stmt` (`tools.py:1912-1931`) is a six-column projection and gains three: `content_hash`, `embedded_content_hash` and `chunks_truncated`. It is the statement `tests/integration/test_search_recall.py` EXPLAINs, and adding scalar columns from an already-joined table changes no plan.

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

The header carries the stale count **always, including `— 0 stale`**, for `get_links`'s reason: an absent token is not evidence of absence, and an agent cannot distinguish "no stale rows" from a build that does not report staleness. Per-row, only `stale: true` is rendered — the header count is the always-present signal, and `stale: false` on every one of fifteen rows is noise, not information.

**D2b — `find_related` states a stale source on every return path, the empty one included (MAJOR).** The source note can itself be stale, in which case the averaged query vector is an average of the note's *previous* content and every neighbour answers a superseded question — a fact no per-row flag can express. The first draft put that line only above a non-empty list, which loses it exactly where it explains the most: `No related notes for 'X'` from a stale source is the reading an agent will act on ("this note has no neighbours") when the truth is "the vector we searched with describes content this note no longer has". The line is therefore emitted on the ranked path, on the true-zero-result path (`tools.py:2029-2035`) and on the not-found/not-embedded paths' successor states alike — wherever the source row was loaded and its two hashes are known. The `related_source_not_embedded` branch keeps its own message: a source with no vectors at all is a different fact with a different fix, and the existing marker classification says so.

**D2c — The staleness guarantee is scoped to what the scan has committed (MAJOR, declared residual).** `stale` is computed from `notes_metadata`, so it reports a note as stale only once the scan has committed the new `content_hash`. Between an edit landing on disk and the next scan reaching that note — up to `INDEX_INTERVAL_SECONDS` plus the pass in flight — the row reads `embedded_content_hash == content_hash` while `chunk_text` is already superseded, and the result is presented as fresh. This is not closable from the read path: detecting it would require hashing the file on disk for every returned row, which puts a per-result filesystem read on the hot path of a search and still races. It is therefore **declared**: the guarantee is "no result presents text the index knows to be superseded", the residual window is named in the docs and in the tool docstrings, and the live post-deploy exercise sets that state up explicitly (edit a note, search *before* the pass, observe the row is **not** marked, then search after the pass and observe that it is). Writing the test that way is what keeps the residual from being quietly re-described as a guarantee later.

*Alternative rejected — keep the preview and only flag it.* The flag is metadata; the preview is content. An agent that summarises three results into a paragraph quotes the previews and drops the metadata, which is exactly the failure `CLAUDE.md` ranks second.
*Alternative rejected — return the note's current first 500 characters instead.* It would be a different span from the one that matched, presented where the matching span goes: a fabricated excerpt, worse than none.

**D3 — `MAX_CHUNKS_PER_NOTE`, applied in `chunk_text`, as a declared degradation that still certifies.**
`chunk_text` (`embeddings.py:222`) gains a bounded sibling in the shape #203 established for links: `chunk_text_bounded(content, *, chunk_size, overlap, max_chunks) -> tuple[list[str], bool]`, returning the first N chunks in document order and whether it truncated. `MAX_CHUNKS_PER_NOTE` is a module constant in `src/config.py` beside `MAX_LINKS_PER_NOTE`, not a settings field — the same call the link cap made.

A capped note:
- has its **first N chunks embedded and is certified**, through the unchanged `certify_embedded` predicate. It is emphatically **not** held uncertified: an uncertified note is re-selected by the backlog on every tick for ever, which is #127's permanent burn arriving by a new route. `MAX_NOTE_BYTES` is 10 MiB and `chunk_size` is 512 tokens, so an over-cap note is reachable with legal input. Because this narrows what "full coverage" means, the existing requirement that mandates full coverage is **modified**, not merely accompanied — see the spec delta.
- sets `notes_metadata.chunks_truncated` (migration 023), cleared when a later embed of that note fits under the cap — exactly `links_truncated`'s lifecycle. The exclusion branch and the zero-chunk certification clear it too, because both leave the note with no vectors at all.
- emits **one ERROR line naming the path and the cap, after the certifying transaction has committed** (MINOR). Logging before the commit would put a permanent ERROR in the ops-health buffer for a truncation that then rolled back on a `StaleCertification` — an operator chasing a note that was never actually written that way. The line never names the true chunk count, which could only be obtained by the unbounded chunking the cap exists to prevent.
- is marked in every vector-search row for that note (`embedding_truncated: true`), because the tail of the note is not semantically searchable at all and a result from its head reads as a result from the whole note.

**Why a cap at all, given #127 removed the aggregate deadline.** #127 removed a *time* budget that fired on healthy chunks; this is a *count* bound that changes what is embedded, deterministically, and says so. The two are not the same trade: a note the deadline killed never certified and burned the same 300 s every tick for ever; a capped note certifies once and is never re-selected.

The zero-chunk probe in `_reconcile_exclusions` (`indexer.py:2796`) uses the bounded form as well, so "has chunks" means the same thing in both places and the probe stops at the first chunk instead of chunking a 10 MiB note to find out it is non-empty.

**D3b — `CHUNK_OVERLAP` must be strictly less than `CHUNK_SIZE` (MAJOR).** `chunk_text` computes `step = max(char_size - char_overlap, 1)` (`embeddings.py:230`), the #10 infinite-loop guard. At `CHUNK_OVERLAP == CHUNK_SIZE` the step collapses to **one character**, so ~3 KB of prose produces ~3,000 chunks and hits a 1,000-chunk cap — a configuration typo silently truncating the embedding of every ordinary note in the vault, with the cap's ERROR line firing thousands of times. The guard turned a hang into a quiet catastrophe; it does not make the configuration sane. `Settings` therefore gains a model validator rejecting `chunk_overlap >= chunk_size` at startup, naming both values, and the interaction is documented at both keys: the cap is a bound on a *note*, and it only behaves like one while the step is a meaningful fraction of the chunk.

**D4 — `_active_user_ids()` is ordered; the periodic and startup passes rotate from a cursor persisted in `indexer_state`.**
`_active_user_ids()` (`indexer.py:3130`) has no `ORDER BY`, so its order is whatever the planner returns — stable enough in practice that the same tenant goes first every cycle, and unspecified enough that nothing can be asserted about it. It gains `ORDER BY users.id`, which alone makes the order a fact.

Rotation is applied by a new `_rotated_user_ids()` used by `run_indexer_loop` only — the startup pass (`:3358`) and the periodic tick (`:3431`). It reads `indexer_state['embed_rotation_cursor']`, and returns the ordered list rotated to begin at the smallest id **strictly greater** than the stored one, wrapping. After each user's per-user sequence finishes — success or failure, in a short session opened by the lock holder after that user's pass has closed its own, the discipline `_write_indexer_run` already follows (`indexer.py:262`) — the cursor is written to that user's id. Writing it can never fail the pass; it is logged and swallowed.

**The cursor stores a user id, not an index.** An integer offset into a list whose membership changes when a user is added, deactivated or deleted points somewhere else on the next cycle; "start after id 7" is well-defined whether or not user 7 still exists, because the successor query does not require it to.

**Rotating a freshly re-fetched list is what makes this worth a table.** In-process state resets on every restart and every deploy, and a deploy recreates the container: the tenants at the tail of the order are exactly the ones a restart-truncated pass never reaches, so an in-memory cursor would be reset precisely when it was about to pay off. That is why it is persisted, and it is persisted next to the fingerprints because both are single facts about the index as a whole.

`_reindex_background` (`routes.py:2129`) and `scripts/rebuild_tsvectors.py` keep calling `_active_user_ids()` unrotated: an operator-triggered reindex is not the starvation vector, and giving it a cursor would let a panel click move the periodic pass's rotation.

**D5 — A per-user chunk and time budget on the embed stage, checked only at a note boundary.**
Two settings, `EMBED_CHUNK_BUDGET_PER_USER` (default 5,000) and `EMBED_TIME_BUDGET_SECONDS_PER_USER` (default 300, matching one `INDEX_INTERVAL_SECONDS`); `0` disables either. The budget is consumed by both the backlog loop and the reconciliation sweep, since both call the provider, and is checked at exactly the two places `_is_paused()` already sits — `indexer.py:2530` and `:2744` — **before** a note is started, never inside one.

Three clauses are load-bearing:

- **Never mid-note.** `embed_note` refuses partial certification, so a note abandoned between chunks is uncertified, re-selected next tick, and re-performs every provider call it already made — #127, exactly. Checking only at the boundary means the overrun is bounded by one note, which D3 has already bounded to `MAX_CHUNKS_PER_NOTE`.
- **At least one note, always.** The check runs *after* the first note of that user's pass, so a tenant whose very first note exceeds the whole budget still advances by one note per pass instead of zero for ever. Without this clause a small budget is a livelock.
- **Only when the pass serves more than one user scope.** In single-user mode, and in a multi-user deployment with one active user, there is no other tenant to be fair to; a budget there would turn an initial index of 2,577 notes into several passes separated by five-minute sleeps for no benefit, and would look like a stall. This is the clause that makes the default deployment's behaviour identical to today's.

A budget stop is **not** a failure and does not touch `error` — it is a deliberate decision, the same class as a pause, and writing it into `error` would fire #201's own outage signal on a healthy server. It logs once at WARNING per user per pass. The operator-visible signal for a tenant that is permanently over budget is the dashboard's **pending count** (D8), which stays high across passes: a persistent backlog is what the operator needs to see, and it is a property of the index rather than of one pass.

**The bound this buys, stated exactly (MAJOR, accepted limitation).** Because the budget is evaluated only between notes, the delay one tenant can impose on the next is *the budget plus one note's embedding time*, and one note's embedding time is bounded by `MAX_CHUNKS_PER_NOTE` × the provider's per-call bound. With the cap at 1,000 and Ollama's 30 s per-chunk `wait_for` as the only liveness bound (#127 removed the aggregate deadline deliberately, and its absence is pinned by tests), the arithmetic worst case is **1,000 × 30 s ≈ 8.3 hours for one note** on a provider that is answering every call at the very edge of its timeout. That is a pathological-provider figure, not a steady-state one: at the measured healthy latency the same note is minutes, and a provider that slow trips #201's outage detector on the notes around it. The owner's decision is to keep the cap at 1,000 and record this as an accepted limitation rather than reintroduce an aggregate deadline — which is precisely the shape #127 removed, and which would recreate the never-finishing note one size class up. Shrinking the cap trades it against how much of a large note is searchable at all.

**D5b — The fairness claim covers the embed stage only.** See Non-Goals. `index_vault` and `link_backfill_pass` run first in each user's sequence and stay unbudgeted; the scoping is stated in the spec so a later reader does not read "one tenant cannot starve another" as a claim about the whole pass.

**D6 — One `indexer_state` key/value table (migration 023) holding two fingerprints and the cursor.**
```
indexer_state
  key         VARCHAR(64)  PRIMARY KEY
  value       TEXT         NOT NULL
  updated_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
  CHECK (key IN ('embedding_fingerprint', 'fts_fingerprint', 'embed_rotation_cursor'))
  table comment: 023's ownership marker
```

*Why a table and not columns on an existing row.* There is no singleton row to hang them on: `users` is per-tenant, `notes_metadata` is per-note (and a per-note copy is a Non-Goal), and `indexer_runs` is an append-only display history that this change must not start reading for a decision — its own docstring says nothing reads it for one. The three facts share a lifecycle ("state about the index as a whole, written by the pass or by a maintenance command") and differ in shape, which is what a key/value table is for.

*Why the CHECK, given a key/value table.* A key read from this table that does not exist reads as **absent**, and absent is the state that makes the startup fingerprint check *adopt* rather than refuse. A single mistyped key therefore silently disables the guard whose whole purpose is to prevent a permanent, undetectable corruption. `ck_indexer_runs_trigger` exists for the weaker version of the same argument. Adding a key becomes a migration, which is correct: every key here has a startup or a scheduling consequence.

The migration follows 022's device exactly — reconcile-or-refuse against `pg_class`/`pg_constraint`, an ownership marker as the table comment mirrored on the ORM model so `alembic check` compares it, `downgrade()` drops only what carries the marker, `lock_timeout`/`statement_timeout` set and `RESET` at the end. It also adds `notes_metadata.chunks_truncated BOOLEAN NOT NULL DEFAULT FALSE` with its own column marker, `links_truncated`'s shape for `links_truncated`'s reasons: the constant server default keeps it a catalogue-only `ADD COLUMN` on a table carrying a `tsvector` and two GIN indexes, and `false` is the *true* value for every pre-existing row, since every existing vector set was produced by an uncapped chunker.

**023 backfills nothing.** Neither fingerprint is derived from the current settings at migration time, for 016's reason inverted: a stamp written by the migration would assert that the stored rows were produced by the configuration the `.env` carries *now*, which is exactly the claim the fingerprint exists to test.

**D7 — The fingerprint format, and the comparison rule.**
Both values are canonical JSON — `json.dumps(obj, sort_keys=True, separators=(",", ":"))` — built by two functions in a new `src/services/index_state.py` (a new module, so `embeddings.py` and `indexer.py` can both import it without a cycle):

```
embedding_fingerprint() -> {"v":1,"provider":"ollama","model":"bge-m3","dimensions":1024,
                            "chunk_size":512,"chunk_overlap":0,"max_chunks_per_note":1000}
fts_fingerprint()       -> {"v":1,"configs":["english"]}
```

- `model` is the **active** provider's model — `settings.embedding_model` for `ollama`, `settings.openai_embedding_model` for `openai` — selected by the same branch `get_provider()` takes. Reading the inactive one is the exact bug this guard exists to catch, so the selection lives in one function.
- **`max_chunks_per_note` is in the fingerprint (MAJOR).** It changes what a note's stored vector set *is*: at cap N a long note holds N chunks and its tail is absent, and the same note at cap 2N would hold a different set. A build that lowers the cap leaves rows over the new bound; one that raises it leaves rows that are silently incomplete against the new policy, and nothing selects them — `embedded_content_hash` still equals `content_hash`. Including it makes a cap change a declared reset instead of a silent, permanent under-embedding. The cost is that changing the cap forces a full re-embed even where it would only *widen* coverage; that is recorded as a trade-off below rather than optimised with a comparison rule that would have to know which direction is safe.
- `configs` is `sorted(settings.fts_configs)`. Index-time tsvectors are `||`-concatenated and query-time tsqueries are OR'd, and both operators are order-insensitive over lexeme sets, so `["english","norwegian"]` and `["norwegian","english"]` produce identical stored vectors. Comparing them as ordered lists would refuse startup over a reordering that changed nothing.
- **`OPENAI_BASE_URL` is deliberately excluded.** Moving between `api.openai.com`, an Azure deployment and a compatible proxy with the same model name is an infrastructure change, and including it would demand a full vault re-embed for it. The residual — a proxy that serves something *else* under a name it borrows — is an operator-trust question the fingerprint cannot answer anyway, and is recorded as a risk.
- Canonical JSON rather than a `|`-joined string: a model name may contain any character, so a delimiter needs an escaping rule that would then have to be specified, versioned and tested. JSON has one already, it parses on both sides so the mismatch message can name **which field changed** rather than printing two opaque strings, and `sort_keys` makes exactly one spelling per value — the property byte-equality comparison needs, and the reason `indexed_vault_realpath` chose hex over base64.
- The `v` field is what makes adding a future field a deliberate act: it changes every fingerprint, so the change that adds one must ship a migration that rewrites the stored value or an instruction to reset. Recorded as a risk rather than solved in advance.

**Comparison, at startup, in `_check_embedding_dim`'s shape** (`src/main.py:40`) and inserted immediately after `_validate_fts_configs()` in lifespan (`main.py:275`) — after it deliberately, so a typo'd config name still fails with `validate_fts_configs`'s own message listing the installed configs, rather than as an opaque fingerprint diff. Both are inside the sandbox-mode short-circuit at `main.py:263`, so `MCP_SANDBOX_MODE` skips them like every other guard.

| stored | embeddings | keyword vectors |
| --- | --- | --- |
| absent | **adopt**: write the current fingerprint, log at WARNING that it was assumed and not verified | same |
| equal | proceed silently | proceed silently |
| different | log at CRITICAL naming both fingerprints and the differing fields, point at `make reset-embeddings`, `sys.exit(1)` | log at CRITICAL naming both and the differing entries, point at `make rebuild-tsvectors`, `sys.exit(1)` |
| unparseable, or a `v` this build does not know | treated as **different** — refuse, with a message saying the stored value could not be interpreted, and **write nothing** | same |
| table absent | return, deferring to alembic (`_check_embedding_dim`'s stance for a missing column) | same |

**Both fail closed, and the first draft's asymmetry was wrong (BLOCKER, owner decision).** The draft let an FTS mismatch warn and serve, on the reasoning that a stale stemmer is *incomplete* rather than wrong. That reasoning does not survive contact with the actual failure: under `["english"]` the token `running` is stored as the lexeme `run`, and a query under `["simple"]` for `run` then **matches a note that does not contain the word `run`** — a false positive, indistinguishable from a real hit, handed to an agent that acts on it without a human ever seeing the query. That is the product's second-named expensive failure, not a recall shortfall. Symmetrically, a query for `running` under `simple` misses the note entirely. Changing `FTS_CONFIGS` is an operator action, rare, and the refusal names the one command that fixes it; there is always a second exit, which is to put `FTS_CONFIGS` back. So keyword vectors fail closed exactly as embeddings do, and the ERROR-versus-WARNING inconsistency the review flagged as a NIT disappears with the branch that had it.

**Malformed values fail closed and are never rewritten (MINOR).** A stored value that will not parse, or that carries a `v` this build does not recognise (a downgrade past a fingerprint-format bump), is a value this build cannot compare — so it cannot certify the rows, and it refuses. It must not be silently overwritten with the current fingerprint: that would convert an unreadable claim into a confident false one, and it is the same rule `clean_at_version` already follows when a stamped extraction version has no registered cleaner ("an unknown stamped version counts as *differs*").

**Only the maintenance workflows write a fingerprint after the initial adoption**, and this is what stops a refusal from clearing itself. Startup never rewrites a fingerprint it has just refused on.

The dimension guard stays. It reads the live column width from `pg_attribute`, a physical fact about the table; the fingerprint records the configuration the rows were generated under, a logical fact about their content. A restored dump into a differently configured deployment trips the first; a same-dimension model swap trips only the second.

**D7b — The keyword fingerprint is written by an operation that rebuilt every retained row, not by a per-owner rebuild (BLOCKER).**
`_rebuild_tsvectors_pinned` is per owner and `scripts/rebuild_tsvectors.py` drives it once per active user. Writing the global fingerprint inside it claims something the per-owner rebuild cannot know: that *every retained row* in `notes_metadata` was rebuilt. Two shapes falsify it — user B's rebuild raises after user A's wrote the fingerprint, and a scope with rows that the driver never visited at all (an inactive or unassigned user; the NULL-owned single-user scope in a database that also has named users). Either way the stored fingerprint certifies rows that are still on the previous configuration, and a startup that now fails closed on the fingerprint would pass while keyword search is exactly as wrong as before.

So the fingerprint is written by the **outer** operation, and only after coverage is proved:

1. The driver enumerates the owner scopes to rebuild as `SELECT DISTINCT user_id FROM notes_metadata` — every scope that **retains rows**, `NULL` included — not `_active_user_ids()`.
2. It rebuilds each scope with the existing per-owner rebuild, which is already atomic and already certifies each row it writes against owner, path and hash.
3. The whole multi-scope rebuild and the fingerprint write are **one transaction**. Any scope that raises, and any scope whose root cannot be pinned, rolls the entire operation back and writes no fingerprint, naming the scope that stopped it.

The cost is that a multi-tenant rebuild becomes all-or-nothing rather than per tenant; that is the price of the fingerprint meaning what it says, and the rebuild is already the cheap maintenance path (keyword index only, no provider calls, seconds for a few thousand notes).

**The escape hatch is what makes fail-closed safe here.** A scope holding rows whose vault cannot be read — an unassigned user's leftovers — cannot be rebuilt, so no fingerprint is written, so startup keeps refusing. The remedy is stated in the refusal and in the runbook: assign or delete that user, or **put `FTS_CONFIGS` back to the value the stored fingerprint names**, which clears the refusal immediately with no rebuild at all. A configuration edit is always reversible; that is what distinguishes this from an outage.

**D7c — The reset's ordering, and the pass-level re-check that enforces it (BLOCKER).**
`make reset-embeddings` runs `docker compose run --rm` deliberately (#142), so it reads the edited `.env` and works while the service is up or down. That last property is the hole: with the live service still running the old configuration, the reset wipes the column, writes the **new** fingerprint, and the old container's next tick embeds the backlog with the **old** model and stamps `embedded_content_hash` — old-model vectors under a fingerprint claiming the new model, permanently, with every later startup silent because the stored value already matches.

Two mechanisms close it, and both are specified because the documentation alone is not enforcement.

- **The ordering, which the guard now makes self-enforcing.** For a model, chunking or dimension change the runbook becomes: edit `.env` → `make deploy` (the new image refuses at the fingerprint or dimension guard and the container stays down, embedding nothing) → `make reset-embeddings` while it is down → restart. This inverts the pre-fingerprint advice in `README.md:915`, which told operators to reset *before* recreating; that ordering was safe only because nothing then depended on a stored claim. The refusal is what creates the quiescent window, so the guard pays for its own runbook.
- **The pass re-checks the stored fingerprint (the enforcement).** `_embed_vault_pinned` reads `indexer_state['embedding_fingerprint']` once per user pass and embeds nothing when it differs from the process's own, logging at ERROR and recording nothing. A live old-configuration container therefore stops embedding within one tick of any reset, whatever order the operator used, and an operator who ignores the runbook loses time rather than correctness. The read is one row per user pass. The panel's in-process reset is unaffected: the process wrote the value it is about to compare against.

**D7d — A fingerprint write that fails aborts its maintenance transaction (MAJOR).** The "recording never fails a pass" rule (`indexer.py`, `_write_indexer_run`) covers *instrumentation* — the run history and the rotation cursor — where a lost write costs an operator a view. A fingerprint is not instrumentation: it is the claim a later startup refuses on. A reset that wiped the column and then swallowed a failed fingerprint write leaves the stored value naming the **old** configuration over rows that are about to be built with the new one, and the next startup refuses on a database that is actually consistent — or, worse in the other direction, a rebuild that rebuilt everything and lost its write keeps refusing forever. So the fingerprint upsert is inside the maintenance operation's transaction and a failure rolls that operation back and surfaces to the operator who invoked it. Swallow-on-failure stays exactly where it was: the cursor write and `_write_indexer_run`.

**D8 — The dashboard shows currency beside coverage, and does not redefine coverage.**
`stats.embedding_pct` counts notes with at least one vector row (`routes.py:398-407`); the reset poller counts notes whose `embedded_content_hash` equals their `content_hash` (`routes.py:2092`). They are different questions and both are worth answering, so the dashboard gains `notes_pending` (stale or never-embedded) and `notes_chunks_truncated` beside the existing bar, and the bar's meaning is unchanged.

The poller's query is admin-only and **unscoped** — it counts the whole table. The dashboard is scoped by `_scope_user_id(user)`, so the new counts must be scoped the same way as the coverage numbers directly above them, or a regular user reads another tenant's backlog as their own. The shared predicate goes in one helper used by both call sites so the two cannot drift; the poller keeps its unscoped, admin-only behaviour.

A non-zero pending count on an otherwise-idle vault is exactly the shape a provider outage now takes, and it is the operator-visible signal D5's budget stop deliberately does not write into `error`.

**D9 — The reconciliation sweep reports into the same accumulator, and `attempted` is defined (MAJOR).**
`_reconcile_exclusions` re-embeds re-included notes (`indexer.py:2807`) and today swallows their failures into its own `except Exception` with nothing riding back to the pass. A provider outage during a pass whose *backlog was empty* — the steady state of a fully-indexed vault, where the sweep is the only thing making provider calls — therefore still writes a clean run row, which is #201 surviving in the one code path #201's fix would not have touched.

The sweep takes the same `EmbedPassResult`, calls the same `record_failure_detail`, and the denominator is defined so the summary stays readable:

> **`attempted` = the number of backlog rows the pass selected + the number of reconciliation rows the sweep actually attempted to embed** (the included-and-vectorless rows whose bytes verified and which reached `embed_note`).

It is deliberately *attempts*, not *rows considered*: the sweep scans every certification-current row in the scope (16,700 on the production vault) and decides about almost all of them without a provider call, so counting those would render "embed failures: 3 of 16,700" for three failures out of three attempts. Rows the sweep skipped — agreement between config and stored vectors, a hash mismatch, a zero-chunk note — are not attempts and are not failures, matching the backlog side's rule exactly.

## Degradation markers

Every bound this change adds is declared in the same four places, matching #203's:

| Degradation | Durable marker | Log | Agent-visible surface | Certification |
| --- | --- | --- | --- | --- |
| Link extraction over `MAX_LINKS_PER_NOTE` (#203, shipped) | `notes_metadata.links_truncated` | one ERROR per capping | `get_links` → `truncated: true` | note is **not** a skip; re-derive still certifies |
| Chunking over `MAX_CHUNKS_PER_NOTE` (#202, this change) | `notes_metadata.chunks_truncated` | one ERROR per capping, **after the commit** | `semantic_search` / `find_related` → `embedding_truncated: true` | note **is** certified; never re-selected while unchanged |
| Vectors older than the note's bytes, as the index knows them (#200) | none needed — `embedded_content_hash != content_hash` **is** the marker | none (a normal, self-healing state) | `stale: true` per row, a stale count in the header, preview withheld | unchanged; the backlog owns the repair |
| Vectors older than the note's bytes, before the scan has seen the edit (D2c) | **none — declared residual** | none | none | unchanged |
| Provider outage (#201) | `indexer_runs.error` via `EmbedPassResult.failure_summary` | WARNING per note, one aggregate ERROR | dashboard pending count; no tool-level marker (every affected row is already `stale`) | nothing is certified, by construction |
| Per-tenant budget exhausted (#202) | none — deliberately | one WARNING per user per pass | none (not a wrong answer, only a late one) | nothing is certified early |
| Scan and link backfill unbudgeted (D5b) | none — declared residual | none | none | unchanged |
| Keyword vector retreat (#127, shipped) | none | one line with the prefix length | none | unchanged |

The rows with no durable marker are the ones where a marker would have to be invented: the budget stop, whose honest surface is the pending count that already exists, and the two declared residuals, which are bounds on the claim rather than states of a row.

## Risks / Trade-offs

- **[D2 withholds a preview an agent was using]** → the row, its rank, its path and its title are all still there, and `read_note` returns the true text. During an outage every preview is withheld, which is loud — that is the point, and the alternative is quoting superseded text for the length of the outage.
- **[D2's staleness is a 0–5 minute normal state]** → a note edited a minute ago is stale by construction until the next pass. The header count makes that visible without making it alarming. If the steady-state noise proves too high the remedy is a shorter `INDEX_INTERVAL_SECONDS`, not a threshold on the marker — a time threshold would re-introduce a silent window.
- **[D2c's pre-scan residual]** → an edit that the scan has not yet seen is served as fresh. Declared, documented at the tool docstring, and set up explicitly in the post-deploy exercise so it stays a known bound.
- **[D3's cap changes what is searchable]** → a note over `MAX_CHUNKS_PER_NOTE` is ~2 MB of prose; its tail becomes semantically unsearchable while staying fully keyword-searchable. Marked on the row and in every result.
- **[D5's worst case is one note's embedding time]** → up to 1,000 × 30 s on a provider answering at its timeout. Accepted per the owner's decision; the alternative is an aggregate deadline, which is exactly what #127 removed.
- **[D5b leaves the scan unbudgeted]** → a tenant with an enormous vault still delays others through `index_vault`. Recorded as a residual on #202; the measured starvation was the embed stage's.
- **[D7's `max_chunks_per_note` in the fingerprint]** → raising the cap forces a full re-embed even though it only widens coverage. Deliberate: a comparison rule that knew which direction was safe would have to reason about every field, and the fields are not independent (a larger cap with a smaller chunk size is not a widening).
- **[D7b makes a multi-tenant rebuild all-or-nothing]** → one unreadable scope blocks the fingerprint and therefore, now, startup. The refusal names the scope, and reverting `FTS_CONFIGS` clears it with no rebuild at all.
- **[D7's FTS fail-closed turns a config typo into a refusal]** → intended. `validate_fts_configs` still runs first, so a *misspelled* config name fails with its own message listing what is installed; the fingerprint refusal is only reached by a name that exists.
- **[D7c's pass-level re-check adds a read per user pass]** → one row. It is the only thing that makes the reset ordering enforced rather than documented.
- **[D7 adopts on absence, so the first startup blesses whatever is configured]** → if an operator changes the configuration *in the same deploy* that ships this change, adoption blesses it. Unavoidable — there is no evidence to compare against — and stated in the migration plan: **deploy this change with the embedding and FTS configuration unchanged.**
- **[D7's `OPENAI_BASE_URL` exclusion]** → a proxy serving a different model under a borrowed name is not detected. Named as a Non-Goal rather than half-solved.
- **[D1 changes `embed_note`'s return type]** → every caller and every existing test that compared it to an integer is enumerated in `tasks.md` and assigned to a slice; the merge gate in §8 greps for callers of every new export.
- **[Migration 023 on a deploy]** → one new table plus one additive constant-default column; `make test-schema` is the gate and `alembic check` must be clean after.

## Call sites this change touches

Enumerated so no slice discovers one late.

- **`chunk_text`** — `embeddings.py:504` (`embed_note`), `indexer.py:2796` (`_reconcile_exclusions` zero-chunk probe). Only those two in production.
- **`embed_note`** — `indexer.py:2622` (backlog), `indexer.py:2807` (reconciliation sweep). Both consume the new result by field.
- **`certify_embedded`** — `embeddings.py:510`, `:541`, `indexer.py:2555`, `:2767`. Unchanged, but the capped path must still route through it.
- **`_active_user_ids()`** — `indexer.py:3358` (startup, rotates), `indexer.py:3431` (tick, rotates), `routes.py:2129` (`_reindex_background`, does **not** rotate), `scripts/rebuild_tsvectors.py:19,30` (replaced by the retained-scope enumeration, D7b).
- **`_is_paused()`** — `indexer.py:2530` and `:2744` gain the budget check beside them; `:3276` (prewarm) and `:3417` (tick) are untouched.
- **`semantic_search`** (service) — one caller, `tools.py:1170`.
- **`find_related_stmt`** — `tools.py:2005` and `tests/integration/test_search_recall.py`.
- **Startup guards** — `main.py:273-275`; the two new checks are appended in that block.
- **Fingerprint writers** — `scripts/reset_embeddings.py` (in its transaction, before commit), `routes.py:2007-2013`/`:2028`/`:2057` (the panel's reset paths), and the new outer rebuild driver (D7b). Not `_rebuild_tsvectors_pinned`.
- **Dashboard** — `routes.py:389-425` and `:483-489`; `templates/dashboard.html:110-125`.
- **Existing tests that must move with the change** — `tests/test_issue_11_embed_note_preserves_vectors_on_failure.py`, `tests/test_issue_127_embed_batch_deadline.py` (call the real `embed_note` and compare its return to an integer); `tests/test_issue_19_embed_vault_rechecks_pause.py`, `tests/test_issue_91_indexed_root.py`, `tests/test_issue_160_indexer_runs.py` (monkeypatch `indexer.embed_note` with fakes returning ints); `tests/test_vector_iterative_scan.py` (asserts the exact semantic result key set at `:265` and fakes `find_related` rows at `:281`); `tests/test_search_phase_timing.py:120`, `tests/test_issue_161_result_telemetry.py:104` (fake `(chunk, note, distance)` tuples for `semantic_search`); `tests/integration/test_pgvector_search.py`, `tests/integration/test_search_recall.py`. Assigned to slices in `tasks.md`.

## Invariants each delta must preserve

Named, so a reviewer can check them one by one.

1. **"A many-chunk note completes, and certifies only on full coverage"** (`index-integrity`) — **modified** by this change, not merely honoured: full coverage is redefined over the bounded requested set, and the modification is in the spec delta so the two cannot contradict each other.
2. **"The embedding pass is not gated on provenance, because it verifies every hash it certifies"** (`index-integrity`) — every new outcome either goes through `certify_embedded`'s `id + file_path + content_hash` predicate or writes nothing. `PROVIDER_FAILED` writes nothing.
3. **"Exclusion-pattern changes reconcile on the next completed embed pass"** (`index-integrity`) — convergence is defined for a *completed* sweep with three declared exceptions; a budget stop joins the pause's clause, and the provider-failure exception is unchanged in substance and now typed and counted (D9).
4. **"A re-derive that skipped any file is incomplete"** / A.7a (`index-integrity`) — a chunk-capped note is **not** a skip, for the same reason a link-capped note is not.
5. **#160's deliberate asymmetry** — `_record_index_run` stays green through a provider outage; only the `indexer_runs` row turns red.
6. **"Recording never fails a pass"** — applies to the run-history write and the rotation cursor **only**. A fingerprint write is not recording, and D7d requires it to abort its transaction.
7. **Session discipline under `index_pass_lock`** — the cursor write opens its own short session after the wrapped body's session closes, so one task never holds two pooled connections.
8. **"Filtered vector search — the `SET LOCAL`s are correctness"** and **"The owner predicate makes every vector query a filtered query"** (`search-quality`) — D2 adds columns and post-processing only; no predicate, no `SET LOCAL`, no overfetch, no exact-fallback eligibility changes.
9. **The recall SLO's baseline** — set-recall is measured over notes returned, and D2 removes no note from any result set.
10. **`usage_logs.tool` holds the registered name**, and the timing holder's budget is enforced at the record site — the new markers ride existing `timing.record` calls and add no new params key.
11. **`content_hash` is never nulled and never sentinelled** — nothing here writes it.
12. **`index_pass_lock` stays one lock** (#12).
13. **The keyword rebuild is atomic and certifies every row it writes** (`index-integrity`, keyword indexing) — D7b widens the transaction across scopes; it does not relax the per-row certification or the abort rule.
14. **`alembic check` clean** — 023's markers are mirrored on the ORM table and column.
15. **`clean_at_version`'s "unknown version counts as differs"** — the fingerprint's unknown-`v` rule is the same rule in a new place.

## Migration Plan

1. `make test-schema` before the deploy (023 carries a table, a CHECK and a column). The gate's asserted head moves from 017 to 023.
2. **Deploy this change with `EMBEDDING_PROVIDER`, the model name, `EMBEDDING_DIMENSIONS`, `CHUNK_SIZE`, `CHUNK_OVERLAP` and `FTS_CONFIGS` unchanged.** The first startup adopts whatever it finds; adopting a configuration changed in the same deploy would bless rows built under the previous one.
3. `make deploy` → `make db-check` clean.
4. First startup writes both fingerprints and logs each adoption once at WARNING. `indexer_state` then holds two rows; the cursor row appears after the first pass.
5. No re-embed and no re-extraction: `CURRENT_EXTRACTION_VERSION` is untouched, `chunks_truncated` defaults to the correct value, and no note's `embedded_content_hash` is cleared.
6. Rollback: revert the code. `indexer_state` and `chunks_truncated` are inert to the previous build — it neither reads nor writes them — so a revert costs nothing and a re-deploy adopts the fingerprints again. `downgrade()` drops only what carries 023's markers.

**The runbook this change replaces (D7c), recorded here and in `README.md`:**

- *Changing the embedding provider, model, dimensions, chunk size, chunk overlap or the chunk cap:* edit `.env` → `make deploy` → the new container refuses at the fingerprint (or dimension) guard and stays down → `make reset-embeddings` while it is down → restart. The refusal is what guarantees nothing is embedding during the reset; the pass-level re-check is the backstop if the order is not followed.
- *Changing `FTS_CONFIGS`:* edit `.env` → `make deploy` → the new container refuses at the keyword fingerprint guard → `make rebuild-tsvectors`, which rebuilds **every** scope holding rows and writes the fingerprint only if all of them succeeded → restart. If a scope cannot be rebuilt the command names it and writes nothing; assign or delete that user, or put `FTS_CONFIGS` back, which clears the refusal with no rebuild at all.

## Rejected findings

Nothing from the pre-code review was rejected outright. Two findings were folded in with a **narrower scope than proposed**, and the narrowing is recorded here so it is reviewed rather than assumed:

- **"Budget the scan and the link backfill too, if cheap."** Not done. Both are single transactions over a vault walk, so a budget there means either committing a partial derive — which A.7a exists to forbid — or discarding the pass's work at the stop, and neither is cheap. The fairness claim is narrowed instead (D5b, and stated in the spec), and the residual is recorded on #202 rather than left implied.
- **"Per-owner generation state" as an alternative to a coverage-proving rebuild.** Not taken. It would need one `indexer_state` key per owner, which the closed-set CHECK forbids by design, or a per-row column, which the per-row Non-Goal argues against for the vector case and which has no better claim here. The single-transaction, all-scopes rebuild (D7b) proves the same thing with no new schema and no new key shape; its cost — an all-or-nothing rebuild — is recorded above.

## Open Questions

1. **`EMBED_CHUNK_BUDGET_PER_USER=5000` / `EMBED_TIME_BUDGET_SECONDS_PER_USER=300`, enforced only when the pass serves more than one active scope.** The single-scope clause is what keeps the default deployment byte-identical to today; always-enforcing spreads a first index of 2,577 notes across several five-minute-spaced passes. *Recommendation: as stated.*
2. **`OPENAI_BASE_URL` out of the embedding fingerprint.** Including it forces a full re-embed on an infrastructure move; excluding it misses a proxy that borrows a model name. *Recommendation: exclude, and record the residual.*
3. **D7b makes `make rebuild-tsvectors` all-or-nothing across tenants, and a scope with rows but no readable vault blocks it.** With FTS now failing closed, that state also blocks startup until the operator assigns the user, deletes it, or reverts `FTS_CONFIGS`. *Recommendation: accept — the revert is always available and immediate — but confirm the production database has no `notes_metadata` rows owned by an unassigned or inactive user before the deploy.*
