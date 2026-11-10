## Context

#428 (v1.3.0, archived as `2026-11-10-task-tools`) gave agents five task tools over `note_tasks`. Every Markdown checkbox is a row, and every row is a task. In a real vault that makes 83% of the open "tasks" plain checklists (proposal, Why). The default scope of #428 (D12, D14) excludes by folder and by note tag. It cannot separate a checklist from a task that sit in the same note.

**What the code does today** (verified on `origin/task-rule` at 58564cd):

- `task_query.scope_clause(settings)` returns the default scope's *keep* predicate (no excluded folder, no excluded note tag) or `None`. Its callers are:
  - `_Filters` (the `list_tasks` page, every count, `excluded by scope`);
  - `task_agenda_query` (totals, rows, counts);
  - `task_write.duplicate_scan`, the index side of `add_task`'s exact-duplicate refusal and its similar-task list.
- `excluded by scope: N` counts rows that match every other filter and fail that predicate.
- The extractor (`tasks.extract_tasks_bounded`) reads every field from the **whole, uncut** line: dates and priority last-wins anywhere on the line (#428 L2), tags by the inline-tag rule (cut only past 64 tags or over 128 characters, `tags_cut`), recurrence from the trailing run. Only the stored `text` is cut, at 1,000 characters (`text_cut`). An invalid date is stored NULL.
- Rows awaiting the version-5 backfill (NULL `task_key`) have every version-4 column (`due`, `scheduled`, `start`, `done_on`, `priority`, `tags`, `tags_cut`, `text`, `text_cut`) and NULL `recurrence`, `created`, `cancelled`, `tag_prefixes`, `text_fold`.
- `task_settings` holds one row per owner scope. `effective()` is read once per call with no cache, and `save()` treats an absent boolean key as "unchanged".
- `list_tasks`'s description is 3,895 characters, `update_task`'s 3,932, `add_task`'s 3,699 and `task_agenda`'s 2,365 (raw docstrings, the shipped text). The cap is 4,000.

**The plugin** (archived `references/tasks-plugin-reference.md`, §1.4 and §3): with a global filter set, a checkbox is a task only if its body contains the filter, by a plain case-sensitive substring test (`description.includes(globalFilter)`). The filter "doesn't have to be a tag". When the plugin's task modal saves a task that lacks the filter, it **prepends** it: `globalFilter + ' ' + description`.

**This reverses #428 L12** for the global filter: the filter is now modelled, as rule `filter`. "Remove scheduled date on recurrence" stays unmodelled.

## Goals / Non-Goals

**Goals:** a per-user rule that keeps checklists out of default listings and out of `add_task`'s duplicate checks; byte-identical behaviour under the default rule; every exclusion stated; one implementation of the scope.

**Non-goals:** see the proposal. In particular, the rule is never stored in the index and never blocks a write.

## Decisions

### D1. Three rules, each a predicate over indexed columns

The effective rule (D8) is one of `all`, `fields` or `filter`. `R(row)` below is "the row is a task".

**`all`.** `R(row)` is true. No predicate is built (`rule_clause` returns `None`), so SQL and output are unchanged.

**`fields`.** With `T` the effective task tags (normalised, D9), possibly empty:

```
has_field(row) := due IS NOT NULL OR scheduled IS NOT NULL OR start IS NOT NULL
               OR done_on IS NOT NULL OR priority IS NOT NULL
               OR recurrence IS NOT NULL OR created IS NOT NULL OR cancelled IS NOT NULL
has_tag(row)   := T ≠ ∅ AND tags_any_clause(T)        -- the list_tasks tag rule
R(row)         := has_field OR has_tag OR (T ≠ ∅ AND tags_cut)      -- D3
```

- The eight field columns are the ones the brief lists: due, scheduled, start, created, done and cancelled dates; priority; recurrence rule. `🆔`, `⛔` and `🏁` are not indexed and do not count (L1).
- **An invalid date** (`📅 2026-02-30`) is stored NULL by the extractor, so it does not count. A line whose only field is an invalid date is not a task under `fields` (L1). Rejected: a signifier-presence fallback on the text. It would make the predicate disagree with every other date filter, and the plugin itself hides invalid dates from date searches (reference §4).
- **Fields anywhere on the line count**, because that is how the extractor stores them (#428 L2). A checkbox mentioning `🔼` mid-sentence counts (L2).
- **Task tags match exactly as `list_tasks(tags=…)` does.** That is hierarchical by `/` segment and case-insensitive, through `tag_prefixes` built by `task_line.tag_prefixes` and values normalised with `task_line.normalise_tag`. So `waiting-for` matches `#Waiting-For/bob` and not `#waiting-forever`. `tags_any_clause(T)` is `NOT tags_none_clause(T)`, the existing NULL-safe clause. Its pending-row branch (D2) is reused rather than written again.
- A task tag only adds tasks. `T = ∅` means "fields only", a valid and common setting.

**`filter`.** With `F` the effective global filter text (non-empty, D9):

```
R(row) := strpos(text, F) > 0 OR text_cut                            -- D3
```

- The test is a case-sensitive substring on the stored `text`. That is the line after the checkbox and its gap, right-stripped, cut at 1,000 characters, and it is the body the plugin tests. `strpos` is exact under the database's deterministic collation. No folding is applied: `#Task` does not satisfy `#task`, as in the plugin (L3).
- The text is the original line, not the code-masked one, as in the plugin: a filter inside inline code still counts.
- A filter text found only inside a field value still counts, because the plugin tests the whole body too.

### D2. Rows awaiting the version-5 backfill are evaluated exactly where they can be, and the approximation is stated

A pending row has NULL `task_key`.

- **`filter`** reads only `text` and `text_cut`. Both are version-4 columns, so it is exact on pending rows.
- **`fields`** on a pending row:
  - `due`, `scheduled`, `start`, `done_on` and `priority` are exact.
  - `has_tag` is exact. `tags_none_clause` already evaluates a NULL-`tag_prefixes` row over its stored `tags` in SQL (#428 D12; stored tags are ASCII).
  - `recurrence`, `created` and `cancelled` are NULL, so they fall back to the presence of their signifier in the stored text: `strpos(text, '🔁') > 0 OR strpos(text, '➕') > 0 OR strpos(text, '❌') > 0`. This is approximate: a signifier without a valid value counts, and one past the 1,000-character cut does not.
  - The branch is chosen by `task_key IS NULL`, the key `recurring` already uses.
- **The approximation is never silent.** Under `fields`, the `task ids pending` count ignores the rule, as it already ignores `text` and `recurring`, so it stays the conservative "might match" count. When that count is non-zero, the rule line (D5) carries `rule approximate for <P> tasks until the next index pass`, where P is that count.
  - Rejected: extending the closing incomplete-list notice. Naming a third filter there takes it past its 120-character bound. The rule line already exists whenever the rule is in force, so the statement belongs there.
- **In practice** the backfill completes on the first pass after the v1.3.0 deploy, and every task write refreshes its note's rows at version 5. The case matters only for a deployment that upgrades straight from v1.2 to this change, or a note that has failed extraction since. It is handled anyway.

### D3. A verdict that depended on cut data keeps the row, and says so

Two rows cannot be judged from what is stored:

- **`filter`:** `text_cut` and `F` is absent from the stored 1,000 characters. The filter may sit after the cut.
- **`fields`:** `T ≠ ∅`, `tags_cut`, no field and no task-tag match. A dropped tag (the 65th, or one over 128 characters) may be a task tag.

Both are **kept** (listed) and counted as `kept though unsure (text cut)` or `kept though unsure (tags cut)` on the rule line.

- Rejected: excluding them and counting them. A real task would then be hidden behind a count, while keeping a rare non-task is visible and harmless. Fail-visible bias.
- `add_task`'s filter prepending (D7) puts the filter at the start of the line, so a line the server adds can never be in this case.

The `unsure` predicate is `text_cut AND strpos(text, F) = 0` under `filter`, and `T ≠ ∅ AND tags_cut AND NOT has_field AND NOT has_tag` under `fields`.

### D4. One place: `task_rule.rule_clause`, composed into `scope_clause`

- **A new pure module, `src/services/task_rule.py`**, holds the rule and nothing else:
  - `RULES = ("all", "fields", "filter")`;
  - `rule_clause(eff)` returns the keep predicate (D1, D2) or `None` for `all`;
  - `unsure_clause(eff)` returns the D3 predicate, or `None`;
  - `pending_approximate(eff)` is true only for `fields`;
  - `extracted_is_task(task, eff)`, the Python twin of `rule_clause` over an `ExtractedTask`, used only by the write tools' statement (D7);
  - the validators (D9).
  
  SQLAlchemy is imported inside functions, as `task_query` does.
- **`task_query.scope_clause(settings)` keeps its name, signature and meaning**, "the default scope's keep predicate", and becomes `and_(location_clause(settings), rule_clause(settings))`, dropping `None` parts. `location_clause` is today's body, moved verbatim. Every existing caller therefore gets the rule with no change of its own:
  - the `list_tasks` page;
  - the `task_agenda` totals and rows;
  - `duplicate_scan`'s exact and similar statements.
  
  A test asserts that `duplicate_scan` and `_Filters.scope` both call `scope_clause`, and that no other module imports `rule_clause` except `task_query` and the tests.
- **`scope="all"` lifts it everywhere.** `_Filters.scope` and the agenda's `keep` are already `None` under `all`, and `duplicate_scan` always uses the default scope (#428 D9).
- **Tenancy is untouched.** The rule is a further `AND` inside the same statements. The owner predicate stays in the JOIN's ON clause, and the rule reads only the caller's own settings row (#428 D18).
- **Counts split the two parts**, so each exclusion is stated once (D5). Let *other* be "matches every filter of the call except the default scope", L the location predicate and R the rule.
  - `excluded by scope` = other ∧ ¬L. The rule is not consulted, so this count is unchanged from v1.3.0 for every settings row.
  - `excluded by rule` = other ∧ L ∧ ¬R.
  - `kept though unsure` = other ∧ L ∧ R ∧ unsure.
  - **Identity, pinned by test:** the rows `list_tasks(scope="all")` returns for the same other arguments are exactly the default listing's rows plus the `excluded by scope` rows plus the `excluded by rule` rows. The sets are disjoint.
  - `task ids pending` keeps its #428 meaning. Under `fields` it also ignores R (D2).
  - `tasks with cut tags` and `tasks with cut text` keep theirs, and include R like any other filter.

### D5. The response: one rule line, present only while a rule is in force

The rule line is present **iff** `scope="default"` and the effective rule is not `all`. Under the rule `all`, and under `scope="all"` with any rule, every response is byte-identical to v1.3.0.

**`list_tasks`.** The rule line is line 3, after the query line. While the rule line is present, the query line is present too: its trigger "the default scope excludes anything" is extended to "or a task rule is in force". The query line's own text is unchanged. The exact formats:

```
Rule — task rule: fields — task tags: <n>[ [<name>, … +K more]] — excluded by rule: <N> tasks — kept though unsure (tags cut): <U> tasks[ — rule approximate for <P> tasks until the next index pass] — pass scope="all" to list every checkbox
Rule — task rule: filter <json F> — excluded by rule: <N> tasks — kept though unsure (text cut): <U> tasks — pass scope="all" to list every checkbox
```

- `<json F>` is `json.dumps(F, ensure_ascii=False)`, so quotes and backslashes are escaped and the value is unambiguous. F has no control characters (D9). It is operator-set text, never note text.
- Task-tag names are listed through the query line's `_names_part`, each cut at 32 characters, while the line stays within its bound. Then `+K more`. The count is always present.
- Every count is stated even at 0. The P token appears only when P > 0, like the closing notice.
- `pass scope="all" to list every checkbox` is the instruction an agent acts on. It names the argument and its value literally.

**Bound: `RULE_LINE_MAX = 320` characters.** Every part except the tag names is mandatory and never cut. With 10-digit counts:

| Line | Mandatory length |
|---|---|
| `fields` | 24 + 16 + 37 + 50 + 66 + 42 = **235** (the remaining 85 characters list names) |
| `filter` | 25 + 130 (64 characters, each escaped to 2) + 37 + 50 + 42 = **284** |

These are paper figures from the format strings. The S1 render test builds both worst cases and pins the measured lengths at or below them, as #428's S4 audit did for the query line.

**The response-cap guarantees** (#428 D12 measured figures, plus at most `RULE_LINE_MAX` + 1 for the line and its newline):

| Guarantee | #428 | With the rule line |
|---|---|---|
| a row always renders | 1,531 | ≤ 1,852 |
| never refused | 1,600 | **1,921** |
| 40 + 40 characters of path and text | 1,608 | ≤ 1,929 |
| 40 + 40, with the incomplete notice | 1,641 | **≤ 1,962** |

The spec pins 1,921 and 1,962. The implementation records its measured figures in `task_render.py` constants and pins them with tests (`test_rule_line_min_budget`, `test_rule_line_full_budget`). The default cap is 40,000, so none of this binds in practice. An empty result gives way as today (`_render_empty`): first the names, then the existing echo steps. Mandatory parts are never cut.

**`task_agenda`.** The same line (`render_rule_line`) follows the agenda header line under the same condition. Its counts are taken over the union of the five sections:

- `excluded by rule` = open ∧ union ∧ tag filters ∧ L ∧ ¬R;
- `kept though unsure`, likewise;
- P = the agenda's `task ids pending` computed without R.

`AGENDA_MIN_BUDGET` stays 2,800 without the line, and becomes **3,121** (2,800 + 321) with it.

Under `fields`, every agenda row has a due or scheduled date, so `excluded by rule` is always 0 there. The line still appears, so the agenda states the rule the same way the listing does. Each section's `continue:` arguments already say "with this call's … scope". A `list_tasks` continuation under `scope="default"` applies the same rule, and its cursor digest matches (D6).

**Rejected:**

- **Rule tokens on the query line.** The query line was measured at 591 of 600 characters. The `filter` tokens alone are 284 characters. Raising its bound would change the guarantees of every query-line response, including ones with no rule.
- **A rule token under `rule=all`.** It would change every default response for every deployment, against the brief's byte-identity preference, and it adds nothing an agent needs.
- **A rule token under `scope="all"`.** The query line already says `scope: all`, which means nothing was applied.

### D6. The cursor digest carries the rule only when it applies

`filters_digest` adds a `"rule"` key to its payload only when `n.scope == "default"` and the effective rule is not `all`:

- `{"kind":"fields","tags":[sorted T]}` under `fields` (F is not included: it does not change the rows);
- `{"kind":"filter","filter":F}` under `filter` (T is not included).

Consequences:

- Under `all`, and under `scope="all"`, the payload is byte-identical to v1.3.0. Every outstanding cursor survives the deploy.
- A cursor minted under one rule is refused (`invalid_argument`, the existing `CURSOR_FOREIGN` text) after a change of kind, of task tags under `fields`, or of the filter text under `filter`. That is #428 L4's settings-change case.
- `task_agenda`'s continuation cursors use the same function and stay valid in the listing.

### D7. The write tools: `add_task` follows the rule, nothing else changes, and every write states a non-task

**`add_task` under `filter`.**

- After `serialize_new_task` builds the line, if the line's body (the text after `- [ ] `, the same text the extractor stores) does not contain F as a case-sensitive substring, the description passed to the serialiser becomes `F + " " + text`, and the line is serialised again. This is the plugin's own placement (`GlobalFilter.prependTo`).
- **Prepend, not append before the field run** (the brief suggested appending, "like a tag"). Prepending:
  - is what the plugin does;
  - cannot touch the trailing field run, so the line parses the same whether or not F is a tag;
  - keeps F inside the first 1,000 characters, so the stored `text` always contains it and D3's unsure case cannot arise for a server-added line;
  - needs no special case for a non-tag filter such as `TODO` or `[task]`.
- **Already contains F** (in the text, in a tag the caller passed, `tags=["task"]` with F = `#task`, or anywhere in the serialised body): nothing is added.
- **Case differs** (`#Task` in the text, F = `#task`): the substring test is case-sensitive, as the plugin's is, so F is prepended. The response says so, and the agent sees both.
- **Length.** The D4 description bound (1,000 characters) applies to the combined description. A `text` whose combination would exceed it is refused `invalid_argument` with "text plus the global filter text (N characters) exceeds 1,000 characters", writing nothing.
- The duplicate key, the similar terms and the D5 verification all use the final line.
- The response, and the dry run, carry `global filter added: <json F>` on its own line after the `line N:` line.

**`add_task` under `fields`.** The line is written as asked. The server imposes no policy. When `task_rule.extracted_is_task` says the new line is not a task, the response and the dry run carry one line after the `line N:` line:

```
note: not a task under your task rule "fields" (no task field and no task tag), so default listings will not show it; list_tasks(scope="all") will.
```

`set_created_date` adds `➕`, which is a field, so with that setting on every added line is a task.

**`update_task`.** It is unchanged in what it writes. After a successful write, or a dry run, when the resulting line is not a task under the effective rule (whatever it was before), the response carries the same line, worded per rule:

```
note: not a task under your task rule "fields" (no task field and no task tag), so default listings will not show it; list_tasks(scope="all") will.
note: not a task under your task rule "filter" (the line does not contain <json F>), so default listings will not show it; list_tasks(scope="all") will.
```

`update_task` never re-adds F (L8). `complete_task` is unchanged in what it writes and what it says.

**One verdict for the statement.** The note line is decided by `extracted_is_task` over the `ExtractedTask` of the written line, which comes from `tasks.tasks_from_note_text`, the definition every writer shares (#428). A unit test runs a corpus through both `extracted_is_task` and `rule_clause` on real Postgres. Over version-5 rows, the two agree on every row. The statement never changes the outcome classification or the usage row.

**Duplicate detection.**

- **Index side (#428 D9 (b)) and the similar-task list:** both go through `scope_clause`, so they consider only tasks by the rule (D4). A plain checklist line elsewhere no longer refuses or crowds a new task.
- **Target-note side (#428 D9 (a)) stays rule-independent**, as it is scope-independent today. It is a byte check of the note being written. A same-description checkbox in the same note is most likely the thing the agent means: `update_task` can give that line a date instead of adding a second one. `allow_duplicate=true` remains the escape. Under `filter` the new line carries F, so a plain checkbox without F has a different description key and cannot collide. This is listed as owner question Q1.

**The rule never blocks a write.** Write resolution reads the note's bytes (#428 D2) and never consults `note_tasks` scope, so an id from a `scope="all"` listing works under any rule.

### D8. Storage: three columns on `task_settings`, the rule inherited as one unit

| Column | Type | Meaning |
|---|---|---|
| `task_rule` | `varchar(8) NULL` | `all`, `fields` or `filter`; NULL means "use the server's rule" |
| `task_tags` | `varchar(128)[] NOT NULL DEFAULT '{}'` | ≤ 32, stored lower-case without `#` |
| `global_filter` | `varchar(64) NULL` | NULL means none |

A CHECK, `ck_task_settings_task_rule`, enforces `task_rule IN ('all','fields','filter')`; NULL passes.

**Inheritance is per rule, not per field.**

- `task_rule` NULL: the effective rule is the server's whole rule (`TASKS_RULE`, `TASKS_RULE_TAGS`, `TASKS_GLOBAL_FILTER`).
- `task_rule` set: the effective rule is the row's kind, the row's `task_tags` and the row's `global_filter`.
- **Rejected: per-field NULL inheritance**, the brief's "nullable columns, NULL follows the server default". A list column then needs a fourth state ("explicitly none" against "inherit"), which the card cannot show without extra controls. Worse, a user who picks `filter` could silently inherit a filter text they never see. With one unit, what the card shows is exactly what is used.
- `task_tags` mirrors `excluded_note_tags` (`NOT NULL DEFAULT '{}'`). `global_filter` is NULL rather than empty.

**`EffectiveTaskSettings`** gains `task_rule: str`, `task_tags: tuple[str, ...]`, `global_filter: str | None` and `task_rule_inherited: bool`. `effective()` is still one indexed read per call with no cache, so a saved change applies on the caller's next call. A tool already in flight keeps the snapshot it read.

### D9. Environment defaults and validation

| Variable | Default | Validation |
|---|---|---|
| `TASKS_RULE` | `all` | one of `all`, `fields`, `filter` (stripped, case-insensitive) |
| `TASKS_RULE_TAGS` | empty | comma-separated (`NoDecode`, the `FTS_CONFIGS` pattern); each entry one leading `#` stripped, lower-cased, and must pass the card's tag rule (`task_settings.normalise_tag`: the tag grammar, ≤ 128, no trailing `/`); ≤ 32 distinct |
| `TASKS_GLOBAL_FILTER` | empty | the filter rule below; empty is allowed unless `TASKS_RULE=filter` |

- **Startup refusal, as #428 did for `TASKS_DEFAULT_TIMEZONE`.** The checks are `Settings` field validators plus one model validator: `TASKS_RULE=filter` with an empty `TASKS_GLOBAL_FILTER` is refused naming both variables.
- Like the timezone, an invalid value therefore also stops the migration init container, which imports `Settings`. That is deliberate and documented. The values are image-independent text, so a value the init container refuses the app would refuse too, and failing before the migration is the earlier, clearer failure.
- The validators are pure functions in `task_rule` and are shared with `save`. `config.py` imports them inside the validator body, so no import cycle reaches `Settings` at module load.

**The filter text rule** (the same for `TASKS_GLOBAL_FILTER` and the card):

- spaces and tabs are stripped at both ends;
- 1–64 characters;
- no control character, line break or line/paragraph separator (`task_settings._CONTROL_RE`);
- no backtick: it would open inline code and change how the added line is masked;
- no code point of `task_line.ALL_SIGNIFIERS`, and no U+FE0F: a field emoji would add a field to every line it is prepended to.

Each check has its own readable message (spec). Rejected: allowing any string, as the plugin does. The plugin never prepends to a line its own reader then re-reads with masking. These refusals cost nothing realistic: real filters are `#task`, `TODO` or `[t]`.

**`save()` semantics.**

- `task_rule` takes `default` (store NULL), `all`, `fields` or `filter`. Anything else is refused with "Unrecognised value for the task rule."
- `task_tags` is one per line, at most 32.
- `global_filter` is blank (store NULL) or text that passes the rule.
- **An absent key leaves the stored value unchanged**, for all three, as for the four booleans. A client posting part of the card must not erase a rule.
- **The combination check runs in `save()` after the row is locked**, over the merged result (submitted values over stored ones). A row whose own rule is `filter` must have a non-NULL `global_filter`. Refusal: "The “filter” rule needs a global filter text. Enter the text every task line contains (for example #task), or choose another rule." Nothing is written.
- **Every other combination is accepted and means what the card says:**
  - `fields` with no task tags means "fields only";
  - tags or a filter text kept while the rule is `all`, `default` or the other kind are stored and unused, and the card labels each input with the rule that uses it.
  
  Rejected: refusing unused values. It would erase a user's filter text when they switch rules to compare.

### D10. The Tasks card

There is one new block in `_task_settings_card.html`, titled **"Which checkboxes are tasks"**, above the timezone. It has three controls:

- **`task_rule`, a select**, consistent with the card's existing three-way selects:
  - `default`: "Server default (<the server rule in words>)";
  - `all`: "Every checkbox is a task";
  - `fields`: "Only checkboxes with task details or a task tag";
  - `filter`: "Only checkboxes that contain the global filter text".
- **`task_tags`, a textarea**, one per line like the card's other lists. Labelled "Task tags (used by “task details or a task tag”)".
- **`global_filter`, a text input**, `maxlength="64"`, placeholder `#task`. Labelled "Global filter text (used by “global filter”)".

The help text is for people who have never installed the Tasks plugin:

> Agents list, check off and add tasks in your notes. A task is a Markdown checkbox (`- [ ] …`). If you also use checkboxes for checklists, steps or reference cards, choose which ones count as tasks. "Task details" are the optional emoji fields of the Obsidian Tasks plugin, which you can type without it: 📅 due, ⏳ scheduled, 🛫 start, ➕ created, ✅ done, ❌ cancelled, 🔺⏫🔼🔽⏬ priority, 🔁 repeat. The global filter is the Tasks plugin's "Global task filter": if you set one there, set the same text here. Agents can always ask for every checkbox.

Under the select: "In use now: <rule in words>[, task tags: …][, filter: …] (server default | set here)". With the server default selected, the server's tags or filter are shown read-only in that line.

Like the existing card, the block has no `<script>`, no inline handler, no `style=` and only existing utility classes. `panel.js` is untouched: every control is always visible, so no show/hide logic is needed. The routes pass the three new names through `_save_task_settings`'s allow-list, and nothing else changes in them. The `page_account_*` goldens are regenerated deliberately, and the owner walks the card by hand with devtools open (zero CSP violations).

### D11. Migration 040 and 039's allow-list

**`040_task_rule.py`**, `down_revision = "039"`, in 039's house style:

- adds `task_rule`, `task_tags` and `global_filter` (D8) and the CHECK `ck_task_settings_task_rule`, each with a comment marker `"… (040_task_rule)"` (`COMMENT ON COLUMN` / `COMMENT ON CONSTRAINT`);
- reconcile-or-refuse:
  - a column is verified by type, nullability, default and marker;
  - the CHECK by its validated definition against a TEMP-table probe and its marker;
  - a same-named object of another shape, or one without the marker, is refused by name before anything is created;
- `search_path` pinned; `lock_timeout` and `statement_timeout` set and `RESET`;
- **no data written**: no row is updated, and `ADD COLUMN … DEFAULT '{}'` is metadata-only on PostgreSQL 16;
- `downgrade()` drops only marked objects, CHECK first;
- needs no superuser and creates no extension.

**039 gains an allow-list.** 039 compares `task_settings` completely, against a TEMP twin of its own DDL (columns, constraints, indexes). A stamp-back to 038 that re-runs 039 over a 040 database would therefore refuse the table as foreign. The precedent is 032's allow-list for 039's `note_tasks` units. 039's `_shape` comparison leaves out exactly:

- the columns `task_rule`, `task_tags` and `global_filter`;
- the constraint `ck_task_settings_task_rule`,

each only when it carries its exact 040 marker. Any other extra column or constraint, or one of these names with another comment, is still compared and refused. The 032 allow-list is untouched: 040 adds nothing to `note_tasks`. A future revision adding to `task_settings` must extend 039's list on purpose.

**Models and checks.** `src/models/db.py` mirrors the three columns and the CHECK (in `__table_args__`), with their markers, so `alembic check` reports "No new upgrade operations detected". `task_settings` is already durable for backups, so the new columns are dumped with it, and `tests/test_backup_derived_tables.py` is unchanged.

**The previous image against the 040 schema.**

- v1.3.0 maps `TaskSettings` without the new columns. Its insert names its own columns, so the new ones take NULL, `'{}'` and NULL, and its update touches only its own. It never reads them.
- It therefore runs, and lists every checkbox: a rollback silently reverts to rule `all` (L6).
- `alembic downgrade 039` (with the new image) drops the three columns and every saved rule.

### D12. Tool surface: descriptions only

No argument, schema, annotation, title, count or refusal code changes. The 1.0 tool-contract promise holds: only response text and descriptions are added to.

- **`list_tasks`** (3,895 characters).
  - The `scope` argument line becomes "`default` applies your task settings: excluded folders, excluded note tags and the task rule (which checkboxes count as tasks); `all` ignores them."
  - The header sentence becomes "how many tasks your excluded folders, note tags and task rule removed".
  - It must stay ≤ 4,000. If the measured length exceeds it, another sentence is tightened; the limit is not raised.
- **`task_agenda`**: the same `scope` wording.
- **`add_task`** (3,699 characters) gains one sentence: "Under a `filter` task rule your global filter text is put at the start of `text` when the line lacks it; under `fields`, a line with no task field or task tag is still written and the response says default listings will not show it."
- **`update_task`** (3,932) and **`complete_task`**: unchanged. `update_task`'s statement is self-explaining in the response.
- **`SERVER_INSTRUCTIONS`**: unchanged.

The fixtures `tools_list_full_v2.json` and `tools_list_readonly_oauth.json` are regenerated for the three descriptions. The test deriving v1 from v2 still holds, since `list_tasks`'s description was already its stated exception. The description-cap test runs unchanged.

## What existing behaviour changes

**Under the default rule (`all`, every existing deployment and user): nothing.** Every response, header, cursor, refusal and write is byte-identical to v1.3.0. Pinned by:

- the existing golden and render tests;
- an integration test that runs a fixed call set under an absent row, under a row with `task_rule='all'` and tags and filter filled in, and under v1.3.0's expectations;
- a cursor minted before the change and accepted after it.

Once a user picks `fields` or `filter`:

1. Default-scope `list_tasks` and `task_agenda` responses carry the query line (when they did not already) and the rule line. They exclude non-tasks and count them.
2. `add_task` prepends the filter text (`filter`), and `add_task` / `update_task` add the note line.
3. `add_task`'s index-side duplicate refusal and similar list ignore non-tasks.
4. Cursors minted before a rule change are refused once.
5. Startup refuses an invalid `TASKS_RULE`, `TASKS_RULE_TAGS` or `TASKS_GLOBAL_FILTER`, or `TASKS_RULE=filter` with no filter text. That is a new boot refusal, but only for a value someone set.

## Accepted limitations

- **L1**: The rule reads indexed columns only. An invalid date (stored NULL) and the unindexed fields `🆔`, `⛔` and `🏁` do not make a line a task under `fields`.
- **L2**: Dates and priority are read anywhere on the line (#428 L2), so a checklist line quoting `📅 2026-01-01` counts under `fields`.
- **L3**: `filter` is a case-sensitive substring test of the whole body, as the plugin's is. It also counts the text inside a field value or inline code.
- **L4**: Rows whose verdict depended on cut text or cut tags are listed (D3). They may be non-tasks, and they are counted.
- **L5**: During a version-5 backfill, `fields` approximates recurrence, created and cancelled by signifier presence (D2). The rule line says so.
- **L6**: Rolling back to v1.3.0 reverts listings to every checkbox without a statement, because the old image knows no rule.
- **L7**: The note line is decided from the written bytes. The listing reflects them after the write-time refresh (#428 D10), or after the next pass when the refresh lagged.
- **L8**: `update_task` never re-adds the global filter. A description replacement that drops a non-tag filter makes the line a non-task, and the response says so.
- **L9**: The duplicate check against the target note's own bytes ignores the rule (D7, Q1).
- **L10**: The plugin removes a global-filter tag from a task's tags when it reads the line. This server keeps it in `tags` and `tag_prefixes`, so `list_tasks(tags=["task"])` matches every filter-rule task when F is `#task`.
- **L11**: A `scope="all"` response does not mention the caller's rule. Its query line says `scope: all`.

## Test plan

**Unit (no database).**

- `tests/test_task_rule.py`:
  - `extracted_is_task` over each field family, an invalid date, `🆔` only, task tags hierarchical and case-insensitive (`waiting-for` vs `Waiting-For/bob` vs `waiting-forever`), a 65th-tag `tags_cut` row with and without task tags, and `filter` case-sensitivity, a filter in inline code and in a field value, a 1,200-character line with the filter after and before the cut;
  - the validators: every refusal message, `#`-stripping, trailing `/` refused, 33 tags, a 65-character filter, `\n`, U+2028, a backtick, `📅`, U+FE0F;
  - `rule_clause` returns `None` under `all`.
- `tests/test_task_settings.py`:
  - `effective()` inheritance as a unit (row NULL gives the whole env rule; row `fields` with empty tags ignores `TASKS_RULE_TAGS`);
  - `save()` absent keys unchanged; `default` stores NULL;
  - the `filter`-without-text refusal against a stored row (merged check);
  - unused values kept;
  - `Settings` refusals for each variable and the combination, naming the variables.
- `tests/test_task_render.py`:
  - both rule-line worst cases at or below 235 / 284 and ≤ 320, with names filling to the bound and `+K more`;
  - the P token present only when P > 0;
  - `test_rule_line_min_budget`: a row renders and nothing is refused at B = 1,921 with every count at 10 digits, maximal echoes and the rule line;
  - `test_rule_line_full_budget`: 40 + 40 at 1,962 with the notice;
  - the agenda refused below 3,121 with the line and rendered at 3,121;
  - the query line byte-identical with and without the rule line;
  - an empty result with the rule line never refused.
- `filters_digest` is byte-identical under `all` and under `scope="all"`, and changes under each rule edit that matters (D6) and only those.
- The tool-surface tests: description caps, regenerated fixtures, schemas unchanged.

**Integration (`tests/integration/test_task_rule_pg.py`, real Postgres).**

- **Each rule × each consumer.** For `all` / `fields` / `filter`, against the `list_tasks` page, `excluded by scope`, `excluded by rule`, `kept though unsure`, `task ids pending`, `task_agenda` totals and rows, `duplicate_scan` exact and similar, and `scope="all"`.
  - The fixture mixes dated tasks, priority-only, recurrence-only, created-only, cancelled-only, done with `✅`, done without, tagged with a task tag, nested and differently-cased task tags, a lookalike tag, plain checklists, `#task` lines, `#Task` lines, a cut text with and without the filter in the first 1,000 characters, and a 65-tag line.
  - The D4 identity holds for each rule, with folder and note-tag exclusions configured at the same time.
- **Backfill rows.** Rows with `task_key` and the version-5 columns forced NULL:
  - `fields` matches the fallback (a `🔁` row kept, a `➕` row kept, a plain row excluded);
  - `task ids pending` ignores the rule;
  - the rule line carries the P token;
  - `filter` is exact on them.
- **Tenancy.** User 7 under `filter`, user 8 under `fields`, NULL-owned under `all`, with identical notes. Each sees its own rule's listing, counts, agenda and duplicate refusals, and never another owner's rows.
- **Cursor.** A cursor from a `fields` listing is refused after the tags change, refused after a switch to `filter`, and accepted after a change of the unused filter text. A v1.3.0-style cursor under `all` is accepted.
- **SQL and Python agree.** `extracted_is_task` equals `rule_clause` on every version-5 row of the fixture, for every rule.
- **Writes end to end** (`tests/integration/test_task_tools_pg.py`, extended):
  - `add_task` under `filter`: prepended, already present (text or tag), case differs, the length refusal, dry run;
  - `add_task` under `fields`: the note line present without a field, absent with `due`, absent with `set_created_date` on;
  - `update_task` removing the last field, and replacing a description that held a non-tag filter: the note line appears;
  - `complete_task` unchanged;
  - an id from a `scope="all"` listing completes a non-task under `fields`;
  - a plain checklist line elsewhere no longer refuses `add_task` as a duplicate under `fields`, while the same line in the target note still does.
- **Schema** (`tests/integration/test_schema_check.py`, `make test-schema`): 040 fresh, stamp-back to 039 and to 038 over head (both reconcile, with 039's allow-list exercised), impostors (an unmarked `task_rule`; a `task_tags varchar(64)[]`; an unmarked CHECK), the CHECK refusing `'any'`, downgrade dropping only marked units, `test_nonsuperuser_migrations_pg.py`, and `alembic check` clean.

**Panel** (`tests/test_panel_task_settings.py`):

- GET and POST in both modes;
- each refusal flashed with nothing saved;
- absent keys keep stored values;
- the select shows the server default's rule;
- the CSP scan;
- the regenerated `page_account_*` goldens.

## Slice plan

The change is small enough for two agents, in sequence or in parallel. The docs agent needs only this change's text, not the code. Pre-flight: this change committed on the base, typecheck green. Each brief opens with the base check: `alembic/versions/039_task_tools.py` exists, no `040_*` exists, and `openspec/changes/task-rule/` exists.

| Slice | Owns (only these files) | Schema-test container |
|---|---|---|
| **I, implementation** | `src/services/task_rule.py` (new), `src/services/task_query.py`, `src/mcp_server/task_render.py`, `src/services/task_settings.py`, `src/config.py`, `src/models/db.py`, `alembic/versions/040_task_rule.py` (new), `alembic/versions/039_task_tools.py` (the allow-list only), `src/services/task_write.py`, `src/mcp_server/tools.py` (`add_task_impl`, `update_task_impl`), `src/mcp_server/server.py` (three descriptions), `src/control_panel/templates/_task_settings_card.html`, `src/control_panel/routes.py` (the two task helpers), `tests/test_task_rule.py` (new), `tests/test_task_render.py`, `tests/test_task_settings.py`, `tests/test_panel_task_settings.py`, `tests/golden/oidc_off/page_account_*`, `tests/fixtures/tools_list_full_v2.json`, `tests/fixtures/tools_list_readonly_oauth.json`, `tests/fixtures/tools_list_schema_additions.json` (descriptions only, if it carries them), `tests/integration/test_task_rule_pg.py` (new), `tests/integration/test_task_tools_pg.py`, `tests/integration/test_schema_check.py` | `omcp-tr-impl` / 55481 (`SCHEMA_TEST_CONTAINER` / `SCHEMA_TEST_PORT`; also used by `make test-integration`, never concurrently) |
| **D, docs** | `README.md`, `.env.example`, `DEPLOYMENT.md`, `docs/architecture/vault-tools.md`, `docs/architecture/control-panel.md`, `docs/architecture/schema-and-migrations.md`, `CLAUDE.md` | none |

**Seams the supervisor greps after merging** (a production caller of each):

- `task_rule.rule_clause`: called only by `task_query.scope_clause` and the counts.
- `scope_clause`: still called by `_Filters`, `task_agenda_query` and `duplicate_scan`.
- `task_rule.extracted_is_task`: called by `add_task_impl` and `update_task_impl`.
- The `task_rule` validators: called by `Settings` and `task_settings.save`.
- `render_rule_line`: called by both renderers.
- The three new form names: in `_save_task_settings`'s allow-list.

## Open questions for the owner

- **Q1. The target note's own duplicate check ignores the rule (D7).** A plain `- [ ] Renew passport` in `Inbox.md` still refuses `add_task(path="Inbox.md", text="Renew passport", due=…)` under `fields`.
  - **Recommendation: keep.** Same note, same words is almost always the same intent: give that line a date with `update_task`. `allow_duplicate=true` overrides.
- **Q2. Rows whose verdict depended on cut data are kept, not excluded (D3).**
  - **Recommendation: keep.** It is fail-visible, it is counted, and it is vanishingly rare (1,000-character lines, 65-tag lines).
- **Q3. Prepend instead of the brief's "append before the field run" (D7).**
  - **Recommendation: prepend.** It is the plugin's own behaviour, it is parse-safe for a non-tag filter, and it guarantees the stored text contains the filter.
- **Q4. "Server default" inherits the whole rule (kind, tags and filter) rather than each field (D8).**
  - **Recommendation: whole rule.** It is what the card can show honestly.
- **Q5. The default stays `all`.** The evidence argues for `fields` on this vault. The brief and the 1.0 promise keep `all` for every deployment.
  - **Recommendation:** the owner sets `fields` with the workflow tags on their own account after deploy. That is the live exercise in tasks §4.
