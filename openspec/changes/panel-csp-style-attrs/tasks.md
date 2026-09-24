# Tasks: panel-csp-style-attrs (#289)

Six implementation slices in two waves. Each slice is one worktree and one independent Opus subagent, and the docs slice belongs to the supervisor. **File sets are disjoint and exhaustive.** A subagent that needs a file owned by another slice stops and reports; it does not edit that file. No slice adds `!important` (design D1) or edits `_utilities.html` after Slice A has generated it.

| Wave | Slice | Branch | Owns | Attributes |
| --- | --- | --- | --- | ---: |
| 1 | A: foundation and bases | `wt-psa-a-bases` | new `src/control_panel/templates/_utilities.html`; `base.html`, `auth_base.html`, `authorize.html`, `account.html` | 35 |
| 1 | F: policy and gates | `wt-psa-f-policy` | `src/services/panel_csp.py`, `tests/test_panel_csp_headers.py`, `tests/test_panel_csp_templates.py` | — |
| 2 | B: dashboard and settings | `wt-psa-b-dashboard` | `dashboard.html`, `settings.html`, `reembed_confirm.html` | 109 |
| 2 | C: health and performance | `wt-psa-c-perf` | `performance.html`, `health.html` | 114 |
| 2 | D: analytics | `wt-psa-d-analytics` | `search_analytics.html`, `usage.html` | 84 |
| 2 | E: admin pages | `wt-psa-e-admin` | `oauth.html`, `keys.html`, `users.html`, `user_edit.html`, `vault.html` | 110 |
| — | G: docs (supervisor) | on the merge branch | `docs/architecture/control-panel.md`, `CLAUDE.md` | — |

**Why two waves.** Wave 2 consumes the utility vocabulary and the `{% block page_style %}` hook that Slice A creates. Wave 2 branches from the merge of wave 1, never from `origin/main`. Slice F is independent of every template and runs in wave 1. On its own branch its new static and rendered-body tests fail, because the templates still carry attributes. That is expected, and **F's tests are authoritative only on the fully merged tree** (task 3.2).

**Base check, first line of every subagent brief.** Wave 1: confirm `openspec/changes/panel-csp-style-attrs/design.md` exists and `git log` contains `f3ecba5`. Wave 2: additionally confirm `src/control_panel/templates/_utilities.html` exists and `base.html` contains `{% block page_style %}`. If a check fails, stop and report.

**Every template slice's contract:** apply design D1–D4 exactly: utilities for eligible attributes, page-prefixed semantic classes in `{% block page_style %}<style nonce="{{ csp_nonce }}">…</style>{% endblock %}` for the rest, conditional classes for Jinja conditionals, and plain classes for CSSOM-overridden initial states. When done, `grep -n 'style\s*=' <owned templates>` finds no attribute, only `<style` tags and `el.style` script lines. Report every selector whose specificity had to be raised, and why.

## 0. Spec review before any code

- [x] 0.1 Commit this proposal on `wt-panel-csp-style-attrs` (Codex reads the committed tree). Comment on #289 linking the branch, so the issue shows that work is open.
- [x] 0.2 **Codex reviews the proposal, before implementation.** `codex exec -C <repo> --sandbox read-only --skip-git-repo-check -c model_reasoning_effort=high -o <scratchpad>/spec-verdict.txt --color never - < <scratchpad>/spec-review-prompt.md`, in the background with both redirects. Frame it as a defensive PASS/FAIL control review. "Wrong" here means one of two things: a panel control silently stops working (the panel mints keys and manages cross-tenant grants, and the consent page is the live path for every agent connector), or a visible layout change ships unnoticed. Ask specifically whether:
  - (a) the inventory misses a style attribute source: a macro, Python-built markup, a script, or a vendored library;
  - (b) D1's eligibility rule is ambiguous for any real attribute in the templates;
  - (c) D4's cascade order can let a converted class lose to a component rule without D9 noticing;
  - (d) D6's `'none'`, and the nonced fallback, can break anything in a current Chromium, Firefox or Safari, or in a CSP2-only browser;
  - (e) D5's defence-in-depth reasoning for refusing `'self'` holds;
  - (f) the D9 diff can pass while a page visibly changed.

  Demand a machine-readable closing verdict block. Fold the findings in and record them in `design.md` under a "Spec review history" table before dispatching wave 1. (Round 1: **FAIL**. There were 2 MAJOR and 1 MINOR findings, and all three are folded in. The rollout was decided by the owner. See the Spec review history in `design.md`. A round 2 on the spec is at the supervisor's discretion.)
- [x] 0.3 `openspec validate panel-csp-style-attrs --strict` passes.

## 1. Wave 1 (parallel)

### Slice A: foundation and bases

- [x] 1.1 Write a one-off generator (scratchpad, not tracked) over the **pre-change** `src/control_panel/templates/*.html`. It collects every declaration of every utility-eligible attribute (D1: at most three declarations, each a keyword, length, number or a single `var(--token)`), skipping SVG `fill`/`stroke` colour attributes, and emits `src/control_panel/templates/_utilities.html`: a leading `{# … #}` comment naming #289, the naming rule and "generated, do not hand-edit; add page classes in `page_style` instead", then one `<style nonce="{{ csp_nonce }}">` holding one `u-<property>-<value-slug>` rule per declaration, sorted, no `!important`. Expect about 118 rules.
- [x] 1.2 `base.html`: after the existing nonced `<style>` in `<head>`, add `{% include "_utilities.html" %}` and then `{% block page_style %}{% endblock %}` (D4 order). Convert the 14 gem-mark colours (sidebar and mobile header) to classes in the existing `<style>` (D2), and the 3 layout attributes (the header bar, the logout form and the brand row) to utilities or `base-` classes.
- [x] 1.3 `auth_base.html`: 6 gem-mark colours to classes in its existing `<style>` (D2). `auth_base.html` does not include `_utilities.html` (none of its pages needs it).
- [x] 1.4 `authorize.html`: 6 gem-mark colours to consent-token classes (`--consent-primary`, `--consent-gem-facet`, `--consent-gem-facet-2`), and 2 layout attributes to classes in its existing `<style>`.
- [x] 1.5 `account.html`: 4 attributes, as utilities or `account-` classes in `page_style`.
- [x] 1.6 Commit on `wt-psa-a-bases`: `panel-csp-style-attrs slice A: utility vocabulary, page_style hook, bases and gem marks (#289)`.

### Slice F: policy and gates

- [x] 1.7 `src/services/panel_csp.py` `build_policy`: `style-src 'nonce-N' https://fonts.googleapis.com`, `style-src-elem` unchanged, `style-src-attr 'none'` (D6). Rewrite the module docstring's "Styles are split on purpose" paragraph: style elements are nonce-only, style attributes are refused, the fallback carries the nonce and why, and CSSOM writes are the sanctioned dynamic path.
- [x] 1.8 `tests/test_panel_csp_headers.py`: `EXPECTED_DIRECTIVES` becomes the D6 set. Invert the "no nonce in `style-src`" assertion: it now holds the response nonce. Assert `style-src-attr == ["'none'"]`. Assert that `'unsafe-inline'`, `'unsafe-eval'` and `'unsafe-hashes'` appear in **no** directive. Add the rendered-body scan: parse each HTML response in the existing route inventory, and the login-401 and register-400 cases, with `html.parser`, and assert that no start tag or self-closing tag has a `style` attribute (D7). Leave the transfer-page expectations untouched.
- [x] 1.9 `tests/test_panel_csp_templates.py`: add the static attribute ban over every template (after comment stripping and script/style body blanking), matching an attribute named `style` preceded by whitespace, a quote or `%}`, case-insensitive, and reporting file:line. Add the script scan over `panel.js` and every template `<script>` body: no `style=` inside a string literal, and no `setAttribute` with a literal `'style'`/`"style"` first argument; `el.style.*` and `style.setProperty` are allowed. Add the Chart.js tripwire over `static/vendor/chart-*.umd.min.js` (D7). Add one self-test per scenario in the spec delta, feeding a synthetic string to the scanning helper: static, conditional (`{% if x %}style="…"{% endif %}`), SVG, `setAttribute('style', …)`, markup string, and a CSSOM write that must pass. The self-tests prove the scanner, not the templates.
- [x] 1.10 Commit on `wt-psa-f-policy`: `panel-csp-style-attrs slice F: style-src-attr 'none', nonced fallback, style-attribute gates (#289)`. Report which new tests fail on the unconverted tree; that is expected.

### Wave 1 merge

- [x] 1.11 The supervisor merges A and F into `wt-panel-csp-style-attrs`, then confirms `_utilities.html` renders with the nonce on a panel page and that the existing suite, apart from F's new style-attribute tests, is green. Wave 2 branches from this merge.

## 2. Wave 2 (parallel, from the wave-1 merge)

- [x] 2.1 **Slice B** (`wt-psa-b-dashboard`): `dashboard.html` (76 static, plus the chunk-truncated conditional colour → conditional utility class, D3; the progress-bar `width:0%` and the `.activity-row` `opacity`/`transform` initial states become `dash-` classes, and the existing CSSOM writes stay), `settings.html` (23; `#reset-modal`'s `display:none` becomes a `settings-` class and `panel.js` `showModal`/`hideModal` stay unchanged), `reembed_confirm.html` (9). Commit: `panel-csp-style-attrs slice B: dashboard, settings, reembed (#289)`.
- [x] 2.2 **Slice C** (`wt-psa-c-perf`): `performance.html` (68), `health.html` (46). Commit: `panel-csp-style-attrs slice C: performance, health (#289)`.
- [x] 2.3 **Slice D** (`wt-psa-d-analytics`): `search_analytics.html` (46, including the attributes inside the `group_cell`/`group_tables` macros), `usage.html` (38). Leave the Chart.js canvas containers' sizing to classes; Chart.js's own CSSOM sizing is untouched. Commit: `panel-csp-style-attrs slice D: search analytics, usage (#289)`.
- [x] 2.4 **Slice E** (`wt-psa-e-admin`): `oauth.html` (34, plus the inactive-token-row conditional → `u-opacity-0_5`, the generated utility, as implemented), `keys.html` (21), `users.html` (15), `user_edit.html` (20; `#vault-custom`'s `display:none` becomes a `ue-` class, and the inline toggle script stays), `vault.html` (18, plus the selected-note conditional → `vault-link-selected`; the existing `.vault-link` hover rule must keep working for unselected links and must not apply to the selected one, as today). Commit: `panel-csp-style-attrs slice E: oauth, keys, users, user edit, vault (#289)`.

## 3. Merge and gates

- [x] 3.1 The supervisor merges B–E into `wt-panel-csp-style-attrs`. `grep -rnE '(\s|["'"'"'}])style\s*=' src/control_panel/templates/` returns nothing, and neither does `grep -n 'style=' src/control_panel/static/panel.js`.
- [x] 3.2 Full offline suite green, including F's gates, now authoritative. Then `make test-integration` and `make audit`.
- [x] 3.3 **Seam check:** every class used in a converted template is defined (the utility partial, a base `<style>`, or that page's `page_style`), and every `u-` class in `_utilities.html` is used at least once. A small scratchpad script, with its result recorded in the PR.
- [x] 3.4 `openspec validate panel-csp-style-attrs --strict` passes.

## 4. Independent verification

- [x] 4.1 **`openspec-verifier`** subagent on the merged tree, against this proposal, the design and the spec delta. Iterate to zero blocking gaps.
- [x] 4.2 **Adversarial Codex, one round** (the security-header surface on every panel page): give it the spec delta, D1–D7 and the changed files, and ask it to find a control that no longer works, a style attribute source the gates miss, a class that loses the cascade, or a policy value that breaks a current browser. Triage by the review budget: two rounds by default, declined findings recorded under accepted limitations, one line each.

## 5. Local browser verification (design D8, D9)

- [x] 5.1 **Seeded local instance.** A throwaway pgvector container (not concurrently with `make test-schema` / `make test-integration`, which share its port) and the app from this branch with `PANEL_CSP=enforce`. The seed must render every conditional branch: a vault subfolder with notes and a selected note; a chunk-truncated note; active, revoked, expired and no-vault-scope tokens; several API keys, one with a daily limit; a second user; usage and search-analytics rows; a reindex progress bar; and activity rows.
- [x] 5.2 **Headless Playwright pass under `enforce`**, with a `securitypolicyviolation` listener installed by `add_init_script` before any page script. **Positive controls first, on one panel page:** (a) an injected `<div style="color: rgb(255, 0, 0)">` records a `style-src-attr` violation and does not render red; (b) an appended unnonced `<style>` records a `style-src-elem` violation; (c) `el.style.color = 'rgb(255, 0, 0)'` applies with no violation; (d) an injected unnonced `<script>` records a `script-src-elem` violation. If any control does not behave as stated, the pass fails.
- [x] 5.3 On every panel, auth and consent page (including the login 401 and the consent page), in both themes, at 1280 px and 390 px: **zero** CSP violations. Every document carries `Content-Security-Policy` with `style-src-attr 'none'`, no `'unsafe-inline'`, and body nonces equal to the header nonce. Walk again every control from archived `panel-csp` task 6.3: the eight confirms (dismiss sends 0 POSTs, accept sends exactly 1), the modals, copy, edit-limit, scope autosubmit, the reindex fetch (with its CSRF header), the 390 px sidebar, the theme toggle on panel, login and consent pages, Chart.js on usage, performance and search analytics, and OAuth approve (then exchange the code at `/token`) and deny. Plus D3's four CSSOM cases, the vault breadcrumb hover inside a subfolder, a note click, and hovering the converted action buttons on `oauth.html`, `keys.html` and `users.html`.
- [x] 5.4 **Computed-style and geometry diff (D9)** of the same seed on `f3ecba5` and on the merged branch, both under `PANEL_CSP=off`, over every page × {light, dark} × {1280, 390}.
  - Use D9's bounded settle: fake clock, a fixed 5 s advance, finite animations `finish()`ed, infinite ones paused at 0 ms, and any animation still running is an error.
  - Compare every element's bounding rect and the base longhand list plus every longhand of every migrated declaration.
  - **Run the mutation checks first:** removing `reembed_confirm.html`'s `max-width: 480px` class declaration, and removing one gem-facet `fill`, must each be reported as a difference. If either is not, the harness is broken and the task fails.
  - Pass only on zero differences outside a committed allow-list, each entry with a reason. The expected allow-list is empty.
  - Capture a side-by-side screenshot set for Max; it is not the gate.
- [x] 5.5 Record the results in `design.md` "Verification record": the positive controls, violation counts per page, the diff counts and allow-list, and what was not tested.

## 6. Deploy and live checks (report-only first, then enforce; owner decision in design.md Migration Plan)

- [ ] 6.1 Set a Kuma maintenance window for the recreate. Set `PANEL_CSP=report-only` in the deploy directory's `.env`, then `make deploy` from the repo. There is no migration, and `make db-check` stays clean. The startup log shows `report-only` and its WARNING.
- [ ] 6.2 In-container probe (as archived task 6.2): `GET /admin/auth/login` with the configured `Host` returns `Content-Security-Policy-Report-Only` with `style-src-attr 'none'`, `style-src 'nonce-…' https://fonts.googleapis.com`, no `'unsafe-inline'`, and matching body nonces. `/transfer/upload` keeps its own policy unchanged. `/docs` carries no panel policy.
- [ ] 6.3 **The supervisor runs a headless Playwright walk under report-only. The step does not wait on a walk by Max (owner decision).**
  - Use the D8 `securitypolicyviolation` listener installed by `add_init_script`. Report-only violations fire the same event.
  - Run the positive controls (a), (b) and (d) first. Under report-only they must *report*, even though nothing is blocked.
  - Cover the fixed page list: dashboard, keys, OAuth, usage, performance, health, search analytics, settings (reset modal open and cancel), users and user edit (custom vault path toggle), vault (subfolder, note), account, and login and consent, in both themes and at 390 px.
  - **Target:** the deployed panel if an authenticated session can be driven there headlessly. Otherwise, use a local instance of the deployed image and commit, with the D8 seed, `PANEL_CSP=report-only` and the same fixed list. Record which target was used and why.
  - **Pass:** the header is the report-only policy with `style-src-attr 'none'`, there are **zero** reported violations, and the controls behave as before.
- [ ] 6.4 **Flip to enforce as soon as 6.3 passes.** Set `PANEL_CSP=enforce` and recreate (no rebuild). Re-run 6.2, expecting the enforcing header. Load one panel page headlessly and confirm zero violations. Tell Max the flip is done, so that anything he notices in normal use gets reported against this change.
- [ ] 6.5 If a violation or a layout glitch appears at any point: fix forward if it is cosmetic. If it blocks use, return to `PANEL_CSP=report-only` and recreate, fix, and repeat 6.3 before flipping. Last resort: `git revert` the merge.

## 7. Docs (Slice G, supervisor) and archive

- [x] 7.1 `docs/architecture/control-panel.md`: update the directive block. Replace "Style attributes stay allowed, by decision" with the D5/D6 rationale: nonce-only elements, `style-src-attr 'none'` spelled out, the nonced fallback, why not a static stylesheet (defence in depth: `'self'` would admit any current or future same-origin `text/css` response; today's `/transfer/*` byte routes need an `Authorization: Bearer` header and so cannot be loaded as a stylesheet, but the panel's style policy should not depend on that staying true), and CSSOM as the dynamic path. Rewrite "SVG colors ride in `style=""`" as "SVG colours ride in stylesheet classes, never in `fill=`/`stroke=` with `var()`". Describe the utility vocabulary, the `page_style` block, and the no-`!important` rule. Mark archived limitations 1 and 2 closed.
- [x] 7.2 `CLAUDE.md`: the panel-CSP key-decision bullet. Replace "Style *attributes* are allowed (`style-src-attr 'unsafe-inline'`) by decision" with "no inline `style=`; `style-src-attr 'none'`; dynamic presentation through `el.style.*` or classes; SVG colours via classes".
- [ ] 7.3 `openspec archive panel-csp-style-attrs -y`. Open a PR to main with `Closes #289`, then merge it through CI (main is protected). Push. The change is not done until it is on the remote.
