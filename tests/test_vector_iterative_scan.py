"""Filtered vector search: iterative scan, distance re-sort, exact fallback.

Filtered semantic search used to lose recall silently. With
`random_page_cost = 1.1` the planner picks an HNSW index scan; a non-iterative
HNSW scan hands up the `ef_search` nearest candidates and the folder/tags/
frontmatter/user predicate is applied *after*, discarding most of them with
nothing to refill — 45 of 120 live folder-filtered probes returned zero rows.
An agent reads an empty result as "the note does not exist".

These tests pin the three things the fix rests on, using a fake session so they
run fully offline (the real behaviour needs a live pgvector and is covered by
`tests/integration/test_search_recall.py`):

  * both vector paths issue `SET LOCAL hnsw.iterative_scan` before the query,
  * rows are re-sorted by cosine distance before the per-note dedupe (
    `relaxed_order` does not promise a globally sorted stream, and the dedupe
    keeps the first chunk it sees per note),
  * a filtered query that comes back empty is re-run as an exact scan
    (`enable_indexscan = off`) rather than believed,
  * `find_related` ranks and reports the cosine distance the *database*
    computed, never one recomputed in NumPy from the round-tripped vectors.
"""

import os
import tempfile

import pytest

# `src.config`'s module-level `Settings()` reads `./.env`; the real one on this
# host carries forbidden host-only keys. Minimal defaults + a chdir away from
# any `.env` BEFORE importing keeps this module offline.
os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
os.chdir(tempfile.gettempdir())

import src.main as main_module  # noqa: E402
import src.mcp_server.tools as tools  # noqa: E402
from src.services import timing  # noqa: E402
from src.services.embeddings import semantic_search  # noqa: E402


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class _Result:
    def __init__(self, rows):
        self._rows = list(rows)

    def fetchall(self):
        return self._rows

    def all(self):
        return self._rows

    def scalars(self):
        return self

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class _RecordingSession:
    """Records every executed statement and replays a scripted row batch.

    `batches` is consumed one entry per *select* (statements that are not
    `SET LOCAL`), so a test can make the first select return nothing and the
    exact-fallback re-run return rows.
    """

    def __init__(self, batches):
        self.statements: list[str] = []
        self.clauses: list = []
        self._batches = list(batches)

    async def execute(self, clause, *_a, **_k):
        sql = str(clause)
        self.statements.append(sql)
        self.clauses.append(clause)
        if sql.lstrip().upper().startswith("SET LOCAL"):
            return _Result([])
        return _Result(self._batches.pop(0) if self._batches else [])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return None


class _Chunk:
    def __init__(self, note_id, chunk_index, embedding, text="chunk"):
        self.note_id = note_id
        self.chunk_index = chunk_index
        self.embedding = embedding
        self.chunk_text = text


class _Note:
    def __init__(self, note_id, path):
        self.id = note_id
        self.file_path = path
        self.title = path.removesuffix(".md")
        self.tags = []


def _semantic_row(note_id, path, embedding, distance):
    """A (NoteEmbedding, NoteMetadata, distance) row as the select returns it."""
    return (_Chunk(note_id, 0, embedding), _Note(note_id, path), distance)


def _set_locals(statements):
    return [s for s in statements if s.lstrip().upper().startswith("SET LOCAL")]


def _selects(statements):
    return [s for s in statements if not s.lstrip().upper().startswith("SET LOCAL")]


def _limit_of(clause) -> int:
    """The literal LIMIT on a Core select. SQLAlchemy renders it as a bound
    parameter, so the compiled SQL string cannot be asserted on directly."""
    return clause._limit_clause.value


@pytest.fixture
def holder():
    """A `_tracked`-owned timing holder, so `record`/`add_ms` have somewhere to
    write. Real lifecycle is exercised in the timing tests."""
    token = timing.begin()
    yield timing.current()
    timing.clear(token)


@pytest.fixture(autouse=True)
def _fake_embedding(monkeypatch):
    async def _fake(_text):
        return [1.0, 0.0, 0.0]

    monkeypatch.setattr("src.services.embeddings.get_embedding", _fake)


# --------------------------------------------------------------------------- #
# semantic_search
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_semantic_search_issues_all_three_set_locals_before_the_select():
    session = _RecordingSession([[]])
    await semantic_search(session, "q", limit=5)

    sets = _set_locals(session.statements)
    joined = " | ".join(sets)
    assert "hnsw.ef_search = 80" in joined
    assert "random_page_cost = 1.1" in joined
    assert "hnsw.iterative_scan = 'relaxed_order'" in joined
    # All three must precede the query, or the transaction runs the old plan.
    first_select = next(
        i
        for i, s in enumerate(session.statements)
        if not s.lstrip().upper().startswith("SET LOCAL")
    )
    assert first_select == 3, session.statements


@pytest.mark.asyncio
async def test_semantic_search_resorts_out_of_order_rows_before_dedupe():
    """`relaxed_order` can emit a nearer chunk after a farther one; the dedupe
    keeps the first chunk per note, so without the re-sort the output order (and
    which chunk represents a note) depends on scan iteration boundaries."""
    rows = [
        _semantic_row(2, "b.md", [0.0, 1.0, 0.0], 0.9),
        _semantic_row(1, "a.md", [1.0, 0.0, 0.0], 0.1),
        _semantic_row(3, "c.md", [0.6, 0.8, 0.0], 0.4),
    ]
    session = _RecordingSession([rows])
    results = await semantic_search(session, "q", limit=10)

    assert [r["path"] for r in results] == ["a.md", "c.md", "b.md"]
    sims = [r["similarity"] for r in results]
    assert sims == sorted(sims, reverse=True), sims


@pytest.mark.asyncio
async def test_filtered_zero_row_result_falls_back_to_exact_scan(holder):
    """Empty from an approximate filtered scan is ambiguous — re-run exactly."""
    fallback_rows = [_semantic_row(7, "B/found.md", [1.0, 0.0, 0.0], 0.2)]
    session = _RecordingSession([[], fallback_rows])

    results = await semantic_search(session, "q", limit=5, folder="B/")

    assert [r["path"] for r in results] == ["B/found.md"]
    assert "SET LOCAL enable_indexscan = off" in " | ".join(
        _set_locals(session.statements)
    )
    # The re-run must be the *identical* statement, not a different query.
    selects = _selects(session.statements)
    assert len(selects) == 2 and selects[0] == selects[1]
    assert holder["exact_fallback"] is True


@pytest.mark.asyncio
async def test_an_ownerless_zero_row_result_falls_back_too(holder):
    """There is no unfiltered vector query left (#127, D1a).

    This used to assert the opposite: a call with no `folder`/`tags`/
    `frontmatter`/`user_id` was treated as unfiltered, so an empty approximate
    result was believed and the O(n) exact scan skipped. The owner mapping is
    now total, so such a call carries `user_id IS NULL` — precisely the shape
    where an HNSW window fills with a named user's vectors and the predicate
    discards every one of them. Believing that empty result returned nothing
    while a NULL-owned match sat in the table.
    """
    fallback_rows = [_semantic_row(1, "a.md", [1.0, 0.0, 0.0], 0.1)]
    session = _RecordingSession([[], fallback_rows])

    results = await semantic_search(session, "q", limit=5)

    assert [r["path"] for r in results] == ["a.md"]
    assert "SET LOCAL enable_indexscan = off" in " | ".join(
        _set_locals(session.statements)
    )
    selects = _selects(session.statements)
    assert len(selects) == 2 and selects[0] == selects[1]
    assert holder["exact_fallback"] is True


@pytest.mark.asyncio
async def test_non_empty_filtered_result_does_not_fall_back(holder):
    rows = [_semantic_row(1, "B/a.md", [1.0, 0.0, 0.0], 0.1)]
    session = _RecordingSession([rows])

    await semantic_search(session, "q", limit=5, folder="B/")

    assert "enable_indexscan" not in " | ".join(session.statements)
    assert holder["exact_fallback"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"folder": "B/"},
        {"tags": ["x"]},
        {"frontmatter": {"k": "v"}},
        {"user_id": 3},
        # The owner predicate is itself a filter, so the empty kwargs shape
        # arms the fallback too (#127, D1a).
        {},
    ],
)
async def test_every_filter_shape_arms_the_fallback(kwargs, holder):
    session = _RecordingSession([[], []])
    await semantic_search(session, "q", limit=5, **kwargs)
    assert "SET LOCAL enable_indexscan = off" in " | ".join(session.statements)


@pytest.mark.asyncio
async def test_semantic_search_returns_a_plain_list(holder):
    """The service's return type must not become a (results, timing) tuple —
    the panel and integration tests consume the list directly."""
    session = _RecordingSession([[_semantic_row(1, "a.md", [1.0, 0.0, 0.0], 0.1)]])
    results = await semantic_search(session, "q", limit=5)
    assert isinstance(results, list)
    assert set(results[0]) == {
        "path", "title", "tags", "chunk", "chunk_index", "similarity",
    }


# --------------------------------------------------------------------------- #
# find_related
# --------------------------------------------------------------------------- #
class _RelatedRow:
    """A row of `find_related_stmt`, which selects no embedding column.

    It deliberately has no `embedding` attribute: the ranking must come from
    the distance the database computed, so a service that reached for the raw
    vector to recompute one would fail here rather than silently disagree with
    its own ORDER BY.
    """

    def __init__(self, note_id, path, distance, chunk="chunk"):
        self.note_id = note_id
        self.file_path = path
        self.title = path.removesuffix(".md")
        self.tags = []
        self.chunk_text = f"{path} {chunk}"
        self.distance = distance


class _FindRelatedSession(_RecordingSession):
    """`find_related_impl` runs three selects before the vector query: the
    source note, then its chunk vectors. Those are scripted separately so the
    scripted `batches` only describe the vector query and its re-run."""

    def __init__(self, source, chunks, batches):
        super().__init__(batches)
        self._source = source
        self._chunks = chunks
        self._preamble = 2

    async def execute(self, clause, *_a, **_k):
        sql = str(clause)
        if not sql.lstrip().upper().startswith("SET LOCAL") and self._preamble:
            self.statements.append(sql)
            self.clauses.append(clause)
            self._preamble -= 1
            return _Result([self._source] if self._preamble == 1 else self._chunks)
        return await super().execute(clause, *_a, **_k)


def _install_find_related_session(monkeypatch, session) -> dict:
    """Wire the fake session in and return a dict that receives the params
    `_tracked` logs. `_tracked` owns the timing holder and clears it on the way
    out, so the usage row is the only place to observe `exact_fallback`."""
    monkeypatch.setattr(tools, "async_session", lambda: session)
    logged: dict = {}

    async def _capture(_tool, params, _duration_ms, _size):
        logged.update(params)

    monkeypatch.setattr(tools, "_log_usage", _capture)
    return logged


@pytest.mark.asyncio
async def test_find_related_issues_the_iterative_scan_setting(monkeypatch, holder):
    session = _FindRelatedSession(
        _Note(1, "src.md"),
        [[1.0, 0.0, 0.0]],
        [[_RelatedRow(2, "b.md", 0.1)]],
    )
    _install_find_related_session(monkeypatch, session)

    await tools.find_related_impl("src.md", limit=10)

    joined = " | ".join(_set_locals(session.statements))
    assert "hnsw.ef_search = 80" in joined
    assert "random_page_cost = 1.1" in joined
    assert "hnsw.iterative_scan = 'relaxed_order'" in joined


@pytest.mark.asyncio
@pytest.mark.parametrize("limit,expected", [(2, 50), (10, 50), (20, 100)])
async def test_find_related_overfetch_matches_semantic_search(
    limit, expected, monkeypatch, holder
):
    """Both vector paths must share `max(5 * limit, 50)`; `find_related` used
    a bare `limit * 5`, so a `limit=2` call fetched 10 chunks and one verbose
    neighbour could own all of them."""
    session = _FindRelatedSession(
        _Note(1, "src.md"),
        [[1.0, 0.0, 0.0]],
        [[_RelatedRow(2, "b.md", 0.1)]],
    )
    _install_find_related_session(monkeypatch, session)

    await tools.find_related_impl("src.md", limit=limit)

    assert _limit_of(session.clauses[-1]) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("limit,expected", [(5, 50), (20, 100)])
async def test_semantic_search_overfetch(limit, expected):
    session = _RecordingSession([[]])
    await semantic_search(session, "q", limit=limit)
    assert _limit_of(session.clauses[-1]) == expected


@pytest.mark.asyncio
async def test_find_related_zero_rows_falls_back_to_exact_scan(monkeypatch, holder):
    session = _FindRelatedSession(
        _Note(1, "src.md"),
        [[1.0, 0.0, 0.0]],
        [[], [_RelatedRow(2, "b.md", 0.1)]],
    )
    logged = _install_find_related_session(monkeypatch, session)

    out = await tools.find_related_impl("src.md", limit=10)

    assert "b.md" in out
    assert "SET LOCAL enable_indexscan = off" in " | ".join(session.statements)
    selects = _selects(session.statements)
    assert selects[-1] == selects[-2]
    assert logged["exact_fallback"] is True


@pytest.mark.asyncio
async def test_find_related_orders_by_distance(monkeypatch, holder):
    session = _FindRelatedSession(
        _Note(1, "src.md"),
        [[1.0, 0.0, 0.0]],
        [[
            _RelatedRow(3, "far.md", 0.95),
            _RelatedRow(2, "near.md", 0.05),
        ]],
    )
    _install_find_related_session(monkeypatch, session)

    out = await tools.find_related_impl("src.md", limit=10)
    assert out.index("near.md") < out.index("far.md"), out


@pytest.mark.asyncio
async def test_find_related_similarity_is_one_minus_the_sql_distance(
    monkeypatch, holder
):
    """The printed number must be the database's own cosine distance, inverted.

    pgvector computes `<=>` over float32; recomputing a similarity in NumPy
    from the round-tripped vectors gives a slightly different value, and near
    the cutoff a slightly different *order* — which is how the displayed
    ranking drifts away from the ORDER BY that chose the rows (and away from
    the recall baseline the benchmark measures against).
    """
    session = _FindRelatedSession(
        _Note(1, "src.md"),
        [[1.0, 0.0, 0.0]],
        [[_RelatedRow(2, "b.md", 0.25), _RelatedRow(3, "c.md", 0.75)]],
    )
    _install_find_related_session(monkeypatch, session)

    out = await tools.find_related_impl("src.md", limit=10)

    sims = [float(line.rsplit("sim: ", 1)[1]) for line in out.splitlines()
            if "sim: " in line]
    assert sims == [0.75, 0.25], out


@pytest.mark.asyncio
async def test_find_related_keeps_the_nearest_chunk_of_a_note(monkeypatch, holder):
    """Per-note dedupe keeps the minimum-distance chunk, and the note is ranked
    at that distance — not at whichever chunk happened to arrive first."""
    session = _FindRelatedSession(
        _Note(1, "src.md"),
        [[1.0, 0.0, 0.0]],
        [[
            # Deliberately out of order, as `relaxed_order` may emit them.
            _RelatedRow(2, "b.md", 0.60, chunk="far-chunk"),
            _RelatedRow(3, "c.md", 0.40),
            _RelatedRow(2, "b.md", 0.10, chunk="near-chunk"),
        ]],
    )
    _install_find_related_session(monkeypatch, session)

    out = await tools.find_related_impl("src.md", limit=10)

    result_lines = [line for line in out.splitlines() if "sim: " in line]
    assert [line.split("(`", 1)[1].split("`)", 1)[0] for line in result_lines] == [
        "b.md", "c.md",
    ], out
    assert "near-chunk" in out and "far-chunk" not in out, out
    assert [float(line.rsplit("sim: ", 1)[1]) for line in result_lines] == [
        0.90, 0.60,
    ], out


def test_find_related_statement_selects_no_embedding_column():
    """Ranking comes from the SQL distance, so the raw vectors are dead weight
    on the wire — an overfetch of 50 chunks × 1024 float32s per call."""
    stmt = tools.find_related_stmt(1, [1.0, 0.0, 0.0], None, 10)
    labels = [c.key for c in stmt.selected_columns]
    assert "embedding" not in labels, labels
    assert "distance" in labels, labels


# --------------------------------------------------------------------------- #
# Startup version guard
# --------------------------------------------------------------------------- #
class _VersionSession:
    def __init__(self, version):
        self._version = version

    async def execute(self, _stmt):
        class _R:
            def __init__(self, v):
                self._v = v

            def first(self):
                return (self._v,) if self._v is not None else None

        return _R(self._version)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return None


def _patch_version(monkeypatch, version):
    monkeypatch.setattr(
        main_module, "async_session", lambda: _VersionSession(version)
    )


@pytest.mark.asyncio
async def test_old_pgvector_exits_naming_the_setting_and_minimum(monkeypatch, caplog):
    _patch_version(monkeypatch, "0.7.4")
    with caplog.at_level("CRITICAL"):
        with pytest.raises(SystemExit) as exc:
            await main_module._check_pgvector_version()
    assert exc.value.code == 1
    message = caplog.text
    assert "hnsw.iterative_scan" in message
    assert "0.8.0" in message


@pytest.mark.asyncio
async def test_supported_pgvector_passes(monkeypatch):
    _patch_version(monkeypatch, "0.8.2")
    await main_module._check_pgvector_version()


@pytest.mark.asyncio
async def test_extension_absent_defers_to_migrations(monkeypatch):
    """A database that has not been migrated yet has no `vector` row; alembic
    installs it. Exiting here would make a fresh deploy unbootable."""
    _patch_version(monkeypatch, None)
    await main_module._check_pgvector_version()


@pytest.mark.asyncio
async def test_unparseable_version_is_refused(monkeypatch):
    _patch_version(monkeypatch, "not-a-version")
    with pytest.raises(SystemExit):
        await main_module._check_pgvector_version()


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("0.8.2", (0, 8, 2)),
        ("0.8", (0, 8)),
        ("1.0.0", (1, 0, 0)),
        ("0.8.0-rc1", (0, 8, 0)),
        ("0.10.0", (0, 10, 0)),
        ("", None),
    ],
)
def test_version_parsing(raw, expected):
    assert main_module._parse_pgvector_version(raw) == expected


def test_version_ordering_is_numeric_not_lexicographic():
    """`'0.10.0' < '0.8.0'` as strings but not as versions — the guard must not
    reject a future 0.10 release."""
    assert main_module._parse_pgvector_version("0.10.0") >= main_module.MIN_PGVECTOR_VERSION
    assert main_module._parse_pgvector_version("0.7.4") < main_module.MIN_PGVECTOR_VERSION


@pytest.mark.asyncio
async def test_sandbox_mode_skips_the_version_guard(monkeypatch):
    """Sandbox mode (registry eval) has no real database; the lifespan must
    return before any startup check runs."""
    called = []

    async def _spy():
        called.append(True)

    monkeypatch.setattr(main_module.settings, "mcp_sandbox_mode", True, raising=False)
    monkeypatch.setattr(main_module, "_check_pgvector_version", _spy)
    monkeypatch.setattr(main_module, "_check_embedding_dim", _spy)

    class _Mgr:
        def run(self):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

    class _Mcp:
        session_manager = _Mgr()

    monkeypatch.setattr(main_module, "mcp", _Mcp())

    cm = main_module.lifespan(object())
    await cm.__aenter__()
    await cm.__aexit__(None, None, None)
    assert called == []
