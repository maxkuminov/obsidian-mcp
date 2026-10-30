## Context

### What the code does today (verified on ed7d334)

- **`semantic_search`**: `semantic_search_impl` (`src/mcp_server/tools.py`) opens `async with async_session()` and calls `embeddings.semantic_search(session, …)`. The service calls `get_embedding(query)` before any statement, so no connection is checked out during the provider call. It then issues the three `SET LOCAL`s and one statement ordered by the `halfvec` expression with `LIMIT max(5 × limit, 50)`, with an exact re-run on zero rows. It re-sorts by full-precision distance, dedupes per note, truncates to `limit`, and annotates `stale` (with `chunk = None`) and `embedding_truncated`. It records `embed_ms`, `db_ms`, `exact_fallback` and `record_results(paths)`. The impl renders two lines per row: the row, then `  > {chunk[:200]}...`. Title and path are raw, and the chunk is not one-lined (hybrid-search L9). `ProviderInputTooLarge` becomes an `argument_too_long` refusal with the post-body marker `provider_input_rejected`. `limit` is clamped to `_MAX_SEMANTIC_RESULTS = 50`.
- **`hybrid_search`** does not exist on `main`. The `hybrid-search` proposal (branch `hybrid-search`) defines it: RRF over a keyword arm (50 rows) and a semantic arm (50-chunk overfetch, deduped, untruncated), in one session and one `REPEATABLE READ, READ ONLY` snapshot, with the embedding computed before the transaction. At 2d5b981 (after two Codex rounds) each row is one line: a fixed-key JSON object `{"title","path","tags","preview_kind","preview"}` with every value `change_feed.quote`d and `preview` last, then the separator `"} — `, then the server fields. Size bounds are on rendered (escaped) length, and its eval membership is the new `keyword_or_semantic` with per-constituent corpus loss. The registry holds 28 tools. It degrades to `arms: keyword-only` on a provider failure, sits in class `embedding`, and records `result_paths` from the rendered rows. Its D1 refactor adds `semantic_arm_rows(…, overfetch)`, which returns the untruncated deduped list. Its harness arm reads `--arm-opt hybrid_k`, and it adds `ARM_OPTS` to `make search-eval`.
- **The result `chunk` is cut at 500 characters in the service** (`src/services/embeddings.py`, `row.chunk_text[:500]`), and the impl previews 200 of those. Candidate text therefore cannot come from the legacy result row (Codex round 1).
- **Outbound transport** (`src/services/transport_security.py`, #184/#185): `classify_embedding_url` (raw-string screen, then `httpx.URL`: scheme `http|https`, no userinfo, a host, a valid port, a loopback test that fails closed) and `check_embedding_url_policy` (`http` to a non-loopback host needs `EMBEDDING_ALLOW_PLAINTEXT`). `embedding_http_client(timeout)` is the only constructor (`trust_env=False`, `follow_redirects=False`, `verify=` the `EMBEDDING_CA_FILE` context or certifi). `log_embedding_transport` writes one INFO line (scheme, host, port, trust, override) and the `internal_transport_plaintext` event for a non-loopback `http` URL. The settings validator `_validate_embedding_transport` skips the URL policy under `MCP_SANDBOX_MODE`.
- **Lifespan** (`src/main.py`): the sandbox branch returns before any probe. Otherwise the guards run in sequence, and a fatal one logs CRITICAL and calls `sys.exit(1)`. `_warm_embedding_model` is fire-and-forget. `/health` never probes, is always HTTP 200, and reports counts and states only.
- **Concurrency** (`src/services/concurrency.py`): `TOOL_CLASSES` maps a tool name to a class. `semantic_search` is in `embedding` (ceiling 1, `CLASS_CONNECTIONS` 1), `CLASS_MAPPING_VERSION = 4`, and the default mode is `shadow`. Leases are held through telemetry.
- **Harness** (`scripts/search_eval/arms.py`): an arm is `(EvalQuery, depth) -> list[str]`, and factories are `module:factory` with `membership` and `requires_db`. `--arm-opt` is one dict shared by every arm in a run. The built-in `semantic` arm calls the service directly, with one session per query.

### Constraints

- The consumer is an agent. It acts on the order without a human looking. A page that is *claimed* reranked but is not, or a partly reranked one, is the expensive failure (CLAUDE.md, "Codex framing"). A slower call is the cheap one.
- A reranker outage must not take down the vault, and must not fail a search.
- No connection may be held across an outbound hop (the embedding rule, held by the checkout-peak test).
- Public repo: no host paths, every setting in README and `.env.example`.

## Goals / Non-Goals

**Goals:**

- One optional argument that reorders the page `semantic_search` (and `hybrid_search`) already fetched, by a cross-encoder, within a hard latency bound.
- Every response says whether it was reranked and, if not, why, using a closed set of codes.
- The same tool schema on every deployment, configured or not.
- Measurable: the production reranking replayable as a harness arm against the unreranked incumbent in one report.

**Non-Goals:**

- Reranking by default, or recommending a reranker in the README default setup.
- Reranking `keyword_search`, `find_related` or any non-search tool.
- A second retrieval, a deeper candidate fetch, or chunk-level re-expansion of a note.
- More than one wire format in v1, authentication headers, or a separate trust anchor.
- Running a reranker in-process (no model weights, no new Python dependency).

## Decisions

### D1. Settings and validation

| Setting | Type, default, range | Meaning |
| --- | --- | --- |
| `RERANK_URL` | str, empty | The **full** endpoint URL (e.g. `http://127.0.0.1:8081/v1/rerank`). Empty (or unset) means the feature is absent. |
| `RERANK_MODEL` | str, empty | Sent as `model` in the body. Empty sends `""`, which `llama-server` ignores because it serves one model. At most 200 characters, no control characters. |
| `RERANK_TIMEOUT_MS` | int, 1500, 100–10000 | One wall-clock deadline per rerank call, covering pool wait, connect, write and read. |
| `RERANK_ALLOW_PLAINTEXT` | bool, false | Admits `http` to a non-loopback host. |

- A model validator `_validate_rerank_transport` reuses `classify_embedding_url` and the policy rule with `setting="RERANK_URL"` and `allow_plaintext=self.rerank_allow_plaintext`. The ValueError names `RERANK_URL` and `RERANK_ALLOW_PLAINTEXT`, not the embedding settings. It never echoes userinfo. **A policy violation refuses boot**, exactly as `OLLAMA_URL` does: it is a configuration error, not an outage. Under `MCP_SANDBOX_MODE` the URL policy is skipped, like the embedding policy.
- **What is validated**: the raw string (no surrounding whitespace, no control characters), the scheme, no userinfo, a host, a valid port, and the plaintext rule. **What is not validated**: the address class. Loopback, private and link-local hosts are allowed and expected, because a reranker is internal infrastructure. DNS resolution is not pinned, and the path is not checked. This is the opposite of `import_from_url`'s SSRF policy (file-transfer.md), on purpose: that URL is caller-supplied, and this one is operator configuration with the trust of `OLLAMA_URL`. No caller input reaches the URL, a header, or the request's shape. The caller controls only the query text, and the documents are the caller's own notes.
- `RERANK_ALLOW_PLAINTEXT` is a separate flag, not a reuse of `EMBEDDING_ALLOW_PLAINTEXT`. The two hops are separate written decisions, and an operator who allowed a plaintext Ollama on the LAN has not thereby allowed a plaintext reranker elsewhere.
- **The trust anchor is shared** (owner decision 6): an `https` reranker is verified against the `EMBEDDING_CA_FILE` context when it is set, else certifi. One internal CA for the internal inference hops is the common case. A separate `RERANK_CA_FILE` is a later addition if someone needs it (L10). **Compatibility restriction** (Codex round 1, MINOR): the existing rule refuses `EMBEDDING_CA_FILE` whenever the active embedding URL is plain `http`. So the common pairing of a loopback-`http` Ollama with an `https` reranker under a private CA cannot share the file. That reranker is then verified against certifi's bundle only (the OS store is never consulted), so it needs a publicly trusted certificate, or it runs on plaintext loopback. This change does **not** relax the embedding rule.

### D2. Wire format: Jina-compatible `/v1/rerank`, one only

Request (`POST RERANK_URL`, `Content-Type: application/json`):

```json
{"model": "<RERANK_MODEL>", "query": "<query>", "documents": ["<doc 0>", "..."], "top_n": <n>}
```

The response must be a 2xx with a JSON object holding `results`: a list of exactly `n` objects, each with an integer `index` (not a bool) in `[0, n)` and a finite number `relevance_score` (int or float, not bool). The indices are a permutation of `0..n-1`. Extra keys are ignored. Order within `results` is not trusted: the scores are mapped back by `index`.

This is what `llama-server --reranking` serves, and vLLM, Infinity and Jina's API serve it too. TEI's native `/rerank` (`{"query", "texts"}` → `[{"index", "score"}]`) and Cohere v2 are not supported in v1 (L1). Picking one format means one validator and one fake server, and none of the format sniffing a malformed response could exploit.

### D3. The client: `rerank_http_client`, pooled and bounded

- `transport_security.rerank_http_client(timeout)` builds `httpx.AsyncClient(timeout=…, verify=<ctx>, follow_redirects=False, trust_env=False, limits=httpx.Limits(max_connections=2, max_keepalive_connections=2))`. It is the only constructor for that hop, and an AST guard test pins this beside the embedding factory's guard.
- `src/services/rerank.py` holds one client per process, created lazily on first use (or by the probe) and closed in the lifespan's shutdown `finally`, after the indexer stops and next to `close_provider_client`.
- **One deadline**: `asyncio.timeout(RERANK_TIMEOUT_MS / 1000)` wraps the whole exchange, pool wait included. The httpx per-phase timeouts are set to the same value as a backstop. When the deadline passes the code is `timeout`, and the in-flight request is cancelled.
- **The body is streamed and capped at 1 MiB** (`RERANK_MAX_RESPONSE_BYTES`, a module constant). Over the cap the code is `oversize_response`. 50 scores take under 5 KB, so this cap only matters for a misbehaving endpoint.
- **No retry.** A retry would double the bounded latency, and the fallback is correct anyway.
- `max_connections=2` bounds the work in flight on the reranker **whatever the concurrency mode**. In `shadow` (the default) nothing else stops a burst of `rerank=true` calls, and a small GPU should see at most two batches at once. `--workers 1` is part of the contract, so per-process is global. A call waiting for a pool connection is waiting inside its own deadline.
- `log_rerank_transport()` writes one INFO line at startup when `RERANK_URL` is set (scheme, host, port, trust `certifi | ca-file | n/a`, `plaintext_override`; no path, query or userinfo). A non-loopback `http` URL also emits `internal_transport_plaintext` with `reason=rerank outcome=override`.

### D4. Candidates and the scored unit (`semantic_search`)

- **Candidates are the deduped notes of the existing overfetch**: the same statement with `LIMIT max(5 × limit, 50)` chunks, re-sorted and deduped exactly as today, **before** truncation to `limit`, capped at the first **50 notes** (`RERANK_MAX_CANDIDATES`) in vector order. No statement is added and none is changed.
- **One shared candidate-fetch service** (Codex round 1): `embeddings.semantic_candidates(session, query, *, limit, folder, tags, frontmatter, user_id) -> SemanticCandidates`. It runs the embedding call, the three `SET LOCAL`s and the unchanged statement (built over `semantic_arm_rows` when `hybrid-search` has merged), records `embed_ms`, `db_ms` and `exact_fallback`, and does **not** call `record_results` (the impl does that after the final order, D10). Both `semantic_search_impl(rerank=true)` and the harness arm call it, so replay at depth 20 scores the same up-to-50 candidates as production, and a note at vector rank 23 can be promoted in both. The legacy `semantic_search` is unchanged in signature, statements and results.
- **Candidate text is a separate internal field** (`rerank_text`), taken from the projected `chunk_text` **before** the existing `[:500]` cut. It is bounded so that `title + "\n\n" + rerank_text` is at most 2,048 characters, cut at a code-point boundary. It exists only on `SemanticCandidates` rows and never on legacy results.
- **Property**: because `limit ≤ 50` and the candidates are the first `min(50, n)` deduped notes, every note on today's unreranked page is a candidate. Reranking can therefore only reorder that page and promote notes from vector ranks `limit+1…50`. It never drops a note the vector order would have shown, except by outranking it.
- **The unit scored is the note.** Each candidate is sent as one document: `title + "\n\n" + rerank_text`, from the note's best-distance chunk (the one the row previews), at most **2,048 characters in all** (`RERANK_DOC_CHARS`). A note's score is that document's score. Max-over-chunks was rejected because it needs the note's other chunks, which the overfetch may not contain, and fetching them would be the second retrieval this change rules out. Sending all ≤ 250 overfetched chunks would raise the cost five-fold for a note-level page.
- **A stale candidate** (`chunk = None`, #200) has no `rerank_text` and is sent as its **title alone**. The service withholds superseded text from every caller, and the reranker is a caller. It will usually sink, which is the honest outcome for a note whose vectors predate its content (L4).
- The query is sent as given (it is already bounded by `MAX_SEARCH_QUERY_CHARS`). A reranker that rejects it for length answers non-2xx, and the result is `http_error` → fallback.

### D5. Ordering and the page

- Reranked order: `rerank_score DESC`, then the candidate's unreranked position ASC (a deterministic, order-preserving tie-break). The page is the first `limit` rows of that order.
- Every other field on a row (similarity, stale, truncated, and for hybrid the fused score, ranks and `explain`) keeps its unreranked value. The reranker changes the order and adds `rerank_score`, nothing else.
- **All or nothing.** Either every candidate has a validated score and the page is in reranked order, or no score is used and the page equals the capped unreranked `rerank=true` baseline (the unreranked order rendered in the `rerank=true` encoding under the same cap, D9) in rows, order, fields and preview selection, with a `reranked: false (<code>)` header. There is no partial reranking: no "scored the first 30" and no mixing of scored and unscored rows.

### D6. `hybrid_search` (when it exists)

- Candidates: the first 50 rows of the fused list (the union can hold 100) in fused order, before truncation to `limit`. The same superset property holds.
- Document text: `title + "\n\n" + X`, cut to 2,048 characters. X is the note's semantic-arm chunk when the note is in the semantic arm and not stale. The semantic chunk is taken before any preview cut, and the whole document is bounded at 2,048 characters. Otherwise X is the excerpt's leading characters up to that bound (`left(notes_metadata.body_excerpt, 2048)`, the text the keyword vector was built from), read by **one extra statement inside the hybrid transaction and snapshot**: `SELECT file_path, left(body_excerpt, 2048) FROM notes_metadata WHERE file_path IN (…)` under the owner predicate (path, not id: the semantic arm's rows carry no note id, and `(user_id, file_path)` is unique), issued after the keyword snippet statements (after hybrid's D2 step 7) and only when such a candidate exists. A NULL excerpt (a pre-031 row) sends the title alone. This fetches text for candidates already chosen, it is not a retrieval, and it runs under the snapshot that chose them.
- A keyword-only hybrid call (embedding provider down) still reranks: the reranker is a separate hop. The header carries both markers: `arms: keyword-only (…)` and `reranked: …`.
- The harness arm for hybrid fails the run on either degradation.
- `hybrid-search` merged before implementation (task 0.1), so all of D6 is built. Re-checked against the merged code: the excerpt statement follows hybrid's step 7 (snippets), the D9 floor arithmetic starts from hybrid's pinned 400 / 340 / 75 / 75 bounds, and D12's `keyword_or_semantic` membership and `hybrid_k` passthrough (`ARM_OPTS`) exist as described.

### D7. Reason codes and validation

`reranked: false (<code>)`, one code per response, taken from a closed set and checked in this order:

| Code | When |
| --- | --- |
| `not_configured` | `RERANK_URL` is empty. |
| `probe_skipped` | `MCP_SANDBOX_MODE` with `RERANK_URL` set. |
| `probe_failed` | The startup probe did not pass (D8). |
| `no_candidates` | The search returned zero rows. No request is sent. |
| `timeout` | The deadline passed. |
| `transport_error` | Connect, TLS or a reset (an `httpx.TransportError` other than a timeout). |
| `http_error` | A non-2xx status, **3xx included** (redirects are not followed). |
| `oversize_response` | The body exceeded 1 MiB. |
| `malformed_response` | Not JSON, not an object, `results` missing or not a list, an element without an integer `index` or a numeric `relevance_score`. |
| `count_mismatch` | `len(results) != n`. |
| `bad_index` | An index that is duplicated, out of range, or a bool. |
| `non_finite_score` | Any score that is NaN or ±Inf. |

Probe diagnostic codes (log only, never rendered; a call still answers `probe_failed`): any exchange code above, `probe_wrong_order` (the irrelevant document scored higher in either order), and `probe_tie` (equal scores in either order).

- Each runtime failure logs one WARNING per code at most once per 60 s, with a count of the suppressed ones, the code and the exception class. It never logs the URL, the response text or note content. The suppression is a module-level map, so a down reranker cannot flood the log.
- `asyncio.CancelledError` propagates (a client disconnect, or `_tracked` cancellation), and no fallback is rendered for a cancelled call.
- Any other unexpected exception inside the rerank step is caught, logged once as above with the code `malformed_response`, and falls back. **The rerank step can never fail the search.**
- The response is never rendered and never logged. Only validated numbers leave `rerank.py`.

### D8. The startup probe, process state and `/health`

- **Placement**: in the lifespan after `log_embedding_transport()` and the fingerprint guards, before `_publish_first_root_snapshot()`, outside the sandbox branch. It needs no database.
- **Probe attempt**: two requests with the fixed query `How do I reset a forgotten account password?` and two documents: `To reset a forgotten password, open the sign-in page, choose "Forgot password" and follow the link sent to your email.` (relevant) and `Sourdough bread needs a starter, flour, water and salt, and a long cold fermentation.` (irrelevant). The first request sends them as (relevant, irrelevant), the second as (irrelevant, relevant). The attempt passes when both responses validate under D7 with `n = 2` and, with scores mapped by `index`, the relevant document scores strictly higher **in each**. This defeats a position-only scorer (Codex round 1), which favours index 0 and loses the second order. A failure carries one diagnostic code (D7). The startup attempt's deadline is **10 s** for both requests together (`RERANK_PROBE_TIMEOUT_S`), because the first request after a reranker cold start may load weights.
- **Outcome**: a pass sets the process state to `enabled` and logs one INFO line. Anything else (wrong order, a tie, any D7 code) sets it to `disabled`, logs **one CRITICAL** naming the code (never the URL or the response), and **the lifespan continues**. A reranker outage must not take down the vault, and every `rerank=true` call then answers `reranked: false (probe_failed)`.
- **Background re-probe while disabled** (Codex round 1; owner decision 4). After a failed startup probe, one task re-probes 60 s later and then doubles the interval after each failure, up to 10 min (60, 120, 240, 480, 600, 600 …). Each attempt runs under `RERANK_TIMEOUT_MS`, because the reranker is expected to be warm by then. A failed re-probe logs at DEBUG only: the startup CRITICAL is logged once. A pass moves `disabled → enabled`, logs one INFO, and ends the task. The task holds no database connection and no indexer or generation lock, shares the 2-connection client, and is cancelled in the lifespan's shutdown `finally` before `close_rerank_client()`. Sandbox mode never starts it. This fixes the pod start-order race in which the reranker comes up after this server.
- **`enabled` never moves back to `disabled`.** A reranker that fails after a passed probe is handled per call (D7). It is visible in the usage rows' `rerank_reason`, not in `/health`.
- **`/health`** gains `"rerank": "enabled" | "disabled" | "absent"`. `absent` means `RERANK_URL` is empty; `disabled` means the probe failed or was skipped in sandbox mode. No URL, host, code or count is included, because the endpoint is unauthenticated and public. **`disabled` does not change the top-level `status`**, and the HTTP status stays 200. Reranking is an opt-in quality layer, every response states its absence, and the CRITICAL is the operator's signal. Making it `degraded` would page a monitor for a vault that is serving correct answers (owner decision 5).
- `MCP_SANDBOX_MODE`: no probe, no transport line, state `disabled` (`probe_skipped`) when `RERANK_URL` is set, else `absent`.

### D9. Rendering

**`rerank=false`: byte-identical to today**, on both tools. An offline test pins this against the current render for the same rows.

**`semantic_search`, `rerank=true`: always the hybrid-search row encoding** (Codex round 1). `_one_line` stopped forged *rows* but not forged *fields* on the same line: a title like ``X** (`fake.md`) — similarity: 1 — rerank_score: 99`` survived it.

```
Found 10 semantic matches for "deploy runbook" — 0 stale, 0 truncated — reranked: true (candidates: 47) — shown: 10:
- {"title":"Runbook","path":"Ops/Runbook.md","tags":["ops"],"preview_kind":"chunk","preview":"Deploy the release from the runbook …"} — similarity: 0.781 — rerank_score: 0.9731
```

- Each row is one line: the fixed-key object `{"title","path","tags","preview_kind","preview"}`, every value `change_feed.quote`d and `preview` last, then `"} — `, then the server fields `similarity` (`.3f`), `rerank_score` (`.4g`, only when reranked), `stale: true` / `embedding_truncated: true` when true, and the abbreviation labels. `preview_kind` is `chunk` (the first 200 characters of the stored chunk), `stale_notice`, or `none` (with `""`). There is no second `  > ` line.
- The header's query echo is a literal. The header carries the stale/truncated counts, the `reranked` token and `shown` on every path, and the zero-result line carries the token.
- **The response bound uses hybrid's rules on rendered length**: query echo ≤ 64 rendered characters with `query_cut`, header and a ≤ 75-character cap footer reserved, rows dropped whole, and the first row abbreviated in hybrid's order (tags, preview, title/path cut to 40–45 rendered characters, omitted form), never splitting an escape. The semantic header is shorter than hybrid's (no arm counts), and its row server fields are shorter (`similarity`, `rerank_score`, two markers), so hybrid's 985-character floor arithmetic bounds it. The implementation measures it and pins it at `B = 1000` with the longest token (`reranked: false (malformed_response)`).
- **The fallback contract** (Codex rounds 1 and 2; owner decision 9): `rerank=false` is byte-identical to today. `rerank=true` **always** renders in this encoding, under the cap, on success and on every fallback. The **baseline** for a fallback is the page a `rerank=true` call renders from the unreranked order under the same cap. A fallback has that baseline's row identities, order, fields, values and preview selection, without `rerank_score`, plus `reranked: false (<code>)`. The legacy `rerank=false` page is not the baseline, because the cap can admit fewer rows in the escaped encoding (L14). Whenever the cap drops rows, the footer states the omitted count and `more: true`, and names `limit=`/`folder=` as the remedy (`semantic_search` has no cursor). Tests compare decoded rows against the capped baseline, including a multi-row case at `B = 1000` where a success (scores in unreranked order) and a timeout fallback show the same rows.
- Scores and codes are the only rerank-derived text, and both are server-formatted from validated values.

**`hybrid_search`, `rerank=true`:** the row object is unchanged and `preview` stays last inside it. `rerank_score` is appended **after the separator**, following hybrid's server fields (after `explain` and the markers, before the abbreviation labels). The header gains the `reranked` token after `arms:` on every return path. Hybrid's header worst case (≤ 400) and abbreviated-row structure (≤ 340) are recomputed with the longest rerank token (+38) and `rerank_score` (+≈ 25). With hybrid's bounds the floor sum would be about 1,048, so on hybrid `rerank=true` responses only, the title/path cut floor is 20 rendered characters (kept 20–25). On hybrid's measured figures this gives 431 + 358 + 25 + 25 + 150 + 5 = 994 ≤ 1,000. The implementer re-measures and pins it at `B = 1000`.

### D10. Telemetry and usage attribution

- `_tracked`'s logged-argument list gains `rerank` on both tools, so `params.rerank` records the argument.
- With `rerank=true`, the holder gets: `reranked` (bool, always), `rerank_reason` (the code, only when false), `rerank_ms` (int, the exchange time, present when a request was sent), and `rerank_candidates` (int, the candidate count, present when computed).
- `result_count` / `result_paths` are recorded **once, from the final order** by the impl, under the existing bound (10 paths, 2048 bytes, dropped from the end). For `semantic_search` with `rerank=true`, the service's own `record_results` is not the last write: the impl records after reranking, and a test asserts that the logged paths equal the rendered prefix.
- `reranked: false` is **not** an error marker. The call returned an honest, labelled, correct unreranked page, it stays minable, and `rerank_reason` is an analysable key. This follows hybrid-search D4.
- `embed_ms + db_ms + rerank_ms ≤ duration_ms`.
- The miner credits a reranked row like any search row. The query identity does not include `rerank`, so a reranked and an unreranked search of the same text fold into one query, and their judgments union (L8).

### D11. Concurrency and connection holding

- **The class is unchanged**: the class is per tool, not per argument. Both tools are in `embedding` (ceiling 1, one connection), and `CLASS_MAPPING_VERSION` stays at its value. The slot is held for the embedding call, the database work and the rerank exchange, so **the hard deadline bounds the extra hold at `RERANK_TIMEOUT_MS`** (1.5 s by default, at most 10 s). Under `queue`/`enforce` a burst of reranked calls queues semantic and hybrid calls behind one slot. That is the intended protection for a single GPU, and in `shadow` nothing waits (the `max_connections=2` pool is the bound there, D3).
- A separate `rerank` class was rejected because `TOOL_CLASSES` maps names, not arguments. Splitting by argument would change the controller's admission key, the counters and the readiness criteria for a feature that is off by default.
- **No connection during the rerank call.** The impl's `async with async_session()` block closes before the request is sent: the candidates and their texts are plain Python values by then. For `hybrid_search` the excerpt statement (D6) runs inside the transaction, and the request is sent after the session closes. The checkout-peak test gains a case with a fake reranker that sleeps 1 s and asserts zero checked-out connections during the sleep.
- `rate_limits` charges nothing extra: `rerank=true` is one tool call.

### D12. Harness arms

- `scripts/search_eval_arms/rerank.py`:
  - `make_semantic_arm` (membership `semantic`, `requires_db = True`) calls `embeddings.semantic_candidates` at `limit = min(depth, 50)` (the same up-to-50 candidates as production, D4), reranks through the same `rerank.rerank_candidates` the impl uses, and returns the first `limit` reranked paths.
  - `make_hybrid_arm` (membership **`keyword_or_semantic`**, with hybrid-search's per-constituent corpus-loss rule; Codex round 1) does the same over the hybrid service's untruncated fused candidates, reading `hybrid_k` from `ctx.options` (defaulting to `HYBRID_RRF_K`) through the hybrid change's `ARM_OPTS` passthrough. It exists only once `hybrid-search` has merged. Without it, importing the factory raises `UsageError` (exit 64) naming the missing service.
- **Each factory checks the reranker before the first answer**: it runs the D8 probe, and a failing probe raises `UsageError`. An arm whose answer for any query comes back `reranked: false` (any code), or keyword-only for hybrid, **raises, so the run fails** (exit 1, `failed` manifest). A degraded answer scored as the reranker is wrong evidence. Timeouts during replay are therefore visible as failed runs, and the harness doc says to raise `RERANK_TIMEOUT_MS` for replay if the reranker is slow (L11).
- Factories, not `--arm-opt rerank=true`. The option dict is run-global (hybrid-search L11), so an option would rerank the incumbent `semantic` arm as well, and the A/B would compare the candidate with itself.
- A rerank arm makes the same embedding-provider connection the arm it wraps makes (the incumbent `semantic` arm has always embedded each query), plus `RERANK_URL` through `rerank_http_client`; nothing else (Codex implementation review round 1, C1; L15). They call the services directly, never `_tracked`, and write nothing (the `pg_stat_user_tables` assertion).
- Eval-gate invocation: `ARMS="semantic scripts.search_eval_arms.rerank:make_semantic_arm"` (plus `hybrid scripts.search_eval_arms.rerank:make_hybrid_arm` when hybrid exists), `BASELINE=semantic`.

### D13. Docstrings and the tool schema

- Both tools gain `rerank: bool = False`. The docstring sentence (≤ 4,000 characters on the wire stays true) reads: "`rerank=true` asks the deployment's reranker to reorder the candidates this call already fetched. It can add up to the configured timeout. Its rows are one-line quoted objects followed by server fields. The header says `reranked: true` or `reranked: false (<reason>)`, and an unreranked page keeps the unreranked order." No "better", "best" or "recommended". The peers rule (feedback: no tool or mode ranked above another) applies to the argument too.
- The annotations are unchanged: read-only, idempotent, closed-world. The reranker is deployment infrastructure, like the embedding provider (vault-tools.md L5).
- `semantic_search` is one of the frozen 25. Its schema change is recorded as an **amendment entry** in `tests/fixtures/tools_list_schema_amendments.json`, which then names `get_recent`, `list_notes` and `semantic_search`. The amendment differs from the frozen entry only by the optional `rerank` boolean property (not in `required`). The frozen fixture is not edited. `hybrid_search`'s additions entry carries `rerank` from the start if both changes land in sequence, or gains it in the follow-up.
- `SERVER_INSTRUCTIONS` is unchanged. The argument is discoverable from the schema, and the lead's 512-character budget is spoken for.

### D14. The fake reranker fixture

`tests/fixtures/fake_reranker.py` is a tiny ASGI app served by uvicorn on `127.0.0.1:<ephemeral>` inside the test (loopback `http` passes the policy with no override). It has one mode per behaviour: `correct` (scores from a keyword overlap, deterministic), `inverted` (the probe pair reversed), `position` (score by index only, ignoring query and text; must fail the two-order probe), `tie`, `drip` (headers at once, then one body byte every 100 ms past the deadline; the test asserts a `timeout` fallback, the stream closed, pool occupancy never above 2 and back to 0, and a following fast request reranked), `late_start` (refuses connections for N seconds, then `correct`, for the re-probe), `sleep:<ms>`, `status:<code>`, `redirect` (302 to another loopback port, which must not be followed), `not_json`, `no_results`, `short` (n−1 results), `long` (n+1), `dup_index`, `oob_index`, `bool_index`, `nan`, `inf`, `string_score`, and `oversize` (a 2 MiB body). It records each request body, so the tests can assert the document texts, `top_n` and `model`. It is offline (no network), runs in the default `pytest tests` (not integration-guarded), and is reused by the harness arm tests.

## Risks / Trade-offs

- **[CPU latency]** A CPU reranker is ~6 s for 10 documents, so 50 candidates on CPU will time out at 1.5 s on every call. → Opt-in and per call. Every timeout is a visible `reranked: false (timeout)`, and the docs say plainly that a CPU reranker is not a supported configuration for interactive use. The harness measures the deployment's actual latency before anyone enables it.
- **[GPU availability]** A small shared GPU can be busy with Ollama's embedding model or another tenant. Its latency is unmeasured and can spike past the deadline under contention. → The hard deadline, the 2-connection pool, the per-call fallback, and `rerank_ms` / `rerank_reason` in the usage rows. The search-analytics data then shows the timeout rate. A VRAM-starved reranker fails the probe or times out. It does not hang a search.
- **[Slot hold under `enforce`]** Each reranked call holds the single `embedding` slot up to 1.5 s longer, so semantic/hybrid throughput under `queue`/`enforce` falls. → Shadow is the default, `make concurrency-report` gates promotion, and L7 records the evidence mix.
- **[A reranker that agrees with the probe but is wrong]** A model that is only slightly better than random passes a two-document probe. → The probe catches a broken or mis-wired endpoint (wrong model type, an embedding server at a rerank path, inverted scores), not a weak one. Quality is the harness's job, and the eval gate is mandatory.
- **[Reranker score manipulation by note content]** A note can phrase itself to score well, especially against an LLM-based reranker like Qwen3-Reranker. → The same exposure keyword and vector ranking already have within a tenant's own notes. Reranking only reorders the caller's own candidates, so it crosses no tenant boundary.
- **[Note text leaves the process]** Candidate texts go to the reranker. → It has the same trust as the embedding provider, with the same transport policy and a separate plaintext flag. The README states that `RERANK_URL` receives note text.
- **[`rerank=false` regression]** The refactor that exposes the untruncated candidate list changes `semantic_search`. → The same statements, the byte-identity test, and the projection-identity and recall suites run unmodified.

## Migration Plan

No migration and no re-derive. Deploy is merge → CI → image → Flux. To enable: deploy a reranker (DEPLOYMENT.md example), set `RERANK_URL` (and `RERANK_MODEL` if the server needs it) in `.env`, restart, and check `/health` for `rerank: enabled` and the INFO probe line. To disable: unset `RERANK_URL` and restart. Rollback to an image without the argument: a client still sending `rerank` is refused by the unknown-argument rule (#295) on the old image, which is the normal rollback cost of a new argument. Usage rows with rerank keys stay harmless.

## Owner decisions taken by default

1. **`rerank` is always in the schema.** With no `RERANK_URL`, `rerank=true` returns the unreranked page marked `reranked: false (not_configured)`. It is not refused. A refusal would make one agent prompt fail on every deployment without a reranker, so a portable client would have to probe per server before sending an optional quality hint. The 1.0 tool-contract promise is a stable schema. The marker keeps it honest, and the page returned is the correct unreranked answer in the `rerank=true` encoding (the capped baseline, D9).
2. **One wire format: Jina-compatible `/v1/rerank`** (`llama-server`, vLLM, Infinity). TEI native and Cohere v2 are limitations (L1).
3. **The scored unit is the note**: title plus best-distance chunk, cut to 2,048 characters. The candidates are the existing deduped overfetch, capped at 50 notes. There is no second retrieval.
4. **A failed probe disables reranking, and the lifespan still starts.** A background re-probe (60 s doubling to 10 min, `RERANK_TIMEOUT_MS` per attempt) re-enables it. `enabled` is never revoked. (Revised in Codex round 1.)
5. **`/health` reports `rerank` without affecting the top-level `status`**, and the HTTP status stays 200.
6. **The trust anchor is `EMBEDDING_CA_FILE`'s context** (else certifi). There is a separate `RERANK_ALLOW_PLAINTEXT` and no `RERANK_CA_FILE`.
7. **The concurrency class follows the tool (`embedding`)**, with no new class and no version bump. A 2-connection client pool bounds in-flight requests in every mode.
8. **`reranked: false` is not an error marker.** Usage rows record `rerank`, `reranked`, `rerank_reason`, `rerank_ms` and `rerank_candidates`.
9. **`rerank=true` responses always use hybrid-search's quoted-object row encoding**, and a fallback is the unreranked page in that encoding. `rerank=false` stays byte-identical. (Revised in Codex round 1.)
10. **Harness A/B uses dedicated factories**, not a run-global option, and a degraded answer fails the run.
11. **No auth header in v1.** It targets self-hosted servers.

## Open Questions

1. ~~Disabled until restart, or a background re-probe?~~ Resolved in Codex round 1: re-probe (D8).
2. Max: does a hosted reranker (Jina, Cohere) need to be supported soon? If so, `RERANK_API_KEY` (a bearer header, never logged) is a small follow-up. v1 has none.
3. Max: which GPU host and model the live end-to-end check (task 13.2) should use. The proposal assumes `bge-reranker-v2-m3` (Q8 GGUF) on the shared GPU. If none is available at ship time, the live check covers only `not_configured` and the fake-server paths, and enabling waits for a measured harness report.

## Codex spec review, round 1 (2026-10-30)

Verdict on de4a738: FAIL, 10 MAJOR and 2 MINOR. All twelve were accepted and folded in.

1. **MAJOR: a position-only scorer passed the probe.** → The probe now scores both document orders, mapped by `index`, and the relevant document must win in each. There is a `position` fake mode (D8, D14).
2. **MAJOR: disabled until restart.** → A background re-probe while disabled: 60 s doubling to 10 min, a `RERANK_TIMEOUT_MS` deadline per attempt, one INFO on `disabled → enabled`, one startup CRITICAL only, cancelled at shutdown, no connection or lock held, skipped in sandbox (D8, owner decision 4).
3. **MAJOR: the candidate row holds only 500 characters of the chunk.** → A separate internal `rerank_text` field taken before the `[:500]` cut, bounded so that title plus text ≤ 2,048. Legacy results are unchanged (D4).
4. **MAJOR: `_one_line` does not stop same-line field forgery.** → `rerank=true` rows use hybrid-search's quoted-object encoding, with `rerank_score` after the separator. There are same-line forgery scenarios and a rendered-length response bound (D9).
5. **MAJOR: the replay arm could not see candidates deeper than its depth.** → One shared `semantic_candidates` service for the tool and the arm, with a depth-20, rank-23 promotion test (D4, D12).
6. **MAJOR: hybrid rerank arm membership.** → `keyword_or_semantic`, with per-constituent corpus loss (D12, search-evaluation delta).
7. **MAJOR: hybrid `rerank_score` before the preview broke the fixed object.** → Appended after the separator. Hybrid's bounds are recomputed (D9).
8. **MAJOR: a fallback was not byte-identical to `rerank=false`.** → Contract chosen: `rerank=false` is byte-identical, and `rerank=true` always uses the new encoding. A fallback equals the unreranked page by identities, order, fields and preview selection, plus the marker (D9, spec). Superseded in round 2: the comparison baseline is the capped unreranked `rerank=true` page.
9. **MAJOR: no slow-drip test of the deadline and pool.** → A `drip` fake mode asserting the fallback, the stream closed, occupancy ≤ 2 and a following fast success (D14, spec scenario).
10. **MAJOR: the 27-tool routing delta contradicted the stated sequence.** → An explicit pre-implementation rebase task (0.1). The requirement text states both the 28-tool merged form and the 27-tool fallback.
11. **MINOR: probe wrong-order and tie had no code.** → `probe_wrong_order` and `probe_tie` diagnostic codes (D7).
12. **MINOR: `EMBEDDING_CA_FILE` cannot be shared with a plaintext embedding URL.** → Documented in D1 and L10. The embedding rule is not relaxed.

## Codex spec review, round 2 (2026-10-30)

Verdict on 898f306: 11 of the 12 round-1 fixes were VERIFIED. One REGRESSED (MAJOR). The rendered-length cap (one abbreviated row, later rows dropped whole) contradicted "a fallback equals the `rerank=false` page": at `B = 1000`, `limit=10` and large escaped rows, `rerank=true` shows one row where `rerank=false` shows ten. → The contract is redefined against the **capped `rerank=true` baseline** (D9). The cap applies identically to success and every fallback, and its footer discloses the omitted count with `more: true`. The `rerank=false` page stays the byte-identical legacy rendering. L14 records the bounded omission. Multi-row success and fallback tests at the 1,000-character cap are added (task 7.4).

## Codex spec review, round 3 (2026-10-30)

Verdict on 2d8ea06: the spec and task 7.4 close the capped-page case. **NOT_FIXED on stale text only**: three places still stated the old "fallback equals the `rerank=false` page" contract (D5, tasks 11.2 and 13.1). The docstring draft (D13) and owner decision 1 carried the same phrasing. All of them now name the capped unreranked `rerank=true` baseline, and 11.2 keeps the separate `rerank=false` byte-identity check explicitly. This commit closes it. Verified by the supervisor by `git grep -n "rerank=false"` over the change, with no round 4.

## Accepted limitations

- **L1** Only the Jina-compatible `/v1/rerank` format is supported. TEI's native `/rerank` and Cohere v2 answer `malformed_response` (or `http_error`) and therefore fail the probe.
- **L2** A note is judged on its best-distance chunk only. A note whose relevant passage sits in another chunk that the vector order did not prefer is not rescued by reranking.
- **L3** A candidate document is cut at 2,048 characters. With the default `CHUNK_SIZE` (512 tokens) this rarely binds, but a larger chunk size is truncated before scoring.
- **L4** A stale candidate in `semantic_search` is scored on its title alone and usually sinks until it is re-embedded. In `hybrid_search` it is scored on its current excerpt.
- **L5** The two-document probe detects a broken or mis-wired endpoint, not a weak model.
- **L6** A disabled reranker is re-enabled only by the next re-probe, which can be up to 10 minutes after it recovers. A reranker that fails *after* a passed probe shows up per call and in usage rows, not in `/health`.
- **L7** A `rerank=true` call holds the `embedding` slot up to `RERANK_TIMEOUT_MS` longer. Queue-mode readiness evidence gathered while reranking is in use mixes the two hold profiles, and the class mapping version does not separate them.
- **L8** The query identity in the miner does not include `rerank`, so the judgments of reranked and unreranked searches of the same text union. This is intended (relevance is a property of the note), but a reranked page's shown set influences which judgments exist.
- **L9** The `rerank=false` path of `semantic_search` keeps its raw title and path and its un-one-lined preview (hybrid-search L9, follow-up issue there). Only the `rerank=true` path uses the quoted-object encoding, so the same call renders in two formats depending on the argument.
- **L10** There is no separate trust anchor for the reranker. An `https` reranker under a different private CA than the embedding endpoint cannot be verified in v1. Nor can one under any private CA while the embedding URL is plain `http`, because `EMBEDDING_CA_FILE` is then refused (D1).
- **L11** The harness arms fail the run on any timeout. A slow reranker therefore needs a larger `RERANK_TIMEOUT_MS` for replay than in production, and that difference is a measurement caveat to state in the PR.
- **L12** No authentication header, so a reranker behind an auth proxy cannot be used in v1.
- **L14** Under `MAX_READ_RESPONSE_CHARS`, a `rerank=true` page (reranked or fallback) can show fewer rows than the `rerank=false` page of the same call, because the quoted encoding is longer. Rows are dropped whole from the end, and the cap footer always discloses the omitted count with `more: true`. It is never silent.
- **L13** The reranker sees the caller's note text. It has the embedding provider's trust and is documented, but not otherwise mitigated.
- **L15** The rerank harness arms embed each replayed query through the configured provider, exactly as the incumbent `semantic` arm does. A precomputed-query-vector seam that would let replay run with the provider blocked was declined as scope (Codex implementation review round 1, C1); the arms' only new outbound hop is `RERANK_URL`.

## Implementation notes

Choices the spec left open, one line each (recorded during implementation, 2026-10-30):

- Sequencing: `hybrid-search` (#354) had merged, so every task including the [hybrid] ones was done; task 13.4 does not apply.
- Each exchange passes `timeout=httpx.Timeout(deadline_s)` per request, so the httpx per-phase backstop equals that call's own deadline (the 10 s probe is not cut by a 1.5 s client default).
- The body is requested with `Accept-Encoding: identity` and read raw; a response with any other `Content-Encoding` is `malformed_response` (the 1 MiB cap bounds bytes held, never a decoder's output).
- A declared `Content-Length` over 1 MiB is `oversize_response` before any body byte is read; an unparseable one is `malformed_response`.
- The re-probe schedule is read as intervals (60, 120, 240, 480, 600, 600 … s between attempts), per D8's wording.
- Before any probe has run (no lifespan: a test, or the harness before its preflight), a configured reranker answers `probe_failed`.
- The process client is rebuilt when the running event loop changes (the server has one loop; harness and test processes run several in turn).
- The D6 excerpt statement selects by `file_path IN (…)` plus the owner predicate rather than by id: the semantic arm's rows carry no note id, and path is unique per owner.
- `hybrid_search(rerank=true)` computes snippets exactly as today over the 50-row candidate list (first `KEYWORD_SNIPPET_ROWS` keyword rows); a row promoted from beyond that cap previews its chunk, head or `none`. The fallback page's previews equal the `rerank=false` page's.
- The `rerank=true` semantic page does not repeat the legacy prose degradation paragraphs; the header's stale/truncated counts and the row markers carry them. Its zero-result line keeps "(embeddings may still be building)" and adds the counts, the token and `shown: 0`.
- The `rerank=true` cap footer on both tools is `Response cap: N omitted — more: true; narrow with limit= or folder=.` (≤ 69 characters); `hybrid_search(rerank=false)` keeps its own footer.
- Pinned worst cases at 1,000 (measured 181 / 230 / 427 / 360): semantic header ≤ 185 and abbreviated structure ≤ 235; hybrid `rerank=true` header ≤ 431 and structure ≤ 362, title/path cut 20–25 (431 + 362 + 25 + 25 + 75 + 75 + 5 = 998).
- `rerank_candidates` is recorded on every `rerank=true` row whose search ran (0 for an empty result); `rerank_ms` only when a request was sent.
- A provider input rejection on `rerank=true` returns the existing `argument_too_long` refusal unchanged, with no rerank token; its usage row records `reranked: false` and no reason, count or time, since nothing was searched (verifier V4).
- `rerank_ms` is recorded only when the request reached the wire, detected by httpcore's trace extension (`…send_request_headers.started`); a pool wait or connect failure before it records none (Codex C3).
- Harness: the probe runs in a new generic hook, an async `factory.preflight(options)` that the runner awaits once per distinct hook after resolving the replay URL and before reserving the generation, so no URL means no probe and a failed probe exits 64 with no generation directory (Codex C2).
- Harness: an empty answer (`no_candidates`) is an empty run, not a failure; every other `reranked: false` raises `UnrerankedAnswer`.
- `make_hybrid_arm`'s "hybrid service missing" `UsageError` path is kept but untested: the service ships with #354.
- `RERANK_MODEL`'s control-character rule lives in the `_validate_rerank_transport` model validator; its length bound is `Field(max_length=200)`.
- `check_embedding_url_policy` gained `allow_setting` and `what` parameters; the embedding message is byte-identical (a test pins its wording).
- The rerank WARNING carries `suppressed_since_last`, the number of records the previous window withheld for that code.

## Codex implementation review, round 1 (2026-10-30)

Verdict on 553253d: FAIL, 3 MAJOR. One was a spec-text defect, two were fixed.

1. **MAJOR (spec text): the rerank arms dial the embedding provider, which the search-evaluation delta said they must not.** The incumbent `semantic` arm has always embedded each query through the provider, so the sentence was wrong rather than the code. → The delta and D12 now say a rerank arm makes the connection the arm it wraps makes, plus `RERANK_URL`; the precomputed-vector seam is declined as scope (L15). The integration test's `get_embedding` stub stands in for that ordinary provider hop.
2. **MAJOR: factory preflights ran before `resolve_url`**, so a reachable reranker was probed with no replay URL. → Preflights now run after the URL is resolved and before the generation is reserved; a test asserts exit 64 with zero requests at the fake.
3. **MAJOR: `rerank_ms` was recorded for a pool timeout that sent nothing.** → `RerankFailure.sent` comes from httpcore's trace extension; `ms` is left unset when the request never reached the wire. Tested with three concurrent calls against two held connections, and with nothing listening.

The verifier's round (0 blocking, 6 minor) was folded into the same commits: preflight ordering (as 2), the backoff scenario's wording, the `gzip` / `cl_oversize` / `chunked_oversize` fake modes with an `Accept-Encoding: identity` assertion and an unparseable-`Content-Length` test, `reranked: false` on the provider-rejection row, a same-path other-tenant excerpt fixture, and the re-probe test passing at 60 s.
