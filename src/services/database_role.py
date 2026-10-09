"""The server must not run as a PostgreSQL superuser (#324, ASVS V13.2.2).

Two startup checks, both called from the lifespan:

* `check_database_role()` — after the database transport assertion, and so
  skipped by `MCP_SANDBOX_MODE`. Reads `rolsuper` for `current_user` on the
  application's own engine. A superuser session refuses the boot (one CRITICAL
  line, `sys.exit(1)`) unless `DATABASE_ALLOW_SUPERUSER=true`, which logs one
  WARNING and continues. A non-superuser session logs nothing. It applies to
  every deployment, not only the bundled compose stacks: the property — the
  long-lived process does not hold cluster administration — is what the ASVS
  requirement asks for, and this process is the only component that sees the
  real session. The Kubernetes bundle and the homelab compose already connect
  as a non-superuser owner and pass.
* `warn_admin_password_in_environment()` — one WARNING when a non-empty
  `POSTGRES_PASSWORD` is in this process's environment (an install that
  followed the pre-#324 instructions). A warning, not a refusal: the app
  cannot tell a live admin secret from a leftover. The bundles blank the
  variable in the app service, so there it cannot fire.

See openspec/changes/compose-db-roles, design D2 and D5.
"""
from __future__ import annotations

import logging
import os
import sys

logger = logging.getLogger(__name__)

_ROLE_PROBE = "SELECT current_user, rolsuper FROM pg_roles WHERE rolname = current_user"

UPGRADE_POINTER = (
    "DEPLOYMENT.md, 'Upgrading: split database roles' "
    "(a compose install created before #324 runs docker/upgrade-split-db-roles.sql)"
)


async def check_database_role() -> None:
    from sqlalchemy import text

    from src import database
    from src.config import settings

    async with database.async_session() as session:
        row = (await session.execute(text(_ROLE_PROBE))).first()
    if row is None:
        # current_user always has a pg_roles row; anything else is a server we
        # do not understand, and the safe reading is "could be a superuser".
        logger.critical(
            "Database role check: pg_roles has no row for current_user. "
            "Refusing to start."
        )
        sys.exit(1)
    role, is_superuser = row[0], bool(row[1])
    if not is_superuser:
        return
    if settings.database_allow_superuser:
        logger.warning(
            "Database role %s is a PostgreSQL superuser; continuing because "
            "DATABASE_ALLOW_SUPERUSER=true. Connect as a non-superuser role "
            "that owns the database instead; see %s.",
            role,
            UPGRADE_POINTER,
        )
        return
    logger.critical(
        "Database role %s is a PostgreSQL superuser. Refusing to start: the "
        "server must not hold cluster administration. Connect as a "
        "non-superuser role that owns the database (see %s), or set "
        "DATABASE_ALLOW_SUPERUSER=true to override.",
        role,
        UPGRADE_POINTER,
    )
    sys.exit(1)


def warn_admin_password_in_environment() -> None:
    if os.environ.get("POSTGRES_PASSWORD"):
        logger.warning(
            "POSTGRES_PASSWORD is set in the application's environment. The "
            "database superuser password belongs in postgres.env, which only "
            "the postgres service loads; remove it from .env (or wherever "
            "this process's environment comes from)."
        )


# ── alembic/env.py's hint for an unconverted install (design D6) ──────────

# 28P01 invalid_password, 28000 invalid_authorization_specification (no
# pg_hba entry, or no such role).
AUTHENTICATION_SQLSTATES = frozenset({"28P01", "28000"})


def is_authentication_failure(exc: BaseException) -> bool:
    """True when `exc` or anything it wraps carries an authentication SQLSTATE.

    SQLAlchemy re-raises asyncpg's error as its own `DBAPIError` subclass, with
    the driver's exception reachable through `.orig` and the `__cause__` /
    `__context__` chain, so a handler for asyncpg's classes alone never runs
    (Codex spec review). Every link is checked for `sqlstate` (asyncpg and
    SQLAlchemy's adapted errors) or `pgcode` (psycopg).
    """
    seen: set[int] = set()
    stack: list[BaseException | None] = [exc]
    while stack:
        current = stack.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        for attr in ("sqlstate", "pgcode"):
            code = getattr(current, attr, None)
            if isinstance(code, str) and code in AUTHENTICATION_SQLSTATES:
                return True
        stack.extend(
            (getattr(current, "orig", None), current.__cause__, current.__context__)
        )
    return False


def authentication_hint(role: str | None) -> str:
    return (
        f"database authentication failed for role {role or '(unknown)'}; a "
        "Compose install created before #324 must run "
        "docker/upgrade-split-db-roles.sql. See DEPLOYMENT.md 'Upgrading: "
        "split database roles'"
    )
