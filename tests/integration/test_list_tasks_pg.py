"""`list_tasks` against a real PostgreSQL (#350, design D6–D8).

What needs the database: the resume predicate with `'infinity'::date` on both
sides, the owner-scoped join, the header counts in one snapshot, the compact
cursor's path-hash resolution in SQL (`sha256(convert_to(...))` agreeing with
Python's), and that no `notes_metadata.id` reaches a cursor or a line. Rows are
seeded directly (stamped at extraction version 4) so each scenario controls
exactly what is indexed; the not-yet-scanned count is driven by a real pass.

The tool body is called through `__wrapped__`: the property under test is the
body's query and rendering, not `_tracked`'s admission, which the offline
suite pins for every tool.

Skipped unless `PGVECTOR_TEST_ADMIN_URL` is set (see `_harness`).
"""
import base64
import datetime
import json

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import src.database
import src.mcp_server.tools as tools
from src.config import settings
from src.services import indexer
import _harness

pytestmark = [
    _harness.requires_pgvector,
    pytest.mark.asyncio(loop_scope="module"),
]

DIM = 8
D = datetime.date
#: Note ids start far above any line number, so "no id in the output" is a
#: substring check that a line number cannot satisfy by coincidence.
ID_BASE = 700_000_001


@pytest.fixture(scope="module")
def migrated_url():
    yield from _harness.throwaway_database("list_tasks_350", DIM)


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def sessionmaker(migrated_url):
    engine = create_async_engine(migrated_url, poolclass=None)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest_asyncio.fixture(loop_scope="module")
async def db(sessionmaker, monkeypatch, tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    monkeypatch.setattr(settings, "vault_path", str(root), raising=False)
    monkeypatch.setattr(indexer.settings, "vault_path", str(root), raising=False)
    monkeypatch.setattr(indexer.settings, "multi_user_mode", False, raising=False)
    monkeypatch.setattr(tools, "async_session", sessionmaker)
    monkeypatch.setattr(indexer, "async_session", sessionmaker)
    monkeypatch.setattr(src.database, "async_session", sessionmaker)
    monkeypatch.setattr(indexer, "_is_paused", lambda: False)
    indexer._last_full_hash.clear()
    async with sessionmaker() as session:
        for table in ("note_tasks", "note_links", "note_embeddings", "notes_metadata", "users"):
            await session.execute(text(f"DELETE FROM {table}"))
        await session.execute(text(f"ALTER SEQUENCE notes_metadata_id_seq RESTART WITH {ID_BASE}"))
        for uid in (7, 8):
            await session.execute(text(
                "INSERT INTO users (id, username, password_hash) VALUES (:i, :u, 'x')"
            ), {"i": uid, "u": f"user{uid}"})
        await session.commit()
    yield root
    indexer._last_full_hash.clear()


class Seeder:
    def __init__(self, sessionmaker):
        self.sessionmaker = sessionmaker

    async def note(self, path, tasks, *, uid=None, failed=False, truncated=False, version=4):
        """`tasks`: list of dicts (line, status_char, state, text, due, …)."""
        async with self.sessionmaker() as session:
            note_id = (await session.execute(text(
                "INSERT INTO notes_metadata (user_id, file_path, title, content_hash, "
                " extraction_version, tasks_failed, tasks_truncated) "
                "VALUES (:u, :p, 't', 'h', :v, :f, :tr) RETURNING id"
            ), {"u": uid, "p": path, "v": version, "f": failed, "tr": truncated})).scalar_one()
            for i, t in enumerate(tasks, start=1):
                await session.execute(text(
                    "INSERT INTO note_tasks (note_id, line, status_char, state, text, due, "
                    " scheduled, priority, tags, tags_cut, heading_path) "
                    "VALUES (:n, :l, :c, :s, :t, :d, :sc, :pr, :tg, :tc, :h)"
                ), {
                    "n": note_id, "l": t.get("line", i), "c": t.get("status_char", " "),
                    "s": t.get("state", "open"), "t": t.get("text", f"task {i}"),
                    "d": t.get("due"), "sc": t.get("scheduled"), "pr": t.get("priority"),
                    "tg": t.get("tags"), "tc": t.get("tags_cut", False),
                    "h": t.get("heading_path"),
                })
            await session.commit()
            return note_id


@pytest.fixture
def seed(sessionmaker, db):
    return Seeder(sessionmaker)


async def call(uid=None, **kw):
    token = tools.current_user_id.set(uid)
    try:
        return await tools.list_tasks_impl.__wrapped__(**kw)
    finally:
        tools.current_user_id.reset(token)


def lines(out):
    return [ln for ln in out.splitlines() if ln.startswith("- [")]


def next_of(out):
    found = [ln[len("next: "):] for ln in out.splitlines() if ln.startswith("next: ")]
    return found[0] if found else None


def decoded(token):
    return json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))


async def follow(uid=None, **kw):
    seen, pages, cursor = [], [], None
    while True:
        out = await call(uid, cursor=cursor, **kw) if cursor else await call(uid, **kw)
        assert "MCP-REFUSAL" not in out, out
        pages.append(out)
        seen += lines(out)
        cursor = next_of(out)
        if cursor is None:
            return seen, pages
        assert len(pages) < 1000


def refused(out, fragment=""):
    assert out.rstrip().splitlines()[-1].startswith('MCP-REFUSAL {"code":"invalid_argument"'), out
    assert fragment in out
    return True


# ══════════════════════════════════════════════════════════════════════════


async def test_open_tasks_across_the_vault_in_one_call(seed):
    for n in range(21):
        opened = 6 if n < 15 else 5  # 15*6 + 6*5 = 120
        tasks = [{"text": f"open {n}-{i}"} for i in range(opened)]
        tasks += [{"text": f"done {n}-{i}", "status_char": "x", "state": "done"}
                  for i in range(300 // 21 + (1 if n < 300 % 21 else 0))]
        await seed.note(f"Tasks/n{n:02d}.md", tasks)
    out = await call()
    assert len(lines(out)) == 100
    assert "more: yes" in out.splitlines()[0]
    assert next_of(out)
    seen, _ = await follow()
    assert len(seen) == 120 and all("text: open" in ln for ln in seen)


async def test_paging_1234_tasks_returns_each_exactly_once(seed):
    for n in range(20):
        tasks = []
        for i in range(62 if n < 14 else 61):
            due = None if i % 4 else D(2026, 1, 1) + datetime.timedelta(days=(i * 7 + n) % 50)
            tasks.append({"text": f"t{n}-{i}", "due": due})
        await seed.note(f"P/n{n:02d}.md", tasks)
    seen, pages = await follow(limit=97)
    assert len(seen) == 1234 == len(set(seen))
    assert all(len(p) <= settings.max_read_response_chars for p in pages)


async def test_a_page_boundary_between_two_undated_tasks(seed):
    await seed.note("u.md", [{"text": "a"}, {"text": "b"}, {"text": "c"}])
    first = await call(limit=1)
    # The #428 version-2 cursor: the order's leading key is `k`.
    assert decoded(next_of(first))["v"] == 2
    assert decoded(next_of(first))["k"] == ["infinity"]
    second = await call(limit=1, cursor=next_of(first))
    third = await call(limit=1, cursor=next_of(second))
    assert [lines(p)[0].rsplit(": ", 1)[1] for p in (first, second, third)] == ["a", "b", "c"]
    assert next_of(third) is None


async def test_ordering_is_due_then_path_then_line(seed):
    await seed.note("b.md", [{"text": "b-undated"}, {"text": "b-late", "due": D(2026, 3, 1)}])
    await seed.note("a.md", [{"text": "a-undated"}, {"text": "a-early", "due": D(2026, 1, 1)}])
    out = await call()
    assert [ln.rsplit(": ", 1)[1] for ln in lines(out)] == [
        "a-early", "b-late", "a-undated", "b-undated",
    ]


async def test_status_folder_tag_and_date_filters(seed):
    await seed.note("Projects/p.md", [
        {"text": "a", "tags": ["work"], "due": D(2026, 10, 28)},
        {"text": "b", "status_char": "/", "due": D(2026, 10, 29)},
        {"text": "c", "status_char": "-", "state": "cancelled", "due": D(2026, 10, 30)},
        {"text": "d", "status_char": "x", "state": "done", "tags": ["work"]},
        {"text": "e", "scheduled": D(2026, 10, 1)},
    ])
    await seed.note("Other/o.md", [{"text": "f", "tags": ["work"]}])
    await seed.note("Projects_x/q.md", [{"text": "g"}])

    def texts(out):
        return sorted(ln.rsplit(": ", 1)[1] for ln in lines(out))

    assert texts(await call(status="open", folder="Projects/")) == ["a", "b", "e"]
    cancelled = await call(status="cancelled")
    # Rows seeded without a key render `id pending` (#428 D12).
    assert lines(cancelled) == ["- [-] `Projects/p.md`:3 — id pending — due 2026-10-30 — text: c"]
    every = await call(status="all", folder="Projects/")
    assert [ln[:5] for ln in lines(every)] == ["- [ ]", "- [/]", "- [-]", "- [x]", "- [ ]"]
    assert texts(await call(due_by="2026-10-29")) == ["a", "b"]
    assert texts(await call(scheduled_by="2026-10-01")) == ["e"]
    assert texts(await call(folder="Projects/", tags=["#work"])) == ["a"]
    assert texts(await call(status="all", tags=["work"])) == ["a", "d", "f"]
    # The folder prefix escapes LIKE wildcards: `Projects_` is not `Projects/`.
    assert texts(await call(folder="Projects_")) == ["g"]


async def test_a_tag_miss_caused_by_the_cap_is_reported(seed):
    await seed.note("tags.md", [
        {"text": "capped", "tags": [f"t{i:02d}" for i in range(1, 65)], "tags_cut": True},
        {"text": "plain", "tags": ["x"]},
    ])
    out = await call(tags=["t65"])
    assert lines(out) == []
    assert "tasks with cut tags: 1" in out.splitlines()[0]
    out = await call(tags=["x"])
    assert "tasks with cut tags: 1" in out  # conservative: the capped row may carry it
    await seed.note("other.md", [{"text": "o", "tags": ["y"]}])
    out = await call(tags=["y"], folder="other.md")
    assert "tasks with cut tags: 0" in out


async def test_the_failed_and_capped_counts(seed):
    await seed.note("ok.md", [{"text": "ok"}])
    await seed.note("bad.md", [], failed=True)
    await seed.note("big.md", [{"text": "big"}], truncated=True)
    out = await call()
    header = out.splitlines()[0]
    assert "tasks could not be indexed: 1 notes" in header
    assert "over the task cap: 1 notes" in header
    assert "Incomplete list:" in out
    out = await call(folder="ok.md")
    assert "tasks could not be indexed: 0 notes" in out
    assert "over the task cap: 0 notes" in out


async def test_tenancy_rows_and_counts(seed):
    await seed.note("shared.md", [{"text": "null-owned"}], failed=False)
    await seed.note("shared.md", [{"text": "user seven"}], uid=7, failed=True)
    await seed.note("shared.md", [{"text": "user eight"}], uid=8, version=3)
    as7 = await call(7)
    assert [ln.rsplit(": ", 1)[1] for ln in lines(as7)] == ["user seven"]
    assert "tasks could not be indexed: 1 notes" in as7
    assert "not yet scanned for tasks: 0 notes" in as7
    as8 = await call(8)
    assert [ln.rsplit(": ", 1)[1] for ln in lines(as8)] == ["user eight"]
    assert "not yet scanned for tasks: 1 notes" in as8
    assert "tasks could not be indexed: 0 notes" in as8
    ownerless = await call(None)
    assert [ln.rsplit(": ", 1)[1] for ln in lines(ownerless)] == ["null-owned"]
    assert "not yet scanned for tasks: 0 notes" in ownerless
    assert "tasks could not be indexed: 0 notes" in ownerless


async def test_a_cross_owner_cursor_is_refused(seed):
    await seed.note("a.md", [{"text": "a"}, {"text": "b"}], uid=7)
    await seed.note("a.md", [{"text": "x"}, {"text": "y"}], uid=8)
    await seed.note("a.md", [{"text": "n"}, {"text": "m"}])
    cursor = next_of(await call(7, limit=1))
    assert refused(await call(8, limit=1, cursor=cursor), "does not belong")
    assert refused(await call(None, limit=1, cursor=cursor), "does not belong")
    assert "MCP-REFUSAL" not in await call(7, limit=1, cursor=cursor)


async def test_a_filters_changed_cursor_is_refused(seed):
    await seed.note("a.md", [{"text": "a"}, {"text": "b"}])
    cursor = next_of(await call(status="open", limit=1))
    assert refused(await call(status="done", limit=1, cursor=cursor), "does not belong")
    assert refused(await call(status="open", folder="a", limit=1, cursor=cursor))
    # `limit` is not part of the digest.
    assert "MCP-REFUSAL" not in await call(status="open", limit=5, cursor=cursor)


async def test_no_note_id_in_any_cursor_or_line(seed, sessionmaker, monkeypatch):
    long_path = "Deep/" + "d" * 700 + ".md"
    for n in range(4):
        await seed.note(f"n{n}.md", [{"text": f"t{n}-{i}", "tags": ["a"], "heading_path": "H"}
                                     for i in range(3)])
    await seed.note(long_path, [{"text": "x" * 900, "tags": ["t" * 128] * 10}] * 1)
    async with sessionmaker() as session:
        ids = [str(i) for i in (await session.execute(text("SELECT id FROM notes_metadata"))).scalars()]
    assert all(int(i) >= ID_BASE for i in ids)
    for budget in (40_000, 1000):
        monkeypatch.setattr(tools.settings, "max_read_response_chars", budget, raising=False)
        cursor = None
        while True:
            out = await call(limit=2, cursor=cursor) if cursor else await call(limit=2)
            assert len(out) <= budget
            for i in ids:
                assert i not in out
            cursor = next_of(out)
            if cursor is None:
                break
            raw = json.dumps(decoded(cursor))
            for i in ids:
                assert i not in raw


async def test_a_compact_cursor_whose_note_was_deleted_is_refused(seed, sessionmaker, monkeypatch):
    monkeypatch.setattr(tools.settings, "max_read_response_chars", 1000, raising=False)
    path = "p" * 900 + ".md"
    await seed.note(path, [{"text": "one"}, {"text": "two"}])
    first = await call(limit=1)
    cursor = next_of(first)
    assert "h" in decoded(cursor) and len(cursor) <= 2048
    second = await call(limit=1, cursor=cursor)
    # A 900-character path at the 1,000 minimum: the row is abbreviated.
    assert "text: two" in lines(second)[0]
    async with sessionmaker() as session:
        await session.execute(text("DELETE FROM notes_metadata WHERE file_path = :p"), {"p": path})
        await session.commit()
    assert refused(await call(limit=1, cursor=cursor), "no longer indexed")


async def test_a_long_multibyte_path_still_pages(seed):
    path = "語" * 1020 + ".md"
    await seed.note(path, [{"text": "one"}, {"text": "two"}])
    first = await call(limit=1)
    cursor = next_of(first)
    assert len(cursor) <= 2048 and "h" in decoded(cursor)
    assert lines(await call(limit=1, cursor=cursor))[0].endswith("text: two")


async def test_a_note_moved_mid_pagination(seed, sessionmaker):
    await seed.note("a.md", [{"text": "a1"}, {"text": "a2"}])
    await seed.note("b.md", [{"text": "b1"}, {"text": "b2"}])
    first = await call(limit=1)
    async with sessionmaker() as session:
        await session.execute(text("UPDATE notes_metadata SET file_path = 'c.md' WHERE file_path = 'a.md'"))
        await session.commit()
    second = await call(limit=10, cursor=next_of(first))
    assert "MCP-REFUSAL" not in second
    rows = lines(second)
    assert len(rows) == len(set(rows))


async def test_the_character_cap_stops_a_page_early(seed, monkeypatch):
    for n in range(5):
        await seed.note(f"big{n}.md", [{"text": "w" * 900} for _ in range(100)])
    out = await call(limit=500)
    assert len(out) <= settings.max_read_response_chars
    assert "more: yes" in out.splitlines()[0]
    shown = len(lines(out))
    assert 0 < shown < 500
    seen, _ = await follow(limit=500)
    assert len(seen) == 500 == len(set(seen))


async def test_the_not_yet_scanned_count_before_and_after_a_pass(db, sessionmaker):
    (db / "n.md").write_text("- [ ] real\n", encoding="utf-8")
    await indexer.index_vault()
    async with sessionmaker() as session:
        await session.execute(text("UPDATE notes_metadata SET extraction_version = 3"))
        await session.execute(text("DELETE FROM note_tasks"))
        await session.commit()
    before = await call()
    assert "not yet scanned for tasks: 1 notes" in before
    assert "Incomplete list:" in before and lines(before) == []
    await indexer.index_vault()
    after = await call()
    assert "not yet scanned for tasks: 0 notes" in after
    from src.services import task_line

    real = task_line.task_key("real")
    assert lines(after) == [f"- [ ] `n.md`:1 — id {real} — text: real"]
    assert "Incomplete list:" not in after


async def test_invalid_arguments_are_refused(seed):
    assert refused(await call(status="pending"))
    # `tomorrow` is a relative date since #428 (D12); free text is not.
    assert refused(await call(due_by="next week"))
    assert refused(await call(tags=["t"] * 33), "at most 32")
    out = await call(tags=["a" * 128] * 32)
    assert "MCP-REFUSAL" not in out
