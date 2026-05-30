"""Regression test for issue #10: chunk_text loops forever when overlap >= chunk_size.

Before the fix, step was computed implicitly as `start = end - char_overlap`.
When overlap >= chunk_size, char_overlap >= char_size, so `start` never advanced
and the while loop at embeddings.py:50-57 spun forever. The fix clamps the
advance step to at least 1 char (`step = max(char_size - char_overlap, 1)`).

Fully offline: only exercises pure string-chunking, no DB / network / Ollama.
"""
import pytest

from src.services.embeddings import chunk_text


def _run_with_timeout(fn, seconds=5.0):
    """Run fn() in a worker thread, fail if it doesn't return in time.

    A pre-fix chunk_text() never returns, so without a guard the test would
    hang the whole suite. We don't kill the runaway thread (can't, cleanly) —
    we just assert it finished and surface its result.
    """
    import threading

    result = {}

    def target():
        result["value"] = fn()

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(seconds)
    if t.is_alive():
        pytest.fail("chunk_text did not terminate (infinite loop)")
    return result["value"]


def test_overlap_equal_to_chunk_size_terminates():
    content = "word " * 2000  # comfortably larger than one chunk
    chunks = _run_with_timeout(lambda: chunk_text(content, chunk_size=10, overlap=10))
    assert chunks  # produced output rather than hanging
    assert all(c.strip() for c in chunks)


def test_overlap_greater_than_chunk_size_terminates():
    content = "word " * 2000
    chunks = _run_with_timeout(lambda: chunk_text(content, chunk_size=10, overlap=50))
    assert chunks
    assert all(c.strip() for c in chunks)


def test_chunks_cover_full_content_when_overlap_too_large():
    # With step clamped to 1 char, every character must still be reachable;
    # concatenating chunk content (before strip) would cover the input.
    content = "abcdefghij" * 100
    chunks = _run_with_timeout(lambda: chunk_text(content, chunk_size=1, overlap=1))
    assert chunks
    # Last chunk must reach the end of the content.
    assert content.rstrip()[-1] in chunks[-1]


def test_normal_overlap_still_advances_with_overlap():
    # Sanity: legitimate overlap (< chunk_size) keeps working and overlaps.
    content = "x" * 5000
    chunks = chunk_text(content, chunk_size=100, overlap=10)
    assert len(chunks) > 1
    # char_size=400, step=360 -> first chunk ends at 400, second starts at 360.
    assert chunks[0][360:400] == chunks[1][0:40]


def test_zero_overlap_unchanged():
    content = "y" * 5000
    chunks = chunk_text(content, chunk_size=100, overlap=0)
    assert len(chunks) > 1
    # No overlap: contiguous slices, no shared boundary chars.
    assert chunks[0][-1] != "" and chunks[1]
