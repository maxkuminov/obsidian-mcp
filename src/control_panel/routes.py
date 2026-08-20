import logging
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.session import _SingleUserSentinel, get_current_user
from src.config import settings
from src.csrf import generate_csrf_token, verify_csrf
from src.database import async_session, get_session
from src.mcp_server.auth import hash_key
from src.models.db import (
    APIKey,
    NoteEmbedding,
    NoteLink,
    NoteMetadata,
    OAuthClient,
    OAuthToken,
    User,
    UsageLog,
)
from src.services.indexer import invalidate_hnsw_index_cache
from src.services.vault import warm_user_vault_cache

logger = logging.getLogger(__name__)


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
    if user is None or (isinstance(user, User) and not user.is_active):
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
        raise HTTPException(
            status_code=status.HTTP_302_FOUND,
            headers={"Location": f"/admin/auth/login?next={target}"},
        )
    return user


async def require_admin_panel(
    user: User | _SingleUserSentinel = Depends(require_user_panel),
):
    """Gate dangerous handlers (settings, user management) on `is_admin`.

    In single-user mode the sentinel reports `is_admin=True` so these
    handlers work exactly as today.
    """
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin required")
    return user


# Attach `require_user_panel` to every route in this router. Individual
# handlers can additionally depend on `require_admin_panel` for the danger
# zone; FastAPI runs both dependencies but the redirect from the user one
# fires first if there's no session.
router.dependencies.append(Depends(require_user_panel))
router.dependencies.append(Depends(verify_csrf))


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
    """
    ctx: dict[str, Any] = {
        "is_admin": bool(user.is_admin),
        "username": user.username,
        "multi_user_mode": bool(settings.multi_user_mode),
        "csrf_token": generate_csrf_token(request),
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
    keys = []
    for k, owner_username, owner_is_active in result.all():
        # A key whose `user_id` has no row (owner_is_active is None) is dead
        # too — the middleware's `is True` test fails for it just the same,
        # so it must not read as live here.
        owner_ok = k.user_id is None or owner_is_active is True
        keys.append({
            "id": k.id,
            "name": k.name,
            "key_prefix": k.key_prefix,
            "permission": k.permission,
            "is_active": k.is_active,
            "owner_is_active": owner_ok,
            "effective_active": bool(k.is_active) and owner_ok,
            "owner_username": owner_username,
            "created_at": k.created_at.isoformat(),
            "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None,
            "user_id": k.user_id,
        })
    try:
        new_key = request.session.pop("flash_new_key", None)
    except (AssertionError, AttributeError):
        new_key = None
    return templates.TemplateResponse(request, "keys.html", _panel_context(request, user, {
        "active": "keys", "keys": keys, "new_key": new_key,
    }))


@router.post("/keys/create")
async def create_key_form(
    request: Request,
    name: str = Form(...),
    permission: str = Form("read"),
    session: AsyncSession = Depends(get_session),
    user=Depends(require_user_panel),
):
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


def _assert_key_owner(key: APIKey | None, user: User | _SingleUserSentinel) -> APIKey:
    if key is None:
        raise HTTPException(404, "Key not found")
    # Admin can mutate any key. Regular user can only mutate their own.
    if not _is_admin(user) and key.user_id != user.id:
        raise HTTPException(403, "Not your key")
    return key


@router.post("/keys/{key_id}/revoke")
async def revoke_key_form(
    key_id: int,
    session: AsyncSession = Depends(get_session),
    user=Depends(require_user_panel),
):
    result = await session.execute(select(APIKey).where(APIKey.id == key_id))
    api_key = result.scalar_one_or_none()
    _assert_key_owner(api_key, user)
    api_key.is_active = False
    await session.commit()
    return RedirectResponse("/admin/keys", status_code=303)


@router.post("/keys/{key_id}/delete")
async def delete_key_form(
    key_id: int,
    session: AsyncSession = Depends(get_session),
    user=Depends(require_user_panel),
):
    from sqlalchemy import update as sa_update
    result = await session.execute(
        select(APIKey).where(APIKey.id == key_id, APIKey.is_active == False)
    )
    api_key = result.scalar_one_or_none()
    _assert_key_owner(api_key, user)
    await session.execute(
        sa_update(UsageLog).where(UsageLog.key_id == key_id).values(key_id=None)
    )
    await session.delete(api_key)
    await session.commit()
    return RedirectResponse("/admin/keys", status_code=303)


# --- OAuth ----------------------------------------------------------------


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
    clients = []
    for c in result.scalars().all():
        # Tokens inherit their client's scope, but we also scope tokens by
        # user_id directly for defense in depth: an unbound/legacy client
        # could have tokens stamped with a user_id that diverges from the
        # client's. Filter both ways.
        token_q = (
            select(OAuthToken)
            .where(
                OAuthToken.client_id == c.client_id,
                OAuthToken.revoked == False,
                OAuthToken.expires_at > now,
            )
            .order_by(OAuthToken.created_at.desc())
        )
        if uid is not None:
            token_q = token_q.where(OAuthToken.user_id == uid)
        token_result = await session.execute(token_q)
        tokens = []
        for t in token_result.scalars().all():
            tokens.append({
                "id": t.id,
                "token_type": t.token_type,
                "scope": t.scope,
                # t.scope is a space-separated set (e.g. "offline_access
                # readwrite"), not always exactly "read" or "readwrite" -
                # the template needs membership, not string equality, or
                # a readwrite token showing an offline_access marker
                # renders as if it were read-only.
                "has_write": "readwrite" in t.scope.split(),
                "revoked": t.revoked,
                "expired": False,
                "expires_at": t.expires_at.isoformat(),
                "created_at": t.created_at.isoformat(),
            })
        clients.append({
            "client_id": c.client_id,
            "client_name": c.client_name,
            "created_at": c.created_at.isoformat(),
            "tokens": tokens,
        })
    return templates.TemplateResponse(request, "oauth.html", _panel_context(request, user, {
        "active": "oauth", "clients": clients,
    }))


async def _assert_oauth_client_owner(
    session: AsyncSession, client_id: str, user: User | _SingleUserSentinel
) -> OAuthClient:
    result = await session.execute(select(OAuthClient).where(OAuthClient.client_id == client_id))
    client = result.scalar_one_or_none()
    if client is None:
        raise HTTPException(404, "Client not found")
    if not _is_admin(user) and client.user_id != user.id:
        raise HTTPException(403, "Not your client")
    return client


async def _assert_oauth_token_owner(
    session: AsyncSession, token_id: int, user: User | _SingleUserSentinel
) -> OAuthToken:
    result = await session.execute(select(OAuthToken).where(OAuthToken.id == token_id))
    token = result.scalar_one_or_none()
    if token is None:
        raise HTTPException(404, "Token not found")
    if not _is_admin(user) and token.user_id != user.id:
        raise HTTPException(403, "Not your token")
    return token


@router.post("/oauth/{client_id}/delete")
async def delete_oauth_client(
    client_id: str,
    session: AsyncSession = Depends(get_session),
    user=Depends(require_user_panel),
):
    client = await _assert_oauth_client_owner(session, client_id, user)
    await session.delete(client)
    await session.commit()
    return RedirectResponse("/admin/oauth", status_code=303)


@router.post("/oauth/token/{token_id}/revoke")
async def revoke_oauth_token(
    token_id: int,
    session: AsyncSession = Depends(get_session),
    user=Depends(require_user_panel),
):
    token = await _assert_oauth_token_owner(session, token_id, user)
    token.revoked = True
    await session.commit()
    return RedirectResponse("/admin/oauth", status_code=303)


@router.post("/oauth/token/{token_id}/scope")
async def update_oauth_token_scope(
    token_id: int,
    scope: str = Form(...),
    session: AsyncSession = Depends(get_session),
    user=Depends(require_user_panel),
):
    if scope not in ("read", "readwrite"):
        return RedirectResponse("/admin/oauth", status_code=303)
    token = await _assert_oauth_token_owner(session, token_id, user)
    if not token.revoked:
        # Preserve the offline_access marker (informational only - see
        # DEFAULT_CLIENT_SCOPE comment in src/oauth/routes.py) instead of
        # dropping it whenever an admin flips read/readwrite here.
        has_offline = "offline_access" in token.scope.split()
        token.scope = f"{scope} offline_access" if has_offline else scope
        await session.commit()
    return RedirectResponse("/admin/oauth", status_code=303)


# --- Usage ----------------------------------------------------------------


@router.get("/usage", response_class=HTMLResponse)
async def usage_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user=Depends(require_user_panel),
):
    uid = _scope_user_id(user)
    # Recent logs with attributed actor (API key name+prefix or OAuth client name)
    if uid is None:
        result = await session.execute(
            text("""
                SELECT
                    ul.id,
                    ul.tool,
                    ul.duration_ms,
                    ul.created_at,
                    ak.name        AS api_key_name,
                    ak.key_prefix  AS api_key_prefix,
                    oc.client_name AS oauth_client_name
                FROM usage_logs ul
                LEFT JOIN api_keys ak ON ul.key_id = ak.id
                LEFT JOIN oauth_tokens ot ON ul.oauth_token_id = ot.id
                LEFT JOIN oauth_clients oc ON ot.client_id = oc.client_id
                ORDER BY ul.created_at DESC
                LIMIT 100
            """)
        )
    else:
        result = await session.execute(
            text("""
                SELECT
                    ul.id,
                    ul.tool,
                    ul.duration_ms,
                    ul.created_at,
                    ak.name        AS api_key_name,
                    ak.key_prefix  AS api_key_prefix,
                    oc.client_name AS oauth_client_name
                FROM usage_logs ul
                LEFT JOIN api_keys ak ON ul.key_id = ak.id
                LEFT JOIN oauth_tokens ot ON ul.oauth_token_id = ot.id
                LEFT JOIN oauth_clients oc ON ot.client_id = oc.client_id
                WHERE ul.user_id = :uid
                ORDER BY ul.created_at DESC
                LIMIT 100
            """),
            {"uid": uid},
        )
    logs = []
    for r in result.fetchall():
        if r.api_key_name:
            actor_name = r.api_key_name
            actor_detail = r.api_key_prefix
        elif r.oauth_client_name:
            actor_name = r.oauth_client_name
            actor_detail = "OAuth"
        else:
            actor_name = None
            actor_detail = None
        logs.append({
            "tool": r.tool,
            "duration_ms": r.duration_ms,
            "created_at": r.created_at.isoformat(),
            "actor_name": actor_name,
            "actor_detail": actor_detail,
        })

    # Chart data: requests per day for last 7 days
    if uid is None:
        chart_result = await session.execute(
            text("""
                SELECT date_trunc('day', created_at)::date AS day, count(*) AS cnt
                FROM usage_logs
                WHERE created_at >= now() - interval '7 days'
                GROUP BY day ORDER BY day
            """)
        )
    else:
        chart_result = await session.execute(
            text("""
                SELECT date_trunc('day', created_at)::date AS day, count(*) AS cnt
                FROM usage_logs
                WHERE created_at >= now() - interval '7 days' AND user_id = :uid
                GROUP BY day ORDER BY day
            """),
            {"uid": uid},
        )
    chart_rows = chart_result.fetchall()
    chart_data = {
        "labels": [r.day.strftime("%m/%d") for r in chart_rows],
        "values": [r.cnt for r in chart_rows],
    }

    return templates.TemplateResponse(request, "usage.html", _panel_context(request, user, {
        "active": "usage", "logs": logs, "chart_data": chart_data,
    }))


# --- Vault browser --------------------------------------------------------


@router.get("/vault", response_class=HTMLResponse)
async def vault_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user=Depends(require_user_panel),
):
    folder = request.query_params.get("folder", "")
    selected_note = request.query_params.get("note")

    # Resolve the per-user vault root. In single-user mode `user.id` is None
    # and `_vault_root(None)` returns `settings.vault_path` — the legacy
    # behavior. In multi-user mode, this requires the cache to be warmed
    # for the user; login already does that, but we re-warm here so a
    # session that survived a process restart (no warm yet) still works.
    if user.id is not None:
        await warm_user_vault_cache(session, user.id)

    from src.services.vault import _vault_root

    # `_vault_root` raises RuntimeError if the user has no `vault_path`
    # assigned in multi-user mode. Surface that as a friendly empty state
    # rather than a 500.
    try:
        vault = _vault_root(user.id)
    except RuntimeError as e:
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
            "vault_error": str(e),
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


# Indexer pause flag, also surfaced via the reset progress endpoint.
indexer_paused: bool = False


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
    global indexer_paused
    try:
        _reembed_serializer().loads(token, max_age=60)
    except (BadSignature, SignatureExpired):
        raise HTTPException(status_code=400, detail="Invalid or expired confirmation token")

    from src.models.db import NoteEmbedding, NoteMetadata
    from sqlalchemy import delete, update

    indexer_paused = True
    try:
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
    finally:
        indexer_paused = False

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
    global indexer_paused
    from sqlalchemy import delete
    from src.models.db import NoteEmbedding, NoteMetadata

    dim = int(settings.embedding_dimensions)
    # pgvector caps HNSW-indexable vectors at 2000 dims; above that, CREATE
    # INDEX ... USING hnsw hard-errors. Skip the index so the reset still
    # completes; semantic_search falls back to a sequential scan. See issue #6.
    hnsw = dim <= 2000

    indexer_paused = True
    try:
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
    finally:
        indexer_paused = False

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
    from src.services.indexer import (
        index_vault, embed_vault, _active_user_ids, index_pass_lock,
    )
    async with index_pass_lock:
        if settings.multi_user_mode:
            for uid in await _active_user_ids():
                try:
                    await index_vault(user_id=uid)
                except Exception as e:
                    logger.error(f"On-demand index failed (user_id={uid}): {e}")
                try:
                    await embed_vault(user_id=uid)
                except Exception as e:
                    logger.error(f"On-demand embedding failed (user_id={uid}): {e}")
        else:
            await index_vault()
            await embed_vault()
