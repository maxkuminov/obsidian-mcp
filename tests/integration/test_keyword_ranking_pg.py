"""#344 — keyword ranking, the disjunctive fill, the snippet, and the writers.

Real PostgreSQL, the real indexer and the real tools. What is pinned here is
what the offline suite cannot see: `ts_rank_cd`'s arithmetic under
`RANK_WEIGHTS`, PostgreSQL's own `websearch_to_tsquery` text forms feeding the
fallback, the superset property of the per-config fallback over a generated
query corpus (`english` and `english,simple`), `ts_headline` over the stored
excerpt, the snippet cost gate, both move paths re-heading the vector under
the generation lock, and the rebuild storing the excerpt.

Skipped unless `PGVECTOR_TEST_ADMIN_URL` is set — see `_harness.py`.
"""
import asyncio
import json
import random
import time

import pytest
import pytest_asyncio
import yaml
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import src.database
import src.main as main
import src.mcp_server.tools as tools
from src.config import settings
from src.mcp_server.auth import current_permission
from src.services import fts, indexer, timing
from src.services.fts import index_tsvector_sql
from src.services.search import _snippets, full_text_search
import _harness

pytestmark = [
    _harness.requires_pgvector,
    pytest.mark.asyncio(loop_scope="module"),
]

DIM = 8


@pytest.fixture(scope="module")
def migrated_url():
    yield from _harness.throwaway_database("keyword_ranking_344", DIM)


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def sessionmaker(migrated_url):
    engine = create_async_engine(migrated_url, poolclass=None)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest_asyncio.fixture(loop_scope="module")
async def vault(sessionmaker, monkeypatch, tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    monkeypatch.setattr(settings, "vault_path", str(root), raising=False)
    monkeypatch.setattr(tools.settings, "vault_path", str(root), raising=False)
    monkeypatch.setattr(indexer.settings, "vault_path", str(root), raising=False)
    monkeypatch.setattr(indexer, "async_session", sessionmaker)
    monkeypatch.setattr(tools, "async_session", sessionmaker)
    monkeypatch.setattr(main, "async_session", sessionmaker)
    monkeypatch.setattr(src.database, "async_session", sessionmaker)
    monkeypatch.setattr(indexer, "_is_paused", lambda: False)

    async def permitted(*_a, **_k):
        return True

    monkeypatch.setattr(indexer, "_ancillary_pass_is_permitted", permitted)

    async def noop(*_a, **_k):
        return None

    monkeypatch.setattr(tools, "_log_usage", noop)
    permission = current_permission.set("readwrite")

    async with sessionmaker() as session:
        await session.execute(text("DELETE FROM note_links"))
        await session.execute(text("DELETE FROM note_embeddings"))
        await session.execute(text("DELETE FROM notes_metadata"))
        await session.execute(text("DELETE FROM indexer_state"))
        await session.commit()
    try:
        yield root
    finally:
        current_permission.reset(permission)


def write(root, files: dict):
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, tuple):
            front, body = content
            content = "---\n" + yaml.safe_dump(front, sort_keys=False) + "---\n\n" + body
        path.write_text(content, encoding="utf-8")


async def index(root):
    await indexer.index_vault(user_id=None)


async def search(sessionmaker, query, **kw):
    async with sessionmaker() as session:
        return await full_text_search(session, query, **kw)


async def tracked_search(sessionmaker, query, **kw):
    """`full_text_search` with a telemetry holder, returning `(rows, params)`."""
    token = timing.begin()
    try:
        rows = await search(sessionmaker, query, **kw)
        return rows, dict(timing.current())
    finally:
        timing.clear(token)


async def scalar(sessionmaker, statement, **params):
    async with sessionmaker() as session:
        return (await session.execute(text(statement), params)).scalar()


# ── ranking: title and aliases above the body ────────────────────────────────


async def test_a_note_is_found_first_by_its_own_title(sessionmaker, vault):
    """The issue's reproduction: a two-word title against a ~3,000-word hub
    note repeating the phrase thirty times. Under PostgreSQL's default weights
    the hub wins (3.725 vs 1.0, measured); under `RANK_WEIGHTS` the title does."""
    hub = ("quarterly review filler words here " * 30) + ("other prose " * 1400)
    write(vault, {
        "Quarterly Review.md": "Notes about the period, with no repetition of the phrase.\n",
        "Hub.md": hub + "\n",
    })
    await index(vault)

    rows = await search(sessionmaker, "quarterly review")
    assert [r["path"] for r in rows] == ["Quarterly Review.md", "Hub.md"]
    assert rows[0]["matched_in"] == ["title"] and rows[0]["match"] == "all"
    assert rows[0]["snippet"] is None and rows[0]["head"].startswith("Notes about the period")
    assert rows[1]["matched_in"] == ["body"] and "«quarterly» «review»" in rows[1]["snippet"]
    assert rows[0]["rank"] > rows[1]["rank"]

    # The same two rows under PostgreSQL's default array: the hub first.
    default_order = await scalar(
        sessionmaker,
        "SELECT array_agg(file_path ORDER BY ts_rank_cd(content_tsvector, "
        "  websearch_to_tsquery('english', 'quarterly review')) DESC) FROM notes_metadata",
    )
    assert default_order == ["Hub.md", "Quarterly Review.md"]


async def test_an_alias_finds_the_note(sessionmaker, vault):
    write(vault, {
        "Retro.md": ({"aliases": ["Q3 retro", "retrospective"]}, "nothing of note here\n"),
        "Other.md": "an unrelated note\n",
    })
    await index(vault)
    rows = await search(sessionmaker, "retrospective")
    assert [r["path"] for r in rows] == ["Retro.md"]
    assert rows[0]["matched_in"] == ["title"]


async def test_body_only_order_is_unchanged_by_the_weight_array(sessionmaker, vault):
    write(vault, {
        f"b{i}.md": ("budget " * i + "filler words that differ ") * 3 + "\n" for i in (1, 2, 4, 8)
    })
    await index(vault)
    rows = await search(sessionmaker, "budget")
    ours = [r["path"] for r in rows if r["matched_in"] == ["body"]]
    default = await scalar(
        sessionmaker,
        "SELECT array_agg(file_path ORDER BY ts_rank_cd(content_tsvector, "
        "  websearch_to_tsquery('english', 'budget')) DESC, file_path ASC) "
        "FROM notes_metadata WHERE file_path LIKE 'b%.md'",
    )
    assert ours == default and len(ours) == 4


@pytest.mark.parametrize("configs", [["english"], ["simple", "english"]])
async def test_every_writer_produces_the_same_shape(sessionmaker, vault, monkeypatch, configs):
    """The pass's vector equals migration 031's re-head of its own D part, and
    the rebuild's rewrite — byte for byte (the first application over a
    D-only vector is exact). Under `simple,english` too (V-N4): the writers
    run in `FTS_CONFIGS` order, 031 under the fingerprint's sorted list, and
    the builders sort, so the bytes agree."""
    monkeypatch.setattr(settings, "fts_configs", configs, raising=False)
    body = "a body with words running\n"
    write(vault, {"Shape.md": ({"aliases": "one, two"}, body)})
    await index(vault)
    # A row as a pre-031 build left it (a D-only vector), re-headed the way
    # 031 does it from stored columns — under the fingerprint's configs,
    # which are stored sorted — equals the pass's vector byte for byte.
    body_frag, body_params = fts.body_tsvector_sql("body")
    frag, params = fts.rehead_tsvector_sql("head_title", "head_aliases", sorted(configs))
    async with sessionmaker() as session:
        await session.execute(text(
            "INSERT INTO notes_metadata (user_id, file_path, title, frontmatter, content_hash, content_tsvector) "
            f"VALUES (NULL, 'Raw.md', 'Shape', CAST(:fm AS jsonb), 'h', {body_frag})"
        ), {"fm": json.dumps({"aliases": "one, two"}), "body": body, **body_params})
        await session.execute(text(
            f"UPDATE notes_metadata SET content_tsvector = {frag} WHERE file_path = 'Raw.md'"
        ), {"head_title": "Shape", "head_aliases": "one\ntwo", **params})
        await session.commit()
    same = await scalar(
        sessionmaker,
        "SELECT (SELECT content_tsvector::text FROM notes_metadata WHERE file_path = 'Shape.md') = "
        "       (SELECT content_tsvector::text FROM notes_metadata WHERE file_path = 'Raw.md')",
    )
    assert same is True
    async with sessionmaker() as session:
        await session.execute(text("DELETE FROM notes_metadata WHERE file_path = 'Raw.md'"))
        await session.commit()
    before = await scalar(sessionmaker, "SELECT content_tsvector::text FROM notes_metadata WHERE file_path = 'Shape.md'")
    async with sessionmaker() as session:
        await indexer._rebuild_tsvectors_single_scope_for_tests(session, user_id=None)
    after = await scalar(sessionmaker, "SELECT content_tsvector::text FROM notes_metadata WHERE file_path = 'Shape.md'")
    assert before == after


# ── the disjunctive fill ────────────────────────────────────────────────────


async def test_an_over_specified_query_still_finds_the_target(sessionmaker, vault):
    write(vault, {
        "ab.md": "alpha and beta appear here\n",
        "a.md": "alpha alone\n",
        "cd.md": "gamma with delta\n",
    })
    await index(vault)
    rows, params = await tracked_search(sessionmaker, "alpha beta gamma", limit=10)
    assert [(r["path"], r["match"]) for r in rows] == [("ab.md", "any"), ("a.md", "any"), ("cd.md", "any")]
    assert rows[0]["matched_terms"] == (2, 3) and rows[1]["matched_terms"] == (1, 3)
    assert params["fallback"] == "ran" and params["any_rows"] == 3


async def test_conjunctive_rows_come_first_and_are_never_displaced(sessionmaker, vault):
    files = {f"full{i}.md": "kappa lambda together\n" for i in range(3)}
    files.update({f"part{i}.md": f"kappa alone {i}\n" for i in range(7)})
    write(vault, files)
    await index(vault)
    rows = await search(sessionmaker, "kappa lambda", limit=5)
    assert [r["match"] for r in rows] == ["all"] * 3 + ["any"] * 2
    assert sorted(r["path"] for r in rows[:3]) == ["full0.md", "full1.md", "full2.md"]
    assert [r["path"] for r in rows[3:]] == sorted(r["path"] for r in rows[3:])  # rank ties → file_path


async def test_a_full_page_runs_no_fallback(sessionmaker, vault):
    write(vault, {f"mu{i}.md": "mu nu together\n" for i in range(4)})
    await index(vault)
    rows, params = await tracked_search(sessionmaker, "mu nu", limit=3)
    assert len(rows) == 3 and params["fallback"] == "not_needed" and params["any_rows"] == 0


@pytest.mark.parametrize("query", ["alpha OR beta", 'alpha -"beta gamma"', "alpha"])
async def test_the_users_operators_skip_the_fallback(sessionmaker, vault, query):
    write(vault, {"ab.md": "alpha and beta\n", "g.md": "gamma only\n"})
    await index(vault)
    _rows, params = await tracked_search(sessionmaker, query, limit=10)
    assert params["fallback"] == "skipped", query


async def test_a_negation_is_honoured_and_a_phrase_stays_a_phrase(sessionmaker, vault):
    write(vault, {
        "p.md": "alpha with delta\n",
        "q.md": "the beta gamma phrase is here\n",
        "r.md": "gamma beta reversed\n",
    })
    await index(vault)
    rows = await search(sessionmaker, 'alpha "beta gamma" -delta', limit=10)
    assert [(r["path"], r["match"]) for r in rows] == [("q.md", "any")]


async def test_configs_whose_parses_differ_are_combined_per_config(sessionmaker, vault, monkeypatch):
    """Codex r1 #3, pinned: `english,simple`, body `runs fast`, query `the
    running`. english parses to `'run'` (one unit, ineligible), simple to
    `'the' & 'running'` (eligible). The note matches the conjunction through
    english; the fallback retains english's AND form; `matched_terms` is the
    best config's pair."""
    monkeypatch.setattr(settings, "fts_configs", ["english", "simple"], raising=False)
    write(vault, {"runs.md": "runs fast\n", "walk.md": "walks slowly\n"})
    await index(vault)
    async with sessionmaker() as session:
        parsed = [row[1] for row in (await session.execute(
            text(fts.tsquery_text_sql()), {"q": "the running", "fts_cfg_0": "english", "fts_cfg_1": "simple"}
        )).all()]
    assert parsed == ["'run'", "'the' & 'running'"]

    rows, params = await tracked_search(sessionmaker, "the running", limit=10)
    assert [(r["path"], r["match"]) for r in rows] == [("runs.md", "all")]
    assert rows[0]["matched_terms"] == (1, 1)
    assert params["fallback"] == "ran"


STOP_AND_WORDS = ["the", "a", "running", "runs", "budget", "forecast", "alpha", "beta",
                  "it's", "back\\slash", "2026-10-28", "quarterly", "review", "of", "notes"]


@pytest.mark.parametrize("configs", [["english"], ["english", "simple"]])
async def test_the_fallback_is_a_superset_of_the_conjunction(sessionmaker, vault, monkeypatch, configs):
    """For every generated query: every row matching the combined conjunctive
    tsquery matches the combined fallback tsquery, and every eligible form
    casts. Checked in SQL over the whole table, not over a page."""
    monkeypatch.setattr(settings, "fts_configs", configs, raising=False)
    rng = random.Random(344)
    write(vault, {
        f"s{i}.md": " ".join(rng.choice(STOP_AND_WORDS) for _ in range(12)) + "\n" for i in range(40)
    })
    await index(vault)
    cfg_params = {f"fts_cfg_{i}": c for i, c in enumerate(configs)}
    checked = eligible = 0
    for _ in range(120):
        words = [rng.choice(STOP_AND_WORDS) for _ in range(rng.randint(1, 4))]
        if rng.random() < 0.3:
            words[0] = f'"{words[0]} {rng.choice(STOP_AND_WORDS)}"'
        if rng.random() < 0.3:
            words.append("-" + rng.choice(STOP_AND_WORDS))
        query = " ".join(words)
        async with sessionmaker() as session:
            parsed = [row[1] for row in (await session.execute(
                text(fts.tsquery_text_sql()), {"q": query, **cfg_params}
            )).all()]
            plan = fts.fallback_plan(parsed)
            if not plan.eligible:
                continue
            eligible += 1
            from sqlalchemy import func, select
            from src.models.db import NoteMetadata

            and_q = fts.combined_tsquery(query)
            fb_q = fts.fallback_tsquery(plan, query)
            missing = (await session.execute(
                select(func.count()).select_from(NoteMetadata).where(
                    NoteMetadata.content_tsvector.op("@@")(and_q),
                    ~NoteMetadata.content_tsvector.op("@@")(fb_q),
                )
            )).scalar_one()
            assert missing == 0, (query, parsed, plan.forms)
            checked += 1
    assert eligible > 20 and checked == eligible


# ── snippets ────────────────────────────────────────────────────────────────


async def test_snippets_highlight_the_body_head_the_title_and_count_terms(sessionmaker, vault):
    write(vault, {
        "Budget Forecast.md": "Only the title carries the words.\n",
        "fin.md": "Some intro. The budget forecast for Q3 is here. More text follows.\n",
        "part.md": "a budget without the other word\n",
    })
    await index(vault)
    rows = await search(sessionmaker, "budget forecast", limit=10)
    by_path = {r["path"]: r for r in rows}
    fin = by_path["fin.md"]
    assert "«budget» «forecast»" in fin["snippet"] and fin["matched_in"] == ["body"]
    assert fin["matched_terms"] == (2, 2) and fin["has_excerpt"]
    title = by_path["Budget Forecast.md"]
    assert title["snippet"] is None and title["head"].startswith("Only the title")
    assert title["matched_in"] == ["title"]
    part = by_path["part.md"]
    assert part["match"] == "any" and part["matched_terms"] == (1, 2)
    assert "«budget»" in part["snippet"]


async def test_a_match_beyond_the_excerpt_shows_the_head(sessionmaker, vault, monkeypatch):
    monkeypatch.setattr(settings, "keyword_excerpt_chars", 1_000, raising=False)
    write(vault, {"long.md": ("prose " * 400) + "zebrawood appears only here\n"})
    await index(vault)
    rows = await search(sessionmaker, "zebrawood")
    assert rows[0]["matched_in"] == ["body"] and rows[0]["snippet"] is None
    assert rows[0]["head"].startswith("prose prose")
    excerpt_len = await scalar(sessionmaker, "SELECT length(body_excerpt) FROM notes_metadata WHERE file_path = 'long.md'")
    assert excerpt_len == 1_000


async def test_a_row_without_an_excerpt_is_counted_not_guessed(sessionmaker, vault):
    write(vault, {"old.md": "a pre-031 row mentions xylograph\n", "new.md": "xylograph too\n"})
    await index(vault)
    async with sessionmaker() as session:
        await session.execute(text("UPDATE notes_metadata SET body_excerpt = NULL WHERE file_path = 'old.md'"))
        await session.commit()
    out = await tools.search_notes_impl("xylograph")
    assert "— without excerpt: 1" in out
    assert "**1 of these notes has no stored excerpt yet**" in out and "make rebuild-tsvectors" in out
    rows = await search(sessionmaker, "xylograph")
    old = next(r for r in rows if r["path"] == "old.md")
    assert old["snippet"] is None and old["head"] is None and old["has_excerpt"] is False


async def test_a_title_only_or_split_match_shows_a_clean_head(sessionmaker, vault):
    """V-B1: the body holding one of the two query words is not a body match,
    so no highlight is shown even though `ts_headline` would mark the word.
    V-B2: the head carries no marker of any kind, including PostgreSQL's
    default `<b>`, for a query word inside the head's leading words. V-N5: a
    match split across title and body renders `title+body`."""
    write(vault, {
        "Quarterly Review.md": "Quarterly numbers were up this period, as expected.\n",
        "Zephyr Quill.md": "one two three four five six seven eight nine zephyr eleven twelve\n",
        "Zephyr Notes.md": "a quill is kept here\n",
    })
    await index(vault)
    by_path = {r["path"]: r for r in await search(sessionmaker, "quarterly review", limit=10)}
    qr = by_path["Quarterly Review.md"]
    assert qr["matched_in"] == ["title"] and qr["snippet"] is None
    assert qr["head"].startswith("Quarterly numbers were up")
    for marker in ("«", "»", "<b>", "\x02", "\x03"):
        assert marker not in qr["head"], marker

    by_path = {r["path"]: r for r in await search(sessionmaker, "zephyr quill", limit=10)}
    zq = by_path["Zephyr Quill.md"]
    assert zq["matched_in"] == ["title"] and zq["snippet"] is None
    assert "nine zephyr eleven" in zq["head"]
    split = by_path["Zephyr Notes.md"]
    assert split["match"] == "all" and split["matched_in"] == ["title+body"]
    assert split["snippet"] is None and split["head"].startswith("a quill is kept")
    out = await tools.search_notes_impl("zephyr quill")
    assert "— matched_in: title+body" in out and "«" not in out and "<b>" not in out


async def test_the_call_reads_one_repeatable_read_snapshot(sessionmaker, vault):
    """Codex C-1: the conjunctive, disjunctive and snippet statements share one
    snapshot. Still inside the call's transaction, the session reports
    REPEATABLE READ, read-only."""
    write(vault, {"s.md": "snapshot alpha only\n", "t.md": "snapshot alpha beta\n"})
    await index(vault)
    async with sessionmaker() as session:
        rows = await full_text_search(session, "alpha beta gamma", limit=10)
        isolation = (await session.execute(text("SHOW transaction_isolation"))).scalar()
        read_only = (await session.execute(text("SHOW transaction_read_only"))).scalar()
    assert {r["path"] for r in rows} == {"s.md", "t.md"}
    assert isolation == "repeatable read" and read_only == "on"


async def test_the_snippet_cost_is_bounded_by_the_row_cap_not_the_limit(sessionmaker, vault, monkeypatch):
    monkeypatch.setattr(settings, "keyword_snippet_rows", 2, raising=False)
    write(vault, {f"cap{i}.md": "yarrow grows here\n" for i in range(6)})
    await index(vault)
    rows = await search(sessionmaker, "yarrow", limit=500)
    assert len(rows) == 6
    assert all("has_excerpt" in r for r in rows[:2])
    assert all("has_excerpt" not in r and "snippet" not in r for r in rows[2:])
    out = await tools.search_notes_impl("yarrow", limit=500)
    assert "— snippets: first 2:" in out


async def test_the_worst_case_snippet_latency_is_under_the_gate(sessionmaker, vault, monkeypatch, capsys):
    """Codex r1 #5's gate: twenty distinct 20,000-character excerpts with
    repeated matches, `english,simple`, head-fallback rows included — the
    snippet statements for that page in under 300 ms on the throwaway
    container. The measurement is printed for the record."""
    monkeypatch.setattr(settings, "fts_configs", ["english", "simple"], raising=False)
    rng = random.Random(7)
    vocab = ["ledger", "forecast", "variance", "quarter", "review", "capital", "runway", "margin"]
    files = {}
    for i in range(20):
        words = [rng.choice(vocab) + str(rng.randint(0, 50)) for _ in range(3400)]
        if i % 4 != 0:
            for k in range(0, len(words), 37):
                words[k] = "budget"
            body = " ".join(words)[:20_000]
        else:
            body = " ".join(words)[:20_000]  # title-only rows: a head-fallback parse
        title = f"Budget Plan {i}" if i % 4 == 0 else f"Plan {i}"
        files[f"{title}.md"] = body + "\n"
    write(vault, files)
    await index(vault)
    lengths = await scalar(sessionmaker, "SELECT min(length(body_excerpt)) FROM notes_metadata")
    assert lengths >= 19_900

    async with sessionmaker() as session:
        ids = [r[0] for r in (await session.execute(text("SELECT id FROM notes_metadata ORDER BY id"))).all()]
        assert len(ids) == 20
        q = fts.combined_tsquery("budget")
        await _snippets(session, ids, q, fts.fallback_plan(["'budget'", "'budget'"]))  # warm
        timings = []
        for _ in range(3):
            started = time.perf_counter()
            details = await _snippets(session, ids, q, fts.fallback_plan(["'budget'", "'budget'"]))
            timings.append(time.perf_counter() - started)
    assert len(details) == 20
    assert sum(1 for d in details.values() if d["snippet"] is None and d["head"]) == 5
    best = min(timings) * 1000
    print(f"\n[snippet gate] 20 × 20,000 chars, english,simple, 5 head rows: "
          f"{best:.0f} ms best of 3 ({', '.join(f'{t * 1000:.0f}' for t in timings)})")
    assert best < 300, f"snippet statements took {best:.0f} ms"


# ── the writers: moves, the rebuild, the startup warning ────────────────────


async def move_with_the_tool(root, frm, to):
    out = await tools.move_note_impl(frm, to)
    assert "Moved" in out, out


async def move_with_the_indexer(root, frm, to):
    (root / frm).rename(root / to)
    await indexer.index_vault(user_id=None)


MOVERS = {"move_note": move_with_the_tool, "indexer": move_with_the_indexer}


async def body_part(sessionmaker, path):
    return await scalar(
        sessionmaker,
        "SELECT tsvector_to_array(ts_filter(content_tsvector, '{d}')) FROM notes_metadata WHERE file_path = :p",
        p=path,
    )


@pytest.mark.parametrize("mover", list(MOVERS))
async def test_a_rename_is_searchable_by_its_new_name_and_not_its_old_one(sessionmaker, vault, mover):
    write(vault, {"Alpha.md": ({"aliases": ["Nickname"]}, "just a body about nothing\n")})
    await index(vault)
    before_body = await body_part(sessionmaker, "Alpha.md")
    before_excerpt = await scalar(sessionmaker, "SELECT body_excerpt FROM notes_metadata WHERE file_path = 'Alpha.md'")
    assert [r["path"] for r in await search(sessionmaker, "Alpha")] == ["Alpha.md"]

    await MOVERS[mover](vault, "Alpha.md", "Beta.md")

    beta = await search(sessionmaker, "Beta")
    assert [r["path"] for r in beta] == ["Beta.md"] and beta[0]["matched_in"] == ["title"]
    assert await search(sessionmaker, "Alpha") == []
    assert [r["path"] for r in await search(sessionmaker, "Nickname")] == ["Beta.md"]
    assert await body_part(sessionmaker, "Beta.md") == before_body
    assert await scalar(sessionmaker, "SELECT body_excerpt FROM notes_metadata WHERE file_path = 'Beta.md'") == before_excerpt
    assert [r["path"] for r in await search(sessionmaker, '"body about nothing"')] == ["Beta.md"]


class _RecordingSession(AsyncSession):
    statements: list = []

    async def execute(self, statement, *a, **kw):
        _RecordingSession.statements.append(str(statement))
        return await super().execute(statement, *a, **kw)


async def test_move_note_takes_the_generation_lock_and_checks_the_fingerprint_first(
    sessionmaker, vault, monkeypatch
):
    write(vault, {"Alpha.md": "a body\n"})
    await index(vault)
    engine = sessionmaker.kw["bind"]
    recording = async_sessionmaker(engine, class_=_RecordingSession, expire_on_commit=False)
    monkeypatch.setattr(tools, "async_session", recording)
    _RecordingSession.statements = []
    await move_with_the_tool(vault, "Alpha.md", "Beta.md")
    stmts = _RecordingSession.statements
    lock = next(i for i, s in enumerate(stmts) if "pg_advisory_xact_lock" in s)
    fingerprint = next(i for i, s in enumerate(stmts) if "indexer_state" in s)
    update = next(i for i, s in enumerate(stmts) if s.startswith("UPDATE notes_metadata"))
    assert lock < fingerprint < update, stmts
    assert "ts_filter(content_tsvector, '{d}')" in stmts[update]


async def test_a_fingerprint_mismatch_aborts_the_moves_metadata_write(sessionmaker, vault):
    write(vault, {"Alpha.md": "a body\n"})
    await index(vault)
    async with sessionmaker() as session:
        await session.execute(text(
            "INSERT INTO indexer_state (key, value, updated_at) VALUES ('fts_fingerprint', :v, now()) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
        ), {"v": json.dumps({"configs": ["simple"], "v": 1}, sort_keys=True, separators=(",", ":"))})
        await session.commit()
    try:
        out = await tools.move_note_impl("Alpha.md", "Beta.md")
        assert "DB update failed" in out and "partial" in out
        assert (vault / "Beta.md").exists() and not (vault / "Alpha.md").exists()
        assert await scalar(sessionmaker, "SELECT count(*) FROM notes_metadata WHERE file_path = 'Beta.md'") == 0
        assert await scalar(sessionmaker, "SELECT count(*) FROM notes_metadata WHERE file_path = 'Alpha.md'") == 1
    finally:
        async with sessionmaker() as session:
            await session.execute(text("DELETE FROM indexer_state WHERE key = 'fts_fingerprint'"))
            await session.commit()
    # The next pass's move detection repairs path, title and head.
    await index(vault)
    assert [r["path"] for r in await search(sessionmaker, "Beta")] == ["Beta.md"]
    assert await search(sessionmaker, "Alpha") == []


async def test_a_move_waits_for_the_generation_lock_holder(sessionmaker, vault):
    """The rebuild holds the generation lock for its whole transaction; a
    concurrent `move_note` waits and re-heads afterwards (the other pairing —
    a move committing first makes the rebuild re-read — is
    `test_a_row_that_moves_mid_rebuild_is_repaired_not_skipped`)."""
    from src.services.index_state import INDEX_GENERATION_LOCK_KEY

    write(vault, {"Alpha.md": "a body\n"})
    await index(vault)
    engine = sessionmaker.kw["bind"]
    async with engine.connect() as holder:
        await holder.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": INDEX_GENERATION_LOCK_KEY})
        task = asyncio.create_task(tools.move_note_impl("Alpha.md", "Beta.md"))
        await asyncio.sleep(0.5)
        assert not task.done(), "the move must wait for the lock holder"
        assert await scalar(sessionmaker, "SELECT count(*) FROM notes_metadata WHERE file_path = 'Beta.md'") == 0
        await holder.rollback()  # releases the transaction-scoped lock
        out = await asyncio.wait_for(task, 30)
    assert "Moved" in out and "DB update failed" not in out
    assert [r["path"] for r in await search(sessionmaker, "Beta")] == ["Beta.md"]


async def test_the_rebuild_stores_the_excerpt_and_the_head(sessionmaker, vault):
    write(vault, {f"r{i}.md": ({"aliases": f"nick{i}"}, f"body number {i} with words\n") for i in range(5)})
    await index(vault)
    async with sessionmaker() as session:
        await session.execute(text(
            "UPDATE notes_metadata SET body_excerpt = NULL, "
            "content_tsvector = ts_filter(content_tsvector, '{d}')"
        ))
        await session.commit()
    assert await search(sessionmaker, "nick3") == []
    async with sessionmaker() as session:
        updated = await indexer._rebuild_tsvectors_single_scope_for_tests(session, user_id=None)
    assert updated == 5
    assert await scalar(sessionmaker, "SELECT count(*) FROM notes_metadata WHERE body_excerpt IS NULL") == 0
    assert [r["path"] for r in await search(sessionmaker, "nick3")] == ["r3.md"]
    assert [r["path"] for r in await search(sessionmaker, "r4")] == ["r4.md"]


async def test_startup_warns_about_rows_without_an_excerpt(sessionmaker, vault, caplog):
    write(vault, {"w1.md": "one\n", "w2.md": "two\n"})
    await index(vault)
    caplog.clear()
    with caplog.at_level("WARNING", logger="src.main"):
        await main._warn_missing_keyword_excerpts()
    assert not [r for r in caplog.records if "no stored excerpt" in r.getMessage()]
    async with sessionmaker() as session:
        await session.execute(text("UPDATE notes_metadata SET body_excerpt = NULL"))
        await session.commit()
    with caplog.at_level("WARNING", logger="src.main"):
        await main._warn_missing_keyword_excerpts()
    warnings = [r for r in caplog.records if "no stored excerpt" in r.getMessage()]
    assert len(warnings) == 1 and warnings[0].getMessage().startswith("2 note(s)")
