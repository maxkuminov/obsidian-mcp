"""Verifies FakeProvider behavior and that get_provider() can be overridden."""
import pytest

from src.config import settings
from src.services import embeddings
from tests.fakes import FakeProvider


@pytest.mark.asyncio
async def test_fake_provider_dim_matches_settings():
    fake = FakeProvider()
    v = await fake.embed_one("hello", kind="query")
    assert len(v) == settings.embedding_dimensions


@pytest.mark.asyncio
async def test_fake_provider_deterministic():
    fake = FakeProvider()
    a = await fake.embed_one("same", kind="query")
    b = await fake.embed_one("same", kind="query")
    assert a == b


@pytest.mark.asyncio
async def test_fake_provider_distinct_inputs_distinct_outputs():
    fake = FakeProvider()
    a = await fake.embed_one("alpha", kind="query")
    b = await fake.embed_one("beta", kind="query")
    assert a != b


@pytest.mark.asyncio
async def test_get_provider_override(monkeypatch):
    monkeypatch.setattr(embeddings, "get_provider", lambda: FakeProvider())
    out = await embeddings.get_embedding("test", kind="query")
    assert len(out) == settings.embedding_dimensions
