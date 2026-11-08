## Context

`TeiProvider` (`src/services/embeddings.py`, merged at 0227f73 on `feat-tei-embeddings`) sends `{"inputs": [...], "truncate": true}` to `/embed`, in slices of `TEI_EMBED_BATCH_SIZE`, with the OpenAI provider's retry loop copied in. It has its own fingerprint branch (`index_state.active_embedding_model`), transport policy branch (`Settings._validate_embedding_transport`, `log_embedding_transport`) and panel card.

What the code does around it today, read on 0227f73:

- `_check_embedding_dim` and the fingerprint guards in the lifespan `sys.exit(1)` on a proven mismatch. They touch only the database.
- **No provider is contacted by a startup check.** Ollama's warm-up is best effort and never fatal, and OpenAI gets a key-presence check. A down Ollama or OpenAI today means a running server whose embedding calls fail per note.
- `rerank.startup_probe` never exits, logs one CRITICAL, and a background loop re-probes.
- `embed_note` catches every provider exception into `PROVIDER_FAILED` with `EmbedNoteFailure.capture` (class name and `str(exc)`). `hybrid_search` degrades on any exception. `semantic_search` translates only `ProviderInputTooLarge`.

### Claims to confirm

When this proposal was written nothing here had been run against a real server (the docs lookup failed too), so these came from reading #424 and from recollection of TEI's source. Task 7 has since run them against `ghcr.io/huggingface/text-embeddings-inference:cpu-1.8` (version 1.8.3, sha 78502d8) with `sentence-transformers/all-MiniLM-L6-v2`; the outcomes are under "TC outcomes" below. Marked (TC) where used:

- TC1. `GET /info` returns JSON with `model_id` and `max_client_batch_size` (field names and types).
- TC2. `model_id` echoes the `--model-id` argument, including a local path.
- TC3. With `truncate: true`, an over-limit input returns 200 with a vector, and neither the "less than N tokens" nor a "less than N characters" validation fires.
- TC4. A request above `--max-client-batch-size` answers 413 with `batch size N > maximum allowed batch size M`.
- TC5. `/v1/embeddings` exists on the TEI version named in the README, and the claim about its version floor.
- TC6. A healthy `/embed` answers a bare `[[...], ...]`.

If TC3 is false, D4 is reopened before implementation continues. If TC1 is false, D1's field names change.

### TC outcomes

- TC1 confirmed: `/info` has string `model_id`, integer `max_client_batch_size`.
- TC2 confirmed: `model_id` echoes `--model-id` for a Hub id and for a local snapshot path.
- TC3 confirmed: `truncate: true` returned 200 with one vector at 13,500, 108,000 and 70,000 CJK characters; `truncate: false` returned 413 (`must have less than 256 tokens`). The only other refusal under `truncate: true` is the ~2 MB body limit (L7).
- TC4 confirmed: 5 inputs at `--max-client-batch-size 4` returned 413 with `batch size 5 > maximum allowed batch size 4`.
- TC5 partly: `/v1/embeddings` exists on 1.8.3; no version floor established, so the claim is removed from the README. Observed default `max_client_batch_size` is 32, not 96.
- TC6 confirmed: bare array of arrays, one row per input.

## Goals / Non-Goals

**Goals:** the specs describe what ships; a wrong server cannot write vectors; a malformed answer never reaches pgvector; Ollama and OpenAI behaviour is unchanged.

**Non-Goals:** `TEI_API_KEY` (#426); detecting replaced weights behind one model name (existing fingerprint L1); per-note truncation reporting; pre-warming TEI.

## Decisions

### D1. `/info` unreachable at startup: start, but withhold embedding until verified

Rule: **the process exits only on positive evidence of a mismatch; a vector is requested only on positive evidence of a match.**

| `/info` result | Startup | Embedding |
| --- | --- | --- |
| 200, matches | proceed | allowed for the process lifetime |
| 200, `model_id` differs or batch too large | CRITICAL, `sys.exit(1)` | n/a |
| transport error, timeout, non-200, non-object body, no usable `model_id` | one CRITICAL, keep serving | withheld, re-checked at most every 30 s |

Why not exit when unreachable. Ollama and OpenAI do not stop the server when the embedder is down, and TEI must not be made worse than they are. TEI is often a sibling pod that needs minutes to load a model, so an exit becomes a crash loop that takes keyword search, reads and writes down with it. The reranker's precedent is the same argument.

Why not just warn and proceed, which is what the reranker does. A reranker failure is disclosed in each response and writes nothing. Here, the case that the check exists for is a wrong model, and "unreachable at startup, wrong model a minute later" is what happens when both pods are restarted by one rollout. A start-only check would write wrong-space vectors for the whole process lifetime. So the gate lives in `TeiProvider`, and a server that has not proven itself gets no `/embed` traffic. Search and indexing during that window fail through the ordinary provider-failure path: `PROVIDER_FAILED`, previous vectors intact, `/health` degraded by the existing counters, `hybrid_search` keyword-only. That is what an Ollama outage already looks like.

The gate also covers maintenance scripts that never run the lifespan, because it sits in the provider.

**A late mismatch does not exit.** A process already serving reads and writes should not die from a sibling's rollout. Every embedding call fails with the reason, one CRITICAL marks the verdict change, and a corrected server recovers without a restart.

**Non-200 includes 401** from a server started with `--api-key`. Until #426 such a server cannot work with this provider anyway, and the refusal text names the status.

**Implementation notes.** One function, `verify_tei_server()`, used by both lifespan and gate. Module-level state (verified flag, last reason, last check time) rather than a lock: concurrent checks are harmless, an `asyncio.Lock` would bind to one loop, and the maintenance scripts run their own. The check time is stamped before the await so concurrent callers respect the 30 s interval. The request uses `_tei_client()`, one attempt, with the whole exchange inside `asyncio.wait_for(..., 5)`. An httpx `timeout=` bounds each phase, not the total (see D3), so on its own it would let a trickling `/info` hold the lifespan indefinitely; the 60 s client default is for `POST /embed` attempts only. Reaching the 5 s bound is an inconclusive result, the same as any other timeout. The lifespan call sits below the sandbox short-circuit, after the fingerprint guards so that the more specific "run the reset" message wins.

**A missing `max_client_batch_size` passes with a WARNING** (TC1). Unlike the model, too large a batch fails loudly with a 413 on the first full slice. It cannot produce wrong vectors.

**Exact string comparison.** No case folding or path normalisation: the operator's remedy is to copy the reported value. The value also goes into the fingerprint, so `tei` deployments change it once, before release.

### D2. Response validation sits in the provider

Done in `TeiProvider` so that every caller gets it. Each row: a `list`, length `EMBEDDING_DIMENSIONS`, each element `int` or `float` but not `bool`, and finite. Finite matters because Python's `json` accepts `NaN` and `Infinity`, and pgvector refuses them late. A JSON integer too large for a float must be rejected without raising `OverflowError` out of the check, so the implementation converts inside a guarded helper. The failure is a `RuntimeError` naming the row index and observed width and no input text, which `EmbedNoteFailure.capture` already stores. A non-JSON 200 is that same `RuntimeError`, not retried: it is not in the retry statuses and a misrouted proxy does not heal in 3 s.

### D3. A 90 s deadline per slice

`asyncio.wait_for` over one slice's `_post`, the same place Ollama's sits. 30 s was rejected because TEI's attempt timeout is 60 s. A value of exactly 60 s would make retry unreachable after a hung attempt. 90 s admits one full timed-out attempt, its backoff and a normal-speed retry. The worst case falls from ~183 s to 90 s. Note httpx's `timeout=` bounds each phase, not the total, so `wait_for` is also what bounds a server that trickles bytes.

It is per slice, not per batch, so the D5/D15 argument against aggregate deadlines is intact: the bound covers a fixed amount of work whatever the note's size. `embed_one` gets the same bound (Ollama's `embed_one` has only the request timeout, unchanged here). `TimeoutError` has an empty `str()`, so it is converted to a `RuntimeError` that names the deadline; otherwise the recorded failure message is blank. A constant, not a setting, following the other request bounds.

### D4. Remove TEI input-limit handling; delete the phrase

Under `truncate: true` the server truncates rather than refuses (TC3), so the `_input_limit_reason` branch in `TeiProvider` cannot fire. **From the repo alone no TEI refusal reaches it**: the only message #424 targeted is the "less than N tokens" one, which TEI emits when not truncating. The test `test_the_token_limit_is_translated_and_not_retried` simulates a response that a `truncate: true` client should not receive.

Scoping the phrase to TEI or anchoring it to input wording were the alternatives. Once the TEI branch is gone, no provider needs it, so deletion is simpler and exact: Ollama and OpenAI classification is byte-for-byte what it is on `main`. `max_tokens must be less than 4096 tokens` then matches no remaining alternative (checked against `_INPUT_LIMIT_PHRASES`: `maximum ... tokens` needs the word `maximum`, and no input-shaped word is present). `_TEI_INPUT_LIMIT_CODES` goes too.

If such a 413 does arrive (a TEI version ignoring `truncate`), it fails as an ordinary non-retried provider error. `mcp-rate-limits`' translation requirement is conditional on a provider rejecting input, so it needs no delta.

### D5. Shared retry helper

`_post_with_retries(client, url, *, label, headers, payload, timeout, max_attempts, base_delay, parse, input_limit)` holds the loop. `parse(response)` handles a 200. `input_limit` is a callable returning the exception to raise, or `None` for TEI. OpenAI passes its existing parse and `_OPENAI_INPUT_LIMIT_CODES` check and keeps the log text and order of decisions. "Unchanged" is pinned by characterization tests written against the unrefactored code before the extraction (task 1.1), and the retry requirement now states what that code does, confirmed by running it on 0227f73: `httpx.HTTPError` is retried with 1 s then 2 s backoff and no log line; exhaustion re-raises the last attempt's exception object; 429/5xx exhaustion raises `HTTPStatusError`; a non-JSON 200 raises `json.JSONDecodeError` after one request. OpenAI's non-JSON 200 stays that way (TEI's becomes a `RuntimeError`, D2), so the helper's `parse` must run outside the transport-error `try`.

### D6. Requirement retained as-built: transport errors are retried

The issue names 429/5xx. The code, like OpenAI's, also retries `httpx.HTTPError`, and the requirement says so rather than removing it.

## Risks / Trade-offs

- **Withheld embedding looks like an outage while TEI is starting.** Intended, and bounded by the 30 s re-check. Indexing retries on the next pass.
- **A 30 s window after TEI becomes ready** where calls fail fast without HTTP. Accepted over probing per call.
- **`/info` adds a coupling to a server endpoint** whose fields are not formally versioned here; TC1 and the tolerant batch-field rule limit the damage, and a missing `model_id` fails closed rather than silently.
- **Test churn:** every TEI test now needs an `/info` mock and full-width vectors. A shared fixture absorbs this.

## Accepted limitations

- **L1 (owner decision). Over-limit chunks are truncated.** With `truncate: true` the server embeds only the leading tokens, the rest of that chunk is unsearchable by vector, and the note is certified anyway, with no marker. Consistent with Ollama's truncation to model context. `CHUNK_SIZE` is the operator's control. Queries truncate the same way.
- **L2. Verification is once per process.** A TEI server replaced with another model while this process runs is not noticed until restart. It is the same operator rule as the fingerprint's L1: a change of model needs the reset.
- **L3. Same name, different weights** stays undetected (fingerprint L1). `model_sha` is not compared, because there is no stored value to compare it with.
- **L4. `/info` is the server's own claim.** A hostile or misconfigured proxy can report anything. The check catches misconfiguration, not an adversary on the hop, which is the transport policy's job.
- **L5. Out-of-range finite values.** A finite value above float32 range passes validation and fails at pgvector; only a broken server can produce it, and it fails loudly.
- **L6. Fail-fast during a check.** Concurrent callers on an unverified process fail fast while one `/info` check runs.
- **L7. Very large bodies.** A request body of about 2 MB or more is refused by TEI with 413 as an ordinary non-retried provider error; a default slice is about 32 KB, so only a very large operator-set `CHUNK_SIZE` reaches it.
- **L8. Task 7.7 not run.** The end-to-end MCP tool exercise was not run because no TEI deployment exists; real-provider startup and embedding were exercised against a live TEI 1.8.3 instead, with `semantic_search`, `hybrid_search` and `find_related` not called.

## Migration Plan

Nothing to migrate: `tei` is unreleased. At archive, update the `embedding-providers` Purpose line, which the delta format cannot change, to mention TEI.

## Open Questions

None blocking. TC1–TC6 are settled by task 7 (TC outcomes above).
