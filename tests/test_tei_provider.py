"""Native Hugging Face Text Embeddings Inference (TEI) provider.

TEI speaks its own `/embed` protocol: `{"inputs": [...], "truncate": true}`
returns a bare `[[float, ...], ...]`, and it does not take a `model` field.
The OpenAI-compatible `/v1/embeddings` path stays on `OpenAIProvider`; this
provider is for deployments whose embedder exposes only the native API.

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
from src.services import embeddings, refusals
from src.services.embeddings import TeiProvider


@pytest.fixture(autouse=True)
def _fresh_client(monkeypatch):
    monkeypatch.setattr(embeddings, "_TEI_CLIENT", embeddings._SharedClient())


@pytest.fixture
def tei(monkeypatch):
    monkeypatch.setattr(settings, "tei_url", "http://tei:80")
    monkeypatch.setattr(settings, "tei_embedding_model", "BAAI/bge-m3")
    monkeypatch.setattr(settings, "tei_embed_batch_size", 16)
    return settings


def _answer_per_input(request) -> Response:
    inputs = json.loads(request.read())["inputs"]
    return Response(200, json=[[float(len(text))] for text in inputs])


async def test_embed_one_uses_the_native_embed_shape(tei):
    with respx.mock(base_url="http://tei:80") as mock:
        route = mock.post("/embed").mock(side_effect=_answer_per_input)
        out = await TeiProvider().embed_one("hello", kind="query")

    body = json.loads(route.calls[0].request.read())
    assert body == {"inputs": ["hello"], "truncate": True}
    assert out == [5.0]


async def test_embed_batch_slices_at_the_configured_limit(tei):
    texts = [f"chunk-{i}" for i in range(40)]
    with respx.mock(base_url="http://tei:80") as mock:
        route = mock.post("/embed").mock(side_effect=_answer_per_input)
        out = await TeiProvider().embed_batch(texts, kind="document")

    sent = [json.loads(call.request.read())["inputs"] for call in route.calls]
    assert [len(slice_) for slice_ in sent] == [16, 16, 8]
    assert sent[0] + sent[1] + sent[2] == texts
    assert out == [[float(len(text))] for text in texts]


async def test_a_short_response_fails_the_batch(tei):
    with respx.mock(base_url="http://tei:80") as mock:
        mock.post("/embed").mock(return_value=Response(200, json=[[0.1]] * 15))
        with pytest.raises(RuntimeError, match="15 vectors for 16 inputs"):
            await TeiProvider().embed_batch(
                [f"c{i}" for i in range(16)], kind="document"
            )


async def test_the_token_limit_is_translated_and_not_retried(tei, monkeypatch):
    slept: list[float] = []

    async def _sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", _sleep)
    body = {
        "error": (
            "Input validation error: `inputs` must have less than 2048 "
            "tokens. Given: 4003"
        ),
        "error_type": "Validation",
    }
    with respx.mock(base_url="http://tei:80") as mock:
        route = mock.post("/embed").mock(return_value=Response(413, json=body))
        with pytest.raises(refusals.ProviderInputTooLarge) as excinfo:
            await TeiProvider().embed_one("a very long query", kind="query")

    assert excinfo.value.provider == "tei"
    assert "must have less than 2048 tokens" in excinfo.value.reason
    assert route.call_count == 1
    assert slept == []


async def test_the_batch_size_limit_is_not_translated_as_an_input_refusal(
    tei, monkeypatch
):
    """`batch size 97 > maximum allowed batch size 96` is a client
    configuration fault. Translating it to `argument_too_long` would tell the
    caller to shorten a query that is not the problem."""
    slept: list[float] = []

    async def _sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", _sleep)
    body = {
        "error": "batch size 17 > maximum allowed batch size 16",
        "error_type": "Validation",
    }
    with respx.mock(base_url="http://tei:80") as mock:
        route = mock.post("/embed").mock(return_value=Response(413, json=body))
        with pytest.raises(httpx.HTTPStatusError):
            await TeiProvider().embed_one("hello", kind="query")
    assert route.call_count == 1
    assert slept == []


async def test_a_5xx_is_retried_then_succeeds(tei, monkeypatch):
    slept: list[float] = []

    async def _sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", _sleep)
    with respx.mock(base_url="http://tei:80") as mock:
        route = mock.post("/embed")
        route.side_effect = [
            Response(503, json={"error": "busy"}),
            Response(200, json=[[0.5]]),
        ]
        out = await TeiProvider().embed_one("hello", kind="query")

    assert out == [0.5]
    assert route.call_count == 2
    assert len(slept) == 1


def test_get_provider_selects_tei(tei, monkeypatch):
    monkeypatch.setattr(settings, "embedding_provider", "tei")
    assert isinstance(embeddings.get_provider(), TeiProvider)


def test_the_embedding_fingerprint_uses_the_tei_model(tei, monkeypatch):
    from src.services.index_state import active_embedding_model

    monkeypatch.setattr(settings, "embedding_provider", "tei")
    assert active_embedding_model() == "BAAI/bge-m3"


def test_the_tei_batch_size_defaults_to_16_and_is_bounded():
    field = type(settings).model_fields["tei_embed_batch_size"]
    assert field.default == 16
    bounds = {type(meta).__name__: meta for meta in field.metadata}
    assert bounds["Ge"].ge == 1 and bounds["Le"].le == 256
