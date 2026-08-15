## Context

- The server is FastAPI + FastMCP (mcp 1.29, Streamable HTTP, `stateless_http`),
  behind Traefik on a public hostname. `/mcp`, `/health`, OAuth discovery/token
  routes sit on the `obsidian-mcp-api-rtr` router with **no** OAuth middleware
  (app-level auth); a `root-rtr` catches any request carrying `Authorization:
  Bearer` for the MCP root fallback; `/admin`, `/api` sit behind
  `chain-oauth@file`. Both compose files (repo and `$(DEPLOY_DIR)`) carry the
  labels and must stay identical (apart from the documented host lines).
- MCP identity: `readwrite`/`read` API key or OAuth token; `APIKeyMiddleware`
  sets `current_permission`, `current_api_key_id`, `current_oauth_token_id`,
  `current_user_id` contextvars **and** warms the per-user vault-root cache.
  Multi-user mode maps `user_id` → vault root (`vault._vault_root(user_id)`),
  and a user's root can be reassigned in the panel.
- File primitives: `validate_visible_path` (traversal + dot-dir), `_atomic_write`
  (same-dir temp + `os.replace`, hard-link no-clobber, `expected=` conflict
  check), `write_bytes`, `read_bytes`, `move_no_clobber` (soft-delete),
  `classify_bytes` (MIME), `MAX_FILE_READ_BYTES` / `MAX_FILE_WRITE_BYTES`. All
  are *path-based*: they resolve a pathname and then operate by name, which
  leaves an ancestor-symlink TOCTOU. Acceptable for the note tools today, not
  for a route reachable with a bearer capability from the open internet.
- Research (2026-08-15): no MCP client can pass attachment bytes to a tool;
  spec 2026-07-28 has no file primitive; SEP-2631 (`files/authorizeUpload`)
  is a draft; MCP Apps could render an in-chat drop-zone on claude.ai/ChatGPT
  (2.0 SDK, unverified for custom connectors); URL-mode elicitation works in
  Claude Code only. Prior art (gdrive-upload-mcp, Tigris, futuresearch): a
  short-lived signed upload URL.
- `settings.base_url` is derived from `MCP_HOSTNAME`, falling back to
  `http://localhost:8000` when neither is set — so "unset" is not observable
  from `base_url` alone. Uvicorn's default access logger records the full
  request target. `itsdangerous`, `slowapi`, `httpx 0.28`, `python-multipart`
  are dependencies.
- Codex reviewed the first draft of this design and found: a non-linearizable
  single-use check, overwrite tokens that could clobber edits made after mint,
  symlink TOCTOU in path-based FS ops, multipart parsed before token
  authentication, tokens landing in access logs, download tokens bound to a
  live path rather than a file, missing multi-user root binding, an
  under-specified SSRF guard, and an unobservable "base_url unset" state. This
  revision addresses each; the corresponding decisions are marked (C).

## Goals / Non-Goals

**Goals:**
- An agent in *any* client can get a binary into the vault (the human drops it
  on a link the agent produced, or the agent's shell `curl`s it) and confirm by
  handle; and can hand the human a download link for a vault file.
- An agent can import a file by URL safely from a container that sits next to
  Postgres/Ollama/registry.
- Every new write is atomic, no-clobber by default, path- and root-validated at
  use time, size-capped during streaming, symlink-safe, single-use, bound to the
  file state observed at mint, and logged with the minting identity. Nothing in
  this change can destroy vault content that a token was not explicitly minted
  to replace.
- Non-markdown files become deletable (soft-delete) by the agent.
- Capability tokens never appear in any request target, access log, or error
  trace.

**Non-Goals:**
- In-chat drop-zone (MCP Apps) — follow-up spike; this design keeps the same
  backend so it can be added as another front door.
- Server-side text extraction / OCR / PDF parsing.
- Uploads larger than `MAX_FILE_WRITE_BYTES` (no chunked/resumable protocol);
  HTTP `Range` on downloads.
- Public/shareable links.
- Migrating the existing note/`write_file` path-based writes to the anchored FS
  helper (recorded as a follow-up; the helper is written to make that easy).

## Decisions

1. **Opaque random tokens, stored hashed, in a DB table with an explicit state
   machine.** `secrets.token_urlsafe(32)` (256 bits), SHA-256 at rest. Table
   `transfer_tokens`: `id`, `token_hash` (unique), `direction`
   (`upload`|`download`), `state` (`pending`|`claimed`|`completed`|`consumed`),
   `path` (canonical vault-relative), `vault_root` (absolute root at mint),
   `overwrite`, `expected_fingerprint` (nullable JSON
   `{dev,inode,size,mtime_ns,ctime_ns,sha256|null}` of the target at mint —
   for overwrite uploads and for downloads),
   `key_id`/`oauth_token_id`/`user_id` (FKs `ON DELETE CASCADE`, nullable,
   exactly the minting identity), `created_at`, `expires_at`, `claimed_at`, `completed_at`,
   `size`, `sha256`, `mime`. Index on `expires_at`; pruned opportunistically
   on mint (`expires_at < now() − 1 day`). *Alternative:* in-memory or
   stateless signed tokens — cannot be consumed or revoked; rejected.

2. **(C) Single-use is a linearizable claim, made before any body byte is
   read.** `PUT` first executes `UPDATE transfer_tokens SET state='claimed',
   claimed_at=now() WHERE token_hash=:h AND direction='upload' AND
   state='pending' AND expires_at > now() RETURNING *` in its own committed
   transaction. Zero rows → uniform 404 (concurrent loser, expired, unknown,
   already used). Only after the claim commits does the handler read the body.
   On a *handled* pre-publication failure (413 over cap, 409 conflict, client
   disconnect, malformed request) the claim is released back to `pending` so
   the human can retry the same link — no bytes were published, so there is no
   replay risk. On success the row moves to `completed` in the same transaction
   that records `size`/`sha256`/`mime`. A crash after publication and before
   that commit leaves the row `claimed`, which is never replayable; once
   `expires_at` passes it reads as expired. Barrier-based concurrency test (against real Postgres — the conditional
   UPDATE's transaction boundary is the control, so a fake session cannot
   prove it): two `PUT`s racing on one token → exactly one 200, one 404, one
   file. **Streaming is bounded:** the body read runs under a deadline of
   `min(expires_at, claimed_at + TRANSFER_MAX_UPLOAD_SECONDS (600))` plus a
   30 s per-chunk idle timeout; a `TRANSFER_MAX_CONCURRENT_UPLOADS (4)`
   semaphore bounds simultaneous streams. Deadline or idle timeout →
   cleanup and the token becomes `consumed` (not reusable — the capability's
   TTL is the outer bound). Immediately before publication the handler
   opens a short transaction that `SELECT … FOR UPDATE`s the token row and
   its credential row (API key or OAuth token) and the user row, re-validates
   identity/root from those locked rows, **holds the locks across the
   filesystem publish**, then commits completion + the usage-log row in the
   same transaction; a row that vanished (cascade) or an identity that
   expired → abort, temp removed, nothing published. This closes the
   check-then-act window between the final validation and `link`/`replace`
   (revocation, reassignment, and cascade deletion all need a row lock that
   the publisher holds).

3. **(C) Tokens are redeemed from an `Authorization: Bearer <token>` header,
   never from the URL path.** Human-facing URLs carry the token in the
   *fragment*: `https://host/transfer/upload#<token>` and
   `https://host/transfer/download#<token>`. Fragments are not sent to the
   server, so Traefik and Uvicorn access logs see only `/transfer/upload`. The
   pages are static (identical for every token); their inline JS reads
   `location.hash`, calls `GET /transfer/upload/info` (or `/download/info`)
   with the header to display the bound path/cap/expiry, then `PUT
   /transfer/upload` (raw file body) or `GET /transfer/download/file`
   (fetch → Blob → `<a download>` click). `curl` uses `-H "Authorization:
   Bearer …" -T file`. Error handlers never echo request headers; a test
   captures logs during a failing upload and asserts the token is absent. The
   guarantee is scoped to what this repo controls: *server-generated* request
   targets and the app's own logging; a caller who pastes a token into a query
   string puts it in access logs themselves (the route ignores it, and the
   docstring warns). The deploy notes require Traefik header logging to stay
   at its default `drop` and no APM header capture (operator constraint,
   checked once at deploy).
   Because the pages render nothing token- or path-specific server-side, the
   XSS surface is the JSON→DOM step, done with `textContent`; the pages ship a
   nonce-based CSP (`default-src 'none'; script-src 'nonce-…'; style-src
   'nonce-…'; connect-src 'self'; form-action 'none'`).
   *Trade-off:* the download is a fetch-to-Blob in the browser rather than a
   native navigation download; fine for vault-sized files, and the only way to
   keep the secret out of the request line.

4. **(C) Path *and root* are committed at mint and re-validated at use, from the
   token row alone.** The transfer routes bypass `APIKeyMiddleware`, so they
   resolve everything from the row: read `users.vault_path` / `users.is_active`
   **directly from the database** (not the process-local vault-root cache,
   which the admin flow clears on reassignment and which another worker may
   hold stale), canonicalise it exactly as `_vault_root` does, and require it
   to equal the stored `vault_root` (a reassigned user's token is dead → 404); require the minting credential
   to still be valid with exact predicates — API key: `is_active`,
   `expires_at IS NULL OR expires_at > now()`, and `permission = 'readwrite'`
   for upload/import tokens; OAuth token: `revoked = false`, `expires_at >
   now()`, scope grants write for upload/import; and `users.is_active` — → 404
   otherwise; re-checked again immediately before publication. Re-run
   `validate_visible_path` against that root. The bound path is never taken
   from the request. Because the FKs cascade, deleting a key/token/user
   removes its transfer rows instead of blocking the delete; an in-flight
   upload whose row was cascaded fails the pre-publication re-read and
   publishes nothing.

5. **(C) Overwrite and download tokens are bound to the file state observed at
   mint — with content identity, and with honestly optimistic semantics.**
   The fingerprint is `{dev, inode, size, mtime_ns, ctime_ns, sha256}` where
   `sha256` is computed at mint when `size ≤ MAX_FILE_WRITE_BYTES` and is
   `null` for larger files (hashing multi-GB media at mint is not acceptable
   tool latency; the limitation is documented — metadata-only binding for
   files above the write cap). At publish (overwrite upload,
   `import_from_url`) the incumbent is `fstatat`ed `follow_symlinks=False`
   and, when the mint hash is present, re-hashed; any mismatch → 409, claim
   released. `os.stat` → `os.replace` is a check-then-act window: a writer
   that lands between them can still be overwritten. This is **declared as
   optimistic conflict detection**, not linearizable replacement — the same
   guarantee level `edit_note`'s `expected=` guard gives today, and adequate
   for a human-driven upload; the barrier test documents the window. A
   token minted with `overwrite=False` requires the target to be *absent* at
   publish, and there the hard-link no-clobber *is* kernel-linearizable.
   A null `expected_fingerprint` on an overwrite token means "target was
   absent at mint" and is an *expected-absence sentinel*: at publish the
   target must still be absent (409 otherwise) — never "skip the comparison".
   `request_download` records the fingerprint at mint; at fetch the file is
   opened `O_NOFOLLOW`, `fstat`ed and (for ≤ cap files) re-hashed from that
   descriptor before headers are sent — mismatch → 404; MIME/length come from
   the descriptor. In-place mutation of the inode *during* streaming is not
   prevented (declared limitation; would need a private copy).

6. **(C) Descriptor-anchored filesystem operations for every transfer write and
   for `delete_file`** — new helper `src/services/vault_fs.py`:
   `open_dir_beneath(root_fd, rel_dir, create=False) -> fd` walks components one
   at a time with `os.open(name, O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC,
   dir_fd=parent)` (creating with `os.mkdir(name, dir_fd=parent)` when asked,
   then re-opening with `O_NOFOLLOW`), so a symlink anywhere in the chain
   raises rather than being followed; `create_temp(dir_fd) -> (fd, name)`
   with `O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC`, mode `0600`, name
   `.tmp-<32 hex>`; `publish(dir_fd, tmp_name, final_name, *, overwrite,
   expected_fingerprint)` — no-clobber via `os.link(src_dir_fd=…,
   dst_dir_fd=…)` (EEXIST → conflict) then unlink tmp; overwrite via
   `os.stat(final, dir_fd, follow_symlinks=False)` (symlink → refuse;
   fingerprint mismatch → conflict) then `os.replace(src_dir_fd, dst_dir_fd)`;
   `remove(dir_fd, name)` / `soft_delete(...)` with `lstat` symlink refusal and
   link-then-unlink into `.trash/` under the same no-clobber discipline as
   `move_no_clobber`. Temp files are unlinked in `finally`, and publication is tracked
   separately from cleanup: once `link`/`replace` has succeeded the upload
   is complete even if the trailing temp unlink fails (logged, never releases
   the claim). Filesystem prerequisites — hard links and same-device
   `.trash` — are probed once at startup by linking a temp file in the vault
   root (`EPERM`/`EOPNOTSUPP`/`EXDEV` → the transfer tools return a stable
   "unsupported filesystem" error naming the probe, and nothing is written);
   `.trash` is created inside the vault root, so it is same-device by
   construction. `openat2`
   `RESOLVE_BENEATH` would be stronger but is not exposed by Python's stdlib;
   per-component `O_NOFOLLOW` opens from an anchored root fd give the same
   guarantee for our purposes (no symlink traversal, no escape). The helper is
   written so the existing `_atomic_write` can adopt it later (follow-up).

7. **Streaming size cap enforced while receiving, on the only body path (raw
   `PUT`).** (C) Multipart is dropped: FastAPI's `UploadFile` would parse and
   spool the body before the handler runs — i.e. before the token claim — and a
   multipart envelope has unbounded extra parts. `PUT` with `request.stream()`
   reads only after the claim, counts bytes, rejects early on a `Content-Length`
   above the cap, aborts at cap+1 with 413 (temp unlinked, claim released),
   computes `sha256` and MIME during the stream, and publishes via D6. The
   MCP transport limit is irrelevant here (plain HTTP route). Client
   disconnect → cleanup + release.

8. **Uniform 404 for unknown / expired / consumed / claimed-by-other / dead-
   identity / wrong-root tokens on the bearer-protected endpoints; precise
   status via the tool side.** Method matrix, explicit: `GET|HEAD
   /transfer/upload` and `/transfer/download` → static page (200; query
   strings ignored; no token processing); `GET|HEAD /transfer/upload/info`,
   `/transfer/download/info`, `/transfer/download/file` and `PUT
   /transfer/upload` → bearer-protected, uniform 404 on any non-usable token;
   any other method → 405. `OPTIONS` with CORS preflight headers is answered
   by the app-wide `CORSMiddleware` before route dispatch (its allowed
   origins do not include foreign origins, so a cross-origin preflight is
   refused there); a bare `OPTIONS` reaching the router → 405. Same-origin
   `fetch` with a custom header needs no preflight. Uniform-404 applies to
   the bearer-protected endpoints, not to the pages or to method dispatch. The public route is not an oracle (same body, no timing-distinct
   branches beyond a single indexed lookup); the authenticated agent gets
   `pending | uploading | completed | expired` from `check_upload`, scoped to
   the minting identity (another key/user → `not found`). If `uploading`
   persists (crash window), the agent mints a new token; the docstring says so.

9. **HTTP routes under `/transfer/` on the api-router** (`PathPrefix(\`/transfer\`)`
   added to `obsidian-mcp-api-rtr` in both compose files). The in-app
   `APIKeyMiddleware` must not intercept `/transfer/*` (it currently keys off
   `/mcp` and the bearer root fallback — task verifies and, if the root
   fallback matcher would swallow a `/transfer/*` request carrying `Bearer`,
   excludes the prefix explicitly). Rate limits via the existing `slowapi`
   limiter: `30/minute` for page/info, `10/minute` for `PUT` and `download/file`
   per client IP (proxy trust is the app's existing configuration; hardening
   that CIDR is out of scope and noted).

10. **`request_download`: multi-use within TTL, `HEAD` supported, `Range`
    ignored.** Browsers preview-then-save; single-use would break that. `HEAD`
    returns the same headers without a body; `Range` is ignored with
    `Accept-Ranges: none` and a full 200 (declared, not "supported").
    Headers: `Content-Type` from the classifier on the opened descriptor,
    `Content-Length` from `fstat`, `Content-Disposition: attachment;
    filename="<ascii-safe>"; filename*=UTF-8''<pct-encoded basename>` with
    CR/LF/quotes stripped, `X-Content-Type-Options: nosniff`, `Cache-Control:
    private, no-store`. The route bypasses the app-wide `GZipMiddleware`
    (`src/main.py:160`) — the response sets `Content-Encoding: identity`
    explicitly and the test asserts `GET` and `HEAD` agree on
    `Content-Length` with `Accept-Encoding: gzip`. Not subject to
    `MAX_READ_RESPONSE_CHARS` / `MAX_FILE_READ_BYTES` (not model input).

11. **(C) SSRF guard for `import_from_url`, one function, matrix-tested.**
    Canonicalise once with `urllib.parse.urlsplit` + IDNA-encoded host; reject:
    non-`https` (or non-`http` when `IMPORT_ALLOW_HTTP` is on), userinfo,
    empty/single-label hosts, `localhost` and any name ending `.localhost`,
    `.local`, `.internal`, `.home.arpa`, IPv6 zone ids (`%`), ports other than
    443/80 (+8443/8080 when http is allowed) — applied at *every* hop.
    ports are scheme-paired: `https` → 443 or 8443, `http` → 80 or 8080
    (only when allowed), checked per hop after any scheme change. Resolve
    with `getaddrinfo`; every answer must pass an **explicit deny policy**,
    not just `is_global` (Python 3.12's `is_global` admits multicast and the
    NAT64 prefix): reject loopback, private (RFC 1918, ULA `fc00::/7`),
    link-local (`169.254/16`, `fe80::/10`), CGNAT `100.64/10`, `0.0.0.0/8`,
    `240.0.0.0/4`, `198.18/15`, `192.0.0.0/24`, documentation ranges,
    multicast (`224/4`, `ff00::/8`), unspecified, reserved, IPv4-mapped/compat
    (`::ffff:0:0/96`, `::/96` → unmap and re-check the v4), NAT64
    (`64:ff9b::/96`, `64:ff9b:1::/48` → extract embedded v4 and re-check),
    6to4 (`2002::/16` → embedded v4 re-checked), Teredo (`2001::/32` →
    embedded v4 re-checked), and finally require `is_global`. Decimal/octal/
    hex IPv4 spellings are normalised by `ipaddress` before the check.
    Connect to the validated address, not a re-resolution: a per-call
    `httpx.AsyncHTTPTransport` subclass rewrites the request URL host to the
    IP and sets `Host` + `extensions["sni_hostname"]` to the original name;
    a **new client per hop** (no cross-host connection reuse); `trust_env=False`
    (no proxies); `http2=False`; `follow_redirects=False` with a manual loop
    ≤ 5 hops resolving relative `Location`s with `urljoin` and re-running the
    whole check; one wall-clock deadline of 30 s (`asyncio.timeout`) covering
    DNS, connects, redirects, headers and body; `Accept-Encoding: identity`
    and any response `Content-Encoding` → error, so the byte cap counts bytes
    written; final response must be `200`; body streamed through D7's cap and
    D6's publish with the D5 fingerprint rule. Tests: unit matrix on the
    checker (all ranges in v4/v6/mapped/decimal forms, names, ports, userinfo,
    zone ids, redirect targets, hop count, encoding, non-200, deadline) plus a
    real local plain-HTTP server on `127.0.0.1` reached only through an
    injected test policy that admits that one address, asserting the peer
    address used, the `Host` header received, and the header/URL rewrite
    (`sni_hostname` asserted on the transport call), and a TLS variant that
    generates a throwaway local CA + a leaf for the multi-label name
    `transfer.test` with the `openssl` CLI at test time (skipped if `openssl`
    is absent), injects that name's resolution to `127.0.0.1` and admits only
    that address in the test policy (so the *production* canonicaliser runs
    unchanged — `localhost` would be rejected before resolution), and asserts
    the pinned connection verifies against that CA under the correct name and
    that the server observed SNI `transfer.test`.

12. **`delete_file` for non-markdown files, anchored (D6).** Refuses `.md`
    with a pointer to `delete_note` (keeps the note/file split crisp and the
    destructive surface for an agent unchanged for notes), refuses directories
    and symlinks, soft-deletes to `.trash/<YYYYMMDD-HHMMSS>-<basename>` with
    collision suffixing, `permanent=True` unlinks. Index cleanup is not
    involved (non-markdown files are not indexed).

13. **Public origin must be explicit.** New `Settings.public_base_url` property:
    `base_url` if `MCP_HOSTNAME` or `BASE_URL` was operator-supplied, else
    `None` (tracked by a private flag before `_derive_public_urls` fills the
    localhost default). Mint tools return an error naming both settings when it
    is `None`. Setting names used consistently (`MCP_HOSTNAME`, `BASE_URL`).

14. **Filesystem semantics declared.** Supported: case-sensitive,
    non-normalising filesystems (the production bind mount is ext4/xfs). The
    canonical relative path stored in the token is what `validate_visible_path`
    yields; the kernel `link`/`replace` outcome is the authority for
    no-clobber. Case-insensitive/normalising mounts are out of scope and noted.

15. **Logging.** Mint tools log through `_tracked` with param allow-lists (never
    the token or a URL containing it — the URL fragment form makes this easy:
    log `upload_id`, `path`, `expires_in`, `overwrite`, import URL *host*).
    Route completions insert `usage_logs` rows (`tool="upload_file"` /
    `"download_file"`, identity from the token row).

## Risks / Trade-offs

- [Public unauthenticated route family] → D1–D5, D8, D9: 256-bit tokens hashed
  at rest, TTL ≤ 1 h, linearizable single-use, path+root pre-committed,
  fingerprint-bound overwrite/download, uniform 404, rate limits, header-only
  redemption, every completion logged with the minting identity.
- [Token leaks via chat transcript] → bounded to one pre-chosen path, one use
  (upload) or one file for ≤ 1 h (download); docstrings say "treat as secret".
- [SSRF] → D11 guard + matrix; accepted gap: real TLS-SNI test.
- [Symlink TOCTOU / escape] → D6 anchored ops; symlinks refused outright in
  transfer paths.
- [Overwrite token destroys later edits] → D5 fingerprint → 409.
- [Concurrent PUTs] → D2 claim; barrier test.
- [Large upload memory] → streamed to disk with a running counter.
- [Traefik label drift] → one task edits both files and diffs them.
- [Multi-user reassignment / revoked credentials] → D4 checks at use.
- [XSS on the page] → static page, `textContent`, nonce CSP; tests with
  script-closing/quote/newline/bidi/Unicode filenames.
- [Rate-limit spoofing from a sibling container] → pre-existing proxy-trust
  CIDR; noted, out of scope.
- [Fetch-to-Blob download for very large files] → acceptable; documented.
- [Existing path-based `_atomic_write` remains] → follow-up to adopt D6.

## Migration Plan

1. Alembic migration adds `transfer_tokens`. No backfill.
2. Compose label change in both files; `make deploy` recreates the container so
   Traefik picks up the new rule.
3. Post-deploy end-to-end via live tools + host `curl`: `request_upload` →
   `curl -H "Authorization: Bearer …" -T <100 KB png>` → `check_upload`
   (completed, sha matches) → `read_file` (image renders) → `request_download`
   → `curl -H … -o` + sha256 compare → `import_from_url` of a small public
   https asset → negatives: replay consumed token → 404; `import_from_url("https://127.0.0.1/x")`
   → tool error; overwrite token after touching the file → 409 →
   `delete_file` both files → `list_files` confirms; `.trash` holds them;
   `curl` the page URLs and confirm the response comes from the app (CSP
   header), not an OAuth redirect. Report which tools were called.
4. Rollback: previous image; the table is inert.

## Open Questions

- Default the target folder to Obsidian's `attachmentFolderPath` when `path`
  is a bare filename — deferred; v1 requires an explicit path.
