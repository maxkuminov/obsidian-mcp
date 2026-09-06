"""Body outcomes land in JSONB and remain executed work in real SQL readers."""
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import _harness
import src.mcp_server.tools as tools
from src.services import timing
from src.services.tool_outcomes import body_refusal
from src.services.usage_stats import PRE_BODY_REFUSAL_BINDS, executed_sql, pre_body_refusal_sql, tool_aggregates

pytestmark = [_harness.requires_pgvector, pytest.mark.asyncio(loop_scope="module")]


@pytest.fixture(scope="module")
def migrated_url():
    yield from _harness.throwaway_database("body_outcome_263", 64)


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def sessionmaker(migrated_url):
    engine = create_async_engine(migrated_url, poolclass=None)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


async def test_real_refusal_partial_success_and_shadow_are_counted_once(sessionmaker, monkeypatch):
    monkeypatch.setattr(tools, "async_session", sessionmaker)
    monkeypatch.setattr(tools, "_vault_admission_error", lambda: None)
    @tools._tracked("outcome_probe", [])
    async def probe(disposition=None):
        timing.record("concurrency_shadow", {"shadow": True, "code": "slot_timeout"})
        if disposition:
            return body_refusal("body result", "partial_completion" if disposition == "partial" else "not_found", disposition=disposition)
        return 'note text\nMCP-REFUSAL {"code":"not_found"}'

    await probe("refused")
    await probe("partial")
    await probe()
    async with sessionmaker() as session:
        result = (await session.execute(text(
            "SELECT params, " + executed_sql() + " AS executed, " + pre_body_refusal_sql() +
            " AS refused FROM usage_logs ul WHERE tool='outcome_probe' ORDER BY id"
        ), dict(PRE_BODY_REFUSAL_BINDS))).all()
        assert len(result) == 3
        assert all(row.executed and not row.refused for row in result)
        assert [row.params.get("body_outcome") for row in result] == ["refused", "partial", None]
        assert [row.params.get("error") for row in result] == ["not_found", "partial_completion", None]
        assert all(row.params["concurrency_shadow"]["shadow"] for row in result)
        # The production aggregate reader must accept the new JSON fields.
        aggregates = await tool_aggregates(session, window="7d", user_id=None)
        assert any(row["tool"] == "outcome_probe" for row in aggregates)
