import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.session import _SingleUserSentinel
from src.config import settings
from src.control_panel.routes import (
    _assert_key_owner,
    _log_panel_forbidden,
    require_admin_panel,
    require_user_panel,
)
from src.csrf import verify_csrf
from src.database import get_session
from src.limiter import limiter
from src.mcp_server.auth import hash_key
from src.models.db import (
    DAILY_REQUEST_LIMIT_MAX,
    DAILY_REQUEST_LIMIT_MIN,
    APIKey,
    User,
    UsageLog,
)
from src.services.quotas import apply_daily_request_limit

router = APIRouter(prefix="/api", tags=["api"])
router.dependencies.append(Depends(require_user_panel))
router.dependencies.append(Depends(verify_csrf))


class CreateKeyRequest(BaseModel):
    """The JSON twin of the panel's create form.

    **`extra="forbid"` is a security control, not tidiness.** Pydantic's
    default is to ignore unknown fields, so before #162 a client that sent
    `{"daily_request_limit": 100}` got a 201 and an *unlimited* key: the
    request looked accepted, the operator believed a ceiling existed, and
    nothing enforced one. That is the "silently never enforces" failure this
    whole change exists to prevent, arriving through the door nobody was
    watching. Forbidding extras makes every future control that this model does
    not implement a loud 422 instead of a quiet no-op — including the next one
    somebody adds to the form and forgets here.

    **Omitted and explicit-null are different requests** (#194). An omitted
    `daily_request_limit` means "whatever the server considers sensible" and
    the handler applies `DEFAULT_DAILY_REQUEST_LIMIT`; an explicit
    `{"daily_request_limit": null}` still means *unlimited*, as it always has,
    and is the only way to ask for one. The two are told apart by
    `model_fields_set` — whether the field was present in the request — never
    by the value's truthiness, because a `None` default cannot distinguish
    them and a sentinel default would leak into the schema. Any explicit value
    wins over the default outright.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=255, pattern=r"^[\w\-. ]+$")
    permission: str = Field("read", pattern="^(read|readwrite)$")
    # Null is unlimited, matching the column and the form's empty box; absent
    # is "apply the configured default", resolved in the handler (see
    # `_created_key_limit`). The bounds are the same ones
    # `ck_api_keys_daily_request_limit` enforces, restated here so a bad value
    # is a 422 naming the field rather than a 500 out of the database.
    daily_request_limit: int | None = Field(
        None, ge=DAILY_REQUEST_LIMIT_MIN, le=DAILY_REQUEST_LIMIT_MAX
    )


class SetKeyLimitRequest(BaseModel):
    """Set, change, or clear one key's daily limit. Explicit `null` clears it.

    **The field is required-but-nullable, and the distinction is the point.**
    With a `None` *default* this model made `PUT {}` a success that silently
    cleared an existing ceiling — the same class of failure as the ignored
    field above, in the opposite direction: a request that named nothing
    removed the operator's quota and reported 200. Omission is not a way to
    say "unlimited"; `{"daily_request_limit": null}` is, and it is the only
    way, because clearing a quota should have to be typed.

    `Field(...)` on an `int | None` means required and still nullable, so
    omission is a 422 while the documented clearing operation is unchanged.
    """

    model_config = ConfigDict(extra="forbid")

    daily_request_limit: int | None = Field(
        ..., ge=DAILY_REQUEST_LIMIT_MIN, le=DAILY_REQUEST_LIMIT_MAX
    )


class CreateKeyResponse(BaseModel):
    id: int
    name: str
    key: str  # Full key shown once
    key_prefix: str
    permission: str
    # Echoed back so a client can see what it actually got. A create that
    # returned only the fields it was always going to return is how the
    # silently-ignored limit stayed invisible.
    daily_request_limit: int | None


class KeyLimitResponse(BaseModel):
    id: int
    daily_request_limit: int | None
    #: Whether this write reset the day's counter (the NULL-to-limited
    #: transition). Reported rather than inferred, because "0 / 100" right
    #: after a change is otherwise indistinguishable from a key that has made
    #: no calls.
    counter_reset: bool


class KeyInfo(BaseModel):
    id: int
    name: str
    key_prefix: str
    permission: str
    is_active: bool
    created_at: str
    last_used_at: str | None
    daily_request_limit: int | None


def _created_key_limit(req: CreateKeyRequest) -> int | None:
    """The `daily_request_limit` a newly created key receives.

    Three inputs, three answers, and the middle one is the whole reason this is
    a function rather than an `or`:

    * the field was **omitted** — apply `DEFAULT_DAILY_REQUEST_LIMIT`;
    * the field was sent as **null** — unlimited, exactly as before #194;
    * the field carries a **value** — that value, whatever the default is.

    `model_fields_set` is what separates the first two: it holds the names the
    request actually carried, so an absent field and a null field are
    distinguishable even though both read as `None`. An `or` against the
    default would silently turn the documented "unlimited" request into a
    limited key — the same silent substitution the panel's create handler
    deliberately refuses to perform (D9).

    **Applied here, in application code, never as a column default** (D9). A
    `server_default` would reach every future insert path, would be a schema
    change, and — the point — could not express "grandfather the rows that
    already exist": keys created before this setting existed keep whatever they
    carry, including NULL, with no migration and no backfill.

    The setting is read per request rather than captured at import, so this
    module never holds a stale copy of it.
    """
    if "daily_request_limit" in req.model_fields_set:
        return req.daily_request_limit
    return settings.default_daily_request_limit


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
        # Persisted, not dropped (#162). A create that accepted this field and
        # then ignored it handed the caller an unlimited key while reporting
        # success. Omitted now means the configured default (#194); explicit
        # null still means unlimited.
        daily_request_limit=_created_key_limit(req),
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
        daily_request_limit=api_key.daily_request_limit,
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
            daily_request_limit=k.daily_request_limit,
        )
        for k in keys
    ]


@router.put("/keys/{key_id}/limit", response_model=KeyLimitResponse)
async def set_key_limit(
    key_id: int,
    req: SetKeyLimitRequest,
    # Defaulted, like the panel's own handlers: `request` is here so the
    # ownership refusal can be *recorded* with its route and method, and a
    # caller that has none must not thereby change who is allowed through.
    # `_assert_key_owner` takes it keyword-only and tolerates `None`.
    request: Request = None,
    session: AsyncSession = Depends(get_session),
    user: User | _SingleUserSentinel = Depends(require_user_panel),
):
    """Set, change, or clear one key's daily request limit (#162).

    The API had create, revoke and delete but no update surface at all, so a
    limit could be set at creation and then never changed without the panel.
    This is deliberately the *only* mutable field exposed: widening the API's
    write surface is not what this change is for.

    Ownership, auth and CSRF are the router's and `_assert_key_owner`'s — the
    same posture as the sibling key routes, using the same predicate as the
    panel rather than a second inline copy of it.

    The enable-reset rule is `apply_daily_request_limit`, shared verbatim with
    the panel form, so the two surfaces cannot disagree about whether an
    operator is charged for traffic that was unlimited when it happened.
    """
    result = await session.execute(select(APIKey).where(APIKey.id == key_id))
    api_key = result.scalar_one_or_none()
    _assert_key_owner(api_key, user, request=request)

    counter_reset = await apply_daily_request_limit(
        session, api_key, req.daily_request_limit
    )
    return KeyLimitResponse(
        id=api_key.id,
        daily_request_limit=api_key.daily_request_limit,
        counter_reset=counter_reset,
    )


@router.delete("/keys/{key_id}")
async def revoke_key(
    key_id: int,
    request: Request = None,
    session: AsyncSession = Depends(get_session),
    user: User | _SingleUserSentinel = Depends(require_user_panel),
):
    result = await session.execute(select(APIKey).where(APIKey.id == key_id))
    api_key = result.scalar_one_or_none()
    if not api_key:
        raise HTTPException(404, "Key not found")
    # An inline duplicate of `_assert_key_owner`'s predicate, kept as it is
    # rather than folded into the helper — narrowing this route's 404/403
    # ordering is not what this change is for — but it refuses just as the
    # helper does, so it records just as the helper does.
    if not user.is_admin and api_key.user_id != user.id:
        _log_panel_forbidden(request, "not_your_key", user, api_key.user_id)
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
