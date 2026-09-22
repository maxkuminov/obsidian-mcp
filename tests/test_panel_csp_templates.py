"""panel-csp (#195) — the templates are written to survive a nonce-only policy.

Static checks over every file in `src/control_panel/templates/` and over
`panel.js`; no app, no database. The header itself is tested through the real
middleware stack in `tests/test_panel_csp_headers.py`; this module holds the
markup to the contract that header relies on (design D1, D2, D6, D9):

- no inline event handler, no `javascript:` URL, no `hx-` attribute;
- every `<script>` and `<style>` carries the response nonce;
- every script is same-origin under `/admin/static/`, every stylesheet link is
  Google Fonts;
- the eight destructive confirmations fail closed: a `type="button"` with
  `data-confirm`, in a form with no other submit control;
- `panel.js` never evaluates or renders markup from an attribute value.

Jinja (`{# … #}`) and HTML (`<!-- … -->`) comments are stripped first, with
their newlines kept so a failure still names the right line.
"""
import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_TEMPLATES = _ROOT / "src" / "control_panel" / "templates"
_STATIC = _ROOT / "src" / "control_panel" / "static"
_PANEL_JS = _STATIC / "panel.js"

_TRANSFER = {"transfer_upload.html", "transfer_download.html"}
_PANEL_NONCE = 'nonce="{{ csp_nonce }}"'
_TRANSFER_NONCE = 'nonce="{{ nonce }}"'

_TEMPLATE_FILES = sorted(_TEMPLATES.glob("*.html"))

# An opening tag, tolerant of `>` inside quoted attribute values.
_TAG = re.compile(r"<([a-zA-Z][a-zA-Z0-9-]*)((?:[^>\"']|\"[^\"]*\"|'[^']*')*)>")


def _blank(match: re.Match) -> str:
    """Replace a match with whitespace of the same shape, keeping newlines."""
    return re.sub(r"[^\n]", " ", match.group(0))


def _strip_comments(text: str) -> str:
    text = re.sub(r"\{#.*?#\}", _blank, text, flags=re.S)
    return re.sub(r"<!--.*?-->", _blank, text, flags=re.S)


def _strip_element_bodies(text: str) -> str:
    """Blank the bodies of `<script>` and `<style>` elements.

    Attribute checks look at markup only; JavaScript such as
    `var onSystemChange = …` is not an HTML attribute.
    """
    def body(m: re.Match) -> str:
        return m.group(1) + re.sub(r"[^\n]", " ", m.group(2)) + m.group(3)

    return re.sub(
        r"(?is)(<(?:script|style)\b[^>]*>)(.*?)(</(?:script|style)\s*>)", body, text
    )


def _line(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _source(path: Path) -> str:
    return _strip_comments(path.read_text())


def _markup(path: Path) -> str:
    return _strip_element_bodies(_source(path))


def _ids(paths):
    return [p.name for p in paths]


def test_the_template_set_is_not_empty():
    # A moved directory must not turn every check below into a vacuous pass.
    assert len(_TEMPLATE_FILES) >= 20


@pytest.mark.parametrize("path", _TEMPLATE_FILES, ids=_ids(_TEMPLATE_FILES))
def test_no_inline_event_handler(path):
    text = _markup(path)
    offenders = [
        f"{path.name}:{_line(text, m.start())}: {m.group(0).strip()}"
        for m in re.finditer(r"\son[a-z]+\s*=", text, flags=re.I)
    ]
    assert offenders == []


@pytest.mark.parametrize("path", _TEMPLATE_FILES, ids=_ids(_TEMPLATE_FILES))
def test_no_javascript_url(path):
    text = _markup(path)
    offenders = [
        f"{path.name}:{_line(text, m.start())}: {m.group(0).strip()}"
        for m in re.finditer(
            r"\b(?:href|src|action|formaction)\s*=\s*[\"']?\s*javascript:", text, flags=re.I
        )
    ]
    assert offenders == []


@pytest.mark.parametrize("path", _TEMPLATE_FILES, ids=_ids(_TEMPLATE_FILES))
def test_no_htmx_attribute_or_reference(path):
    text = _markup(path)
    offenders = [
        f"{path.name}:{_line(text, m.start())}: {m.group(0).strip()}"
        for m in re.finditer(r"\s(?:data-)?hx-[a-z]", text, flags=re.I)
    ]
    source = _source(path)
    offenders += [
        f"{path.name}:{_line(source, m.start())}: references htmx"
        for m in re.finditer(r"htmx", source, flags=re.I)
    ]
    assert offenders == []


@pytest.mark.parametrize("path", _TEMPLATE_FILES, ids=_ids(_TEMPLATE_FILES))
def test_every_script_and_style_carries_the_nonce(path):
    expected = _TRANSFER_NONCE if path.name in _TRANSFER else _PANEL_NONCE
    text = _source(path)
    offenders = [
        f"{path.name}:{_line(text, m.start())}: {m.group(0)}"
        for m in _TAG.finditer(text)
        if m.group(1).lower() in ("script", "style") and expected not in m.group(2)
    ]
    assert offenders == []


@pytest.mark.parametrize("path", _TEMPLATE_FILES, ids=_ids(_TEMPLATE_FILES))
def test_every_script_src_is_same_origin_static(path):
    text = _source(path)
    offenders = []
    for m in _TAG.finditer(text):
        if m.group(1).lower() != "script":
            continue
        src = re.search(r"\bsrc\s*=\s*[\"']([^\"']*)[\"']", m.group(2), flags=re.I)
        if src and not src.group(1).startswith("/admin/static/"):
            offenders.append(f"{path.name}:{_line(text, m.start())}: {src.group(1)}")
    assert offenders == []


@pytest.mark.parametrize("path", _TEMPLATE_FILES, ids=_ids(_TEMPLATE_FILES))
def test_every_stylesheet_link_is_google_fonts(path):
    text = _source(path)
    offenders = []
    for m in _TAG.finditer(text):
        if m.group(1).lower() != "link":
            continue
        attrs = m.group(2)
        if not re.search(r"\brel\s*=\s*[\"']?[^\"'>]*stylesheet", attrs, flags=re.I):
            continue
        href = re.search(r"\bhref\s*=\s*[\"']([^\"']*)[\"']", attrs, flags=re.I)
        if not href or not href.group(1).startswith("https://fonts.googleapis.com/"):
            offenders.append(f"{path.name}:{_line(text, m.start())}: {m.group(0)}")
    assert offenders == []


# --- D2: the eight confirm() controls fail closed ---------------------------

# Where the eight pre-change `confirm()` controls live (spec: "The eight
# existing confirm() controls MUST fail closed"). A ninth needs a deliberate
# edit here; so does losing one.
_EXPECTED_CONFIRMS = {"keys.html": 3, "oauth.html": 2, "settings.html": 1, "user_edit.html": 2}


def _type_of(attrs: str) -> str | None:
    t = re.search(r"\btype\s*=\s*[\"']?([a-zA-Z]+)", attrs, flags=re.I)
    return t.group(1).lower() if t else None


def _submit_controls(form_body: str):
    """Tags in a form body that would submit it: `type="submit"`, an image
    input, or a `<button>` without a type (whose default is submit)."""
    for m in _TAG.finditer(form_body):
        name, attrs = m.group(1).lower(), m.group(2)
        kind = _type_of(attrs)
        if kind in ("submit", "image"):
            yield m
        elif name == "button" and kind is None:
            yield m


def test_the_eight_confirm_controls_are_where_the_spec_says():
    counts = {}
    for path in _TEMPLATE_FILES:
        n = len(re.findall(r"\sdata-confirm\s*=", _markup(path)))
        if n:
            counts[path.name] = n
    assert counts == _EXPECTED_CONFIRMS


@pytest.mark.parametrize("path", _TEMPLATE_FILES, ids=_ids(_TEMPLATE_FILES))
def test_confirm_controls_are_buttons_in_forms_with_no_other_submit(path):
    text = _markup(path)
    offenders = []
    forms = [
        (m.start(), m.end(), m.group(0))
        for m in re.finditer(r"(?is)<form\b.*?</form\s*>", text)
    ]
    for m in _TAG.finditer(text):
        if not re.search(r"\sdata-confirm\s*=", m.group(2)):
            continue
        where = f"{path.name}:{_line(text, m.start())}"
        if m.group(1).lower() != "button" or _type_of(m.group(2)) != "button":
            offenders.append(f"{where}: data-confirm must be on a <button type=\"button\">")
        enclosing = [f for f in forms if f[0] <= m.start() < f[1]]
        if not enclosing:
            offenders.append(f"{where}: data-confirm control is not inside a <form>")
            continue
        start, _, body = enclosing[0]
        for s in _submit_controls(body):
            offenders.append(
                f"{path.name}:{_line(text, start + s.start())}: "
                f"submit control in a confirm-guarded form: {s.group(0)}"
            )
    assert offenders == []


def test_the_other_confirmation_mechanisms_keep_native_submits():
    # D2: the reset-embeddings modal and the re-embed page are their own
    # confirmation step and keep a real submit button.
    settings = _markup(_TEMPLATES / "settings.html")
    reset = re.search(r'(?is)<form[^>]*action="/admin/settings/reset-embeddings".*?</form>', settings)
    assert reset and 'type="submit"' in reset.group(0)
    assert "data-confirm" not in reset.group(0)

    reembed = _markup(_TEMPLATES / "reembed_confirm.html")
    form = re.search(r'(?is)<form[^>]*action="/admin/settings/reembed".*?</form>', reembed)
    assert form and 'type="submit"' in form.group(0)
    assert "data-confirm" not in reembed


def test_vault_hover_is_css():
    text = _source(_TEMPLATES / "vault.html")
    assert ".vault-crumb:hover" in text
    assert ".vault-link:hover" in text


# --- D1/D6: the client script -----------------------------------------------


def test_panel_js_evaluates_and_renders_nothing():
    js = _PANEL_JS.read_text()
    for needle in ("eval(", "Function(", "innerHTML", "insertAdjacentHTML", "document.write"):
        assert needle not in js, f"panel.js contains {needle}"


def test_panel_js_is_loaded_by_base_with_the_nonce():
    base = _source(_TEMPLATES / "base.html")
    assert '<script src="/admin/static/panel.js" nonce="{{ csp_nonce }}" defer></script>' in base


@pytest.mark.parametrize(
    "attribute",
    [
        "data-confirm",
        "data-modal-open",
        "data-modal-close",
        "data-modal-backdrop",
        "data-autosubmit",
        "data-copy-from",
        "data-limit-edit",
        "data-key-id",
        "data-limit",
        "data-sidebar-open",
        "data-sidebar-close",
        "data-async-reindex",
    ],
)
def test_every_behaviour_attribute_has_a_listener_and_a_user(attribute):
    # Seam check: a template attribute with no listener is a dead control,
    # a listener with no template user is dead code.
    assert attribute in _PANEL_JS.read_text()
    assert any(
        re.search(rf"\s{re.escape(attribute)}(?:\s*=|[\s>])", _markup(p)) for p in _TEMPLATE_FILES
    ), f"no template uses {attribute}"


def test_the_theme_toggle_listener_lives_in_the_bootstrap():
    theme = _source(_TEMPLATES / "_theme.html")
    assert "[data-theme-toggle]" in theme
    assert "addEventListener('click'" in theme
    assert "data-theme-toggle" in (_TEMPLATES / "_theme_toggle.html").read_text()


def test_htmx_is_not_vendored():
    assert not list(_STATIC.rglob("htmx*"))
