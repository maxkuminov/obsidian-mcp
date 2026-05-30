"""Issue #16: numeric settings must reject 0/negative (and absurd dims).

Without bounds, INDEX_INTERVAL_SECONDS<=0 turns the periodic indexer into a
busy-loop (asyncio.sleep(0)), EMBEDDING_DIMENSIONS<=0 produces invalid
`vector(<dim>)` DDL, and CHUNK_SIZE<=0 with CHUNK_OVERLAP=0 makes the chunker
loop forever. These constraints fail fast at Settings() instantiation instead.
"""
import pytest
from pydantic import ValidationError

from src.config import Settings


def _make(**overrides):
    return Settings(_env_file=None, **overrides)


def test_defaults_still_valid():
    s = _make()
    assert s.index_interval_seconds == 300
    assert s.embedding_dimensions == 1024
    assert s.chunk_size == 512
    assert s.chunk_overlap == 0


@pytest.mark.parametrize("value", [0, -1, -300])
def test_index_interval_seconds_rejects_non_positive(value):
    with pytest.raises(ValidationError):
        _make(index_interval_seconds=value)


@pytest.mark.parametrize("value", [0, -1, 16001, 100000])
def test_embedding_dimensions_out_of_range(value):
    with pytest.raises(ValidationError):
        _make(embedding_dimensions=value)


@pytest.mark.parametrize("value", [0, -1, -512])
def test_chunk_size_rejects_non_positive(value):
    with pytest.raises(ValidationError):
        _make(chunk_size=value)


@pytest.mark.parametrize("value", [-1, -100])
def test_chunk_overlap_rejects_negative(value):
    with pytest.raises(ValidationError):
        _make(chunk_overlap=value)


def test_chunk_overlap_zero_allowed():
    # Zero overlap is the documented default and must stay valid.
    assert _make(chunk_overlap=0).chunk_overlap == 0


def test_sane_operator_overrides_still_validate():
    s = _make(
        index_interval_seconds=600,
        embedding_dimensions=3072,
        chunk_size=256,
        chunk_overlap=32,
    )
    assert s.index_interval_seconds == 600
    assert s.embedding_dimensions == 3072
    assert s.chunk_size == 256
    assert s.chunk_overlap == 32
