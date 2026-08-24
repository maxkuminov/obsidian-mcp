## ADDED Requirements

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

#### Scenario: The transient overwrite name is substituted before the identity check

- **WHEN** another process replaces the transient staging name with a different file after it is created and before the identity check that immediately precedes the rename
- **THEN** the upload SHALL be refused, the destination SHALL hold its prior content, and the substituted file SHALL be left in place rather than unlinked

#### Scenario: The transient overwrite name is substituted after the identity check

- **WHEN** the substitution lands in the interval between that identity check and the rename itself
- **THEN** the refusal SHALL NOT be guaranteed — the identity check narrows the window to one syscall and does not close it — and this SHALL be recorded as an accepted residual rather than specified as prevented
- **AND** reaching it SHALL require write access to the owner-only staging directory, which is the same access that permits editing the destination directly and is therefore outside the threat this change addresses

#### Scenario: Unnamed staging or by-descriptor publication is unavailable

- **WHEN** the vault filesystem cannot allocate a file without a directory entry, or the container cannot publish an open descriptor by reference
- **THEN** the transfer SHALL be refused with an error naming the unsupported capability
- **AND** SHALL NOT publish by staging name instead

### Requirement: Transfer publication refuses a destination on another mount, before the body where the boundary already exists and inside the publish gate where it appears later

Transfer publication SHALL establish that the destination parent directory is on the **same mount** as the staging directory, and SHALL refuse the transfer with an error naming the mount boundary when it is not. The check SHALL run at two points, and what each can promise about *timing* differs and SHALL NOT be blurred: a boundary that is already present when the capability is minted or when a fetch is about to begin SHALL be refused **before any body is read, staged or published**; a boundary that appears **afterwards** SHALL be refused inside the publish gate, before the link or rename — which is still pre-publication, so nothing is written and an upload claim is released, but it runs only after the body may already have streamed in full.

The first check has to happen before the bytes move because the failure is otherwise terminal and late. Uploads stage in a root-level staging directory and publish from there into the destination with a hard link (no-clobber) or a replacing rename (overwrite), and `link(2)` and `rename(2)` both refuse to cross a mount boundary with `EXDEV`. The publication probe links root→root and is cached per root, so it cannot see a destination on another mount; without the mint-time check the refusal arrives only after the whole body has been streamed, which is exactly what the in-gate check still costs in the one case the mint could not have seen.

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

#### Scenario: Mount identity cannot be read

- **WHEN** the kernel or the container cannot report a mount id for a directory descriptor
- **THEN** the transfer SHALL be refused with an error naming the missing capability
- **AND** SHALL NOT fall back to an `st_dev` comparison
- **AND** SHALL NOT stream a body and let the publish errno decide

#### Scenario: An ordinary single-mount vault is unaffected

- **WHEN** the staging directory and the destination parent are on the same mount, as they are on a vault that contains no nested mount
- **THEN** the check SHALL pass and the transfer SHALL proceed exactly as it does today

## MODIFIED Requirements

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

### Requirement: Filesystem probes run only on write paths, and sweep stale staging

The filesystem capability probes SHALL be split by the capability they test and SHALL run only where that capability is about to be used: a **publication** probe SHALL run on `request_upload`, `import_from_url` and `PUT /transfer/upload`, and a **trash** probe (`rename` of a temp file into `.trash/`) SHALL run only on a `delete_file` soft delete. The publication probe SHALL exercise every primitive the publish depends on and can test from the vault root — a hard link within the vault root, allocation of a file with no directory entry, publication of such a file by descriptor, a flush of that file to durable storage, and a flush of a directory descriptor — so that an environment missing any of them is refused at the probe rather than after a body has been streamed. A filesystem that supports unnamed staging and by-descriptor publication but rejects a directory flush would otherwise pass the probe, accept a token and a body, publish the file, and only then strand the claim as a post-publication failure. Each SHALL be cached per vault root. No read path — `request_download`, `check_upload`, `GET|HEAD /transfer/download/info`, `GET|HEAD /transfer/download/file` — SHALL run any probe, because a probe writes. On the first publication probe per root the server SHALL remove `.transfer-tmp/.tmp-*` files whose mtime is older than 24 hours, and SHALL NOT remove newer ones; that sweep SHALL be retained for staging files left by earlier releases even though the streaming path no longer creates named staging files.

Availability of the beneath-root lookup SHALL NOT be tested by these probes: it is a property of the kernel and the container rather than of a vault root, it is identical for every root, and it is enforced by the read-only startup probe instead.

**What the probe covers SHALL be stated honestly.** It answers for the vault root and is cached per root, so it answers for properties the root and the destination share. It SHALL NOT be described as catching every capability the publish needs: a destination directory whose filesystem or mount differs from the root's can refuse a primitive the root accepted, and the probe cannot see it. The one such difference that is known to occur — a destination on a different mount, which refuses the link and the rename the publish depends on — is covered by the separate mount-identity check, not by this probe: that check refuses before any body is streamed where the boundary already exists at mint or fetch start, and inside the publish gate — after the body may already have streamed, but before anything is published — where it appears afterwards. Anything else is detected at the operation itself. The probe's guarantee is therefore "an environment that fails at the root is refused before any body is streamed", not "an environment that passes will publish".

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

#### Scenario: A vault that cannot flush a directory is refused at the probe

- **WHEN** the publication probe runs against a root whose filesystem refuses a flush of an open directory descriptor
- **THEN** the probe SHALL raise the unsupported-filesystem error naming that capability
- **AND** no upload token SHALL be minted for that root, so no body SHALL be streamed and published only to strand its claim on the first directory flush
