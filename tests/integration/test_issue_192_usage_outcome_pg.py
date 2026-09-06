"""Real-Postgres gate for the usage page's outcome column (#192, design D9).

What is asserted here *is* the database's behaviour, so a fake session cannot
see it:

* **`params->>'x'` yields `text`, always, and this query casts nothing.** The
  hazard being closed is `/admin/performance`'s unguarded
  `(params->>'over_quota')::boolean`: one row carrying anything that is not
  `true`/`false` raises `invalid input syntax for type boolean` and 500s the
  page — for every user, until that row ages out of the window. A string
  assertion on the SQL fragment cannot prove the cast is gone; running the
  query against a row that would have triggered it can.
* **The three values reach the mapping intact**, including a `NULL` for a row
  with no marker, so nothing is discarded between the query and the template.
* **The mapping's precedence over real rows.** `tool_exception` renders as a
  failure carrying the exception class; any other marker as a refusal naming
  it; `over_quota` as a quota refusal; a malformed over-quota value as a
  refusal showing the raw text. A row with none renders no outcome at all.

Skipped unless `PGVECTOR_TEST_ADMIN_URL` names a throwaway Postgres *server*
(the harness creates and drops its own database):

    docker run --rm -d --name pgvector-test -e POSTGRES_PASSWORD=test \\
        -p 55432:5432 pgvector/pgvector:pg16
    PGVECTOR_TEST_ADMIN_URL=postgresql+asyncpg://postgres:test@localhost:55432/postgres \\
        pytest -q tests/integration/test_issue_192_usage_outcome_pg.py
    docker rm -f pgvector-test
"""
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import _harness
from src.control_panel.routes import _usage_outcome
from src.models.db import UsageLog, User
from src.services.usage_filters import recent_logs, resolve_filters

DIM = 64

pytestmark = [
    pytest.mark.asyncio(loop_scope="module"),
    _harness.requires_pgvector,
]


@pytest.fixture(scope="module")
def migrated_url():
    yield from _harness.throwaway_database("usage_outcome_192", DIM)


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def sessionmaker(migrated_url):
    engine = create_async_engine(migrated_url, poolclass=None)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest_asyncio.fixture(loop_scope="module")
async def clean(sessionmaker):
    async with sessionmaker() as session:
        await session.execute(delete(UsageLog))
        await session.execute(delete(User))
        await session.commit()
    yield sessionmaker
    async with sessionmaker() as session:
        await session.execute(delete(UsageLog))
        await session.execute(delete(User))
        await session.commit()


def _log(tool, *, params=None, duration=1, ago_minutes=1):
    return UsageLog(
        tool=tool,
        params=params,
        duration_ms=duration,
        response_size=1,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=ago_minutes),
    )


def _filters():
    options = {"users": [], "keys": [], "tools": []}
    return resolve_filters("24h", None, None, None, None, options)


async def _rows(session):
    """`{tool: outcome}` for the seeded window, oldest marker last."""
    rows = await recent_logs(session, _filters())
    return {row.tool: _usage_outcome(row) for row in rows}


async def test_each_marker_maps_to_its_declared_outcome(clean):
    async with clean() as session:
        session.add_all([
            # 1. A body that ran and raised.
            _log(
                "read_file",
                params={
                    "path": "a.bin",
                    "error": "tool_exception",
                    "error_type": "IsADirectoryError",
                },
            ),
            # 2. A write refused for a read-only credential — the row this
            #    change exists to make distinguishable from a successful write.
            _log("create_note", params={"path": "a.md", "error": "permission_denied"}),
            # 3. The quota gate, whose marker is a JSON *boolean*.
            _log("keyword_search", params={"over_quota": True}),
            # 4. An ordinary successful call.
            _log("list_notes", params={"folder": "Projects"}),
        ])
        await session.commit()
        outcomes = await _rows(session)

    assert outcomes["read_file"] == {
        "kind": "failed",
        "label": "failed",
        "detail": "IsADirectoryError",
    }, "a failure must not render as a refusal"
    assert outcomes["create_note"] == {
        "kind": "refused",
        "label": "refused",
        "detail": "permission_denied",
    }
    assert outcomes["keyword_search"] == {
        "kind": "refused",
        "label": "refused",
        "detail": "over_quota",
    }
    assert outcomes["list_notes"] is None, "a plain call has no outcome"


async def test_a_malformed_over_quota_value_renders_instead_of_500ing(clean):
    """The whole reason the value is read as text and cast nowhere.

    `(params->>'over_quota')::boolean` would raise here and take the page down
    for every user until this row left the window. Reading it as text cannot,
    and the row is *shown* — it is not an ordinary successful call, and the
    operator is the one who can tell whether the value is a bug.
    """
    async with clean() as session:
        session.add_all([
            _log("semantic_search", params={"over_quota": "yes-ish"}),
            _log("list_notes", params={"folder": "Projects"}),
        ])
        await session.commit()
        # The query itself must not raise — that is the assertion.
        outcomes = await _rows(session)

    assert outcomes["semantic_search"] == {
        "kind": "refused",
        "label": "refused",
        "detail": "yes-ish",
    }
    assert outcomes["list_notes"] is None, "the rest of the window still renders"


async def test_the_exception_marker_beats_a_co_occurring_quota_marker(clean):
    """Precedence, over a row that carries both.

    `_tracked` merges `error`/`error_type` *over the top* of whatever the body
    recorded, so a call that noted its quota state and then raised is a
    `tool_exception`: the exception is the outcome.
    """
    async with clean() as session:
        session.add(
            _log(
                "edit_note",
                params={
                    "over_quota": True,
                    "error": "tool_exception",
                    "error_type": "OSError",
                },
            )
        )
        await session.commit()
        outcomes = await _rows(session)

    assert outcomes["edit_note"]["kind"] == "failed"
    assert outcomes["edit_note"]["detail"] == "OSError"


async def test_a_null_marker_is_null_and_not_the_string_null(clean):
    """`->>` on an absent key yields SQL NULL, which must reach the mapping."""
    async with clean() as session:
        session.add(_log("get_tags", params={"folder": None}))
        await session.commit()
        rows = await recent_logs(session, _filters())

    assert len(rows) == 1
    row = rows[0]
    assert row.error_marker is None
    assert row.error_type is None
    assert row.over_quota is None
    assert _usage_outcome(row) is None


async def test_every_selected_value_survives_to_the_row(clean):
    """Nothing is discarded between the query and the mapping.

    The first draft of this page selected the markers and then dropped two of
    them on the way out; the column names are asserted here so a future edit
    that narrows the SELECT fails loudly instead of quietly rendering "refused"
    with no reason.
    """
    async with clean() as session:
        session.add(
            _log(
                "write_file",
                params={
                    "error": "tool_exception",
                    "error_type": "PermissionError",
                    "over_quota": True,
                },
            )
        )
        await session.commit()
        rows = await recent_logs(session, _filters())

    row = rows[0]
    assert row.error_marker == "tool_exception"
    assert row.error_type == "PermissionError"
    assert row.over_quota == "true", "a JSON boolean renders as lowercase text"
    # And the columns the page already read are untouched.
    assert row.tool == "write_file"
    assert row.duration_ms == 1
    assert row.created_at is not None


async def test_a_null_params_row_is_not_an_outcome(clean):
    """Rows predating any marker carry `params = NULL` entirely."""
    async with clean() as session:
        session.add(_log("get_recent", params=None))
        await session.commit()
        rows = await recent_logs(session, _filters())

    assert _usage_outcome(rows[0]) is None
