# file-transfer Specification

## Purpose
TBD - created by archiving change binary-file-transfer. Update Purpose after archive.
## Requirements
### Requirement: Transfer tokens are opaque, hashed, bound, stateful, and short-lived

The server SHALL mint transfer tokens as random values of at least 256 bits, store only their SHA-256 hash in a `transfer_tokens` table, and bind each token at mint time to a direction (`upload` or `download`), a canonical vault-relative path, the absolute vault root in effect for the minting user, the minting identity (`key_id` / `oauth_token_id` / `user_id`), an expiry, and — for overwrite uploads and for downloads — a fingerprint `{dev, inode, size, mtime_ns, ctime_ns, sha256}` of the target as observed at mint, where `sha256` is computed when the target is at most `MAX_FILE_WRITE_BYTES` and is null for larger files (metadata-only binding above the cap is a documented limitation). Identity references SHALL be `ON DELETE CASCADE`. Tokens carry a state (`pending`, `claimed`, `completed`, `consumed`). `expires_in` SHALL be clamped to `[60, 3600]` seconds with the default from `TRANSFER_TOKEN_TTL_SECONDS` (600), and the stored `expires_at` SHALL then be clamped again to the minting credential's own expiry — `api_keys.expires_at` when it is non-null, and `oauth_tokens.expires_at` always, where a null value counts as already expired — so that no surface can advertise a deadline redemption would refuse. The clamp SHALL be computed by the mint routine itself, in the transaction that performs the INSERT and immediately before it, and SHALL NOT be accepted as a parameter from any caller. The same routine SHALL re-validate the credential against the exact predicates redemption uses (active, unrevoked, unexpired, write-capable for an upload, still belonging to the same user). When that clamp would leave under 30 seconds of lifetime, when the credential fails those predicates, or when no credential row backs the call at all, the mint tool SHALL return an error telling the caller to re-authenticate and SHALL create no `transfer_tokens` row. A mint whose deadline was shortened by the credential SHALL say so, and why, in its tool result. Tokens SHALL be redeemed only from an `Authorization: Bearer <token>` request header; a token SHALL never be accepted from a URL path or query string, and no *server-generated* request target SHALL contain a token (the tool docstrings SHALL warn that a caller who places the token in a query string exposes it to access logs). Vault-root and identity checks SHALL read `users.vault_path`, `users.is_active`, and credential rows from the database, never from a process-local cache.

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

#### Scenario: The minting credential's expiry clamps the link

- **WHEN** `request_upload` or `request_download` is called with `expires_in=3600` by a credential that expires in 120 seconds
- **THEN** the stored `expires_at` SHALL be the credential's expiry, every surface that shows a deadline (tool result, `/transfer/*/info`, both pages) SHALL show that same instant, and the tool result SHALL state that the lifetime was shortened by the credential

#### Scenario: A credential about to expire mints nothing

- **WHEN** a mint tool is called by a credential with under 30 seconds of life left, or by a request that carries no credential row
- **THEN** the tool SHALL return an error naming re-authentication as the fix and no `transfer_tokens` row SHALL be created

#### Scenario: A credential invalidated between the permission check and the INSERT

- **WHEN** the calling credential is revoked, deactivated, scope-downgraded below `readwrite`, or reassigned to another user after an upload mint tool starts but before it inserts its row
- **THEN** the mint SHALL refuse with an error naming re-authentication and no `transfer_tokens` row SHALL be created

### Requirement: Uniform 404 for every non-usable token

The bearer-protected transfer endpoints (`GET|HEAD /transfer/upload/info`, `GET|HEAD /transfer/download/info`, `GET|HEAD /transfer/download/file`, `PUT /transfer/upload`) SHALL return HTTP 404 with an identical body for a missing, unknown, expired, consumed, or already-claimed token, for a token whose minting credential is no longer valid or whose user is inactive, and for a token whose stored vault root no longer equals the root currently in effect for its user. When `MULTI_USER_MODE` is enabled, a credential or token row with `user_id IS NULL` SHALL be treated as no identity at all: the credential predicate SHALL reject it, and the vault-root checks (both the unlocked one and the one run against the rows held `FOR UPDATE`) SHALL reject it rather than authorising the globally configured `VAULT_PATH`. In single-user mode a null owner is normal and SHALL behave exactly as before.

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

#### Scenario: An ownerless capability outliving single-user mode

- **WHEN** an API key or OAuth token with `user_id IS NULL` mints an upload or download capability while `MULTI_USER_MODE` is disabled, and the operator then enables `MULTI_USER_MODE`
- **THEN** every redemption of that capability SHALL return the uniform 404, no bytes SHALL be written or served, and a further mint attempt from the same identity SHALL be refused

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

`GET /transfer/upload` SHALL serve a static self-contained HTML page (no external assets, nonce-based CSP) whose script reads the token from the URL fragment, calls `GET /transfer/upload/info` with the bearer header to display the bound path, the **mode** — whether the upload creates a new file or replaces the file already at that path, taken from the `overwrite` field of the info payload — the cap and the expiry, labelling the replace case destructively on both the action control and the status copy so the person pressing it knows the existing file will be lost, and sends the chosen file as the raw body of `PUT /transfer/upload` with the bearer header. `PUT /transfer/upload` SHALL: (1) atomically transition the token from `pending` to `claimed` in a committed statement conditioned on `state='pending' AND expires_at > now()`, returning 404 if no row transitions, before reading any body byte; (2) re-validate identity (exact predicates: active, unexpired, write-capable credential; active user), vault root, and path from the token row; (3) reject early on a `Content-Length` above `MAX_FILE_WRITE_BYTES`; (4) stream the body — under a `TRANSFER_MAX_CONCURRENT_UPLOADS` semaphore, a deadline of `min(expires_at, claimed_at + TRANSFER_MAX_UPLOAD_SECONDS)`, and a 30 s per-chunk idle timeout — into a file staged in the vault's staging directory through descriptor-anchored operations and holding **no directory entry**, counting bytes and aborting with HTTP 413 at cap+1; (5) compute `sha256` and MIME during the stream, relax the staged file's mode to the umask default, and **flush the staged bytes to durable storage** — all of this after the body ends and before the gate is opened, so the flush never runs under the gate's locks; (6) in a short transaction, lock (`SELECT … FOR UPDATE`) the token, credential, and user rows, re-validate identity and vault root from those locked rows, **re-check that the destination parent is on the same mount as the staging directory, and re-check the stream deadline against the current time immediately before the publish and inside those locks** — a gate that waited past the deadline SHALL raise the deadline error and the token SHALL become `consumed`, exactly as an overrun during the body does, and nothing SHALL be written — hold the locks across the filesystem publish, and commit completion and the usage-log row in that transaction; then publish the staged inode **by descriptor** when the token was minted without `overwrite` (kernel-linearizable), or via fingerprint-checked replace when minted with `overwrite` (optimistic: `stat`+hash compare then `replace`; a writer landing inside that window is a documented limitation), returning 409 if the target appeared, changed, or is a symlink; **flush the destination directory once the publish has been recorded and before the completion is committed**; (7) move the token to `completed` with `size`, `sha256`, `mime`, `completed_at`, insert a `usage_logs` row (`tool="upload_file"`) attributed to the minting identity, and return JSON `{path, size, sha256, mime}`. On any handled failure before publication (413, 409, disconnect, malformed request) the staged bytes SHALL be discarded — releasing the unnamed inode, and unlinking any transient staging name only while it still refers to that inode — and the claim released to `pending`; on deadline or idle timeout the staged bytes SHALL be discarded and the token SHALL become `consumed`; a crash after publication SHALL leave the token `claimed` (never replayable). Publication SHALL be tracked separately from *all* trailing work: the fact that the publish succeeded SHALL be recorded before any subsequent step runs, and a failure in any of them — the destination-directory flush, the trailing discard, or the close of the destination, staging or root directory descriptor — SHALL NOT release the claim, SHALL NOT surface as a generic `OSError`, and SHALL NOT leave the token `pending`. The path SHALL never be taken from the request. A destination that has come to sit on a different mount SHALL be refused before the link or rename is attempted, which is pre-publication and SHALL release the claim. An **unexpected** failure that is demonstrably before publication — an `OSError` while writing or flushing the staged body, an error opening the publish gate — SHALL also discard the staged bytes and release the claim; only a failure after the bytes are in place (`PostPublishFailure`) SHALL leave the token `claimed`.

#### Scenario: A publish gate delayed past the deadline

- **WHEN** the body finishes inside the stream deadline but the publish gate's lock acquisition or re-validation runs past it
- **THEN** nothing SHALL be published (including over an existing file for an overwrite token), no staged bytes SHALL remain, the response SHALL be the deadline overrun (HTTP 408), and the token SHALL become `consumed` rather than `pending` or `claimed`

#### Scenario: A full disk mid-stream releases the claim

- **WHEN** writing or flushing the staged body fails with an `OSError` (e.g. `ENOSPC`)
- **THEN** the token SHALL be `pending` again, no staged bytes SHALL remain, and nothing SHALL exist at the bound path

#### Scenario: A descriptor close fails after publication

- **WHEN** closing the destination, staging or root directory descriptor fails (e.g. `EIO`) after the publish has already placed the bytes
- **THEN** the file SHALL exist at the bound path, the token SHALL NOT be returned to `pending`, and the request SHALL either succeed or fail as a post-publication failure — never as a generic pre-publication `OSError`

#### Scenario: Successful upload via PUT

- **WHEN** a valid upload token's bearer `PUT` carries a 100 KB PNG body
- **THEN** the file SHALL exist at the bound path with identical bytes, the response SHALL carry its `sha256`, and the token SHALL be `completed`

#### Scenario: Concurrent PUTs on one token

- **WHEN** two `PUT` requests with the same token start concurrently
- **THEN** exactly one SHALL succeed with HTTP 200 and one SHALL receive HTTP 404, and exactly one file SHALL be written

#### Scenario: Oversized body

- **WHEN** an upload body exceeds `MAX_FILE_WRITE_BYTES` (with or without `Content-Length`)
- **THEN** the route SHALL return HTTP 413, no file SHALL exist at the path, no staged bytes SHALL remain, and the token SHALL be `pending` again

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
- **THEN** the request SHALL be terminated, the staged bytes discarded, and the token SHALL be `consumed` (not reusable)

#### Scenario: Concurrent-upload bound

- **WHEN** more than `TRANSFER_MAX_CONCURRENT_UPLOADS` uploads stream simultaneously
- **THEN** the excess SHALL wait or receive HTTP 503, and no upload SHALL exceed the semaphore

#### Scenario: Body read only after claim

- **WHEN** a `PUT` arrives with an unknown token and a multi-gigabyte body
- **THEN** the route SHALL respond 404 without reading the body to disk

#### Scenario: An overwrite link says so at the consent step

- **WHEN** the upload page loads for a token minted with `overwrite=True`
- **THEN** the page SHALL state that the upload replaces the existing file at the bound path, and its action control and status copy SHALL be labelled destructively; for a token minted with `overwrite=False` the page SHALL state that it creates a new file

### Requirement: `check_upload` tool

`check_upload(upload_id)` SHALL report the outcome of an upload link honestly, never asserting more about the vault than the token row proves, and SHALL return one of: `completed` (with `path`, `size`, `sha256`, `mime`, `completed_at`); `uploading` for a claimed token still inside its stream deadline, naming that deadline; `unknown` for a claimed token past it, stating that the bytes may already be in the vault because a publish can succeed and still fail to record its completion, and directing the caller to `list_files` / `read_file` the bound path before re-minting; `revoked` for a `pending` token whose minting credential no longer satisfies the redemption predicates or whose vault root has changed; `expired`; or `pending`. The stream deadline SHALL be `min(expires_at, claimed_at + TRANSFER_MAX_UPLOAD_SECONDS)` as an absolute UTC instant, computed by the same helper the upload route enforces it with **and measured against the same clock**: the route SHALL enforce that instant directly rather than converting it to a monotonic value, so that a realtime clock step cannot make the route and this tool describe different instants. A claimed or consumed token SHALL NEVER be reported as unused, whatever its expiry — "never used" SHALL be reachable only for a `pending` row — and a consumed token SHALL additionally state that nothing was published, since the deadline and idle-timeout paths abort before `publish`. For `pending` and `claimed` rows the tool SHALL re-check `resolve_identity_ok(need_write=True)` and `resolve_root_ok` against the database inside the session that read the row, because redemption decides usability from a strictly larger predicate than the row's state; `completed` rows SHALL NOT be re-checked. It SHALL report `not found` for an `upload_id` minted outside the calling **principal** — a different API key, a different OAuth grant family, or, in multi-user mode, a different user — as the handle-scoping requirement defines it, and SHALL NOT report `not found` merely because the presenting OAuth access token is a later rotation of the one that minted the handle. Precise status is permitted here and nowhere else: this side is authenticated and identity-scoped, unlike the public routes' uniform 404. The argument SHALL be validated against the exact shape `upload_id`s are minted with (22 characters of the URL-safe base64 alphabet) **before** it is written to `usage_logs`: an off-shape value SHALL be logged as a fixed `<invalid>` marker, SHALL NOT reach the database lookup, and SHALL return `not found`. No branch SHALL write a token, a credential, or any other secret into `usage_logs`.

#### Scenario: A misused argument never reaches the log

- **WHEN** `check_upload` is called with a whole `…/transfer/upload#<token>` URL, or with the token itself, in place of the handle
- **THEN** the tool SHALL return `not found` and the `usage_logs` row SHALL record `upload_id` as `<invalid>`, containing no part of the supplied value

#### Scenario: Status transitions

- **WHEN** `check_upload` is called before an upload, after a completed upload, and after expiry of an unused token
- **THEN** it SHALL return `pending`, then `completed` with the file's `sha256`, then `expired`

#### Scenario: Cross-principal lookup

- **WHEN** a credential outside the minting principal — another API key, or an access token from another grant family — calls `check_upload` with that handle
- **THEN** the tool SHALL return `not found`

#### Scenario: The agent's own handle after a refresh rotation

- **WHEN** the OAuth access token that minted an upload link has been replaced by a routine refresh, and the agent calls `check_upload` with that `upload_id` presenting the new access token
- **THEN** the tool SHALL answer for the row — `completed` with its `sha256` for an upload that landed, or the state's ordinary answer otherwise — and SHALL NOT report `not found`

#### Scenario: A claimed token past its stream deadline

- **WHEN** a token was claimed, the publish stranded it in `claimed` (a `PostPublishFailure`), and `check_upload` is called after `min(expires_at, claimed_at + TRANSFER_MAX_UPLOAD_SECONDS)` has passed
- **THEN** the tool SHALL report `unknown`, SHALL NOT say the link was never used, and SHALL direct the caller to inspect the bound path with `list_files` or `read_file` before minting another link

#### Scenario: A claimed token still in flight

- **WHEN** `check_upload` is called on a token claimed a moment ago, inside its stream deadline
- **THEN** the tool SHALL report `uploading`, name the deadline as a timestamp, and SHALL NOT assert that the transfer will not complete

#### Scenario: A realtime clock step

- **WHEN** the system clock steps forward or backward past a claimed token's stream deadline
- **THEN** `check_upload`'s classification and the upload route's enforcement SHALL move together, both measured against the same clock and the same absolute instant

#### Scenario: The credential lost write access after the mint

- **WHEN** the OAuth token that minted a still-`pending` upload link has its scope downgraded to read, or the user's vault root is reassigned
- **THEN** `check_upload` SHALL report that the link is no longer redeemable and name the reason, rather than reporting `pending`

#### Scenario: A consumed link past its TTL

- **WHEN** `check_upload` is called on a token whose stream was cut short (state `consumed`) after its TTL has passed
- **THEN** the tool SHALL say the upload was cut short and that nothing was published, and SHALL NOT say the link was never used

#### Scenario: A completed row needs no liveness re-check

- **WHEN** `check_upload` is called on a `completed` row whose credential has since been revoked
- **THEN** the tool SHALL report `completed` with the recorded `size`, `sha256`, `mime` and `completed_at`, and SHALL NOT run the liveness predicates

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

`delete_file(path, permanent=False)` SHALL require a `readwrite` identity, validate the path with the vault guards, refuse markdown files (pointing to `delete_note`), directories, and symlinks, and by default move the file through anchored operations to `.trash/<YYYYMMDD-HHMMSS>-<basename>-<8 hex>`; with `permanent=True` it SHALL unlink the file. The markdown refusal SHALL be applied case-insensitively to the **canonical** final path component — the one the filesystem will open — not to the caller's raw string. The soft delete SHALL be a single `renameat2` call carrying `RENAME_NOREPLACE`, so that it never unlinks anything, never pre-creates or reserves the destination name, never clobbers an existing trash entry whoever created it, and moves a file that replaced the source concurrently into the trash rather than destroying it. A destination name already taken (`EEXIST`) SHALL be retried with a fresh random suffix, bounded; a kernel or filesystem that cannot perform a non-replacing rename (`EINVAL`, `ENOSYS`, `EOPNOTSUPP`) SHALL raise the unsupported-filesystem error rather than fall back to a replacing `rename`. `EXDEV` SHALL NOT be classified with those, and SHALL be classified before it is named (the rename primitive's rule): a definite mount mismatch is surfaced as the mount-boundary error naming the mount layout as the cause and `permanent=True` as the workaround; a definite same-mount `EXDEV` names a security policy or filesystem-internal boundary instead; an unreadable mount identity is presented as ambiguous between them — never as missing non-replacing-rename support and never as `.trash/` being unable to receive a rename. Where the kernel can answer the mount question (`STATX_MNT_ID`), a best-effort preflight comparing the source parent with the opened `.trash` descriptor SHALL raise that same refusal before the rename is attempted; where it cannot answer, the preflight SHALL be skipped — never failed closed — and the rename's `EXDEV` mapping is the backstop. `probe_trash` SHALL exercise that same primitive, so an environment without `RENAME_NOREPLACE` is caught at first use rather than at the first delete — and when the probe's own rename fails `EXDEV` because `.trash/` sits on a different mount than the vault root, the probe SHALL preserve the mount-boundary cause in type and prose rather than re-wrapping it as generic filesystem inability.

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

- **WHEN** `renameat2(RENAME_NOREPLACE)` is unavailable or refused (`EINVAL`, `ENOSYS`, `EOPNOTSUPP`)
- **THEN** the soft delete SHALL fail with the unsupported-filesystem error, SHALL NOT fall back to a replacing rename, and the source file SHALL remain in place

#### Scenario: The source is on a different mount than `.trash/`

- **WHEN** `delete_file` soft-deletes a file whose directory is on a different mount than the vault root's `.trash/` (e.g. a directory of the same filesystem bind-mounted beneath the root)
- **THEN** the tool SHALL refuse with the mount-boundary error naming the mount layout as the cause and `permanent=True` as the workaround
- **AND** the file SHALL be untouched and nothing SHALL be created in `.trash/`
- **AND** the error SHALL NOT claim the filesystem cannot perform a non-replacing rename or that `.trash/` cannot receive one

#### Scenario: `.trash/` itself is a separate mount

- **WHEN** the vault root's `.trash/` directory is itself a mount distinct from the root's and `probe_trash` runs
- **THEN** the probe SHALL fail with the mount-boundary error, in prose naming the root/`.trash` mount layout
- **AND** the failure SHALL NOT be reported as the filesystem being unable to move files with a non-replacing rename

#### Scenario: A kernel that cannot answer the mount question keeps its soft delete

- **WHEN** a soft delete runs where `STATX_MNT_ID` is unavailable and the source and `.trash/` share a mount
- **THEN** the preflight SHALL be skipped and the soft delete SHALL proceed and succeed
- **AND** a cross-mount attempt on such a kernel SHALL still be refused by the rename's `EXDEV` classification, presented as ambiguous between a mount boundary and a policy or filesystem-internal boundary — the identity that would prove the mount claim is exactly what such a kernel cannot read

#### Scenario: Soft delete

- **WHEN** `delete_file("Attachments/shot.png")` is called
- **THEN** the file SHALL no longer exist at the path and SHALL exist under `.trash/` with the timestamped name

#### Scenario: Permanent delete

- **WHEN** `delete_file(path, permanent=True)` is called
- **THEN** the file SHALL be removed and nothing SHALL be written to `.trash/`

#### Scenario: Permanent delete still works across a mount boundary

- **WHEN** `delete_file(path, permanent=True)` targets a file on a mount beneath the vault root
- **THEN** the file SHALL be unlinked exactly as on a single-mount vault — an unlink crosses no mount boundary

#### Scenario: Markdown, symlink, directory refused

- **WHEN** `delete_file` targets `note.md`, a symlink, or a directory
- **THEN** the tool SHALL return an error and nothing SHALL change

#### Scenario: Concurrent soft-deletes with the same basename

- **WHEN** two files with the same basename are soft-deleted within the same second
- **THEN** both SHALL exist in `.trash/` under distinct names

### Requirement: Filesystem probes run only on write paths, and sweep stale staging

The filesystem capability probes SHALL be split by the capability they test and SHALL run only where that capability is about to be used: a **publication** probe SHALL run on `request_upload`, `import_from_url` and `PUT /transfer/upload`, and a **trash** probe (`rename` of a temp file into `.trash/`) SHALL run only on a `delete_file` soft delete. The publication probe SHALL exercise every primitive the publish depends on and can test from the vault root — a hard link within the vault root, allocation of a file with no directory entry, publication of such a file by descriptor, a flush of that file to durable storage, and a flush of a directory descriptor — so that an environment missing any of them is refused at the probe rather than after a body has been streamed. Where unnamed staging is the primitive that fails and the operator flag permits the named-staging fallback, the probe SHALL exercise the primitives *that* mode depends on instead of refusing, and every other primitive in the list SHALL still be required of it. A filesystem that supports unnamed staging and by-descriptor publication but rejects a directory flush would otherwise pass the probe, accept a token and a body, publish the file, and only then strand the claim as a post-publication failure. Each SHALL be cached per vault root. No read path — `request_download`, `check_upload`, `GET|HEAD /transfer/download/info`, `GET|HEAD /transfer/download/file` — SHALL run any probe, because a probe writes. On the first publication probe per root the server SHALL remove `.transfer-tmp/.tmp-*` files whose mtime is older than 24 hours, and SHALL NOT remove newer ones; that sweep SHALL be retained for staging files left by earlier releases even though the streaming path no longer creates named staging files.

**The publication probe is also what selects the staging mode, once per root.** Its cached result SHALL record which mode that root uses — unnamed staging with by-descriptor publication, or the named-staging fallback where unnamed staging is unavailable and the operator flag permits it — and every publication on that root SHALL use the recorded mode. The mode SHALL NOT be decided per call, per token or per body, and SHALL NOT flip for the life of the cached result: a root that stages one upload without a name and the next one under a name would make the window each upload ran in unknowable after the fact. `/health` SHALL read the fallback's activity from the process, not by re-probing, so consulting it creates nothing.

Availability of the beneath-root lookup SHALL NOT be tested by these probes: it is a property of the kernel and the container rather than of a vault root, it is identical for every root, and it is enforced by the read-only startup probe instead.

**What the probe covers SHALL be stated honestly.** It answers for the vault root and is cached per root, so it answers for properties the root and the destination share. It SHALL NOT be described as catching every capability the publish needs: a destination directory whose filesystem or mount differs from the root's can refuse a primitive the root accepted, and the probe cannot see it. The one such difference that is known to occur — a destination on a different mount, which refuses the link and the rename the publish depends on — is covered by the separate mount-identity check, not by this probe: that check refuses before any body is streamed where the boundary already exists at mint or fetch start, and inside the publish gate — after the body may already have streamed, but before anything is published — where it appears afterwards. Anything else is detected at the operation itself. The probe's guarantee is therefore "an environment that fails at the root is refused before any body is streamed", not "an environment that passes will publish".

#### Scenario: A read creates nothing

- **WHEN** a read-only identity calls `request_download` against a fresh vault
- **THEN** the vault SHALL contain exactly the files and directories it contained before the call — no `.trash/`, no probe temp file, no staging directory

#### Scenario: Stale staged uploads are swept, live ones are not

- **WHEN** `.transfer-tmp/` holds one `.tmp-*` file with an mtime 25 hours old and one written moments ago, and the publication probe runs for the first time for that root
- **THEN** the old file SHALL be removed and the recent one SHALL remain

#### Scenario: A vault that cannot stage without a name is refused at the probe

- **WHEN** the publication probe runs against a root whose filesystem cannot allocate a file with no directory entry, or in a container where an open descriptor cannot be published by reference, and the named-staging fallback flag is unset
- **THEN** the probe SHALL raise the unsupported-filesystem error naming that capability and naming the flag
- **AND** the transfer tools and routes for that root SHALL refuse rather than publish by staging name

#### Scenario: The probe records the staging mode and the mode does not change

- **WHEN** the publication probe has run for a root and selected a staging mode, and further uploads are then served for that root
- **THEN** every one of them SHALL stage in the mode the probe recorded
- **AND** the mode SHALL NOT be re-decided per call, and the probe SHALL NOT run again for that root

#### Scenario: A vault that cannot flush a directory is refused at the probe

- **WHEN** the publication probe runs against a root whose filesystem refuses a flush of an open directory descriptor
- **THEN** the probe SHALL raise the unsupported-filesystem error naming that capability
- **AND** no upload token SHALL be minted for that root, so no body SHALL be streamed and published only to strand its claim on the first directory flush

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

### Requirement: A transfer handle is scoped to the minting principal

The identity-scoped lookup behind `check_upload` SHALL resolve a `public_id` only for the **principal** that minted it, where the principal of an API key is that `api_keys` row and the principal of an OAuth access token is its **grant family** (`oauth_tokens.grant_id`), and SHALL additionally require the calling `user_id` to equal the row's. A handle SHALL therefore remain visible to its minting agent across any number of refresh rotations within one grant, and SHALL remain invisible to every other principal — a different API key, a different client, or a second `/authorize` approval by the same user for the same client. When the presenting credential row cannot be resolved, the lookup SHALL find nothing. The family comparison SHALL include the credentials' `client_id` as well as their `grant_id`, so that two clients can never share a principal. Grant separation is exact for every grant issued after migration 014; for rows whose `grant_id` came from 014's backfill it is approximate in one direction, because that backfill groups by `(client_id, user_id)` — two pre-014 consents by the same user for the same client share one family, and a token of either MAY therefore read the other's handle status. That is an accepted limitation, not a permitted widening: it grants read-only status on a handle that authorises nothing, and no other pair of principals SHALL be conflated.

Redemption SHALL NOT be widened in the same way: the `/transfer/*` routes and the publish gate SHALL keep re-validating the exact credential row recorded on the token. This is safe because a capability's expiry is already clamped to that credential's own expiry, so the minting credential outlives every link it minted unless it is revoked — and a revocation SHALL kill the link.

#### Scenario: A rotated access token still owns its handles

- **WHEN** an upload link is minted, completes, and the minting access token is then replaced by one or more refresh rotations within the same grant
- **THEN** the identity-scoped lookup SHALL return that row for any live token of the grant, with its recorded `path`, `size`, `sha256` and `mime`

#### Scenario: A second consent is a different principal

- **WHEN** the same user approves the same client a second time, producing a second grant family, and a token of that family presents the first family's handle
- **THEN** the lookup SHALL find nothing

#### Scenario: Two clients never share a principal

- **WHEN** a token registered to a different `client_id` presents a handle whose minting token carries the same `grant_id`
- **THEN** the lookup SHALL find nothing, and a token of the minting client's own family SHALL still resolve it

#### Scenario: Another user, another key

- **WHEN** a handle minted by one principal is presented by another user's credential, by the same grant under a different `user_id`, or by a different API key of the same user
- **THEN** the lookup SHALL find nothing in every case

#### Scenario: The two credential kinds do not see each other

- **WHEN** an API-key-minted handle is presented by an OAuth token, or an OAuth-minted handle is presented by an API key
- **THEN** the lookup SHALL find nothing in both directions

#### Scenario: The presenting credential row is gone

- **WHEN** the `oauth_tokens` row of the presenting access token has been deleted while the transfer row survives
- **THEN** the lookup SHALL find nothing

#### Scenario: Redemption stays bound to the minting credential

- **WHEN** an upload link's minting access token is revoked while a sibling token of the same grant family is still live
- **THEN** redemption SHALL be refused with the uniform 404, and the identity-scoped lookup SHALL still return the row so the authenticated tool can report it as no longer redeemable rather than as never minted

### Requirement: Every below-root directory descriptor comes from one kernel-enforced beneath-root lookup

The anchored filesystem layer SHALL obtain every directory descriptor below the vault root with a **single** kernel-enforced beneath-root lookup — `openat2(2)` carrying `RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS` — and SHALL NOT produce that descriptor by opening one path component at a time, so that no rename of an intermediate directory *during* the lookup can yield a descriptor outside the root. This governs every caller of the layer, without exception: the transfer publish's up-front and in-gate destination walks, the staging-directory open, the trash open, `delete_file`, every note mutation that anchors through it, **and the read-side callers** — the mint-time fingerprint and MIME-sniff reads (`_fingerprint_of`, `_head_bytes`) and the download route's bound-file open (`_open_bound_file`), which reach the layer through the same `open_parent`.

`RESOLVE_NO_XDEV` SHALL NOT be set: a mount point beneath the vault root is a supported deployment and is still beneath the root, and containment is what `RESOLVE_BENEATH` enforces. The lexical guard that refuses `..`, absolute paths and NUL bytes SHALL remain in front of the lookup, so those are refused with a message naming the offending path rather than by an errno.

Errno mapping SHALL distinguish four kinds of failure, because they tell an operator to do four different things.

- **A refused path.** A symbolic-link or non-directory component (`ELOOP`, `ENOTDIR`) SHALL raise the traversal error, and an attempted escape of the root (`EXDEV`) SHALL raise the traversal error as a **containment** refusal; neither SHALL be reported as an unsupported filesystem. `ENOENT` SHALL raise the not-found error as it does today.
- **A transient condition.** `EAGAIN` (the kernel could not prove containment because the path was being renamed concurrently) and `EINTR` (a signal interrupted the call) SHALL each be retried a **bounded** number of times and then refused. Neither SHALL be reported as success, and neither SHALL be allowed to escape as a generic `OSError`: the per-component walk it replaces used `os.open`, which retries `EINTR` transparently, so a raw syscall that does not retry would turn an ordinary signal into a false failure of `create_note`, `delete_file`, a transfer or a download.
- **An unavailable syscall.** `ENOSYS` and `EPERM` SHALL raise the unsupported-filesystem error naming `openat2`, the kernel version that introduced it, and the container seccomp profile as the two causes.
- **An ABI disagreement.** `EINVAL` (the kernel does not recognise the `struct open_how` size, or a flag or resolve bit the call passed) and `E2BIG` (the call passed extension data beyond the size this kernel knows) SHALL raise the unsupported-filesystem error naming `openat2` and the structure mismatch. They are not expected from a correct binding against any kernel that has the syscall — they are what a binding bug or a future ABI revision looks like — and treating them as anything softer than a refusal would let a lookup that never ran be mistaken for one that succeeded.

The traversal error SHALL name the **requested vault-relative path**. It SHALL NOT be required to name the offending component: a single `openat2` reports `ELOOP` for the path as a whole and says nothing about which component caused it, and a diagnostic walk issued afterwards can report a different state than the one the kernel refused. Any component identification an implementation adds SHALL be best-effort and SHALL be worded as such, never as an authoritative statement about what the kernel saw. This is a different check from the one that names a **symlinked final component** with its canonical vault-relative target: that is the leaf `lstat` a mutating tool performs through the parent descriptor, it is unchanged by this requirement, and it SHALL keep naming the target.

Availability SHALL be enforced twice: a **read-only** startup probe that creates nothing SHALL terminate the process with a message naming those causes when the syscall is unavailable, and the call site SHALL raise the unsupported-filesystem error if it encounters the same condition anyway. There SHALL be **no** fallback to a per-component walk on any path, under any condition.

The startup probe SHALL be skipped only under `MCP_SANDBOX_MODE`, alongside the startup guards that are already skipped there. That is the one configuration in which a call site can be reached with the syscall unavailable, and what each surface then answers SHALL follow the error contract it already has rather than a new one: a tool SHALL return the unsupported-filesystem error, `PUT /transfer/upload` SHALL answer its existing unsupported-filesystem status, and a **read** route SHALL answer the uniform 404 that every other refusal on a bearer-protected read produces. The download route SHALL NOT be made to distinguish an unavailable syscall from a missing file — that endpoint answering one status for every refusal is a deliberate property, and precision comes from the authenticated side.

Creating a missing directory MAY still be done one component at a time, because the syscall cannot create intermediate directories. Every such creation SHALL be issued through a directory descriptor obtained by a **fresh** beneath-root lookup of the prefix that already exists, performed from the root descriptor immediately before that one creation; no directory descriptor SHALL be carried across a creation and reused for the next one, and no directory descriptor produced by a creation SHALL be returned to a caller or used as a pathname anchor for any later operation. The directory descriptor a caller receives SHALL always come from a fresh single beneath-root lookup of the whole parent path performed **after** the creation completes, including where the creation is deferred to first use of a validated target's parent.

**The creation side therefore keeps a bounded residual, and it SHALL be stated rather than claimed closed.** There is no beneath-root form of directory creation, so between the atomic lookup of a prefix and the single creation issued through it, that prefix can still be renamed out of the vault, and the directory created is then outside the root. What SHALL hold regardless: no file and no file content is ever written through a directory descriptor a creation produced, because the directory descriptor every subsequent operation of the call anchors to comes from a fresh lookup the kernel proved beneath the root; and the residual costs at most one **empty** directory per component **per creation descent**, in a directory the renaming process already controls.

The bound is per creation descent, not per call, and the difference SHALL NOT be papered over: an upload walks its destination twice with creation enabled — once up front, so that a `..`, a symlinked ancestor or a non-directory costs one syscall rather than a whole body, and once authoritatively inside the publish gate — so a sufficiently coordinated race can leave one escaped empty directory for each. A note write performs one such descent. Neither descent may return, or use as a pathname anchor, a directory descriptor it created, which is what keeps the cost at empty directories.

**What a beneath-root lookup proves, and what it does not, SHALL be stated exactly**, in the words every artifact of this change uses: **Every below-root directory descriptor a call uses as a pathname anchor comes from a lookup the kernel proved beneath the vault root at the moment it resolved, and no directory descriptor retained from a creation descent is ever returned to a caller or used as a pathname anchor — so no operation is ever redirected into a directory that was never beneath the root.** This is a claim about **directory** descriptors used as pathname anchors: a call's own staged payload descriptor is created by that call, is written, flushed and published through by descriptor, and never anchors a pathname lookup. A lookup does not, and cannot, promise where that directory will be a moment later. A directory descriptor keeps naming the same directory however its pathname is subsequently renamed — the property this whole design relies on to keep a publish on the directory that was validated rather than on a substitute left at its name — so a process that renames the resolved directory out of the vault after the lookup and before the link or rename carries the call with it, and the bytes land there. That interval exists after the transfer gate's final destination lookup and before publication, and after a note tool's lookup and before its publish. It SHALL be recorded as a retained residual of descriptor anchoring, inherent to it and unchanged by this change, rather than specified as prevented.

#### Scenario: An ancestor is renamed out of the vault during the lookup

- **WHEN** a path `A/B/note.md` is being resolved to a parent descriptor and another process renames `<vault>/A` to a directory outside the vault root while the resolution is in progress
- **THEN** the lookup SHALL either return a descriptor the kernel resolved beneath the vault root or fail
- **AND** SHALL NOT return a descriptor obtained by opening the path one component at a time, nor any directory descriptor whose containment the kernel did not establish
- **AND** the call SHALL NOT be redirected into a directory that was never beneath the root

#### Scenario: The resolved directory is renamed out of the vault after the lookup

- **WHEN** a lookup has returned a descriptor the kernel proved beneath the vault root, and another process then renames that directory — or an ancestor of it — outside the root before the call publishes through it
- **THEN** the operation SHALL take effect in the directory that was resolved, wherever it has since been moved, and MAY be reported as successful
- **AND** this SHALL be recorded as a retained residual of anchoring an operation to a directory descriptor, not specified as prevented
- **AND** no directory other than the one the lookup resolved SHALL be written to

#### Scenario: An ancestor is renamed out of the vault during a directory creation

- **WHEN** a write names `A/B/C/note.md` with `A` present and `B` absent, and another process renames `<vault>/A` outside the root while the missing directories are being created
- **THEN** each creation SHALL have been issued through a directory descriptor obtained by a fresh beneath-root lookup of the prefix that already existed, not through a directory descriptor carried over from an earlier creation
- **AND** the directory descriptor the write anchors to SHALL come from a beneath-root lookup performed after the creation, so no file content SHALL be written through a directory descriptor the creation produced
- **AND** what the race can leave outside the root SHALL be at most one empty directory per component per creation descent — never a file and never file content
- **AND** where a call performs more than one creation descent, as an upload does, the bound SHALL be stated per descent rather than per call
- **AND** the residual SHALL be documented rather than reported as prevented

#### Scenario: A deferred parent creation is followed by a fresh beneath-root lookup

- **WHEN** a write names a path whose parent directory does not exist, and the parent is created on first use of the validated target
- **THEN** the directory descriptor the write anchors to SHALL be the result of a beneath-root lookup performed after the creation
- **AND** SHALL NOT be a directory descriptor retained from the creation descent

#### Scenario: A symlinked component is still refused with the traversal error

- **WHEN** any component of the path below the root is a symbolic link at lookup time
- **THEN** the lookup SHALL raise the traversal error naming the requested vault-relative path
- **AND** any identification of which component was the link SHALL be best-effort and worded as such, never an authoritative statement about what the kernel refused
- **AND** SHALL NOT follow the link, whether its target is inside or outside the vault

#### Scenario: A containment refusal is not reported as an unsupported filesystem

- **WHEN** the lookup fails because resolution would have escaped the vault root
- **THEN** the error SHALL identify it as a path-containment refusal
- **AND** SHALL NOT tell the operator that the filesystem lacks a capability

#### Scenario: A mount point inside the vault still resolves

- **WHEN** a directory beneath the vault root is a separate mount and a path below it is resolved
- **THEN** the lookup SHALL succeed and the descriptor SHALL name that directory

#### Scenario: A raced resolution is retried, not failed and not looped

- **WHEN** the kernel reports the resolution as raced by a concurrent rename
- **THEN** the lookup SHALL retry a bounded number of times
- **AND** SHALL refuse with an error once that bound is exhausted, rather than retrying indefinitely or returning a descriptor it did not obtain

#### Scenario: The syscall is unavailable at startup

- **WHEN** the server starts on a kernel that does not provide `openat2`, or in a container whose seccomp profile blocks it
- **THEN** the server SHALL terminate during startup with a message naming the syscall, the required kernel version and the seccomp profile
- **AND** the probe that detected it SHALL have created no file or directory anywhere

#### Scenario: The syscall is unavailable at a call site

- **WHEN** a beneath-root lookup is attempted and the syscall is unavailable
- **THEN** the operation SHALL be refused with the unsupported-filesystem error
- **AND** SHALL NOT fall back to opening the path one component at a time
- **AND** nothing SHALL be written

#### Scenario: A read path refuses without becoming a capability oracle

- **WHEN** the syscall is unavailable in the one configuration that skips the startup probe, and a valid download token is redeemed
- **THEN** the route SHALL answer the same uniform 404 it answers for a missing or replaced file
- **AND** the mint-time fingerprint and MIME-sniff reads SHALL surface the unsupported-filesystem error to their authenticated caller
- **AND** no path SHALL fall back to opening the path one component at a time

#### Scenario: A signal interrupts the lookup

- **WHEN** the syscall returns `EINTR` on its first attempt and would succeed on a retry
- **THEN** the lookup SHALL retry and succeed
- **AND** SHALL NOT fail the calling tool, transfer or download

#### Scenario: A symlinked component is refused by path, not by component

- **WHEN** a component below the root is a symbolic link at lookup time and is removed again before anything else can observe it
- **THEN** the traversal error SHALL name the requested vault-relative path
- **AND** SHALL NOT assert authoritatively which component the kernel refused

### Requirement: Staged transfer bytes and their publication are made durable

The transfer publish path SHALL flush the staged payload to durable storage before the publish gate is entered, and SHALL flush the destination directory after publication, so that a crash cannot leave a transfer recorded `completed` whose file at the bound path is absent, truncated, or does not match the recorded `sha256`. This SHALL apply to `PUT /transfer/upload` and to `import_from_url`, which share the same streaming publish.

The payload flush SHALL happen after the body has been fully received and before the pre-publication gate is opened, so that a flush of up to `MAX_FILE_WRITE_BYTES` never runs while the gate's `SELECT … FOR UPDATE` locks are held, and SHALL NOT block the event loop for its duration. A failure of the payload flush SHALL be treated as pre-publication: nothing SHALL be published, the staged bytes SHALL be discarded, and an upload claim SHALL be released to `pending`.

The directory flush SHALL happen after the publication has been recorded and before the completion is committed. A failure of the directory flush SHALL therefore be classified as post-publication and SHALL surface as the post-publication failure type — never as a generic `OSError`, which the upload route reads as "nothing was published" and answers by releasing a replayable claim over a path that already holds the file. An upload whose directory flush failed SHALL remain `claimed`, SHALL NOT be reported `completed` by `check_upload`, and SHALL NOT be replayable. The directories to flush SHALL be the **complete ancestor chain** from the destination parent up to the vault root, innermost first — not only the directories the publishing call itself created. Per-call creation provenance is insufficient and SHALL NOT be relied on: a call that creates a directory and then aborts before publication flushes nothing, correctly, because it published nothing; the call that later succeeds finds that directory already present, records no creation of it, and would leave the entry naming it made durable by nobody. The obligation outlives the call that incurred it, and outlives the process, so no in-memory record of "who created what" can discharge it. The chain is bounded by path depth and a directory flush is metadata-only, so the conservative rule is also the cheap one.

#### Scenario: The payload is durable before the gate

- **WHEN** an upload body has been fully received
- **THEN** the staged bytes SHALL have been flushed to durable storage before the publish gate is entered
- **AND** the flush SHALL NOT run while the gate holds its row locks

#### Scenario: The flush does not stall the server

- **WHEN** the payload flush of a maximum-size upload is in progress
- **THEN** other requests in the same process SHALL continue to be served

#### Scenario: The payload flush fails

- **WHEN** flushing the staged payload fails
- **THEN** nothing SHALL exist at the bound path, no staged bytes SHALL remain, the token SHALL be `pending` again, and the failure SHALL NOT be reported as a post-publication failure

#### Scenario: The directory flush fails after the bytes are in place

- **WHEN** the publish has placed the bytes at the bound path and the subsequent flush of the destination directory fails
- **THEN** the file SHALL exist at the bound path
- **AND** the token SHALL remain `claimed` — never returned to `pending`, never `completed`
- **AND** the request SHALL fail as a post-publication failure, not as a generic pre-publication `OSError`
- **AND** `check_upload` SHALL answer `uploading` or `unknown` for that handle, directing the caller to inspect the bound path, and SHALL NOT answer `completed`

#### Scenario: A newly created destination folder is durable too

- **WHEN** an upload publishes into a folder that the same call created
- **THEN** the directories that call created SHALL be made durable along with the destination entry

#### Scenario: A retry makes durable what an aborted attempt created

- **WHEN** an upload creates the missing directories of its destination and is then refused before publication — an over-cap body, a disconnect, a deadline overrun — releasing its claim without flushing anything, and the capability is redeemed again with a body that publishes successfully
- **THEN** the successful call SHALL flush the entry naming every directory above its destination parent, up to the vault root, even though it created none of them and recorded no creation
- **AND** the completed upload SHALL NOT depend for its survival on a flush that only the aborted attempt could have performed

#### Scenario: `import_from_url` gets the same durability

- **WHEN** `import_from_url` fetches a body and publishes it
- **THEN** the payload SHALL have been flushed before its gate is entered and the destination directory SHALL be flushed after publication, with the same failure classification as an upload

### Requirement: Transfer staging holds no directory entry wherever unnamed staging is available

An upload's staged bytes SHALL be held for the whole of the streaming window in a file with **no directory entry** on every vault root whose publication probe establishes that unnamed staging and by-descriptor publication work there, so that nothing in the staging directory can be observed, replaced or raced, and so that abandoned bytes are reclaimed by the kernel rather than left for a sweep. Staging SHALL allocate the unnamed file in the staging directory beneath the vault root, which SHALL continue to exist and to be held owner-only, since the directory is what selects the filesystem the inode lives on.

The no-clobber publish SHALL publish that inode **by descriptor**, so what lands at the destination is provably the inode this call wrote and no name is consulted. The overwrite publish SHALL NOT be required to be nameless, because a replacing rename has no by-descriptor form; instead it SHALL create a name for the staged inode only **inside the publish gate**, immediately before the fingerprint check and the rename, and only in the staging directory — never in the destination directory. That name SHALL be created no-clobber, retried under a fresh name if it is already taken, verified to still refer to the staged inode immediately before the rename, and on cleanup unlinked **only** while it still refers to that inode, otherwise left in place and logged.

This requirement governs the unnamed-staging mode and nothing else. Where the probe establishes that unnamed staging or by-descriptor publication is *unavailable*, the transfer SHALL be refused with an error naming both the missing capability and the operator flag; the named-staging fallback that flag permits is the **only** departure from this requirement, and it is governed by the requirement below. Absent that flag an implementation SHALL NOT publish whatever a staging name refers to.

#### Scenario: Nothing is observable while a body streams

- **WHEN** an upload body is streaming on a root whose publication probe selected unnamed staging
- **THEN** the staging directory SHALL contain no directory entry for the bytes being staged

#### Scenario: An abandoned upload leaves nothing behind

- **WHEN** an upload on a root whose publication probe selected unnamed staging is abandoned mid-stream, or the process is killed while one is in flight
- **THEN** the staged bytes SHALL be reclaimed without any file remaining in the staging directory for a later sweep to remove

#### Scenario: The overwrite path's name exists only inside the gate

- **WHEN** an overwrite upload publishes on a root whose publication probe selected unnamed staging
- **THEN** a name for the staged inode SHALL exist only between the publish gate's acquisition of its locks and the completion of its rename
- **AND** that name SHALL be in the staging directory, not in the destination directory

#### Scenario: The transient overwrite name is substituted before the identity check

- **WHEN** another process replaces the transient staging name with a different file after it is created and before the identity check that immediately precedes the rename
- **THEN** the upload SHALL be refused, the destination SHALL hold its prior content, and the substituted file SHALL be left in place rather than unlinked

#### Scenario: The transient overwrite name is substituted after the identity check

- **WHEN** the substitution lands in the interval between that identity check and the rename itself
- **THEN** the refusal SHALL NOT be guaranteed — the identity check narrows the window to one syscall and does not close it — and this SHALL be recorded as an accepted residual rather than specified as prevented
- **AND** reaching it SHALL require write access to the owner-only staging directory, which is the same access that permits editing the destination directly and is therefore outside the threat this change addresses

#### Scenario: Unnamed staging or by-descriptor publication is unavailable

- **WHEN** the vault filesystem cannot allocate a file without a directory entry, or the container cannot publish an open descriptor by reference
- **THEN** the transfer SHALL be refused with an error naming the unsupported capability and the operator flag that permits the named-staging fallback
- **AND** SHALL NOT publish by staging name unless that flag is set, in which case the requirement below governs the whole of the departure

### Requirement: Where unnamed staging is unavailable the transfer path stages under a name only behind the operator flag

Transfer publication SHALL treat the absence of unnamed staging or of by-descriptor publication as a refusal by default, and SHALL stage under a name instead only where the operator has set `VAULT_ALLOW_NAMED_STAGING_FALLBACK` — the same single flag that governs the note path's fallback, default off. With the flag unset the refusal SHALL be the unsupported-filesystem error and SHALL name that flag, so an operator meeting it does not have to read the source to find the escape valve; this is the refusal shape the note path already uses, and the two SHALL be phrased alike.

With the flag set, the transfer path SHALL keep the named `.transfer-tmp` staging it used before this change: an exclusively created, non-symlink-following `.tmp-*` file, made through the staging directory descriptor the beneath-root lookup returned, held owner-only for the staging window, published out of that directory by hard link (no-clobber) or replacing rename (overwrite). Everything outside the staging mode SHALL be untouched by the fallback — the payload flush, the directory flush, the publish gate and its lock order, the mount-identity check, the beneath-root lookup, the size caps and the token state machine are the same on both branches. The fallback changes where the bytes are staged and nothing else.

Two guarantees the fallback SHALL carry that the pre-change path did **not**, because the unnamed mode's transient overwrite name is specified with them and a mode that keeps a name for minutes has more need of them, not less: the staged name SHALL be verified to still refer to the inode this call staged immediately before it is published, and the discard SHALL unlink it **only** while it still refers to that inode, otherwise leave it in place and log. The pre-change publish unlinks its staging name unconditionally; the fallback SHALL NOT reproduce that, for the reason that already governs every other cleanup here — answering a substitution by deleting the substitute is a destructive write aimed at a different file. The no-clobber publish SHALL remain no-clobber in either mode: a hard link that fails when the destination already exists, never a replacing rename.

The window the fallback reopens SHALL be declared rather than implied, in the same register as the overwrite publish's in-gate window. A named staging file carries a directory entry for the whole streaming window, so the substitution the unnamed mode closes structurally is open again for that window, narrowed — not closed — by the identity check that precedes the publish. The threat difference between the two fallbacks SHALL be stated rather than rounded off: the transfer path stages in `.transfer-tmp`, an owner-only dot-directory beneath the vault root that the indexer skips and every tool's hidden-path guard refuses, so no agent, no capability and no vault tool can reach a staged name and the residual adversary is a process running as the same uid — which can rewrite the destination directly and needs no race. The note path's fallback stages beside the destination, in a directory the vault's own tools can write to. The transfer fallback's window is therefore **narrower** than the note path's, and the two SHALL NOT be documented as equivalent.

The fallback SHALL be observable without reading the source. It SHALL log a warning exactly once per process, the first time a call actually stages under a name, and SHALL NOT log it when the flag is merely set or when the probe merely selects the mode — the distinction between "an operator enabled this defensively" and "this mount is taking the fallback" is the whole value of the warning. `/health` SHALL expose the same field the note path's fallback exposes, under the same name and with the same meaning, so one field answers for both write paths.

An abandoned or killed upload in fallback mode SHALL leave its staged file for the existing 24-hour sweep of `.transfer-tmp/.tmp-*`, which is retained for exactly this reason as well as for pre-change litter.

#### Scenario: The flag is off and the filesystem cannot stage without a name

- **WHEN** the publication probe runs for a root whose filesystem rejects unnamed staging, or in a container where an open descriptor cannot be published by reference, and `VAULT_ALLOW_NAMED_STAGING_FALLBACK` is unset
- **THEN** the probe SHALL raise the unsupported-filesystem error naming the missing capability **and** naming that flag
- **AND** no upload token SHALL be minted and no body SHALL be staged or published for that root
- **AND** the refusal SHALL NOT fall back to staging under a name

#### Scenario: The flag is on and the filesystem cannot stage without a name

- **WHEN** the same root is probed with `VAULT_ALLOW_NAMED_STAGING_FALLBACK` set
- **THEN** the probe SHALL select named staging for that root rather than refusing, after establishing that the primitives the fallback needs — an exclusive, non-symlink-following creation in the staging directory, a hard link within the root, a flush of the staged file, and a flush of a directory descriptor — all work there
- **AND** an upload on that root SHALL stage in `.transfer-tmp` under a name and publish out of it
- **AND** a root whose probe selected named staging SHALL be refused if any of those primitives fails, rather than accepting a body it cannot publish

#### Scenario: The fallback publish is still no-clobber

- **WHEN** a no-clobber upload publishes in named-staging mode and a file already exists at the destination
- **THEN** the publish SHALL fail on the destination already existing, exactly as the by-descriptor publication does
- **AND** the existing file SHALL be unchanged
- **AND** the claim SHALL be released, since nothing was published

#### Scenario: The fallback announces itself once, on first exercise

- **WHEN** `VAULT_ALLOW_NAMED_STAGING_FALLBACK` is set and the process then serves several uploads on a root whose probe selected named staging
- **THEN** exactly one warning SHALL be logged for the whole process, at the first call that actually stages under a name
- **AND** setting the flag, starting the process, and the probe selecting the mode SHALL each log nothing on their own

#### Scenario: `/health` reports that the fallback is in use

- **WHEN** a call has staged under a name in this process
- **THEN** `/health` SHALL report the named-staging fallback as active, in the same field and with the same meaning as the note path's fallback uses
- **AND** while nothing has staged under a name — including where the flag is set but every root supports unnamed staging — that field SHALL report it as inactive

#### Scenario: A named staging file survives an abandoned upload

- **WHEN** an upload in named-staging mode is abandoned mid-stream, or the process is killed while one is in flight
- **THEN** the staged file MAY remain in `.transfer-tmp`
- **AND** the existing sweep of `.transfer-tmp/.tmp-*` files older than 24 hours SHALL collect it

#### Scenario: The staged name is substituted while the body streams

- **WHEN** another process replaces the named staging file of an in-flight fallback upload before the publish
- **THEN** a substitution observable at the identity check that immediately precedes the publish SHALL be refused, and the substituted file SHALL be left in place rather than unlinked
- **AND** a substitution landing between that check and the publishing link or rename SHALL NOT be specified as prevented — the check narrows the window to one syscall exactly as it does in the unnamed mode's transient-name case, and closing it is not achievable
- **AND** that residual is what the flag hands to the operator: reaching it needs write access to an owner-only directory beneath the vault root that no agent, capability or vault tool can reach, held by a process that could rewrite the destination directly

### Requirement: One flag governs the named-staging fallback on both write paths

The named-staging fallback SHALL be governed by exactly one operator flag for the note path and the transfer path together — `VAULT_ALLOW_NAMED_STAGING_FALLBACK`, default off — and an implementation SHALL NOT split it into a per-path knob. An operator meets the missing capability on both paths for one reason, a filesystem that cannot allocate an unnamed inode; two knobs would let a deployment run with a working `create_note` and a refusing upload, which is a state nobody chose and nobody can diagnose from either symptom alone.

The flag's *definition* — the settings field, the environment variable it reads, and its default — belongs to the note path's fallback, and this capability consumes it without redefining it. Where the transfer fallback ships **before** that definition exists, this change SHALL introduce the field under exactly that name and exactly that default, so that whichever lands first, the other finds the flag it expects and the two never diverge into two settings.

#### Scenario: One flag, both paths

- **WHEN** an operator sets `VAULT_ALLOW_NAMED_STAGING_FALLBACK` on a deployment whose filesystem rejects unnamed staging
- **THEN** both the note path's no-clobber writes and the transfer path's publications SHALL take their named-staging fallback
- **AND** no second flag SHALL be required, offered or consulted to enable either one

#### Scenario: The default is unchanged refusal

- **WHEN** the flag is not set
- **THEN** both paths SHALL refuse on a filesystem that cannot stage without a name, each with an error naming the flag
- **AND** neither path SHALL stage under a name

### Requirement: Transfer publication refuses a destination on another mount, before the body where the boundary already exists and inside the publish gate where it appears later

Transfer publication SHALL establish that the destination parent directory is on the **same mount** as the staging directory, and SHALL refuse the transfer with an error naming the mount boundary when it is not. The check SHALL run at two points, and what each can promise about *timing* differs and SHALL NOT be blurred: a boundary that is already present when the capability is minted or when a fetch is about to begin SHALL be refused **before any body is read, staged or published**; a boundary that appears **afterwards** SHALL be refused inside the publish gate, before the link or rename — which is still pre-publication, so nothing is written and an upload claim is released, but it runs only after the body may already have streamed in full.

The first check has to happen before the bytes move because the failure is otherwise terminal and late. Uploads stage in a root-level staging directory and publish from there into the destination with a hard link (no-clobber) or a replacing rename (overwrite), and `link(2)` and `rename(2)` both refuse to cross a mount boundary with `EXDEV`. The publication probe links root→root and is cached per root, so it cannot see a destination on another mount; without the mint-time check the refusal arrives only after the whole body has been streamed, which is exactly what the in-gate check still costs in the one case the mint could not have seen.

**The final component SHALL be checked as well as the parent, for a publication that replaces an existing file.** A mount can be established on the destination *file* rather than on its directory; the parent then compares equal to the staging directory and the replacing rename still refuses, with `EBUSY` rather than `EXDEV`. That check SHALL read the final component's mount identity without following a symbolic link, SHALL run at the same two points as the parent check, and SHALL apply only where the publication replaces an existing file — a no-clobber publication onto an existing name is refused as an already-existing target whatever the mount layout, and that is the accurate refusal. A final component whose mount identity cannot be read SHALL NOT be treated as a boundary.

**The comparison SHALL be of mount identity, not of `st_dev`.** A bind mount of a directory of the *same* filesystem, mounted beneath the vault root, presents the same `st_dev` as the staging directory and still refuses a link or a rename across itself, so an `st_dev` comparison passes and the publish fails `EXDEV` after the body has streamed. Mount identity SHALL be read with `statx(2)`'s mount-id field.

Both sides of a comparison SHALL be read within the same call and compared immediately. A mount id SHALL NOT be recorded at mint time and compared against a reading taken later, because a mount id may be reused once its mount is gone; the check is performed twice — each time against a freshly read pair — rather than once and remembered.

Where the destination parent does not exist yet, the check SHALL be made against the deepest ancestor of the destination that does exist, since a directory created beneath it is created on that ancestor's mount. A mount established beneath the vault root after the first check is what the second check exists to catch.

The check SHALL run at both points at which a publication is committed to: when the capability is minted (`request_upload`) or when the fetch is about to begin (`import_from_url`), so that a person is never handed a link that cannot be redeemed and no body is fetched that cannot land; and again inside the publish gate, after the authoritative destination lookup and before the link or rename, so that a mount appearing between the two is refused rather than published into. Only the first of those spares the body: an upload whose destination was on the staging mount at mint and is not by the time the gate runs has already streamed its whole body when the in-gate check refuses it. That is the accepted cost of a check that cannot be made before a boundary exists, and the requirement SHALL NOT be summarised as though every mount refusal precedes the body.

An environment that cannot report a mount id SHALL cause the publication to be refused with an error naming the missing capability. It SHALL NOT fall back to comparing `st_dev`, and SHALL NOT proceed on the assumption that the mounts match and let the errno decide after the body has streamed.

This applies to transfer publication only. Note writes stage in the destination's own directory and publish with a same-directory rename or link, so they never cross a mount boundary and SHALL NOT be made to perform this check.

#### Scenario: A same-filesystem bind mount is detected

- **WHEN** the destination parent is a bind mount of a directory of the same filesystem as the staging directory, so the two report identical `st_dev`
- **THEN** the transfer SHALL be refused
- **AND** the refusal SHALL NOT depend on the two directories reporting different `st_dev`

#### Scenario: A boundary already present at mint or fetch start is refused before any body moves

- **WHEN** a capability is minted for, or a fetch is about to begin against, a destination on a different mount from the staging directory
- **THEN** the mint or the fetch SHALL be refused with an error naming the mount boundary
- **AND** no body SHALL have been read, staged or published
- **AND** no upload link SHALL be handed out that could only ever fail at publication

#### Scenario: A mount appears between the mint and the publish

- **WHEN** the destination parent is on the staging directory's mount at mint time and a separate mount has been established at or above it by the time the publish gate runs
- **THEN** the publish SHALL be refused before the link or rename is attempted
- **AND** the destination SHALL hold its prior content
- **AND** the refusal MAY come after the whole body has been streamed, since the boundary did not exist when the mint-time check ran — the refusal is pre-publication, not pre-body
- **AND** the upload claim SHALL be released to `pending`, because nothing was published

#### Scenario: The destination parent does not exist yet

- **WHEN** the destination's parent directory does not exist at the time of the check
- **THEN** the check SHALL be made against the deepest existing ancestor of the destination

#### Scenario: The destination file is itself a mount point

- **WHEN** a capability that replaces an existing file is minted for, or published to, a path whose final component is itself a mount point, so that its parent directory is on the staging directory's mount
- **THEN** the transfer SHALL be refused with the mount-boundary error, at mint where the mount is already present and inside the publish gate where it appears afterwards
- **AND** where such a mount is reached by the replacing rename regardless, the resulting `EBUSY` SHALL be reported as a mount boundary **only** after a fresh check establishes that cause, since `EBUSY` has other sources and naming a mount that is not there sends an operator after the wrong thing
- **AND** the existing file SHALL be left unchanged and the claim released

#### Scenario: Mount identity cannot be read

- **WHEN** the kernel or the container cannot report a mount id for a directory descriptor
- **THEN** the transfer SHALL be refused with an error naming the missing capability
- **AND** SHALL NOT fall back to an `st_dev` comparison
- **AND** SHALL NOT stream a body and let the publish errno decide

#### Scenario: An ordinary single-mount vault is unaffected

- **WHEN** the staging directory and the destination parent are on the same mount, as they are on a vault that contains no nested mount
- **THEN** the check SHALL pass and the transfer SHALL proceed exactly as it does today

### Requirement: A mount refusal on the upload route is distinguishable from a filesystem refusal

The upload route SHALL answer a mount-boundary refusal with a body that names a mount boundary, distinct from the body it uses when the filesystem cannot perform atomic no-clobber publication. Collapsing the two states that the vault's filesystem lacks a capability it has, and is flatly false for a capability minted with `overwrite`, which does not use the no-clobber publication at all. Because the mount-boundary error is a *subtype* of the unsupported-filesystem error, the handler that answers it SHALL be ordered before the general one, or it can never be reached.

That body SHALL NOT contain the destination path or any other vault path: the route is unauthenticated beyond the bearer capability. Precision about *which* path and *which* side of the boundary comes from the authenticated surfaces — the mint tools' error text and `check_upload`. Every unknown, expired, consumed or otherwise unusable token SHALL continue to answer the uniform 404; this refusal is reached only for a token that was valid and whose destination stopped being publishable after it was minted.

#### Scenario: A mount appears between the mint and the redemption

- **WHEN** a valid upload capability is redeemed and the destination has come to sit on a different mount, or its final component has become a mount point
- **THEN** the route SHALL answer with the mount-boundary body rather than the unsupported-filesystem body
- **AND** that body SHALL contain no vault path
- **AND** the claim SHALL be released, so the same link may be redeemed again once the mount is gone

### Requirement: A transfer capability records the actor that minted it
Minting a transfer capability SHALL record the denormalised actor of the minting request — its kind (`api_key` or `oauth`), its label (the key's name or the OAuth client's name) and its reference (the key's `omcp_` prefix or the `client_id`) — on the token row, and the redemption route SHALL copy those three values onto the `usage_logs` row it writes. The label SHALL be taken from the request-scoped actor the authentication middleware already bound, inside the mint's own transaction, so no path gains a database query for it. It SHALL be read through the **same single reader** the MCP tool-call log uses, so the two writers cannot drift in shape or truncation.

A redemption request carries a capability, not a credential, so the route has no request-scoped actor to read and attributed its usage rows by join alone — through `transfer_tokens.key_id` or through `transfer_tokens.oauth_token_id` → `oauth_clients`. Both joins go null on the operator's most urgent path: deleting an OAuth client cascades its tokens, and the panel nulls a key's `usage_logs.key_id` before deleting the key. The rows this destroys are the ones where bytes entered or left the vault, which are exactly the rows an operator reviewing a suspect credential opens the page to read.

The recorded actor is a **snapshot**, never re-derived: it names what the credential was called when the capability was minted. It is display and audit only and SHALL NOT be read for any authorization decision; the credential re-validation, the root check and the publish gate are unchanged by its presence.

#### Scenario: An upload's usage row survives deletion of its OAuth client

- **WHEN** a capability minted by an OAuth-authenticated request is redeemed, and the OAuth client is deleted afterwards
- **THEN** the `usage_logs` row for that redemption SHALL still render the client's name and `client_id`
- **AND** SHALL NOT render as an unknown actor

#### Scenario: A download's usage row survives deletion of its API key

- **WHEN** a capability minted by an API key is redeemed, and the panel then nulls that row's `key_id` and deletes the key
- **THEN** the `usage_logs` row SHALL still render the key's name and `omcp_` prefix

#### Scenario: The label costs no additional query

- **WHEN** `request_upload` or `request_download` mints a capability
- **THEN** the number of database statements issued SHALL be unchanged from before this requirement

#### Scenario: The label is a snapshot, not a lookup

- **WHEN** the minting credential is renamed between the mint and the redemption
- **THEN** the usage row SHALL carry the name the credential had at mint time

#### Scenario: One reader, so mint and log cannot disagree

- **WHEN** the mint records an actor and a tool call in the same request logs one
- **THEN** both SHALL produce the same kind, label and reference, including the same truncation to the stored widths

#### Scenario: A mint with no request-scoped actor records none

- **WHEN** a capability is minted on a path that carries no request-scoped actor
- **THEN** the three values SHALL be left unset rather than inferred from any other row
- **AND** the redemption's usage row SHALL keep the row shape it had before this requirement

#### Scenario: A pre-migration transfer usage row is not relabelled

- **WHEN** a transfer-route `usage_logs` row written before this scheme is rendered
- **THEN** it SHALL be attributed by the existing credential joins, and rendered as an unattributable row when those joins resolve to nothing
- **AND** nothing SHALL write an actor onto it after the fact

#### Scenario: The recorded actor is never consulted for authorization

- **WHEN** a capability whose recorded actor names a credential that has since been deleted is redeemed
- **THEN** the redemption decision SHALL be made by the credential re-validation and the root check exactly as before, and the recorded actor SHALL affect only what is written to the usage log

### Requirement: `delete_file` confirms the caller's vault assignment before it deletes
`delete_file` SHALL re-read the caller's vault assignment from the database immediately before it soft-deletes or unlinks, and SHALL refuse when the assignment no longer equals the root the request bound at admission — when it differs, when it has been cleared, when the user row is gone, or when the user is no longer active. On refusal nothing SHALL be deleted and no trash entry SHALL be created.

`delete_file` does not publish through the shared mutation target the note tools use: it resolves its root separately and walks from an independently opened root descriptor. The structural refusal that covers the note tools therefore does not reach it, and its confirmation is stated here rather than left as an unremarked gap — a destructive operation in a vault the caller has been reassigned away from is the same defect as a write into one.

Single-user mode has no user row to re-read and SHALL be unaffected.

#### Scenario: Reassignment between admission and deletion

- **WHEN** an administrator reassigns the caller to a different vault root after the request was admitted and before `delete_file` reaches its delete
- **THEN** the call SHALL be refused with a tool error
- **AND** the file in the former vault SHALL be unchanged, and no `.trash` entry SHALL exist for it

#### Scenario: Unassignment between admission and deletion

- **WHEN** the caller's vault assignment is cleared in that same window
- **THEN** the call SHALL be refused and nothing SHALL be deleted

#### Scenario: An unchanged assignment deletes as before

- **WHEN** the assignment is unchanged
- **THEN** `delete_file` SHALL behave exactly as it does today, soft-deleting by default and unlinking on `permanent=True`

#### Scenario: Single-user mode is unaffected

- **WHEN** `delete_file` runs in single-user mode
- **THEN** it SHALL issue no assignment re-read and SHALL behave exactly as before

### Requirement: The transfer publish gate is not weakened to the optimistic form
The transfer routes and `import_from_url` SHALL continue to hold their credential and user rows `SELECT … FOR UPDATE` across the filesystem publish, and SHALL continue to compare the database's current root against the root captured at mint. The optimistic re-read adopted for the note mutation tools SHALL NOT replace either gate.

Those paths hold a token row and an already-open session, and their publish is a bounded byte stream, so the locked gate costs them little and buys linearizability. The note tools have none of those properties, which is why they take the weaker guarantee — a difference in what each path can afford, not a difference of opinion about what is correct.

#### Scenario: The upload route still publishes under held locks

- **WHEN** `PUT /transfer/upload` publishes
- **THEN** the token, credential and user rows SHALL be held `FOR UPDATE` from before the publish until the completion and the usage row commit

#### Scenario: `import_from_url` still locks its own identity

- **WHEN** `import_from_url` publishes fetched bytes
- **THEN** it SHALL hold its credential and user rows `FOR UPDATE` across the publish and SHALL re-check the database's current root against the root captured when the tool started

### Requirement: The fallback's staging discard distinguishes a published name from a disappeared one

The named-staging fallback's discard SHALL be told whether the publication landed, and SHALL treat an absent staging name after a successful publish as the ordinary consumed case — silent, because the overwrite publish is a rename that consumes the name — reserving the "staging name disappeared before its write was published" warning for a name that vanished while the write had genuinely not published. Every discard call site on the transfer path SHALL pass the publication's actual outcome, including the outer cleanup reached when a failure *after* publication (a post-publication directory flush failing, correctly classified as a post-publish failure with the claim stranded) unwinds the stream: hardcoding "not published" there makes the warning false in exactly the doubly-degraded corner where an operator most needs to trust it, and a false disappearance warning trains an operator to ignore the true one, which is the substitution signal. The published-state record SHALL be initialized before the staging name can exist, so that a failure at any point after staging — the body drain, the identity `fstat`, the mode change, the payload flush — finds it present and false rather than absent: the cleanup consulting a record that is not yet bound would replace the original failure with a name error and skip the guarded discard entirely. The inode-guarded unlink direction is unchanged: a present name still referring to the staged inode is removed quietly, a substituted or unidentifiable name is left in place and logged, published or not.

#### Scenario: A fallback upload publishes and a post-publication flush fails

- **WHEN** a named-fallback upload's overwrite publish lands and a subsequent post-publication directory flush raises
- **THEN** the failure SHALL remain classified post-publish and the claim SHALL strand exactly as specified elsewhere
- **AND** the outer cleanup's discard SHALL be invoked with the published outcome true and SHALL log no warning about the staging name having disappeared, whether the name was consumed by the publish or a matching residual name remains to be removed quietly

#### Scenario: A failure after the staged identity was recorded cleans up exactly as before

- **WHEN** a named-fallback upload fails after its staging name exists and its identity `fstat` has succeeded, but before publication — an over-cap body, a disconnect, a failing `fchmod` or payload flush
- **THEN** the original failure SHALL propagate unmasked and the claim handling SHALL be the pre-publication behavior specified elsewhere
- **AND** the discard SHALL run with the published outcome false, removing a name that still refers to the staged inode and warning if the name has disappeared

#### Scenario: A failing identity `fstat` leaves the name in place

- **WHEN** a named-fallback upload's identity `fstat` itself fails, so no staged identity was ever recorded
- **THEN** the original failure SHALL propagate unmasked with the published outcome false
- **AND** the discard SHALL be invoked with no recorded identity and SHALL remove nothing — the name is left in place with the cannot-confirm warning, because with no identity nothing can prove the name still refers to the staged inode, and unlinking it could destroy a concurrent substitute (the destructive-write class the guard exists to refuse)

#### Scenario: A pre-publication disappearance still warns

- **WHEN** a named-fallback upload fails before publication and its staging name is found absent at discard
- **THEN** the discard SHALL log the disappearance warning exactly as it does today
