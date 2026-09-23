"""#281 (D14, D15) — the pooled provider client and Ollama `/api/embed` batching.

Fully offline: HTTP is answered by respx, the clock by `wait_for` spies.
"""

from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path

import httpx
import pytest
import respx
from httpx import Response

from src.config import settings
from src.services import embeddings
from src.services.embeddings import OllamaProvider, OpenAIProvider

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _fresh_clients(monkeypatch):
    """Each test starts with no shared client, and leaves none behind."""
    monkeypatch.setattr(embeddings, "_OLLAMA_CLIENT", embeddings._SharedClient())
    monkeypatch.setattr(embeddings, "_OPENAI_CLIENT", embeddings._SharedClient())


@pytest.fixture
def ollama(monkeypatch):
    monkeypatch.setattr(settings, "ollama_url", "http://ollama:11434")
    monkeypatch.setattr(settings, "embedding_model", "bge-m3")
    monkeypatch.setattr(settings, "ollama_embed_batch_size", 16)
    return settings


def _answer_per_input(request: httpx.Request) -> Response:
    inputs = json.loads(request.read())["input"]
    return Response(
        200, json={"embeddings": [[float(len(t))] for t in inputs]}
    )


# ── Batching ────────────────────────────────────────────────────────────────


def test_the_batch_size_defaults_to_16_and_is_bounded():
    field = type(settings).model_fields["ollama_embed_batch_size"]
    assert field.default == 16
    bounds = {type(m).__name__: m for m in field.metadata}
    assert bounds["Ge"].ge == 1 and bounds["Le"].le == 256


async def test_40_chunks_at_16_are_three_requests_in_order(ollama):
    texts = [f"chunk-{i:02d}" + "x" * i for i in range(40)]
    with respx.mock(base_url="http://ollama:11434") as mock:
        route = mock.post("/api/embed").mock(side_effect=_answer_per_input)
        out = await OllamaProvider().embed_batch(texts)

    sent = [json.loads(c.request.read())["input"] for c in route.calls]
    assert [len(s) for s in sent] == [16, 16, 8]
    assert sent[0] + sent[1] + sent[2] == texts
    # Input order is preserved end to end.
    assert out == [[float(len(t))] for t in texts]


async def test_batch_size_one_is_the_pre_batching_shape(ollama, monkeypatch):
    monkeypatch.setattr(settings, "ollama_embed_batch_size", 1)
    with respx.mock(base_url="http://ollama:11434") as mock:
        route = mock.post("/api/embed").mock(side_effect=_answer_per_input)
        out = await OllamaProvider().embed_batch(["a", "bb", "ccc"])
    assert [json.loads(c.request.read())["input"] for c in route.calls] == [
        ["a"], ["bb"], ["ccc"]
    ]
    assert out == [[1.0], [2.0], [3.0]]


async def test_a_short_response_fails_the_batch(ollama):
    """16 inputs, 15 vectors: the batch raises and nothing of it is used."""
    with respx.mock(base_url="http://ollama:11434") as mock:
        mock.post("/api/embed").mock(
            return_value=Response(200, json={"embeddings": [[0.1]] * 15})
        )
        with pytest.raises(RuntimeError, match="15 vectors for 16 inputs"):
            await OllamaProvider().embed_batch([f"c{i}" for i in range(16)])


async def test_a_short_later_slice_fails_the_whole_batch(ollama):
    answers = iter([16, 7])  # second slice carries 8 inputs, returns 7

    def _respond(request):
        n = next(answers)
        return Response(200, json={"embeddings": [[0.1]] * n})

    with respx.mock(base_url="http://ollama:11434") as mock:
        mock.post("/api/embed").mock(side_effect=_respond)
        with pytest.raises(RuntimeError, match="7 vectors for 8 inputs"):
            await OllamaProvider().embed_batch([f"c{i}" for i in range(24)])


@pytest.fixture
def openai(monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")
    monkeypatch.setattr(settings, "openai_base_url", "https://api.example.test/v1")
    return settings


def _openai_rows(indices):
    return Response(200, json={"data": [
        {"index": i, "embedding": [float(i)]} for i in indices
    ]})


async def test_openai_answer_in_index_order_is_accepted(openai):
    with respx.mock() as mock:
        mock.post("https://api.example.test/v1/embeddings").mock(
            return_value=_openai_rows([2, 0, 1])
        )
        out = await OpenAIProvider().embed_batch(["a", "b", "c"])
    assert out == [[0.0], [1.0], [2.0]]


@pytest.mark.parametrize("indices", [
    pytest.param([0, 1], id="short"),
    pytest.param([0, 1, 1], id="duplicate-index"),
    pytest.param([0, 2, 3], id="missing-index"),
])
async def test_openai_per_request_cardinality_fails_the_batch(openai, indices):
    """Three inputs must come back as exactly indices 0, 1, 2 — a short,
    duplicated or gapped answer fails the batch and no vector of it is used."""
    with respx.mock() as mock:
        route = mock.post("https://api.example.test/v1/embeddings").mock(
            return_value=_openai_rows(indices)
        )
        with pytest.raises(RuntimeError, match="for 3 inputs"):
            await OpenAIProvider().embed_batch(["a", "b", "c"])
    assert route.call_count == 1  # a malformed answer is not retried


async def test_embed_one_sends_a_one_element_array(ollama):
    with respx.mock(base_url="http://ollama:11434") as mock:
        route = mock.post("/api/embed").mock(side_effect=_answer_per_input)
        assert await OllamaProvider().embed_one("query") == [5.0]
    assert json.loads(route.calls[0].request.read())["input"] == ["query"]


async def test_a_hung_request_fails_at_30s_and_so_does_the_next(
    ollama, monkeypatch
):
    """Each slice has its own 30 s bound; there is no deadline over the batch.

    The first slice answers, the second hangs. The spy records the bound
    production asks for and runs the real `wait_for` at a test-sized one.
    """
    provider = OllamaProvider()
    calls: list[list[str]] = []

    async def _request(inputs):
        calls.append(inputs)
        if len(calls) == 1:
            return [[0.0] for _ in inputs]
        await asyncio.sleep(3600)

    monkeypatch.setattr(provider, "_embed_inputs", _request)
    real_wait_for = asyncio.wait_for
    seen: list[float] = []

    async def _spy(coro, timeout):
        seen.append(timeout)
        return await real_wait_for(coro, 0.05)

    monkeypatch.setattr(embeddings.asyncio, "wait_for", _spy)

    with pytest.raises((asyncio.TimeoutError, TimeoutError)):
        await provider.embed_batch([f"c{i}" for i in range(20)])

    assert seen == [30.0, 30.0]
    assert [len(c) for c in calls] == [16, 4]


async def test_each_request_carries_its_per_request_timeout(ollama, monkeypatch):
    monkeypatch.setattr(settings, "embedding_provider", "openai")
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")
    monkeypatch.setattr(settings, "openai_base_url", "https://api.example.test/v1")
    with respx.mock() as mock:
        ollama_route = mock.post("http://ollama:11434/api/embed").mock(
            side_effect=_answer_per_input
        )
        openai_route = mock.post("https://api.example.test/v1/embeddings").mock(
            return_value=Response(
                200, json={"data": [{"index": 0, "embedding": [0.2]}]}
            )
        )
        await OllamaProvider().embed_one("q")
        await OpenAIProvider().embed_one("q")

    def _read_timeout(route):
        return route.calls[0].request.extensions["timeout"]["read"]

    assert _read_timeout(ollama_route) == 30.0
    assert _read_timeout(openai_route) == 60.0


# ── The shared client ───────────────────────────────────────────────────────


def _count_builds(monkeypatch) -> list[httpx.AsyncClient]:
    built: list[httpx.AsyncClient] = []
    real = embeddings.embedding_http_client

    def _factory(timeout):
        client = real(timeout)
        built.append(client)
        return client

    monkeypatch.setattr(embeddings, "embedding_http_client", _factory)
    return built


async def test_one_client_serves_every_request_on_a_loop(ollama, monkeypatch):
    built = _count_builds(monkeypatch)
    with respx.mock(base_url="http://ollama:11434") as mock:
        mock.post("/api/embed").mock(side_effect=_answer_per_input)
        provider = OllamaProvider()
        await provider.embed_batch([f"c{i}" for i in range(40)])
        await provider.embed_one("q")
        await OllamaProvider().embed_one("another instance, same client")
    assert len(built) == 1


def test_a_new_loop_gets_a_new_client(ollama, monkeypatch):
    built = _count_builds(monkeypatch)

    async def _twice():
        a = embeddings._ollama_client()
        b = embeddings._ollama_client()
        assert a is b
        return a

    first = asyncio.run(_twice())
    second = asyncio.run(_twice())
    assert len(built) == 2
    assert first is not second


async def test_a_closed_client_is_rebuilt(ollama, monkeypatch):
    built = _count_builds(monkeypatch)
    first = embeddings._ollama_client()
    await embeddings.close_provider_client()
    assert first.is_closed
    second = embeddings._ollama_client()
    assert second is not first and not second.is_closed
    assert len(built) == 2
    await embeddings.close_provider_client()


async def test_close_provider_client_closes_both_and_is_idempotent(ollama):
    o = embeddings._ollama_client()
    oa = embeddings._openai_client()
    await embeddings.close_provider_client()
    assert o.is_closed and oa.is_closed
    await embeddings.close_provider_client()


async def test_the_shared_instance_has_the_factorys_properties(ollama):
    for client in (embeddings._ollama_client(), embeddings._openai_client()):
        assert client.trust_env is False
        assert client.follow_redirects is False
        assert client._mounts == {}
    assert embeddings._ollama_client().timeout == httpx.Timeout(30.0)
    assert embeddings._openai_client().timeout == httpx.Timeout(60.0)
    await embeddings.close_provider_client()


def test_the_lifespan_closes_the_client_after_the_indexer_before_dispose():
    """Source order inside the lifespan's shutdown `finally`: the indexer task
    is cancelled and awaited, then the provider client is closed, then the
    engine is disposed."""
    source = (ROOT / "src" / "main.py").read_text()
    tree = ast.parse(source)
    lifespan = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "lifespan"
    )
    body = ast.get_source_segment(source, lifespan)
    cancel = body.index("indexer_task.cancel()")
    shield = body.index("asyncio.shield(indexer_task)")
    close = body.index("await close_provider_client()")
    dispose = body.index("await engine.dispose()")
    assert cancel < shield < close < dispose
