"""`add_task`'s duplicate and similar-task scan against a real PostgreSQL
(#428, design D9, D12 default scope, D18 tenancy; tasks.md 5.4).

Rows are seeded directly with the D11 columns (`description_key`,
`text_fold`, the id triple) computed by the same `task_line` helpers the
extractor uses, so each scenario controls exactly what is indexed. What needs
the database: the owner predicate inside the join, the default-scope clause
(folder prefix with LIKE wildcards escaped, hierarchical note-tag exclusion
over `notes_metadata.tags`), `strpos` over `text_fold`, and the scan bound.

Skipped unless `PGVECTOR_TEST_ADMIN_URL` is set (see `_harness`).
"""
import dataclasses

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.services import task_line as tl
from src.services import task_settings
from src.services import task_write as tw
import _harness

pytestmark = [
    _harness.requires_pgvector,
    pytest.mark.asyncio(loop_scope="module"),
]

DIM = 8


@pytest.fixture(scope="module")
def migrated_url():
    yield from _harness.throwaway_database("task_duplicates_428", DIM)


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def sessionmaker(migrated_url):
    engine = create_async_engine(migrated_url, poolclass=None)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest_asyncio.fixture(loop_scope="module")
async def db(sessionmaker):
    async with sessionmaker() as session:
        for table in ("note_tasks", "note_links", "note_embeddings", "notes_metadata", "users"):
            await session.execute(text(f"DELETE FROM {table}"))
        for uid in (7, 8):
            await session.execute(text(
                "INSERT INTO users (id, username, password_hash) VALUES (:i, :u, 'x')"
            ), {"i": uid, "u": f"user{uid}"})
        await session.commit()
    yield sessionmaker


def S(**kw):
    return dataclasses.replace(task_settings.defaults(), **kw)


async def seed(maker, path, lines, *, uid=None, note_tags=None):
    """One note whose task rows are the given task lines (document order)."""
    parsed = [tl.parse(line) for line in lines]
    keys = [tl.task_key(p.full_text) for p in parsed]
    async with maker() as session:
        note_id = (await session.execute(text(
            "INSERT INTO notes_metadata (user_id, file_path, title, content_hash, extraction_version, tags) "
            "VALUES (:u, :p, 't', 'h', 5, :tags) RETURNING id"
        ), {"u": uid, "p": path, "tags": note_tags})).scalar_one()
        for i, (p, k) in enumerate(zip(parsed, keys), start=1):
            cut = p.full_text[:1000]
            await session.execute(text(
                "INSERT INTO note_tasks (note_id, line, status_char, state, text, task_key, key_ordinal, "
                " key_count, description_key, text_fold) "
                "VALUES (:n, :l, :c, :s, :t, :k, :o, :cnt, :dk, :tf)"
            ), {
                "n": note_id, "l": i, "c": p.status, "s": "done" if p.status in "xX" else ("cancelled" if p.status == "-" else "open"),
                "t": cut, "k": k, "o": keys[:i].count(k), "cnt": keys.count(k),
                "dk": tl.description_key(tl.normalise_description(p.full_text)),
                "tf": tl.fold_text(cut),
            })
        await session.commit()
    return note_id


async def scan(maker, uid, text_, settings=None, **kw):
    norm = tl.normalise_description(text_)
    async with maker() as session:
        return await tw.duplicate_scan(session, uid, settings or S(), norm, tw.similarity_terms(norm), **kw)


async def test_an_exact_duplicate_elsewhere_in_scope_is_found(db):
    await seed(db, "Projects/Alpha.md", ["- [ ] Renew passport #admin 📅 2026-12-01", "- [ ] other"])
    exact, similar, stopped = await scan(db, None, "renew  Passport #admin")
    assert [(r.path, r.line, r.id) for r in exact] == [
        ("Projects/Alpha.md", 1, tl.task_key("Renew passport #admin 📅 2026-12-01"))]
    assert similar == [] and stopped is False


async def test_done_tasks_and_the_target_path_are_not_exact_duplicates(db):
    await seed(db, "Done.md", ["- [x] Renew passport #admin"])
    await seed(db, "Inbox.md", ["- [ ] Renew passport #admin"])
    exact, _s, _ = await scan(db, None, "Renew passport #admin")
    assert [r.path for r in exact] == ["Inbox.md"]
    exact, _s, _ = await scan(db, None, "Renew passport #admin", exclude_path="Inbox.md")
    assert exact == []


async def test_identical_rows_render_suffixed_ids(db):
    await seed(db, "A.md", ["- [ ] Water plants", "- [ ] Water plants"])
    exact, _s, _ = await scan(db, None, "water plants")
    k = tl.task_key("Water plants")
    assert [r.id for r in exact] == [f"{k}.1of2", f"{k}.2of2"]


async def test_a_similar_task_is_listed_not_exact(db):
    await seed(db, "Trips.md", ["- [ ] Renew passport soon", "- [ ] Buy milk"])
    exact, similar, stopped = await scan(db, None, "Renew passport")
    assert exact == []
    assert [(r.path, r.text) for r in similar] == [("Trips.md", "Renew passport soon")]
    assert similar[0].score == pytest.approx(2 / 3)
    assert stopped is False


async def test_a_similar_task_is_listed_by_containment(db):
    """D9: `Renew passport` is contained in `Renew passport for the trip`
    (Jaccard 2/5), so it is listed — and containment ranks first."""
    await seed(db, "A.md", ["- [ ] Renew"])  # Jaccard 1/2; one term, so never by containment
    await seed(db, "B.md", ["- [ ] Renew passport for the trip"])          # contained
    _e, similar, _ = await scan(db, None, "Renew passport")
    assert [r.path for r in similar] == ["B.md", "A.md"]
    assert similar[0].score == pytest.approx(0.4)


async def test_containment_in_the_other_direction(db):
    await seed(db, "Short.md", ["- [ ] Renew passport"])
    _e, similar, _ = await scan(db, None, "Renew passport for the trip")
    assert [r.path for r in similar] == ["Short.md"]


async def test_a_one_term_description_never_matches_by_containment(db):
    await seed(db, "One.md", ["- [ ] passport"])
    await seed(db, "Many.md", ["- [ ] passport renewal for the whole family"])
    _e, similar, _ = await scan(db, None, "passport")
    assert similar == []
    _e, similar, _ = await scan(db, None, "passport renewal for the whole family trip")
    assert [r.path for r in similar] == ["Many.md"]  # contained (6 of 7 terms); `One.md` (one term) is not listed


async def test_similar_matching_is_case_and_unicode_folded(db):
    await seed(db, "Ru.md", ["- [ ] Позвонить Ивану завтра"])
    _e, similar, _ = await scan(db, None, "позвонить ивану")
    assert [r.path for r in similar] == ["Ru.md"]


async def test_the_scan_bound_is_reported(db, monkeypatch):
    monkeypatch.setattr(tw, "TASK_SIMILAR_SCAN_MAX", 3)
    await seed(db, "Many.md", [f"- [ ] report item {k}" for k in range(6)])
    _e, similar, stopped = await scan(db, None, "report item")
    assert stopped is True
    assert len(similar) == 3
    _e, _s, stopped = await scan(db, None, "something unrelated")
    assert stopped is False


async def test_duplicate_checks_never_cross_owners(db):
    await seed(db, "Shared.md", ["- [ ] Call the bank"], uid=8)
    await seed(db, "Other.md", ["- [ ] Call the bank today"], uid=8)
    exact, similar, _ = await scan(db, 7, "Call the bank")
    assert exact == [] and similar == []
    exact, similar, _ = await scan(db, None, "Call the bank")
    assert exact == [] and similar == []
    exact, similar, _ = await scan(db, 8, "Call the bank")
    assert [r.path for r in exact] == ["Shared.md"] and [r.path for r in similar] == ["Other.md"]


async def test_the_default_scope_excludes_folders_and_note_tags(db):
    await seed(db, "Archive/Old.md", ["- [ ] Renew passport"])
    await seed(db, "Archive_2/Keep.md", ["- [ ] Renew passport"])
    await seed(db, "Tagged.md", ["- [ ] Renew passport"], note_tags=["Project/Archived"])
    await seed(db, "Near.md", ["- [ ] Renew passport"], note_tags=["projectx"])
    await seed(db, "Hash.md", ["- [ ] Renew passport"], note_tags=["Project//Archived/"])
    scoped = S(excluded_folders=("Archive/",), excluded_note_tags=("project",))
    exact, _s, _ = await scan(db, None, "Renew passport", settings=scoped)
    # `Archive_2/` is not under `Archive/` (the `_` is not a wildcard);
    # `projectx` is not beneath `project` (segment rule, never substring).
    # (Order is the database collation's `(file_path, line)`; compared as sets.)
    assert sorted(r.path for r in exact) == ["Archive_2/Keep.md", "Near.md"]
    exact, _s, _ = await scan(db, None, "Renew passport", settings=S(excluded_note_tags=("project/archived",)))
    assert sorted(r.path for r in exact) == ["Archive/Old.md", "Archive_2/Keep.md", "Near.md"]
    exact, _s, _ = await scan(db, None, "Renew passport")
    assert len(exact) == 5


async def test_rows_awaiting_the_backfill_have_no_id(db):
    async with db() as session:
        note_id = (await session.execute(text(
            "INSERT INTO notes_metadata (user_id, file_path, title, content_hash, extraction_version) "
            "VALUES (NULL, 'Legacy.md', 't', 'h', 4) RETURNING id"))).scalar_one()
        norm = tl.normalise_description("Pay rent")
        await session.execute(text(
            "INSERT INTO note_tasks (note_id, line, status_char, state, text, description_key, text_fold) "
            "VALUES (:n, 3, ' ', 'open', 'Pay rent', :dk, 'pay rent')"),
            {"n": note_id, "dk": tl.description_key(norm)})
        await session.commit()
    exact, _s, _ = await scan(db, None, "Pay rent")
    assert [(r.path, r.line, r.id) for r in exact] == [("Legacy.md", 3, None)]
