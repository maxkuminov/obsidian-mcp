## 1. Token sweep (zero-visual-diff gate)

- [ ] 1.1 Inventory every color literal outside `:root` across all templates; map each to an existing token or a new role token (chart grid, code bg, overlays, shadows, danger surface, etc.)
- [ ] 1.2 Replace literals with `var(--…)` in `base.html` and all page templates; extend the `:root` block with the new tokens at current (dark) values — rendered values identical to production
- [ ] 1.3 Port `auth_base.html` to the shared token names; keep rendered auth pages visually identical

## 2. Theming machinery

- [ ] 2.1 Add the light palette under `:root[data-theme="light"]` plus the `prefers-color-scheme: light` default block guarded by `:root:not([data-theme="dark"])`
- [ ] 2.2 Add the shared pre-paint theme-bootstrap inline script (Jinja include used by both `base.html` and `auth_base.html`); try/catch storage access
- [ ] 2.3 Add the toggle control to the panel nav and auth pages; explicit choice → localStorage; dispatch `themechange`
- [ ] 2.4 usage.html: build Chart.js colors from computed tokens; re-color/update chart on `themechange`

## 3. Docs and verification

- [ ] 3.1 Add a theming section to `docs/architecture/control-panel.md` (token contract, dark-canonical decision, no-CSP interplay with the inline bootstrap script)
- [ ] 3.2 Literal-sweep check: scripted scan proves no color literals outside token definitions (vendor excluded)
- [ ] 3.3 Contrast check for light palette pairs (text/bg, text-2/surface, buttons); record results in the change
