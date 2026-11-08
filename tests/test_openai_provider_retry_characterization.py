"""Characterization of `OpenAIProvider`'s retry loop (native-tei-embeddings, task 1.1).

Written and passing against the code *before* `_post_with_retries` was
extracted, and required to pass unmodified after it. The non-JSON case is the
one the extraction could most easily break: the helper's parse step runs
outside the transport-error `try`, so OpenAI's bad 200 stays a
`json.JSONDecodeError` after one request.

Fully offline: respx answers every request.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import respx
from httpx import Response

from src.config import settings
from src.services import embeddings
from src.services.embeddings import OpenAIProvider

URL = "https://api.openai.test/v1"
OK = {"data": [{"index": 0, "embedding": [0.25]}]}


@pytest.fixture(autouse=True)
def _openai(monkeypatch):
    monkeypatch.setattr(embeddings, "_OPENAI_CLIENT", embeddings._SharedClient())
    monkeypatch.setattr(settings, "openai_base_url", URL)
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")
    monkeypatch.setattr(settings, "openai_embedding_model", "text-embedding-3-small")


@pytest.fixture
def slept(monkeypatch):
    calls: list[float] = []

    async def _sleep(seconds):
        calls.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", _sleep)
    return calls


async def test_a_transport_error_then_success_is_two_requests(slept):
    with respx.mock(base_url=URL) as mock:
        route = mock.post("/embeddings")
        route.side_effect = [httpx.ConnectError("boom"), Response(200, json=OK)]
        out = await OpenAIProvider().embed_one("hello", kind="query")

    assert out == [0.25]
    assert route.call_count == 2
    assert slept == [1.0]


async def test_three_transport_errors_propagate_the_third_object(slept):
    third = httpx.ConnectError("third")
    with respx.mock(base_url=URL) as mock:
        route = mock.post("/embeddings")
        route.side_effect = [
            httpx.ConnectError("first"),
            httpx.ReadTimeout("second"),
            third,
        ]
        with pytest.raises(httpx.ConnectError) as excinfo:
            await OpenAIProvider().embed_one("hello", kind="query")

    assert excinfo.value is third
    assert route.call_count == 3
    assert slept == [1.0, 2.0]


@pytest.mark.parametrize("status", [429, 503])
async def test_status_exhaustion_raises_http_status_error(slept, status):
    with respx.mock(base_url=URL) as mock:
        route = mock.post("/embeddings").mock(
            return_value=Response(status, json={"error": {"message": "busy"}})
        )
        with pytest.raises(httpx.HTTPStatusError) as excinfo:
            await OpenAIProvider().embed_one("hello", kind="query")

    assert excinfo.value.response.status_code == status
    assert route.call_count == 3
    assert slept == [1.0, 2.0]


async def test_a_retried_status_is_logged_at_warning(slept, caplog):
    with respx.mock(base_url=URL) as mock:
        route = mock.post("/embeddings")
        route.side_effect = [
            Response(429, json={"error": {"message": "slow down"}}),
            Response(200, json=OK),
        ]
        with caplog.at_level("WARNING", logger=embeddings.logger.name):
            await OpenAIProvider().embed_one("hello", kind="query")

    assert "OpenAI embeddings 429 on attempt 1/3, retrying" in caplog.text


async def test_a_retried_transport_error_is_not_logged(slept, caplog):
    with respx.mock(base_url=URL) as mock:
        route = mock.post("/embeddings")
        route.side_effect = [httpx.ConnectError("boom"), Response(200, json=OK)]
        with caplog.at_level("DEBUG", logger=embeddings.logger.name):
            await OpenAIProvider().embed_one("hello", kind="query")

    assert caplog.records == []


async def test_a_non_retryable_400_is_one_request(slept):
    with respx.mock(base_url=URL) as mock:
        route = mock.post("/embeddings").mock(
            return_value=Response(400, json={"error": {"message": "bad request"}})
        )
        with pytest.raises(httpx.HTTPStatusError):
            await OpenAIProvider().embed_one("hello", kind="query")

    assert route.call_count == 1
    assert slept == []


async def test_a_non_json_200_is_one_request_and_a_decode_error(slept):
    with respx.mock(base_url=URL) as mock:
        route = mock.post("/embeddings").mock(
            return_value=Response(200, text="<html>proxy</html>")
        )
        with pytest.raises(json.JSONDecodeError):
            await OpenAIProvider().embed_one("hello", kind="query")

    assert route.call_count == 1
    assert slept == []
