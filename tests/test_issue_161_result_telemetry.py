"""Search result telemetry in `usage_logs.params` (#161).

What `/admin/search-analytics` can say is exactly what this contract records,
so the parts worth pinning are the ones that are silent when they break:

* **The byte budget is enforced at the record site.** `_tracked` merges
  `timing.current()` over its logged params *after* `_truncate_params` has run,
  so nothing downstream bounds these keys. A regression here does not fail a
  call — it writes a params blob orders of magnitude larger than every other
  row in `usage_logs`.
* **`result_count` is the full count**, not the number of paths that fit. The
  zero-result view reads it, and a count clipped to the logging cap would
  report a search that found forty notes as having found ten.
* **`find_related`'s two operational failures carry distinct error markers.**
  They used to return a plain string with no marker at all, which made "the
  note you named does not exist" indistinguishable *in the log* from "the vault
  holds nothing near this note" — and the second is the whole point of the
  zero-result view.

Fully offline; the fake session is the one `test_search_phase_timing.py` uses.
"""

import asyncio
import os
import tempfile

import pytest

os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
os.chdir(tempfile.gettempdir())

import src.mcp_server.tools as tools  # noqa: E402
from src.services import timing  # noqa: E402
from src.services.usage_stats import (  # noqa: E402
    PRE_BODY_REFUSAL_ERROR_MARKERS,
)


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class _Result:
    def __init__(self, rows=()):
        self._rows = list(rows)

    def fetchall(self):
        return self._rows

    def all(self):
        return self._rows

    def scalars(self):
        return self

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class _Session:
    def __init__(self, batches=()):
        self._batches = list(batches)

    async def execute(self, clause, *_a, **_k):
        if str(clause).lstrip().upper().startswith("SET LOCAL"):
            return _Result()
        return _Result(self._batches.pop(0) if self._batches else [])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return None


class _FindRelatedSession(_Session):
    """`find_related` reads the source note and its chunks before the vector
    query; those two come off a preamble so a test can make either empty."""

    def __init__(self, source, chunks, batches=()):
        super().__init__(batches)
        self._preamble = [[source] if source else [], chunks]

    async def execute(self, clause, *_a, **_k):
        if not str(clause).lstrip().upper().startswith("SET LOCAL") and self._preamble:
            return _Result(self._preamble.pop(0))
        return await super().execute(clause, *_a, **_k)


class _Note:
    """A `notes_metadata` row, fresh and uncapped.

    Equal hashes and `chunks_truncated=False`, so the staleness and truncation
    annotations (#200, #202) are inert: this file asserts *which paths* the
    telemetry names, and every row it builds must read as an ordinary fresh
    hit. Carrying the attributes here is also what lets the tool-surface slice
    land without editing this file.
    """

    def __init__(
        self,
        note_id=1,
        path="a.md",
        content_hash="h",
        embedded_content_hash="h",
        chunks_truncated=False,
    ):
        self.id = note_id
        self.file_path = path
        self.title = "a"
        self.tags = []
        self.content_hash = content_hash
        self.embedded_content_hash = embedded_content_hash
        self.chunks_truncated = chunks_truncated


def _keyword_row(path):
    return (_Note(1, path), 0.5)


def _semantic_row(note_id, path):
    chunk = type("C", (), {
        "note_id": note_id, "chunk_index": 0, "embedding": [1.0, 0.0, 0.0],
        "chunk_text": "x",
    })()
    return (chunk, _Note(note_id, path), 0.1)


def _related_row(note_id, path, distance=0.1):
    # The last three are `find_related_stmt`'s widened projection (#200 /
    # #202), pre-added so the tool slice lands against a fake that already
    # carries them. Equal hashes, no truncation: a fresh, uncapped neighbour.
    return type("R", (), {
        "note_id": note_id, "file_path": path, "title": path, "tags": [],
        "chunk_text": "x", "distance": distance,
        "content_hash": "h", "embedded_content_hash": "h",
        "chunks_truncated": False,
    })()


class _Captured:
    def __init__(self):
        self.rows: list[tuple[str, dict, int]] = []

    async def __call__(self, tool, params, duration_ms, response_size):
        self.rows.append((tool, params, duration_ms))

    def params_for(self, tool):
        return next(p for t, p, _ in self.rows if t == tool)


@pytest.fixture
def captured(monkeypatch):
    cap = _Captured()
    monkeypatch.setattr(tools, "_log_usage", cap)
    return cap


@pytest.fixture(autouse=True)
def _fake_embedding(monkeypatch):
    async def _fake(_text):
        return [1.0, 0.0, 0.0]

    monkeypatch.setattr("src.services.embeddings.get_embedding", _fake)


@pytest.fixture(autouse=True)
def _no_tsquery(monkeypatch):
    monkeypatch.setattr("src.services.search.combined_tsquery", lambda _q: "tsq")


# --------------------------------------------------------------------------- #
# 1. The pure budget helper.
# --------------------------------------------------------------------------- #
def test_at_most_ten_paths_are_kept():
    paths = [f"n{i}.md" for i in range(40)]
    assert timing.fit_result_paths(paths) == paths[:10]


def test_the_byte_budget_drops_from_the_end_and_keeps_a_prefix():
    """Ten paths that are individually fine and collectively are not.

    The kept list must be a *prefix* — the head of a ranked result set is the
    part worth having — and it must fit the budget with the JSON quoting
    counted, because that is what the column stores.
    """
    paths = [f"{'d' * 300}/{i}.md" for i in range(10)]
    kept = timing.fit_result_paths(paths)

    assert kept == paths[:len(kept)], "the kept paths must be a prefix"
    assert 0 < len(kept) < 10, f"the budget must have bitten; kept {len(kept)}"
    assert timing._json_bytes(kept) <= timing.MAX_RESULT_PATHS_BYTES
    assert timing._json_bytes(paths[:len(kept) + 1]) > timing.MAX_RESULT_PATHS_BYTES, (
        "one more path would have fit — the budget is being applied too early"
    )


def test_a_single_path_over_the_whole_budget_is_dropped_rather_than_cut():
    """A truncated path is a path to a note that does not exist. Dropping it
    leaves `result_paths` a shorter but *true* prefix; cutting it would put a
    fabricated path into the coverage ranking."""
    kept = timing.fit_result_paths(["x" * 4000 + ".md", "b.md"])
    assert kept == []


def test_non_ascii_paths_are_measured_in_utf8():
    """The column stores UTF-8. Measuring `\\uXXXX` escapes would over-count by
    a factor of three and silently drop paths that fit."""
    # ["é"] -> [ " <2 bytes> " ] = 6, not the 10 an escaped é would cost.
    assert timing._json_bytes(["é"]) == 6
    # 700 two-byte characters: 1,404 bytes of UTF-8 and inside the budget, but
    # 4,206 bytes of `\uXXXX` escapes and outside it.
    paths = ["é" * 700 + ".md"]
    assert timing.fit_result_paths(paths) == paths


# --------------------------------------------------------------------------- #
# 2. record_results / record_source_path
# --------------------------------------------------------------------------- #
def test_the_helpers_are_no_ops_without_a_holder():
    assert timing.current() is None
    timing.record_results(["a.md"])
    timing.record_source_path("a.md")
    assert timing.current() is None


def test_result_count_is_the_full_count_not_the_logged_one():
    token = timing.begin()
    try:
        timing.record_results([f"n{i}.md" for i in range(40)])
        holder = timing.current()
    finally:
        timing.clear(token)

    assert holder["result_count"] == 40
    assert len(holder["result_paths"]) == 10


def test_source_path_is_the_path_when_it_fits():
    token = timing.begin()
    try:
        timing.record_source_path("Projects/Alpha.md")
        assert timing.current()["source_path"] == "Projects/Alpha.md"
    finally:
        timing.clear(token)


def test_source_path_falls_back_to_a_sha256_digest_when_it_does_not():
    """Two distinct over-long paths must not collapse onto one analytics row —
    which is exactly what the truncated `path` param would do."""
    import hashlib

    long_a = "d" * 1100 + "/a.md"
    long_b = "d" * 1100 + "/b.md"
    assert len(long_a.encode()) > timing.MAX_SOURCE_PATH_BYTES

    values = []
    for path in (long_a, long_b):
        token = timing.begin()
        try:
            timing.record_source_path(path)
            values.append(timing.current()["source_path"])
        finally:
            timing.clear(token)

    assert values[0] != values[1], "the digest must keep the grouping non-colliding"
    assert values[0] == hashlib.sha256(long_a.encode()).hexdigest()
    assert all(len(v) == 64 and set(v) <= set("0123456789abcdef") for v in values)


def test_a_path_exactly_on_the_bound_is_recorded_whole():
    path = "x" * timing.MAX_SOURCE_PATH_BYTES
    token = timing.begin()
    try:
        timing.record_source_path(path)
        assert timing.current()["source_path"] == path
    finally:
        timing.clear(token)


# --------------------------------------------------------------------------- #
# 3. The three tools, end to end through `_tracked`.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_keyword_search_logs_count_and_paths(monkeypatch, captured):
    rows = [_keyword_row(f"n{i}.md") for i in range(4)]
    monkeypatch.setattr(tools, "async_session", lambda: _Session([rows]))

    await tools.search_notes_impl("needle")

    params = captured.params_for("keyword_search")
    assert params["result_count"] == 4
    assert params["result_paths"] == ["n0.md", "n1.md", "n2.md", "n3.md"]
    # The existing param keys are untouched.
    assert params["query"] == "needle"
    assert "error" not in params


@pytest.mark.asyncio
async def test_semantic_search_logs_count_and_paths(monkeypatch, captured):
    """The spec's scenario: four notes returned, four paths and a count of 4."""
    rows = [_semantic_row(i, f"n{i}.md") for i in range(4)]
    monkeypatch.setattr(tools, "async_session", lambda: _Session([rows]))

    await tools.semantic_search_impl("needle", limit=5)

    params = captured.params_for("semantic_search")
    assert params["result_count"] == 4
    assert params["result_paths"] == ["n0.md", "n1.md", "n2.md", "n3.md"]
    assert "error" not in params


@pytest.mark.asyncio
async def test_the_budget_reaches_usage_logs_untruncated_by_the_wrapper(
    monkeypatch, captured
):
    """The whole reason the budget lives at the record site.

    `_tracked` truncates *named* params at 200 characters and then merges the
    timing holder over the top, so `result_paths` never meets that truncation.
    Fifty long results must therefore arrive already bounded.
    """
    rows = [_keyword_row(f"{'d' * 300}/{i}.md") for i in range(50)]
    monkeypatch.setattr(tools, "async_session", lambda: _Session([rows]))

    await tools.search_notes_impl("needle")

    params = captured.params_for("keyword_search")
    assert params["result_count"] == 50, "the count is not clipped by the budget"
    assert len(params["result_paths"]) < 10
    assert timing._json_bytes(params["result_paths"]) <= timing.MAX_RESULT_PATHS_BYTES
    # And nothing shortened the individual paths: a cut path is a path to a
    # note that does not exist.
    assert all(p in [r[0].file_path for r in rows] for p in params["result_paths"])


@pytest.mark.asyncio
async def test_a_zero_result_search_logs_zero_and_no_marker(monkeypatch, captured):
    monkeypatch.setattr(tools, "async_session", lambda: _Session([[], []]))

    await tools.search_notes_impl("nothing-matches-this")
    await tools.semantic_search_impl("nothing-matches-this")

    for tool in ("keyword_search", "semantic_search"):
        params = captured.params_for(tool)
        assert params["result_count"] == 0
        assert params["result_paths"] == []
        assert "error" not in params, f"{tool} logged a marker for a real zero-result"


# --------------------------------------------------------------------------- #
# 4. find_related: the three ways a call comes back with nothing.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_find_related_logs_source_path_count_and_paths(monkeypatch, captured):
    rows = [_related_row(2, "b.md", 0.1), _related_row(3, "c.md", 0.2)]
    monkeypatch.setattr(
        tools, "async_session",
        lambda: _FindRelatedSession(_Note(), [[1.0, 0.0, 0.0]], [rows]),
    )

    await tools.find_related_impl("a.md", limit=5)

    params = captured.params_for("find_related")
    assert params["source_path"] == "a.md"
    assert params["result_count"] == 2
    assert params["result_paths"] == ["b.md", "c.md"]
    assert "error" not in params


@pytest.mark.asyncio
async def test_find_related_missing_source_is_marked_not_a_zero_result(
    monkeypatch, captured
):
    monkeypatch.setattr(
        tools, "async_session", lambda: _FindRelatedSession(None, [])
    )

    out = await tools.find_related_impl("gone.md")

    assert "Note not found" in out
    params = captured.params_for("find_related")
    assert params["error"] == tools._RELATED_SOURCE_NOT_FOUND_MARKER
    assert params["source_path"] == "gone.md", (
        "a failure that cannot be attributed to a note is not actionable"
    )
    assert params["result_count"] == 0


@pytest.mark.asyncio
async def test_find_related_unembedded_source_is_marked_distinctly(
    monkeypatch, captured
):
    monkeypatch.setattr(
        tools, "async_session", lambda: _FindRelatedSession(_Note(), [])
    )

    out = await tools.find_related_impl("a.md")

    assert "has not been embedded yet" in out
    params = captured.params_for("find_related")
    assert params["error"] == tools._RELATED_SOURCE_NOT_EMBEDDED_MARKER
    assert params["result_count"] == 0
    # The pre-existing phase timing is untouched by the new keys.
    assert "db_ms" in params


@pytest.mark.asyncio
async def test_find_related_on_a_lonely_note_is_a_true_zero_result(
    monkeypatch, captured
):
    """The distinction the whole change turns on. The source exists, it is
    embedded, the exact fallback has already re-run the query, and the vault
    holds nothing near it: that is memory the vault was asked for and does not
    have, and it must reach the zero-result view unmarked."""
    monkeypatch.setattr(
        tools, "async_session",
        lambda: _FindRelatedSession(_Note(), [[1.0, 0.0, 0.0]], [[], []]),
    )

    out = await tools.find_related_impl("a.md")

    assert "No related notes" in out
    params = captured.params_for("find_related")
    assert params["result_count"] == 0
    assert params["result_paths"] == []
    assert "error" not in params, "a lonely note is not an operational failure"
    assert params["exact_fallback"] is True, (
        "the zero-result claim is only honest after the exact re-run"
    )


def test_the_three_find_related_outcomes_are_told_apart_by_their_markers():
    """One value per side of the body/no-body line, and no sharing.

    `vault_anchor_lost_at_publish` exists because that rule was broken once:
    a post-body refusal filed under a pre-body marker vanished from every
    latency percentile. These two are post-body — the body ran and queried the
    database — so they must not appear in the pre-body predicate.
    """
    markers = {
        tools._RELATED_SOURCE_NOT_FOUND_MARKER,
        tools._RELATED_SOURCE_NOT_EMBEDDED_MARKER,
    }
    assert len(markers) == 2, "the two failures must be distinguishable"
    assert not markers & set(PRE_BODY_REFUSAL_ERROR_MARKERS), (
        "these bodies ran; enumerating them as pre-body refusals would drop "
        "them out of the performance page's percentiles"
    )
    assert not markers & {
        tools._NO_VAULT_MARKER,
        tools._UNENCODABLE_ARG_MARKER,
        tools._VAULT_REASSIGNED_MARKER,
        tools._CONFIRMATION_UNAVAILABLE_MARKER,
        tools._ANCHOR_LOST_AT_PUBLISH_MARKER,
    }


@pytest.mark.asyncio
async def test_result_telemetry_does_not_leak_between_calls(monkeypatch, captured):
    """A ContextVar per call, as with the phase timings: a search that returned
    nothing must not inherit the previous call's paths and report them as its
    own retrievals."""
    rows = [_keyword_row("n0.md")]
    monkeypatch.setattr(tools, "async_session", lambda: _Session([rows]))
    await tools.search_notes_impl("first")

    monkeypatch.setattr(tools, "async_session", lambda: _Session([[]]))
    await tools.search_notes_impl("second")

    first, second = [p for t, p, _ in captured.rows if t == "keyword_search"]
    assert first["result_paths"] == ["n0.md"]
    assert second["result_count"] == 0 and second["result_paths"] == []


@pytest.mark.asyncio
async def test_concurrent_searches_keep_their_own_results(monkeypatch, captured):
    class _SlowSession(_Session):
        async def execute(self, clause, *_a, **_k):
            await asyncio.sleep(0.01)
            return await super().execute(clause, *_a, **_k)

    sessions = iter([
        _SlowSession([[_keyword_row("one.md")]]),
        _SlowSession([[_keyword_row("two.md"), _keyword_row("three.md")]]),
    ])
    monkeypatch.setattr(tools, "async_session", lambda: next(sessions))

    await asyncio.gather(
        tools.search_notes_impl("one"),
        tools.search_notes_impl("two"),
    )

    by_query = {p["query"]: p for _, p, _ in captured.rows}
    assert by_query["one"]["result_paths"] == ["one.md"]
    assert by_query["two"]["result_paths"] == ["two.md", "three.md"]


@pytest.mark.asyncio
async def test_a_direct_service_call_outside_a_tracked_tool_records_nothing():
    """The panel and the benchmarks call the services directly. With no holder
    installed every telemetry write must be a silent no-op."""
    from src.services.embeddings import semantic_search
    from src.services.search import full_text_search

    assert timing.current() is None
    assert await semantic_search(_Session([[]]), "needle", limit=5) == []
    assert await full_text_search(_Session([[]]), "needle") == []
    assert timing.current() is None
