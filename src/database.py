from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.config import settings
from src.services.pool_budget import POOL_SIZE, POOL_OVERFLOW
from src.services.transport_security import (
    STRICT_DB_MODES,
    database_ssl_connect_args,
    install_strict_transport_listener,
)

engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_size=POOL_SIZE,
    max_overflow=POOL_OVERFLOW,
    # 30s — SQLAlchemy's own default, written down rather than inherited. This
    # is the bound a pool-exhaustion failure is measured against: once all
    # 5 + 10 connections are checked out, every other caller (MCP tools, OAuth
    # `/token`, the panel) waits this long and then gets a `TimeoutError` → 500.
    # An assessment of #208 had to go read SQLAlchemy's source to learn the
    # number that decides how long a cross-tenant outage takes to become
    # visible; a number that load-bearing belongs in the engine configuration.
    #
    # Deliberately **no** `idle_in_transaction_session_timeout` in
    # `server_settings`. The index pass no longer holds a transaction across
    # its walk — since #278 (D9) the snapshot commits before the walk and the
    # locked transaction opens after it — but that transaction still awaits
    # worker threads between statements (C4 re-reads, parsing, tag and link
    # extraction of every changed note), which is idle-in-transaction time
    # that grows with how much changed, and the link backfill scans the vault
    # before its first insert. A server-side idle-in-transaction timeout would
    # kill those on the COMMIT. See the Non-Goals in the
    # `asvs-high-availability-hardening` design.
    pool_timeout=30,
    pool_pre_ping=True,
    # **Security posture, not debuggability.** SQLAlchemy renders the bound
    # parameters of a failing statement into `StatementError.__str__`, and this
    # server binds credential material on half a dozen hot paths: the API-key
    # and OAuth-token lookups bind a SHA-256 key hash, the transfer admission
    # binds a token hash, DCR binds a client-secret hash, the authorization-code
    # exchange binds a code hash, and the refresh rotation binds the hash of the
    # pair it is minting. A `StatementError` on any of them used to render that
    # hash into the exception's message — and from there into the `stack` field
    # of any security record carrying `exc_info`, and into the health page's
    # ERROR ring buffer. The parameters are worth less than the credential is:
    # every one of those statements is a single filtered lookup whose shape is
    # in the source, and the record still names the statement and the exception
    # class. Do not turn this off to debug a query; log the query.
    hide_parameters=True,
    connect_args={
        # 60s — embedding INSERTs into a vector(1024) column with an HNSW
        # index can take a few seconds each on a large vault. 10s (the old
        # value) caused QueryCanceledError on occasional notes and may have
        # left the indexer's session in a stuck state.
        "server_settings": {"statement_timeout": "60000"},
        # **The one place engine TLS is decided** (#184), shared with
        # `alembic/env.py`. A strict `DATABASE_SSL_MODE` is an explicit
        # `ssl.SSLContext`, not asyncpg's mode string: the string path consults
        # `$PGSSLROOTCERT` and `~/.postgresql/root.crt` and silently turns
        # `require` into verification when such a file exists, and only a
        # context is non-advisory by construction (no plaintext retry). The
        # settings validator already refused a TLS key in the URL, `PGSSL*`
        # variables and, under a strict mode, every Unix-socket route.
        **database_ssl_connect_args(settings),
    },
)

if settings.database_ssl_mode in STRICT_DB_MODES:
    # D3b: every *new* pooled connection is checked against `pg_stat_ssl` and
    # discarded if it is not encrypted — one query per connection, never per
    # checkout. The lifespan's startup assertion checks only the first.
    install_strict_transport_listener(engine)

async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_session() -> AsyncSession:
    async with async_session() as session:
        yield session
