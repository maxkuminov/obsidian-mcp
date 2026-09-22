"""`PANEL_CSP`: the panel policy's boot-validated rollback lever (#195).

`enforce` is the default; `report-only` and `off` exist so a policy that breaks
a panel control can be backed out with one `.env` line and a recreate. Any
other value refuses startup, and the effective mode is logged once when the
lifespan starts — at WARNING as well when it is not `enforce`, so a forgotten
rollback is visible in the logs.
"""
from __future__ import annotations

import logging

import pytest
from pydantic import ValidationError

from src import main
from src.config import Settings


def _settings(**kwargs) -> Settings:
    # An explicit secret: the conftest scrubs the environment only while the
    # singleton is built, so a bare `Settings()` here would trip the
    # placeholder-secret guard instead of reaching `panel_csp`.
    kwargs.setdefault("secret_key", "test-" + "k" * 32)
    return Settings(_env_file=None, **kwargs)


def test_the_default_is_enforce(monkeypatch):
    monkeypatch.delenv("PANEL_CSP", raising=False)
    assert _settings().panel_csp == "enforce"


@pytest.mark.parametrize("value", ["enforce", "report-only", "off"])
def test_each_accepted_value(value):
    assert _settings(panel_csp=value).panel_csp == value


@pytest.mark.parametrize("value", ["enforce", "report-only", "off"])
def test_each_accepted_value_from_the_environment(value, monkeypatch):
    monkeypatch.setenv("PANEL_CSP", value)
    assert _settings().panel_csp == value


@pytest.mark.parametrize("value", ["strict", "", "on", "true", "report_only", "Enforce "])
def test_an_unknown_value_refuses_construction(value, monkeypatch):
    monkeypatch.setenv("PANEL_CSP", value)
    with pytest.raises(ValidationError) as excinfo:
        _settings()
    assert "panel_csp" in str(excinfo.value)


# ── the startup log ─────────────────────────────────────────────────────────


class _Capture(logging.Handler):
    """`src.main`'s logger does not propagate (`configure_logging` owns the
    handlers), so `caplog` would see nothing."""

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


class _StubSessionManager:
    def run(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _StubMcp:
    session_manager = _StubSessionManager()


async def _start(mode, monkeypatch) -> list[logging.LogRecord]:
    """Run the real lifespan (sandbox branch, nothing external) and capture."""
    from src.services import vault_overlap

    monkeypatch.setattr(main.settings, "panel_csp", mode)
    monkeypatch.setattr(main.settings, "mcp_sandbox_mode", True, raising=False)
    monkeypatch.setattr(vault_overlap.settings, "mcp_sandbox_mode", True, raising=False)
    monkeypatch.setattr(main, "mcp", _StubMcp())

    logger = logging.getLogger("src.main")
    handler = _Capture()
    level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        cm = main.lifespan(object())
        await cm.__aenter__()
        await cm.__aexit__(None, None, None)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(level)
    return [r for r in handler.records if "PANEL_CSP" in r.getMessage()]


async def test_enforce_is_logged_once_at_info(monkeypatch):
    records = await _start("enforce", monkeypatch)
    assert [(r.levelno, r.getMessage()) for r in records] == [
        (logging.INFO, "Panel CSP: PANEL_CSP = enforce"),
    ]


@pytest.mark.parametrize("mode", ["report-only", "off"])
async def test_a_non_enforcing_mode_also_warns(mode, monkeypatch):
    records = await _start(mode, monkeypatch)
    assert [r.levelno for r in records] == [logging.INFO, logging.WARNING]
    assert records[0].getMessage() == f"Panel CSP: PANEL_CSP = {mode}"
    assert mode in records[1].getMessage()
    assert "not enforced" in records[1].getMessage()
