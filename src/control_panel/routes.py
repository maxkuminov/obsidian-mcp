import logging
import os
import re
import secrets
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.session import _SingleUserSentinel, get_current_user
from src.config import settings
from src.control_panel.flash import pop_flash
from src.csrf import generate_csrf_token, verify_csrf
from src.database import async_session, get_session
from src.mcp_server.auth import hash_key
from src.models.db import (
    DAILY_REQUEST_LIMIT_MAX,
    APIKey,
    NoteEmbedding,
    NoteLink,
    NoteMetadata,
    OAuthClient,
    OAuthToken,
    User,
    UsageLog,
)
from src.oauth.grants import (
    live_family_scopes,
    revoke_grant_family,
    set_grant_family_scope,
)
from src.oauth.scope import (
    clamp_scope,
    client_can_write,
    has_vault_scope,
    token_has_write,
)
from src.services import security_events
from src.services.indexer import invalidate_hnsw_index_cache
from src.services.quotas import (
    apply_daily_request_limit,
    consumed_today,
    parse_limit_form_value,
    utc_day,
)
from src.services.usage_filters import (
    LOG_LIMIT,
    actor_totals,
    chart_series,
    filter_options,
    recent_logs,
    resolve_filters,
)
from src.services.search_analytics import (
    COVERAGE_LIMIT,
    LOGGED_RESULTS_PER_CALL,
    QUERY_TOOLS,
    RELATED_TOOL,
    TABLE_LIMIT,
    never_retrieved,
    top_logged_retrievals,
    top_queries,
    zero_result_queries,
)
from src.services.error_log import (
    ERROR_BUFFER_SIZE,
    error_count,
    is_full as error_buffer_full,
    observing_since,
    recent_errors,
)
from src.services.ops_health import (
    HEALTH_RUNS_LIMIT,
    STALE_AFTER_DAYS,
    latest_backup,
)
from src.services.usage_stats import (
    RECENT_RUNS_LIMIT,
    WINDOW_LABELS,
    WINDOWS,
    normalize_window,
    phase_breakdown,
    recent_indexer_runs,
    slowest_requests,
    tool_aggregates,
)
from src.services.vault import warm_user_vault_cache

logger = logging.getLogger(__name__)


def _request_route(request) -> str | None:
    """`request.url.path`, defensively. A logging call may not raise.

    Not paranoia about Starlette: these emitters are reached from plain
    helpers that a test — or a future non-HTTP caller — can drive with
    something that is not a full `Request`. `security_events.emit` swallows its
    own failures, but the field expressions are evaluated *before* it is
    called, so an attribute error here would escape into a request path and
    turn a 403 into a 500. The record is allowed to be poorer; the response is
    not allowed to change.
    """
    try:
        return request.url.path
    except (AttributeError, TypeError):
        return None


def _request_method(request) -> str | None:
    """`request.method`, defensively. Same reason as `_request_route`."""
    try:
        return request.method
    except (AttributeError, TypeError):
        return None


def _actor(user) -> tuple[int | None, str | None]:
    """`(actor_user_id, actor_username)` — who *acted*, for a refusal record.

    D19's pair: `actor_user_id` is the principal that caused the record,
    `user_id` is the account the record is *about*. On the panel's ownership
    guards the two differ exactly when one person touches another's resource,
    which is the case the log has to be able to answer.

    The single-user sentinel has `id=None`, so a record from that deployment
    carries the name and no id rather than inventing one.
    """
    return getattr(user, "id", None), getattr(user, "username", None)


def _reembed_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.secret_key, salt="reembed-confirm")


def _humanize_delta(dt: datetime | None) -> str:
    if dt is None:
        return "never"
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    seconds = int((now - dt).total_seconds())
    if seconds < 45:
        return "just now"
    if seconds < 3600:
        m = max(1, seconds // 60)
        return f"{m} min ago"
    if seconds < 86400:
        h = seconds // 3600
        return f"{h} hour{'s' if h != 1 else ''} ago"
    d = seconds // 86400
    return f"{d} day{'s' if d != 1 else ''} ago"

router = APIRouter(prefix="/admin", tags=["panel"])
templates = Jinja2Templates(
    directory=os.path.join(os.path.dirname(__file__), "templates")
)


# --- Auth dependencies ----------------------------------------------------
#
# Panel routes need browser-friendly redirects (302 → login) when a session
# is missing, not the JSON 401 that `require_user` raises. The plain
# `src.auth.session.require_user` is fine for API contexts; here we wrap it
# so each handler can pick up a real `User` (or the single-user sentinel) and
# any unauthenticated request 302s to `/admin/auth/login?next=<original>`.


def _forget_new_key_flash(request: Request) -> None:
    """Drop a raw API key still parked in the session cookie.

    `create_key_form` stashes the one-time key in the session so it survives
    the POST/redirect/GET, and `keys_page` pops it for the single render it
    exists for. If that redirect is never followed — the operator closes the
    tab, the browser blocks it, the response is discarded — the raw secret
    stays in the signed (and *unencrypted*) session cookie, replayed on every
    later request until the keys page happens to be visited.

    So any other request clears it: the value is only ever meant to survive
    one hop. This runs on every panel and `/api` route (the dependency below
    is attached router-wide) and on login and logout, which is every way a
    session continues, so "displayed once, then gone" holds regardless of
    which request comes next.
    """
    if request.method in ("GET", "HEAD") and request.url.path.rstrip("/") == "/admin/keys":
        return  # the one render the flash exists for; `keys_page` pops it
    try:
        request.session.pop("flash_new_key", None)
    except (AssertionError, AttributeError):
        # No SessionMiddleware in this app (some unit-test harnesses), so
        # there is no cookie holding anything to forget.
        pass


def _wants_json(request: Request) -> bool:
    """True for the REST surface, which must never be answered with HTML.

    `/api/*` shares this dependency with the browser panel (see
    `src/api/routes.py`), so an expired session sent a JSON client a 302 to a
    login *page*. A programmatic caller follows it, parses the HTML as JSON
    and reports a decoding failure instead of "your session expired".
    """
    return request.url.path == "/api" or request.url.path.startswith("/api/")


async def require_user_panel(
    request: Request,
    user: User | _SingleUserSentinel | None = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Return the logged-in user, or raise a 302 redirect to login.

    In single-user mode `get_current_user` returns the sentinel
    (id=None, is_admin=True, username="admin"), so every handler keeps
    working with zero per-handler branching. In multi-user mode an absent
    or inactive user raises a redirect to the login form, preserving
    `?next=` so the user lands back where they were after signing in.

    Special case: if the users table is empty (fresh multi-user deploy
    or post-flag-flip pre-bootstrap), redirect to `/admin/register`
    instead. Without this, users land on a login form they can't pass
    and have no obvious way to reach the bootstrap form.
    """
    _forget_new_key_flash(request)
    if user is None or (isinstance(user, User) and not user.is_active):
        if _wants_json(request):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Not authenticated",
            )
        target = request.url.path
        if request.url.query:
            target = f"{target}?{request.url.query}"
        if settings.multi_user_mode:
            user_count = (
                await session.execute(select(func.count(User.id)))
            ).scalar() or 0
            if user_count == 0:
                raise HTTPException(
                    status_code=status.HTTP_302_FOUND,
                    headers={"Location": "/admin/register"},
                )
        # FastAPI surfaces an HTTPException with a Location header as a 302.
        # `next` is percent-encoded: interpolating the raw path+query put the
        # original `?a=1&b=2` into the *login* URL's own query string, so the
        # login form read `next=/admin/usage` and dropped everything after the
        # first `&` — and a crafted link could inject parameters of its own
        # into the login handler. `_safe_next` on the consuming side is what
        # keeps the decoded value a relative in-app path.
        raise HTTPException(
            status_code=status.HTTP_302_FOUND,
            headers={"Location": "/admin/auth/login?" + urlencode({"next": target})},
        )
    return user


async def require_admin_panel(
    request: Request,
    user: User | _SingleUserSentinel = Depends(require_user_panel),
):
    """Gate dangerous handlers (settings, user management) on `is_admin`.

    In single-user mode the sentinel reports `is_admin=True` so these
    handlers work exactly as today.

    `request` is here only so the 403 can be *recorded* with the route, the
    method and who was refused (D12); it takes no part in the decision. FastAPI
    injects it, so no call site changes.
    """
    if not user.is_admin:
        actor_user_id, actor_username = _actor(user)
        # No `user_id`: nothing here names a resource with an owner. Absence is
        # the honest rendering — inventing the actor's own id in that field
        # would make "whose resource was this about" unanswerable.
        security_events.emit(
            "panel_forbidden",
            subject=security_events.subject_for(
                user_id=actor_user_id, request=request
            ),
            reason="admin_required",
            actor_user_id=actor_user_id,
            actor_username=actor_username,
            route=_request_route(request),
            method=_request_method(request),
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin required")
    return user


# Attach `require_user_panel` to every route in this router. Individual
# handlers can additionally depend on `require_admin_panel` for the danger
# zone; FastAPI runs both dependencies but the redirect from the user one
# fires first if there's no session.
router.dependencies.append(Depends(require_user_panel))
router.dependencies.append(Depends(verify_csrf))


def _log_panel_forbidden(
    request: Request | None,
    reason: str,
    user,
    owner_user_id: int | None,
) -> None:
    """One `panel_forbidden` record for an ownership refusal.

    `actor_*` is who was refused; `user_id` is the owner of the resource they
    named (D19). The pair is what makes "who tried to touch whose key" a query
    rather than an inference, and it is emitted even when the two are equal so
    that a search for one administrator's actions is complete.

    The response is decided by the caller and is never affected by this.
    """
    actor_user_id, actor_username = _actor(user)
    security_events.emit(
        "panel_forbidden",
        subject=security_events.subject_for(user_id=actor_user_id, request=request),
        reason=reason,
        actor_user_id=actor_user_id,
        actor_username=actor_username,
        user_id=owner_user_id,
        route=_request_route(request),
        method=_request_method(request),
    )


def _panel_context(
    request: Request,
    user: User | _SingleUserSentinel,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Base template context: role/username chrome plus optional extras.

    Every panel handler returning a template merges this in. The single-
    user sentinel has `username="admin"` and `is_admin=True`, so
    `base.html`'s `{% if multi_user_mode and username %}` user-badge block
    stays hidden in single-user mode regardless of how it's rendered.

    It is also the **only** place a panel flash is read (#138). Every render
    pops the session entry, so a message set before a redirect is shown once
    and is gone on reload — and, because it comes from the session, nothing
    a link can carry ever reaches the page. An `extra` may still override
    `flash`/`flash_kind` explicitly; nothing does today.
    """
    flash_message, flash_kind = pop_flash(request)
    ctx: dict[str, Any] = {
        "is_admin": bool(user.is_admin),
        "username": user.username,
        "multi_user_mode": bool(settings.multi_user_mode),
        "csrf_token": generate_csrf_token(request),
        "flash": flash_message,
        "flash_kind": flash_kind,
    }
    if extra:
        ctx.update(extra)
    return ctx


def _is_admin(user: User | _SingleUserSentinel) -> bool:
    return bool(user.is_admin)


def _scope_user_id(user: User | _SingleUserSentinel) -> int | None:
    """Return `user.id` for non-admin scoping, or None for "no filter".

    Admins see all rows on the control-plane surfaces (keys/oauth/usage/
    dashboard); regular users see only their own. Vault contents are a
    separate concern — even admins only see their own vault since
    `vault_page` is intentionally per-user (admin can troubleshoot a
    user's vault by temporarily switching the user record's vault_path,
    not by browsing another user's files in the UI).
    """
    if _is_admin(user):
        return None
    return user.id


# --- Dashboard ------------------------------------------------------------


async def _graph_stats(session: AsyncSession, user_id: int | None) -> dict:
    """Return totals + top hub notes for the dashboard's Graph widget.

    Admins (user_id=None) see vault-wide stats. Regular users see only
    their own notes' link graph — links across users can't physically
    exist because note_links endpoints are always co-user (indexer
    invariant) but we still filter at read time defensively.
    """
    base_links = select(func.count(NoteLink.id))
    dangling_q = select(func.count(NoteLink.id)).where(NoteLink.target_note_id.is_(None))
    if user_id is not None:
        base_links = base_links.join(
            NoteMetadata, NoteMetadata.id == NoteLink.source_note_id
        ).where(NoteMetadata.user_id == user_id)
        dangling_q = dangling_q.join(
            NoteMetadata, NoteMetadata.id == NoteLink.source_note_id
        ).where(NoteMetadata.user_id == user_id)

    total_links = (await session.execute(base_links)).scalar() or 0
    dangling_links = (await session.execute(dangling_q)).scalar() or 0

    # Orphans: notes that appear in neither source_note_id nor target_note_id.
    if user_id is None:
        orphans_stmt = text("""
            SELECT count(*) FROM notes_metadata nm
            WHERE nm.id NOT IN (
                SELECT source_note_id FROM note_links WHERE source_note_id IS NOT NULL
                UNION
                SELECT target_note_id FROM note_links WHERE target_note_id IS NOT NULL
            )
        """)
        orphan_params: dict = {}
    else:
        orphans_stmt = text("""
            SELECT count(*) FROM notes_metadata nm
            WHERE nm.user_id = :uid
              AND nm.id NOT IN (
                SELECT source_note_id FROM note_links WHERE source_note_id IS NOT NULL
                UNION
                SELECT target_note_id FROM note_links WHERE target_note_id IS NOT NULL
            )
        """)
        orphan_params = {"uid": user_id}
    orphan_count = (await session.execute(orphans_stmt, orphan_params)).scalar() or 0

    # Top 5 hub notes by inbound resolved-link count.
    if user_id is None:
        hubs_stmt = text("""
            SELECT nm.file_path, nm.title, count(nl.id) AS hits
            FROM note_links nl
            JOIN notes_metadata nm ON nm.id = nl.target_note_id
            WHERE nl.target_note_id IS NOT NULL
            GROUP BY nm.id, nm.file_path, nm.title
            ORDER BY hits DESC
            LIMIT 5
        """)
        hub_params: dict = {}
    else:
        hubs_stmt = text("""
            SELECT nm.file_path, nm.title, count(nl.id) AS hits
            FROM note_links nl
            JOIN notes_metadata nm ON nm.id = nl.target_note_id
            WHERE nl.target_note_id IS NOT NULL AND nm.user_id = :uid
            GROUP BY nm.id, nm.file_path, nm.title
            ORDER BY hits DESC
            LIMIT 5
        """)
        hub_params = {"uid": user_id}
    hub_rows = (await session.execute(hubs_stmt, hub_params)).fetchall()
    top_hubs = [
        {"path": r.file_path, "title": r.title, "hits": int(r.hits)}
        for r in hub_rows
    ]

    return {
        "total_links": int(total_links),
        "dangling_links": int(dangling_links),
        "orphan_count": int(orphan_count),
        "top_hubs": top_hubs,
    }


@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user=Depends(require_user_panel),
):
    uid = _scope_user_id(user)

    notes_q = select(func.count(NoteMetadata.id))
    keys_q = select(func.count(APIKey.id)).where(APIKey.is_active == True)
    if uid is not None:
        notes_q = notes_q.where(NoteMetadata.user_id == uid)
        keys_q = keys_q.where(APIKey.user_id == uid)

    notes_count = (await session.execute(notes_q)).scalar() or 0

    # Embeddings count joins through notes_metadata when scoping.
    if uid is not None:
        emb_q = (
            select(func.count(func.distinct(NoteEmbedding.note_id)))
            .join(NoteMetadata, NoteMetadata.id == NoteEmbedding.note_id)
            .where(NoteMetadata.user_id == uid)
        )
    else:
        emb_q = select(func.count(func.distinct(NoteEmbedding.note_id)))
    notes_with_embeddings = (await session.execute(emb_q)).scalar() or 0

    keys_count = (await session.execute(keys_q)).scalar() or 0

    if uid is not None:
        requests_today = (await session.execute(
            text(
                "SELECT count(*) FROM usage_logs "
                "WHERE created_at >= date_trunc('day', now()) "
                "AND user_id = :uid"
            ),
            {"uid": uid},
        )).scalar() or 0
    else:
        requests_today = (await session.execute(
            text("SELECT count(*) FROM usage_logs WHERE created_at >= date_trunc('day', now())")
        )).scalar() or 0

    embedding_pct = round(notes_with_embeddings / notes_count * 100) if notes_count else 0

    cutoff_24h = datetime.now(timezone.utc) - timedelta(hours=24)
    reindex_q = select(func.count(NoteMetadata.id)).where(NoteMetadata.indexed_at >= cutoff_24h)
    last_indexed_q = select(func.max(NoteMetadata.indexed_at))
    if uid is not None:
        reindex_q = reindex_q.where(NoteMetadata.user_id == uid)
        last_indexed_q = last_indexed_q.where(NoteMetadata.user_id == uid)
    reindexed_24h = (await session.execute(reindex_q)).scalar() or 0
    last_indexed_at = (await session.execute(last_indexed_q)).scalar()

    # Recent usage
    usage_q = select(UsageLog).order_by(UsageLog.created_at.desc()).limit(10)
    if uid is not None:
        usage_q = usage_q.where(UsageLog.user_id == uid)
    result = await session.execute(usage_q)
    def _usage_detail(tool: str, params: dict | None) -> str | None:
        if not params:
            return None
        # "search_notes" is the legacy spelling `keyword_search` was logged
        # under before #78; historical rows keep it, so it stays here.
        if tool in ("search_notes", "keyword_search", "semantic_search"):
            return params.get("query")
        if tool in ("read_note", "create_note", "edit_note"):
            return params.get("path")
        if tool in ("list_notes", "get_recent"):
            return params.get("folder") or None
        return None
    recent_usage = [
        {
            "tool": l.tool,
            "detail": _usage_detail(l.tool, l.params),
            "created_at": l.created_at.isoformat(),
        }
        for l in result.scalars().all()
    ]

    graph = await _graph_stats(session, uid)
    # Imported at call time so the values are read fresh each request (module
    # attributes, not a snapshot taken at import).
    from src.services import indexer as _indexer
    from src.services.indexer import link_backfill_in_progress

    # "Last run" is the indexer's own heartbeat, not `max(indexed_at)`.
    # `notes_metadata.indexed_at` only moves for notes a pass actually
    # upserted or moved, so on an idle vault a perfectly healthy indexer
    # looked stalled for days (#78). It is process-wide (the loop is), so it
    # is not scoped by `uid` the way the note aggregates are — that is a fact
    # about the server, and every panel user sees the same one.
    last_run_at = _indexer.last_index_run_at
    last_run_ok = _indexer.last_index_run_ok

    # The persisted counterpart to the heartbeat above: last recorded pass,
    # backup age, errors since process start. Survives a restart, which the
    # heartbeat does not, and links to the full history on /admin/health.
    health = await _health_strip_or_degraded(session, user)

    return templates.TemplateResponse(request, "dashboard.html", _panel_context(request, user, {
        "active": "dashboard",
        "stats": {
            "notes_indexed": notes_count,
            "notes_with_embeddings": notes_with_embeddings,
            "embedding_pct": embedding_pct,
            "active_keys": keys_count,
            "requests_today": requests_today,
        },
        "recent_usage": recent_usage,
        "reindexed_24h": reindexed_24h,
        "last_indexed_iso": last_indexed_at.isoformat() if last_indexed_at else None,
        "last_indexed_rel": _humanize_delta(last_indexed_at),
        "last_run_iso": last_run_at.isoformat() if last_run_at else None,
        "last_run_rel": _humanize_delta(last_run_at),
        "last_run_ok": last_run_ok,
        "index_interval": settings.index_interval_seconds,
        "graph": graph,
        "graph_backfill_running": link_backfill_in_progress,
        "health": health,
    }))


# --- API keys -------------------------------------------------------------


@router.get("/keys", response_class=HTMLResponse)
async def keys_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user=Depends(require_user_panel),
):
    uid = _scope_user_id(user)
    # Join the owner. A key's *effective* liveness is what `APIKeyMiddleware`
    # enforces, and that is stricter than `api_keys.is_active` alone:
    # `src/mcp_server/auth.py` also selects `User.is_active` for the key's
    # `user_id` and 401s (reason=inactive_user) unless it is exactly True.
    # Deactivating a user leaves their keys' own `is_active` untouched, so
    # without this join the panel badged dead credentials green (#76). The
    # outer join keeps single-user keys (`user_id IS NULL`), which the
    # middleware exempts from the owner check entirely.
    q = (
        select(APIKey, User.username, User.is_active)
        .select_from(APIKey)
        .outerjoin(User, User.id == APIKey.user_id)
        .order_by(APIKey.created_at.desc())
    )
    if uid is not None:
        q = q.where(APIKey.user_id == uid)
    result = await session.execute(q)
    # One `now` for the whole page: rows compared against different instants
    # could render two keys with the same `expires_at` differently — and the
    # quota day is read from the same instant, so a page rendered across a UTC
    # midnight cannot show one key's consumption against today and another's
    # against yesterday.
    now = datetime.now(timezone.utc)
    rows = result.all()
    # Today's admissions, from the counter rows rather than a COUNT over
    # `usage_logs` (#162). The log counts refused calls and traffic from before
    # the limit existed; neither is what "43 / 100" tells an operator. One
    # statement for the whole page — a per-key query here is a query per row.
    consumed = await consumed_today(session, [k.id for k, _, _ in rows], now)
    quota_day = utc_day(now).isoformat()
    keys = []
    for k, owner_username, owner_is_active in rows:
        # A key whose `user_id` has no row (owner_is_active is None) is dead
        # too — the middleware's `is True` test fails for it just the same,
        # so it must not read as live here.
        owner_ok = k.user_id is None or owner_is_active is True
        # Expiry, phrased exactly as `APIKeyMiddleware` phrases it:
        # `if api_key.expires_at and api_key.expires_at < now: 401`. So a key
        # is *not* expired at exactly `expires_at` — the boundary instant is
        # still live — and this comparison must keep matching that one rather
        # than a plausible-looking `>=` of its own. Nothing in the codebase
        # sets `expires_at` today, but the panel must not be the surface that
        # goes stale the day something does.
        expired = k.expires_at is not None and k.expires_at < now
        keys.append({
            "id": k.id,
            "name": k.name,
            "key_prefix": k.key_prefix,
            "permission": k.permission,
            "is_active": k.is_active,
            "owner_is_active": owner_ok,
            "is_expired": expired,
            "expires_at": k.expires_at.isoformat() if k.expires_at else None,
            "effective_active": bool(k.is_active) and owner_ok and not expired,
            "owner_username": owner_username,
            "created_at": k.created_at.isoformat(),
            "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None,
            "user_id": k.user_id,
            # NULL means unlimited, and an unlimited key performs no quota
            # accounting at all — so `quota_used` is only meaningful beside a
            # limit, and the template says "Unlimited" without one rather than
            # printing a `0 / —` that would read as a measurement of something.
            "daily_request_limit": k.daily_request_limit,
            # Absent counter row = 0. True twice over: nothing admitted today,
            # and enabling a limit deletes the day's row so consumption is
            # counted from the enablement, not from midnight.
            "quota_used": consumed.get(k.id, 0),
        })
    try:
        new_key = request.session.pop("flash_new_key", None)
        key_error = request.session.pop("flash_key_error", None)
    except (AssertionError, AttributeError):
        new_key = None
        key_error = None
    return templates.TemplateResponse(request, "keys.html", _panel_context(request, user, {
        "active": "keys", "keys": keys, "new_key": new_key, "key_error": key_error,
        "quota_day": quota_day,
        "quota_limit_max": DAILY_REQUEST_LIMIT_MAX,
    }))


# The JSON twin of this form (`CreateKeyRequest` in `src/api/routes.py`)
# constrains the name; the form did not, so the two surfaces disagreed about
# the same column. `name` is `String(255)` — over that, PostgreSQL raises and
# the operator gets a 500 with no key and no explanation — and a name of pure
# whitespace produced an unidentifiable row on a page whose only handle on a
# credential *is* its name.
_KEY_NAME_MAX = 255
_KEY_NAME_RE = re.compile(r"^[\w\-. ]+$")


def _key_name_error(name: str) -> str | None:
    """Validate a form-submitted key name exactly as the JSON API does.

    Returns the message to show, or None. The strip is this side's own
    addition: pydantic's `min_length=1` accepts `"   "` because the pattern
    admits the space character, which is defensible for a name with spaces
    *in* it and useless as a whole name.
    """
    if not name.strip():
        return "Key name is required."
    if len(name) > _KEY_NAME_MAX:
        return f"Key name must be at most {_KEY_NAME_MAX} characters."
    if not _KEY_NAME_RE.match(name):
        return (
            "Key name may contain only letters, digits, spaces, and the "
            "characters _ - . "
        )
    return None


def _flash_key_error(request: Request, message: str) -> None:
    """Carry a key-form error to the redirect target.

    In the session rather than a query string: the value is the server's own
    message, and a `?error=` the operator can be linked to lets a third party
    choose what an authenticated admin reads.
    """
    try:
        request.session["flash_key_error"] = message
    except (AssertionError, AttributeError):
        pass


@router.post("/keys/create")
async def create_key_form(
    request: Request,
    name: str = Form(...),
    permission: str = Form("read"),
    daily_request_limit: str = Form(""),
    session: AsyncSession = Depends(get_session),
    user=Depends(require_user_panel),
):
    name_error = _key_name_error(name)
    if name_error is not None:
        _flash_key_error(request, name_error)
        return RedirectResponse("/admin/keys", status_code=303)

    # Server-side, above the DB CHECK (#162). Both layers: the constraint is
    # what makes the invariant true of the data, and this is what makes it
    # fixable — a violated CHECK is a 500 with no key and no explanation.
    limit, limit_error = parse_limit_form_value(daily_request_limit)
    if limit_error is not None:
        _flash_key_error(request, limit_error)
        return RedirectResponse("/admin/keys", status_code=303)

    raw_key = f"omcp_{secrets.token_hex(24)}"
    # The keys.html <select> only constrains the UI; a scripted/tampered POST
    # can submit any value. Mirror the JSON API's invariant (src/api/routes.py)
    # and fail safe to read-only so the column never holds nonsense like
    # "admin" or a trailing-space "readwrite " that silently behaves as read.
    if permission not in ("read", "readwrite"):
        permission = "read"
    # Always stamp the creator's user_id (even admins get their own keys
    # attributed to themselves — admin's omniscient view doesn't extend to
    # "create keys on behalf of"; that's a separate per-user-edit action).
    api_key = APIKey(
        name=name,
        key_hash=hash_key(raw_key),
        key_prefix=raw_key[:12],
        permission=permission,
        user_id=user.id,
        daily_request_limit=limit,
    )
    session.add(api_key)
    await session.commit()
    try:
        request.session["flash_new_key"] = raw_key
    except (AssertionError, AttributeError):
        pass
    return RedirectResponse("/admin/keys", status_code=303)


@router.post("/keys/delete-revoked")
async def delete_all_revoked(
    session: AsyncSession = Depends(get_session),
    user=Depends(require_user_panel),
):
    from sqlalchemy import delete as sa_delete, update as sa_update
    uid = _scope_user_id(user)
    revoked_q = select(APIKey.id).where(APIKey.is_active == False)
    if uid is not None:
        revoked_q = revoked_q.where(APIKey.user_id == uid)
    revoked_ids = (await session.execute(revoked_q)).scalars().all()
    if revoked_ids:
        await session.execute(
            sa_update(UsageLog).where(UsageLog.key_id.in_(revoked_ids)).values(key_id=None)
        )
        await session.execute(sa_delete(APIKey).where(APIKey.id.in_(revoked_ids)))
        await session.commit()
    return RedirectResponse("/admin/keys", status_code=303)


def _assert_key_owner(
    key: APIKey | None,
    user: User | _SingleUserSentinel,
    *,
    request: Request | None = None,
) -> APIKey:
    """The shared key-ownership predicate. `request` is for the record only.

    Keyword-only and defaulted so the predicate keeps its meaning if a future
    caller has no request in hand: a refusal with no route recorded is worse
    than no refusal recorded, but neither may change who is allowed through.
    `src/api/routes.py` calls this too — see D12.
    """
    if key is None:
        raise HTTPException(404, "Key not found")
    # Admin can mutate any key. Regular user can only mutate their own.
    if not _is_admin(user) and key.user_id != user.id:
        _log_panel_forbidden(request, "not_your_key", user, key.user_id)
        raise HTTPException(403, "Not your key")
    return key


@router.post("/keys/{key_id}/revoke")
async def revoke_key_form(
    key_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user=Depends(require_user_panel),
):
    result = await session.execute(select(APIKey).where(APIKey.id == key_id))
    api_key = result.scalar_one_or_none()
    _assert_key_owner(api_key, user, request=request)
    api_key.is_active = False
    await session.commit()
    return RedirectResponse("/admin/keys", status_code=303)


@router.post("/keys/{key_id}/limit")
async def set_key_limit_form(
    request: Request,
    key_id: int,
    daily_request_limit: str = Form(""),
    session: AsyncSession = Depends(get_session),
    user=Depends(require_user_panel),
):
    """Set, change, or clear a key's daily request limit (#162).

    The enable-reset rule and its single transaction live in
    `src.services.quotas.apply_daily_request_limit`, shared with the JSON API's
    twin endpoint — two copies of "did this go NULL to a value" is how the two
    surfaces start disagreeing about whether an operator is charged for traffic
    that was unlimited when it happened, invisibly, because both would look
    like they worked.
    """
    result = await session.execute(select(APIKey).where(APIKey.id == key_id))
    api_key = result.scalar_one_or_none()
    _assert_key_owner(api_key, user, request=request)

    limit, limit_error = parse_limit_form_value(daily_request_limit)
    if limit_error is not None:
        _flash_key_error(request, limit_error)
        return RedirectResponse("/admin/keys", status_code=303)

    await apply_daily_request_limit(session, api_key, limit)
    return RedirectResponse("/admin/keys", status_code=303)


@router.post("/keys/{key_id}/delete")
async def delete_key_form(
    request: Request,
    key_id: int,
    session: AsyncSession = Depends(get_session),
    user=Depends(require_user_panel),
):
    from sqlalchemy import update as sa_update
    result = await session.execute(
        select(APIKey).where(APIKey.id == key_id, APIKey.is_active == False)
    )
    api_key = result.scalar_one_or_none()
    _assert_key_owner(api_key, user, request=request)
    await session.execute(
        sa_update(UsageLog).where(UsageLog.key_id == key_id).values(key_id=None)
    )
    await session.delete(api_key)
    await session.commit()
    return RedirectResponse("/admin/keys", status_code=303)


# --- OAuth ----------------------------------------------------------------


# How many dead (revoked or expired) tokens to render per grant. Revocation
# history is the point — a Revoke that made the row vanish read as success even
# when it was a no-op (issue #64) — but a client that refreshes hourly leaves
# hundreds of rotated-away refresh tokens in a 30-day window, and a page that
# renders all of them buries the live pair the operator came to look at. Older
# rows are counted and reported, never silently dropped.
GRANT_HISTORY_LIMIT = 5

# Upper bound on *dead* token rows read per client. Live rows are deliberately
# unbounded: a single bound over the whole table applied before grants were
# identified could push a live grant's refresh token off the page entirely,
# leaving a working credential with no control to revoke it. That is a strictly
# worse failure than losing some history, and it is reachable — 501 rotations
# of one chatty grant were enough. Dead rows are fetched newest-first, so what
# a truncation loses is the oldest history, never a live grant and never a
# recent revocation.
CLIENT_DEAD_TOKEN_SCAN_LIMIT = 500


def _token_status(token, now: datetime, owner_active: bool) -> str:
    """The status of one token row, strictest reason first.

    `owner_active` is the part the panel used to ignore (issue #76):
    `APIKeyMiddleware` additionally requires the owning `User.is_active` and
    401s with `reason=inactive_user` otherwise, so a deactivated user's tokens
    were already dead while this page rendered them green. The error direction
    matters — this can over-report deadness, never liveness, so no working
    credential is ever shown as revoked.
    """
    if token.revoked:
        return "revoked"
    if token.expires_at <= now:
        return "expired"
    if not has_vault_scope(token.scope):
        # `offline_access` says the grant may carry a refresh token, not that
        # it may read a note, so `src/mcp_server/auth.py` 401s this token
        # (`reason=no_vault_scope`). No path can mint one any more, but a
        # client registered `scope="offline_access"` before that could already
        # hold one — and the panel showed it "Active" with a working scope
        # control, which is the same over-reporting of liveness #76 was about.
        return "no_vault_scope"
    if not owner_active:
        return "owner_inactive"
    return "active"


async def _owner_active_map(session: AsyncSession, user_ids: set[int]) -> dict[int, bool]:
    """`{user_id: is_active}` for the owners of the tokens on this page.

    A `user_id` missing from the result — a deleted user, whose tokens the FK
    cascade should have taken with it — is read as inactive by the caller,
    which is the same direction the middleware fails in.
    """
    if not user_ids:
        return {}
    result = await session.execute(
        select(User.id, User.is_active).where(User.id.in_(user_ids))
    )
    return {row[0]: bool(row[1]) for row in result.all()}


def _grant_view(grant_id: str, family: list, now: datetime, active_by_owner: dict[int, bool]) -> dict:
    """One grant family as the template consumes it.

    `family` is every token row sharing `grant_id`, newest first. A *live*
    token is one that is neither revoked nor expired; whether its owner is
    still active changes the badge but not liveness, because reactivating the
    user brings the same token back rather than minting a new one.
    """
    live: list[tuple] = []
    dead: list[dict] = []
    for t in family:
        owner_active = t.user_id is None or active_by_owner.get(t.user_id) is True
        status = _token_status(t, now, owner_active)
        row = {
            "id": t.id,
            "token_type": t.token_type,
            "scope": t.scope,
            "status": status,
            "expires_at": t.expires_at.isoformat(),
            "created_at": t.created_at.isoformat(),
        }
        if status in ("active", "owner_inactive"):
            live.append((t, row))
        else:
            # `no_vault_scope` counts as dead, deliberately: the middleware
            # rejects it, so treating it as live would offer a Revoke and a
            # scope select for a credential that cannot authenticate — and the
            # scope select would try to write a scope its client is not
            # registered for, which `update_oauth_token_scope` refuses anyway.
            dead.append(row)

    # Every still-usable token is always rendered; only history is capped.
    shown = [row for _, row in live] + dead[:GRANT_HISTORY_LIMIT]
    hidden = max(0, len(dead) - GRANT_HISTORY_LIMIT)

    # One scope control and one "Revoke access" per grant, addressed through a
    # representative live token. Acting on a single row was the whole defect:
    # `_handle_refresh` copies the *refresh* token's scope, so a downgrade
    # applied to the access row silently restored itself on the next rotation,
    # and a Revoke on the access row bought at most its one-hour lifetime. The
    # handlers behind these forms resolve the family and write all of it.
    if live:
        representative, representative_row = live[0]
        token_id = representative.id
        status = representative_row["status"]
        scope = representative_row["scope"]
    else:
        # Nothing left to act on, so no controls are offered. The rows are
        # still listed: a revocation that leaves a blank space reads as
        # success even when it did nothing.
        token_id = None
        if all(t.revoked for t in family):
            status = "revoked"
        elif not has_vault_scope(family[0].scope):
            status = "no_vault_scope"
        else:
            status = "expired"
        scope = family[0].scope

    # A family is normally uniform — every row descends from one consent and
    # rotation copies the scope — but migration 014's backfill can legitimately
    # merge two pre-014 sessions of the same client and user, one `read` and
    # one `readwrite`. Reading the permission off the newest live row alone
    # would then show "read" while an older live access token still holds
    # write. `any` is the fail-safe direction: it over-reports capability, so
    # the operator is never told a grant is narrower than it is.
    live_write = [token_has_write(t.scope) for t, _ in live]
    has_write = any(live_write) if live_write else token_has_write(scope)
    mixed_scope = bool(live_write) and any(live_write) and not all(live_write)

    return {
        "grant_id": grant_id,
        "token_id": token_id,
        "scope": scope,
        # Membership, not equality: "offline_access readwrite" is a write grant
        # and `== "readwrite"` says it is not. Derived here, from the tokens'
        # own scopes, through the same helper `src/mcp_server/auth.py` enforces
        # with — the two agreeing is the property that actually matters (#65).
        "has_write": has_write,
        "mixed_scope": mixed_scope,
        "live_scopes": sorted({t.scope for t, _ in live}),
        "status": status,
        "created_at": min(t.created_at for t in family).isoformat(),
        "last_seen_at": max(t.created_at for t in family).isoformat(),
        "tokens": shown,
        "hidden_count": hidden,
    }


@router.get("/oauth", response_class=HTMLResponse)
async def oauth_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user=Depends(require_user_panel),
):
    uid = _scope_user_id(user)
    now = datetime.now(timezone.utc)
    q = select(OAuthClient).order_by(OAuthClient.created_at.desc())
    if uid is not None:
        q = q.where(OAuthClient.user_id == uid)
    result = await session.execute(q)
    client_rows = list(result.scalars().all())

    # Tokens inherit their client's scope, but we also scope tokens by
    # user_id directly for defense in depth: an unbound/legacy client
    # could have tokens stamped with a user_id that diverges from the
    # client's. Filter both ways.
    #
    # Revoked and expired rows are *included* now. Filtering them out is what
    # made a per-row Revoke read as success: the row simply disappeared, the
    # sibling refresh token minted a replacement pair within the hour, and the
    # page showed a fresh access+refresh pair as if nothing had happened. It
    # also left the template's `revoked` branches as unreachable dead code.
    tokens_by_client: dict[str, list] = {}
    truncated_clients: set[str] = set()
    owner_ids: set[int] = set()
    for c in client_rows:
        def _scoped(query):
            query = query.where(OAuthToken.client_id == c.client_id)
            return query if uid is None else query.where(OAuthToken.user_id == uid)

        # Every *live* token, unbounded. A bound here would decide which grants
        # the operator can act on, and a chatty grant's rotations would decide
        # it for them.
        live_q = _scoped(
            select(OAuthToken).where(
                OAuthToken.revoked == False,
                OAuthToken.expires_at > now,
            )
        ).order_by(OAuthToken.created_at.desc(), OAuthToken.id.desc())
        live_rows = list((await session.execute(live_q)).scalars().all())

        # History, newest first and bounded. Losing the tail of this costs an
        # old row nobody can act on; losing a live row costs a revocation.
        dead_q = (
            _scoped(
                select(OAuthToken).where(
                    or_(
                        OAuthToken.revoked == True,
                        OAuthToken.expires_at <= now,
                    )
                )
            )
            .order_by(OAuthToken.created_at.desc(), OAuthToken.id.desc())
            .limit(CLIENT_DEAD_TOKEN_SCAN_LIMIT)
        )
        dead_rows = list((await session.execute(dead_q)).scalars().all())
        if len(dead_rows) == CLIENT_DEAD_TOKEN_SCAN_LIMIT:
            # Say so rather than printing a total we did not count.
            truncated_clients.add(c.client_id)

        rows = live_rows + dead_rows
        tokens_by_client[c.client_id] = rows
        owner_ids.update(t.user_id for t in rows if t.user_id is not None)

    active_by_owner = await _owner_active_map(session, owner_ids)

    clients = []
    for c in client_rows:
        # Group into grant families. `grant_id` is NOT NULL (migration 014) and
        # is the *only* way a family is ever resolved — the decision in #64 was
        # explicit that a second "find the family" path is how the bug returns.
        grouped: dict[str, list] = {}
        for t in tokens_by_client[c.client_id]:
            grouped.setdefault(t.grant_id, []).append(t)

        history_truncated = c.client_id in truncated_clients
        grants = []
        for grant_id, family in grouped.items():
            # `_grant_view` expects the family newest-first; the two queries
            # were each ordered that way but concatenated live-then-dead.
            family.sort(key=lambda t: (t.created_at, t.id), reverse=True)
            view = _grant_view(grant_id, family, now, active_by_owner)
            view["history_truncated"] = history_truncated
            grants.append(view)
        grants.sort(key=lambda g: g["last_seen_at"], reverse=True)

        clients.append({
            "client_id": c.client_id,
            "client_name": c.client_name,
            "created_at": c.created_at.isoformat(),
            # The registered scope caps everything this client's grants may
            # hold, so the panel must not offer an option above it (issue #67).
            # Same helper the consent screen and the token endpoint use.
            "can_write": client_can_write(c.scope),
            "scope": c.scope,
            "grants": grants,
        })

    # Why the last scope change did nothing, if it did nothing. The select
    # posts on `onchange`, so a bare redirect reads as the browser undoing the
    # operator's click. Absent in single-user mode, which has no session.
    try:
        flash_error = request.session.pop("flash_oauth_error", None)
    except (AssertionError, AttributeError):
        flash_error = None

    return templates.TemplateResponse(request, "oauth.html", _panel_context(request, user, {
        "active": "oauth", "clients": clients, "flash_error": flash_error,
    }))


async def _assert_oauth_client_owner(
    session: AsyncSession,
    client_id: str,
    user: User | _SingleUserSentinel,
    *,
    request: Request | None = None,
) -> OAuthClient:
    result = await session.execute(select(OAuthClient).where(OAuthClient.client_id == client_id))
    client = result.scalar_one_or_none()
    if client is None:
        raise HTTPException(404, "Client not found")
    if not _is_admin(user) and client.user_id != user.id:
        _log_panel_forbidden(request, "not_your_client", user, client.user_id)
        raise HTTPException(403, "Not your client")
    return client


async def _assert_oauth_token_owner(
    session: AsyncSession,
    token_id: int,
    user: User | _SingleUserSentinel,
    *,
    request: Request | None = None,
) -> OAuthToken:
    result = await session.execute(select(OAuthToken).where(OAuthToken.id == token_id))
    token = result.scalar_one_or_none()
    if token is None:
        raise HTTPException(404, "Token not found")
    if not _is_admin(user) and token.user_id != user.id:
        _log_panel_forbidden(request, "not_your_token", user, token.user_id)
        raise HTTPException(403, "Not your token")
    return token


@router.post("/oauth/{client_id}/delete")
async def delete_oauth_client(
    client_id: str,
    request: Request = None,
    session: AsyncSession = Depends(get_session),
    user=Depends(require_user_panel),
):
    """Delete the client row and let the cascades run. Attribution survives.

    The button used to be labelled "Delete this client and revoke all its
    tokens?", which described something weaker and more reassuring than what
    happens (issue #77). The delete cascades `oauth_tokens`, `oauth_codes` and
    — through `transfer_tokens.oauth_token_id` — any outstanding transfer
    capabilities minted under those tokens. All of that is *wanted*: it is what
    makes the delete a real stop, and it is stronger than flipping `revoked` on
    each token, which #64 established is not durable while the client row still
    exists to be refreshed against.

    What was not wanted is the fourth cascade. `usage_logs.oauth_token_id` is
    ON DELETE SET NULL, so every historical line the client produced lost its
    actor, and `/admin/usage` — which resolved the actor by joining back
    through `oauth_tokens` — rendered them "unknown". An operator stopping a
    suspect connector destroyed the evidence they were about to read.

    That is fixed at the source rather than here: `usage_logs` now carries the
    actor label denormalised at call time (`actor_kind` / `actor_label` /
    `actor_ref`, migration 015), so this delete no longer touches attribution
    at all and the confirm text can honestly promise the history stays. Do not
    "fix" this by swapping the delete for a revoke — see #64 and #77.
    """
    client = await _assert_oauth_client_owner(session, client_id, user, request=request)
    await session.delete(client)
    await session.commit()
    return RedirectResponse("/admin/oauth", status_code=303)


@router.post("/oauth/token/{token_id}/revoke")
async def revoke_oauth_token(
    token_id: int,
    request: Request = None,
    session: AsyncSession = Depends(get_session),
    user=Depends(require_user_panel),
):
    """Revoke the whole grant family, not the single row that was clicked.

    Flipping one row was close to a durable no-op (issue #64): the sibling
    refresh token was untouched, `_handle_refresh` resolves on
    `token_hash` + `token_type` + `revoked` alone, and the client's ordinary
    401-then-refresh cycle minted a fresh, identically-scoped pair — within the
    hour for an access token, automatically, with no sign of it in the panel.

    `token_id` is now a handle on the grant, not the unit of work. Ownership is
    still asserted on the row the operator named; the family cannot span users
    (see `src/oauth/grants.py`), so that check covers everything this touches.
    """
    token = await _assert_oauth_token_owner(session, token_id, user, request=request)
    revoked = await revoke_grant_family(session, token.grant_id)
    await session.commit()
    # **After the commit** (D17): a record asserting tokens were revoked must
    # not outlive a transaction that then rolled back. And emitted *here*
    # rather than inside `revoke_grant_family` (D10), which has no request, no
    # client address and no session user, and does not commit — the rowcount it
    # returns and every caller discarded is what `count` carries.
    #
    # Both halves of D19's pair: `actor_user_id` is the administrator who
    # clicked, `user_id` is the grant's owner. An admin revoking someone else's
    # grant used to produce one ambiguous id.
    actor_user_id, _actor_username = _actor(user)
    security_events.emit(
        "oauth_grant_revoked",
        level=logging.INFO,
        subject=security_events.subject_for(user_id=actor_user_id, request=request),
        client_id=token.client_id,
        user_id=token.user_id,
        actor_user_id=actor_user_id,
        grant_id=token.grant_id,
        count=revoked,
        client_ip=security_events.client_ip(request),
        route=_request_route(request),
    )
    return RedirectResponse("/admin/oauth", status_code=303)


def _flash_oauth_error(request: Request | None, message: str) -> None:
    """Say why a scope change did nothing, instead of snapping the select back.

    The select posts on `onchange`, so a silent redirect looks like the browser
    reverting the operator's click for no reason. Session flashes are absent in
    single-user mode (no `SessionMiddleware`), which is the same
    `AssertionError` dance `create_key_form` does — the refusal still holds
    there, it just goes unexplained.
    """
    if request is None:
        return
    try:
        request.session["flash_oauth_error"] = message
    except (AssertionError, AttributeError):
        pass


@router.post("/oauth/token/{token_id}/scope")
async def update_oauth_token_scope(
    token_id: int,
    request: Request = None,
    scope: str = Form(...),
    session: AsyncSession = Depends(get_session),
    user=Depends(require_user_panel),
):
    """Set one scope on every live token in the grant. Never above the client's.

    Two defects meet here. Writing a single row let the change revert on the
    next rotation, because `_handle_refresh` copies the *refresh* token's scope
    (issue #64). And nothing clamped the submitted value against
    `OAuthClient.scope`, so the panel could hand `readwrite` to a client that
    registered read-only — permanently, since rotation re-minted from the
    token's own scope forever after (issue #67).

    The registration is a statement about that software, so a request above it
    is refused outright rather than quietly clamped: an operator who picked
    `readwrite` should not be told nothing happened when the select snaps back.
    The clamp still runs afterwards as the belt to that braces — no path may
    write a scope the client is not registered for.
    """
    if scope not in ("read", "readwrite"):
        return RedirectResponse("/admin/oauth", status_code=303)
    token = await _assert_oauth_token_owner(session, token_id, user, request=request)
    if token.revoked:
        return RedirectResponse("/admin/oauth", status_code=303)

    client = (
        await session.execute(
            select(OAuthClient).where(OAuthClient.client_id == token.client_id)
        )
    ).scalar_one_or_none()
    if client is None:
        raise HTTPException(404, "Client not found")
    if scope == "readwrite" and not client_can_write(client.scope):
        _flash_oauth_error(
            request,
            f"'{client.client_name}' is registered for '{client.scope}', so its "
            "grants cannot be raised to readwrite. Re-register the client if it "
            "genuinely needs write access.",
        )
        return RedirectResponse("/admin/oauth", status_code=303)

    # Preserve the offline_access marker (informational only - see
    # DEFAULT_CLIENT_SCOPE comment in src/oauth/routes.py) instead of
    # dropping it whenever an admin flips read/readwrite here.
    #
    # Read across the whole live family, not just the row the operator clicked:
    # 014's backfill can merge two pre-014 sessions into one family, and one of
    # them may carry the marker while the other does not. The write below is
    # uniform either way, which is the point — a mixed family is exactly the
    # case where deciding from one row leaves the others disagreeing.
    live_scopes = await live_family_scopes(session, token.grant_id)
    has_offline = any("offline_access" in s.split() for s in live_scopes)
    desired = f"{scope} offline_access" if has_offline else scope
    granted = clamp_scope(desired, client.scope)
    if not granted:
        # The registration names no vault scope at all (`offline_access` alone
        # is not one). `clamp_scope` used to answer `read` here, so this
        # control could hand a client read access to the whole vault that its
        # registration never granted. Writing an *empty* scope would be worse
        # still — `src/mcp_server/auth.py` maps anything without `readwrite`
        # to `read`, so an empty scope string reads as read access.
        _flash_oauth_error(
            request,
            f"'{client.client_name}' is registered for '{client.scope}', which "
            "grants no vault access, so its tokens cannot be given a scope. "
            "Re-register the client with 'read' or 'readwrite'.",
        )
        return RedirectResponse("/admin/oauth", status_code=303)

    await set_grant_family_scope(session, token.grant_id, granted)
    await session.commit()
    return RedirectResponse("/admin/oauth", status_code=303)


# --- Usage ----------------------------------------------------------------


def _usage_actor(row) -> tuple[str | None, str | None]:
    """`(name, detail)` for one usage row — denormalised label first.

    The LEFT JOINs through `api_keys` and `oauth_tokens` -> `oauth_clients` are
    still there, but they are now the *fallback*, not the source. They go NULL
    precisely when the operator most needs the answer: deleting an OAuth client
    cascades its tokens and `usage_logs.oauth_token_id` is ON DELETE SET NULL,
    and the panel NULLs `usage_logs.key_id` by hand before deleting an API key
    (that column has no ON DELETE). Either way every historical line of the
    credential being investigated rendered as "unknown" (issue #77).

    So `actor_*` — written at call time, immune to a later delete — wins, and
    the join only answers for rows written before migration 015. `(None, None)`
    means both are gone, which the template renders as
    "unknown (credential deleted)" rather than a bare "unknown": the row did
    have an actor, and saying so is the difference between a gap in the data
    and a gap in the audit trail.
    """
    # The gate is `actor_label`, not `actor_kind`. A kind with no label names
    # nothing an operator can read, so treating it as "recorded" would suppress
    # a join that could still have answered. The writer never produces that
    # combination -- `api_keys.name` and `oauth_clients.client_name` are both
    # NOT NULL -- so preferring the join there costs nothing and fails safe.
    if row.actor_label:
        if row.actor_kind == "api_key":
            # Same two lines the join produced: name, then the omcp_ prefix.
            return (row.actor_label, row.actor_ref)
        if row.actor_kind == "oauth":
            # For OAuth the second line used to be the literal string "OAuth".
            # The client_id is strictly more useful — it is what identifies the
            # connector once its row is gone — so it is shown alongside it.
            return (
                row.actor_label,
                f"OAuth · {row.actor_ref}" if row.actor_ref else "OAuth",
            )
        # A kind this version does not know about — a row written by a newer
        # build, or by hand. Show the label and whatever reference it carries,
        # and do not assert a credential type. Falling through to the OAuth
        # branch (the previous shape) would have labelled an API key "OAuth".
        return (row.actor_label, row.actor_ref)
    if row.api_key_name:
        return (row.api_key_name, row.api_key_prefix)
    if row.oauth_client_name:
        return (row.oauth_client_name, "OAuth")
    return (None, None)


#: The marker a raising tool body writes (`src/mcp_server/tools.py`). It is the
#: one value that renders as a *failure* rather than a refusal, so it is named
#: here rather than spelled inline in the mapping below.
_TOOL_EXCEPTION_MARKER = "tool_exception"


def _usage_outcome(row) -> dict | None:
    """The displayed outcome for one request-log row, or `None` for a plain call.

    The precedence is declared (design D9) and total, because the three raw
    values can co-occur — a body that recorded `over_quota` and then raised
    carries both — and a page that decided by whichever branch it tested first
    would rank them differently as the code moved:

    1. `tool_exception` — the body ran and raised. Rendered as a **failure**
       carrying the exception class, because "it broke" and "it was refused"
       are different answers to "why did this call not do anything".
    2. any other `error` marker — a refusal, showing the marker as its reason.
    3. `over_quota` equal to the string `"true"` — a refusal for quota.
    4. any other non-empty `over_quota` value — a refusal showing the **raw
       value**. This is the malformed case, and showing it beats hiding it: the
       row is not an ordinary successful call, and the operator is the one who
       can tell whether a `"True"`, a `"1"` or a hand-edited `"false"` in there
       is a bug or a repair. `_tracked` writes the key only when it refuses,
       and only as the JSON boolean `true`, so nothing the server produces
       reaches this branch. Nothing is cast, so nothing here can raise — which
       is the whole reason the value arrives as text.
    5. none of the above — no outcome; the row rendered exactly as it always
       has.
    """
    error = getattr(row, "error_marker", None)
    if error:
        if error == _TOOL_EXCEPTION_MARKER:
            return {
                "kind": "failed",
                "label": "failed",
                "detail": getattr(row, "error_type", None),
            }
        return {"kind": "refused", "label": "refused", "detail": error}
    over_quota = getattr(row, "over_quota", None)
    if over_quota:
        if over_quota == "true":
            return {"kind": "refused", "label": "refused", "detail": "over_quota"}
        return {"kind": "refused", "label": "refused", "detail": over_quota}
    return None


@router.get("/usage", response_class=HTMLResponse)
async def usage_page(
    request: Request,
    window: str | None = None,
    user_filter: str | None = Query(None, alias="user"),
    key_filter: str | None = Query(None, alias="key"),
    tool_filter: str | None = Query(None, alias="tool"),
    session: AsyncSession = Depends(get_session),
    user=Depends(require_user_panel),
):
    """The request log, its chart, and per-actor totals — all four filters
    applied to all three (#162).

    The SQL and the reasoning for the filter composition live in
    `src/services/usage_filters.py`. Two things belong here:

    * **The owner scope is separate from the user filter.** `_scope_user_id`
      is the viewer's tenancy — None for an admin, their own id otherwise,
      exactly as `/admin/performance` scopes — and it is applied whatever the
      query string says. The `user=` filter is only an admin's choice of whose
      rows to look at; a regular user is never offered it and cannot introduce
      it by editing the URL.
    * **Every filter is clamped onto what the page itself offered**, and an
      unrecognised value becomes "no filter" rather than a 422. The selectors
      are links; a stale bookmark should render the page.

    Actors are resolved through `_usage_actor` in both the log and the totals,
    so a deleted credential is named identically in the two — and named at all,
    which resolving by join alone would not do (#77).
    """
    uid = _scope_user_id(user)
    window = normalize_window(window)
    options = await filter_options(session, window, uid)
    filters = resolve_filters(window, user_filter, key_filter, tool_filter, uid, options)

    logs = []
    for r in await recent_logs(session, filters):
        actor_name, actor_detail = _usage_actor(r)
        logs.append({
            "tool": r.tool,
            "duration_ms": r.duration_ms,
            "created_at": r.created_at.isoformat(),
            "actor_name": actor_name,
            "actor_detail": actor_detail,
            "outcome": _usage_outcome(r),
        })

    totals = []
    for r in await actor_totals(session, filters):
        actor_name, actor_detail = _usage_actor(r)
        totals.append({
            "actor_name": actor_name,
            "actor_detail": actor_detail,
            "requests": int(r.requests or 0),
            "last_seen": r.last_seen.isoformat() if r.last_seen else None,
        })

    chart_data = await chart_series(session, filters)

    return templates.TemplateResponse(request, "usage.html", _panel_context(request, user, {
        "active": "usage",
        "logs": logs,
        "chart_data": chart_data,
        "totals": totals,
        "window": filters.window,
        "window_label": WINDOW_LABELS[filters.window],
        "windows": [
            {
                "key": key,
                "label": key,
                "selected": key == filters.window,
                "query": filters.query_string(window=key),
            }
            for key in WINDOWS
        ],
        # Passed as three names rather than one `filter_options` dict:
        # Jinja resolves `filter_options.keys` to the dict's **method**,
        # because attribute lookup is tried before item lookup — so the
        # key selector silently iterated a builtin and 500'd the page.
        "filter_users": options["users"],
        "filter_keys": options["keys"],
        "filter_tools": options["tools"],
        "selected_user": filters.user_id,
        "selected_key": filters.key_id,
        "selected_tool": filters.tool,
        "filters_active": filters.any_active,
        "log_limit": LOG_LIMIT,
    }))


# --- Performance ----------------------------------------------------------


@router.get("/performance", response_class=HTMLResponse)
async def performance_page(
    request: Request,
    window: str | None = None,
    session: AsyncSession = Depends(get_session),
    user=Depends(require_user_panel),
):
    """Per-tool latency, phase timings, the window's slowest calls, and the
    most recent index/embed passes.

    Read-only aggregation over `usage_logs`; the SQL and the one shared
    executed/refused predicate live in `src/services/usage_stats.py`, which is
    where the reasoning for the predicate is written down.

    Scoped exactly like `/admin/usage`: an admin sees every row, a regular user
    only their own. `window` is clamped rather than validated — the selector is
    a set of links, and a hand-edited query string should render the page.
    """
    uid = _scope_user_id(user)
    window = normalize_window(window)

    tools = await tool_aggregates(session, window, uid)
    phases = await phase_breakdown(session, window, uid)
    slowest_rows = await slowest_requests(session, window, uid)
    # Deliberately **not** window-bounded: the passes are what the window's
    # latencies happened around, and a quiet 24 hours with no pass in it is
    # exactly when an operator needs to see the last one that did run.
    runs = await recent_indexer_runs(session, uid)

    slowest = []
    for r in slowest_rows:
        actor_name, actor_detail = _usage_actor(r)
        slowest.append({
            "created_at": r.created_at.isoformat(),
            "tool": r.tool,
            "duration_ms": r.duration_ms,
            "response_size": r.response_size,
            "actor_name": actor_name,
            "actor_detail": actor_detail,
        })

    return templates.TemplateResponse(
        request,
        "performance.html",
        _panel_context(request, user, {
            "active": "performance",
            "window": window,
            "window_label": WINDOW_LABELS[window],
            "windows": [
                {"key": key, "label": key, "selected": key == window}
                for key in WINDOWS
            ],
            "tools": tools,
            "phases": phases,
            "slowest": slowest,
            "runs": runs,
            "runs_limit": RECENT_RUNS_LIMIT,
            # A window with no executed rows at all is a real state (a fresh
            # deploy, a quiet weekend) and gets its own copy rather than a
            # table of zeroes that reads like a measurement.
            "has_data": any(t["executed"] or t["refusals"] for t in tools),
        }),
    )


# --- Health ---------------------------------------------------------------
#
# Three sections, three sources, and two of them are **admin-only**:
#
#   * The pass history is scoped exactly as `/admin/performance` scopes it — an
#     admin sees every pass, a regular user only their own, and deliberately
#     none of the ownerless single-user/global passes, which are not theirs to
#     read (`recent_indexer_runs`' docstring has the reasoning).
#   * The error list and the backup age are **operator concerns about the
#     server**, not about a tenant's data, and neither has any notion of an
#     owner to scope by. The ring buffer holds whatever the process logged,
#     including the OAuth internals of other tenants and any path, query or
#     identifier a failure happened to carry; the backup covers the whole
#     database. Showing either to a non-admin would leak across tenants with no
#     filter available to prevent it, so both are gated on `is_admin` and a
#     regular user sees a health page consisting of their own run history.
#
# The dashboard strip below follows the same split.


def _error_view(entries: list[dict]) -> list[dict]:
    """Ring-buffer entries as template rows: ISO timestamp, logger, message."""
    return [
        {
            "timestamp": entry["timestamp"].isoformat(),
            "logger": entry["logger"],
            "level": entry["level"],
            "message": entry["message"],
        }
        for entry in entries
    ]


def _error_window() -> dict:
    """The count and the window it was observed over — no records.

    The window is not decoration. A container restarted five minutes ago has an
    empty buffer, and "no errors" without "since 14:32" reads as "this server
    has been healthy" when it means "this process has not been running long
    enough to have failed yet".
    """
    since = observing_since()
    return {
        "error_count": error_count(),
        "errors_capped": error_buffer_full(),
        "error_buffer_size": ERROR_BUFFER_SIZE,
        "observing_since_iso": since.isoformat() if since else None,
        "observing_since_rel": _humanize_delta(since) if since else None,
    }


def _error_section() -> dict:
    """`_error_window()` plus the records themselves, for the page."""
    return {**_error_window(), "errors": _error_view(recent_errors())}


async def _backup_view(session: AsyncSession) -> dict | None:
    """`latest_backup` with a humanized age, or None when nothing is recorded."""
    backup = await latest_backup(session)
    if backup is None:
        return None
    return {**backup, "age_rel": _humanize_delta(backup["created_at"])}


@router.get("/health", response_class=HTMLResponse)
async def health_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user=Depends(require_user_panel),
):
    """Indexer pass history, recent application errors, and backup age.

    Read-only throughout. Every section has an explicit empty state and none of
    them is an error condition: a fresh install has no passes, no errors and no
    recorded backup, and that page has to render cleanly — it is the first
    thing an operator looks at when they are not sure the server is working.
    """
    uid = _scope_user_id(user)
    is_admin = _is_admin(user)

    runs = await recent_indexer_runs(session, uid, limit=HEALTH_RUNS_LIMIT)

    backup = await _backup_view(session) if is_admin else None
    errors = _error_section() if is_admin else None

    return templates.TemplateResponse(
        request,
        "health.html",
        _panel_context(request, user, {
            "active": "health",
            "runs": runs,
            "runs_limit": HEALTH_RUNS_LIMIT,
            # The two operator-only sections. False renders the note that says
            # so, rather than silently showing a page missing two thirds of
            # what its heading promises.
            "show_ops": is_admin,
            "backup": backup,
            "stale_after_days": STALE_AFTER_DAYS,
            **(errors or {}),
        }),
    )


async def _health_strip(session: AsyncSession, user) -> dict:
    """The dashboard's compact health summary.

    Reuses the health page's own reads rather than approximating them: the last
    pass comes from `indexer_runs` (the persisted record, not the in-process
    heartbeat the dashboard already shows above it — a restarted container has
    no heartbeat and still has a pass history), the backup age from
    `backups_log`, the error count from the ring buffer.

    Same admin split as the page. A non-admin gets the pass outcome and nothing
    else, and the strip renders without the two cells rather than with empty
    ones.
    """
    uid = _scope_user_id(user)
    is_admin = _is_admin(user)

    runs = await recent_indexer_runs(session, uid, limit=1)
    last_run = runs[0] if runs else None

    strip: dict[str, Any] = {
        "show_ops": is_admin,
        "last_run": last_run,
        "stale_after_days": STALE_AFTER_DAYS,
    }
    if is_admin:
        strip["backup"] = await _backup_view(session)
        # The count and the window, not the hundred records — the strip links
        # to the page, which is where they are read.
        strip.update(_error_window())
    return strip


async def _health_strip_or_degraded(session: AsyncSession, user) -> dict:
    """`_health_strip`, with a failure boundary around it.

    **The dashboard is the page an operator opens when something is wrong, and
    the strip is the newest thing on it.** Its reads are the only ones on this
    handler that touch `indexer_runs` and `backups_log`, so a fault confined to
    those two tables — a `NOT VALID` FK left by a hand repair, a permission
    revoked, a 021 that has not run on a database the container was pointed at —
    would take `/admin/` down for every user while every other query on the page
    succeeds. Losing three cells is the right trade against losing the page.

    The rollback is not optional: a failed statement poisons the session's
    transaction, and everything after it on this request would raise
    `InFailedSQLTransaction` instead of the original error. It runs first, so
    the render that follows has a usable session.

    The failure is logged at ERROR, which means the ring buffer catches it and
    the health page shows an operator *why* their strip is missing.
    """
    try:
        return await _health_strip(session, user)
    except Exception as exc:  # noqa: BLE001 - the strip must never take the page down
        try:
            await session.rollback()
        except Exception as rollback_exc:  # noqa: BLE001 - best effort; the render is what matters
            # Same event as the read failure below (D18's table names both):
            # they are two ways for the same feature to be unavailable, and an
            # operator filtering on one name wants to see either. The class in
            # `error_type` says which.
            security_events.emit(
                "panel_health_strip_failed",
                level=logging.ERROR,
                subject=security_events.subject_for(user_id=getattr(user, "id", None)),
                exc_info=True,
                error_type=type(rollback_exc).__name__,
            )
        security_events.emit(
            "panel_health_strip_failed",
            level=logging.ERROR,
            subject=security_events.subject_for(user_id=getattr(user, "id", None)),
            exc_info=True,
            error_type=type(exc).__name__,
        )
        return {"unavailable": True, "show_ops": _is_admin(user)}


# --- Search analytics -----------------------------------------------------


#: How the page labels each search tool. `find_related` is kept out of
#: `QUERY_TOOLS` and rendered separately because it has no query — its tables
#: are grouped by source note.
_SEARCH_TOOL_LABELS = {
    "keyword_search": "Keyword search",
    "semantic_search": "Semantic search",
    "find_related": "Related notes",
}

#: A `source_path` recorded as a digest rather than a path (over the telemetry
#: contract's 1024-byte bound). Labelled as such on the page: showing 64 hex
#: characters in a column of note paths, unmarked, would read as a note.
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


def _label_group_value(tool: str, value: str) -> dict:
    """Render one grouping value: the query text, or a source path/digest."""
    if tool != RELATED_TOOL:
        return {"text": value, "is_digest": False}
    return {"text": value, "is_digest": bool(_DIGEST_RE.match(value or ""))}


@router.get("/search-analytics", response_class=HTMLResponse)
async def search_analytics_page(
    request: Request,
    window: str | None = None,
    session: AsyncSession = Depends(get_session),
    user=Depends(require_user_panel),
):
    """What agents searched for, what came back empty, and what retrieval never
    surfaced — over a selectable window.

    Read-only aggregation over the search result telemetry in
    `usage_logs.params`; the SQL, the `(user_id, path)` identity rule and the
    reason the error filter is broad here and enumerated on
    `/admin/performance` all live in `src/services/search_analytics.py`.

    Scoped exactly like `/admin/performance`: an admin sees every row — grouped
    per owner, never merged, because a path names a different note in each
    user's vault — and a regular user only their own. `window` is clamped
    rather than validated, so a hand-edited query string renders the page.
    """
    uid = _scope_user_id(user)
    window = normalize_window(window)

    query_tables = []
    for tool in QUERY_TOOLS:
        top = await top_queries(session, window, uid, tool)
        zero = await zero_result_queries(session, window, uid, tool)
        query_tables.append({
            "tool": tool,
            "label": _SEARCH_TOOL_LABELS[tool],
            "top": [dict(r, label=_label_group_value(tool, r["value"])) for r in top],
            "zero": [dict(r, label=_label_group_value(tool, r["value"])) for r in zero],
        })

    related = {
        "tool": RELATED_TOOL,
        "label": _SEARCH_TOOL_LABELS[RELATED_TOOL],
        "top": [
            dict(r, label=_label_group_value(RELATED_TOOL, r["value"]))
            for r in await top_queries(session, window, uid, RELATED_TOOL)
        ],
        "zero": [
            dict(r, label=_label_group_value(RELATED_TOOL, r["value"]))
            for r in await zero_result_queries(session, window, uid, RELATED_TOOL)
        ],
    }

    retrievals = await top_logged_retrievals(session, window, uid)
    missing, missing_total = await never_retrieved(session, window, uid)

    tables = [*query_tables, related]
    # The owner column earns its place only when the page is actually showing
    # more than one owner's rows. In single-user mode every row is ownerless,
    # and a column of "no owner" is noise that pushes the paths off the side.
    owners = {
        row["user_id"]
        for table in tables
        for row in (*table["top"], *table["zero"])
    } | {row["user_id"] for row in (*retrievals, *missing)}
    has_data = any(table["top"] for table in tables) or bool(retrievals)

    return templates.TemplateResponse(
        request,
        "search_analytics.html",
        _panel_context(request, user, {
            "active": "search-analytics",
            "window": window,
            "window_label": WINDOW_LABELS[window],
            "windows": [
                {"key": key, "label": key, "selected": key == window}
                for key in WINDOWS
            ],
            "query_tables": query_tables,
            "related": related,
            "retrievals": retrievals,
            "never_retrieved": missing,
            "never_retrieved_total": missing_total,
            "table_limit": TABLE_LIMIT,
            "coverage_limit": COVERAGE_LIMIT,
            "logged_per_call": LOGGED_RESULTS_PER_CALL,
            "show_owner": len(owners) > 1,
            "has_data": has_data,
        }),
    )


# --- Vault browser --------------------------------------------------------


@router.get("/vault", response_class=HTMLResponse)
async def vault_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user=Depends(require_user_panel),
):
    folder = request.query_params.get("folder", "")
    selected_note = request.query_params.get("note")

    from src.services.vault import _vault_root, vault_unassigned_error

    # Resolve the per-user vault root. In single-user mode `user.id` is None
    # and `_vault_root(None)` returns `settings.vault_path` — the legacy
    # behavior.
    #
    # In multi-user mode we use the root the warm *itself* read, not a re-read
    # of the shared `_user_vault_cache`. The indexer's bulk warm writes to that
    # dict too and is add-only, so a bulk SELECT taken before the admin cleared
    # `vault_path` can land between the warm and the lookup and hand this page
    # the vault the user no longer holds — the same race `current_vault_root`
    # closes for tool calls (issue #66). A None here is the refusal, rendered
    # as the friendly empty state rather than a 500.
    vault = None
    vault_error = None
    if user.id is None:
        try:
            vault = _vault_root(None)
        except RuntimeError as e:
            vault_error = str(e)
    else:
        vault = await warm_user_vault_cache(session, user.id)
        if vault is None:
            vault_error = vault_unassigned_error(user.id)

    if vault_error is not None:
        return templates.TemplateResponse(request, "vault.html", _panel_context(request, user, {
            "active": "vault",
            "current_folder": "",
            "breadcrumbs": [],
            "folders": [],
            "notes": [],
            "selected_note": None,
            "note_content": None,
            "note_title": None,
            "note_tags": [],
            "vault_error": vault_error,
        }))

    if folder:
        try:
            resolved = (vault / folder).resolve()
            if not resolved.is_relative_to(vault.resolve()):
                folder = ""
        except (ValueError, OSError):
            folder = ""
    base = vault / folder if folder else vault

    # Breadcrumbs
    breadcrumbs = []
    if folder:
        parts = Path(folder).parts
        for i, part in enumerate(parts):
            breadcrumbs.append({
                "name": part,
                "path": str(Path(*parts[: i + 1])),
            })

    # List folders and files
    folders = []
    notes = []
    if base.is_dir():
        for item in sorted(base.iterdir()):
            if item.name.startswith("."):
                continue
            rel = str(item.relative_to(vault))
            if item.is_dir():
                folders.append({"name": item.name, "path": rel})
            elif item.suffix == ".md":
                notes.append({"name": item.stem, "path": rel})

    # Selected note content
    note_content = None
    note_title = None
    note_tags = []
    if selected_note:
        from src.services.vault import read_file
        try:
            data = read_file(selected_note, user_id=user.id)
            note_content = data["content"]
            note_title = data["title"]
            note_tags = data["tags"]
        except Exception:
            note_content = "Error reading note"

    return templates.TemplateResponse(request, "vault.html", _panel_context(request, user, {
        "active": "vault",
        "current_folder": folder,
        "breadcrumbs": breadcrumbs,
        "folders": folders,
        "notes": notes,
        "selected_note": selected_note,
        "note_content": note_content,
        "note_title": note_title,
        "note_tags": note_tags,
    }))


def _mask_openai_key(key: str | None) -> str:
    if not key:
        return "(not set)"
    if len(key) < 8:
        return "***"
    return f"***...{key[-4:]}"


# --- Settings (admin only) ------------------------------------------------


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user=Depends(require_admin_panel),
):
    notes_count = (await session.execute(select(func.count(NoteMetadata.id)))).scalar() or 0
    embeddings_count = (await session.execute(select(func.count(NoteEmbedding.id)))).scalar() or 0
    notes_with_emb = (await session.execute(
        select(func.count(func.distinct(NoteEmbedding.note_id)))
    )).scalar() or 0

    # Test DB connection
    db_ok = True
    try:
        await session.execute(text("SELECT 1"))
    except Exception:
        db_ok = False

    provider = settings.embedding_provider
    provider_card = {
        "name": provider,
        "dimensions": settings.embedding_dimensions,
    }
    if provider == "openai":
        provider_card["model"] = settings.openai_embedding_model
        provider_card["base_url"] = settings.openai_base_url
        provider_card["masked_key"] = _mask_openai_key(settings.openai_api_key)
    else:
        provider_card["model"] = settings.embedding_model
        provider_card["ollama_url"] = settings.ollama_url

    # Test the active provider's reachability
    provider_ok = False
    try:
        if provider == "ollama":
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(f"{settings.ollama_url}/api/tags")
                provider_ok = r.status_code == 200
        else:
            provider_ok = bool((settings.openai_api_key or "").strip())
    except Exception:
        pass

    return templates.TemplateResponse(request, "settings.html", _panel_context(request, user, {
        "active": "settings",
        "stats": {
            "notes_indexed": notes_count,
            "embeddings": embeddings_count,
            "notes_with_embeddings": notes_with_emb,
        },
        "index_interval": settings.index_interval_seconds,
        "db_ok": db_ok,
        "provider_ok": provider_ok,
        "provider": provider_card,
        "vault_path": settings.vault_path,
    }))


# Keep strong references to background tasks to prevent GC
_background_tasks: set = set()


def _spawn(coro):
    import asyncio
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


# Indexer pause flag, also surfaced via the reset progress endpoint and read
# by `src.services.indexer._is_paused` (which `getattr`s this module
# attribute). It stays a plain bool because that is the published contract;
# what changed is that nothing sets it directly any more.
indexer_paused: bool = False

# How many danger-zone actions are currently holding the pause. A bare
# `indexer_paused = False` in each handler's `finally` was wrong under
# overlap: two resets that interleave (the second starts while the first is
# still waiting on `index_pass_lock`) had the first one to finish clear the
# flag for both, so the indexer resumed underneath the other's destructive
# statements and `/settings/reset-embeddings/progress` reported "not paused"
# about a pause that was still in force. The depth is mutated only between
# `await`s on the single event loop, so a lock would guard nothing.
_pause_depth: int = 0


@contextmanager
def _pause_indexer():
    """Hold the indexer pause for the duration of the block.

    Nesting-safe: the flag goes true on the first holder and false only when
    the last one leaves.
    """
    global _pause_depth, indexer_paused
    _pause_depth += 1
    indexer_paused = True
    try:
        yield
    finally:
        _pause_depth -= 1
        indexer_paused = _pause_depth > 0


@asynccontextmanager
async def _pass_lock_without_a_connection(session: AsyncSession):
    """Hold the indexer pass lock for the destructive statements, without
    pinning a pool connection while waiting for it.

    The pre-warm added to the periodic tick runs *inside* `index_pass_lock`,
    which means a reset that ran concurrently could drop the HNSW index out
    from under a probe. So the destructive panel actions take the lock too.

    Ending this request's own transaction first is not tidiness. `get_session`
    has already checked out one of five pooled connections; blocking on the
    lock while holding it means a handful of concurrent resets exhaust the
    pool, and the lock *holder* — an index pass that needs a connection of its
    own to finish — then deadlocks against the waiters. Close first, wait, and
    only take a connection once the lock is ours.

    The wait is bounded by the current index pass, not by the pre-warm's 15 s
    timeout; the danger-zone page already warns the action can take a while.
    """
    from src.services.indexer import index_pass_lock

    await session.close()
    async with index_pass_lock:
        yield


@router.post("/settings/reindex")
async def trigger_reindex(
    request: Request,
    user=Depends(require_admin_panel),
):
    _spawn(_reindex_background())
    if "application/json" in request.headers.get("accept", ""):
        return JSONResponse({"status": "started"})
    return RedirectResponse("/admin/settings", status_code=303)


@router.get("/settings/reembed", response_class=HTMLResponse)
async def reembed_confirm_page(
    request: Request,
    user=Depends(require_admin_panel),
):
    """Generate a one-time signed token and render a confirmation page."""
    token = _reembed_serializer().dumps(secrets.token_hex(16))
    return templates.TemplateResponse(request, "reembed_confirm.html", _panel_context(request, user, {
        "active": "settings",
        "token": token,
    }))


@router.post("/settings/reembed")
async def trigger_reembed(
    token: str = Form(...),
    session: AsyncSession = Depends(get_session),
    user=Depends(require_admin_panel),
):
    """Clear all embeddings and re-embed from scratch. Requires a valid signed token."""
    try:
        _reembed_serializer().loads(token, max_age=60)
    except (BadSignature, SignatureExpired):
        raise HTTPException(status_code=400, detail="Invalid or expired confirmation token")

    from src.models.db import NoteEmbedding, NoteMetadata
    from sqlalchemy import delete, update

    with _pause_indexer():
        async with _pass_lock_without_a_connection(session):
            async with async_session() as fresh:
                await fresh.execute(delete(NoteEmbedding))
                # Deleting the vectors is not enough: `embed_vault` selects
                # notes whose `embedded_content_hash` differs from
                # `content_hash`, so leaving the hashes stamped meant the
                # reindex spawned below re-embedded *nothing* and the vault
                # silently stayed unsearchable. Same transaction as the DELETE,
                # so the two can never disagree.
                await fresh.execute(
                    update(NoteMetadata).values(embedded_content_hash=None)
                )
                await fresh.commit()

    _spawn(_reindex_background())
    return RedirectResponse("/admin/settings", status_code=303)


@router.post("/settings/reset-embeddings")
async def reset_embeddings(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user=Depends(require_admin_panel),
):
    """Recreate `note_embeddings.embedding` at the configured dim and null
    every `embedded_content_hash` so the indexer re-embeds the vault on
    the next pass. Pauses the indexer during the SQL.

    Returns a JSON status object the dashboard can poll.
    """
    from sqlalchemy import delete
    from src.models.db import NoteEmbedding, NoteMetadata

    dim = int(settings.embedding_dimensions)
    # pgvector caps HNSW-indexable vectors at 2000 dims; above that, CREATE
    # INDEX ... USING hnsw hard-errors. Skip the index so the reset still
    # completes; semantic_search falls back to a sequential scan. See issue #6.
    hnsw = dim <= 2000

    with _pause_indexer():
        async with _pass_lock_without_a_connection(session):
            async with async_session() as fresh:
                await fresh.execute(text("SET LOCAL statement_timeout = '5min'"))
                await fresh.execute(
                    text("DROP INDEX IF EXISTS ix_note_embeddings_embedding_hnsw")
                )
                await fresh.execute(delete(NoteEmbedding))
                await fresh.execute(
                    text(
                        f"ALTER TABLE note_embeddings "
                        f"ALTER COLUMN embedding TYPE vector({dim})"
                    )
                )
                await fresh.execute(
                    text("UPDATE notes_metadata SET embedded_content_hash = NULL")
                )
                if hnsw:
                    await fresh.execute(
                        text(
                            "CREATE INDEX ix_note_embeddings_embedding_hnsw "
                            "ON note_embeddings USING hnsw (embedding vector_cosine_ops) "
                            "WITH (m = 16, ef_construction = 64)"
                        )
                    )
                else:
                    logger.warning(
                        "Skipping HNSW index: embedding_dimensions=%d exceeds "
                        "pgvector's 2000-dim HNSW limit; semantic_search will "
                        "use a sequential scan.",
                        dim,
                    )
                await fresh.commit()
        # The pre-warm caches whether an HNSW index exists; this route is the
        # one place that changes the answer.
        invalidate_hnsw_index_cache()

    _spawn(_reindex_background())

    if "application/json" in request.headers.get("accept", ""):
        return JSONResponse({"status": "reset", "dimensions": dim, "hnsw": hnsw})
    return RedirectResponse("/admin/settings", status_code=303)


@router.get("/settings/reset-embeddings/progress")
async def reset_progress(
    session: AsyncSession = Depends(get_session),
    user=Depends(require_admin_panel),
):
    """Return re-embedding progress (notes still pending) for dashboard polling."""
    total = (await session.execute(select(func.count(NoteMetadata.id)))).scalar() or 0
    embedded = (await session.execute(
        text(
            "SELECT count(*) FROM notes_metadata "
            "WHERE embedded_content_hash IS NOT NULL "
            "AND embedded_content_hash = content_hash"
        )
    )).scalar() or 0
    pending = max(0, total - embedded)
    return JSONResponse({
        "paused": indexer_paused,
        "total": int(total),
        "embedded": int(embedded),
        "pending": int(pending),
    })


async def _reindex_background():
    # Panel-triggered on-demand reindex. Mirrors `run_indexer_loop` so the
    # multi-user-mode case fans out to every active user; in single-user
    # mode it stays a single legacy pass with user_id=None.
    #
    # Acquire `index_pass_lock` for the whole pass: without it this task can
    # run index_vault/embed_vault concurrently with the periodic loop over the
    # same scope, racing on move-detection, deleted-path removal, and per-note
    # embedding delete+insert.
    #
    # Each pass records an `indexer_runs` row under the `manual` trigger
    # (#160), so the history distinguishes "the operator pressed Reindex Now"
    # from a five-minute tick — which is the first thing anyone asks when a
    # pass in the history took ten times as long as its neighbours.
    from src.services.indexer import (
        index_vault, embed_vault, _active_user_ids, index_pass_lock,
        record_indexer_run,
    )
    async with index_pass_lock:
        if settings.multi_user_mode:
            for uid in await _active_user_ids():
                async with record_indexer_run("manual", uid) as stats:
                    try:
                        stats.record_index(await index_vault(user_id=uid))
                    except Exception as e:
                        stats.record_error("index", e)
                        security_events.emit(
                            "panel_ondemand_index_failed",
                            level=logging.ERROR,
                            subject=security_events.subject_for(user_id=uid),
                            user_id=uid,
                            error_type=type(e).__name__,
                        )
                    try:
                        stats.record_embedded(await embed_vault(user_id=uid))
                    except Exception as e:
                        stats.record_error("embed", e)
                        security_events.emit(
                            "panel_ondemand_embed_failed",
                            level=logging.ERROR,
                            subject=security_events.subject_for(user_id=uid),
                            user_id=uid,
                            error_type=type(e).__name__,
                        )
        else:
            async with record_indexer_run("manual", None) as stats:
                stats.record_index(await index_vault())
                stats.record_embedded(await embed_vault())
