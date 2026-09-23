## Why

**#289**, the follow-up to #195 (the `panel-csp` change). The panel policy has been enforced in production since 2026-09-22, and one `'unsafe-inline'` is left in it: `style-src-attr 'unsafe-inline'`. It stayed because the panel, auth and consent templates carry 452 inline `style=""` attributes, and converting them was out of #195's scope. It is accepted limitation 1 of the archived `panel-csp` design. Accepted limitation 2 depends on it too: the CSP2 `style-src` fallback carries `'unsafe-inline'` and no nonce, so that a browser without the split directives still honours the attributes.

The residual risk is small, and this change is hardening rather than a fix. Under a hypothetical HTML-injection bug, an injected `style` attribute can restyle the element it sits on: overlay it, hide content next to it, or redress a control. It cannot run script, cannot select other elements, and cannot beacon off-origin. Removing the allowance takes that primitive away and turns the style half of the policy into the same nonce-only shape as the script half. It also lets the legacy fallback carry the nonce, which closes limitation 2 as well.

## What Changes

- **Every inline `style` attribute leaves the panel, auth and consent templates.** The inventory in `design.md` counts 452 across 16 of the 20 panel/auth/consent templates. 423 are static layout, 26 are SVG gem colours, 3 are Jinja-conditional, and none is built by JavaScript. Static declarations become classes: short ones use a generated utility vocabulary, longer ones get semantic page classes. The SVG gem colours become CSS classes, not presentation attributes, because `fill="var(--…)"` does not substitute reliably (see `docs/architecture/control-panel.md`). The Jinja conditionals become conditional classes. Four initial states are later changed by script through CSSOM: the dashboard progress-bar width, the dashboard activity-row fade-in, the settings reset scrim and the user-edit custom-path input. They become ordinary classes that the existing `el.style.*` writes still override. D1–D4.
- **All panel CSS stays in nonced `<style>` elements.** A static stylesheet would need `style-src-elem 'self'`, which admits every current and future same-origin `text/css` response. On an origin whose job is serving stored bytes, that is kept out as defence in depth, so the panel's style policy does not depend on what other routes serve. D5.
- **The policy's style directives become nonce-only:**

  ```
  style-src 'nonce-N' https://fonts.googleapis.com
  style-src-elem 'nonce-N' https://fonts.googleapis.com
  style-src-attr 'none'
  ```

  `'unsafe-inline'` appears in no directive. `style-src-attr` is spelled out as `'none'` and not left out: if it were omitted, a CSP3 browser would fall back to `style-src` for attributes, and the attribute ban would then rest on the absence of `'unsafe-inline'` there rather than on its own directive. The fallback `style-src` now carries the nonce, which closes accepted limitation 2. D6.
- **Regression gates.** The static template scan refuses any `style` attribute, including one a Jinja block emits (`{% if … %}style="…"{% endif %}`). A rendered-body scan through the real middleware stack refuses any element with a `style` attribute on every HTML route. A script scan refuses `style=` in markup strings and `setAttribute('style', …)` in `panel.js` and in template inline scripts. A tripwire on the vendored Chart.js catches any upgrade that starts writing a `style` attribute. The header test drops `'unsafe-inline'` from the expected set and asserts it appears nowhere in the policy. D7.
- **Verification.** A headless Playwright pass runs against a local instance under `PANEL_CSP=enforce` with a seeded vault and seeded data. Every page must show zero CSP violations. Positive controls prove that an injected `style` attribute and an unnonced `<style>` are refused and reported, and that a CSSOM write still applies. A computed-style and geometry diff of every page, before and after the change, catches cascade and layout regressions that a screenshot glance would miss, and a mutation check proves that it can fail. Production then goes `report-only` first, and `enforce` right after a headless Playwright walk by the supervisor shows zero violations (owner decision). D8, D9.

No migration, no new dependency, no new setting, no route change. `PANEL_CSP` stays the single rollback lever.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `panel-content-security-policy`: the enumerated directive set changes. `style-src` gains the nonce and loses `'unsafe-inline'`, `style-src-attr` becomes `'none'`, and no directive admits `'unsafe-inline'`. A new requirement forbids inline `style` attributes in the templates, in rendered panel HTML, and in markup or attributes created by panel scripts.

`panel-theming` is unchanged. Its requirements (charts follow the theme, the toggle on every page, the pre-paint bootstrap) still hold. The gem colours still come from the same theme tokens, now through classes.

## Impact

- **Templates:** `base.html`, `auth_base.html`, `authorize.html`, `account.html`, `dashboard.html`, `reembed_confirm.html`, `settings.html`, `performance.html`, `health.html`, `search_analytics.html`, `usage.html`, `oauth.html`, `keys.html`, `users.html`, `user_edit.html` and `vault.html`. `login.html`, `register.html`, `_theme.html` and `_theme_toggle.html` carry no style attribute today. `base.html` gains a new partial, `src/control_panel/templates/_utilities.html` (a generated, nonced utility `<style>`), and a `{% block page_style %}` in `<head>`. The transfer templates are untouched: they have no style attributes and keep their own policy.
- **Server:** `src/services/panel_csp.py` (`build_policy` and the module docstring). No other Python changes.
- **Script:** `src/control_panel/static/panel.js` needs no change today. It is only scanned.
- **Tests:** `tests/test_panel_csp_headers.py` (expected directives, no `'unsafe-inline'` anywhere, rendered-body `style` scan) and `tests/test_panel_csp_templates.py` (static `style` attribute ban, script scan, Chart.js tripwire).
- **Docs:** `docs/architecture/control-panel.md` (the style-attribute bullet, the "SVG colors ride in `style=""`" bullet, the directive block, the accepted-limitations list), and the panel-CSP bullet in `CLAUDE.md`.
- **Gates:** full offline suite, `make test-integration`, `openspec-verifier`, one adversarial Codex round (the change touches the security-header surface on every panel page), the local enforce-mode Playwright pass with its computed-style diff, and the production report-only walk before the enforce flip.
- Closes #289.
