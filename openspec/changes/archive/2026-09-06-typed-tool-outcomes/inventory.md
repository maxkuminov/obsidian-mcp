# Issue #263 implementation inventory

Read-only investigation, 2026-09-06. Locations refer to the baseline tools.py before #263 edits; locate by function/branch as lines move. This is an implementer brief, not a verified implementation or approved specification.

## Recommended contract and mechanism

All 25 tracked tools are inventoried below: 197 direct return sites, plus nested/helper return paths. Preserve wire schemas, original prose, permission/validation precedence, successful bytes, empty results, no-ops, and publication boundaries. Append the existing final MCP-REFUSAL line only to server-authored refusals; never classify by searching returned text. Tool results can contain arbitrary user note content, including forged sentinels.

Main now prefers a pure BodyOutcome str subclass carrying explicit Refusal/code, closed usage marker and disposition (refused/partial). _tracked inspects this internal type (never prose), then writes bounded outcome metadata into its existing call-scoped timing holder or merged params after execution. ReadNoteResult carries the metadata through a Pydantic PrivateAttr, absent from the wire schema. This is sound if the explicit reconstruction sites below preserve metadata. Keep refusals.py dependency-free. Add a bounded explicit outcome register, not arbitrary error text as markers. Never mutate the global renderer into a logging side effect: it is also used for pre-body gates and static values.

Use pure typed values in shared helpers; commit telemetry only for the value actually returned from the tool. This avoids eager helper evaluation poisoning a later successful result. In particular, don't append a sentinel inside _note_size_error_for when its prose is embedded by move preflight: the sentinel would land in parentheses rather than as the final line. Likewise _move_precondition_error rearranges prose; preserve the single final sentinel and its existing path/hash semantics. A str subclass loses metadata under interpolation/concatenation. Reconstruct with an explicit preserve-metadata/replace-prose method at the finite sites identified below; do not try to recover metadata by parsing a string.

Code suggestions below intentionally use broad validation_failed where existing ValueError catches combine path, cap, and policy failures. Do not parse exception prose to pretend to know a narrower reason; branch on an existing exception type/call-site fact. A separately justified typed service exception can refine these later without widening #263 into filesystem redesign.

## Proposed closed register

Retain all existing pre-body codes/markers unchanged. Retain existing post-body markers: permission_denied, provider_input_rejected, related_source_not_found, related_source_not_embedded, vault_assignment_changed, vault_anchor_lost_at_publish, vault_confirmation_unavailable, tool_exception. Add the six existing precondition codes as usage markers: malformed_precondition, no_incumbent, precondition_required, precondition_unavailable, stale_precondition, concurrent_write. Add ordinary body codes/markers: invalid_argument, validation_failed, invalid_path, unsafe_path, not_found, already_exists, read_window_unavailable, selector_unresolved, match_not_found, match_ambiguous, content_unsafe, size_limit, resource_limit, unsupported_filesystem, io_failure, index_not_ready, transfer_unavailable, credential_unusable, fetch_refused, transfer_busy, transfer_timeout. Add partial_completion/publication_uncertain only for genuinely partial/uncertain operations; preserve concurrent_write on its already-typed partial move path and add disposition=partial separately.

Names are proposals; main's reviewed OpenSpec is authoritative. New generic body Refusal values should use absent fields, not irrelevant null bucket fields. Preserve the old gate JSON shape exactly. No retry_after_seconds unless an existing measured timer gives an honest interval. Do not automatically emit nothing_written=true: the helper cannot know whether setup, indexing, rename or partial writes occurred.

## Telemetry and compatibility obligations

- _tracked (1480-1635): after body completion, record one row and optionally one generic bounded outcome event from the response-neutral telemetry tail. Exceptions still override a pending outcome with tool_exception; cancellation remains cancellation. Do not put post-body markers in PRE_BODY_REFUSAL_ERROR_MARKERS (usage_stats.py): those calls belong in latency calculations and already consumed their quota.
- _precondition_refusal (2268): currently renders only, does not record usage. Wire all six codes once here or at terminal callers, preserve current hashes/caps/nothing_written and _move_precondition_error scope prose.
- Existing security events: permission refusal already emits tool_write_refused; provider/related/confirmation paths have specific markers; move overlaps and partial failures have existing events. Decide explicitly whether the generic outcome event replaces only overlapping events or carries a separate summary. Never emit two copies of the same decision accidentally. Put only closed code/disposition/tool/actor identifiers in events, no path/prose/hash/query/URL/token.
- security_events.py event catalog/allowed fields and AST allowlist tests need corresponding additions. _security_subject remains user/principal/address based, not attacker-minted outcome text. Suppression does not suppress usage rows.
- ReadNoteResult is the only structured tool result. _fail must accept a code and reserve space for the final sentinel within max_read_response_chars; bounded/scalar-safe original prose first, sentinel intact last. No content/heading/frontmatter on failure. Do not append a sentinel after truncating to the entire existing budget.
- read_file may return Image or opaque base64; successful values remain byte/schema-identical. No scanning their payloads for sentinels, permission words, or error fields.
- check_upload status is about another operation: completed/pending/uploading/expired/revoked/unknown are all successful queries. Only malformed or inaccessible IDs are refused. Keep _loggable_upload_id redaction.
- Durability cleanup/fsync warnings under security-event R10 are still successful operations, outside this task. Do not instrument quiet filesystem cleanup as refusals.
- No database migration is needed: bounded outcome metadata fits existing usage_logs.params JSON. Preserve historical rows; don't infer/backfill their outcome from prose.

## Complete tracked-tool return inventory

Each entry identifies every direct return branch. PROPAGATE means classify at the defining helper/terminal branch, never blindly replace a more specific existing marker. SUCCESS remains unchanged. Nested implementation helpers are listed separately after the 25 tools.

### search_notes_impl — src/mcp_server/tools.py:1647
- Line 1668: **SUCCESS — unchanged; no outcome marker**. Current return: `f"No results for '{query}'"`
- Line 1673: **SUCCESS — unchanged; no outcome marker**. Current return: `'\n'.join(lines)`

### read_note_impl — src/mcp_server/tools.py:1815
- Line 1891: **REFUSE not_found**. Current return: `_fail(f'Note not found: {path}')`
- Line 1893: **REFUSE validation_failed — existing generic ValueError combines path/size/policy cases; do not parse prose or narrow accepted inputs**. Current return: `_fail(str(e))`
- Line 1897: **REFUSE invalid_argument**. Current return: `_fail(f'read_note: limit must be >= 1 (got {limit}).')`
- Line 1900: **REFUSE invalid_argument**. Current return: `_fail(f'read_note: offset must be >= 0 (got {offset}).')`
- Line 1907: **REFUSE selector_unresolved — service returns missing/ambiguous/invalid section; avoid parsing its prose**. Current return: `_fail(err)`
- Line 1916: **REFUSE read_window_unavailable — EOF and past-end remain existing error/status behavior**. Current return: `_fail(f'read_note: offset {offset:,} is exactly the end of {_origin_label(section)} in {path} ({total:,} chars) — the whole selection has been read, there is nothing further.')`
- Line 1921: **REFUSE read_window_unavailable — EOF and past-end remain existing error/status behavior**. Current return: `_fail(f'read_note: offset {offset:,} is past the end of {_origin_label(section)} in {path} ({total:,} chars).')`
- Line 1971: **SUCCESS — unchanged; no outcome marker**. Current return: `result`

### list_notes_impl — src/mcp_server/tools.py:1976
- Line 1998: **SUCCESS — unchanged; no outcome marker**. Current return: `f"No markdown files in '{folder or '/'}'"`
- Line 2008: **SUCCESS — unchanged; no outcome marker**. Current return: `'\n'.join(lines)`

### get_tags_impl — src/mcp_server/tools.py:2012
- Line 2036: **SUCCESS — unchanged; no outcome marker**. Current return: `'No tags found'`
- Line 2041: **SUCCESS — unchanged; no outcome marker**. Current return: `'\n'.join(lines)`

### get_recent_impl — src/mcp_server/tools.py:2045
- Line 2067: **SUCCESS — unchanged; no outcome marker**. Current return: `'No recent notes found'`
- Line 2074: **SUCCESS — unchanged; no outcome marker**. Current return: `'\n'.join(lines)`

### semantic_search_impl — src/mcp_server/tools.py:2082
- Line 2128: **EXISTING — preserve provider_input_rejected usage marker and argument_too_long caller code**. Current return: `refusals.render(f'Error: the embedding provider refused this query as too large for its own input limit: {exc.reason} The query is under {MAX_SEARCH_QUERY_CHARS} characters, but a character cap cannot promise a token limit. Send a shorter query.', refusals.Refusal(code=refusals.ARGUMENT_TOO_LONG, scope='provider', limit=None, limit_unit=None))`
- Line 2151: **SUCCESS — unchanged; no outcome marker**. Current return: `f"No semantic results for '{query}' (embeddings may still be building)"`
- Line 2176: **SUCCESS — unchanged; no outcome marker**. Current return: `'\n'.join(lines)`

### get_vault_guide_impl — src/mcp_server/tools.py:2180
- Line 2194: **SUCCESS — unchanged; no outcome marker**. Current return: `f'{_VAULT_GUIDE_PRIMER}\n\n---\n\n{vault_section}'`

### create_note_impl — src/mcp_server/tools.py:2938
- Line 2960: **PROPAGATE — existing permission/precondition/leaf/cap/publication helper outcome; make defining helper typed, do not overwrite its code**. Current return: `err`
- Line 2969: **PROPAGATE — existing permission/precondition/leaf/cap/publication helper outcome; make defining helper typed, do not overwrite its code**. Current return: `err`
- Line 2980: **PROPAGATE — existing permission/precondition/leaf/cap/publication helper outcome; make defining helper typed, do not overwrite its code**. Current return: `err`
- Line 2988: **REFUSE validation_failed — existing generic ValueError combines path/size/policy cases; do not parse prose or narrow accepted inputs**. Current return: `str(e)`
- Line 2991: **PROPAGATE — existing permission/precondition/leaf/cap/publication helper outcome; make defining helper typed, do not overwrite its code**. Current return: `err`
- Line 3006: **PROPAGATE — existing permission/precondition/leaf/cap/publication helper outcome; make defining helper typed, do not overwrite its code**. Current return: `err`
- Line 3009: **SUCCESS — unchanged; no outcome marker**. Current return: `f'Created note: {path}' + _published_hash_clause(content.encode('utf-8'))`
- Line 3016: **PROPAGATE — leaf refusal if present, otherwise already_exists; preserve short-circuit and precedence**. Current return: `_leaf_state_error(target, path) or f'Note already exists: {path}. Use edit_note to modify it.'`
- Line 3020: **REFUSE validation_failed — existing generic ValueError combines path/size/policy cases; do not parse prose or narrow accepted inputs**. Current return: `str(e)`
- Line 3022: **REFUSE io_failure — preserve caught exception response, no exception text in telemetry**. Current return: `f'Failed to write {path}: {e}'`

### get_backlinks_impl — src/mcp_server/tools.py:3044
- Line 3057: **REFUSE not_found**. Current return: `f'Note not found: {path}'`
- Line 3087: **SUCCESS — unchanged; no outcome marker**. Current return: `f'No backlinks to \`{path}\`'`
- Line 3094: **SUCCESS — unchanged; no outcome marker**. Current return: `'\n'.join(lines)`

### get_links_impl — src/mcp_server/tools.py:3098
- Line 3130: **REFUSE not_found**. Current return: `f'Note not found: {path}'`
- Line 3205: **SUCCESS — unchanged; no outcome marker**. Current return: `f'\`{path}\` has no outgoing links — truncated: {truncated}'`
- Line 3247: **SUCCESS — unchanged; no outcome marker**. Current return: `'\n'.join(lines)`

### get_neighborhood_impl — src/mcp_server/tools.py:3251
- Line 3267: **REFUSE not_found**. Current return: `f'Note not found: {path}'`
- Line 3337: **SUCCESS — unchanged; no outcome marker**. Current return: `f'\`{path}\` has no resolved-link neighbors'`
- Line 3347: **SUCCESS — unchanged; no outcome marker**. Current return: `f'\`{path}\` has no resolved-link neighbors'`
- Line 3372: **SUCCESS — unchanged; no outcome marker**. Current return: `'\n'.join(lines)`

### find_related_impl — src/mcp_server/tools.py:3421
- Line 3463: **EXISTING — preserve related_source_not_found usage marker; add matching typed caller outcome**. Current return: `f'Note not found: {path}'`
- Line 3482: **EXISTING — preserve related_source_not_embedded usage marker; add matching typed caller outcome**. Current return: `f'\`{path}\` has not been embedded yet — the indexer is still catching up. Try again in a few minutes.'`
- Line 3544: **SUCCESS — unchanged; no outcome marker**. Current return: `'\n'.join(empty)`
- Line 3602: **SUCCESS — unchanged; no outcome marker**. Current return: `'\n'.join(lines)`

### find_orphans_impl — src/mcp_server/tools.py:3606
- Line 3649: **SUCCESS — unchanged; no outcome marker**. Current return: `f'No orphan notes{scope}'`
- Line 3655: **SUCCESS — unchanged; no outcome marker**. Current return: `'\n'.join(lines)`

### edit_note_impl — src/mcp_server/tools.py:3675
- Line 3771: **PROPAGATE — existing permission/precondition/leaf/cap/publication helper outcome; make defining helper typed, do not overwrite its code**. Current return: `err`
- Line 3778: **PROPAGATE — existing permission/precondition/leaf/cap/publication helper outcome; make defining helper typed, do not overwrite its code**. Current return: `err`
- Line 3783: **REFUSE invalid_argument**. Current return: `f'edit_note: operation must be "append" or "replace" (got {operation!r}).'`
- Line 3816: **REFUSE invalid_argument**. Current return: `message`
- Line 3830: **REFUSE validation_failed — existing generic ValueError combines path/size/policy cases; do not parse prose or narrow accepted inputs**. Current return: `str(e)`
- Line 3837: **PROPAGATE — existing permission/precondition/leaf/cap/publication helper outcome; make defining helper typed, do not overwrite its code**. Current return: `err`
- Line 3862: **PROPAGATE — existing permission/precondition/leaf/cap/publication helper outcome; make defining helper typed, do not overwrite its code**. Current return: `err`
- Line 3863: **REFUSE size_limit — known bounded reader/content cap branch**. Current return: `f'Failed to read {path}: {e}'`
- Line 3867: **REFUSE io_failure — preserve caught exception response, no exception text in telemetry**. Current return: `f'Failed to read {path}: {e}'`
- Line 3880: **PROPAGATE — existing permission/precondition/leaf/cap/publication helper outcome; make defining helper typed, do not overwrite its code**. Current return: `err`
- Line 3885: **REFUSE io_failure — preserve caught exception response, no exception text in telemetry**. Current return: `f'Failed to read {path}: {e}'`
- Line 3901: **REFUSE content_unsafe — preserve existing frontmatter/fence safety decision**. Current return: `_frontmatter_defect_error('edit_note', path, diagnosis)`
- Line 3911: **PROPAGATE — existing permission/precondition/leaf/cap/publication helper outcome; make defining helper typed, do not overwrite its code**. Current return: `err`
- Line 3914: **REFUSE selector_unresolved — service returns missing/ambiguous/invalid section; avoid parsing its prose**. Current return: `err`
- Line 3918: **REFUSE invalid_argument**. Current return: `'edit_note: find must be a non-empty string. An empty find would match every position and corrupt the note.'`
- Line 3925: **REFUSE match_not_found**. Current return: `f'Find text not found in {path}. First 500 chars of note:\n---\n{preview}\n---'`
- Line 3930: **REFUSE match_ambiguous**. Current return: `f'Find text matches {count} locations in {path}. Provide more surrounding context to match a unique section, or set replace_all=True.'`
- Line 3985: **PROPAGATE — existing permission/precondition/leaf/cap/publication helper outcome; make defining helper typed, do not overwrite its code**. Current return: `err`
- Line 3989: **SUCCESS — unchanged; no outcome marker**. Current return: `f'No changes for {path}'`
- Line 3998: **SUCCESS — unchanged; no outcome marker**. Current return: `diff or f'No changes for {path}'`
- Line 4012: **PROPAGATE — existing permission/precondition/leaf/cap/publication helper outcome; make defining helper typed, do not overwrite its code**. Current return: `err`
- Line 4020: **EXISTING — concurrent_write sentinel, record same post-body usage marker**. Current return: `_concurrent_write_refusal(str(e), path)`
- Line 4021: **REFUSE validation_failed — existing generic ValueError combines path/size/policy cases; do not parse prose or narrow accepted inputs**. Current return: `str(e)`
- Line 4023: **REFUSE io_failure — preserve caught exception response, no exception text in telemetry**. Current return: `f'Failed to write {path}: {e}'`
- Line 4024: **SUCCESS — unchanged; no outcome marker**. Current return: `success_message + _published_hash_clause(new_content.encode('utf-8'))`

### move_note_impl — src/mcp_server/tools.py:4444
- Line 4482: **PROPAGATE — existing permission/precondition/leaf/cap/publication helper outcome; make defining helper typed, do not overwrite its code**. Current return: `err`
- Line 4488: **PROPAGATE — existing permission/precondition/leaf/cap/publication helper outcome; make defining helper typed, do not overwrite its code**. Current return: `err`
- Line 4498: **PROPAGATE — _move_note_locked owns full refusal/success/partial classification**. Current return: `await _move_note_locked(from_path, to_path, rewrite_links, uid, expected_hash)`

### delete_note_impl — src/mcp_server/tools.py:5292
- Line 5314: **PROPAGATE — existing permission/precondition/leaf/cap/publication helper outcome; make defining helper typed, do not overwrite its code**. Current return: `err`
- Line 5318: **PROPAGATE — existing permission/precondition/leaf/cap/publication helper outcome; make defining helper typed, do not overwrite its code**. Current return: `err`
- Line 5324: **REFUSE validation_failed — existing generic ValueError combines path/size/policy cases; do not parse prose or narrow accepted inputs**. Current return: `str(e)`
- Line 5329: **PROPAGATE — existing permission/precondition/leaf/cap/publication helper outcome; make defining helper typed, do not overwrite its code**. Current return: `err`
- Line 5342: **REFUSE io_failure — preserve caught exception response, no exception text in telemetry**. Current return: `f'Failed to read {path}: {e}'`
- Line 5347: **REFUSE not_found**. Current return: `f'Note not found: {path}'`
- Line 5357: **PROPAGATE — existing permission/precondition/leaf/cap/publication helper outcome; make defining helper typed, do not overwrite its code**. Current return: `err`
- Line 5370: **PROPAGATE — existing permission/precondition/leaf/cap/publication helper outcome; make defining helper typed, do not overwrite its code**. Current return: `err`
- Line 5372: **REFUSE io_failure — preserve caught exception response, no exception text in telemetry**. Current return: `f'Permanent delete failed: {e}'`
- Line 5373: **SUCCESS — unchanged; no outcome marker**. Current return: `f'Permanently deleted: {path}'`
- Line 5401: **PROPAGATE — existing permission/precondition/leaf/cap/publication helper outcome; make defining helper typed, do not overwrite its code**. Current return: `err`
- Line 5403: **REFUSE io_failure — preserve caught exception response, no exception text in telemetry**. Current return: `str(e)`
- Line 5405: **REFUSE unsupported_filesystem**. Current return: `str(e)`
- Line 5407: **REFUSE not_found**. Current return: `f'Note not found: {path}'`
- Line 5409: **REFUSE unsafe_path**. Current return: `str(e)`
- Line 5411: **REFUSE concurrent_write — known typed Conflict, not inferred from prose**. Current return: `f'{e}. Nothing was deleted.'`
- Line 5413: **REFUSE io_failure — preserve caught exception response, no exception text in telemetry**. Current return: `str(e)`
- Line 5415: **REFUSE io_failure — preserve caught exception response, no exception text in telemetry**. Current return: `f'Soft-delete failed: {e}'`
- Line 5416: **SUCCESS — unchanged; no outcome marker**. Current return: `f'Soft-deleted: {path} → {dest}'`

### set_frontmatter_impl — src/mcp_server/tools.py:5489
- Line 5532: **PROPAGATE — existing permission/precondition/leaf/cap/publication helper outcome; make defining helper typed, do not overwrite its code**. Current return: `err`
- Line 5536: **PROPAGATE — existing permission/precondition/leaf/cap/publication helper outcome; make defining helper typed, do not overwrite its code**. Current return: `err`
- Line 5552: **REFUSE validation_failed — existing generic ValueError combines path/size/policy cases; do not parse prose or narrow accepted inputs**. Current return: `str(e)`
- Line 5557: **PROPAGATE — existing permission/precondition/leaf/cap/publication helper outcome; make defining helper typed, do not overwrite its code**. Current return: `err`
- Line 5575: **PROPAGATE — existing permission/precondition/leaf/cap/publication helper outcome; make defining helper typed, do not overwrite its code**. Current return: `err`
- Line 5576: **REFUSE size_limit — known bounded reader/content cap branch**. Current return: `f'Failed to read {path}: {e}'`
- Line 5580: **REFUSE io_failure — preserve caught exception response, no exception text in telemetry**. Current return: `f'Failed to read {path}: {e}'`
- Line 5592: **PROPAGATE — existing permission/precondition/leaf/cap/publication helper outcome; make defining helper typed, do not overwrite its code**. Current return: `err`
- Line 5597: **REFUSE io_failure — preserve caught exception response, no exception text in telemetry**. Current return: `f'Failed to read {path}: {e}'`
- Line 5605: **REFUSE content_unsafe — preserve existing frontmatter/fence safety decision**. Current return: `_frontmatter_defect_error('set_frontmatter', path, diagnosis)`
- Line 5617: **REFUSE content_unsafe — preserve existing frontmatter/fence safety decision**. Current return: `f'set_frontmatter: {path} has frontmatter this server cannot represent, under {keys}, so rewriting the block from the parsed mapping would silently delete it. Edit the raw block with \`edit_note(find=...)\`, or replace it with \`edit_note(replace_frontmatter=True)\`.'`
- Line 5626: **SUCCESS — unchanged; no outcome marker**. Current return: `f'No changes for {path} (empty updates and remove)'`
- Line 5663: **SUCCESS — unchanged; no outcome marker**. Current return: `f'No changes for {path}'`
- Line 5667: **SUCCESS — unchanged; no outcome marker**. Current return: `f'No changes for {path}'`
- Line 5672: **PROPAGATE — existing permission/precondition/leaf/cap/publication helper outcome; make defining helper typed, do not overwrite its code**. Current return: `err`
- Line 5682: **PROPAGATE — existing permission/precondition/leaf/cap/publication helper outcome; make defining helper typed, do not overwrite its code**. Current return: `err`
- Line 5685: **EXISTING — concurrent_write sentinel, record same post-body usage marker**. Current return: `_concurrent_write_refusal(str(e), path)`
- Line 5686: **REFUSE validation_failed — existing generic ValueError combines path/size/policy cases; do not parse prose or narrow accepted inputs**. Current return: `str(e)`
- Line 5688: **REFUSE io_failure — preserve caught exception response, no exception text in telemetry**. Current return: `f'Failed to write {path}: {e}'`
- Line 5697: **SUCCESS — unchanged; no outcome marker**. Current return: `f'Updated frontmatter in {path} ({'; '.join(summary)})' + _published_hash_clause(new_raw.encode('utf-8'))`

### read_file_impl — src/mcp_server/tools.py:5756
- Line 5770: **REFUSE invalid_argument**. Current return: `f"Invalid encoding '{encoding}'. Use 'auto', 'text', or 'base64'."`
- Line 5772: **REFUSE invalid_argument**. Current return: `'read_file: hash_only cannot be combined with offset or limit windows.'`
- Line 5774: **REFUSE invalid_argument**. Current return: `f'read_file: offset must be >= 0 (got {offset}).'`
- Line 5778: **REFUSE invalid_argument**. Current return: `f'read_file: limit must be >= 1 (got {limit}).'`
- Line 5785: **REFUSE not_found**. Current return: `f'File not found: {path}'`
- Line 5787: **REFUSE validation_failed — existing generic ValueError combines path/size/policy cases; do not parse prose or narrow accepted inputs**. Current return: `str(e)`
- Line 5791: **SUCCESS — unchanged; no outcome marker**. Current return: `f'path: {json.dumps(path)}\nbytes: {len(data)}\nmime: {mime}\ncontent_hash: {content_hash_for_bytes(data)}'`
- Line 5800: **PROPAGATE — _capped_text decides success/truncation/window refusal**. Current return: `_capped_text(data.decode('utf-8'), path, offset, cap)`
- Line 5802: **REFUSE invalid_argument — requested UTF-8 decoding unavailable**. Current return: `f'Cannot decode {path} as UTF-8 text (not valid UTF-8). Use encoding="base64" for binary files.'`
- Line 5809: **SUCCESS — unchanged; no outcome marker**. Current return: `_base64_payload(path, data, mime)`
- Line 5815: **PROPAGATE — _capped_text decides success/truncation/window refusal**. Current return: `_capped_text(data.decode('utf-8'), path, offset, cap)`
- Line 5817: **SUCCESS — unchanged; no outcome marker**. Current return: `_base64_payload(path, data, mime)`
- Line 5821: **SUCCESS — unchanged; no outcome marker**. Current return: `Image(data=data, format=mime.split('/', 1)[1])`
- Line 5822: **SUCCESS — unchanged; no outcome marker**. Current return: `_base64_payload(path, data, mime)`

### write_file_impl — src/mcp_server/tools.py:5846
- Line 5862: **PROPAGATE — existing permission/precondition/leaf/cap/publication helper outcome; make defining helper typed, do not overwrite its code**. Current return: `err`
- Line 5864: **PROPAGATE — existing permission/precondition/leaf/cap/publication helper outcome; make defining helper typed, do not overwrite its code**. Current return: `_precondition_error('write_file', path, None, expected_hash, no_incumbent=True)`
- Line 5868: **PROPAGATE — existing permission/precondition/leaf/cap/publication helper outcome; make defining helper typed, do not overwrite its code**. Current return: `err`
- Line 5870: **REFUSE invalid_argument**. Current return: `f"Invalid encoding '{encoding}'. Use 'base64' or 'text'."`
- Line 5879: **REFUSE validation_failed — existing generic ValueError combines path/size/policy cases; do not parse prose or narrow accepted inputs**. Current return: `str(e)`
- Line 5893: **PROPAGATE — existing permission/precondition/leaf/cap/publication helper outcome; make defining helper typed, do not overwrite its code**. Current return: `err`
- Line 5902: **PROPAGATE — leaf refusal if present, otherwise io_failure; preserve short-circuit**. Current return: `_leaf_state_error(target, path) or f'Failed to read {path}: {exc}'`
- Line 5908: **PROPAGATE — existing permission/precondition/leaf/cap/publication helper outcome; make defining helper typed, do not overwrite its code**. Current return: `err`
- Line 5919: **REFUSE invalid_argument**. Current return: `'Invalid base64 content: could not decode. No file was written.'`
- Line 5935: **REFUSE size_limit — known bounded reader/content cap branch**. Current return: `f'Content too large ({len(data):,} bytes, max {cap:,} — {cap_name}). No file was written.'`
- Line 5948: **PROPAGATE — existing permission/precondition/leaf/cap/publication helper outcome; make defining helper typed, do not overwrite its code**. Current return: `err`
- Line 5950: **PROPAGATE — leaf refusal if present, otherwise already_exists; preserve short-circuit and precedence**. Current return: `_leaf_state_error(target, path) or f'File already exists: {path}. Pass overwrite=True to replace it.'`
- Line 5955: **EXISTING — concurrent_write sentinel, record same post-body usage marker**. Current return: `_concurrent_write_refusal(str(e), path)`
- Line 5956: **REFUSE validation_failed — existing generic ValueError combines path/size/policy cases; do not parse prose or narrow accepted inputs**. Current return: `str(e)`
- Line 5958: **REFUSE io_failure — preserve caught exception response, no exception text in telemetry**. Current return: `f'Failed to write {path}: {e}'`
- Line 5961: **SUCCESS — unchanged; no outcome marker**. Current return: `result + f' — hash not reported: incumbent or published file exceeds {read_cap_name} ({read_cap:,} bytes)'`
- Line 5965: **SUCCESS — unchanged; no outcome marker**. Current return: `result + _published_hash_clause(data)`

### list_files_impl — src/mcp_server/tools.py:5969
- Line 5983: **REFUSE invalid_path — directory required**. Current return: `str(e)`
- Line 5985: **REFUSE validation_failed — existing generic ValueError combines path/size/policy cases; do not parse prose or narrow accepted inputs**. Current return: `str(e)`
- Line 5989: **SUCCESS — unchanged; no outcome marker**. Current return: `f"No entries in '{where}' matching '{pattern}'"`
- Line 6012: **SUCCESS — unchanged; no outcome marker**. Current return: `'\n'.join(lines)`

### request_upload_impl — src/mcp_server/tools.py:6179
- Line 6187: **PROPAGATE — existing permission/precondition/leaf/cap/publication helper outcome; make defining helper typed, do not overwrite its code**. Current return: `pre`
- Line 6193: **REFUSE unsafe_path**. Current return: `str(e)`
- Line 6195: **REFUSE already_exists**. Current return: `f'File already exists: {rel}. Pass overwrite=True to replace it (the link will then refuse to publish if the file changes before the upload). Nothing was minted.'`
- Line 6230: **REFUSE credential_unusable — use typed CredentialNotUsable/PrePublishAborted; preserve pre-publication guarantee**. Current return: `f'{e} Nothing was minted.'`
- Line 6232: **SUCCESS — unchanged; no outcome marker**. Current return: `f'Upload link for \`{rel}\` (expires {_expiry_line(row)}):\n\n{base}/transfer/upload#{token}\n\n{_clamp_note(window)}upload_id: {row.public_id}\nmax_bytes: {cap:,} ({cap_name})\noverwrite: {overwrite}\n\nGive the URL to the person you are helping and ask them to open it — it is a page with a file picker. Treat it as a secret: anyone holding it can write this one path once, until it expires. From a shell you can upload directly instead:\n\n  curl -H "Authorization: Bearer <the part after the #>" -T <file> {base}/transfer/upload\n\nThen call \`check_upload("{row.public_id}")\` to confirm the bytes landed and get their sha256. Do not paste the token into a query string — that would put it in access logs.'`

### check_upload_impl — src/mcp_server/tools.py:6266
- Line 6272: **REFUSE invalid_argument**. Current return: `'not found: that is not an upload_id. \`check_upload\` takes the \`upload_id\` from \`request_upload\` (22 characters), not the upload URL and not the token after the \`#\`.'`
- Line 6314: **REFUSE not_found**. Current return: `f'not found: no upload link with id {upload_id} was minted by this identity.'`
- Line 6328: **SUCCESS — unchanged; no outcome marker**. Current return: `f'completed: {row.path}\nsize: {row.size:,} bytes\nsha256: {row.sha256}\nmime: {row.mime}\ncompleted_at: {_utc_stamp(row.completed_at)}'`
- Line 6350: **SUCCESS — unchanged; no outcome marker**. Current return: `f'uploading: someone is sending {row.path} right now (started {started}). The stream has until {_utc_stamp(deadline)}; check again after that and this tool will say whether the bytes landed.' + dead_note`
- Line 6356: **SUCCESS — unchanged; no outcome marker**. Current return: `f'unknown: an upload of {row.path} started ({started}) and the server never recorded how it finished. The bytes may already be in the vault — a publish can succeed and still fail to record its completion. Check \`{row.path}\` with \`list_files\` or \`read_file\` before you mint another link or tell anyone the file did not arrive. Do not assume nothing landed.' + dead_note`
- Line 6372: **SUCCESS — unchanged; no outcome marker**. Current return: `f'expired: the upload of {row.path} was cut short (it stalled or ran past its deadline) and the link is spent. Nothing was published: the deadline and idle-timeout paths abort before the bytes reach the vault. Call \`request_upload\` again for a fresh one.'`
- Line 6381: **SUCCESS — unchanged; no outcome marker**. Current return: `f'expired: the link for {row.path} was never used and can no longer be redeemed. Call \`request_upload\` again for a fresh one.'`
- Line 6386: **SUCCESS — unchanged; no outcome marker**. Current return: `f'revoked: the link for {row.path} is no longer redeemable — ' + ' and '.join(dead) + '. Nothing has been uploaded through it. Mint a new link with \`request_upload\` from a credential that still has write access.'`
- Line 6392: **SUCCESS — unchanged; no outcome marker**. Current return: `f'pending: nothing has been uploaded to {row.path} yet. The link is valid until {_expiry_line(row)}.'`

### request_download_impl — src/mcp_server/tools.py:6399
- Line 6403: **PROPAGATE — existing permission/precondition/leaf/cap/publication helper outcome; make defining helper typed, do not overwrite its code**. Current return: `pre`
- Line 6411: **REFUSE unsafe_path**. Current return: `str(e)`
- Line 6413: **REFUSE not_found**. Current return: `f'File not found: {rel}. Nothing was minted.'`
- Line 6418: **REFUSE io_failure — preserve caught exception response, no exception text in telemetry**. Current return: `f'Could not read {rel}: {e}. Nothing was minted.'`
- Line 6434: **REFUSE credential_unusable — use typed CredentialNotUsable/PrePublishAborted; preserve pre-publication guarantee**. Current return: `f'{e} Nothing was minted.'`
- Line 6436: **SUCCESS — unchanged; no outcome marker**. Current return: `f'Download link for \`{rel}\` (expires {_expiry_line(row)}):\n\n{base}/transfer/download#{token}\n\n{_clamp_note(window)}size: {fingerprint['size']:,} bytes\nmime: {mime}\n\nGive the URL to the person you are helping — it is a page with a save button, and it keeps working until it expires. Treat it as a secret: anyone holding it can read this one file. From a shell:\n\n  curl -H "Authorization: Bearer <the part after the #>" -o <file> {base}/transfer/download/file\n\nThe link is bound to the file as it is right now; if it is edited or replaced the link stops working and you should mint a new one.'`

### import_from_url_impl — src/mcp_server/tools.py:6488
- Line 6492: **PROPAGATE — existing permission/precondition/leaf/cap/publication helper outcome; make defining helper typed, do not overwrite its code**. Current return: `pre`
- Line 6498: **REFUSE unsafe_path**. Current return: `str(e)`
- Line 6500: **REFUSE already_exists**. Current return: `f'File already exists: {rel}. Pass overwrite=True to replace it. Nothing was fetched.'`
- Line 6547: **REFUSE fetch_refused — typed SSRFError**. Current return: `f'Refused to fetch that URL: {e}'`
- Line 6549: **REFUSE size_limit — known bounded reader/content cap branch**. Current return: `f'{e} ({cap_name}). Nothing was written.'`
- Line 6561: **REFUSE transfer_busy — queue wait expired; no invented retry interval**. Current return: `f'{e}. Nothing was written.'`
- Line 6563: **REFUSE transfer_timeout — transfer deadline/idle expiration**. Current return: `f'{e}. Nothing was written.'`
- Line 6565: **REFUSE credential_unusable — use typed CredentialNotUsable/PrePublishAborted; preserve pre-publication guarantee**. Current return: `f'Your credentials are no longer valid for writing to {rel} (the key was revoked, downgraded, or repointed while the fetch was in flight). Nothing was written.'`
- Line 6576: **PARTIAL partial_completion — bytes landed, bookkeeping failed; no nothing_written=true and no blind retry**. Current return: `f'Imported the file to {rel}, but the server could not finish recording the import: {e}\nThe file IS in place. Do not retry blindly — check it with \`read_file\` or \`list_files\` first.'`
- Line 6583: **REFUSE concurrent_write — known typed Conflict**. Current return: `f'{e}. Nothing was written.'`
- Line 6585: **REFUSE unsafe_path**. Current return: `f'{e}. Nothing was written.'`
- Line 6590: **REFUSE unsupported_filesystem**. Current return: `f'{e}. Nothing was written.'`
- Line 6592: **REFUSE io_failure — preserve caught exception response, no exception text in telemetry**. Current return: `f'Could not write {rel}: {e}'`
- Line 6594: **SUCCESS — unchanged; no outcome marker**. Current return: `f'Imported {written['size']:,} bytes to {rel}\nsha256: {written['sha256']}\nmime: {written['mime']}\nsource: {final_url}'`

### delete_file_impl — src/mcp_server/tools.py:6603
- Line 6620: **PROPAGATE — existing permission/precondition/leaf/cap/publication helper outcome; make defining helper typed, do not overwrite its code**. Current return: `err`
- Line 6622: **PROPAGATE — existing permission/precondition/leaf/cap/publication helper outcome; make defining helper typed, do not overwrite its code**. Current return: `err`
- Line 6627: **REFUSE validation_failed — existing generic ValueError combines path/size/policy cases; do not parse prose or narrow accepted inputs**. Current return: `str(e)`
- Line 6636: **REFUSE invalid_argument**. Current return: `f'{rel} is a markdown note. Use \`delete_note\` for notes — it is the tool that knows about the index and about backlinks. \`delete_file\` handles everything else.'`
- Line 6712: **PROPAGATE — existing permission/precondition/leaf/cap/publication helper outcome; make defining helper typed, do not overwrite its code**. Current return: `err`
- Line 6714: **REFUSE io_failure — preserve caught exception response, no exception text in telemetry**. Current return: `str(e)`
- Line 6716: **REFUSE unsupported_filesystem — typed UnsupportedFilesystem**. Current return: `str(e)`
- Line 6718: **REFUSE not_found**. Current return: `f'File not found: {rel}'`
- Line 6722: **REFUSE unsafe_path**. Current return: `str(e)`
- Line 6724: **REFUSE concurrent_write — known typed Conflict, not inferred from prose**. Current return: `f'{e}. Nothing was deleted.'`
- Line 6726: **REFUSE validation_failed — existing generic ValueError combines path/size/policy cases; do not parse prose or narrow accepted inputs**. Current return: `str(e)`
- Line 6728: **REFUSE io_failure — preserve caught exception response, no exception text in telemetry**. Current return: `f'Failed to delete {rel}: {e}'`
- Line 6730: **PROPAGATE — existing permission/precondition/leaf/cap/publication helper outcome; make defining helper typed, do not overwrite its code**. Current return: `precondition_refusal`
- Line 6732: **SUCCESS — unchanged; no outcome marker**. Current return: `f'Permanently deleted {rel}'`
- Line 6733: **SUCCESS — unchanged; no outcome marker**. Current return: `f"Moved {rel} to {dest}. It is out of the vault's visible tree but still on disk; pass permanent=True to unlink instead."`

## Shared/nested helper inventory

### _confirmed_publication — tools.py:706

Pure typed refusal for VaultAssignmentChanged (preserve vault_assignment_changed marker) and VaultAnchorUnavailable (preserve vault_anchor_lost_at_publish). Success returns (None, publish result), which may itself be typed _verify_the_moved_inode output. Do not convert successful publication into a refusal. In move rewrite loop, preserve semantic reason separately until final partial result.

- Return 741: `(None, await confirmed_publication(uid, publish))`
- Return 744: `(str(exc), None)`
- Return 760: `(str(exc), None)`

### _precondition_refusal — tools.py:2268

Construct pure BodyOutcome with existing precondition code/fields and marker equal to code. No timing/logging side effect. Its concurrent_write caller may pass nothing_written=None for partial move; carry partial disposition explicitly.

- Return 2286: `refusals.render(prose, refusals.Refusal(code=code, path=_precondition_path(path), current_hash=current_hash, cap_name=cap_name, cap_bytes=cap_bytes, nothing_written=nothing_written))`

### _precondition_syntax_error — tools.py:2299

None means proceed. Non-None refusal is malformed_precondition; preserve pure forwarding.

- Return 2316: `None`
- Return 2318: `_precondition_refusal(f"{tool}: expected_hash{where} is not a content hash this server can compare. The one accepted form is \`{_PRECONDITION_CANONICAL_FORM}\` — the exact value a read returns, prefix included. It was not compared against the file and nothing was written. {_PRECONDITION_READ_HINT} (Note: \`notes_metadata\`'s own content hash is a different digest of different bytes and is not accepted here.)", refusals.MALFORMED_PRECONDITION, path=path)`

### _precondition_error — tools.py:2330

None means proceed. syntax forwards malformed_precondition. The other returns are no_incumbent, precondition_unavailable, precondition_required, stale_precondition exactly as Refusal already specifies. Direct pure forwarding remains safe. Runtime ValueError for missing incumbent wiring bug must still raise.

- Return 2366: `syntax`
- Return 2372: `None`
- Return 2373: `_precondition_refusal(f'{tool}: there are no existing bytes at {path} for expected_hash to bind — nothing was overwritten, so there was nothing to guard. Nothing was written. Call again without expected_hash if you meant to create this file.', refusals.NO_INCUMBENT, path=path)`
- Return 2394: `None`
- Return 2395: `_precondition_refusal(f"{tool}: {path} is larger than {cap_name} ({cap_bytes:,} bytes), the most this tool may read, so its current bytes cannot be hashed and no precondition can be checked. Nothing was written. Raising {cap_name} is an operator action; to fetch this file's bytes, use the transfer download route (\`request_download\`).", refusals.PRECONDITION_UNAVAILABLE, path=path, cap_name=cap_name, cap_bytes=cap_bytes)`
- Return 2422: `None`
- Return 2423: `_precondition_refusal(f'{tool}: this deployment requires every write to name the bytes it is replacing (WRITE_PRECONDITION_REQUIRED). Resend with expected_hash. Nothing was written. {path} currently hashes to {current_hash}, which you may send as expected_hash if nothing changes in between.', refusals.PRECONDITION_REQUIRED, path=path, current_hash=current_hash)`
- Return 2435: `None`
- Return 2437: `_precondition_refusal(f'{tool}: {path} has changed since the hash you supplied was taken, so this write was refused rather than applied to bytes you have not seen. Nothing was written. Its current content_hash is {current_hash} — re-read the file, recompute the write from its current bytes, and resend; the hash named here may be sent as expected_hash if nothing else changes in between. {_PRECONDITION_READ_HINT}', refusals.STALE_PRECONDITION, path=path, current_hash=current_hash)`

### _concurrent_write_refusal — tools.py:2450

Forward typed concurrent_write. Add explicit kind parameter or derive kind solely from trustworthy caller intent, not prose. Existing nothing_written=None use at moved-note rewrite conflict is partial.

- Return 2469: `_precondition_refusal(prose, refusals.CONCURRENT_WRITE, path=path, nothing_written=nothing_written)`

### _require_write — tools.py:2515

None for readwrite. Pure typed permission_denied otherwise; existing tool_write_refused event policy must be coordinated with central summary emission, without duplicate outcome recording.

- Return 2543: `None`
- Return 2558: `"Permission denied: this credential has read-only access. Write permission is required — a 'readwrite' API key, or an OAuth token carrying the 'readwrite' scope."`

### _leaf_state_error — tools.py:2565

None for acceptable leaf. missing argument return, if non-None: not_found. Symlink/nonregular leaves: unsafe_path. OR fallbacks at create/write/move must construct already_exists only when this returns None. No-clobber short-circuit stays intact.

- Return 2584: `missing`
- Return 2586: `f'{path} became a symbolic link after it was validated — mutating tools act only on the named file. Nothing was changed.'`
- Return 2591: `f'{path} is not a regular file. Nothing was changed.'`
- Return 2592: `None`

### _verify_the_moved_inode — tools.py:2632

Returns at 2670,2676,2684,2690: publication_uncertain, kind=partial, omit nothing_written. Return None at 2696 means successful verified move. Return 2713 rollback failure: partial_completion, kind=partial. Return 2718 rollback successful: concurrent_write or unsafe_path, kind=refused; conservatively omit nothing_written since rename/rollback occurred. The central classifier must never falsely report a plain unattempted write.

- Return 2669: `f'Move published but unverifiable: {from_path} was moved to {to_rel} and the result could not be inspected ({exc}). Nothing was reindexed; check both paths before retrying.'`
- Return 2675: `f'Move published but {to_rel} is already gone: something removed or replaced it immediately after {from_path} was moved there. Nothing was reindexed; check both paths before retrying.'`
- Return 2683: `f'Move published but {to_rel} is not the file that was moved: something else took that name immediately afterwards. Nothing was reindexed and nothing was moved back — check both paths before retrying.'`
- Return 2690: `f'Move published but unverifiable: {from_path} could not be identified before it was moved to {to_rel}. Nothing was reindexed; check both paths before retrying.'`
- Return 2696: `None`
- Return 2712: `f'Move refused: {from_path} was replaced by {kind} after it was checked, and it could not be moved back ({exc}). It is now at {to_rel} — restore it from there. Nothing was reindexed.'`
- Return 2717: `f'Move refused: {from_path} was replaced by {kind} after it was checked. It was moved back and nothing was reindexed.'`

### _note_size_error_for — tools.py:2723

None for under cap. Pure size_limit error otherwise. This value is returned directly in ordinary write paths but interpolated into move preflight prose at 4891: reconstruction required, remove old sentinel from prose by accessing explicit .prose rather than splitting arbitrary user text.

- Return 2731: `f'Content too large ({size} bytes, max {MAX_NOTE_BYTES})'`
- Return 2732: `None`

### _note_size_error — tools.py:2735

Directly delegates to _note_size_error_for; preserve type.

- Return 2742: `_note_size_error_for(len(content.encode('utf-8')))`

### _frontmatter_defect_error — tools.py:2745

content_unsafe. The diagnosis message remains only in original bounded/prose response, never telemetry.

- Return 2759: `f'{tool}: {path} has a malformed frontmatter block — {diagnosis.message}. Nothing was written. Read the note, then repair the whole file with \`edit_note(path, content=<complete note text>, replace_frontmatter=True)\`, which replaces the frontmatter block along with the body.'`

### _unmatched_fence_error — tools.py:2810

None if decidable. content_unsafe otherwise. Direct use by edit_note; move performs independent collected-source branch.

- Return 2821: `None`
- Return 2823: `f'edit_note: {path} contains an indented fence opener that nothing below it closes — {where}. Nothing was written. A fence indented by one to three spaces may be inside a list item, whose code block ends where the item does; this server does not parse container blocks, so it cannot tell whether the text below the opener is code or content, and a section write there would either split the block or replace real content. Close the fence (or unindent it to column zero), then reissue the section write. \`read_note(path, section=...)\` still works, and a whole-note \`edit_note\` without \`section=\` is unaffected.'`

### _move_precondition_error — tools.py:2891

None if admitted; otherwise existing precondition code/marker/kind. CURRENT TYPE-LOSS SITE: rpartition + f-string rebuild at 2933/2934. Use pure outcome.with_prose(original prose + scope), retaining one final sentinel and metadata. Do not split or inspect arbitrary returned strings in _tracked.

- Return 2921: `None`
- Return 2933: `f'{err}{scope}'`
- Return 2934: `f'{prose}{scope}\n{line}'`

### _stale_extraction_error — tools.py:4385

None if generation current. index_not_ready refusal otherwise. All checks remain owner scoped; no extra queries for classification.

- Return 4415: `None`
- Return 4416: `f"Move aborted: this vault's index is still being re-derived after a note-parsing change, so the link graph is not yet a trustworthy list of the notes that link here — a link the previous parser read as code has no row yet, and rewriting would silently leave it pointing at the old path (first note still pending: {stale[0].file_path}). Nothing was moved, rewritten or reindexed. The re-derivation runs automatically on the indexer's next pass (within about five minutes); retry after that, or move now with rewrite_links=False and update the links yourself."`

### _capped_text — tools.py:5727

Full text return at 5730 and chunk+notice at 5752 are SUCCESS (including truncation). EOF response at 5734 and past-end at 5739 are read_window_unavailable. They currently look like plain prose; returning BodyOutcome here preserves metadata through read_file return. Empty text with offset=0 remains success.

- Return 5730: `text`
- Return 5734: `f'read_file: offset {offset:,} is exactly the end of {path} ({len(text):,} chars) — the whole file has been read, there is nothing further.'`
- Return 5739: `f'read_file: offset {offset:,} is past the end of {path} ({len(text):,} chars).'`
- Return 5752: `chunk + notice`

### _mint_preflight — tools.py:6098

Return err at 6120 preserves permission_denied. Missing PUBLIC_BASE_URL (6123) is transfer_unavailable. ValueError at 6128 is validation_failed, RuntimeError at 6130 transfer_unavailable/vault_anchor_lost_at_publish according to explicit code semantics (do not reuse PRE_BODY no_vault). UnsupportedFilesystem at 6146 is unsupported_filesystem; OSError/VaultFSError at 6148 is io_failure. Success tuple remains unchanged. isinstance(pre,str) accepts a BodyOutcome subclass and forwards it intact.

- Return 6119: `err`
- Return 6122: `_NO_PUBLIC_ORIGIN`
- Return 6127: `str(e)`
- Return 6129: `str(e)`
- Return 6146: `str(e)`
- Return 6148: `f'Vault root is not usable: {e}'`
- Return 6149: `(uid, root, rel, base.rstrip('/'))`

### Nested helpers

- read_note_impl._fail (1870, return 1883): accepts explicit code or BodyOutcome; sanitize/truncate only .prose, reserve space for final sentinel, then attach pure outcome via ReadNoteResult PrivateAttr. Success `result` at 1971 has no private outcome. `_read_note_refusal` (1797) is the PRE-BODY adapter and stays separately classified by _tracked's admission path.
- _move_note_locked.drop (4662), _move_rewrite_gate (4511), and descriptor cleanup: no tool result. Keep unmarked.
- _move_note_locked._commit_the_move (4961, return 4972): forwards `_verify_the_moved_inode` result through `_confirmed_publication`; preserve subtype. Callback does not independently emit final outcome.
- delete_note_impl._probe_then_soft_delete (5380, return 5394): its returned destination is SUCCESS. _TrashUnusable exceptions are classified only at outer catch 5403; UnsupportedFilesystem at 5405. Keep callback behavior.
- import_from_url_impl.gate: no response; retains original publish locks and exception behavior.
- delete_file_impl._delete (6660): return None at 6684 means precondition refusal recorded in nonlocal `precondition_refusal` and surfaced at 6730; return None at 6690/6702 means successful permanent delete. Returns 6691/6703 are successful soft-delete destinations. NEVER classify None alone as failure. The outer return distinguishes them already.
- delete_file_impl._check_trash: throws typed _TrashUnusable/UnsupportedFilesystem; outer catches classify. No outcome on a successful probe.

Formatting-only helpers (`_window`, `_bounded`, `_scalar_safe`, `_origin_label`, `_read_notice`, `_base64_payload`, `_published_hash_clause`, `_hash_unavailable_clause`, `_clamp_note`, `_link_excerpt`, `_degradation_*`, `_stale_source_line`, `_rewrite_failure_warning`) remain ordinary strings/data. They may describe warnings on SUCCESS; never infer a failure from their contents. Service helpers `_vault_context`, `_fingerprint_of`, `_head_bytes`, `_read_incumbent`, `_rewrite_links_in_text`, `_splice_rewrites`, `_ensure_move_source_in_index`, and source inode context managers return data or raise. Existing catches above or _tracked tool_exception classify them; don't add generic catch-and-convert behavior.

## _move_note_locked complete return inventory and nonterminal outcomes

This function is the actual move implementation, reached via the one forwarded return in move_note_impl.
- Return 4569: **validation_failed**. `str(e)`
- Return 4571: **io_failure**. `f'Could not open {from_path} or {to_path}: {e}'`
- Return 4577: **propagate _leaf_state_error**. `err`
- Return 4594: **io_failure**. `f'Failed to read {from_path}: {e}'`
- Return 4596: **not_found**. `f'Source note not found: {from_path}'`
- Return 4600: **propagate _move_precondition_error**. `err`
- Return 4643: **propagate _stale_extraction_error**. `err`
- Return 4697: **resource_limit**. `f'Move aborted: ran out of file descriptors before the link rewrites could be planned ({e}). Nothing was moved, rewritten or reindexed.'`
- Return 4785: **resource_limit**. `f"Move aborted: ran out of file descriptors while planning the link rewrites ({e}). Nothing was moved, rewritten or reindexed. Move without rewrite_links and update links in batches, or raise the process's RLIMIT_NOFILE."`
- Return 4817: **unsafe_path (typed VaultRootMismatch)**. `f'Move aborted: {e} Nothing was moved, rewritten or reindexed.'`
- Return 4829: **resource_limit (MAX_LINKS_PER_NOTE)**. `f"Move aborted: rewriting links in {e.source_path} would change {e.count} links, more than the per-note limit (MAX_LINKS_PER_NOTE={e.cap}). Nothing was moved, rewritten or reindexed. Move without rewrite_links and update that note's links in batches instead."`
- Return 4864: **content_unsafe (nested rewrite overlap)**. `f"Move aborted: {original_src_path} holds a link to {from_rel} nested inside another link to it, and rewriting either would corrupt the other. Nothing was moved, rewritten or reindexed. Move without rewrite_links, or rewrite that note's nested link by hand first."`
- Return 4891: **size_limit — reconstruct outer prose from helper .prose without nested sentinel**. `f'Move aborted: rewriting links in {original_src_path} would exceed the note size limit ({err}). Nothing was moved, rewritten or reindexed.'`
- Return 4898: **resource_limit (MAX_MOVE_REWRITE_BYTES)**. `f'Move aborted: rewriting links across {len(planned_rewrites) + 1} notes would need {rewrite_bytes_held} bytes in memory (limit {MAX_MOVE_REWRITE_BYTES} bytes, {MAX_MOVE_REWRITE_BYTES // (1024 * 1024)} MiB). Nothing was moved, rewritten or reindexed. Move without rewrite_links and update links in batches instead.'`
- Return 4918: **resource_limit (descriptor budget)**. `f'Move aborted: rewriting links across more than {fd_budget} notes would hold more open file descriptors than this process can spare. Nothing was moved, rewritten or reindexed. Move without rewrite_links and update links in batches instead.'`
- Return 4938: **content_unsafe (undecidable fences)**. `f'Move aborted: rewriting links would touch {len(undecidable_sources)} note(s) containing an indented fence opener that nothing below them closes — {where}. Nothing was moved, rewritten or reindexed. A fence indented by one to three spaces may be inside a list item, whose code block ends where the item does; this server does not parse container blocks, so it cannot tell whether a link below the opener is code or content. Close the fences (or unindent them to column zero), or move with rewrite_links=False and update the links yourself.'`
- Return 4980: **propagate leaf refusal or already_exists fallback**. `_leaf_state_error(dst_target, to_path) or f'Destination already exists: {to_path}'`
- Return 4984: **not_found**. `f'Source note not found: {from_path}'`
- Return 4986: **validation_failed (ValueError/VaultFSError)**. `f'Move failed: {e}'`
- Return 4988: **io_failure**. `f'Move failed: {e}'`
- Return 4990: **propagate confirmation outcome**. `err`
- Return 4992: **propagate verification outcome; includes partial and refused**. `verify_error`
- Return 5265: **PARTIAL concurrent_write — existing caller code/hash omission remains**. `_concurrent_write_refusal(' — '.join(parts), to_rel, nothing_written=None)`
- Return 5276: **SUCCESS if complete; PARTIAL partial_completion if db_failed or failed_rewrite_sources or stopped; retain more-specific existing publication marker if present**. `' — '.join(parts) if len(parts) > 1 else parts[0]`

Nonterminal paths requiring final partial classification:

- Preflight unreadable/skipped rewrite sources append failed_rewrite_sources and continue (4799-4810, 4871-4884). They are not whole-move refusals; if the rename ultimately stands, the returned move is partial.
- Post-rename DB update failure sets db_failed (5117-5130), still returns a moved result: partial_completion.
- Publication confirmation failure within rewrite loop sets outcome=reassigned/unavailable and stops remaining rewrites (5150-5230). It currently records more-specific timing error; central outcome must preserve that marker, attach kind=partial and describe actual completed rename.
- Ordinary rewrite failure appends a source and continues. Final joined response must carry a partial outcome; source-specific events can remain separate from one summary event but must not multiply summary rows.
- Moved note's own rewrite conflict returns _concurrent_write_refusal at 5265: kind=partial, nothing_written absent, hash absent. A backlink-only conflict cannot invalidate the moved note's destination hash and should remain partial_completion unless a more-specific explicit marker is present.
- `_rewrite_failure_warning` is only prose about these facts. Compute kind from flags/lists, not by matching its output.

## Pure-outcome metadata propagation audit

Safe forwarding: `return err`, `return pre`, `return verify_error`; `_leaf_state_error(...) or fallback` if each fallback constructs its own outcome; `_confirmed_publication` tuple result; `_precondition_syntax_error` -> `_precondition_error`; read_file return `_capped_text`; _mint_preflight -> mint tools via isinstance(str).

Required explicit reconstruction:

1. `_move_precondition_error`: rpartition and f-string currently produce base str. Preserve metadata via with_prose and explicit original .prose.
2. `_move_note_locked` cap refusal at 4891 wraps `_note_size_error_for` inside larger prose. Without reconstruction it loses type and embeds the sentinel mid-sentence. Use `.prose` in the inner position and construct a size_limit outcome for outer response.
3. read_note._fail sanitizes/truncates and wraps in Pydantic model. Preserve private outcome after rendering under budget; normal model serialization must not expose new fields. Test copy/model_dump and FastMCP actual output serialization.
4. Move final `parts` join consumes warning strings and discards intermediate `_confirmed_publication` error into a textual outcome variable. Reconstruct outcome from flags and explicit inherited reason; never assume arbitrary joins retain a subclass.
5. Existing `_concurrent_write_refusal` forwards _precondition_refusal but must carry kind=partial where caller passes post-rename intent. No other concatenation follows this existing final sentinel path.
6. Any helper wrapping an already typed outcome through `refusals.render` must preserve type explicitly; do not rely on renderer idempotence (`has_sentinel` only checks text). Keep renderer pure and BodyOutcome construction as the authoritative classifier.

A final AST/manual diff sweep must check every added `str(outcome)`, f-string, concatenation, join, bounded/safe conversion, and Pydantic wrapping touching a typed value. Raising or serializing a BodyOutcome should not accidentally leak private metadata. Avoid adding side effects to `__str__` or to property reads.

## Verification matrix

- Every listed refusal site has a terminal explicit code (table-driven/fault-injected groups are fine); each of 25 tools appears in a coverage registry.
- Body refusal yields original prose plus one final parseable sentinel; usage marker belongs to closed body register; one row, post-body latency inclusion and quota consumed.
- Six precondition codes across create/edit/set_frontmatter/move/write_file/delete_note/delete_file, existing permission/provider/related/confirmation markers retained.
- ReadNoteResult schema unchanged; min configured metadata budget (1000), long Unicode path/section/errors, no content on errors, sentinel not truncated. Raw UTF-8/body/base64/image success with forged sentinel remains success.
- No-result searches and lists, no-op updates/diffs, empty note/section/file at offset zero, stale/truncated search annotation and over-cap hash-unavailable successful write remain success.
- check_upload valid statuses including unknown/expired/revoked remain successful; malformed/foreign handle refusals retain handle redaction.
- Partial move: DB failure, preflight skipped source, backlink rewrite failure, stopped confirmation, own rewrite conflict, failed/successful rollback and unverifiable destination; correct surviving path/hash, no fabricated nothing_written. Import PostPublishFailure is partial and does not encourage retry.
- Eager pure helper construction then successful return causes no usage marker/event. Concurrent calls, nested calls, to_thread work, exception after constructing an outcome, cancellation, telemetry failure: no leakage or successful-write failure.
- Event secret canaries: invalid upload URL/token, notes/queries, exceptions containing SQL parameters and paths absent from new fields/events. Existing suppression and AST event allowlist gates pass.
- Offline focused tests, real-Postgres usage/performance predicates and attribution integration; independent specification verification and adversarial pass required for the write/transfer surface.

## Read scope and boundaries

Reviewed CLAUDE.md/shared workflow plus relevant usage-attribution, security-event logging, vault-tool publication and transfer contracts. This task authorizes an implementation/PR, not a change to HTTP transfer redemption status contracts, quota ordering, write preconditions, filesystem permissions, accepted limits, R10 durability-warning policy or mutation behavior. No new migrations, dependencies or network services are necessary for #263.
