"""Real-Postgres gate for #310: an unstorable value in `params` no longer
loses the `usage_logs` row, and an unstorable coalesced row is dropped once
instead of being retried every tick.

What only a real server can show: that PostgreSQL's `jsonb` input actually
accepts the rendered values (the three raw ones are rejected with class-22
SQLSTATEs, which the identity-renderer test proves against the same server),
and that the writer's classification of that rejection is `unstorable`.

Skipped unless `PGVECTOR_TEST_ADMIN_URL` names a throwaway Postgres server; run
it with `make test-integration`.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import _harness
import src.mcp_server.auth as mcp_auth
import src.mcp_server.tools as tools
import src.services.rate_limits as rate_limits
from src.auth.session import current_actor, current_principal, current_user_id
from src.services import refusals, security_events, usage_params

DIM = 64
NAN = float("nan")
INF = float("inf")

pytestmark = [
    pytest.mark.asyncio(loop_scope="module"),
    _harness.requires_pgvector,
]


@pytest.fixture(scope="module")
def migrated_url():
    yield from _harness.throwaway_database("issue_310_usage_params", DIM)


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def engine_maker(migrated_url):
    engine = create_async_engine(migrated_url, pool_size=4, max_overflow=0)
    inserts: list[str] = []

    def before(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("INSERT INTO USAGE_LOGS"):
            inserts.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", before)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield maker, inserts
    await engine.dispose()


@pytest_asyncio.fixture(loop_scope="module")
async def db(engine_maker, monkeypatch):
    maker, inserts = engine_maker
    async with maker() as session:
        await session.execute(text("DELETE FROM usage_logs"))
        await session.commit()
    monkeypatch.setattr(tools, "async_session", maker)
    events: list[tuple[str, dict]] = []
    real_emit = security_events.emit

    def capture(event_name, **kw):
        events.append((event_name, kw))
        return real_emit(event_name, **kw)

    monkeypatch.setattr(security_events, "emit", capture)
    rate_limits.reset_state_for_tests()
    inserts.clear()
    yield maker, inserts, events
    rate_limits.reset_state_for_tests()


async def _rows(maker, tool):
    async with maker() as session:
        result = await session.execute(
            text("SELECT params FROM usage_logs WHERE tool = :t ORDER BY id"),
            {"t": tool},
        )
        return [r[0] for r in result]


def _values(tool, params):
    return dict(
        key_id=None, oauth_token_id=None, user_id=None, tool=tool,
        params=params, duration_ms=1, response_size=0,
        actor_kind="api_key", actor_label="agent", actor_ref="omcp_x",
    )


# ── 3.1 each bad value lands through write_usage_row ─────────────────────


CASES = [
    ("nul_top", {"query": "a\x00b"},
     {"query": "a\\x00b", "rendered_params": ["query"]}),
    ("nul_nested_value", {"frontmatter": {"k": "v\x00"}},
     {"frontmatter": {"k": "v\\x00"}, "rendered_params": ["frontmatter"]}),
    ("nul_nested_key", {"frontmatter": {"k\x00": 1}},
     {"frontmatter": {"k\\x00": 1}, "rendered_params": ["frontmatter"]}),
    ("surrogate_top", {"path": "x\ud800"},
     {"path": "x\\ud800", "rendered_params": ["path"]}),
    ("surrogate_nested", {"frontmatter": {"t": ["\udfff"]}},
     {"frontmatter": {"t": ["\\udfff"]}, "rendered_params": ["frontmatter"]}),
    ("non_finite_nested", {"frontmatter": {"a": NAN, "b": INF, "c": -INF}},
     {"frontmatter": {"a": ".nan", "b": ".inf", "c": "-.inf"},
      "rendered_params": ["frontmatter"]}),
    ("non_json", {"tags": ("a", "b")},
     {"tags": ["a", "b"], "rendered_params": ["tags"]}),
    ("literal_escape", {"find": ["\x00", "\\x00"]},
     {"find": ["\\x00", "\\\\x00"], "rendered_params": ["find"]}),
]


@pytest.mark.parametrize("name, params, stored", CASES, ids=[c[0] for c in CASES])
async def test_each_unstorable_value_lands_rendered(db, name, params, stored):
    maker, _, _ = db
    tool = f"probe_{name}"
    assert await tools.write_usage_row(_values(tool, params)) is True
    assert await _rows(maker, tool) == [stored]


async def test_the_raw_values_are_really_rejected_by_this_server(db, monkeypatch):
    """With the renderer disabled, the same server refuses every raw value
    with a data-class SQLSTATE, classified `unstorable` — so the passing test
    above is evidence of the rendering, not of a lenient server."""
    maker, _, _ = db
    monkeypatch.setattr(tools, "render_usage_params", lambda p: p)
    for name, params, _stored in CASES:
        if name in ("non_json", "literal_escape"):
            continue  # not rejected by the server (TypeError / storable)
        outcome = await tools.write_usage_row_outcome(_values(f"raw_{name}", params))
        assert outcome is tools.UsageWriteOutcome.UNSTORABLE, name
    assert await _rows(maker, "raw_nul_top") == []


async def test_clean_and_null_params_are_unchanged(db):
    maker, _, _ = db
    clean = {"query": "hello \\ world", "limit": 5}
    assert await tools.write_usage_row(_values("probe_clean", clean))
    assert await _rows(maker, "probe_clean") == [clean]
    assert await tools.write_usage_row(_values("probe_null", None))
    assert await _rows(maker, "probe_null") == [None]


# ── 3.2 end to end through _tracked ───────────────────────────────────────


@tools._tracked("issue_310_probe", ["query", "frontmatter"], resource_class="light")
async def _probe(query: str = "", frontmatter: dict | None = None) -> str:
    return "ran"


async def _call(monkeypatch, **kwargs):
    monkeypatch.setattr(tools, "_vault_admission_error", lambda: None)

    async def no_quota():
        return None

    monkeypatch.setattr(tools, "_quota_admission_error", no_quota)
    tokens = [
        (current_principal, current_principal.set(("api_key", 310))),
        (mcp_auth.current_api_key_id, mcp_auth.current_api_key_id.set(None)),
        (current_user_id, current_user_id.set(None)),
        (current_actor, current_actor.set(("api_key", "probe key", "omcp_abc"))),
    ]
    try:
        return await _probe(**kwargs)
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


async def test_a_tracked_call_with_a_nul_argument_lands_its_row(db, monkeypatch):
    maker, _, _ = db
    monkeypatch.setattr(tools, "_bucket_admission", lambda write: None)
    assert await _call(monkeypatch, query="a\x00b",
                       frontmatter={"x": NAN}) == "ran"
    rows = await _rows(maker, "issue_310_probe")
    assert len(rows) == 1
    assert rows[0]["query"] == "a\\x00b"
    assert rows[0]["frontmatter"] == {"x": ".nan"}
    assert rows[0]["rendered_params"] == ["frontmatter", "query"]


async def test_the_unencodable_argument_refusal_lands_its_row(db, monkeypatch):
    maker, _, _ = db
    monkeypatch.setattr(tools, "_bucket_admission", lambda write: None)
    result = await _call(monkeypatch, query="x\ud800")
    assert "argument_not_encodable" in result
    rows = await _rows(maker, "issue_310_probe")
    assert len(rows) == 1
    assert rows[0]["error"] == "argument_not_encodable"
    assert rows[0]["query"] == "x\\ud800"
    assert rows[0]["rendered_params"] == ["query"]


async def test_a_rate_refusal_for_a_surrogate_argument_lands_first_time(db, monkeypatch):
    maker, inserts, events = db
    monkeypatch.setattr(
        tools, "_bucket_admission", lambda write: (refusals.SCOPE_PRINCIPAL, 5)
    )
    result = await _call(monkeypatch, query="x\ud800")
    assert "rate_limited" in result
    rows = await _rows(maker, "issue_310_probe")
    assert len(rows) == 1 and len(inserts) == 1
    assert rows[0]["error"] == "rate_limited"
    assert rows[0]["query"] == "x\\ud800"
    assert rows[0]["rendered_params"] == ["query"]
    assert not [e for e, _ in events if e == "usage_log_failed"]


# ── 3.3 the drop, against the real rejection ─────────────────────────────


async def test_an_unstorable_flushed_row_is_dropped_once(db, monkeypatch):
    maker, inserts, events = db
    monkeypatch.setattr(tools, "render_usage_params", lambda p: p)
    template = dict(_values("issue_310_drop", {"query": "a\x00b",
                                                "error": "rate_limited"}))
    principal = ("api_key", 3101)

    def refuse():
        return rate_limits.record_rate_refusal(
            principal, "issue_310_drop", "rate_limited", "principal",
            lambda: template,
        )

    first = refuse()
    assert await rate_limits.write_planned_row(first) is False  # dropped
    assert refuse() is None and refuse() is None  # folded: pending 2
    entry = first.entry
    entry.windows[first.key].started -= 10_000
    inserts.clear()
    assert await rate_limits.flush_expired() == 0
    assert len(inserts) == 1  # exactly one failing INSERT
    assert first.key not in entry.windows
    dropped = [kw for e, kw in events if e == "usage_refusal_row_dropped"]
    assert [d["count"] for d in dropped] == [1, 2]
    inserts.clear()
    assert await rate_limits.flush_expired() == 0
    assert await rate_limits.flush_all() == 0
    assert inserts == []
    assert await _rows(maker, "issue_310_drop") == []
    assert entry.in_flight == 0


def test_module_is_importable():
    assert usage_params.RENDERED_PARAMS_KEY == "rendered_params"
