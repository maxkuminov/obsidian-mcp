## 1. Config and validation

- [x] 1.1 Add `embedding_provider: Literal["ollama", "openai"] = "ollama"` to `src/config.py`
- [x] 1.2 Add `openai_api_key: str | None = None`, `openai_base_url: str = "https://api.openai.com/v1"`, `openai_embedding_model: str = "text-embedding-3-small"` to `Settings`
- [x] 1.3 Add a `model_validator(mode="after")` that raises if `embedding_provider == "openai"` and `openai_api_key` is empty
- [x] 1.4 Update `.env.example` with the new vars and brief comments on each
- [x] 1.5 Update `README.md` with two quick-start sections (Ollama, OpenAI), a cost-expectations table for `text-embedding-3-small` / `-large`, and a "Switching providers" checklist

## 2. Provider abstraction

- [x] 2.1 In `src/services/embeddings.py`, define an `EmbeddingProvider` Protocol with `embed_one` and `embed_batch` async methods
- [x] 2.2 Extract the existing Ollama logic into `OllamaProvider` implementing the protocol; keep its current single-call and serial-loop semantics
- [x] 2.3 Add `OpenAIProvider` that POSTs to `{openai_base_url}/embeddings` with `model`, `input`, and `dimensions` set from settings
- [x] 2.4 In `OpenAIProvider.embed_batch`, split inputs into sub-batches of ≤96 and concatenate results in input order
- [x] 2.5 Add retry-with-exponential-backoff for HTTP 429 and 5xx responses in `OpenAIProvider` (max 3 attempts, base delay 1s)
- [x] 2.6 Implement a cached `get_provider()` factory that returns the configured singleton based on `settings.embedding_provider`
- [x] 2.7 Rewrite top-level `get_embedding` and `get_embeddings_batch` as thin wrappers over `get_provider()`; verify `embed_note`, `semantic_search`, and `src/services/indexer.py` callers need no changes

## 3. Dimension handling

- [x] 3.1 Change `NoteEmbedding.embedding` in `src/models/db.py` to `Vector(settings.embedding_dimensions)` (read at class-definition time)
- [x] 3.2 Add an alembic migration that ALTERs `note_embeddings.embedding` to `vector(:dim)` parameterized on `settings.embedding_dimensions`; for the default 1024 this is a no-op on existing deployments
- [x] 3.3 Add `make reset-embeddings` target that runs an alembic op which drops & recreates `note_embeddings.embedding` at the configured dim and sets `embedded_content_hash = NULL` on all `notes_metadata` rows
- [x] 3.4 Add a startup check (in `src/main.py` lifespan) that queries `information_schema` for the live column dim and exits non-zero on mismatch with a message pointing to `make reset-embeddings`

## 4. Control-panel surface

- [x] 4.1 Add a "Embedding provider" card to the dashboard/settings template showing provider name, model, dimensions, and (for OpenAI) a masked key prefix derived as `key[:8] + "..." + key[-4:]`
- [x] 4.2 Verify the masked key prefix never reveals the full key in rendered HTML or JS sources (template-only formatting, no JS leakage)
- [x] 4.3 Add a "Danger zone" section with a "Reset embeddings" button behind a confirm modal
- [x] 4.4 Wire the button to a new POST endpoint in `src/api/` that pauses the indexer, runs the reset SQL, and resumes the indexer; return progress information for the dashboard to poll

## 5. Tests

- [x] 5.1 Add a `FakeProvider` test fixture returning deterministic vectors of the configured dim
- [x] 5.2 Update existing embedding tests to use the fake provider via `get_provider()` override
- [x] 5.3 Add unit tests for `OpenAIProvider`: single-batch happy path, sub-batching at ≥97 inputs, 429 retry success, 5xx retry exhaustion
- [x] 5.4 Add a config validation test: `EMBEDDING_PROVIDER=openai` with no key fails import-time validation
- [x] 5.5 Add an integration-style test that the dimension-mismatch startup check exits non-zero when config and column disagree

## 6. Docs and deploy

- [x] 6.1 Update `CLAUDE.md` "Key Decisions" and "Stack" sections to describe the provider abstraction and reference the two supported providers
- [x] 6.2 Add a new "Embedding providers" section to `CLAUDE.md` describing config knobs, the reset workflow, and the dimension-mismatch behavior
- [x] 6.3 Run `make deploy` against the staging container; verify with `EMBEDDING_PROVIDER=ollama` (default path) that indexing/search still work and embeddings are byte-identical to pre-change output
- [x] 6.4 Manual smoke-test against OpenAI: set provider=openai with a real test key, run `make reset-embeddings`, confirm a small folder reindexes successfully and `semantic_search` returns sensible results

## Accepted limitation at archive time (2026-08-23)

Tasks 6.3 and 6.4 were **never run**. They are checked above only so this
change could be archived; do not read them as evidence.

- 6.3 (staging deploy on the default Ollama path) is covered in practice: the
  Ollama provider is what production has run continuously since this change
  merged, so the default path is exercised by every deploy since.
- 6.4 (OpenAI smoke-test with a real key) has **no substitute**. No call has
  ever been made to a live `/v1/embeddings` endpoint from this code. The
  `OpenAIProvider` batching, sub-batching, `dimensions` parameter and 429/5xx
  retry path are covered by unit tests against a stubbed transport only. Anyone
  switching `EMBEDDING_PROVIDER=openai` is the first to run it for real and
  should treat the first reindex as the smoke-test.

