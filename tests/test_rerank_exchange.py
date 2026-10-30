"""The rerank exchange (#367, design D2, D3, D7; task 7.2): every fake mode
maps to its closed code, scores are mapped by `index`, the one deadline holds
against a sleeping and a slow-drip server, redirects and proxy variables are
ignored, the pool admits two, cancellation propagates, and the WARNING
suppressor logs one record per code per window without response text."""
from __future__ import annotations

import asyncio
import logging
import time

import pytest

from tests._rerank_support import fake, fake_server, no_reranker  # noqa: F401
from src.config import settings
from src.services import rerank
from src.services import transport_security as ts

CANDS = [("Alpha", "alpha bravo"), ("Bravo", "bravo charlie"), ("Charlie", "charlie delta")]


@pytest.mark.parametrize("mode, code", [
    ("status:500", rerank.HTTP_ERROR),
    ("status:404", rerank.HTTP_ERROR),
    ("redirect", rerank.HTTP_ERROR),
    ("not_json", rerank.MALFORMED_RESPONSE),
    ("no_results", rerank.MALFORMED_RESPONSE),
    ("string_score", rerank.MALFORMED_RESPONSE),
    ("short", rerank.COUNT_MISMATCH),
    ("long", rerank.COUNT_MISMATCH),
    ("dup_index", rerank.BAD_INDEX),
    ("oob_index", rerank.BAD_INDEX),
    ("bool_index", rerank.BAD_INDEX),
    ("nan", rerank.NON_FINITE_SCORE),
    ("inf", rerank.NON_FINITE_SCORE),
    ("oversize", rerank.OVERSIZE_RESPONSE),
])
async def test_every_rejected_response_falls_back_with_its_code(fake, mode, code):
    fake.mode = mode
    out = await rerank.rerank_candidates("bravo", CANDS)
    assert out.reranked is False and out.reason == code
    assert out.order == [0, 1, 2] and out.scores is None
    assert out.token == f"reranked: false ({code})"
    assert out.ms is not None


async def test_a_correct_response_reorders_and_maps_scores_by_index(fake):
    fake.mode = "sorted"   # results sorted by score, each with its own index
    out = await rerank.rerank_candidates("charlie delta", CANDS)
    assert out.reranked is True and out.reason is None
    # Charlie (2 words) > Bravo (1) > Alpha (0); mapped by index, not position.
    assert out.order == [2, 1, 0]
    assert out.scores == [0.0, 1.0, 2.0]
    assert out.token == "reranked: true (candidates: 3)"
    body = fake.requests[-1]
    assert body["model"] == "fake-model" and body["query"] == "charlie delta"
    assert body["top_n"] == 3
    assert body["documents"] == ["Alpha\n\nalpha bravo", "Bravo\n\nbravo charlie",
                                 "Charlie\n\ncharlie delta"]


async def test_equal_scores_keep_the_unreranked_order(fake):
    fake.mode = "scores:[0.1, 0.9, 0.5, 0.9, 0.1]"
    out = await rerank.rerank_candidates("q", [("t", "x")] * 5)
    assert out.order == [1, 3, 2, 0, 4]


async def test_the_state_precedence_sends_no_request(fake, monkeypatch):
    rerank._set_disabled(rerank.PROBE_FAILED)
    assert (await rerank.rerank_candidates("q", CANDS)).reason == rerank.PROBE_FAILED
    rerank._set_disabled(rerank.PROBE_SKIPPED)
    assert (await rerank.rerank_candidates("q", CANDS)).reason == rerank.PROBE_SKIPPED
    rerank._set_enabled()
    out = await rerank.rerank_candidates("q", [])
    assert out.reason == rerank.NO_CANDIDATES and out.ms is None
    monkeypatch.setattr(settings, "rerank_url", "", raising=False)
    out = await rerank.rerank_candidates("q", CANDS)
    assert out.reason == rerank.NOT_CONFIGURED and out.ms is None
    assert fake.requests == []


async def test_before_any_probe_a_configured_reranker_answers_probe_failed(fake):
    rerank._reset_state_for_tests()
    assert (await rerank.rerank_candidates("q", CANDS)).reason == rerank.PROBE_FAILED
    assert fake.requests == []


async def test_the_deadline_bounds_a_sleeping_reranker(fake, monkeypatch):
    monkeypatch.setattr(settings, "rerank_timeout_ms", 300, raising=False)
    fake.mode = "sleep:3000"
    started = time.monotonic()
    out = await rerank.rerank_candidates("q", CANDS)
    assert out.reason == rerank.TIMEOUT
    assert time.monotonic() - started < 0.3 + 0.25


async def test_a_slow_drip_body_times_out_closes_and_frees_the_pool(fake, monkeypatch):
    monkeypatch.setattr(settings, "rerank_timeout_ms", 400, raising=False)
    fake.mode = "drip"
    started = time.monotonic()
    out = await rerank.rerank_candidates("q", CANDS)
    assert out.reason == rerank.TIMEOUT
    assert time.monotonic() - started < 0.4 + 0.25
    pool = rerank._get_client()._transport._pool
    assert len(pool.connections) <= 2
    assert all(not c.is_available() or c.is_idle() for c in pool.connections)
    assert not any(c.info() and "ACTIVE" in c.info() for c in pool.connections)
    # The server sees the stream closed under it.
    for _ in range(50):
        if fake.in_flight == 0:
            break
        await asyncio.sleep(0.05)
    assert fake.in_flight == 0 and fake.max_in_flight <= 2
    assert fake.disconnects == 1, "the client closed the dripping stream"
    fake.mode = "correct"
    assert (await rerank.rerank_candidates("bravo", CANDS)).reranked is True


async def test_a_redirect_is_not_followed(fake):
    fake.mode = "redirect"
    out = await rerank.rerank_candidates("q", CANDS)
    assert out.reason == rerank.HTTP_ERROR
    assert fake.redirect_target_hits == 0


async def test_proxy_variables_are_ignored(fake, monkeypatch):
    # A proxy on a port where nothing listens: honouring it would fail.
    from tests.fixtures.fake_reranker import free_port

    dead = f"http://127.0.0.1:{free_port()}"
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.setenv(name, dead)
    await rerank.close_rerank_client()
    out = await rerank.rerank_candidates("bravo", CANDS)
    assert out.reranked is True and len(fake.requests) == 1


async def test_the_pool_admits_two_and_the_third_waits_within_its_deadline(fake, monkeypatch):
    monkeypatch.setattr(settings, "rerank_timeout_ms", 2500, raising=False)
    fake.mode = "sleep:400"
    outs = await asyncio.gather(*(rerank.rerank_candidates("bravo", CANDS) for _ in range(3)))
    assert all(o.reranked for o in outs)
    assert fake.max_in_flight == 2


async def test_cancellation_propagates(fake):
    fake.mode = "sleep:3000"
    task = asyncio.create_task(rerank.rerank_candidates("q", CANDS))
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_an_unexpected_exception_falls_back_as_malformed(fake, monkeypatch):
    async def boom(*_a, **_k):
        raise KeyError("note text that must not be logged")

    monkeypatch.setattr(rerank, "score", boom)
    out = await rerank.rerank_candidates("q", CANDS)
    assert out.reason == rerank.MALFORMED_RESPONSE


async def test_an_unreachable_reranker_is_a_transport_error(fake, monkeypatch):
    from tests.fixtures.fake_reranker import free_port

    monkeypatch.setattr(settings, "rerank_url", f"http://127.0.0.1:{free_port()}/v1/rerank",
                        raising=False)
    out = await rerank.rerank_candidates("q", CANDS)
    assert out.reason == rerank.TRANSPORT_ERROR


async def test_the_warning_suppressor(fake, caplog):
    fake.mode = "status:500"
    with caplog.at_level(logging.WARNING, logger="src.services.rerank"):
        for _ in range(5):
            await rerank.rerank_candidates("secret query", CANDS)
        fake.mode = "not_json"
        await rerank.rerank_candidates("secret query", CANDS)
    records = [r for r in caplog.records if r.name == "src.services.rerank"]
    assert len(records) == 2, [r.getMessage() for r in records]
    assert "code=http_error" in records[0].getMessage()
    assert "code=malformed_response" in records[1].getMessage()
    text = " ".join(r.getMessage() for r in records)
    for leak in ("secret-response-text", "secret query", "127.0.0.1", str(fake.port),
                 "Alpha", "bravo"):
        assert leak not in text
    # The next window reports what it suppressed.
    rerank._warned["http_error"][0] -= rerank.WARNING_WINDOW_S
    fake.mode = "status:500"
    with caplog.at_level(logging.WARNING, logger="src.services.rerank"):
        caplog.clear()
        await rerank.rerank_candidates("q", CANDS)
    assert "suppressed_since_last=4" in caplog.records[-1].getMessage()


def test_documents_are_title_two_newlines_and_text_cut_to_2048():
    assert rerank.build_document("T", "body") == "T\n\nbody"
    assert rerank.build_document("T", None) == "T"
    assert rerank.build_document("T", "") == "T"
    doc = rerank.build_document("t" * 300, "x" * 2000)
    assert len(doc) == 2048 and doc.startswith("t" * 300 + "\n\n")
    # A code point is never split: astral characters are one code point each.
    doc = rerank.build_document("T", "\U0001F600" * 3000)
    assert len(doc) == 2048 and doc.endswith("\U0001F600")
    assert rerank.bounded_text("t" * 40, "x" * 3000) == "x" * (2048 - 42)
    assert rerank.bounded_text("t" * 3000, "x") == ""
    assert rerank.bounded_text("t", None) is None


def test_the_client_factory_policy(monkeypatch):
    client = ts.rerank_http_client(1.5)
    try:
        assert client.follow_redirects is False
        assert client._trust_env is False
        pool = client._transport._pool
        assert pool._max_connections == 2 and pool._max_keepalive_connections == 2
    finally:
        asyncio.run(client.aclose())
