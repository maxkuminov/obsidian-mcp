Refs #425 (the release PR from `feat-tei-embeddings` closes it with `Closes #425`). Credits the #424 author as co-author.

## Why

PR #424 added a native Hugging Face Text Embeddings Inference (TEI) provider to the `feat-tei-embeddings` integration branch without an OpenSpec change, so the specs still say only `ollama` and `openai` are valid. The provider also has gaps on the path this product treats as most expensive, silently wrong search:

- Nothing checks that the server behind `TEI_URL` serves `TEI_EMBEDDING_MODEL`. `/embed` has no model field, so the configured name is only a label in the fingerprint. A wrong server fills the index with vectors from another model.
- A 200 response is checked for row count only. A wrong-width row reaches pgvector as a late database error, and a non-JSON 200 raises an unrelated decode error.
- One slice can take about 183 s (three 60 s attempts plus backoff) while holding the index pass.
- The new shared phrase `less than \d+ tokens` changes classification for Ollama and OpenAI gateways: `max_tokens must be less than 4096 tokens` would tell an agent to shorten a query that was never the problem.

## What Changes

- **Spec the provider as built.** `EMBEDDING_PROVIDER` accepts `tei`, no API key, `TEI_URL` is the active URL for transport policy, and every requirement that names providers or clients is updated (`embedding-providers`, `internal-transport-security`, `index-integrity`, `search-quality`).
- **Request contract.** `POST /embed` with `{"inputs": [...], "truncate": true}`, slices of at most `TEI_EMBED_BATCH_SIZE`, exactly one vector per input in order, retry on 429/5xx and transport errors.
- **Server identity check.** `GET /info` at startup: `model_id` must equal `TEI_EMBEDDING_MODEL` and `TEI_EMBED_BATCH_SIZE` must not exceed `max_client_batch_size`. A mismatch refuses startup. An unreachable server does not, but **no `/embed` request is sent until a check has passed** (design D1).
- **Response validation.** Every row is a list of `EMBEDDING_DIMENSIONS` finite numbers. A malformed or non-JSON 200 is a provider failure.
- **Input-limit handling removed for TEI**, and the `less than \d+ tokens` phrase is deleted, which restores Ollama and OpenAI classification exactly.
- **90 s overall deadline per TEI slice**, retries included.
- **Accepted limitation (owner decision).** Over-limit chunks stay truncated with `truncate: true`, and such a note is still certified.
- **Refactor.** The retry loop shared with `OpenAIProvider` is extracted with OpenAI's behaviour unchanged.
- **Docs.** `server.json`, `docs/architecture/rate-limits.md`, `docs/architecture/search.md`, README (the "same vectors" and "TEI 1.8" claims), the indexing architecture note, `.env.example`.
- **Verification against a real TEI container.** The TEI server behaviour this change relies on has not been confirmed on a running server. See design "Claims to confirm".

Out of scope: `TEI_API_KEY` for servers started with `--api-key` (#426).

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `embedding-providers`: `tei` as a valid provider, its request, retry, deadline and validation contract, the `/info` check and embedding gate, the input-limit boundary, the truncation limitation.
- `internal-transport-security`: `TEI_URL` as the active URL, the TEI provider and panel check as factory clients, the TEI transport line.
- `index-integrity`: the many-chunk requirement states TEI's per-slice bound and that there is still no aggregate deadline.
- `search-quality`: the pre-warm's "local provider" wording becomes `ollama` only, because the code does not pre-warm TEI.

## Impact

- **Code:** `src/services/embeddings.py` (provider, gate, shared retry helper, phrase list), `src/main.py` (startup check after the fingerprint guards). No migration and no new setting.
- **Tests:** `tests/test_tei_provider.py` (vectors of `EMBEDDING_DIMENSIONS` width, `/info` mock; the token-limit test is inverted), `tests/test_perf_provider_batching.py` if it reaches `/embed`, new OpenAI and Ollama phrase tests.
- **Operators:** with `tei`, `TEI_EMBEDDING_MODEL` must equal the server's reported `model_id` exactly, including for a local path such as `/data/model`. `tei` has not been released, so no deployed configuration changes meaning.
- **No effect** on Ollama or OpenAI request behaviour.
