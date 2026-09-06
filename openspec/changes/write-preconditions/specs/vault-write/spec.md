## ADDED Requirements

### Requirement: The write-precondition digest is defined in exactly one place

A file's `content_hash` SHALL be the string `"sha256:"` followed by the **lowercase** hexadecimal SHA-256 digest of the **complete raw bytes of that file as stored on disk**, and every producer and consumer of the digest across this server SHALL use that one definition.

The bytes hashed are every byte of the file, in file order, with no universal-newline translation, no frontmatter stripping, no windowing, and no re-encoding — the same bytes the read-modify-write tools compare through `expected=` immediately before publication, and the same digest the transfer fingerprint already computes over a file. The digest is therefore **not** a hash of any body, section, or rendered response field: a note carrying frontmatter, or CRLF terminators, has a `content_hash` that a hash of the text returned to the caller cannot reproduce, and every docstring that mentions the field SHALL say so.

`sha256:<64 lowercase hex>` SHALL be the **only** accepted input form wherever a caller supplies a hash. Any other shape — a bare hexadecimal string, uppercase hexadecimal, surrounding whitespace, another algorithm prefix, a wrong length — SHALL be refused as `malformed_precondition`, never evaluated as a mismatch, because "the wrong kind of value was sent" and "the file changed" call for different actions from the caller.

The digest SHALL NOT be conflated with `notes_metadata.content_hash`, which hashes the universal-newline-translated text of a note and consequently differs for every file whose terminators are not LF. That column's definition SHALL NOT be changed by this capability, and because a caller-supplied hash must be canonical, handing that column's bare-hex value to a write tool SHALL be refused as malformed rather than reported as a conflict.

#### Scenario: The digest describes the file, not the returned body

- **WHEN** a note begins with a valid YAML frontmatter block and its body uses CRLF terminators
- **THEN** its `content_hash` SHALL equal the SHA-256 of the file's raw bytes, lowercase, prefixed `sha256:`
- **AND** it SHALL NOT equal the SHA-256 of the `content` field any read of that note returns

#### Scenario: Only the canonical form is accepted

- **WHEN** a write tool is invoked with `expected_hash` set to a bare 64-character hex string, to the same digest in uppercase, or to a value carrying an unknown algorithm prefix
- **THEN** the tool SHALL return a `malformed_precondition` refusal naming the accepted form, SHALL NOT write, and SHALL NOT report a mismatch

#### Scenario: The index column's value is malformed input, not a conflict

- **WHEN** a caller supplies the bare-hex value of `notes_metadata.content_hash` as `expected_hash`
- **THEN** the tool SHALL refuse as `malformed_precondition`, so the caller learns it sent the wrong digest rather than believing the file changed

### Requirement: Precondition refusals are typed and machine-parseable

Every refusal this capability introduces SHALL be delivered through the shared caller-visible refusal shape — a final, line-initial, single-line `MCP-REFUSAL {…}` sentinel appended to the tool's prose, carried into a structured tool's declared error field unchanged — and SHALL carry a `code` drawn from a closed set, together with `path` and `nothing_written: true`.

The six codes this capability contributes:

- `stale_precondition` — the supplied hash does not match the incumbent bytes. It SHALL additionally carry `current_hash`, the file's `content_hash` as the tool just read it.
- `concurrent_write` — the **in-call** comparison observed a change between this call's own read and its publication. Its existing prose (`File changed while editing: <name>`) SHALL be unchanged and the sentinel appended to it, so existing assertions keep holding while the two windows become distinguishable by code.
- `no_incumbent` — `expected_hash` was supplied where there are no incumbent bytes to bind.
- `malformed_precondition` — `expected_hash` is not in the canonical form.
- `precondition_unavailable` — an incumbent exists but is larger than the cap this tool may read, so no comparison is possible. It SHALL carry the cap's name and its value.
- `precondition_required` — an unguarded call to an enforceable tool while the deployment requires preconditions. It SHALL carry `current_hash` when the tool had already read the incumbent bytes, and SHALL omit it otherwise rather than performing a read solely to populate it.

"I could not check" and "I checked and it differs" SHALL NOT share a code: answering an over-cap file with a mismatch sends the caller to fetch a hash it can never obtain.

No code SHALL carry a retry delay: no amount of waiting makes a stale or missing hash valid, and none shrinks a file below a cap. **Each code's prose half SHALL state the action that resolves it**, and the actions differ, so one generic "re-read and retry" sentence is not sufficient:

- `stale_precondition` — re-read the file and recompute the write from its current bytes; the returned `current_hash` may be resent as `expected_hash` if nothing else changes in between;
- `concurrent_write` — the file changed during this call; re-read it and retry, since no hash from before this call can be valid;
- `precondition_required` — this deployment requires a precondition; re-read the file and resend with `expected_hash` (using the `current_hash` returned here when one is present);
- `no_incumbent` — there is nothing to guard at this path; **call again without `expected_hash`**;
- `malformed_precondition` — state the canonical form, `sha256:<64 lowercase hex>`, and where a valid one comes from;
- `precondition_unavailable` — name the cap that applied and its value, and state that only an operator can raise it, so a caller that must inspect the file's bytes should use the transfer download route instead.

The prose SHALL name the read that produces a usable hash (`read_note`, or `read_file(hash_only=true)` for a raw file) wherever re-reading is the remedy.

The refusal SHALL carry no note content: no excerpt, no diff, no length. `path` SHALL be bounded as every other path-bearing message is.

If the shared refusal module does not yet exist when this capability is implemented, it SHALL be created with that identical contract rather than a second refusal shape being invented for these codes.

#### Scenario: A stale precondition is machine-readable

- **WHEN** a guarded write is refused because the file changed
- **THEN** the result SHALL end with a single-line `MCP-REFUSAL` sentinel whose JSON carries `code` `stale_precondition`, the `path`, the file's `current_hash`, and `nothing_written` true

#### Scenario: Refusal codes are distinguishable

- **WHEN** a caller receives, in turn, a stale precondition, a precondition on a path with no incumbent, and a malformed hash
- **THEN** the three results SHALL carry three different `code` values, and a client SHALL be able to distinguish them without parsing prose

#### Scenario: The two conflict windows are distinguishable by code

- **WHEN** a caller receives a refusal for a hash that was stale on arrival, and separately a refusal for a file that changed between this call's read and its publication
- **THEN** the first SHALL carry `stale_precondition` with `current_hash` and the second SHALL carry `concurrent_write`
- **AND** the second's existing prose SHALL be unchanged, with the sentinel appended to it

#### Scenario: An unhashable incumbent has its own code

- **WHEN** a guarded write names a file larger than the cap the tool may read
- **THEN** the refusal SHALL carry `precondition_unavailable` with the cap's name and value, and SHALL NOT be reported as a mismatch

#### Scenario: No refusal invites a blind retry

- **WHEN** any of the six refusals is returned
- **THEN** it SHALL NOT carry a retry delay, and the prose SHALL name the read that produces a usable hash wherever re-reading is the remedy

#### Scenario: Each code states its own remedy

- **WHEN** a caller receives `no_incumbent`, `malformed_precondition` and `precondition_unavailable` in turn
- **THEN** the first SHALL tell it to call again **without** `expected_hash`, the second SHALL state the canonical form `sha256:<64 lowercase hex>`, and the third SHALL name the cap and its value and say that raising it is an operator action
- **AND** none of the three SHALL tell the caller to re-read and retry, which would not resolve any of them

### Requirement: Every overwrite path accepts an optional caller-supplied precondition

`edit_note` (in all four modes, `dry_run` included, and with `replace_frontmatter` set or unset), `set_frontmatter`, `write_file`, `move_note`, `delete_note`, `delete_file` and `create_note` SHALL each accept an optional `expected_hash` argument, and SHALL behave exactly as they do today when it is omitted and the deployment does not require it.

The list SHALL cover **every tool that can destroy content the caller may have read**, which includes both delete tools in both of their modes: a permanent delete is irreversible, and a soft delete puts the bytes under a `.trash` name only an agent that knows to look will find. For `delete_file` the comparison SHALL run before the trash rename or the unlink, through the same anchored lookup that tool already validates with.

The argument SHALL NOT be required by default. A call that omits it keeps today's behaviour in full, including the absence of any conflict detection on `write_file(overwrite=true)`, so no deployed client is broken by this capability. Backward compatibility is claimed for **mutation and conflict semantics**; success result text changes, because every publishing write now reports the hash of what it published.

When `expected_hash` is supplied, the tool SHALL compare it against the digest of the bytes it read inside the call, and:

- a **match** proceeds to the write;
- a **mismatch** is a `stale_precondition` refusal naming the file's current `content_hash`, leaving the file, every other file, and every derived index row untouched;
- **no incumbent bytes to bind** is a `no_incumbent` refusal. This covers `create_note`, `write_file(overwrite=false)`, and `write_file(overwrite=true)` on a path that does not exist — the last SHALL NOT fall through to creating the file, because the caller asserted it was replacing something;
- a file the tool cannot read within its byte cap is a refusal naming the cap, never an unguarded write.

**Every one of those tools SHALL accept the argument in its signature**, including the ones that can never honour it: a signature that rejects `expected_hash` answers with a protocol-level argument error instead of the typed refusal this capability promises, which is the opposite of the contract. `create_note` SHALL evaluate it and refuse **before any filesystem work**.

For `move_note` the precondition binds the **source note's own bytes** and nothing else; the backlink sources a `rewrite_links=true` move rewrites are not bound, because the caller never read them, and the refusal prose SHALL say so.

The value of `expected_hash` SHALL be recorded in the tool's usage-log parameters, so an operator can tell a guarded write from an unguarded one after the fact. The "nothing was written" guarantee covers the vault and the derived index; the call's own `usage_logs` row is written for a refusal exactly as for any other call.

#### Scenario: A stale hash refuses and names the current one

- **WHEN** an agent reads a note, the note changes on disk, and the agent then calls `edit_note` with the `expected_hash` from its read
- **THEN** the tool SHALL return a `stale_precondition` refusal naming the note's current `content_hash`, the file SHALL be byte-identical to what the concurrent writer left, and no `notes_metadata` or `note_links` row SHALL change

#### Scenario: An omitted hash keeps today's behaviour

- **WHEN** the same stale `edit_note` call is made with no `expected_hash` and the deployment does not require one
- **THEN** the write SHALL proceed exactly as it does today, overwriting the concurrent change

#### Scenario: A matching hash proceeds

- **WHEN** `expected_hash` equals the file's current `content_hash`
- **THEN** the tool SHALL perform its write and report success

#### Scenario: A precondition on a creation is answered, not rejected by the signature

- **WHEN** `create_note` is invoked with `expected_hash`
- **THEN** the tool SHALL accept the argument, return a `no_incumbent` refusal before touching the filesystem, and create nothing
- **AND** the call SHALL NOT fail as a protocol-level argument error

#### Scenario: A guarded overwrite of a path that does not exist

- **WHEN** `write_file(overwrite=true, expected_hash=…)` names a path with no file at it
- **THEN** the tool SHALL return a `no_incumbent` refusal and SHALL NOT create the file

#### Scenario: An unreadable incumbent refuses rather than proceeding unguarded

- **WHEN** `write_file(overwrite=true, expected_hash=…)` targets a file larger than the read cap, so the incumbent bytes cannot be hashed
- **THEN** the tool SHALL refuse naming the cap, and SHALL NOT write

#### Scenario: A guarded delete

- **WHEN** `delete_note` is invoked with an `expected_hash` that no longer matches, with `permanent` either true or false
- **THEN** the note SHALL remain at its path, nothing SHALL be moved to `.trash` or unlinked, and the refusal SHALL name the current hash

#### Scenario: A guarded raw-file delete

- **WHEN** `delete_file` is invoked with an `expected_hash` that no longer matches, with `permanent` either true or false
- **THEN** the file SHALL remain at its path, no `.trash` entry SHALL be created and nothing SHALL be unlinked, and the refusal SHALL carry `stale_precondition` with the current hash
- **AND** with a matching hash the delete SHALL proceed exactly as an unguarded one does

#### Scenario: A guarded move binds the source only

- **WHEN** `move_note(from_path, to_path, expected_hash=…)` is invoked and `from_path`'s bytes no longer match
- **THEN** the refusal SHALL happen before the rename and before any link rewrite, the note SHALL stay at `from_path`, and no source note SHALL be rewritten

### Requirement: A deployment may require the precondition

The server SHALL support a deployment setting, `WRITE_PRECONDITION_REQUIRED`, defaulting to **false**, which when true makes every tool that accepts an enforceable `expected_hash` refuse a call that supplies none, with the `precondition_required` code and nothing written.

The setting is a deployment decision and SHALL NOT be overridable per call: an agent cannot turn it off, and a client that does not send hashes is refused visibly rather than silently exposed. The tools that can never honour a precondition — `create_note` and `write_file(overwrite=false)` — SHALL be **exempt**, because requiring a hash there would make creation impossible. Where the refusing tool had already read the incumbent bytes, the refusal SHALL carry `current_hash` so a compliant agent recovers in one retry; where it had not, it SHALL name the read that supplies one instead of performing an extra read.

With the setting false — the default — the deploy SHALL be a no-op for every existing client.

#### Scenario: Required mode refuses an unguarded write

- **WHEN** `WRITE_PRECONDITION_REQUIRED` is true and `edit_note` is invoked with no `expected_hash`
- **THEN** the tool SHALL return a `precondition_required` refusal carrying the note's current `content_hash`, and nothing SHALL be written

#### Scenario: Required mode does not block creation

- **WHEN** `WRITE_PRECONDITION_REQUIRED` is true and `create_note` is invoked with no `expected_hash`
- **THEN** the note SHALL be created normally

#### Scenario: The default changes nothing

- **WHEN** the setting is left at its default
- **THEN** every tool SHALL accept unguarded calls exactly as it does today

### Requirement: The precondition checks run in one fixed precedence

A tool that receives `expected_hash` SHALL evaluate the precondition in this order, and SHALL report the **first** condition that applies:

1. **syntax** — a hash that is not in the canonical form is `malformed_precondition`, decided **at the tool's entry, before path resolution, before any leaf check and before any read**, in every tool and every mode. It is a pure function of the argument and SHALL live apart from the comparison, so that a malformed hash wins over "not found", over a symlinked leaf, and over the size cap — a caller told "not found" for a call whose argument was never valid fixes the wrong thing;
2. **no incumbent** — a tool or mode that can never bind incumbent bytes (`create_note`, `write_file` with `overwrite=false`), and an overwrite naming a path with no file at it, is `no_incumbent`;
3. **unavailable** — an incumbent larger than the cap the tool may read is `precondition_unavailable`, naming the cap;
4. **required** — an enforceable call supplying no hash while the deployment requires one is `precondition_required`;
5. **comparison** — a digest that differs from the incumbent's is `stale_precondition`;
6. **publication** — the existing in-call comparison, reported as `concurrent_write`.

Malformed always wins, because a caller who sent the wrong *kind* of value must learn that rather than learning something about a file its argument never validly named. A call that is both unguarded and over-cap under required mode SHALL be `precondition_unavailable`, not `precondition_required`: telling such a caller to supply a hash sends it after one it cannot obtain.

The comparison SHALL run immediately after the read of the incumbent bytes and **before** mode dispatch, before the result size cap, before `dry_run` diff generation, and before any no-op or defect determination. Ordering is observable and therefore normative: a unified diff, a "no changes" answer, or a frontmatter-defect report computed against a base the caller does not hold is a wrong answer, not a cheap one. A tool that reads nothing today when unguarded (`write_file`, `move_note`, `delete_note`, `delete_file`) SHALL perform the read **only** when a hash is supplied or required mode demands one.

**Every read this capability adds is bounded, and the bound is the one that already governs that tool's content**: `MAX_NOTE_BYTES` for a note tool, `MAX_FILE_READ_BYTES` for a raw-file tool. The size SHALL be established from the descriptor the tool has already opened — `fstat` first, then a bounded read through that same descriptor, never a second pathname resolution — so the bytes measured are the bytes hashed. A file over the bound is `precondition_unavailable`.

The final in-call byte comparison SHALL also be bounded by the expected bytes' length: a size larger than that length SHALL refuse without a full read, and growth after the size check SHALL be detected with at most one extra byte. This refusal SHALL retain the existing `concurrent_write` code and prose, since the incumbent changed after preflight. Missing and non-regular leaves SHALL retain their existing error behavior.

#### Scenario: The incumbent grows after preflight

- **WHEN** a guarded raw overwrite or note edit sees the incumbent grow after its initial read, including growth after the final comparison's `fstat`
- **THEN** the final comparison SHALL read at most the expected length plus one byte, refuse as `concurrent_write`, and leave the concurrent writer's bytes untouched

**The compatibility rule for an over-cap file:** when no `expected_hash` was supplied and required mode is off, the tool SHALL proceed exactly as it does today and simply report no `content_hash`. This capability SHALL NOT make a call fail that succeeds today merely because the file is too large to hash.

#### Scenario: A dry run against a stale base refuses instead of diffing

- **WHEN** `edit_note(dry_run=true, expected_hash=…)` is invoked with a hash that no longer matches
- **THEN** the tool SHALL return the `stale_precondition` refusal and SHALL NOT return a diff

#### Scenario: A stale precondition outranks a no-op

- **WHEN** `set_frontmatter` is invoked with a stale `expected_hash` and with `updates` that would have been a byte-identical no-op
- **THEN** the tool SHALL return the `stale_precondition` refusal rather than "no changes"

#### Scenario: An unguarded call reads no more than it does today

- **WHEN** `write_file(overwrite=true)` is invoked without `expected_hash` and the deployment does not require one
- **THEN** the tool SHALL NOT read the incumbent file

#### Scenario: Malformed beats no-incumbent on a creation

- **WHEN** `create_note` is invoked with an `expected_hash` that is not in the canonical form
- **THEN** the refusal SHALL be `malformed_precondition`, not `no_incumbent`, and nothing SHALL be created

#### Scenario: Malformed beats no-incumbent on a no-clobber write

- **WHEN** `write_file(overwrite=false, expected_hash=<bare hex>)` is invoked
- **THEN** the refusal SHALL be `malformed_precondition`

#### Scenario: Malformed beats no-incumbent on a missing path

- **WHEN** `write_file(overwrite=true, expected_hash=<uppercase hex>)` names a path with no file at it
- **THEN** the refusal SHALL be `malformed_precondition`, and the tool SHALL NOT have needed to look at the filesystem to decide it

#### Scenario: Malformed beats unavailable on an over-cap file

- **WHEN** a guarded write names a file larger than the tool's read cap and supplies a hash that is not in the canonical form
- **THEN** the refusal SHALL be `malformed_precondition`, not `precondition_unavailable`

#### Scenario: Required mode on an unhashable file names the real cause

- **WHEN** the deployment requires preconditions and a guarded write names a file larger than the tool's read cap with no `expected_hash`
- **THEN** the refusal SHALL be `precondition_unavailable`, not `precondition_required`

#### Scenario: Malformed beats a missing file and a symlinked leaf

- **WHEN** any guarded tool is invoked with a non-canonical `expected_hash` against a path that does not exist, and again against a path whose final component is a symlink
- **THEN** both SHALL refuse with `malformed_precondition`, not with the not-found or symlink error, because the syntax check runs at the tool's entry before any path work

#### Scenario: An over-cap file is unchanged for an unguarded caller

- **WHEN** a file larger than the tool's read cap is written by a call that supplies no `expected_hash`, with required mode off
- **THEN** the call SHALL behave exactly as it does today and SHALL succeed, reporting no `content_hash`

### Requirement: A section write's precondition is the whole file's hash

`edit_note(section=…)` SHALL accept the file's whole-file `content_hash` as its precondition and SHALL NOT define or accept any section-scoped digest.

A hash over a section body is unsound as a precondition because section selectors are positional: an `#N` ordinal names the Nth heading of the *current* document, so a body-only digest could certify an unchanged body while an insertion above it changed which section the selector resolves to. The consequence is declared rather than hidden — the mode with the smallest blast radius becomes the most conflict-prone, since an unrelated edit elsewhere in the file invalidates the precondition. That is acceptable only because the argument is optional, and both docstring layers SHALL say so.

#### Scenario: An edit elsewhere in the file invalidates a section precondition

- **WHEN** a caller reads `read_note(path, section="Tasks")`, another writer edits an unrelated section of the same note, and the caller writes back with `expected_hash`
- **THEN** the tool SHALL refuse with `stale_precondition` naming the current hash

#### Scenario: A section read carries the whole file's hash

- **WHEN** `read_note(path, section=…)` returns a section body
- **THEN** the `content_hash` in that response SHALL be the whole file's, and passing it straight to `edit_note(section=…, expected_hash=…)` on an unchanged file SHALL succeed

### Requirement: Write tools report the content hash of the bytes they published

`create_note`, `edit_note`, `set_frontmatter`, `write_file` and `move_note` SHALL report a `content_hash` in their success result, computed over the bytes that call published, and `delete_note` SHALL report none.

The value describes what this call wrote, not what is on disk at the moment the caller reads the message; the docstrings SHALL state that distinction. Reporting it makes a write→write chain guardable without an intervening read.

The reported value SHALL always be the hash of the bytes the call **actually published**, never of bytes it intended to publish. Reporting an intended-but-unpublished result would hand the caller a token that binds nothing and would make its next guarded write fail against bytes that were never written.

`move_note` therefore reports by this matrix:

- a plain move (`rewrite_links=false`) SHALL report the moved bytes, unchanged from the source;
- a `rewrite_links=true` move whose **moved note's own rewrite published** SHALL report the post-rewrite bytes at the destination;
- a `rewrite_links=true` move whose **moved note's own rewrite failed after the rename** SHALL report by the *observed cause*, per the post-rename contract below;
- a **backlink source's** rewrite failing SHALL NOT change what is reported: the value is always the moved note's, and the rewritten sources' hashes SHALL NOT be reported at all.

**The post-rename partial-success contract.** Once the rename has committed, the call has changed the vault, and what the server may claim about the destination depends on why the follow-up rewrite failed:

- the rewrite failed **without observing a change** — an I/O error, a cap, a refusal computed before it read — so the destination still holds exactly the bytes the rename published: report **that** hash, and say the move completed while the rewrite did not;
- the rewrite failed with the in-call conflict (`concurrent_write`) — something changed the destination between the rename and the rewrite's publication, so the destination holds **that writer's** bytes, which this call never read: the result SHALL **omit** the `content_hash` entirely and SHALL state that the move completed, the link rewrite did not, and the destination must be re-read before it is written to. Reporting the rename's hash there would name bytes that are no longer on disk, which is precisely the token-that-binds-nothing failure this requirement exists to prevent.

A post-rename failure SHALL NOT be reported as a whole-call refusal and SHALL NOT carry `nothing_written: true` in any form: something *was* written — the rename — and a caller told otherwise would look for a note that has moved. The existing partial-success report remains the shape, extended with the statements above.

A result that publishes nothing SHALL report no hash: `edit_note(dry_run=true)`, a `set_frontmatter` no-op, both delete tools, and every refusal.

**A hash is reported only when the bytes can be bounded.** Where a tool would have to read a file back to report its hash and that file exceeds the cap the tool may read, the result SHALL omit the `content_hash` and say so, rather than failing a call that has already succeeded.

#### Scenario: A write reports a hash the next write can bind to

- **WHEN** `edit_note` succeeds and its result is followed immediately by a second `edit_note` supplying the reported hash as `expected_hash`
- **THEN** the second call SHALL proceed

#### Scenario: A link-rewriting move reports the destination

- **WHEN** `move_note(from_path, to_path, rewrite_links=true)` succeeds and the moved note's own body contained a self-link that was rewritten
- **THEN** the reported `content_hash` SHALL be that of the file now at `to_path`, and passing it to `edit_note(to_path, …, expected_hash=…)` SHALL proceed

#### Scenario: A move whose own rewrite failed without a conflict reports what is on disk

- **WHEN** `move_note(rewrite_links=true)` completes its rename but the **moved note's own** body rewrite fails for a reason that observed no change to the destination, producing the partial-success report
- **THEN** the reported `content_hash` SHALL be that of the unrewritten bytes now at the destination — the bytes the rename published — and passing it to a following guarded write SHALL proceed

#### Scenario: A move whose own rewrite lost the in-call conflict reports no hash

- **WHEN** `move_note(rewrite_links=true)` completes its rename and the **moved note's own** body rewrite is then refused as `concurrent_write`, because another writer changed the destination in between
- **THEN** the result SHALL omit `content_hash` entirely, SHALL state that the move completed and the link rewrite did not, and SHALL direct the caller to re-read the destination before writing to it
- **AND** the result SHALL NOT claim that nothing was written, because the rename committed

#### Scenario: A destination too large to hash still reports success

- **WHEN** a call succeeds but the file whose hash it would report exceeds the cap that tool may read
- **THEN** the result SHALL report the success without a `content_hash`, naming the reason, and SHALL NOT fail

#### Scenario: A backlink source's failure does not change the reported hash

- **WHEN** `move_note(rewrite_links=true)` completes and one **backlink source's** rewrite fails
- **THEN** the reported `content_hash` SHALL still be the moved note's, and no source's hash SHALL be reported

#### Scenario: A dry run reports no hash

- **WHEN** `edit_note(dry_run=true)` returns a diff
- **THEN** the result SHALL NOT report a `content_hash`, because nothing was published

## MODIFIED Requirements

### Requirement: Note read-modify-write operations detect conflicts
`edit_note`, `set_frontmatter`, and backlink body rewrites SHALL compare the current on-disk content with the content on which the new result was computed immediately before atomic publication. They SHALL reject a mutation when that comparison observes a difference. This is optimistic conflict detection and does not claim coordination with a non-cooperating writer in the interval after comparison.

This comparison covers **one window: this call's own read through to this call's publishing rename.** It structurally cannot see a change that landed between a caller's earlier read and this call, because the bytes it compares are the ones this call read. The caller-visible `expected_hash` precondition covers that other window — the caller's read through to this call's read — and the two SHALL be documented as a pair, with neither described as subsuming the other and neither removed in favour of the other. A supplied `expected_hash` that matched therefore does not exempt a call from this comparison: a writer landing between the precondition check and the publication SHALL still be detected here.

A refusal from this comparison SHALL be typed as `concurrent_write` through the shared refusal contract: its existing prose SHALL be unchanged and the sentinel appended, so that existing callers and assertions are unaffected while an agent can tell this window from the caller-visible one **by code** rather than by prose shape. The guidance it carries is to re-read and retry, and it SHALL carry no retry delay. `move_note`'s per-source rewrite failures keep their existing partial-success report and are not this refusal.

The raw-byte publish helpers SHALL accept the same `expected` parameter the text helpers accept, defaulting to no comparison, so that a caller which reads before it writes can reach the comparison at all; existing callers that pass nothing SHALL be unaffected.

`write_file(overwrite=true)` SHALL perform this comparison **only** on calls that supply `expected_hash` — on those calls the bytes read for the precondition are the ones compared before the rename — and SHALL remain an unconditional replace on calls that do not, which is its documented behaviour.

#### Scenario: External edit occurs concurrently
- **WHEN** Obsidian changes a note after the server reads it and the pre-publication comparison observes the new content
- **THEN** the server SHALL return a conflict
- **AND** SHALL NOT overwrite the newer external content

#### Scenario: Backlink rewrite conflicts after a move
- **WHEN** a backlink source changes before its post-move rewrite is published
- **THEN** the source's newer content SHALL remain unchanged
- **AND** `move_note` SHALL report partial success and identify that one or more link rewrites failed

#### Scenario: A matching precondition does not disable the in-call comparison
- **WHEN** `edit_note` is called with a matching `expected_hash` and another writer changes the note after the precondition check but before publication
- **THEN** the in-call comparison SHALL observe the difference and refuse with its own distinct conflict message
- **AND** nothing SHALL be written

#### Scenario: The two refusals are distinguishable by code
- **WHEN** a caller compares a stale-precondition refusal with an in-call conflict refusal
- **THEN** the first SHALL carry `stale_precondition` with the file's current `content_hash` and the second SHALL carry `concurrent_write`
- **AND** the in-call refusal's prose SHALL be unchanged from today's

#### Scenario: The raw-byte helper can carry an expectation
- **WHEN** a raw-byte write is published through the shared helper with an expectation that no longer matches the file
- **THEN** the publication SHALL be refused and the file SHALL be unchanged
- **AND** a call that passes no expectation SHALL publish exactly as it does today
