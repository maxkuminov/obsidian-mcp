import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import create_async_engine

from src.models.db import Base

target_metadata = Base.metadata

def include_object(object, name, type_, reflected, compare_to):
    """Exclude exactly one object from autogenerate: the half-precision vector
    index (#283, design D19).

    It is an expression index — `(embedding::halfvec(D)) halfvec_cosine_ops` —
    and deliberately not declared on the model: Alembic on SQLAlchemy 2
    compares expression indexes, strips the `::halfvec` cast by regex, and
    then either reports spurious drift or skips it with a warning. Left in the
    comparison undeclared, `alembic check` would report it as a drop. The
    schema gate verifies it through the catalogue instead (`pg_get_indexdef`,
    `indisvalid`, the operator class). Every other object — every other index
    included — is compared as before; `tests/test_perf_vector_index.py` pins
    that this names exactly one.
    """
    from src.services.vector_index import INDEX_NAME

    if type_ == "index" and name == INDEX_NAME:
        return False
    return True


def get_url():
    return os.environ.get("DATABASE_URL", context.config.get_main_option("sqlalchemy.url"))


def run_migrations_offline():
    url = get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection):
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations():
    # The same transport policy as the application engine (#184). The URL is
    # validated **before** the engine exists, because alembic may resolve a
    # different URL than the settings validator saw (`alembic.ini`'s fallback):
    # a TLS key in its query is refused, and under a strict mode so is every
    # Unix-socket route — no socket is ever opened, and no startup assertion
    # runs in this process to notice one afterwards.
    from src.config import settings
    from src.services.transport_security import (
        STRICT_DB_MODES,
        database_ssl_connect_args,
        install_strict_transport_listener,
        validate_database_url_transport,
    )

    url = get_url()
    validate_database_url_transport(url, settings.database_ssl_mode)
    connectable = create_async_engine(
        url,
        poolclass=pool.NullPool,
        connect_args=database_ssl_connect_args(settings),
    )
    if settings.database_ssl_mode in STRICT_DB_MODES:
        install_strict_transport_listener(connectable)
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online():
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
