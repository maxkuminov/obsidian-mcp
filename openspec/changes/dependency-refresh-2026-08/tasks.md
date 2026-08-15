## 1. Prerequisites (each step: `pytest -q` green before the next)

- [ ] 1.1 Add `numpy==2.5.1` to `requirements.txt` (the version currently in the production image) with a comment naming the two direct import sites.
- [ ] 1.2 Move `pip-audit` from `requirements.txt` to `requirements-dev.txt`; delete the `urllib3>=2.7.0` pin and its comment from `requirements.txt`. Confirm `make audit` still runs (`Makefile:191`).
- [ ] 1.3 Dev bumps in `requirements-dev.txt`: `pytest==9.1.1`, `pytest-asyncio==1.4.0`, `respx==0.23.1`.

## 2. Settings: filtered dotenv source

- [ ] 2.1 `src/config.py`: keep `extra="forbid"`; override `settings_customise_sources` to wrap the dotenv source so keys that are not `Settings` fields (case-insensitive) are dropped before validation. Comment names the compose-only keys (`VAULT_HOST_PATH`, `BACKUPS_HOST_PATH`) as the reason.
- [ ] 2.2 Tests: (a) a temp `.env` containing `VAULT_HOST_PATH=/x` plus required keys loads; (b) `Settings(databse_url=..., _env_file=None)` still raises a validation error; (c) `pytest -q tests/test_config_validation.py` alone collects and passes from a checkout whose `.env` has the compose-only keys.

## 3. MCP SDK 1.29.0 + transport body limit + note write caps

- [ ] 3.1 `requirements.txt`: `mcp[cli]==1.29.0`; update the CVE comment (fixed since 1.28.1; 1.29.0 adds the request-body limit we now size explicitly).
- [ ] 3.2 `src/config.py`: computed property `mcp_max_request_body_bytes = 2 * max_file_write_bytes + 1024 * 1024`, with a comment giving the base64 (`4·⌈n/3⌉ ≤ 2n`) and text-mode (2× JSON-escape headroom; astral/control-char content is an accepted limitation) reasoning and the invariant "every tool cap < transport limit".
- [ ] 3.3 `src/mcp_server/server.py`: pass `max_request_body_size=settings.mcp_max_request_body_bytes` to `FastMCP(...)`.
- [ ] 3.4 `src/mcp_server/tools.py`: in `edit_note_impl` and `set_frontmatter_impl`, after computing the resulting content and before writing, refuse if `len(result.encode("utf-8")) > MAX_NOTE_BYTES` with the same message shape as `create_note` (`Content too large (N bytes, max MAX_NOTE_BYTES)`); under `edit_note(dry_run=True)` return the error instead of a diff.
- [ ] 3.5 Docs: `write_file` docstring gains one sentence on the transport limit and the text-mode limitation (base64 is always safe); `CLAUDE.md` "Two kinds of size cap" gains a short third bullet: transport limit = `2 × MAX_FILE_WRITE_BYTES + 1 MiB`, every tool cap sits below it, bare 413 only for unsupported payloads.
- [ ] 3.6 Tests, `tests/test_transport_body_limit.py`, offline, driving the mounted FastAPI app with a **readwrite** identity (reuse the pattern the existing MCP e2e tests use to authenticate — grep tests/ for how `/mcp/` is posted with an API key or how `current_permission` is set; do NOT rely on sandbox mode's read-only default) and a temp vault: (a) `tools/call write_file` base64 with decoded size == `MAX_FILE_WRITE_BYTES` (use a small overridden cap, e.g. 64 KiB, so the test is fast) → HTTP 200, success tool result, bytes on disk identical; (b) decoded size == cap+1 → HTTP 200, tool error naming the cap, no file; (c) body of `mcp_max_request_body_bytes + 1` bytes with `Content-Length` → 413 on `/mcp/`; (d) same on the bearer-authenticated root path; (e) same body streamed as multiple ASGI `http.request` chunks without `Content-Length` → 413; (f) `settings.mcp_max_request_body_bytes` tracks an overridden `MAX_FILE_WRITE_BYTES` (parametrize two values).
- [ ] 3.7 Tests for 3.4: `edit_note` (full-replace and append) and `set_frontmatter` producing > `MAX_NOTE_BYTES` → error, file unchanged (mtime/bytes); exactly `MAX_NOTE_BYTES` → success; `dry_run` over cap → error, no diff.

## 4. Low-risk production bumps

- [ ] 4.1 `requirements.txt`: `fastapi==0.141.1`, `sqlalchemy[asyncio]==2.0.52`, `alembic==1.19.1`, `pgvector==0.5.0`, `pydantic-settings==2.15.0`, `python-multipart==0.0.32`, `slowapi==0.1.10`. Full suite green; `pip check` clean; `pip-audit -r requirements.txt` clean.
- [ ] 4.2 Fresh venv from `requirements.txt` only: `import numpy`, `import src.main` succeed.
- [ ] 4.3 Opt-in real-Postgres integration test `tests/integration/test_pgvector_search.py`, gated on `TEST_DATABASE_URL` (skip when unset): start a throwaway `pgvector/pgvector:pg16` container (document the exact `docker run` in the test module docstring and in `requirements-dev.txt`/README), run alembic migrations against it, insert a handful of notes + known 1024-dim vectors, and assert for the `semantic_search` and `find_related` query paths: similarity values within tolerance, ordering, per-note dedupe after overfetch, and that results are usable as plain lists. Run it once with pgvector 0.5.0 and record the outcome in the PR.

## 5. Verification & ship

- [ ] 5.1 `openspec validate dependency-refresh-2026-08 --strict` passes.
- [ ] 5.2 `openspec-verifier` subagent audit against this change; adversarial Codex pass on the write path (transport limit ↔ `write_file` cap; `edit_note`/`set_frontmatter` caps), framed per `CLAUDE.md`. Iterate until no BLOCKER/MAJOR.
- [ ] 5.3 Pre-deploy: capture live results for three fixed queries via `semantic_search`, `find_related`, `keyword_search` (save to scratch).
- [ ] 5.4 `make deploy`; `alembic check` against the live DB (read-only) confirms 1.19 reports no phantom diff; `make status`.
- [ ] 5.5 Post-deploy end-to-end against the live server, recording which tools were called: re-issue the three queries and diff against 5.3; `write_file` (base64, ~100 KB PNG, unique path `_scratch/deps-refresh-<ts>.png`) → `read_file` → `list_files`; remove the file from the host vault path (no `delete_file` tool exists — note as follow-up) and confirm with `list_files`. `make logs` shows no `Request body too large`.
- [ ] 5.6 Archive the change (`openspec archive -y`), open PR closing #46, merge, push.
