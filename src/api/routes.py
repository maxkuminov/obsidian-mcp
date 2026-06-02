import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.session import _SingleUserSentinel
from src.control_panel.routes import require_admin_panel, require_user_panel
from src.csrf import verify_csrf
from src.database import get_session
from src.limiter import limiter
from src.mcp_server.auth import hash_key
from src.models.db import APIKey, User, UsageLog

router = APIRouter(prefix="/api", tags=["api"])
router.dependencies.append(Depends(require_user_panel))
router.dependencies.append(Depends(verify_csrf))


class CreateKeyRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255, pattern=r"^[\w\-. ]+$")
    permission: str = Field("read", pattern="^(read|readwrite)$")


class CreateKeyResponse(BaseModel):
    id: int
    name: str
    key: str  # Full key shown once
    key_prefix: str
    permission: str


class KeyInfo(BaseModel):
    id: int
    name: str
    key_prefix: str
    permission: str
    is_active: bool
    created_at: str
    last_used_at: str | None


@router.post("/keys", response_model=CreateKeyResponse)
@limiter.limit("5/minute")
async def create_key(
    request: Request,
    req: CreateKeyRequest,
    session: AsyncSession = Depends(get_session),
    user: User | _SingleUserSentinel = Depends(require_user_panel),
):
    if req.permission not in ("read", "readwrite"):
        raise HTTPException(400, "Permission must be 'read' or 'readwrite'")

    raw_key = f"omcp_{secrets.token_hex(32)}"
    key_prefix = raw_key[:12]

    api_key = APIKey(
        name=req.name,
        key_hash=hash_key(raw_key),
        key_prefix=key_prefix,
        permission=req.permission,
        user_id=user.id,
    )
    session.add(api_key)
    await session.commit()
    await session.refresh(api_key)

    return CreateKeyResponse(
        id=api_key.id,
        name=api_key.name,
        key=raw_key,
        key_prefix=key_prefix,
        permission=api_key.permission,
    )


@router.get("/keys", response_model=list[KeyInfo])
async def list_keys(
    session: AsyncSession = Depends(get_session),
    user: User | _SingleUserSentinel = Depends(require_user_panel),
):
    q = select(APIKey).order_by(APIKey.created_at.desc())
    if not user.is_admin:
        q = q.where(APIKey.user_id == user.id)
    result = await session.execute(q)
    keys = result.scalars().all()
    return [
        KeyInfo(
            id=k.id,
            name=k.name,
            key_prefix=k.key_prefix,
            permission=k.permission,
            is_active=k.is_active,
            created_at=k.created_at.isoformat(),
            last_used_at=k.last_used_at.isoformat() if k.last_used_at else None,
        )
        for k in keys
    ]


@router.delete("/keys/{key_id}")
async def revoke_key(
    key_id: int,
    session: AsyncSession = Depends(get_session),
    user: User | _SingleUserSentinel = Depends(require_user_panel),
):
    result = await session.execute(select(APIKey).where(APIKey.id == key_id))
    api_key = result.scalar_one_or_none()
    if not api_key:
        raise HTTPException(404, "Key not found")
    if not user.is_admin and api_key.user_id != user.id:
        raise HTTPException(403, "Not your key")
    api_key.is_active = False
    await session.commit()
    return {"status": "revoked"}


@router.get("/usage")
async def get_usage(
    limit: int = 100,
    key_id: int | None = None,
    session: AsyncSession = Depends(get_session),
    user: User | _SingleUserSentinel = Depends(require_user_panel),
):
    limit = max(1, min(limit, 500))
    query = select(UsageLog).order_by(UsageLog.created_at.desc()).limit(limit)
    if key_id:
        query = query.where(UsageLog.key_id == key_id)
    if not user.is_admin:
        query = query.where(UsageLog.user_id == user.id)
    result = await session.execute(query)
    logs = result.scalars().all()
    return [
        {
            "id": l.id,
            "key_id": l.key_id,
            "tool": l.tool,
            "params": l.params,
            "duration_ms": l.duration_ms,
            "response_size": l.response_size,
            "created_at": l.created_at.isoformat(),
        }
        for l in logs
    ]


@router.get("/stats", dependencies=[Depends(require_admin_panel)])
async def get_stats(session: AsyncSession = Depends(get_session)):
    from src.models.db import NoteMetadata, NoteEmbedding
    notes_count = (await session.execute(select(func.count(NoteMetadata.id)))).scalar()
    keys_count = (await session.execute(select(func.count(APIKey.id)).where(APIKey.is_active == True))).scalar()
    embeddings_count = (await session.execute(select(func.count(NoteEmbedding.id)))).scalar()
    return {
        "notes_indexed": notes_count,
        "active_keys": keys_count,
        "embeddings": embeddings_count,
    }
