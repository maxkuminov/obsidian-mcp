Refs #392. Read `proposal.md`, `design.md` (D1–D10, L1–L8, the adversarial scenarios) and `specs/` first, then `docs/architecture/indexing-and-embeddings.md` sections "The scan, off the loop and ahead of the lock", "NUL-free derivation, not-indexable paths…", "The poison-note quarantine", "A pass does not prune what it could not see", "Re-derive progress" and "Change feed".

**Base check:** `src/services/task_rule.py` exists, `openspec/changes/index-exclude-conflict-files/` exists, and no `src/services/conflict_copies.py` exists yet. No migration. Do not touch any tool's registration, description or `tests/fixtures/tools_list_*.json`.

## 1. Patterns and setting

- [x] 1.1 `src/services/conflict_copies.py` (new, pure, stdlib `re` only):
  - `CONFLICT_PATTERNS`: P1–P6 from design D2, compiled, each preceded by a comment citing its source;
  - `is_conflict_copy(name: str) -> bool`: `fullmatch` on the basename, with `[0-9]` digit classes only.
- [x] 1.2 `src/config.py`: `index_exclude_conflict_files: bool = False`, beside the other `INDEX_*` settings, with a comment pointing to the module and design.
- [x] 1.3 `tests/test_issue_392_conflict_copies.py` (the matcher table):
  - every positive example in the index-integrity spec;
  - every look-alike in scenario 7 / the spec;
  - the U+2019 apostrophe in P4;
  - non-ASCII digits refused;
  - a directory-shaped name in a path (the function takes basenames; the scan test covers folders).

## 2. Scan and pass

- [x] 2.1 `src/services/indexer.py`:
  - `EXCLUDED_CONFLICT_COPY` reason constant;
  - `_scan_vault(…, exclude_conflicts: bool = False)` classifies after `_path_not_indexable_reason` and before any stat, with no `_warn_not_indexable`;
  - `_index_vault_pinned` reads the setting once and passes it (quarantine restarts reuse the scan, so the value is fixed per invocation).
- [x] 2.2 In `_index_vault_attempt`:
  - split `not_indexable_rows` for logging: the existing WARNING covers only non-excluded reasons, and a new INFO removal line covers excluded rows (`_format_skips`: up to `SKIP_REPORT_LIMIT` = 20 paths, then `, and N more`);
  - add the per-pass INFO count line when `excluded > 0`;
  - `IndexPassResult.excluded`;
  - `not_indexable` excludes `EXCLUDED_CONFLICT_COPY`.
- [x] 2.3 Amend the comments that say a name-decided not-indexable path "can have no row" (the C4 loop, `_collect_feed_events`). No branch change is expected there; add one only if a test proves it necessary, and say so in the design.
- [x] 2.4 In-process `excluded_conflict_copies: dict[int | None, int]`, set after each committed pass; the lifespan's startup INFO line when the setting is on.
- [x] 2.5 Offline tests (extend the existing scan and pass tests, mocked session where they already are):
  - an excluded path is in `seen` and `not_indexable`, and is never statted or read (spies on `read_note_at` / `_stat_follow`);
  - with the setting off, the scan is unchanged;
  - `excluded` and `not_indexable` counts;
  - no WARNING for excluded paths;
  - the startup line (`main._log_index_exclusion`), logged only when on;
  - the backstop clock advancing and a quarantine entry at an excluded path being cleared are integration cases (2.6), not offline ones.
- [x] 2.6 Integration (`tests/integration/`, real Postgres; port 55446, container `omcp-it-index-exclude-conflicts`). Trace design scenarios 1, 2, 4, 5, 6, 9 and 10:
  - rows, embeddings, task rows and the inbound link deleted or dangling on toggle-on;
  - the original's row untouched;
  - `created` on toggle-off;
  - no move pairing to a conflict path, also across a forced `PoisonNote` restart;
  - a protected row kept beneath an unlistable directory;
  - a re-derive over conflict-copy rows records (review r1, D);
  - a quarantine entry at an excluded path, and a quarantined note renamed onto one, are cleared (review r1, E);
  - the backstop clock advances on the toggle pass, with the seeding pass's record cleared first (review r1, F);
  - the removal line (`_format_skips`: 20 paths, then `, and N more`) and the per-pass count line, captured (review r1, B);
  - a second pass writes nothing.

## 3. Change feed

- [x] 3.1 Tests (integration, with 2.6):
  - toggle-on gives exactly one `deleted reason=not_indexable by human/sync` per removed row and nothing for row-less excluded paths;
  - the following pass records nothing;
  - toggle-off gives `created` with the hash;
  - a tool `edit_note` on an excluded path records its own `modified` and the pass adds nothing;
  - `get_changes` renders the lines with the existing format.

- [x] 3.2 Pin the limitations as tests:
  - L9: an `edit_note` on an excluded copy, then toggle-off, gives an upserted row and no event;
  - L13: toggle-off over a scope with no rows gives one `reset`.

## 3b. Tool notices (spec review r1)

- [x] 3b.1 `src/mcp_server/tools.py`: one constant `CONFLICT_EXCLUSION_NOTICE` (exact spec text) and a helper that appends `"\n" + notice` when the setting is on and `is_conflict_copy(basename)` holds. Apply it to `create_note`'s success string and to `move_note`'s fully successful `return message` only.
- [x] 3b.2 Tests:
  - the notice for both tools when the setting is on and the name matches;
  - byte-identical output when the setting is off, or the name does not match;
  - the integration path `read_note` / `edit_note` / `move_note` out of an excluded name;
  - `move_note` onto an excluded name: the row stays until the next pass, which removes the row, vectors, links and task rows.

- [x] 3b.3 Review r1, A: `CONFLICT_REWRITE_NOTICE` on a fully successful `move_note(rewrite_links=True)` whose SOURCE is an excluded name, ahead of the destination notice. Integration-tested: links not rewritten, both notices once, no notice when off, when `rewrite_links=False`, or when the source does not match.

## 4. Dashboard (panel template; owner browser pass)

- [x] 4.1 `src/control_panel/routes.py` dashboard handler: pass `excluded_conflicts` (the viewer scope's count, or `None` when the setting is off or nothing is counted yet).
- [x] 4.2 `src/control_panel/templates/dashboard.html`: one Jinja line beside "Last run", with existing utility classes only. No script, no `style=`, no `on*=`, no new class.
- [x] 4.3 Tests:
  - the render with the setting off is unchanged for the same data;
  - the singular and plural lines;
  - a multi-user viewer sees only their own scope's count;
  - the existing CSP/no-inline-style template guards stay green.
- [ ] 4.4 Owner: walk the dashboard with devtools open, with the setting on and off; zero CSP violations.

## 5. Documentation

- [x] 5.1 `README.md`: a settings-table row for `INDEX_EXCLUDE_CONFLICT_FILES` (`false`), and two sentences: what is excluded, that files are never touched and stay readable by path.
- [x] 5.2 `.env.example`: the setting, commented, under `# Indexer`, with the tools covered.
- [x] 5.3 `DEPLOYMENT.md`, a short section:
  - turning it on (restart; the first pass removes the rows and the feed records `deleted reason=not_indexable`);
  - turning it off (`created` on the next pass);
  - recovering a conflict copy (`read_note`, then `move_note` to an ordinary name, or merge by `edit_note`);
  - deployment-wide in multi-user mode;
  - look-alike titles (L1) and unmatched variants (L2);
  - the dashboard line and the log lines.
- [x] 5.4 `docs/architecture/indexing-and-embeddings.md`: a subsection under the #308 not-indexable section, "Excluded sync-conflict copies (#392)", with D3, D5, D6, D8 and the L-list. Add a CLAUDE.md Key-decisions bullet only if the owner wants one (it is one more not-indexable reason, not a new invariant).

## 6. Gates

- [x] 6.1 `openspec validate index-exclude-conflict-files --strict`.
- [x] 6.2 The offline suite (`OMCP_ALLOW_SKIP_TRANSFER_INTEGRATION=1 … pytest tests --ignore=tests/integration`) and the integration modules touched in 2.6 / 3.1 on port 55446.
- [x] 6.3 `openspec-verifier`, then the adversarial Codex pass, which is mandatory because this touches the indexing path. Give it design D3–D8 and scenarios 1–13.
- [ ] 6.4 End-to-end on the live server after deploy: with the setting on, `keyword_search` / `list_notes` omit a known conflict copy, `read_note` returns it, and `get_changes` shows its `deleted reason=not_indexable`. Report which tools were called.
