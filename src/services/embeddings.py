import asyncio
import enum
import logging
import re
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Callable, Protocol

import httpx
import numpy as np
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import (
    MAX_CHUNKS_PER_NOTE,
    MAX_EMBED_FAILURE_MESSAGE_CHARS,
    settings,
)
from src.models.db import NoteEmbedding, NoteMetadata
from src.services import refusals, timing
from src.services.filters import apply_note_filters
from src.services.index_state import (
    KEY_EMBEDDING_FINGERPRINT,
    FingerprintStatus,
    acquire_generation_lock,
    compare_fingerprint,
    embedding_fingerprint,
    get_state,
    state_table_exists,
)
from src.services.links import BODY, scan_fences

logger = logging.getLogger(__name__)

# Strip fenced code blocks before embedding so serialized data dumps
# (Excalidraw JSON, base64 blobs, mermaid graphs) don't dominate vector
# space. Keyword search is unaffected — tsvector still indexes everything.
#
# The grammar is `src/services/links.py`'s shared recognizer, not a private
# one. This module carried its own pair of regexes (LF-only, column-zero,
# exact-length closer) until #150: they disagreed with the masker heading
# resolution uses, so an indented or longer-closed block was embedded as prose
# while the same block was invisible to `read_note(section=…)`.


def clean_for_embedding(content: str) -> str:
    """Strip fenced code blocks (``` and ~~~) from markdown before embedding.

    `content` is a note's post-frontmatter **body**, so the recognizer runs in
    `BODY` context and never re-partitions. Inline backtick code is preserved
    (typically short identifiers, often semantically meaningful). Indented
    code blocks are not stripped — they're ambiguous with regular indented
    prose in personal notes, and are a documented divergence of the grammar.
    """
    return _remove_spans(content, scan_fences(content, context=BODY).spans)


def _remove_spans(text: str, spans) -> str:
    if not spans:
        return text
    out: list[str] = []
    cursor = 0
    for start, end in spans:
        out.append(text[cursor:start])
        cursor = end
    out.append(text[cursor:])
    return "".join(out)


# ── The frozen per-version cleaners (`notes_metadata.extraction_version`)
#
# A grammar change does not change a note's bytes, so `content_hash` cannot
# see it and the embed backlog would certify stale vectors forever. The
# indexer therefore compares what a note's **stamped** version would have
# embedded against what the **current** one embeds, and clears
# `embedded_content_hash` only when the two differ
# (`CURRENT_EXTRACTION_VERSION` in `src/services/indexer.py`).
#
# **The comparison is over cleaned OUTPUT, never over recognised spans.** Span
# equality is neither necessary nor sufficient for embedded-text equality,
# because v0's cleaner applied its two regexes *sequentially*: the first
# substitution changed the text the second matched against, and the two
# patterns' `$`-anchored spans could overlap. Both directions have real
# inputs, and both are pinned in `tests/test_clean_for_embedding.py`:
#
#   "~~~\ncode\n~~~\n```\n# H\ncode\n```\n[[X]]\n"
#       identical spans under v0 and v1, DIFFERENT cleaned text — span
#       comparison would wrongly certify the stale vector.
#   "```\n~~~\ncode\n~~~\n```"
#       different spans, IDENTICAL cleaned text — span comparison would
#       re-embed for nothing.
#
# So what is registered is each version's whole cleaning function. Only the
# *embedding* cleaner needs a frozen history: links and tags are re-derived
# unconditionally whenever the marker is stale, so they need no
# version-to-version diff. Version 0 is the exact pair of regexes this module
# used before #150, applied in the exact order it applied them; the entry
# becomes removable once no row is stamped 0.
#
# The comparison is direction-aware by construction, which is what makes a
# rollback work: revert the grammar, bump the current version, and a
# v1-stamped row compares frozen v1 against the restored cleaner and
# invalidates exactly the notes that change — see the rollback recipe in
# `docs/architecture/indexing-and-embeddings.md`.
# The v0 cleaner WAS this pair of regexes, applied sequentially:
#
#   _V0_FENCE_BACKTICK_RE = r"^```[^\n]*\n.*?\n```\s*$"   MULTILINE | DOTALL
#   _V0_FENCE_TILDE_RE    = r"^~~~[^\n]*\n.*?\n~~~\s*$"   MULTILINE | DOTALL
#
# Both are quadratic in the number of UNCLOSED openers: each `.*?` walks to
# the end of the input before the attempt at that opener fails, and `^` then
# retries at the next line (issue #180). A note of `` ```x\n `` repeated is
# ordinary, in-cap input, and this cleaner runs on the event loop inside the
# indexer, so that is a cross-tenant stall.
#
# `_v0_clean` below is a LINE SCANNER whose output must stay byte-identical to
# that pair forever — `extraction_version` comparisons and the documented
# rollback recipe depend on it. The regexes themselves now live in
# `tests/test_asvs_v0_cleaner.py` as the differential test's ORACLE; the test
# is the proof, not this comment. What the scanner has to reproduce, in the
# regex's terms rather than CommonMark's — every clause below was established
# empirically against the oracle, and several are the opposite of the v1
# fence grammar above:
#
# * `^`/`$` under `re.MULTILINE` anchor on `\n` and NOTHING else. Split on
#   `\n` only — never `str.splitlines()` (which breaks on `\v`, `\f`, `\x1c`,
#   ` `, …), never the fence recognizer's `_LINE_BREAK_RE` (which treats
#   a lone `\r` as a terminator). A lone `\r` is an ordinary character here.
# * OPENER — a line whose first three characters are the fence run. No
#   indent is allowed, and the rest of the line is an unrestricted info
#   string (so a 4-backtick line IS an opener, with `` ` `` as its info).
# * CLOSER — the NEAREST following line, at least two lines below the opener,
#   whose first three characters are the same fence run and whose remainder
#   is entirely `\s` under **Unicode** semantics. So `` ```\xa0 ``,
#   `` ```\x0b `` and `` ```  `` all close — the exact opposite of the
#   v1 grammar, which admits only U+0020 and U+0009. A run LONGER than three
#   (`` ```` ``) leaves a backtick in the remainder and does not close.
# * EMPTY BLOCK — an opener immediately followed by a closer is NOT removed.
#   `.*?` sits between two `\n`s, so the closer must be at least two lines
#   below the opener; `` ```\n``` `` survives untouched.
# * TRAILING RUN — `\s*$` after the closer's fence run is greedy and then
#   backtracks to the LAST position where `$` holds, so it swallows whole
#   blank lines after the closer, stopping at the newline before the first
#   line that has a non-`\s` character (or running to end of input). In line
#   terms: the removal covers the opener line through the last all-`\s` line
#   after the closer, and the newline that ends it is what survives — which
#   is exactly "replace those lines with one empty line".
# * ORDER — backtick pass, then tilde pass, over the FIRST pass's output. The
#   two patterns' `$`-anchored spans can overlap, so the order is part of the
#   behaviour, not an implementation detail (see the two pinned inputs above).
_V0_NON_WS_RE = re.compile(r"[^\s]")


def _v0_sub_fences(body: str, fence: str) -> str:
    """One frozen-v0 pass, linear in `len(body)`.

    Byte-identical to `re.sub(rf"^{fence}[^\\n]*\\n.*?\\n{fence}\\s*$", "",
    body, flags=MULTILINE | DOTALL)` — see the comment above for the clause
    list and `tests/test_asvs_v0_cleaner.py` for the differential proof.
    """
    if fence not in body:
        return body
    lines = body.split("\n")
    n = len(lines)

    # A line that would satisfy `\n{fence}\s*$` — i.e. a legal closer.
    is_closer = [
        line.startswith(fence) and _V0_NON_WS_RE.search(line, len(fence)) is None
        for line in lines
    ]
    # `next_closer[k]` — the smallest closer index >= k, or n for "none". One
    # backward pass, so an opener never rescans the tail: that rescan is the
    # quadratic step this function exists to remove.
    next_closer = [n] * (n + 1)
    for k in range(n - 1, -1, -1):
        next_closer[k] = k if is_closer[k] else next_closer[k + 1]

    out: list[str] = []
    i = 0
    while i < n:
        # `.*?` spans at least one line boundary, so the closer sits at least
        # two lines below the opener.
        if lines[i].startswith(fence) and i + 2 < n and next_closer[i + 2] < n:
            j = next_closer[i + 2]
            # `\s*$` swallows every wholly-blank line after the closer.
            m = j + 1
            while m < n and _V0_NON_WS_RE.search(lines[m]) is None:
                m += 1
            out.append("")
            i = m
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)


def _v0_clean(body: str) -> str:
    """`clean_for_embedding` exactly as it stood before #150. Frozen.

    **Sequential** substitution included: the order is part of the behaviour
    being reproduced, not an implementation detail. Do not "simplify" it into
    one pass or into a span set. The two passes are line scanners rather than
    the original regexes only because the regexes were quadratic; the OUTPUT
    is frozen and any change to it is a data-integrity bug.
    """
    body = _v0_sub_fences(body, "```")
    body = _v0_sub_fences(body, "~~~")
    return body


_EXTRACTION_CLEANERS = {
    0: _v0_clean,
    1: clean_for_embedding,
    # Version 2 is the version-1 grammar under a new key. The link grammar
    # changed (#180), so `CURRENT_EXTRACTION_VERSION` has to move for the
    # indexer to re-derive `note_links` — but the *embedding* text is
    # untouched, so a v1-stamped row must compare equal here and NOT be
    # re-embedded. Binding the same function is what makes that true by
    # construction rather than by inspection.
    2: clean_for_embedding,
}


def clean_at_version(version: int, body: str) -> str | None:
    """What version `version` would have embedded for `body`, or None.

    None means "no frozen cleaner for that version", which the caller must
    treat as *differs* — a row stamped with a grammar this build cannot
    reproduce (a downgrade past a bump) must be re-embedded rather than
    certified against a comparison that was never made.
    """
    cleaner = _EXTRACTION_CLEANERS.get(version)
    return cleaner(body) if cleaner is not None else None


def chunk_text_bounded(
    content: str,
    *,
    chunk_size: int = 512,
    overlap: int = 0,
    max_chunks: int = MAX_CHUNKS_PER_NOTE,
) -> tuple[list[str], bool]:
    """The first `max_chunks` chunks in DOCUMENT order, and whether it capped.

    The bounded sibling `chunk_text` delegates to, in the shape #203 gave
    `extract_links_bounded`. `MAX_NOTE_BYTES` is 10 MiB and `CHUNK_SIZE` is 512
    tokens (~4 characters each), so one *legal* note is ~5,120 chunks — each of
    them one sequential, 30 s-bounded provider call, under `index_pass_lock`,
    with no LIMIT on the backlog behind it. Re-editing one such note kept every
    later tenant's notes out of the index indefinitely (#202).

    **Document order, and the head rather than an arbitrary window**, so a
    capped note keeps the part a reader would call the note. A capped note is a
    *declared degradation*, never a skip: the caller certifies it through the
    ordinary conditional stamp (an uncertified note is re-selected by the
    backlog on every tick for ever — #127's permanent burn arriving by a new
    route), sets `notes_metadata.chunks_truncated`, and both vector paths
    report `embedding_truncated`.

    The second element is `True` only when a chunk was actually *dropped*: a
    note that lands on exactly `max_chunks` is complete and is not marked. That
    is settled by generating one chunk past the cap and discarding it — one
    extra window, never the unbounded chunking the cap exists to prevent, which
    is also why nothing here can ever report the note's true chunk count.

    `step = max(char_size - char_overlap, 1)` is #10's infinite-loop guard and
    stays. It is a floor, not a sane configuration: `Settings` refuses
    `CHUNK_OVERLAP >= CHUNK_SIZE` at startup, because at the floor ~3 KB of
    prose becomes ~3,000 chunks and every ordinary note in the vault would be
    silently truncated here (D3b).
    """
    char_size = chunk_size * 4
    char_overlap = overlap * 4
    # Guard against overlap >= chunk_size, which would make the window
    # never advance (start <= previous start) and loop forever.
    step = max(char_size - char_overlap, 1)

    if len(content) <= char_size:
        # Deliberately unstripped, which is this branch's long-standing
        # behaviour: the emptiness test is `strip()`, the stored chunk is the
        # content. One chunk can never exceed a cap of at least one.
        return ([content] if content.strip() else []), False

    # One past the cap: if that chunk materialises, something was dropped.
    ceiling = max_chunks + 1 if max_chunks > 0 else None

    chunks: list[str] = []
    start = 0
    while start < len(content):
        end = start + char_size
        chunk = content[start:end]
        if chunk.strip():
            chunks.append(chunk.strip())
            if ceiling is not None and len(chunks) >= ceiling:
                break
        if end >= len(content):
            break
        start += step

    if ceiling is not None and len(chunks) >= ceiling:
        return chunks[:max_chunks], True
    return chunks, False


def chunk_text(content: str, chunk_size: int = 512, overlap: int = 0) -> list[str]:
    """Split text into chunks of ~chunk_size tokens with overlap.
    Approximation: 1 token ~ 4 chars.

    Bounded by `MAX_CHUNKS_PER_NOTE` through `chunk_text_bounded`, so "this
    note produces no chunks" and "this note's chunks" mean the same thing
    everywhere they are asked — `embed_note` and the exclusion sweep's
    zero-chunk probe alike. Callers that need to know whether the cap bit call
    `chunk_text_bounded` directly.
    """
    chunks, _truncated = chunk_text_bounded(
        content,
        chunk_size=chunk_size,
        overlap=overlap,
        max_chunks=MAX_CHUNKS_PER_NOTE,
    )
    return chunks


class EmbeddingProvider(Protocol):
    async def embed_one(self, text: str) -> list[float]: ...
    async def embed_batch(self, texts: list[str]) -> list[list[float]]: ...


def _coerce_keep_alive(value: str):
    """Normalize `settings.ollama_keep_alive` for the `/api/embed` payload.

    Ollama's `keep_alive` JSON field wants an *integer* for second-counts and
    for -1 (pin in VRAM forever); a bare string like "-1" is rejected by its
    Go duration parser ("missing unit in duration"). So integer-like values
    are sent as ints, while duration strings ("30m", "1h") pass through.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


# ── Provider input-limit translation (#194) ─────────────────────────────────
#
# A provider can refuse a request because the input exceeds *its* limit, and
# that limit is a token limit, not the character cap `_tracked` enforces before
# the tool body: 8,192 characters of a densely-tokenizing script still exceed
# 8,192 tokens. `MAX_SEARCH_QUERY_CHARS` is therefore necessary and **not
# sufficient**, and the second half of that pair lives here — each provider
# recognises its own input-limit rejection and raises
# `refusals.ProviderInputTooLarge`, which `semantic_search_impl` renders as the
# ordinary `argument_too_long` refusal carrying the provider's stated reason.
# The agent then sees one actionable failure mode for "the query was too large"
# whichever limit actually applied, instead of a raw provider error.
#
# The exception type is declared in `src/services/refusals.py`, which imports
# nothing from the application, so the module that raises it and the module
# that handles it share one dependency-free contract and neither depends on the
# other's module.
#
# **An input-limit rejection is never retried.** It is a fact about the bytes
# we sent, not about the provider's health — the same input fails identically
# on every attempt — so both checks below sit *before* the retry decision.
# #127's 429/5xx backoff is untouched for every other error, and a generic
# provider error still propagates exactly as it did.
#
# **The indexer is not a caller that can shorten its input.** `embed_note`
# catches this exception along with every other provider exception and records
# the ordinary `PROVIDER_FAILED` outcome carrying the class name; only a search
# caller can act on the refusal, so only the search tools translate it.

#: Truncation bound for the provider's own message. The reason reaches two
#: places a vendor's text must not be able to flood — the caller-facing refusal
#: `semantic_search_impl` renders, and the `usage_logs` row beside it — and a
#: provider that echoes the offending input back inside its error message is a
#: real shape. So the bound is applied **at capture**, the
#: `MAX_EMBED_FAILURE_MESSAGE_CHARS` precedent, rather than trusted to whoever
#: renders it later.
MAX_PROVIDER_REASON_CHARS = 300

#: The HTTP statuses an input-limit rejection is looked for on. 400 is the
#: documented one for both providers; 413 and 422 are what an
#: OpenAI-compatible gateway in front of a model may answer instead. **429 is
#: deliberately absent**: a rate limit is a fact about velocity, it is
#: retryable, and translating it into "your query was too long" would both lie
#: to the caller and skip the backoff that makes it succeed.
_INPUT_LIMIT_STATUSES = frozenset({400, 413, 422})

#: OpenAI `error.code` / `error.type` values that mean the input was too large.
#: `context_length_exceeded` is the long-standing one; `max_tokens_per_request`
#: is the per-request token ceiling the embeddings endpoint enforces;
#: `string_above_max_length` is the raw character bound on a single input
#: element.
_OPENAI_INPUT_LIMIT_CODES = frozenset(
    {
        "context_length_exceeded",
        "max_tokens_per_request",
        "string_above_max_length",
    }
)

#: Ollama sends a bare `{"error": "..."}` string with no machine-readable
#: code, so its detection rests entirely on the phrase test below. Named rather
#: than written inline so the two providers' branches read identically and a
#: future Ollama error code has one obvious place to go.
_OLLAMA_INPUT_LIMIT_CODES: frozenset[str] = frozenset()

#: The message phrases that identify an input-limit rejection where the vendor
#: sends no code to match on. Prose matching is the **fallback**, not the
#: primary rule — a code is preferred wherever one is sent — and it is applied
#: only on the statuses above, so a generic 400 (an unknown model, a malformed
#: body) still propagates as the provider error it is.
#:
#: **Every alternative names what was too large.** A bare size complaint —
#: `maximum length`, which this list used to carry on its own — matches
#: sentences that have nothing to do with the input: OpenAI answers an unknown
#: deployment with `invalid_model` and the message "identifier exceeds maximum
#: length", which was translated into `argument_too_long` and told an agent to
#: shorten a query that was never the problem. A caller acting on that refusal
#: trims its query, retries, and is refused identically forever, while the real
#: fault — a misconfigured model name, an operator's problem — never surfaces.
#: So a phrase must pair the complaint with an input-shaped word (input,
#: prompt, query, message, string, context, token) within one sentence.
_INPUT_LIMIT_PHRASES = re.compile(
    r"maximum context length"
    r"|context length"
    r"|context_length_exceeded"
    r"|maximum (?:input )?(?:number of )?tokens"
    r"|tokens per request"
    r"|too many tokens"
    r"|token limit"
    r"|reduce your (?:prompt|input|message|query)"
    r"|(?:input|prompt|query|message|string)\b[^.]{0,40}?\b"
    r"(?:too (?:long|large|big)|exceeds? the maximum|above the maximum)"
    r"|(?:too (?:long|large|big)|exceeds? the maximum)[^.]{0,40}?\b"
    r"(?:input|prompt|query|message|context|token)",
    re.IGNORECASE,
)


def _bounded_reason(message: str) -> str:
    """The provider's own words, whitespace-collapsed, bounded, never empty.

    Never empty because the reason is interpolated into a sentence the caller
    reads: an empty one would render "…refused this query as too large for its
    own input limit: . The query is under…".
    """
    text = " ".join((message or "").split())
    if not text:
        return "the provider stated no reason"
    if len(text) > MAX_PROVIDER_REASON_CHARS:
        return text[: MAX_PROVIDER_REASON_CHARS - 1].rstrip() + "…"
    return text


def _error_message_and_code(response: httpx.Response) -> tuple[str, str, str]:
    """`(message, code, kind)` from an error body in either provider's shape.

    OpenAI answers `{"error": {"message": …, "type": …, "code": …}}`; Ollama
    answers `{"error": "…"}`. Anything else — HTML from a proxy, an empty body,
    a truncated stream — yields the raw text and neither field, so the phrase
    test still gets its chance and **nothing on this path may raise**: a
    detection helper that threw would convert a provider error into an internal
    one.

    `code` and `kind` (`error.type`) are returned **separately**, and the
    difference decides whether the prose fallback may run at all. `code` is the
    provider's specific machine name for this fault — `context_length_exceeded`,
    `invalid_model` — so an unrecognised one is a positive statement that the
    fault is something else. `type` is a coarse bucket (`invalid_request_error`)
    that accompanies input-limit rejections and unknown models alike, so it
    says nothing either way. Collapsing the two, as this used to, meant either
    trusting prose the provider had already contradicted or refusing to read
    the prose of every gateway that sends only a type.
    """
    try:
        payload = response.json()
    except Exception:
        payload = None
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            message = err.get("message")
            code = err.get("code")
            kind = err.get("type")
            return (
                message if isinstance(message, str) else "",
                code if isinstance(code, str) else "",
                kind if isinstance(kind, str) else "",
            )
        if isinstance(err, str):
            return err, "", ""
        if isinstance(payload.get("message"), str):
            return payload["message"], "", ""
    try:
        return response.text or "", "", ""
    except Exception:
        return "", "", ""


def _input_limit_reason(
    response: httpx.Response, *, codes: frozenset[str]
) -> str | None:
    """The provider's stated reason when this response is an input-limit
    rejection, else `None`.

    `None` is the "nothing changed" answer: the caller must go on handling the
    response exactly as it did before this function existed, retries and all.
    """
    if response.status_code not in _INPUT_LIMIT_STATUSES:
        return None
    message, code, kind = _error_message_and_code(response)
    if code:
        # The provider named the fault. If it is one of ours this *is* an
        # input-limit rejection; if it is not, the provider has already said
        # what went wrong and the prose fallback must not overrule it. That
        # ordering is the whole fix for `invalid_model` + "identifier exceeds
        # maximum length": a specific code we do not recognise is evidence
        # **against** this branch, not the absence of evidence.
        return _bounded_reason(message or code) if code in codes else None
    if kind and kind in codes:
        return _bounded_reason(message or kind)
    # No specific code — an Ollama-shaped body, a gateway that sends only a
    # coarse `type`, or no JSON at all. Prose is all there is.
    if message and _INPUT_LIMIT_PHRASES.search(message):
        return _bounded_reason(message)
    return None


class OllamaProvider:
    """Default provider — POSTs to a self-hosted Ollama instance, one input
    per request. Sends `keep_alive` so the model stays resident between
    (often infrequent) calls instead of paying a cold reload each time."""

    async def embed_one(self, text: str) -> list[float]:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{settings.ollama_url}/api/embed",
                json={
                    "model": settings.embedding_model,
                    "input": text,
                    "keep_alive": _coerce_keep_alive(settings.ollama_keep_alive),
                },
            )
            if response.status_code >= 400:
                # Ollama **truncates** rather than rejecting on the ordinary
                # path: `/api/embed` takes a `truncate` flag that defaults to
                # true and we do not send it, so an over-long input normally
                # comes back as a vector computed over the model's context
                # window. This branch is for the deployments and models that
                # report the limit instead of silently shortening the input;
                # where it is not taken, nothing about this call changes.
                reason = _input_limit_reason(
                    response, codes=_OLLAMA_INPUT_LIMIT_CODES
                )
                if reason is not None:
                    raise refusals.ProviderInputTooLarge(reason, provider="ollama")
            response.raise_for_status()
            data = response.json()
            return data["embeddings"][0]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed each chunk in turn. **The per-call timeout is the only
        deadline, deliberately** (#127, D5).

        There used to be a fixed 300 s budget over the whole batch. It could
        only ever fire when every individual chunk was healthy — a hung
        provider trips the 30 s `wait_for` long before it — so the one thing it
        actually caught was a note with more chunks than 300 s of normal
        latency covers. Such a note timed out, was never certified, and was
        re-selected on the next pass: a permanent 300 s burn per tick, under
        `index_pass_lock`, that could never complete. A *proportional* budget
        was rejected in review for re-introducing the same boundary one size
        class up — chunks that each answer just under 30 s exhaust any
        per-chunk allowance once loop overhead is counted.

        Liveness is unaffected: a provider that stops responding still fails in
        ≤ 30 s. The cost is that a giant note holds the pass for 30 s × chunks
        in the worst case, once, and the pause flag is honoured at the next
        note boundary as it always was. `OpenAIProvider` is untouched — it
        batches natively and never had this defect.
        """
        results: list[list[float]] = []
        for t in texts:
            results.append(await asyncio.wait_for(self.embed_one(t), timeout=30.0))
        return results


class OpenAIProvider:
    """OpenAI / OpenAI-compatible provider. Uses native batch endpoint and
    retries 429/5xx with exponential backoff."""

    OPENAI_BATCH_LIMIT = 96
    MAX_ATTEMPTS = 3
    BASE_DELAY = 1.0

    async def _post(self, inputs: list[str]) -> list[list[float]]:
        url = f"{settings.openai_base_url.rstrip('/')}/embeddings"
        headers = {
            "Authorization": f"Bearer {settings.openai_api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": settings.openai_embedding_model,
            "input": inputs,
            "dimensions": settings.embedding_dimensions,
        }

        last_exc: Exception | None = None
        async with httpx.AsyncClient(timeout=60.0) as client:
            for attempt in range(1, self.MAX_ATTEMPTS + 1):
                try:
                    response = await client.post(url, headers=headers, json=payload)
                except httpx.HTTPError as e:
                    last_exc = e
                    if attempt >= self.MAX_ATTEMPTS:
                        raise
                    await asyncio.sleep(self.BASE_DELAY * (2 ** (attempt - 1)))
                    continue

                status = response.status_code
                if status == 200:
                    data = response.json()
                    rows = sorted(data["data"], key=lambda r: r["index"])
                    return [r["embedding"] for r in rows]

                # Decided **before** `retryable`, deliberately: an input
                # the provider will not accept fails identically on every
                # attempt, so retrying it would turn one refusal into three
                # provider round trips and three times the latency before the
                # caller hears the one thing it can act on. No status in
                # `_INPUT_LIMIT_STATUSES` is retryable today — this ordering is
                # what keeps that true if one ever becomes so.
                reason = _input_limit_reason(
                    response, codes=_OPENAI_INPUT_LIMIT_CODES
                )
                if reason is not None:
                    raise refusals.ProviderInputTooLarge(reason, provider="openai")

                retryable = status == 429 or 500 <= status < 600
                if retryable and attempt < self.MAX_ATTEMPTS:
                    logger.warning(
                        "OpenAI embeddings %d on attempt %d/%d, retrying",
                        status,
                        attempt,
                        self.MAX_ATTEMPTS,
                    )
                    await asyncio.sleep(self.BASE_DELAY * (2 ** (attempt - 1)))
                    continue

                response.raise_for_status()
                raise RuntimeError(f"Unexpected OpenAI response: {status}")

        if last_exc:
            raise last_exc
        raise RuntimeError("OpenAI embeddings failed without an exception")

    async def embed_one(self, text: str) -> list[float]:
        result = await self._post([text])
        return result[0]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        out: list[list[float]] = []
        for start in range(0, len(texts), self.OPENAI_BATCH_LIMIT):
            sub = texts[start : start + self.OPENAI_BATCH_LIMIT]
            out.extend(await self._post(sub))
        return out


@lru_cache(maxsize=1)
def get_provider() -> EmbeddingProvider:
    """Return a singleton provider instance based on `settings.embedding_provider`."""
    if settings.embedding_provider == "openai":
        return OpenAIProvider()
    return OllamaProvider()


async def get_embedding(text_input: str) -> list[float]:
    return await get_provider().embed_one(text_input)


async def get_embeddings_batch(texts: list[str]) -> list[list[float]]:
    return await get_provider().embed_batch(texts)


class StaleCertification(RuntimeError):
    """The metadata row moved between verification and certification.

    Raised by `certify_embedded` when the conditional stamp matches no row: the note's
    `content_hash` or `file_path` changed after the pass verified the bytes it
    read against them, so the vectors in hand describe content the row no
    longer claims. Nothing is written; the caller rolls back and a later pass
    embeds the row as it then stands.
    """


async def certify_embedded(
    session: AsyncSession,
    note_id: int,
    certified_hash: str,
    certified_path: str,
    *,
    expire_on: NoteMetadata | None = None,
) -> None:
    """Stamp `embedded_content_hash` conditionally, and lock the row doing it.

    Public because `embed_vault`'s **exclusion** branch needs the identical
    predicate: adversarial round 2 found it stamping by `id` alone, so a
    concurrent `move_note` out of an excluded folder (same content, so the same
    hash) left an included note recorded as embedded with zero vectors —
    permanently absent from `semantic_search`, and never selected again because
    `embedded_content_hash == content_hash`.

    **This is the ordering that closes the database TOCTOU, and both halves
    matter.** `embed_vault` verifies the bytes it read against the
    `content_hash` from its *initial* query (call it H1) and then re-reads the
    row through the ORM — a second read, in a later transaction, which can see
    a hash another indexer pass has since committed (H2). The old code copied
    that re-read value onto the vectors it had just built from H1's content, so
    the row ended up marked embedded for content it does not have; and because
    `embed_vault` selects on `embedded_content_hash != content_hash`, H2 == H2
    then blocked every later repair. Permanently wrong semantic results, which
    is the failure this product ranks above every expensive one.

    So the certification is a conditional `UPDATE … WHERE id = :i AND
    file_path = :p AND content_hash = :h` against **H1**, the hash of what was
    actually embedded, and never a value re-read from the row. Under READ
    COMMITTED an `UPDATE` re-evaluates its predicate against the latest
    committed version after taking the row lock, so a concurrent commit of H2
    makes it match zero rows rather than silently winning. Nought rows raises,
    the caller rolls back, and the vectors are discarded.

    It runs **before** the delete/insert of vectors, so from the moment the
    stamp lands the row is locked for the rest of this transaction and nothing
    can change it underneath the write it authorises. It runs **after** the
    provider call, so a row lock is never held across an embedding request.
    """
    result = await session.execute(
        update(NoteMetadata)
        .where(
            NoteMetadata.id == note_id,
            NoteMetadata.file_path == certified_path,
            NoteMetadata.content_hash == certified_hash,
        )
        .values(embedded_content_hash=certified_hash)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        raise StaleCertification(
            f"notes_metadata row {note_id} ({certified_path!r}) no longer "
            f"records content_hash {certified_hash!r} at that path, so nothing "
            "may be certified against it"
        )
    if expire_on is not None:
        # The raw UPDATE bypassed the identity map; expire the attribute so
        # nothing later in this session reads (or flushes) the pre-update
        # value. Passed explicitly rather than inferred, because the exclusion
        # branch certifies from a plain result row that no session maps.
        session.expire(expire_on, ["embedded_content_hash"])


class NoteEmbedOutcome(enum.Enum):
    """What `embed_note` did, as five distinct things instead of one `0`.

    The return used to be a chunk count, and `0` meant three unrelated things:
    a note that cleaned to zero chunks *and was certified*, a provider
    exception the function swallowed, and a vector/chunk cardinality mismatch.
    `_embed_vault_pinned` then ran `outcome.embedded += 1` after all three, so a
    total Ollama or OpenAI outage wrote an `indexer_runs` row reading
    `notes_embedded = N, error = NULL` — byte for byte the record a healthy
    pass writes, with a *positive* count (#201).
    """

    #: Chunks were embedded and the note was certified.
    EMBEDDED = "embedded"
    #: Cleaning and chunking produced nothing. The note is certified with zero
    #: vectors, which is the correct representation of it, and **no provider
    #: call was made** — so it is not an attempt.
    CERTIFIED_EMPTY = "certified_empty"
    #: The provider raised. Nothing certified, nothing written, previous
    #: vectors intact (#11). A failure of the pass.
    PROVIDER_FAILED = "provider_failed"
    #: The provider returned a number of vectors that is not the number of
    #: chunks requested. Same disposition, a distinct diagnosis.
    PROVIDER_CARDINALITY_MISMATCH = "provider_cardinality_mismatch"
    #: The embedding configuration moved under the provider call (D7c): the
    #: fingerprint re-read under the generation lock no longer matches this
    #: process's. Nothing certified, inserted or deleted. **Not** a failure —
    #: nothing went wrong with the provider — but an attempt, because a call
    #: was issued.
    GENERATION_MISMATCH = "generation_mismatch"


@dataclass(frozen=True)
class EmbedNoteFailure:
    """Bounded, structured detail for the two failing outcomes.

    The pass's own record of a failed embed is built from this and from nothing
    else: `embed_note` swallows the provider exception, so by the time
    `_embed_vault_pinned` sees a failure there is no exception left to inspect
    and `EpassResult.first_error` would otherwise read `"... first: None"`.

    `message` is truncated **at capture**, not where the run row is written:
    `MAX_RUN_ERROR_CHARS` (4,000) bounds the whole `indexer_runs.error` text,
    and one untruncated provider traceback can exceed that on its own and evict
    the stage labels beside it.
    """

    #: `type(exc).__name__`, or the literal `"CardinalityMismatch"`.
    exc_type: str
    #: Truncated to `MAX_EMBED_FAILURE_MESSAGE_CHARS` by `capture`.
    message: str
    #: Chunks sent to the provider. Set for both failing outcomes.
    requested: int | None = None
    #: Vectors the provider returned. Set for the cardinality mismatch only.
    received: int | None = None

    @classmethod
    def capture(cls, exc: BaseException, *, requested: int) -> "EmbedNoteFailure":
        """From a provider exception, with the message bounded here."""
        return cls(
            exc_type=type(exc).__name__,
            message=str(exc)[:MAX_EMBED_FAILURE_MESSAGE_CHARS],
            requested=requested,
        )

    @classmethod
    def cardinality(cls, *, requested: int, received: int) -> "EmbedNoteFailure":
        """For a batch whose size is not the requested chunk count."""
        return cls(
            exc_type="CardinalityMismatch",
            message=f"{received} vectors for {requested} chunks",
            requested=requested,
            received=received,
        )


#: The outcomes that carry an `EmbedNoteFailure`, and the only ones that may.
_FAILING_OUTCOMES = frozenset(
    {
        NoteEmbedOutcome.PROVIDER_FAILED,
        NoteEmbedOutcome.PROVIDER_CARDINALITY_MISMATCH,
    }
)


@dataclass(frozen=True)
class EmbedNoteResult:
    """What one `embed_note` call did, read by field and never as a number.

    **Two chunk counts, deliberately.** `chunks_submitted` is what went to the
    provider and is what the per-tenant budget debits; `chunks_embedded` is
    what was stored and is what feeds a pass's `total_chunks`. A budget debited
    by *stored* chunks is not debited at all when the provider fails, so a
    tenant whose every note fails would burn unbounded provider time and never
    reach its own bound — the starvation #202 is about, surviving inside the
    fix for it.

    **There is deliberately no `__int__` and no `__radd__`.** A first draft of
    this design claimed `total_chunks += result` would keep working through an
    `__int__`; it does not — `int.__iadd__` falls back to `int.__add__(result)`,
    which returns `NotImplemented`, and with no `__radd__` the statement raises
    `TypeError`. Explicit is also the outcome we want: a caller that must name
    the field it means cannot silently go on treating five outcomes as one
    number.
    """

    outcome: NoteEmbedOutcome
    #: Chunks handed to the provider. Zero exactly when no call was issued.
    chunks_submitted: int
    #: Chunks stored as `note_embeddings` rows by this call.
    chunks_embedded: int
    #: Whether the chunker dropped chunks at `MAX_CHUNKS_PER_NOTE`. Reported
    #: for every outcome because it is a fact about the note's text; only a
    #: certifying outcome licenses writing `notes_metadata.chunks_truncated`
    #: from it, and only after that transaction commits.
    truncated: bool
    #: Non-null exactly for `PROVIDER_FAILED` and
    #: `PROVIDER_CARDINALITY_MISMATCH`.
    failure: EmbedNoteFailure | None = None

    def __post_init__(self) -> None:
        # An invariant rather than a convention: `record_failure_detail` is
        # driven off `failure`, so a failing outcome without one would report a
        # provider outage as `"first: None"` — the exact hole #201 is.
        if (self.outcome in _FAILING_OUTCOMES) != (self.failure is not None):
            raise ValueError(
                f"{self.outcome.value} must carry a failure "
                f"{'' if self.outcome in _FAILING_OUTCOMES else 'no failure '}"
                "and does not"
            )


async def _generation_matches(session: AsyncSession, note: NoteMetadata) -> bool:
    """Take the generation lock and re-read the embedding fingerprint under it.

    **This is the whole enforcement of D7c**, and it lives here because this is
    the only place the window exists: `get_embeddings_batch` and
    `certify_embedded` are twenty lines apart inside `embed_note`, so no caller
    can interpose between them. `_embed_vault_pinned`'s per-stage read is an
    early exit, not the guarantee — a check at the head of a stage is separated
    from the act by a provider round trip:

        old process: read fingerprint A == A, proceed
        old process: get_embeddings_batch(note)     <- seconds to minutes
        reset:       wipe column, write fingerprint B, commit
        old process: certify + insert              <- old-model vectors under B

    `make reset-embeddings` runs as a one-off container on purpose (#142), so
    it can and does run while a previous container is still serving; the
    vectors that interleaving stores are permanently wrong and every later
    startup is silent, because the stored fingerprint already matches.

    Called **after** the provider call and **before** the certification, which
    is the window `index-integrity` already reserves — so no lock of any kind
    is held across a network request. It is also before the first row lock this
    transaction takes (the certification is that lock), so the
    advisory-before-any-row-lock ordering and the after-the-provider-call rule
    agree here rather than conflict.

    An **absent** fingerprint is not a mismatch: nothing has been claimed about
    the stored rows, so there is nothing to contradict, and this path never
    writes one — only the maintenance workflows and the startup adoption do,
    which is what stops a refusal from clearing itself. `DIFFERS` and
    `UNREADABLE` both refuse; an unreadable stored value is one this build
    cannot compare, so it cannot certify against it (the rule
    `clean_at_version` already follows for an unknown stamped version).

    An absent `indexer_state` table has the same disposition as an absent
    fingerprint. Probe with `to_regclass` before querying its contents, because
    a missing-relation SELECT aborts the transaction after the provider call.
    This matches the startup guard's ABSENT disposition. It does not make an
    otherwise unmigrated schema supported: the other required columns and
    tables must still exist.
    """
    if not await state_table_exists(session):
        # Before the lock, deliberately: nothing to compare means nothing to
        # serialise against, and `to_regclass` takes no row or table lock, so
        # the ordering rule is untouched either way.
        return True
    await acquire_generation_lock(session)
    current = embedding_fingerprint()
    verdict = compare_fingerprint(
        await get_state(session, KEY_EMBEDDING_FINGERPRINT), current
    )
    if verdict.status in (FingerprintStatus.MATCH, FingerprintStatus.ABSENT):
        return True
    logger.error(
        "Refusing to certify %s: the embedding configuration changed under "
        "this pass. Stored fingerprint %s, this process's %s (%s). Nothing was "
        "certified, inserted or deleted; a pass running the stored "
        "configuration will embed it.",
        note.file_path,
        verdict.stored,
        current,
        ", ".join(verdict.fields) if verdict.fields else verdict.reason,
    )
    return False


async def embed_note(
    session: AsyncSession,
    note: NoteMetadata,
    content: str,
    *,
    certified_hash: str | None = None,
    certified_path: str | None = None,
    on_provider_call: Callable[[int], None] | None = None,
) -> EmbedNoteResult:
    """Chunk a note's content, embed, and store in note_embeddings.

    Returns an `EmbedNoteResult`, **never an integer**: the five things this
    function can do are five outcomes, and the caller reads the field it means.
    See `NoteEmbedOutcome` for why one number was wrong.

    `certified_hash` / `certified_path` are what the caller **verified the
    bytes against** — `embed_vault` passes the hash and path from its own
    initial query. When they are given, the row is stamped through
    `certify_embedded`:
    conditionally, with that hash, under a row lock, before any vector is
    replaced. When they are absent the legacy behaviour stands (copy the
    in-memory row's `content_hash`), which is what the unit tests that drive
    this function with a stub session exercise.

    The chunking is bounded (`MAX_CHUNKS_PER_NOTE`) and a capped note is still
    certified — on full coverage of the *requested* list, which is what the
    cap redefines. The truncation ERROR line is emitted by the caller **after
    the certifying transaction commits**, not here: logging it before the
    commit would leave a permanent ERROR in a bounded, process-lifetime buffer
    for a write that then rolled back on a `StaleCertification`, sending an
    operator after a note that was never stored that way.
    """
    if (certified_hash is None) != (certified_path is None):
        # Half a certification is worse than none: `file_path == None` renders
        # as `IS NULL` and would match no row, turning every certified embed
        # into a silent skip.
        raise ValueError(
            "certified_hash and certified_path are one unit: pass both or "
            "neither"
        )
    cleaned = clean_for_embedding(content)
    chunks, truncated = chunk_text_bounded(
        cleaned,
        chunk_size=settings.chunk_size,
        overlap=settings.chunk_overlap,
        max_chunks=MAX_CHUNKS_PER_NOTE,
    )
    if not chunks:
        # Empty/fully-filtered notes are successfully represented by zero
        # vectors. Remove stale vectors and stamp the hash so they do not get
        # selected on every embedding pass forever.
        #
        # No generation lock and no fingerprint re-read: no provider call was
        # made, and "the correct vector set for this note is the empty one" is
        # true under every embedding configuration — the same argument that
        # exempts the exclusion branch (D7c).
        if certified_hash is not None:
            await certify_embedded(
                session, note.id, certified_hash, certified_path, expire_on=note
            )
        await session.execute(
            delete(NoteEmbedding).where(NoteEmbedding.note_id == note.id)
        )
        if certified_hash is None:
            note.embedded_content_hash = note.content_hash
        await session.flush()
        # `truncated` is False by construction here: a note the cap bit has at
        # least `MAX_CHUNKS_PER_NOTE` chunks.
        return EmbedNoteResult(
            outcome=NoteEmbedOutcome.CERTIFIED_EMPTY,
            chunks_submitted=0,
            chunks_embedded=0,
            truncated=False,
        )

    # ── The accounting boundary, and it is *here* ────────────────────────
    # The caller used to account for the provider call from the returned
    # `chunks_submitted`, which is correct for every path that returns — and
    # silently wrong for every path that raises after this point.
    # `certify_embedded` raises `StaleCertification` on a moved row, and the
    # database can raise anywhere below; the call had been made, the provider
    # time had been spent, and none of it was recorded as an attempt or debited
    # from the tenant's chunk budget. A tenant whose every note lost its
    # certification race could therefore issue provider calls for ever without
    # becoming budget-exhaustible — the starvation #202 exists to bound,
    # surviving inside the fix for it.
    #
    # So issuance is announced at the moment of issuance, before the await, and
    # the callback is the caller's own accumulator. It fires exactly once per
    # note that reaches the provider, on every subsequent path: the swallowed
    # failure below, the cardinality refusal, the generation mismatch, the
    # successful embed, and every exception that escapes this function.
    # `CERTIFIED_EMPTY` never reaches it, because it makes no provider call —
    # which is the same rule stated once rather than reconstructed by the
    # caller from a field on a result it may never receive.
    if on_provider_call is not None:
        on_provider_call(len(chunks))
    try:
        embeddings = await get_embeddings_batch(chunks)
    except Exception as e:
        # Swallowed here rather than raised, deliberately: `_reconcile_exclusions`
        # calls this function too and its declared convergence exception is that
        # a row whose provider call fails is left unstamped and retried, so a
        # raise would have to be re-caught there anyway — and a raise makes a
        # provider blip indistinguishable from a database error at the call
        # site, which is the conflation the typed outcome exists to remove.
        # Nothing is written, so the note's previous vectors survive (#11).
        #
        # `refusals.ProviderInputTooLarge` lands here with every other provider
        # exception, deliberately (#194): the indexer is not a caller that can
        # shorten its input, so there is nobody to translate the refusal for.
        # The pass record is the ordinary `PROVIDER_FAILED` carrying the class
        # name, which is the honest one — nothing certified, previous vectors
        # intact, the note retried next pass. Only the search tools translate
        # this exception into a caller-facing refusal.
        logger.warning(f"Failed to embed {note.file_path}: {e}")
        return EmbedNoteResult(
            outcome=NoteEmbedOutcome.PROVIDER_FAILED,
            chunks_submitted=len(chunks),
            chunks_embedded=0,
            truncated=truncated,
            failure=EmbedNoteFailure.capture(e, requested=len(chunks)),
        )

    if len(embeddings) != len(chunks):
        # Cardinality is exact over the *requested* list, which the cap has
        # already bounded: one vector short of the capped list is still a
        # refusal.
        logger.warning(
            "Embedding provider returned %d vectors for %d chunks in %s",
            len(embeddings), len(chunks), note.file_path,
        )
        return EmbedNoteResult(
            outcome=NoteEmbedOutcome.PROVIDER_CARDINALITY_MISMATCH,
            chunks_submitted=len(chunks),
            chunks_embedded=0,
            truncated=truncated,
            failure=EmbedNoteFailure.cardinality(
                requested=len(chunks), received=len(embeddings)
            ),
        )

    # ── The generation interlock (D7c) ───────────────────────────────────
    # After the provider call, before the certification, before this
    # transaction's first row lock. A mismatch certifies nothing, inserts
    # nothing and deletes nothing — the disposition `StaleCertification`
    # already has — and is neither an embedded note nor a failure.
    if not await _generation_matches(session, note):
        return EmbedNoteResult(
            outcome=NoteEmbedOutcome.GENERATION_MISMATCH,
            chunks_submitted=len(chunks),
            chunks_embedded=0,
            truncated=truncated,
        )

    # Certify first, then replace. The stamp is the conditional write that
    # proves the row still records the hash these vectors were built from, and
    # it takes the row lock that keeps that true for the rest of the
    # transaction — so nothing is deleted on the strength of a row that has
    # since moved. It raises rather than returning, and `embed_vault` rolls the
    # whole note back.
    if certified_hash is not None:
        await certify_embedded(
            session, note.id, certified_hash, certified_path, expire_on=note
        )

    # Only delete the old embeddings once new ones are in hand. If the provider
    # call above had failed, deleting first would let embed_vault commit the
    # DELETE and drop good vectors (issue #11).
    await session.execute(
        delete(NoteEmbedding).where(NoteEmbedding.note_id == note.id)
    )

    for i, (chunk, embedding) in enumerate(zip(chunks, embeddings)):
        session.add(NoteEmbedding(
            note_id=note.id,
            chunk_index=i,
            chunk_text=chunk,
            embedding=embedding,
        ))

    await session.flush()
    if certified_hash is None:
        note.embedded_content_hash = note.content_hash
    return EmbedNoteResult(
        outcome=NoteEmbedOutcome.EMBEDDED,
        chunks_submitted=len(chunks),
        chunks_embedded=len(chunks),
        truncated=truncated,
    )


async def semantic_search(
    session: AsyncSession,
    query: str,
    limit: int = 15,
    folder: str | None = None,
    tags: list[str] | None = None,
    frontmatter: dict | None = None,
    user_id: int | None = None,
) -> list[dict]:
    """Embed query and return the best-matching chunk per note (dedup), ordered by cosine distance.

    The HNSW index handles ranking; we over-fetch chunks and dedup per note in Python
    so a single verbose note can't dominate the result set. Each result is a pointer
    to a note plus its most-relevant chunk as preview — the caller should `read_note`
    for full content.
    """
    limit = max(1, min(limit, 50))
    embed_start = time.monotonic()
    query_embedding = await get_embedding(query)
    timing.add_ms("embed_ms", time.monotonic() - embed_start)

    db_start = time.monotonic()
    # ef_search=80 lifts HNSW recall@10 to ~98% at modest latency cost.
    # random_page_cost=1.1 reflects SSD storage; the postgres default of 4
    # makes the planner avoid the HNSW index in favor of a seq+sort, which
    # is faster on small tables but degrades linearly as the vault grows.
    # All three SET LOCALs scope to the current transaction.
    await session.execute(text("SET LOCAL hnsw.ef_search = 80"))
    await session.execute(text("SET LOCAL random_page_cost = 1.1"))
    # iterative_scan (pgvector >= 0.8; guarded at startup by
    # `_check_pgvector_version`) is what keeps a *filtered* search honest.
    # Without it the HNSW scan hands the planner at most `ef_search`
    # candidates, the folder/tags/frontmatter/user predicate throws most of
    # them away, and nothing refills — 45 of 120 folder-filtered probes came
    # back empty. `relaxed_order` lets the scan keep walking the graph until
    # the LIMIT is satisfied *after* filtering. Recall is still bounded by
    # `hnsw.max_scan_tuples` (20,000) and `hnsw.scan_mem_multiplier` (1);
    # those are the next knobs if the vault outgrows them (~16.7k chunks
    # today). `relaxed_order` may emit rows slightly out of distance order
    # across iterations, so we re-sort below — that is presentation only, it
    # cannot recover candidates the scan never returned.
    await session.execute(text("SET LOCAL hnsw.iterative_scan = 'relaxed_order'"))

    # Over-fetch by 5x: HNSW is logarithmic so this is essentially free, and it
    # gives the per-note dedup enough headroom when a note contributes many chunks.
    overfetch = max(limit * 5, 50)
    distance = NoteEmbedding.embedding.cosine_distance(query_embedding)
    stmt = (
        select(NoteEmbedding, NoteMetadata, distance.label("distance"))
        .join(NoteMetadata, NoteEmbedding.note_id == NoteMetadata.id)
    )
    stmt = apply_note_filters(
        stmt, folder=folder, tags=tags, frontmatter=frontmatter, user_id=user_id
    )
    stmt = stmt.order_by(distance).limit(overfetch)

    result = await session.execute(stmt)
    rows = result.fetchall()

    # Zero-row safety net. HNSW is approximate, so "no rows" from a *filtered*
    # query is ambiguous: it can mean "nothing matches" or "the scan ran out of
    # budget before it found anything that matched". Re-running the identical
    # statement with index scans off is pgvector's documented exact search — it
    # is O(n), but only on this rare path, and it turns "non-empty whenever a
    # match exists" from a benchmark hope into a construction.
    #
    # **Eligibility is unconditional** (#127, D1a). It used to require one of
    # `folder`/`tags`/`frontmatter`/a named user, on the reasoning that an
    # unfiltered scan cannot lose candidates to a post-filter. That reasoning
    # died with the total owner mapping: `apply_note_filters` now always
    # appends an owner predicate, so *every* query here is filtered and the
    # ownerless one — `user_id IS NULL` against a database whose vectors are
    # mostly a named user's — is exactly the shape where the HNSW window fills
    # with candidates the predicate then discards. Under the old condition
    # that query returned empty while NULL-owned matches existed.
    exact_fallback = False
    if not rows:
        # Transaction-scoped, like the three SET LOCALs above: it applies to
        # the re-run on the next line and dies with this transaction. The sole
        # caller (`search_notes_impl`) closes the session as soon as this
        # function returns, so nothing else can inherit the exact plan — do not
        # append further statements after the re-run without re-reading that.
        await session.execute(text("SET LOCAL enable_indexscan = off"))
        rows = (await session.execute(stmt)).fetchall()
        exact_fallback = True
    timing.record("exact_fallback", exact_fallback)
    timing.add_ms("db_ms", time.monotonic() - db_start)

    # Re-sort by distance before dedupe/truncate: `relaxed_order` does not
    # promise a globally sorted stream, and the dedupe below keeps the *first*
    # chunk seen per note.
    rows = sorted(rows, key=lambda r: r[2])

    seen: set[int] = set()
    deduped: list[tuple] = []
    for ne, nm, _distance in rows:
        if ne.note_id in seen:
            continue
        seen.add(ne.note_id)
        deduped.append((ne, nm))
        if len(deduped) >= limit:
            break

    # ── Staleness and truncation, annotated from the already-hydrated row ──
    #
    # `stale` is `embedded_content_hash IS DISTINCT FROM content_hash`,
    # computed in Python: the statement above already hydrates the whole
    # `NoteMetadata` entity, so both hashes are in hand and **no predicate, no
    # `SET LOCAL`, no overfetch and no exact-fallback eligibility changes** —
    # which is the point. Filtering on the hashes was rejected outright: it
    # would remove every note edited since the last completed embed pass (the
    # whole vault during a provider outage), and because the owner mapping
    # makes every query here a filtered one whose zero-row result re-runs
    # exactly, it would turn an outage into an O(n) scan of the embedding table
    # on every search.
    #
    # `IS DISTINCT FROM`, never `!=`: a note that was never embedded, or whose
    # certification a move cleared, holds `NULL` and must read stale rather
    # than NULL-propagating into "fresh". Python's `!=` against `None` **is**
    # that operator, which is why this reads as it does — do not "fix" it into
    # an `is not None` guard.
    #
    # **A stale row's `chunk` is `None`, not a clip of superseded text.** Of
    # the fields on a result, `path`, `title` and `tags` come from
    # `notes_metadata`, which the *scan* refreshed — a row is stale precisely
    # because the scan already committed the new `content_hash` — and
    # `similarity` is a retrieval score, not a claim about content. The chunk
    # is the only field that is a verbatim quotation of the note's text, the
    # only one that is out of date, and the one an agent pastes into an answer.
    # Withholding it turns a silently wrong answer into a visibly degraded one
    # whose remedy (`read_note`) is one call away. The note is still found,
    # still ranked, still named: nothing leaves the result set and nothing
    # moves in it.
    #
    # The bound this signal has, stated here so it is not later read as a
    # stronger claim: staleness is derived from `notes_metadata`, so it reports
    # what the *index knows*. Between an edit landing on disk and the next pass
    # committing the new hash, the row reads fresh while the chunk is already
    # superseded. Closing that would put a filesystem read on the hot path of
    # every search and would still race the writer.
    results = [
        {
            "path": nm.file_path,
            "title": nm.title,
            "tags": nm.tags,
            "chunk": None if stale else ne.chunk_text[:500],
            "chunk_index": ne.chunk_index,
            "similarity": float(np.dot(ne.embedding, query_embedding) / (
                np.linalg.norm(ne.embedding) * np.linalg.norm(query_embedding)
            )),
            "stale": stale,
            # Read from the durable column, never inferred from the number of
            # chunk rows: a capped note holds exactly the cap and is
            # indistinguishable by count from a note that legitimately produces
            # that many.
            "embedding_truncated": bool(nm.chunks_truncated),
        }
        for ne, nm, stale in (
            (ne, nm, nm.embedded_content_hash != nm.content_hash)
            for ne, nm in deduped
        )
    ]
    # Result telemetry, recorded after the dedupe so the count and the paths
    # are what the tool actually returns (#161) — an overfetched, not-yet-
    # deduped row list would report a note twice. Bounds are enforced inside
    # `record_results`; no-op outside a tracked tool call.
    timing.record_results(r["path"] for r in results)
    return results
