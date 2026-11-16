Refs #430 (follow-up to #428, v1.3.0, and #434)

## Why

#428 shipped `add_task`, `complete_task` and `update_task`, but no way to move a task. The commonest task flow in an agent-kept vault is triage: tasks are captured in an inbox note and later filed into the project notes they belong to. Today an agent does that with `add_task` in the project note followed by an `edit_note` of the inbox, which:

- re-serialises the line (`add_task` builds a canonical line from arguments, so field order, rule text, block links, `🆔`/`⛔`/`🏁` fields and the status are lost or changed);
- drops the continuation block (the indented and blockquote lines under the task: context, sub-tasks, notes), unless the agent copies them by hand with a second `edit_note`;
- removes the source line with a whole-note or section text edit that the agent composes itself, the most expensive failure this product has (an edit that clobbers a note);
- has no defined state after a failure between the two calls, and nothing tells the agent which state it is in.

A move is a two-note write. Its order of publication, what a failure between the two leaves behind, and how the caller completes an interrupted move must be designed, not left to each agent. The bar, from the issue: **a duplicate is acceptable, a loss is not.**

## What Changes

- **A new tool, `move_task(path, id, to_path, to_section=None, allow_duplicate=False, resume=False, dry_run=False, expected_hash=None, to_expected_hash=None)`.** It moves one top-level task line, with its whole uncapped continuation block (indented sub-tasks and context included), from one note to another note, or to another place (`to_section`) in the same note.
- **The smaller first version (owner decision, Max, 2026-11-16).** The moved task must be top-level and unquoted, and its block may hold no blockquote, fence or heading line; a destination region whose last top-level task is quoted is refused too. Each such case is refused `task_malformed`, nothing written, with a pointer to `edit_note`. Nested and callout tasks, and the prefix re-framing they need, are the documented next step in `design.md`, not this change.
- **Destination first, source second.** The destination insert is published before the source removal. Immediately before the source is written, in the same synchronous step, the destination is re-read and must still hold the moved lines where they were written; otherwise the source is left alone. A failure or conflict at the second step returns a distinct, parseable partial outcome, `task_move_incomplete`, which states only what the tool verified about each note and names the exact call that completes the move. An identical block already at the destination is never taken as proof of an earlier move: it is refused as a duplicate, and completing a partial move is an explicit `resume=true`, which removes the source copy only when exactly one byte-identical copy is at the destination. One window remains in which an outside save of an older destination can lose the task (during the source write itself); it is stated, not hidden.
- **Each note keeps its own file permissions.** Both publications replace an existing file, so under #447 each keeps its own mode, ACL and group, and neither is ever published under the folder's create rule. A permission change during the call is an in-call conflict on that note; a permission rule that cannot be applied publishes nothing, so at the destination it is a nothing-written `io_failure` and at the source a partial outcome (`permissions_not_applied`) completed by `resume=true`.
- **Byte-exact.** The moved lines arrive with their content bytes unchanged; only their line terminators follow the destination. The moved task lands as a top-level task of the destination region, placed where `add_task` would place a new task.
- **The same verification as the other task writes**, for both notes: every other line and task unchanged, no line changes its owning task, code status and headings unchanged, every moved task re-extracts exactly as it did at the source.
- **Same note, one publication.** When the destination is the source note, removal and insertion are one plan, verified once and published once.
- **Contract surface.** 32 tools become 33; `move_task` joins the write bucket (12), `WRITE_REQUIRED_TOOLS` (13), the destructive set and the `write` concurrency class (`CLASS_MAPPING_VERSION` 7). A read-only OAuth grant still lists 20. A new full-list oracle `tools_list_full_v3.json`, from which v2 is derived. One sentence in `SERVER_INSTRUCTIONS`, one in the vault-guide primer.
- **New refusal codes:** `task_move_incomplete` (a partial outcome, never `nothing_written`, with four optional fields on the `MCP-REFUSAL` line: `destination_path`, `source_id`, `destination_id`, `reason`), and `resume_unmatched` and `block_id_conflict` (nothing written).
- **Owner decisions (Max, 2026-11-16):** every recommendation in `design.md` accepted as written (Q0 the smaller first version, Q3–Q10); Q1 and Q2 do not apply to the smaller version.

## Non-goals

- Moving several tasks in one call, or moving a task by a query. One call moves one task (and the sub-tasks under it).
- Editing the task while moving it (dates, status, tags, text). `update_task` before or after does that.
- Moving a nested task, a task inside a callout, or a block holding a blockquote, fence or heading line, or moving into a region whose last top-level task is quoted. Refused in this version; the full design in `design.md` is the documented next step.
- Re-numbering ordered lists, rewriting links or block references that point at the moved lines, or applying the global filter text (#434) to a moved line.
- A delete-task tool, or a new change-feed event kind.
- Hardening the shared publish path's residual window (issue #431). It is inherited, twice, and stated.
- Any schema change. No migration, no extraction-version bump.

## Capabilities

### New Capabilities

None. `move_task` belongs to the existing `task-write` capability.

### Modified Capabilities

- `task-write`: the `move_task` tool: arguments, resolution, the scope refusals, placement, verification, file permissions, the publication order and every failure state, the destination presence check, `resume`, duplicates, refresh, events, usage and response.
- `mcp-request-routing`: tool count, annotations, the write-required registry, the fixtures and oracle.
- `mcp-rate-limits`: `move_task` joins the write bucket, charged once per call.
- `change-feed`: `move_task` records one `modified` event per note it published.
- `vault-guide`: the primer and the instructions name `move_task`.
- `readme-positioning`: the README lists `move_task`.

## Impact

- **Code:** `src/services/task_write.py` (new pure planners and verifiers; the existing ones unchanged), `src/services/refusals.py` (three codes, four optional fields), `src/mcp_server/tools.py` (`move_task_impl` and its two-phase runner), `src/mcp_server/server.py` (registration, description, instructions), `src/mcp_server/tool_permissions.py`, `src/services/concurrency.py`, `src/mcp_server/vault_guide_primer.md`.
- **Fixtures and count-pinning tests:** listed exhaustively in design D17.
- **Schema:** none.
- **Behaviour:** additive. No existing tool's name, schema, title, annotations or description changes; the instructions gain one clause outside the lead. `CLASS_MAPPING_VERSION` 6 → 7 starts a new concurrency-readiness epoch.
- **Docs:** README, `CLAUDE.md`, `DEPLOYMENT.md`, `docs/deployment-kubernetes.md`, `docs/architecture/vault-tools.md` (including the "Published file permissions (#447)" list of edit-rule writers), `rate-limits.md`, `usage-attribution.md`, `indexing-and-embeddings.md` (the refresh runs per note).
- **Base:** rebased onto `main` at fa27234, after #443 (sync-conflict exclusion), #444 (task refresh after generic writes) and #448 (published file permissions); see `design.md`, "Spec review record".
