## ADDED Requirements

### Requirement: Transfer tokens are opaque, hashed, bound, stateful, and short-lived

The server SHALL mint transfer tokens as random values of at least 256 bits, store only their SHA-256 hash in a `transfer_tokens` table, and bind each token at mint time to a direction (`upload` or `download`), a canonical vault-relative path, the absolute vault root in effect for the minting user, the minting identity (`key_id` / `oauth_token_id` / `user_id`), an expiry, and — for overwrite uploads and for downloads — a fingerprint `{dev, inode, size, mtime_ns, ctime_ns, sha256}` of the target as observed at mint, where `sha256` is computed when the target is at most `MAX_FILE_WRITE_BYTES` and is null for larger files (metadata-only binding above the cap is a documented limitation). Identity references SHALL be `ON DELETE CASCADE`. Tokens carry a state (`pending`, `claimed`, `completed`, `consumed`). `expires_in` SHALL be clamped to `[60, 3600]` seconds with the default from `TRANSFER_TOKEN_TTL_SECONDS` (600). Tokens SHALL be redeemed only from an `Authorization: Bearer <token>` request header; a token SHALL never be accepted from a URL path or query string, and no *server-generated* request target SHALL contain a token (the tool docstrings SHALL warn that a caller who places the token in a query string exposes it to access logs). Vault-root and identity checks SHALL read `users.vault_path`, `users.is_active`, and credential rows from the database, never from a process-local cache.

#### Scenario: Token at rest is a hash

- **WHEN** `request_upload` or `request_download` mints a token
- **THEN** the `transfer_tokens` row SHALL contain the SHA-256 of the token and never the token itself

#### Scenario: Expiry is clamped

- **WHEN** a mint tool is called with `expires_in=5` or `expires_in=99999`
- **THEN** the token SHALL expire 60 s or 3600 s after minting respectively

#### Scenario: Token in the path or query is not accepted

- **WHEN** a request places a valid token in the URL path or query string instead of the `Authorization` header
- **THEN** the route SHALL respond as for a missing token (HTTP 404) and SHALL NOT act on it

#### Scenario: Token never appears in logs

- **WHEN** an upload fails with an application error while log capture is active
- **THEN** the captured logs SHALL NOT contain the token

### Requirement: Uniform 404 for every non-usable token

The bearer-protected transfer endpoints (`GET|HEAD /transfer/upload/info`, `GET|HEAD /transfer/download/info`, `GET|HEAD /transfer/download/file`, `PUT /transfer/upload`) SHALL return HTTP 404 with an identical body for a missing, unknown, expired, consumed, or already-claimed token, for a token whose minting credential is no longer valid or whose user is inactive, and for a token whose stored vault root no longer equals the root currently in effect for its user.

#### Scenario: Indistinguishable failures

- **WHEN** `/transfer/upload/info` is requested with each of: no header, an unknown token, an expired token, a completed upload token, a token minted by a since-revoked API key, and a token minted for a user whose vault root was since reassigned
- **THEN** every response SHALL be HTTP 404 with the same body

#### Scenario: Method matrix

- **WHEN** the routes are exercised with each HTTP method
- **THEN** `GET|HEAD /transfer/upload` and `/transfer/download` SHALL return the static page (200) regardless of query string and without processing any token; the bearer-protected endpoints SHALL accept only their listed methods; every other method reaching the router (including bare `OPTIONS` and `POST /transfer/upload`) SHALL return HTTP 405; a CORS preflight `OPTIONS` is answered by the app-wide CORS middleware, which does not allow foreign origins

#### Scenario: Credential predicates

- **WHEN** an upload token's API key has `expires_at` in the past, or its permission was downgraded to `read`, or its OAuth token is expired/revoked or lacks write scope, or its user is inactive
- **THEN** `/transfer/upload/info` and `PUT /transfer/upload` SHALL return HTTP 404 and, if the change happens mid-upload, the pre-publication re-check SHALL abort without publishing

#### Scenario: Cascade on credential deletion

- **WHEN** an API key, OAuth token, or user with pending or completed transfer rows is deleted
- **THEN** the delete SHALL succeed and the transfer rows SHALL be removed; an in-flight upload for a removed row SHALL fail its pre-publication locked re-validation and publish nothing

#### Scenario: Revocation racing publication

- **WHEN** a key revocation, permission downgrade, root reassignment, or credential deletion is committed after the upload's final validation began
- **THEN** either the change waits for the publisher's row locks and the upload completes under the old, valid state, or the change commits first and the upload aborts — never a publish under a revoked/reassigned identity

#### Scenario: Reassigned root read from the database

- **WHEN** a user's vault root is reassigned in the panel after a token was minted (process cache cleared or stale in another worker)
- **THEN** redemption SHALL return the uniform 404 (never a 500), by comparing the database's current `vault_path` with the stored root

#### Scenario: Overwrite token minted against an absent target

- **WHEN** `request_upload(path, overwrite=True)` is minted while `path` is absent (null fingerprint), a file then appears at `path`, and the upload is attempted
- **THEN** the upload SHALL return HTTP 409 and the file that appeared SHALL be byte-identical afterwards

### Requirement: `request_upload` tool

`request_upload(path, overwrite=False, expires_in=None)` SHALL require a `readwrite` identity, validate `path` with the vault traversal and dot-dir guards against the caller's vault root, refuse when the target exists and `overwrite` is false, record the target's fingerprint when `overwrite` is true and the target exists, mint an upload token, and return `upload_id`, an absolute `upload_url` of the form `<public_base_url>/transfer/upload#<token>`, `expires_at`, and `max_bytes` (`MAX_FILE_WRITE_BYTES`). If no public origin is configured (`MCP_HOSTNAME`/`BASE_URL` both unset) the tool SHALL return an error naming those settings and mint nothing.

#### Scenario: Successful mint

- **WHEN** a readwrite caller invokes `request_upload("Attachments/shot.png")` and the path is free
- **THEN** the response SHALL contain `upload_id`, an `https://…/transfer/upload#…` URL, `expires_at`, and `max_bytes`

#### Scenario: Read-only identity is refused

- **WHEN** a read-only caller invokes `request_upload`
- **THEN** the tool SHALL return the standard permission-denied error and mint nothing

#### Scenario: Existing target without overwrite

- **WHEN** `request_upload` targets an existing file with `overwrite=False`
- **THEN** the tool SHALL return an error and mint nothing

#### Scenario: Hidden or traversing path

- **WHEN** `request_upload` targets `.obsidian/x` or `../x`
- **THEN** the tool SHALL return an error and mint nothing

#### Scenario: No public origin

- **WHEN** neither `MCP_HOSTNAME` nor `BASE_URL` is set
- **THEN** `request_upload` SHALL return an error naming both settings and mint nothing

### Requirement: Upload endpoint claims first, streams within the cap, publishes atomically to the pre-committed path

`GET /transfer/upload` SHALL serve a static self-contained HTML page (no external assets, nonce-based CSP) whose script reads the token from the URL fragment, calls `GET /transfer/upload/info` with the bearer header to display the bound path, cap and expiry, and sends the chosen file as the raw body of `PUT /transfer/upload` with the bearer header. `PUT /transfer/upload` SHALL: (1) atomically transition the token from `pending` to `claimed` in a committed statement conditioned on `state='pending' AND expires_at > now()`, returning 404 if no row transitions, before reading any body byte; (2) re-validate identity (exact predicates: active, unexpired, write-capable credential; active user), vault root, and path from the token row; (3) reject early on a `Content-Length` above `MAX_FILE_WRITE_BYTES`; (4) stream the body — under a `TRANSFER_MAX_CONCURRENT_UPLOADS` semaphore, a deadline of `min(expires_at, claimed_at + TRANSFER_MAX_UPLOAD_SECONDS)`, and a 30 s per-chunk idle timeout — to a temporary file created in the target directory through descriptor-anchored operations (`O_CREAT|O_EXCL|O_NOFOLLOW`, mode 0600), counting bytes and aborting with HTTP 413 at cap+1; (5) compute `sha256` and MIME during the stream; (6) in a short transaction, lock (`SELECT … FOR UPDATE`) the token, credential, and user rows, re-validate identity and vault root from those locked rows, hold the locks across the filesystem publish, and commit completion and the usage-log row in that transaction; then publish via hard-link no-clobber when the token was minted without `overwrite` (kernel-linearizable), or via fingerprint-checked replace when minted with `overwrite` (optimistic: `stat`+hash compare then `replace`; a writer landing inside that window is a documented limitation), returning 409 if the target appeared, changed, or is a symlink; (7) move the token to `completed` with `size`, `sha256`, `mime`, `completed_at`, insert a `usage_logs` row (`tool="upload_file"`) attributed to the minting identity, and return JSON `{path, size, sha256, mime}`. On any handled failure before publication (413, 409, disconnect, malformed request) the temporary file SHALL be removed and the claim released to `pending`; on deadline or idle timeout the temporary file SHALL be removed and the token SHALL become `consumed`; a crash after publication SHALL leave the token `claimed` (never replayable). Publication SHALL be tracked separately from *all* trailing cleanup: the fact that `link`/`replace` succeeded SHALL be recorded before any subsequent step runs, and a failure in any of them — the trailing temp unlink, or the close of the destination, staging or root directory descriptor — SHALL be logged and SHALL NOT release the claim, SHALL NOT surface as a generic `OSError`, and SHALL NOT leave the token `pending`. The path SHALL never be taken from the request. An **unexpected** failure that is demonstrably before publication — an `OSError` while writing the staged body, an error opening the publish gate — SHALL also remove the temporary file and release the claim; only a failure after the bytes are in place (`PostPublishFailure`) SHALL leave the token `claimed`.

#### Scenario: A full disk mid-stream releases the claim

- **WHEN** writing the staged body fails with an `OSError` (e.g. `ENOSPC`)
- **THEN** the token SHALL be `pending` again, no temporary file SHALL remain, and nothing SHALL exist at the bound path

#### Scenario: A descriptor close fails after publication

- **WHEN** closing the destination, staging or root directory descriptor fails (e.g. `EIO`) after `link`/`replace` has already placed the bytes
- **THEN** the file SHALL exist at the bound path, the token SHALL NOT be returned to `pending`, and the request SHALL either succeed or fail as a post-publication failure — never as a generic pre-publication `OSError`

#### Scenario: Successful upload via PUT

- **WHEN** a valid upload token's bearer `PUT` carries a 100 KB PNG body
- **THEN** the file SHALL exist at the bound path with identical bytes, the response SHALL carry its `sha256`, and the token SHALL be `completed`

#### Scenario: Concurrent PUTs on one token

- **WHEN** two `PUT` requests with the same token start concurrently
- **THEN** exactly one SHALL succeed with HTTP 200 and one SHALL receive HTTP 404, and exactly one file SHALL be written

#### Scenario: Oversized body

- **WHEN** an upload body exceeds `MAX_FILE_WRITE_BYTES` (with or without `Content-Length`)
- **THEN** the route SHALL return HTTP 413, no file SHALL exist at the path, no temporary file SHALL remain, and the token SHALL be `pending` again

#### Scenario: Target appeared since mint (no-overwrite token)

- **WHEN** a file was created at the bound path after a no-overwrite token was minted
- **THEN** the upload SHALL return HTTP 409, the existing file SHALL be untouched, and the token SHALL be `pending` again

#### Scenario: Target changed since mint (overwrite token)

- **WHEN** an overwrite token was minted while the target had fingerprint F, the target was modified afterwards, and the upload is then attempted
- **THEN** the upload SHALL return HTTP 409 and the modified target SHALL be untouched

#### Scenario: Symlink in the target path

- **WHEN** any component of the bound path is a symlink at publish time
- **THEN** the upload SHALL be refused and nothing SHALL be written outside or inside the vault through the link

#### Scenario: Deadline and idle timeout

- **WHEN** a claimed upload sends one byte every 20 s past `claimed_at + TRANSFER_MAX_UPLOAD_SECONDS`, or stops sending for more than 30 s
- **THEN** the request SHALL be terminated, the temporary file removed, and the token SHALL be `consumed` (not reusable)

#### Scenario: Concurrent-upload bound

- **WHEN** more than `TRANSFER_MAX_CONCURRENT_UPLOADS` uploads stream simultaneously
- **THEN** the excess SHALL wait or receive HTTP 503, and no upload SHALL exceed the semaphore

#### Scenario: Body read only after claim

- **WHEN** a `PUT` arrives with an unknown token and a multi-gigabyte body
- **THEN** the route SHALL respond 404 without reading the body to disk

### Requirement: `check_upload` tool

`check_upload(upload_id)` SHALL return `pending`, `uploading` (claimed, not completed), `completed` (with `path`, `size`, `sha256`, `mime`, `completed_at`), or `expired`, and SHALL report `not found` for an `upload_id` minted by a different identity (or, in multi-user mode, a different user). The docstring SHALL tell the agent to mint a new token if `uploading` persists. The argument SHALL be validated against the exact shape `upload_id`s are minted with (22 characters of the URL-safe base64 alphabet) **before** it is written to `usage_logs`: an off-shape value SHALL be logged as a fixed `<invalid>` marker, SHALL NOT reach the database lookup, and SHALL return `not found`.

#### Scenario: A misused argument never reaches the log

- **WHEN** `check_upload` is called with a whole `…/transfer/upload#<token>` URL, or with the token itself, in place of the handle
- **THEN** the tool SHALL return `not found` and the `usage_logs` row SHALL record `upload_id` as `<invalid>`, containing no part of the supplied value

#### Scenario: Status transitions

- **WHEN** `check_upload` is called before an upload, after a completed upload, and after expiry of an unused token
- **THEN** it SHALL return `pending`, then `completed` with the file's `sha256`, then `expired`

#### Scenario: Cross-identity lookup

- **WHEN** identity B calls `check_upload` with an `upload_id` minted by identity A
- **THEN** the tool SHALL return `not found`

### Requirement: `request_download` tool and download endpoint

`request_download(path, expires_in=None)` SHALL validate the path with the vault guards, require the file to exist and not be a symlink or directory, record its fingerprint, mint a download token, and return an absolute `download_url` of the form `<public_base_url>/transfer/download#<token>`, `expires_at`, `size`, and `mime`. `GET /transfer/download` SHALL serve a static page whose script fetches `GET /transfer/download/file` with the bearer header and saves the result. `GET|HEAD /transfer/download/file` SHALL open the bound path `O_NOFOLLOW` through anchored operations, `fstat` it, compare with the mint fingerprint including a re-hash from the descriptor when the mint recorded a `sha256` (mismatch → 404), and stream it from that descriptor (in-place mutation of the inode during streaming is a documented limitation) with `Content-Encoding: identity` (bypassing the app's gzip middleware so `GET` and `HEAD` agree on `Content-Length`), `Content-Type` from the classifier, `Content-Length`, `Content-Disposition: attachment` with an ASCII-safe `filename` and RFC 5987 `filename*` (CR/LF/quotes stripped), `X-Content-Type-Options: nosniff`, `Cache-Control: private, no-store`, and `Accept-Ranges: none` (any `Range` header is ignored; `HEAD` returns headers only). Each `GET` SHALL insert a `usage_logs` row (`tool="download_file"`). Download tokens MAY be used repeatedly until expiry.

#### Scenario: Download round-trip

- **WHEN** `request_download("Docs/spec.pdf")` is followed by a bearer `GET /transfer/download/file`
- **THEN** the response body SHALL equal the file bytes with `Content-Type: application/pdf`, an attachment disposition, and `Cache-Control: private, no-store`

#### Scenario: File replaced after mint

- **WHEN** the file at the bound path is replaced after the token was minted, or modified in place with equal length and its mtime restored
- **THEN** `GET /transfer/download/file` SHALL return HTTP 404 and SHALL NOT serve the new content (for files above `MAX_FILE_WRITE_BYTES` only the metadata part of the fingerprint applies)

#### Scenario: Gzip does not alter the download

- **WHEN** `GET` and `HEAD /transfer/download/file` are issued with `Accept-Encoding: gzip`
- **THEN** both SHALL report the same `Content-Length` equal to the file size, with `Content-Encoding: identity`

#### Scenario: HEAD and Range

- **WHEN** `HEAD /transfer/download/file` or a `GET` with `Range: bytes=0-99` is issued with a valid token
- **THEN** `HEAD` SHALL return the same headers and no body, and the ranged `GET` SHALL return the full file with HTTP 200 and `Accept-Ranges: none`

#### Scenario: Missing file

- **WHEN** `request_download` targets a path that does not exist, is hidden, is a directory, or is a symlink
- **THEN** the tool SHALL return an error and mint nothing

### Requirement: `import_from_url` tool with SSRF guard

`import_from_url(url, path, overwrite=False)` SHALL require a `readwrite` identity, validate `path` with the vault guards and no-clobber (recording the target fingerprint when `overwrite` is true), and fetch `url` under all of the following rules, re-applied at every redirect hop: scheme `https` (or `http` only when `IMPORT_ALLOW_HTTP` is true); no userinfo; host is a multi-label IDNA-encodable name or an IP literal without zone id, and is not `localhost` nor ends in `.localhost`, `.local`, `.internal`, or `.home.arpa`; ports are scheme-paired: `https` → 443 or 8443, `http` → 80 or 8080 (only when allowed), re-checked after any scheme change; every resolved address SHALL pass an explicit deny policy — loopback, private (RFC 1918, ULA `fc00::/7`), link-local, CGNAT `100.64/10`, `0.0.0.0/8`, `240.0.0.0/4`, `198.18/15`, `192.0.0.0/24`, documentation ranges, multicast, unspecified, reserved, IPv4-mapped/compat (unmapped and re-checked), NAT64 (`64:ff9b::/96`, `64:ff9b:1::/48`; embedded IPv4 extracted and re-checked), 6to4 (`2002::/16`; embedded IPv4 re-checked), Teredo (`2001::/32`; embedded IPv4 re-checked) — and then be `is_global`, in decimal/octal/hex/IPv6 spellings alike; the connection SHALL be made to a validated resolved address (not re-resolved) with `Host` and TLS SNI set to the original name; a new HTTP client per hop; environment proxies disabled; HTTP/2 disabled; redirects followed manually up to 5 hops with relative `Location`s resolved against the current URL; one 30 s wall-clock deadline covering resolution, connects, redirects, headers and body; `Accept-Encoding: identity` with any response `Content-Encoding` rejected; final status must be 200; the body streamed through the same size-capped, anchored, fingerprint-checked publish path as uploads. The tool SHALL return `{path, size, sha256, mime, final_url}` on success and a tool-level error naming the violated rule otherwise, without writing. The publish SHALL happen inside a locked gate of the same kind the upload route uses: a transaction that `SELECT … FOR UPDATE`s the calling identity's credential row and (multi-user) user row, re-validates the write predicates and that the database's current vault root still equals the root captured when the tool started, and holds those locks across the filesystem publish.

#### Scenario: The identity dies while the body streams

- **WHEN** the calling API key is revoked or downgraded to `read`, or the user's vault root is reassigned, after `import_from_url` has begun streaming the response body
- **THEN** nothing SHALL be published, no temporary file SHALL remain, and the tool SHALL return an error saying the credentials are no longer valid

#### Scenario: Public https asset imported

- **WHEN** `import_from_url("https://example.com/a.png", "Attachments/a.png")` resolves to a global address and returns 200 with a body under the cap
- **THEN** the file SHALL be written atomically and the response SHALL include its `sha256`

#### Scenario: Non-global address rejected before connect

- **WHEN** the URL host resolves to (or literally is) `127.0.0.1`, `10.0.0.5`, `169.254.169.254`, `100.64.0.1`, `0.0.0.0`, `224.0.0.1`, `::1`, `::`, `ff02::1`, `fc00::1`, `::ffff:10.0.0.5`, `64:ff9b::a00:5`, `2002:0a00:0005::1`, `2001:0:0:0:0:0:f5ff:fffa` (Teredo embedding 10.0.0.5), `0x7f000001`, or `2130706433`
- **THEN** the tool SHALL return an error and no connection SHALL be attempted

#### Scenario: Forbidden names, ports, schemes, userinfo, zone ids

- **WHEN** the URL is `http://…` with `IMPORT_ALLOW_HTTP` false, or has host `localhost`, `db`, `x.internal`, `x.local`, or `[fe80::1%eth0]`, or port 5432/11434/5000/8080 with `https` or 443 with `http`, or contains `user:pw@`
- **THEN** the tool SHALL return an error and no connection SHALL be attempted

#### Scenario: Redirect to a non-global address rejected

- **WHEN** a public URL responds with a redirect (absolute or relative) whose target resolves to a non-global address or a forbidden port/scheme
- **THEN** the tool SHALL return an error and no file SHALL be written

#### Scenario: Pinned connection

- **WHEN** the fetch proceeds
- **THEN** the TCP connection SHALL be to the address validated for that hop, the `Host` header and TLS SNI SHALL be the original host name, and certificate verification SHALL be against that name

#### Scenario: Oversized, encoded, slow, or non-200 body

- **WHEN** the response body exceeds `MAX_FILE_WRITE_BYTES`, or carries `Content-Encoding`, or the deadline elapses, or the final status is not 200, or more than 5 redirects occur
- **THEN** the tool SHALL return an error and no file or temporary file SHALL remain

### Requirement: `delete_file` tool

`delete_file(path, permanent=False)` SHALL require a `readwrite` identity, validate the path with the vault guards, refuse markdown files (pointing to `delete_note`), directories, and symlinks, and by default move the file through anchored operations to `.trash/<YYYYMMDD-HHMMSS>-<basename>-<8 hex>`; with `permanent=True` it SHALL unlink the file. The markdown refusal SHALL be applied case-insensitively to the **canonical** final path component — the one the filesystem will open — not to the caller's raw string. The soft delete SHALL be a single `renameat2` call carrying `RENAME_NOREPLACE`, so that it never unlinks anything, never pre-creates or reserves the destination name, never clobbers an existing trash entry whoever created it, and moves a file that replaced the source concurrently into the trash rather than destroying it. A destination name already taken (`EEXIST`) SHALL be retried with a fresh random suffix, bounded; a kernel or filesystem that cannot perform a non-replacing rename (`EINVAL`, `ENOSYS`, `EXDEV`) SHALL raise the unsupported-filesystem error rather than fall back to a replacing `rename`. `probe_trash` SHALL exercise that same primitive, so an environment without `RENAME_NOREPLACE` is caught at first use rather than at the first delete.

#### Scenario: Markdown is refused however the path is spelled

- **WHEN** `delete_file` is called with `note.md/.`, `note.md/`, `a//note.md`, or `NOTE.MD`
- **THEN** each SHALL be refused with the pointer to `delete_note` and nothing SHALL be deleted

#### Scenario: The source is replaced while it is being trashed

- **WHEN** a different file replaces the source name after the `lstat` refusal has passed but before the move completes
- **THEN** that replacement SHALL end up in `.trash/` intact and no file SHALL be unlinked

#### Scenario: The trash destination name is taken by someone else

- **WHEN** the chosen `.trash/<YYYYMMDD-HHMMSS>-<basename>-<8 hex>` name is created by another writer before the move runs
- **THEN** the move SHALL fail `EEXIST` and retry under a different random suffix, and the other writer's file SHALL be left byte-for-byte intact

#### Scenario: The filesystem cannot do a non-replacing rename

- **WHEN** `renameat2(RENAME_NOREPLACE)` is unavailable or refused (`EINVAL`, `ENOSYS`, `EXDEV`)
- **THEN** the soft delete SHALL fail with the unsupported-filesystem error, SHALL NOT fall back to a replacing rename, and the source file SHALL remain in place

#### Scenario: Soft delete

- **WHEN** `delete_file("Attachments/shot.png")` is called
- **THEN** the file SHALL no longer exist at the path and SHALL exist under `.trash/` with the timestamped name

#### Scenario: Permanent delete

- **WHEN** `delete_file(path, permanent=True)` is called
- **THEN** the file SHALL be removed and nothing SHALL be written to `.trash/`

#### Scenario: Markdown, symlink, directory refused

- **WHEN** `delete_file` targets `note.md`, a symlink, or a directory
- **THEN** the tool SHALL return an error and nothing SHALL change

#### Scenario: Concurrent soft-deletes with the same basename

- **WHEN** two files with the same basename are soft-deleted within the same second
- **THEN** both SHALL exist in `.trash/` under distinct names

### Requirement: Filesystem probes run only on write paths, and sweep stale staging

The filesystem capability probes SHALL be split by the capability they test and SHALL run only where that capability is about to be used: a **publication** probe (hard link within the vault root) SHALL run on `request_upload`, `import_from_url` and `PUT /transfer/upload`, and a **trash** probe (`rename` of a temp file into `.trash/`) SHALL run only on a `delete_file` soft delete. Each SHALL be cached per vault root. No read path — `request_download`, `check_upload`, `GET|HEAD /transfer/download/info`, `GET|HEAD /transfer/download/file` — SHALL run any probe, because a probe writes. On the first publication probe per root the server SHALL remove `.transfer-tmp/.tmp-*` files whose mtime is older than 24 hours, and SHALL NOT remove newer ones.

#### Scenario: A read creates nothing

- **WHEN** a read-only identity calls `request_download` against a fresh vault
- **THEN** the vault SHALL contain exactly the files and directories it contained before the call — no `.trash/`, no probe temp file, no staging directory

#### Scenario: Stale staged uploads are swept, live ones are not

- **WHEN** `.transfer-tmp/` holds one `.tmp-*` file with an mtime 25 hours old and one written moments ago, and the publication probe runs for the first time for that root
- **THEN** the old file SHALL be removed and the recent one SHALL remain

### Requirement: Transfer routes are rate-limited and bypass the panel OAuth chain

The `/transfer/*` routes SHALL be served on the same reverse-proxy router as `/mcp` (no OAuth middleware) in both the repository and deployment compose files, SHALL NOT be intercepted by the in-app API-key middleware, SHALL be rate-limited per client address, and SHALL NOT expose any vault listing or any path other than the token's bound path.

#### Scenario: Rate limit enforced

- **WHEN** a client exceeds the configured per-minute limit on `PUT /transfer/upload` with concurrent requests
- **THEN** the excess SHALL receive HTTP 429

#### Scenario: Compose files agree

- **WHEN** the repository `docker-compose.yml` and the deployment copy are diffed
- **THEN** the api-router rule in both SHALL include `PathPrefix(\`/transfer\`)`

#### Scenario: Not intercepted by API-key auth

- **WHEN** a `/transfer/upload/info` request carries a bearer transfer token
- **THEN** it SHALL be handled by the transfer route, not rejected by `APIKeyMiddleware`
