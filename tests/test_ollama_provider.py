"""Unit tests for OllamaProvider: keep_alive handling on /api/embed.

Uses respx to intercept httpx calls so no real Ollama instance is required.
"""
import json

import pytest
import respx
from httpx import Response

from src.config import settings
from src.services.embeddings import OllamaProvider, _coerce_keep_alive


@pytest.mark.parametrize(
    "value,expected",
    [
        ("-1", -1),     # pin forever — must be an int, not the string "-1"
        ("0", 0),       # unload immediately
        ("300", 300),   # seconds
        ("30m", "30m"), # Go duration string — passes through unchanged
        ("1h", "1h"),
    ],
)
def test_coerce_keep_alive(value, expected):
    assert _coerce_keep_alive(value) == expected


@pytest.fixture
def ollama_settings(monkeypatch):
    monkeypatch.setattr(settings, "ollama_url", "http://ollama:11434")
    monkeypatch.setattr(settings, "embedding_model", "bge-m3")
    return settings


@pytest.mark.asyncio
async def test_embed_one_pins_model_with_int_keep_alive(ollama_settings, monkeypatch):
    """`-1` is sent as the integer Ollama needs (the string "-1" is rejected
    by its Go duration parser), alongside the model and input."""
    monkeypatch.setattr(settings, "ollama_keep_alive", "-1")
    provider = OllamaProvider()

    with respx.mock(base_url="http://ollama:11434") as mock:
        route = mock.post("/api/embed").mock(
            return_value=Response(200, json={"embeddings": [[0.1, 0.2, 0.3]]})
        )
        out = await provider.embed_one("hello")

    assert out == [0.1, 0.2, 0.3]
    body = json.loads(route.calls[0].request.read())
    # `== -1` already fails for the string "-1" (since "-1" == -1 is False),
    # so this asserts the integer form Ollama requires.
    assert body["keep_alive"] == -1
    assert body["model"] == "bge-m3"
    assert body["input"] == "hello"


@pytest.mark.asyncio
async def test_embed_one_passes_duration_string_through(ollama_settings, monkeypatch):
    monkeypatch.setattr(settings, "ollama_keep_alive", "30m")
    provider = OllamaProvider()

    with respx.mock(base_url="http://ollama:11434") as mock:
        route = mock.post("/api/embed").mock(
            return_value=Response(200, json={"embeddings": [[1.0]]})
        )
        await provider.embed_one("hi")

    body = json.loads(route.calls[0].request.read())
    assert body["keep_alive"] == "30m"
