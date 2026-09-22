"""Transport policy for the application's own outbound hops (#184, #185).

Two hops leave this process to reach a dependency: the database connection
and the embedding endpoint. This module owns how each of them is secured, so
that no call site can decide it on its own:

* **Database.** `DATABASE_SSL_MODE` (libpq's vocabulary, default `prefer`) is
  the one source of TLS truth. The strict modes are an explicit
  `ssl.SSLContext`, never asyncpg's mode strings (design D2); every TLS input
  that could be merged with it by rules an operator cannot see — a TLS key in
  the URL's query, a `PGSSL*` variable — is refused instead of reconciled
  (D3); under a strict mode every Unix-socket route is refused before any
  connection is attempted (D3a) and every new pooled connection is checked
  against `pg_stat_ssl` (D3b); and the lifespan asserts the live session once
  at startup (D4).
* **Embedding endpoint.** The active provider's URL must be `https`, loopback
  `http`, or `http` admitted by `EMBEDDING_ALLOW_PLAINTEXT` (D6), judged with
  httpx's own parser; and every HTTP client that talks to it is built by
  `embedding_http_client`, with the environment switched off and redirects
  refused (D7).

`src/config.py` imports the pure helpers here from inside its validators, so
this module imports nothing from `src` at module level (`src.database` imports
`src.config`, which would otherwise be a cycle).

See `docs/architecture/schema-and-migrations.md` ("Database transport") and
`docs/architecture/indexing-and-embeddings.md` ("Embedding providers").
"""

from __future__ import annotations

import asyncio
import logging
import os
import ssl
import sys
from typing import Any, Mapping, NamedTuple
from urllib.parse import unquote

import httpx

logger = logging.getLogger(__name__)


# ── Database: the modes ─────────────────────────────────────────────────────

#: Every accepted `DATABASE_SSL_MODE`, after strip + case-fold. `allow` is not
#: offered: it tries plaintext first and is strictly weaker than the default.
DB_SSL_MODES = ("disable", "prefer", "require", "verify-ca", "verify-full")

#: The modes under which a plaintext session is a refusal, not a warning.
STRICT_DB_MODES = frozenset({"require", "verify-ca", "verify-full"})

#: The modes whose trust anchor is `DATABASE_SSL_CA_FILE`.
VERIFYING_DB_MODES = frozenset({"verify-ca", "verify-full"})

#: `DATABASE_URL` query keys that carry TLS configuration. SQLAlchemy passes
#: every query key to `asyncpg.connect()` as a keyword and then lets
#: `connect_args` override it, so any of these would be silently merged with
#: (or discarded in favour of) `DATABASE_SSL_MODE`. Compared case-insensitively.
DB_URL_TLS_KEYS = frozenset(
    {
        "ssl",
        "sslmode",
        "sslrootcert",
        "sslcert",
        "sslkey",
        "sslcrl",
        "sslpassword",
        "sslnegotiation",
        "direct_tls",
    }
)

#: Query keys that can name a host this module cannot see (a service file).
_DB_URL_SERVICE_KEYS = frozenset({"service", "servicefile"})

#: Environment variables asyncpg consults for a host when the URL gives none,
#: or that name a service file which can supply one.
_DB_HOST_ENV_VARS = ("PGHOST", "PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE")

#: The one statement both the startup probe and the per-connection check run.
#: Readable by an unprivileged role for its own backend.
_PG_STAT_SSL_PROBE = (
    "SELECT ssl, version, cipher FROM pg_stat_ssl WHERE pid = pg_backend_pid()"
)


class DatabaseTransportError(ConnectionError):
    """A new database connection under a strict mode is not encrypted.

    Raised from the D3b pool listener, which the pre-connect refusal (D3a) is
    supposed to make unreachable — so reaching it is a bug, and it surfaces as
    an ordinary connection error rather than an exit.
    """


def normalise_db_ssl_mode(value: Any) -> Any:
    """Strip and case-fold a mode string; leave anything else to the type check."""
    if isinstance(value, str):
        return value.strip().lower()
    return value


def build_database_ssl_context(
    mode: str,
    ca_file: str | None = None,
    cert_file: str | None = None,
    key_file: str | None = None,
) -> ssl.SSLContext:
    """The explicit TLS client context for a strict mode (design D2 table).

    `PROTOCOL_TLS_CLIENT` with TLS 1.2 as the floor. `require` encrypts without
    verifying; `verify-ca` requires a chain to `ca_file` and does not check the
    hostname; `verify-full` also checks the hostname against the URL's host.
    Only `ca_file` is loaded as a trust anchor — no system store, no
    `$PGSSLROOTCERT`, no `~/.postgresql/root.crt`.

    A load failure raises `ValueError` naming the setting, never the file's
    contents.
    """
    if mode not in STRICT_DB_MODES:
        raise ValueError(f"no TLS context for DATABASE_SSL_MODE={mode}")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    if mode == "require":
        # Order matters: `check_hostname` must be off before `CERT_NONE`.
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    else:
        ctx.check_hostname = mode == "verify-full"
        ctx.verify_mode = ssl.CERT_REQUIRED
        if not ca_file:
            raise ValueError(f"DATABASE_SSL_MODE={mode} requires DATABASE_SSL_CA_FILE")
        try:
            ctx.load_verify_locations(cafile=ca_file)
        except (ssl.SSLError, OSError, ValueError) as exc:
            raise ValueError(
                "DATABASE_SSL_CA_FILE could not be loaded as a PEM CA "
                f"certificate ({type(exc).__name__})"
            ) from None
        if not ctx.get_ca_certs():
            raise ValueError(
                "DATABASE_SSL_CA_FILE contains no CA certificate (a PEM "
                "certificate is required)"
            )
    if cert_file and key_file:
        try:
            ctx.load_cert_chain(certfile=cert_file, keyfile=key_file)
        except (ssl.SSLError, OSError, ValueError) as exc:
            raise ValueError(
                "DATABASE_SSL_CERT_FILE / DATABASE_SSL_KEY_FILE could not be "
                f"loaded as a client certificate and key ({type(exc).__name__})"
            ) from None
    return ctx


def database_ssl_connect_args(settings: Any) -> dict[str, Any]:
    """The `ssl` connect argument for every production connection creator.

    `disable` → `False` (plaintext); `prefer` → asyncpg's own advisory string,
    exactly today's behaviour; a strict mode → an explicit `SSLContext`, which
    asyncpg treats as mandatory TLS with **no** plaintext retry and which makes
    it consult no environment variable or home-directory file.
    """
    mode = settings.database_ssl_mode
    if mode == "disable":
        return {"ssl": False}
    if mode == "prefer":
        return {"ssl": "prefer"}
    return {
        "ssl": build_database_ssl_context(
            mode,
            settings.database_ssl_ca_file,
            settings.database_ssl_cert_file,
            settings.database_ssl_key_file,
        )
    }


def _is_socket_candidate(host: str) -> bool:
    """A host asyncpg would dial as a Unix socket (or an abstract one)."""
    decoded = unquote(host)
    return decoded.startswith(("/", "@")) or host.startswith(("/", "@"))


def _raw_host_candidates(url: Any) -> list[str]:
    """Every host token the URL spells: netloc host and each `?host=` value."""
    candidates: list[str] = []
    if url.host:
        candidates.append(url.host)
    for key, value in url.query.items():
        if key.lower() != "host":
            continue
        values = value if isinstance(value, (list, tuple)) else (value,)
        for item in values:
            candidates.extend(str(item).split(","))
    return candidates


def _effective_hosts(url: Any) -> list[str]:
    """The host list asyncpg will actually receive, after SQLAlchemy's parsing.

    Computed with the asyncpg dialect's own `create_connect_args`, so the
    netloc/query precedence and the `host=h:p` splitting are SQLAlchemy's, not
    a re-implementation: `postgresql+asyncpg://u:p@db.example/app?host=:5432`
    reaches asyncpg with `host=''` (which then falls through to `$PGHOST` and
    the socket directories), although the URL's netloc names a TCP host.
    Raises `ValueError` when SQLAlchemy cannot turn the URL into arguments.
    """
    from sqlalchemy.dialects.postgresql.asyncpg import PGDialect_asyncpg

    try:
        _, opts = PGDialect_asyncpg().create_connect_args(url)
    except Exception as exc:  # noqa: BLE001 - any parse failure is a refusal
        raise ValueError(type(exc).__name__) from None
    host = opts.get("host")
    if host is None:
        return []
    if isinstance(host, (list, tuple)):
        return [str(h) for h in host]
    return [str(host)]


def validate_database_url_transport(
    url: str,
    mode: str,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Refuse a database URL / environment that contradicts `mode` (D3, D3a).

    Shared by the settings validator (for `DATABASE_URL`) and
    `alembic/env.py` (for the URL alembic resolves), so both refuse the same
    inputs before any engine exists. Raises `ValueError`; messages never
    contain the URL or its password.

    In every mode:
    * a TLS key in the URL's query, and
    * a `PGSSL*` environment variable
    are refused — TLS for the database is configured only by `DATABASE_SSL_*`.

    Under `require`, `verify-ca` and `verify-full`, every route to a Unix
    socket (which never carries TLS, and for which asyncpg ignores the context)
    is refused: a socket-shaped host token anywhere in the URL, an *effective*
    host list that is empty, contains an empty host, or contains a socket path
    after SQLAlchemy's own parsing, a `service`/`servicefile` query key, and
    `PGHOST`/`PGHOSTADDR`/`PGSERVICE`/`PGSERVICEFILE` in the environment.
    """
    from sqlalchemy.engine import make_url

    env = os.environ if environ is None else environ
    try:
        parsed = make_url(url)
    except Exception:  # noqa: BLE001 - the message must not echo the URL
        raise ValueError("DATABASE_URL is not a valid database URL") from None

    for key in parsed.query:
        if key.lower() in DB_URL_TLS_KEYS:
            raise ValueError(
                f"DATABASE_URL carries the TLS query parameter {key!r}. TLS for "
                "the database is configured only through DATABASE_SSL_MODE "
                "(and DATABASE_SSL_CA_FILE / DATABASE_SSL_CERT_FILE / "
                "DATABASE_SSL_KEY_FILE); remove it from the URL and set "
                "DATABASE_SSL_MODE instead."
            )
    for name in env:
        if name.upper().startswith("PGSSL"):
            raise ValueError(
                f"The environment variable {name} is set. TLS for the database "
                "is configured only through DATABASE_SSL_MODE (and "
                "DATABASE_SSL_CA_FILE / DATABASE_SSL_CERT_FILE / "
                f"DATABASE_SSL_KEY_FILE); unset {name} and use DATABASE_SSL_MODE."
            )

    if mode not in STRICT_DB_MODES:
        return

    def _refuse(route: str) -> None:
        raise ValueError(
            f"DATABASE_SSL_MODE={mode} cannot be honoured: {route}. A Unix "
            "socket never carries TLS. Use DATABASE_SSL_MODE=disable or prefer "
            "for a socket, or name a TCP host in DATABASE_URL for a strict mode."
        )

    for key in parsed.query:
        if key.lower() in _DB_URL_SERVICE_KEYS:
            _refuse(
                f"DATABASE_URL carries the {key!r} query parameter, and a "
                "service file can supply a socket host"
            )
    for name in _DB_HOST_ENV_VARS:
        if env.get(name):
            _refuse(
                f"the environment variable {name} is set and can supply a "
                "socket host"
            )
    for candidate in _raw_host_candidates(parsed):
        if _is_socket_candidate(candidate.strip()):
            _refuse("DATABASE_URL names a Unix-socket host")
    try:
        hosts = _effective_hosts(parsed)
    except ValueError as exc:
        _refuse(
            "the effective host list of DATABASE_URL could not be determined "
            f"({exc})"
        )
    if not hosts:
        _refuse(
            "DATABASE_URL names no host, so the driver would fall back to "
            "$PGHOST and then to its default socket directories"
        )
    for host in hosts:
        if not host.strip():
            _refuse(
                "DATABASE_URL's effective host is empty, so the driver would "
                "fall back to $PGHOST and then to its default socket directories"
            )
        if _is_socket_candidate(host):
            _refuse("DATABASE_URL's effective host is a Unix socket")


def _verify_new_connection(dbapi_connection: Any, connection_record: Any) -> None:
    """D3b: refuse a newly opened pooled connection that is not encrypted.

    Runs once per **new** connection (a pool `connect` event), never per
    checkout. The adapted asyncpg cursor runs synchronously here because the
    pool connects inside SQLAlchemy's greenlet.
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute(_PG_STAT_SSL_PROBE)
        row = cursor.fetchone()
    finally:
        try:
            cursor.close()
        except Exception:  # noqa: BLE001 - the verdict below still stands
            pass
    if not (row and row[0]):
        try:
            dbapi_connection.close()
        except Exception:  # noqa: BLE001 - it is being discarded either way
            pass
        raise DatabaseTransportError(
            "A new database connection is not encrypted although "
            "DATABASE_SSL_MODE is strict; the connection was discarded."
        )


def install_strict_transport_listener(engine: Any) -> None:
    """Attach the D3b per-connection check to `engine` (idempotent)."""
    from sqlalchemy import event

    target = getattr(engine, "sync_engine", engine)
    if not event.contains(target, "connect", _verify_new_connection):
        event.listen(target, "connect", _verify_new_connection)


def strict_transport_listener_installed(engine: Any) -> bool:
    from sqlalchemy import event

    target = getattr(engine, "sync_engine", engine)
    return event.contains(target, "connect", _verify_new_connection)


def _probe_errors() -> tuple[type[BaseException], ...]:
    errors: list[type[BaseException]] = [
        OSError,  # ConnectionError, ssl.SSLError and socket errors included
        asyncio.TimeoutError,
        TimeoutError,
    ]
    try:
        from sqlalchemy.exc import SQLAlchemyError

        errors.append(SQLAlchemyError)
    except ImportError:  # pragma: no cover - sqlalchemy is a hard dependency
        pass
    try:
        import asyncpg

        errors.extend([asyncpg.PostgresError, asyncpg.InterfaceError])
    except ImportError:  # pragma: no cover
        pass
    return tuple(errors)


async def check_database_transport() -> None:
    """D4: assert the application's own database session's transport, once.

    Awaited in the lifespan before every other database check. Under a strict
    mode an unencrypted session, a missing `pg_stat_ssl` row, or a connection
    that could not be established logs CRITICAL and exits. Under `prefer` /
    `disable` a plaintext session emits one `internal_transport_plaintext`
    event. Either way, one INFO line reports the effective transport;
    `server_verified` comes from the mode, never from the session.
    """
    from sqlalchemy import text

    from src import database
    from src.config import settings
    from src.services import security_events

    mode = settings.database_ssl_mode
    strict = mode in STRICT_DB_MODES
    try:
        async with database.async_session() as session:
            row = (await session.execute(text(_PG_STAT_SSL_PROBE))).first()
    except _probe_errors() as exc:
        if strict:
            # The class name only: a driver error's text can carry the DSN.
            logger.critical(
                "Database transport: DATABASE_SSL_MODE=%s but no session could "
                "be established (%s). The server may not offer TLS, or its "
                "certificate did not verify.",
                mode,
                type(exc).__name__,
            )
            sys.exit(1)
        raise

    encrypted = bool(row is not None and row[0])
    tls_version = row[1] if (encrypted and row is not None) else None
    if strict and not encrypted:
        logger.critical(
            "Database transport: DATABASE_SSL_MODE=%s but the session is not "
            "encrypted (pg_stat_ssl %s). Refusing to start.",
            mode,
            "returned no row" if row is None else "reports ssl=false",
        )
        sys.exit(1)
    if not encrypted:
        security_events.emit(
            "internal_transport_plaintext", reason="database", outcome=mode
        )
    logger.info(
        "Database transport: mode=%s encrypted=%s tls_version=%s server_verified=%s",
        mode,
        encrypted,
        tls_version or "-",
        mode in VERIFYING_DB_MODES,
    )
