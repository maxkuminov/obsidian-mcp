import hashlib
import logging
import time
from contextvars import ContextVar
from datetime import datetime, timezone

from sqlalchemy import select, update
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from src.auth.session import (
    UNSET_VAULT_ROOT,
    current_actor,
    current_principal,
    current_user_id,
    current_vault_root,
)
from src.config import settings
from src.database import async_session
from src.models.db import APIKey, OAuthClient, OAuthToken, User
from src.oauth.scope import has_vault_scope, token_has_write
from src.services import rate_limits, security_events
from src.services.vault import warm_user_vault_cache

logger = logging.getLogger(__name__)

# Context variables for current request's auth state
current_permission: ContextVar[str] = ContextVar("current_permission", default="read")
current_api_key_id: ContextVar[int | None] = ContextVar("current_api_key_id", default=None)
current_oauth_token_id: ContextVar[int | None] = ContextVar("current_oauth_token_id", default=None)
# This request's key quota ceiling, or None for "unlimited" (#162). Bound here
# for the reason the actor label is (issue #77): the `APIKey` row is already
# loaded, so reading the limit costs no extra query — and `_tracked` must be
# able to decide "does this caller have a quota at all" without issuing one,
# because the answer is None for every key today and a round trip per call to
# learn that is a tax on a feature nobody has turned on.
#
# Default None, and the OAuth branch never sets it: OAuth traffic is the
# operator's own panel session and is exempt in v1. Reset in the same `finally`
# as the rest, so a limit can never leak into another request's calls.
current_daily_request_limit: ContextVar[int | None] = ContextVar(
    "current_daily_request_limit", default=None
)


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _redacted_prefix(token: str) -> str:
    """Stable, non-reversible tag for an auth-failure log line.

    A SHA-256 prefix keeps failures correlatable (same token -> same tag)
    without writing raw credential material to logs, unlike the previous
    `token[:8]` which leaked the first 8 chars of an attacker-supplied
    (or, worst case, valid) token.

    Delegates to `security_events.redacted_token_tag`, which is now the single
    definition: a second copy of "how a credential may appear in a log" is a
    second place for it to be got wrong. Kept as a name here because it is the
    one this module has always exported.
    """
    return security_events.redacted_token_tag(token) or ""


def _emit_auth_failure(
    request: Request,
    reason: str,
    *,
    token: str | None = None,
    key_id: int | None = None,
    oauth_token_id: int | None = None,
    user_id: int | None = None,
) -> None:
    """One `auth_failure` record, through the suppressor — and the one place
    the failed-authentication budget is charged.

    **Every** 401 branch in this middleware goes through here, which is why the
    increment lives here rather than at each `return`: a prober picks the
    cheapest branch, so a budget covering six of seven bounds nothing, and a
    list of call sites somebody has to remember to extend is how the seventh
    gets missed. `tests/test_mcp_auth_failure_budget.py` pins that every 401 in
    this module is preceded by a call to this function.

    These are the highest-volume caller-triggerable refusals in the server — a
    credential-stuffing burst is N of them a second — so they go through
    `security_events.emit` rather than straight to a logger: a direct call would
    be an unbounded flood channel beside the bounded one.

    `token_tag` is the only form a presented credential may take, and it is
    absent when nothing was presented. `key_id` names an `api_keys` row and
    `oauth_token_id` an `oauth_tokens` row — the OAuth branches used to put the
    latter in the former, which made the two indistinguishable in a query.
    The suppression subject is the resolved user where there is one and the
    trusted client address otherwise; never the token tag, or every rotated
    bogus token would mint itself a fresh allowance.
    """
    rate_limits.record_auth_failure(security_events.client_ip(request))
    security_events.emit(
        "auth_failure",
        subject=security_events.subject_for(user_id=user_id, request=request),
        reason=reason,
        token_tag=security_events.redacted_token_tag(token),
        key_id=key_id,
        oauth_token_id=oauth_token_id,
        client_ip=security_events.client_ip(request),
        route=request.url.path,
    )


def _www_authenticate(error: str | None = None) -> str:
    """Build a `WWW-Authenticate: Bearer ...` value pointing redirect-averse
    MCP clients at our RFC 9728 protected-resource metadata.

    Emitted on every 401 from this middleware so a client can (re)discover the
    auth server whether it sent no token, an invalid one, or an expired one.
    `error` is an RFC 6750 code (`invalid_token` for a credential that was
    presented but rejected); omit it when no credential was presented at all.
    """
    base_url = settings.base_url.rstrip("/")
    resource_metadata = f"{base_url}/.well-known/oauth-protected-resource/mcp"
    parts = []
    if error:
        parts.append(f'error="{error}"')
    parts.append(f'resource_metadata="{resource_metadata}"')
    return "Bearer " + ", ".join(parts)


class APIKeyMiddleware:
    """ASGI middleware that authenticates requests via Bearer token against api_keys table."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        # Registry-eval only, and it short-circuits **everything** below —
        # including the failed-authentication budget, which is therefore never
        # reached in sandbox mode. That is not an exemption with a hole in it:
        # authentication is bypassed entirely here, so there are no failures to
        # budget and no credential lookup to protect. Nothing untrusted may
        # ever reach a sandbox deployment, which is why `Settings` refuses to
        # boot one with a public hostname at all.
        if settings.mcp_sandbox_mode:
            await self.app(scope, receive, send)
            return

        request = Request(scope)

        # L1 — the failed-authentication budget, **before everything**: before
        # the credential lookup, and before the bearer check that is the
        # cheapest 401 of all. A refused probe therefore costs no database
        # session and no query, which is exactly what this control bounds — the
        # database work an unauthenticated caller can force. It is *not* a
        # defence against credential guessing (256-bit keys need none).
        #
        # The address comes from `security_events.client_ip`, which reads
        # `scope["client"]` — already rewritten from `X-Forwarded-For` by
        # `ProxyHeadersMiddleware`, and **only** for peers inside the trusted
        # private ranges. A budget keyed on a header any client could forge
        # would be worse than none.
        #
        # A **transport** refusal, deliberately outside the in-band refusal
        # contract every gate inside `_tracked` obeys: there is no tool call to
        # answer, no principal and no `usage_logs` row, so it carries
        # `Retry-After` and `WWW-Authenticate` instead of a sentinel line.
        # Fabricating a tool result for a request that never authenticated
        # would be a worse answer than an honest HTTP error.
        over_budget = rate_limits.check_auth_failures(
            security_events.client_ip(request)
        )
        if over_budget is not None:
            if over_budget.first:
                # One record per slot per window. Every later refusal in the
                # same window is the same fact, and would be the unbounded
                # channel this control exists to close.
                security_events.emit(
                    "auth_failure_rate_limited",
                    subject=security_events.subject_for(request=request),
                    client_ip=security_events.client_ip(request),
                    route=request.url.path,
                    limit_count=over_budget.limit,
                    window_seconds=over_budget.window_seconds,
                )
            response = JSONResponse(
                {"error": "Too many failed authentication attempts"},
                status_code=429,
                headers={
                    "Retry-After": str(over_budget.retry_after_seconds),
                    "WWW-Authenticate": _www_authenticate(),
                },
            )
            await response(scope, receive, send)
            return

        auth_header = request.headers.get("authorization", "")

        if not auth_header.startswith("Bearer "):
            # Charged like every other 401 — this is the *cheapest* probe there
            # is, so a budget that skipped it would bound nothing: a prober
            # would simply stop sending a token. Charged directly rather than
            # through `_emit_auth_failure` because no credential was presented,
            # so there is no `auth_failure` record to make: that event's
            # `reason` is a closed vocabulary about a credential that was read,
            # and this branch read none.
            rate_limits.record_auth_failure(security_events.client_ip(request))
            response = JSONResponse(
                {"error": "Missing Bearer token"},
                status_code=401,
                headers={"WWW-Authenticate": _www_authenticate()},
            )
            await response(scope, receive, send)
            return

        token = auth_header[7:]

        # Set default ContextVar values and capture reset tokens for cleanup
        token_perm = current_permission.set("read")
        token_key = current_api_key_id.set(None)
        token_oauth = current_oauth_token_id.set(None)
        token_user = current_user_id.set(None)
        # Unlimited unless an API key says otherwise (#162). Every branch that
        # does not set it — OAuth, and any future credential — is exempt by
        # construction rather than by a list somebody has to remember.
        token_limit = current_daily_request_limit.set(None)
        # Bound below from the authenticated user's *own* freshly read
        # `vault_path`. It stays unset for single-user keys (no user row) so
        # `_vault_root(None)` keeps answering from settings. Resetting it in
        # `finally` alongside the others keeps the snapshot request-scoped —
        # that is what stops the indexer's bulk warm from re-admitting a user
        # whose assignment was revoked mid-request (issue #66).
        token_vault = current_vault_root.set(UNSET_VAULT_ROOT)
        # Denormalised attribution for `usage_logs` (issue #77). Bound below
        # from the credential row this request authenticated with, and reset
        # with the rest so it can never label another request's log line.
        token_actor = current_actor.set(None)
        # The identity every per-principal rate control is keyed on, bound
        # below from the same row: `("api_key", api_keys.id)` or
        # `("oauth", oauth_tokens.grant_id)`. Default `None` — no principal —
        # which every per-principal control reads as *exempt*, so sandbox mode
        # and a direct in-process caller behave exactly as they did.
        token_principal = current_principal.set(None)

        try:
            if token.startswith("omcp_"):
                # Legacy API key auth
                key_hash = hash_key(token)

                async with async_session() as session:
                    result = await session.execute(
                        select(APIKey).where(
                            APIKey.key_hash == key_hash,
                            APIKey.is_active == True,
                        )
                    )
                    api_key = result.scalar_one_or_none()

                    if api_key is None:
                        _emit_auth_failure(request, "invalid_key", token=token)
                        response = JSONResponse(
                            {"error": "Invalid or revoked key"},
                            status_code=401,
                            headers={"WWW-Authenticate": _www_authenticate("invalid_token")},
                        )
                        await response(scope, receive, send)
                        return

                    if api_key.user_id is None and settings.multi_user_mode:
                        # An ownerless key in multi-user mode. These exist: a
                        # key minted while multi-user was off keeps
                        # `user_id = NULL`, and the bootstrap backfill in
                        # `src/auth/routes.py` only claims those rows when
                        # `users` is *empty* — flip the flag after users
                        # exist and the NULLs are never adopted. Such a key
                        # used to be treated as single-user by every layer:
                        # the warm was skipped and `_vault_root(None)`
                        # returned the global `settings.vault_path`, so an
                        # ownerless readwrite key could edit the whole vault.
                        # Refuse it here, with the same body as any other
                        # rejected key.
                        _emit_auth_failure(
                            request, "ownerless_credential", key_id=api_key.id
                        )
                        response = JSONResponse(
                            {"error": "Invalid or revoked key"},
                            status_code=401,
                            headers={"WWW-Authenticate": _www_authenticate("invalid_token")},
                        )
                        await response(scope, receive, send)
                        return

                    if api_key.user_id is not None:
                        result = await session.execute(
                            select(User.is_active).where(User.id == api_key.user_id)
                        )
                        if result.scalar_one_or_none() is not True:
                            _emit_auth_failure(
                                request,
                                "inactive_user",
                                key_id=api_key.id,
                                user_id=api_key.user_id,
                            )
                            response = JSONResponse(
                                {"error": "Invalid or revoked key"},
                                status_code=401,
                                headers={"WWW-Authenticate": _www_authenticate("invalid_token")},
                            )
                            await response(scope, receive, send)
                            return

                    # Check expiry
                    if api_key.expires_at and api_key.expires_at < datetime.now(timezone.utc):
                        _emit_auth_failure(
                            request,
                            "key_expired",
                            key_id=api_key.id,
                            user_id=api_key.user_id,
                        )
                        response = JSONResponse(
                            {"error": "Key expired"},
                            status_code=401,
                            headers={"WWW-Authenticate": _www_authenticate("invalid_token")},
                        )
                        await response(scope, receive, send)
                        return

                    # Update last_used_at
                    await session.execute(
                        update(APIKey).where(APIKey.id == api_key.id).values(
                            last_used_at=datetime.now(timezone.utc)
                        )
                    )
                    await session.commit()

                    # Store key info in scope for tools to access
                    scope["state"] = scope.get("state", {})
                    scope["state"]["api_key_id"] = api_key.id
                    scope["state"]["api_key_permission"] = api_key.permission
                    scope["state"]["request_start"] = time.time()

                    # Set context variables so tools can check permission and log usage
                    current_permission.set(api_key.permission)
                    current_api_key_id.set(api_key.id)
                    current_user_id.set(api_key.user_id)
                    # The key row is already loaded, so the actor label costs
                    # no extra query -- and once written to `usage_logs` it
                    # survives the row's deletion, which the panel performs
                    # after NULLing `usage_logs.key_id` (issue #77).
                    current_actor.set(("api_key", api_key.name, api_key.key_prefix))
                    # The key row is loaded, so the quota ceiling is free here
                    # too (#162). NULL — every key until an operator sets one —
                    # means unlimited, and `_tracked` then issues no quota
                    # statement at all.
                    current_daily_request_limit.set(api_key.daily_request_limit)
                    # The row is in hand, so the principal costs no query
                    # either — the same house rule the actor label and the
                    # quota ceiling follow.
                    current_principal.set(("api_key", api_key.id))
                    # In single-user mode `api_key.user_id` is None so this
                    # is skipped entirely. In multi-user mode, read the user's
                    # `vault_path` now and bind the answer to this request:
                    # it both warms the shared cache (so sync
                    # `_vault_root(user_id)` calls don't hit a cold one) and
                    # gives `_vault_root` a snapshot no other task can
                    # overwrite. A None here means "unassigned", and every
                    # tool call in this request is refused (issue #66).
                    if api_key.user_id is not None:
                        current_vault_root.set((
                            api_key.user_id,
                            await warm_user_vault_cache(session, api_key.user_id),
                        ))
            else:
                # OAuth token auth
                token_hash = hash_key(token)

                async with async_session() as session:
                    # One statement, three consumers: the token itself, the
                    # client's owner for the cross-user check below, and the
                    # client's name for the denormalised `usage_logs` actor
                    # label (issue #77). Reading the name in a *second* query
                    # would add a round trip to every OAuth request, including
                    # the single-user path that previously issued none -- and
                    # the join is over the FK `oauth_tokens.client_id` already
                    # is. `outerjoin`, not `join`: the FK makes a token without
                    # a client row impossible, and if that ever stopped holding
                    # an inner join would silently turn the token into a 401,
                    # which is a different decision than the one made here.
                    result = await session.execute(
                        select(
                            OAuthToken,
                            OAuthClient.user_id.label("client_owner"),
                            OAuthClient.client_name,
                        )
                        .outerjoin(
                            OAuthClient,
                            OAuthClient.client_id == OAuthToken.client_id,
                        )
                        .where(
                            OAuthToken.token_hash == token_hash,
                            OAuthToken.token_type == "access",
                            OAuthToken.revoked == False,
                        )
                    )
                    row = result.first()
                    oauth_token, client_owner, client_name = (
                        row if row is not None else (None, None, None)
                    )

                    if oauth_token is None:
                        _emit_auth_failure(request, "invalid_key", token=token)
                        response = JSONResponse(
                            {"error": "Invalid or revoked token"},
                            status_code=401,
                            headers={"WWW-Authenticate": _www_authenticate("invalid_token")},
                        )
                        await response(scope, receive, send)
                        return


                    if oauth_token.user_id is None and settings.multi_user_mode:
                        # Same as the API-key branch above: an ownerless token
                        # in multi-user mode would resolve the global vault.
                        _emit_auth_failure(
                            request,
                            "ownerless_credential",
                            oauth_token_id=oauth_token.id,
                        )
                        response = JSONResponse(
                            {"error": "Invalid or revoked token"},
                            status_code=401,
                            headers={"WWW-Authenticate": _www_authenticate("invalid_token")},
                        )
                        await response(scope, receive, send)
                        return

                    if oauth_token.user_id is not None:
                        result = await session.execute(
                            select(User.is_active).where(User.id == oauth_token.user_id)
                        )
                        if result.scalar_one_or_none() is not True:
                            _emit_auth_failure(
                                request,
                                "inactive_user",
                                oauth_token_id=oauth_token.id,
                                user_id=oauth_token.user_id,
                            )
                            response = JSONResponse(
                                {"error": "Invalid or revoked token"},
                                status_code=401,
                                headers={"WWW-Authenticate": _www_authenticate("invalid_token")},
                            )
                            await response(scope, receive, send)
                            return

                    # The grant's owner must still be the client's owner. A
                    # cross-user grant can no longer be created, but one made
                    # before the consent and rotation paths refused it stays
                    # live for the access token's full hour and is invisible in
                    # either user's panel. An unbound client (NULL owner) is
                    # not a conflict — it has simply never been claimed.
                    if oauth_token.user_id is not None:
                        if client_owner is not None and client_owner != oauth_token.user_id:
                            _emit_auth_failure(
                                request,
                                "cross_user_grant",
                                oauth_token_id=oauth_token.id,
                                user_id=oauth_token.user_id,
                            )
                            response = JSONResponse(
                                {"error": "Invalid or revoked token"},
                                status_code=401,
                                headers={"WWW-Authenticate": _www_authenticate("invalid_token")},
                            )
                            await response(scope, receive, send)
                            return

                    if oauth_token.expires_at < datetime.now(timezone.utc):
                        _emit_auth_failure(
                            request,
                            "key_expired",
                            oauth_token_id=oauth_token.id,
                            user_id=oauth_token.user_id,
                        )
                        response = JSONResponse(
                            {"error": "Token expired"},
                            status_code=401,
                            headers={"WWW-Authenticate": _www_authenticate("invalid_token")},
                        )
                        await response(scope, receive, send)
                        return

                    # A token that names no vault scope grants nothing. Falling
                    # through to `read` here is the same conflation
                    # `clamp_scope` used to make: `offline_access` says the
                    # grant may carry a refresh token, not that it may read a
                    # note. No path can mint such a token any more, but a
                    # client registered `scope="offline_access"` before this
                    # could already hold one, and this is the boundary that
                    # decides what it may do.
                    if not has_vault_scope(oauth_token.scope):
                        _emit_auth_failure(
                            request,
                            "no_vault_scope",
                            oauth_token_id=oauth_token.id,
                            user_id=oauth_token.user_id,
                        )
                        response = JSONResponse(
                            {"error": "Invalid or revoked token"},
                            status_code=401,
                            headers={"WWW-Authenticate": _www_authenticate("invalid_token")},
                        )
                        await response(scope, receive, send)
                        return

                    # Map OAuth scope to permission. Scopes are space-separated
                    # sets (OAuth 2.0 convention), so this is a membership test
                    # -- and it is the *same* helper the control panel uses to
                    # decide what to display, so the badge and the enforcement
                    # cannot disagree (issue #65).
                    permission = "readwrite" if token_has_write(oauth_token.scope) else "read"

                    scope["state"] = scope.get("state", {})
                    scope["state"]["api_key_id"] = None
                    scope["state"]["api_key_permission"] = permission
                    scope["state"]["request_start"] = time.time()

                    current_permission.set(permission)
                    current_api_key_id.set(None)
                    current_oauth_token_id.set(oauth_token.id)
                    current_user_id.set(oauth_token.user_id)
                    current_actor.set(("oauth", client_name, oauth_token.client_id))
                    # The **grant**, not the access token: `grant_id` is NOT
                    # NULL and shared by every rotation of one `/authorize`
                    # approval (migration 014, #64), so refreshing an access
                    # token continues an allowance rather than resetting it,
                    # and two independently revocable grants never share one.
                    current_principal.set(("oauth", oauth_token.grant_id))
                    if oauth_token.user_id is not None:
                        current_vault_root.set((
                            oauth_token.user_id,
                            await warm_user_vault_cache(session, oauth_token.user_id),
                        ))

            await self.app(scope, receive, send)
        finally:
            current_permission.reset(token_perm)
            current_api_key_id.reset(token_key)
            current_oauth_token_id.reset(token_oauth)
            current_user_id.reset(token_user)
            current_vault_root.reset(token_vault)
            current_actor.reset(token_actor)
            current_daily_request_limit.reset(token_limit)
            current_principal.reset(token_principal)
