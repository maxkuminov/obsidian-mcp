## Context

`requirements.txt` pins `mcp[cli]==1.28.1`. The August 2026 audit (three
independent passes: SDK trial-upgrade, non-MCP dependency audit, and a
`pip-audit`/OSV sweep), plus a Codex review of the first draft of this
proposal, established:

- `mcp` 1.29.0 and 2.0.0 add `RequestBodyLimitMiddleware` inside the
  streamable-HTTP session manager with `DEFAULT_MAX_REQUEST_BODY_SIZE = 4 MiB`.
  It enforces on `Content-Length` *and* on accumulated chunks for bodies
  without one, buffering and replaying the full body. `src/main.py` routes
  every MCP request (canonical `/mcp/` and the bearer root fallback) through
  `mcp.session_manager.handle_request`, i.e. through the limiter. Empirically
  a ~3 MiB body returns 200 and a ~5 MiB body a bare `413 Request body too
  large` before the tool runs. `MAX_FILE_WRITE_BYTES` defaults to 25 MiB.
- 1.29.0 is otherwise a zero-code-change drop-in (335 passed / 5 skipped on an
  unpatched tree). 2.0.0 requires ~15 lines of import/ctor moves, adds `httpx2`
  alongside our `httpx 0.28` and a mandatory `opentelemetry-api`, and
  implements spec 2026-07-28 features we do not use.
- `pgvector` 0.5.0 drops its `numpy` install requirement **and** changes
  SQLAlchemy vector results from NumPy arrays to plain lists. `numpy` is
  imported directly at `src/services/embeddings.py:9` and
  `src/mcp_server/tools.py:719` and appears in the image only via pgvector.
- `pip-audit` sits in `requirements.txt` (production image), pulling
  `requests` → `urllib3` and forcing the `urllib3>=2.7.0` pin. `make audit`
  runs from the host.
- The only advisory in the tree is `pytest` 8.4.0 (PYSEC-2026-1845), dev-only.
- `Settings.model_config = {"env_file": ".env"}` with pydantic-settings' default
  `extra="forbid"` rejects the compose-only keys in the repo-root `.env`, so a
  single test file run fails at collection; the full suite passes only because
  of collection order.
- Note write tools: `create_note` refuses content > `MAX_NOTE_BYTES` (10 MiB);
  `edit_note` and `set_frontmatter` bound what they *read* (10 MiB) but not the
  content they *write* — a pre-existing gap that matters now that the
  transport gets a hard ceiling.

## Goals / Non-Goals

**Goals:**
- Land the low-risk refresh with the write-path interaction covered by tests
  that assert successful writes, not merely "not 413".
- Every write tool has a tool-level cap strictly below the transport limit, so
  every legitimate write is accepted or rejected *by the tool* with an
  actionable message; the transport limit only ever bounds pathological or
  unsupported payloads.
- Shrink the runtime image's dependency surface (drop pip-audit/requests/urllib3).
- Keep single-file `pytest` runs working from a checkout with a real `.env`,
  without weakening validation anywhere else.

**Non-Goals:**
- MCP SDK 2.0 migration (deferred until a 2026-spec feature is wanted; the
  binary-upload feature may be that trigger).
- `passlib` → `bcrypt` and `uvicorn` 0.42 → 0.52 (separate changes).
- Streaming uploads, raising `MAX_FILE_WRITE_BYTES`, or a `delete_file` tool.
- Any change to search ranking, chunking, or indexing.

## Decisions

1. **`mcp` 1.29.0 over 2.0.0.** No security driver exists for either. 1.29 is
   the last v1 minor, still security-maintained, and drop-in. 2.0 buys nothing
   used here today and doubles the HTTP stack in the image.

2. **Transport limit = `2 × MAX_FILE_WRITE_BYTES + 1 MiB`, derived, no second
   knob.** Exposed as `Settings.mcp_max_request_body_bytes` and passed to
   `FastMCP(..., max_request_body_size=...)`.
   - Base64 mode: exact base64 length is `4·⌈n/3⌉ ≤ 2n` for all n ≥ 1, so a
     write at the cap always fits with the envelope allowance to spare.
   - Text mode: JSON escaping can expand UTF-8. Clients that emit
     `ensure_ascii` escapes turn 3-byte BMP characters into 6-byte `\uXXXX`
     (2×); astral characters become 12-byte surrogate pairs for 4 bytes (3×)
     and control characters 6 bytes for 1 (6×). The 2× factor covers realistic
     text (prose in any script); an emoji- or control-character-dominated 25 MiB
     text-mode payload is an **accepted limitation** documented in the tool
     docstring and `CLAUDE.md`, and fails with HTTP 413. Callers with such
     content use base64 mode, which is always safe.
   - The 1 MiB envelope allowance bounds JSON-RPC framing, `path`, and other
     arguments; it is explicit and tested (a body just over the limit → 413).
   *Alternatives:* derive from the 6× worst case (150 MiB bodies; memory cost
   without a real use case); leave the SDK default and document "keep uploads
   under 3 MB" (silently regresses a documented 25 MiB cap). Both rejected.

3. **Tool-level caps below the transport limit for every write.** `edit_note`
   and `set_frontmatter` check the *resulting* content against `MAX_NOTE_BYTES`
   before writing (and report it under `dry_run`), matching `create_note`. With
   defaults: notes ≤ 10 MiB, files ≤ 25 MiB, transport 51 MiB — the transport
   is never the tighter bound for a supported call. This is the fix Codex asked
   for so that "failures remain actionable"; it is a spec-level addition to
   `vault-write`.

4. **Compatibility break stated, not hidden.** 1.28.1 accepted unbounded
   bodies. After this change bodies > 51 MiB (defaults) get a bare 413. No
   supported call can produce one (D3), so this is recorded as a deliberate
   resource-safety bound, tested, and documented — not as "no behaviour change".

5. **Memory budget.** The SDK holds a `bytearray` plus a replayed `bytes` copy
   of the body; JSON parsing adds the argument string; base64 decoding adds the
   25 MiB payload. Peak ≈ 4 × body ≈ 200 MiB per max-size request. Container
   limit is 2 GiB (`mem_limit`/`deploy.resources` in compose). Accepted budget:
   ≥ 4 concurrent worst-case writes with headroom for the indexer; this is a
   single-operator deployment whose observed `write_file` volume is single
   digits per month. No app-level concurrency control added; revisit if the
   upload feature changes the traffic shape.

6. **`numpy` declared explicitly, pinned to the version in the running image**
   (`numpy==2.5.1` per `pip freeze` in `obsidian-mcp` on 2026-08-15), so the
   pgvector bump does not change the numpy the code runs against.

7. **pgvector 0.5.0 result-shape change is verified against a real database.**
   `np.dot`/`np.linalg.norm` accept lists, and the code paths at
   `tools.py:719` and `embeddings.py` are exercised by an **opt-in integration
   test** (`TEST_DATABASE_URL`; skipped when unset) run by the implementer
   against a throwaway `pgvector/pgvector:pg16` container: insert known
   vectors, assert similarity values, ordering, and per-note dedupe for the
   `semantic_search` and `find_related` query paths. Post-deploy, the same
   queries are issued through the live tools and compared with results
   captured *before* deploy.

8. **Filtered dotenv source instead of model-level `extra="ignore"`.** Override
   `settings_customise_sources` to wrap the dotenv source and drop keys that are
   not model fields (case-insensitive, respecting `env_prefix`), keeping
   `extra="forbid"` on the model. Environment variables are unaffected either
   way (pydantic-settings only applies `extra` to dotenv and init kwargs);
   `Settings(databse_url=...)` still raises. *Alternative:* `extra="ignore"` —
   simpler, but silently accepts misspelled constructor kwargs in tests.

9. **`pip-audit` moves to `requirements-dev.txt`; `urllib3` pin deleted.** The
   Trivy gate scans the built image; with pip-audit gone the package it pinned
   is gone too.

10. **All bumps land in one PR but are applied in order on the branch, running
    the suite after each step**, so a failure is attributable without a
    bisect: numpy → pip-audit move → dev bumps → mcp 1.29 + body limit + note
    caps → prod bumps.

## Risks / Trade-offs

- [Body-limit arithmetic wrong] → tests post a real JSON-RPC `tools/call
  write_file` (base64, decoded size = cap) with a readwrite identity and
  assert a *successful tool result and exact bytes on disk*; cap+1 → tool
  error and no file; body > derived limit → 413 on `/mcp/` **and** the bearer
  root fallback, with and without `Content-Length` (chunked ASGI body).
- [Text-mode pathological payload 413s] → accepted limitation, documented;
  base64 mode is the always-safe path.
- [Larger accepted bodies increase memory per request] → bounded and budgeted
  (D5); strictly tighter than 1.28.1's unbounded behaviour.
- [`edit_note`/`set_frontmatter` cap rejects an edit an operator wanted] →
  only for results > 10 MiB, which `read_note` already cannot read; error
  message names the cap.
- [pgvector 0.5 list results change ranking or break `np.dot`] → integration
  test + pre/post-deploy comparison of `semantic_search`, `find_related`, and
  `keyword_search` results for fixed queries.
- [`alembic` 1.19 CHECK-constraint autogenerate plugin invents a phantom diff]
  → `alembic check` once on the branch; decide explicitly if it flags anything.
- [`pydantic-settings` 2.15 `IncompleteFieldDefinitionWarning` from the SDK's
  settings model] → cosmetic; not blanket-ignored.
- [Filtered dotenv source drifts from pydantic-settings internals] → covered by
  two tests (extras ignored; constructor typo raises).

## Migration Plan

1. Branch, apply the ordered steps, full `pytest -q` green after each; run the
   opt-in integration test against a throwaway pgvector container.
2. Before deploy: capture live results for three fixed queries via
   `semantic_search`, `find_related`, `keyword_search`.
3. `make deploy` (build → Trivy → push → backup → migrate → recreate). No DB
   migration; `alembic check` is read-only confirmation.
4. Post-deploy: `make status`; re-issue the three queries and diff; exercise
   `write_file` (base64, ~100 KB, unique path under a scratch folder) →
   `read_file` → `list_files`; remove the file from the host vault path and
   confirm with `list_files`. Record which tools were called. `make logs` shows
   no `Request body too large`.
5. Rollback: redeploy the previous image tag; no data changes.

## Open Questions

- None blocking.
