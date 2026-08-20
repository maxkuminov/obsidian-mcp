## MODIFIED Requirements

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

### Requirement: Upload endpoint claims first, streams within the cap, publishes atomically to the pre-committed path

`GET /transfer/upload` SHALL serve a static self-contained HTML page (no external assets, nonce-based CSP) whose script reads the token from the URL fragment, calls `GET /transfer/upload/info` with the bearer header to display the bound path, the **mode** — whether the upload creates a new file or replaces the file already at that path, taken from the `overwrite` field of the info payload — the cap and the expiry, labelling the replace case destructively on both the action control and the status copy so the person pressing it knows the existing file will be lost, and sends the chosen file as the raw body of `PUT /transfer/upload` with the bearer header. `PUT /transfer/upload` SHALL: (1) atomically transition the token from `pending` to `claimed` in a committed statement conditioned on `state='pending' AND expires_at > now()`, returning 404 if no row transitions, before reading any body byte; (2) re-validate identity (exact predicates: active, unexpired, write-capable credential; active user), vault root, and path from the token row; (3) reject early on a `Content-Length` above `MAX_FILE_WRITE_BYTES`; (4) stream the body — under a `TRANSFER_MAX_CONCURRENT_UPLOADS` semaphore, a deadline of `min(expires_at, claimed_at + TRANSFER_MAX_UPLOAD_SECONDS)`, and a 30 s per-chunk idle timeout — to a temporary file created in the target directory through descriptor-anchored operations (`O_CREAT|O_EXCL|O_NOFOLLOW`, mode 0600), counting bytes and aborting with HTTP 413 at cap+1; (5) compute `sha256` and MIME during the stream; (6) in a short transaction, lock (`SELECT … FOR UPDATE`) the token, credential, and user rows, re-validate identity and vault root from those locked rows, hold the locks across the filesystem publish, and commit completion and the usage-log row in that transaction; then publish via hard-link no-clobber when the token was minted without `overwrite` (kernel-linearizable), or via fingerprint-checked replace when minted with `overwrite` (optimistic: `stat`+hash compare then `replace`; a writer landing inside that window is a documented limitation), returning 409 if the target appeared, changed, or is a symlink; (7) move the token to `completed` with `size`, `sha256`, `mime`, `completed_at`, insert a `usage_logs` row (`tool="upload_file"`) attributed to the minting identity, and return JSON `{path, size, sha256, mime}`. On any handled failure before publication (413, 409, disconnect, malformed request) the temporary file SHALL be removed and the claim released to `pending`; on deadline or idle timeout the temporary file SHALL be removed and the token SHALL become `consumed`; a crash after publication SHALL leave the token `claimed` (never replayable). Publication SHALL be tracked separately from *all* trailing cleanup: the fact that `link`/`replace` succeeded SHALL be recorded before any subsequent step runs, and a failure in any of them — the trailing temp unlink, or the close of the destination, staging or root directory descriptor — SHALL be logged and SHALL NOT release the claim, SHALL NOT surface as a generic `OSError`, and SHALL NOT leave the token `pending`. The path SHALL never be taken from the request. An **unexpected** failure that is demonstrably before publication — an `OSError` while writing the staged body, an error opening the publish gate — SHALL also remove the temporary file and release the claim; only a failure after the bytes are in place (`PostPublishFailure`) SHALL leave the token `claimed`.

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

#### Scenario: An overwrite link says so at the consent step

- **WHEN** the upload page loads for a token minted with `overwrite=True`
- **THEN** the page SHALL state that the upload replaces the existing file at the bound path, and its action control and status copy SHALL be labelled destructively; for a token minted with `overwrite=False` the page SHALL state that it creates a new file

### Requirement: `check_upload` tool

`check_upload(upload_id)` SHALL report the outcome of an upload link honestly, never asserting more about the vault than the token row proves, and SHALL return one of: `completed` (with `path`, `size`, `sha256`, `mime`, `completed_at`); `uploading` for a claimed token still inside its stream deadline, naming that deadline; `unknown` for a claimed token past it, stating that the bytes may already be in the vault because a publish can succeed and still fail to record its completion, and directing the caller to `list_files` / `read_file` the bound path before re-minting; `revoked` for a `pending` token whose minting credential no longer satisfies the redemption predicates or whose vault root has changed; `expired`; or `pending`. The stream deadline SHALL be `min(expires_at, claimed_at + TRANSFER_MAX_UPLOAD_SECONDS)` as an absolute UTC instant, computed by the same helper the upload route enforces it with **and measured against the same clock**: the route SHALL enforce that instant directly rather than converting it to a monotonic value, so that a realtime clock step cannot make the route and this tool describe different instants. A claimed or consumed token SHALL NEVER be reported as unused, whatever its expiry — "never used" SHALL be reachable only for a `pending` row — and a consumed token SHALL additionally state that nothing was published, since the deadline and idle-timeout paths abort before `publish`. For `pending` and `claimed` rows the tool SHALL re-check `resolve_identity_ok(need_write=True)` and `resolve_root_ok` against the database inside the session that read the row, because redemption decides usability from a strictly larger predicate than the row's state; `completed` rows SHALL NOT be re-checked. It SHALL report `not found` for an `upload_id` minted by a different identity (or, in multi-user mode, a different user). Precise status is permitted here and nowhere else: this side is authenticated and identity-scoped, unlike the public routes' uniform 404. The argument SHALL be validated against the exact shape `upload_id`s are minted with (22 characters of the URL-safe base64 alphabet) **before** it is written to `usage_logs`: an off-shape value SHALL be logged as a fixed `<invalid>` marker, SHALL NOT reach the database lookup, and SHALL return `not found`. No branch SHALL write a token, a credential, or any other secret into `usage_logs`.

#### Scenario: A misused argument never reaches the log

- **WHEN** `check_upload` is called with a whole `…/transfer/upload#<token>` URL, or with the token itself, in place of the handle
- **THEN** the tool SHALL return `not found` and the `usage_logs` row SHALL record `upload_id` as `<invalid>`, containing no part of the supplied value

#### Scenario: Status transitions

- **WHEN** `check_upload` is called before an upload, after a completed upload, and after expiry of an unused token
- **THEN** it SHALL return `pending`, then `completed` with the file's `sha256`, then `expired`

#### Scenario: Cross-identity lookup

- **WHEN** identity B calls `check_upload` with an `upload_id` minted by identity A
- **THEN** the tool SHALL return `not found`

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
