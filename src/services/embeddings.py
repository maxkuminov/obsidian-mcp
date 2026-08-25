import asyncio
import logging
import re
import time
from functools import lru_cache
from typing import Protocol

import httpx
import numpy as np
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.models.db import NoteEmbedding, NoteMetadata
from src.services import timing
from src.services.filters import apply_note_filters

logger = logging.getLogger(__name__)

# Strip fenced code blocks before embedding so serialized data dumps
# (Excalidraw JSON, base64 blobs, mermaid graphs) don't dominate vector
# space. Keyword search is unaffected — tsvector still indexes everything.
_FENCE_BACKTICK_RE = re.compile(r"^```[^\n]*\n.*?\n```\s*$", re.MULTILINE | re.DOTALL)
_FENCE_TILDE_RE = re.compile(r"^~~~[^\n]*\n.*?\n~~~\s*$", re.MULTILINE | re.DOTALL)


def clean_for_embedding(content: str) -> str:
    """Strip fenced code blocks (``` and ~~~) from markdown before embedding.

    Inline backtick code is preserved (typically short identifiers, often
    semantically meaningful). Indented code blocks are not stripped — they're
    ambiguous with regular indented prose in personal notes.
    """
    content = _FENCE_BACKTICK_RE.sub("", content)
    content = _FENCE_TILDE_RE.sub("", content)
    return content


def chunk_text(content: str, chunk_size: int = 512, overlap: int = 0) -> list[str]:
    """Split text into chunks of ~chunk_size tokens with overlap.
    Approximation: 1 token ~ 4 chars.
    """
    char_size = chunk_size * 4
    char_overlap = overlap * 4
    # Guard against overlap >= chunk_size, which would make the window
    # never advance (start <= previous start) and loop forever.
    step = max(char_size - char_overlap, 1)

    if len(content) <= char_size:
        return [content] if content.strip() else []

    chunks = []
    start = 0
    while start < len(content):
        end = start + char_size
        chunk = content[start:end]
        if chunk.strip():
            chunks.append(chunk.strip())
        if end >= len(content):
            break
        start += step

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


async def embed_note(
    session: AsyncSession,
    note: NoteMetadata,
    content: str,
    *,
    certified_hash: str | None = None,
    certified_path: str | None = None,
):
    """Chunk a note's content, embed, and store in note_embeddings.

    `certified_hash` / `certified_path` are what the caller **verified the
    bytes against** — `embed_vault` passes the hash and path from its own
    initial query. When they are given, the row is stamped through
    `certify_embedded`:
    conditionally, with that hash, under a row lock, before any vector is
    replaced. When they are absent the legacy behaviour stands (copy the
    in-memory row's `content_hash`), which is what the unit tests that drive
    this function with a stub session exercise.
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
    chunks = chunk_text(cleaned, chunk_size=settings.chunk_size, overlap=settings.chunk_overlap)
    if not chunks:
        # Empty/fully-filtered notes are successfully represented by zero
        # vectors. Remove stale vectors and stamp the hash so they do not get
        # selected on every embedding pass forever.
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
        return 0

    try:
        embeddings = await get_embeddings_batch(chunks)
    except Exception as e:
        logger.warning(f"Failed to embed {note.file_path}: {e}")
        return 0

    if len(embeddings) != len(chunks):
        logger.warning(
            "Embedding provider returned %d vectors for %d chunks in %s",
            len(embeddings), len(chunks), note.file_path,
        )
        return 0

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
    return len(chunks)


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

    return [
        {
            "path": nm.file_path,
            "title": nm.title,
            "tags": nm.tags,
            "chunk": ne.chunk_text[:500],
            "chunk_index": ne.chunk_index,
            "similarity": float(np.dot(ne.embedding, query_embedding) / (
                np.linalg.norm(ne.embedding) * np.linalg.norm(query_embedding)
            )),
        }
        for ne, nm in deduped
    ]
