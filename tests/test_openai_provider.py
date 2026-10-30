"""Unit tests for OpenAIProvider: batching, retries, error propagation.

Uses respx to intercept httpx calls so no real network access is required.
"""
import pytest
import respx
from httpx import Response

from src.config import settings
from src.services.embeddings import OpenAIProvider


def _embedding_payload(n: int, dim: int) -> dict:
    """Build a fake `/v1/embeddings` JSON response with n rows."""
    return {
        "object": "list",
        "data": [
            {
                "object": "embedding",
                "index": i,
                "embedding": [float(i)] * dim,
            }
            for i in range(n)
        ],
        "model": settings.openai_embedding_model,
        "usage": {"prompt_tokens": n, "total_tokens": n},
    }


@pytest.fixture
def openai_settings(monkeypatch):
    monkeypatch.setattr(settings, "embedding_provider", "openai")
    monkeypatch.setattr(settings, "openai_api_key", "sk-test-key-1234")
    monkeypatch.setattr(settings, "openai_base_url", "https://api.openai.com/v1")
    monkeypatch.setattr(settings, "embedding_dimensions", 1024)
    return settings


@pytest.mark.asyncio
async def test_single_batch_happy_path(openai_settings):
    provider = OpenAIProvider()
    with respx.mock(base_url="https://api.openai.com/v1") as mock:
        mock.post("/embeddings").mock(
            return_value=Response(200, json=_embedding_payload(3, 1024))
        )
        out = await provider.embed_batch(["a", "b", "c"], kind="document")
    assert len(out) == 3
    assert all(len(v) == 1024 for v in out)


@pytest.mark.asyncio
async def test_subbatching_at_97_inputs(openai_settings):
    """97 inputs → 2 sub-batches (96 + 1). Verifies request count and order."""
    provider = OpenAIProvider()
    inputs = [f"text-{i}" for i in range(97)]

    with respx.mock(base_url="https://api.openai.com/v1") as mock:
        route = mock.post("/embeddings")
        # First sub-batch: 96 vectors. Second: 1.
        route.side_effect = [
            Response(200, json=_embedding_payload(96, 1024)),
            Response(200, json=_embedding_payload(1, 1024)),
        ]
        out = await provider.embed_batch(inputs, kind="document")

    assert len(out) == 97
    assert route.call_count == 2

    # Verify each request body had the expected sub-batch size.
    first_req_body = route.calls[0].request.read()
    second_req_body = route.calls[1].request.read()
    import json

    assert len(json.loads(first_req_body)["input"]) == 96
    assert len(json.loads(second_req_body)["input"]) == 1


@pytest.mark.asyncio
async def test_429_retry_then_success(openai_settings, monkeypatch):
    # Skip the actual sleep so the test runs fast.
    import asyncio as _asyncio

    async def _no_sleep(_t):
        return None

    monkeypatch.setattr(_asyncio, "sleep", _no_sleep)

    provider = OpenAIProvider()
    with respx.mock(base_url="https://api.openai.com/v1") as mock:
        route = mock.post("/embeddings")
        route.side_effect = [
            Response(429, json={"error": "rate limited"}),
            Response(200, json=_embedding_payload(1, 1024)),
        ]
        out = await provider.embed_one("hello", kind="query")
    assert len(out) == 1024
    assert route.call_count == 2


@pytest.mark.asyncio
async def test_5xx_exhausts_retries(openai_settings, monkeypatch):
    import asyncio as _asyncio

    async def _no_sleep(_t):
        return None

    monkeypatch.setattr(_asyncio, "sleep", _no_sleep)

    provider = OpenAIProvider()
    with respx.mock(base_url="https://api.openai.com/v1") as mock:
        route = mock.post("/embeddings")
        route.side_effect = [
            Response(503, json={"error": "down"}),
            Response(503, json={"error": "down"}),
            Response(503, json={"error": "down"}),
        ]
        with pytest.raises(Exception):
            await provider.embed_one("hello", kind="query")
    assert route.call_count == 3


@pytest.mark.asyncio
async def test_request_includes_dimensions_param(openai_settings):
    provider = OpenAIProvider()
    with respx.mock(base_url="https://api.openai.com/v1") as mock:
        route = mock.post("/embeddings").mock(
            return_value=Response(200, json=_embedding_payload(1, 1024))
        )
        await provider.embed_one("hi", kind="query")

    import json

    body = json.loads(route.calls[0].request.read())
    assert body["dimensions"] == 1024
    assert body["model"] == settings.openai_embedding_model
    assert body["input"] == ["hi"]
