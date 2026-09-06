"""The security-event emitter: one catalogue, one allowance check, one tag.

Every authentication, authorization and credential-outcome record in this server
is emitted from here, so that three properties hold in one place instead of at
forty call sites.

**One catalogue.** `EVENT_FIELDS` mirrors the table in
`docs/architecture/security-event-logging.md`: one entry per event, naming the
fields that event may carry. `emit` drops a field the event does not declare —
and raises under the test suite's strict flag — so the catalogue and the code
cannot drift apart. The declared set is *permitted*, never required: every field
is optional, and a record missing one means the emitting path did not have it.
Absence is meaningful.

**One allowance check.** A record is bounded by `acquire(event, subject)`, which
charges the subject's allowance **once** and returns a `Permit` or `None`;
`emit(permit, …)` consumes it and performs *no* second check. An earlier design
had a `should_emit` predicate followed by an `emit` that also checked, which
charged the flood bound twice for the one call site that has to do work to build
its fields (the transfer refusal diagnosis). There is no `should_emit`. A caller
that acquires a permit and then does not spend it has **spent** its slot anyway:
the failure direction is a quieter log, never a louder one.

The subject is `user_id` when the request already resolved a credential to one,
otherwise the **trusted** client address, otherwise `-`. It is never a token
tag, a submitted username or a submitted client id — a caller that could mint
subjects could mint allowances, which is exactly what rotating a bogus bearer
token on every request would do.

Every level is bounded, INFO included. Bounded volume beats audit completeness
because a summary carrying an exact count still answers "how many logins
succeeded", while a quiet log answers nothing. Nothing is dropped silently:
every withheld record is counted and an `events_suppressed` summary names the
event and the count.

**One redaction.** `redacted_token_tag` is the only function in the codebase
that turns a presented credential into something loggable, and `sha:` plus eight
hex characters is the only form in which one may appear. It answers `None` when
nothing was presented, so the field is *absent* rather than `sha:` of the empty
string — a constant that reads like a tag.

What this does **not** do: it bounds what reaches the log sink and nothing else.
It never suppresses a `usage_logs` row, and nothing here bounds how many events
a caller can *cause* — that is the sibling change `mcp-rate-limits` (#188/#194).
Background work (the indexer, the embed pass, filesystem housekeeping, startup)
stays on the bare logger, so suppression can never hide the operational errors
the health page exists to show.
"""
from __future__ import annotations

import collections
import contextlib
import hashlib
import logging
import os
import threading
import time
from dataclasses import dataclass

from src.logging_setup import ALLOWED_FIELDS, EMITTER_CONTROL, FORMATTER_OWNED

#: Records are emitted on their own logger so an operator can select the whole
#: security stream with one label, and so the module that owns the policy owns
#: the name. It propagates to the root, which is where both sinks live (the
#: stream handler and, for ERROR, the health page's ring buffer).
logger = logging.getLogger("security_events")


class SecurityEventFieldError(AssertionError):
    """A field the catalogue does not permit, raised only under strict mode.

    Subclasses `AssertionError` because it means the same thing a failed
    assertion does: the tree is inconsistent with its own registry. In
    production the field is dropped instead and the record is still emitted —
    losing a field is bad, losing the record is worse.
    """


# ── The catalogue ───────────────────────────────────────────────────────────
#
# One entry per row of the table in docs/architecture/security-event-logging.md.
# `ts`, `level`, `logger` and `msg` are formatter-owned and appear on every
# record without being declared here; `stack` likewise, and it arrives through
# `exc_info=` rather than as a field.
#
# Provenance is carried by the field name and is a property of the (event,
# field) pair: an unsuffixed identifier holds only a value read from a database
# row, a `_submitted` name is the only place a caller-supplied identifier that
# did not resolve may appear, and a `_session` name is the only place a value
# copied from the session cookie without a database read may appear.

EVENT_FIELDS: dict[str, frozenset[str]] = {
    # ── Panel authentication (Slice B) ──
    "panel_login_succeeded": frozenset({"user_id", "username", "client_ip", "route"}),
    "panel_login_failed": frozenset(
        {"reason", "username_submitted", "user_id", "client_ip", "route"}
    ),
    "panel_logout": frozenset({"user_id_session", "username_session", "client_ip"}),
    "panel_bootstrap_admin_created": frozenset({"user_id", "username", "client_ip"}),
    "panel_bootstrap_refused": frozenset({"reason", "client_ip"}),
    "panel_password_reset": frozenset(
        {"actor_user_id", "user_id", "username", "client_ip", "route"}
    ),
    "password_hash_malformed": frozenset({"user_id"}),
    # The session registry's `last_seen_at` write, which is telemetry and may
    # never fail a request. Bounded because the touch interval gates the
    # *write*: a failing update records no new `last_seen_at`, so the interval
    # check passes on every retry and a stale browser drives one record per
    # `GET`. `reason` is the stage — `touch` or `rollback` — because a failing
    # update with a working rollback is a database refusing one statement,
    # while a failing rollback is a connection that is gone. Class only: the
    # statement binds `user_sessions.session_hash`.
    "panel_session_touch_failed": frozenset(
        {"reason", "user_id", "error_type", "route"}
    ),
    # ── The panel session registry (#198) ──
    #
    # **No credential material, ever** — not the cookie's session identifier,
    # and **not its stored SHA-256**. That digest is `user_sessions.id`, so a
    # record carrying it names one specific live session; it is as much a
    # secret as the identifier for the purpose of a log. Where a record must
    # identify a session it uses `token_tag`, which is `sha:` plus eight hex
    # characters — four short of the twelve-character fragment the canary test
    # forbids.
    "panel_session_replay_refused": frozenset(
        {"reason", "user_id", "token_tag", "route", "client_ip"}
    ),
    # `user_id_session` is the logout path's provenance: there the id is copied
    # from the session cookie and no row was read. The account-event callers
    # (an administrative reset, a deactivation, a delete, a password change)
    # revoke against a row they hold and pass `user_id`.
    "panel_sessions_revoked": frozenset(
        {"reason", "user_id", "user_id_session", "count"}
    ),
    # The logout whose revocation write — or whose rollback — failed. The
    # cookie is still cleared and the redirect still happens: failing closed
    # would leave the user signed in *and* the cookie alive. No `exc_info` and
    # no `str(exc)`, for `oauth_refresh_reuse_revocation_failed`'s reason: a
    # SQLAlchemy error renders the failing statement *and its bound
    # parameters*, one of which here is the stored session hash.
    "panel_session_revocation_failed": frozenset(
        {"reason", "user_id_session", "error_type", "route", "client_ip"}
    ),
    # ── The self-service password change (#197) ──
    #
    # Emitted **after** the commit that made it true, and never carrying a
    # password, a hash or a session identifier in any field — the account these
    # records name is the one whose credential just moved, so a record that
    # leaked any part of it would be the worst line in the file.
    "panel_password_changed": frozenset({"user_id", "username", "client_ip", "route"}),
    # `reason` says which rule refused, never anything about the stored
    # password: the two credential branches (`wrong_current_password` and
    # `same_as_current`) are distinguished *here* for the operator while the
    # caller is told one constant message, which is the whole point of
    # separating the record from the response.
    "panel_password_change_refused": frozenset(
        {"reason", "user_id", "client_ip", "route"}
    ),
    # The mint that follows a committed password change and did not happen. The
    # change is **not** rolled back — it is the durable half — so this records
    # a user who is signed out holding a new password, which is a recoverable
    # state an operator should still see. Modelled on
    # `panel_session_revocation_failed`, including its rule: **no `exc_info`
    # and no `str(exc)`**, only the class name, because a SQLAlchemy error
    # renders the failing statement *and its bound parameters*, one of which on
    # this path is a stored session hash.
    "panel_session_reissue_failed": frozenset(
        {"reason", "user_id", "error_type", "route", "client_ip"}
    ),
    # ── OAuth (Slice B) ──
    "oauth_token_issued": frozenset(
        {"client_id", "user_id", "grant_id", "scope", "client_ip", "reason"}
    ),
    "oauth_token_refreshed": frozenset(
        {"client_id", "user_id", "grant_id", "scope", "client_ip"}
    ),
    "oauth_token_refused": frozenset(
        {
            "reason",
            "client_id",
            "client_id_submitted",
            "user_id",
            "grant_id",
            "client_ip",
        }
    ),
    "oauth_token_rotation_failed": frozenset(
        {"client_id", "grant_id", "client_ip", "error_type"}
    ),
    # The #182 refresh-reuse alarm, migrated onto the suppressor by Slice B: a
    # replayed refresh token is caller-driven and repeatable, so a bare
    # `logger.warning` was an unbounded flood channel beside the bounded one.
    "oauth_refresh_reuse_detected": frozenset(
        {"client_id", "grant_id", "user_id", "revoked_tokens", "client_ip"}
    ),
    # No `exc_info` and no `stack` on this one, deliberately: a SQLAlchemy
    # error renders the failing statement *and its bound parameters*, one of
    # which is the token hash. Only the exception's class name is recorded.
    "oauth_refresh_reuse_revocation_failed": frozenset(
        {"client_id", "grant_id", "user_id", "client_ip", "error_type"}
    ),
    "oauth_consent_granted": frozenset({"client_id", "user_id", "scope", "client_ip"}),
    "oauth_consent_denied": frozenset(
        {"client_id", "client_id_submitted", "user_id", "client_ip"}
    ),
    "oauth_authorize_refused": frozenset(
        {"reason", "client_id", "client_id_submitted", "user_id", "client_ip"}
    ),
    "oauth_cross_user_client_refused": frozenset(
        {"client_id", "actor_user_id", "user_id", "route", "client_ip"}
    ),
    "oauth_client_registered": frozenset(
        {"client_id", "client_name_submitted", "scope", "count", "client_ip"}
    ),
    "oauth_client_registration_refused": frozenset({"reason", "client_ip"}),
    "oauth_grant_revoked": frozenset(
        {
            "client_id",
            "user_id",
            "actor_user_id",
            "grant_id",
            "count",
            "client_ip",
            "route",
        }
    ),
    "oauth_revoke_noop": frozenset({"reason", "client_id_submitted", "client_ip"}),
    "oauth_revoke_refused": frozenset({"reason", "client_id", "client_ip"}),
    # ── The request boundary (Slice A) ──
    "rate_limit_exceeded": frozenset(
        {"route", "method", "client_ip", "limit_count", "window_seconds"}
    ),
    "auth_failure": frozenset(
        {"reason", "token_tag", "key_id", "oauth_token_id", "client_ip", "route"}
    ),
    # An address over its failed-authentication budget (#194). **One record per
    # slot per window** — the first refusal — because every later refusal in
    # the same window is the same fact and would be an unbounded channel beside
    # the bounded one. No `token_tag` and no `reason`: nothing was looked up,
    # and the presented credential (if any) was never read. `limit_count` and
    # `window_seconds` are the two numbers an operator needs to decide whether
    # the budget is too tight for a shared egress address.
    "auth_failure_rate_limited": frozenset(
        {"client_ip", "route", "limit_count", "window_seconds"}
    ),
    # ── The tool surface (Slice C) ──
    # No `client_ip`: `_tracked` and `_require_write` run below
    # `ProxyHeadersMiddleware` and nothing binds the request address into a
    # ContextVar, so these records identify the *credential*, not the address
    # (residual R8). They identify it by **row id**: `actor_ref` — the key's
    # `omcp_` prefix — is a substring of the live credential and has no field
    # in the allow-list any more.
    "tool_write_refused": frozenset(
        {"tool", "user_id", "actor_kind", "key_id", "oauth_token_id"}
    ),
    "tool_body_outcome": frozenset(
        {"tool", "reason", "outcome", "user_id", "key_id", "oauth_token_id"}
    ),
    # #261 transport observer integration; no credential fingerprints.
    "mcp_concurrency_pressure": frozenset(
        {"reason", "outcome", "limit_count", "method", "route", "client_ip",
         "user_id", "key_id", "oauth_token_id"}
    ),
    "tool_exception": frozenset(
        {
            "tool",
            "error_type",
            "user_id",
            "actor_kind",
            "key_id",
            "oauth_token_id",
            "duration_ms",
        }
    ),
    # The post-body telemetry tail, which runs after the tool has already
    # returned. Structurally distinct from `tool_exception` on purpose: the
    # call succeeded, and an operator filtering for failures must not find it.
    "tool_telemetry_failed": frozenset({"tool", "error_type"}),
    "tool_usage_log_failed": frozenset({"tool", "error_type"}),
    "tool_refused_no_vault": frozenset({"user_id", "tool"}),
    # The vault-root quarantine (#199), refused by the *same* admission gate.
    # Its own event rather than a fourth reason on `tool_refused_no_vault`:
    # that one means "this credential has no vault", and this one means "it has
    # one and the server will not serve it". `reason` tells the three apart —
    # `overlap`, `root_unexaminable`, `snapshot_not_ready` — and is a closed
    # vocabulary, never a peer's name or a path: the caller-facing refusal
    # names no other tenant and neither does the field that classifies it. The
    # accounts, reasons and roots are named on the operator surfaces.
    "tool_refused_vault_quarantined": frozenset({"user_id", "tool", "reason"}),
    "tool_refused_over_quota": frozenset({"key_id", "limit", "day", "user_id", "tool"}),
    # A call refused by one of the two per-principal token buckets (#188,
    # #194). `reason` is the bucket — `principal` or `principal_write` — a
    # closed vocabulary and the same string the caller's refusal carries as its
    # `scope`, so an operator reading the log and an agent reading its result
    # are talking about the same control. No `client_ip`, for the reason every
    # other tool-surface event has none (residual R8): `_tracked` runs below
    # `ProxyHeadersMiddleware`, so these records identify the *credential*.
    #
    # The record is bounded by the suppressor like every other one — which is
    # the point, because the `usage_logs` half is bounded by a coalescer and
    # the two bounds are independent.
    "tool_refused_rate_limited": frozenset(
        {"tool", "reason", "limit", "user_id", "key_id", "oauth_token_id"}
    ),
    "usage_log_credential_gone": frozenset({"tool", "cleared_user_id"}),
    "usage_log_failed": frozenset({"tool", "error_type", "reason"}),
    "tool_result_measure_failed": frozenset({"tool", "error_type"}),
    "move_rewrite_failed": frozenset({"tool", "error_type"}),
    # `move_note` aborting the whole move because one source holds a link
    # nested inside another link to the same note (#211). Its own event rather
    # than `move_rewrite_failed`: that one skips a source and carries on, this
    # one mutates nothing at all.
    "move_rewrite_overlap_refused": frozenset({"tool", "error_type"}),
    # The two best-effort failures *after* the rename has already stood, told
    # apart by `reason` (`title_read_failed` | `db_update_failed`) because an
    # operator seeing a burst needs to know whether the file or the database is
    # the one misbehaving. Neither fails the call.
    "move_post_rename_failed": frozenset({"tool", "error_type", "reason"}),
    # ── Vault and quota admission (Slice A) ──
    "quota_admission_failed": frozenset({"key_id", "day", "error_type"}),
    "quota_counter_prune_failed": frozenset({"error_type"}),
    "publication_refused_confirmation_unavailable": frozenset(
        {"user_id", "error_type"}
    ),
    "publication_refused_vault_assignment_changed": frozenset({"user_id", "reason"}),
    # ── Transfer (Slice D) ──
    "transfer_refused": frozenset(
        {
            "reason",
            "token_tag",
            "route",
            "method",
            "client_ip",
            "user_id",
            "key_id",
            "oauth_token_id",
            "error_type",
        }
    ),
    # `PUT /transfer/upload` refused by the **minting** principal's write
    # bucket (#194). Its own event rather than a `reason` on `transfer_refused`
    # for the reason that one exists: `transfer_refused` accompanies the
    # uniform 404 and means "this token is not usable", while this one means
    # the token is perfectly usable and the minter's write rate is spent — a
    # 429 the same link survives. `reason` is the bucket scope, the same closed
    # vocabulary `tool_refused_rate_limited` carries, so the two surfaces of
    # one control read as one control. No `client_ip`: the request carried a
    # capability, and the identity that matters is the minter's, which the
    # token row names.
    "transfer_refused_rate_limited": frozenset(
        {"reason", "limit", "route", "method", "user_id", "key_id", "oauth_token_id"}
    ),
    "transfer_refused_mount_boundary": frozenset({"error_type", "route", "method"}),
    "transfer_refused_unsupported_fs": frozenset({"error_type", "route", "method"}),
    "transfer_root_unusable": frozenset({"error_type", "user_id", "route", "method"}),
    "transfer_post_publish_failure": frozenset({"error_type", "user_id", "route"}),
    "transfer_prepublish_failure": frozenset({"error_type", "user_id", "route"}),
    # Returning a claimed upload token to `pending` is bookkeeping, and it runs
    # on paths that have *already decided* their response — a 404, a 413, a
    # 503. A failure there may not turn the decided answer into a 500 or take
    # the refusal record with it, so it is caught, recorded class-only, and the
    # response goes out unchanged.
    "transfer_claim_release_failed": frozenset(
        {"error_type", "user_id", "route", "method"}
    ),
    # ── Panel authorization and operations (Slice D) ──
    "panel_forbidden": frozenset(
        {"reason", "actor_user_id", "actor_username", "user_id", "route", "method"}
    ),
    "csrf_refused": frozenset({"route", "method", "user_id", "client_ip"}),
    "panel_ondemand_index_failed": frozenset({"user_id", "error_type"}),
    "panel_ondemand_embed_failed": frozenset({"user_id", "error_type"}),
    "panel_health_strip_failed": frozenset({"error_type"}),
    # ── The suppressor's own record (this module) ──
    "events_suppressed": frozenset({"reason", "count", "window_seconds"}),
}

#: The suppressor's summary. Never itself suppressed, never counted.
SUMMARY_EVENT = "events_suppressed"


# ── Redaction and request context ───────────────────────────────────────────


def redacted_token_tag(value: str | None) -> str | None:
    """`sha:` plus eight hex characters, or `None` when nothing was presented.

    The single definition in the codebase (`src/mcp_server/auth.py` delegates to
    it), and the only form in which a presented credential may reach a log. A
    SHA-256 prefix keeps failures correlatable — the same token yields the same
    tag — without writing credential material anywhere; the `token[:8]` this
    replaced leaked the first eight characters of an attacker-supplied, or in
    the worst case a valid, token.

    `None` rather than `sha:` of the empty string: a constant that looks like a
    tag on every credential-less request is worse than an absent field, because
    an operator would correlate on it.
    """
    if not value:
        return None
    return "sha:" + hashlib.sha256(value.encode()).hexdigest()[:8]


def client_ip(request) -> str | None:
    """The peer address ASGI reports, never a header.

    `ProxyHeadersMiddleware` (`src/main.py`) has already rewritten
    `scope["client"]` from `X-Forwarded-For` — but **only** for peers inside the
    RFC 1918 ranges the deployment trusts. Reading the header here instead would
    accept a forged one from any client on the internet, which is the precise
    thing that middleware exists to prevent, so this helper never touches
    headers.
    """
    try:
        client = getattr(request, "client", None)
        host = getattr(client, "host", None)
        return host or None
    except Exception:  # noqa: BLE001 - a logging helper may not raise
        return None


def subject_for(user_id=None, request=None, ip: str | None = None) -> str:
    """The suppression subject: resolved user, else trusted address, else `-`.

    Prefixed (`user:` / `ip:`) so that user 5 and the address "5" cannot share a
    bucket. Must be computable **before** any work the permit gates — that is
    why the transfer refusal always keys on the address even on the branches
    where a row later resolves: the owner is knowable only after the diagnosis
    the permit is supposed to gate.
    """
    if isinstance(user_id, int) and not isinstance(user_id, bool):
        return f"user:{user_id}"
    address = ip if ip is not None else client_ip(request)
    if address:
        return f"ip:{address}"
    return "-"


# ── The suppressor ──────────────────────────────────────────────────────────

#: Records of one event, for one subject, per window.
MAX_EVENTS_PER_WINDOW = 10
#: Records of *all* events for one subject per window, so a source cycling
#: through twenty refusal events cannot multiply its allowance by twenty.
MAX_EVENTS_PER_SUBJECT_PER_WINDOW = 50
WINDOW_SECONDS = 60
#: Bound on each map. Unbounded state is a memory leak whose trigger is a flood,
#: i.e. the one shape of leak an attacker can ask for.
MAX_TRACKED_KEYS = 512


@dataclass
class _Window:
    started: float
    count: int = 0
    suppressed: int = 0
    level: int = logging.WARNING


@dataclass(frozen=True)
class Permit:
    """A charged allowance for one record. Spend it with `emit(permit, …)`.

    Carries the level so a summary for the window can be emitted at the level of
    the records it withheld — an operator filtering at WARNING still sees that
    warnings were withheld.
    """

    event: str
    subject: str
    level: int = logging.WARNING


_lock = threading.Lock()
_event_windows: "collections.OrderedDict[tuple[str, str], _Window]" = (
    collections.OrderedDict()
)
_subject_windows: "collections.OrderedDict[str, _Window]" = collections.OrderedDict()

#: Strict mode turns a catalogue violation into a raise. On in the test suite
#: (via the environment variable or the `strict_fields()` context manager),
#: never in production, where dropping the field and keeping the record is the
#: right failure.
STRICT_ENV_VAR = "OMCP_SECURITY_EVENTS_STRICT"
_strict = os.environ.get(STRICT_ENV_VAR) == "1"

#: Tests that count emission attempts need the suppressor out of the way.
_suppression_enabled = True


@contextlib.contextmanager
def strict_fields(enabled: bool = True):
    """Raise on a catalogue violation for the duration. Tests only."""
    global _strict
    previous = _strict
    _strict = enabled
    try:
        yield
    finally:
        _strict = previous


@contextlib.contextmanager
def suppression_disabled():
    """Force every `acquire` to return a permit. Tests only.

    The suppressor's own tests exercise the caps directly; every *other* test
    that counts emission attempts wants one attempt to mean one record.
    """
    global _suppression_enabled
    previous = _suppression_enabled
    _suppression_enabled = False
    try:
        yield
    finally:
        _suppression_enabled = previous


def reset_state() -> None:
    """Forget every window without emitting summaries. Tests only."""
    with _lock:
        _event_windows.clear()
        _subject_windows.clear()


def _roll(window: _Window | None, now: float) -> tuple[_Window, tuple | None]:
    """Return the live window for `now`, plus any summary the closed one owes."""
    if window is not None and now - window.started < WINDOW_SECONDS:
        return window, None
    owed = None
    if window is not None and window.suppressed:
        owed = (window.suppressed, window.level)
    return _Window(started=now), owed


def _evict_locked() -> list[tuple]:
    """Trim both maps to their bound, **least recently used first**, keeping
    every count.

    Least recently *used*, not oldest *window*: `acquire` moves each key to the
    end, so eviction pops the key nobody has touched for longest. Evicting by
    window start would throw away the busiest key — one acquired steadily
    inside a single 60-second window keeps its original start time — which is
    the opposite of what a bound on memory should do.

    An entry holding a nonzero withheld count emits its summary **before** it
    goes: otherwise the count vanishes and the log silently under-reports, which
    is worse than the flood it was bounding.
    """
    pending = []
    while len(_event_windows) > MAX_TRACKED_KEYS:
        (event, subject), window = _event_windows.popitem(last=False)
        if window.suppressed:
            pending.append((event, subject, window.suppressed, window.level))
    while len(_subject_windows) > MAX_TRACKED_KEYS:
        _subject_windows.popitem(last=False)
    return pending


def acquire(event: str, subject: str | None = None, *, level: int = logging.WARNING):
    """Charge one unit of `subject`'s allowance for `event`. Fails open.

    Returns a `Permit` when the record may be emitted and `None` when it is
    withheld (and counted). The allowance is charged **here and only here**;
    `emit(permit, …)` performs no second check.

    `level` is recorded so the window's summary can carry the level of what it
    withheld; pass `emit` the same level.

    Any internal error returns a permit — a suppressor that has broken must not
    also silence the log, and it may never raise into a request path.
    """
    try:
        key_subject = subject or "-"
        now = time.monotonic()
        pending: list[tuple] = []
        with _lock:
            key = (event, key_subject)
            event_window, owed = _roll(_event_windows.get(key), now)
            if owed is not None:
                pending.append((event, key_subject, owed[0], owed[1]))
            _event_windows[key] = event_window
            _event_windows.move_to_end(key)

            subject_window, _ = _roll(_subject_windows.get(key_subject), now)
            _subject_windows[key_subject] = subject_window
            _subject_windows.move_to_end(key_subject)

            allowed = _suppression_enabled is False or (
                event_window.count < MAX_EVENTS_PER_WINDOW
                and subject_window.count < MAX_EVENTS_PER_SUBJECT_PER_WINDOW
            )
            if allowed:
                event_window.count += 1
                subject_window.count += 1
            else:
                # Charged against the *event's* entry whichever cap refused, so
                # the summary can name the event that was withheld.
                event_window.suppressed += 1
                event_window.level = level
            pending.extend(_evict_locked())

        for summary in pending:
            _emit_summary(*summary)
        return Permit(event=event, subject=key_subject, level=level) if allowed else None
    except Exception:  # noqa: BLE001 - fail open, never raise into a request
        return Permit(event=event, subject=subject or "-", level=level)


def flush_suppression_summaries() -> None:
    """Emit every outstanding withheld count. Called at shutdown.

    Registered in the FastAPI lifespan and, for the stdio entry point, via
    `atexit`. Without it, a window holding a count when the process stops takes
    the count with it and the log under-reports for good.
    """
    try:
        with _lock:
            pending = [
                (event, subject, window.suppressed, window.level)
                for (event, subject), window in _event_windows.items()
                if window.suppressed
            ]
            for (event, subject), window in _event_windows.items():
                window.suppressed = 0
        for summary in pending:
            _emit_summary(*summary)
    except Exception:  # noqa: BLE001 - shutdown may not fail on bookkeeping
        pass


def _emit_summary(event: str, subject: str, count: int, level: int) -> None:
    """One `events_suppressed` record. Bypasses suppression; never counted."""
    _log(
        SUMMARY_EVENT,
        level,
        None,
        {"reason": event, "count": count, "window_seconds": WINDOW_SECONDS},
    )


# ── Emission ────────────────────────────────────────────────────────────────


def _clean_fields(event: str, fields: dict) -> dict:
    """The subset of `fields` this event may carry. Strict mode raises instead.

    Three rejections, for three different reasons:
    * a **formatter-owned** name (`ts`, `level`, `logger`, `msg`, `stack`) —
      a call site may not forge a timestamp, a level or a traceback;
    * a name outside `ALLOWED_FIELDS` — the allow-list is the whole point;
    * a name outside this event's declared set — so a call site cannot quietly
      widen an event past its catalogue row.
    """
    allowed = EVENT_FIELDS.get(event)
    if allowed is None and _strict:
        raise SecurityEventFieldError(
            f"{event!r} is not in EVENT_FIELDS; add its catalogue row first"
        )
    clean: dict[str, object] = {}
    for name, value in fields.items():
        if value is None:
            # Every field is optional; absence is the honest rendering of "the
            # emitting path did not have this".
            continue
        if name in FORMATTER_OWNED:
            if _strict:
                raise SecurityEventFieldError(
                    f"{event!r} passed formatter-owned field {name!r}"
                )
            continue
        if name not in ALLOWED_FIELDS:
            if _strict:
                raise SecurityEventFieldError(
                    f"{event!r} passed field {name!r}, which is not allow-listed"
                )
            continue
        if allowed is not None and name not in allowed:
            if _strict:
                raise SecurityEventFieldError(
                    f"{event!r} does not declare field {name!r}"
                )
            continue
        clean[name] = value
    return clean


def _log(event: str, level: int, exc_info, fields: dict) -> None:
    logger.log(level, event, extra=fields, exc_info=exc_info)


def emit(
    permit_or_event,
    *,
    level: int = logging.WARNING,
    exc_info=None,
    subject: str | None = None,
    **fields,
) -> None:
    """Emit one security event. Never raises (except under strict mode).

    Two shapes:

    * `emit("auth_failure", subject=…, reason=…)` — acquire and consume in one
      step. What almost every call site wants.
    * `emit(permit, reason=…)` — consume a permit obtained from `acquire`,
      **without a second allowance check**. For the one call site that must do
      work to build its fields and therefore has to know it will be emitting
      before it does that work (the transfer refusal diagnosis). Pass the same
      `level` the permit was acquired with; the permit's own level is used only
      for the window's summary.
    """
    try:
        if permit_or_event is None:
            # A caller that passed a denied permit straight through. The record
            # was already withheld and counted at `acquire`; emitting it here
            # would be the second check this design exists to remove.
            return
        if isinstance(permit_or_event, Permit):
            event = permit_or_event.event
        else:
            event = permit_or_event
            if acquire(event, subject, level=level) is None:
                return
        _log(event, level, exc_info, _clean_fields(event, fields))
    except SecurityEventFieldError:
        raise
    except Exception:  # noqa: BLE001 - logging may not fail a request path
        pass
