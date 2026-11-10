Refs #428. Read `proposal.md`, `design.md` (D1–D19), `references/tasks-plugin-reference.md` and `specs/` before starting.

**Base check, every slice:** `alembic/versions/038_oauth_cimd_private_key_jwt.py` exists, no `039_*` exists, and `src/services/tasks.py` defines `TASKS_SINCE_EXTRACTION_VERSION = 4`.

The slices are design §"Slice plan". Each group below is one worktree. Touch only the files its row lists; do not bundle other findings. Waves: 1 = S1 ∥ S2; 2 = S3 ∥ S4 ∥ S5 ∥ S6 (after wave 1 merges); 3 = S7, then S8.

## 1. S1: pure modules (wave 1, no database)

- [x] 1.1 `src/services/task_line.py`. Implement and unit-test against every R§1.4 example:
  - `parse(line) -> TaskLine`: prefix, box, gap, description span, run tokens with spans, block link, `tail_ws`;
  - the D4 operations, as pure functions returning `(new_line, spans)` or a `TaskLineRefusal(code="task_malformed", reason)`;
  - `description_region(text)`;
  - `normalise_description(text)` (D9);
  - `task_key(full_text)` (D1);
  - `description_key(norm)` (D9);
  - `render_id(key, ordinal, count)` and `parse_id(s)`.
- [x] 1.2 `src/services/task_recurrence.py`, D7:
  - `parse_rule(text) -> Rule | Unsupported`, tier 1 only;
  - `next_occurrence(rule, *, due, scheduled, start, done_on) -> Shift | Unsupported`, using stdlib `datetime` and `calendar` only;
  - a docstring listing tier 1, tier 2 and the R§ citations.
- [x] 1.3 `src/services/task_dates.py`: `resolve(value, tz, *, now=None) -> date | invalid` (D12 forms, N ≤ 3660) and `today(tz, *, now=None)`. The clock is injectable.
- [x] 1.4 `tests/test_task_line.py`, `tests/test_task_recurrence.py` and `tests/test_task_dates.py`:
  - every case in the design §Test plan "Unit, pure";
  - the R§2b/2c table;
  - `tag_prefixes` normalisation (slash runs collapsed, trailing `/` stripped, the same on filter values), plus the largest buildable case: 64 distinct 127-character tags of a 3-character first segment and 62 one-character segments give 4,032 prefixes, uncapped;
  - every tier-2 text refused;
  - the CRLF, lone CR, hard break, VS16 and variant-signifier cases;
  - the property test "bytes outside the declared spans are identical".

## 2. S2: schema, models, settings (wave 1, owns migration 039)

- [x] 2.1 `alembic/versions/039_task_tools.py` (`revision="039"`, `down_revision="038"`), exactly as D11/D14 and the `schema-integrity` delta define it:
  - markers `… (039_task_tools)`;
  - reconcile-or-refuse shape;
  - timeouts set and `RESET`;
  - no data written;
  - marked-only downgrade.
- [x] 2.2 `src/models/db.py`: mirror every column, CHECK, index (`postgresql_where` / `postgresql_using`) and marker byte for byte. Add `class TaskSettings`. Add no ORM relationship that loads tasks or settings implicitly.
- [x] 2.3 `src/config.py`:
  - `TASKS_DEFAULT_TIMEZONE` (validated with `zoneinfo.ZoneInfo`; startup refusal naming the variable);
  - `TASKS_SET_DONE_DATE=true`, `TASKS_SET_CANCELLED_DATE=true`, `TASKS_SET_CREATED_DATE=false`, `TASKS_NEXT_RECURRENCE_BELOW=false`;
  - the constants `TASK_REFRESH_LOCK_WAIT_MS=2000`, `TASK_SIMILAR_SCAN_MAX=2000` and `TASK_CONTEXT_MAX_LINES=8`.
  
  Add `tzdata` (pinned) to `requirements.txt`.
- [x] 2.4 `src/services/task_settings.py`: `EffectiveTaskSettings` (frozen), `effective(session, uid)` and `save(session, uid, form) -> list[str] errors` (D14 validation, upsert under `FOR UPDATE`). Owner-scoped.
- [x] 2.5 Backup classification: `task_settings` durable (data dumped), `note_tasks` stays derived. Update `Makefile`, `deploy/kubernetes/components/db-backup/configmap.yaml` and `tests/test_backup_derived_tables.py`.
- [x] 2.6 Extend the schema gate's per-migration cases for 039 (fresh run, stamp-back to 038, impostor table, impostor column, key-shape CHECK). Run `make test-schema` with `SCHEMA_TEST_CONTAINER=omcp-tt-s2 SCHEMA_TEST_PORT=55452`. Confirm `test_nonsuperuser_migrations_pg.py` passes.
- [x] 2.7 `tests/test_task_settings.py`: validation, defaults merge, NULL-scope row, cascade on user delete (integration-guarded).

## 3. S3: extractor version 5, indexer guard, write-time refresh (wave 2)

- [x] 3.1 `src/services/tasks.py`:
  - `ExtractedTask` gains `task_key`, `key_ordinal`, `key_count`, `description_key`, `text_fold`, `tag_prefixes`, `recurrence`, `recurrence_cut`, `created`, `cancelled`, `context` and `context_cut` (the `note-tasks` delta, using `task_line` for the run, the key and the description);
  - add `tasks_from_note_text(raw) -> (tasks, truncated)`, the one shared function (frontmatter split + offset + extraction);
  - add `TASK_IDS_SINCE_EXTRACTION_VERSION = 5`;
  - docstring: the composition rule gains the write-time refresh exception (D10).
- [x] 3.2 `src/services/indexer.py`:
  - `CURRENT_EXTRACTION_VERSION = 5`, with a "Version 5 adds task ids (#428)" paragraph;
  - R2: `scan_started_at` from the database before the first file read, once per pass invocation;
  - R3: task-dirty rows are ineligible for the stat shortcut and join the task stage's list;
  - R4: the keep-or-derive rule, setting `tasks_source_hash` and clearing `tasks_refreshed_at`;
  - insert the new columns.
  
  `src/services/embeddings.py`: `clean_at_version` gains `5: clean_for_embedding`.
- [x] 3.3 `src/services/task_index.py`: `refresh_note_tasks(*, uid, root, target, rel, published: bytes) -> RefreshResult(status, code)`, exactly R1 including step 3a (re-read through the anchored `target` under the row lock; on a mismatch write nothing and return `superseded`). Its own session, `SET LOCAL lock_timeout`, root re-check, `FOR UPDATE` on the owner-scoped row, delete and insert, the hash and timestamp. It never raises to the caller, and takes no generation lock.
- [x] 3.4 Unit: `tests/test_note_tasks_extractor.py` gains the version-5 columns, the context rule and the run-based recurrence. Add an AST guard that `task_index` never calls `acquire_generation_lock*`.
- [x] 3.5 Integration (`tests/integration/test_task_refresh_pg.py`, container `omcp-tt-s3` / 55453):
  - the version 4 → 5 backfill (no `embedded_content_hash` cleared);
  - every D10 interleaving converging within two passes;
  - a task-dirty row with an unchanged stat re-read;
  - refresh lag on a held row lock;
  - no deadlock against a pass holding the generation lock;
  - pass and refresh derivations equal column for column;
  - tenancy (the user-8 refresh never touches user 7's same-path row);
  - the #350 lifecycle tests still green;
  - two task writes on one note whose refreshes finish out of order: the first write's refresh reports `superseded`, and the rows describe the second write.

## 4. S4: query service and rendering (wave 2, does not touch `tools.py`)

- [x] 4.1 `src/services/task_query.py`:
  - `list_tasks_query(uid, args, settings) -> TaskPage | Refusal` (D12: validation, relative dates, every filter, `tag_prefixes` matching, `text_fold` with `strpos`, scope with the excluded count, every `order_by` with its keyset predicate, cursor version 2 encode and decode with the owner, filter and settings digests, the counts, one `REPEATABLE READ, READ ONLY` snapshot);
  - `task_agenda_query(uid, args, settings)` (D13: the five predicates, caps, totals, continuation cursors minted for the exact `list_tasks` query).
- [x] 4.2 `src/mcp_server/task_render.py`:
  - header line 1 (the #350 tokens plus `task ids pending: N tasks`, counting rows with a NULL `task_key`) and the query line (≤ 600);
  - an id rendered whenever `task_key` is populated, else `id pending`;
  - full rows with ids, brief rows (≤ 260 plus the path), context lines (`    ┆ `);
  - the abbreviated row (measured structure 86, plus the 29-character id token);
  - the agenda layout;
  - `response_cap_too_small`.
  
  Move or reuse the #350 helpers (`_escape_task_path`, `_one_line`, cursor helpers) by import. Do not edit `tools.py`; S7 rewires.
- [x] 4.3 `tests/test_task_render.py`:
  - every bound;
  - the minimum-cap test with ids at 1,000 (985 arithmetic);
  - `response_cap_too_small` at 1,000 with exclusions;
  - the agenda at 2,800;
  - forged rows through text, heading, tag and context.
- [x] 4.4 `tests/integration/test_task_query_pg.py` (container `omcp-tt-s4` / 55454): every `list_tasks` and `task_agenda` scenario of the `note-tasks` delta, including:
  - exactly-once paging per `order_by` over 1,234 tasks;
  - a version-1 cursor refused;
  - hierarchical tags vs `waiting-forever`;
  - Cyrillic, final sigma and `ß` text;
  - scope counts;
  - agenda disjointness over the generated combination set;
  - continuation cursors accepted by `list_tasks_query`, minted only from shown rows;
  - a zero-row section continuing without a cursor;
  - tenancy.

## 5. S5: write planning (wave 2, pure plans plus one query; does not publish)

- [x] 5.1 `src/services/task_write.py`:
  - `resolve(tasks, id, *, hash_supplied) -> target | refusal` (the D2 table: a suffixed id without a hash gives `precondition_required`);
  - `continuation_block(lines, i)` (uncapped, D8), used by placement, `append_context` and below-placed recurrence;
  - `plan_complete`, `plan_update` and `plan_add(note_text, args, settings, today) -> Plan(spans, new_text, new_lines, new_ids) | refusal` (D4–D8);
  - `verify(old, new, plan)` (D5);
  - `async duplicate_scan(session, uid, settings, norm, terms) -> (exact, similar, stopped)` (D9).
  
  It uses `task_line`, `task_recurrence`, `task_dates` and `tasks.tasks_from_note_text`.
- [x] 5.2 `src/services/refusals.py`: add `task_not_found`, `task_ambiguous`, `task_changed`, `already_completed`, `task_cancelled`, `task_malformed`, `recurrence_unsupported`, `on_completion_unsupported`, `duplicate_task` and `response_cap_too_small`, with the catalogue entries their siblings carry.
- [x] 5.3 `tests/test_task_write.py`: every `task-write` scenario reachable without a database, including:
  - the D6 table;
  - next-line construction and placement, above and below, at end of file without a terminator;
  - `add_task` placement cases 1–3 (ordered and callout anchors);
  - context-line insertion;
  - every D4(a)–(d) refusal;
  - `text` replacement keeping tags (trailing tag, a mid-description tag with a `[[link]]` replaced, a tag already in the new text, `text` with `remove_tags`), with D5 check 6 catching an injected tag loss;
  - the next-occurrence terminator in a mixed CRLF/LF note for both placements;
  - 9- and 10-line continuation blocks (a blank line inside, a deeper child task) for `add_task`, `append_context` and below placement, with D5 check 5 catching an injected reparenting;
  - D5 verification catching an injected corruption.
- [x] 5.4 `tests/integration/test_task_duplicates_pg.py` (container `omcp-tt-s5` / 55455): exact duplicate in scope and in the target note, similar listed, the scan bound, tenancy.

## 6. S6: panel Tasks card (wave 2)

- [x] 6.1 `src/control_panel/routes.py`:
  - `POST /account/tasks` (multi-user, own row, `_require_account_route`);
  - `POST /admin/settings/tasks` (single-user, admin, NULL row);
  - context for the GET pages (effective values, timezone list).
  
  Both posts use CSRF, flash and a 303 redirect, and call `task_settings.save`.
- [x] 6.2 The card in `templates/account.html` (rendered only in multi-user mode) and `templates/settings.html` (rendered only in single-user mode). It uses `data-*` attributes, has no inline handler or style, and uses a `<datalist>` for timezones. Touch `panel.js` only if a control needs behaviour.
- [x] 6.3 `tests/test_panel_task_settings.py`: GET and POST in both modes, validation flashes with nothing saved, CSRF refused, 404 and admin gates, the CSP scan (no `on*=`, no `style=`, nonce on every `<script>`/`<style>`). Regenerate `tests/golden/oidc_off/page_account_*` deliberately and say so in the commit.

## 7. S7: wiring and tool surface (wave 3, after S3–S5 merge)

- [x] 7.1 `src/mcp_server/tools.py`:
  - **`list_tasks_impl`** delegates to `task_query` and `task_render`, with `_tracked` params extended (D17) and `arg_char_caps` for `path`, `text` and `cursor`;
  - **`task_agenda_impl`**;
  - **`complete_task_impl`, `update_task_impl` and `add_task_impl`**, following the D2 order exactly (reusing `_require_write`, `_precondition_syntax_error`, `open_mutable`, `_leaf_state_error`, `read_bytes_at`, `_precondition_error` with the D3 enforceability, `_note_size_error`, the `dry_run` diff, `_confirmed_publication` with `write_file_at(expected=…)`, `_concurrent_write_refusal`), then `task_index.refresh_note_tasks`, then `_record_note_events`, then the D16 response;
  - every refusal typed;
  - the D2 bounded retry: on an in-call conflict with no `expected_hash`, repeat from the read, at most 3 attempts in total, carrying nothing over; no retry when a hash was supplied;
  - `_tracked(..., write_class=True)` for the three writes, never logging text or context.
- [x] 7.2 `src/mcp_server/server.py`:
  - register `task_agenda` (`_READ_ONLY`), `complete_task` (`_DESTRUCTIVE`), `update_task` (`_DESTRUCTIVE`) and `add_task` (`_ADDITIVE`) after `hybrid_search`, in that order, with D15 titles;
  - descriptions ≤ 4,000 characters, leading with the contract;
  - the extended `list_tasks` signature and description;
  - update the classification comment (19 read-only; destructive = write bucket minus `create_note` and `add_task` plus `request_upload`);
  - replace the `list_tasks` sentence in `SERVER_INSTRUCTIONS` with the D15 sentence, keeping the lead byte-identical.
- [x] 7.3 `src/mcp_server/tool_permissions.py`: add the three write tools (12 names). `src/services/concurrency.py`: the three in `write`, `task_agenda` in `scan`, `CLASS_MAPPING_VERSION = 6` with a comment line.
- [x] 7.4 `src/mcp_server/vault_guide_primer.md`: the Tasks section per the `vault-guide` delta, neutral.
- [x] 7.5 Fixtures and pinned counts (re-grep `tests/` for `28`, `19`, `eighteen`, `nine`, `eight`, `full_v1`, `CLASS_MAPPING_VERSION`):
  - regenerate the `list_tasks` entry in `tools_list_schema_additions.json`, append the four new tools, and add the additive-amendment test;
  - generate `tools_list_full_v2.json` and the v1-derivation test; regenerate `tools_list_readonly_oauth.json` (20);
  - update `test_issue_343_tool_metadata.py` (32, `READ_TOOLS` 19, the destructive rule), `test_issue_358_scoped_tools_list.py`, `test_issue_295_unknown_arguments.py`, `test_issue_263_coverage_registry.py`, `test_issue_261_controller.py`, `test_issue_261_tool_admission.py`, `test_body_budget_config.py`, `test_tei_identity.py`, `test_search_confidence.py`, `test_transfer_overlap_gate.py`, and anything else the grep finds.
- [x] 7.6 `tests/integration/test_task_tools_pg.py` (container `omcp-tt-s7` / 55457): the end-to-end `task-write` scenarios through the registered tools. That covers resolution (wrong line, duplicates, count changed, concurrent modification, CRLF, blockquote, fence, frontmatter look-alike), each write tool, `dry_run`, required mode, permission refusal, the change-feed event after the refresh, read-your-writes (list → complete → list), usage params free of task text, and tenancy. It also covers:
  - suffixed ids refused without `expected_hash` in default mode;
  - swapped identical lines refused under the old hash;
  - three parallel completions of different tasks in one note all succeeding;
  - two parallel completions of the same task yielding exactly one success.

## 8. Integration and seams (orchestrator, after S7 merges)

- [x] 8.1 Grep for a production caller of every new export (design §Slice plan, "Seams"):
  - `refresh_note_tasks`, `tasks_from_note_text`, `effective`;
  - `list_tasks_query`, `task_agenda_query`;
  - `plan_complete`, `plan_update`, `plan_add`, `duplicate_scan`;
  - `next_occurrence`;
  - every new refusal code;
  - the R2/R3/R4 hooks.
  
  Any export without one blocks.
- [x] 8.2 Full offline `pytest tests`, then `make test-integration`, then `make test-schema` (not concurrently), on the merged result. This is the authoritative gate.

## 9. S8: docs (same PR)

- [x] 9.1 `README.md`: the tasks section per the `readme-positioning` delta (optional capability, plain checkboxes, the five tools, the `TASKS_*` table, the panel card, the supported recurrence shapes and the refusal of everything else), and every tool count (32).
- [x] 9.2 `.env.example`: each `TASKS_*` variable with its default and a one-line comment.
- [x] 9.3 `DEPLOYMENT.md` and `docs/deployment-kubernetes.md`: upgrade notes covering:
  - migration 039;
  - the one-pass version-5 re-derive (no re-embed), with `move_note(rewrite_links=True)` returning `index_not_ready` until it ends;
  - `list_tasks` cursors issued before the deploy refused once;
  - the new boot refusal for an invalid `TASKS_DEFAULT_TIMEZONE`;
  - the `CLASS_MAPPING_VERSION` 6 epoch;
  - `tzdata`;
  - rollback as roll-forward.
- [x] 9.4 `docs/architecture/vault-tools.md`: a "Task write tools (#428)" section (D1–D9, D16), the tool-metadata counts and the destructive rule, the `list_tasks` arguments and cursor version 2, and `task_agenda`.
- [x] 9.5 `docs/architecture/indexing-and-embeddings.md`: "Version 5 adds task ids" and "The write-time task refresh" (D10 R1–R4, convergence, why no generation lock).
- [x] 9.6 `docs/architecture/schema-and-migrations.md`: migration 039, `task_settings` durable. `docs/architecture/control-panel.md`: the Tasks card in both modes. `docs/architecture/rate-limits.md`: the write-bucket and class tables, version 6. `docs/architecture/usage-attribution.md`: the task-tool params and `task_refresh`.
- [x] 9.7 `CLAUDE.md`: the MCP tools section (32, a read-only grant sees 20, the Tasks line with the five tools), the #343 bullet (19 read-only, the destructive rule), and a key-decisions bullet:
  - the task id over the uncut text with an ordinal suffix;
  - resolution bound to bytes;
  - minimal edits;
  - the closed recurrence grammar;
  - the write-time refresh with no generation lock and the R4 convergence;
  - per-scope `task_settings`.
- [x] 9.8 Grep-check that every new setting appears in README and `.env.example`.

## 10. Gates

- [x] 10.1 `openspec validate task-tools --strict`.
- [x] 10.2 `make audit` (including `tzdata`), `make test-schema`, `make test-integration`, `make db-check` (after deploy).
- [x] 10.3 `openspec-verifier` subagent against this change, iterated to zero blocking gaps.
- [x] 10.4 Adversarial Codex. This is mandatory: write tools, recurrence correctness and an index-path change. Frame "wrong" as a write to the wrong line or damage to a note, a series that ends silently or gets a wrong date, a listing that silently omits or wrongly includes, and a tenant boundary crossed. Two rounds by default, with triage per the workflow.
- [ ] 10.5 After deploy, an end-to-end exercise against the live server, recording which tools were called and the results:
  - `list_tasks(due_from="today", due_by="+7d")`;
  - `list_tasks(tags=["<a nested tag's parent>"])`;
  - `list_tasks(detail="brief", context=true)`;
  - `task_agenda()`, following one continuation;
  - `add_task(dry_run=true)` then `add_task` in a scratch note;
  - `update_task` (due, a tag, `append_context`);
  - `complete_task` on a plain task and on a recurring task (`every week`);
  - `list_tasks(path=<scratch note>, status="all")` to confirm read-your-writes before the next pass;
  - `get_changes` for the events;
  - `get_vault_guide`.
  
  Confirm the not-yet-scanned and ids-pending counts reached 0.
- [ ] 10.6 The owner's manual panel pass: walk the Tasks card in the deployed mode with devtools open, save valid and invalid values, and confirm zero CSP violations.
- [x] 10.7 Archive (`openspec archive task-tools -y`) and docs as the last commits of the feature branch, one PR, `Closes #428`.

## Accepted limitations (record in the archived spec; do not re-fix when re-reported)

These are L1–L15 of `design.md`. One line each there.
