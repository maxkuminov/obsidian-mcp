"""Shared stubs for the offline TEI tests (native-tei-embeddings, task 4.1).

`tei` configures a small `EMBEDDING_DIMENSIONS` so full-width rows stay
readable, and gives each test an unverified process. `answer_info` mounts
`GET /info`; a test that reaches `/embed` needs it, because the provider sends
no `/embed` until the server has been verified.
"""

from __future__ import annotations

import json

import pytest
from httpx import Response

from src.config import settings
from src.services import embeddings

URL = "http://tei:80"
MODEL = "BAAI/bge-m3"
WIDTH = 3
INFO = {"model_id": MODEL, "max_client_batch_size": 32, "version": "1.8.3"}


@pytest.fixture
def tei(monkeypatch):
    monkeypatch.setattr(embeddings, "_TEI_CLIENT", embeddings._SharedClient())
    monkeypatch.setattr(embeddings, "_TEI_VERIFICATION", embeddings._TeiVerification())
    monkeypatch.setattr(settings, "tei_url", URL)
    monkeypatch.setattr(settings, "tei_embedding_model", MODEL)
    monkeypatch.setattr(settings, "tei_embed_batch_size", 16)
    monkeypatch.setattr(settings, "embedding_dimensions", WIDTH)
    return settings


def answer_info(mock, **fields):
    """Mount a `/info` that matches the `tei` fixture, plus any overrides."""
    return mock.get("/info").mock(
        return_value=Response(200, json={**INFO, **fields})
    )


def row(value: float = 0.5) -> list[float]:
    return [value] + [0.0] * (WIDTH - 1)


def answer_per_input(request) -> Response:
    inputs = json.loads(request.read())["inputs"]
    return Response(200, json=[row(float(len(text))) for text in inputs])
