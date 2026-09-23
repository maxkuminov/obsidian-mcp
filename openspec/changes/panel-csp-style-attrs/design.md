## Context

`panel-csp` (#195, archived 2026-09-22) put a nonce-based Content-Security-Policy on every HTML response rendered by the four panel, auth and consent `Jinja2Templates` instances (`src/control_panel/routes.py`, `src/control_panel/users.py`, `src/auth/routes.py`, `src/oauth/routes.py`). Their shared context processor, `panel_csp.template_context`, marks the request. The policy is enforced in production. Its style half was deliberately split (archived design D3):

```
style-src      https://fonts.googleapis.com 'unsafe-inline'   (CSP2 fallback, no nonce)
style-src-elem 'nonce-N' https://fonts.googleapis.com
style-src-attr 'unsafe-inline'
```

Style *elements* are the real CSS-exfiltration primitive (attribute-selector scraping of the CSRF token, font-based probes), and they are nonce-only. Style *attributes* stayed allowed because there were too many to convert inside #195. That is accepted limitation 1, and accepted limitation 2 (a CSP2-only browser gets `'unsafe-inline'` for elements too) follows from it. #289 is the follow-up.

### Inventory

A scan of the 20 panel/auth/consent templates, excluding the two transfer templates (which have none and keep their own policy), on base `f3ecba5`. "Jinja-conditional" means the attribute or part of its value is emitted by a `{% if %}`. "JS-generated" means a `style=` in a markup string or a `setAttribute('style', …)` in `panel.js` or a template's inline script.

| Template | Static layout | SVG colour (gem marks) | Jinja-conditional | JS-generated | Total |
| --- | ---: | ---: | ---: | ---: | ---: |
| `dashboard.html` | 76 | 0 | 1 | 0 | 77 |
| `performance.html` | 68 | 0 | 0 | 0 | 68 |
| `health.html` | 46 | 0 | 0 | 0 | 46 |
| `search_analytics.html` | 46 | 0 | 0 | 0 | 46 |
| `usage.html` | 38 | 0 | 0 | 0 | 38 |
| `oauth.html` | 34 | 0 | 1 | 0 | 35 |
| `settings.html` | 23 | 0 | 0 | 0 | 23 |
| `keys.html` | 21 | 0 | 0 | 0 | 21 |
| `user_edit.html` | 20 | 0 | 0 | 0 | 20 |
| `vault.html` | 18 | 0 | 1 | 0 | 19 |
| `base.html` | 3 | 14 | 0 | 0 | 17 |
| `users.html` | 15 | 0 | 0 | 0 | 15 |
| `reembed_confirm.html` | 9 | 0 | 0 | 0 | 9 |
| `authorize.html` | 2 | 6 | 0 | 0 | 8 |
| `auth_base.html` | 0 | 6 | 0 | 0 | 6 |
| `account.html` | 4 | 0 | 0 | 0 | 4 |
| `login.html`, `register.html`, `_theme.html`, `_theme_toggle.html` | 0 | 0 | 0 | 0 | 0 |
| `panel.js` | — | — | — | 0 | 0 |
| **Total** | **423** | **26** | **3** | **0** | **452** |

What the table does not show:

- **No attribute interpolates a server value.** No `style="width:{{ pct }}%"` exists. The one per-row width, the dashboard progress bars, already goes through `data-progress` and a CSSOM write (`bar.style.width = …`). The three conditionals are `dashboard.html:206` (warning or muted colour), `oauth.html:134` (the whole `style="opacity:0.5"` on revoked, expired or no-vault-scope token rows) and `vault.html:55` (the selected-note highlight appended to the link's style).
- **Four static attributes are initial states that script later overrides through CSSOM:** the dashboard progress bar (`width:0%`, then `bar.style.width`), the dashboard `.activity-row` (`opacity:0; transform:translateX(-6px)`, then the stagger), the settings `#reset-modal` scrim (`display:none`, then `panel.js` `showModal`/`hideModal`) and `user_edit.html`'s `#vault-custom` (`display:none`, then the inline toggle script).
- **Values repeat.** There are 191 distinct attribute values and 216 distinct declarations. The top value, `font-size:11px;color:var(--text-3)`, appears 26 times. 344 attributes have at most three simple declarations and draw on 118 distinct declarations. 82 have four or more, or a compound value (gradient, transition, multi-value `font-family`).
- **No script builds a style attribute.** `panel.js` writes only `el.style.display`, and the template scripts write only `el.style.*` (the dashboard, `user_edit.html`). There is no `innerHTML`, `insertAdjacentHTML` or `setAttribute('style', …)` anywhere in the panel.
- **The vendored Chart.js (`chart-4.4.7.umd.min.js`) is CSSOM-only.** It has no `innerHTML`, `insertAdjacentHTML`, `cssText`, `insertRule`, `style=` literal or created `<style>` element. It sizes the canvas through `canvas.style.width/height` and `style.setProperty`. On release it restores the initial style through `e.style[t] = …`, and its only `setAttribute` restores `width`/`height`. CSSOM writes are not governed by `style-src-attr`, so Chart.js needs nothing.
- **Python emits no style attributes.** No `Markup(…)` or string-built HTML under `src/` contains `style=`. The two `search_analytics.html` macros are template markup and are counted above.

## Goals / Non-Goals

**Goals:**

- Zero `style` attributes in panel, auth and consent markup: in template source, in rendered responses, and created by panel script.
- `'unsafe-inline'` in no directive of the panel policy, with the style directives nonce-only.
- No visual change. The panel looks and behaves as it does today, which a computed-style diff checks mechanically, not by eye alone.
- Gates that fail the build on regression, so the attribute count cannot drift back.

**Non-Goals:**

- A visual redesign, a CSS framework, a build step or a CSS preprocessor.
- Touching the transfer pages or their policy.
- A second rollback switch that relaxes only the style directives (D6).
- `/docs`, `/redoc`: still outside the surface (archived limitation 4).

## Decisions

### D1 — Static declarations become classes: a generated utility vocabulary for short ones, semantic page classes for the rest

One mechanical rule, so parallel implementers make the same choice:

- **Utility-eligible:** the attribute has at most three declarations, and each value is simple: a keyword, a length, a number, or a single `var(--token)`, with no commas and no other function. Each declaration becomes one utility class. The vocabulary is **generated once**, from the whole pre-change inventory, into the new partial `_utilities.html` (Slice A). Page slices only consume it; they never edit it.
- **Semantic:** everything else (4+ declarations, gradients, transitions, compound values), and every element whose inline state is later overridden by script (D3). It becomes a page-prefixed class in that page's own `{% block page_style %}` (D5). Prefixes: `dash-`, `perf-`, `health-`, `sa-`, `usage-`, `oauth-`, `keys-`, `users-`, `ue-`, `vault-`, `settings-`, `reembed-`, `account-`. `base.html`, `auth_base.html` and `authorize.html` add theirs to their existing nonced `<style>`.
- An element that already has a `class` attribute keeps it and appends the new classes. An element whose semantic class would duplicate an existing component class uses the existing one only if the computed-style diff (D9) proves it identical.

**Utility naming** is deterministic, so a name can be checked by reading it: `u-<css-property>-<value-slug>`. The property is spelled in full. The value slug is lower-cased, with `var(--name)` → `name`, `%` → `pct`, `.` → `_`, a leading `-` → `neg-`, and whitespace → `-`. Examples: `font-size:11px` → `u-font-size-11px`, `color:var(--text-3)` → `u-color-text-3`, `text-align:right` → `u-text-align-right`, `margin:0 auto 14px` → `u-margin-0-auto-14px`, `opacity:0.5` → `u-opacity-0_5`. The generator (a one-off script in Slice A, not tracked) sorts rules by property, then value, and writes one rule per line.

Alternatives considered:

1. *Page-local semantic classes only.* Fully parallel, with no shared file, but the 26-times-repeated `font-size:11px;color:var(--text-3)` would be defined in up to a dozen page blocks, and the next page would copy it again. Rejected.
2. *Hand-named utilities (`.muted-sm`).* Nicer names, but two agents would name the same declaration pair differently, and the review would argue taste. Rejected in favour of the mechanical scheme.
3. *Utilities with `!important`*, which would mimic the precedence of an inline style exactly. Rejected: it would also beat the four CSSOM writes (D3), which set non-important inline styles, and it would silently break the modals and the dashboard animation.

### D2 — SVG gem colours become CSS classes, not presentation attributes

The 26 `style="stroke:var(--primary)"` / `style="fill:var(--gem-facet);stroke:none"` attributes on the gem marks (the `base.html` sidebar and mobile header, `auth_base.html`, `authorize.html`) become classes such as `.gem-edge { stroke: var(--primary); }`, `.gem-facet { fill: var(--gem-facet); stroke: none; }` and `.gem-facet-2 { … }`, plus consent-token variants in `authorize.html`, defined in each base's existing nonced `<style>`. They do **not** become `fill="var(--…)"` presentation attributes. `docs/architecture/control-panel.md` records why: SVG2 parses presentation attributes with the property's own grammar, `var()` substitution in them is not dependable across browsers, and a failed substitution falls back to black. A stylesheet rule is real CSS, which was the reason for the `style=""` in the first place, so a class keeps that property. Existing presentation attributes (`stroke-width`, `opacity`, `fill="none"`) stay. An author rule outranks a presentation attribute, so the class decides where they overlap, exactly as the inline style did.

### D3 — Initial states that script overrides become plain classes, and the CSSOM writes stay

For the four elements listed in Context, the inline initial state moves to a non-`!important` class (`.settings-reset-scrim { display: none; … }`, `.ue-vault-custom { display: none; … }`, `.dash-progress-fill { width: 0%; … }`, `.dash-activity-row { opacity: 0; transform: translateX(-6px); … }`). The script is unchanged: `el.style.display = 'flex'` and the other writes set an inline style **through CSSOM**, which `style-src-attr` does not govern, and a non-important inline declaration beats any non-important class. This is the "dynamic values go through `el.style.*`" path, and it already exists. Nothing is converted to `data-*` plus script, because no attribute carries a server value (Context).

The three Jinja conditionals become conditional classes. The dashboard picks `u-color-warning` or `u-color-text-3` inside the `class` attribute's `{% if %}`. The token row gets a class such as `oauth-row-inactive` under the same condition. The vault link gets `vault-link-selected` in place of the appended declarations. No `style` is emitted on any branch.

### D4 — The cascade order is fixed: base component CSS, then utilities, then the page block

An inline style beats every non-important stylesheet rule. A class does not: it must win by order or by specificity. The order is therefore fixed in `base.html`'s `<head>`: `_theme.html`, then the existing nonced `<style>`, then `{% include "_utilities.html" %}`, then `{% block page_style %}{% endblock %}`. At equal specificity a utility or page class beats the component rules. Where a component rule is *more* specific (for example `.card .title` or `table td` against a single class), the page class is scoped to match (`.oauth-page .x`, or a compound selector). The computed-style diff (D9) finds every such case, and it is the only way the implementer learns of one; nobody is expected to reason the cascade out by hand across 5,400 lines.

`vault.html`'s existing body-level nonced `<style>` block may stay where it is or move into `page_style`. Either is valid. It must still carry the nonce.

### D5 — CSS lives in nonced `<style>` elements, not in a static stylesheet

There are two places the new classes could live:

1. **Nonced `<style>` in the templates** (chosen): the new `_utilities.html` partial, the three bases' existing blocks, and a per-page `{% block page_style %}<style nonce="{{ csp_nonce }}">…</style>{% endblock %}`. The policy is unchanged in shape: `style-src-elem` admits the nonce and Google Fonts, nothing more.
2. **A static file under `/admin/static/`**, which would need `style-src-elem 'self'`. Rejected, as **defence in depth**. `'self'` admits every URL on the origin that answers with `text/css`, now and in the future, not just the panel's own file. Today no route serves tenant-controlled bytes to a plain same-origin subresource request. The `/transfer/*` byte routes redeem a capability only from an `Authorization: Bearer` header and deliberately ignore a token in the path or query (`_bearer` in `src/transfer/routes.py`), and the `GET /transfer/download` document is an HTML page, so a `<link rel=stylesheet>` cannot load vault bytes through them. But the origin is a byte-transport service by design. A future route that serves stored content to a URL alone, such as a shareable link, a raw-file preview or a static mount, would turn `'self'` into a way to load attacker CSS, with nothing in the panel change to show it. Under an HTML-injection bug, that is exactly the stylesheet primitive (attribute-selector scraping of the CSRF token) that nonce-only `style-src-elem` exists to refuse. The nonce keeps the panel's style policy independent of what else the origin serves. A path-scoped source (`https://<host>/admin/static/`) would need the hostname in the policy, and archived D5 forbids request-derived values in it. CSP also ignores source paths after a redirect.

The cost of inline CSS: roughly 3–6 KB more per page (≈118 utility rules plus page blocks), uncompressed and not cached separately. `base.html` already inlines about 740 lines of CSS, so the increment is small (accepted limitation 3).

### D6 — The style directives become nonce-only; `style-src-attr` is `'none'`, spelled out

```
default-src 'self'
script-src 'nonce-N'
style-src 'nonce-N' https://fonts.googleapis.com
style-src-elem 'nonce-N' https://fonts.googleapis.com
style-src-attr 'none'
img-src 'self' data:
font-src https://fonts.gstatic.com
connect-src 'self'
object-src 'none'
base-uri 'none'
frame-ancestors 'none'
form-action 'self'            (consent page: 'self' https:)
```

- **Why `'none'` and not omission.** Without `style-src-attr`, a CSP3 browser falls back to `style-src` for attributes. With the new `style-src` that would also refuse them, because a nonce never matches an attribute. But the ban would then rest on the fallback's contents: whoever someday adds `'unsafe-inline'` to `style-src` "for an old browser" would silently re-allow attributes everywhere. An explicit `'none'` puts the rule in its own directive, is self-documenting in the header, and is what the test asserts.
- **Why the fallback now carries the nonce.** Archived D3 kept the nonce out of `style-src`, because a CSP2 browser that sees a nonce ignores `'unsafe-inline'` and would have refused every style attribute. With no attributes left, that reason is gone. A CSP2-only browser now gets nonce-only style elements and no attributes, the same as a CSP3 browser. That closes archived accepted limitation 2. In a CSP3 browser `style-src` is never consulted for elements or attributes, because the `-elem`/`-attr` directives take precedence.
- **`style-src-elem` stays** even though it now equals `style-src`. Keeping it keeps the enumerated directive set the same shape, so the header test's structure and the documentation still line up. It is also the directive a CSP3 browser actually reads.
- **Not `'unsafe-hashes'`** or hashed attribute values. With zero attributes, there is nothing to hash.
- **The rollback story.** `PANEL_CSP=report-only` (recreate the container, no rebuild) sends the same policy as `Content-Security-Policy-Report-Only`. A missed attribute then renders as before and is reported in the console. The lever is coarse: it also relaxes the script half. A style-only switch was considered and rejected. It would be a second security setting to validate, log and document, protecting against a failure (a missed attribute) that the static and rendered scans make a test failure and the enforce-mode pass would show. A visible layout glitch under `enforce` is cosmetic, not an outage: no control depends on a style attribute for its function once D3's four cases are classes. So the order of preference is: fix forward; `report-only` only if a glitch blocks use; `git revert` of the merge commit as the last resort. The templates and the policy land in one merge, so a revert restores both together.

### D7 — Regression gates

In `tests/test_panel_csp_templates.py` (static, no app):

- **Template attribute ban.** Over every template under `src/control_panel/templates/`, including the transfer pages (they have none, and scanning them costs nothing), after stripping Jinja and HTML comments and blanking `<script>`/`<style>` bodies: no opening tag may carry an attribute named `style`, case-insensitive, whether preceded by whitespace, a quote or a Jinja tag close (`%}style=`). The failure names the file and line.
- **Script scan.** In `panel.js` and in the bodies of every template `<script>`: no `style=` inside a string literal (markup built as a string) and no `setAttribute` whose first argument is the literal `'style'`/`"style"`. `el.style.*` and `style.setProperty` stay allowed: they are the sanctioned CSSOM path.
- **Chart.js tripwire.** The vendored `chart-*.umd.min.js` contains no `innerHTML`, `insertAdjacentHTML`, `setAttribute("style"` or `style=` literal. This is a heuristic on an upgrade, not a proof. The enforce-mode browser pass (D8), which renders every chart, is the real check.

In `tests/test_panel_csp_headers.py` (through the real middleware stack, over the existing HTML-route inventory plus the explicit login-401 and register-400 cases):

- `EXPECTED_DIRECTIVES` becomes the D6 set. `'unsafe-inline'`, `'unsafe-eval'` and `'unsafe-hashes'` appear in **no** directive of the header. `style-src` contains the response nonce (the old assertion that it does not is inverted). `style-src-attr` is exactly `['none']`.
- **Rendered-body scan.** Each HTML response body is parsed with `html.parser`, and no element, HTML or SVG, may carry a `style` attribute. This secondary gate catches markup that the static scan cannot see (a future Python-built fragment, a macro argument). The fake-session fixtures render mostly empty states, so it is not a substitute for the static scan.
- The transfer-page tests stay byte-for-byte: that policy is not touched.

### D8 — Local enforce-mode Playwright pass, with positive controls

The same shape as `panel-csp` task 6.3's substituted pass, but against a local instance of this branch under **`PANEL_CSP=enforce`**, with a throwaway pgvector container and a **seeded** vault and database. #195's pass ran on an empty vault and so missed the subfolder hover and the note click. The seed must make every conditional branch render: a subfolder with notes, and a selected note (vault); a chunk-truncated note (dashboard warning colour); revoked, expired and active tokens (OAuth inactive rows); several API keys, one with a limit; a second user; usage and search-analytics rows; the reindex progress bar; and activity rows.

- A `securitypolicyviolation` listener is registered before any page script (`page.add_init_script`). Pass criterion: **zero** violations on every panel, auth and consent page, including the login 401 and consent pages, in both themes, at 1280 px and 390 px.
- **Positive controls, on one panel page:** (a) `insertAdjacentHTML` of `<div id="pc" style="color: rgb(255, 0, 0)">` records a violation with `violatedDirective`/`effectiveDirective` `style-src-attr`, and its computed colour is not red. (b) An appended unnonced `<style>` records a `style-src-elem` violation. (c) `el.style.color = 'rgb(255, 0, 0)'` applies, and the computed colour is red, with no violation. That last control proves the CSSOM path D3 relies on is open. (d) An injected unnonced `<script>` records a `script-src-elem` violation, proving the listener still sees script violations. If the controls do not fire, the zero-violation result means nothing and the pass fails.
- **Every interactive control from `panel-csp` task 6.3** is walked again: the eight confirms (dismiss sends no POST, accept sends exactly one), the modals, copy, edit-limit, the scope autosubmit, the reindex fetch, the 390 px sidebar, the theme toggle on panel, login and consent pages, Chart.js, and OAuth approve and deny. Also D3's four CSSOM cases: the progress bar animates to its value, the activity rows fade in, the settings reset scrim opens and closes, and the user-edit custom path shows and hides.
- Every HTML document's header is `Content-Security-Policy` with `style-src-attr 'none'` and no `'unsafe-inline'`, and its body nonces equal the header nonce.

### D9 — A computed-style diff against the pre-change tree is the "no visual change" gate

Screenshots miss a 1 px padding change and flag every timestamp. So the same seeded instance is run twice, once on `f3ecba5` (before) and once on this branch (after), with the same theme and viewport. Both runs use `PANEL_CSP=off`: the diff measures the cascade, not the policy, and the before-tree would otherwise differ only by what its own policy allows.

**Settling is bounded and deterministic, and identical in both runs.** Waiting until `document.getAnimations()` is empty would never finish: the gem-glow animations are `infinite` and run on every page, and fill-mode fade-ups stay in the list after they end. So:

1. Install `page.clock` before navigation, at a fixed epoch.
2. After `load`, advance the fake clock by a fixed 5 s. That covers the dashboard's timers: the 120 ms count-up and progress start, the 900 ms count-up, the 1.1 s progress transition, and the activity stagger (200 ms + 55 ms × rows, plus 300 ms). The durations come from the templates, so 5 s is a documented bound, not a guess.
3. Then, for every `Animation` in `document.getAnimations()`: if its effect's computed timing has a finite end (`iterations !== Infinity`), call `finish()`, which applies the final keyframe under its fill mode. Otherwise `pause()` it and set `currentTime` to a fixed 0 ms.
4. Check that no animation is still `running`. If one is, the settle failed and the page is reported as an error, not a pass. Only then take the snapshot.

For each page, theme (light, dark) and viewport (1280, 390), the diff:

- walks every element under `<body>` except `script`, `style`, `template` and the newly added `page_style`/utility `<style>` elements, keyed by a structural path of tag names and sibling indices that skips those elements;
- **compares geometry for every element**: `getBoundingClientRect()` (x, y, width, height, rounded to 0.5 px), taken after scrolling to the top. It is not limited to elements whose old inline style named a width or a height. A lost `max-width`, `min-width`, `inset` or margin shows up as a moved or resized box even when no compared property names it directly;
- **compares computed longhands**: the union of (a) a fixed base list — `display`, `position`, `box-sizing`, `margin-*`, `padding-*`, `border-*-width`, `border-*-style`, `border-*-color`, `border-*-radius`, `font-family`, `font-size`, `font-weight`, `font-style`, `line-height`, `letter-spacing`, `text-transform`, `text-align`, `text-decoration-line`, `text-decoration-color`, `white-space`, `word-break`, `overflow-wrap`, `color`, `background-color`, `background-image`, `opacity`, `transform`, `transition-property`, `transition-duration`, `row-gap`, `column-gap`, `flex-direction`, `flex-wrap`, `flex-grow`, `flex-shrink`, `flex-basis`, `align-items`, `align-self`, `justify-content`, `grid-template-columns`, `overflow-x`, `overflow-y`, `text-overflow`, `z-index`, `vertical-align`, `cursor`, `fill`, `stroke` — and (b) **every longhand of every declaration that was migrated**. The generator expands each declaration in the pre-change inventory into its longhands (for example `margin` → `margin-top`/`-right`/`-bottom`/`-left`, `inset` → `top`/`right`/`bottom`/`left`, `border` → its width, style and colour longhands, and `max-width`, `min-width`, `max-height`, `min-height` as themselves), and all of those are compared on every element, not only on the element that carried the attribute;
- **passes only on zero differences**, apart from an explicit allow-list committed with the result (design.md "Verification record"), each entry with a one-line reason. The expected allow-list is empty.

**Mutation check: the diff must prove it can fail.** Before its result counts, the harness runs once against a deliberately broken copy of the after-tree, with the `max-width: 480px` that replaces `reembed_confirm.html:10`'s inline style removed. At 1280 px that widens the card from 480 to 960 px. The diff **must** report that page as different, in both geometry and `max-width`. If it reports zero differences, the harness is broken and D9 fails. A second mutation removes one gem-facet class's `fill` and must be caught in both themes. The mutation results are recorded next to the real result. This check exists because a Codex spec review showed the first draft of this diff, which compared width and height only where they had been inline, missed exactly the `reembed_confirm.html` case.

The script lives in the scratchpad, not the repo: it needs a running instance, a browser and seeded data, and CI has none of them. Its result (page × theme × viewport counts, differences, allow-list) is recorded in this design under "Verification record". Hover and focus states are not in the diff (accepted limitation 1). A side-by-side screenshot set is still captured for Max, but it is not the gate.

## Risks / Trade-offs

- [A converted class loses the cascade to a more specific component rule, a silent visual change] → D4 fixes the order; D9's computed-style diff finds every loss before merge.
- [A converted element now picks up a component `:hover`/`:focus` rule that its inline style used to mask] → Not in D9's static diff. The Playwright pass hovers the vault links (the known hover surface) and the buttons whose inline style set `color`, `background` or `padding` (the `oauth.html`/`keys.html`/`users.html` action buttons), and the supervisor's report-only walk (Migration Plan) adds the production pages. Accepted limitation 1.
- [A CSSOM toggle stops working because its initial state became `!important` or moved to a higher-specificity rule] → D1 bans `!important`; D3 names the four cases; D8 exercises each.
- [The gem marks render black] → D2 uses stylesheet rules, not presentation attributes; D8 runs both themes; D9 compares `fill`/`stroke`.
- [A missed attribute ships] → three gates (static, rendered, script); under enforce the failure is cosmetic, and `PANEL_CSP=report-only` is a recreate away.
- [Parallel slices diverge on utility names] → D1's names are deterministic and generated once, in Slice A, before the page slices start.
- [Legacy-browser change: a CSP2-only browser now refuses style attributes] → there are none left, and the policy is stricter for it than before.
- [Chart.js upgrade starts writing style attributes] → the D7 tripwire and the D8 chart render.

## Migration Plan

No schema change and no data change. The rollout mirrors #195's owner-decided shape:

1. Merge the branch: templates, policy and tests together.
2. **First production deploy with `PANEL_CSP=report-only`** in the deploy directory's `.env`, then `make deploy`. In report-only mode the browser still reports every would-be violation, as a `securitypolicyviolation` event and in the console. **Owner decision (spec review): the flip does not wait on a hand walk by Max.** The supervisor runs a headless Playwright walk under report-only, with the D8 violation listener and positive controls, over the fixed page list (tasks §6) in both themes and at the mobile width. It runs against the deployed panel when an authenticated browser session can be driven there, and otherwise against a local instance of the deployed image and commit with the same seed as D8. The report records which target was used.
3. Zero reported violations on that walk → `PANEL_CSP=enforce` and a recreate (no rebuild), with no further wait. A short re-check: the login page header shows `style-src-attr 'none'`, and a headless load of one panel page shows zero violations.
4. Rollback at any point: `PANEL_CSP=report-only` and a recreate. A code revert is `git revert` of the merge commit.

Default in code stays `enforce`.

## Accepted limitations

1. **Hover and focus states are checked by eye, not by the computed-style diff.** A converted element can now take a component `:hover`/`:focus` rule that its inline style used to mask. The known surfaces are hovered in D8. Anything else found later, by the supervisor's walk or by Max in normal use, is fixed forward.
2. **The computed-style diff covers the seeded data states only.** A branch the seed does not render is covered by the static scan (no attribute) but not by the "looks the same" check.
3. **Inline CSS is not cacheable separately.** Roughly 3–6 KB more per page, by D5's decision.
4. **`PANEL_CSP=report-only` is the only runtime rollback, and it also relaxes the script half.** No style-only switch, by D6's decision.
5. **The Chart.js tripwire is a heuristic.** A library that builds a style attribute some other way would pass it. The enforce-mode render is the real check.
6. **`panel.js` still has no JS test runner.** Its CSSOM behaviour is covered by D8 (archived limitation 7, unchanged).

Archived `panel-csp` limitations 1 (style attributes allowed) and 2 (CSP2 fallback `'unsafe-inline'`) are **closed** by this change. The others stand.

## Open Questions

None open. **Resolved by the owner after the Codex spec review:** the rollout stays report-only → enforce, but the report-only step is gated by the supervisor's headless Playwright walk, not by a devtools walk by Max, and the enforce flip follows it directly (Migration Plan).

## Spec review history

| Round | Finding | Severity | Resolution |
| --- | --- | --- | --- |
| 1 | D9 missed layout regressions: removing `max-width:480px` (`reembed_confirm.html:10`) widens the card 480 → 960 px at 1280 px, and width and height were compared only where they had been inline | MAJOR | D9 compares every element's bounding rect and every longhand of every migrated declaration (including min/max dimensions, `inset`, `text-decoration`, `word-break`); a mandatory mutation check proves that removing that `max-width` fails the diff |
| 1 | The settle condition (`getAnimations()` empty) never completes: infinite gem-glow animations and fill-mode fade-ups | MAJOR | D9 settle is bounded: fake clock advanced a fixed 5 s, finite animations `finish()`ed, infinite ones paused at 0 ms, and a still-running animation is an error |
| 1 | D5's `/transfer/download` stylesheet example is wrong: bytes are served only with an `Authorization: Bearer` header (path and query tokens are ignored), and the `GET` is an HTML page | MINOR | D5 reframed as defence in depth against future same-origin serving; the no-`'self'` decision stands; task 7.1's docs text corrected |
| 1 | Rollout (owner question) | — | Owner: keep report-only → enforce; the report-only gate is the supervisor's headless Playwright walk, not Max's devtools walk |

## Verification record

*(Filled in during tasks §5–§6: the D9 diff counts and allow-list, the mutation-check results, the D8 result, the report-only walk and its target.)*
