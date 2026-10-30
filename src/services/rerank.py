"""The optional cross-encoder reranker (#367).

An operator-configured endpoint (`RERANK_URL`, a Jina-compatible
`/v1/rerank`, as `llama-server --reranking` serves it) reorders the
candidates `semantic_search` / `hybrid_search` already fetched, when a call
passes `rerank=true`. Everything here is **all or nothing**: a response is
either a fully validated score for every candidate, or it is not used at all
and the caller renders the unreranked page with one closed reason code. The
rerank step can never fail a search (design D5, D7).

What this module owns:

* the process client (`rerank_http_client`, ≤ 2 connections), created lazily
  and closed at shutdown (D3);
* `score` — one exchange under one wall-clock deadline, the body read under a
  1 MiB cap, validated in the D7 order, scores mapped by `index`;
* `rerank_candidates` — the one function the tools and the harness arms share:
  state precedence, document building, the order, the WARNING suppressor;
* the startup probe (both document orders), the background re-probe, and the
  process state `/health` reports (D8).

Only validated numbers and closed codes leave this module. The response body,
the URL and exception messages are never rendered and never logged.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from dataclasses import dataclass, field

import httpx

from src.config import settings

logger = logging.getLogger(__name__)

#: At most this many notes are sent to the reranker per call (D4).
RERANK_MAX_CANDIDATES = 50
#: One document — title, two newlines and candidate text — is at most this
#: many characters (code points), cut at a code-point boundary (D4).
RERANK_DOC_CHARS = 2048
#: The response body is read under this cap; past it, `oversize_response`.
RERANK_MAX_RESPONSE_BYTES = 1 << 20
#: The startup probe's deadline for both requests together: the first
#: request after a reranker cold start may load weights (D8).
RERANK_PROBE_TIMEOUT_S = 10
#: The re-probe backoff (D8): 60 s, doubling after each failure, up to 600 s.
REPROBE_INITIAL_S = 60
REPROBE_MAX_S = 600
#: The WARNING suppressor's window (D7).
WARNING_WINDOW_S = 60

# ── The closed reason-code set (D7), in precedence order ────────────────────
NOT_CONFIGURED = "not_configured"
PROBE_SKIPPED = "probe_skipped"
PROBE_FAILED = "probe_failed"
NO_CANDIDATES = "no_candidates"
TIMEOUT = "timeout"
TRANSPORT_ERROR = "transport_error"
HTTP_ERROR = "http_error"
OVERSIZE_RESPONSE = "oversize_response"
MALFORMED_RESPONSE = "malformed_response"
COUNT_MISMATCH = "count_mismatch"
BAD_INDEX = "bad_index"
NON_FINITE_SCORE = "non_finite_score"

REASON_CODES = (
    NOT_CONFIGURED, PROBE_SKIPPED, PROBE_FAILED, NO_CANDIDATES, TIMEOUT,
    TRANSPORT_ERROR, HTTP_ERROR, OVERSIZE_RESPONSE, MALFORMED_RESPONSE,
    COUNT_MISMATCH, BAD_INDEX, NON_FINITE_SCORE,
)

#: Probe diagnostic codes (log only, never rendered).
PROBE_WRONG_ORDER = "probe_wrong_order"
PROBE_TIE = "probe_tie"

# ── The probe pair (D8) ─────────────────────────────────────────────────────
PROBE_QUERY = "How do I reset a forgotten account password?"
PROBE_RELEVANT = (
    "To reset a forgotten password, open the sign-in page, choose \"Forgot "
    "password\" and follow the link sent to your email."
)
PROBE_IRRELEVANT = (
    "Sourdough bread needs a starter, flour, water and salt, and a long cold "
    "fermentation."
)

# ── Process state (D8) ──────────────────────────────────────────────────────
STATE_ENABLED = "enabled"
STATE_DISABLED = "disabled"
STATE_ABSENT = "absent"

#: `None` until a probe ran (or sandbox mode marked it skipped); a call made
#: before any probe answers `probe_failed`, exactly as a disabled one does.
_state: str | None = None
#: Why the state is `disabled`: `probe_failed` or `probe_skipped`.
_disabled_code: str = PROBE_FAILED

_client: httpx.AsyncClient | None = None


class RerankFailure(Exception):
    """One rejected exchange: `code` from the closed set, `exc_class` the
    exception type name behind it (or `None`). Returned, not raised, by
    `score`; it subclasses `Exception` only so tests can match it."""

    def __init__(self, code: str, exc_class: str | None = None):
        super().__init__(code)
        self.code = code
        self.exc_class = exc_class


@dataclass
class RerankOutcome:
    """The result of `rerank_candidates`.

    `order` is a permutation of the candidate indices: the reranked order when
    `reranked`, otherwise the identity (the unreranked order). `scores[i]` is
    candidate `i`'s validated score, only when `reranked`.
    """

    reranked: bool
    order: list[int]
    candidates: int
    scores: list[float] | None = None
    reason: str | None = None
    #: The exchange time in ms, present when a request was sent.
    ms: int | None = None
    extra: dict = field(default_factory=dict)

    @property
    def token(self) -> str:
        """The header token every `rerank=true` response carries."""
        if self.reranked:
            return f"reranked: true (candidates: {self.candidates})"
        return f"reranked: false ({self.reason})"


# ── State ──────────────────────────────────────────────────────────────────


def configured() -> bool:
    return bool(settings.rerank_url)


def health_state() -> str:
    """`enabled | disabled | absent` — never the URL, a code or a count."""
    if not configured():
        return STATE_ABSENT
    return STATE_ENABLED if _state == STATE_ENABLED else STATE_DISABLED


def mark_sandbox() -> None:
    """`MCP_SANDBOX_MODE`: no probe; `disabled` (`probe_skipped`) when a URL
    is set, else nothing (the state reads `absent`)."""
    global _state, _disabled_code
    if configured():
        _state, _disabled_code = STATE_DISABLED, PROBE_SKIPPED


def _set_enabled() -> None:
    global _state
    _state = STATE_ENABLED


def _set_disabled(code: str = PROBE_FAILED) -> None:
    global _state, _disabled_code
    _state, _disabled_code = STATE_DISABLED, code


def _reset_state_for_tests() -> None:
    global _state, _disabled_code
    _state, _disabled_code = None, PROBE_FAILED
    _warned.clear()


# ── Client lifecycle (D3) ──────────────────────────────────────────────────


_client_loop: asyncio.AbstractEventLoop | None = None


def _get_client() -> httpx.AsyncClient:
    """The process client, rebuilt when it was closed or belongs to another
    event loop (the server has one loop; a harness or test process may run
    several in turn, and a pooled connection cannot cross loops)."""
    global _client, _client_loop
    loop = asyncio.get_running_loop()
    if _client is None or _client.is_closed or _client_loop is not loop:
        from src.services.transport_security import rerank_http_client

        _client = rerank_http_client(settings.rerank_timeout_ms / 1000)
        _client_loop = loop
    return _client


async def close_rerank_client() -> None:
    global _client, _client_loop
    client, loop, _client, _client_loop = _client, _client_loop, None, None
    if client is not None and not client.is_closed and loop is asyncio.get_running_loop():
        await client.aclose()


# ── The exchange (D2, D3, D7) ──────────────────────────────────────────────


def _validate(body: bytes, n: int) -> list[float] | RerankFailure:
    """D7 validation, in order: malformed, count, index, finiteness. Scores
    are mapped by `index`, never by list position."""
    try:
        data = json.loads(body)
    except (ValueError, RecursionError):
        return RerankFailure(MALFORMED_RESPONSE)
    if not isinstance(data, dict):
        return RerankFailure(MALFORMED_RESPONSE)
    results = data.get("results")
    if not isinstance(results, list):
        return RerankFailure(MALFORMED_RESPONSE)
    for item in results:
        if not isinstance(item, dict):
            return RerankFailure(MALFORMED_RESPONSE)
        index, score_ = item.get("index"), item.get("relevance_score")
        # A bool index is an `int` to Python; it is classified as `bad_index`
        # below, not as malformed.
        if not isinstance(index, int):
            return RerankFailure(MALFORMED_RESPONSE)
        if isinstance(score_, bool) or not isinstance(score_, (int, float)):
            return RerankFailure(MALFORMED_RESPONSE)
    if len(results) != n:
        return RerankFailure(COUNT_MISMATCH)
    scores: list[float | None] = [None] * n
    for item in results:
        index = item["index"]
        if isinstance(index, bool) or not 0 <= index < n or scores[index] is not None:
            return RerankFailure(BAD_INDEX)
        try:
            value = float(item["relevance_score"])
        except OverflowError:
            value = math.inf
        scores[index] = value
    if not all(math.isfinite(s) for s in scores):  # type: ignore[arg-type]
        return RerankFailure(NON_FINITE_SCORE)
    return scores  # type: ignore[return-value]


async def score(
    query: str, documents: list[str], *, deadline_s: float
) -> list[float] | RerankFailure:
    """One `POST` of the D2 body under one wall-clock deadline (pool wait,
    connect, write and read), the body streamed under the 1 MiB cap, then the
    D7 validation. Never retried. `CancelledError` propagates; every other
    exception becomes a code."""
    n = len(documents)
    body = {
        "model": settings.rerank_model,
        "query": query,
        "documents": documents,
        "top_n": n,
    }
    try:
        async with asyncio.timeout(deadline_s):
            client = _get_client()
            async with client.stream(
                "POST", settings.rerank_url, json=body,
                # Asked for uncompressed, and read raw: the cap is on the
                # bytes this process holds, never on a decoder's output.
                headers={"Accept-Encoding": "identity"},
                # The per-phase backstop is the call's own deadline.
                timeout=httpx.Timeout(deadline_s),
            ) as response:
                # 3xx included: redirects are never followed (D7).
                if not 200 <= response.status_code < 300:
                    return RerankFailure(HTTP_ERROR)
                declared = response.headers.get("content-length")
                if declared is not None:
                    try:
                        if int(declared) > RERANK_MAX_RESPONSE_BYTES:
                            return RerankFailure(OVERSIZE_RESPONSE)
                    except ValueError:
                        return RerankFailure(MALFORMED_RESPONSE)
                buf = bytearray()
                async for chunk in response.aiter_raw():
                    buf += chunk
                    if len(buf) > RERANK_MAX_RESPONSE_BYTES:
                        return RerankFailure(OVERSIZE_RESPONSE)
                encoding = response.headers.get("content-encoding", "identity")
                if encoding.lower() not in ("", "identity"):
                    # Not requested and not decoded: a compressed body is not
                    # a body this validator can bound. Fail closed.
                    return RerankFailure(MALFORMED_RESPONSE)
    except asyncio.CancelledError:
        raise
    except (TimeoutError, httpx.TimeoutException) as exc:
        return RerankFailure(TIMEOUT, type(exc).__name__)
    except httpx.TransportError as exc:
        return RerankFailure(TRANSPORT_ERROR, type(exc).__name__)
    except Exception as exc:  # noqa: BLE001 - the rerank step never fails a search
        return RerankFailure(MALFORMED_RESPONSE, type(exc).__name__)
    return _validate(bytes(buf), n)


# ── The WARNING suppressor (D7) ────────────────────────────────────────────

#: code → [window start (monotonic), suppressed count]
_warned: dict[str, list] = {}


def _warn(code: str, exc_class: str | None) -> None:
    """At most one WARNING per code per 60 s, carrying the count suppressed
    since the previous one. Never the URL, the response or note text."""
    now = time.monotonic()
    entry = _warned.get(code)
    if entry is not None and now - entry[0] < WARNING_WINDOW_S:
        entry[1] += 1
        return
    suppressed = entry[1] if entry is not None else 0
    _warned[code] = [now, 0]
    logger.warning(
        "rerank: falling back to the unreranked page (code=%s exc=%s "
        "suppressed_since_last=%d)",
        code,
        exc_class or "-",
        suppressed,
    )


# ── Documents and the shared rerank step (D4, D5) ──────────────────────────


def build_document(title: str | None, text: str | None) -> str:
    """`title + "\\n\\n" + text`, or the title alone when there is no text,
    cut to `RERANK_DOC_CHARS` code points (Python slices whole code points)."""
    title = title or ""
    doc = f"{title}\n\n{text}" if text else title
    return doc[:RERANK_DOC_CHARS]


def bounded_text(title: str | None, text: str | None) -> str | None:
    """The candidate text bounded so `title + "\\n\\n" + text` fits
    `RERANK_DOC_CHARS`; `None` stays `None` (a stale row sends its title)."""
    if text is None:
        return None
    room = RERANK_DOC_CHARS - len(title or "") - 2
    return text[: max(0, room)]


async def rerank_candidates(
    query: str, candidates: list[tuple[str | None, str | None]]
) -> RerankOutcome:
    """Rerank `(title, text)` candidates, all or nothing.

    The state precedence (`not_configured`, `probe_skipped`, `probe_failed`,
    `no_candidates`) sends no request. Otherwise one exchange; on success the
    order is `(-score, unreranked position)`, on any failure the identity
    order with the code. Any exception other than cancellation falls back as
    `malformed_response`.
    """
    n = len(candidates)
    identity = list(range(n))
    if not configured():
        return RerankOutcome(False, identity, n, reason=NOT_CONFIGURED)
    if _state != STATE_ENABLED:
        code = _disabled_code if _state == STATE_DISABLED else PROBE_FAILED
        return RerankOutcome(False, identity, n, reason=code)
    if n == 0:
        return RerankOutcome(False, identity, 0, reason=NO_CANDIDATES)
    started = time.monotonic()
    try:
        documents = [build_document(t, x) for t, x in candidates]
        result = await score(query, documents, deadline_s=settings.rerank_timeout_ms / 1000)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - the rerank step never fails a search
        result = RerankFailure(MALFORMED_RESPONSE, type(exc).__name__)
    ms = int((time.monotonic() - started) * 1000)
    if isinstance(result, RerankFailure):
        _warn(result.code, result.exc_class)
        return RerankOutcome(False, identity, n, reason=result.code, ms=ms)
    order = sorted(identity, key=lambda i: (-result[i], i))
    return RerankOutcome(True, order, n, scores=result, ms=ms)


# ── The probe (D8) ─────────────────────────────────────────────────────────


async def probe_attempt(deadline_s: float) -> str | None:
    """Both document orders under one deadline for the pair; `None` on a
    pass, else one diagnostic code (an exchange code, `probe_wrong_order` or
    `probe_tie`). Scores are mapped by `index`, so a position-only scorer
    loses the second order."""
    loop = asyncio.get_running_loop()
    end = loop.time() + deadline_s
    for relevant_at, docs in (
        (0, [PROBE_RELEVANT, PROBE_IRRELEVANT]),
        (1, [PROBE_IRRELEVANT, PROBE_RELEVANT]),
    ):
        remaining = end - loop.time()
        if remaining <= 0:
            return TIMEOUT
        result = await score(PROBE_QUERY, docs, deadline_s=remaining)
        if isinstance(result, RerankFailure):
            return result.code
        relevant, irrelevant = result[relevant_at], result[1 - relevant_at]
        if relevant == irrelevant:
            return PROBE_TIE
        if relevant < irrelevant:
            return PROBE_WRONG_ORDER
    return None


async def startup_probe() -> bool:
    """One attempt under `RERANK_PROBE_TIMEOUT_S`. A pass enables reranking
    with one INFO line; a failure disables it with exactly one CRITICAL naming
    the code. Never exits: a reranker outage must not take down the vault."""
    try:
        code = await probe_attempt(RERANK_PROBE_TIMEOUT_S)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - never fatal
        code = MALFORMED_RESPONSE
        logger.debug("rerank probe raised %s", type(exc).__name__)
    if code is None:
        _set_enabled()
        logger.info("Reranker probe passed: reranking enabled")
        return True
    _set_disabled(PROBE_FAILED)
    logger.critical(
        "Reranker probe failed (code=%s): rerank=true calls answer "
        "'reranked: false (probe_failed)' until a background re-probe passes",
        code,
    )
    return False


async def reprobe_loop(sleep=asyncio.sleep) -> None:
    """While disabled: 60 s, then doubling to 600 s, one attempt each under
    `RERANK_TIMEOUT_MS`. Failures log at DEBUG; a pass enables, logs one INFO
    line and ends the task. Holds no database session and no lock."""
    interval = REPROBE_INITIAL_S
    while True:
        await sleep(interval)
        try:
            code = await probe_attempt(settings.rerank_timeout_ms / 1000)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a re-probe never kills the task
            code = MALFORMED_RESPONSE
            logger.debug("rerank re-probe raised %s", type(exc).__name__)
        if code is None:
            _set_enabled()
            logger.info("Reranker re-probe passed: reranking enabled")
            return
        logger.debug("Reranker re-probe failed (code=%s); next in %ss", code,
                     min(interval * 2, REPROBE_MAX_S))
        interval = min(interval * 2, REPROBE_MAX_S)


async def stop_task(task: asyncio.Task | None) -> None:
    """Cancel the re-probe task and wait for it, best-effort."""
    if task is None:
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001 - shutdown
        pass
