"""The consent page's selected access-level title stays readable (#445).

The selected `.scope-option` card is tinted with `--consent-option-active`
over the card's opaque `.card` surface. Its title used to take
`--consent-primary-text` — the token for text on the Approve button's solid
fill — which is white in both light blocks, so the selected title rendered
white on pale lavender (about 1.2:1). The title now has its own token,
`--consent-option-active-text`, and this module pins, for every theme block in
`_theme.html`, a WCAG contrast ratio of at least 4.5:1 against the composited
background, plus the rule in `authorize.html` that uses it.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_TEMPLATES = Path(__file__).resolve().parent.parent / "src" / "control_panel" / "templates"
_THEME = (_TEMPLATES / "_theme.html").read_text()
_AUTHORIZE = (_TEMPLATES / "authorize.html").read_text()

# The three blocks that define the palette: bare `:root` (dark, canonical),
# the explicit light theme, and the OS-default light block inside
# `@media (prefers-color-scheme: light)`.
_BLOCK_SELECTORS = {
    "dark": r":root",
    "light-explicit": r':root[data-theme="light"]',
    "light-os": r':root:not([data-theme="dark"])',
}

_SELECTED_TITLE = '.scope-option:has(input[type="radio"]:checked) .scope-option-title'
_SELECTED_CARD = '.scope-option:has(input[type="radio"]:checked)'

AA_NORMAL_TEXT = 4.5


def _rule_body(source: str, selector: str) -> str:
    """The declaration list of the first rule whose selector is exactly `selector`."""
    pattern = r"(?:^|[\s}])" + re.escape(selector) + r"\s*\{([^{}]*)\}"
    match = re.search(pattern, source, re.MULTILINE)
    assert match, f"no rule for {selector!r}"
    return match.group(1)


def _declarations(body: str) -> dict[str, str]:
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.DOTALL)
    out: dict[str, str] = {}
    for decl in body.split(";"):
        if ":" in decl:
            name, value = decl.split(":", 1)
            out[name.strip()] = value.strip()
    return out


def _block(name: str) -> dict[str, str]:
    return _declarations(_rule_body(_THEME, _BLOCK_SELECTORS[name]))


def _var_name(value: str) -> str:
    match = re.fullmatch(r"var\((--[\w-]+)\)", value)
    assert match, f"expected a single var(), got {value!r}"
    return match.group(1)


def _hex(value: str) -> tuple[float, float, float]:
    match = re.fullmatch(r"#([0-9a-fA-F]{6})", value)
    assert match, f"expected an opaque #rrggbb colour, got {value!r}"
    h = match.group(1)
    return tuple(float(int(h[i : i + 2], 16)) for i in (0, 2, 4))  # type: ignore[return-value]


def _rgba(value: str) -> tuple[tuple[float, float, float], float]:
    match = re.fullmatch(r"rgba\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*([\d.]+)\s*\)", value)
    assert match, f"expected rgba(), got {value!r}"
    r, g, b, a = match.groups()
    return (float(r), float(g), float(b)), float(a)


def _composite(fg, alpha, bg):
    return tuple(alpha * f + (1 - alpha) * b for f, b in zip(fg, bg))


def _luminance(rgb) -> float:
    def channel(c: float) -> float:
        c /= 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a, b) -> float:
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _surface_token() -> str:
    """The opaque surface beneath a `.scope-option`: the `.card` background.

    Between `.card` and `.scope-option` sit only `.card-body` and the
    `<form>`, neither of which paints a background.
    """
    card = _declarations(_rule_body(_AUTHORIZE, ".card"))
    assert "background" not in _declarations(_rule_body(_AUTHORIZE, ".card-body"))
    return _var_name(card["background"])


def test_selected_title_rule_uses_its_own_token():
    title = _declarations(_rule_body(_AUTHORIZE, _SELECTED_TITLE))
    assert "--consent-primary-text" not in title.get("color", "")
    assert _var_name(title["color"]) == "--consent-option-active-text"


def test_selected_card_is_tinted_with_option_active():
    card = _declarations(_rule_body(_AUTHORIZE, _SELECTED_CARD))
    assert _var_name(card["background"]) == "--consent-option-active"


@pytest.mark.parametrize("block", sorted(_BLOCK_SELECTORS))
def test_selected_title_contrast(block):
    tokens = _block(block)
    title_token = _var_name(_declarations(_rule_body(_AUTHORIZE, _SELECTED_TITLE))["color"])
    surface = _hex(tokens[_surface_token()])
    tint, alpha = _rgba(tokens["--consent-option-active"])
    effective = _composite(tint, alpha, surface)
    title = _hex(tokens[title_token])
    ratio = _contrast(title, effective)
    assert ratio >= AA_NORMAL_TEXT, (
        f"{block}: {title_token} {tokens[title_token]} on "
        f"--consent-option-active over {tokens[_surface_token()]} is {ratio:.2f}:1"
    )


def test_dark_selected_title_is_unchanged():
    """Dark is canonical: the selected title renders exactly as before #445."""
    assert _block("dark")["--consent-option-active-text"] == _block("dark")["--consent-primary-text"]


def test_light_blocks_agree_and_primary_text_keeps_its_values():
    explicit, os_default = _block("light-explicit"), _block("light-os")
    assert explicit == os_default
    assert explicit["--consent-primary-text"] == "#ffffff"
    assert _block("dark")["--consent-primary-text"] == "#e0d9f8"


def test_consent_token_sets_are_in_parity():
    names = [{k for k in _block(b) if k.startswith("--consent-")} for b in _BLOCK_SELECTORS]
    assert names[0] == names[1] == names[2]
