## 0. Pre-implementation

- [ ] 0.1 Base check: the branch contains `tests/test_tei_provider.py` and merge 0227f73. Coordination issue #425 references this change.
- [ ] 0.2 Codex spec review of this proposal (read-only, `-m gpt-daybreak-blue-latest`, effort high). "Wrong" here means an index filled from the wrong model, a late pgvector error, or a changed Ollama/OpenAI classification. Fold findings in before coding.

## 1. Provider contract and validation (`src/services/embeddings.py`)

- [ ] 1.1 Extract `_post_with_retries` per design D5. Convert `OpenAIProvider._post` to it with no change to log text, decision order or exceptions; existing OpenAI tests pass unmodified.
- [ ] 1.2 Convert `TeiProvider._post` to the helper with no input-limit callable.
- [ ] 1.3 Add `TEI_SLICE_DEADLINE_SECONDS = 90.0`. Wrap each slice and `embed_one` in `asyncio.wait_for`. Convert the timeout to a `RuntimeError` with a non-empty message naming the deadline.
- [ ] 1.4 Validate every 200 per design D2, including a non-JSON body, non-list body, `bool`, non-finite values and an integer too large for a float.

## 2. Input-limit boundary

- [ ] 2.1 Delete `r"|less than \d+ tokens"` from `_INPUT_LIMIT_PHRASES`, `_TEI_INPUT_LIMIT_CODES`, the `ProviderInputTooLarge` branch in TEI, and update the TEI docstring.
- [ ] 2.2 Tests: `max_tokens must be less than 4096 tokens` (400) stays non-input-limit for the OpenAI and Ollama providers; `context_length_exceeded` still translates; a TEI 413 token message is one request and a provider failure.

## 3. Server identity (`/info`)

- [ ] 3.1 Implement `verify_tei_server()` and its module-level state per design D1, using `_tei_client()`, 5 s, one attempt, bounded and escaped reported values, CRITICAL only on a verdict change.
- [ ] 3.2 Gate `TeiProvider` calls on it, at most one check per 30 s. No `/embed` before verification.
- [ ] 3.3 Call it from the lifespan after `_check_fts_fingerprint`, only under `tei`, below the sandbox short-circuit; `sys.exit(1)` on a proven mismatch only.
- [ ] 3.4 Tests: every scenario of the two identity requirements, including a late start, late mismatch, recovery, the one-check-per-interval bound, and no `/info` under other providers or sandbox.

## 4. Test fixtures

- [ ] 4.1 Shared fixture answering `/info` and full-width vectors. Update `tests/test_tei_provider.py` (`[[float(len(text))]]` rows are one-dimensional and now fail validation) and any `/embed` user in `tests/test_perf_provider_batching.py`.
- [ ] 4.2 Replace `test_the_token_limit_is_translated_and_not_retried` per 2.2.
- [ ] 4.3 Tests for the deadline: a hung slice ends by 90 s under a fake clock or patched timeout; a many-slice healthy note completes.
- [ ] 4.4 Transport tests: `TEI_URL` rejection and inactive-URL scenarios, TEI transport line, panel TEI check through the factory (extend the existing sweep to `TEI_URL` if it does not already cover it).
- [ ] 4.5 `make test-integration` on the merged result; `python -m pytest tests`; `openspec validate native-tei-embeddings --strict`.

## 5. Docs

- [ ] 5.1 `server.json`: `EMBEDDING_PROVIDER` description and choices include `tei`, and `TEI_URL`, `TEI_EMBEDDING_MODEL`, `TEI_EMBED_BATCH_SIZE` entries.
- [ ] 5.2 `docs/architecture/rate-limits.md:297` and `docs/architecture/search.md:1175`: the oversized-input sentence states TEI truncates like Ollama.
- [ ] 5.3 README: remove or correct "Both paths produce the same vectors"; verify or remove "TEI 1.8 and later" (task 7).
- [ ] 5.4 README, `.env.example`, `docs/deployment-kubernetes.md`: `TEI_EMBEDDING_MODEL` must equal the server's `/info` `model_id` exactly; `TEI_EMBED_BATCH_SIZE` above the server limit refuses startup. `.env.example` at the TEI model key states that no startup check detects replaced weights.
- [ ] 5.5 `docs/architecture/indexing-and-embeddings.md`: the `/info` check, gate, 90 s deadline, validation, truncation limitation, and the "model is reported in the fingerprint only" sentence corrected.
- [ ] 5.6 `CLAUDE.md` Key decisions: one bullet for the rule in design D1.
- [ ] 5.7 At archive: `embedding-providers` Purpose line names TEI.

## 6. Gates

- [ ] 6.1 `openspec-verifier` against this change.
- [ ] 6.2 Adversarial Codex pass (embedding path is a mandatory trigger). Two rounds by default, findings triaged per the workflow file, declined ones listed.
- [ ] 6.3 Search eval row: this change does not alter the chunking or Ollama/OpenAI path. Record in the PR why no eval was attached, or attach one if the implementation touched shared search code.

## 7. Verification against a real TEI server

- [ ] 7.1 Run the CPU image `ghcr.io/huggingface/text-embeddings-inference` (pinned tag) with a small model, on loopback.
- [ ] 7.2 Confirm design TC1 and TC2: `/info` field names and types, `model_id` for a Hub id and a local path. Record the raw response in the PR.
- [ ] 7.3 Confirm TC3: an input above the model limit with `truncate: true` returns a vector. Confirm the same input with `truncate: false` is refused and record the message. If TC3 fails, stop and reopen D4.
- [ ] 7.4 Confirm TC4: a batch above `--max-client-batch-size` returns the expected status and body. Confirm TC6.
- [ ] 7.5 Confirm TC5 for the README claim.
- [ ] 7.6 Run the real provider end to end: startup verified, startup with a wrong `TEI_EMBEDDING_MODEL` exits non-zero, startup with TEI stopped serves and withholds embedding, then a late TEI start verifies.
- [ ] 7.7 End-to-end MCP exercise on a test deployment: `semantic_search`, `hybrid_search`, `find_related`, with the tools called named in the report.
