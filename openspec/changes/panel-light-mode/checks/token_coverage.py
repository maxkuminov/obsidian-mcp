"""Token coverage and palette-drift check.

Proves the scenarios "Complete light coverage", "One physical palette" and
"Native controls follow theme":

  * every token the dark palette defines has a light value — none falls
    through to a dark value;
  * the `:root[data-theme="light"]` block and the `prefers-color-scheme: light`
    block are identical. CSS cannot share one declaration list between a
    selector and a media-guarded selector, so the palette is written twice;
    this is what stops the two copies drifting;
  * `color-scheme` is declared in all three blocks, so native form controls
    follow the theme;
  * only `_theme.html` defines a panel palette — `base.html`, `auth_base.html`
    and `authorize.html` include it and define no core tokens of their own,
    and each carries the pre-paint bootstrap through that include.

    python3 openspec/changes/panel-light-mode/checks/token_coverage.py
"""

import re
import sys

import colorscan

DARK = ":root"
LIGHT = ':root[data-theme="light"]'
OS_LIGHT = '@media (prefers-color-scheme: light) > :root:not([data-theme="dark"])'

PANEL_ROOTS = ["base.html", "auth_base.html", "authorize.html"]


def blocks() -> dict:
    text = colorscan.blank_jinja((colorscan.TEMPLATE_DIR / "_theme.html").read_text())
    out: dict = {}
    for m in re.finditer(r"<style[^>]*>(.*?)</style>", text, re.S | re.I):
        for context, prop, value, _ in colorscan.parse_css(m.group(1), 0, text, "_theme.html"):
            if prop.startswith("--") or prop == "color-scheme":
                out.setdefault(context, {})[prop] = colorscan.normalize(value)
    return out


def main() -> int:
    failures = []
    b = blocks()
    for name in (DARK, LIGHT, OS_LIGHT):
        if name not in b:
            failures.append(f"missing token block: {name}")
    if failures:
        for f in failures:
            print("  " + f)
        return 1

    dark, light, os_light = b[DARK], b[LIGHT], b[OS_LIGHT]

    missing = sorted(set(dark) - set(light))
    for prop in missing:
        failures.append(f"{prop} has no light value — it falls through to dark")

    extra = sorted(set(light) - set(dark))
    for prop in extra:
        failures.append(f"{prop} is defined for light but not for dark")

    if light != os_light:
        for prop in sorted(set(light) | set(os_light)):
            if light.get(prop) != os_light.get(prop):
                failures.append(
                    f"{prop} differs between the explicit-light and OS-light "
                    f"blocks: {light.get(prop)!r} vs {os_light.get(prop)!r}"
                )

    for name, want in ((DARK, "dark"), (LIGHT, "light"), (OS_LIGHT, "light")):
        if b[name].get("color-scheme") != want:
            failures.append(f"{name}: color-scheme is {b[name].get('color-scheme')!r}, want {want!r}")

    # No panel root may keep a palette of its own.
    core = {p for p in dark if not p.startswith("--consent-")}
    for name in PANEL_ROOTS:
        text = (colorscan.TEMPLATE_DIR / name).read_text()
        if '{% include "_theme.html" %}' not in text:
            failures.append(f"{name} does not include the token partial")
        own = set()
        blanked = colorscan.blank_jinja(text)
        for m in re.finditer(r"<style[^>]*>(.*?)</style>", blanked, re.S | re.I):
            for context, prop, _v, _o in colorscan.parse_css(m.group(1), 0, blanked, name):
                if prop in core:
                    own.add(prop)
        if own:
            failures.append(f"{name} redefines palette tokens of its own: {sorted(own)}")

    print(f"dark tokens        : {len(dark)}")
    print(f"light tokens       : {len(light)}")
    print(f"OS-light tokens    : {len(os_light)}")
    print(f"failures           : {len(failures)}")
    for f in failures:
        print("  " + f)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
