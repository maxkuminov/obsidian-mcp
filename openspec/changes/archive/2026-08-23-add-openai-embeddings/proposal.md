## Why

The server currently embeds notes via a single hard-coded Ollama endpoint. As the project goes public, most users will not run a GPU-backed Ollama instance, and self-hosting embeddings has become the single steepest setup hurdle. Supporting OpenAI's embedding API alongside Ollama gives new users a zero-infrastructure path (BYO API key) while keeping the existing local-first option intact for users like the maintainer who already run Ollama.

## What Changes

- Introduce a pluggable embedding provider abstraction with two implementations: `ollama` (current behavior) and `openai`.
- Add `embedding_provider` setting (default `ollama` for backwards compatibility) plus OpenAI-specific settings (`openai_api_key`, `openai_base_url` for proxies/Azure-compatible endpoints, `openai_embedding_model`).
- Wire the provider selection through `get_embedding`, `get_embeddings_batch`, and `semantic_search` so the indexer and query path both honor the chosen provider.
- Use OpenAI's native batch endpoint (up to ~2048 inputs per call) when provider is `openai`, instead of the serial loop currently used for Ollama.
- Document the dimension-change workflow: switching providers (or models with different output dims) requires reindexing because `note_embeddings.embedding` is a fixed-width `Vector(N)` column. Provide an alembic migration template and a control-panel "reset embeddings" action.
- **BREAKING (operator-facing only):** the hardcoded `Vector(1024)` column type becomes `Vector(<embedding_dimensions>)`; existing deployments stay on 1024 by default but operators changing models must run the new reset migration.
- Update `README.md`, `.env.example`, and the control-panel settings page so the choice is discoverable.

## Capabilities

### New Capabilities
- `embedding-providers`: configurable embedding backend selection (ollama vs. openai), provider-specific request shape (single vs. batch), credential/config validation, and the dimension-mismatch reset workflow.

### Modified Capabilities
<!-- None: no existing capability spec covers embeddings; semantic_search behavior is unchanged from a contract perspective. -->

## Impact

- Code: `src/services/embeddings.py` (provider dispatch, OpenAI client), `src/config.py` (new settings), `src/models/db.py` (parameterize Vector dim), `src/services/indexer.py` (call new batch path), `src/control_panel/` (settings UI surface), `alembic/` (reset-embeddings migration).
- Dependencies: add `openai` Python SDK (or use existing `httpx` directly to avoid a heavy dep — decided in design.md).
- Config / ops: new env vars `EMBEDDING_PROVIDER`, `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `OPENAI_EMBEDDING_MODEL`. `.env.example` and deploy docs updated.
- Data: existing 1024-dim embeddings remain valid for the default Ollama+bge-m3 path. Switching to OpenAI requires reindex; documented as a one-time operator action.
- No public MCP tool surface changes — `semantic_search` behavior is unchanged from the client's perspective.
