## ADDED Requirements

### Requirement: Every below-root directory descriptor comes from one kernel-enforced beneath-root lookup

The anchored filesystem layer SHALL obtain every directory descriptor below the vault root with a **single** kernel-enforced beneath-root lookup — `openat2(2)` carrying `RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS` — and SHALL NOT produce that descriptor by opening one path component at a time, so that no rename of an intermediate directory *during* the lookup can yield a descriptor outside the root. This governs every caller of the layer: the transfer publish's up-front and in-gate destination walks, the staging-directory open, the trash open, `delete_file`, and every note mutation that anchors through it.

`RESOLVE_NO_XDEV` SHALL NOT be set: a mount point beneath the vault root is a supported deployment and is still beneath the root, and containment is what `RESOLVE_BENEATH` enforces. The lexical guard that refuses `..`, absolute paths and NUL bytes SHALL remain in front of the lookup, so those are refused with a message naming the offending path rather than by an errno.

Errno mapping SHALL distinguish the three kinds of failure, because they tell an operator to do three different things: a symbolic-link or non-directory component SHALL raise the traversal error; an attempted escape of the root SHALL raise the traversal error as a **containment** refusal and SHALL NOT be reported as an unsupported filesystem; and an unavailable syscall (`ENOSYS`, `EPERM`, or an unrecognised `struct open_how` size) SHALL raise the unsupported-filesystem error naming `openat2`, the kernel version that introduced it, and the container seccomp profile as the two causes. A resolution the kernel reports as raced (`EAGAIN`) SHALL be retried a bounded number of times and then refused; it SHALL NOT be reported as success.

Availability SHALL be enforced twice: a **read-only** startup probe that creates nothing SHALL terminate the process with a message naming those causes when the syscall is unavailable, and the call site SHALL raise the unsupported-filesystem error if it encounters the same condition anyway. There SHALL be **no** fallback to a per-component walk on any path, under any condition.

Creating a missing directory MAY still descend one component at a time, because the syscall cannot create intermediate directories; but no descriptor produced by that creation descent SHALL be returned to a caller or written through. The descriptor a caller receives SHALL always come from a fresh single beneath-root lookup performed **after** the creation completes, including where the creation is deferred to first use of a validated target's parent.

#### Scenario: An ancestor is renamed out of the vault during the lookup

- **WHEN** a path `A/B/note.md` is being resolved to a parent descriptor and another process renames `<vault>/A` to a directory outside the vault root while the resolution is in progress
- **THEN** the lookup SHALL either return a descriptor for a directory beneath the vault root or fail
- **AND** SHALL NOT return a descriptor for a directory outside the root
- **AND** no file outside the vault root SHALL be created, read or modified by the call

#### Scenario: A deferred parent creation is followed by a fresh beneath-root lookup

- **WHEN** a write names a path whose parent directory does not exist, and the parent is created on first use of the validated target
- **THEN** the descriptor the write acts through SHALL be the result of a beneath-root lookup performed after the creation
- **AND** SHALL NOT be a descriptor retained from the creation descent

#### Scenario: A symlinked component is still refused with the traversal error

- **WHEN** any component of the path below the root is a symbolic link at lookup time
- **THEN** the lookup SHALL raise the traversal error naming the component
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

### Requirement: Staged transfer bytes and their publication are made durable

The transfer publish path SHALL flush the staged payload to durable storage before the publish gate is entered, and SHALL flush the destination directory after publication, so that a crash cannot leave a transfer recorded `completed` whose file at the bound path is absent, truncated, or does not match the recorded `sha256`. This SHALL apply to `PUT /transfer/upload` and to `import_from_url`, which share the same streaming publish.

The payload flush SHALL happen after the body has been fully received and before the pre-publication gate is opened, so that a flush of up to `MAX_FILE_WRITE_BYTES` never runs while the gate's `SELECT … FOR UPDATE` locks are held, and SHALL NOT block the event loop for its duration. A failure of the payload flush SHALL be treated as pre-publication: nothing SHALL be published, the staged bytes SHALL be discarded, and an upload claim SHALL be released to `pending`.

The directory flush SHALL happen after the publication has been recorded and before the completion is committed. A failure of the directory flush SHALL therefore be classified as post-publication and SHALL surface as the post-publication failure type — never as a generic `OSError`, which the upload route reads as "nothing was published" and answers by releasing a replayable claim over a path that already holds the file. An upload whose directory flush failed SHALL remain `claimed`, SHALL NOT be reported `completed` by `check_upload`, and SHALL NOT be replayable. When the call created directories on the way to the destination, each created directory's parent SHALL be flushed as well, outward to the first directory that already existed.

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

#### Scenario: `import_from_url` gets the same durability

- **WHEN** `import_from_url` fetches a body and publishes it
- **THEN** the payload SHALL have been flushed before its gate is entered and the destination directory SHALL be flushed after publication, with the same failure classification as an upload

### Requirement: Transfer staging holds no directory entry

An upload's staged bytes SHALL be held for the whole of the streaming window in a file with **no directory entry**, so that nothing in the staging directory can be observed, replaced or raced, and so that abandoned bytes are reclaimed by the kernel rather than left for a sweep. Staging SHALL allocate the unnamed file in the staging directory beneath the vault root, which SHALL continue to exist and to be held owner-only, since the directory is what selects the filesystem the inode lives on.

The no-clobber publish SHALL publish that inode **by descriptor**, so what lands at the destination is provably the inode this call wrote and no name is consulted. The overwrite publish SHALL NOT be required to be nameless, because a replacing rename has no by-descriptor form; instead it SHALL create a name for the staged inode only **inside the publish gate**, immediately before the fingerprint check and the rename, and only in the staging directory — never in the destination directory. That name SHALL be created no-clobber, retried under a fresh name if it is already taken, verified to still refer to the staged inode immediately before the rename, and on cleanup unlinked **only** while it still refers to that inode, otherwise left in place and logged.

A filesystem or container that cannot stage without a name, or cannot publish by descriptor, SHALL cause the transfer to be refused with an error naming the missing capability, and SHALL NOT cause a fallback to publishing whatever a staging name refers to.

#### Scenario: Nothing is observable while a body streams

- **WHEN** an upload body is streaming
- **THEN** the staging directory SHALL contain no directory entry for the bytes being staged

#### Scenario: An abandoned upload leaves nothing behind

- **WHEN** an upload is abandoned mid-stream, or the process is killed while one is in flight
- **THEN** the staged bytes SHALL be reclaimed without any file remaining in the staging directory for a later sweep to remove

#### Scenario: The overwrite path's name exists only inside the gate

- **WHEN** an overwrite upload publishes
- **THEN** a name for the staged inode SHALL exist only between the publish gate's acquisition of its locks and the completion of its rename
- **AND** that name SHALL be in the staging directory, not in the destination directory

#### Scenario: The transient overwrite name is substituted

- **WHEN** another process replaces the transient staging name with a different file after it is created and before the rename
- **THEN** the upload SHALL be refused, the destination SHALL hold its prior content, and the substituted file SHALL be left in place rather than unlinked

#### Scenario: Unnamed staging or by-descriptor publication is unavailable

- **WHEN** the vault filesystem cannot allocate a file without a directory entry, or the container cannot publish an open descriptor by reference
- **THEN** the transfer SHALL be refused with an error naming the unsupported capability
- **AND** SHALL NOT publish by staging name instead

## MODIFIED Requirements

### Requirement: Upload endpoint claims first, streams within the cap, publishes atomically to the pre-committed path

`GET /transfer/upload` SHALL serve a static self-contained HTML page (no external assets, nonce-based CSP) whose script reads the token from the URL fragment, calls `GET /transfer/upload/info` with the bearer header to display the bound path, the **mode** — whether the upload creates a new file or replaces the file already at that path, taken from the `overwrite` field of the info payload — the cap and the expiry, labelling the replace case destructively on both the action control and the status copy so the person pressing it knows the existing file will be lost, and sends the chosen file as the raw body of `PUT /transfer/upload` with the bearer header. `PUT /transfer/upload` SHALL: (1) atomically transition the token from `pending` to `claimed` in a committed statement conditioned on `state='pending' AND expires_at > now()`, returning 404 if no row transitions, before reading any body byte; (2) re-validate identity (exact predicates: active, unexpired, write-capable credential; active user), vault root, and path from the token row; (3) reject early on a `Content-Length` above `MAX_FILE_WRITE_BYTES`; (4) stream the body — under a `TRANSFER_MAX_CONCURRENT_UPLOADS` semaphore, a deadline of `min(expires_at, claimed_at + TRANSFER_MAX_UPLOAD_SECONDS)`, and a 30 s per-chunk idle timeout — into a file staged in the vault's staging directory through descriptor-anchored operations and holding **no directory entry**, counting bytes and aborting with HTTP 413 at cap+1; (5) compute `sha256` and MIME during the stream, relax the staged file's mode to the umask default, and **flush the staged bytes to durable storage** — all of this after the body ends and before the gate is opened, so the flush never runs under the gate's locks; (6) in a short transaction, lock (`SELECT … FOR UPDATE`) the token, credential, and user rows, re-validate identity and vault root from those locked rows, **re-check the stream deadline against the current time immediately before the publish and inside those locks** — a gate that waited past the deadline SHALL raise the deadline error and the token SHALL become `consumed`, exactly as an overrun during the body does, and nothing SHALL be written — hold the locks across the filesystem publish, and commit completion and the usage-log row in that transaction; then publish the staged inode **by descriptor** when the token was minted without `overwrite` (kernel-linearizable), or via fingerprint-checked replace when minted with `overwrite` (optimistic: `stat`+hash compare then `replace`; a writer landing inside that window is a documented limitation), returning 409 if the target appeared, changed, or is a symlink; **flush the destination directory once the publish has been recorded and before the completion is committed**; (7) move the token to `completed` with `size`, `sha256`, `mime`, `completed_at`, insert a `usage_logs` row (`tool="upload_file"`) attributed to the minting identity, and return JSON `{path, size, sha256, mime}`. On any handled failure before publication (413, 409, disconnect, malformed request) the staged bytes SHALL be discarded — releasing the unnamed inode, and unlinking any transient staging name only while it still refers to that inode — and the claim released to `pending`; on deadline or idle timeout the staged bytes SHALL be discarded and the token SHALL become `consumed`; a crash after publication SHALL leave the token `claimed` (never replayable). Publication SHALL be tracked separately from *all* trailing work: the fact that the publish succeeded SHALL be recorded before any subsequent step runs, and a failure in any of them — the destination-directory flush, the trailing discard, or the close of the destination, staging or root directory descriptor — SHALL NOT release the claim, SHALL NOT surface as a generic `OSError`, and SHALL NOT leave the token `pending`. The path SHALL never be taken from the request. An **unexpected** failure that is demonstrably before publication — an `OSError` while writing or flushing the staged body, an error opening the publish gate — SHALL also discard the staged bytes and release the claim; only a failure after the bytes are in place (`PostPublishFailure`) SHALL leave the token `claimed`.

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

### Requirement: Filesystem probes run only on write paths, and sweep stale staging

The filesystem capability probes SHALL be split by the capability they test and SHALL run only where that capability is about to be used: a **publication** probe SHALL run on `request_upload`, `import_from_url` and `PUT /transfer/upload`, and a **trash** probe (`rename` of a temp file into `.trash/`) SHALL run only on a `delete_file` soft delete. The publication probe SHALL exercise every primitive the publish depends on — a hard link within the vault root, allocation of a file with no directory entry, and publication of such a file by descriptor — so that an environment missing any of them is refused at the probe rather than at the first upload. Each SHALL be cached per vault root. No read path — `request_download`, `check_upload`, `GET|HEAD /transfer/download/info`, `GET|HEAD /transfer/download/file` — SHALL run any probe, because a probe writes. On the first publication probe per root the server SHALL remove `.transfer-tmp/.tmp-*` files whose mtime is older than 24 hours, and SHALL NOT remove newer ones; that sweep SHALL be retained for staging files left by earlier releases even though the streaming path no longer creates named staging files.

Availability of the beneath-root lookup SHALL NOT be tested by these probes: it is a property of the kernel and the container rather than of a vault root, it is identical for every root, and it is enforced by the read-only startup probe instead.

#### Scenario: A read creates nothing

- **WHEN** a read-only identity calls `request_download` against a fresh vault
- **THEN** the vault SHALL contain exactly the files and directories it contained before the call — no `.trash/`, no probe temp file, no staging directory

#### Scenario: Stale staged uploads are swept, live ones are not

- **WHEN** `.transfer-tmp/` holds one `.tmp-*` file with an mtime 25 hours old and one written moments ago, and the publication probe runs for the first time for that root
- **THEN** the old file SHALL be removed and the recent one SHALL remain

#### Scenario: A vault that cannot stage without a name is refused at the probe

- **WHEN** the publication probe runs against a root whose filesystem cannot allocate a file with no directory entry, or in a container where an open descriptor cannot be published by reference
- **THEN** the probe SHALL raise the unsupported-filesystem error naming that capability
- **AND** the transfer tools and routes for that root SHALL refuse rather than publish by staging name
