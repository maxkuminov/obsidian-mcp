"""The create-key dialog opens in its first-open state every time (#332).

`[data-modal-open]` used to add `open` and Cancel only removed it, so an
Unlimited box ticked and then cancelled survived into the next open — still
ticked, with the limit field disabled. The dialog now carries
`data-modal-reset`, and the delegated open handler in `panel.js` resets its
forms and re-syncs every Unlimited toggle before showing it.

Two layers:

* the wiring, read from the template and the script (no browser): the create
  dialog opts in, the edit-limit dialog does not (it is filled by script from
  the row, and its behaviour is unchanged);
* the behaviour, by running the real `panel.js` under Node against a minimal
  DOM stand-in that models exactly what the flow touches — `form.reset()`
  restoring default values *without* firing `change` (the browser's
  behaviour, and the reason a re-sync is needed). Skipped when `node` is not
  on PATH.

Manual check (browser, devtools open, zero CSP violations): Keys → New key →
tick Unlimited (limit greys out) → type a name → Cancel → New key again: the
box is unticked, the limit is enabled and shows the server default, the name
is empty. Then Edit on an existing key's limit → it still opens with that
key's own value.
"""
import json
import pathlib
import re
import shutil
import subprocess

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_PANEL_JS = _ROOT / "src" / "control_panel" / "static" / "panel.js"
_KEYS_HTML = _ROOT / "src" / "control_panel" / "templates" / "keys.html"


def _modal_tag(html: str, modal_id: str) -> str:
    match = re.search(rf'<div id="{modal_id}"[^>]*>', html)
    assert match, modal_id
    return match.group(0)


def test_the_create_dialog_opts_in_and_the_limit_dialog_does_not():
    html = _KEYS_HTML.read_text(encoding="utf-8")
    assert "data-modal-reset" in _modal_tag(html, "create-modal")
    assert "data-modal-reset" not in _modal_tag(html, "limit-modal")
    # Opened by the delegated listener, not an inline handler.
    assert 'data-modal-open="create-modal"' in html


def test_the_open_branch_resets_before_showing():
    js = _PANEL_JS.read_text(encoding="utf-8")
    assert "if (target) { resetModal(target); showModal(target); }" in js
    assert "el.hasAttribute('data-modal-reset')" in js
    assert "form.reset();" in js


_HARNESS = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');

function El(attrs, extra) {
    const el = Object.assign({
        nodeType: 1, parentElement: null, attrs: Object.assign({}, attrs),
        classes: new Set(), children: [],
    }, extra || {});
    el.classList = {
        add: (c) => el.classes.add(c), remove: (c) => el.classes.delete(c),
        contains: (c) => el.classes.has(c),
    };
    el.hasAttribute = (n) => Object.prototype.hasOwnProperty.call(el.attrs, n);
    el.getAttribute = (n) => (el.hasAttribute(n) ? el.attrs[n] : null);
    el.closest = (sel) => {
        const name = sel.replace(/^\[|\]$/g, '');
        for (let n = el; n; n = n.parentElement) {
            if (n.hasAttribute(name)) { return n; }
        }
        return null;
    };
    return el;
}
function adopt(parent, child) { child.parentElement = parent; parent.children.push(child); return child; }

const byId = {};
const modal = El({id: 'create-modal', 'data-modal-reset': ''});
modal.classes.add('modal');
const form = adopt(modal, El({}));
const name = adopt(form, El({name: 'name'}, {value: '', defaultValue: ''}));
const limit = adopt(form, El({id: 'create-limit-input'}, {value: '5000', defaultValue: '5000', disabled: false}));
const toggle = adopt(form, El({'data-unlimited-toggle': '', 'data-unlimited-target': 'create-limit-input'},
                              {checked: false, defaultChecked: false}));
const cancel = adopt(form, El({'data-modal-close': 'create-modal'}));
// The browser's reset(): defaults back, no `change` event, `disabled` untouched.
form.reset = () => {
    name.value = name.defaultValue; limit.value = limit.defaultValue;
    toggle.checked = toggle.defaultChecked;
};
modal.querySelectorAll = (sel) => (sel === 'form' ? [form] : sel === '[data-unlimited-toggle]' ? [toggle] : []);
const openBtn = El({'data-modal-open': 'create-modal'});
byId['create-modal'] = modal; byId['create-limit-input'] = limit;

const listeners = {};
const document = {
    addEventListener: (t, f) => { (listeners[t] = listeners[t] || []).push(f); },
    getElementById: (id) => byId[id] || null,
    querySelector: () => null,
};
function fire(type, target) { (listeners[type] || []).forEach((f) => f({target})); }

new Function('document', 'window', 'navigator', src)(document, {}, {});

const seen = {};
fire('click', openBtn);
seen.first = {open: modal.classList.contains('open'), checked: toggle.checked, disabled: limit.disabled, value: limit.value};
toggle.checked = true; fire('change', toggle);
name.value = 'scratch'; limit.value = '42';
seen.ticked = {disabled: limit.disabled};
fire('click', cancel);
seen.cancelled = {open: modal.classList.contains('open')};
fire('click', openBtn);
seen.reopened = {open: modal.classList.contains('open'), checked: toggle.checked,
                 disabled: limit.disabled, value: limit.value, name: name.value};
process.stdout.write(JSON.stringify(seen));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not on PATH")
def test_reopening_after_cancel_shows_the_first_open_state(tmp_path):
    harness = tmp_path / "harness.js"
    harness.write_text(_HARNESS, encoding="utf-8")
    out = subprocess.run(
        ["node", str(harness), str(_PANEL_JS)],
        capture_output=True, text=True, timeout=30, check=True,
    ).stdout
    seen = json.loads(out)

    assert seen["first"] == {"open": True, "checked": False, "disabled": False, "value": "5000"}
    assert seen["ticked"] == {"disabled": True}
    assert seen["cancelled"] == {"open": False}
    assert seen["reopened"] == {
        "open": True, "checked": False, "disabled": False, "value": "5000", "name": "",
    }
