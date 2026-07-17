import asyncio
import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import text
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.middleware.trustedhost import TrustedHostMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from src.api.routes import router as api_router
from src.config import settings
from src.control_panel.routes import router as panel_router
from src.database import async_session
from src.limiter import limiter
from src.mcp_server.auth import APIKeyMiddleware
from src.mcp_server.server import mcp
from src.oauth.routes import router as oauth_router
from src.services.indexer import run_indexer_loop

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# Initialize the MCP app (creates session manager lazily)
_mcp_starlette = mcp.streamable_http_app()


async def _check_embedding_dim() -> None:
    """Compare the live `note_embeddings.embedding` column dim against
    `settings.embedding_dimensions`. Exit non-zero on mismatch.
    """
    configured = int(settings.embedding_dimensions)
    async with async_session() as session:
        result = await session.execute(
            text(
                "SELECT atttypmod FROM pg_attribute "
                "WHERE attrelid = 'note_embeddings'::regclass "
                "AND attname = 'embedding'"
            )
        )
        row = result.first()
    if row is None:
        # Table not yet migrated; let alembic handle it on first run.
        return
    column_dim = int(row[0])
    if column_dim != configured:
        logging.getLogger(__name__).critical(
            "Embedding dim mismatch: configured=%d, column=%d. "
            "Run `make reset-embeddings` to recreate the column at the "
            "configured dimension.",
            configured,
            column_dim,
        )
        sys.exit(1)


async def _validate_fts_configs() -> None:
    """Fail fast at startup if `FTS_CONFIGS` names a text-search config that
    isn't installed in this Postgres instance (e.g. a typo), so a bad config
    surfaces with a clear message instead of silent zero-result keyword
    searches. Delegates to `src.services.fts.validate_fts_configs`.
    """
    from src.services.fts import validate_fts_configs

    async with async_session() as session:
        await validate_fts_configs(session)


async def _warm_embedding_model() -> None:
    """Pre-load the embedding model so the first semantic_search after startup
    isn't a cold reload. Combined with OLLAMA_KEEP_ALIVE the model then stays
    resident. Best-effort: any failure is logged, never fatal. Ollama only —
    the OpenAI provider has no local warm state.
    """
    if settings.embedding_provider != "ollama":
        return
    try:
        from src.services.embeddings import get_provider

        await get_provider().embed_one("warmup")
        logging.getLogger(__name__).info(
            "Embedding model warm-up complete (keep_alive=%s)",
            settings.ollama_keep_alive,
        )
    except Exception as e:  # noqa: BLE001 - warm-up must never block startup
        logging.getLogger(__name__).warning(
            "Embedding model warm-up failed (non-fatal): %s", e
        )


def _on_indexer_done(task: asyncio.Task) -> None:
    if task.cancelled():
        logging.getLogger(__name__).info("Indexer task cancelled (lifespan shutdown)")
        return
    exc = task.exception()
    if exc is not None:
        logging.getLogger(__name__).critical(
            "Indexer task died with unhandled exception", exc_info=exc
        )
    else:
        logging.getLogger(__name__).warning("Indexer task exited without exception (should run forever)")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.mcp_sandbox_mode:
        logging.getLogger(__name__).warning(
            "MCP_SANDBOX_MODE active — skipping DB check and indexer. "
            "Tools are registered but cannot run. Registry-eval only."
        )
        async with mcp.session_manager.run():
            yield
        return
    await _check_embedding_dim()
    await _validate_fts_configs()
    # Fire-and-forget so a ~15s cold load doesn't block the app from serving.
    # The lifespan frame stays suspended at `yield`, keeping this referenced.
    warmup_task = asyncio.create_task(_warm_embedding_model())
    indexer_task = asyncio.create_task(run_indexer_loop())
    indexer_task.add_done_callback(_on_indexer_done)
    try:
        async with mcp.session_manager.run():
            yield
    finally:
        warmup_task.cancel()
        indexer_task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(indexer_task), timeout=10.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass


app = FastAPI(title="Obsidian MCP", lifespan=lifespan)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Honor X-Forwarded-Proto/For from upstream reverse proxy so that scheme-aware
# redirects (e.g. trailing-slash on /mcp) keep the https:// scheme.
app.add_middleware(
    ProxyHeadersMiddleware,
    trusted_hosts=["127.0.0.1", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"],
)

# Reject forged Host headers at the application boundary. This complements
# FastMCP's DNS-rebinding checks and also protects OAuth/admin routes.
app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)

# GZip compression for responses >= 1000 bytes
app.add_middleware(GZipMiddleware, minimum_size=1000)

if settings.multi_user_mode:
    from starlette.middleware.sessions import SessionMiddleware
    # Browsers refuse Secure cookies on plain HTTP, so only localhost dev uses
    # insecure cookies. BASE_URL is authoritative for proxy deployments.
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        max_age=settings.session_max_age,
        https_only=settings.base_url.startswith("https://"),
        same_site="lax",
        session_cookie=settings.session_cookie_name,
    )

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Authorization", "Content-Type", "X-CSRF-Token"],
)


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


# OAuth routes (public endpoints)
app.include_router(oauth_router)

# API routes (protected by Traefik OAuth)
app.include_router(api_router)

# Control panel routes at /admin (protected by Traefik OAuth)
app.include_router(panel_router)

# Admin user-management routes at /admin/users (admin-gated; in single-user
# mode the sentinel is admin so the routes work too, but the sidebar link
# is hidden when `multi_user_mode=False`).
from src.control_panel.users import router as users_router
app.include_router(users_router)

# Multi-user auth routes (login / logout / bootstrap registration).
# Mounted only in multi-user mode so single-user mode preserves the existing
# Traefik-OAuth-only path; the routes would 404 in that case anyway since
# `SessionMiddleware` isn't active.
if settings.multi_user_mode:
    from src.auth.routes import router as auth_router
    app.include_router(auth_router)


@app.get("/health")
async def health():
    return JSONResponse({"status": "ok"})


# MCP handler used by both /mcp mount and root proxy
async def mcp_handler(scope, receive, send):
    await mcp.session_manager.handle_request(scope, receive, send)


# Mount MCP at /mcp with API key auth
app.mount("/mcp", APIKeyMiddleware(mcp_handler))


class MCPSlashRewriteMiddleware:
    """Rewrite /mcp to /mcp/ so Starlette's Mount doesn't 307 redirect.

    Many MCP clients (including claude.ai) don't follow redirects on POST,
    which breaks the OAuth discovery flow.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] == "http" and scope["path"] == "/mcp":
            scope = dict(scope, path="/mcp/", raw_path=b"/mcp/")
        await self.app(scope, receive, send)


class RootMCPProxyMiddleware:
    """Intercept POST/GET/DELETE to / with Bearer token and route to MCP.

    Some MCP clients (e.g. OpenWebUI) strip the path and send requests to root.
    Without this, those requests hit the OAuth-protected panel router and fail.
    Supports both omcp_ API keys and OAuth Bearer tokens.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] == "http" and scope["path"] in ("/", ""):
            headers = dict(scope.get("headers", []))
            auth = headers.get(b"authorization", b"").decode()
            if auth.startswith("Bearer "):
                await APIKeyMiddleware(mcp_handler)(scope, receive, send)
                return
        await self.app(scope, receive, send)


app.add_middleware(RootMCPProxyMiddleware)
app.add_middleware(MCPSlashRewriteMiddleware)
