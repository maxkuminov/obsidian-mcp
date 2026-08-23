## Context

`src/services/embeddings.py` couples the indexer and search path directly to Ollama: `get_embedding` POSTs to `{ollama_url}/api/embed`, `get_embeddings_batch` is a serial loop over that call, and `NoteEmbedding.embedding` is a fixed `Vector(1024)` matching bge-m3's output. The control-panel and `.env.example` only reference Ollama settings.

The current setup is fine for the maintainer — Ollama runs on a GPU host inside the home network, embedding 2,577 notes locally. But for public users:
- Most won't have GPU-backed Ollama; CPU bge-m3 is ~30× slower and embarrassing on first-run indexing.
- "Bring your own OpenAI key" is the lowest-friction path: zero local GPU, no model downloads, predictable cost (~$0.02 per million tokens for `text-embedding-3-small`).

Stakeholders: project maintainer (keeps Ollama), public users (want OpenAI), and operators of self-hosted instances who may want Azure OpenAI / OpenAI-compatible proxies (OpenRouter, Together, etc.).

## Goals / Non-Goals

**Goals:**
- A single `EMBEDDING_PROVIDER` env var picks between `ollama` and `openai`.
- OpenAI path uses native batch API for indexing throughput.
- OpenAI base URL is configurable to support Azure OpenAI and OpenAI-compatible proxies.
- Switching providers requires no code change — only env vars and a documented reset-embeddings step.
- The default behavior on existing deployments (no new env vars set) is byte-identical to today: Ollama + bge-m3 + 1024 dims.
- Validate provider config at startup so a missing `OPENAI_API_KEY` fails loudly, not on first index.

**Non-Goals:**
- Mixing providers within one deployment (per-note provider selection). A deployment picks one.
- Hot-swapping providers without reindex. Different models produce non-comparable vectors; we require a reset.
- Adding more providers (Cohere, Voyage, local sentence-transformers) in this change. The abstraction must permit it later, but only Ollama and OpenAI ship.
- A provider-agnostic dimension-reduction layer (PCA/Matryoshka). OpenAI's native `dimensions` param covers our needs.

## Decisions

### 1. Provider abstraction: thin protocol, not heavy class hierarchy

A `Protocol`-typed `EmbeddingProvider` with two methods:
```python
class EmbeddingProvider(Protocol):
    async def embed_one(self, text: str) -> list[float]: ...
    async def embed_batch(self, texts: list[str]) -> list[list[float]]: ...
```
Two implementations: `OllamaProvider` and `OpenAIProvider`, selected by a `get_provider()` factory that reads `settings.embedding_provider`. The factory caches a singleton per process.

**Why over alternatives:**
- Considered a full ABC + registry pattern — overkill for two providers. A protocol keeps the dispatch readable and avoids ceremony.
- Considered keeping `get_embedding` as a top-level function with `if/elif` branching — works, but bloats one file and makes per-provider testing harder.

`get_embedding(text)` and `get_embeddings_batch(texts)` keep their current signatures and become thin wrappers that call `get_provider().embed_one(...)` / `.embed_batch(...)`. Callers (`embed_note`, `semantic_search`, indexer) don't change.

### 2. OpenAI client: use `httpx` directly, not the `openai` SDK

The codebase already uses `httpx` everywhere. The OpenAI embeddings endpoint is one POST with a static request shape. Pulling in the full `openai` SDK adds ~5 transitive deps (pydantic v2 churn, distro detection, retries layer) for no gain.

**Why over alternatives:**
- `openai` SDK gives nicer typed errors and built-in retries. Worth it if we used chat/streaming/tools, but for `/v1/embeddings` it's a sledgehammer.
- We replicate retry-on-429 with `tenacity`-style logic in ~15 lines — already a known pattern in the codebase via `httpx`.

### 3. Batch behavior: per-provider native, with a uniform interface

- **Ollama**: keeps the current serial loop (its `/api/embed` accepts a list but in practice serial is fine for our ~2,500-note vault and avoids worker contention on the maintainer's GPU).
- **OpenAI**: single `POST /v1/embeddings` with the full `texts` list as `input`, up to a soft cap of 96 inputs per call (well under their 2048 limit; keeps individual request size sane and 429 retries cheap).

Indexer code stays the same (`get_embeddings_batch(chunks)`); the speedup for OpenAI users falls out for free.

### 4. Dimension handling: match config, require reset on change

`NoteEmbedding.embedding` becomes `Vector(settings.embedding_dimensions)` (read at model definition time from config). pgvector requires a fixed column dim — we cannot store mixed-dim vectors in one column without separate tables.

The migration adds a `Vector(N)` column with `N = settings.embedding_dimensions` at migration time, defaulting to 1024 to keep existing deployments untouched.

**For OpenAI users**: `text-embedding-3-small` natively supports a `dimensions` parameter; we pass `settings.embedding_dimensions` so it matches whatever column width is configured (default 1536 if they set provider=openai without overriding dim, but we recommend 1024 to match Ollama's column for users migrating).

**Switching providers post-deploy**:
1. Stop server.
2. Update `EMBEDDING_PROVIDER` and (optionally) `EMBEDDING_DIMENSIONS`.
3. Run `make reset-embeddings` (new make target → alembic op that drops & recreates the column with the new dim, then nulls all `embedded_content_hash` rows).
4. Start server. Indexer re-embeds everything on next pass.

The control-panel adds a "Reset embeddings" button that does the same thing while the server runs (locks the indexer, runs the SQL, releases — implementation detail in tasks).

**Why over alternatives:**
- Considered storing provider name + dim alongside each embedding row to enable mixed-state operation. Rejected: query path would need to filter by current provider, doubling complexity for a one-time switch.
- Considered Matryoshka-style truncation to a common dim (e.g., always 1024). Rejected: silently degrades quality and confuses users who expect their configured dim to be used.

### 5. Config validation at startup

`src/config.py` adds a `model_validator` that, when `embedding_provider == "openai"`, requires `openai_api_key` to be set. Failure raises at import time so the container won't even reach the indexer.

For Ollama, we keep the current laissez-faire approach — Ollama failures surface on first embed call, which is fine because the URL is set by default.

### 6. Control-panel surface

Settings page adds a read-only "Embedding provider" row showing the active provider, model, dimensions, and (for OpenAI) the masked API key prefix. The existing settings page is read-only for env-driven values, so no new write path. Add the "Reset embeddings" action button under a "Danger zone" group with a confirm modal.

## Risks / Trade-offs

- **[Cost surprise on first index]** A user pointing at a 10k-note vault with `text-embedding-3-large` could spend ~$1–2 on first index. → Mitigation: README documents expected cost per 1k notes for each model; default OpenAI model is `text-embedding-3-small` (cheapest non-deprecated option); control-panel shows a pre-index estimate based on `notes_metadata` row count × avg chunks.
- **[Network failures mid-batch]** OpenAI returns the entire batch's embeddings or none. A 5xx kills throughput. → Mitigation: retry with exponential backoff on 429/5xx (max 3); on persistent failure, log and skip the batch (matches current per-chunk failure handling). Indexer's hash-based change detection means the next pass picks up unembedded notes.
- **[Dim mismatch silently breaks search]** If an operator changes `EMBEDDING_DIMENSIONS` without running the reset migration, pgvector raises a dimension error on insert; indexer would log per-chunk failures and the vault gradually loses embedding coverage. → Mitigation: startup check compares the live column's dim (queried via `pg_attribute` / `information_schema`) against `settings.embedding_dimensions` and refuses to start if they differ, with a clear "run make reset-embeddings" error.
- **[Vendor lock-in on OpenAI]** Users who pick OpenAI become tied to their API. → Mitigation: `openai_base_url` setting works with Azure OpenAI, OpenRouter, and any other OpenAI-compatible endpoint, so the provider name is "openai-protocol" in spirit.
- **[Tests need a mock provider]** Existing tests likely hit a real Ollama. → Mitigation: introduce a `FakeProvider` returning deterministic vectors, used in tests via dependency injection on `get_provider()`.

## Migration Plan

1. Ship code with `embedding_provider` defaulting to `ollama`; existing deployments need no env-var changes and continue working.
2. Alembic migration: parameterize `note_embeddings.embedding` column to `Vector(:dim)` where `:dim` is read from `settings.embedding_dimensions` at migration runtime. For existing 1024-dim deployments this is a no-op (same dim). Provide a separate, opt-in migration `reset_embeddings` that drops & recreates the column at a new dim and nulls `embedded_content_hash`.
3. Document in README:
   - Quick-start with OpenAI (4 env vars).
   - Quick-start with Ollama (existing).
   - Cost expectations table.
   - Switching providers checklist.
4. Deploy via existing `make deploy`; no infra changes needed.

**Rollback:** revert the deployment image. Existing 1024-dim Ollama embeddings remain valid; no data loss. If a user had switched to OpenAI and rolled back, their OpenAI-generated vectors are still 1024-dim (because we recommend matching dims) and remain queryable by Ollama-embedded queries — search quality will degrade because the vectors come from different models, but nothing breaks.

## Open Questions

- Should `text-embedding-3-small` be the default OpenAI model, or `text-embedding-3-large`? Going with `-small` (cost) but happy to flip if the public-launch demo benefits from `-large`.
- Should the control-panel "Reset embeddings" action also trigger an immediate reindex, or just clear and let the 5-minute timer pick up? Leaning toward clear-only with a "Trigger reindex now" sibling button to keep concerns separate.
