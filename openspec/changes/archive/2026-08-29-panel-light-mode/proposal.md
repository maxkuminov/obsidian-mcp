## Why

The control panel ships one hand-written dark palette themed after Obsidian. As the project repositions toward "agent memory with a SaaS-style dashboard" (change `readme-agent-memory-positioning`, issue #158), the panel needs a light theme to read as a regular product dashboard; today's CSS also has ~150 hardcoded color literals outside the token block, which blocks any theming at all.

## What Changes

- Consolidate all panel colors into the CSS custom-property token block: fold hardcoded literals in `base.html` (47), `authorize.html` (43), `auth_base.html` (19 — currently its own separate palette), `transfer_upload.html` (13), `usage.html` (11, incl. Chart.js literals), `transfer_download.html` (10), and the remaining templates (0–4 each) into `var(--…)` references, adding per-theme tokens for glows/overlays/shadows that don't invert cleanly.
- Unify `auth_base.html` (login/register/OAuth pages) onto the shared token set.
- Add a light palette; theme selection via `data-theme` on `<html>`, persisted in `localStorage`, defaulting from `prefers-color-scheme`.
- Add a visible light/dark toggle to the panel chrome (and auth pages).
- Chart.js on `usage.html` reads its colors from computed CSS variables and re-renders on theme change.
- No backend, route, or auth changes. No CSP added (deliberate — `docs/architecture/control-panel.md`).

## Capabilities

### New Capabilities

- `panel-theming`: the panel's theming contract — single token source, light + dark palettes, persistence and default rules, chart/theme synchronization, no flash of wrong theme.

### Modified Capabilities

(none — presentational only; no existing spec's requirements change)

## Impact

- Templates under `src/control_panel/templates/` (base, auth_base, and per-page style blocks); no Python changes.
- `docs/architecture/control-panel.md` gains a theming section in the same change.
- Deploy is a container rebuild only; no migration. Coordination issue: #159.
