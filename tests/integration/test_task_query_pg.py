"""`list_tasks_query` / `task_agenda_query` against a real PostgreSQL (#428,
design D12, D13, D18).

What needs the database: every filter's SQL (hierarchical tags over
`tag_prefixes` and, for a row not yet re-derived, over `tags`; `strpos` on
`text_fold`; the scope's folder and note-tag exclusion and its count), every
`order_by`'s keyset predicate including `done`'s mixed direction, the
version-2 cursor's compact-path resolution, the owner-scoped join, and the
agenda's disjoint sections and continuation cursors.

**Rows are inserted directly.** The version-5 extractor that fills the new
`note_tasks` columns is a separate slice (S3); here each column is computed
with the merged pure helpers of `task_line` (`task_key`, `fold_text`,
`tag_prefixes`, `normalise_description`/`description_key`), so each scenario
controls exactly what is indexed. An end-to-end test through the real
extractor belongs to the wiring slice.

Responses are rendered with `task_render`, so the cursors under test are the
ones the renderer actually mints from the last row it showed.

Skipped unless `PGVECTOR_TEST_ADMIN_URL` is set (see `_harness`).
"""
import base64
import dataclasses
import datetime
import json
import re

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.mcp_server import task_render
from src.services import task_line, task_query, task_settings
from src.services.task_query import AgendaArgs, ListTasksArgs, QueryRefusal
import _harness

pytestmark = [
    _harness.requires_pgvector,
    pytest.mark.asyncio(loop_scope="module"),
]

DIM = 8
D = datetime.date
#: Note ids start far above any line number, so "no id in the output" is a
#: substring check a line number cannot satisfy by coincidence.
ID_BASE = 800_000_001
#: The injected clock: 2026-11-10T12:00Z.
NOW = datetime.datetime(2026, 11, 10, 12, 0, tzinfo=datetime.timezone.utc)
BIG = 40_000


@pytest.fixture(scope="module")
def migrated_url():
    yield from _harness.throwaway_database("task_query_428", DIM)


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def maker(migrated_url):
    engine = create_async_engine(migrated_url, poolclass=None)
    m = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield m
    await engine.dispose()


@pytest_asyncio.fixture(loop_scope="module")
async def db(maker):
    async with maker() as session:
        for table in ("note_tasks", "notes_metadata", "task_settings", "users"):
            await session.execute(text(f"DELETE FROM {table}"))
        await session.execute(text(f"ALTER SEQUENCE notes_metadata_id_seq RESTART WITH {ID_BASE}"))
        for uid in (7, 8):
            await session.execute(text(
                "INSERT INTO users (id, username, password_hash) VALUES (:i, :u, 'x')"
            ), {"i": uid, "u": f"user{uid}"})
        await session.commit()
    return maker


def v5_columns(task: dict, ordinal: int, count: int) -> dict:
    """The version-5 columns, computed with the merged S1 helpers exactly as
    the extractor will (D1, D9, D11)."""
    body = task["text"]
    tags = task.get("tags")
    return {
        "task_key": task_line.task_key(task.get("full_text", body)),
        "key_ordinal": ordinal, "key_count": count,
        "description_key": task_line.description_key(task_line.normalise_description(body)),
        "text_fold": task_line.fold_text(body),
        "tag_prefixes": task_line.tag_prefixes(tags) if tags else [],
        "recurrence": task.get("recurrence"),
    }


class Seeder:
    def __init__(self, maker):
        self.maker = maker

    async def note(self, path, tasks, *, uid=None, failed=False, truncated=False,
                   version=5, note_tags=None, v5=True):
        """`tasks`: dicts (text, status_char, state, due, …). `v5=False`
        leaves the version-5 columns NULL (a row awaiting the backfill)."""
        async with self.maker() as session:
            note_id = (await session.execute(text(
                "INSERT INTO notes_metadata (user_id, file_path, title, content_hash, "
                " extraction_version, tasks_failed, tasks_truncated, tags) "
                "VALUES (:u, :p, 't', 'h', :v, :f, :tr, :tg) RETURNING id"
            ), {"u": uid, "p": path, "v": version, "f": failed, "tr": truncated,
                "tg": note_tags})).scalar_one()
            keys: dict[str, int] = {}
            for t in tasks:
                k = task_line.task_key(t.get("full_text", t["text"]))
                keys[k] = keys.get(k, 0) + 1
            seen: dict[str, int] = {}
            params = []
            for i, t in enumerate(tasks, start=1):
                k = task_line.task_key(t.get("full_text", t["text"]))
                seen[k] = seen.get(k, 0) + 1
                cols = v5_columns(t, seen[k], keys[k]) if v5 else {
                    "task_key": None, "key_ordinal": None, "key_count": None,
                    "description_key": None, "text_fold": None, "tag_prefixes": None,
                    "recurrence": None,
                }
                params.append({
                    "n": note_id, "l": t.get("line", i), "c": t.get("status_char", " "),
                    "s": t.get("state", "open"), "t": t["text"], "tc": t.get("text_cut", False),
                    "d": t.get("due"), "sc": t.get("scheduled"), "st": t.get("start"),
                    "dn": t.get("done_on"), "pr": t.get("priority"), "tg": t.get("tags"),
                    "tgc": t.get("tags_cut", False), "h": t.get("heading_path"),
                    "cx": t.get("context"), "cxc": t.get("context_cut", False), **cols,
                })
            if params:
                await session.execute(text(
                    "INSERT INTO note_tasks (note_id, line, status_char, state, text, text_cut, "
                    " due, scheduled, start, done_on, priority, tags, tags_cut, heading_path, "
                    " context, context_cut, task_key, key_ordinal, key_count, description_key, "
                    " text_fold, tag_prefixes, recurrence) "
                    "VALUES (:n, :l, :c, :s, :t, :tc, :d, :sc, :st, :dn, :pr, :tg, :tgc, :h, "
                    " :cx, :cxc, :task_key, :key_ordinal, :key_count, :description_key, "
                    " :text_fold, :tag_prefixes, :recurrence)"
                ), params)
            await session.commit()
            return note_id


@pytest.fixture
def seed(db):
    return Seeder(db)


def settings_for(**kw):
    return dataclasses.replace(task_settings.defaults(), **kw)


DEFAULT = settings_for()


async def query(maker, uid=None, *, ts=DEFAULT, now=NOW, **kw):
    return await task_query.list_tasks_query(uid, ListTasksArgs(**kw), ts, now=now,
                                             session_factory=maker)


async def call(maker, uid=None, *, ts=DEFAULT, now=NOW, budget=BIG, **kw):
    page = await query(maker, uid, ts=ts, now=now, **kw)
    if isinstance(page, QueryRefusal):
        return page
    return task_render.render_list_tasks(page, budget=budget)


async def agenda(maker, uid=None, *, ts=DEFAULT, now=NOW, budget=BIG, **kw):
    page = await task_query.task_agenda_query(uid, AgendaArgs(**kw), ts, now=now,
                                              session_factory=maker)
    if isinstance(page, QueryRefusal):
        return page
    return task_render.render_task_agenda(page, budget=budget)


def rows_of(out):
    return [ln for ln in out.splitlines() if ln.startswith("- [")]


def texts(out):
    return [ln.rsplit("text: ", 1)[1] for ln in rows_of(out)]


def next_of(out):
    found = [ln[len("next: "):] for ln in out.splitlines() if ln.startswith("next: ")]
    return found[0] if found else None


def decoded(token):
    return json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))


async def follow(maker, uid=None, cursor=None, **kw):
    seen, pages = [], []
    while True:
        out = await call(maker, uid, cursor=cursor, **kw)
        assert isinstance(out, str), out
        pages.append(out)
        seen += rows_of(out)
        cursor = next_of(out)
        if cursor is None:
            return seen, pages
        assert len(pages) < 2000


def refused(result, code="invalid_argument"):
    assert isinstance(result, QueryRefusal), result
    assert result.code == code
    return True


# ── list_tasks: filters ──────────────────────────────────────────────────────


async def test_open_tasks_across_the_vault_with_ids(db, seed):
    for n in range(21):
        opened = 6 if n < 15 else 5
        tasks = [{"text": f"open {n}-{i}"} for i in range(opened)]
        tasks += [{"text": f"done {n}-{i}", "status_char": "x", "state": "done"} for i in range(14)]
        await seed.note(f"Tasks/n{n:02d}.md", tasks)
    out = await call(db)
    assert len(rows_of(out)) == 100 and "more: yes" in out.splitlines()[0]
    assert all(re.search(r"`:\d+ — id [0-9a-f]{12} — text: open", ln) for ln in rows_of(out))
    assert "task ids pending: 0 tasks" in out.splitlines()[0]
    # A #350-only call under empty settings has no query line.
    assert not out.splitlines()[1].startswith("Query — ")
    seen, _ = await follow(db)
    assert len(seen) == 120


async def test_status_mapping(db, seed):
    await seed.note("s.md", [
        {"text": "a"}, {"text": "b", "status_char": "/"},
        {"text": "c", "status_char": "-", "state": "cancelled"},
        {"text": "d", "status_char": "x", "state": "done"},
    ])
    assert texts(await call(db)) == ["a", "b"]
    out = await call(db, status="cancelled")
    assert texts(out) == ["c"] and rows_of(out)[0].startswith("- [-] ")
    assert [ln[:5] for ln in rows_of(await call(db, status="all"))] == [
        "- [ ]", "- [/]", "- [-]", "- [x]"]


async def test_due_by_is_inclusive_and_excludes_undated(db, seed):
    await seed.note("d.md", [{"text": "28", "due": D(2026, 10, 28)},
                             {"text": "29", "due": D(2026, 10, 29)},
                             {"text": "30", "due": D(2026, 10, 30)}, {"text": "none"}])
    assert texts(await call(db, due_by="2026-10-29")) == ["28", "29"]


async def test_a_relative_window_resolves_in_the_callers_zone_and_is_echoed(db, seed):
    await seed.note("w.md", [{"text": "a", "due": D(2026, 11, 1)},
                             {"text": "b", "due": D(2026, 11, 11)},
                             {"text": "c", "due": D(2026, 11, 20)}])
    out = await call(db, due_from="tomorrow", due_by="+7d")
    assert texts(out) == ["b"]
    q = out.splitlines()[1]
    assert q.startswith("Query — timezone: UTC — today: 2026-11-10")
    assert "due_from: tomorrow=2026-11-11" in q and "due_by: +7d=2026-11-17" in q


async def test_the_query_line_states_tokyo_and_its_today(db, seed):
    await seed.note("t.md", [{"text": "x", "due": D(2026, 11, 12)}])
    late = datetime.datetime(2026, 11, 10, 20, 0, tzinfo=datetime.timezone.utc)
    out = await call(db, ts=settings_for(timezone="Asia/Tokyo"), now=late, due_by="+7d")
    q = out.splitlines()[1]
    assert "timezone: Asia/Tokyo" in q and "today: 2026-11-11" in q
    assert "due_by: +7d=2026-11-18" in q


async def test_hierarchical_tags_match_nested_and_case_but_not_lookalikes(db, seed):
    await seed.note("g.md", [
        {"text": "bob", "tags": ["waiting-for/bob"]},
        {"text": "cased", "tags": ["Waiting-For"]},
        {"text": "forever", "tags": ["waiting-forever"]},
        {"text": "empty-segments", "tags": ["Waiting-For//Chase/"]},
    ])
    assert texts(await call(db, tags=["waiting-for"])) == ["bob", "cased", "empty-segments"]
    assert texts(await call(db, tags=["#Waiting-For//chase"])) == ["empty-segments"]
    assert texts(await call(db, exclude_tags=["waiting-for"])) == ["forever"]


async def test_tag_filters_judge_a_row_awaiting_the_backfill_by_its_tags(db, seed):
    """A NULL `tag_prefixes` row is neither dropped nor wrongly kept."""
    await seed.note("pending.md", [
        {"text": "old-nested", "tags": ["Waiting-For/x"]},
        {"text": "old-forever", "tags": ["waiting-forever"]},
        {"text": "old-untagged"},
    ], version=4, v5=False)
    out = await call(db, tags=["waiting-for"])
    assert texts(out) == ["old-nested"]
    assert rows_of(out)[0].startswith("- [ ] `pending.md`:1 — id pending — ")
    assert "task ids pending: 1 tasks" in out.splitlines()[0]
    assert texts(await call(db, exclude_tags=["waiting-for"])) == ["old-forever", "old-untagged"]


async def test_the_largest_buildable_prefix_case_agrees_with_the_stored_tags(db, seed):
    tags = [f"t{i:02d}" + "/b" * 62 for i in range(64)]
    assert all(len(t) == 127 for t in tags)
    assert len(task_line.tag_prefixes(tags)) == 4032
    await seed.note("big.md", [{"text": "big", "tags": tags}, {"text": "small", "tags": ["t05x"]}])
    assert texts(await call(db, tags=[tags[17]])) == ["big"]
    assert texts(await call(db, tags=["T05/b/b"])) == ["big"]
    assert texts(await call(db, exclude_tags=["t05"])) == ["small"]


async def test_cut_tags_are_counted_with_tags_and_exclude_tags(db, seed):
    await seed.note("c.md", [{"text": "cut", "tags": ["t01"], "tags_cut": True}, {"text": "ok"}])
    out = await call(db, tags=["t65"])
    assert rows_of(out) == [] and "tasks with cut tags: 1" in out.splitlines()[0]
    out = await call(db, exclude_tags=["zzz"])
    assert "tasks with cut tags: 1" in out.splitlines()[0]
    assert "tasks with cut tags" not in (await call(db)).splitlines()[0]


@pytest.mark.parametrize("stored,query_text", [
    ("Позвонить в БАНК", "банк позвонить"),
    ("ΣΊΣΥΦΟΣ rolls", "σίσυφος"),
    ("Visit Straße 5", "STRASSE"),
])
async def test_text_matches_case_insensitively_in_any_script(db, seed, stored, query_text):
    await seed.note("x.md", [{"text": stored}, {"text": "unrelated"}])
    out = await call(db, text=query_text)
    assert texts(out) == [stored]
    assert "tasks with cut text: 0" in out.splitlines()[0]
    assert "text: " in out.splitlines()[1]


async def test_text_counts_cut_rows(db, seed):
    await seed.note("x.md", [{"text": "a" * 1000, "text_cut": True}, {"text": "b"}])
    out = await call(db, text="needle")
    assert rows_of(out) == [] and "tasks with cut text: 1" in out.splitlines()[0]


async def test_priority_recurring_undated_has_due_done_and_path(db, seed):
    await seed.note("p/a.md", [
        {"text": "hi", "priority": "high", "due": D(2026, 11, 1)},
        {"text": "plain", "scheduled": D(2026, 11, 2)},
        {"text": "low", "priority": "low"},
        {"text": "rec", "recurrence": "every week", "due": D(2026, 11, 3)},
        {"text": "fin", "status_char": "x", "state": "done", "done_on": D(2026, 11, 5)},
    ])
    await seed.note("p/b.md", [{"text": "other"}])
    assert texts(await call(db, priority=["high", "none"])) == ["hi", "rec", "plain", "other"]
    assert texts(await call(db, recurring=True)) == ["rec"]
    assert "rec" not in texts(await call(db, recurring=False))
    assert texts(await call(db, undated=True)) == ["low", "other"]
    assert texts(await call(db, undated=False)) == ["hi", "rec", "plain"]
    assert texts(await call(db, has_due=False)) == ["plain", "low", "other"]
    assert texts(await call(db, status="done", done_from="2026-11-05", done_by="2026-11-05")) == ["fin"]
    assert texts(await call(db, status="done", done_from="2026-11-06")) == []
    assert texts(await call(db, path="p/b.md")) == ["other"]
    assert texts(await call(db, path="p/b.md", folder="p/")) == ["other"]
    assert texts(await call(db, path="p/b.md", folder="q/")) == []


@pytest.mark.parametrize("kw", [
    dict(status="pending"), dict(due_by="next week"), dict(undated=True, due_by="today"),
    dict(undated=False, scheduled_from="2026-01-01"), dict(undated=True, has_due=True),
    dict(order_by="title"), dict(tags=["t"] * 33), dict(tags=["a" * 129]), dict(tags=["#"]),
    dict(tags=["a/!"]), dict(due_from="2026-11-02", due_by="2026-11-01"),
    dict(due_by="+3661d"), dict(text=""), dict(text=" ".join("t" * 9)), dict(text="x" * 257),
    dict(priority=["urgent"]), dict(priority=[]), dict(detail="short"), dict(scope="mine"),
    dict(cursor="garbage"), dict(path=""), dict(text="a\x00b"),
])
async def test_invalid_arguments_are_refused_before_any_query(kw):
    def no_session():
        raise AssertionError("a session was opened for an invalid argument")

    out = await task_query.list_tasks_query(None, ListTasksArgs(**kw), DEFAULT, now=NOW,
                                            session_factory=no_session)
    assert refused(out)


async def test_32_tags_of_128_are_accepted(db, seed):
    out = await call(db, tags=["#" + "a" * 128] + ["b" * 128] * 31)
    assert isinstance(out, str) and "tasks with cut tags: 0" in out


# ── scope ───────────────────────────────────────────────────────────────────


async def test_scope_excludes_counts_and_all_lifts_it(db, seed):
    await seed.note("Work/Tickets/t.md", [{"text": f"ticket {i}"} for i in range(40)])
    await seed.note("Home/h.md", [{"text": "home"}])
    ts = settings_for(excluded_folders=("Work/Tickets/",))
    out = await call(db, ts=ts)
    assert texts(out) == ["home"]
    q = out.splitlines()[1]
    assert "scope: default" in q and "excluded by scope: 40 tasks" in q
    assert "excluded folders: 1 [Work/Tickets/]" in q and "excluded note tags: 0" in q
    out = await call(db, ts=ts, scope="all")
    assert len(rows_of(out)) == 41 and "excluded by scope: 0 tasks" in out
    # An explicitly named note inside an excluded folder: 0 rows, counted.
    out = await call(db, ts=ts, path="Work/Tickets/t.md")
    assert rows_of(out) == [] and "excluded by scope: 40 tasks" in out


async def test_scope_excludes_by_note_tag_hierarchically(db, seed):
    await seed.note("a.md", [{"text": "secret"}], note_tags=["Private/Journal"])
    await seed.note("b.md", [{"text": "lookalike"}], note_tags=["privateer"])
    await seed.note("c.md", [{"text": "plain"}])
    ts = settings_for(excluded_note_tags=("private",))
    out = await call(db, ts=ts)
    assert texts(out) == ["lookalike", "plain"]
    assert "excluded by scope: 1 tasks" in out and "excluded note tags: 1 [private]" in out


# ── paging ──────────────────────────────────────────────────────────────────


def _python_key(order_by):
    rank = task_query.PRIORITY_RANK
    inf = D.max

    def key(r):
        if order_by == "due":
            return (r["due"] or inf, r["path"], r["line"])
        if order_by == "scheduled":
            return (r["scheduled"] or inf, r["path"], r["line"])
        if order_by == "priority":
            return (rank[r["priority"] or "none"], r["due"] or inf, r["path"], r["line"])
        if order_by == "done":
            # DESC with NULL as -infinity (last); path and line ascending.
            return (-(r["done_on"] or D.min).toordinal(), r["path"], r["line"])
        return (r["path"], r["line"])
    return key


@pytest.mark.parametrize("order_by", task_query.ORDER_BYS)
async def test_paging_1234_tasks_exactly_once_in_order(db, seed, order_by):
    prios = [None, "highest", "high", "medium", "low", "lowest"]
    expected = []
    for n in range(20):
        tasks = []
        for i in range(62 if n < 14 else 61):
            t = {
                "text": f"t{n}-{i}",
                "due": None if i % 4 == 0 else D(2026, 1, 1) + datetime.timedelta(days=(i * 7 + n) % 50),
                "scheduled": None if i % 3 == 0 else D(2026, 2, 1) + datetime.timedelta(days=(i + n) % 9),
                "done_on": None if i % 5 == 0 else D(2026, 3, 1) + datetime.timedelta(days=(i * 3) % 7),
                "priority": prios[(i + n) % 6],
                "status_char": "x" if i % 2 else " ", "state": "done" if i % 2 else "open",
            }
            tasks.append(t)
            expected.append({**t, "path": f"P/n{n:02d}.md", "line": len(tasks)})
        await seed.note(f"P/n{n:02d}.md", tasks)
    assert len(expected) == 1234
    seen, pages = await follow(db, status="all", order_by=order_by, limit=97)
    assert len(seen) == 1234 == len(set(seen))
    order = sorted(expected, key=_python_key(order_by))
    assert [ln.rsplit("text: ", 1)[1] for ln in seen] == [r["text"] for r in order]
    tok = next_of(pages[0])
    assert decoded(tok)["v"] == 2 and decoded(tok)["ob"] == order_by


async def test_a_page_boundary_between_undated_tasks(db, seed):
    await seed.note("u.md", [{"text": "a"}, {"text": "b"}, {"text": "c"}])
    first = await call(db, limit=1)
    assert decoded(next_of(first))["k"] == ["infinity"]
    second = await call(db, limit=1, cursor=next_of(first))
    third = await call(db, limit=1, cursor=next_of(second))
    assert [texts(p)[0] for p in (first, second, third)] == ["a", "b", "c"]
    assert next_of(third) is None


async def test_undated_tasks_sort_last_under_due_and_done_393(db, seed):
    """#393. Undated rows sit where path and line would put them *first*
    (`a.md`, line 1), so only the date key can move them to the end: under
    the default `due` order an undated task follows every dated one, and
    under `done` (most recent first) a done task with no done date follows
    every dated one — on one page and across a page boundary alike."""
    await seed.note("a.md", [
        {"text": "undated-open"},
        {"text": "undated-done", "status_char": "x", "state": "done"},
    ])
    await seed.note("z.md", [
        {"text": "due-late", "due": D(2026, 12, 1)},
        {"text": "due-early", "due": D(2026, 1, 1)},
        {"text": "done-old", "status_char": "x", "state": "done",
         "done_on": D(2026, 1, 5), "due": D(2026, 6, 1)},
        {"text": "done-new", "status_char": "x", "state": "done",
         "done_on": D(2026, 11, 9), "due": D(2026, 3, 1)},
    ])

    by_due = ["due-early", "done-new", "done-old", "due-late",
              "undated-open", "undated-done"]
    assert texts(await call(db, status="all")) == by_due, "due is the default order"
    assert texts(await call(db, status="all", order_by="due")) == by_due
    seen, _ = await follow(db, status="all", order_by="due", limit=1)
    assert [ln.rsplit("text: ", 1)[1] for ln in seen] == by_due

    by_done = ["done-new", "done-old", "undated-done"]
    assert texts(await call(db, status="done", order_by="done")) == by_done
    seen, _ = await follow(db, status="done", order_by="done", limit=1)
    assert [ln.rsplit("text: ", 1)[1] for ln in seen] == by_done


async def test_the_compact_cursor_resolves_a_long_multibyte_path(db, seed):
    path = "語" * 1024
    await seed.note(path, [{"text": "one"}, {"text": "two"}])
    first = await call(db, limit=1)
    tok = next_of(first)
    assert len(tok) <= 2048 and "h" in decoded(tok)
    assert texts(await call(db, limit=1, cursor=tok)) == ["two"]


async def test_cursor_refusals(db, seed):
    await seed.note("r.md", [{"text": "a", "due": D(2026, 1, 1)}, {"text": "b"},
                             {"text": "c", "due": D(2026, 2, 1)}])
    tok = next_of(await call(db, limit=1))
    assert tok
    # Another owner, another status, another order.
    assert refused(await query(db, 7, limit=1, cursor=tok))
    assert refused(await query(db, limit=1, status="done", cursor=tok))
    assert refused(await query(db, limit=1, order_by="priority", cursor=tok))
    # A settings change between pages.
    assert refused(await query(db, ts=settings_for(excluded_folders=("X/",)), limit=1, cursor=tok))
    # A version-1 (#350) cursor.
    v1 = base64.urlsafe_b64encode(json.dumps(
        {"v": 1, "o": decoded(tok)["o"], "f": decoded(tok)["f"], "d": "2026-01-01",
         "p": "r.md", "l": 1}).encode()).decode().rstrip("=")
    assert refused(await query(db, limit=1, cursor=v1))
    # A relative query continued after midnight.
    rel = next_of(await call(db, limit=1, due_by="+400d"))
    tomorrow = NOW + datetime.timedelta(days=1)
    assert refused(await query(db, limit=1, due_by="+400d", now=tomorrow, cursor=rel))
    assert not isinstance(await query(db, limit=1, due_by="+400d", cursor=rel), QueryRefusal)


async def test_a_hashed_cursor_for_a_gone_note_is_refused(db, seed):
    path = "語" * 1024
    await seed.note(path, [{"text": "one"}, {"text": "two"}])
    tok = next_of(await call(db, limit=1))
    async with db() as session:
        await session.execute(text("DELETE FROM notes_metadata"))
        await session.commit()
    out = await query(db, limit=1, cursor=tok)
    assert refused(out) and "no longer indexed" in out.prose


# ── ids and tenancy ─────────────────────────────────────────────────────────


async def test_pending_ids_are_counted_and_a_refreshed_v4_note_shows_ids(db, seed):
    await seed.note("old.md", [{"text": "o1"}, {"text": "o2"}], version=4, v5=False)
    # A version-4 note a task write refreshed: keys populated, stamp still 4.
    await seed.note("refreshed.md", [{"text": "r1"}, {"text": "r1"}], version=4)
    out = await call(db)
    assert "task ids pending: 2 tasks" in out.splitlines()[0]
    assert all("— id pending —" in ln for ln in rows_of(out) if "`old.md`" in ln)
    key = task_line.task_key("r1")
    refreshed = [ln for ln in rows_of(out) if "`refreshed.md`" in ln]
    assert f"— id {key}.1of2 —" in refreshed[0] and f"— id {key}.2of2 —" in refreshed[1]


async def test_tenancy_with_same_path_notes(db, seed):
    ids = [await seed.note("Same.md", [{"text": "null-owned"}]),
           await seed.note("Same.md", [{"text": "seven"}], uid=7),
           await seed.note("Same.md", [{"text": "eight", "due": D(2026, 11, 1)}], uid=8)]
    assert texts(await call(db, 7)) == ["seven"]
    assert texts(await call(db, 8)) == ["eight"]
    assert texts(await call(db)) == ["null-owned"]
    assert texts(await call(db, 7, path="Same.md")) == ["seven"]
    # A cursor from user 7 is refused for user 8 and the ownerless caller.
    await seed.note("Second.md", [{"text": "seven-2"}], uid=7)
    tok = next_of(await call(db, 7, limit=1))
    assert refused(await query(db, 8, limit=1, cursor=tok))
    assert refused(await query(db, None, limit=1, cursor=tok))
    for out in (await call(db, 7), await call(db, 7, limit=1), await call(db, 8)):
        for note_id in ids:
            assert str(note_id) not in out
    assert all(str(i) not in json.dumps(decoded(tok)) for i in ids)


async def test_header_counts_and_incomplete_notice(db, seed):
    await seed.note("f.md", [{"text": "a"}], failed=True)
    await seed.note("old.md", [], version=3)
    out = await call(db)
    head = out.splitlines()[0]
    assert "not yet scanned for tasks: 1 notes" in head
    assert "tasks could not be indexed: 1 notes" in head
    assert out.splitlines()[-1].startswith("Incomplete list:")


async def test_the_query_runs_in_one_read_only_snapshot(db, seed):
    await seed.note("s.md", [{"text": "a"}])
    statements = []

    class Recording:
        def __init__(self):
            self.inner = db()

        async def __aenter__(self):
            self.session = await self.inner.__aenter__()
            return self

        async def __aexit__(self, *a):
            return await self.inner.__aexit__(*a)

        async def execute(self, stmt, *a, **k):
            statements.append(str(stmt))
            return await self.session.execute(stmt, *a, **k)

        async def rollback(self):
            await self.session.rollback()

    await task_query.list_tasks_query(None, ListTasksArgs(tags=["x"], text="a"), DEFAULT,
                                      now=NOW, session_factory=Recording)
    assert statements[0] == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
    assert all("notes_metadata.user_id IS NULL" in s for s in statements[1:])


# ── task_agenda ─────────────────────────────────────────────────────────────


def _agenda_rows(out):
    return [ln for ln in out.splitlines() if ln.startswith("- [")]


def _continuations(out):
    found = {}
    section = None
    for ln in out.splitlines():
        if ln.startswith("## "):
            section = ln
        elif ln.startswith("continue: list_tasks("):
            inner = ln[len("continue: list_tasks("):ln.index(") with this call's")]
            args = {}
            for m in re.finditer(r'(\w+)=("(?:[^"\\]|\\.)*"|true|false)', inner):
                args[m.group(1)] = json.loads(m.group(2))
            found[section] = args
    return found


async def test_agenda_sections_are_disjoint_and_cover_the_horizon(db, seed):
    d = D(2026, 11, 10)
    days = 7
    choices = {
        "none": None, "before": d - datetime.timedelta(days=3), "on": d,
        "within": d + datetime.timedelta(days=3), "after": d + datetime.timedelta(days=days + 4),
    }
    tasks, expected = [], set()
    for dn, due in choices.items():
        for sn, sched in choices.items():
            name = f"due-{dn}/sched-{sn}"
            tasks.append({"text": name, "due": due, "scheduled": sched})
            if (due is not None and due <= d + datetime.timedelta(days=days)) or (
                    sched is not None and sched <= d):
                expected.add(name)
    tasks.append({"text": "done-overdue", "due": d - datetime.timedelta(days=1),
                  "status_char": "x", "state": "done"})
    await seed.note("A.md", tasks)
    out = await agenda(db)
    shown = [ln.rsplit(" — ", 1)[1] for ln in _agenda_rows(out)]
    assert len(shown) == len(set(shown))
    assert set(shown) == expected
    assert "Agenda — timezone: UTC — date: today=2026-11-10 — days: 7" in out.splitlines()[0]


async def test_a_capped_section_continues_exactly(db, seed):
    d = D(2026, 11, 10)
    await seed.note("O.md", [{"text": f"over {i:02d}", "due": d - datetime.timedelta(days=1 + i % 9)}
                             for i in range(40)])
    out = await agenda(db)
    over = [ln for ln in out.splitlines() if ln.startswith("## Overdue")][0]
    assert "total: 40 tasks — showing 25" in over
    cont = _continuations(out)[over]
    assert cont["due_by"] == "2026-11-09" and cont["order_by"] == "due" and "cursor" in cont
    shown = [ln.rsplit(" — ", 1)[1] for ln in _agenda_rows(out)]
    rest = await call(db, **cont)
    assert isinstance(rest, str), rest
    remaining = texts(rest)
    assert len(remaining) == 15 and not set(remaining) & set(shown)
    assert set(remaining) | set(shown) == {f"over {i:02d}" for i in range(40)}


async def test_a_section_with_no_room_continues_without_a_cursor(db, seed):
    d = D(2026, 11, 10)
    await seed.note("Long/" + "p" * 300 + ".md",
                    [{"text": f"over {i:02d} " + "z" * 200, "due": d - datetime.timedelta(days=1)}
                     for i in range(25)]
                    + [{"text": f"today {i}", "due": d} for i in range(5)])
    out = await agenda(db, budget=2800)
    assert isinstance(out, str) and len(out) <= 2800
    today = [ln for ln in out.splitlines() if ln.startswith("## Due today")][0]
    assert "total: 5 tasks — not shown (response cap)" in today
    cont = _continuations(out)[today]
    assert "cursor" not in cont and cont == {"due_from": "2026-11-10", "due_by": "2026-11-10"}
    assert sorted(texts(await call(db, **cont))) == [f"today {i}" for i in range(5)]
    over = [ln for ln in out.splitlines() if ln.startswith("## Overdue")][0]
    cont = _continuations(out)[over]
    if "showing" in over:
        assert "cursor" in cont
    else:
        assert "cursor" not in cont


async def test_every_section_continuation_is_accepted_by_list_tasks(db, seed):
    d = D(2026, 11, 10)
    tasks = []
    for i in range(60):
        tasks.append({"text": f"up {i:02d}", "due": d + datetime.timedelta(days=1 + i % 7)})
        tasks.append({"text": f"su {i:02d}", "scheduled": d - datetime.timedelta(days=i % 3)})
        tasks.append({"text": f"sl {i:02d}", "scheduled": d, "due": d + datetime.timedelta(days=30)})
        tasks.append({"text": f"to {i:02d}", "due": d})
    await seed.note("M.md", tasks, note_tags=None)
    out = await agenda(db, tags=None)
    conts = _continuations(out)
    assert len(conts) == 4  # every section but overdue (empty) is capped
    for header, cont in conts.items():
        shown_in_section = []
        capture = False
        for ln in out.splitlines():
            if ln.startswith("## "):
                capture = ln == header
            elif capture and ln.startswith("- ["):
                shown_in_section.append(ln.rsplit(" — ", 1)[1])
        total = int(re.search(r"total: (\d+) tasks", header).group(1))
        seen, _ = await follow(db, **cont)
        remaining = [ln.rsplit("text: ", 1)[1] for ln in seen]
        assert len(shown_in_section) + len(remaining) == total, header
        assert not set(remaining) & set(shown_in_section)


async def test_agenda_respects_the_caller_and_the_scope(db, seed):
    d = D(2026, 11, 10)
    await seed.note("Archive/a.md", [{"text": "archived", "due": d}], uid=7)
    await seed.note("Live/a.md", [{"text": "mine", "due": d}], uid=7)
    await seed.note("Live/a.md", [{"text": "theirs", "due": d - datetime.timedelta(days=2)}], uid=8)
    ts = settings_for(excluded_folders=("Archive/",))
    out = await agenda(db, 7, ts=ts)
    shown = [ln.rsplit(" — ", 1)[1] for ln in _agenda_rows(out)]
    assert shown == ["mine"]
    assert "excluded by scope: 1 tasks" in out.splitlines()[0]
    out = await agenda(db, 7, ts=ts, scope="all")
    assert sorted(ln.rsplit(" — ", 1)[1] for ln in _agenda_rows(out)) == ["archived", "mine"]


async def test_agenda_refuses_invalid_arguments(db):
    for kw in (dict(date="someday"), dict(days=0), dict(days=32), dict(days=True),
               dict(tags=["#"]), dict(scope="x")):
        assert refused(await task_query.task_agenda_query(None, AgendaArgs(**kw), DEFAULT,
                                                          now=NOW, session_factory=db))


# ── audit round 1: rows built by the real extractor ─────────────────────────

V5_ONLY = ("task_key", "key_ordinal", "key_count", "description_key", "text_fold",
           "tag_prefixes", "recurrence", "created", "cancelled", "context")


async def real_note(maker, path, body, *, uid=None, pending=False, note_tags=None):
    """A note's rows through `tasks.tasks_from_note_text` / `note_task_row`.
    `pending=True` stamps the note 4 and blanks every version-5 column, as a
    row awaiting the backfill holds them."""
    from sqlalchemy import insert
    from src.models.db import NoteTask
    from src.services import tasks as task_extract

    extracted, _capped = task_extract.tasks_from_note_text(body)
    async with maker() as session:
        note_id = (await session.execute(text(
            "INSERT INTO notes_metadata (user_id, file_path, title, content_hash, "
            " extraction_version, tags) VALUES (:u, :p, 't', 'h', :v, :tg) RETURNING id"
        ), {"u": uid, "p": path, "v": 4 if pending else 5, "tg": note_tags})).scalar_one()
        rows = [task_extract.note_task_row(note_id, t) for t in extracted]
        if pending:
            rows = [{**r, **{c: None for c in V5_ONLY}} for r in rows]
        if rows:
            await session.execute(insert(NoteTask), rows)
        await session.commit()
    return len(rows)


NOTICE = "approximate until the next index pass."


async def test_pending_rows_raise_the_notice_for_text(db):
    await real_note(db, "old.md", "- [ ] Visit Straße 5\n- [ ] Café run\n", pending=True)
    await real_note(db, "new.md", "- [ ] Strasse sweep\n")
    out = await call(db, text="strasse")
    assert "Strasse sweep" in texts(out)
    assert out.splitlines()[-1] == f"Incomplete list: text filter {NOTICE}"
    assert "task ids pending: 2 tasks" in out.splitlines()[0]
    # No pending row among the matches of the other filters: no notice.
    clean = await call(db, text="strasse", path="new.md")
    assert NOTICE not in clean
    # A filter exact on pending rows (tags, dates) does not raise it.
    assert NOTICE not in await call(db, due_by="2030-01-01")


async def test_recurring_judges_pending_rows_by_their_text(db):
    body = "- [ ] Pay rent 🔁 every month 📅 2026-12-01\n- [ ] One-off 📅 2026-12-02\n"
    await real_note(db, "old.md", body, pending=True)
    await real_note(db, "new.md", body)
    rec = await call(db, recurring=True)
    assert sorted(texts(rec)) == ["Pay rent 🔁 every month 📅 2026-12-01"] * 2
    assert rec.splitlines()[-1] == f"Incomplete list: recurring filter {NOTICE}"
    once = await call(db, recurring=False)
    assert sorted(texts(once)) == ["One-off 📅 2026-12-02"] * 2
    assert NOTICE in once
    both = await call(db, recurring=True, text="rent")
    assert both.splitlines()[-1] == f"Incomplete list: text and recurring filters {NOTICE}"


async def test_trailing_slash_tag_values_are_normalised(db):
    await real_note(db, "w.md", "- [ ] a #Work/Alpha\n- [ ] b #workshop\n- [ ] c\n")
    assert texts(await call(db, tags=["work/"])) == ["a #Work/Alpha"]
    assert texts(await call(db, exclude_tags=["#WORK//"])) == ["b #workshop", "c"]
    assert isinstance(await agenda(db, tags=["work/"], exclude_tags=["x/"]), str)


@pytest.mark.parametrize("stored", ["#private", "private", "#Private/Journal", "PRIVATE"])
async def test_scope_strips_a_leading_hash_from_note_tags(db, stored):
    await real_note(db, "a.md", "- [ ] secret\n", note_tags=[stored])
    await real_note(db, "b.md", "- [ ] lookalike\n", note_tags=["#privateer"])
    out = await call(db, ts=settings_for(excluded_note_tags=("private",)))
    assert texts(out) == ["lookalike"]
    assert "excluded by scope: 1 tasks" in out


async def test_real_extractor_rows_render_ids_and_page(db):
    body = "".join(f"- [ ] item {i} 📅 2026-11-{1 + i % 28:02d}\n" for i in range(60))
    assert await real_note(db, "r.md", body) == 60
    seen, pages = await follow(db, limit=7, order_by="due")
    assert len(seen) == 60 == len(set(seen)) and len(pages) == 9
    assert all(re.search(r"`:\d+ — id [0-9a-f]{12} — due ", ln) for ln in seen)
