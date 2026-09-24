## MODIFIED Requirements

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

## ADDED Requirements

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
