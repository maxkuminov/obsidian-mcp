## Why

PR #62 fixed a real disclosure bug on the OAuth consent screen: a client
requesting `scope=readwrite` still rendered "Read only" preselected, so the
user approved and silently received a read-only grant with no explanation of
why. The fix bound the preselected radio to the requested scope.

The side effect (#63, found by an adversarial audit of that PR) is that **the
checked radio is the value the form submits**, so the preselect is not a
display detail — it is the default grant. `/register` is unauthenticated
(rate-limited 3/min and nothing else) and a DCR client that omits `scope` is
registered for `read readwrite offline_access`, so any party can register a
write-capable client with a trusted-looking `client_name`, start a PKCE flow
with `scope=readwrite`, and have the vault owner grant vault-wide write with a
single unchanged **Approve** click.

The enforcement boundary is unchanged and still correct — `_clamp_scope` in
`authorize_post` still refuses to widen a grant past the client's registered
scope. This is about the removed fail-safe *default*: the consumer is an
autonomous agent and the vault is the owner's single source of truth, so an
unintended write grant is the expensive failure.

#65 (gap 3) is folded in because it constrains the same tests: three of the
four consent tests shipped with #62 pass against the pre-#62 tree, and the
fourth is mislabeled — it claims to exercise the `scope: str = Query("read")`
default but passes `scope` explicitly.

## What Changes

- **Preselect is a constant, not a function of the request.** "Read only" is
  checked unconditionally in `authorize.html`; the write radio is never
  checked. Write costs a deliberate click, regardless of the requested scope
  and regardless of the client's registered scope.
- **Disclosure moves to prose.** `authorize_get` passes `requested_write`
  (ungated by the registered scope, display only) and `write_unavailable`, and
  the request box names the level the client asked for — "<client> is
  requesting **Read + Write** / **Read only** access" — plus, when the client
  asked for write it is not registered for, that write is not available to it.
  A write-capable client also gets a line saying Read only is preselected and
  write is granted only if selected.
- **The selected level is visibly marked.** `.scope-option` gains a
  `:has(input[type="radio"]:checked)` highlight, kept in standalone rule
  blocks so a browser without `:has()` drops only those rules and falls back to
  the native `accent-color` radio dot.
- **Consent tests pin behaviour.** `tests/test_authorize_get_scope_preselect.py`
  is rewritten to drive `/authorize` through a real `TestClient` (so the
  `Query("read")` default is genuinely exercised by omitting the parameter) and
  every behavioural test in it fails against the pre-change tree.

## Impact

- `src/oauth/routes.py` — `authorize_get` template context only. No change to
  `authorize_post`, `_clamp_scope`, `_handle_auth_code` or `_handle_refresh`.
- `src/control_panel/templates/authorize.html` — request box, radio `checked`
  attributes, two CSS rules.
- `tests/test_authorize_get_scope_preselect.py` — rewritten.
- No database, migration, config or API-surface change.

Out of scope: #65 gaps 1 and 2 (`oauth_page`'s `has_write` derivation and the
tautological assertion in `tests/test_oauth_panel_scope_display.py`), which
belong with the shared scope-helper work on the panel/auth side.
