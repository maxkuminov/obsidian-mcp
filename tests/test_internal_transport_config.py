"""Settings-time transport policy for the database and embedding hops (#184, #185).

Covers tasks 1.8, 1.8a, 1.9 and 2.5 of `internal-transport-tls`:

* the `DATABASE_SSL_MODE` matrix and every D3 refusal;
* the D3a socket-route refusal under strict modes, proven to open nothing —
  both at settings construction and on `alembic/env.py`'s path, driven in a
  subprocess with sentinels on `asyncpg.connect` and the event loop's
  connection functions;
* the D3b per-connection listener, and which engines carry it;
* the explicit TLS contexts and the connect arguments every creator receives;
* the embedding endpoint scheme policy (D6), its parser cases and the
  `EMBEDDING_CA_FILE` parse-at-boot rule.

The certificates under `tests/fixtures/transport_tls/` were generated for this
suite and are **test-only**: the keys protect nothing and must never be used
outside it.
"""

from __future__ import annotations

import json
import os
import ssl
import subprocess
import sys
import textwrap
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest

from src.config import Settings
from src.services import transport_security as ts

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "transport_tls"
CA = str(FIXTURES / "ca.pem")
OTHER_CA = str(FIXTURES / "other_ca.pem")
CLIENT_CERT = str(FIXTURES / "client.pem")
CLIENT_KEY = str(FIXTURES / "client.key")
NOT_A_CERT = str(FIXTURES / "not_a_cert.pem")

SECRET = "a-real-test-secret-not-a-placeholder"
PASSWORD = "s3cr3t-db-pw"
TCP_URL = f"postgresql+asyncpg://app:{PASSWORD}@db.example:5432/app"


def make(**kw) -> Settings:
    kw.setdefault("secret_key", SECRET)
    kw.setdefault("database_url", TCP_URL)
    return Settings(_env_file=None, **kw)


def refused(match: str | None = None, **kw) -> str:
    with pytest.raises(ValueError) as info:
        make(**kw)
    text = str(info.value)
    assert PASSWORD not in text
    if match is not None:
        assert match in text, text
    return text


# ════════════════════════════════════════════════════════════════════════════
# Database — the mode matrix (1.8)
# ════════════════════════════════════════════════════════════════════════════


def test_default_mode_is_prefer():
    assert make().database_ssl_mode == "prefer"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("disable", "disable"),
        ("prefer", "prefer"),
        ("REQUIRE", "require"),
        (" Verify-Full ", "verify-full"),
        ("verify-ca", "verify-ca"),
    ],
)
def test_modes_are_normalised(raw, expected):
    kw = {"database_ssl_mode": raw}
    if expected.startswith("verify"):
        kw["database_ssl_ca_file"] = CA
    assert make(**kw).database_ssl_mode == expected


def test_mode_from_the_environment_is_normalised(monkeypatch):
    monkeypatch.setenv("DATABASE_SSL_MODE", " Verify-Full ")
    monkeypatch.setenv("DATABASE_SSL_CA_FILE", CA)
    assert make().database_ssl_mode == "verify-full"


@pytest.mark.parametrize("raw", ["allow", "true", "on", "verify_full", "", "strict"])
def test_unknown_or_weaker_modes_are_refused(raw):
    with pytest.raises(ValueError) as info:
        make(database_ssl_mode=raw)
    text = str(info.value)
    for mode in ts.DB_SSL_MODES:
        assert mode in text


# ── D3: one source of TLS truth ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "query",
    ["ssl=require", "sslmode=disable", "SSLRootCert=x", "sslcert=x", "direct_tls=true",
     "sslnegotiation=direct", "prepared_statement_cache_size=0&sslkey=x"],
)
@pytest.mark.parametrize("mode", ["prefer", "disable", "require"])
def test_a_tls_key_in_the_url_is_refused_in_every_mode(query, mode):
    refused(
        "DATABASE_SSL_MODE",
        database_url=f"{TCP_URL}?{query}",
        database_ssl_mode=mode,
    )


def test_a_non_tls_url_parameter_is_untouched():
    s = make(database_url=f"{TCP_URL}?prepared_statement_cache_size=0")
    assert s.database_ssl_mode == "prefer"


@pytest.mark.parametrize("var", ["PGSSLMODE", "PGSSLROOTCERT", "PGSSLNEGOTIATION"])
def test_a_pgssl_environment_variable_is_refused(monkeypatch, var):
    monkeypatch.setenv(var, "require")
    text = refused("DATABASE_SSL_MODE")
    assert var in text


def test_a_ca_file_with_require_is_refused():
    text = refused(database_ssl_mode="require", database_ssl_ca_file=CA)
    assert "verify-ca" in text and "verify-full" in text


@pytest.mark.parametrize("mode", ["disable", "prefer"])
def test_a_ca_file_with_a_lax_mode_is_refused(mode):
    refused("DATABASE_SSL_CA_FILE", database_ssl_mode=mode, database_ssl_ca_file=CA)


@pytest.mark.parametrize("mode", ["verify-ca", "verify-full"])
def test_a_verifying_mode_without_a_ca_is_refused(mode):
    refused("DATABASE_SSL_CA_FILE", database_ssl_mode=mode)


def test_a_missing_ca_file_is_refused(tmp_path):
    refused(
        "DATABASE_SSL_CA_FILE",
        database_ssl_mode="verify-full",
        database_ssl_ca_file=str(tmp_path / "absent.pem"),
    )


def test_a_directory_as_ca_file_is_refused(tmp_path):
    refused(
        "DATABASE_SSL_CA_FILE",
        database_ssl_mode="verify-full",
        database_ssl_ca_file=str(tmp_path),
    )


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_an_unreadable_ca_file_is_refused(tmp_path):
    path = tmp_path / "ca.pem"
    path.write_bytes(Path(CA).read_bytes())
    path.chmod(0)
    refused(
        "DATABASE_SSL_CA_FILE",
        database_ssl_mode="verify-full",
        database_ssl_ca_file=str(path),
    )


def test_a_non_pem_ca_file_is_refused():
    refused(
        "DATABASE_SSL_CA_FILE",
        database_ssl_mode="verify-ca",
        database_ssl_ca_file=NOT_A_CERT,
    )


@pytest.mark.parametrize(
    "kw",
    [{"database_ssl_cert_file": CLIENT_CERT}, {"database_ssl_key_file": CLIENT_KEY}],
)
def test_half_a_client_pair_is_refused(kw):
    refused("DATABASE_SSL_KEY_FILE", database_ssl_mode="require", **kw)


@pytest.mark.parametrize("mode", ["prefer", "disable"])
def test_a_client_pair_under_a_lax_mode_is_refused(mode):
    refused(
        "DATABASE_SSL_CERT_FILE",
        database_ssl_mode=mode,
        database_ssl_cert_file=CLIENT_CERT,
        database_ssl_key_file=CLIENT_KEY,
    )


def test_an_unloadable_client_pair_is_refused():
    refused(
        "DATABASE_SSL_CERT_FILE",
        database_ssl_mode="require",
        database_ssl_cert_file=NOT_A_CERT,
        database_ssl_key_file=CLIENT_KEY,
    )


def test_blank_file_settings_mean_unset(monkeypatch):
    monkeypatch.setenv("DATABASE_SSL_CA_FILE", "")
    monkeypatch.setenv("DATABASE_SSL_CERT_FILE", " ")
    s = make()
    assert s.database_ssl_ca_file is None and s.database_ssl_cert_file is None


def test_a_client_pair_under_a_strict_mode_is_accepted():
    s = make(
        database_ssl_mode="verify-full",
        database_ssl_ca_file=CA,
        database_ssl_cert_file=CLIENT_CERT,
        database_ssl_key_file=CLIENT_KEY,
    )
    assert s.database_ssl_cert_file == CLIENT_CERT


# ════════════════════════════════════════════════════════════════════════════
# Database — socket routes under strict modes (1.8a, Codex r1 #2 and r2)
# ════════════════════════════════════════════════════════════════════════════

STRICT = ["require", "verify-ca", "verify-full"]

REFUSED_URLS = {
    "query_socket": "postgresql+asyncpg://u:p@/db?host=/var/run/postgresql",
    "mixed_multihost": "postgresql+asyncpg://u:p@/db?host=db.example:5432,/run/postgresql",
    "repeated_host": "postgresql+asyncpg://u:p@db.example/db?host=db.example&host=/tmp",
    "percent_netloc": "postgresql+asyncpg://u:p@%2Frun%2Fpostgresql/db",
    "abstract": "postgresql+asyncpg://u:p@@abstract/db",
    "no_host": "postgresql+asyncpg://u:p@/db",
    "service": "postgresql+asyncpg://u:p@db.example/db?service=x",
    "servicefile": "postgresql+asyncpg://u:p@db.example/db?servicefile=/x",
    # Codex spec round 2: the *effective* host after SQLAlchemy's parsing.
    # `?host=:5432` becomes `host=''`, and asyncpg then falls through to
    # `$PGHOST` and its socket directories — with or without a TCP netloc.
    "empty_query_host_with_netloc": "postgresql+asyncpg://u:p@db.example/app?host=:5432",
    "empty_query_host_without_netloc": "postgresql+asyncpg://u:p@/app?host=:5432",
}


def _strict_kw(mode: str) -> dict:
    kw = {"database_ssl_mode": mode}
    if mode.startswith("verify"):
        kw["database_ssl_ca_file"] = CA
    return kw


@pytest.fixture
def connect_sentinels(monkeypatch):
    """Fail — and record — any attempt to open a database connection."""
    import asyncio
    import asyncio.unix_events

    import asyncpg

    calls: list[str] = []

    async def _sentinel(*args, **kwargs):
        calls.append("called")
        raise AssertionError("a connection was attempted")

    monkeypatch.setattr(asyncpg, "connect", _sentinel)
    monkeypatch.setattr(asyncio.AbstractEventLoop, "create_unix_connection", _sentinel)
    monkeypatch.setattr(
        asyncio.unix_events._UnixSelectorEventLoop, "create_unix_connection", _sentinel
    )
    return calls


@pytest.mark.parametrize("name", sorted(REFUSED_URLS))
@pytest.mark.parametrize("mode", STRICT)
def test_a_socket_route_is_refused_under_a_strict_mode(name, mode, connect_sentinels):
    text = refused("DATABASE_SSL_MODE", database_url=REFUSED_URLS[name], **_strict_kw(mode))
    assert "socket" in text.lower() or "service" in text.lower()
    assert connect_sentinels == []


@pytest.mark.parametrize("var", ["PGHOST", "PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE"])
@pytest.mark.parametrize("mode", STRICT)
def test_a_host_environment_variable_is_refused_under_a_strict_mode(
    monkeypatch, var, mode, connect_sentinels
):
    monkeypatch.setenv(var, "/var/run/postgresql")
    text = refused(**_strict_kw(mode))
    assert var in text
    assert connect_sentinels == []


@pytest.mark.parametrize(
    "name",
    ["query_socket", "percent_netloc", "no_host", "abstract",
     "empty_query_host_with_netloc", "empty_query_host_without_netloc"],
)
@pytest.mark.parametrize("mode", ["prefer", "disable"])
def test_lax_modes_keep_sockets(name, mode):
    assert make(database_url=REFUSED_URLS[name], database_ssl_mode=mode)


def test_lax_mode_ignores_the_host_environment(monkeypatch):
    monkeypatch.setenv("PGHOST", "/var/run/postgresql")
    assert make(database_ssl_mode="prefer")


@pytest.mark.parametrize("mode", STRICT)
@pytest.mark.parametrize(
    "url",
    [
        TCP_URL,
        "postgresql+asyncpg://u:p@db.example/db?host=db.example:5432",
        "postgresql+asyncpg://u:p@[::1]:5432/db",
    ],
)
def test_a_tcp_url_is_accepted_under_a_strict_mode(mode, url):
    assert make(database_url=url, **_strict_kw(mode))


def test_the_effective_host_check_is_sqlalchemys_own_parse():
    """The netloc names a TCP host, but SQLAlchemy hands asyncpg `host=''`."""
    from sqlalchemy.engine import make_url

    url = make_url(REFUSED_URLS["empty_query_host_with_netloc"])
    assert url.host == "db.example"
    assert ts._effective_hosts(url) == [""]


# ── The same refusals on alembic's path, in a real subprocess ───────────────

_ALEMBIC_WRAPPER = textwrap.dedent(
    """
    import asyncio, asyncio.unix_events, json, pathlib, ssl, sys
    marker = pathlib.Path(sys.argv[1])
    ini = sys.argv[2]

    def note(entry):
        with marker.open("a") as fh:
            fh.write(json.dumps(entry) + "\\n")

    async def _sentinel(*args, **kwargs):
        s = kwargs.get("ssl")
        note({"call": "connect", "ssl": type(s).__name__ if not isinstance(s, (str, bool)) else s})
        raise ConnectionRefusedError("sentinel: no connection may be attempted")

    async def _unix_sentinel(*args, **kwargs):
        note({"call": "unix"})
        raise ConnectionRefusedError("sentinel")

    import asyncpg
    asyncpg.connect = _sentinel
    asyncio.AbstractEventLoop.create_unix_connection = _unix_sentinel
    asyncio.unix_events._UnixSelectorEventLoop.create_unix_connection = _unix_sentinel

    import src.services.transport_security as ts
    _orig = ts.install_strict_transport_listener
    def _recording(engine):
        note({"call": "listener"})
        return _orig(engine)
    ts.install_strict_transport_listener = _recording

    from alembic.config import main
    main(argv=["-c", ini, "upgrade", "head"])
    """
)


def _subprocess_env(tmp_path, **extra) -> dict:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "PYTHONPATH": str(ROOT),
        "SECRET_KEY": SECRET,
        "VAULT_PATH": str(tmp_path),
        "EMBEDDING_ALLOW_PLAINTEXT": "true",
    }
    env.update(extra)
    return env


def _run_alembic(tmp_path, alembic_url: str, **env_extra):
    ini = tmp_path / "alembic.ini"
    ini.write_text(
        "[alembic]\n"
        f"script_location = {ROOT / 'alembic'}\n"
        # configparser interpolation: a literal `%` is `%%`.
        f"sqlalchemy.url = {alembic_url.replace('%', '%%')}\n"
    )
    marker = tmp_path / "calls.jsonl"
    result = subprocess.run(
        [sys.executable, "-c", _ALEMBIC_WRAPPER, str(marker), str(ini)],
        cwd=tmp_path,
        env=_subprocess_env(tmp_path, **env_extra),
        capture_output=True,
        text=True,
        timeout=120,
    )
    calls = (
        [json.loads(line) for line in marker.read_text().splitlines()]
        if marker.exists()
        else []
    )
    return result, calls


@pytest.mark.parametrize("name", sorted(REFUSED_URLS))
def test_alembic_refuses_a_socket_route_before_connecting(tmp_path, name):
    """`DATABASE_URL` is unset in the child, so the settings validator sees its
    TCP default and passes; alembic resolves `alembic.ini`'s socket URL, and
    `run_async_migrations` must refuse it before building the engine."""
    result, calls = _run_alembic(
        tmp_path, REFUSED_URLS[name], DATABASE_SSL_MODE="require"
    )
    assert result.returncode != 0
    assert "DATABASE_SSL_MODE=require" in result.stderr, result.stderr[-2000:]
    assert calls == [], calls


def test_alembic_and_settings_both_refuse_the_same_socket_url(tmp_path):
    """With `DATABASE_URL` naming the socket, the settings construction that
    `alembic/env.py` triggers refuses first — still before any connection."""
    url = REFUSED_URLS["empty_query_host_with_netloc"]
    result, calls = _run_alembic(
        tmp_path, url, DATABASE_URL=url, DATABASE_SSL_MODE="require"
    )
    assert result.returncode != 0
    assert "DATABASE_SSL_MODE=require" in result.stderr
    assert calls == []


def test_alembic_refuses_a_tls_key_in_its_url(tmp_path):
    result, calls = _run_alembic(tmp_path, f"{TCP_URL}?sslmode=disable")
    assert result.returncode != 0
    assert "DATABASE_SSL_MODE" in result.stderr
    assert PASSWORD not in result.stderr
    assert calls == []


@pytest.mark.parametrize(
    "mode,extra,expected_ssl,listener",
    [
        ("prefer", {}, "prefer", False),
        ("disable", {}, False, False),
        ("require", {}, "SSLContext", True),
        ("verify-full", {"DATABASE_SSL_CA_FILE": CA}, "SSLContext", True),
    ],
)
def test_alembic_connects_under_the_shared_transport_arguments(
    tmp_path, mode, extra, expected_ssl, listener
):
    """A TCP URL passes the pre-connect check; the migration engine then dials
    with the helper's `ssl` argument, and carries the D3b listener exactly
    under the strict modes."""
    result, calls = _run_alembic(
        tmp_path, TCP_URL, DATABASE_SSL_MODE=mode, **extra
    )
    assert result.returncode != 0  # the sentinel refuses the connection
    kinds = [c["call"] for c in calls]
    assert ("listener" in kinds) is listener
    connects = [c for c in calls if c["call"] == "connect"]
    assert connects and all(c["ssl"] == expected_ssl for c in connects), calls
    assert "unix" not in kinds


# ── D3b: the per-connection check ──────────────────────────────────────────


class _FakeCursor:
    def __init__(self, row):
        self._row = row
        self.executed: list[str] = []

    def execute(self, sql):
        self.executed.append(sql)

    def fetchone(self):
        return self._row

    def close(self):
        pass


class _FakeDBAPIConnection:
    def __init__(self, row):
        self.cursor_obj = _FakeCursor(row)
        self.closed = False

    def cursor(self):
        return self.cursor_obj

    def close(self):
        self.closed = True


@pytest.mark.parametrize("row", [(False, None, None), None])
def test_the_listener_discards_an_unencrypted_connection(row):
    conn = _FakeDBAPIConnection(row)
    with pytest.raises(ts.DatabaseTransportError):
        ts._verify_new_connection(conn, None)
    assert conn.closed
    assert "pg_stat_ssl" in conn.cursor_obj.executed[0]


def test_the_listener_passes_an_encrypted_connection():
    conn = _FakeDBAPIConnection((True, "TLSv1.3", "TLS_AES_256_GCM_SHA384"))
    ts._verify_new_connection(conn, None)
    assert not conn.closed


def test_install_is_idempotent():
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(TCP_URL)
    assert not ts.strict_transport_listener_installed(engine)
    ts.install_strict_transport_listener(engine)
    ts.install_strict_transport_listener(engine)
    assert ts.strict_transport_listener_installed(engine)


def test_the_application_engine_has_no_listener_under_the_default_mode():
    from src import database

    assert not ts.strict_transport_listener_installed(database.engine)


_ENGINE_PROBE = textwrap.dedent(
    """
    import asyncio, json
    import asyncpg
    seen = {}
    async def _sentinel(*args, **kwargs):
        s = kwargs.get("ssl")
        seen["ssl"] = type(s).__name__ if not isinstance(s, (str, bool)) else s
        if isinstance(s, __import__("ssl").SSLContext):
            seen["check_hostname"] = s.check_hostname
            seen["verify_mode"] = int(s.verify_mode)
        seen["server_settings"] = kwargs.get("server_settings")
        raise ConnectionRefusedError("sentinel")
    asyncpg.connect = _sentinel
    from src import database
    from src.services import transport_security as ts
    seen["listener"] = ts.strict_transport_listener_installed(database.engine)
    async def main():
        try:
            async with database.engine.connect():
                pass
        except Exception as exc:
            seen["error"] = type(exc).__name__
    asyncio.run(main())
    print(json.dumps(seen))
    """
)


@pytest.mark.parametrize(
    "mode,extra,ssl_value,listener",
    [
        ("prefer", {}, "prefer", False),
        ("disable", {}, False, False),
        ("require", {}, "SSLContext", True),
        ("verify-ca", {"DATABASE_SSL_CA_FILE": CA}, "SSLContext", True),
        ("verify-full", {"DATABASE_SSL_CA_FILE": CA}, "SSLContext", True),
    ],
)
def test_the_application_engine_carries_the_helpers_ssl_value(
    tmp_path, mode, extra, ssl_value, listener
):
    result = subprocess.run(
        [sys.executable, "-c", _ENGINE_PROBE],
        cwd=tmp_path,
        env=_subprocess_env(
            tmp_path, DATABASE_URL=TCP_URL, DATABASE_SSL_MODE=mode, **extra
        ),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    seen = json.loads(result.stdout.strip().splitlines()[-1])
    assert seen["ssl"] == ssl_value
    assert seen["listener"] is listener
    # The existing server settings are kept beside the TLS argument.
    assert seen["server_settings"] == {"statement_timeout": "60000"}
    if mode == "verify-full":
        assert seen["check_hostname"] is True
    if mode == "verify-ca":
        assert seen["check_hostname"] is False


# ════════════════════════════════════════════════════════════════════════════
# Database — the contexts and connect arguments (1.9)
# ════════════════════════════════════════════════════════════════════════════


def test_verify_full_context():
    ctx = ts.build_database_ssl_context("verify-full", CA)
    assert ctx.check_hostname is True
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.minimum_version == ssl.TLSVersion.TLSv1_2
    assert len(ctx.get_ca_certs()) == 1


def test_verify_ca_context():
    ctx = ts.build_database_ssl_context("verify-ca", CA)
    assert ctx.check_hostname is False
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.minimum_version == ssl.TLSVersion.TLSv1_2


def test_require_context():
    ctx = ts.build_database_ssl_context("require")
    assert ctx.check_hostname is False
    assert ctx.verify_mode == ssl.CERT_NONE
    assert ctx.minimum_version == ssl.TLSVersion.TLSv1_2


def test_require_ignores_a_stray_home_root_crt(tmp_path, monkeypatch):
    """libpq-style `require` silently verifies when `~/.postgresql/root.crt`
    exists; the explicit context never looks there."""
    home = tmp_path / "home"
    (home / ".postgresql").mkdir(parents=True)
    (home / ".postgresql" / "root.crt").write_bytes(Path(CA).read_bytes())
    monkeypatch.setenv("HOME", str(home))
    args = ts.database_ssl_connect_args(make(database_ssl_mode="require"))
    assert isinstance(args["ssl"], ssl.SSLContext)
    assert args["ssl"].verify_mode == ssl.CERT_NONE
    assert args["ssl"].get_ca_certs() == []


def test_client_certificate_is_loaded_into_the_context():
    # `load_cert_chain` succeeding is the observable: a mismatched pair raises.
    ctx = ts.build_database_ssl_context("require", None, CLIENT_CERT, CLIENT_KEY)
    assert isinstance(ctx, ssl.SSLContext)


def test_a_mismatched_client_pair_fails_to_load(tmp_path):
    with pytest.raises(ValueError, match="DATABASE_SSL_CERT_FILE"):
        ts.build_database_ssl_context("require", None, OTHER_CA, CLIENT_KEY)


@pytest.mark.parametrize(
    "mode,expected", [("disable", False), ("prefer", "prefer")]
)
def test_lax_connect_args_are_asyncpg_values(mode, expected):
    assert ts.database_ssl_connect_args(make(database_ssl_mode=mode)) == {"ssl": expected}


@pytest.mark.parametrize("mode", STRICT)
def test_strict_connect_args_are_a_context_not_a_string(mode):
    args = ts.database_ssl_connect_args(make(**_strict_kw(mode)))
    assert isinstance(args["ssl"], ssl.SSLContext)


# ════════════════════════════════════════════════════════════════════════════
# Embedding — the scheme policy (2.5)
# ════════════════════════════════════════════════════════════════════════════

pytest_transport_defaults = pytest.mark.transport_defaults

ACCEPTED = [
    "https://embeddings.internal.example/v1",
    "http://localhost:11434",
    "http://127.0.0.1:11434",
    "http://127.8.9.10",
    "http://[::1]:11434",
    "http://LOCALHOST.",
]
REFUSED_WITHOUT_OVERRIDE = [
    "http://ollama:11434",
    "http://127.0.0.1.evil.example",
    "http://localhost.evil.example",
    "http://0.0.0.0:11434",
    "http://10.0.0.5:11434",
]
REFUSED_ALWAYS = [
    "http://127.0.0.1@evil.example:11434",
    "https://user:secret@gateway.example/v1",
    "ftp://ollama",
    "ftp://ollama:11434",
    "ollama:11434",
    "file:///tmp/x",
    "http://:11434",
    "http://ollama:99999",
    "http://ollama:0",
]
PARSER_CASES = [
    " https://x.example/v1",
    "https://x.example/v1 ",
    "http://127.0.0.1:11434\n",
    "http://127.0.0.1:11434\r",
    "http://127.0.\t0.1:11434",
    "http://127.0.0.1:11434\x00",
    "http://127.0.0.1:11434\x7f",
]

PROVIDERS = {
    "ollama": ("ollama_url", {}),
    "openai": ("openai_base_url", {"openai_api_key": "sk-test"}),
}


def emb(provider: str, url: str, **kw) -> Settings:
    field, extra = PROVIDERS[provider]
    return make(embedding_provider=provider, **{field: url}, **extra, **kw)


@pytest_transport_defaults
@pytest.mark.parametrize("provider", sorted(PROVIDERS))
@pytest.mark.parametrize("url", ACCEPTED)
def test_accepted_endpoints(provider, url):
    assert emb(provider, url)


@pytest_transport_defaults
@pytest.mark.parametrize("provider", sorted(PROVIDERS))
@pytest.mark.parametrize("url", REFUSED_WITHOUT_OVERRIDE)
def test_plaintext_to_a_non_loopback_host_needs_the_override(provider, url):
    with pytest.raises(ValueError) as info:
        emb(provider, url)
    text = str(info.value)
    assert "EMBEDDING_ALLOW_PLAINTEXT" in text and "https" in text
    assert emb(provider, url, embedding_allow_plaintext=True)


@pytest_transport_defaults
@pytest.mark.parametrize("override", [False, True])
@pytest.mark.parametrize("provider", sorted(PROVIDERS))
@pytest.mark.parametrize("url", REFUSED_ALWAYS + PARSER_CASES)
def test_malformed_endpoints_are_refused_even_with_the_override(provider, url, override):
    with pytest.raises(ValueError) as info:
        emb(provider, url, embedding_allow_plaintext=override)
    text = str(info.value)
    assert "secret" not in text
    assert "evil.example" not in text or "127.0.0.1" not in text


@pytest_transport_defaults
def test_the_default_ollama_url_is_refused_without_the_override():
    with pytest.raises(ValueError, match="EMBEDDING_ALLOW_PLAINTEXT"):
        make()


@pytest_transport_defaults
def test_the_override_from_the_environment_admits_the_default(monkeypatch):
    monkeypatch.setenv("EMBEDDING_ALLOW_PLAINTEXT", "true")
    assert make().embedding_allow_plaintext is True


@pytest_transport_defaults
def test_the_inactive_provider_is_not_validated():
    # ollama active, https: a plaintext OpenAI base URL is never dialled.
    assert make(
        ollama_url="https://ollama.internal.example",
        openai_base_url="http://gateway.example/v1",
    )
    # openai active, https: the default plaintext OLLAMA_URL is never dialled.
    assert make(
        embedding_provider="openai",
        openai_api_key="sk-test",
        openai_base_url="https://api.example.test/v1",
        ollama_url="http://ollama:11434",
    )


@pytest_transport_defaults
def test_sandbox_mode_admits_the_default_without_the_override():
    assert make(mcp_sandbox_mode=True)


@pytest_transport_defaults
def test_sandbox_mode_does_not_excuse_a_broken_ca():
    with pytest.raises(ValueError, match="EMBEDDING_CA_FILE"):
        make(mcp_sandbox_mode=True, embedding_ca_file=NOT_A_CERT)


@pytest_transport_defaults
@pytest.mark.parametrize("provider", sorted(PROVIDERS))
def test_ca_file_checks(provider, tmp_path):
    https = "https://embeddings.internal.example/v1"
    for bad in (str(tmp_path / "absent.pem"), str(tmp_path), NOT_A_CERT):
        with pytest.raises(ValueError, match="EMBEDDING_CA_FILE"):
            emb(provider, https, embedding_ca_file=bad)
    with pytest.raises(ValueError, match="EMBEDDING_CA_FILE"):
        emb(provider, "http://127.0.0.1:11434", embedding_ca_file=CA)
    with pytest.raises(ValueError, match="EMBEDDING_CA_FILE"):
        emb(
            provider,
            "http://ollama:11434",
            embedding_ca_file=CA,
            embedding_allow_plaintext=True,
        )
    assert emb(provider, https, embedding_ca_file=CA).embedding_ssl_context is not None


@pytest_transport_defaults
def test_the_validated_ca_context_is_reused_by_the_factory(monkeypatch):
    import src.config

    s = make(ollama_url="https://ollama.internal.example", embedding_ca_file=CA)
    stored = s.embedding_ssl_context
    assert isinstance(stored, ssl.SSLContext)

    def _no_second_parse(*a, **k):
        raise AssertionError("the CA file was parsed again")

    monkeypatch.setattr(ssl, "create_default_context", _no_second_parse)
    monkeypatch.setattr(src.config, "settings", s)
    client = ts.embedding_http_client(5.0)
    assert client._transport._pool._ssl_context is stored


# ── Differential: the policy follows httpx where `urlsplit` disagrees ───────


def _httpx_view(url):
    try:
        u = httpx.URL(url)
    except Exception:  # noqa: BLE001
        return None
    return (u.scheme, u.host)


def _urlsplit_view(url):
    try:
        u = urlsplit(url)
        return (u.scheme, (u.hostname or ""))
    except Exception:  # noqa: BLE001
        return None


def _policy_accepts(url) -> bool:
    try:
        emb("ollama", url)
    except ValueError:
        return False
    return True


def _httpx_verdict(url) -> bool:
    """What the policy must say, derived from httpx's parse alone."""
    if url != url.strip() or any(ord(c) < 0x20 or ord(c) == 0x7F for c in url):
        return False
    view = _httpx_view(url)
    if view is None:
        return False
    try:
        endpoint = ts.classify_embedding_url(url)
    except ValueError:
        return False
    return endpoint.scheme == "https" or endpoint.is_loopback


@pytest_transport_defaults
def test_differential_parsers():
    urls = ACCEPTED + REFUSED_WITHOUT_OVERRIDE + REFUSED_ALWAYS + PARSER_CASES
    disagreements = [u for u in urls if _httpx_view(u) != _urlsplit_view(u)]
    # The table must actually contain the cases the two parsers split on.
    assert " https://x.example/v1" in disagreements
    assert "http://127.0.0.1:11434\n" in disagreements
    for url in urls:
        assert _policy_accepts(url) is _httpx_verdict(url), repr(url)
    for url in disagreements:
        split = _urlsplit_view(url)
        naive = bool(split) and (
            split[0] == "https"
            or (split[0] == "http" and split[1] in ("localhost", "127.0.0.1", "::1"))
        )
        if naive:
            # `urlsplit` would have approved it; the policy did not.
            assert not _policy_accepts(url), repr(url)


def test_classify_reports_what_the_client_dials():
    ep = ts.classify_embedding_url("http://[::1]:11434/api")
    assert ep == ts.EmbeddingEndpoint("http", "::1", 11434, True)
    assert ts.classify_embedding_url("https://x.example/v1").port == 443
    assert ts.classify_embedding_url("http://127.0.0.1.evil.example").is_loopback is False


def test_a_refused_setting_is_not_echoed_into_the_startup_traceback():
    """Codex impl r1: pydantic appends the raw input to a ValidationError.

    Imported in a clean subprocess with a short credential-bearing URL, so the
    assertion does not depend on repr truncation hiding it.
    """
    import subprocess
    import sys

    env = {
        "PATH": os.environ.get("PATH", ""),
        "SECRET_KEY": "test",
        "EMBEDDING_PROVIDER": "openai",
        "OPENAI_API_KEY": "sk-test",
        "OPENAI_BASE_URL": "https://u:s3cr3tpw@x",
    }
    result = subprocess.run(
        [sys.executable, "-c", "import src.config"],
        cwd=str(Path(__file__).resolve().parent.parent),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode != 0, "the userinfo URL must be refused"
    assert "s3cr3tpw" not in result.stdout + result.stderr
