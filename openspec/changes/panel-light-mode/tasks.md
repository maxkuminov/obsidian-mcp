## 1. Token sweep (zero-visual-diff gate)

- [x] 1.0 FIRST, before any edit: record the baseline manifest of every literal-bearing color declaration (template, selector/context, property, literal); semantic keywords recorded syntactically
- [x] 1.1 Inventory every color literal outside `:root` across all templates; map each to an existing token or a new role token (chart grid, code bg, overlays, shadows, danger surface, etc.)
- [x] 1.2 Replace literals with `var(--…)` in `base.html` and all page templates; extend the `:root` block with the new tokens at current (dark) values — rendered values identical to production
- [x] 1.3 Port `auth_base.html` to the shared token names; keep rendered auth pages visually identical

## 2. Theming machinery

- [x] 2.1 Add the light palette under `:root[data-theme="light"]` plus the `prefers-color-scheme: light` default block guarded by `:root:not([data-theme="dark"])`
- [x] 2.2 Add the shared pre-paint theme-bootstrap inline script (Jinja include used by both `base.html` and `auth_base.html`); try/catch storage access
- [x] 2.3 Add the toggle control to the panel nav and auth pages; explicit choice → localStorage; dispatch `themechange`
- [x] 2.4 usage.html: build Chart.js colors from computed tokens; re-color/update chart on `themechange`

## 3. Docs and verification

- [x] 3.1 Add a theming section to `docs/architecture/control-panel.md` (token contract, dark-canonical decision, no-CSP interplay with the inline bootstrap script)
- [x] 3.2 Literal-sweep check: scripted scan proves no color literals outside token definitions (vendor excluded)
- [x] 3.3 Contrast check for light palette pairs (text/bg, text-2/surface, buttons); record results in the change

## 4. Codex-review additions

- [x] 4.1 Add the replay check: every migrated manifest entry resolves to its baseline literal under dark theme; keyword entries unchanged (manifest itself recorded in task 1.0)
- [x] 4.2 Extract tokens + pre-paint bootstrap into one shared Jinja partial; include from base.html, auth_base.html, AND authorize.html; sync meta theme-color and color-scheme from the script
- [x] 4.3 Toggle + persistence scenarios verified on /authorize as well as login and dashboard
- [x] 4.4 Transfer pages: local token sweep only, keep OS-responsive light-first behavior, no toggle/localStorage; diff response headers before/after with each response's per-request nonce replaced by a canonical placeholder (CSP, Referrer-Policy, Cache-Control byte-identical under that canonicalization); assert every inline style/script nonce equals the CSP nonce in each response
- [x] 4.5 Contrast matrix per spec, covering every normative category: text/text-2 on each surface they appear on, flash/alert text on tinted surfaces, button labels, link text, form-control text (all ≥4.5:1); text-3, chart labels/tooltips, status badges, disabled labels, focus indicators, control borders (≥3:1); composite translucent colors over their actual backgrounds before measuring; record every pair and ratio in the change

## 5. Screenshots (post-deploy; run under supervisor control, not by the implementation agent)

- [ ] 5.1 Stand up a local instance seeded with fictional demo data (fake note paths/titles, fake key names, no real IPs/hostnames in settings view)
- [ ] 5.2 Retake all six README screenshots in the light theme, same filenames (no README edit needed); privacy checklist: no internal IPs/URLs, no real note paths or project names, no infrastructure-revealing key names, no real client IDs
- [ ] 5.3 Verify each new PNG against the checklist by reading the image before committing
