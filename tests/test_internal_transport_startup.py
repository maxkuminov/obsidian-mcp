"""The startup transport assertion and report (#184, #185; tasks 1.10, 2.7).

`check_database_transport()` runs against a mocked `async_session`, so every
branch of design D4 is exercised without a database; the real `pg_stat_ssl`
behaviour against a server is `tests/integration/test_internal_transport_pg.py`.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

import src.config
import src.database
import src.main as main
from src.config import Settings
from src.services import security_events
from src.services import transport_security as ts

SECRET = "a-real-test-secret-not-a-placeholder"
CA = str(Path(__file__).resolve().parent / "fixtures" / "transport_tls" / "ca.pem")
EVENT = "internal_transport_plaintext"


# ── A fake session ──────────────────────────────────────────────────────────


class _Result:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _Session:
    def __init__(self, row, raises):
        self._row = row
        self._raises = raises
        self.statements: list[str] = []

    async def __aenter__(self):
        if self._raises is not None:
            raise self._raises
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, stmt):
        self.statements.append(str(stmt))
        return _Result(self._row)


@pytest.fixture
def session(monkeypatch):
    """Install a fake `src.database.async_session`; returns a configurator."""
    state = {}

    def configure(row=None, raises=None):
        state["session"] = _Session(row, raises)
        monkeypatch.setattr(src.database, "async_session", lambda: state["session"])
        return state["session"]

    return configure


@pytest.fixture
def mode(monkeypatch):
    def set_mode(value: str):
        monkeypatch.setattr(src.config.settings, "database_ssl_mode", value)

    return set_mode


@pytest.fixture
def events():
    """Every `security_events` record, strict about fields, never suppressed."""
    records: list[logging.LogRecord] = []

    class _Handler(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _Handler(level=logging.DEBUG)
    logger = logging.getLogger("security_events")
    logger.addHandler(handler)
    old_level = logger.level
    logger.setLevel(logging.DEBUG)
    security_events.reset_state()
    try:
        with security_events.strict_fields(), security_events.suppression_disabled():
            yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)
        security_events.reset_state()


def _plaintext(records):
    return [r for r in records if r.getMessage() == EVENT]


def _transport_lines(caplog, prefix):
    return [r for r in caplog.records if r.getMessage().startswith(prefix)]


# ════════════════════════════════════════════════════════════════════════════
# Database hop (1.10)
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("strict", ["require", "verify-ca", "verify-full"])
async def test_strict_mode_with_a_plaintext_session_exits(session, mode, events, caplog, strict):
    mode(strict)
    s = session(row=(False, None, None))
    with caplog.at_level(logging.INFO), pytest.raises(SystemExit) as info:
        await ts.check_database_transport()
    assert info.value.code == 1
    assert "pg_stat_ssl" in s.statements[0]
    critical = [r for r in caplog.records if r.levelno == logging.CRITICAL]
    assert critical and strict in critical[0].getMessage()
    assert _plaintext(events) == []


async def test_strict_mode_with_no_row_exits(session, mode, events, caplog):
    mode("require")
    session(row=None)
    with caplog.at_level(logging.INFO), pytest.raises(SystemExit):
        await ts.check_database_transport()
    assert any(r.levelno == logging.CRITICAL for r in caplog.records)


async def test_strict_mode_when_the_connection_fails_exits_naming_the_class_only(
    session, mode, caplog
):
    mode("verify-full")
    session(raises=ConnectionError("postgresql://app:hunter2@db.example/app refused"))
    with caplog.at_level(logging.INFO), pytest.raises(SystemExit):
        await ts.check_database_transport()
    critical = [r for r in caplog.records if r.levelno == logging.CRITICAL]
    assert len(critical) == 1
    text = critical[0].getMessage()
    assert "ConnectionError" in text and "verify-full" in text
    assert "hunter2" not in text and "db.example" not in text


async def test_a_listener_refusal_is_a_connection_failure(session, mode, caplog):
    mode("require")
    session(raises=ts.DatabaseTransportError("not encrypted"))
    with caplog.at_level(logging.INFO), pytest.raises(SystemExit):
        await ts.check_database_transport()


async def test_lax_mode_reraises_a_connection_failure(session, mode):
    mode("prefer")
    session(raises=ConnectionError("down"))
    with pytest.raises(ConnectionError):
        await ts.check_database_transport()


@pytest.mark.parametrize("lax", ["prefer", "disable"])
async def test_lax_mode_with_a_plaintext_session_warns_once(session, mode, events, caplog, lax):
    mode(lax)
    session(row=(False, None, None))
    with caplog.at_level(logging.INFO):
        await ts.check_database_transport()
    records = _plaintext(events)
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert records[0].reason == "database"
    assert records[0].outcome == lax
    lines = _transport_lines(caplog, "Database transport:")
    assert len(lines) == 1
    text = lines[0].getMessage()
    assert f"mode={lax}" in text and "encrypted=False" in text
    assert "server_verified=False" in text


async def test_lax_mode_missing_row_is_treated_as_plaintext(session, mode, events):
    mode("prefer")
    session(row=None)
    await ts.check_database_transport()
    assert len(_plaintext(events)) == 1


@pytest.mark.parametrize(
    "db_mode,verified",
    [("prefer", False), ("require", False), ("verify-ca", True), ("verify-full", True)],
)
async def test_an_encrypted_session_reports_its_version(
    session, mode, events, caplog, db_mode, verified
):
    mode(db_mode)
    session(row=(True, "TLSv1.3", "TLS_AES_256_GCM_SHA384"))
    with caplog.at_level(logging.INFO):
        await ts.check_database_transport()
    assert _plaintext(events) == []
    (line,) = _transport_lines(caplog, "Database transport:")
    text = line.getMessage()
    assert "encrypted=True" in text and "tls_version=TLSv1.3" in text
    assert f"server_verified={verified}" in text


# ── Lifespan ordering ───────────────────────────────────────────────────────


class _StopLifespan(Exception):
    pass


async def test_the_probe_runs_first_and_the_report_right_after(monkeypatch):
    order: list[str] = []
    monkeypatch.setattr(main.settings, "mcp_sandbox_mode", False, raising=False)
    monkeypatch.setattr(main, "_check_openat2_support", lambda: order.append("openat2"))
    monkeypatch.setattr(
        main, "_check_mount_identity_support", lambda: order.append("mount")
    )

    async def _db():
        order.append("database_transport")

    async def _dim():
        order.append("embedding_dim")
        raise _StopLifespan

    monkeypatch.setattr(main, "check_database_transport", _db)
    monkeypatch.setattr(
        main, "log_embedding_transport", lambda: order.append("embedding_transport")
    )
    monkeypatch.setattr(main, "_check_embedding_dim", _dim)
    with pytest.raises(_StopLifespan):
        async with main.lifespan(main.app):
            pass
    assert order == [
        "openat2",
        "mount",
        "database_transport",
        "embedding_transport",
        "embedding_dim",
    ]


class _FakeSessionManager:
    def run(self):
        class _CM:
            async def __aenter__(self_inner):
                return None

            async def __aexit__(self_inner, *exc):
                return False

        return _CM()


class _FakeMcp:
    session_manager = _FakeSessionManager()


async def test_sandbox_mode_runs_no_probe(monkeypatch):
    called: list[str] = []
    monkeypatch.setattr(main.settings, "mcp_sandbox_mode", True, raising=False)

    async def _db():
        called.append("database_transport")

    async def _noop():
        return None

    monkeypatch.setattr(main, "check_database_transport", _db)
    monkeypatch.setattr(
        main, "log_embedding_transport", lambda: called.append("embedding_transport")
    )
    monkeypatch.setattr(main, "_publish_first_root_snapshot", _noop)
    monkeypatch.setattr(main, "mcp", _FakeMcp())
    async with main.lifespan(main.app):
        pass
    assert called == []


# ════════════════════════════════════════════════════════════════════════════
# Embedding hop (2.7)
# ════════════════════════════════════════════════════════════════════════════


def _use_settings(monkeypatch, **kw) -> Settings:
    kw.setdefault("secret_key", SECRET)
    s = Settings(_env_file=None, **kw)
    monkeypatch.setattr(src.config, "settings", s)
    return s


def test_plaintext_embedding_hop_admitted_by_the_override(monkeypatch, events, caplog):
    _use_settings(
        monkeypatch, ollama_url="http://ollama:11434", embedding_allow_plaintext=True
    )
    with caplog.at_level(logging.INFO):
        ts.log_embedding_transport()
    (line,) = _transport_lines(caplog, "Embedding transport:")
    text = line.getMessage()
    for part in (
        "provider=ollama",
        "scheme=http",
        "host=ollama",
        "port=11434",
        "verify=n/a",
        "plaintext_override=True",
    ):
        assert part in text, text
    (record,) = _plaintext(events)
    assert record.reason == "embedding" and record.outcome == "override"
    # The serialised record carries no endpoint identity.
    from src.logging_setup import StructuredFormatter

    serialised = StructuredFormatter("json").format(record)
    for forbidden in ("ollama", "11434", "http://"):
        assert forbidden not in serialised, serialised
    assert json.loads(serialised)["msg"] == EVENT


def test_loopback_plaintext_logs_the_line_and_emits_nothing(monkeypatch, events, caplog):
    _use_settings(monkeypatch, ollama_url="http://127.0.0.1:11434")
    with caplog.at_level(logging.INFO):
        ts.log_embedding_transport()
    (line,) = _transport_lines(caplog, "Embedding transport:")
    assert "plaintext_override=False" in line.getMessage()
    assert _plaintext(events) == []


def test_https_with_a_ca_file_reports_ca_file(monkeypatch, events, caplog):
    _use_settings(
        monkeypatch,
        embedding_provider="openai",
        openai_api_key="sk-test",
        openai_base_url="https://gateway.internal.example/v1",
        embedding_ca_file=CA,
    )
    with caplog.at_level(logging.INFO):
        ts.log_embedding_transport()
    (line,) = _transport_lines(caplog, "Embedding transport:")
    text = line.getMessage()
    assert "provider=openai" in text and "verify=ca-file" in text and "port=443" in text
    assert _plaintext(events) == []


def test_https_without_a_ca_file_reports_certifi(monkeypatch, events, caplog):
    _use_settings(monkeypatch, ollama_url="https://ollama.internal.example")
    with caplog.at_level(logging.INFO):
        ts.log_embedding_transport()
    (line,) = _transport_lines(caplog, "Embedding transport:")
    assert "verify=certifi" in line.getMessage()


def test_the_line_never_carries_path_query_or_userinfo(monkeypatch, events, caplog):
    _use_settings(
        monkeypatch,
        embedding_provider="openai",
        openai_api_key="sk-test",
        openai_base_url="https://gateway.internal.example/v1/deployments/x?api-version=2024",
    )
    with caplog.at_level(logging.INFO):
        ts.log_embedding_transport()
    (line,) = _transport_lines(caplog, "Embedding transport:")
    text = line.getMessage()
    assert "/v1" not in text and "deployments" not in text and "api-version" not in text
