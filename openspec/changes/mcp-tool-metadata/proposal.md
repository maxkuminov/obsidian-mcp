Refs #343

## Why

All 25 tools are registered as bare `@mcp.tool()` (`src/mcp_server/server.py`, lines 113–1252 on 5a391ca): no `title`, no `ToolAnnotations`, and the `FastMCP(...)` at line 92 is constructed without `instructions=`. The pinned SDK (`mcp[cli]==1.30.0`, `requirements.txt:21`) exposes all three — `FastMCP.tool(name, title, description, annotations, icons, meta, structured_output)` and `FastMCP(name, instructions, …)` — and `FastMCP.list_tools` copies `title` and `annotations` straight into the `tools/list` response.

Three things follow from the gap, none of them a server-side bug and all of them a worse experience for the agent that is this product's consumer:

1. **The spec defaults are the worst case.** A client that honours annotations reads an absent `readOnlyHint` as `false`, an absent `destructiveHint` as `true` and an absent `openWorldHint` as `true`. So every one of our fifteen read tools is treated as a potentially destructive, open-world call. Claude's connector review checklist and OpenAI's plugin guidelines both require `readOnlyHint: true` on read-only tools and `destructiveHint: true` on tools that modify or delete, because those hints drive auto-permissions (a read-only tool runs without a per-call confirmation; a destructive one always prompts), and Claude Code runs `readOnlyHint` tools in parallel with other read-only calls. Today a `keyword_search` prompts like a `delete_note`.
2. **The descriptions are too long for the clients that read them.** FastMCP publishes `fn.__doc__` verbatim — `Tool.from_function` does `func_doc = description or fn.__doc__ or ""` — so the wire description is the *raw* docstring, indentation included. Measured on 5a391ca the 25 raw descriptions total **54,548** characters (51,335 after `inspect.cleandoc`); `edit_note` is **10,458** raw (9,857 clean), `read_note` 5,400 (5,091), `move_note` 5,316 (5,019), `set_frontmatter` 3,764 (3,551). Claude Code cuts an upfront tool description at 4,096 characters (16,384 for a searched tool), so `edit_note`'s contract — the modes, the two section-mode refusals, `expected_hash` and the `MCP-REFUSAL` codes — is truncated before the model ever sees it; and under Claude Code's default tool search only tool names and the server instructions are loaded at session start, where we publish none.
3. **There is no server-level orientation.** `get_vault_guide` exists for this, but a client has to know to call it; `instructions` is the field the protocol provides for "what this server is and how to start", and it is the only text some clients load before the first tool call.

The product framing in `CLAUDE.md` is that the consumer is an agent and the expensive failures are destructive writes and silently wrong results. Tool metadata is the cheapest lever on the first: a client that can tell `read_note` from `delete_note` without a confirmation dialog on each is a client whose human will still be reading the dialogs when one matters.

## What Changes

- **Every registered tool carries a `title` and a `ToolAnnotations` with all four hints set explicitly** (`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`), through the public `FastMCP.tool(title=…, annotations=…)` kwargs on each registration — no SDK internals. The fifteen read tools are `readOnly=true, destructive=false, idempotent=true, openWorld=false`. The eight write-bucket tools (`_tracked(write_class=True)`: `create_note`, `edit_note`, `move_note`, `delete_note`, `set_frontmatter`, `write_file`, `delete_file`, `import_from_url`) are not read-only; of those, every tool except `create_note` is `destructiveHint=true`, and `import_from_url` alone is `openWorldHint=true`. `request_upload` and `request_download` mint capabilities (a `transfer_tokens` row), so they are not read-only; `request_download` is additive, and `request_upload` is `destructiveHint=true` because its `overwrite=True` capability overwrites an existing file when redeemed (owner decision, Codex spec round 1). So the destructive set is the write-bucket set minus `create_note` plus `request_upload` — eight tools. An undo path through `.trash/` does **not** make a delete non-destructive. The full table, with a justification per tool, is design D1. **Annotations are a client-side hint.** Enforcement of read-only credentials stays exactly where it is — `_tracked` resolves the vault root and runs the admission gates before any body (`tools.py:1454`), `_require_write` refuses a read-only credential inside every write body (`tools.py:2821`), and `current_permission` is set from the API key's permission or the OAuth scope (`auth.py:955`, `auth.py:1178`). A client that ignores the hints loses nothing but convenience.
- **The server is constructed with `instructions`**: one module-level constant, ≤ 2,000 characters, whose first 512 characters say that this is agent memory in a markdown vault, name `keyword_search` / `semantic_search` for search, `read_note` for reading, `create_note` / `edit_note` (with `dry_run` first) for writing, point at `get_vault_guide` as the once-per-session orientation in neutral framing, state that `create_note` never overwrites, and describe `expected_hash` as optional, caller-supplied stale-read protection — a guarded edit is refused, an unguarded one overwrites. The read-to-write round trip is stated with its conditions (whole-note `content` only from a complete read with `offset=0` and `truncated=false`; a section's `content` only back to the same `section=`; finish a truncated read first), never as "pass `content` back unchanged". The text is in design D3; a test pins both limits, the lead's content and the round-trip conditions.
- **No published tool description exceeds 4,000 characters**, measured on the wire string (`tool.description` from the server's own registry, i.e. the raw docstring). The three over the cap (`edit_note`, `read_note`, `move_note`) and `set_frontmatter` are restructured so the first 1,500 characters carry the contract — every argument, the write-permission precondition, `expected_hash` and the `MCP-REFUSAL` line for the write tools; the field names and `error` for `read_note` — and the narrative moves to `docs/architecture/vault-tools.md` and the README. Design D4 lists, paragraph by paragraph, what stays, what moves and where. **No argument name, default or behaviour changes**, and every sentence an existing spec requires of these docstrings stays (design D5 enumerates them, with their pinning tests). The `tools.py` implementation docstrings are not on the wire and are not shortened.
- **No behaviour change, proven by snapshot**: `tools/list` returns the same 25 names in the same order, and every tool's `inputSchema` and `outputSchema` is identical to a fixture generated on `main` at 5a391ca.
- **Documentation**: a paragraph in README "What's in the box" on titles, annotations and what clients do with them; a new section in `docs/architecture/vault-tools.md` recording the mapping, its relationship to the write-bucket set, and the rule "annotations are hints; `_tracked` enforces"; one key-decisions bullet in `CLAUDE.md`.

No migration, no dependency, no new setting (grep-checked: nothing to add to `.env.example` or the README settings tables).

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `mcp-request-routing` (the spec that already governs the published tool surface — "Tool calls with undeclared arguments are refused" lives there):
  - ADDED "Every registered tool SHALL publish a title and a complete set of behaviour annotations".
  - ADDED "The server SHALL publish bounded instructions whose lead states the contract".
  - ADDED "No published tool description SHALL exceed 4,000 characters, and the restructured descriptions SHALL lead with their contract".
  - ADDED "Tool metadata SHALL leave the published tool surface otherwise unchanged".
- `vault-guide`:
  - MODIFIED "Other tools reference the guide neutrally" — the neutral-framing rule now covers the server instructions as well as peer-tool docstrings.

## Impact

- `src/mcp_server/server.py`: `instructions=` on the `FastMCP(...)` constructor; `title=` and `annotations=` on all 25 registrations; four shared `ToolAnnotations` constants; the four restructured docstrings; the `SERVER_INSTRUCTIONS` constant.
- `src/mcp_server/tools.py`: `_tracked` stamps its wrapper with `__write_class__` (beside the existing `__tracked_tool__`), and `production_write_bucket_tools()` enumerates the stamped production implementations (module attributes of `tools` whose `__module__` is `tools`), so the test can reconcile the destructive set against the bucket set without a hand-kept list and without a global that test probes could pollute (design D2). No behaviour change.
- Tests: one new module (`tests/test_issue_343_tool_metadata.py`) and one fixture (`tests/fixtures/tools_list_schema_5a391ca.json`). The existing modules that read registered descriptions (`test_issue_150_docstrings.py`, `test_issue_149_read_note_framing.py`, `test_issue_128_edit_note_frontmatter.py`, `test_issue_89_tool_names_in_copy.py`, `test_issue_263_coverage_registry.py`, `test_issue_295_unknown_arguments.py`, `test_issue_66_vault_unassignment_revokes_tools.py`, `test_issue_88_root_confirmed_before_publish.py`) must stay green unmodified — they are the truth-sweep's (#131) guard that a shorter docstring is still an accurate one.
- Docs: `README.md`, `docs/architecture/vault-tools.md`, `CLAUDE.md`.
- Clients: a client that honours annotations stops prompting on read tools and keeps prompting on destructive ones; Claude Code can run read tools in parallel. A client that ignores them sees shorter descriptions and otherwise nothing.
- `src/mcp_stdio.py` re-exposes the same `mcp` instance (`src/mcp_stdio.py:18`), so the registry-introspection path (Glama) sees the metadata without a change.

## Out of scope

- Structured output schemas on the string-returning tools; resources and prompts (#368, per the issue).
- `icons`, `meta`, `structured_output` on registrations.
- Shortening any tool other than the four named (all others are already ≤ 3,136 raw; the cap test covers them without edits).
- The `tools.py` implementation docstrings, which are not on the wire.
- The vault guide primer (`src/mcp_server/vault_guide_primer.md`) — it describes the vault, not the tools, and nothing moves into it.
- A setting to switch the metadata off. Annotations and instructions are static facts about the tools; there is nothing to roll back to but the spec defaults, which are worse.

## Accepted limitations

- L1: Annotations are hints. The MCP spec says clients MUST treat them as untrusted unless the server is trusted; a client may ignore them entirely. Nothing here depends on a client honouring them — `_tracked` and `_require_write` enforce exactly as before.
- L2: claude.ai is reported to drop server `instructions`. Nothing may depend on the instructions being seen; `get_vault_guide` and the per-tool descriptions stay self-sufficient.
- L3: Annotations are per tool, not per call. `edit_note(dry_run=True)` and `write_file(overwrite=False)` carry `destructiveHint=true` although those calls destroy nothing — the conservative direction, by design.
- L4: `request_upload` is `destructiveHint=true` although the tool call itself writes no vault byte — the `overwrite=True` capability it mints overwrites on redemption, and the hint describes what the call authorises (owner decision, Codex spec round 1). The cost is one more confirmation prompt on an annotation-aware client for a no-clobber upload link; `request_download`, which can destroy nothing, stays additive.
- L5: `semantic_search` calls the configured embedding provider, which may be a remote API, yet is `openWorldHint=false`: the tool's domain of interaction is the vault; the provider is server infrastructure, not an entity the tool interacts with on the caller's behalf.
- L6: `idempotentHint=false` on every non-read-only tool, including `delete_note` / `delete_file` (a repeat delete has no additional effect) and `set_frontmatter` (a repeat with the same `updates` is a no-op). A false hint costs a client a confirmation on retry; a wrong `true` lets a client silently re-run a write — so the hint is set in the safe direction and not per tool.
- L7: Narrative moved to `docs/` is no longer visible to the agent at all. What stays in the description is the contract; what moves is the reasoning, which an agent could not act on anyway.
- L8: The 4,000-character cap is on Python `len()` of the description (code points). Claude Code's 4,096 cutoff is in its own units; the 96-character margin is deliberate and is not a promise about every client's measure.
