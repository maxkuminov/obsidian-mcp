"""`full_text_search` planner hint and deterministic tie-break.

`keyword_search` never used its GIN index on the live vault: 5 index scans
lifetime against 3,655 sequential ones. The planner costs the `notes_metadata`
heap at `relpages` and does not model detoast I/O, so a Seq Scan looks cheap
while it actually detoasts every tsvector out of a 36 MB TOAST table (13,086
buffers per query). The same `SET LOCAL random_page_cost = 1.1` the vector path
already uses flips it to a bitmap index scan.

That hint changes the *plan*, and `ts_rank_cd` produces many exact ties, so
without a deterministic tie-break it would also change which tied rows survive
the LIMIT — a planner tweak silently altering search results. The
`file_path ASC` secondary sort is what keeps the change semantics-preserving.

Offline: a fake session records statements; plan and buffer assertions live in
`tests/integration/test_keyword_plan.py`.
"""

import os
import tempfile

import pytest

os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
os.chdir(tempfile.gettempdir())

from src.services import search  # noqa: E402


class _Result:
    def all(self):
        return []


class _RecordingSession:
    def __init__(self):
        self.statements: list[str] = []
        self.clauses: list = []

    async def execute(self, clause, *_a, **_k):
        self.statements.append(str(clause))
        self.clauses.append(clause)
        return _Result()


@pytest.fixture(autouse=True)
def _fake_tsquery(monkeypatch):
    monkeypatch.setattr(search, "combined_tsquery", lambda _q: "tsq")


@pytest.mark.asyncio
async def test_random_page_cost_is_set_before_the_select():
    session = _RecordingSession()
    await search.full_text_search(session, "needle")

    assert session.statements[0].strip() == "SET LOCAL random_page_cost = 1.1"
    assert len(session.statements) == 2
    assert "SET LOCAL" not in session.statements[1]


@pytest.mark.asyncio
async def test_order_by_carries_the_file_path_tie_break():
    session = _RecordingSession()
    await search.full_text_search(session, "needle")

    order_by = str(session.clauses[-1]).split("ORDER BY", 1)[1]
    assert "rank DESC" in order_by
    assert "notes_metadata.file_path ASC" in order_by
    # The tie-break must come *after* rank, or it becomes the primary sort and
    # keyword search stops ranking at all.
    assert order_by.index("rank DESC") < order_by.index("file_path ASC")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"folder": "Projects/"},
        {"tags": ["work"]},
        {"frontmatter": {"status": "open"}},
        {"user_id": 4},
    ],
)
async def test_hint_and_tie_break_apply_under_every_filter(kwargs):
    session = _RecordingSession()
    await search.full_text_search(session, "needle", **kwargs)

    assert session.statements[0].strip() == "SET LOCAL random_page_cost = 1.1"
    assert "file_path ASC" in str(session.clauses[-1])


@pytest.mark.asyncio
async def test_matching_predicate_is_untouched():
    """The hint may change the plan; it must not change what matches."""
    session = _RecordingSession()
    await search.full_text_search(session, "needle")

    sql = str(session.clauses[-1])
    assert "content_tsvector @@ " in sql
    assert "ts_rank_cd" in sql
