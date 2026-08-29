## ADDED Requirements

### Requirement: Single token source for panel colors
All colors rendered by control-panel and auth templates SHALL come from CSS custom properties defined in the shared token block(s); no color literal (hex, rgb/rgba, hsl, named color) may appear outside token definitions in template style blocks or inline styles, except inside vendored assets.

#### Scenario: Literal sweep
- **WHEN** panel and auth templates are scanned for color literals outside custom-property definitions
- **THEN** the scan finds none (vendored files under `static/vendor/` excluded)

#### Scenario: Auth pages share the token set
- **WHEN** `auth_base.html` styles are inspected
- **THEN** they reference the same token names as `base.html` rather than a separate palette

### Requirement: Light and dark palettes
The panel SHALL provide a complete light palette and retain the existing dark palette, selected via a `data-theme` attribute on the root element, with every token defined in both themes.

#### Scenario: Dark theme unchanged
- **WHEN** the dark theme is active
- **THEN** every token resolves to the same value as before this change

#### Scenario: Complete light coverage
- **WHEN** the light theme is active
- **THEN** every token defined for dark has a light value; none falls through to a dark value

#### Scenario: Readable light text
- **WHEN** body text (`--text` on `--bg`/`--surface`) renders in light theme
- **THEN** the pair meets WCAG AA contrast (≥ 4.5:1)

### Requirement: Theme selection and persistence
The panel SHALL default to the OS `prefers-color-scheme` when the user has made no explicit choice, SHALL persist an explicit toggle choice in `localStorage`, SHALL apply the stored choice before first paint on subsequent loads, and SHALL tolerate unavailable storage.

#### Scenario: OS default honored
- **WHEN** a visitor with no stored preference and OS set to light loads any panel or auth page
- **THEN** the page renders in light theme

#### Scenario: Explicit choice wins and persists
- **WHEN** the user toggles to dark on an OS-light machine and reloads
- **THEN** the page renders dark with no flash of light theme before paint

#### Scenario: Storage unavailable
- **WHEN** localStorage access throws
- **THEN** the page renders using the OS preference without script errors

### Requirement: Theme toggle control
Every panel page and every auth page SHALL expose a visible control that switches between light and dark and takes effect immediately without reload.

#### Scenario: Toggle on an authenticated page
- **WHEN** the user activates the toggle on the dashboard
- **THEN** the page's colors switch theme in place and the choice is stored

#### Scenario: Toggle on the login page
- **WHEN** an unauthenticated visitor activates the toggle on the login page
- **THEN** the login page switches theme in place

### Requirement: Charts follow the theme
Chart.js visualizations SHALL derive their colors from the computed token values and SHALL re-render with the new palette when the theme changes, without page reload.

#### Scenario: Chart built under active theme
- **WHEN** the usage page loads in light theme
- **THEN** the chart's dataset, grid, and tooltip colors are the light token values

#### Scenario: Chart updates on toggle
- **WHEN** the user toggles theme while a chart is displayed
- **THEN** the chart re-renders in the new theme's colors without reload
