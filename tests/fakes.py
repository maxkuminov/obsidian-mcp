"""Test doubles for embedding providers."""
from __future__ import annotations

import hashlib

from src.config import settings


class FakeProvider:
    """Deterministic in-memory provider for tests.

    Vectors are derived from sha256 of the input text so the same string
    always produces the same vector, but different strings produce
    different vectors. Width matches `settings.embedding_dimensions` at
    call time.
    """

    def _vector_for(self, text: str) -> list[float]:
        dim = int(settings.embedding_dimensions)
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        # Repeat the digest cyclically and normalize bytes into [-1, 1].
        out: list[float] = []
        for i in range(dim):
            b = digest[i % len(digest)]
            out.append((b - 128) / 128.0)
        return out

    async def embed_one(self, text: str, **_kw) -> list[float]:
        return self._vector_for(text)

    async def embed_batch(self, texts: list[str], **_kw) -> list[list[float]]:
        return [self._vector_for(t) for t in texts]
