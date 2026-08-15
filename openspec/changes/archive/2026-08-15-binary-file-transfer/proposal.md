## Why

The vault is Max's single source of truth, but an agent in a chat client cannot
get a binary *into* it or *out of* it. `write_file` needs the bytes as a
base64 tool argument, and no chat client (claude.ai web/desktop, ChatGPT
connectors) can hand a pasted screenshot's bytes to a tool — MCP has no
file-input primitive (SEP-2631 is a draft) and attachments live in the model's
context, not the connector's. Only filesystem agents (Claude Code, Cursor) can
use `write_file` today: 6 calls in 5 months. Symmetrically, `read_file` on a
PDF returns base64 the model cannot use, and there is no way to hand the human
a download. And there is no `delete_file` at all, so a mistaken binary write
is permanent from the agent's side.

The maintainer-track answer in MCP is out-of-band transfer with an opaque
handle: the tool mints a short-lived capability URL, the bytes move over plain
HTTP, and the agent confirms by handle. That works in every client because the
human's browser does the transfer, and it composes with the future
`files/authorizeUpload` shape if the spec lands.

## What Changes

- **New table `transfer_tokens`** — opaque random tokens (stored as SHA-256,
  like API keys) bound at mint time to a direction (`upload`/`download`), a
  canonical vault path **and the vault root in effect**, the minting identity
  (`key_id`/`oauth_token_id`/`user_id`), an expiry, a state machine
  (`pending → claimed → completed`), and — for overwrite uploads and
  downloads — a fingerprint of the target file as observed at mint, so a token
  can never replace or disclose a file other than the one it was minted for.
- **Tokens are redeemed from an `Authorization: Bearer` header, never a URL
  path.** Human-facing links carry the token in the URL *fragment*
  (`…/transfer/upload#<token>`), which browsers do not send, so no access log
  (Traefik, Uvicorn) ever contains a token; the pages are static and read the
  fragment client-side.
- **New tools (all validate paths with the existing traversal + dot-dir
  guards; readwrite required except `request_download`):**
  - `request_upload(path, overwrite=False, expires_in=600)` → `{upload_id,
    upload_url, expires_at, max_bytes}`. The URL is a human-openable page;
    shells use `curl -H "Authorization: Bearer <token>" -T file …/transfer/upload`.
  - `check_upload(upload_id)` → `pending | uploading | completed{size, sha256,
    mime, completed_at} | expired`, so the agent confirms *this* upload landed
    rather than inferring from `list_files`.
  - `request_download(path, expires_in=600)` → `{download_url, expires_at,
    size, mime}`; multi-use within TTL; each fetch logged.
  - `import_from_url(url, path, overwrite=False)` → server-side fetch into the
    vault with an SSRF guard (https-only by default; canonical URL parsing;
    forbidden names/ports/userinfo/zone-ids; every resolved address must be
    globally routable; connection pinned to the validated address with
    original `Host`/SNI; new client per hop, no proxies, no HTTP/2; ≤ 5
    manual redirects re-checked per hop; identity encoding only; one 30 s
    deadline; streamed size cap = `MAX_FILE_WRITE_BYTES`).
  - `delete_file(path, permanent=False)` — peer of `delete_note` for
    non-markdown files (refuses `.md`, directories, symlinks): soft-delete to
    `.trash/<ts>-<basename>` by default.
- **New HTTP routes under `/transfer/`** (app-level, bearer-capability
  authenticated, added to the Traefik router that carries `/mcp` so the OAuth
  chain does not intercept them; not intercepted by the API-key middleware):
  - `GET /transfer/upload`, `GET /transfer/download` — static self-contained
    pages (nonce CSP, no external assets) that read the token from the
    fragment, show the bound path/cap/expiry via `…/info`, and transfer with
    `fetch`.
  - `GET /transfer/upload/info`, `GET /transfer/download/info` — bound
    path, cap/size, MIME, expiry, after full token/identity/root validation.
  - `PUT /transfer/upload` (raw body only; multipart dropped so nothing is
    parsed before authentication) — atomically claim the token
    (`pending→claimed`) *before reading the body*, stream to a temp file in
    the target directory through descriptor-anchored (`dir_fd` +
    `O_NOFOLLOW`) operations with the byte cap enforced during streaming,
    publish by hard-link no-clobber or fingerprint-checked replace, mark
    `completed`, log `usage_logs` (`tool="upload_file"`), return
    `{path, size, sha256, mime}`; handled failures release the claim, a
    crash after publication leaves it unusable.
  - `GET|HEAD /transfer/download/file` — open the bound file `O_NOFOLLOW`,
    compare its fingerprint with the mint-time one (changed → 404), stream
    from that descriptor with `Content-Type`, `Content-Disposition:
    attachment` (RFC 5987), `nosniff`, `Cache-Control: private, no-store`,
    `Accept-Ranges: none`; log each fetch.
  - All `/transfer/*` routes are rate-limited (slowapi) and return one uniform
    404 for unknown/expired/consumed/claimed/dead-identity/wrong-root tokens.
- **New helper `src/services/vault_fs.py`** — descriptor-anchored open/temp/
  publish/soft-delete primitives (per-component `O_NOFOLLOW`, `O_EXCL` temp,
  `link`/`replace` with `dir_fd`), used by every transfer write and by
  `delete_file`. Adopting it for the existing `_atomic_write` is a recorded
  follow-up.
- **Config:** `TRANSFER_TOKEN_TTL_SECONDS` default 600 (per-call `expires_in`
  clamped to `[60, 3600]`), `IMPORT_ALLOW_HTTP` default false; a new
  `Settings.public_base_url` that is set only when `MCP_HOSTNAME` or `BASE_URL`
  was operator-supplied (the derived localhost fallback does not count) —
  mint tools refuse with an error naming both settings when it is unset.
- **Docs:** `get_vault_guide` primer gains a short "getting files in and out"
  section (`request_upload` → give the human the link → `check_upload`;
  embed with `![[path]]` via `edit_note`); `CLAUDE.md` gains a "File transfer"
  section and the Traefik label note.
- Out of scope (follow-ups): MCP Apps in-chat drop-zone (needs SDK 2.0 /
  extension support and a claude.ai custom-connector spike), URL-mode
  elicitation auto-open, server-side text extraction from PDFs, resumable
  uploads above `MAX_FILE_WRITE_BYTES`, HTTP `Range` on downloads, an
  `attach_to_note` convenience (agents use `edit_note(append="![[…]]")`),
  migrating `_atomic_write` to the anchored helper, tightening the proxy-trust
  CIDR behind the rate limiter, and removing the host-specific bind-mount line
  from the tracked `docker-compose.yml`.

## Capabilities

### New Capabilities

- `file-transfer`: capability-token upload/download endpoints, the five tools,
  token lifecycle, SSRF guard for URL import, and their logging.

### Modified Capabilities

- `file-access`: `delete_file` joins the file tools; the "Dot-dir exclusion and
  path-traversal safety" requirement is extended to name the transfer
  endpoints and `import_from_url` as subject to the same guards, applied at
  use time and with symlinks refused.
- `vault-guide`: primer content requirement gains the file-transfer paragraph.

## Impact

- New: `src/services/vault_fs.py` (anchored FS ops), `src/services/transfer.py`
  (token state machine, streaming writer, SSRF-guarded fetch),
  `src/transfer/routes.py` (FastAPI router), two static templates, alembic
  migration for `transfer_tokens`, tests (anchored-FS unit tests incl. symlink
  races; token lifecycle incl. a two-task claim barrier; SSRF matrix plus a
  real local server behind an injected policy; route tests incl. concurrency,
  log capture, uniform-404 matrix, XSS filenames; tool tests).
- Modified: `src/mcp_server/server.py` + `tools.py` (five tools), `src/main.py`
  (mount router), `src/config.py`, `src/models/db.py`, `docker-compose.yml`
  **and** `$(DEPLOY_DIR)/docker-compose.yml` (Traefik api-router rule gains
  `PathPrefix(\`/transfer\`)`), `src/mcp_server/vault_guide_primer.md`,
  `CLAUDE.md`, `README.md`.
- Security surface: one new unauthenticated-by-session route family on the
  public host, authorized solely by 256-bit, header-carried, single-use /
  short-TTL capability tokens bound to a pre-committed path, root, identity
  and file fingerprint; server-side outbound HTTP from a container adjacent
  to Postgres/Ollama/registry (SSRF guard is mandatory and tested).
  Adversarial-Codex trigger per `CLAUDE.md` (destructive-write path,
  auth/permissions, new call sites).
- Deploy: migration + compose label change; `make deploy` handles both.
  End-to-end via live tools (no browser UI gate): `request_upload` → `curl -H
  "Authorization: Bearer …" -T` → `check_upload` → `read_file` →
  `request_download` → `curl` → `import_from_url` → `delete_file`, plus the
  negative cases.
