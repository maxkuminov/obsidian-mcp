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
from src.database import async_session, engine
from src.limiter import limiter
from src.mcp_server.auth import APIKeyMiddleware
from src.mcp_server.server import mcp
from src.oauth.routes import router as oauth_router
from src.services.indexer import run_indexer_loop
from src.services import vault_fs
from src.transfer.routes import router as transfer_router

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


MIN_PGVECTOR_VERSION = (0, 8, 0)


def _parse_pgvector_version(raw: str) -> tuple[int, ...] | None:
    """`'0.8.2'` → `(0, 8, 2)`. None when the string is not parseable.

    Only the leading numeric dot-separated components are read, so a suffixed
    build string (`0.8.0-rc1`) still compares as `0.8.0` rather than being
    treated as unknown.
    """
    parts: list[int] = []
    for chunk in raw.strip().split("."):
        digits = ""
        for ch in chunk:
            if not ch.isdigit():
                break
            digits += ch
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts) if parts else None


async def _check_pgvector_version() -> None:
    """Require pgvector >= 0.8.0, which is where `hnsw.iterative_scan` landed.

    This is not cosmetic. Postgres accepts `SET LOCAL hnsw.iterative_scan` on
    an older backend as an unrecognised *placeholder* GUC — no error, no
    warning — and the extension simply ignores it. Filtered semantic search
    would then keep silently dropping post-filter candidates while the code
    looks like it asked for the fix. Failing at startup makes that impossible.
    """
    async with async_session() as session:
        result = await session.execute(
            text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        )
        row = result.first()
    if row is None:
        # Extension not installed yet (fresh database before `make db-init` /
        # the first migration). Alembic creates it; the dim guard above takes
        # the same "defer to migrations" stance.
        return
    parsed = _parse_pgvector_version(str(row[0]))
    if parsed is None or parsed < MIN_PGVECTOR_VERSION:
        logging.getLogger(__name__).critical(
            "pgvector %s is too old: filtered semantic search needs "
            "hnsw.iterative_scan, which requires pgvector >= %s. Older "
            "versions accept the setting as an unknown placeholder GUC and "
            "silently run the non-iterative plan, which loses recall on every "
            "filtered search. Upgrade the pgvector extension "
            "(ALTER EXTENSION vector UPDATE) or the database image.",
            row[0],
            ".".join(str(p) for p in MIN_PGVECTOR_VERSION),
        )
        sys.exit(1)


def _check_openat2_support() -> None:
    """Refuse to start where a beneath-root lookup is impossible (#87).

    Every directory descriptor below a vault root is obtained with one
    `openat2(RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS)`,
    and there is deliberately **no fallback** to the per-component walk it
    replaced. A server that cannot enforce containment should not accept its
    first write, so this runs before one can arrive — and the guarantee would
    otherwise degrade invisibly, since every test runs on a kernel that has
    the syscall.

    Two causes, both named in the message because they need different fixes:
    a kernel older than 5.6, or a container seccomp profile that blocks the
    syscall (an older Docker default does, returning `EPERM` or `ENOSYS`
    depending on the profile's default action).

    The probe is **read-only and creates nothing** — one `openat2` of `"."`
    relative to a descriptor of the process's own working directory — which is
    why it can be a startup guard rather than a per-root probe like
    `probe_publication` (D21). It is not redundant with the refusal at the call
    site: this answers for the process at startup, the call site is what a
    future caller cannot get around, and `MCP_SANDBOX_MODE` — which skips this
    guard as it skips the others — is the one configuration in which a call
    site can be reached with the syscall unavailable.
    """
    try:
        vault_fs.probe_beneath_root_lookup()
    except vault_fs.UnsupportedFilesystem as exc:
        logging.getLogger(__name__).critical(
            "Cannot perform a beneath-root path lookup: %s Every vault write "
            "anchors to a directory descriptor obtained this way, and there is "
            "no safe fallback to opening one path component at a time — an "
            "ancestor renamed out of the vault between two such opens yields a "
            "descriptor outside the root while the tool reports success. "
            "Upgrade the kernel to 5.6 or newer, or allow openat2 in the "
            "container's seccomp profile.",
            exc,
        )
        sys.exit(1)


def _check_mount_identity_support() -> None:
    """Warn — and keep serving — when `statx` cannot report a mount id.

    `STATX_MNT_ID` is Linux 5.8 and is what lets transfer publication refuse a
    destination on a mount beneath the vault root *before* a body streams. It is
    a **transfer-write minimum, not a whole-server floor**, and the difference
    decides what this does.

    Without it `mount_id_of` raises and `request_upload`, `import_from_url` and
    `PUT /transfer/upload` refuse — the safe direction, since the alternative is
    consuming a whole body and failing `EXDEV` at the publish. Everything else —
    reads, search, every note tool, the panel, OAuth, downloads — is correct and
    unaffected. Exiting here would trade a transfer-only capability for a
    whole-server outage, which is the false-positive direction this codebase
    treats as the expensive failure.

    So: one warning naming exactly what is degraded, the verdict recorded for
    `/health`, and the server starts. Read-only, and skipped under
    `MCP_SANDBOX_MODE` alongside the other startup guards.
    """
    try:
        vault_fs.probe_mount_identity()
    except vault_fs.UnsupportedFilesystem as exc:
        vault_fs.record_mount_identity_support(False)
        logging.getLogger(__name__).warning(
            "Transfer writes are unavailable on this kernel: %s "
            "statx(2)'s STATX_MNT_ID (Linux 5.8) is how publication refuses a "
            "destination on a mount beneath the vault root before a body is "
            "streamed, and there is no safe substitute — st_dev compares equal "
            "across a same-filesystem bind mount. request_upload, "
            "import_from_url and PUT /transfer/upload will refuse; every other "
            "path — reads, search, the note tools, downloads, the panel — is "
            "unaffected. This is a transfer-write minimum, not a server floor, "
            "so the server is starting. /health reports it as "
            "transfer_mount_check_available.",
            exc,
        )
        return
    vault_fs.record_mount_identity_support(True)


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
    _check_openat2_support()
    _check_mount_identity_support()
    await _check_embedding_dim()
    await _check_pgvector_version()
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
        # Explicitly close pooled database connections during application
        # shutdown (important for reloads and test/application lifecycles).
        await engine.dispose()


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

from starlette.middleware.sessions import SessionMiddleware
# Browsers refuse Secure cookies on plain HTTP, so only localhost dev uses
# insecure cookies. BASE_URL is authoritative for proxy deployments.
# SessionMiddleware is always required: verify_csrf runs on all /admin routes
# regardless of multi_user_mode, and it depends on request.session.
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

# Capability-token transfer routes. Public by design: no OAuth chain in
# Traefik and no `APIKeyMiddleware` in the app — `APIKeyMiddleware` wraps only
# the `/mcp` mount, and `RootMCPProxyMiddleware` only rewrites a bare `/`, so a
# `/transfer/*` request carrying a bearer transfer token reaches these routes
# untouched. `tests/test_transfer_routes.py` asserts that rather than trusting
# it. Each handler authenticates its own request from the token row.
app.include_router(transfer_router)

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
    """Liveness, plus whether the named-staging fallback is in use.

    `vault_named_staging_fallback_active` is process state — true once a call
    has *actually* staged under a name because this vault's filesystem cannot
    allocate an unnamed inode and `VAULT_ALLOW_NAMED_STAGING_FALLBACK` is set.
    One field for both write paths, because there is one flag for both (D27).

    It reports false while nothing has staged under a name, including where the
    flag is set and every root supports unnamed staging: the distinction between
    "an operator enabled this defensively" and "this mount is taking the
    fallback" is the whole point of reporting it.

    `transfer_mount_check_available` is the startup verdict on `statx`'s
    `STATX_MNT_ID` (Linux 5.8) — `false` means transfer *writes* refuse on this
    kernel while every other path is unaffected, and `null` means the probe
    never ran (`MCP_SANDBOX_MODE`, or a process that has no lifespan). It is
    reported as found rather than guessed.

    It **never probes.** A probe writes, and a health check must not create a
    file in the vault — nor may it be the thing that decides a root's staging
    mode. The mount-identity probe is read-only, but it belongs to startup: a
    health check must not be where a capability verdict is made either.
    """
    return JSONResponse(
        {
            "status": "ok",
            "vault_named_staging_fallback_active": (
                vault_fs.named_staging_fallback_active()
            ),
            "transfer_mount_check_available": (
                vault_fs.mount_identity_available()
            ),
        }
    )


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
                # Rewrite into the normal mounted MCP route. Calling the MCP
                # handler directly here would bypass every middleware already
                # wrapped by ``self.app`` (TrustedHost, CORS, proxy headers,
                # security headers, sessions, and gzip).
                scope = dict(scope, path="/mcp/", raw_path=b"/mcp/")
        await self.app(scope, receive, send)


app.add_middleware(RootMCPProxyMiddleware)
app.add_middleware(MCPSlashRewriteMiddleware)
