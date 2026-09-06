# Security event logging

What this server can tell an operator about what happened to it. Read it before
touching `src/logging_setup.py`, `src/services/security_events.py`, or any call
site that logs a refusal.

The short version: **one configuration applied after the SDK has had its way,
one allow-list of fields, one allowance check per record, one redaction
function.** Everything below is why each of those is one and not several.

## The SDK wins the import race, so the fix has to run late

`src/main.py` imports `src.mcp_server.server`, whose module-level
`FastMCP(...)` calls the MCP SDK's own `configure_logging()`, which runs
`logging.basicConfig(handlers=[RichHandler(...)], format="%(message)s")` on the
**root** logger — at import time. `basicConfig` is a documented no-op once the
root has a handler, so the `logging.basicConfig(...)` that used to sit below
that import block never applied.

The consequences were the whole of #190: every `extra=` this server passed was
dropped on the floor, timestamps were local with no offset, long records wrapped
at console width and tracebacks were boxed, so Alloy's per-line Docker ingestion
put fragments into Loki. An operator watching a credential-stuffing burst saw N
identical `WARNING auth_failure auth.py:143` lines with no reason, no token tag
and no user id.

`src/logging_setup.configure_logging()` therefore runs **after** the import
block in `src/main.py`, and again from `src/mcp_stdio.py` — where the handler's
stderr destination matters most, because stdout is the MCP protocol channel.

### Why not `basicConfig(force=True)`

`force=True` removes **and closes** every handler already on the root. That
includes the one `src/services/error_log.py` owns: the 100-entry ERROR ring
buffer behind the panel's health page. Today the ordering happens to save us —
the SDK configures at import time, `error_log.attach()` runs in the lifespan —
but "happens to" is not a guarantee, the lifespan is re-entered in tests, and
the failure would be silent: the health page would simply stop filling.

So the reconfiguration is an explicit loop that removes and closes every root
handler **except** the instance `error_log.installed_handler()` returns. Same
effect, with the exception written down. Either call order is safe,
`error_log.attach()` stays idempotent, and "the health page keeps working" is a
property of the code rather than of an import order. Both orders are tested.

## The field policy: three disjoint name spaces

`extra` is attacker-influenced — one of the ten `auth_failure` sites derives its
value from a *presented bearer token* — so the formatter never serialises
`record.__dict__` minus the standard keys. One careless `extra={"path": path}`
away, that design puts one tenant's vault paths into a shared sink.

* **`FORMATTER_OWNED`** = `ts`, `level`, `logger`, `msg`, `stack`. Produced by
  the formatter from the `LogRecord`. A call site that passes one has it
  dropped (and `security_events` raises under the test suite's strict flag), so
  a caller cannot forge a timestamp, a level or a traceback.
* **`EMITTER_CONTROL`** = `permit`, `event`, `subject`, `level`, `exc_info`.
  Consumed by `acquire`/`emit`, never rendered as data. `level` is deliberately
  in both sets: it is a control keyword the formatter renders from the record,
  and is never a field.
* **`ALLOWED_FIELDS`** — everything a call site may pass, each with a declared
  type and, for strings, a maximum length. The list lives in
  `src/logging_setup.py`.

**A value that fails its type check is dropped, never converted.**
`user_id="not-an-int"` yields a record with *no* `user_id`, not one whose
`user_id` is a string: a reader who has to ask "is this integer field an integer
today?" cannot query the field at all, and a silent type change is how a Loki
dashboard starts lying. Truncating an over-long string is not a conversion and
stays. Booleans are not integers here, whatever `isinstance` says.

`key_prefix` is deliberately **absent** from the allow-list: the name invites
logging a raw `omcp_` prefix of a presented token, and dropping the field is a
safer failure than shipping it. **`actor_ref` is absent for the same reason**,
and was removed after it shipped: `usage_logs.actor_ref` holds
`api_keys.key_prefix` for an API-key caller — the first twelve characters of the
live key — and `tool_write_refused` / `tool_exception` were emitting it beside a
traceback. The rule is now absolute: **no security record carries a substring of
a credential**, only `token_tag` (a SHA-256 prefix of a *presented* value) and
row ids (`key_id`, `oauth_token_id`, `grant_id`). `usage_logs` keeps its actor
columns unchanged — that is the #77 attribution design, and those rows are read
behind the panel's own authentication rather than shipped to a log sink. Nothing free-text is allow-listed except
`reason`, which is a closed vocabulary per event. There is no field a path, a
query string or a request body could ride in — `route` is `request.url.path`
only.

`error_type` is the one dual name: the formatter derives it whenever `exc_info`
is set and a call site may pass it otherwise; **when both are present the
exception wins**, because the class of the exception being logged is a fact and
the passed value is a claim.

### Provenance is a property of the (event, field) pair

A *successful* login logs a username that resolved; a failed one logs a username
that did not. So the name carries the role:

* an **unsuffixed** identifier (`user_id`, `username`, `client_id`, `key_id`,
  `oauth_token_id`, `grant_id`, `scope`, `actor_kind`) may hold only a value
  read from a database row;
* a **`_submitted`** name (`username_submitted`, `client_id_submitted`,
  `client_name_submitted`) is the only place a caller-supplied identifier
  appears, is truncated **to 64 characters**, and is **never** a suppression
  subject. The bound is a written-down accepted limitation, not a guarantee: a
  caller who pastes a live credential into a username, a client id or a client
  name field has up to 64 characters of it logged. The fields stay because an
  operator watching a credential-stuffing burst has to see what was *tried* —
  a refusal record that withheld the attempted username answers none of the
  questions such a burst raises — and the bound is the mitigation;
* a **`_session`** name (`user_id_session`, `username_session`) is the only
  place a value copied from the session cookie without a database read appears
  — which is exactly what a logout record can honestly say, because the account
  may have been renamed or deleted since sign-in and a logout must not pay for a
  lookup to notice;
* `key_id` names an `api_keys` row and `oauth_token_id` an `oauth_tokens` row.
  The OAuth branches of `auth_failure` used to put the latter in the former,
  which made the two indistinguishable in a query.

**`actor_user_id` is who acted; `user_id` is who the record is about.** On every
surface where one account can act on another's resource — panel user
administration, OAuth grant/client/token administration, key administration, the
cross-user refusals — `actor_user_id` is present *even when the two are equal*,
so a query for "everything this administrator did" is complete rather than
silently missing self-actions. Where the subject *is* the actor (login, logout,
consent, a token exchange, a tool call) only `user_id` is emitted.

**Every field is optional.** `EVENT_FIELDS` declares the *permitted* set per
event, not a required one: a record missing a field means the emitting path did
not have it, and absence is meaningful.

### The single redaction

`security_events.redacted_token_tag(value)` — `"sha:" + sha256(value)[:8]` — is
the only function that turns a presented credential into something loggable, and
`token_tag` is the only field it may occupy. `src/mcp_server/auth.py`'s
`_redacted_prefix` delegates to it so there is one definition and one test.

When no credential was presented the field is **absent**, not `sha:` of the
empty string: a constant that looks like a tag on every credential-less request
is worse than nothing, because an operator would correlate on it.

### Messages and tracebacks have their own rule

They are not structured fields:

* **`msg`** is a developer-authored constant or format string. It MAY
  interpolate operational context, including a vault-relative path, exactly as
  the existing `move_note` warnings do. It MUST NOT interpolate credential
  material — a password, secret, code, token, verifier, cookie or CSRF token.
  It is bounded (`MAX_MESSAGE_CHARS`).
* **`stack`** appears only with `exc_info`. An exception message is not under
  this change's control and is accepted as operational text under the same rule
  — which is exactly why **`src/database.py` sets `hide_parameters=True`**.
  SQLAlchemy renders a failing statement's bound parameters into
  `StatementError.__str__`, and this server binds credential material on half a
  dozen hot paths: the API-key and OAuth-token lookups bind a SHA-256 key hash,
  the transfer admission binds a token hash, DCR binds a client-secret hash, the
  authorization-code exchange binds a code hash, and the refresh rotation binds
  the hash of the pair it is minting. Without that flag a pool timeout or a
  statement timeout on any of them put the hash into the record's `stack` and
  into the health page's ERROR ring buffer. It is a security setting, not a
  debugging one: do not turn it off to inspect a query.
* **A catalogue event on a credential-bound write does not carry `exc_info` at
  all.** `oauth_token_rotation_failed` and
  `oauth_refresh_reuse_revocation_failed` are class-only for that reason —
  belt to `hide_parameters`' braces. The events that *do* carry a traceback
  (`tool_exception`, the two transfer publish failures, the panel's health
  strip) sit on paths that bind row ids and note content, never a credential.
* **Bounding is head-and-tail, not truncation.** "The whole traceback, bounded"
  is a contradiction. `stack` keeps the first 4 KiB and the last 3 KiB with a
  marker naming the dropped byte count between them, and the exception's **type
  line and the traceback's final line are guaranteed present**: if elision would
  have removed them they are appended after the tail. Each appended line is
  itself bounded (`STACK_GUARANTEED_LINE_BYTES`, 1 KiB) — otherwise a
  megabyte-long exception message would unbound the record through the very
  guarantee that exists to keep it readable. A traceback under the budget is
  emitted whole.

Compliance is verified against real request paths with high-entropy canaries,
not by inspection: **submitted** canaries planted in every caller-controlled
credential position, and **captured** secrets the server itself generated (a
generated secret cannot be planted). See `tests/test_issue_191_secret_canaries.py`.

### The allow-list cannot drift from the call sites

An AST sweep (`tests/test_issue_190_field_allowlist.py`) parses every module
under `src/` and collects field names from `logger.<level>(..., extra={...})`
and from `security_events.emit(...)` keywords — excluding the emitter's control
keywords, rejecting formatter-owned names, and failing on any name outside
`ALLOWED_FIELDS` or outside that event's `EVENT_FIELDS` where the event name is
a literal. **A dynamic field set fails too**: `extra=some_dict`,
`extra={**base}` and `emit(**fields)` are rejected outright, because a field set
the sweep cannot read is a field set the allow-list cannot police. A regex over
`extra={...}` would have missed the emitter's keywords entirely.

`src/services/security_events.py` is the one exempt module: it is the emitter,
its single `logger.log(..., extra=fields)` passes a dict it has already policed
at runtime, and the rest of that test file is the check on it.

## The event catalogue

`EVENT_FIELDS` in `src/services/security_events.py` mirrors this table, one
entry per row, and a test asserts neither side has a row the other lacks. Four
fields are on every record and are not repeated below: `ts`, `level`, `logger`,
`msg` (the event name). `stack` rides on `exc_info`, not as a field.

`client_ip` is listed per event rather than assumed. The MCP tool events omit
it: `_tracked` and `_require_write` run below `ProxyHeadersMiddleware` and
nothing binds the request address into a ContextVar (residual R8).

| Event | Level | Fields | Emitted where |
| --- | --- | --- | --- |
| `panel_login_succeeded` | INFO | `client_ip`, `route`, `user_id`, `username` | `src/auth/routes.py`, after the `last_login_at` commit |
| `panel_login_failed` | WARNING | `client_ip`, `reason`, `route`, `user_id`, `username_submitted` | `src/auth/routes.py`; `reason` is `unknown_user` / `inactive_user` / `bad_password`, and the 401 is byte-identical across all three |
| `panel_logout` | INFO | `client_ip`, `user_id_session`, `username_session` | `src/auth/routes.py`, read before `request.session.clear()` |
| `panel_bootstrap_admin_created` | INFO | `client_ip`, `user_id`, `username` | `src/auth/routes.py`, after the commit |
| `panel_bootstrap_refused` | WARNING | `client_ip`, `reason` | `src/auth/routes.py`, each refusal branch |
| `panel_password_reset` | INFO | `actor_user_id`, `client_ip`, `route`, `user_id`, `username` | `src/control_panel/users.py`, after the commit |
| `password_hash_malformed` | WARNING | `user_id` | `src/auth/passwords.py` — a caller can drive it through the login form |
| `panel_session_touch_failed` | WARNING | `error_type`, `reason`, `route`, `user_id` | `src/auth/session.py` `touch_session` — the `last_seen_at` write, or the rollback after it, failed; `reason` is the stage (`touch` \| `rollback`). Bounded because a failing write records no new `last_seen_at` to throttle against, so a stale browser drives one per `GET` |
| `panel_session_replay_refused` | WARNING | `client_ip`, `reason`, `route`, `token_tag`, `user_id` | `src/auth/session.py` `get_active_session_user`, every refusal branch; `reason` is `no_session_id` / `unknown_session` / `revoked_session` / `expired_session` / `user_mismatch` / `user_missing` / `user_inactive` / `version_mismatch` — **all eight**, because every one of them clears the cookie and so signs a browser out mid-session. **Never** the cookie's session identifier and **never its stored SHA-256** — that digest is `user_sessions.id`, so a record carrying it names a specific live session. `token_tag` is the only form a session may appear in |
| `panel_sessions_revoked` | INFO | `count`, `reason`, `user_id`, `user_id_session` | after the commit that made it true; `reason` is `logout` / `password_change` / `admin_password_reset` / `user_deactivated` / `user_deleted`. Logout passes `user_id_session` (copied from the cookie, no row read); the account-event callers hold a row and pass `user_id` |
| `panel_session_revocation_failed` | ERROR | `client_ip`, `error_type`, `reason`, `route`, `user_id_session` | `src/auth/routes.py` `logout`, when the revocation write or the rollback after it failed. The cookie is still cleared and the redirect still happens. **No `exc_info` and no `str(exc)`**: a SQLAlchemy error renders the failing statement and its bound parameters, one of which is the stored session hash |
| `panel_password_changed` | INFO | `client_ip`, `route`, `user_id`, `username` | `src/control_panel/routes.py` `change_password` (#197), **after** the commit that carries the new hash, the `session_version` bump and the revocation of every session of that user |
| `panel_password_change_refused` | WARNING | `client_ip`, `reason`, `route`, `user_id` | `src/control_panel/routes.py` `change_password`, every refusal that reaches the handler; `reason` is `wrong_current_password` / `too_short` / `mismatch` / `same_as_current` / `nul_byte` / `account_inactive`. The two credential branches are distinguished here and **not** in the response, which carries one constant message. A request either rate limit rejected never reaches the handler and is recorded by `rate_limit_exceeded` instead |
| `panel_session_reissue_failed` | ERROR | `client_ip`, `error_type`, `reason`, `route`, `user_id` | `src/control_panel/routes.py` `change_password`, when the mint that follows the committed change raised. The change is not rolled back and the browser is signed out. **No `exc_info` and no `str(exc)`**, for `panel_session_revocation_failed`'s reason: the rendered statement's bound parameters include a stored session hash |
| `oauth_token_issued` | INFO | `client_id`, `client_ip`, `grant_id`, `reason`, `scope`, `user_id` | `src/oauth/routes.py` `/token`, after the mint's commit |
| `oauth_token_refreshed` | INFO | `client_id`, `client_ip`, `grant_id`, `scope`, `user_id` | `src/oauth/routes.py` `/token`, after the rotation's commit |
| `oauth_token_refused` | WARNING | `client_id`, `client_id_submitted`, `client_ip`, `grant_id`, `reason`, `user_id` | every `/token` refusal; `reason` is `<rfc_code>.<sub_reason>` |
| `oauth_token_rotation_failed` | ERROR | `client_id`, `client_ip`, `error_type`, `grant_id` | the `except Exception` whose traceback is discarded behind a 500 |
| `oauth_refresh_reuse_detected` | WARNING | `client_id`, `client_ip`, `grant_id`, `revoked_tokens`, `user_id` | `src/oauth/routes.py` `/token`, when a replayed refresh token killed its whole family (#182) |
| `oauth_refresh_reuse_revocation_failed` | ERROR | `client_id`, `client_ip`, `error_type`, `grant_id`, `user_id` | the same branch when the revocation write failed. **No `exc_info`**: a SQLAlchemy error renders the failing statement and its bound parameters, one of which is the token hash |
| `oauth_consent_granted` | INFO | `client_id`, `client_ip`, `scope`, `user_id` | `/authorize`, after the code row commits |
| `oauth_consent_denied` | INFO | `client_id`, `client_id_submitted`, `client_ip`, `user_id` | `/authorize` deny |
| `oauth_authorize_refused` | WARNING | `client_id`, `client_id_submitted`, `client_ip`, `reason`, `user_id` | every `/authorize` refusal |
| `oauth_cross_user_client_refused` | WARNING | `actor_user_id`, `client_id`, `client_ip`, `route`, `user_id` | `_cross_user_client_error`, the one helper both call sites return |
| `oauth_client_registered` | INFO | `client_id`, `client_ip`, `client_name_submitted`, `count`, `scope` | DCR, after the commit. **Never** the client secret |
| `oauth_client_registration_refused` | WARNING | `client_ip`, `reason` | DCR refusals |
| `oauth_grant_revoked` | INFO | `actor_user_id`, `client_id`, `client_ip`, `count`, `grant_id`, `route`, `user_id` | each HTTP caller, after **its own** commit — never inside `revoke_grant_family` |
| `oauth_revoke_noop` | INFO | `client_id_submitted`, `client_ip`, `reason` | RFC 7009 §2.2 — the response says nothing, so the log must |
| `oauth_revoke_refused` | WARNING | `client_id`, `client_ip`, `reason` | `/revoke` client-auth failure |
| `rate_limit_exceeded` | WARNING | `client_ip`, `limit_count`, `method`, `route`, `window_seconds` | `src/main.py`, a local wrapper around slowapi's handler — the one hook every 429 passes through |
| `auth_failure` | WARNING | `client_ip`, `key_id`, `oauth_token_id`, `reason`, `route`, `token_tag` | `src/mcp_server/auth.py`, all ten sites |
| `auth_failure_rate_limited` | WARNING | `client_ip`, `limit_count`, `route`, `window_seconds` | `APIKeyMiddleware`, when an address is over its failed-authentication budget (#194). **One per slot per window**, on the first refusal: every later one in the same window is the same fact. No `token_tag` and no `reason` — nothing was looked up and no credential was read |
| `tool_write_refused` | WARNING | `actor_kind`, `key_id`, `oauth_token_id`, `tool`, `user_id` | `_require_write`, the single definition all nine call sites reach |
| `tool_body_outcome` | WARNING | `tool`, `reason`, `outcome`, `user_id`, `key_id`, `oauth_token_id` | One terminal typed body result in `_tracked` (#263). Closed marker/disposition only; no paths, hashes, prose, note content or capabilities. Existing specific authorization/publication events remain separate facts. The ordinary suppressor bounds these events, and telemetry failures cannot change the completed result. |
| `mcp_concurrency_pressure` | WARNING | `reason`, `outcome`, `limit_count`, `method`, `route`, `client_ip`, `user_id`, `key_id`, `oauth_token_id` | Reserved for #261 transport concurrency integration; reason is closed stage/scope, outcome shadow/refused. No bearer fingerprint or token. Separate from the #263 body-outcome contract. |
| `tool_exception` | ERROR | `actor_kind`, `duration_ms`, `error_type`, `key_id`, `oauth_token_id`, `tool`, `user_id` | `_tracked`'s `except Exception` around **only** `await fn(...)` |
| `tool_usage_log_failed` | WARNING | `error_type`, `tool` | the same handler, when the best-effort `usage_logs` write reports failure |
| `tool_telemetry_failed` | WARNING | `error_type`, `tool` | `_tracked`'s post-body tail — `named_params`, result sizing, the `usage_logs` await — when it raises **after** the body completed. The completed result is returned unchanged; never `tool_exception`, because the call succeeded |
| `tool_refused_no_vault` | WARNING | `tool`, `user_id` | the vault admission gate |
| `tool_refused_vault_quarantined` | WARNING | `reason`, `tool`, `user_id` | the same admission gate, for a vault-root quarantine (#199); `reason` is `overlap`, `root_unexaminable` or `snapshot_not_ready`. Distinct from `tool_refused_no_vault` because that one says the credential has no vault and this one says it has one the server will not serve. No peer username and no path in any field — the caller-facing refusal names no other tenant, and the operator surfaces are where the pair is named |
| `tool_refused_over_quota` | WARNING | `day`, `key_id`, `limit`, `tool`, `user_id` | the quota admission gate |
| `tool_refused_rate_limited` | WARNING | `key_id`, `limit`, `oauth_token_id`, `reason`, `tool`, `user_id` | either per-principal token bucket (#188, #194); `reason` is the bucket — `principal` or `principal_write` — and is the same string the caller-facing refusal carries as its `scope`, so the log and the agent name one control. The `usage_logs` half of the same refusal is coalesced separately; the two bounds are independent |
| `usage_log_credential_gone` | WARNING | `cleared_user_id`, `tool` | the FK-recovery retry in `_log_usage` |
| `usage_log_failed` | WARNING | `error_type`, `reason`, `tool` | `_log_usage` giving up; `reason` is `initial` or `after_clearing_fks` |
| `tool_result_measure_failed` | WARNING | `error_type`, `tool` | result telemetry |
| `move_rewrite_failed` | WARNING | `error_type`, `tool` | `move_note`'s link rewrite failing for one source, and the confirmation outage that stops the remaining ones; the move carries on or stops, but the rename stands. The path is named in the tool's reply and in the move's `params`, not in a field |
| `move_rewrite_overlap_refused` | WARNING | `error_type`, `tool` | `move_note` aborting the **whole** move because one source holds a link nested inside another link to the same note (#211). Distinct from `move_rewrite_failed`: nothing was mutated |
| `move_post_rename_failed` | WARNING | `error_type`, `reason`, `tool` | the two best-effort failures after the rename has stood; `reason` is `title_read_failed` or `db_update_failed`. Neither fails the call |
| `quota_admission_failed` | ERROR | `day`, `error_type`, `key_id` | `src/services/quotas.py` — re-raised unchanged; a quota that has stopped deciding is an incident |
| `quota_counter_prune_failed` | WARNING | `error_type` | the same module's housekeeping prune |
| `publication_refused_confirmation_unavailable` | WARNING | `error_type`, `user_id` | `src/services/vault.py` — the assignment is *unknown*, which is not "changed" |
| `publication_refused_vault_assignment_changed` | WARNING | `reason`, `user_id` | `src/services/vault.py` |
| `transfer_refused` | WARNING | `client_ip`, `error_type`, `key_id`, `method`, `oauth_token_id`, `reason`, `route`, `token_tag`, `user_id` | every `_not_found()` in `src/transfer/routes.py`; the 404 stays byte-identical. `reason` includes `owner_quarantined` and `root_unverified`, which the root check used to collapse into `root_reassigned` — a different fault with a different fix |
| `transfer_refused_rate_limited` | WARNING | `key_id`, `limit`, `method`, `oauth_token_id`, `reason`, `route`, `user_id` | `PUT /transfer/upload` refused by the **minting** principal's write bucket (#194). Not a `transfer_refused` reason: that one accompanies the uniform 404 and means the token is unusable, this one means it is usable and the minter's write rate is spent — a 429 the same link survives. `reason` is the bucket scope, the same string `tool_refused_rate_limited` carries |
| `transfer_refused_mount_boundary` | ERROR | `error_type`, `method`, `route` | `src/transfer/routes.py` |
| `transfer_refused_unsupported_fs` | ERROR | `error_type`, `method`, `route` | `src/transfer/routes.py` |
| `transfer_root_unusable` | ERROR | `error_type`, `method`, `route`, `user_id` | `src/transfer/routes.py` |
| `transfer_post_publish_failure` | ERROR | `error_type`, `route`, `user_id` | published but not recorded |
| `transfer_prepublish_failure` | ERROR | `error_type`, `route`, `user_id` | failed before publication |
| `transfer_claim_release_failed` | ERROR | `error_type`, `method`, `route`, `user_id` | returning a claimed token to `pending` failed. Best-effort by construction: the response was already decided, so it goes out unchanged and the claim stands until its TTL |
| `panel_forbidden` | WARNING | `actor_user_id`, `actor_username`, `method`, `reason`, `route`, `user_id` | the panel's 403 guards, the REST duplicate ownership check, and the actor re-checks |
| `csrf_refused` | WARNING | `client_ip`, `method`, `route`, `user_id` | `src/csrf.py` `verify_csrf` |
| `panel_ondemand_index_failed` | ERROR | `error_type`, `user_id` | the panel's on-demand index action |
| `panel_ondemand_embed_failed` | ERROR | `error_type`, `user_id` | the panel's on-demand embed action |
| `panel_health_strip_failed` | ERROR | `error_type` | the dashboard health strip's read and rollback failures |
| `events_suppressed` | *the suppressed event's own level* | `count`, `reason`, `window_seconds` | the suppressor itself; `reason` names the suppressed event |

### What stays on the bare logger, and why

A site emits through `security_events` when **a caller can trigger it
repeatedly, on demand, through a request** — otherwise a direct `logger.*` call
is an unbounded flood channel beside the bounded one. Everything driven by the
indexer, the embed pass, `vault_fs` housekeeping and startup stays on the bare
logger: those are background or once-per-pass, they are not refusals, and
suppressing them would hide the one class of error the health page exists to
show.

**A test enforces this in the four request-path modules** —
`src/mcp_server/auth.py`, `src/mcp_server/tools.py`, `src/transfer/routes.py`
and `src/control_panel/routes.py`. `tests/test_issue_190_field_allowlist.py`
parses each of them and fails on any `logger.warning`, `logger.error`,
`logger.exception` or `logger.critical` call, matched on the literal message so
the exemption list survives reformatting. `logger.info` and `logger.debug` are
out of scope: they sit below the sink's default level and they are not
refusals.

The exemptions are all **one shape**, and that shape is the rule rather than a
hole in it: the panel's **Danger zone**. The "Skipping HNSW index" notice in
`reset_embeddings`, and the two abort notices in
`_record_embedding_fingerprint`, are each reached only through
`require_admin_panel` (the re-embed route additionally through a signed
one-time confirmation token), fire at most once per action, and report exactly
the operational fact the health page exists to show — that `semantic_search`
has silently fallen back to a sequential scan, or that a destructive reset
aborted and rolled itself back. No credential can drive any of them in a loop,
so there is no flood to bound; and the abort notices keep `logger.exception`
deliberately, because the traceback saying *why* the fingerprint write failed
is the whole value of the record, and a bound that withheld it would leave an
administrator with a flash message and nothing else.

The second shape is **background and post-publication work**:
`src/services/vault_overlap.py`'s detection pass, and the flush, close and
staging-cleanup helpers in `src/services/vault_fs.py`, `src/services/vault.py`
and `src/services/transfer.py`. The detection pass is background or
admin-guarded; the flush and close helpers are reached by a foreground tool
call, and are exempted on an owner decision recorded below as **R10**.

**R10 — post-publication filesystem failures.** `flush_dir_quietly` and
`flush_publication_ancestors_quietly` fire when a directory `fsync` returns
`EIO`/`EINVAL` after a create, edit, move or delete has *already published*;
`_close_quietly` fires up to three times per transfer publication when `close`
returns `EIO`; `_unlink_quietly` and `discard_staged_name` clean up staging
after a write has landed or aborted. Every one of them runs when the vault's
own filesystem is failing and the operation has already stood, so the tool
result is **success** and the record is a durability note, not a refusal.
They stay on the bare logger, with the decision quoted verbatim at each site:

> post-publication filesystem failure on a successful write; class-only,
> bounded by the write bucket; not routed through the suppressor to keep the
> destructive publication path unchanged

**The consequence, stated rather than glossed:** an agent writing into a vault
whose filesystem is returning `EIO` produces one warning per successful write —
three per publication on the transfer path — with nothing but
`mcp-rate-limits`' write bucket (#188/#194) to bound it. The trade is
deliberate. The alternative is threading an emission through the destructive
publication path, which is the one path in this codebase that has clobbered a
note, to bound records that tell the caller nothing it can act on. If the
volume matters before that bucket lands, the answer is a counter at the flush
helpers rather than an event inside the publish.

Each entry carries its justification in the test, and the list itself is
asserted — a new exemption is a decision somebody has to write down, not a
line somebody can add. So is the *scope*: round 2's three findings and round
3's two were all "a sibling change added a call to a module the list did not
name", so the guarded-module list is asserted too.

## The suppressor: one allowance check, on a subject a caller cannot mint

```
permit = security_events.acquire(event, subject)   # charges the allowance ONCE
security_events.emit(permit, **fields)             # consumes it; no second check
security_events.emit(event, subject=…, **fields)   # acquire + consume, the usual form
```

There is no `should_emit`. An earlier design had one, and a caller that
consulted it and then emitted was charged **twice**, so the flood bound was
either doubled or violated depending on which check won. The permit form exists
for the one call site that must do work to build its fields — the transfer
refusal diagnosis, which runs a read-only query *after* the refusal decision and
only when the record will actually be emitted.

**A caller that acquires a permit and does not spend it has spent its slot
anyway.** That is the fail-safe direction: it can only make the log quieter,
never noisier.

* **The subject** is the resolved `user_id` when the request already has one,
  otherwise the **trusted** client address (`request.client.host`, after
  `ProxyHeadersMiddleware`, never a header), otherwise `-`. It must be
  computable *before* any work the permit gates, which is why `transfer_refused`
  always keys on the address even where a row later resolves: the owner is
  knowable only after the diagnosis the permit is supposed to gate.
* **Caller-supplied values are never subjects** — not `token_tag`, not
  `username_submitted`, not `client_id_submitted`. Rotating credentials must not
  mint fresh allowances, which is exactly what a per-token subject would do.
* **Two caps.** `MAX_EVENTS_PER_WINDOW` = 10 per `(event, subject)` per 60 s,
  and `MAX_EVENTS_PER_SUBJECT_PER_WINDOW` = 50 per subject across all events, so
  a source cycling through twenty refusal events cannot multiply its allowance
  by twenty.
* **Every level is accounted, INFO included.** Bounded volume beats audit
  completeness: a summary carrying an exact count still answers "how many logins
  succeeded", while a quiet log answers nothing. Unauthenticated INFO paths — a
  replayed consent, a logout, a CSRF-failing form — are otherwise unbounded on
  routes no rate limit covers.
* **No count is lost.** A window's summary is emitted lazily when the next
  `acquire` for that key arrives after the window closed; **an entry holding a
  nonzero withheld count emits its summary before it is evicted**; and anything
  outstanding is flushed at shutdown (the lifespan for the app, `atexit` for the
  stdio entry point). No timer task.
* **A summary carries the suppressed event's own level**, so an operator
  filtering at WARNING still sees that warnings were withheld. It is never
  itself suppressed and never counted.
* **Bounded and safe.** At most 512 keys per map, evicted **least recently
  used** — every `acquire` moves its key to the end of an `OrderedDict` and
  eviction pops from the front, so what goes is the key nobody has touched for
  longest, which is not the same as the oldest *window* (a key acquired
  steadily keeps a fresh window and never reaches the front). The guarantee
  that matters is unchanged and is asserted separately: **an entry holding a
  nonzero withheld count emits its summary before it is evicted**
  under the rule above; a `threading.Lock` guards both; `acquire` catches
  everything and **fails open** — an internal error returns a permit, so the
  record is emitted — and never raises into a request path.

### What the suppressor is not

* It bounds **log records**, never a `usage_logs` row. A refused or failed tool
  call always writes its row.
* It does not bound how many events a caller can **cause**. Nothing here does;
  admission control is the sibling change `mcp-rate-limits` (#188/#194).
* It is **per process**, like the error ring buffer. Single-worker today
  (`--workers 1`); under multiple workers each keeps its own counters and the
  effective cap multiplies by the worker count.

## Why WARNING events do not reach the health page

`error_log.CAPTURE_LEVEL` stays at ERROR. The page is "recent errors", not
"recent refusals": a credential-stuffing burst would evict every real error from
a 100-entry buffer, which is the opposite of what the page is for. Auth failures
and every other refusal stay at WARNING and reach Loki only. `tool_exception`
and the quota/transfer/panel ERROR events do reach the buffer, which is the
point of logging them at that level.

## Residuals

* **Nothing bounds event admission** — `mcp-rate-limits` (#188/#194). Log
  retention, shipping and Loki cardinality are operator concerns.
* **Per-process suppression** under multiple workers (above).
* **uvicorn's own loggers keep their format.** `uvicorn`/`uvicorn.access`/
  `uvicorn.error` carry their own handlers with `propagate: false` — which is
  exactly why `error_log` attaches to `uvicorn.error` by name — so the container
  log is mixed JSON and text. Clearing their handlers would give a uniform
  stream and would change the shape of the highest-volume lines in an existing
  Loki pipeline for no security benefit.
* **Shared source addresses share an allowance.** Two tenants behind one NAT
  share one bucket, so a noisy neighbour can suppress a co-located tenant's
  *records*. It cannot affect their responses, their `usage_logs` rows or their
  ability to redeem, and the summary states the withheld count. The alternative
  — per-token subjects — is the unbounded-allowance hole.
* **MCP tool events carry no `client_ip`** (R8, above).
* **Post-publication filesystem failures are unbounded** (R10, above). On a
  failing filesystem an agent produces one WARNING per successful write, and
  up to three per transfer publication, until `mcp-rate-limits`' write bucket
  refuses. They are durability notes on operations that succeeded, and keeping
  them off the destructive publication path is the reason.
