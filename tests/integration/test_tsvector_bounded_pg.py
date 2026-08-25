"""#127 / D4 — the keyword vector attempts the full note and retreats per note.

Both tsvector writers used to bind `content[:100000]` unconditionally, so every
term past 100,000 characters was invisible to `keyword_search` — for a note the
tool reported on, with no indication it had only been half read. Removing the
slice is not by itself the fix: PostgreSQL rejects a tsvector larger than 1 MiB
and an uncaught statement error aborts the pass transaction, so nothing
commits, no content hash advances, and the same fatal batch is retried on every
tick for ever (the #126 freeze class).

**This module has to run against a real PostgreSQL**, and that is a
requirement, not a preference. What is under test is the *driver's* behaviour:
that a genuine statement failure inside `session.begin_nested()` unwinds
through the context manager's rollback and leaves the outer transaction usable,
so the next attempt and every later statement still work. A mocked savepoint
returns whatever the mock returns and proves none of that — an earlier draft
caught the error *inside* the `async with` and passed every mocked test while
leaving asyncpg's aborted-transaction state set on the real thing.

Skipped unless `PGVECTOR_TEST_ADMIN_URL` is set — see `_harness.py`.
"""
import hashlib

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import src.database
from src.config import settings
from src.models.db import NoteMetadata
from src.services import indexer
from src.services.fts import index_tsvector_sql
import _harness

pytestmark = [
    _harness.requires_pgvector,
    pytest.mark.asyncio(loop_scope="module"),
]

DIM = 8

# Distinct 24-character tokens. A tsvector's size is the size of its *lexeme*
# data, so unique long words are what pushes it past the 1 MiB limit; repeating
# one word would produce a tiny tsvector out of any amount of text.
def unique_words(n: int, seed: str = "w") -> str:
    return " ".join(f"{seed}{i:018d}zz" for i in range(n))


# ~90k distinct tokens ≈ 2.2 MB of lexemes: comfortably over the limit at full
# length, comfortably under it at the 100,000-character floor (~4,000 tokens).
OVERSIZED = unique_words(90_000)
NEEDLE = "distinctivemarkerterm"
NORMAL = f"a short ordinary note that mentions {NEEDLE} once\n"


@pytest.fixture(scope="module")
def migrated_url():
    yield from _harness.throwaway_database("tsvector_bounded_127", DIM)


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
    monkeypatch.setattr(indexer.settings, "vault_path", str(root), raising=False)
    monkeypatch.setattr(indexer, "async_session", sessionmaker)
    monkeypatch.setattr(src.database, "async_session", sessionmaker)
    monkeypatch.setattr(indexer, "_is_paused", lambda: False)

    async def permitted(*_a, **_k):
        return True

    monkeypatch.setattr(indexer, "_ancillary_pass_is_permitted", permitted)

    async with sessionmaker() as session:
        await session.execute(text("DELETE FROM note_links"))
        await session.execute(text("DELETE FROM note_embeddings"))
        await session.execute(text("DELETE FROM notes_metadata"))
        await session.commit()

    yield root


async def tsvector_state(sessionmaker, path: str):
    """`(present, length)` of a note's stored tsvector."""
    async with sessionmaker() as session:
        row = (await session.execute(text(
            "SELECT content_tsvector IS NOT NULL, "
            "       coalesce(length(content_tsvector::text), 0) "
            "FROM notes_metadata WHERE file_path = :p"
        ), {"p": path})).first()
    return (row[0], row[1]) if row else (None, None)


async def matches(sessionmaker, term: str) -> set[str]:
    async with sessionmaker() as session:
        rows = (await session.execute(text(
            "SELECT file_path FROM notes_metadata "
            "WHERE content_tsvector @@ plainto_tsquery('english', :t)"
        ), {"t": term})).fetchall()
    return {r[0] for r in rows}


# ── the incremental pass ────────────────────────────────────────────────────
async def test_a_pathological_note_retreats_alone_and_the_pass_commits(
    sessionmaker, vault, caplog
):
    """The failing input, and the whole point of the savepoint.

    The oversized note's full-content statement genuinely fails on the server.
    The retreat must leave the outer transaction usable, so the *other* note
    indexed after it in the same pass still commits — which is what a caught
    error inside the savepoint block would have destroyed, taking the whole
    pass with it.
    """
    (vault / "Huge.md").write_text(OVERSIZED, encoding="utf-8")
    (vault / "Normal.md").write_text(NORMAL, encoding="utf-8")

    with caplog.at_level("WARNING", logger="src.services.indexer"):
        await indexer.index_vault(user_id=None)

    huge_present, huge_len = await tsvector_state(sessionmaker, "Huge.md")
    normal_present, _ = await tsvector_state(sessionmaker, "Normal.md")
    assert huge_present is True, "the pathological note is indexed at a prefix"
    assert normal_present is True, "the pass must still commit the other notes"
    assert "Normal.md" in await matches(sessionmaker, NEEDLE)

    retreats = [r for r in caplog.records if "retreating to a" in r.getMessage()]
    assert retreats, "every retreat must be logged with the prefix length"
    assert "characters" in retreats[0].getMessage()

    # And the row really did commit — a fresh session sees it.
    async with sessionmaker() as session:
        n = (await session.execute(
            select(NoteMetadata.id).where(NoteMetadata.file_path == "Normal.md")
        )).scalar_one_or_none()
    assert n is not None


async def test_a_term_past_the_old_slice_becomes_searchable(sessionmaker, vault):
    """The defect the removal of the slice fixes: a note whose distinctive term
    sits past 100,000 characters and whose full build succeeds."""
    body = unique_words(6_000, seed="pad") + f" {NEEDLE}beyond\n"
    assert len(body) > 120_000
    (vault / "Long.md").write_text(body, encoding="utf-8")

    await indexer.index_vault(user_id=None)

    assert "Long.md" in await matches(sessionmaker, f"{NEEDLE}beyond")


async def test_a_floor_failure_aborts_the_pass_with_nothing_committed(
    sessionmaker, vault, caplog
):
    """The terminal behaviour is deliberately unchanged from before the change.

    A statement that fails at the 100,000-character floor also failed at
    `content[:100000]` before it. The pass aborts, nothing commits, and the
    note's `content_hash` therefore does not advance — so the next tick retries
    it rather than leaving a committed hash beside a stale keyword vector,
    which is what the first draft's skip list would have done, permanently.
    """
    async with sessionmaker() as session:
        await session.execute(text("DELETE FROM note_links"))
        await session.execute(text("DELETE FROM notes_metadata"))
        await session.commit()
    for f in vault.iterdir():
        f.unlink()

    # A NUL byte: PostgreSQL text cannot carry one, so the statement fails at
    # every prefix length including the floor. A genuine driver-level failure,
    # not a monkeypatched one.
    (vault / "Bad.md").write_text("short body \x00 with a nul\n", encoding="utf-8")
    (vault / "Good.md").write_text(NORMAL, encoding="utf-8")

    with caplog.at_level("ERROR", logger="src.services.indexer"):
        with pytest.raises(Exception):
            await indexer.index_vault(user_id=None)

    # The failure really is the floor attempt, not something upstream of it.
    assert [
        r for r in caplog.records
        if "at or below the 100000-character floor" in r.getMessage()
    ], [r.getMessage() for r in caplog.records]

    async with sessionmaker() as session:
        rows = (await session.execute(select(NoteMetadata.file_path))).all()
    assert rows == [], "a floor failure must leave the pass with nothing committed"


# ── the full rebuild ────────────────────────────────────────────────────────
def content_hash(body: str) -> str:
    """Exactly what the indexer records — the hash of the *whole* file.

    The rebuild now verifies the bytes it reads against the row's
    `content_hash` before it writes anything, so a fixture that invents hashes
    would exercise nothing but the abort path.
    """
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


async def _seed_many(
    sessionmaker, root, count: int, bad_index: int | None,
    overrides: dict[int, str] | None = None,
):
    """`count` indexed notes on disk and in the table, tsvectors left NULL.

    `bad_index` names the one note whose body cannot be turned into a tsvector
    at any length. It sits **past 500** so the failure lands where the removed
    intermediate commit used to be. `overrides` replaces individual bodies, with
    the row's `content_hash` following the file — the two must agree or the
    rebuild refuses to certify the row.
    """
    overrides = overrides or {}
    async with sessionmaker() as session:
        await session.execute(text("DELETE FROM note_links"))
        await session.execute(text("DELETE FROM notes_metadata"))
        await session.commit()
    for f in root.iterdir():
        f.unlink()

    async with sessionmaker() as session:
        for i in range(count):
            name = f"n{i:04d}.md"
            if i in overrides:
                body = overrides[i]
            elif i == bad_index:
                body = "body with a nul \x00 in it\n"
            else:
                body = f"ordinary body number {i} mentioning {NEEDLE}\n"
            (root / name).write_text(body, encoding="utf-8")
            session.add(NoteMetadata(
                user_id=None, file_path=name, title=name[:-3], tags=[],
                frontmatter={}, content_hash=content_hash(body),
            ))
        await session.commit()


async def test_a_floor_failure_past_the_old_commit_boundary_rolls_everything_back(
    sessionmaker, vault
):
    """`rebuild_tsvectors` is atomic (D4).

    It used to commit every 500 notes, so a floor failure a thousand notes in
    left the keyword index half-rebuilt: the first N notes under the new
    `FTS_CONFIGS`, the rest under the old one, with no periodic pass that would
    ever repair them — an unchanged tsvector is never re-selected. The failing
    note here sits at index 550, past that boundary, and *no* note's tsvector
    may survive the rollback.
    """
    await _seed_many(sessionmaker, vault, 600, bad_index=550)

    async with sessionmaker() as session:
        with pytest.raises(Exception):
            await indexer.rebuild_tsvectors(session, user_id=None)

    async with sessionmaker() as session:
        written = (await session.execute(text(
            "SELECT count(*) FROM notes_metadata WHERE content_tsvector IS NOT NULL"
        ))).scalar_one()
    assert written == 0, (
        "a floor failure must roll the whole rebuild back, including the notes "
        "the removed 500-note commit would have made durable"
    )


async def test_a_successful_rebuild_commits_every_note(sessionmaker, vault):
    """The other half of atomicity: with no failure, one commit at the end
    still writes every row — the removed intermediate commits were not what
    made the work durable."""
    await _seed_many(sessionmaker, vault, 600, bad_index=None)

    async with sessionmaker() as session:
        updated = await indexer.rebuild_tsvectors(session, user_id=None)
    assert updated == 600

    async with sessionmaker() as session:
        written = (await session.execute(text(
            "SELECT count(*) FROM notes_metadata WHERE content_tsvector IS NOT NULL"
        ))).scalar_one()
    assert written == 600


async def test_the_rebuild_retreats_per_note_and_still_commits_the_rest(
    sessionmaker, vault, caplog
):
    """The rebuild shares the helper, so it inherits the retreat — and the
    savepoint must leave its outer transaction usable for the notes after it,
    right through to the single final commit."""
    await _seed_many(
        sessionmaker, vault, 3, bad_index=None, overrides={1: OVERSIZED}
    )

    with caplog.at_level("WARNING", logger="src.services.indexer"):
        async with sessionmaker() as session:
            updated = await indexer.rebuild_tsvectors(session, user_id=None)

    assert updated == 3
    assert [r for r in caplog.records if "retreating to a" in r.getMessage()]

    async with sessionmaker() as session:
        written = (await session.execute(text(
            "SELECT count(*) FROM notes_metadata WHERE content_tsvector IS NOT NULL"
        ))).scalar_one()
    assert written == 3, "the notes after the retreat must still commit"


# ── the rebuild certifies what it writes ────────────────────────────────────
#
# The rebuild snapshots the table once and then reads the vault note by note,
# so both the rows and the files keep moving underneath it. What makes those
# races *permanent* rather than transient is that a keyword vector is only ever
# rewritten again when a note's `content_hash` changes: both move paths preserve
# `content_tsvector`, and the ordinary scan skips a row whose hash is unchanged.
# So a row the rebuild steps over — or writes the wrong bytes into — stays on
# the old configuration with nothing that would ever revisit it.


async def _rebuild(sessionmaker):
    async with sessionmaker() as session:
        return await indexer.rebuild_tsvectors(session, user_id=None)


def _commit_concurrently(migrated_url, statements):
    """Run `statements` to completion on a **separate connection**, right now.

    A separate connection is the whole point: the race is with another
    transaction, and driving it on the rebuild's own session would prove nothing
    about what it can see under READ COMMITTED. It is a raw `asyncpg` connection
    rather than another SQLAlchemy session because the rebuild is mid-`await` on
    the engine's pool and a second loop cannot borrow from it — asyncpg
    autocommits outside an explicit transaction, so each statement lands.
    """
    import asyncio
    import threading

    dsn = _harness.asyncpg_dsn(migrated_url)

    async def _go():
        import asyncpg

        conn = await asyncpg.connect(dsn)
        try:
            for sql, args in statements:
                await conn.execute(sql, *args)
        finally:
            await conn.close()

    box: dict = {}

    def run():
        try:
            asyncio.run(_go())
        except BaseException as exc:  # pragma: no cover - re-raised below
            box["exc"] = exc

    thread = threading.Thread(target=run)
    thread.start()
    thread.join(30)
    if "exc" in box:
        raise box["exc"]


def _at_note(migrated_url, target: str, action):
    """Fire `action()` the instant the rebuild reads `target`.

    That is the interleaving both findings live in: after the rebuild has
    snapshotted the table, before it writes the row it is holding.
    """
    real_read = indexer.read_note_beneath
    fired = {"done": False}

    def hooked(root_fd, rel_path):
        if rel_path == target and not fired["done"]:
            fired["done"] = True
            action()
        return real_read(root_fd, rel_path)

    return hooked, fired


async def test_a_row_that_moves_mid_rebuild_is_repaired_not_skipped(
    sessionmaker, vault, migrated_url, monkeypatch, caplog
):
    """FINDING 1. The snapshot holds the old path, the read fails, and the old
    code silently `continue`d — committing every other note and leaving the
    moved row on the previous `FTS_CONFIGS` for ever.

    Both move paths preserve `content_tsvector` and the scan skips an unchanged
    hash, so nothing would ever have revisited it.
    """
    await _seed_many(sessionmaker, vault, 5, bad_index=None)
    moved_body = (vault / "n0002.md").read_text(encoding="utf-8")

    def move_it():
        (vault / "n0002.md").rename(vault / "moved.md")
        _commit_concurrently(migrated_url, [(
            "UPDATE notes_metadata SET file_path = 'moved.md' "
            "WHERE file_path = 'n0002.md'", (),
        )])

    hooked, fired = _at_note(migrated_url, "n0002.md", move_it)
    monkeypatch.setattr(indexer, "read_note_beneath", hooked)
    with caplog.at_level("INFO", logger="src.services.indexer"):
        updated = await _rebuild(sessionmaker)
    monkeypatch.undo()

    assert fired["done"], "the move never landed; the test proves nothing"
    assert updated == 5, "every row must be written, the moved one included"
    assert [r for r in caplog.records if "moved or changed" in r.getMessage()]

    # The repaired row carries a vector, at its *new* path, and the hash it
    # was certified against is untouched.
    present, _ = await tsvector_state(sessionmaker, "moved.md")
    assert present is True, "the moved row must be rebuilt, not stepped over"
    async with sessionmaker() as session:
        stored = (await session.execute(text(
            "SELECT content_hash FROM notes_metadata WHERE file_path = 'moved.md'"
        ))).scalar_one()
    assert stored == content_hash(moved_body)

    async with sessionmaker() as session:
        written = (await session.execute(text(
            "SELECT count(*) FROM notes_metadata WHERE content_tsvector IS NOT NULL"
        ))).scalar_one()
    assert written == 5


async def test_a_stale_write_does_not_land_when_the_hash_advances(
    sessionmaker, vault, migrated_url, monkeypatch
):
    """FINDING 2. A concurrent index pass commits `content_hash(C2)` and
    `tsvector(C2)` between the rebuild's read of `C1` and its UPDATE.

    Addressed by `id` alone, the rebuild overwrote the vector with `tsvector(C1)`
    while the hash stayed `C2` — so every later scan skipped the row and its
    keyword vector described content the note does not have, permanently. The
    certified predicate makes that UPDATE match zero rows, and the re-read then
    rebuilds the row against `C2`.
    """
    await _seed_many(sessionmaker, vault, 5, bad_index=None)
    c2 = f"the replacement body mentioning {NEEDLE}c2 and nothing else\n"

    def advance_it():
        # Exactly what an index pass commits: new bytes, new hash, and the
        # keyword vector for those bytes.
        (vault / "n0002.md").write_text(c2, encoding="utf-8")
        _commit_concurrently(migrated_url, [(
            "UPDATE notes_metadata "
            "SET content_hash = $1, content_tsvector = to_tsvector('english', $2) "
            "WHERE file_path = 'n0002.md'", (content_hash(c2), c2),
        )])

    hooked, fired = _at_note(migrated_url, "n0002.md", advance_it)
    monkeypatch.setattr(indexer, "read_note_beneath", hooked)
    updated = await _rebuild(sessionmaker)
    monkeypatch.undo()

    assert fired["done"], "the concurrent commit never landed"
    assert updated == 5

    # The row describes C2 — its hash and its vector agree — and the stale C1
    # terms are nowhere in the index.
    async with sessionmaker() as session:
        stored = (await session.execute(text(
            "SELECT content_hash FROM notes_metadata WHERE file_path = 'n0002.md'"
        ))).scalar_one()
    assert stored == content_hash(c2)
    assert "n0002.md" in await matches(sessionmaker, f"{NEEDLE}c2")
    assert "n0002.md" not in await matches(sessionmaker, "number 2")


async def test_a_row_deleted_mid_rebuild_is_safely_absent(
    sessionmaker, vault, migrated_url, monkeypatch
):
    """The one outcome that is a legitimate skip: the row is gone, so there is
    nothing left in scope to leave stale."""
    await _seed_many(sessionmaker, vault, 5, bad_index=None)

    def delete_it():
        (vault / "n0002.md").unlink()
        _commit_concurrently(migrated_url, [(
            "DELETE FROM notes_metadata WHERE file_path = 'n0002.md'", (),
        )])

    hooked, fired = _at_note(migrated_url, "n0002.md", delete_it)
    monkeypatch.setattr(indexer, "read_note_beneath", hooked)
    updated = await _rebuild(sessionmaker)
    monkeypatch.undo()

    assert fired["done"]
    assert updated == 4, "the deleted row is not counted, and does not abort"
    async with sessionmaker() as session:
        written = (await session.execute(text(
            "SELECT count(*) FROM notes_metadata WHERE content_tsvector IS NOT NULL"
        ))).scalar_one()
    assert written == 4


async def test_an_unreadable_note_aborts_rather_than_committing_around_it(
    sessionmaker, vault
):
    """The row still records the path and hash the rebuild selected, so this is
    not a race a re-read can win: the file cannot be read and the scan will
    never rewrite this row's vector (its hash is unchanged). Aborting is the
    only outcome that does not leave one row on the old configuration for ever.
    """
    await _seed_many(sessionmaker, vault, 5, bad_index=None)
    (vault / "n0002.md").unlink()

    with pytest.raises(indexer.TsvectorRebuildAborted) as caught:
        await _rebuild(sessionmaker)
    assert "n0002.md" in str(caught.value)
    assert "rolled back" in str(caught.value)

    async with sessionmaker() as session:
        written = (await session.execute(text(
            "SELECT count(*) FROM notes_metadata WHERE content_tsvector IS NOT NULL"
        ))).scalar_one()
    assert written == 0, "nothing may be committed around a row it cannot certify"


async def test_bytes_that_do_not_hash_to_the_row_abort_the_rebuild(
    sessionmaker, vault
):
    """The other uncertifiable state, and the reason it cannot be a skip: a note
    edited and then reverted before the next index pass never changes its
    `content_hash`, so a row skipped here can stay on the old configuration
    permanently."""
    await _seed_many(sessionmaker, vault, 5, bad_index=None)
    (vault / "n0002.md").write_text("different bytes entirely\n", encoding="utf-8")

    with pytest.raises(indexer.TsvectorRebuildAborted) as caught:
        await _rebuild(sessionmaker)
    assert "no longer hash" in str(caught.value)

    async with sessionmaker() as session:
        written = (await session.execute(text(
            "SELECT count(*) FROM notes_metadata WHERE content_tsvector IS NOT NULL"
        ))).scalar_one()
    assert written == 0


async def test_a_row_that_keeps_changing_is_not_chased_for_ever(
    sessionmaker, vault, migrated_url, monkeypatch
):
    """The re-read is bounded. A row being rewritten in a tight loop aborts the
    rebuild rather than spinning inside it."""
    await _seed_many(sessionmaker, vault, 3, bad_index=None)
    seen = {"n": 0}
    real_read = indexer.read_note_beneath

    def always_moving(root_fd, rel_path):
        if rel_path == "n0001.md" or rel_path.startswith("spin-"):
            seen["n"] += 1
            # Rename the row every time it is looked at, so every re-read finds
            # a fresh path and the read fails again.
            _commit_concurrently(migrated_url, [(
                "UPDATE notes_metadata SET file_path = $1 WHERE file_path = $2",
                (f"spin-{seen['n']}.md", rel_path),
            )])
            raise FileNotFoundError(rel_path)
        return real_read(root_fd, rel_path)

    monkeypatch.setattr(indexer, "read_note_beneath", always_moving)
    with pytest.raises(indexer.TsvectorRebuildAborted) as caught:
        await _rebuild(sessionmaker)
    monkeypatch.undo()

    assert "kept changing" in str(caught.value)
    assert seen["n"] == indexer.MAX_REBUILD_REREADS + 1


# ── the bound itself ────────────────────────────────────────────────────────
async def test_the_floor_is_exactly_the_pre_change_statement(sessionmaker, vault):
    """100,000 characters, exactly. It is not a tuning knob: it is the
    statement production ran before this change, which is what makes "a floor
    failure behaves exactly as before" a true statement rather than a hope."""
    assert indexer.TSVECTOR_CONTENT_FLOOR_CHARS == 100_000


async def test_the_retreat_halves_down_to_the_floor(sessionmaker, vault):
    """The arithmetic, against the real helper and a real savepoint: a
    statement that only succeeds below some length is retried at halved
    prefixes and never below the floor."""
    frag, params = index_tsvector_sql("content")
    stmt = text(
        f"UPDATE notes_metadata SET content_tsvector = {frag} "
        "WHERE file_path = :p"
    )
    await _seed_many(sessionmaker, vault, 1, bad_index=None)

    async with sessionmaker() as session:
        used, rowcount = await indexer.write_tsvector_bounded(
            session, stmt, OVERSIZED, {"p": "n0000.md", **params},
            label="n0000.md",
        )
        await session.commit()
    assert rowcount == 1

    assert indexer.TSVECTOR_CONTENT_FLOOR_CHARS <= used < len(OVERSIZED)
    # A halving sequence from the full length, so the value is one of them.
    lengths, n = [], len(OVERSIZED)
    while n > indexer.TSVECTOR_CONTENT_FLOOR_CHARS:
        lengths.append(n)
        n = max(n // 2, indexer.TSVECTOR_CONTENT_FLOOR_CHARS)
    lengths.append(indexer.TSVECTOR_CONTENT_FLOOR_CHARS)
    assert used in lengths, (used, lengths)

    present, _ = await tsvector_state(sessionmaker, "n0000.md")
    assert present is True
