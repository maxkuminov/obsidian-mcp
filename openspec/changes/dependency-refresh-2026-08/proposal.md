## Why

An August 2026 audit of every pin found no exploitable CVE in the production
image but several latent breakages that will bite on the *next* routine bump:
the MCP SDK's newer releases add a 4 MiB transport body limit that silently
rejects large `write_file` calls, `pgvector` 0.5 stops shipping `numpy` (an
undeclared direct import of ours), and `pip-audit` — a host-side audit tool —
is baked into the runtime image, dragging in `urllib3` and a defensive pin
that exists only to appease Trivy. The one CVE present is dev-only (`pytest`
8.4.0). Doing the refresh deliberately, with the body-limit interaction
covered by a test, is cheaper than discovering it during an emergency bump.

The MCP SDK 2.0.0 line was evaluated and **not** adopted: it is a working
migration but implements spec features this server does not use, and pulls a
second HTTP stack (`httpx2`) plus OpenTelemetry into the image. `mcp` 1.29.0
is the last v1 minor and is a zero-code-change drop-in; v1 remains
security-maintained.

## What Changes

- Bump `mcp[cli]` 1.28.1 → 1.29.0.
- **New requirement:** the streamable-HTTP transport's request body limit is
  derived from `MAX_FILE_WRITE_BYTES` (base64 inflation + JSON envelope
  headroom) instead of the SDK's 4 MiB default, so a `write_file` payload
  that satisfies the write cap is never rejected at the transport with a bare
  HTTP 413 the agent cannot interpret.
- Declare `numpy` as a direct dependency (imported by `src/services/embeddings.py`
  and `src/mcp_server/tools.py`; today present only transitively via `pgvector`).
- Move `pip-audit` from `requirements.txt` to `requirements-dev.txt`; delete
  the `urllib3>=2.7.0` transitive pin (only reachable through pip-audit's
  `requests`). `make audit` runs from the host and is unaffected.
- Dev bumps: `pytest` 8.4.0 → 9.1.1 (fixes PYSEC-2026-1845), `pytest-asyncio`
  1.0.0 → 1.4.0, `respx` 0.22.0 → 0.23.1.
- Low-risk production bumps: `fastapi` 0.135.1 → 0.141.1, `sqlalchemy`
  2.0.48 → 2.0.52, `alembic` 1.18.4 → 1.19.1, `pgvector` 0.4.2 → 0.5.0,
  `pydantic-settings` 2.14.2 → 2.15.0, `python-multipart` 0.0.31 → 0.0.32,
  `slowapi` 0.1.9 → 0.1.10.
- `Settings` ignores unknown keys in the dotenv file (`extra="ignore"`): the
  repo-root `.env` legitimately carries compose-only keys
  (`VAULT_HOST_PATH`, `BACKUPS_HOST_PATH`), which today make single-file
  `pytest` runs fail at collection with `extra_forbidden`. Environment
  variables were never affected (pydantic-settings only forbids extras from
  the dotenv source), so container behaviour is unchanged.
- Out of scope, tracked separately: `passlib` → `bcrypt` (auth code, own
  change), `uvicorn` 0.42 → 0.52 (proxy-header and exit-code semantics, own
  change), MCP SDK 2.0.

## Capabilities

### New Capabilities

_None._

### Modified Capabilities

- `file-access`: adds a requirement that the MCP transport body limit is
  sized from `MAX_FILE_WRITE_BYTES` so the write cap is the *only* limit a
  `write_file` caller can hit; the transport limit must never be the tighter
  of the two.

## Impact

- `requirements.txt`, `requirements-dev.txt` — pins as above.
- `src/mcp_server/server.py` — `FastMCP(..., max_request_body_size=...)`.
- `src/config.py` — new derived setting/helper for the transport body limit;
  `model_config` gains `extra="ignore"`.
- `tests/` — regression test that a `write_file`-sized request body passes the
  transport (and one over the derived limit is rejected), plus a settings
  test for dotenv extras.
- Docker image: `pip-audit`, `requests`, `urllib3` leave the runtime image;
  `numpy` becomes explicit. No migration. `alembic check` run once after the
  1.19 bump to confirm no phantom autogenerate diff.
- Adversarial-Codex trigger per `CLAUDE.md`: this touches the write path
  (transport limit in front of `write_file`).
