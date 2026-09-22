## Why

**#195 (ASVS V3.4.3)** — the admin panel, the login/register pages and the OAuth consent page send no `Content-Security-Policy`. `add_security_headers` (`src/main.py`) emits HSTS, `nosniff`, `X-Frame-Options` and `Referrer-Policy` and stops there; only the two `/transfer/*` pages carry a policy. The omission is deliberate and documented (`docs/architecture/control-panel.md`, "No CSP, and vendored assets"; the comment block in `base.html`): every panel control is driven by an inline `onclick`/`onsubmit`, so the only policy the templates survive is one with `script-src 'unsafe-inline'`, which is no policy.

The panel is the surface that mints API keys, deletes users, reassigns vault roots and changes OAuth grant scope across tenants, and it renders attacker-influenced strings — note titles, paths and tags from any tenant's vault, and `client_name` from the unauthenticated `/register`. Jinja autoescape is currently the only barrier. The assessment found no injection primitive (Codex downgraded the finding to **low**), so this is a second layer, not a fix for a live hole: one future escaping mistake, or a compromised vendored library, should not become script execution with an admin's session and CSRF token.

Codex's audit note adds the constraint that shapes the design: a nonced `style-src` blocks the ~430 inline `style=` attributes the templates carry, so the style policy has to be decided explicitly rather than copied from the transfer pages.

## What Changes

- **The reason for "no CSP" is removed.** All 31 inline event-handler attributes, across nine template files, are replaced: behaviour moves to event delegation on `data-*` attributes in a new static `/admin/static/panel.js`, and the `vault.html` hover handlers become CSS `:hover` rules. The theme toggle's delegated listener lives in the existing pre-paint bootstrap in `_theme.html`, so it keeps working on the auth and consent pages that do not load `panel.js`. No `javascript:` URL exists today and none may be added.
- **Confirmations fail closed.** The eight `onclick="return confirm(…)"` buttons become `type="button"` controls carrying `data-confirm`; the delegated handler asks, then calls `form.requestSubmit()`. Without script the button does nothing — today a missing or throwing handler submits the destructive form unconfirmed.
- **htmx is removed.** It is loaded by `base.html` on every panel page and used by none — no template carries an `hx-*` attribute. Under a CSP it is not inert: htmx turns markup attributes into same-origin requests (`hx-post` + `hx-include` of the page's own CSRF field) and, with its default `allowEval`, into code, so it is precisely the script gadget a nonce policy exists to deny an injected fragment. Chart.js 4.4.7 stays: it uses no `eval`/`Function` and injects no stylesheet (checked against the vendored bytes).
- **An enforced, per-response nonce policy on every HTML response the application renders**, except responses that already set their own (the transfer pages keep theirs byte-for-byte). Every `<script>` and `<style>` element in the panel, auth and consent templates carries the response's nonce, including the two vendored script tags and the pre-paint theme bootstrap, which stays inline. The policy, enumerated from the templates rather than guessed:

  ```
  default-src 'self'; script-src 'nonce-N';
  style-src https://fonts.googleapis.com 'unsafe-inline';
  style-src-elem 'nonce-N' https://fonts.googleapis.com; style-src-attr 'unsafe-inline';
  img-src 'self' data:; font-src https://fonts.gstatic.com; connect-src 'self';
  object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'
  ```

  No `'unsafe-inline'` and no `'unsafe-eval'` for scripts. Inline **style attributes** stay allowed through `style-src-attr 'unsafe-inline'`; style **elements** are nonce-only. `design.md` D3 argues the trade.
- **The consent page's `form-action` names the client's redirect origin.** `POST /authorize` answers with a 302 to the registered `redirect_uri`, and Chromium applies `form-action` to redirects that follow a form submission, so a flat `form-action 'self'` would break every OAuth connection. `GET /authorize` adds exactly the origin of the already-validated `redirect_uri` (HTTPS-only by registration) to that one response's `form-action`, and only when the host can be written as a CSP host-source. D5.
- **`PANEL_CSP=enforce|report-only|off`**, default `enforce`, validated at boot (`Literal`), logged once at startup, with a warning when not enforcing. Rollback is one `.env` line and a container recreate — no rebuild. `report-only` sends the same policy as `Content-Security-Policy-Report-Only`; there is no report collection endpoint.
- **Two gates in the test suite:** a route-level test that every HTML route answers with the header, a fresh nonce per response, and every nonce in the body equal to it — with an inventory check that fails when a new HTML route is added without being covered; and a static scan of every template for `on*=` attributes, `javascript:` URLs, `hx-*` attributes, and `<script>`/`<style>` elements lacking the nonce.

No migration, no new dependency (one fewer vendored file), no new permission, no change to CSRF, OAuth or any route's semantics.

## Capabilities

### New Capabilities

- `panel-content-security-policy`: every HTML response the application renders carries an enforced nonce-based Content-Security-Policy unless it sets its own; the directive set is fixed and enumerated; the consent page's form-action admits exactly the validated redirect origin; a boot-validated mode switch allows rollback; templates carry no inline handlers, no `javascript:` URLs and no un-nonced script or style elements; confirmation-guarded controls fail closed; the panel loads no library that turns markup attributes into requests or code.

### Modified Capabilities

None. `panel-theming`'s requirements (pre-paint bootstrap in all three bases, toggle on every page, charts follow the theme, transfer pages untouched) hold unchanged; this change adds a nonce to the bootstrap and moves the toggle's listener, which no requirement there constrains.

## Impact

- **Server:** `src/config.py` (`panel_csp`), new `src/services/panel_csp.py` (nonce helper, policy builder, Jinja context processor), `src/main.py` (`add_security_headers` sets the policy; startup log line), `src/control_panel/routes.py`, `src/control_panel/users.py`, `src/auth/routes.py`, `src/oauth/routes.py` (register the context processor on each `Jinja2Templates`; `authorize_get` records the redirect origin), `.env.example`.
- **Templates and assets:** all 20 panel/auth/consent templates under `src/control_panel/templates/` except the two transfer pages; new `src/control_panel/static/panel.js`; delete `src/control_panel/static/vendor/htmx-2.0.4.min.js`.
- **Tests:** new `tests/test_panel_csp_headers.py`, `tests/test_panel_csp_templates.py`, `tests/test_panel_csp_config.py`; existing assertions that name inline handlers or htmx are updated (`tests/test_issue_130_hardening_minors.py`, `tests/test_issue_162_quota_gate.py`, `tests/test_issue_77_usage_attribution.py` — see `tasks.md` 1.8).
- **Docs:** `docs/architecture/control-panel.md` ("No CSP" section rewritten, bootstrap bullet rewritten), `CLAUDE.md` stack line and a key-decisions bullet, `README.md` stack line (it still says "htmx, Tailwind").
- **Gates:** `make audit`, full offline suite, `make test-integration` (no schema change, but the panel routes run against real Postgres there), one adversarial Codex pass (auth/permission surface and a change of the consent flow), and a browser pass on the live panel — see `tasks.md` §6 for why that pass is manual.
- Closes #195.
