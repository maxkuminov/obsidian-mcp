## Representation and terminal classification

Use a pure `BodyOutcome(str)` value with an explicit `Refusal`, a closed usage
marker, original server-authored prose, and disposition `refused` or `partial`.
It is a string on the wire and under existing equality/substring consumers.
Constructing a value records no telemetry. `_tracked` inspects only the final
returned value after the body completes; an intermediate refusal that was
considered and discarded cannot poison a success. A normal string containing
MCP-REFUSAL or JSON-looking text is never an outcome.

The `read_note` error factory attaches the typed value via a Pydantic PrivateAttr
on `ReadNoteResult`. It is absent from model dumps and the public output schema.
The error renderer reserves the final sentinel length before truncating/sanitizing
prose, so MAX_READ_RESPONSE_CHARS is obeyed without destroying the machine line.
No note-controlled field is relabeled or shortened by this change.

The generic renderer stays dependency-free. Add an explicit authoritative mode
(or equivalent pure API) for new body outcomes so caller-controlled text at the
end of server prose cannot suppress the genuine final sentinel through the
historical idempotence check. Preserve existing default gate rendering behavior.
When rewording an already typed helper result, use its original prose and metadata
rather than interpolation/rpartition of its rendered sentinel. Exactly one
server-generated final sentinel is appended; a forged line inside quoted prose
has no authority and cannot choose the recorded code.

## Closed register

Retain every existing pre-body marker and caller code. Existing post-body usage
markers retain their identity: permission_denied, provider_input_rejected,
related_source_not_found, related_source_not_embedded, vault_assignment_changed,
vault_anchor_lost_at_publish, vault_confirmation_unavailable and tool_exception.
Provider input rejection retains caller code argument_too_long while its usage
marker remains provider_input_rejected.

The six write-precondition codes become their own post-body usage markers:
malformed_precondition, no_incumbent, precondition_required,
precondition_unavailable, stale_precondition, concurrent_write. Existing hash,
cap, path, nothing_written and no-retry contracts remain unchanged.

New body codes/markers are a finite register: invalid_argument, validation_failed,
invalid_path, unsafe_path, not_found, already_exists, read_window_unavailable,
selector_unresolved, match_not_found, match_ambiguous, content_unsafe, size_limit,
resource_limit, unsupported_filesystem, io_failure, index_not_ready,
transfer_unavailable, credential_unusable, fetch_refused, transfer_busy,
transfer_timeout, partial_completion and publication_uncertain. Existing specific
post-body markers may become caller codes where no caller code existed. The
inventory assigns branch facts to these values. Generic ValueError boundaries
use validation_failed when several causes share a catch; never parse exception
prose to claim a narrower cause. A refinement outside this register needs a
reviewed contract change.

New body payloads omit irrelevant null bucket fields. Do not manufacture a retry
interval or infer nothing_written from disposition. Broad categories are honest
at current exception boundaries; changing service validation algorithms is not
part of this issue.

## Publication and compatibility

No permission, validation, error precedence, quota, filesystem or transaction
boundary moves. Empty results, successful status reads (including check_upload
unknown/revoked/expired), no-op edits and ordinary note contents remain successes.

The inventory explicitly covers metadata-loss sites: _move_precondition_error,
move's embedded cap explanation, read_note._fail, and move's final joined parts.
A returned move/import that has committed some work, failed a rewrite, failed
metadata repair or cannot verify rollback is partial/uncertain, not a refusal
claiming nothing happened. Preserve existing typed concurrent_write and its
nothing_written omission where already correct. Successful byte writes may not
be turned into tool_exception by classification/logging failures.

## Observability

The normal single usage row gains `error=<closed marker>` and
`body_outcome=refused|partial`. These are POST-body, always retained in latency
statistics and actual request/quota counts. Nothing is added to the existing
pre-body predicate by #263. Existing marker-specific consumers keep working.

Emit one `tool_body_outcome` event in the response-neutral telemetry tail, with
only tool, reason (closed marker), outcome (closed disposition) and authenticated
row IDs. Existing specific authorization/provider/publication events remain as
separate facts; do not emit two generic outcomes for one returned value. No paths,
content, hashes, exception prose or capabilities enter the new event. Existing
suppressor bounds it. Exceptions still take precedence as tool_exception;
cancellation propagates without inventing completion. A generic event or usage
write failure never changes the result already produced.

#261 shadow data lives in a separate namespaced params object; it cannot overwrite
this error/disposition or make executed work disappear from performance views.

## Verification

Check inventory completeness against all 25 registered tools and helper returns.
Use behavioral tests for every new code/category and representative actual tool
paths, all six preconditions, private structured metadata, bounded sentinels,
forged sentinel content, successful no-ops/statuses/empty sets, partial writes,
exception precedence, ContextVar isolation, and telemetry failure neutrality.
Use real PostgreSQL to verify stored markers and existing latency predicates.
Independently review the full branch inventory and adversarial write scenarios;
then run the combined offline, integration, strict OpenSpec and dependency gates.
