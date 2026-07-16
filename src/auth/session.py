from contextvars import ContextVar
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.database import get_session
from src.models.db import User

current_user_id: ContextVar[int | None] = ContextVar("current_user_id", default=None)


@dataclass
class _SingleUserSentinel:
    id: int | None = None
    is_admin: bool = True
    username: str = "admin"
    vault_path: str | None = None
    is_active: bool = True


_SINGLE_USER_SENTINEL = _SingleUserSentinel()


async def get_current_user(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> User | _SingleUserSentinel | None:
    if not settings.multi_user_mode:
        return _SINGLE_USER_SENTINEL
    user_id = request.session.get("user_id") if hasattr(request, "session") else None
    if user_id is None:
        return None
    result = await session.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        return None
    # Starlette sessions are signed client-side cookies. Binding the cookie
    # to a database-backed version lets password resets invalidate every
    # previously issued session without introducing a server-side store.
    if request.session.get("session_version") != user.session_version:
        request.session.clear()
        return None
    return user


async def require_user(
    user: User | _SingleUserSentinel | None = Depends(get_current_user),
) -> User | _SingleUserSentinel:
    if user is None or (isinstance(user, User) and not user.is_active):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    return user


async def require_admin(
    user: User | _SingleUserSentinel = Depends(require_user),
) -> User | _SingleUserSentinel:
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin required")
    return user
