## Context

The panel's styling is hand-written CSS living in `<style>` blocks inside Jinja2 templates. `base.html` defines a `:root` token block (18 tokens, ~83 `var()` uses) but ~150 color literals have drifted outside it across templates; `auth_base.html` (login/register/bootstrap/OAuth consent) carries an entirely separate palette. Chart.js is vendored and configured with literal colors in `usage.html`. There is deliberately no CSP (inline handlers everywhere — see `docs/architecture/control-panel.md`), so an inline theme-bootstrap script is available. Templates are server-rendered; there is no build step and no Tailwind.

## Goals / Non-Goals

**Goals:**
- One token source for every color in the panel, including auth pages.
- A light palette that reads as a standard SaaS dashboard; the existing dark palette preserved pixel-for-pixel as the dark theme.
- Correct default (OS preference), explicit override (toggle + `localStorage`), no flash of wrong theme on load, charts that follow the theme.

**Non-Goals:**
- No backend/session persistence of the preference (localStorage is enough for an admin panel).
- No CSP, no build tooling, no CSS framework, no restyle/redesign of layout or typography — colors only.
- No theming of the README screenshot or docs (follow-up once light mode exists).

## Decisions

1. **Theme mechanism: `data-theme` attribute on `<html>` + `localStorage`, OS default.** Bare `:root { }` keeps the dark palette (current default, zero regression risk for existing sessions); `:root[data-theme="light"]` overrides with the light palette; a `prefers-color-scheme: light` media block guarded as `:root:not([data-theme="dark"])` supplies the OS default. An inline `<head>` script reads `localStorage` and stamps `data-theme` before first paint (no FOUC). try/catch around storage access. Alternative (light-first tokens) rejected: inverts the tested baseline and risks regressing every existing page for no benefit.
2. **Dark stays canonical; light is the override set.** Every token gets a light value chosen for AA contrast on light surfaces; glows (`--primary-glow*`), overlays, and shadows get their own per-theme values rather than being derived, because rgba glows on dark do not invert.
3. **Token sweep before palette work, gated by a baseline inventory.** Before any edit, record every in-scope color declaration (template, selector/context, property, resolved value) into a manifest checked into the change directory. Phase 1 replaces literals with tokens; the gate is mechanical replay — under dark theme every manifest entry must resolve to its exact baseline value, including through newly introduced role tokens (chart grid, code background, danger surface, overlays). Scan scope and exceptions are normative in the spec (style blocks, style attributes, SVG presentation attributes, Chart.js options, meta theme-color; `currentColor`/`transparent`/`inherit` and theme-neutral data-URI colors allowed).
4. **One physical token partial.** `base.html`, `auth_base.html`, AND `authorize.html` (the standalone OAuth consent page — it extends neither base) all include a single Jinja partial holding the token definitions and the pre-paint bootstrap script; none keeps its own palette, so they cannot drift. The toggle appears on all three surfaces. The bootstrap also syncs `<meta name="theme-color">` via script (CSS variables cannot reach it) and sets `color-scheme` so native controls follow the theme.
5. **Chart.js reads computed tokens.** `usage.html` resolves colors via `getComputedStyle(document.documentElement).getPropertyValue('--…')` at chart-build time; the toggle dispatches a `themechange` event and the chart updates its dataset/scale/tooltip colors and calls `update()`. Alternative (destroy/rebuild chart) acceptable fallback if in-place update misbehaves.
6. **Toggle UI**: a small icon button in the top nav (and auth card footer), cycling light ↔ dark; explicit choice writes `localStorage` and wins over OS preference thereafter. Three-state (system/light/dark) rejected as over-engineering for an admin panel; clearing the key is the escape hatch.

## Risks / Trade-offs

- [Literal→token sweep silently changes a shade] → phase 1 is zero-visual-diff by construction: literals map to tokens with identical values; reviewer diffs rendered CSS values, not looks.
- [Light palette contrast failures (text-3, borders, warning-on-light)] → pick values against WCAG AA up front; verifier checks the declared pairs.
- [FOUC / wrong theme flash] → inline pre-paint script in `<head>` of both bases; verified by loading with `localStorage` set to the opposite of OS preference.
- [Chart keeps stale colors after toggle] → explicit `themechange` listener requirement with its own spec scenario.
- [Jinja `{{ }}` vs CSS/JS braces friction in the shared include] → keep the bootstrap script brace-free JS; no template interpolation inside it.

7. **Transfer pages are a separate surface.** `transfer_upload.html` / `transfer_download.html` serve third parties under a strict nonce CSP with static-response discipline (`src/transfer/routes.py`) and already ship a light-first OS-responsive palette. They get only a local token sweep with the same naming convention — no shared partial (would break nonce/static discipline), no toggle, no localStorage. Their security headers must be byte-identical before/after once each response's per-request nonce is replaced with the same canonical placeholder, and within each response every inline nonce must equal the CSP's nonce.

**Accepted limitations (verifier advisories):** the control↔token binding is documented in the architecture note but not gated — a future control reusing `--border` regresses silently until the proposed selector-scan gate exists; `render.py`'s matchMedia assertion cannot distinguish the OS-flip listener from the OS-default read. Both recorded here rather than silently dropped.

## Migration Plan

Deploy is `make deploy` (container rebuild; no migration, no schema gate). Rollback is redeploying the previous image. Existing users see dark by default unless their OS prefers light and they never chose — called out to Max at review since that flips OS-light users' default; the localStorage escape (`theme=dark`) is one click.

## Open Questions

(none blocking)
