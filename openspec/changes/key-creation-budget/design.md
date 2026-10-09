## Context

Two routes insert `api_keys` rows: `create_key` in `src/api/routes.py` (JSON) and `create_key_form` in `src/control_panel/routes.py` (form). No other production code mints a key. Both routes sit behind `require_user_panel`, and both are CSRF-checked: the JSON route through the router dependency `verify_csrf`, the form route through the panel's form CSRF. Only the JSON route is throttled (`@limiter.limit("5/minute")`, keyed by client address).

An API key is a **principal** to the `/mcp` rate controls (`rate-limits.md`, "The principal is the grant…"). A new key starts with a full general bucket (burst 30) and a full write bucket (burst 15). Its daily quota is whatever the create handler stored in it, and on the form route a blank field stores NULL, which means unlimited. Unthrottled creation therefore replenishes per-principal velocity and gets around the durable ceiling. That is #323.

Precedents this design follows:
- The per-account panel login budget (#189). It is an exact dict keyed by `users.id`, swept on access, is not a lockout, and lives in `src/services/rate_limits.py`. It exists because slowapi's `key_func` cannot see anything beyond the session cookie or the address.
- #197's password-change route. It stacks two slowapi decorators, one keyed by session account and one by address, because "neither key subsumes the other".
- `--workers 1` and in-process state (`rate-limits.md`).

## Goals / Non-Goals

**Goals.**
- One creation budget that both routes draw from, keyed by account and by address.
- A blank form submission can never produce an unlimited key.
- Only an admin can set NULL, and only through an explicit control. This holds at creation and when editing.
- The total stock of keys a non-admin can hold is bounded.
- Tests alternate JSON and HTML requests against one budget.

**Non-goals.**
- Any change to the `/mcp` gates or to the quota mechanics.
- A daily quota for OAuth grants. The `usage-quotas` limitation stays as accepted; see D9.
- Revoking or backfilling existing unlimited keys. Grandfathering stays.
- Capping the *value* a non-admin may choose within 1..1,000,000. See D8 and the owner questions.

## Decisions

### D1. The budget lives in `rate_limits.py`, not in slowapi

Stacking slowapi decorators cannot produce a **shared** budget. slowapi namespaces its storage key by the decorated endpoint, so `@limiter.limit(..., key_func=session_user_key)` on two routes gives two independent buckets. A caller alternating JSON and form requests would get both allowances, which is the bypass the issue forbids.

`limiter.shared_limit(scope=...)` would share the bucket. It still cannot express "refuse only after validation and authorization, charge only on a creation that will be attempted", because slowapi counts every request that reaches the decorator. That includes invalid names and non-admin unlimited attempts, so a typo would use up allowance. It also keeps its state in a backend the rest of these controls do not use.

So the control is a plain function, `try_charge_key_creation(account_key, address) -> KeyCreationRefusal | None`, in `src/services/rate_limits.py`. Both handlers call it. It sits next to the login budget it copies.

### D2. Two exact counters, account and address, both must admit, charged together

- **Account key.** `("user", users.id)`, or `("single-user",)` for the sentinel, which has `id=None`. The account is the authenticated principal that `require_user_panel` returned. It is not read from the cookie in a `key_func`.
- **Address key.** `request.client.host` after the app's `ProxyHeadersMiddleware`. This is the same trusted address every other control keys on (`TRUSTED_PROXY_IPS`, #189). A request with no client address is keyed `("address", None)`, one shared bucket. That is a bound, not a bypass.
- **Admission.** Admit only if *both* counters are below their limits. Then increment *both*, with no `await` between the check and the increments, so the operation is atomic on the single event loop. If either counter refuses, neither is charged. The refusal names which budget refused, for the log only.
- **Windows.** A fixed window opens at the first charge, `KEY_CREATION_WINDOW_SECONDS` (default 3600), the same shape as the login budget. A fixed window can admit up to twice the limit across a window boundary. This is accepted (L3): it is still a hard per-hour order of magnitude and matches the precedent.
- **Defaults.** 10 per account and 20 per address per hour. A person creates a handful of keys in a session. The address limit is the looser of the two so that several accounts behind one NAT are not refused by each other's normal use, while one address driving many accounts is still bounded.
- **Admins are subject to the budget.** A compromised or scripted admin session is the case where it matters most. It is not a lockout: the window heals by itself, and a restart clears it (same as the login budget).

**Bounded by construction.** The account dict holds at most one entry per account that created a key in the last window. The address dict is charged only on an admitted creation, so its size is at most the number of admitted creations in the window, which is at most (accounts × account limit). An attacker cannot grow it without a session and spent account allowance. Both dicts are swept on access, as `_sweep_login_failures` does. No salted table is used, for the login budget's reason: merging account keys would let one account's activity refuse another's.

### D3. Where in the handler the charge happens

The order is the same on both surfaces:

1. Auth and CSRF (dependencies, unchanged).
2. Pure validation: name, permission, limit domain, and the unlimited-authorization check from D5. These touch no DB and charge nothing. A refusal here is a 400 or a flash and does not use up allowance.
3. Open the transaction. For a non-admin, `SELECT … FROM users WHERE id = :uid FOR UPDATE`, then count `api_keys WHERE user_id = :uid AND is_active`. At or over the cap: refuse (D4), with no charge.
4. `try_charge_key_creation`. On refusal: roll back, emit `key_creation_throttled`, and answer 429 + `Retry-After` (JSON) or a flash + 303 (form).
5. Insert, commit.

A charged creation whose commit then fails is **not refunded**. A refund path is a second piece of state to get wrong, and an error that leaves the caller over-charged is the safe direction. Step 4 comes after step 3 so that a cap refusal does not spend velocity allowance.

### D4. The active-key cap is in scope, because the budget bounds flow and not stock

At 10/hour an account can add 240 keys a day. After a month it can hold thousands of active keys, each with its own 30/15 bursts, and the aggregate velocity grows linearly with time. That defeats the per-principal buckets a different way. It also pushes towards `MCP_LIMITER_MAX_TRACKED_PRINCIPALS` (10,000), past which unrelated principals share one overflow entry. So the cap is justified.

It is cheap: one locked row read and one count, on a route that runs a few times a month.

- `KEY_MAX_ACTIVE_PER_ACCOUNT` defaults to 25, and null disables it.
- "Active" means `is_active = true`. Revoked keys do not count. Expired-but-not-revoked keys do count; the owner can revoke them.
- Admins and the single-user operator are exempt. An admin can already create unlimited keys, so capping them bounds nothing that matters. In single-user mode there is no `users` row to lock.
- An account already over the cap is grandfathered. Nothing is revoked. Its next create is refused until it revokes keys below the cap.
- The `FOR UPDATE` on the `users` row serializes concurrent creates for one account, so the count is exact. The JSON refusal is 409 with a message naming the cap and the remedy (revoke a key). The form refusal is a flash. Neither charges the budget. The refusal is not a security event: it is an ordinary quota, and it is visible to the user.

### D5. Unlimited is an admin-only, explicit request, on create and on edit

There is one resolver per operation in `src/services/api_keys.py`, called by both surfaces.

**Create**, `resolve_create_limit(user, *, provided: bool, value: int | None, unlimited: bool)`:

| Input | Admin | Non-admin |
| --- | --- | --- |
| a value 1..1,000,000 | value | value |
| blank (form) / omitted (JSON), default set | default | default |
| blank / omitted, default **null** | 400 "a daily request limit is required" | same |
| form `unlimited=1` / JSON explicit `null` | NULL | **403**, `panel_forbidden` `unlimited_requires_admin` |

**Form.** `unlimited` is a checkbox with `value="1"`. Only the exact value `"1"` counts as the request. If the box is ticked, the number field is ignored: `panel.js` disables it, so a browser does not even submit it, and the server ignores any value a scripted request sends alongside. A non-admin's form never renders the checkbox, so a non-admin `unlimited=1` is a tampered request. It is refused with a flash and no key is created. The refusal is not a silent downgrade to the default: substituting a different outcome for what was explicitly requested is the D9 surprise of #194, in reverse.

**JSON: omitted vs explicit null.** The distinction from #194 (`model_fields_set`) stays. The owner decision is about *blank*, and JSON has no blank. An omitted field is the JSON equivalent of an untouched form, so it gets the default. An explicit `null` is a deliberate request, the JSON equivalent of ticking Unlimited, so it is honoured for an admin and refused (403) for anyone else. Rejected alternative: treating a non-admin's null as "default". That would let an API client believe it had an unlimited key.

**Edit**, `resolve_edit_limit(user, *, value: int | None, unlimited: bool)`, on the panel modal and `PUT /api/keys/{id}/limit`. Ownership is still `_assert_key_owner` and runs first.

- A value: that value, for anyone allowed to edit the key.
- Panel blank: a validation error for everyone ("enter a limit" for non-admins, "enter a limit or tick Unlimited" for admins). Blank no longer means "clear".
- Panel `unlimited=1` / JSON `{"daily_request_limit": null}`: NULL for an admin, 403 + `panel_forbidden` for a non-admin.
- The edit path still never applies the default to an existing key.
- The NULL→value counter reset in `apply_daily_request_limit` is unchanged.

**Grandfathered unlimited keys** owned by non-admins stay unlimited. Opening their edit modal shows an empty, *enabled* number field: the "open with Unlimited ticked" behaviour exists only where the checkbox is rendered, which is for admins. `editLimit` in `panel.js` must therefore tolerate an absent toggle and never leave the number input disabled when there is no box to untick it. Saving requires a number, and cancelling keeps them unlimited. A non-admin may assign a numeric limit to their own grandfathered unlimited key (that only tightens it). The remedy for those keys is otherwise an admin setting a limit. Nothing is backfilled (L4).

### D6. Refusal shapes

| Refusal | JSON | Form |
| --- | --- | --- |
| budget | 429, `Retry-After` = seconds until the refusing window ends, `detail` names the window, not which counter | flash "Too many keys created recently — try again in N minutes." + 303 `/admin/keys` |
| cap | 409, `detail` names the cap and "revoke a key" | flash, same text |
| unlimited by non-admin | 403 | flash + 303 |
| limit required (null default) | 400 | flash + 303 |

A form refusal is a flash, not a status page, the same as every other key-form error (`_flash_key_error`). The flash keeps the session-carried message pattern; nothing goes in a query string.

### D7. `key_creation_throttled` security event

`security_events.emit("key_creation_throttled", subject=<account subject>, …)` with fields `actor_user_id`, `actor_username`, `client_ip`, `route`, `method`, `reason` (`account_budget` | `address_budget`), `limit_count`, `window_seconds`. All of them except the event name are existing allow-listed field names, so no new field bound is introduced. Exactly one emitter invocation per refusal; whether it reaches the sink stays subject to the global suppressor.

**The suppression subject is the same exact account identity the budget keys on**, derived from the D2 account key: `user:<users.id>` for a real account and one fixed `account:single-user` for the sentinel. It is *not* `subject_for(user_id=actor, request=request)`: the sentinel has `id=None`, so that helper falls back to the client address, and a single-user caller rotating trusted addresses would get a fresh log allowance per address. `client_ip` stays only an event field. Cap refusals emit nothing (D4). Unlimited refusals reuse `panel_forbidden`.

### D8. What a non-admin may still choose

A non-admin may still set any value in 1..1,000,000. At the default 120/min a single key cannot spend more than 172,800 calls a day, so a limit of 1,000,000 bounds nothing. It is visible, though: the keys page shows "x / 1000000", which is very different from a blank turning silently into "Unlimited". #323 asks that "a blank scripted form [not] silently become a quota-bypass primitive". This change closes that. It does not add a per-role ceiling on the value, which is a separate policy decision (owner question 1).

### D9. Related but out of scope: OAuth grants are also principals

An authenticated user can mint OAuth grants (DCR plus consent), and each grant is a fresh principal with no daily quota. Consent requires a panel session, and `/register` (3/min) and `/token` (10/min) are address-throttled. This is the same class of finding on a different surface, but it is not #323's route pair. It is recorded here and raised as an owner question, not closed silently.

### D10. Settings and boot validation

| Setting | Default | Domain |
| --- | --- | --- |
| `KEY_CREATION_ACCOUNT_LIMIT` | 10 | `NullableLimit`, 1..`LIMITER_COUNT_MAX` |
| `KEY_CREATION_ADDRESS_LIMIT` | 20 | `NullableLimit`, 1..`LIMITER_COUNT_MAX` |
| `KEY_CREATION_WINDOW_SECONDS` | 3600 | `int`, 1..`LIMITER_WINDOW_SECONDS_MAX` |
| `KEY_MAX_ACTIVE_PER_ACCOUNT` | 25 | `NullableLimit`, 1..`LIMITER_COUNT_MAX` |

All of them use the one `NullableLimit` representation of "off" (empty, `null` or `none`), and zero is refused. Each counter can be disabled independently, and disabling both budget counters turns the budget off.

## Tests (contract for the implementer)

Offline (`tests/test_issue_323_key_creation_budget.py`) with the settings patched small (e.g. account 3, address 5):

1. **Alternating representations share one budget.** Alternate JSON `POST /api/keys` and form `POST /admin/keys/create` for one account. Exactly the account limit succeed in total, the next is refused on *whichever* route it uses, and row counts confirm it.
2. **Account key survives address rotation.** One account, a different trusted client address per request: refused at the account limit.
3. **Address key survives account rotation.** Several accounts from one address: refused at the address limit while each account is under its own.
4. **Another account at another address is unaffected** by an exhausted pair.
5. **Validation failures and non-admin unlimited attempts charge nothing.** After N invalid names, a valid create still succeeds at full allowance.
6. **Atomicity.** Concurrent `asyncio.gather` creates for one account: admitted ≤ limit.
7. **Window expiry** (monotonic clock patched): admits again with no intervention.
8. **Refusal shapes**: JSON 429 with an integer `Retry-After`; form 303 with a flash; `key_creation_throttled` emitted with only the allow-listed fields; no row written.
9. **Unlimited matrix** (D5 table), create and edit, both surfaces, admin and non-admin, including blank form edit → error, and JSON edit with null by a non-admin → 403 and `panel_forbidden`.
10. **Null default**: blank or omitted create refused with "required"; admin Unlimited still works.
11. **Template**: the checkbox is rendered for an admin and absent for a non-admin; no inline handler or style (the existing CSP template inventory tests cover new markup); `data-unlimited-toggle` is handled in `panel.js`.
12. **Settings**: zero refused, the null forms accepted, above-ceiling refused, through a real env file like the existing limiter-setting tests.
13. **Single-user suppression subject.** In single-user mode, budget refusals from rotating trusted client addresses all charge one fixed suppression subject: with the suppressor live, they do not each get a fresh allowance.
14. **Non-admin grandfathered key.** A non-admin can open the editor of their own grandfathered unlimited key (no Unlimited control rendered) and assign a numeric limit.

Real Postgres (`tests/integration/`):

15. **Cap is exact under concurrency.** With the cap at N-1 active keys, two concurrent creates for one non-admin account result in exactly one new row. An admin is not capped, and revoked keys do not count.

## Risks / Trade-offs

- **[A behaviour change for operators who clear the box to get unlimited]** → The help copy says so. An admin has the checkbox. A non-admin who wants unlimited asks an admin. CLAUDE.md, the README and the specs all change in this PR.
- **[A shared NAT with many users hitting 20/hour]** → Not plausible for a key-creation route. The setting is tunable.
- **[An admin locked out of creating keys for up to an hour by their own script]** → The window heals by itself, and a restart clears it, as with the login budget.

## Accepted limitations

- **L1.** The budget is in-process. A restart clears it, and `--workers N` multiplies it by N, as for every control in `rate-limits.md`. A restart is an operator action, not something a caller can trigger.
- **L2.** A creation whose commit fails after the charge is not refunded. This errs towards stricter.
- **L3.** A fixed window can admit up to 2× the limit across a boundary.
- **L4.** Existing unlimited keys, including non-admin-owned ones, stay unlimited. No backfill.
- **L5.** A non-admin may choose any limit up to 1,000,000, which is above what one key's velocity can spend in a day (D8).
- **L6.** OAuth grants remain principals that can be minted without a creation budget beyond the address throttles, and they have no daily quota (D9).
- **L7.** Admins are exempt from the active-key cap.

## Migration plan

No schema change. Deploy is the normal merge to `main` followed by Flux. Rollback, configuration only: set `KEY_CREATION_ACCOUNT_LIMIT`, `KEY_CREATION_ADDRESS_LIMIT` and `KEY_MAX_ACTIVE_PER_ACCOUNT` to null, which disables the account counter, the address counter and the active-key cap and restores pre-change creation velocity. `KEY_CREATION_WINDOW_SECONDS` is a non-nullable integer and stays at a valid value (it is inert once both counters are off). The blank/null limit semantics are code: only an image rollback restores the old "blank or explicit null means unlimited for anyone" behaviour.
