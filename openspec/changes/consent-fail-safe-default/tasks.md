## 1. Fail-safe preselect (#63)

- [x] 1.1 `authorize.html`: hardcode `checked` on the read-only radio and drop `checked` from the read + write radio, with a comment naming why the preselect must not follow the requested scope
- [x] 1.2 `src/oauth/routes.py::authorize_get`: compute `requested_write` from the validated scope alone (no `client_can_write` gate) and add `write_unavailable`; document that both are display-only

## 2. Disclosure (#63)

- [x] 2.1 `authorize.html`: request box names the requested level — "<client> is requesting Read + Write / Read only access to your Obsidian vault"
- [x] 2.2 `authorize.html`: when `write_unavailable`, say read + write is not available to this client
- [x] 2.3 `authorize.html`: when `client_can_write`, say read only is preselected and read + write is granted only if selected
- [x] 2.4 `authorize.html`: `.request-note` / `.request-note.warn` styles for the two notices

## 3. Selected-level highlight (#63)

- [x] 3.1 `authorize.html`: `.scope-option:has(input[type="radio"]:checked)` border/background rule, in its own block
- [x] 3.2 `authorize.html`: matching `.scope-option-title` rule, also standalone; comment recording that a browser without `:has()` falls back to the native `accent-color` radio dot

## 4. Tests that pin it (#63, #65 gap 3)

- [x] 4.1 Rewrite `tests/test_authorize_get_scope_preselect.py` to drive `/authorize` through `TestClient` on a bare app so query defaults resolve the way FastAPI resolves them
- [x] 4.2 `test_default_query_scope_is_read` omits `scope` entirely, exercising the `Query("read")` default the old test claimed to cover
- [x] 4.3 `test_readwrite_request_still_preselects_read_radio` — the #63 property
- [x] 4.4 `test_readwrite_request_is_disclosed_even_though_read_is_preselected` — the "requesting Read + Write" notice on a write-capable client
- [x] 4.5 `test_only_the_read_radio_is_ever_checked` — whole-form property, so a future third level cannot ship pre-checked
- [x] 4.6 `test_readonly_client_is_told_write_is_not_available` and its negative complement
- [x] 4.7 `test_selected_scope_option_is_visibly_highlighted` — asserts the `:has()` rules exist *and* are not grouped with other selectors
- [x] 4.8 Verify by stashing `src/`: every behavioural test in the module fails against the pre-change tree
- [x] 4.9 Full suite green (`pytest -q --ignore=tests/integration`)

## 5. Spec

- [x] 5.1 Spec deltas under `specs/oauth-authorization-integrity/`
- [x] 5.2 `openspec validate consent-fail-safe-default --strict` passes
