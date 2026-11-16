## MODIFIED Requirements

### Requirement: Upload endpoint claims first, streams within the cap, publishes atomically to the pre-committed path

`GET /transfer/upload` SHALL serve a static self-contained HTML page (no external assets, nonce-based CSP) whose script reads the token from the URL fragment, calls `GET /transfer/upload/info` with the bearer header to display the bound path, the **mode** — whether the upload creates a new file or replaces the file already at that path, taken from the `overwrite` field of the info payload — the cap and the expiry, labelling the replace case destructively on both the action control and the status copy so the person pressing it knows the existing file will be lost, and sends the chosen file as the raw body of `PUT /transfer/upload` with the bearer header. `PUT /transfer/upload` SHALL: (1) atomically transition the token from `pending` to `claimed` in a committed statement conditioned on `state='pending' AND expires_at > now()`, returning 404 if no row transitions, before reading any body byte; (2) re-validate identity (exact predicates: active, unexpired, write-capable credential; active user), vault root, and path from the token row; (3) reject early on a `Content-Length` above `MAX_FILE_WRITE_BYTES`; (4) stream the body — under a `TRANSFER_MAX_CONCURRENT_UPLOADS` semaphore, a deadline of `min(expires_at, claimed_at + TRANSFER_MAX_UPLOAD_SECONDS)`, and a 30 s per-chunk idle timeout — into a file staged in the vault's staging directory through descriptor-anchored operations and holding **no directory entry**, counting bytes and aborting with HTTP 413 at cap+1; (5) compute `sha256` and MIME during the stream, give the staged file the publication permissions the `file-permissions` capability defines for the bound path — captured once, from the destination directory and, for an overwrite token, from the target file, which SHALL be the file the fingerprint names by device, inode and change time or the upload SHALL be refused with HTTP 409 and nothing written, after the existing mount-boundary checks, so a destination or leaf that came to sit on another mount since the mint is still refused as a mount boundary — and **flush the staged bytes and those permissions to durable storage** in one flush — all of this after the body ends and before the gate is opened, so the flush never runs under the gate's locks; (6) in a short transaction, lock (`SELECT … FOR UPDATE`) the token, credential, and user rows, re-validate identity and vault root from those locked rows, **re-check that the destination parent is on the same mount as the staging directory, and re-check the stream deadline against the current time immediately before the publish and inside those locks** — a gate that waited past the deadline SHALL raise the deadline error and the token SHALL become `consumed`, exactly as an overrun during the body does, and nothing SHALL be written — for a create plan, refuse with HTTP 409, writing nothing, when the destination parent the gate resolved is not the directory whose permissions were captured in (5), by device and inode; the permissions SHALL NOT be captured a second time and the staged file SHALL NOT be flushed a second time — hold the locks across the filesystem publish, and commit completion and the usage-log row in that transaction; then publish the staged inode **by descriptor** when the token was minted without `overwrite` (kernel-linearizable), or via fingerprint-checked replace when minted with `overwrite` (optimistic: `stat`+hash compare then `replace`; a writer landing inside that window is a documented limitation), returning 409 if the target appeared, changed, or is a symlink; **flush the destination directory once the publish has been recorded and before the completion is committed**; (7) move the token to `completed` with `size`, `sha256`, `mime`, `completed_at`, insert a `usage_logs` row (`tool="upload_file"`) attributed to the minting identity, and return JSON `{path, size, sha256, mime}`. On any handled failure before publication (413, 409, disconnect, malformed request) the staged bytes SHALL be discarded — releasing the unnamed inode, and unlinking any transient staging name only while it still refers to that inode — and the claim released to `pending`; on deadline or idle timeout the staged bytes SHALL be discarded and the token SHALL become `consumed`; a crash after publication SHALL leave the token `claimed` (never replayable). Publication SHALL be tracked separately from *all* trailing work: the fact that the publish succeeded SHALL be recorded before any subsequent step runs, and a failure in any of them — the destination-directory flush, the trailing discard, or the close of the destination, staging or root directory descriptor — SHALL NOT release the claim, SHALL NOT surface as a generic `OSError`, and SHALL NOT leave the token `pending`. The path SHALL never be taken from the request. A destination that has come to sit on a different mount SHALL be refused before the link or rename is attempted, which is pre-publication and SHALL release the claim. An **unexpected** failure that is demonstrably before publication — an `OSError` while writing or flushing the staged body, an error opening the publish gate — SHALL also discard the staged bytes and release the claim; only a failure after the bytes are in place (`PostPublishFailure`) SHALL leave the token `claimed`.

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

#### Scenario: An upload over an existing file keeps that file's permissions

- **WHEN** an overwrite token is minted for a file with mode 0600 and no ACL, or for a file whose POSIX access ACL grants a named group `rw-` under `mask::rw-`, both with a gid the server holds, and the upload completes
- **THEN** the published file's permission bits (`st_mode & 0o777`), `st_gid` and `system.posix_acl_access` value (bytes, or `ENODATA`) SHALL equal the replaced file's

#### Scenario: A new upload gets what an ordinary new file gets

- **WHEN** a no-overwrite upload completes in a directory whose default ACL is `u::rwx,g::rwx,g:<group>:rwx,m::rwx,o::r-x`, while `.transfer-tmp` inherits a different default ACL
- **THEN** the published file's permission bits, `st_gid` and `system.posix_acl_access` bytes SHALL equal those of a control file created in the same directory with `open(O_CREAT, 0o666)`

#### Scenario: The destination folder was replaced during the upload

- **WHEN** a no-overwrite upload's destination folder is renamed away and recreated after the upload captured its permissions and before the publish gate resolves it
- **THEN** the upload SHALL be refused with HTTP 409, nothing SHALL be published, and the token SHALL be `pending` again

