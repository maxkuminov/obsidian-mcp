## Why

An August 2026 audit of every pin found no exploitable CVE in the production
image but several latent breakages that will bite on the *next* routine bump:
the MCP SDK's newer releases add a 4 MiB transport body limit that silently
rejects large `write_file` calls, `pgvector` 0.5 stops shipping `numpy` (an
undeclared direct import of ours), and `pip-audit` — a host-side audit tool —
is baked into the runtime image, dragging in `urllib3` and a defensive pin
that exists only to appease Trivy. The one CVE present is dev-only (`pytest`
8.4.0). Doing the refresh deliberately, with the body-limit interaction
covered by tests, is cheaper than discovering it during an emergency bump.

The MCP SDK 2.0.0 line was evaluated and **not** adopted: it is a working
migration but implements spec features this server does not use, and pulls a
second HTTP stack (`httpx2`) plus OpenTelemetry into the image. `mcp` 1.29.0
is the last v1 minor and is a zero-code-change drop-in; v1 remains
security-maintained.

## What Changes

- Bump `mcp[cli]` 1.28.1 → 1.29.0.
- **New requirement (file-access):** the streamable-HTTP transport's request
  body limit is derived from `MAX_FILE_WRITE_BYTES` —
  `2 × MAX_FILE_WRITE_BYTES + 1 MiB` — instead of the SDK's 4 MiB default. This
  guarantees a base64-mode `write_file` at the cap always reaches the tool
  (base64 is 4·⌈n/3⌉ < 2n), and gives text-mode writes 2× headroom for JSON
  escaping (`ensure_ascii` clients expand non-ASCII UTF-8 up to 2×; astral
  and control characters expand further and are an accepted, documented
  limitation that surfaces as HTTP 413).
- **New requirement (vault-write):** every note write tool bounds the
  *resulting* note at `MAX_NOTE_BYTES` (10 MiB) with a tool-level error and no
  write. `create_note` already does; `edit_note` and `set_frontmatter` gain the
  same check, so every write path has a tool-level cap strictly below the
  transport limit and no legitimate write can be rejected only by the
  transport. (Notes above `MAX_NOTE_BYTES` are already unreadable by
  `read_note`, so nothing that could be read is lost.)
- **Explicit compatibility statement:** the transport limit applies to *every*
  MCP POST. Under 1.28.1 bodies were unbounded; after this change any body
  above ≈51 MiB (default caps) is rejected with a bare HTTP 413. The largest
  legitimate payload is a 25 MiB `write_file` (≈35 MB base64) — 5× the largest
  writable note — so no supported caller is affected; the bound is a
  resource-safety improvement.
- Declare `numpy` as a direct dependency (imported by `src/services/embeddings.py`
  and `src/mcp_server/tools.py`; today present only transitively via `pgvector`).
- Move `pip-audit` from `requirements.txt` to `requirements-dev.txt`; delete
  the `urllib3>=2.7.0` transitive pin (only reachable through pip-audit's
  `requests`). `make audit` runs from the host and is unaffected.
- Dev bumps: `pytest` 8.4.0 → 9.1.1 (fixes PYSEC-2026-1845), `pytest-asyncio`
  1.0.0 → 1.4.0, `respx` 0.22.0 → 0.23.1.
- Low-risk production bumps: `fastapi` 0.135.1 → 0.141.1, `sqlalchemy`
  2.0.48 → 2.0.52, `alembic` 1.18.4 → 1.19.1, `pgvector` 0.4.2 → 0.5.0
  (SQLAlchemy vector results become plain lists instead of NumPy arrays —
  covered by a real-Postgres integration test and post-deploy checks),
  `pydantic-settings` 2.14.2 → 2.15.0, `python-multipart` 0.0.31 → 0.0.32,
  `slowapi` 0.1.9 → 0.1.10.
- `Settings` ignores unknown keys **from the dotenv source only** (a filtered
  dotenv source; the model keeps `extra="forbid"` so misspelled constructor
  kwargs and env still fail loudly). The repo-root `.env` legitimately carries
  compose-only keys (`VAULT_HOST_PATH`, `BACKUPS_HOST_PATH`), which today make
  single-file `pytest` runs fail at collection. The container receives its
  config as process environment, which pydantic-settings never subjects to
  `extra` checks, so container behaviour is unchanged.
- Out of scope, tracked separately: `passlib` → `bcrypt` (auth code, own
  change), `uvicorn` 0.42 → 0.52 (proxy-header and exit-code semantics, own
  change), MCP SDK 2.0, a `delete_file` tool for non-markdown files (the
  binary-access feature).

## Capabilities

### New Capabilities

_None._

### Modified Capabilities

- `file-access`: adds the derived transport body limit requirement (a
  base64-mode `write_file` at the cap always passes the transport; oversized
  bodies — including chunked bodies without `Content-Length`, on both the
  canonical `/mcp/` path and the bearer root fallback — are bounded; the limit
  tracks `MAX_FILE_WRITE_BYTES`).
- `vault-write`: adds the requirement that `create_note`, `edit_note`, and
  `set_frontmatter` refuse to produce a note larger than `MAX_NOTE_BYTES`,
  with a tool-level error and no write.

## Impact

- `requirements.txt`, `requirements-dev.txt` — pins as above.
- `src/mcp_server/server.py` — `FastMCP(..., max_request_body_size=...)`.
- `src/config.py` — computed `mcp_max_request_body_bytes`; filtered dotenv
  source in `settings_customise_sources`.
- `src/mcp_server/tools.py` — `MAX_NOTE_BYTES` check on the resulting content
  in `edit_note_impl` and `set_frontmatter_impl` (before write, after the
  edit is computed; `dry_run` reports it too).
- `tests/` — transport-limit tests through the mounted app with a real
  readwrite identity asserting a *successful* max-size `write_file` and exact
  bytes on disk, cap+1 → tool error and no file, oversized/chunked → 413 on
  both routes; settings tests (dotenv extras ignored, constructor typo still
  raises); note-cap tests for `edit_note`/`set_frontmatter`; an opt-in
  real-Postgres integration test (`TEST_DATABASE_URL`, throwaway
  `pgvector/pgvector:pg16` container) asserting `semantic_search` /
  `find_related` similarity, ordering and dedupe on pgvector 0.5 rows.
- Docker image: `pip-audit`, `requests`, `urllib3` leave the runtime image;
  `numpy` becomes explicit. Memory: a max-size request costs ≈4× body
  (≈200 MiB) transiently; the container's 2 GiB limit accommodates several
  concurrent worst-case writes, which is far beyond this single-operator
  deployment's real concurrency — accepted and documented.
- No DB migration. `alembic check` run once after the 1.19 bump.
- Adversarial-Codex trigger per `CLAUDE.md`: touches the write path
  (transport limit; `edit_note`/`set_frontmatter` size check).
