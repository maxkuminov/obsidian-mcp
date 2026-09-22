## Context

The panel, auth and consent surface has no Content-Security-Policy because its controls are inline event handlers. This change removes the handlers and then adds the policy. The facts below come from reading the tree at `f2ec082` and the vendored bytes, not from assumption.

**What renders HTML.** Four `Jinja2Templates` instances render from `src/control_panel/templates/`: `src/control_panel/routes.py` (dashboard, account, keys, oauth, usage, performance, health, search-analytics, vault, settings, reembed-confirm), `src/control_panel/users.py` (users, user edit), `src/auth/routes.py` (login, register — the latter doubling as first-admin bootstrap) and `src/oauth/routes.py` (`GET /authorize`, the consent page). A fifth, in `src/transfer/routes.py`, renders the two transfer pages under their own per-response nonce policy (`_page`, `default-src 'none'`). Some **error paths render HTML too**, through the same instances and without `response_class=HTMLResponse` on their decorator: a failed `POST /admin/auth/login` renders `login.html` with 401, and a failed bootstrap `POST /admin/register` renders `register.html` with 400. **Route metadata is therefore not an HTML inventory** (Codex round 1, finding 4).

**Framework HTML.** `src/main.py` constructs `FastAPI()` with its default documentation routes, so `/docs`, `/redoc` and `/docs/oauth2-redirect` also answer `text/html` — with un-nonced inline scripts and, for the first two, scripts from `cdn.jsdelivr.net`. They are Starlette `Route`s, not `APIRoute`s. No proxy router in `docker-compose.yml` matches them, so they are reachable only from inside the container network, and nothing in the repo (README, DEPLOYMENT, tests) references them.

**The three roots.** `base.html` (panel), `auth_base.html` (login, register) and `authorize.html` (consent, standalone) each include `_theme.html` — one `<style>` block of tokens plus the inline pre-paint theme bootstrap `<script>` — and `_theme_toggle.html`. Page templates add their own `<style>` (`vault.html`) and `<script>` blocks (`dashboard.html`, `keys.html`, `usage.html`, `user_edit.html`, and `base.html`'s footer script). `usage.html` interpolates `{{ chart_data | tojson }}` into its script; `tojson` escapes `<`, `>`, `&` and `'`, so that is safe inside a nonced script as it is today.

**Inline handlers (31 attributes, nine files).** `keys.html` 8, `vault.html` 8 (hover colour via `onmouseover`/`onmouseout`), `settings.html` 4, `oauth.html` 3 (two confirms, one `onchange="this.form.submit()"`), `base.html` 2 (mobile sidebar), `users.html` 2, `user_edit.html` 2, `dashboard.html` 1 (`onsubmit` → `triggerReindex`), `_theme_toggle.html` 1. Eight are `return confirm(…)`. No `javascript:` URL exists anywhere.

**Inline `style=` attributes: about 430** across 18 templates, including the SVG gem marks, which put colours in `style=""` on purpose (`control-panel.md`: SVG2 presentation attributes do not dependably substitute `var()`).

**External and non-script resources.** Google Fonts: a stylesheet from `https://fonts.googleapis.com`, font files from `https://fonts.gstatic.com`, in all three roots. `data:image/svg+xml` URLs in CSS (noise background in all three roots, `--select-arrow` in `_theme.html`). One `fetch('/admin/settings/reindex')` (`dashboard.html`). No `<img>`, `<iframe>`, `<object>`, `<embed>`, `<base>`, `<meta http-equiv>`, `EventSource` or `WebSocket`. Every form's `action` is same-origin, or absent.

**The vendored libraries.**
- `htmx-2.0.4.min.js` is loaded by `base.html` on every panel page and **used by none** — `grep -r 'hx-' src/control_panel/templates` is empty, and no Python route reads an `HX-*` header. Its defaults include `includeIndicatorStyles: true` (on load it inserts a `<style>` element into `<head>`, nonced only if `htmx.config.inlineStyleNonce` is set) and `allowEval: true` (`hx-on*`, event-filter expressions and `js:`-prefixed `hx-vals` go through `Function(...)`).
- `chart-4.4.7.umd.min.js`: no `eval`, no `Function(`, no `createElement("style")`, no `setAttribute("style", …)` in the vendored bytes. It sizes the canvas through the CSSOM (`element.style.x = …`), which CSP does not govern. Chart.js 4 needs neither `'unsafe-eval'` nor an inline-style allowance.

**The consent redirect.** `authorize.html` posts `action="/authorize"`. `authorize_post` re-validates the submitted `redirect_uri` against the client's registered list and answers **302** to it for both approve (`code=…`) and deny (`error=access_denied`). Registration only accepts `https` URIs with a non-empty host, normalised to an ASCII A-label (`_normalized_redirect_uri`). Chromium enforces `form-action` on every redirect in a form-submission navigation, so the 302 destination and any further hop must match `form-action` of the page that submitted the form.

**The login redirect.** `login.html` posts to `/admin/auth/login`, which 302s to `_safe_next(next)` — same-origin paths only, including the round trip back to `/authorize?...`. `register.html` and the logout form redirect same-origin.

**Deployment.** Every HTML route sits behind the SSO forward-auth chain at the proxy (`/admin/*` and `/authorize` share `obsidian-mcp-panel-rtr` with `chain-oauth@file`), so nothing HTML is reachable from outside without an SSO session.

## Goals / Non-Goals

**Goals**
- An **enforced** nonce-based policy on every HTML response the app renders, with no `'unsafe-inline'` or `'unsafe-eval'` for scripts.
- Zero inline event handlers and zero `javascript:` URLs in templates, held by a static test.
- Every existing panel control, the theme toggle on all three roots, the usage chart and the full OAuth consent flow keep working under the enforced policy.
- A rollback that needs no rebuild.

**Non-goals**
- Removing inline `style=` attributes. D3 keeps them under `style-src-attr 'unsafe-inline'`; converting ~430 attributes to classes is a separate, mechanical change (follow-up, see Open Questions).
- Self-hosting Google Fonts. It ships no executable code; `font-src`/`style-src-elem` allow-list exactly the two origins. Recorded as a separate call in `control-panel.md` already.
- A CSP violation report endpoint (`report-uri`/`report-to`). It would be a new unauthenticated write surface; report-only mode is for an operator with devtools open.
- Trusted Types, `'strict-dynamic'`, `Cross-Origin-*-Policy` headers, SRI on same-origin scripts (already argued against in `base.html`).
- Changing the transfer pages' policy, which is stricter than this one and stays byte-for-byte.
- A CSP on the proxy's own SSO sign-in pages, which the app does not serve.

## Decisions

### D1 — Behaviour moves to delegation on `data-*`; page scripts stay inline and nonced

One static file, `src/control_panel/static/panel.js`, loaded by `base.html` with the nonce, installs document-level delegated listeners keyed on `data-*` attributes:

| Today | After |
| --- | --- |
| `onclick="return confirm('…')"` (8) | `type="button" data-confirm="…"` — D2 |
| modal open/close (`keys`, `users`, `settings`) | `data-modal-open="id"`, `data-modal-close="id"`, `data-modal-backdrop` (close when the click target is the scrim itself) |
| `onchange="this.form.submit()"` (oauth scope select) | `data-autosubmit` → `form.requestSubmit()` |
| clipboard copy (new key) | `data-copy-from="new-key-val"` |
| `omcpEditLimit(id, current)` | `data-limit-edit` with `data-key-id` / `data-limit` |
| mobile sidebar open/close | `data-sidebar-open`, `data-sidebar-close` |
| `onsubmit` reindex (dashboard) | `data-async-reindex` on the form; the existing `triggerReindex` body moves into `panel.js` |
| `vault.html` hover colour | CSS `:hover` rules in `vault.html`'s nonced `<style>` |

The existing page `<script>` blocks (count-up, activity stagger, chart build, vault-path toggle, timestamp localisation) stay inline with `nonce="{{ csp_nonce }}"`. Moving them to static files buys nothing under a nonce policy and would turn `usage.html`'s `tojson` interpolation into a data-attribute round trip.

`data-*` values are read with `dataset`/`getAttribute` and used as strings or element ids — never evaluated, never assigned to `innerHTML`. That removes the `confirm()` quoting defect class `tests/test_issue_130_hardening_minors.py` guards (an apostrophe in an interpolated name broke the JS string, the handler threw, and a throwing `onclick` submits unconfirmed): an attribute value is not parsed as JavaScript.

**The theme toggle is the exception.** `_theme_toggle.html` is rendered on the auth and consent roots, which do not load `panel.js`. Its listener goes into the existing inline bootstrap in `_theme.html` (a delegated `click` on `[data-theme-toggle]`, registered on `document` from `<head>`, which works before the body exists). `window.__themeToggle` may remain as the function the listener calls.

### D2 — Confirmations fail closed

A `data-confirm` control is a `type="button"`, not a submit button. The delegated handler calls `confirm(el.dataset.confirm)` and, on yes, `el.form.requestSubmit()` — `requestSubmit` rather than `submit` so the form's own validation and `submit` event still run. Without script (blocked, failed to load, or disabled) the button does nothing and the form is not submitted. Today the opposite holds: a missing or throwing `onclick` lets the destructive POST through unconfirmed.

Implicit submission is closed too: none of the eight forms has a text field (only the hidden `csrf_token`), and the spec forbids a confirm-guarded form from carrying any other submit button, so Enter cannot submit around the prompt. `disabled` on the self-delete buttons (`user_edit.html`, `is_self`) is kept as is.

**Scope: exactly the eight existing `confirm()` controls** (Codex round 1, finding 6). Two other confirmation mechanisms keep what they have:
- The settings page's "Reset embeddings" flow is a custom modal, not a `confirm()`; its final "Yes, reset" is a real submit button inside the modal and stays one — the modal is the confirmation, and opening it requires script (D1), so without script the submit is never reachable.
- `reembed_confirm.html` is a dedicated server-rendered confirmation page; its "Yes, Re-embed All" is a native submit button and stays one. The page itself is the confirmation step and needs no script.

### D3 — Style elements are nonced; style attributes stay, under `style-src-attr 'unsafe-inline'`

Three candidates:

1. **Nonce `style-src` and move every `style=` to classes.** The strongest policy, and ~430 edits across 18 templates plus the SVG marks, whose `style=""` exists because presentation attributes do not substitute `var()` reliably. A large visual-regression surface whose own review would dwarf this change. Rejected for now, filed as a follow-up.
2. **`style-src 'self' 'unsafe-inline' …` for everything.** Keeps attributes working but also admits injected `<style>` *elements*, which are the real CSS-exfiltration primitive (attribute-selector scraping of the CSRF token's `value`, font-based text probes). Rejected.
3. **Split: `style-src-elem 'nonce-N' https://fonts.googleapis.com` and `style-src-attr 'unsafe-inline'`.** Chosen.

What an injected `style=` attribute can still do: restyle the element it sits on (overlays, hiding, UI redress within the page). What it cannot do: select other elements, so no attribute-selector scraping; and any `url()` it names is fetched under `img-src 'self' data:` / `font-src`, so it cannot beacon off-origin. Reaching even that needs an HTML-injection primitive, which the nonce policy already disarms for scripts. That is the trade the Codex note asked to be made explicitly.

**Legacy fallback.** A browser without CSP3 `style-src-elem`/`-attr` support falls back to `style-src`. Its value is deliberately `https://fonts.googleapis.com 'unsafe-inline'` — **no nonce**, because the presence of a nonce makes a CSP2 browser ignore `'unsafe-inline'` and would block every style attribute, breaking the panel. In a CSP3 browser the `-elem`/`-attr` directives take precedence and the fallback value is never consulted. Current Chromium, Firefox and Safari all implement the split directives; an older browser gets option 2's weaker style policy and the same script policy. Accepted limitation 2.

CSSOM writes (`el.style.display = …` in page scripts, Chart.js canvas sizing) are not governed by CSP and need no allowance.

### D4 — The directive set, enumerated

```
default-src 'self'
script-src 'nonce-N'
style-src https://fonts.googleapis.com 'unsafe-inline'
style-src-elem 'nonce-N' https://fonts.googleapis.com
style-src-attr 'unsafe-inline'
img-src 'self' data:
font-src https://fonts.gstatic.com
connect-src 'self'
object-src 'none'
base-uri 'none'
frame-ancestors 'none'
form-action 'self'            (consent page: 'self' https:, D5)
```

Each source is justified by something in the templates: `script-src` — every script, inline or `src=`, carries the nonce, so no host-source (not even `'self'`) is listed and an injected `<script src="/admin/static/…">` is still refused; `style-src-elem` — nonced `<style>` blocks plus the Google Fonts stylesheet `<link>`; `img-src data:` — the SVG noise and select-arrow backgrounds, `'self'` for the favicon request; `font-src` — Google's font files; `connect-src 'self'` — the reindex `fetch`. `default-src 'self'` covers what is not enumerated (manifest, media, frames, workers); none is used. `frame-ancestors 'none'` duplicates `X-Frame-Options: DENY`, which stays for older browsers.

No `'unsafe-eval'`: Chart.js does not need it (Context) and htmx is removed (D6). No `'strict-dynamic'`: nothing loads scripts dynamically.

### D5 — The consent page's `form-action` is `'self' https:` (owner decision, Codex round 1)

`form-action` is the policy of the **page that submits the form**, so the question is decided on the `GET /authorize` response. Its form posts to `/authorize`, which answers 302 to the client's registered `redirect_uri` for approve and deny alike, and Chromium checks `form-action` on **every hop** of a form-submission navigation.

**Decision: the consent page's `form-action` is `'self' https:`, unconditionally.** Every other page keeps `'self'`. No value from the request reaches the header; the directive is a constant selected by which template rendered the response.

The first draft emitted the exact origin of the validated `redirect_uri`. Codex round 1 showed two ways that breaks a working connector, and the owner ruled that a broken approve/deny on a live connector is the most expensive failure this change can cause:
- **Multi-hop callbacks.** A callback on `api.client.example` that 302s to `app.client.example` is blocked on the second hop, after the code has already been delivered.
- **Non-canonical hosts.** Registration keeps an already-ASCII host as written, so `https://127.1/cb` is stored and would be emitted as the source `https://127.1`, while the browser navigates to its canonical `https://127.0.0.1/cb`. Chromium compares the source spelling against the canonical URL host, so even a single hop can fail. Matching browser canonicalisation is a parser-fidelity problem this change does not need to take on.

What `'self' https:` still refuses on the consent page: a form (or `formaction`) posting to `http:`, `javascript:`, `data:` or any other non-HTTPS scheme. What it gives up: an injected form posting the hidden fields to an arbitrary HTTPS origin. Reaching that needs an HTML-injection primitive on a page whose attacker-influenced strings (`client_name`, the redirect host) are autoescaped, and injected script is refused outright by the nonce-only `script-src`. Accepted limitation 3.

Unchanged and relied on: the existing registration rule that every `redirect_uri` is `https` with a non-empty host (`_normalized_redirect_uri`), and `authorize_post`'s exact-match re-validation of the submitted `redirect_uri` against the registered list. Those, not the CSP, decide where a code can be delivered; `https:` in `form-action` never admits a destination registration would refuse.

Alternatives rejected:
- **Flat `'self'`** breaks every OAuth approve and deny in Chromium.
- **Exact redirect origin** — the first draft; rejected for the two reasons above.
- **Omit `form-action` on the consent page** would also admit `http:` and `javascript:` targets, for no gain over `https:`.
- **Answer the POST with a page that navigates by script or meta refresh** changes the OAuth protocol surface to satisfy a header.

`POST /authorize` returns a redirect, not HTML, and carries no policy — none is needed on a 302.

### D6 — htmx is removed, not configured

Two options once the policy exists:
- **Keep it and configure it**: `<meta name="htmx-config" content='{"includeIndicatorStyles":false,"allowEval":false}'>` stops the un-nonced `<style>` insertion and the `Function` paths. What remains is the attribute-driven request machinery: an injected `<div hx-post="/admin/keys/create" hx-include="[name=csrf_token]" hx-trigger="load">` needs no script and no eval, and makes an authenticated, CSRF-valid request. That is a script gadget that bypasses the nonce policy by construction.
- **Remove it.** Chosen. Nothing uses it; its only effect on this surface is to be the bypass. The file, its `<script>` tag, its provenance line in `base.html`'s comment and the `README.md` mention go; `tests/test_issue_130_hardening_minors.py` drops it from the vendored-asset parameters. A future change that wants htmx reintroduces it with that configuration and re-argues the gadget.

A static test forbids `hx-*` attributes in templates and any `<script src>` naming htmx, so a reintroduction is a deliberate act.

### D7 — Where the nonce and the header come from, and which responses get it

**Scope is decided by a marker from the four panel template instances, not by path or by content type alone** (Codex round 1, finding 3). A new `src/services/panel_csp.py` owns:

- `nonce_for(request) -> str`: lazily creates `secrets.token_urlsafe(16)` (the transfer pages' size) on first call and stores it in `request.state`, so every call during one request returns the same value and a new request gets a new one.
- `template_context(request) -> {"csp_nonce": …}`: a Starlette `Jinja2Templates` context processor, registered on each of the four panel/auth/consent `Jinja2Templates` instances and **not** on the transfer instance. Besides returning the nonce it marks the request as a **panel surface** on `request.state`. Templates write `nonce="{{ csp_nonce }}"`. A context processor rather than a Jinja global because several tests render templates through their own `Environment` with `ChainableUndefined`, where a missing *variable* renders empty but a missing *callable* raises.
- `mark_consent(request)`: called by `authorize_get` immediately before it renders `authorize.html`, so that response gets the D5 `form-action`.
- `build_policy(nonce, consent: bool) -> str`: the D4 string; `form-action 'self' https:` when `consent`, `form-action 'self'` otherwise. No request-derived value is ever interpolated except the server-generated nonce.

`add_security_headers` in `src/main.py` gains one step after `call_next`: when the mode is not `off`, the request carries the panel-surface marker, and the response is `text/html`, it writes the policy — as `Content-Security-Policy` under `enforce`, as `Content-Security-Policy-Report-Only` under `report-only` — **unless the response already carries an enforcing `Content-Security-Policy` header**, which is left byte-for-byte alone. A pre-existing `Content-Security-Policy-Report-Only` header does **not** suppress the enforcing panel policy (Codex round 1, finding 5); under `report-only` mode the panel value replaces any report-only value (no route sets one today). Using `nonce_for` in the middleware guarantees a policy even for a marked response that rendered no nonce (it then admits no inline script at all). `request.state` is shared between middleware and endpoint because both wrap the same ASGI `scope["state"]`; the header test proves header nonce equals every body nonce.

**Why a marker and not a path prefix** (`/admin`, `/authorize`): the policy is only correct for markup written to carry its nonce, and "rendered by one of the four instances" is exactly that set. It covers the HTML error renders (login 401, register 400) and any future panel route with no inventory to maintain, and it excludes by construction everything that is not panel markup — the transfer pages (own instance, own policy) and FastAPI's `/docs`, `/redoc` and `/docs/oauth2-redirect`, which would break under a nonce-only policy. A path prefix would need its own exception list and would still miss a panel template rendered from a new prefix. A static test asserts that every `Jinja2Templates(` in `src/` other than the transfer one registers the processor, so a fifth instance cannot silently escape.

**The FastAPI documentation pages stay enabled and outside the policy.** Nothing in the repo relies on them, and no proxy router exposes them, so they are reachable only from inside the container network; disabling them is an unrelated hardening call and is not bundled here. A test asserts `/docs` receives no panel policy, which is what keeps them working.

### D8 — `PANEL_CSP` kill switch

`panel_csp: Literal["enforce", "report-only", "off"] = "enforce"` in `src/config.py`, next to the other boot-validated `Literal` settings (`mcp_concurrency_mode`, `log_format`). An unknown value refuses startup. The effective mode is logged once at INFO during lifespan startup; any value other than `enforce` also logs a WARNING naming the setting, so a forgotten rollback is visible in the logs.

- `enforce` — the header is `Content-Security-Policy`.
- `report-only` — the same value as `Content-Security-Policy-Report-Only`. Browsers log violations to the console and block nothing. (`frame-ancestors` in a report-only header only reports; `X-Frame-Options: DENY` still refuses framing.)
- `off` — no policy header. Templates still render nonces and still carry no inline handlers; behaviour is identical.

Rollback: set the value in the deploy directory's `.env`, recreate the container. The compose file already passes `.env` through `env_file`, so no compose change is needed. `.env.example` documents it.

### D9 — Tests

- `tests/test_panel_csp_headers.py`, all through the real app middleware stack with the existing fake-session harness patterns. **Route metadata is a starting list, not an exhaustive inventory** — it misses HTML rendered by decorators without `response_class`, and error renders — so the test combines three sources: (1) every `APIRoute` whose `response_class` is `HTMLResponse`, with an inventory assertion that fails if one is not in the parametrised set; (2) `GET /authorize` for a registered client; (3) **explicit method/status cases**: `POST /admin/auth/login` with a valid CSRF token and wrong credentials → 401 rendering `login.html`; bootstrap `POST /admin/register` with invalid fields → 400 rendering `register.html`. For each: the enforcing header is present, it contains every D4 directive, `script-src` is exactly one nonce source, the nonce is at least 22 URL-safe characters, the body contains at least one nonced `<script>` and one nonced `<style>`, and every `nonce="…"` in the body equals the header's; two consecutive requests carry different nonces. Plus: the consent page's `form-action` is exactly `'self' https:` and every other page's is exactly `'self'`; `report-only` moves the value and `off` removes it; `GET` **and `HEAD`** of both transfer pages keep their own policy unchanged in all three modes; a marked HTML response that already carries only a `Content-Security-Policy-Report-Only` header still receives the enforcing panel policy (a test-only route or a patched response); `GET /docs` receives no panel policy; a JSON response (an `/authorize` validation error) carries no policy.
- A static assertion, in the same file, that every `Jinja2Templates(` construction under `src/` except `src/transfer/routes.py` passes the context processor.
- `tests/test_panel_csp_templates.py` (static, no app): over every file in `src/control_panel/templates/`, after stripping `{# … #}` and `<!-- … -->` comments — no attribute matching `\son[a-z]+\s*=`; no `javascript:` in any `href`/`src`/`action`/`formaction`; no `hx-` attribute; every `<script` and `<style` opening tag carries `nonce="{{ csp_nonce }}"` (transfer templates: `nonce="{{ nonce }}"`); every `<script src=` is under `/admin/static/`; no `<link rel="stylesheet">` outside `https://fonts.googleapis.com`; every `data-confirm` element is `type="button"` and its form contains no `type="submit"` control; `vault.html` hover is expressed in CSS.
- `tests/test_panel_csp_config.py`: default `enforce`; each accepted value; an unknown value raises at settings construction; the startup log lines.
- `panel.js` behaviour has no JS test runner in this repo; it is covered by the live browser pass (tasks §6) and by the static test's structural assertions. Adding a JS test harness is out of scope.

### D10 — Live verification is partly manual, by construction

Every HTML route is behind the SSO forward-auth chain. `curl` from outside sees the SSO 302, never the app's response, and driving an SSO login from a script would mean handling the owner's SSO credentials. What can be automated and what cannot:

- **Scriptable, on the host:** a request from inside the container network straight to the app (`docker exec` into the container, a Python one-liner against `http://localhost:8000/admin/auth/login` with the configured hostname as `Host`) — the login page renders without a panel session in multi-user mode — asserting the header, the mode, and that the body nonces match. Also `GET /transfer/upload` to confirm the transfer policy is unchanged. This proves the app emits the policy on the deployed build.
- **Not scriptable:** that the header survives the proxy chain on an authenticated request, and that every control works in a real browser under it. Traefik does not strip response headers by default, but that is exactly the kind of "should" the in-container probe cannot see.
- **The pragmatic check:** one browser pass by Max, by hand (owner decision), with devtools open, first under `report-only` and then again briefly under `enforce` (Migration Plan), over a fixed list: dashboard (count-up, reindex button), keys (new-key modal, copy, edit-limit modal, revoke confirm — cancel and accept on a throwaway key), OAuth page (scope select auto-submit on a throwaway grant, revoke confirm cancel), usage (chart renders, theme toggle recolours it), settings (reset modal open and cancel), users and user edit (create modal; deactivate confirm cancel), vault (hover), mobile width (sidebar open/close), login and consent pages (theme toggle), and one full OAuth connect → approve with a real client in use, plus one deny. Pass criterion: the response headers show the policy, and the console and the Issues panel show **zero** CSP violations (under `report-only`, zero *reported* violations). Only that result authorises the flip to `enforce`.

## Spec review history

| Round | Finding | Severity | Resolution |
| --- | --- | --- | --- |
| 1 | Enforcement precedes the only connector-compatibility check; multi-hop callbacks break under an exact-origin `form-action` | MAJOR | Owner decision: consent `form-action 'self' https:` (D5); mandatory report-only first deploy with a real approve and deny before enforcing (Migration Plan, tasks §6) |
| 1 | Exact-origin derivation diverges from browser canonicalisation (`https://127.1`) | MAJOR | Moot: the derivation is removed (D5) |
| 1 | `/docs`, `/redoc`, `/docs/oauth2-redirect` would receive an incompatible policy and escape the inventory | MAJOR | Scope by a panel-surface marker from the four template instances (D7); framework pages excluded and tested; accepted limitation 4 |
| 1 | HTML error renders (login 401, register 400) and transfer `HEAD` missing from coverage; route metadata is not an inventory | MAJOR | Explicit method/status cases through the real stack; stated that metadata is not exhaustive (D9, task 2.7) |
| 1 | Task 2.5 let an existing Report-Only header suppress the enforcing policy | MINOR | Aligned: only an enforcing header suppresses; tested (D7, tasks 2.5, 2.7) |
| 1 | `data-confirm` requirement broader than the plan (reset modal, reembed page) | MINOR | Requirement scoped to the eight existing `confirm()` controls; the two other mechanisms kept and named (D2, spec) |

## Implementation review history

(Filled in by tasks 5.x.)

## Risks / Trade-offs

- [A panel control silently stops working under the policy] → the static scan makes an inline handler a test failure; the browser pass exercises every control; `PANEL_CSP=report-only` is a one-line rollback that keeps nonces and delegation in place.
- [The OAuth consent redirect is blocked] → D5 admits any HTTPS target on the consent page, so neither multi-hop callbacks nor non-canonical hosts can break it; the rollout (Migration Plan) runs report-only through a real approve and deny before enforcing.
- [The policy lands on HTML it was not written for and breaks it] → D7's marker scopes it to the four template instances; `/docs` and the transfer pages are asserted untouched.
- [A delegated confirm regresses to fail-open] → D2 makes the button non-submitting; the static test asserts `type="button"` and no sibling submit.
- [The nonce reaches an attacker] → it is per-response, 128 bits, rendered only in attributes of elements this server wrote; browsers hide `nonce` from CSS selectors and from `getAttribute` on script elements. A page that reflects attacker markup *before* a nonced tag could still dangle it — autoescape stands in front of that, as today.
- [A cached panel page replays a stale nonce] → the header is cached with the body, so they stay consistent; nothing new is exposed.
- [Removing htmx breaks something] → nothing references it (grep of templates and Python); the static test forbids `hx-*` so a silent dependency cannot exist.
- [Middleware ordering] → `add_security_headers` already wraps every response; adding the policy there reuses the one place response headers are set. The header test goes through the real stack, not a unit call to `build_policy`.

## Migration Plan

No schema change, no data change. **Rollout is two steps (owner decision):**

1. The first production deploy sets `PANEL_CSP=report-only` in the deploy directory's `.env` before `make deploy`. Nothing is blocked; violations appear in the browser console.
2. Max runs the D10 browser pass by hand, with devtools, over the fixed page list, including **one real connector approve and one deny**. When it shows zero violations, `PANEL_CSP=enforce` goes into the `.env` and the container is recreated (no rebuild). A short second pass confirms the enforcing header on a panel page and one more approve.

The code default stays `enforce`, so a fresh deployment is protected without operator action; report-only is this deployment's rollout step, not the product default. Rollback at any time is `PANEL_CSP=report-only` (or `off`) plus a recreate.

## Accepted limitations

1. **Inline style attributes remain allowed** (`style-src-attr 'unsafe-inline'`). Under an HTML-injection bug an attacker can restyle the injected element itself — overlay, hide, UI redress — but cannot run script, scrape other elements through selectors, or beacon off-origin. D3. Converting to classes is a follow-up.
2. **A browser without CSP3 `style-src-elem`/`-attr` support gets `style-src 'unsafe-inline'`** for style elements too. Script policy is unaffected.
3. **The consent page's `form-action` admits any HTTPS origin** (`'self' https:`), owner-decided (D5). An HTML-injection bug on the consent page could post the form's hidden fields to an attacker's HTTPS origin; `http:`, `javascript:` and other schemes stay refused, script stays refused, and where a code can be delivered is still decided by registration (HTTPS-only) and exact-match re-validation, not by CSP.
4. **The FastAPI documentation pages (`/docs`, `/redoc`, `/docs/oauth2-redirect`) carry no CSP.** They are framework HTML, not proxy-routed, and outside the panel surface by D7's marker. Disabling them is a separate call.
5. **A panel POST made after the SSO session has expired** is answered by the forward-auth chain with a redirect to the identity provider — a different origin — which `form-action 'self'` blocks in Chromium. The user sees a blocked-navigation page instead of the SSO login; a reload (a GET) proceeds to SSO. The POST was never going to be replayed after SSO anyway, so nothing is lost that is not lost today.
6. **`report-only` mode reports only to the browser console.** No collection endpoint (non-goal).
7. **`panel.js` has no automated behavioural test.** No JS runner exists here; the static structure test and the live browser pass cover it.
8. **CSP is not a CSRF control.** `form-action 'self'` still permits an injected form posting to the panel's own endpoints; the CSRF token (unreadable without script) remains the control for that.

## Open Questions

- **Follow-up to file:** move inline `style=` attributes to classes and drop `style-src-attr 'unsafe-inline'`.
- **Follow-up to consider (not filed by default):** disable the unused FastAPI documentation routes.

Resolved by the owner after Codex round 1: the consent `form-action` (`'self' https:`, D5), the first-deploy mode (report-only, then enforce — Migration Plan), and who runs the browser pass (Max, by hand).
