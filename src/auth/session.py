from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.database import get_session
from src.models.db import User

current_user_id: ContextVar[int | None] = ContextVar("current_user_id", default=None)


class _UnsetVaultRoot:
    """Sentinel: this context carries no authenticated vault snapshot."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<unset vault root>"


UNSET_VAULT_ROOT = _UnsetVaultRoot()

# The authenticated request's *own* answer to "where is this user's vault, right
# now" — `(user_id, Path)` when assigned, `(user_id, None)` when the user has no
# usable assignment, and `UNSET_VAULT_ROOT` outside a request (indexer, panel,
# tests).
#
# It exists because the process-level cache in `src/services/vault.py` is shared
# with the indexer's bulk warm, and a bulk `SELECT` that started *before* an
# admin cleared `vault_path` can land *after* the per-request warm evicted the
# entry — re-admitting a user whose assignment was already revoked, mid-call.
# A snapshot bound to this request cannot be overwritten by another task, so
# `_vault_root` prefers it whenever it is set. See "The vault assignment is the
# admission gate for every tool" in CLAUDE.md.
current_vault_root: ContextVar[tuple[int, Path | None] | _UnsetVaultRoot] = ContextVar(
    "current_vault_root", default=UNSET_VAULT_ROOT
)

# The actor label denormalised onto every `usage_logs` row this request writes:
# `(kind, label, ref)` — `("api_key", <key name>, <omcp_ prefix>)` or
# `("oauth", <client_name>, <client_id>)`. `APIKeyMiddleware` binds it from the
# credential row it has already loaded, so the label costs no extra query and
# nothing later has to go looking for it.
#
# It exists because both credential tables are allowed to disappear while their
# history stays. `usage_logs.oauth_token_id` is `ON DELETE SET NULL`, so
# deleting an OAuth client cascades its tokens and unattributes every line that
# client produced; `usage_logs.key_id` has no `ON DELETE` at all, so the panel
# explicitly NULLs it before deleting an API key. Resolving the actor by LEFT
# JOIN at read time therefore turned all of that history into "unknown" — the
# evidence an operator opens `/admin/usage` to read, destroyed by the button
# they pressed to stop the client (issue #77). A label written at call time
# cannot be taken away by a later delete.
current_actor: ContextVar[tuple[str, str | None, str | None] | None] = ContextVar(
    "current_actor", default=None
)


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
    return await get_active_session_user(request, session)


async def get_active_session_user(
    request: Request,
    session: AsyncSession,
) -> User | None:
    """Resolve and validate a multi-user browser session.

    Keep every browser entry point on the same database-backed checks: the
    user must still exist, be active, and have the session version encoded in
    the signed cookie. Invalid cookies are cleared so they cannot be reused by
    another route (notably OAuth consent) after a password reset or account
    deactivation.
    """
    try:
        user_id = request.session.get("user_id")
    except (AssertionError, AttributeError):
        return None
    if user_id is None:
        return None
    result = await session.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        request.session.clear()
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
