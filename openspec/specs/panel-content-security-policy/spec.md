# panel-content-security-policy Specification

## Purpose
Covers the Content-Security-Policy on the control panel, the login pages and the OAuth consent page, the surface that mints keys and grants scope. The policy uses a per-response nonce, so script runs only from nonced elements and markup carries no inline handlers or `javascript:` URLs. The consent page's `form-action` admits the HTTPS callbacks that connectors use. `PANEL_CSP` is the rollback switch. Introduced by `panel-csp` (#195). Rationale is in `docs/architecture/control-panel.md`.
## Requirements
### Requirement: Every HTML response rendered from the panel, auth and consent templates SHALL carry a nonce-based Content-Security-Policy
Every `text/html` response whose body was rendered by one of the four panel, auth and consent template instances (those in `src/control_panel/routes.py`, `src/control_panel/users.py`, `src/auth/routes.py` and `src/oauth/routes.py`) SHALL carry the panel policy built from a per-response nonce, whatever the request method or response status. That set — the **panel surface** — SHALL be identified by a marker the templates' shared context processor sets during rendering, not by path or by route metadata, and every template instance in `src/` other than the transfer one SHALL register that processor. HTML not rendered by those instances — the transfer pages and the framework's `/docs`, `/redoc` and `/docs/oauth2-redirect` — SHALL NOT receive the panel policy. A response that already carries an enforcing `Content-Security-Policy` header SHALL keep it byte-for-byte; a pre-existing `Content-Security-Policy-Report-Only` header SHALL NOT suppress the panel policy. Responses of any other content type SHALL NOT gain a policy from this requirement.

The nonce SHALL be generated from a cryptographically secure source with at least 128 bits of entropy, SHALL be fresh for every response, and SHALL be the same value in the header and in every `nonce` attribute of that response's body.

#### Scenario: A panel page carries the policy
- **WHEN** an authenticated administrator requests any panel page, such as `GET /admin/keys`
- **THEN** the response SHALL carry a `Content-Security-Policy` header containing `script-src 'nonce-<N>'`
- **AND** every `nonce="…"` attribute in the body SHALL equal `<N>`

#### Scenario: Auth and consent pages carry the policy
- **WHEN** a visitor requests `GET /admin/auth/login`, `GET /admin/register`, or `GET /authorize` for a registered client with a valid request
- **THEN** each HTML response SHALL carry the policy with a nonce equal to every nonce in its body

#### Scenario: HTML error renders carry the policy
- **WHEN** `POST /admin/auth/login` is submitted with a valid CSRF token and wrong credentials, answering 401 with the login page, or a bootstrap `POST /admin/register` with invalid fields answers 400 with the register page
- **THEN** each response SHALL carry the policy, and its body SHALL contain nonced `<script>` and `<style>` elements whose nonce equals the header's

#### Scenario: The nonce is fresh per response
- **WHEN** the same page is requested twice
- **THEN** the two responses SHALL carry different nonces

#### Scenario: The transfer pages keep their own policy
- **WHEN** `GET` or `HEAD` is issued for `/transfer/upload` or `/transfer/download`
- **THEN** its `Content-Security-Policy` header SHALL be exactly the one the transfer route sets, with `default-src 'none'`, unchanged by this requirement

#### Scenario: Framework documentation pages are outside the surface
- **WHEN** `GET /docs` is requested from inside the container network
- **THEN** the response SHALL NOT carry the panel policy

#### Scenario: A report-only header does not suppress enforcement
- **WHEN** a panel-surface HTML response already carries `Content-Security-Policy-Report-Only` and no `Content-Security-Policy`, with `PANEL_CSP=enforce`
- **THEN** the response SHALL receive the enforcing panel policy

#### Scenario: A JSON response gains no policy
- **WHEN** a request is answered with `application/json`, such as an OAuth error from `GET /authorize`
- **THEN** the response SHALL NOT gain a `Content-Security-Policy` header from this requirement

#### Scenario: Coverage is not taken from route metadata alone
- **WHEN** the test suite runs
- **THEN** it SHALL exercise every route declared with an HTML response class, `GET /authorize`, and the explicit HTML error renders above, and SHALL fail if a route declared with an HTML response class is not exercised

### Requirement: The panel policy SHALL consist of the enumerated directive set and SHALL NOT permit inline or evaluated script
The policy SHALL contain exactly these directives, with `<N>` the response's nonce: `default-src 'self'`; `script-src 'nonce-<N>'`; `style-src 'nonce-<N>' https://fonts.googleapis.com`; `style-src-elem 'nonce-<N>' https://fonts.googleapis.com`; `style-src-attr 'none'`; `img-src 'self' data:`; `font-src https://fonts.gstatic.com`; `connect-src 'self'`; `object-src 'none'`; `base-uri 'none'`; `frame-ancestors 'none'`; `form-action 'self'`, which the consent page alone widens to `'self' https:`. `script-src` SHALL NOT contain a scheme source or a host source. No directive of the policy SHALL contain `'unsafe-inline'`, `'unsafe-eval'` or `'unsafe-hashes'`.

`style-src-attr` is stated explicitly as `'none'` rather than left to fall back to `style-src`, so that the ban on inline style attributes does not depend on the fallback directive's contents. `style-src` carries the nonce, so a browser that implements only the CSP2 `style-src` directive also admits nonced style elements alone and refuses every inline style attribute.

#### Scenario: The script directive admits only the nonce
- **WHEN** the policy header of any panel page is parsed
- **THEN** `script-src` SHALL consist of exactly one source, `'nonce-<N>'`

#### Scenario: Style elements need the nonce and style attributes are refused
- **WHEN** the policy header is parsed
- **THEN** `style-src-elem` SHALL consist of exactly `'nonce-<N>'` and `https://fonts.googleapis.com`
- **AND** `style-src-attr` SHALL consist of exactly `'none'`

#### Scenario: The legacy style fallback carries the nonce
- **WHEN** the policy header is parsed
- **THEN** `style-src` SHALL consist of exactly `'nonce-<N>'` and `https://fonts.googleapis.com`, with `<N>` equal to the nonce in `script-src`

#### Scenario: No directive admits unsafe-inline
- **WHEN** the policy header of any panel, auth or consent page, including the login 401 and bootstrap register 400 error renders, is parsed
- **THEN** no directive SHALL contain `'unsafe-inline'`, `'unsafe-eval'` or `'unsafe-hashes'`

#### Scenario: The fixed directives are present
- **WHEN** the policy header is parsed
- **THEN** it SHALL contain `object-src 'none'`, `base-uri 'none'`, `frame-ancestors 'none'`, `img-src 'self' data:`, `font-src https://fonts.gstatic.com` and `connect-src 'self'`

#### Scenario: An injected style attribute is refused in the browser
- **WHEN** an element with a `style` attribute is inserted into a panel page under the enforced policy
- **THEN** the browser SHALL NOT apply the attribute's declarations and SHALL report a violation of `style-src-attr`

#### Scenario: CSSOM writes still apply
- **WHEN** a panel script sets a property through `element.style` under the enforced policy
- **THEN** the property SHALL apply and the browser SHALL report no violation

### Requirement: The consent page's form-action SHALL be 'self' https: and every other page's SHALL be 'self'
The `GET /authorize` response that renders the consent form SHALL carry `form-action 'self' https:`, and every other panel-surface response SHALL carry `form-action 'self'`. No request-derived value SHALL be interpolated into the policy; the only variable part of the header is the server-generated nonce. Where an authorization code may be delivered SHALL continue to be decided by the existing rules — a registered `redirect_uri` must be `https` with a non-empty host, and `POST /authorize` re-validates the submitted `redirect_uri` by exact match against the client's registered list — which this change does not alter.

The consent form posts to `/authorize`, which answers with a redirect to the registered redirect URI for both approve and deny, and browsers apply `form-action` to every redirect hop of a form-submission navigation; an HTTPS scheme source keeps multi-hop callbacks and non-canonically spelled hosts working while still refusing non-HTTPS form targets.

#### Scenario: Approve and deny reach the registered redirect URI
- **WHEN** a user approves, or denies, on the consent page for a client whose registered callback redirects on to another HTTPS origin
- **THEN** the consent page's policy SHALL contain `form-action 'self' https:`
- **AND** the browser SHALL follow every hop without a policy violation

#### Scenario: Non-HTTPS form targets stay refused on the consent page
- **WHEN** the consent page's policy is parsed
- **THEN** `form-action` SHALL consist of exactly `'self'` and `https:`

#### Scenario: Other pages keep form-action 'self'
- **WHEN** any panel, login or register page is rendered
- **THEN** its `form-action` SHALL be exactly `'self'`

### Requirement: The policy mode SHALL be selected by a boot-validated PANEL_CSP setting defaulting to enforce
The application SHALL read `PANEL_CSP` with accepted values `enforce`, `report-only` and `off`, defaulting to `enforce`, and SHALL refuse to start on any other value. Under `enforce` the policy SHALL be sent as `Content-Security-Policy`; under `report-only` the identical value SHALL be sent as `Content-Security-Policy-Report-Only` and no enforcing header SHALL be sent; under `off` neither header SHALL be sent by this mechanism. The effective mode SHALL be logged once at startup, and a mode other than `enforce` SHALL additionally be logged at WARNING. The setting SHALL NOT change the transfer pages' own policy, the rendering of nonces in templates, or any control's behaviour.

#### Scenario: Default enforces
- **WHEN** `PANEL_CSP` is unset
- **THEN** panel HTML responses SHALL carry `Content-Security-Policy`

#### Scenario: Report-only for rollback
- **WHEN** `PANEL_CSP=report-only`
- **THEN** panel HTML responses SHALL carry `Content-Security-Policy-Report-Only` with the same value and SHALL NOT carry `Content-Security-Policy`

#### Scenario: Off
- **WHEN** `PANEL_CSP=off`
- **THEN** panel HTML responses SHALL carry neither header
- **AND** `/transfer/upload` SHALL still carry its own `Content-Security-Policy`

#### Scenario: The first production deploy runs report-only
- **WHEN** this change is first deployed to production
- **THEN** the deployment SHALL set `PANEL_CSP=report-only`
- **AND** it SHALL be switched to `enforce` only after a browser pass over the panel pages, including one approve and one deny through a real connector, reports zero violations

#### Scenario: An invalid value refuses startup
- **WHEN** `PANEL_CSP=strict`
- **THEN** settings construction SHALL raise and the application SHALL NOT start

### Requirement: Templates SHALL contain no inline event handlers, no javascript URLs and no un-nonced script or style elements
No template under `src/control_panel/templates/` SHALL contain an HTML event-handler attribute (`on` followed by letters, then `=`), a `javascript:` URL in any `href`, `src`, `action` or `formaction` attribute, or an `hx-` attribute. Every `<script>` and `<style>` element SHALL carry the response nonce — `nonce="{{ csp_nonce }}"` in panel, auth and consent templates, `nonce="{{ nonce }}"` in the two transfer templates. Every `<script src>` SHALL reference a path under `/admin/static/`, and every stylesheet `<link>` SHALL reference `https://fonts.googleapis.com`. A static test SHALL scan every template, after removing Jinja and HTML comments, and fail on any violation.

#### Scenario: An inline handler fails the build
- **WHEN** a template gains `<button onclick="…">`
- **THEN** the static template test SHALL fail naming the file

#### Scenario: A script without the nonce fails the build
- **WHEN** a template gains `<script>` without `nonce="{{ csp_nonce }}"`
- **THEN** the static template test SHALL fail naming the file

#### Scenario: The theme toggle works without an inline handler
- **WHEN** a visitor activates the theme toggle on a panel page, the login page or the consent page under the enforced policy
- **THEN** the page SHALL switch theme in place with no policy violation, the listener being registered by the nonced pre-paint bootstrap

#### Scenario: Panel controls work under the enforced policy
- **WHEN** an administrator uses the create-key modal, the copy button, the edit-limit modal, the OAuth scope select, the settings reset modal, the mobile sidebar, the dashboard reindex button and the usage chart under the enforced policy
- **THEN** each SHALL behave as before this change and the browser SHALL report no policy violation

### Requirement: The eight existing confirm() controls MUST fail closed
Each of the eight controls that used an inline `confirm()` before this change — revoke key, delete key and delete revoked keys (`keys.html`), delete client and revoke grant (`oauth.html`), trigger reindex (`settings.html`), deactivate user and permanently delete user (`user_edit.html`) — SHALL be a non-submitting button (`type="button"`) carrying its confirmation text in a `data-confirm` attribute, and its form SHALL contain no other submit control. The form SHALL be submitted only by script, after the user accepts the confirmation, using `requestSubmit()`. If the script does not run, activating the control SHALL submit nothing. The confirmation text SHALL be read as an attribute string and SHALL NOT be evaluated as script. The settings page's reset-embeddings modal and the `reembed_confirm.html` confirmation page are separate mechanisms and SHALL keep their native submit buttons.

#### Scenario: Declining submits nothing
- **WHEN** an administrator activates "Revoke" on an API key and declines the confirmation
- **THEN** no request SHALL be sent

#### Scenario: Accepting submits the form once
- **WHEN** the administrator accepts the confirmation
- **THEN** the form SHALL be submitted once, with its CSRF token

#### Scenario: No script, no submission
- **WHEN** the panel script fails to load or is blocked and an administrator activates "Permanently delete" on a user
- **THEN** the form SHALL NOT be submitted

#### Scenario: A quote in the confirmation text cannot bypass it
- **WHEN** the rendered confirmation text contains an apostrophe
- **THEN** the confirmation SHALL still be shown and the form SHALL still require acceptance

#### Scenario: The other confirmation mechanisms are unchanged
- **WHEN** an administrator opens the reset-embeddings modal, or the re-embed confirmation page
- **THEN** its final button SHALL remain a native submit button, reachable only through the modal or the dedicated page as before

### Requirement: The panel SHALL NOT load a script library that turns markup attributes into requests or code
The panel, auth and consent templates SHALL NOT load htmx or any other library that issues requests or evaluates code from declarative HTML attributes, and the vendored htmx file SHALL be removed from the static directory. Chart.js MAY remain loaded because it evaluates no markup and requires no `'unsafe-eval'`.

#### Scenario: htmx is gone
- **WHEN** the templates and `src/control_panel/static/vendor/` are inspected
- **THEN** no template SHALL reference htmx and no htmx file SHALL be served under `/admin/static/`

#### Scenario: Chart.js still renders under the policy
- **WHEN** the usage page is loaded under the enforced policy
- **THEN** the chart SHALL render and SHALL recolour on theme change with no policy violation

### Requirement: Panel, auth and consent markup SHALL carry no inline style attributes
No template under `src/control_panel/templates/` SHALL contain a `style` attribute on any HTML or SVG element, including an attribute emitted only on one branch of a Jinja conditional. No HTML response rendered by the panel, auth or consent template instances SHALL contain an element with a `style` attribute. `src/control_panel/static/panel.js` and the inline scripts in those templates SHALL NOT create a `style` attribute, neither by a markup string containing `style=` nor by `setAttribute` with the name `style`. Presentation that changes at runtime SHALL be applied through CSSOM (`element.style`) or by toggling classes. SVG colours that use theme tokens SHALL be applied by stylesheet rules, not by presentation attributes that contain `var()`. A static test SHALL scan every template, `panel.js` and every template's inline scripts after removing Jinja and HTML comments, and fail on any violation. A test through the application's middleware stack SHALL fail when any HTML route's response body contains an element with a `style` attribute.

#### Scenario: A static style attribute fails the build
- **WHEN** a template gains `<div style="margin:0">`
- **THEN** the static template test SHALL fail naming the file and line

#### Scenario: A conditional style attribute fails the build
- **WHEN** a template gains `<tr {% if revoked %}style="opacity:0.5"{% endif %}>`
- **THEN** the static template test SHALL fail naming the file and line

#### Scenario: An SVG style attribute fails the build
- **WHEN** a template gains `<polygon style="fill:var(--gem-facet)"/>`
- **THEN** the static template test SHALL fail naming the file and line

#### Scenario: A script that builds a style attribute fails the build
- **WHEN** `panel.js` or a template's inline script gains `el.setAttribute('style', …)` or a string containing `style=` that is inserted as markup
- **THEN** the static test SHALL fail naming the file

#### Scenario: A CSSOM write is not a violation
- **WHEN** `panel.js` or a template's inline script contains `el.style.display = 'flex'`
- **THEN** the static test SHALL pass

#### Scenario: Rendered panel HTML carries no style attribute
- **WHEN** every HTML route of the panel, auth and consent surface, and the login 401 and bootstrap register 400 error renders, are requested through the application
- **THEN** no element in any response body SHALL carry a `style` attribute

#### Scenario: Script-driven initial states still toggle
- **WHEN** an administrator opens and closes the settings reset modal, selects and deselects the custom vault path on the user edit page, and loads the dashboard with a reindex progress bar and activity rows, all under the enforced policy
- **THEN** the modal SHALL show and hide, the custom path input SHALL show and hide, the progress bar SHALL grow to its value and the activity rows SHALL fade in, with no policy violation

#### Scenario: The gem marks keep their theme colours
- **WHEN** a panel page, the login page and the consent page render in the light theme and in the dark theme under the enforced policy
- **THEN** the gem mark's edges and facets SHALL take their colours from the theme tokens and SHALL NOT render in the default black

#### Scenario: The panel looks the same after the conversion
- **WHEN** each panel, auth and consent page is rendered from the same seeded data before and after the conversion, in both themes, at desktop and mobile widths
- **THEN** every element's bounding box, and the computed values of the compared style properties (including every longhand of every migrated declaration), SHALL be identical, except for entries on a recorded allow-list, each with its reason

#### Scenario: The look-the-same comparison can fail
- **WHEN** the comparison is run against a copy of the converted tree from which the `max-width` that replaced `reembed_confirm.html`'s inline style has been removed
- **THEN** it SHALL report that page as different at desktop width

