"""Color-declaration scanner for the panel templates.

Shared by the three gates of change `panel-light-mode`:

  * ``manifest.py``  — records the pre-sweep baseline (task 1.0)
  * ``replay.py``    — proves the post-sweep templates resolve, under the dark
                       theme, to exactly the baseline literals (task 4.1)
  * ``literal_sweep.py`` — proves no color literal survives outside token
                       definitions (task 3.2)

Scan scope is normative in `specs/panel-theming/spec.md`:

  CSS declarations in ``<style>`` blocks, ``style=""`` attributes, SVG
  ``fill``/``stroke`` presentation attributes, Chart.js color options in
  template JavaScript, and ``<meta name="theme-color">``.

Permitted non-token values: the semantic keywords ``currentColor``,
``transparent`` and ``inherit``; colors inside data-URI images; vendored
assets under ``static/vendor/`` (never scanned).

Entry identity
--------------
Entries are keyed by ``(template, kind, context, property, ordinal)`` and NOT
by line number, so the key survives insertions elsewhere in the file.  The
context of a ``style=""`` / SVG entry is a hash of its enclosing start tag with
every color-bearing attribute removed, so the identity survives a color moving
from an SVG presentation attribute into ``style=""``.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, asdict
from pathlib import Path

TEMPLATE_DIR = Path(__file__).resolve().parents[4] / "src" / "control_panel" / "templates"

# Panel/auth surface: one shared token partial, one palette.
PANEL_TEMPLATES = [
    "_theme.html",
    "_theme_toggle.html",
    "base.html",
    "auth_base.html",
    "authorize.html",
    "dashboard.html",
    "keys.html",
    "login.html",
    "oauth.html",
    "reembed_confirm.html",
    "register.html",
    "settings.html",
    "usage.html",
    "user_edit.html",
    "users.html",
    "vault.html",
]

# Transfer surface: distinct, locally tokenized, OS-responsive, no toggle.
TRANSFER_TEMPLATES = ["transfer_upload.html", "transfer_download.html"]

ALL_TEMPLATES = PANEL_TEMPLATES + TRANSFER_TEMPLATES

SEMANTIC_KEYWORDS = {"currentcolor", "transparent", "inherit"}

# Properties whose value may name a color with a bare CSS keyword.  Hex and
# rgb()/hsl() forms are detected in any property.
COLOR_PROPERTIES = {
    "color", "background", "background-color", "background-image",
    "border", "border-color", "border-top", "border-right", "border-bottom",
    "border-left", "border-top-color", "border-right-color",
    "border-bottom-color", "border-left-color", "outline", "outline-color",
    "box-shadow", "text-shadow", "fill", "stroke", "accent-color",
    "caret-color", "text-decoration-color", "column-rule-color",
    "-webkit-text-fill-color", "filter", "scrollbar-color",
}

# Chart.js option keys that carry a color.
CHART_COLOR_KEYS = {
    "backgroundColor", "borderColor", "color", "titleColor", "bodyColor",
    "footerColor", "hoverBackgroundColor", "hoverBorderColor", "pointBorderColor",
    "pointBackgroundColor", "gridColor", "tickColor",
}

# The CSS named colors that actually occur (or plausibly could) in this
# codebase.  Deliberately narrow: a full 148-name list makes `none`-adjacent
# words like `linen` fire on unrelated values.
NAMED_COLORS = {
    "white", "black", "red", "green", "blue", "yellow", "orange", "purple",
    "gray", "grey", "silver", "maroon", "navy", "teal", "olive", "lime",
    "aqua", "fuchsia", "crimson", "gold", "ivory", "khaki", "salmon",
    "tomato", "violet", "indigo", "coral", "beige", "brown", "cyan",
    "magenta", "pink", "plum", "tan", "wheat", "azure", "orchid", "sienna",
    "snow", "thistle", "turquoise",
}

HEX_RE = re.compile(r"#[0-9a-fA-F]{3,8}\b")
FUNC_RE = re.compile(r"\b(?:rgba?|hsla?|hwb|lab|lch|oklab|oklch|color)\s*\(", re.I)
VAR_RE = re.compile(r"var\(\s*(--[A-Za-z0-9_-]+)\s*(?:,([^()]*(?:\([^()]*\)[^()]*)*))?\)")
TOK_RE = re.compile(r"""tok\(\s*['"](--[A-Za-z0-9_-]+)['"]\s*\)""")
JINJA_RE = re.compile(r"\{%.*?%\}|\{\{.*?\}\}|\{#.*?#\}", re.S)
DATA_URI_RE = re.compile(r"""url\(\s*(['"]?)data:[^)]*?\1\s*\)""", re.S)


def blank_jinja(text: str) -> str:
    """Replace Jinja tags with same-length filler so offsets/lines survive."""

    def repl(m: re.Match) -> str:
        return "".join("\n" if c == "\n" else " " for c in m.group(0))

    return JINJA_RE.sub(repl, text)


def _blank_data_uris(value: str) -> str:
    """Blank out data-URI payloads: colors inside them are a spec exception."""

    def repl(m: re.Match) -> str:
        return "".join("\n" if c == "\n" else " " for c in m.group(0))

    return DATA_URI_RE.sub(repl, value)


@dataclass
class Entry:
    template: str
    kind: str          # css | style-attr | svg-attr | js | meta
    context: str       # selector path / tag hash / chart key
    prop: str
    ordinal: int
    value: str         # the declaration value, verbatim
    literals: list      # color literals found in it (empty for keyword-only)
    keywords: list      # semantic keywords found in it
    line: int

    @property
    def key(self) -> str:
        return f"{self.template}|{self.kind}|{self.context}|{self.prop}|{self.ordinal}"


def normalize(value: str) -> str:
    """Whitespace-insensitive, case-insensitive comparison form."""
    return re.sub(r"\s+", "", value).lower()


def find_literals(prop: str, value: str) -> tuple[list, list]:
    """Return (color literals, semantic keywords) in a declaration value.

    A color function whose arguments are entirely literal counts as a literal;
    one that is empty of literals (all `var()`) does not.  Colors inside
    data-URI payloads are excluded per the spec's exception list.
    """
    scan = _blank_data_uris(value)
    literals: list = []

    literals += HEX_RE.findall(scan)

    for m in FUNC_RE.finditer(scan):
        start = m.end() - 1
        depth = 0
        for i in range(start, len(scan)):
            if scan[i] == "(":
                depth += 1
            elif scan[i] == ")":
                depth -= 1
                if depth == 0:
                    call = scan[m.start():i + 1]
                    # `rgb(var(--x) / 40%)` etc. is token-derived, not literal.
                    if "var(" not in call and re.search(r"\d", call):
                        literals.append(call)
                    break

    if prop in COLOR_PROPERTIES or prop.startswith("--"):
        for word in re.findall(r"[A-Za-z][A-Za-z-]*", scan):
            if word.lower() in NAMED_COLORS:
                literals.append(word)

    keywords = [w for w in re.findall(r"[A-Za-z][A-Za-z-]*", scan)
                if w.lower() in SEMANTIC_KEYWORDS]
    return literals, keywords


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


# Attributes that may carry a color.  They are removed *entirely* — name and
# value — before a start tag is hashed, so one identity covers a declaration
# whether it rides in an SVG presentation attribute or in `style=""`.  That
# matters: `var()` is not reliably substituted in SVG presentation attributes
# (SVG2 parses them with the property's own grammar, not as CSS declarations),
# so the sweep moves those colors into `style=""`, which is real CSS.
COLOR_ATTRS = ("style", "fill", "stroke")


def _tag_context(text: str, attr_start: int, extra: str = "") -> str:
    """Stable identity for a start tag: its bytes minus color-bearing attrs.

    `extra` carries the tag's non-color inline declarations.  A bare
    `<div style="...">` is otherwise indistinguishable from every other bare
    `<div style="...">` in the file, and layout declarations (padding,
    font-size, flex) are both untouched by the sweep and highly
    discriminating.
    """
    open_idx = text.rfind("<", 0, attr_start)
    close_idx = text.find(">", attr_start)
    tag = text[open_idx:close_idx + 1] if open_idx >= 0 and close_idx > 0 else text[attr_start:attr_start + 80]
    for name in COLOR_ATTRS:
        tag = re.sub(rf'\b{name}\s*=\s*"[^"]*"', " ", tag, flags=re.S)
        tag = re.sub(rf"\b{name}\s*=\s*'[^']*'", " ", tag, flags=re.S)
    tag = re.sub(r"\s+", " ", tag).strip()
    tag = re.sub(r"\s+(?=/?>)", "", tag)      # the stripped attr may have been last
    return hashlib.sha256((tag + "|" + extra).encode()).hexdigest()[:12]


def _noncolor_fingerprint(style_body: str) -> str:
    """The declarations of a style="" attribute that the sweep never rewrites."""
    parts = []
    for decl, _ in _split_declarations(style_body, 0):
        parsed = _parse_decl(decl)
        if not parsed:
            continue
        prop, value = parsed
        if prop in COLOR_PROPERTIES or prop.startswith("--"):
            continue
        parts.append(f"{prop}:{normalize(value)}")
    return ";".join(parts)


# --------------------------------------------------------------------------
# CSS parsing
# --------------------------------------------------------------------------

def _split_declarations(block: str, base_offset: int):
    """Yield (prop, value, offset) for each `prop: value` in a block body."""
    buf, i, n = [], 0, len(block)
    depth, quote, start = 0, None, 0
    while i < n:
        c = block[i]
        if quote:
            buf.append(c)
            if c == quote:
                quote = None
            i += 1
            continue
        if c in "\"'":
            quote = c
            buf.append(c)
        elif c == "(":
            depth += 1
            buf.append(c)
        elif c == ")":
            depth -= 1
            buf.append(c)
        elif c == ";" and depth == 0:
            decl = "".join(buf)
            if ":" in decl:
                yield decl, base_offset + start
            buf, start = [], i + 1
        else:
            buf.append(c)
        i += 1
    decl = "".join(buf)
    if ":" in decl.strip():
        yield decl, base_offset + start


def _parse_decl(decl: str):
    stripped = decl.strip()
    if not stripped or stripped.startswith("/*"):
        return None
    stripped = re.sub(r"/\*.*?\*/", " ", stripped, flags=re.S).strip()
    if ":" not in stripped:
        return None
    prop, value = stripped.split(":", 1)
    prop = prop.strip().lower()
    if not re.fullmatch(r"--[A-Za-z0-9_-]+|-?[A-Za-z][A-Za-z0-9-]*", prop):
        return None
    return prop, value.strip()


def parse_css(css: str, base_offset: int, full_text: str, template: str,
              context_prefix: str = "") -> list:
    """Parse a CSS block into declaration records with selector paths."""
    css_nc = re.sub(r"/\*.*?\*/", lambda m: "".join("\n" if c == "\n" else " " for c in m.group(0)), css, flags=re.S)
    out = []
    stack: list = []
    buf, i, n = [], 0, len(css_nc)
    depth_paren, quote = 0, None
    block_start = 0
    while i < n:
        c = css_nc[i]
        if quote:
            buf.append(c)
            if c == quote:
                quote = None
            i += 1
            continue
        if c in "\"'":
            quote = c
            buf.append(c)
        elif c == "(":
            depth_paren += 1
            buf.append(c)
        elif c == ")":
            depth_paren -= 1
            buf.append(c)
        elif c == "{" and depth_paren == 0:
            prelude = re.sub(r"\s+", " ", "".join(buf)).strip()
            stack.append(prelude)
            buf, block_start = [], i + 1
        elif c == "}" and depth_paren == 0:
            body = "".join(buf)
            if stack:
                path = " > ".join(stack)
                for decl, off in _split_declarations(body, base_offset + block_start):
                    parsed = _parse_decl(decl)
                    if parsed:
                        out.append((context_prefix + path, parsed[0], parsed[1], off))
                stack.pop()
            buf, block_start = [], i + 1
        else:
            buf.append(c)
        i += 1
    return out


def scan_template(path: Path, record_all: bool = False) -> list:
    """Return every in-scope color declaration of one template.

    ``record_all`` also records declarations that carry no literal but do
    reference a token (``var()`` / ``tok()``).  The baseline manifest is built
    without it — the spec defines the baseline as the *literal-bearing*
    declarations — while the replay check needs it, because after the sweep
    those very declarations hold nothing but a ``var()``.
    """
    raw = path.read_text()
    text = blank_jinja(raw)
    template = path.name
    entries: list = []
    counters: dict = {}

    def add(kind, context, prop, value, offset):
        lits, kws = find_literals(prop, value)
        if not lits and not kws:
            if not record_all:
                return
            if "var(" not in value and "tok(" not in value:
                return
        k = (kind, context, prop)
        ordinal = counters.get(k, 0)
        counters[k] = ordinal + 1
        entries.append(Entry(
            template=template, kind=kind, context=context, prop=prop,
            ordinal=ordinal, value=re.sub(r"\s+", " ", value).strip(),
            literals=lits, keywords=kws, line=_line_of(raw, offset),
        ))

    # 1. <style> blocks
    for m in re.finditer(r"<style[^>]*>(.*?)</style>", text, re.S | re.I):
        for context, prop, value, off in parse_css(m.group(1), m.start(1), text, template):
            add("css", context, prop, value, off)

    # 2. style="" attributes
    for m in re.finditer(r'style\s*=\s*"([^"]*)"', text, re.S):
        context = _tag_context(text, m.start(), _noncolor_fingerprint(m.group(1)))
        for decl, off in _split_declarations(m.group(1), m.start(1)):
            parsed = _parse_decl(decl)
            if parsed:
                add("style-attr", context, parsed[0], parsed[1], off)

    # 3. SVG fill/stroke presentation attributes
    for m in re.finditer(r'\b(fill|stroke)\s*=\s*"([^"]*)"', text):
        context = _tag_context(text, m.start())
        add("svg-attr", context, m.group(1), m.group(2), m.start(2))

    # 4. Chart.js color options in <script> blocks
    for sm in re.finditer(r"<script[^>]*>(.*?)</script>", text, re.S | re.I):
        body, base = sm.group(1), sm.start(1)
        for m in re.finditer(
            r"\b([A-Za-z][A-Za-z0-9]*)\s*:\s*("
            r"""['"][^'"]*['"]"""          # quoted literal
            r"|tok\([^)]*\)"                # token lookup helper
            r")", body,
        ):
            key = m.group(1)
            if key not in CHART_COLOR_KEYS:
                continue
            value = m.group(2)
            if value[0] in "\"'":
                value = value[1:-1]
            add("js", "chart", key, value, base + m.start(2))

    # 5. <meta name="theme-color">
    for m in re.finditer(r'<meta[^>]*name\s*=\s*"theme-color"[^>]*>', text, re.I):
        tag = m.group(0)
        content = re.search(r'content\s*=\s*"([^"]*)"', tag)
        token = re.search(r'data-theme-color-token\s*=\s*"([^"]*)"', tag)
        value = content.group(1).strip() if content else ""
        if token:
            # The tag is token-bound: the bootstrap stamps `content` from this
            # token on every load, and the static value is only the no-JS
            # default. It is therefore not a stray literal — but it could still
            # drift, so `token_coverage.py` pins it to the token's dark value.
            value = f"var({token.group(1)})"
        add("meta", "head", "theme-color", value, m.start())

    return entries


def scan_all(templates=None, record_all: bool = False) -> list:
    out = []
    for name in (templates or ALL_TEMPLATES):
        p = TEMPLATE_DIR / name
        if p.exists():
            out.extend(scan_template(p, record_all=record_all))
    return out


# --------------------------------------------------------------------------
# Token tables and resolution
# --------------------------------------------------------------------------

def token_tables(entries: list) -> dict:
    """Build {(template, media, selector): {token: value}} from entries."""
    tables: dict = {}
    for e in entries:
        if e.kind != "css" or not e.prop.startswith("--"):
            continue
        parts = [p.strip() for p in e.context.split(" > ")]
        media = " > ".join(p for p in parts if p.startswith("@"))
        selector = parts[-1] if parts else ""
        tables.setdefault((e.template, media, selector), {})[e.prop] = e.value
    return tables


def dark_env(entries: list, template: str) -> dict:
    """Token values in force for `template` under the dark/default theme.

    Panel templates read the shared partial's bare ``:root``.  Transfer
    templates are OS-responsive: their own bare ``:root`` is the base, with the
    ``prefers-color-scheme: dark`` block layered on top when an entry lives
    there (handled by ``resolve``'s ``media`` argument).
    """
    tables = token_tables(entries)
    env: dict = {}
    sources = ["_theme.html", template] if template in PANEL_TEMPLATES else [template]
    for src in sources:
        for (tpl, media, selector), toks in tables.items():
            if tpl == src and media == "" and selector in (":root", "html", ":root, html"):
                env.update(toks)
    return env


def media_env(entries: list, template: str, media: str) -> dict:
    tables = token_tables(entries)
    env: dict = {}
    for (tpl, m, selector), toks in tables.items():
        if tpl == template and m == media and selector in (":root", "html", ":root, html"):
            env.update(toks)
    return env


class Unresolved(Exception):
    pass


def resolve(value: str, env: dict, depth: int = 0) -> str:
    """Expand every var()/tok() reference against `env`."""
    if depth > 20:
        raise Unresolved(f"var() cycle in {value!r}")
    out = TOK_RE.sub(lambda m: f"var({m.group(1)})", value)

    def repl(m: re.Match) -> str:
        name, fallback = m.group(1), m.group(2)
        if name in env:
            return env[name]
        if fallback is not None:
            return fallback.strip()
        raise Unresolved(f"undefined token {name}")

    prev = None
    while prev != out and VAR_RE.search(out):
        prev = out
        out = VAR_RE.sub(repl, out)
        depth += 1
        if depth > 20:
            raise Unresolved(f"var() cycle in {value!r}")
    return out


def load(path: Path) -> list:
    data = json.loads(path.read_text())
    return [Entry(**d) for d in data["entries"]]


def dump(entries: list, path: Path, note: str) -> None:
    path.write_text(json.dumps(
        {"note": note, "count": len(entries),
         "entries": [asdict(e) for e in entries]},
        indent=1, sort_keys=True) + "\n")
