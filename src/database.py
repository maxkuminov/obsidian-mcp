from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.config import settings

engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_size=5,
    max_overflow=10,
    # 30s — SQLAlchemy's own default, written down rather than inherited. This
    # is the bound a pool-exhaustion failure is measured against: once all
    # 5 + 10 connections are checked out, every other caller (MCP tools, OAuth
    # `/token`, the panel) waits this long and then gets a `TimeoutError` → 500.
    # An assessment of #208 had to go read SQLAlchemy's source to learn the
    # number that decides how long a cross-tenant outage takes to become
    # visible; a number that load-bearing belongs in the engine configuration.
    #
    # Deliberately **no** `idle_in_transaction_session_timeout` in
    # `server_settings`: the steady-state index pass holds one transaction from
    # its first select to its commit across the whole synchronous walk (minutes
    # on a large vault), the embed pass calls the provider before its first
    # statement, and the link backfill scans the vault before its first insert.
    # A server-side idle-in-transaction timeout would kill all three on the
    # COMMIT, every tick. See the Non-Goals in the
    # `asvs-high-availability-hardening` design.
    pool_timeout=30,
    pool_pre_ping=True,
    connect_args={
        # 60s — embedding INSERTs into a vector(1024) column with an HNSW
        # index can take a few seconds each on a large vault. 10s (the old
        # value) caused QueryCanceledError on occasional notes and may have
        # left the indexer's session in a stuck state.
        "server_settings": {"statement_timeout": "60000"}
    },
)

async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_session() -> AsyncSession:
    async with async_session() as session:
        yield session
