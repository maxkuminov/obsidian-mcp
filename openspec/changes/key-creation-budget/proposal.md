Refs #323

## Why

ASVS V2.4.1 (#323, medium). API keys can be created through two routes that do the same thing: `POST /api/keys` (JSON) and `POST /admin/keys/create` (the panel form). Only the JSON route has a limiter, a 5/min per-address slowapi decorator. The form route has none, so anyone with a panel session and its CSRF token can script it and create keys without limit.

The extra keys are not only extra rows. Every new key is a new MCP principal, and a new principal starts with full general (30) and write (15) bursts. A form submitted with the limit field blank creates a key with **no daily quota**. Creating keys repeatedly therefore resets both per-principal velocity buckets and gets around the daily quota. Those are the two controls `mcp-rate-limits` and `usage-quotas` set up to bound one credential's work.

The documented rule "an explicit null, or a blank panel field, means unlimited" (CLAUDE.md, `usage-quotas`, `panel-usage-slicing`) is the reason a scripted blank is a way to bypass the quota. The owner has decided to change it. A blank or omitted limit gets `DEFAULT_DAILY_REQUEST_LIMIT`. Only an administrator can create an unlimited key, and must ask for it explicitly.

## What Changes

- **One shared key-creation budget across both routes.** A new in-process budget in `src/services/rate_limits.py` is checked and charged by both create handlers through one function. It has two counters, and a creation must pass both:
  - one keyed **exactly** on the authenticated account (`users.id`, or the single-user operator);
  - one keyed on the trusted client address.
  Defaults: 10 per account and 20 per address per hour (`KEY_CREATION_ACCOUNT_LIMIT`, `KEY_CREATION_ADDRESS_LIMIT`, `KEY_CREATION_WINDOW_SECONDS`; null disables, zero is refused). JSON and form requests draw from the same counters, so switching representation gains nothing. A refusal is a 429 with `Retry-After` on the JSON route and a flash error plus 303 on the form route. Either way no key is created, and a `key_creation_throttled` security event is emitted. The JSON route's existing 5/min slowapi decorator stays as it is.
- **A cap on active keys per non-admin account** (`KEY_MAX_ACTIVE_PER_ACCOUNT`, default 25, null disables). The budget limits how fast keys can be added. Without a cap, nothing limits how many accumulate, and each one carries its own bursts. The count runs under a row lock on the owning `users` row, so concurrent creates cannot overshoot it. Admins and the single-user operator are exempt. Existing keys are never revoked to get under the cap.
- **Only an administrator can make a key unlimited, at creation or by editing.**
  - Panel create: a blank limit field gets `DEFAULT_DAILY_REQUEST_LIMIT`. An "Unlimited" checkbox, rendered only for admins, is the only way to request NULL.
  - JSON create: an omitted field gets the default, as before. An explicit `null` still means unlimited, but **only for an admin**. A non-admin gets 403.
  - Editing a limit (panel modal and `PUT /api/keys/{id}/limit`): clearing a limit to NULL is admin-only. A blank edit field is a validation error for everyone, and admins use the same Unlimited checkbox.
  - With `DEFAULT_DAILY_REQUEST_LIMIT` null, a blank or omitted create limit is **refused** ("a daily request limit is required"). It no longer silently becomes unlimited.
  - A non-admin request that asks for unlimited is refused with no key written and no budget charged, and records `panel_forbidden` (`reason=unlimited_requires_admin`).
- **Panel template and `panel.js`.** The Unlimited checkbox goes into the create and edit modals. A `data-unlimited-toggle` delegated listener disables the number input while the box is ticked. The template gets new help copy. The nonce CSP rules apply: no inline handlers or styles, and no `!important`.
- **Docs.** `docs/architecture/rate-limits.md` (new control-table row, a budget section, settings, the account/address key spaces), `docs/architecture/control-panel.md`, the CLAUDE.md key-decisions bullet, the README settings table and quota paragraph, `.env.example`, and `docs/architecture/security-event-logging.md`.

## Capabilities

### New Capabilities
- `api-key-issuance`: the shared account+address key-creation budget, the active-key cap, and the rule that only an admin can set a key to unlimited, on both creation surfaces and both edit surfaces.

### Modified Capabilities
- `usage-quotas`: "New API keys receive a configurable default daily request limit". A blank panel field now gets the default, an explicit null is admin-only, and a null default makes the limit required.
- `panel-usage-slicing`: "Quota administration". The create and edit forms get the admin-only Unlimited control, and a blank field no longer means unlimited.
- `security-event-logging`: adds the `key_creation_throttled` event.
- `api-auth-hardening`: "REST API application-level auth". The archived requirement still called `POST /api/keys` admin-only, which the code has not been since multi-user mode shipped; the delta states the actual owner-scoped creation (bounded by this change's budget, cap and unlimited rule) and the 401 a JSON client gets without a session.

## Impact

- `src/services/rate_limits.py`: key-creation budget (`try_charge_key_creation`), exact dicts swept on access.
- `src/services/api_keys.py` (new, small): `resolve_create_limit` / `resolve_edit_limit` and `assert_active_key_capacity`, shared by both surfaces so they cannot drift.
- `src/api/routes.py`: `create_key`, `set_key_limit`.
- `src/control_panel/routes.py`: `keys_page` context (`is_admin`), `create_key_form`, `set_key_limit_form`.
- `src/control_panel/templates/keys.html`, `src/control_panel/static/panel.js`, and `_utilities.html` regenerated only if a new utility class is needed.
- `src/config.py`: four settings plus boot validation. `src/services/security_events.py`: one event in the allow-list.
- No migration. No change to MCP-side gates.
- Panel template change, so the owner does a browser pass with zero CSP violations.
