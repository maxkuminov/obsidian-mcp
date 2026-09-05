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
    GENERATION_MISMATCH = "generation_mismatch"      # D7c

@dataclass(frozen=True)
class EmbedNoteFailure:
    exc_type: str            # type(exc).__name__, or "CardinalityMismatch"
    message: str             # truncated to MAX_EMBED_FAILURE_MESSAGE_CHARS (200)
    requested: int | None    # chunks requested — set for both failing outcomes
    received: int | None     # vectors returned — set for the mismatch

@dataclass(frozen=True)
class EmbedNoteResult:
    outcome: NoteEmbedOutcome
    chunks_submitted: int              # sent to the provider; debits the budget
    chunks_embedded: int               # stored; feeds the pass's total_chunks
    truncated: bool
    failure: EmbedNoteFailure | None   # non-null exactly for the two failing outcomes
```

| outcome | certifies | `notes_embedded` | a failure | an attempt | `chunks_submitted` | `chunks_embedded` |
| --- | --- | --- | --- | --- | --- | --- |
| `EMBEDDED` | yes | yes | no | yes | N | N |
| `CERTIFIED_EMPTY` (cleaned to zero chunks) | yes | yes | no | **no** — no provider call | 0 | 0 |
| `PROVIDER_FAILED` (the swallowed exception) | no | **no** | **yes** | yes | N | 0 |
| `PROVIDER_CARDINALITY_MISMATCH` | no | **no** | **yes** | yes | N | 0 |
| `GENERATION_MISMATCH` (the configuration moved under the call) | no | **no** | **no** | yes | N | 0 |

**Two chunk counts, not one (MAJOR, round 3).** A single `chunks` field would have to mean either "sent" or "stored", and the per-tenant budget needs the first while the pass's `total_chunks` metric wants the second. A budget debited by *embedded* chunks is not debited at all when the provider fails — so a tenant whose every note fails burns unbounded provider time and never trips its own bound, which is precisely the starvation #202 is about, surviving inside the fix for it. The budget therefore debits `chunks_submitted` for every provider call, failures and cardinality mismatches included.

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

**A cursor value the pass cannot use is logged and ignored, never fatal (MAJOR, round 2).** The stored value is text in a key/value table, so it can be non-numeric, negative, or larger than any live user id — through drift, a hand-edited row, or a downgrade. The disposition is the opposite of the fingerprints': the pass logs it at WARNING once and **starts the cycle at the first user in the deterministic order**, which is precisely today's behaviour and is a complete, correct pass. A cursor is scheduling state whose worst possible consequence is an order; a fingerprint is a claim about what the stored rows *are*, and its worst consequence is a permanently wrong answer. Fail-closed belongs to the second and would be absurd on the first — a stray character in a bookkeeping row must not stop every tenant's indexing. An out-of-range value needs no special case beyond this: "the smallest id strictly greater than N" simply selects nothing and wraps to the first, which is the same outcome by the ordinary rule.

`_reindex_background` (`routes.py:2129`) and `scripts/rebuild_tsvectors.py` keep calling `_active_user_ids()` unrotated: an operator-triggered reindex is not the starvation vector, and giving it a cursor would let a panel click move the periodic pass's rotation.

**D5 — A per-user chunk and time budget on the embed stage, checked only at a note boundary.**
Two settings, `EMBED_CHUNK_BUDGET_PER_USER` (default 5,000) and `EMBED_TIME_BUDGET_SECONDS_PER_USER` (default 300, matching one `INDEX_INTERVAL_SECONDS`); `0` disables either. The budget is consumed by both the backlog loop and the reconciliation sweep, since both call the provider, and is checked at exactly the two places `_is_paused()` already sits — `indexer.py:2530` and `:2744` — **before** a note is started, never inside one.

**The chunk budget debits chunks *submitted*, never chunks stored (MAJOR, round 3).** Every provider call debits the number of chunks it sent, whatever it returned — a raise and a cardinality mismatch debit exactly as a success does. Debiting stored chunks would leave the budget untouched by a failing provider, so a tenant whose notes all fail would consume the whole pass, every pass, without ever reaching its bound: the starvation the budget exists to stop, reappearing inside it. The wall-clock budget does not save that case either, because an operator may disable it (`0`) and keep only the chunk budget, which is exactly how the regression test exercises this.

Three further clauses are load-bearing:

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
- **Endpoint identity is deliberately excluded — `OPENAI_BASE_URL` and `OLLAMA_URL` alike (owner-accepted residual).** Moving between `api.openai.com`, an Azure deployment and a compatible proxy, or repointing `OLLAMA_URL` at another host, is an infrastructure change; including it would demand a full vault re-embed for a move that usually serves the identical artifact. **What the fingerprint therefore cannot see is a change of model *artifact* under an unchanged name**: `bge-m3` is a mutable Ollama tag, so `ollama pull` can replace the weights behind it, and a second Ollama host can serve different weights under the same tag. The fingerprint records the configuration, not the artifact, and no value available to this process distinguishes them. This is an **accepted limitation** with an operator rule stated in the documentation beside the model keys: *a change of model artifact — re-pulling a tag, or repointing at a host serving different weights — requires `make reset-embeddings`, and nothing will detect it if you skip that.* Recorded as a residual rather than half-solved with a probe that would have to trust the endpoint it is checking.
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

1. The driver takes `INDEX_GENERATION_LOCK_KEY` **before it reads anything** (D7c2), so no writer can commit between its snapshot and its record.
2. It enumerates the owner scopes to rebuild as `SELECT DISTINCT user_id FROM notes_metadata` — every scope that **retains rows**, `NULL` included — not `_active_user_ids()`.
3. It rebuilds each scope with the existing per-owner rebuild, which is already atomic and already certifies each row it writes against owner, path and hash.
4. The whole multi-scope rebuild and the fingerprint write are **one transaction**. Any scope that does not report a *completed* rebuild rolls the entire operation back and writes no fingerprint, naming the scope that stopped it.

**The per-owner rebuild returns a typed outcome, because `0` already means two different things (MAJOR, round 2).** It returns a row count today, and `0` is both "this scope had nothing to do" and "this scope was **skipped** because its provenance is not settled" — the gate `index-integrity` puts on the keyword rebuild ("The unverified ancillary passes do nothing for a user whose provenance is not settled"). A driver that read `0` as success would record a fingerprint certifying a scope the rebuild deliberately declined to touch, which is the exact class of false claim D7b exists to remove. The rebuild therefore returns `RebuildOutcome.COMPLETED(rows)` or a named skip — `SKIPPED_PROVENANCE_UNSETTLED`, `SKIPPED_ROOT_UNPINNABLE` — and **any retained scope whose outcome is not `COMPLETED` aborts the driver**, names the scope and its reason, and prevents the fingerprint write. Only a completed rebuild of every retained scope licenses the record.

**An inactive owner with an assigned vault and settled provenance is rebuildable, and the driver resolves its root itself (MAJOR, round 3).** `_active_user_ids()` and `warm_user_vault_cache` are keyed to *active* users because that is what the periodic pass serves; the coverage proof is a different question — it is about **rows that exist**, and an inactive user's rows are as retained as anyone's. So the driver resolves each scope's root from `users.vault_path` **directly, read-only, inside the driver**, and pins it the way every other file-reading pass pins a root. It does not widen `_vault_root`, does not touch the active-user cache's semantics, and does not make an inactive user visible to anything else — the read is local to the maintenance operation. Only a scope with **no** assigned `vault_path`, or one whose path cannot be pinned, is `SKIPPED_ROOT_UNPINNABLE`; the provenance gate still applies to it exactly as it does to an active user. This narrows L5 materially: "an inactive user's leftover rows" is no longer a blocking state unless that user is also unassigned.

**A `NULL`-owned retained scope in multi-user mode aborts, by decision (MAJOR, round 2).** The ownerless scope cannot be rebuilt when `MULTI_USER_MODE` is on: `_vault_root(None)` refuses there by design, and substituting `settings.vault_path` would read one tenant's notes under an unowned scope — a tenancy violation to satisfy a bookkeeping row. Nor can such rows be silently excluded from the coverage proof: they are retained rows that `keyword_search` can return. So the driver **aborts**, naming them and the count, and the operator's prerequisite is to delete or reassign them. The pre-deploy check in `tasks.md` runs `select count(*) from notes_metadata where user_id is null` against production and requires zero before this change ships, so the state cannot first be discovered by a refusing startup. In single-user mode the ownerless scope is the *only* scope and rebuilds normally.

**All-or-nothing across scopes needs an explicit exception to the per-user skip rule, and gets one (MAJOR, round 3).** `index-integrity` states that the keyword-vector rebuild's provenance skip "SHALL be **per user**, not global: a user whose provenance is unsettled SHALL NOT prevent these passes from running for every other user." Rolling every scope back because one was skipped contradicts that as written, so the rule is **modified** rather than quietly overridden — the delta carries a narrow carve-out for the one operation that makes a *global* claim.

The carve-out is narrow in three ways, and each matters. It applies only to the **operator-invoked coverage-proof rebuild**, the one that records the fingerprint — not to the startup link backfill, and not to any pass on the periodic loop. It does not weaken the per-user gate itself: the skipped user still gets nothing written, which is exactly what the original rule demands. And the reason it must be global is structural rather than convenient: a fingerprint is one row asserting something about *every* retained row, so "all scopes rebuilt" is not a fact that can be established one user at a time. The original rule's purpose — one tenant's unsettled provenance not blocking another tenant's *ongoing* indexing — is untouched, because this operation is a one-shot an operator ran, not the loop that keeps the index fresh.

The cost is that a multi-tenant rebuild becomes all-or-nothing rather than per tenant; that is the price of the fingerprint meaning what it says, and the rebuild is already the cheap maintenance path (keyword index only, no provider calls, seconds for a few thousand notes).

**The escape hatch is what makes fail-closed safe here.** A scope that cannot be rebuilt — an unassigned user's leftovers, a tenant still in re-derive mode, ownerless rows under multi-user mode — blocks the fingerprint, so startup keeps refusing. Three remedies are stated in the refusal and in the runbook: settle the scope (assign or delete the user, or let the re-derive complete), delete or reassign ownerless rows, or **put `FTS_CONFIGS` back to the value the stored fingerprint names**, which clears the refusal immediately with no rebuild at all. A configuration edit is always reversible; that is what distinguishes this from an outage.

**D7c — One database-level generation lock, held across every mutation that depends on the configuration (BLOCKER, round 2).**
`make reset-embeddings` runs `docker compose run --rm` deliberately (#142), so it reads the edited `.env` and works while the service is up or down. That last property is the hole: with the live service still running the old configuration, the reset wipes the column, writes the **new** fingerprint, and the old container embeds the backlog with the **old** model and stamps `embedded_content_hash` — old-model vectors under a fingerprint claiming the new model, permanently, with every later startup silent because the stored value already matches.

The first draft answered this with a per-pass re-read of the fingerprint. **That is not sufficient, and the review is right.** The re-read happens once at the head of a user's embed stage; the interleaving that defeats it is ordinary rather than exotic:

```
old process: read fingerprint A == A, proceed
old process: get_embeddings_batch(note)      ← seconds to minutes, no lock held
reset:       wipe column, write fingerprint B, commit
old process: certify_embedded + insert vectors   ← old-model vectors, stamped, under B
```

The check and the act are separated by a network call. So the enforcement is a **transaction-scoped PostgreSQL advisory lock** — `pg_advisory_xact_lock` on one fixed key, `INDEX_GENERATION_LOCK_KEY`, a documented 64-bit constant declared once in `src/services/index_state.py` and never derived at runtime from a hash that could drift between builds.

- **Every maintenance operation that mutates the generation takes it first**, before its wipe, its rebuild, or its fingerprint write: the CLI reset, the panel reset, and the keyword rebuild driver.
- **Every transaction that writes a configuration-dependent derived row takes it too** — *after* the provider call and *before* the certification, which is exactly the window `index-integrity` already reserves ("issued **before** any stored vector is deleted or inserted … and **after** the embedding provider call, so no row lock is held across a network request"). Under the lock the transaction **re-reads the fingerprint** and, on a mismatch, refuses: it certifies nothing, inserts nothing, deletes nothing, and leaves the row for a later pass, which is the same disposition `StaleCertification` already has.
- **On the embed path that window exists only inside `embed_note`** (MAJOR, round 3). `get_embeddings_batch` (`embeddings.py:521`) and `certify_embedded` (`:541`) are twenty lines apart in the *same* function, and the caller in `indexer.py` sees only the result — there is no point between them the indexer can reach. So the acquisition and the re-read live in `embed_note` itself, between those two statements, and this is a Slice B change rather than a Slice E one. A mismatch there returns a fifth outcome, `GENERATION_MISMATCH`: nothing certified, nothing written, not counted as embedded and **not counted as a failure** (nothing went wrong with the provider), logged at ERROR. It is an *attempt*, because a provider call was issued — which is the `attempted` rule applying unchanged rather than gaining an exception.
- **Lock ordering is fixed and stated: the advisory lock is acquired before any row or table lock** in any transaction that takes it. One direction everywhere, so the new lock cannot close a cycle with the row locks the pass and the panel already contend for.
- **It is a transaction-scoped lock, never a session lock.** `pg_advisory_xact_lock` releases at commit or rollback, so a crashed pass cannot strand it; a session lock leaked into a pooled connection would be held by whatever ran next.

**The exclusion branch is deliberately exempt**, and the exemption is argued rather than assumed: it makes no provider call, writes no vector, and stamps a row to record that an *excluded* note has been dealt with — a claim that is true under any model, because the correct vector set for an excluded note is the empty one. It therefore has nothing a generation change can invalidate. It keeps its existing `certify_embedded` predicate and its existing per-note commit.

**The pass-level re-read stays as the cheap early exit**, not as the enforcement: `_embed_vault_pinned` reads the fingerprint once per user stage and skips the stage entirely on a mismatch, so an old container stops doing work within one tick instead of grinding through a backlog whose every certification the lock will refuse. The lock is what makes the guarantee; the re-read is what makes it cheap.

**The ordering is still documented, because a runbook that avoids the race is better than one that survives it.** For any change to the embedding configuration: edit `.env` → `make deploy` (the new image refuses at the fingerprint or dimension guard and stays down, embedding nothing) → `make reset-embeddings` while it is down → restart. This inverts the pre-fingerprint advice in `README.md:915`, which told operators to reset *before* recreating; that ordering was safe only because nothing then depended on a stored claim. The lock is what makes an operator who ignores the runbook lose time rather than correctness.

**D7c2 — The same lock covers every keyword-vector writer (MAJOR, round 2).**
The identical check-then-act gap exists on the keyword side, one step removed. The rebuild driver can rebuild every retained row under the new `FTS_CONFIGS` and record the fingerprint while an old-configuration container's *incremental* pass writes `content_tsvector` for a changed note under the previous configuration — and because a keyword vector is only ever rewritten when a note's `content_hash` changes, that row then stays on the old configuration for ever behind a fingerprint claiming otherwise. That is the same permanent, silent staleness the fingerprint exists to prevent, arriving through the writer the fingerprint does not describe.

So **every writer of `content_tsvector` takes the same `INDEX_GENERATION_LOCK_KEY`**, re-reads the keyword fingerprint under it, and refuses to write when it differs from the configuration the process is running — which, for the incremental pass, aborts that pass with nothing committed, exactly as a floor failure already does. Symmetrically, the **rebuild driver takes the lock before it snapshots or reads any row**, not merely before its fingerprint write, so no writer can commit between the snapshot and the record.

**D7c3 — The incremental pass takes the lock at the head of its transaction, not at its tsvector write (BLOCKER, round 3).**
Taking the lock where the tsvector is written would have inverted the ordering rule and produced a real deadlock, not merely an inelegance. The incremental index pass is **one transaction** (`indexer.py:1448` → `:1926`) and it mutates `notes_metadata` long before it reaches the tsvector: the upsert of each changed note, the id-preserving move UPDATE, the prune DELETE, the `note_links` delete-and-insert, and the grammar-invalidation `UPDATE … SET embedded_content_hash = NULL`. All of those take row locks. So:

```
pass:    upsert notes_metadata rows        (holds row locks)
rebuild: pg_advisory_xact_lock             (holds advisory)
rebuild: rebuild those rows                → waits on the pass's row locks
pass:    pg_advisory_xact_lock at the write → waits on the rebuild's advisory
```

— a cycle the database resolves by killing one side, and a direct violation of "advisory before any row or table lock".

The rule is therefore stated as a property of the **transaction**, not of the statement that happens to need the fingerprint: *a transaction that will write any configuration-dependent derived row acquires the generation lock and re-validates the fingerprint before its first row-locking mutation.* For the incremental pass that is the head of the pass transaction, before the first upsert. Implementation is required to **audit every mutation earlier in that transaction** rather than reason from the tsvector write backwards, and the audit list above is the starting point, not the whole answer.

Two consequences, both accepted and documented:

- **The pass holds the generation lock for the duration of its transaction** — minutes on a large vault. `make reset-embeddings` and `make rebuild-tsvectors` therefore *wait* for an in-flight pass instead of interleaving with it, which is the behaviour we want and which the maintenance paths must not defeat with a short `lock_timeout` (the reset already runs under `statement_timeout = '5min'`; an advisory-lock wait is subject to `lock_timeout`, so it is left unset or set generously on those paths, deliberately).
- **The embed path is unaffected and stays fine-grained.** Its per-note transaction's first row-locking mutation is `certify_embedded`, and the lock is taken immediately before it — inside `embed_note`, after the provider call. Nothing earlier in that transaction takes a row lock (the ORM re-read at `indexer.py:2602` is a plain `SELECT`), so the head-of-transaction rule and the after-the-provider-call rule agree there rather than conflicting.

A two-connection deadlock regression test drives the exact interleaving above and asserts that neither side is killed.

*One key, not two.* The two subsystems guard one fact — "which configuration the derived rows were built under" — and a single key makes the ordering rule trivially total. The cost is that an embed certification and a tsvector write serialise against each other across processes; within a process they already serialise under `index_pass_lock`, and across processes serialising them is the point.

*Alternative rejected — have the rebuild refuse while an old writer could run.* There is no way to ask that question: "an old writer" is another container the database cannot enumerate, and a heuristic on `pg_stat_activity` would be a guess with a silent-corruption failure direction.

**D7d — A fingerprint write that fails aborts its maintenance transaction (MAJOR).** The "recording never fails a pass" rule (`indexer.py`, `_write_indexer_run`) covers *instrumentation* — the run history and the rotation cursor — where a lost write costs an operator a view. A fingerprint is not instrumentation: it is the claim a later startup refuses on. A reset that wiped the column and then swallowed a failed fingerprint write leaves the stored value naming the **old** configuration over rows that are about to be built with the new one, and the next startup refuses on a database that is actually consistent — or, worse in the other direction, a rebuild that rebuilt everything and lost its write keeps refusing forever. So the fingerprint upsert is inside the maintenance operation's transaction and a failure rolls that operation back and surfaces to the operator who invoked it. Swallow-on-failure stays exactly where it was: the cursor write and `_write_indexer_run`.

**D8 — The dashboard shows currency beside coverage, and does not redefine coverage.**
`stats.embedding_pct` counts notes with at least one vector row (`routes.py:398-407`); the reset poller counts notes whose `embedded_content_hash` equals their `content_hash` (`routes.py:2092`). They are different questions and both are worth answering, so the dashboard gains `notes_pending` (stale or never-embedded) and `notes_chunks_truncated` beside the existing bar, and the bar's meaning is unchanged.

The poller's query is admin-only and **unscoped** — it counts the whole table. The dashboard is scoped by `_scope_user_id(user)`, so the new counts must be scoped the same way as the coverage numbers directly above them, or a regular user reads another tenant's backlog as their own. The shared predicate goes in one helper used by both call sites so the two cannot drift; the poller keeps its unscoped, admin-only behaviour.

A non-zero pending count on an otherwise-idle vault is exactly the shape a provider outage now takes, and it is the operator-visible signal D5's budget stop deliberately does not write into `error`.

**D9 — The reconciliation sweep reports into the same accumulator, and `attempted` is defined (MAJOR).**
`_reconcile_exclusions` re-embeds re-included notes (`indexer.py:2807`) and today swallows their failures into its own `except Exception` with nothing riding back to the pass. A provider outage during a pass whose *backlog was empty* — the steady state of a fully-indexed vault, where the sweep is the only thing making provider calls — therefore still writes a clean run row, which is #201 surviving in the one code path #201's fix would not have touched.

The sweep takes the same `EmbedPassResult` and calls the same `record_failure_detail`. The denominator has **one** increment point, and the first draft did not: it said "backlog rows selected + sweep rows that reached `embed_note`" in one place and "reached the provider" in another, which differ by every zero-chunk note (`embed_note` certifies those without calling the provider at all).

> **`attempted` is incremented exactly once per note for which an embedding provider call is issued.** It is incremented at that call site — in `embed_note`'s caller path — and nowhere else.

Three consequences follow and are stated because they change existing behaviour:

- **`attempted` is no longer initialised from the backlog size.** `EmbedPassResult(attempted=len(unembedded))` becomes a counter that starts at zero and rises with each provider call, so it counts work done rather than work contemplated.
- **A zero-chunk note is not an attempt.** It certifies, it counts into `notes_embedded`, and it makes no provider call, so a pass over 400 notes of which 50 clean to nothing reports `… of 350`.
- **A sweep row decided without a provider call is not an attempt.** The sweep scans every certification-current row in the scope (~16,700 on the production vault) and decides about almost all of them without calling anything; counting those would render three failures out of three calls as "3 of 16,700".

Every other non-attempt already follows from the same rule rather than needing its own clause: an excluded note, a hash-mismatched note, a note left by a pause or a budget stop, and a certification that matched no row all issue no provider call for that note, so none of them moves the denominator. The rule is one sentence and the exclusions are its consequences, which is what makes design, requirement, tasks and tests able to agree.

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
- **[D7c's advisory lock is taken by every certifying and every tsvector-writing transaction]** → one uncontended `pg_advisory_xact_lock` per such transaction, released at commit. It serialises those writers across processes, which is the point; within a process `index_pass_lock` already did. The ordering rule (advisory before any row or table lock) is what keeps it from closing a cycle.
- **[D7c's lock makes an old container's pass fail loudly rather than quietly]** → an old-configuration container after a reset now refuses every certification instead of writing wrong vectors. It logs, records nothing, and its pass reports no work. That is the intended trade.
- **[D7 adopts on absence, so the first startup blesses whatever is configured]** → if an operator changes the configuration *in the same deploy* that ships this change, adoption blesses it. Unavoidable — there is no evidence to compare against — and stated in the migration plan: **deploy this change with the embedding and FTS configuration unchanged.**
- **[D1 changes `embed_note`'s return type]** → every caller and every existing test that compared it to an integer is enumerated in `tasks.md` and assigned to a slice; the merge gate in §8 greps for callers of every new export.
- **[Migration 023 on a deploy]** → one new table plus one additive constant-default column; `make test-schema` is the gate and `alembic check` must be clean after.

## Accepted limitations

This is the last spec round before implementation, so every residual is listed here explicitly rather than left in prose. Each is a thing this change **does not** do, with the reason and the operator's recourse. None is a defect to be found later.

| # | Limitation | Why it is not closed | Recourse |
| --- | --- | --- | --- |
| L1 | **A model artifact can change under an unchanged name.** `bge-m3` is a mutable Ollama tag, and `OLLAMA_URL` / `OPENAI_BASE_URL` are excluded from the fingerprint, so re-pulling a tag or repointing at a host serving different weights mixes vector spaces undetected. | The fingerprint records configuration, not artifact; no value this process can read distinguishes them, and a probe would have to trust the endpoint it is checking. Including endpoint identity would force a full re-embed for an infrastructure move that usually serves the identical artifact. | **Operator rule, documented at the model keys:** a change of model artifact requires `make reset-embeddings`, and nothing will detect it if you skip that. |
| L2 | **An edit the scan has not yet committed is served as fresh.** Between the write landing on disk and the next pass reaching the note, `stale` reads false while the chunk text is superseded. | Detecting it means hashing the file on disk for every returned row — a filesystem read on the hot path of every search — and it still races the writer. | Bounded by `INDEX_INTERVAL_SECONDS` plus the pass in flight. Stated in the tool docstrings and pinned by a test. |
| L3 | **The scan and the one-shot link backfill are unbudgeted.** A tenant with an enormous vault still delays the next tenant through those stages. | Each is a single transaction over a vault walk; stopping one part-way means committing a partial derive (A.7a forbids it) or discarding the pass's work. | The measured starvation was the embed stage's; filed as a residual on #202. |
| L4 | **The cross-tenant delay is the budget plus one note's embedding time.** With the cap at 1,000 and a 30 s per-call bound that is ~8.3 h arithmetic worst case on a provider answering at its timeout. | The only tighter bound is an aggregate deadline, which is exactly the construct #127 removed because it produced a note the pass could never finish. | Owner-accepted. A provider that slow trips #201's outage detector on the notes around it. |
| L5 | **A multi-tenant keyword rebuild is all-or-nothing, and a scope that cannot be rebuilt blocks it — and therefore, with FTS failing closed, blocks startup after an `FTS_CONFIGS` change.** Reachable by an **unassigned** user's leftover rows, a tenant still in re-derive mode, or ownerless rows under multi-user mode. An *inactive but assigned* user is **not** a blocking state: the driver resolves and pins that owner's `vault_path` itself (D7b, round 3). | A fingerprint written over an unrebuilt scope certifies rows still on the previous configuration, which is the exact false claim the fingerprint exists to remove; and "all scopes rebuilt" is not a fact that can be established one user at a time. | Settle the scope (assign or delete the user, let the re-derive finish), delete or reassign ownerless rows, **or revert `FTS_CONFIGS`**, which clears the refusal immediately with no rebuild. §8.7 checks for the reachable states before the deploy. |
| L5b | **The incremental index pass holds the generation lock for its whole transaction**, so `make reset-embeddings` and `make rebuild-tsvectors` wait for an in-flight pass — minutes on a large vault. | The pass mutates `notes_metadata` from its first statement, and the ordering rule (advisory before any row lock) puts the acquisition at the head of that transaction. Taking it later is the deadlock D7c3 describes. | Waiting is the correct behaviour — a reset must not land mid-pass. The maintenance paths deliberately do not set a short `lock_timeout`. |
| L6 | **`NULL`-owned `notes_metadata` rows abort the rebuild when `MULTI_USER_MODE` is on.** | `_vault_root(None)` refuses there by design, and substituting `settings.vault_path` would read one tenant's notes under an unowned scope — a tenancy violation to satisfy a bookkeeping row. Silently excluding them would certify rows `keyword_search` can still return. | Owner decision. Delete or reassign them; §8.7 requires `select count(*) from notes_metadata where user_id is null` to be zero in production before this ships. |
| L7 | **Raising `MAX_CHUNKS_PER_NOTE` forces a full re-embed**, although it only widens coverage. | A comparison rule that knew which direction was safe would have to reason about every field jointly — a larger cap with a smaller chunk size is not a widening. | Accepted; the cap is not expected to move. |
| L8 | **The first startup after this change adopts whatever is configured.** A configuration changed in the same deploy is blessed rather than caught. | There is no prior evidence to compare against; refusing instead would take every existing deployment down on upgrade. | Deploy this change with the embedding and FTS configuration unchanged (§8.8). |
| L9 | **A capped note's tail is not semantically searchable at all.** | That is what the cap is. | Marked on the row, in every vector result, and counted on the dashboard; the note stays fully keyword-searchable. |
| L10 | **`semantic_search` hydrates every candidate's full vector** to recompute a similarity the query already returned as `distance`. | Pre-existing and unrelated to these four findings; changing it here would widen the blast radius of a change that already touches both read paths. | Filed as a follow-up issue in §8.11. |

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
- **Generation-lock takers (D7c/D7c2/D7c3)** — **inside `embed_note`** (`embeddings.py`, between `get_embeddings_batch` at `:521` and `certify_embedded` at `:541`), which is the only place that window exists, so both callers (`indexer.py:2622` backlog and `:2807` sweep) inherit it; the **head of the incremental pass transaction** (`indexer.py:1448`, before the first `notes_metadata` mutation — *not* at the tsvector write at `:1820`); `_rebuild_tsvectors_pinned` (~`:2907`) via its driver, which takes it before reading; `scripts/reset_embeddings.py`; the panel reset paths. **Not** the exclusion branch (`indexer.py:2555`, `:2767`), which writes no vector — exemption argued in D7c.
- **Mutations to audit before the incremental pass's lock acquisition (D7c3)** — the changed-note upsert, the id-preserving move UPDATE, the prune DELETE, the `note_links` delete-and-insert, and the grammar-invalidation `UPDATE … SET embedded_content_hash = NULL` (`indexer.py:1793-1809`). The list is the starting point for the audit, not a claim to be complete.
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
14. **"The unverified ancillary passes do nothing for a user whose provenance is not settled"** (`index-integrity`) — the per-user gate is preserved exactly (a skipped user still gets nothing written); the requirement is **modified**, not overridden, to carve out the one operator-invoked rebuild that records a global fingerprint and therefore cannot commit per user.
15. **The certification window is unchanged** (`index-integrity`, "after the embedding provider call … before any stored vector is deleted or inserted") — the advisory lock is acquired inside exactly that window, which on the embed path exists only inside `embed_note`, so no lock of any kind is held across a network request.
16. **Lock ordering is one direction everywhere** — the advisory generation lock before **any** row or table lock, in every transaction that takes it. Stated as a property of the transaction, not of the statement that needs the fingerprint: the incremental pass acquires it at the head of its transaction, before its first upsert, because taking it at the tsvector write would deadlock against a rebuild holding the advisory lock and waiting on those rows.
17. **`alembic check` clean** — 023's markers are mirrored on the ORM table and column.
18. **`clean_at_version`'s "unknown version counts as differs"** — the fingerprint's unknown-`v` rule is the same rule in a new place.

## Migration Plan

1. `make test-schema` before the deploy (023 carries a table, a CHECK and a column). The gate's asserted head moves from 017 to 023.
2. **Deploy this change with `EMBEDDING_PROVIDER`, the model name, `EMBEDDING_DIMENSIONS`, `CHUNK_SIZE`, `CHUNK_OVERLAP` and `FTS_CONFIGS` unchanged.** The first startup adopts whatever it finds; adopting a configuration changed in the same deploy would bless rows built under the previous one.
3. `make deploy` → `make db-check` clean.
4. First startup writes both fingerprints and logs each adoption once at WARNING. `indexer_state` then holds two rows; the cursor row appears after the first pass.
5. No re-embed and no re-extraction: `CURRENT_EXTRACTION_VERSION` is untouched, `chunks_truncated` defaults to the correct value, and no note's `embedded_content_hash` is cleared.
6. Rollback: revert the code. `indexer_state` and `chunks_truncated` are inert to the previous build — it neither reads nor writes them — so a revert costs nothing and a re-deploy adopts the fingerprints again. `downgrade()` drops only what carries 023's markers.

**The runbook this change replaces (D7c), recorded here and in `README.md`:**

- *Changing the embedding provider, model, dimensions, chunk size, chunk overlap or the chunk cap:* edit `.env` → `make deploy` → the new container refuses at the fingerprint (or dimension) guard and stays down → `make reset-embeddings` while it is down → restart. The refusal is what keeps anything from embedding during the reset; the generation lock (D7c) is what makes a skipped step cost time rather than correctness.
- *Changing `FTS_CONFIGS`:* edit `.env` → `make deploy` → the new container refuses at the keyword fingerprint guard → `make rebuild-tsvectors`, which takes the generation lock, rebuilds **every** scope holding rows, and writes the fingerprint only if every one of them reported a completed rebuild → restart. If a scope reports a skip — provenance unsettled, root unpinnable, or ownerless rows under multi-user mode — the command names it and its reason and writes nothing. Recourses, in order of preference: settle the scope, delete or reassign the rows, or put `FTS_CONFIGS` back, which clears the refusal with no rebuild at all.
- *Before shipping this change at all:* `select count(*) from notes_metadata where user_id is null` on production must return zero while `MULTI_USER_MODE` is on (L6). Ownerless leftovers do not hurt anything today, and would first be discovered as a refusing startup after some future `FTS_CONFIGS` edit — which is the worst possible time to learn about them.

## Rejected findings

Nothing from either pre-code review round was rejected outright. Three findings were folded in with a **narrower scope or a different mechanism than proposed**, and each narrowing is recorded here so it is reviewed rather than assumed:

- **Round 1 — "Budget the scan and the link backfill too, if cheap."** Not done. Both are single transactions over a vault walk, so a budget there means either committing a partial derive — which A.7a exists to forbid — or discarding the pass's work at the stop, and neither is cheap. The fairness claim is narrowed instead (D5b, stated in the spec), and the residual is L3.
- **Round 1 — "Per-owner generation state" as an alternative to a coverage-proving rebuild.** Not taken. It would need one `indexer_state` key per owner, which the closed-set CHECK forbids by design, or a per-row column, which the per-row Non-Goal argues against for the vector case and which has no better claim here. The single-transaction, all-scopes rebuild (D7b) proves the same thing with no new schema and no new key shape; its cost is L5.
- **Round 2 — "…or have the rebuild refuse while an old writer can run."** Not taken; the lock is used instead. There is no way to ask that question: "an old writer" is another container the database cannot enumerate, and a heuristic over `pg_stat_activity` would be a guess whose failure direction is a silent, permanent staleness. The generation lock answers the question the refusal was trying to approximate, and answers it exactly.

## Open Questions

1. **`EMBED_CHUNK_BUDGET_PER_USER=5000` / `EMBED_TIME_BUDGET_SECONDS_PER_USER=300`, enforced only when the pass serves more than one active scope.** The single-scope clause is what keeps the default deployment byte-identical to today; always-enforcing spreads a first index of 2,577 notes across several five-minute-spaced passes. *Recommendation: as stated.* This is the only genuinely open question left; everything else that was open is now an accepted limitation above.
