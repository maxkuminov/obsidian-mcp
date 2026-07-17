import asyncio
import logging
import re
import time
from functools import lru_cache
from typing import Protocol

import httpx
import numpy as np
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.models.db import NoteEmbedding, NoteMetadata
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

    async def embed_batch(
        self, texts: list[str], batch_timeout: float = 300.0
    ) -> list[list[float]]:
        results: list[list[float]] = []
        deadline = time.monotonic() + batch_timeout
        for t in texts:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Embedding batch exceeded total timeout")
            emb = await asyncio.wait_for(
                self.embed_one(t), timeout=min(30.0, remaining)
            )
            results.append(emb)
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


async def embed_note(session: AsyncSession, note: NoteMetadata, content: str):
    """Chunk a note's content, embed, and store in note_embeddings."""
    cleaned = clean_for_embedding(content)
    chunks = chunk_text(cleaned, chunk_size=settings.chunk_size, overlap=settings.chunk_overlap)
    if not chunks:
        # Empty/fully-filtered notes are successfully represented by zero
        # vectors. Remove stale vectors and stamp the hash so they do not get
        # selected on every embedding pass forever.
        await session.execute(
            delete(NoteEmbedding).where(NoteEmbedding.note_id == note.id)
        )
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
    query_embedding = await get_embedding(query)

    # ef_search=80 lifts HNSW recall@10 to ~98% at modest latency cost.
    # random_page_cost=1.1 reflects SSD storage; the postgres default of 4
    # makes the planner avoid the HNSW index in favor of a seq+sort, which
    # is faster on small tables but degrades linearly as the vault grows.
    # Both SET LOCALs scope to the current transaction.
    await session.execute(text("SET LOCAL hnsw.ef_search = 80"))
    await session.execute(text("SET LOCAL random_page_cost = 1.1"))

    # Over-fetch by 5x: HNSW is logarithmic so this is essentially free, and it
    # gives the per-note dedup enough headroom when a note contributes many chunks.
    overfetch = max(limit * 5, 50)
    stmt = (
        select(NoteEmbedding, NoteMetadata)
        .join(NoteMetadata, NoteEmbedding.note_id == NoteMetadata.id)
    )
    stmt = apply_note_filters(
        stmt, folder=folder, tags=tags, frontmatter=frontmatter, user_id=user_id
    )
    stmt = stmt.order_by(
        NoteEmbedding.embedding.cosine_distance(query_embedding)
    ).limit(overfetch)

    result = await session.execute(stmt)
    rows = result.fetchall()

    seen: set[int] = set()
    deduped: list[tuple] = []
    for ne, nm in rows:
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
