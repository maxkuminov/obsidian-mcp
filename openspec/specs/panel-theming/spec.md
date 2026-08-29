# panel-theming Specification

## Purpose
TBD - created by archiving change panel-light-mode. Update Purpose after archive.
## Requirements
### Requirement: Single token source for panel colors
All colors rendered by panel and auth templates (`base.html`, `auth_base.html`, `authorize.html`, and every page extending them) SHALL come from CSS custom properties defined in one shared Jinja token partial included by all three bases; page templates MAY add scoped tokens but SHALL NOT define an independent palette. The color scan scope is: CSS declarations in `<style>` blocks and `style=""` attributes, SVG `fill`/`stroke` presentation attributes, Chart.js color options in template JavaScript, and `<meta name="theme-color">`. Within that scope no color literal may appear outside token definitions, with these exceptions: the semantic keywords `currentColor`, `transparent`, and `inherit`; colors inside data-URI images (which SHALL be theme-neutral or provided per theme); vendored assets under `static/vendor/`; and a token-bound static default on `<meta name="theme-color">` — a literal serving the no-JS case that a check SHALL pin to its declared token's dark value.

#### Scenario: Literal sweep
- **WHEN** panel and auth templates are scanned across the defined scope
- **THEN** no color literal appears outside custom-property definitions, allowing only the enumerated exceptions

#### Scenario: One physical palette
- **WHEN** `base.html`, `auth_base.html`, and `authorize.html` are inspected
- **THEN** each includes the same token partial and none contains its own full palette definition

### Requirement: Dark baseline preserved (zero-visual-diff sweep)
Before any template mutation, a baseline inventory SHALL be recorded of every literal-bearing color declaration in scope (template, selector/context, property, literal value); declarations using the permitted semantic keywords (`currentColor`, `transparent`, `inherit`) are inventoried syntactically and SHALL keep their keyword unchanged. After the sweep, with the dark theme active, resolving every migrated declaration through its token SHALL reproduce the baseline literal exactly, for pre-existing and newly introduced tokens alike.

#### Scenario: Baseline replay
- **WHEN** the post-sweep templates are evaluated against the recorded baseline inventory under the dark theme
- **THEN** every migrated entry resolves to its exact baseline literal, and every semantic-keyword entry still carries its keyword

### Requirement: Light and dark palettes
The panel SHALL provide a complete light palette selected via `data-theme` on the root element (bare `:root` = dark; `:root[data-theme="light"]` = light; a `prefers-color-scheme: light` block guarded by `:root:not([data-theme="dark"])` supplies the OS default), and SHALL set the CSS `color-scheme` property to match the active theme so native form controls follow it.

#### Scenario: Complete light coverage
- **WHEN** the light theme is active
- **THEN** every token defined for dark has a light value; none falls through to a dark value

#### Scenario: Native controls follow theme
- **WHEN** the light theme is active on a page with checkboxes, radios, or selects
- **THEN** the computed `color-scheme` is `light` (and `dark` under the dark theme)

### Requirement: Light palette contrast matrix
In the light theme: primary and secondary body text (`--text`, `--text-2`) on every surface they appear on, flash/alert text (success, error, warning, info) on its tinted surface, button labels, link text, and form-control text SHALL meet WCAG AA for normal text (≥ 4.5:1); muted text (`--text-3`), chart axis labels and tooltip text, status badges, disabled-state labels, focus indicators, and control borders SHALL meet ≥ 3:1 against their backgrounds.

#### Scenario: Contrast audit
- **WHEN** the light token values are checked pairwise per the matrix above
- **THEN** every pair meets its threshold, with the checked pairs and ratios recorded in the change

### Requirement: Theme selection and persistence
Panel and auth pages SHALL default to the OS `prefers-color-scheme` when no stored choice exists; an explicit toggle choice SHALL be persisted to `localStorage` when storage is available and applied before first paint on subsequent loads via a shared pre-paint bootstrap script included in the `<head>` of all three bases. When storage access throws, the toggle SHALL still switch the current document without script errors, and the next load SHALL follow the OS preference. The bootstrap/toggle SHALL also keep `<meta name="theme-color">` in sync via script.

#### Scenario: OS default honored
- **WHEN** a visitor with no stored preference and OS set to light loads any panel or auth page (including `/authorize`)
- **THEN** the page renders in light theme

#### Scenario: Explicit choice wins and persists
- **WHEN** the user toggles to dark on an OS-light machine and reloads any panel or auth page
- **THEN** the page renders dark with no flash of light theme before paint

#### Scenario: Storage unavailable
- **WHEN** localStorage access throws
- **THEN** the toggle still switches the current document without errors, and the next load uses the OS preference

### Requirement: Theme toggle control
Every panel page and every auth page (login, register, bootstrap, OAuth consent at `/authorize`) SHALL expose a visible control that switches between light and dark in place, without reload.

#### Scenario: Toggle on an authenticated page
- **WHEN** the user activates the toggle on the dashboard
- **THEN** the page's colors switch theme in place and the choice is stored (when storage is available)

#### Scenario: Toggle on the OAuth consent page
- **WHEN** a visitor activates the toggle on `/authorize`
- **THEN** the consent page switches theme in place

### Requirement: Charts follow the theme
Chart.js visualizations SHALL derive their colors from computed token values at build time and SHALL re-render with the new palette when the theme changes, without page reload.

#### Scenario: Chart built under active theme
- **WHEN** the usage page loads in light theme
- **THEN** the chart's dataset, grid, and tooltip colors are the light token values

#### Scenario: Chart updates on toggle
- **WHEN** the user toggles theme while a chart is displayed
- **THEN** the chart re-renders in the new theme's colors without reload

### Requirement: Transfer pages are a distinct theming surface
The transfer templates (`transfer_upload.html`, `transfer_download.html`) SHALL receive only a local token sweep (their own token block, same naming convention) and SHALL keep their existing OS-responsive `prefers-color-scheme` behavior with no toggle and no `localStorage`; their nonce-bearing inline styles/scripts, restrictive CSP, `no-store`/static response discipline, and absence of external origins SHALL be preserved, and they SHALL NOT include panel chrome or shared panel partials.

#### Scenario: Transfer page security headers unchanged
- **WHEN** a transfer page response is compared before and after the change with each response's per-request nonce value replaced by a canonical placeholder
- **THEN** the CSP, Referrer-Policy, and Cache-Control headers are byte-identical under that canonicalization, and in each response every inline style/script nonce equals the CSP's nonce

#### Scenario: Transfer page theming
- **WHEN** a transfer page is loaded with OS set to light, then dark
- **THEN** it follows the OS preference in both directions with no persistence and no toggle control

