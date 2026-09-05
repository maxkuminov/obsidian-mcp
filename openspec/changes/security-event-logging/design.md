## Context

Single uvicorn worker (`Dockerfile:31`, `--workers 1`), stdout/stderr scraped per line by Alloy into Loki. The panel's health page reads a second, in-process sink: `src/services/error_log.py`'s 100-entry ERROR ring buffer, attached in the lifespan to the root logger **and** to `uvicorn.error`. Those two sinks are the whole observability surface, and both are downstream of one root-logger configuration that never takes effect.

Constraints that shape the fix:

- **The SDK wins the race by construction.** `src/main.py:26` imports `src.mcp_server.server`, whose module-level `FastMCP(...)` (`server.py:91`) calls the SDK's `configure_logging()`, which runs `logging.basicConfig(handlers=[RichHandler(...)], format="%(message)s")`. `basicConfig` is a no-op once the root has a handler, so `src/main.py:32` never applies. Any fix must run **after** that import and must be forceful.
- **The ring buffer is a live feature and must not be collateral damage.** `logging.basicConfig(force=True)` removes *and closes* every existing root handler. Today the ordering happens to save us — `configure_logging()` at import time precedes `error_log.attach()` in the lifespan — but "happens to" is not a guarantee, and the lifespan is re-entered in tests.
- **`extra` is attacker-influenced.** Ten `auth_failure` sites already pass `extra=`, one of them derived from a **presented bearer token** (`_redacted_prefix(token)`, `src/mcp_server/auth.py:57`). A formatter that serialises `record.__dict__` minus the standard keys would ship key prefixes, vault paths and query strings into a structured sink the moment somebody adds a field.
- **The transfer 404 is an anti-oracle.** `src/transfer/routes.py:114` — *"Never add a reason: the reason is the oracle."* Whatever is logged, the response stays byte-identical. And the admission predicates that produce it (`lookup_token`, `claim_upload`) are one filtered query each, deliberately: they are the linearizability argument for single-use redemption.
- **`_tracked` is the only place a tool call is observed**, and `usage_logs` rows are read by `/admin/usage`, `/admin/performance` and `/admin/search-analytics` with **unguarded casts** on reserved `params` keys (`docs/architecture/usage-attribution.md`). A new marker is a schema decision even without a migration.
- **Codex reviewed all four findings** before this proposal was written and attached mandatory caveats: allow-list the extras (#190), reason codes only and never secret material (#191), centralise the permission-denied marker, log a redacted tag plus the trusted client identity, keep responses uniform, rate-limit denial logging (#192), catch `Exception` and not `BaseException`, make the audit write best-effort, re-raise (#193).

**A pre-code Codex review of the first draft of this design returned FAIL with 2 BLOCKER, 11 MAJOR and 4 MINOR findings.** It was right about all of them: the draft promised "exactly one record per outcome" while also promising to suppress records; it specified six transfer refusal reasons the code cannot distinguish; its exception handler wrapped work that is not the tool body; its suppressor handed a fresh allowance to every new bogus bearer token; and several of its "SHALL NOT contain" claims were unsatisfiable as written. Every finding is folded in below, and the map from finding to decision is the last section of this document.

## Goals / Non-Goals

**Goals:**
- One physical line per record, machine-parseable, UTC-stamped, with the structured fields the code already passes actually present.
- One record **submitted** per authentication and authorization outcome, carrying a reason code and identities of a declared provenance.
- A refused write and a failed tool body are distinguishable from a successful call in `usage_logs` and on `/admin/usage`.
- No new failure mode: logging never raises into a request path, never changes a response, never adds a query to an accepted request, and never masks an exception.

**Non-Goals:**
- **Re-formatting uvicorn's own loggers.** `uvicorn`/`uvicorn.access`/`uvicorn.error` carry their own handlers with `propagate: false` (which is exactly why `error_log` attaches to `uvicorn.error` by name), so their lines stay in uvicorn's format and the container log is mixed JSON and text. Clearing their handlers and letting them propagate would give a uniform stream, but it changes the shape of the highest-volume lines in an existing Loki pipeline for no security benefit. Residual R4.
- **A persistent audit table**, and any retention or shipping policy. `usage_logs` records tool calls; security events go to stdout, and what happens to them after Alloy is an operator concern (Residual R2).
- **Bounding how many security events a caller can *cause*.** This change bounds what reaches the log sink (D7) and nothing else. Admission control — a per-principal request bucket and a failed-authentication budget — is the sibling change **`mcp-rate-limits`** (#188/#194). Residual R2.
- **Lowering `error_log.CAPTURE_LEVEL` to WARNING.** The health page is "recent errors", not "recent refusals"; a credential-stuffing burst would evict every real error from a 100-entry buffer. Auth failures stay WARNING and reach Loki only.
- **Moving the write-permission gate into `_tracked`.** That would make `permission_denied` a *pre-body* refusal with a refusal count on `/admin/performance`, but it also changes quota accounting (a refused write would stop consuming its already-committed slot) and the refusal ordering for nine tools. See D6 and Residual R5.
- **A typed outcome for every in-body tool refusal.** `create_note` on an existing path, a path-validation refusal, a size refusal and a conflict all return an in-band string today and are *not* marked in `usage_logs`; this change does not make them so. See Residual R1.
- **Logging the bare "no `Authorization` header" 401** at `src/mcp_server/auth.py:96`. Unauthenticated probing of `/mcp` is constant background noise with no identity attached; Traefik's access log already counts it.

## What "one record per outcome" means

The first draft's requirement was self-contradictory: eleven bad passwords in sixty seconds cannot be both eleven records and at most ten. The contract is therefore stated at the **call site**, not at the sink:

- **Every outcome in the catalogue SHALL cause exactly one `security_events.emit(...)` call** — never zero, never two for one decision, and never one per retry loop iteration.
- **INFO events are always emitted** to the sink. They are bounded by the existing rate limits (`5/minute` login, `10/minute` token, `3/minute` register, `20/minute` revoke) or by a human clicking a panel button, and they are the ones an operator reconstructs a session from.
- **WARNING and ERROR events are submitted to the suppressor** (D7). Within a window, the first `MAX_EVENTS_PER_WINDOW` for a `(event, subject)` are emitted individually and the rest are counted and represented by exactly one `events_suppressed` summary carrying the count. A suppressed record is *accounted*, never silently dropped.
- **The `usage_logs` row is not a log record and is never suppressed.** A refused or failed tool call always writes its row.

Tests assert the emit-call count (with the suppressor disabled) and, separately, the sink behaviour under a flood.

## The event catalogue

Every record is emitted by `security_events.emit(...)`, whose message is the event name and whose fields are allow-listed (D2). Four fields are always present and are not repeated in the table: `ts`, `level`, `logger`, `msg` (the event name). `client_ip` is present on every event raised from an HTTP request path and is `request.client.host` **after** `ProxyHeadersMiddleware` (D3). Field provenance — resolved or submitted — is D15. Line numbers are the current tree.

| Event | Level | Fields (beyond the always-on four) | Emitted where |
| --- | --- | --- | --- |
| `panel_login_succeeded` | INFO | `user_id`, `username`, `client_ip`, `route` | `src/auth/routes.py:183` (`login_submit`, success branch, before the session is written) |
| `panel_login_failed` | WARNING | `reason` (`unknown_user` \| `inactive_user` \| `bad_password`), `username` (submitted), `user_id` (only when a row resolved), `client_ip`, `route` | `src/auth/routes.py:173-180` (`login_submit`; the merged condition is split for the reason code only — the rendered 401 is unchanged) |
| `panel_logout` | INFO | `user_id`, `username`, `client_ip` | `src/auth/routes.py:204` (`logout`, read **before** `request.session.clear()` at `:205`) |
| `panel_bootstrap_admin_created` | INFO | `user_id`, `username`, `client_ip` | `src/auth/routes.py:305` (`register_submit`, after the insert) |
| `panel_bootstrap_refused` | WARNING | `reason` (`invalid_username` \| `weak_password` \| `password_mismatch` \| `vault_path_missing` \| `vault_path_invalid` \| `already_bootstrapped`), `client_ip` | `src/auth/routes.py:234, 242, 250, 258, 267, 285` |
| `panel_password_reset` | INFO | `user_id` (actor), `username` (actor), `owner_user_id` (target), `client_ip`, `route` | `src/control_panel/users.py:609-611` (`reset_password`, after the `session_version` bump) |
| `oauth_token_issued` | INFO | `client_id`, `user_id`, `grant_id`, `scope`, `reason` = `authorization_code` | `src/oauth/routes.py:757` (`_handle_auth_code`) |
| `oauth_token_refreshed` | INFO | `client_id`, `user_id`, `grant_id`, `scope` | `src/oauth/routes.py:918` (`_handle_refresh`) |
| `oauth_token_refused` | WARNING | `reason` (`<rfc_code>.<sub_reason>`, e.g. `invalid_grant.pkce_mismatch`), `client_id` (submitted until a client row resolves), `user_id` / `grant_id` only when resolved, `client_ip` | `src/oauth/routes.py:603, 614, 641, 648, 651, 656, 659, 663, 666, 675, 693, 709, 772, 808, 822, 829, 832, 837, 844, 859, 874` |
| `oauth_token_rotation_failed` | ERROR | `client_id`, `grant_id`, `error_type`, `stack` (`exc_info=True`) | `src/oauth/routes.py:914-916` (the `except Exception` whose traceback is currently discarded behind a 500) |
| `oauth_consent_granted` | INFO | `client_id`, `user_id`, `scope`, `client_ip` | `src/oauth/routes.py:583` (`authorize_post`, after the code row commits) |
| `oauth_consent_denied` | INFO | `client_id`, `user_id`, `client_ip` | `src/oauth/routes.py:493` (`authorize_post`; the session user is resolved for the record — today a deny carries no identity at all) |
| `oauth_authorize_refused` | WARNING | `reason` (`unsupported_response_type` \| `pkce_invalid` \| `invalid_scope` \| `unknown_client` \| `invalid_redirect_uri` \| `state_mismatch` \| `session_required` \| `scope_clamped_empty`), `client_id`, `user_id` when resolved, `client_ip` | `src/oauth/routes.py:348, 351, 357, 366, 369, 446, 449, 455, 464, 482, 485, 516` |
| `oauth_cross_user_client_refused` | WARNING | `client_id`, `user_id` (requester), `owner_user_id`, `route` | `src/oauth/routes.py:127-139` (`_cross_user_client_error`, the one helper both `:528` and `:570` return) |
| `oauth_client_registered` | INFO | `client_id`, `client_name`, `scope`, `count` (redirect URIs), `client_ip` | `src/oauth/routes.py:300-311` (`register_client`; **never** `client_secret`) |
| `oauth_client_registration_refused` | WARNING | `reason` (`invalid_client_metadata` \| `invalid_redirect_uri` \| `invalid_scope` \| `unsupported_auth_method`), `client_ip` | `src/oauth/routes.py:212, 214, 219, 226, 228, 233, 244, 248, 258, 273` |
| `oauth_grant_revoked` | INFO | `client_id`, `user_id`, `grant_id`, `count` (rows revoked), `client_ip`, `route` | **After the caller's commit**, at each HTTP caller: `src/oauth/routes.py:1003-1004` (`/revoke`) and the panel's revoke handler in `src/control_panel/routes.py`. Never inside `revoke_grant_family` (D10) |
| `oauth_revoke_noop` | INFO | `reason` (`missing_token` \| `unknown_token` \| `unknown_client` \| `client_mismatch`), `client_id`, `client_ip` | `src/oauth/routes.py:942, 948, 982, 983` (RFC 7009 §2.2 — the response says nothing, so the log must) |
| `oauth_revoke_refused` | WARNING | `reason` = `client_auth_failed`, `client_id`, `client_ip` | `src/oauth/routes.py:985` |
| `rate_limit_exceeded` | WARNING | `route`, `method`, `client_ip`, `limit_count`, `window_seconds` | `src/main.py:299` — a local wrapper around slowapi's `_rate_limit_exceeded_handler`, the one hook every 429 in the app passes through |
| `auth_failure` | WARNING | `reason` (unchanged: `invalid_key` \| `ownerless_credential` \| `inactive_user` \| `key_expired` \| `cross_user_grant` \| `no_vault_scope`), `token_tag` **or** (`key_id` for an API key row, `oauth_token_id` for an OAuth row), `client_ip`, `route` | `src/mcp_server/auth.py:143, 165, 185, 199, 287, 300, 320, 340, 353, 371` — existing event; `key_prefix` becomes `token_tag`, the OAuth branches stop putting an `oauth_tokens.id` in `key_id` (D15), and the request context is added |
| `tool_write_refused` | WARNING | `tool`, `user_id`, `actor_kind`, `actor_ref`, `key_id` / `oauth_token_id` | `src/mcp_server/tools.py:1213` (`_require_write`, the single definition all nine call sites reach) |
| `transfer_refused` | WARNING | `reason` (D8's enum), `token_tag` (absent when no token was presented), `route`, `method`, `client_ip`, `user_id` / `key_id` / `oauth_token_id` **only when a row resolved** | `src/transfer/routes.py:261, 265, 280, 284, 374, 382, 391, 412, 527, 668, 673, 680, 691, 697` (every `_not_found()` return) |
| `panel_forbidden` | WARNING | `reason` (`admin_required` \| `not_your_key` \| `not_your_client` \| `not_your_token` \| `actor_revoked`), `user_id`, `username`, `route`, `method` | `src/control_panel/routes.py:234, 713, 1055, 1067`; `src/api/routes.py:224-225`; `src/control_panel/users.py:241, 320, 449, 601` (`_actor_still_privileged` false) |
| `csrf_refused` | WARNING | `route`, `method`, `user_id` (from the session when present), `client_ip` | `src/csrf.py:57` (`verify_csrf`) |
| `tool_exception` | ERROR | `tool`, `error_type`, `stack`, `user_id`, `actor_kind`, `actor_ref`, `duration_ms` | `src/mcp_server/tools.py` — the `except Exception` wrapping **only** `await fn(...)` at `:711` (D5) |
| `tool_usage_log_failed` | WARNING | `tool`, `error_type` (the audit failure's class) | same handler, when the best-effort `usage_logs` write reports failure (D5, D11) |
| `events_suppressed` | WARNING | `reason` (the suppressed event name), `count`, `window_seconds` | `src/services/security_events.py` (D7) |

Two events already in the tree keep their names and gain nothing but the working formatter: `quota_admission_failed` (`src/services/quotas.py:261`) and `usage_log_credential_gone` (`src/mcp_server/tools.py`). `quota_counter_prune_failed` and the two `src/services/vault.py` sites currently pass `extra={"error": str(exc)}`; they move to `error_type` (D2).

## Decisions

**D1 — Reconfigure the root logger explicitly, after the SDK import, preserving handlers `error_log` owns.**
`src/logging_setup.py` exposes `configure_logging(level=..., fmt=...)`. It walks `logging.getLogger().handlers`, removes and closes every handler **except** the instance `error_log.installed_handler()` returns, installs one `StreamHandler(sys.stderr)` carrying the formatter of D2, sets the root level from `LOG_LEVEL` (default `INFO`), re-applies the existing `httpx`/`httpcore` `WARNING` floors, and is idempotent. `src/main.py` calls it immediately after the import block (replacing the `basicConfig` at `:32`); `src/mcp_stdio.py` calls it with `WARNING` — **stderr matters there**, because stdout is the MCP protocol channel.
*Why not `logging.basicConfig(force=True, handlers=[...])`, which is what #190 recommends:* `force=True` removes **and closes** every root handler, including the ring buffer's, so its safety would depend on `configure_logging()` always running before `error_log.attach()`. It does today (import time versus lifespan) and it must not have to. The explicit loop is the same effect with the exception written down, and it makes "the health page keeps working" a property of the code rather than of an import order. `error_log.attach()` stays idempotent and re-attaching after a reconfiguration is a no-op, so either order is safe.
*Verification:* an in-process integration test that imports `src.main` in a **subprocess**, emits a record carrying `extra`, and asserts on the captured stderr that the root has exactly one non-ring handler, that the line parses as JSON, that `ts` ends in `Z` and parses as UTC, and that the extra field is present — the "actual-process integration test" Codex asked for on #190. A unit test attaches the ring handler first, calls `configure_logging()`, and asserts the buffer still receives an ERROR.

**D2 — A typed, bounded field allow-list; unknown keys are dropped.**
The formatter builds its object from `ts` (UTC ISO-8601, milliseconds, `Z`), `level`, `logger`, `msg` (the rendered message, bounded — D16), plus `stack` when `exc_info` is set (D16) and `error_type` derived from the exception class. Then, and only then, it copies the record's `extra` keys that appear in `ALLOWED_FIELDS`, coercing each to its declared type and truncating to its declared bound:

`reason`(str 64), `outcome`(str 16), `route`(str 200), `method`(str 8), `status`(int), `tool`(str 100), `user_id`(int), `username`(str 64), `owner_user_id`(int), `actor_kind`(str 20), `actor_ref`(str 64), `key_id`(int), `oauth_token_id`(int), `client_id`(str 64), `client_name`(str 120), `grant_id`(str 64), `scope`(str 64), `token_tag`(str 16), `client_ip`(str 45), `error_type`(str 100), `limit`(int), `limit_count`(int), `count`(int), `day`(str 10), `window_seconds`(int), `duration_ms`(int), `cleared_user_id`(bool).

`key_prefix` is **removed from the allow-list**: the name invites logging a raw `omcp_` prefix of a *presented* token, and dropping the field is a safer failure than shipping it. Nothing free-text is allow-listed except `reason` (a closed vocabulary per event, not a message). `error` (a bare `str(exc)`) is *not* a field; the three existing sites move to `error_type`, and an exception message reaches the log only through `msg` or `stack`, which is where a reader already expects prose. Query strings, request bodies and note contents have no field to ride in — `route` is `request.url.path` only.
*Alternative rejected:* serialising the whole `extra` dict (the shortest reading of #190's "the record's extra dict"). It is one careless `extra={"path": path}` away from putting one tenant's vault paths into a shared sink, which is the Codex caveat verbatim.
*Alternative rejected:* logfmt. A value containing a space or a quote needs escaping rules that every consumer must agree on; JSON already has them, and Alloy/Loki parse it natively.
`LOG_FORMAT=text` produces the same allow-listed fields as `ts level logger msg k=v …` on one line, for local `make logs` reading. It is a rendering of the same object, never a second field policy.

**D15 — Field provenance: resolved, submitted, or derived — and the rule is per field, not per record.**
The first draft said identifiers come only from resolved rows, which is false the moment a login names an unknown user or a token request names an unknown `client_id` — the whole point of those records is the identifier that did not resolve. `ALLOWED_FIELDS` therefore declares a **class** per field:
- **Resolved** — read from a database row the server loaded: `user_id`, `owner_user_id`, `key_id`, `oauth_token_id`, `grant_id`, `actor_kind`, `actor_ref`, `client_name`, `scope`. A call site MUST NOT put a caller-supplied value in one of these. `key_id` is an `api_keys.id` and nothing else: the OAuth branches of `auth_failure` move their `oauth_tokens.id` to `oauth_token_id`, which is the field that names it in `usage_logs` too.
- **Submitted** — copied from the request when it did not resolve: `username`, `client_id`. Truncated to their bound, never used as a suppression subject (D7), and carried only on refusal events where the identifier *is* the fact being recorded. Both are public identifiers by construction: a username is typed into a login form and a `client_id` travels in an OAuth redirect URL.
- **Derived** — `token_tag` only: `"sha:" + sha256(value).hexdigest()[:8]`, `_redacted_prefix`'s exact behaviour, moved into `src/services/security_events.py` as `redacted_token_tag()` with `src/mcp_server/auth.py` delegating to it, so there is one definition and one test. A presented credential may reach the log **in no other form**. When no token was presented, the field is **absent** — not `"sha:"` of the empty string, which would be a constant that reads like a tag.
An AST sweep (D14) checks the resolved/submitted split where it can — a literal `client_id=form.get(...)` in a resolved field fails — and the field list documents it for the rest.

**D16 — Messages and tracebacks have their own rule, because the allow-list cannot reach them.**
The draft's "no record SHALL contain a request-derived path" was unsatisfiable: `move_note`'s existing warnings interpolate vault paths into `msg`, and any traceback may carry an exception message built from a path. The rule is split:
- **Structured fields** carry no path, no body, no query string and no credential material — enforced by the allow-list, which simply has no field for them.
- **`msg`** is a developer-authored constant or format string. It MAY interpolate operational context, including a vault-relative path, exactly as it does today. It MUST NOT interpolate credential material — a password, secret, code, token, verifier, cookie or CSRF token — and that is what the canary tests check.
- **`stack`** is present only with `exc_info`, and an exception message is not under this change's control; it is accepted as operational text under the same rule as `msg`.
- **Bounding is head-and-tail, not truncation.** "The whole traceback, bounded at 8 KiB" was a contradiction. `stack` keeps the **first 4 KiB and the last 3 KiB** with an elision marker naming the dropped byte count in between, and the formatter guarantees the exception **type line and the final line survive** — the two lines that identify a fault — by appending them after the tail if elision would have removed them. A traceback under the budget is emitted whole.
- **Canary tests, not substring sweeps.** Secret-absence tests submit unique high-entropy values (a 32-character random token per case), assert that no captured record contains that exact value **or any substring of it 12 characters or longer**, and assert that a record for a request carrying no token has no `token_tag` field at all.

**D3 — The client identity is `request.client.host`, after `ProxyHeadersMiddleware`.**
There is no IP helper in the tree today. `security_events.client_ip(request)` returns `request.client.host` (or `None`), because `ProxyHeadersMiddleware` (`src/main.py:303`) has already rewritten `scope["client"]` from `X-Forwarded-For` **only** for peers inside the RFC 1918 ranges the `request-trust` capability mandates. Reading `X-Forwarded-For` directly would accept a spoofed header from any client — the exact thing that capability exists to prevent — so the helper never touches headers, and a test pins that a forged header from an untrusted peer does not appear in a record.

**D4 — `permission_denied` is recorded once, at `_require_write` itself.**
`_require_write` gains two lines before its return: `timing.record("error", _PERMISSION_DENIED_MARKER)` and one `security_events.emit("tool_write_refused", ...)`. `timing.record` is a no-op outside a tracked call (`src/services/timing.py:72`), so a call from a test or a future non-tool path cannot raise, and `_tracked` merges the holder into `params` at `:733` — which means **all nine call sites** (`create_note`, `edit_note`, `move_note`, `delete_note`, `set_frontmatter`, `write_file`, `delete_file`, and `request_upload` / `import_from_url` through `_mint_preflight`) inherit the marker without being touched. That is Codex's "centralize the marker" caveat; a per-caller marker is eight chances to forget one and a tenth tool that silently has none.
`request_download` calls `_mint_preflight(need_write=False)` and is deliberately unaffected.

**D5 — The exception handler wraps the tool body and nothing else.**
The draft put `except Exception` on `_tracked`'s outer `try`, which spans the three admission gates *and* the whole telemetry tail. That misclassifies two unrelated failures as tool failures: a database fault inside the quota gate (before the body ever ran — and `quotas.admit` already logs `quota_admission_failed` and re-raises deliberately), and a failure in `_truncate_params`/`_log_usage` **after a write has already landed on disk**. Reporting a completed `edit_note` as `tool_exception` is exactly the "silently wrong record" this change exists to prevent.
So the structure is an explicit two-state one, and the body call gets its own `try`:

```
if refusal is not None:
    result = <refusal>                 # no body ran; existing markers apply
else:
    try:
        result = await fn(*args, **kwargs)     # the only guarded expression
    except Exception as exc:
        <duration; emit tool_exception; best-effort row; raise>
# from here the body has COMPLETED; nothing below may write tool_exception
duration_ms = ...; params = ...; await _log_usage(...)
```

The outer `try:`/`finally: timing.clear(token)` is unchanged. The invariant, stated for the reviewer and pinned by a test: **`tool_exception` is written only on the path where `await fn(...)` raised**, and a failure of anything after that expression returns leaves the ordinary success-path row (or, if `_log_usage` itself fails, the existing `Failed to log usage` warning).
`Exception`, never `BaseException`: `asyncio.CancelledError` is a `BaseException` in 3.8+, and a client disconnect or a shutdown must not be recorded as a tool failure or write a row (Codex's caveat on #193, and its own test).
Inside the handler, in order: (1) measure `duration_ms` from the same `start`; (2) `emit("tool_exception", level=ERROR, exc_info=exc, …)` — first, because it is the cheapest step and the one the health page depends on; (3) the best-effort row (D11); (4) a bare `raise`, so the SDK still produces its error result and the traceback is unchanged.
The row carries the same `params` the success path builds (bound arguments through `_truncate_params`, then `timing.current()`), with `error = "tool_exception"` and `error_type` merged **over the top**, `duration_ms` as measured and `response_size = 0`. That precedence is deliberate: a body that recorded `related_source_not_found` and *then* raised is logged as `tool_exception`, because the exception is the outcome. `error_type` is a **new reserved `params` key** (string), registered in `docs/architecture/usage-attribution.md`; no reader casts it.
*Alternative rejected:* logging the exception and skipping the row. `/admin/performance` is the view built to find slow paths, and a tool that raises after eight seconds of I/O is the slowest path there is.

**D11 — The audit write reports whether it wrote, and cancellation of the audit never replaces the tool's exception.**
`_log_usage` swallows every insertion failure internally (its last-resort `except` exists so that logging can never fail a call that already did its work), so the exception handler could not have known whether the row landed. `_log_usage` therefore **returns an explicit status** — `True` when a row was inserted, `False` when it gave up — with its existing behaviour and every existing caller unchanged (the success path ignores the return). The exception handler logs `tool_usage_log_failed` when the status is `False`.
Around the audit `await`, the handler catches **`BaseException`**, deliberately and only there: if the enclosing task is cancelled while the audit row is being written, that `CancelledError` must not become the exception the caller sees in place of the tool's own failure. The handler records `tool_usage_log_failed` with the cancellation's class name and re-raises the **original** tool exception; the runtime re-delivers the cancellation at the task's next suspension point. This is the one place `BaseException` appears in this change, it is scoped to a single `await` that is pure bookkeeping, and it is pinned by a test that cancels during the audit and asserts the original exception propagates.

**D6 — Both new markers are post-body, and the pre-body refusal predicate is unchanged.**
`docs/architecture/usage-attribution.md` states the rule: a marker belongs to exactly one side of the body/no-body line, and only pre-body markers belong in `pre_body_refusal_sql()`. `_require_write` is called *inside* a tool body that has already passed the vault gate, the argument screen and the quota gate (and has spent its quota slot); `tool_exception` is by definition a body that ran. Neither is added to `PRE_BODY_REFUSAL_ERROR_MARKERS`; `usage_stats.py` gains only a comment naming them as classified post-body, so the next reader does not have to re-derive it.
*Known cost, accepted:* a read-only credential probing `create_note` five thousand times contributes five thousand near-zero rows to that tool's percentiles, dragging p50 down. The honest fix is to move the write gate into `_tracked`, which changes quota accounting and refusal order for nine tools. Instead the refusal is made **visible** on `/admin/usage` (D9). Residual R5.

**D7 — Suppression is keyed on a subject that a caller cannot mint, and summaries are lazy.**
`security_events` exposes `should_emit(event, subject)` and `emit(...)`; every WARNING/ERROR event consults the suppressor, INFO events never do (see "What one record per outcome means").
- **The subject is a stable source, not the most specific identifier.** The draft keyed on the most specific identity available, which handed a fresh allowance to every new bogus bearer token: a new token means a new `token_tag`, and with 512 keys evicted oldest-first an attacker rotating tokens is never suppressed *and* evicts everyone else's counters. So: `token_tag`, `username` and `client_id` (the submitted and derived classes) are **never** subjects. The subject is `user_id` when the credential resolved to one, otherwise the **trusted client IP** (D3), otherwise `-`. Every unauthenticated refusal from one source therefore shares one allowance, whatever it presents.
- **Two caps, not one.** Per `(event, subject)`: `MAX_EVENTS_PER_WINDOW` = 10 per 60 s. Per `subject` across all events: `MAX_EVENTS_PER_SUBJECT_PER_WINDOW` = 50 per 60 s, so a source cycling through twenty different refusal events cannot multiply its allowance by twenty.
- **Summaries are emitted lazily and bypass suppression.** A window's `events_suppressed` is emitted when the next event for that key arrives after the window closed, and any window still holding a nonzero count is flushed on shutdown (a lifespan hook, and `atexit` for the stdio entry point). No timer task. A summary is never itself suppressed and never counts against either cap.
- **Bounded and safe.** At most 512 `(event, subject)` keys plus 512 subject keys, oldest window evicted first; a `threading.Lock` guards both maps; `should_emit` catches everything internally and **fails open** — an internal error means the record is emitted, never that it is dropped and never that the request raises.

**D8 — The transfer refusal reason is diagnosed after the decision, by a separate read, and only when it will be logged.**
The draft named six reasons the code cannot distinguish: `lookup_token` (`src/services/transfer.py:619`) and `claim_upload` (`:536`) are each **one filtered query** — hash, direction, `state = pending`, `expires_at > now` — so expired, wrong-direction, claimed, completed and consumed all arrive as the same `None`, and `_load_valid` collapses credential, root and path failures into another. Re-ordering those queries to yield a reason would rewrite the linearizability argument for single-use redemption, which this change must not touch.
So the reason comes from a **diagnosis pass that runs after the refusal decision is already made**:
- `src/services/transfer.py` gains `classify_token_refusal(session, token, *, direction) -> TransferRefusal`, a **read-only** helper that selects the row **by token hash alone** and derives one reason from its columns: `unknown_token`, `wrong_direction`, `expired`, `already_claimed`, `already_completed`, `already_consumed`, or `claim_lost` (the row is still pending at diagnosis time, so the conditional UPDATE lost a race). `TransferRefusal` is a frozen dataclass of `(reason: str, row: TransferToken | None)`.
- The branches the route already knows do **not** call it: `missing_token` (no header — and therefore **no `token_tag`**), `credential_invalid`, `root_reassigned`, `path_invalid`, `revalidation_failed` (with the failing predicate as a sub-reason), `publication_unsupported`, `prepublish_aborted`, `file_unreadable`, `fingerprint_mismatch`, `content_changed`. `_load_valid` (`:163-180`) returns `TransferRefusal` instead of bare `None` on the three predicates it evaluates itself, so those three stop collapsing; its accept path still returns the row and no caller's control flow changes.
- **The diagnosis query runs only when the record will actually be emitted** — the route calls `should_emit` first (D7). Under an enumeration flood the extra read stops with the log record, so this cannot become a DoS amplifier, and an accepted request never issues it at all.
- The external answer is untouched: `_not_found()` and `NOT_FOUND_BODY` are not modified, and a test asserts the response bytes, status and headers are identical across every cause. The uniform-404 requirement in `openspec/specs/file-transfer/spec.md` is unchanged and the added requirement restates it as a constraint on the logging.

**D9 — `/admin/usage` selects the raw values and maps the outcome in the route.**
`recent_logs` (`src/services/usage_filters.py:247`) selects **three raw values**: `ul.params->>'error'`, `ul.params->>'error_type'` and `ul.params->>'over_quota'` — the last as **text, never a `::boolean` cast** (`/admin/performance`'s unguarded casts are a standing hazard: one bad row 500s the page for every user until it ages out). The route maps them to a display outcome with explicit precedence — `tool_exception` (rendered as *failed*, with `error_type`) beats any other `error` value, any other `error` value renders as *refused* with that value as its reason, then `over_quota` equal to the string `true` renders as *refused / over_quota*; anything else, including an `over_quota` value that is neither `true` nor `false`, renders as *refused* with the raw value shown. Nothing is discarded on the way from SQL to the template, and an unrecognised future marker degrades to its own string rather than to a blank.

**D10 — The revocation event is emitted by each HTTP caller, after its commit.**
The draft put it inside `revoke_grant_family` (`src/oauth/grants.py:127`), which cannot attribute it: the helper has no request, no client IP and no session user, and it **does not commit** — a record emitted there is a claim about a transaction that may still roll back. It keeps returning its rowcount and stays free of logging. `/revoke` (`src/oauth/routes.py:1003-1004`, Slice B) and the panel's revoke handler (`src/control_panel/routes.py`, Slice D) each emit `oauth_grant_revoked` **after their own commit**, with the identity each has already resolved plus its request context. Two call sites, one event name declared once in the catalogue; neither slice touches the other's file.

**D12 — The panel guards that raise 403 gain the request they need, and `src/api/routes.py` comes with them.**
`require_admin_panel` (`src/control_panel/routes.py:225`) takes no `Request` today, so its 403 has no route, method or IP; it gains `request: Request` as a FastAPI dependency parameter. `_assert_key_owner`, `_assert_oauth_client_owner` and `_assert_oauth_token_owner` are plain helpers and gain a keyword-only `request` argument — and **`_assert_key_owner` is called from `src/api/routes.py:202`**, so that file changes in the same slice or the build breaks. It also carries an **inline duplicate** of the ownership check at `:224-225` (`revoke_key`, `raise HTTPException(403, "Not your key")`) which the draft missed entirely; that site emits `panel_forbidden` with `reason=not_your_key` too. `verify_csrf` already has the request and reads `request.session.get("user_id")` for the record — without depending on `get_current_user`, which it deliberately does not use.

**D13 — Nothing bounds how many events a caller can cause, and the change says so.**
The draft claimed refusal rows were "already bounded by the quota gate". They are not: `api_keys.daily_request_limit` is NULL by default (unlimited) and OAuth is exempt by construction, so an authenticated read-only client can drive `permission_denied` rows without limit, and unauthenticated transfer refusals never reach the quota gate at all. D7 bounds the **log**; nothing here bounds the **admission**, and no new mechanism is added by this change. That is the sibling change `mcp-rate-limits` (#188/#194): a per-principal request bucket and a failed-authentication budget. Recorded as Residual R2, together with the fact that log retention and shipping are an operator concern this change does not take on.

**D14 — The allow-list sweep is an AST check.**
A regex over `extra={...}` would miss `security_events.emit(...)` keywords entirely and would be defeated by any formatting change. The sweep parses every module under `src/` with `ast`, and for each call whose callee is `logger.<level>`/`logging.<level>` with an `extra=` keyword, or `security_events.emit` (however imported), it collects the field names: the keys of a literal `dict` for `extra=`, and the keyword names for `emit`. A name outside `ALLOWED_FIELDS` fails the test naming file, line and key. **Dynamic expansion fails the test as well** — `extra=some_dict`, `extra={**base, ...}` and `emit(**fields)` are rejected outright, because a field set the sweep cannot read is a field set the allow-list cannot police. (The formatter still drops unknown keys at runtime; the sweep is what makes the drop visible at review time instead of in production.)

## Residuals

- **R1 — In-body tool refusals other than `permission_denied` remain unmarked.** `create_note` on an existing path, path validation, size and conflict refusals return an in-band string and write an ordinary row. A general typed outcome for every tool return is the right long-term shape and is a change of its own; this one adds two markers and narrows its spec requirement to the enumerated set.
- **R2 — Nothing bounds event *admission*.** D13. Deferred to `mcp-rate-limits` (#188/#194). Log retention, shipping and Loki cardinality are operator concerns; this change adds no policy.
- **R3 — Multi-worker.** The suppressor, like the error ring buffer, is per process. Single-worker today (`--workers 1`); under multiple workers each would keep its own counters and the effective per-window cap would multiply by the worker count.
- **R4 — uvicorn's own loggers keep their format**, so the container log is mixed JSON and text (Non-Goals).
- **R5 — `permission_denied` dilutes a write tool's percentiles** (D6), visible on `/admin/usage` and measurable before deciding whether to move the gate.

## Risks / Trade-offs

- [The formatter drops a field somebody needed] → the allow-list is enumerated in one module, the AST sweep names the offending file, line and key, and adding a field is a one-line change plus a bound. Failing closed is the point.
- [Splitting the login failure branch leaks which usernames exist] → only into the **log**; `_render_login(..., status_code=401)` is byte-identical across all three reasons and a test asserts that, so the external oracle is unchanged (#191's "keep the existing constant-response behaviour").
- [The transfer diagnosis query adds a read to a hot refusal path] → it runs only behind `should_emit` (D8), so a flood pays for at most ten reads per subject per minute and an accepted request pays for none.
- [`classify_token_refusal` becomes a second, drifting copy of the admission predicate] → it is explicitly **not** an admission path: it takes no decision, returns no permission, and its reasons are derived from the row's own columns. A test asserts that for every refusal cause the route still returns 404 and that the diagnosis never changes the outcome.
- [Log volume and cost] → INFO events are bounded by the existing rate limits; WARNING/ERROR by D7's two caps. Admission is unbounded (R2).
- [`configure_logging` runs twice, or in a test that already attached the buffer] → idempotent by construction (D1), and the ring handler is exempt in both directions.
- [A record's `stack` is elided] → head and tail are kept with the type line and the final line guaranteed (D16); `make logs` on the container is unchanged for anything longer, and the ring buffer keeps only the first line anyway.
- [`tool_exception` at ERROR fills the 100-entry buffer during a backend outage] → that is the intended signal (the health page should say Ollama is down), and D7 keeps it to ten per minute per subject so other errors survive.
- [Catching `BaseException` around the audit await swallows a cancellation] → scoped to one bookkeeping `await`, re-raises the original exception, and the runtime re-delivers the cancellation at the next suspension point (D11). Pinned by a test.
- [Slowapi's handler signature changes] → the wrapper delegates to `_rate_limit_exceeded_handler` and returns its response unchanged; if the import breaks, the app fails at startup, not at the first 429.
- [A logging call raises inside a request path] → `emit` and `should_emit` catch everything internally and fail open; `error_log.emit` is the precedent. The exception path in `_tracked` is the one that matters most and has its own negative test.

## Migration Plan

**This change claims no migration number. 024 remains unclaimed and is available to whatever needs it next.** `usage_logs.params` is `JSONB` and both markers plus `error_type` ride in it; no DDL, no alembic revision, no `make test-schema` gate beyond the standing `make db-check` (which must still report "No new upgrade operations detected", because nothing touched the schema). Rollback is a code revert — rows already carrying `error: 'tool_exception'` or `'permission_denied'` are read by the existing `params->>'error'` paths as any other post-body marker.

## Pre-code review: where each finding landed

| # | Finding | Where it is addressed |
| --- | --- | --- |
| B1 | "Exactly one record per outcome" contradicts suppression | "What one record per outcome means"; spec R3 and R6 rewritten in those terms |
| B2 | The six transfer reasons are not derivable from the code | D8 (diagnosis pass, `TransferRefusal`, `_load_valid` returns a reason, no tag for a missing token) |
| M1 | Suppressor gives a fresh allowance per bogus token | D7 (subject is `user_id` or the trusted IP; submitted/derived fields are never subjects; second per-subject cap) |
| M2 | "Bounded by the quota gate" is false | D13, Residual R2, and the claim is removed from the spec |
| M3 | `except` misclassifies pre-body and post-body failures | D5 (the guard wraps only `await fn(...)`; completed bodies can never be reported as failed) |
| M4 | "No record contains paths/bodies" is unsatisfiable | D16 (structured fields vs `msg`/`stack`, canary tests) |
| M5 | "Whole traceback AND bounded" is contradictory | D16 (head/tail elision, type line and final line preserved) |
| M6 | "Identifiers only from resolved rows" conflicts with unknown user/client | D15 (resolved / submitted / derived classes; `oauth_token_id` for OAuth rows) |
| M7 | `revoke_grant_family` cannot emit an attributable event | D10 (emit at each HTTP caller after commit) |
| M8 | Slices not disjoint; `src/api/routes.py` uncovered | D12 and the Slice D file list in `tasks.md` (including the inline 403 at `api/routes.py:224`) |
| M9 | Usage page query drops fields and discards malformed values | D9 (three raw selects, precedence mapped in the route) |
| M10 | `_log_usage` swallows failures; cancellation can mask the exception | D11 (explicit status return; `BaseException` scoped to the audit await, original exception preserved) |
| M11 | "Every refused tool call carries a marker" is false | Spec `mcp-request-routing` narrowed to the enumerated markers; Residual R1 |
| m1 | Sweep must be AST-based, both call shapes, no dynamic expansion | D14 |
| m2 | Summary emission lifecycle | D7 (lazy before the next event, shutdown flush, summaries bypass suppression) |
| m3 | Rate-limit fields | `limit_count` + `window_seconds` in the catalogue and `ALLOWED_FIELDS` |
| m4 | Secret-absence tests need canaries | D16 (32-char canaries, ≥12-char substring rule, absent-token tag assertion) |
| nit | Migration 024 | Migration Plan states it explicitly |

**Rejected findings: none.** Two were folded in with a narrower mechanism than the finding proposed, and both are recorded above rather than silently: B2 is answered by a **diagnosis pass after the decision** rather than by threading a typed result through `lookup_token`/`claim_upload`, because re-shaping those two filtered queries would rewrite the single-use linearizability argument the transfer design rests on — the observable contract (a typed refusal with a reason enum, an optional resolved row, a uniform external response) is the one the finding asked for. M10's cancellation half is answered by catching `BaseException` around one bookkeeping `await` rather than by a strict-mode flag on `_log_usage`, because the flag would still have left the cancellation window open.

## Open Questions

None blocking. The three questions the first draft left open were decided by the owner: `permission_denied` stays post-body (Residual R5), uvicorn's loggers are left alone (Residual R4), and `LOG_FORMAT` defaults to `json` in every environment.
