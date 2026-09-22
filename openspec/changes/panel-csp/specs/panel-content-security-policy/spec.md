## ADDED Requirements

### Requirement: Every HTML response the application renders SHALL carry a nonce-based Content-Security-Policy unless it sets its own
Every response whose `Content-Type` is `text/html` SHALL carry a `Content-Security-Policy` header built from a per-response nonce, unless the route that produced it has already set a `Content-Security-Policy` header of its own, in which case that header SHALL be left byte-for-byte unchanged. This covers every panel page under `/admin`, the login and register pages, and the OAuth consent page at `GET /authorize`. Responses of any other content type SHALL NOT gain a policy from this requirement.

The nonce SHALL be generated from a cryptographically secure source with at least 128 bits of entropy, SHALL be fresh for every response, and SHALL be the same value in the header and in every `nonce` attribute of that response's body.

#### Scenario: A panel page carries the policy
- **WHEN** an authenticated administrator requests any panel page, such as `GET /admin/keys`
- **THEN** the response SHALL carry a `Content-Security-Policy` header containing `script-src 'nonce-<N>'`
- **AND** every `nonce="…"` attribute in the body SHALL equal `<N>`

#### Scenario: Auth and consent pages carry the policy
- **WHEN** a visitor requests `GET /admin/auth/login`, `GET /admin/register`, or `GET /authorize` for a registered client with a valid request
- **THEN** each HTML response SHALL carry the policy with a nonce equal to every nonce in its body

#### Scenario: The nonce is fresh per response
- **WHEN** the same page is requested twice
- **THEN** the two responses SHALL carry different nonces

#### Scenario: The transfer pages keep their own policy
- **WHEN** `GET /transfer/upload` or `GET /transfer/download` is requested
- **THEN** its `Content-Security-Policy` header SHALL be exactly the one the transfer route sets, with `default-src 'none'`, unchanged by this requirement

#### Scenario: A JSON response gains no policy
- **WHEN** a request is answered with `application/json`, such as an OAuth error from `GET /authorize`
- **THEN** the response SHALL NOT gain a `Content-Security-Policy` header from this requirement

#### Scenario: Every HTML route is covered by the test suite
- **WHEN** the test suite runs
- **THEN** it SHALL enumerate every route declared with an HTML response class, together with `GET /authorize`, and SHALL fail if any such route is not exercised by the header test

### Requirement: The panel policy SHALL consist of the enumerated directive set and SHALL NOT permit inline or evaluated script
The policy SHALL contain exactly these directives, with `<N>` the response's nonce: `default-src 'self'`; `script-src 'nonce-<N>'`; `style-src https://fonts.googleapis.com 'unsafe-inline'`; `style-src-elem 'nonce-<N>' https://fonts.googleapis.com`; `style-src-attr 'unsafe-inline'`; `img-src 'self' data:`; `font-src https://fonts.gstatic.com`; `connect-src 'self'`; `object-src 'none'`; `base-uri 'none'`; `frame-ancestors 'none'`; `form-action 'self'`, the last extended only as the consent-page requirement permits. `script-src` SHALL NOT contain `'unsafe-inline'`, `'unsafe-eval'`, `'unsafe-hashes'`, a scheme source or a host source.

`style-src` carries no nonce on purpose: it is consulted only by browsers that do not implement `style-src-elem` and `style-src-attr`, and a nonce there would make such a browser ignore `'unsafe-inline'` and refuse every inline style attribute.

#### Scenario: The script directive admits only the nonce
- **WHEN** the policy header of any panel page is parsed
- **THEN** `script-src` SHALL consist of exactly one source, `'nonce-<N>'`

#### Scenario: Style elements need the nonce, style attributes do not
- **WHEN** the policy header is parsed
- **THEN** `style-src-elem` SHALL contain `'nonce-<N>'` and `https://fonts.googleapis.com` and SHALL NOT contain `'unsafe-inline'`
- **AND** `style-src-attr` SHALL be `'unsafe-inline'`

#### Scenario: The fixed directives are present
- **WHEN** the policy header is parsed
- **THEN** it SHALL contain `object-src 'none'`, `base-uri 'none'`, `frame-ancestors 'none'`, `img-src 'self' data:`, `font-src https://fonts.gstatic.com` and `connect-src 'self'`

### Requirement: The consent page's form-action SHALL admit exactly the origin of the validated redirect URI
The `GET /authorize` response that renders the consent form SHALL extend `form-action` to `'self' https://<host>[:<port>]`, where host and port are those of the `redirect_uri` that the request validated against the client's registered redirect URIs, the host is lower-case, and the port is written only when present and not 443. The origin SHALL be written into the header only when the host matches the DNS-label pattern of letters, digits, hyphens and dots and the port consists of digits alone; otherwise that response's `form-action` SHALL be `'self' https:`. No other response SHALL carry a form-action source beyond `'self'`, and no value derived from a request SHALL reach the header without passing that check.

The consent form posts to `/authorize`, which answers with a redirect to the registered redirect URI for both approve and deny; browsers that apply `form-action` to redirects following a form submission would otherwise block the OAuth flow.

#### Scenario: Approve and deny reach the registered redirect URI
- **WHEN** a client registered with `https://client.example/cb` is authorized and the user approves, or denies, on the consent page
- **THEN** the consent page's policy SHALL contain `form-action 'self' https://client.example`
- **AND** the browser SHALL follow the 302 to `https://client.example/cb?…` without a policy violation

#### Scenario: A non-default port is carried
- **WHEN** the validated redirect URI is `https://client.example:8443/cb`
- **THEN** the consent page's `form-action` SHALL be `'self' https://client.example:8443`

#### Scenario: An inexpressible host falls back without omitting the directive
- **WHEN** the validated redirect URI's host is an IPv6 literal or otherwise fails the host check
- **THEN** that response's `form-action` SHALL be `'self' https:`

#### Scenario: Header metacharacters never reach the header
- **WHEN** a host value containing `;`, `,`, whitespace or a quote reaches the origin builder
- **THEN** the emitted header SHALL NOT contain that value and SHALL use the fallback

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

### Requirement: Confirmation-guarded controls MUST fail closed
Every control that asks for confirmation before submitting a form SHALL be a non-submitting button (`type="button"`) carrying the confirmation text in a `data-confirm` attribute, and its form SHALL contain no other submit control. The form SHALL be submitted only by script, after the user accepts the confirmation, using `requestSubmit()`. If the script does not run, activating the control SHALL submit nothing. The confirmation text SHALL be read as an attribute string and SHALL NOT be evaluated as script.

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

### Requirement: The panel SHALL NOT load a script library that turns markup attributes into requests or code
The panel, auth and consent templates SHALL NOT load htmx or any other library that issues requests or evaluates code from declarative HTML attributes, and the vendored htmx file SHALL be removed from the static directory. Chart.js MAY remain loaded because it evaluates no markup and requires no `'unsafe-eval'`.

#### Scenario: htmx is gone
- **WHEN** the templates and `src/control_panel/static/vendor/` are inspected
- **THEN** no template SHALL reference htmx and no htmx file SHALL be served under `/admin/static/`

#### Scenario: Chart.js still renders under the policy
- **WHEN** the usage page is loaded under the enforced policy
- **THEN** the chart SHALL render and SHALL recolour on theme change with no policy violation
