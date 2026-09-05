## Why

The 2026-09-04 OWASP ASVS 5.0 assessment (Claude workflow plus a Codex cross-family pass, every finding independently verified against the running container) left four medium-severity V16 findings open. They are one defect in four places: **this server cannot tell an operator what happened to it.** Production is multi-user and the MCP consumer is an agent, so the two expensive failures named in `CLAUDE.md` — a destructive write and a silently wrong result — are exactly the ones that leave no trace today.

- **#190 — the intended log format never applies.** `src/mcp_server/server.py:91` constructs `FastMCP` at import time; the SDK's `configure_logging()` installs a `RichHandler` on the **root** logger before `src/main.py:32` runs its `logging.basicConfig`, which is therefore a no-op. Every one of the ten `auth_failure` records in `src/mcp_server/auth.py` renders as `WARNING auth_failure auth.py:143` with `extra={reason, key_prefix}` **dropped**; timestamps are local time with no offset; long records wrap at console width and tracebacks are boxed, so Alloy's per-line Docker ingestion puts fragments into Loki. An operator watching a credential-stuffing burst sees N identical lines with no reason, no token tag and no user id.
- **#191 — authentication decisions are unlogged.** `src/auth/routes.py` and `src/oauth/routes.py` contain no logger at all. Panel login success and failure, OAuth issuance, refresh, every `/token` refusal, `/authorize` consent and its refusals, DCR registrations, `/revoke` outcomes and every slowapi 429 leave nothing behind. The OAuth token endpoint is the primary authentication surface for third-party AI clients here, and no record ties a refusal or an issuance to a `client_id` or a user.
- **#192 — authorization refusals are indistinguishable.** `_require_write` (`src/mcp_server/tools.py:1213`) returns a string; the `usage_logs` row written for that call is **shaped exactly like a successful write**, so `/admin/usage` shows a read-only credential apparently writing. The transfer routes answer every unusable capability token with the uniform 404 and log nothing server-side, so token enumeration is invisible. Panel 403s (`routes.py:234, 713, 1055, 1067`), the CSRF 403 (`csrf.py:57`) and the OAuth cross-user 403 (`oauth/routes.py:528, 570`) are all silent.
- **#193 — tool exceptions vanish.** `_tracked`'s wrapper has `try`/`finally` and **no `except`**: an exception from the tool body skips `_log_usage` entirely (probe: 0 usage rows), and the SDK converts it to an error result with no logger call. A write tool that fails halfway leaves no audit row and no log line, and the health page's ERROR ring buffer never sees it.

## What Changes

- **One logging configuration, applied after the SDK has had its way.** A new `src/logging_setup.py` owns `configure_logging()`: it removes and closes every root handler the SDK installed — **except a handler `src/services/error_log.py` owns** — installs one `StreamHandler(sys.stderr)`, and formats each record as **one line of JSON** with a UTC ISO-8601 `ts`. It is called from `src/main.py` immediately after the import block and from `src/mcp_stdio.py`, so no entry point can inherit the SDK's handler. `LOG_LEVEL` and `LOG_FORMAT` (`json` default, `text` for local work) become settings.
- **An allow-list, not a dump of `extra`.** The formatter emits a fixed, typed, length-bounded set of field names and **drops everything else**. Presented credential material never appears: identifiers may come only from a resolved database row, and anything derived from a bearer value appears solely as `token_tag`, the `sha:xxxxxxxx` SHA-256 tag `_redacted_prefix` already produces — which moves to the shared module so there is one definition. `key_prefix` is removed from the allow-list on purpose, so a future call site that logs a raw prefix logs nothing.
- **One structured record per authentication outcome** (#191): panel login success/failure with a reason code (the single merged failure branch is split into `unknown_user` / `inactive_user` / `bad_password` — **the 401 response is unchanged**), logout, bootstrap, admin password reset, OAuth issuance/refresh/refusal (every `/token` branch carries an RFC error code plus a sub-reason), consent granted/denied, `/authorize` refusals, DCR registration and its refusals, `/revoke` success (with the rowcount `revoke_grant_family` currently discards), its RFC 7009 §2.2 no-ops and its one real refusal, the swallowed rotation `server_error` at `oauth/routes.py:916`, and every slowapi 429 — logged once, centrally, by wrapping `_rate_limit_exceeded_handler` at `src/main.py:299`. No password, client secret, authorization code, access/refresh token or PKCE verifier is ever a field.
- **Every authorization refusal gets a record and, where it is a tool call, a usage marker** (#192): `_require_write` records `params.error = 'permission_denied'` through the `timing` holder **at its single definition**, so all nine call sites inherit it, and logs one WARNING with tool and actor; the transfer routes log every `_not_found()` with the redacted `token_tag`, the trusted client IP and the reason the response deliberately withholds, **keeping the uniform 404 byte-identical**; panel, CSRF and OAuth 403s log route, method and user id.
- **A tool body exception is recorded, then re-raised** (#193): `_tracked` gains `except Exception` — **never `BaseException`**, so `CancelledError` and shutdown pass through untouched — which logs at ERROR with `exc_info` (so the ring buffer and the health page see it), writes a **best-effort** `usage_logs` row carrying `error: 'tool_exception'` and `error_type`, and re-raises. A failing audit write is caught and logged; it can never mask the original exception.
- **Refusal logging is rate-limited.** A shared suppressor emits at most N records per event per subject per window and then one `events_suppressed` summary, so a hostile client cannot flood Loki or evict the 100-entry error buffer. It never suppresses a `usage_logs` row.
- **`/admin/usage` shows the outcome**, so a refused write is distinguishable from a successful one on the page, not only in the JSON.

**No migration.** `usage_logs.params` is `JSONB`; both new markers ride in it. Migration **024** stays unclaimed by this change.

## Capabilities

### New Capabilities

- `security-event-logging`: the log configuration and its field allow-list, the authentication and authorization event catalogue, the tool-exception record, and denial-event suppression.

### Modified Capabilities

- `mcp-request-routing`: the "refused tool call is recorded" requirement widens from the vault-admission refusal to every refusal and every failed body; a raising tool body now leaves a usage row.
- `panel-performance-views`: the two new markers are declared **post-body** and MUST NOT enter the pre-body refusal predicate.
- `panel-usage-slicing`: the request log gains an outcome column derived from the row's marker.
- `file-transfer`: capability-token refusals are recorded server-side; the uniform 404 is unchanged (added requirement).
- `panel-ops-health`: the error ring buffer survives logging reconfiguration (added requirement).

## Impact

- New: `src/logging_setup.py`, `src/services/security_events.py`, `docs/architecture/security-event-logging.md`.
- Changed: `src/main.py`, `src/mcp_stdio.py`, `src/config.py`, `src/services/error_log.py`, `src/mcp_server/auth.py`, `src/services/vault.py`, `src/services/quotas.py`, `src/auth/routes.py`, `src/oauth/routes.py`, `src/oauth/grants.py`, `src/mcp_server/tools.py`, `src/services/usage_stats.py`, `src/csrf.py`, `src/control_panel/routes.py`, `src/control_panel/users.py`, `src/transfer/routes.py`, `src/services/usage_filters.py`, `src/control_panel/templates/usage.html`.
- Docs: `CLAUDE.md` (index row), `docs/architecture/usage-attribution.md` (marker register), `docs/architecture/control-panel.md` (buffer survival, usage outcome), `docs/architecture/file-transfer.md` (refusal logging beside the uniform 404).
- No schema change, no migration, no new dependency. Log volume rises; the suppressor bounds the hostile case.
- Closes #190
- Closes #191
- Closes #192
- Closes #193
