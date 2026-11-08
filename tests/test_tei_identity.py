"""The TEI server identity check and embedding gate (design D1).

The rule under test: the process exits only on positive evidence of a
mismatch, and a vector is requested only on positive evidence of a match.
"""

from __future__ import annotations

import asyncio
import logging
import time as real_time
from types import SimpleNamespace

import httpx
import pytest
import respx
from httpx import Response

import src.main as main
from _lifespan_stubs import stub_transport_checks
from _tei_stubs import INFO, MODEL, URL, answer_info, answer_per_input, tei  # noqa: F401
from src.config import settings
from src.services import embeddings
from src.services.embeddings import TeiProvider, verify_tei_server


def _criticals(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.CRITICAL]


def _warnings(caplog) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and "TEI" in r.getMessage()
    ]


@pytest.fixture
def clock(monkeypatch):
    now = SimpleNamespace(value=1000.0)
    monkeypatch.setattr(
        embeddings, "time", SimpleNamespace(monotonic=lambda: now.value)
    )
    return now


# ── One check ───────────────────────────────────────────────────────────────


async def test_a_matching_server_is_verified(tei):
    with respx.mock(base_url=URL) as mock:
        answer_info(mock)
        result = await verify_tei_server()
    assert result.status == "ok"
    assert embeddings._TEI_VERIFICATION.verified


async def test_a_different_model_is_a_mismatch_naming_both(tei, caplog):
    with respx.mock(base_url=URL) as mock:
        answer_info(mock, model_id="intfloat/multilingual-e5-small")
        result = await verify_tei_server()
    assert result.status == "mismatch"
    (line,) = _criticals(caplog)
    for part in ("TEI_EMBEDDING_MODEL", MODEL, "intfloat/multilingual-e5-small"):
        assert part in line
    assert not embeddings._TEI_VERIFICATION.verified


async def test_the_comparison_is_exact(tei):
    with respx.mock(base_url=URL) as mock:
        answer_info(mock, model_id=MODEL.lower())
        assert (await verify_tei_server()).status == "mismatch"


async def test_a_batch_above_the_server_limit_is_a_mismatch(tei, monkeypatch, caplog):
    monkeypatch.setattr(settings, "tei_embed_batch_size", 128)
    with respx.mock(base_url=URL) as mock:
        answer_info(mock, max_client_batch_size=96)
        result = await verify_tei_server()
    assert result.status == "mismatch"
    (line,) = _criticals(caplog)
    for part in ("TEI_EMBED_BATCH_SIZE", "128", "96"):
        assert part in line


async def test_a_batch_equal_to_the_server_limit_passes(tei, monkeypatch):
    monkeypatch.setattr(settings, "tei_embed_batch_size", 32)
    with respx.mock(base_url=URL) as mock:
        answer_info(mock, max_client_batch_size=32)
        assert (await verify_tei_server()).status == "ok"


@pytest.mark.parametrize("value", [None, "96", True, 1.5], ids=repr)
async def test_an_unusable_batch_limit_passes_with_one_warning(tei, caplog, value):
    fields = {"model_id": MODEL}
    if value is not None:
        fields["max_client_batch_size"] = value
    with respx.mock(base_url=URL) as mock:
        mock.get("/info").mock(return_value=Response(200, json=fields))
        with caplog.at_level(logging.WARNING):
            result = await verify_tei_server()
    assert result.status == "ok"
    assert len(_warnings(caplog)) == 1


@pytest.mark.parametrize(
    "answer",
    [
        Response(401, json={"error": "unauthorized"}),
        Response(404),
        Response(302, headers={"location": "http://elsewhere/info"}),
        Response(503, json={"error": "loading"}),
        Response(200, text="<html>proxy</html>"),
        Response(200, json=["not", "an", "object"]),
        Response(200, json={"version": "1.8.3"}),
        Response(200, json={"model_id": ""}),
        Response(200, json={"model_id": 7}),
    ],
    ids=["401", "404", "302", "503", "html", "list", "no-model", "empty-model", "int-model"],
)
async def test_an_unusable_answer_is_inconclusive(tei, caplog, answer):
    with respx.mock(base_url=URL) as mock:
        mock.get("/info").mock(return_value=answer)
        result = await verify_tei_server()
    assert result.status == "inconclusive"
    assert len(_criticals(caplog)) == 1
    assert "withheld" in _criticals(caplog)[0]
    assert not embeddings._TEI_VERIFICATION.verified


async def test_a_connection_error_is_inconclusive(tei):
    with respx.mock(base_url=URL) as mock:
        mock.get("/info").mock(side_effect=httpx.ConnectError("refused"))
        assert (await verify_tei_server()).status == "inconclusive"


async def test_one_attempt_only(tei):
    with respx.mock(base_url=URL) as mock:
        route = mock.get("/info").mock(side_effect=httpx.ConnectError("refused"))
        await verify_tei_server()
    assert route.call_count == 1


async def test_a_hung_info_is_abandoned_at_the_wall_clock_bound(tei, monkeypatch):
    monkeypatch.setattr(embeddings, "TEI_INFO_TIMEOUT_SECONDS", 0.05)

    async def _hang(request):
        await asyncio.sleep(30)
        return Response(200, json=INFO)

    with respx.mock(base_url=URL, assert_all_called=False) as mock:
        mock.get("/info").mock(side_effect=_hang)
        result = await verify_tei_server()
    assert result.status == "inconclusive"
    assert "did not finish" in result.reason


async def test_a_trickling_info_is_abandoned_at_the_wall_clock_bound(tei, monkeypatch):
    """Every read succeeds well inside httpx's per-phase timeout, so only the
    wall-clock bound can end this. A real socket: respx cannot trickle."""

    async def _trickle(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\ncontent-length: 100000\r\n\r\n")
        try:
            while True:
                writer.write(b"x")
                await writer.drain()
                await asyncio.sleep(0.01)
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(_trickle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    monkeypatch.setattr(settings, "tei_url", f"http://127.0.0.1:{port}")
    monkeypatch.setattr(embeddings, "TEI_INFO_TIMEOUT_SECONDS", 0.2)
    started = real_time.monotonic()
    try:
        result = await verify_tei_server()
    finally:
        server.close()
    assert result.status == "inconclusive"
    assert real_time.monotonic() - started < 3


async def test_the_info_request_is_bounded_at_five_seconds(tei):
    assert embeddings.TEI_INFO_TIMEOUT_SECONDS == 5.0
    with respx.mock(base_url=URL) as mock:
        route = answer_info(mock)
        await verify_tei_server()
    assert route.calls[0].request.extensions["timeout"]["read"] == 5.0


async def test_the_client_comes_from_the_factory(tei, monkeypatch):
    built: list[float] = []
    real = embeddings.embedding_http_client

    def _spy(timeout):
        built.append(timeout)
        return real(timeout)

    monkeypatch.setattr(embeddings, "embedding_http_client", _spy)
    with respx.mock(base_url=URL) as mock:
        answer_info(mock)
        await verify_tei_server()
    assert len(built) == 1


async def test_a_reported_value_is_bounded_and_escaped(tei, caplog):
    hostile = "x\nFORGED LINE " + "y" * 5000
    with respx.mock(base_url=URL) as mock:
        answer_info(mock, model_id=hostile)
        result = await verify_tei_server()
    assert result.status == "mismatch"
    (line,) = _criticals(caplog)
    assert "\n" not in line and "x\\nFORGED" in line
    assert "y" * 200 not in line and len(line) < 400


# ── The gate ────────────────────────────────────────────────────────────────


async def test_no_embed_before_verification(tei, clock):
    with respx.mock(base_url=URL, assert_all_called=False) as mock:
        mock.get("/info").mock(side_effect=httpx.ConnectError("refused"))
        embed = mock.post("/embed").mock(side_effect=answer_per_input)
        with pytest.raises(RuntimeError, match="withheld"):
            await TeiProvider().embed_one("hello", kind="query")
    assert embed.call_count == 0


async def test_a_late_start_verifies_after_the_interval(tei, clock):
    with respx.mock(base_url=URL) as mock:
        info = mock.get("/info")
        info.side_effect = [httpx.ConnectError("refused"), Response(200, json=INFO)]
        embed = mock.post("/embed").mock(side_effect=answer_per_input)
        await verify_tei_server()  # the lifespan's check

        clock.value += 29.0
        with pytest.raises(RuntimeError, match="ConnectError"):
            await TeiProvider().embed_one("hello", kind="query")
        assert embed.call_count == 0 and info.call_count == 1

        clock.value += 1.0
        out = await TeiProvider().embed_batch(["hello"], kind="document")
    assert len(out) == 1
    assert info.call_count == 2 and embed.call_count == 1


async def test_a_wrong_server_coming_up_late_writes_nothing_and_does_not_exit(
    tei, clock, caplog
):
    with respx.mock(base_url=URL, assert_all_called=False) as mock:
        info = mock.get("/info")
        wrong = Response(200, json={**INFO, "model_id": "other/model"})
        info.side_effect = [httpx.ConnectError("refused"), wrong, wrong]
        embed = mock.post("/embed").mock(side_effect=answer_per_input)
        await verify_tei_server()
        for _ in range(2):
            clock.value += 30.0
            with pytest.raises(RuntimeError, match="other/model"):
                await TeiProvider().embed_one("hello", kind="query")
    assert embed.call_count == 0
    # Inconclusive, then one line for the change to mismatch, none for the
    # repeated verdict.
    assert len(_criticals(caplog)) == 2


async def test_recovery_needs_no_restart(tei, clock):
    with respx.mock(base_url=URL) as mock:
        info = mock.get("/info")
        info.side_effect = [
            Response(200, json={**INFO, "model_id": "other/model"}),
            Response(200, json=INFO),
        ]
        mock.post("/embed").mock(side_effect=answer_per_input)
        assert (await verify_tei_server()).status == "mismatch"
        clock.value += 30.0
        assert len(await TeiProvider().embed_one("hello", kind="query")) == 3


async def test_an_outage_repeats_no_critical_as_its_reason_changes(tei, clock, caplog):
    with respx.mock(base_url=URL) as mock:
        mock.get("/info").side_effect = [
            httpx.ConnectError("refused"),
            Response(503),
            httpx.ReadTimeout("slow"),
        ]
        for _ in range(3):
            await verify_tei_server()
            clock.value += 30.0
    assert len(_criticals(caplog)) == 1


async def test_at_most_one_check_per_interval_across_many_notes(tei, clock):
    with respx.mock(base_url=URL) as mock:
        info = mock.get("/info").mock(side_effect=httpx.ConnectError("refused"))
        for _ in range(100):
            with pytest.raises(RuntimeError):
                await TeiProvider().embed_batch(["c"], kind="document")
    assert info.call_count == 1


async def test_concurrent_callers_do_not_stampede_the_check(tei):
    release = asyncio.Event()

    async def _held(request):
        await release.wait()
        return Response(200, json=INFO)

    with respx.mock(base_url=URL, assert_all_called=False) as mock:
        info = mock.get("/info").mock(side_effect=_held)
        mock.post("/embed").mock(side_effect=answer_per_input)
        calls = [
            asyncio.create_task(TeiProvider().embed_one("hello", kind="query"))
            for _ in range(20)
        ]
        await asyncio.sleep(0.01)
        release.set()
        outcomes = await asyncio.gather(*calls, return_exceptions=True)
    assert info.call_count == 1
    # The caller that ran the check proceeds; the others failed fast rather
    # than queueing behind it.
    assert sum(isinstance(o, list) for o in outcomes) == 1
    assert sum(isinstance(o, RuntimeError) for o in outcomes) == 19


async def test_verification_is_not_repeated(tei, clock):
    with respx.mock(base_url=URL) as mock:
        info = answer_info(mock)
        mock.post("/embed").mock(side_effect=answer_per_input)
        await verify_tei_server()
        clock.value += 3600.0
        await TeiProvider().embed_one("a", kind="query")
        await TeiProvider().embed_batch(["b"], kind="document")
    assert info.call_count == 1


# ── The lifespan ────────────────────────────────────────────────────────────


class _Stop(Exception):
    """Raised by the guard after the TEI check: the rest needs a database."""


class _FakeSessionManager:
    def run(self):
        class _CM:
            async def __aenter__(self_inner):
                return None

            async def __aexit__(self_inner, *exc):
                return False

        return _CM()


def _stub_guards(monkeypatch):
    async def _noop():
        return None

    async def _stop():
        raise _Stop

    monkeypatch.setattr(main.settings, "mcp_sandbox_mode", False, raising=False)
    stub_transport_checks(monkeypatch, main)
    monkeypatch.setattr(main, "_check_openat2_support", lambda: None)
    monkeypatch.setattr(main, "_check_mount_identity_support", lambda: None)
    monkeypatch.setattr(main, "_log_retrieval_fingerprint", lambda: None)
    for name in (
        "_check_embedding_dim",
        "_check_pgvector_version",
        "_validate_fts_configs",
        "_check_embedding_fingerprint",
        "_check_fts_fingerprint",
    ):
        monkeypatch.setattr(main, name, _noop)
    monkeypatch.setattr(main, "_warn_missing_keyword_excerpts", _stop)


async def test_the_lifespan_exits_on_a_proven_mismatch(tei, monkeypatch, caplog):
    _stub_guards(monkeypatch)
    monkeypatch.setattr(main.settings, "embedding_provider", "tei")
    with respx.mock(base_url=URL) as mock:
        answer_info(mock, model_id="other/model")
        with pytest.raises(SystemExit) as excinfo:
            async with main.lifespan(main.app):
                pass
    assert excinfo.value.code == 1
    assert any("other/model" in line for line in _criticals(caplog))


@pytest.mark.parametrize(
    "answer",
    [httpx.ConnectError("refused"), Response(401), Response(200, json={})],
    ids=["refused", "401", "no-model-id"],
)
async def test_the_lifespan_continues_when_inconclusive(tei, monkeypatch, caplog, answer):
    _stub_guards(monkeypatch)
    monkeypatch.setattr(main.settings, "embedding_provider", "tei")
    with respx.mock(base_url=URL) as mock:
        mock.get("/info").mock(side_effect=[answer])
        with pytest.raises(_Stop):
            async with main.lifespan(main.app):
                pass
    assert sum("withheld" in line for line in _criticals(caplog)) == 1
    assert not embeddings._TEI_VERIFICATION.verified


async def test_the_lifespan_marks_a_matching_server_verified(tei, monkeypatch):
    _stub_guards(monkeypatch)
    monkeypatch.setattr(main.settings, "embedding_provider", "tei")
    with respx.mock(base_url=URL) as mock:
        answer_info(mock)
        with pytest.raises(_Stop):
            async with main.lifespan(main.app):
                pass
    assert embeddings._TEI_VERIFICATION.verified


async def test_the_check_follows_the_fingerprint_guards(tei, monkeypatch):
    _stub_guards(monkeypatch)
    monkeypatch.setattr(main.settings, "embedding_provider", "tei")
    order: list[str] = []

    async def _fts():
        order.append("fts_fingerprint")

    async def _verify():
        order.append("tei")
        return embeddings.TeiServerCheck("ok", "ok")

    monkeypatch.setattr(main, "_check_fts_fingerprint", _fts)
    monkeypatch.setattr(main, "verify_tei_server", _verify)
    with pytest.raises(_Stop):
        async with main.lifespan(main.app):
            pass
    assert order == ["fts_fingerprint", "tei"]


@pytest.mark.parametrize("provider", ["ollama", "openai"])
async def test_other_providers_are_not_probed(tei, monkeypatch, provider):
    _stub_guards(monkeypatch)
    monkeypatch.setattr(main.settings, "embedding_provider", provider)
    monkeypatch.setattr(main.settings, "openai_api_key", "sk-test")
    with respx.mock(base_url=URL, assert_all_called=False) as mock:
        info = answer_info(mock)
        with pytest.raises(_Stop):
            async with main.lifespan(main.app):
                pass
    assert info.call_count == 0


async def test_sandbox_mode_skips_the_check(tei, monkeypatch):
    async def _noop():
        return None

    monkeypatch.setattr(main.settings, "mcp_sandbox_mode", True, raising=False)
    monkeypatch.setattr(main.settings, "embedding_provider", "tei")
    monkeypatch.setattr(main, "_publish_first_root_snapshot", _noop)
    monkeypatch.setattr(main, "mcp", SimpleNamespace(session_manager=_FakeSessionManager()))
    with respx.mock(base_url=URL, assert_all_called=False) as mock:
        info = answer_info(mock)
        async with main.lifespan(main.app):
            pass
    assert info.call_count == 0
