"""The task rule against a real PostgreSQL (#434, design D1–D9).

What needs the database: the rule's SQL (`task_rule.rule_clause` composed into
`task_query.scope_clause`) for every rule × every consumer — the `list_tasks`
page, `excluded by scope`, `excluded by rule`, `kept though unsure`, `task ids
pending`, the `task_agenda` totals and rows, `add_task`'s exact and similar
duplicate scans, and `scope="all"` — the D4 identity, rows awaiting the
version-5 backfill, two owners with two rules, cursors across rule changes,
the defensive fallback, `save`'s serialised first saves and its CHECK
handling, and SQL ≡ Python (`extracted_is_task` against `rule_clause`).

**The rows come from the real extractor** (`tasks.tasks_from_note_text`), so
every column — `created`, `cancelled`, `recurrence`, `tag_prefixes`, a cut
text, a cut tag list, an invalid date stored NULL — is exactly what the index
stores. A "pending" row is the same row with the version-5 columns NULL.

**The oracle is independent**: `_r_py` evaluates the rule over the fetched
rows in Python, written from the spec text, not from `task_rule`. Every count
the response states is checked against it.

Skipped unless `PGVECTOR_TEST_ADMIN_URL` is set (see `_harness`).
"""
from __future__ import annotations

import asyncio
import dataclasses
import datetime
import re

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.mcp_server import task_render
from src.services import task_line, task_query, task_rule, task_settings, task_write
from src.services.task_query import AgendaArgs, ListTasksArgs, QueryRefusal
from src.services.tasks import tasks_from_note_text
import _harness

pytestmark = [
    _harness.requires_pgvector,
    pytest.mark.asyncio(loop_scope="module"),
]

DIM = 8
NOW = datetime.datetime(2026, 11, 10, 12, 0, tzinfo=datetime.timezone.utc)
BIG = 40_000

#: The awkward fixture (design test plan): dated, priority-only,
#: recurrence-only, created-only, cancelled-only, done with and without ✅,
#: task tags nested and differently cased, a look-alike tag, plain
#: checklists, `#task` / `#Task` lines, cut text with and without the filter
#: in the first 1,000 characters, a 65-tag line, an invalid date, unindexed
#: fields only, overdue and upcoming dates for the agenda.
LINES = [
    "- [ ] Pay rent 📅 2026-11-30",
    "- [ ] Chase reply #waiting-for/bob",
    "- [ ] Chase again #Waiting-For",
    "- [ ] Lookalike #waiting-forever",
    "- [ ] Step one",
    "- [ ] Buy milk",
    "- [ ] Scheduled only ⏳ 2026-12-01",
    "- [ ] Start only 🛫 2026-12-01",
    "- [ ] Priority only 🔼",
    "- [ ] Water plants 🔁 every week",
    "- [ ] Created only ➕ 2026-11-01",
    "- [ ] Invalid date 📅 2026-02-30",
    "- [ ] Only an id 🆔 abc123",
    "- [x] Done with date ✅ 2026-11-09",
    "- [x] Done without date",
    "- [-] Cancelled with date ❌ 2026-11-09",
    "- [-] Cancelled without date",
    "- [ ] #task Book flights",
    "- [ ] Book hotel #task",
    "- [ ] #Task Rent car",
    "- [ ] Overdue plain #task 📅 2026-11-01",
    "- [ ] Overdue checklist 📅 2026-11-02",
    "- [ ] " + "x" * 1100 + " #task",
    "- [ ] #task " + "y" * 1100,
    "- [ ] " + "z" * 1100,
    "- [ ] many " + " ".join(f"#t{i:02d}" for i in range(65)),
    "- [ ] Renew passport",
]
BODY = "# Tasks\n" + "\n".join(LINES) + "\n"


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def migrated_url():
    yield from _harness.throwaway_database("task_rule_434", DIM)


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
        for uid in (7, 8):
            await session.execute(text(
                "INSERT INTO users (id, username, password_hash) VALUES (:i, :u, 'x')"
            ), {"i": uid, "u": f"user{uid}"})
        await session.commit()
    return maker


_V5 = ("task_key", "key_ordinal", "key_count", "description_key", "text_fold",
       "tag_prefixes", "recurrence", "created", "cancelled")


async def seed(maker, path, body=BODY, *, uid=None, pending=False, note_tags=None):
    """Index `body` at `path` with the real extractor's rows (version-5
    columns NULL when `pending`)."""
    tasks, _ = tasks_from_note_text(body)
    async with maker() as session:
        note_id = (await session.execute(text(
            "INSERT INTO notes_metadata (user_id, file_path, title, content_hash, "
            " extraction_version, tags) VALUES (:u, :p, 't', 'h', 5, :tg) RETURNING id"
        ), {"u": uid, "p": path, "tg": note_tags})).scalar_one()
        for t in tasks:
            row = dataclasses.asdict(t)
            if pending:
                for c in _V5:
                    row[c] = None
            row["n"] = note_id
            await session.execute(text(
                "INSERT INTO note_tasks (note_id, line, status_char, state, text, text_cut, "
                " due, scheduled, start, done_on, priority, tags, tags_cut, heading_path, "
                " task_key, key_ordinal, key_count, description_key, text_fold, tag_prefixes, "
                " recurrence, recurrence_cut, created, cancelled, context, context_cut) "
                "VALUES (:n, :line, :status_char, :state, :text, :text_cut, :due, :scheduled, "
                " :start, :done_on, :priority, :tags, :tags_cut, :heading_path, :task_key, "
                " :key_ordinal, :key_count, :description_key, :text_fold, :tag_prefixes, "
                " :recurrence, :recurrence_cut, :created, :cancelled, :context, :context_cut)"
            ), row)
        await session.commit()
    return tasks


def eff(rule="all", tags=(), filt=None, **kw):
    return dataclasses.replace(task_settings.defaults(), task_rule=rule, task_tags=tuple(tags),
                               global_filter=filt, **kw)


async def page(maker, uid=None, *, ts, **kw):
    kw.setdefault("limit", 500)
    got = await task_query.list_tasks_query(uid, ListTasksArgs(**kw), ts, now=NOW,
                                            session_factory=maker)
    assert not isinstance(got, QueryRefusal), got
    return got


async def render(maker, uid=None, *, ts, budget=BIG, **kw):
    return task_render.render_list_tasks(await page(maker, uid, ts=ts, **kw), budget=budget)


async def agenda(maker, uid=None, *, ts, **kw):
    got = await task_query.task_agenda_query(uid, AgendaArgs(**kw), ts, now=NOW,
                                             session_factory=maker)
    assert not isinstance(got, QueryRefusal), got
    return got


def keyset(rows):
    return {(r["file_path"], r["line"]) for r in rows}


async def all_rows(maker, uid=None):
    """Every row of the owner with the columns the oracle reads."""
    owner = "m.user_id IS NULL" if uid is None else "m.user_id = :u"
    async with maker() as session:
        result = await session.execute(text(
            "SELECT m.file_path, m.tags AS note_tags, t.* FROM note_tasks t "
            f"JOIN notes_metadata m ON m.id = t.note_id WHERE {owner}"), {"u": uid})
        return [dict(r._mapping) for r in result]


# ── the independent oracle (from the spec text) ──────────────────────────────


def _norm(tag):
    return re.sub("/{2,}", "/", tag.lower()).rstrip("/")


def _carries(row, tags):
    """`list_tasks(tags=…)`-style: hierarchical by `/`, case-insensitive,
    over `tag_prefixes`, or over the stored `tags` on a pending row."""
    if row["tag_prefixes"] is not None:
        return bool(set(row["tag_prefixes"]) & set(tags))
    for stored in row["tags"] or ():
        n = _norm(stored)
        if any(n == t or n.startswith(t + "/") for t in tags):
            return True
    return False


def _r_py(row, kind, tags=(), filt=None):
    tags = [_norm(t) for t in tags]
    if kind == "fields":
        fields = any(row[c] is not None for c in (
            "due", "scheduled", "start", "done_on", "priority", "recurrence", "created",
            "cancelled"))
        if row["task_key"] is None:
            fields = fields or any(s in row["text"] for s in ("🔁", "➕", "❌"))
        return fields or (bool(tags) and (_carries(row, tags) or row["tags_cut"]))
    if kind == "filter":
        return filt in row["text"] or row["text_cut"]
    return True


def _unsure_py(row, kind, tags=(), filt=None):
    tags = [_norm(t) for t in tags]
    if kind == "fields":
        return bool(tags) and row["tags_cut"] and not _r_py(row, "fields") \
            and not _carries(row, tags)
    if kind == "filter":
        return row["text_cut"] and filt not in row["text"]
    return False


def _location_py(row, folders=(), note_tags=()):
    if any(row["file_path"].startswith(f) for f in folders):
        return False
    stored = [_norm(t.lstrip("#")) for t in (row["note_tags"] or ())]
    return not any(s == n or s.startswith(n + "/") for s in stored for n in note_tags)


RULES = [
    ("all", (), None),
    ("fields", (), None),
    ("fields", ("waiting-for",), None),
    ("fields", ("Waiting-For", "zzz"), None),
    ("filter", (), "#task"),
    ("filter", (), "Book"),
    ("filter", (), "x" * 64),
]


# ── every rule × the listing and its counts, with the D4 identity ───────────


@pytest.mark.parametrize("kind,tags,filt", RULES)
@pytest.mark.parametrize("status", ["open", "all"])
async def test_each_rule_listing_counts_and_identity(db, kind, tags, filt, status):
    await seed(db, "Projects/Alpha.md")
    await seed(db, "Archive/Old.md")
    await seed(db, "Notes/Private.md", note_tags=["#Private/x"])
    await seed(db, "Pending/Backfill.md", pending=True)
    ts = eff(kind, tags, filt, excluded_folders=("Archive/",), excluded_note_tags=("private",))
    resolved = task_rule.resolve(kind, tags, filt)
    ts = dataclasses.replace(ts, task_rule=resolved[0], task_tags=resolved[1])
    p = await page(db, ts=ts, status=status)
    everything = await page(db, ts=ts, status=status, scope="all")
    universe = [r for r in await all_rows(db) if status == "all" or r["state"] == "open"]
    assert keyset(everything.rows) == keyset(universe)

    loc = [r for r in universe if _location_py(r, ("Archive/",), ("private",))]
    kept = [r for r in loc if _r_py(r, kind, tags, filt)]
    dropped = [r for r in loc if not _r_py(r, kind, tags, filt)]
    assert keyset(p.rows) == keyset(kept)
    assert p.exclusions.excluded == len(universe) - len(loc)
    # D4: the unscoped rows are the default rows + excluded by scope + by rule.
    assert len(everything.rows) == len(p.rows) + p.exclusions.excluded + len(dropped)
    if kind == "all":
        assert p.rule is None
        return
    assert p.rule is not None and p.rule.excluded == len(dropped)
    assert p.rule.unsure == sum(_unsure_py(r, kind, tags, filt) for r in kept)
    pending = [r for r in (loc if kind == "fields" else kept) if r["task_key"] is None]
    assert p.ids_pending == len(pending)
    assert p.rule.pending == (len(pending) if kind == "fields" else 0)
    out = task_render.render_list_tasks(p, budget=BIG)
    rline = out.splitlines()[2]
    assert rline.startswith(f"Rule — task rule: {kind}")
    assert f"excluded by rule: {len(dropped)} tasks" in rline
    assert rline.endswith('pass scope="all" to list every checkbox')
    if kind == "fields" and pending:
        assert f"rule approximate for {len(pending)} tasks until the next index pass" in rline
    # scope="all" carries no rule line and its query line says scope: all.
    unscoped = task_render.render_list_tasks(everything, budget=BIG)
    assert "Rule —" not in unscoped and "scope: all" in unscoped.splitlines()[1]


async def test_the_spec_scenarios(db):
    note = ("- [ ] Pay rent 📅 2026-11-30\n- [ ] Chase reply #waiting-for/bob\n"
            "- [ ] Step one\n- [ ] Buy milk\n")
    await seed(db, "a.md", note)
    out = await render(db, ts=eff("fields", ["waiting-for"]))
    assert [ln.rsplit("text: ", 1)[1] for ln in out.splitlines() if ln.startswith("- [")] == [
        "Pay rent 📅 2026-11-30", "Chase reply #waiting-for/bob"]
    assert "excluded by rule: 2 tasks" in out.splitlines()[2]
    assert len((await page(db, ts=eff("fields", ["waiting-for"]), scope="all")).rows) == 4

    await seed(db, "b.md", "- [ ] #task Book flights\n- [ ] Book hotel #task\n"
                           "- [ ] #Task Rent car\n- [ ] Pack bag\n")
    out = await render(db, ts=eff("filter", filt="#task"), path="b.md")
    lines = out.splitlines()
    assert lines[2].startswith('Rule — task rule: filter "#task"')
    assert "excluded by rule: 2 tasks" in lines[2]
    assert len([ln for ln in lines if ln.startswith("- [")]) == 2

    await seed(db, "c.md", "- [ ] " + "x" * 1200 + " #task\n")
    out = await render(db, ts=eff("filter", filt="#task"), path="c.md")
    assert "kept though unsure (text cut): 1 tasks" in out.splitlines()[2]
    assert len([ln for ln in out.splitlines() if ln.startswith("- [")]) == 1


async def test_every_field_family_counts_an_invalid_date_does_not(db):
    await seed(db, "f.md", "- [ ] a ⏳ 2026-12-01\n- [ ] b 🛫 2026-12-01\n- [ ] c 🔼\n"
                           "- [ ] d 🔁 every week\n- [ ] e ➕ 2026-11-01\n"
                           "- [ ] f 📅 2026-02-30\n- [ ] g\n")
    p = await page(db, ts=eff("fields"))
    assert sorted(r["text"][0] for r in p.rows) == list("abcde")
    assert p.rule.excluded == 2


async def test_backfill_rows_are_judged_without_silent_loss(db):
    await seed(db, "p.md", "- [ ] Water plants 🔁 every week\n- [ ] Note ➕ 2026-11-01\n"
                           "- [ ] Plain\n- [ ] #task here\n", pending=True)
    p = await page(db, ts=eff("fields"))
    assert sorted(r["text"] for r in p.rows) == ["Note ➕ 2026-11-01",
                                                 "Water plants 🔁 every week"]
    assert p.ids_pending == 4  # ignores the rule
    rline = task_render.render_list_tasks(p, budget=BIG).splitlines()[2]
    assert "rule approximate for 4 tasks until the next index pass" in rline
    # filter is exact on pending rows, and its pending count includes the rule.
    q = await page(db, ts=eff("filter", filt="#task"))
    assert [r["text"] for r in q.rows] == ["#task here"] and q.ids_pending == 1
    assert "rule approximate" not in task_render.render_list_tasks(q, budget=BIG)


# ── the agenda ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("kind,tags,filt", RULES)
async def test_the_agenda_applies_the_rule(db, kind, tags, filt):
    await seed(db, "Projects/Alpha.md")
    await seed(db, "Pending/Backfill.md", pending=True)
    ts = eff(kind, tags, filt)
    resolved = task_rule.resolve(kind, tags, filt)
    ts = dataclasses.replace(ts, task_rule=resolved[0], task_tags=resolved[1])
    a = await agenda(db, ts=ts)
    unscoped = await agenda(db, ts=ts, scope="all")
    in_agenda = set()
    for s in unscoped.sections:
        in_agenda |= keyset(s.rows)
    universe = [r for r in await all_rows(db) if (r["file_path"], r["line"]) in in_agenda]
    kept = [r for r in universe if _r_py(r, kind, tags, filt)]
    shown = set()
    for s in a.sections:
        shown |= keyset(s.rows)
    assert shown == keyset(kept)
    assert sum(s.total for s in a.sections) == len(kept)
    if kind == "all":
        assert a.rule is None
        return
    assert a.rule.excluded == len(universe) - len(kept)
    out = task_render.render_task_agenda(a, budget=BIG)
    assert out.splitlines()[1].startswith(f"Rule — task rule: {kind}")
    if kind == "fields":
        assert a.rule.excluded == 0  # every agenda row is dated


async def test_the_agenda_scenario(db):
    await seed(db, "o.md", "- [ ] Overdue #task 📅 2026-11-01\n- [ ] Overdue plain 📅 2026-11-02\n")
    a = await agenda(db, ts=eff("filter", filt="#task"))
    out = task_render.render_task_agenda(a, budget=BIG)
    assert "excluded by rule: 1 tasks" in out.splitlines()[1]
    rows = [ln for ln in out.splitlines() if ln.startswith("- [")]
    assert len(rows) == 1 and "`o.md`:1 —" in rows[0]


# ── duplicate scan ───────────────────────────────────────────────────────────


async def test_the_duplicate_scan_considers_only_tasks_by_the_rule(db):
    await seed(db, "Projects/Alpha.md", "- [ ] Renew passport\n- [ ] Renew passport soon\n")
    norm = task_line.normalise_description("Renew passport")
    terms = task_write.similarity_terms(norm)
    async with db() as session:
        exact, similar, _ = await task_write.duplicate_scan(
            session, None, eff("all"), norm, terms, exclude_path="Inbox.md")
        assert [r.path for r in exact] == ["Projects/Alpha.md"] and similar
        exact, similar, _ = await task_write.duplicate_scan(
            session, None, eff("fields"), norm, terms, exclude_path="Inbox.md")
        assert exact == [] and similar == []
        exact, _s, _ = await task_write.duplicate_scan(
            session, None, eff("filter", filt="Renew"), norm, terms, exclude_path="Inbox.md")
        assert len(exact) == 1


# ── tenancy ──────────────────────────────────────────────────────────────────


async def test_two_owners_two_rules_same_paths(db):
    for uid in (None, 7, 8):
        await seed(db, "Projects/Alpha.md", uid=uid)
    async with db() as session:
        await session.execute(text(
            "INSERT INTO task_settings (user_id, task_rule, global_filter) "
            "VALUES (7, 'filter', '#task')"))
        await session.execute(text(
            "INSERT INTO task_settings (user_id, task_rule) VALUES (8, 'fields')"))
        await session.commit()
        e7 = await task_settings.effective(session, 7)
        e8 = await task_settings.effective(session, 8)
        e0 = await task_settings.effective(session, None)
    assert (e7.task_rule, e8.task_rule, e0.task_rule) == ("filter", "fields", "all")
    universe = await all_rows(db, 7)
    for uid, e, kind, filt in ((7, e7, "filter", "#task"), (8, e8, "fields", None),
                               (None, e0, "all", None)):
        p = await page(db, uid, ts=e, status="all")
        expected = [r for r in universe if _r_py(r, kind, (), filt)]
        assert {r["line"] for r in p.rows} == {r["line"] for r in expected}
        assert len(p.rows) == len(expected)
        owner_ids = {r["note_id"] for r in await all_rows(db, uid)}
        assert owner_ids  # the rows really are this owner's
        a = await agenda(db, uid, ts=e)
        if kind != "all":
            assert a.rule.kind == kind


# ── cursors ──────────────────────────────────────────────────────────────────


async def test_a_rule_change_refuses_an_old_cursor_and_all_keeps_old_cursors(db):
    await seed(db, "Projects/Alpha.md")
    fields = eff("fields", ["waiting-for"])
    p = await page(db, ts=fields, limit=1)
    token = task_query.encode_cursor(p.cursor, p.rows[-1], compact=False)

    async def with_cursor(ts):
        return await task_query.list_tasks_query(
            None, ListTasksArgs(limit=1, cursor=token), ts, now=NOW, session_factory=db)

    assert not isinstance(await with_cursor(fields), QueryRefusal)
    for changed in (eff("fields", ["other"]), eff("filter", ["waiting-for"], "#task"),
                    eff("all")):
        refused = await with_cursor(changed)
        assert isinstance(refused, QueryRefusal) and refused.code == "invalid_argument"
        assert refused.prose == task_query.CURSOR_FOREIGN
    # An unused filter text under `fields` does not change the rows: accepted.
    assert not isinstance(await with_cursor(eff("fields", ["waiting-for"], "#x")), QueryRefusal)
    # A cursor minted by v1.3.0 itself stays valid, and continues after its
    # row, under no rule and under a stored `all`. The literal was produced by
    # loading `git show origin/main:src/services/task_query.py` (58564cd) into
    # a temporary module and calling its `encode_cursor` for owner None,
    # `ListTasksArgs()`, NOW and the row `Projects/Alpha.md:3` due 2026-11-30
    # (the same literal is `V130_CURSOR` in `tests/test_task_rule.py`).
    v130_cursor = (
        "eyJ2IjoyLCJvIjoiYzU3MTc4Nzg4ZmEwMTE0MCIsImYiOiI1NjJlYjkxODczZWU5ZjQzIiwib2IiOiJkdWUi"
        "LCJrIjpbIjIwMjYtMTEtMzAiXSwicCI6IlByb2plY3RzL0FscGhhLm1kIiwibCI6M30")
    V130_CURSOR = v130_cursor
    for ts in (task_settings.defaults(), eff("all", ["x"], "#task")):
        got = await task_query.list_tasks_query(
            None, ListTasksArgs(limit=500, cursor=V130_CURSOR), ts, now=NOW,
            session_factory=db)
        assert not isinstance(got, QueryRefusal), got
        # Strictly after (2026-11-30, Projects/Alpha.md, line 3) in `due` order.
        assert all(r["due"] is None or (r["due"], r["file_path"], r["line"])
                   > (datetime.date(2026, 11, 30), "Projects/Alpha.md", 3) for r in got.rows)
        assert got.rows


# ── byte identity under `all` ────────────────────────────────────────────────


CALLS = [dict(), dict(status="all"), dict(order_by="priority"), dict(detail="brief"),
         dict(tags=["task"]), dict(text="Book"), dict(limit=3), dict(scope="all"),
         dict(due_by="+30d")]


@pytest.mark.parametrize("kw", CALLS)
async def test_the_default_rule_changes_nothing(db, kw):
    """Under no row and a stored `all` the rendered output is identical. The
    previous release itself is pinned by literals captured from `origin/main`
    (the default-scope SQL, the filters digest and a cursor:
    `tests/test_task_rule.py::test_scope_sql_and_digest_under_all_equal_v130`);
    the rendering is pinned by the pre-existing render and integration tests,
    which pass unmodified."""
    await seed(db, "Projects/Alpha.md")
    await seed(db, "Pending/Backfill.md", pending=True)
    base = task_settings.defaults()
    expected = await render(db, ts=base, **kw)
    assert "Rule —" not in expected
    # A stored rule `all` with task tags and filter text filled in, and an
    # inherited `all`: byte-identical, every time.
    async with db() as session:
        await session.execute(text(
            "INSERT INTO task_settings (user_id, task_rule, task_tags, global_filter) "
            "VALUES (NULL, 'all', '{waiting-for}', '#task')"))
        await session.commit()
        stored_all = await task_settings.effective(session, None)
    assert stored_all.task_rule == "all" and not stored_all.task_rule_inherited
    assert await render(db, ts=stored_all, **kw) == expected
    a0 = task_render.render_task_agenda(await agenda(db, ts=base), budget=BIG)
    assert task_render.render_task_agenda(await agenda(db, ts=stored_all), budget=BIG) == a0
    assert "Rule —" not in a0


# ── SQL ≡ Python ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("kind,tags,filt", RULES)
async def test_extracted_is_task_agrees_with_rule_clause(db, kind, tags, filt):
    tasks = await seed(db, "Projects/Alpha.md")
    ts = eff(kind, tags, filt)
    resolved = task_rule.resolve(kind, tags, filt)
    ts = dataclasses.replace(ts, task_rule=resolved[0], task_tags=resolved[1])
    p = await page(db, ts=ts, status="all")
    listed = {r["line"] for r in p.rows}
    for t in tasks:
        assert task_rule.extracted_is_task(t, ts) == (t.line in listed), (kind, t.text[:40])


# ── settings: the fallback, serialised first saves, the CHECK ───────────────


async def test_an_unevaluable_stored_rule_lists_every_checkbox_and_says_so(db, migrated_url):
    await seed(db, "Projects/Alpha.md")
    async with db() as session:
        await session.execute(text(
            "ALTER TABLE task_settings DROP CONSTRAINT ck_task_settings_global_filter"))
        await session.execute(text(
            "INSERT INTO task_settings (user_id, task_rule, global_filter) "
            "VALUES (NULL, 'filter', NULL)"))
        await session.commit()
    try:
        async with db() as session:
            e = await task_settings.effective(session, None)
        assert (e.task_rule, e.task_rule_unusable) == ("all", "filter rule without filter text")
        p = await page(db, ts=e)
        assert len(p.rows) == len([r for r in await all_rows(db) if r["state"] == "open"])
        line = task_render.render_list_tasks(p, budget=BIG).splitlines()[2]
        assert line == ("Rule — task rule: all (the configured rule cannot be applied: filter "
                        'rule without filter text) — pass scope="all" to list every checkbox')
        a = task_render.render_task_agenda(await agenda(db, ts=e), budget=BIG)
        assert a.splitlines()[1] == line
        # A disallowed stored text (written past the save validation).
        async with db() as session:
            await session.execute(text("UPDATE task_settings SET global_filter = '%%'"))
            await session.commit()
            e = await task_settings.effective(session, None)
        assert (e.task_rule, e.task_rule_unusable) == ("all", "filter text not allowed")
    finally:
        async with db() as session:
            await session.execute(text("DELETE FROM task_settings"))
            await session.execute(text(
                "ALTER TABLE task_settings ADD CONSTRAINT ck_task_settings_global_filter "
                "CHECK ((global_filter IS NULL OR btrim(global_filter) <> '') AND "
                "(task_rule IS DISTINCT FROM 'filter' OR global_filter IS NOT NULL))"))
            await session.execute(text(
                "COMMENT ON CONSTRAINT ck_task_settings_global_filter ON task_settings IS "
                "'a filter rule has its filter text (040_task_rule)'"))
            await session.commit()


async def _save(maker, uid, form):
    async with maker() as session:
        errors = await task_settings.save(session, uid, form)
        if errors:
            await session.rollback()
        else:
            await session.commit()
        return errors


async def _stored(maker, uid):
    async with maker() as session:
        row = await task_settings.stored(session, uid)
        return None if row is None else (row.task_rule, list(row.task_tags), row.global_filter)


async def test_save_semantics(db):
    assert await _save(db, 7, {"task_rule": "filter", "global_filter": " #task "}) == []
    assert await _stored(db, 7) == ("filter", [], "#task")
    # An absent key keeps its stored value.
    assert await _save(db, 7, {"timezone": "UTC"}) == []
    assert await _stored(db, 7) == ("filter", [], "#task")
    # The merged check: blanking the filter while the stored rule is filter.
    assert await _save(db, 7, {"global_filter": ""}) == [task_rule.FILTER_NEEDS_TEXT]
    assert await _stored(db, 7) == ("filter", [], "#task")
    # A first save asking for filter without text.
    assert await _save(db, 8, {"task_rule": "filter", "global_filter": ""}) == [
        task_rule.FILTER_NEEDS_TEXT]
    assert await _stored(db, 8) is None
    # Unused values are kept; default stores NULL; tags normalised.
    assert await _save(db, 7, {"task_rule": "default", "task_tags": "#Someday\nwaiting-for"}) == []
    assert await _stored(db, 7) == (None, ["someday", "waiting-for"], "#task")
    # A rule change applies on the next call.
    async with db() as session:
        assert (await task_settings.effective(session, 7)).task_rule == "all"
    assert await _save(db, 7, {"task_rule": "fields"}) == []
    async with db() as session:
        e = await task_settings.effective(session, 7)
    assert (e.task_rule, e.task_tags) == ("fields", ("someday", "waiting-for"))


async def test_concurrent_first_saves_never_leave_filter_without_text(db):
    for attempt in range(8):
        async with db() as session:
            await session.execute(text("DELETE FROM task_settings"))
            await session.commit()
        forms = [{"task_rule": "filter", "global_filter": "#task"}, {"global_filter": ""}]
        if attempt % 2:
            forms.reverse()
        results = await asyncio.gather(*(_save(db, 7, f) for f in forms))
        stored = await _stored(db, 7)
        assert stored is not None
        assert not (stored[0] == "filter" and stored[2] is None), stored
        for errors in results:
            assert errors in ([], [task_rule.FILTER_NEEDS_TEXT])
        if stored == ("filter", [], "#task"):
            # Either the blank save went first (both saved) or it was refused.
            assert results.count([task_rule.FILTER_NEEDS_TEXT]) <= 1


async def test_a_check_violation_at_flush_is_the_same_refusal(db, monkeypatch):
    """The last line of defence: with the merged check defeated, the CHECK
    rejects the write at flush; `save` rolls back to its savepoint and
    returns the readable refusal, storing nothing."""
    monkeypatch.setattr(task_settings.task_rule, "RULE_FILTER", "never-matches")
    assert await _save(db, 7, {"task_rule": "filter", "global_filter": ""}) == [
        task_rule.FILTER_NEEDS_TEXT]
    assert await _stored(db, 7) is None
    assert await _save(db, 7, {"task_rule": "fields"}) == []
    assert await _save(db, 7, {"task_rule": "filter"}) == [task_rule.FILTER_NEEDS_TEXT]
    assert await _stored(db, 7) == ("fields", [], None)


@pytest.mark.parametrize("value,fragment", [("%%x%%", "%%"), ("<!-- task -->", "HTML comment")])
async def test_a_balanced_comment_is_refused_as_a_filter(db, value, fragment):
    """Codex spec r2: no comment delimiter at all, balanced or not."""
    errors = await _save(db, 7, {"task_rule": "filter", "global_filter": value})
    assert len(errors) == 1 and fragment in errors[0] and "comment marker" in errors[0]
    assert await _stored(db, 7) is None
