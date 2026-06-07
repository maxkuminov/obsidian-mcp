"""Integration tests for configurable FTS — exercises real PostgreSQL stemming.

These verify the end-to-end semantics the unit tests can't: that the Norwegian
stemmer matches `datasenter` against `datasenteret`, that `english` does not,
that `simple` matches only exact forms, and that a multi-config list matches
across languages.

The index side is built through the real `index_tsvector_sql` helper; the query
side mirrors `combined_tsquery`'s OR-of-`websearch_to_tsquery` shape in raw SQL
so the whole match runs in one round-trip. (`combined_tsquery`'s SQLAlchemy
structure is asserted separately in `test_fts.py`.)

Requires a Postgres instance with the standard `norwegian` and `simple`
text-search configs installed (both ship with PostgreSQL). The whole module is
skipped when `TEST_DATABASE_URL` is unset or unreachable, so the offline suite
in `test_fts.py` always runs while these light up in CI/dev with a database.

Run with e.g.:
    TEST_DATABASE_URL=postgresql+asyncpg://obsidian_mcp:pass@localhost/obsidian_mcp \
        pytest tests/test_fts_integration.py
"""
import os

import pytest
from sqlalchemy import text

import src.services.fts as fts
from src.services.fts import index_tsvector_sql

DB_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DB_URL, reason="TEST_DATABASE_URL not set — skipping FTS integration tests"
)


@pytest.fixture
async def conn():
    """A throwaway async connection, rolled back after each test."""
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(DB_URL)
    try:
        async with engine.connect() as connection:
            yield connection
            await connection.rollback()
    finally:
        await engine.dispose()


def _query_frag(configs):
    """Raw-SQL OR of websearch_to_tsquery over `configs`, matching
    `combined_tsquery`'s semantics with bound, cast config names."""
    return " || ".join(
        f"websearch_to_tsquery(CAST(:fts_cfg_{i} AS regconfig), :q)"
        for i in range(len(configs))
    )


async def _matches(conn, configs, body, query):
    """True iff `query` parsed under `configs` matches `body`'s tsvector,
    where the tsvector is built via the production `index_tsvector_sql`."""
    orig = fts.settings.fts_configs
    fts.settings.fts_configs = configs
    try:
        tsv_frag, params = index_tsvector_sql("content")
    finally:
        fts.settings.fts_configs = orig
    sql = f"SELECT ({tsv_frag}) @@ ({_query_frag(configs)})"
    row = await conn.execute(text(sql), {"content": body, "q": query, **params})
    return bool(row.scalar())


# "datasenteret" = "the data center" (definite form); stem is "datasenter".
NORWEGIAN = "Vi bygde datasenteret i fjor."


@pytest.mark.asyncio
async def test_norwegian_stemmer_matches_inflected_form(conn):
    assert await _matches(conn, ["norwegian"], NORWEGIAN, "datasenter")


@pytest.mark.asyncio
async def test_english_config_misses_norwegian_inflection(conn):
    assert not await _matches(conn, ["english"], NORWEGIAN, "datasenter")


@pytest.mark.asyncio
async def test_simple_matches_exact_form_only(conn):
    assert await _matches(conn, ["simple"], NORWEGIAN, "datasenteret")
    assert not await _matches(conn, ["simple"], NORWEGIAN, "datasenter")


@pytest.mark.asyncio
async def test_multi_config_matches_either_language(conn):
    body = "The servers run fast. Vi bygde datasenteret."
    assert await _matches(conn, ["english", "norwegian"], body, "server")
    assert await _matches(conn, ["english", "norwegian"], body, "datasenter")


@pytest.mark.asyncio
async def test_default_english_equals_hardcoded_baseline(conn):
    """The default `["english"]` path must agree with the historical
    hardcoded `to_tsvector('english', …)` / `websearch_to_tsquery('english', …)`
    SQL it replaced."""
    body = "The servers are running."
    assert await _matches(conn, ["english"], body, "run")
    baseline = await conn.execute(
        text(
            "SELECT to_tsvector('english', :c) "
            "@@ websearch_to_tsquery('english', :q)"
        ),
        {"c": body, "q": "run"},
    )
    assert baseline.scalar() is True
