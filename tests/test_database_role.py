"""The server refuses a superuser session and a placeholder DB password (#324).

Spec: openspec/changes/compose-db-roles (database-least-privilege), design D2
and D5. `check_database_role()` runs against a fake `async_session` here; the
non-superuser path against a real server is
tests/integration/test_nonsuperuser_migrations_pg.py.
"""
from __future__ import annotations

import logging

import pytest

import src.config
import src.database
import src.main as main
from src.config import Settings
from src.services import database_role

SECRET = "a-real-test-secret-not-a-placeholder"
ROLE_LOGGER = "src.services.database_role"


class _Result:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _Session:
    def __init__(self, row):
        self._row = row
        self.statements: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, stmt):
        self.statements.append(str(stmt))
        return _Result(self._row)


@pytest.fixture
def session(monkeypatch):
    state = {}

    def configure(row):
        state["s"] = _Session(row)
        monkeypatch.setattr(src.database, "async_session", lambda: state["s"])
        return state["s"]

    return configure


def _role_records(caplog):
    return [r for r in caplog.records if r.name == ROLE_LOGGER]


# ── check_database_role ────────────────────────────────────────────────────


async def test_superuser_session_is_refused_by_default(session, monkeypatch, caplog):
    monkeypatch.setattr(src.config.settings, "database_allow_superuser", False)
    s = session(("obsidian_mcp", True))
    with caplog.at_level(logging.DEBUG), pytest.raises(SystemExit) as info:
        await database_role.check_database_role()
    assert info.value.code == 1
    assert any("rolsuper" in stmt and "current_user" in stmt for stmt in s.statements)
    records = _role_records(caplog)
    assert len(records) == 1
    assert records[0].levelno == logging.CRITICAL
    message = records[0].getMessage()
    assert "obsidian_mcp" in message
    assert "DATABASE_ALLOW_SUPERUSER" in message
    assert "Upgrading: split database roles" in message


async def test_opt_out_allows_a_superuser_with_one_warning(session, monkeypatch, caplog):
    monkeypatch.setattr(src.config.settings, "database_allow_superuser", True)
    session(("postgres", True))
    with caplog.at_level(logging.DEBUG):
        await database_role.check_database_role()
    records = _role_records(caplog)
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert "postgres" in records[0].getMessage()


@pytest.mark.parametrize("allow", [False, True])
async def test_non_superuser_session_is_silent(session, monkeypatch, caplog, allow):
    monkeypatch.setattr(src.config.settings, "database_allow_superuser", allow)
    session(("obsidian_mcp", False))
    with caplog.at_level(logging.DEBUG):
        await database_role.check_database_role()
    assert _role_records(caplog) == []


async def test_missing_role_row_fails_closed(session, monkeypatch, caplog):
    monkeypatch.setattr(src.config.settings, "database_allow_superuser", True)
    session(None)
    with caplog.at_level(logging.DEBUG), pytest.raises(SystemExit):
        await database_role.check_database_role()


# ── In the lifespan ────────────────────────────────────────────────────────


class _Stop(Exception):
    pass


async def test_lifespan_refuses_before_any_later_startup_step(session, monkeypatch, caplog):
    later: list[str] = []
    monkeypatch.setattr(main.settings, "mcp_sandbox_mode", False, raising=False)
    monkeypatch.setattr(main.settings, "database_allow_superuser", False)
    monkeypatch.setattr(main, "_check_openat2_support", lambda: None)
    monkeypatch.setattr(main, "_check_mount_identity_support", lambda: None)

    async def _noop():
        return None

    async def _dim():
        later.append("embedding_dim")
        raise _Stop

    monkeypatch.setattr(main, "check_database_transport", _noop)
    monkeypatch.setattr(main, "log_embedding_transport", lambda: later.append("embedding_transport"))
    monkeypatch.setattr(main, "_check_embedding_dim", _dim)
    session(("obsidian_mcp", True))
    with caplog.at_level(logging.DEBUG), pytest.raises(SystemExit):
        async with main.lifespan(main.app):
            pytest.fail("served as a superuser")
    assert later == []


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


async def test_sandbox_mode_issues_no_rolsuper_query(monkeypatch):
    called: list[str] = []
    monkeypatch.setattr(main.settings, "mcp_sandbox_mode", True, raising=False)

    async def _role():
        called.append("role")

    async def _noop():
        return None

    monkeypatch.setattr(main, "check_database_role", _role)
    monkeypatch.setattr(main, "_publish_first_root_snapshot", _noop)
    monkeypatch.setattr(main, "mcp", _FakeMcp())
    async with main.lifespan(main.app):
        pass
    assert called == []


# ── The leftover admin password warning ────────────────────────────────────


def test_admin_password_in_environment_warns_once_without_the_value(monkeypatch, caplog):
    value = "leftover0admin0password0" + "9" * 8
    monkeypatch.setenv("POSTGRES_PASSWORD", value)
    with caplog.at_level(logging.DEBUG):
        database_role.warn_admin_password_in_environment()
    records = _role_records(caplog)
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert "postgres.env" in records[0].getMessage()
    assert value not in records[0].getMessage()


@pytest.mark.parametrize("present", [False, True])
def test_absent_or_blank_admin_password_is_silent(monkeypatch, caplog, present):
    # The bundles set POSTGRES_PASSWORD="" in the app service (design D2).
    if present:
        monkeypatch.setenv("POSTGRES_PASSWORD", "")
    else:
        monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    with caplog.at_level(logging.DEBUG):
        database_role.warn_admin_password_in_environment()
    assert _role_records(caplog) == []


async def test_lifespan_warns_about_a_leftover_admin_password_and_continues(monkeypatch, caplog):
    value = "leftover0admin0password0" + "8" * 8
    monkeypatch.setenv("POSTGRES_PASSWORD", value)
    monkeypatch.setattr(main.settings, "mcp_sandbox_mode", True, raising=False)

    async def _noop():
        return None

    monkeypatch.setattr(main, "_publish_first_root_snapshot", _noop)
    monkeypatch.setattr(main, "mcp", _FakeMcp())
    with caplog.at_level(logging.DEBUG):
        async with main.lifespan(main.app):
            pass
    records = [r for r in _role_records(caplog) if "postgres.env" in r.getMessage()]
    assert len(records) == 1
    assert all(value not in r.getMessage() for r in caplog.records)


# ── Settings: placeholder password, default URL, opt-out ───────────────────


def _settings(**kw) -> Settings:
    kw.setdefault("secret_key", SECRET)
    return Settings(_env_file=None, **kw)


@pytest.mark.parametrize("password", ["CHANGE_ME", "changeme", "%20Change_Me%20", "change_me"])
def test_placeholder_database_password_is_refused(password):
    url = f"postgresql+asyncpg://obsidian_mcp:{password}@postgres:5432/obsidian_mcp"
    with pytest.raises(ValueError) as info:
        _settings(database_url=url)
    text = str(info.value)
    assert "DATABASE_URL" in text
    assert "openssl rand -hex 32" in text
    assert url not in text
    assert "obsidian_mcp:" not in text


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+asyncpg://obsidian_mcp@postgres:5432/obsidian_mcp",
        "postgresql+asyncpg://obsidian_mcp:@postgres:5432/obsidian_mcp",
        "postgresql+asyncpg://obsidian_mcp:0123456789abcdef0123456789abcdef@postgres:5432/obsidian_mcp",
        "postgresql+asyncpg://obsidian_mcp:changemeplease0000000000@postgres:5432/obsidian_mcp",
    ],
    ids=["no-password", "empty-password", "hex-32", "placeholder-prefix"],
)
def test_real_or_absent_passwords_are_accepted(url):
    assert _settings(database_url=url).database_url == url


def test_default_database_url_carries_no_credential(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from urllib.parse import urlsplit

    s = _settings()
    assert urlsplit(s.database_url).password is None
    assert "changeme" not in s.database_url.lower()


@pytest.mark.parametrize("raw,expected", [("true", True), ("false", False), ("1", True)])
def test_allow_superuser_setting_parses(monkeypatch, raw, expected):
    monkeypatch.setenv("DATABASE_ALLOW_SUPERUSER", raw)
    assert _settings().database_allow_superuser is expected


def test_allow_superuser_defaults_off(monkeypatch):
    monkeypatch.delenv("DATABASE_ALLOW_SUPERUSER", raising=False)
    assert _settings().database_allow_superuser is False


# ── alembic/env.py's authentication hint (design D6) ───────────────────────


class _DriverError(Exception):
    def __init__(self, sqlstate):
        super().__init__("driver error")
        self.sqlstate = sqlstate


def _wrapped(inner: BaseException) -> BaseException:
    """The shape SQLAlchemy produces: its own error, the driver's as `.orig`."""
    from sqlalchemy.exc import OperationalError

    try:
        try:
            raise inner
        except Exception as e:
            raise OperationalError("connect", {}, e) from e
    except OperationalError as outer:
        return outer


@pytest.mark.parametrize("code", ["28P01", "28000"])
def test_authentication_sqlstate_is_found_through_the_wrapper(code):
    assert database_role.is_authentication_failure(_wrapped(_DriverError(code)))


def test_authentication_sqlstate_found_on_the_context_chain_only():
    outer = RuntimeError("outer")
    outer.__context__ = _DriverError("28P01")
    assert database_role.is_authentication_failure(outer)


@pytest.mark.parametrize("code", ["3D000", "08006", None])
def test_other_failures_are_not_authentication(code):
    assert not database_role.is_authentication_failure(_wrapped(_DriverError(code)))
    assert not database_role.is_authentication_failure(ConnectionRefusedError())


def test_cyclic_chain_terminates():
    a, b = RuntimeError("a"), RuntimeError("b")
    a.__context__, b.__context__ = b, a
    assert not database_role.is_authentication_failure(a)


def test_hint_names_the_script_and_the_doc_section():
    hint = database_role.authentication_hint("obsidian_mcp")
    assert "\n" not in hint
    assert "role obsidian_mcp" in hint
    assert "docker/upgrade-split-db-roles.sql" in hint
    assert "Upgrading: split database roles" in hint
