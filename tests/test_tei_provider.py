"""Native Hugging Face Text Embeddings Inference (TEI) provider.

TEI speaks its own `/embed` protocol: `{"inputs": [...], "truncate": true}`
returns a bare `[[float, ...], ...]`, and it does not take a `model` field.
The OpenAI-compatible `/v1/embeddings` path stays on `OpenAIProvider`; this
provider is for deployments whose embedder exposes only the native API.

Fully offline: respx answers every request. The server-identity check and its
gate are in `tests/test_tei_identity.py`.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import respx
from httpx import Response

from _tei_stubs import URL, WIDTH, answer_info, answer_per_input, row, tei  # noqa: F401
from src.config import settings
from src.models.db import NoteEmbedding
from src.services import embeddings, refusals
from src.services.embeddings import NoteEmbedOutcome, OllamaProvider, OpenAIProvider, TeiProvider


@pytest.fixture
def slept(monkeypatch):
    calls: list[float] = []

    async def _sleep(seconds):
        calls.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", _sleep)
    return calls


def _bodies(route) -> list[dict]:
    return [json.loads(call.request.read()) for call in route.calls]


# ── Request contract ────────────────────────────────────────────────────────


async def test_embed_one_uses_the_native_embed_shape(tei):
    with respx.mock(base_url=URL) as mock:
        answer_info(mock)
        route = mock.post("/embed").mock(side_effect=answer_per_input)
        out = await TeiProvider().embed_one("hello", kind="query")

    assert _bodies(route) == [{"inputs": ["hello"], "truncate": True}]
    assert out == row(5.0)
    assert "authorization" not in route.calls[0].request.headers


async def test_get_embedding_sends_one_request_with_the_exact_body(tei, monkeypatch):
    monkeypatch.setattr(settings, "embedding_provider", "tei")
    monkeypatch.setattr(settings, "embedding_query_instruction", "")
    embeddings.get_provider.cache_clear()
    try:
        with respx.mock(base_url=URL) as mock:
            answer_info(mock)
            route = mock.post("/embed").mock(side_effect=answer_per_input)
            await embeddings.get_embedding("hello", kind="query")
    finally:
        embeddings.get_provider.cache_clear()
    assert _bodies(route) == [{"inputs": ["hello"], "truncate": True}]


async def test_embed_batch_slices_at_the_configured_limit(tei):
    texts = [f"chunk-{i}" for i in range(40)]
    with respx.mock(base_url=URL) as mock:
        answer_info(mock)
        route = mock.post("/embed").mock(side_effect=answer_per_input)
        out = await TeiProvider().embed_batch(texts, kind="document")

    sent = [body["inputs"] for body in _bodies(route)]
    assert [len(slice_) for slice_ in sent] == [16, 16, 8]
    assert sent[0] + sent[1] + sent[2] == texts
    assert all(set(body) == {"inputs", "truncate"} for body in _bodies(route))
    assert out == [row(float(len(text))) for text in texts]


async def test_an_empty_batch_makes_no_request(tei):
    with respx.mock(base_url=URL, assert_all_called=False) as mock:
        info = answer_info(mock)
        assert await TeiProvider().embed_batch([], kind="document") == []
    assert info.call_count == 0


async def test_a_short_response_fails_the_batch(tei):
    with respx.mock(base_url=URL) as mock:
        answer_info(mock)
        mock.post("/embed").mock(return_value=Response(200, json=[row()] * 15))
        with pytest.raises(RuntimeError, match="15 vectors for 16 inputs"):
            await TeiProvider().embed_batch(
                [f"c{i}" for i in range(16)], kind="document"
            )


# ── Retries and the slice deadline ──────────────────────────────────────────


async def test_a_5xx_is_retried_then_succeeds(tei, slept):
    with respx.mock(base_url=URL) as mock:
        answer_info(mock)
        route = mock.post("/embed")
        route.side_effect = [
            Response(503, json={"error": "busy"}),
            Response(200, json=[row()]),
        ]
        out = await TeiProvider().embed_one("hello", kind="query")

    assert out == row()
    assert route.call_count == 2
    assert slept == [1.0]


async def test_every_embed_attempt_carries_the_60_s_request_timeout(tei, slept):
    with respx.mock(base_url=URL) as mock:
        answer_info(mock)
        route = mock.post("/embed")
        route.side_effect = [
            Response(503, json={"error": "busy"}),
            Response(200, json=[row()]),
        ]
        await TeiProvider().embed_one("hello", kind="query")

    assert route.call_count == 2
    assert [c.request.extensions["timeout"]["read"] for c in route.calls] == [60.0, 60.0]


async def test_a_transport_error_is_retried(tei, slept):
    with respx.mock(base_url=URL) as mock:
        answer_info(mock)
        route = mock.post("/embed")
        route.side_effect = [
            httpx.ConnectError("boom"),
            Response(200, json=[row()]),
        ]
        await TeiProvider().embed_one("hello", kind="query")
    assert route.call_count == 2
    assert slept == [1.0]


async def test_persistent_5xx_exhausts_three_attempts(tei, slept):
    with respx.mock(base_url=URL) as mock:
        answer_info(mock)
        route = mock.post("/embed").mock(
            return_value=Response(503, json={"error": "busy"})
        )
        with pytest.raises(httpx.HTTPStatusError):
            await TeiProvider().embed_one("hello", kind="query")
    assert route.call_count == 3
    assert slept == [1.0, 2.0]


def test_the_deadline_admits_one_timed_out_attempt_and_a_retry():
    assert embeddings.TEI_SLICE_DEADLINE_SECONDS == 90.0
    assert embeddings.TEI_REQUEST_TIMEOUT_SECONDS == 60.0


async def _hang(request):
    await asyncio.sleep(30)
    return Response(200, json=[row()])


@pytest.mark.parametrize("call", ["one", "batch"])
async def test_a_hung_slice_fails_at_the_deadline_with_a_named_message(
    tei, monkeypatch, call
):
    monkeypatch.setattr(embeddings, "TEI_SLICE_DEADLINE_SECONDS", 0.05)
    with respx.mock(base_url=URL, assert_all_called=False) as mock:
        answer_info(mock)
        mock.post("/embed").mock(side_effect=_hang)
        with pytest.raises(RuntimeError, match="deadline") as excinfo:
            if call == "one":
                await TeiProvider().embed_one("hello", kind="query")
            else:
                await TeiProvider().embed_batch(["hello"], kind="document")
    assert "0.05 s" in str(excinfo.value)


async def test_a_many_slice_note_outlasts_the_per_slice_deadline(tei, monkeypatch):
    """The bound is per slice: the whole batch takes longer than one deadline."""
    monkeypatch.setattr(embeddings, "TEI_SLICE_DEADLINE_SECONDS", 0.25)
    monkeypatch.setattr(settings, "tei_embed_batch_size", 1)

    async def _slow(request):
        await asyncio.sleep(0.05)
        return Response(200, json=[row()])

    with respx.mock(base_url=URL) as mock:
        answer_info(mock)
        route = mock.post("/embed").mock(side_effect=_slow)
        out = await TeiProvider().embed_batch(
            [f"c{i}" for i in range(10)], kind="document"
        )
    assert route.call_count == 10
    assert len(out) == 10


# ── Response validation ─────────────────────────────────────────────────────


_BAD_BODIES = {
    "wrong width": json.dumps([[0.1, 0.2]]),
    "string element": json.dumps([[0.1, "x", 0.2]]),
    "null element": json.dumps([[0.1, None, 0.2]]),
    "boolean element": json.dumps([[0.1, True, 0.2]]),
    "nested list": json.dumps([[0.1, [0.2], 0.3]]),
    "NaN": "[[0.1, NaN, 0.2]]",
    "Infinity": "[[0.1, Infinity, 0.2]]",
    "integer too large for a float": f"[[0.1, 1{'0' * 400}, 0.2]]",
    "row not a list": json.dumps([{"embedding": row()}]),
    "object body": json.dumps({"error": "x"}),
    "html": "<html>proxy</html>",
}


@pytest.mark.parametrize("body", _BAD_BODIES.values(), ids=_BAD_BODIES.keys())
async def test_a_malformed_200_is_a_provider_failure_sent_once(tei, slept, body):
    with respx.mock(base_url=URL) as mock:
        answer_info(mock)
        route = mock.post("/embed").mock(return_value=Response(200, text=body))
        with pytest.raises(RuntimeError) as excinfo:
            await TeiProvider().embed_one("SECRET-CHUNK-TEXT", kind="query")
    assert route.call_count == 1
    assert slept == []
    assert "SECRET-CHUNK-TEXT" not in str(excinfo.value)
    assert not isinstance(excinfo.value, json.JSONDecodeError)


async def test_the_failure_names_the_row_and_width(tei):
    answer = [row(), [0.1] * (WIDTH + 1)]
    with respx.mock(base_url=URL) as mock:
        answer_info(mock)
        mock.post("/embed").mock(return_value=Response(200, json=answer))
        with pytest.raises(RuntimeError, match=f"vector 1 has width {WIDTH + 1}"):
            await TeiProvider().embed_batch(["a", "b"], kind="document")


async def test_integers_are_numbers(tei):
    with respx.mock(base_url=URL) as mock:
        answer_info(mock)
        mock.post("/embed").mock(
            return_value=Response(200, text="[[1, 0, 0]]")
        )
        assert await TeiProvider().embed_one("a", kind="query") == [1, 0, 0]


async def test_a_malformed_200_records_provider_failed_and_writes_nothing(tei):
    class _Note:
        id = 42
        file_path = "notes/example.md"
        content_hash = "new"
        embedded_content_hash = "old"

    class _Session:
        def __init__(self):
            self.added = []

        def add(self, row_):
            self.added.append(row_)

        async def execute(self, *a, **k):  # pragma: no cover - never reached
            raise AssertionError("the database must not be touched")

    session = _Session()
    note = _Note()
    provider = TeiProvider()

    async def _batch(chunks, **_kw):
        return await provider.embed_batch(chunks, kind="document")

    with respx.mock(base_url=URL) as mock, pytest.MonkeyPatch.context() as patch:
        patch.setattr(embeddings, "get_embeddings_batch", _batch)
        answer_info(mock)
        mock.post("/embed").mock(return_value=Response(200, json=[[0.1]]))
        result = await embeddings.embed_note(session, note, "some real content")

    assert result.outcome is NoteEmbedOutcome.PROVIDER_FAILED
    assert result.failure is not None
    assert not any(isinstance(o, NoteEmbedding) for o in session.added)
    assert note.embedded_content_hash == "old"


# ── The input-limit boundary ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "error",
    [
        "Input validation error: `inputs` must have less than 2048 tokens. Given: 4003",
        "batch size 17 > maximum allowed batch size 16",
    ],
    ids=["token-limit", "batch-limit"],
)
async def test_a_413_is_one_request_and_a_provider_failure(tei, slept, error):
    """Not `ProviderInputTooLarge`: under `truncate: true` a token refusal should
    not arrive at all, and a batch-limit refusal is an operator's fault that
    `argument_too_long` would misdirect to the caller."""
    with respx.mock(base_url=URL) as mock:
        answer_info(mock)
        route = mock.post("/embed").mock(
            return_value=Response(413, json={"error": error, "error_type": "Validation"})
        )
        with pytest.raises(httpx.HTTPStatusError):
            await TeiProvider().embed_one("hello", kind="query")
    assert route.call_count == 1
    assert slept == []


_GATEWAY_MESSAGE = "max_tokens must be less than 4096 tokens"


async def test_a_gateway_token_message_stays_a_provider_error_for_openai(monkeypatch):
    monkeypatch.setattr(embeddings, "_OPENAI_CLIENT", embeddings._SharedClient())
    monkeypatch.setattr(settings, "openai_base_url", "https://api.openai.test/v1")
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")
    with respx.mock(base_url="https://api.openai.test/v1") as mock:
        route = mock.post("/embeddings").mock(
            return_value=Response(400, json={"error": {"message": _GATEWAY_MESSAGE}})
        )
        with pytest.raises(httpx.HTTPStatusError):
            await OpenAIProvider().embed_one("hello", kind="query")
    assert route.call_count == 1


async def test_the_same_message_stays_a_provider_error_for_ollama(monkeypatch):
    monkeypatch.setattr(embeddings, "_OLLAMA_CLIENT", embeddings._SharedClient())
    monkeypatch.setattr(settings, "ollama_url", "http://ollama:11434")
    with respx.mock(base_url="http://ollama:11434") as mock:
        mock.post("/api/embed").mock(
            return_value=Response(400, json={"error": _GATEWAY_MESSAGE})
        )
        with pytest.raises(httpx.HTTPStatusError):
            await OllamaProvider().embed_one("hello", kind="query")


async def test_existing_input_limit_detection_is_unchanged(monkeypatch):
    monkeypatch.setattr(embeddings, "_OPENAI_CLIENT", embeddings._SharedClient())
    monkeypatch.setattr(settings, "openai_base_url", "https://api.openai.test/v1")
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")
    body = {"error": {"message": "too long", "code": "context_length_exceeded"}}
    with respx.mock(base_url="https://api.openai.test/v1") as mock:
        mock.post("/embeddings").mock(return_value=Response(400, json=body))
        with pytest.raises(refusals.ProviderInputTooLarge):
            await OpenAIProvider().embed_one("hello", kind="query")


def test_the_token_phrase_is_gone_from_the_shared_list():
    assert embeddings._INPUT_LIMIT_PHRASES.search(_GATEWAY_MESSAGE) is None


# ── Selection ───────────────────────────────────────────────────────────────


def test_get_provider_selects_tei(tei, monkeypatch):
    monkeypatch.setattr(settings, "embedding_provider", "tei")
    embeddings.get_provider.cache_clear()
    try:
        assert isinstance(embeddings.get_provider(), TeiProvider)
    finally:
        embeddings.get_provider.cache_clear()


def test_the_embedding_fingerprint_uses_the_tei_model(tei, monkeypatch):
    from src.services.index_state import active_embedding_model

    monkeypatch.setattr(settings, "embedding_provider", "tei")
    assert active_embedding_model() == "BAAI/bge-m3"


def test_the_tei_batch_size_defaults_to_16_and_is_bounded():
    field = type(settings).model_fields["tei_embed_batch_size"]
    assert field.default == 16
    bounds = {type(meta).__name__: meta for meta in field.metadata}
    assert bounds["Ge"].ge == 1 and bounds["Le"].le == 256
