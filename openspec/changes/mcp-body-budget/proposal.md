## Why

`/mcp` accepts request bodies up to `mcp_max_request_body_bytes`
(`max(2 × MAX_FILE_WRITE_BYTES, 6 × MAX_NOTE_BYTES) + 1 MiB`, 63,963,136 bytes
with the defaults) so that a supported 25 MB `write_file` reaches the tool.
That limit is **per request**. Nothing bounds the bytes that are in flight
across requests, and every near-limit body is copied several times before any
tool-level gate runs:

1. the SDK's `RequestBodyLimitMiddleware` accumulates a `bytearray` and copies
   it to `bytes`;
2. `json.loads` builds the decoded `str`;
3. `JSONRPCMessage.model_validate` and then FastMCP's argument model keep their
   own references and copies;
4. `_tracked`'s `named_params()` runs the `import_from_url` logging transform
   (`urlsplit(str(url))`) on the full argument, which produced the highest peak
   in the reproduction.

The ASVS reassessment (issue #322, V4.2.5 / V15.4.4, severity high) reproduced
this offline. One 60 MiB envelope took RSS from 57,096 KiB to 467,284 KiB, an
amplification of about 6.7×. Five synchronised requests can cross the 2 GiB
container limit and restart the single worker, and `--workers 1` is part of
the contract (rate limits and concurrency state are in-process). A restart
denies every tenant. The precondition is only a valid write-capable credential,
and a fresh principal's write burst is 15 calls, so the attack fits inside
normal authenticated limits.

The concurrency controller (#261/#188) does not cover this. It counts requests
rather than bytes, and it ships in `shadow`, where it observes and never
refuses. Its replay budget bounds only the bytes the transport watcher reads
while a request waits, not the bytes the app reads afterwards.

## What Changes

- **A process-wide, always-on MCP body-memory budget** (new
  `src/services/body_budget.py`). Every authenticated `/mcp` `POST` (the only
  method whose body the SDK buffers; GET streams and DELETE bypass) reserves
  its declared `Content-Length`, or the full per-request limit when
  the length is unknown (chunked), **before the app reads a single body byte**.
  It holds the reservation until the downstream ASGI call returns. The budget is
  a memory-safety bound, not a tuning knob. It ignores
  `MCP_CONCURRENCY_MODE`, has no shadow mode and no off switch.
- **The budget is derived, not guessed.** Memory budget =
  `MCP_BODY_MEMORY_BUDGET_BYTES` if set, otherwise
  `MCP_BODY_MEMORY_FRACTION` (0.5) × the container's cgroup memory limit, with a
  1 GiB fallback when no limit is readable. The raw-byte capacity is the memory
  budget ÷ `MCP_BODY_MEMORY_MULTIPLIER` (8, from the measured ~6.7×). On the
  2 GiB reference deployment that gives 128 MiB of raw body in flight: one
  maximum-size write plus all ordinary traffic. The cgroup limit is the
  process's own (its `/proc/self/cgroup` path and ancestors). Overrides only
  move in the safe direction: the multiplier's floor is 8, the fraction's
  ceiling 0.5, and an explicit budget above the safe allocation of a readable
  limit is refused. Web-application startup **refuses** a configuration whose
  large lane cannot hold one maximum-size body; importing the settings (the
  migration init container) never runs that check. Large
  supported writes therefore never become impossible, and the global body limit
  is unchanged.
- **Two lanes, so small requests are never starved.** Requests of at most
  1 MiB (the envelope allowance) draw from a reserved small lane (⅛ of
  capacity) and may borrow large-lane space only when no large request is
  waiting. Large requests use only the large lane, in strict FIFO order.
- **Bounded, disconnect-aware waiting, then refusal.** A request that does not
  fit waits FIFO in its lane for up to `MCP_BODY_BUDGET_WAIT_SECONDS` (15).
  The number of waiters is limited by `MCP_BODY_BUDGET_WAITERS` (8). A second
  `ReceiveWatch` keeps the wait disconnect-aware. Expiry or a full waiter queue
  returns a **transport HTTP 429** with the same JSON shape as the concurrency
  429 (`code: "body_memory"`). Like every transport 429, this is outside the
  in-band `MCP-REFUSAL` contract. A refused request consumes no rate token, no
  quota slot and no usage row.
- **Fast 413 before any wait** for a declared `Content-Length` above the
  per-request limit. A receive wrapper ends the body, as a disconnect to the
  app, if delivered bytes ever exceed the reservation (defence in depth).
- **Release on every exit:** normal completion, SDK validation failure (400 or
  413), a tool exception, client disconnect while waiting or while running,
  cancellation, and refusal. Releasing wakes the eligible waiters.
- **`import_from_url` URL cap.** `MAX_IMPORT_URL_CHARS` (8,192) applies
  through the existing declarative `arg_char_caps` pre-body screen, as
  `argument_too_long`. The `_url_host` logging transform no longer parses a
  value over the cap: it logs the fixed placeholder `<over-long>`. This removes
  the canonicalisation peak. It is **not** the fix: every other near-limit
  envelope still buffers, and the budget bounds them all.
- Telemetry: the budget reuses the `mcp_concurrency_pressure` security event
  with `reason="body:memory"` and outcomes `waited` or `refused`. It is kept
  out of the concurrency durable counters, so it cannot affect the
  shadow→queue→enforce readiness evidence. The resolved budget is logged once
  at startup.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `mcp-request-routing`: adds the body-memory budget requirements (admission
  point, derivation, lanes, wait and refusal, release, interaction with the
  concurrency modes).
- `file-transfer`: adds the `import_from_url` URL-length cap and its non-parsing
  logging transform.

## Impact

- Code: new `src/services/body_budget.py` (the derivation, the cgroup
  reader and the boot check); `src/config.py` (five settings with static
  ranges); `src/mcp_server/auth.py` (the
  reservation step in `APIKeyMiddleware.__call__` after authentication, a
  receive-counting wrapper); `src/mcp_server/tools.py` (`arg_char_caps` on
  `import_from_url_impl`, `_url_host` guard); `src/mcp_server/server.py`
  (docstring); `src/main.py` (startup log line).
- No migration and no schema change. No new dependency.
- Behaviour for ordinary traffic does not change: small requests never touch
  the large lane. A large request can now wait up to 15 s or receive a 429
  under concurrent large uploads, where before it could crash the process.
- Settings: `MCP_BODY_MEMORY_BUDGET_BYTES`, `MCP_BODY_MEMORY_FRACTION`,
  `MCP_BODY_MEMORY_MULTIPLIER`, `MCP_BODY_BUDGET_WAIT_SECONDS`,
  `MCP_BODY_BUDGET_WAITERS` in `.env.example`.
- Docs: `docs/architecture/rate-limits.md` (new section and accepted
  limitations), `docs/architecture/vault-tools.md` (size caps: the fourth
  cap), `docs/architecture/file-transfer.md` (the URL cap),
  `docs/architecture/security-event-logging.md` (the new `reason` value),
  `CLAUDE.md` key decisions, `.env.example`.
- Out of scope: `/transfer/upload` streams to disk and is already bounded by
  `TRANSFER_MAX_CONCURRENT_UPLOADS`. Sandbox mode bypasses `APIKeyMiddleware`
  entirely and stays exempt.

Refs #322
