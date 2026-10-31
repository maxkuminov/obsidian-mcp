"""The reranker's settings, startup probe, re-probe, process state, `/health`
and transport line (#367; design D1, D3, D8; tasks 1.3, 3.x, 7.3)."""
from __future__ import annotations

import asyncio
import json
import logging

import pydantic
import pytest

from tests._rerank_support import fake, fake_server, no_reranker  # noqa: F401
from src.config import Settings, settings
from src.services import rerank, security_events
from src.services import transport_security as ts
from tests.fixtures.fake_reranker import FakeReranker

SECRET = "a-real-test-secret-not-a-placeholder"


def _settings(**kw) -> Settings:
    kw.setdefault("secret_key", SECRET)
    return Settings(_env_file=None, **kw)


# ── 1.3 settings and validation ─────────────────────────────────────────────


def test_the_defaults():
    s = _settings()
    assert s.rerank_url == "" and s.rerank_model == ""
    assert s.rerank_timeout_ms == 1500 and s.rerank_allow_plaintext is False


@pytest.mark.parametrize("value, ok", [(100, True), (10000, True), (20000, True),
                                       (30000, True), (50, False), (99, False),
                                       (30001, False)])
def test_the_timeout_range(value, ok):
    # #402: the ceiling is 30,000 so the reference deployment's 20 s is accepted.
    if ok:
        assert _settings(rerank_timeout_ms=value).rerank_timeout_ms == value
    else:
        with pytest.raises(pydantic.ValidationError):
            _settings(rerank_timeout_ms=value)


@pytest.mark.parametrize("value, ok", [(50, True), (100, True), (200, True),
                                       (49, False), (201, False)])
def test_the_document_budget_range(value, ok):
    # #402: RERANK_MAX_DOCUMENTS, default 100, range 50–200.
    assert _settings().rerank_max_documents == 100
    if ok:
        assert _settings(rerank_max_documents=value).rerank_max_documents == value
    else:
        with pytest.raises(pydantic.ValidationError):
            _settings(rerank_max_documents=value)


@pytest.mark.parametrize("model", ["m" * 201, "bge\nreranker", "bge\x7f"])
def test_an_invalid_model_refuses_startup(model):
    with pytest.raises(pydantic.ValidationError):
        _settings(rerank_model=model)


@pytest.mark.parametrize("url", [
    " http://127.0.0.1:8081/v1/rerank", "http://127.0.0.1:8081/v1/rerank ",
    "http://127.0.0.1:8081/v1/re\nrank", "ftp://127.0.0.1/v1/rerank", "http:///v1/rerank",
    "http://127.0.0.1:99999/v1/rerank", "not a url",
])
def test_malformed_urls_refuse_startup(url):
    with pytest.raises(pydantic.ValidationError) as e:
        _settings(rerank_url=url)
    assert "RERANK_URL" in str(e.value)


def test_credentials_in_the_url_are_refused_without_echo():
    with pytest.raises(pydantic.ValidationError) as e:
        _settings(rerank_url="https://user:secret@rerank.internal/v1/rerank")
    assert "RERANK_URL" in str(e.value) and "secret" not in str(e.value)


def test_plaintext_to_the_lan_needs_its_own_override():
    with pytest.raises(pydantic.ValidationError) as e:
        _settings(rerank_url="http://gpu-host:8081/v1/rerank", embedding_allow_plaintext=True,
                  rerank_allow_plaintext=False)
    msg = str(e.value)
    assert "RERANK_URL" in msg and "RERANK_ALLOW_PLAINTEXT" in msg
    assert "EMBEDDING_ALLOW_PLAINTEXT=true" not in msg
    s = _settings(rerank_url="http://gpu-host:8081/v1/rerank", rerank_allow_plaintext=True)
    assert s.rerank_allow_plaintext is True


@pytest.mark.parametrize("url", ["http://127.0.0.1:8081/v1/rerank", "http://[::1]:8081/r",
                                 "http://localhost:8081/v1/rerank",
                                 "https://10.0.0.5/v1/rerank", "https://169.254.1.1/v1/rerank"])
def test_loopback_plaintext_and_private_https_are_accepted(url):
    assert _settings(rerank_url=url).rerank_url == url


def test_sandbox_mode_skips_the_url_policy():
    s = _settings(rerank_url="http://gpu-host:8081/v1/rerank", mcp_sandbox_mode=True)
    assert s.rerank_url


def test_the_embedding_plaintext_message_is_unchanged():
    with pytest.raises(pydantic.ValidationError) as e:
        _settings(ollama_url="http://ollama:11434", embedding_allow_plaintext=False)
    assert "set EMBEDDING_ALLOW_PLAINTEXT=true to acknowledge" in str(e.value)
    assert "every chunk and query would cross" in str(e.value)


# ── 3.1 the probe ───────────────────────────────────────────────────────────


async def test_a_correct_reranker_passes_in_both_orders(fake, caplog):
    rerank._reset_state_for_tests()
    with caplog.at_level(logging.INFO, logger="src.services.rerank"):
        assert await rerank.startup_probe() is True
    assert rerank.health_state() == "enabled"
    docs = [r["documents"] for r in fake.requests]
    assert docs == [[rerank.PROBE_RELEVANT, rerank.PROBE_IRRELEVANT],
                    [rerank.PROBE_IRRELEVANT, rerank.PROBE_RELEVANT]]
    assert all(r["query"] == rerank.PROBE_QUERY and r["top_n"] == 2 for r in fake.requests)
    assert [r.levelname for r in caplog.records if r.name == "src.services.rerank"] == ["INFO"]


@pytest.mark.parametrize("mode, code", [
    ("position", rerank.PROBE_WRONG_ORDER), ("inverted", rerank.PROBE_WRONG_ORDER),
    ("tie", rerank.PROBE_TIE), ("not_json", rerank.MALFORMED_RESPONSE),
    ("short", rerank.COUNT_MISMATCH), ("nan", rerank.NON_FINITE_SCORE),
    ("status:500", rerank.HTTP_ERROR), ("oversize", rerank.OVERSIZE_RESPONSE),
    ("bool_index", rerank.BAD_INDEX), ("redirect", rerank.HTTP_ERROR),
])
async def test_probe_failures_carry_one_code(fake, mode, code):
    fake.mode = mode
    assert await rerank.probe_attempt(5) == code


async def test_a_position_only_scorer_loses_the_second_order(fake):
    fake.mode = "position"
    assert await rerank.probe_attempt(5) == rerank.PROBE_WRONG_ORDER
    assert len(fake.requests) == 2, "the first order passes; the second exposes it"


@pytest.mark.parametrize("mode", ["inverted", "tie", "not_json", "sleep:2000", "unreachable"])
async def test_a_failed_startup_probe_disables_with_one_critical_and_never_exits(
        fake, monkeypatch, caplog, mode):
    rerank._reset_state_for_tests()
    monkeypatch.setattr(rerank, "RERANK_PROBE_TIMEOUT_S", 0.3)
    if mode == "unreachable":
        from tests.fixtures.fake_reranker import free_port

        monkeypatch.setattr(settings, "rerank_url",
                            f"http://127.0.0.1:{free_port()}/v1/rerank", raising=False)
    else:
        fake.mode = mode
    with caplog.at_level(logging.DEBUG, logger="src.services.rerank"):
        assert await rerank.startup_probe() is False   # returns; no SystemExit
    criticals = [r for r in caplog.records if r.levelno == logging.CRITICAL]
    assert len(criticals) == 1
    assert settings.rerank_url not in criticals[0].getMessage()
    assert rerank.health_state() == "disabled"
    out = await rerank.rerank_candidates("q", [("t", "x")])
    assert out.reason == rerank.PROBE_FAILED


def test_no_exit_in_the_rerank_module():
    import inspect

    assert "sys.exit" not in inspect.getsource(rerank)


# ── 3.1b the re-probe ───────────────────────────────────────────────────────


async def test_the_reranker_starting_after_the_server_is_re_enabled(monkeypatch, caplog):
    late = FakeReranker()   # not started: nothing listens yet
    monkeypatch.setattr(settings, "rerank_url", late.url, raising=False)
    monkeypatch.setattr(settings, "rerank_timeout_ms", 1500, raising=False)
    monkeypatch.setattr(rerank, "_client", None)
    rerank._reset_state_for_tests()
    intervals: list[float] = []

    async def fake_sleep(seconds):
        intervals.append(seconds)
        # The reranker comes up during the first wait (the scenario's "ready
        # 30 seconds later"), so the 60 s re-probe passes.
        if len(intervals) == 1:
            late.start()

    try:
        with caplog.at_level(logging.DEBUG, logger="src.services.rerank"):
            assert await rerank.startup_probe() is False
            assert rerank.health_state() == "disabled"
            await rerank.reprobe_loop(sleep=fake_sleep)
        assert intervals == [60]
        assert rerank.health_state() == "enabled"
        levels = [r.levelname for r in caplog.records if r.name == "src.services.rerank"]
        assert levels.count("CRITICAL") == 1 and levels.count("INFO") == 1
        assert levels[-1] == "INFO"
        out = await rerank.rerank_candidates("bravo", [("a", "bravo"), ("b", "x")])
        assert out.reranked is True
    finally:
        await rerank.close_rerank_client()
        late.stop()
        rerank._reset_state_for_tests()


async def test_the_backoff_schedule_with_no_further_critical(monkeypatch, caplog):
    from tests.fixtures.fake_reranker import free_port

    monkeypatch.setattr(settings, "rerank_url", f"http://127.0.0.1:{free_port()}/v1/rerank",
                        raising=False)
    monkeypatch.setattr(rerank, "_client", None)
    rerank._reset_state_for_tests()
    intervals: list[float] = []

    async def fake_sleep(seconds):
        intervals.append(seconds)
        if len(intervals) > 7:
            raise asyncio.CancelledError

    with caplog.at_level(logging.DEBUG, logger="src.services.rerank"):
        with pytest.raises(asyncio.CancelledError):
            await rerank.reprobe_loop(sleep=fake_sleep)
    await rerank.close_rerank_client()
    assert intervals[:7] == [60, 120, 240, 480, 600, 600, 600]
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert rerank.health_state() == "disabled"


async def test_shutdown_cancels_the_re_probe_promptly(monkeypatch):
    monkeypatch.setattr(settings, "rerank_url", "http://127.0.0.1:1/v1/rerank", raising=False)
    task = asyncio.create_task(rerank.reprobe_loop())
    await asyncio.sleep(0.05)
    loop = asyncio.get_running_loop()
    started = loop.time()
    await rerank.stop_task(task)
    assert task.done() and loop.time() - started < 1


async def test_the_re_probe_opens_no_session(fake, monkeypatch):
    import src.database

    def forbidden(*_a, **_k):
        raise AssertionError("the re-probe opened a database session")

    monkeypatch.setattr(src.database, "async_session", forbidden)
    rerank._reset_state_for_tests()

    async def no_sleep(_s):
        return None

    await rerank.reprobe_loop(sleep=no_sleep)
    assert rerank.health_state() == "enabled"


# ── sandbox, /health, the transport line ────────────────────────────────────


def test_sandbox_marks_disabled_or_absent(monkeypatch):
    rerank._reset_state_for_tests()
    monkeypatch.setattr(settings, "rerank_url", "http://127.0.0.1:8081/v1/rerank", raising=False)
    rerank.mark_sandbox()
    assert rerank.health_state() == "disabled"
    out = asyncio.run(rerank.rerank_candidates("q", [("t", "x")]))
    assert out.reason == rerank.PROBE_SKIPPED
    rerank._reset_state_for_tests()
    monkeypatch.setattr(settings, "rerank_url", "", raising=False)
    rerank.mark_sandbox()
    assert rerank.health_state() == "absent"


@pytest.mark.parametrize("state, expected", [("enabled", "enabled"), ("disabled", "disabled"),
                                             ("absent", "absent")])
async def test_health_reports_the_state_without_identity(monkeypatch, state, expected):
    from src import main

    url = "http://127.0.0.1:8081/v1/rerank"
    monkeypatch.setattr(settings, "rerank_url", "" if state == "absent" else url, raising=False)
    rerank._reset_state_for_tests()
    if state == "enabled":
        rerank._set_enabled()
    elif state == "disabled":
        rerank._set_disabled()
    try:
        response = await main.health()
    finally:
        rerank._reset_state_for_tests()
    assert response.status_code == 200
    body = json.loads(response.body)
    assert body["rerank"] == expected
    if body["indexer"]["status"] != "degraded":
        assert body["status"] == "ok"
    raw = response.body.decode()
    for leak in ("127.0.0.1", "8081", "/v1/rerank", "probe_failed"):
        assert leak not in raw


def test_the_rerank_transport_line_and_event(monkeypatch, caplog):
    events: list[dict] = []
    monkeypatch.setattr(security_events, "emit", lambda name, **kw: events.append({name: kw}))
    monkeypatch.setattr(settings, "rerank_url", "http://gpu-host:8081/v1/rerank", raising=False)
    with caplog.at_level(logging.INFO, logger="src.services.transport_security"):
        ts.log_rerank_transport()
    lines = [r.getMessage() for r in caplog.records if "Rerank transport" in r.getMessage()]
    assert len(lines) == 1
    line = lines[0]
    assert "scheme=http" in line and "host=gpu-host" in line and "port=8081" in line
    assert "verify=n/a" in line and "plaintext_override=True" in line
    assert "/v1/rerank" not in line
    assert events == [{"internal_transport_plaintext": {"reason": "rerank",
                                                         "outcome": "override"}}]


def test_loopback_and_absent_emit_no_event(monkeypatch, caplog):
    events: list = []
    monkeypatch.setattr(security_events, "emit", lambda *a, **kw: events.append(a))
    monkeypatch.setattr(settings, "rerank_url", "http://127.0.0.1:8081/v1/rerank", raising=False)
    ts.log_rerank_transport()
    monkeypatch.setattr(settings, "rerank_url", "", raising=False)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="src.services.transport_security"):
        ts.log_rerank_transport()
    assert events == []
    assert not [r for r in caplog.records if "Rerank transport" in r.getMessage()]


def test_the_catalogue_entry_is_unchanged():
    assert security_events.EVENT_FIELDS["internal_transport_plaintext"] == frozenset(
        {"reason", "outcome"})
