## Context

`requirements.txt` pins `mcp[cli]==1.28.1`. The August 2026 audit (three
independent passes: SDK trial-upgrade, non-MCP dependency audit, and a
`pip-audit`/OSV sweep) established:

- `mcp` 1.29.0 and 2.0.0 both add `RequestBodyLimitMiddleware` inside the
  streamable-HTTP session manager with `DEFAULT_MAX_REQUEST_BODY_SIZE = 4 MiB`.
  `src/main.py` routes every MCP request through `mcp.session_manager.handle_request`,
  which delegates to the SDK's ASGI app — i.e. through the limiter. Empirically
  a ~3 MiB body returns 200 and a ~5 MiB body returns a bare `413 Request body
  too large` before the tool runs. `MAX_FILE_WRITE_BYTES` defaults to 25 MB, so
  the transport becomes the tighter, invisible cap on `write_file`.
- 1.29.0 is otherwise a zero-code-change drop-in for this repo (335 passed / 5
  skipped on an unpatched tree). 2.0.0 requires ~15 lines of import/ctor moves,
  adds `httpx2` alongside our `httpx 0.28` and a mandatory `opentelemetry-api`,
  and implements spec 2026-07-28 features we do not use.
- `pgvector` 0.5.0 drops its `numpy` install requirement. `numpy` is imported
  directly at `src/services/embeddings.py:9` and `src/mcp_server/tools.py:719`
  and appears in the image only via pgvector; removing it makes `import
  src.main` fail.
- `pip-audit` sits in `requirements.txt` (production image), pulling
  `requests` → `urllib3` and forcing the `urllib3>=2.7.0` pin. `make audit`
  runs from the host.
- The only advisory in the tree is `pytest` 8.4.0 (PYSEC-2026-1845), dev-only.
- `Settings.model_config = {"env_file": ".env"}` with pydantic-settings' default
  `extra="forbid"` rejects the compose-only keys in the repo-root `.env`, so a
  single test file run fails at collection; the full suite passes only because
  of collection order.

## Goals / Non-Goals

**Goals:**
- Land the low-risk refresh with the write-path interaction covered by a test.
- Make the write cap (`MAX_FILE_WRITE_BYTES`) the *only* limit a `write_file`
  caller can hit; the transport limit is derived from it and always looser.
- Shrink the runtime image's dependency surface (drop pip-audit/requests/urllib3).
- Keep single-file `pytest` runs working from a checkout that has a real `.env`.

**Non-Goals:**
- MCP SDK 2.0 migration (deferred until a 2026-spec feature is wanted; the
  binary-upload feature may be that trigger).
- `passlib` → `bcrypt` (auth code — separate change with its own Codex pass).
- `uvicorn` 0.42 → 0.52 (proxy-header / exit-code semantics — separate change
  so any regression is unambiguously attributable).
- Any behaviour change in tools, search, or indexing.

## Decisions

1. **`mcp` 1.29.0 over 2.0.0.** No security driver exists for either. 1.29 is
   the last v1 minor, still security-maintained, and drop-in. 2.0 buys nothing
   used here today and doubles the HTTP stack in the image. Revisit with the
   upload feature.

2. **Derive the transport limit from `MAX_FILE_WRITE_BYTES`; do not add a
   second operator knob.** A new derived value
   `mcp_max_request_body_bytes = ceil(max_file_write_bytes × 4/3) + 1 MiB`
   (base64 inflation plus JSON-RPC envelope and other tool arguments) is passed
   as `FastMCP(..., max_request_body_size=...)`. One knob keeps the invariant
   "write cap ≤ transport cap" true by construction; a separate env var would
   let an operator re-create today's bug. Exposed as a computed property on
   `Settings` so tests and the tool docstring can reference it.
   *Alternative considered:* leave the SDK default and document "keep uploads
   under 3 MB" — rejected, it silently regresses a documented 25 MB cap and the
   failure mode is a transport 413 with no JSON-RPC error for the agent.

3. **`numpy` declared explicitly, pinned to the version currently resolved in
   the image** (whatever `pip freeze` in the running container reports), so the
   pgvector bump does not change the numpy the code runs against.

4. **`pip-audit` moves to `requirements-dev.txt`; `urllib3` pin deleted.** The
   Trivy gate scans the built image; with pip-audit gone the package it pinned
   is gone too. If a future dependency reintroduces urllib3, Trivy will say so.

5. **`extra="ignore"` on `Settings.model_config`.** Only affects the dotenv
   source; environment variables were never subject to `extra_forbidden`, so the
   container is unaffected. Trade-off: a mistyped key in `.env` is silently
   ignored — same as it already is when set via compose `environment:`.
   *Alternative:* strip the compose-only keys out of `.env` into a separate
   file — rejected, it changes the documented single-`.env` deployment story.

6. **All bumps land in one PR but are applied in the order below on the branch,
   running the suite after each step**, so a failure is attributable without a
   bisect: numpy → pip-audit move → dev bumps → mcp 1.29 + body limit → prod
   bumps.

## Risks / Trade-offs

- [Body-limit arithmetic wrong → large `write_file` still 413s] → the
  regression test posts a real JSON-RPC `tools/call` body sized just under and
  just over the derived limit through the mounted app and asserts 200 / 413
  respectively; a second test asserts a `write_file` whose decoded payload
  equals `MAX_FILE_WRITE_BYTES` is *not* rejected at the transport.
- [Larger accepted bodies increase memory per request] → 1.28.1 had no limit
  at all, so this is strictly tighter than today.
- [`alembic` 1.19 CHECK-constraint autogenerate plugin invents a phantom diff]
  → run `alembic check` once on the branch against a migrated DB; if it flags
  something, decide explicitly rather than autogenerating.
- [`pydantic-settings` 2.15 emits `IncompleteFieldDefinitionWarning` from the
  SDK's own settings model] → cosmetic; not ours to fix. Do not blanket-ignore
  warnings in `pytest.ini`.
- [numpy version drift when pgvector stops pulling it] → pin explicitly (D3).
- [`extra="ignore"` hides `.env` typos] → accepted; documented in the setting's
  comment.

## Migration Plan

1. Branch, apply the six ordered steps, full `pytest -q` green.
2. `make deploy` (build → Trivy → push → backup → migrate → recreate). No DB
   migration in this change; `alembic check` is a read-only confirmation.
3. Post-deploy: `make status`; exercise `write_file` end-to-end against the live
   server (call it, then `read_file`/`list_files` to confirm) and note in the
   report which tools were called. Confirm the container log shows no
   `RequestBodyLimit` 413s during the check.
4. Rollback: redeploy the previous image tag; no data changes.

## Open Questions

- None blocking. Whether to also raise `MAX_FILE_WRITE_BYTES` or add streaming
  uploads is deferred to the binary-access feature.
