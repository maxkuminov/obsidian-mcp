## 1. Prerequisites (each step: `pytest -q` green before the next)

- [ ] 1.1 Add `numpy==2.5.1` to `requirements.txt` (the version currently in the production image) with a comment naming the two direct import sites.
- [ ] 1.2 Move `pip-audit` from `requirements.txt` to `requirements-dev.txt`; delete the `urllib3>=2.7.0` pin and its comment from `requirements.txt`. Confirm `make audit` still runs (`Makefile:191`).
- [ ] 1.3 Dev bumps in `requirements-dev.txt`: `pytest==9.1.1`, `pytest-asyncio==1.4.0`, `respx==0.23.1`.

## 2. MCP SDK 1.29.0 + transport body limit

- [ ] 2.1 `requirements.txt`: `mcp[cli]==1.29.0`; update the CVE comment (fixed since 1.28.1; 1.29.0 adds the request-body limit we now size explicitly).
- [ ] 2.2 `src/config.py`: add a computed property `mcp_max_request_body_bytes` = `ceil(max_file_write_bytes * 4 / 3) + 1 MiB`, with a comment explaining the base64 + envelope headroom and the invariant (write cap ≤ transport cap). Set `model_config = {"env_file": ".env", "extra": "ignore"}` with a comment about compose-only keys.
- [ ] 2.3 `src/mcp_server/server.py`: pass `max_request_body_size=settings.mcp_max_request_body_bytes` to `FastMCP(...)`.
- [ ] 2.4 Update the `write_file` docstring / `CLAUDE.md` "Two kinds of size cap" section with one sentence: the transport limit is derived from `MAX_FILE_WRITE_BYTES` and is never the tighter cap.
- [ ] 2.5 Tests (new file `tests/test_transport_body_limit.py`, offline, through the mounted FastAPI app in sandbox/auth-bypass mode or with a test key as existing MCP e2e tests do — check `tests/` for the established pattern): (a) a `tools/call write_file` body whose base64 content decodes to exactly `MAX_FILE_WRITE_BYTES` is not 413'd by the transport; (b) a body larger than `settings.mcp_max_request_body_bytes` gets HTTP 413; (c) `mcp_max_request_body_bytes` tracks an overridden `MAX_FILE_WRITE_BYTES`.
- [ ] 2.6 Test for `Settings` dotenv extras: a temp `.env` containing `VAULT_HOST_PATH=/x` plus required keys loads without `extra_forbidden`.

## 3. Low-risk production bumps

- [ ] 3.1 `requirements.txt`: `fastapi==0.141.1`, `sqlalchemy[asyncio]==2.0.52`, `alembic==1.19.1`, `pgvector==0.5.0`, `pydantic-settings==2.15.0`, `python-multipart==0.0.32`, `slowapi==0.1.10`. Full suite green; `pip check` clean; `pip-audit -r requirements.txt` clean.
- [ ] 3.2 Confirm `import numpy` still resolves with pgvector 0.5.0 installed and numpy explicit (fresh venv from `requirements.txt` only).

## 4. Verification & ship

- [ ] 4.1 `openspec validate dependency-refresh-2026-08 --strict` passes.
- [ ] 4.2 `openspec-verifier` subagent audit against this change; adversarial Codex pass on the write-path change (transport limit ↔ `write_file` cap), framed per `CLAUDE.md`.
- [ ] 4.3 `make deploy`; `alembic check` against the live DB (read-only) to confirm 1.19 reports no phantom diff; `make status`.
- [ ] 4.4 End-to-end against the live server: `write_file` (base64 image, ~100 KB) → `read_file` → `list_files` → `delete_note`-equivalent cleanup via `write_file`-safe path; record which tools were called. Confirm no `Request body too large` in `make logs`.
- [ ] 4.5 Archive the change, open PR, merge, push.
