"""Light-palette contrast audit (task 4.5).

Checks every pair named by the requirement "Light palette contrast matrix"
and writes the table into `contrast-audit.md`.

    python3 openspec/changes/panel-light-mode/checks/contrast.py          # check
    python3 openspec/changes/panel-light-mode/checks/contrast.py --write  # + rewrite the audit

Thresholds, straight from the spec:

  4.5:1  primary and secondary body text on every surface they appear on;
         flash/alert text on its tinted surface; button labels; link text;
         form-control text
  3.0:1  muted text; chart axis labels and tooltip text; status badges;
         disabled-state labels; focus indicators; control borders

Translucent tokens are composited over the surfaces they actually sit on
before the ratio is taken — a badge tint is `rgba(...)` over `--surface`,
never a standalone color — which is the whole reason this is a script and
not a table typed by hand.
"""

import argparse
import re
import sys
from pathlib import Path

import colorscan

AUDIT = Path(__file__).resolve().parent.parent / "contrast-audit.md"

AA_TEXT = 4.5
AA_LARGE = 3.0


# ---------------------------------------------------------------- color math

def parse_color(value: str):
    """-> (r, g, b, a) floats in 0..255 / 0..1."""
    v = value.strip()
    if v == "white":
        return (255.0, 255.0, 255.0, 1.0)
    if v == "black":
        return (0.0, 0.0, 0.0, 1.0)
    m = re.fullmatch(r"#([0-9a-fA-F]{3,8})", v)
    if m:
        h = m.group(1)
        if len(h) in (3, 4):
            h = "".join(c * 2 for c in h)
        r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
        a = int(h[6:8], 16) / 255 if len(h) == 8 else 1.0
        return (float(r), float(g), float(b), a)
    m = re.fullmatch(r"rgba?\(([^)]*)\)", v)
    if m:
        parts = [p.strip() for p in re.split(r"[,\s/]+", m.group(1)) if p.strip()]
        r, g, b = (float(p) for p in parts[:3])
        a = float(parts[3]) if len(parts) > 3 else 1.0
        return (r, g, b, a)
    raise ValueError(f"cannot parse color {value!r}")


def over(fg, bg):
    """Composite fg (may be translucent) onto an opaque bg."""
    fr, fg_, fb, fa = fg
    br, bg_, bb, _ = bg
    return (fa * fr + (1 - fa) * br,
            fa * fg_ + (1 - fa) * bg_,
            fa * fb + (1 - fa) * bb,
            1.0)


def flatten(stack):
    """Composite a stack, base first, onto an opaque base."""
    out = stack[0]
    if out[3] != 1.0:
        raise ValueError("the base of a stack must be opaque")
    for layer in stack[1:]:
        out = over(layer, out)
    return out


def luminance(c):
    def channel(x):
        x = x / 255
        return x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4
    return 0.2126 * channel(c[0]) + 0.7152 * channel(c[1]) + 0.0722 * channel(c[2])


def ratio(fg, bg):
    a, b = luminance(fg), luminance(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


# ------------------------------------------------------------- token loading

def light_tokens() -> dict:
    """Every declaration of the `:root[data-theme="light"]` block.

    Read straight out of the partial rather than through the color scanner:
    `--disabled-opacity` is a theme token that is not a color, and the audit
    needs it to fade a disabled button correctly.
    """
    text = colorscan.blank_jinja((colorscan.TEMPLATE_DIR / "_theme.html").read_text())
    out = {}
    for m in re.finditer(r"<style[^>]*>(.*?)</style>", text, re.S | re.I):
        for context, prop, value, _ in colorscan.parse_css(m.group(1), 0, text, "_theme.html"):
            if context == ':root[data-theme="light"]' and prop.startswith("--"):
                out[prop] = value
    if not out:
        sys.exit("no light tokens found in _theme.html")
    return out


class Palette:
    def __init__(self, tokens: dict):
        self.tokens = tokens

    def raw(self, name: str) -> str:
        if name not in self.tokens:
            sys.exit(f"light palette has no token {name}")
        return self.tokens[name]

    def color(self, name: str):
        return parse_color(self.raw(name))

    def bg(self, *names):
        """Composite a background stack given base-first token names."""
        return flatten([self.color(n) for n in names])


# ------------------------------------------------------------------ the matrix
# (category, threshold, foreground token, background stack, where it renders)
def matrix(P: Palette):
    S = ("--surface",)              # cards, sidebar, tables, auth/consent card
    B = ("--bg",)                   # page ground, and the field-input fill
    S2 = ("--surface-2",)
    S3 = ("--surface-3",)

    rows = [
        # ── primary and secondary body text on every surface they appear on
        ("body text", AA_TEXT, "--text", B, "body, page copy"),
        ("body text", AA_TEXT, "--text", S, "card body, table hover row, modal, kv value"),
        ("body text", AA_TEXT, "--text", S2, "nav-item:hover, table row hover"),
        ("body text", AA_TEXT, "--text", S3, ".btn-ghost:hover, .theme-toggle:hover"),
        ("body text", AA_TEXT, "--text", ("--bg-2",), "settings confirm modal"),
        ("body text", AA_TEXT, "--text", ("--surface", "--topbar-bg"), "mobile top bar brand"),
        ("body text", AA_TEXT, "--text", ("--surface", "--code-bg"), "keys.html code block"),
        ("body text", AA_TEXT, "--text", ("--surface", "--code-bg-soft"), "vault.html note <pre>"),
        ("secondary text", AA_TEXT, "--text-2", B, "page copy"),
        ("secondary text", AA_TEXT, "--text-2", S, "card-title, table cell, kv key, stat-label"),
        ("secondary text", AA_TEXT, "--text-2", S2, ".user-badge, .btn-ghost, .theme-toggle"),
        ("secondary text", AA_TEXT, "--text-2", ("--surface", "--neutral-surface"), ".badge-gray"),
        ("secondary text", AA_TEXT, "--consent-text", S, "/authorize card copy"),
        ("secondary text", AA_TEXT, "--consent-text-2", ("--surface", "--consent-surface"),
         "/authorize request box"),

        # ── flash / alert text on its tinted surface
        ("alert text", AA_TEXT, "--success-text", ("--surface", "--success-surface-faint"),
         ".alert-success"),
        ("alert text", AA_TEXT, "--error-text", ("--surface", "--error-surface-faint"),
         ".alert-warning, auth .alert"),
        ("alert text", AA_TEXT, "--info-text", ("--surface", "--info-surface-faint"),
         "auth .alert-info"),
        ("alert text", AA_TEXT, "--success", ("--surface", "--success-surface-soft"),
         ".inline-status.ok"),
        ("alert text", AA_TEXT, "--error", ("--surface", "--error-surface-soft"),
         ".inline-status.err"),
        ("alert text", AA_TEXT, "--warning", ("--surface", "--warning-surface"),
         "warning copy on its tint"),
        ("alert text", AA_TEXT, "--info", ("--surface", "--info-surface"), "info copy on its tint"),

        # ── button labels
        ("button label", AA_TEXT, "--on-primary", ("--primary-dim",), ".btn-primary"),
        ("button label", AA_TEXT, "--on-primary-strong", ("--primary",),
         ".btn-primary:hover, .btn-lg gradient (light end)"),
        ("button label", AA_TEXT, "--on-primary-strong", ("--primary-dim",),
         ".btn-lg gradient (dark end), auth .btn-submit"),
        ("button label", AA_TEXT, "--error", ("--surface", "--error-surface-soft"), ".btn-danger"),
        ("button label", AA_TEXT, "--error", ("--surface", "--error-surface-strong"),
         ".btn-danger:hover"),
        ("button label", AA_TEXT, "--text-2", S2, ".btn-ghost"),
        ("button label", AA_TEXT, "--consent-primary-text", ("--consent-primary-dim",),
         "/authorize .btn-approve"),
        ("button label", AA_TEXT, "--consent-primary-text", ("--consent-primary",),
         "/authorize .btn-approve:hover"),
        ("button label", AA_TEXT, "--consent-text-3", ("--surface", "--consent-neutral-surface"),
         "/authorize .btn-deny"),

        # ── link text
        ("link text", AA_TEXT, "--text-2", S, ".nav-item, vault note links"),
        ("link text", AA_TEXT, "--text", S, "dashboard note links"),
        ("link text", AA_TEXT, "--error", S, ".btn-link"),
        ("link text", AA_TEXT, "--error-text-hover", S, ".btn-link:hover"),
        ("link text", AA_TEXT, "--primary-text", ("--surface", "--primary-glow"),
         ".nav-item.active, .badge-purple, dashboard tool chip"),
        ("link text", AA_TEXT, "--primary-text", S, ".stat-num.accent, usage actor column"),

        # ── form-control text
        ("form text", AA_TEXT, "--text", B, ".field-input, .field-select value"),
        ("form text", AA_TEXT, "--text-2", B, ".field-label above the control"),
        ("form text", AA_TEXT, "--key-text", ("--surface", "--code-bg"), "keys.html revealed key"),

        # ── muted text (>= 3:1)
        ("muted text", AA_LARGE, "--text-3", S, ".nav-section, .brand-tagline, .stat-sub"),
        ("muted text", AA_LARGE, "--text-3", B, ".auth-sub, .field-hint on the page ground"),
        ("muted text", AA_LARGE, "--consent-text-3", S, "/authorize .scope-option-desc"),
        ("muted text", AA_LARGE, "--consent-label", S, "/authorize .scope-label"),
        ("muted text", AA_LARGE, "--consent-warn", ("--surface", "--consent-surface"),
         "/authorize .request-note.warn"),

        # ── chart axis labels and tooltip text (>= 3:1)
        ("chart text", AA_LARGE, "--chart-tick", S, "usage chart axis ticks"),
        ("chart text", AA_LARGE, "--chart-tooltip-title", ("--chart-tooltip-bg",), "tooltip title"),
        ("chart text", AA_LARGE, "--chart-tooltip-body", ("--chart-tooltip-bg",), "tooltip body"),

        # ── status badges (>= 3:1)
        ("badge", AA_LARGE, "--success", ("--surface", "--success-surface"), ".badge-green"),
        ("badge", AA_LARGE, "--error", ("--surface", "--error-surface"), ".badge-red"),
        ("badge", AA_LARGE, "--warning", ("--surface", "--warning-surface"), ".badge-yellow"),
        ("badge", AA_LARGE, "--info", ("--surface", "--info-surface"), ".badge-blue"),
        ("badge", AA_LARGE, "--primary-text", ("--surface", "--primary-glow"), ".badge-purple"),
        ("badge", AA_LARGE, "--text-2", ("--surface", "--neutral-surface"), ".badge-gray"),
        ("badge", AA_LARGE, "--primary-text", ("--surface", "--primary-glow"), ".user-badge-role"),

        # ── focus indicators (>= 3:1)
        ("focus", AA_LARGE, "--primary", B, ".field-input:focus border on the control fill"),
        ("focus", AA_LARGE, "--primary", S, ".field-input:focus border against the card"),
        ("focus", AA_LARGE, "--primary", S2, ".theme-toggle:focus-visible outline"),
        ("focus", AA_LARGE, "--consent-primary-dim", S,
         "/authorize .scope-option:checked border"),

        # ── control borders (>= 3:1)
        # Enumerated per control, not per token: a boundary has to clear 3:1
        # against the fill it encloses AND the ground it sits on, and those are
        # different colors for almost every control in the panel.
        ("control border", AA_LARGE, "--control-border", B, ".field-input against its own fill"),
        ("control border", AA_LARGE, "--control-border", S,
         ".field-input / .btn-ghost / .theme-toggle against the card or sidebar"),
        ("control border", AA_LARGE, "--control-border", S2,
         ".btn-ghost / .theme-toggle / .topbar-toggle against their own fill"),
        ("control border", AA_LARGE, "--control-border", ("--bg", "--topbar-bg"),
         ".topbar-toggle against the mobile top bar"),
        ("control border", AA_LARGE, "--control-border-strong", S3,
         ".btn-ghost:hover / .theme-toggle:hover / .topbar-toggle:hover fill"),
        ("control border", AA_LARGE, "--control-border-strong", S,
         ".btn-ghost:hover / .theme-toggle:hover against the card"),
        ("control border", AA_LARGE, "--error-border-strong", ("--surface", "--error-surface-soft"),
         ".btn-danger against its own fill — the fill is ~1.1:1 on the card, so "
         "the border is the only boundary"),
        ("control border", AA_LARGE, "--error-border-strong",
         ("--surface", "--error-surface-strong"), ".btn-danger:hover fill"),
        ("control border", AA_LARGE, "--error-border-strong", S,
         ".btn-danger against the card"),
        ("control border", AA_LARGE, "--consent-control-border", S,
         "/authorize .scope-option radio card against the card"),
        ("control border", AA_LARGE, "--consent-control-border",
         ("--surface", "--consent-neutral-surface"), "/authorize .btn-deny against its own fill"),
        ("control border", AA_LARGE, "--consent-control-border",
         ("--surface", "--consent-neutral-surface-hover"), "/authorize .btn-deny:hover fill"),
        ("control border", AA_LARGE, "--consent-control-border-hover",
         ("--surface", "--consent-option-hover"), "/authorize .scope-option:hover"),
        ("control border", AA_LARGE, "--consent-control-border-hover", S,
         "/authorize .scope-option:hover against the card"),
        ("control border", AA_LARGE, "--consent-primary-dim",
         ("--surface", "--consent-option-active"), "/authorize checked .scope-option"),
        ("control border", AA_LARGE, "--scrollbar-thumb", S,
         "::-webkit-scrollbar-thumb over a card"),
        ("control border", AA_LARGE, "--scrollbar-thumb", B,
         "::-webkit-scrollbar-thumb over the page ground"),
        ("control border", AA_LARGE, "--text-3", S,
         "::-webkit-scrollbar-thumb:hover"),
        # A graphical object that conveys a value: what has to be legible is the
        # filled part against the track, not the track against the card.
        ("control border", AA_LARGE, "--primary-dim", ("--surface", "--border"),
         "dashboard embedding-progress fill against its track"),
    ]

    # Disabled-state labels (>= 3:1). `.btn:disabled` sets opacity on the whole
    # button, so both its fill and its label composite over what is behind it.
    def disabled(fill_stack, label_token, where):
        alpha = float(P.raw("--disabled-opacity"))
        behind = P.bg(*fill_stack[:-1]) if len(fill_stack) > 1 else P.bg("--surface")
        fill = P.bg(*fill_stack)
        faded_fill = over((fill[0], fill[1], fill[2], alpha), behind)
        label = P.color(label_token)
        label_on_fill = over(label, fill)
        faded_label = over((label_on_fill[0], label_on_fill[1], label_on_fill[2], alpha), behind)
        return ("disabled label", AA_LARGE, ratio(faded_label, faded_fill), where)

    extra = [
        disabled(("--primary-dim",), "--on-primary", ".btn-primary:disabled"),
        disabled(("--surface", "--surface-2"), "--text-2", ".btn-ghost:disabled"),
        disabled(("--surface", "--error-surface-soft"), "--error", ".btn-danger:disabled"),
    ]
    return rows, extra


# Not in the normative matrix: decorative rules that WCAG does not govern,
# recorded so a reader can see they were considered rather than missed.
def informational(P: Palette):
    return [
        ("--chart-grid", ("--surface",), "chart gridlines — decorative; the axis "
         "labels that carry the meaning are in the matrix above"),
        ("--border", ("--surface",), "card outline, table/kv divider, sidebar rule, "
         "progress-bar track. No control boundary uses this token any more — "
         "`.field-input`, `.btn-ghost`, `.theme-toggle` and `.topbar-toggle` were "
         "repointed at `--control-border`"),
        ("--border-2", ("--surface",), "`.stat-card:hover` outline, `.modal-panel` "
         "outline, `.badge-gray` outline. No control boundary uses this token any "
         "more — the hover borders went to `--control-border-strong` and the "
         "scrollbar thumb to `--scrollbar-thumb`"),
    ]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="rewrite contrast-audit.md")
    args = ap.parse_args()

    P = Palette(light_tokens())
    rows, extra = matrix(P)

    results = []
    for category, threshold, fg_token, bg_stack, where in rows:
        bg = P.bg(*bg_stack)
        fg = over(P.color(fg_token), bg)
        results.append((category, threshold, fg_token,
                        " on ".join(reversed(bg_stack)), ratio(fg, bg), where))
    for category, threshold, r, where in extra:
        results.append((category, threshold, "(button label)", "(faded button fill)", r, where))

    failures = [r for r in results if r[4] < r[1]]

    worst: dict = {}
    for category, threshold, _, _, r, _ in results:
        key = f"{threshold:.1f}"
        worst[key] = min(worst.get(key, 99.0), r)

    print(f"pairs checked : {len(results)}")
    print(f"failures      : {len(failures)}")
    for key in sorted(worst):
        print(f"  worst ratio at >= {key}:1 threshold : {worst[key]:.2f}:1")
    for category, threshold, fg, bg, r, where in failures:
        print(f"  FAIL {r:5.2f}:1 (needs {threshold}) {fg} on {bg} — {where}")

    if args.write:
        AUDIT.write_text(render_audit(P, results, failures, worst))
        print(f"wrote {AUDIT}")
    return 1 if failures else 0


def render_audit(P, results, failures, worst) -> str:
    lines = [
        "# Light palette contrast audit",
        "",
        "Generated by `checks/contrast.py` — re-run it to reproduce this table:",
        "",
        "```",
        "python3 openspec/changes/panel-light-mode/checks/contrast.py --write",
        "```",
        "",
        "Every translucent token is composited over the surface it actually sits",
        "on before the ratio is taken (a badge tint is `rgba(...)` over",
        "`--surface`, not a standalone color); `.btn:disabled` sets `opacity` on",
        "the whole button, so its label and its fill are both faded over what is",
        "behind the button.",
        "",
        f"**{len(results)} pairs checked, {len(failures)} failures.**",
        "",
    ]
    for key in sorted(worst):
        lines.append(f"- worst ratio at the ≥ {key}:1 threshold — **{worst[key]:.2f}:1**")
    lines += ["", "| Category | Need | Ratio | Foreground | Background | Where |",
              "| --- | --- | --- | --- | --- | --- |"]
    for category, threshold, fg, bg, r, where in results:
        mark = "✅" if r >= threshold else "❌"
        lines.append(f"| {category} | {threshold}:1 | {mark} {r:.2f}:1 | `{fg}` | `{bg}` | {where} |")

    lines += ["", "## Outside the normative matrix", "",
              "Decorative rules WCAG 1.4.11 does not govern — recorded so it is",
              "clear they were considered, not overlooked. A 3:1 gridline or card",
              "divider would read as a hard rule across every card in the panel.",
              "", "| Token | Against | Ratio | Note |", "| --- | --- | --- | --- |"]
    for token, stack, note in informational(P):
        bg = P.bg(*stack)
        fg = over(P.color(token), bg)
        lines.append(f"| `{token}` | `{stack[-1]}` | {ratio(fg, bg):.2f}:1 | {note} |")
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
