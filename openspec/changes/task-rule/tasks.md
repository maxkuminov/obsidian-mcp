Refs #434. Read `proposal.md`, `design.md` (D1–D12, the accepted limitations and the open questions) and `specs/` before starting. The archived `openspec/changes/archive/2026-11-10-task-tools/` is the source of every invariant this builds on.

**Base check, every slice:** `alembic/versions/039_task_tools.py` exists, no `040_*` exists, and `openspec/changes/task-rule/` exists.

There are two slices (design §"Slice plan"): **I** (groups 1–2) and **D** (group 3). Touch only the files the slice's row lists, and do not bundle other findings. Group 4 runs on the merged result.

## 1. I: rule, schema and settings

- [x] 1.1 `src/services/task_rule.py` (new, pure, SQLAlchemy imported inside functions):
  - `RULES`;
  - `rule_clause(eff)` (D1, with the D2 pending-row branch, reusing `task_query.tags_none_clause` for the tag part);
  - `unsure_clause(eff)` (D3);
  - `pending_approximate(eff)`;
  - `extracted_is_task(task, eff)`;
  - the three validators (D9: rule kind, task tag, filter text), with the spec's messages. The filter-text validator includes `task_write._structure_refusal(F, context=True)`'s rules; share that check rather than copying it, keeping `config.py` cycle-free.
- [x] 1.2 `alembic/versions/040_task_rule.py` (`revision="040"`, `down_revision="039"`): the three columns and both CHECKs (`ck_task_settings_task_rule`, `ck_task_settings_global_filter`; D8, D11), markers `… (040_task_rule)`, reconcile-or-refuse, timeouts set and `RESET`, no data written, marked-only downgrade with the CHECKs first.
- [x] 1.3 `alembic/versions/039_task_tools.py`: the allow-list only. `_shape` leaves out exactly 040's five units, each only when it carries its exact 040 marker. Change nothing else in 039.
- [x] 1.4 `src/models/db.py`: mirror the three columns and the CHECK (in `TaskSettings.__table_args__`) with their markers. `alembic check` must be clean.
- [x] 1.5 `src/config.py`:
  - `tasks_rule` (`all`);
  - `tasks_rule_tags` (`Annotated[list[str], NoDecode]`, comma-separated, normalised);
  - `tasks_global_filter` (empty);
  - field validators calling the `task_rule` validators (imported inside the validator), and a model validator refusing `filter` with an empty filter text, naming both variables;
  - a comment block in the style of the #428 one, saying that the migration init container is refused too.
- [x] 1.6 `src/services/task_settings.py`:
  - `EffectiveTaskSettings` gains `task_rule`, `task_tags`, `global_filter`, `task_rule_inherited` and `task_rule_unusable`, with D8's one-unit inheritance and its defensive fallback (an unevaluable rule becomes `all` with a reason and one WARNING) in `_merge`;
  - `validate`: the three keys, each absent → unchanged;
  - `save`: `pg_advisory_xact_lock` on the scope before the read; the merged `filter`-needs-text check; a `CheckViolation` on `ck_task_settings_global_filter` handled as the same refusal, writing nothing.
- [x] 1.7 Tests:
  - `tests/test_task_rule.py` (new);
  - `tests/test_task_settings.py` (unit: inheritance, absent keys, merged check, `Settings` refusals);
  - `tests/integration/test_schema_check.py` (040 fresh, stamp-back to 039 and 038 over head, impostors, both CHECKs, the upgrade → `downgrade 039` → upgrade round trip);
  - the concurrent-first-saves test, in `tests/integration/test_task_rule_pg.py`;
  - `test_nonsuperuser_migrations_pg.py` covering 040.

  Run `make test-schema SCHEMA_TEST_CONTAINER=omcp-tr-impl SCHEMA_TEST_PORT=55481`.

## 2. I: query, render, writes, surface, panel

- [x] 2.1 `src/services/task_query.py`:
  - `location_clause` (today's `scope_clause` body, moved verbatim);
  - `scope_clause` = location ∧ rule (D4);
  - `_Filters` and `task_agenda_query` compute `excluded by scope` over ¬L only, `excluded by rule`, `kept though unsure` and P (D4, D5), with `task ids pending` ignoring R under `fields`;
  - `filters_digest` adds `"rule"` only under default scope with a rule other than `all` (D6);
  - the query-line trigger is extended by "a rule is in force";
  - `TaskPage` / `AgendaPage` carry the rule and the new counts.
- [x] 2.2 `src/mcp_server/task_render.py`:
  - `RULE_LINE_MAX = 320`;
  - `render_rule_line`, D5's exact formats, names through `_names_part`;
  - the line inserted after the query line in `render_list_tasks` and `_render_empty` (names give way first), and after the agenda header;
  - `AGENDA_MIN_BUDGET` 3,121 with the line;
  - measured guarantee constants (≤ 1,921 never refused; 40 + 40 ≤ 1,962).
- [x] 2.3 `src/services/task_write.py`:
  - `plan_add` prepends the filter text under `filter` (D7), with the combined-length refusal, and re-runs `_structure_refusal` on the final combined description before any dry-run answer or publication (unit-tested in `tests/test_task_write.py` with `%%` and `<!--` handed in directly);
  - the plan reports `filter_added`;
  - `duplicate_scan` is unchanged in code (it gets the rule through `scope_clause`), and a test pins that it calls `scope_clause`.
- [x] 2.4 `src/mcp_server/tools.py`, in `add_task_impl` and `update_task_impl` only: the `global filter added:` line, and the note line from `task_rule.extracted_is_task` over the written line's extraction (`tasks.tasks_from_note_text`), in the result and the dry run (D7). `complete_task_impl` is untouched.
- [x] 2.5 `src/mcp_server/server.py`: the `list_tasks` and `task_agenda` `scope` wording, the `list_tasks` header sentence, and the `add_task` sentence (D12). Each description stays ≤ 4,000 characters; tighten, never raise. `update_task`, `complete_task` and `SERVER_INSTRUCTIONS` are unchanged. Regenerate `tests/fixtures/tools_list_full_v2.json` and `tools_list_readonly_oauth.json` (and the `list_tasks` description in `tools_list_schema_additions.json`, if it carries one) deliberately, with schemas unchanged.
- [x] 2.6 `src/control_panel/templates/_task_settings_card.html`: the "Which checkboxes are tasks" block (D10), with no script, inline handler or `style=`. `src/control_panel/routes.py`: `_task_settings_context` exposes the rule (stored, effective, server default in words); `_save_task_settings` passes `task_rule`, `task_tags` and `global_filter`.
- [x] 2.7 Tests:
  - `tests/test_task_render.py` (both worst-case rule lines, `test_rule_line_min_budget`, `test_rule_line_full_budget`, agenda at 3,120 / 3,121, query line byte-identical, empty result never refused);
  - `tests/test_panel_task_settings.py` (both modes, every refusal flashed, absent keys, CSP scan, `user_id` ignored);
  - the regenerated `tests/golden/oidc_off/page_account_*`;
  - `tests/integration/test_task_rule_pg.py` (new): every rule × page, counts, agenda and duplicate scan, the D4 identity with folder exclusions, backfill rows, two owners with two rules, cursor refusal and survival, SQL ≡ Python;
  - `tests/integration/test_task_tools_pg.py`: the write cases of the design's test plan;
  - the byte-identity test under `all`.

  Run the offline suite and `make test-integration SCHEMA_TEST_CONTAINER=omcp-tr-impl SCHEMA_TEST_PORT=55481`, never concurrently with the schema gate.

## 3. D: documentation (from this change's text; no code)

- [x] 3.1 `README.md`, the task section:
  - a short "Which checkboxes are tasks" paragraph for readers without the plugin (the three rules, the default, `scope="all"`, writes never blocked, `add_task` adding the filter);
  - the settings table gains `TASKS_RULE`, `TASKS_RULE_TAGS` and `TASKS_GLOBAL_FILTER`;
  - the Tasks card paragraph names the new block.
- [x] 3.2 `.env.example`: the three variables, commented, with defaults and one line each.
- [x] 3.3 `DEPLOYMENT.md`: "Upgrading across migration 040 (task rule, #434)" in the 039 section's style: what 040 adds, that nothing changes until a rule is chosen, the new boot refusals (and that the init container is refused too), backups unchanged, and rollback stated precisely:
  - an already-running previous-image process keeps working during the rolling deploy;
  - a restarted previous image cannot start at revision 040;
  - rollback is roll-forward, or `alembic downgrade 039` run with the 040-aware image **before** starting the previous image, which discards every saved task rule, task tag and filter text (the previous image then lists every checkbox).
- [x] 3.4 `docs/architecture/vault-tools.md` ("Task tools (#428)"):
  - the rule, its predicates and the one-place composition into `scope_clause`;
  - the counts, the rule line and its bounds;
  - the filter prepend and the note line;
  - the duplicate-check split;
  - the accepted limitations;
  - #428 L12 reversed for the global filter.
- [x] 3.5 `docs/architecture/control-panel.md`: the card's rule block and the absent-key rule. `docs/architecture/schema-and-migrations.md`: 040 and 039's new allow-list (the second allow-list after 032's).
- [x] 3.6 `CLAUDE.md`, a short Key-decisions bullet: the rule and its default `all`; byte-identical under `all`; one place (`task_rule` composed into `scope_clause`); the rule line; writes never blocked; the filter prepended; 040 and 039's allow-list.
- [x] 3.7 Public-repo check: no personal names, private paths, real tags or task text. Grep every new setting name across README, `.env.example` and DEPLOYMENT.

## 4. Gates (merged result)

- [x] 4.1 `openspec validate task-rule --strict`.
- [x] 4.2 `make test-schema`, `make test-integration` (sequentially), `make audit` and the offline suite on the merged tree. Then grep the seams (design §"Slice plan").
- [x] 4.3 `openspec-verifier` subagent: iterate to zero blocking gaps.
- [x] 4.4 Adversarial Codex (mandatory: listing correctness and `add_task` change). Frame "wrong" as a default listing that silently omits a real task, a count that does not add up, a cursor that crosses rules, a write the rule blocks or reshapes, or a byte changed under `all`. Two rounds by default; triage per the workflow, and record declined findings as accepted limitations.
- [ ] 4.5 Live exercise after deploy, saying which tools were called:
  - `list_tasks()` and `list_tasks(scope="all")` under `all` (byte-identical to before);
  - set `fields` with workflow tags on the owner's own account in the panel;
  - `list_tasks()` against `list_tasks(scope="all")`: the rule line, and the counts adding up;
  - `task_agenda()`;
  - `add_task(dry_run=true)` with and without a field;
  - one real `add_task` and its `list_tasks(path=…)`;
  - `filter` with a test filter on a scratch note;
  - restore the owner's chosen rule.
- [ ] 4.6 The owner's manual panel pass: the card in both modes, each refusal message, zero CSP violations in devtools.

## 5. Archive

- [x] 5.1 `/openspec-archive-change task-rule -y` as the last commits of the feature PR, with group 3's docs. One PR, `Closes #434`, and a release tag at its merge commit.
