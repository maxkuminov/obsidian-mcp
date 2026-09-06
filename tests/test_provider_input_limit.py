"""#194 / D6 — a provider's own input-limit rejection becomes a typed exception.

`MAX_SEARCH_QUERY_CHARS` is a **character** cap and the providers enforce a
**token** limit, so the cap is necessary and not sufficient: 8,192 characters
of a densely-tokenizing script still exceed 8,192 tokens. When the provider
says so, `OllamaProvider` and `OpenAIProvider` raise
`refusals.ProviderInputTooLarge` carrying the provider's stated reason, and
`semantic_search_impl` (Slice A) renders it as the ordinary
`argument_too_long` refusal — one actionable failure mode for "the query was
too large" whichever limit actually applied.

Three properties are load-bearing and each is pinned below:

1. **Only an input-limit rejection is translated.** A generic provider error —
   an unknown model, a malformed body, a 5xx — propagates exactly as it did
   before, through the same #127 retry/backoff.
2. **An input-limit rejection is never retried.** It is a fact about the bytes
   we sent, so three attempts would fail identically and cost three round
   trips. A 429 in particular is *not* translated: it is a velocity fact and
   must keep going through the backoff that makes it succeed.
3. **The indexer is not a caller that can shorten its input.** `embed_note`
   catches this exception with every other provider exception and records
   `PROVIDER_FAILED` carrying the class name — the honest pass record.

These stubs are the **deterministic** coverage of the translation branch: the
default Ollama deployment truncates rather than rejecting, so the live
exercise cannot reach it. Fully offline — respx intercepts both providers'
HTTP calls, so no Ollama, no OpenAI and no network.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import respx
from httpx import Response
from sqlalchemy import delete
from sqlalchemy.sql.elements import TextClause

import src.services.embeddings as embeddings
from src.config import MAX_SEARCH_QUERY_CHARS, settings
from src.models.db import NoteEmbedding
from src.services import refusals
from src.services.embeddings import (
    MAX_PROVIDER_REASON_CHARS,
    NoteEmbedOutcome,
    OllamaProvider,
    OpenAIProvider,
)

# ── The vendors' documented error shapes ────────────────────────────────────

#: OpenAI's long-standing context-length rejection: HTTP 400, the code in
#: `error.code`, the numbers in `error.message`.
OPENAI_CONTEXT_LENGTH = {
    "error": {
        "message": (
            "This model's maximum context length is 8192 tokens, however you "
            "requested 10531 tokens (10531 in your prompt). Please reduce your "
            "prompt; or completion length."
        ),
        "type": "invalid_request_error",
        "param": None,
        "code": "context_length_exceeded",
    }
}

#: The per-request token ceiling the embeddings endpoint enforces, whose type
#: and code are the same string.
OPENAI_MAX_TOKENS_PER_REQUEST = {
    "error": {
        "message": "Requested 402931 tokens, max 300000 tokens per request",
        "type": "max_tokens_per_request",
        "param": None,
        "code": "max_tokens_per_request",
    }
}

#: The raw character bound on one input element.
OPENAI_STRING_ABOVE_MAX_LENGTH = {
    "error": {
        "message": (
            "Invalid 'input': string too long. Expected a string with maximum "
            "length 1048576, but got a string with length 2097152 instead."
        ),
        "type": "invalid_request_error",
        "param": "input",
        "code": "string_above_max_length",
    }
}

#: Ollama's `/api/embed` when it reports the limit instead of truncating: a
#: bare string, no machine-readable code at all.
OLLAMA_INPUT_LENGTH = {"error": "input length exceeds maximum context length"}


@pytest.fixture
def openai_settings(monkeypatch):
    monkeypatch.setattr(settings, "embedding_provider", "openai")
    monkeypatch.setattr(settings, "openai_api_key", "sk-test-key-1234")
    monkeypatch.setattr(settings, "openai_base_url", "https://api.openai.com/v1")
    monkeypatch.setattr(settings, "embedding_dimensions", 1024)
    return settings


@pytest.fixture
def ollama_settings(monkeypatch):
    monkeypatch.setattr(settings, "ollama_url", "http://ollama:11434")
    monkeypatch.setattr(settings, "embedding_model", "bge-m3")
    monkeypatch.setattr(settings, "ollama_keep_alive", "-1")
    return settings


@pytest.fixture
def no_sleep(monkeypatch):
    """Collapse the backoff so a retry test costs no wall clock.

    It also *counts*: a translated rejection must not sleep at all, which is
    the difference between one round trip and three.
    """
    slept: list[float] = []

    async def _sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", _sleep)
    return slept


def _embedding_payload(n: int, dim: int) -> dict:
    return {
        "object": "list",
        "data": [
            {"object": "embedding", "index": i, "embedding": [float(i)] * dim}
            for i in range(n)
        ],
        "model": "text-embedding-3-small",
        "usage": {"prompt_tokens": n, "total_tokens": n},
    }


# ── OpenAI: the documented input-too-large shapes ───────────────────────────


@pytest.mark.parametrize(
    "body,expected_fragment",
    [
        (OPENAI_CONTEXT_LENGTH, "maximum context length is 8192 tokens"),
        (OPENAI_MAX_TOKENS_PER_REQUEST, "max 300000 tokens per request"),
        (OPENAI_STRING_ABOVE_MAX_LENGTH, "string too long"),
    ],
    ids=["context_length_exceeded", "max_tokens_per_request", "string_above_max_length"],
)
@pytest.mark.asyncio
async def test_openai_input_limit_shapes_raise_the_typed_exception(
    openai_settings, no_sleep, body, expected_fragment
):
    """Each documented shape yields the typed exception, the vendor's own
    message preserved, and **one** request — never the retry path."""
    provider = OpenAIProvider()
    with respx.mock(base_url="https://api.openai.com/v1") as mock:
        route = mock.post("/embeddings").mock(return_value=Response(400, json=body))
        with pytest.raises(refusals.ProviderInputTooLarge) as excinfo:
            await provider.embed_one("a query in a densely tokenizing script")

    exc = excinfo.value
    assert exc.provider == "openai"
    assert expected_fragment in exc.reason
    # `str(exc)` is the reason too — `semantic_search_impl` reads `.reason`,
    # but a stray `str(exc)` in a log must say the same thing.
    assert str(exc) == exc.reason
    assert route.call_count == 1, "an input-limit rejection must not be retried"
    assert no_sleep == [], "no backoff for a rejection that cannot succeed"


@pytest.mark.asyncio
async def test_openai_gateway_without_a_code_is_matched_on_its_message(
    openai_settings, no_sleep
):
    """An OpenAI-compatible gateway that sends no recognisable code still
    gets translated, on the prose fallback."""
    provider = OpenAIProvider()
    body = {
        "error": {
            "message": "input is too long for this model's context length",
            "type": "invalid_request_error",
        }
    }
    with respx.mock(base_url="https://api.openai.com/v1") as mock:
        mock.post("/embeddings").mock(return_value=Response(422, json=body))
        with pytest.raises(refusals.ProviderInputTooLarge) as excinfo:
            await provider.embed_one("hello")

    assert excinfo.value.reason.startswith("input is too long")


# ── OpenAI: everything else is untouched ────────────────────────────────────


@pytest.mark.asyncio
async def test_openai_generic_400_still_propagates_as_today(
    openai_settings, no_sleep
):
    """An unknown model is a 400 too, and must stay a raw provider error —
    translating it would tell an agent to shorten a query that was fine."""
    provider = OpenAIProvider()
    body = {
        "error": {
            "message": "The model `text-embedding-9` does not exist",
            "type": "invalid_request_error",
            "code": "model_not_found",
        }
    }
    with respx.mock(base_url="https://api.openai.com/v1") as mock:
        route = mock.post("/embeddings").mock(return_value=Response(400, json=body))
        with pytest.raises(httpx.HTTPStatusError):
            await provider.embed_one("hello")
    assert route.call_count == 1
    assert no_sleep == []


@pytest.mark.asyncio
async def test_openai_429_is_retried_and_never_translated(openai_settings, no_sleep):
    """A rate limit is a velocity fact. Even carrying tokens-per-minute prose,
    it keeps the #127 backoff rather than becoming `argument_too_long`."""
    provider = OpenAIProvider()
    body = {
        "error": {
            "message": (
                "Request too large for text-embedding-3-small in organization "
                "org-x on tokens per min (TPM): Limit 1000000, Requested 1200000"
            ),
            "type": "tokens",
            "code": "rate_limit_exceeded",
        }
    }
    with respx.mock(base_url="https://api.openai.com/v1") as mock:
        route = mock.post("/embeddings")
        route.side_effect = [Response(429, json=body)] * 3
        with pytest.raises(httpx.HTTPStatusError):
            await provider.embed_one("hello")
    assert route.call_count == 3, "the 429 backoff is untouched"
    assert len(no_sleep) == 2


@pytest.mark.asyncio
async def test_openai_5xx_still_exhausts_its_retries(openai_settings, no_sleep):
    provider = OpenAIProvider()
    with respx.mock(base_url="https://api.openai.com/v1") as mock:
        route = mock.post("/embeddings")
        route.side_effect = [Response(503, json={"error": "down"})] * 3
        with pytest.raises(httpx.HTTPStatusError):
            await provider.embed_one("hello")
    assert route.call_count == 3
    assert len(no_sleep) == 2


@pytest.mark.asyncio
async def test_openai_transport_error_still_propagates(openai_settings, no_sleep):
    """The `httpx.HTTPError` branch — no response at all — is unchanged."""
    provider = OpenAIProvider()
    with respx.mock(base_url="https://api.openai.com/v1") as mock:
        mock.post("/embeddings").mock(side_effect=httpx.ConnectError("refused"))
        with pytest.raises(httpx.ConnectError):
            await provider.embed_one("hello")


@pytest.mark.asyncio
async def test_openai_rejection_in_a_later_subbatch_propagates(
    openai_settings, no_sleep
):
    """`embed_batch` sub-batches at 96. A rejection in the second sub-batch
    escapes the loop rather than being counted as a short result."""
    provider = OpenAIProvider()
    with respx.mock(base_url="https://api.openai.com/v1") as mock:
        route = mock.post("/embeddings")
        route.side_effect = [
            Response(200, json=_embedding_payload(96, 1024)),
            Response(400, json=OPENAI_CONTEXT_LENGTH),
        ]
        with pytest.raises(refusals.ProviderInputTooLarge):
            await provider.embed_batch([f"chunk-{i}" for i in range(97)])
    assert route.call_count == 2


@pytest.mark.asyncio
async def test_the_reason_is_bounded_at_capture(openai_settings, no_sleep):
    """A vendor that echoes the offending input back cannot flood the
    caller-facing refusal or the usage row through this field."""
    secret_tail = "ZZZ-END-OF-INPUT"
    provider = OpenAIProvider()
    body = {
        "error": {
            "message": (
                "This model's maximum context length is 8192 tokens. Input was: "
                + "x" * 20_000
                + secret_tail
            ),
            "type": "invalid_request_error",
            "code": "context_length_exceeded",
        }
    }
    with respx.mock(base_url="https://api.openai.com/v1") as mock:
        mock.post("/embeddings").mock(return_value=Response(400, json=body))
        with pytest.raises(refusals.ProviderInputTooLarge) as excinfo:
            await provider.embed_one("hello")

    reason = excinfo.value.reason
    assert len(reason) <= MAX_PROVIDER_REASON_CHARS
    assert secret_tail not in reason
    # Bounded, but still the vendor's own words up to the bound.
    assert reason.startswith("This model's maximum context length is 8192 tokens")
    assert reason.endswith("…")


# ── Ollama: the equivalent, where it reports instead of truncating ──────────


@pytest.mark.asyncio
async def test_ollama_input_length_error_raises_the_typed_exception(ollama_settings):
    provider = OllamaProvider()
    with respx.mock(base_url="http://ollama:11434") as mock:
        route = mock.post("/api/embed").mock(
            return_value=Response(400, json=OLLAMA_INPUT_LENGTH)
        )
        with pytest.raises(refusals.ProviderInputTooLarge) as excinfo:
            await provider.embed_one("a very long query")

    exc = excinfo.value
    assert exc.provider == "ollama"
    assert exc.reason == "input length exceeds maximum context length"
    assert route.call_count == 1


@pytest.mark.asyncio
async def test_ollama_generic_400_still_propagates_as_today(ollama_settings):
    provider = OllamaProvider()
    with respx.mock(base_url="http://ollama:11434") as mock:
        mock.post("/api/embed").mock(
            return_value=Response(400, json={"error": "model 'bge-m3' not found"})
        )
        with pytest.raises(httpx.HTTPStatusError):
            await provider.embed_one("hello")


@pytest.mark.asyncio
async def test_ollama_5xx_still_propagates_as_today(ollama_settings):
    provider = OllamaProvider()
    with respx.mock(base_url="http://ollama:11434") as mock:
        mock.post("/api/embed").mock(
            return_value=Response(500, json={"error": "internal error"})
        )
        with pytest.raises(httpx.HTTPStatusError):
            await provider.embed_one("hello")


@pytest.mark.asyncio
async def test_ollama_batch_propagates_the_typed_exception(ollama_settings):
    """`embed_batch` wraps each call in `asyncio.wait_for`; the exception must
    come out of it as itself, not as a `TimeoutError` or a swallowed None."""
    provider = OllamaProvider()
    with respx.mock(base_url="http://ollama:11434") as mock:
        route = mock.post("/api/embed")
        route.side_effect = [
            Response(200, json={"embeddings": [[0.1, 0.2, 0.3]]}),
            Response(400, json=OLLAMA_INPUT_LENGTH),
        ]
        with pytest.raises(refusals.ProviderInputTooLarge):
            await provider.embed_batch(["short", "very long"])
    assert route.call_count == 2


@pytest.mark.asyncio
async def test_ollama_truncation_is_still_the_ordinary_path(ollama_settings):
    """The default deployment **truncates** — a long input answers 200 with a
    vector over the model's context window. Nothing here refuses it, and this
    is why the live exercise cannot cover the translation branch."""
    provider = OllamaProvider()
    with respx.mock(base_url="http://ollama:11434") as mock:
        mock.post("/api/embed").mock(
            return_value=Response(200, json={"embeddings": [[0.5] * 3]})
        )
        out = await provider.embed_one("字" * 20_000)
    assert out == [0.5, 0.5, 0.5]


# ── The character cap does not pre-empt the provider ────────────────────────


@pytest.mark.asyncio
async def test_a_dense_non_ascii_query_under_the_cap_reaches_the_provider(
    ollama_settings,
):
    """The whole reason this branch exists: a query *within* the character cap
    is sent verbatim, and it is the provider that decides. Nothing in the
    embedding path second-guesses the cap with a byte or token estimate."""
    query = "漢" * MAX_SEARCH_QUERY_CHARS
    assert len(query) == MAX_SEARCH_QUERY_CHARS
    # Three bytes per character: well past the cap in bytes, exactly at it in
    # characters, which is the mismatch the cap cannot speak to.
    assert len(query.encode("utf-8")) == 3 * MAX_SEARCH_QUERY_CHARS

    provider = OllamaProvider()
    with respx.mock(base_url="http://ollama:11434") as mock:
        route = mock.post("/api/embed").mock(
            return_value=Response(200, json={"embeddings": [[0.1]]})
        )
        out = await provider.embed_one(query)

    assert out == [0.1]
    body = json.loads(route.calls[0].request.read())
    assert body["input"] == query


# ── The detection rule itself ───────────────────────────────────────────────


# ── A specific code the provider chose is evidence *against* this branch ───


@pytest.mark.parametrize(
    "code,message",
    [
        # The finding: OpenAI answers an unknown deployment with a 400 whose
        # message happens to contain a size complaint. Read as an input limit,
        # it told the agent to shorten a query that was never the problem —
        # and no amount of shortening would ever succeed, while the real fault
        # (a misconfigured model name, an operator's problem) never surfaced.
        ("invalid_model", "identifier exceeds maximum length"),
        ("model_not_found", "The model `nonesuch` does not exist"),
        ("invalid_api_key", "Incorrect API key provided: sk-***. Maximum length"),
        ("unsupported_value", "input must be a string of maximum length 64"),
    ],
)
def test_an_unrecognised_code_is_never_read_as_an_input_limit(code, message):
    """A code is the provider naming the fault. One we do not recognise is a
    positive statement that this is something else, so the prose fallback must
    not overrule it."""
    response = Response(400, json={"error": {"message": message, "code": code}})
    assert (
        embeddings._input_limit_reason(
            response, codes=embeddings._OPENAI_INPUT_LIMIT_CODES
        )
        is None
    )


def test_a_coarse_type_alone_does_not_suppress_the_prose_fallback():
    """`type` is a bucket (`invalid_request_error`) that accompanies input
    limits and unknown models alike, so it says nothing either way — and
    treating it as a code would silence every gateway that sends only a type.
    The distinction between `code` and `type` is what makes both true."""
    response = Response(
        422,
        json={
            "error": {
                "message": "input is too long for this model's context length",
                "type": "invalid_request_error",
            }
        },
    )
    assert embeddings._input_limit_reason(
        response, codes=embeddings._OPENAI_INPUT_LIMIT_CODES
    ) == "input is too long for this model's context length"


@pytest.mark.parametrize(
    "message",
    [
        # Code-less bodies (the Ollama shape) whose prose is a size complaint
        # about something that is not the input.
        "identifier exceeds maximum length",
        "name is too long",
        "the requested adapter exceeds the maximum length",
        "file too large",
    ],
)
def test_a_size_complaint_about_something_else_is_not_an_input_limit(message):
    """Every phrase must pair the complaint with what was too large. A bare
    `maximum length` — which the pattern list used to carry — matches sentences
    that have nothing to do with the caller's query."""
    assert (
        embeddings._input_limit_reason(
            Response(400, json={"error": message}), codes=frozenset()
        )
        is None
    )


@pytest.mark.parametrize(
    "message",
    [
        "input length exceeds maximum context length",
        "prompt is too long",
        "too many tokens in the request",
        "reduce your input and try again",
        "the query exceeds the maximum for this model",
    ],
)
def test_a_size_complaint_about_the_input_still_matches(message):
    """The other direction, so the tightening cannot silently become a
    refusal to translate anything."""
    assert (
        embeddings._input_limit_reason(
            Response(400, json={"error": message}), codes=frozenset()
        )
        is not None
    )


def test_a_429_is_never_read_as_an_input_limit():
    """Belt and braces on the status gate: even a 429 whose prose names the
    context length is not an input-limit rejection. It is retryable, and the
    provider branches must never see a reason for it."""
    response = Response(
        429,
        json={"error": {"message": "maximum context length", "code": "rate_limited"}},
    )
    assert (
        embeddings._input_limit_reason(
            response, codes=embeddings._OPENAI_INPUT_LIMIT_CODES
        )
        is None
    )


@pytest.mark.parametrize(
    "response",
    [
        Response(400, text="<html><body>502 Bad Gateway</body></html>"),
        Response(400, text=""),
        Response(400, json={"error": None}),
        Response(400, json=["not", "a", "dict"]),
        Response(400, json={"error": {"message": 17, "code": 42}}),
    ],
    ids=["html", "empty", "null-error", "list", "non-string-fields"],
)
def test_a_malformed_error_body_never_raises(response):
    """A detection helper that threw would convert a provider error into an
    internal one, which is strictly worse than not translating it."""
    assert (
        embeddings._input_limit_reason(
            response, codes=embeddings._OPENAI_INPUT_LIMIT_CODES
        )
        is None
    )


def test_a_code_match_with_an_empty_message_still_yields_a_reason():
    """The reason is interpolated into a caller-facing sentence, so it is
    never empty — the code stands in when the vendor sent no prose."""
    response = Response(400, json={"error": {"code": "context_length_exceeded"}})
    reason = embeddings._input_limit_reason(
        response, codes=embeddings._OPENAI_INPUT_LIMIT_CODES
    )
    assert reason == "context_length_exceeded"


def test_the_ollama_code_set_is_empty_by_construction():
    """Ollama sends a bare string; if that ever changes, this is the seam."""
    assert embeddings._OLLAMA_INPUT_LIMIT_CODES == frozenset()


# ── The indexer path ────────────────────────────────────────────────────────


class _FakeNote:
    def __init__(self):
        self.id = 7
        self.file_path = "notes/dense.md"
        self.content_hash = "newhash"
        self.embedded_content_hash = "oldhash"


class _StateResult:
    def scalar_one_or_none(self):
        return None


class _RecordingSession:
    """The minimal `AsyncSession` stand-in `embed_note`'s failure path needs.

    Copied in shape from `tests/test_issue_11_...`: it answers the advisory
    lock and the `indexer_state` fingerprint read, and records whether the
    DELETE of existing vectors was staged.
    """

    def __init__(self):
        self.delete_executed = False
        self.added: list = []
        self.lock_taken = False

    async def execute(self, stmt, params=None):
        if isinstance(stmt, TextClause):
            if "pg_advisory_xact_lock" in stmt.text:
                self.lock_taken = True
            elif "indexer_state" in stmt.text:
                return _StateResult()
            return None
        if isinstance(stmt, delete(NoteEmbedding).__class__):
            self.delete_executed = True
        return None

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        return None


@pytest.mark.asyncio
async def test_embed_note_records_provider_failed_with_the_class_name(monkeypatch):
    """The indexer is not a caller that can shorten its input, so there is
    nobody to translate the refusal for. The pass record is the ordinary
    `PROVIDER_FAILED` carrying the class name — and it must stay ordinary: a
    special case here would either hide a real failure from `indexer_runs` or
    certify a note the provider never embedded."""
    session = _RecordingSession()
    note = _FakeNote()

    async def _reject(_chunks):
        raise refusals.ProviderInputTooLarge(
            "input length exceeds maximum context length", provider="ollama"
        )

    monkeypatch.setattr(embeddings, "get_embeddings_batch", _reject)

    result = await embeddings.embed_note(session, note, "some real content here")

    assert result.outcome is NoteEmbedOutcome.PROVIDER_FAILED
    assert result.chunks_submitted >= 1
    assert result.chunks_embedded == 0
    assert result.failure is not None
    # The class name, so an operator reading `indexer_runs.error` can tell this
    # apart from an outage without the exception being in hand.
    assert result.failure.exc_type == "ProviderInputTooLarge"
    assert "maximum context length" in result.failure.message
    # And the #11 invariant is untouched: nothing deleted, nothing certified.
    assert session.delete_executed is False
    assert session.added == []
    assert note.embedded_content_hash == "oldhash"
